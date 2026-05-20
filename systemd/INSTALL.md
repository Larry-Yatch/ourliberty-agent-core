# Installing Ourliberty systemd units

These units replace the tmux-based bot launchers with proper systemd-managed services. Benefits:
- Auto-restart on crash
- Auto-start on droplet boot
- Centralized logs via `journalctl`
- Resource limits (memory, tasks)
- Filesystem hardening (`ProtectHome`, `ReadWritePaths`)

## Prerequisites

- All bots' tokens populated in `/home/larry/credentials/.env.larry`
- Anthropic API key set in `.env.larry` (for Pulse `/cycle`)
- `larry` user can `sudo` without password (already configured)

## Install all units

Run on the droplet as `larry`:

```bash
# Copy units into systemd's directory
sudo cp ~/agent-core/systemd/ourliberty-*.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-*.timer /etc/systemd/system/

# Reload systemd to see the new units
sudo systemctl daemon-reload

# (Recommended) verify each unit parses cleanly
for unit in ~/agent-core/systemd/ourliberty-*; do
  systemd-analyze verify "$unit" || echo "VERIFY FAILED: $unit"
done
```

## Enable and start the units

### Beacon (replaces the tmux session)

If you're currently running Beacon via tmux, stop it first to avoid two instances polling the same Telegram bot:

```bash
tmux kill-session -t beacon-bot 2>/dev/null
sudo systemctl enable --now ourliberty-beacon-bot.service
sudo systemctl status ourliberty-beacon-bot.service
journalctl -u ourliberty-beacon-bot.service -f   # tail the log
```

### Forge / Mirror / Pulse (Phase C / D activation)

After you've created the bot via BotFather and installed the token in `.env.larry`:

```bash
sudo systemctl enable --now ourliberty-forge-bot.service
sudo systemctl enable --now ourliberty-mirror-bot.service
sudo systemctl enable --now ourliberty-pulse-bot.service
```

### Inbox watcher (Phase D2)

The shared inbox watcher polls `~/agents/inboxes/{beacon,forge,mirror,pulse}/`
every 5s, validates each task, runs `claude --print` per-agent, and writes the
result to `~/agents/outboxes/<agent>/`. One process, four agent threads, max
one in-flight task per agent (lease primitive).

```bash
sudo systemctl enable --now ourliberty-inbox-watcher.service
journalctl -u ourliberty-inbox-watcher.service -f   # tail the log
```

To smoke-test end-to-end, drop a HANDSHAKE-conformant JSON into one of the
inbox dirs (see `runbooks/cycle-prompt.md` §8 for the format) and watch:
- `journalctl -u ourliberty-inbox-watcher.service -f` for the pickup line
- `~/agents/outboxes/<agent>/` for the result file
- `~/agents/blackboard/costs.jsonl` for the cost record
- `~/agents/inboxes/<agent>/.archive/` to confirm the task was consumed

### Periodic services (timers)

These are **timers**, not the underlying services. Enabling the timer is what schedules the work:

```bash
# /cycle runs every 30 min (Phase D)
sudo systemctl enable --now ourliberty-cycle.timer

# Sync runs every 1 hour
sudo systemctl enable --now ourliberty-sync.timer

# Health check runs every 30 min
sudo systemctl enable --now ourliberty-agent-core-health.timer

# Watchdog runs every 5 min (Phase D activation; depends on watchdog.py being wired)
sudo systemctl enable --now ourliberty-watchdog.timer
```

### Self-healing healers (Phase D2.5 + E1.3 + E1.5.2 + E2.1 + E2.2)

Twelve scripts under `scripts/heal_*.py`, `scripts/sync_*.py`, and `scripts/deploy_notifier.py` watch for specific failure modes the audit identified. Each runs on its own systemd timer (2 min–12 h cadence) and is one-shot — fires, reports, exits. Enabling these closes audit Gap 8 and the credential-discipline + install-discipline + deploy-targets + deploy-notifier gaps surfaced in E1.5 / E2.1 / E2.2.

```bash
# Install (copy unit files into systemd's directory)
sudo cp ~/agent-core/systemd/ourliberty-heal-*.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-heal-*.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-sync-deploy-targets.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-sync-deploy-targets.timer /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-deploy-notifier.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-deploy-notifier.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start all 12 timers at once
sudo systemctl enable --now ourliberty-heal-abandoned-inbox-tasks.timer
sudo systemctl enable --now ourliberty-heal-blocked-inbox-age.timer
sudo systemctl enable --now ourliberty-heal-empty-inbox-files.timer
sudo systemctl enable --now ourliberty-heal-recovery-already-merged.timer
sudo systemctl enable --now ourliberty-heal-restart-dedup-obsolete.timer
sudo systemctl enable --now ourliberty-heal-silent-loop-death.timer
sudo systemctl enable --now ourliberty-heal-zombie-main-workers.timer
sudo systemctl enable --now ourliberty-heal-pr-auto-merge.timer  # E1.3 — DRY-RUN by default; see service file for activation
sudo systemctl enable --now ourliberty-heal-credential-registry-drift.timer  # E1.5.2 — DRY-RUN by default
sudo systemctl enable --now ourliberty-heal-systemd-install-drift.timer  # E1.5.2 — DRY-RUN by default
sudo systemctl enable --now ourliberty-sync-deploy-targets.timer  # E2.1 — DRY-RUN by default
sudo systemctl enable --now ourliberty-deploy-notifier.timer  # E2.2 — DRY-RUN by default

# Confirm
systemctl list-timers 'ourliberty-heal-*' 'ourliberty-sync-*' 'ourliberty-deploy-*' --all
```

