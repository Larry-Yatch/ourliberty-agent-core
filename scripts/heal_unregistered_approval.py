#!/usr/bin/env python3
"""heal_unregistered_approval.py — reconciliation net for stranded direction-asks.

The Approvals tab is fed ONLY by `approval_request` chain_events, which exist
only when Beacon emits a canonical `=== APPROVAL_REQUEST ===` marker. A decision
that needs Larry's DIRECTION (choose between two options before a dispatch) is
supposed to be emitted as a binary approval_request (see agents/beacon/CLAUDE.md
"Direction-asks are APPROVAL_REQUESTs"). When that emission is missed and the ask
is written as a `pulse/beacon-result` larry-alert instead, no event is registered
and the decision never reaches the tab — it strands in the Telegram stream. That
is exactly what happened to the 2026-06-03 deploy-notifier ask.

This healer is the ENFORCEMENT NET behind the emission guidance. Each run it:

  1. SCANS `blackboard/larry-alerts.jsonl` over a trailing window (default 24h)
     for APPROVAL-CLASS escalations: `route == "escalate"` AND a decision signal
     (suggested_action starts with a decision verb, or message/subject contains a
     decision phrase). The heuristic is conservative and config-driven
     (config/unregistered-approval-heuristics.json) — a false positive is a
     dismissible tab card; a false negative is the bug we are killing.
  2. MATCHES each candidate against already-registered approvals
     (beacon-pending-approvals.json pending + history) by a stable dedup_identity
     derived from the alert subject, so a marker Beacon DID emit is never
     duplicated.
  3. PROMOTES each UNMATCHED candidate by registering an `approval_request`
     (target_agent="beacon") via the same `add_pending` + `emit_event` pair the
     bot uses, so it lands on the tab. Binary options are reconstructed from the
     alert's suggested_action where parseable; otherwise a single "needs-triage"
     approval_request carries the message + suggested_action verbatim, with
     approve/reject both routing back to Beacon to formalize.
  4. DEDUPS via a state file (state/heal-unregistered-approval-promoted.json) so
     each source alert is promoted at most once; idempotent across ticks.
  5. HEARTBEATS each run; on its own failure emits a larry-alert (it is itself
     covered by the daemon-liveness watchers).

Stdlib + the existing chain_event_emit / beacon_approval_handler helpers only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import beacon_approval_handler as approval  # noqa: E402
import chain_event_emit  # noqa: E402

CONFIG_FILE = _SCRIPT_DIR.parent / 'config' / 'unregistered-approval-heuristics.json'

# Built-in defaults. The healer NEVER silently disables itself: if the config
# file is missing or malformed it falls back to these (and logs a WARN), so a
# bad edit cannot turn the net off without anyone noticing.
DEFAULT_SCAN_WINDOW_HOURS = 24
DEFAULT_SUGGESTED_ACTION_PREFIXES = ('Reply', 'Tell Beacon', 'Choose', 'Pick')
DEFAULT_DECISION_PHRASES = (
    'holding APPROVAL_REQUEST',
    'needs your call',
    'your direction',
    'which option',
)

# Source label stamped on promoted approvals + self-failure alerts.
HEALER_SOURCE = 'heal-unregistered-approval'

# Deterministic task_id prefix for promoted approvals. Keeps the healer's own
# registrations recognizable and lets the dedup hold even if the state file is
# lost (the deterministic id already lives in pending/history).
PROMOTED_TASK_PREFIX = 'unreg-approval'

# "Choose A or B" / "Pick A vs B" / "Reply A or B" splitter for binary options.
_BINARY_SPLIT_RE = re.compile(r'\s+(?:or|vs\.?|versus)\s+', re.IGNORECASE)
_LEADING_VERB_RE = re.compile(
    r'^\s*(?:reply|tell\s+beacon|choose|pick|whether\s+to|between)\b[:\s]*',
    re.IGNORECASE,
)


# -------------------- paths / env --------------------

def agents_root() -> Path:
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def blackboard() -> Path:
    return agents_root() / 'blackboard'


def state_dir() -> Path:
    return agents_root() / 'state'


def alerts_file() -> Path:
    return blackboard() / 'larry-alerts.jsonl'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def healer_heartbeat() -> Path:
    return blackboard() / 'heal-unregistered-approval.heartbeat'


def promoted_state_file() -> Path:
    return state_dir() / 'heal-unregistered-approval-promoted.json'


def log_file() -> Path:
    return agents_root() / 'logs' / 'heal-unregistered-approval.log'


def _chat_id() -> Optional[int]:
    """Chat id stamped on the promoted pending entry. Tab resolution flows
    through the chain_event (Supabase), not this field, so it is non-critical;
    an env override lets the promoted ask still reach a Telegram thread if set.
    """
    raw = os.environ.get('OURLIBERTY_APPROVAL_HEALER_CHAT_ID')
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        lf = log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        hb = healer_heartbeat()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({
            'ts': datetime.now(timezone.utc).isoformat(),
            'healer': HEALER_SOURCE,
        }))
    except OSError:
        pass


# -------------------- config --------------------

def load_heuristics(path: Optional[Path] = None) -> dict[str, Any]:
    """Return the heuristic config, falling back to built-in defaults on a
    missing/malformed file (never disables the net). The returned dict always
    has the three keys the evaluator reads."""
    defaults = {
        'scan_window_hours': DEFAULT_SCAN_WINDOW_HOURS,
        'suggested_action_prefixes': list(DEFAULT_SUGGESTED_ACTION_PREFIXES),
        'decision_phrases': list(DEFAULT_DECISION_PHRASES),
    }
    cfg_path = path or CONFIG_FILE
    try:
        data = json.loads(cfg_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    out = dict(defaults)
    window = data.get('scan_window_hours')
    if isinstance(window, (int, float)) and window > 0:
        out['scan_window_hours'] = float(window)
    prefixes = data.get('suggested_action_prefixes')
    if isinstance(prefixes, list):
        clean = [p for p in prefixes if isinstance(p, str) and p.strip()]
        if clean:
            out['suggested_action_prefixes'] = clean
    phrases = data.get('decision_phrases')
    if isinstance(phrases, list):
        clean = [p for p in phrases if isinstance(p, str) and p.strip()]
        if clean:
            out['decision_phrases'] = clean
    return out


# -------------------- alert scanning (pure) --------------------

def parse_alert_lines(lines: list[str]) -> list[dict[str, Any]]:
    """Parse JSONL alert lines into dicts, skipping blanks + malformed."""
    out: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_alerts(path: Optional[Path] = None) -> list[dict[str, Any]]:
    p = path or alerts_file()
    try:
        with open(p, encoding='utf-8') as fh:
            return parse_alert_lines(fh.readlines())
    except OSError:
        return []


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def within_window(record: dict[str, Any], now: datetime, window_hours: float) -> bool:
    """True if the record's ts is within the trailing window ending at `now`.
    A record with a missing/unparseable ts is INCLUDED (fail toward catching a
    real decision rather than dropping it on a bad timestamp)."""
    ts = _parse_ts(record.get('ts'))
    if ts is None:
        return True
    age_h = (now - ts).total_seconds() / 3600.0
    return -1.0 <= age_h <= window_hours  # small negative tolerance for clock skew


def is_approval_class(record: dict[str, Any], heuristics: dict[str, Any]) -> bool:
    """Conservative decision-signal test. Requires route == 'escalate' AND
    either a decision-verb suggested_action prefix or a decision phrase in the
    message/subject. Notifications / non-escalate routes never qualify."""
    if record.get('kind') in ('notification', 'approval_request'):
        return False
    if record.get('route', approval_default_route()) != 'escalate':
        return False
    suggested = record.get('suggested_action')
    if isinstance(suggested, str):
        stripped = suggested.lstrip()
        for prefix in heuristics['suggested_action_prefixes']:
            if stripped[:len(prefix) + 1].lower().startswith(prefix.lower()) and (
                len(stripped) == len(prefix)
                or not stripped[len(prefix):len(prefix) + 1].isalnum()
            ):
                return True
    haystack = ' '.join(
        str(record.get(field, '')) for field in ('message', 'subject')
    ).lower()
    for phrase in heuristics['decision_phrases']:
        if phrase.lower() in haystack:
            return True
    return False


def approval_default_route() -> str:
    """Default route an alert without an explicit `route` is treated as. The
    queue's own default is 'escalate' (larry_alerts.DEFAULT_ROUTE); mirror it so
    a legacy alert written before routing existed is still eligible."""
    return 'escalate'


# -------------------- dedup identity + payload (pure) --------------------

def alert_dedup_key(record: dict[str, Any]) -> str:
    """Stable key for a source alert. Prefers the subject (the brief's
    dedup_identity basis); falls back to a hash of source+message so an alert
    with no subject still dedups deterministically."""
    subject = record.get('subject')
    if isinstance(subject, str) and subject.strip():
        return subject.strip()
    basis = f"{record.get('source', '')}|{record.get('message', '')}"
    return 'nosubject:' + hashlib.sha256(basis.encode('utf-8')).hexdigest()[:16]


def derive_task_id(dedup_key: str) -> str:
    digest = hashlib.sha256(dedup_key.encode('utf-8')).hexdigest()[:12]
    return f'{PROMOTED_TASK_PREFIX}-{digest}'


def parse_binary_options(suggested_action: Any) -> Optional[tuple[str, str]]:
    """Reconstruct (option_a, option_b) from a suggested_action like
    'Choose ship-now or scope-the-fix'. Returns None when it does not parse
    cleanly into exactly two options (caller falls back to needs-triage)."""
    if not isinstance(suggested_action, str) or not suggested_action.strip():
        return None
    body = _LEADING_VERB_RE.sub('', suggested_action.strip())
    # Only split on the first line to avoid swallowing multi-line shell hints.
    body = body.splitlines()[0].strip().rstrip('.')
    if not body:
        return None
    parts = _BINARY_SPLIT_RE.split(body)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if not a or not b:
        return None
    return a, b


def build_approval_payload(
    record: dict[str, Any], dedup_key: str,
) -> dict[str, Any]:
    """Build the approval_request payload for add_pending + the chain_event
    builder. target_agent is always 'beacon' (the action handler routes
    approve/reject there). Both options are stated in plain language so the
    tab's approve/reject buttons are self-explanatory."""
    task_id = derive_task_id(dedup_key)
    message = str(record.get('message', '')).strip()
    suggested = record.get('suggested_action')
    subject = record.get('subject') or dedup_key
    options = parse_binary_options(suggested)
    if options is not None:
        option_a, option_b = options
        summary = (
            f'Direction needed (promoted from a missed marker): '
            f'Approve = {option_a}; Reject = {option_b}.'
        )
        prompt = (
            'This direction-ask was raised as a larry-alert without an '
            'APPROVAL_REQUEST marker, so it never reached the Approvals tab; '
            f'{HEALER_SOURCE} promoted it.\n\n'
            f'Source alert subject: {subject}\n'
            f'Original message: {message}\n\n'
            f'Approve = option A: {option_a}\n'
            f'Reject  = option B: {option_b}\n\n'
            'On Larry\'s click the dashboard routes his choice back to Beacon; '
            'Beacon shapes + dispatches the chosen option.'
        )
    else:
        summary = (
            'Decision needs your direction (promoted from a missed marker; '
            'could not be parsed into two options — needs triage).'
        )
        suggested_text = suggested if isinstance(suggested, str) else '(none)'
        prompt = (
            'This decision was raised as a larry-alert without an '
            'APPROVAL_REQUEST marker, so it never reached the Approvals tab; '
            f'{HEALER_SOURCE} promoted it as a needs-triage item.\n\n'
            f'Source alert subject: {subject}\n'
            f'Original message: {message}\n'
            f'Suggested action: {suggested_text}\n\n'
            'Approve OR Reject both route back to Beacon to formalize this into '
            'a proper binary approval_request (or resolve in chat).'
        )
    return {
        'task_id': task_id,
        'summary': summary,
        'target_agent': 'beacon',
        'prompt': prompt,
        'task_type': 'direction-ask',
        'promoted_from_alert': dedup_key,
    }


