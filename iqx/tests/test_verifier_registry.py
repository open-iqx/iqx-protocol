"""SDK contract tests for ``iqx.registry`` + the re-export shim in
``iqx.verifier``.

Three layers of coverage:

1. ``TestRegistryMechanics`` — locks the decorator + dispatcher primitives
   in ``iqx.registry`` (signature, current overwrite behavior, graceful
   FAIL for unknown methods, idempotency).
2. ``TestRegistryReexportShim`` — locks the ``is``-identity guarantees the
   registry/verifier split relies on so external poller code that reaches
   into ``iqx.verifier._REGISTRY`` continues to see the canonical registry.
3. ``TestReferenceHandlersSmoke`` — confirms the four reference methods
   register on ``import iqx.verifier`` and each returns a ``Verdict`` for
   a synthetic input. **No grading-logic re-test** — the verifier freeze
   covers handler scoring, and the integration suite that ships alongside
   the central node covers depth.

Isolation pattern: the registry tests snapshot/restore the entire
``_REGISTRY`` dict in setUp / tearDown. A test that fails mid-way cannot
leave stray entries because tearDown always runs the full restore.
"""

from __future__ import annotations

import unittest
from unittest import mock

import iqx
import iqx.registry
import iqx.schema
import iqx.verifier
from iqx.registry import _REGISTRY, register_verifier, verify
from iqx.schema import Task, TaskStatus


def _make_task(verification_method: str, result: str | None = None,
               signal_data: str | None = None, description: str = "t") -> Task:
    """Synthetic Task for dispatcher tests. Not persisted."""
    return Task(
        id=f"t-{verification_method}",
        description=description,
        budget=1.0,
        verification_method=verification_method,
        result=result,
        signal_data=signal_data,
    )


class _RegistrySnapshotMixin:
    """Snapshot/restore ``iqx.registry._REGISTRY`` around every test.

    Locks the four reference handlers (``defillama_tvl_retention_24h``,
    ``echo``, ``price_move_4h``, ``worker_prediction_accuracy_4h``) so
    they survive any test outcome — including raised assertions before
    a targeted cleanup would run.
    """

    def setUp(self):
        self._registry_snapshot = dict(_REGISTRY)

    def tearDown(self):
        _REGISTRY.clear()
        _REGISTRY.update(self._registry_snapshot)


