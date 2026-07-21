#!/usr/bin/env python3
"""
heal_pr_auto_merge.py — Defense-in-depth auto-merge healer.

Phase E1.3. The primary auto-merge path lives in outbox_notifier._auto_merge_pr
(shipped D3.5 5d): when outbox_notifier processes a Mirror REVIEW_PASS marker,
it immediately fires `gh pr merge --squash --delete-branch` and DMs Larry the
outcome. That path catches the common case.

This healer fills the residual gaps where the primary path missed:
  - outbox_notifier crashed between PASS-process and merge call
  - gh pr merge returned non-zero (CI wasn't green yet, network blip,
    transient API error) — primary logs outcome=failed and moves on; the
    PR sits unmerged with no further retry
  - Mirror PASS landed in an outbox the notifier never processed (paused,
    daemon stopped, race with restart)
  - Race: PR was merge-conflicted at PASS time; conflict later resolved
    manually but no one re-triggered merge

How it identifies "should-be-merged PRs": scans ~/agents/logs/outbox-notifier.log
for `AUTO_MERGE task=<id> pr=<url> outcome=failed` entries in the last 24h,
collects the PR URLs, and cross-references with the open-PR list. Only PRs
that had a Mirror PASS that failed to merge are considered — the healer
never auto-merges a PR that Mirror never reviewed.

Adapted from GrowthMastery-ai/gm-agent-core/scripts/heal_pr_auto_merge.py
(2026-05-10 #240). What we pulled: is_mergeable defensive checks,
HOLD_PREFIXES + label gates, statusCheckRollup scan, MAX_MERGES_PER_RUN
blast-radius cap, CANCELLED-workflow rerun logic, "benign already-merged"
detection. What we did NOT pull: the `allow_auto_merge: false` premise
(our setup direct-merges, no --auto), walkthrough-needed gate (GM-specific),
multi-repo REPOS list (we have one repo today; will extend in E2).

What we added on top of upstream:
  - Mirror-PASS detection via outbox-notifier.log scan (upstream had no
    equivalent — they merged anything mergeable, no review-passed
    requirement, because GM's contract was different)
  - Per-PR retry budget with state file (~/agents/state/heal-pr-auto-merge.json)
    so a chronically-failing PR doesn't get hammered every 5 min
  - Telegram DM (via larry_alerts.append_alert with cooldown) when:
    (a) dry-run mode finds a candidate (one-time, tells Larry how to
        activate)
    (b) retry budget exhausts (one-time per PR, tells Larry to investigate
        manually)
  - Two-layer kill-switch: ~/agents/healers.disabled (blanket) AND
    OURLIBERTY_AUTOMERGE_ENABLED env var (default OFF -> dry-run mode).
    Per phase-e-plan: "default off until verified."

Safe-by-construction:
  - Kill-switch aware (exits immediately on healers.disabled)
  - Dry-run mode by default (logs candidates, doesn't merge; DMs Larry
    with activation instructions when it has actual work to do)
  - Bounded blast radius (MAX_MERGES_PER_RUN=5 per tick)
  - Per-PR retry budget (MAX_RETRIES_PER_PR=3)
  - Idempotent (already-merged PRs detected and treated as benign)
  - Read-only on Mirror's review state (consumes outbox-notifier.log only)
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
from atomic_io import atomic_write_json  # noqa: E402
from log_ts import parse_log_ts  # noqa: E402  (shared log-ts parser)
from test_isolation_guard import gh_write  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-pr-auto-merge.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-pr-auto-merge.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-pr-auto-merge.json'
OUTBOX_NOTIFIER_LOG = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'

# E3.2 extended REPOS to include ourliberty-dashboard (first Forge dispatch
# against a non-agent-core target). Healer reads Mirror PASS markers from
# outbox_notifier.log and tries to merge matching PRs across all repos here.
REPOS = ['Larry-Yatch/ourliberty-agent-core', 'Larry-Yatch/ourliberty-dashboard', 'Larry-Yatch/ourliberty-graph', 'Larry-Yatch/RSDPM']

GH_TIMEOUT_S = 30
MAX_MERGES_PER_RUN = 5
MAX_RETRIES_PER_PR = 3
# CANCELLED-workflow rerun budget (#24): a PR stuck non-CLEAN purely on
# CANCELLED runs is re-run via `gh run rerun`, but that path never counted
# against any budget — a perpetually-cancelled PR was re-run every tick forever.
MAX_RERUNS_PER_PR = 6
LOG_LOOKBACK_HOURS = 24
STALLED_LABEL = 'automerge-stalled'

# Activation env var. Per phase-e-plan E1.3: "default off until verified."
ENV_AUTOMERGE_ENABLED = 'OURLIBERTY_AUTOMERGE_ENABLED'

# Title-prefix and label gates pulled from upstream (case-insensitive).
HOLD_TITLE_PREFIXES = (
    'wip:', 'wip(', 'hold:', 'hold(', 'do-not-merge', 'do not merge',
    '[wip]', '[hold]', '[draft]',
)
HOLD_LABELS = ('do-not-merge', 'hold', 'wip', 'human-blocked')

# Regex matches AUTO_MERGE log entries in two upstream shapes:
#   AUTO_MERGE task=<id> pr=<url> outcome=failed reason=...
#   AUTO_MERGE task=<id> pr='<url>' outcome=failed reason=...   (repr'd path)
_AUTO_MERGE_FAILED_RE = re.compile(
    r"AUTO_MERGE\s+task=(\S+)\s+pr=['\"]?"
    r"(https?://[^\s'\"]+)['\"]?\s+outcome=failed"
    r"\s+reason=(.+?)(?:\s*$|\s*\Z)",
    re.MULTILINE,
)

# Log line timestamp prefix from outbox_notifier's log() helper. Loose match.
_LOG_TS_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})')


# -------------------- logging + heartbeat --------------------

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


# The registry template (config/auto-fix-patterns.json) this backstop merge
# path records for. Shares the template with outbox_notifier's primary
# auto-merge path; a given PR is merged by exactly one path, so recording in
# both captures every auto-merge without double-counting.
_AUTO_MERGE_TEMPLATE = 'auto-merge-clean-pr'


def _record_auto_merge_success() -> None:
    """Record one clean ``auto-merge-clean-pr`` execution toward Check V's
    graduation streak (delegates to the shared, registry-gated, never-raise
    ``alert_triage_state.record_clean_execution_if_registered``, PR #832 pattern).
    SUCCESS-ONLY, matching outbox_notifier — a gh merge failure is an unreliable
    action-quality signal (transient / infra), so the streak grows only on a
    verified landed merge and demotion comes from a Larry-correction. Additive/
    best-effort: it observes a merge that already happened and must never affect
    it (or the healer's retry loop)."""
    try:
        import alert_triage_state  # lazy: keep import surface light
        alert_triage_state.record_clean_execution_if_registered(
            _AUTO_MERGE_TEMPLATE)
    except Exception:  # never let a track-record write surface into heal
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- kill-switch + activation --------------------

def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def automerge_enabled() -> bool:
    """True iff OURLIBERTY_AUTOMERGE_ENABLED=true (case-insensitive)."""
    return os.environ.get(ENV_AUTOMERGE_ENABLED, '').strip().lower() == 'true'


# -------------------- state file (per-PR retry counter) --------------------

def load_state() -> dict[str, Any]:
    """Return {'prs': {pr_url: {'attempts': N, 'stalled_alerted': bool,
    'activation_alerted': bool, 'last_attempt_iso': str}}}."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'prs': {}}
        data.setdefault('prs', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'prs': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        atomic_write_json(STATE_FILE, state, indent=2)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def get_pr_state(state: dict[str, Any], pr_url: str) -> dict[str, Any]:
    entry = state['prs'].setdefault(pr_url, {})
    entry.setdefault('attempts', 0)
    entry.setdefault('rerun_attempts', 0)
    entry.setdefault('stalled_alerted', False)
    entry.setdefault('rerun_alerted', False)
    entry.setdefault('head_changed_alerted', False)
    entry.setdefault('activation_alerted', False)
    entry.setdefault('last_attempt_iso', None)
    return entry


# -------------------- DM to Larry --------------------

def dm_larry(message: str, subject: str, suggested_action: str,
             severity: str = 'warning') -> bool:
    """Fire a larry_alerts broadcast. Per-source-per-subject cooldown is
    handled inside larry_alerts. Returns True iff appended (False on
    cooldown-suppress or write error). Never raises."""
    try:
        # Lazy import — larry_alerts may not be on PYTHONPATH at import-time
        # if scripts/ isn't pre-loaded. The path insert below handles it.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-pr-auto-merge',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- gh CLI helpers --------------------

def gh_json(*args: str, default=None):
    """Run `gh <args>` capturing JSON stdout. Returns parsed JSON or default."""
    env = {**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/snap/bin'}
    try:
        result = subprocess.run(
            ['gh', *args],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=env,
        )
        if result.returncode != 0:
            log(f'gh {" ".join(args[:3])}... rc={result.returncode} '
                f'stderr={result.stderr.strip()[:200]}', 'WARN')
            return default
        out = result.stdout.strip()
        return json.loads(out) if out else default
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        log(f'gh {" ".join(args[:3])}... exception: {type(e).__name__}: {e}', 'WARN')
        return default


def list_open_prs(repo: str) -> list[dict]:
    prs = gh_json(
        'pr', 'list', '--repo', repo, '--state', 'open', '--limit', '40',
        '--json',
        'number,url,title,isDraft,mergeStateStatus,reviewDecision,'
        'statusCheckRollup,headRefName,labels',
        default=[],
    )
    for pr in prs:
        pr['_repo'] = repo
    return prs


def add_stalled_label(repo: str, pr_number: int) -> bool:
    env = {**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/snap/bin'}
    try:
        result = gh_write(
            ['gh', 'pr', 'edit', str(pr_number), '--repo', repo,
             '--add-label', STALLED_LABEL],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=env,
        )
        if result.returncode == 0:
            log(f'labeled {repo}#{pr_number} as {STALLED_LABEL}')
            return True
        log(f'add_stalled_label failed for {repo}#{pr_number}: '
            f'rc={result.returncode} stderr={result.stderr.strip()[:160]}', 'WARN')
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f'add_stalled_label exception: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- mergeability predicate --------------------

def is_mergeable(pr: dict) -> tuple[bool, str]:
    """Return (mergeable, reason). reason is for log clarity."""
    if pr.get('isDraft'):
        return False, 'draft'
    title_lc = (pr.get('title') or '').lower()
    if any(title_lc.startswith(p) for p in HOLD_TITLE_PREFIXES):
        return False, f'hold-prefix: {title_lc[:40]}'
    review = pr.get('reviewDecision') or ''
    if review == 'CHANGES_REQUESTED':
        return False, 'changes-requested'
    mss = pr.get('mergeStateStatus') or ''
    if mss != 'CLEAN':
        return False, f'merge-state={mss}'

    # Defensive: scan check rollup even though CLEAN usually implies it.
    for check in pr.get('statusCheckRollup') or []:
        status = check.get('status', '')
        concl = check.get('conclusion', '')
        if status == 'IN_PROGRESS':
            return False, f'check-in-progress: {check.get("name", "?")}'
        if status == 'COMPLETED' and concl not in (
                'SUCCESS', 'SKIPPED', 'NEUTRAL', ''):
            return False, f'check-failed: {check.get("name", "?")} ({concl})'

    label_names = {(lbl.get('name') or '').lower()
                   for lbl in pr.get('labels') or []}
    for hold_label in HOLD_LABELS:
        if hold_label in label_names:
            return False, f'label: {hold_label}'

    # If we've already labeled this PR as stalled, the operator is
    # investigating — back off until the label is removed manually.
    if STALLED_LABEL.lower() in label_names:
        return False, f'label: {STALLED_LABEL} (operator investigating)'

    return True, 'mergeable'


# -------------------- CANCELLED-workflow rerun (from upstream) --------------------

def find_cancelled_checks_to_rerun(pr: dict) -> list[dict]:
    """If the PR is non-CLEAN solely because of CANCELLED workflow runs on
    the current HEAD (every non-success check is CANCELLED, no FAILURE),
    return the cancelled checks. Otherwise []. Per upstream's empirical
    pattern: GitHub auto-cancels in-flight runs when a healer pushes a fresh
    commit; the cancellation leaves CANCELLED status with no fresh trigger,
    blocking auto-merge until someone manually re-runs."""
    rollup = pr.get('statusCheckRollup') or []
    cancelled = []
    has_other_problem = False
    for check in rollup:
        status = check.get('status', '')
        concl = check.get('conclusion', '')
        if status == 'IN_PROGRESS':
            return []
        if status == 'COMPLETED':
            if concl == 'CANCELLED':
                cancelled.append(check)
            elif concl not in ('SUCCESS', 'SKIPPED', 'NEUTRAL', ''):
                has_other_problem = True
                break
    if has_other_problem or not cancelled:
        return []
    return cancelled


def rerun_cancelled_checks(pr: dict, checks: list[dict]) -> int:
    repo = pr.get('_repo', '')
    pr_num = pr.get('number')
    env = {**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/snap/bin'}
    triggered = 0
    seen_runs: set[str] = set()
    for check in checks:
        details_url = check.get('detailsUrl', '') or ''
        m = re.search(r'/actions/runs/(\d+)', details_url)
        if not m:
            continue
        run_id = m.group(1)
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        try:
            result = subprocess.run(
                ['gh', 'run', 'rerun', run_id, '--repo', repo],
                capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=env,
            )
            if result.returncode == 0:
                triggered += 1
                log(f'HEALED: rerun CANCELLED workflow {repo}#{pr_num} '
                    f'run_id={run_id}')
            else:
                log(f'rerun {repo}#{pr_num} run_id={run_id} '
                    f'rc={result.returncode}: '
                    f'{(result.stderr or "").strip()[:160]}', 'WARN')
        except subprocess.TimeoutExpired:
            log(f'rerun {repo}#{pr_num} run_id={run_id} timed out', 'WARN')
    return triggered


# -------------------- Mirror-PASS detection via log scan --------------------

def find_mirror_passed_failures(
    log_path: Path = OUTBOX_NOTIFIER_LOG,
    lookback_hours: int = LOG_LOOKBACK_HOURS,
    now: Optional[datetime] = None,
) -> dict[str, dict[str, str]]:
    """Scan outbox-notifier.log for `AUTO_MERGE ... outcome=failed` entries
    within the lookback window. Return a dict keyed by pr_url with the
    most recent failure metadata: {'task_id', 'reason', 'ts'}.

    A PR appears in the returned dict iff outbox_notifier already attempted
    to merge it (proves Mirror PASSed it) AND the merge failed. The healer
    only acts on PRs that pass this filter, so it never auto-merges
    anything Mirror didn't review.
    """
    if not log_path.exists():
        return {}
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    found: dict[str, dict[str, str]] = {}
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            for raw in f:
                m = _AUTO_MERGE_FAILED_RE.search(raw)
                if not m:
                    continue
                ts_m = _LOG_TS_RE.search(raw)
                ts_str = ts_m.group(1) if ts_m else ''
                # Only filter by ts if we could parse it; otherwise keep the
                # entry (safer to include than to silently drop).
                if ts_str:
                    try:
                        ts_dt = datetime.fromisoformat(
                            ts_str.replace(' ', 'T')
                        ).replace(tzinfo=timezone.utc)
                        if ts_dt < cutoff:
                            continue
                    except ValueError:
                        pass
                task_id, pr_url, reason = m.group(1), m.group(2), m.group(3).strip()
                found[pr_url] = {
                    'task_id': task_id,
                    'reason': reason[:200],
                    'ts': ts_str,
                }
    except OSError as e:
        log(f'find_mirror_passed_failures read error: {e}', 'WARN')
    return found


# -------------------- merge primitive --------------------

def _pr_is_merged(repo: str, pr_number: int) -> Optional[bool]:
    """True/False whether the PR is actually merged on GitHub; None if it can't
    be determined (gh error / parse failure). Used to disambiguate gh's
    'not in a mergeable state' error — a None/False result means we must NOT
    claim the PR shipped."""
    env = {**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/snap/bin'}
    try:
        out = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number), '--repo', repo,
             '--json', 'state,mergedAt'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=env,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or '{}')
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
            json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get('state') == 'MERGED' or bool(data.get('mergedAt'))


def merge_pr(repo: str, pr_number: int, title: str) -> tuple[str, str]:
    """Execute `gh pr merge <N> --squash --delete-branch`.
    Returns (outcome, reason) where outcome in
    {'merged', 'already_merged', 'failed'}. Mirrors the outcome shape of
    outbox_notifier._auto_merge_pr."""
    env = {**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/snap/bin'}
    try:
        result = gh_write(
            ['gh', 'pr', 'merge', str(pr_number),
             '--repo', repo, '--squash', '--delete-branch'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=env,
        )
        if result.returncode == 0:
            return 'merged', f'merged {repo}#{pr_number}'
        stderr = (result.stderr or '').strip().lower()
        # 'already been merged' is unambiguous — gh only says it for a true race.
        if 'already been merged' in stderr:
            return 'already_merged', f'already-merged race for {repo}#{pr_number}'
        # 'not in a mergeable state' is AMBIGUOUS: gh emits it for conflicts,
        # failing/late required checks, and branch-protection blocks too — not
        # only already-merged races. Classifying all of those as 'already_merged'
        # (which process_pr treats as success: drops retry state + DMs Larry
        # "Healer merged") silently reports an UNMERGED PR as shipped. Verify the
        # PR's real state before claiming success.
        if 'not in a mergeable state' in stderr:
            if _pr_is_merged(repo, pr_number) is True:
                return 'already_merged', f'already-merged (verified) {repo}#{pr_number}'
            return 'failed', (
                'not in a mergeable state and PR is not merged — likely '
                'conflict / failing checks / branch protection: '
                + (result.stderr or '').strip()[:160])
        return 'failed', (result.stderr or '').strip()[:200]
    except subprocess.TimeoutExpired:
        return 'failed', f'gh pr merge timed out after {GH_TIMEOUT_S}s'
    except FileNotFoundError as e:
        return 'failed', f'gh-cli-missing: {e}'


# -------------------- per-PR orchestration --------------------

def _to_utc(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-ish timestamp (log 'YYYY-MM-DD HH:MM:SS' or gh
    'YYYY-MM-DDTHH:MM:SSZ') to an aware UTC datetime via the shared
    log_ts.parse_log_ts, or None if unparseable. A naive log value is
    host-local (heal + outbox run on the same host); see log_ts for the 6h-skew
    that a prior UTC pin caused here — it refused every merge. An aware gh
    '...Z' value just normalizes to the same instant."""
    return parse_log_ts(s)


def _head_pushed_since_pass(repo: str, pr_num: int, passed_ts_iso: str) -> bool:
    """True (=> REFUSE to auto-merge) if the PR's HEAD commit is newer than the
    recorded Mirror PASS — i.e. the branch was pushed/rebased after review, so
    the current HEAD was never reviewed (audit #15). The AUTO_MERGE log line
    proves Mirror PASSed *some* HEAD of this URL, not necessarily today's.

    When there is NO review-time anchor (the PASS log line had no parseable
    timestamp — a rare log-format gap), we can't compare, so defer to the prior
    behavior and return False (don't block). But when we DO have an anchor and
    still can't fetch the commit dates (transient gh error), fail CLOSED and
    return True — that only delays the merge one tick (it retries), and never
    merges a HEAD we couldn't prove was the reviewed one."""
    passed_dt = _to_utc(passed_ts_iso)
    if passed_dt is None:
        return False  # no anchor (rare log gap) -> no regression vs prior behavior
    data = gh_json('pr', 'view', str(pr_num), '--repo', repo,
                   '--json', 'commits', default=None)
    if data is None:
        return True  # anchored but gh failed -> fail closed (retries next tick)
    dates = [d for d in (_to_utc(c.get('committedDate'))
                         for c in (data.get('commits') or [])) if d]
    if not dates:
        return True
    # The reviewed commit predates the PASS log line; a post-review push lands a
    # commit dated after it. Small grace absorbs clock skew between the two.
    return max(dates) > passed_dt + timedelta(seconds=2)


def process_pr(pr: dict, mirror_passed: dict[str, dict[str, str]],
               state: dict[str, Any], dry_run: bool) -> str:
    """Return one of: 'merged', 'rerun', 'budget-exhausted', 'skipped',
    'dry-run-candidate', 'no-mirror-pass', 'not-mergeable'."""
    repo = pr['_repo']
    pr_num = pr.get('number')
    pr_url = pr.get('url') or ''
    title = pr.get('title') or ''

    if pr_url not in mirror_passed:
        return 'no-mirror-pass'

    # #15: the AUTO_MERGE log proves Mirror PASSed *some* HEAD of this URL, not
    # necessarily the current one. If the branch was pushed/rebased after the
    # PASS, the current HEAD was never reviewed — never auto-merge it.
    if _head_pushed_since_pass(repo, pr_num, mirror_passed[pr_url].get('ts', '')):
        pr_state = get_pr_state(state, pr_url)
        log(f'PR {repo}#{pr_num}: HEAD changed since the Mirror PASS '
            f'({mirror_passed[pr_url].get("ts") or "unknown"}); refusing to '
            f'auto-merge an unreviewed HEAD', 'WARN')
        # Surface it once so a persistently-refused PR isn't silently stuck.
        if not pr_state['head_changed_alerted'] and not dry_run:
            add_stalled_label(repo, pr_num)
            dm_larry(
                message=(
                    f'A Mirror-PASSed PR was pushed/rebased after review, so its '
                    f'current HEAD was never reviewed. The healer will NOT '
                    f'auto-merge it.\n\nPR: {pr_url}'
                ),
                subject=f'Auto-merge needs re-review: {repo}#{pr_num}',
                suggested_action=(
                    f'Re-run Mirror review on the new HEAD, or merge manually if '
                    f'the push was trivial: `gh pr merge {pr_num} --repo {repo} '
                    f'--squash --delete-branch`'
                ),
                severity='warning',
            )
            pr_state['head_changed_alerted'] = True
            save_state(state)
        return 'head-changed-since-review'

    mergeable, reason = is_mergeable(pr)
    if not mergeable:
        # CANCELLED-workflow rescue: if the only thing in the way is CANCELLED
        # workflow runs, rerun them so the next tick sees green — but under a
        # budget (#24) so a perpetually-cancelled PR isn't re-run forever.
        cancelled = find_cancelled_checks_to_rerun(pr)
        if cancelled and not dry_run:  # reruns are a mutation — never in dry-run
            pr_state = get_pr_state(state, pr_url)
            if pr_state['rerun_attempts'] >= MAX_RERUNS_PER_PR:
                if not pr_state['rerun_alerted']:
                    log(f'PR {repo}#{pr_num}: CANCELLED-rerun budget exhausted '
                        f'({pr_state["rerun_attempts"]}/{MAX_RERUNS_PER_PR}); '
                        f'stopping reruns', 'WARN')
                    add_stalled_label(repo, pr_num)
                    dm_larry(
                        message=(
                            f'A Mirror-PASSed PR keeps failing on CANCELLED '
                            f'workflow runs. The healer reran them '
                            f'{MAX_RERUNS_PER_PR} times and has stopped.\n\n'
                            f'PR: {pr_url}'
                        ),
                        subject=f'Auto-merge rerun-stalled: {repo}#{pr_num}',
                        suggested_action=(
                            f'Investigate why checks keep cancelling (concurrency '
                            f'group / queued-then-superseded). To merge manually: '
                            f'`gh pr merge {pr_num} --repo {repo} --squash '
                            f'--delete-branch`'
                        ),
                        severity='warning',
                    )
                    pr_state['rerun_alerted'] = True
                    save_state(state)
            else:
                # Count the attempt BEFORE firing so the budget advances even if
                # `gh run rerun` fails to fire (n==0) — otherwise it would retry
                # the rerun every tick forever, the very loop #24 closes.
                pr_state['rerun_attempts'] += 1
                save_state(state)
                rerun_cancelled_checks(pr, cancelled)
                return 'rerun'
        log(f'PR {repo}#{pr_num} mirror-passed but not mergeable: {reason}')
        return 'not-mergeable'

    pr_state = get_pr_state(state, pr_url)
    # The PR is mergeable now — any CANCELLED bout resolved, so reset the rerun
    # budget for a future one.
    if pr_state['rerun_attempts']:
        pr_state['rerun_attempts'] = 0
        save_state(state)
    if pr_state['attempts'] >= MAX_RETRIES_PER_PR:
        if not pr_state['stalled_alerted']:
            log(f'PR {repo}#{pr_num} retry budget exhausted '
                f'(attempts={pr_state["attempts"]}); labeling + DMing Larry',
                'WARN')
            add_stalled_label(repo, pr_num)
            failure_info = mirror_passed.get(pr_url, {})
            last_reason = failure_info.get('reason', '(no reason in log)')
            dm_larry(
                message=(
                    f'Mirror-PASSed PR has failed auto-merge '
                    f'{MAX_RETRIES_PER_PR} times. Healer has labeled it '
                    f'`{STALLED_LABEL}` and stopped retrying.\n\n'
                    f'PR: {pr_url}\nLast failure reason: {last_reason}'
                ),
                subject=f'Auto-merge stalled: {repo}#{pr_num}',
                suggested_action=(
                    f'Investigate the failure (check CI / branch '
                    f'protection / conflict). To unblock the healer: '
                    f'remove the `{STALLED_LABEL}` label from the PR. To '
                    f'merge manually: `gh pr merge {pr_num} --repo '
                    f'{repo} --squash --delete-branch`'
                ),
                severity='warning',
            )
            pr_state['stalled_alerted'] = True
            save_state(state)
        return 'budget-exhausted'

    if dry_run:
        # One-time activation DM per PR — only on first time we discover
        # this PR is a real candidate.
        if not pr_state['activation_alerted']:
            log(f'DRY-RUN candidate: {repo}#{pr_num} — would merge', 'INFO')
            dm_larry(
                message=(
                    f'Heal-pr-auto-merge dry-run found a candidate: a '
                    f'Mirror-PASSed PR that the primary path failed to '
                    f'merge.\n\nPR: {pr_url}\n\nThe healer is in dry-run '
                    f'mode (OURLIBERTY_AUTOMERGE_ENABLED is not set). It '
                    f'logged the candidate but did not merge.'
                ),
                subject='Auto-merge healer: activate to merge stalled PRs',
                suggested_action=(
                    'To activate the healer: ssh larry@134.209.44.80, '
                    'then `sudo systemctl edit '
                    'ourliberty-heal-pr-auto-merge.service`, add an '
                    '`Environment="OURLIBERTY_AUTOMERGE_ENABLED=true"` '
                    'line under [Service], then `sudo systemctl restart '
                    'ourliberty-heal-pr-auto-merge.timer`. Subsequent '
                    'healer ticks will merge candidates automatically '
                    'and DM the outcome.'
                ),
                severity='warning',
            )
            pr_state['activation_alerted'] = True
            save_state(state)
        return 'dry-run-candidate'

    # Live path: actually merge.
    pr_state['attempts'] += 1
    pr_state['last_attempt_iso'] = datetime.now(timezone.utc).isoformat()
    outcome, detail = merge_pr(repo, pr_num, title)
    save_state(state)
    if outcome in ('merged', 'already_merged'):
        # Only a fresh merge THIS backstop performed is a clean execution;
        # 'already_merged' means another path (outbox_notifier) landed it and
        # recorded it there — skip to avoid double-counting.
        if outcome == 'merged':
            _record_auto_merge_success()
        log(f'HEALED: {outcome} {repo}#{pr_num} (attempt '
            f'{pr_state["attempts"]}/{MAX_RETRIES_PER_PR}) — {detail}')
        # Stop tracking — clear the entry so a future PR re-using this URL
        # (shouldn't happen, URLs are unique) wouldn't carry stale state.
        state['prs'].pop(pr_url, None)
        save_state(state)
        # DM Larry the success — short, no cooldown collision because
        # subject embeds the PR number.
        dm_larry(
            message=f'Healer merged {repo}#{pr_num} ({outcome}) on '
                    f'attempt {pr_state["attempts"]}.',
            subject=f'Auto-merge healed: {repo}#{pr_num}',
            suggested_action='No action needed.',
            severity='warning',
        )
        return 'merged'
    # No graduation record on failure (success-only; see _record_auto_merge_success).
    log(f'PR {repo}#{pr_num} merge attempt '
        f'{pr_state["attempts"]}/{MAX_RETRIES_PER_PR} failed: {detail}',
        'WARN')
    return 'skipped'


# -------------------- main --------------------

def main() -> int:
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return 0
    heartbeat()

    dry_run = not automerge_enabled()
    state = load_state()
    mirror_passed = find_mirror_passed_failures()

    if not mirror_passed:
        log(f'tick: no mirror-passed failures in last {LOG_LOOKBACK_HOURS}h '
            f'(dry_run={dry_run})')
        return 0

    # gh-api-burn phase 1: back off before the per-repo `gh pr list` queries
    # below when the shared GraphQL budget is low. Fail-open (unknown -> proceed);
    # a broken guard never wedges the healer.
    try:
        import gh_budget
        if gh_budget.should_skip('heal_pr_auto_merge', log=log):
            return 0
    except Exception as e:  # noqa: BLE001
        log(f'gh_budget guard unavailable ({type(e).__name__}); proceeding')

    counts = {
        'merged': 0, 'rerun': 0, 'budget-exhausted': 0,
        'dry-run-candidate': 0, 'no-mirror-pass': 0,
        'not-mergeable': 0, 'skipped': 0,
    }
    for repo in REPOS:
        prs = list_open_prs(repo)
        if not prs:
            continue
        for pr in prs:
            if counts['merged'] >= MAX_MERGES_PER_RUN:
                log(f'MAX_MERGES_PER_RUN={MAX_MERGES_PER_RUN} reached; '
                    f'remainder next tick')
                break
            outcome = process_pr(pr, mirror_passed, state, dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1

    log(f'tick: dry_run={dry_run} '
        f'mirror_passed_failures={len(mirror_passed)} '
        + ' '.join(f'{k}={v}' for k, v in counts.items() if v))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
