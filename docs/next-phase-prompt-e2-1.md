# Next-phase resume prompt — E2.1 kickoff

**Copy this into a fresh Claude Code session when you're ready to start the next phase.** It's self-contained — designed so a Claude with no memory of the 2026-05-19 session can pick up cleanly.

---

We just closed Phase E2.0 + E1.5 on 2026-05-19. Vercel token installed + verified, credential rotation discipline shipped end-to-end (registry + 2 drift healers + Pulse extension + 8 runbooks + Mirror-enforced 4-artifact convention + headless Beacon APPROVAL_REQUEST handler + source-routing narrowing fix), and **5 PRs landed via the agent OS chain (#45 design, #46 implementation, #47 chat-ID followup, #48 task #17 headless Beacon handler, #49 task #19 source-routing narrowing fix)**. Test suite at ~1050 green. Total LLM spend ~$25 for the session.

**Next phase per the canonical roadmap is E2.1 — `config/deploy_targets.json` schema design**. Small spec + validator, ~½ day. After E2.1 closes, E2.2 builds the `scripts/deploy_notifier.py` daemon (~1.5 days), then E2.3 connects the first repo (`ourliberty-dashboard`, new private repo) to Vercel + verifies end-to-end (~½ day).

## Read these in this order, then summarize back where we are

1. **Your auto-memory** at `~/.claude/projects/-Users-Larry-Desktop-Rocket-Station-PResentation/memory/MEMORY.md` — particularly:
   - `project_phase_e1_5_complete.md` — what shipped 2026-05-19
   - `feedback_credential_rotation_discipline.md` — the 4-artifact rule that's now load-bearing for every credential install
   - `feedback_headless_mode_chain_gaps.md` — pattern: when Claude dispatches Beacon/Forge headlessly, expected friction (now mostly closed by PR #48 + PR #49)
   - `project_phase_e_plan.md` — the canonical roadmap pointer
   - `user_profile.md` — Larry's role + collaboration mode
   - `feedback_decision_classification.md` — TECHNICAL/ARCHITECTURAL/VALUES classification before asking
   - `feedback_plain_overview_before_questions.md` — system-level frame before architectural questions

2. **`~/Desktop/ourliberty-agent-core/docs/phase-e-plan.md`** — read the Current Status block at bottom FIRST (last updated 2026-05-19 post task #19), then the entire E2 phase section (E2.1 through E2.3) in detail. The plan says E2.1 is a JSON schema for `config/deploy_targets.json` with: `{ "repo_name": { "vercel_project_id": "...", "framework": "nextjs", "env_var_keys": [] } }` and a parse-time validator script.

3. **`~/Desktop/ourliberty-agent-core/config/token-rotation-schedule.json`** — read this for shape inspiration. The E2.1 deploy_targets registry should follow a similar schema-versioned + validator-backed shape (since the convention from E1.5 is "every config file gets a validator + drift healer when appropriate").

4. **`~/Desktop/ourliberty-agent-core/shared/credentials-discipline.md`** — if E2.1 involves any new credentials (it shouldn't — VERCEL_TOKEN is already installed), the 4-artifact rule applies. Likely E2.3 (not E2.1) is where this matters: connecting a real repo to Vercel may not need new credentials but `VERCEL_ORG_ID` + `VERCEL_PROJECT_ID` placeholders need to be filled then.

5. **`~/Desktop/ourliberty-agent-core/docs/operating-manual.md` Part II** — scroll to the bottom for the 2026-05-19 entry (E2.0 + E1.5) to absorb the build narrative for what just shipped. Note the five architectural findings, four closed and one (DM delivery delay) deferred to E6.

## Context that matters from the last session

- **E1.5 is FULLY closed**. All five architectural findings from the E1.5 session are addressed:
  - #1 Source-routing gap → FIXED in PR #46
  - #2 Healer install drift → FIXED + new install-drift healer in PR #46
  - #3 DM delivery delay → noted, deferred to E6 polish
  - #4 Headless Beacon APPROVAL_REQUEST handler → CLOSED in PR #48 (task #17)
  - #5 PR #46 source-routing over-broad interception (Forge PROCEED hijacked) → CLOSED in PR #49 (task #19)
- **Headless-mode chain now works end-to-end after PR #49.** Future Claude-driven Beacon → Forge → Mirror → auto-merge dispatches should run without manual bridges (validated on task #19's own chain — Forge build → Mirror auto-dispatch → auto-merge fired autonomously after PR #46+#48+#49 code was loaded in the daemons).
- **The new credential-drift healer is live + flagging zero drift**. Runs every 6h. Future credential install that skips the registry entry → DM within 6h.
- **The systemd-install-drift healer is live + flagging zero drift**. Runs every 12h. Future PR that ships systemd files but skips operator install → DM within 12h with copy-pasteable install commands.
- **`.env.larry` is clean**: 7 active values (4 Telegram bot tokens + 2 chat-ID identifiers + VERCEL_TOKEN). No empty placeholder slots. When E2.3 fills VERCEL_ORG_ID + VERCEL_PROJECT_ID, do so via the 4-artifact discipline.
- **Both daemons (outbox-notifier + inbox-watcher) restarted at end of session** with PR #46 + #48 + #49 code loaded. Source-routing fix + narrowing fix + headless Beacon handler all active in the running daemon memory.

## What we're picking up — primary task: E2.1

Per phase-e-plan.md Phase E2.1 section:

- Design the JSON schema for `config/deploy_targets.json`
- Each entry has: `name`, `vercel_project_id`, `framework`, `env_var_keys` (list of `.env.larry` keys that should be passed as Vercel project env vars), maybe `branch_filter` (which branches trigger preview deploys)
- Build `scripts/validate_deploy_targets.py` (parse-time errors only; no live API check at validation time)
- Probably initially has one entry: the agent-core dashboard project (which we'll create in E3)
- Bundle with phase-e-plan.md Current Status update

This is a smaller piece (~3-4 hours of design + dispatched implementation). Expected to land in 1-2 sessions.

## But — don't jump into E2.1 commands

Per the established pattern (`feedback_plain_overview_before_questions`):

1. Read the phase-plan + relevant memories first
2. Give Larry the plain-language top-level frame (what we're building, why now, what this unlocks) BEFORE technical questions or commands
3. Define any new terms on first use with Google-Apps analogies where they fit (`feedback_explain_tech_with_google_analogies`)
4. Classify each decision before asking (TECHNICAL → just decide; VALUES/ARCHITECTURAL → present as A/B with recommendation)
5. Surface OPEN questions early
6. Don't dive into code or dispatches until Larry's seen the frame + open questions and said go

## Resumption sanity check

After memory + plan read-in, send Larry:
- (a) A one-paragraph "where we are" summary
- (b) Drift findings (anything between the docs and disk that's stale)
- (c) Plain-language E2.1 frame (what we're building, why, what it unlocks) plus the open questions
- (d) Wait for his go

## Likely open questions for E2.1

- **Repo enumeration strategy**: do we hardcode the list in `deploy_targets.json` (manual upkeep), or auto-discover from Vercel API on each notifier tick (auto-sync but coupling)? Recommend hardcode for E2.1 (1 repo initially); auto-discover when we have >3 repos.
- **Branch filter default**: do we want preview builds on ALL branches by default, or only branches matching a pattern (e.g., `forge/*` for Forge-opened PRs)? Recommend all branches initially; tune if Vercel free tier limits become a real signal.
- **Validator scope**: just JSON schema (cheap), or also live Vercel API verification that the `vercel_project_id` exists (slower, requires API call)? Recommend schema-only for E2.1; live verification can land in E2.2's notifier.

These are TECHNICAL/preference choices — surface, decide-and-move.

## Collaboration mode reminders

- Larry is a founder/operator with Google Apps-only background. Define new infra/web-stack terms on first use; use Google-Apps analogies where they fit naturally.
- Build complete and robust, not thin and fast. Estimate in design depth.
- Classify decisions (technical / architectural / values) before asking — don't fake-ask on technical calls.
- Lead with plain-language top-level frame before architectural questions.
- Larry prefers terse Q&A from himself but values substantive analysis from you. Length is fine when load-bearing.
- For values/architectural decisions, present as A/B with your recommendation and the main tradeoff — let him redirect, don't decide.
- Don't commit / push / merge / open PRs without explicit authorization. Each PR is a separate confirmation.
- Headless-mode chain works end-to-end after PR #48 + #49. You can dispatch Beacon → Forge → Mirror → auto-merge without manual bridges. If you DO hit a stall, surface it immediately + diagnose — there shouldn't be any open gaps as of session close.

Start by reading the five sources above and reporting back: (a) one-paragraph "where we are" summary, (b) any drift between docs and disk, (c) plain-language E2.1 frame plus the open questions you'd like Larry to answer before E2.1 begins.
