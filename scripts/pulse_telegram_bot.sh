#!/usr/bin/env bash
# Launch Pulse's Telegram bot in a tmux session. Survives SSH disconnects.
#
# Provides a no-sudo recovery path for pulse-bot when systemctl is unavailable
# (e.g. StartLimitBurst exhausted, polkit refusals from agent sessions).
# Mirrors scripts/beacon_telegram_bot.sh — same shape, different agent.
#
# Usage:   bash ~/agent-core/scripts/pulse_telegram_bot.sh
# Attach:  tmux attach -t pulse-bot
# Detach:  Ctrl-b d
# Stop:    tmux kill-session -t pulse-bot

set -e

SESSION="pulse-bot"
ENV_FILE="${HOME}/credentials/.env.larry"
BOT_PY="${HOME}/agent-core/scripts/agent_telegram_bot.py"

if ! [ -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found." >&2
  exit 1
fi
if ! [ -f "$BOT_PY" ]; then
  echo "ERROR: $BOT_PY not found." >&2
  exit 1
fi

# Check for required env vars (without echoing values)
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

if [ -z "$TELEGRAM_BOT_TOKEN_PULSE" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN_PULSE is empty in $ENV_FILE" >&2
  exit 1
fi
if [ -z "$TELEGRAM_ALLOWED_CHAT_IDS" ]; then
  echo "ERROR: TELEGRAM_ALLOWED_CHAT_IDS is empty in $ENV_FILE" >&2
  echo "       Add at least your own chat ID (from @userinfobot)." >&2
  exit 1
fi

# Best-effort: stop the systemd unit if it's hanging on or in failed state.
# Redirect stdin from /dev/null so polkit's interactive password prompt
# fails immediately when sudo isn't available — without </dev/null, polkit
# blocks on stdin waiting for a password the agent session can't provide.
# || true keeps the script alive when the stop fails (expected in agent
# sessions). Prevents double-polling if systemd later recovers the unit
# while tmux is running.
systemctl stop ourliberty-pulse-bot </dev/null 2>/dev/null || true
systemctl reset-failed ourliberty-pulse-bot </dev/null 2>/dev/null || true

# Kill any existing tmux session
tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"

# Start fresh — tmux loads env from the parent shell and exports AGENT=pulse
# so the generic agent_telegram_bot.py reads the pulse-specific config.
tmux new-session -d -s "$SESSION" \
  "set -a; . '$ENV_FILE'; set +a; export AGENT=pulse; cd '${HOME}/agent-core/agents/pulse' && exec python3 '$BOT_PY' 2>&1 | tee -a ${HOME}/agents/logs/pulse_telegram_bot.tmux.log"

sleep 1
tmux ls | grep "$SESSION" || { echo "Failed to start tmux session"; exit 1; }
echo "Bot running in tmux session '$SESSION'."
echo "View live:  tmux attach -t $SESSION   (Ctrl-b d to detach)"
echo "Tail log:   tail -f ~/agents/logs/pulse_telegram_bot.log"
echo "Stop:       tmux kill-session -t $SESSION"
