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

# Helper: push_with_rebase (with non-FF rebase fallback). See header.
# shellcheck source=_lib_push_with_rebase.sh
source "${HOME}/agent-core/scripts/_lib_push_with_rebase.sh"
# Single source of truth for the Pulse-owned runtime paths this script
# auto-commits (PULSE_RUNTIME_PATHS) — shared with sync_agent_core.sh so the set
# can't drift between the two committers.
# shellcheck source=_lib_pulse_runtime.sh
source "${HOME}/agent-core/scripts/_lib_pulse_runtime.sh"

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

# --- tier-window gate (PR-β) ---
# systemd fires every 5 min (Tier 1 cadence); Tier 2/3 sessions self-throttle
# by sleeping until their next window elapses. The tier state machine lives
# at ~/agents/state/cycle-tier.json (see scripts/cycle_tier_state.py).
#
# Window per tier (seconds): Tier 1 = 300, Tier 2 = 900, Tier 3 = 1800.
# Idempotency anchor: ~/agents/state/cycle-last-run.flag mtime. Even if the
# clock skews or systemd fires faster than expected, we never invoke /cycle
# more than once per Tier-1 window (Mirror review focus #5 + #8).
#
# One-writer invariant: this wrapper ONLY reads tier state. The per-iter
# `record` that advances/resets the tier lives in the cycle prompt
# (runbooks/cycle-prompt.md § 13.1) so it fires in both systemd and
# interactive /cycle modes and has direct access to checks_clean. Do NOT add
# a `record` call here — two writers per iter double-count consecutive_clean
# and promote a tier early.
TIER_STATE_FLAG="${LOCK_DIR}/cycle-last-run.flag"
TIER_STATE_JSON=$(python3 "${HOME}/agent-core/scripts/cycle_tier_state.py" read 2>>"$LOG_FILE" || echo '{"tier":1}')
CURRENT_TIER=$(echo "$TIER_STATE_JSON" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("tier",1))' 2>>"$LOG_FILE" || echo 1)
case "$CURRENT_TIER" in
    1) TIER_WINDOW_S=300 ;;
    2) TIER_WINDOW_S=900 ;;
    3) TIER_WINDOW_S=1800 ;;
    *) TIER_WINDOW_S=300 ; log "tier-window: unknown tier=${CURRENT_TIER}; defaulting to 300s" ;;
esac
LAST_RUN=$(stat -c %Y "$TIER_STATE_FLAG" 2>/dev/null || stat -f %m "$TIER_STATE_FLAG" 2>/dev/null || echo 0)
NOW_TS=$(date +%s)
ELAPSED=$((NOW_TS - LAST_RUN))
if [ "$ELAPSED" -lt "$TIER_WINDOW_S" ]; then
    log "tier-window: tier ${CURRENT_TIER} window not elapsed (${ELAPSED}s < ${TIER_WINDOW_S}s); skipping this fire"
    exit 0
fi
touch "$TIER_STATE_FLAG"
log "tier-window: tier ${CURRENT_TIER}, elapsed=${ELAPSED}s >= ${TIER_WINDOW_S}s; proceeding"

