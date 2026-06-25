# Spec: Missions v2 — Funnel-item doorbell projection (closed-card unread badge)

**Status:** Draft — authored as the follow-on Larry approved (dashboard event `f6234cea`, 2026-06-24) when he shipped PR #88 and descoped the closed-card badge.
**Author:** Beacon
**Approver:** Larry (pending review of this draft)
**Parent:** [Phase 4b — Live-feel thread (poll + unread doorbell)](missions-v2-phase4b-live-thread-poll.md)
**Predecessor:** Phase 4b Contract A (live polling of the **open** thread) — shipped in PR #88 (`ourliberty-dashboard`). Phase 4 §9 — the blocked-on-you doorbell (`missions_doorbell` + `alert_triage_state`).
**Build path:** two contracts — one agent-core projection (+ guard tests), one dashboard card render. No new endpoint, no DB migration (projection of signals that already exist server-side).

---

## 1. Purpose

Phase 4b shipped the **open-thread** live experience: with a card's conversation drawer open, Beacon's reply now appears within ~5s, no browser refresh (Contract A). But the **closed-card** half of Phase 4b's done-gate — *"if Larry navigates away, the card shows an unread badge when a reply arrives, and clicking back in clears it"* — was **not buildable dashboard-only** and was descoped at Mirror's review of PR #88.

**Why it couldn't ship in #88 (Mirror's ESCALATE, verified 2026-06-24):** Contract A correctly **stops polling when the drawer is closed** (`swrKey` → null; no background load — the Phase 4b / `e4-4f` §5.7 discipline). So a closed card has **no live data source** to light an unread/loud-vs-quiet badge. And the card-level list payload that the kanban *does* keep polling carries **no doorbell / `needs_reply` / unread field** to drive a closed-card badge. The only dashboard-only alternative — background-polling closed threads — is forbidden by Contract A. Delivering the closed-card badge therefore needs a **server-driven doorbell signal projected onto the card-list payload** (agent-core), which is what this contract adds.

**Done-gate:** a `team_to_larry` reply (or a blocked-on-you doorbell) that arrives while a card's drawer is **closed** lights an **unread badge** on that card — driven by the card-list poll Larry already has running, with **no closed-thread polling**. A card the team is **blocked-on-you** on reads visibly **louder** than an FYI one. Opening the card and viewing the reply **clears** the badge.

---

## 2. Scope decision

| In scope (this contract) | Deferred |
|---|---|
| **E — project a per-card doorbell signal** onto the card-list payload the kanban already polls (agent-core), derived from the **existing** Phase 4 §9 doorbell state + unseen `team_to_larry` replies. + guard tests | True push (SSE / Supabase Realtime) — still Phase 4c, only if polling latency annoys |
| **F — render the closed-card unread badge + loud-vs-quiet** from the projected field (dashboard); clear-on-view | Re-opening the open-thread polling work — already shipped in #88; this contract does not touch the open drawer |
| | Dashboard-wide rollout of the conversation card (Approvals / Operations / Alerts) — Phase 4 §12 |

This is the **assembly** that closes Phase 4b's done-gate: it reuses the doorbell that already fires server-side (Phase 4 §9) and the card-list poll that already runs (`e4-4f` §5.7) — the only missing piece is the **projection** that connects them, plus the card-level render.

---

## 3. Reuse map (to be confirmed at build against `scripts/dashboard_api.py`)

| Capability | Reuses (existing) | Mode |
|---|---|---|
| Blocked-on-you doorbell state | `missions_doorbell` + `alert_triage_state` (Phase 4 §9); `POST .../message` already resolves it (`missions_doorbell.resolve_doorbell`, verified `dashboard_api.py`) | reuse |
| Per-card "is the team waiting on Larry" | `needs_reply` already computed per thread message + on the POST path (`dashboard_api.py` §§5841–6073) | extend (lift to card level) |
| Card list the kanban polls | the existing card/list endpoint that feeds the Missions/Projects kanban (poll cadence `e4-4f` §5.7) | extend (project) |
| Per-message stable id (for "unseen since") | `chain_events.event_id`, now projected as `CaptureThreadMessage.id` (Phase 4b Contract C, shipped) | direct |
| Closed-card badge UI | Phase 4b Contract B badge/pill affordance — now driven by the **list field** instead of the open-thread poll | re-point |

**Build-time verification (one open item, mirrors how Contract C was specified):** confirm the exact card-list response model + builder that the closed Missions/Projects card renders from (the payload the kanban already polls), so Contract E projects the doorbell field onto **that** shape. The contract below is written against "the card-list payload"; the builder pins the concrete model/function name from `dashboard_api.py` before adding the field. No new endpoint is introduced regardless.

---

## 4. Contract E — project the per-card doorbell (agent-core)

Add a **server-derived doorbell signal per card** to the card-list payload the kanban already polls. No new endpoint, no DB migration — derive from signals that already exist:

