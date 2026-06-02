# Check X — Chain-Quality Regression Watch (brief)

## Why
We rolled Opus 4.8 to the Forge/Mirror auto-merge chain on 2026-06-01 (PR #233;
Beacon piloted 2026-05-30). A same-family model bump won't show in routine chat —
it shows on hard build/review tasks. We need an OBJECTIVE early-warning that the
chain's quality has regressed since the cutover, instead of eyeballing it. Build
this as a general chain-quality regression detector (re-point the baseline for any
future model/prompt change), motivated by but not hard-coded to 4.8.

## Pattern to follow (do not invent — match these)
- `scripts/pulse_check_viii.py` — cadence (weekly, fires from /cycle Mondays),
  proposal-artifact + DM shape, same-week sentinel idempotency, `larry_alerts.append_alert`
  DM, NEVER edits config, human-approval flow. Copy this skeleton.
- `scripts/pulse_check_iv.py` — the `chain_events` Supabase read pattern:
  `_connect_supabase()` (lazy import of the SDK), `fetch_*_from_supabase(client)` doing
  `client.table('chain_events').select(...)`, `--from-json` test path that bypasses Supabase.
- Deterministic, stdlib-only core, no LLM calls. Pure analysis core unit-testable
  via synthetic events (`--from-json`).

## Data sources
1. Supabase `chain_events` (event_type, ts, task_id, payload). Relevant event_types
   (from `chain_event_shipper.KNOWN_EVENT_TYPES`): `preflight_proceed`,
   `preflight_clarify`, `preflight_reject`, `clarify_request`, `clarify_response`,
   `review_request`, `marker_emit`, `escalation`, `auto_merge`.
2. Local `~/agents/blackboard/costs.jsonl` — keys: `agent`, `model`, `task_type`,
   `attempts`, `task_id`, `ts`, `cost_usd`, etc. Use for model-era partitioning
   (filter by `model` substring `claude-opus-4-7` vs `claude-opus-4-8`, or by `ts`
   vs cutover) and for counting build/revision dispatches per `task_id`.

Mirror review OUTCOME (PASS / REVISION / ESCALATE) representation — RESOLVED by the
follow-up `check-x-verdict-emission` PR (see `docs/check-x-verdict-emission-brief.md`).
`outbox_notifier.py` now push-emits dedicated `review_pass` / `review_revision` /
`review_escalate` chain_events at the verdict-classification site (mapped by routed
intent, so an auto-promoted / budget-exhausted REVISION is recorded as `review_escalate`).
Dedicated types were chosen over reusing `auto_merge` so a PASS is recorded at the
verdict moment, not at actual merge (a PASS can sit in the auto-merge queue behind a
blocker). Forge preflight outcomes are likewise emitted as `preflight_proceed` /
`preflight_clarify` / `preflight_reject`.

HONEST LIMITATION (non-retroactive): verdict emission started at the merge of the
`check-x-verdict-emission` PR. The baseline window for the CURRENT 4.8 cutover (before
2026-06-01) has NO verdict events, so the verdict-mix thresholds CANNOT read the 4.8
transition retroactively — they report `insufficient_signal` for 4.8 and activate for
FUTURE cutovers once verdict history exists on both sides of `cutover_date`. The
clarify/revision thresholds remain the 4.8 read (they have pre-cutover history).

## Metrics (computed per window, Forge/Mirror dispatches only)
- Forge preflight CLARIFY rate = preflight_clarify / (proceed + clarify + reject)
- avg clarify rounds per task = clarify_request count / distinct task_id
- avg revision rounds per task = build/revision dispatches per task_id (from
  costs.jsonl `task_type`, or REVISION markers — whichever is cleaner; document choice)
- Mirror outcome mix: PASS rate, REVISION rate, ESCALATE rate

## Comparison + firing
- Baseline window = the 4 weeks immediately BEFORE the cutover date.
- Trailing window = most recent 4 weeks (configurable; default 28d).
- New config block `check_x_regression` in `config/agent-models.json` (additive,
  top-level, with a `_note` like the other blocks):
  - `cutover_date` (default `"2026-06-01"`)
  - `window_days` (default 28)
  - `min_tasks_per_window` (default 8) — below this either window → `insufficient_signal`,
    no DM, no proposal (log silently). Beacon/Forge/Mirror volume is modest; do NOT
    cry wolf on thin data.
  - thresholds (relative unless noted): `clarify_rate_rel_increase` (0.5),
    `revision_rounds_rel_increase` (0.5), `escalate_rate_abs_increase` (0.10, absolute),
    `pass_rate_abs_drop` (0.15, absolute). All four ACTIVE as of the
    `check-x-verdict-emission` PR. The verdict-mix pair (`escalate_rate_abs_increase`,
    `pass_rate_abs_drop`) is additionally gated on `min_tasks_per_window` Mirror
    verdicts per window → `insufficient_signal` when below the floor.
- If ANY threshold breaches → outcome `regression_suspected`. Else `none`.
- Drop fixture task_ids via `fixture_patterns.is_fixture_task_id` BEFORE counting
  (same as Check VIII).

## Outputs
- Artifact `~/agents/blackboard/pulse-check-x-proposals/check-x-<monday>.json`
  (full metric table both windows + outcome + which thresholds breached). Doubles
  as the same-week sentinel (idempotent re-run within the ISO week).
- DM ONLY when `regression_suspected`, via `larry_alerts.append_alert`
  `source='pulse-check-x'`, severity `warning`. Plain-language, CEO-readable
  (Larry sees outcomes, not jargon): name the metric, show before -> after numbers,
  state explicitly it is CORRELATIONAL (cutover coincides, not proven cause),
  suggested action = review the recent Forge/Mirror PRs and, if confirmed, revert
  the affected agent to `claude-opus-4-7` (flip its routing value in
  `config/agent-models.json` + restart inbox-watcher — note inbox-watcher caches
  config at startup, `inbox_watcher.py:778`). No auto-action.

## Scheduling / registration
- Fire from `/cycle` on Mondays alongside Checks I / IV / VIII. Wire into
  `runbooks/cycle-prompt.md` and whatever check registry/sequence the cycle uses
  (audit how IV/VIII are invoked and match exactly).

## Tests
- Pure-core unit tests, stdlib only, via `--from-json` synthetic events + a synthetic
  costs.jsonl fixture. Cover: clean (none), each threshold breach, insufficient_signal,
  fixture-id exclusion. Follow test-isolation discipline — env-overridable paths
  (`OURLIBERTY_AGENTS_ROOT`), reserve `zz-fixture-` task_ids, never write to prod
  blackboard/log paths in tests.

## Constraints
- No LLM calls. Read-only against chain_events + costs.jsonl. supabase SDK lazy-imported.
- Config block is additive; existing blocks untouched. Text-only (no emoji).
- Dial-3 scope: this is observability only; it proposes, Larry disposes.
