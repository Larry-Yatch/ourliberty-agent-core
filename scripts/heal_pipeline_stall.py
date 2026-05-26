#!/usr/bin/env python3
"""
heal_pipeline_stall.py — Proactively DM Larry when work stops flowing.

Adapted from GrowthMastery-ai/gm-agent-core/scripts/pipeline_watcher.py
(2026-04-15, Joe's "you never have to discover a stall on your own"
doctrine). Codifies the manager-duty pattern that Larry + Claude have
been doing manually each session: scan the pipeline state, identify
stalls, DM Larry with what stalled + recommended action.

Runs every 15 minutes via systemd timer. Silent unless something needs
action. Never self-heals; never auto-dispatches. Five concrete checks:

  1. **Forge built but no PR opened** — `[forge] done task=X
     success=True` >2h ago AND no PR exists with `forge/X` branch
     on any tracked repo.

  2. **PR opened but no Mirror review-request dispatched** — PR on a
     `forge/` branch exists, but no `review-request dispatched mirror`
     log line for that task_id. Threshold: 30 min for short doc
     reviews, 60 min for code reviews where regression-check runs.

  3. **Mirror PASS but PR still OPEN** — `marker-notified beacon <-
     mirror (mirror-result, intent=review-pass)` >30 min ago, but the
     PR for that task_id is still OPEN. Catches the gap that hit
     PR #101 + PR #104 (Mirror approved but auto-merge never fired,
     either because of marker-shape drift or a notifier crash). The
     existing `heal_pr_auto_merge.py` covers the case where AUTO_MERGE
     fired and failed; this check covers the case where AUTO_MERGE
     never fired in the first place.

  4. **Mirror reviewed but no marker classified** — `notified beacon
     <- mirror (mirror-result, depth=1)` log line exists for a task,
     but no subsequent `marker-notified beacon <- mirror` for the same
     task_id, and time elapsed > 30 min. Catches Mirror marker-shape
     drift (REVIEW_RESULT wrappers, inline REVIEW_PASS:+JSON, etc.)
     that the PR #103 always-scan parser doesn't recognize. This bit
     us three times yesterday across PR #104's review chain.

  5. **Retry-cap exhausted in last 30 min** — `All retries exhausted`
     log lines from inbox-watcher journal in the recent window. Surfaces
     when an agent is hard-failing repeatedly and stuck in dead-letter.

Every alert: ONE Telegram DM via `larry_alerts.append_alert` (cooldown
1h per unique stall key). Each alert states: what stalled, how long,
recommended action, the specific log grep to run for details.

Never acts on the stall. Surface only. The healer never kills processes,
never merges PRs, never re-dispatches anything. Same posture as Joe's
`pipeline_watcher.py` and our `dispatch_sentinel.py`.

State: `~/agents/blackboard/heal-pipeline-stall-state.json`
Heartbeat: `~/agents/blackboard/heal-pipeline-stall.heartbeat`
Kill switch: `~/agents/healers.disabled`
Log: `~/agents/logs/heal-pipeline-stall.log`

Phase E4 followup, 2026-05-26.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import larry_alerts  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-pipeline-stall.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-pipeline-stall.heartbeat'
STATE_FILE = AGENTS_ROOT / 'blackboard' / 'heal-pipeline-stall-state.json'
OUTBOX_NOTIFIER_LOG = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'
# `[forge] done task=X success=True` lines are emitted by inbox_watcher.py to
# THIS file, NOT by outbox_notifier.py to OUTBOX_NOTIFIER_LOG. Check 1 reads
# inbox_watcher.log; all other checks (review-request dispatched,
# marker-notified, AUTO_MERGE, notified depth=1) read OUTBOX_NOTIFIER_LOG.
# Mirror PR #107 review (2026-05-26) caught this — original Check 1 silently
# no-op'd because it grepped the wrong file.
INBOX_WATCHER_LOG = AGENTS_ROOT / 'logs' / 'inbox_watcher.log'

# Repos with active dispatch chains. Mirrors heal_pr_auto_merge.REPOS.
REPOS = [
    'Larry-Yatch/ourliberty-agent-core',
    'Larry-Yatch/ourliberty-dashboard',
]

GH_TIMEOUT_S = 30

# Stall thresholds. All in minutes.
FORGE_BUILT_NO_PR_MIN = 120          # 2h — Forge done but no PR opened
PR_NO_MIRROR_DISPATCH_MIN = 30       # 30 min — PR opened but no review dispatched
PR_NO_MIRROR_DISPATCH_CODE_MIN = 60  # 60 min — same but for code PRs (regression check legitimate)
MIRROR_PASS_UNMERGED_MIN = 30        # 30 min — PASS marker classified, PR still OPEN
MIRROR_MARKER_INVISIBLE_MIN = 30     # 30 min — Mirror notified but no marker classified
RETRY_EXHAUST_WINDOW_MIN = 30        # 30 min — All retries exhausted in this window
LOG_LOOKBACK_HOURS = 24              # Read at most this far back into outbox-notifier.log
JOURNAL_LOOKBACK_HOURS = 1           # Read at most this far back for retry-exhausted lines
ALERT_DEDUP_HOURS = 1                # Same stall key not re-DMed within this window

# Log line patterns from outbox_notifier's log() helper.
_FORGE_DONE_RE = re.compile(
    r'\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]'
    r'.*?\[forge\]\s+done\s+task=(?P<task>\S+)\s+success=True'
)
_REVIEW_REQUEST_DISPATCHED_RE = re.compile(
    r'\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]'
    r'.*review-request dispatched mirror.*task=(?P<task>\S+?),'
)
_MARKER_NOTIFIED_MIRROR_RE = re.compile(
    r'\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]'
    r'.*marker-notified beacon <- mirror.*intent=(?P<intent>review-\S+?),\s*file=notify-(?P<task>\S+?)\.json'
)
_NOTIFIED_MIRROR_GENERIC_RE = re.compile(
    r'\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]'
    r'.*\[INFO\] notified beacon <- mirror.*depth=\d+.*file=notify-(?P<task>\S+?)\.json'
)
_AUTO_MERGE_MERGED_RE = re.compile(
    r'\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]'
    r'.*AUTO_MERGE task=(?P<task>\S+).*outcome=merged'
)


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
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat() + '\n')
    except OSError as e:
        log(f'heartbeat write failed: {e}', 'WARN')


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f'load_state failed: {e} — starting fresh', 'WARN')
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def should_alert(state: dict, key: str) -> bool:
    """True if this stall key hasn't been DMed in the last ALERT_DEDUP_HOURS."""
    last = state.get(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace('Z', '+00:00'))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last_dt) > timedelta(hours=ALERT_DEDUP_HOURS)


