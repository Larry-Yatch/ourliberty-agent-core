#!/usr/bin/env bash
# _lib_cost_capture.sh — the ONE definition of the costs.jsonl cost row.
#
# Sourced by run_cycle.sh and run_medic.sh, the two shell wrappers around the
# Claude Code CLI in `--print --output-format json` mode. (The spawn and the
# OAuth-token export stay in the WRAPPERS:
# scripts/tests/test_claude_durable_auth_guard.py pins the inference-spawn
# surface and the durable-token bridge PER FILE, so neither may migrate in
# here — not even as a comment the guard's regexes would match.)
# Both wrappers append exactly one JSON line per
# paid invocation to ~/agents/blackboard/costs.jsonl, which is the factory's
# cost/attribution source of truth (scripts/ledger_weekly.py,
# active_tier.rolling_5h_token_volume, scripts/dashboard_api.py, the Ledger
# agent, and Pulse Check X's post-cutover per-model view all read it).
#
# WHY A LIB: the row-building jq program lived as two hand-copied literals, one
# per wrapper. They were byte-identical, so every fix had to be applied twice
# and a missed copy left two attribution rules under one schema with the suite
# green. There is now exactly one copy; scripts/tests/test_cost_row_model_
# attribution.py asserts that structurally.
#
# ---------------------------------------------------------------------------
# MODEL ATTRIBUTION — the `work_model($wm)` selector
# ---------------------------------------------------------------------------
# Claude Code reports EVERY model that ran during an invocation in
# `.modelUsage`, keyed by model id — including a background utility model (a
# haiku, used for chores like title generation) alongside the model that did
# the actual work. Two or more keys is the normal shape.
#
# The selector is a TOTAL function: for any parseable envelope it returns a
# string and never raises, because an evaluation error here would abort the
# whole jq program and take cost_usd, all four token counts, duration, success
# and account down with it (the row is simply never appended, and the wrapper
# still exits 0 — a silent loss that reads downstream as "no burn").
#
# Rules, in order:
#   1. NAME MATCH. If any key starts with the model the wrapper actually passed
#      to `--model`, that key wins. `startswith`, not `==`, because live keys
#      are versioned (`claude-sonnet-4-6-20260101`); the versioned key is
#      recorded verbatim. Ties inside this branch break by sorted key so the
#      answer is deterministic.
#   2. HIGHEST POSITIVE OUTPUT, uniquely. Among entries whose value is an
#      OBJECT and whose `.outputTokens` is a POSITIVE NUMBER, the single
#      maximal one wins. If the maximum is shared, or nothing qualifies, we do
#      NOT pick one — see rule 3.
#   3. FALL BACK to $wm, the dispatched model.
#
# ORDER IS LOAD-BEARING: object-type filter -> positive-numeric filter -> max
# -> uniqueness -> $wm. Moving `max` ahead of the type filter reinstates jq's
# cross-type ordering, where a STRING sorts above every number and
# `{"outputTokens":"16"}` beats `{"outputTokens":3841}`.
#
# Why no positional tie-break: jq's `max_by` returns the LAST maximal entry, so
# resolving a tie by position makes the recorded model depend on CLI key order
# — a well-formed, wrong, unalarmed row that no consumer can detect. A tie is
# DETECTED (`$top|length == 1`) and answered with the dispatched model instead.
#
# Why the positive filter: an absent/renamed/null/zero output count must never
# be a DECIDING value. Without it, a failure-path envelope (the work model
# walled before emitting anything, the utility haiku having burned 16 tokens)
# hands the row to the utility model, and an upstream rename of `outputTokens`
# collapses every entry to a tie and degrades the whole expression to
# positional selection.
#
# DOCUMENTED RESIDUAL — do not "fix" this back to $wm: when the dispatched
# model is ABSENT from .modelUsage entirely and only some other model produced
# positive output, that other model is recorded. It is the only model that
# burned tokens; attributing its burn to a model that never appeared would be
# the larger lie. Pinned by test_cost_row_model_attribution.
#
# ---------------------------------------------------------------------------
# ROW SHAPE
# ---------------------------------------------------------------------------
# Deliberately NOT unified with active_tier.append_cost_row / agent_runner.py,
# which stamp the model they DISPATCHED and whose row has no `success` field.
# Re-pointing the shell path at that writer would silently drop a field the
# wrappers write.

