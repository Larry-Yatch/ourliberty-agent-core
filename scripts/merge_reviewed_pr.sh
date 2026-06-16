#!/usr/bin/env bash
# merge_reviewed_pr.sh — merge a PR from desktop after a local /code-review,
# stamping a LOCAL_REVIEW_PASS marker so heal_unreviewed_merge_detector.py does
# NOT page Larry with a false "merged without Mirror review" alert.
#
# WHY: a PR merged from desktop Claude Code after `/code-review` never enters
# Mirror's droplet review cycle, so it has no AUTO_MERGE evidence in
# outbox-notifier.log — the detector's normal positive signal. Without a marker
# it pages Larry as unreviewed even though it WAS reviewed. This script posts the
# marker (the desktop-path equivalent of Mirror's REVIEW_PASS) just before
# merging. The detector accepts the marker ONLY when its comment author is a
# trusted reviewer listed in config/local-review-reviewers.json — so the gh
# identity running this must be that reviewer (normally Larry-Yatch).
#
# Use this INSTEAD OF a bare `gh pr merge` whenever Claude Code (desktop) merges
# a locally-reviewed PR. If you forget and merge bare, nothing breaks — the
# detector simply pages as before (fail-safe); this only removes that noise.
#
# Run AFTER `/code-review` has passed — this does not review, it only marks+merges.
#
# After merging, it nudges the droplet to pull main immediately (via
# `ourliberty-sync.service`) so a freshly-merged spec/code change is visible to
# the agent team within ~a minute instead of waiting up to an hour for the sync
# timer. Best-effort (a failure never fails the merge; the hourly timer is the
# backstop). Skip with OURLIBERTY_SKIP_DROPLET_SYNC=1; override the host with
# OURLIBERTY_DROPLET_SSH (default larry@134.209.44.80).
#
# Usage:
#   scripts/merge_reviewed_pr.sh <PR_NUMBER> [extra gh pr merge args...]
# Examples:
#   scripts/merge_reviewed_pr.sh 512            # default: --squash --delete-branch
#   scripts/merge_reviewed_pr.sh 512 --merge    # override the merge method
#   scripts/merge_reviewed_pr.sh 512 --squash --admin
set -euo pipefail

REPO="Larry-Yatch/ourliberty-agent-core"

PR="${1:-}"
if [[ -z "$PR" || ! "$PR" =~ ^[0-9]+$ ]]; then
  echo "usage: $0 <PR_NUMBER> [gh pr merge args...]" >&2
  exit 2
fi
shift

# Caller's extra args (may be empty). bash 3.2 (macOS default) errors on
# "${arr[@]}" for an empty array under `set -u`, so use the ${arr[@]+...} guard.
MERGE_ARGS=("$@")

# Default the merge method + branch cleanup to the repo's house style
# (matches heal_pr_auto_merge: `gh pr merge --squash --delete-branch`) unless the
# caller supplied an explicit method.
has_method=false
for a in ${MERGE_ARGS[@]+"${MERGE_ARGS[@]}"}; do
  case "$a" in
    --squash|--merge|--rebase) has_method=true ;;
  esac
done
if [[ "$has_method" == false ]]; then
  MERGE_ARGS=(--squash --delete-branch ${MERGE_ARGS[@]+"${MERGE_ARGS[@]}"})
fi

reviewer="$(gh api user --jq .login)"

# The reviewed head SHA. The detector accepts the marker only if this equals the
# PR's merged headRefOid (binds the evidence to the exact reviewed commit), so it
# is captured NOW, just before merging, and must be a real 40-hex oid.
head="$(gh pr view "$PR" --repo "$REPO" --json headRefOid --jq .headRefOid)"
if [[ ! "$head" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[merge_reviewed_pr] could not resolve head SHA for #${PR} (got '${head}'); aborting" >&2
  exit 1
fi

# The marker LINE must match heal_unreviewed_merge_detector.LOCAL_REVIEW_MARKER_RE
# exactly: `=== LOCAL_REVIEW_PASS sha=<40-hex> ===` on its own line.
body="=== LOCAL_REVIEW_PASS sha=${head} ===
Reviewed locally via Claude Code desktop \`/code-review\` before this merge by ${reviewer}.
This PR did not pass through Mirror's droplet review cycle by design (desktop
merge); the marker line above is the desktop-path equivalent of Mirror's
REVIEW_PASS, bound to the reviewed head SHA."

echo "[merge_reviewed_pr] posting LOCAL_REVIEW_PASS marker on #${PR} (reviewer=${reviewer}, head=${head:0:12})..."
# Post the marker FIRST so it is present the instant the merge lands — if this
# fails, abort rather than merge unmarked (set -e handles the non-zero exit).
# The SHA binding makes a stale marker harmless: if the merge later fails and the
# PR gets new commits, the recorded SHA no longer matches and the detector pages.
gh pr comment "$PR" --repo "$REPO" --body "$body"

echo "[merge_reviewed_pr] merging #${PR}: gh pr merge ${MERGE_ARGS[*]}"
gh pr merge "$PR" --repo "$REPO" ${MERGE_ARGS[@]+"${MERGE_ARGS[@]}"}

echo "[merge_reviewed_pr] done — #${PR} merged with LOCAL_REVIEW_PASS marker."

# Nudge the droplet to pull main NOW so freshly-merged specs/code reach the agent
# team within ~a minute instead of waiting up to an hour for ourliberty-sync.timer
# (the recurring "spec merged but the team can't see it" friction). Best-effort:
# a failure here never fails the merge — the hourly timer is the backstop. The
# `if ssh ...` form keeps `set -e` from aborting on a non-zero ssh.
DROPLET_SSH="${OURLIBERTY_DROPLET_SSH:-larry@134.209.44.80}"
if [[ "${OURLIBERTY_SKIP_DROPLET_SYNC:-0}" != "1" && -n "$DROPLET_SSH" ]]; then
  echo "[merge_reviewed_pr] nudging droplet (${DROPLET_SSH}) to sync main now..."
  if ssh -o ConnectTimeout=10 -o BatchMode=yes "$DROPLET_SSH" 'sudo systemctl start ourliberty-sync.service' 2>/dev/null; then
    echo "[merge_reviewed_pr] droplet synced — main is current for the team."
  else
    echo "[merge_reviewed_pr] WARN: droplet sync nudge failed; hourly timer will catch up (skip with OURLIBERTY_SKIP_DROPLET_SYNC=1)." >&2
  fi
fi
