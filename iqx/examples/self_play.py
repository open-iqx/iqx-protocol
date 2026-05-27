"""Self-play loop — exercises publisher_id != worker_id end-to-end.

Two logical agent identities share one process:
  * Publisher (`selfplay-publisher-v1`) creates a task with the `echo`
    verification_method and a known token in the description.
  * Worker (`selfplay-worker-v1`) claims and submits an `echo` payload
    matching that token.

The task uses `task_type="defi_alpha"` (the same production category external
agents will see) and `verification_method="echo"`. The verifier's `echo`
handler grades the submission deterministically — this whole loop is plumbing,
not a real signal.

The point: force the codepath where the publisher and worker are different
agents to actually run, even before any external Worker joins the live node.
Once the protocol opens publicly, this same shape carries unchanged.

Usage (module form is canonical):
    python3 -m iqx.examples.self_play --once     # one round (default)
    python3 -m iqx.examples.self_play --loop     # poll forever
    python3 -m iqx.examples.self_play --dry-run  # print intent, no HTTP

This script is a developer/demo plumbing test for the
``publisher_id != worker_id`` codepath, not a live-production agent.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from iqx.helpers.state import resolve_state_dir

PUBLISHER_ID = "selfplay-publisher-v1"
PUBLISHER_NAME = "Self-Play Publisher"
WORKER_ID = "selfplay-worker-v1"
WORKER_NAME = "Self-Play Worker"

TASK_TYPE = "defi_alpha"
VERIFICATION_METHOD = "echo"
VERIFICATION_MODE = "automatic"
# Echo tasks resolve quickly; set the deadline 60s in the past so the verifier
# picks the task up on its very next pass without a wall-clock wait.
VERIFICATION_BACKDATE_SEC = 60

POLL_INTERVAL_SEC = 5 * 60  # 5 minutes
REQUEST_TIMEOUT_SEC = 20

BASE_URL = os.environ.get("IQX_BASE_URL", "http://localhost:8000")

# STATE_DIR resolution policy: see iqx.helpers.state.resolve_state_dir().
# Two credential files live under it:
#   - ``self_play_publisher.key`` (selfplay-publisher-v1)
#   - ``self_play_worker.key``    (selfplay-worker-v1)
# Both keys are registered at the central node on first run; subsequent
# runs reuse the cached keys.
STATE_DIR = resolve_state_dir()
PUBLISHER_KEY_PATH = STATE_DIR / "self_play_publisher.key"
WORKER_KEY_PATH = STATE_DIR / "self_play_worker.key"


# ---- agent identity / auth (one helper, two identities) ----------------------


def _register(agent_id: str, agent_name: str, key_path: Path) -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    resp = requests.post(
        f"{BASE_URL}/agents/register",
        json={"id": agent_id, "name": agent_name},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    if resp.status_code == 409:
        # See iqx/examples/worker_judge.py:_register for the full rationale.
        # The dispatcher does not silently rotate existing keys; we can't
        # auto-rotate because /agents/{id}/rotate-key requires the current
        # key, which is exactly what we lack here.
        print(
            f"[registry] {agent_id} already registered with a different "
            f"api_key. If you lost {key_path}, ask an admin to delete the "
            f"agent row; if this is a fresh deploy clashing with a stale id, "
            f"choose a different agent_id. Crashing — no silent recovery.",
            flush=True,
        )
    resp.raise_for_status()
    api_key = resp.json()["api_key"]
    key_path.write_text(api_key)
    print(f"[registry] registered {agent_id}; api_key saved to {key_path}",
          flush=True)
    return api_key


def _cached_key_authenticates(agent_id: str, key: str) -> bool:
    """True iff `key` still authenticates as `agent_id` on the dispatcher.

    See worker_judge._cached_key_authenticates for the full rationale —
    same shape, same trap: the old _agent_exists_on_server check was a
    public unauthenticated GET that confirmed the row existed without
    proving the cached key still matched it, so a stale key persisted
    silently across any external re-register.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/agents/me",
            headers={"X-Worker-Id": agent_id, "X-API-Key": key},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException:
        return True  # surface the real failure on the next HTTP call
    return resp.status_code == 200


