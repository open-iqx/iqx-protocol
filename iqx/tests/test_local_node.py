"""The local node and the quickstart Worker.

The node claims to follow ``PROTOCOL.md`` for the endpoints it serves, and to
keep the forward-only order: answers only before a task's deadline, an outcome
only after it, grading only against that outcome. These tests hold it to both,
with a controlled clock, and run the quickstart Worker once against a real
server on a loopback port.

Everything here is local: an in-memory or temporary SQLite database, and a
server bound to 127.0.0.1 for the duration of one test.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from iqx.local import __main__ as local_cli
from iqx.local.node import (
    SYNTHETIC_METHOD,
    SyntheticOutcome,
    create_app,
    open_engine,
    publish_synthetic_tasks,
    resolve_due_tasks,
)
from iqx.registry import _REGISTRY
from iqx.schema import Agent, Task, TaskStatus, TaskSubmission

T0 = 1_800_000_000.0
WINDOW = 60.0


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def memory_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


class NodeTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = memory_engine()
        self.clock = Clock(T0)
        self.client = TestClient(create_app(self.engine, clock=self.clock))
        self.task = publish_synthetic_tasks(
            self.engine, count=1, window_sec=WINDOW, now=T0)[0]
        self.deadline = T0 + WINDOW

    def register(self, agent_id: str) -> str:
        resp = self.client.post("/agents/register",
                                json={"id": agent_id, "name": agent_id})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["api_key"]

    def submit(self, agent_id: str, key: str, answer, task_id=None, body_task_id=None):
        task_id = task_id or self.task.id
        return self.client.post(
            f"/tasks/{task_id}/submissions",
            json={"task_id": body_task_id or task_id, "worker_id": agent_id,
                  "result": answer if isinstance(answer, str) else json.dumps(answer)},
            headers={"X-API-Key": key},
        )

    def row(self, model, key):
        with Session(self.engine) as session:
            return session.get(model, key)


class TestForwardOnlyOrder(NodeTestCase):
    """Submit before the deadline; outcome, grading and settlement after it."""

    def test_a_round_in_time_order(self):
        key = self.register("w1")

        self.clock.now = T0 + 10
        resp = self.submit("w1", key, {"prediction": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["elo_change"], 0)
        self.assertEqual(resp.json()["new_elo"], 1200)
        self.assertEqual(resp.json()["submission"]["status"], "submitted")

        # Before the deadline the grader refuses: no outcome is drawn at all.
        early = resolve_due_tasks(self.engine, now=self.deadline - 1)
        self.assertEqual([t for t, _ in early.not_due], [self.task.id])
        self.assertEqual(early.resolved, [])
        self.assertIsNone(self.row(SyntheticOutcome, self.task.id))

        # At the deadline the window is closed to every Worker.
        late_key = self.register("w2")
        self.clock.now = self.deadline
        late = self.submit("w2", late_key, {"prediction": True})
        self.assertEqual(late.status_code, 400)
        self.assertIn("window closed", late.json()["detail"])

        report = resolve_due_tasks(self.engine, now=self.deadline + 5)
        [resolution] = report.resolved
        self.assertTrue(resolution.settled)
        self.assertEqual(resolution.graded, 1)

        outcome = self.row(SyntheticOutcome, self.task.id)
        [sub] = self.client.get(f"/tasks/{self.task.id}/submissions").json()
        self.assertLess(sub["submitted_at"], self.deadline)
        self.assertGreaterEqual(outcome.recorded_at, self.deadline)
        self.assertGreaterEqual(sub["verified_at"], outcome.recorded_at)
        self.assertEqual(sub["verified"], outcome.above)
        self.assertEqual(sub["status"], "verified" if outcome.above else "failed")
        self.assertEqual(self.row(Task, self.task.id).status, TaskStatus.SETTLED)

    def test_elo_moves_once_at_grading_by_the_published_rule(self):
        """PROTOCOL.md: G = round(32 x (1 - expected)); 1200 vs min_elo 1000 is 8."""
        key = self.register("w1")
        self.submit("w1", key, {"prediction": True})
        self.assertEqual(self.row(Agent, "w1").elo, 1200)

        resolve_due_tasks(self.engine, now=self.deadline + 1)
        outcome = self.row(SyntheticOutcome, self.task.id)
        expected = 8 if outcome.above else -8
        [sub] = self.client.get(f"/tasks/{self.task.id}/submissions").json()
        self.assertEqual(sub["elo_delta"], expected)
        self.assertEqual(self.row(Agent, "w1").elo, 1200 + expected)

        # A second run neither redraws the outcome nor re-grades the answer.
        again = resolve_due_tasks(self.engine, now=self.deadline + 100)
        self.assertEqual(again.resolved, [])
        self.assertEqual(self.row(SyntheticOutcome, self.task.id).value, outcome.value)
        self.assertEqual(self.row(Agent, "w1").elo, 1200 + expected)

    def test_a_malformed_answer_is_graded_as_a_fail(self):
        key = self.register("w1")
        self.submit("w1", key, {"prediction": "yes"})
        resolve_due_tasks(self.engine, now=self.deadline + 1)
        [sub] = self.client.get(f"/tasks/{self.task.id}/submissions").json()
        self.assertEqual(sub["status"], "failed")
        self.assertIn("boolean 'prediction'", sub["verification_notes"])
        self.assertEqual(sub["elo_delta"], -8)

    def test_a_task_with_no_answers_is_never_settled(self):
        report = resolve_due_tasks(self.engine, now=self.deadline + 1)
        [resolution] = report.resolved
        self.assertFalse(resolution.settled)
        self.assertEqual(self.row(Task, self.task.id).status, TaskStatus.OPEN)

    def test_seeded_outcomes_are_reproducible(self):
        values = []
        for _ in range(2):
            engine = memory_engine()
            with Session(engine) as session:
                session.add(Task(**self.row(Task, self.task.id).model_dump()))
                session.commit()
            resolve_due_tasks(engine, now=self.deadline + 1, seed=7)
            with Session(engine) as session:
                values.append(session.get(SyntheticOutcome, self.task.id).value)
        self.assertEqual(values[0], values[1])


class TestEndpointContract(NodeTestCase):
    """The rejections PROTOCOL.md documents for the endpoints the node serves."""

    def test_registration_is_not_idempotent_and_returns_the_key_once(self):
        key = self.register("w1")
        self.assertTrue(key)
        dup = self.client.post("/agents/register", json={"id": "w1", "name": "x"})
        self.assertEqual(dup.status_code, 409)
        public = self.client.get("/agents/w1").json()
        self.assertEqual(set(public), {"id", "name", "elo", "staked_amount"})

    def test_agents_me_authenticates_the_cached_key(self):
        key = self.register("w1")
        me = lambda k: self.client.get(  # noqa: E731
            "/agents/me", headers={"X-Worker-Id": "w1", "X-API-Key": k})
        self.assertEqual(me(key).status_code, 200)
        self.assertEqual(me("wrong").status_code, 403)
        self.assertEqual(self.client.get("/agents/me").status_code, 400)

    def test_submission_rejections(self):
        key = self.register("w1")
        answer = {"prediction": False}
        self.assertEqual(self.submit("w1", "", answer).status_code, 401)
        self.assertEqual(self.submit("w1", "wrong", answer).status_code, 403)
        self.assertEqual(self.submit("nobody", key, answer).status_code, 404)
        self.assertEqual(
            self.submit("w1", key, answer, body_task_id="other").status_code, 400)
        self.assertEqual(self.submit("w1", key, answer, task_id="nope").status_code, 404)
        self.assertEqual(self.submit("w1", key, answer).status_code, 200)
        self.assertEqual(self.submit("w1", key, answer).status_code, 409)

    def test_min_elo_is_an_eligibility_gate(self):
        [hard] = publish_synthetic_tasks(self.engine, count=1, window_sec=WINDOW,
                                         min_elo=1300, now=T0)
        key = self.register("w1")
        self.assertEqual(self.submit("w1", key, {"prediction": True},
                                     task_id=hard.id).status_code, 403)

    def test_the_synthetic_family_is_not_in_the_registry(self):
        """PROTOCOL.md lists the registry's complete set; the local family is
        graded by the local node alone."""
        self.assertNotIn(SYNTHETIC_METHOD, _REGISTRY)


class TestServeIsLoopbackOnly(unittest.TestCase):
    def test_refuses_a_non_loopback_bind(self):
        for host in ("0.0.0.0", "192.168.1.10", "example.test"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(local_cli.main(["serve", "--host", host]), 2)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestQuickstartWorkerAgainstALocalServer(unittest.TestCase):
    """The documented workflow, against a real server on a loopback port."""

    def setUp(self):
        import uvicorn

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state"
        self.engine = open_engine(state / "node.sqlite3")
        port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(
            create_app(self.engine), host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, self.server, "should_exit", True)
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.02)
        self.assertTrue(self.server.started)

        env = {"IQX_BASE_URL": f"http://127.0.0.1:{port}",
               "IQX_STATE_DIR": str(state)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("IQX_QUICKSTART_WORKER_ID", None)
        os.environ.pop("IQX_ALLOW_PUBLIC_WRITES", None)
        self.state = state

    def run_worker(self, *argv: str):
        from iqx.examples import quickstart_worker
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = quickstart_worker.main(list(argv))
        return code, out.getvalue()

    def test_answer_resolve_report(self):
        from iqx.examples import quickstart_worker

        tasks = publish_synthetic_tasks(self.engine, count=4, window_sec=WINDOW)
        deadline = tasks[0].verification_deadline

        code, out = self.run_worker()
        self.assertEqual(code, 0, out)
        worker_id, source = quickstart_worker.resolve_worker_id(None)
        self.assertEqual(source, "version-bound")
        self.assertIn(worker_id, out)

        key_file = self.state / f"{worker_id}.key"
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)

        with Session(self.engine) as session:
            subs = session.exec(select(TaskSubmission)).all()
        self.assertEqual(len(subs), 4)
        self.assertTrue(all(s.worker_id == worker_id for s in subs))
        self.assertTrue(all(s.submitted_at < deadline for s in subs))

        # Report before resolution: nothing graded, nothing invented.
        code, out = self.run_worker("--report")
        self.assertEqual(code, 0, out)
        self.assertIn("no answer graded yet (4 awaiting)", out)

        resolve_due_tasks(self.engine, now=deadline + 1)
        code, out = self.run_worker("--report")
        self.assertEqual(code, 0, out)
        self.assertIn("graded answers: 4", out)
        self.assertIn("informedness J", out)
        self.assertIn("SYNTHETIC", out)

        # The same file, run again, is the same identity: it has nothing new to
        # answer, and does not register a second time.
        code, out = self.run_worker()
        self.assertEqual(code, 0, out)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(Agent)).all()), 1)

    def test_an_edited_file_is_a_new_identity(self):
        from iqx.examples import quickstart_worker

        original = quickstart_worker.version_bound_id()
        self.assertEqual(original, quickstart_worker.version_bound_id())
        with mock.patch.object(Path, "read_bytes", return_value=b"MARGIN = 1.0"):
            edited = quickstart_worker.version_bound_id()
        self.assertNotEqual(original, edited)
        # Same installation suffix: the change is the code digest.
        self.assertEqual(original.rsplit("-", 1)[1], edited.rsplit("-", 1)[1])

    def test_no_node_is_a_clear_message_not_a_traceback(self):
        os.environ["IQX_BASE_URL"] = f"http://127.0.0.1:{_free_port()}"
        code, out = self.run_worker()
        self.assertEqual(code, 1)
        self.assertIn("python -m iqx.local serve", out)


if __name__ == "__main__":
    unittest.main()
