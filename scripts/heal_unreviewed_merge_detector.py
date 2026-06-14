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
  2. For each merged PR, verify a review happened. Two positive signals are
     accepted:
       a) Mirror REVIEW_PASS — local + dependency-free: the outbox-notifier only
          fires its `AUTO_MERGE task=<id> pr=<url> ...` log line AFTER classifying
          a Mirror REVIEW_PASS marker (auto-merge is gated on the pass). So ANY
          AUTO_MERGE* line referencing a PR is proof a pass was classified for
          it. (The brief's alternative — a chain_events `review_pass` row with
          that pr_url — lives in Supabase; the log scan is preferred here because
          it's local, needs no network, and is trivially testable.)
       b) Local /code-review (desktop) — a PR merged from desktop Claude Code
          after a local `/code-review` never enters Mirror's droplet cycle, so it
          has no AUTO_MERGE evidence and would page uselessly. The desktop merge
          flow (scripts/merge_reviewed_pr.sh) instead posts a marker comment just
          before merging. The marker is accepted as evidence only under three
          simultaneous gates — trusted comment author
          (config/local-review-reviewers.json), an anchored marker LINE (not a
          loose substring), and an embedded head SHA equal to the PR's merged
          headRefOid — so a quoted/stale/replayed marker can't suppress a real
          page. This per-PR check runs ONLY for the merges that fail signal (a),
          so the extra gh fetch never touches the common reviewed merge.
  3. A PR merged with NEITHER signal -> emit a Larry ESCALATION
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
  - Read-only on GitHub (lists PRs + views a candidate's comments; never
    merges, closes, or comments).

The alert is Python-emitted, so it has a registered entry in
config/alert-translations.json under source `heal-unreviewed-merge-detector`
(subject prefix `unreviewed-merge`), enforced by the translation-coverage CI
gate (scripts/tests/test_alert_translations.py).
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
# Per-PAGE row cap. gh returns merged PRs NEWEST-first capped at this many; a
# busy window with more than this truncates the OLDER in-window merges off the
# tail. fetch_merged_prs handles that by RANGE-PAGING within the tick (fetch
# newest page in [cursor, now]; if full, page into the older sub-window via
# `merged:<lower>..<oldest-scanned>` and repeat until a short page) so the tail
# is fully scanned — the cursor only advances after every in-window merge has
# been seen. Sized to match heal_pipeline_stall._MERGED_PR_FETCH_LIMIT.
_MERGED_PR_FETCH_LIMIT = 300
# Hard cap on range-paging iterations per tick, so a pathological merge burst (or
# a bug) can't spin gh forever. 20 pages * 300 = 6000 merges/tick of headroom;
# hitting it logs a loud WARN (an older tail may remain unscanned that tick — the
# only residual gap, and far beyond any realistic window).
_MAX_FETCH_PAGES = 20
# Keep the alerted-PR ledger from growing without bound. Recent entries are all
# that matter (the cursor only re-presents PRs near the boundary).
MAX_ALERTED_LEDGER = 200

# Metadata-only exemption (the unreviewed-merge alert's whole point is to catch
# CODE that landed without Mirror review; benign dashboard promote-capture PRs
# only ever rewrite these two beacon state files and never enter the
# Forge->Mirror chain, so they have no AUTO_MERGE evidence and would alert
# uselessly). A PR is exempt IFF its title starts with one of these prefixes AND
# EVERY changed file is in the allowlist below — the file-scope guard is
# non-negotiable: a title-only exemption would be a literal review-evasion
# bypass (any code PR named `chore(missions): ...` would be ignored). An empty
# or missing files list is NOT exempt (indeterminate => fail toward alerting).
EXEMPT_TITLE_PREFIXES = ('chore(missions):',)
EXEMPT_FILE_ALLOWLIST = frozenset({
    'agents/beacon/missions.json',
    'agents/beacon/captures.json',
})

