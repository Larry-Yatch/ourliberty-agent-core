# Spec: heal_pipeline_stall noise reduction (episode-dedup + merge-truth gate + laptop-PR suppression)

**Status:** ready to build (single PR).
**Owner agent:** Forge build, Mirror review.
**Priority:** Phase 2 item 1 of the pipeline-redesign — the highest-leverage operator-noise reduction.

## Why (measured)

`heal_pipeline_stall` is the single largest source of operator-facing false alerts: ~42% of everything that escalates to Larry, and an estimated ~85% of those are false. The dominant pattern is **a slow-but-healthy PR re-alerting hourly** while it traverses the routing/review path, then merging fine. Concrete (live, 2026-06-23): `unrouted-pr:PR#86` fired **8 times** between 05:15 and 12:49, then PR #86 **merged cleanly at 13:00**. Same shape on #646, #713, #634.

Root cause is mechanical: `ALERT_DEDUP_HOURS = 1` (`scripts/heal_pipeline_stall.py:209`) with a stable per-PR key (`unrouted_open_pr:{repo}:{number}`). A condition that legitimately persists for hours (a slow but progressing PR) therefore re-DMs once per hour. The detector also fires on PRs it cannot see are being handled (laptop-authored `claude/`-branch / labeled PRs whose dispatched review uses a `pr-<repo>-<num>` task_id that the branch-matching suppressors don't recognize).

## Goal

Cut `heal_pipeline_stall` operator escalations to roughly one alert per genuine stuck-PR **episode**, and zero alerts for PRs that are actually progressing or already terminal — without weakening detection of a real, novel stall. Stay within the existing healer architecture (build on the #719 shared-live-state and the existing `should_alert`/`record_alert` cooldown + `gh pr view` reconciliation; do not rebuild them).

## Non-goals
- Do NOT touch the recovery actions (the checks that auto-recover stay as-is); this is about the *alerting* side.
- Do NOT remove any check. Reduce false/duplicate firing, don't blind a real stall.
- Notifier-layer auto-retraction (resolve_alert adoption) is a SEPARATE Phase 2 item — out of scope here, but keep keys compatible so a later retraction can target them.

## Required behaviors

### 1. Episode-dedup, not hourly re-DM (the biggest lever)
For the per-PR "slow PR" checks — at minimum `check_unrouted_open_prs`, `check_forge_built_no_pr`, `check_pr_no_mirror_dispatch`, `check_mirror_pass_unmerged` — a given (check, PR) condition must alert **once per episode**, not once per `ALERT_DEDUP_HOURS`.
- Replace the flat 1h re-DM window for these checks with episode semantics: fire on condition **onset**, then stay silent while the same condition persists for the same PR. Re-alert only after a long backstop interval (config default 24h) OR after the condition cleared and recurred.
- Implement via the existing per-key state (`should_alert`/`record_alert`) — e.g. a per-check `re_dm_hours` (default 24 for these slow-PR checks; keep 1h only where a tight loop genuinely warrants it). Make the per-check window a value in `config/pipeline-stall-rules.json` (new, optional; sane defaults in code) so Pulse-Check can tune it later — same pattern as `config/review-reaper-rules.json`.

### 2. GitHub merge-truth gate at fire time
Before emitting any per-PR stall alert, confirm the PR is genuinely non-terminal via `gh pr view <pr> --json state` (reuse the existing gh helpers / the #719 live-state). Suppress (do not DM) if the PR is `MERGED` or `CLOSED`. This kills the "alarmed then merged" tail. Cache/batch the lookups so this doesn't add N serial `gh` calls per tick (one batched query, or reuse the open-PR list already fetched).

### 3. Recognize in-progress handling for laptop-authored PRs
The unrouted/no-dispatch suppressors currently recognize only `forge/` and `larry/` branch prefixes (`_task_id_from_branch`, ~`:554`); a laptop PR on a `claude/` branch (or labeled `fix|feat|chore`) whose review was dispatched under task_id `pr-<repo>-<num>` escapes all suppressors and false-alerts even while its review is actively in progress.
- Extend `_task_id_from_branch` (and the resolution-signal / active-session suppressors that gate on `branch_task`) to recognize the `claude/` prefix and the `pr-<repo>-<num>` dispatch task_id form, so a dispatched-or-in-progress review on a laptop PR suppresses the unrouted/no-dispatch alert exactly as it does for a `forge/` PR.

## Acceptance criteria (tests, unittest — no pytest)
Add/extend `scripts/tests/test_heal_pipeline_stall*.py`:
1. **Episode-dedup:** a PR meeting an unrouted/forge-no-pr condition across 8 consecutive hourly ticks produces **exactly 1** alert (not 8); a second alert only after the 24h backstop. A *different* PR still alerts independently.
2. **Merge-truth gate:** a PR whose `gh pr view` reports `MERGED` (mock gh) produces **0** alerts from these checks, even if the log-derived condition still looks unrouted.
3. **Laptop-PR suppression:** a `claude/`-branch (and a `fix:`-labeled) PR with a dispatched `pr-<repo>-<num>` review in progress produces **0** unrouted/no-dispatch alerts; the same PR with NO dispatch still alerts once (detection preserved).
4. **No-regression:** a genuinely novel stall (a forge/ PR, open, non-terminal, no dispatch, past the age floor) still alerts exactly once.
5. Config defaults load when `config/pipeline-stall-rules.json` is absent; per-check window override is read when present.

## Mirror review focus
Confirm: (a) detection of a REAL stall is preserved (criterion 4) — the change only suppresses duplicate/terminal/in-progress noise, never a first genuine alert; (b) the gh merge-truth gate degrades safely (a PR whose state can't be read is treated as non-terminal = still alertable, never silently dropped); (c) no added unbounded/serial `gh` fan-out per tick (batched/reused); (d) alert keys stay stable+compatible so a future resolve_alert retraction can target them.

## Validation reference
Live evidence: `unrouted-pr:PR#86` 8 fires 05:15→12:49 (2026-06-23), merged 13:00; `ALERT_DEDUP_HOURS = 1` at `scripts/heal_pipeline_stall.py:209`; key `unrouted_open_pr:{repo}:{number}` (stable). Expected post-fix: that episode = 1 alert, and 0 if #86 had already merged.
