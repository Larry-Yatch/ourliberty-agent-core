# Approvals Queue Rework — Consolidated Spec + DAG Definition

Status: DRAFT for Larry review (2026-06-02)
Scope: 2 repos — `agent-core` (droplet) + `ourliberty-dashboard` (Vercel UI)
Deployment: orchestrator / DAG (dedicated multi-PR project)

---

## 1. Problem (one paragraph)

The dashboard Approvals tab is an append-only firehose. A `chain_events` row leaves
the pending list ONLY when `read_at` is set, which today happens ONLY when Larry
clicks Approve/Reject/Mark-done. Nothing auto-clears a row when the underlying
work resolves on its own, nothing dedupes recurring alerts, and there is no
retention on the table. Result on 2026-06-02: 1,423 "pending" items, of which
~1,100 were repeated healer/health noise and ~244 were leaked test mocks. A
one-time cleanup reset it to 74 real decisions; this spec makes that state durable.

## 2. Locked decisions (from this session)

- L1. Alerts/escalations MOVE OUT of the Approvals tab. Approvals becomes a pure
  yes/no decision inbox.
- L2. Sequence: do the one-time cleanup first (DONE), then this systemic rework.
- L3. Ship via DAG/orchestrator, not loose single dispatches.
- L4. Auto-clear is SIGNAL-ONLY (confirmed resolution), never blind age-out. Headline
  reads "N decisions need you."
- L5. Decision card altitude: a LITTLE more than one line — a compact context strip,
  not the full dispatch. Increase modestly, keep it scannable.
- L6. Health view is SILENT to Larry by default. He does NOT browse it. Only items
  that cross a "needs CEO attention" bar get PROMOTED into his decision inbox. Below
  that bar, alerts stay in an agent/ops-internal surface he never has to look at.
- L7. Daily + weekly CEO digests as their own card on the Approvals page. Daily covers
  the prior day; weekly fires Monday. Voice = CEO-to-CEO, not CTO.
- L8. No blind bulk-purge. A "clean up" button runs an AGENT that triages the batch,
  auto-clears the confirmed-stale, and SURFACES any item that still needs a decision
  with the reason ("item 5 is actually important — you decide"). This is exactly the
  live/stale triage proven manually on 2026-06-02, productized.

## 3. Current-state facts (grounded in code, not assumed)

Backend (`agent-core`):
- Decision event types: `approval_request`, `clarify_request`.
- Noise event types: `larry_alert`, `sentinel_alert`, `escalation`.
- The model has ONLY `read_at` (no status column). Pending = `read_at IS NULL`.
  Clearing = set `read_at`. Reversible by resetting to NULL.
- `clarify_request` resolution signal: a `clarify_response` event with the same
  `task_id` is emitted by `scripts/outbox_notifier.py` when the clarification is
  answered down the chain.
- `approval_request` resolution signal: `scripts/beacon_approval_handler.py`
  `resolve()` moves the source entry in `~/agents/state/beacon-pending-approvals.json`
  to history with a `resolved_at` stamp (fires when beacon approves via Telegram or
  auto-approves) — today this leaves the dashboard row stranded as "pending."
- Noise re-accumulates: `scripts/chain_event_shipper.py` re-emits alerts/escalations
  each cycle on content change (escalations come from a snapshot file overwritten
  every Pulse cycle), so a persistent condition stacks a new row every run.
- Table also holds ~3,200 `session_start`/`session_done` bookkeeping rows that never
  clear — unbounded growth, invisible to the Approvals tab but real.

UI (`ourliberty-dashboard`):
- `lib/approval-queries.ts` -> `APPROVABLE_EVENT_TYPES` is the SINGLE source of truth
  for what the Approvals tab shows. Currently all 5 types. Narrowing this is the
  one-line lever for L1.
- `app/approvals/components/FilterChips.tsx` -> `CHIP_ORDER` has All / Approvals /
  Clarifications / Escalations / Alerts chips.
- `app/approvals/components/ApprovalsClient.tsx` computes per-type counts and the
  "blocking the chain" strip (`PinnedBlockingStrip.tsx`).
- `extractHeadline` already exists (H1 -> bold line -> first sentence -> task_id slug),
  so a CEO-readable one-line summary per card is already available.
- Alerts already have a home that was switched off: `app/operations/system/page.tsx:30`
  says "EscalationsAlertsPanel mount is REMOVED — Approvals tab owns that surface."
  `components/EscalationsAlertsPanel.tsx`, `lib/system-queries.ts`
  (`ESCALATION_EVENT_TYPES` query + `markEventRead`) all still exist. L1 is largely a
  revert + rewire, NOT greenfield.

## 4. The five moves -> node mapping

