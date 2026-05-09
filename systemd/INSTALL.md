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
| `ourliberty-watchdog.timer` | Phase D activation | After cycle has been observed for ≥ 1 day |
