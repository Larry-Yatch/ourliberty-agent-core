#!/usr/bin/env python3
"""
heal_pipeline_stall.py — Proactively DM Larry when work stops flowing.

Adapted from GrowthMastery-ai/gm-agent-core/scripts/pipeline_watcher.py
(2026-04-15, Joe's "you never have to discover a stall on your own"
doctrine). Codifies the manager-duty pattern that Larry + Claude have
been doing manually each session: scan the pipeline state, identify
stalls, DM Larry with what stalled + recommended action.

Runs every 15 minutes via systemd timer. Silent unless something needs
action.

Recover-then-alert (chain-context-durability M4, 2026-06-11). The healer
is no longer surface-only. For the *recoverable* checks (2, 3, 6, 7, 9) it
now attempts an automatic recovery — re-dispatch / re-route through the M1
``build_chain_envelope`` + M2 route-to-owning-agent machinery — BEFORE it
alerts, and DMs Larry only if that recovery fails (the actionable-only
doctrine: "an alert fires only if auto-remediation fails"). The recovery
reuses the same idempotent primitives the notifier and the standalone
healers use (``_dispatch_mirror_review`` + its presence-check, ``merge_pr``,
the no-session-revision / DAG-revision Beacon route), so a recovery that has
already landed is a safe no-op rather than a double-dispatch. The
*detective-only* checks (4, 5, 8) and Check 1 stay alert-only by design —
they have no clean recovery primitive. Five concrete checks:

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

  9. **Sequence stuck pending after unresolved DAG REVISION** — a build
     sequence in `status: pending` whose newest
     `dag-preflight-revision-routed` audit entry is older than
     ``STALLED_PENDING_SEQUENCE_MIN`` (30 min) with no newer
     `dag-preflight-pass-kickoff` (PR-S4 follow-up). The notifier routes
     Mirror's DAG-preflight REVISION to Beacon for autonomous amend +
     re-dispatch; this backstop fires the one Larry-actionable alert when
     that self-heal fails to land and the sequence never activates.
     (Check 8 is the tier2-fallback scan, defined below.)

Every alert: ONE Telegram DM via `larry_alerts.append_alert` (cooldown
1h per unique stall key). Each alert states: what stalled, how long,
recommended action, the specific log grep to run for details.

Recovery posture (M4). For recoverable stalls the healer DOES act — it
re-dispatches a missing Mirror review, retries an unfired auto-merge, or
re-routes a no-session / DAG REVISION to Beacon, then alerts only if the
recovery did not land. It never kills processes. Recovery is bounded by the
same per-key ALERT_DEDUP_HOURS cooldown that gates alerts (one attempt per
key per window), and a successful recovery stamps that cooldown so the next
tick does not re-attempt while the merge/review settles asynchronously. The
detective-only checks (4, 5, 8) and Check 1 stay surface-only — same posture
as Joe's `pipeline_watcher.py` and our `dispatch_sentinel.py`.

Scan window
-----------
All Checks gate their stall-trigger event timestamp against
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

import argparse
import functools
import json
import os
import re
import subprocess
import sys
import glob as _glob
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import larry_alerts  # noqa: E402
import fixture_patterns  # noqa: E402
import no_session_ledger  # noqa: E402  # S3: cold-start obligation backstop
import for_larry_escalations  # noqa: E402  # mirror-review-visibility Contract C/E action surface
import rebase_obligation_ledger  # noqa: E402  # post-open auto-rebase backstop
import safe_write_inbox  # noqa: E402  # sanitize_component: match on-disk outbox names
import pipeline_live_state  # noqa: E402  # canonical "is pipeline work live?" probes
from heal_undispatched_pr_review import task_id_for_branch  # noqa: E402  # canonical PR->task_id key
from id_match import id_matches  # noqa: E402
from log_ts import parse_log_ts  # noqa: E402  (shared log-ts parser)

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
# Production review worktrees live under ~/agent-worktrees (NOT under
# AGENTS_ROOT). Env-overridable for tests, matching heal_phantom_dispatch_claim
# / dashboard_api conventions.
WORKTREES_ROOT = Path(
    os.environ.get('OURLIBERTY_WORKTREES_ROOT', str(HOME / 'agent-worktrees'))
)
# The kernel appends this to /proc/<pid>/cwd when a still-running process's
# working directory (here, a Mirror review worktree) was removed out from
# under it. A live mid-review session still reads cleanly; we strip the suffix
# so a torn-down cwd is name-matched the same way.
_DELETED_CWD_SUFFIX = ' (deleted)'
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
# Check 9 (stalled-pending-sequence backstop, PR-S4 follow-up). Build
# sequences live one JSON file per sequence here. The notifier's DAG-
# preflight REVISION self-heal records a `dag-preflight-revision-routed`
# audit entry; a PASS records `dag-preflight-pass-kickoff`. Check 9 reads
# these to detect a sequence stuck in `pending` after an unresolved REVISION.
BUILD_SEQUENCES_DIR = AGENTS_ROOT / 'blackboard' / 'build-sequences'

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
RETRY_EXHAUST_TASK_SCAN = 12         # journal lines to scan around an exhaustion line for the real task_id
LOG_LOOKBACK_HOURS = 24              # Read at most this far back into outbox-notifier.log
JOURNAL_LOOKBACK_HOURS = 1           # Read at most this far back for retry-exhausted lines
ALERT_DEDUP_HOURS = 1                # Same stall key not re-DMed within this window
STALLED_PENDING_SEQUENCE_MIN = 30    # 30 min — sequence pending after unresolved DAG REVISION
# Check 10 (mirror-review-visibility Contract E, spec §8). Backstop grace before
# a PR sitting on a RED `mirror-review` commit status is treated as the #653
# silent-red shape. Deliberately the longest threshold here: a healthy revision
# loop (Mirror REVISION -> Forge re-push -> re-review) flips the status well
# inside this window, so the backstop only fires after every faster path
# (step-2 routing, the live revision loop, Check 6) has had its chance.
RED_MIRROR_STATUS_MIN = 90           # 90 min — red mirror-review status gone quiet

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
# Check 6 (forge-cold-start-revision S3). The session-less REVISION backstop no
# longer scrapes the notifier log — the message text drifted and the regex went
# dead (the #645 / #653 stalls; S0 was the interim regex fix this supersedes).
# It now reads the durable obligation ledger (`no_session_ledger`): a cold-start
# revision OPENS an obligation, a Mirror PASS / merge RESOLVES it. An obligation
# still OPEN past this grace is a loop the mechanical dispatch did not close —
# surfaced via recover-then-alert (verify the PR didn't resolve out-of-band,
# else fire one loud, non-suppressed alert).
NO_SESSION_STUCK_MIN = 45  # grace before a stuck cold-start obligation alerts

# Check 10 (forge-post-open-mergeable-rebase-001). The post-open auto-rebase
# backstop reads the SIBLING `rebase_obligation_ledger`: the notifier OPENS an
# obligation when it dispatches a phase=rebase to Forge (PR opened CONFLICTING
# because main advanced), and RESOLVES it when the rebased PR comes back
# MERGEABLE and Mirror is dispatched. An obligation still OPEN past this grace is
# a rebase loop the mechanical dispatch did not close — Forge may have aborted a
# conflicted rebase and surfaced a blocker that failed to route, or the rebase
# session died. Same recover-then-alert shape as Check 6.
REBASE_STUCK_MIN = 45  # grace before a stuck rebase obligation alerts

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
# Dashboard "+New mission" PRs (branch prefix below) are reconciled into
# missions.json by heal_orphan_autoregister (it ingests the named mission and
# closes the PR), NOT by a Mirror review — so an open one is not "unrouted" and
# must not page here.
NEWMISSION_BRANCH_PREFIX = 'feat/new-mission-'

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

# Check 10 (Contract E). The commit-status context outbox_notifier posts a
# Mirror verdict under (`_MIRROR_REVIEW_STATUS_CONTEXT` there). A "red" status
# is GitHub state FAILURE or ERROR — the notifier maps every non-PASS verdict
# (REVISION / ESCALATE / EMERGENCY_HALT) to `failure`, so a red status alone
# cannot distinguish those verdicts; the backstop routes to the action surface
# (spec §8) rather than re-deriving Mirror's exact bucket.
MIRROR_REVIEW_STATUS_CONTEXT = 'mirror-review'


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


# ---------- Episode-dedup: per-check re-DM windows (tunable) ----------
# A slow-but-healthy per-PR condition (an unrouted PR traversing the review
# path, a built PR awaiting its review dispatch) legitimately persists for
# hours. The flat 1h ALERT_DEDUP_HOURS therefore re-DMs once per hour for the
# whole life of the condition — the dominant heal_pipeline_stall false-alert
# pattern (PR #86 fired 8x 05:15->12:49 on 2026-06-23, then merged clean). The
# fix is EPISODE semantics: alert on condition onset, stay silent while the
# same condition persists for the same PR, and only re-alert after a long
# backstop window. We implement that by giving the slow-PR checks a per-check
# re-DM window (default 24h) instead of the 1h flat window. The window is
# tunable in config/pipeline-stall-rules.json (Pulse-Check can adjust it) over
# the code defaults below — same optional-config pattern as
# config/review-reaper-rules.json. A genuinely tight loop keeps 1h simply by
# not appearing here (its alerts carry no `re_dm_hours`, so `should_alert`
# falls back to ALERT_DEDUP_HOURS).
PIPELINE_STALL_RULES_FILE = (
    _SCRIPTS_DIR.parent / 'config' / 'pipeline-stall-rules.json'
)
_DEFAULT_RE_DM_HOURS = {
    '_default': 24,
    'forge_built_no_pr': 24,
    'pr_no_mirror_dispatch': 24,
    'mirror_pass_unmerged': 24,
    'unrouted_open_pr': 24,
}


def load_stall_rules() -> dict:
    """Read config/pipeline-stall-rules.json over the code defaults. Missing
    file / malformed JSON / wrong types → defaults (never raises), so the
    healer never crashes on a bad config and criterion-5 (absent file) is the
    plain default path. Read fresh each call (no cache): the healer is a
    per-tick oneshot and the file is tiny, so re-reading a handful of times per
    run is negligible and keeps the loader trivially testable."""
    rules = {'re_dm_hours': dict(_DEFAULT_RE_DM_HOURS)}
    try:
        data = json.loads(PIPELINE_STALL_RULES_FILE.read_text())
    except OSError:
        return rules
    except json.JSONDecodeError as e:
        log(f'pipeline-stall-rules malformed ({e}); using defaults', 'WARN')
        return rules
    if not isinstance(data, dict):
        log('pipeline-stall-rules top-level not an object; using defaults',
            'WARN')
        return rules
    raw = data.get('re_dm_hours')
    if isinstance(raw, dict):
        for check, hours in raw.items():
            if (isinstance(hours, (int, float)) and not isinstance(hours, bool)
                    and hours > 0):
                rules['re_dm_hours'][check] = int(hours)
            else:
                log(f'pipeline-stall-rules re_dm_hours[{check!r}]={hours!r} '
                    f'invalid; keeping default', 'WARN')
    return rules


def re_dm_hours_for(check_name: str) -> int:
    """Episode-dedup re-DM window (hours) for a slow-PR check. Tunable config
    over per-check code default over the `_default` entry over
    ALERT_DEDUP_HOURS."""
    windows = load_stall_rules().get('re_dm_hours', {})
    if check_name in windows:
        return windows[check_name]
    if '_default' in windows:
        return windows['_default']
    return ALERT_DEDUP_HOURS


def should_alert(state: dict, key: str,
                 re_dm_hours: Optional[float] = None) -> bool:
    """True if this stall key hasn't been DMed within its re-DM window.

    `re_dm_hours` overrides the flat ALERT_DEDUP_HOURS for the slow-PR checks
    (episode-dedup, default 24h): fire on onset, stay silent while the same key
    persists, re-alert only after the backstop. `None` keeps the legacy 1h
    window for tight-loop checks."""
    window = ALERT_DEDUP_HOURS if re_dm_hours is None else re_dm_hours
    last = state.get(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace('Z', '+00:00'))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last_dt) > timedelta(hours=window)


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
    """Parse the timestamp shape outbox_notifier writes to an aware UTC
    datetime. Tolerates space-or-T, optional microseconds, optional tz suffix.

    A naive value comes from outbox_notifier.log() (datetime.now() — the
    droplet's LOCAL clock, America/Denver); astimezone() interprets a naive
    datetime as the system-local zone and converts to UTC, so it lines up with
    aware (gh '...Z') timestamps and the aware-UTC `now` every Check compares
    against. heal_pipeline_stall and the notifier run on the SAME host, so the
    local zone matches the writer's. (Stamping naive as UTC instead — the prior
    `dt.replace(tzinfo=timezone.utc)` — skewed every event ~6h into the past,
    so a recent event could look stale: false stall trigger, or clipped scan
    windows. See heal_pr_auto_merge._to_utc / chain_event_shipper._normalize_iso_ts
    for the same 6h-skew incident.) An aware value passes through unchanged
    (same instant, normalized to UTC)."""
    return parse_log_ts(ts_str)


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
                # Capture the WHOLE bracketed timestamp incl. optional fractional
                # seconds and tz offset. The notifier writes a naive local stamp
                # (`2026-05-26 13:08:39`) but inbox_watcher writes an aware UTC one
                # (`2026-05-26T04:46:20.823929+00:00`) — dropping the offset would
                # make _parse_ts read the watcher's UTC digits as host-local and
                # shift them, mis-bounding this coarse lookback window.
                m = re.match(
                    r'\[(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}'
                    r'(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)', line)
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
             'number,title,state,mergedAt,closedAt,headRefName,createdAt'],
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


# gh returns merged PRs newest-first. The fetch limit must comfortably
# exceed the busiest realistic 7-day merge count, or the window is silently
# truncated to "the most-recent N merges". That was the 2026-06-04 bug: on a
# high-velocity repo PR #294 merged ~22h before the alert but already sat
# >30 PRs back, so the old limit=30 dropped it and `_preflight_family_shipped`
# never saw the shipped sibling build. Sized with headroom; if a cycle ever
# hits the cap with the oldest fetched PR still inside the window, we WARN
# instead of truncating silently (so the limit gets bumped, not the bug
# rediscovered).
_MERGED_PR_FETCH_LIMIT = 300


def _all_merged_prs_recent() -> list[dict]:
    """Return MERGED PRs across tracked repos in the last 7 days, augmented
    with `_repo`. Used to detect PRs that merged after a Mirror PASS so we
    can skip the still-OPEN check, and (via `_preflight_family_shipped`) to
    correlate a preflight/clarify task to its already-shipped sibling build."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    out = []
    for repo in REPOS:
        raw = gh_pr_list(repo, state='merged', limit=_MERGED_PR_FETCH_LIMIT)
        for pr in raw:
            merged = pr.get('mergedAt')
            if not merged:
                continue
            ts = _parse_ts(merged)
            if ts and ts >= cutoff:
                pr['_repo'] = repo
                out.append(pr)
        # No silent caps: if we fetched exactly the limit AND the oldest
        # result is still inside the 7-day window, older in-window merges may
        # have fallen off the tail. Surface it; correctness (a missed
        # family-shipped match -> false stall alert) depends on full coverage.
        if len(raw) >= _MERGED_PR_FETCH_LIMIT:
            oldest = raw[-1].get('mergedAt') if raw else None
            oldest_ts = _parse_ts(oldest) if oldest else None
            if oldest_ts and oldest_ts >= cutoff:
                log(
                    f'merged-PR fetch for {repo} hit the '
                    f'{_MERGED_PR_FETCH_LIMIT} cap with the oldest result '
                    f'({oldest}) still inside the 7-day window — older '
                    f'in-window merges may be truncated; bump '
                    f'_MERGED_PR_FETCH_LIMIT.',
                    'WARN',
                )
    return out


