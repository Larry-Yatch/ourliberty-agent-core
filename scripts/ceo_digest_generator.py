#!/usr/bin/env python3
"""ceo_digest_generator.py — N6 daily + weekly CEO digest generator.

Spec: agents/beacon/specs/approvals-queue-rework.md § 5 node N6.

A scheduled, LLM-backed job that reads the prior period's chain activity and
writes a CEO-voice summary to a store the dashboard reads. Two cadences share
this module:

  - daily  — fires 6:00am Larry-local, covers the prior local-day.
  - weekly — fires Monday 6:00am Larry-local, covers the prior local-week
             (the Mon..Sun that just ended).

The summary is written as a single `ceo_digest` chain_event row (payload
carries the period, window bounds, the CEO-voice text, and a structured raw
fallback). The dashboard's Approvals page already reads `chain_events`, so the
N6 digest card consumes this with no new infrastructure.

Voice contract (acceptance criterion, spec § 5 N6): CEO-to-CEO, plain
business outcomes. NO healer / PR / marker / dispatch / chain jargon. When the
LLM voice is unavailable (timeout, auth, parse failure), the row still lands
with a deterministic plain-English `raw` rendering and `used_fallback: true`,
so the card always has something glanceable to show ("falls through to raw").

Timezone (spec § 6 [LOCKED] 6:00am Larry-local): Larry's tz is documented as
America/Denver (MDT/MST) — the droplet system tz, the ledger timer comment,
and Pulse's MDT digest all agree. It is configurable via OURLIBERTY_LARRY_TZ
for the single case where that documented value is ever wrong; the systemd
timers pin the same zone in their OnCalendar lines.

Stdlib + supabase-py (via chain_event_emit) + the `claude` CLI.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# Sibling scripts/ on path for chain_event_emit / chain_event_shipper / ledger.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import active_tier  # noqa: E402
import chain_event_emit as cee  # noqa: E402
import larry_alerts  # noqa: E402
import task_terminal_state as tts  # noqa: E402 — shared terminal-state probe
from test_isolation_guard import refuse_under_test  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
APPROVALS_FILE = AGENTS_ROOT / 'state' / 'beacon-pending-approvals.json'
COSTS_FILE = AGENTS_ROOT / 'blackboard' / 'costs.jsonl'
ACTIVE_TIER_FILE = AGENTS_ROOT / 'blackboard' / 'active-tier.json'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'ceo-digest-generator.heartbeat'

GH_REPO = 'Larry-Yatch/ourliberty-agent-core'
GH_TIMEOUT_SEC = 20
GH_LIST_LIMIT = 60

# Single documented edit point for Larry's timezone (matches the zone pinned in
# the systemd timer OnCalendar lines). See module docstring.
DEFAULT_LARRY_TZ = 'America/Denver'

# Summarization model. Sonnet is the established choice for these scheduled
# LLM jobs (see scripts/run_medic.sh); a digest is a cheap summarization task.
DIGEST_MODEL = 'claude-sonnet-4-6'
CLAUDE_TIMEOUT_SEC = int(os.environ.get('CEO_DIGEST_CLAUDE_TIMEOUT_SEC', '180'))

# chain_events types that signal a decision resolved WITHOUT Larry acting on it
# (the "auto-cleared" bucket). `clarify_response` = a clarification answered
# down the chain; `larry_action` carries the actor, filtered below so only
# system/auto resolutions count here, not Larry's own clicks.
AUTO_CLEARED_EVENT_TYPES = ('clarify_response', 'larry_action')
# chain_events types that represent raised-attention conditions in the window.
ATTENTION_EVENT_TYPES = ('escalation', 'larry_alert', 'sentinel_alert')

VALID_PERIODS = ('daily', 'weekly')

_LOGGER = logging.getLogger('ceo_digest_generator')


def resolve_log_dir() -> Path:
    """Log dir, honoring OURLIBERTY_LOG_DIR (test isolation; see conftest)."""
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    return Path(override) if override else AGENTS_ROOT / 'logs'


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        log_dir = resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / 'ceo-digest-generator.log', 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- timezone + period window --------------------


def resolve_larry_tz() -> ZoneInfo:
    name = os.environ.get('OURLIBERTY_LARRY_TZ', DEFAULT_LARRY_TZ)
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — bad tz name must not crash the digest
        log(f'invalid OURLIBERTY_LARRY_TZ={name!r}; falling back to '
            f'{DEFAULT_LARRY_TZ}', 'WARN')
        return ZoneInfo(DEFAULT_LARRY_TZ)


def period_window(
    period: str, now_utc: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime, str]:
    """Return (start_utc, end_utc, human_label) for the period to summarize.

    Boundaries are computed in Larry-local time so "the prior day"/"the prior
    week" match Larry's calendar, not UTC's. Both returned datetimes are UTC
    (chain_events.ts and costs.jsonl rows are UTC).
    """
    now_local = now_utc.astimezone(tz)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'daily':
        end_local = today_local
        start_local = end_local - timedelta(days=1)
        label = start_local.strftime('%A, %B %-d')
    elif period == 'weekly':
        # Most recent local Monday at 00:00 is the window end; covers the
        # 7 local days (Mon..Sun) immediately before it.
        this_monday = today_local - timedelta(days=today_local.weekday())
        end_local = this_monday
        start_local = end_local - timedelta(days=7)
        label = (f"{start_local.strftime('%B %-d')}–"
                 f"{(end_local - timedelta(days=1)).strftime('%B %-d')}")
    else:
        raise ValueError(f'unknown period: {period!r}')
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
        label,
    )


# -------------------- activity gathering --------------------


def _strip_commit_prefix(title: str) -> str:
    """Drop a conventional-commit `type(scope):` prefix for CEO-readable text."""
    return re.sub(r'^\s*\w+(\([^)]*\))?!?:\s*', '', title or '').strip()


def _run_gh(args: list[str]) -> Optional[list[dict]]:
    try:
        res = subprocess.run(
            args, capture_output=True, text=True,
            timeout=GH_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or '[]')
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fetch_shipped(start_utc: datetime, end_utc: datetime) -> list[dict]:
    """Merged PRs with mergedAt in [start, end). 'What shipped' ground truth."""
    data = _run_gh([
        'gh', 'pr', 'list', '--repo', GH_REPO, '--state', 'merged',
        '--limit', str(GH_LIST_LIMIT), '--json', 'number,title,mergedAt',
    ])
    if not data:
        return []
    out = []
    for pr in data:
        merged = _parse_iso(pr.get('mergedAt', ''))
        if merged and start_utc <= merged < end_utc:
            out.append({
                'number': pr.get('number'),
                'title': _strip_commit_prefix(pr.get('title', '')),
            })
    out.sort(key=lambda p: p.get('number') or 0, reverse=True)
    return out


def fetch_events_in_window(
    start_utc: datetime, end_utc: datetime, client: Optional[Any] = None
) -> list[dict]:
    """Paginate chain_events in [start, end). Returns raw rows (may be empty).

    Mirrors heal_chain_event_type_audit.fetch_event_type_counts pagination.
    Best-effort: any Supabase failure logs WARN and returns []. The digest
    degrades to a spend/shipped/waiting-only summary rather than crashing.
    """
    cli = client if client is not None else cee._get_client()
    if cli is None:
        log('Supabase client unavailable; chain_events buckets will be empty',
            'WARN')
        return []
    start_iso, end_iso = start_utc.isoformat(), end_utc.isoformat()
    rows: list[dict] = []
    page, page_size = 0, 1000
    try:
        while True:
            res = (
                cli.table('chain_events')
                   .select('event_type,ts,task_id,payload')
                   .gte('ts', start_iso)
                   .lt('ts', end_iso)
                   .range(page * page_size, (page + 1) * page_size - 1)
                   .execute()
            )
            batch = getattr(res, 'data', None) or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
    except Exception as e:  # noqa: BLE001
        log(f'chain_events query failed: {type(e).__name__}: {e}', 'WARN')
        return []
    return rows


def _payload_actor(row: dict) -> str:
    payload = row.get('payload')
    if isinstance(payload, dict):
        return str(payload.get('actor', '') or '')
    return ''


def count_auto_cleared(events: list[dict]) -> int:
    """Decisions resolved without Larry acting.

    `clarify_response` always counts (a clarification answered down the chain).
    `larry_action` counts ONLY when the actor is not Larry (system/auto clear);
    Larry's own clicks are decisions he made, not auto-clears.
    """
    n = 0
    for ev in events:
        et = ev.get('event_type')
        if et == 'clarify_response':
            n += 1
        elif et == 'larry_action' and _payload_actor(ev).lower() != 'larry':
            n += 1
    return n


def _attention_fix_task_id(ev: dict, payload: dict) -> Optional[str]:
    """Derive the candidate fix task_id for an attention event, or None.

    The terminal-state probe needs a stable task id to look up the fix PR. An
    attention event carries it as a top-level `task_id` or in its payload
    (`task_id` / `fix_task_id`). When none is present we CANNOT derive a fix
    id — the conservative posture (spec §3.6) is to leave the item unchanged
    rather than guess, so this returns None and the caller does not probe."""
    for cand in (ev.get('task_id'), payload.get('task_id'),
                 payload.get('fix_task_id')):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return None


def collect_attention(events: list[dict], probe_fn=None) -> list[str]:
    """Subjects of raised-attention conditions in the window (deduped).

    Terminal-state suppression (spec §3.6): before surfacing an item as a
    still-open "needs your call" problem, derive its candidate fix task_id and
    probe the work's terminal ground truth. If the fix PR has MERGED, the
    problem is already resolved — suppress the item rather than re-present a
    fixed issue as open (the 2026-06-14 Check-0 watermark / PR #482 incident).

    Conservative posture: ONLY a positively MERGED fix suppresses. No derivable
    task_id, or any non-MERGED probe (OPEN/CLOSED/UNKNOWN — gh failure, no
    match, abandoned fix), leaves the item in place. A genuinely-open problem
    is never hidden.

    `probe_fn` is an injectable seam for tests; defaults to the shared
    `task_terminal_state` probe."""
    if probe_fn is None:
        probe_fn = tts.task_terminal_state
    seen: list[str] = []
    for ev in events:
        if ev.get('event_type') not in ATTENTION_EVENT_TYPES:
            continue
        payload = ev.get('payload') if isinstance(ev.get('payload'), dict) else {}
        subject = (payload.get('subject') or payload.get('message')
                   or ev.get('task_id') or ev.get('event_type'))
        subject = str(subject).strip()
        if not subject or subject in seen:
            continue
        fix_task_id = _attention_fix_task_id(ev, payload)
        if fix_task_id is not None and probe_fn(fix_task_id) == tts.MERGED:
            log(f'attention item suppressed — fix shipped: subject={subject!r} '
                f'task_id={fix_task_id!r}')
            continue
        seen.append(subject)
    return seen


def collect_self_healed(start_utc: datetime, end_utc: datetime) -> list[str]:
    """One-line outcome summaries of routine heals that fixed themselves.

    These are `route: "digest"` alerts the bot deliberately did NOT DM (the
    fix-first/notify-on-outcome routing, 2026-06-03): something broke, the team
    fixed it, and it wasn't significant enough to interrupt Larry. They belong
    in the digest as proof-of-work, not in his DMs.

    Each line is the producer's own outcome `message` (written at emit time when
    the real outcome is known). Records without a usable message are skipped
    rather than rendered as an empty bullet. Deduped, newest-window order
    preserved. Best-effort — any read failure yields []."""
    try:
        records = larry_alerts.read_digest_window(start_utc, end_utc)
    except Exception as e:  # noqa: BLE001 — digest must not crash on this
        log(f'self-healed digest read failed: {type(e).__name__}: {e}', 'WARN')
        return []
    seen: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        summary = str(rec.get('message') or '').strip()
        if summary and summary not in seen:
            seen.append(summary)
    return seen


def fetch_decisions_waiting() -> list[str]:
    """Plain-English summaries of decisions still pending Larry (point-in-time)."""
    if not APPROVALS_FILE.exists():
        return []
    try:
        with open(APPROVALS_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    pending = data.get('pending') if isinstance(data, dict) else None
    if not isinstance(pending, list):
        return []
    out = []
    for item in pending:
        if isinstance(item, dict):
            summary = item.get('plan_summary') or item.get('summary') or item.get('id')
            if summary:
                out.append(str(summary).strip())
    return out


def sum_spend(start_utc: datetime, end_utc: datetime) -> float:
    """Total LLM spend (USD) over the window, from costs.jsonl."""
    if not COSTS_FILE.exists():
        return 0.0
    total = 0.0
    try:
        with open(COSTS_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ts = _parse_iso(obj.get('ts', ''))
                except (json.JSONDecodeError, ValueError):
                    continue
                if ts is None or not (start_utc <= ts < end_utc):
                    continue
                cost = obj.get('cost_usd')
                if cost is None:
                    continue
                try:
                    total += float(cost)
                except (TypeError, ValueError):
                    continue
    except OSError:
        return 0.0
    return round(total, 2)


def gather_activity(
    start_utc: datetime, end_utc: datetime, client: Optional[Any] = None
) -> dict:
    """Assemble the structured activity record for the window."""
    events = fetch_events_in_window(start_utc, end_utc, client=client)
    shipped = fetch_shipped(start_utc, end_utc)
    return {
        'shipped': shipped,
        'shipped_count': len(shipped),
        'auto_cleared_count': count_auto_cleared(events),
        'decisions_waiting': fetch_decisions_waiting(),
        'spend_usd': sum_spend(start_utc, end_utc),
        'attention': collect_attention(events),
        'self_healed': collect_self_healed(start_utc, end_utc),
    }


# -------------------- rendering --------------------


def _plural(n: int, singular: str, plural: Optional[str] = None) -> str:
    return singular if n == 1 else (plural or singular + 's')


def render_raw(period: str, label: str, activity: dict) -> str:
    """Deterministic plain-English fallback. No jargon — plain outcomes only."""
    span = 'Yesterday' if period == 'daily' else 'Last week'
    ship_n = activity['shipped_count']
    cleared_n = activity['auto_cleared_count']
    waiting = activity['decisions_waiting']
    waiting_n = len(waiting)
    spend = activity['spend_usd']
    attention = activity['attention']
    self_healed = activity.get('self_healed', [])
    healed_n = len(self_healed)

    lines = [f'{span} ({label}):']
    if ship_n:
        lines.append(f"  • {ship_n} {_plural(ship_n, 'improvement')} shipped.")
        for item in activity['shipped'][:5]:
            title = item.get('title') or 'an update'
            lines.append(f"      – {title}")
        extra = ship_n - 5
        if extra > 0:
            lines.append(f"      – (+{extra} more)")
    else:
        lines.append('  • Nothing new shipped.')

    if cleared_n:
        lines.append(
            f"  • {cleared_n} {_plural(cleared_n, 'item')} resolved on "
            f"{'its' if cleared_n == 1 else 'their'} own — no action needed "
            f"from you.")

    if healed_n:
        lines.append(
            f"  • {healed_n} {_plural(healed_n, 'thing')} broke and got fixed "
            f"automatically — no action needed:")
        for s in self_healed[:5]:
            lines.append(f"      – {s}")
        extra = healed_n - 5
        if extra > 0:
            lines.append(f"      – (+{extra} more)")

    if waiting_n:
        lines.append(
            f"  • {waiting_n} {_plural(waiting_n, 'decision')} waiting on you:")
        for s in waiting[:5]:
            lines.append(f"      – {s}")
        extra = waiting_n - 5
        if extra > 0:
            lines.append(f"      – (+{extra} more)")
    else:
        lines.append('  • Nothing waiting on you.')

    lines.append(f"  • ${spend:,.2f} spent running the team.")

    if attention:
        lines.append('  • Worth a look:')
        for s in attention[:3]:
            lines.append(f"      – {s}")
    return '\n'.join(lines)


# Words the CEO voice must never use — engineering-internal jargon. The prompt
# bans them and the raw fallback avoids them by construction.
JARGON_BANLIST = (
    'PR', 'pull request', 'merge', 'commit', 'branch', 'healer', 'heal',
    'marker', 'dispatch', 'preflight', 'chain_events', 'chain event',
    'Mirror', 'Forge', 'Beacon', 'Pulse', 'Medic', 'systemd', 'timer',
    'clarify', 'approval_request', 'auto-clear', 'supabase', 'token',
)


def build_prompt(period: str, label: str, activity: dict) -> str:
    """CEO-to-CEO summarization prompt with an explicit jargon ban."""
    span = 'the prior day' if period == 'daily' else 'the prior week'
    facts = {
        'period': period,
        'window': label,
        'things_shipped': [i.get('title') for i in activity['shipped']],
        'things_shipped_count': activity['shipped_count'],
        'items_resolved_automatically': activity['auto_cleared_count'],
        'things_that_broke_and_self_fixed': activity.get('self_healed', []),
        'decisions_waiting_on_you': activity['decisions_waiting'],
        'amount_spent_usd': activity['spend_usd'],
        'worth_a_look': activity['attention'],
    }
    banned = ', '.join(f'"{w}"' for w in JARGON_BANLIST)
    return (
        "You are writing a short status note from one CEO to another CEO "
        f"about how an autonomous software team performed over {span}.\n\n"
        "VOICE RULES (strict):\n"
        "- Plain business outcomes only. Talk about what got done and what "
        "needs the CEO's attention — never about the machinery.\n"
        f"- NEVER use any of these engineering words: {banned}. If a fact "
        "involves one, translate it into a plain outcome (e.g. a shipped code "
        "change is \"an improvement we shipped\").\n"
        "- Warm, direct, concise. No bullet-point dump, no preamble, no "
        "sign-off. 2-5 sentences.\n"
        "- Lead with the single most important thing the CEO should know.\n"
        "- If a number is zero, you may omit it rather than reporting a "
        "void.\n\n"
        "Here are the facts (JSON):\n"
        f"{json.dumps(facts, indent=2)}\n\n"
        "Write the note now. Output ONLY the note text."
    )


def generate_ceo_voice(prompt: str) -> tuple[Optional[str], Optional[float]]:
    """Invoke the claude CLI to produce the CEO-voice note.

    Returns (text, cost_usd). On any failure (timeout, non-zero exit, JSON
    parse failure, empty result) returns (None, None) and the caller falls
    through to the raw rendering. Never raises.
    """
    refuse_under_test('claude-spawn')
    try:
        proc = subprocess.run(
            ['claude', '--print', '--model', DIGEST_MODEL,
             '--output-format', 'json', prompt],
            capture_output=True, text=True,
            env=active_tier.durable_claude_env(),
            timeout=CLAUDE_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f'claude invocation failed: {type(e).__name__}: {e}', 'WARN')
        return None, None
    if proc.returncode != 0:
        log(f'claude exited {proc.returncode}; falling through to raw', 'WARN')
        return None, None
    try:
        data = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError:
        log('claude output was not JSON; falling through to raw', 'WARN')
        return None, None
    text = (data.get('result') or '').strip() if isinstance(data, dict) else ''
    if not text:
        log('claude returned empty result; falling through to raw', 'WARN')
        return None, None
    cost = None
    if isinstance(data, dict):
        raw_cost = data.get('total_cost_usd', data.get('cost_usd'))
        if raw_cost is not None:
            try:
                cost = float(raw_cost)
            except (TypeError, ValueError):
                cost = None
    return text, cost


# -------------------- cost capture + write --------------------


def _account_tier() -> str:
    try:
        with open(ACTIVE_TIER_FILE, encoding='utf-8') as f:
            return str(json.load(f).get('tier') or 'tier1')
    except (OSError, json.JSONDecodeError, AttributeError):
        return 'tier1'


def append_cost_row(period: str, cost_usd: Optional[float], success: bool) -> None:
    """Record this run's own LLM spend to costs.jsonl (ledger + future digests
    see it). Best-effort; mirrors run_medic.sh's cost row shape."""
    if cost_usd is None:
        return
    ts = datetime.now(timezone.utc).isoformat()
    row = {
        'ts': ts,
        'agent': 'ceo-digest',
        'task_id': f'ceo-digest-{period}-{ts}',
        'task_type': f'ceo-digest-{period}',
        'model': DIGEST_MODEL,
        'account': _account_tier(),
        'cost_usd': cost_usd,
        'success': success,
        'source': 'ceo_digest_generator.py',
    }
    try:
        COSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COSTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')
    except OSError as e:
        log(f'cost-capture append failed: {type(e).__name__}: {e}', 'WARN')


