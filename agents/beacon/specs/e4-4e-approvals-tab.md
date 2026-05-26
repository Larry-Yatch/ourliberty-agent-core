# Spec: E4.4e — Approvals Tab (Larry-Action Inbox + Interactive Chain Control)

**Status:** Draft (awaiting Larry approval — sub-spec of E4)
**Author:** Claude-as-Forge (written 2026-05-26 evening, decisions A–D locked conversationally same session)
**Approver:** Larry (pending)
**Phase:** E4.4e of `docs/phase-e-plan.md` Phase E4
**Parent spec:** [agents/beacon/specs/e4-overview.md](e4-overview.md)
**Predecessors:** E4.4a (read-only MVP, 2026-05-24); E4.4d (Operations + System view, 2026-05-26 morning); 5 same-day hotfix PRs (#122, #123, #125 surfaced + fixed during the System tab activation).
**Successor:** Future MVP — auto-remediation actions, batched approvals, voice-driven approvals via Telegram replay.

---

## 1. Problem statement (what triggered this)

E4.4d gave Larry a "is the system healthy" surface (Operations / System view). It does NOT give him a "what needs me right now" surface. Today every Larry-action item — Beacon approval gate proposals, Forge/Mirror clarifications, Pulse Check III config-tuning proposals, heal-pipeline-stall alerts that genuinely require human triage — arrives as a Telegram DM. Telegram is fine as a notification channel; it is awful as a queue. There is no:

- Single inventory of "everything pending your decision right now"
- Way to clear an item without losing it (Telegram has read receipts; "I saw it" is not the same as "I decided")
- Way to act on an item from somewhere other than Telegram (you cannot click Approve from a laptop browser)
- Way to differentiate FYI alerts ("memory bumped to 4G") from blocking gates ("Beacon needs your approval before Forge starts")

Today's E4.4d Escalations + Alerts panel is the read-only v0 of this. Larry's verdict on the existing Mark-as-read affordance, after using it for a few hours: *"relatively useless"* — passive acknowledgment doesn't help if the underlying thing still needs action. The real value is being able to act from the dashboard, in one queue, one item at a time.

E4.4e closes the action gap. Approvals becomes the canonical "Larry's inbox" surface; Telegram remains the notification hotline but stops being the only action channel.

---

## 2. Four locked decisions (chat round, 2026-05-26)

| # | Decision | Locked value | Rationale |
|---|---|---|---|
| A | Phasing | **Skip read-only phase; ship interactive (write-capable) from V1** | E4.4d's read-only Escalations panel proved Larry's intuition that passive acknowledgment without action is low-value. We have the schema groundwork (chain_events + read_at + service-role write path) so the marginal cost of going interactive in one PR sequence is small. |
| B | Auth model | **Supabase Auth + Google OAuth, single-email allowlist (`larry@sealteamleaders.com`)** | Dashboard is currently public read-only. The moment action routes exist, "anyone with the URL can approve PRs" is broken. Token-prompt was considered (cheaper to wire) and rejected as the wrong long-term shape for what will become a workhorse surface. Same pattern as Marvin Mission Control. |
| C | Action transport | **Dashboard writes JSON envelopes directly to droplet inbox dirs (e.g. `~/agents/inboxes/beacon/larry-approval-<task>.json`), shape-compatible with Telegram → beacon-bot writes** | Existing chain pattern is envelope-based. Beacon cannot distinguish a Telegram-originated approval from a dashboard-originated one — same envelope shape, same `source` field, same handler. Alternative was a `pending_actions` Supabase table with a droplet daemon polling; rejected as an extra moving part without a clear payoff. |
| D | Reject semantics | **Soft reject — archive the pending item + write a "Larry rejected" envelope back to the originating agent; that agent decides whether to abort / revise / replan** | Matches existing Telegram-rejection behavior. Hard reject (kill the worktree, abort the chain task immediately) would be a separate "Kill session" affordance, properly belongs on the Operations / System view's Active Sessions card, not folded into Approvals. |

---

## 3. Success criteria

A working Approvals tab delivers when ALL of the following are true:

- Larry can open `dashboard.ourliberty.dev/approvals` (signed in via Google) and within 5 seconds see every Larry-action item that is currently pending, ranked by what's blocking the chain.
- A Beacon approval gate proposal that today arrives as a Telegram DM ALSO appears as a card on the Approvals tab within ~30 seconds of being raised. Clicking Approve writes the same envelope shape that a Telegram-typed "approve" produces today. Beacon's `beacon_approval_handler` cannot distinguish the two.
- A Forge or Mirror `CLARIFY_REQUEST` appears with the question prompt + an inline text field; submitting writes a resume-envelope into the originating agent's inbox identical to the Telegram-typed response flow.
- A `larry_alert` (e.g. heal-pipeline-stall warning) appears with the alert's `suggested_action` field rendered as the card body. The card has only a "Mark done" affordance (no Approve/Reject) — this is the FYI bucket.
- A Pulse Check III config-tuning proposal appears with the proposed config diff inline. Clicking Apply writes a "Larry approved" envelope routed to whichever agent owns the config (typically Beacon → Forge).
- Acknowledged items collapse to a secondary "Acknowledged today" section that auto-expires at midnight local time. No item is ever silently deleted — every action writes a `larry_action` event to chain_events for audit.
- Rejecting an item writes a `larry_action` event AND a notification envelope back to the originating agent. The originating agent's CLAUDE.md governs what happens next (Beacon abandons, Forge revises, etc.).
- Unauthorized requests (anyone not `larry@sealteamleaders.com`) get a 401 from the action routes and a Google sign-in prompt on the page. No fallback to anonymous read.

---

## 4. Source taxonomy — what flows into Approvals

Six source types, three already in `chain_events`, three need new instrumentation.

| Source | Event type today | Today's surface | V1 wiring |
|---|---|---|---|
| 1. Heal/pipeline alerts | `larry_alert` (live, ~204 rows as of 2026-05-26) | Telegram DM + chain_events | Read existing rows; payload already has `{message, subject, severity, suggested_action}` — render directly. **No droplet change needed.** |
| 2. Pulse Check escalations | `escalation` (live, ~58 rows) | Telegram DM + chain_events | Read existing rows; payload already has `{headline, severity, needs_response, source_finding}` — render directly. Pin `needs_response=true` at top. **No droplet change needed.** |
| 3. Dispatch sentinel alerts | `sentinel_alert` (live, ~1 row) | Telegram DM + chain_events | Same shape as larry_alert. Render directly. **No droplet change needed.** |
| 4. Beacon approval gate | None (Telegram-only) | Telegram DM via `beacon_approval_handler` | NEW: emit `approval_request` chain_event when handler raises an approval DM. Payload: `{proposing_agent, target_agent, prompt, dedup_identity, suggested_envelope_for_approve, suggested_envelope_for_reject}`. |
| 5. Forge/Mirror CLARIFY | None (Telegram-only) | Telegram DM via outbox_notifier marker-classification | NEW: emit `clarify_request` chain_event when notifier classifies a clarify marker. Payload: `{asking_agent, task_id, question, resume_session_id}`. |
| 6. AUTO_MERGE_DEFERRED PRs | None (log-only) | outbox_notifier.log lines | NEW (optional, V1 stretch): emit `merge_blocked` chain_event after N consecutive AUTO_MERGE_DEFERRED_UNKNOWN retries on the same PR. Payload: `{pr_url, task_id, deferral_count, last_mergeable_state}`. |

**V1 scope decision:** Wire sources 1, 2, 3 immediately (zero droplet work). Wire 4 + 5 in the same PR as the dashboard UI since they require small `scripts/beacon_approval_handler.py` + `scripts/outbox_notifier.py` edits. Defer 6 to a V1.1 follow-up (the heuristic for "stuck merge" needs tuning and the existing `heal-pr-auto-merge` healer already catches a slice of it).

---

## 5. Data model additions

### 5.1 Migration 0006 — chain_events action audit

New event types added to the `KNOWN_EVENT_TYPES` frozenset in `scripts/chain_event_shipper.py`:

- `approval_request` — Beacon raises an approval gate
- `clarify_request` — Forge or Mirror asks a clarification
- `larry_action` — Larry took action via the dashboard (the AUDIT row)
- (Future) `merge_blocked` — AUTO_MERGE deferred too many times

No Supabase schema change needed for the new event_types — `chain_events.event_type` is plain TEXT with no CHECK constraint per the 0004 design.

**However**, we DO add one column for the action audit trail:

```sql
-- 0006_chain_events_actor.sql
ALTER TABLE chain_events
  ADD COLUMN actor TEXT NULL;

CREATE INDEX chain_events_actor_idx ON chain_events (actor) WHERE actor IS NOT NULL;
```

`actor` is NULL for all daemon-emitted events (the historical default). It gets populated with the authed user's email (`larry@sealteamleaders.com`) on `larry_action` rows so the audit trail is grep-able. The partial index keeps storage cheap.

### 5.2 `larry_action` event_type payload schema

When Larry clicks Approve / Reject / Comment / Mark-done, the droplet's action endpoint writes a `larry_action` row to chain_events:

```jsonc
{
  "event_id": "<sha1 of source_event_id + action + ts>",
  "ts": "<UTC iso>",
  "agent": "dashboard",
  "task_id": "<inherited from source event>",
  "event_type": "larry_action",
  "actor": "larry@sealteamleaders.com",
  "payload": {
    "source_event_id": "<the event being acted on>",
    "source_event_type": "approval_request|clarify_request|larry_alert|escalation|sentinel_alert",
    "action": "approve|reject|comment|mark_done",
    "comment": "<optional free-text>",
    "envelope_written": "<absolute path of the envelope file written, or null>",
    "target_agent": "<beacon|forge|mirror|pulse|null>"
  }
}
```

This row is the single source of truth for "what did Larry do." Replaying any approval decision means querying `chain_events WHERE event_type='larry_action' ORDER BY ts`.

### 5.3 Read-state lookup

`read_at` (from migration 0005) gets used differently than today's "Mark as read" — it now means "Larry acted on this." The Mark-done affordance still flips read_at as a passive ack; the Approve / Reject / Comment affordances flip read_at AND write a `larry_action` row. A pending item is `read_at IS NULL`.

---

## 6. Auth model

### 6.1 Supabase Auth + Google OAuth flow

1. Set up a Google OAuth client (single-page app) in Google Cloud Console. Redirect URI: `https://<supabase-project>.supabase.co/auth/v1/callback`.
2. Enable Google provider in Supabase Auth dashboard (project `ezldtkbhexyrgujqmxpd`), paste OAuth client ID + secret.
3. Configure Supabase Auth allowed redirect URLs to include `https://dashboard.ourliberty.dev/auth/callback` + `http://localhost:3000/auth/callback` (local dev).
4. In the dashboard Next.js app, install `@supabase/ssr` (replaces deprecated `auth-helpers-nextjs`). Wire middleware to refresh sessions; expose `getUser()` to Route Handlers.
5. Login page at `/login` shows a single "Sign in with Google" button. On success, callback handler verifies `user.email === 'larry@sealteamleaders.com'`; otherwise signs out + redirects to `/login?error=unauthorized`.
6. The `/approvals` page + all `/api/approvals/*` Route Handlers require an authed session AND the allowlisted email. Unauth gets 401 (Route Handlers) or redirect (page).

### 6.2 Allowlist enforcement

The allowed email is in a `config/approvals_allowlist.json` checked into the dashboard repo:

```json
{
  "allowed_emails": ["larry@sealteamleaders.com"]
}
```

Single file, single source of truth. Future additions (e.g. a delegate) are a code review PR, not an env var or runtime config — appropriate friction for an auth boundary.

### 6.3 Droplet action endpoint auth

The dashboard's action Route Handlers proxy to the droplet's NEW endpoint `POST /api/larry/action`. The droplet endpoint:

- Requires `X-Dashboard-Token` (existing `DASHBOARD_API_TOKEN` from .env.larry) — proves the call came from the dashboard server (not the browser)
- Requires `X-Actor` header set by the dashboard to the authed email — gets persisted as `larry_action.actor`
- Validates `X-Actor` against the same `approvals_allowlist.json` (synced from dashboard repo via CI or a `/api/larry/allowlist` GET on dashboard boot — defer the sync mechanism to spec § 6.4)

Two-layer auth: dashboard authenticates the human (Google), droplet authenticates the dashboard (token). Neither layer alone is sufficient.

### 6.4 Allowlist sync to droplet — TECHNICAL choice deferred

PR-author's call between:
- A: Hard-code `larry@sealteamleaders.com` in droplet endpoint code (simplest; future delegates need code PR + droplet sync)
- B: Droplet reads `/api/approvals/allowlist` from dashboard on startup + cache for 5 min (slight indirection; future delegates need only a dashboard PR)

Either is acceptable. PR description must document choice.

---

## 7. Action transport

### 7.1 Envelope shapes

When Larry clicks Approve on an `approval_request`:

```jsonc
// Written to ~/agents/inboxes/beacon/larry-approval-<task_id>.json
{
  "task_id": "larry-approval-<source_event_id>",
  "source": "dashboard",
  "actor": "larry@sealteamleaders.com",
  "dedup_identity": "larry-approval:<source_event_id>",
  "timeout": 600,
  "prompt": "Larry approved the pending proposal via dashboard. Source event: <source_event_id>. Proceed per the approve-path that beacon_approval_handler.py describes for this approval_request type. Use the suggested_envelope_for_approve payload from the source event."
}
```

When Larry clicks Reject:

```jsonc
{
  "task_id": "larry-reject-<source_event_id>",
  "source": "dashboard",
  "actor": "larry@sealteamleaders.com",
  "dedup_identity": "larry-reject:<source_event_id>",
  "timeout": 600,
  "prompt": "Larry rejected the pending proposal via dashboard. Source event: <source_event_id>. Optional comment: <comment or empty>. Soft reject — archive the pending item, do not abort any in-flight work, route per the suggested_envelope_for_reject payload from the source event."
}
```

When Larry submits a clarification reply:

```jsonc
// Written to ~/agents/inboxes/<asking_agent>/resume-<task_id>-r<N>.json
{
  "task_id": "<original_task_id>",
  "source": "dashboard",
  "actor": "larry@sealteamleaders.com",
  "resume_session_id": "<from source event payload>",
  "round": <round_number>,
  "prompt": "<Larry's typed response>"
}
```

The resume-envelope shape mimics the existing outbox_notifier-written `resume-<task>-r<N>.json` exactly (per the 2026-05-25 chain-discipline-v2 work in PR #115).

### 7.2 Droplet `POST /api/larry/action` endpoint

New endpoint on `ourliberty-dashboard-api.service` (extends existing FastAPI app at `scripts/dashboard_api.py`):

```
POST /api/larry/action
Headers: X-Dashboard-Token, X-Actor
Body: {
  "source_event_id": str,
  "action": "approve" | "reject" | "comment" | "mark_done",
  "comment": str (optional)
}
Response: {
  "action_event_id": str,    // the larry_action row's event_id
  "envelope_written": str | null,
  "target_agent": str | null
}
```

Implementation:
1. Validate token + actor allowlist; 401 on either fail.
2. Fetch source event from Supabase via service-role client.
3. If `action=="mark_done"`: flip `read_at` on source event, write a `larry_action` chain_event with `envelope_written=null`, return.
4. Otherwise: build the envelope per § 7.1, write to the appropriate inbox dir, flip source `read_at`, write `larry_action` chain_event with the absolute envelope path.
5. Return action_event_id.

**Idempotency:** Source event's `read_at` doubles as a "this has already been acted on" lock. The endpoint refuses (409 Conflict) if `read_at IS NOT NULL` and the new action is not `mark_done` (Mark-done is idempotent; Approve/Reject are not — Larry shouldn't double-click).

### 7.3 Droplet allowed-inbox-dirs allowlist

The action endpoint's envelope-writer takes a `target_agent` parameter derived from the source event's payload. To prevent path injection, the endpoint maintains a hard-coded allowed-agent allowlist:

```python
ALLOWED_TARGET_AGENTS = frozenset({'beacon', 'forge', 'mirror', 'pulse'})
ALLOWED_INBOX_ROOT = Path('/home/larry/agents/inboxes')
```

Any `target_agent` outside this set returns 400. Path constructed only as `ALLOWED_INBOX_ROOT / target_agent / filename.json`, never accepts a path component from the request body.

---

## 8. UI surface — Approvals tab

### 8.1 Routing + layout

- Top-level nav: existing tabs (Programs / Live System / Operations / Tasks / Costs / Healers) gain a new "**Approvals**" sibling, positioned **second** (after Programs) so Larry's eye lands on it first.
- Route: `/approvals` with no children for V1 (single unified queue).
- Login required: middleware redirects unauthed to `/login`. Non-allowlisted emails get signed out + redirected with error.

### 8.2 Three-bucket card layout

**Pending** (top, default-expanded):
- Cards where `read_at IS NULL` and `event_type IN ('approval_request', 'clarify_request', 'larry_alert', 'escalation', 'sentinel_alert')`
- Sort: `approval_request` first (blocking the chain), then `clarify_request`, then escalations/alerts by severity (critical/red > warning/yellow > info/green), then by recency
- Each card: source bubble (Beacon / Forge / Mirror / Pulse / Heal-*), subject/headline, severity dot, age, body (message or question), per-action affordances:
  - `approval_request` → Approve / Reject buttons + optional comment field
  - `clarify_request` → text field + Submit
  - `larry_alert` / `escalation` / `sentinel_alert` → Mark done (only)
  - `escalation` with `needs_response=true` → additionally Approve / Reject if the payload defines an action affordance

**Acknowledged today** (collapsed by default):
- Cards where `read_at >= today (Larry's local zone)`
- Sort by `read_at DESC` (most-recently-acked first)
- Each card shows the action taken in muted text ("Approved 14:32" / "Rejected 14:35 — 'wrong approach, try X'")

**Cleared** (collapsed, paginated):
- Cards where `read_at < today`
- Searchable, not actionable
- Exists for audit / "did I approve that yesterday?"

### 8.3 Real-time + polling

- SWR poll cadence: 5 sec for Pending bucket, 30 sec for Acknowledged, 60 sec for Cleared
- Optimistic UI on action: button click immediately moves the card to Acknowledged with a "submitting…" indicator; on droplet success, indicator drops and card persists; on failure, card returns to Pending with a toast.
- Cards never silently disappear — every transition is an explicit user action OR a fresh poll showing a new state from chain_events.

### 8.4 Components (file paths in dashboard repo)

- `app/approvals/page.tsx` — top-level page (server component for initial data + Suspense boundary for client SWR)
- `app/approvals/components/PendingCard.tsx` — per-card render with affordances
- `app/approvals/components/AcknowledgedCard.tsx` — muted-style card
- `app/approvals/components/ApprovalActionBar.tsx` — Approve/Reject/Comment widget
- `app/approvals/components/ClarifyReplyBox.tsx` — text input for clarify_request
- `app/api/approvals/list/route.ts` — GET handler returning the three buckets
- `app/api/approvals/action/route.ts` — POST handler that proxies to droplet `/api/larry/action`
- `lib/approval-queries.ts` — Supabase query helpers
- `lib/auth.ts` — Supabase Auth client + allowlist check
- `middleware.ts` — route protection

---

## 9. PR breakdown

Four PRs, sequenced for review-able size. Each goes through the standard Forge → Mirror → AUTO_MERGE chain.

### PR-A — Droplet instrumentation (chain_event emissions)

- `scripts/beacon_approval_handler.py`: emit `approval_request` chain_event when raising an approval DM; payload includes the dual envelopes (approve + reject suggested shapes)
- `scripts/outbox_notifier.py`: emit `clarify_request` chain_event when classifying a clarify marker; payload includes the asking agent's question text + `resume_session_id`
- `scripts/chain_event_shipper.py`: extend `KNOWN_EVENT_TYPES` to include `approval_request`, `clarify_request`, `larry_action`
- Tests: unit tests for both emission paths with fixture envelopes
- No UI work in this PR; verifiable via Supabase row count after triggering a Beacon approval or Forge clarify

### PR-B — Supabase migration 0006 + droplet action endpoint

- `supabase/migrations/0006_chain_events_actor.sql` — adds `actor` column + partial index (in dashboard repo)
- `scripts/dashboard_api.py`: add `POST /api/larry/action` endpoint with auth, envelope-writer, audit-row emitter
- `scripts/dashboard_api.py`: add `GET /api/larry/allowlist` returning the static allowed-email list (decision deferred per § 6.4)
- Tests: pytest covering 401 (bad token), 401 (bad actor), 409 (already acted), 400 (bad target_agent), 200 (happy path for each action type)
- Verifiable via curl + Supabase inspection

### PR-C — Dashboard Supabase Auth + Google login

- Install `@supabase/ssr`
- Add `/login` page + `/auth/callback` handler
- `lib/auth.ts` + `middleware.ts` for session + allowlist enforcement
- `config/approvals_allowlist.json` checked in
- All EXISTING routes remain anon-readable (no regression on Operations / System view); only `/approvals/*` + `/api/approvals/*` + `/api/larry/*` require auth
- Tests: Vitest covers middleware redirect on unauth, allowlist enforcement, callback validation
- Vercel env vars added: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` (already set), no new server secret

### PR-D — Approvals tab UI

- `app/approvals/*` per § 8.4
- `app/api/approvals/list/route.ts` + `app/api/approvals/action/route.ts`
- `lib/approval-queries.ts`
- Cypress / Playwright smoke test: sign in as allowlisted user, see a fixture Beacon approval, click Approve, assert droplet receives envelope (mock the droplet response in unit; manual smoke on staging)
- Manual acceptance per § 3

### Optional PR-E (V1.1) — `merge_blocked` source + Pulse Check III hookup

- Notifier emits `merge_blocked` after 3+ consecutive AUTO_MERGE_DEFERRED_UNKNOWN
- Pulse Check III config-tuning proposals get a structured `approval_request` payload with the proposed config diff + apply-on-approve envelope

---

## 10. Out of scope (explicit)

- **Mobile UI** — V1 targets desktop browser. Mobile-responsive is a stretch within PR-D if cheap; otherwise a follow-up.
- **Batch actions** — "Approve all pending Pulse proposals" is not in V1. One-at-a-time per spec § 3.
- **Telegram retirement** — Telegram DMs continue in parallel. Dashboard is additive, not replacing. V2 may add a per-user setting "suppress Telegram for items I've cleared in dashboard."
- **Multi-user / delegate** — Allowlist is single-email V1. Adding a delegate is a code PR per § 6.2; multi-user UX work (assignment, hand-off) is out of scope.
- **Comment threads** — Larry's comment on Approve/Reject is a single string. Threaded back-and-forth lives in Telegram + the resume-envelope flow (Forge replies via the chain, not via the dashboard).
- **Notification fan-out** — V1 does NOT push a browser notification when a new approval lands. Polling at 5s in the pending bucket is close enough; browser-push is a V2 polish.
- **Audit UI** — `larry_action` rows are queryable in Supabase + visible in the Cleared bucket; a dedicated audit-log view is out of scope.

---

## 11. Open questions for spec review

These are decisions I'm deferring to Larry, the PR author, or the spec review:

1. **`approval_request` payload contract.** PR-A needs to define exactly what `suggested_envelope_for_approve` and `suggested_envelope_for_reject` look like. Today's `beacon_approval_handler.py` constructs the approval DM body — adapting it to also emit a structured envelope template is straightforward, but the contract should be reviewed before PR-A merges so PR-D can rely on it.

2. **Severity normalization.** `larry_alert` uses `warning/critical/info`; `escalation` uses `red/yellow/green`. PR-D's UI needs a single severity scale. Proposal: normalize to `critical/warning/info` at render-time only (don't rewrite database rows). PR-D should document the mapping.

3. **Local zone for "today" boundary.** Acknowledged-today bucket needs a wall-clock "today" definition. Larry's zone is MDT (UTC-6). Dashboard browser already has this via `Intl.DateTimeFormat().resolvedOptions().timeZone`. Confirm: bucket boundary is browser-local midnight, not droplet-local.

4. **Allowlist sync mechanism (§ 6.4).** Option A vs Option B — PR-B author's call, documented in PR description.

5. **`merge_blocked` heuristic threshold.** N consecutive AUTO_MERGE_DEFERRED_UNKNOWN before emitting. Defer to PR-E.

---

## 12. Cost estimate

Best-guess Forge + Mirror chain spend, based on today's PR #122 / #123 / #125 actuals (~$2.50 each):

- PR-A: $3-4 (touches 3 files + tests; medium)
- PR-B: $4-6 (FastAPI endpoint + migration + comprehensive auth tests; larger)
- PR-C: $5-7 (Next.js auth wiring; first-time pattern in this repo, expect one revision round)
- PR-D: $5-8 (most UI surface; expect one Mirror revision round)
- **Total: $17-25** across the four PRs, ~2-3 hours wall-clock if dispatched sequentially.

Compared to Telegram-as-action-channel (free), this buys: durable audit trail, one-click action from any device, queue-able backlog, allowlist enforcement, and a path to delegate-friendly approvals in V2.

---

## End of spec
