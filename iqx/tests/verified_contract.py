"""Frozen description of the verified IQX node contract.

This is the reference the drift tests compare the published SDK models and
documentation against. It is data, not behaviour: changing a value here is a
deliberate statement that the node's contract changed, and the tests that read
it will then say which published artefact has fallen out of step.

**Provenance.** Every entry was verified against the node implementation that
is deployed, not inferred from the SDK models it is used to check — otherwise
the comparison would be circular. Nothing here was taken from a live response
body, and nothing here contains a hostname, credential, or operator detail.

Keep this file free of anything that is merely *planned*. A method, status, or
endpoint belongs here only once the node actually implements it.
"""

from __future__ import annotations

# ---- task (parent) statuses --------------------------------------------------

#: Every value ``Task.status`` can hold. ``settled`` is the competing-submission
#: parent's terminal state; the other six belong to the legacy single-answer
#: lifecycle.
TASK_STATUS_VALUES: frozenset[str] = frozenset({
    "open",
    "claimed",
    "submitted",
    "verified",
    "published",
    "failed",
    "settled",
})

#: The parent status that closes a competing-submission round.
PARENT_TERMINAL_STATUS: str = "settled"


# ---- submission statuses -----------------------------------------------------

#: Every value ``TaskSubmission.status`` can hold, in lifecycle order. This
#: vocabulary is deliberately separate from ``TASK_STATUS_VALUES``: a submission
#: is never "settled", and a parent is never "verifying".
SUBMISSION_STATUSES: tuple[str, ...] = (
    "submitted",
    "verifying",
    "verified",
    "failed",
)

#: The only submission statuses that mean "graded, and done". ``verifying`` is
#: an exclusive-claim state and is explicitly not terminal — a grader that
#: fails mid-way releases the row back to ``submitted``.
TERMINAL_SUBMISSION_STATUSES: tuple[str, ...] = ("verified", "failed")


# ---- verification methods ----------------------------------------------------

#: The complete registered set. Not a subset, not a roadmap.
VERIFICATION_METHODS: frozenset[str] = frozenset({
    "defillama_tvl_retention_24h",
    "price_move_4h",
    "worker_prediction_accuracy_4h",
    "echo",
})


# ---- endpoints ---------------------------------------------------------------

#: ``(method, path, auth)`` for every public endpoint. ``auth`` is one of
#: ``"none"``, ``"agent"`` (X-API-Key), ``"admin"`` (X-Admin-Key).
ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("POST", "/agents/register", "none"),
    ("POST", "/agents/register/challenge", "none"),
    ("POST", "/agents/{agent_id}/rotate-key", "agent"),
    ("GET", "/agents/me", "agent"),
    ("GET", "/agents/{agent_id}", "none"),
    ("POST", "/tasks", "none"),
    ("GET", "/tasks", "none"),
    ("POST", "/tasks/{task_id}/submissions", "agent"),
    ("GET", "/tasks/{task_id}/submissions", "none"),
    ("POST", "/tasks/{task_id}/claim", "agent"),
    ("POST", "/tasks/{task_id}/submit", "agent"),
    ("POST", "/tasks/{task_id}/verify", "admin"),
    ("POST", "/tasks/{task_id}/submissions/{submission_id}/verify", "admin"),
    ("POST", "/tasks/{task_id}/publish", "admin"),
)

#: The endpoint a Worker writes a competing answer to.
SUBMISSION_WRITE_ENDPOINT: str = "/tasks/{task_id}/submissions"

#: The **only** public endpoint that exposes per-submission state, and therefore
#: the only place a Worker can observe its own terminal result.
SUBMISSION_READ_ENDPOINT: str = "/tasks/{task_id}/submissions"

#: Endpoints the legacy single-claim lifecycle uses. Live for compatibility;
#: not the path a new Worker is built on.
LEGACY_CLAIM_ENDPOINTS: tuple[str, ...] = (
    "/tasks/{task_id}/claim",
    "/tasks/{task_id}/submit",
)

#: Deliberately absent. A single-task read does not exist; callers list tasks
#: and filter, or read the submissions endpoint.
ABSENT_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET", "/tasks/{task_id}"),
)


# ---- DTO field sets ----------------------------------------------------------

#: Request body of ``POST /tasks/{task_id}/submissions``.
TASK_SUBMISSION_CREATE_FIELDS: frozenset[str] = frozenset({
    "task_id", "worker_id", "result",
})

#: Response element of ``GET /tasks/{task_id}/submissions``.
TASK_SUBMISSION_READ_FIELDS: frozenset[str] = frozenset({
    "id", "task_id", "worker_id", "result", "status", "submitted_at",
    "verified", "verified_at", "verification_notes", "elo_delta",
    "baseline_return", "created_at",
})

#: Persisted competing-submission row.
TASK_SUBMISSION_FIELDS: frozenset[str] = TASK_SUBMISSION_READ_FIELDS

#: Request body of ``POST /agents/register``.
AGENT_REGISTER_FIELDS: frozenset[str] = frozenset({
    "id", "name", "stake", "challenge_id", "nonce",
})

#: Public Agent view, returned by ``GET /agents/me`` and ``GET /agents/{id}``.
AGENT_PUBLIC_FIELDS: frozenset[str] = frozenset({
    "id", "name", "elo", "staked_amount",
})


# ---- stake compatibility -----------------------------------------------------

#: The vestigial stake fields, request-side and response-side. Present on the
#: wire; read by nothing. No staking, token, payment, or economic system is
#: activated, and these names must never be documented as if one were.
STAKE_REQUEST_FIELD: str = "stake"
STAKE_RESPONSE_FIELD: str = "staked_amount"
STAKE_DEFAULT: float = 0.0


# ---- Worker answer contract --------------------------------------------------

#: Fields a ``worker_prediction_accuracy_4h`` answer must carry. ``is_alpha`` is
#: the only graded field: a missing or non-boolean value fails the answer before
#: any price is fetched.
ANSWER_REQUIRED_FIELDS: dict[str, type] = {"is_alpha": bool}

#: Recorded but never graded. Notably ``predicted_4h_return_pct``: there is no
#: return-magnitude scoring and no ``horizon`` field.
ANSWER_OPTIONAL_FIELDS: tuple[str, ...] = (
    "confidence",
    "reasoning",
    "evidence_tx",
    "predicted_4h_return_pct",
)

#: An ``echo`` answer. Deterministic string equality against the token carried
#: in the task description as ``echo:<token>``.
ECHO_ANSWER_FIELD: str = "echo"

#: The move threshold both price-based graders use, as a fraction.
PRICE_MOVE_PASS_PCT: float = 0.03

#: The TVL retention threshold, as a fraction.
TVL_RETENTION_PASS_RATIO: float = 0.8


# ---- statements that must stay true in public material -----------------------

#: Onboarding is **not** live: no onboarding task family is published, and this
#: repository publishes no reference node URL.
ONBOARDING_IS_LIVE: bool = False

#: Whether any staking/token/payment system is activated.
STAKING_IS_ACTIVATED: bool = False

#: Whether registration proof-of-work is on by default, and whether any client
#: in this repository implements the challenge/solve flow.
POW_REQUIRED_BY_DEFAULT: bool = False
POW_IMPLEMENTED_BY_ANY_SHIPPED_CLIENT: bool = False