def _all_closed_prs_recent() -> list[dict]:
    """Return CLOSED-not-merged PRs across tracked repos in the last 7 days,
    augmented with `_repo`. Used to skip the forge-built-no-PR check for a
    task whose PR was opened and then deliberately CLOSED (abandoned or
    superseded) — a valid resolution, not a stall.

    gh's `--state closed` set ALSO includes merged PRs (GitHub treats merged
    as a closed subset), so we keep ONLY entries whose `state == 'CLOSED'`;
    merged PRs are already handled by `_all_merged_prs_recent` via the
    step-1 pr_exists path. Window-filters on `closedAt` within the same
    7-day cutoff and reuses the identical fetch-limit cap-WARN discipline.
    Fail-safe to [] on gh failure (mirrors the merged helper), preserving the
    legitimate alert path."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    out = []
    for repo in REPOS:
        raw = gh_pr_list(repo, state='closed', limit=_MERGED_PR_FETCH_LIMIT)
        for pr in raw:
            if pr.get('state') != 'CLOSED':
                continue
            closed = pr.get('closedAt')
            if not closed:
                continue
            ts = _parse_ts(closed)
            if ts and ts >= cutoff:
                pr['_repo'] = repo
                out.append(pr)
        # Same no-silent-caps discipline as `_all_merged_prs_recent`: if we
        # fetched exactly the limit AND the oldest result is still inside the
        # 7-day window, older in-window closures may have fallen off the tail.
        if len(raw) >= _MERGED_PR_FETCH_LIMIT:
            oldest = raw[-1].get('closedAt') if raw else None
            oldest_ts = _parse_ts(oldest) if oldest else None
            if oldest_ts and oldest_ts >= cutoff:
                log(
                    f'closed-PR fetch for {repo} hit the '
                    f'{_MERGED_PR_FETCH_LIMIT} cap with the oldest result '
                    f'({oldest}) still inside the 7-day window — older '
                    f'in-window closures may be truncated; bump '
                    f'_MERGED_PR_FETCH_LIMIT.',
                    'WARN',
                )
    return out


def _task_id_from_branch(branch: str) -> Optional[str]:
    """Extract task_id from a `forge/<task_id>`, `larry/<task_id>`, or
    `claude/<task_id>` branch. Returns None if the branch isn't a recognized
    chain pattern.

    `claude/` covers laptop-authored PRs (Claude Code on Larry's machine). A
    laptop PR's review is dispatched under the synthetic task_id
    `pr-<repo>-<num>` (see `_pr_dispatch_task_id`) rather than its branch
    suffix, so recognizing the prefix here lets the branch_task-gated
    resolution-signal / recovery paths engage for `claude/` PRs the same way
    they do for `forge/` — while the `pr-<repo>-<num>` form is what the unrouted
    dispatch-match keys on."""
    for prefix in ('forge/', 'larry/', 'claude/'):
        if branch.startswith(prefix):
            return branch[len(prefix):]
    return None


def _pr_dispatch_task_id(pr: dict) -> Optional[str]:
    """The synthetic `pr-<repo>-<num>` task_id a laptop-authored PR's review is
    dispatched under (the form `_PR_TASK_ID_RE` matches and
    `_mirror_session_active_for_pr` keys on). Derived from the PR's repo +
    number so the unrouted suppressor can recognize a dispatched-or-in-progress
    review on a `claude/`-branch (or `fix|feat|chore`-labeled) PR that carries
    no `forge/<task_id>` branch token. Returns None if repo/number are
    missing/malformed."""
    repo = pr.get('_repo')
    number = pr.get('number')
    if not repo or number is None:
        return None
    repo_short = repo.rsplit('/', 1)[-1]
    return f'pr-{repo_short}-{number}'


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
        from supabase_factory import get_supabase_client  # type: ignore
        client = get_supabase_client(url, key)
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


def _gh_pr_state(pr_url: str) -> Optional[str]:
    """Return the PR's GitHub state ('OPEN' / 'MERGED' / 'CLOSED'), or None on
    ANY gh transport / non-zero exit / JSON error. The None-vs-state distinction
    lets callers tell 'gh could not be reached' apart from 'confirmed OPEN' — so
    an outage never masquerades as a positive signal (verify-before-alarm)."""
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
        return state if isinstance(state, str) and state else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _check_pr_closed_via_gh(pr_url: str) -> Optional[str]:
    """Return the PR state string if MERGED or CLOSED, else None (OPEN, or any
    gh/JSON error — both 'not terminal', preserving the legitimate alert
    behavior). Thin wrapper over `_gh_pr_state`."""
    state = _gh_pr_state(pr_url)
    return state if state in ('MERGED', 'CLOSED') else None


def _pr_is_terminal(pr_url: str, cache: dict[str, Optional[str]]) -> bool:
    """Merge-truth gate (spec §2). True iff the PR is gh-confirmed MERGED or
    CLOSED — i.e. a per-PR stall alert for it should be SUPPRESSED because the
    PR is already terminal (the "alarmed then merged" tail PR #86 showed).

    `cache` collapses the lookup per pr_url within a tick: if the same PR is
    about to alert from two checks (e.g. unrouted + no-mirror-dispatch), only
    ONE `gh pr view` fires. The gate is applied only to alerts that actually
    pass the cooldown — so the gh fan-out is bounded by the number of FIRING
    per-PR alerts (small after episode-dedup), never the full open-PR list.

    Degrades safe: `_gh_pr_state` returns None on any transport / non-zero exit
    / JSON error, and None is NOT in {MERGED, CLOSED}, so an unreadable state is
    treated as non-terminal = still alertable. A gh outage can never silently
    drop a real alert (verify-before-alarm)."""
    if pr_url not in cache:
        cache[pr_url] = _gh_pr_state(pr_url)
    return cache[pr_url] in ('MERGED', 'CLOSED')


_PR_TASK_ID_RE = re.compile(r'^pr-([a-zA-Z0-9_-]+)-([0-9]+)$')


def _forge_pr_task_id_resolved(task: str) -> Optional[str]:
    """For a task_id shaped `pr-<repo>-<num>` (the task is named directly after
    an existing PR number rather than a Forge-built branch), return the gh-truth
    state string ('MERGED' or 'CLOSED') of that named PR, else None.

    `_pr_matches_task` only correlates `forge/<task_id>` branches or title
    tokens, so it never matches a PR named in the task_id itself — those tasks
    fall through to a false `forge_built_no_pr` stall even when the named PR is
    CLOSED-not-merged (e.g. PR #712).

    Fail-safe to None on EVERY non-terminal branch — a non-matching task_id, an
    unmappable repo, an OPEN PR, or any gh/JSON error — so the task falls through
    to the remaining reconciliation steps and existing alert behavior is
    preserved. Suppression happens ONLY on a gh-confirmed CLOSED/MERGED state;
    an outage must never masquerade as a positive skip signal (mirrors the
    verify-before-alarm posture on `_gh_pr_state`)."""
    m = _PR_TASK_ID_RE.match(task)
    if not m:
        return None
    bare_repo, num = m.group(1), m.group(2)
    slug = next(
        (r for r in REPOS if r.split('/', 1)[-1] == bare_repo),
        None,
    )
    if slug is None:
        return None
    url = f'https://github.com/{slug}/pull/{num}'
    return _check_pr_closed_via_gh(url)


# Test seam (mirrors the notifier's `_POST_STATUS_FN_OVERRIDE`). When set,
# replaces the whole `_mirror_review_status` body so Check 10's tests don't
# shell out to real `gh`. Signature: (repo, pr_number) -> (state, head_sha,
# red_since). Production leaves this None.
_MIRROR_REVIEW_STATUS_FN_OVERRIDE: Optional[Any] = None


def _mirror_review_status(
    repo: str, pr_number: int,
) -> tuple[Optional[str], Optional[str], Optional[datetime]]:
    """Read the `mirror-review` commit status on a PR's head (Check 10).

    Returns ``(state, head_sha, red_since)``:
      * ``state`` — the lowercased GitHub status state of the `mirror-review`
        context (``'failure'`` / ``'error'`` / ``'success'`` / ``'pending'``),
        or None when no such context exists OR gh is unreachable. The
        None-vs-state distinction keeps a gh outage from masquerading as
        "not red" — a None simply means "couldn't read", so the check skips
        rather than acting on a guess.
      * ``head_sha`` — the PR head oid (the Contract D dedup identity), or None.
      * ``red_since`` — when the status context was posted (its ``createdAt``),
        falling back to None when gh doesn't surface it (the caller then uses
        the PR's own age as the gate).

    Reads via ``gh pr view --json statusCheckRollup,headRefOid`` — the rollup
    flattens both check-runs and commit statuses; the `mirror-review` verdict is
    a ``StatusContext``. Fail-safe: any transport/parse error → (None, None,
    None)."""
    if _MIRROR_REVIEW_STATUS_FN_OVERRIDE is not None:
        try:
            return _MIRROR_REVIEW_STATUS_FN_OVERRIDE(repo, pr_number)
        except Exception as e:  # noqa: BLE001 — test seam must not crash the healer
            log(f'_mirror_review_status override raised for {repo}#{pr_number}: '
                f'{type(e).__name__}: {e}', 'WARN')
            return None, None, None
    try:
        result = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number), '--repo', repo,
             '--json', 'statusCheckRollup,headRefOid'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S,
            env={**os.environ,
                 'PATH': '/usr/bin:/usr/local/bin:' + os.environ.get('PATH', '')},
        )
        if result.returncode != 0:
            return None, None, None
        data = json.loads(result.stdout or '{}')
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError,
            OSError) as e:
        log(f'_mirror_review_status gh failed for {repo}#{pr_number}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return None, None, None
    head_sha = data.get('headRefOid') if isinstance(data, dict) else None
    if not (isinstance(head_sha, str) and head_sha):
        head_sha = None
    rollup = data.get('statusCheckRollup') if isinstance(data, dict) else None
    if not isinstance(rollup, list):
        return None, head_sha, None
    for ctx in rollup:
        if not isinstance(ctx, dict):
            continue
        if ctx.get('context') != MIRROR_REVIEW_STATUS_CONTEXT:
            continue
        raw_state = ctx.get('state')
        state = raw_state.lower() if isinstance(raw_state, str) else None
        red_since = _parse_ts(ctx['createdAt']) if isinstance(
            ctx.get('createdAt'), str) else None
        return state, head_sha, red_since
    return None, head_sha, None


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
      3. Title fallback: task_id appears in the PR title as a whole,
         boundary-delimited token with a length floor (id_matches) — covers
         re-keyed dispatches and human-titled PRs that carry the original task
         verbatim. A bare substring match here let a short/common task_id match
         an unrelated PR title (audit #19, mirror of task_resolution).
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
    if id_matches(task_id, title):
        return 'title'
    return None


# Preflight/clarify tasks (task_ids containing `-preflight`) never open a
# PR themselves -- they emit a PROCEED that spawns a *separate* build task
# with a fresh timestamp. Example (the 2026-06-04 false-fire that drove
# this): preflight `forge-queue-api-preflight-20260603T231401Z-clarify1`
# produced build branch `forge/build-forge-queue-api-20260603T234656Z`,
# merged as PR #294. `_pr_matches_task` can't correlate the two -- the
# build branch suffix is a different task_id and the PR title doesn't carry
# the preflight task_id -- so the build looks "missing" and the healer
# escalated four times (02:07/03:11/04:17/05:18Z). When the build for the
# same task *family* has already shipped (open OR merged), the preflight
# stall is a false positive. The 12-char floor avoids matching short,
# ambiguous stems (e.g. `build-` / `fix-` families).
_PREFLIGHT_FAMILY_MIN_LEN = 12

# A build branch for a family is `forge/build-<family>-<ts>`, so after we
# strip the prefix + optional `build-` and the `<family>-` head, the
# remainder STARTS with the build task's own timestamp. Anchoring the family
# match to a real timestamp (instead of `stem.startswith(family + '-')`
# accepting ANY suffix) is what stops a DIFFERENT, longer family that merely
# shares a dash-prefix from matching — e.g. preflight family `add-user-auth`
# was falsely silenced by the shipped build of `add-user-auth-v2`.
#
# The stamp shape varies in production branch names — `20260604T045743Z`
# (HHMMSS+Z) and the shorter `20260604T1528` (HHMM, no Z) both occur — so
# accept `YYYYMMDDT` + 4-to-6 digits + optional Z. `.match` (not `.fullmatch`)
# keeps a trailing retry/clarify suffix on the stamp fine.
_BUILD_TS_PREFIX_RE = re.compile(r'\d{8}T\d{4,6}Z?')


def _preflight_family_shipped(task_id: str, prs: list[dict]) -> Optional[dict]:
    """If `task_id` is a preflight/clarify task whose build *family* has
    already shipped as a PR in `prs`, return that PR; else None.

    Family = the task_id stem before `-preflight`. A build PR for the same
    family carries a branch like `forge/build-<family>-<ts>` (or
    `forge/<family>-...`). We strip the `forge/`|`larry/` prefix and an
    optional leading `build-` segment, then accept the PR iff the remainder
    equals `<family>` exactly, or equals `<family>-<ts>` where `<ts>` is a
    real build timestamp. The timestamp anchor is what stops a longer,
    distinct family (`<family>-v2-...`) from matching by mere dash-prefix.
    Only applies to task_ids that contain `-preflight`; non-preflight tasks
    fall through unchanged.
    """
    idx = task_id.find('-preflight')
    if idx <= 0:
        return None
    family = task_id[:idx]
    if len(family) < _PREFLIGHT_FAMILY_MIN_LEN:
        return None
    for pr in prs:
        branch_task = _task_id_from_branch(pr.get('headRefName') or '')
        if not branch_task:
            continue
        stem = branch_task
        if stem.startswith('build-'):
            stem = stem[len('build-'):]
        if stem == family:
            return pr
        head = family + '-'
        if stem.startswith(head) and _BUILD_TS_PREFIX_RE.match(stem[len(head):]):
            return pr
    return None


def _load_forge_outbox(task_id: str) -> Optional[dict]:
    """Load + parse the archived first-attempt Forge outbox `<task_id>.json`.
    Returns the parsed dict, or None on a missing / unreadable / invalid-JSON
    archive. Single source for the first-attempt read shared by the `_forge_*`
    reconciliation helpers below (retry siblings glob `<task_id>.*.json`
    separately in `_forge_retry_succeeded`, so they don't route through here)."""
    archive = FORGE_OUTBOX_ARCHIVE / safe_write_inbox.sanitize_component(f'{task_id}.json')
    if not archive.exists():
        return None
    try:
        with open(archive, errors='replace') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
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
    data = _load_forge_outbox(task_id)
    if data is None:
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


# Reconciliation step 4 / dispatch-spec "Step 5" (pr-create-inferred-failure).
# Applies ONLY to preflight/clarify
# task_ids -- those never open a PR of their own (the build is a separate,
# fresh-timestamp dispatch). When such a pointer's archived outbox shows the
# dispatch was *consumed but errored* (the worker started, then the run
# bailed -- e.g. `exit_code=-1` with `error='All retries exhausted'`, or a
# gh-pr-create auth_401), the `[forge] done success=True` line that triggers
# Check 1 is a stale/optimistic signal: there is no build gap, the run died
# at the infra/auth layer. That genuine loss should surface ONCE under a
# distinct, lower-urgency subject -- not be mislabeled as a build gap that
# re-alarms hourly. Production driver (2026-06-04): clarify task
# `forge-queue-api-preflight-20260603T231401Z-clarify1` archived with
# exit_code=-1, error='All retries exhausted'; Step 4 already suppresses it
# (its sibling build shipped as PR #294), but the same shape with NO sibling
# PR would otherwise repeat the build-gap alarm six times. The preflight/
# clarify gating is what keeps a genuine *build*-phase crash (phase='build',
# task_id without `-preflight`/`-clarify`) firing the original alert.
def _is_preflight_or_clarify_task(task_id: str) -> bool:
    """True if `task_id` is a preflight/clarify pointer (contains
    `-preflight` or `-clarify`). These never open their own PR."""
    return '-preflight' in task_id or '-clarify' in task_id


def _classify_outbox_error(data: dict) -> Optional[str]:
    """Return a short human-readable cause string if the archived outbox
    `data` shows a consumed-but-errored dispatch (no clean PR), else None
    for a clean outbox (exit_code==0/absent, no error, no auth_401).

    Errored signals (any one suffices):
      * `exit_code` present and != 0 (e.g. -1 from 'All retries exhausted').
      * a non-empty `error` field (string).
      * a recognizable gh-pr-create auth_401 signal in `error` / `result`.
    The returned string prefers the `error` field text, names an auth_401
    when detected, and otherwise reports the non-zero exit code. Shared by
    `_forge_pr_create_inferred_failure` (reads the first-attempt outbox)
    and `_forge_retry_succeeded` (reads each retry sibling) so the two stay
    in lockstep on what 'errored' means."""
    exit_code = data.get('exit_code')
    error = data.get('error')
    result = data.get('result')
    error_str = error.strip() if isinstance(error, str) else ''
    haystack = ' '.join(
        s for s in (error_str, result if isinstance(result, str) else '') if s
    ).lower()
    auth_401 = 'auth_401' in haystack or '401' in haystack
    errored = (
        (isinstance(exit_code, int) and exit_code != 0)
        or bool(error_str)
        or auth_401
    )
    if not errored:
        return None
    if auth_401:
        return 'gh pr create auth_401 (credentials lapsed)'
    if error_str:
        return error_str
    return f'exit_code={exit_code}'


def _forge_pr_create_inferred_failure(task_id: str) -> Optional[str]:
    """If the archived Forge outbox for `task_id` shows the dispatch was
    consumed but errored (no clean PR), return a short human-readable cause
    string for the alert message; else None.

    Reads only `<task_id>.json` (the first-attempt outbox). A successful
    retry sibling does NOT change this return value — that recovery is
    reconciled separately by `_forge_retry_succeeded` at the call site, so
    this function stays a pure read of the first attempt. Returns None on a
    missing/unreadable archive or a clean (exit_code==0, no error) outbox --
    the caller then proceeds with the normal build-gap alert decision."""
    data = _load_forge_outbox(task_id)
    if data is None:
        return None
    return _classify_outbox_error(data)


def _outbox_shows_pr(data: dict) -> bool:
    """True iff an archived outbox proves a PR was actually opened/updated by
    that dispatch: a `PR opened:` / `PR updated:` terminal preamble in the
    `result` (agents/forge/CLAUDE.md post-marker exit signals), or a non-empty
    `pr_url` / `html_url` field.

    A clarify outbox that merely resolved with `=== PROCEED ===` does NOT
    count — PROCEED proves the clarification was resolved and a SEPARATE
    downstream build was dispatched, not that any PR exists. Conflating the
    two is the false-positive this guards: a PROCEED sibling whose downstream
    build later crashed without opening a PR must NOT read as 'recovered'."""
    for key in ('pr_url', 'html_url'):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return True
    result = data.get('result')
    if isinstance(result, str):
        low = result.lower()
        if 'pr opened:' in low or 'pr updated:' in low:
            return True
    return False


def _forge_retry_succeeded(task_id: str) -> Optional[str]:
    """If a retry-sibling outbox for `task_id` (`<task_id>.<N>.json`, e.g. the
    marker-error retry `...-clarify1.1.json`) exited cleanly AND carries proof
    a PR was opened, return that sibling's filename; else None.

    `_forge_pr_create_inferred_failure` reads only the first-attempt outbox
    `<task_id>.json`. When the first attempt errored (exit_code=-1, 'All
    retries exhausted') but a marker-error retry SUCCEEDED and opened a PR, the
    first-attempt failure is NOT a loss. This helper detects that recovery so
    Step 5 can suppress the false `pr-create-inferred-failure` alert. It keys on
    the archived OUTBOX (always present), so it stays correct even when the
    sibling PR aged out of the merged-PR query window.

    Two signals are BOTH required, because 'clean exit' alone is not proof of a
    PR: a clarify retry that resolves with `=== PROCEED ===` exits cleanly but
    only dispatches a SEPARATE downstream build, which can itself crash without
    ever opening a PR. Suppressing on PROCEED alone silently masked genuine
    no-PR stalls. So a sibling counts as recovery only when it is both:
      1. clean — the inverse of `_classify_outbox_error` (exit_code==0/absent,
         no error, no auth_401); and
      2. PR-bearing — `_outbox_shows_pr` (a `PR opened:`/`PR updated:` preamble
         or a `pr_url`/`html_url` field).

    The clarify-PROCEED-then-separate-build recovery is instead covered by the
    caller's Step 1b `_preflight_family_shipped`, which cross-checks the live
    open + 7-day merged PR list by branch family (widened to limit=300 in the
    2026-06-04 fix so PR #294-class shipped siblings are no longer truncated).
    That is the authoritative 'a PR exists' signal; this helper only adds the
    residual case where a retry sibling opened a PR DIRECTLY that has since aged
    out of that window.

    The glob `<task_id>.*.json` requires a non-empty middle segment, so it
    naturally excludes the first-attempt `<task_id>.json`. Returns the first
    clean PR-bearing sibling's name; None if the archive dir is missing, no
    retry siblings exist, or no sibling both succeeded and opened a PR."""
    try:
        # The on-disk outbox stem is sanitize(f'{task_id}.json') minus '.json'
        # (the whole filename is sanitized at write, so compose the same way —
        # sanitizing task_id alone diverges for dot-only ids).
        archive_stem = safe_write_inbox.sanitize_component(f'{task_id}.json')[:-len('.json')]
        # Escape the stem so a '*'/'?'/'[' surviving in a task_id is matched
        # literally, not as a glob wildcard; the trailing '.*.json' stays a glob.
        siblings = sorted(FORGE_OUTBOX_ARCHIVE.glob(f'{_glob.escape(archive_stem)}.*.json'))
    except OSError:
        return None
    for sib in siblings:
        try:
            with open(sib, errors='replace') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if _classify_outbox_error(data) is None and _outbox_shows_pr(data):
            return sib.name
    return None


def _forge_already_merged_bridge(task_id: str, merged_prs: list[dict]) -> Optional[dict]:
    """Bridge a `forge built / no PR` task to the PR its work already merged
    under, when that PR's branch + title carry no task_id token so
    `_pr_matches_task` (Reconciliation step 1) cannot correlate it.

    This is the heal_pipeline_stall sibling of build_sequence_advancer's
    `_bridge_already_merged_pr` (PR #610), which closed the same matcher-blind
    class for build-SEQUENCE steps ONLY. A standalone (non-sequence) Forge
    dispatch whose work was already merged under a differently-named PR — e.g.
    a follow-up whose change shipped inside the parent PR before the dispatch
    ran — still fell all the way through to the build-gap alarm. Production
    driver (2026-06-23): task `fix-645-alert-translation-001` — its alert-
    translation entry was already committed to `fix/proposed-retirement-forge-
    matcher` before the dispatch ran, so Forge honestly opened NO PR; the work
    shipped under PR #645, whose branch + title carry none of the task_id token.
    Medic could only durably *silence* that false stall per-fingerprint; this
    closes it at the source so the stall never emits.

    Reads the archived first-attempt outbox `<task_id>.json`; if its `result`
    carries Forge's canonical `NO PR — already merged: #<N>` contract line (or
    the prose-cue fallback), resolves PR #<N> and returns the matching PR dict
    IFF it is confirmed MERGED — first against the already-fetched recent-merged
    list `merged_prs` (scoped to the outbox's `target_repo` when present; else a
    UNIQUE cross-repo number match, so a #<N> that collides between repos can't
    bridge to the wrong PR), then a direct `gh pr view` verify for a PR older
    than the fetch window. Searches `merged_prs` ONLY — never the open-PR list —
    so an OPEN #<N> (a coincidental cross-repo number collision) can't be
    mistaken for the merge; a genuinely-open correlatable PR is already step 1's
    job. Fail-safe: any miss, ambiguity, or error → None — the alert fires,
    today's behavior, so this can only REMOVE false stalls, never mask a real
    one.

    Reuses `sequence_shortcut_helpers` (parse_already_merged_pr_ref /
    qualify_repo / gh_pr_merge_info) under a lazy import guard so a partial
    deploy that lacks the module degrades to None rather than raising — the
    single shared already-merged matcher, not a second copy that could drift."""
    data = _load_forge_outbox(task_id)
    if data is None:
        return None
    result_text = data.get('result')
    if not isinstance(result_text, str) or not result_text:
        return None
    try:
        import sequence_shortcut_helpers as ssh  # lazy: see docstring
    except Exception:
        return None
    pr_number = ssh.parse_already_merged_pr_ref(result_text)
    if pr_number is None:
        return None
    target_repo = data.get('target_repo')
    qualified = ssh.qualify_repo(target_repo) if target_repo else None
    # Prefer the already-fetched recent-merged list (no extra gh call). Scope
    # to the outbox's target_repo when known; else require a UNIQUE cross-repo
    # number match (both tracked repos number independently).
    candidates = [pr for pr in merged_prs if pr.get('number') == pr_number]
    if qualified:
        candidates = [pr for pr in candidates if pr.get('_repo') == qualified]
    if len(candidates) == 1:
        return candidates[0]
    # Not in the fetched window (or ambiguous without a repo to scope by) —
    # verify the named PR directly IFF we know which repo to ask. Refuse to
    # guess otherwise (the safe direction: fall through to the alert).
    if not qualified:
        return None
    info = ssh.gh_pr_merge_info(qualified, pr_number)
    if info is None:
        return None
    url, merged_at = info
    return {'number': pr_number, 'url': url, 'mergedAt': merged_at, '_repo': qualified}


def _forge_sibling_pr_title_shipped(task_id: str,
                                    all_prs: list[dict]) -> Optional[dict]:
    """Bridge a `forge built / no PR` task to the PR a SIBLING dispatch opened
    for the exact same unit of work, identified by an identical `pr_title`.

    Production driver (2026-06-25): task `reconcile-hardening-mission-shipped-001`
    (archived outbox, exit_code=0, phase=build, task_type=doc-only) honestly
    opened NO PR — its `result` is prose ('Done. ... reconciled'). A redispatch
    `reconcile-hardening-mission-shipped-002` carried the IDENTICAL `pr_title`
    (`chore(missions): reconcile orchestrator-terminal-signal-hardening to
    shipped (#672/#673)`) and shipped it: its `result` names PR #688 (MERGED,
    branch `forge/reconcile-hardening-mission-shipped-002`). So -001 is a
    genuinely-superseded duplicate, yet every existing reconciliation step
    missed it: `_pr_matches_task` fails (PR #688's branch + title carry the
    `-002` token, not `-001`); `_preflight_family_shipped` only fires for
    preflight/clarify ids; `_forge_already_merged_bridge` needs -001's own
    `result` to carry the canonical `NO PR — already merged: #<N>` line, which
    it does not; `_resolution_signal_present` matches only a fresh chain_event
    for the SAME task_id, not a sibling. So this false stall fired every cycle.

    Loads this task's archived outbox via `_load_forge_outbox`; reads its own
    `pr_title` (None on missing/empty/non-str — no signal to match on, fail
    safe). Scans the caller's already-built open+merged `all_prs` union for PRs
    whose `title` equals this `pr_title` EXACTLY (full-string equality, never a
    substring) AND whose branch task_id (via `_task_id_from_branch`) DIFFERS
    from this `task_id` (a real sibling dispatch, never self). A PR in `all_prs`
    is already gh-truth (open or merged), so no extra `gh` call is needed.

    Returns the single matching sibling PR dict on exactly one match; None on
    zero matches OR ambiguity (more than one distinct sibling PR) OR any
    exception. Fail-safe contract identical to `_forge_already_merged_bridge`:
    any miss, ambiguity, or error → None, so the alert still fires. This step
    can only REMOVE false stalls, never mask a real one. An exact pr_title match
    plus a different branch task_id is the robust, convention-independent
    duplicate-dispatch signal; a numeric -001/-002 suffix heuristic would be
    brittle and misfire across unrelated task families, so it is NOT used."""
    try:
        data = _load_forge_outbox(task_id)
        if data is None:
            return None
        pr_title = data.get('pr_title')
        if not isinstance(pr_title, str) or not pr_title:
            return None
        matches = []
        for pr in all_prs:
            if (pr.get('title') or '') != pr_title:
                continue
            branch_task = _task_id_from_branch(pr.get('headRefName') or '')
            if branch_task == task_id:
                continue
            matches.append(pr)
        # Exactly one sibling PR is the strong signal; ambiguity → fall through.
        seen_numbers = {pr.get('number') for pr in matches}
        if len(seen_numbers) == 1:
            return matches[0]
        return None
    except Exception:
        return None


def _forge_retry_pr_exists(task_id: str,
                           all_prs: list[dict]) -> Optional[dict]:
    """Bridge a `forge built / no PR` task to the PR a `-retry<N>` REDISPATCH
    opened for it. Returns a representative PR dict (or a synthetic marker dict)
    when proof of a retry-sibling PR exists, else None.

    Production driver (2026-06-25): task `reconcile-hardening-mission-shipped-001`
    (doc-only, exit 0) honestly opened NO PR. A SEPARATE redispatch
    `reconcile-hardening-mission-shipped-001-retry1` opened PR #699 (OPEN, in
    the Mirror queue) on branch `forge/reconcile-hardening-mission-shipped-001-
    retry1`. The sibling-pr_title step does NOT suppress -001 (different driver:
    a retry redispatch can re-key the pr_title or have none), so -001's stall
    keeps firing. The robust, convention-independent signal is the BRANCH: a PR
    whose branch task_id (via `_task_id_from_branch`) is exactly
    `<original-task-id>-retry<N>` proves the retry redispatch opened the PR —
    independent of title text or fetch-window quirks.

    Two independent proofs (either suffices):
      a) a PR in `all_prs` (open OR merged — already gh-truth) whose branch
         task_id matches `^<re.escape(task_id)>-retry\\d+$`; OR
      b) an archived retry-sibling outbox `<task_id>-retry*.json` that
         `_outbox_shows_pr` confirms opened/updated a PR. Covers a retry PR
         that has aged out of the live fetch window — keyed on the always-
         present archive, mirroring `_forge_retry_succeeded`'s archive anchor.

    Fail-safe: any miss / unreadable archive / exception → None, so the alert
    still fires. This step can only REMOVE false stalls, never mask a real one
    (a genuinely no-PR task has no `-retry<N>` PR and no PR-bearing retry
    outbox). Reuses `_task_id_from_branch` + `_outbox_shows_pr` (the established
    conventions), not a fresh matcher that could drift."""
    try:
        retry_branch_re = re.compile(rf'^{re.escape(task_id)}-retry\d+$')
        # Proof (a): a live open/merged PR on the retry branch.
        for pr in all_prs:
            branch_task = _task_id_from_branch(pr.get('headRefName') or '')
            if branch_task and retry_branch_re.match(branch_task):
                return pr
        # Proof (b): an archived retry-sibling outbox that opened a PR (the
        # retry PR may have aged out of the fetch window). The on-disk stem is
        # sanitize(f'{task_id}.json') minus '.json'; escape it so a glob
        # metacharacter surviving a task_id is matched literally, then append
        # the literal `-retry` plus a `*` wildcard for the suffix + `.json`.
        archive_stem = safe_write_inbox.sanitize_component(
            f'{task_id}.json')[:-len('.json')]
        pattern = f'{_glob.escape(archive_stem)}-retry*.json'
        for sib in sorted(FORGE_OUTBOX_ARCHIVE.glob(pattern)):
            try:
                with open(sib, errors='replace') as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and _outbox_shows_pr(data):
                return {'number': None, '_repo': None, '_retry_outbox': sib.name}
        return None
    except Exception:
        return None


def _forge_rebase_target_shipped(task_id: str,
                                 all_prs: list[dict]) -> Optional[dict]:
    """Suppress a `forge built / no PR` stall for a REBASE-class task, which by
    design operates on an EXISTING PR and opens none of its own. Returns the
    targeted PR dict when the task named a PR that exists in the open+merged
    union (gh-truth), else None.

    Applies ONLY when `task_id.startswith('rebase-')` — a deliberately tight
    scope, because rebase tasks are the only class expected to open no PR while
    still doing real work. Every other task is left to the normal build-gap
    decision.

    Production driver (2026-06-25): task `rebase-forge-post-open-mergeable-687-
    001` rebased PR #687 to `MERGEABLE / CLEAN`; its archived `result` reads
    'PR #687 is now MERGEABLE / CLEAN. Done. Rebase complete — PR #687...'.
    PR #687 then MERGED with its branch deleted, so both branch-match and
    title-match fail and the stall fires (live alert L1128). The robust signal:
    the rebase task's own `result` names `#<N>` and PR #<N> is present in the
    already-fetched open+merged `all_prs` union — the work landed on the PR it
    targeted; no new PR is expected.

    Reads the archived outbox via `_load_forge_outbox`, extracts every `#<N>`
    reference from the `result`, and returns the matching PR dict IFF exactly
    one referenced number resolves to a PR in `all_prs`.

    Disambiguation (2026-06-25, fixes the residual original-task false fire):
    when MORE than one referenced number resolves — e.g. the original task
    `rebase-forge-post-open-mergeable-687-001` rebased PR #687 but its `result`
    prose also narrates its now-merged blocker ('Its blocker (#685...) had
    merged'), so `seen_numbers == {685, 687}` — intersect the resolving PR
    numbers with the digit-groups present in the `task_id` itself
    (`re.findall(r'\\d+', task_id)` → {687, 1}). {687, 1} ∩ {685, 687} = {687},
    a single disambiguated target, so return that PR. The task_id's own PR
    number is the authoritative signal for which PR the rebase targeted; the
    other `#<N>` references are incidental narration. If the intersection is
    empty or still names more than one PR, fall through to the fail-safe.

    Fail-safe: missing / unreadable archive, no `#<N>` reference, no resolving
    PR, OR ambiguity that the task_id cannot reduce to exactly one PR → None, so
    the alert still fires. This step can only REMOVE false stalls, never mask a
    real one."""
    try:
        if not task_id.startswith('rebase-'):
            return None
        data = _load_forge_outbox(task_id)
        if data is None:
            return None
        result = data.get('result')
        if not isinstance(result, str) or not result:
            return None
        referenced = {int(n) for n in re.findall(r'#(\d+)', result)}
        if not referenced:
            return None
        matches = [pr for pr in all_prs if pr.get('number') in referenced]
        seen_numbers = {pr.get('number') for pr in matches}
        if len(seen_numbers) == 1:
            return matches[0]
        if len(seen_numbers) > 1:
            task_id_numbers = {int(n) for n in re.findall(r'\d+', task_id)}
            disambiguated = seen_numbers & task_id_numbers
            if len(disambiguated) == 1:
                target = next(iter(disambiguated))
                for pr in matches:
                    if pr.get('number') == target:
                        return pr
        return None
    except Exception:
        return None


def check_forge_built_no_pr(watcher_lines: list[str], open_prs: list[dict],
                            merged_prs: list[dict], state: dict,
                            closed_prs: Optional[list[dict]] = None) -> list[dict]:
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
      a2) `_preflight_family_shipped` for preflight/clarify task_ids:
         the build for the same task *family* shipped under a fresh
         timestamp branch (`forge/build-<family>-<ts>`) that (a) can't
         correlate. Fixes the 2026-06-04 forge-queue-api 4-escalation
         false-fire (preflight task -> merged PR #294).
      a3) `_forge_already_merged_bridge` for ANY task (not just preflight):
         the archived outbox carries Forge's `NO PR — already merged: #<N>`
         contract line naming the gh-MERGED PR the work shipped under, when
         that PR's branch/title carry no task_id token. Sibling of build_
         sequence_advancer's #610 bridge for standalone (non-sequence)
         dispatches. Fixes the 2026-06-23 `fix-645-alert-translation-001`
         false stall (work shipped inside PR #645).
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
      c) `_forge_pr_create_inferred_failure` for preflight/clarify
         task_ids only: the archived outbox shows the dispatch was
         consumed but errored (exit_code != 0, non-empty `error`, or a
         gh-pr-create auth_401). This is an infra/auth loss, not a build
         gap. Instead of the build-gap alert, emit ONE lower-urgency
         `pipeline-stall:pr-create-inferred-failure:<task>` alert (own
         cooldown). Ordered after (a2) so a sibling-shipped task is
         fully suppressed and never reaches here.
    The skip paths (a/a2/b) emit a FORGE_NO_PR_SKIP INFO log; the
    reclassify path (c) emits a FORGE_NO_PR_RECLASSIFY INFO log and a
    distinct-subject alert. Only if every path misses does this check
    DM Larry with the original build-gap alert.

    Module-internal numbering: this is Check 1 (forge-no-pr). Some
    earlier dispatch narration referred to the same reconciliation as
    'Check 6'; that off-by-five is retired. The log tag
    `FORGE_NO_PR_SKIP` is the canonical forensic identifier."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=FORGE_BUILT_NO_PR_MIN)
    all_prs = open_prs + merged_prs
    closed_prs = closed_prs or []
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
        # Reconciliation step 1a: a PR for this task was opened and then
        # CLOSED-not-merged (deliberately abandoned or superseded). gh's
        # `--state merged`/`--state open` fetchers never surface it, so step 1
        # can't correlate it and the task would fall through to a false stall.
        # A closed PR is a valid resolution — skip, exactly analogous to the
        # open/merged pr_exists path. Fixes the `forge-built-no-pr-closed-pr-fp`
        # recurrence (PR #712 CLOSED, refiring each 6h cooldown).
        closed_match = None
        for pr in closed_prs:
            if _pr_matches_task(pr, task):
                closed_match = pr
                break
        if closed_match is not None:
            log(
                f'FORGE_NO_PR_SKIP task={task} reason=pr_closed '
                f'pr=#{closed_match.get("number")} '
                f'repo={closed_match.get("_repo")}',
                'INFO',
            )
            seen_tasks.add(task)
            continue
        # Reconciliation step 1a2: the task_id is itself shaped `pr-<repo>-<num>`
        # — named directly after an existing PR number rather than a Forge-built
        # branch. `_pr_matches_task` only correlates `forge/<task_id>` branches
        # or title tokens, so it never matches a PR named in the task_id itself
        # and the alert fires even when that PR is CLOSED-not-merged (PR #712,
        # CLOSED). `_forge_pr_task_id_resolved` gh-resolves the named PR and
        # returns its state ONLY when MERGED/CLOSED (fail-safe None on
        # non-match / unmappable repo / OPEN / gh error). A terminal state is a
        # valid resolution — skip, exactly analogous to the step-1a closed path.
        pr_task_state = _forge_pr_task_id_resolved(task)
        if pr_task_state is not None:
            log(
                f'FORGE_NO_PR_SKIP task={task} '
                f'reason=pr_task_id_closed_or_merged pr_state={pr_task_state}',
                'INFO',
            )
            seen_tasks.add(task)
            continue
        # Reconciliation step 1b: preflight/clarify task whose build
        # *family* already shipped under a fresh-timestamp branch that
        # `_pr_matches_task` cannot correlate (different task_id). Covers
        # the preflight -> build re-keying (e.g. `...-preflight-...` task
        # -> `forge/build-<family>-<ts>` PR). See `_preflight_family_shipped`.
        family_pr = _preflight_family_shipped(task, all_prs)
        if family_pr is not None:
            log(
                f'FORGE_NO_PR_SKIP task={task} reason=preflight_family_shipped '
                f'pr=#{family_pr.get("number")} repo={family_pr.get("_repo")} '
                f'branch={family_pr.get("headRefName")!r}',
                'INFO',
            )
            seen_tasks.add(task)
            continue
        # Reconciliation step 1c: the archived outbox carries Forge's canonical
        # `NO PR — already merged: #<N>` contract line (or the prose-cue
        # fallback) naming the PR the work already shipped under, when that PR's
        # branch + title carry no task_id token so step 1's `_pr_matches_task`
        # cannot correlate it. Sibling of build_sequence_advancer's #610 bridge,
        # for standalone (non-sequence) dispatches — drove the 2026-06-23
        # `fix-645-alert-translation-001` false stall that Medic could only
        # per-fingerprint silence. gh-truth-gated, fail-safe to None.
        bridged_pr = _forge_already_merged_bridge(task, merged_prs)
        if bridged_pr is not None:
            log(
                f'FORGE_NO_PR_SKIP task={task} reason=already_merged_bridge '
                f'pr=#{bridged_pr.get("number")} repo={bridged_pr.get("_repo")}',
                'INFO',
            )
            seen_tasks.add(task)
            continue
        # Reconciliation step 1d: a SIBLING Forge dispatch with an identical
        # `pr_title` already opened the PR (open or merged), so this task's
        # `built / no PR` is a superseded-duplicate false stall. Drove the
        # 2026-06-25 `reconcile-hardening-mission-shipped-001` recurring
        # false-fire: -001 honestly opened no PR, its redispatch -002 carried
        # the exact same pr_title and shipped it under MERGED PR #688 (branch
        # `forge/reconcile-hardening-mission-shipped-002`), which carries the
        # -002 token so step 1's `_pr_matches_task` couldn't correlate it and
        # the `_forge_already_merged_bridge` line was absent from -001's result.
        # See `_forge_sibling_pr_title_shipped`. gh-truth-gated (scans the
        # already-built open+merged union), fail-safe to None.
        sibling_pr = _forge_sibling_pr_title_shipped(task, all_prs)
        if sibling_pr is not None:
            log(
                f'FORGE_NO_PR_SKIP task={task} reason=sibling_pr_title_shipped '
                f'pr=#{sibling_pr.get("number")} repo={sibling_pr.get("_repo")}',
                'INFO',
            )
            seen_tasks.add(task)
            continue
        # Reconciliation step 1e: a `-retry<N>` REDISPATCH of this task opened
        # the PR, so this original task's `built / no PR` is a superseded false
        # stall. Drove the 2026-06-25 `reconcile-hardening-mission-shipped-001`
        # recurring false-fire: -001 (doc-only, exit 0) opened no PR, its
        # redispatch -001-retry1 opened OPEN PR #699 on branch
        # `forge/reconcile-hardening-mission-shipped-001-retry1`, whose branch
        # task_id carries the `-retry1` token so step 1's `_pr_matches_task`
        # couldn't correlate it and the sibling-pr_title step's driver (an
        # identical pr_title) does not hold for a retry redispatch. See
        # `_forge_retry_pr_exists`: a retry-branch PR in the open+merged union,
        # or a PR-bearing archived retry-sibling outbox (aged-out case).
        # gh-truth / archive-anchored, fail-safe to None.
        retry_pr = _forge_retry_pr_exists(task, all_prs)
        if retry_pr is not None:
            log(
                f'FORGE_NO_PR_SKIP task={task} reason=retry_pr_exists '
                f'pr=#{retry_pr.get("number")} repo={retry_pr.get("_repo")} '
                f'retry_outbox={retry_pr.get("_retry_outbox")!r}',
                'INFO',
            )
            seen_tasks.add(task)
            continue
        # Reconciliation step 1f: a REBASE-class task (task_id starts with
        # `rebase-`) operates on an EXISTING PR by design and opens none of its
        # own. Drove the 2026-06-25 `rebase-forge-post-open-mergeable-687-001`
        # false stall (live alert L1128): its archived `result` names PR #687,
        # which it rebased to MERGEABLE/CLEAN before #687 MERGED with its branch
        # deleted — so both branch-match and title-match fail and the stall
        # fired. See `_forge_rebase_target_shipped`: the rebase task's `result`
        # names `#<N>` and PR #<N> is present in the open+merged union.
        # gh-truth-gated, tightly scoped to `rebase-` ids, fail-safe to None.
        rebase_pr = _forge_rebase_target_shipped(task, all_prs)
        if rebase_pr is not None:
            log(
                f'FORGE_NO_PR_SKIP task={task} reason=rebase_target_shipped '
                f'pr=#{rebase_pr.get("number")} repo={rebase_pr.get("_repo")}',
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
        # Reconciliation step 4 (pr-create-inferred-failure): a preflight/
        # clarify pointer (never opens its own PR) whose archived outbox
        # shows the dispatch was consumed but errored (exit_code != 0,
        # non-empty error, or gh-pr-create auth_401). The `success=True`
        # line is stale — the run died at the infra/auth layer, not a
        # build gap. Suppress the build-gap alarm and surface ONCE under a
        # distinct lower-urgency subject (own cooldown), so a genuine loss
        # stays visible without re-alarming hourly. Ordered AFTER step 1b
        # so a task whose sibling build already shipped is fully suppressed
        # and never reaches this branch.
        if _is_preflight_or_clarify_task(task):
            cause = _forge_pr_create_inferred_failure(task)
            if cause is not None:
                # The first attempt errored, but a marker-error retry
                # sibling (`<task>.N.json`) may have recovered with PROCEED
                # and dispatched the build that opened the PR. That is not an
                # infra/auth loss — suppress rather than re-alarm. Keys on the
                # archived outbox, so it holds even when the sibling build PR
                # re-keyed its branch or aged out of the merged-PR window
                # (the 2026-06-04 clarify1 -> PR #294 two-day false-fire).
                recovered = _forge_retry_succeeded(task)
                if recovered is not None:
                    log(
                        f'FORGE_NO_PR_SKIP task={task} reason=retry_recovered '
                        f'sibling={recovered!r} '
                        f'archive={FORGE_OUTBOX_ARCHIVE / (task + ".json")}',
                        'INFO',
                    )
                    seen_tasks.add(task)
                    continue
                log(
                    f'FORGE_NO_PR_RECLASSIFY task={task} '
                    f'reason=pr_create_inferred_failure cause={cause!r} '
                    f'archive={FORGE_OUTBOX_ARCHIVE / (task + ".json")}',
                    'INFO',
                )
                elapsed_min = int(
                    (datetime.now(timezone.utc) - ts).total_seconds() / 60)
                subject = f'pipeline-stall:pr-create-inferred-failure:{task}'
                alerts.append({
                    'key': subject,
                    'message': (
                        f'Preflight/clarify task `{task}` was consumed '
                        f'{elapsed_min} min ago but errored before opening a '
                        f'PR ({cause}). Likely an infra/auth failure at or '
                        f'after the PR-create step, not a build gap.'),
                    'subject': subject,
                    'suggested_action': (
                        f'Inspect the archived outbox '
                        f'`~/agents/outboxes/forge/.archive/{task}.json` and '
                        f'the Forge session log for the failure cause. If it '
                        f'was a transient infra/auth lapse, re-dispatch the '
                        f'task; no manual `gh pr create` is expected here.'),
                })
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
            # Episode-dedup: a built-but-no-PR condition persists until the PR
            # opens or the worktree is reconciled — alert once per episode, not
            # hourly (spec §1).
            're_dm_hours': re_dm_hours_for('forge_built_no_pr'),
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
            # Episode-dedup (spec §1): a PR awaiting its review dispatch can sit
            # for hours while routing settles — alert once per episode, not
            # hourly.
            're_dm_hours': re_dm_hours_for('pr_no_mirror_dispatch'),
            # Merge-truth gate (spec §2): suppress at fire time if the PR has
            # since merged/closed.
            'pr_url': pr_url,
            # M4: recoverable — re-dispatch the missing Mirror review before alerting.
            'recovery': functools.partial(
                _recover_via_mirror_review, task, branch, pr['_repo'],
                pr.get('title') or '', pr_url,
            ),
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
        pr_url = (pr.get('html_url')
                  or f'https://github.com/{pr["_repo"]}/pull/{pr["number"]}')
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
            # Episode-dedup (spec §1): a PASS-but-unmerged PR persists until
            # auto-merge lands — alert once per episode, not hourly.
            're_dm_hours': re_dm_hours_for('mirror_pass_unmerged'),
            # Merge-truth gate (spec §2): suppress at fire time if the PR has
            # since merged/closed (the exact "alarmed then merged" tail).
            'pr_url': pr_url,
            # M4: recoverable — retry the unfired squash-merge before alerting.
            'recovery': functools.partial(
                _recover_via_auto_merge, pr['_repo'], pr['number'],
                pr.get('title') or '',
            ),
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

# The `All retries exhausted` log line carries no inline `task=` token
# (observed shape: `[forge] [ERROR] All retries exhausted`). The real
# task_id lives on adjacent structured lines emitted in the same journal
# batch: `... [forge] done task=<id> success=False` / `start task=<id>`,
# or the worktree path on a preceding traceback line
# (`/agent-worktrees/wt-forge-<id>`). These resolve the nearest real id.
_EXHAUST_TASK_RE = re.compile(r'\b(?:done|start)\s+task=(\S+)')
_EXHAUST_WORKTREE_RE = re.compile(r'wt-(?:forge|larry|medic|mirror|pulse)-([\w.-]+?)(?=[/\s]|$)')


def _resolve_exhausted_task_id(lines: list[str], idx: int) -> Optional[str]:
    """Resolve the real task_id for an `All retries exhausted` line at
    `lines[idx]`. Tries, in order: inline `task=<id>` on the line itself,
    then a `done/start task=<id>` token on the nearest line in a bounded
    look-back/look-ahead window, then a `wt-<agent>-<id>` worktree path on
    the nearest such line. Returns None when nothing identifiable is found."""
    inline = re.search(r'task=(\S+)', lines[idx])
    if inline:
        return inline.group(1)
    lo = max(0, idx - RETRY_EXHAUST_TASK_SCAN)
    hi = min(len(lines), idx + RETRY_EXHAUST_TASK_SCAN + 1)
    # Nearest-first ordering: probe outward by distance so the closest
    # structured line in the same batch wins. Type priority (done/start
    # over worktree path) is applied within each distance ring.
    for dist in range(1, RETRY_EXHAUST_TASK_SCAN + 1):
        for j in (idx - dist, idx + dist):
            if j < lo or j >= hi:
                continue
            m = _EXHAUST_TASK_RE.search(lines[j])
            if m:
                return m.group(1)
    for dist in range(1, RETRY_EXHAUST_TASK_SCAN + 1):
        for j in (idx - dist, idx + dist):
            if j < lo or j >= hi:
                continue
            m = _EXHAUST_WORKTREE_RE.search(lines[j])
            if m:
                return m.group(1)
    return None


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
    for idx, line in enumerate(lines):
        if 'All retries exhausted' not in line:
            continue
        # The exhaustion line itself carries no inline `task=`; the real id
        # lives on adjacent structured lines in the same journal batch.
        task = _resolve_exhausted_task_id(lines, idx)
        if not task:
            # Unidentifiable exhaustion: un-actionable (nothing to point a
            # human at) and un-resolvable (no id for the resolution-signal
            # path), so it would page forever. Suppress instead of emitting
            # a literal `unknown` subject.
            log('RETRY_EXHAUSTED_SKIP task=<unidentifiable> reason=no_task_id', 'INFO')
            continue
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

def check_revision_dispatched_with_no_session(state: dict) -> list[dict]:
    """Backstop for the cold-start no-session revision loop (forge-cold-start-revision S3).

    Reads the durable obligation ledger (`no_session_ledger`) instead of
    scraping the notifier log — the old regex drifted out of match and the
    backstop went dead (the #645 / #653 stalls). A cold-start revision OPENS an
    obligation; a terminal Mirror verdict / merge RESOLVES it. An obligation
    still OPEN past `NO_SESSION_STUCK_MIN` means the mechanical dispatch ran but
    the loop never closed — Forge may have flagged a finding it could not
    mechanically apply, or the loop stalled.

    Each stuck obligation gets ONE recover-then-alert entry: the recovery
    verifies the PR didn't resolve out-of-band (merged/closed → clear the
    ledger, suppress the alert); otherwise a loud, non-suppressed alert fires
    with the PR link. No silent re-deposit — the mechanical cold-start dispatch
    already ran, so a blind re-dispatch would risk a loop. Same 6h-cooldown
    idempotency as Checks 1-5 via `should_alert`/`record_alert`.
    """
    try:
        stuck = no_session_ledger.list_open(
            older_than_minutes=NO_SESSION_STUCK_MIN,
        )
    except Exception as e:  # noqa: BLE001 — a ledger hiccup must not crash the timer
        log(f'no_session_ledger.list_open failed: {type(e).__name__}: {e}',
            'WARN')
        return []
    now = datetime.now(timezone.utc)
    alerts: list[dict] = []
    for ob in stuck:
        task = ob.get('task_id')
        if not task:
            continue
        pr_url = ob.get('pr_url') or ''
        last = _parse_ts(ob.get('last_dispatch_at') or ob.get('opened_at'))
        elapsed_min = int((now - last).total_seconds() / 60) if last else 0
        round_num = ob.get('round', 1)
        key = f'no_session_revision:{task}'
        pr_ref = pr_url if pr_url and pr_url != '(no pr_url)' else 'unknown'
        alerts.append({
            'key': key,
            'message': (
                f'A cold-start (session-less) revision on task `{task}` '
                f'(round {round_num}, PR {pr_ref}) was dispatched {elapsed_min} '
                f'min ago but never closed — no Mirror PASS and the PR is still '
                f'open. Forge may have flagged a finding it could not '
                f'mechanically apply, or the revision loop stalled.'
            ),
            'subject': f'pipeline-stall:no-session-revision:{task}',
            'suggested_action': (
                f'Review the PR ({pr_ref}). Inspect the cold-start revision + '
                f'Forge\'s result: `grep "{task}" '
                f'~/agents/logs/outbox-notifier.log | tail -20`. Apply the '
                f'remaining finding by hand, or close/merge the PR — the '
                f'obligation clears on a Mirror PASS or a merge. Ledger: '
                f'`python3 ~/agent-core/scripts/no_session_ledger.py get {task}`.'
            ),
            # M4 recover-then-alert: verify the PR didn't resolve out-of-band
            # before alerting. NOT a re-dispatch — the mechanical cold-start
            # dispatch already ran.
            'recovery': functools.partial(
                _recover_no_session_revision, task, pr_url,
            ),
        })
    return alerts


# ---------- Check 10: post-open auto-rebase obligation stuck OPEN ----------

def check_rebase_obligation_stuck(state: dict) -> list[dict]:
    """Backstop for the post-open auto-rebase loop (forge-post-open-mergeable-rebase-001).

    Reads the durable `rebase_obligation_ledger`. The notifier OPENS an
    obligation when it dispatches a phase=rebase to Forge (a PR opened
    CONFLICTING because main advanced) and RESOLVES it when the rebased PR comes
    back MERGEABLE and Mirror is dispatched. An obligation still OPEN past
    `REBASE_STUCK_MIN` means the loop never closed — Forge may have aborted a
    conflicted rebase and surfaced a blocker that failed to route, the
    re-emitted `PR updated:` was dropped, or the rebase round cap was hit while
    main kept advancing.

    Each stuck obligation gets ONE recover-then-alert entry: the recovery
    verifies the PR didn't resolve out-of-band (merged/closed → clear the
    ledger, suppress the alert); otherwise a loud, non-suppressed alert fires
    with the PR link. No silent re-dispatch — the mechanical rebase dispatch
    already ran. Same 6h-cooldown idempotency as the other checks.
    """
    try:
        stuck = rebase_obligation_ledger.list_open(
            older_than_minutes=REBASE_STUCK_MIN,
        )
    except Exception as e:  # noqa: BLE001 — a ledger hiccup must not crash the timer
        log(f'rebase_obligation_ledger.list_open failed: {type(e).__name__}: {e}',
            'WARN')
        return []
    now = datetime.now(timezone.utc)
    alerts: list[dict] = []
    for ob in stuck:
        task = ob.get('task_id')
        if not task:
            continue
        pr_url = ob.get('pr_url') or ''
        last = _parse_ts(ob.get('last_dispatch_at') or ob.get('opened_at'))
        elapsed_min = int((now - last).total_seconds() / 60) if last else 0
        round_num = ob.get('round', 1)
        pr_ref = pr_url if pr_url and pr_url != '(no pr_url)' else 'unknown'
        alerts.append({
            'key': f'rebase_obligation:{task}',
            'message': (
                f'A post-open auto-rebase on task `{task}` (round {round_num}, '
                f'PR {pr_ref}) was dispatched {elapsed_min} min ago but never '
                f'closed — the PR opened CONFLICTING with main and the rebase '
                f'loop did not reach a MERGEABLE state. Forge may have aborted a '
                f'conflicted rebase and surfaced a blocker that failed to route, '
                f'or main kept advancing past the rebase retry cap.'
            ),
            'subject': f'pipeline-stall:rebase-obligation:{task}',
            'suggested_action': (
                f'Review the PR ({pr_ref}). Inspect the rebase dispatch + '
                f'Forge\'s result: `grep "{task}" '
                f'~/agents/logs/outbox-notifier.log | tail -20`. Rebase by hand '
                f'(`gh pr checkout <N> && git fetch origin && git rebase '
                f'origin/main && git push --force-with-lease`), or close/merge '
                f'the PR — the obligation clears on a MERGEABLE re-check or a '
                f'merge. Ledger: `python3 ~/agent-core/scripts/'
                f'rebase_obligation_ledger.py get {task}`.'
            ),
            # recover-then-alert: verify the PR didn't resolve out-of-band before
            # alerting. NOT a re-dispatch — the mechanical rebase dispatch ran.
            'recovery': functools.partial(
                _recover_rebase_obligation, task, pr_url,
            ),
        })
    return alerts


# ---------- Check 9: Sequence stuck pending after unresolved DAG REVISION ----------
def check_stalled_pending_sequence(state: dict) -> list[dict]:
    """Scan build-sequences for any sequence stuck in `status: pending`
    with an UNRESOLVED Mirror DAG-preflight REVISION older than the
    threshold (PR-S4 follow-up, 2026-06-10).

    Background: the notifier's REVISION self-heal routes a
    `dag-preflight-revision` notify to Beacon, who is expected to amend the
    sequence's DAG + re-dispatch the preflight; on Mirror's PASS the notifier
    flips the sequence `pending` → `active` (recording a
    `dag-preflight-pass-kickoff` audit entry). If that resume fails or
    bounces, the sequence sits `pending` forever with no human in the loop —
    exactly the silent stall this backstop catches. This IS Larry-actionable:
    work he kicked off is stuck and the auto-heal didn't land.

    Durable signal: the `dag-preflight-revision-routed` audit entry the
    notifier writes when it routes the REVISION (log-retention-independent).
    A sequence is STALLED iff:
      * status == 'pending', AND
      * its newest `dag-preflight-revision-routed` audit entry is older than
        STALLED_PENDING_SEQUENCE_MIN, AND
      * no `dag-preflight-pass-kickoff` entry is newer than that REVISION
        (Mirror hasn't PASSed the amended sequence yet).

    Scan window: the REVISION ts must be within SCAN_WINDOW_SECONDS — a
    long-abandoned pending sequence is historical record, not a live stall.

    Dedup: keyed on `stalled_pending_sequence:<seq_id>:<revision_ts>` so a
    fresh REVISION round (new ts) re-arms, while the same unresolved REVISION
    fires once per ALERT_DEDUP_HOURS via the shared cooldown.
    """
    alerts: list[dict] = []
    if not BUILD_SEQUENCES_DIR.is_dir():
        return alerts
    now = datetime.now(timezone.utc)
    for seq_path in sorted(BUILD_SEQUENCES_DIR.glob('*.json')):
        try:
            seq = json.loads(seq_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(
                f'check_stalled_pending_sequence: skip {seq_path.name} '
                f'({type(e).__name__}: {e})', 'WARN',
            )
            continue
        if not isinstance(seq, dict) or seq.get('status') != 'pending':
            continue
        audit = seq.get('audit_log')
        if not isinstance(audit, list):
            continue
        latest_rev_ts: Optional[datetime] = None
        latest_pass_ts: Optional[datetime] = None
        for entry in audit:
            if not isinstance(entry, dict):
                continue
            ts_raw = entry.get('ts')
            ts = _parse_ts(ts_raw) if isinstance(ts_raw, str) else None
            if ts is None:
                continue
            event = entry.get('event')
            if event == 'dag-preflight-revision-routed':
                if latest_rev_ts is None or ts > latest_rev_ts:
                    latest_rev_ts = ts
            elif event == 'dag-preflight-pass-kickoff':
                if latest_pass_ts is None or ts > latest_pass_ts:
                    latest_pass_ts = ts
        if latest_rev_ts is None:
            continue  # no REVISION routed → not this stall shape
        # Resolved: a PASS-kickoff at/after the REVISION means Mirror
        # re-reviewed the amended sequence + the notifier activated it.
        if latest_pass_ts is not None and latest_pass_ts >= latest_rev_ts:
            continue
        if not _within_scan_window(latest_rev_ts, now):
            continue  # historical abandoned pending sequence, not a stall
        elapsed_min = int((now - latest_rev_ts).total_seconds() / 60)
        if elapsed_min < STALLED_PENDING_SEQUENCE_MIN:
            continue  # give Beacon's resume time to land
        seq_id = seq.get('seq_id') or seq_path.stem
        key = f'stalled_pending_sequence:{seq_id}:{latest_rev_ts.isoformat()}'
        alerts.append({
            'key': key,
            'message': (
                f'Build sequence `{seq_id}` is stuck in `status: pending` '
                f'{elapsed_min} min after a Mirror DAG-preflight REVISION was '
                f'routed to Beacon for auto-amend ({seq_path}). The self-heal '
                f'(Beacon amends the DAG + re-dispatches the preflight, then '
                f'Mirror PASS activates it) did not land — the sequence never '
                f'activated. Work you kicked off is stalled.'
            ),
            'subject': f'stalled-pending-sequence:{seq_id}',
            'suggested_action': (
                f'Inspect the sequence: `cat {seq_path}`. Read Mirror\'s '
                f'finding: `grep "MIRROR_DAG_PREFLIGHT seq={seq_id} '
                f'verdict=REVISION" ~/agents/logs/outbox-notifier.log`. Then '
                f'amend the DAG + re-dispatch the preflight via Beacon, or '
                f'cancel the sequence if it is no longer needed.'
            ),
            # M4: recoverable — re-route the DAG REVISION to Beacon before alerting.
            'recovery': functools.partial(
                _recover_stalled_sequence, seq_id, str(seq_path),
            ),
        })
    return alerts


# ---------- Check 10: Silent red mirror-review status (Contract E) ----------

def _self_heal_in_progress(task_id: str) -> bool:
    """True iff a no-session self-heal obligation is OPEN for this task — the
    mechanical cold-start re-dispatch (`_dispatch_revision_to_forge`) is running
    and Check 6 owns it once it goes stuck. Fail-safe: a ledger error returns
    False (don't suppress a genuine silent-red on infra trouble)."""
    if not task_id:
        return False
    try:
        ob = no_session_ledger.get_obligation(task_id)
    except Exception as e:  # noqa: BLE001
        log(f'_self_heal_in_progress ledger read failed for {task_id}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False
    return isinstance(ob, dict) and ob.get('status') == no_session_ledger.OPEN


def _larry_artifact_exists(task_id: str, head_sha: Optional[str]) -> bool:
    """True iff a Larry-facing artifact already covers this PR — an OPEN
    for-Larry "Waiting on You" record (action bucket) OR a decision approval on
    the Approvals tab (decision bucket). Uses the SAME id derivations the step-2
    routing site uses (`mirror-review:<task>` and `mirror-review-<task>[-<sha8>]`)
    so the backstop never double-notifies a case step 2 already surfaced
    (Contract D). Fail-safe: read errors return False."""
    record_id = f'mirror-review:{task_id}'
    try:
        rec = for_larry_escalations.get(record_id)
        if isinstance(rec, dict) and rec.get('resolved') is not True:
            return True
    except Exception as e:  # noqa: BLE001
        log(f'_larry_artifact_exists feed read failed for {task_id}: '
            f'{type(e).__name__}: {e}', 'WARN')
    try:
        import beacon_approval_handler as approval
        base = f'mirror-review-{task_id}'
        if approval.find_by_id_any_state(base) is not None:
            return True
        if head_sha and approval.find_by_id_any_state(
                f'{base}-{head_sha[:8]}') is not None:
            return True
    except Exception as e:  # noqa: BLE001
        log(f'_larry_artifact_exists approval read failed for {task_id}: '
            f'{type(e).__name__}: {e}', 'WARN')
    return False


def check_red_mirror_status_no_artifact(open_prs: list[dict],
                                        state: dict) -> list[dict]:
    """Backstop the #653 silent-red failure mode (mirror-review-visibility
    Contract E, spec §8).

    Catches an OPEN PR sitting on a RED `mirror-review` commit status
    (state=failure/error) past `RED_MIRROR_STATUS_MIN` where BOTH:
      * no self-heal is in progress (no OPEN no_session_ledger obligation), AND
      * no Larry-facing artifact exists (no OPEN Waiting-on-You record, no
        Approvals-tab decision).

    That is exactly the shape #653 hit: a session-less PR Mirror wants revised
    whose findings reach no one — recoverable only by manual digging. The M4
    recover-then-alert posture (spec §8): the `recovery` promotes it to its
    Contract C surface (the action-needed durable for-Larry record) FIRST; the
    fallback larry_alert fires ONLY if even that write fails — so a healthy
    catch produces exactly one Waiting-on-You record and zero double-notify with
    step-2 routing.
    """
    now = datetime.now(timezone.utc)
    alerts: list[dict] = []
    for pr in open_prs:
        repo = pr.get('_repo')
        number = pr.get('number')
        branch = pr.get('headRefName', '') or ''
        if not repo or number is None:
            continue
        # Cheap pre-filter: a red status cannot have aged past the threshold on a
        # PR younger than it, so skip the per-PR gh status call for fresh PRs.
        created = _parse_ts(pr.get('createdAt', ''))
        if created and (now - created).total_seconds() < RED_MIRROR_STATUS_MIN * 60:
            continue
        status_state, head_sha, red_since = _mirror_review_status(repo, number)
        # Only a confirmed RED status is in scope. None (no context / gh
        # unreachable) and the green/pending states are skipped — verify before
        # alarm; an outage must not look like a stall.
        if status_state not in ('failure', 'error'):
            continue
        # Gate on how long the status has been red. Prefer the status's own
        # post time; fall back to the PR's age when gh didn't surface it.
        red_since = red_since or created
        if red_since is None:
            continue
        if not _within_scan_window(red_since, now=now):
            continue  # long-abandoned red PR is historical record, not a stall
        elapsed_min = int((now - red_since).total_seconds() / 60)
        if elapsed_min < RED_MIRROR_STATUS_MIN:
            continue
        # Canonical task_id — the SAME key the review-request envelope (and
        # therefore step-2 routing) used, so the artifact + ledger guards and
        # the recovery's record_id line up exactly (no double-notify).
        task_id = task_id_for_branch(branch, number, repo)
        if _self_heal_in_progress(task_id):
            continue
        if _larry_artifact_exists(task_id, head_sha):
            continue
        pr_url = f'https://github.com/{repo}/pull/{number}'
        record_id = f'mirror-review:{task_id}'
        alerts.append({
            'key': f'red_mirror_status:{repo}:{number}',
            'message': (
                f'PR #{number} ({repo}) on branch `{branch}` has carried a RED '
                f'`mirror-review` status for {elapsed_min} min with no self-heal '
                f'in progress and nothing on your surfaces (no Waiting-on-You '
                f'record, no approval). Mirror wants changes but the finding is '
                f'reachable only by digging into the PR — the #653 silent-red '
                f'shape.'
            ),
            'subject': f'pipeline-stall:red-mirror-status:PR#{number}',
            'suggested_action': (
                f'Review the PR ({pr_url}) — Mirror\'s findings are on its '
                f'review/commit-status. Then re-dispatch a Forge build for '
                f'`{task_id}` via Beacon, or close the PR.'
            ),
            # M4 / Contract E: recoverable — promote to the action-needed
            # Waiting-on-You surface before alerting.
            'recovery': functools.partial(
                _recover_red_mirror_status, task_id, pr_url, head_sha, record_id,
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


def _mirror_session_active_for_pr(
    repo_short: str, pr_number: int,
) -> tuple[bool, str]:
    """Is a Mirror review session live (or freshly dispatched) for this PR?

    Thin delegator to the canonical probe — the logic now lives in
    `pipeline_live_state.pr_review_in_progress` (de-duped from this module's
    original #716 implementation). Kept under its original name + signature so
    the `check_unrouted_open_prs` call site (and its tests) are unchanged.

    Used by check_unrouted_open_prs to suppress the `unrouted_open_pr` alert
    while Mirror is mid-review — the stall checker otherwise has no visibility
    into active agent sessions and re-fires after its 6h cooldown (G-rule
    unrouted-open-pr-active-mirror-session-fp-001).
    """
    return pipeline_live_state.pr_review_in_progress(repo_short, pr_number)


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
        if branch.startswith(NEWMISSION_BRANCH_PREFIX):
            # Dashboard "+New mission" PR — reconciled into missions.json + closed
            # by heal_orphan_autoregister, not Mirror-reviewed. Not "unrouted".
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
        # A laptop-authored PR (claude/ branch, or fix|feat|chore-labeled) has
        # its review dispatched under the synthetic `pr-<repo>-<num>` task_id,
        # not a `forge/<task_id>` branch token — so match against BOTH the
        # branch task and the pr-<repo>-<num> form. Without the latter, a
        # dispatched-or-in-progress review on a laptop PR escapes the suppressor
        # and false-alerts (spec §3).
        pr_task_id = _pr_dispatch_task_id(pr)
        candidate_task_ids = [t for t in (branch_task, pr_task_id) if t]
        matched = False
        for task_id in review_dispatch_task_ids:
            if any(c == task_id for c in candidate_task_ids):
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
        # Out-of-band resolution: query chain_events under each candidate id
        # (branch task AND/OR pr-<repo>-<num>). The PR-state (MERGED/CLOSED)
        # arm is keyed on pr_url, identical across candidates, so run it once
        # (first candidate) to avoid a duplicate `gh pr view`.
        resolved = False
        seen_candidates: set[str] = set()
        for idx, cand in enumerate(candidate_task_ids):
            if cand in seen_candidates:
                continue
            seen_candidates.add(cand)
            hit, reason = _resolution_signal_present(
                task_id=cand, since_ts=created,
                check_pr_state=(idx == 0), pr_url=pr_html_url,
            )
            if hit:
                log(
                    f'UNROUTED_OPEN_PR_SKIP task={cand} reason={reason}',
                    'INFO',
                )
                resolved = True
                break
        if resolved:
            continue
        # Active-Mirror suppression (G-rule unrouted-open-pr-active-mirror-
        # session-fp-001): the alert means "no review will happen until Larry
        # routes it", but a live review session (or a freshly dispatched review
        # task) is exactly that routing in flight. The stall checker has no
        # session visibility on its own, so it re-fires after the 6h cooldown
        # mid-review. Skip while a Mirror session is active for this PR.
        repo_short = pr['_repo'].rsplit('/', 1)[-1]
        mirror_active, mirror_reason = _mirror_session_active_for_pr(
            repo_short, pr['number'],
        )
        if mirror_active:
            log(
                f'MIRROR_ACTIVE_SKIP task=pr-{repo_short}-{pr["number"]} '
                f'reason={mirror_reason}',
                'INFO',
            )
            continue
        key = f'unrouted_open_pr:{pr["_repo"]}:{pr["number"]}'
        alert = {
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
            # Episode-dedup (spec §1): the dominant false-alert pattern — a slow
            # but progressing PR traversing the routing path re-DMed hourly
            # (PR #86: 8 fires, then merged). Alert once per episode.
            're_dm_hours': re_dm_hours_for('unrouted_open_pr'),
            # Merge-truth gate (spec §2): suppress at fire time if the PR has
            # since merged/closed.
            'pr_url': pr_html_url,
        }
        # M4: recoverable ONLY when the branch yields a dispatchable task_id —
        # the review-request idempotency key is task-keyed, so a branch with no
        # parseable task (e.g. a non-forge/ external branch) has no clean
        # recovery primitive and stays alert-only.
        if branch_task:
            alert['recovery'] = functools.partial(
                _recover_via_mirror_review, branch_task, branch, pr['_repo'],
                pr.get('title') or '', pr_html_url,
            )
        alerts.append(alert)
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


# ---------- Recovery primitives (M4: recover-then-alert) ----------
#
# Each returns True iff the recoverable stall was remediated this tick. They
# reuse the notifier's / the standalone healers' canonical, idempotent
# dispatch primitives so a recovery that has already landed (or a duplicate
# re-attempt on the next tick) is a safe no-op rather than a double-dispatch.
# They are fail-safe: any unexpected exception is logged and surfaces as
# `False` (recovery failed → the run() loop falls through to the alert),
# never as a crash that would take the whole healer down.


def _recover_via_mirror_review(task: str, branch: str, repo: str,
                               title: str, pr_url: str) -> bool:
    """Recover an open PR with no Mirror review dispatched (Checks 2 & 7).

    Writes a review-request to Mirror's inbox via the notifier's canonical
    `_dispatch_mirror_review`, then verifies it landed via the shared
    `_review_request_already_dispatched` presence-check. Idempotent: the
    inner dispatch's own presence-gate skips a duplicate write, and an
    already-present review counts as success. `repo` may be the full
    `owner/name`; the notifier wants the short name."""
    try:
        import outbox_notifier as notifier
    except Exception as e:  # noqa: BLE001
        log(f'recover(mirror-review) import failed for {task}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False
    data = {
        'task_id': task,
        'target_repo': repo.split('/')[-1],
        'branch': branch,
        'pr_title': title or '',
        'dispatched_by': 'heal-pipeline-stall',
    }
    try:
        notifier._dispatch_mirror_review(data, pr_url)
    except Exception as e:  # noqa: BLE001 — inner call swallows routing/cost denials
        log(f'recover(mirror-review) dispatch raised for {task}: '
            f'{type(e).__name__}: {e}', 'WARN')
    try:
        fname = safe_write_inbox.canonical_inbox_name(f'review-{task}.json')
        landed = notifier._review_request_already_dispatched(fname)
    except Exception as e:  # noqa: BLE001
        log(f'recover(mirror-review) verify failed for {task}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False
    log(f'recover(mirror-review) task={task} landed={landed}', 'INFO')
    return landed


def _recover_via_auto_merge(repo: str, pr_number: int, title: str) -> bool:
    """Recover a Mirror-PASS PR that never merged (Check 3) by retrying the
    squash-merge via `heal_pr_auto_merge.merge_pr`. Returns True on a
    `merged` or `already_merged` outcome; `failed` (conflict / branch
    protection / network) falls through to the alert."""
    try:
        import heal_pr_auto_merge
    except Exception as e:  # noqa: BLE001
        log(f'recover(auto-merge) import failed for {repo}#{pr_number}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False
    try:
        outcome, reason = heal_pr_auto_merge.merge_pr(repo, pr_number, title)
    except Exception as e:  # noqa: BLE001
        log(f'recover(auto-merge) raised for {repo}#{pr_number}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False
    log(f'recover(auto-merge) {repo}#{pr_number} outcome={outcome}: {reason}',
        'INFO')
    return outcome in ('merged', 'already_merged')


def _route_revision_notify_to_beacon(*, task_id: str, intent: str,
                                     notify_task_id: str, filename: str,
                                     base_extra: dict, intent_kwargs: dict,
                                     body: str) -> bool:
    """Shared M2 route: write an inter-agent notify to Beacon's inbox via the
    notifier's `build_notify_prompt` + `build_chain_envelope` (M1) +
    `safe_write_inbox`. Every chain-context field is an explicit DROP — these
    are agent-to-agent routing signals whose payload is the intent_kwargs, not
    per-task chain context. Deterministic filename → re-processing overwrites
    the pending notify (atomic same-path write) rather than duplicating it.
    Returns True iff the notify was written."""
    try:
        import outbox_notifier as notifier
    except Exception as e:  # noqa: BLE001
        log(f'recover(beacon-route) import failed for {task_id}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False
    try:
        notify_prompt = notifier.build_notify_prompt(
            intent=intent, sender='mirror', task_id=task_id,
            success=True, output=body, intent_kwargs=intent_kwargs,
        )
        notify_base = {
            'task_id': notify_task_id,
            'prompt': notify_prompt,
            'source': 'mirror-result',
            'intent': intent,
            **base_extra,
        }
        notify_task = notifier.build_chain_envelope(
            notify_base, {},
            carry={
                'target_repo': notifier.DROP,
                'pr_url': notifier.DROP,
                'forge_build_session_id': notifier.DROP,
                'reply_chat_id': notifier.DROP,
                'revision_count': notifier.DROP,
                'replan_count': notifier.DROP,
                'max_replans': notifier.DROP,
            },
        )
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='beacon',
            task_dict=notify_task,
            source_agent='mirror-result',
            filename=filename,
        )
        log(f'recover(beacon-route) intent={intent} task={task_id} '
            f'routed to beacon (file={dest.name})', 'INFO')
        return True
    except (safe_write_inbox.DispatchRejected,
            safe_write_inbox.RoutingDenied) as e:
        log(f'recover(beacon-route) intent={intent} task={task_id} route '
            f'denied: {type(e).__name__}: {e}', 'WARN')
        return False
    except Exception as e:  # noqa: BLE001
        log(f'recover(beacon-route) intent={intent} task={task_id} failed: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False


def _recover_no_session_revision(task: str, pr_url: str = '') -> bool:
    """Check 6 'recovery' for a stuck cold-start obligation (forge-cold-start-revision S3).

    This is a VERIFY, not a re-dispatch: the mechanical cold-start revision
    already ran (outbox_notifier), so a blind re-dispatch here would risk a
    loop. Three outcomes:
      - PR MERGED / CLOSED → clear the ledger obligation and report recovered
        (the framework suppresses the alert).
      - PR confirmed OPEN → return False so exactly one loud, non-suppressed
        alert fires (the loop is genuinely stuck).
      - state UNVERIFIABLE (no pr_url, or gh unreachable) → return True to
        SUPPRESS this round rather than fire a possibly-false page on an
        unknown state (verify-before-alarm). The obligation stays OPEN, so the
        next tick re-checks once gh recovers.
    (Replaces the old silent Beacon re-deposit that suppressed the alert
    without anything actually resolving.)"""
    if not pr_url or pr_url == '(no pr_url)':
        return True  # nothing to verify against → defer rather than false-alert
    pr_state = _gh_pr_state(pr_url)
    if pr_state in ('MERGED', 'CLOSED'):
        try:
            no_session_ledger.resolve_obligation(
                task, resolution=pr_state.lower(),
            )
        except Exception as e:  # noqa: BLE001
            log(f'resolve_obligation failed for {task}: '
                f'{type(e).__name__}: {e}', 'WARN')
        log(f'NO_SESSION_REVISION_RESOLVED task={task} pr_state={pr_state}; '
            f'cleared ledger obligation', 'INFO')
        return True
    if pr_state is None:
        # gh could not be reached — do NOT fire a loud alert on an unverifiable
        # state. Suppress this round; the obligation stays open and re-checks.
        log(f'NO_SESSION_REVISION task={task} pr_state unverifiable (gh '
            f'unreachable); deferring alert to next tick', 'INFO')
        return True
    return False  # confirmed OPEN (or any live state) → genuinely stuck


def _recover_rebase_obligation(task: str, pr_url: str = '') -> bool:
    """Check 10 'recovery' for a stuck rebase obligation (forge-post-open-mergeable-rebase-001).

    VERIFY, not re-dispatch (the mechanical rebase dispatch already ran). Same
    three outcomes as `_recover_no_session_revision`:
      - PR MERGED / CLOSED → clear the ledger obligation, suppress the alert.
      - PR confirmed OPEN → return False so one loud, non-suppressed alert fires.
      - state UNVERIFIABLE (no pr_url / gh unreachable) → return True to suppress
        this round (verify-before-alarm); the obligation stays OPEN and re-checks.
    """
    if not pr_url or pr_url == '(no pr_url)':
        return True  # nothing to verify against → defer rather than false-alert
    pr_state = _gh_pr_state(pr_url)
    if pr_state in ('MERGED', 'CLOSED'):
        try:
            rebase_obligation_ledger.resolve_obligation(
                task, resolution=pr_state.lower(),
            )
        except Exception as e:  # noqa: BLE001
            log(f'rebase resolve_obligation failed for {task}: '
                f'{type(e).__name__}: {e}', 'WARN')
        log(f'REBASE_OBLIGATION_RESOLVED task={task} pr_state={pr_state}; '
            f'cleared ledger obligation', 'INFO')
        return True
    if pr_state is None:
        log(f'REBASE_OBLIGATION task={task} pr_state unverifiable (gh '
            f'unreachable); deferring alert to next tick', 'INFO')
        return True
    return False  # confirmed OPEN (or any live state) → genuinely stuck


def _recover_stalled_sequence(seq_id: str, seq_path: str) -> bool:
    """Recover a sequence stuck pending after an unresolved DAG-preflight
    REVISION (Check 9) by re-routing the `dag-preflight-revision` notify to
    Beacon so a fresh Beacon session amends the DAG + re-dispatches the
    preflight (M2). Mirrors the notifier's
    `_handle_mirror_dag_preflight_result` REVISION branch."""
    body = (
        f'Build sequence `{seq_id}` is still pending after an unresolved '
        f'DAG-preflight REVISION (heal-pipeline-stall backstop). Amend the '
        f'DAG in `{seq_path}` per Mirror\'s finding and re-dispatch the '
        f'preflight via marker.py (--phase routing-signal).'
    )
    return _route_revision_notify_to_beacon(
        task_id=f'review-sequence-dag-{seq_id}',
        intent='dag-preflight-revision',
        notify_task_id=f'notify-dag-revision-{seq_id}',
        filename=f'notify-dag-revision-{seq_id}.json',
        base_extra={'seq_id': seq_id, 'seq_path': str(seq_path)},
        intent_kwargs={'seq_id': seq_id, 'seq_path': str(seq_path)},
        body=body,
    )


def _recover_red_mirror_status(task_id: str, pr_url: str,
                               head_sha: Optional[str],
                               record_id: str) -> bool:
    """Check 10 'recovery' for the #653 silent-red backstop (Contract E).

    The recovery IS the promotion: surface the silent-red PR on its Contract C
    action-needed surface — the durable, self-clearing for-Larry "Waiting on
    You" record. On success the framework suppresses the alert (the record IS
    Larry's surface; a separate DM would be the forbidden double-notify). The
    fallback larry_alert fires ONLY when even that write fails. Outcomes:
      - PR MERGED / CLOSED → the red status is moot; clear any record and
        report recovered (suppress).
      - state UNVERIFIABLE (gh unreachable) → return True to defer rather than
        write/alert on an unknown state (verify-before-alarm). The next tick
        re-checks once gh recovers.
      - PR confirmed live → upsert the for-Larry record and report recovered
        iff an OPEN record exists afterward; a write failure (no open record)
        returns False so exactly one fallback alert fires.

    Idempotent (Contract D): `dedup_identity` is the SAME `{pr_url}@{head_sha}`
    step-2 routing uses, so a re-run on the same head is a no-op upsert and a
    fresh push refreshes the record in place — never a duplicate."""
    pr_state = _gh_pr_state(pr_url) if pr_url else None
    if pr_state in ('MERGED', 'CLOSED'):
        try:
            for_larry_escalations.clear(record_id)
        except Exception as e:  # noqa: BLE001
            log(f'red-mirror clear failed for {record_id}: '
                f'{type(e).__name__}: {e}', 'WARN')
        log(f'RED_MIRROR_RESOLVED task={task_id} pr_state={pr_state}; '
            f'cleared for-Larry record', 'INFO')
        return True
    if pr_url and pr_state is None:
        # gh unreachable — do NOT write or alert on an unverifiable state.
        log(f'RED_MIRROR task={task_id} pr_state unverifiable (gh '
            f'unreachable); deferring to next tick', 'INFO')
        return True
    dedup_identity = f'{pr_url or task_id}@{head_sha or ""}'
    try:
        for_larry_escalations.upsert(
            record_id,
            source='mirror-review',
            headline=f'Session-less PR needs you: {task_id}',
            context=(
                f'Mirror wants changes on {pr_url or task_id} but no session '
                f'is dispatched and nothing self-healed. Review the PR, then '
                f're-dispatch a Forge build for `{task_id}` via Beacon, or '
                f'close the PR.'
            ),
            severity='warning',
            pr_url=pr_url or None,
            head_sha=head_sha,
            dedup_identity=dedup_identity,
        )
    except Exception as e:  # noqa: BLE001
        log(f'red-mirror upsert raised for {record_id}: '
            f'{type(e).__name__}: {e}', 'WARN')
    # upsert returns None on both idempotent no-op AND write failure, so verify
    # the durable state directly: an OPEN record means Larry IS covered.
    try:
        rec = for_larry_escalations.get(record_id)
    except Exception as e:  # noqa: BLE001
        log(f'red-mirror verify-read failed for {record_id}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False
    recovered = isinstance(rec, dict) and rec.get('resolved') is not True
    if recovered:
        log(f'RED_MIRROR_ROUTED task={task_id} → for-Larry record '
            f'{record_id} (Contract C action surface)', 'INFO')
    return recovered


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


def run(dry_run: bool = False) -> int:
    """Single pass. Returns 0 always (healer never fails systemd).

    `dry_run=True` makes the pass a true no-op: every check still runs (that's
    what makes the preview useful) and the per-key cooldown is still READ, but
    every side-effecting WRITE is suppressed — no heartbeat, no recovery
    primitive (those dispatch to Mirror/Beacon inboxes and run `gh pr merge`),
    no `larry_alerts.append_alert`, no `record_alert`/`save_state` cooldown
    stamp. Each stall that WOULD fire is logged instead. Default `False`
    preserves exactly today's production behavior (the systemd timer invokes
    with no flags)."""
    if KILL_SWITCH.exists():
        log('kill switch present — exiting', 'INFO')
        return 0
    if not dry_run:
        heartbeat()
    state = load_state()
    try:
        notifier_lines = _read_recent_log_lines(OUTBOX_NOTIFIER_LOG, LOG_LOOKBACK_HOURS)
        watcher_lines = _read_recent_log_lines(INBOX_WATCHER_LOG, LOG_LOOKBACK_HOURS)
        open_prs = _all_open_prs()
        merged_prs = _all_merged_prs_recent()
        closed_prs = _all_closed_prs_recent()
        routing_events = _read_recent_routing_events(ROUTING_EVENTS_LOOKBACK_HOURS)
    except Exception as e:
        log(f'pre-flight read failed: {type(e).__name__}: {e}', 'ERROR')
        return 0

    all_alerts: list[dict] = []
    try:
        all_alerts += check_forge_built_no_pr(watcher_lines, open_prs, merged_prs, state,
                                              closed_prs=closed_prs)
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
        all_alerts += check_revision_dispatched_with_no_session(state)
    except Exception as e:
        log(
            f'check_revision_dispatched_with_no_session failed: '
            f'{type(e).__name__}: {e}', 'ERROR'
        )
    try:
        all_alerts += check_rebase_obligation_stuck(state)
    except Exception as e:
        log(
            f'check_rebase_obligation_stuck failed: '
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
    try:
        all_alerts += check_stalled_pending_sequence(state)
    except Exception as e:
        log(f'check_stalled_pending_sequence failed: {type(e).__name__}: {e}',
            'ERROR')
    try:
        all_alerts += check_red_mirror_status_no_artifact(open_prs, state)
    except Exception as e:
        log(f'check_red_mirror_status_no_artifact failed: '
            f'{type(e).__name__}: {e}', 'ERROR')

    if not all_alerts:
        log('no stalls detected', 'INFO')
        if not dry_run:
            save_state(state)
        return 0

    fired = 0
    recovered_count = 0
    would_fire = 0
    would_recover = 0
    # Per-tick cache for the merge-truth gate so a PR that would alert from two
    # checks costs at most one `gh pr view` (spec §2 batching).
    gh_state_cache: dict[str, Optional[str]] = {}
    for alert in all_alerts:
        if _alert_is_fixture(alert):
            log(f'suppressed (fixture task): {alert["key"]}', 'INFO')
            continue
        if not should_alert(state, alert['key'], alert.get('re_dm_hours')):
            log(f'suppressed (cooldown): {alert["key"]}', 'INFO')
            continue
        # Merge-truth gate (spec §2): a per-PR alert (one carrying `pr_url`) is
        # suppressed if the PR is gh-confirmed terminal (MERGED/CLOSED) at fire
        # time — the "alarmed then merged" tail. Applied after the cooldown
        # check so only alerts that would actually fire incur a gh call, and
        # before recovery so we never re-dispatch a review/merge on a terminal
        # PR. Degrades safe: an unreadable state is non-terminal = still
        # alertable (`_pr_is_terminal`).
        pr_url = alert.get('pr_url')
        if pr_url and _pr_is_terminal(pr_url, gh_state_cache):
            log(f'suppressed (pr terminal): {alert["key"]}', 'INFO')
            continue
        if dry_run:
            # No writes: log what WOULD happen and move on. A recoverable
            # stall would attempt auto-remediation FIRST (and only alert if
            # it failed); a non-recoverable one would alert directly. We
            # cannot know whether the recovery would land without running it
            # (which is exactly the side effect dry-run forbids), so report
            # the attempt, not the outcome.
            if alert.get('recovery') is not None:
                would_recover += 1
                log(f'DRY-RUN would recover-then-alert: {alert["key"]} '
                    f'(subject={alert["subject"]!r})', 'INFO')
            else:
                would_fire += 1
                log(f'DRY-RUN would alert: {alert["key"]} '
                    f'(subject={alert["subject"]!r})', 'INFO')
            continue
        # M4 recover-then-alert: a recoverable stall attempts auto-remediation
        # FIRST and alerts only if it does not land. A successful recovery
        # stamps the same per-key cooldown an alert would — so the next tick
        # does not re-attempt while the merge/review/route settles async, and
        # no Larry DM fires. A failed (or absent) recovery falls through to the
        # single alert below — exactly one per key per window, never a double.
        recovery = alert.get('recovery')
        if recovery is not None:
            try:
                recovered = bool(recovery())
            except Exception as e:  # noqa: BLE001 — recovery must never crash the healer
                log(f'recovery raised for {alert["key"]}: '
                    f'{type(e).__name__}: {e}', 'WARN')
                recovered = False
            if recovered:
                record_alert(state, alert['key'])
                recovered_count += 1
                log(f'recovered (alert suppressed): {alert["key"]}', 'INFO')
                continue
            log(f'recovery failed for {alert["key"]}; falling through to alert',
                'INFO')
        message = alert['message']
        if recovery is not None:
            message += (
                ' [heal-pipeline-stall attempted auto-recovery and it did not '
                'land — manual intervention needed.]'
            )
        ok = larry_alerts.append_alert(
            source='heal-pipeline-stall',
            severity='warning',
            message=message,
            subject=alert['subject'],
            suggested_action=alert['suggested_action'],
        )
        if ok:
            record_alert(state, alert['key'])
            fired += 1
            log(f'alerted: {alert["key"]}', 'INFO')
        else:
            log(f'larry_alerts append failed for {alert["key"]}', 'WARN')
    if dry_run:
        log(f'DRY-RUN: {would_fire + would_recover} alert(s) would fire, '
            f'{would_recover} recovery(ies) would be attempted; '
            f'no writes performed', 'INFO')
        return 0
    log(f'done: {fired} new alert(s) fired, {recovered_count} recovered, '
        f'{len(all_alerts) - fired - recovered_count} suppressed', 'INFO')
    save_state(state)
    return 0


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Proactively DM Larry when work stops flowing in the '
                    'pipeline. Runs every 15 min via systemd timer.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Detect and log what WOULD be alerted/recovered, but perform '
             'zero side-effecting writes (no alerts, no recovery dispatch, '
             'no state/heartbeat mutation).',
    )
    return parser.parse_args(argv)


if __name__ == '__main__':
    args = _parse_args()
    sys.exit(run(dry_run=args.dry_run))
