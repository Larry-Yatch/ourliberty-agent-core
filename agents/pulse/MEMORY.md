# Pulse — Long-term Memory

*Distilled wisdom carried across cycles. The cycle-journal is the chronological record; this file is the curated essence — patterns I've internalized, calibration notes, things to keep in mind.*

*Keep under 15,000 characters. Above 18,000 = condense.*

---

## Status snapshot — updated 2026-05-14 (Iteration 31)

Thirty-one cycles run. **System: ✅ Nominal.** D3.5 5c ("Beacon auto-replan on Mirror ESCALATE") shipped — PR #7 merged (HEAD=463c6d8). Iter 30 escalation (empty branch, Mirror REVIEW_ESCALATE) resolved. 2 new PRs open: #8 (forge/opmanual-d35-5c-shipped-section-001, Forge doc for D3.5 5c, ~55 min old) and #9 (larry/d35-5c-followup-discipline-prefix, followup discipline fix, ~36 min old). Both within normal Mirror review window. Core 6 units active. 4 decommissioned services (orchestrator, telegram-webhook, github-webhook, merge-watcher.timer) still inactive — 9 consecutive (iters 23–31); iter 23b escalation (needs_response=true) open awaiting Larry's intentional-decommission confirmation. pulse/.invalid/ unchanged at 3 files. forge/.invalid/ unchanged at 1 file. F24 G-rule: no new occurrences since D3.5; monitoring.

## Known calibration issues

- **All-bot log-silence false positive (confirmed iter 2, generalized from iter 1 beacon-only).** Check C threshold (>30m log silence → ask-then-do) fires on idle Telegram polling periods for ALL bots (beacon, forge, mirror, pulse). None of the bots log anything when no user messages arrive. Observed silence times: beacon 77m, forge 47m, mirror 45m, pulse 31m — all units were systemctl active. Do not escalate for log silence unless the systemd unit is also non-active or there's error-spam in the last visible log lines. Confirmed again iter 3 (silence 4h30m–5h18m, all 4 units active, no errors).

- **D3.5 infrastructure decommission (observed iter 23, 2026-05-13; G-rule threshold iter 25).** Four services now inactive as expected D3.5 transition: ourliberty-orchestrator, ourliberty-telegram-webhook, ourliberty-github-webhook, ourliberty-merge-watcher.timer. The watchdog.py adapter rewrite removed them from monitoring at 22:56 MDT May 12. Do not escalate these as "down." G-rule threshold (3 consecutive: iters 23–24–25) reached iter 25 — holding Forge dispatch since this is a confirmation question for Larry, not a code bug. iter 23b (needs_response=true) still open; once Larry confirms intentional decommission, close it and update this entry.

- **Watchdog dispatch to Pulse inbox missing task_id (discovered iter 23, 2026-05-13).** watchdog.py generates dispatch payloads without a task_id field. Validator rejects them. Critical watchdog alerts to Pulse are silently dropped. watchdog.py rewritten in D3.5 5a (commit d908ca6); verify fix includes task_id in all dispatch payloads. See pulse/.invalid/watchdog-alert-1778648185.json.reason. Verify post-D3.5 — no recurrence since iter 23.

