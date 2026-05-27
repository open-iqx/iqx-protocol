"""SDK verifier — DB-agnostic registry + reference verification methods.

External operators import from this module to extend the verifier with new
methods (``@register_verifier(method_id)``) or to run the registry against a
hydrated ``(task, ctx)`` pair (``verify(task, ctx)``). The central node ships
a thin poller (operator-private) that loads tasks from the DB, hydrates
``ctx``, calls ``verify()``, and persists the returned ``Verdict``.

Context dict keys (poller-hydrated):
  - ``protocol_index``   — slug-keyed DefiLlama protocols dict (TVL handler)
  - ``_protocol_error``  — set when DefiLlama prefetch failed (TVL handler defers)
  - ``price_cache``      — CoinGecko per-process LRU (price / worker_prediction)
  - ``baseline_returns`` — handler-populated side channel for ETH baseline

SDK boundary discipline: this module does NOT import the DefiLlama network
helper. ``_build_protocol_index`` is a pure data transformer over the
already-fetched protocol list; the poller is responsible for calling
``iqx.helpers.defillama.fetch_protocols`` and passing the result in via the
context dict.
"""

from __future__ import annotations

import json
from typing import Optional

from iqx.helpers.price import (
    WETH_ADDRESS as SMART_MONEY_WETH_ADDRESS,
    coingecko_price as smart_money_coingecko_price,
)
from iqx.schema import Task

# Registry machinery lives in iqx/registry.py so external Worker authors can
# ``from iqx.registry import register_verifier`` (or
# ``from iqx import register_verifier``) without pulling in the reference
# handlers and their network-helper deps (``requests``, the CoinGecko /
# DefiLlama path through ``iqx.helpers.price``). Re-exported here so
# existing consumers — the central-node poller and any test that accesses
# ``iqx.verifier._REGISTRY`` / ``iqx.verifier.verify_worker_prediction`` —
# keep working unchanged. The re-exports are aliases (same objects), so the
# four ``@register_verifier``-decorated handlers below still mutate the
# canonical ``_REGISTRY`` dict at module load.
from iqx.registry import (  # noqa: F401  (re-export for backward compat)
    _REGISTRY,
    Verdict,
    VerifyFn,
    register_verifier,
    verify,
)


# ---- handler-intrinsic thresholds (locked behavior — do not change) ----------

# Pass threshold for the TVL method: the protocol must retain ≥80% of the TVL
# it had at signal time. Below that the surge was a flash that didn't stick.
RETENTION_PASS_RATIO = 0.8
# Pass threshold for the price-move method: the target token must move at least
# this much in the tracked direction over the 4h horizon.
PRICE_MOVE_PASS_PCT = 0.03


# ---- registered methods ------------------------------------------------------


