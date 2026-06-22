#!/usr/bin/env bash
# open_pr_for_team.sh — open a PR already tagged `auto-review` so the agent team
# (Mirror) picks it up, reviews it, and auto-merges on PASS without a human step.
#
# WHY: heal_undispatched_pr_review.py routes a Mirror review for any open PR that
# is (a) a Forge build PR, or (b) NON-DRAFT and carries the `auto-review` label.
# The label is the opt-in marker because nothing else distinguishes a PR Larry
# wants the team to take from an agent-authored PR: the agents commit as Larry's
# own GitHub identity AND use the same branch prefixes (`fix/`, `feat/`, …). The
# label is only ever applied on the DESKTOP side (here) — the droplet agents never
# add it — so a labeled PR is unambiguously "cleared for the team."
#
# Use this INSTEAD OF a bare `gh pr create` whenever Claude Code (desktop) opens a
# PR for Larry that should flow through the team's review+merge. If you forget and
# create bare, nothing breaks — the PR simply sits unreviewed (the pre-existing
# behavior) until labeled by hand on github.com (PR page → Labels → auto-review).
#
# Still iterating? Pass --draft. A draft labeled PR is left alone (the safety
# valve) until you mark it ready-for-review — then the team takes it.
#
# Idempotently creates the `auto-review` label if the repo doesn't have it yet.
#
# Usage:
#   scripts/open_pr_for_team.sh --title "<t>" --body "<b>" [gh pr create args...]
#   scripts/open_pr_for_team.sh --title "<t>" --body-file body.md --draft
# All extra args pass through to `gh pr create` verbatim (e.g. --base, --head).
set -euo pipefail

REPO="Larry-Yatch/ourliberty-agent-core"
AUTO_REVIEW_LABEL="auto-review"

# Ensure the opt-in label exists (no-op if already present). Color/description are
# only set on first creation; `|| true` keeps a benign "already exists" non-fatal.
if ! gh label list --repo "$REPO" --limit 200 \
      --json name --jq '.[].name' 2>/dev/null | grep -qx "$AUTO_REVIEW_LABEL"; then
  echo "[open_pr_for_team] creating '${AUTO_REVIEW_LABEL}' label on ${REPO}..."
  gh label create "$AUTO_REVIEW_LABEL" --repo "$REPO" \
    --color "0e8a16" \
    --description "Cleared for the agent team: Mirror reviews + auto-merges on PASS" \
    || true
fi

# Default base to main unless the caller supplied a base (long or gh short form).
ARGS=("$@")
has_base=false
for a in ${ARGS[@]+"${ARGS[@]}"}; do
  case "$a" in --base|--base=*|-B) has_base=true ;; esac
done
if [[ "$has_base" == false ]]; then
  ARGS=(--base main ${ARGS[@]+"${ARGS[@]}"})
fi

echo "[open_pr_for_team] gh pr create --repo ${REPO} --label ${AUTO_REVIEW_LABEL} ${ARGS[*]}"
gh pr create --repo "$REPO" --label "$AUTO_REVIEW_LABEL" ${ARGS[@]+"${ARGS[@]}"}

echo "[open_pr_for_team] done — PR opened with '${AUTO_REVIEW_LABEL}'. The team will"
echo "[open_pr_for_team] review it and auto-merge on PASS (unless it's a --draft)."
