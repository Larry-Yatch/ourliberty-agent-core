#!/usr/bin/env python3
"""
heal_unreviewed_merge_detector.py — Build B of the Mirror-review merge gate.

Defense-in-depth detective control. Build A makes Mirror's pass a *required*
GitHub commit status (`mirror-review`) and branch protection enforces it, so
no actor — including the admin identity — can merge without Mirror's pass.
This healer is the backstop that runs EVEN WITH protection on: if a merge
somehow lands on `main` without a Mirror REVIEW_PASS (protection mis-config, a
status that got force-posted, GitHub bug, manual admin override of protection),
this catches it after the fact and pages Larry.

The hole this was built for: this session a `/babysit-prs` loop holding the
`gh` token merged #303 despite a REVISION verdict and #324 with no review at
all. Branch protection (Build A's operator step) prevents that going forward;
this detector verifies the gate actually held.

How it works, each tick (~every 5 min via an OnCalendar timer):
  1. List PRs merged into `main` since a persisted cursor:
       gh pr list --state merged --base main --search "merged:>=<cursor>" ...
  2. For each merged PR, verify a Mirror REVIEW_PASS exists for it. The
     positive signal is local + dependency-free: the outbox-notifier only
     fires its `AUTO_MERGE task=<id> pr=<url> ...` log line AFTER classifying
     a Mirror REVIEW_PASS marker (auto-merge is gated on the pass). So ANY
     AUTO_MERGE* line referencing a PR is proof a pass was classified for it.
     (The brief's alternative — a chain_events `review_pass` row with that
     pr_url — lives in Supabase; the log scan is preferred here because it's
     local, needs no network, and is trivially testable.)
  3. A PR merged with NO REVIEW_PASS evidence -> emit a Larry ESCALATION
     alert "PR #N merged without Mirror review (actor=<login>)".
  4. Advance the cursor; record which PRs were already alerted so a re-scan of
     the same window never double-alerts.

Safe-by-construction (follows the heal_* conventions):
  - Kill-switch aware (exits 0 on ~/agents/healers.disabled).
  - Dry-run env override (OURLIBERTY_UNREVIEWED_MERGE_DETECTOR_ENABLED): when
    not "true", detects + logs candidates but emits no DM. Lets Larry verify
    the detector before it starts paging.
  - First run establishes a baseline cursor and alerts on nothing (so deploy
    doesn't flood Larry with the historical backlog).
  - Tolerates gh errors (logs, NEVER crashes the healer).
  - Idempotent: per-PR alert dedup in the state file + the larry_alerts
    per-(source,subject) cooldown.
  - Read-only on GitHub (lists PRs; never merges, closes, or comments).

The alert is Python-emitted, so it has a registered entry in
config/alert-translations.json under source `heal-unreviewed-merge-detector`
(subject prefix `unreviewed-merge`), enforced by the translation-coverage CI
gate (scripts/tests/test_alert_translations.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-unreviewed-merge-detector.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-unreviewed-merge-detector.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-unreviewed-merge-detector.json'
OUTBOX_NOTIFIER_LOG = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'

REPO = 'Larry-Yatch/ourliberty-agent-core'
BASE_BRANCH = 'main'

GH_TIMEOUT_S = 30
# Don't list an unbounded backlog if the cursor is somehow far in the past;
# cap the query window so one slow/stale tick can't fan out into hundreds of
# PRs. The cursor still advances normally; this only bounds a single query.
MAX_LOOKBACK_DAYS = 7
# Keep the alerted-PR ledger from growing without bound. Recent entries are all
# that matter (the cursor only re-presents PRs near the boundary).
MAX_ALERTED_LEDGER = 200

ALERT_SOURCE = 'heal-unreviewed-merge-detector'
# Subject prefix MUST stay in sync with the translation entry keyed under
# `unreviewed-merge` in config/alert-translations.json (the longest-prefix
# lookup strips the `:<pr>` suffix back to this prefix).
SUBJECT_PREFIX = 'unreviewed-merge'

# Activation env var. Detection is read-only, so unlike heal_pr_auto_merge this
# defaults ON — its whole job is to alert. The override exists so Larry can run
# it in dry-run during initial verification without tripping the blanket
# kill-switch.
ENV_ENABLED = 'OURLIBERTY_UNREVIEWED_MERGE_DETECTOR_ENABLED'


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


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- kill-switch + activation --------------------

def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def detector_enabled() -> bool:
    """True unless OURLIBERTY_UNREVIEWED_MERGE_DETECTOR_ENABLED is explicitly a
    non-true value. Default ON (read-only detector). Set to e.g. 'false' to run
    in dry-run (detect + log, no DM)."""
    raw = os.environ.get(ENV_ENABLED)
    if raw is None:
        return True
    return raw.strip().lower() == 'true'


# -------------------- state (cursor + alert dedup) --------------------

def load_state() -> dict[str, Any]:
    """Return {'cursor_iso': str|None, 'alerted_prs': {pr_url: iso}}."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'cursor_iso': None, 'alerted_prs': {}}
        data.setdefault('cursor_iso', None)
        if not isinstance(data.get('alerted_prs'), dict):
            data['alerted_prs'] = {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'cursor_iso': None, 'alerted_prs': {}}


