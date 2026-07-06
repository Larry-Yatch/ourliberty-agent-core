#!/usr/bin/env python3
"""
agent_core_health_check.py — Detect and fix ourliberty-agent-core repo drift.

Runs hourly via systemd timer. Five health checks:

  1. Branch == main
  2. Working tree clean (no uncommitted modifications)
  3. No untracked files outside .gitignore patterns
  4. Local main is in sync with origin/main (or can fast-forward)
  5. sync_agent_core.sh has run successfully recently (<= 6 hours ago)

For each check, the script either AUTO-FIXES (safe cases) or ALERTS Larry
via the configured notify channel (cases requiring human judgment).

Auto-fix policy (intentionally narrow — never destructive):
  - On main + behind origin + clean tree → fast-forward (safe)
  - Stale sync run + clean repo → trigger sync_agent_core.sh
  - Anything else → alert Larry, do NOT mutate

This system exists because the ourliberty-agent-core repo silently drifted onto
a feature branch with 20+ uncommitted modifications for ~2 days before
being noticed (2026-04-29). The drift broke sync_agent_core.sh, so live
agent prompts went unsynced. Periodic monitoring + alerting closes that
gap so divergence cannot persist undetected.

Usage:
  agent_core_health_check.py            # Run all checks, autofix where safe
  agent_core_health_check.py --dry-run  # Report only, no mutations
  agent_core_health_check.py --json     # Machine-readable output
  agent_core_health_check.py --quiet    # No stdout, only alerts on issues

Exit codes:
  0 — repo is healthy (or healthy after autofix)
  1 — repo needs human attention; alert dispatched

Part of /sweep upgrades extension (ourliberty-agent-core hygiene), 2026-04-29.
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_DIR = Path('/home/larry/agent-core')
SCRIPTS_DIR = Path('/home/larry/agents/scripts')
SYNC_SCRIPT = SCRIPTS_DIR / 'sync_agent_core.sh'
SYNC_STATUS_FILE = Path('/home/larry/agents/blackboard/agent-core-sync.json')
HEALTH_LOG = Path('/home/larry/agents/blackboard/agent-core-health.log')
HEALTH_STATE_FILE = Path('/home/larry/agents/blackboard/agent-core-health-state.json')
NOTIFY_SCRIPT = SCRIPTS_DIR / 'notify_larry.py'

SYNC_STALE_HOURS = 6
GIT_TIMEOUT = 30


def log(msg: str, quiet: bool = False) -> None:
    line = f'[{datetime.now(timezone.utc).isoformat()}] {msg}'
    if not quiet:
        print(line, flush=True)
    try:
        HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(HEALTH_LOG, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def git(*args: str) -> tuple[int, str, str]:
    """Run a git command in REPO_DIR. Returns (rc, stdout, stderr)."""
    try:
        result = subprocess.run(
            ['git', *args],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, '', str(e)


def alert_larry(subject: str, message: str) -> bool:
    if not NOTIFY_SCRIPT.exists():
        log(f'WARN: notify script missing, alert dropped: {subject}')
        return False
    try:
        subprocess.run(
            ['python3', str(NOTIFY_SCRIPT),
             '--tier', 'breakdown',
             '--subject', f'ourliberty-agent-core health: {subject}',
             '--message', message],
            timeout=15,
            capture_output=True,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f'WARN: alert send failed: {e}')
        return False


def read_prior_issue_names() -> set[str]:
    """Issue check-names flagged on the previous run (for the persist-2-runs gate)."""
    try:
        with open(HEALTH_STATE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()
    names = data.get('issue_names', [])
    return set(names) if isinstance(names, list) else set()


def write_issue_names(names) -> None:
    """Persist the current run's issue check-names for the next run to compare."""
    try:
        HEALTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_STATE_FILE.with_name(HEALTH_STATE_FILE.name + '.tmp')
        with open(tmp, 'w') as f:
            json.dump(
                {'issue_names': sorted(names),
                 'updated': datetime.now(timezone.utc).isoformat()},
                f,
            )
        os.replace(tmp, HEALTH_STATE_FILE)
    except OSError as e:
        log(f'WARN: could not persist health state: {e}')


