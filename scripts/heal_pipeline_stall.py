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

  6. **REVIEW_REVISION dispatched with no Forge session** — outbox-notifier
     logged `no forge_build_session_id` for a REVIEW_REVISION (chain
     discipline v3 GAP 1, 2026-05-26). The direct fix already DMs Larry
     via `larry_alerts.append_alert` when the path fires; this check is
     defense-in-depth in case the alert was suppressed by per-subject
     cooldown or the queue file was unreadable at the moment.

  7. **Open PRs with no review-request dispatch logged** — open PR on a
     tracked repo older than 1h with no matching `review-request`
     entry in `~/agents/logs/routing-events.jsonl` (chain discipline v3
     GAP 3). Catches externally-authored PRs (Claude-as-Forge, manual
     pushes) that skip the notifier's auto-dispatch entirely; the chain
     can't pick them up without a manual Beacon dispatch.

Every alert: ONE Telegram DM via `larry_alerts.append_alert` (cooldown
1h per unique stall key). Each alert states: what stalled, how long,
recommended action, the specific log grep to run for details.

Never acts on the stall. Surface only. The healer never kills processes,
never merges PRs, never re-dispatches anything. Same posture as Joe's
`pipeline_watcher.py` and our `dispatch_sentinel.py`.

Scan window
-----------
All seven Checks gate their stall-trigger event timestamp against
``SCAN_WINDOW_SECONDS`` (default 86400s = 24h). Events older than the
window are skipped silently — they represent historical record, not a
current stall. This retires already-resolved incidents instead of
re-firing on log lines from yesterday. Three false positives on
2026-05-26 (Mirror PASS unmerged for tasks Larry had already merged
manually, plus an outbox-notifier WARN replay from the prior day) drove
the explicit window. The 24h default is long enough to cover overnight
quiet periods but short enough that a stall older than a day is past
the point where another DM helps — at that age the action is human
investigation, not another notification.

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
import fixture_patterns  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-pipeline-stall.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-pipeline-stall.heartbeat'
STATE_FILE = AGENTS_ROOT / 'blackboard' / 'heal-pipeline-stall-state.json'
# Check 8 per-(agent, outcome, reason) cursor. Records the latest log-line
# timestamp already processed per signature so subsequent runs don't re-emit
# alerts on the same log lines. Lives in state/ (not blackboard/) per the
# healer-state convention — heal_pipeline_stall-state.json is the only
# blackboard/ resident for legacy reasons; new persistent state lives in
# state/. Atomic tmp+rename writes; safe-resume on JSON-decode corruption
# (logs WARN, treats all cursors as empty, writes a fresh empty file).
CHECK8_CURSOR_FILE = AGENTS_ROOT / 'state' / 'heal-pipeline-stall-check-8-cursor.json'
OUTBOX_NOTIFIER_LOG = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'
# Archived Forge outboxes carry the first-attempt result string with the
# preflight marker (PROCEED / CLARIFY_REQUEST / REJECT_REQUEST). Check 1
# reads this to distinguish 'Forge done but no PR by design' from a real
# stall. See `_forge_preflight_non_proceed` for the read shape.
FORGE_OUTBOX_ARCHIVE = AGENTS_ROOT / 'outboxes' / 'forge' / '.archive'
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

# Bounded scan window for stall-trigger events. Events whose anchor
# timestamp is older than this are treated as historical record, not
# stalls, and produce no alert. See module docstring's "Scan window"
# section for rationale. Tunable: edit the constant, then
# `sudo systemctl restart ourliberty-heal-pipeline-stall.timer`.
SCAN_WINDOW_SECONDS = 86400          # 24h

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
# Chain discipline v3 GAP 1 (Check 6). Matches the WARN line emitted by
# outbox_notifier.py when REVIEW_REVISION fires on an envelope with no
# `forge_build_session_id`. Production line shape:
#   [<ts>] [notifier] [WARN] REVIEW_REVISION on task <task> has no
#   forge_build_session_id (routing_source='beacon', chat_id=None);
#   revision dispatch would have no session to --resume — skipping.
# The task token is non-greedy up to the first space so the regex matches
# both the pre-v3 ("propagation gap?") shape and the v3 ("routing_source=…")
# shape — the direct fix changed the trailing diagnostic, not the prefix.
_NO_FORGE_SESSION_RE = re.compile(
    r'\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]'
    r'.*REVIEW_REVISION on task (?P<task>\S+) has no forge_build_session_id'
)

# Chain discipline v3 GAP 3 (Check 7). routing-events.jsonl carries one
# JSON object per line; the shape we read has these fields:
#   action, source_agent, target_agent_requested, target_agent_final,
#   filename_requested, filename_final, truncated, rerouted,
#   reroute_reason, task_id, intent, phase, target_repo, timestamp
# A review-request dispatch from Beacon to Mirror is uniquely identified
# by source_agent='beacon', target_agent_final='mirror', phase='review'.
ROUTING_EVENTS_LOG = AGENTS_ROOT / 'logs' / 'routing-events.jsonl'
ROUTING_EVENTS_LOOKBACK_HOURS = 24 * 7  # PRs aren't routed faster than this
PR_UNROUTED_MIN_AGE_MIN = 60            # don't race with in-flight auto-dispatch

