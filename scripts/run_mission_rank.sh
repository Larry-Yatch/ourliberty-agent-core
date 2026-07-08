#!/usr/bin/env bash
# run_mission_rank.sh — invoke the operator rank brain for one pass.
#
# Wraps scripts/mission_rank.py --once with concurrency-lock + EMERGENCY_HALT
# gate. Invoked by the ourliberty-mission-rank.service unit, or manually:
#   bash ~/agent-core/scripts/run_mission_rank.sh
#
# Modeled on scripts/run_ceo_digest.sh. The halt gate matters here more than
# on most timers: a rank pass spends up to DEFAULT_LLM_CAP (40) claude calls,
# so the factory kill switch must stop it cold. The lock max-age (2h15m) strictly exceeds the
# unit's TimeoutStartSec (7800s), so a systemd-bounded pass can never look stale to a concurrent manual run.

set -e

REPO_DIR="${HOME}/agent-core"
RANK_PY="${REPO_DIR}/scripts/mission_rank.py"
LOCK_DIR="${HOME}/agents/state"
LOCK_FILE="${LOCK_DIR}/.mission-rank.lock"
LOCK_MAX_AGE_SEC=$((135 * 60))
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/mission-rank.log"
HALT_FLAG="${HOME}/agents/blackboard/EMERGENCY_HALT"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_mission_rank: $*" | tee -a "$LOG_FILE"
}

# --- emergency halt ---
if [ -f "$HALT_FLAG" ]; then
    log "EMERGENCY_HALT present at $HALT_FLAG; aborting"
    exit 0
fi

# --- concurrency guard ---
if [ -f "$LOCK_FILE" ]; then
    LOCK_AGE=$(($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || stat -f %m "$LOCK_FILE")))
    if [ "$LOCK_AGE" -lt "$LOCK_MAX_AGE_SEC" ]; then
        LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "?")
        log "Lock present, ${LOCK_AGE}s old, held by pid=${LOCK_PID}; aborting (another run in flight)"
        exit 0
    fi
    log "Stale lock (${LOCK_AGE}s old, > ${LOCK_MAX_AGE_SEC}s); overwriting"
fi

echo "$$" > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"; log "lock released"' EXIT

log "Starting rank pass; RANK_PY=$RANK_PY"
if python3 "$RANK_PY" --once >>"$LOG_FILE" 2>&1; then
    log "mission_rank.py --once completed successfully"
    exit 0
else
    RC=$?
    log "mission_rank.py --once exited ${RC}; see $LOG_FILE"
    exit "$RC"
fi