What each one does:

| Healer | Cadence | What it watches for |
|---|---|---|
| `abandoned-inbox-tasks` | 10 min | Tasks stuck in an inbox because the worker exited silently |
| `blocked-inbox-age` | 15 min | Stale tasks in `inboxes/*/blocked/` past their TTL |
| `empty-inbox-files` | 15 min | Empty / trivially-malformed JSON files dropped into agent inboxes |
| `recovery-already-merged` | 5 min | Recovery tasks pointing at PRs that have since been merged |
| `restart-dedup-obsolete` | 5 min | Stale `RESTART_DEDUP` duplicate markers |
| `silent-loop-death` | 10 min | Self-scheduled re-queue loops that died without leaving a trace |
| `zombie-main-workers` | 5 min | `claude` agent processes still running in deleted worktree paths |
| `pr-auto-merge` (E1.3) | 5 min | Mirror-PASSed PRs whose auto-merge primary path missed |
| `credential-registry-drift` (E1.5.2) | 6 h | Credentials in store without registry entries; registry entries without credentials in store |
| `systemd-install-drift` (E1.5.2) | 12 h | systemd units shipped in repo but never installed under `/etc/systemd/system/` |
| `sync-deploy-targets` (E2.1) | 12 h | `config/deploy_targets.json` ↔ Vercel API drift (project missing on either side, name mismatch) |
| `deploy-notifier` (E2.2) | 2 min | Vercel preview-URL READY + build-ERROR events for configured deploy targets |

Each healer's logs land in `journalctl -u ourliberty-heal-<name>.service`. They `Nice=10` so they never starve real work.

#### Install-audit pattern (E1.5.2)

The `systemd-install-drift` healer above is itself an audit primitive: it catches every PR that ships a new `systemd/*.service` or `*.timer` but doesn't get installed on the droplet. Motivating example: PR #43 shipped `heal-pr-auto-merge.{service,timer}` to the repo, but they were never copied to `/etc/systemd/system/` — the gap stayed silent until E1.5 review caught it.

The pattern: any PR that adds a unit file is operator-completed when:

1. The file lands in `systemd/` in the repo (PR merge).
2. `sudo cp ~/agent-core/systemd/<unit> /etc/systemd/system/` + `sudo systemctl daemon-reload` + (for timers) `sudo systemctl enable --now <unit>` on the droplet.
3. The drift healer's next tick (within 12 h) finds no drift — confirms the install landed.

If step 2 is missed, the healer DMs Larry with the exact install commands; the gap closes within one tick.

#### Credential-discipline pattern (E1.5.2)

