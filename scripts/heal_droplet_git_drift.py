#!/usr/bin/env python3
"""heal_droplet_git_drift.py — observe + alert on droplet git-state drift.

Surfaces the failure mode where the droplet's working tree falls out of
sync with `origin/main` without anyone noticing. Three signals are checked
on every tick and each tripping condition fires its own larry_alert with a
recovery hint:

  - AHEAD : unpushed commits whose oldest is older than 2 hours. The 2h
            grace window matches the "active work in progress" cadence; a
            commit younger than that is presumed to be in the middle of a
            branch flow and shouldn't alert.
  - BEHIND: more than 2 un-pulled commits on `origin/main`. The threshold
            is `> 2` (strict) so a fast-moving day's first new merge does
            not page; a small cluster does.
  - UNCOMMITTED: any uncommitted change whose newest mtime is older than
            6 hours. The age signal is the *newest* mtime so a stale edit
            session sitting overnight trips while an active edit doesn't.
            Healer-managed runtime paths (config/healer-managed-runtime-paths.json,
            e.g. captures.json) are subtracted first — a tree whose only dirt is
            those files is nominal-by-design and never pages; the 6h gate then
            applies to the remaining non-managed dirt only.

Observation-only by construction: no auto-pull / auto-push / auto-commit.
Broken main on origin or mid-edit local state must NEVER be silently
overwritten. Recovery commands appear in the alert body for Larry to run.

Cooldown: per-subject 6h via a state file at
`~/agents/state/heal-droplet-git-drift-cooldowns.json`. larry_alerts
itself has a 60-min warning cooldown; this healer needs a longer window
(an unpushed commit sitting for 7h is one alert, not seven), so we gate
locally first and rely on larry_alerts only as the final write.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from atomic_io import atomic_write_json  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-droplet-git-drift.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-droplet-git-drift.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-droplet-git-drift-cooldowns.json'

REPO_ROOT = Path(os.environ.get('OURLIBERTY_REPO_ROOT', '/home/larry/agent-core'))

# Canonical allowlist of working-tree files written by a healer/ingest on its
# own cadence and committed by a SOLE healer-committer on a timer. A tree whose
# ONLY dirt is these paths is nominal-by-design (Missions v2 § 4 batched
# durability), not a discipline violation — so it must NOT page. Loaded from the
# repo the code is versioned with (next to scripts/), not REPO_ROOT, so the list
# travels with the script regardless of which working tree is being inspected.
HEALER_MANAGED_RUNTIME_PATHS_FILE = (
    _SCRIPTS_DIR.parent / 'config' / 'healer-managed-runtime-paths.json'
)
# Hardcoded fallback so a JSON read/parse failure never crashes the tick nor
# silences a real stale-edit alert. Kept consistent with the canonical JSON and
# with _lib_pulse_runtime.sh SYNC_EXTRA_RUNTIME_PATHS by the drift test in
# scripts/tests/test_heal_droplet_git_drift.py.
_HEALER_MANAGED_PATHS_FALLBACK = (
    'agents/beacon/captures.json',
    'agents/beacon/missions.json',
    'agents/beacon/projects.json',
)

GIT_TIMEOUT_S = 30

# Trip thresholds (per-signal). Picked from the 2026-05-27 incident shapes:
# Pulse commit sat unpushed for ~2h before manual rebase; droplet was 5
# commits behind without notice. The strict `>` makes the boundary cases
# (exactly 2h, exactly 2 behind) safe.
AHEAD_AGE_THRESHOLD_SEC = 2 * 60 * 60
BEHIND_COMMIT_THRESHOLD = 2
UNCOMMITTED_AGE_THRESHOLD_SEC = 6 * 60 * 60

# Per-subject larry_alert cooldown (matches heal_stale_daemon_code's
# PER_SERVICE_COOLDOWN_SEC). larry_alerts' built-in 60-min warning
# cooldown is the floor; this 6h gate is the binding constraint.
PER_SUBJECT_COOLDOWN_SEC = 6 * 60 * 60

# --- G7 wedged-committer delta-age (completeness PR-3 R1) ---
# A managed runtime path is written by an ingest writer on its own cadence and
# committed by a SOLE healer-committer on a timer. The 6h newest-mtime gate above
# can NEVER catch a dead committer: the ingest writer keeps the mtime fresh
# exactly while the committer is wedged, so the file looks "actively edited"
# forever. G7 tracks time-since-FIRST-OBSERVED-DIRTY per managed path in a small
# state file; if a path stays continuously dirty past its committer's SLA the
# committer is wedged and we fire ONE escalation naming it. Threshold per path is
# max(3x cadence_min, G7_MIN_THRESHOLD_SEC) — 3 missed commits or 45 min,
# whichever is longer.
G7_MIN_THRESHOLD_SEC = 45 * 60

# Deploy-grace suppression (R1): right after a deploy every runtime file looks
# dirty until the committers catch up; a marker file's mtime opens a short grace
# window during which G7 stays silent. Absent marker ⇒ NOT in grace (fail-open:
# a real wedge must still page). Injectable in evaluate for tests.
DEPLOY_MARKER_FILE = AGENTS_ROOT / 'blackboard' / 'last-deploy.marker'
DEPLOY_GRACE_SEC = 10 * 60


# -------------------- logging --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- kill switch --------------------

def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


# -------------------- cooldown state --------------------

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'subjects': {}}
        data.setdefault('subjects', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'subjects': {}}


def save_state(state: dict) -> None:
    try:
        atomic_write_json(STATE_FILE, state, indent=2)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def in_cooldown(state: dict, subject: str, now: Optional[float] = None) -> bool:
    last = state['subjects'].get(subject)
    if not isinstance(last, (int, float)):
        return False
    now = now if now is not None else time.time()
    return (now - last) < PER_SUBJECT_COOLDOWN_SEC


def mark_alerted(state: dict, subject: str, now: Optional[float] = None) -> None:
    state['subjects'][subject] = now if now is not None else time.time()


# -------------------- git shellouts --------------------

def _git(args: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    """Run `git <args>` in cwd (default REPO_ROOT). Return (rc, stdout, stderr)."""
    try:
        result = subprocess.run(
            ['git', '-C', str(cwd or REPO_ROOT), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
        return result.returncode, result.stdout, result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return -1, '', f'{type(e).__name__}: {e}'


def fetch_origin() -> bool:
    """Refresh remote-tracking refs. Return True on success."""
    rc, _, stderr = _git(['fetch', 'origin', '--quiet'])
    if rc != 0:
        log(f'git fetch origin failed (rc={rc} stderr={stderr.strip()[:200]}); '
            f'skipping tick without alert', 'WARN')
        return False
    return True


def current_branch() -> str:
    rc, stdout, _ = _git(['rev-parse', '--abbrev-ref', 'HEAD'])
    if rc != 0:
        return 'unknown'
    return stdout.strip() or 'unknown'


def remote_tracking_ref() -> Optional[str]:
    """Return the upstream ref name (e.g. `origin/main`) or None if absent."""
    rc, stdout, _ = _git(['rev-parse', '--abbrev-ref', '--symbolic-full-name',
                          '@{upstream}'])
    if rc != 0:
        return None
    return stdout.strip() or None


def ahead_count(upstream: str) -> Optional[int]:
    """Commits HEAD is ahead of `upstream`, or None on a git error.

    None (not 0) on failure so the caller does not read a transient rev-list
    error as "0 commits ahead / no drift" and silently mask real drift (audit
    #50)."""
    rc, stdout, _ = _git(['rev-list', f'{upstream}..HEAD', '--count'])
    if rc != 0:
        return None
    try:
        return int(stdout.strip())
    except ValueError:
        return None


def behind_count(upstream: str) -> Optional[int]:
    """Commits HEAD is behind `upstream`, or None on a git error (see
    ahead_count — None is distinct from a real 0 so a soft error never reads as
    no-drift, audit #50)."""
    rc, stdout, _ = _git(['rev-list', f'HEAD..{upstream}', '--count'])
    if rc != 0:
        return None
    try:
        return int(stdout.strip())
    except ValueError:
        return None


def oldest_unpushed_commit_ts(upstream: str) -> Optional[float]:
    """Return the unix timestamp of the OLDEST unpushed commit, or None."""
    rc, stdout, _ = _git(['log', f'{upstream}..HEAD', '--format=%ct'])
    if rc != 0:
        return None
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return float(lines[-1])  # `git log` is reverse-chrono; last = oldest
    except ValueError:
        return None


def uncommitted_files() -> list[str]:
    """Return relative paths from `git status --porcelain` (any kind of change)."""
    rc, stdout, _ = _git(['status', '--porcelain'])
    if rc != 0:
        return []
    paths = []
    for line in stdout.splitlines():
        if len(line) < 3:
            continue
        # Porcelain v1 format: `XY <path>` with two status columns + space.
        # For renames the path is `<orig> -> <new>`; we take the new name.
        path_part = line[3:].strip()
        if ' -> ' in path_part:
            path_part = path_part.split(' -> ', 1)[1]
        # Strip quotes that git adds for paths with special chars.
        if path_part.startswith('"') and path_part.endswith('"'):
            path_part = path_part[1:-1]
        if path_part:
            paths.append(path_part)
    return paths


def healer_managed_paths() -> tuple[str, ...]:
    """Return the canonical healer-managed runtime paths.

    Read from HEALER_MANAGED_RUNTIME_PATHS_FILE; fall back to the hardcoded
    tuple if the file is missing/unreadable/malformed so a read failure never
    breaks the drift check (it would otherwise either crash or, worse, suppress
    a real alert)."""
    try:
        data = json.loads(HEALER_MANAGED_RUNTIME_PATHS_FILE.read_text())
        paths = data.get('paths')
        if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
            return tuple(paths)
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        pass
    return _HEALER_MANAGED_PATHS_FALLBACK


def healer_managed_committers() -> list[dict]:
    """Return the R1 `committers` list: [{path, committer, cadence_min}, ...].

    Empty list on a missing/malformed file (G7 then simply doesn't run — it
    never crashes the tick nor silences the other signals). Each entry is
    validated for the three required keys with the right types; malformed rows
    are dropped individually."""
    try:
        data = json.loads(HEALER_MANAGED_RUNTIME_PATHS_FILE.read_text())
        rows = data.get('committers')
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if (isinstance(row, dict)
                and isinstance(row.get('path'), str) and row['path']
                and isinstance(row.get('committer'), str) and row['committer']
                and isinstance(row.get('cadence_min'), (int, float))
                and row['cadence_min'] > 0):
            out.append({'path': row['path'], 'committer': row['committer'],
                        'cadence_min': float(row['cadence_min'])})
    return out


def _in_deploy_grace(now: Optional[float] = None,
                     marker: Optional[Path] = None) -> bool:
    """True iff a deploy landed within the last DEPLOY_GRACE_SEC (R1). Absent
    marker ⇒ False (fail-open so a real wedge still pages)."""
    marker = marker or DEPLOY_MARKER_FILE
    try:
        mt = marker.stat().st_mtime
    except OSError:
        return False
    return ((now if now is not None else time.time()) - mt) < DEPLOY_GRACE_SEC


def newest_mtime_of(paths: list[str], root: Path) -> Optional[float]:
    """Return the newest mtime among tracked-file paths, or None if none stat."""
    newest: Optional[float] = None
    for rel in paths:
        candidate = root / rel
        try:
            mt = candidate.stat().st_mtime
        except OSError:
            continue
        if newest is None or mt > newest:
            newest = mt
    return newest


# -------------------- alert emit --------------------

def _import_larry_alerts():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import larry_alerts as la  # noqa: E402
    return la


def emit_alert(
    state: dict, subject: str, message: str, suggested_action: str,
    now: Optional[float] = None,
) -> bool:
    """Emit one larry_alert IF outside the local 6h cooldown.

    Marks `subject` as alerted whether or not larry_alerts itself wrote
    the record (its internal 60-min cooldown can suppress; the local 6h
    gate is the binding cadence for this healer).
    """
    if in_cooldown(state, subject, now=now):
        log(f'cooldown-suppress subject={subject}')
        return False
    try:
        la = _import_larry_alerts()
        wrote = la.append_alert(
            source='heal-droplet-git-drift',
            severity='warning',
            subject=subject,
            message=message,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'append_alert failed: {type(e).__name__}: {e}', 'WARN')
        wrote = False
    mark_alerted(state, subject, now=now)
    save_state(state)
    if wrote:
        log(f'ALERT subject={subject}')
    else:
        log(f'alert suppressed by larry_alerts internal cooldown or error; '
            f'subject={subject}')
    return wrote


# -------------------- per-signal evaluators --------------------

def evaluate_ahead(
    upstream: str, branch: str, state: dict, now: Optional[float] = None,
) -> bool:
    """Return True if the ahead-trip alert fired (or was cooldown-suppressed)."""
    n = ahead_count(upstream)
    if n is None:
        log(f'ahead_count for {upstream} failed (git error after a successful '
            f'fetch); skipping ahead-drift check this tick rather than reporting '
            f'no drift', 'WARN')
        return False
    if n <= 0:
        return False
    oldest_ts = oldest_unpushed_commit_ts(upstream)
    if oldest_ts is None:
        return False
    age_sec = (now if now is not None else time.time()) - oldest_ts
    if age_sec <= AHEAD_AGE_THRESHOLD_SEC:
        return False
    age_h = age_sec / 3600.0
    subject = f'droplet-ahead:{branch}'
    msg = (
        f'Droplet branch `{branch}` is {n} commit(s) ahead of `{upstream}`; '
        f'oldest unpushed commit is {age_h:.1f}h old.\n\n'
        f'No automated push — recovery is manual:\n'
        f'  cd {REPO_ROOT} && git push origin {branch}'
    )
    emit_alert(state, subject, msg,
               suggested_action=f'cd {REPO_ROOT} && git push origin {branch}',
               now=now)
    return True


def evaluate_behind(
    upstream: str, branch: str, state: dict, now: Optional[float] = None,
) -> bool:
    n = behind_count(upstream)
    if n is None:
        log(f'behind_count for {upstream} failed (git error after a successful '
            f'fetch); skipping behind-drift check this tick rather than reporting '
            f'no drift', 'WARN')
        return False
    if n <= BEHIND_COMMIT_THRESHOLD:
        return False
    subject = f'droplet-behind:{branch}'
    msg = (
        f'Droplet branch `{branch}` is {n} commit(s) behind `{upstream}` '
        f'(threshold > {BEHIND_COMMIT_THRESHOLD}).\n\n'
        f'No automated pull — recovery is manual:\n'
        f'  cd {REPO_ROOT} && git pull --rebase origin {branch}'
    )
    emit_alert(state, subject, msg,
               suggested_action=f'cd {REPO_ROOT} && git pull --rebase origin {branch}',
               now=now)
    return True


def evaluate_uncommitted(
    branch: str, state: dict, now: Optional[float] = None,
) -> bool:
    paths = uncommitted_files()
    if not paths:
        return False
    # Subtract healer-managed runtime paths first: a tree whose ONLY dirt is
    # those files is nominal-by-design and must not page at any mtime age. The
    # 6h gate then applies to the REMAINING (non-managed) dirt only, so a real
    # stale edit still pages and is named — while captures.json's batched-
    # durability window stays silent.
    managed = set(healer_managed_paths())
    paths = [p for p in paths if p not in managed]
    if not paths:
        return False
    newest = newest_mtime_of(paths, REPO_ROOT)
    if newest is None:
        return False
    age_sec = (now if now is not None else time.time()) - newest
    if age_sec <= UNCOMMITTED_AGE_THRESHOLD_SEC:
        return False
    age_h = age_sec / 3600.0
    preview = ', '.join(paths[:5])
    if len(paths) > 5:
        preview += f', … (+{len(paths) - 5} more)'
    subject = f'droplet-uncommitted:{branch}'
    msg = (
        f'Droplet has {len(paths)} uncommitted file(s); newest edit is '
        f'{age_h:.1f}h old.\n\n'
        f'Files: {preview}\n\n'
        f'Recovery (review then commit or restore):\n'
        f'  cd {REPO_ROOT} && git status\n'
        f'  cd {REPO_ROOT} && git diff'
    )
    emit_alert(state, subject, msg,
               suggested_action=f'cd {REPO_ROOT} && git status',
               now=now)
    return True


def evaluate_managed_committer_wedged(
    branch: str, state: dict, now: Optional[float] = None,
    *, dirty_paths: Optional[list[str]] = None,
    in_deploy_grace: Optional[bool] = None,
) -> bool:
    """G7 (R1): detect a WEDGED healer-committer via first-observed-dirty age.

    For each managed committer path: while it is dirty, remember when it FIRST
    went dirty (a small `managed_dirty` map in the cooldown state file); when it
    goes clean, forget it. If a path stays continuously dirty past
    max(3x cadence_min, G7_MIN_THRESHOLD_SEC) the committer that owns it is
    wedged — fire ONE escalation naming the committer (6h cooldown). Deploy-grace
    suppresses (post-deploy everything is transiently dirty). Delta-age (NOT
    newest-mtime) is load-bearing: the ingest writer keeps the mtime fresh
    exactly while the committer is dead, so mtime can never catch this."""
    committers = healer_managed_committers()
    if not committers:
        return False
    now = now if now is not None else time.time()
    if dirty_paths is None:
        dirty_paths = uncommitted_files()
    dirty = set(dirty_paths)
    tracked = state.setdefault('managed_dirty', {})

    # Update first-observed-dirty bookkeeping for every managed path.
    managed_paths = {c['path'] for c in committers}
    for path in managed_paths:
        if path in dirty:
            tracked.setdefault(path, now)
        else:
            tracked.pop(path, None)
    # Drop stale keys for paths no longer managed (schema shrank).
    for path in [p for p in tracked if p not in managed_paths]:
        tracked.pop(path, None)

    if in_deploy_grace is None:
        in_deploy_grace = _in_deploy_grace(now)
    if in_deploy_grace:
        save_state(state)
        log('G7: deploy grace active; suppressing wedged-committer check')
        return False

    fired = False
    for c in committers:
        path, committer, cadence_min = c['path'], c['committer'], c['cadence_min']
        first = tracked.get(path)
        if not isinstance(first, (int, float)):
            continue
        threshold = max(3 * cadence_min * 60, G7_MIN_THRESHOLD_SEC)
        age_sec = now - first
        if age_sec <= threshold:
            continue
        age_min = age_sec / 60.0
        subject = f'managed-committer-wedged:{committer}'
        msg = (
            f'Runtime path `{path}` has been continuously uncommitted for '
            f'{age_min:.0f} min (> {threshold/60:.0f} min SLA = max(3x '
            f'{cadence_min:.0f}min cadence, 45min)). Its sole committer '
            f'`{committer}` appears WEDGED — the file keeps being written but '
            f'never committed.\n\n'
            f'Check the committer:\n'
            f'  systemctl status ourliberty-{committer.replace("_", "-")}.* \n'
            f'  journalctl -u "ourliberty-{committer.replace("_", "-")}*" '
            f'--since "1 hour ago" | tail -80'
        )
        if emit_alert(state, subject, msg,
                      suggested_action=f'Inspect committer {committer}; restart if crashed.',
                      now=now):
            fired = True
    save_state(state)
    return fired


# -------------------- main --------------------

def run_tick(now: Optional[float] = None) -> dict:
    """One healer tick. Returns a small dict of per-signal trip booleans for tests."""
    result = {'ahead': False, 'behind': False, 'uncommitted': False,
              'committer_wedged': False, 'fetched': False}
    if not fetch_origin():
        return result
    result['fetched'] = True
    upstream = remote_tracking_ref()
    if upstream is None:
        log('no upstream configured for HEAD; skipping ahead/behind checks')
        upstream_branch = None
    else:
        upstream_branch = upstream
    branch = current_branch()
    state = load_state()
    if upstream_branch is not None:
        result['ahead'] = evaluate_ahead(upstream_branch, branch, state, now=now)
        result['behind'] = evaluate_behind(upstream_branch, branch, state, now=now)
    result['uncommitted'] = evaluate_uncommitted(branch, state, now=now)
    result['committer_wedged'] = evaluate_managed_committer_wedged(
        branch, state, now=now)
    tripped = [k for k, v in result.items() if v and k != 'fetched']
    log(f'GIT_DRIFT_CHECK ahead={int(result["ahead"])} '
        f'behind={int(result["behind"])} '
        f'uncommitted={int(result["uncommitted"])} '
        f'committer_wedged={int(result["committer_wedged"])} '
        f'tripped={tripped}')
    return result


def main() -> int:
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return 0
    heartbeat()
    run_tick()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
