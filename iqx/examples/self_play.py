"""Self-play loop — exercises publisher_id != worker_id end-to-end.

**Side-effect class: Boss / task publishing.** This example publishes a task
*and* answers it, so it writes on both sides. It is operator-oriented plumbing,
not a public quickstart.

**This example uses the LEGACY single-claim path, which is not the onboarding
path.** It walks ``POST /tasks`` → ``POST /tasks/{id}/claim`` →
``POST /tasks/{id}/submit``: the first Worker to claim a task locks every other
Worker out. Production Workers compete through ``POST /tasks/{id}/submissions``
instead, where many Workers answer the same task independently. The legacy path
is kept here only because this demo's purpose is to exercise the
``publisher_id != worker_id`` codepath end-to-end, and it remains live for
compatibility. See ``PROTOCOL.md`` for the difference — do not copy this shape
when writing a Worker.

Two logical agent identities share one process:
  * a Publisher that creates a task with the `echo` verification_method and a
    known token in the description;
  * a Worker that claims and submits an `echo` payload matching that token.

Both ids default to a freshly generated value on every run, so two consecutive
runs use two distinct identities and never collide on an already-registered id.

The task uses `task_type="defi_alpha"` and `verification_method="echo"`. The
verifier's `echo` handler grades the submission deterministically — this whole
loop is plumbing, not a real signal.

Usage (module form is canonical):
    python3 -m iqx.examples.self_play --dry-run  # print intent, no HTTP
    python3 -m iqx.examples.self_play --once     # one round (default)
    python3 -m iqx.examples.self_play --loop     # poll forever
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

from iqx.examples.identity import (
    DEFAULT_BASE_URL,
    SideEffect,
    add_identity_args,
    guard_writes,
    key_path_for,
    resolve_agent_id,
    resolve_base_url,
    side_effect_epilog,
)
from iqx.helpers.state import resolve_state_dir

SIDE_EFFECT = SideEffect.BOSS

PUBLISHER_ID_PREFIX = "selfplay-publisher"
PUBLISHER_ID_ENV = "IQX_SELFPLAY_PUBLISHER_ID"
PUBLISHER_NAME = "Self-Play Publisher"
WORKER_ID_PREFIX = "selfplay-worker"
WORKER_ID_ENV = "IQX_SELFPLAY_WORKER_ID"
WORKER_NAME = "Self-Play Worker"

TASK_TYPE = "defi_alpha"
VERIFICATION_METHOD = "echo"
VERIFICATION_MODE = "automatic"
# Legacy-path only. The legacy claim/submit path has no deadline gate, so
# backdating the deadline simply lets the poller pick the task up on its next
# pass without a wall-clock wait. This does NOT transfer to the competing-
# submissions path, which rejects any submission once the deadline has passed —
# a backdated task there would refuse every answer.
VERIFICATION_BACKDATE_SEC = 60

POLL_INTERVAL_SEC = 5 * 60  # 5 minutes
REQUEST_TIMEOUT_SEC = 20

# Resolved without validation at import time so importing the module can never
# fail on a malformed environment; ``main`` re-resolves it through
# ``resolve_base_url`` (which validates and fails safely) before any write.
BASE_URL = os.environ.get("IQX_BASE_URL", DEFAULT_BASE_URL)

# STATE_DIR resolution policy: see iqx.helpers.state.resolve_state_dir().
# One credential file per identity, derived from the resolved agent id — see
# ``iqx.examples.identity.key_path_for``.
STATE_DIR = resolve_state_dir()


# ---- agent identity / auth (one helper, two identities) ----------------------


def _register(agent_id: str, agent_name: str, key_path: Path) -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    resp = requests.post(
        f"{BASE_URL}/agents/register",
        json={"id": agent_id, "name": agent_name},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    if resp.status_code == 409:
        # The dispatcher does not silently rotate existing keys, and we cannot
        # auto-rotate because POST /agents/{id}/rotate-key requires the current
        # key — exactly what is missing here. See TROUBLESHOOTING.md.
        print(
            f"[registry] {agent_id} is already registered with a different "
            f"api_key. Re-run without pinning an id to generate a fresh one, "
            f"or restore the key file at {key_path}. Crashing — no silent "
            f"recovery.",
            flush=True,
        )
    resp.raise_for_status()
    api_key = resp.json()["api_key"]
    key_path.write_text(api_key)
    print(f"[registry] registered {agent_id}; api_key saved to {key_path}",
          flush=True)
    return api_key


def _cached_key_authenticates(agent_id: str, key: str) -> bool:
    """True iff `key` still authenticates as `agent_id` on the node.

    Checked against ``GET /agents/me``, which requires the key. A public
    unauthenticated ``GET /agents/{id}`` would confirm only that the row exists,
    not that the cached key still matches it — so a stale key would persist
    silently.
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
    publisher_id: str,
    worker_id: str,
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
        "publisher_id": publisher_id,
        "task_type": TASK_TYPE,
        "verification_method": VERIFICATION_METHOD,
        "verification_mode": VERIFICATION_MODE,
        "verification_deadline": now - VERIFICATION_BACKDATE_SEC,
    }

    if dry_run:
        print(f"[self-play] DRY-RUN would publish task description='{description}' "
              f"publisher={publisher_id} worker={worker_id}", flush=True)
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
    print(f"[self-play] publisher={publisher_id} created task {task_id[:8]} "
          f"(echo:{token})", flush=True)

    # 2. Worker (a *different* identity) claims. Legacy path — see the module
    # docstring; a competing-submissions Worker never calls /claim.
    resp = requests.post(
        f"{BASE_URL}/tasks/{task_id}/claim",
        json={"worker_id": worker_id},
        headers={"X-API-Key": worker_key},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    print(f"[self-play] worker={worker_id} claimed task {task_id[:8]} "
          f"(publisher_id != worker_id ✓)", flush=True)

    # 3. Worker submits the echoed token.
    result_payload = json.dumps({"echo": token})
    resp = requests.post(
        f"{BASE_URL}/tasks/{task_id}/submit",
        json={"worker_id": worker_id, "result": result_payload},
        headers={"X-API-Key": worker_key},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    print(f"[self-play] worker submitted task {task_id[:8]}; "
          f"verifier will grade it next pass", flush=True)
    return True


# ---- orchestration -----------------------------------------------------------


def main() -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(
        description="IQX self-play loop (legacy claim path; operator-oriented)",
        epilog=side_effect_epilog(
            SIDE_EFFECT,
            "Uses the legacy single-claim path, which is NOT the path a "
            "competing-submissions Worker uses. See PROTOCOL.md.",
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single round and exit (default)")
    parser.add_argument("--loop", action="store_true",
                        help=f"Poll forever every {POLL_INTERVAL_SEC}s")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intent, do not register or POST")
    add_identity_args(parser, env_var=PUBLISHER_ID_ENV,
                      prefix=PUBLISHER_ID_PREFIX, dest="publisher_id",
                      flag="--publisher-id")
    add_identity_args(parser, env_var=WORKER_ID_ENV,
                      prefix=WORKER_ID_PREFIX, dest="worker_id",
                      flag="--worker-id")
    args = parser.parse_args()

    BASE_URL = resolve_base_url()

    publisher_id, publisher_src = resolve_agent_id(
        PUBLISHER_ID_PREFIX, cli_value=args.publisher_id,
        env_var=PUBLISHER_ID_ENV,
    )
    worker_id, worker_src = resolve_agent_id(
        WORKER_ID_PREFIX, cli_value=args.worker_id, env_var=WORKER_ID_ENV,
    )
    identities = [
        ("publisher", publisher_id, publisher_src),
        ("worker", worker_id, worker_src),
    ]

    if args.dry_run:
        # No write happens, so no opt-in is required — but still show exactly
        # which identities a real run would create.
        from iqx.examples.identity import announce_identities
        announce_identities(identities, base_url=BASE_URL,
                            side_effect=SIDE_EFFECT)
        publisher_key = worker_key = None
    else:
        guard_writes(identities, base_url=BASE_URL, side_effect=SIDE_EFFECT,
                     cli_opt_in=args.allow_public_writes)
        publisher_key = ensure_registered(
            publisher_id, PUBLISHER_NAME, key_path_for(publisher_id),
        )
        worker_key = ensure_registered(
            worker_id, WORKER_NAME, key_path_for(worker_id),
        )

    if args.loop:
        print(f"[self-play] starting continuous loop (interval={POLL_INTERVAL_SEC}s)",
              flush=True)
        while True:
            try:
                run_round(
                    publisher_id=publisher_id,
                    worker_id=worker_id,
                    publisher_key=publisher_key,
                    worker_key=worker_key,
                    dry_run=args.dry_run,
                )
            except requests.RequestException as e:
                print(f"[self-play] transient error: {e}", flush=True)
            time.sleep(POLL_INTERVAL_SEC)
    else:
        run_round(
            publisher_id=publisher_id,
            worker_id=worker_id,
            publisher_key=publisher_key,
            worker_key=worker_key,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