class TestRegistryMechanics(_RegistrySnapshotMixin, unittest.TestCase):
    """Primitives in ``iqx.registry``."""

    def test_decorator_stores_handler_under_method_id(self):
        @register_verifier("_test_registry_stores_001")
        def handler(task: Task, ctx: dict) -> tuple[bool, str]:
            return True, "ok"

        self.assertIs(_REGISTRY["_test_registry_stores_001"], handler)

    def test_decorator_returns_function_unmodified(self):
        """The decorator must be identity-returning so external callers can
        stack additional decorators on top of ``@register_verifier``."""
        def raw(task: Task, ctx: dict) -> tuple[bool, str]:
            return True, "ok"

        wrapped = register_verifier("_test_identity_002")(raw)
        self.assertIs(wrapped, raw)

    def test_decorator_overwrites_same_method_id_silently(self):
        """Locks the **current** registry behavior: a second registration
        for the same ``method_id`` wins, with no exception and no warning.

        This is intentionally framed as "current behavior", not a publicly-
        documented commitment — the registry-overwrite semantics may tighten
        later (e.g. start emitting a warning) without invalidating this test
        as long as the call still completes without raising and the second
        handler takes the slot.
        """
        @register_verifier("_test_overwrite_003")
        def first(task: Task, ctx: dict) -> tuple[bool, str]:
            return True, "first"

        @register_verifier("_test_overwrite_003")
        def second(task: Task, ctx: dict) -> tuple[bool, str]:
            return True, "second"

        self.assertIs(_REGISTRY["_test_overwrite_003"], second)
        # Sanity-check that calling verify() routes to the second.
        task = _make_task("_test_overwrite_003")
        verdict = verify(task, {})
        self.assertEqual(verdict, (True, "second"))

    def test_register_other_method_ids_unaffected(self):
        @register_verifier("_test_isolation_a_004")
        def handler_a(task: Task, ctx: dict) -> tuple[bool, str]:
            return True, "a"

        @register_verifier("_test_isolation_b_005")
        def handler_b(task: Task, ctx: dict) -> tuple[bool, str]:
            return True, "b"

        self.assertIs(_REGISTRY["_test_isolation_a_004"], handler_a)
        self.assertIs(_REGISTRY["_test_isolation_b_005"], handler_b)
        # Registering 'b' did not touch 'a' — the registry is a flat dict
        # with no aliasing across keys.
        self.assertIsNot(_REGISTRY["_test_isolation_a_004"],
                         _REGISTRY["_test_isolation_b_005"])

    def test_verify_dispatches_to_registered_handler(self):
        @register_verifier("_test_dispatch_006")
        def handler(task: Task, ctx: dict) -> tuple[bool, str]:
            return True, f"saw ctx_key={ctx.get('marker')!r}"

        task = _make_task("_test_dispatch_006")
        verdict = verify(task, {"marker": "hello"})
        self.assertEqual(verdict, (True, "saw ctx_key='hello'"))

    def test_verify_unknown_method_returns_graceful_fail(self):
        """Dispatcher returns a Verdict for unknown methods rather than
        raising — pollers can decide to skip / log / defer."""
        task = _make_task("_test_method_that_is_not_registered_007")
        verdict = verify(task, {})
        self.assertIsInstance(verdict, tuple)
        self.assertEqual(len(verdict), 2)
        verified, notes = verdict
        self.assertFalse(verified)
        self.assertIn("_test_method_that_is_not_registered_007", notes)

    def test_verify_idempotent(self):
        """Calling ``verify(task, ctx)`` twice with the same inputs returns
        equal Verdicts. The dispatcher itself does not mutate state; the
        registered handler may (e.g. populate ``ctx['baseline_returns']``),
        but the Verdict's truth/notes are stable for a deterministic
        handler."""
        @register_verifier("_test_idempotent_008")
        def handler(task: Task, ctx: dict) -> tuple[bool, str]:
            return True, f"task_id={task.id}"

        task = _make_task("_test_idempotent_008")
        ctx: dict = {}
        v1 = verify(task, ctx)
        v2 = verify(task, ctx)
        self.assertEqual(v1, v2)


class TestRegistryReexportShim(unittest.TestCase):
    """Step 7's ``iqx.verifier`` re-export shim must preserve ``is``-identity
    for the registry primitives so external poller code reaching into
    ``iqx.verifier._REGISTRY`` keeps seeing the canonical dict."""

    def test_iqx_verifier_registry_is_iqx_registry_registry(self):
        self.assertIs(iqx.verifier._REGISTRY, iqx.registry._REGISTRY)

    def test_iqx_verifier_register_verifier_is_iqx_registry_register_verifier(self):
        self.assertIs(iqx.verifier.register_verifier,
                      iqx.registry.register_verifier)

    def test_iqx_verifier_verify_is_iqx_registry_verify(self):
        self.assertIs(iqx.verifier.verify, iqx.registry.verify)

    def test_iqx_verifier_verdict_is_iqx_registry_verdict(self):
        self.assertIs(iqx.verifier.Verdict, iqx.registry.Verdict)

    def test_iqx_top_level_aliases(self):
        """``import iqx`` exposes the SDK vocabulary as ``is``-identical
        aliases of the canonical objects in ``iqx.schema`` / ``iqx.registry``."""
        self.assertIs(iqx.Task, iqx.schema.Task)
        self.assertIs(iqx.Agent, iqx.schema.Agent)
        self.assertIs(iqx.register_verifier, iqx.registry.register_verifier)
        self.assertIs(iqx.Verdict, iqx.registry.Verdict)


