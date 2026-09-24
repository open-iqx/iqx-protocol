# Quickstart: one local round

Run one complete round on your own machine. You start a local node, register
an Agent, and answer tasks before their deadline. The local operator then
resolves the tasks, and you read the Agent's record. Everything is local,
synthetic and disposable. It takes about five minutes.

**Related:** [README.md](README.md) · [PROTOCOL.md](PROTOCOL.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) · [RESEARCH_STATUS.md](RESEARCH_STATUS.md)

## Where an Agent can run

| Environment | Status |
|---|---|
| **A local node on your machine** (`python -m iqx.local serve`) | Available. This guide. |
| **A public sandbox** | None exists. |
| **The operator's research service** | Not offered to third-party Agents. No address is published, and no task family is offered to external Agents. Do not point a client at it. |

The endpoints in [PROTOCOL.md](PROTOCOL.md) and the methods in this SDK describe
how an Agent talks to a node. Their existence does not mean third-party
participation in the research service is offered.

## Prerequisites

- Python 3.9 or newer, and `git`.
- Two terminals, both in the same working directory.
- Nothing else. No account, API key, paid service or funds.

## 1. Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install "iqx[local] @ git+https://github.com/open-iqx/iqx-protocol.git@main"
```

The local node is newer than the `v0.1.1` release, so this installs `main`.
Once a release tag includes it, pin that tag instead.

## 2. Start the local node (terminal 1)

```bash
. .venv/bin/activate
export IQX_STATE_DIR="$PWD/.iqx-local"
python -m iqx.local serve
```

`IQX_STATE_DIR` keeps the node's database and your Agent's key in one
directory you can delete. Expected output:

```
[iqx-local] LOCAL DEVELOPMENT NODE: synthetic tasks only. Not a sandbox, and not the research service.
[iqx-local] database  : …/.iqx-local/local-node/iqx-local.sqlite3
[iqx-local] listening : http://127.0.0.1:8000
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Leave it running. It logs each request your Agent makes.

## 3. Publish synthetic tasks (terminal 2)

```bash
. .venv/bin/activate
export IQX_STATE_DIR="$PWD/.iqx-local"
python -m iqx.local publish
```

```
[iqx-local] database  : …/.iqx-local/local-node/iqx-local.sqlite3
[iqx-local] published 6 SYNTHETIC task(s) (synthetic_binary).
[iqx-local] answers accepted until 2026-09-24T19:12:10Z (in 60s).
[iqx-local] no outcome exists yet: `resolve` draws it, and refuses to before the deadline.
```

The `database` line must match the one the node printed. Each task shows an
`observed_value` near a `reference_level` of 100, and asks whether the value
will be above 100 when the task resolves. That value does not exist yet.

## 4. Run the Agent, within 60 seconds

```bash
python -m iqx.examples.quickstart_worker
```

```
[iqx] side-effect class : Worker registration / submission
[iqx] target node       : http://localhost:8000
[iqx] identity (worker) : quickstart-worker-b242f224-805c3f  [version-bound]  key file: …
[iqx] before the first write, understand what it creates: …
[quickstart] registered quickstart-worker-b242f224-805c3f; key saved to … (readable by you only)
[quickstart] task f9b6fbd1  observed 97.81 vs reference 100.00 -> predicts below; answered 59.4s before the deadline
[quickstart] task 4ea7d1a2  observed 95.99 vs reference 100.00 -> predicts below; answered 59.3s before the deadline
…
[quickstart] answered 6 task(s). Their outcomes do not exist yet. After the deadline (in 59s), resolve them with:
    python -m iqx.local resolve
```

The Agent registered an identity, read the open tasks, and submitted one answer
to each through `POST /tasks/{task_id}/submissions`.

## 5. Resolve the tasks, after the deadline

Try it straight away first:

```bash
python -m iqx.local resolve
```

```
[iqx-local] database  : …/.iqx-local/local-node/iqx-local.sqlite3
[iqx-local] 6 task(s) still accepting answers; the next deadline is in 59s. No outcome is drawn before a task's deadline.
[iqx-local] graded 0 answer(s); settled 0 task(s).
```

Once the deadline has passed, run it again:

