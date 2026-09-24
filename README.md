# IQX

**IQX studies forward-only reputation and trust routing for AI agents.**

**White paper:** [*IQX: Forward-Only Reputation and Trust Routing for AI
Agents*](papers/iqx-whitepaper-v0.1.pdf), v0.1 — Editorial revision 1,
23 September 2026. It is a technical report, not peer reviewed, and its
experimental results, methods and verdicts are unchanged from the edition of
22 September 2026. Its summary, citation and license are in
[papers/](papers/README.md).

An agent commits a falsifiable, time-bounded answer before the outcome that
settles it exists. A verifier grades that answer later, against an outcome the
agent could not have observed when it answered. In the research design, only
settled outcomes count as performance evidence: an ungraded submission does not
support a scientific claim.

Four properties define the method:

- **Forward-only evidence.** A claim is fixed while its outcome is still unknown,
  so evaluation cannot draw on information that postdates the answer.
- **Frozen predictor identity.** The evaluated subject is a specific predictor at
  a specific code and configuration version — not a model brand, and not a
  human-readable name.
- **Explicit evidence boundaries.** An answer is bound to the capture it was
  produced from and to the schema under which it was written, and a stored
  artifact keeps that schema's semantics — a newer reader may verify old evidence
  but may not reinterpret it. The forward activation boundary and the enrolled
  predictor identities are held separately, in durable cohort and authority
  metadata.
- **No retrospective reassignment.** A materially changed predictor enters under
  a new identity and a new boundary. Historical evidence stays attached to the
  version that produced it and is never rescored into the changed predictor.

**Why raw-accuracy ELO is insufficient.** The v0.1 protocol scores agents with an
ELO rule driven by raw accuracy, which is not a measure of discrimination: on an
imbalanced task stream a majority-class predictor can reach strong accuracy — and
so a strong ranking — while its class-balanced informedness,
`J = TPR + TNR − 1`, is zero. A constant predictor scores `J = 0` at any base
rate, by construction. The research program therefore treats raw-accuracy ELO as
insufficient as a discrimination metric and has moved to class-balanced,
preregistered, forward-only evaluation against explicit constant baselines. The
offline replay benchmark below shows the same arithmetic concretely.

**The active research program** runs a paired experiment in a delayed-outcome
market domain and asks two ordered questions:

1. **Is market-only information learnable?** Does a market-only predictor
   discriminate better than a constant baseline on forward tasks?
2. **Does wallet-derived information add incremental value** over that
   market-only baseline, when the wallet term is the only intended difference
   between two otherwise identical predictors answering the same task from the
   same market capture?

The metric, the baselines, the support conditions, and the stopping rules are
frozen before any eligible outcome from the evaluated run is observed.

**What is not claimed.** No market-alpha claim, no wallet-signal claim, no agent
capability claim, and no trust-routing claim has been established here. The
V3.3 evidence reported in the [white paper](papers/README.md) ends at a frozen
cut, closed by an administrative decision and not by the preregistered stopping
rule, which would have continued. Under the preregistered rules, its readout
concludes that learnability is **not established**, with no capability tier
claimed, and that the wallet increment is **inconclusive**. Predictive
reputation and routing value remain unvalidated. Predictions made after the cut
fall outside it and do not change it. [RESEARCH_STATUS.md](RESEARCH_STATUS.md)
states the method and the claims that are explicitly not being made.

## 🧭 What this repository is

IQX's **public research record, protocol specification, and reference SDK**.

- It is **not** a mirror of the production or experimental system, and it
  publishes neither that system's configuration nor its operational state.
- The `iqx/` package, the protocol reference, and the offline replay benchmark
  are the **current public reference snapshot**: a working, self-contained
  artifact documenting the v0.1-era protocol surface.
- There is **no public onboarding flow**. No reference node URL is published
  here, no onboarding or practice task family exists, and this repository cannot
  by itself take a new developer through a live end-to-end round. The offline
  replay benchmark below needs no node and works fully.
- A **local round on your own machine** is possible:
  [QUICKSTART.md](QUICKSTART.md) starts a local node with synthetic tasks,
  registers an Agent, answers before the deadline, resolves after it, and prints
  the Agent's record.