# --- wrong-branch guard ---
# Added 2026-05-27 (closes 2026-05-26 'merged but not deployed' outage class):
# run_cycle.sh auto-commits cycle-journal / cycle-actions / Pulse MEMORY on
# every successful cycle (see auto-commit block below). If the working tree
# is on a non-main branch when this fires, those commits land on the wrong
# branch — silent corruption of feature-branch history. Three-way decision:
#   - on main           → pass through (existing behavior)
#   - non-main + clean  → auto-restore (checkout main + ff-pull) + audit-log
#   - non-main + dirty  → larry_alert + exit 1; never touch the working tree
# This runs BEFORE the claude --print invocation so a dirty non-main tree
# doesn't even start a cycle (which would also error out at the auto-commit).
REPO_DIR="${REPO_DIR:-${HOME}/agent-core}"
CYCLE_ACTIONS_LOG="${REPO_DIR}/runbooks/cycle-actions.jsonl"
if [ -d "${REPO_DIR}/.git" ]; then
    CURRENT_BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    if [ "$CURRENT_BRANCH" != "main" ]; then
        if git -C "$REPO_DIR" diff --quiet 2>/dev/null && git -C "$REPO_DIR" diff --cached --quiet 2>/dev/null; then
            log "branch-guard: tree on '${CURRENT_BRANCH}' but clean; auto-restoring to main"
            if git -C "$REPO_DIR" checkout main --quiet 2>>"$LOG_FILE" \
               && git -C "$REPO_DIR" pull --ff-only --quiet 2>>"$LOG_FILE"; then
                AUTO_RESTORE_TS=$(date -u +%FT%TZ)
                mkdir -p "$(dirname "$CYCLE_ACTIONS_LOG")"
                printf '{"ts": "%s", "event": "auto-restored-main-from-%s", "actor": "run_cycle"}\n' \
                    "$AUTO_RESTORE_TS" "$CURRENT_BRANCH" >> "$CYCLE_ACTIONS_LOG"
                log "branch-guard: auto-restore succeeded; logged to ${CYCLE_ACTIONS_LOG}"
            else
                log "branch-guard: auto-restore FAILED (checkout or pull); aborting cycle"
                timeout 10 python3 "${HOME}/agent-core/scripts/larry_alerts.py" append_alert \
                    --source pulse-cycle \
                    --severity warning \
                    --subject "cycle-blocked:auto-restore-failed-from-${CURRENT_BRANCH}" \
                    --message "Pulse cycle tried to auto-restore from branch ${CURRENT_BRANCH} to main but the checkout or fast-forward pull failed. Recovery: cd ${REPO_DIR}; resolve manually (origin may have diverged); then return to main." \
                    >/dev/null 2>&1 || true
                exit 1
            fi
        else
            log "branch-guard: tree on '${CURRENT_BRANCH}' with uncommitted changes; refusing to auto-commit cycle journal"
            timeout 10 python3 "${HOME}/agent-core/scripts/larry_alerts.py" append_alert \
                --source pulse-cycle \
                --severity warning \
                --subject "cycle-blocked:dirty-tree-on-${CURRENT_BRANCH}" \
                --message "Pulse cycle on branch ${CURRENT_BRANCH} with uncommitted changes; refusing to auto-commit cycle journal (would land on wrong branch). Recovery: cd ${REPO_DIR}; resolve work on ${CURRENT_BRANCH}, then return to main." \
                >/dev/null 2>&1 || true
            exit 1
        fi
    fi
fi

# --- run cycle via Claude Code ---
log "Starting /cycle iteration; PULSE_DIR=$PULSE_DIR"

cd "$PULSE_DIR"

# Use --print so claude exits after one response; --output-format json so we can
# parse if needed; --resume would maintain Pulse's session continuity but for
# /cycle we want a fresh-ish read each iteration so it picks up changes to
# cycle-prompt.md cleanly. Pulse's continuity is in the journal, not the
# Claude Code session.
CYCLE_OUT="${LOG_DIR}/cycle.last-output.json"
CYCLE_OK=0

