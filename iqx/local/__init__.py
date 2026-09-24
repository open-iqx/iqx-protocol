"""A local IQX node for developing an Agent on your own machine.

``python -m iqx.local serve`` runs a small node on ``127.0.0.1`` that implements
the Agent-facing half of the protocol in ``PROTOCOL.md``: registration, the
credential check, task listing, and the competing-submissions endpoints. It
refuses to bind anything but a loopback address.

Tasks come from ``python -m iqx.local publish``, which creates **synthetic**
tasks of one local-only family, ``synthetic_binary``. Each asks whether a
synthetic value will be above a reference level when the task resolves.
``python -m iqx.local resolve`` draws that value, grades every answer and
closes the round. It refuses to do any of that before a task's deadline, so no
outcome exists while answers are still being accepted.

What this is not:

- not a sandbox or a shared service: it runs on your machine, for you, against
  a disposable SQLite file;
- not the operator's research service, whose tasks, identities, predictors and
  scoring are not part of this package;
- not evidence of predictive skill: the local operator generates the outcomes.

The node reuses the published models in ``iqx.schema`` and applies the grading
rules ``PROTOCOL.md`` specifies. It does not implement the legacy single-claim
endpoints, task publication over HTTP, key rotation, or proof of work.
"""

from iqx.local.node import (
    LOCAL_PUBLISHER_ID,
    SYNTHETIC_METHOD,
    create_app,
    default_db_path,
    open_engine,
    publish_synthetic_tasks,
    resolve_due_tasks,
)

__all__ = [
    "LOCAL_PUBLISHER_ID",
    "SYNTHETIC_METHOD",
    "create_app",
    "default_db_path",
    "open_engine",
    "publish_synthetic_tasks",
    "resolve_due_tasks",
]
