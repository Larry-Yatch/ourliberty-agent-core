# Spec: Missions v2 — Phase 4b.2: Closed-card doorbell (deferred unread badge)

**Status:** Draft — canonical spec for the closed-card unread badge deferred out of Phase 4b (Larry, 2026-06-23). Spec-first, doc-only; the 2-step build sequence follows after this doc merges.
**Author:** Forge (spec-first doc PR, 2026-06-23)
**Approver:** Larry
**Parent:** [Phase 4b — Live-feel thread (poll + unread doorbell)](missions-v2-phase4b-live-thread-poll.md) (§ 2, § 5, § 11 defer this work)
**Predecessor:** [Phase 4 — Operator Meaning Layer](missions-v2-phase4-meaning-layer.md) (§ 9 — the loud-vs-quiet `needs_reply` doorbell) · [e4-4f Missions tab v1](e4-4f-missions-tab-v1.md) (§ 5.7 SWR kanban polling; § 5.2 the captures card-list payload)
**Build path:** 2 steps — Contract E (agent-core: project a per-card doorbell signal onto the existing captures card-list response + guard tests, no new endpoint, no DB migration) → Contract F (dashboard: render the closed-card badge from it). Single-repo per step.

---

## 1. Purpose

Phase 4b made an **open** Missions card thread feel live: while the conversation drawer is open, Contract A polls it and Beacon's reply appears within ~5s, with an in-drawer "N new ↓" pill for replies that land below the fold. But Phase 4b explicitly **deferred** the case where the drawer is **closed**: a reply lands on a card Larry isn't looking at, and **nothing on the card tells him it arrived** (parent § 2, § 5, § 11).

Root cause (parent § 5, confirmed 2026-06-23): Contract A only polls the **open** thread. A closed card has **no client-side signal** that a `team_to_larry` reply was written to `chain_events` — the client can't poll a thread it hasn't opened. Detecting it needs a **server-driven** signal: the kanban already polls the card-list payload on its SWR cadence (`e4-4f` § 5.7), so if the **server** projects a compact per-card "newest reply" doorbell onto that payload, the closed card gets an unread signal for free — no new poll, no thread fetch.

Phase 4b.2 delivers exactly that, as the smallest server-side addition: **project a compact doorbell signal onto the card-list payload the kanban already polls** (Contract E), then **render a closed-card badge from it** (Contract F).

**Done-gate (this phase):** a `team_to_larry` reply lands on a card whose drawer is **closed**; within one kanban poll interval the card shows an **unread badge** (a louder "blocked on you" treatment when the newest reply has `needs_reply`, a quiet dot otherwise). Opening the card and reading the newest message clears the badge. No browser refresh; no new endpoint; no background thread-polling.

---

## 2. Scope decision

| In scope (this phase) | Deferred / out of arc |
|---|---|
| **E — card-list doorbell projection** (agent-core): enrich the existing `GET /api/system/captures` response so each card carries a compact `doorbell` object — newest `team_to_larry` message `id` + `ts` + a `blocked` flag (from `needs_reply`) — derived from `chain_events` on the read path the kanban already polls | **True push** (SSE / Supabase Realtime) — still Phase 4c, pursued only if poll latency annoys (parent § 11) |
| **F — closed-card badge** (dashboard): render an unread badge on a **closed** card from E's signal; louder when `blocked`, quiet dot otherwise; clear when Larry opens the card and sees the newest message (per-card "last seen" tracked client-side, mirroring Contract B(open-drawer)) | **Open-drawer affordances** ("N new ↓" pill, manual refresh, optimistic reconcile) — already shipped in Phase 4b Contracts A/B(open-drawer) |
| **Guard tests** pinning E's projection shape (newest team reply id/ts + blocked) and its fail-safe degradation | **Dashboard-wide** conversation card on Approvals / Operations / Alerts — its own design pass (parent § 11; Phase 4 § 12) |

This is **assembly, not greenfield.** It reuses the captures read path (`e4-4f` § 5.2), the `card_message` / `team_to_larry` direction convention and the `needs_reply` loud-vs-quiet doorbell (Phase 4 § 9), the `event_id` stable-id projection pattern proven in Phase 4b Contract C, and the kanban's existing SWR poll (`e4-4f` § 5.7). **No new endpoint and no DB migration.**

---

## 3. Reuse map

Verified against `scripts/dashboard_api.py` on 2026-06-23:

