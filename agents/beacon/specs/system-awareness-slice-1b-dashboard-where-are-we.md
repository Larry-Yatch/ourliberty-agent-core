# Build spec — System self-awareness, Slice 1b: dashboard "Where are we" view

**Mission:** System self-awareness (the "standing brain") — see `docs/system-awareness-north-star.md`. Slice 1b of N.
**Status:** Draft v1 for build — 2026-06-21.
**Repo:** **ourliberty-dashboard** (dashboard-repo build; this spec lives in agent-core per convention — set the mission's repo to `ourliberty-dashboard`).
**Author:** Claude Code (desktop). **Approver:** Larry.

> Grounded against the live dashboard repo (Explore 2026-06-21). Reuse-first: mirror an existing system page; reuse the proxy fetch hook + shared components. **READ-ONLY view — no actions, no writes.**

## 0. Goal

A new read-only dashboard page **"Where are we"** that renders the live State Log (`GET /api/system/state-log`, already shipped + live in Slice 1) so Larry can **see** the whole-system status at a glance — not only by asking Beacon. This is the human surface for Slice 1.

## 1. Why

Slice 1 made the system narrate its own work-in-flight, but today Larry can only consume it via Beacon or the raw endpoint. His preferred surface is the dashboard (glance → skim → drill). This delivers that.

## 2. Reuse (mirror these — do not invent patterns)

- **Mirror** `app/operations/system/page.tsx` — the template for a read-only multi-section system page (fetch + loading + error/stale + `PanelErrorBoundary` + grid layout).
- **Fetch via** the canonical hook (NEVER call the droplet directly — always the same-origin proxy):
  `useDashboardData<StateLogResponse>("/api/system/state-log", { refreshInterval: 30_000 })` from `lib/api.ts`. Use `useDataUpdatedAt(data)` for the freshness timestamp.
- **Reuse components:** `PanelErrorBoundary`, `StaleDataBanner`, `LastRefresh`, `LoadingState`, `RelativeTime`.
- **Styling:** Tailwind v4 (zinc / emerald / amber / rose), matching the existing pages + root layout.
- **Auth:** automatic via `proxy.ts` middleware — no per-page gate code.

## 3. Deliverables

### D1 — Types (`lib/types.ts`)
Add `StateLogResponse` (+ nested `StateLogSnapshot`, `StateLogProvenance`) matching the live endpoint:
`{ present: boolean, stale: boolean, last_synced_at: string, schema_version, as_of: string, narrative_prose: string, structured_snapshot: { as_of, missions_active: [], pipeline: { building, in_review, merged_recent, stuck: [] }, in_flight_now: number, sequences_active: [], waiting_on_larry, health }, provenance: { by, model, at, fallback: boolean } }`.
**Verify exact field shapes against a live `GET /api/proxy/api/system/state-log` while building** — the snapshot may carry minor additions; type defensively (optional where unsure). `health` is currently null/reserved.

### D2 — Page (`app/where-we-are/page.tsx`) — `"use client"`
Lay it out in the DES "depths":
- **Glance (top):** render `narrative_prose` as the headline ("here's where we are"), with a freshness line (`as_of` via `RelativeTime` + `LastRefresh`). Show `StaleDataBanner` when the data is stale (the `stale` flag) or the fetch errors. If `provenance.fallback === true`, a subtle "(basic summary — AI write unavailable)" note.
- **Skim (panels — each wrapped in `PanelErrorBoundary`):**
  - **Work in flight** — `structured_snapshot.pipeline` (building / in review / recently merged) + `in_flight_now` + `sequences_active`; **call out `pipeline.stuck[]` prominently** when non-empty (amber/rose).
  - **Active missions** — `missions_active[]` (name, phase, rollup).
  - **Waiting on you** — `waiting_on_larry`, highlighted (this is the "what needs Larry" teaser; Slice 2 expands it).
  - **Health** — if `health` present, render it; if null/reserved, show "—" with a "coming soon" hint (Slice C, out of scope).
- **Loading / empty:** use `LoadingState` while fetching; if `present === false`, a friendly empty state ("No status yet — the narrator writes one every ~10 min.").

### D3 — Nav (`components/Nav.tsx`)
Add `{ href: "/where-we-are", label: "Where are we" }` to the `LINKS` array (place in the diagnostic cluster — after "Live System" or before "Healers").

### D4 — Tests (`app/where-we-are/__tests__/page.test.tsx`)
Vitest + RTL, mirroring `app/missions/__tests__/page.test.tsx`: wrap in `<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>`, mock `globalThis.fetch`. Assert:
- renders the header + `narrative_prose` on success;
- fetches `/api/proxy/api/system/state-log` and **NOT** `api.ourliberty.dev` (same-origin proxy);
- shows the stale banner on a fetch error;
- renders the snapshot panels (work-in-flight / missions / waiting-on-you).

## 4. Acceptance criteria

- [ ] `/where-we-are` renders the live narrative + the structured-snapshot panels, read-only.
- [ ] Loading, error/stale, and empty (`present:false`) states are handled gracefully (no crash; `PanelErrorBoundary` contains panel render errors).
- [ ] Fetches ONLY via the same-origin proxy (`/api/proxy/...`), never the droplet directly.
- [ ] Nav entry present + routes correctly; auth-gated (inherited).
- [ ] New vitest suite green (`npm test`); matches the look of `operations/system`.

## 5. Out of scope

- **Depth-3 "full story" drill-through** (click a mission/PR → detail) — later enrichment.
- **Health synthesis** (the `health` field) — that's Slice C (held).
- Any write/action controls — read-only only.

## 6. Notes

- This is a NEW system-state view, **distinct from the Missions/Projects board** (boundary respected) — do not touch the board's funnel/pipeline.
- The State Log refreshes ~every 10 min on the droplet; a 30s client poll keeps the view current.
