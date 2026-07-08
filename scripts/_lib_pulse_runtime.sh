#!/usr/bin/env bash
# _lib_pulse_runtime.sh — shared facts about the Pulse-owned runtime allowlist.
#
# Sourced by run_cycle.sh (auto-commits the Pulse runtime files after a
# successful cycle) and sync_agent_core.sh (auto-commits + pushes the Pulse
# runtime files when they are the only dirt, so an interactive /cycle no longer
# blocks the next sync — see the 2026-05-28 iter-98 incident, brief
# docs/sync-resilience-and-alert-translation-brief.md).
#
# Two machine-owned runtime classes, handled DIFFERENTLY by sync:
#   - PULSE_RUNTIME_PATHS    — Pulse-owned files sync may auto-commit + push
#                              (iter-98 resilience). Most are rewritten every
#                              cycle; the exception is the .claude/settings.json
#                              session config, which changes only on permission
#                              grants — see the inline note on that entry.
#   - SYNC_EXTRA_RUNTIME_PATHS — captures.json, whose SOLE committer is
#                              heal_missions_card_gc.py. Sync must NOT commit it;
#                              it only TOLERATES the dirt and proceeds to the
#                              ff-pull (the GC healer persists it on its own
#                              tick). Two committers racing on origin/main, plus
#                              sync's hard-reset rollback, opened a data-loss
#                              window (#409 follow-up) — see sync_agent_core.sh.
#
# Single source of truth for:
#   - PULSE_RUNTIME_PATHS / SYNC_EXTRA_RUNTIME_PATHS / SYNC_AUTOCOMMIT_PATHS.
#   - all_modified_in_pulse_runtime_allowlist — every modified file is Pulse.
#   - all_modified_in_sync_autocommit_allowlist — every modified file is in the
#     union (the outer "all dirt is machine-owned" guard).
#   - any_modified_in_pulse_runtime_allowlist — at least one modified file is
#     Pulse (does sync have anything to auto-commit?).
#   - all_modified_in_sync_extra_allowlist — every modified file is a
#     healer-owned extra (captures.json); the set sync tolerates without
#     committing.

PULSE_RUNTIME_PATHS=(
    "runbooks/cycle-journal.md"
    # Overflow chunks from rotate_cycle_journal.py (2026-07-07 gc-overload fix).
    # run_cycle.sh trims cycle-journal.md every cycle and moves older entries
    # here; the chunks must ride the same Pulse-owned auto-commit so the tree
    # returns clean. Chunks are append-only and freeze once full — run_cycle.sh
    # is their sole committer, same as the journal itself.
    "runbooks/journal-archive/"
    "runbooks/cycle-actions.jsonl"
    "agents/pulse/MEMORY.md"
    "agents/pulse/memory/"
    # Pulse's interactive Claude Code session config (allow/deny tool perms).
    # Unlike the entries above it is NOT rewritten every cycle — it only changes
    # when new permissions are granted. Tracked here so sync auto-commits the
    # grant instead of refusing-and-paging on otherwise-clean machine-owned dirt.
    "agents/pulse/.claude/settings.json"
)

# Machine-owned runtime files that have their OWN designated committer, so sync
# must TOLERATE (not commit) their dirt rather than refusing and paging Larry.
# Criteria for adding a path here:
#   1. The file is written exclusively by automation (never hand-edited).
#   2. Writes are atomic (tmp+rename), so a snapshot is never torn.
#   3. Some other automation is its SOLE committer on its own cadence — sync only
#      needs to absorb the race window between write and that committer's tick,
#      WITHOUT becoming a second committer.
# captures.json (2026-06-10): written by the missions ingest endpoint
# (dashboard_api.py) and committed every ~10min by heal_missions_card_gc.py.
# The hourly sync tick can land in that gap and refuse-and-page on a purely
# machine-owned file — the Pulse iter-98 class, different file.
# missions.json (2026-06-17): same shape — written by automation (the missions
# queue drain / dashboard cleanups, never hand-edited; atomic tmp+rename) and
# committed by heal_missions_card_gc.py as the single committer of ANY pending
# missions.json delta on its tick (Contract D). A sync tick landing in the gap
# between a cleanup's write and the healer's commit refused-and-paged — the P1
# incident. Sync now tolerates it, same as captures.json.
#
# IMPORTANT (#409 follow-up): sync does NOT auto-commit these. #409 originally
# made sync a second committer of captures.json; that created a dual-committer
# race on origin/main and, on a failing push, sync's `git reset --hard` reverted
# captures.json on disk and lost ingests written during the push window. Sync now
# leaves these files entirely to their owner: it neither commits nor resets them,
# only tolerates the dirt and proceeds to the ff-pull. The ff-pull is safe
# because the sole committer (the GC healer) commits to THIS working tree first,
# so its captures.json commits are already in local HEAD before origin advances —
# an incoming ff-pull never carries a captures.json change (and git fast-forwards
# cleanly past commits that don't touch a dirty file).
#
# DRIFT GUARD: this array is the source of truth for the SYNC path and MUST stay
# consistent with the canonical config/healer-managed-runtime-paths.json (the
# same allowlist consumed by heal_droplet_git_drift.py and Pulse cycle Check A).
# Equality is enforced by scripts/tests/test_heal_droplet_git_drift.py — keep
# this kept-untouched-on-purpose bash literal in lock-step with that JSON.
SYNC_EXTRA_RUNTIME_PATHS=(
    "agents/beacon/captures.json"
    "agents/beacon/missions.json"
    "agents/beacon/projects.json"
)