# --- auth: follow the team's active account tier ---------------------------
# Authenticate /cycle against the active tier's durable setup-token — the SAME
# source agent_runner uses for a dispatch — rather than HOME's auto-refreshing
# ~/.claude/.credentials.json, which rots silently on an OAuth refresh failure
# and then 401s every iteration (the recurring false "Tier N down"). active_tier
# reads blackboard/active-tier.json, so a dashboard tier switch or an automatic
# rotation moves the heartbeat with the team. HOME deliberately stays put: the
# cycle operates against /home/larry/.claude (project trust, settings, MCP) — so
# only the auth token follows the tier. Empty token => keep prior behavior.
# Exported (never passed as an arg) so the token cannot leak into `ps`; NEVER
# logged. Best-effort: a lookup failure leaves the token empty and the cycle
# simply falls back to the pre-existing credentials.json path. The active tier
# itself is already recorded per-iteration in the cost ledger below
# (ACCOUNT_TIER), so we don't re-resolve it here just to name it in a log line.
CYCLE_OAUTH_TOKEN="$(timeout 10 python3 "${HOME}/agent-core/scripts/active_tier.py" active-setup-token 2>>"$LOG_FILE" || true)"
if [ -n "$CYCLE_OAUTH_TOKEN" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$CYCLE_OAUTH_TOKEN"
    log "auth: /cycle authenticating via active-tier setup_token"
else
    log "auth: /cycle falling back to credentials.json (no active-tier setup-token)"
fi

if claude --print --model claude-sonnet-4-6 --output-format json "Run /cycle now per the spec in ../../runbooks/cycle-prompt.md. Report findings, take auto-fix actions, write the journal entry, send any escalations." > "$CYCLE_OUT" 2>&1; then
    log "/cycle iteration completed successfully"
    CYCLE_OK=1
else
    log "/cycle iteration failed (non-zero exit); see $CYCLE_OUT"
fi

# --- cost capture (D2) ---
# Append a Ledger-feed line to ~/agents/blackboard/costs.jsonl on every cycle,
# success or failure. Best-effort: jq absence or malformed JSON is non-fatal.
COSTS_FILE="${HOME}/agents/blackboard/costs.jsonl"
mkdir -p "$(dirname "$COSTS_FILE")"
if command -v jq >/dev/null 2>&1 && [ -s "$CYCLE_OUT" ]; then
    # Step C: per-account field. Read active tier from blackboard/active-tier.json
    # so a future rotation flip is reflected here too. Fall back to tier1 (the
    # historical default) when the file is missing or malformed.
    ACTIVE_TIER_FILE="${HOME}/agents/blackboard/active-tier.json"
    ACCOUNT_TIER="$(jq -r '.tier // "tier1"' "$ACTIVE_TIER_FILE" 2>/dev/null || echo tier1)"
    if [ -z "$ACCOUNT_TIER" ] || [ "$ACCOUNT_TIER" = "null" ]; then
        ACCOUNT_TIER="tier1"
    fi
    if COST_LINE=$(jq -c --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S%z)" --argjson ok "$CYCLE_OK" --arg acct "$ACCOUNT_TIER" '
        {
            ts: $ts,
            agent: "pulse",
            task_id: ("cycle-" + ($ts | gsub("[^0-9]"; ""))),
            task_type: "cycle",
            model: (.modelUsage // {} | keys | first // "claude-sonnet-4-6"),
            account: $acct,
            cost_usd: (.total_cost_usd // .cost_usd // null),
            input_tokens: (.usage.input_tokens // null),
            output_tokens: (.usage.output_tokens // null),
            cache_read: (.usage.cache_read_input_tokens // null),
            cache_creation: (.usage.cache_creation_input_tokens // null),
            duration_sec: (.duration_ms // null | if . then ./1000 else null end),
            success: ($ok == 1),
            source: "run_cycle.sh"
        }
    ' "$CYCLE_OUT" 2>/dev/null); then
        echo "$COST_LINE" >> "$COSTS_FILE"
        log "cost record appended to $COSTS_FILE"
    else
        log "cost-capture: jq parse failed; skipping"
    fi
else
    log "cost-capture: jq missing or empty output; skipping"
fi

# --- auto-commit journal + actions + Pulse MEMORY (D2) ---
# Pulse writes runbooks/cycle-journal.md, runbooks/cycle-actions.jsonl, and
# (sometimes) agents/pulse/MEMORY.md during a cycle. Leaving these uncommitted
# trips agent_core_health_check.py's working-copy discipline check every 30 min.
# Commit + push so the tree returns clean. Best-effort: failures are logged
# but do not abort the cycle.
#
# The path set is PULSE_RUNTIME_PATHS, sourced from _lib_pulse_runtime.sh so it
# stays identical to the set sync_agent_core.sh auto-commits (no inline drift).
REPO_DIR="${HOME}/agent-core"
if [ "$CYCLE_OK" = "1" ] && [ -d "$REPO_DIR/.git" ]; then
    cd "$REPO_DIR"
    if ! git diff --quiet -- "${PULSE_RUNTIME_PATHS[@]}" 2>/dev/null \
       || ! git diff --quiet --cached -- "${PULSE_RUNTIME_PATHS[@]}" 2>/dev/null \
       || git ls-files --others --exclude-standard -- "${PULSE_RUNTIME_PATHS[@]}" | grep -q .; then
        git add -- "${PULSE_RUNTIME_PATHS[@]}" 2>/dev/null || true

        TS=$(date -u +%Y%m%dT%H%M%SZ)
        if git commit -q -m "Pulse cycle ${TS}" -m "Auto-committed by run_cycle.sh after successful /cycle." 2>>"$LOG_FILE"; then
            log "auto-commit: created commit for cycle ${TS}"
            # push_with_rebase handles non-FF refusal (origin advanced via
            # an interactive PR merge) by pull --rebase --autostash + retry.
            # It writes its own log lines; || true keeps set -e happy when
            # the push genuinely fails (the next cycle will retry).
            push_with_rebase origin main "$LOG_FILE" || true
        else
            log "auto-commit: nothing to commit (or commit failed; see log)"
        fi
    else
        log "auto-commit: no Pulse-owned changes to commit"
    fi
fi

if [ "$CYCLE_OK" = "1" ]; then
    exit 0
else
    exit 1
fi
