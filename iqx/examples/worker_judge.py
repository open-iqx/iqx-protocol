"""Wallet-history Judge Worker — example independent Worker for the role-split.

**Side-effect class: Worker registration / submission.** Unless run with
``--dry-run`` it registers an Agent identity and submits answers.

**Uses the LEGACY single-claim path.** It calls ``POST /tasks/{id}/claim`` then
``POST /tasks/{id}/submit``, so the first Worker to claim a task locks the
others out, and the legacy path applies a provisional ELO change at submit time
rather than at grading time. Production Workers compete through
``POST /tasks/{id}/submissions`` instead. This example predates that path and is
kept on the legacy one for compatibility; see ``PROTOCOL.md`` for the current
lifecycle and do not treat this loop as the onboarding shape.

Polls the node for open Boss tasks published under
`verification_method=worker_prediction_accuracy_4h`, claims them, and submits
a structured verdict `{is_alpha, confidence, reasoning, evidence_tx,
predicted_4h_return_pct}`. The answer schema is documented in ``PROTOCOL.md``.

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

from iqx.examples.identity import (
    DEFAULT_BASE_URL,
    SideEffect,
    add_identity_args,
    announce_identities,
    guard_writes,
    key_path_for,
    resolve_agent_id,
    resolve_base_url,
    side_effect_epilog,
)
from iqx.helpers.state import resolve_state_dir

SIDE_EFFECT = SideEffect.WORKER

# ---- agent identity ----------------------------------------------------------

# Resolved per run: the default is freshly generated, so two consecutive runs
# use two distinct identities and neither collides with an id already registered
# on the target node. Pin one with --agent-id or $IQX_JUDGE_WORKER_ID.
WORKER_ID_PREFIX = "wallet-history-judge"
WORKER_ID_ENV = "IQX_JUDGE_WORKER_ID"
WORKER_NAME = "Wallet History Judge"

# Tasks we'll claim are filtered down by:
#   - publisher_id (the Boss whose stream we grade)
#   - verification_method (the verifier we know how to satisfy)
# Both are configurable via CLI for easy retargeting / future Boss agents.
DEFAULT_PUBLISHER_ID = "smart-money-monitor-v1"
DEFAULT_VERIFICATION_METHOD = "worker_prediction_accuracy_4h"

POLL_INTERVAL_SEC = 5 * 60   # 5 min — same cadence as the Boss
REQUEST_TIMEOUT_SEC = 20

# Resolved without validation at import time so importing the module can never
# fail on a malformed environment; ``main`` re-resolves it through
# ``resolve_base_url`` (which validates and fails safely) before any write.
BASE_URL = os.environ.get("IQX_BASE_URL", DEFAULT_BASE_URL)

# ---- paths -------------------------------------------------------------------

# STATE_DIR resolution policy: see iqx.helpers.state.resolve_state_dir().
# Three kinds of file live under it:
#   - ``<agent-id>.key``  — the node credential, derived from the resolved
#     agent id (see ``iqx.examples.identity.key_path_for``, which appends a
#     digest for ids that are not filename-safe). Pinning the id keeps the
#     same credential across restarts; a generated id mints a new one.
#   - ``wallet_pnl.json`` — produced by an offline compute script.
#   - ``bot_army.json``   — produced by an offline compute script.
# Both cache producers stay operator-private; only this consumer ships
# in the SDK examples.
STATE_DIR = resolve_state_dir()
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


def _register(worker_id: str) -> str:
    key_path = key_path_for(worker_id)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    resp = requests.post(
        f"{BASE_URL}/agents/register",
        json={"id": worker_id, "name": WORKER_NAME},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    if resp.status_code == 409:
        # The node does not silently rotate an existing agent's key via
        # /agents/register — a 409 here means either we lost our local key
        # file, or another caller registered under this id. We cannot recover
        # via POST /agents/{id}/rotate-key from this code path because that
        # endpoint requires the current key, which is exactly what is missing.
        # Surface clearly and crash; auto-recovery here would be the DoS
        # surface the node's auth model is designed to close.
        print(
            f"[registry] {worker_id} is already registered with a different "
            f"api_key. Re-run without pinning an id to generate a fresh one, "
            f"or restore the key file at {key_path}. Crashing — no silent "
            f"recovery. See TROUBLESHOOTING.md.",
            flush=True,
        )
    resp.raise_for_status()
    api_key = resp.json()["api_key"]
    key_path.write_text(api_key)
    print(f"[registry] registered {worker_id}; api_key saved to {key_path}",
          flush=True)
    return api_key


def _cached_key_authenticates(worker_id: str, key: str) -> bool:
    """True iff `key` still authenticates as `worker_id` on the node.

    Checked against ``GET /agents/me``, which requires the key. A public
    unauthenticated ``GET /agents/{id}`` would confirm only that the row exists
    — not that the cached key still matches it — so a stale cached key would
    persist silently across any legitimate state divergence (an explicit
    ``POST /agents/{id}/rotate-key`` issued by another process, a node DB
    restore or reset, or any drift between the on-disk key file and the stored
    value), and the Worker would then 403 on every write with no diagnostic
    signal. ``POST /agents/register`` does NOT rotate existing keys — a
    duplicate id returns 409; rotation requires the authenticated rotate
    endpoint. See ``PROTOCOL.md``.

    Network failures return True so the real error surfaces on the next
    HTTP call rather than triggering a spurious re-register storm.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/agents/me",
            headers={"X-Worker-Id": worker_id, "X-API-Key": key},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException:
        return True
    return resp.status_code == 200


