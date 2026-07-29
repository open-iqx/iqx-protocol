"""Drift gates between the published contract and the verified node contract.

The published contract lives in three places that can fall out of step with each
other and with the node: the SDK models (``iqx.schema``), the verifier registry
(``iqx.verifier``), and the public documentation (``PROTOCOL.md``,
``TROUBLESHOOTING.md``, ``README.md``). ``iqx.tests.verified_contract`` is the
single frozen description all three are checked against.

**These tests are read-only and offline by default.** They perform no network
call, create nothing, and touch no node. An optional live check against a node's
OpenAPI document is available and **skips unless explicitly configured** — see
:class:`TestLiveOpenAPIDrift`. It issues one GET and never writes.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from iqx.registry import _REGISTRY
from iqx.schema import (
    AgentPublic,
    AgentRegister,
    AgentRegisterResponse,
    TaskStatus,
    TaskSubmission,
    TaskSubmissionCreate,
    TaskSubmissionRead,
)
from iqx.tests.verified_contract import (
    ABSENT_ENDPOINTS,
    AGENT_PUBLIC_FIELDS,
    AGENT_REGISTER_FIELDS,
    ANSWER_OPTIONAL_FIELDS,
    ANSWER_REQUIRED_FIELDS,
    ECHO_ANSWER_FIELD,
    ENDPOINTS,
    LEGACY_CLAIM_ENDPOINTS,
    PARENT_TERMINAL_STATUS,
    PRICE_MOVE_PASS_PCT,
    STAKE_REQUEST_FIELD,
    STAKE_RESPONSE_FIELD,
    SUBMISSION_READ_ENDPOINT,
    SUBMISSION_STATUSES,
    SUBMISSION_WRITE_ENDPOINT,
    TASK_STATUS_VALUES,
    TASK_SUBMISSION_CREATE_FIELDS,
    TASK_SUBMISSION_FIELDS,
    TASK_SUBMISSION_READ_FIELDS,
    TERMINAL_SUBMISSION_STATUSES,
    TVL_RETENTION_PASS_RATIO,
    VERIFICATION_METHODS,
)
import iqx.verifier as reference_verifier

# iqx/tests/test_contract_drift.py → iqx/tests/ → iqx/ → repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

PROTOCOL_MD = REPO_ROOT / "PROTOCOL.md"
TROUBLESHOOTING_MD = REPO_ROOT / "TROUBLESHOOTING.md"
README_MD = REPO_ROOT / "README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestSchemaMatchesVerifiedContract(unittest.TestCase):
    """The published models must describe exactly what the node accepts."""

    def test_task_status_values(self):
        self.assertEqual({m.value for m in TaskStatus}, set(TASK_STATUS_VALUES))

    def test_parent_terminal_status_is_published(self):
        self.assertIn(PARENT_TERMINAL_STATUS, {m.value for m in TaskStatus})

    def test_submission_row_fields(self):
        self.assertEqual(
            set(TaskSubmission.model_fields), TASK_SUBMISSION_FIELDS,
        )

    def test_submission_create_fields(self):
        self.assertEqual(
            set(TaskSubmissionCreate.model_fields), TASK_SUBMISSION_CREATE_FIELDS,
        )

    def test_submission_read_fields(self):
        self.assertEqual(
            set(TaskSubmissionRead.model_fields), TASK_SUBMISSION_READ_FIELDS,
        )

    def test_agent_register_fields(self):
        self.assertEqual(set(AgentRegister.model_fields), AGENT_REGISTER_FIELDS)

    def test_agent_public_fields(self):
        self.assertEqual(set(AgentPublic.model_fields), AGENT_PUBLIC_FIELDS)

    def test_register_response_carries_the_key_once(self):
        self.assertEqual(
            set(AgentRegisterResponse.model_fields),
            AGENT_PUBLIC_FIELDS | {"api_key"},
        )

    def test_stake_compatibility_fields_are_present_on_both_sides(self):
        self.assertIn(STAKE_REQUEST_FIELD, AgentRegister.model_fields)
        self.assertIn(STAKE_RESPONSE_FIELD, AgentPublic.model_fields)


class TestVerifierRegistryMatchesVerifiedContract(unittest.TestCase):
    """The registered method set is the complete set, and its thresholds are
    the ones documented."""

    def test_registered_methods_are_exactly_the_verified_set(self):
        self.assertEqual(set(_REGISTRY), set(VERIFICATION_METHODS))

    def test_price_move_threshold(self):
        self.assertEqual(
            reference_verifier.PRICE_MOVE_PASS_PCT, PRICE_MOVE_PASS_PCT,
        )

    def test_tvl_retention_threshold(self):
        self.assertEqual(
            reference_verifier.RETENTION_PASS_RATIO, TVL_RETENTION_PASS_RATIO,
        )


class TestAnswerContractIsGraded(unittest.TestCase):
    """The documented answer contract must be the one the grader enforces.

    Exercised through the reference handlers with a stub context so no oracle
    is called: every case below fails before any price lookup.
    """

    SPEC = ('{"chain": "arbitrum", "token_address": "0xabc", '
            '"price_at_signal_usd": 1.0}')

    def _task(self, result, signal_data=SPEC):
        from iqx.schema import Task
        return Task(id="t-1", description="q", budget=0.0,
                    result=result, signal_data=signal_data)

    def test_is_alpha_is_required(self):
        ok, notes = reference_verifier.verify_worker_prediction(
            self._task('{"confidence": 0.9}'), {},
        )
        self.assertFalse(ok)
        self.assertIn("is_alpha", notes)

    def test_is_alpha_must_be_a_boolean(self):
        for bad in ('{"is_alpha": "true"}', '{"is_alpha": 1}',
                    '{"is_alpha": null}'):
            ok, notes = reference_verifier.verify_worker_prediction(
                self._task(bad), {},
            )
            self.assertFalse(ok, f"{bad} must not grade")
            self.assertIn("is_alpha", notes)

    def test_required_field_set_is_exactly_is_alpha(self):
        self.assertEqual(ANSWER_REQUIRED_FIELDS, {"is_alpha": bool})

    def test_optional_answer_fields_are_never_required(self):
        """Every optional field must be absent-able. Proven by grading an
        answer that carries only ``is_alpha``: it must fail for a missing
        *price context*, not for a missing optional answer field."""
        ok, notes = reference_verifier.verify_worker_prediction(
            self._task('{"is_alpha": false}', signal_data=None), {},
        )
        self.assertFalse(ok)
        self.assertIn("signal_data", notes)
        for field in ANSWER_OPTIONAL_FIELDS:
            self.assertNotIn(field, notes)

    def test_echo_answer_field(self):
        from iqx.schema import Task
        task = Task(id="t-2", description="echo:abc", budget=0.0,
                    result='{"%s": "abc"}' % ECHO_ANSWER_FIELD)
        ok, _notes = reference_verifier.verify_echo(task, {})
        self.assertTrue(ok)


class TestDocumentationMatchesVerifiedContract(unittest.TestCase):
    """Documentation drift gates.

    Cheap substring checks, deliberately: they catch a document that was never
    updated when the contract moved, which is the failure this whole file
    exists to prevent.
    """

    def test_protocol_doc_exists(self):
        self.assertTrue(PROTOCOL_MD.is_file(), "PROTOCOL.md must ship")

    def test_protocol_documents_every_endpoint(self):
        text = _read(PROTOCOL_MD)
        for _method, path, _auth in ENDPOINTS:
            self.assertIn(path, text, f"{path} is undocumented")

    def test_protocol_documents_every_task_status(self):
        text = _read(PROTOCOL_MD)
        for value in TASK_STATUS_VALUES:
            self.assertIn(f"`{value}`", text, f"status {value} is undocumented")

    def test_protocol_documents_every_submission_status(self):
        text = _read(PROTOCOL_MD)
        for value in SUBMISSION_STATUSES:
            self.assertIn(f"`{value}`", text,
                          f"submission status {value} is undocumented")

    def test_protocol_documents_every_verification_method(self):
        text = _read(PROTOCOL_MD)
        for method in VERIFICATION_METHODS:
            self.assertIn(method, text, f"method {method} is undocumented")

    def test_protocol_documents_the_answer_contract(self):
        text = _read(PROTOCOL_MD)
        for field in list(ANSWER_REQUIRED_FIELDS) + list(ANSWER_OPTIONAL_FIELDS):
            self.assertIn(field, text, f"answer field {field} is undocumented")

    def test_protocol_names_the_submission_endpoints(self):
        text = _read(PROTOCOL_MD)
        self.assertIn(SUBMISSION_WRITE_ENDPOINT, text)
        self.assertIn(SUBMISSION_READ_ENDPOINT, text)

    def test_protocol_states_the_absent_endpoint_is_absent(self):
        """A caller reaching for ``GET /tasks/{task_id}`` must be told it does
        not exist rather than discovering it as a 404."""
        text = _read(PROTOCOL_MD)
        for method, path in ABSENT_ENDPOINTS:
            self.assertIn(f"**no** `{method} {path}`", text)

    def test_all_documented_relative_links_resolve(self):
        """Every relative markdown link in the shipped docs must point at a
        file that exists."""
        pattern = re.compile(r"\[[^\]]+\]\((?!https?://)([^)#\s]+)")
        for doc in (PROTOCOL_MD, TROUBLESHOOTING_MD, README_MD):
            for target in pattern.findall(_read(doc)):
                self.assertTrue(
                    (REPO_ROOT / target).exists(),
                    f"{doc.name} links to missing {target}",
                )


class TestLiveOpenAPIDrift(unittest.TestCase):
    """Optional read-only drift check against a node's OpenAPI document.

    **Skipped unless ``IQX_CONTRACT_CHECK_URL`` is set.** There is deliberately
    no default: a drift check must never acquire a target by inheriting one, and
    must never be pointed at a node by accident. When configured it issues a
    single ``GET /openapi.json`` and asserts the published endpoint set is a
    subset of what that node serves. It writes nothing, registers nothing, and
    submits nothing, so it cannot pollute the node it inspects.
    """

    def setUp(self):
        self.url = os.environ.get("IQX_CONTRACT_CHECK_URL", "").strip()
        if not self.url:
            self.skipTest(
                "IQX_CONTRACT_CHECK_URL is not set; live drift check skipped "
                "(offline gates above still ran)"
            )

    def _openapi(self) -> dict:
        import requests
        resp = requests.get(f"{self.url.rstrip('/')}/openapi.json", timeout=20)
        resp.raise_for_status()
        return resp.json()

    def test_published_endpoints_exist_on_the_node(self):
        served = self._openapi().get("paths") or {}
        missing = [
            f"{method} {path}"
            for method, path, _auth in ENDPOINTS
            if path not in served or method.lower() not in served[path]
        ]
        self.assertEqual(missing, [], f"published but not served: {missing}")

    def test_legacy_claim_endpoints_still_exist(self):
        """They are documented as live-for-compatibility; if a node drops them
        that documentation becomes wrong."""
        served = self._openapi().get("paths") or {}
        for path in LEGACY_CLAIM_ENDPOINTS:
            self.assertIn(path, served)

    def test_single_task_read_is_still_absent(self):
        served = self._openapi().get("paths") or {}
        for method, path in ABSENT_ENDPOINTS:
            if path in served:
                self.assertNotIn(
                    method.lower(), served[path],
                    f"{method} {path} now exists; PROTOCOL.md says it does not",
                )


if __name__ == "__main__":
    unittest.main()
