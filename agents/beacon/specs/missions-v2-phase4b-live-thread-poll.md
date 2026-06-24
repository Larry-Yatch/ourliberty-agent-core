# Spec: Missions v2 — Phase 4b: Live-feel thread (poll + unread doorbell)

**Status:** Partially shipped — Contract D merged (dashboard PR #87); Contracts A + B(open-drawer) in dashboard PR #88 (open). The CLOSED-card unread badge is descoped to a deferred follow-on (Larry, 2026-06-23) — it needs a server-driven agent-core funnel-item doorbell projection that isn't built yet.
**Author:** Claude Code (web session, 2026-06-23)
**Approver:** Larry
**Parent:** [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md)
**Predecessor:** [Phase 4 — Operator Meaning Layer](missions-v2-phase4-meaning-layer.md) (shipped; async thread via CLARIFY rails) · [e4-4f Missions tab v1](e4-4f-missions-tab-v1.md) (SWR kanban polling, §5.7)
**Build path:** build-sequence orchestrator, single-repo (dashboard); agent-core touched only by Contract C — a one-field `id` projection on the existing `/thread` response + guard tests (no new endpoint, no DB migration)

---

## 1. Purpose

The "Talk to the team" thread on a Missions card is **write-once-read-stale**. When Larry posts a message, his line appears (optimistic UI), but **Beacon's reply never arrives until Larry manually refreshes the whole browser**. That breaks the core promise of the card — *decide and close in the moment* — and makes the team feel unresponsive even when Beacon answered seconds ago.

Root cause (confirmed 2026-06-23): the kanban cards poll on the SWR cadence from `e4-4f` §5.7 (10/30/60s), but the **on-demand conversation drawer** (`ClarifyRoundDrawer` + `ClarifyReplyBox`, mounted per Phase 4 §8) fetches `GET /api/missions/captures/{id}/thread` **once on open** — no `refreshInterval`, no push. Beacon's reply lands in `chain_events` and sits there invisibly. Phase 4 explicitly deferred the "feels truly live" upgrade to Phase 4b (Phase 4 §12).

Phase 4b delivers the **perceived-live** experience with the smallest, lowest-risk change: **poll the open thread** and **signal unread replies** on the card. The work is almost entirely dashboard-side; the only agent-core change is Contract C — a one-field projection surfacing the existing `chain_events.event_id` as a per-message `id` on the `/thread` response (no new endpoint, no DB migration), which the client needs to dedupe and mark-as-seen.

**Done-gate (this phase):** Larry opens a card's thread, asks a question, and **Beacon's reply appears on its own within ~5 seconds — no browser refresh**. Inside the open drawer, a reply that lands while he's scrolled up surfaces a **"N new ↓" pill**, and a manual refresh forces an immediate pull.

> **Deferred follow-on — CLOSED-card unread badge.** Showing an unread dot/count on a card whose drawer is *closed* is moved out of this phase. Contract A only polls the **open** thread, so a closed card has no client-side signal that a reply arrived; detecting it needs a **server-driven agent-core funnel-item doorbell projection** that isn't built yet. Tracked as a follow-on — see § 2, § 5, and § 11.

---

## 2. Scope decision

| In scope (this phase) | Deferred |
|---|---|
| **A — poll the open thread** (SWR `refreshInterval` while the drawer is open and the tab is visible) | True low-latency **push** responder (SSE / Supabase Realtime) — the original "near-live Beacon front desk" (Phase 4 §12) → Phase 4c, only if polling latency proves annoying |
| **B(open-drawer) — in-drawer unread + manual refresh** ("N new ↓" pill when an open-but-scrolled-up drawer receives a `team_to_larry` reply; manual refresh button; loud-vs-quiet reflected on the message inside the open thread; reconcile the optimistic send so it never double-renders) | **B(closed-card) — unread badge on a *closed* card** (dot/count when a reply lands while the drawer is shut) → deferred follow-on: needs a server-driven agent-core **funnel-item doorbell projection** (the client can't poll a thread it hasn't opened) |
| **D — clear the input on confirmed send** (empty the reply box after a successful POST; restore on failure) | Dashboard-wide rollout of the conversation card to Approvals / Operations / Alerts (Phase 4 §12); rich composer features (drafts, attachments, markdown preview) — out of arc |

This is the lightweight realization of Phase 4b. It reuses the SWR polling pattern already proven on the kanban (`e4-4f` §5.7) and the doorbell signal already live from Phase 4 §9 — **assembly, not greenfield.**

---

## 3. Reuse map

Verified against `scripts/dashboard_api.py` on 2026-06-23:

| Capability | Reuses (existing) | Mode |
|---|---|---|
| Thread read, already poll-shaped | `GET /api/missions/captures/{id}/thread` → `CaptureThreadResponse { messages[], last_synced_at }` (read-only, token-gated) | direct |
| Per-message direction + unread hint | `CaptureThreadMessage.direction` (`larry_to_team` \| `team_to_larry`) and `.needs_reply` | direct |
| Per-message stable `id` | `chain_events.event_id` already exists per turn (`compute_event_id`); Contract C projects it as `CaptureThreadMessage.id` | extend (project) |
| Poll cadence + pause-when-hidden | SWR config pattern from `e4-4f` §5.7 (kanban 10/30/60s) | pattern |
| Loud-vs-quiet "blocked on you" | `larry_alerts` + `alert_triage_state` doorbell (Phase 4 §9); POST message already clears it | reuse |
| Thread render + input | `ClarifyRoundDrawer`, `ClarifyReplyBox`, `PanelErrorBoundary` (Phase 4 §8) | extend |
| Relative timestamps | `RelativeTime` | direct |

**No new backend endpoint and no DB migration.** The contract already returns everything polling needs **except a per-message `id`**: today `CaptureThreadMessage` / `_shape_thread_message` project only `ts`/`direction`/`text`/`actor`/`needs_reply` (verified `scripts/dashboard_api.py`). The dedupe/unread logic (Contracts A/B) keys on a stable `id`, so Contract C adds it by **surfacing the existing `chain_events.event_id` as `id`** — a one-field response-model projection on the existing `/thread` endpoint, **not** a new endpoint and **not** a DB schema change (the `event_id` column already exists).

---

## 4. Contract A — poll the open thread (dashboard)

Wrap the thread drawer's fetch in SWR (or the existing data-fetch hook used for the kanban) with:

- **`refreshInterval` ≈ 5000ms** while the drawer is **open AND the tab is visible**. Reuse the same visibility/pause behavior the kanban poller uses — **stop polling when the drawer is closed or `document.hidden`** (no background load, matches `e4-4f` §5.7 discipline).
- **`revalidateOnFocus: true`** so tabbing back to the window refetches immediately (cheap win, complements B).
- **Key by `capture_id`** so each open card polls only its own thread.
- **Dedupe + merge, don't clobber:**
  - Identify messages by their stable `id` (the per-message `id` Contract C projects from the existing `chain_events.event_id`); the poll response replaces the rendered list by id.
  - **Reconcile the optimistic send:** when the server copy of Larry's just-sent message arrives (same text/direction `larry_to_team`), drop the optimistic placeholder — never render it twice.
- **No scroll-jump:** only auto-scroll to the newest message **if the user is already pinned to the bottom**. If they've scrolled up to read history, hold their scroll position and surface the "N new ↓" pill from Contract B instead.
- **Error/empty:** keep the existing `PanelErrorBoundary`; a failed poll must not blank the thread — show the last good messages and a subtle retry.

Acceptance: with a card thread open, a `team_to_larry` reply written to `chain_events` appears in the drawer within ~5s with **zero manual refresh**, no duplicate of Larry's own message, and no scroll yank if he was reading history.

## 5. Contract B(open-drawer) — in-drawer unread + manual refresh (dashboard)

So a reply isn't missed when the **open** drawer is scrolled away from the newest message:

- **"N new ↓" pill** inside an open-but-scrolled-up drawer: when Contract A's poll brings a **new `team_to_larry` message** below the fold, show the pill; clicking it scrolls to newest and clears the in-drawer unread state. Track "last seen" within the open drawer (client-side, e.g. last seen message `id`/timestamp).
- **Loud vs quiet inside the open drawer stays server-driven:** the **blocked-on-you** signal (`needs_reply` on the message; Phase 4 §9) already distinguishes a reply that's waiting on Larry from an FYI; reflect it visually on the relevant message in the open thread — do **not** add a second notification system.
- **Manual "Refresh" control** in the drawer header: triggers an immediate revalidate. Cheap insurance for the impatient case and for when polling is paused.
- **Optimistic send unchanged:** Larry's line still appears instantly on submit; Contract A's reconcile keeps it from duplicating once the server echoes it back.

Acceptance: with the drawer open and scrolled up, a `team_to_larry` reply lights the "N new ↓" pill; clicking it jumps to the message and clears the pill; the manual refresh button forces an immediate pull; a `needs_reply`/blocked message reads visibly louder than an FYI one.

> **Deferred — B(closed-card) unread badge.** Lighting an unread dot/count on a card whose drawer is **closed** is moved to a follow-on (Larry, 2026-06-23). Rationale: Contract A only polls the **open** thread, so a closed card has no client-side signal that a reply arrived. Detecting it requires a **server-driven agent-core funnel-item doorbell projection** — a per-capture unread/blocked flag surfaced on the funnel/card-list response — which is not built yet. That projection (and the card-badge UI that consumes it) is the follow-on; this phase ships only the open-drawer affordances above. **Implementing the projection is out of scope here** (see § 11).

## 6. Contract D — clear the input on confirmed send (dashboard)

Today the reply box (`ClarifyReplyBox`) keeps the typed text after send, so Larry has to clear it by hand and risks re-sending. Fix the submit flow to be **clear-on-success**:

- On submit, **disable** the input/send button and keep the in-flight text in component state.
- **Clear the input only after the `POST /api/missions/captures/{id}/message` resolves successfully** — set the controlled value back to empty, re-enable, and (per Contract A) reconcile the optimistic line with the server echo so the sent message shows in the thread, not in the box.
- **On failure, restore the text** to the input (or leave it untouched if you never optimistically cleared) and surface the existing error toast — Larry must never lose what he typed to a failed send.
- Guard against **double-send**: the disabled-while-in-flight state plus clear-on-success prevents an Enter-mash from posting twice; ignore empty/whitespace-only submits.
- Keyboard: Enter-to-send (Shift+Enter newline) should follow the same clear-on-success path as the button.

Acceptance: typing a message and sending it leaves the input **empty** the moment the server confirms; the message appears once in the thread; a failed send leaves the text in the box with an error, no duplicate post.

## 7. Contract C — backend (agent-core): project the id, then guard

**No new endpoint and no DB migration.** Two pieces, both small and confined to the existing `/thread` read path:

1. **Project the per-message `id` (one-field response-model add).** Contracts A/B dedupe and track "last seen" by a stable per-message `id`, but the current contract does **not** return one — `CaptureThreadMessage` and `_shape_thread_message` (`scripts/dashboard_api.py`) project only `ts`/`direction`/`text`/`actor`/`needs_reply`. Surface the **existing** `chain_events.event_id` (already computed per turn via `compute_event_id`, already read on each `ev` in `_card_thread_messages`) as a new `id` field: add `id: Optional[str]` to `CaptureThreadMessage` and emit `'id': ev.get('event_id')` from `_shape_thread_message`. This is a **projection of a column that already exists** — explicitly **NOT** a new endpoint and **NOT** a DB schema/migration change.
2. **Guard the contract so a future change can't silently regress it:**
   - Add/extend a test asserting `GET /api/missions/captures/{id}/thread` returns `messages` **oldest-first** with the projected stable `id` (== the row's `event_id`), plus `ts`, `direction`, and `needs_reply`, plus a fresh `last_synced_at` — i.e. the fields polling and unread-detection rely on.
   - Add/extend a test asserting `POST /api/missions/captures/{id}/message` **clears the blocked-on-you doorbell** (already implemented; pin it so the loud→quiet transition the UI reflects stays true).

The doorbell-clear guard may already exist as coverage; if so that sub-step is a no-op. The `id` projection (piece 1) is the only net-new behavior, and it adds no endpoint and no migration.

---

## 8. Build plan — 2 steps (single-repo: dashboard, with an agent-core guard)

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **1 — id projection + contract guard** | agent-core | Project the existing `event_id` as a per-message `id` on `/thread` (one-field, no endpoint/migration) + tests pinning the `/thread` read shape and the POST→doorbell-clear (Contract C) | — |
| **2 — live-feel thread** | dashboard | Contract A (poll open thread) + Contract B(open-drawer) (in-drawer "N new ↓" pill / manual refresh / loud-vs-quiet reflected inside the open drawer) + Contract D (clear input on confirmed send); reuse SWR `e4-4f` §5.7 pattern + `ClarifyRoundDrawer`/`ClarifyReplyBox`/`PanelErrorBoundary`. **B(closed-card) unread badge is descoped to a follow-on** (needs the agent-core funnel-item doorbell projection). | 1 |

> **Quick win:** Contract D is a standalone, low-risk change (clear the controlled input on POST success) and can ship first/independently of A and B if a faster fix is wanted before the full polling work lands.

Step 2 is the substance; step 1 is a thin agent-core guard so the dashboard can rely on the contract. They can run in parallel after step 1's contract is confirmed.

---

## 9. Test / proof plan

- **Contract A:** drawer-open poll picks up a seeded `team_to_larry` reply within one interval; optimistic `larry_to_team` message reconciles to its server copy with no duplicate; scrolled-up state does not auto-scroll.
- **Contract B(open-drawer):** a reply arriving while the drawer is open-but-scrolled-up lights the "N new ↓" pill; clicking it scrolls to the message and clears it; manual refresh forces a revalidate; `needs_reply`/blocked renders louder than FYI. *(The closed-card unread badge is a deferred follow-on and is not tested here.)*
- **Contract D:** a successful send empties the input and shows the message once in the thread; a mocked failed send leaves the text in the box, shows an error, and posts nothing twice; whitespace-only submit is ignored; rapid double-Enter posts once.
- **Contract C (agent-core):** the two guard tests above are green.
- **End-to-end (the real gate):** Larry opens a real parked card, asks Beacon a question, and **watches the answer appear without touching refresh**; if he scrolls up to read history, a "N new ↓" pill flags the newer reply. *(Spotting an unread badge on a card he wasn't looking at is the deferred closed-card follow-on.)* That lived experience is the done-gate — not green unit tests.

## 10. Beacon kickoff & sequencing (single source of truth)

> **Status (2026-06-23):** Contract D shipped — **dashboard PR #87 (merged)**. Contracts A + B(open-drawer) are in **dashboard PR #88 (open)**. The **CLOSED-card unread badge** is descoped from this phase to a deferred follow-on (Larry, 2026-06-23) — it requires a server-driven agent-core funnel-item doorbell projection that isn't built yet (see § 2, § 5, § 11). The sequencing detail below is the original plan, retained for provenance.

**Sequencing decision (Larry, 2026-06-23):** ship **Contract D first as a standalone fast PR**, then **A+B as the follow-on**. D is a tiny, low-risk UX fix (clear the box on confirmed send) that delivers value immediately and depends on nothing else. A/B (polling + unread) is the larger change and **requires the agent-core Contract C `id` projection merged first** (surfaced by Mirror's review of this spec, 2026-06-23 — A/B dedupe and mark-as-seen by a per-message `id` the `/thread` shape didn't expose). Order: **Contract C (agent-core) → A/B (dashboard)**; Contract D can land any time in parallel.

**Repo split (orchestrator V1 is single-repo, build-sequence spec § 4):**
- **Dashboard sequence `phase4b-live-thread-001`** (`ourliberty-dashboard`): step `clear-input` (Contract D) → step `live-thread` (Contracts A+B, `depends_on: clear-input`). Sequential, not parallel — both touch the same thread surface (`ClarifyReplyBox`/`ClarifyRoundDrawer`), so serialize to avoid file-overlap conflicts.
- **Agent-core projection + guard (Contract C)** (`ourliberty-agent-core`): a single APPROVAL_REQUEST that surfaces the existing `chain_events.event_id` as a per-message `id` on `/thread` (one-field projection, no new endpoint, no DB migration) + guard tests. **Prerequisite for the A+B step** (the client dedupes / marks-seen by that `id`), so it must merge **before** `live-thread`. Not skippable — the field isn't projected yet. Contract D does not depend on it. **Dispatch this first.**

**Draft sequence file** — `~/agents/blackboard/build-sequences/phase4b-live-thread-001.json` (Beacon synthesizes/validates per build-sequence spec § 5.1; `dispatch_text` ≤500 chars, points at this spec, no inline design):

```jsonc
{
  "seq_id": "phase4b-live-thread-001",
  "label": "Missions Phase 4b — clear-input (D) then live thread (A+B)",
  "spec_doc": "agents/beacon/specs/missions-v2-phase4b-live-thread-poll.md",
  "target_repo": "ourliberty-dashboard",
  "status": "pending",
  "current_steps": ["clear-input"],
  "steps": [
    {
      "step_id": "clear-input",
      "label": "Contract D — clear reply box on confirmed send",
      "depends_on": [],
      "dispatch_text": "Implement Phase 4b Contract D in the missions card reply box (ClarifyReplyBox): clear the controlled input ONLY after POST /api/missions/captures/{id}/message succeeds; restore text + error toast on failure; disable-in-flight to block double-send; ignore whitespace-only; Enter follows same path. Spec § 6 (Contract D); acceptance therein. Mirror focus: no clear-before-success path, no lost text on failed send, no double-post."
    },
    {
      "step_id": "live-thread",
      "label": "Contracts A+B(open-drawer) — poll open thread + in-drawer unread/refresh",
      "depends_on": ["clear-input"],
      "dispatch_text": "Implement Phase 4b Contracts A+B(open-drawer) on the missions card thread: SWR refreshInterval ~5s on GET .../thread while drawer open AND tab visible (pause when hidden/closed); dedupe by message id; reconcile optimistic send; no scroll-jump. Plus 'N new down' pill for unseen team_to_larry replies in an open-but-scrolled-up drawer, manual refresh, reflect blocked-on-you inside the open drawer. CLOSED-card unread badge is deferred (needs the funnel-item doorbell projection). Spec §§ 4-5. Mirror focus: poll-pause discipline, optimistic reconcile, no duplicate render."
    }
  ]
}
```

**Agent-core guard one-off** — single APPROVAL_REQUEST, `target_repo: ourliberty-agent-core`, dispatch_text: *"Phase 4b Contract C: surface the existing chain_events.event_id as a per-message `id` on GET /api/missions/captures/{id}/thread — add id to CaptureThreadMessage and emit ev['event_id'] from _shape_thread_message (one-field projection, NO new endpoint, NO DB migration). Then guard tests: assert thread returns messages oldest-first with stable id(==event_id)/direction/ts/needs_reply + fresh last_synced_at; assert POST .../message clears the blocked-on-you doorbell. Spec § 7. Mirror focus: id is a projection of an existing column, no schema/endpoint change; tests pin the contract the dashboard relies on."*

**What Larry pastes to Beacon** is the short intent below; Beacon synthesizes the file above, runs the Mirror DAG preflight (build-sequence spec discipline 3), and emits the kickoff — Larry's role is approving the plan, not authoring the file (build-sequence spec decision J).

> **Kickoff intent (paste to Beacon):**
> Beacon — build Missions Phase 4b from `agents/beacon/specs/missions-v2-phase4b-live-thread-poll.md`. **First** dispatch Contract C as a one-off against `ourliberty-agent-core` — it surfaces the existing `chain_events.event_id` as a per-message `id` on the `/thread` response (one-field projection, no new endpoint, no DB migration) plus guard tests; it's a prerequisite for A+B (not skippable — the field isn't projected yet). **Then** run the dashboard sequence `phase4b-live-thread-001` against `ourliberty-dashboard`: step `clear-input` (Contract D — clear the reply box on confirmed send, the standalone quick win, no deps) → step `live-thread` (Contracts A+B(open-drawer) — poll the open thread + in-drawer "N new ↓" pill/manual refresh, `depends_on: clear-input`, and only after Contract C has merged; the CLOSED-card unread badge is deferred to a follow-on). Synthesize the sequence per § 10, run the Mirror DAG preflight, and bring me the kickoff to approve. Contract D can ship in parallel with C if you want it out fast. Don't add SSE/Realtime — that's the deferred Phase 4c.

## 11. Out of scope (later / separate)

- **CLOSED-card unread badge + its server-driven agent-core funnel-item doorbell projection** — lighting a dot/count on a card whose thread drawer is *closed*. Deferred follow-on (Larry, 2026-06-23): Contract A only polls the *open* thread, so a closed card has no client signal that a reply arrived; detecting it needs a per-capture unread/blocked flag projected onto the funnel/card-list response (the "funnel-item doorbell projection"), which doesn't exist yet. The follow-on builds that projection in agent-core, then the card-badge UI that consumes it. **Implementing the projection is out of scope here.**
- **True push** (SSE stream from `dashboard_api.py`, or a Supabase Realtime subscription on `chain_events`) — the original low-latency "Beacon front desk" → **Phase 4c**, pursued only if 5s polling latency proves annoying in practice.
- **Dashboard-wide** conversation card (Approvals / Operations / Alerts) → its own design pass (Phase 4 §12).
- **Multi-voice** (addressing a specific agent vs Beacon), drag-drop, Programs↔Missions unification → out of arc.
