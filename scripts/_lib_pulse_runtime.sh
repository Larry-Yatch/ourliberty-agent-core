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
#   - pulse_runtime_fixture_token_regex — the shell-side fixture-pattern guard
#     regex (mirrors scripts/fixture_patterns.py SHELL_FIXTURE_REGEX). Callers
#     use it to refuse auto-commits whose staged diff mentions a fixture-leak
#     task_id (defense-in-depth against the 2026-05-27 cycle hallucination class).
#   - all_modified_in_pulse_runtime_allowlist — true if every tracked file
#     modified vs HEAD is inside PULSE_RUNTIME_PATHS.

PULSE_RUNTIME_PATHS=(
    "runbooks/cycle-journal.md"
    "runbooks/cycle-actions.jsonl"
    "agents/pulse/MEMORY.md"
    "agents/pulse/memory/"
)

pulse_runtime_fixture_token_regex() {
    # Token boundary requires a ", `, or = on the left so we match
    # JSONL ("task_id":"t-fail"), markdown prose (`t-fail`), and KV-style log
    # lines (task_id=t-fail) without tripping on incidental prose. Keep in
    # lock-step with run_cycle.sh's inline copy and with
    # scripts/fixture_patterns.py SHELL_FIXTURE_REGEX.
    echo '["`=](t-|sess-abc-|notify-t-|notify-q-|marker-error-t-|marker-error-opmanual-|task-001([^A-Za-z0-9_-]|$)|task-legacy([^A-Za-z0-9_-]|$)|headless-001([^A-Za-z0-9_-]|$)|opmanual-d35-5b-shipped-note-001([^A-Za-z0-9_-]|$)|pf-ok([^A-Za-z0-9_-]|$)|bad-pf([^A-Za-z0-9_-]|$)|no-preamble([^A-Za-z0-9_-]|$)|no-chat([^A-Za-z0-9_-]|$))'
}

_pulse_runtime_path_allowed() {
    local p="$1"
    local allowed
    for allowed in "${PULSE_RUNTIME_PATHS[@]}"; do
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

# all_modified_in_pulse_runtime_allowlist <repo_dir>
#   Returns 0 iff every tracked file that differs from HEAD (staged or
#   unstaged) is inside PULSE_RUNTIME_PATHS. Returns 1 if any file is outside
#   the allowlist, or if the tree is clean (callers gate on dirt themselves).
all_modified_in_pulse_runtime_allowlist() {
    local repo_dir="$1"
    local files
    files=$(git -C "$repo_dir" diff --name-only HEAD 2>/dev/null)
    if [ -z "$files" ]; then
        return 1
    fi
    local path
    while IFS= read -r path; do
        [ -z "$path" ] && continue
        _pulse_runtime_path_allowed "$path" || return 1
    done <<< "$files"
    return 0
}
