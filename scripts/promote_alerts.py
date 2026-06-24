#!/usr/bin/env python3
"""promote_alerts.py — N4 "needs-CEO-attention" promotion rule.

Spec: agents/beacon/specs/approvals-queue-rework.md L6 / node N4.

Per L6, Larry does NOT browse a health view. The escalation stream that Pulse
writes to ~/agents/blackboard/pulse-escalations.json is ops-internal and silent
by default — it lands in chain_events as `escalation` rows and shows on the
Ops/System page, but it never pushes to Larry. This job is the classifier that
decides which of those escalations crosses the "needs CEO attention" bar and
gets PROMOTED into Larry's decision inbox; everything below the bar stays silent.

The bar (conservative by design, Pulse-tunable via config/promotion-rules.json):

  - severity == `severity_bar` (default "critical"), AND
  - NOT self-resolved within one cycle — the escalation has persisted at least
    `self_resolve_window_seconds` (default 600s ≈ one Pulse cycle) since first
    seen, so it didn't clear itself, OR
  - the escalation carries an explicit-for-Larry flag (one of
    `explicit_promotion_fields`), which promotes immediately.

Default is NOT to promote: anything we cannot confirm crosses the bar stays
silent. A critical escalation is held for one cycle before promoting; if it
vanishes from the snapshot in the meantime it is treated as self-resolved and
never reaches Larry.

Surfacing vehicle: a `needs_attention` chain_event (push-emitted via
chain_event_emit.emit_event). The dashboard's NeedsAttentionCard on /live reads
this type. We deliberately do NOT consume larry-alerts.jsonl / sentinel-alerts.jsonl
here — those already DM Larry through the alert queue + Beacon bot, so promoting
them again would double-surface and break existing routing. The escalation
snapshot is the silent stream that needs this gate.

Cross-cycle state lives at ~/agents/state/promotion-probation.json. Idempotent:
once an escalation is promoted it is not re-promoted while it remains in the
snapshot, and a failed surface (Supabase down) leaves it un-promoted so the next
cycle retries.

Runs each Pulse cycle via systemd timer. Stdlib + supabase-py (transitively).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'promote-alerts.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'promote-alerts.heartbeat'
PULSE_ESCALATIONS_JSON = AGENTS_ROOT / 'blackboard' / 'pulse-escalations.json'
STATE_FILE = AGENTS_ROOT / 'state' / 'promotion-probation.json'

# Repo scripts dir on sys.path so we can import sibling modules.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import larry_alerts  # noqa: E402  — reused for the canonical severity vocabulary
import for_larry_signal  # noqa: E402  — the canonical durable for-Larry signal (§5.1)

CONFIG_FILE = _SCRIPTS_DIR.parent / 'config' / 'promotion-rules.json'

# Conservative built-in defaults. Used verbatim when the config file is missing
# or malformed, so a config mishap can never accidentally promote routine noise.
DEFAULT_CONFIG: dict[str, Any] = {
    'promotion_enabled': True,
    'severity_bar': 'critical',
    'self_resolve_window_seconds': 600,
    'dedup_key_fields': ['dedup_identity', 'task_id', 'headline', 'id'],
    'explicit_promotion_fields': ['escalate_to_larry', 'for_larry'],
}

# Reuse larry_alerts' canonical alert-severity vocabulary + ordering rather than
# inventing a parallel enum. larry_alerts.VALID_SEVERITIES == ('warning', 'critical');
# the index is the rank, so 'critical' outranks 'warning'. An unrecognized
# severity has no rank and therefore never meets the bar (conservative).
_SEVERITY_RANK = {sev: idx for idx, sev in enumerate(larry_alerts.VALID_SEVERITIES)}


# -------------------- logging + heartbeat --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
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


# -------------------- config + severity --------------------

def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    """Read promotion-rules.json over the conservative built-in defaults.

    Missing file or malformed JSON → defaults (never raises). Unknown keys in
    the file are ignored; known keys override the default.
    """
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = path.read_text()
    except OSError:
        return cfg
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f'config malformed ({e}); using conservative defaults', 'WARN')
        return cfg
    if not isinstance(data, dict):
        log('config top-level not an object; using defaults', 'WARN')
        return cfg
    for key in DEFAULT_CONFIG:
        if key in data:
            cfg[key] = data[key]
    return cfg


def normalize_severity(raw: Any) -> Optional[str]:
    """Lower-case + trim a severity onto larry_alerts' canonical vocabulary.

    Returns the normalized string (which may be outside the canonical set — the
    rank lookup in `meets_severity_bar` handles unknowns conservatively) or None
    when the input isn't a usable string.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    return s or None


def meets_severity_bar(severity: Optional[str], bar: str) -> bool:
    """True iff `severity` ranks at or above `bar` in the canonical vocabulary.

    Unknown severities (not in larry_alerts.VALID_SEVERITIES) never meet the
    bar — conservative default. An unknown `bar` is treated as 'critical'.
    """
    bar_rank = _SEVERITY_RANK.get(bar, _SEVERITY_RANK.get('critical', 0))
    sev_rank = _SEVERITY_RANK.get(severity or '')
    if sev_rank is None:
        return False
    return sev_rank >= bar_rank


