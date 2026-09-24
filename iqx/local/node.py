"""Local node: the Agent-facing HTTP endpoints plus the local operator's two jobs.

Three processes share one SQLite file, as a real node's dispatcher, publisher
and grader do:

- ``create_app`` builds the HTTP app an Agent talks to;
- ``publish_synthetic_tasks`` is the local publisher;
- ``resolve_due_tasks`` is the local grader.

The endpoint behaviour follows ``PROTOCOL.md``: the same paths, request and
response shapes, status codes and error order, and the same grading rule —
answers only before ``verification_deadline``, grading only after it, the whole
ELO change applied once at grading, and a parent task closed to ``settled`` once
it has at least one answer and every answer is terminal.
"""

from __future__ import annotations

import json
import random
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, create_engine, select

from iqx.helpers.state import resolve_state_dir
from iqx.schema import (
    Agent,
    AgentPublic,
    AgentRegister,
    AgentRegisterResponse,
    Task,
    TaskStatus,
    TaskSubmission,
    TaskSubmissionCreate,
    TaskSubmissionRead,
)

# ---- the synthetic task family ----------------------------------------------

#: Verification method of every task the local publisher creates. Graded by
#: this module only: it is deliberately not registered in ``iqx.registry``, and
#: no other node accepts it.
SYNTHETIC_METHOD = "synthetic_binary"
SYNTHETIC_TASK_TYPE = "synthetic"
LOCAL_PUBLISHER_ID = "local-operator"

REFERENCE_LEVEL = 100.0
#: ``observed_value`` is drawn uniformly within this distance of the reference.
OBSERVED_SPREAD = 5.0
#: At resolution the value moves from ``observed_value`` by a Gaussian step
#: with this standard deviation. The observed value is informative but far
#: from decisive: a rule that follows it is right about 70% of the time.
RESOLUTION_NOISE_SD = 4.0

SYNTHETIC_QUESTION = (
    "Will the synthetic value be above reference_level when the task resolves?"
)
SYNTHETIC_DESCRIPTION = (
    "SYNTHETIC local task: will the value be above the reference level at "
    "resolution?"
)

TERMINAL_SUBMISSION_STATUSES = ("verified", "failed")
ELO_K_FACTOR = 32


class SyntheticOutcome(SQLModel, table=True):
    """The resolved value of one synthetic task. Written once, after its deadline."""

    __tablename__ = "local_synthetic_outcome"

    task_id: str = Field(primary_key=True)
    value: float
    above: bool
    recorded_at: float


# ---- storage -------------------------------------------------------------------


def default_db_path() -> Path:
    """The node's database file, under the state directory (see ``PROTOCOL.md``)."""
    return resolve_state_dir() / "local-node" / "iqx-local.sqlite3"


def open_engine(db_path: Optional[Union[str, Path]] = None) -> Engine:
    """Open (creating if needed) the local node's SQLite database."""
    path = Path(db_path) if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    return engine


# ---- the Agent-facing HTTP app ------------------------------------------------