# Check 8 (claude-quota-tier2-fallback-wrapper, 2026-05-26). Scan per-agent
# logs for TIER2_FALLBACK_UNAVAILABLE / TIER2_FALLBACK_FAILED /
# TIER2_FALLBACK_SKIPPED markers within the last 24h. The in-flight
# healer-read-discipline PR introduces `SCAN_WINDOW_SECONDS`; this check
# uses that constant when it lands and falls back to a local equivalent
# until then. Either way, the value is 24h.
TIER2_LOG_LOOKBACK_HOURS = 24
# Match: '[<ts>] [agent] [LEVEL] TIER2_FALLBACK_<UNAVAILABLE|FAILED|SKIPPED>
#         reason=<rate_limit|auth_401> ...' OR the same line shape without
# the agent prefix (beacon_telegram_bot.log uses a slightly different
# format). The reason= is non-greedy and bounded to keep the regex safe.
_TIER2_FALLBACK_RE = re.compile(
    r'\[(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\]'
    r'.*?TIER2_FALLBACK_(?P<outcome>UNAVAILABLE|FAILED|SKIPPED)\s+'
    r'reason=(?P<reason>rate_limit|auth_401)'
)
# Per-agent log files that the wrappers write to. agent_runner.py logs
# to ~/agents/logs/<agent>.log; the bot logs to beacon_telegram_bot.log.
_TIER2_LOG_NAMES = (
    'forge.log', 'mirror.log', 'pulse.log', 'beacon.log',
    'beacon_telegram_bot.log',
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


def _within_scan_window(ts: Optional[datetime],
                        now: Optional[datetime] = None) -> bool:
    """True iff `ts` is no older than SCAN_WINDOW_SECONDS ago. Inclusive at
    the boundary — an event exactly at `now - SCAN_WINDOW_SECONDS` is in.
    Returns False for None (un-parseable timestamps are not stall triggers).

    Used by every Check that anchors a stall on an event timestamp. Events
    older than the window are historical record, not current stalls."""
    if ts is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - ts).total_seconds() <= SCAN_WINDOW_SECONDS


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


# ---------- Shared resolution-signal reconciliation (2026-05-27) ----------
#
# Checks 1, 2, 4, 5, 6, 7 share the same false-positive class: they fire on
# tasks Larry already resolved via the Approvals tab (emitting a
# `larry_action` chain_event), OR that the producing agent already re-ran
# (a subsequent `session_start` chain_event supersedes the original stuck
# pointer), OR — when a PR exists — that the PR was already closed/merged
# out-of-band. The shared helper below queries chain_events for those
# signals and returns (True, reason) when any are present. Each call site
# logs a `<CHECK_NAME>_SKIP reason=<reason>` line mirroring PR #133's
# FORGE_NO_PR_SKIP shape and bypasses the alert.
#
# Failsafe: on infrastructure failure (missing env, import error, query
# exception) the helper returns (False, None) so the legitimate alert path
# is preserved. The 6h per-subject cooldown is unchanged — SKIP bypasses
# it entirely; alerts that do fire still cooldown-gate DM spam.
#
# Check 3 (mirror-pass-unmerged) already handles PR.state inline via a
# different code path. Check 8 (tier2-fallback) is a different signal
# type — resolution signals don't apply. Both untouched.

def _get_chain_events_for_task(task_id: str,
                               since_ts: datetime) -> Optional[list[dict]]:
    """Query Supabase chain_events for rows matching `task_id` with
    `ts > since_ts`. Returns a list of dicts (possibly empty) on success,
    or None on infrastructure failure (missing env, import error, query
    exception). Callers treat None as 'no signal present' so the existing
    alert behavior is preserved as the failsafe.

    Lazy `from supabase import create_client` per the
    `heal_chain_event_type_audit.py` canonical pattern — keeps tests
    mock-friendly and avoids importing the supabase SDK at module load."""
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        # Quiet on missing env — tests don't set these, and the failsafe
        # path is the correct behavior in that case (legitimate alerts
        # still fire). WARN noise here would pollute every healer cycle
        # that runs in an environment without Supabase configured.
        return None
    try:
        from supabase import create_client  # type: ignore
        client = create_client(url, key)
        since_iso = since_ts.astimezone(timezone.utc).isoformat()
        res = (
            client.table('chain_events')
                  .select('event_type,ts,actor,task_id')
                  .eq('task_id', task_id)
                  .gt('ts', since_iso)
                  .execute()
        )
        rows = getattr(res, 'data', None) or []
        return rows
    except Exception as e:
        log(
            f'chain_events query failed for task={task_id}: '
            f'{type(e).__name__}: {e}',
            'WARN',
        )
        return None