# Local-review (desktop) exemption. A PR merged from desktop Claude Code after a
# local `/code-review` never goes through Mirror's droplet cycle, so it has no
# AUTO_MERGE evidence and would page Larry as unreviewed even though it WAS
# reviewed. The desktop merge flow (scripts/merge_reviewed_pr.sh) posts a marker
# comment just before merging; this detector accepts that as valid review
# evidence, but only under THREE simultaneous gates, each closing a forgery hole:
#
#   1. Author gate — the comment author must be in the trusted-reviewer set
#      (config/local-review-reviewers.json). Keeps the control intact for any
#      other identity; bots merge through Mirror (AUTO_MERGE evidence) and never
#      need this path. (Note: a GitHub *review approval* would be the natural
#      signal but GitHub forbids approving your own PR, and desktop PRs are
#      self-authored — hence a comment marker.)
#   2. Anchored marker — the body must contain a whole line matching
#      LOCAL_REVIEW_MARKER_RE exactly, not a bare substring. A trusted reviewer
#      quoting the runbook / pasting this alert / discussing the token in prose
#      must NOT suppress the page.
#   3. SHA binding — the marker embeds the reviewed head SHA, which must equal the
#      PR's actual merged headRefOid. This ties the evidence to the exact commit
#      reviewed: a stale marker (review, then more commits, then merge) or a
#      replayed marker no longer matches and still pages. An issue comment has no
#      native commit association, so we carry the SHA ourselves.
#
# Reviewers load from config/local-review-reviewers.json. A missing/malformed
# file falls back to _DEFAULT_LOCAL_REVIEW_REVIEWERS; an explicit empty list is
# honored as "exemption disabled" (so operators can close the hole on disk).
LOCAL_REVIEW_REVIEWERS_FILE = (
    Path(__file__).resolve().parent.parent / 'config' / 'local-review-reviewers.json'
)
_DEFAULT_LOCAL_REVIEW_REVIEWERS = frozenset({'Larry-Yatch'})
# A whole marker line, e.g. `=== LOCAL_REVIEW_PASS sha=<40-hex> ===`. Anchored to
# the full (stripped) line and carrying the reviewed head SHA — see gate 2 & 3
# above. Kept in sync with the body scripts/merge_reviewed_pr.sh emits.
LOCAL_REVIEW_MARKER_RE = re.compile(
    r'^=== LOCAL_REVIEW_PASS sha=([0-9a-f]{7,64}) ===$')

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


def _fmt_gh_ts(dt: datetime) -> str:
    """Format an aware datetime as the date-qualifier gh search expects, e.g.
    2026-06-04T18:05:03+00:00. The SOLE place this format is produced — both the
    lower bound (cursor) and the range-paging upper bound must use it verbatim so
    the two ends of a `merged:A..B` window are directly comparable."""
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')


def _gh_search_cursor(cursor_iso: Optional[str], now: datetime) -> str:
    """The lower-bound `merged:>=<ts>` value for the gh search. Floors the cursor
    to at most MAX_LOOKBACK_DAYS ago so a stale cursor can't request a huge
    window. GitHub's search accepts an ISO-8601 datetime with timezone offset."""
    floor = now - timedelta(days=MAX_LOOKBACK_DAYS)
    cur = _parse_iso(cursor_iso) if cursor_iso else None
    eff = max(cur, floor) if cur else floor
    return _fmt_gh_ts(eff)


# -------------------- gh + log I/O (mockable seams) --------------------

