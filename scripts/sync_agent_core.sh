#!/usr/bin/env bash
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
# sync_agent_core.sh — Atomic-swap sync from ourliberty-agent-core repo to live VM
#
# Pulls latest from origin/main, validates, waits for quiescence,
# then atomic-swaps updated files into the live agent tree.
#
# Safety: NEVER touches inboxes/, outboxes/, blackboard/, telegram/,
# logs/, memory/, audit-log/, or cloned project repos inside workspaces.

set -euo pipefail

# REPO_DIR / LIVE_ROOT default to the production paths but accept env-var
# overrides so the test harness can point them at a tmpdir-rooted fake tree.
# Production callers (ourliberty-sync.service) don't set these vars; the
# defaults apply unchanged.
REPO_DIR="${REPO_DIR:-/home/larry/agent-core}"
LIVE_ROOT="${LIVE_ROOT:-/home/larry/agents}"
STAGING_ROOT="${STAGING_ROOT:-/home/larry/agents/.sync-staging}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/larry/agents/.sync-backup}"
# Derived from LIVE_ROOT (default /home/larry/agents/blackboard, unchanged in
# production) so the test harness, which already redirects LIVE_ROOT into a
# tmpdir, doesn't trip write_status on the hardcoded /home/larry path.
BLACKBOARD_DIR="${BLACKBOARD_DIR:-${LIVE_ROOT}/blackboard}"
SYNC_STATUS_FILE="${BLACKBOARD_DIR}/agent-core-sync.json"
# One-tick grace marker for the uncommitted-changes refusal (see the dirty-tree
# block below). Presence means "the previous sync tick already saw
# non-allowlisted uncommitted dirt". We alert only when the condition persists
# across two consecutive ticks, and the EXIT trap clears the marker on every
# non-dirty-refuse exit — so a single-tick blip (the common case: Pulse's
# auto-commit or the GC healer commits the dirt by the next ~5min tick) never
# pages.
UNCOMMITTED_GRACE_MARKER="${BLACKBOARD_DIR}/agent-core-sync-uncommitted-grace"
# Set to 1 ONLY on the tick that refuses because of non-allowlisted uncommitted
# dirt. The EXIT trap reads it: it preserves the grace marker on a dirty-refuse
# tick and clears it on every other exit, so the marker strictly tracks
# *consecutive* dirty-refuse ticks (see the uncommitted-changes block).
UNCOMMITTED_DIRTY_REFUSE=0
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

# Directories that must NEVER be touched by sync
PROTECTED_DIRS=(
    "inboxes"
    "outboxes"
    "blackboard"
    "telegram"
    "logs"
    "memory"
    "audit-log"
)

# Subdirectories inside agent workspaces that must NEVER be synced
PROTECTED_WORKSPACE_SUBDIRS=(
    "repos"
    "state"
    "node_modules"
    "__pycache__"
    ".venv"
)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync_agent_core: $*"
}

# alert_larry — DM Larry (TODO Phase D notify wiring) when sync fails in a way
# that requires human attention. Best-effort: failure to alert never
# blocks or alters the sync flow itself. Added 2026-04-29 after the
# divergent-feature-branch incident silently broke sync for days.
alert_larry() {
    local subject="$1"
    local message="$2"
    local notify_script="${SCRIPTS_DIR}/notify_larry.py"
    if [ ! -f "$notify_script" ]; then
        return 0
    fi
    timeout 10 python3 "$notify_script" \
        --tier breakdown \
        --subject "sync_agent_core: $subject" \
        --message "$message" >/dev/null 2>&1 || true
}

# emit_larry_alert_envelope — append a record to the larry-alerts queue via
# the larry_alerts.py CLI. Subject is the cooldown dedup key (subject-specific
# 60min window for severity=warning). Added 2026-05-27 after the 'merged but
# not deployed' incident: alert_larry()->notify_larry.py never reached Larry
# (the notify wiring is still a TODO), so silent sync failures sat unobserved
# for hours. This is the belt-and-suspenders second channel that Beacon's
# Telegram bot polls. Best-effort: failure to enqueue never blocks the sync
# flow's existing exit semantics.
# Optional third arg `route` (escalate|closure|digest); defaults to escalate
# (fail-loud — a genuine block still DMs). Pass `digest` for transient
# self-healing conditions that need no action (the auto-commit-push retry).
emit_larry_alert_envelope() {
    local subject="$1"
    local message="$2"
    local route="${3:-escalate}"
    local cli="${SCRIPTS_DIR}/larry_alerts.py"
    if [ ! -f "$cli" ]; then
        return 0
    fi
    timeout 10 python3 "$cli" append_alert \
        --source sync.service \
        --severity warning \
        --subject "$subject" \
        --message "$message" \
        --route "$route" >/dev/null 2>&1 || true
}

