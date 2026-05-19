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
| **E1** | Hardening (markers, watcher, auto-merge) | ~3 days | — | Not started |
| **E2** | Deploy layer (Vercel preview-first) | ~3–4 days | E1 | Not started |
| **E3** | Dashboard B (read-only) | ~3 days | E2 (dogfood) | Not started |
| **E4** | Dashboard C (interactive) | ~1 week | E3 + 1 week's usage | Not started |
| **E5** | Google Suite via MCP for Beacon | ~½ day | — (can run parallel) | Not started |
| **E6** | Bench items (Ledger, audit logger, Guardian, prod deploy, etc.) | — | Trigger-based | Deferred |

Critical path: **E1 → E2 → E3 → E4**. E5 can run in parallel with any of E1–E4.

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

**E2.0 — One-time Vercel setup walkthrough** (~30 min, Larry-driven, I narrate)

- Sign up for Vercel with GitHub auth
- Generate a personal access token, scope: `Full Access` (we'll restrict later)
- Add token to droplet: `/etc/systemd/system/ourliberty-secrets.env`, mode 0600 (matches existing pattern)
- Note: NEVER commit the token; it goes in the secrets env file only (see `feedback_security_no_plaintext_secrets`)

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
- E2 complete (this dashboard *is* the dogfood deploy)
- A subdomain for it (e.g., `dashboard.ourliberty.dev`) — DNS is already with you, ~15 min config

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
- Auth: shared secret in header (`X-Dashboard-Token`), token in `ourliberty-secrets.env`
- Bind to localhost only initially; we'll add an Nginx reverse proxy with HTTPS in E3.3
- Systemd service: `ourliberty-dashboard-api.service`

**E3.2 — Next.js dashboard UI** (~1.5 days)

- New repo: `ourliberty-dashboard`
- Next.js + Tailwind (industry default, well-supported by Claude/Forge)
- Pages:
  - `/` — overview: 4 agent status cards, today's cost, in-flight tasks, last 5 cycle entries
  - `/tasks` — recent tasks table, sortable
  - `/costs` — cost trends (daily/weekly)
  - `/healers` — healer status grid
- Auto-refresh every 30s via SWR or react-query
- API token via Next.js env var (`DASHBOARD_API_TOKEN` — set in Vercel project settings, never in code)

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

## Phase E4 — Dashboard C (Interactive)

**Goal:** Replace Telegram as your primary cockpit. Buttons to approve markers, pause/resume agents, drop new spec tasks.

### Prerequisites
- E3 deployed and used for ≥1 week
- A list of "I wish I could do X from here" notes you've kept while using E3

### Tasks (sketch; refine after E3 usage feedback)

**E4.1 — Mutation API endpoints** (~2 days)
- `POST /approvals/{task_id}/approve` — bypasses Telegram approval flow
- `POST /approvals/{task_id}/reject` — same
- `POST /tasks` — drop a new task into the appropriate inbox (with full validation)
- `POST /agents/{name}/pause` — set EMERGENCY_HALT marker for one agent
- `POST /agents/{name}/resume` — clear it
- Auth: same shared-secret pattern + an HMAC signature on mutating requests

**E4.2 — Dashboard UI updates** (~3 days)
- Add buttons + confirmation dialogs
- Add a "draft a task" form (spec textarea, agent dropdown, priority)
- Toast notifications when actions succeed

**E4.3 — Telegram parity audit** (~½ day)
- Confirm every Telegram action has a dashboard equivalent
- Keep Telegram running in parallel; don't deprecate yet

**Success criteria:** You can run a full spec → PR → review → merge cycle without touching Telegram or terminal.

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

**Last updated:** 2026-05-18 (late session)
**Current phase:** E5 — Google Suite via MCP — verified live via CLI, Telegram smoke test in progress
**Next concrete action:** After Telegram smoke confirms, commit `agents/beacon/.claude/settings.json` change (currently only on droplet) and tackle E5.3 (spec workflow update in Beacon's CLAUDE.md). Then E1 hardening.
**Blockers:** None
**Open questions for Larry:** None outstanding

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
- Drive connector has no `share_file` / `set_permissions` tool — known gap for "Beacon drafts spec doc and shares to Larry's personal account." Workaround in E5.3.
- delete-class tools intentionally excluded from Beacon allow list (Beacon should not be able to delete user data without explicit approval)

**Known follow-ups (small):**
- `agents/beacon/.claude/settings.json` change is only on droplet; needs to land in repo via PR (small)
- Personal credentials backups in `~/.claude/` (3 files) can be cleaned up once new auth is fully validated (~1 day)
- workspace-mcp registration removed but install + OAuth client + client_secret.json remain on droplet (~5 min cleanup if desired)
- E5.3 (spec workflow update in Beacon's CLAUDE.md): teach Beacon when to draft to Google Docs vs Telegram, and how to handle the doc-sharing-back-to-Larry workflow

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
