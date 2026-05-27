"""Wallet-history Judge Worker — example independent Worker for the role-split.

Polls the IQX dispatcher for open Boss tasks published by smart_money under
`verification_method=worker_prediction_accuracy_4h`, claims them, and submits
a structured verdict `{is_alpha, confidence, reasoning, evidence_tx,
predicted_4h_return_pct}`.

The verdict is informed by two **optional** complementary signals — when
their cache files are available under ``STATE_DIR``, the Judge uses them
to upgrade its verdict above the default-skeptical baseline:

  - **bot-army membership** (``STATE_DIR/bot_army.json``, produced by an
    offline compute script not shipped with the SDK) — high-confidence
    "fake alpha" verdict when the source wallet is part of a coordinated
    buy network.
  - **historical PnL per token** (``STATE_DIR/wallet_pnl.json``, produced
    by an offline compute script not shipped with the SDK) — supports an
    alpha verdict when the wallet has a positive realized PnL track
    record on the target token.

**Graceful degrade when caches are absent**: ``_load_json_cache`` returns
an empty dict when the file is missing or malformed (see its docstring
below), so a Judge running without the bot-army / wallet-PnL caches
still claims open tasks and submits verdicts — every task lands in the
default-skeptical path (``is_alpha=False``, ``confidence=0.5``,
``predicted_4h_return_pct=0.0``). This is useful for plumbing tests,
baseline-rate measurement, and as a starting point for external Workers
who want to swap in their own signals by editing ``build_verdict``
without touching the dispatcher / claim / submit loop.

Neither signal is consulted by `smart_money`'s cluster detector itself,
which is the point: the Judge must reach for signals the Boss does not, or
it would be cluster-detection-with-a-different-filename (the role-split's
anti-pattern guard).

The verdict the verifier (`worker_prediction_accuracy_4h`) grades is the
Worker's **prediction accuracy**, not the cluster's correctness. A correct
"is_alpha=False" call when the price stays flat earns ELO just like a
correct "is_alpha=True" call when the price moves.

Usage (module form is canonical):
    python3 -m iqx.examples.worker_judge --once       # one pass + exit (default)
    python3 -m iqx.examples.worker_judge --loop       # poll forever
    python3 -m iqx.examples.worker_judge --dry-run    # print verdicts, no POST
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from iqx.helpers.state import resolve_state_dir

# ---- agent identity ----------------------------------------------------------

WORKER_ID = "wallet-history-judge-v1"
WORKER_NAME = "Wallet History Judge"

# Tasks we'll claim are filtered down by:
#   - publisher_id (the Boss whose stream we grade)
#   - verification_method (the verifier we know how to satisfy)
# Both are configurable via CLI for easy retargeting / future Boss agents.
DEFAULT_PUBLISHER_ID = "smart-money-monitor-v1"
DEFAULT_VERIFICATION_METHOD = "worker_prediction_accuracy_4h"

POLL_INTERVAL_SEC = 5 * 60   # 5 min — same cadence as the Boss
REQUEST_TIMEOUT_SEC = 20

BASE_URL = os.environ.get("IQX_BASE_URL", "http://localhost:8000")

# ---- paths -------------------------------------------------------------------

# STATE_DIR resolution policy: see iqx.helpers.state.resolve_state_dir().
# Three files live under it:
#   - ``wallet_history_judge.key`` — dispatcher credential for
#     WORKER_ID. Basename is stable across deployments so the existing
#     credential remains valid; no re-registration on restart.
#   - ``wallet_pnl.json`` — produced by an offline compute script.
#   - ``bot_army.json``   — produced by an offline compute script.
# Both cache producers stay operator-private; only this consumer ships
# in the SDK examples.
STATE_DIR = resolve_state_dir()
KEY_PATH = STATE_DIR / "wallet_history_judge.key"
WALLET_PNL_PATH = STATE_DIR / "wallet_pnl.json"
BOT_ARMY_PATH = STATE_DIR / "bot_army.json"

# ---- verdict thresholds ------------------------------------------------------

# Tuned for the three-quadrant logic in build_verdict: bot-army=0.85,
# alpha-with-history=0.7, default-skeptical=0.5.
ALPHA_WIN_RATE_FLOOR = 0.6
ALPHA_PREDICTED_RETURN_PCT = 5.0
DEFAULT_PREDICTED_RETURN_PCT = 0.0

CONFIDENCE_BOT_ARMY = 0.85
CONFIDENCE_ALPHA_WITH_HISTORY = 0.7
CONFIDENCE_DEFAULT = 0.5


# ---- agent identity / auth ---------------------------------------------------


def _register() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    resp = requests.post(
        f"{BASE_URL}/agents/register",
        json={"id": WORKER_ID, "name": WORKER_NAME},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    if resp.status_code == 409:
        # The dispatcher does not silently rotate an existing agent's key
        # via /agents/register — a 409 here means either we lost our local
        # key file, or another caller registered under our id. We can't
        # recover via POST /agents/{id}/rotate-key from this code path
        # because that endpoint requires the current key, which is exactly
        # what we lack. Surface clearly and crash; auto-recovery here would
        # be the DoS surface the dispatcher's auth model is designed to close.
        print(
            f"[registry] {WORKER_ID} already registered with a different "
            f"api_key. If you lost the local key file, ask an admin to "
            f"delete the agent row (then this worker will register fresh "
            f"on next start); if this is a fresh deploy clashing with a "
            f"stale id, choose a new WORKER_ID. Crashing — no silent "
            f"recovery.",
            flush=True,
        )
    resp.raise_for_status()
    api_key = resp.json()["api_key"]
    KEY_PATH.write_text(api_key)
    print(f"[registry] registered {WORKER_ID}; api_key saved to {KEY_PATH}",
          flush=True)
    return api_key


def _cached_key_authenticates(key: str) -> bool:
    """True iff `key` still authenticates as WORKER_ID on the dispatcher.

    Replaces the older _agent_exists_on_server() check, which only
    confirmed the row existed — not that our cached key still matched
    it. The old check let a stale cached key persist silently across any
    legitimate DB-state divergence (an explicit `/agents/{id}/rotate-key`
    issued by another operator process, a dispatcher DB restore / reset,
    a prior-deployment legacy rotation, or any external drift between
    the on-disk key file and the dispatcher's stored value), so the
    worker would 403 on every /claim with no diagnostic signal. The
    dispatcher's `POST /agents/register` does NOT rotate existing keys
    (duplicate id returns 409 — see register_agent in main.py); rotation
    requires authenticated `POST /agents/{id}/rotate-key`.

    Network failures return True so the real error surfaces on the next
    HTTP call rather than triggering a spurious re-register storm.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/agents/me",
            headers={"X-Worker-Id": WORKER_ID, "X-API-Key": key},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException:
        return True
    return resp.status_code == 200


def ensure_registered() -> str:
    """Re-register if our cached credential no longer works.

    Two paths to re-register:
      - KEY_PATH missing entirely (fresh install, deleted cache).
      - Cached key fails the /agents/me round-trip (dispatcher rotated it,
        DB was reset and re-populated by another caller, etc.).
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        key = KEY_PATH.read_text().strip()
        if key and _cached_key_authenticates(key):
            return key
        if key:
            print(f"[registry] cached key no longer authenticates for "
                  f"{WORKER_ID} (rotated or dispatcher state changed); "
                  f"re-registering", flush=True)
    return _register()


# ---- cache loading -----------------------------------------------------------


def _load_json_cache(path: Path, label: str) -> dict:
    """Read a JSON cache file. Missing or malformed files are fine — the Judge
    just falls into the default-skeptical path for every task. Better to grade
    cautiously than to crash."""
    if not path.exists():
        print(f"[judge] {label} cache absent at {path}; default-skeptical path "
              f"will apply to all tasks", flush=True)
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"[judge] WARN {label} cache at {path} is not valid JSON ({e}); "
              f"treating as empty", file=sys.stderr, flush=True)
        return {}
    if not isinstance(raw, dict):
        print(f"[judge] WARN {label} cache at {path} is not a JSON object; "
              f"treating as empty", file=sys.stderr, flush=True)
        return {}
    return raw


# ---- verdict logic -----------------------------------------------------------


def build_verdict(spec: dict, wallet_pnl: dict, bot_army: dict) -> dict:
    """Return the Worker submission shape the verifier
    (`worker_prediction_accuracy_4h`) expects.

    Three-quadrant logic in priority order:
      1. Bot-army membership → not alpha (high confidence, regardless of token)
      2. Strong historical PnL on this token → alpha (medium confidence)
      3. No priors → not alpha (low confidence — default skeptical)

    Bot-army takes precedence over PnL because being part of a coordinated
    network is a stronger negative signal than any single-token PnL is a
    positive one. A bot-army wallet with one profitable token in its
    history is still a coordinated buyer — its PnL on that one token is
    likely the bot-army's coordinated success, not independent skill.
    """
    wallet = (spec.get("wallet") or "").lower()
    token = (spec.get("token_address") or "").lower()
    token_symbol = spec.get("token_symbol") or (token[:8] + "…" if token else "?")

    # 1. Bot-army check.
    bot_entry = bot_army.get(wallet) if wallet else None
    if isinstance(bot_entry, dict) and bot_entry.get("is_bot_army"):
        cluster_size = bot_entry.get("cluster_size", "?")
        target = (bot_entry.get("target_token") or "").lower()
        target_short = target[:8] + "…" if target else "?"
        same_token = bool(target) and target == token
        return {
            "is_alpha": False,
            "confidence": CONFIDENCE_BOT_ARMY,
            "reasoning": (
                f"wallet is in {cluster_size}-wallet bot army "
                f"(originally caught targeting {target_short}; "
                f"{'same target' if same_token else 'different target'} as this signal)"
            ),
            "evidence_tx": [],
            "predicted_4h_return_pct": DEFAULT_PREDICTED_RETURN_PCT,
        }

    # 2. PnL track record on this token.
    wallet_data = wallet_pnl.get(wallet) if wallet else None
    token_metrics = None
    if isinstance(wallet_data, dict):
        token_metrics = (wallet_data.get("tokens") or {}).get(token)
    if isinstance(token_metrics, dict):
        win_rate = float(token_metrics.get("win_rate") or 0.0)
        realized = float(token_metrics.get("realized_pnl_usd") or 0.0)
        n_sells = int(token_metrics.get("n_sells") or 0)
        if win_rate >= ALPHA_WIN_RATE_FLOOR and realized > 0:
            return {
                "is_alpha": True,
                "confidence": CONFIDENCE_ALPHA_WITH_HISTORY,
                "reasoning": (
                    f"wallet has {win_rate * 100:.0f}% historical win rate on "
                    f"{token_symbol} ({n_sells} closed sell{'s' if n_sells != 1 else ''}, "
                    f"${realized:,.0f} realized PnL)"
                ),
                "evidence_tx": [],
                "predicted_4h_return_pct": ALPHA_PREDICTED_RETURN_PCT,
            }

    # 3. Default skeptical.
    wallet_short = wallet[:8] + "…" if wallet else "?"
    return {
        "is_alpha": False,
        "confidence": CONFIDENCE_DEFAULT,
        "reasoning": (
            f"no PnL or bot-army priors for wallet {wallet_short} on "
            f"{token_symbol}; defaulting skeptical"
        ),
        "evidence_tx": [],
        "predicted_4h_return_pct": DEFAULT_PREDICTED_RETURN_PCT,
    }


# ---- HTTP glue ---------------------------------------------------------------


def fetch_open_tasks(publisher_id: str, verification_method: str) -> list[dict]:
    """Pull all OPEN tasks and filter client-side by publisher + method.

    Server-side filtering by these fields would be cleaner; the central
    node's list_tasks endpoint only supports `status` today (a small server
    change tracked as a follow-up). At expected production volume (~1-2
    Boss tasks/day) the client-side filter is trivially fast.
    """
    resp = requests.get(
        f"{BASE_URL}/tasks", params={"status": "open"},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    all_tasks = resp.json() or []
    return [
        t for t in all_tasks
        if t.get("publisher_id") == publisher_id
        and t.get("verification_method") == verification_method
    ]


def claim_task(task_id: str, api_key: str) -> bool:
    """Attempt to claim. Returns True on success; False on 400/409 (claim
    rejected for a recoverable reason) so the loop can move on. Anything
    else is raised.

    The two recoverable statuses come from main.py:claim_task:
      - 409: lost the atomic-claim race (task was OPEN when we read it
        but another Worker claimed before our UPDATE landed)
      - 400: either the task is no longer OPEN, or our ELO is below the
        task's min_elo gate. The dispatcher conflates these two; from
        the agent's perspective both mean "skip this task and move on."
    Logs distinguish the two so an operator can tell a high race rate
    (healthy multi-Worker contention) from an ELO mismatch (the Worker
    needs more ELO before it can grade higher-stake tasks)."""
    try:
        resp = requests.post(
            f"{BASE_URL}/tasks/{task_id}/claim",
            json={"worker_id": WORKER_ID},
            headers={"X-API-Key": api_key},
            timeout=REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return True
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 409:
            print(f"[judge] claim raced for {task_id[:8]} (HTTP 409); "
                  f"another worker got it", flush=True)
            return False
        if status == 400:
            print(f"[judge] claim rejected for {task_id[:8]} (HTTP 400); "
                  f"task no longer OPEN or worker ELO < task.min_elo",
                  flush=True)
            return False
        raise


def submit_verdict(task_id: str, verdict: dict, api_key: str) -> None:
    resp = requests.post(
        f"{BASE_URL}/tasks/{task_id}/submit",
        json={"worker_id": WORKER_ID, "result": json.dumps(verdict)},
        headers={"X-API-Key": api_key},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()


# ---- orchestration -----------------------------------------------------------


def grade_one_task(
    task: dict,
    *,
    api_key: Optional[str],
    wallet_pnl: dict,
    bot_army: dict,
    dry_run: bool,
) -> bool:
    """Process one Boss task: claim, build verdict, submit. Returns True on
    successful end-to-end; False on any expected non-fatal skip (malformed
    signal_data, claim race lost, etc.)."""
    task_id = task.get("id") or ""
    raw = task.get("signal_data")
    if not raw:
        print(f"[judge] task {task_id[:8]}: signal_data missing, skipping",
              flush=True)
        return False
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[judge] task {task_id[:8]}: signal_data invalid JSON, skipping",
              flush=True)
        return False

    verdict = build_verdict(spec, wallet_pnl, bot_army)
    pred_label = "alpha" if verdict["is_alpha"] else "no-alpha"
    wallet_short = (spec.get("wallet") or "")[:8] + "…"
    sym = spec.get("token_symbol") or (spec.get("token_address") or "")[:8] + "…"

    if dry_run:
        print(f"[judge] DRY-RUN task {task_id[:8]} ({wallet_short} → {sym}): "
              f"{pred_label} @ {verdict['confidence']} — {verdict['reasoning']}",
              flush=True)
        return True

    assert api_key is not None
    if not claim_task(task_id, api_key):
        return False
    submit_verdict(task_id, verdict, api_key)
    print(f"[judge] task {task_id[:8]} ({wallet_short} → {sym}): submitted "
          f"{pred_label} @ {verdict['confidence']} — {verdict['reasoning']}",
          flush=True)
    return True


def run_once(
    *,
    api_key: Optional[str],
    publisher_id: str,
    verification_method: str,
    dry_run: bool,
) -> int:
    """One pass over the open-task queue. Returns count of tasks graded."""
    wallet_pnl = _load_json_cache(WALLET_PNL_PATH, "wallet_pnl")
    bot_army = _load_json_cache(BOT_ARMY_PATH, "bot_army")

    try:
        eligible = fetch_open_tasks(publisher_id, verification_method)
    except requests.RequestException as e:
        print(f"[judge] failed to fetch open tasks: {e}", flush=True)
        return 0

    if not eligible:
        print(f"[judge] no eligible tasks (publisher={publisher_id}, "
              f"method={verification_method})", flush=True)
        return 0

    print(f"[judge] {len(eligible)} eligible task(s) — grading", flush=True)
    graded = 0
    for task in eligible:
        try:
            if grade_one_task(
                task,
                api_key=api_key,
                wallet_pnl=wallet_pnl,
                bot_army=bot_army,
                dry_run=dry_run,
            ):
                graded += 1
        except requests.RequestException as e:
            tid = task.get("id", "?")[:8]
            print(f"[judge] transient error on task {tid}: {e}", flush=True)
    return graded


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wallet-history Judge Worker — example independent Worker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--once", action="store_true",
                        help="One pass and exit (default)")
    parser.add_argument("--loop", action="store_true",
                        help=f"Poll forever every {POLL_INTERVAL_SEC}s")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print verdicts, do not register or POST")
    parser.add_argument("--publisher-id", default=DEFAULT_PUBLISHER_ID,
                        help=f"Boss whose tasks we grade (default {DEFAULT_PUBLISHER_ID})")
    parser.add_argument("--verification-method", default=DEFAULT_VERIFICATION_METHOD,
                        help=f"Method we know how to satisfy "
                             f"(default {DEFAULT_VERIFICATION_METHOD})")
    args = parser.parse_args()

    if args.dry_run:
        api_key = None
    else:
        api_key = ensure_registered()

    if args.loop:
        print(f"[judge] starting continuous loop (interval={POLL_INTERVAL_SEC}s, "
              f"publisher={args.publisher_id}, method={args.verification_method})",
              flush=True)
        while True:
            try:
                run_once(
                    api_key=api_key,
                    publisher_id=args.publisher_id,
                    verification_method=args.verification_method,
                    dry_run=args.dry_run,
                )
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                print(f"[judge] unexpected error: {e}", flush=True)
            time.sleep(POLL_INTERVAL_SEC)
    else:
        run_once(
            api_key=api_key,
            publisher_id=args.publisher_id,
            verification_method=args.verification_method,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
