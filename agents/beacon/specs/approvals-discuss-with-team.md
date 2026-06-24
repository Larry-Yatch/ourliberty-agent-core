# Spec: Approvals — "Discuss with team" (conversation card on the Approvals surface)

**Status:** Draft — blocked on Phase 4b landing (see § 7)
**Author:** Claude Code (dashboard session, 2026-06-23)
**Approver:** Larry
**Parent:** [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md)
**Predecessor / prerequisite:** [Phase 4b — Live-feel thread](missions-v2-phase4b-live-thread-poll.md) (poll + unread doorbell + clear-on-send; **must land first** — this surface inherits all three by reusing the same components)
**Realizes:** the deferred "dashboard-wide conversation card → Approvals" item called out in Phase 4 §12 and Phase 4b §11.
**Build path:** build-sequence orchestrator, two repos — agent-core (one new card-kind + two thin handlers, **no DB migration**) then dashboard (two proxy routes + a card affordance, all by analogy to the live Missions thread)

---

## 1. Purpose

On a Missions card, Larry can press **"Talk to the team"** and hold a real back-and-forth with Beacon. On the **Approvals** tab he cannot — an approval card only offers **Approve / Reject** (plus one optional comment line), and a clarify card offers a single fire-and-forget reply. When Larry wants to *think something through with the team before deciding* — "why is this risky?", "what's the blast radius?", "can you do the cheaper version instead?" — there is nowhere to have that conversation. He either approves blind, rejects to be safe, or leaves the tab and pings the team out-of-band.

This spec brings the **exact same conversation drawer** that already works on Missions to the Approvals surface, on both card types Larry asked for:

- **`approval_request`** — discuss is **additive**: Approve / Reject stay as the decision; "Discuss with team" opens a thread alongside them.
- **`clarify_request`** — discuss is **additive**: the existing one-shot answer stays; "Discuss with team" opens a thread for talking it over.

**Done-gate:** Larry opens an approval card, presses **"Discuss with team"**, asks Beacon a question, and **Beacon's reply appears in the drawer on its own within ~5 seconds — no browser refresh** — then he Approves/Rejects with the context he needed. The lived experience (ask → answer → decide, all on the card) is the gate, not green unit tests.

> **Why this is small:** the conversation mechanism is already store-agnostic. The droplet thread store keys a `card_message` event stream by an `item_id` and already serves three card kinds (`capture`, `mission`, `phase`) through one `_CARD_KIND_META` table and one `_post_card_message` core (`scripts/dashboard_api.py`, verified 2026-06-23). The dashboard already folds a flat message list into the drawer via `foldThreadRounds` (`lib/thread-rounds.ts`) and renders it with `ClarifyRoundDrawer` + `ClarifyReplyBox`. This spec adds a **fourth card kind** keyed by the approval event's id, two thin dashboard proxy routes, and one card affordance. **Assembly, not greenfield.**

---

## 2. Scope decision

