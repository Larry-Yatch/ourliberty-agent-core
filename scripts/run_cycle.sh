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

# --- auth: per-run tier from the dispatch pool ------------------------------
# Each /cycle run is a fresh short task, so it dispatches through the same
# {tier1,tier3} round-robin pool as every other wiring point (spec §4) instead
# of riding the single active tier — pre-pool the cycle burned the large
# majority of tier1's budget while tier3 idled. select-dispatch-env honors the
# operator pin (rotation.disabled), cooldowns, and the proactive budget caps,
# and prints TIER=<tier> plus the env delta for the spawn (normally just the
# tier's setup-token; HOME + agents-root/gh/git pins only on the token-less
# credentials.json fallback). The delta is exported INSIDE the claude subshell
# below — never in this shell — so a HOME swap can't repoint this script's own
# ${HOME} paths (the #755 class), and the token never appears in argv/`ps` and
# is NEVER logged.
# Fail-safe: on ANY selection failure (missing/erroring CLI, no usable tier)
# fall back to the legacy active-setup-token path — the self-healing heartbeat
# must never die with the pool machinery it heals. Empty token there => keep
# the original credentials.json behavior.
DISPATCH_ENV_LINES="$(timeout 15 python3 "${HOME}/agent-core/scripts/active_tier.py" select-dispatch-env 2>>"$LOG_FILE" || true)"
DISPATCH_TIER="$(printf '%s' "$DISPATCH_ENV_LINES" | sed -n '1s/^TIER=//p')"
if [ -n "$DISPATCH_TIER" ]; then
    log "auth: /cycle dispatching on pool-selected ${DISPATCH_TIER}"
else
    DISPATCH_ENV_LINES=""
    CYCLE_OAUTH_TOKEN="$(timeout 10 python3 "${HOME}/agent-core/scripts/active_tier.py" active-setup-token 2>>"$LOG_FILE" || true)"
    if [ -n "$CYCLE_OAUTH_TOKEN" ]; then
        DISPATCH_ENV_LINES="CLAUDE_CODE_OAUTH_TOKEN=${CYCLE_OAUTH_TOKEN}"
        log "auth: /cycle authenticating via active-tier setup_token (pool selection unavailable)"
    else
        log "auth: /cycle falling back to credentials.json (no active-tier setup-token)"
    fi
fi

if (
    # Whitelist-export the selected tier's env delta for the claude child
    # only (see the auth block above); the subshell keeps it out of this
    # script's environment and out of `ps`.
    while IFS= read -r kv; do
        case "$kv" in
            CLAUDE_CODE_OAUTH_TOKEN=*|HOME=*|OURLIBERTY_AGENTS_ROOT=*|GH_CONFIG_DIR=*|GIT_CONFIG_GLOBAL=*)
                export "$kv" ;;
        esac
    done <<< "$DISPATCH_ENV_LINES"
    exec claude --print --model claude-sonnet-4-6 --output-format json "Run /cycle now per the spec in ../../runbooks/cycle-prompt.md. Report findings, take auto-fix actions, write the journal entry, send any escalations." > "$CYCLE_OUT" 2>&1
); then
    log "/cycle iteration completed successfully"
    CYCLE_OK=1
else
    log "/cycle iteration failed (non-zero exit); see $CYCLE_OUT"
    # Reactive benching (spec §6): when the pool dispatched this run and the
    # failure is a rate-limit / auth-401 wall, bench the tier so the next
    # fire round-robins onto a healthy one (and the calibration job learns
    # from wrapper walls too). Best-effort; '' = not a wall shape.
    if [ -n "$DISPATCH_TIER" ]; then
        FAIL_CLASS="$(timeout 10 python3 "${HOME}/agent-core/scripts/active_tier.py" report-dispatch-failure "$DISPATCH_TIER" pulse "$CYCLE_OUT" 2>>"$LOG_FILE" || true)"
        if [ -n "$FAIL_CLASS" ]; then
            log "tier-pool: classified failure as ${FAIL_CLASS}; benched ${DISPATCH_TIER}"
        fi
    fi
fi