def ensure_registered(agent_id: str, agent_name: str, key_path: Path) -> str:
    """Re-register if our cached credential no longer works on the dispatcher."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key = key_path.read_text().strip()
        if key and _cached_key_authenticates(agent_id, key):
            return key
        if key:
            print(f"[registry] cached key no longer authenticates for "
                  f"{agent_id} (rotated or dispatcher state changed); "
                  f"re-registering", flush=True)
    return _register(agent_id, agent_name, key_path)


# ---- single round ------------------------------------------------------------


def run_round(
    *,
    publisher_key: Optional[str],
    worker_key: Optional[str],
    dry_run: bool,
) -> bool:
    """Run one publish → claim → submit cycle. Returns True on success."""
    token = secrets.token_hex(8)
    description = f"echo:{token}"
    now = time.time()
    payload_in = {
        "description": description,
        "budget": 0.0,
        "min_elo": 1000,
        "publisher_id": PUBLISHER_ID,
        "task_type": TASK_TYPE,
        "verification_method": VERIFICATION_METHOD,
        "verification_mode": VERIFICATION_MODE,
        "verification_deadline": now - VERIFICATION_BACKDATE_SEC,
    }

    if dry_run:
        print(f"[self-play] DRY-RUN would publish task description='{description}' "
              f"publisher={PUBLISHER_ID} worker={WORKER_ID}", flush=True)
        return True

    assert publisher_key is not None
    assert worker_key is not None

    # 1. Publisher creates the task. /tasks is unauthenticated in the MVP, so
    # publisher_key isn't sent — but we still verify the publisher is
    # registered (above) so future-auth doesn't surprise us.
    resp = requests.post(
        f"{BASE_URL}/tasks", json=payload_in, timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    task = resp.json()
    task_id = task["id"]
    print(f"[self-play] publisher={PUBLISHER_ID} created task {task_id[:8]} "
          f"(echo:{token})", flush=True)

    # 2. Worker (a *different* identity) claims.
    resp = requests.post(
        f"{BASE_URL}/tasks/{task_id}/claim",
        json={"worker_id": WORKER_ID},
        headers={"X-API-Key": worker_key},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    print(f"[self-play] worker={WORKER_ID} claimed task {task_id[:8]} "
          f"(publisher_id != worker_id ✓)", flush=True)

    # 3. Worker submits the echoed token.
    result_payload = json.dumps({"echo": token})
    resp = requests.post(
        f"{BASE_URL}/tasks/{task_id}/submit",
        json={"worker_id": WORKER_ID, "result": result_payload},
        headers={"X-API-Key": worker_key},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    print(f"[self-play] worker submitted task {task_id[:8]}; "
          f"verifier will grade it next pass", flush=True)
    return True


# ---- orchestration -----------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="IQX self-play loop")
    parser.add_argument("--once", action="store_true",
                        help="Run a single round and exit (default)")
    parser.add_argument("--loop", action="store_true",
                        help=f"Poll forever every {POLL_INTERVAL_SEC}s")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intent, do not register or POST")
    args = parser.parse_args()

    if args.dry_run:
        publisher_key = worker_key = None
    else:
        publisher_key = ensure_registered(
            PUBLISHER_ID, PUBLISHER_NAME, PUBLISHER_KEY_PATH,
        )
        worker_key = ensure_registered(
            WORKER_ID, WORKER_NAME, WORKER_KEY_PATH,
        )

    if args.loop:
        print(f"[self-play] starting continuous loop (interval={POLL_INTERVAL_SEC}s)",
              flush=True)
        while True:
            try:
                run_round(
                    publisher_key=publisher_key,
                    worker_key=worker_key,
                    dry_run=args.dry_run,
                )
            except requests.RequestException as e:
                print(f"[self-play] transient error: {e}", flush=True)
            time.sleep(POLL_INTERVAL_SEC)
    else:
        run_round(
            publisher_key=publisher_key,
            worker_key=worker_key,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
