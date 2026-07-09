#!/usr/bin/env python3
"""
heal_review_ceiling_fit.py — the durable feedback loop for the Mirror
review-session wall-clock ceiling (PR #734).

PR #734 added a hard, harness-enforced ceiling on every Mirror review session
(`agent_runner.REVIEW_SESSION_CEILING_SECONDS`, default 2100s/35min, env
`OL_REVIEW_SESSION_CEILING_SECONDS`, `<=0` disables). A review that exceeds it is
killed and the outbox-notifier synthesizes a `review_escalate` whose reason
contains `review_session_timeout`. The 35-min value was validated ONCE against
147 historical reviews (p99 ≈ 30.5 min). This monitor is the standing answer to
"is that value still right?" — it runs weekly, recomputes the duration
distribution, counts ceiling firings + likely false-kills, and emits ONE
low-severity digest carrying a recommended ceiling.

OBSERVE + REPORT ONLY. This build does NOT auto-tune anything:
  - It NEVER mutates the ceiling, the config the ceiling reads, costs.jsonl, or
    chain_events. It is strictly read-only on every input.
  - It NEVER pages Larry: the digest is emitted via larry_alerts with
    `route='digest'` (no DM; surfaced in the CEO digest), NEVER `escalate`.
  - It is a REPORTER, not a recoverer — the `heal_*` name follows the family
    convention (sibling to heal_wedged_review_sessions / heal_unreviewed_merge_
    detector), but it has no remediation path. Auto-tuning is a deliberate
    follow-up once the recommendation is trusted.

Inputs (all read-only):
  1. Review durations — ~/agents/blackboard/costs.jsonl (+ archive/costs*.jsonl).
     Rows with agent == 'mirror' AND task_id starting with 'pr-' or 'review'
     (the PR reviews the ceiling bounds). Field: duration_sec.
  2. Ceiling firings — the outbox-notifier log line
     `REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED task=<id> pr_url=<url> ...`. This is
     the local, no-network, testable signal for a harness-killed review (the
     chain_events `review_escalate` row with a review_session_timeout reason is
     the Supabase source of truth, but the log line is preferred here for the
     same reasons heal_unreviewed_merge_detector prefers its local AUTO_MERGE
     log scan over a chain_events query).
  3. GitHub truth for false-kills — `gh pr view <pr_url> --json state,mergedAt`
     for each fired PR. A fired-then-MERGED PR is a likely false-kill (the review
     was killed but the PR was fine). A PR whose state can't be read degrades to
     'unknown' (NEVER counted as a false-kill).

Computation (over a configurable window, default last 30 days):
  - Duration distribution: count, p50, p90, p95, p99, max.
  - Headroom: ceiling - p95, ceiling - p99. HEADROOM_LOW when
    p95 >= ceiling * headroom_low_ratio (default 0.8).
  - Firing rate: count of review_session_timeout firings in the window, and as
    a fraction of total reviews.
  - False-kills: fired PRs that later reached state == MERGED. FALSE_KILL when
    >= 1.
  - Recommended ceiling: max(round_up_to_5min(p99 * recommend_multiplier),
    current_ceiling). If HEADROOM_LOW or FALSE_KILL, recommend raising to it;
    otherwise "no change — ceiling has N min headroom over p99."

Safe on small/empty samples: every percentile is 0.0 on an empty window (no
div-by-zero), HEADROOM_LOW cannot fire when there are no samples, and the
recommendation degrades to "no change". An empty window still emits a terse
"ceiling OK" digest so the absence of a problem is observable.

Two-slot audit (mirror-two-slot-review spec §4 PR2): slot-safe as-is. Each
review emits its OWN costs.jsonl row with its OWN duration_sec, so N concurrent
review slots simply produce N rows with overlapping timestamps — the duration
distribution samples per-review wall-clock, never a gap between consecutive
rows, so concurrency cannot distort it. is_mirror_review_row filters on
agent/task_id, NOT on any single-lease/slot assumption; the ceiling is
correctly sized against the full population of both slots' reviews.

Config: config/review-ceiling-fit-rules.json (window_days, headroom_low_ratio,
recommend_multiplier, enabled). A missing/malformed file falls back to the
DEFAULTS baked in below.

The digest is Python-emitted via larry_alerts, so it has a registered entry in
config/alert-translations.json under source `review-ceiling-fit`, enforced by
the translation-coverage CI gate (scripts/tests/test_alert_translations.py).
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-review-ceiling-fit.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-review-ceiling-fit.heartbeat'
COSTS_FILE = AGENTS_ROOT / 'blackboard' / 'costs.jsonl'
COSTS_ARCHIVE_GLOB = str(AGENTS_ROOT / 'blackboard' / 'archive' / 'costs*.jsonl')
OUTBOX_NOTIFIER_LOG = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'
RULES_FILE = Path(__file__).resolve().parent.parent / 'config' / 'review-ceiling-fit-rules.json'

REPO = 'Larry-Yatch/ourliberty-agent-core'
GH_TIMEOUT_S = 30

ALERT_SOURCE = 'review-ceiling-fit'
# Single static subject — this is ONE weekly digest, not a per-entity alert, so
# there is no rotating suffix. Kept in exact sync with the entry in
# config/alert-translations.json under `review-ceiling-fit`.
ALERT_SUBJECT = 'review-ceiling-fit'

# Defaults baked in so a missing/malformed config degrades gracefully. Mirror
# agent_runner.REVIEW_SESSION_CEILING_SECONDS' own default (2100s) for the
# ceiling fallback so the two agree when the env var is unset.
DEFAULT_WINDOW_DAYS = 30
DEFAULT_HEADROOM_LOW_RATIO = 0.8
DEFAULT_RECOMMEND_MULTIPLIER = 1.25
DEFAULT_CEILING_SECONDS = 2100

# Mirror PR-review task_ids start with one of these (the reviews the ceiling
# bounds). A leading 'pr-' (e.g. pr-734-review) or 'review' (e.g. review-foo).
REVIEW_TASK_PREFIXES = ('pr-', 'review')

# The outbox-notifier line that records a harness-killed review (see
# outbox_notifier._maybe_synthesize_timeout_escalate). Shape:
#   [YYYY-MM-DD HH:MM:SS] [notifier] [INFO] REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED
#     task='<id>' pr_url='<url>' timeout_seconds=<n> — ...
# The leading stamp is NAIVE HOST-LOCAL time (the droplet runs America/Denver),
# matching outbox_notifier.log()'s format — see that function's comment. We
# window firings on this naive-local stamp; costs.jsonl rows are UTC. For a
# multi-day window the sub-day tz skew at the boundary is immaterial to a weekly
# digest, and it is documented rather than papered over.
_FIRING_LINE_RE = re.compile(
    r'^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*'
    r'REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED\s+'
    r'task=(?P<task>\S+)\s+pr_url=(?P<pr>\S+)'
)
# Fallback for a firing line whose leading stamp can't be parsed (format drift /
# truncation): still capture the firing so a kill is never silently dropped.
_FIRING_BARE_RE = re.compile(
    r'REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED\s+task=(?P<task>\S+)\s+pr_url=(?P<pr>\S+)'
)


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


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


# -------------------- config + ceiling --------------------

def load_rules() -> dict[str, Any]:
    """Read config/review-ceiling-fit-rules.json, falling back to DEFAULTS for
    any missing/invalid field. A missing or malformed file returns all defaults
    (never raises)."""
    rules = {
        'enabled': True,
        'window_days': DEFAULT_WINDOW_DAYS,
        'headroom_low_ratio': DEFAULT_HEADROOM_LOW_RATIO,
        'recommend_multiplier': DEFAULT_RECOMMEND_MULTIPLIER,
    }
    try:
        data = json.loads(RULES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f'rules file unreadable ({type(e).__name__}); using defaults', 'WARN')
        return rules
    if not isinstance(data, dict):
        return rules
    if isinstance(data.get('enabled'), bool):
        rules['enabled'] = data['enabled']
    wd = data.get('window_days')
    if isinstance(wd, (int, float)) and wd > 0:
        rules['window_days'] = int(wd)
    hr = data.get('headroom_low_ratio')
    if isinstance(hr, (int, float)) and hr > 0:
        rules['headroom_low_ratio'] = float(hr)
    rm = data.get('recommend_multiplier')
    if isinstance(rm, (int, float)) and rm > 0:
        rules['recommend_multiplier'] = float(rm)
    return rules


def get_current_ceiling() -> int:
    """The ceiling the harness enforces, in seconds. Reads the same knob
    agent_runner reads (env OL_REVIEW_SESSION_CEILING_SECONDS), falling back to
    agent_runner's baked-in default (2100). A value <= 0 means the ceiling is
    DISABLED (agent_runner's documented semantics) and is returned verbatim so
    the caller can report "no headroom analysis". READ-ONLY — never writes."""
    raw = os.environ.get('OL_REVIEW_SESSION_CEILING_SECONDS')
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            log(f'OL_REVIEW_SESSION_CEILING_SECONDS={raw!r} not an int; '
                f'using default {DEFAULT_CEILING_SECONDS}', 'WARN')
    return DEFAULT_CEILING_SECONDS


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


# -------------------- costs.jsonl (durations) --------------------

def cost_source_paths() -> list[Path]:
    """The live costs.jsonl plus any archived costs*.jsonl, newest live file
    first. Missing files are simply absent from the list."""
    paths: list[Path] = []
    if COSTS_FILE.exists():
        paths.append(COSTS_FILE)
    for p in sorted(glob.glob(COSTS_ARCHIVE_GLOB)):
        paths.append(Path(p))
    return paths


def iter_cost_rows(paths: list[Path]):
    """Yield parsed JSON objects from the given cost files, skipping blank and
    malformed lines. Never raises on a bad file/line."""
    for path in paths:
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        yield obj
        except OSError as e:
            log(f'cost file {path} unreadable: {type(e).__name__}: {e}', 'WARN')


def is_mirror_review_row(row: dict[str, Any]) -> bool:
    """True iff this cost row is a Mirror PR-review with a usable duration —
    agent == 'mirror', task_id starts with a REVIEW_TASK_PREFIX, and
    duration_sec is a positive number."""
    if row.get('agent') != 'mirror':
        return False
    task_id = row.get('task_id')
    if not isinstance(task_id, str) or not task_id.startswith(REVIEW_TASK_PREFIXES):
        return False
    dur = row.get('duration_sec')
    return isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0


def collect_review_durations(
    rows, window_start: datetime, now: datetime,
) -> list[float]:
    """Return duration_sec for every Mirror PR-review row whose ts is within
    [window_start, now]. A row with no/unparseable ts is skipped (can't be
    windowed). Pure over an iterable of rows so tests inject fixtures."""
    out: list[float] = []
    for row in rows:
        if not is_mirror_review_row(row):
            continue
        ts = _parse_iso(row.get('ts', ''))
        if ts is None or not (window_start <= ts <= now):
            continue
        out.append(float(row['duration_sec']))
    return out


# -------------------- percentile + distribution (pure) --------------------

def compute_percentile(values: list[float], percentile: float) -> float:
    """Closed-form percentile with linear interpolation. Empty -> 0.0, so it is
    safe on small/empty samples (no div-by-zero). Same algorithm as
    pulse_check_iii.compute_percentile."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * percentile
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(sorted_values[int(k)])
    return (sorted_values[lo] * (hi - k) + sorted_values[hi] * (k - lo))


def duration_distribution(durations: list[float]) -> dict[str, float]:
    """count/p50/p90/p95/p99/max over the durations. Every field is 0 on an
    empty list — terse-but-valid, never a crash."""
    return {
        'count': len(durations),
        'p50': compute_percentile(durations, 0.50),
        'p90': compute_percentile(durations, 0.90),
        'p95': compute_percentile(durations, 0.95),
        'p99': compute_percentile(durations, 0.99),
        'max': float(max(durations)) if durations else 0.0,
    }


# -------------------- ceiling firings (notifier log) --------------------

def read_notifier_log() -> str:
    """Read the outbox-notifier log. Returns '' if absent/unreadable."""
    try:
        return OUTBOX_NOTIFIER_LOG.read_text(errors='replace')
    except (FileNotFoundError, OSError):
        return ''


def _unrepr(token: str) -> Optional[str]:
    """Turn a repr'd log field (`'foo'`, `"foo"`, or `None`) into its value.
    Trailing punctuation from the line is trimmed. `None` -> None."""
    token = token.strip().rstrip(',')
    if token == 'None' or not token:
        return None
    if len(token) >= 2 and token[0] in '\'"' and token[-1] == token[0]:
        return token[1:-1]
    return token.strip('\'"')


def parse_timeout_firings(
    log_text: str, window_start: datetime, now: datetime,
) -> list[dict[str, Optional[str]]]:
    """Extract `review_session_timeout` firings from the notifier log, windowed
    on each line's leading naive-local stamp, deduped by task_id (one firing per
    killed task).

    A line whose leading stamp parses and is OUTSIDE the window is excluded. A
    line whose stamp can't be parsed (format drift) is INCLUDED — a kill we
    can't timestamp is still a kill, and a digest must not silently drop one.
    Returns [{'task_id', 'pr_url'}] (either may be None).

    `window_start`/`now` are naive datetimes in host-local time, matching the
    log stamp. Pure over the log text so tests inject fixtures."""
    firings: list[dict[str, Optional[str]]] = []
    seen: set[Optional[str]] = set()
    for line in log_text.splitlines():
        if 'REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED' not in line:
            continue
        m = _FIRING_LINE_RE.search(line)
        if m:
            try:
                stamp = datetime.strptime(m.group('ts'), '%Y-%m-%d %H:%M:%S')
            except ValueError:
                stamp = None
            if stamp is not None and not (window_start <= stamp <= now):
                continue
            task = _unrepr(m.group('task'))
            pr = _unrepr(m.group('pr'))
        else:
            bm = _FIRING_BARE_RE.search(line)
            if not bm:
                continue
            task = _unrepr(bm.group('task'))
            pr = _unrepr(bm.group('pr'))
        # Dedup by task_id when present (a retry/replay of the same kill is one
        # firing). A task-less line falls back to dedup on pr_url so it isn't
        # collapsed with every other task-less line.
        dedup_key = task if task is not None else f'pr::{pr}'
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        firings.append({'task_id': task, 'pr_url': pr})
    return firings


# -------------------- false-kill detection (gh) --------------------

def gh_pr_state(pr_url: str) -> str:
    """Return 'merged', 'open', 'closed', or 'unknown' for a PR. Read-only
    (`gh pr view --json state,mergedAt`). ANY failure — gh missing, network,
    auth, bad URL, unparseable JSON — degrades to 'unknown', NEVER a false-kill.
    Mockable seam for tests."""
    if not pr_url:
        return 'unknown'
    cmd = ['gh', 'pr', 'view', pr_url, '--repo', REPO,
           '--json', 'state,mergedAt']
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=GH_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        log(f'gh pr view {pr_url} failed: {type(e).__name__}: {e}', 'WARN')
        return 'unknown'
    if proc.returncode != 0:
        log(f'gh pr view {pr_url} returned {proc.returncode}: '
            f'{proc.stderr.strip()[:200]}', 'WARN')
        return 'unknown'
    try:
        data = json.loads(proc.stdout or '{}')
    except (json.JSONDecodeError, TypeError):
        return 'unknown'
    if not isinstance(data, dict):
        return 'unknown'
    state = data.get('state')
    if isinstance(state, str) and state:
        # gh reports MERGED/OPEN/CLOSED; mergedAt corroborates a merge.
        normalized = state.strip().lower()
        if normalized == 'merged' or data.get('mergedAt'):
            return 'merged'
        return normalized
    return 'unknown'


def classify_false_kills(
    firings: list[dict[str, Optional[str]]],
    state_fn: Callable[[str], str] = gh_pr_state,
) -> dict[str, int]:
    """For each fired PR, ask `state_fn` whether it later merged. Returns
    {'false_kills', 'unknown', 'not_merged'} counts. A fired-then-MERGED PR is a
    likely false-kill (the review was killed but the PR was fine). A PR whose
    state can't be read — or a firing with no pr_url — is 'unknown', NEVER a
    false-kill (gh failures degrade gracefully). Pure over `state_fn` so tests
    inject a stub."""
    counts = {'false_kills': 0, 'unknown': 0, 'not_merged': 0}
    for firing in firings:
        pr_url = firing.get('pr_url')
        if not pr_url:
            counts['unknown'] += 1
            continue
        state = state_fn(pr_url)
        if state == 'merged':
            counts['false_kills'] += 1
        elif state == 'unknown':
            counts['unknown'] += 1
        else:
            counts['not_merged'] += 1
    return counts


# -------------------- recommendation (pure) --------------------

def round_up_to_5min(seconds: float) -> int:
    """Round UP to the next 5-minute (300s) boundary. <= 0 -> 0."""
    if seconds <= 0:
        return 0
    return int(math.ceil(seconds / 300.0) * 300)


def compute_recommendation(
    dist: dict[str, float],
    ceiling: int,
    firing_count: int,
    false_kill_count: int,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Decide whether to recommend raising the ceiling, and to what.

    HEADROOM_LOW: p95 >= ceiling * headroom_low_ratio (only when there are
    samples AND the ceiling is enabled). FALSE_KILL: >= 1 fired-then-merged PR.
    recommended = max(round_up_to_5min(p99 * recommend_multiplier), ceiling).
    If HEADROOM_LOW or FALSE_KILL -> recommend RAISING to `recommended`; else
    "no change". Pure; safe on an empty distribution (all percentiles 0)."""
    p95 = dist['p95']
    p99 = dist['p99']
    count = dist['count']
    ceiling_enabled = ceiling > 0

    headroom_low = bool(
        ceiling_enabled and count > 0
        and p95 >= ceiling * rules['headroom_low_ratio'])
    false_kill = false_kill_count >= 1

    recommended = max(
        round_up_to_5min(p99 * rules['recommend_multiplier']),
        ceiling if ceiling_enabled else 0,
    )

    if not ceiling_enabled:
        text = ('no change — ceiling is DISABLED '
                '(OL_REVIEW_SESSION_CEILING_SECONDS <= 0); no headroom analysis. '
                f'p99={_min(p99)}.')
        raise_recommended = False
    elif headroom_low or false_kill:
        reasons = []
        if headroom_low:
            reasons.append(
                f'p95={_min(p95)} >= {int(rules["headroom_low_ratio"] * 100)}% '
                f'of the {_min(ceiling)} ceiling (HEADROOM_LOW)')
        if false_kill:
            reasons.append(f'{false_kill_count} fired-then-merged PR(s) '
                           '(FALSE_KILL)')
        text = (f'RAISE ceiling to {_min(recommended)} '
                f'(from {_min(ceiling)}) — ' + '; '.join(reasons) + '.')
        raise_recommended = True
    else:
        headroom = ceiling - p99
        text = (f'no change — ceiling has {_min(headroom)} headroom over p99 '
                f'(p99={_min(p99)}, ceiling={_min(ceiling)}).')
        raise_recommended = False

    return {
        'headroom_low': headroom_low,
        'false_kill': false_kill,
        'recommended_ceiling_sec': recommended,
        'raise_recommended': raise_recommended,
        'recommendation_text': text,
    }


# -------------------- digest formatting --------------------

def _min(seconds: float) -> str:
    """Render a seconds value as a compact 'Nm' (rounded to 1 decimal min)."""
    return f'{seconds / 60.0:.1f}min'


def build_digest(
    dist: dict[str, float],
    ceiling: int,
    firings: list[dict[str, Optional[str]]],
    false_kill_counts: dict[str, int],
    rec: dict[str, Any],
    window_days: int,
) -> str:
    """Assemble the digest body: a one-line headline + distribution + headroom +
    firing/false-kill counts + the recommendation. Terse on a clean window."""
    count = dist['count']
    firing_count = len(firings)
    notable = rec['headroom_low'] or rec['false_kill'] or firing_count > 0
    headline = ('Review-ceiling fit: ATTENTION' if (rec['headroom_low']
                or rec['false_kill']) else 'Review-ceiling fit: OK')

    if count == 0:
        body = (
            f'{headline} — no Mirror PR-review samples in the last '
            f'{window_days}d (ceiling={_min(ceiling)}). '
            f'Firings={firing_count}. {rec["recommendation_text"]}'
        )
        return body

    firing_rate = (firing_count / count) if count else 0.0
    headroom_p95 = ceiling - dist['p95']
    headroom_p99 = ceiling - dist['p99']

    lines = [
        f'{headline} (window={window_days}d, ceiling={_min(ceiling)}).',
        (f'Durations: n={count}, p50={_min(dist["p50"])}, '
         f'p90={_min(dist["p90"])}, p95={_min(dist["p95"])}, '
         f'p99={_min(dist["p99"])}, max={_min(dist["max"])}.'),
        (f'Headroom: ceiling-p95={_min(headroom_p95)}, '
         f'ceiling-p99={_min(headroom_p99)}'
         + (' [HEADROOM_LOW]' if rec['headroom_low'] else '') + '.'),
        (f'Firings: {firing_count} review_session_timeout '
         f'({firing_rate * 100:.1f}% of reviews); '
         f'false-kills={false_kill_counts["false_kills"]}'
         f' (unknown={false_kill_counts["unknown"]})'
         + (' [FALSE_KILL]' if rec['false_kill'] else '') + '.'),
        f'Recommendation: {rec["recommendation_text"]}',
    ]
    if not notable:
        # Lead with the terse OK so the absence of a problem is observable at a
        # glance, then keep the detail for the record.
        lines[0] = (f'{headline} — p99={_min(dist["p99"])}, '
                    f'headroom={_min(headroom_p99)} over p99.')
    return '\n'.join(lines)


# -------------------- alert emission --------------------

def emit_digest(message: str) -> bool:
    """Emit ONE informational digest via larry_alerts with route='digest' (NO
    DM; surfaced in the CEO digest) — NEVER 'escalate'. severity='warning'
    buckets the cooldown only; the route is what guarantees this cannot page
    Larry. Returns True iff appended; never raises."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source=ALERT_SOURCE,
            severity='warning',
            message=message,
            subject=ALERT_SUBJECT,
            route='digest',
        )
    except Exception as e:  # noqa: BLE001 — emission must never crash the healer
        log(f'emit_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- main --------------------

def main() -> int:
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return 0
    heartbeat()

    rules = load_rules()
    if not rules['enabled']:
        log('disabled via config (enabled=false); exiting cleanly')
        return 0

    window_days = rules['window_days']
    ceiling = get_current_ceiling()

    now_utc = datetime.now(timezone.utc)
    window_start_utc = now_utc - timedelta(days=window_days)
    # Firings are windowed in host-local naive time (the notifier-log stamp).
    now_local = datetime.now()
    window_start_local = now_local - timedelta(days=window_days)

    durations = collect_review_durations(
        iter_cost_rows(cost_source_paths()), window_start_utc, now_utc)
    dist = duration_distribution(durations)

    firings = parse_timeout_firings(
        read_notifier_log(), window_start_local, now_local)
    false_kill_counts = classify_false_kills(firings)

    rec = compute_recommendation(
        dist, ceiling, len(firings), false_kill_counts['false_kills'], rules)

    message = build_digest(
        dist, ceiling, firings, false_kill_counts, rec, window_days)

    emitted = emit_digest(message)
    log(f'tick: n={dist["count"]} p99={dist["p99"]:.0f}s ceiling={ceiling}s '
        f'firings={len(firings)} false_kills={false_kill_counts["false_kills"]} '
        f'unknown={false_kill_counts["unknown"]} '
        f'raise_recommended={rec["raise_recommended"]} '
        f'recommended={rec["recommended_ceiling_sec"]}s emitted={emitted}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the timer
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
