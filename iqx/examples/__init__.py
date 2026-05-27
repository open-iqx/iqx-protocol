"""IQX SDK — runnable example agents.

Canonical Boss / Worker / dual-role examples external operators can copy
and modify:

- ``iqx.examples.boss_smart_money`` — Boss-only smart-money cluster
  monitor. Exposes ``SmartMoneyConfig`` and the thin ``SmartMoneyBoss``
  class for SDK consumers.
- ``iqx.examples.worker_judge`` — independent Judge Worker that submits
  structured verdicts against smart-money Boss tasks, informed by
  optional off-chain signal caches.
- ``iqx.examples.baseline_worker`` — reference Worker that ships with
  the SDK. Defaults to auto-claiming toy ``echo`` tasks only; the
  ``defi_alpha`` ``worker_prediction_accuracy_4h`` shape (with a
  deterministic default-skeptical verdict) requires explicit
  ``--methods`` opt-in so the baseline does not steal live prediction
  tasks from smarter Workers under single-claim semantics. The
  out-of-the-box counter-party for external Boss authors and the
  accuracy floor a future replay benchmark scores against.
- ``iqx.examples.self_play`` — dual-role demo exercising the
  ``publisher_id != worker_id`` codepath via the ``echo`` verification
  method.
"""
