#!/usr/bin/env bash
# run_ledger.sh — invoke Ledger weekly cost report.
#
# Wraps scripts/ledger_weekly.py with concurrency-lock + auto-commit of the
# journal. Invoked by ourliberty-ledger.service (systemd) or manually:
#   bash ~/agent-core/scripts/run_ledger.sh
#
# Modeled on scripts/run_cycle.sh.

set -e

REPO_DIR="${HOME}/agent-core"
LEDGER_PY="${REPO_DIR}/scripts/ledger_weekly.py"
LOCK_DIR="${HOME}/agents/state"
LOCK_FILE="${LOCK_DIR}/.ledger.lock"
LOCK_MAX_AGE_SEC=$((30 * 60))
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/ledger.log"
HALT_FLAG="${HOME}/agents/blackboard/EMERGENCY_HALT"

# shellcheck source=_lib_push_with_rebase.sh
source "${REPO_DIR}/scripts/_lib_push_with_rebase.sh"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_ledger: $*" | tee -a "$LOG_FILE"
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
        log "Lock present, ${LOCK_AGE}s old, held by pid=${LOCK_PID}; aborting (another run in flight or recently completed)"
        exit 0
    fi
    log "Stale lock (${LOCK_AGE}s old, > ${LOCK_MAX_AGE_SEC}s); overwriting"
fi

echo "$$" > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"; log "lock released"' EXIT

# --- run the Python module ---
log "Starting Ledger weekly run; LEDGER_PY=$LEDGER_PY"

LEDGER_OK=0
if python3 "$LEDGER_PY" >>"$LOG_FILE" 2>&1; then
    log "ledger_weekly.py completed successfully"
    LEDGER_OK=1
else
    log "ledger_weekly.py exited non-zero; see $LOG_FILE"
fi

# --- auto-commit journal ---
# ledger_weekly.py appends to runbooks/ledger-journal.md (whether or not the
# report succeeded). Commit + push so the journal is visible to anyone tailing
# the repo. Best-effort: failures are logged but do not abort.
if [ -d "$REPO_DIR/.git" ]; then
    cd "$REPO_DIR"
    if ! git diff --quiet -- runbooks/ledger-journal.md 2>/dev/null \
       || ! git diff --quiet --cached -- runbooks/ledger-journal.md 2>/dev/null; then
        git add runbooks/ledger-journal.md 2>/dev/null || true
        TS=$(date -u +%Y%m%dT%H%M%SZ)
        if git commit -q -m "ledger: weekly run ${TS}" -m "Auto-committed by run_ledger.sh." 2>>"$LOG_FILE"; then
            log "auto-commit: created commit for run ${TS}"
            # Rebase fallback (same helper run_cycle.sh uses): a bare push to
            # main loses the race when a PR merge advances origin mid-run, and a
            # bare-push failure here would leave a local-only commit on main that
            # breaks the next sync fast-forward. push_with_rebase rebases + retries.
            if push_with_rebase origin main "$LOG_FILE"; then
                log "auto-commit: pushed to origin/main (rebase fallback available)"
            else
                log "auto-commit: push failed even after rebase fallback (commit retained locally)"
            fi
        else
            log "auto-commit: nothing to commit"
        fi
    else
        log "auto-commit: no journal changes to commit"
    fi
fi

if [ "$LEDGER_OK" = "1" ]; then
    exit 0
else
    exit 1
fi
