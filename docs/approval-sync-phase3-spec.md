# Approval-Sync Phase 3 — the "Needs You" surface

**Status:** Spec — 2026-07-01. Author-reviewed via a 4-agent combative review (see §Doctrine). Phased for the team.
**Parent:** `docs/approval-sync-north-star.md`. Phase 1 (#771), Phase 2/2.1 (#781/#790) shipped the canonical `resolve_decision` WRITE fan-out across the 4 stores. **Phase 3 is the READ mirror — solved at the SOURCE (author-at-emit), the same doctrine Phase 2 used for writes.**
**Risk tier:** 3a touches the approval/resolve machinery (`larry_alerts.py`, `for_larry_*.py`, `chain_event_emit.py`) — the deep-review critical fileset (#786/#794). 3a's build is hand-reviewed (`/code-review high`) before the team's second check, per the critical-mission rule.

## 0. Goal
The dashboard **Approvals tab** (name + URL `/approvals` unchanged — zero deep-link risk) becomes the single "Needs You" surface: every action needing Larry, one canonical count everywhere, no surface re-derives its own number, no alert-toil noise. Locked with Larry 2026-07-01: main list = approvals + clarifies + blocking escalations + stuck/paused sequences; NOW alerts in a separate section where **each is actionable**; fold the Where-are-we glance; one count incl. a nav badge; reuse the proven Missions card actions.

## 1. Doctrine — author-at-emit, not reconcile-at-read
`chain_events` is already ~70% the single substrate: the shipper emits `escalation`, `promote_alerts` emits a first-class `needs_attention`, sequences emit lifecycle events. Only **parked captures** and the **stuck/paused-sequence signal** aren't projected. Project those two and the dashboard reads ONE table — the deterministic Supabase PK absorbs double-emits for free, `read_at` is the one resolution identity, and the read-time dedup engine + two-clock count + resurrect class all disappear. Classification (`needs_larry`) and identity (`decision_key`) are stamped where the meaning is known — at emit. (A combative review rejected the merge-two-substrates-and-dedup-at-read alternative: the read-time dedup is inert because 3 of 4 droplet item types carry no join key; the anti-toil gate was backwards; the count was un-summable across a live source + a 10-min snapshot.)

---

## PHASE 3a — Backend classification (the core; invisible, load-bearing)
Makes `chain_events` the complete, correctly-classified, self-clearing "needs-you" substrate. Ships value alone (the count becomes trustworthy; noise is filtered at the source) while the existing tab keeps working. **Lands + soaks first, so the `needs_larry` tagging is proven on real data before any UI surfaces it — this is what makes the 2026-06-10 alert-toil replay impossible.**

### 3a.1 Project the two stragglers (event-driven, NOT the 10-min GC tick)
Two new emit sites via `chain_event_emit.emit_event` (copy the `promote_alerts` shape), firing on state transition so the count is live:
- `parked_capture` — when a capture enters `parked` (captures committer / `heal_missions_card_gc`).
- `sequence_needs_you` — when a sequence goes `paused` or a step is stuck >15 min (move the `load_waiting_sequences` logic, `system_state_log.py:312-392`, to emit-time).
Each row stamps at emit: `event_type`, coordinate/`task_id`, `decision_key` (`canonical_decision_key`), `needs_larry: bool` (§3a.2), `lane` (decide/steer/acknowledge).

### 3a.2 The anti-toil gate is PRODUCER-SIDE (the make-or-break)
A read-time "shows iff it carries an action" gate is a no-op — the ~101 benign healer alerts all carry `suggested_action` (245 emit sites). Two agent-core changes:
1. **`needs_larry: bool` (default FALSE)** on `larry_alerts.append_alert` + `for_larry_signal` records. Only genuinely irreversible/ambiguous emitters set it true (the alert-toil principle: only those reach Larry). The ~150 healer alerts never set it → they never reach the surface (they stay in the Healers tab, healer-owned). Scoped to the *few* blocking emitters, not all 158 call sites. Trust `promote_alerts`' existing `critical`+persistence bar; **do NOT re-classify at read time by `severity ≥ warning`** (that widens the gate below the producer's own bar).
2. **Close the retraction gap.** Today a healer auto-fix calls `resolve_alert`, which deletes `larry-alerts.jsonl` lines but never touches Supabase — so the row stays `read_at=NULL` forever and renders as live. `resolve_alert` (+ the healer auto-fix paths) must clear the READ substrate: set `read_at` on the correlated `chain_events` row (subject→task_id correlation exists, `chain_event_shipper.py:805`), OR route healer alerts through the self-clearing `for_larry_signal` store.

### 3a.3 One resolution identity
`chain_events.event_id` + `read_at` is the single read+write identity. `/api/larry/action` clears `read_at` (Phase 2's CAS) → the row leaves the pending set on the next poll. No cross-substrate join, no resolved-wins ambiguity, no 10-min resurrect — the class is gone because there is one store and one key.

### 3a.4 Tests (each closes a combative-review finding)
- A `critical` healer alert with `suggested_action`, auto-resolved, does NOT remain `read_at=NULL` (retraction test — fails today).
- A `needs_larry:false` alert never enters the needs-you query; a `true` one does.
- `parked_capture`/`sequence_needs_you` emit on transition and clear on resolve; no duplicate rows (deterministic-PK test).

---

## PHASE 3b — The "Needs You" UI (assembly on 3a's correct data)
Renders the classified substrate inside the existing `/approvals` tab. Reuses the proven, well-liked **Missions card actions**, not the killed Operator Action Queue.

### 3b.1 Lanes cut by ACTION-TYPE (so the badge means "only YOU can decide")
- **DECIDE** (drives the badge count): approval_request, clarify_request, clarify_exhausted, blocking escalations. Actions: **Approve / Reject / Delegate to team / Talk to team**.
- **STEER** (own count, NOT in the badge — reversible nudges): paused/stuck sequences, drafting missions. Actions: Resume / Cancel / Skip via the EXISTING `/api/system/build-sequences/{seq_id}/action` (these do NOT route through `/api/larry/action`). Plus Delegate / Talk.
- **ACKNOWLEDGE** (the actionable-alert section, `needs_larry`-gated): Dismiss / Delegate / Talk / Snooze.
- **PARKED** (calm backlog, 3c): via Snooze.

### 3b.2 Reuse the Missions actions (verdict from the live code)
- **Delegate to team** + **Talk to the team** — GENERIC, reuse as-is. Both write an envelope to Beacon's inbox and thread via `chain_events` keyed by item-id; `_CARD_KIND_META` is already abstracted across captures/missions/phases → add a `needs_you` card kind keyed on the item's `event_id`/`decision_key`. "Talk to team" auto-clears the doorbell on reply. This is the upgrade: a blocking escalation gets hand-back + ask-the-team, not just approve/reject.
- **Snooze** — reuse the date-picker UX; add a per-item snooze (or a generic `{item_id, item_type, snoozed_until}` handler). Drives the Parked lane.
- **Drop / Promote** — capture-specific endpoints; reuse the *button* JSX but map to the right verb per lane (Acknowledge→"Dismiss"=mark resolved; don't force "Promote" onto an escalation).
- **Component:** extract a generic `<ActionBar actions=[…] onRun>` from `CaptureActionBar`; the parent coordinator routes each verb to the correct endpoint.

### 3b.3 Never a false-empty; cheap badge
- The current Approvals list is fail-CLOSED (`Promise.all`→502→error card, not "all clear"); preserve that on the DECIDE leg.
- Badge value is `blocking_count: number | "unknown"`; any consumer renders `"unknown"` as a warning glyph (⚠), never numeric 0 on a partial read failure.
- `GET /api/needs-you/count` (Supabase `count:'exact', head:true`) so `Nav.tsx` doesn't pay the full list fan-out on every page; only the tab does.

### 3b.4 Tests
One-count divergence (badge == DECIDE count); partial-outage badge renders "unknown" not 0; a resolved item stays gone across a full poll cycle; the existing approve/reject/clarify flow unchanged; delegate/talk on a needs-you item threads + clears the doorbell like a mission card does.

---

## PHASE 3c — Consolidation cosmetics (deferred; lowest risk/value)
Retire the `/where-we-are` page (fold its itemized glance into the tab); rewrite the CEO chip's `decisions_waiting_count` to a live link reading `/api/needs-you/count`; the Parked-lane polish. No URL change.

---

## Reuse — honest accounting
SALVAGE: the 3 Supabase reader fns (`queryPendingApprovals`, `queryClarifyExhaustedPRs`, a rewritten actionable-alert query) + the `Promise.allSettled`/`source_errors` degraded-source shape; the Missions **Delegate/Talk** buttons + inbox/thread mechanism (generic); the Snooze date-picker.
DROP: the Operator Action Queue's `command` copy-paste field, `ActionCommandCopyButton`, `isNOWAlert` (the noise generator), the 6 flat row types. Render **buttons**, not copy-commands.

## Success criteria
One trustworthy count everywhere; a healer-reconcilable alert never appears (producer-gate + retraction); a partial outage shows "unknown", never a false all-clear; no resurrect/double-tap; delegate/talk work on needs-you items; the trusted approve/reject flow untouched.

## Out of scope
Beacon scoped read (Phase 4); the full alert-taxonomy overhaul (`needs_larry` is the minimal slice); direct-invocation for every steer verb (buttons for DECIDE + delegate/talk first).

## Doctrine + provenance
Author-at-emit, not reconcile-at-read (the Phase-2 FIX-1/FIX-2 lesson, enforced for reads). One substrate, one resolution identity, classification stamped at the source. Fail-open on the count (never a false all-clear), fail-closed on the write. 3a lands + soaks before 3b surfaces it, so noise is tuned out while invisible. Reuse the card actions Larry already trusts; don't resurrect the panel that was killed. This spec was hardened by a 4-agent combative review that broke the first draft on 5 counts (inert read-time dedup, backwards anti-toil gate, un-summable count, false-empty badge, stranded deep links); every §-level fix above traces to one of those findings.