# The full machine-owned set: Pulse runtime (sync auto-commits) + healer-owned
# extras (sync tolerates). Used as the outer "all dirt is machine-owned" guard.
SYNC_AUTOCOMMIT_PATHS=(
    "${PULSE_RUNTIME_PATHS[@]}"
    "${SYNC_EXTRA_RUNTIME_PATHS[@]}"
)

# _path_in_list <path> <allowed>...
#   Returns 0 if <path> matches any <allowed> entry. An entry ending in '/' is
#   a directory prefix (matches the dir and anything under it); otherwise it is
#   an exact match.
_path_in_list() {
    local p="$1"
    shift
    local allowed
    for allowed in "$@"; do
        if [[ "$allowed" == */ ]]; then
            if [[ "$p" == "$allowed"* ]]; then
                return 0
            fi
        else
            if [[ "$p" == "$allowed" ]]; then
                return 0
            fi
        fi
    done
    return 1
}

_pulse_runtime_path_allowed() {
    _path_in_list "$1" "${PULSE_RUNTIME_PATHS[@]}"
}

# _all_modified_in <repo_dir> <allowed>...
#   Returns 0 iff every tracked file that differs from HEAD (staged or
#   unstaged) is inside the given allowlist. Returns 1 if any file is outside
#   it, or if the tree is clean (callers gate on dirt themselves).
_all_modified_in() {
    local repo_dir="$1"
    shift
    local files
    files=$(git -C "$repo_dir" diff --name-only HEAD 2>/dev/null)
    if [ -z "$files" ]; then
        return 1
    fi
    local path
    while IFS= read -r path; do
        [ -z "$path" ] && continue
        _path_in_list "$path" "$@" || return 1
    done <<< "$files"
    return 0
}

# all_modified_in_pulse_runtime_allowlist <repo_dir>
#   Returns 0 iff every tracked file that differs from HEAD is inside
#   PULSE_RUNTIME_PATHS.
all_modified_in_pulse_runtime_allowlist() {
    _all_modified_in "$1" "${PULSE_RUNTIME_PATHS[@]}"
}

# all_modified_in_sync_autocommit_allowlist <repo_dir>
#   Returns 0 iff every tracked file that differs from HEAD is inside
#   SYNC_AUTOCOMMIT_PATHS (Pulse runtime + healer-owned extras). The outer
#   "all dirt is machine-owned" guard for sync's runtime handling.
all_modified_in_sync_autocommit_allowlist() {
    _all_modified_in "$1" "${SYNC_AUTOCOMMIT_PATHS[@]}"
}

# all_modified_in_sync_extra_allowlist <repo_dir>
#   Returns 0 iff every tracked file that differs from HEAD is inside
#   SYNC_EXTRA_RUNTIME_PATHS (captures.json). True when the ONLY remaining dirt
#   is healer-owned — the set sync tolerates and proceeds past without
#   committing. Returns 1 on a clean tree (callers gate on dirt themselves).
all_modified_in_sync_extra_allowlist() {
    _all_modified_in "$1" "${SYNC_EXTRA_RUNTIME_PATHS[@]}"
}

# any_modified_in_pulse_runtime_allowlist <repo_dir>
#   Returns 0 if AT LEAST ONE tracked file that differs from HEAD is inside
#   PULSE_RUNTIME_PATHS (i.e. sync has Pulse runtime dirt to auto-commit).
#   Returns 1 on a clean tree or when no modified file is Pulse-owned.
any_modified_in_pulse_runtime_allowlist() {
    local repo_dir="$1"
    local files
    files=$(git -C "$repo_dir" diff --name-only HEAD 2>/dev/null)
    if [ -z "$files" ]; then
        return 1
    fi
    local path
    while IFS= read -r path; do
        [ -z "$path" ] && continue
        _pulse_runtime_path_allowed "$path" && return 0
    done <<< "$files"
    return 1
}
