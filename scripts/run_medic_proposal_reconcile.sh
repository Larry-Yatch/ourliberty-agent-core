#!/usr/bin/env bash
# run_medic_proposal_reconcile.sh — one reconcile pass for the medic-proposal
# feed loop (slice 9).
#
# Wraps scripts/medic_proposal_reconcile.py with a concurrency lock + the
# EMERGENCY_HALT gate. Invoked by ourliberty-medic-proposal-reconcile.service,
# or manually:
#   bash ~/agent-core/scripts/run_medic_proposal_reconcile.sh [--dry-run]
#
# Modeled on scripts/run_mission_rank.sh, but this pass is cheap + deterministic
# (no claude spawn — it reads the medic ledger and emits/retracts captures over
# HTTP), so the lock max-age is short. The halt gate still applies: the factory
# kill switch stops it proposing/retracting cold.

set -e

REPO_DIR="${HOME}/agent-core"
PY="${REPO_DIR}/scripts/medic_proposal_reconcile.py"
LOCK_DIR="${HOME}/agents/state"
LOCK_FILE="${LOCK_DIR}/.medic-proposal-reconcile.lock"
# Runs finish in seconds; the timer fires every 15 min. A 10-min max-age
# strictly exceeds the unit TimeoutStartSec (300s) so a systemd-bounded pass
# never looks stale to a concurrent manual run, yet a truly dead lock clears
# well before the next tick.
LOCK_MAX_AGE_SEC=$((10 * 60))
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/medic-proposal-reconcile.log"
HALT_FLAG="${HOME}/agents/blackboard/EMERGENCY_HALT"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_medic_proposal_reconcile: $*" | tee -a "$LOG_FILE"
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

log "Starting reconcile pass; PY=$PY $*"
if python3 "$PY" "$@" >>"$LOG_FILE" 2>&1; then
    log "medic_proposal_reconcile.py completed successfully"
    exit 0
else
    RC=$?
    log "medic_proposal_reconcile.py exited ${RC}; see $LOG_FILE"
    exit "$RC"
fi