def write_digest(
    period: str,
    label: str,
    start_utc: datetime,
    end_utc: datetime,
    activity: dict,
    summary_text: str,
    used_fallback: bool,
    client: Optional[Any] = None,
) -> bool:
    """Emit the ceo_digest chain_event the dashboard card reads."""
    payload = {
        'period': period,
        'window_label': label,
        'window_start': start_utc.isoformat(),
        'window_end': end_utc.isoformat(),
        'summary': summary_text,
        'used_fallback': used_fallback,
        'raw': render_raw(period, label, activity),
        'metrics': {
            'shipped_count': activity['shipped_count'],
            'auto_cleared_count': activity['auto_cleared_count'],
            'decisions_waiting_count': len(activity['decisions_waiting']),
            'spend_usd': activity['spend_usd'],
            'attention_count': len(activity['attention']),
            'self_healed_count': len(activity.get('self_healed', [])),
        },
    }
    # NB: cost_usd stays None. activity['spend_usd'] is the window's *reported
    # rollup* (the sum of everyone else's session spend over the digest window),
    # not cost newly incurred by this digest — it lives in payload.metrics.
    # spend_usd above. Writing it into chain_events.cost_usd double-counted it
    # in any SUM(cost_usd) consumer (e.g. the dashboard's day-over-day spend
    # panel), which only expects per-session incurred cost. The digest's own
    # LLM cost is tracked separately via append_cost_row -> costs.jsonl.
    return cee.emit_event(
        event_type='ceo_digest',
        agent='ceo-digest',
        task_id=f'ceo-digest-{period}-{start_utc.date().isoformat()}',
        payload=payload,
        cost_usd=None,
        client=client,
    )


