# Spec: Dashboard UI — E3.2 (Next.js read-only frontend)

**Status:** Draft (awaiting Larry approval)
**Author:** Claude-as-Beacon (drafted 2026-05-20)
**Approver:** Larry (pending)
**Phase:** E3.2 of the Phase E plan (`docs/phase-e-plan.md` in `ourliberty-agent-core`)
**Predecessor:** E3.1 — droplet API shipped 2026-05-20 (PR #62 in `ourliberty-agent-core`)
**Target repo:** `ourliberty-dashboard` (NOT `ourliberty-agent-core` — first Forge dispatch against this repo)
**Successor:** E3.3 (Nginx + Let's Encrypt for `api.ourliberty.dev`)

## 1. Problem statement

E3.1 shipped the droplet-side JSON API. Right now nothing consumes it. E3.2 replaces the default `create-next-app` scaffold at `ourliberty-dashboard` with a real read-only dashboard rendering live agent OS state via 4 routes. Once E3.3 lands the public HTTPS endpoint, Larry can open `dashboard.ourliberty.dev` on his laptop or phone and watch the agent OS work in near-real-time — no SSH required.

## 2. Success criteria

- 4 routes deployed at the Vercel project (`prj_b1jhpIqS8VDyZfDQvIoyzm32Rf6b`): `/`, `/tasks`, `/costs`, `/healers`.
- All pages auto-refresh every 30 s via SWR (locked Q3=3 architecture decision from the phase plan).
- Loading + error states are visible and graceful — error state keeps last-known-good data visible with a banner, not a blank page.
- **API token never appears in the client bundle.** All API calls flow through server-side Next.js route handlers (`app/api/proxy/[...]/route.ts`). The browser hits same-origin proxy routes; the proxy adds `X-Dashboard-Token` server-side.
- Vercel preview URL DMs Larry via `deploy_notifier` (E2.2) within ~5 min of `git push`.
- `npm run build` clean. `npm run lint` clean. TypeScript strict mode passes.
- Verifiable via `grep -r DASHBOARD_API_TOKEN .next` returning ZERO matches after a production build.

## 3. Users / consumers

- **Primary:** Larry. Opens the dashboard on laptop or phone to monitor agent OS state.
- **Secondary (future):** Used as the foundation for E4 (interactive controls). Architecture must support adding mutation endpoints later without rework.

## 4. Scope (what's in)

### 4.1 Architecture

```
┌──────────────┐         ┌──────────────────┐         ┌─────────────────────┐
│ Browser      │ ──HTTPS─│ Vercel (Next.js) │ ──HTTPS─│ Droplet API         │
│ / /tasks     │←──SWR───│ /api/proxy/*     │←─server─│ api.ourliberty.dev  │
│ /costs       │ (30 s)  │   adds token     │  ←HTTP  │ (E3.1 FastAPI :8000)│
│ /healers     │         │   server-side    │         │                     │
└──────────────┘         └──────────────────┘         └─────────────────────┘
```

**The proxy pattern is load-bearing — DO NOT call the droplet API directly from the browser:**
- E3.1's CORS is locked to `https://dashboard.ourliberty.dev` exactly. Preview deploys at `*-pr-N.vercel.app` would fail CORS if the browser called the API directly. The proxy sidesteps CORS because the browser only hits same-origin Vercel routes.
- Keeps `DASHBOARD_API_TOKEN` server-side. The token is already set as a Vercel project env var (Production + Preview, Sensitive). It must NEVER be exposed as `NEXT_PUBLIC_*`.

### 4.2 Proxy route: `app/api/proxy/[...path]/route.ts`

Catch-all route handler. App Router conventions per Next.js 16 — **read `node_modules/next/dist/docs/` first** (this repo's `AGENTS.md` flags v16 as different from training data).

Behavior:
- Accept GET only. Other methods → 405.
- Forward path segments to `${DASHBOARD_API_URL}/${segments.join('/')}`.
- Forward query string verbatim.
- Add header `X-Dashboard-Token: ${DASHBOARD_API_TOKEN}` (env var, server-side only).
- 5 s timeout. On timeout → 504.
- On `fetch` throw (network error / DNS fail / connection refused) → 502.
- On upstream non-2xx → pass status + body through.
- On upstream 2xx → return JSON with `Cache-Control: no-store` (we want every 30 s SWR poll to be a fresh fetch — Vercel's edge cache must not interfere).

Both env vars are required; if either is missing at request time, return 500 with body `{"error": "server misconfigured"}` and log to `console.error` (Vercel collects).

### 4.3 Client-side fetcher: `lib/api.ts`

Single hook `useDashboardData<T>(path: string)` wrapping SWR:
- Fetches from `/api/proxy/${path}` (relative — same origin, no CORS, no token in the browser).
- `refreshInterval: 30000`.
- `revalidateOnFocus: true`, `keepPreviousData: true` — so when the API blips, the UI keeps showing the last-known-good values with the error banner.
- Returns `{ data, error, isLoading }` from SWR.
- Generic over `T` — caller specifies the response shape from `lib/types.ts`.

### 4.4 Types: `lib/types.ts`

TS interfaces mirroring **exactly** the response shapes from E3.1 spec § 4.1 (`agents/beacon/specs/dashboard-api-e3-1.md` in `ourliberty-agent-core`). The full spec is embedded in this dispatch's prompt for reference. One type per endpoint:

- `HealthResponse`
- `AgentsStatusResponse` (including the `bot_model: "systemd-bot" | "inbox-watcher"` discriminator added in E3.1 for mirror/pulse)
- `TasksRecentResponse`
- `CostsTodayResponse`
- `CostsWeekResponse`
- `CycleJournalRecentResponse`
- `HealersStatusResponse`

All ISO timestamp fields typed as `string` (consumer formats with `RelativeTime` component, § 4.7).

### 4.5 Pages

**`/` (Overview)** — `app/page.tsx`. Replaces the scaffold. Sections (top to bottom):
- Header: "Ourliberty Agent OS" + `<LastRefresh>` showing when data last updated.
- 4 `<AgentStatusCard>`s in a responsive grid (beacon, forge, mirror, pulse).
- 1 `<CostBlock>` for today (total + by-agent breakdown).
- "In-flight" mini-section: total in_flight_count across all agents + list of task_ids if any.
- Last 5 cycle-journal entries (compact list: timestamp + headline).

**`/tasks`** — `app/tasks/page.tsx`. Table of the 20 most-recent tasks: agent | task_id | outcome (badge-colored) | started_at (relative) | duration | cost | PR link (if present). Defaults to most-recent-first; no sort/filter (E4).

**`/costs`** — `app/costs/page.tsx`. Two blocks: Today (same as overview's CostBlock) + Week (with `by_day` rendered as a simple 7-cell row showing date + total + task_count; no charting library — keep deps minimal).

**`/healers`** — `app/healers/page.tsx`. Responsive grid of `<HealerCard>`s. One card per healer returned by `/healers/status`. Each card: name + last_result badge + last_run_at relative + next_scheduled_at relative + kill_switch_active warning if true.

### 4.6 Layout: `app/layout.tsx`

Update the existing scaffold layout:
- Keep Geist + Geist Mono font configuration (already wired).
- Update `metadata.title` to "Ourliberty Agent OS".
- Add `<Nav>` component at top: 4 links with active-route highlighting via `usePathname()` (client component).
- Body wrapper provides a `<main className="container mx-auto px-4 py-6 max-w-6xl">`.
- Slot for `<ErrorBanner>` at bottom — only renders when any consumer has an SWR error (use a small zustand-or-context pattern, OR simpler: each page owns its own banner. Recommend per-page for v1; consolidate in E4 if it feels redundant.)

### 4.7 Components

All under `components/`:
- `<Nav>` — 4 links (`/`, `/tasks`, `/costs`, `/healers`); active route highlighted via Tailwind background.
- `<AgentStatusCard>` — props: `{ name, bot_active, bot_model, in_flight_count, last_activity_at }`. Renders bot-active dot (green/red/gray-null), agent name, in-flight count, last activity time.
- `<TaskRow>` — one row of the tasks table.
- `<CostBlock>` — total + 4-line by-agent breakdown.
- `<HealerCard>` — name, result badge (ok/warn/error/stale), times, optional kill-switch warning.
- `<LoadingState>` — centered spinner using Tailwind `animate-spin`.
- `<ErrorBanner>` — fixed-bottom banner with "API unreachable — retrying every 30 s" + small refresh icon spinning. Only shown when SWR error is non-null.
- `<RelativeTime>` — formats ISO timestamps as "3 m ago" / "just now" / "5 h ago". Use `Intl.RelativeTimeFormat` (stdlib, no extra dep). Client component (re-renders every minute on `setInterval`).
- `<LastRefresh>` — small "Updated 3 s ago" indicator in the layout header. Driven by SWR's `dataUpdatedAt` from any page's hook (or a heartbeat hook calling `/health`).

### 4.8 Env vars

Two server-side variables (never `NEXT_PUBLIC_*`):
- `DASHBOARD_API_URL` — base URL of the droplet API. Default in `next.config.ts` or `lib/env.ts`: `"https://api.ourliberty.dev"`. Overridable per-environment via Vercel project settings, and via `.env.local` for local dev (see § 4.9). Already missing — **add to Vercel project env vars in same Production + Preview scopes as DASHBOARD_API_TOKEN**, value `https://api.ourliberty.dev`. Larry's action post-merge.
- `DASHBOARD_API_TOKEN` — shared secret. Already set in Vercel project env vars (Production + Preview, Sensitive). No change needed.

Add a small `lib/env.ts` that reads both vars and throws clearly at server-startup if either is missing — keeps the proxy route handler clean.

### 4.9 Local dev workflow

The droplet API is `localhost:8000` only until E3.3 ships Nginx + TLS. For local dev during E3.2 build (or for Larry post-merge tinkering):

```bash
# Open an SSH tunnel that maps localhost:8000 on the laptop to localhost:8000 on the droplet
ssh -L 8000:127.0.0.1:8000 -N larry@134.209.44.80 &

# In ourliberty-dashboard repo:
cat > .env.local << EOF
DASHBOARD_API_URL=http://localhost:8000
DASHBOARD_API_TOKEN=<grep from droplet .env.larry>
EOF

npm run dev
# → http://localhost:3000
```

Document this in `README.md`. The `.env.local` is gitignored already (the scaffold gitignores `.env*`).

### 4.10 Testing

Add Vitest as a devDep (modern default, integrates cleanly with Next 16). 5–10 tests targeted at what's mechanically verifiable:
- Proxy route: forwards path + query + header correctly (mock `fetch`), 405 on POST, 502 on fetch throw, 504 on timeout, 500 when env vars missing, status + body pass-through.
- `lib/api.ts`: `useDashboardData` returns expected shape on success / error; respects `refreshInterval`.
- `<RelativeTime>`: formats edge cases correctly (just now / N minutes / N hours / N days).

UI rendering tests are deferred to E4+. The components are simple enough that visual review of preview deploys is sufficient for v1.

### 4.11 Documentation

- `README.md` — replace the scaffold's boilerplate with a real one: what this is, how to run locally (including the SSH tunnel + .env.local pattern), how to deploy (auto via Vercel + push), how the proxy route works.
- `.env.local.example` — committed file showing the var shape; the real `.env.local` stays gitignored.
- Inline JSDoc on the proxy route + `useDashboardData` explaining the rationale (why server-side, why no NEXT_PUBLIC_).

## 5. Out of scope (what's deliberately not in)

- Mutation endpoints (approve / pause / dispatch) — those are E4.
- Server-Sent Events / WebSockets — E6 trigger only.
- Per-route polling cadence (all 30 s for v1) — E4 if needed.
- Skeleton loading screens / animations beyond a basic spinner — E4.
- Mobile-optimized layouts beyond "usable on phone."
- Authentication / user accounts (single shared secret only).
- i18n.
- E2E tests (Playwright) — E4+ trigger.
- Storybook — never necessary for a 4-route internal tool.
- Custom charting / time-series — keep deps minimal; week-cost is a 7-cell row, not a graph.
- Analytics / observability beyond Vercel's built-in.
- Dark mode toggle UI — keep existing system-preference behavior in globals.css.

## 6. Acceptance criteria

- [ ] PR opens against `ourliberty-dashboard` (NOT `ourliberty-agent-core`). Branch under `forge/`. Title: `feat(dashboard-ui): E3.2 — Next.js dashboard for read-only droplet status`.
- [ ] `npm install` clean (only `swr` added to `dependencies`; only `vitest` + `@vitejs/plugin-react` added to `devDependencies`; lock file regenerated).
- [ ] `npm run lint` clean.
- [ ] `npm run build` clean. Type check passes under TS strict mode.
- [ ] `grep -r DASHBOARD_API_TOKEN .next` returns **zero** matches after `npm run build`. Verified in PR body.
- [ ] All 4 routes (`/`, `/tasks`, `/costs`, `/healers`) render without errors on Vercel preview deploy.
- [ ] Vercel preview deploy succeeds; `deploy_notifier` DMs Larry the preview URL within ~5 min of push.
- [ ] On the preview deploy, the dashboard shows "API unreachable — retrying every 30 s" banner (because E3.3 isn't live yet) but the page chrome (nav, layout, headings, structure) renders cleanly. This is the EXPECTED state in this PR.
- [ ] Vitest suite passes (target 5–10 tests; mostly proxy route + fetcher).
- [ ] No CORS configuration anywhere — proxy pattern sidesteps it entirely.
- [ ] `README.md` documents local dev setup (SSH tunnel + .env.local).
- [ ] `.env.local.example` committed (no real secrets — just var names).
- [ ] PR body has Summary + Test plan + a "Verification post-E3.3" section explaining what should work once Nginx is live.

## 7. Open questions / risks

- **Next.js 16 + Tailwind 4 are not in training data** per this repo's `AGENTS.md`. Forge MUST read `node_modules/next/dist/docs/` for current API surfaces — particularly route handler conventions (`route.ts`), `[...path]` catch-all syntax, `cookies()`/`headers()`/`searchParams` access patterns, and Tailwind 4's CSS-first config. If Forge attempts a Next 15-era pattern, the build will fail mysteriously. **This is the #1 risk in this dispatch.**
- **First Forge dispatch against `ourliberty-dashboard` repo.** All prior Forge work has been against `ourliberty-agent-core`. Possible chain gaps surface here: worktree creation against this remote, gh PR auth scope, outbox_notifier source-routing for non-agent-core target_repo, auto-merge healer scope. Mirror's CLAUDE.md `REPO-GUARDRAILS.md` may need to list `ourliberty-dashboard` as T0 sandbox — confirm at preflight (CLARIFY_REQUEST if it's not listed and you're unsure whether to proceed).
- **Mirror dial-3 regression gate** runs `scripts/test_regression_check.py --parent-sha <BASE> --head-sha <HEAD>` — but that helper lives in `ourliberty-agent-core`, and the dispatched test suite is the dashboard's Vitest. Confirm at preflight: does the regression helper handle target_repo=ourliberty-dashboard correctly, or does Mirror need different gate machinery here? If unclear, CLARIFY_REQUEST.
- **CORS sidestep via proxy** is the architectural commitment. Forge must not introduce direct browser-side fetches to `api.ourliberty.dev`. If a "simpler" approach feels tempting (e.g. widen CORS on the API side), CLARIFY_REQUEST instead — the proxy pattern is locked by E3.1's intentionally-narrow CORS.
- **`DASHBOARD_API_URL` is not yet set in Vercel.** This PR depends on it being set in Production + Preview env vars before the preview deploy can actually return non-error data. Larry's post-merge action (mirrors the DASHBOARD_API_TOKEN install pattern). Acceptable: PR ships with the "API unreachable" banner shown until Larry sets the var + E3.3 ships.
- **Build phase API stubbing.** Forge can't reach the real droplet API during build (the worktree's tests run in isolation). Vitest tests mock `fetch`; the proxy route doesn't actually call any upstream during tests. Component-level testing is deferred. Acceptable.
- **Bundle-size grep verification.** The acceptance criterion `grep -r DASHBOARD_API_TOKEN .next` must return zero matches. Forge runs this as part of the build verification + records the result in the PR body. Standard hygiene; cheap to verify; catches NEXT_PUBLIC_ regressions.

## 8. Handoff package requirements

- `app/layout.tsx` — updated layout with nav + container.
- `app/page.tsx` — overview page (replaces scaffold).
- `app/tasks/page.tsx`, `app/costs/page.tsx`, `app/healers/page.tsx` — new routes.
- `app/api/proxy/[...path]/route.ts` — server proxy.
- `lib/api.ts` — SWR fetcher hook.
- `lib/types.ts` — TS types for all 7 E3.1 response shapes.
- `lib/env.ts` — env var loader with clear errors.
- `components/Nav.tsx`, `AgentStatusCard.tsx`, `TaskRow.tsx`, `CostBlock.tsx`, `HealerCard.tsx`, `LoadingState.tsx`, `ErrorBanner.tsx`, `RelativeTime.tsx`, `LastRefresh.tsx`.
- `lib/__tests__/proxy.test.ts`, `lib/__tests__/api.test.ts`, `components/__tests__/relativeTime.test.tsx` (or similar — ~5-10 tests across).
- `package.json` — `swr` added to `dependencies`; `vitest` + `@vitejs/plugin-react` + `@vitest/ui` + `@testing-library/react` (if used for component tests) added to `devDependencies`. Add `"test": "vitest"` to `scripts`.
- `vitest.config.ts` — minimal config; jsdom environment for component tests.
- `README.md` — replace boilerplate with real docs.
- `.env.local.example` — env var shape template.
- `.gitignore` — confirm `.env.local` covered (scaffold already does this).

## 9. References

- E3.1 API spec (endpoint contracts): `agents/beacon/specs/dashboard-api-e3-1.md` in `ourliberty-agent-core` — the source of truth for response shapes. EMBEDDED in this dispatch's envelope prompt for offline reference.
- Phase plan: `docs/phase-e-plan.md` § Phase E3 in `ourliberty-agent-core`.
- Dashboard repo: `https://github.com/Larry-Yatch/ourliberty-dashboard`.
- Live API URL (post-E3.3): `https://api.ourliberty.dev/*`.
- Local dev URL (via tunnel): `http://localhost:8000/*`.
- Next.js 16 docs: `node_modules/next/dist/docs/` (READ FIRST per this repo's AGENTS.md).
- Tailwind 4 config: `app/globals.css` (CSS-first — `@import "tailwindcss"` + `@theme inline`; no `tailwind.config.js`).