def save_state(state: dict[str, Any]) -> None:
    # Trim the alerted ledger to the most recent entries (by recorded ts) so it
    # can't grow unbounded across months of runs.
    alerted = state.get('alerted_prs', {})
    if isinstance(alerted, dict) and len(alerted) > MAX_ALERTED_LEDGER:
        kept = sorted(alerted.items(), key=lambda kv: kv[1] or '')[-MAX_ALERTED_LEDGER:]
        state['alerted_prs'] = dict(kept)
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, indent=2))
        tmp.rename(STATE_FILE)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


# -------------------- time helpers --------------------

def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z') to an aware UTC
    datetime. Returns None if unparseable."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _gh_search_cursor(cursor_iso: Optional[str], now: datetime) -> str:
    """The `merged:>=<ts>` value for the gh search. Floors the cursor to at
    most MAX_LOOKBACK_DAYS ago so a stale cursor can't request a huge window.
    GitHub's search accepts an ISO-8601 datetime with timezone offset."""
    floor = now - timedelta(days=MAX_LOOKBACK_DAYS)
    cur = _parse_iso(cursor_iso) if cursor_iso else None
    eff = max(cur, floor) if cur else floor
    # GitHub search wants e.g. 2026-06-04T18:05:03+00:00.
    return eff.strftime('%Y-%m-%dT%H:%M:%S+00:00')


# -------------------- gh + log I/O (mockable seams) --------------------

