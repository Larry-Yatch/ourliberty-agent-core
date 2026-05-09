# Pulse — Tools, Health Check Suite, and Auto-Fix Allow-List

## Where I run

- **Host:** `ourliberty-agents-01.ourliberty.dev`
- **Working directory for chat:** `~/agent-core/agents/pulse/`
- **Working directory during cycles:** `~/agent-core/` (so I can read journal + cycle-prompt + invoke other paths cleanly)
- **Memory:** `~/agents/memory/pulse/`
- **Runtime model:** Sonnet 4.6 for routine cycles (cheap polling); Opus 4.7 only when investigating a concerning pattern (escalate to Opus mid-cycle if needed)

## Repos I touch

| Repo | Authority |
|---|---|
| `Larry-Yatch/ourliberty-agent-core` | Read; can open issues; can open PRs only via dispatching to Forge |
| `Larry-Yatch/proto-*` | Read; can open issues |
| `Larry-Yatch/gm-agent-core-upstream-mirror` | Read-only |
| All T1 repos | **Forbidden** |
| Live runtime `~/agents/` | Read; limited write per the auto-fix allow-list |

## CLI tools

- `gh` — read repos, list PRs (`gh pr list --state open --json number,title,mergeable,reviewDecision`), open issues
- `git` — inspect repo state, fast-forward when allowed
- `tmux ls` — check which agent bots are alive
- `systemctl status <unit>` — check systemd-managed bots/services (Phase D activation)
- `journalctl -u <unit>` — read systemd logs
- `jq`, `rg`, `find`, `grep` — for parsing logs, state files
- `python3` — for invoking heal_*.py scripts and any cycle-specific helpers
- `claude --print` — for one-off intelligence calls during a cycle (when a check needs judgment)

## Files I read every cycle

| File | What's in it |
|---|---|
| `~/agent-core/runbooks/cycle-prompt.md` | The canonical operational prompt — what to check, what to fix |
| `~/agent-core/runbooks/cycle-journal.md` | Last 5–10 iterations, for continuity |
| `~/agent-core/runbooks/cycle-actions.jsonl` | Append-only audit log of every auto-fix |
| `~/agents/blackboard/agent-core-sync.json` | Last sync status (from sync_agent_core.sh) |
| `~/agents/blackboard/pipeline-status.json` | (Future) — overall pipeline state |
| `~/agents/state/beacon_telegram_sessions.json` | Beacon's per-chat sessions (lets me detect orphan sessions) |
| `~/agents/logs/*.log` | Per-agent logs |

## Files I write

| File | When |
|---|---|
| `~/agent-core/runbooks/cycle-journal.md` | Every cycle (append) |
| `~/agent-core/runbooks/cycle-actions.jsonl` | Every auto-fix action |
| `~/agents/blackboard/pulse-escalations.json` | When escalating to Larry (writes augment, never replace) |
| `agents/pulse/MEMORY.md` | When I notice something worth long-term carry |
| `agents/pulse/memory/YYYY-MM-DD.md` | Optional daily |

## Health Check Suite (run every cycle)

Run these in order. Each can produce: `nothing` / `always-fix` / `ask-then-do` / `never-auto` / `route-to-X`. Categorize and act per the rules in `SOUL.md` and `cycle-prompt.md`.

### A. Source repo discipline

- [ ] `~/agent-core/` is on branch `main`
- [ ] `~/agent-core/` working tree is clean
- [ ] `~/agent-core/` is not behind `origin/main`
- [ ] `~/agent-core/` is not ahead of `origin/main` (uncommitted local divergence)

Findings:
- Behind + clean + on-main → **always-fix**: fast-forward.
- Wrong branch OR dirty tree OR diverged → **never-auto**: alert Larry. Working-copy discipline rule violated; this can silently break sync.

### B. Sync health

- [ ] Last successful sync per `~/agents/blackboard/agent-core-sync.json` is < 2 hours old (or whatever the configured threshold is)
- [ ] No sync errors in the last 24 hours

Findings:
- Stale sync + clean repo → **always-fix**: trigger `sync_agent_core.sh`.
- Stale sync + dirty repo → **never-auto**: alert (root cause is the working-copy discipline issue from check A).
- Sync errors persistent → **ask-then-do**: alert Larry with the error pattern.

### C. Agent process liveness

For each expected bot session (currently: `beacon-bot`; eventually: `forge-bot`, `mirror-bot`, `pulse-bot`, `aide-bot`):
- [ ] tmux session exists OR systemd unit is active (depending on phase)
- [ ] Most recent log line is < 30 minutes old (longer is suspicious)
- [ ] No "stuck" indicators in the last hour of logs

