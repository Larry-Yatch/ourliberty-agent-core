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

### Self-healing healers (Phase D2.5)

Seven healer scripts under `scripts/heal_*.py` watch for specific failure modes the audit identified. Each runs on its own systemd timer (5–15 min cadence) and is one-shot — fires, reports, exits. Enabling these closes audit Gap 8.

```bash
# Install (copy unit files into systemd's directory)
sudo cp ~/agent-core/systemd/ourliberty-heal-*.service /etc/systemd/system/
sudo cp ~/agent-core/systemd/ourliberty-heal-*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start all 8 timers at once
sudo systemctl enable --now ourliberty-heal-abandoned-inbox-tasks.timer
sudo systemctl enable --now ourliberty-heal-blocked-inbox-age.timer
sudo systemctl enable --now ourliberty-heal-empty-inbox-files.timer
sudo systemctl enable --now ourliberty-heal-recovery-already-merged.timer
sudo systemctl enable --now ourliberty-heal-restart-dedup-obsolete.timer
sudo systemctl enable --now ourliberty-heal-silent-loop-death.timer
sudo systemctl enable --now ourliberty-heal-zombie-main-workers.timer
sudo systemctl enable --now ourliberty-heal-pr-auto-merge.timer  # E1.3 — runs in DRY-RUN mode by default; see service file for activation

# Confirm
systemctl list-timers 'ourliberty-heal-*' --all
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

Each healer's logs land in `journalctl -u ourliberty-heal-<name>.service`. They `Nice=10` so they never starve real work.

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