| Document | Contents |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | One local round on your own machine: a local node, synthetic tasks, one Agent, its record |
| [RESEARCH_STATUS.md](RESEARCH_STATUS.md) | The active research questions, the forward-only and preregistered method, and the claims not being made |
| [papers/](papers/README.md) | White Paper v0.1 (PDF): the protocol, the V3.3 evidence cut and its limits. Licensed separately, under CC BY 4.0 |
| [PROTOCOL.md](PROTOCOL.md) | The v0.1 protocol surface — statuses, both lifecycles, endpoints, the Worker answer schema, verification methods, terminal-state semantics, credentials |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Failure modes in the public reference SDK, and what each one actually means |

> **⚠️ Pre-v1.0 stability.** The published surface is at `v0.1` and may change
> without notice until `v1.0-stable`. Pin the **`v0.1.1` release tag** — it is the
> snapshot this documentation describes. The `v0.1.0` tag is the initial May 2026
> SDK release and describes an earlier, different contract. See the SDK install
> section below.

## 🏗️ Core pillars of the v0.1 protocol

These are the abstractions the public snapshot published. The first three are
protocol-level and unaffected by what follows. The fourth is the one the research
program treats as superseded.

- **Agent Task Protocol (ATP)**: Standardized JSON schema for tasks (`publisher_id`, `worker_id`, `task_type`, `verification_method`, `verification_mode`) — supports the publisher → worker → verifier role split natively from day one.
- **Pluggable Verifier Registry**: Verification methods are registered plugins keyed on `verification_method`. The complete set is `defillama_tvl_retention_24h`, `price_move_4h`, `worker_prediction_accuracy_4h`, and `echo` — see [PROTOCOL.md § Verification methods](PROTOCOL.md#verification-methods).
- **Competing submissions**: many Workers answer the **same** open task independently through `POST /tasks/{task_id}/submissions`, and each is graded and scored on its own row. An older single-claim path remains in the published surface for compatibility; see [PROTOCOL.md § Two lifecycles](PROTOCOL.md#two-lifecycles).
- **Reputation via raw-accuracy ELO** — **superseded.** v0.1 rates agents with an ELO update computed from a raw pass/fail verdict. That is the rule the overview above describes as insufficient: on an imbalanced task stream it can rank majority-class behavior above predictors that discriminate. It stays documented because it is what the published snapshot implements; it is not the metric the research program uses. Sybil defense is intended to be a PoW challenge at registration plus per-agent rate limits — explicitly **not** staking or token deposits. The `stake` / `staked_amount` fields on the wire are **compatibility fields only**: no staking, token, payment, or economic system is activated, and their value is stored, echoed back, and read by nothing.

## 📦 SDK install — the current public reference snapshot

> The `iqx/` package, its examples, and the replay benchmark below are the
> **public reference snapshot** of the v0.1-era SDK, not the current research
> integration path. They are preserved, installable, and tested. They do not
> track the active experiment, and nothing in this section should be read as a
> description of it.

> ⚠️ **Heads-up on PyPI**: there is an unrelated package named `iqx` on PyPI that is **not** affiliated with this project. Do **not** `pip install iqx`. The canonical install for the IQX SDK pins the `v0.1.1` release tag: `pip install git+https://github.com/open-iqx/iqx-protocol.git@v0.1.1`. No PyPI release for this project is planned in v0.x.

The `iqx/` package is the public protocol surface — installable via `pip` directly from this Git repo. No PyPI release in v0.x; distribution is `git+https://…` until external adoption justifies the maintenance overhead.

```bash
# Canonical install — v0.1.1: the July 2026 protocol-aligned public
# implementation plus the current research-positioning documentation.
# This is the SDK this documentation describes.
pip install git+https://github.com/open-iqx/iqx-protocol.git@v0.1.1
```

The **initial May 2026 SDK release tag** is still installable, and is the right
choice only if you specifically want that first release:

```bash
# Initial release tag — predates PROTOCOL.md, TROUBLESHOOTING.md, the
# competing-submission schema, the safety examples and their contract tests.
# It does NOT implement the contract documented in PROTOCOL.md.
pip install git+https://github.com/open-iqx/iqx-protocol.git@v0.1.0
```

Sanity-check the install:

```bash
python -c "import iqx; print(iqx.__version__)"                        # → 0.1.1
python -c "from iqx import Task, Agent, register_verifier, Verdict"   # top-level vocabulary
python -c "import iqx.schema, iqx.registry, iqx.pow, iqx.verifier"    # SDK modules
python -c "import iqx.examples.worker_judge, iqx.examples.boss_smart_money, iqx.examples.baseline_worker, iqx.examples.self_play"
python -c "import iqx.bench.replay, iqx.bench.dataset"
python -c "import iqx.helpers.price, iqx.helpers.defillama"
```

The installable package surface:

| Module | Purpose |
|---|---|
| `iqx` | `__version__` + top-level re-exports (`Task`, `Agent`, `register_verifier`, `Verdict`) |
| `iqx.schema` | `Task`, `Agent`, `TaskSubmission`, `TaskStatus` enum and the request/response DTOs |
| `iqx.registry` | Dep-light verifier registry — `@register_verifier`, `verify(task, ctx)`, `Verdict` |
| `iqx.pow` | PoW challenge primitives (`generate_challenge_prefix`, `verify_pow`, `TokenBucket`) |
| `iqx.verifier` | Registry re-export + reference verification methods (TVL, price-move, worker-prediction, echo) |
| `iqx.helpers.price` | CoinGecko price helpers + per-chain WETH addresses |
| `iqx.helpers.defillama` | DefiLlama protocol fetch helper |
| `iqx.helpers.state` | `resolve_state_dir()` — canonical state-directory resolution for credentials and caches across source-tree and pip-installed deployments |
| `iqx.local` | Local development node — `python -m iqx.local serve / publish / resolve`. Loopback only, synthetic tasks only; needs the `local` extra. On `main`, not in `v0.1.1`. See [QUICKSTART.md](QUICKSTART.md) |
| `iqx.examples.identity` | Agent-id resolution (CLI / env / generated) and the write safeguards every write-capable example goes through |
| `iqx.examples.quickstart_worker` | Minimal Worker on the competing-submissions path, with a version-bound identity and a `--report` of its graded record. Answers only the local node's synthetic tasks. On `main`, not in `v0.1.1` |
| `iqx.examples.boss_smart_money` | Boss-only smart-money cluster monitor (operator-oriented) |
| `iqx.examples.worker_judge` | Independent Judge Worker (`worker_prediction_accuracy_4h`) |
| `iqx.examples.baseline_worker` | Reference Worker — defaults to claiming `echo` only; `worker_prediction_accuracy_4h` requires explicit `--methods` opt-in (the legacy claim path is single-claim, so the baseline must not take live prediction tasks from smarter Workers) |
| `iqx.examples.self_play` | Dual-role demo (publisher ≠ worker via `echo` verification; operator-oriented) |
| `iqx.bench.replay` | Offline replay benchmark — `python3 -m iqx.bench.replay --worker module:fn` scores a Worker against a frozen 8-record dataset and prints accuracy vs. the baseline floor (exit 0 if Worker ≥ baseline). No network. |
| `iqx.bench.dataset` | Replay dataset loader — JSONL reader + `ReplayRecord` dataclass + `default_dataset_path()` (package-resource resolution for the shipped 8-record dataset) |

The operator-private central-node code (`main.py`, `db.py`, `verifier.py` (the poller), `publisher.py`, `agents/`) is **not** installed — it stays in the operator's own repo and runs alongside the SDK (installed via the canonical tagged URL above) only on the operator's own node. `iqx.local` is not that code: it is a small local node written for developing an Agent, and it serves only the Agent-facing endpoints.

> **Versioning policy**: no stability guarantee until `v1.0-stable`. Pin the `v0.1.1` release tag (or its commit SHA); `main` may change beneath you.

## 🚀 Getting started

> Everything in this section belongs to the **current public reference snapshot**.
> **This repository publishes no reference node URL.** The published
> specification, not any particular deployment, is what this repository keeps.

### Where an Agent can run

- **A local node on your own machine** — available. [QUICKSTART.md](QUICKSTART.md)
  runs one complete round with synthetic tasks. It is local and disposable, and
  it says nothing about predictive skill.
- **A public sandbox** — none exists.
- **The operator's research service** — not offered to third-party Agents. No
  address is published and no task family is offered to external Agents. Do not
  point a client at it. The endpoints in [PROTOCOL.md](PROTOCOL.md) describe how
  an Agent talks to a node; they do not mean third-party participation is
  offered.

### Start offline — the replay benchmark

The replay benchmark ships a frozen 8-record dataset and grades any conforming Worker against the reference baseline accuracy floor. It is **fully offline** — no node, no CoinGecko, no network — and it is the part of this repository that runs end-to-end today.

```bash
# 1. Score the shipped baseline first (sanity check that the bench runs)
python3 -m iqx.bench.replay
# → worker accuracy: 4/8 (50.0%); baseline floor: 4/8 (50.0%); exit 0

# 2. Plug in your own Worker — any callable with signature
#    `(task: dict) -> dict | None` returning {"is_alpha": bool, ...}
python3 -m iqx.bench.replay --worker my_pkg.my_module:my_build_verdict
# → worker accuracy: 7/8 (87.5%); baseline floor: 4/8 (50.0%); exit 0
```

The benchmark exits `0` when your Worker ≥ baseline and `1` otherwise — a one-line gate you can wire into your own CI.

**What the benchmark does and does not show.** It demonstrates **mechanics and
reproducibility**: that the answer schema, the grading rule, and the dataset
loader agree, and that the same Worker scores identically on every run. It is
**not** evidence of predictive skill, and not a measure of production
performance.

Its dataset is eight retrospective records with a balanced outcome split. The
shipped reference Worker answers the same way on all eight, so its 50% comes
from the dataset's class balance alone — it discriminates between no two
records. That is exactly the failure mode raw accuracy cannot see, and the
reason the research program scores against explicit constant baselines instead.

The benchmark is also **not** the paired experiment described in
[RESEARCH_STATUS.md](RESEARCH_STATUS.md). It shares none of its data,
identities, or evaluation rules, and says nothing about it.

### Writing a Worker against a node

Read [PROTOCOL.md](PROTOCOL.md) first. The short version:

- A Worker answers an open task with `POST /tasks/{task_id}/submissions`, authenticated as its agent id via `X-API-Key`. Many Workers answer the **same** task independently.
- Answers are accepted only **before** the task's `verification_deadline` and graded only **after** it. The deadline is a hard boundary in both directions.
- No ELO moves at submit time; the whole signed change is applied once, at grading.
- Read your own result from `GET /tasks/{task_id}/submissions` and match on your `worker_id`. Terminal per-submission values are `verified` and `failed`. The parent task's `settled` status is a **different** event and may come much later.
- The answer schema is per verification method — see [PROTOCOL.md § Worker answer contract](PROTOCOL.md#worker-answer-contract).

**Before you copy an example:** start from `iqx.examples.quickstart_worker`. It
answers through `POST /tasks/{task_id}/submissions` and reads its verdicts back
from `GET /tasks/{task_id}/submissions`; [QUICKSTART.md](QUICKSTART.md) runs it.
The other modules in `iqx/examples/` are **legacy v0.1 SDK examples; not the
current research integration path.** Each of them uses the older single-claim path
(`/claim` → `/submit`), where one Worker locks a task and ELO moves before
grading. They are kept that way for compatibility, they each say so in their
module docstring and `--help`, and they are **not** the shape to build a new
Worker on.

The published surface defines one task family — a 4-hour DeFi prediction — and no practice family, so a Worker that submits waits out that window before any verdict exists.

### Example side-effect classification

Apart from `quickstart_worker`, these are legacy v0.1 SDK examples, not the
current research integration path. Every example is exactly one of four classes,
and each states its class in its module docstring and in `--help`.

| Command | Side-effect class | Notes |
|---|---|---|
| `python3 -m iqx.bench.replay` | **offline / read-only** | Frozen dataset, no network. |
| `python3 -m iqx.examples.quickstart_worker --dry-run` | **Worker registration / submission** | Competing-submissions Worker for the local node's synthetic tasks. `--dry-run` and `--report` write nothing. See [QUICKSTART.md](QUICKSTART.md). |
| `python3 -m iqx.examples.baseline_worker --dry-run` | **Worker registration / submission** | Reference Worker; claims `echo` only by default. `--dry-run` previews without registering or writing. |
| `python3 -m iqx.examples.worker_judge --dry-run` | **Worker registration / submission** | Judge Worker for smart-money tasks. `--dry-run` prints verdicts without writing. |
| `python3 -m iqx.examples.boss_smart_money --dry-run` | **Boss / task publishing** | Operator-oriented. Detects without publishing. Requires `ETHERSCAN_API_KEY` ([free tier](https://etherscan.io/myapikey)). |
| `python3 -m iqx.examples.self_play --dry-run` | **Boss / task publishing** | Operator-oriented dual-role demo. Prints intent, makes no HTTP call. |

Both verify endpoints and the publish endpoint are **admin / operator-oriented** and are not callable by an external developer. Public Boss onboarding is not offered; the Boss examples are references, not a quickstart.

Every invocation above uses a bounded first-run flag so a copy-paste does not start a long-running consumer. `--loop` and non-`--dry-run` modes are documented in each module's `--help`.

### Safeguards on write-capable examples

Anything that writes must clear all of these first:

- **The default node URL is loopback** (`http://localhost:8000`). An unconfigured client cannot reach, let alone write to, a remote node.
- **Writing to any non-loopback node requires an explicit opt-in** — `--allow-public-writes`, or `IQX_ALLOW_PUBLIC_WRITES=1`. Without it the example refuses and exits, before any HTTP call.
- **The exact identity and target node are printed before the first write**, together with a disclosure that registration creates a persistent public identity and permanent public records, and that removal is operator-only.
- **Incomplete configuration fails safely** rather than surfacing later inside a request.

### Agent identities

Agent ids are resolved per run: `--agent-id` (or `--publisher-id` / `--worker-id` for `self_play`), then the example's environment variable, then a **freshly generated unique default**. Two consecutive runs therefore use two distinct identities with no source edit, and never collide with an id already registered on a node.

```bash
python3 -m iqx.examples.baseline_worker --dry-run        # generated id, new each run
IQX_BASELINE_WORKER_ID=my-worker-1 python3 -m iqx.examples.baseline_worker --dry-run
python3 -m iqx.examples.baseline_worker --agent-id my-worker-1 --dry-run
```

| Example | Environment variable |
|---|---|
| `baseline_worker` | `IQX_BASELINE_WORKER_ID` |
| `worker_judge` | `IQX_JUDGE_WORKER_ID` |
| `boss_smart_money` | `IQX_BOSS_AGENT_ID` |
| `self_play` | `IQX_SELFPLAY_PUBLISHER_ID`, `IQX_SELFPLAY_WORKER_ID` |
| `quickstart_worker` | `IQX_QUICKSTART_WORKER_ID` |

`quickstart_worker` is the exception to the per-run default: its default id
embeds a digest of its own source file plus a random suffix kept in the state
directory, so the same file keeps one identity across runs and an edited file
gets a new one.

The credential is cached under the state directory in a file derived from the resolved id, so pinning an id keeps its key across runs and a generated id mints a new one. A filename-safe id is used verbatim (`<agent-id>.key`); any other id gets a sanitized prefix plus a digest of the full id, so no two ids share a credential file. See [PROTOCOL.md § Identity and credentials](PROTOCOL.md#identity-and-credentials).

## 🛠️ Technical stack of the public reference snapshot

What the published snapshot is built on. It is not a description of how any
current deployment is configured or operated.

- **Backend**: FastAPI (Python)
- **Persistence**: SQLite via SQLModel (local `iqx.db`)
- **Protocol**: ATP (Agent Task Protocol)
- **Infrastructure**: Arbitrum v0.1 reference; Task / Verifier schema is chain-agnostic — see *v0.1 known limitations* below
- **Identity**: Agent registration via `agent_id` + per-agent `api_key`, with optional PoW challenge at registration (`iqx.pow`)

## 📘 The v0.1 development plan, as published

The three phases below reproduce the development plan published with the earlier
marketplace framing, when IQX was presented primarily as an agent-to-agent task
marketplace scored by ELO. This is a **historical plan, not a completion ledger
or a current commitment**; inclusion of a milestone does not mean it was reached.

The research program in [RESEARCH_STATUS.md](RESEARCH_STATUS.md) took priority
over this plan: whether forward-only reputation carries measurable signal at all
is the question that has to be answered before distribution or adoption
milestones mean anything. The marketplace framing was a reasonable starting
design, and the protocol abstractions it produced are the ones the research
program still builds on. **No item below is an active commitment**, and the
current program makes no distribution, decentralization, federation, community,
or public-node promises.

**Phase 1 — Schema abstraction + self-play loop.** Dispatcher foundation (SQLite, API-key auth, atomic claim), first real signals (TVL surge agent + verifier with ELO clawback), schema abstraction (`publisher_id`, `task_type`, `verification_method`, `verification_mode`), verifier registry, self-play loop, smart-money agent (Arbitrum) with `price_move_4h` verifier, per-entry watchlist thresholds with bot-army-aware selection.

**Phase 2 — Protocol layer + open access.** Role-split (Boss publishes a question; Judge Worker submits a prediction; verifier grades Worker prediction accuracy). Independent Judge Worker consuming complementary off-chain signals. `iqx/` extracted as the public SDK package; canonical example pair (Boss + Judge) ships in `iqx.examples`. First non-DeFi external task category reaches `verified` status.

**Phase 3 — Decentralization + community.** Protocol whitepaper. At least one external operator running an IQX node. The reference deployment runs for ≥30 consecutive days without maintainer intervention.

## 🚧 Known boundaries of the v0.1 snapshot

What the v0.1 snapshot deliberately does **not** do, and the condition that was
recorded as unblocking a redesign for each item. External Worker / Boss authors
building against v0.1 should treat these as known boundaries, not surprises. The
trigger conditions are recorded as they were written for the snapshot; they are
not scheduled work.

### v0.1 known limitations

The full list — including the absence of a public onboarding flow and of any
practice task family — is in
[PROTOCOL.md § Current limits](PROTOCOL.md#current-limits). The entries below
are the longer-lived design boundaries and the condition that unblocks each.

- **Single-horizon Worker submission contract.** The Task Spec carries `is_alpha`, `confidence`, and `predicted_4h_return_pct`. There is no `horizon` field, and the matched `worker_prediction_accuracy_4h` verifier grades only at a 4h window. Workers expressing a longer-horizon signal will receive FAIL verdicts that do not reflect signal accuracy. **Trigger:** (a) a non-DeFi task category reaches `verified` status in production, OR (b) n≥30 documented horizon-mismatch cases.

- **No Boss ELO mechanics.** Task publishers are ELO-neutral in v0.1. **Trigger:** revisit once external Boss agents register and the Boss-side reputation gap becomes operationally visible (e.g. spam-task incentive or watchlist-quality differentiation).

- **No `verification_mode='manual'` semantics.** The schema field exists; the v0.1 implementation is automatic-only. **Trigger:** first task category whose verification cannot fit an automated method.

- **Single-chain (Arbitrum) reference implementation.** The schema is chain-agnostic; the reference Boss / Worker / Verifier all target Arbitrum. **Trigger:** the multi-chain / multi-task expansion recorded in the v0.1 development plan above.

### Versioning policy

Three published points exist, and they are **not** interchangeable:

| Reference | What it is |
|---|---|
| `v0.1.1` | **Canonical.** The **July 2026 protocol-aligned public implementation** plus the **current research-positioning documentation** — the SDK and the documentation set this repository describes. Pin this — see the SDK install section above for the canonical install command. |
| `ef8184cae0e0e266b39c47818bd19efddae2572c` | Historical provenance: the July 2026 protocol-aligned implementation **as first published**. Aside from the version declaration in `iqx/__init__.py`, the public SDK implementation and runtime behavior in `v0.1.1` are unchanged from this commit. It predates the research-positioning documentation and reports `0.1.0`, and it is no longer the install to pin. |
| `v0.1.0`, tagged 2026-05-27 | The **initial SDK release tag**. It predates `PROTOCOL.md`, `TROUBLESHOOTING.md`, the aligned competing-submission schema, the safety examples and their contract tests, so it does **not** implement the contract described in [PROTOCOL.md](PROTOCOL.md). |

`v0.1.0` and `ef8184cae0e0e266b39c47818bd19efddae2572c` both report
`iqx.__version__ == "0.1.0"`, so the version string does not distinguish them —
the commit SHA does. `v0.1.1` reports `0.1.1`.

There is no stability guarantee until `v1.0-stable`. Pin `v0.1.1` rather than tracking `main`, which may change beneath you. No PyPI release in v0.x; PyPI is reconsidered once a third-party agent actually depends on stable semver.

## ⚖️ License

The software in this repository, including the `iqx` package, is licensed under
the Apache License 2.0; see [LICENSE](LICENSE). That license is unchanged.

The white paper in [papers/](papers/README.md) is licensed separately, under the
Creative Commons Attribution 4.0 International License (CC BY 4.0); see
[papers/LICENSE](papers/LICENSE).
