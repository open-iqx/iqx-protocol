"""IQX SDK — replay benchmark subpackage.

Provides a one-command CLI and a frozen ground-truth dataset that lets
an external developer score their Worker against the reference
``baseline_worker`` accuracy floor locally, without touching a live
node:

  python3 -m iqx.bench.replay --worker iqx.examples.baseline_worker:build_verdict

The dataset ships as package data (``iqx/bench/replay_dataset.jsonl``)
and is resolved via ``importlib.resources`` so the default works
identically from a source checkout and from a ``pip install``-ed wheel.

The benchmark is pure-offline by design: no CoinGecko, no DefiLlama,
no HTTP, no ``iqx.verifier`` import. Each record carries the verdict
the live verifier produced at the time, and scoring is a direct
comparison of the Worker's ``is_alpha`` boolean against the frozen
``verdict_was_alpha``.
"""
