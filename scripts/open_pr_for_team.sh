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
# Pass --deep-review to ALSO apply the `deep-review-required` label: the team
# still reviews, but the merge machinery HOLDS it for a human `/code-review
# high` + manual merge (merge-gate-deep-review-hold) instead of auto-merging.
# Use it to force the hold on a risky PR that touches none of the critical-path
# fileset. (Fileset PRs hold automatically — no flag needed.)
#
# Idempotently creates the `auto-review` label if the repo doesn't have it yet.
#
# Multi-repo: heal_undispatched_pr_review routes labeled PRs in BOTH
# ourliberty-agent-core AND ourliberty-dashboard, so this opener takes an optional
# `--repo <owner/name>` (default agent-core). It ensures the label on whichever
# repo you target and opens the PR there.
#
# Usage:
#   scripts/open_pr_for_team.sh --title "<t>" --body "<b>" [gh pr create args...]
#   scripts/open_pr_for_team.sh --repo Larry-Yatch/ourliberty-dashboard \
#       --title "<t>" --body-file body.md --draft
# All other args pass through to `gh pr create` verbatim (e.g. --base, --head).
set -euo pipefail

REPO="Larry-Yatch/ourliberty-agent-core"   # default; override with --repo
AUTO_REVIEW_LABEL="auto-review"
DEEP_REVIEW_LABEL="deep-review-required"
WANT_DEEP_REVIEW=false

# Pull an optional `--repo <coords>` / `--repo=<coords>` and the `--deep-review`
# flag out of the args so we ensure the label(s) on the RIGHT repo and pass
# --repo to gh exactly once (a bare passthrough would collide with our own
# --repo). Everything else is preserved.
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:?--repo requires a value (owner/name)}"; shift 2 ;;
    --repo=*) REPO="${1#--repo=}"; shift ;;
    --deep-review) WANT_DEEP_REVIEW=true; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

# Ensure the opt-in label exists on the TARGET repo (no-op if already present).
# Color/description are only set on first creation; `|| true` keeps a benign
# "already exists" non-fatal.
if ! gh label list --repo "$REPO" --limit 200 \
      --json name --jq '.[].name' 2>/dev/null | grep -qx "$AUTO_REVIEW_LABEL"; then
  echo "[open_pr_for_team] creating '${AUTO_REVIEW_LABEL}' label on ${REPO}..."
  gh label create "$AUTO_REVIEW_LABEL" --repo "$REPO" \
    --color "0e8a16" \
    --description "Cleared for the agent team: Mirror reviews + auto-merges on PASS" \
    || true
fi

# --deep-review: also apply `deep-review-required` so the merge machinery holds
# the PR for a human /code-review high (merge-gate-deep-review-hold). Ensure the
# label exists first.
LABEL_ARGS=(--label "$AUTO_REVIEW_LABEL")
if [[ "$WANT_DEEP_REVIEW" == true ]]; then
  if ! gh label list --repo "$REPO" --limit 200 \
        --json name --jq '.[].name' 2>/dev/null | grep -qx "$DEEP_REVIEW_LABEL"; then
    echo "[open_pr_for_team] creating '${DEEP_REVIEW_LABEL}' label on ${REPO}..."
    gh label create "$DEEP_REVIEW_LABEL" --repo "$REPO" \
      --color "b60205" \
      --description "Critical-path: team reviews, but HELD for a human /code-review high + manual merge" \
      || true
  fi
  LABEL_ARGS+=(--label "$DEEP_REVIEW_LABEL")
fi

# Default base to main unless the caller supplied a base (long or gh short form).
has_base=false
for a in ${ARGS[@]+"${ARGS[@]}"}; do
  case "$a" in --base|--base=*|-B) has_base=true ;; esac
done
if [[ "$has_base" == false ]]; then
  ARGS=(--base main ${ARGS[@]+"${ARGS[@]}"})
fi

echo "[open_pr_for_team] gh pr create --repo ${REPO} ${LABEL_ARGS[*]} ${ARGS[*]}"
gh pr create --repo "$REPO" "${LABEL_ARGS[@]}" ${ARGS[@]+"${ARGS[@]}"}

echo "[open_pr_for_team] done — PR opened with '${AUTO_REVIEW_LABEL}'. The team will"
echo "[open_pr_for_team] review it and auto-merge on PASS (unless it's a --draft)."
