"""CoinGecko price helpers + per-chain WETH addresses.

A shared helper module so the verifier and the example agents don't each
re-implement CoinGecko fetch + throttle + retry.

Two helpers ship:

- ``coingecko_price(chain, token_address, cache)`` — point-in-time USD price
  for an ERC-20 contract address. In-memory 60s TTL cache; one process-wide
  call budget enforced by ``_coingecko_throttle``.
- ``coingecko_market_chart_range(chain, token_address, ts_from, ts_to)`` —
  historical price series. Same throttle / 429 backoff as the point-in-time
  call; no in-memory cache (callers persist to disk).

Both share the throttle state (``_coingecko_last_call_ts``) **within a single
process**. That covers the test suite, ``iqx/examples/self_play.py``, and any
future single-process use that imports both helpers. In production,
``iqx/examples/boss_smart_money.py`` and the verifier poller run as separate
``python3`` invocations, each with its own module-level
``_coingecko_last_call_ts`` — there is no cross-process throttle. Per-process
rate budgeting at the call site (poll cadence × API key choice) is the
operator's responsibility for the live deployment.

Constants exposed for callers that need them directly (e.g. the verifier
looks up the WETH address for the chain it's grading):

- ``WETH_ADDRESS`` — per-chain WETH contract address. Used by the smart-money
  detector to spot the "wallet spent WETH" leg of a swap and by the verifier
  to fetch the ETH baseline price for ``price_move_4h``.
- ``COINGECKO_PLATFORM`` — chain name → CoinGecko platform-id (used when
  building the ``/simple/token_price/{platform}`` URL).
- ``COINGECKO_BASE`` — base URL for the CoinGecko v3 API.
"""

from __future__ import annotations

import time
from typing import Optional

import requests

# ---- HTTP timeout ------------------------------------------------------------
#
# Local constant — keeps this module free of cross-module back-references.
HTTP_TIMEOUT_SEC = 20


# ---- CoinGecko configuration -------------------------------------------------

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# CoinGecko platform IDs (used to look up ERC-20 prices by contract address).
COINGECKO_PLATFORM = {
    "arbitrum": "arbitrum-one",
    "base": "base",
}

# CoinGecko free-tier rate limits as of late 2025 are nominally 10-30 req/min,
# so cap at ~6 req/s (one request per ~170 ms). On HTTP 429 we back off
# exponentially up to COINGECKO_MAX_RETRIES before giving up.
COINGECKO_MIN_INTERVAL_SEC = 0.17
COINGECKO_MAX_RETRIES = 3
COINGECKO_BACKOFF_BASE_SEC = 1.0
_coingecko_last_call_ts: list = [0.0]  # module-level mutable state, sloppy on
                                       # purpose — the agent + verifier are
                                       # single-threaded, so a list-of-one
                                       # avoids `global` declarations.


# ---- WETH addresses ----------------------------------------------------------
#
# Per-chain WETH addresses — used to detect the "wallet spent WETH" leg of a
# swap and to fetch the ETH baseline price.
WETH_ADDRESS = {
    "arbitrum": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    "base": "0x4200000000000000000000000000000000000006",
}


# ---- helpers -----------------------------------------------------------------

def _coingecko_throttle() -> None:
    """Block until at least COINGECKO_MIN_INTERVAL_SEC has elapsed since the
    last CoinGecko call. Single-process, single-threaded — fine for the agent
    + verifier (both run in their own one-shot processes)."""
    now = time.time()
    elapsed = now - _coingecko_last_call_ts[0]
    if elapsed < COINGECKO_MIN_INTERVAL_SEC:
        time.sleep(COINGECKO_MIN_INTERVAL_SEC - elapsed)
    _coingecko_last_call_ts[0] = time.time()