# --- cost capture (D2) ---
# Append a Ledger-feed line to ~/agents/blackboard/costs.jsonl on every cycle,
# success or failure. Best-effort: jq absence or malformed JSON is non-fatal.
COSTS_FILE="${HOME}/agents/blackboard/costs.jsonl"
mkdir -p "$(dirname "$COSTS_FILE")"
if command -v jq >/dev/null 2>&1 && [ -s "$CYCLE_OUT" ]; then
    # Step C: per-account field — the pool-selected tier when the pool
    # dispatched this run (so burn is attributed to the account that actually
    # ran, feeding rolling_5h_token_volume/near_cap). On the legacy fallback
    # path, read active-tier.json as before; tier1 when missing/malformed.
    ACCOUNT_TIER="$DISPATCH_TIER"
    if [ -z "$ACCOUNT_TIER" ]; then
        ACTIVE_TIER_FILE="${HOME}/agents/blackboard/active-tier.json"
        ACCOUNT_TIER="$(jq -r '.tier // "tier1"' "$ACTIVE_TIER_FILE" 2>/dev/null || echo tier1)"
        if [ -z "$ACCOUNT_TIER" ] || [ "$ACCOUNT_TIER" = "null" ]; then
            ACCOUNT_TIER="tier1"
        fi
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

    # --- rotate the cycle journal before committing (2026-07-07 gc-overload fix) ---
    # cycle-journal.md is rewritten and committed every cycle; left unbounded it
    # grew to ~30MB and its per-commit git blob (~90 commits/day) choked git gc,
    # spiking droplet load to 9. Trim the live journal to the most-recent entries
    # and move older ones into immutable runbooks/journal-archive/ chunks. Both
    # the trimmed journal and any new chunk are Pulse-owned paths, so the
    # auto-commit below stages them in the same commit (one-writer invariant
    # preserved). Best-effort: a rotation failure must never abort the cycle.
    if [ -f "$REPO_DIR/scripts/rotate_cycle_journal.py" ]; then
        python3 "$REPO_DIR/scripts/rotate_cycle_journal.py" >>"$LOG_FILE" 2>&1 \
            || log "rotate_cycle_journal: failed (non-fatal; see log)"
    fi

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

# --- clean-tree guard: revert stray out-of-contract edits (sync-wedge class) ---
# The /cycle agent runs INSIDE the live repo, so it can edit a TRACKED file
# outside Pulse's own write-set (e.g. config/alert-translations.json). The scoped
# auto-commit above stages only PULSE_RUNTIME_PATHS, so such an edit is orphaned
# uncommitted — which makes sync_agent_core.sh refuse to swap ("uncommitted
# changes block sync") and agent_core_health_check.py fail its clean-tree check,
# paging Larry every ~30 min until cleared (observed 2026-06-26; PR #728 only
# silenced the alert, it never stopped the stray edit, so it recurred as a hard
# service failure). The live tree must only ever carry machine-owned runtime
# dirt — PULSE_RUNTIME_PATHS (committed above) plus the healer-owned files sync
# tolerates — i.e. SYNC_AUTOCOMMIT_PATHS. Anything else is an out-of-contract
# stray: a governed config/code change belongs in a Forge PR, NOT hand-applied to
# live. Revert it (archiving the diff first so nothing is lost) and surface at
# FYI/digest, never paging. Runs regardless of CYCLE_OK so a stray from a failed
# cycle is cleaned too; gated to main so a feature-branch checkout is never
# touched. Scope: tracked modifications (the confirmed class); untracked strays
# are left for the heal_droplet_git_drift backstop.
if [ -d "$REPO_DIR/.git" ]; then
    cd "$REPO_DIR"
    GUARD_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
    if [ "$GUARD_BRANCH" = "main" ]; then
        STRAY=()
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            _path_in_list "$f" "${SYNC_AUTOCOMMIT_PATHS[@]}" || STRAY+=("$f")
        done < <({ git diff --name-only HEAD 2>/dev/null; git diff --cached --name-only 2>/dev/null; } | sort -u)
        if [ "${#STRAY[@]}" -gt 0 ]; then
            STRAY_TS=$(date -u +%Y%m%dT%H%M%SZ)
            STRAY_DIFF="${LOG_DIR}/stray-cycle-edits-${STRAY_TS}.diff"
            git diff HEAD -- "${STRAY[@]}" > "$STRAY_DIFF" 2>/dev/null || true
            # `git checkout HEAD --` (not bare `--`) so a STAGED stray resets too.
            git checkout HEAD -- "${STRAY[@]}" 2>>"$LOG_FILE" || true
            log "clean-tree-guard: reverted ${#STRAY[@]} stray tracked edit(s) outside Pulse write-set: ${STRAY[*]} (diff archived at ${STRAY_DIFF})"
            timeout 10 python3 "${HOME}/agent-core/scripts/larry_alerts.py" append_alert \
                --source pulse-cycle \
                --severity warning \
                --subject "cycle:stray-tree-edit-reverted" \
                --message "run_cycle.sh reverted out-of-contract edit(s) the /cycle agent made to tracked file(s) outside Pulse's write-set: ${STRAY[*]}. Left uncommitted these would wedge ourliberty-sync + agent-core-health (the 2026-06-26 config/alert-translations.json class). Diff archived at ${STRAY_DIFF}. If the change was intended, route it through a Forge PR (the governed channel)." \
                >/dev/null 2>&1 || true
        fi
    fi
fi

if [ "$CYCLE_OK" = "1" ]; then
    exit 0
else
    exit 1
fi