def fetch_merged_prs(cursor_iso: Optional[str], now: datetime) -> list[dict[str, Any]]:
    """Return parsed `gh pr list` rows for PRs merged into BASE since the
    cursor. Each row: {number, mergedAt, url, author_login, title}. Tolerates
    any gh failure (logs + returns []) — a detector must never crash the
    healer."""
    search = f'merged:>={_gh_search_cursor(cursor_iso, now)}'
    cmd = [
        'gh', 'pr', 'list',
        '--repo', REPO,
        '--state', 'merged',
        '--base', BASE_BRANCH,
        '--search', search,
        '--limit', '100',
        '--json', 'number,mergedAt,url,author,title',
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh pr list failed: {type(e).__name__}: {e}', 'WARN')
        return []
    if proc.returncode != 0:
        log(f'gh pr list returned {proc.returncode}: '
            f'{proc.stderr.strip()[:300]}', 'WARN')
        return []
    return parse_merged_prs(proc.stdout)


def parse_merged_prs(raw_json: str) -> list[dict[str, Any]]:
    """Normalize `gh pr list --json ...` stdout into flat rows. Skips
    malformed entries rather than raising."""
    try:
        rows = json.loads(raw_json or '[]')
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        number = r.get('number')
        url = r.get('url')
        merged_at = r.get('mergedAt')
        if number is None or not url or not merged_at:
            continue
        author = r.get('author') or {}
        login = author.get('login') if isinstance(author, dict) else None
        out.append({
            'number': number,
            'mergedAt': merged_at,
            'url': url,
            'author_login': login or 'unknown',
            'title': r.get('title') or '',
        })
    return out


def read_notifier_log() -> str:
    """Read the outbox-notifier log. Returns '' if absent/unreadable."""
    try:
        return OUTBOX_NOTIFIER_LOG.read_text(errors='replace')
    except (FileNotFoundError, OSError):
        return ''


# -------------------- review-pass evidence (pure) --------------------

def review_passed_pr_urls(notifier_log_text: str) -> set[str]:
    """Extract the set of PR URLs that have Mirror REVIEW_PASS evidence.

    The outbox-notifier only logs an `AUTO_MERGE...` line carrying `pr=<url>`
    after it has classified a Mirror REVIEW_PASS marker for that PR (auto-merge
    is gated on the pass). So every PR URL that appears on an AUTO_MERGE line —
    regardless of the merge OUTCOME (merged / already_merged / failed / held /
    deferred / skipped) — had a pass classified. The merge mechanics may have
    succeeded, failed, or been deferred to the healer; what matters here is
    only that a pass happened.

    Pure function over the log text so tests inject fixtures."""
    passed: set[str] = set()
    for line in notifier_log_text.splitlines():
        if 'AUTO_MERGE' not in line:
            continue
        idx = line.find('pr=')
        if idx < 0:
            continue
        rest = line[idx + 3:].lstrip()
        # The url may be repr'd (quoted) or bare: pr='https://...' or pr=https://...
        if rest and rest[0] in '\'"':
            rest = rest[1:]
        # Take the token up to the next whitespace or closing quote.
        token = []
        for ch in rest:
            if ch.isspace() or ch in '\'"':
                break
            token.append(ch)
        url = ''.join(token)
        if url.startswith('http'):
            passed.add(url.rstrip('/'))
    return passed


# -------------------- core detection (pure) --------------------

def run_once(
    merged_prs: list[dict[str, Any]],
    passed_urls: set[str],
    alerted_prs: dict[str, Any],
) -> list[dict[str, Any]]:
    """Pure detection step. Given the merged-PR rows, the set of PR URLs with
    REVIEW_PASS evidence, and the already-alerted ledger, return the list of
    alert dicts to emit (one per unreviewed, not-yet-alerted merge).

    Each alert dict: {pr_url, number, subject, message, suggested_action}.
    """
    alerts: list[dict[str, Any]] = []
    for pr in merged_prs:
        url = str(pr['url']).rstrip('/')
        if url in passed_urls:
            continue  # Mirror passed it — fine.
        if url in alerted_prs:
            continue  # already paged Larry about this one.
        number = pr['number']
        actor = pr['author_login']
        subject = f'{SUBJECT_PREFIX}:{number}'
        message = (
            f'PR #{number} merged without Mirror review (actor={actor}). '
            f'No REVIEW_PASS evidence found for {url}. The Mirror-review merge '
            f'gate did not hold for this merge.'
        )
        suggested_action = (
            f'Review the merge: `gh pr view {number} --repo {REPO}`. If it '
            f'should not have merged, assess the change and revert if needed. '
            f'Check branch protection on {BASE_BRANCH}: '
            f'`gh api repos/{REPO}/branches/{BASE_BRANCH}/protection`.'
        )
        alerts.append({
            'pr_url': url,
            'number': number,
            'subject': subject,
            'message': message,
            'suggested_action': suggested_action,
        })
    return alerts


def advance_cursor(merged_prs: list[dict[str, Any]],
                   current_cursor_iso: Optional[str]) -> Optional[str]:
    """Return the newest mergedAt across the scanned PRs (vs. the current
    cursor). Monotonic — never moves backward."""
    newest = _parse_iso(current_cursor_iso) if current_cursor_iso else None
    for pr in merged_prs:
        dt = _parse_iso(pr.get('mergedAt'))
        if dt is None:
            continue
        if newest is None or dt > newest:
            newest = dt
    return newest.isoformat() if newest else current_cursor_iso


# -------------------- alert emission --------------------

def emit_alert(alert: dict[str, Any]) -> bool:
    """Fire one Larry ESCALATION alert via larry_alerts (route=escalate,
    severity=critical). Per-(source,subject) cooldown handled inside
    larry_alerts. Returns True iff appended. Never raises."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source=ALERT_SOURCE,
            severity='critical',
            message=alert['message'],
            subject=alert['subject'],
            suggested_action=alert['suggested_action'],
            route='escalate',
        )
    except Exception as e:  # noqa: BLE001 — emission must never crash the healer
        log(f'emit_alert failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- main --------------------

def main() -> int:
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return 0
    heartbeat()

    now = datetime.now(timezone.utc)
    state = load_state()
    cursor_iso = state.get('cursor_iso')

    # First run: establish a baseline so deploy doesn't page Larry about the
    # entire historical backlog of merges. Going forward is what matters.
    if not cursor_iso:
        state['cursor_iso'] = now.isoformat()
        save_state(state)
        log('baseline established (first run); no scan this tick '
            f'(cursor={state["cursor_iso"]})')
        return 0

    merged_prs = fetch_merged_prs(cursor_iso, now)
    log(f'scanning {len(merged_prs)} PR(s) merged into {BASE_BRANCH} '
        f'since {cursor_iso}')

    passed_urls = review_passed_pr_urls(read_notifier_log())
    alerted = state['alerted_prs']
    alerts = run_once(merged_prs, passed_urls, alerted)

    dry_run = not detector_enabled()
    emitted = 0
    for alert in alerts:
        if dry_run:
            log(f'[dry-run] would alert: {alert["message"]}', 'WARN')
            continue
        if emit_alert(alert):
            emitted += 1
            log(f'ALERT PR #{alert["number"]} merged without Mirror review '
                f'({alert["pr_url"]})', 'WARN')
        # Record as alerted regardless of cooldown-suppress so we don't re-emit
        # on the next tick once the cooldown lifts — one detection, one page.
        alerted[alert['pr_url']] = now.isoformat()

    # Advance the cursor over everything we scanned (advancing past clean merges
    # too — they're verified-good and shouldn't be re-scanned).
    state['cursor_iso'] = advance_cursor(merged_prs, cursor_iso)
    save_state(state)

    log(f'tick: scanned={len(merged_prs)} unreviewed={len(alerts)} '
        f'emitted={emitted} dry_run={dry_run} cursor={state["cursor_iso"]}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the timer
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