def _run_gh_pr_list(search: str) -> Optional[tuple[list[dict[str, Any]], int]]:
    """Issue ONE `gh pr list` for the given `--search` value. Returns
    (parsed_rows, raw_row_count) where raw_row_count is gh's pre-parse row count
    (the truncation signal — it may exceed len(parsed_rows) if some rows were
    malformed). Returns None on ANY gh failure (already logged) — a detector must
    never crash the healer."""
    cmd = [
        'gh', 'pr', 'list',
        '--repo', REPO,
        '--state', 'merged',
        '--base', BASE_BRANCH,
        '--search', search,
        '--limit', str(_MERGED_PR_FETCH_LIMIT),
        '--json', 'number,mergedAt,url,author,title,files',
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh pr list failed: {type(e).__name__}: {e}', 'WARN')
        return None
    if proc.returncode != 0:
        log(f'gh pr list returned {proc.returncode}: '
            f'{proc.stderr.strip()[:300]}', 'WARN')
        return None
    rows = parse_merged_prs(proc.stdout)
    try:
        raw_count = len(json.loads(proc.stdout or '[]'))
    except (json.JSONDecodeError, TypeError):
        raw_count = len(rows)
    return rows, raw_count


def _oldest_merged_dt(rows: list[dict[str, Any]]) -> Optional[datetime]:
    """The oldest parseable mergedAt across the rows (the range-paging pivot)."""
    oldest: Optional[datetime] = None
    for r in rows:
        dt = _parse_iso(r.get('mergedAt'))
        if dt is None:
            continue
        if oldest is None or dt < oldest:
            oldest = dt
    return oldest


def fetch_merged_prs(cursor_iso: Optional[str], now: datetime) -> list[dict[str, Any]]:
    """Return parsed `gh pr list` rows for ALL PRs merged into BASE since the
    cursor, RANGE-PAGED so a busy window can't drop its older tail.

    gh returns merged PRs newest-first capped at _MERGED_PR_FETCH_LIMIT, so a
    single fetch silently truncates the oldest in-window merges — and since the
    cursor advances to the newest mergedAt, those dropped merges are never
    re-scanned (the exact gap this detector exists to close). To avoid that we
    page: fetch the newest page in [cursor, now]; while a page comes back FULL
    (truncated), re-query the OLDER sub-window `merged:<lower>..<oldest-scanned>`
    and merge the results, until a page returns fewer than the limit. The
    inclusive upper bound re-presents the boundary PR(s), so we dedup by number.

    Fail-safe: NEVER skip a merge. A page cap and a strict-decrease livelock
    guard bound the loop; if either trips we WARN loudly (an older tail may
    remain unscanned that tick) rather than spin. Any gh failure mid-paging
    returns what was gathered so far. Each row: {number, mergedAt, url,
    author_login, title}."""
    lower = _gh_search_cursor(cursor_iso, now)
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    upper_str: Optional[str] = None  # None => open-ended newest-first first page
    page = 0
    while True:
        page += 1
        if page > _MAX_FETCH_PAGES:
            log(f'fetch hit page cap {_MAX_FETCH_PAGES} '
                f'(~{_MAX_FETCH_PAGES * _MERGED_PR_FETCH_LIMIT} merges) for window '
                f'since {cursor_iso}; an older in-window tail may remain UNSCANNED '
                f'this tick — investigate the merge burst', 'WARN')
            break
        search = (f'merged:>={lower}' if upper_str is None
                  else f'merged:{lower}..{upper_str}')
        result = _run_gh_pr_list(search)
        if result is None:
            break  # gh failure already logged; return what we have (fail-safe).
        rows, raw_count = result
        for r in rows:
            n = r.get('number')
            if n in seen:
                continue  # boundary PR re-presented by the inclusive `..` upper.
            seen.add(n)
            out.append(r)
        if raw_count < _MERGED_PR_FETCH_LIMIT:
            break  # short page => the whole window is drained, no truncation.
        # Page was full: an older in-window tail remains. Pivot to the older
        # sub-window bounded above by the oldest mergedAt we just scanned. gh
        # emits mergedAt at SECOND precision and GitHub's `merged:..U` qualifier
        # compares at the same granularity, so the inclusive `..<oldest-second>`
        # re-presents EVERY PR sharing that boundary second (deduped by number
        # below) — none is skipped. The sole residual is >_MERGED_PR_FETCH_LIMIT
        # merges inside one second, which the livelock guard catches + WARNs.
        oldest = _oldest_merged_dt(rows)
        if oldest is None:
            log(f'fetch truncated but no parseable mergedAt to page older '
                f'(since {cursor_iso}); an older tail may remain unscanned', 'WARN')
            break
        next_upper_str = _fmt_gh_ts(oldest)
        # Livelock guard: the upper bound MUST strictly decrease, else the same
        # window re-fetches forever (e.g. >_MERGED_PR_FETCH_LIMIT merges sharing
        # one second so the pivot can't move below them). Compare the formatted
        # query strings, since that is what actually gets issued.
        if upper_str is not None and next_upper_str >= upper_str:
            log(f'fetch range-paging stalled at {next_upper_str}: upper bound did '
                f'not decrease below {upper_str} (>= {_MERGED_PR_FETCH_LIMIT} '
                f'merges at one timestamp?); an older tail may remain unscanned '
                f'— investigate', 'WARN')
            break
        upper_str = next_upper_str
    if page > 1:
        log(f'range-paged {page} window(s); {len(out)} merged PR(s) since '
            f'{cursor_iso}')
    return out


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
        # gh returns `files` as a list of objects, each carrying a `path`.
        # Normalize to a flat list of path strings; default [] when absent or
        # malformed so the exemption check fails toward alerting.
        raw_files = r.get('files')
        files: list[str] = []
        if isinstance(raw_files, list):
            for f in raw_files:
                if isinstance(f, dict):
                    p = f.get('path')
                    if isinstance(p, str) and p:
                        files.append(p)
        out.append({
            'number': number,
            'mergedAt': merged_at,
            'url': url,
            'author_login': login or 'unknown',
            'title': r.get('title') or '',
            'files': files,
        })
    return out


def read_notifier_log() -> str:
    """Read the outbox-notifier log. Returns '' if absent/unreadable."""
    try:
        return OUTBOX_NOTIFIER_LOG.read_text(errors='replace')
    except (FileNotFoundError, OSError):
        return ''


# -------------------- review-pass evidence (pure) --------------------

# Match ONLY the standalone `pr=<url>` field — i.e. `pr=` at the start of a
# token (preceded by start-of-line or whitespace), with an optional opening
# quote from `{pr_url!r}` repr. The whitespace anchor is load-bearing: the
# marker/envelope pr-url-MISMATCH refusal line (nervous-system-audit #15)
# carries `marker_pr='<wrong-url>' envelope_pr='<real-url>'`, and a naive
# `find('pr=')` matched the `pr=` *inside* `marker_pr=`, recording a
# REVIEW-REFUSED wrong-PR url as "passed". That both (a) mislabels a PR the
# gate explicitly refused to merge and (b) could suppress a future
# unreviewed-merge alert for that url. Requiring a boundary before `pr=`
# excludes `marker_pr=`/`envelope_pr=` (preceded by `_`) entirely, so a
# refusal line contributes nothing — the fail-loud direction.
_PR_FIELD_RE = re.compile(r'(?:^|\s)pr=([\'"]?)(\S+)')


def review_passed_pr_urls(notifier_log_text: str) -> set[str]:
    """Extract the set of PR URLs that have Mirror REVIEW_PASS evidence.

    The outbox-notifier only logs an `AUTO_MERGE...` line carrying a standalone
    `pr=<url>` field after it has classified a Mirror REVIEW_PASS marker for that
    PR (auto-merge is gated on the pass). So every PR URL that appears as the
    `pr=` field of an AUTO_MERGE line — regardless of the merge OUTCOME (merged /
    already_merged / failed / held / deferred / skipped) — had a pass classified.
    The merge mechanics may have succeeded, failed, or been deferred to the
    healer; what matters here is only that a pass happened.

    Note: the `marker_pr=`/`envelope_pr=` fields on a pr-url-mismatch *refusal*
    line are deliberately NOT treated as evidence — that line means the merge was
    refused, and neither url passed review through that path. `_PR_FIELD_RE`'s
    boundary anchor enforces this.

    Pure function over the log text so tests inject fixtures."""
    passed: set[str] = set()
    for line in notifier_log_text.splitlines():
        if 'AUTO_MERGE' not in line:
            continue
        m = _PR_FIELD_RE.search(line)
        if not m:
            continue
        quote, token = m.group(1), m.group(2)
        # Trim a trailing closing quote / punctuation from the repr form.
        if quote:
            end = token.find(quote)
            if end >= 0:
                token = token[:end]
        url = token.rstrip('\'",')
        if url.startswith('http'):
            passed.add(url.rstrip('/'))
    return passed


# -------------------- metadata exemption (pure) --------------------

def is_metadata_exempt(pr: dict[str, Any]) -> bool:
    """True iff this merged PR is a benign metadata-only change that should be
    skipped by the unreviewed-merge alert.

    Exempt IFF BOTH hold:
      (a) the PR title starts with one of EXEMPT_TITLE_PREFIXES, AND
      (b) the PR's changed-file list is non-empty and EVERY file is in
          EXEMPT_FILE_ALLOWLIST.

    The file-scope guard (b) is what makes this safe: a title-prefix-only
    exemption would let any code PR named `chore(missions): ...` evade the
    security detector. An empty/missing files list is treated as NOT exempt —
    an indeterminate file set fails toward alerting."""
    title = pr.get('title') or ''
    if not title.startswith(EXEMPT_TITLE_PREFIXES):
        return False
    files = pr.get('files')
    if not isinstance(files, list) or not files:
        return False
    return all(f in EXEMPT_FILE_ALLOWLIST for f in files)


# -------------------- local-review (desktop) evidence --------------------

def load_local_review_reviewers() -> frozenset[str]:
    """GitHub logins whose LOCAL_REVIEW_PASS marker comment is accepted as valid
    review evidence (the desktop `/code-review` merge path). Read from
    config/local-review-reviewers.json — {"reviewers": ["login", ...]}.

    An explicit empty list (`"reviewers": []`) is honored verbatim — it DISABLES
    the exemption, the on-disk way to close the hole. Only a missing/unreadable/
    malformed file (or a missing/non-list `reviewers` key) falls back to
    _DEFAULT_LOCAL_REVIEW_REVIEWERS, so a broken file fails toward the known
    reviewer set rather than silently flipping behavior."""
    try:
        data = json.loads(LOCAL_REVIEW_REVIEWERS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError) as e:
        log(f'load_local_review_reviewers fell back to default: '
            f'{type(e).__name__}: {e}', 'WARN')
        return _DEFAULT_LOCAL_REVIEW_REVIEWERS
    raw = data.get('reviewers') if isinstance(data, dict) else None
    if not isinstance(raw, list):
        log('load_local_review_reviewers: no list `reviewers` key; using default',
            'WARN')
        return _DEFAULT_LOCAL_REVIEW_REVIEWERS
    return frozenset(n for n in raw if isinstance(n, str) and n)


def fetch_pr_review_artifacts(number: int) -> Optional[dict[str, Any]]:
    """Fetch the per-PR data needed to verify a desktop local-review marker:
    the merged head SHA and the conversation comments. Returns
    {'headRefOid': str, 'comments': [{'author_login', 'body'}, ...]} or None on
    ANY gh failure (logged) — None makes has_local_review_pass fail toward
    alerting. Called ONLY for the small candidate set (merges that already lack
    Mirror evidence), so the comment payload is never fetched for the common
    already-reviewed merge."""
    cmd = ['gh', 'pr', 'view', str(number), '--repo', REPO,
           '--json', 'headRefOid,comments']
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=GH_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh pr view #{number} failed: {type(e).__name__}: {e}', 'WARN')
        return None
    if proc.returncode != 0:
        log(f'gh pr view #{number} returned {proc.returncode}: '
            f'{proc.stderr.strip()[:200]}', 'WARN')
        return None
    try:
        data = json.loads(proc.stdout or '{}')
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    comments: list[dict[str, str]] = []
    raw_comments = data.get('comments')
    if isinstance(raw_comments, list):
        for c in raw_comments:
            if not isinstance(c, dict):
                continue
            body = c.get('body')
            if not isinstance(body, str):
                continue
            cauthor = c.get('author') or {}
            clogin = cauthor.get('login') if isinstance(cauthor, dict) else None
            comments.append({'author_login': clogin or 'unknown', 'body': body})
    return {'headRefOid': data.get('headRefOid'), 'comments': comments}


def has_local_review_pass(artifacts: Optional[dict[str, Any]],
                          reviewers: frozenset[str]) -> bool:
    """True iff `artifacts` (from fetch_pr_review_artifacts) shows a valid desktop
    local-review marker. All three gates must hold (see the LOCAL_REVIEW_MARKER_RE
    constant block): the comment author is trusted, the body carries the anchored
    marker line, and the marker's SHA equals the PR's merged head SHA. Pure over
    the artifacts shape so tests inject fixtures; None/missing data => False
    (fail toward alerting)."""
    if not reviewers or not isinstance(artifacts, dict):
        return False
    head = artifacts.get('headRefOid')
    if not isinstance(head, str) or not head:
        return False
    comments = artifacts.get('comments')
    if not isinstance(comments, list):
        return False
    for c in comments:
        if not isinstance(c, dict) or c.get('author_login') not in reviewers:
            continue
        body = c.get('body')
        if not isinstance(body, str):
            continue
        for line in body.splitlines():
            m = LOCAL_REVIEW_MARKER_RE.match(line.strip())
            if m and head.startswith(m.group(1)):
                return True
    return False


# -------------------- core detection (pure) --------------------

def run_once(
    merged_prs: list[dict[str, Any]],
    passed_urls: set[str],
    alerted_prs: dict[str, Any],
) -> list[dict[str, Any]]:
    """Pure detection step. Given the merged-PR rows, the set of PR URLs with
    Mirror REVIEW_PASS evidence, and the already-alerted ledger, return the list
    of alert dicts to emit (one per unreviewed, not-yet-alerted merge).

    Covers the cheap, list-data-only filters: metadata-only PRs are skipped,
    Mirror-passed PRs are skipped, already-alerted PRs are skipped. The
    desktop local-review marker is a SEPARATE, per-candidate check applied by
    main() after this step (it needs an extra gh fetch, so it runs only for the
    survivors here rather than for every merged PR).

    Each alert dict: {pr_url, number, subject, message, suggested_action}.
    """
    alerts: list[dict[str, Any]] = []
    for pr in merged_prs:
        url = str(pr['url']).rstrip('/')
        if is_metadata_exempt(pr):
            # Benign metadata-only promote-capture PR (title-prefix + all files
            # in the allowlist). It never enters the Mirror chain, so it has no
            # AUTO_MERGE evidence by design — skip rather than alert uselessly.
            prefix = next(p for p in EXEMPT_TITLE_PREFIXES
                          if (pr.get('title') or '').startswith(p))
            log(f'exempt metadata-only PR #{pr["number"]} ({prefix}); '
                f'skipping unreviewed-merge check')
            continue
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

    # Per-candidate desktop local-review check. Only the would-be alerts (merges
    # with no Mirror evidence) are probed, so the extra gh fetch never runs for
    # the common already-reviewed merge. A trusted, anchored, SHA-bound
    # LOCAL_REVIEW_PASS marker means the PR was reviewed via desktop /code-review
    # — drop it rather than page.
    reviewers = load_local_review_reviewers()
    if alerts and reviewers:
        kept: list[dict[str, Any]] = []
        for alert in alerts:
            artifacts = fetch_pr_review_artifacts(alert['number'])
            if has_local_review_pass(artifacts, reviewers):
                log(f'PR #{alert["number"]} has a trusted, SHA-bound '
                    f'LOCAL_REVIEW_PASS marker; treating as reviewed '
                    f'(desktop /code-review)')
                continue
            kept.append(alert)
        alerts = kept

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
    # too — they're verified-good and shouldn't be re-scanned). fetch_merged_prs
    # range-pages the entire [cursor, now] window, so a busy tick has already
    # scanned the older tail rather than dropping it — advancing to the newest
    # mergedAt no longer skips an unreviewed merge. (The only residual is an
    # extreme burst that trips the page-cap/livelock guard, which WARNs loudly.)
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
