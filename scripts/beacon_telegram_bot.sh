#!/usr/bin/env bash
# Launch Beacon's Telegram bot in a tmux session. Survives SSH disconnects.
#
# Usage:   bash ~/agent-core/scripts/beacon_telegram_bot.sh
# Attach:  tmux attach -t beacon-bot
# Detach:  Ctrl-b d
# Stop:    tmux kill-session -t beacon-bot

set -e

SESSION="beacon-bot"
ENV_FILE="${HOME}/credentials/.env.larry"
BOT_PY="${HOME}/agent-core/scripts/beacon_telegram_bot.py"

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

if [ -z "$TELEGRAM_BOT_TOKEN_BEACON" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN_BEACON is empty in $ENV_FILE" >&2
  exit 1
fi
if [ -z "$TELEGRAM_ALLOWED_CHAT_IDS" ]; then
  echo "ERROR: TELEGRAM_ALLOWED_CHAT_IDS is empty in $ENV_FILE" >&2
  echo "       Add at least your own chat ID (from @userinfobot)." >&2
  exit 1
fi

# Kill any existing session
tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"

# Resolved ONCE and reused by both the tee target and the operator hint
# below. They used to be spelled independently, so when the tee moved under
# the override the printed "Tail log:" line kept naming ~/agents/logs — under
# a swapped HOME it sent the operator to a path this script does not write.
# beacon_telegram_bot.py's own resolve_log_dir() honours the same override,
# so BOTH files move together and a hardcoded hint is wrong for both.
LOG_DIR="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/logs"

# Start fresh — tmux loads the env from the parent shell
tmux new-session -d -s "$SESSION" \
  "set -a; . '$ENV_FILE'; set +a; exec python3 '$BOT_PY' 2>&1 | tee -a ${LOG_DIR}/beacon_telegram_bot.tmux.log"

sleep 1
tmux ls | grep "$SESSION" || { echo "Failed to start tmux session"; exit 1; }
echo "Bot running in tmux session '$SESSION'."
echo "View live:  tmux attach -t $SESSION   (Ctrl-b d to detach)"
echo "Tail log:   tail -f ${LOG_DIR}/beacon_telegram_bot.log"
echo "Stop:       tmux kill-session -t $SESSION"