| Move | What | Node |
|------|------|------|
| 1 | Alerts out of Approvals; silent ops surface + promote-to-inbox rule | N4 |
| 2 | Auto-clear a decision when its work resolves (signal-only) | N1 |
| 3 | Dedupe the alert stream; promote only attention-worthy to Larry | N4 |
| 4 | Retention on chain_events table | N2 |
| 5 | "N decisions need you" headline + richer-but-compact cards | N5 |
| 6 | Agent-reviewed "clean up" button (no blind purge) | N1 engine + N5 button |
| 7 | Daily + weekly CEO digest card | N6 |

---

## 5. DAG definition

### Nodes

**N1 — Auto-clear on resolution** (repo: agent-core, backend)
- New job `scripts/heal_stale_approvals.py` on a systemd timer. Each tick, for every
  pending decision row, set `read_at` if the work has resolved:
  - `clarify_request`: STALE if EITHER (a) a `clarify_response` exists for the same
    `task_id`, OR (b) the task "progressed past" the question — the same `task_id` has
    an `approval_request` resolved in beacon history at/after the clarify's `ts`, OR a
    PR for the task merged after the clarify. NOTE (empirical, 2026-06-02 triage): the
    `clarify_response` event type is newer and was NOT emitted for older rounds, so
    signal (a) alone UNDER-clears. Signal (b) is required. In the 2026-06-02 backlog,
    (a)-only left 19 "live" rows that were all actually shipped; (b) + PR-merge check
    resolved all of them.
  - `approval_request`: source entry is no longer pending in
    `beacon-pending-approvals.json` (in history / has `resolved_at`). Empirically this
    cleared 49/49 — beacon resolves every approval (pending=0) but the dashboard row
    is left stranded; that stranding IS the bug N1 fixes.
- Backup-first (reuse `scripts/approvals_cleanup.py` backup pattern); idempotent;
  structured logging; never touches rows whose source is still genuinely pending.
- Reuses: `beacon_approval_handler.resolve()` history, `outbox_notifier` clarify_response.
- Tests: reserve `zz-fixture-` namespace (never `real-*`/`prod-*` — those are live mocks).
- ALSO exposes a `/api/larry/cleanup-review` action (the L8 button engine): given the
  current pending set, it runs the same triage (`scripts/triage_decisions.py` logic),
  AUTO-clears the confirmed-stale, and RETURNS the items it judged still-live with a
  one-line reason each, so the UI can surface "these N still need you." For low-confidence
  cases it escalates to a verification subagent (the pattern proven 2026-06-02) rather
  than guessing. NEVER clears an item it cannot confirm resolved.
- Acceptance: a resolved clarify/approval leaves the Approvals tab within one tick;
  a still-pending one is untouched; the cleanup-review action never clears an
  unconfirmed item and always reports what it kept and why.
- Dependencies: NONE. Fans out at start.

**N2 — Retention on chain_events** (repo: agent-core, backend)
- New retention job (timer): archive/clear bookkeeping rows
  (`session_start`, `session_done`, marker/plumbing types) older than the retention
  window. NEVER archives a decision row that is still pending.
- Default retention window: 14 days (tunable; wire a Pulse-Check self-optimizing hook
  per existing pattern rather than hand-pinning long-term).
- Acceptance: pending row count stops growing unbounded; no pending decision archived.
- Dependencies: NONE. Fans out at start.

**N4 — Silence alerts + promote-only-what-needs-Larry** (repos: agent-core + ourliberty-dashboard)
- Per L6, Larry does NOT browse a health view. Two parts:
  - Ops-internal surface (dashboard): alerts live in the Ops/System page, deduped —
    group by dedup key (`payload.dedup_identity` else `task_id`), ONE row per distinct
    condition with count + last-seen, newest-first, age-out beyond cutoff. This page is
    for the system / for when Larry chooses to dig — it never pushes to him.
    Re-mount `EscalationsAlertsPanel` on `app/operations/system/page.tsx`; reuse
    `lib/system-queries.ts` `ESCALATION_EVENT_TYPES` + `markEventRead`.
  - Promotion rule (agent-core): a classifier decides which alert/escalation crosses
    the "needs CEO attention" bar (e.g. severity=critical AND not self-resolved within
    one cycle, or an explicit escalation flagged for Larry). Only those are surfaced to
    Larry — as a "needs attention" item in the Approvals decision inbox (reuse the
    existing `NeedsAttentionCard` vehicle on `/live`). Everything below the bar stays in
    the ops surface, silent. Default bar is conservative; wire a Pulse-Check tuning hook.
- Acceptance: routine alerts never reach Larry; a genuine attention item appears in his
  inbox within one cycle; the ops surface shows deduped alerts (1 line + count).
- Dependencies: NONE (uses existing data). Fans out at start.

**N5 — Approvals rescope + CEO framing** (repo: ourliberty-dashboard, UI)
- `lib/approval-queries.ts`: `APPROVABLE_EVENT_TYPES` -> `["approval_request","clarify_request"]`;
  drop escalation/alert from comparator + `EVENT_TYPE_RANK`.