def _build_protocol_index(protocols: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for p in protocols:
        key = p.get("slug") or p.get("name")
        if key:
            index[key] = p
    return index


@register_verifier("defillama_tvl_retention_24h")
def verify_tvl_retention(task: Task, ctx: dict) -> tuple[bool, str]:
    """Pass iff the protocol retained ≥80% of its TVL after the surge.

    Expects `ctx["protocol_index"]` populated from a DefiLlama fetch and
    `task.result` to be the JSON payload base_tvl.py writes at submit time
    (with at least `slug` and `current_tvl_usd` fields).
    """
    protocol_index = ctx.get("protocol_index") or {}

    try:
        signal = json.loads(task.result) if task.result else {}
    except json.JSONDecodeError:
        return False, "result payload is not valid JSON"

    slug = signal.get("slug")
    surge_tvl = signal.get("current_tvl_usd")
    if not slug or not isinstance(surge_tvl, (int, float)) or surge_tvl <= 0:
        return False, "result payload missing slug or current_tvl_usd"

    protocol = protocol_index.get(slug)
    if protocol is None:
        return False, f"protocol '{slug}' not found on DefiLlama"

    current_tvl = protocol.get("tvl")
    if not isinstance(current_tvl, (int, float)) or current_tvl <= 0:
        return False, f"DefiLlama reports no usable TVL for '{slug}'"

    retained = current_tvl / surge_tvl
    retained_pct = round(retained * 100, 1)
    if retained >= RETENTION_PASS_RATIO:
        return True, (
            f"TVL retained {retained_pct}% "
            f"(${current_tvl:,.0f} of ${surge_tvl:,.0f} surge value)"
        )
    return False, (
        f"TVL fell to ${current_tvl:,.0f}, only {retained_pct}% retained "
        f"(threshold {int(RETENTION_PASS_RATIO * 100)}%)"
    )


@register_verifier("price_move_4h")
def verify_price_move(task: Task, ctx: dict) -> tuple[bool, str]:
    """Pass iff the target token moved ≥3% in the tracked direction over 4h.

    Reads the smart-money agent's result payload for chain, token_address,
    direction, price_at_signal_usd, and eth_price_at_signal_usd. Re-fetches
    the current USD price for both the target token and WETH from CoinGecko
    (same source the agent used at signal time). Records ETH return for the
    same window into `ctx["baseline_returns"][task.id]` so the post-verdict
    HTTP call can persist it to `Task.baseline_return`.

    v1 only fires on buy-direction (direction="up") signals; sell support is
    a follow-up. A token CoinGecko no longer recognises (e.g. delisted) is
    treated as a fail with a descriptive note rather than crashed.
    """
    try:
        signal = json.loads(task.result) if task.result else {}
    except json.JSONDecodeError:
        return False, "result payload is not valid JSON"

    chain = signal.get("chain")
    token_address = signal.get("token_address")
    direction = signal.get("direction") or "up"
    price_at_signal = signal.get("price_at_signal_usd")
    eth_at_signal = signal.get("eth_price_at_signal_usd")
    if not chain or not token_address:
        return False, "result payload missing chain or token_address"
    if not isinstance(price_at_signal, (int, float)) or price_at_signal <= 0:
        return False, "result payload missing usable price_at_signal_usd"

    cache = ctx.setdefault("price_cache", {})
    current_price = smart_money_coingecko_price(chain, token_address, cache)
    if current_price is None:
        return False, (
            f"CoinGecko has no current USD price for {token_address[:10]}… "
            f"on {chain}"
        )

    ret = (current_price - price_at_signal) / price_at_signal
    ret_pct = round(ret * 100, 2)

    # Baseline (ETH return over the same wall-clock window). Stored regardless
    # of verdict so we can compute excess return later.
    baseline_pct: Optional[float] = None
    if isinstance(eth_at_signal, (int, float)) and eth_at_signal > 0:
        weth_addr = SMART_MONEY_WETH_ADDRESS.get(chain)
        eth_now = (smart_money_coingecko_price(chain, weth_addr, cache)
                   if weth_addr else None)
        if eth_now is not None:
            baseline = (eth_now - eth_at_signal) / eth_at_signal
            ctx.setdefault("baseline_returns", {})[task.id] = baseline
            baseline_pct = round(baseline * 100, 2)

    baseline_str = (f" (baseline ETH {'+' if baseline_pct >= 0 else ''}{baseline_pct}%)"
                    if baseline_pct is not None else "")

    if direction == "up":
        verified = ret >= PRICE_MOVE_PASS_PCT
        sign = "+" if ret >= 0 else ""
        threshold_pct = int(PRICE_MOVE_PASS_PCT * 100)
        if verified:
            return True, (
                f"price moved {sign}{ret_pct}% (≥{threshold_pct}% threshold)"
                f"{baseline_str}"
            )
        return False, (
            f"price moved only {sign}{ret_pct}% (threshold {threshold_pct}%)"
            f"{baseline_str}"
        )

    # v1 doesn't fire on direction="down" / "sell" clusters, but if some future
    # caller writes one we don't want to crash.
    return False, f"unsupported direction={direction!r}"


@register_verifier("worker_prediction_accuracy_4h")
def verify_worker_prediction(task: Task, ctx: dict) -> tuple[bool, str]:
    """Pass iff the Worker's `is_alpha` prediction matches the 4h price oracle.

    Role-split architecture: a Boss publishes a task (carrying the structured
    signal in `task.signal_data`); a Judge Worker submits its prediction in
    `task.result` as `{is_alpha, confidence, reasoning, evidence_tx,
    predicted_4h_return_pct}`. This verifier grades **Worker prediction
    accuracy**, not signal correctness — the Worker is right when its
    `is_alpha` boolean matches the actual 4h move (≥3% threshold, same as
    price_move_4h's bar):

      - Worker said `is_alpha=True`  AND price moved ≥ +3% → PASS (caught the alpha)
      - Worker said `is_alpha=False` AND price moved <  +3% → PASS (correctly skeptical)
      - Otherwise → FAIL

    The asymmetric threshold is intentional: the Boss task asks "is this
    real upside alpha?", so a downward move with `is_alpha=False` counts as
    a correct skeptical verdict (not a missed short opportunity).

    Reuses smart_money_coingecko_price for the oracle so signal-time and
    verify-time prices come from the same source as price_move_4h. Records
    ETH baseline to ctx["baseline_returns"][task.id] just like price_move_4h.

    The Boss is grading-bystander by design: ELO deltas land on the Worker
    only — the central node's ELO update routes the delta to
    `task.worker_id` (the Judge under the role-split).
    """
    # Boss spec — what was the question and the price context at signal time.
    if not task.signal_data:
        return False, "task.signal_data missing — Boss did not attach signal context"
    try:
        spec = json.loads(task.signal_data)
    except json.JSONDecodeError:
        return False, "signal_data is not valid JSON"

    chain = spec.get("chain")
    token_address = spec.get("token_address")
    price_at_signal = spec.get("price_at_signal_usd")
    eth_at_signal = spec.get("eth_price_at_signal_usd")
    if not chain or not token_address:
        return False, "signal_data missing chain or token_address"
    if not isinstance(price_at_signal, (int, float)) or price_at_signal <= 0:
        return False, "signal_data missing usable price_at_signal_usd"

    # Worker submission — the prediction we're grading.
    try:
        prediction = json.loads(task.result) if task.result else {}
    except json.JSONDecodeError:
        return False, "result payload is not valid JSON"

    is_alpha = prediction.get("is_alpha")
    if not isinstance(is_alpha, bool):
        return False, "result missing required boolean `is_alpha`"

    # Oracle: current price → actual 4h return.
    cache = ctx.setdefault("price_cache", {})
    current_price = smart_money_coingecko_price(chain, token_address, cache)
    if current_price is None:
        return False, (
            f"CoinGecko has no current USD price for {token_address[:10]}… "
            f"on {chain}"
        )
    ret = (current_price - price_at_signal) / price_at_signal
    ret_pct = round(ret * 100, 2)

    # Baseline (ETH return) — captured regardless of verdict for excess-return
    # analysis later. Same shape as price_move_4h.
    baseline_pct: Optional[float] = None
    if isinstance(eth_at_signal, (int, float)) and eth_at_signal > 0:
        weth_addr = SMART_MONEY_WETH_ADDRESS.get(chain)
        eth_now = (smart_money_coingecko_price(chain, weth_addr, cache)
                   if weth_addr else None)
        if eth_now is not None:
            baseline = (eth_now - eth_at_signal) / eth_at_signal
            ctx.setdefault("baseline_returns", {})[task.id] = baseline
            baseline_pct = round(baseline * 100, 2)
    baseline_str = (f" (baseline ETH {'+' if baseline_pct >= 0 else ''}{baseline_pct}%)"
                    if baseline_pct is not None else "")

    # Grading: did the Worker call it right?
    actual_alpha = ret >= PRICE_MOVE_PASS_PCT
    correct = (is_alpha == actual_alpha)
    sign = "+" if ret >= 0 else ""
    threshold_pct = int(PRICE_MOVE_PASS_PCT * 100)
    pred_label = "alpha" if is_alpha else "no-alpha"
    actual_label = "alpha" if actual_alpha else "no-alpha"

    if correct:
        return True, (
            f"Worker predicted {pred_label}; actual {actual_label} "
            f"({sign}{ret_pct}% vs {threshold_pct}% threshold){baseline_str}"
        )
    return False, (
        f"Worker predicted {pred_label}; actual {actual_label} "
        f"({sign}{ret_pct}% vs {threshold_pct}% threshold){baseline_str}"
    )


@register_verifier("echo")
def verify_echo(task: Task, _ctx: dict) -> tuple[bool, str]:
    """Pass iff the submitted result echoes the expected payload.

    Used exclusively by iqx/examples/self_play.py to force
    publisher_id != worker_id end-to-end. The expected payload is encoded
    in the task description as `echo:<token>`; the worker submits a JSON
    `{"echo": "<token>"}` body.
    """
    expected_token: Optional[str] = None
    if task.description and task.description.startswith("echo:"):
        expected_token = task.description.split("echo:", 1)[1].strip()
    if not expected_token:
        return False, "echo task missing expected token in description"

    try:
        payload = json.loads(task.result) if task.result else {}
    except json.JSONDecodeError:
        return False, "echo result is not valid JSON"

    submitted = payload.get("echo")
    if submitted != expected_token:
        return False, f"echo mismatch: expected '{expected_token}', got '{submitted}'"
    return True, f"echo matched ('{expected_token}')"