def persisted_issues(issues, prior_names):
    """Issues from this run that were ALSO flagged on the previous run.

    The persist-2-runs gate: an issue alerts only once it has survived two
    consecutive runs, so the transient mid-cycle dirty-tree window self-clears
    instead of paging.
    """
    return [(name, result) for name, result in issues if name in prior_names]


def _is_push_fail_only(persisted) -> bool:
    """True when the persisted issues are EXACTLY the sync push-fail signal.

    The push-fail is origin_sync in its 'ahead/unpushed-commits' shape — the same
    routine hiccup the sync.service sync-blocked digest already DMs Larry. When
    that's the sole persisted issue, the summary escalate DM is a duplicate and is
    suppressed. Any other persisted issue, or origin_sync in a different shape
    (diverged, fetch-failed) which the digest does NOT cover, returns False so the
    DM still fires.
    """
    if len(persisted) != 1:
        return False
    name, result = persisted[0]
    return name == 'origin_sync' and result.get('signal') == 'unpushed-commits'


def check_branch() -> dict:
    rc, branch, _ = git('rev-parse', '--abbrev-ref', 'HEAD')
    if rc != 0:
        return {'ok': False, 'detail': 'cannot read branch', 'autofix': None}
    if branch == 'main':
        return {'ok': True, 'branch': branch}
    return {
        'ok': False,
        'branch': branch,
        'detail': f'on {branch!r}, expected main',
        'autofix': None,
        'remediation': (
            f"ssh ourliberty-vm; cd /home/larry/agent-core; review branch '{branch}' "
            f"with 'git log --oneline main..{branch}'; merge to main if work is done, "
            f"otherwise stash and switch."
        ),
    }


def check_clean_tree() -> dict:
    rc, status_out, _ = git('status', '--porcelain')
    if rc != 0:
        return {'ok': False, 'detail': 'cannot read status', 'autofix': None}
    if not status_out:
        return {'ok': True}
    lines = status_out.splitlines()
    modified = [l for l in lines if l[:2].strip() and not l.startswith('??')]
    untracked = [l for l in lines if l.startswith('??')]
    return {
        'ok': False,
        'detail': f'{len(modified)} modified, {len(untracked)} untracked',
        'modified': modified[:10],
        'untracked': untracked[:10],
        'autofix': None,
        'remediation': (
            'ssh ourliberty-vm; cd /home/larry/agent-core; review with git status; '
            'commit changes (per direct-commit-to-main rule), or git stash if WIP.'
        ),
    }


def check_origin_sync(autofix: bool) -> dict:
    rc, _, _ = git('fetch', 'origin', 'main', '--quiet')
    if rc != 0:
        return {'ok': False, 'detail': 'fetch failed', 'autofix': None}

    rc, local_head, _ = git('rev-parse', 'HEAD')
    if rc != 0:
        return {'ok': False, 'detail': 'cannot read HEAD', 'autofix': None}

    rc, remote_head, _ = git('rev-parse', 'origin/main')
    if rc != 0:
        return {'ok': False, 'detail': 'cannot read origin/main', 'autofix': None}

    if local_head == remote_head:
        return {'ok': True, 'head': local_head[:8]}

    rc, _, _ = git('merge-base', '--is-ancestor', local_head, remote_head)
    is_behind = rc == 0
    rc, _, _ = git('merge-base', '--is-ancestor', remote_head, local_head)
    is_ahead = rc == 0

    if is_behind and not is_ahead:
        if autofix:
            rc, _, err = git('merge', '--ff-only', 'origin/main', '--quiet')
            if rc == 0:
                return {'ok': True, 'autofixed': 'fast-forwarded to origin/main'}
            return {
                'ok': False,
                'detail': f'fast-forward attempted but failed: {err}',
                'autofix': 'attempted',
                'remediation': 'ssh ourliberty-vm; investigate why fast-forward failed.',
            }
        return {
            'ok': False,
            'detail': 'local behind origin/main (would auto-ff but --dry-run)',
            'autofix': 'would-fast-forward',
        }

    if is_ahead and not is_behind:
        return {
            'ok': False,
            # Stable machine key: this is the push-fail shape already surfaced to
            # Larry by the sync.service sync-blocked digest, so the summary-alert
            # guard in main() suppresses the duplicate DM when it's the only issue.
            'signal': 'unpushed-commits',
            'detail': 'local ahead of origin/main (unpushed commits)',
            'autofix': None,
            'remediation': (
                'ssh ourliberty-vm; cd /home/larry/agent-core; git push origin main; '
                'investigate what wasn\'t pushed.'
            ),
        }

    return {
        'ok': False,
        'detail': 'local and origin/main have DIVERGED',
        'autofix': None,
        'remediation': (
            'ssh ourliberty-vm; cd /home/larry/agent-core; git log --oneline '
            'origin/main...HEAD; reconcile by rebasing local commits onto origin '
            '(or vice versa, depending on which commits are canonical).'
        ),
    }