The `credential-registry-drift` healer enforces the 4-artifact rule from `shared/credentials-discipline.md` at runtime: every credential in `.env.larry` / `~/.config/gh/hosts.yml` / `~/.claude/.credentials.json` / `~/.google_workspace_mcp/credentials/` must have a matching entry in `config/token-rotation-schedule.json`, and vice versa. DMs every 6 h until reconciled (fail-closed per Larry's Q2 design decision). Activation env var: `OURLIBERTY_CREDENTIALS_HEALER_ENABLED=true` per the service file's commented activation snippet.

#### Deploy-targets drift pattern (E2.1)

The `sync-deploy-targets` script reconciles `config/deploy_targets.json` against the actual project list returned by the Vercel API (`GET /v9/projects`, personal Hobby account — no `teamId`). Three drift kinds: `MISSING_FROM_REGISTRY` (project exists on Vercel without a registry entry), `MISSING_FROM_VERCEL` (registry entry whose `vercel_project_id` returns 404), `NAME_MISMATCH` (both sides have the project but the human-readable names diverge). DMs every 24 h per persistent drift item (2 ticks at the 12 h timer cadence). Activation env var: `OURLIBERTY_DEPLOY_TARGETS_SYNC_ENABLED=true` per the service file's commented activation snippet. Vercel auth failures (401/403) emit a `critical`-severity `INFRASTRUCTURE_ALERT` and the unit exits non-zero so systemd surfaces it.

#### Deploy-notifier pattern (E2.2)

The `deploy-notifier` script polls Vercel's `GET /v6/deployments?state=READY,ERROR` every 2 min, filters by the GitHub repos in `config/deploy_targets.json`, and DMs Larry via the shared `larry_alerts` queue. READY → `warning`-severity DM with the preview URL. ERROR → `critical`-severity DM with the inspect link. BUILDING / QUEUED / INITIALIZING / CANCELED are skipped silently. Per-target `branch_filter` (null = match all branches; glob like `forge/*` for feature-branch-only) gates which deployments surface. PR number comes from `deployment.meta.githubPrId` first; falls back to `gh pr list --head <branch> --repo <repo>`; renders `PR #(unknown)` if both miss. Dedup is keyed by `<uid>:<state>` so a deployment that transitions READY → ERROR re-DMs; the same uid+state pair is never re-DMed. State file at `~/agents/state/deploy-notifier.json` capped at the 1000 most-recent entries (FIFO prune). Activation env var: `OURLIBERTY_DEPLOY_NOTIFIER_ENABLED=true` per the service file's commented activation snippet — default dry-run logs `would-DM` lines and fires a one-time activation prompt on first real event. Vercel auth failures (401/403) emit a `critical` `INFRASTRUCTURE_ALERT` throttled to one DM per 24 h; the unit exits non-zero so systemd surfaces transient errors via its retry path. Empty `deploy_targets` array → no API call, no DM, clean exit (`E2.3` lands the first real target).

## Checking state

```bash
# What's running right now?
systemctl list-units 'ourliberty-*' --type=service

# What timers are scheduled?
systemctl list-timers 'ourliberty-*'

# Status of one unit
systemctl status ourliberty-beacon-bot.service

# Tail logs of one unit
journalctl -u ourliberty-beacon-bot.service -f

# Last 100 lines of all ourliberty logs combined
journalctl --since "1 hour ago" SYSLOG_IDENTIFIER=ourliberty-beacon SYSLOG_IDENTIFIER=ourliberty-cycle SYSLOG_IDENTIFIER=ourliberty-sync
```

## Stopping / disabling

```bash
# Stop a service (keeps it enabled — auto-starts on boot)
sudo systemctl stop ourliberty-beacon-bot.service

# Disable (won't auto-start on boot)
sudo systemctl disable ourliberty-beacon-bot.service

# Both
sudo systemctl disable --now ourliberty-beacon-bot.service
```

## Rolling back to tmux

If a systemd unit is misbehaving and you need to fall back to tmux while debugging:

```bash
sudo systemctl disable --now ourliberty-beacon-bot.service
bash ~/agent-core/scripts/beacon_telegram_bot.sh   # tmux launcher
```

## Updating after a code pull

After `git pull` in `~/agent-core/`, restart the affected services:

```bash
# If Python bot code changed
sudo systemctl restart ourliberty-beacon-bot.service
sudo systemctl restart ourliberty-forge-bot.service
# etc.

# If a unit file itself changed
sudo cp ~/agent-core/systemd/ourliberty-beacon-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ourliberty-beacon-bot.service

# Agent prompt files (.md) — no restart needed; next bot invocation picks them up
```

## Hardening notes

Each service runs with:
- `User=larry`, `Group=larry` — never root
- `ProtectHome=read-only` with explicit `ReadWritePaths` for the directories the bot legitimately needs
- `ProtectSystem=strict` — system dirs read-only
- `NoNewPrivileges=true` — can't escalate via setuid
- `PrivateTmp=true` — own /tmp, isolated from other services
- `MemoryMax=2G`, `TasksMax=64` — prevents runaway resource use

If a bot needs a path it doesn't currently have, edit the `ReadWritePaths=` line in the appropriate service file rather than relaxing other hardening.

## Phase activation checklist

| Service | When to enable | Larry-actions required first |
|---|---|---|
| `ourliberty-beacon-bot.service` | Now (replaces tmux) | Already done in Phase B |
| `ourliberty-sync.timer` | Now (low risk) | None — just enable |
| `ourliberty-agent-core-health.timer` | Now | None |
| `ourliberty-forge-bot.service` | Phase C activation | Create Forge bot via BotFather; install token |
| `ourliberty-mirror-bot.service` | Phase C activation | Create Mirror bot; install token |
| `ourliberty-pulse-bot.service` | Phase D activation | Create Pulse bot; install token |
| `ourliberty-cycle.timer` | Phase D activation | Anthropic API key in .env; first dry-run with Larry watching |
| `ourliberty-inbox-watcher.service` | Phase D2 activation | None — just enable; relies on existing `scripts/dispatch_lease.py` + `dispatch_validator.py` |
| `ourliberty-watchdog.timer` | Phase D activation | After cycle has been observed for ≥ 1 day |
| `ourliberty-ledger.timer` | After build-ledger-001 lands | None — `scripts/ledger_weekly.py` is pure-Python, no extra credentials. First Monday after enable triggers the inaugural run. |

### Ledger (weekly cost report)

After this PR lands:

```bash
sudo cp ~/agent-core/systemd/ourliberty-ledger.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-ledger.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-ledger.timer
systemctl list-timers ourliberty-ledger.timer

# Manual smoke (writes a real report for the current Monday):
sudo systemctl start ourliberty-ledger.service
journalctl -u ourliberty-ledger.service -n 50
ls -la ~/agents/blackboard/ledger/
```

See `docs/operating-manual.md` §10.1 for full ops detail (recovery from missed run, manual `--week-ending` invocation, sentinel contract with Pulse Check I).
