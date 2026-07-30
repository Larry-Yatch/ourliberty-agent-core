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
LOCK_DIR="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/state"
LOCK_FILE="${LOCK_DIR}/.medic.lock"
LOCK_MAX_AGE_SEC=$((30 * 60))   # 30 minutes -- stale lock threshold
LOG_DIR="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/logs"
LOG_FILE="${LOG_DIR}/medic.log"

# The model this wrapper dispatches on. SINGLE SOURCE: it is both the value
# passed to `claude --model` below and the $wm the cost-row selector falls back
# to (see _lib_cost_capture.sh). A second literal anywhere in this file would
# let the dispatched model and the recorded model drift apart.
WORK_MODEL="claude-sonnet-4-6"

mkdir -p "$LOCK_DIR" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] run_medic: $*" | tee -a "$LOG_FILE"
}

# Single definition of the costs.jsonl row + the model selector, shared with
# run_cycle.sh (they carried byte-identical hand-copies until 2026-07-30).
# Resolved RELATIVE TO THIS SCRIPT, not $HOME, so it is found wherever the
# wrapper is invoked from.
#
# GUARDED IN BOTH DIRECTIONS: under `set -e` an unguarded `source` aborts the
# entire medic tick — strictly worse than the best-effort cost row it protects
# — and `[ -f ]` alone only covers ABSENCE. A file that exists but cannot be
# loaded kills the tick just as dead, so:
#   * `[ -r ]` covers mode/ownership drift (a mode-000 lib);
#   * `bash -n` covers a PARSE error, which `source X || …` does NOT survive
#     (bash still exits 2 through the `||`);
#   * `|| _COST_LIB_STATE=unloadable` covers the remaining direction, a RUNTIME
#     failure inside the lib (errexit is suspended inside a `||` list).
# The state is tri-valued so the cost block below names the ACTUAL reason
# rather than reporting every failure as "lib missing" — a log that asserts the
# wrong cause is the same honesty defect the cost row's rc surface exists to
# close. Deliberately placed BELOW `log()` (so an unloadable lib reaches
# medic.log, not just the journal) and ABOVE the lock write (so a failure here
# still cannot leave a stale lock behind).
#
# A malformed lib must not reach the droplet in the first place:
# scripts/tests/test_shell_scripts_parse.py runs `bash -n` over every
# scripts/**/*.sh, so this guard is the second line, not the only one.
_COST_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_lib_cost_capture.sh"
_COST_LIB_STATE=absent
if [ -f "$_COST_LIB" ]; then
    if [ -r "$_COST_LIB" ] && "${BASH:-bash}" -n "$_COST_LIB" 2>/dev/null; then
        # shellcheck source=_lib_cost_capture.sh
        _COST_LIB_STATE=loaded
        source "$_COST_LIB" || _COST_LIB_STATE=unloadable
    else
        _COST_LIB_STATE=unloadable
    fi
fi

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
PROMPT_BODY="Run the Medic protocol now per the spec in ./CLAUDE.md. The batch of owned alerts is at ${BATCH_PATH}. For each alert: investigate read-only, then classify the remediation into an action_type and map it to a tier via config/medic-action-policy.json, and act exactly as ./CLAUDE.md specifies for PR2. (1) For a REVERSIBLE action_type that ./CLAUDE.md routes through the enforcement module -- restart-daemon, retrigger-inbox/retrigger-watcher, or silence-false-positive -- invoke scripts/medic_actions.py with the matching subcommand plus the target/fingerprint. medic_actions.py re-checks every gate, validates the target against the fail-safe allowlist, and hard-gates one-action-per-fingerprint; it REFUSES (not-permitted) any target not on the allowlist, in which case you fall back to a diagnose-only escalation. On a SUCCESSFUL restart/retrigger (ok:true, outcome:acted) emit exactly one act-then-notify notification via scripts/larry_alerts.py append_notification, per ./CLAUDE.md. A confirmed-benign fingerprint matching a silenceable_subjects pattern is durably silenced with NO DM (do not also notify on success); on any refusal fall back to diagnose-only. (2) For every OTHER reversible action_type, and for the JUDGMENT tier, escalate diagnose-only via scripts/larry_alerts.py append_notification. (3) For the PRIVILEGED tier, emit scripts/larry_alerts.py append_approval_request whose body carries the exact proposed command. Never run a raw mutating command yourself: the ONLY mutating path is scripts/medic_actions.py, which re-checks every gate at action time."