# -------------------- candidate model --------------------

@dataclass
class Candidate:
    """A normalized escalation considered for promotion."""
    dedup_identity: str
    severity: Optional[str]
    task_id: Optional[str]
    ts: str
    headline: Optional[str]
    detail: Optional[str]
    explicit: bool
    raw: dict[str, Any] = field(default_factory=dict)


def _derive_dedup_identity(entry: dict[str, Any], key_fields: list[str]) -> Optional[str]:
    """First non-empty value among `key_fields` (spec § 6: dedup_identity || task_id)."""
    for k in key_fields:
        val = entry.get(k)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if val not in (None, '') and not isinstance(val, (dict, list)):
            return str(val)
    return None


def normalize_entry(entry: dict[str, Any], cfg: dict[str, Any]) -> Optional[Candidate]:
    """Turn one pulse-escalations.json entry into a Candidate, or None if it
    lacks a stable identity (can't be tracked across cycles → skipped)."""
    if not isinstance(entry, dict):
        return None
    dedup = _derive_dedup_identity(entry, cfg['dedup_key_fields'])
    if not dedup:
        return None
    explicit = any(bool(entry.get(f)) for f in cfg['explicit_promotion_fields'])
    ts = entry.get('ts') or entry.get('timestamp') or \
        datetime.now(timezone.utc).isoformat()
    return Candidate(
        dedup_identity=dedup,
        severity=normalize_severity(entry.get('severity')),
        task_id=entry.get('task_id') or entry.get('id'),
        ts=ts,
        headline=entry.get('headline'),
        detail=entry.get('detail'),
        explicit=explicit,
        raw=entry,
    )


def read_candidates(path: Path = PULSE_ESCALATIONS_JSON,
                    cfg: Optional[dict[str, Any]] = None) -> list[Candidate]:
    """Parse the current escalation snapshot into Candidates.

    The snapshot is rewritten by Pulse each cycle; an entry that disappears
    between cycles is, by construction, absent from this list — that absence is
    how `run_cycle` detects self-resolution.
    """
    cfg = cfg or load_config()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get('escalations') if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    out: list[Candidate] = []
    for entry in entries:
        cand = normalize_entry(entry, cfg)
        if cand is not None:
            out.append(cand)
    return out


# -------------------- the classifier (pure) --------------------

PROMOTE = 'promote'
HOLD = 'hold'
SKIP = 'skip'


def _parse_ts(ts: str) -> Optional[datetime]:
    s = (ts or '').strip()
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def decide(candidate: Candidate, *, first_seen: datetime, now: datetime,
           cfg: dict[str, Any]) -> tuple[str, str]:
    """Pure promotion decision for one candidate.

    Returns (decision, reason). Decisions:
      - PROMOTE: surface to Larry now.
      - HOLD:    critical but still inside the self-resolve window; re-evaluate
                 next cycle (don't promote, don't drop).
      - SKIP:    below the bar / disabled — stays silent, ops-internal.
    """
    if not cfg.get('promotion_enabled', True):
        return SKIP, 'promotion-disabled'
    # Explicit-for-Larry escalation bypasses the persistence window.
    if candidate.explicit:
        return PROMOTE, 'explicit-flag'
    if not meets_severity_bar(candidate.severity, cfg['severity_bar']):
        return SKIP, 'below-severity-bar'
    # Critical: promote only if it survived one cycle without self-resolving.
    window = cfg.get('self_resolve_window_seconds',
                     DEFAULT_CONFIG['self_resolve_window_seconds'])
    age_sec = (now - first_seen).total_seconds()
    if age_sec >= window:
        return PROMOTE, 'critical-persisted'
    return HOLD, 'critical-awaiting-window'


# -------------------- state --------------------

def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cands = data.get('candidates')
    return cands if isinstance(cands, dict) else {}


