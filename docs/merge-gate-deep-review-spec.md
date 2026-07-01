# Merge Gate — Deep-Review Hold Build Spec

**Status:** Spec for the team to build — 2026-07-01.
**Parent:** general pipeline hardening; sibling of [approval-sync-phase2p1-spec.md](approval-sync-phase2p1-spec.md).
**Born from:** Phase 2 (#781) auto-merged on a Mirror PASS *before* a human `/code-review high` could run, and that deep review then found two confirmed resolve-path blockers Mirror missed. The pipeline needs a durable rule: **changes to the highest-stakes machinery pass Mirror review but do NOT auto-merge — they hold for a human deep review + manual merge.**
**Risk tier:** HIGH — this modifies the merge machinery itself. This PR's own build must be **hand-reviewed** before merge (the gate can't yet protect itself); after it lands, `outbox_notifier.py` is in its own critical-path list, so future changes to it self-hold.

---

## 0. Goal
Enforce the two-review rule for critical-path changes: **Claude runs `/code-review high` FIRST (before handoff) and stamps the PR, the team's Mirror pass is the independent second check, then it auto-merges.** This gate is the **backstop** that makes "Claude reviewed first" un-skippable — it does NOT replace the primary flow.

A **deep-review hold**: when a PASS'd PR touches a configured critical-path fileset (or carries a `deep-review-required` label) **AND lacks a `deep-review-passed` stamp**, the auto-merge path returns a new terminal `held_deep_review` outcome instead of merging, and surfaces it for the missing `/code-review high`. A critical-path PR that WAS reviewed+stamped by Claude sails through Mirror to auto-merge normally. Small and additive — it reuses the existing `held_conflict` seam end-to-end; the merge shell-out path is untouched.

