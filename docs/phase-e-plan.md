# Phase E — Spec → Deployed Prototype

**Authored:** 2026-05-18
**Status:** Planning complete, awaiting Phase E1 kickoff
**Predecessor:** Phase D3.5 (complete — see `operating-manual.md` Part II)
**Through-line:** Take the agent OS from "produces PRs" to "produces preview-deployable prototypes, drafted in Google Docs, visible in a real dashboard."

---

## Top-Level Frame

The agent OS today is a **spec → PR pipeline**. You talk to Beacon, the chain runs (Forge builds → Mirror reviews → revision loop → auto-merge gap), code lands on `main`. That's it. Past the merge boundary is greenfield.

Phase E closes four gaps in dependency order:

1. **Harden the merge boundary** — finish the auto-merge handoff that D3.5 set up, plus two structural fixes (marker render helpers, watcher migration onto `agent_runner.run_claude`) that we've been deferring.
2. **Add a deploy layer** — make Forge able to land *previews* on Vercel for prototyped products. Preview-first because most client work will be prototyping; we defer full prod-deploy machinery until a real client asks for it.
3. **Build a dashboard** — end terminal-only visibility. Read-only first (Phase E3), interactive second (E4). The dashboard is itself the first product our own deploy chain ships, which dogfoods E2.
4. **Wire Google Suite via MCP** — let Beacon read/write Google Docs, Gmail, Calendar through MCP connectors. Specs get drafted in your native tool instead of in Telegram.

After E, the system supports a workflow like: *"Larry drafts a spec in Google Docs with Beacon → Forge builds → Mirror reviews → auto-merges → Vercel preview URL lands in dashboard + Telegram → Larry shows client → iterate."*

What we are NOT building in E: production-grade deploys, custom Supabase per project, full audit-logger compliance, Guardian dep versioning, Ledger agent. Those wait for E6 / Phase F.

---

## Decisions Already Made (2026-05-18)

| Decision | Value | Rationale |
|---|---|---|
| Deploy autonomy dial — internal repos | **5** (auto-merge + auto-deploy) | Trust the chain for our own infra; rollback is cheap |
| Deploy autonomy dial — client repos | **2** (auto-merge, manual deploy) | But mostly N/A near-term — client work is prototyping (previews are automatic) |
| Dashboard scope | **B → C** (read-only first, interactive next, no rework) | One week of using B reveals which C controls actually matter |
| Droplet count | **One shared droplet for now** | Droplet hosts agents, not products; Vercel/Supabase give per-project isolation natively |
| Google Suite path | **MCP connectors via Beacon**, not a dedicated Aide agent | Promote to Aide only if Beacon does the same Google work repeatedly on a schedule |
| Upstream pulls | Surgical: `heal_pr_auto_merge`, `audit_logger` (later), `council_watchdog` pattern (later). **Not** Joe's 7-agent fleet | Our 4-agent governance is deeper; upstream's breadth is wider — pull breadth-features, keep our depth |

---

## Open Decisions (deferred until triggered)

| Decision | Trigger to revisit |
|---|---|
| Client repo ownership — our GitHub vs client's GitHub | When first client signs |
| Supabase activation strategy | When first prototype needs persistent data |
| Production-deploy pipeline (vs preview-only) | When first prototype gets a real user base |
| Audit logger pull (compliance) | When TruPath-adjacent work enters the system |
| Guardian dep versioning | When we add `agent-browser` or any other pinned binary |
| Ledger agent (Phase F) | When weekly cost variance becomes a real signal worth automating on |

---

## Phase Sequence Overview

