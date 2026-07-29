"""SDK contract tests for ``iqx.schema``.

Locks down the public field shape of ``Task`` / ``Agent`` / ``RegistrationChallenge``
and the request DTOs an external Boss / Worker writes against. These are
pure unit tests — no DB session, no network. SQLModel's ``table=True``
persistence path is intentionally out of scope here; it is exercised by the
operator-private integration suite that ships alongside the central node.
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from iqx.tests.verified_contract import (
    AGENT_PUBLIC_FIELDS,
    AGENT_REGISTER_FIELDS,
    STAKE_DEFAULT,
    SUBMISSION_STATUSES,
    TASK_STATUS_VALUES,
    TASK_SUBMISSION_CREATE_FIELDS,
    TASK_SUBMISSION_READ_FIELDS,
    TERMINAL_SUBMISSION_STATUSES,
)
from iqx.schema import (
    Agent,
    AgentPublic,
    AgentRegister,
    AgentRegisterResponse,
    RegistrationChallenge,
    Task,
    TaskCreate,
    TaskStatus,
    TaskSubmission,
    TaskSubmissionCreate,
    TaskSubmissionRead,
)


class TestTaskStatusEnum(unittest.TestCase):
    """Lock the parent-task lifecycle enum.

    The state-machine values are read as plain strings by the node
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
        "SETTLED": "settled",
    }

    def test_enum_member_names_and_values(self):
        for name, value in self.EXPECTED.items():
            member = getattr(TaskStatus, name)
            self.assertEqual(member.value, value, f"{name}.value")

    def test_enum_member_set_is_exactly_seven_states(self):
        actual = {m.name for m in TaskStatus}
        self.assertEqual(actual, set(self.EXPECTED.keys()))

    def test_enum_values_match_the_verified_contract(self):
        self.assertEqual({m.value for m in TaskStatus}, set(TASK_STATUS_VALUES))

    def test_settled_is_constructible_from_the_live_string(self):
        """Regression: ``settled`` is emitted by the live API for a closed
        competing-submission parent. Before it was published here, building the
        enum from that value raised, so any client parsing a task list broke the
        moment one parent settled."""
        self.assertIs(TaskStatus("settled"), TaskStatus.SETTLED)

    def test_settled_is_assignable_as_a_parent_task_status(self):
        task = Task(id="t-settled", description="round over", budget=1.0,
                    status=TaskStatus.SETTLED)
        self.assertEqual(task.status, TaskStatus.SETTLED)
        self.assertEqual(task.model_dump()["status"], TaskStatus.SETTLED)

    def test_settled_is_not_a_submission_status(self):
        """``settled`` closes a *parent*; per-submission terminal values are
        ``verified`` / ``failed``. A client that reads ``settled`` off a
        submission row is reading the wrong level."""
        self.assertNotIn(
            TaskStatus.SETTLED.value, TERMINAL_SUBMISSION_STATUSES,
        )
        self.assertNotIn(TaskStatus.SETTLED.value, SUBMISSION_STATUSES)

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


class TestTaskSubmissionModel(unittest.TestCase):
    """Lock the competing-submission row.

    ``POST /tasks/{task_id}/submissions`` writes these rows and
    ``GET /tasks/{task_id}/submissions`` reads them, so the field set is a live
    wire contract, not a placeholder.
    """

    def _make_minimal_submission(self) -> TaskSubmission:
        return TaskSubmission(id="s-001", task_id="t-001", worker_id="w-001")

    def test_required_fields_instantiate(self):
        sub = self._make_minimal_submission()
        self.assertEqual(sub.id, "s-001")
        self.assertEqual(sub.task_id, "t-001")
        self.assertEqual(sub.worker_id, "w-001")

    def test_grade_fields_default_to_none(self):
        """An ungraded submission must serialize cleanly: nothing about the
        verdict is invented before grading, and ``elo_delta`` in particular
        stays None because no ELO is applied at submit time."""
        sub = self._make_minimal_submission()
        for field in ("result", "status", "submitted_at", "verified",
                      "verified_at", "verification_notes", "elo_delta",
                      "baseline_return"):
            self.assertIsNone(getattr(sub, field), f"{field} should default to None")

    def test_created_at_is_populated(self):
        sub = self._make_minimal_submission()
        self.assertIsInstance(sub.created_at, float)
        self.assertGreater(sub.created_at, 0)

    def test_accepts_every_live_status_value(self):
        for status in SUBMISSION_STATUSES:
            sub = TaskSubmission(id=f"s-{status}", task_id="t", worker_id="w",
                                 status=status)
            self.assertEqual(sub.status, status)

    def test_terminal_statuses_are_a_subset_of_the_vocabulary(self):
        self.assertTrue(
            set(TERMINAL_SUBMISSION_STATUSES).issubset(set(SUBMISSION_STATUSES))
        )

    def test_json_round_trip_preserves_fields(self):
        original = TaskSubmission(
            id="s-002", task_id="t-002", worker_id="w-002",
            result='{"is_alpha": false}', status="verified",
            submitted_at=1.0, verified=True, verified_at=2.0,
            verification_notes="ok", elo_delta=8, baseline_return=0.01,
        )
        rebuilt = TaskSubmission.model_validate(original.model_dump())
        self.assertEqual(rebuilt.model_dump(), original.model_dump())


