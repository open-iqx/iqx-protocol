"""SDK contract tests — public-bundle hygiene regression gates.

Asserts that the public-bundle source tree (iqx/ + every top-level markdown
document + pyproject.toml + LICENSE) contains neither operator-private residue
nor a claim the project cannot support.

Leak invariants:

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
  7. No concrete node address where the ``IQX_BASE_URL`` placeholder belongs.
  8. No committed credential material.

Truthfulness invariants — each pins a claim the published material must not
make, or must make:

  9. Onboarding is never presented as live, and its absence is stated where a
     reader will see it.
 10. The legacy claim path is never taught as the current path.
 11. The stake compatibility fields are never described as activated staking.

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


def _root_docs() -> list[Path]:
    """Return every top-level markdown document plus project metadata.

    Globbed rather than listed so a document added later is covered by the
    invariants automatically — an unscanned doc is exactly where a leak or an
    unsupported claim would survive.
    """
    files: list[Path] = []
    for p in sorted(REPO_ROOT.glob("*.md")):
        if p.is_file():
            files.append(p)
    for top in ("pyproject.toml", "LICENSE"):
        p = REPO_ROOT / top
        if p.is_file():
            files.append(p)
    return files


def _public_bundle_files() -> list[Path]:
    """Return every file that ships in the public-bundle source tree:
    iqx/ tree (all extensions) + top-level markdown + pyproject.toml + LICENSE.
    iqx/tests/ ships in the source repo but is excluded from the
    installed wheel — included here because the invariants must hold
    in the public source tree too.
    """
    files: list[Path] = list(_root_docs())
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

    def test_no_node_hostname_replaces_the_placeholder(self):
        """No concrete node address may appear where the placeholder belongs.

        Wherever the bundle shows how to configure ``IQX_BASE_URL``, the value
        must be a placeholder or a loopback address — never a real host. The
        check is structural on purpose: it cannot name the host it is guarding
        against, because writing that host down is the very thing it exists to
        prevent.
        """
        assignment = re.compile(r"IQX_BASE_URL[=\s]*[\"']?(https?://[^\s\"'`,)]+)")
        bad = []
        for path, lineno, line in _scan(assignment, _public_bundle_files()):
            for value in assignment.findall(line):
                host = value.split("//", 1)[1].split("/")[0].split(":")[0]
                is_placeholder = "<" in value and ">" in value
                is_loopback = host in ("localhost", "127.0.0.1", "0.0.0.0")
                is_example = host.endswith((".invalid", ".example", ".test"))
                if not (is_placeholder or is_loopback or is_example):
                    bad.append((path, lineno, line))
        self.assertEqual(bad, [], f"concrete node address published: {bad}")

    def test_onboarding_is_not_presented_as_live(self):
        """Public material must not claim a working onboarding flow.

        Onboarding is not live: no onboarding task family is published and no
        reference node URL ships here. Documentation that promises otherwise
        sends a developer to something that does not exist.
        """
        claims = re.compile(
            r"onboarding (?:is |now )?(?:live|available|ready|open)"
            r"|complete onboarding in \d"
            r"|onboarding task(?:s)? (?:are |is )?(?:available|published|waiting)",
            re.IGNORECASE,
        )
        hits = _scan(claims, _public_bundle_files())
        self.assertEqual(hits, [], f"onboarding presented as live: {hits}")

    def test_absence_of_onboarding_is_stated_where_a_reader_will_see_it(self):
        """The honest counterpart to the gate above: saying nothing is not
        enough, because silence reads as availability."""
        for name in ("README.md", "PROTOCOL.md"):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("no public onboarding", text.lower(),
                          f"{name} must state that onboarding is not available")

    def test_legacy_claim_is_not_taught_as_the_onboarding_path(self):
        """Every example that uses ``/claim`` must say it is the legacy path.

        The published material once taught single-claim as *the* lifecycle. A
        module that reaches for ``/claim`` without labelling it teaches the
        superseded path by omission.
        """
        for path in _iqx_py_files():
            if "examples" not in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "/claim" not in text:
                continue
            doc = text.split('"""')[1] if text.count('"""') >= 2 else ""
            self.assertIn(
                "LEGACY", doc.upper(),
                f"{path.name} uses the claim path without labelling it legacy",
            )

    def test_competing_submissions_is_documented_as_the_current_path(self):
        text = (REPO_ROOT / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("/tasks/{task_id}/submissions", text)
        self.assertIn("the current path", text.lower())

    def test_stake_fields_are_never_described_as_activated_staking(self):
        """The compatibility fields must never read as a working economic
        system. Any line mentioning them in prose has to carry a disclaimer, or
        say nothing about what they do."""
        activated = re.compile(
            r"stake (?:your|to|in order)"
            r"|staking (?:is |now )?(?:live|enabled|active|available|required)"
            r"|deposit (?:tokens|funds)"
            r"|(?:token|payment) system (?:is )?(?:live|enabled|active)",
            re.IGNORECASE,
        )
        hits = _scan(activated, _public_bundle_files())
        self.assertEqual(hits, [], f"stake described as activated: {hits}")

    def test_stake_disclaimer_is_present_where_the_fields_are_documented(self):
        """Either phrasing of the disclaimer is fine; its absence is not."""
        disclaimer = re.compile(
            r"(?is)(?:no staking, token, payment, or economic system is\s+activated"
            r"|not represent an activated staking, token, payment, or\s+economic\s+system)"
        )
        for name in ("README.md", "PROTOCOL.md"):
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            if "staked_amount" not in text:
                continue
            self.assertTrue(
                disclaimer.search(text),
                f"{name} documents the stake fields without the disclaimer",
            )

    def test_no_credential_material_is_committed(self):
        """No key, token, or secret literal anywhere in the bundle."""
        secrets_re = re.compile(
            r"(?:api_key|apikey|admin_key|secret|password|token)\s*=\s*"
            r"[\"'][A-Za-z0-9_\-]{16,}[\"']",
            re.IGNORECASE,
        )
        hits = _scan(secrets_re, _public_bundle_files())
        self.assertEqual(hits, [], f"credential material committed: {hits}")


if __name__ == "__main__":
    unittest.main()