# -------------------- registration matching (pure) --------------------

def registered_identities(state: dict[str, Any]) -> tuple[set[str], list[str]]:
    """Return (ids, haystacks) from pending + history. `ids` is the set of
    entry ids (for exact match against the healer's deterministic task_id).
    `haystacks` is a list of lowercased searchable strings (id + plan_summary +
    dispatch_payload prompt/summary/promoted_from_alert) for the subject-based
    collision guard against a marker Beacon already emitted."""
    ids: set[str] = set()
    haystacks: list[str] = []
    for bucket in ('pending', 'history'):
        for entry in state.get(bucket, []) or []:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get('id')
            if isinstance(entry_id, str):
                ids.add(entry_id)
            payload = entry.get('dispatch_payload') or {}
            parts = [
                entry.get('id'),
                entry.get('plan_summary'),
                payload.get('summary') if isinstance(payload, dict) else None,
                payload.get('prompt') if isinstance(payload, dict) else None,
                payload.get('promoted_from_alert') if isinstance(payload, dict) else None,
            ]
            haystacks.append(' '.join(str(p) for p in parts if p).lower())
    return ids, haystacks


def is_already_registered(
    dedup_key: str, task_id: str, state: dict[str, Any],
) -> bool:
    """True if this ask is already on the tab. Two ways to match:
      1. Our own deterministic task_id is already an entry id (idempotent
         re-run, even if the promoted-state file was lost).
      2. The alert subject text appears in any registered entry — the
         collision guard so a marker Beacon DID emit for the same decision is
         never duplicated.
    """
    ids, haystacks = registered_identities(state)
    if task_id in ids:
        return True
    needle = dedup_key.strip().lower()
    if not needle or needle.startswith('nosubject:'):
        return False
    return any(needle in hay for hay in haystacks)


