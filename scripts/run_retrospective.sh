#!/usr/bin/env bash
# run_retrospective.sh — run the weekly elevation retrospective (Stage A → B).
#
# Wraps the two-stage retrospective with a concurrency-lock + EMERGENCY_HALT
# gate. Stage A (pulse_check_retrospective.py) is the deterministic
# gather→bucket→join that emits retrospective-candidates.json; Stage B
# (pulse_check_retrospective_author.py) is the bounded-LLM author that
# classifies + pre-drafts + posts proposed missions. Stage B reads Stage A's
# artifact + ledger, so they MUST run in order and in the same process window.
#
# Invoked by ourliberty-retrospective-weekly.service, or manually:
#   bash ~/agent-core/scripts/run_retrospective.sh
#   bash ~/agent-core/scripts/run_retrospective.sh --force   # bypass weekly sentinel
#
# Modeled on scripts/run_ceo_digest.sh. Cost capture for Stage B's LLM call is
# done inside the Python module (append to costs.jsonl); each stage emits its
# own liveness heartbeat via pulse_check_heartbeat.run_check, so this wrapper
# only owns the lock + halt gate + logging.
#
# Stage B is best-effort relative to Stage A: if Stage A succeeds but Stage B
# fails, we still exit non-zero so the failure surfaces, but Stage A's artifact
# + ledger are already durably written (the next run can re-author).

set -e

REPO_DIR="${HOME}/agent-core"
STAGE_A_PY="${REPO_DIR}/scripts/pulse_check_retrospective.py"
STAGE_B_PY="${REPO_DIR}/scripts/pulse_check_retrospective_author.py"
LOCK_DIR="${HOME}/agents/state"
LOCK_FILE="${LOCK_DIR}/.retrospective.lock"
LOCK_MAX_AGE_SEC=$((30 * 60))
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/retrospective.log"
HALT_FLAG="${HOME}/agents/blackboard/EMERGENCY_HALT"

# Pass-through flags (e.g. --force) forwarded to BOTH stages.
EXTRA_ARGS=("$@")

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_retrospective: $*" | tee -a "$LOG_FILE"
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

log "Stage A (gather→bucket→join); STAGE_A_PY=$STAGE_A_PY"
if python3 "$STAGE_A_PY" "${EXTRA_ARGS[@]}" >>"$LOG_FILE" 2>&1; then
    log "Stage A completed successfully"
else
    RC=$?
    log "Stage A exited ${RC}; NOT running Stage B (no candidates artifact). See $LOG_FILE"
    exit "$RC"
fi

log "Stage B (LLM author → proposed missions); STAGE_B_PY=$STAGE_B_PY"
if python3 "$STAGE_B_PY" "${EXTRA_ARGS[@]}" >>"$LOG_FILE" 2>&1; then
    log "Stage B completed successfully"
    exit 0
else
    RC=$?
    log "Stage B exited ${RC}; Stage A artifact+ledger are durable, re-author next run. See $LOG_FILE"
    exit "$RC"
fi