# The cost-row jq program. Single-quoted: every $name below is a jq argument
# bound via --arg / --argjson by capture_cost_row, never a shell expansion.
_COST_ROW_JQ='
def work_model($wm):
    (.modelUsage) as $mu
    | (if ($mu | type) == "object" then ($mu | to_entries) else [] end) as $es
    | ($es | map(select((.value | type) == "object"))) as $objs
    | (if ($wm | length) > 0
       then ($objs | map(select(.key | startswith($wm))))
       else [] end) as $named
    | if ($named | length) > 0
      then ($named | sort_by(.key) | first | .key)
      else
        ($objs | map(select(((.value.outputTokens) | type) == "number"
                            and .value.outputTokens > 0))) as $pos
        | ($pos | map(.value.outputTokens) | max) as $mx
        | ($pos | map(select(.value.outputTokens == $mx))) as $top
        | (if ($top | length) == 1 then $top[0].key else $wm end)
      end;
{
    ts: $ts,
    agent: $agent,
    task_id: ($prefix + ($ts | gsub("[^0-9]"; ""))),
    task_type: $task_type,
    model: (work_model($wm) // $wm),
    account: $acct,
    cost_usd: (.total_cost_usd // .cost_usd // null),
    input_tokens: (.usage.input_tokens // null),
    output_tokens: (.usage.output_tokens // null),
    cache_read: (.usage.cache_read_input_tokens // null),
    cache_creation: (.usage.cache_creation_input_tokens // null),
    duration_sec: (.duration_ms // null | if . then ./1000 else null end),
    success: ($ok == 1),
    source: $src
}
'

# capture_cost_row <out_json> <agent> <task_type> <task_id_prefix> <ok 0|1> \
#                  <dispatch_tier_or_empty> <source> <work_model> <costs_file>
#
# Appends exactly one row to <costs_file>. Best-effort by contract — the
# caller's paid work already happened, so nothing here may abort it.
#
# Returns:
#    0  row appended
#   10  skipped (jq absent, or the captured output file is missing/empty)
#   20  jq failed (the captured output is not parseable JSON — e.g. the
#       wrappers' `2>&1` put a plain-text stderr wall in the file)
capture_cost_row() {
    local out_json="$1"
    local agent="$2"
    local task_type="$3"
    local task_id_prefix="$4"
    local ok="$5"
    local dispatch_tier="$6"
    local src="$7"
    local work_model="$8"
    local costs_file="$9"

    mkdir -p "$(dirname "$costs_file")" 2>/dev/null || true

    command -v jq >/dev/null 2>&1 || return 10
    [ -s "$out_json" ] || return 10

    # Account attribution: the pool-selected tier when the pool dispatched this
    # run (so burn is attributed to the account that actually ran, feeding
    # rolling_5h_token_volume / near_cap). On the legacy fallback path, read
    # active-tier.json; tier1 when missing/malformed.
    local account_tier="$dispatch_tier"
    if [ -z "$account_tier" ]; then
        local active_tier_file="${HOME}/agents/blackboard/active-tier.json"
        account_tier="$(jq -r '.tier // "tier1"' "$active_tier_file" 2>/dev/null || echo tier1)"
        if [ -z "$account_tier" ] || [ "$account_tier" = "null" ]; then
            account_tier="tier1"
        fi
    fi

    case "$ok" in
        0|1) ;;
        *) ok=0 ;;
    esac

    local cost_line
    if cost_line=$(jq -c \
            --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S%z)" \
            --argjson ok "$ok" \
            --arg acct "$account_tier" \
            --arg agent "$agent" \
            --arg task_type "$task_type" \
            --arg prefix "$task_id_prefix" \
            --arg src "$src" \
            --arg wm "$work_model" \
            "$_COST_ROW_JQ" "$out_json" 2>/dev/null); then
        printf '%s\n' "$cost_line" >> "$costs_file"
        return 0
    fi
    return 20
}
