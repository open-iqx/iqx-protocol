"""SDK contract tests — public-bundle hygiene regression gates.

Asserts that the public-bundle source tree (iqx/ + pyproject.toml +
README.md + LICENSE) does not contain operator-private narrative
residue. Six invariants are checked:

  1. No URLs pointing at an operator-private source repository.
  2. No cross-references to operator-private documentation paths
     under docs/*.md (which do not ship in the public bundle).
  3. No internal operator-sequencing narrative tokens inside iqx/.
     README.md is excluded because its public-facing roadmap uses
     "Phase 1" / "Phase 2" / "Phase 3" labels for the protocol's
     milestone narrative; pyproject.toml and LICENSE are also
     excluded as project metadata / verbatim upstream text.
  4. No review-process attribution strings.
  5. No hardcoded operator-side filesystem paths for agent state —
     STATE_DIR is the canonical reference resolved at runtime.
  6. No references to any superseded public-repo URL — the canonical
     install URL is the only one that should appear.

iqx/tests/ ships in the source repo but is excluded from the
installed wheel (per pyproject `exclude = ["iqx.tests*"]`). It is
still included in this scan because the invariants must hold in the
public source tree.

The scanner uses pure-Python file reads + regex (no git dependency),
so the test runs anywhere pytest can find these files — including
unpacked tarballs or install-test containers.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

# Repo root: iqx/tests/test_public_bundle_hygiene.py → iqx/tests/ → iqx/ → repo
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# This file naturally contains the leak patterns as quoted test fixtures;
# exclude it from its own scan to avoid false-positive self-matches.
SELF_EXCLUDE = {"test_public_bundle_hygiene.py"}


def _iqx_py_files() -> list[Path]:
    """Return every .py file under iqx/ (excluding the self-match test
    file and __pycache__). Used by invariant #3 for the iqx/-only,
    .py-only scope."""
    files: list[Path] = []
    for p in sorted((REPO_ROOT / "iqx").rglob("*.py")):
        if p.name in SELF_EXCLUDE:
            continue
        if "__pycache__" in p.parts:
            continue
        files.append(p)
    return files


def _iqx_all_files() -> list[Path]:
    """Return every file under iqx/ regardless of extension (excluding
    the self-match test file and __pycache__). Used by the four
    invariants whose scope is the full public-bundle source tree.

    Scanning all extensions — not just .py — future-proofs the test:
    any additional text file shipped under iqx/ (a future README.md,
    a typing-marker py.typed, JSON / YAML data, etc.) is automatically
    covered. Binary files are silently skipped by _scan via
    UnicodeDecodeError.
    """
    files: list[Path] = []
    for p in sorted((REPO_ROOT / "iqx").rglob("*")):
        if not p.is_file():
            continue
        if p.name in SELF_EXCLUDE:
            continue
        if "__pycache__" in p.parts:
            continue
        files.append(p)
    return files


def _public_bundle_files() -> list[Path]:
    """Return every file that ships in the public-bundle source tree:
    iqx/ tree (all extensions) + pyproject.toml + README.md + LICENSE.
    iqx/tests/ ships in the source repo but is excluded from the
    installed wheel — included here because the invariants must hold
    in the public source tree too.
    """
    files: list[Path] = []
    for top in ("pyproject.toml", "README.md", "LICENSE"):
        p = REPO_ROOT / top
        if p.is_file():
            files.append(p)
    files.extend(_iqx_all_files())
    return files


def _scan(pattern: re.Pattern[str], files: list[Path]) -> list[tuple[str, int, str]]:
    """Return (relative-path, lineno, line) for every regex match across
    the given files. Binary files are silently skipped."""
    hits: list[tuple[str, int, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                rel = path.relative_to(REPO_ROOT)
                hits.append((str(rel), i, line.rstrip()))
    return hits


class TestPublicBundleHygiene(unittest.TestCase):
    """Regression gates for the public-bundle invariants."""

    def test_no_iqx_labs_urls(self):
        """Public bundle must not reference an operator-private source
        repository URL."""
        hits = _scan(re.compile(r"iqx-labs"), _public_bundle_files())
        self.assertEqual(hits, [], f"operator-private repo URL leak: {hits}")

    def test_no_xjin1020_urls(self):
        """Public bundle must not reference any superseded public-repo
        URL — only the canonical install URL should appear."""
        hits = _scan(re.compile(r"xjin1020/iqx-protocol"), _public_bundle_files())
        self.assertEqual(hits, [], f"superseded URL leak: {hits}")

    def test_no_docs_cross_refs(self):
        """Public bundle must not reference docs/*.md — operator-private
        documentation does not ship in the public bundle."""
        hits = _scan(
            re.compile(r"\bdocs/[A-Za-z0-9_\-]+\.md\b"),
            _public_bundle_files(),
        )
        self.assertEqual(hits, [], f"docs/* cross-ref leak: {hits}")

    def test_no_phase_week_track_pr_narrative_in_iqx(self):
        """iqx/ must not leak internal operator-sequencing labels.

        Scope is iqx/ only: README.md's public-facing roadmap uses
        'Phase 1' / 'Phase 2' / 'Phase 3' phase labels for the
        protocol's milestone narrative, which is intentional.
        pyproject.toml and LICENSE are also excluded as project
        metadata / verbatim upstream text.
        """
        hits = _scan(
            re.compile(
                r"Phase [AB1-9]|Week [0-9]|Track [A0]|PR #[0-9]|\bW[1-9]\b"
            ),
            _iqx_py_files(),
        )
        self.assertEqual(hits, [], f"operator-sequencing leak in iqx/: {hits}")

    def test_no_cto_review_attribution(self):
        """Public bundle must not contain review-process attribution
        strings."""
        hits = _scan(
            re.compile(r"CTO review", re.IGNORECASE),
            _public_bundle_files(),
        )
        self.assertEqual(hits, [], f"review-process attribution leak: {hits}")

    def test_no_agents_state_filesystem_refs(self):
        """Public bundle must not hardcode operator-side filesystem
        paths for agent state — STATE_DIR is the canonical reference
        resolved at runtime."""
        hits = _scan(re.compile(r"agents/state/"), _public_bundle_files())
        self.assertEqual(hits, [], f"agents/state/ filesystem leak: {hits}")


if __name__ == "__main__":
    unittest.main()