def _authenticate(session: Session, worker_id: str, api_key: Optional[str]) -> Agent:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    agent = session.get(Agent, worker_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not registered")
    if not secrets.compare_digest(agent.api_key, api_key):
        raise HTTPException(status_code=403, detail="Invalid API key")
    return agent


def _public(agent: Agent) -> AgentPublic:
    return AgentPublic(
        id=agent.id, name=agent.name, elo=agent.elo,
        staked_amount=agent.staked_amount,
    )


def create_app(engine: Engine, *, clock: Callable[[], float] = time.time) -> FastAPI:
    """Build the local node's HTTP app over ``engine``.

    ``clock`` is the node's notion of "now" for the submission window. Tests
    pass a fake one; everything else uses wall-clock time.
    """
    app = FastAPI(
        title="IQX local node",
        description="Local development node. Synthetic tasks only.",
    )

    def get_session():
        with Session(engine) as session:
            yield session

    @app.get("/")
    def banner():
        return {
            "node": "iqx-local",
            "synthetic_tasks_only": True,
            "task_family": SYNTHETIC_METHOD,
        }

    @app.post("/agents/register", response_model=AgentRegisterResponse)
    def register_agent(reg: AgentRegister, session: Session = Depends(get_session)):
        if session.get(Agent, reg.id):
            raise HTTPException(
                status_code=409,
                detail=f"Agent {reg.id!r} already registered.",
            )
        api_key = secrets.token_urlsafe(32)
        agent = Agent(id=reg.id, name=reg.name, staked_amount=reg.stake,
                      api_key=api_key)
        session.add(agent)
        session.commit()
        session.refresh(agent)
        return AgentRegisterResponse(**_public(agent).model_dump(), api_key=api_key)

    # Declared before /agents/{agent_id}, or "me" would match as an agent id.
    @app.get("/agents/me", response_model=AgentPublic)
    def get_self(
        x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
        x_worker_id: Optional[str] = Header(default=None, alias="X-Worker-Id"),
        session: Session = Depends(get_session),
    ):
        if not x_worker_id:
            raise HTTPException(status_code=400, detail="Missing X-Worker-Id header")
        return _public(_authenticate(session, x_worker_id, x_api_key))

    @app.get("/agents/{agent_id}", response_model=AgentPublic)
    def get_agent(agent_id: str, session: Session = Depends(get_session)):
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return _public(agent)

    @app.get("/tasks", response_model=List[Task])
    def list_tasks(
        status: Optional[TaskStatus] = None,
        session: Session = Depends(get_session),
    ):
        stmt = select(Task)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        return session.exec(stmt).all()

    @app.post("/tasks/{task_id}/submissions")
    def create_submission(
        task_id: str,
        body: TaskSubmissionCreate,
        x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
        session: Session = Depends(get_session),
    ):
        agent = _authenticate(session, body.worker_id, x_api_key)
        if body.task_id != task_id:
            raise HTTPException(
                status_code=400,
                detail=(f"task_id mismatch: URL {task_id!r} != body "
                        f"{body.task_id!r}; the URL task_id is authoritative"),
            )
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != TaskStatus.OPEN:
            raise HTTPException(
                status_code=400,
                detail=f"Task is not open for submissions (status={task.status})",
            )
        if (task.verification_deadline is not None
                and task.verification_deadline <= clock()):
            raise HTTPException(
                status_code=400,
                detail=(f"Task submission window closed: verification_deadline "
                        f"({task.verification_deadline}) has passed"),
            )
        if agent.elo < task.min_elo:
            raise HTTPException(
                status_code=403,
                detail=f"Incompetent Agent: ELO {agent.elo} < Required {task.min_elo}",
            )
        duplicate = session.exec(
            select(TaskSubmission).where(
                TaskSubmission.task_id == task_id,
                TaskSubmission.worker_id == agent.id,
            )
        ).first()
        if duplicate is not None:
            raise HTTPException(status_code=409,
                                detail="Worker already submitted to this task")

        submitted_at = clock()
        submission = TaskSubmission(
            id=str(uuid.uuid4()), task_id=task_id, worker_id=agent.id,
            result=body.result, status="submitted", submitted_at=submitted_at,
            created_at=submitted_at,
        )
        session.add(submission)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(status_code=409,
                                detail="Worker already submitted to this task")
        session.refresh(submission)
        # No ELO moves at submit time; the whole change is applied at grading.
        return {
            "submission": TaskSubmissionRead.model_validate(
                submission, from_attributes=True),
            "elo_change": 0,
            "new_elo": agent.elo,
        }

    @app.get("/tasks/{task_id}/submissions", response_model=List[TaskSubmissionRead])
    def list_submissions(task_id: str, session: Session = Depends(get_session)):
        rows = session.exec(
            select(TaskSubmission)
            .where(TaskSubmission.task_id == task_id)
            .order_by(TaskSubmission.submitted_at)
        ).all()
        return [TaskSubmissionRead.model_validate(r, from_attributes=True)
                for r in rows]

    return app


# ---- the local publisher -------------------------------------------------------


def publish_synthetic_tasks(
    engine: Engine,
    *,
    count: int = 6,
    window_sec: float = 60.0,
    min_elo: int = 1000,
    now: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> List[Task]:
    """Create ``count`` open synthetic tasks that accept answers for ``window_sec``.

    Each task shows an ``observed_value`` near ``REFERENCE_LEVEL``. The value
    that settles it does not exist yet: :func:`resolve_due_tasks` draws it
    after the deadline.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if window_sec <= 0:
        raise ValueError("window_sec must be positive: a task whose deadline "
                         "has passed can never be answered")
    now = time.time() if now is None else now
    rng = rng if rng is not None else random.Random()
    tasks = []
    for _ in range(count):
        observed = round(
            REFERENCE_LEVEL + rng.uniform(-OBSERVED_SPREAD, OBSERVED_SPREAD), 2)
        spec = {
            "synthetic": True,
            "question": SYNTHETIC_QUESTION,
            "reference_level": REFERENCE_LEVEL,
            "observed_value": observed,
        }
        tasks.append(Task(
            id=str(uuid.uuid4()),
            description=SYNTHETIC_DESCRIPTION,
            budget=0.0,
            min_elo=min_elo,
            created_at=now,
            publisher_id=LOCAL_PUBLISHER_ID,
            task_type=SYNTHETIC_TASK_TYPE,
            verification_method=SYNTHETIC_METHOD,
            verification_mode="automatic",
            verification_deadline=now + window_sec,
            signal_data=json.dumps(spec, sort_keys=True),
        ))
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(tasks)
        session.commit()
    return tasks


# ---- the local grader ----------------------------------------------------------


@dataclass
class TaskResolution:
    """What resolving one task did."""

    task_id: str
    deadline: float
    value: float
    above: bool
    recorded_at: float
    #: True iff this run drew the outcome; False iff an earlier run did.
    newly_recorded: bool
    graded: int
    settled: bool


@dataclass
class ResolveReport:
    resolved: List[TaskResolution] = field(default_factory=list)
    #: ``(task_id, deadline)`` for open synthetic tasks still inside their window.
    not_due: List[tuple] = field(default_factory=list)


def elo_change(worker_elo: int, min_elo: int, verified: bool) -> int:
    """The v0.1 rule in ``PROTOCOL.md``: ``±round(32 × (1 − expected))``."""
    expected = 1 / (1 + 10 ** ((min_elo - worker_elo) / 400))
    gain = round(ELO_K_FACTOR * (1 - expected))
    return gain if verified else -gain


def grade_answer(result: Optional[str], outcome: SyntheticOutcome) -> tuple:
    """Grade one ``synthetic_binary`` answer. Returns ``(verified, notes)``."""
    try:
        payload = json.loads(result or "")
    except (TypeError, ValueError):
        return False, "answer is not valid JSON"
    prediction = payload.get("prediction") if isinstance(payload, dict) else None
    if not isinstance(prediction, bool):
        return False, "answer must carry a boolean 'prediction'"
    said = "above" if prediction else "below"
    return prediction == outcome.above, f"predicted {said}"


def _draw_outcome(task: Task, seed: Optional[int], now: float) -> SyntheticOutcome:
    spec = json.loads(task.signal_data or "{}")
    rng = (random.Random(f"{seed}:{task.id}") if seed is not None
           else random.Random())
    value = round(spec["observed_value"] + rng.gauss(0.0, RESOLUTION_NOISE_SD), 2)
    return SyntheticOutcome(task_id=task.id, value=value,
                            above=value > spec["reference_level"], recorded_at=now)


def _close_parent_if_settled(session: Session, task: Task, now: float) -> bool:
    if task.verification_deadline is None or task.verification_deadline > now:
        return False
    statuses = session.exec(
        select(TaskSubmission.status).where(TaskSubmission.task_id == task.id)
    ).all()
    if not statuses or any(s not in TERMINAL_SUBMISSION_STATUSES for s in statuses):
        return False
    result = session.execute(
        update(Task)
        .where(Task.id == task.id)
        .where(Task.status == TaskStatus.OPEN)
        .values(status=TaskStatus.SETTLED)
    )
    session.commit()
    return result.rowcount == 1


def resolve_due_tasks(
    engine: Engine,
    *,
    now: Optional[float] = None,
    seed: Optional[int] = None,
) -> ResolveReport:
    """Resolve every open synthetic task whose deadline has passed.

    For each one: draw and record its outcome (once — a second run reuses it),
    grade every answer still ``submitted``, apply each Worker's ELO change, and
    close the parent to ``settled`` if it has at least one answer and all of
    them are terminal. A task still inside its window is left untouched and
    reported in ``not_due``: no outcome is drawn for it.

    Safe to run repeatedly. An answer that lands just before the deadline but
    after this run read the task is graded by the next run.

    ``seed`` makes the drawn outcomes reproducible.
    """
    now = time.time() if now is None else now
    report = ResolveReport()
    with Session(engine) as session:
        tasks = session.exec(
            select(Task)
            .where(Task.verification_method == SYNTHETIC_METHOD)
            .where(Task.status == TaskStatus.OPEN)
            .order_by(Task.created_at)
        ).all()
        for task in tasks:
            if task.verification_deadline is None:
                continue
            if task.verification_deadline > now:
                report.not_due.append((task.id, task.verification_deadline))
                continue

            outcome = session.get(SyntheticOutcome, task.id)
            newly_recorded = outcome is None
            if newly_recorded:
                outcome = _draw_outcome(task, seed, now)
                session.add(outcome)
                session.commit()

            pending = session.exec(
                select(TaskSubmission)
                .where(TaskSubmission.task_id == task.id)
                .where(TaskSubmission.status == "submitted")
            ).all()
            side = "above" if outcome.above else "below"
            for submission in pending:
                verified, notes = grade_answer(submission.result, outcome)
                agent = session.get(Agent, submission.worker_id)
                if agent is not None:
                    delta = elo_change(agent.elo, task.min_elo, verified)
                    agent.elo += delta
                    submission.elo_delta = delta
                    session.add(agent)
                submission.verified = verified
                submission.verified_at = now
                submission.status = "verified" if verified else "failed"
                submission.verification_notes = (
                    f"SYNTHETIC outcome {side} (value {outcome.value:.2f} vs "
                    f"reference {REFERENCE_LEVEL:.2f}), recorded "
                    f"{outcome.recorded_at - task.verification_deadline:.1f}s "
                    f"after the deadline; {notes}"
                )
                session.add(submission)
                session.commit()

            report.resolved.append(TaskResolution(
                task_id=task.id,
                deadline=task.verification_deadline,
                value=outcome.value,
                above=outcome.above,
                recorded_at=outcome.recorded_at,
                newly_recorded=newly_recorded,
                graded=len(pending),
                settled=_close_parent_if_settled(session, task, now),
            ))
    return report
