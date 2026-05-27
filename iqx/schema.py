from __future__ import annotations

from enum import Enum
from typing import Optional
import time

from pydantic import BaseModel
from sqlmodel import Field, SQLModel


class TaskStatus(str, Enum):
    # Explicit five-state lifecycle (open → claimed → submitted → verified →
    # published, with FAILED branching off submitted) — legible in the DB
    # without cross-referencing the boolean columns.
    OPEN = "open"
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    PUBLISHED = "published"
    FAILED = "failed"


class Agent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    elo: int = 1200
    api_key: str


class RegistrationChallenge(SQLModel, table=True):
    """PoW challenge minted by ``POST /agents/register/challenge`` and consumed
    by ``POST /agents/register`` when ``IQX_REQUIRE_POW=1``.

    Additive table — empty on existing DBs, populated only when the PoW
    flag is on. The flag is OFF by default.

    Lifecycle:
      1. Client calls ``POST /agents/register/challenge`` (no auth).
      2. Server generates a fresh ``prefix``, persists this row with
         ``consumed_at=None``, returns ``{challenge_id, prefix, difficulty}``.
      3. Client solves ``sha256(prefix||nonce)`` to ``difficulty`` leading
         zero bits.
      4. Client calls ``POST /agents/register`` with ``challenge_id`` + ``nonce``.
      5. Server looks up the row, verifies it's unconsumed, within TTL
         (``pow.CHALLENGE_TTL_SEC`` = 5 min), and the PoW is correct.
      6. On success, server stamps ``consumed_at=now()`` and proceeds with
         registration. On failure, returns 401/403/410 with a clear reason.

    Replay protection: ``consumed_at IS NOT NULL`` means the challenge has been
    used; a second register call with the same ``(challenge_id, nonce)`` is
    rejected. GC is opportunistic — the dispatcher prunes rows older than
    ``CHALLENGE_TTL_SEC`` on each ``/challenge`` call. Acceptable for a
    low-volume registration endpoint; revisit if challenge issue rate ever
    exceeds ~1/sec.
    """
    id: str = Field(primary_key=True)
    prefix: str
    difficulty: int
    created_at: float = Field(default_factory=time.time)
    consumed_at: Optional[float] = None


class Task(SQLModel, table=True):
    id: str = Field(primary_key=True)
    description: str
    budget: float
    min_elo: int = 1000
    status: TaskStatus = Field(default=TaskStatus.OPEN)
    worker_id: Optional[str] = None
    result: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    # Protocol-shape fields. publisher_id distinguishes the agent that posted
    # the task from worker_id (claimer); they may be equal (self-publish) or
    # distinct (the role-split architecture). task_type is the category
    # label; verification_method is the registry key the verifier dispatches
    # on; verification_mode is automatic|manual.
    publisher_id: Optional[str] = None
    task_type: Optional[str] = None
    verification_method: Optional[str] = None
    verification_mode: Optional[str] = None  # "automatic" | "manual"
    # Signal fields — populated when a monitoring agent files a signal.
    signal_type: Optional[str] = None
    evidence_urls: Optional[str] = None  # JSON-encoded list of URLs
    verification_deadline: Optional[float] = None  # unix timestamp
    # Verification fields — populated retroactively by the verifier poller.
    verified: Optional[bool] = None  # None = not yet verified
    verified_at: Optional[float] = None  # unix timestamp of verdict
    verification_notes: Optional[str] = None  # human-readable reason
    # ELO delta actually applied to the worker on submit; needed for exact
    # clawback at verify time since calculate_elo_change is dynamic (K=32).
    elo_delta: Optional[int] = None
    # Publish fields — populated by the publisher after a verified signal is
    # posted. None/False = not yet posted; True = post live.
    published: Optional[bool] = None
    published_at: Optional[float] = None
    tweet_url: Optional[str] = None
    # Reference-asset return over the same verification window (e.g. ETH
    # return over the price_move_4h horizon). Populated by the verifier when
    # it knows the baseline; used later for excess-return analysis. Not
    # consulted in the verdict itself today.
    baseline_return: Optional[float] = None
    # Boss task spec — JSON-encoded structured payload the publishing agent
    # attached to the question (e.g. wallet, token_address,
    # price_at_signal_usd, direction). Distinct from `result` (Worker's
    # submission) and `description` (human-readable narrative). Read by
    # `worker_prediction_accuracy_4h` to fetch the original price context
    # independent of what the Worker submitted. Optional so old rows and
    # tasks using single-role verifiers (price_move_4h, echo) stay valid.
    signal_data: Optional[str] = None


# Request / response DTOs (not persisted).
class TaskCreate(BaseModel):
    description: str
    budget: float
    min_elo: int = 1000
    signal_type: Optional[str] = None
    evidence_urls: Optional[str] = None
    verification_deadline: Optional[float] = None
    # Pluggable protocol fields. publisher_id is optional on the wire — the
    # dispatcher defaults a missing value to the literal "system" so every
    # row has a non-null publisher and downstream queries never have to
    # special-case NULL. worker_id can't serve as the fallback at create-time
    # because /tasks is unauthenticated and the row hasn't been claimed yet.
    publisher_id: Optional[str] = None
    task_type: Optional[str] = None
    verification_method: Optional[str] = None
    verification_mode: Optional[str] = None
    # JSON-encoded Boss task spec. See Task.signal_data.
    signal_data: Optional[str] = None


class AgentRegister(BaseModel):
    id: str
    name: str
    # Optional PoW proof. Required when ``IQX_REQUIRE_POW=1`` on the server;
    # ignored when the flag is off (default). Clients can always include them
    # — backward compatible. ``challenge_id`` is the UUID returned by
    # ``POST /agents/register/challenge``; ``nonce`` is the solver output.
    challenge_id: Optional[str] = None
    nonce: Optional[str] = None


class RegistrationChallengeResponse(BaseModel):
    """Returned by ``POST /agents/register/challenge``."""
    challenge_id: str
    prefix: str
    difficulty: int
    expires_at: float


class TaskClaim(BaseModel):
    worker_id: str


class TaskSubmit(BaseModel):
    worker_id: str
    result: str


class VerifyRequest(BaseModel):
    verified: bool
    notes: Optional[str] = None
    # Optional reference-asset return for the same window (e.g. ETH return
    # over a price_move_4h task's 4h horizon). The TVL verifier doesn't send
    # this; the price-move verifier does. Persisted to Task.baseline_return.
    baseline_return: Optional[float] = None


class PublishRequest(BaseModel):
    tweet_url: str


class AgentPublic(BaseModel):
    id: str
    name: str
    elo: int


class AgentRegisterResponse(AgentPublic):
    api_key: str
