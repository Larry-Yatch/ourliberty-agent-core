# Spec: Notifier auto-retraction — recurring detectors clear their own reds

**Status:** Slice 1 shipping (mechanism + 2 pilots); slices 2–3 planned
**Author:** Claude-as-Forge (dispatched by Beacon, `notifier-auto-retraction-slice1-001`)
**Related:** `heal_systemd_install_drift.py` (the retraction exemplar); `alert-pipeline-rework.md` (severity→route model this reuses)

---

## 1. Problem statement

The `larry-alerts.jsonl` queue is append-only: it has no retraction primitive.
When a recurring detector (a healer / sentinel) emits a 🔴 escalate line and the
underlying condition later resolves out-of-band, the original red stays in the
queue until 14-day retention prunes it. Today exactly **one** of ~40 recurring
detectors — `heal_systemd_install_drift` — retracts its own red when it observes
the condition clear. Every other resolved issue can leave a lingering 🔴 in the
feed for up to two weeks, eroding the signal value of the queue.

The retraction machinery already exists (`larry_alerts.resolve_alert`); what is
missing is (a) an *auditable* wrapper so a retraction of something Larry saw is
itself visible, and (b) adoption across the detectors whose clear-state is a
safe, positive observation.

## 2. The three-slice program

### Slice 1 — mechanism + 2 pilots (THIS PR)

- New helper `larry_alerts.retract_with_standdown(key, standdown_message, subject=None) -> int`
  (see §3). Makes retraction auditable and never silent.
- Pilot adoption on two heartbeat healers whose clear-state is a POSITIVE
  observation (a recent timestamp): `heal_chain_event_shipper_heartbeat.py` and
  `heal_build_sequence_advancer_heartbeat.py`. Each calls the helper on its
  positively-fresh branch (`reason == 'fresh'`), keyed on its own
  `source:subject`.
- This canonical spec doc (the reference for slices 2–3).

### Slice 2 — classification audit + expansion (separate dispatch)

Audit all ~40 recurring detectors and classify each as:

- **Retractable, single-subject shape** — one detector, one subject; clear-state
  is a single positive observation (like the two pilots). Expand to this batch.
- **Retractable, set-diff shape** — the clear-state is a *set difference* over a
  live inventory (the `heal_systemd_install_drift` `live_set` pattern: a unit
  left the drift set). Needs the per-member diff loop; deferred within slice 2.
- **One-shot / non-retractable** — fires once on an irreversible or
  point-in-time event with no recurring "now-clear" observation. Excluded.

Then adopt the helper across the structurally-safe single-subject detectors.

### Slice 3 — confidence → severity threading (separable, deferrable)

Thread each detector's detection confidence into its `severity` argument so a
low-confidence detection routes to the digest lane instead of escalate.
`append_alert(..., severity=, route=)` **already** implements the routing
(`severity == 'info'` defaults to digest; `critical` forces escalate) — so slice
3 is an ADOPTION task per detector, not a missing mechanism. Slice 1 does **not**
touch severity routing.

## 3. The helper (slice 1)

```python
retract_with_standdown(key, standdown_message, subject=None) -> int
```

Beside `resolve_alert` in `scripts/larry_alerts.py`. Behavior:

1. Calls `resolve_alert(key)` (removes pending escalate line(s) whose cooldown
   key == `key`, under the queue flock, keeping beacon+medic cursors consistent
   and clearing the shipped `chain_event` rows).
2. **Only when** `resolve_alert` removed ≥ 1 line — proof a real 🔴 had been
   delivered — appends exactly ONE closure stand-down line via
   `append_alert(severity='info', route='closure', ...)`.
3. On a 0-removal no-match, appends nothing and returns 0.
4. Never raises (fire-and-forget, matching `resolve_alert` / `append_alert`).

This generalizes the `heal_systemd_install_drift` exemplar
(`_resolve_install_alert` → retract, then a closure DM gated on `removed`) so any
positive-clear detector can adopt one call.

## 4. Guardrails (the review contract)

- **POSITIVE-CLEAR ONLY.** A detector retracts solely on a positively-observed
  resolved condition — a fresh timestamp, a member leaving a drift set — NEVER on
  absence-of-signal or a degraded / unreadable probe. This inverts
  `heal_pipeline_stall`'s "unreadable == non-terminal == still alertable"
  degrade-safe posture for the clear side. In the pilots this is enforced by
  gating the retract on `reason == 'fresh'`: `check_staleness()` returns
  `is_stale=True` on every error path, so a degraded read can never reach the
  retract call, and the positive sentinel makes the invariant local + testable.
- **AUDITABLE, NEVER SILENT.** Every retraction that actually removed a pending
  red emits a closure stand-down line, so a wrongful retraction surfaces as a
  disputable closure DM rather than a silently vanished red.

## 5. Acceptance criteria (slice 1)

- `retract_with_standdown`: returns 0 and appends nothing on no-match; on a
  seeded pending escalate line matching the key, removes it and appends exactly
  one closure line; never raises.
- Each pilot healer: (a) a positively-fresh tick retracts a seeded stale alert
  for its key; (b) a probe-error / degraded-read tick does NOT call retract
  (REQUIRED, not optional).
- Existing behavior unchanged: the stale-emit branches of both pilots are
  untouched; the full suite stays green.

## 6. Out of scope for slice 1

- Any detector beyond the two named pilots (slice 2, gated on the audit).
- Confidence → severity threading (slice 3).
- Any change to `resolve_alert`'s semantics, the cooldown/route model, or the
  digest pipeline.
- Set-diff-shaped detectors (the `live_set` diff pattern) — deferred to slice 2.
