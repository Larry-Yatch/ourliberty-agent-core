#!/usr/bin/env bash
# run_main_suite_guardian.sh — nightly Main-Suite Green Guardian run (D1.9).
#
# Wraps scripts/main_suite_guardian.py with the suite-runner ecosystem's
# single-flight lock, a hard wall-clock kill, EMERGENCY_HALT, and a liveness
# heartbeat. Invoked by ourliberty-main-suite-guardian.service (systemd) or:
#   bash ~/agent-core/scripts/run_main_suite_guardian.sh
#
# Modeled on scripts/run_ledger.sh. Spec: main-suite-green-guardian.md L7/D1.9.
#
# Discipline (spec D1.9):
#   * Acquire the RELOCATED warmer single-flight lock (absolute path — never
#     $HOME-relative, per L7) so a suite-scale guardian run and the regbaseline
#     warmer never stack (the documented droplet-OOM class: no swap, stacked
#     suite runs OOM a live agent). Can't acquire => skip the night, journal it.
#   * Hard wall-clock kill at 2x the in-process cap.
#   * Raw suite output is NEVER teed to ~/agents/logs pre-scan: the Python
#     process captures suite stdout internally, runs the outside-jail sentinel
#     scan itself, and prints ONLY the parsed result JSON. We buffer even that
#     to a tmp file and append it to the log only after the process returns.
#   * Heartbeat on clean exit so the pulse-check staleness healer owns liveness.

set -u

REPO_DIR="${HOME}/agent-core"
GUARDIAN_PY="${REPO_DIR}/scripts/main_suite_guardian.py"
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/main-suite-guardian.log"
HALT_FLAG="${HOME}/agents/blackboard/EMERGENCY_HALT"

# Absolute lock path — MUST match regression_baseline_cache.REGBASELINE_LOCK_PATH
# byte-for-byte (L7). NEVER $HOME-relative: a tier-swapped process would flock a
# different file and void single-flight (the #755 HOME-swap class).
LOCK_FILE="/home/larry/agents/state/ol-regbaseline-warm.lock"

# In-process wall cap is TOTAL_WALL_CAP_S=5400 (90 min); the hard kill is 2x.
HARD_KILL_SEC=10800
HEARTBEAT_ID="main-suite-guardian"

mkdir -p "$LOG_DIR" "$(dirname "$LOCK_FILE")"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_main_suite_guardian: $*" \
        | tee -a "$LOG_FILE"
}

# --- emergency halt ---
if [ -f "$HALT_FLAG" ]; then
    log "EMERGENCY_HALT present at $HALT_FLAG; aborting"
    exit 0
fi

# --- run under the warmer single-flight lock (flock: dead holders auto-release,
#     so there is no stale-pid-file class to reap) ---
run_guarded() {
    log "Starting guardian run; GUARDIAN_PY=$GUARDIAN_PY"

    OUT_TMP="$(mktemp -t guardian-out.XXXXXX)"
    RC=0
    # Hard wall-clock kill at 2x the in-process cap; --kill-after escalates to
    # SIGKILL if the process ignores SIGTERM.
    timeout --kill-after=30 "$HARD_KILL_SEC" \
        python3 "$GUARDIAN_PY" "$@" >"$OUT_TMP" 2>&1 || RC=$?

    # Append the (parsed-only) output to the log AFTER the process returned —
    # by which point its internal sentinel scan has already run.
    cat "$OUT_TMP" >>"$LOG_FILE"
    rm -f "$OUT_TMP"

    if [ "$RC" -eq 0 ]; then
        log "guardian run completed successfully"
        # Heartbeat only on a clean exit (freshness-of-success liveness).
        python3 -c "import sys; sys.path.insert(0, '${REPO_DIR}/scripts'); \
import pulse_check_heartbeat as h; h.emit_heartbeat('${HEARTBEAT_ID}')" \
            2>>"$LOG_FILE" || log "heartbeat emit failed (non-fatal)"
    elif [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
        log "guardian run HARD-KILLED after ${HARD_KILL_SEC}s (rc=$RC)"
    else
        log "guardian run exited non-zero (rc=$RC); see $LOG_FILE"
    fi
    return "$RC"
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another suite-scale run holds ${LOCK_FILE}; skipping the night (single-flight)"
    exit 0
fi

run_guarded "$@"
GUARDIAN_RC=$?
flock -u 9

exit "$GUARDIAN_RC"
