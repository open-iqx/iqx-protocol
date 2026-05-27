"""Smart-money cluster monitoring agent — Boss-only example.

Polls Etherscan V2's multichain account API per watchlisted address on
Arbitrum. Groups transfers by tx hash to identify swaps (the wallet appears on
both `from` and `to` sides of the same tx, with at least one non-quote-asset
leg). Buy clusters that meet `MIN_SWAP_COUNT`, `MIN_TOTAL_USD`, and
`MIN_DISTINCT_ROUTERS` within a sliding `WINDOW_SEC` window are posted as
**Boss tasks** (`signal_type=smart_money_swap_cluster`,
`verification_method=worker_prediction_accuracy_4h`).

Role-split: this agent is Boss-only — `file_signal` posts to /tasks and
stops. A separate Judge Worker (`worker_judge`) claims the open task and
submits a structured prediction; the verifier
(`worker_prediction_accuracy_4h`) grades the Worker's prediction accuracy
at T+4h. The Boss is a grading bystander by design — ELO deltas land on
the Judge only. Pre-role-split tasks (legacy collapsed-role flow with
`verification_method=price_move_4h` where this agent self-claimed and
self-submitted) remain valid in the DB; the verifier still has
`price_move_4h` registered for them.

Chain scope:
- **Arbitrum is enabled.** Etherscan V2 free tier covers it.
- **Base is deferred.** Etherscan V2 free tier returns "Free API access is
  not supported for this chain" for Base (chainid 8453). The Base config
  (chain_id, quote-assets, router-allowlist, WETH, CoinGecko platform) is
  left in place so re-enabling is a one-line change to `SUPPORTED_CHAINS`
  once a Base provider is wired up (Blockscout's etherscan-compatible API
  or a paid Etherscan plan). Watchlist entries on Base are loaded but
  skipped with a one-line log message — the agent does not crash on a
  mixed-chain list.

Why Etherscan V2 instead of Alchemy: Alchemy's signup wall blocked
bring-up. Etherscan V2 is also strictly better for what we need on
Arbitrum — free tier is 100K calls/day (we use ~6K with the watchlist
size halved to one chain), and each ERC-20 row carries `tokenDecimal` so
notional is correctly normalised.

v1 fires on **buy clusters only** — direction is always "up", which keeps the
verifier simple. Sell-direction signals are deferred.

USD valuation at signal time uses CoinGecko's free `simple/token_price/{platform}`
endpoint with an in-memory TTL cache; the verifier re-fetches the same source at
T+4h so signal-time and verify-time prices are consistent. Tokens missing from
CoinGecko cause the cluster to be **dropped** — no router-quote fallback (gameable).

The `recent_swaps` buffer in ``STATE_DIR/smart_money.json`` persists
across polls so a multi-swap accumulation that straddles several poll
boundaries still triggers the threshold. `WINDOW_SEC` is 30 minutes — calibrated to
real retail accumulation cadence (one buy every ~30-60 min when actively
sizing into a position), not the original "MEV burst" assumption that drove
the 6-min v1 default. Revisit after observing 5-10 hits' realised pass rate;
tighten if precision drops.

Usage (module form is canonical):
    python3 -m iqx.examples.boss_smart_money                       # one-shot
    python3 -m iqx.examples.boss_smart_money --loop                # poll every 5 min forever
    python3 -m iqx.examples.boss_smart_money --dry-run             # detect, do not POST or register
    python3 -m iqx.examples.boss_smart_money --threshold-usd 5000  # lower bar for smoke tests
    python3 -m iqx.examples.boss_smart_money --reset-cursor 0xabc  # wipe cursor for one address

SDK shape:
    ``SmartMoneyConfig`` consolidates the environment-variable and CLI-flag
    surface ``__main__`` reads to construct a ``SmartMoneyBoss``. Defaults
    mirror today's module-level constants. ``SmartMoneyBoss`` is a thin
    wrapper that exposes ``.ensure_registered()``, ``.run_once()``,
    ``.loop()``, ``.reset_cursor()`` — it holds the config but delegates to
    the module-level functions below. Module-level mutable globals
    (Etherscan throttle, pool-classification cache, watchlist-load
    suppression flag) remain at module scope today; migrating them to
    instance attributes is tracked as a future SDK-ergonomics improvement.

    For SDK consumers: only ``dry_run``, ``verbose``, and the
    ``--threshold-usd`` override are wired through to the detector today
    (via ``run_once``'s existing args). Fields like ``base_url``,
    ``etherscan_api_key``, ``state_dir``, and threshold/window constants
    on ``SmartMoneyConfig`` document the surface but are read from module
    scope at runtime — overriding them on a ``SmartMoneyConfig`` instance
    has no effect until the helpers are wired to read from ``config``.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

# CoinGecko helpers + per-chain WETH addresses live in iqx/helpers/price.py
# so the verifier and other consumers don't have to import from this example
# agent. Re-exported here so existing call sites keep working unchanged.
from iqx.helpers.state import resolve_state_dir
from iqx.helpers.price import (
    COINGECKO_BACKOFF_BASE_SEC,
    COINGECKO_BASE,
    COINGECKO_MAX_RETRIES,
    COINGECKO_MIN_INTERVAL_SEC,
    COINGECKO_PLATFORM,
    WETH_ADDRESS,
    _coingecko_last_call_ts,
    _coingecko_throttle,
    coingecko_market_chart_range,
    coingecko_price,
)

# ---- agent identity ----------------------------------------------------------

AGENT_ID = "smart-money-monitor-v1"
AGENT_NAME = "Smart Money Monitor"
SIGNAL_TYPE = "smart_money_swap_cluster"
TASK_TYPE = "defi_alpha"
# Boss-only role: smart_money posts the question ("is this real alpha?")
# and a Judge Worker submits its prediction. The verifier method grades
# the Worker's prediction accuracy, not signal correctness. Pre-role-split
# tasks (under the legacy collapsed-role flow with "price_move_4h" where
# smart_money self-claimed and self-submitted) remain valid in the DB and
# the verifier still has price_move_4h registered for them.
VERIFICATION_METHOD = "worker_prediction_accuracy_4h"
VERIFICATION_MODE = "automatic"

# ---- thresholds & cadence ----------------------------------------------------

POLL_INTERVAL_SEC = 300              # 5 min
WINDOW_SEC = 1800                    # 30-min sliding cluster window. Widened from
                                      # 360 (6 min) after live data showed real
                                      # retail accumulation runs at one buy every
                                      # ~30-60 min (human decision cadence +
                                      # slippage management), not the "MEV burst"
                                      # pattern the v1 default assumed. 30 min ==
                                      # 6× the poll interval — a 2-buy accumulation
                                      # spanning up to 5 cycles still survives the
                                      # buffer prune. Trade-off: wider window =
                                      # more unrelated buys could land in the same
                                      # buffer. Other 6 gates (USD, KNOWN_POOL_NAMES
                                      # / ROUTER_ALLOWLIST, swap shape, buy-only,
                                      # CoinGecko price, dedup) cover that risk.
DEDUP_WINDOW_SEC = 4 * 3600          # don't re-signal same (chain, wallet, token) within 4h
VERIFICATION_HORIZON_SEC = 4 * 3600  # verifier grades at signal_time + 4h
FIRST_SIGHT_LOOKBACK_BLOCKS = 100    # never seed cursor=0; ~25 sec of Arbitrum
                                      # history (Arbitrum block time is ~250ms;
                                      # change if SUPPORTED_CHAINS adds slower chains)
ADDRESS_POLL_DELAY_SEC = 1.5         # stagger polls — Etherscan free tier is
                                      # 5 req/s, and we make 2 calls (tokentx +
                                      # txlist) per address; this keeps us well
                                      # under both the per-second and per-day caps.

# Realistic retail accumulation is 2-3 buys over 30-90 min, not 3+ in a
# single 6-min burst — empirical data from a representative wallet doing
# 2 buys 30 min apart on Arbitrum (a textbook conviction-accumulation
# pattern) was invisible to a 3/6-min gate.
#
# Defense in depth: the buy still needs to clear `MIN_TOTAL_USD` (CLI-tunable),
# `KNOWN_POOL_NAMES`/`ROUTER_ALLOWLIST` counterparty check, swap shape, buy
# direction, and CoinGecko price. A coordinated noise pattern that fakes 2 of
# those simultaneously is not "smart money" but worth knowing about anyway.
#
# Revisit plan: observe 5-10 fired signals' verified pass rate (the
# verifier's price_move_4h gate). If pass rate drops below ~30% the gate is
# too loose — tighten back to MIN_SWAP_COUNT=3 with WINDOW_SEC=3600 (1h).
# If pass rate sits comfortably above the random baseline, current setting
# is fine.
#
# A complementary single-buy ≥$5K signal type (recall play, vs. cluster's
# precision play) is tracked for follow-up; running both side-by-side will
# tell us which gate correlates better with the verifier verdict.
MIN_SWAP_COUNT = 2
# Global fallback for cluster USD floor. Real production thresholds come from
# per-entry overrides in the watchlist (wallets vary ~2 orders of magnitude
# in typical buy size, so a single global floor is the wrong shape).
# This value applies only when an entry doesn't pin its own `min_total_usd`,
# in which case load_watchlist() also emits a WARN so the operator notices.
# $300 matches the lowest live per-entry value (CHIP/ESP/EDGE anchor) so the
# fallback is a sane noise floor, not a silent kill-switch. Earlier $50_000
# was so high vs. real per-entry values ($300–$801) that an unset entry
# silently fell into "essentially never fires" territory.
MIN_TOTAL_USD = 300.0

# Original rationale: "two distinct routers" was a noise filter against MEV
# bots that route 100% of their flow through a single venue. The rule worked
# when ROUTER_ALLOWLIST was the only counterparty gate — bot flow
# concentrated, retail spread across aggregators.
#
# After the agent gained direct-to-pool swap support (KNOWN_POOL_NAMES),
# live data showed real conviction-buyers repeatedly hit the *same*
# AlgebraPool / UniswapV3Pool when accumulating one token — that's the
# signal we want to fire on, not noise. With MIN_DISTINCT_ROUTERS=2 a
# 6-buy AlgebraPool spree (USDC → target, ~hourly cadence) would have
# failed the gate and silently dropped, even though every other gate
# (count, USD threshold, fresh window, verified counterparty) was
# satisfied.
#
# Setting to 1 makes the gate a no-op against the other gates — kept as a
# named constant so future investigation (e.g., if signal precision drops
# and we want to reintroduce diversity as a filter) doesn't have to hunt
# for the magic number; just bump it back up.
MIN_DISTINCT_ROUTERS = 1

REQUEST_TIMEOUT_SEC = 20

# CoinGecko free-tier rate limiting. The public tier advertises ~10-30 req/min
# but we want headroom for the verifier sharing the same module-level helper,
# so cap at ~6 req/s (one request per ~170 ms). On HTTP 429 we back off
# exponentially up to COINGECKO_MAX_RETRIES before giving up.
# CoinGecko throttle / retry knobs live in iqx/helpers/price.py; imported at
# the top of this file for backward compat with consumers that still
# reference them as ``iqx.examples.boss_smart_money.COINGECKO_*``.

# Etherscan V2 free-tier rate limit is 3 req/s (the LITE tier is 5 req/s; an
# earlier version of this comment assumed LITE and set 0.25s, which the
# overnight log showed correlated with `Read timed out` errors). Cap at
# ~2.5 req/s for headroom against burst penalties.
ETHERSCAN_MIN_INTERVAL_SEC = 0.40
ETHERSCAN_MAX_RETRIES = 3
ETHERSCAN_BACKOFF_BASE_SEC = 1.0
_etherscan_last_call_ts: list = [0.0]


# Etherscan V2 requires the api key as a query-string parameter — when the
# request raises, requests' exception str() embeds the full URL including
# `apikey=...`. Without this scrubber, every transient DNS / 429 / timeout
# error would surface the key into logs. Apply to anything that might
# format an exception originating from an etherscan call.
_APIKEY_QUERY_RE = re.compile(r"apikey=[A-Za-z0-9]+", re.IGNORECASE)


def _redact_secrets(msg: str) -> str:
    return _APIKEY_QUERY_RE.sub("apikey=<REDACTED>", msg)

# ---- chain config ------------------------------------------------------------

# Etherscan V2 chain IDs (used as the `chainid` query parameter on V2 calls).
# Both chains are kept here so adding a Base path back is a one-line config
# change once we have a free-tier provider for Base (see SUPPORTED_CHAINS).
ETHERSCAN_CHAIN_ID = {
    "arbitrum": 42161,
    "base": 8453,
}

# Chains the agent will actually poll. Etherscan V2's free tier currently
# returns "Free API access is not supported for this chain" for Base
# (chainid 8453); only Ethereum-mainnet-class chains are free, and Arbitrum
# happens to be one of them. v1 runs Arbitrum-only; Base re-enablement
# (Blockscout or paid Etherscan) is a clean follow-up that doesn't touch
# detection / classification / verification.
SUPPORTED_CHAINS: set[str] = {"arbitrum"}

# COINGECKO_PLATFORM lives in iqx/helpers/price.py; re-exported via the
# top-of-file import so ``iqx.examples.boss_smart_money.COINGECKO_PLATFORM``
# still resolves for downstream consumers.

# Chain explorer prefixes used for evidence URLs.
EXPLORER_TX_PREFIX = {
    "arbitrum": "https://arbiscan.io/tx/",
    "base": "https://basescan.org/tx/",
}

# Quote-asset addresses (lowercased). Filtered out of swap classification so a
# buy is recognised as "wallet sent USDC, received TOKEN" — direction is set by
# the *target* (non-quote) leg.
QUOTE_ASSETS: dict[str, set[str]] = {
    "arbitrum": {
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC (native)
        "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",  # USDC.e (bridged)
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
        "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",  # DAI
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
    },
    "base": {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC (native)
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC (bridged)
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
        "0x4200000000000000000000000000000000000006",  # WETH (predeploy)
    },
}

# Router/aggregator allowlist (lowercased). A swap is only counted toward the
# cluster threshold if its tx's `to` address is in this set. Patterns that look
# like a swap but go to an unknown contract emit `unclassified-swap` warnings so
# the allowlist can be grown manually as new venues appear.
ROUTER_ALLOWLIST: dict[str, set[str]] = {
    "arbitrum": {
        "0xe592427a0aece92de3edee1f18e0157c05861564",  # Uniswap V3 SwapRouter
        "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",  # Uniswap V3 SwapRouter02
        "0x5e325eda8064b456f4781070c0738d849c824258",  # Uniswap Universal Router
        "0xc873fecbd354f5a56e00e710b90ef4201db2448d",  # Camelot V2 Router
        "0x1f721e2e82f6676fce4ea07a5958cf098d339e18",  # Camelot V3 Router
        "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506",  # Sushi RouteProcessor
        "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5 Aggregator
        "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x ExchangeProxy
        "0xa15bb66138824a1c7167f5e85b957d04dd34e468",  # Odos Router v2
    },
    "base": {
        "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43",  # Aerodrome Router
        "0x6cb442acf35158d5eda88fe602221b67b400be3e",  # Aerodrome Slipstream Router
        "0x327df1e6de05895d2ab08513aadd9313fe505d86",  # BaseSwap Router
        "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap V3 SwapRouter02
        "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router
        "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
        "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x ExchangeProxy
    },
}

# WETH_ADDRESS lives in iqx/helpers/price.py; re-exported via the top-of-file
# import so ``iqx.examples.boss_smart_money.WETH_ADDRESS`` still resolves for
# downstream consumers.

# Known DEX-pool implementation contract names, as reported by Etherscan's
# `getsourcecode` endpoint. Sophisticated retail traders on Arbitrum
# increasingly bypass routers and call pools directly (one less contract hop —
# saves gas, harder to MEV-sandwich). The earlier ROUTER_ALLOWLIST-only check
# misses 100% of these flows; this set lets us classify a `to` address as a
# legitimate swap counterparty when it's a known pool implementation.
#
# Names are matched exactly against the verified contract name. Adding a new
# DEX is a one-line append here — no per-pool address curation needed because
# every pool of a given DEX shares the same contract bytecode (and therefore
# the same Etherscan-reported name). Examples seen in live data:
#   UniswapV3Pool   — Uniswap V3 (Arbitrum, Base, mainnet)
#   AlgebraPool     — Camelot V3, QuickSwap V3, ThenaFi (Algebra is the V3 engine)
#   PancakeV3Pool   — PancakeSwap V3
#   UniswapV4Pool   — Uniswap V4 (when deployed)
#
# Unverified pool contracts can't be classified this way (Etherscan returns no
# ContractName); those still hit the [unclassified-swap] path and require
# manual triage. Acceptable v1 trade-off — the alternative (eth_call against
# the pool's slot0() to fingerprint it) is a meaningful complexity bump for
# a marginal coverage gain.
KNOWN_POOL_NAMES: set[str] = {
    "UniswapV3Pool",
    "UniswapV4Pool",
    "AlgebraPool",
    "PancakeV3Pool",
    "AerodromePool",   # Base mainnet — for when SUPPORTED_CHAINS adds Base
    "AerodromeCLPool",
}

# Module-level cache for `is_known_pool` lookups. Keyed by `(chain, addr_lc)`.
# A verified pool contract's name does not change after deployment, so a
# cache-hit is permanently valid. Misses (unverified, non-pool) are also
# cached so we don't repeatedly call Etherscan for the same dud address.
# Process-lifetime only — rebuilt on restart, which costs a few extra
# `getsourcecode` calls on the first poll cycle (free-tier budget: trivial).
_pool_classification_cache: dict[tuple[str, str], bool] = {}


# ---- paths -------------------------------------------------------------------

BASE_URL = os.environ.get("IQX_BASE_URL", "http://localhost:8000")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
# COINGECKO_BASE lives in iqx/helpers/price.py; re-exported via the
# top-of-file import.

# STATE_DIR resolution policy: see iqx.helpers.state.resolve_state_dir().
# Four files live under it:
#   - ``smart_money.key``            — dispatcher credential for AGENT_ID
#     ``smart-money-monitor-v1``. Basenames are stable across deployments
#     so existing credentials remain valid; no re-registration on restart.
#   - ``smart_money.json``           — per-(chain, address) block cursors,
#     rolling buffers, dedup history.
#   - ``smart_money_watchlist.json`` — operator-edited watchlist.
#   - ``smart_money_watchlist.example.json`` — repo-shipped fallback; not
#     included in the SDK install. External pip-install users without a
#     watchlist file get an empty list (a non-crash failure mode that
#     load_watchlist handles gracefully — locked by
#     iqx/tests/test_state.py).
STATE_DIR = resolve_state_dir()
KEY_PATH = STATE_DIR / "smart_money.key"
STATE_PATH = STATE_DIR / "smart_money.json"
WATCHLIST_PATH = STATE_DIR / "smart_money_watchlist.json"
WATCHLIST_EXAMPLE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "agents" / "smart_money_watchlist.example.json"
)


# ---- SDK config + Boss class -------------------------------------------------

@dataclass(frozen=True)
class SmartMoneyConfig:
    """Operational configuration for ``SmartMoneyBoss``.

    Consolidates the environment-variable + CLI-flag surface that
    ``__main__`` reads to construct a Boss. Defaults mirror today's
    module-level constants so a bare ``SmartMoneyConfig()`` reproduces
    the pre-Step-6 ``__main__`` behavior.

    Load-bearing fields in this PR (wired through ``SmartMoneyBoss`` →
    module-level ``run_once``):
      - ``threshold_usd`` — flows into the per-entry threshold fallback
      - ``dry_run``       — skips dispatcher writes (POST /tasks)
      - ``verbose``       — emits per-address diagnostics on quiet polls

    Documentation-only fields in this PR (still read from module scope
    by the helpers; overriding them on a ``SmartMoneyConfig`` instance
    has no effect on the detector until the follow-up cleanup PR wires
    helpers to read from ``config``):
      - ``base_url``, ``etherscan_api_key`` — used by HTTP helpers
        from module-level ``BASE_URL`` / ``ETHERSCAN_API_KEY``
      - ``state_dir`` — used by load_state/save_state/ensure_registered
        from module-level ``STATE_DIR`` / ``KEY_PATH`` / ``STATE_PATH``
      - ``poll_interval_sec`` — read from module ``POLL_INTERVAL_SEC``
        inside ``SmartMoneyBoss.loop()``
    """

    base_url: str = BASE_URL
    etherscan_api_key: str = ETHERSCAN_API_KEY
    threshold_usd: float = MIN_TOTAL_USD
    dry_run: bool = False
    verbose: bool = False
    state_dir: Path = STATE_DIR
    poll_interval_sec: int = POLL_INTERVAL_SEC

    @classmethod
    def from_env_and_args(cls, args: argparse.Namespace) -> "SmartMoneyConfig":
        """Build a config from environment variables + parsed argparse flags.

        Mirrors the pre-Step-6 ``__main__`` block: ``IQX_BASE_URL`` and
        ``ETHERSCAN_API_KEY`` are read at module-load time into the
        module-level ``BASE_URL`` / ``ETHERSCAN_API_KEY`` constants; this
        classmethod simply re-reads them for the dataclass. CLI flags
        override ``threshold_usd``, ``dry_run``, ``verbose``.
        """
        return cls(
            base_url=BASE_URL,
            etherscan_api_key=ETHERSCAN_API_KEY,
            threshold_usd=args.threshold_usd,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )


# ---- agent identity / auth -------------------------------------------------

def _register() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    resp = requests.post(
        f"{BASE_URL}/agents/register",
        json={"id": AGENT_ID, "name": AGENT_NAME},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    if resp.status_code == 409:
        # See iqx/examples/worker_judge.py:_register for the full rationale.
        # The dispatcher does not silently rotate existing keys; we can't
        # auto-rotate because /agents/{id}/rotate-key requires the current
        # key, which is exactly what we lack here.
        print(
            f"[registry] {AGENT_ID} already registered with a different "
            f"api_key. If you lost the local key file, ask an admin to "
            f"delete the agent row; if this is a fresh deploy clashing with "
            f"a stale id, choose a new AGENT_ID. Crashing — no silent "
            f"recovery.",
            flush=True,
        )
    resp.raise_for_status()
    api_key = resp.json()["api_key"]
    KEY_PATH.write_text(api_key)
    print(f"[registry] registered {AGENT_ID}; api_key saved to {KEY_PATH}", flush=True)
    return api_key


def _cached_key_authenticates(key: str) -> bool:
    """True iff `key` still authenticates as AGENT_ID on the dispatcher.

    See worker_judge._cached_key_authenticates for the full rationale —
    same shape, same trap: the old _agent_exists_on_server check was a
    public unauthenticated GET that confirmed the row existed without
    proving the cached key still matched it, so a stale key persisted
    silently across any external re-register.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/agents/me",
            headers={"X-Worker-Id": AGENT_ID, "X-API-Key": key},
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except requests.RequestException:
        return True
    return resp.status_code == 200


