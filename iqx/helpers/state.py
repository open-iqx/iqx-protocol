"""Resolve the canonical IQX state directory.

Used by ``iqx.examples.*`` to find a stable on-disk location for
credentials, cursors, watchlists, and verdict caches across both
source-tree deployments (cloned from this repo) and pip-installed
deployments (under ``site-packages``).

Precedence:

  1. ``IQX_STATE_DIR`` environment variable — explicit override. Path
     is expanded (``~`` → home) and resolved. Useful for CI, tests, or
     operators with non-default layouts.

  2. ``<source-tree-root>/agents/state`` — auto-detected when the package
     is imported from a source tree that has an ``agents/`` directory
     next to the ``iqx/`` package. Preserves operator continuity for
     deployments running directly out of this repo: no env var needed,
     no credential migration.

  3. ``~/.iqx/state`` — the SDK default for external pip-installed
     consumers. Hidden directory in the user's home; cross-platform-safe.

The directory is NOT created by this function — callers create it on
first write (e.g. via ``STATE_DIR.mkdir(parents=True, exist_ok=True)``
in ``_register`` / ``save_state``).
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_state_dir() -> Path:
    """Return the canonical IQX state directory per the precedence above."""
    env = os.environ.get("IQX_STATE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    # iqx/helpers/state.py → iqx/helpers/ → iqx/ → <source-tree-root>
    repo_root = Path(__file__).resolve().parent.parent.parent
    if (repo_root / "agents").is_dir():
        return repo_root / "agents" / "state"
    return (Path.home() / ".iqx" / "state").resolve()
