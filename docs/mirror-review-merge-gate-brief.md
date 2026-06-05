# Brief: enforce the Mirror-review merge gate at the GitHub level

Audience: Forge. Target repo: `ourliberty-agent-core`. Two builds (A:
notifier status emission; B: detective watcher) plus an operator config step
(branch protection) done by Claude. Mirror reviews each PR. Regression dial 3.

## Why now

The fleet operates as ONE GitHub identity (`Larry-Yatch`, repo admin) with no
branch protection, so anything holding the `gh` token (a stray `/babysit-prs`
loop — confirmed culprit — manual `gh pr merge`, admin) can merge a PR and
bypass Mirror. This session it happened twice (#303 merged despite a
REVISION verdict; #324 merged with no review). GitHub Pro is now active, so
the Mirror gate can be made physically enforceable.

## Design

A required GitHub commit-status check `mirror-review` on `main`, set by the
agent OS ONLY when Mirror passes a PR; branch protection (with
`enforce_admins`) requires it, so no actor — including the admin identity —
can merge without Mirror's pass. Status-check (not required-reviewers)
because everything is one identity and GitHub never counts self-approval.

## Build A — Mirror verdict -> commit status (`scripts/outbox_notifier.py`)

Where the notifier classifies a Mirror verdict marker for a PR (the same spot
that drives auto-merge), POST a commit status to the PR head SHA:
- `REVIEW_PASS`  -> `context=mirror-review, state=success,
  description="Mirror review passed"`. POST this BEFORE the auto-merge step so
  the merge satisfies the required check.
- `REVIEW_REVISION` / `REVIEW_ESCALATE` / `REVIEW_EMERGENCY_HALT` ->
  `context=mirror-review, state=failure, description="<verdict>"` (so
  revision/escalate PRs are blocked from merging — this is exactly the #303
  hole).

Use `gh api repos/{owner}/{repo}/statuses/{headSha} -f state=.. -f
context=mirror-review -f description=..`; get head SHA via
`gh pr view {n} --json headRefOid`. The notifier already has the gh auth + PR
coords. Tolerate gh errors (log, NEVER crash the notifier). Idempotent.
Tests: pass -> success status posted before merge; revision -> failure;
gh-failure tolerated; status targets the correct head SHA.

## Operator step (Claude, AFTER A deploys + is verified live)

Enable branch protection on `main` via `gh api -X PUT
repos/Larry-Yatch/ourliberty-agent-core/branches/main/protection`:
`required_status_checks={strict:false, contexts:["mirror-review"]}`,
`enforce_admins=true`, `allow_force_pushes=false`, `allow_deletions=false`,
`required_pull_request_reviews=null` (single identity — do NOT require
approvals or it deadlocks). Do NOT enable until A is live, or legit
Mirror-passed merges deadlock. Verify: a Mirror-passed PR merges; an
unreviewed PR is blocked.

## Build B — detective watcher (defense-in-depth, runs even with protection on)

New `scripts/heal_unreviewed_merge_detector.py` + a systemd service + timer
(`OnCalendar`, ~every 5 min — OnCalendar not OnUnitActiveSec). Each run: list
PRs merged into `main` since a persisted cursor
(`gh pr list --state merged --base main --search "merged:>=<ts>"`); for each,
verify a Mirror `REVIEW_PASS` exists for that PR/task (chain_events
`review_pass` with that `pr_url`, or the outbox-notifier log). A PR merged
with NO REVIEW_PASS -> emit a Larry alert (ESCALATION) "PR #N merged without
Mirror review (actor=<login>)". Persist the cursor under `state/`. Follow the
existing healer conventions (heartbeat, kill-switch file, fail-safe). Tests:
merged-with-pass -> no alert; merged-without-pass -> alert; cursor advances;
idempotent (no duplicate alerts on re-scan). If the alert is shell/Python
emitted, it MUST pass the alert-translation gate.

## MUST VERIFY (in build)

- The exact notifier function that classifies Mirror markers + performs
  auto-merge — hook the status POST there, before merge.
- The notifier's `gh` token has `repo:status` scope (it already merges PRs as
  Larry-Yatch, so it does — confirm).
- The Larry-alert / `append_alert` emission helper + the alert-translation
  gate (shell-emitted alerts must register a translation).

## Out of scope

Changing Mirror's review logic. The branch-protection config (operator step)
is done by Claude, not Forge.