- `FilterChips.tsx`: `CHIP_ORDER` -> All / Approvals / Clarifications (drop Escalations, Alerts).
- `ApprovalsClient.tsx` + `PinnedBlockingStrip.tsx`: remove alert/escalation count
  branches; reframe header to "N decisions need you" (default headline, exact wording = L-open-1).
- `PendingCard` / `MarkdownBody` (L5 card altitude): collapsed default = the
  `extractHeadline` sentence PLUS a compact context strip — program/agent, what's being
  asked (1 short clause), dollar cost if present, age. A LITTLE more than today, not the
  full dispatch. Full dispatch stays behind the "details" expander.
- Cleanup button (L8, net-new): replaces any blind "clear older than Nd". Calls the N1
  `cleanup-review` engine -> auto-clears confirmed-stale, then shows a short
  "kept N for you" list with reasons. Never clears an unconfirmed item.
- Update existing tests (`FilterChips`, `ApprovalsClient`, query tests).
- Acceptance: Approvals shows only decisions; headline reads "N decisions need you";
  cards show the compact context strip; cleanup button clears only confirmed-stale and
  surfaces the rest.
- Dependencies: N4 (alerts re-homed/silenced before removed from Approvals) and N1
  (cleanup-review engine + auto-clear). 


**N6 — Daily + weekly CEO digest** (repos: agent-core + ourliberty-dashboard)
- Generator (agent-core, scheduled): an LLM-backed job that reads the prior period's
  chain activity — what shipped, what auto-cleared, what's still waiting, spend, any
  attention items — and writes a CEO-voice summary (plain outcomes, no healer/PR/marker
  jargon; falls through to render-layer human translation). Daily job covers the prior
  day; weekly job fires Monday covering the prior week. Writes the digest to a small
  table/file the UI reads. CEO-for-CEO voice is an explicit acceptance criterion.
- Digest card (dashboard): a separate card on the Approvals page showing the latest
  daily digest, with the Monday weekly pinned/available. Read-only, glanceable.
- Acceptance: a non-technical reader understands it without context; daily + weekly
  cadence fire on schedule; no CTO jargon.
- Dependencies: generator is independent (fans out at start); the card coordinates with
  N5's page layout (soft).

### Edges

```
N1 ─┐
N2 ─┤  (backend, independent — start immediately, in parallel)
N4 ─┤  (ops surface + promotion rule; spans both repos)
N6 ─┘  (digest generator independent; card pairs with N5)
   └────► N5   (gated on N1 cleanup-engine + N4 contract; N6 card lands on same page)
```

- Layer 0 (parallel): N1, N2, N4, N6-generator.
- Layer 1: N5 (gated on N1 + N4) and N6-card (pairs with N5's page work).
- Repos: N1/N2 = agent-core; N4/N6 span both; N5 = ourliberty-dashboard. ~6-9 PRs total.

## 6. Defaults to confirm

Technical (decide-and-move; sensible defaults chosen, flag if you disagree):
- Auto-clear timer cadence: every 10 min (aligns with Pulse cycle).
- approval_request auto-clear trigger: beacon-pending-approvals history signal first
  (most reliable); PR-merge signal can be added later.
- Retention window: 14 days for bookkeeping rows.
- Ops-surface alert dedup key: `dedup_identity || task_id`; ops age-out: 7 days.
- "Needs CEO attention" promotion bar: severity=critical AND not self-resolved within
  one cycle (conservative; Pulse-Check tunable).
- Digest cadence: daily at 6:00am (Larry's local tz) covering prior day; weekly Monday
  6:00am covering prior week. [LOCKED]
- "Needs CEO attention" bar: conservative — only true criticals not self-resolved within
  one cycle. [LOCKED]
- Cleanup button: NO time cutoff — agent-reviewed only (L8), so "7 days old but still
  important" is caught and surfaced, never silently cleared.

Values (yours):
- L-open-1: exact headline wording — "N decisions need you" vs alternative.
- L-open-2: how aggressive to be on already-done approvals — auto-clear strictly on a
  confirmed resolution signal (safe, recommended) vs also age-out decisions older than
  N days regardless of signal (faster cleanup, small risk of clearing a live one).

## 7. Risks / guards

- Mock trap: `real-*` and `prod-*` (prod-clr/prod-fail) are live test mocks, NOT
  fixtures and NOT real decisions. Auto-clear/retention must verify a real resolution
  signal, not pattern-match task_id. Tests use the reserved `zz-fixture-` namespace.
- Verify-before-clear: N1 must NOT clear an approval_request still present + pending in
  beacon-pending-approvals.json.
- Double-surface gap: enforce edge N4 -> N5 so alerts are never invisible in both views.
- Every clear remains reversible (read_at -> NULL); jobs back up affected rows first.

## 8. Kickoff sequencing

1. Land this spec to `agent-core/docs/` (pre-launch audit artifact).
2. Orchestrator kickoff: fan out N1, N2, N4; gate N5 on N4.
3. Per-node Forge briefs are sections 5.N above, near-verbatim.
