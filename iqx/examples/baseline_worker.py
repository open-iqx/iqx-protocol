"""Baseline / reference Worker — the SDK's default counter-party.

**Side-effect class: Worker registration / submission.** Unless run with
``--dry-run`` it registers an Agent identity and submits answers.

**Uses the LEGACY single-claim path.** It calls ``POST /tasks/{id}/claim`` then
``POST /tasks/{id}/submit``, so the first Worker to claim a task locks the
others out. Production Workers compete through ``POST /tasks/{id}/submissions``
instead. This example predates that path and is kept on the legacy one for
compatibility; see ``PROTOCOL.md`` for the current lifecycle and do not treat
this loop as the onboarding shape.

Purpose:
  Ship a small, deterministic Worker that runs out of the box with **zero
  side data** so external Boss authors get an immediate counter-party
  when they emit their first task. The intended flow: copy / modify a
  Boss example → emit a toy task → watch the baseline Worker claim and
  submit → watch the verifier close the loop. The same Worker is also
  the reference accuracy floor a future replay benchmark scores against.

Design choices:
  - **Capability vs. default scope.** ``SUPPORTED_METHODS`` is
    intentionally narrow — exactly ``echo`` (toy plumbing) and
    ``worker_prediction_accuracy_4h`` (the ``defi_alpha`` Boss shape).
    But the **default** ``--methods`` is ``echo`` only:
    ``DEFAULT_METHODS = ("echo",)``. Prediction tasks require explicit
    opt-in via ``--methods worker_prediction_accuracy_4h`` (or the
    comma-pair). See the safety note below.
  - **Default-skeptical verdict** for ``worker_prediction_accuracy_4h``:
    ``is_alpha=False``, ``confidence=0.5``,
    ``predicted_4h_return_pct=0.0``, no signal-conditional logic. The
    verifier passes only when the token moves ≥+3% in 4h
    (``iqx/verifier.py``), so a fixed-skeptical reply is a stable
    accuracy floor a future Worker can try to beat. Mirrors the
    ``worker_judge`` no-cache fallback path exactly.
  - **No off-chain caches.** Unlike ``worker_judge``, this Worker
    does not read ``bot_army.json`` / ``wallet_pnl.json``. It must
    be useful to a developer who just cloned the repo.
  - **Accepts default-eligible tasks from any publisher.** Use
    ``--publisher-id`` to narrow scope (useful for replay-benchmark
    targeting later).

Safety note — single-claim model:
  Current IQX tasks are single-claim / single-submission: the first
  Worker to call ``POST /tasks/{id}/claim`` wins, others get HTTP 409.
  If the baseline Worker auto-claims live prediction tasks, it can
  consume them before a smarter Worker sees them — and submit the
  default-skeptical verdict regardless of whether the signal is real
  alpha. That is the wrong default for live prediction tasks.

  Therefore: ``--methods`` defaults to ``echo`` only. Enable prediction
  mode (``--methods worker_prediction_accuracy_4h`` or both, comma-
  separated) only for:
    - The toy / demo Boss-onboarding loop (when the operator wants the
      baseline to grade their own published prediction tasks against
      themselves);
    - The replay benchmark (offline, against a frozen historical
      dataset — no live competition for tasks);
    - Intentional empty-market baseline runs (where the operator
      explicitly wants the baseline to be the only claimer).

Usage (module form is canonical):
    python3 -m iqx.examples.baseline_worker --once       # one pass + exit (default; echo only)
    python3 -m iqx.examples.baseline_worker --loop       # poll forever (echo only)
    python3 -m iqx.examples.baseline_worker --dry-run    # print intent, no POST
    python3 -m iqx.examples.baseline_worker --methods worker_prediction_accuracy_4h
                                                         # explicit prediction opt-in
    python3 -m iqx.examples.baseline_worker --methods echo,worker_prediction_accuracy_4h
                                                         # both, comma-separated
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
# use two distinct identities and neither collides with an id already
# registered on the target node. Pin one with --agent-id or $IQX_BASELINE_WORKER_ID.
WORKER_ID_PREFIX = "baseline-worker"
WORKER_ID_ENV = "IQX_BASELINE_WORKER_ID"
WORKER_NAME = "Baseline Reference Worker"

# Methods this Worker is *capable* of answering. Kept intentionally narrow —
# a future baseline_worker_v2 (or a distinct, named example) is the right
# place to expand; not here.
SUPPORTED_METHODS: tuple[str, ...] = (
    "echo",
    "worker_prediction_accuracy_4h",
)

# Methods this Worker auto-claims by *default*. Strictly a subset of
# SUPPORTED_METHODS. Echo-only because live prediction tasks are
# single-claim and the baseline should not silently steal them from
# smarter Workers — see the "Safety note — single-claim model" in the
# module docstring. Prediction mode is one CLI flag away
# (``--methods worker_prediction_accuracy_4h``) for the demo / replay /
# empty-market cases where that is the intent.
DEFAULT_METHODS: tuple[str, ...] = ("echo",)

POLL_INTERVAL_SEC = 5 * 60   # 5 min — match worker_judge / self_play
REQUEST_TIMEOUT_SEC = 20

# Resolved without validation at import time so importing the module can never
# fail on a malformed environment; ``main`` re-resolves it through
# ``resolve_base_url`` (which validates and fails safely) before any write.
BASE_URL = os.environ.get("IQX_BASE_URL", DEFAULT_BASE_URL)

# Verdict-shape constants for worker_prediction_accuracy_4h. Centralised so
# tests can import them and assert against the same values the runtime uses.
BASELINE_IS_ALPHA = False
BASELINE_CONFIDENCE = 0.5
BASELINE_PREDICTED_RETURN_PCT = 0.0
BASELINE_REASONING = (
    "baseline reference Worker: default-skeptical verdict with no off-chain "
    "signals (replay-benchmark accuracy floor)"
)

# ---- paths -------------------------------------------------------------------

# STATE_DIR resolution policy: see iqx.helpers.state.resolve_state_dir().
# The credential file is named after the resolved agent id — see
# ``iqx.examples.identity.key_path_for``.
STATE_DIR = resolve_state_dir()


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
        # The node does not silently rotate existing keys, and we cannot
        # auto-rotate because POST /agents/{id}/rotate-key requires the current
        # key — exactly what is missing here. See TROUBLESHOOTING.md.
        print(
            f"[registry] {worker_id} is already registered with a different "
            f"api_key. Re-run without pinning an id to generate a fresh one, "
            f"or restore the key file at {key_path}. Crashing — no silent "
            f"recovery.",
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
    unauthenticated ``GET /agents/{id}`` would confirm only that the row exists,
    not that the cached key still matches it — so a stale key would persist
    silently.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/agents/me",
            headers={"X-Worker-Id": worker_id, "X-API-Key": key},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException:
        # Surface the real failure on the next HTTP call, not here.
        return True
    return resp.status_code == 200


def ensure_registered(worker_id: str) -> str:
    """Re-register if the cached credential no longer authenticates (the key was
    rotated, or the node's state changed under a surviving local .key file)."""
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


# ---- verdict builders --------------------------------------------------------


def build_echo_verdict(task: dict) -> Optional[dict]:
    """Return the echo submission payload, or None if the task is not a
    well-formed echo task (skip, don't crash).

    Echo tasks carry the expected token in ``description`` as
    ``echo:<token>`` (see iqx/verifier.py::verify_echo). The submission is
    ``{"echo": "<token>"}`` — the verifier compares the two for equality.
    """
    description = (task.get("description") or "").strip()
    if not description.startswith("echo:"):
        return None
    token = description.split("echo:", 1)[1].strip()
    if not token:
        return None
    return {"echo": token}


def build_prediction_verdict(task: dict) -> Optional[dict]:
    """Return the default-skeptical prediction verdict, or None if the task's
    ``signal_data`` is missing / malformed (skip, don't crash).

    The five-key submission shape is what ``worker_prediction_accuracy_4h``
    expects (iqx/verifier.py). Only ``is_alpha`` is actually graded; the
    other keys are part of the structured contract for human readability /
    downstream tooling.

    This Worker reads ``signal_data`` to validate the task's well-formedness
    (so a Boss that fails to attach signal_data gets a skip rather than a
    submitted-and-failed verdict that costs the Worker ELO), but does NOT
    use any signal fields in its decision — the verdict is the same for
    every well-formed prediction task. That is the point: the baseline is
    the no-side-data Bayesian default, not an alpha-seeking Worker.
    """
    raw = task.get("signal_data")
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(spec, dict):
        return None
    # Sanity-check the minimum fields the verifier itself will need; if any
    # are absent, the verifier would fail the task anyway — better to skip.
    for required in ("chain", "token_address", "price_at_signal_usd"):
        if not spec.get(required):
            return None

    return {
        "is_alpha": BASELINE_IS_ALPHA,
        "confidence": BASELINE_CONFIDENCE,
        "reasoning": BASELINE_REASONING,
        "evidence_tx": [],
        "predicted_4h_return_pct": BASELINE_PREDICTED_RETURN_PCT,
    }


def build_verdict(task: dict) -> Optional[dict]:
    """Dispatch on ``verification_method``; return the submission payload or
    None to skip."""
    method = task.get("verification_method")
    if method == "echo":
        return build_echo_verdict(task)
    if method == "worker_prediction_accuracy_4h":
        return build_prediction_verdict(task)
    return None


# ---- HTTP glue ---------------------------------------------------------------


def fetch_open_tasks(
    methods: tuple[str, ...] = DEFAULT_METHODS,
    publisher_id: Optional[str] = None,
) -> list[dict]:
    """Pull all OPEN tasks and filter client-side by verification_method (and
    optionally publisher_id).

    Server-side filtering by verification_method would be cleaner; the
    central node's list_tasks endpoint only supports ``status`` today
    (a small server change tracked as a follow-up). At expected production
    volume the client-side filter is trivially fast.
    """
    resp = requests.get(
        f"{BASE_URL}/tasks", params={"status": "open"},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    all_tasks = resp.json() or []
    out = []
    for t in all_tasks:
        if t.get("verification_method") not in methods:
            continue
        if publisher_id is not None and t.get("publisher_id") != publisher_id:
            continue
        out.append(t)
    return out


def claim_task(task_id: str, api_key: str, worker_id: str) -> bool:
    """Attempt to claim. Returns True on success; False on 400/409 (claim
    rejected for a recoverable reason) so the loop can move on. Anything
    else is raised.

    The two recoverable statuses come from main.py:claim_task:
      - 409: lost the atomic-claim race (task was OPEN when we read it
        but another Worker claimed before our UPDATE landed)
      - 400: the task is no longer OPEN. (403 — ELO gate — is also
        possible but raises here; the baseline Worker should never hit
        it in normal use since it starts at the default ELO.)
    """
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
            print(f"[baseline] claim raced for {task_id[:8]} (HTTP 409); "
                  f"another worker got it", flush=True)
            return False
        if status == 400:
            print(f"[baseline] claim rejected for {task_id[:8]} (HTTP 400); "
                  f"task no longer OPEN", flush=True)
            return False
        raise


def submit_result(task_id: str, payload: dict, api_key: str,
                  worker_id: str) -> None:
    resp = requests.post(
        f"{BASE_URL}/tasks/{task_id}/submit",
        json={"worker_id": worker_id, "result": json.dumps(payload)},
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
    dry_run: bool,
) -> bool:
    """Process one task: build verdict, claim, submit. Returns True on a
    full successful submission; False on any expected non-fatal skip
    (unsupported method, malformed payload, claim race lost)."""
    task_id = task.get("id") or ""
    method = task.get("verification_method") or "?"

    payload = build_verdict(task)
    if payload is None:
        print(f"[baseline] task {task_id[:8]} ({method}): not a well-formed "
              f"task this baseline can answer, skipping", flush=True)
        return False

    if dry_run:
        print(f"[baseline] DRY-RUN task {task_id[:8]} ({method}): would submit "
              f"{json.dumps(payload, sort_keys=True)}", flush=True)
        return True

    assert api_key is not None
    if not claim_task(task_id, api_key, worker_id):
        return False
    submit_result(task_id, payload, api_key, worker_id)
    print(f"[baseline] task {task_id[:8]} ({method}): submitted "
          f"{json.dumps(payload, sort_keys=True)}", flush=True)
    return True


def run_once(
    *,
    api_key: Optional[str],
    worker_id: str,
    methods: tuple[str, ...],
    publisher_id: Optional[str],
    dry_run: bool,
) -> int:
    """One pass over the open-task queue. Returns count of tasks submitted."""
    try:
        eligible = fetch_open_tasks(methods=methods, publisher_id=publisher_id)
    except requests.RequestException as e:
        print(f"[baseline] failed to fetch open tasks: {e}", flush=True)
        return 0

    if not eligible:
        scope = ",".join(methods)
        pub = f", publisher={publisher_id}" if publisher_id else ""
        print(f"[baseline] no eligible tasks (methods={scope}{pub})",
              flush=True)
        return 0

    print(f"[baseline] {len(eligible)} eligible task(s) — processing",
          flush=True)
    submitted = 0
    for task in eligible:
        try:
            if grade_one_task(task, api_key=api_key, worker_id=worker_id,
                              dry_run=dry_run):
                submitted += 1
        except requests.RequestException as e:
            tid = task.get("id", "?")[:8]
            print(f"[baseline] transient error on task {tid}: {e}", flush=True)
    return submitted


def _parse_methods(arg: str) -> tuple[str, ...]:
    """Parse the --methods CSV flag and validate against SUPPORTED_METHODS."""
    requested = tuple(m.strip() for m in arg.split(",") if m.strip())
    if not requested:
        raise argparse.ArgumentTypeError("--methods must list at least one method")
    bad = [m for m in requested if m not in SUPPORTED_METHODS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unsupported method(s): {bad}; supported: {list(SUPPORTED_METHODS)}"
        )
    return requested


def main() -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(
        description="Baseline / reference Worker — defaults to claiming "
                    "echo (toy) tasks only. Default-skeptical defi_alpha "
                    "prediction mode requires explicit --methods opt-in.",
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
                        help="Print intent, do not register or POST")
    parser.add_argument("--publisher-id", default=None,
                        help="Optional: only claim tasks from this publisher "
                             "(default: any publisher)")
    parser.add_argument("--methods", type=_parse_methods,
                        default=DEFAULT_METHODS,
                        help=f"Comma-separated subset of "
                             f"{list(SUPPORTED_METHODS)} "
                             f"(default: {list(DEFAULT_METHODS)}). "
                             f"Prediction mode (worker_prediction_accuracy_4h) "
                             f"requires explicit opt-in: live tasks are "
                             f"single-claim and the baseline should not "
                             f"silently steal them from smarter Workers. "
                             f"Use prediction mode for demo / replay / "
                             f"intentionally empty-market runs only.")
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
        scope = ",".join(args.methods)
        pub = f", publisher={args.publisher_id}" if args.publisher_id else ""
        print(f"[baseline] starting continuous loop "
              f"(interval={POLL_INTERVAL_SEC}s, methods={scope}{pub})",
              flush=True)
        while True:
            try:
                run_once(
                    api_key=api_key,
                    worker_id=worker_id,
                    methods=args.methods,
                    publisher_id=args.publisher_id,
                    dry_run=args.dry_run,
                )
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                print(f"[baseline] unexpected error: {e}", flush=True)
            time.sleep(POLL_INTERVAL_SEC)
    else:
        run_once(
            api_key=api_key,
            worker_id=worker_id,
            methods=args.methods,
            publisher_id=args.publisher_id,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
