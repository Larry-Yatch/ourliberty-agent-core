#!/usr/bin/env bash
# _lib_push_with_rebase.sh — push to a tracked remote with rebase fallback.
#
# Sourced by callers that auto-commit to a long-running branch (run_cycle.sh,
# run_ledger.sh). Defines: push_with_rebase, soft_validate_main_before_push.
#
# The bug it addresses: when a long-running agent auto-commits to a tracked
# branch (e.g. Pulse's 4h cycle committing to main), an interactive PR merge
# on origin during the same interval makes the next push non-fast-forward.
# A bare `git push` returns non-zero and the commit lingers locally; the next
# cycle commits AGAIN and pushes also fail, compounding until manual recovery.
# cycle.log showed 6 consecutive failed pushes 2026-05-17/18 with the
# signature `failed to push some refs ... use 'git pull' before pushing again`.
#
# Behavior: try push; on failure try `git pull --rebase --autostash` and retry
# the push; on rebase failure abort cleanly. Each terminal path writes one
# log line so cycle.log shows what happened.

# soft_validate_main_before_push <branch> <log_file>
#   Soft (alert-only, NEVER-blocking) deploy gate on the push path.
#
#   Why this exists: the hourly sync-time gate (sync_agent_core.sh ->
#   validate_agent_core.py) only runs when local HEAD is BEHIND origin/main.
#   The routine commit path, though, is Pulse / Ledger auto-committing locally
#   and pushing through THIS helper — which never passes the sync-time gate.
#   So a bad commit (missing agent files, a leaked secret) would reach
#   origin/main, and from there the live runtime, completely un-inspected.
#
#   This runs the SAME validator at push time. If it FAILS, we emit one Larry
#   alert (deduped by failure fingerprint) and then PUSH ANYWAY. It must never
#   block: a validator false-positive freezing the self-healing cycle is worse
#   than the bad commit it would catch. Promoting this to a hard block is a
#   deliberate, separately-tested follow-up — not a flag flip here.
#
#   Knobs:
#     PUSH_SOFT_VALIDATE=0  → disable the soft gate entirely (default: on)
#
#   State (failure fingerprint, for once-per-distinct-failure alerting) lives
#   under ${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/state/ — OUTSIDE the repo, so
#   the gate never dirties the working tree. Always returns 0.
soft_validate_main_before_push() {
    local branch="$1"
    local log_file="$2"

    [ "${PUSH_SOFT_VALIDATE:-1}" = "1" ] || return 0
    [ "$branch" = "main" ] || return 0

    local lib_dir validator alerts_cli repo_dir
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 0
    validator="${lib_dir}/validate_agent_core.py"
    alerts_cli="${lib_dir}/larry_alerts.py"
    [ -f "$validator" ] || return 0
    repo_dir="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0
    [ -n "$repo_dir" ] || return 0

    local ts err_file rc
    ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"
    err_file="$(mktemp 2>/dev/null)" || return 0
    timeout 30 python3 "$validator" --repo-dir "$repo_dir" >/dev/null 2>"$err_file"
    rc=$?

    local state_dir fp_file
    state_dir="${OURLIBERTY_AGENTS_ROOT:-$HOME/agents}/state"
    fp_file="${state_dir}/push-validate-soft.fp"

    if [ "$rc" -eq 0 ]; then
        # Clean: drop any stale failure fingerprint so a future regression
        # re-alerts rather than being suppressed as a duplicate.
        rm -f "$fp_file" 2>/dev/null || true
        rm -f "$err_file" 2>/dev/null || true
        return 0
    fi

    # Validator failed. Fingerprint the ERROR lines only (sorted, no
    # timestamps) so we alert once per DISTINCT failure, not once per push.
    local errors fp last_fp head_sha msg
    errors="$(grep '^  ERROR:' "$err_file" 2>/dev/null | sed 's/^  ERROR: //' | sort)"
    [ -n "$errors" ] || errors="validator exited ${rc} with no parseable ERROR lines"
    fp="$(printf '%s' "$errors" | sha256sum | cut -d' ' -f1)"
    last_fp="$(cat "$fp_file" 2>/dev/null || true)"

    echo "[$ts] soft_validate: validate_agent_core.py FAILED (exit ${rc}) pre-push to ${branch}; alerting Larry, push NOT blocked" >> "$log_file"

    if [ "$fp" != "$last_fp" ]; then
        mkdir -p "$state_dir" 2>/dev/null || true
        printf '%s' "$fp" > "$fp_file" 2>/dev/null || true
        if [ -f "$alerts_cli" ]; then
            head_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
            msg="Pre-push validate_agent_core.py FAILED for HEAD ${head_sha} about to push to origin/main (SOFT gate — push was NOT blocked). Errors: ${errors//$'\n'/ | }. Investigate: cd ~/agent-core && python3 scripts/validate_agent_core.py"
            timeout 10 python3 "$alerts_cli" append_alert \
                --source push-soft-gate \
                --severity warning \
                --subject "push-soft-validate-failed:${head_sha}" \
                --message "$msg" >/dev/null 2>&1 || true
        fi
    else
        echo "[$ts] soft_validate: same failure fingerprint as last alert; suppressing duplicate" >> "$log_file"
    fi

    rm -f "$err_file" 2>/dev/null || true
    return 0
}

# push_with_rebase <remote> <branch> <log_file>
#   Returns 0 if push (or rebase+push) succeeded, non-zero otherwise.
#   Writes one log line per terminal outcome.
push_with_rebase() {
    local remote="$1"
    local branch="$2"
    local log_file="$3"
    local ts
    ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"

    # Soft deploy gate: inspect what's about to ship and alert on failure.
    # Never blocks the push (see function header).
    soft_validate_main_before_push "$branch" "$log_file"

    if git push -q "$remote" "$branch" 2>>"$log_file"; then
        echo "[$ts] push_with_rebase: pushed to $remote/$branch" >> "$log_file"
        return 0
    fi

    echo "[$ts] push_with_rebase: push refused (likely non-FF); attempting pull --rebase --autostash" >> "$log_file"
    if git pull --rebase --autostash -q "$remote" "$branch" 2>>"$log_file"; then
        if git push -q "$remote" "$branch" 2>>"$log_file"; then
            echo "[$ts] push_with_rebase: rebased and pushed to $remote/$branch" >> "$log_file"
            return 0
        fi
        echo "[$ts] push_with_rebase: retry-push failed after rebase; commit retained locally" >> "$log_file"
        return 1
    fi

    echo "[$ts] push_with_rebase: rebase failed (conflicts?); aborting; commit retained locally" >> "$log_file"
    git rebase --abort 2>>"$log_file" || true
    return 1
}
