#!/usr/bin/env bash
# _lib_pulse_runtime.sh — shared facts about the Pulse-owned runtime allowlist.
#
# Sourced by run_cycle.sh (auto-commits these after a successful cycle) and
# sync_agent_core.sh (auto-commits + pushes them when they are the only dirt,
# so an interactive /cycle no longer blocks the next sync — see the 2026-05-28
# iter-98 incident, brief docs/sync-resilience-and-alert-translation-brief.md).
#
# Single source of truth for:
#   - PULSE_RUNTIME_PATHS  — the four paths/prefixes Pulse rewrites every cycle.
#   - SYNC_AUTOCOMMIT_PATHS — the broader set sync (only) may auto-commit+push:
#     Pulse runtime + other machine-owned, atomically-written runtime files
#     that other automation commits on its own cadence.
#   - all_modified_in_pulse_runtime_allowlist — true if every tracked file
#     modified vs HEAD is inside PULSE_RUNTIME_PATHS.
#   - all_modified_in_sync_autocommit_allowlist — same, against
#     SYNC_AUTOCOMMIT_PATHS.

PULSE_RUNTIME_PATHS=(
    "runbooks/cycle-journal.md"
    "runbooks/cycle-actions.jsonl"
    "agents/pulse/MEMORY.md"
    "agents/pulse/memory/"
)

# Machine-owned runtime files that are NOT Pulse-owned but are still safe for
# sync to auto-commit+push when they are the only dirt, instead of refusing and
# paging Larry. Criteria for adding a path here:
#   1. The file is written exclusively by automation (never hand-edited).
#   2. Writes are atomic (tmp+rename), so a snapshot is never torn.
#   3. Some other automation already commits it on its own cadence — sync only
#      needs to absorb the race window between write and that committer's tick.
# captures.json (2026-06-10): written by the missions ingest endpoint
# (dashboard_api.py) and committed every ~10min by heal_missions_card_gc.py.
# The hourly sync tick can land in that gap and refuse-and-page on a purely
# machine-owned file — exactly the Pulse iter-98 class, different file.
SYNC_EXTRA_RUNTIME_PATHS=(
    "agents/beacon/captures.json"
)

# The full set sync may auto-commit: Pulse runtime + sync-only extras.
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
#   SYNC_AUTOCOMMIT_PATHS (Pulse runtime + sync-only machine-owned extras).
all_modified_in_sync_autocommit_allowlist() {
    _all_modified_in "$1" "${SYNC_AUTOCOMMIT_PATHS[@]}"
}