write_status() {
    local status="$1"
    local message="$2"
    local timestamp
    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    mkdir -p "$(dirname "$SYNC_STATUS_FILE")"
    cat > "$SYNC_STATUS_FILE" <<STATUSEOF
{
  "last_sync": "$timestamp",
  "status": "$status",
  "message": "$message",
  "commit": "$(cd "$REPO_DIR" && git rev-parse HEAD 2>/dev/null || echo 'unknown')",
  "branch": "$(cd "$REPO_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
}
STATUSEOF
}

# One-tick grace for the uncommitted-changes refusal -------------------------
# The dirty working tree almost always clears by the next sync tick (~5min) when
# Pulse's auto-commit or heal_missions_card_gc.py lands, so alerting on the FIRST
# dirty tick cries wolf — and because larry_alerts is append-only with no resolve,
# the 🟡 SOON board warning never retracts (the 2026-06-18 07:51→07:56 one-tick
# false alarm). We defer: suppress the alert on the first dirty tick, fire only
# when the dirt is STILL present on a second consecutive tick. The refusal/exit-1
# itself is unconditional — sync never pulls onto a dirty tree; only the
# *alerting* is graced. The EXIT trap clears the marker on every non-dirty-refuse
# exit, so it tracks *consecutive* dirty ticks and can't go stale.
uncommitted_grace_marker_present() {
    [ -f "$UNCOMMITTED_GRACE_MARKER" ]
}
set_uncommitted_grace_marker() {
    # Best-effort: advisory state must never break the sync safety flow. If it
    # can't persist, the next tick simply re-defers (fail-safe-quiet) rather
    # than the script exiting under `set -e`.
    mkdir -p "$(dirname "$UNCOMMITTED_GRACE_MARKER")" 2>/dev/null || true
    date -u '+%Y-%m-%dT%H:%M:%SZ' > "$UNCOMMITTED_GRACE_MARKER" 2>/dev/null || true
}
clear_uncommitted_grace_marker() {
    rm -f "$UNCOMMITTED_GRACE_MARKER" 2>/dev/null || true
}

cleanup() {
    if [ -d "$STAGING_ROOT" ]; then
        rm -rf "$STAGING_ROOT"
    fi
    # One-tick grace bookkeeping (see the uncommitted-changes block). The marker
    # must track ONLY consecutive dirty-refuse ticks, so clear it on EVERY exit
    # except the dirty-refuse path that just set/kept it. Centralising the clear
    # here — rather than at each clean/tolerate/abort site — means no early exit
    # (wrong-branch, auto-commit-push-failed, validation/quiescence abort, or an
    # unexpected `set -e` death) can leave a stale marker that would spuriously
    # page on a later genuine first dirty tick.
    if [ "${UNCOMMITTED_DIRTY_REFUSE:-0}" != "1" ]; then
        clear_uncommitted_grace_marker
    fi
}
trap cleanup EXIT

# Behavior knobs (all opt-in, default off for backward compat)
#   FORCE_SYNC=1  — proceed with rsync even when HEAD didn't move. Use after
#                   manual workspace edits to push the repo state into the
#                   live tree without needing a dummy commit.
FORCE_SYNC="${FORCE_SYNC:-0}"

# ── Step 1: Fetch from origin ──────────────────────────────────────
log "Fetching latest from origin..."
cd "$REPO_DIR"

# Refuse to operate from a non-main branch — ourliberty-agent-core operates with
# direct commits to main per project convention. A non-main checkout means
# someone left a feature branch active; fixing that is human work.
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
if [ "$CURRENT_BRANCH" != "main" ]; then
    log "ERROR: Repo is on '$CURRENT_BRANCH', expected 'main'. Sync refuses to operate."
    write_status "error" "Wrong branch: $CURRENT_BRANCH"
    alert_larry "wrong branch ($CURRENT_BRANCH)" "sync_agent_core.sh refused to run because ${REPO_DIR} is checked out on '$CURRENT_BRANCH', not 'main'. Per ourliberty-agent-core operating model, all work commits direct to main. Action: ssh ourliberty-vm, cd ${REPO_DIR}, switch to main and merge or discard the feature branch."
    emit_larry_alert_envelope "sync-blocked:wrong-branch:${CURRENT_BRANCH}" "ourliberty-sync.service refusing to pull on branch ${CURRENT_BRANCH} (expected main). Working tree will not receive PR merges from origin/main until restored. Recovery: cd ${REPO_DIR} && git checkout main && git pull --ff-only (if tree is clean; else commit/stash work first)."
    exit 1
