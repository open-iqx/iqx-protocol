"""IQX SDK — runnable example agents.

Every example declares exactly one side-effect class (see
``iqx.examples.identity.SideEffect``), stated in its module docstring and in
its ``--help`` output:

- ``iqx.examples.boss_smart_money`` — **Boss / task publishing**,
  operator-oriented. Boss-only smart-money cluster monitor. Exposes
  ``SmartMoneyConfig`` and the thin ``SmartMoneyBoss`` class for SDK
  consumers. Public Boss onboarding is not offered.
- ``iqx.examples.worker_judge`` — **Worker registration / submission**.
  Independent Judge Worker that submits structured verdicts against
  smart-money Boss tasks, informed by optional off-chain signal caches.
- ``iqx.examples.baseline_worker`` — **Worker registration / submission**.
  Reference Worker that ships with the SDK. Defaults to claiming toy ``echo``
  tasks only; the ``defi_alpha`` ``worker_prediction_accuracy_4h`` shape (with
  a deterministic default-skeptical verdict) requires explicit ``--methods``
  opt-in so the baseline does not take live prediction tasks from smarter
  Workers under single-claim semantics. It is also the accuracy floor the
  replay benchmark scores against.
- ``iqx.examples.self_play`` — **Boss / task publishing**, operator-oriented.
  Dual-role demo exercising the ``publisher_id != worker_id`` codepath via the
  ``echo`` verification method.
- ``iqx.examples.identity`` — not an agent: the shared identity resolver and
  write safeguards the examples above use.

**All four agent examples use the legacy single-claim path**
(``/claim`` → ``/submit``), which is not the lifecycle a competing-submissions
Worker uses. See ``PROTOCOL.md`` for the current lifecycle before copying one
as a starting point.

Agent ids are resolved per run and default to a freshly generated value, so
two consecutive runs never collide on an already-registered id. Writing to any
non-loopback node requires an explicit opt-in — see
``iqx.examples.identity.guard_writes``.
"""