def coingecko_price(chain: str, token_address: str, cache: dict) -> Optional[float]:
    """Return USD price for a contract address, or None if not listed.

    Uses an in-memory TTL cache (60s) keyed by (chain, address). Throttles to
    ~6 req/s and retries with exponential backoff on HTTP 429 up to
    COINGECKO_MAX_RETRIES, so volatile-day bursts don't silently drop clusters.
    On any other non-200 or network error, returns None and the caller drops
    the cluster.
    """
    key = (chain, token_address.lower())
    now = time.time()
    cached = cache.get(key)
    if cached and now - cached[0] < 60:
        return cached[1]

    platform = COINGECKO_PLATFORM[chain]
    url = f"{COINGECKO_BASE}/simple/token_price/{platform}"
    params = {"contract_addresses": token_address, "vs_currencies": "usd"}

    data = None
    for attempt in range(COINGECKO_MAX_RETRIES + 1):
        _coingecko_throttle()
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SEC)
        except requests.RequestException:
            return None
        if resp.status_code == 200:
            try:
                data = resp.json() or {}
            except ValueError:
                return None
            break
        if resp.status_code == 429 and attempt < COINGECKO_MAX_RETRIES:
            # Honor Retry-After if CoinGecko sends one; otherwise exponential.
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else (
                    COINGECKO_BACKOFF_BASE_SEC * (2 ** attempt)
                )
            except ValueError:
                wait = COINGECKO_BACKOFF_BASE_SEC * (2 ** attempt)
            print(
                f"[coingecko] 429 rate-limited; sleeping {wait:.1f}s "
                f"(attempt {attempt + 1}/{COINGECKO_MAX_RETRIES})",
                flush=True,
            )
            time.sleep(wait)
            continue
        # Any other status — give up.
        return None

    if data is None:
        return None

    entry = data.get(token_address.lower()) or {}
    price = entry.get("usd")
    now = time.time()
    if not isinstance(price, (int, float)):
        cache[key] = (now, None)
        return None
    cache[key] = (now, float(price))
    return float(price)


def coingecko_market_chart_range(
    chain: str,
    token_address: str,
    ts_from: int,
    ts_to: int,
) -> Optional[list[tuple[int, float]]]:
    """Return list of (ts_unix, price_usd) over [ts_from, ts_to], sorted ascending.

    Wraps CoinGecko's `/coins/{platform}/contract/{address}/market_chart/range`.
    Granularity is automatic per the CoinGecko docs: 5-min for ranges <1d,
    hourly for 1-90d, daily beyond. Free-tier supported. Returns None on any
    non-200, malformed payload, or network error so the caller can drop the
    token without aborting the larger PnL walk.

    Reuses the same throttle and 429 backoff path as `coingecko_price` — same
    free-tier budget, just a different endpoint. No in-memory cache here:
    historical data is consumed by offline scripts that walk one (wallet,
    token) pair at a time and persist results to disk, so a per-process cache
    would never hit twice.
    """
    platform = COINGECKO_PLATFORM[chain]
    url = (
        f"{COINGECKO_BASE}/coins/{platform}/contract/"
        f"{token_address.lower()}/market_chart/range"
    )
    params = {"vs_currency": "usd", "from": ts_from, "to": ts_to}

    data = None
    for attempt in range(COINGECKO_MAX_RETRIES + 1):
        _coingecko_throttle()
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SEC)
        except requests.RequestException:
            return None
        if resp.status_code == 200:
            try:
                data = resp.json() or {}
            except ValueError:
                return None
            break
        if resp.status_code == 429 and attempt < COINGECKO_MAX_RETRIES:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else (
                    COINGECKO_BACKOFF_BASE_SEC * (2 ** attempt)
                )
            except ValueError:
                wait = COINGECKO_BACKOFF_BASE_SEC * (2 ** attempt)
            print(
                f"[coingecko] 429 on market_chart_range; sleeping {wait:.1f}s "
                f"(attempt {attempt + 1}/{COINGECKO_MAX_RETRIES})",
                flush=True,
            )
            time.sleep(wait)
            continue
        return None

    if data is None:
        return None

    raw = data.get("prices")
    if not isinstance(raw, list):
        return None

    out: list[tuple[int, float]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 2:
            continue
        ts_ms, price = row[0], row[1]
        if isinstance(ts_ms, (int, float)) and isinstance(price, (int, float)):
            out.append((int(ts_ms / 1000), float(price)))
    return out
