# Next-phase resume prompt — E3 kickoff

**Copy this into a fresh Claude Code session when you're ready to start Phase E3.** It's self-contained — designed so a Claude with no memory of the 2026-05-20 session can pick up cleanly.

---

Phase E2 (Deploy Layer) fully shipped 2026-05-20 — 10 PRs in one day (#51 E2.1 deploy_targets registry, #52 E2.2 deploy_notifier, #53 AGENTS_ROOT path isolation, #54 stale tests, #55 chain gap #5, #56 outbox_notifier isolation, #57 Mirror regression gate dial-3, #58 E2.3 deploy_targets entry, #59 docs closeout, #60 chain gap #6). End-to-end smoke verified: `git push` to `ourliberty-dashboard` → Vercel preview build → Telegram DM in ~5 min.

**Next phase per the canonical roadmap is E3 — Dashboard B (read-only).** Replace the default Next.js scaffold at `https://ourliberty-dashboard-*.vercel.app` with a real read-only dashboard rendering droplet state (agent status, recent tasks, costs, cycle-journal, healer status). ~3 days estimate.

## Read these in order, then summarize back where we are

1. **Your auto-memory** at `~/.claude/projects/-Users-Larry-Desktop-Rocket-Station-PResentation/memory/MEMORY.md` — particularly:
   - `project_phase_e2_2_complete.md` — what shipped 2026-05-20
   - `project_mirror_gate_posture.md` — dial-3 regression gate; Mirror reviews every PR via `scripts/test_regression_check.py`
   - `project_claude_as_forge_pattern.md` — when to bypass full Forge dispatch for trivial edits
   - `feedback_headless_mode_chain_gaps.md` — all 6 known chain gaps are now closed; treat any new manual bridge as a regression signal
   - `project_phase_e_plan.md` — canonical roadmap pointer
   - `user_profile.md` — Larry's role + collaboration mode

2. **`~/Desktop/ourliberty-agent-core/docs/phase-e-plan.md`** — read the Current Status block at bottom FIRST, then the entire E3 section (E3.1 through E3.3) in detail. The plan has been refined 2026-05-20 with Architecture decisions locked under E3 → Prerequisites:
   - Q1=A two subdomains (`dashboard.ourliberty.dev` + `api.ourliberty.dev`)
   - Q2=A static shared-secret header (`X-Dashboard-Token`)
   - Q3=3 of 5 dial (30s auto-refresh)
   - Q4=A HTTP polling (SSE deferred to E6)

3. **`~/Desktop/ourliberty-dashboard/`** — the existing Next.js scaffold. Default `create-next-app` output. `app/page.tsx` is what we replace in E3.2.

4. **`~/Desktop/ourliberty-agent-core/scripts/larry_alerts.py`** + **`scripts/deploy_notifier.py`** — for understanding how the droplet exposes state today + the conventions to mirror in `scripts/dashboard_api.py` (E3.1).

5. **`~/Desktop/ourliberty-agent-core/agents/mirror/CLAUDE.md`** — Mirror's review workflow now includes the dial-3 regression gate. Every PR you dispatch in E3 will go through it.

## Context that matters from 2026-05-20

- **All 6 known headless chain gaps are closed.** Future Claude-driven dispatches should run autonomously. If you find yourself needing a manual bridge, that's a regression signal — surface immediately.
- **Mirror's dial-3 regression gate is live.** Mirror runs `scripts/test_regression_check.py --parent-sha <BASE> --head-sha <HEAD>` before every REVIEW_PASS. Blocks on NEW failures; tolerates pre-existing (lists them in review body). Your specs don't need to remind Mirror — she'll do it per her CLAUDE.md.
- **Claude-as-Forge is a real velocity pattern for trivial edits.** Saves ~$3-5 + ~12 min for config / docs / one-line fixes. See `project_claude_as_forge_pattern.md` for when to use it. The bug it surfaced (chain gap #6) was closed in PR #60.
- **DASHBOARD_API_TOKEN doesn't exist yet** — generate it during E3.1 setup + register it in `config/token-rotation-schedule.json` per the E1.5 4-artifact discipline (token + registry entry + runbook at `docs/runbooks/rotate-dashboard-api-token.md` + Beacon-owned annual scope-audit calendar event).
- **Vercel project already exists** (`prj_b1jhpIqS8VDyZfDQvIoyzm32Rf6b`) + registered in `config/deploy_targets.json`. Pushing to `ourliberty-dashboard` already DMs Larry on preview URLs via `deploy_notifier`.
- **Subdomain DNS** is on Larry's side (not in any chain). When E3.3 needs `api.ourliberty.dev` to resolve to `134.209.44.80`, ask Larry to add the A record.

## E3 work split

**E3.1 — Droplet-side JSON API (~1.5 days):**
- `scripts/dashboard_api.py` — FastAPI app on `localhost:8443` reading `~/agents/blackboard/`, `~/agents/state/`, `~/agents/logs/`, `costs.jsonl`
- 7 GET endpoints per the phase plan (health, agents/status, tasks/recent, costs/today, costs/week, cycle-journal/recent, healers/status)
- `X-Dashboard-Token` auth via the new DASHBOARD_API_TOKEN credential
- CORS for `https://dashboard.ourliberty.dev` only
- `OURLIBERTY_AGENTS_ROOT` env-var override pattern (per PR #53)
- New systemd unit `ourliberty-dashboard-api.service` (Type=simple, Restart=on-failure)
- Unit tests with mocked filesystem reads + auth-header checks
- DASHBOARD_API_TOKEN 4-artifact discipline (token + registry + runbook + calendar event)

**E3.2 — Next.js dashboard UI (~1.5 days):**
- Edit `~/Desktop/ourliberty-dashboard/` — replace the scaffold's `app/page.tsx` with the real dashboard
- 4 routes per the phase plan (`/`, `/tasks`, `/costs`, `/healers`)
- SWR with `refreshInterval: 30000` + custom fetcher that wraps `X-Dashboard-Token`
- Loading: spinner + last-known-good cache. Error: banner + cache. No skeleton screens (E4+).
- API token via Vercel project env var `DASHBOARD_API_TOKEN`
- The PR opens against `ourliberty-dashboard` repo (not agent-core); deploy_notifier will DM Larry the preview URL

**E3.3 — Nginx + HTTPS for the API (~½ day, Larry-driven for browser steps, Claude narrates):**
- Larry adds A record `api.ourliberty.dev` → `134.209.44.80`
- Claude installs Nginx via apt + writes the reverse-proxy config (`api.ourliberty.dev` → `localhost:8443`)
- Larry runs Certbot interactively (or Claude orchestrates via PTY per the workspace-mcp pattern from E5)
- Firewall: ufw allow 443, deny everything except 22 + 443
- Smoke test: curl from laptop to `https://api.ourliberty.dev/health` with proper token

## Collaboration mode reminders

- Larry is a founder/operator with Google Apps-only background. Define new infra/web-stack terms on first use; use Google-Apps analogies where they fit naturally.
- Build complete and robust, not thin and fast. Estimate in design depth.
- Classify decisions (technical / architectural / values) before asking — don't fake-ask on technical calls.
- Lead with plain-language top-level frame before architectural questions.
- Larry prefers terse Q&A from himself but values substantive analysis from you. Length is fine when load-bearing.
- For values/architectural decisions, present as A/B with your recommendation and the main tradeoff — let him redirect, don't decide.
- Don't commit / push / merge / open PRs without explicit authorization. Each PR is a separate confirmation.
- Use Claude-as-Forge (per `project_claude_as_forge_pattern`) for trivial config + docs edits; dispatch the full Forge chain for new code paths.

## Likely open questions to surface at E3 kickoff

- **DASHBOARD_API_TOKEN generation:** secure random vs Larry-picks-it. Recommend `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` + paste into `.env.larry`.
- **Polling cadence per route:** is `/agents/status` (fast-changing) different from `/costs/week` (slow-changing)? Recommend single 30s cadence for E3, per-route cadence is E4.
- **FastAPI vs stdlib http.server:** plan says FastAPI. Confirm? (Recommend FastAPI — pydantic models give us free schema validation + free OpenAPI docs at `/docs`.)
- **First page to build:** all 4 in parallel, or `/` (overview) first then iterate? Recommend `/` first for fastest feedback loop.

Start by reading the five sources above and reporting back: (a) one-paragraph "where we are" summary, (b) any drift between docs and disk, (c) plain-language E3 frame plus the open questions you'd like Larry to answer before E3.1 dispatches.