def _check_pr_closed_via_gh(pr_url: str) -> Optional[str]:
    """Return the PR state string if MERGED or CLOSED, else None. Returns
    None on subprocess / JSON error too (treated as 'unknown' so the
    legitimate alert behavior is preserved)."""
    try:
        result = subprocess.run(
            ['gh', 'pr', 'view', pr_url, '--json', 'state'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S,
            env={**os.environ,
                 'PATH': '/usr/bin:/usr/local/bin:' + os.environ.get('PATH', '')},
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or '{}')
        state = data.get('state')
        if state in ('MERGED', 'CLOSED'):
            return state
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None
    return None


def _resolution_signal_present(
    task_id: str,
    since_ts: datetime,
    *,
    check_pr_state: bool = False,
    pr_url: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Return (True, reason) if the task has been resolved out-of-band
    since `since_ts`. Reasons checked, in cheap-to-expensive order:

      1. `larry_action` chain_event — Larry approved/rejected the
         queued item via the Approvals tab. Reason: `'larry_action'`.
      2. `session_start` chain_event — the producing agent already
         started a fresh attempt; the older stuck pointer is moot.
         Reason: `'superseded_session'`.
      3. PR.state in {MERGED, CLOSED} via `gh pr view` (only when
         `check_pr_state=True` AND `pr_url` provided). Reason:
         `'pr_closed'`.

    Returns (False, None) when no signal is present OR when the
    chain_events query failed (failsafe — current alert behavior is
    preserved on infrastructure errors). Task_ids of `'unknown'` or
    empty short-circuit to (False, None) since no row could match."""
    if not task_id or task_id == 'unknown':
        return (False, None)
    rows = _get_chain_events_for_task(task_id, since_ts)
    if rows is None:
        return (False, None)
    for row in rows:
        if row.get('event_type') == 'larry_action':
            return (True, 'larry_action')
    for row in rows:
        if row.get('event_type') == 'session_start':
            return (True, 'superseded_session')
    if check_pr_state and pr_url:
        state = _check_pr_closed_via_gh(pr_url)
        if state is not None:
            return (True, 'pr_closed')
    return (False, None)


# ---------- Check 1: Forge built but no PR opened ----------

# Branch-truncation tolerance. Forge writes feature branches as
# `forge/<task_id>` but the branch name has a soft cap (verified
# empirically 2026-05-26: PR #116's full task_id was 54 chars; the
# branch was truncated to `forge/chain-discipline-gap-beacon-plan-
# synthesis-stale-s`, 49 chars after the prefix). When a PR's branch
# suffix is a strict prefix of the task_id AND long enough to make a
# coincidental shared prefix implausible, treat it as a match. 30 char
# floor avoids matching short common prefixes like `fix-`, `build-`.
_BRANCH_TRUNCATION_MIN_LEN = 30

# Preflight non-PROCEED detection lives in `_forge_preflight_non_proceed`.
# Two-tier read: (1) scan `result` for `=== CLARIFY_REQUEST ===` /
# `=== REJECT_REQUEST ===` delimiters and return the specific label
# when present (preserves precise reporting for the oauth-orchestrator-
# promotion-plus-auto-restart shape); (2) fall back to a generic
# `PREFLIGHT_EXIT` label when phase=='preflight' AND exit_code==0 AND
# attempts>=1, which catches the build-sequence-orchestrator-pr-s1
# shape (2026-05-26) where Forge emits the marker block to its session
# log but the outbox `result` field is prose narration without the
# literal delimiter — that was the 185-min false-fire driver.


def _pr_matches_task(pr: dict, task_id: str) -> Optional[str]:
    """If `pr` corresponds to `task_id`, return a short reason string
    (`'branch'` / `'branch_truncated'` / `'title'`); else None.

    Match order:
      1. Exact: headRefName == `forge/<task_id>` (or `larry/<task_id>`).
      2. Branch-truncation tolerant: branch suffix is a strict prefix of
         task_id and >= `_BRANCH_TRUNCATION_MIN_LEN` chars long.
      3. Title fallback: task_id appears (case-insensitive) in the PR
         title — covers re-keyed dispatches and human-titled PRs that
         carry the original task verbatim in the subject line.
    """
    branch = pr.get('headRefName') or ''
    branch_task = _task_id_from_branch(branch)
    if branch_task:
        if branch_task == task_id:
            return 'branch'
        if (
            len(branch_task) >= _BRANCH_TRUNCATION_MIN_LEN
            and task_id.startswith(branch_task)
        ):
            return 'branch_truncated'
    title = pr.get('title') or ''
    if task_id and task_id.lower() in title.lower():
        return 'title'
    return None


def _forge_preflight_non_proceed(task_id: str) -> Optional[str]:
    """Classify the archived Forge outbox as a clean preflight non-PROCEED.
    Returns a short label string if so, else None.

    Reads only `<task_id>.json` — the first-attempt outbox, which is the
    canonical preflight decision. Retry variants (`<task_id>.N.json`)
    inherit the original PROCEED/non-PROCEED outcome and are not
    consulted.

    Return values:
      * `'CLARIFY_REQUEST'` / `'REJECT_REQUEST'` — the `result` field
        contains the corresponding marker delimiter (e.g.
        `=== CLARIFY_REQUEST ===`). Preserves the original precise
        reporting path.
      * `'PREFLIGHT_EXIT'` — fallback for a clean preflight exit when
        the `result` field carries prose narration of the marker
        outcome rather than the literal delimiters. Triggered iff
        `phase == 'preflight'`, `exit_code == 0`, and `attempts >= 1`.
        Production shape: Forge emits the marker block to its session
        log, but the outbox-archive `result` field is the model's
        prose summary — so the delimiter scan misses. Verified
        2026-05-26 against the build-sequence-orchestrator-pr-s1
        archive, which was the 185-min false-fire case that drove
        this fallback.
      * `None` — archive missing, unreadable JSON, or not a
        recognized clean preflight outcome. Caller treats this as
        'no preflight skip-signal' and proceeds with the normal
        PR-existence reconciliation + alert decision."""
    archive = FORGE_OUTBOX_ARCHIVE / f'{task_id}.json'
    if not archive.exists():
        return None
    try:
        with open(archive, errors='replace') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    result = data.get('result')
    if isinstance(result, str):
        if '=== CLARIFY_REQUEST ===' in result:
            return 'CLARIFY_REQUEST'
        if '=== REJECT_REQUEST ===' in result:
            return 'REJECT_REQUEST'
    if (
        data.get('phase') == 'preflight'
        and data.get('exit_code') == 0
        and isinstance(data.get('attempts'), int)
        and data.get('attempts') >= 1
    ):
        return 'PREFLIGHT_EXIT'
    return None


def check_forge_built_no_pr(watcher_lines: list[str], open_prs: list[dict],
                            merged_prs: list[dict], state: dict) -> list[dict]:
    """Find Forge build-done lines >FORGE_BUILT_NO_PR_MIN ago where no PR
    matches the task_id on any tracked repo. Returns list of alert dicts.

    Reads from `inbox_watcher.log` (NOT `outbox-notifier.log`) — the
    `[forge] done task=X success=True` shape is emitted by
    `inbox_watcher.py` to its own log. Verified against production logs
    (Mirror PR #107 review): outbox-notifier.log has zero matches for
    this pattern; inbox_watcher.log has hundreds.

    Reconciliation before alerting (2026-05-26, fixes false-fire on
    truncated-branch PRs + clean-preflight-exit tasks):
      a) `_pr_matches_task` against open + merged PRs across both
         tracked repos. Matches exact branch, truncated branch, or
         title substring.
      b) `_forge_preflight_non_proceed` against the archived outbox.
         Returns `'CLARIFY_REQUEST'` / `'REJECT_REQUEST'` when the
         `result` field carries the marker delimiter, OR
         `'PREFLIGHT_EXIT'` when the outbox shape is
         phase=='preflight' AND exit_code==0 AND attempts>=1 but the
         `result` field is prose narration only (the 2026-05-26
         build-sequence-orchestrator-pr-s1 false-fire shape — Forge
         emits the marker to its session log; `result` summarizes it
         in prose). All three labels mean Forge intentionally did not
         produce a PR.
    Both reconciliation paths emit an INFO log on skip for forensic
    visibility. Only if BOTH paths miss does this check DM Larry.

    Module-internal numbering: this is Check 1 (forge-no-pr). Some
    earlier dispatch narration referred to the same reconciliation as
    'Check 6'; that off-by-five is retired. The log tag
    `FORGE_NO_PR_SKIP` is the canonical forensic identifier."""
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
        if not _within_scan_window(ts):
            # Historical record — not a stall. Skip silently per Scan
            # window discipline (see module docstring).
            seen_tasks.add(task)
            continue
        # Reconciliation step 1: ANY PR (open or merged, across all
        # tracked repos) corresponds to this task?
        matched_pr = None
        match_reason = None
        for pr in all_prs:
            reason = _pr_matches_task(pr, task)
            if reason:
                matched_pr = pr
                match_reason = reason
                break
        if matched_pr is not None:
            log(
                f'FORGE_NO_PR_SKIP task={task} reason=pr_exists '
                f'match={match_reason} pr=#{matched_pr.get("number")} '
                f'repo={matched_pr.get("_repo")}',
                'INFO',
            )
            seen_tasks.add(task)
            continue
        # Reconciliation step 2: archived outbox carries a clean
        # preflight non-PROCEED signal (marker delimiter in result, or
        # phase=preflight + exit_code=0 + attempts>=1 fallback)?
        preflight_marker = _forge_preflight_non_proceed(task)
        if preflight_marker is not None:
            if preflight_marker == 'PREFLIGHT_EXIT':
                reason = 'preflight_exit'
            else:
                reason = 'preflight_non_proceed'
            log(
                f'FORGE_NO_PR_SKIP task={task} reason={reason} '
                f'marker={preflight_marker!r} '
                f'archive={FORGE_OUTBOX_ARCHIVE / (task + ".json")}',
                'INFO',
            )
            seen_tasks.add(task)
            continue
        # Reconciliation step 3: chain_events shows an out-of-band
        # resolution (Larry approved via Approvals tab, or the producer
        # already started a fresh session that supersedes this pointer)?
        hit, reason = _resolution_signal_present(task_id=task, since_ts=ts)
        if hit:
            log(f'FORGE_NO_PR_SKIP task={task} reason={reason}', 'INFO')
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
        if not _within_scan_window(created, now=now):
            # PR opened > SCAN_WINDOW_SECONDS ago — past the point where
            # another DM is the right response. Skip per Scan window.
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
        pr_url = (pr.get('html_url')
                  or f'https://github.com/{pr["_repo"]}/pull/{pr["number"]}')
        hit, reason = _resolution_signal_present(
            task_id=task, since_ts=created,
            check_pr_state=True, pr_url=pr_url,
        )
        if hit:
            log(
                f'PR_NO_MIRROR_DISPATCH_SKIP task={task} reason={reason}',
                'INFO',
            )
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
        if not _within_scan_window(ts):
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
            if ts and _within_scan_window(ts):
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
        hit, reason = _resolution_signal_present(task_id=task, since_ts=ts)
        if hit:
            log(
                f'MIRROR_MARKER_INVISIBLE_SKIP task={task} reason={reason}',
                'INFO',
            )
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
    cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=RETRY_EXHAUST_WINDOW_MIN)
    cutoff_iso = cutoff_dt.strftime('%Y-%m-%d %H:%M:%S UTC')
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
        hit, reason = _resolution_signal_present(
            task_id=task, since_ts=cutoff_dt,
        )
        if hit:
            log(f'RETRY_EXHAUSTED_SKIP task={task} reason={reason}', 'INFO')
            continue
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


# ---------- Check 6: REVIEW_REVISION dispatched with no Forge session ----------

def check_revision_dispatched_with_no_session(notifier_lines: list[str],
                                              state: dict) -> list[dict]:
    """Scan outbox-notifier.log for `no forge_build_session_id` WARN lines
    on REVIEW_REVISION. Each unique task_id seen in the lookback window
    produces one alert. The direct fix in `outbox_notifier.py` already
    DMs Larry via `larry_alerts.append_alert`; this check is defense in
    depth (catches per-subject-cooldown suppression or queue-file write
    failures at the source).

    Chain discipline v3 GAP 1 (2026-05-26). Same 6h-cooldown idempotency
    as Checks 1-5 via the shared state dict + `should_alert/record_alert`.
    """
    alerts: list[dict] = []
    seen_tasks: set[str] = set()
    for line in notifier_lines:
        m = _NO_FORGE_SESSION_RE.search(line)
        if not m:
            continue
        task = m.group('task')
        if task in seen_tasks:
            continue
        ts = _parse_ts(m.group('ts'))
        if not _within_scan_window(ts):
            # Historical WARN replay — direct-fix DM already fired
            # within its own cooldown when the event was fresh; another
            # alert now would be noise. Skip per Scan window.
            seen_tasks.add(task)
            continue
        seen_tasks.add(task)
        hit, reason = _resolution_signal_present(task_id=task, since_ts=ts)
        if hit:
            log(
                f'NO_SESSION_REVISION_SKIP task={task} reason={reason}',
                'INFO',
            )
            continue
        elapsed_min = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
        key = f'no_session_revision:{task}'
        alerts.append({
            'key': key,
            'message': (
                f'REVIEW_REVISION on task `{task}` fired without a Forge build '
                f'session ({elapsed_min} min ago). Auto-resume cannot run — the '
                f'PR was authored outside the dispatch chain (Claude-as-Forge '
                f'or manual). Apply Mirror\'s revisions manually, or re-dispatch '
                f'via Beacon with a fresh task_id to thread a new session.'
            ),
            'subject': f'pipeline-stall:no-session-revision:{task}',
            'suggested_action': (
                f'Read Mirror\'s findings: '
                f'`grep "no forge_build_session_id" ~/agents/logs/outbox-notifier.log | grep "{task}"`. '
                f'Apply the revisions to the PR branch by hand, OR re-dispatch '
                f'via Beacon: `dispatch forge --task fix-{task}-revisions ...`.'
            ),
        })
    return alerts


# ---------- Check 7: Open PRs with no review-request dispatch logged ----------

def _read_recent_routing_events(hours: int) -> list[dict]:
    """Return parsed routing-event records from `ROUTING_EVENTS_LOG` whose
    `timestamp` field is within `hours` of now. Tolerates malformed JSON
    lines (skips them) and missing/empty file (returns [])."""
    if not ROUTING_EVENTS_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[dict] = []
    try:
        with open(ROUTING_EVENTS_LOG, errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts_str = rec.get('timestamp')
                if not isinstance(ts_str, str):
                    continue
                ts = _parse_ts(ts_str)
                if ts and ts >= cutoff:
                    out.append(rec)
    except OSError as e:
        log(f'read {ROUTING_EVENTS_LOG} failed: {e}', 'WARN')
    return out


def check_unrouted_open_prs(open_prs: list[dict],
                            routing_events: list[dict],
                            state: dict) -> list[dict]:
    """Find OPEN PRs older than `PR_UNROUTED_MIN_AGE_MIN` that have NO
    matching review-request dispatch entry in routing-events.jsonl.

    Match heuristic (defensive against task_id ↔ branch drift): the PR's
    headRefName must end with the routing-event's task_id, OR the routing
    event's task_id must appear verbatim in the PR's branch (for
    `forge/<task>` and `larry/<task>` prefixes, this is exact-suffix
    match). A review-request event is identified by
    `source_agent='beacon' AND target_agent_final='mirror' AND
    phase='review'`.

    Chain discipline v3 GAP 3 (2026-05-26). Catches externally-authored
    PRs (Claude-as-Forge, manual pushes) that skip the notifier's
    auto-dispatch entirely. Same 6h-cooldown idempotency as other checks.
    """
    review_dispatch_task_ids: set[str] = set()
    for rec in routing_events:
        if (
            rec.get('source_agent') == 'beacon'
            and rec.get('target_agent_final') == 'mirror'
            and rec.get('phase') == 'review'
        ):
            task = rec.get('task_id')
            if isinstance(task, str) and task:
                review_dispatch_task_ids.add(task)

    now = datetime.now(timezone.utc)
    alerts: list[dict] = []
    for pr in open_prs:
        branch = pr.get('headRefName', '')
        if not branch:
            continue
        created = _parse_ts(pr.get('createdAt', ''))
        if not created:
            continue
        if not _within_scan_window(created, now=now):
            # PR is older than SCAN_WINDOW_SECONDS — long-standing
            # un-routed PR has had ample alert cycles; another DM here
            # is noise. Skip per Scan window.
            continue
        elapsed_min = int((now - created).total_seconds() / 60)
        if elapsed_min < PR_UNROUTED_MIN_AGE_MIN:
            continue
        branch_task = _task_id_from_branch(branch)
        matched = False
        for task_id in review_dispatch_task_ids:
            if branch_task and branch_task == task_id:
                matched = True
                break
            if task_id in branch:
                matched = True
                break
        if matched:
            continue
        pr_html_url = pr.get('html_url') or (
            f'https://github.com/{pr["_repo"]}/pull/{pr["number"]}'
        )
        if branch_task:
            hit, reason = _resolution_signal_present(
                task_id=branch_task, since_ts=created,
                check_pr_state=True, pr_url=pr_html_url,
            )
            if hit:
                log(
                    f'UNROUTED_OPEN_PR_SKIP task={branch_task} '
                    f'reason={reason}',
                    'INFO',
                )
                continue
        key = f'unrouted_open_pr:{pr["_repo"]}:{pr["number"]}'
        alerts.append({
            'key': key,
            'message': (
                f'PR #{pr["number"]} ({pr["_repo"]}) on branch `{branch}` opened '
                f'{elapsed_min} min ago has NO review-request dispatch logged in '
                f'routing-events.jsonl. Externally-authored PRs skip the '
                f'notifier\'s auto-dispatch — Mirror won\'t review until Larry '
                f'manually routes it.'
            ),
            'subject': f'pipeline-stall:unrouted-pr:PR#{pr["number"]}',
            'suggested_action': (
                f'Dispatch a Mirror review via Beacon chat: '
                f'`dispatch mirror review pr={pr_html_url}`. '
                f'Verify routing fires: '
                f'`tail -50 ~/agents/logs/routing-events.jsonl | grep "{branch}"`.'
            ),
        })
    return alerts


# ---------- Check 8: Tier 1 quota+auth + Tier 2 missing/failed/skipped ----------

def _check8_cursor_key(agent: str, outcome: str, reason: str) -> str:
    """Compose the per-signature cursor key. Stable string form so cursor
    file is human-inspectable / grep-friendly."""
    return f'{agent}:{outcome}:{reason}'


def load_check8_cursor() -> dict[str, datetime]:
    """Load the Check 8 cursor map: signature → last-processed-timestamp.

    Safe-resume discipline: on JSON-parse failure (corrupted file from a
    partial write or disk error), log WARN, treat all cursors as empty,
    and write a fresh empty file. Callers then re-process everything in
    the scan window — same behavior as the very first run. Crashing
    instead would mean the healer fails silently per the systemd Type=
    oneshot pattern, which would defeat the purpose of having a cursor."""
    if not CHECK8_CURSOR_FILE.exists():
        return {}
    try:
        with open(CHECK8_CURSOR_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f'check 8 cursor corrupted ({type(e).__name__}: {e}) — '
            f'treating all cursors as empty + writing fresh', 'WARN')
        save_check8_cursor({})
        return {}
    if not isinstance(data, dict):
        log(f'check 8 cursor not a dict ({type(data).__name__}) — '
            f'treating all cursors as empty + writing fresh', 'WARN')
        save_check8_cursor({})
        return {}
    raw_cursors = data.get('cursors')
    if not isinstance(raw_cursors, dict):
        return {}
    out: dict[str, datetime] = {}
    for key, ts_str in raw_cursors.items():
        if not isinstance(key, str) or not isinstance(ts_str, str):
            continue
        ts = _parse_ts(ts_str)
        if ts is not None:
            out[key] = ts
    return out


def save_check8_cursor(cursors: dict[str, datetime]) -> None:
    """Atomically persist the Check 8 cursor map (tmp+rename, per the
    test-isolation PR #137 + PR-S2 atomic-write discipline)."""
    try:
        CHECK8_CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'version': 1,
            'cursors': {
                key: ts.isoformat() for key, ts in sorted(cursors.items())
            },
        }
        tmp = CHECK8_CURSOR_FILE.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(payload, f, indent=2)
        tmp.rename(CHECK8_CURSOR_FILE)
    except OSError as e:
        log(f'save check 8 cursor failed: {e}', 'WARN')


def check_tier2_fallback_failures(state: dict) -> list[dict]:
    """Scan per-agent logs for TIER2_FALLBACK_UNAVAILABLE / FAILED / SKIPPED
    markers within the lookback window. Each unique (agent, outcome,
    reason) combo produces one alert per cooldown window.

    Composes with the in-flight healer-read-discipline PR's
    `SCAN_WINDOW_SECONDS` when present — if that PR has merged by the
    time this code runs, the module-level constant will already be in
    `globals()`; we prefer it. Otherwise we fall back to
    `TIER2_LOG_LOOKBACK_HOURS` (same 24h default).

    Cursor (2026-05-27): per-(agent, outcome, reason) cursor in
    `CHECK8_CURSOR_FILE` records the latest log-line timestamp already
    processed per signature. On entry, skip log lines whose ts is <=
    the cursor for their signature. On exit, advance the cursor to the
    max-ts seen per signature (NEVER backward — defensive against log
    reordering). The cursor composes WITH the scan window (events
    outside the window are filtered upstream) AND with the
    larry_alerts per-subject cooldown (cooldown suppresses DM emission;
    the cursor suppresses re-detection at the log-scan layer). Different
    layers, complementary.
    """
    # Compose with healer-read-discipline if it's landed
    scan_window_secs = globals().get('SCAN_WINDOW_SECONDS')
    if isinstance(scan_window_secs, int) and scan_window_secs > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=scan_window_secs)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=TIER2_LOG_LOOKBACK_HOURS)

    cursor = load_check8_cursor()
    # Working copy — only persist if a value strictly advances. Never-backward
    # invariant is enforced by the `> existing` comparison at advance time.
    new_cursor: dict[str, datetime] = dict(cursor)

    alerts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    logs_dir = AGENTS_ROOT / 'logs'
    for log_name in _TIER2_LOG_NAMES:
        log_path = logs_dir / log_name
        if not log_path.exists():
            continue
        try:
            with open(log_path, errors='replace') as f:
                for line in f:
                    m = _TIER2_FALLBACK_RE.search(line)
                    if not m:
                        continue
                    ts = _parse_ts(m.group('ts'))
                    if not ts or ts < cutoff:
                        continue
                    outcome = m.group('outcome')
                    reason = m.group('reason')
                    agent = log_name.replace('.log', '').replace(
                        'beacon_telegram_bot', 'beacon-bot',
                    )
                    cursor_key = _check8_cursor_key(agent, outcome, reason)
                    # Cursor gate — skip log lines already processed in a
                    # prior run. Cursor is inclusive at the boundary (a
                    # line exactly AT the cursor was processed last run).
                    prior_cursor_ts = cursor.get(cursor_key)
                    if prior_cursor_ts is not None and ts <= prior_cursor_ts:
                        continue
                    # Always advance the working cursor — even if within-run
                    # dedup (the `seen` set) suppresses the alert, the line
                    # was processed and the next run shouldn't see it again.
                    existing = new_cursor.get(cursor_key)
                    if existing is None or ts > existing:
                        new_cursor[cursor_key] = ts
                    sig = (agent, outcome, reason)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    if outcome == 'UNAVAILABLE':
                        cause = (
                            f'Tier 1 hit {reason} and Tier 2 OAuth was not '
                            f'provisioned at /home/larry/.claude-larry-personal/.'
                        )
                        action = (
                            'Provision Tier 2 OAuth per '
                            '`docs/runbooks/restore-larry-personal-claude-oauth-tier2.md` '
                            'to restore the fallback safety net.'
                        )
                    elif outcome == 'FAILED':
                        # 2026-06-02 false-alarm fix: don't word this as lapsed
                        # Tier 2 credentials when Tier 2 OAuth actually verifies.
                        # The alert still fires (both tiers failing is worth
                        # surfacing) but the cause/action reflect the real story
                        # instead of a false "OAuth expired" claim.
                        try:
                            import active_tier as _at
                            _t2_ok = _at.tier_auth_ok('tier2')
                        except Exception:
                            _t2_ok = False
                        if _t2_ok:
                            cause = (
                                f'Tier 1 hit {reason} and the Tier 2 retry also '
                                f'failed, but Tier 2 OAuth verifies OK — likely '
                                f'{reason} (capacity) or a session account-bound '
                                f'resume, not a credentials problem.'
                            )
                            action = (
                                'No Tier 2 OAuth action needed (auth verified); '
                                'transient — clears as the rate-limit window rolls.'
                            )
                        else:
                            cause = (
                                f'Tier 1 hit {reason} and the Tier 2 retry also '
                                f'failed (Tier 2 credentials likely lapsed or '
                                f'silently landed in the wrong account).'
                            )
                            action = (
                                'Re-provision Tier 2 OAuth per '
                                '`docs/runbooks/restore-larry-personal-claude-oauth-tier2.md` '
                                '(the fallback was attempted and rejected by the API).'
                            )
                    else:  # SKIPPED
                        # SKIPPED is expected + auto-remediated (heal_resume_paused_on_tier1
                        # re-dispatches). Log-only per the actionable-only alert discipline;
                        # only FAILED/UNAVAILABLE reach the operator. The cursor already
                        # advanced above, so this line won't re-emit on the next scan.
                        log(
                            f'tier2-fallback SKIPPED (by-design, auto-remediated): '
                            f'agent={agent} reason={reason} — no DM',
                            'INFO',
                        )
                        continue
                    key = f'tier2_fallback:{agent}:{outcome}:{reason}'
                    alerts.append({
                        'key': key,
                        'message': (
                            f'Agent `{agent}` Tier 2 fallback issue: '
                            f'{outcome.lower()} ({reason}). {cause}'
                        ),
                        'subject': (
                            f'pipeline-stall:tier2-fallback-'
                            f'{outcome.lower()}-{reason}:{agent}'
                        ),
                        'suggested_action': action,
                    })
        except OSError as e:
            log(f'read {log_path} failed: {e}', 'WARN')

    # Persist only if anything advanced. Avoids touching the file on
    # idle ticks (no Tier 2 log activity).
    if new_cursor != cursor:
        save_check8_cursor(new_cursor)
    return alerts


