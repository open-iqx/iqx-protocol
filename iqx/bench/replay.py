"""Replay benchmark CLI — score a Worker against the frozen dataset.

Usage:

    python3 -m iqx.bench.replay                                   # default Worker
    python3 -m iqx.bench.replay --worker my_pkg.my_mod:my_verdict # custom Worker
    python3 -m iqx.bench.replay --dataset path/to/other.jsonl     # alt dataset
    python3 -m iqx.bench.replay --quiet                           # aggregate only

The Worker is any callable with signature ``(task: dict) -> dict | None``.
The wire-shape ``task`` dict carries ``id``, ``verification_method``,
``description``, and ``signal_data`` (JSON-encoded string) — exactly the
fields a Worker like ``iqx.examples.baseline_worker.build_verdict``
already reads. The CLI calls the Worker per record and scores its
``is_alpha`` boolean against the frozen ``verdict_was_alpha``.

Defensive grading (a malformed or buggy Worker never aborts the run):
  - Worker returns ``None``                       → counts as **incorrect** (no-call).
  - Worker returns dict missing ``is_alpha``      → counts as **incorrect**.
  - Worker returns dict with non-bool ``is_alpha``→ counts as **incorrect**.
  - Worker raises any exception                   → caught, logged per-record,
                                                    counts as incorrect, run continues.

The CLI also computes the **baseline floor**: what
``iqx.examples.baseline_worker:build_verdict`` would score on the same
dataset (always ``is_alpha=False``, so the floor is the fraction of
records whose frozen ``verdict_was_alpha`` is also ``False``). Exit code
``0`` when Worker accuracy ≥ baseline floor; ``1`` otherwise — lets a
follow-up CI integration gate Worker changes against the floor without
parsing output.

Pure offline: this module imports neither ``iqx.verifier`` nor any
network helper. The frozen dataset *is* the ground truth.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from iqx.bench.dataset import ReplayRecord, default_dataset_path, load_dataset


DEFAULT_WORKER = "iqx.examples.baseline_worker:build_verdict"
BASELINE_WORKER = DEFAULT_WORKER  # what the floor is computed against

WorkerCallable = Callable[[dict], Optional[dict]]


@dataclass(frozen=True)
class RecordResult:
    """Per-record grading outcome."""

    record_id: str
    predicted_is_alpha: Optional[bool]   # None when Worker skipped / errored
    actual_is_alpha: bool
    correct: bool
    note: str                              # short human-readable explanation


@dataclass(frozen=True)
class Score:
    """Aggregate scoring outcome."""

    correct: int
    total: int

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def resolve_worker(spec: str) -> WorkerCallable:
    """Import a Worker callable from a ``module:attribute`` spec.

    Raises ``ValueError`` with a clear message on any failure so a
    typo or bad path is surfaced before the dataset is loaded.
    """
    if ":" not in spec:
        raise ValueError(
            f"--worker must be of the form 'module:attribute', got {spec!r}"
        )
    module_name, attr = spec.split(":", 1)
    if not module_name or not attr:
        raise ValueError(
            f"--worker must be of the form 'module:attribute', got {spec!r}"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ValueError(
            f"--worker module '{module_name}' could not be imported: {e}"
        ) from e
    try:
        worker = getattr(module, attr)
    except AttributeError as e:
        raise ValueError(
            f"--worker module '{module_name}' has no attribute '{attr}'"
        ) from e
    if not callable(worker):
        raise ValueError(
            f"--worker target '{spec}' is not callable"
        )
    return worker  # type: ignore[return-value]


def _grade_one(record: ReplayRecord, worker: WorkerCallable) -> RecordResult:
    """Run one record through the Worker and grade defensively. Never raises."""
    task = record.as_task()
    try:
        verdict = worker(task)
    except Exception as e:  # noqa: BLE001 — defensive grading is the point
        return RecordResult(
            record_id=record.id,
            predicted_is_alpha=None,
            actual_is_alpha=record.verdict_was_alpha,
            correct=False,
            note=f"worker raised {type(e).__name__}: {e}",
        )

    if verdict is None:
        return RecordResult(
            record_id=record.id,
            predicted_is_alpha=None,
            actual_is_alpha=record.verdict_was_alpha,
            correct=False,
            note="worker returned None (no-call)",
        )
    if not isinstance(verdict, dict) or "is_alpha" not in verdict:
        return RecordResult(
            record_id=record.id,
            predicted_is_alpha=None,
            actual_is_alpha=record.verdict_was_alpha,
            correct=False,
            note="worker verdict missing 'is_alpha'",
        )
    predicted = verdict["is_alpha"]
    if not isinstance(predicted, bool):
        return RecordResult(
            record_id=record.id,
            predicted_is_alpha=None,
            actual_is_alpha=record.verdict_was_alpha,
            correct=False,
            note=f"worker verdict 'is_alpha' is non-bool ({type(predicted).__name__})",
        )

    correct = predicted == record.verdict_was_alpha
    pred_label = "alpha" if predicted else "no-alpha"
    actual_label = "alpha" if record.verdict_was_alpha else "no-alpha"
    note_suffix = f" — {record.verdict_notes}" if record.verdict_notes else ""
    return RecordResult(
        record_id=record.id,
        predicted_is_alpha=predicted,
        actual_is_alpha=record.verdict_was_alpha,
        correct=correct,
        note=f"predicted {pred_label}; actual {actual_label}{note_suffix}",
    )


def grade(
    records: list[ReplayRecord], worker: WorkerCallable
) -> tuple[list[RecordResult], Score]:
    """Grade every record. Returns (per-record results, aggregate Score)."""
    results = [_grade_one(r, worker) for r in records]
    correct = sum(1 for r in results if r.correct)
    return results, Score(correct=correct, total=len(records))


def _format_pct(score: Score) -> str:
    return f"{score.correct}/{score.total} ({score.accuracy * 100:.1f}%)"


def run(
    *,
    worker_spec: str = DEFAULT_WORKER,
    dataset_path: Optional[Path] = None,
    quiet: bool = False,
    stream=sys.stdout,
) -> int:
    """Programmatic entry-point. Returns the exit code (0 if Worker ≥
    baseline, 1 otherwise). The CLI is a thin wrapper around this."""
    worker = resolve_worker(worker_spec)
    records = load_dataset(dataset_path)
    path = dataset_path if dataset_path is not None else default_dataset_path()

    results, worker_score = grade(records, worker)

    # Baseline floor: always the reference baseline against the same dataset.
    if worker_spec == BASELINE_WORKER:
        baseline_score = worker_score
    else:
        baseline_worker = resolve_worker(BASELINE_WORKER)
        _, baseline_score = grade(records, baseline_worker)

    if not quiet:
        print(f"[replay] dataset: {path} ({len(records)} record(s))", file=stream)
        print(f"[replay] worker:  {worker_spec}", file=stream)
        for r in results:
            status = "PASS" if r.correct else "FAIL"
            print(f"[replay] {status} {r.record_id} — {r.note}", file=stream)

    print(
        f"[replay] worker accuracy: {_format_pct(worker_score)}; "
        f"baseline floor: {_format_pct(baseline_score)}",
        file=stream,
    )

    # Exit 0 when the Worker meets-or-exceeds the floor; 1 otherwise.
    return 0 if worker_score.correct >= baseline_score.correct else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay benchmark — score a Worker against the frozen "
                    "smart-money-shaped dataset. Pure offline; no network.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--worker", default=DEFAULT_WORKER,
        help=f"Worker callable in 'module:attribute' form "
             f"(default: {DEFAULT_WORKER}). The callable must accept a "
             f"wire-shape task dict and return a verdict dict (with an "
             f"'is_alpha' bool) or None.",
    )
    parser.add_argument(
        "--dataset", type=Path, default=None,
        help="Override the JSONL dataset path (default: shipped "
             "iqx/bench/replay_dataset.jsonl).",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-record lines; print aggregate only.",
    )
    args = parser.parse_args(argv)
    return run(
        worker_spec=args.worker,
        dataset_path=args.dataset,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    sys.exit(main())
