# Spec: Missions v2 — Phase 4b: Live-feel thread (poll + unread doorbell)

**Status:** Draft — ready to sequence
**Author:** Claude Code (web session, 2026-06-23)
**Approver:** Larry
**Parent:** [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md)
**Predecessor:** [Phase 4 — Operator Meaning Layer](missions-v2-phase4-meaning-layer.md) (shipped; async thread via CLARIFY rails) · [e4-4f Missions tab v1](e4-4f-missions-tab-v1.md) (SWR kanban polling, §5.7)
**Build path:** build-sequence orchestrator, single-repo (dashboard); agent-core unchanged except tests

---

## 1. Purpose

The "Talk to the team" thread on a Missions card is **write-once-read-stale**. When Larry posts a message, his line appears (optimistic UI), but **Beacon's reply never arrives until Larry manually refreshes the whole browser**. That breaks the core promise of the card — *decide and close in the moment* — and makes the team feel unresponsive even when Beacon answered seconds ago.

Root cause (confirmed 2026-06-23): the kanban cards poll on the SWR cadence from `e4-4f` §5.7 (10/30/60s), but the **on-demand conversation drawer** (`ClarifyRoundDrawer` + `ClarifyReplyBox`, mounted per Phase 4 §8) fetches `GET /api/missions/captures/{id}/thread` **once on open** — no `refreshInterval`, no push. Beacon's reply lands in `chain_events` and sits there invisibly. Phase 4 explicitly deferred the "feels truly live" upgrade to Phase 4b (Phase 4 §12).

Phase 4b delivers the **perceived-live** experience with the smallest, lowest-risk change: **poll the open thread** and **signal unread replies** on the card. The backend contract is already poll-ready — this is a dashboard-only change.

**Done-gate:** Larry opens a card's thread, asks a question, and **Beacon's reply appears on its own within ~5 seconds — no browser refresh**. If he navigates away, the card shows an **unread badge** when a reply arrives, and clicking back in clears it.

---

## 2. Scope decision

| In scope (this phase) | Deferred |
|---|---|
| **A — poll the open thread** (SWR `refreshInterval` while the drawer is open and the tab is visible) | True low-latency **push** responder (SSE / Supabase Realtime) — the original "near-live Beacon front desk" (Phase 4 §12) → Phase 4c, only if polling latency proves annoying |
| **B — unread + manual refresh affordance** (badge on the card when a `team_to_larry` reply arrives unseen; "N new ↓" pill; manual refresh button; drive loud/quiet from the existing doorbell) | Dashboard-wide rollout of the conversation card to Approvals / Operations / Alerts (Phase 4 §12) |
| **D — clear the input on confirmed send** (empty the reply box after a successful POST; restore on failure) | Rich composer features (drafts, attachments, markdown preview) — out of arc |

This is the lightweight realization of Phase 4b. It reuses the SWR polling pattern already proven on the kanban (`e4-4f` §5.7) and the doorbell signal already live from Phase 4 §9 — **assembly, not greenfield.**

---

## 3. Reuse map

Verified against `scripts/dashboard_api.py` on 2026-06-23:

| Capability | Reuses (existing) | Mode |
|---|---|---|
| Thread read, already poll-shaped | `GET /api/missions/captures/{id}/thread` → `CaptureThreadResponse { messages[], last_synced_at }` (read-only, token-gated) | direct |
| Per-message direction + unread hint | `CaptureThreadMessage.direction` (`larry_to_team` \| `team_to_larry`) and `.needs_reply` | direct |
| Poll cadence + pause-when-hidden | SWR config pattern from `e4-4f` §5.7 (kanban 10/30/60s) | pattern |
| Loud-vs-quiet "blocked on you" | `larry_alerts` + `alert_triage_state` doorbell (Phase 4 §9); POST message already clears it | reuse |
| Thread render + input | `ClarifyRoundDrawer`, `ClarifyReplyBox`, `PanelErrorBoundary` (Phase 4 §8) | extend |
| Relative timestamps | `RelativeTime` | direct |

**No new backend endpoint.** The contract already returns everything polling needs.

---

## 4. Contract A — poll the open thread (dashboard)

Wrap the thread drawer's fetch in SWR (or the existing data-fetch hook used for the kanban) with:

- **`refreshInterval` ≈ 5000ms** while the drawer is **open AND the tab is visible**. Reuse the same visibility/pause behavior the kanban poller uses — **stop polling when the drawer is closed or `document.hidden`** (no background load, matches `e4-4f` §5.7 discipline).
- **`revalidateOnFocus: true`** so tabbing back to the window refetches immediately (cheap win, complements B).
- **Key by `capture_id`** so each open card polls only its own thread.
- **Dedupe + merge, don't clobber:**
  - Identify messages by their stable `id`; the poll response replaces the rendered list by id.
  - **Reconcile the optimistic send:** when the server copy of Larry's just-sent message arrives (same text/direction `larry_to_team`), drop the optimistic placeholder — never render it twice.
- **No scroll-jump:** only auto-scroll to the newest message **if the user is already pinned to the bottom**. If they've scrolled up to read history, hold their scroll position and surface the "N new ↓" pill from Contract B instead.
- **Error/empty:** keep the existing `PanelErrorBoundary`; a failed poll must not blank the thread — show the last good messages and a subtle retry.

Acceptance: with a card thread open, a `team_to_larry` reply written to `chain_events` appears in the drawer within ~5s with **zero manual refresh**, no duplicate of Larry's own message, and no scroll yank if he was reading history.