fi

# Machine-owned runtime handling (sync resilience). Two distinct classes of
# machine-owned dirt, handled differently so each has exactly ONE committer:
#
#  * Pulse runtime files (PULSE_RUNTIME_PATHS): an interactive Pulse /cycle can
#    leave runbooks/cycle-journal.md, runbooks/cycle-actions.jsonl, or pulse
#    MEMORY files uncommitted (the 2026-05-28 iter-98 incident class). Sync, which
#    is otherwise pull-only, auto-commits + pushes EXACTLY ONE commit of these so
#    it isn't blocked for hours until run_cycle.sh's next tick.
#
#  * captures.json + missions.json (SYNC_EXTRA_RUNTIME_PATHS): machine-owned
#    missions state whose SOLE committer is heal_missions_card_gc.py (every
#    ~10min — captures via the aging/sweep commit, missions.json via the
#    single-committer "commit ANY pending delta" path, Contract D). Sync must NOT
#    also commit them. #409 originally made sync a second committer of
#    captures.json, which created a dual-committer race on origin/main and, on a
#    failing push, sync's `git reset --hard` reverted it on disk and lost the
#    ingests written during the push window. A missions.json delta left by a
#    cleanup similarly jammed sync (the P1 incident). Sync now TOLERATES this
#    dirt: it neither commits nor resets these files and proceeds to the ff-pull.
#    The healer remains the single committer and persists them on its own tick.
#    The ff-pull is safe because the healer commits to THIS working tree first,
#    so its commits are already in local HEAD before origin advances — an
#    incoming ff never carries one of these changes, and git fast-forwards
#    cleanly past commits that don't touch a dirty file.
#
# Any dirt outside both sets falls through to the refuse-and-alert path below.
# shellcheck source=_lib_pulse_runtime.sh
source "${SCRIPTS_DIR}/_lib_pulse_runtime.sh"
# shellcheck source=_lib_push_with_rebase.sh
source "${SCRIPTS_DIR}/_lib_push_with_rebase.sh"
if ! git diff --quiet || ! git diff --cached --quiet; then
    # Auto-commit ONLY Pulse runtime dirt, and only when ALL dirt is
    # machine-owned (Pulse + healer-owned extras) and at least one Pulse file is
    # dirty. captures.json, if also dirty, is intentionally NOT staged — it is
    # left for its sole committer (the GC healer).
    if all_modified_in_sync_autocommit_allowlist "$REPO_DIR" \
       && any_modified_in_pulse_runtime_allowlist "$REPO_DIR"; then
        AUTO_PRE_HEAD="$(git rev-parse HEAD)"
        log "Pulse runtime allowlist dirty — auto-commit + push (captures.json, if dirty, left for the GC healer)"
        git add -- "${PULSE_RUNTIME_PATHS[@]}" 2>/dev/null || true

        TS=$(date -u +%Y%m%dT%H%M%SZ)
        if git commit -q -m "runtime: auto-commit Pulse runtime files (sync resilience) ${TS}" -m "Auto-committed by sync_agent_core.sh: Pulse-owned runtime files were dirty (see PULSE_RUNTIME_PATHS in scripts/_lib_pulse_runtime.sh). captures.json, if also dirty, is left to heal_missions_card_gc.py (its sole committer). Sync would otherwise refuse to pull from origin/main." 2>/dev/null; then
            log "Auto-committed Pulse runtime files; pushing to origin/main"
            # Reuse run_cycle.sh's rebase fallback: a bare push loses the race
            # when an interactive PR merge advances origin/main mid-cycle. Rebase
            # onto origin and retry instead of rolling back + alerting on every
            # routine non-FF (SYNC-PUSH-REBASE-FALLBACK-001).
            if push_with_rebase origin main /dev/stdout; then
                log "Pushed Pulse runtime auto-commit to origin/main (rebase fallback available)"
            else
                log "ERROR: push of Pulse runtime auto-commit failed even after rebase fallback; rolling back to ${AUTO_PRE_HEAD} (captures.json preserved)"
                git rebase --abort 2>/dev/null || true
                # Undo the auto-commit WITHOUT touching the working tree: a
                # `--mixed` reset moves HEAD+index back to AUTO_PRE_HEAD (so no
                # local-only commit lingers to break the next fast-forward, and
                # the index is clean) while leaving every worktree file alone.
                # captures.json keeps its live on-disk content (a plain
                # `git reset --hard` would have reverted it and lost the ingests
                # written during the push window — the data-loss class this
                # change removes). The Pulse worktree dirt is retained and simply
                # re-attempted on the next sync tick. We deliberately do NOT
                # `git checkout`/`restore` the Pulse paths to discard their dirt:
                # such a pathspec list aborts entirely when any entry (e.g. an
                # absent cycle-actions.jsonl) doesn't match a tracked file.
                git reset --mixed "$AUTO_PRE_HEAD" --quiet 2>/dev/null || true
                write_status "error" "Auto-commit push failed; rolled back"
                alert_larry "auto-commit push failed" "sync_agent_core.sh auto-committed Pulse runtime files but the push to origin/main failed; rolled back Pulse paths to ${AUTO_PRE_HEAD} (captures.json left live). Action: ssh ourliberty-vm, cd ${REPO_DIR}, run 'git push origin main' to debug (likely non-FF, auth, or network)."
                # Routine self-healing transient: the rollback restored a
                # pushable tree and sync retries the push on the next tick — no
                # action required, so route to the digest, not a DM (fix-first).
                emit_larry_alert_envelope "sync-blocked:auto-commit-push-failed" "ourliberty-sync.service: auto-committed Pulse runtime files but push to origin/main failed; rolled back Pulse paths to ${AUTO_PRE_HEAD:0:8} (captures.json left live). Self-heals on the next sync tick; no action needed." "digest"
                exit 1
            fi
        else
            log "Auto-commit produced no commit (nothing staged?); falling through"
        fi
    fi
