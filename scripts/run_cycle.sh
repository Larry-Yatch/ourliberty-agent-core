#!/usr/bin/env bash
# run_cycle.sh — invoke /cycle (Pulse self-healing iteration).
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
#
# Wraps Claude Code with concurrency-lock + per-iteration logging.
# Invoked by ourliberty-cycle.service (systemd) or manually:
#   bash ~/agent-core/scripts/run_cycle.sh

set -e

PULSE_DIR="${HOME}/agent-core/agents/pulse"
LOCK_DIR="${HOME}/agents/state"
LOCK_FILE="${LOCK_DIR}/.cycle.lock"
LOCK_MAX_AGE_SEC=$((30 * 60))   # 30 minutes — stale lock threshold
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/cycle.log"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_cycle: $*" | tee -a "$LOG_FILE"
}

# --- concurrency guard ---
if [ -f "$LOCK_FILE" ]; then
    LOCK_AGE=$(($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || stat -f %m "$LOCK_FILE")))
    if [ "$LOCK_AGE" -lt "$LOCK_MAX_AGE_SEC" ]; then
        LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "?")
        log "Lock present, ${LOCK_AGE}s old, held by pid=${LOCK_PID}; aborting (another cycle in flight or recently completed)"
        exit 0
    fi
    log "Stale lock (${LOCK_AGE}s old, > ${LOCK_MAX_AGE_SEC}s); overwriting"
fi

echo "$$" > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"; log "lock released"' EXIT

# --- run cycle via Claude Code ---
log "Starting /cycle iteration; PULSE_DIR=$PULSE_DIR"

cd "$PULSE_DIR"

# Use --print so claude exits after one response; --output-format json so we can
# parse if needed; --resume would maintain Pulse's session continuity but for
# /cycle we want a fresh-ish read each iteration so it picks up changes to
# cycle-prompt.md cleanly. Pulse's continuity is in the journal, not the
# Claude Code session.
if claude --print --output-format json "Run /cycle now per the spec in ../../runbooks/cycle-prompt.md. Report findings, take auto-fix actions, write the journal entry, send any escalations." > "${LOG_DIR}/cycle.last-output.json" 2>&1; then
    log "/cycle iteration completed successfully"
else
    log "/cycle iteration failed (non-zero exit); see ${LOG_DIR}/cycle.last-output.json"
    exit 1
fi