# Per-session timeout. One hung `claude -p` session held the lock ~30 min
# during the first live hours of Medic; the lock bounded the blast radius
# but a hung session has no business holding a rate-window slot that long.
# Configurable via MEDIC_CLAUDE_TIMEOUT (any `timeout(1)` duration string),
# default 10 minutes. On timeout the EXIT trap above releases the lock.
CLAUDE_TIMEOUT="${MEDIC_CLAUDE_TIMEOUT:-10m}"

# Expose Larry's chat id to the operator under the name CLAUDE.md uses.
# The systemd unit loads TELEGRAM_CHAT_ID_LARRY via EnvironmentFiles=.env.larry;
# the Medic escalation CLI requires --chat-id, so alias it here.
export LARRY_CHAT_ID="${TELEGRAM_CHAT_ID_LARRY:-}"

# Claude Code BLOCKS any command containing a shell-variable expansion, so the
# operator cannot use "$LARRY_CHAT_ID" inside an append_notification command --
# it must write a LITERAL integer. Inject the resolved value into the prompt.
MEDIC_CHAT_ID="${TELEGRAM_CHAT_ID_LARRY:-}"
PROMPT_BODY="${PROMPT_BODY} IMPORTANT: when you emit via scripts/larry_alerts.py append_notification / append_approval_request, write --chat-id ${MEDIC_CHAT_ID} as a LITERAL integer. Never use a shell variable (e.g. \$LARRY_CHAT_ID) in any command -- Claude Code blocks commands containing variable expansions, so the escalation would be denied and silently fail."

# --- auth: per-run tier from the dispatch pool (mirror run_cycle.sh) --------
# Each Medic tick is a fresh short task, so it dispatches through the same
# {tier1,tier3} round-robin pool as every other wiring point (spec §4) instead
# of riding the single active tier. select-dispatch-env honors the operator
# pin, cooldowns, and the proactive budget caps, and prints TIER=<tier> plus
# the selected tier's setup-token. Only the token follows the tier -- HOME
# deliberately stays on /home/larry/.claude; select-dispatch-env returns "no
# tier" for any tier that would need a HOME swap (the units run
# ProtectHome=read-only with only /home/larry/.claude writable, so a swap would
# EROFS the child on ~/.claude.json and never fall back). The token is exported
# INSIDE the claude subshell below -- never in this shell -- so it never
# appears in this script's argv/`ps` and is NEVER logged.
# Fail-safe: on ANY selection failure (missing/erroring CLI, no usable tier,
# a would-need-HOME-swap tier) fall back to the legacy active-setup-token path
# so Medic never silently stops with the pool machinery. Empty token there =>
# keep the original credentials.json behavior.
DISPATCH_ENV_LINES="$(timeout 15 python3 "${HOME}/agent-core/scripts/active_tier.py" select-dispatch-env 2>>"$LOG_FILE" || true)"
DISPATCH_TIER="$(printf '%s' "$DISPATCH_ENV_LINES" | sed -n '1s/^TIER=//p')"
if [ -n "$DISPATCH_TIER" ]; then
    log "auth: Medic dispatching on pool-selected ${DISPATCH_TIER}"
else
    DISPATCH_ENV_LINES=""
    MEDIC_OAUTH_TOKEN="$(timeout 10 python3 "${HOME}/agent-core/scripts/active_tier.py" active-setup-token 2>>"$LOG_FILE" || true)"
    if [ -n "$MEDIC_OAUTH_TOKEN" ]; then
        DISPATCH_ENV_LINES="CLAUDE_CODE_OAUTH_TOKEN=${MEDIC_OAUTH_TOKEN}"
        log "auth: Medic authenticating via active-tier setup_token (pool selection unavailable)"
    else
        log "auth: Medic falling back to credentials.json (no active-tier setup-token)"
    fi
fi