fi

# Refuse to operate with uncommitted changes — UNLESS the only remaining dirt is
# healer-owned runtime state (SYNC_EXTRA_RUNTIME_PATHS: captures.json +
# missions.json), which sync tolerates and the GC healer commits on its own tick.
# Any other dirt (human edits, or Pulse dirt mixed with non-allowlisted files
# that the block above declined to commit) falls through to refuse-and-alert.
if ! git diff --quiet || ! git diff --cached --quiet; then
    if all_modified_in_sync_extra_allowlist "$REPO_DIR"; then
        log "Only healer-owned runtime dirt (captures.json/missions.json) present — tolerating; heal_missions_card_gc.py is its committer. Proceeding to pull."
        # Proceeding (tolerated dirt) is not a dirty-refuse, so the EXIT trap
        # clears the grace marker and a future first dirty tick starts fresh.
    else
        # This tick refuses because of non-allowlisted dirt. Flag it so the EXIT
        # trap PRESERVES the grace marker (every other exit clears it) — that is
        # what makes the marker track *consecutive* dirty-refuse ticks.
        UNCOMMITTED_DIRTY_REFUSE=1
        log "ERROR: Working tree has uncommitted changes. Sync refuses to operate."
        write_status "error" "Uncommitted changes in working tree"
        DIRTY_FILES=$(git status --short | head -10)
        # One-tick grace: in practice this non-allowlisted dirt clears by the
        # next tick (~5min) when Pulse's auto-commit or the GC healer commits it,
        # so the first dirty tick is almost always a false alarm. Suppress the
        # alert on the first occurrence; page only when the dirt is STILL present
        # on a second consecutive tick. The refusal/exit-1 below is unconditional
        # either way — sync never pulls onto a dirty tree.
        if uncommitted_grace_marker_present; then
            log "Uncommitted changes still present on a second consecutive tick — alerting."
            alert_larry "uncommitted changes block sync" "sync_agent_core.sh refused to run because ${REPO_DIR} has uncommitted modifications across two consecutive sync ticks. First 10 files: $DIRTY_FILES. Action: ssh ourliberty-vm, commit or stash the changes."
            emit_larry_alert_envelope "sync-blocked:uncommitted-changes" "ourliberty-sync.service refusing to pull: ${REPO_DIR} has uncommitted modifications (persisted across two consecutive sync ticks). Working tree will not receive PR merges from origin/main until cleaned. Recovery: cd ${REPO_DIR} && git status; commit or stash the changes."
        else
            set_uncommitted_grace_marker
            log "First dirty tick — deferring the uncommitted-changes alert one tick (usually clears when Pulse auto-commit / the GC healer lands by the next tick). Will alert if still dirty next tick."
        fi
        exit 1
    fi
fi
# Clean (or tolerated) fall-through: this tick is not a dirty-refuse, so the EXIT
# trap clears any grace marker left by a prior tick. Keeping the clear in the
# trap (not here) closes the stale-marker gap on the early wrong-branch and
# auto-commit-push-failed exits above, which never reach this point.

# Store current HEAD before fetch
OLD_HEAD="$(git rev-parse HEAD)"

