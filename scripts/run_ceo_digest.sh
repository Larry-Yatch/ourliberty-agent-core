#!/usr/bin/env bash
# run_ceo_digest.sh — invoke the N6 CEO digest generator for one cadence.
#
# Wraps scripts/ceo_digest_generator.py with concurrency-lock + EMERGENCY_HALT
# gate. Invoked by the ourliberty-ceo-digest-{daily,weekly}.service units, or
# manually:
#   bash ~/agent-core/scripts/run_ceo_digest.sh daily
#   bash ~/agent-core/scripts/run_ceo_digest.sh weekly
#
# Modeled on scripts/run_ledger.sh. Cost capture for the digest's own LLM call
# is done inside the Python module (append to costs.jsonl), so this wrapper
# only owns the lock + halt gate + logging.

set -e

PERIOD="${1:-}"
if [ "$PERIOD" != "daily" ] && [ "$PERIOD" != "weekly" ]; then
    echo "run_ceo_digest.sh: first arg must be 'daily' or 'weekly' (got '${PERIOD}')" >&2
    exit 2
fi

REPO_DIR="${HOME}/agent-core"
GEN_PY="${REPO_DIR}/scripts/ceo_digest_generator.py"
LOCK_DIR="${HOME}/agents/state"
LOCK_FILE="${LOCK_DIR}/.ceo-digest-${PERIOD}.lock"
LOCK_MAX_AGE_SEC=$((30 * 60))
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/ceo-digest.log"
HALT_FLAG="${HOME}/agents/blackboard/EMERGENCY_HALT"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_ceo_digest[${PERIOD}]: $*" | tee -a "$LOG_FILE"
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

log "Starting ${PERIOD} CEO digest; GEN_PY=$GEN_PY"
if python3 "$GEN_PY" --period "$PERIOD" >>"$LOG_FILE" 2>&1; then
    log "ceo_digest_generator.py (${PERIOD}) completed successfully"
    exit 0
else
    RC=$?
    log "ceo_digest_generator.py (${PERIOD}) exited ${RC}; see $LOG_FILE"
    exit "$RC"
fi
