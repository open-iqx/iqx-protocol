"""Proof-of-work + per-agent rate-limit primitives for IQX registration.

Two independent mechanisms ship behind two independent feature flags,
**both default OFF** so existing deployments see no behavior change:

  ``IQX_REQUIRE_POW``        — gate ``POST /agents/register`` on a solved
                               hashcash-style challenge (default 0 = off).
  ``IQX_RATE_LIMIT_PER_HOUR`` — cap ``POST /tasks`` and ``POST /tasks/{id}/claim``
                               per agent per hour (default 0 = off, meaning
                               unlimited).

Why both: opening ``POST /agents/register`` publicly without a friction gate
invites botnet spam (the registration endpoint creates an Agent row +
ELO=1200 baseline on every call). PoW puts a 10–30s CPU cost on each fresh
identity without introducing staking, tokens, or KYC — which keeps the
"public good, no financialization" constraint intact. Per-agent rate limits
handle the orthogonal post-registration spam case (a registered agent
flooding ``/tasks`` or ``/tasks/{id}/claim``).

This module exposes:

- ``generate_challenge(difficulty)`` — server-side: mint a fresh prefix.
- ``count_leading_zero_bits(digest)`` — bit counter used by ``verify_pow``.
- ``verify_pow(prefix, nonce, difficulty)`` — server-side: True if
  ``sha256(prefix || nonce)`` has ≥ ``difficulty`` leading zero bits.
- ``solve_challenge(prefix, difficulty, max_attempts)`` — client-side: find a
  nonce. Returns ``(nonce, attempts)`` or raises ``RuntimeError`` after
  ``max_attempts``.
- ``TokenBucket`` — in-memory per-key rate limiter used by main.py's middleware.

All functions are pure (no DB, no HTTP, no env reads) so they unit-test in
microseconds. The DB-backed challenge lifecycle (``RegistrationChallenge``
table, TTL, replay protection) lives in main.py — that's where the FastAPI
session is available.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


# ---- PoW (registration gate) -------------------------------------------------

# Challenge TTL: how long a minted challenge stays valid before the server
# refuses to consume it. 5 minutes is plenty for a 20-bit puzzle (10–30s on a
# laptop CPU); short enough that a stolen/leaked challenge can't sit around.
CHALLENGE_TTL_SEC = 300

# Default difficulty (leading zero bits). 20 bits ≈ 1M expected hashes ≈
# ~10–30s on a 2020s laptop CPU. Tunable via ``IQX_POW_DIFFICULTY`` env var
# read by main.py — this constant is just the fallback.
DEFAULT_DIFFICULTY = 20

# Solver upper bound. At difficulty=20 the expected work is 2^20 ≈ 1M hashes;
# 10× that should virtually never fall short. Caller surfaces a clear error
# if it does (rather than spinning forever).
DEFAULT_MAX_ATTEMPTS = 10_000_000


def generate_challenge_prefix() -> str:
    """Return a fresh random hex prefix for a new challenge.

    Server-side: the caller persists ``(challenge_id, prefix, difficulty,
    created_at)`` and returns ``(challenge_id, prefix, difficulty)`` to the
    client.

    Prefix is 32 hex chars (128 bits of randomness) — overkill from a collision
    standpoint, sized so a single prefix is statistically unique across all
    historical challenges. The cost is a few bytes per row; we have it.
    """
    return secrets.token_hex(16)


def count_leading_zero_bits(digest: bytes) -> int:
    """Count leading zero bits in a byte string.

    Helper for ``verify_pow``. Pulled out for unit-testability — boundary
    cases (zero-prefix bytes, partial-byte zeros) are easy to misread.
    """
    n = 0
    for byte in digest:
        if byte == 0:
            n += 8
            continue
        # Count leading zeros within this byte (1..7).
        mask = 0x80
        while mask and not (byte & mask):
            n += 1
            mask >>= 1
        break
    return n


def verify_pow(prefix: str, nonce: str, difficulty: int) -> bool:
    """True iff ``sha256(prefix||nonce)`` has ≥ ``difficulty`` leading zero bits.

    Pure function, no I/O. Inputs are strings to keep the wire format simple
    (clients send hex/ascii nonces; server stores hex prefix). UTF-8 encoded
    before hashing — caller is responsible for sending bytes-safe strings.

    ``difficulty <= 0`` always passes — that's the "off" semantics so a server
    misconfig (difficulty=0) doesn't accidentally lock everyone out.
    """
    if difficulty <= 0:
        return True
    payload = (prefix + nonce).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return count_leading_zero_bits(digest) >= difficulty


def solve_challenge(
    prefix: str,
    difficulty: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Tuple[str, int]:
    """Client-side: find a nonce such that ``verify_pow(prefix, nonce, d)`` is True.

    Returns ``(nonce, attempts)``. Raises ``RuntimeError`` if no solution is
    found within ``max_attempts`` (caller can retry with a higher bound, or
    request a lower-difficulty challenge if the server allows).

    Single-threaded by design — the agent's register() path is one-shot at
    startup and the puzzle is small enough that threading overhead would
    dominate. If a future operator runs at difficulty ≫ 24, revisit.
    """
    if difficulty <= 0:
        # No-op shortcut. Lets clients call solve_challenge unconditionally
        # without checking the difficulty themselves — useful when the server
        # is configured with PoW off.
        return ("", 0)
    for attempts in range(1, max_attempts + 1):
        # token_hex(8) gives 64 bits of entropy per attempt — random search is
        # equivalent to counter search for sha256 at these difficulties, and
        # random keeps us out of any pathological counter-aligned pattern.
        nonce = secrets.token_hex(8)
        if verify_pow(prefix, nonce, difficulty):
            return (nonce, attempts)
    raise RuntimeError(
        f"PoW solver exhausted {max_attempts} attempts at difficulty={difficulty}"
    )


# ---- Rate limit (per-agent token bucket) -------------------------------------

@dataclass
class _Bucket:
    """Internal per-key state for the token-bucket rate limiter."""
    tokens: float
    last_refill_ts: float


@dataclass
class TokenBucket:
    """In-memory per-key token-bucket rate limiter.

    Used by main.py's middleware to cap ``POST /tasks`` and
    ``POST /tasks/{id}/claim`` per (agent_id, endpoint). One instance per
    endpoint, keyed on whichever identity that endpoint exposes
    (``publisher_id`` for /tasks, authenticated ``worker_id`` for /claim).

    Process-local — restarting the dispatcher resets every bucket. That's a
    feature, not a bug: a misbehaving agent gets one free request after every
    restart, which is fine because (a) restarts are rare and (b) the worst
    case is one extra request per agent per restart.

    Why a class with mutable state rather than a free function: the middleware
    needs to keep the bucket map alive across requests. Wrapping it in a class
    makes the state ownership explicit and unit-testable (instantiate with a
    fake clock).

    Caveat on ``/tasks``: that endpoint is unauthenticated, so the bucket key
    is ``publisher_id`` from the request body — spoofable. Acceptable for the
    flag-off observation window; when the flag flips on we either tighten
    ``/tasks`` to require an api-key, or rate-limit ``/tasks`` by source IP
    in addition to ``publisher_id``. Documented here so the spoof surface is
    explicit, not hidden.
    """

    capacity: float
    refill_per_sec: float
    _buckets: Dict[str, _Bucket] = field(default_factory=dict)

    @classmethod
    def per_hour(cls, limit: int) -> "TokenBucket":
        """Build a bucket that allows ``limit`` requests per hour at steady state.

        ``limit <= 0`` returns a no-op bucket (``allow`` always True) so callers
        don't have to special-case the off-by-flag path.
        """
        if limit <= 0:
            # capacity=0 + refill=0 would trap callers; use sentinel-large
            # capacity + zero refill is wrong too. Cleanest: a small flag.
            return _OFF_BUCKET
        return cls(capacity=float(limit), refill_per_sec=limit / 3600.0)

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        """Try to consume one token for ``key``. Returns True if allowed."""
        if now is None:
            now = time.time()
        bucket = self._buckets.get(key)
        if bucket is None:
            # First request for this key — initialize to a full bucket.
            self._buckets[key] = _Bucket(tokens=self.capacity - 1.0, last_refill_ts=now)
            return True
        # Refill: tokens added since last touch, capped at capacity.
        elapsed = max(0.0, now - bucket.last_refill_ts)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_sec)
        bucket.last_refill_ts = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False

    def retry_after_sec(self, key: str, now: Optional[float] = None) -> float:
        """How many seconds until ``key`` gets its next token. Caller uses
        this for the HTTP ``Retry-After`` header on 429 responses."""
        if now is None:
            now = time.time()
        bucket = self._buckets.get(key)
        if bucket is None or self.refill_per_sec <= 0:
            return 0.0
        if bucket.tokens >= 1.0:
            return 0.0
        deficit = 1.0 - bucket.tokens
        return deficit / self.refill_per_sec


class _OffBucket:
    """No-op bucket. Returned by ``TokenBucket.per_hour(0)`` so the off-by-flag
    path doesn't require callers to branch on ``None``."""
    def allow(self, key: str, now: Optional[float] = None) -> bool:
        return True

    def retry_after_sec(self, key: str, now: Optional[float] = None) -> float:
        return 0.0


_OFF_BUCKET = _OffBucket()  # type: ignore[assignment]