def ensure_registered() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        key = KEY_PATH.read_text().strip()
        if key and _cached_key_authenticates(key):
            return key
        if key:
            print(
                f"[registry] cached key no longer authenticates for "
                f"{AGENT_ID} (rotated or dispatcher state changed); "
                f"re-registering",
                flush=True,
            )
    return _register()


# ---- watchlist ---------------------------------------------------------------

# Module-level toggle so load_watchlist's verbose triage logs (fallback warning,
# invalid entries, skipped-because-unsupported entries, summary count) only fire
# on the first call per process. Subsequent calls re-read the file silently —
# preserving hot-edit behaviour for the looping agent without spamming stdout
# with the same skip messages every 5 minutes. List-of-one to dodge `global`,
# matching the throttle-state pattern elsewhere in this module.
_watchlist_logged: list[bool] = [False]


def _coerce_positive_number(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _coerce_positive_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i > 0 else None


def load_watchlist() -> list[dict]:
    """Load the watchlist from ``WATCHLIST_PATH`` (under ``STATE_DIR``).

    Falls back to the example file shipped in the repo
    (``WATCHLIST_EXAMPLE_PATH``) if the user hasn't yet provided one —
    useful for first-run smoke tests, but emits a warning so the user
    knows to localize their list. If neither file exists (e.g. external
    pip-install users on first run without a custom watchlist), returns
    an empty list — a non-crash failure mode the agent handles gracefully.

    Triage logs (fallback warning, invalid-entry skips, unsupported-chain
    skips, and the summary count) only print on the first call per process.
    ``--loop`` re-reads the file every cycle to pick up edits, but logging
    the same skip lines on each cycle floods stdout — see
    ``_watchlist_logged``.
    """
    log = not _watchlist_logged[0]
    path = WATCHLIST_PATH if WATCHLIST_PATH.exists() else WATCHLIST_EXAMPLE_PATH
    if log and path == WATCHLIST_EXAMPLE_PATH:
        print(
            f"[watchlist] {WATCHLIST_PATH} not found; falling back to "
            f"{WATCHLIST_EXAMPLE_PATH}. Copy and edit for live use.",
            flush=True,
        )
    if not path.exists():
        _watchlist_logged[0] = True
        return []
    entries = json.loads(path.read_text())
    cleaned: list[dict] = []
    skipped_unsupported = 0
    for e in entries:
        addr = (e.get("address") or "").lower()
        chain = (e.get("chain") or "").lower()
        if not addr.startswith("0x") or chain not in ETHERSCAN_CHAIN_ID:
            if log:
                print(f"[watchlist] skipping invalid entry {e!r}", flush=True)
            continue
        if chain not in SUPPORTED_CHAINS:
            # Chain is recognised but disabled in v1 (typically Base, which is
            # paywalled on Etherscan V2 free tier). One log line per entry so
            # the user sees their list isn't silently being ignored.
            if log:
                print(
                    f"[watchlist] skipping {chain} entry {addr} "
                    f"(label={e.get('label')!r}) — {chain} is deferred "
                    f"(Etherscan V2 free tier doesn't cover Base; SUPPORTED_CHAINS "
                    f"in iqx/examples/boss_smart_money.py controls this).",
                    flush=True,
                )
            skipped_unsupported += 1
            continue
        # Optional per-entry threshold overrides. Watchlist wallets vary by
        # ~2 orders of magnitude in typical buy size (a small-buyer wallet
        # might average ~$90/buy while a mid-cap accumulator clusters around
        # ~$400/buy in the same window), so a single global USD floor either
        # over-fires on small wallets or under-fires on large ones. Per-entry
        # overrides let each wallet declare its own gate; missing fields fall
        # back to the CLI/global defaults at detect time.
        # Validation is intentionally loose — we accept any positive number;
        # zero or negative values are treated as "unset" and trigger fallback.
        entry_min_usd = _coerce_positive_number(e.get("min_total_usd"))
        entry_min_count = _coerce_positive_int(e.get("min_swap_count"))
        cleaned_entry = {
            "address": addr,
            "chain": chain,
            "label": e.get("label") or "",
            "source": e.get("source") or "",
        }
        if entry_min_usd is not None:
            cleaned_entry["min_total_usd"] = entry_min_usd
        else:
            # Per-entry override is the production contract; the global
            # MIN_TOTAL_USD fallback exists only as a safety net. Surface
            # the fallback loudly at load time so a forgotten override doesn't
            # silently fire on a wallet that wasn't actually calibrated.
            if log:
                print(
                    f"[watchlist] WARN: entry {addr} ({chain}, "
                    f"label={e.get('label')!r}) missing min_total_usd; "
                    f"falling back to global default ${MIN_TOTAL_USD:.0f}. "
                    f"Set a per-entry value in the watchlist file to silence "
                    f"this warning.",
                    flush=True,
                )
        if entry_min_count is not None:
            cleaned_entry["min_swap_count"] = entry_min_count
        cleaned.append(cleaned_entry)
    if log and skipped_unsupported:
        print(
            f"[watchlist] {skipped_unsupported} entry/entries skipped due to "
            f"unsupported chains; {len(cleaned)} active.",
            flush=True,
        )
    _watchlist_logged[0] = True
    return cleaned


# ---- state (cursor + buffer + dedup) -----------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"cursors": {}, "buffers": {}, "dedups": {}}
    try:
        state = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {"cursors": {}, "buffers": {}, "dedups": {}}
    state.setdefault("cursors", {})
    state.setdefault("buffers", {})
    state.setdefault("dedups", {})
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _cursor_key(chain: str, address: str) -> str:
    return f"{chain}:{address.lower()}"


def _buffer_key(chain: str, address: str, token: str) -> str:
    return f"{chain}:{address.lower()}:{token.lower()}"


def prune_buffers(state: dict, now: float) -> None:
    """Drop in-memory buffer entries older than WINDOW_SEC and dedup entries
    older than DEDUP_WINDOW_SEC."""
    new_buffers: dict[str, list] = {}
    for k, entries in state.get("buffers", {}).items():
        kept = [e for e in entries if now - e[0] < WINDOW_SEC]
        if kept:
            new_buffers[k] = kept
    state["buffers"] = new_buffers

    state["dedups"] = {
        k: ts for k, ts in state.get("dedups", {}).items()
        if now - ts < DEDUP_WINDOW_SEC
    }


# ---- Etherscan V2 / CoinGecko HTTP ------------------------------------------

def _etherscan_throttle() -> None:
    """Pace Etherscan V2 calls under the 5 req/s free-tier limit."""
    now = time.time()
    elapsed = now - _etherscan_last_call_ts[0]
    if elapsed < ETHERSCAN_MIN_INTERVAL_SEC:
        time.sleep(ETHERSCAN_MIN_INTERVAL_SEC - elapsed)
    _etherscan_last_call_ts[0] = time.time()


def _etherscan_get(chain: str, params: dict) -> dict:
    """Issue one Etherscan V2 GET, with throttle + 429 + read-timeout retry.
    Raises on fatal error (non-OK HTTP status, or timeout exhausted retries).

    Read-timeout retry was added after a sporadic Etherscan slow-response
    crashed a 30-min `pick_watchlist.py` run mid-flight (one transient
    20s timeout on the 7th of 9 router seeds → ~2K Etherscan calls of
    work lost). Same exponential backoff shape as the 429 path, so a
    transient Etherscan hiccup doesn't kill long batch jobs (picker,
    probe) or single-cycle losses (live agent's poll loop).
    """
    if not ETHERSCAN_API_KEY:
        raise RuntimeError("ETHERSCAN_API_KEY env var not set; cannot poll RPC")
    full = {**params, "chainid": ETHERSCAN_CHAIN_ID[chain], "apikey": ETHERSCAN_API_KEY}
    for attempt in range(ETHERSCAN_MAX_RETRIES + 1):
        _etherscan_throttle()
        try:
            resp = requests.get(ETHERSCAN_BASE, params=full,
                                timeout=REQUEST_TIMEOUT_SEC)
        except requests.Timeout:
            if attempt >= ETHERSCAN_MAX_RETRIES:
                raise
            wait = ETHERSCAN_BACKOFF_BASE_SEC * (2 ** attempt)
            print(
                f"[etherscan] read-timeout; sleeping {wait:.1f}s "
                f"(attempt {attempt + 1}/{ETHERSCAN_MAX_RETRIES})",
                flush=True,
            )
            time.sleep(wait)
            continue
        if resp.status_code == 429 and attempt < ETHERSCAN_MAX_RETRIES:
            wait = ETHERSCAN_BACKOFF_BASE_SEC * (2 ** attempt)
            print(
                f"[etherscan] 429 rate-limited; sleeping {wait:.1f}s "
                f"(attempt {attempt + 1}/{ETHERSCAN_MAX_RETRIES})",
                flush=True,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json() or {}
    raise RuntimeError("etherscan: exhausted retries")


def _ts_to_iso(ts: int) -> str:
    """Unix seconds → ISO-8601 with 'Z' suffix to match _block_timestamp's
    parser."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def get_block_number(chain: str) -> int:
    """Fetch the current head block via Etherscan V2's eth_blockNumber proxy.

    Etherscan returns `{"jsonrpc":"2.0","id":1,"result":"0xXXX"}` on success;
    on auth/throttle errors `result` is a human-readable string (e.g.
    'Invalid API Key (#err2)'). We raise a clear RuntimeError in that case
    so the caller doesn't get an opaque ValueError from int(...,16).
    """
    body = _etherscan_get(chain, {"module": "proxy", "action": "eth_blockNumber"})
    raw = body.get("result")
    if isinstance(raw, str) and raw.startswith("0x"):
        try:
            return int(raw, 16)
        except ValueError:
            pass
    raise RuntimeError(
        f"etherscan eth_blockNumber on {chain} returned non-hex result: "
        f"{raw!r}. Check ETHERSCAN_API_KEY."
    )


def is_known_pool(chain: str, addr: str) -> bool:
    """Return True if `addr` is a verified DEX-pool contract whose name is in
    KNOWN_POOL_NAMES.

    Used as a fallback to ROUTER_ALLOWLIST in detect_for_address: when a swap
    routes direct-to-pool (bypassing routers — common on Arbitrum), the `to`
    address of the wallet's outgoing leg is the pool itself. ROUTER_ALLOWLIST
    misses these; this lookup catches them.

    Cached in `_pool_classification_cache` for the process lifetime. Both
    hits and misses are cached:
      - Hit: contract name doesn't change after deployment, so a True verdict
        is permanent.
      - Miss: an unverified or non-pool contract isn't going to spontaneously
        become a pool, so caching False prevents repeat Etherscan calls on
        the same dud address.

    Transient errors (network, 429-after-retries) are NOT cached — we'll try
    again next time. Returns False on error to fail closed (the swap stays
    unclassified, matching the legacy behaviour).
    """
    key = (chain, addr.lower())
    if key in _pool_classification_cache:
        return _pool_classification_cache[key]
    try:
        body = _etherscan_get(chain, {
            "module": "contract",
            "action": "getsourcecode",
            "address": addr,
        })
    except (requests.RequestException, RuntimeError):
        # Don't cache transient errors. is_known_pool returning False keeps
        # the existing unclassified path; the next poll cycle will retry.
        return False

    result = body.get("result")
    name = ""
    if isinstance(result, list) and result:
        name = (result[0].get("ContractName") or "").strip()
    is_pool = name in KNOWN_POOL_NAMES
    _pool_classification_cache[key] = is_pool
    return is_pool


def fetch_asset_transfers(
    chain: str,
    address: str,
    from_block: int,
    to_block: int,
) -> list[dict]:
    """Return a flat list of transfer dicts both *from* and *to* the address.

    Two Etherscan V2 calls per address: `account.tokentx` for ERC-20 transfers
    and `account.txlist` for native ETH transfers. Each row is normalised into
    the internal transfer-dict shape the rest of the agent expects (preserves
    classify_swap_in_tx / _received_amount / _block_timestamp without churn).

    Notable wins over the prior Alchemy call:
      - `tokentx` returns transfers in *both* directions in a single call (the
        wallet may be sender or receiver), so we get half the API spend per
        cycle.
      - Each ERC-20 row carries `tokenDecimal` — `_received_amount` no longer
        has to trust an opaque decimal-adjusted `value`.
    """
    address_lc = address.lower()
    transfers: list[dict] = []

    # ERC-20 transfers (both directions in one call).
    erc20 = _etherscan_get(chain, {
        "module": "account",
        "action": "tokentx",
        "address": address,
        "startblock": str(from_block),
        "endblock": str(to_block),
        "sort": "asc",
        "page": "1",
        "offset": "1000",
    })
    for row in _etherscan_rows(erc20):
        norm = _normalise_erc20_row(row, address_lc)
        if norm:
            transfers.append(norm)

    # Native ETH transfers — wallet→router for ETH→TOKEN swaps.
    native = _etherscan_get(chain, {
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": str(from_block),
        "endblock": str(to_block),
        "sort": "asc",
        "page": "1",
        "offset": "1000",
    })
    for row in _etherscan_rows(native):
        norm = _normalise_native_row(row, address_lc)
        if norm:
            transfers.append(norm)

    return transfers


def _etherscan_rows(body: dict) -> list[dict]:
    """Etherscan returns `{"status":"1","message":"OK","result":[...]}` on
    success and `{"status":"0","message":"No transactions found","result":[]}`
    when the range is empty. Anything else (a non-list `result`) is an error
    string we surface to the caller as a no-op — but we log it loudly so an
    operator can tell "no transactions" apart from "auth failed" or "chain
    not on free tier" (Etherscan returns those at HTTP 200 too)."""
    result = body.get("result")
    if isinstance(result, list):
        return result
    msg = body.get("message") or "unknown"
    if isinstance(result, str) and "rate limit" in result.lower():
        # Rare — _etherscan_get already retries on 429 status, but Etherscan
        # also returns 200 with a rate-limit string under sustained load.
        print(f"[etherscan] rate-limit message in 200 body; skipping batch ({msg})",
              flush=True)
    elif isinstance(result, str):
        # Common surprise modes Etherscan delivers as HTTP 200 + status="0":
        #   - "Invalid API Key (#err2)" when ETHERSCAN_API_KEY is bad / missing
        #   - "Free API access is not supported for this chain..." for
        #     paywalled chains (Base on free tier today, etc.)
        #   - "Max rate limit reached, please use API Key for higher rate"
        #     when an unkeyed call sneaks through
        # Without this branch the caller silently sees "no transfers" forever
        # and the operator never finds out why the agent looks dead. Logged
        # at WARN-equivalent (single line, no traceback) so we don't spam
        # under transient errors but the failure mode is visible.
        print(
            f"[etherscan] unexpected response: status={body.get('status')!r} "
            f"message={msg!r} result={result!r}",
            flush=True,
        )
    return []


def _normalise_erc20_row(row: dict, wallet_lc: str) -> Optional[dict]:
    """Translate an Etherscan tokentx row into the internal transfer shape."""
    frm = (row.get("from") or "").lower()
    to = (row.get("to") or "").lower()
    if wallet_lc == frm:
        direction = "from"
    elif wallet_lc == to:
        direction = "to"
    else:
        return None  # shouldn't happen with `address` filter, but defensive

    try:
        # `tokenDecimal` is reliably populated by Etherscan V2 on every tokentx
        # row we've seen; the `or "18"` is defense-in-depth in case the field
        # ever goes missing. Log a one-line warning if we have to fall back —
        # silent 18-decimals on a 6-decimal token (USDC, USDT) would mis-scale
        # the notional by 10^12 and leak past the threshold gate.
        token_decimal_raw = row.get("tokenDecimal")
        if token_decimal_raw in (None, ""):
            print(
                f"[etherscan] tokentx row missing tokenDecimal "
                f"(contract={(row.get('contractAddress') or '').lower()} "
                f"hash={(row.get('hash') or '').lower()}); falling back to 18.",
                flush=True,
            )
            decimals = 18
        else:
            decimals = int(token_decimal_raw)
        raw = int(row.get("value") or "0")
        block_num = int(row.get("blockNumber") or "0")
        ts = int(row.get("timeStamp") or "0")
    except (TypeError, ValueError):
        return None
    if raw == 0 or decimals < 0 or decimals > 36:
        return None
    decimal_value = raw / (10 ** decimals)
    contract = (row.get("contractAddress") or "").lower()
    if not contract:
        return None
    return {
        "_direction": direction,
        "hash": (row.get("hash") or "").lower(),
        "blockNum": hex(block_num),
        "category": "erc20",
        "rawContract": {"address": contract, "decimal": hex(decimals)},
        "asset": row.get("tokenSymbol") or "",
        "from": frm,
        "to": to,
        "value": decimal_value,
        "metadata": {"blockTimestamp": _ts_to_iso(ts)},
    }


def _normalise_native_row(row: dict, wallet_lc: str) -> Optional[dict]:
    """Translate an Etherscan txlist row into the internal transfer shape.

    Skip zero-value rows (contract calls without native ETH transfer); they're
    not relevant for swap detection — the ERC-20 legs already cover those.
    `isError` rows (failed txs) are also skipped.
    """
    if (row.get("isError") or "0") == "1":
        return None
    try:
        raw_wei = int(row.get("value") or "0")
        block_num = int(row.get("blockNumber") or "0")
        ts = int(row.get("timeStamp") or "0")
    except (TypeError, ValueError):
        return None
    if raw_wei == 0:
        return None
    frm = (row.get("from") or "").lower()
    to = (row.get("to") or "").lower()
    if wallet_lc == frm:
        direction = "from"
    elif wallet_lc == to:
        direction = "to"
    else:
        return None
    return {
        "_direction": direction,
        "hash": (row.get("hash") or "").lower(),
        "blockNum": hex(block_num),
        "category": "external",
        "rawContract": {"address": None, "decimal": "0x12"},
        "asset": "ETH",
        "from": frm,
        "to": to,
        "value": raw_wei / 1e18,
        "metadata": {"blockTimestamp": _ts_to_iso(ts)},
    }


# _coingecko_throttle, coingecko_price, and coingecko_market_chart_range
# live in iqx/helpers/price.py. The top-of-file import re-exports them so
# existing call sites keep working unchanged.


# ---- swap classification -----------------------------------------------------

def _transfer_token_addr(transfer: dict, chain: str) -> Optional[str]:
    """Return the lowercased ERC-20 address moved by a transfer.

    For raw external (native ETH) transfers, fall back to WETH on that chain
    so they classify alongside ERC-20 WETH legs.
    """
    if transfer.get("category") == "external":
        return WETH_ADDRESS[chain]
    raw = (transfer.get("rawContract") or {}).get("address")
    return raw.lower() if isinstance(raw, str) else None


def classify_swap_in_tx(
    chain: str,
    wallet: str,
    transfers_in_tx: list[dict],
) -> Optional[dict]:
    """Inspect all transfers in a single tx; return swap metadata or None.

    A swap is: at least one transfer where wallet is sender AND at least one
    where wallet is receiver, with at least one of those legs hitting a
    non-quote-asset token. Mixed direction in the *target* token (wallet both
    sent and received the same non-quote token) is dropped as noise.

    Known v1 limitation: when a tx receives multiple non-quote tokens —
    e.g. USDC → TOKEN_A → TOKEN_B routed through a DEX aggregator in a
    single tx, or a batch buy across two tokens — only
    `received_targets[0]` is kept and the rest are silently ignored. The
    cluster still fires on TOKEN_A, so the alpha is not lost, but the
    per-tx notional in the result payload may understate what the wallet
    actually picked up. Acceptable for v1 cluster detection; revisit if
    multi-token aggregator routes become common in observed signals.
    """
    wallet_lc = wallet.lower()
    quote = QUOTE_ASSETS[chain]

    sent_targets: list[tuple[str, str]] = []      # (token_addr, asset_symbol)
    received_targets: list[tuple[str, str]] = []
    has_quote_send = False
    has_quote_receive = False
    to_addrs: set[str] = set()

    for t in transfers_in_tx:
        token_addr = _transfer_token_addr(t, chain)
        if not token_addr:
            continue
        symbol = (t.get("asset") or "").upper()
        is_quote = token_addr in quote
        direction = t.get("_direction")
        # `to` field is the immediate recipient of the transfer; for a
        # wallet-sent leg this is the router/aggregator contract that
        # consumed the wallet's tokens.
        tx_to = (t.get("to") or "").lower()
        if direction == "from" and tx_to and tx_to != wallet_lc:
            to_addrs.add(tx_to)

        if direction == "from":
            if is_quote:
                has_quote_send = True
            else:
                sent_targets.append((token_addr, symbol))
        elif direction == "to":
            if is_quote:
                has_quote_receive = True
            else:
                received_targets.append((token_addr, symbol))

    # Need at least one leg in each direction (otherwise it's a pure transfer,
    # not a swap).
    if not (sent_targets or has_quote_send):
        return None
    if not (received_targets or has_quote_receive):
        return None

    # Determine target. Buy: only received non-quote legs; spent quote.
    # Sell: only sent non-quote legs; received quote. Mixed → drop.
    if received_targets and not sent_targets:
        target_addr, target_symbol = received_targets[0]
        direction = "up"
    elif sent_targets and not received_targets:
        target_addr, target_symbol = sent_targets[0]
        direction = "down"
    else:
        return None  # mixed-direction or non-quote-vs-non-quote rotation

    # v1 fires on buys only.
    if direction != "up":
        return None

    return {
        "tx_hash": (transfers_in_tx[0].get("hash") or "").lower(),
        "block_num": int(transfers_in_tx[0].get("blockNum", "0x0"), 16),
        "ts": _block_timestamp(transfers_in_tx),
        "target_addr": target_addr,
        "target_symbol": target_symbol,
        "to_addrs": to_addrs,
        "direction": direction,
    }


def _block_timestamp(transfers_in_tx: list[dict]) -> float:
    """Best-effort timestamp from the first transfer's metadata."""
    md = (transfers_in_tx[0] or {}).get("metadata") or {}
    ts = md.get("blockTimestamp")
    if isinstance(ts, str):
        # ISO-8601 → unix seconds. Strip 'Z' and parse as UTC.
        # calendar.timegm treats struct_time as UTC; time.mktime would treat
        # it as local and silently shift by the host's tz offset.
        try:
            return calendar.timegm(time.strptime(ts.replace("Z", "+0000"),
                                                 "%Y-%m-%dT%H:%M:%S.%f%z"))
        except (ValueError, TypeError):
            try:
                return calendar.timegm(time.strptime(ts.replace("Z", "+0000"),
                                                     "%Y-%m-%dT%H:%M:%S%z"))
            except (ValueError, TypeError):
                pass
    return time.time()


# ---- detection orchestration -------------------------------------------------

def detect_for_address(
    *,
    chain: str,
    address: str,
    state: dict,
    price_cache: dict,
    threshold_usd: float,
    min_swap_count: int = MIN_SWAP_COUNT,
    verbose: bool = False,
) -> list[dict]:
    """Poll one watchlist address; return any cluster hits ready to file.

    Side effects (mutating `state`):
      - `cursors[chain:addr]`        advances to the latest scanned block
      - `buffers[chain:addr:token]`  appended with new (ts, usd, router) tuples
      - `dedups[chain:addr:token]`   set when a hit is emitted

    Known caveat: the cursor advances to `head_block` *before* the caller
    has POSTed any signal hits. If the agent process crashes between
    detection and `file_signal`, the cursor has moved past the transfers
    that produced the hit and the signal is permanently lost. Hardening:
    move the cursor commit into a per-hit callback that runs after a
    successful POST. Tracked for a future hardening pass — not blocking
    v1.
    """
    cur_key = _cursor_key(chain, address)
    head_block = get_block_number(chain)
    cursor = state["cursors"].get(cur_key)
    if cursor is None:
        from_block = max(head_block - FIRST_SIGHT_LOOKBACK_BLOCKS, 0)
    else:
        from_block = int(cursor) + 1

    if from_block > head_block:
        return []

    transfers = fetch_asset_transfers(chain, address, from_block, head_block)
    state["cursors"][cur_key] = head_block

    if not transfers:
        # Quiet path — no activity for this address in the scanned range.
        # Default behavior stays silent so steady-state stdout isn't flooded
        # during typical "watchlist asleep" cycles. Under --verbose we emit a
        # one-line diagnostic so the operator can distinguish "wallet asleep"
        # from "scanner skipped a range" / "cursor stuck at head" — the same
        # ambiguity that produced the all-zero overnight log.
        if verbose:
            print(
                f"[poll] {chain} {address[:10]}…: blocks={head_block - from_block + 1} "
                f"transfers=0 (cursor={cursor})",
                flush=True,
            )
        return []

    # Group by tx hash.
    by_tx: dict[str, list[dict]] = {}
    for t in transfers:
        h = (t.get("hash") or "").lower()
        if h:
            by_tx.setdefault(h, []).append(t)

    # Per-address diagnostic counters. Printed below as a single line so the
    # operator can tell apart "wallets are quiet" from "swap pattern never
    # matches" from "router allowlist is missing this DEX" from "swaps detected
    # but all sells (v1 buy-only)" from "CoinGecko has no price".
    #
    # An earlier APE→USDC sell investigation showed why the
    # `swap_pattern` counter must increment based on tx *shape*, not on the
    # classifier's verdict: if you only count buys, sells (which classify as
    # None due to the v1 buy-only filter) silently disappear and the operator
    # sees `swap-pattern=0` when in reality the wallet is actively swapping —
    # just selling. Counting the shape first and accounting for the buy-only
    # filter via `sell_dropped` makes the diagnostic line honest.
    swap_pattern = 0    # txs where wallet appears on BOTH from and to sides (shape match)
    unclassified = 0    # swap-shape buys whose `to` is neither a router in
                        # ROUTER_ALLOWLIST nor a verified pool whose ContractName
                        # is in KNOWN_POOL_NAMES (e.g. unverified pool contract,
                        # or a newer DEX we haven't catalogued yet)
    sell_dropped = 0    # swap-shape txs that classify as sells (or quote↔non-quote
                        # rotations) — v1 fires on buys only
    price_skip = 0      # passed allowlist but CoinGecko had no price / zero amount
    buffer_add = 0      # passed all gates and was appended to the sliding buffer

    hits: list[dict] = []
    now = time.time()

    for tx_hash, leg_list in by_tx.items():
        # Shape pre-check: did the wallet appear on BOTH from and to sides via
        # any leg in this tx? Native ETH out + ERC-20 in (or any other mix)
        # both qualify; we trust the per-leg `_direction` set by the
        # normaliser. Counting shape first means a sell still shows up as
        # swap-pattern=1 in the diagnostic even though the classifier will
        # drop it under the v1 buy-only filter — see comment above.
        has_send = any(l.get("_direction") == "from" for l in leg_list)
        has_recv = any(l.get("_direction") == "to"   for l in leg_list)
        is_swap_shape = has_send and has_recv
        if is_swap_shape:
            swap_pattern += 1

        meta = classify_swap_in_tx(chain, address, leg_list)
        if meta is None:
            # Three reasons classify_swap_in_tx returns None: (a) tx isn't a
            # swap shape (caught above and not counted), (b) tx is a sell
            # (direction=="down"), (c) tx is a mixed quote↔non-quote rotation.
            # When `is_swap_shape` is True, one of (b)/(c) applied — we group
            # them under sell_dropped because (c) is rare in practice (a
            # wallet that received non-quote AND sent non-quote in one tx is
            # a routing oddity, not the common case) and the operator's
            # actionable next step is the same: "v1 ignores this; expect
            # nothing to fire from it."
            if is_swap_shape:
                sell_dropped += 1
            continue

        # Counterparty gate. Two acceptance paths:
        #   (1) `to` is a router we know (ROUTER_ALLOWLIST — vanilla retail
        #       flow through Uniswap Universal Router, 1inch, Camelot V3,
        #       etc.)
        #   (2) `to` is a known DEX pool implementation (KNOWN_POOL_NAMES —
        #       sophisticated retail bypassing routers and calling pools
        #       directly for gas savings + MEV protection; common on
        #       Arbitrum). Resolved via Etherscan getsourcecode, cached.
        #
        # We accept the union: a tx that hits either a known router or a
        # known pool counts as a legitimate swap. Anything else still falls
        # to the [unclassified-swap] path and increments `unclassified`.
        allowed_routers = ROUTER_ALLOWLIST[chain]
        legitimate = meta["to_addrs"] & allowed_routers
        if not legitimate:
            # Fallback: are any of the `to` addresses verified DEX pools?
            # is_known_pool is cached, so this is at most one Etherscan call
            # per *new* unknown counterparty seen (steady-state ~zero).
            legitimate = {a for a in meta["to_addrs"] if is_known_pool(chain, a)}
        if not legitimate:
            unclassified += 1
            for unknown in meta["to_addrs"]:
                print(
                    f"[unclassified-swap] chain={chain} hash={tx_hash} "
                    f"wallet={address} to={unknown}",
                    flush=True,
                )
            continue

        # USD valuation. Drop if CoinGecko has no price for the target.
        token_price = coingecko_price(chain, meta["target_addr"], price_cache)
        if token_price is None:
            price_skip += 1
            continue
        # Notional: sum the *received* leg's decimal-adjusted token amount
        # (Etherscan V2 returns `tokenDecimal` per row; _normalise_erc20_row
        # divides raw value by 10^decimals before we get here) and multiply
        # by CoinGecko's USD price.
        target_amount = _received_amount(leg_list, address, meta["target_addr"], chain)
        if target_amount is None or target_amount <= 0:
            price_skip += 1
            continue
        usd_amount = target_amount * token_price
        router = next(iter(legitimate))

        # Append to per-(chain, wallet, token) buffer.
        buf_key = _buffer_key(chain, address, meta["target_addr"])
        buf = state["buffers"].setdefault(buf_key, [])
        buf.append([meta["ts"], usd_amount, router])
        buffer_add += 1

        # Prune stale entries (older than WINDOW_SEC).
        buf[:] = [e for e in buf if now - e[0] < WINDOW_SEC]

        # Threshold check.
        swap_count = len(buf)
        total_usd = sum(e[1] for e in buf)
        distinct_routers = len({e[2] for e in buf})

        if (swap_count >= min_swap_count
                and total_usd >= threshold_usd
                and distinct_routers >= MIN_DISTINCT_ROUTERS):
            # Dedup.
            last_signal = state["dedups"].get(buf_key, 0)
            if now - last_signal < DEDUP_WINDOW_SEC:
                continue

            eth_price = coingecko_price(chain, WETH_ADDRESS[chain], price_cache)
            if eth_price is None:
                # Can't compute baseline_return at verify time without this.
                continue

            evidence = sorted({
                EXPLORER_TX_PREFIX[chain] + e_hash
                for e_hash in (
                    [tx_hash]  # current tx + others within window
                    + _evidence_tx_hashes_for_buffer(by_tx, buf_key, address, chain)
                )
            })

            hit = {
                "chain": chain,
                "wallet": address,
                "token_address": meta["target_addr"],
                "token_symbol": meta["target_symbol"],
                "swap_count": swap_count,
                "distinct_routers": distinct_routers,
                "total_usd": round(total_usd, 2),
                "direction": meta["direction"],
                "price_at_signal_usd": round(token_price, 8),
                "eth_price_at_signal_usd": round(eth_price, 4),
                "block_at_signal": head_block,
                # price_source is reserved for a future TWAP migration. v1
                # uses CoinGecko at both signal time and verify time.
                "price_source": None,
                "evidence_urls": evidence[:5],
                "observed_at": now,
            }
            hits.append(hit)
            state["dedups"][buf_key] = now
            # Reset the buffer so we don't immediately re-fire on the next swap.
            state["buffers"][buf_key] = []

    # One-line per-address summary so the operator can see why nothing fired.
    # Only printed when there were transfers — quiet polls stay quiet (the
    # run_once-level "[poll] N cluster(s) ready to file" line is the heartbeat).
    print(
        f"[poll] {chain} {address[:10]}…: blocks={head_block - from_block + 1} "
        f"transfers={len(transfers)} txs={len(by_tx)} "
        f"swap-pattern={swap_pattern} unclassified={unclassified} "
        f"sell-dropped={sell_dropped} price-skip={price_skip} "
        f"buffer-add={buffer_add} hits={len(hits)} "
        f"gate=usd>=${threshold_usd:.0f}/n>={min_swap_count}",
        flush=True,
    )

    return hits


def _received_amount(
    leg_list: list[dict],
    wallet: str,
    target_addr: str,
    chain: str,
) -> Optional[float]:
    """Sum the wallet-receive amounts of the target token in this tx.

    Under the Etherscan V2 data source each `tokentx` row carries an
    authoritative `tokenDecimal` field, and `_normalise_erc20_row` divides
    raw uint256 `value` by `10**decimals` before populating the transfer
    dict. The notional this function sums is therefore correctly
    decimal-adjusted for tokens with any decimals (6 like USDC, 8 like
    WBTC, 18 like ERC-20 default, or anything else).
    """
    wallet_lc = wallet.lower()
    target_lc = target_addr.lower()
    total = 0.0
    saw_any = False
    for t in leg_list:
        if t.get("_direction") != "to":
            continue
        if (t.get("to") or "").lower() != wallet_lc:
            continue
        token_addr = _transfer_token_addr(t, chain)
        if token_addr != target_lc:
            continue
        v = t.get("value")
        if isinstance(v, (int, float)):
            total += float(v)
            saw_any = True
    return total if saw_any else None


def _evidence_tx_hashes_for_buffer(
    by_tx: dict[str, list[dict]],
    buf_key: str,
    wallet: str,
    chain: str,
) -> list[str]:
    """Return up to 4 additional tx hashes (this poll only) for the same buf_key."""
    chain_in_key, addr_in_key, token_in_key = buf_key.split(":")
    if chain_in_key != chain or addr_in_key != wallet.lower():
        return []
    out: list[str] = []
    for h, leg_list in by_tx.items():
        if any(_transfer_token_addr(t, chain) == token_in_key
               and t.get("_direction") == "to"
               and (t.get("to") or "").lower() == wallet.lower()
               for t in leg_list):
            out.append(h)
        if len(out) >= 4:
            break
    return out


# ---- IQX flow — Boss-only role ----------------------------------------------

def file_signal(signal: dict) -> None:
    """Post a Boss task carrying the cluster as a question.

    Boss-only role: smart_money's file_signal stops at POST /tasks.
    A separate Judge Worker (worker_judge) claims the task and submits
    its prediction; the verifier (worker_prediction_accuracy_4h) grades
    the Worker's prediction accuracy.

    The structured cluster fields (wallet, token_address, prices, etc.)
    are JSON-encoded into `task.signal_data` — the verifier reads them to
    fetch the oracle and grade against price_at_signal_usd. The
    `description` is human-readable and frames the cluster as a *question*
    so external Workers grading the same stream see what they're being
    asked, not a verdict.

    No api_key needed: POST /tasks is unauthenticated (per main.py),
    and Boss-mode smart_money never claims or submits.
    """
    now = signal["observed_at"]

    token_label = signal['token_symbol'] or signal['token_address'][:8]
    description = (
        f"wallet {signal['wallet']} clustered {signal['swap_count']} buys on "
        f"{token_label} ({signal['token_address']}) on {signal['chain']} in "
        f"window {WINDOW_SEC}s totaling ${signal['total_usd']:,.0f}; "
        f"is this real alpha?"
    )

    resp = requests.post(
        f"{BASE_URL}/tasks",
        json={
            "description": description,
            "budget": 1.0,
            "min_elo": 1000,
            "publisher_id": AGENT_ID,
            "task_type": TASK_TYPE,
            "verification_method": VERIFICATION_METHOD,
            "verification_mode": VERIFICATION_MODE,
            "signal_type": SIGNAL_TYPE,
            "evidence_urls": json.dumps(signal["evidence_urls"]),
            "verification_deadline": now + VERIFICATION_HORIZON_SEC,
            "signal_data": json.dumps(signal),
        },
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    task_id = resp.json()["id"]
    print(
        f"  ✅ posted Boss task {task_id} for "
        f"{signal['wallet'][:6]}…/{signal['token_symbol']} "
        f"({signal['swap_count']} swaps, ${signal['total_usd']:,.0f}); "
        f"awaiting Judge claim",
        flush=True,
    )


# ---- orchestration -----------------------------------------------------------

def run_once(
    *,
    threshold_usd: float,
    dry_run: bool,
    verbose: bool = False,
) -> int:
    """One detection pass. Returns the count of clusters posted as Boss tasks.

    Boss-only role: file_signal POSTs to /tasks (unauthenticated on the
    central node), so this function no longer needs an api_key.
    Registration still happens once at startup for hygiene (publisher_id
    refers to a real agent record).
    """
    state = load_state()
    now = time.time()
    prune_buffers(state, now)

    watchlist = load_watchlist()
    if not watchlist:
        print("[poll] watchlist is empty; nothing to do", flush=True)
        return 0

    price_cache: dict = {}
    all_hits: list[dict] = []

    for entry in watchlist:
        chain = entry["chain"]
        address = entry["address"]
        # Per-entry override falls back to the CLI/global threshold when the
        # watchlist entry doesn't pin its own. See load_watchlist() for the
        # parse and the per-entry threshold rationale (wallets vary by ~2
        # orders of magnitude in typical buy size — one global floor doesn't
        # fit).
        entry_threshold = entry.get("min_total_usd", threshold_usd)
        entry_min_count = entry.get("min_swap_count", MIN_SWAP_COUNT)
        try:
            hits = detect_for_address(
                chain=chain,
                address=address,
                state=state,
                price_cache=price_cache,
                threshold_usd=entry_threshold,
                min_swap_count=entry_min_count,
                verbose=verbose,
            )
        except RuntimeError as e:
            # Per-entry config error (e.g. chain not in SUPPORTED_CHAINS).
            # `continue` to the next watchlist entry rather than exit
            # run_once — under --loop, the prior `return 0` would also kill
            # the next iteration's pass even though only one wallet was
            # broken. Same shape as the RequestException branch below.
            print(f"[poll] {chain}:{address[:10]}… config error: {e}", flush=True)
            continue
        except requests.RequestException as e:
            print(
                f"[poll] {chain}:{address[:10]}… RPC error: "
                f"{_redact_secrets(str(e))}",
                flush=True,
            )
            continue

        for h in hits:
            print(
                f"  • cluster {chain} {address[:8]}… → "
                f"{h['token_symbol']} (${h['total_usd']:,.0f}, "
                f"{h['swap_count']} swaps, {h['distinct_routers']} routers)",
                flush=True,
            )
        all_hits.extend(hits)

        # Stagger across watchlist to stay under Etherscan free-tier 5 req/s.
        time.sleep(ADDRESS_POLL_DELAY_SEC)

    print(f"[poll] {len(all_hits)} cluster(s) ready to file", flush=True)

    for hit in all_hits:
        if dry_run:
            print(f"    (dry-run) would file {hit['token_symbol']} "
                  f"on {hit['chain']}", flush=True)
            continue
        try:
            file_signal(hit)
        except requests.HTTPError as e:
            status = e.response.status_code
            text = e.response.text
            print(f"  ❌ failed to file: {status} {text}", flush=True)
        except requests.RequestException as e:
            print(f"  ❌ network error filing cluster: {e}", flush=True)

    save_state(state)
    return len(all_hits)


# ---- CLI ---------------------------------------------------------------------

def reset_cursor(address: str) -> None:
    state = load_state()
    addr_lc = address.lower()
    removed = [k for k in list(state["cursors"]) if k.endswith(":" + addr_lc)]
    for k in removed:
        del state["cursors"][k]
    save_state(state)
    if removed:
        print(f"[reset-cursor] cleared {len(removed)} entry/entries: {removed}",
              flush=True)
    else:
        print(f"[reset-cursor] no cursor found for {address}", flush=True)


class SmartMoneyBoss:
    """Boss-only smart-money agent — posts cluster signals to the IQX dispatcher.

    Thin wrapper around the module-level functions in this file. Holds a
    ``SmartMoneyConfig`` and exposes ``.ensure_registered()``,
    ``.run_once()``, ``.loop()``, ``.reset_cursor()`` so SDK consumers can
    instantiate the Boss without re-implementing the CLI plumbing.

    Module-level mutable state (the Etherscan throttle list
    ``_etherscan_last_call_ts``, the pool-classification cache
    ``_pool_classification_cache``, the watchlist-load suppression flag
    ``_watchlist_logged``) stays at module scope today. Migrating these
    to instance attributes — and threading ``config`` through the helper
    signatures — is tracked as a future SDK-ergonomics improvement.
    """

    def __init__(self, config: SmartMoneyConfig):
        self.config = config

    def ensure_registered(self) -> str:
        """Register with the dispatcher if needed; return the api_key."""
        return ensure_registered()

    def run_once(self) -> int:
        """One detection pass over the watchlist. Returns #signals fired."""
        return run_once(
            threshold_usd=self.config.threshold_usd,
            dry_run=self.config.dry_run,
            verbose=self.config.verbose,
        )

    def loop(self) -> None:
        """Continuous poll loop — same cadence as the pre-Step-6 ``--loop``."""
        print(
            f"[poll] starting continuous loop "
            f"(threshold-usd=${self.config.threshold_usd:,.0f}, "
            f"interval={POLL_INTERVAL_SEC}s)",
            flush=True,
        )
        while True:
            try:
                self.run_once()
            except requests.RequestException as e:
                print(f"[poll] transient error: {_redact_secrets(str(e))}",
                      flush=True)
            time.sleep(POLL_INTERVAL_SEC)

    def reset_cursor(self, address: str) -> None:
        """Wipe the persisted fromBlock cursor for one address."""
        reset_cursor(address)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smart-money cluster monitoring agent")
    parser.add_argument("--threshold-usd", type=float, default=MIN_TOTAL_USD,
                        help=f"Min cluster notional in USD (default {MIN_TOTAL_USD})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect and print signals but do not register or POST")
    parser.add_argument("--loop", action="store_true",
                        help=f"Poll forever every {POLL_INTERVAL_SEC}s")
    parser.add_argument("--reset-cursor", metavar="ADDRESS",
                        help="Wipe the persisted fromBlock cursor for one address "
                             "(use after re-adding a removed watchlist entry)")
    parser.add_argument("--verbose", action="store_true",
                        help="Emit one diagnostic line per address per poll, "
                             "even when no transfers are returned — distinguishes "
                             "'wallet asleep' from 'scanner skipped a range'. "
                             "Off by default to keep --loop stdout clean.")
    args = parser.parse_args()

    config = SmartMoneyConfig.from_env_and_args(args)
    boss = SmartMoneyBoss(config)

    if args.reset_cursor:
        boss.reset_cursor(args.reset_cursor)
        return 0

    if not ETHERSCAN_API_KEY:
        print("[error] ETHERSCAN_API_KEY env var is required. Get a free key "
              "at https://etherscan.io/myapikey. V1 of this agent runs on "
              "Arbitrum only on the free tier; Base and other L2s require a "
              "paid Etherscan plan — see SUPPORTED_CHAINS in this file.",
              flush=True)
        return 2

    # Register the publisher identity for hygiene (publisher_id refers to
    # a real agent record), but discard the api_key — Boss-mode smart_money
    # posts to the unauthenticated /tasks endpoint and never claims or
    # submits, so no header auth is needed.
    if not args.dry_run:
        boss.ensure_registered()

    if args.loop:
        boss.loop()
    else:
        boss.run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