| Phase | Goal | Est. effort | Depends on | Status |
|---|---|---|---|---|
| **E1** | Hardening (markers, watcher, auto-merge) | ~3 days (actual: ~1 day) | — | **Done 2026-05-19** (PRs #40, #41, #42, #43) |
| **E1.5** | Credential rotation discipline | ~1 day | E1 | **Done 2026-05-19** (PRs #45, #46, #47) |
| **E2** | Deploy layer (Vercel preview-first) | ~3–4 days | E1, E1.5 | **Done 2026-05-20** (10 PRs same day: #51-#60) |
| **E3** | Dashboard B (read-only) | ~3 days | E2 (dogfood) | **Done 2026-05-21** (PR #62, PR #1 in ourliberty-dashboard, + 4 recovery PRs #65-#68); public dashboard at `dashboard.ourliberty.dev` |
| **E4** | Unified PM Dashboard (rewritten 2026-05-24 from "Dashboard C interactive") | ~3 weeks wall clock | E3 + dogfood trigger | **In progress 2026-05-24** — E4.0 + E4.1 + E4.2 + E4.4a shipped same day; E4.4b/c + E4.3 + E4.5 pending. ~14 PRs + ~$60 LLM so far. |
| **E5** | Google Suite via MCP for Beacon | ~½ day | — (can run parallel) | **Done 2026-05-19** (PRs #37, #38, #39) |
| **E6** | Bench items (Ledger, audit logger, Guardian, prod deploy, etc.) | — | Trigger-based | Deferred |

Critical path: **E1 → E1.5 → E2 → E3 → E4**. E5 can run in parallel with any of these.

---

## Phase E1 — Hardening

**Goal:** Solidify the existing spec → PR chain before adding load. Three structural fixes the audit flagged, each load-bearing.

### Prerequisites
- Phase D3.5 complete (verified: PR #15 closed the 5d phase, auto-merge command structure exists, EMERGENCY_HALT verified live)
- `docs/upstream-audit.md` §5 + §6 reviewed (per `feedback_audit_upstream_first` memory)

### Tasks

**E1.1 — Marker render helpers** (~1 day)

Today Forge and Mirror emit markers like `=== PROCEED ===` by free-handing the string into their responses. Notifier regex-parses them. A typo silently dead-letters. Fix:

- Add to `scripts/forge_preflight_handler.py`: `render_marker(marker_type, **kwargs) -> str` that returns the correctly-bracketed block.
- Same for `scripts/mirror_review_handler.py`, `scripts/beacon_approval_handler.py`.
- Update each agent's `CLAUDE.md` to instruct: "call `render_marker()` from your tools, do not hand-type the markers."
- Add round-trip tests: `render → parse → matches input`. One test per marker type per agent.

**Success criteria:** All existing marker emission paths go through `render_marker()`. Round-trip tests green. One live cycle through the chain (any task) confirms no regression.

**E1.2 — Migrate `inbox_watcher.py` onto `agent_runner.run_claude`** (~1.5 days)

Today `inbox_watcher.py` spawns `claude` directly. It bypasses defenses already built into `agent_runner.py`: parent-CLAUDE.md quarantine guard, `/tmp` landmine scrubber, identity-assertion preamble, in-flight registry. The April 2026 stray-CLAUDE.md incident is exactly what these defenses prevent.

- Refactor `inbox_watcher.run_inbox_task()` to call `agent_runner.run_claude()` with the lease + expected_agent parameters.
- Preserve current lease/heartbeat semantics (they already work).
- Test on droplet against one inbox per agent in series, verify nothing regresses.

**Success criteria:** All four agents dispatched via `agent_runner.run_claude`. `docs/upstream-audit.md` §5 Finding 1 marked resolved.

**E1.3 — Pull `heal_pr_auto_merge.py` from upstream** (~½ day)

Auto-merge command is the missing piece of the Mirror PASS chain. Upstream has it.

- Copy `heal_pr_auto_merge.py` from `/tmp/audit/gm-agent-core-upstream-mirror/scripts/`.
- Adapt to our 4-agent topology (rename references, align with our marker schema).
- Add a systemd timer (every 5 min, matches other healers' cadence).
- Configure to require: Mirror PASS marker + green CI + no human-blocked label.
- Add a kill-switch: env var `OURLIBERTY_AUTOMERGE_ENABLED=true` required, default off until verified.

**Success criteria:** One real PR from Forge gets Mirror PASS → healer detects → enables auto-merge → GitHub merges when CI green. Verified live on a low-risk PR.

### Deferred from E1 (intentionally)
- Healer monitoring (per-healer heartbeat) — push to E6
- Replay log for dispatch — push to E6, low priority
- `outbox_notifier.py` decomposition (2,470-line monolith) — push to E6, only do it if a real bug surfaces

---

## Phase E1.5 — Credential Rotation Discipline

**Goal:** Make credential rotation a system primitive instead of a Larry-remembering responsibility. Every credential ships with a registry entry + runbook + Beacon-owned calendar event; a drift healer fails closed on any credential found in a credential store without a matching registry entry.

**Why inserted between E1 and E2:** Surfaced during E2.0 Vercel token install on 2026-05-19. Auditing `.env.larry` revealed 8 active credentials across 4 storage locations (`.env.larry`, gh CLI keychain, Claude Max OAuth, workspace-mcp OAuth), 17 empty template placeholders accumulated since Phase A, and zero tracking of rotation cadences. The DigitalOcean template comment "rotate every 90 days" had been silent for 11 days. Larry's framing: *"We need to make that a part of the system."* Doing this now means E2 + E3 + every future credential ship with the discipline baked in.

**Plain-language framing for the learning curve:**

- **Credential rotation** = swapping out a long-lived secret (API key, OAuth token) on a schedule, so that even if a leak happens, the leaked value stops working after a known window.
- **Registry** = a list, in JSON, of every credential the system knows about. Each entry says where the credential is stored, when it was last rotated, when it's due next, and a link to the rotation runbook.
- **Drift healer** = a small Python script run by systemd every 6 hours; reads the registry + the actual credential stores; DMs Larry if anything is in one place but not the other.

### Prerequisites
- E1 complete (the drift healer is a sibling pattern to `heal_pr_auto_merge`; both are defense-in-depth periodic scripts)
- Phase E2.0 done (Vercel token installed; it's the first credential to populate the registry)

### Decisions locked 2026-05-19
- Default cadence for credentials without a vendor-mandated schedule: **365 days** (Q1 Dial 4 — matches Vercel; cognitive load matches; vendor-mandated overrides this on a per-entry basis)
- Drift healer posture: **fail-closed, DM every 6h until reconciled** (Q2 Option A)
- Cleanup posture for empty `.env.larry` slots: **remove them, re-add with full discipline when needed** (Q1 of cleanup pair, Option B)
- Registry scope: **all credential stores, not just `.env.larry`** (Q2 of cleanup pair, Option B)
- `scope_audit` DM body when due: **(B) Pulse runs usage analysis + proposes specific drops** ("scopes-used-in-last-90-days" — `scripts/scope_usage_tracker.py` instruments gh CLI / workspace-mcp / Vercel API call sites, logs to `~/agents/blackboard/scope-usage.jsonl`)

### Tasks

**E1.5.1 — Design artifacts** (~½ day, Claude-driven) — **THIS PR**

- `config/token-rotation-schedule.json` — registry with all 8 entries populated
- `docs/runbooks/rotate-vercel-token.md` — canonical runbook (template for the other 7)
- `shared/credentials-discipline.md` — Mirror-enforced 4-artifact rule
- This phase plan + docs/roadmap.md drift cleanup

**E1.5.2 — Implementation** (~½ day, Forge dispatch) — **NEXT PR**

- `scripts/validate_token_rotation_schedule.py` — JSON schema validator + tests
- `scripts/heal_credential_registry_drift.py` — every-6h drift detection + DM (default dry-run; activation pattern from `heal_pr_auto_merge`)
- `runbooks/cycle-prompt.md` Pulse extension — "any rotation within 60d? DM Larry" check
- `scripts/scope_usage_tracker.py` — instrument gh CLI + workspace-mcp + Vercel API call sites with scope-tagged log emits
- 7 templated runbooks: `rotate-telegram-bot-token.md`, `audit-github-gh-oauth.md`, `audit-claude-max-oauth.md`, `audit-google-oauth.md`, + stubs for the future Aide / Supabase / DO / Cloudflare credentials (to be filled when those land)
- Beacon batch-dispatch: 3 additional calendar events (GitHub gh-OAuth annual scope audit + Claude Max annual audit + Google OAuth annual scope audit). Telegram bot tokens get no scheduled event; Vercel is already done.
- `.env.larry` cleanup on droplet: remove the 17 empty template lines
- New systemd unit + timer for the drift healer

**Success criteria:**
- Validator passes on `config/token-rotation-schedule.json`
- Drift healer dry-run reports zero drift against current state
- Pulse cycle DM check fires correctly when a `next_rotation_due` is within 60 days (live-test with a synthetic past-due entry)
- Larry receives a single batch DM from Beacon confirming the 3 new calendar events

### Deferred from E1.5 (intentionally)
- Multi-operator support (`owner_role` already in schema, but no real operator other than Larry today)
- Automated rotation (Vercel/GitHub PATs aren't programmatically rotatable; would require vendor SDKs)
- Pre-commit credential scanner (separate hygiene tool; revisit in E6 if needed)
- Aide bot Telegram token registry entry — wait until the Aide agent is actually built

---

## Phase E2 — Deploy Layer (Vercel Preview-First)

**Goal:** When Forge ships a PR on a configured product repo, a Vercel preview URL is automatically posted to Telegram. That's the whole MVP.

**Plain-language framing for the learning curve:**

- **Vercel** = a hosting service that takes your code, builds it, and gives you a public URL where the website runs. Free tier is generous; we'll start there.
- **Vercel "project"** = one website. Each connected GitHub repo = one Vercel project.
- **Preview URL** = Vercel auto-generates a unique URL per PR (e.g., `my-project-pr-7.vercel.app`). It's live as long as the PR is open. Closing/merging the PR doesn't delete it.
- **Production URL** = the URL for `main` branch (e.g., `my-project.vercel.app`). We're not using this for client previews, just for our internal dashboard.

### Prerequisites
- E1 complete (don't add a new load surface to an un-hardened chain)
- A Vercel account (free tier, larry@sealteamleaders.com) — **manual one-time setup, walk-through in E2.0**

### Tasks

**E2.0 — One-time Vercel setup walkthrough** (~30 min, Larry-driven, I narrate) — **DONE 2026-05-19**

- Sign up for Vercel with GitHub auth
- Generate a 1-year personal access token, scope: `Full Account` (Hobby tier has no finer-grained option)
- Add token to droplet: `/home/larry/credentials/.env.larry` as `VERCEL_TOKEN=...` (mode 0600, owner `larry:larry`; matches the existing template's reserved slot — the file is the systemd-units' `EnvironmentFile=` reference, NOT `/etc/systemd/system/ourliberty-secrets.env` as the original plan said)
- The `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` slots already exist in the template (empty); they fill in E2.3 when the dashboard Vercel project is connected
- Note: NEVER commit the token; it goes in `.env.larry` only (see `feedback_security_no_plaintext_secrets`)
- Token rotation discipline lives in `config/token-rotation-schedule.json` per E1.5 (see below); Beacon owns the calendar event

**E2.1 — `config/deploy_targets.json`** (~½ day)

- Schema: `{ "repo_name": { "vercel_project_id": "...", "framework": "nextjs", "env_var_keys": [] } }`
- Initially just one entry: the agent-core dashboard project (which we'll create in E3)
- Validation script: `scripts/validate_deploy_targets.py` (parse-time errors only; no live API check at validation time)

**E2.2 — `scripts/deploy_notifier.py`** (~1.5 days)

- New systemd service, runs continuously
- Polls Vercel API for deployments tagged with PR numbers from configured repos
- When a preview URL goes live, calls `larry_alerts.append_notification` with: source=`vercel-preview`, intent=`result-notification`, message=`Preview ready: <url>` + chat_id (Beacon thread for now)
- Caches "already notified" so each preview is announced once
- Cost: zero LLM calls; just API polling

**E2.3 — Connect first repo (the dashboard) to Vercel** (~½ day, Larry-driven)

- Create a new GitHub repo: `ourliberty-dashboard` (private)
- Connect it to Vercel via the Vercel dashboard (one-time, in browser)
- Verify a `git push` to that repo triggers a Vercel build automatically
- Test the notifier end-to-end: push a trivial change, see the preview URL land in Telegram

**Success criteria:** A push to `ourliberty-dashboard` triggers a Vercel preview build → URL appears in Beacon Telegram thread within ~2 min of CI green.

### Deferred from E2
- Production deploys (only previews for now)
- Supabase integration (no DB needed yet — dashboard is read-only on JSON files)
- Multi-repo support (will need it for first client, but YAGNI)
- Rollback automation (Vercel UI rollback is fine for now)

---

## Phase E3 — Dashboard B (Read-Only)

**Goal:** End terminal dependency for visibility. A web page at `dashboard.ourliberty.dev` (or similar) that shows what's happening on the droplet without you SSH-ing in.

### Prerequisites
- E2 complete (this dashboard *is* the dogfood deploy) ✅ (as of 2026-05-20)
- `ourliberty-dashboard` GitHub repo + Vercel project ✅ (created in E2.3, project_id `prj_b1jhpIqS8VDyZfDQvIoyzm32Rf6b`)
- Two subdomains via DNS — `dashboard.ourliberty.dev` (Vercel) + `api.ourliberty.dev` (droplet via Nginx + Certbot). ~15-20 min DNS + TLS config in E3.3.

### Architecture decisions locked 2026-05-20
- **Subdomain pattern (Q1=A):** two separate subdomains. `dashboard.*` points at Vercel via CNAME; `api.*` points at droplet IP `134.209.44.80` via A record, Nginx terminates TLS via Let's Encrypt.
- **Auth (Q2=A):** static shared-secret header `X-Dashboard-Token: <token>`. Token generated once, stored in `.env.larry` on droplet + as a Vercel project env var on the dashboard side. Joins the credential-rotation registry per E1.5 discipline (4 artifacts: token + registry entry + runbook + Beacon-owned annual scope-audit calendar event).
- **Update cadence (Q3=3 of 5):** 30s polling via SWR. Tune up to 15s (dial 2) if active-watching UX feels laggy, down to 60s (dial 4) if droplet load becomes a signal.
- **Transport (Q4=A):** HTTP polling. SSE is a Phase E6 trigger ("if Larry watches the dashboard for >10 min stretches and 30s feels laggy, build SSE then").

### Architecture

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│ Browser (you)   │ ──HTTPS─│ Vercel (Next.js) │ ──HTTPS─│ Droplet API     │
│                 │         │ dashboard UI     │         │ (FastAPI :8443) │
└─────────────────┘         └──────────────────┘         └─────────────────┘
                                                                  │
                                                                  │ reads
                                                                  ▼
                                                          ┌──────────────────┐
                                                          │ Droplet files:   │
                                                          │ - costs.jsonl    │
                                                          │ - cycle-journal  │
                                                          │ - inboxes/       │
                                                          │ - healers/last   │
                                                          └──────────────────┘
```

### Tasks

**E3.1 — Droplet-side JSON API** (~1.5 days)

- New Python service: `scripts/dashboard_api.py` (FastAPI or stdlib `http.server` — pick FastAPI for ease)
- Endpoints (all GET, all read-only):
  - `/health` — basic ping
  - `/agents/status` — for each agent: bot running? in-flight task? last activity?
  - `/tasks/recent?limit=20` — last N completed tasks across all agents with cost + duration
  - `/costs/today` — sum + breakdown by agent
  - `/costs/week` — same, weekly
  - `/cycle-journal/recent?n=5` — last N Pulse cycle entries
  - `/healers/status` — each healer's last run + success/fail
- Auth: shared secret in header (`X-Dashboard-Token`), token at `DASHBOARD_API_TOKEN` in `/home/larry/credentials/.env.larry`. Registered in `config/token-rotation-schedule.json` per E1.5 discipline (annual scope-audit, severity=high). CORS allows `Origin: https://dashboard.ourliberty.dev` only.
- Bind to localhost only initially; Nginx reverse proxy with HTTPS lands in E3.3
- Systemd service: `ourliberty-dashboard-api.service` (Type=simple, Restart=on-failure, EnvironmentFile=/home/larry/credentials/.env.larry)

**E3.2 — Next.js dashboard UI** (~1.5 days)

- Repo: `ourliberty-dashboard` (already exists, scaffolded in E2.3 with `create-next-app --typescript --tailwind --app`)
- Next.js + TypeScript + Tailwind + App Router (industry default, well-supported by Claude/Forge)
- Pages:
  - `/` — overview: 4 agent status cards, today's cost, in-flight tasks, last 5 cycle entries
  - `/tasks` — recent tasks table, sortable
  - `/costs` — cost trends (daily/weekly)
  - `/healers` — healer status grid
- Auto-refresh every 30s via SWR (locked Q3=3 of 5 — see Architecture decisions above)
- API token via Next.js env var `DASHBOARD_API_TOKEN` set in Vercel project settings (never committed). SWR fetcher wraps the `X-Dashboard-Token` header on every call.
- Loading state: spinner + last-known-good cached data. Error state: subtle banner "API unreachable — retrying every 30s" + keep showing cached data. Polish (skeleton screens, transitions) is E4+ territory.

**E3.3 — Nginx + HTTPS for the API** (~½ day, Larry-driven, I narrate)

- Install Nginx on droplet (one apt command)
- Reverse-proxy `https://api.ourliberty.dev` → `localhost:8443`
- Certbot for free SSL cert (Let's Encrypt — automated renewal)
- Firewall rule: only allow 443 in, drop everything else

**Success criteria:** You open a browser on your laptop, see the dashboard, watch a Forge task complete in near-real-time without opening a terminal.

### Deferred from E3
- Historical graphs (just lists for now)
- Filtering/search beyond basic sort
- Multi-user auth (it's just you)
- Mobile-responsive polish (works on mobile, not optimized)

---

## Phase E4 — Unified PM Dashboard (REWRITTEN 2026-05-24)

**Original scope** (this section, pre-2026-05-24): "Dashboard C (Interactive)" — buttons to approve markers, pause/resume agents, drop new spec tasks. That was the sketch.

**Actual scope** (current): unified PM surface hosting Larry's personal projects (migrated from Marvin Mission Control) AND agent OS build initiatives in the same Programs > Projects > Tasks model, backed by Supabase as the system's first persistent DB primitive. Trigger: ~6h of real E3 usage on 2026-05-21 surfaced the Telegram-as-database antipattern + Larry's adjacent need for unified personal PM.

**Goal:** Replace Marvin Mission Control with a unified dashboard. Telegram demoted to actionable comms only (clarifications, approvals, escalations); dashboard owns state surface.

**Prerequisites:** E3 deployed (✅ shipped 2026-05-21). ≥6h of E3 usage to surface the gaps (✅ provided the trigger).

### Canonical sub-spec docs (read these for scope, not the bullets below)

- `agents/beacon/specs/e4-overview.md` — phase overview + locked decisions table (14 rows across 5 design rounds)
- `agents/beacon/specs/e4-0-supabase-activation.md` — Supabase activation (✅ shipped)
- `agents/beacon/specs/e4-1-schema-v1.md` — schema v1 + 0002 + 0003 calibrations (✅ shipped)
- `agents/beacon/specs/e4-2-mission-control-migration.md` — MC → Supabase migration script (✅ shipped)
- `agents/beacon/specs/e4-4-dashboard-ui-rebuild.md` — UI rebuild split into 3 sub-sub-phases (E4.4a ✅ shipped; E4.4b/c pending)

### Sub-phase status (2026-05-24)

| Sub-phase | Status | PRs |
|---|---|---|
| E4.0 — Supabase activation | ✅ shipped 2026-05-24 | #78 agent-core + #2 dashboard |
| E4.1 — Schema v1 + 0002 GRANT + 0003 external_id | ✅ shipped 2026-05-24 | #3 + #4 + #5 dashboard + #95 agent-core spec |
| E4.2 — Mission Control migration script | ✅ shipped 2026-05-24 | #96 agent-core |
| E4.4a — MVP read-only dashboard UI | ✅ shipped 2026-05-24 | #6 dashboard |
| E4.4b — Kanban + drag-drop | 🟡 next | — |
| E4.4c — CRUD + forms | ⏳ pending after E4.4b | — |
| E4.3 — `pm_writer` + Beacon CLAUDE.md | ⏳ pending after E4.4 (reordered round 5) | — |
| E4.5 — Mission Control decommission | ⏳ pending after ≥1 week of new dashboard usage | — |

**Success criteria** (for full E4): Larry uses `dashboard.ourliberty.dev` daily for ≥1 week without going back to Mission Control; Telegram traffic drops ≥60%; agent OS dispatches auto-populate Supabase Tasks with full Events timeline visible in dashboard.

**Parallel small dispatches** (independent of E4.4 a/b/c sequence): P-1 (comms narrowing), P-2 (Plan-First Protocol), P-3 (adversarial fix auditor), P-4 (Promise-Before-Work + Unified Verifier + Sweep ledger). Defined in overview spec § 6.

---

## Phase E5 — Google Suite via MCP

**Goal:** Beacon reads/writes Google Docs, Gmail, Calendar through MCP connectors so specs get drafted in your native tool.

**Plain-language framing:**

- **MCP** = "Model Context Protocol." A standard that lets Claude talk to other services. There are pre-built MCP "connectors" for Google Drive, Gmail, Calendar, and dozens of other things.
- For you, MCP is the lightest path: no custom OAuth code, no new agent. You connect once via the Claude.ai connectors page; Beacon's Claude sessions inherit access.

### Prerequisites
- Google account (already have)
- Beacon running with Claude Code MCP support (which it already does)

### Tasks

**E5.1 — Connect Google connectors** (~15 min, Larry-driven)
- Browser: visit https://claude.ai/customize/connectors
- Click "Connect" on: Google Drive, Gmail, Google Calendar
- Authorize the OAuth scopes
- Done.

**E5.2 — Add MCP server config to Beacon agent** (~1 hr)
- Edit `agents/beacon/.claude/settings.json` (or wherever MCP servers are declared in our setup)
- Add the three Google MCP servers
- Restart Beacon's Telegram bot service
- Smoke test: ask Beacon "create a Google Doc titled 'test'" via Telegram, verify it appears in Drive

**E5.3 — Spec workflow update** (~1 hr)
- Update Beacon's `CLAUDE.md`: when Larry says "draft a spec for X," create a Google Doc, populate it with the spec template, share it back via link in Telegram.
- For ongoing collaboration: Beacon reads the doc, suggests edits, you accept/reject in Docs.
- When spec is final, Beacon converts it to a Forge task envelope and dispatches.

**Success criteria:** You can say "Beacon, draft a spec for a client onboarding form" and Beacon produces a Google Doc you can edit in your normal flow.

### Deferred from E5
- Calendar-driven scheduled work (would need a dedicated agent — Aide territory)
- Gmail auto-triage (same)
- Sheets-based reporting (push to Ledger agent if/when built)

---

## Phase E6 — Bench (Trigger-Based)

These are good ideas that we are NOT building in Phase E. Listed so we don't lose them.

- **Ledger agent + weekly cost digest** — trigger: variance becomes a real signal you want automated reporting on
- **Audit logger with chain hashing** (from upstream) — trigger: TruPath-adjacent work enters the system
- **Guardian dep versioning** (from upstream) — trigger: we add `agent-browser` or any pinned external binary
- **Production-deploy pipeline (vs preview)** — trigger: first prototype graduates to real users
- **Supabase activation** — trigger: first prototype needs persistent data
- **Post-merge smoke test pipeline (Playwright)** — trigger: prod deploys arrive
- **Outbox notifier decomposition** — trigger: a real bug surfaces (it's a 2,470-line monolith but 457 tests cover it)
- **Healer monitoring (per-healer heartbeat)** — trigger: a silent healer death causes a real incident
- **Dispatch replay log** — trigger: a lost-task incident
- **GitHub webhook bridge (issue → Beacon)** — trigger: someone other than Larry wants to file tasks

---

## Resuming Work Mid-Phase

When picking this up in a future chat, the first thing to do is read:
1. This file's **Current Status** block (below) — updated at the end of every working session
2. `operating-manual.md` Part II — for context on what shipped most recently
3. The relevant phase section above

Then ask: "where did we stop, and what's the next concrete task?" The Current Status block answers that.

---

## Current Status

**Last updated:** 2026-05-25 (E4 IN PROGRESS — E4.4d spec landed, chain discipline v2 shipped, page-cache investigation closed; chain healthy; E4.4d implementation queued as next dispatch)
**Current phase:** **E1, E1.5, E2, E3, E5 all DONE.** **E4 in progress.** Today (2026-05-25) shipped: PR #101 (Pulse Check I retry-counting heuristic), PR #102 (page-cache investigation Fix A + Fix B applied manually via `systemctl set-property MemoryHigh=3221225472`), PR #103 (marker-parser always-scan + regression-check foreground discipline + post-marker exit guidance), PR #104 (E4.4d sub-spec — Operations tab + System view chain telemetry MVP-2, authored Claude-as-Forge), PR #105 (chain discipline v2 — Mirror marker.py mandate + stale-daemon-code healer). Plus Larry's hotfix commit `0fc8f92` (Mirror CLAUDE.md regression-check polling discipline) and a new PM-dashboard project "Operational Config Self-Optimization" tracking the 10-surface self-optimization backlog. Sub-spec docs at `agents/beacon/specs/e4-overview.md` + `e4-0-supabase-activation.md` + `e4-1-schema-v1.md` + `e4-2-mission-control-migration.md` + `e4-4-dashboard-ui-rebuild.md` + `e4-4d-system-tab.md` are authoritative for current scope.
**Next concrete action:** **E4.4d PR-A dispatch** (Supabase migration `0004_chain_events.sql` — first of 4 sequenced PRs per E4.4d § 6 implementation staging). PR-A ships first (no deps); PR-B (ingestion daemon + Pulse Check III) and PR-C (droplet API endpoints) can ship in parallel after; PR-D (UI) ships last. Total MVP-2 estimate: ~$25 LLM, ~4.5 days wall, ~50 min Larry-time. After E4.4d ships: E4.4b (kanban reshape per Larry's E4.4a feedback), then E4.4c (CRUD), then E4.3 (`pm_writer`), then E4.5 (Mission Control decommission) after ≥1 week of new-dashboard usage.
**Blockers:** None. The "child-process leak" framing from 2026-05-24 was a misdiagnosis — Pulse off-cycle investigation (PR #102) found it was page cache from large dashboard worktree `node_modules`, not anonymous RSS. Fix A (cleanup retention 24h→4h) plus Fix B (MemoryHigh=3G) eliminated the doubling cadence. `memory.current` dropped 3.67 GB → 3.12 GB within seconds of Fix B applying. 8G MemoryMax stays as headroom.
**Open questions for Larry:** None outstanding. E4.4d implementation can dispatch on Larry's word.

**E3 completion summary (2026-05-21) — public dashboard live:**

`https://dashboard.ourliberty.dev` serves the live droplet dashboard end-to-end: 4 agent status cards (with `bot_model` discrimination — systemd-bot vs inbox-watcher), today's spend with by-agent breakdown, in-flight task count, recent cycle-journal entries (relative-time formatted), SWR auto-refresh every 30 s, dark theme, mobile-usable. Full proxy isolation — `DASHBOARD_API_TOKEN` never enters the browser bundle (verified `grep -r DASHBOARD_API_TOKEN .next/static` → 0 matches).

Stack: browser → Vercel (`dashboard.ourliberty.dev`, Vercel-managed TLS, valid through 2026-08-19) → Next.js route handler `app/api/proxy/[...path]/route.ts` (adds `X-Dashboard-Token` server-side) → Caddy (`api.ourliberty.dev`, Let's Encrypt TLS, valid through 2026-08-19, auto-renew) → uvicorn FastAPI on `127.0.0.1:8000`.

**Total E3 cost:** ~$25.27 LLM across 4 PRs (E3.1, E3.2, and 4 recovery PRs) + 3 dead-letters. ~50 min of chain wall-clock for the core work; ~3 hours including the cross-repo gap saga.

**E3.2 completion summary (2026-05-21, PR #1 in ourliberty-dashboard):**

- ✅ Replaces the default Next.js scaffold. App Router + TS strict + Tailwind 4 (CSS-first `@theme inline`, no `tailwind.config.js`) + SWR.
- ✅ **Server-side proxy pattern.** `app/api/proxy/[...path]/route.ts` is a catch-all Next 16 route handler. Forwards GET to `${DASHBOARD_API_URL}/${path}` with `X-Dashboard-Token` from `process.env`. 5 s timeout → 504; fetch throw → 502; upstream non-2xx → status + body passthrough; `Cache-Control: no-store` on success. Browser only hits same-origin proxy routes, so CORS doesn't matter for preview-deploy hostnames and the token never leaves the server.
- ✅ 4 pages: `/` overview (4 `<AgentStatusCard>` grid + today's `<CostBlock>` + in-flight count + recent cycle-journal list), `/tasks` (20-row table), `/costs` (today + 7-day breakdown by agent + by day), `/healers` (responsive card grid with last_result badges + kill_switch warnings).
- ✅ 9 components: `<Nav>` with `usePathname()` active highlighting, `<AgentStatusCard>`, `<TaskRow>`, `<CostBlock>`, `<HealerCard>`, `<LoadingState>`, `<ErrorBanner>`, `<RelativeTime>` (`Intl.RelativeTimeFormat` + 60 s re-render), `<LastRefresh>`.
- ✅ Typed `lib/api.ts` `useDashboardData<T>(path)` SWR hook + `lib/types.ts` mirroring all 7 E3.1 response shapes + `lib/env.ts` server-side env loader with `MissingEnvError`.
- ✅ 20 Vitest tests across proxy route (forwarding + 405 + 500 + 502 + 504 + passthrough + no-store), env loader, `formatRelative`.
- ✅ Bundle-leakage check: `.next/static` has 0 matches for `DASHBOARD_API_TOKEN`. 4 matches in `.next/server` are server-only `process.env.DASHBOARD_API_TOKEN` lookups (var name, not value).
- ✅ `README.md` rewritten with SSH-tunnel local-dev workflow (`ssh -L 8000:127.0.0.1:8000 -N larry@134.209.44.80` + `.env.local`). `.env.local.example` committed.

**E3.3 completion summary (2026-05-21) — no PR, purely operational:**

- ✅ Caddy was already on droplet (port 80 conflict caught the planned Nginx install). Pivoted to Caddy + auto-TLS rather than nginx + certbot — simpler. Removed the nginx package, kept Caddy.
- ✅ `/etc/caddy/Caddyfile` rewritten with `api.ourliberty.dev { reverse_proxy 127.0.0.1:8000 }` block + `:80, :443 { respond "Not Found" 404 }` fallback for non-matching hostnames. Backup at `/etc/caddy/Caddyfile.bak.<ts>`.
- ✅ Cloudflare DNS (Cloudflare-hosted zone, gray-cloud / DNS only): `A api.ourliberty.dev → 134.209.44.80`, `CNAME dashboard.ourliberty.dev → cname.vercel-dns.com`. Propagation was near-instant. Both records must be DNS only (not proxied) — Caddy provisions our own TLS for the api side, Vercel manages TLS for the dashboard side, and Cloudflare's orange-cloud proxy would conflict with both.
- ✅ Caddy's ACME flow provisioned the Let's Encrypt cert via HTTP-01 challenge within ~1 second of DNS resolving. Auto-renews; no certbot maintenance needed.
- ✅ Vercel: added `dashboard.ourliberty.dev` as a custom domain on the `ourliberty-dashboard` project (Vercel-managed TLS auto-provisioned). Added `DASHBOARD_API_URL=https://api.ourliberty.dev` env var (Production + Preview, alongside the existing `DASHBOARD_API_TOKEN`). Redeployed the merge commit via the Deployments UI so the new env vars baked in.
- ✅ Firewall already had 80/443 open (Caddy's existing setup). No ufw changes.

**Cross-repo chain gap recovery (2026-05-20/21) — PRs #65-#68:**

First-ever Forge dispatch against a non-`ourliberty-agent-core` `target_repo` surfaced 8 chain gaps across 4 architectural layers. Each was at a different layer, so they only revealed themselves as the previous gap was closed.

- **PR #65 — chain gap #7** (allowlist scope): `config/agent-models.json` `forge.allowed_repos` + `mirror.allowed_repos` += `ourliberty-dashboard`. Beacon dead-lettered the dispatch cleanly + diagnosed the gap precisely.
- **PR #66 — chain gap #8** (canonical filesystem paths): `scripts/inbox_watcher.py:72` `CANONICAL_REPO_PATHS` dict + `scripts/cleanup_stale_worktrees.py:39` `CANONICAL_REPOS` list both gained the dashboard repo entry. Both files have TODOs to move this mapping to `config/agent-models.json` under a `repo_paths` block — deferred (would add new code paths needing tests; not blocking E3).
- **PR #67 — chain gaps #9-#13** (T0 tier classification): `shared/REPO-GUARDRAILS.md` T0 row, `agents/mirror/CLAUDE.md` tier list, `agents/forge/CLAUDE.md` tier list (line 27) + allowed_repos statement (line 125), `agents/forge/TOOLS.md` repo authority table, and the critical `scripts/heal_pr_auto_merge.py:77` `REPOS` list — without that REPOS update, Mirror PASS on a dashboard PR would silently fail to auto-merge.
- **PR #68 — chain gap #14** (systemd sandbox): `systemd/ourliberty-inbox-watcher.service` + `systemd/ourliberty-outbox-notifier.service` `ReadWritePaths` += `/home/larry/ourliberty-dashboard`. Without this, `git worktree add` against the dashboard canonical repo hit "Read-only file system" inside the systemd namespace (ProtectHome=read-only + path not whitelisted).

**Operational requirements surfaced (not git gaps but operational gates worth documenting):**

- New repo MUST be cloned at `/home/larry/<repo-name>` on the droplet before dispatch (no auto-clone today).
- `inbox_watcher.py` reads `agent-models.json` at process startup — config edits require `sudo systemctl restart ourliberty-inbox-watcher.service`. Same for any Python source change (`CANONICAL_REPO_PATHS`).
- systemd unit changes need `sudo cp` to `/etc/systemd/system/` + `daemon-reload` + restart.

**Mirror oddities (non-blocking, worth tracking):**

- **First-pass marker-error pattern.** PRs #63, #64, #65, #66, #68 all had Mirror's first review attempt emit `REVIEW_PASS` without the `===` delimiters; parser couldn't extract JSON. All self-recovered on second-pass retry. ~$0.30 wasted per first attempt. Worth investigating Mirror's CLAUDE.md marker-discipline emphasis for short-PR contexts.
- **One-off URL hallucination on PR #66.** Marker payload contained `pr_url: ".../lyatch-ourliberty/ourliberty-agent-core/pull/66"` (wrong org name). Auto-merge failed; fixed by manual `gh pr merge 66 --squash --delete-branch`. Did not recur.

**Larry-actions completed during E3 ship:**

- Vercel `DASHBOARD_API_TOKEN` env var set via Chrome MCP
- Cloudflare A + CNAME records added via Chrome MCP
- Vercel `dashboard.ourliberty.dev` custom domain added via Chrome MCP
- Vercel `DASHBOARD_API_URL` env var added via Chrome MCP
- Vercel redeploy triggered to bake in env vars
- Beacon DM for `DASHBOARD_API_TOKEN` rotation calendar event (PR #63 pasted URL into registry)

**E3.1 completion summary (2026-05-20):**
- ✅ `scripts/dashboard_api.py` — FastAPI app exposing 7 read-only GET endpoints (`/health`, `/agents/status`, `/tasks/recent`, `/costs/today`, `/costs/week`, `/cycle-journal/recent`, `/healers/status`). Pydantic response models per endpoint (free OpenAPI schema); pure `_reader_*` helpers so tests can call readers directly without TestClient. `OURLIBERTY_AGENTS_ROOT` env override mirrors the E2.2 `deploy_notifier.py` pattern for tmpdir isolation. Constant-time `secrets.compare_digest` on the `X-Dashboard-Token` header; CORS allows exactly one origin (`https://dashboard.ourliberty.dev`) — preview URLs route through a Vercel env-var indirection in E3.2 rather than widening CORS here. FastAPI auto-docs at `/docs` + `/openapi.json` gated by the same auth dependency. Cycle-journal parser is lenient (regex matches `## Iteration N — YYYY-MM-DD HH:MM UTC` headers and surfaces `parse_warnings` rather than 500-ing on unfamiliar entries). `bot_active=null` + new `bot_model` field disambiguates systemd-bot (beacon/forge) from inbox-watcher (mirror/pulse). subprocess calls to `systemctl is-active` + `systemctl list-timers` are best-effort with 5 s timeouts; return None on failure rather than blowing up the request.
- ✅ 50 unit tests with `unittest.TestCase`. `setUpModule` redirects `OURLIBERTY_AGENTS_ROOT` to a tmpdir before module import (same pattern as `test_deploy_notifier.py`). Coverage: auth (missing / wrong / correct header + sweep across all endpoints + constant-time source check + misconfigured-server case), CORS (allowed-origin preflight 200 + disallowed-origin no-headers + simple-GET with origin), health (shape + perf <100 ms + no-prod-pollution), agents-status (empty root + in-flight counts ignoring non-task-prefixed files + bot_model disambiguation + last_activity = max mtime across inbox+outbox archives), tasks-recent (empty costs + ordering most-recent-first + limit 422 caps + in_flight detection + outbox classification + multi-row cost aggregation), costs-today (UTC day boundary at 23:59 + by_agent shape), costs-week (7 buckets + inclusive 6-days-ago edge + per-day aggregation), cycle-journal (missing-file warning + iteration parse + n=51 422 + 4 KB body truncation), healers (empty + heartbeat listing + stale via 2× cadence + log ERROR classification + kill-switch active), path-isolation (prod log untouched), docs gating (401 without auth, 200 with auth, OpenAPI spec lists all 7 paths), and direct reader-function unit tests.
- ✅ `systemd/ourliberty-dashboard-api.service` — `Type=simple` long-running unit; `Restart=on-failure`, `RestartSec=5s`; logs to `~/agents/logs/dashboard-api.log`. No timer (continuous service, not oneshot). `systemd-analyze verify` passes.
- ✅ `systemd/INSTALL.md` updated with a new "Dashboard API (E3.1)" subsection covering `pip3 install --user --break-system-packages fastapi 'uvicorn[standard]' httpx`, token generation via `secrets.token_urlsafe(32)`, install + enable + start, and the curl smoke test.
- ✅ `config/token-rotation-schedule.json` adds the `DASHBOARD_API_TOKEN` entry with all 15 schema fields. `next_rotation_due` = 2027-05-20. `calendar_event_url` intentionally null at PR time per the spec — Larry DMs Beacon post-merge to create the annual scope-audit event and a small follow-up commit pastes the URL.
- ✅ `docs/runbooks/rotate-dashboard-api-token.md` — full rotation runbook covering BOTH the droplet `.env.larry` half AND the Vercel project env-var half (the token is shared and must rotate together). Follows the `rotate-vercel-token.md` template; 9 numbered steps + rollback + related-links section.

**E2.2 completion summary (2026-05-20):**
- ✅ `scripts/deploy_notifier.py` — Type=oneshot 2-min poller; mirrors `sync_deploy_targets.py` patterns (stdlib urllib + kill-switch + dry-run-by-default + activation DM + state-file dedup + infra-alert throttle). State key `<uid>:<state>` so a deployment that transitions `READY → ERROR` re-DMs once with the new severity; `notified` history capped at 1000 entries (FIFO prune). Per-target `branch_filter` (null = match all; glob like `forge/*` via fnmatch). PR# resolution priority: `deployment.meta.githubPrId` → `gh pr list --head ... --repo ...` fallback → `PR #(unknown)` literal in the body. 401/403 → `critical` `INFRASTRUCTURE_ALERT` DM throttled to once per 24 h. 5xx / network → log + exit non-zero, no DM (systemd retries on the next 2-min tick).
- ✅ Path-isolation fix scoped to this script: `AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))`. Tests monkeypatch the env var before import so they write logs / state to a tmpdir instead of polluting `~/agents/logs/deploy-notifier.log`. (Sibling healers still hardcoded; tracked as a separate followup.)
- ✅ `systemd/ourliberty-deploy-notifier.{service,timer}` — `Type=oneshot`, `OnBootSec=2min`, `OnUnitActiveSec=2min`, `RandomizedDelaySec=15s`. Default off (`OURLIBERTY_DEPLOY_NOTIFIER_ENABLED=false`) until E2.3 activation per the service file's commented snippet.
- ✅ 37 unit tests with mocked `_vercel_get`, mocked `dm_larry`, mocked `gh_runner` covering: empty-registry early-return, READY/ERROR DMs, BUILDING/CANCELED skip, branch_filter glob/null/mismatch, meta-PrId / gh-CLI / unknown PR resolution, uid+state dedup + transition re-DM, dry-run activation DM (once), missing-VERCEL_TOKEN infra alert, 401 auth alert + 24h throttle, 5xx no-DM, 2-page pagination + 5-page cap, LRU prune at 1000, CLI `--once` / `--dry-run` flags, path-isolation guard.
- ✅ `systemd/INSTALL.md` updated (eleven → twelve scripts); new `Deploy-notifier pattern (E2.2)` subsection.
- ✅ `docs/phase-e-plan.md` Current Status block updated.

**E2.1 completion summary (2026-05-20):**
- ✅ `config/deploy_targets.json` — schema-versioned registry; initial state is intentionally empty (first real entry lands in E2.3 with real Vercel IDs).
- ✅ `scripts/validate_deploy_targets.py` — stdlib offline validator, library + CLI surface, mirrors E1.5's `validate_token_rotation_schedule.py`.
- ✅ `scripts/sync_deploy_targets.py` — 12 h Vercel-side drift detector with three drift kinds (`MISSING_FROM_REGISTRY`, `MISSING_FROM_VERCEL`, `NAME_MISMATCH`), 24 h re-DM window per item, dry-run-by-default activation gate (`OURLIBERTY_DEPLOY_TARGETS_SYNC_ENABLED=true`), and a critical `INFRASTRUCTURE_ALERT` path for Vercel auth failures. Cadence decision (2026-05-20): 12 h, not 6 h — Vercel state changes slowly.
- ✅ `systemd/ourliberty-sync-deploy-targets.{service,timer}` — `Type=oneshot`, `OnBootSec=15min`, `OnUnitActiveSec=12h`, `RandomizedDelaySec=10min`, default off until activated per service-file commented snippet. `systemd-analyze verify` passes.
- ✅ 41 validator tests + 29 sync tests; full repo suite green.
- ✅ `systemd/INSTALL.md` updated (ten → eleven scripts); `agents/mirror/CLAUDE.md` gains an additive `deploy_targets.json` review checklist (existing E1.5 items untouched).

**E1 completion summary (2026-05-19):**
- ✅ **E1.1** — `render_marker` helpers in 3 handlers + `scripts/marker.py` CLI + drift tests (PR #40). 167 tests pass; agents now produce canonical marker text via Bash instead of hand-typing delimiters. PR #16's silent-dead-letter shape (bare `REVIEW_PASS`) made structurally impossible.
- ✅ **Hygiene** — 13 stale tests fixed (PR #41): macOS path symlinks in worktree tests, cost-budget cap change from $5→$15, ledger task_type inference drift after PRs #33/#34. Full suite now 690+ green.
- ✅ **E1.2** — `expected_agent` parameter added to `agent_runner.run_claude` (PR #42); identity-assertion preamble gating moved out of `inbox_watcher.process_task` into a centralized helper `_maybe_prepend_identity_assertion`. Watcher is 14 lines simpler. Discovered during framing that D2.5 had already done most of E1.2; this PR closed the small architectural cleanup.
- ✅ **E1.3** — `scripts/heal_pr_auto_merge.py` healer (PR #43, 380 lines + 320 lines of tests). Adapted from upstream gm-agent-core #240 (pulled `is_mergeable`, CANCELLED-rerun, blast-radius cap) plus Larry-specific additions (Mirror-PASS detection via `outbox-notifier.log` scan, per-PR retry budget, two-layer kill-switch, Telegram DMs for activation + stalled-PR alerts). Systemd timer at 5-min cadence, default DRY-RUN mode until `OURLIBERTY_AUTOMERGE_ENABLED=true` is set per the activation DM the healer sends on first real candidate.

**Critical-path E1 unblocks E2.** The deploy layer can land safely on top of a hardened chain.

---

**E5 progress (this session, 2026-05-18):**
- ✅ New Google account created: `agent.beacon.ourliberty@gmail.com`
- ✅ Google Cloud OAuth client created (kept on disk as backup path, not used by current design)
- ✅ Self-hosted workspace-mcp installed + registered (kept on disk, registration removed — reusable later if we ever want per-agent Google isolation)
- ✅ **Decision pivot mid-session:** discovered Claude Code on droplet was inheriting Larry's personal claude.ai MCP connectors. Resolved by signing droplet into a separate Anthropic Max plan tied to the agent Google account. Cleaner architecture than self-hosted MCP.
- ✅ Separate Anthropic Max plan signed up for the agent account
- ✅ Personal credentials backed up on droplet (3 backup files in `~/.claude/`)
- ✅ Droplet now auth'd as agent account via `claude auth login --claudeai` (orchestrated via PTY script + manual code paste — see `/tmp/auth_orchestrator.py` on droplet for the pattern)
- ✅ `claude mcp list` confirms agent-account-only connectors (no personal Drive/Gmail/Calendar visible)
- ✅ Smoke tests pass: list_recent_files + create_file via Drive against agent account
- ✅ `agents/beacon/.claude/settings.json` updated on droplet to allow 25 Google MCP tools (Drive/Gmail/Calendar, read+create+update, no delete-class tools)
- ⏳ Telegram smoke test (in progress with Larry — should land URL of created doc in his Beacon thread)

**Architecture decisions locked this session:**
- Droplet uses a dedicated Anthropic Max plan, NOT API key auth (Larry preferred OAuth-based subscription billing over per-token)
- Droplet's claude.ai account = agent.beacon.ourliberty@gmail.com (same email as Google identity, simpler)
- delete-class tools intentionally excluded from Beacon allow list (Beacon should not be able to delete user data without explicit approval)

**E5 update (2026-05-19 — workspace-mcp wired):**
- ✅ Decision: Path 1 (re-enable existing workspace-mcp install on droplet) over Path 3 (custom Google Docs API wrapper). Probe-first strategy — if workspace-mcp proves reliable in real use, we keep it; otherwise we revisit a thin custom wrapper.
- ✅ `claude mcp add workspace-mcp --scope user -e GOOGLE_OAUTH_CLIENT_ID=... -e GOOGLE_OAUTH_CLIENT_SECRET=... -- uvx --from workspace-mcp workspace-mcp --single-user --tools docs drive --transport stdio` registered.
- ✅ OAuth user-token bootstrapped via PTY orchestrator + SSH-tunneled localhost:8000 (same pattern as `claude auth login`). Token at `~/.google_workspace_mcp/credentials/agent.beacon.ourliberty@gmail.com.json` (NOT `~/.config/workspace-mcp/` — gotcha, doc updated).
- ✅ Required adding `agent.beacon.ourliberty@gmail.com` as a Test User in Cloud Console (`beacon-agent` project, OAuth consent screen). Standard for unpublished OAuth clients in Testing mode.
- ✅ End-to-end smoke verified: `create_doc` → `update_drive_file` (move to `Shared with Larry/inbox/`) → `find_and_replace_doc` → `modify_doc_text` (append) → `get_doc_as_markdown` (readback). Sharing inheritance works on a move; Larry confirmed doc visible in his personal Drive.
- ✅ Closes the known E5 share-file gap: workspace-mcp DOES have `set_drive_file_permissions` + `manage_drive_access`. We **intentionally excluded** them from Beacon's allowlist (same posture as delete tools — sharing changes escalate to Larry).
- ✅ Convention nailed: workspace-mcp owns Docs/Drive primary ownership; claude.ai connectors stay for Gmail/Calendar; claude.ai Drive kept as fallback for week 1 then pruned.
- ✅ Beacon `.claude/settings.json` allowlist expanded from 27 → 56 tools (added 29 workspace-mcp tools, excluded 6: `start_google_auth`, both `debug_*`, `check_drive_file_public_access`, `manage_drive_access`, `set_drive_file_permissions`).

**Known follow-ups (small):**
- Personal credentials backups in `~/.claude/` (3 files) can be cleaned up once new auth is fully validated — auth has been stable >24h as of 2026-05-19.
- Smoke-test Doc `2026-05-19 - workspace-mcp smoke test` in `Shared with Larry/inbox/` should be deleted by Larry (Beacon can't — intentionally) once we're past the smoke phase.
- E5.3 (spec workflow update in Beacon's CLAUDE.md): teach Beacon when to draft to Google Docs vs Telegram, and how the Doc → marker → Forge dispatch flow works. Design agreed: Doc-as-surface, marker-as-source-of-truth, offer-to-summarize.

**Recent commits in agent-core that bear on this plan:**
- PR #30 — per-agent allowlist sweep (related to E1 hardening posture)
- PR #31 — telegram bot strip-leading-slash (UX precursor to E4)
- PR #32 — push_with_rebase fallback (cycle reliability, indirect support for E1)
- PR #33 + #34 — Ledger v2 + task_type inference (related to future Ledger work in E6)
- PR #35 — pulse_check_i filter fix (cycle quality, indirect E1 support)
- PR #36 — docs/phase-e-plan (this plan)

---

## Conventions for This Plan

- **Effort estimates** are honest median estimates assuming I'm doing the technical work and Larry is doing approval/learning. Multiply by ~1.5 for stretch days where Larry is digging deeper into a concept.
- **"Larry-driven"** tasks are ones requiring browser/account access I don't have. I'll narrate as we go.
- **Every phase has explicit "Deferred from..." items** so we don't quietly let scope creep in.
- **Success criteria are concrete and demonstrable** — not "improved" or "working" but "this specific thing happens when this specific input goes in."
- **Plans are revised in place**, not appended. If we change direction mid-phase, edit this doc; the operating-manual gets the append-only narrative.