## 5. Contract B — unread + manual refresh affordance (dashboard)

So a reply isn't missed when the drawer is closed or scrolled away:

- **Unread badge on the card:** when polling brings a **new `team_to_larry` message** that Larry hasn't seen (drawer closed, or open-but-scrolled-up), show a dot/count badge on the card's chat affordance. Track "last seen" per `capture_id` (client-side, e.g. last seen message `id`/timestamp); **clear on view** (drawer opened and scrolled to that message).
- **"N new ↓" pill** inside an open-but-scrolled-up drawer; clicking it scrolls to newest and clears unread.
- **Loud vs quiet stays server-driven:** the **blocked-on-you** doorbell already fires when the team is waiting on Larry, and posting a message already clears it (Phase 4 §9; `needs_reply` on the message). Phase 4b only needs to **reflect that state visually on the card** (e.g. a louder badge when `needs_reply`/blocked) — do **not** add a second notification system.
- **Manual "Refresh" control** in the drawer header: triggers an immediate revalidate. Cheap insurance for the impatient case and for when polling is paused.
- **Optimistic send unchanged:** Larry's line still appears instantly on submit; Contract A's reconcile keeps it from duplicating once the server echoes it back.

Acceptance: a reply that arrives while the drawer is closed lights an unread badge on the card; opening the card clears it; the manual refresh button forces an immediate pull; a thread the team is blocked-on-you on reads visibly louder than an FYI one.

## 6. Contract D — clear the input on confirmed send (dashboard)

Today the reply box (`ClarifyReplyBox`) keeps the typed text after send, so Larry has to clear it by hand and risks re-sending. Fix the submit flow to be **clear-on-success**:

- On submit, **disable** the input/send button and keep the in-flight text in component state.
- **Clear the input only after the `POST /api/missions/captures/{id}/message` resolves successfully** — set the controlled value back to empty, re-enable, and (per Contract A) reconcile the optimistic line with the server echo so the sent message shows in the thread, not in the box.
- **On failure, restore the text** to the input (or leave it untouched if you never optimistically cleared) and surface the existing error toast — Larry must never lose what he typed to a failed send.
- Guard against **double-send**: the disabled-while-in-flight state plus clear-on-success prevents an Enter-mash from posting twice; ignore empty/whitespace-only submits.
- Keyboard: Enter-to-send (Shift+Enter newline) should follow the same clear-on-success path as the button.

Acceptance: typing a message and sending it leaves the input **empty** the moment the server confirms; the message appears once in the thread; a failed send leaves the text in the box with an error, no duplicate post.

## 7. Contract C — backend (agent-core): confirm-and-guard only

No new endpoint or schema. The work here is to **lock the contract Phase 4b depends on** so a future change can't silently regress it:

- Add/extend a test asserting `GET /api/missions/captures/{id}/thread` returns `messages` **oldest-first** with stable `id`, `direction`, `ts`, and `needs_reply`, plus a fresh `last_synced_at` — i.e. the fields polling and unread-detection rely on.
- Add/extend a test asserting `POST /api/missions/captures/{id}/message` **clears the blocked-on-you doorbell** (already implemented; pin it so the loud→quiet transition the UI reflects stays true).

If both already exist, this step is a no-op beyond confirming coverage.

---

## 8. Build plan — 2 steps (single-repo: dashboard, with an agent-core guard)

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **1 — contract guard** | agent-core | Tests pinning the `/thread` read shape + the POST→doorbell-clear (Contract C) | — |
| **2 — live-feel thread** | dashboard | Contract A (poll open thread) + Contract B (unread badge / pill / manual refresh / loud-vs-quiet reflect) + Contract D (clear input on confirmed send); reuse SWR `e4-4f` §5.7 pattern + `ClarifyRoundDrawer`/`ClarifyReplyBox`/`PanelErrorBoundary` | 1 |

> **Quick win:** Contract D is a standalone, low-risk change (clear the controlled input on POST success) and can ship first/independently of A and B if a faster fix is wanted before the full polling work lands.

Step 2 is the substance; step 1 is a thin agent-core guard so the dashboard can rely on the contract. They can run in parallel after step 1's contract is confirmed.

---

## 9. Test / proof plan

- **Contract A:** drawer-open poll picks up a seeded `team_to_larry` reply within one interval; optimistic `larry_to_team` message reconciles to its server copy with no duplicate; scrolled-up state does not auto-scroll.
- **Contract B:** a reply arriving with the drawer closed sets the unread badge; opening + viewing clears it; manual refresh forces a revalidate; `needs_reply`/blocked renders louder than FYI.
- **Contract D:** a successful send empties the input and shows the message once in the thread; a mocked failed send leaves the text in the box, shows an error, and posts nothing twice; whitespace-only submit is ignored; rapid double-Enter posts once.
- **Contract C (agent-core):** the two guard tests above are green.
- **End-to-end (the real gate):** Larry opens a real parked card, asks Beacon a question, and **watches the answer appear without touching refresh**; later he sees an unread badge on a card he wasn't looking at. That lived experience is the done-gate — not green unit tests.

## 10. Out of scope (later / separate)

- **True push** (SSE stream from `dashboard_api.py`, or a Supabase Realtime subscription on `chain_events`) — the original low-latency "Beacon front desk" → **Phase 4c**, pursued only if 5s polling latency proves annoying in practice.
- **Dashboard-wide** conversation card (Approvals / Operations / Alerts) → its own design pass (Phase 4 §12).
- **Multi-voice** (addressing a specific agent vs Beacon), drag-drop, Programs↔Missions unification → out of arc.