**The stamp:** Claude's `/code-review high` (run before flipping the PR to the team) applies a `deep-review-passed` marker — the simplest form is a PR label `deep-review-passed`, or a `DEEP_REVIEW_PASS sha=<40-hex>` marker mirroring `merge_reviewed_pr.sh`'s `LOCAL_REVIEW_PASS` (SHA-bound so it doesn't survive a new push). The gate checks for it before holding.

## 1. Decisions locked
1. **Two triggers, OR'd:** (a) any changed file matches a glob in a new `config/deep-review-paths.json`; (b) the PR carries a `deep-review-required` label. Either holds. The fileset is the durable default (no one has to remember to label); the label is a manual override for a risky change outside the fileset.
2. **Reuse the `held_conflict` model, do not invent a new surface.** `held_deep_review` is a terminal held outcome (not queued for auto-retry), with a DM + status-line variant, exactly like `held_conflict`.
3. **Manual merge stays the existing desktop path** — `scripts/merge_reviewed_pr.sh <PR>` (stamps `LOCAL_REVIEW_PASS` so `heal_unreviewed_merge_detector` doesn't false-page). The DM hands Larry that exact command.
4. **Config is optional + graceful** — missing/malformed `config/deep-review-paths.json` → code-default fileset; Pulse-Check may tune it (house pattern).

## 2. Changes (all in `scripts/outbox_notifier.py` unless noted)

### Change 1 — the new gate
In `_attempt_auto_merge_with_gates` (~line 8662), **after Gate 1 (overlap blocker) and before the merge fires (~line 8908)**, insert:
```python
if _deep_review_required(repo_coords, pr_number, changed_files):
    _dm_larry_deep_review_hold(pr_url, pr_number, repo_coords, task_id, chat_id, summary)
    log(f'AUTO_MERGE_HELD_DEEP_REVIEW task={task_id} pr={pr_url} agent=forge', 'WARN')
    return {'merge_outcome': 'held_deep_review',
            'merge_reason': 'critical-path change; held for /code-review high',
            'pr_number': pr_number, 'repo_coords': repo_coords}
```
Placed before the freshness/merge block so held-blocker *releases* and sweep-retry callers (same function) also hold. **The WARN log line must NOT contain `outcome=failed`** — `heal_pr_auto_merge` only retries on `outcome=failed`, so a plain WARN correctly leaves the held PR alone.

### Change 2 — the predicate
New `_deep_review_required(repo_coords, pr_number, changed_files, labels) -> bool`: returns True (→ HOLD) when **both**:
1. the PR is critical — any `labels` entry equals `deep-review-required`, OR any path in `changed_files` matches a glob from `_load_deep_review_paths()` (via `fnmatch`); AND
2. the PR is **not** already deep-reviewed — no `deep-review-passed` label (and, if the marker form is used, no `DEEP_REVIEW_PASS sha=<current-head-sha>` marker).

So a critical PR that Claude already `/code-review high`'d + stamped returns False (flows through Mirror → auto-merge); a critical PR with no stamp returns True (held). If `changed_files`/`labels` are None, resolve via the existing `_gh_pr_changed_files` (~line 7694) / a `gh pr view --json labels` read — no extra gh call in the common case since `changed_files` is already threaded into the gate (~line 8772). Fail-conservative: on any error resolving the stamp, treat it as ABSENT (hold) — a false hold costs a manual review; a false pass is what this gate exists to prevent.

### Change 3 — config loader
New `_load_deep_review_paths()` modeled on `_load_auto_merge_watchdog_hours_from_config` (~line 518): read `config/deep-review-paths.json`, return `paths` when `enabled` is true, else the code-default list. Graceful on missing/malformed.

### Change 4 — DM + status-line variants (reuse the seam)
- Add `held_deep_review` to `_REVIEW_PASS_DM_VARIANTS` (~line 1032) and a branch in `_render_review_pass_merge_status_line` (~line 1110), modeled on `held_conflict` (~lines 1083/1137). Honor the invariant (~1121-1136): the word "MERGED" must NOT appear for this outcome.
- Add `held_deep_review` to the `_maybe_dm_larry` suppression set (~line 1301-1316) so the generic notify doesn't double-fire.
- New `_dm_larry_deep_review_hold(...)` modeled on `_dm_larry_rebase_needed` (~line 8129), using `larry_alerts.append_notification` (closes the chain in the originating chat) with `append_alert` fallback. This DM fires on the **backstop** case — a critical-path PR reached auto-merge *without* Claude's deep-review stamp (the pre-handoff `/code-review high` was skipped). Body: "Critical-path PR reached merge without a deep-review stamp — held; needs a `/code-review high` first," + the PR url. It signals the "Claude reviews first" step was missed; the fix is to run the deep review (which stamps, then merges via `merge_reviewed_pr.sh <PR>`), not to blind-merge.

### Change 5 — the config file
Add `config/deep-review-paths.json` (also becomes the code default):
```json
{
  "_comment": "PRs touching these paths pass Mirror review but are HELD for a human /code-review high + manual merge (deep-review hold). Optional; missing/malformed -> code defaults. Tuned by Pulse-Check. Globs matched against gh pr view --json files paths.",
  "enabled": true,
  "paths": [
    "scripts/beacon_approval_handler.py",
    "scripts/decision_*.py",
    "scripts/resolve*.py",
    "scripts/for_larry_*.py",
    "scripts/larry_alerts.py",
    "scripts/chain_event_emit.py",
    "scripts/trust_policy.py",
    "scripts/outbox_notifier.py",
    "config/trust-policy.json"
  ]
}
```
Rationale: the approval/resolve fan-out (Phase 2/2.1 surface) + the trust/merge machinery itself — the highest-stakes self-modification. The dashboard `/api/larry/action` handler lives in `ourliberty-dashboard`; a cross-repo glob (e.g. `app/api/larry/**`) belongs in that repo's own copy of the config (the auto-merge path already handles both repos via `repo_coords`) — note as a follow-up, out of scope here.

### Change 6 — desktop label affordance (optional)
Add a `--deep-review` flag to `scripts/open_pr_for_team.sh` that also applies `--label deep-review-required` (mirrors the `auto-review` label at ~line 36/72), so a human can force the hold on a PR outside the fileset. Manual GitHub labeling works without this.

## 3. Success criteria
- A PASS'd PR whose diff touches a fileset path (e.g. `scripts/decision_resolve.py`) does NOT merge: `_attempt_auto_merge_with_gates` returns `held_deep_review`, one DM fires with the `merge_reviewed_pr.sh <PR>` command, no `outcome=failed` is logged, and no healer auto-retries or reaps it.
- A PASS'd PR with the `deep-review-required` label holds even if it touches no fileset path.
- A PASS'd PR touching none of the fileset and unlabeled merges exactly as today (no behavior change — regression test on the existing merge path).
- Malformed/missing `config/deep-review-paths.json` → the code-default fileset still holds critical-path PRs (graceful-degradation test).
- The status line and DM never say "MERGED" for `held_deep_review` (invariant test).
- `merge_reviewed_pr.sh <PR>` still merges a held PR and stamps `LOCAL_REVIEW_PASS` so `heal_unreviewed_merge_detector` stays quiet (existing behavior, assert unbroken).

## 4. Out of scope
- The dashboard-repo copy of the fileset (cross-repo `/api/larry/**`) — follow-up.
- Any change to the merge shell-out (`_auto_merge_pr`) or the other `held_*` outcomes.
- Auto-launching `/code-review` — the hold routes to Larry's manual `/code-review high`; automating that is a later idea.

## 5. Files
`scripts/outbox_notifier.py` (Changes 1-4), `config/deep-review-paths.json` (Change 5, new), `scripts/open_pr_for_team.sh` (Change 6, optional), tests under `scripts/tests/`.

## 6. Doctrine
Additive only: the merge shell-out and every existing `held_*`/merge outcome are untouched; this inserts one gate that can only turn a would-be merge into a hold, never the reverse. Conservative-fail: any ambiguity in resolving labels/files must **hold**, not merge (a false hold costs a manual merge; a false merge is what this gate exists to prevent). Tests are the enforcement mechanism.

## 7. Preflight
Read this spec + `_attempt_auto_merge_with_gates`, `_dm_larry_rebase_needed`, `_REVIEW_PASS_DM_VARIANTS`, `_render_review_pass_merge_status_line`, and an existing `_load_*_from_config` helper. Confirm the gate insertion point covers the held-release/sweep-retry callers and that the WARN log avoids `outcome=failed`. Emit PROCEED / CLARIFY_REQUEST / REJECT with one marker. Build is a separate dispatch.
