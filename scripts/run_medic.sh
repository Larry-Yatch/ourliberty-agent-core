#!/usr/bin/env bash
# run_medic.sh -- invoke the Medic operator on a prepared batch file.
#
# Wraps Claude Code with concurrency-lock + per-iteration logging,
# modeled on scripts/run_cycle.sh. Invoked by scripts/medic_dispatcher.py
# (which is itself fired by ourliberty-medic-dispatcher.service):
#
#   bash ~/agent-core/scripts/run_medic.sh <batch-path>
#
# The dispatcher does the cheap gates + queue scan; this script spins
# the Claude operator once per qualifying batch.

set -e

BATCH_PATH="${1:-}"
if [ -z "$BATCH_PATH" ]; then
    echo "run_medic.sh: missing batch path argument" >&2
    exit 2
fi

MEDIC_DIR="${HOME}/agent-core/agents/medic"
LOCK_DIR="${HOME}/agents/state"
LOCK_FILE="${LOCK_DIR}/.medic.lock"
LOCK_MAX_AGE_SEC=$((30 * 60))   # 30 minutes -- stale lock threshold
LOG_DIR="${HOME}/agents/logs"
LOG_FILE="${LOG_DIR}/medic.log"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_medic: $*" | tee -a "$LOG_FILE"
}

# --- concurrency guard ---
if [ -f "$LOCK_FILE" ]; then
    LOCK_AGE=$(($(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || stat -f %m "$LOCK_FILE")))
    if [ "$LOCK_AGE" -lt "$LOCK_MAX_AGE_SEC" ]; then
        LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "?")
        log "Lock present, ${LOCK_AGE}s old, held by pid=${LOCK_PID}; aborting (another medic run in flight)"
        exit 0
    fi
    log "Stale lock (${LOCK_AGE}s old, > ${LOCK_MAX_AGE_SEC}s); overwriting"
fi

echo "$$" > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"; log "lock released"' EXIT

if [ ! -f "$BATCH_PATH" ]; then
    log "batch file missing at $BATCH_PATH; aborting"
    exit 2
fi

# --- run operator via Claude Code ---
log "Starting Medic operator; MEDIC_DIR=$MEDIC_DIR batch=$BATCH_PATH"

cd "$MEDIC_DIR"

MEDIC_OUT="${LOG_DIR}/medic.last-output.json"
MEDIC_OK=0
PROMPT_BODY="Run the Medic protocol now per the spec in ./CLAUDE.md. The batch of owned alerts is at ${BATCH_PATH}. For each alert: investigate with read-only bash, classify per config/medic-action-policy.json, and emit a diagnosis + recommended command via scripts/larry_alerts.py (append_notification for a diagnosis, or append_approval_request when the recommended fix is privileged). Take NO remediation action -- PR1 is escalate-only."

# Per-session timeout. One hung `claude -p` session held the lock ~30 min
# during the first live hours of Medic; the lock bounded the blast radius
# but a hung session has no business holding a rate-window slot that long.
# Configurable via MEDIC_CLAUDE_TIMEOUT (any `timeout(1)` duration string),
# default 10 minutes. On timeout the EXIT trap above releases the lock.
CLAUDE_TIMEOUT="${MEDIC_CLAUDE_TIMEOUT:-10m}"

if timeout "$CLAUDE_TIMEOUT" claude --print --model claude-sonnet-4-6 --output-format json "$PROMPT_BODY" > "$MEDIC_OUT" 2>&1; then
    log "Medic operator completed successfully"
    MEDIC_OK=1
else
    CLAUDE_EXIT=$?
    if [ "$CLAUDE_EXIT" = "124" ]; then
        log "Medic operator timed out after $CLAUDE_TIMEOUT (timeout exit 124); aborting tick (lock will release via trap)"
        exit 124
    fi
    log "Medic operator failed (exit=$CLAUDE_EXIT); see $MEDIC_OUT"
fi

# --- cost capture (mirror run_cycle.sh) ---
COSTS_FILE="${HOME}/agents/blackboard/costs.jsonl"
mkdir -p "$(dirname "$COSTS_FILE")"
if command -v jq >/dev/null 2>&1 && [ -s "$MEDIC_OUT" ]; then
    ACTIVE_TIER_FILE="${HOME}/agents/blackboard/active-tier.json"
    ACCOUNT_TIER="$(jq -r '.tier // "tier1"' "$ACTIVE_TIER_FILE" 2>/dev/null || echo tier1)"
    if [ -z "$ACCOUNT_TIER" ] || [ "$ACCOUNT_TIER" = "null" ]; then
        ACCOUNT_TIER="tier1"
    fi
    if COST_LINE=$(jq -c --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S%z)" --argjson ok "$MEDIC_OK" --arg acct "$ACCOUNT_TIER" '
        {
            ts: $ts,
            agent: "medic",
            task_id: ("medic-" + ($ts | gsub("[^0-9]"; ""))),
            task_type: "medic-escalate",
            model: (.modelUsage // {} | keys | first // "claude-sonnet-4-6"),
            account: $acct,
            cost_usd: (.total_cost_usd // .cost_usd // null),
            input_tokens: (.usage.input_tokens // null),
            output_tokens: (.usage.output_tokens // null),
            cache_read: (.usage.cache_read_input_tokens // null),
            cache_creation: (.usage.cache_creation_input_tokens // null),
            duration_sec: (.duration_ms // null | if . then ./1000 else null end),
            success: ($ok == 1),
            source: "run_medic.sh"
        }
    ' "$MEDIC_OUT" 2>/dev/null); then
        echo "$COST_LINE" >> "$COSTS_FILE"
        log "cost record appended to $COSTS_FILE"
    else
        log "cost-capture: jq parse failed; skipping"
    fi
else
    log "cost-capture: jq missing or empty output; skipping"
fi

if [ "$MEDIC_OK" = "1" ]; then
    exit 0
else
    exit 1
fi
