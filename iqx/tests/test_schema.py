"""SDK contract tests for ``iqx.schema``.

Locks down the public field shape of ``Task`` / ``Agent`` / ``RegistrationChallenge``
and the request DTOs an external Boss / Worker writes against. These are
pure unit tests — no DB session, no network. SQLModel's ``table=True``
persistence path is intentionally out of scope here; it is exercised by the
operator-private integration suite that ships alongside the central node.
"""

from __future__ import annotations

import unittest

from iqx.schema import (
    Agent,
    AgentRegister,
    RegistrationChallenge,
    Task,
    TaskCreate,
    TaskStatus,
)


class TestTaskStatusEnum(unittest.TestCase):
    """Lock the 6-state task lifecycle enum.

    The state-machine values are read as plain strings by the dispatcher
    (e.g. ``Task.status == "open"`` in SQL filters) — string identity is
    part of the SDK contract, not just the Python ``Enum`` semantics.
    """

    EXPECTED = {
        "OPEN": "open",
        "CLAIMED": "claimed",
        "SUBMITTED": "submitted",
        "VERIFIED": "verified",
        "PUBLISHED": "published",
        "FAILED": "failed",
    }

    def test_enum_member_names_and_values(self):
        for name, value in self.EXPECTED.items():
            member = getattr(TaskStatus, name)
            self.assertEqual(member.value, value, f"{name}.value")

    def test_enum_member_set_is_exactly_six_states(self):
        actual = {m.name for m in TaskStatus}
        self.assertEqual(actual, set(self.EXPECTED.keys()))

    def test_enum_is_str_subclass(self):
        """``TaskStatus`` extends ``str`` so dict / JSON / SQL comparisons
        with plain strings work without explicit ``.value`` access."""
        self.assertIsInstance(TaskStatus.OPEN, str)
        self.assertEqual(TaskStatus.OPEN, "open")

    def test_enum_value_is_lowercased_name(self):
        for member in TaskStatus:
            self.assertEqual(member.value, member.name.lower())


class TestTaskModel(unittest.TestCase):
    """Lock the SDK-public field set on ``Task``.

    Required fields, defaults, and JSON round-trip — the contract an
    external Boss honors when constructing a Task payload to POST to
    ``/tasks`` and a Worker reads when claiming.
    """

    def _make_minimal_task(self) -> Task:
        return Task(id="t-001", description="hello", budget=1.0)

    def test_required_fields_instantiate(self):
        task = self._make_minimal_task()
        self.assertEqual(task.id, "t-001")
        self.assertEqual(task.description, "hello")
        self.assertEqual(task.budget, 1.0)

    def test_status_defaults_to_open(self):
        task = self._make_minimal_task()
        self.assertEqual(task.status, TaskStatus.OPEN)

    def test_min_elo_defaults_to_1000(self):
        task = self._make_minimal_task()
        self.assertEqual(task.min_elo, 1000)

    def test_optional_fields_default_to_none(self):
        task = self._make_minimal_task()
        for field in (
            "worker_id",
            "result",
            "publisher_id",
            "task_type",
            "verification_method",
            "verification_mode",
            "signal_type",
            "evidence_urls",
            "verification_deadline",
            "verified",
            "verified_at",
            "verification_notes",
            "elo_delta",
            "published",
            "published_at",
            "tweet_url",
            "baseline_return",
            "signal_data",
        ):
            self.assertIsNone(getattr(task, field), f"{field} should default to None")

    def test_signal_data_is_optional_string(self):
        """``signal_data`` is the JSON-encoded Boss task spec read by
        ``worker_prediction_accuracy_4h``; it must accept a string payload
        and remain optional for older single-role verifier methods."""
        task = Task(id="t-002", description="boss", budget=1.0,
                    signal_data='{"chain":"arbitrum","token_address":"0xabc"}')
        self.assertEqual(task.signal_data,
                         '{"chain":"arbitrum","token_address":"0xabc"}')

    def test_json_round_trip_preserves_fields(self):
        original = Task(
            id="t-003",
            description="round-trip",
            budget=2.5,
            min_elo=1100,
            status=TaskStatus.CLAIMED,
            worker_id="w-001",
            publisher_id="b-001",
            task_type="defi_alpha",
            verification_method="echo",
            verification_mode="automatic",
            signal_data='{"k":"v"}',
        )
        rebuilt = Task.model_validate(original.model_dump())
        self.assertEqual(rebuilt.model_dump(), original.model_dump())


class TestAgentModel(unittest.TestCase):
    """Lock the SDK-public field set on ``Agent``."""

    def _make_minimal_agent(self) -> Agent:
        return Agent(id="a-001", name="alice", api_key="key-xyz")

    def test_required_fields_instantiate(self):
        agent = self._make_minimal_agent()
        self.assertEqual(agent.id, "a-001")
        self.assertEqual(agent.name, "alice")
        self.assertEqual(agent.api_key, "key-xyz")

    def test_elo_defaults_to_1200(self):
        self.assertEqual(self._make_minimal_agent().elo, 1200)

    def test_json_round_trip_preserves_fields(self):
        original = Agent(id="a-002", name="bob", api_key="k", elo=1350)
        rebuilt = Agent.model_validate(original.model_dump())
        self.assertEqual(rebuilt.model_dump(), original.model_dump())


class TestRegistrationChallengeModel(unittest.TestCase):
    """Smoke test for the PoW challenge model's public fields."""

    def test_required_fields_and_optional_consumed_at(self):
        ch = RegistrationChallenge(id="c-001", prefix="abcd1234", difficulty=20)
        self.assertEqual(ch.id, "c-001")
        self.assertEqual(ch.prefix, "abcd1234")
        self.assertEqual(ch.difficulty, 20)
        self.assertIsNone(ch.consumed_at)
        # ``created_at`` is populated by default_factory=time.time; it must be
        # a float and roughly "now" (no strict bounds — just a smoke check).
        self.assertIsInstance(ch.created_at, float)
        self.assertGreater(ch.created_at, 0)


class TestRequestDTOs(unittest.TestCase):
    """Minimal smoke tests for the request DTOs used at the HTTP boundary."""

    def test_task_create_required_fields(self):
        tc = TaskCreate(description="hi", budget=1.0)
        self.assertEqual(tc.description, "hi")
        self.assertEqual(tc.budget, 1.0)
        self.assertEqual(tc.min_elo, 1000)
        # All protocol-shape fields default to None on the wire — dispatcher
        # back-fills publisher_id="system" if missing.
        for field in ("publisher_id", "task_type", "verification_method",
                      "verification_mode", "signal_type", "evidence_urls",
                      "verification_deadline", "signal_data"):
            self.assertIsNone(getattr(tc, field), f"TaskCreate.{field}")

    def test_agent_register_pow_fields_are_optional(self):
        """``challenge_id`` and ``nonce`` are optional so deployments running
        with ``IQX_REQUIRE_POW=0`` (the default through the observation
        window) can register without the PoW handshake."""
        reg = AgentRegister(id="a-003", name="charlie")
        self.assertIsNone(reg.challenge_id)
        self.assertIsNone(reg.nonce)

    def test_agent_register_accepts_pow_fields_when_present(self):
        reg = AgentRegister(id="a-004", name="dave",
                            challenge_id="c-001", nonce="0xdead")
        self.assertEqual(reg.challenge_id, "c-001")
        self.assertEqual(reg.nonce, "0xdead")


if __name__ == "__main__":
    unittest.main()
