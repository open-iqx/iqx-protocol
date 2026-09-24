"""Quickstart Worker — a minimal Agent on the competing-submissions path.

**Side-effect class: Worker registration / submission.** Unless run with
``--dry-run`` or ``--report`` it registers an Agent identity and submits
answers.

It answers only the local node's synthetic task family, ``synthetic_binary``,
so it is meant to run against a node you started yourself with
``python -m iqx.local serve``. See ``QUICKSTART.md``.

One run does three things:

1. **Identity.** Its default id embeds a digest of this file, e.g.
   ``quickstart-worker-3f9a2c1d-8e5b0a``. Edit the file — the rule, its
   threshold, anything — and the next run registers a *new* identity with an
   empty record, instead of inheriting the old one's history. The random
   suffix is kept in the state directory, so two developers running an
   unmodified copy never share an id. The node enforces none of this; it is
   the discipline the protocol asks of an Agent author.
2. **Answers.** It reads the open tasks, keeps the synthetic ones whose
   submission window is still open, and answers each through
   ``POST /tasks/{task_id}/submissions`` before its deadline. The outcome that
   will grade the answer does not exist yet.
3. **Record** (``--report``, read-only). After the local operator has resolved
   the tasks, it reads its own graded answers from
   ``GET /tasks/{task_id}/submissions`` and prints its record: verdict and
   timing per answer, class-balanced informedness, and the v0.1 ELO.

Usage:
    python -m iqx.examples.quickstart_worker             # answer open synthetic tasks
    python -m iqx.examples.quickstart_worker --report    # print this identity's record
    python -m iqx.examples.quickstart_worker --dry-run   # show answers; write nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

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

WORKER_ID_PREFIX = "quickstart-worker"
WORKER_ID_ENV = "IQX_QUICKSTART_WORKER_ID"
WORKER_NAME = "Quickstart Worker"

#: The only task family this Worker answers. It exists only on a local node.
TASK_FAMILY = "synthetic_binary"

#: The decision rule's one parameter: predict "above" when the observed value
#: exceeds the reference level by more than this. Changing it changes the file,
#: and so the identity.
MARGIN = 0.0

#: Do not start an answer this close to a deadline; it could arrive too late.
DEADLINE_SAFETY_SEC = 1.0
REQUEST_TIMEOUT_SEC = 20

# Resolved without validation at import time so importing the module cannot
# fail on a malformed environment; ``main`` re-resolves it before any request.
BASE_URL = os.environ.get("IQX_BASE_URL", DEFAULT_BASE_URL)

PREFIX = "[quickstart]"


def _say(message: str) -> None:
    print(f"{PREFIX} {message}", flush=True)


# ---- the Agent's decision -------------------------------------------------------


def decide(task: dict) -> Optional[dict]:
    """Return the answer to one synthetic task, or None to skip it.

    The answer is JSON. ``prediction`` is the only graded field; ``reasoning``
    is recorded and not graded.
    """
    try:
        spec = json.loads(task.get("signal_data") or "")
        observed = float(spec["observed_value"])
        reference = float(spec["reference_level"])
    except (TypeError, ValueError, KeyError):
        return None
    prediction = observed > reference + MARGIN
    return {
        "prediction": prediction,
        "reasoning": f"observed {observed:.2f} vs reference {reference:.2f}",
    }


# ---- identity -------------------------------------------------------------------


def _instance_suffix() -> str:
    """A random suffix kept in the state directory, created on first use."""
    path = resolve_state_dir() / f"{WORKER_ID_PREFIX}.instance"
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        value = ""
    if not re.fullmatch(r"[0-9a-f]{6}", value):
        value = secrets.token_hex(3)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n")
    return value


def version_bound_id() -> str:
    """``quickstart-worker-<digest of this file>-<instance suffix>``."""
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:8]
    return f"{WORKER_ID_PREFIX}-{code}-{_instance_suffix()}"


def resolve_worker_id(cli_value: Optional[str]) -> Tuple[str, str]:
    """CLI value, then ``$IQX_QUICKSTART_WORKER_ID``, then the version-bound id."""
    agent_id, source = resolve_agent_id(
        WORKER_ID_PREFIX, cli_value=cli_value, env_var=WORKER_ID_ENV)
    if source != "generated":
        return agent_id, source
    return version_bound_id(), "version-bound"


# ---- HTTP -------------------------------------------------------------------------


class NodeError(RuntimeError):
    """A node response this example cannot continue from."""


def _write_key(path: Path, api_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(api_key)


def ensure_registered(base_url: str, worker_id: str) -> str:
    """Return a key that authenticates as ``worker_id``, registering if needed."""
    key_path = key_path_for(worker_id)
    if key_path.exists():
        key = key_path.read_text().strip()
        resp = requests.get(
            f"{base_url}/agents/me",
            headers={"X-Worker-Id": worker_id, "X-API-Key": key},
            timeout=REQUEST_TIMEOUT_SEC,
        )
        if resp.status_code == 200:
            return key
        if resp.status_code != 404:
            raise NodeError(
                f"the key in {key_path} is rejected for {worker_id} "
                f"(HTTP {resp.status_code}). Use another id with --agent-id.")
        _say(f"{worker_id} is not registered on this node yet; registering.")

    resp = requests.post(
        f"{base_url}/agents/register",
        json={"id": worker_id, "name": WORKER_NAME},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    if resp.status_code == 409:
        raise NodeError(
            f"{worker_id} is already registered on this node and no key for it "
            f"is at {key_path}. Use another id with --agent-id.")
    resp.raise_for_status()
    api_key = resp.json()["api_key"]
    _write_key(key_path, api_key)
    _say(f"registered {worker_id}; key saved to {key_path} (readable by you only)")
    return api_key


def fetch_open_family_tasks(base_url: str) -> List[dict]:
    resp = requests.get(f"{base_url}/tasks", params={"status": "open"},
                        timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    return [t for t in resp.json() if t.get("verification_method") == TASK_FAMILY]


def submit(base_url: str, worker_id: str, api_key: str, task_id: str,
           answer: dict) -> requests.Response:
    return requests.post(
        f"{base_url}/tasks/{task_id}/submissions",
        json={"task_id": task_id, "worker_id": worker_id,
              "result": json.dumps(answer)},
        headers={"X-API-Key": api_key},
        timeout=REQUEST_TIMEOUT_SEC,
    )


# ---- answering ------------------------------------------------------------------


def run_answers(base_url: str, worker_id: str, source: str, *,
                dry_run: bool, allow_public_writes: bool) -> int:
    identities = [("worker", worker_id, source)]
    now = time.time()
    tasks = fetch_open_family_tasks(base_url)
    answerable = [t for t in tasks
                  if (t.get("verification_deadline") or 0) - now > DEADLINE_SAFETY_SEC]
    if not answerable:
        _say(f"no open {TASK_FAMILY} task is accepting answers on {base_url}. "
             f"Create some with: python -m iqx.local publish")
        return 0

    if dry_run:
        announce_identities(identities, base_url=base_url, side_effect=SIDE_EFFECT)
        for task in answerable:
            _say(f"DRY-RUN task {task['id'][:8]}: would submit "
                 f"{json.dumps(decide(task), sort_keys=True)}")
        return 0

    guard_writes(identities, base_url=base_url, side_effect=SIDE_EFFECT,
                 cli_opt_in=allow_public_writes)
    api_key = ensure_registered(base_url, worker_id)

    answered = 0
    latest_deadline = 0.0
    for task in answerable:
        answer = decide(task)
        if answer is None:
            _say(f"task {task['id'][:8]}: malformed signal_data; skipping")
            continue
        resp = submit(base_url, worker_id, api_key, task["id"], answer)
        if resp.status_code == 409:
            _say(f"task {task['id'][:8]}: already answered by this identity")
            continue
        if resp.status_code == 400:
            _say(f"task {task['id'][:8]}: rejected: {resp.json().get('detail')}")
            continue
        resp.raise_for_status()
        submitted_at = resp.json()["submission"]["submitted_at"]
        deadline = task["verification_deadline"]
        latest_deadline = max(latest_deadline, deadline)
        side = "above" if answer["prediction"] else "below"
        _say(f"task {task['id'][:8]}  {answer['reasoning']} -> predicts {side}; "
             f"answered {deadline - submitted_at:.1f}s before the deadline")
        answered += 1

    if answered:
        wait = max(0.0, latest_deadline - time.time())
        _say(f"answered {answered} task(s). Their outcomes do not exist yet. "
             f"After the deadline (in {wait:.0f}s), resolve them with:")
        print("    python -m iqx.local resolve", flush=True)
        _say("then print this Worker's record with:")
        print("    python -m iqx.examples.quickstart_worker --report", flush=True)
    return 0


# ---- the record -------------------------------------------------------------------


def confusion(pairs: List[Tuple[bool, bool]]) -> Tuple[int, int, int, int]:
    """``(TP, FN, TN, FP)`` over ``(predicted, actual)`` pairs."""
    tp = sum(1 for p, a in pairs if p and a)
    fn = sum(1 for p, a in pairs if not p and a)
    tn = sum(1 for p, a in pairs if not p and not a)
    fp = sum(1 for p, a in pairs if p and not a)
    return tp, fn, tn, fp


def informedness(pairs: List[Tuple[bool, bool]]) -> Optional[float]:
    """``J = TPR + TNR - 1`` over ``(predicted, actual)`` pairs.

    Undefined (None) unless both outcome classes occur. A constant answer
    scores exactly 0 whenever it is defined.
    """
    tp, fn, tn, fp = confusion(pairs)
    if tp + fn == 0 or tn + fp == 0:
        return None
    return tp / (tp + fn) + tn / (tn + fp) - 1


def collect_record(base_url: str, worker_id: str) -> List[Tuple[dict, dict]]:
    """``(task, own submission)`` pairs for every family task this id answered."""
    resp = requests.get(f"{base_url}/tasks", timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    record = []
    for task in resp.json():
        if task.get("verification_method") != TASK_FAMILY:
            continue
        subs = requests.get(f"{base_url}/tasks/{task['id']}/submissions",
                            timeout=REQUEST_TIMEOUT_SEC)
        subs.raise_for_status()
        record.extend((task, s) for s in subs.json() if s["worker_id"] == worker_id)
    record.sort(key=lambda pair: pair[1]["submitted_at"] or 0)
    return record


def _prediction(submission: dict) -> Optional[bool]:
    try:
        value = json.loads(submission.get("result") or "").get("prediction")
    except (TypeError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def run_report(base_url: str, worker_id: str) -> int:
    _say(f"record of {worker_id} on {base_url} ({TASK_FAMILY}: SYNTHETIC tasks)")
    record = collect_record(base_url, worker_id)
    if not record:
        _say("this identity has no answers on this node. If you edited this file "
             "since answering, that is expected: the edit made a new identity.")
        return 0

    print(f"  {'task':<9}{'predicted':<11}{'outcome':<9}{'verdict':<10}"
          f"{'answered':<18}{'graded':<17}{'elo_delta':>9}", flush=True)
    pairs = []
    pending = 0
    for task, sub in record:
        predicted = _prediction(sub)
        deadline = task["verification_deadline"]
        answered = f"{deadline - sub['submitted_at']:.1f}s before dl"
        if sub["status"] in ("verified", "failed") and predicted is not None:
            actual = predicted if sub["verified"] else not predicted
            pairs.append((predicted, actual))
            outcome = "above" if actual else "below"
            graded = f"{sub['verified_at'] - deadline:.1f}s after dl"
            delta = f"{sub['elo_delta']:+d}" if sub["elo_delta"] is not None else "-"
        else:
            pending += 1
            outcome, graded, delta = "-", "not yet", "-"
        shown = "-" if predicted is None else ("above" if predicted else "below")
        print(f"  {task['id'][:8]:<9}{shown:<11}{outcome:<9}{sub['status']:<10}"
              f"{answered:<18}{graded:<17}{delta:>9}", flush=True)

    correct = sum(1 for p, a in pairs if p == a)
    if pairs:
        _say(f"graded answers: {len(pairs)}, correct: {correct} "
             f"(accuracy {correct / len(pairs):.2f}); awaiting grading: {pending}")
        tp, fn, tn, fp = confusion(pairs)
        _say(f"confusion, 'above' as positive: TP {tp}  FN {fn}  TN {tn}  FP {fp}")
        j = informedness(pairs)
        if j is None:
            _say("informedness J = TPR + TNR - 1 is undefined: only one outcome "
                 "class occurred.")
        else:
            _say(f"informedness J = TPR + TNR - 1 = {j:.2f}; a constant answer "
                 f"scores J = 0 on the same tasks")
    else:
        _say(f"no answer graded yet ({pending} awaiting). Run "
             f"`python -m iqx.local resolve` after the deadline.")

    agent = requests.get(f"{base_url}/agents/{worker_id}",
                         timeout=REQUEST_TIMEOUT_SEC)
    if agent.status_code == 200:
        net = sum(s["elo_delta"] or 0 for _t, s in record)
        _say(f"v0.1 ELO: {agent.json()['elo']} (net {net:+d} over these "
             f"answers): a raw-accuracy rating, not the research metric")
    if pairs:
        _say(f"SYNTHETIC: the local operator generated these outcomes, and "
             f"{len(pairs)} answers are far too few to conclude anything. This "
             f"shows the mechanics, not predictive skill.")
    return 0


# ---- CLI ------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(
        description="Quickstart Worker: answers the local node's synthetic "
                    "tasks through the competing-submissions endpoint, then "
                    "reports its graded record.",
        epilog=side_effect_epilog(
            SIDE_EFFECT,
            extra="Answers only the local-only synthetic_binary family. "
                  "--report and --dry-run write nothing. See QUICKSTART.md."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true",
                      help="print this identity's graded record (read-only)")
    mode.add_argument("--dry-run", action="store_true",
                      help="show the answers it would submit; write nothing")
    add_identity_args(
        parser, env_var=WORKER_ID_ENV, prefix=WORKER_ID_PREFIX,
        default_help=(f"'{WORKER_ID_PREFIX}-<digest of this file>-<suffix>'. "
                      f"The same file keeps the same identity; an edited file "
                      f"gets a new one."),
    )
    args = parser.parse_args(argv)

    BASE_URL = resolve_base_url()
    worker_id, source = resolve_worker_id(args.agent_id)
    try:
        if args.report:
            return run_report(BASE_URL, worker_id)
        return run_answers(BASE_URL, worker_id, source, dry_run=args.dry_run,
                           allow_public_writes=args.allow_public_writes)
    except requests.ConnectionError:
        _say(f"no IQX node is reachable at {BASE_URL}. Start the local node in "
             f"another terminal with: python -m iqx.local serve")
        return 1
    except (NodeError, requests.HTTPError) as e:
        _say(f"stopped: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
