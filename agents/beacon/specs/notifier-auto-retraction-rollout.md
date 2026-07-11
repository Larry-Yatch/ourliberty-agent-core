# Notifier auto-retraction rollout + confidence-aware severity

**Pipeline-redesign Phase 2 · owner: Beacon (spec) → Forge (build) → Mirror (review)**

## Problem

The alert queue (`larry-alerts.jsonl`) is append-only. When a detector fires a
🔴 escalate alert and the condition later clears, nothing removes the stale
line — Larry's phone keeps a red alert that no longer reflects reality, for up
to the 14-day retention window. Slice-1 (PR #904) shipped the retraction
primitive `retract_with_standdown()` and wired 2 pilot heartbeat detectors,
but of ~104 alert producers only a handful retract. Separately, the
route/severity decision (`classify_route`) keys only on a healed-boolean +
significance — a detector's own *confidence* never influences whether it
escalate-DMs or lands quietly in digest, so low-confidence detections page
Larry as loudly as high-confidence ones.

## What already exists (do NOT rebuild)

- `larry_alerts.resolve_alert(key)` — retracts pending escalate line(s) matching
  the `source:subject` cooldown key, fixes consumer cursors, backs up, and
  retracts already-shipped chain_event rows. No-raise / fire-and-forget.
- `larry_alerts.retract_with_standdown(key, standdown_message, subject)` —
  retract + emit ONE visible `route='closure'` stand-down, gated on >=1 real
  removal. This is the generalized exemplar every clear-branch should call.
- `larry_alerts.resolve_alert_by_decision_key(key)` — decision-key variant.
- `larry_alerts.classify_route(source, subject, healed) -> escalate|closure|digest`;
  `append_alert(..., route=..., severity=...)`; a `critical` severity FORCES
  escalate; the graduation registry (`config/alert-graduation-registry.json`)
  can default a migrated source to `hold`.

## Phase 2a — retraction adoption rollout

**Rule:** every recurring detector that emits an escalate alert with a stable
`source:subject` AND has a detectable cleared-condition branch MUST call
`retract_with_standdown(key, standdown, subject)` on that branch, keyed
identically to how it emitted.

Design:
1. **Inventory.** Enumerate recurring escalate emitters (the `heal_*` family,
   notifier suppressions, advancer/shipper detectors). For each: source,
   subject-shape, and whether a cleared-condition branch exists.
2. **Adoption registry.** Add `config/alert-retraction-registry.json` listing
   each recurring escalate `source` (or `source:subject` shape) with state
   `retracts: true` (wired) or `exempt: "<reason>"` (one-shot / no cleared
   condition / retracts via a different primitive).
3. **Guard test.** `scripts/tests/test_retraction_adoption.py` statically scans
   detector modules for escalate-emitting calls and FAILS if a `source` that
   emits an escalate alert is neither wired to a retraction call nor listed
   exempt in the registry.
4. **Wire the unwired.** Add the `retract_with_standdown` call to each unwired
   detector's clear-branch.

**Enforcement:** `test_retraction_adoption.py` fails the gate for any recurring
escalate `source` missing from the registry or lacking a retraction call.

## Phase 2b — confidence-aware severity

**Rule:** a detection's confidence gates its route. Low-confidence detections
route to `digest` (no DM), never `escalate`, regardless of significance;
medium/high preserve current behavior; `critical` is never low-confidence by
construction and always escalates.

Design:
1. Add optional `confidence: Optional[str]` ('low'|'medium'|'high', default
   None == high) to `append_alert`; thread into route derivation: when
   `confidence == 'low'` and `severity != 'critical'`, force `route='digest'`.
2. Extend `classify_route(source, subject, healed, confidence=None)`: low
   confidence -> 'digest'.
3. Detectors that already reason about confidence (e.g.
   `heal_wedged_review_sessions` confidence ladder) pass ladder state through
   as `confidence`, so low-confidence rungs digest instead of DM.

**Enforcement:** unit test asserting low-confidence non-critical -> digest, and
that `critical` ignores confidence.

## Slices (build sequence, authored after this spec lands)

- **Slice A (Phase 2a):** registry + guard test + wire the recurring detectors.
- **Slice B (Phase 2b):** confidence param + classify_route thread + detector
  adoption. Serialize behind A: both edit `larry_alerts.py`, so ordering them
  avoids merge churn on the shared file (a real file-overlap dependency, not
  over-conservative sequencing).

## Success criteria

- Every recurring escalate `source` either retracts on clear or is
  registry-exempt; guard test green.
- A cleared condition removes its 🔴 within one detector cycle and emits exactly
  one closure stand-down.
- Low-confidence non-critical detections land in digest, not a DM; criticals
  unaffected.
- No new orphan alerts; the pilot retractions from PR #904 stay unchanged.

## Out of scope

- The 14-day retention GC (unchanged).
- Reworking the graduation registry / hybrid `hold` gate.
- Per-producer message wording beyond the standdown one-liner.
