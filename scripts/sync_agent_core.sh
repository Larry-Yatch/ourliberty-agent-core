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
BLACKBOARD_DIR="/home/larry/agents/blackboard"
SYNC_STATUS_FILE="${BLACKBOARD_DIR}/agent-core-sync.json"
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

cleanup() {
    if [ -d "$STAGING_ROOT" ]; then
        rm -rf "$STAGING_ROOT"
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

# Pulse-runtime auto-commit (sync resilience). Closes the 2026-05-28 iter-98
# incident class: an interactive Pulse /cycle leaves runbooks/cycle-journal.md,
# runbooks/cycle-actions.jsonl, agents/pulse/MEMORY.md, or agents/pulse/memory/*
# uncommitted, and sync refuses to pull for hours until Larry intervenes.
#
# Posture change (bounded): sync, which has historically been pull-only,
# gains the ability to push EXACTLY ONE commit to origin/main, and only when
# every modified file is inside the hardcoded Pulse runtime allowlist in
# scripts/_lib_pulse_runtime.sh. Any non-allowlist dirt falls through to the
# existing refuse-and-alert path unchanged.
#
# Failure mode: if the push fails (non-FF, network, auth), the local commit is
# hard-reset to its pre-auto-commit HEAD so we never leave a local-only commit
# on main that would break the next fast-forward. The fixture-pattern guard
# (mirrored from run_cycle.sh) refuses to auto-commit any staged change whose
# diff mentions a fixture-leak task_id.
# shellcheck source=_lib_pulse_runtime.sh
source "${SCRIPTS_DIR}/_lib_pulse_runtime.sh"
# shellcheck source=_lib_push_with_rebase.sh
source "${SCRIPTS_DIR}/_lib_push_with_rebase.sh"
if ! git diff --quiet || ! git diff --cached --quiet; then
    if all_modified_in_pulse_runtime_allowlist "$REPO_DIR"; then
        AUTO_PRE_HEAD="$(git rev-parse HEAD)"
        log "Pulse runtime allowlist dirty (no other modifications) — auto-commit + push"
        git add -- "${PULSE_RUNTIME_PATHS[@]}" 2>/dev/null || true

        TS=$(date -u +%Y%m%dT%H%M%SZ)
        if git commit -q -m "pulse: auto-commit runtime files (sync resilience) ${TS}" -m "Auto-committed by sync_agent_core.sh: working tree had only Pulse-owned runtime files dirty (see scripts/_lib_pulse_runtime.sh allowlist). Sync would otherwise refuse to pull from origin/main." 2>/dev/null; then
            log "Auto-committed Pulse runtime files; pushing to origin/main"
            # Reuse run_cycle.sh's rebase fallback: a bare push loses the race
            # when an interactive PR merge advances origin/main mid-cycle. Rebase
            # onto origin and retry instead of rolling back + alerting on every
            # routine non-FF (SYNC-PUSH-REBASE-FALLBACK-001).
            if push_with_rebase origin main /dev/stdout; then
                log "Pushed Pulse runtime auto-commit to origin/main (rebase fallback available)"
            else
                log "ERROR: push of Pulse runtime auto-commit failed even after rebase fallback; rolling back to ${AUTO_PRE_HEAD}"
                git rebase --abort 2>/dev/null || true
                git reset --hard "$AUTO_PRE_HEAD" --quiet 2>/dev/null || true
                write_status "error" "Auto-commit push failed; rolled back"
                alert_larry "auto-commit push failed" "sync_agent_core.sh auto-committed Pulse runtime files but the push to origin/main failed; rolled back to ${AUTO_PRE_HEAD}. Action: ssh ourliberty-vm, cd ${REPO_DIR}, run 'git push origin main' to debug (likely non-FF, auth, or network)."
                # Routine self-healing transient: the rollback restored a clean,
                # pushable tree and sync retries the push on the next tick — no
                # action required, so route to the digest, not a DM (fix-first).
                emit_larry_alert_envelope "sync-blocked:auto-commit-push-failed" "ourliberty-sync.service: auto-committed Pulse runtime files but push to origin/main failed; rolled back to ${AUTO_PRE_HEAD:0:8} (clean tree restored). Self-heals on the next sync tick; no action needed." "digest"
                exit 1
            fi
        else
            log "Auto-commit produced no commit (nothing staged?); falling through"
        fi
    fi
fi

# Refuse to operate with uncommitted changes — same rationale.
if ! git diff --quiet || ! git diff --cached --quiet; then
    log "ERROR: Working tree has uncommitted changes. Sync refuses to operate."
    write_status "error" "Uncommitted changes in working tree"
    DIRTY_FILES=$(git status --short | head -10)
    alert_larry "uncommitted changes block sync" "sync_agent_core.sh refused to run because ${REPO_DIR} has uncommitted modifications. First 10 files: $DIRTY_FILES. Action: ssh ourliberty-vm, commit or stash the changes."
    emit_larry_alert_envelope "sync-blocked:uncommitted-changes" "ourliberty-sync.service refusing to pull: ${REPO_DIR} has uncommitted modifications. Working tree will not receive PR merges from origin/main until cleaned. Recovery: cd ${REPO_DIR} && git status; commit or stash the changes."
    exit 1
fi

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
if [ "$OLD_HEAD" != "$NEW_HEAD" ] && [ -f "$MANIFEST_CLI" ]; then
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
