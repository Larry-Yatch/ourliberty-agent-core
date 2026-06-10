# Spec: Missions v2 — Phase 2: Resurfacing + Relocated Derive

**Status:** Draft — ready to sequence (builds on the locked decisions in the design pass)
**Author:** Claude Code (desktop session, 2026-06-10)
**Approver:** Larry
**Parent:** [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md)
**Predecessor:** [Phase 1 — durable capture + GC](missions-v2-phase1-durable-capture.md) (shipped + proven/live; GC healer ages parked captures)
**Cross-project:** [docs/missions-scene-graph-interface-reply.md](../../../docs/missions-scene-graph-interface-reply.md) · scene-graph preliminary request `ourliberty-graph/docs/scene-graph-missions-request.md`
**Build path:** build-sequence orchestrator, single-repo split (PR-S3 precedent), interface-first, one phase = one sequence

---

## 1. Purpose

Phase 2 is the **timer replacement** plus the **board-readability** pass, both riding on one load-bearing architecture move: relocating the mission-phase **derive** from the dashboard TypeScript to a single droplet-side Python source-of-truth endpoint.

Three operator-facing outcomes + one shared enabler:

- **(enabler) Relocated derive endpoint** — the phase/orphan/aggregate derive moves to the droplet (`scripts/dashboard_api.py`), filterable by repo/task_id, so there is **one derive, no TS↔Python drift**, and derived work-state is agent-reachable (the sibling scene-graph consumer reads the same endpoint the dashboard does).
- **(a) Contextual resurfacing** — touch a repo/mission → its related **parked captures** surface, no clock involved.
- **(b) Parked-&-aging digest** — a daily + on-demand dashboard summary card (Approvals-summary style, `ceo_digest_generator` pattern), reading the `aging` flag Phase 1's GC healer already persists. **Not Telegram** (decision #3).
- **(c) Orphan-lane readability** — per-orphan **state badges**, collapse/hide **terminal** orphans, and **readable labels** (desktop chat title > repo/branch, not the alphanumeric `task_id`); the lane itself collapses.

**Done-gate:** the dashboard Missions tab renders identically to today but reads its derived state from the droplet endpoint (TS derive deleted, parity-test-gated); opening a repo surfaces its parked captures; a daily "parked & aging" card renders on the dashboard; the Orphans lane shows state badges, hides terminal orphans by default, and labels desktop orphans by chat title.

---

## 2. Scope guard (read first)

Phase 2's **primary deliverable is the operator-facing resurfacing + readability** (a/b/c). The derive endpoint is the **shared enabler** — built to the **dashboard's need + cheap filterability + extensibility**, *not* frozen to the scene-graph's full wishlist.

