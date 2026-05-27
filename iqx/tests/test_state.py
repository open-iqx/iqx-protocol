"""SDK contract tests for ``iqx.helpers.state.resolve_state_dir`` and
the downstream missing-watchlist non-crash behavior in
``iqx.examples.boss_smart_money.load_watchlist``.

Locks four invariants:

1. ``IQX_STATE_DIR`` env var override resolves with ``~`` expansion.
2. ``IQX_STATE_DIR`` env var takes priority over the auto-detect step.
3. Auto-detect returns ``<source-root>/agents/state`` when the source
   tree has an ``agents/`` directory next to the package.
4. Falls through to ``~/.iqx/state`` when there's no ``agents/`` dir.
5. ``load_watchlist()`` returns an empty list (not a crash) when no
   watchlist file exists under STATE_DIR — the non-crash path external
   pip-install users hit on first run.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iqx.helpers import state


class TestResolveStateDirEnvOverride(unittest.TestCase):
    def test_env_var_resolves_with_tilde_expansion(self):
        with mock.patch.dict(
            os.environ, {"IQX_STATE_DIR": "~/custom-iqx"}, clear=False
        ):
            result = state.resolve_state_dir()
        self.assertEqual(result, (Path.home() / "custom-iqx").resolve())

    def test_env_var_takes_priority_over_autodetect(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with mock.patch.dict(os.environ, {"IQX_STATE_DIR": tmp}, clear=False):
                result = state.resolve_state_dir()
            self.assertEqual(result, tmp_path.resolve())


class TestResolveStateDirAutoDetect(unittest.TestCase):
    """Auto-detect behavior with the package's ``__file__`` patched to a
    synthetic source-tree root.
    """

    def _resolve_with_repo_root(self, fake_repo_root: Path) -> Path:
        """Patch ``state.__file__`` so the resolver thinks the package
        lives at ``<fake_repo_root>/iqx/helpers/state.py``."""
        fake_file = fake_repo_root / "iqx" / "helpers" / "state.py"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IQX_STATE_DIR", None)
            with mock.patch.object(state, "__file__", str(fake_file)):
                return state.resolve_state_dir()

    def test_autodetect_uses_repo_agents_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "agents").mkdir()
            result = self._resolve_with_repo_root(tmp_path)
            # tmp_path on macOS resolves /var/folders/... to /private/var/...
            # The resolver applies .resolve() so the expected value must too.
            self.assertEqual(result, tmp_path.resolve() / "agents" / "state")

    def test_autodetect_falls_back_to_home_when_no_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._resolve_with_repo_root(Path(tmp))
        # No agents/ subdir was created → fall through to ~/.iqx/state.
        self.assertEqual(result, (Path.home() / ".iqx" / "state").resolve())


class TestLoadWatchlistMissingFileNonCrash(unittest.TestCase):
    """When neither the operator watchlist file nor the shipped example
    file exists under STATE_DIR (the external pip-install first-run
    scenario), ``load_watchlist()`` must return ``[]`` rather than
    raising — a hard contract the audit's "non-crash failure mode"
    claim depends on.
    """

    def test_returns_empty_list_when_no_watchlist_files_exist(self):
        # Reset the module-level log-suppression flag so the test exercises
        # the first-call path. Direct attribute access is intentional —
        # this is the same module-level mutable state load_watchlist reads.
        from iqx.examples import boss_smart_money

        original_logged = boss_smart_money._watchlist_logged[0]
        original_watchlist_path = boss_smart_money.WATCHLIST_PATH
        original_example_path = boss_smart_money.WATCHLIST_EXAMPLE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                # Point both paths at non-existent files under a fresh dir.
                boss_smart_money.WATCHLIST_PATH = (
                    tmp_path / "smart_money_watchlist.json"
                )
                boss_smart_money.WATCHLIST_EXAMPLE_PATH = (
                    tmp_path / "smart_money_watchlist.example.json"
                )
                boss_smart_money._watchlist_logged[0] = False
                result = boss_smart_money.load_watchlist()
            self.assertEqual(result, [])
        finally:
            boss_smart_money.WATCHLIST_PATH = original_watchlist_path
            boss_smart_money.WATCHLIST_EXAMPLE_PATH = original_example_path
            boss_smart_money._watchlist_logged[0] = original_logged


if __name__ == "__main__":
    unittest.main()
