# Research status

What IQX is currently studying, how it is being measured, and which claims are
explicitly **not** being made.

**Related:** [README.md](README.md) · [White Paper v0.1](papers/README.md) · [PROTOCOL.md](PROTOCOL.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

This document is written to stay accurate as the research continues. It carries
no sample counts, point estimates, intervals, test statistics, or completion
dates: those belong in a frozen readout published after the fact, not in a
document that would silently go stale. The frozen readout of the V3.3 evidence
cut is published in [White Paper v0.1](papers/README.md). Nor does this
document describe deployment, scheduling, or operational state.

## 1. The subject

IQX studies **forward-only reputation and trust routing for AI agents**: whether
outcome-backed operating history, accumulated under strict identity and evidence
discipline, can support a later decision about which agent to trust with a task.

The unit of evidence is a forward commitment. A publisher creates a falsifiable,
time-bounded task. An identified predictor commits a structured answer **before**
the outcome exists. After the horizon passes, a declared verifier grades that
answer against the realized outcome. In the research design, only settled
outcomes count as performance evidence: an ungraded submission does not support a
scientific claim, whatever a rating column may show in the meantime.

## 2. Method

### 2.1 Forward-only evidence

The answer is fixed while the outcome that grades it is still unknown. This
removes one degree of freedom that retrospective evaluation cannot remove: the
answer cannot be constructed, selected, or tuned after its outcome is known. It
does not make a study valid on its own — task selection, verifier quality, class
balance, identity discipline, and operator integrity all still matter.

Forward-only is a property of the evidence boundary, not a claim of
immutability. Records kept in a mutable store can be edited; credibility is
cumulative and rests on mechanisms such as content-addressed captures,
append-oriented journals, version and cohort binding, preserved historical
schemas, and externally observable outcomes.

### 2.2 Frozen identity and versioning

The evaluated subject is a specific predictor at a specific code and
configuration version — not a model brand and not a human-readable name. A
material change to the model, prompt, tools, features, thresholds, gates,
configuration, or executable artifact creates a **new** evaluated subject, which
enters under a new identity and a new activation boundary.

The consequence is deliberate and costly: a repair that changes answers cannot
inherit the history of the predictor it repaired. Evidence stays attached to the
version that produced it. It is never rescored into a changed predictor, and a
changed predictor never starts with borrowed support.

### 2.3 Evidence boundaries and historical semantics

Two different bindings carry the boundary, and they live in different places.

An **answer and its bundle** are bound to the capture they were produced from and
to the schema under which they were written. A stored artifact keeps the semantics
of the schema that wrote it: a newer build may verify old evidence, but may not
reinterpret it under current conventions, and may not re-derive its digests. Old
evidence remains old evidence.

The **forward activation boundary and the enrolled predictor identities** are
recorded separately, in durable cohort and authority metadata. That separation is
deliberate: the boundary is a property of the cohort an answer belongs to, not a
self-asserted field on the answer, so it cannot be restated by whatever wrote the
answer.

### 2.4 Preregistration

Before any eligible outcome from the evaluated run is observed, the following are
written down and frozen: the task family and horizon, the pass rule, the primary statistic, the uncertainty
procedure, the baseline and sentinel set, the population over which each
comparison is computed, the exclusion categories and their counters, the minimum
support required before any claim may be made, the smallest effect worth caring
about, and the ordered ladder of claims the evidence may support.

Freezing these in advance is what stops a result from being selected after the
outcomes are known. A readout that deviates from the frozen rules reports the
deviation rather than the result.

### 2.5 Why raw-accuracy ELO is insufficient

The v0.1 protocol ([PROTOCOL.md](PROTOCOL.md)) rates agents with an ELO update
driven by a raw pass/fail verdict. Raw accuracy is not a measure of
discrimination. Where one outcome class is rare, a majority-class predictor can
reach strong accuracy — and so a strong rating — while separating no two cases:
what it measures is the task stream's base rate, not the predictor.

Class-balanced informedness makes that visible:

> **J = TPR + TNR − 1**, equivalently **J = TPR − FPR**

A constant predictor scores **J = 0** whenever both outcome classes are
represented — exactly, at any base rate, per realization and not merely in
expectation. Positive **J** means the predictor separates the classes better than
a constant does; negative **J** means its direction is systematically harmful.

The research program therefore treats raw-accuracy ELO as insufficient as a
discrimination metric, and has moved to class-balanced, preregistered evaluation.
Two rules follow:

1. **Constant baselines are mandatory, not decorative.** Any claim about
   discrimination is stated against explicit constant and direction-conditioned
   baselines, evaluated over the same answers the compared predictor actually
   answered — a baseline scored on a different question set is not a comparison.
2. **Enough evidence is retained to recompute the metric.** Confusion matrices
   are reconstructible from settled answers, so a scoring rule can be re-examined
   after the fact rather than trusted because it ran.

The offline replay benchmark in the [public reference snapshot](README.md) shows
the arithmetic on published data: its dataset has a balanced outcome split and
the shipped reference Worker answers identically on every record, so its 50%
accuracy carries **J = 0**.

## 3. The paired experiment

**V3.3 is the paired experiment whose evidence cut White Paper v0.1 reports.**
Its domain is a delayed-outcome market, which is used because outcomes are
externally observable, timestamped, and inexpensive to verify — not because
market prediction is the point.

Two predictors receive the same task and share the same recorded,
content-addressed market capture:

- a **market-only** arm, and
- a **wallet-aware** arm, identical to it except for one wallet-derived term.

Pairing on the same task and market capture is what lets the wallet term be
isolated as the treatment, and what makes a common-mode failure diagnosable. Both
arms are frozen, separately identified, and activated behind an explicit forward
boundary.

The experiment asks two ordered questions:

1. **Is market-only information learnable?** Does the market-only arm
   discriminate better than a constant baseline on forward tasks?
2. **Does wallet-derived information add incremental value** over that
   market-only baseline, under the preregistered paired estimand?

Every outcome is scientifically meaningful and publishable, including a null
result: both arms may show discrimination with or without a wallet increment, the
wallet term may prove harmful in the form tested, or neither arm may discriminate
at all. A null wallet effect would be a result about the wallet term as tested,
not about forward evaluation.

**Status.** The evidence for White Paper v0.1 ends at a frozen cut after three
epochs. The cut was closed by an administrative decision, not by the
preregistered stopping rule, which would have continued; the paper states the
truncation and its consequences. The cut's readout is frozen and archived, and
under the preregistered rules it concludes:

- **Q1:** learnability is **not established**, and no capability tier is claimed.
- **Q2:** the wallet increment is **inconclusive** — not demonstrated, which is
  not evidence of absence.

Predictions made after the cut fall outside it. They are not pooled into it and
do not change its conclusions.

The conditions that authorize the experiment's scientific claims were fixed
before any eligible outcome from this run was observed. They gate claims on realized support rather than on elapsed time or
answer volume — minimum realized minority-class outcomes per arm before an
estimate may be quoted, a higher minimum before an interval claim, a minimum
settled-answer count before a predictor may be ranked at all, and stability
across disjoint settled epochs.
A separate degeneracy condition voids the wallet contrast in either direction
when the two arms rarely disagree, or when the answers concentrate in too few
sources for the contrast to mean anything. Which individual conditions the cut
satisfied is not restated here; the paper reports them.

Outside a frozen readout, interim observations are not findings.
They are not published here, and a favorable-looking interim number is not
evidence of skill.

## 4. The claim ladder

Three claims, progressively stronger, that must not be conflated:

| Claim | Question | Status |
|---|---|---|
| **Skill** | Does a predictor discriminate better than a constant baseline on forward tasks in this family? | Under study; not established at the V3.3 cut |
| **Predictive reputation** | Does an estimate frozen on earlier tasks predict performance on later, unseen tasks in the same family? | Not established |
| **Routing value** | Does choosing an agent or reviewer by that reputation improve correctness, severe-failure rate, cost, or latency against a declared routing baseline? | Not established |

Only the first is under active empirical study. The long-term thesis — a
machine-consumable trust and attention-routing layer that agents can query
before delegating, cross-checking, or escalating work — depends most strongly on
the third, which has not been tested.

An ordered ladder of weaker-to-stronger conclusions also governs what a single
run may claim, from "the predictor operated and its answers parsed" through
"it beat every baseline on its own answers" to "it did so with intervals clear of
the baselines, stably, across disjoint epochs." Only the top of that ladder
licenses the sentence *this agent has skill on this task family*.

## 5. Claims not being made

Stated explicitly, because absence of a disclaimer reads as a claim:

- **No market alpha.** IQX does not claim that market alpha exists, that it has
  found any, or that any predictor here is profitable. Nothing in this repository
  is investment advice.
- **No learnability claim.** It is not established that market-only information
  is learnable in this task family.
- **No wallet-signal claim.** It is not established that wallet-derived
  information adds incremental value, and the wallet-aware arm is not claimed to
  work. Neither is it claimed not to.
- **No capability claim.** No agent evaluated here is claimed to have
  demonstrated skill on this or any task family.
- **No predictive-reputation claim.** It is not established that a reputation
  estimate predicts later unseen performance.
- **No trust-routing claim.** IQX has not demonstrated successful trust routing,
  and no routing experiment has been run.
- **No final claim authorized.** The frozen, archived readout of the V3.3 cut
  establishes none of the scientific claims above, and its administrative
  closure is not the preregistered stopping event.
- **No attestation claim.** The system operates under a trusted-operator and
  trusted-host assumption. Content hashes, artifact manifests, clean checkouts,
  and authority records give provenance and detect classes of accidental drift.
  They are not hardware attestation and do not defend against a malicious
  operator or a compromised runtime.

## 6. What would weaken the thesis

A research program is only one if it names what would count against it. The
broad thesis would be materially weakened if corrected reputation failed to
predict later performance across suitable task families; if routing by
reputation improved no relevant outcome once cost and latency were included; if
agent errors proved so correlated that independent verification was unavailable
in practice; if version changes made useful history too short-lived to guide a
decision; if reliable verification routinely cost more than doing the work; or
if forward evidence could not be made credible against operator discretion.

A single predictor generation showing no discrimination does not falsify the
protocol. It falsifies one predictor–task–verifier combination.

## 7. What this repository publishes

The [current public reference snapshot](README.md) — the v0.1-era protocol
specification, the reference SDK, a small offline replay benchmark, and a local
development node with synthetic tasks ([QUICKSTART.md](QUICKSTART.md)) — plus
this statement of method and status, and [White Paper v0.1](papers/README.md),
which is licensed separately, under CC BY 4.0.

Beyond what the white paper reports about the experiment's design and results,
it does not publish the research system's code, configuration, deployment,
schedules, data sources, wallet identifiers, datasets, or runtime artifacts. The
offline replay benchmark shipped here demonstrates mechanics and reproducibility
on a small retrospective dataset; it is not evidence of predictive skill, it is
not a production measurement, and it is unrelated to the paired experiment above.
The same holds for the local node: its outcomes are synthetic, generated by
whoever runs it, and it shares no data, identity or predictor with the research
system.

The V3.3 readout is published at an administrative cut, with the truncation
stated; it does not claim that the preregistered stopping rule was met. A null
or negative result remains publishable, and would be reported as a result.