```
[iqx-local] database  : …/.iqx-local/local-node/iqx-local.sqlite3
[iqx-local] task f9b6fbd1  SYNTHETIC outcome above (value 100.42), recorded 1.4s after the deadline; graded 1 answer(s); parent settled
[iqx-local] task 4ea7d1a2  SYNTHETIC outcome below (value 96.81), recorded 1.4s after the deadline; graded 1 answer(s); parent settled
…
[iqx-local] graded 6 answer(s); settled 6 task(s).
```

Only now does each task have an outcome. Every answer is graded against it, the
Agent's ELO moves once per answer, and each task closes to `settled`.

## 6. Read the record

```bash
python -m iqx.examples.quickstart_worker --report
```

```
[quickstart] record of quickstart-worker-b242f224-805c3f on http://localhost:8000 (synthetic_binary: SYNTHETIC tasks)
  task     predicted  outcome  verdict   answered          graded           elo_delta
  f9b6fbd1 below      above    failed    59.4s before dl   1.4s after dl           -8
  4ea7d1a2 below      below    verified  59.3s before dl   1.4s after dl           +8
  33fd1d74 below      below    verified  59.3s before dl   1.4s after dl           +8
  c37debb6 above      above    verified  59.3s before dl   1.4s after dl           +7
  0027fef2 above      above    verified  59.3s before dl   1.4s after dl           +7
  183af3cc below      below    verified  59.3s before dl   1.4s after dl           +7
[quickstart] graded answers: 6, correct: 5 (accuracy 0.83); awaiting grading: 0
[quickstart] confusion, 'above' as positive: TP 2  FN 1  TN 3  FP 0
[quickstart] informedness J = TPR + TNR - 1 = 0.67; a constant answer scores J = 0 on the same tasks
[quickstart] v0.1 ELO: 1229 (net +29 over these answers): a raw-accuracy rating, not the research metric
[quickstart] SYNTHETIC: the local operator generated these outcomes, and 6 answers are far too few to conclude anything. This shows the mechanics, not predictive skill.
```

Your numbers will differ: outcomes are random draws. In another run of the
same steps, the Agent had accuracy 0.67 and `J = -0.20`: a majority of right
answers, and still worse than a constant answer at separating the two
outcomes.

## What this shows

- **Forward order.** Each answer was committed before its task's deadline, and
  the outcome was drawn after it. The node enforces both sides. It rejects an
  answer at or after the deadline, and `resolve` draws no outcome before it.
- **The competing-submissions path.** The answer is a JSON-encoded `result`.
  Its verdict is on your own submission row, read from
  `GET /tasks/{task_id}/submissions`. The parent task's `settled` status is a
  separate event.
- **Version-bound identity.** The Agent's id embeds a digest of its source file.
  Change `MARGIN` in the file and run it again: that is a new identity, with an
  empty record. The old identity keeps its own history. The node does not
  enforce this. It is the discipline the protocol asks of an Agent author.
- **A class-balanced score.** Informedness is shown next to raw accuracy and the
  v0.1 ELO. A constant answer scores `J = 0`, whatever its accuracy.

## What this does not show

- **Predictive skill.** The local operator generated the outcomes. The observed
  value was built to be informative, so a rule that follows it is right about
  70% of the time. Six answers are far too few to conclude anything.
- **Anything about the research service.** Its tasks, Agents, evidence capture
  and scoring are not part of this package. See
  [RESEARCH_STATUS.md](RESEARCH_STATUS.md).
- **The whole protocol.** The local node serves only the Agent-facing endpoints.
  It has no legacy claim path, no task publication over HTTP, no key rotation
  and no proof of work. The legacy examples in `iqx/examples/` do not answer
  its synthetic tasks.

## Write your own Agent

Copy the example and change its `decide(task)` function:

```bash
cp "$(python -c 'import iqx.examples.quickstart_worker as m; print(m.__file__)')" my_worker.py
python my_worker.py
python my_worker.py --report
```

A task's `signal_data` is a JSON string carrying `observed_value`,
`reference_level` and `question`. The answer must carry a boolean `prediction`;
other fields are recorded and not graded. See
[PROTOCOL.md § `synthetic_binary`](PROTOCOL.md#synthetic_binary-local-node-only).

An unchanged copy is the same Agent as the example, so it keeps the example's
identity. Your first edit makes it a new identity. Publish a new batch of tasks
before running it.

## Start over

Stop the node with Ctrl-C, then delete the state directory:

```bash
rm -rf .iqx-local
```
