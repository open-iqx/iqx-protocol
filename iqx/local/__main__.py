"""``python -m iqx.local`` — run a local node and act as its operator.

    python -m iqx.local serve      # the node, on http://127.0.0.1:8000
    python -m iqx.local publish    # create synthetic tasks (local publisher)
    python -m iqx.local resolve    # after the deadline: outcome, grading, settlement

All three use the same SQLite file, under the state directory unless ``--db``
says otherwise. Delete that directory to start over.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

from iqx.local.node import (
    SYNTHETIC_METHOD,
    create_app,
    default_db_path,
    open_engine,
    publish_synthetic_tasks,
    resolve_due_tasks,
)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
PREFIX = "[iqx-local]"


def _say(message: str) -> None:
    print(f"{PREFIX} {message}", flush=True)


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def cmd_serve(args: argparse.Namespace) -> int:
    if args.host not in LOOPBACK_HOSTS:
        _say(f"refusing to bind {args.host!r}: the local node listens on a "
             f"loopback address only ({', '.join(LOOPBACK_HOSTS)}). It is a "
             f"development tool, not a shared or public service.")
        return 2
    try:
        import uvicorn
    except ImportError:
        _say("uvicorn is not installed. Install the package with its 'local' "
             "extra, e.g. pip install \"iqx[local] @ git+https://github.com/"
             "open-iqx/iqx-protocol.git@main\"")
        return 1
    db = Path(args.db) if args.db else default_db_path()
    app = create_app(open_engine(db))
    _say("LOCAL DEVELOPMENT NODE: synthetic tasks only. Not a sandbox, and "
         "not the research service.")
    _say(f"database  : {db}")
    _say(f"listening : http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _open(args: argparse.Namespace):
    db = Path(args.db) if args.db else default_db_path()
    _say(f"database  : {db}")
    return open_engine(db)


def cmd_publish(args: argparse.Namespace) -> int:
    engine = _open(args)
    try:
        tasks = publish_synthetic_tasks(engine, count=args.count,
                                        window_sec=args.window)
    except ValueError as e:
        _say(f"refusing to publish: {e}")
        return 2
    deadline = tasks[0].verification_deadline
    _say(f"published {len(tasks)} SYNTHETIC task(s) ({SYNTHETIC_METHOD}).")
    _say(f"answers accepted until {_utc(deadline)} "
         f"(in {deadline - time.time():.0f}s).")
    _say("no outcome exists yet: `resolve` draws it, and refuses to before "
         "the deadline.")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    engine = _open(args)
    report = resolve_due_tasks(engine, seed=args.seed)
    now = time.time()
    if report.not_due:
        next_deadline = min(deadline for _task_id, deadline in report.not_due)
        _say(f"{len(report.not_due)} task(s) still accepting answers; the next "
             f"deadline is in {next_deadline - now:.0f}s. No outcome is drawn "
             f"before a task's deadline.")

    acted = [r for r in report.resolved
             if r.newly_recorded or r.graded or r.settled]
    for r in acted:
        side = "above" if r.above else "below"
        state = "settled" if r.settled else "left open (no answers)"
        _say(f"task {r.task_id[:8]}  SYNTHETIC outcome {side} "
             f"(value {r.value:.2f}), recorded "
             f"{r.recorded_at - r.deadline:.1f}s after the deadline; graded "
             f"{r.graded} answer(s); parent {state}")
    idle = len(report.resolved) - len(acted)
    if idle:
        _say(f"{idle} past-deadline task(s) have no answers; a parent with no "
             f"answers is never settled, so they stay open.")
    if not report.resolved and not report.not_due:
        _say("no open synthetic tasks. Create some with: python -m iqx.local "
             "publish")
    graded = sum(r.graded for r in report.resolved)
    settled = sum(1 for r in report.resolved if r.settled)
    _say(f"graded {graded} answer(s); settled {settled} task(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m iqx.local",
        description="Run a local IQX node with synthetic tasks, for developing "
                    "an Agent. Loopback only; nothing here reaches another node.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", default=None,
                       help=f"SQLite file (default: {default_db_path()})")

    serve = sub.add_parser("serve", help="run the node's HTTP API")
    serve.add_argument("--host", default="127.0.0.1",
                       help="loopback address to bind (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000)
    add_db(serve)
    serve.set_defaults(func=cmd_serve)

    publish = sub.add_parser("publish", help="create open synthetic tasks")
    publish.add_argument("--count", type=int, default=6)
    publish.add_argument("--window", type=float, default=60.0,
                         help="seconds the tasks accept answers (default: 60)")
    add_db(publish)
    publish.set_defaults(func=cmd_publish)

    resolve = sub.add_parser(
        "resolve",
        help="after the deadline: draw outcomes, grade answers, settle tasks")
    resolve.add_argument("--seed", type=int, default=None,
                         help="make the drawn outcomes reproducible")
    add_db(resolve)
    resolve.set_defaults(func=cmd_resolve)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