| Capability | Reuses (existing) | Mode |
|---|---|---|
| Card-list payload the kanban polls | `GET /api/system/captures` → `CapturesResponse { captures[], last_synced_at, schema_version }` (`get_missions_captures` → `_reader_captures`, read-only, token-gated). Today a **pure file read** of `captures.json` — no `chain_events` consult. | extend (enrich) |
| Per-card thread events to derive the signal from | `chain_events` `card_message` rows, already read by `_card_thread_messages` (selects `event_id,event_type,task_id,agent,pr_url,ts,payload`) | reuse (read pattern) |
| Direction + blocked-on-you flag | `_CARD_MSG_TEAM = 'team_to_larry'` (Beacon's reply) and the message `needs_reply` flag (Phase 4 § 9 loud-vs-quiet doorbell) | direct |
| Stable per-message `id` to key "newest" + client "last seen" | `chain_events.event_id` (`compute_event_id`), already projected as `id` on the `/thread` response by Phase 4b Contract C (`_shape_thread_message`) | direct |
| Poll cadence + pause-when-hidden | SWR config pattern from `e4-4f` § 5.7 (kanban 10/30/60s); the doorbell rides this existing poll | pattern |
| Card render surface | the kanban card component (the Parked lane card) + `RelativeTime` | extend |

**No new backend endpoint and no DB migration.** The captures response is extended in place; the signal is derived from a `chain_events` column (`event_id`) and payload fields (`direction`, `needs_reply`) that already exist.

---

## 4. Contract E — card-list doorbell projection (agent-core)

Enrich the **existing** `GET /api/system/captures` response so each card carries a compact, server-derived unread signal. **No new endpoint; no DB migration; no background thread.** The kanban already polls this payload (`e4-4f` § 5.7), so the doorbell rides that poll — **Contract A's open-thread poll is untouched.**

**4.1 Per-card `doorbell` shape.** For each capture in the `captures[]` array, project an additive `doorbell` object summarizing the newest team reply on that card's thread:

```jsonc
"doorbell": {
  "latest_team_id": "<event_id of the newest team_to_larry card_message, or null>",
  "latest_team_ts": "<ISO ts of that message, or null>",
  "blocked": true,        // newest team_to_larry message has needs_reply == true
  "team_unread_count": 2  // optional: count of team_to_larry messages newer than... (see 4.3)
}
```

- `latest_team_id` / `latest_team_ts` identify the newest `team_to_larry` (`_CARD_MSG_TEAM`) `card_message` for that card. `null` when the card has no team reply yet — a card with no Beacon reply shows no badge.
- `blocked` reflects the **newest** team reply's `needs_reply` (Phase 4 § 9): `true` = Beacon is waiting on Larry (louder badge), `false`/absent = informational reply (quiet dot). Reuse the existing loud-vs-quiet doorbell — **do not add a second notification system.**
- The object is **additive**: existing readers of the captures payload ignore the unknown key; only Contract F consumes it.

**4.2 Derivation, fail-safe.** `_reader_captures` is a pure file read today; the projection adds **one bounded `chain_events` read** for `card_message` rows across the parked captures, grouped by card id (the same read shape `_card_thread_messages` already uses), and computes each card's newest `team_to_larry` id/ts + `needs_reply`. The enrichment **MUST be fail-safe**: if the `chain_events` read errors or is unavailable, each card degrades to `doorbell: null` (or omits the key) and the captures payload still serves its file-read contract — a broken doorbell **never 500s the kanban**, exactly as `_reader_captures` degrades a missing file to an empty list rather than an error.

> **Enforcement:** guard test in § 6 asserts the captures endpoint still returns its `captures[]`/`last_synced_at` contract with `doorbell: null` when the `chain_events` read is stubbed to fail (fail-safe degradation), and the read pattern reuses `_card_thread_messages`' bounded query rather than an unbounded scan.

**4.3 Read-state stays client-side.** The server projects the **signal** (what's newest + whether it's blocking); it does **not** track what Larry has read. Whether the badge lights is the client's call: Contract F compares `latest_team_id`/`latest_team_ts` against a per-card "last seen" the client persists (the same model Contract B(open-drawer) uses for the in-drawer pill). `team_unread_count` is **optional** — include it only if the bounded read can compute it cheaply (count of `team_to_larry` messages on the card); otherwise the client can render a dot from `latest_team_id` alone. **Do not add server-side per-user read state** — out of scope and unnecessary for a single operator.

**Acceptance:** `GET /api/system/captures` returns each card with an additive `doorbell` object whose `latest_team_id`/`latest_team_ts` match the newest `team_to_larry` `card_message` for that card and whose `blocked` matches that message's `needs_reply`; a card with no team reply has `doorbell: null` (or no key); a stubbed `chain_events` failure degrades every card to `doorbell: null` without erroring the endpoint.

---

## 5. Contract F — closed-card badge (dashboard)

Render an unread badge on a **closed** Missions card from Contract E's `doorbell` signal. This is the **closed-card** complement to Phase 4b's open-drawer affordances — it lights a card Larry **isn't** looking at.

- **Badge on a closed card** when the card's `doorbell.latest_team_id` is **newer than the card's client-tracked "last seen"** (per-card, persisted client-side — last newest-`id`/`ts` Larry saw when the drawer was last open). No `doorbell` / `latest_team_id: null` → no badge.
- **Loud vs quiet stays server-driven:** `doorbell.blocked == true` → louder "blocked on you" treatment (Beacon is waiting); `false` → a quiet unread dot. Reuse the Phase 4 § 9 loud-vs-quiet semantics already used inside the open drawer — visually consistent, no second notification channel.
- **Optional count:** if E provides `team_unread_count`, render it on the badge; otherwise a dot suffices.
- **Clear on read:** opening the card and seeing the newest message updates the client "last seen" to `doorbell.latest_team_id`, which clears the badge on the next poll. This is the same "last seen" mechanism Contract B(open-drawer) already maintains — extend it to persist across drawer close, don't invent a parallel store.
- **No new poll:** the badge updates on the kanban's existing SWR captures poll (`e4-4f` § 5.7). **Do not** add a per-card thread poll — that would reintroduce exactly the background-poll cost Contract A's poll-pause discipline avoids.

**Acceptance:** with a card's drawer **closed**, a `team_to_larry` reply written to `chain_events` lights an unread badge on that card within one kanban poll interval — louder when `needs_reply`, a quiet dot otherwise; opening the card and reading the newest message clears the badge; a card with no unseen team reply shows no badge; closing and reopening does not re-light a badge for an already-seen reply.

---

## 6. Contract E guard tests (agent-core)

Pin the projection so a future change to the captures read path can't silently regress the doorbell:

1. **Projection shape.** Seed `chain_events` with `card_message` rows (a `larry_to_team` then a `team_to_larry` with `needs_reply: true`) for a parked capture; assert `GET /api/system/captures` returns that card with `doorbell.latest_team_id ==` the newest `team_to_larry` `event_id`, `latest_team_ts` == its `ts`, and `blocked == true`. A second card with only a `larry_to_team` message (no team reply) gets `doorbell: null` (or no key).
2. **Blocked-vs-quiet.** A card whose newest `team_to_larry` reply has `needs_reply` falsey projects `blocked: false` — pinning the loud→quiet distinction Contract F renders.
3. **Fail-safe degradation.** With the `chain_events` read stubbed to raise, `GET /api/system/captures` still returns its `captures[]` + `last_synced_at` file-read contract with every card degraded to `doorbell: null` — **no 500** (mirrors `_reader_captures`' missing-file degradation).

> **Enforcement:** these three tests live alongside the existing captures/thread coverage in `scripts/tests/`; they pin the contract Contract F (dashboard) relies on, so an agent-core refactor that drops the projection or breaks its fail-safe fails the suite rather than silently dark-ing the badge.

---

## 7. Build plan — 2 steps

| Step | Repo | Scope | depends_on |
|---|---|---|---|
| **1 — doorbell projection + guard** | agent-core | Contract E: enrich `GET /api/system/captures` with the additive per-card `doorbell` object (newest `team_to_larry` id/ts + `blocked` from `needs_reply`), derived via one bounded `chain_events` read on the existing read path, fail-safe to `doorbell: null`; + the § 6 guard tests. **No new endpoint, no DB migration.** | — |
| **2 — closed-card badge** | dashboard | Contract F: render the unread badge on a **closed** kanban card from `doorbell` (louder when `blocked`, quiet dot otherwise), clear on read via a client-persisted per-card "last seen", riding the existing SWR captures poll — no new poll. | 1 |

Step 1 must merge first: the dashboard badge keys on the `doorbell` field, which the captures payload doesn't project yet. They serialize — step 2 has nothing to render until step 1's contract is live.

---

## 8. Test / proof plan

- **Contract E (agent-core):** the three § 6 guard tests are green — projection shape, blocked-vs-quiet, fail-safe degradation.
- **Contract F (dashboard):** a `team_to_larry` reply on a **closed** card lights the badge within one kanban poll; `needs_reply` renders louder than an FYI; opening + reading the newest message clears it; an already-seen reply does not re-light on close/reopen; a card with no team reply shows no badge.
- **End-to-end (the real gate):** Larry is looking at the board (not at a specific card); Beacon replies on a parked card whose drawer is closed; the card **lights an unread badge on its own**, louder if Beacon is blocked on him; he opens it, reads the reply, and the badge clears. Spotting the reply on a card he wasn't watching — the exact gap Phase 4b deferred — is the lived done-gate, not green unit tests.

---

## 9. Out of scope (later / separate)

- **Implementing the contracts.** This is a spec-first doc only; Contracts E and F ship via the 2-step sequence after this doc merges.
- **True push** (SSE stream from `dashboard_api.py`, or a Supabase Realtime subscription on `chain_events`) — the low-latency "Beacon front desk"; still **Phase 4c**, pursued only if the kanban poll latency proves annoying in practice (parent § 11).
- **Wiring the conversation card / doorbell into Approvals / Operations / Alerts** — dashboard-wide rollout is its own design pass (parent § 11; Phase 4 § 12).
- **Server-side per-user read state.** Read-state stays client-side (§ 4.3); a single operator doesn't need server-tracked seen-marks.
- **Open-drawer affordances** ("N new ↓" pill, manual refresh, optimistic reconcile, clear-on-send) — already shipped in Phase 4b Contracts A / B(open-drawer) / D.