def ensure_registered(worker_id: str) -> str:
    """Re-register if our cached credential no longer works.

    Two paths to re-register:
      - the key file for this identity is missing entirely (fresh install,
        deleted cache, or a newly generated agent id);
      - the cached key fails the /agents/me round-trip (it was rotated, or the
        node's state was reset and re-populated by another caller).
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key_path = key_path_for(worker_id)
    if key_path.exists():
        key = key_path.read_text().strip()
        if key and _cached_key_authenticates(worker_id, key):
            return key
        if key:
            print(f"[registry] cached key no longer authenticates for "
                  f"{worker_id} (rotated or node state changed); "
                  f"re-registering", flush=True)
    return _register(worker_id)


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


def claim_task(task_id: str, api_key: str, worker_id: str) -> bool:
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
            json={"worker_id": worker_id},
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


def submit_verdict(task_id: str, verdict: dict, api_key: str,
                   worker_id: str) -> None:
    resp = requests.post(
        f"{BASE_URL}/tasks/{task_id}/submit",
        json={"worker_id": worker_id, "result": json.dumps(verdict)},
        headers={"X-API-Key": api_key},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()


# ---- orchestration -----------------------------------------------------------


def grade_one_task(
    task: dict,
    *,
    api_key: Optional[str],
    worker_id: str,
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
    if not claim_task(task_id, api_key, worker_id):
        return False
    submit_verdict(task_id, verdict, api_key, worker_id)
    print(f"[judge] task {task_id[:8]} ({wallet_short} → {sym}): submitted "
          f"{pred_label} @ {verdict['confidence']} — {verdict['reasoning']}",
          flush=True)
    return True


def run_once(
    *,
    api_key: Optional[str],
    worker_id: str,
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
                worker_id=worker_id,
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
    global BASE_URL
    parser = argparse.ArgumentParser(
        description="Wallet-history Judge Worker — example independent Worker",
        epilog=side_effect_epilog(
            SIDE_EFFECT,
            "Uses the legacy single-claim path, which is NOT the path a "
            "competing-submissions Worker uses. See PROTOCOL.md.",
        ),
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
    add_identity_args(parser, env_var=WORKER_ID_ENV, prefix=WORKER_ID_PREFIX)
    args = parser.parse_args()

    BASE_URL = resolve_base_url()
    worker_id, source = resolve_agent_id(
        WORKER_ID_PREFIX, cli_value=args.agent_id, env_var=WORKER_ID_ENV,
    )
    identities = [("worker", worker_id, source)]

    if args.dry_run:
        # No write happens, so no opt-in is required — but still show exactly
        # which identity a real run would create.
        announce_identities(identities, base_url=BASE_URL,
                            side_effect=SIDE_EFFECT)
        api_key = None
    else:
        guard_writes(identities, base_url=BASE_URL, side_effect=SIDE_EFFECT,
                     cli_opt_in=args.allow_public_writes)
        api_key = ensure_registered(worker_id)

    if args.loop:
        print(f"[judge] starting continuous loop (interval={POLL_INTERVAL_SEC}s, "
              f"publisher={args.publisher_id}, method={args.verification_method})",
              flush=True)
        while True:
            try:
                run_once(
                    api_key=api_key,
                    worker_id=worker_id,
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
            worker_id=worker_id,
            publisher_id=args.publisher_id,
            verification_method=args.verification_method,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
