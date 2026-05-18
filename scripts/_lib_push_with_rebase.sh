#!/usr/bin/env bash
# _lib_push_with_rebase.sh — push to a tracked remote with rebase fallback.
#
# Sourced by callers that auto-commit to a long-running branch (run_cycle.sh,
# run_ledger.sh). Defines one function: push_with_rebase.
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

# push_with_rebase <remote> <branch> <log_file>
#   Returns 0 if push (or rebase+push) succeeded, non-zero otherwise.
#   Writes one log line per terminal outcome.
push_with_rebase() {
    local remote="$1"
    local branch="$2"
    local log_file="$3"
    local ts
    ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"

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