| In scope (this phase) | Deferred |
|---|---|
| **"Discuss with team" thread on `approval_request` cards** (additive to Approve/Reject) | Routing a clarify discussion to the **asking agent** instead of Beacon (see § 4 decision D2) → follow-up if Larry wants direct-to-agent clarify threads |
| **"Discuss with team" thread on `clarify_request` cards** (additive to the existing one-shot answer) | Conversation card on **alerts / escalations / Operations** (Phase 4 §12) → its own pass; alerts are FYI/ack, not a dialogue |
| **Beacon-routed, single team voice** — same model as Missions (§ 2 decision #5 of Phase 4): one voice, Beacon coordinates the asking agent if needed | **Decision-on-thread** (approving *inside* the conversation, "approve as discussed") → out of arc; decide with the existing buttons |
| **Inherit Phase 4b** — poll, unread doorbell, clear-on-send — by reusing the same components, **for free** (§ 7) | Multi-voice addressing, drafts, attachments, markdown preview → out of arc |

This is deliberately the **narrowest useful slice**: bolt the proven Missions thread onto the two Approvals card types, change nothing about how decisions are made, and lean entirely on Phase 4b for the live feel.

---

## 3. Reuse map

Verified against `scripts/dashboard_api.py` and the dashboard tree on 2026-06-23:

| Capability | Reuses (existing) | Mode |
|---|---|---|
| Thread store (one `card_message` event stream per card) | `_post_card_message`, `_card_thread_messages`, `_shape_thread_message` (`scripts/dashboard_api.py`) — already store-agnostic over `capture`/`mission`/`phase` | **extend** (add `approval` kind) |
| Card-kind registry | `_CARD_KIND_META` dict (id_key + noun + thread_url per kind) | **extend** (one row) |
| Per-message stable `id` | `chain_events.event_id` projected as `CaptureThreadMessage.id` | **inherited from Phase 4b Contract C** |
| Dashboard thread proxy (GET) | `app/api/missions/captures/[capture_id]/thread/route.ts` (token-gated read proxy) | **copy by analogy** |
| Dashboard message proxy (POST) | `app/api/missions/captures/[capture_id]/message/route.ts` (OAuth + X-Actor write proxy) | **copy by analogy** |
| Fold flat messages → rounds | `foldThreadRounds` (`lib/thread-rounds.ts`) | direct |
| Thread render | `ClarifyRoundDrawer` (You/Team labels) | direct |
| Compose box | `ClarifyReplyBox` (`app/approvals/components/` — already lives here, already shared) | direct |
| Card affordance pattern (toggle + lazy load + send + error) | `FunnelCard.tsx` (`threadOpen` / `rounds` / `threadLoading` / `threadError` / `loadThread` / `handleMessage`) | **copy by analogy** |
| Card chrome / header / meaning layer | `UniversalCard` (both tabs already share it) + `approvalToFunnelItem` (`lib/funnel.ts`) | direct |
| Live feel (poll / unread / clear-on-send) | Phase 4b Contracts A/B/D on the shared drawer + reply box | **inherited** (§ 7) |
| Card-level error isolation | `PanelErrorBoundary` (already wraps the drawer in `FunnelCard`) | direct |

**No new DB table and no schema migration.** The conversation lives in `chain_events` as `card_message` rows keyed by `task_id == item_id`, exactly as the capture/mission/phase threads do.

---

## 4. Key design decisions

**D1 — Thread key is the approval event's id, NOT its `task_id`.**
Each Approvals card is a distinct `ChainEvent` with its own `event_id`; the dashboard already addresses it everywhere as `source_event_id` (the body field on `POST /api/approvals/action`). Multiple events can **share a `task_id`** (the Approvals tab even groups them into a `TaskGroupCard` when ≥2 share one). Keying the thread by `task_id` would **cross-thread two cards in the same group**. So the `card_message` rows for an approval discussion carry `task_id == source_event_id` (the per-card join key), isolating each card's conversation. This is the `id_key` for the new card kind.

**D2 — "Discuss with team" routes to Beacon (single team voice), for both card types.**
`_post_card_message` drops a resume/notify envelope into **Beacon's** inbox; Beacon answers on her next cycle as the single team voice (Phase 4 decision #5). We keep that for Approvals: Beacon reads the thread, coordinates the underlying agent if needed, and replies `team_to_larry`. This reuses `_post_card_message` **verbatim** (just a new `item_kind`).
*Rationale:* consistent mental model with Missions, zero new routing code.
*Trade-off (deferred):* a `clarify_request` was raised by a **specific** agent (`payload.asking_agent`), and one could argue its discussion should resume *that* agent directly. That requires `_post_card_message` to target the asking agent's inbox instead of Beacon's — a real divergence. **Deferred** (§ 11). **Decided (Larry, 2026-06-23): Beacon-routed for both card types — "Beacon manages the team."** Beacon fields clarify discussions too and pulls in the asking agent off-thread if needed.

**D3 — Discuss is additive; decisions are unchanged.**
The thread does **not** replace any existing affordance. `approval_request` keeps `ApprovalActionBar` (Approve/Reject + one comment). `clarify_request` keeps its one-shot `ClarifyReplyBox` answer (which still posts `action="comment"` → resumes the asking agent via `/api/approvals/action`). "Discuss with team" is a **separate toggle** that opens the conversation drawer. The formal clarify *answer* and the *discussion* are two channels with two different routings, by design — the answer resumes the asking agent; the discussion talks to the team.

**D4 — Gate the affordance to exactly `approval_request` + `clarify_request`.**
`larry_alert` / `sentinel_alert` / `escalation` cards do **not** get a discuss toggle (they're ack-only; § 2 defers them). The toggle renders in `PendingCardBody` only for the two in-scope `event_type`s.

---

## 5. Contract A — Approvals thread affordance (dashboard)

Mount the proven Missions thread on the Approvals card, by analogy to `FunnelCard`.

- In `PendingCardBody` (`app/approvals/components/PendingCard.tsx`), for `event_type ∈ {approval_request, clarify_request}` only, render a **"Discuss with team"** toggle (→ "Hide thread" when open), with `data-testid={`approvals-thread-toggle-${event_id}`}` and `aria-expanded`. It sits in the expanded body next to the existing action set — additive, never replacing it (D3).
- **Lazy load:** only on first open, `GET /api/approvals/{event_id}/thread`. Fold the flat `messages[]` into `ClarifyRound[]` with `foldThreadRounds` and render `ClarifyRoundDrawer` with `questionLabel="You"` / `answerLabel="Team"` (the same overrides Missions uses outside the Forge↔Beacon framing).
- **Compose:** `ClarifyReplyBox` → `POST /api/approvals/{event_id}/message` with `{ message }`. Optimistic append of Larry's line, exactly as `FunnelCard.handleMessage` does.
- **State + errors:** mirror `FunnelCard` — `threadOpen` / `rounds` / `threadLoading` / `threadError`; keep the `PanelErrorBoundary`; a failed load shows "Couldn't load the thread. Reload to retry." and never blanks the card.
- **Absence is legal:** a card with no messages yet renders an empty drawer + the compose box (no error).

Acceptance: an approval card and a clarify card each show a "Discuss with team" toggle; opening it loads the thread; sending a message appends Larry's line and posts to the message endpoint; the existing Approve/Reject (or one-shot answer) is untouched and still works.

## 6. Contract B — backend: add the `approval` card kind (agent-core)

Two thin pieces in `scripts/dashboard_api.py`, both reusing the existing store-agnostic core. **No new DB table, no migration.**

1. **Register the kind.** Add to `_CARD_KIND_META`:
   ```python
   _CARD_KIND_APPROVAL = 'approval'
   # ...
   _CARD_KIND_APPROVAL: {
       'id_key': 'source_event_id',
       'noun': 'approval card',
       'thread_url': '/api/approvals/{id}/thread',
   },
   ```
   `_post_card_message(item_kind=_CARD_KIND_APPROVAL, ...)` then works unchanged: it emits the `larry_to_team` `card_message` row keyed by the `source_event_id`, drops the resume envelope into Beacon's inbox, and clears any blocked-on-you doorbell — all reused (D2).

2. **Two handlers + routes** (mirror the capture handlers):
   - `GET /api/approvals/{source_event_id}/thread` → validate the source event exists and is an `approval_request`/`clarify_request` (404 otherwise), then return `{ source_event_id, messages: _card_thread_messages(source_event_id, …), last_synced_at }`. Degrade to an **empty thread** when Supabase is unavailable (same read-resilience as `_handle_capture_thread`). `messages` inherit the per-message `id` from Phase 4b Contract C (shared `_shape_thread_message`).
   - `POST /api/approvals/{source_event_id}/message` → `_post_card_message(item_id=source_event_id, item_kind=_CARD_KIND_APPROVAL, text=…, actor=X-Actor, …)`. 400 on empty text; 503 if Supabase is down (the message must be durable). Token-gated for read, X-Actor for write, same as the capture routes.

3. **Guard tests:**
   - `GET .../thread` returns `messages` oldest-first with stable `id` (== `event_id`) / `direction` / `ts` / `needs_reply` + fresh `last_synced_at`; unknown/non-approval event → 404; Supabase-down → empty thread, not 500.
   - `POST .../message` writes one `larry_to_team` `card_message` keyed by `source_event_id`, drops a Beacon envelope, and clears the blocked-on-you doorbell.
   - **Cross-thread guard (D1):** two approval events sharing a `task_id` keep **separate** threads (a message on event A never appears on event B's thread).

Acceptance: `curl GET /api/approvals/{id}/thread` returns the oldest-first conversation; `POST .../message` persists Larry's turn and notifies Beacon; two cards in one task group never share a thread.

## 7. Inherits Phase 4b — do not reimplement (dependency)

This surface reuses the **same** drawer (`ClarifyRoundDrawer`), the **same** reply box (`ClarifyReplyBox`), the **same** fold (`foldThreadRounds`), and the **same** `/thread` response shape (with the per-message `id`) that Phase 4b upgrades. Therefore, **once Phase 4b lands**, the Approvals thread gets — at no extra cost:

- **Poll the open thread** (Phase 4b Contract A) → Beacon's reply appears within ~5s, no refresh.
- **Unread badge / "N new ↓" / manual refresh / loud-vs-quiet doorbell** (Contract B) → a reply to a closed card lights a badge.
- **Clear-on-confirmed-send** (Contract D) → the box empties only after the POST resolves.

**Implication for sequencing:** build the dashboard wiring (Contract A) **on top of the Phase 4b live-thread components**, not before. The agent-core card-kind (Contract B) is **independent of Phase 4b's dashboard work** and can be built in parallel — but it relies on the per-message `id` from **Phase 4b Contract C** (the `_shape_thread_message` projection), so Contract C must merge first (or, if Phase 4b slips, fold the trivial `id` projection into Contract B). **Do not build the Approvals discuss UI as a standalone before Phase 4b — it would copy the soon-replaced fetch-once drawer and collide on the shared files.**

## 8. Build plan — 2 steps (two repos)

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **1 — approval card kind + thread/message endpoints** | agent-core | Contract B: add `_CARD_KIND_APPROVAL` to `_CARD_KIND_META`; `GET/POST /api/approvals/{id}/thread\|message` reusing `_post_card_message`/`_card_thread_messages`; guard tests incl. the cross-thread (D1) case | Phase 4b **Contract C** (per-message `id` projection) merged |
| **2 — Approvals "Discuss with team" affordance** | dashboard | Contract A: new proxy routes `app/api/approvals/[event_id]/thread\|message/route.ts` (copy the capture routes); `PendingCard` discuss toggle gated to `approval_request`+`clarify_request`, reusing `ClarifyRoundDrawer`/`ClarifyReplyBox`/`foldThreadRounds` + the `FunnelCard` lifecycle; an `ApprovalThreadResponse` type in `lib/types.ts` | Step 1 **and** Phase 4b **dashboard live-thread** step merged |

Step 1 can start as soon as Phase 4b Contract C is in. Step 2 must wait for the Phase 4b dashboard live-thread step so it inherits poll/unread/clear-on-send (§ 7).

## 9. Test / proof plan

- **Contract A (dashboard):** the discuss toggle renders for `approval_request` and `clarify_request` and **not** for `larry_alert`/`sentinel_alert`/`escalation`; opening loads + folds the thread; sending optimistically appends and POSTs; a failed load shows the retry copy and never blanks the card; the existing Approve/Reject and one-shot clarify answer still work with the drawer open.
- **Contract B (agent-core):** the three guard tests in § 6 are green — including the **cross-thread (D1)** assertion that two events sharing a `task_id` keep separate threads.
- **Inherited (Phase 4b):** with Phase 4b merged, a seeded `team_to_larry` reply appears in an open Approvals thread within one poll interval; a reply to a closed card lights the unread badge; the reply box clears only on confirmed send. (Covered by Phase 4b's own tests — assert here only that the Approvals surface mounts the same components, not the behavior twice.)
- **End-to-end (the real gate):** Larry opens a live approval card, presses "Discuss with team", asks Beacon a question, **watches the answer arrive without refreshing**, then Approves — the whole loop on one card.

## 10. Beacon kickoff & sequencing

**Prerequisite (hard):** **Phase 4b must land first.** Contract B (agent-core) depends on Phase 4b Contract C's `id` projection; Contract A (dashboard) depends on the Phase 4b live-thread step so it inherits the live feel. Do not dispatch this sequence until Phase 4b's `phase4b-live-thread-001` is merged (or, if Larry wants to parallelize, fold the one-field `id` projection into Step 1 and accept a static drawer until 4b lands — **not recommended**).

**Decided (Larry, 2026-06-23) — D2:** "Discuss with team" on a **clarify** card routes to **Beacon** (single team voice — "Beacon manages the team"), same as Missions, and is **additive** to the existing one-shot answer (which still resumes the asking agent). The direct-to-asking-agent alternative is **deferred** (§ 11).

**Repo split (orchestrator V1 is single-repo per sequence, build-sequence spec § 4):**
- **Agent-core one-off (Contract B)** (`ourliberty-agent-core`): a single APPROVAL_REQUEST adding the `approval` card kind + the two `/api/approvals/{id}/thread|message` handlers + guard tests. **Prerequisite for the dashboard step.** Dispatch after Phase 4b Contract C.
- **Dashboard sequence `approvals-discuss-001`** (`ourliberty-dashboard`): a single step `approvals-discuss` (Contract A) — proxy routes + `PendingCard` toggle + `ApprovalThreadResponse` type, on top of the Phase 4b live-thread components. `depends_on`: the agent-core endpoints live + Phase 4b dashboard step merged.

> **Kickoff intent (paste to Beacon — only after Phase 4b lands):**
> Beacon — build the Approvals "Discuss with team" thread from `agents/beacon/specs/approvals-discuss-with-team.md`, after Phase 4b is merged. **First** dispatch Contract B as a one-off against `ourliberty-agent-core`: add a fourth card kind `approval` to `_CARD_KIND_META` (id_key `source_event_id`, thread_url `/api/approvals/{id}/thread`) and add `GET/POST /api/approvals/{id}/thread|message` reusing `_post_card_message`/`_card_thread_messages` (Beacon-routed), plus guard tests including the cross-thread case (two events sharing a task_id keep separate threads). **Then** run the dashboard sequence `approvals-discuss-001` against `ourliberty-dashboard`: add the two proxy routes (copy the capture thread/message routes) and a "Discuss with team" toggle on `PendingCard`, gated to approval_request + clarify_request, reusing ClarifyRoundDrawer/ClarifyReplyBox/foldThreadRounds and the FunnelCard lifecycle — additive to Approve/Reject and to the one-shot clarify answer. It inherits Phase 4b poll/unread/clear-on-send for free. Spec §§ 5–6; acceptance therein. D2 decided: Beacon-routed. Mirror focus: discuss is additive (decisions unchanged), toggle gated to the two event types only, thread keyed by source_event_id not task_id.

## 11. Out of scope (later / separate)

- **Conversation card on alerts / escalations / Operations** (Phase 4 §12) — alerts are ack/FYI, not a dialogue; its own pass if wanted.
- **Clarify discussion routed to the asking agent** instead of Beacon (D2) — a `_post_card_message` routing option; follow-up if the Beacon-routed default proves wrong.
- **Decide-on-thread** ("Approve as discussed" inside the conversation) — keep decisions on the existing buttons.
- **True push** (SSE / Realtime) — owned by Phase 4c; this surface inherits whatever the shared drawer uses.
- **Multi-voice, drafts, attachments, markdown preview** — out of arc, same as Phase 4b §11.
