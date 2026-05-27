"""Replay dataset loader.

The shipped dataset is a JSONL file under ``iqx/bench/replay_dataset.jsonl``
registered as setuptools package data, so ``importlib.resources`` resolves
the same path from both a source checkout and a pip-installed wheel.

Each record is a JSON object with these required fields:

  - ``id`` (str)                       — stable short identifier (e.g. ``rec01``).
  - ``verification_method`` (str)       — currently always
    ``worker_prediction_accuracy_4h``; locked by the scope-guard test.
  - ``signal_data`` (object)           — the Boss spec the Worker would have
    seen at signal time. Wire-shape keys: ``chain``, ``wallet``,
    ``token_address``, ``token_symbol``, ``price_at_signal_usd``,
    ``eth_price_at_signal_usd``, ``direction``, ``observed_at``.
  - ``verdict_was_alpha`` (bool)        — the frozen ground truth: what the
    live verifier said at the time (``True`` ⇔ price moved ≥ +3% over the
    4h horizon).
  - ``verdict_notes`` (str, optional)   — short human-readable summary of
    the actual outcome (e.g. ``actual +5.2% over 4h``).
  - ``description`` (str, optional)     — narrative description; defaults
    to ``is this real alpha?`` when absent.

Malformed records raise ``DatasetError`` with the offending line number /
record id so curation mistakes surface immediately rather than producing
silent grading drift.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Optional


DATASET_RESOURCE_NAME = "replay_dataset.jsonl"

# The bench is single-method today. Lock it here so the dataset can't
# silently grow to cover methods the scoring math wasn't designed for.
SUPPORTED_METHODS = frozenset({"worker_prediction_accuracy_4h"})

_REQUIRED_FIELDS = ("id", "verification_method", "signal_data", "verdict_was_alpha")


class DatasetError(ValueError):
    """Raised when a replay dataset file is missing, unreadable, or
    contains a malformed / incomplete record."""


@dataclass(frozen=True)
class ReplayRecord:
    """One frozen ground-truth record."""

    id: str
    verification_method: str
    signal_data: dict
    verdict_was_alpha: bool
    verdict_notes: str = ""
    description: str = "is this real alpha?"

    def as_task(self) -> dict:
        """Return a wire-shape task dict the Worker's ``build_verdict``
        callable can consume. ``signal_data`` is JSON-encoded to match
        what a real Task row carries on the wire.
        """
        return {
            "id": self.id,
            "verification_method": self.verification_method,
            "description": self.description,
            "signal_data": json.dumps(self.signal_data),
        }


def default_dataset_path() -> Path:
    """Resolve the shipped dataset via ``importlib.resources``.

    Using ``files()`` (Python 3.9+) means the same call works whether the
    SDK is imported from a source checkout, an installed wheel, or a
    zipped egg. Falls back to a ``Path`` because the dataset file is
    always on a real filesystem under both setuptools layouts we ship.
    """
    return Path(str(resources.files("iqx.bench").joinpath(DATASET_RESOURCE_NAME)))


def _validate_record(raw: Any, lineno: int) -> ReplayRecord:
    if not isinstance(raw, dict):
        raise DatasetError(
            f"line {lineno}: expected a JSON object, got {type(raw).__name__}"
        )
    for field in _REQUIRED_FIELDS:
        if field not in raw:
            raise DatasetError(
                f"line {lineno}: record missing required field '{field}'"
            )
    if not isinstance(raw["id"], str) or not raw["id"]:
        raise DatasetError(f"line {lineno}: 'id' must be a non-empty string")
    if raw["verification_method"] not in SUPPORTED_METHODS:
        raise DatasetError(
            f"line {lineno}: record id={raw['id']!r} has unsupported "
            f"verification_method={raw['verification_method']!r}; "
            f"supported: {sorted(SUPPORTED_METHODS)}"
        )
    if not isinstance(raw["signal_data"], dict):
        raise DatasetError(
            f"line {lineno}: record id={raw['id']!r} 'signal_data' must be an object"
        )
    if not isinstance(raw["verdict_was_alpha"], bool):
        raise DatasetError(
            f"line {lineno}: record id={raw['id']!r} 'verdict_was_alpha' "
            f"must be a JSON boolean"
        )
    return ReplayRecord(
        id=raw["id"],
        verification_method=raw["verification_method"],
        signal_data=raw["signal_data"],
        verdict_was_alpha=raw["verdict_was_alpha"],
        verdict_notes=raw.get("verdict_notes", ""),
        description=raw.get("description", "is this real alpha?"),
    )


def load_dataset(path: Optional[Path] = None) -> list[ReplayRecord]:
    """Load a JSONL replay dataset from ``path`` (default: the shipped
    dataset). Returns the records in file order so the CLI can emit
    deterministic per-record output.

    Blank lines and lines whose first non-whitespace char is ``#`` are
    treated as comments and skipped — gives curators a way to annotate
    the file in-place without breaking the loader.
    """
    if path is None:
        path = default_dataset_path()
    if not path.exists():
        raise DatasetError(f"replay dataset not found at {path}")

    records: list[ReplayRecord] = []
    seen_ids: set[str] = set()
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise DatasetError(f"line {lineno}: invalid JSON ({e.msg})") from e
        rec = _validate_record(raw, lineno)
        if rec.id in seen_ids:
            raise DatasetError(
                f"line {lineno}: duplicate record id={rec.id!r}"
            )
        seen_ids.add(rec.id)
        records.append(rec)

    if not records:
        raise DatasetError(f"replay dataset at {path} contains no records")
    return records