git fetch origin main --quiet 2>/dev/null

# Check if there are new changes
NEW_HEAD="$(git rev-parse origin/main)"

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
    if [ "$FORCE_SYNC" = "1" ]; then
        log "No commits detected (HEAD=$OLD_HEAD) but FORCE_SYNC=1 — proceeding with rsync."
    else
        log "No changes detected (HEAD=$OLD_HEAD). Exiting."
        write_status "no-change" "Already up to date at $OLD_HEAD"
        exit 0
    fi
fi

if [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
    log "New commits detected: $OLD_HEAD -> $NEW_HEAD"

    # Fast-forward to origin/main
    git merge --ff-only origin/main --quiet 2>/dev/null || {
        log "ERROR: Cannot fast-forward to origin/main. Manual intervention required."
        write_status "error" "Fast-forward merge failed"
        alert_larry "fast-forward failed" "sync_agent_core.sh tried to fast-forward main from $OLD_HEAD to $NEW_HEAD but failed. The repo and origin have diverged. Action: ssh ourliberty-vm, cd ${REPO_DIR}, run 'git status' and 'git log --oneline origin/main..HEAD' to see what's local-only, then rebase or merge as appropriate."
        emit_larry_alert_envelope "sync-blocked:fast-forward-failed" "ourliberty-sync.service: cannot fast-forward main from ${OLD_HEAD:0:8} to ${NEW_HEAD:0:8}; repo and origin diverged. Recovery: cd ${REPO_DIR}; investigate divergence; rebase or hard-reset to origin/main once safe."
        exit 1
    }
fi

# ── Step 2: Validate incoming commit ───────────────────────────────
log "Validating incoming commit..."
if [ -f "${SCRIPTS_DIR}/validate_agent_core.py" ]; then
    python3 "${SCRIPTS_DIR}/validate_agent_core.py" --repo-dir "$REPO_DIR"
    VALIDATE_RC=$?
    if [ $VALIDATE_RC -ne 0 ]; then
        log "ERROR: Validation failed (exit code $VALIDATE_RC). Aborting sync."
        write_status "error" "Validation failed for commit $NEW_HEAD"
        alert_larry "validation failed" "sync_agent_core.sh halted at validate_agent_core.py for commit $NEW_HEAD (exit code $VALIDATE_RC). Repo rolled back to $OLD_HEAD. Action: ssh ourliberty-vm, cd ${REPO_DIR}, git checkout $NEW_HEAD, run 'python3 scripts/validate_agent_core.py' to see what failed."
        emit_larry_alert_envelope "sync-blocked:validation-failed:${NEW_HEAD:0:8}" "ourliberty-sync.service: validate_agent_core.py rejected commit ${NEW_HEAD:0:8} (exit ${VALIDATE_RC}); rolled back to ${OLD_HEAD:0:8}. Recovery: cd ${REPO_DIR}; git checkout ${NEW_HEAD}; python3 scripts/validate_agent_core.py to see what failed; fix on origin/main."
        # Roll back to old HEAD
        git reset --hard "$OLD_HEAD" --quiet 2>/dev/null
        exit 1
    fi
else
    log "WARNING: validate_agent_core.py not found, skipping validation"
fi

# ── Step 3: Brief quiescence hint (soft — proceed even on timeout) ─
# 2026-04-28 sweep v2: rsync writes atomically per-file (temp + rename),
# so a sync mid-task cannot corrupt a running agent (which loaded its
# prompt files at task start). The old hard 300s quiescence wait was
# over-conservative and caused every sync to fail because inboxes are
# never fully empty. New behavior: try ≤30s for cleanliness, warn but
# proceed if not reached.
#
# To restore the old strict behavior for a one-off sync, run with:
#   REQUIRE_QUIESCENCE=1 sync_agent_core.sh
QUIESCENCE_TIMEOUT="${QUIESCENCE_TIMEOUT:-30}"
REQUIRE_QUIESCENCE="${REQUIRE_QUIESCENCE:-0}"
if [ -f "${SCRIPTS_DIR}/await_quiescence.py" ]; then
    if [ "$REQUIRE_QUIESCENCE" = "1" ]; then
        log "Waiting for agent quiescence (strict, ${QUIESCENCE_TIMEOUT}s)..."
        python3 "${SCRIPTS_DIR}/await_quiescence.py" --timeout "$QUIESCENCE_TIMEOUT"
        QUIESCENCE_RC=$?
        if [ $QUIESCENCE_RC -ne 0 ]; then
            log "ERROR: Strict quiescence timeout. Aborting sync."
            write_status "error" "Quiescence timeout for commit $NEW_HEAD"
            alert_larry "quiescence timeout" "sync_agent_core.sh strict-quiescence wait exceeded ${QUIESCENCE_TIMEOUT}s for commit ${NEW_HEAD}; rolled back to ${OLD_HEAD}. Action: ssh ourliberty-vm, investigate which agents are non-quiescent (await_quiescence.py output)."
            emit_larry_alert_envelope "sync-blocked:quiescence-timeout" "ourliberty-sync.service: strict-quiescence wait exceeded ${QUIESCENCE_TIMEOUT}s for commit ${NEW_HEAD:0:8}; rolled back to ${OLD_HEAD:0:8}. Recovery: cd ${REPO_DIR}; python3 scripts/await_quiescence.py to identify the non-quiescent agent(s); resolve before next sync tick."
            git reset --hard "$OLD_HEAD" --quiet 2>/dev/null
            exit 1
        fi
        log "Strict quiescence reached."
    else
        log "Trying brief quiescence (soft, ${QUIESCENCE_TIMEOUT}s)..."
        if python3 "${SCRIPTS_DIR}/await_quiescence.py" --timeout "$QUIESCENCE_TIMEOUT" 2>/dev/null; then
            log "Quiescence reached."
        else
            log "WARN: Soft quiescence timeout — proceeding (rsync per-file atomic)."
        fi
    fi
else
    log "WARN: await_quiescence.py not found, skipping quiescence check"
fi

# ── Step 4: Backup live state ──────────────────────────────────────
log "Backing up live state..."
BACKUP_TS="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="${BACKUP_ROOT}/${BACKUP_TS}"
mkdir -p "$BACKUP_DIR"

# Backup only the directories we're about to modify
for dir in agents shared scripts config; do
    if [ -d "${LIVE_ROOT}/${dir}" ]; then
        cp -a "${LIVE_ROOT}/${dir}" "${BACKUP_DIR}/${dir}" 2>/dev/null || true
    fi
done

log "Backup saved to ${BACKUP_DIR}"

# ── Step 5: Stage new files ────────────────────────────────────────
log "Staging new files..."
rm -rf "$STAGING_ROOT"
mkdir -p "$STAGING_ROOT"

# Copy from repo to staging
for dir in agents shared scripts config; do
    if [ -d "${REPO_DIR}/${dir}" ]; then
        cp -a "${REPO_DIR}/${dir}" "${STAGING_ROOT}/${dir}"
    fi
done

# ── Step 6: rsync with exclusions ──────────────────────────────────
log "Syncing files to live tree..."

# Build rsync exclude list
RSYNC_EXCLUDES=()
for pdir in "${PROTECTED_DIRS[@]}"; do
    RSYNC_EXCLUDES+=(--exclude "/${pdir}/")
done

# Protect workspace subdirectories for each agent
for wsdir in "${PROTECTED_WORKSPACE_SUBDIRS[@]}"; do
    RSYNC_EXCLUDES+=(--exclude "agents/*/workspace/${wsdir}/")
    RSYNC_EXCLUDES+=(--exclude "agents/*/${wsdir}/")
done

# Additional exclusions
RSYNC_EXCLUDES+=(
    --exclude ".git/"
    --exclude ".github/"
    --exclude "*.bak"
    --exclude "*.bak-*"
    --exclude "__pycache__/"
    --exclude "*.pyc"
    --exclude "node_modules/"
)

# Sync agents/ — merge, don't delete agent-local files
if [ -d "${STAGING_ROOT}/agents" ]; then
    for agent_dir in "${STAGING_ROOT}/agents"/*/; do
        agent_name="$(basename "$agent_dir")"
        target_dir="${LIVE_ROOT}/agents/${agent_name}/workspace"
        mkdir -p "$target_dir"

        # rsync only .md files from repo into agent workspace
        # --ignore-existing would skip updates, so we use --update (newer wins)
        rsync -a --update \
            "${RSYNC_EXCLUDES[@]}" \
            "${agent_dir}" "${target_dir}/"

        log "  Synced agent: ${agent_name}"
    done
fi

# Sync shared/
if [ -d "${STAGING_ROOT}/shared" ]; then
    rsync -a --update \
        "${RSYNC_EXCLUDES[@]}" \
        --exclude "node_modules/" \
        --exclude "wireframes/" \
        --exclude "wireframes-repo/" \
        "${STAGING_ROOT}/shared/" "${LIVE_ROOT}/shared/"
    log "  Synced shared/"
fi

# Sync scripts/
if [ -d "${STAGING_ROOT}/scripts" ]; then
    rsync -a --update \
        "${RSYNC_EXCLUDES[@]}" \
        "${STAGING_ROOT}/scripts/" "${LIVE_ROOT}/scripts/"
    # Ensure scripts are executable
    chmod +x "${LIVE_ROOT}/scripts/"*.sh 2>/dev/null || true
    chmod +x "${LIVE_ROOT}/scripts/"*.py 2>/dev/null || true
    log "  Synced scripts/"
fi

# Sync config/ — but NEVER overwrite real secret files
if [ -d "${STAGING_ROOT}/config" ]; then
    rsync -a --update \
        "${RSYNC_EXCLUDES[@]}" \
        --exclude "auth-tokens.json" \
        --exclude "webhook-secrets.json" \
        --exclude "*.env" \
        --exclude ".env.*" \
        "${STAGING_ROOT}/config/" "${LIVE_ROOT}/config/"
    log "  Synced config/"
fi

# ── Step 7: Restart long-running daemons whose deployed code changed ──
# deploy-restart-gap-001 (2026-06-03): Type=simple daemons hold their
# imported modules in memory, so a sync that changes a module they import
# leaves them running stale code until restart. The committed manifest
# config/daemon-restart-manifest.json maps each daemon to its watched
# paths; we restart exactly the active units whose watched paths changed
# across OLD_HEAD..NEW_HEAD. (The prior orchestrator-only restart targeted
# ourliberty-orchestrator.service, which no longer exists, and is removed.)
MANIFEST_CLI="${SCRIPTS_DIR}/daemon_restart_manifest.py"
DAEMON_RESTART_STORM_THRESHOLD="${DAEMON_RESTART_STORM_THRESHOLD:-5}"

# ── Ordering guard (dashboard-api-deploy-race-001, 2026-07-08) ──
# We must NEVER act on the deployed code — restart a daemon (Step 7) or install
# + activate a unit file (Step 7b) — while the working tree is at anything other
# than NEW_HEAD, or the action runs whatever code is on disk instead of the
# commit we're deploying. The ff-merge at Step 1 already advanced HEAD; this
# re-asserts it so a concurrent tick, an aborted merge, or a rollback between
# then and now can't let an action race ahead of the code landing. Computed
# once here and consulted by BOTH steps below.
#
# Only a POSITIVELY-read divergent SHA trips the guard. An unreadable HEAD
# (empty — e.g. a transient git error / index.lock) does NOT skip: the ff-merge
# at Step 1 already succeeded, so the tree IS at NEW_HEAD, and a git hiccup here
# must not suppress a real deploy's restart (which would leave the daemon on
# stale code — the exact failure this guard exists to prevent).
HEAD_DRIFT=false
if [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
    LIVE_HEAD="$(cd "$REPO_DIR" && git rev-parse HEAD 2>/dev/null || true)"
    if [ -n "$LIVE_HEAD" ] && [ "$LIVE_HEAD" != "$NEW_HEAD" ]; then
        HEAD_DRIFT=true
        log "Deploy: working tree HEAD ($LIVE_HEAD) != NEW_HEAD ($NEW_HEAD); refusing to restart daemons or install units on a tree not at the deployed commit."
        emit_larry_alert_envelope "deploy-restart-head-drift" "ourliberty-sync.service: refusing daemon restarts + unit installs because ${REPO_DIR} HEAD is ${LIVE_HEAD:0:8}, not the deploy target ${NEW_HEAD:0:8}. Acting now would run/install stale code. Recovery: cd ${REPO_DIR}; git status; the next sync tick and heal_dashboard_api_sha_drift will reconcile once HEAD is clean." escalate
    fi
fi

if [ "$OLD_HEAD" != "$NEW_HEAD" ] && [ "$HEAD_DRIFT" = false ] && [ -f "$MANIFEST_CLI" ]; then
    CHANGED_PATHS="$(cd "$REPO_DIR" && git diff --name-only "$OLD_HEAD" "$NEW_HEAD" 2>/dev/null || true)"
    UNITS_TO_RESTART="$(printf '%s\n' "$CHANGED_PATHS" | python3 "$MANIFEST_CLI" units-for-changed 2>/dev/null || true)"
    if [ -n "$UNITS_TO_RESTART" ]; then
        UNIT_COUNT="$(printf '%s\n' "$UNITS_TO_RESTART" | grep -c .)"
        STORM=false
        if [ "$UNIT_COUNT" -gt "$DAEMON_RESTART_STORM_THRESHOLD" ]; then
            STORM=true
            log "Deploy-restart: $UNIT_COUNT daemons affected (storm — likely a shared base module changed); restarting all, summary only."
            emit_larry_alert_envelope "deploy-restart-storm" "ourliberty-sync.service restarting ${UNIT_COUNT} daemons after ${OLD_HEAD:0:8}->${NEW_HEAD:0:8} (a widely-imported module changed). Units: $(printf '%s ' $UNITS_TO_RESTART)." digest
        else
            log "Deploy-restart: $UNIT_COUNT daemon(s) affected by changed paths."
        fi
        RESTARTED=0
        SKIPPED=0
        FAILED=0
        while IFS= read -r unit; do
            [ -z "$unit" ] && continue
            if systemctl is-active "$unit" >/dev/null 2>&1; then
                if sudo -n systemctl restart "$unit" >/dev/null 2>&1; then
                    RESTARTED=$((RESTARTED + 1))
                    [ "$STORM" = false ] && log "  restarted $unit"
                else
                    FAILED=$((FAILED + 1))
                    log "  WARN: restart failed for $unit (manual: sudo systemctl restart $unit)"
                fi
            else
                SKIPPED=$((SKIPPED + 1))
                [ "$STORM" = false ] && log "  $unit not active, skipping"
            fi
        done <<< "$UNITS_TO_RESTART"
        log "Deploy-restart summary: restarted=$RESTARTED skipped-inactive=$SKIPPED failed=$FAILED of $UNIT_COUNT affected."
    else
        log "Deploy-restart: no long-running daemon affected by this sync."
    fi
fi

# ── Step 7b: Install/reconcile systemd units when a unit file changed ──
# post-merge-install-drift-trigger-001 (2026-06-11): heal_systemd_install_drift
# only installs repo units to /etc/systemd/system/ on its 12h timer, so a new or
# changed systemd/*.service|*.timer sits on disk up to 12h before install — the
# RUNNING unit keeps stale config (e.g. an old ReadWritePaths) and agents fail to
# persist transcripts (PR #438). Fire the healer in --triggered mode within this
# sync cycle (<=1h) when this sync's diff touched a unit file. We force
# OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED=true (mirroring the timer .service env)
# so it REMEDIATES rather than dry-runs; all other gates (kill-switch, allowlist,
# re-DM dedup) still apply. Non-fatal: a healer error is a WARN and sync continues;
# the 12h timer stays as the backstop.
INSTALL_DRIFT_HEALER="${SCRIPTS_DIR}/heal_systemd_install_drift.py"
if [ "$OLD_HEAD" != "$NEW_HEAD" ] && [ "$HEAD_DRIFT" = true ]; then
    # Same ordering guard as Step 7: installing/activating units from a tree not
    # at NEW_HEAD would deploy stale unit files. Skip; the 12h install-drift
    # timer and the next sync tick reconcile once HEAD is clean.
    log "Install-drift trigger: skipped (HEAD drift guard)."
elif [ "$OLD_HEAD" != "$NEW_HEAD" ] && [ -f "$INSTALL_DRIFT_HEALER" ]; then
    UNIT_CHANGED_PATHS="$(cd "$REPO_DIR" && git diff --name-only "$OLD_HEAD" "$NEW_HEAD" 2>/dev/null || true)"
    if printf '%s\n' "$UNIT_CHANGED_PATHS" | grep -Eq '^systemd/.*\.(service|timer)$'; then
        log "Install-drift trigger: a systemd unit file changed this sync; running healer (--triggered)."
        if OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED=true \
            python3 "$INSTALL_DRIFT_HEALER" --triggered; then
            log "Install-drift trigger: healer tick complete."
        else
            log "  WARN: install-drift healer (--triggered) exited non-zero; the 12h timer remains the backstop."
        fi
    else
        log "Install-drift trigger: no systemd unit file changed this sync; skipping."
    fi
fi

# ── Step 8: Cleanup old backups (keep last 5) ──────────────────────
if [ -d "$BACKUP_ROOT" ]; then
    BACKUP_COUNT=$(ls -1d "${BACKUP_ROOT}"/*/ 2>/dev/null | wc -l)
    if [ "$BACKUP_COUNT" -gt 5 ]; then
        REMOVE_COUNT=$((BACKUP_COUNT - 5))
        ls -1d "${BACKUP_ROOT}"/*/ 2>/dev/null | head -n "$REMOVE_COUNT" | while read -r old_backup; do
            rm -rf "$old_backup"
            log "  Cleaned up old backup: $old_backup"
        done
    fi
fi

# ── Step 9: Write sync status ─────────────────────────────────────
write_status "success" "Synced $OLD_HEAD -> $NEW_HEAD"
log "Sync complete: $OLD_HEAD -> $NEW_HEAD"