class TestReferenceHandlersSmoke(unittest.TestCase):
    """The four reference methods register on ``import iqx.verifier`` and
    each returns a ``Verdict`` for a synthetic input.

    **No grading-logic re-test** — those checks live in
    ``tests/test_verifier_worker_prediction.py`` (and the verifier freeze in
    preflight §3 locks the bodies). These tests just confirm the wiring
    works through the registry — that a real handler is reachable via
    ``verify(task, ctx)`` and returns a tuple of the right shape.
    """

    EXPECTED_METHODS = {
        "defillama_tvl_retention_24h",
        "echo",
        "price_move_4h",
        "worker_prediction_accuracy_4h",
    }

    def test_all_four_reference_methods_registered(self):
        self.assertTrue(
            self.EXPECTED_METHODS.issubset(_REGISTRY.keys()),
            f"missing reference handlers: "
            f"{self.EXPECTED_METHODS - _REGISTRY.keys()}",
        )

    def _assert_verdict_shape(self, verdict):
        self.assertIsInstance(verdict, tuple)
        self.assertEqual(len(verdict), 2)
        self.assertIsInstance(verdict[0], bool)
        self.assertIsInstance(verdict[1], str)

    def test_verify_tvl_retention_smoke(self):
        """No network — the protocol index is passed in via ctx."""
        task = _make_task(
            "defillama_tvl_retention_24h",
            result='{"slug": "foo", "current_tvl_usd": 100.0}',
        )
        ctx = {"protocol_index": {"foo": {"tvl": 100.0}}}
        verdict = verify(task, ctx)
        self._assert_verdict_shape(verdict)

    def test_verify_echo_smoke(self):
        """No network. ``echo:<token>`` matches submitted ``{"echo": "<token>"}``."""
        task = _make_task(
            "echo",
            description="echo:hello",
            result='{"echo": "hello"}',
        )
        verdict = verify(task, {})
        self._assert_verdict_shape(verdict)
        # Echo is deterministic and matches-on-equality, so a tight assert
        # is reasonable here (not network-dependent like the price handlers).
        self.assertEqual(verdict[0], True)

    def test_verify_price_move_smoke(self):
        """Network mocked. Mirrors the pattern in
        ``tests/test_verifier_worker_prediction.py:34`` —
        ``mock.patch.object(verifier, "smart_money_coingecko_price", ...)``.
        """
        task = _make_task(
            "price_move_4h",
            result=(
                '{"chain": "arbitrum",'
                ' "token_address": "0xaaa",'
                ' "direction": "up",'
                ' "price_at_signal_usd": 100.0,'
                ' "eth_price_at_signal_usd": 2000.0}'
            ),
        )
        # Return a non-None price so the handler completes the happy path.
        with mock.patch.object(iqx.verifier, "smart_money_coingecko_price",
                               return_value=110.0):
            verdict = verify(task, {})
        self._assert_verdict_shape(verdict)

    def test_verify_worker_prediction_smoke(self):
        """Network mocked. Synthetic ``signal_data`` (Boss spec) +
        ``result`` (Worker submission) of the right shapes."""
        task = _make_task(
            "worker_prediction_accuracy_4h",
            signal_data=(
                '{"chain": "arbitrum",'
                ' "token_address": "0xaaa",'
                ' "direction": "up",'
                ' "price_at_signal_usd": 100.0,'
                ' "eth_price_at_signal_usd": 2000.0}'
            ),
            result=(
                '{"is_alpha": true,'
                ' "confidence": 0.8,'
                ' "predicted_4h_return_pct": 5.0}'
            ),
        )
        with mock.patch.object(iqx.verifier, "smart_money_coingecko_price",
                               return_value=110.0):
            verdict = verify(task, {})
        self._assert_verdict_shape(verdict)


if __name__ == "__main__":
    unittest.main()