- **forge/.invalid/ "worktree: no canonical path for target_repo=None" (observed iter 25, 2026-05-13).** A depth-2 beacon→forge clarification-response notification (`notify-notify-pulse-cost-note-002.json`, source=beacon-clarification) was rejected with "worktree: no canonical path for target_repo=None" at 02:52Z May 13. New validation error class — distinct from F24 (prompt too short). target_repo=None suggests the dispatch had no repo context for the worktree setup step. Underlying task (PR #2, operating-manual cost update) completed via another path. 1 occurrence; monitor. If recurs, route to Forge to investigate worktree path resolution for depth-2 notifies.

## System-state assumptions that have proven wrong

- **2026-05-09 — Unattended run_cycle.sh cannot write journal.** The `claude --print --output-format json` invocation in run_cycle.sh is non-interactive. Write/Edit tool calls require interactive user approval. Until agents/pulse/.claude/settings.json has an allowlist for the cycle-specific write paths (cycle-journal.md, cycle-actions.jsonl, pulse-escalations.json, MEMORY.md), every unattended cycle will run checks and exit 0 but leave no journal trace. **Fix needed:** Forge task to add the allowlist. (See pulse-escalations.json iter=1.)

- **2026-05-09 — Interactive Pulse cycles leave dirty tree.** Interactive cycles write to cycle-journal.md and MEMORY.md as part of normal operation but have no commit step. After every interactive cycle, the repo ends up with uncommitted changes that block sync_agent_core.sh. Observed in iters 2→3→4 (same cause). **Fix needed:** Pulse must commit its own operational writes (journal, MEMORY.md, cycle-actions.jsonl) as the final step of each cycle. Proposal written to agents/pulse/memory/commit-pulse-operational-writes-proposal.md. Needs Larry relay to Forge. (See pulse-escalations.json iters 3 and 4.)

- **2026-05-09 — pulse-proposals/ and forge inbox writes blocked by session scope.** The session's allowed working directory is ~/agent-core/agents/pulse/. Writes to ~/agents/blackboard/pulse-proposals/ and ~/agents/inboxes/forge/ are blocked. Workaround: write proposals to agents/pulse/memory/ and flag Larry to relay. This is a structural constraint until Forge is fully wired (Phase C) or the session's settings.json is updated to allow those paths.

- **2026-05-10 — Bash read-only commands (git status, git branch, systemctl) also require manual approval in interactive sessions.** git status, git branch --show-current, systemctl is-active, gh pr list, and tmux ls all require user approval each invocation. This blocks Check A, C, and E from completing reliably without pre-approved permissions in settings.json. Same settings.json fix that resolves the write-permissions issue (iter 1) should also allow these read-only bash commands. Add to Forge dispatch.

- **2026-05-10 (iter 9) — Telegram approval gap mirrors interactive approval gap.** Larry attempted a manual commit fix via Telegram at 12:13 MDT. Pulse tried, was blocked by the same approval gap that blocks interactive git-commit attempts. Neither interactive chat nor Telegram can drive a `git commit` through without Larry running it himself in a terminal, OR without Forge implementing the settings.json allowlist fix. The escalation mechanism (pulse-escalations.json) is also insufficient on its own — Larry reads it but cannot action it from within the Claude Code session. Resolution paths: Larry's terminal (git commit), Forge task (settings.json + cycle end-commit), or accepted drift.

- **2026-05-10 — Stuck automated cycle failure mode discovered.** Automated cycle (PID 10653) started 02:31 MDT with no completion log after hours. Process alive but dormant (3.5MB RSS, 16 ctx switches). Lock file was not released. This means: (a) the cycle may hang silently without any visible error, (b) subsequent timer cycles will see a live lock and abort silently (if < 30 min) or overwrite (if > 30 min). No repeat yet; flag if it recurs.

## Recurring patterns I've promoted to permanent fixes

- **2026-05-11 — Dirty tree (Pulse operational writes). CLOSED iter 16.** Pattern: 13 consecutive cycles (iters 3–15) left cycle-journal.md and MEMORY.md uncommitted, blocking sync. Permanent fix: `6b6284a` ("Phase D2: shared inbox watcher + cost capture + cycle auto-commit") added auto-commit step to `run_cycle.sh` — commits Pulse-owned files after each successful cycle. Landed ~2 days after G-rule first fired (iter 4). Implemented by Larry (committed 2026-05-11 15:52 MDT). **Confirmed resolved** — iter 16 sync.json shows success, commit e2e5f79, first clean A+B checks since iter 2.

## Recurring patterns I've decided NOT to promote (and why)

*(empty — sometimes the systemic fix is worse than the manual intervention. Document those calls so I don't relitigate.)*

## Auto-fix allow-list expansions

*(empty — when an "ask-then-do" check has been "Larry says yes" for 10+ consecutive iterations, I propose moving it to "always-allowed". Track those decisions here.)*

## Escalations Larry overrode (calibration data)

*(empty — when I escalated and Larry said "no action needed" or "you should have just fixed that," recalibrate. Keeps me from over-paging or under-acting.)*

## System-state assumptions that have proven wrong

- **2026-05-14 (iter 30) — GitHub reviewDecision="" does not mean Mirror is still reviewing.** When Mirror issues REVIEW_ESCALATE rather than a formal GitHub approve/request-changes, `reviewDecision` stays "" on GitHub. Pulse iter 29 saw reviewDecision="" and assumed Mirror was still in progress — Mirror had finished 5h earlier and escalated. **Fix:** Add sub-check to Check E: when a PR has reviewDecision="" and has been open > a short window, also scan mirror outbox for a completed review result. Proposal at agents/pulse/memory/check-gap-mirror-outbox-escalate.md.

---

**Format reminder:** Each entry has a date, a one-line claim, and (where the claim is non-obvious) a "Why" line explaining the reasoning. Date stamps let me judge whether a memory is still current.