def record_alert(state: dict, key: str) -> None:
    state[key] = datetime.now(timezone.utc).isoformat()


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """Parse the timestamp shape outbox_notifier writes. Tolerates space-or-T,
    optional microseconds, optional tz suffix."""
    s = ts_str.strip().replace(' ', 'T')
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    # Normalize +HHMM to +HH:MM if needed
    if re.search(r'[+-]\d{4}$', s):
        s = s[:-2] + ':' + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_recent_log_lines(log_path: Path, hours: int) -> list[str]:
    """Return lines from log_path whose timestamp is within `hours` of now.
    Tolerates missing/empty file."""
    if not log_path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    try:
        with open(log_path, errors='replace') as f:
            for line in f:
                m = re.match(r'\[(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
                if not m:
                    continue
                ts = _parse_ts(m.group(1))
                if ts and ts >= cutoff:
                    out.append(line.rstrip('\n'))
    except OSError as e:
        log(f'read {log_path} failed: {e}', 'WARN')
    return out


def gh_pr_list(repo: str, state: str = 'all', limit: int = 50) -> list[dict]:
    """Call `gh pr list` and return parsed JSON list. Returns [] on failure."""
    try:
        result = subprocess.run(
            ['gh', 'pr', 'list', '--repo', repo, '--state', state,
             '--limit', str(limit), '--json',
             'number,title,state,mergedAt,headRefName,createdAt'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S,
            env={**os.environ, 'PATH': '/usr/bin:/usr/local/bin:' + os.environ.get('PATH', '')},
        )
        if result.returncode != 0:
            log(f'gh pr list {repo} returned {result.returncode}: {result.stderr[:200]}', 'WARN')
            return []
        return json.loads(result.stdout or '[]')
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        log(f'gh pr list {repo} failed: {type(e).__name__}: {e}', 'WARN')
        return []


def _all_open_prs() -> list[dict]:
    """Return all OPEN PRs across tracked repos, augmented with `_repo`."""
    out = []
    for repo in REPOS:
        for pr in gh_pr_list(repo, state='open', limit=50):
            pr['_repo'] = repo
            out.append(pr)
    return out


def _all_merged_prs_recent() -> list[dict]:
    """Return MERGED PRs across tracked repos in the last 7 days, augmented
    with `_repo`. Used to detect PRs that merged after a Mirror PASS so we
    can skip the still-OPEN check."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    out = []
    for repo in REPOS:
        for pr in gh_pr_list(repo, state='merged', limit=30):
            merged = pr.get('mergedAt')
            if not merged:
                continue
            ts = _parse_ts(merged)
            if ts and ts >= cutoff:
                pr['_repo'] = repo
                out.append(pr)
    return out


def _task_id_from_branch(branch: str) -> Optional[str]:
    """Extract task_id from a `forge/<task_id>` or `larry/<task_id>` branch.
    Returns None if the branch isn't a recognized chain pattern."""
    for prefix in ('forge/', 'larry/'):
        if branch.startswith(prefix):
            return branch[len(prefix):]
    return None


# ---------- Check 1: Forge built but no PR opened ----------

def check_forge_built_no_pr(watcher_lines: list[str], open_prs: list[dict],
                            merged_prs: list[dict], state: dict) -> list[dict]:
    """Find Forge build-done lines >FORGE_BUILT_NO_PR_MIN ago where no PR
    matches the task_id on any tracked repo. Returns list of alert dicts.

    Reads from `inbox_watcher.log` (NOT `outbox-notifier.log`) — the
    `[forge] done task=X success=True` shape is emitted by
    `inbox_watcher.py` to its own log. Verified against production logs
    (Mirror PR #107 review): outbox-notifier.log has zero matches for
    this pattern; inbox_watcher.log has hundreds."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=FORGE_BUILT_NO_PR_MIN)
    all_prs = open_prs + merged_prs
    alerts: list[dict] = []
    seen_tasks: set[str] = set()
    for line in watcher_lines:
        m = _FORGE_DONE_RE.search(line)
        if not m:
            continue
        task = m.group('task')
        if task in seen_tasks:
            continue
        ts = _parse_ts(m.group('ts'))
        if not ts or ts > cutoff:
            continue
        # PR with matching branch on any repo?
        if any(_task_id_from_branch(pr.get('headRefName', '')) == task for pr in all_prs):
            seen_tasks.add(task)
            continue
        # No matching PR — stall
        elapsed_min = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
        key = f'forge_built_no_pr:{task}'
        alerts.append({
            'key': key,
            'message': (f'Forge built task `{task}` {elapsed_min} min ago but no PR was opened. '
                        f'Worker may have crashed after build, or `gh pr create` silently failed.'),
            'subject': f'pipeline-stall:forge-no-pr:{task}',
            'suggested_action': (
                f'Check worktree state: `ssh larry@droplet "ls /home/larry/agent-worktrees/wt-forge-{task[:40]}*"`. '
                f'Inspect Forge session log + `git -C <worktree> status`. If branch exists locally but PR is missing, '
                f'run `gh pr create` manually in the worktree.'),
        })
        seen_tasks.add(task)
    return alerts


# ---------- Check 2: PR opened but no Mirror review-request dispatched ----------

def check_pr_no_mirror_dispatch(notifier_lines: list[str], open_prs: list[dict],
                                state: dict) -> list[dict]:
    """Find OPEN PRs on `forge/` branches where no `review-request dispatched
    mirror` log line exists for that task_id, and PR age exceeds threshold."""
    now = datetime.now(timezone.utc)
    # Build set of task_ids that have had a review dispatch.
    dispatched: set[str] = set()
    for line in notifier_lines:
        m = _REVIEW_REQUEST_DISPATCHED_RE.search(line)
        if m:
            dispatched.add(m.group('task'))
    alerts: list[dict] = []
    for pr in open_prs:
        branch = pr.get('headRefName', '')
        task = _task_id_from_branch(branch)
        if not task or not branch.startswith('forge/'):
            continue
        created = _parse_ts(pr.get('createdAt', ''))
        if not created:
            continue
        elapsed_min = int((now - created).total_seconds() / 60)
        # Threshold gating: code PRs get longer (regression check is legitimate work).
        # Heuristic: any PR title starting with `fix(`, `feat(`, `refactor(` is code; everything
        # else (`docs(`, `spec(`, `chore(`) gets the shorter threshold.
        title = (pr.get('title') or '').lower()
        is_code = title.startswith(('fix(', 'feat(', 'refactor(', 'perf(', 'build('))
        threshold = PR_NO_MIRROR_DISPATCH_CODE_MIN if is_code else PR_NO_MIRROR_DISPATCH_MIN
        if elapsed_min < threshold:
            continue
        if task in dispatched:
            continue
        key = f'pr_no_mirror_dispatch:{task}'
        alerts.append({
            'key': key,
            'message': (f'PR #{pr["number"]} ({pr["_repo"]}) on `{branch}` opened {elapsed_min} min ago '
                        f'but Mirror review was never dispatched for task `{task}`.'),
            'subject': f'pipeline-stall:no-mirror-dispatch:PR#{pr["number"]}',
            'suggested_action': (
                f'Check outbox-notifier log for the task: '
                f'`grep "{task}" ~/agents/logs/outbox-notifier.log | tail -20`. '
                f'If Forge notify never fired or the build-phase dispatch was suppressed, '
                f'a manual review dispatch via Beacon is the unstick path.'),
        })
    return alerts


# ---------- Check 3: Mirror PASS but PR still OPEN ----------

def check_mirror_pass_unmerged(notifier_lines: list[str], open_prs: list[dict],
                               state: dict) -> list[dict]:
    """Find `marker-notified ... intent=review-pass` lines >MIRROR_PASS_UNMERGED_MIN
    ago where the corresponding PR is still OPEN on a tracked repo."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MIRROR_PASS_UNMERGED_MIN)
    # Build map task_id -> latest PASS timestamp.
    pass_times: dict[str, datetime] = {}
    for line in notifier_lines:
        m = _MARKER_NOTIFIED_MIRROR_RE.search(line)
        if not m or m.group('intent') != 'review-pass':
            continue
        ts = _parse_ts(m.group('ts'))
        if not ts:
            continue
        task = m.group('task')
        prev = pass_times.get(task)
        if prev is None or ts > prev:
            pass_times[task] = ts
    # Cross-reference with OPEN PRs by branch.
    alerts: list[dict] = []
    for pr in open_prs:
        task = _task_id_from_branch(pr.get('headRefName', ''))
        if not task or task not in pass_times:
            continue
        ts = pass_times[task]
        if ts > cutoff:
            continue
        elapsed_min = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
        key = f'mirror_pass_unmerged:{task}'
        alerts.append({
            'key': key,
            'message': (f'Mirror PASSED PR #{pr["number"]} ({pr["_repo"]}) for task `{task}` '
                        f'{elapsed_min} min ago, but the PR is still OPEN. AUTO_MERGE never fired or failed silently.'),
            'subject': f'pipeline-stall:mirror-pass-unmerged:PR#{pr["number"]}',
            'suggested_action': (
                f'Check AUTO_MERGE outcomes: '
                f'`grep "AUTO_MERGE.*{task}" ~/agents/logs/outbox-notifier.log`. '
                f'If outcome=failed, heal_pr_auto_merge.py should retry; if no AUTO_MERGE line at all, '
                f'manual merge: `gh pr merge {pr["number"]} --repo {pr["_repo"]} --squash --delete-branch`.'),
        })
    return alerts


# ---------- Check 4: Mirror reviewed but no marker classified ----------

def check_mirror_marker_invisible(notifier_lines: list[str], state: dict) -> list[dict]:
    """Find `notified beacon <- mirror (mirror-result, depth=1, file=notify-X.json)`
    lines >MIRROR_MARKER_INVISIBLE_MIN ago where no `marker-notified beacon <- mirror`
    line exists for the same task_id. Indicates marker-shape drift —
    Mirror emitted something, but the parser didn't recognize it.

    Note: skip tasks that ALREADY auto-merged after the notify (the auto-merge
    line implies the marker WAS eventually classified — usually via a follow-up
    Mirror dispatch). These aren't stalls."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MIRROR_MARKER_INVISIBLE_MIN)
    # Latest generic-notify per task.
    generic_notify: dict[str, datetime] = {}
    classified: set[str] = set()
    merged: set[str] = set()
    for line in notifier_lines:
        m = _NOTIFIED_MIRROR_GENERIC_RE.search(line)
        if m:
            ts = _parse_ts(m.group('ts'))
            task = m.group('task')
            if ts:
                prev = generic_notify.get(task)
                if prev is None or ts > prev:
                    generic_notify[task] = ts
            continue
        m = _MARKER_NOTIFIED_MIRROR_RE.search(line)
        if m:
            classified.add(m.group('task'))
            continue
        m = _AUTO_MERGE_MERGED_RE.search(line)
        if m:
            merged.add(m.group('task'))
    alerts: list[dict] = []
    for task, ts in generic_notify.items():
        # Strip Mirror notify prefix conventions: notify-review-pr-N-... → review-pr-N-...
        # We dedup against classified set which uses the same task_id extracted from file=notify-X.json
        if task in classified:
            continue
        if task in merged:
            continue
        if ts > cutoff:
            continue
        elapsed_min = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
        key = f'mirror_marker_invisible:{task}'
        alerts.append({
            'key': key,
            'message': (f'Mirror reviewed task `{task}` {elapsed_min} min ago but the marker parser did '
                        f'NOT classify her verdict. Likely marker-shape drift (REVIEW_RESULT wrapper or '
                        f'inline REVIEW_PASS:+JSON instead of canonical `=== REVIEW_VERDICT ===` block).'),
            'subject': f'pipeline-stall:marker-invisible:{task}',
            'suggested_action': (
                f'Read Mirror session log to confirm verdict + manually merge if PASS: '
                f'`F=$(find ~/.claude/projects -name "*.jsonl" -path "*mirror*{task[:30]}*" | tail -1); '
                f'grep -oE "=== REVIEW_(PASS\\|REVISION\\|ESCALATE\\|EMERGENCY_HALT) ===" "$F"`. '
                f'If she emitted a non-canonical shape, the marker.py discipline mandate from PR #105 '
                f'should have caught it; flag the recurrence.'),
        })
    return alerts


# ---------- Check 5: Retry-cap exhausted in last 30 min ----------

def check_retry_exhausted(state: dict) -> list[dict]:
    """Scan inbox-watcher journal for 'All retries exhausted' in the recent window."""
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=RETRY_EXHAUST_WINDOW_MIN)).strftime('%Y-%m-%d %H:%M:%S UTC')
    try:
        result = subprocess.run(
            ['journalctl', '-u', 'ourliberty-inbox-watcher.service',
             '--since', cutoff_iso, '--no-pager', '--output=cat'],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            log(f'journalctl returned {result.returncode}: {result.stderr[:200]}', 'WARN')
            return []
        lines = result.stdout.splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f'journalctl read failed: {type(e).__name__}: {e}', 'WARN')
        return []
    alerts: list[dict] = []
    seen_tasks: set[str] = set()
    for line in lines:
        if 'All retries exhausted' not in line:
            continue
        # Extract task_id where possible. inbox_watcher logs typically include task=<id>.
        m = re.search(r'task=(\S+)', line)
        task = m.group(1) if m else 'unknown'
        if task in seen_tasks:
            continue
        seen_tasks.add(task)
        key = f'retry_exhausted:{task}'
        alerts.append({
            'key': key,
            'message': (f'Task `{task}` exhausted its retry budget in the last {RETRY_EXHAUST_WINDOW_MIN} min. '
                        f'Likely dead-lettered. Manual investigation required.'),
            'subject': f'pipeline-stall:retry-exhausted:{task}',
            'suggested_action': (
                f'Check journal for full retry trace: '
                f'`journalctl -u ourliberty-inbox-watcher.service --since "30 min ago" | grep "{task}"`. '
                f'Inspect dead-letter dir: `ls /home/larry/agents/inboxes/*/.invalid/ | grep "{task}"`.'),
        })
    return alerts


# ---------- Main ----------

def run() -> int:
    """Single pass. Returns 0 always (healer never fails systemd)."""
    if KILL_SWITCH.exists():
        log('kill switch present — exiting', 'INFO')
        return 0
    heartbeat()
    state = load_state()
    try:
        notifier_lines = _read_recent_log_lines(OUTBOX_NOTIFIER_LOG, LOG_LOOKBACK_HOURS)
        watcher_lines = _read_recent_log_lines(INBOX_WATCHER_LOG, LOG_LOOKBACK_HOURS)
        open_prs = _all_open_prs()
        merged_prs = _all_merged_prs_recent()
    except Exception as e:
        log(f'pre-flight read failed: {type(e).__name__}: {e}', 'ERROR')
        return 0

    all_alerts: list[dict] = []
    try:
        all_alerts += check_forge_built_no_pr(watcher_lines, open_prs, merged_prs, state)
    except Exception as e:
        log(f'check_forge_built_no_pr failed: {type(e).__name__}: {e}', 'ERROR')
    try:
        all_alerts += check_pr_no_mirror_dispatch(notifier_lines, open_prs, state)
    except Exception as e:
        log(f'check_pr_no_mirror_dispatch failed: {type(e).__name__}: {e}', 'ERROR')
    try:
        all_alerts += check_mirror_pass_unmerged(notifier_lines, open_prs, state)
    except Exception as e:
        log(f'check_mirror_pass_unmerged failed: {type(e).__name__}: {e}', 'ERROR')
    try:
        all_alerts += check_mirror_marker_invisible(notifier_lines, state)
    except Exception as e:
        log(f'check_mirror_marker_invisible failed: {type(e).__name__}: {e}', 'ERROR')
    try:
        all_alerts += check_retry_exhausted(state)
    except Exception as e:
        log(f'check_retry_exhausted failed: {type(e).__name__}: {e}', 'ERROR')

    if not all_alerts:
        log('no stalls detected', 'INFO')
        save_state(state)
        return 0

    fired = 0
    for alert in all_alerts:
        if not should_alert(state, alert['key']):
            log(f'suppressed (cooldown): {alert["key"]}', 'INFO')
            continue
        ok = larry_alerts.append_alert(
            source='heal-pipeline-stall',
            severity='warning',
            message=alert['message'],
            subject=alert['subject'],
            suggested_action=alert['suggested_action'],
        )
        if ok:
            record_alert(state, alert['key'])
            fired += 1
            log(f'alerted: {alert["key"]}', 'INFO')
        else:
            log(f'larry_alerts append failed for {alert["key"]}', 'WARN')
    log(f'done: {fired} new alert(s) fired, {len(all_alerts) - fired} suppressed', 'INFO')
    save_state(state)
    return 0


if __name__ == '__main__':
    sys.exit(run())
