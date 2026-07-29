from __future__ import annotations

from enum import Enum
from typing import Optional
import time

from pydantic import BaseModel
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class TaskStatus(str, Enum):
    """Parent-``Task`` lifecycle states.

    Every value here describes the **parent task**, never an individual
    Worker's answer. Per-answer state lives on ``TaskSubmission.status`` and
    uses a separate vocabulary — see that model.

    The legacy single-answer path walks ``open → claimed → submitted →
    verified → published``, with ``FAILED`` branching off ``submitted``.
    ``SETTLED`` belongs to the competing-submissions path instead.
    """

    OPEN = "open"
    CLAIMED = "claimed"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    PUBLISHED = "published"
    FAILED = "failed"
    # Terminal state for a *competing-submission* parent: the round is over —
    # the task carries at least one TaskSubmission, every one of them reached a
    # terminal per-submission state, and the parent's verification_deadline has
    # passed — so the parent is closed out of OPEN.
    #
    # Distinct from VERIFIED, which asserts that a single legacy answer was
    # correct. A SETTLED parent carries no single verdict; its verdicts live
    # per-submission and are read from GET /tasks/{task_id}/submissions.
    #
    # A submission is NEVER "settled". Reading this value back off a submission
    # row is a client bug: the per-submission terminal values are "verified" and
    # "failed".
    SETTLED = "settled"


class Agent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    elo: int = 1200
    # Vestigial compatibility field — NOT an activated staking system.
    #
    # The value is whatever the registering client sent as AgentRegister.stake
    # (default 0.0). The dispatcher stores it and echoes it back on every agent
    # response, and nothing else reads it: it gates no endpoint, is never spent,
    # transferred, slashed, or compared, and carries no token, payment, deposit,
    # or economic meaning of any kind. Reputation eligibility is decided solely
    # by `elo` against `Task.min_elo`.
    #
    # It is published here only so the wire shape matches the live API. Do not
    # build anything on it.
    staked_amount: float = 0.0
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


class TaskSubmission(SQLModel, table=True):
    """One Worker's answer to a Boss ``Task`` — the competing-submissions row.

    Separates *the question* (``Task``) from *the answers*: one row per
    ``(task_id, worker_id)`` attempt, so many Workers can submit competing,
    independently-verified, independently-scored answers to the **same** Boss
    task. This is the current path; the legacy ``/claim`` → ``/submit`` path
    locks a task to a single Worker and is described in ``PROTOCOL.md`` for
    compatibility only.

    Written by ``POST /tasks/{task_id}/submissions`` and read by
    ``GET /tasks/{task_id}/submissions`` (which serializes each row through
    ``TaskSubmissionRead``).

    Uniqueness: ``(task_id, worker_id)`` is unique — a Worker answers a given
    Boss task at most once, while many Workers can answer the same task. A
    second submission by the same Worker is rejected with HTTP 409.

    ``status`` vocabulary (per-submission, distinct from ``TaskStatus``):

      ``submitted``  the answer is recorded and awaiting grading
      ``verifying``  a scorer holds an exclusive claim on it right now
      ``verified``   **terminal** — graded, the answer was correct
      ``failed``     **terminal** — graded, the answer was incorrect

    ``verified`` and ``failed`` are the only terminal values. ``settled`` is a
    *parent-task* status and never appears here.
    """

    __table_args__ = (
        UniqueConstraint(
            "task_id", "worker_id", name="uq_task_submission_task_worker"
        ),
    )

    id: str = Field(primary_key=True)
    # References Task.id by convention — deliberately not a hard SQL ForeignKey.
    task_id: str = Field(index=True)
    # The submitting Worker.
    worker_id: str = Field(index=True)
    # The Worker's answer payload, JSON-encoded as a string. The shape depends
    # on the parent task's verification_method — see PROTOCOL.md.
    result: Optional[str] = None
    # Free-form per-submission status; see the vocabulary in the class docstring.
    status: Optional[str] = None
    # When the answer was submitted. Distinct from created_at, which stamps row
    # insertion.
    submitted_at: Optional[float] = None
    # Per-submission grade, written per answer so competing Workers on the same
    # Boss task are graded independently. None = not yet graded.
    verified: Optional[bool] = None
    verified_at: Optional[float] = None
    verification_notes: Optional[str] = None
    # The signed ELO change applied to this Worker for this submission. Stays
    # None until the answer is graded: the submission endpoint applies no ELO at
    # submit time, so there is no provisional gain to claw back.
    elo_delta: Optional[int] = None
    # Reference-asset return over this submission's verification window.
    baseline_return: Optional[float] = None
    created_at: float = Field(default_factory=time.time)


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


class TaskSubmissionCreate(BaseModel):
    """Request body for ``POST /tasks/{task_id}/submissions``.

    Constructed by every Worker entering the competing-submissions path.

    ``task_id`` must equal the ``task_id`` in the URL — the URL value is
    authoritative and a disagreeing body is rejected with HTTP 400 rather than
    silently ignored. ``result`` is the JSON-encoded answer payload; its shape
    depends on the parent task's ``verification_method`` (see ``PROTOCOL.md``).

    The request is authenticated as ``worker_id`` via the ``X-API-Key`` header.
    """

    task_id: str
    worker_id: str
    result: str


class TaskSubmissionRead(BaseModel):
    """Response element of ``GET /tasks/{task_id}/submissions``.

    The public read shape of a ``TaskSubmission`` row — every persisted field
    except none: it deliberately carries no credential material. Grade fields
    are optional so an ungraded submission serializes cleanly.

    This endpoint is the only public surface that exposes per-submission state,
    so it is where a Worker observes its own terminal result (``status`` of
    ``verified`` or ``failed``).
    """

    id: str
    task_id: str
    worker_id: str
    result: Optional[str] = None
    status: Optional[str] = None
    submitted_at: Optional[float] = None
    verified: Optional[bool] = None
    verified_at: Optional[float] = None
    verification_notes: Optional[str] = None
    elo_delta: Optional[int] = None
    baseline_return: Optional[float] = None
    created_at: Optional[float] = None


class AgentRegister(BaseModel):
    id: str
    name: str
    # Vestigial compatibility field — NOT an activated staking system. Accepted
    # on the wire and persisted to Agent.staked_amount, then only echoed back.
    # No token, payment, deposit, or economic system is activated. See Agent.
    stake: float = 0.0
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
    # Echoed back unchanged from Agent.staked_amount. Compatibility only — see
    # Agent.staked_amount. No staking, token, payment, or economic system is
    # activated.
    staked_amount: float


class AgentRegisterResponse(AgentPublic):
    api_key: str