- **`unread_reply: bool`** — true when the card's most recent `team_to_larry` message is newer than the client's last-seen marker for that card. (Last-seen is tracked client-side per Contract F; the server projects the **latest `team_to_larry` message id/ts** so the client can compare. Surface `last_team_reply_id` / `last_team_reply_ts` for that comparison.)
- **`blocked_on_you: bool`** — true when the card has an **active** blocked-on-you doorbell (`missions_doorbell` / `alert_triage_state`, Phase 4 §9). This is the **loud** state. `POST .../message` already resolves the doorbell, so this flips to false the moment Larry replies (no second notification system — reflect, don't re-invent, per Phase 4b §5).
- Group the three under one nested object (e.g. `doorbell: { unread_reply, blocked_on_you, last_team_reply_id, last_team_reply_ts }`) so the card payload gains exactly one new key and the shape is self-describing.

**Derivation is read-only and best-effort:** if the doorbell lookup fails for a card, project `blocked_on_you: false` and still emit `unread_reply` from the thread tail — a doorbell hiccup must never blank the card list (same posture as the existing best-effort `doorbell_resolved` handling).

**Guard tests:**
- The card-list payload includes the `doorbell` object with `unread_reply` / `blocked_on_you` / `last_team_reply_*` for a card that has a `team_to_larry` reply, and `blocked_on_you: true` when a doorbell is active for that card.
- `POST .../message` clears the doorbell so the next card-list poll projects `blocked_on_you: false` (pins the loud→quiet transition Contract F reflects — extends the Phase 4b Contract C doorbell-clear guard from the thread path to the card path).

Acceptance: the card-list endpoint the kanban polls returns, per card, a doorbell object that distinguishes **no signal** / **unread (quiet)** / **blocked-on-you (loud)**, derived from existing state, with no new endpoint and no migration.

## 5. Contract F — render the closed-card badge (dashboard)

Drive the Phase 4b Contract B badge affordance from the **list field** instead of the (now-absent) closed-thread poll:

- **Unread badge on the card** when `doorbell.unread_reply` is true and the card's drawer is closed (or open-but-scrolled-past). Track **last-seen per `capture_id`** client-side (last-seen `team_to_larry` id/ts); compare against the projected `last_team_reply_id` / `last_team_reply_ts`. **Clear on view** — opening the drawer and reaching that message marks it seen (reuse the Phase 4b Contract B clear-on-view logic).
- **Loud vs quiet:** render a **louder** badge when `doorbell.blocked_on_you` is true (team is waiting on Larry) and a quieter dot for an FYI unread. Do **not** add a second notification channel — this purely reflects the server doorbell.
- **No closed-thread polling:** the badge is fed entirely by the card-list poll Larry already has running. The open-drawer live poll (Phase 4b Contract A) is unchanged and still the source once the drawer is open.
- **Reconcile with the open drawer:** when the drawer is open and Contract A's poll shows the reply, the unread state for that card resolves through the same last-seen marker — no double-counting between the list field and the open poll.

Acceptance: a reply arriving while the card is closed lights an unread badge on the card without any thread polling; a blocked-on-you card reads visibly louder than an FYI one; opening + viewing clears it; opening the drawer reconciles cleanly with the live open-thread poll.

---

## 6. Build plan — 2 steps

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **1 — doorbell projection + guard** | agent-core | Contract E: project `doorbell { unread_reply, blocked_on_you, last_team_reply_* }` per card onto the kanban card-list payload, derived from `missions_doorbell`/`alert_triage_state` + the thread tail; guard tests pinning the projection and the POST→doorbell-clear transition | — |
| **2 — closed-card badge** | dashboard | Contract F: render unread badge + loud-vs-quiet from the projected `doorbell` field; last-seen-per-`capture_id`; clear-on-view; reconcile with the open-drawer poll | 1 |

Step 1 is the prerequisite — the dashboard can't light a closed-card badge until the field is projected (exactly why the badge was descoped from #88). Step 2 re-points the Phase 4b Contract B UI at the new field.

---

## 7. Test / proof plan

- **Contract E (agent-core):** the two guard tests above are green — the card-list payload carries the doorbell object with the right states, and POST→message flips `blocked_on_you` to false on the next projection.
- **Contract F (dashboard):** a seeded `team_to_larry` reply with the drawer closed sets the unread badge; a card with an active doorbell renders louder; opening + viewing clears it; no closed-thread fetch fires (assert the closed card makes no `/thread` request); opening the drawer reconciles without double-counting.
- **End-to-end (the real gate):** Larry is looking at another card / another tab; Beacon replies on a parked card; **an unread badge appears on that card without him opening it**, louder if the team is blocked on him; he clicks in and it clears. That lived experience closes Phase 4b's done-gate.

---

## 8. Out of scope

- **True push** (SSE / Supabase Realtime) — still **Phase 4c**, pursued only if polling latency proves annoying.
- **Re-opening the open-thread poll** — shipped in #88; untouched here.
- **Dashboard-wide** conversation card (Approvals / Operations / Alerts) — Phase 4 §12.

---

## 9. Sequencing note

Single-repo-per-step (orchestrator V1, build-sequence spec § 4): step 1 is an agent-core one-off (Contract E projection + guard); step 2 is the dashboard render (Contract F, `depends_on` step 1). Author the sequence per the multi-step discipline once Larry approves this draft; run the Mirror DAG preflight before kickoff. Until then this is a draft for Larry's review — the one open item is the build-time confirmation of the exact card-list model the closed card renders from (§ 3).
