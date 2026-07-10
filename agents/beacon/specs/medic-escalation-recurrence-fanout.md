# Spec: Medic recurring-escalation → one self-retracting board card (Slice 9 fan-out)

**Status:** Draft — **PARKED, ship-ready.** Scope locked 2026-07-09. Not built: the motivating signal had gone quiet (see §1). The un-park trigger (§8) is already shipped and live.
**Author:** Larry-session Claude (drafted 2026-07-09)
**Approver:** Larry (parked this build on 2026-07-09; will authorize the un-park build when the gauge fires)
**Build gate:** dispatch the fan-out BUILD only after `medic_escalation_recurrence_gauge` (already shipped) DMs that a fingerprint has sustained ≥3 escalations in 7d with a fresh (≤2d) most-recent hit. Until then this is a parked spec, not a build.

## 1. Problem statement

Medic escalations are a large share of the 1063-alerts/14d, 97%-unactioned toil (memory `automated-work-audit-2026-06-28`). A *recurring* escalation — the same fingerprint paging Larry ~daily — is the worst kind: each page is individually irreducible ("Medic couldn't self-heal X"), but the N-th identical page carries no new information. Slice 9 (`scripts/medic_proposal_reconcile.py`, PR #900) already turns recurring **not-graduated** findings into one self-retracting board card. Escalations are the sibling class it does not yet cover.

**Why it was parked, not built (2026-07-09):** the two fingerprints that justified this build had already stopped. Live `~/agents/state/medic-handled-ledger.jsonl`: `forge-revision-preamble-discipline-001` (16 lifetime) last fired **2026-06-27**; `heal-stale-daemon-code:auto-restart-failed:ourliberty-outbox-notifier.service` (9 lifetime) last fired **2026-06-30**. Both root-caused before the build. The "~16×/2wk, ~7×/2wk" that motivated the task was a **trailing-edge burst** counted by a 2-week window drawn after the bursts had ceased — a dying burst still fat inside the window. Building a general fan-out to attack a signal already at low tide is premature; the right move is to arm a trigger and build when a fingerprint genuinely sustains.

## 2. Success criteria

- A recurring `outcome='escalated'` Medic fingerprint produces **exactly one** board card (source badge `medic`), not N repeat pages, and that card **self-retracts** when the recurrence clears.
- A **one-off** escalation NEVER produces a card. A genuinely-recurring one always does. (Antagonistically reviewed — §7.)
- Reuses every Slice 9 primitive already shipped: `emit_capture`, the `medic` source badge, the retract primitive + self-retract-when-cleared. No new alerting substrate.
- The durability gate is **count-over-days**, not the 2h-recency RECUR gate Slice 9 uses for not-graduated findings (which is tuned for a faster cadence and would miss ~daily escalations).

## 3. Users / consumers

- **Primary:** Larry — the card replaces the repeat pages on his Approvals/board surface.
- **Consumer:** the operator ranking brain (Slice 4, PR #844) ranks the card among other proposals. This spec was deferred until Slice 4 landed so the card ranks well — that precondition is now MET.

## 4. The durability gate (locked)

A fingerprint proposes a card when, over the recent window:

    count(outcome='escalated', this fingerprint, last 7d) >= 3   AND   most-recent escalation within 2d

- **Count-over-7d ≥ 3:** proves durability across the slow ~daily escalation cadence. 1 = one-off (never), 2 = coincidence (never), 3 = genuine recurrer.
- **Freshness ≤ 2d:** the clause that separates a live recurrer from a trailing-edge corpse (the §1 trap). A fat count with a stale most-recent hit is a dead/dying burst — excluded.

Thresholds are the shipped gauge's env-overridable constants (`OURLIBERTY_MEDIC_RECUR_WINDOW_DAYS` / `_MIN_COUNT` / `_FRESH_DAYS`); the fan-out MUST read the same values so trigger and action agree.

**Enforcement:** the shipped gauge `scripts/medic_escalation_recurrence_gauge.py` implements this exact gate (`qualifying()`), unit-tested in `scripts/tests/test_medic_escalation_recurrence_gauge.py` (one-off/coincidence never qualify; dead-burst excluded; fresh recurrer fires). The fan-out build reuses the same constants.

## 5. Scope (what's in) — the un-park BUILD

- Add a second propose class to `scripts/medic_proposal_reconcile.py` for `outcome='escalated'` findings, gated on §4 (count-over-days), parallel to the existing not-graduated RECUR class. Group the ledger by fingerprint; propose one card per qualifying fingerprint.
- Emit via `emit_capture` with label `medic-proposal` (existing) OR a new `medic-escalation` label. **If a distinct badge is wanted:** add `medic-escalation` to `CAPTURE_ALLOWED_LABELS` + `CAPTURE_MACHINE_RETRACTABLE_LABELS` in `scripts/dashboard_api.py`.
- Self-retract the card when the fingerprint drops below the §4 gate (mirrors Slice 9's stale-clear), so a cleared recurrence removes its card and a genuinely-later recurrence can propose a fresh one.
- Unit tests mirroring `scripts/tests/test_medic_proposal_reconcile.py` (propose, dedup, self-retract-on-clear, one-off-never-proposes, transport-error keep-and-retry).

## 6. Out of scope (deliberately not in)

- **Fixing the underlying failures.** The card collapses the *pages*; it does not root-cause the escalation. Per the NOTE on the original task: if only ONE fingerprint is loud, root-causing that specific failure directly is faster than the general fan-out — the gauge's DM says so explicitly.
- **Any change to Medic's escalation decision.** This is a board-surface reconciler, not a Medic behavior change.
- **The trigger gauge** — already shipped (§8); this build consumes its signal, it does not re-implement the gate.

## 7. Acceptance criteria

- [ ] A fingerprint with ≥3 escalations in 7d AND a fresh most-recent hit produces exactly one `medic`-badged card; a repeat run produces no duplicate.
- [ ] A one-off (and a 2× coincidence) never produces a card — asserted in tests.
- [ ] A dead/dying burst (fat count, stale most-recent) never produces a card.
- [ ] The card self-retracts once the fingerprint clears; a genuinely-later recurrence proposes a fresh card.
- [ ] If a distinct `medic-escalation` label is used, it is added to BOTH `CAPTURE_ALLOWED_LABELS` and `CAPTURE_MACHINE_RETRACTABLE_LABELS` (else the machine-retract path can't clear it).
- [ ] Tests: stdlib `unittest` (not pytest), sentinel-armed, zero live-tree writes (injected roots/tmp; never `~/agents`).
- [ ] Ship via `open_pr_for_team.sh` + `/code-review high` + `merge_reviewed_pr.sh`. `medic_proposal_reconcile.py` + `dashboard_api.py` are critical-path — HOLD at the deep-review gate (memory `critical-mission-build-review-rule`).

## 8. The un-park trigger — ALREADY SHIPPED

`scripts/medic_escalation_recurrence_gauge.py` (+ tests + `systemd/ourliberty-medic-escalation-recurrence-gauge.{service,timer}`, daily). A timer-driven, fail-open, kill-switchable gauge (sibling of `mirror_queue_wait_gauge.py`) that reads the Medic ledger, applies the §4 gate, and DMs Larry via `larry_alerts.append_alert` (per-fingerprint 7d cooldown, self-clear) the moment a fingerprint sustains. The DM names the fingerprint(s), their 7d count + freshness, and points here for the un-park. It is **timer-backed, not agent-cron** (memory `pulse-check-audit-2026-07-07`).

**Enforcement:** the gauge fires the signal; the daily timer + `heal_systemd_install_drift` (auto-discovers repo `systemd/` units) guarantee it stays installed; the parked-spec + memory `medic-escalation-fanout-parked` preserve the scope so the build ships same-day on trigger.

## 9. Open questions / risks

1. **Label choice** (shared `medic-proposal` vs distinct `medic-escalation`) — a UI/badge preference (values), decide at build. Distinct gives a cleaner board filter but costs the two `dashboard_api.py` allowlist edits.
2. **First-fire on an in-flight fix.** As of 2026-07-09 the gauge would fire once on `notifier-concurrent-scan-dup-review-dispatch-001` (3×/7d, ~42h) whose root fix is already in flight (memory `notifier-restart-dup-review-dispatch`). Correct positive; it self-clears once the fix lands. Not a reason to raise the threshold — it's the gauge working.

## 10. References

- `scripts/medic_proposal_reconcile.py` + `scripts/tests/test_medic_proposal_reconcile.py` (Slice 9, PR #900 — the not-graduated reconciler this extends).
- `scripts/mirror_queue_wait_gauge.py` (the gauge template the trigger was modeled on).
- `scripts/dashboard_api.py` (`CAPTURE_ALLOWED_LABELS` / `CAPTURE_MACHINE_RETRACTABLE_LABELS`).
- Operator Slice 4 rank brain (PR #844) — the deferral precondition, now met.
- Memory `medic-escalation-fanout-parked`, `automated-work-audit-2026-06-28`, `pulse-check-audit-2026-07-07`, `critical-mission-build-review-rule`.

## Changelog

- 2026-07-09 — Drafted + PARKED ship-ready by Larry-session Claude. Fan-out deferred (signal at low tide); un-park trigger gauge shipped live in the same PR.
