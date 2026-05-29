# Spec: CLARIFY round visibility — full Forge↔Beacon Q+A on Missions cards

**Status:** Draft (awaiting Larry approval)
**Author:** Claude-as-Forge (written 2026-05-29, after Larry's design pass surfaced the answer-side gap)
**Approver:** Larry (pending)
**Supersedes:** [`agents/beacon/specs/operator-ux-clarify-visibility.md`](operator-ux-clarify-visibility.md) — that stub only contemplated the *question* side. This spec covers the full round-trip.
**Parent registry entry:** `agents/beacon/missions.json#clarify-round-visibility`
**Related missions:** `missions-tab-v1` (provides the surface), `e4-4g-operator-action-queue` (sibling Missions-tab work)

---

## 1. Purpose

Render the full Forge↔Beacon CLARIFY round-trip (questions AND answers) on the Missions tab side-panel for each task that hit one or more clarification rounds. Today operators can only learn round-trip content from the droplet log; this surfaces it on the dashboard.

The question side already lands as `clarify_request` rows in `chain_events` (per `scripts/chain_event_shipper.py:116`). The answer side lives only on the droplet at `~/agents/outboxes/beacon/.archive/notify-*.json` — there is no API surface and no way for the dashboard to render the round-trip. Larry's 2026-05-29 design pass surfaced this gap: the stub spec `operator-ux-clarify-visibility.md` quietly assumed the answer side could be reconstructed from the existing data plane, which it cannot. This spec closes the gap end-to-end.

---

## 2. Locked decisions

- **(a) Read-only v1.** No operator-intervention affordance in this round. Rendering only; intervene-from-dashboard is explicitly deferred (see § 8).
- **(b) Two-step sequence implementation.** Data plumb (agent-core `chain_event_shipper`) before render (dashboard). Step 2 depends on step 1 — the dashboard cannot render answers until they exist as `chain_events` rows.
- **(c) `chain_event_shipper` extension is the data path.** No new droplet endpoint, no new Supabase table, no direct outbox-archive scrape. The shipper is already the canonical writer for Beacon-originated events; extending it keeps one path in/out.
- **(d) Read-only API contract.** The dashboard does NOT query droplet outbox archives directly. Everything flows through `chain_events` rows (existing `clarify_request` + new `clarify_response`). This preserves the public-dashboard-safe contract: the outbox-archive directory contains raw envelope payloads that are not redaction-scrubbed, while `chain_events` rows go through the shipper's `_REDACT_KEY_SUBSTRS` filter.
- **(e) No backfill of historical rounds.** Spec applies from this PR forward. Historical CLARIFY cycles remain visible only in the droplet log. Backfill would require a one-shot replay of archived `notify-*.json` outboxes through the shipper; not worth the complexity for the v1 surface.

---

## 3. Scope

**In scope (v1):**
- Rendering Q+A pairs chronologically on the Missions-tab side-panel for any task with ≥1 CLARIFY round.
- Per-round threading: each round shows the question, then the matching response, both timestamped and avatar-tagged.
- CLARIFY-exhausted state badge (terminal red banner when the budget exhausted before a PROCEED).
- Graceful empty state for tasks with zero rounds (panel hidden, not "no clarifications" placeholder).

**Out of scope (v1):**
- Editing past responses.
- Replying from the dashboard (the v2 "intervene" affordance from the stub spec).
- Deleting rounds.
- Alerting on long-running unanswered rounds (the Operator Action Queue covers that lane separately per `e4-4g-operator-action-queue.md`).
- Real-time WebSocket updates — v1 polls on the same 30s cadence as the existing chain timeline.

---

## 4. Acceptance

- Visit Missions tab → click a card whose task had ≥1 CLARIFY round → side-panel shows each round as `Q (Forge, ts) → A (Beacon, ts)` pairs, in chronological order.
- CLARIFY-exhausted tasks (e.g. budget exhausted without PROCEED) show a clear terminal badge in the drawer.
- Tasks with zero rounds render no drawer section (empty state is invisibility, not a placeholder).
- A round where Beacon's answer hasn't arrived yet (request without matching response) renders the question in an "awaiting Beacon" state.

---

## 5. Data model

### 5.1 Existing — `clarify_request` chain_event

Already in `KNOWN_EVENT_TYPES` per `scripts/chain_event_shipper.py:116`. Emitted today by the existing shipper outbox-scanning loop when Forge writes a `notify-*.json` envelope with `intent=clarify`. Payload includes `task_id`, `round_number`, `question_text`, and the standard event metadata.

### 5.2 New — `clarify_response` chain_event

Add `clarify_response` to `KNOWN_EVENT_TYPES` in `scripts/chain_event_shipper.py:92` (the frozenset definition; `clarify_request` lives there today at line 116 as the precedent).

Payload contract:

```
{
  "task_id":            "<envelope task_id, kebab-case>",
  "round_number":       <1-indexed integer matching the clarify_request round_number>,
  "response_text":      "<verbatim Beacon answer body, redaction-scrubbed by shipper>",
  "beacon_session_id":  "<optional — present if the envelope carried session metadata>",
  "responded_at":       "<ISO-8601 UTC timestamp of the outbox write>"
}
```

Emission: the shipper recognizes Beacon outboxes whose `prompt` field begins with `[Inter-agent notify | intent=clarification-response`. The canonical intent-classification pattern lives in the outbox notifier code today; Forge will grep for the literal `intent=clarification-response` string at build time to confirm the exact prefix shape used downstream.

`round_number` extraction: the cleanest source is the `--resume` reference embedded in the prompt (the response envelope `--resume`s against Forge's preflight session, which carries the round counter). The build-step Forge will pick the canonical extraction point during preflight; the fallback is the resume-file naming convention. Both surfaces exist today.

`response_text`: extracted verbatim from the `result` field of the notify envelope, then passed through the shipper's existing `_REDACT_KEY_SUBSTRS` filter (so any accidental credential leak in the answer body is stripped before INSERT).

### 5.3 Join semantics

Dashboard query helper joins `clarify_request` ∪ `clarify_response` rows by `task_id`, sorts by `ts`, then groups into rounds keyed on `round_number`. The output shape consumed by the renderer:

```
[
  { round: 1, question: { text, ts }, answer: { text, ts } | null },
  { round: 2, question: { text, ts }, answer: { text, ts } | null },
  ...
]
```

A request row without a matching response row renders as `answer: null`, surfaced as the "awaiting Beacon" state in the drawer. A CLARIFY-exhausted task is detected by joining against the existing `preflight_reject` chain_event with `reason ~= /clarification budget exhausted/i`; on hit, the drawer shows the terminal red banner.

---

## 6. Step 1 — agent-core `chain_event_shipper` extension

**Goal:** make Beacon `clarification-response` envelopes land as `clarify_response` rows in `chain_events`.

**Concrete changes:**

- **Add `clarify_response` to `KNOWN_EVENT_TYPES`** (`scripts/chain_event_shipper.py:92`). Position alphabetically near `clarify_request` (line 116) for readability; the comment block above the frozenset already explains why types are listed there.
- **In the shipper's outbox-scanning loop**, recognize Beacon outboxes whose `prompt` field starts with `[Inter-agent notify | intent=clarification-response`. Forge will grep the existing intent-classification code (the outbox notifier) for the canonical prefix during preflight to ensure exact-string match.
- **Extract `round_number`**: derive from the `--resume` session reference in the prompt, falling back to the resume-file naming. Forge to determine the cleanest extraction point during preflight; both surfaces exist today and the right one is whichever currently carries the round counter farthest into the envelope.
- **Extract `response_text`**: from the `result` field of the notify envelope. Pass through `_REDACT_KEY_SUBSTRS` (shipper-existing behavior; no new redaction logic).
- **Emit a `clarify_response` chain_event row** with payload per § 5.2 and the standard event metadata (event_id deterministic via `sha1(task_id + event_type + ts)` per existing shipper convention).
- **Idempotency**: the deterministic event_id is the PK in `chain_events`; reprocessing the same outbox is a clean no-op. No new dedup logic needed.

**Test coverage:**

- Fixture outbox with `intent=clarification-response` → assert exactly one `clarify_response` row emitted with correct `task_id`, `round_number`, `response_text`, `responded_at`.
- Fixture outbox without that intent → assert zero `clarify_response` rows emitted (existing intent-routing tests must still pass).
- Existing shipper tests unchanged.

**Files:**
- `scripts/chain_event_shipper.py` (frozenset edit + handler branch in scan loop)
- `scripts/tests/test_chain_event_shipper.py` (two fixture cases per above)

**Cost estimate:** $4–6. Single PR. Mirror revisions expected 0–1.

---

## 7. Step 2 — dashboard `ClarifyRoundDrawer`

**Goal:** render the joined Q+A round-trip on the Missions-tab card side-panel.

**Concrete changes:**

- **New query helper** `queryClarifyRounds(taskId)` in `lib/clarify-queries.ts` — fetches `clarify_request` + `clarify_response` rows from Supabase for a task_id, joins by `round_number` per § 5.3, returns the round-array shape.
- **New component** `ClarifyRoundDrawer.tsx` rendering the round-trip chronologically. Each round shows:
  - Q card: Forge avatar, question text, timestamp.
  - A card: Beacon avatar, answer text, timestamp (or "awaiting Beacon" placeholder if answer is null).
  - CLARIFY-exhausted state shows a red banner above the rounds list.
- **Mount on the MissionCard side-panel below the existing chain timeline.** The exact mount point — direct child of `MissionCard.tsx` or extension of `ActionRowCard.tsx` from PR #20 — Forge to verify during preflight of Step 2 (the canonical mount may have shifted as the Missions tab evolved).
- **Polling cadence**: same 30s polling as the existing chain timeline. No WebSocket dependency.

**Test coverage:**

- Fixture chain_events with 3 complete rounds + 1 question-without-answer → renders 4 rounds; the last is in "awaiting Beacon" state.
- CLARIFY-exhausted fixture (matching `preflight_reject` row present) → renders the terminal red banner.
- Empty fixture (task with 0 rounds) → renders nothing (empty-state graceful, no placeholder).

**Files:**
- `lib/clarify-queries.ts` (NEW)
- `app/(dashboard)/missions/_components/ClarifyRoundDrawer.tsx` (NEW)
- `app/(dashboard)/missions/_components/MissionCard.tsx` OR `ActionRowCard.tsx` (wiring edit — Forge to confirm)
- New Vitest fixture file under the dashboard's existing fixture convention

**Cost estimate:** $6–9. Single PR. Mirror revisions expected 0–1.

---

## 8. Out of scope

- **Operator intervention affordance** — replying to or deleting rounds from the dashboard. The stub spec flagged this as a v2 path; this spec inherits that deferral. Requires routing + auth design that does not exist yet.
- **Real-time WebSocket updates** — v1 polls at the same 30s cadence as the rest of the Missions tab.
- **Mobile-optimized drawer** — the Missions tab is a desktop surface in v1.
- **Translation or sanitization of round content** beyond the shipper's existing `_REDACT_KEY_SUBSTRS` filter.
- **Backfill of historical CLARIFY cycles** — see § 2(e).

---

## 9. Sequence dispatch shape

Two steps in a linear DAG:

```
step-clarify-shipper-extend  →  step-clarify-dashboard-render
```

- **Step 1** (`step-clarify-shipper-extend`): § 6 above. `target_repo: ourliberty-agent-core`. No `depends_on`.
- **Step 2** (`step-clarify-dashboard-render`): § 7 above. `target_repo: ourliberty-dashboard`. `depends_on: ["step-clarify-shipper-extend"]` — the dashboard cannot render rows that don't exist yet.

Beacon authors the sequence file post-merge of this spec PR per the multi-step authoring discipline in `agents/beacon/CLAUDE.md`. Mirror runs DAG preflight (no cycles, two valid step_ids, no overlapping file touches, both spec sections exist). Advancer dispatches step 1 on PASS; step 2 dispatches automatically when step 1's PR merges.

---

## 10. Migration

None. The feature is additive:

- Step 1 adds a new event type to an existing frozenset and a new branch in an existing scan loop. No schema migration on `chain_events` (the table accepts any event_type per the row schema; the frozenset is application-side validation only per spec § 5.1 of the shipper's own spec).
- Step 2 adds new dashboard components and a new query helper. No schema migration on the dashboard side.
- No backfill (§ 2(e)).
- No retired event types; `clarify_request` is unchanged.
