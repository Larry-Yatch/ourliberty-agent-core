#!/usr/bin/env bash
# run_parked_aging_digest.sh — invoke the Missions v2 parked-&-aging digest gen.
#
# Wraps scripts/parked_aging_digest_generator.py with a concurrency lock +
# EMERGENCY_HALT gate + logging. Invoked by the
# ourliberty-parked-aging-digest.service unit (daily timer), or manually for an
# on-demand refresh:
#   bash ~/agent-core/scripts/run_parked_aging_digest.sh             # scheduled
#   bash ~/agent-core/scripts/run_parked_aging_digest.sh on-demand   # refresh
#
# Modeled on scripts/run_ceo_digest.sh. The generator is stdlib-only and writes
# a single artifact, so this wrapper only owns the lock + halt gate + logging.

set -e

TRIGGER="${1:-scheduled}"
if [ "$TRIGGER" != "scheduled" ] && [ "$TRIGGER" != "on-demand" ]; then
    echo "run_parked_aging_digest.sh: arg must be 'scheduled' or 'on-demand' (got '${TRIGGER}')" >&2
    exit 2
fi

REPO_DIR="${HOME}/agent-core"
GEN_PY="${REPO_DIR}/scripts/parked_aging_digest_generator.py"
LOCK_DIR="${HOME}/agents/state"
LOCK_FILE="${LOCK_DIR}/.parked-aging-digest.lock"
LOCK_MAX_AGE_SEC=$((30 * 60))
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/parked-aging-digest.log"
HALT_FLAG="${HOME}/agents/blackboard/EMERGENCY_HALT"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_parked_aging_digest[${TRIGGER}]: $*" | tee -a "$LOG_FILE"
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

log "Starting ${TRIGGER} parked-&-aging digest; GEN_PY=$GEN_PY"
if python3 "$GEN_PY" --trigger "$TRIGGER" >>"$LOG_FILE" 2>&1; then
    log "parked_aging_digest_generator.py (${TRIGGER}) completed successfully"
    exit 0
else
    RC=$?
    log "parked_aging_digest_generator.py (${TRIGGER}) exited ${RC}; see $LOG_FILE"
    exit "$RC"
fi
