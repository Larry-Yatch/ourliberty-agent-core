# Spec: Structured gap-log field on sequence files

**Status:** Draft awaiting design pass
**Author:** Forge (stub from operator-UX backlog, 2026-05-28)
**Parent registry entry:** `agents/beacon/missions.json#operator-ux-gap-log-field`

---

## 1. Purpose

Add `gap_log: [{ts, severity, finding, surfaced_by}]` field to build-sequence schema. Verifier specs (bootstrap-N) populate it during run. Missions tab renders the log; cross-sequence search becomes possible.

Today, gaps surfaced mid-sequence (the bootstrap-003 skip-window finding is the canonical example) live in scattered places: the verifier's PR body, Larry's memory, an ad-hoc DM. There is no structured per-sequence record, so cross-sequence patterns (e.g. "the last 3 sequences all surfaced a notification-coverage gap") are invisible.

---

## 2. Sketch

- New optional field on the sequence-file schema (PR-S2's `build_sequence_validator.py` REQUIRED_SEQ_FIELDS stays unchanged; gap_log is additive):
  ```json
  "gap_log": [
    {"ts": "2026-05-28T13:42:00Z", "severity": "medium", "finding": "Message 2 unreachable without notification", "surfaced_by": "bootstrap-003-verifier"}
  ]
  ```
- Verifier specs gain a convention: each verification finding that is NOT in scope for the current sequence appends a gap_log entry via the sequence shortcut helpers (new helper `apply_gap_log(seq_id, severity, finding, surfaced_by)`).
- Missions-tab card side-panel renders the log as a chronological list, color-coded by severity.
- A new search endpoint `GET /api/system/gaps?since=<iso>&severity=<level>` returns gap_log entries across all sequence files, so Pulse Check IX (sibling) can aggregate them.

---

## 3. Open questions

- Should gap_log entries auto-propose missions.json entries when severity ≥ medium, or stay informational until an operator promotes them?
- Severity taxonomy: reuse the alert tier (NOW/SOON/FYI) or introduce a separate scale (low/medium/high)? Reusing the tier avoids drift but conflates two concepts.
- Does the gap_log entry need a `resolved_by: <task_id>` back-reference once the gap is addressed in a later mission?
- Migration: do existing closed sequence files get a back-filled empty `gap_log: []`, or only new sequences?

---

## 4. Acceptance (rough)

- `build_sequence_validator.py` accepts (and round-trips) sequence files with and without the gap_log field.
- A verifier dispatched against a synthetic gap produces a gap_log entry visible on the corresponding Missions-tab card.
- The cross-sequence search endpoint returns the same entry given a matching `since` window.

---

## 5. Estimated cost + sizing

Schema + validator update + helper + API endpoint + side-panel render: ~$10–12. One PR. Mirror revisions expected 1 (schema changes attract review). Sizing: medium; the design work is severity taxonomy + resolved_by semantics.
