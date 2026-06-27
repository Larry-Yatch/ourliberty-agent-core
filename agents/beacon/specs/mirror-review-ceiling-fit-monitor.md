# Spec: Mirror review-ceiling fit-monitor

**Status:** ready to build (single PR).
**Owner agent:** Forge build, Mirror review.
**Context:** PR #734 added a hard, harness-enforced wall-clock ceiling on every Mirror review session — `agent_runner.REVIEW_SESSION_CEILING_SECONDS` (default 2100s/35min, env `OL_REVIEW_SESSION_CEILING_SECONDS`, `<=0` disables), applied in `run_claude`'s poll loop via `review_session_effective_timeout()`. A review that exceeds it is killed and `outbox_notifier` synthesizes a `review_escalate` with reason text containing `review_session_timeout`. The 35-min value was validated once against 147 historical reviews (p99 = 30.5 min). This spec adds the **durable feedback loop** so the value is known to stay right over time instead of being a one-time guess.

## Goal

A read-only, self-firing monitor that periodically answers "is the review ceiling still the right length?" and emits a low-severity digest with a recommended value. **Observe + report only in this PR — no auto-tuning of the ceiling** (that is a deliberate follow-up once the recommendation is trusted).

## Non-goals
- Do NOT change `agent_runner` or the ceiling enforcement.
- Do NOT auto-mutate the ceiling or any config the ceiling reads. Recommendation is reported, not applied.
- Do NOT page Larry (route the digest as a non-escalating informational alert, never `escalate`).

## What to build

A new script `scripts/heal_review_ceiling_fit.py` (naming follows the heal_*/monitor family; it is a reporter, not a recoverer — it never mutates pipeline state) plus a systemd timer to run it weekly.

### Inputs (all read-only)
1. **Review durations** — `~/agents/blackboard/costs.jsonl` (+ any `~/agents/blackboard/archive/costs*.jsonl`). Filter to `agent == "mirror"` AND `task_id` starting with `pr-` or `review` (PR reviews — the thing the ceiling bounds). Field: `duration_sec`.
2. **Ceiling firings** — sessions the ceiling killed. Source of truth: the chain_events table (`event_type == 'review_escalate'`) whose payload/reason contains `review_session_timeout`, OR the notifier log line `REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED`. Reuse the existing chain_events read helpers if present; fall back to the notifier log. Each firing carries a `task_id` and (where available) `pr_url`.
3. **GitHub truth for false-kill detection** — for each fired PR, `gh pr view <pr_url> --json state,mergedAt` to learn whether it later merged.

### Computation (over a configurable window, default last 30 days)
- Duration distribution: count, p50, p90, p95, p99, max.
- **Headroom**: `ceiling - p95` and `ceiling - p99`. Flag `HEADROOM_LOW` if p95 ≥ ceiling × 0.8 (the ceiling is creeping into the healthy population).
- **Firing rate**: number of `review_session_timeout` firings in the window, and as a fraction of total reviews.
- **False-kills**: of the fired PRs, how many later reached `state == MERGED`. A fired-then-merged PR is a likely false-kill (the review was killed but the PR was fine). Flag `FALSE_KILL` if ≥1.
- **Recommended ceiling**: `max(round_up_to_5min(p99 × 1.25), current_ceiling_if_no_signal)`. If `HEADROOM_LOW` or `FALSE_KILL`, recommend raising to that value; otherwise recommend "no change — ceiling has N min headroom over p99."

### Output
- One informational digest via the existing alert sink (`larry_alerts.append_alert` or the project's standard reporter) with `route` set to a non-escalating value (`digest`/`closure`-style, NEVER `escalate`) and a clear `source` like `review-ceiling-fit`. Body: the distribution, headroom, firing count, false-kill count, and the recommendation line. Mirror the conservative, low-noise posture of the other weekly reporters.
- If nothing notable (no firings, healthy headroom), still emit a terse "ceiling OK, p99=<x>min, headroom=<y>min" so the absence of a problem is itself observable (don't go silent).

### Config
- Read the window length and the headroom/firing thresholds from a small `config/review-ceiling-fit-rules.json` (window_days, headroom_low_ratio, recommend_multiplier) so Pulse-Check can tune them later — same pattern as `config/review-reaper-rules.json`. Ship sane defaults in code if the file is absent.

### Systemd
- Add `systemd/ourliberty-heal-review-ceiling-fit.{service,timer}` (weekly, e.g. `OnCalendar=Mon 09:00`), matching the install conventions of the sibling heal_* timers. Note in the PR description that the unit needs the standard install step (install-drift is alert-only).

## Tests (unittest, no pytest)
- `scripts/tests/test_heal_review_ceiling_fit.py`:
  - duration parsing + percentile math on a fixture costs.jsonl (incl. mixed agents/task_ids — only mirror PR-reviews counted).
  - HEADROOM_LOW fires when p95 ≥ ceiling × 0.8; not otherwise.
  - false-kill detection: a fired PR that `gh` reports MERGED is flagged (mock the gh call).
  - the digest is routed non-escalating (assert route != escalate).
  - recommendation math: no-signal → "no change"; low-headroom → raise to round_up(p99 × 1.25).

## Mirror review focus
Confirm: (a) the monitor is strictly read-only (never mutates the ceiling, costs.jsonl, chain_events, or any pipeline state); (b) it cannot page Larry (no escalate route); (c) percentile math is correct on small/empty samples (no div-by-zero, empty-window → terse OK digest, not a crash); (d) gh failures degrade gracefully (a PR whose state can't be read is "unknown", not a false-kill).

## Validation reference (for the reviewer)
The one-shot query that validated 35 min: filter costs.jsonl to mirror pr-/review- tasks, sort durations, report p50/p90/p95/p99/max and counts over {1500,1800,2100,2700,3600}s. Result on 2026-06-27: n=147, p99=1830s, only the single 14577s wedge exceeded 2100s. The monitor automates exactly this, adds the firing + false-kill signals, and emits it weekly.