# ---------- Main ----------

def _alert_is_fixture(alert: dict) -> bool:
    """True if a stall alert's task is a synthetic test fixture.

    Storm-era fixture build records linger in forge/.archive and would
    otherwise phantom-alert Larry forever (the 2026-05-29/30 fixture-replay
    incident). Skipped at EMISSION so the check functions still detect them
    (their unit tests stay green); only the DM is suppressed. Uses the shared
    fixture_patterns definitions plus a healer-local real-*/prod-* prefix
    guard: in the production pipeline those prefixes are exclusively fixtures
    (legit tasks are descriptively named; the only real "real" task,
    add-real-prefix-*, starts with "add-"). The cross-cutting durable fix is
    the zz-fixture- namespace migration.
    """
    key = alert.get('key', '')
    task = key.split(':', 1)[1] if ':' in key else key
    if not isinstance(task, str):
        return False
    if fixture_patterns.is_fixture_task_id(task) or fixture_patterns.is_fixture_envelope_name(task):
        return True
    return task.startswith('real-') or task.startswith('prod-')


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
        routing_events = _read_recent_routing_events(ROUTING_EVENTS_LOOKBACK_HOURS)
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
    try:
        all_alerts += check_revision_dispatched_with_no_session(notifier_lines, state)
    except Exception as e:
        log(
            f'check_revision_dispatched_with_no_session failed: '
            f'{type(e).__name__}: {e}', 'ERROR'
        )
    try:
        all_alerts += check_unrouted_open_prs(open_prs, routing_events, state)
    except Exception as e:
        log(f'check_unrouted_open_prs failed: {type(e).__name__}: {e}', 'ERROR')
    try:
        all_alerts += check_tier2_fallback_failures(state)
    except Exception as e:
        log(f'check_tier2_fallback_failures failed: {type(e).__name__}: {e}',
            'ERROR')

    if not all_alerts:
        log('no stalls detected', 'INFO')
        save_state(state)
        return 0

    fired = 0
    for alert in all_alerts:
        if _alert_is_fixture(alert):
            log(f'suppressed (fixture task): {alert["key"]}', 'INFO')
            continue
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
