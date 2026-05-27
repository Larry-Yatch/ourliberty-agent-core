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
emit_larry_alert_envelope() {
    local subject="$1"
    local message="$2"
    local cli="${SCRIPTS_DIR}/larry_alerts.py"
    if [ ! -f "$cli" ]; then
        return 0
    fi
    timeout 10 python3 "$cli" append_alert \
        --source sync.service \
        --severity warning \
        --subject "$subject" \
        --message "$message" >/dev/null 2>&1 || true
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

# Track whether orchestrator-imported scripts changed (to decide if restart needed)
# F55: narrow trigger from "any script changed" to "orchestrator.py specifically
# changed" since other scripts run as separate timers and don't require an
# orchestrator restart to pick up changes.
ORCHESTRATOR_PY_CHANGED=false
if [ -f "${STAGING_ROOT}/scripts/orchestrator.py" ] && [ -f "${LIVE_ROOT}/scripts/orchestrator.py" ]; then
    if ! diff -q "${STAGING_ROOT}/scripts/orchestrator.py" "${LIVE_ROOT}/scripts/orchestrator.py" >/dev/null 2>&1; then
        ORCHESTRATOR_PY_CHANGED=true
    fi
fi
# Keep SCRIPTS_CHANGED as alias for backward compatibility with other gates.
SCRIPTS_CHANGED=$ORCHESTRATOR_PY_CHANGED

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

# ── Step 7: Restart orchestrator if scripts changed ────────────────
if [ "$SCRIPTS_CHANGED" = true ]; then
    log "Scripts changed — restarting ourliberty-orchestrator..."
    if systemctl is-active ourliberty-orchestrator.service >/dev/null 2>&1; then
        sudo systemctl restart ourliberty-orchestrator.service
        log "  ourliberty-orchestrator restarted"
    else
        log "  ourliberty-orchestrator not active, skipping restart"
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