class TestSubmissionDTOs(unittest.TestCase):
    """The submission DTOs are constructed and consumed by live endpoints.

    ``TaskSubmissionCreate`` is the request body of
    ``POST /tasks/{task_id}/submissions``; ``TaskSubmissionRead`` is the
    response element of ``GET /tasks/{task_id}/submissions``. Neither is a
    forward-declared placeholder, and documentation must not describe them as
    unused.
    """

    def test_create_field_set_matches_the_verified_contract(self):
        self.assertEqual(
            set(TaskSubmissionCreate.model_fields), TASK_SUBMISSION_CREATE_FIELDS,
        )

    def test_create_requires_all_three_fields(self):
        dto = TaskSubmissionCreate(task_id="t-1", worker_id="w-1", result="{}")
        self.assertEqual(dto.task_id, "t-1")
        for missing in ("task_id", "worker_id", "result"):
            kwargs = {"task_id": "t-1", "worker_id": "w-1", "result": "{}"}
            del kwargs[missing]
            with self.assertRaises(ValidationError, msg=f"{missing} must be required"):
                TaskSubmissionCreate(**kwargs)

    def test_read_field_set_matches_the_verified_contract(self):
        self.assertEqual(
            set(TaskSubmissionRead.model_fields), TASK_SUBMISSION_READ_FIELDS,
        )

    def test_read_exposes_no_credential_material(self):
        self.assertNotIn("api_key", TaskSubmissionRead.model_fields)

    def test_read_serializes_an_ungraded_submission(self):
        dto = TaskSubmissionRead(id="s-1", task_id="t-1", worker_id="w-1")
        self.assertIsNone(dto.verified)
        self.assertIsNone(dto.elo_delta)

    def test_read_accepts_a_persisted_row(self):
        """The endpoint builds the DTO from an ORM row with
        ``from_attributes``; that coercion must keep working."""
        row = TaskSubmission(id="s-3", task_id="t-3", worker_id="w-3",
                             status="failed", verified=False, elo_delta=-8)
        dto = TaskSubmissionRead.model_validate(row, from_attributes=True)
        self.assertEqual(dto.status, "failed")
        self.assertEqual(dto.elo_delta, -8)


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

    def test_staked_amount_defaults_to_zero(self):
        """Compatibility field. It is stored and echoed back and read by
        nothing — no staking, token, payment, or economic system is
        activated — but it is on the wire, so the model must carry it."""
        self.assertEqual(self._make_minimal_agent().staked_amount, STAKE_DEFAULT)

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


class TestStakeCompatibilityFields(unittest.TestCase):
    """``stake`` / ``staked_amount`` exist on the wire and are inert.

    They are published so client models match the live API. Nothing reads
    them: they gate no endpoint, are never spent or compared, and confer no
    advantage. These tests pin the shape, not any behaviour, because there is
    no behaviour to pin.
    """

    def test_register_request_carries_stake_defaulting_to_zero(self):
        reg = AgentRegister(id="a-005", name="erin")
        self.assertEqual(reg.stake, STAKE_DEFAULT)

    def test_register_request_field_set_matches_the_verified_contract(self):
        self.assertEqual(set(AgentRegister.model_fields), AGENT_REGISTER_FIELDS)

    def test_public_agent_view_carries_staked_amount(self):
        pub = AgentPublic(id="a-006", name="frank", elo=1200,
                          staked_amount=STAKE_DEFAULT)
        self.assertEqual(pub.staked_amount, STAKE_DEFAULT)

    def test_public_agent_field_set_matches_the_verified_contract(self):
        self.assertEqual(set(AgentPublic.model_fields), AGENT_PUBLIC_FIELDS)

    def test_register_response_extends_the_public_view_with_the_key(self):
        self.assertEqual(
            set(AgentRegisterResponse.model_fields),
            AGENT_PUBLIC_FIELDS | {"api_key"},
        )

    def test_a_nonzero_stake_is_accepted_and_confers_nothing(self):
        """A client may send a non-zero value; it round-trips and is inert.
        Eligibility is decided by ``elo`` against ``Task.min_elo`` alone."""
        reg = AgentRegister(id="a-007", name="grace", stake=42.0)
        self.assertEqual(reg.stake, 42.0)
        agent = Agent(id="a-007", name="grace", api_key="k", staked_amount=42.0)
        self.assertEqual(agent.staked_amount, 42.0)
        self.assertEqual(agent.elo, 1200)


if __name__ == "__main__":
    unittest.main()