def check_sync_freshness() -> dict:
    if not SYNC_STATUS_FILE.exists():
        return {
            'ok': False,
            'detail': 'sync status file missing',
            'autofix': 'trigger-sync',
        }
    try:
        with open(SYNC_STATUS_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {'ok': False, 'detail': f'sync status unreadable: {e}'}

    last_sync = data.get('last_sync', '')
    if not last_sync:
        return {'ok': False, 'detail': 'no last_sync recorded'}
    try:
        ts = datetime.fromisoformat(last_sync.replace('Z', '+00:00'))
    except ValueError:
        return {'ok': False, 'detail': f'unparseable last_sync: {last_sync}'}

    age = datetime.now(timezone.utc) - ts
    age_hours = age.total_seconds() / 3600
    status = data.get('status', 'unknown')

    if status == 'error':
        return {
            'ok': False,
            'detail': f'last sync ERRORED {age_hours:.1f}h ago: {data.get("message", "")}',
            'autofix': None,
            'remediation': 'tail /home/larry/agents/blackboard/agent-core-sync.json',
        }

    if age_hours > SYNC_STALE_HOURS:
        return {
            'ok': False,
            'detail': f'last sync was {age_hours:.1f}h ago (threshold {SYNC_STALE_HOURS}h)',
            'autofix': 'trigger-sync',
        }
    return {'ok': True, 'last_sync': last_sync, 'age_hours': round(age_hours, 1), 'status': status}


def trigger_sync(autofix: bool) -> dict:
    if not autofix:
        return {'ok': False, 'detail': 'sync would be triggered (dry-run)'}
    if not SYNC_SCRIPT.exists():
        return {'ok': False, 'detail': 'sync script missing'}
    try:
        result = subprocess.run(
            ['bash', str(SYNC_SCRIPT)],
            timeout=600,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return {'ok': True, 'detail': 'sync_agent_core.sh ran cleanly'}
        return {
            'ok': False,
            'detail': f'sync exited {result.returncode}: {result.stderr[-500:]}',
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'detail': 'sync timed out (>10 min)'}


def main() -> int:
    p = argparse.ArgumentParser(description='ourliberty-agent-core repo health check')
    p.add_argument('--dry-run', action='store_true', help='Report only, no mutations')
    p.add_argument('--json', action='store_true', help='Machine-readable output')
    p.add_argument('--quiet', action='store_true', help='No stdout (alerts still fire)')
    args = p.parse_args()

    autofix = not args.dry_run
    quiet = args.quiet

    log('Starting health check', quiet=quiet)

    results = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'branch': check_branch(),
        'clean_tree': check_clean_tree(),
        'sync_freshness': check_sync_freshness(),
    }
    results['origin_sync'] = check_origin_sync(autofix)

    issues: list[tuple[str, dict]] = []
    auto_fixed: list[str] = []

    for name, result in results.items():
        if name == 'timestamp':
            continue
        if isinstance(result, dict) and not result.get('ok', True):
            if result.get('autofixed'):
                auto_fixed.append(f"{name}: {result['autofixed']}")
            else:
                issues.append((name, result))

    if results['sync_freshness'].get('autofix') == 'trigger-sync' and not issues:
        log('Sync stale but repo healthy — triggering sync', quiet=quiet)
        sync_result = trigger_sync(autofix)
        results['sync_triggered'] = sync_result
        if sync_result.get('ok'):
            auto_fixed.append('sync_freshness: triggered sync_agent_core.sh successfully')

    # Persist-2-runs guard (actionable-only). The working tree is briefly dirty
    # and/or has untracked files while Pulse commits mid-cycle; that single-run
    # blip clears before the next run, so alerting on it is noise. Only alert
    # about an issue that was ALSO flagged on the previous run — genuine drift
    # persists run-over-run and still surfaces (one run later), while transients
    # self-clear. State is keyed on the set of failing check-names.
    alerted = False
    if issues:
        log(f'{len(issues)} issue(s) require human attention', quiet=quiet)
        prior_names = read_prior_issue_names()
        current_names = {name for name, _ in issues}
        if not args.dry_run:
            write_issue_names(current_names)
        persisted = persisted_issues(issues, prior_names)
        if persisted and _is_push_fail_only(persisted):
            # De-dup guard: the ONLY persisted issue is the sync push-fail
            # (origin_sync ahead/unpushed), which the sync.service sync-blocked
            # digest already reported to Larry. Skip the summary escalate DM so
            # he isn't pinged twice for the same routine hiccup. Any other
            # persisted issue (or a different origin_sync shape) still DMs below.
            log('Persisted issue is ONLY the sync push-fail (origin_sync unpushed-'
                'commits) already covered by the sync-blocked digest — intentionally '
                'skipping duplicate summary DM (dedup guard)', quiet=quiet)
        elif persisted:
            sections = []
            for name, result in persisted:
                detail = result.get('detail', 'unknown')
                remediation = result.get('remediation', '(no remediation hint)')
                sections.append(f'• {name}: {detail}\n  → {remediation}')
            message = (
                'ourliberty-agent-core repo health check found issues that need your judgment:\n\n'
                + '\n\n'.join(sections)
                + '\n\nFull state in /home/larry/agents/blackboard/agent-core-health.log'
            )
            if args.dry_run:
                log(f'Would alert Larry: {len(persisted)} issue(s) persisted across '
                    f'2 runs (dry-run — no DM sent)', quiet=quiet)
            else:
                alerted = alert_larry(f'{len(persisted)} issue(s) need attention', message)
                log(f'Alerted Larry: {len(persisted)} issue(s) persisted across 2 runs',
                    quiet=quiet)
        else:
            log('Issue(s) present but none persisted across 2 consecutive runs — '
                'suppressing alert (actionable-only guard; transient mid-cycle window)',
                quiet=quiet)
    elif not args.dry_run:
        # Healthy: clear the prior-run signature so a future blip starts fresh.
        write_issue_names(set())

    if auto_fixed:
        log(f'Auto-fixed: {auto_fixed}', quiet=quiet)

    if args.json:
        print(json.dumps(results, indent=2))
    elif not quiet:
        for name, result in results.items():
            if name == 'timestamp':
                continue
            ok = '✓' if isinstance(result, dict) and result.get('ok', True) else '✗'
            print(f'  {ok} {name}: {result}')
        if auto_fixed:
            print(f'\nAuto-fixed: {", ".join(auto_fixed)}')
        if issues:
            status = ('Larry alerted via notify' if alerted
                      else 'alert held (not yet persisted across 2 runs)')
            print(f'\nNeeds attention: {len(issues)} issue(s) — {status}.')

    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main())