# -------------------- promoted-state dedup --------------------

def load_promoted(path: Optional[Path] = None) -> dict[str, str]:
    p = path or promoted_state_file()
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    promoted = data.get('promoted') if isinstance(data, dict) else None
    if not isinstance(promoted, dict):
        return {}
    return {k: v for k, v in promoted.items() if isinstance(k, str)}


def save_promoted(promoted: dict[str, str], path: Optional[Path] = None) -> None:
    p = path or promoted_state_file()
    payload = {
        '_schema': {
            'version': 1,
            'purpose': (
                'Dedup ledger for heal_unregistered_approval.py: maps each '
                'promoted source-alert dedup_key to the ISO ts it was first '
                'promoted, so each alert lands on the Approvals tab at most '
                'once across ticks.'
            ),
        },
        'promoted': promoted,
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                       encoding='utf-8')
        os.replace(tmp, p)
    except OSError:
        pass


# -------------------- evaluation (pure) --------------------

def evaluate(
    alerts: list[dict[str, Any]],
    heuristics: dict[str, Any],
    state: dict[str, Any],
    promoted: dict[str, str],
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Return the list of approval payloads to register this tick.

    An alert is promoted iff: in-window, approval-class, not already promoted
    (state file), and not already registered (pending/history match). Pure — no
    I/O, no side effects; main() does the registration + persistence."""
    now = now or datetime.now(timezone.utc)
    window = heuristics['scan_window_hours']
    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for record in alerts:
        if not within_window(record, now, window):
            continue
        if not is_approval_class(record, heuristics):
            continue
        dedup_key = alert_dedup_key(record)
        if dedup_key in promoted or dedup_key in seen_keys:
            continue
        task_id = derive_task_id(dedup_key)
        if is_already_registered(dedup_key, task_id, state):
            continue
        payload = build_approval_payload(record, dedup_key)
        payload['_source_ts'] = record.get('ts')
        out.append(payload)
        seen_keys.add(dedup_key)
    return out


# -------------------- registration (side-effectful) --------------------

def register_approval(payload: dict[str, Any], chat_id: Optional[int]) -> bool:
    """Register one approval_request: add_pending (Beacon state) + emit_event
    (the tab feed). Mirrors the bot's force_ask path. Returns True if the
    chain_event upsert succeeded (the tab write); the pending write is
    best-effort and not gating. Strips the internal helper keys before handing
    the payload to the helpers."""
    # Only the transient _source_ts is stripped. promoted_from_alert stays on
    # the payload so add_pending persists it under dispatch_payload, where the
    # collision guard can find it on later ticks.
    source_ts = payload.pop('_source_ts', None)
    approval.add_pending(payload, chat_id=chat_id)
    kwargs = approval.build_approval_request_chain_event(payload, ts=source_ts)
    return chain_event_emit.emit_event(**kwargs)


# -------------------- main --------------------

def _emit_self_failure(message: str, suggested_action: str) -> None:
    try:
        sys.path.insert(0, str(_SCRIPT_DIR))
        import larry_alerts as la  # noqa: E402
        la.append_alert(
            source=HEALER_SOURCE,
            severity='warning',
            message=message,
            subject=f'{HEALER_SOURCE}:self-failure',
            suggested_action=suggested_action,
        )
    except Exception as e:  # noqa: BLE001
        log(f'self-failure alert emit failed: {type(e).__name__}: {e}', 'WARN')


def main() -> int:
    if kill_switch().exists():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    heuristics = load_heuristics()
    if not CONFIG_FILE.exists():
        log(f'config {CONFIG_FILE.name} missing; using built-in defaults', 'WARN')

    alerts = read_alerts()
    state = approval.load_state()
    promoted = load_promoted()

    try:
        to_promote = evaluate(alerts, heuristics, state, promoted)
    except Exception as e:  # noqa: BLE001
        log(f'evaluate failed: {type(e).__name__}: {e}', 'ERROR')
        _emit_self_failure(
            message=(
                f'{HEALER_SOURCE} failed while scanning larry-alerts for '
                f'unregistered direction-asks: {type(e).__name__}: {e}. '
                'Stranded approval-class alerts may not be reaching the tab.'
            ),
            suggested_action=(
                'Check ~/agents/logs/heal-unregistered-approval.log and run '
                'python3 ~/agent-core/scripts/heal_unregistered_approval.py.'
            ),
        )
        return 1

    if not to_promote:
        log(f'tick: scanned {len(alerts)} alert(s); nothing to promote')
        return 0

    chat_id = _chat_id()
    promoted_count = 0
    for payload in to_promote:
        dedup_key = payload.get('promoted_from_alert', '')
        task_id = payload['task_id']
        try:
            emitted = register_approval(dict(payload), chat_id)
        except Exception as e:  # noqa: BLE001
            log(f'register failed for {task_id}: {type(e).__name__}: {e}', 'ERROR')
            _emit_self_failure(
                message=(
                    f'{HEALER_SOURCE} failed to register a promoted '
                    f'approval_request ({task_id}): {type(e).__name__}: {e}.'
                ),
                suggested_action=(
                    'Check ~/agents/logs/heal-unregistered-approval.log; the '
                    'stranded direction-ask is still only in larry-alerts.'
                ),
            )
            continue
        # Record the promotion regardless of the chain_event upsert result:
        # add_pending already registered it in Beacon's state, and the
        # event_id is deterministic over the source ts, so a retry would
        # upsert the same row. Recording prevents a duplicate pending entry on
        # the next tick if Supabase was briefly down.
        promoted[dedup_key] = datetime.now(timezone.utc).isoformat()
        promoted_count += 1
        log(f'promoted {task_id} (tab-write={"ok" if emitted else "deferred"}) '
            f'from alert key={dedup_key!r}',
            'WARN' if not emitted else 'INFO')

    save_promoted(promoted)
    log(f'done: promoted {promoted_count} direction-ask(s) onto the tab')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