| In scope (build now) | Out of scope (defer) |
|---|---|
| Port the existing derive (phase/aggregate/orphan) to Python, byte-faithful | Re-platforming the derive rules — port verbatim, don't redesign |
| `?repo=` / `?task_id=` filters | File/area-level collision detection (scene-graph ask; needs no data we derive today) |
| New orphan-readability derived fields (state badge, terminal flag, readable label) | A general `blocked_on` model (scene-graph ask; design the field slot, don't populate) |
| `parked[]` array in the response (captures, with `aging`) | A dedicated `?view=scene-graph` flat projection (add at their T8 if asked) |
| Dashboard cutover to the endpoint (parity-gated) | Write-back actions / auto-registration (Phase 3) |
| Daily + on-demand parked-&-aging digest card | Telegram delivery of the digest (decision #3: dashboard only) |
| Contextual resurfacing of parked captures | Programs↔Missions unification, drag-drop (out of arc) |

**The scene-graph consumer is read-only and owned by the other project.** We own the endpoint and its shape. Their final field list arrives as a written request at *their* T8; we design the response **extensible to** §3.5 but build only the dashboard's need now.

---

## 3. Contract D — the relocated derive endpoint

### 3.1 Why this is a drop-in, not a rewrite

The dashboard's [`app/api/missions/list/route.ts`](../../../../ourliberty-dashboard/app/api/missions/list/route.ts) already:
1. fetches `missions.json` from the droplet (`GET /api/system/missions`, `X-Dashboard-Token`),
2. fetches `chain_events` from Supabase for the registered task_ids,
3. derives per-task phase + mission aggregate + orphans (30-day window) via `lib/mission-queries.ts`,
4. returns `MissionListResponse { missions, orphans, last_synced_at, as_of }`.

Steps 2–3 are the only thing that lives in TypeScript. The droplet **already holds both inputs** — `missions.json` is a local file there, and `dashboard_api.py` already has a service-role Supabase client reading `chain_events` (`_fetch_chain_events_for_agent`, `_get_larry_action_supabase_client`). So relocation = **move steps 2–3 to the droplet**; the dashboard route collapses to a thin proxy. No new credential, no new service, no new data path (the 4-artifact credential obligation does **not** trigger).

### 3.2 Request

```
GET /api/missions/derived
GET /api/missions/derived?repo=<target_repo>
GET /api/missions/derived?repo=<target_repo>&task_id=<task_id>
```

- **Auth:** `X-Dashboard-Token` header, same gate as `/api/system/missions` (read-only; the dashboard's own auth proxy still fronts the user-facing read).
- **Filters (both optional, AND-combined):**
  - `repo` — narrows `missions` (entries whose `target_repo`/inferred repo matches), `orphans` (events whose repo matches), and `parked` (captures whose `origin.repo`/area matches) to that repo.
  - `task_id` — narrows to that single task plus its **collisions** (other active tasks on the same repo) — the scene-graph's "don't step on moving work" query.
- **Unfiltered** = the full board (the dashboard's current `/api/missions/list` behavior).
- **Read-only.** No consumer ever writes through this endpoint.

### 3.3 Response (the canonical shape — dashboard's `MissionListResponse`, extended)

```jsonc
{
  "schema_version": 1,
  "missions": [ {
    "id": "…", "name": "…", "phase": "…",          // raw mission entry fields (unchanged)
    "target_repo": "ourliberty-agent-core",
    "tasks": [ {
      "task_id": "…",
      "derived_phase": "drafting|ready|in_flight|awaiting_merge|shipped|deferred",
      "last_event_ts": "…", "pr_url": "…", "pr_state": null, "agent": "…"
    } ],
    "aggregate_phase": "…"                          // mission-level badge
  } ],
  "orphans": [ {
    "task_id": "…",
    "last_event_ts": "…", "agent": "…", "pr_url": "…",
    // NEW in Phase 2 (orphan-readability, § 3.4):
    "derived_phase": "ready|in_flight|awaiting_merge|shipped",
    "state_badge": "building|in-review|stalled|shipped|closed",
    "terminal": true,                  // ONLY shipped/closed (a REAL merge/close signal) → collapsed by default
    "stalled": false,                  // unmerged + quiet > STALE_AFTER_DAYS → kept VISIBLE, flagged for attention
    "label": "Missions tab redesign",  // chat title > repo/branch > task_id
    "repo": "ourliberty-agent-core", "branch": "feat/…"
  } ],
  "parked": [ {                                    // NEW in Phase 2 (resurfacing + scene-graph)
    "capture_id": "cap-…", "title": "…",
    "repo": "ourliberty-agent-core", "area": null,
    "aging": true,                                 // GC healer's persisted flag (Phase 1)
    "last_touched": "…"
  } ],
  "last_synced_at": "…",
  "as_of": "…"
}
```

- `missions` + `orphans` + `last_synced_at` + `as_of` **match the existing `MissionListResponse` byte-for-byte** except for the additive orphan fields in §3.4 — that's what makes the dashboard cutover a proxy swap and what the parity test (§4) asserts.
- `parked` is additive; existing dashboard reads ignore unknown keys.
- `schema_version` is new and lets consumers gate on shape changes.

### 3.4 Derive rules — ported verbatim from `lib/mission-queries.ts`

Port these **without behavior change** (the parity test enforces it): `derivePhaseForTask` (§5.2 rule order), `aggregateMissionPhase` (PHASE_RANK + deferred override), `detectOrphans` + the full infrastructure-task filter set (`INFRASTRUCTURE_AGENT_NAMES`, `INFRASTRUCTURE_TASK_ID_PREFIXES`, `heal-*` prefix, alert-message/test-fixture/review-session heuristics), `summarizeTaskEvents`, `fetchEventsForTaskIds`, `fetchRecentChainEvents` (30-day window).

**New derive logic (orphan readability — capture `cap-orphan-lane-show-state`):**

- **`derived_phase` per orphan** — run the *same* `derivePhaseForTask` on each orphan's events. (Orphans are just task_ids not in a registered mission; nothing stops us deriving their phase.)
- **Hiding is driven by a REAL terminal signal, NEVER by a clock.** This is the safety property: an orphan is only collapsed when there is positive evidence it is *finished or deliberately dropped* — never merely because it went quiet. The danger the time-clock approach created was hiding **stalled-but-incomplete** work (a PR that reached review and then stalled near the finish line is the *most* important orphan, not the least). So:
  - **`shipped`** (terminal) — an `auto_merge` event OR `pr_state == "MERGED"`. Genuinely done. This is the bulk of the orphan noise. **Collapsed by default.**
  - **`closed`** (terminal) — `pr_state == "CLOSED"` and not merged. Deliberately walked away from. **Collapsed by default**, distinct badge from shipped. *(Requires a live PR-state read for orphans carrying a `pr_url` — see cost note below.)*
  - **`building`** — `in_flight`, recent activity. Visible.
  - **`in-review`** — `awaiting_merge`, recent activity. Visible.
  - **`stalled`** (NON-terminal) — unmerged (in_flight / awaiting_merge / open PR / no-PR-yet) AND `last_event_ts` older than `STALE_AFTER_DAYS` (proposed **14 calendar days**, tunable). **Kept VISIBLE and flagged for attention** — this is the overlooked-important bucket; the clock only *surfaces* it, it never hides it. Phase 3 auto-registration actively pulls `stalled` orphans into proposed threads to work — Phase 2's job is just to not lose them.
- **`terminal`** — `true` **only** for `shipped` and `closed`. The dashboard hides terminal orphans behind a "show N done" disclosure; everything else (including `stalled`) stays visible.
- **`stalled`** — separate boolean (true for the `stalled` badge). The dashboard may sort stalled orphans *up* (attention), never down.
  - **Enforcement:** `STALE_AFTER_DAYS` is a single module constant with a unit test asserting (a) a 15-days-quiet unmerged orphan is `stalled:true, terminal:false` (visible), and (b) a merged orphan is `terminal:true` regardless of age. The test pins the invariant that *no unmerged orphan is ever `terminal`*.
  - **Cost note (PR-state reads):** `shipped` via `auto_merge` event is free (already in the event stream). Detecting `closed` (and confirming `merged` where the event is absent) needs a GitHub PR-state read, which is bounded: only for orphans that have a `pr_url`, batched (GraphQL or `gh` multi-fetch). If that read is deferred for build simplicity, the safe fallback is to collapse **only `shipped`** and badge closed-unmerged as `stalled` (visible) — strictly conservative (nothing incomplete is hidden), at the cost of a few genuinely-dropped PRs staying visible. Builder's call; either way the invariant holds.
- **`label` (readable orphan name — capture `cap-orphan-lane-show-desktop-chat-title`)** — resolution order: **desktop chat `title`** (from the latest `desktop_session_*` event's `payload.title`) **> `repo/branch`** (from the event payload) **> the humanized `task_id`** (today's fallback). For non-desktop orphans, `repo/branch` or `task_id` as today.
  - **Phase 0 contract dependency:** `title` is already in the frozen `desktop_session` payload schema (design pass §3.2). **Build-time verification required:** confirm the Mac-side Claude Code hook actually populates `payload.title` on `desktop_session_active`/`_start`. If it emits null (the title appears only *after* a chat names itself), a **small emitter follow-up** re-emits `desktop_session_active` carrying the title once known — additive, within the frozen schema (no new event type, no new field). The endpoint degrades gracefully to `repo/branch` when `title` is absent, so the dashboard work does **not** block on the emitter fix.

### 3.5 Extensibility for the scene-graph (design the slot, don't build)

The scene-graph's preliminary asks that exceed the dashboard's need are accommodated by shape, not implementation:

- **`blocked_on`** — reserve the key on the active-task object; emit `null` until a real blocked-on model exists. (Their final spec at T8 defines the semantics.)
- **File/area-level collision** — `area` is present on `parked[]` and reservable on tasks; today it's `null`. We derive repo-level collision now (via `?repo=`); file/area is their T8 ask.
- **Flat `active[]`/`parked[]` projection** — the scene-graph's illustrative shape (§4 of their request) is a thin reshape of `missions[].tasks` (filtered to non-terminal) + `parked[]`. If they want it server-side, add a `?view=scene-graph` projection at their T8 — non-breaking, additive.

**No scene-graph-only field is populated in Phase 2.** The endpoint is ours; their consumption is read-only and finalized by written request through Larry.

---

## 4. The parity gate (what makes the cutover safe)

The cutover (§5) does **not** merge until a **parity test** proves the Python derive matches the TypeScript derive on a shared fixture set. This is the mechanism that lets us touch the proven board without risk.

- **Shared fixtures:** a committed set of `chain_events` + `missions.json` fixtures (drawn from real anonymized board states — at minimum: a shipped mission, an in-flight mission, an awaiting-merge task, an escalation, a deferred mission, and an orphan set exercising every infra-filter branch).
- **Assertion:** for every fixture, the Python endpoint's `missions[]` + `orphans[]` (the fields that existed pre-Phase-2) are **deep-equal** to the TypeScript derive's output. The additive Phase-2 fields (`parked`, orphan `state_badge`/`terminal`/`label`) are asserted separately against their own expected values.
- **Where it runs:** the fixtures live in agent-core (the Python side, authoritative). A mirrored fixture-equality test runs dashboard-side comparing the TS derive against the committed expected-output JSON, so both repos pin the same contract.
- **Enforcement:** the dashboard-cutover PR **must** include the passing parity test; Mirror's review checklist treats a cutover PR without a green parity assertion as REVIEW_REVISION. The TS derive in `lib/mission-queries.ts` is deleted **in the same PR** as the proven cutover — never a window where both run live against the board.

---

## 5. Dashboard cutover + readability (the dashboard-side work)

**Interface-first:** all dashboard work below builds against the **frozen §3 contract** — the agent-core endpoint ships and is proven before any dashboard step starts.

1. **`/api/missions/list/route.ts` → thin proxy.** Replace the Supabase-join + derive with a single fetch to `GET /api/missions/derived` (passing through any `?repo=`/`?task_id=`), returning the droplet's response. Keep the existing 5s timeout + 502 degradation. The route's response shape is unchanged for existing consumers.
2. **Delete the derive from `lib/mission-queries.ts`.** Remove `derivePhaseForTask`, `aggregateMissionPhase`, `detectOrphans` + filters, `summarizeTaskEvents`, `fetchEventsForTaskIds`, `fetchRecentChainEvents`. Keep only what components still need that *isn't* derive (e.g. `hashToHsl`, `extractHeadline` re-export, `queryTaskEvents` for the drill-down panel — that's a bounded single-task read, not the board derive; it may stay or also relocate, builder's call, but it is **not** in the parity scope). Same-PR as step 1, parity-test-gated (§4).
3. **Orphan-lane readability (`app/missions/components/OrphansLane`).** Render `state_badge` as a pill; default-hide `terminal: true` orphans (shipped/closed only) behind a "show N done" disclosure; **keep `stalled` orphans visible** and sort them toward the top (attention, not burial); render `label` instead of the humanized `task_id`. Collapse the lane itself (collapsed by default with a count, since it's a remediation surface, not a primary lane) — but a non-zero `stalled` count should be visible on the collapsed header so overlooked work advertises itself. *(The Parked-lane-above-Orphans ordering already shipped.)*
4. **Contextual resurfacing.** When the operator opens a repo/mission view, call the endpoint with `?repo=<that repo>` and surface the returned `parked[]` as a "related parked work" affordance near that mission — the by-context resurfacing that replaces the timer. Aging captures (`aging: true`) get a gentle visual nudge, never a nag (design pass §4).

---

## 6. The parked-&-aging digest (the dashboard digest card)

Revives `operator-ux-catch-me-up-shortcut`. **Dashboard surface, not Telegram** (decision #3).

- **Generator (agent-core):** a small producer modeled on `scripts/ceo_digest_generator.py` that reads `captures.json`, selects `state == "parked"` items with **`aging: true`** (the flag the GC healer already persists — the digest does **not** recompute aging), and writes a digest artifact (parked count, aging count, the aging items with title + origin repo + age). Daily cadence via the existing healer/timer pattern + an on-demand path.
  - **Reuse, don't reinvent the aging clock:** aging = the GC healer's `AGING_BUSINESS_DAYS` (5 business days) flag. One definition of "aging" across the system.
  - **Enforcement:** the generator reads `cap["aging"]`; it MUST NOT contain its own business-day computation. A unit test asserts the generator selects exactly the `aging:true` parked set (no independent date math).
- **Surface (dashboard):** a digest card styled like the Approvals-tab daily/weekly summary, answering "what's parked & aging — promote / drop / snooze?" The promote/drop/snooze **actions** are Phase 3 write-back; Phase 2 renders the digest **read-only** (catch-me-up), with the actions stubbed/deferred.
- **On-demand:** a refresh affordance regenerates the digest (or the dashboard reads the latest artifact) without waiting for the daily cycle.

---

## 7. Build sequence (single-repo split, interface-first)

One phase = one build sequence (`missions-v2-phase2`), authored after this spec lands on `main` (Beacon discipline: spec-doc-first). Proposed DAG — agent-core (contract) steps before dashboard (UI) steps:

| Step | Repo | What | `depends_on` |
|---|---|---|---|
| `p2-derive-endpoint` | agent-core | `GET /api/missions/derived` — port the derive (§3.4), add filters (§3.2), `parked[]` + orphan-readability fields (§3.3–3.4), `parity` fixtures (§4) | — |
| `p2-digest-generator` | agent-core | parked-&-aging digest producer (§6), reads `aging` flag | — |
| `p2-dashboard-cutover` | dashboard | list route → proxy + delete TS derive, parity test green (§4–5.1–5.2) | `p2-derive-endpoint` |
| `p2-orphan-readability` | dashboard | OrphansLane badges/collapse/labels (§5.3) | `p2-derive-endpoint` |
| `p2-resurface-and-digest-card` | dashboard | contextual resurfacing (§5.4) + digest card (§6) | `p2-derive-endpoint`, `p2-digest-generator` |

- The two agent-core steps are independent (parallel). The three dashboard steps depend on the frozen endpoint; `p2-resurface-and-digest-card` also needs the digest artifact.
- **Wire-and-prove (handoff §6.5):** the new endpoint needs a `dashboard-api` restart; the digest generator needs a systemd timer install+enable (install-drift/stale-daemon healers may auto-do it — verify). A merge ≠ live; prove the endpoint end-to-end (curl `?repo=` on the droplet), then prove the dashboard reads it, then prove the digest card renders.
- **Kickoff is Beacon-mediated** (handoff §6.4): place the sequence `pending`, Mirror DAG-preflights (`--phase routing-signal`), advancer runs hands-free; Larry gates at the phase boundary.

---

## 8. Success criteria

1. `GET /api/missions/derived` on the droplet returns the §3.3 shape; `?repo=` and `?task_id=` filter correctly; unfiltered output's `missions[]`+`orphans[]` deep-equal the old `/api/missions/list` derive (parity test green).
2. The dashboard Missions tab renders identically to pre-Phase-2 while reading from the droplet endpoint; the TS derive is deleted in the same proven PR.
3. Opening a repo/mission surfaces its related parked captures (contextual resurfacing).
4. A daily "parked & aging" digest card renders on the dashboard (read-only), reading the GC healer's `aging` flag — no Telegram path.
5. The Orphans lane shows per-orphan `state_badge`, hides **only** terminal (shipped/closed) orphans by default, keeps `stalled` orphans visible and surfaced, labels desktop orphans by chat title, and the lane collapses. Invariant: no unmerged orphan is ever auto-hidden.
6. The scene-graph consumer can `GET /api/missions/derived?repo=X` and read derived `phase`/orphan/aggregate without re-deriving (validated by a single curl; their full consumption is their T8).

---

## 9. Builds on locked decisions

| # | Decision (design pass §7) | How Phase 2 honors it |
|---|---|---|
| 3 | Resurfacing = daily digest + on-demand, **dashboard** (Approvals-style), aging = >5 business days | §6 digest card; reuses GC `aging` flag; no Telegram |
| 4 | I prep / Larry dispatches | Digest is read-only catch-me-up; promote/drop/snooze actions deferred to Phase 3 |
| 5 | Programs↔Missions, drag-drop, full PM tooling out of arc | Out of scope (§2) |

Plus the **anchor decision (2026-06-10):** relocate the derive to a droplet-side Python source of truth that both the dashboard and the scene-graph read; **dashboard cuts over in Phase 2, parity-test-gated.**

---

## 10. Cross-references

- Design pass: [docs/missions-redesign-design-pass-2026-06-09.md](../../../docs/missions-redesign-design-pass-2026-06-09.md) (§4 UX, §7 decisions, §8 orchestration)
- Phase 0/1 specs: `missions-v2-phase0-desktop-session-feed.md`, `missions-v2-phase1-durable-capture.md`
- Scene-graph interface: [docs/missions-scene-graph-interface-reply.md](../../../docs/missions-scene-graph-interface-reply.md); their request `ourliberty-graph/docs/scene-graph-missions-request.md`
- Derive being relocated: `ourliberty-dashboard/lib/mission-queries.ts`, consumed by `ourliberty-dashboard/app/api/missions/list/route.ts`
- Reuse anchors: `scripts/dashboard_api.py` (Supabase client + missions/captures endpoints), `scripts/heal_missions_card_gc.py` (`AGING_BUSINESS_DAYS`, `aging` flag), `scripts/ceo_digest_generator.py` (digest pattern)
- Handoff: [docs/missions-v2-phase2-3-handoff.md](../../../docs/missions-v2-phase2-3-handoff.md)