def run(period: str, now_utc: Optional[datetime] = None,
        client: Optional[Any] = None) -> int:
    """Generate and persist one digest. Returns process exit code."""
    if period not in VALID_PERIODS:
        log(f'invalid period {period!r}; expected one of {VALID_PERIODS}', 'ERROR')
        return 2
    heartbeat()
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = resolve_larry_tz()
    start_utc, end_utc, label = period_window(period, now_utc, tz)
    log(f'{period} digest: window {start_utc.isoformat()} .. '
        f'{end_utc.isoformat()} ({label})')

    activity = gather_activity(start_utc, end_utc, client=client)
    log(f'activity: shipped={activity["shipped_count"]} '
        f'auto_cleared={activity["auto_cleared_count"]} '
        f'waiting={len(activity["decisions_waiting"])} '
        f'spend=${activity["spend_usd"]} '
        f'attention={len(activity["attention"])} '
        f'self_healed={len(activity["self_healed"])}')

    summary_text, llm_cost = generate_ceo_voice(build_prompt(period, label, activity))
    used_fallback = summary_text is None
    if used_fallback:
        summary_text = render_raw(period, label, activity)
        log('used raw fallback (LLM voice unavailable)')
    else:
        append_cost_row(period, llm_cost, success=True)

    ok = write_digest(period, label, start_utc, end_utc, activity,
                      summary_text, used_fallback, client=client)
    if not ok:
        log('write_digest did not persist (Supabase unavailable?)', 'WARN')
        return 1
    log(f'{period} digest written (fallback={used_fallback})')
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Generate a CEO digest.')
    parser.add_argument('--period', required=True, choices=VALID_PERIODS,
                        help='daily (prior day) or weekly (prior week)')
    args = parser.parse_args(argv)
    return run(args.period)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
