"""Regression gates for example identities and write safeguards.

Two classes of defect these lock out, both of which shipped once:

  * a **fixed agent id** baked into an example, which collides with an
    already-registered identity on any shared node and leaves the developer
    with a 409 and no route forward that does not involve an operator;
  * a **silent write** to a node the developer did not consciously choose.

Every test here is offline. Nothing registers, submits, or opens a socket: the
safeguards are asserted at the point where they refuse, which is before any
HTTP call is made.
"""

from __future__ import annotations

import argparse
import re
import unittest
from pathlib import Path

from iqx.examples import (
    baseline_worker,
    boss_smart_money,
    identity,
    self_play,
    worker_judge,
)
from iqx.examples.identity import (
    DEFAULT_BASE_URL,
    PUBLIC_WRITE_OPT_IN_ENV,
    REFUSAL_EXIT_CODE,
    SideEffect,
    generate_agent_id,
    guard_writes,
    is_loopback_url,
    key_path_for,
    resolve_agent_id,
    resolve_base_url,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Every module under ``iqx.examples`` that can write to a node.
WRITE_CAPABLE = (baseline_worker, worker_judge, boss_smart_money, self_play)

#: Fixed ids these examples used to ship. Each is registered on at least one
#: live node, so re-introducing any of them as a default reproduces the exact
#: collision this work removed.
RETIRED_FIXED_IDS = (
    "baseline-worker-v1",
    "wallet-history-judge-v1",
    "smart-money-monitor-v1",
    "selfplay-publisher-v1",
    "selfplay-worker-v1",
)


def _example_sources() -> list[Path]:
    return [Path(m.__file__).resolve() for m in WRITE_CAPABLE]


class TestNoFixedCollidingIdentities(unittest.TestCase):
    """No example may default to a fixed agent id."""

    def test_retired_fixed_ids_are_not_assigned_anywhere(self):
        """The strings may still appear as a *filter* default — ``worker_judge``
        legitimately filters on a Boss's publisher id — but never as the
        identity this process registers as."""
        assignment = re.compile(
            r"^\s*(?:AGENT_ID|WORKER_ID|PUBLISHER_ID)\s*(?::[^=]+)?=\s*['\"]"
        )
        for path in _example_sources():
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                self.assertIsNone(
                    assignment.match(line),
                    f"{path.name}:{lineno} assigns a literal agent id: {line.strip()}",
                )

    def test_no_example_resolves_to_a_retired_fixed_id(self):
        """With no configuration at all, no example may land on one of the ids
        that are already registered on a live node."""
        prefixes = {
            baseline_worker: (baseline_worker.WORKER_ID_PREFIX,),
            worker_judge: (worker_judge.WORKER_ID_PREFIX,),
            boss_smart_money: (boss_smart_money.AGENT_ID_PREFIX,),
            self_play: (self_play.PUBLISHER_ID_PREFIX,
                        self_play.WORKER_ID_PREFIX),
        }
        for module, module_prefixes in prefixes.items():
            for prefix in module_prefixes:
                self.assertNotIn(prefix, RETIRED_FIXED_IDS,
                                 f"{module.__name__} prefix is a retired id")
                resolved, source = resolve_agent_id(
                    prefix, env_var="IQX_TEST_DEFINITELY_UNSET",
                )
                self.assertEqual(source, "generated")
                self.assertNotIn(resolved, RETIRED_FIXED_IDS)

    def test_boss_module_identity_is_not_a_retired_fixed_id(self):
        """``boss_smart_money`` keeps a module-level ``AGENT_ID`` that ``main``
        rebinds. Its import-time value must already be generated, so even a
        caller that imports the module and reads the attribute without going
        through ``main`` never picks up a colliding id."""
        self.assertNotIn(boss_smart_money.AGENT_ID, RETIRED_FIXED_IDS)
        self.assertTrue(
            boss_smart_money.AGENT_ID.startswith(
                boss_smart_money.AGENT_ID_PREFIX + "-")
        )

    def test_each_example_exposes_an_env_var_and_a_prefix(self):
        expected = {
            baseline_worker: ("IQX_BASELINE_WORKER_ID",),
            worker_judge: ("IQX_JUDGE_WORKER_ID",),
            boss_smart_money: ("IQX_BOSS_AGENT_ID",),
            self_play: ("IQX_SELFPLAY_PUBLISHER_ID", "IQX_SELFPLAY_WORKER_ID"),
        }
        for module, env_vars in expected.items():
            source = Path(module.__file__).read_text()
            for env_var in env_vars:
                self.assertIn(env_var, source,
                              f"{module.__name__} must offer {env_var}")

    def test_every_example_accepts_an_identity_cli_option(self):
        for module in WRITE_CAPABLE:
            source = Path(module.__file__).read_text()
            self.assertIn("add_identity_args", source,
                          f"{module.__name__} must accept an identity option")


class TestGeneratedIdentitiesAreUnique(unittest.TestCase):
    """Consecutive runs must not collide."""

    def test_two_consecutive_generations_differ(self):
        self.assertNotEqual(generate_agent_id("w"), generate_agent_id("w"))

    def test_many_consecutive_generations_are_all_distinct(self):
        generated = {generate_agent_id("w") for _ in range(200)}
        self.assertEqual(len(generated), 200)

    def test_generated_id_keeps_the_prefix(self):
        self.assertTrue(generate_agent_id("baseline-worker").startswith(
            "baseline-worker-"))

    def test_resolution_precedence_is_cli_then_env_then_generated(self):
        cli, source = resolve_agent_id("w", cli_value="from-cli",
                                       env_var="IQX_TEST_UNSET_VAR")
        self.assertEqual((cli, source), ("from-cli", "cli"))

        import os
        os.environ["IQX_TEST_ID_VAR"] = "from-env"
        try:
            env, source = resolve_agent_id("w", env_var="IQX_TEST_ID_VAR")
            self.assertEqual((env, source), ("from-env", "env"))
            # CLI still wins over a set env var.
            both, source = resolve_agent_id("w", cli_value="from-cli",
                                            env_var="IQX_TEST_ID_VAR")
            self.assertEqual((both, source), ("from-cli", "cli"))
        finally:
            del os.environ["IQX_TEST_ID_VAR"]

        gen_a, source_a = resolve_agent_id("w", env_var="IQX_TEST_UNSET_VAR")
        gen_b, _ = resolve_agent_id("w", env_var="IQX_TEST_UNSET_VAR")
        self.assertEqual(source_a, "generated")
        self.assertNotEqual(gen_a, gen_b,
                            "two runs with no configuration must differ")

    def test_blank_values_fall_through_to_generation(self):
        resolved, source = resolve_agent_id("w", cli_value="   ")
        self.assertEqual(source, "generated")
        self.assertTrue(resolved.startswith("w-"))

    def test_key_file_is_derived_from_the_identity(self):
        """A fixed key filename would hand one identity's cached credential to
        another; deriving it from the id is what prevents that."""
        state = Path("/tmp/iqx-test-state")
        a = key_path_for("worker-aaa", state_dir=state)
        b = key_path_for("worker-bbb", state_dir=state)
        self.assertNotEqual(a, b)
        self.assertEqual(a.name, "worker-aaa.key")

    def test_key_filename_is_sanitised(self):
        path = key_path_for("../../etc/passwd", state_dir=Path("/tmp/s"))
        self.assertEqual(path.parent, Path("/tmp/s"))
        self.assertNotIn("/", path.name[:-4])


class TestDefaultTargetIsNotProduction(unittest.TestCase):
    """An unconfigured example must not be able to reach a remote node."""

    def test_default_base_url_is_loopback(self):
        self.assertTrue(is_loopback_url(DEFAULT_BASE_URL))

    def test_every_write_capable_example_defaults_to_the_loopback_default(self):
        for module in WRITE_CAPABLE:
            self.assertTrue(
                is_loopback_url(module.BASE_URL) or "IQX_BASE_URL" in
                Path(module.__file__).read_text(),
                f"{module.__name__} must default to loopback",
            )

    def test_no_example_hardcodes_a_remote_node(self):
        """The only absolute URLs allowed in example sources are third-party
        services the examples genuinely call, plus documentation links. A node
        address must always come from configuration."""
        allowed_hosts = {
            # Third-party services and block explorers the examples call or
            # link to. None of these is an IQX node.
            "api.etherscan.io", "etherscan.io", "api.coingecko.com",
            "api.llama.fi", "github.com", "arbiscan.io", "basescan.org",
        }
        url_re = re.compile(r"https?://([A-Za-z0-9._-]+)")
        for path in _example_sources():
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for host in url_re.findall(line):
                    if host in allowed_hosts or host in ("localhost", "127.0.0.1"):
                        continue
                    self.fail(f"{path.name}:{lineno} hardcodes host {host!r}")

    def test_loopback_detection(self):
        for url in ("http://localhost:8000", "http://127.0.0.1:9",
                    "http://app.localhost:3000", "http://[::1]:8000"):
            self.assertTrue(is_loopback_url(url), url)
        for url in ("https://node.example", "http://10.0.0.4:8000",
                    "https://sub.node.example:8443"):
            self.assertFalse(is_loopback_url(url), url)


class TestPublicWriteOptIn(unittest.TestCase):
    """A write to a non-loopback node requires explicit consent."""

    IDENTITIES = [("worker", "worker-abc123", "generated")]

    def test_refuses_without_opt_in(self):
        with self.assertRaises(SystemExit) as ctx:
            guard_writes(self.IDENTITIES, base_url="https://node.example",
                         side_effect=SideEffect.WORKER)
        self.assertEqual(ctx.exception.code, REFUSAL_EXIT_CODE)

    def test_allows_with_cli_opt_in(self):
        guard_writes(self.IDENTITIES, base_url="https://node.example",
                     side_effect=SideEffect.WORKER, cli_opt_in=True)

    def test_allows_with_env_opt_in(self):
        import os
        os.environ[PUBLIC_WRITE_OPT_IN_ENV] = "1"
        try:
            guard_writes(self.IDENTITIES, base_url="https://node.example",
                         side_effect=SideEffect.WORKER)
        finally:
            del os.environ[PUBLIC_WRITE_OPT_IN_ENV]

    def test_a_non_literal_one_is_not_consent(self):
        import os
        for value in ("0", "true", "yes", "", "TRUE"):
            os.environ[PUBLIC_WRITE_OPT_IN_ENV] = value
            try:
                with self.assertRaises(SystemExit, msg=f"{value!r} must not consent"):
                    guard_writes(self.IDENTITIES, base_url="https://node.example",
                                 side_effect=SideEffect.WORKER)
            finally:
                del os.environ[PUBLIC_WRITE_OPT_IN_ENV]

    def test_loopback_needs_no_opt_in(self):
        guard_writes(self.IDENTITIES, base_url=DEFAULT_BASE_URL,
                     side_effect=SideEffect.WORKER)

    def test_identity_and_disclosure_are_printed_before_the_refusal(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                guard_writes(self.IDENTITIES, base_url="https://node.example",
                             side_effect=SideEffect.WORKER)
        out = buf.getvalue()
        self.assertIn("worker-abc123", out, "the exact identity must be shown")
        self.assertIn("https://node.example", out, "the target must be shown")
        self.assertIn("PERSISTENT", out, "persistence must be disclosed")
        self.assertIn("PERMANENT", out, "permanence must be disclosed")
        self.assertIn("operator-only", out, "deletion policy must be disclosed")

    def test_empty_identity_is_refused(self):
        for identities in ([], [("worker", "  ", "generated")]):
            with self.assertRaises(SystemExit) as ctx:
                guard_writes(identities, base_url=DEFAULT_BASE_URL,
                             side_effect=SideEffect.WORKER)
            self.assertEqual(ctx.exception.code, REFUSAL_EXIT_CODE)

    def test_offline_examples_must_not_call_the_write_guard(self):
        with self.assertRaises(ValueError):
            guard_writes(self.IDENTITIES, base_url=DEFAULT_BASE_URL,
                         side_effect=SideEffect.OFFLINE)


class TestIncompleteConfigurationFailsSafely(unittest.TestCase):
    """A misconfigured node URL must refuse up front, not mid-request."""

    def _resolve_with(self, value):
        import os
        previous = os.environ.get("IQX_BASE_URL")
        os.environ["IQX_BASE_URL"] = value
        try:
            return resolve_base_url()
        finally:
            if previous is None:
                os.environ.pop("IQX_BASE_URL", None)
            else:
                os.environ["IQX_BASE_URL"] = previous

    def test_empty_url_is_refused(self):
        for value in ("", "   "):
            with self.assertRaises(SystemExit) as ctx:
                self._resolve_with(value)
            self.assertEqual(ctx.exception.code, REFUSAL_EXIT_CODE)

    def test_schemeless_or_unusable_url_is_refused(self):
        for value in ("node.example", "ftp://node.example", "https://", "::::"):
            with self.assertRaises(SystemExit) as ctx:
                self._resolve_with(value)
            self.assertEqual(ctx.exception.code, REFUSAL_EXIT_CODE)

    def test_a_usable_url_is_returned_without_a_trailing_slash(self):
        self.assertEqual(self._resolve_with("https://node.example/"),
                         "https://node.example")


class TestSideEffectClassification(unittest.TestCase):
    """Every example declares exactly one class, and says so where a reader
    will see it."""

    EXPECTED = {
        baseline_worker: SideEffect.WORKER,
        worker_judge: SideEffect.WORKER,
        boss_smart_money: SideEffect.BOSS,
        self_play: SideEffect.BOSS,
    }

    def test_each_module_declares_its_class(self):
        for module, expected in self.EXPECTED.items():
            self.assertEqual(module.SIDE_EFFECT, expected, module.__name__)

    def test_class_appears_in_the_module_docstring(self):
        for module, expected in self.EXPECTED.items():
            self.assertIn(expected.value, module.__doc__ or "",
                          f"{module.__name__} docstring must state its class")

    def test_class_appears_in_help_text(self):
        for module, expected in self.EXPECTED.items():
            epilog = identity.side_effect_epilog(expected)
            self.assertIn(expected.value, epilog)

    def test_boss_examples_are_marked_operator_oriented(self):
        for module in (boss_smart_money, self_play):
            self.assertIn("operator-oriented", (module.__doc__ or "").lower(),
                          f"{module.__name__} must not read as a public quickstart")

    def test_identity_options_are_described_in_help(self):
        parser = argparse.ArgumentParser()
        identity.add_identity_args(parser, env_var="IQX_X_ID", prefix="x")
        help_text = parser.format_help()
        self.assertIn("--agent-id", help_text)
        self.assertIn("IQX_X_ID", help_text)
        self.assertIn("--allow-public-writes", help_text)


if __name__ == "__main__":
    unittest.main()