def write_state(candidates: dict[str, Any], path: Path = STATE_FILE) -> None:
    """Atomic write (tmp + rename) of the probation state."""
    payload = {
        'version': 1,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'candidates': candidates,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.rename(path)
    except OSError as e:
        log(f'state write failed: {type(e).__name__}: {e}', 'WARN')


# -------------------- surfacing --------------------

def _default_emit(candidate: Candidate, reason: str) -> bool:
    """Push a `needs_attention` chain_event for one promoted escalation."""
    import chain_event_emit  # noqa: E402  — lazy: avoids supabase import in tests
    payload = {
        'reason': reason,
        'severity': candidate.severity,
        'headline': candidate.headline,
        'detail': candidate.detail,
        'dedup_identity': candidate.dedup_identity,
        'source_event_type': 'escalation',
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return chain_event_emit.emit_event(
        event_type='needs_attention',
        agent='promote-alerts',
        task_id=candidate.task_id,
        payload=payload,
        ts=candidate.ts,
    )


# -------------------- for-Larry signal (§5.1) --------------------

def for_larry_active_records(
    candidates: list[Candidate], new_state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build the active for-Larry records for THIS cycle's promoted escalations.

    A record is active iff its candidate is still in the snapshot AND its state
    entry is `promoted` (newly this cycle, or promoted earlier and still
    present). Keyed under `escalation:<dedup_identity>` so the durable write
    namespaces cleanly against the CLARIFY-exhausted producer. Candidates that
    have left the snapshot are absent here → `sync_prefix` resolves them
    (self-clear, decision d). A below-bar escalation never reaches `promoted`,
    so it contributes no record (alert-toil bar preserved).
    """
    active: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        entry = new_state.get(cand.dedup_identity)
        if not (entry and entry.get('promoted')):
            continue
        raw = cand.raw if isinstance(cand.raw, dict) else {}
        active[for_larry_signal.ESCALATION_KEY_PREFIX + cand.dedup_identity] = {
            'id': cand.dedup_identity,
            'dedup_identity': cand.dedup_identity,
            'task_id': cand.task_id,
            'headline': cand.headline,
            'severity': cand.severity,
            'context': cand.detail or raw.get('context'),
            'suggested_action': raw.get('suggested_action'),
            'ts': entry.get('promoted_ts') or cand.ts,
            'source_kind': 'critical-unhandled',
        }
    return active


# -------------------- cycle --------------------

def run_cycle(*, candidates: Optional[list[Candidate]] = None,
              state: Optional[dict[str, Any]] = None,
              cfg: Optional[dict[str, Any]] = None,
              now: Optional[datetime] = None,
              emit_fn: Callable[[Candidate, str], bool] = _default_emit,
              persist: bool = True) -> dict[str, Any]:
    """Run one promotion cycle. Returns a summary dict for logging/tests.

    Pure-ish: all IO seams (candidates, state, emit_fn, now) are injectable so
    the whole flow is unit-testable without Supabase or the filesystem.
    """
    cfg = cfg or load_config()
    now = now or datetime.now(timezone.utc)
    candidates = read_candidates(cfg=cfg) if candidates is None else candidates
    prior = load_state() if state is None else state

    new_state: dict[str, Any] = {}
    promoted: list[str] = []
    held: list[str] = []
    skipped: list[str] = []

    for cand in candidates:
        key = cand.dedup_identity
        prev = prior.get(key) if isinstance(prior, dict) else None

        # Already promoted and still present → keep, do not re-promote.
        if prev and prev.get('promoted'):
            entry = dict(prev)
            entry['last_seen_ts'] = now.isoformat()
            new_state[key] = entry
            continue

        first_seen_iso = (prev or {}).get('first_seen_ts') or now.isoformat()
        first_seen = _parse_ts(first_seen_iso) or now
        decision, reason = decide(cand, first_seen=first_seen, now=now, cfg=cfg)

        if decision == SKIP:
            # Below the bar: stays ops-internal + silent, not tracked.
            skipped.append(key)
            continue

        entry = {
            'first_seen_ts': first_seen_iso,
            'last_seen_ts': now.isoformat(),
            'severity': cand.severity,
            'task_id': cand.task_id,
            'promoted': False,
        }

        if decision == PROMOTE:
            ok = False
            try:
                ok = emit_fn(cand, reason)
            except Exception as e:  # noqa: BLE001 — surfacing must not crash the cycle
                log(f'emit failed for {key!r}: {type(e).__name__}: {e}', 'WARN')
            if ok:
                entry['promoted'] = True
                entry['promoted_ts'] = now.isoformat()
                entry['promotion_reason'] = reason
                promoted.append(key)
                log(f'PROMOTED {key!r} reason={reason} severity={cand.severity}')
            else:
                # Surfacing failed → leave un-promoted so next cycle retries.
                held.append(key)
                log(f'promote-emit failed for {key!r}; will retry next cycle', 'WARN')
        else:  # HOLD
            held.append(key)

        new_state[key] = entry

    # Candidates absent from `candidates` are simply not carried into new_state
    # → self-resolved (or already handled) escalations drop out silently.
    if persist:
        write_state(new_state)
        # §5.1: the same promotion decision that emitted needs_attention also
        # feeds the durable for-Larry signal. One transaction writes the active
        # promoted records and resolves any escalation: record whose identity
        # has left the snapshot — the resolve-clear rides this gate's existing
        # idempotency (a promoted-still-present key stays active; an absent one
        # self-clears). Best-effort: a signal-write failure must not crash the
        # promotion cycle.
        try:
            for_larry_signal.sync_prefix(
                for_larry_active_records(candidates, new_state),
                for_larry_signal.ESCALATION_KEY_PREFIX,
            )
        except Exception as e:  # noqa: BLE001 — durable signal is best-effort
            log(f'for-larry signal sync failed: {type(e).__name__}: {e}', 'WARN')

    return {
        'promoted': promoted,
        'held': held,
        'skipped': skipped,
        'considered': len(candidates),
    }


def main() -> int:
    if kill_switch_active():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()
    cfg = load_config()
    summary = run_cycle(cfg=cfg)
    log(
        'tick: considered={considered} promoted={p} held={h} skipped={s}'.format(
            considered=summary['considered'],
            p=len(summary['promoted']),
            h=len(summary['held']),
            s=len(summary['skipped']),
        )
    )
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
