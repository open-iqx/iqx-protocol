"""SDK contract tests for ``iqx.examples.worker_judge`` graceful-degrade
behavior when bot-army / wallet-PnL cache files are absent.

External pip-install users running ``python3 -m iqx.examples.worker_judge``
without operator-private cache files (``STATE_DIR/bot_army.json``,
``STATE_DIR/wallet_pnl.json``) must NOT crash — the module docstring
promises a "default-skeptical" fallback, and these tests lock that
contract so future refactors don't accidentally break the graceful-
degrade promise.

Symmetric to ``iqx/tests/test_state.py``'s ``load_watchlist()``
missing-file non-crash test for the boss example.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iqx.examples import worker_judge


class TestLoadJsonCacheMissingFileNonCrash(unittest.TestCase):
    """``_load_json_cache`` must return ``{}`` (not raise) when the cache
    file is absent or malformed — the graceful-degrade path the module
    docstring promises and ``build_verdict`` relies on for the
    default-skeptical fallback.
    """

    def test_returns_empty_dict_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "definitely-does-not-exist.json"
            result = worker_judge._load_json_cache(missing_path, "test-missing")
        self.assertEqual(result, {})

    def test_returns_empty_dict_when_file_is_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            malformed = Path(tmp) / "malformed.json"
            malformed.write_text("not valid json {")
            result = worker_judge._load_json_cache(malformed, "test-malformed")
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