if (
    # Export the selected tier's token for the claude child only (see the auth
    # block above); the subshell keeps it out of this script's environment and
    # out of `ps`. Token-only by contract -- the TIER= marker and anything else
    # are ignored.
    while IFS= read -r kv; do
        case "$kv" in
            CLAUDE_CODE_OAUTH_TOKEN=*) export "$kv" ;;
        esac
    done <<< "$DISPATCH_ENV_LINES"
    exec timeout "$CLAUDE_TIMEOUT" claude --print --model "$WORK_MODEL" --output-format json "$PROMPT_BODY" > "$MEDIC_OUT" 2>&1
); then
    log "Medic operator completed successfully"
    MEDIC_OK=1
else
    CLAUDE_EXIT=$?
    if [ "$CLAUDE_EXIT" = "124" ]; then
        log "Medic operator timed out after $CLAUDE_TIMEOUT (timeout exit 124); aborting tick (lock will release via trap)"
        exit 124
    fi
    log "Medic operator failed (exit=$CLAUDE_EXIT); see $MEDIC_OUT"
    # Reactive benching (spec §6): when the pool dispatched this tick and the
    # failure is a rate-limit / auth-401 wall, bench the tier so the next
    # tick round-robins onto a healthy one. Best-effort; '' = not a wall.
    if [ -n "$DISPATCH_TIER" ]; then
        FAIL_CLASS="$(timeout 10 python3 "${HOME}/agent-core/scripts/active_tier.py" report-dispatch-failure "$DISPATCH_TIER" medic "$MEDIC_OUT" 2>>"$LOG_FILE" || true)"
        if [ -n "$FAIL_CLASS" ]; then
            log "tier-pool: classified failure as ${FAIL_CLASS}; benched ${DISPATCH_TIER}"
        fi
    fi
fi

# --- cost capture (mirror run_cycle.sh) ---
# Runs on success or failure — a failed paid tick still burned quota, and
# rolling_5h_token_volume must see it; the `success:` field carries the
# outcome. (A TIMED-OUT tick exits 124 above and never reaches here; that is
# pre-existing and pinned by test_run_medic_timeout.)
#
# The row (and the work-model selector) live in _lib_cost_capture.sh so this
# wrapper and run_cycle.sh cannot drift. $WORK_MODEL is the SAME variable
# passed to `claude --model` above.
COSTS_FILE="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/blackboard/costs.jsonl"
mkdir -p "$(dirname "$COSTS_FILE")"
#
# Every rc the lib can return gets its OWN arm. The catch-all must not assert a
# cause: it used to say "jq missing or empty output", which would mislabel an
# append that failed, a bad-arity call and an unbuildable row alike — the same
# class of lie as reporting a lost row as appended.
if [ "$_COST_LIB_STATE" = loaded ] && command -v capture_cost_row >/dev/null 2>&1; then
    COST_RC=0
    capture_cost_row \
        "$MEDIC_OUT" "medic" "medic-escalate" "medic-" "$MEDIC_OK" \
        "$DISPATCH_TIER" "run_medic.sh" "$WORK_MODEL" "$COSTS_FILE" || COST_RC=$?
    case "$COST_RC" in
        0)  log "cost record appended to $COSTS_FILE" ;;
        10) log "cost-capture: jq missing or empty output; skipping" ;;
        20) log "cost-capture: jq parse failed; skipping" ;;
        30) log "cost-capture: called with bad arguments; no row written" ;;
        40) log "cost-capture: no cost row could be built from $MEDIC_OUT; no row written" ;;
        41) log "cost-capture: refused a multi-row build from $MEDIC_OUT; no row written" ;;
        50) log "cost-capture: append to $COSTS_FILE FAILED (row LOST); the tick continues" ;;
        *)  log "cost-capture: failed (rc=$COST_RC); no row written" ;;
    esac
elif [ "$_COST_LIB_STATE" = unloadable ]; then
    log "cost-capture: lib present but could not be loaded ($_COST_LIB); skipping"
elif [ "$_COST_LIB_STATE" = loaded ]; then
    log "cost-capture: lib loaded but defines no capture_cost_row ($_COST_LIB); skipping"
else
    log "cost-capture: lib missing; skipping"
fi

if [ "$MEDIC_OK" = "1" ]; then
    exit 0
else
    exit 1
fi