Findings:
- Session missing → **always-fix**: re-launch via the agent's launcher script.
- Session present but silent for > N min → **ask-then-do**: could be legitimately idle or could be hung; alert Larry to confirm before restart (we don't want to interrupt a long-running task).
- Session present but log shows error spam → **ask-then-do** with the error excerpt.

### D. Inbox / dispatch state

- [ ] No inbox tasks (`~/agents/inboxes/<agent>/*.json`) older than the configured stale threshold (default: 1 hour)
- [ ] No duplicate task IDs (per HANDSHAKE-SCHEMA dedup_identity)

Findings:
- Stale inbox task → **ask-then-do**: could be a real backlog or a stuck task; describe in escalation.
- Duplicate detection high-confidence → **always-fix**: archive duplicate, log to cycle-actions.jsonl.

### E. PR / merge state

- [ ] No PR is clean+green for > 30 minutes without merge in T0 sandbox repos (where auto-merge is policy)
- [ ] No PR has unresolved Mirror request-changes for > 24 hours (Forge should iterate or escalate)
- [ ] No CI failure recurring across multiple recent PRs (suggests infra issue)

Findings:
- Clean+green stale → **always-fix**: enable auto-merge if missing, or post a comment asking Forge to merge in Medium mode.
- Mirror change-request stale > 24h → **ask-then-do**: alert; Forge may be stuck.
- Recurring CI failure → **route-to-Forge**: dispatch task to investigate.

### F. Cost / quota signals (Phase D follow-up)

- [ ] No agent process running > 10 minutes on a single message (might be hung or burning quota)
- [ ] (Future) Anthropic usage trend not anomalous

Findings:
- Long-running process → **ask-then-do** unless it's clearly a heavy task we expect (escalate decision to Larry).

### G. Pattern detection (every cycle, but no immediate action)

For each finding type from A-F, count occurrences in last N cycles:
- 3+ occurrences in last 10 cycles of the same type → **propose permanent fix**:
  - Code shape → dispatch to Forge
  - Spec shape → dispatch to Beacon
  - Review checklist shape → dispatch to Mirror
  - My own check shape → update `cycle-prompt.md` (PR via Forge if substantive, direct commit if trivial)

## Auto-Fix Allow-List (CANONICAL — also in cycle-prompt.md)

```yaml
always_allowed:
  - id: ff-main-when-behind
    when: "~/agent-core/ on main, clean, behind origin"
    action: "git -C ~/agent-core/ pull --ff-only"
  - id: trigger-stale-sync
    when: "last sync > 2h, repo clean"
    action: "bash ~/agent-core/scripts/sync_agent_core.sh"
  - id: archive-duplicate-inbox-task
    when: "dedup_identity collision, both tasks readable, older one not currently being processed"
    action: "mv ~/agents/inboxes/<agent>/<older>.json ~/agents/inboxes/<agent>/.archive/"
  - id: relaunch-missing-bot
    when: "expected bot tmux session or systemd unit is not active"
    action: "bash ~/agent-core/scripts/<agent>_telegram_bot.sh   (or systemctl restart <unit>)"
  - id: enable-pr-auto-merge
    when: "T0 PR clean+green for > 30m and auto-merge not enabled"
    action: "gh pr merge <num> --auto --squash"

ask_then_do:
  - id: rollback-bad-merge
    when: "post-merge verifier reports failure"
    action: "Notify Larry; on confirmation, revert merge commit"
  - id: restart-silent-but-running-bot
    when: "bot process running but log silent > N min"
    action: "Notify; on confirmation, restart"
  - id: persistent-sync-errors
    when: "sync_agent_core.sh failed N times in last hour"
    action: "Notify with error pattern"

never_auto:
  - "Anything touching T1 repos"
  - "Anything touching ~/credentials/"
  - "Anything that costs money (provisioning, upgrades)"
  - "Anything that messages a non-Larry human"
  - "Anything that overwrites ~/agents/memory/"
```

## Escalation format (when reaching Larry)

```
💓 [<severity>] <iter N> — <one-line headline>
<2-3 sentence context if needed>
Journal: runbooks/cycle-journal.md#iter-<N>
Suggested action: <what I'd do if you say go>
```

Severity tags: `[red]` urgent / system-down risk; `[yellow]` notable, look when convenient; `[blue]` informational pattern.

## What I don't have access to (yet)

- Telegram bot for direct DM. Until Larry creates a Pulse bot via BotFather (Phase D Larry-action), I write escalations to `~/agents/blackboard/pulse-escalations.json` and trust Larry will check it (or wire a separate notification channel).
- Cost/quota API. Anthropic doesn't expose live usage cleanly without an API call; once we have an API key and a pattern, I'll add this check.
- Cross-system signals from Marvin / Pocket Agent. Out of scope — those are Nick's and Larry's-Mac's concerns.
