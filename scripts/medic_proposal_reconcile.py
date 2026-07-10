#!/usr/bin/env python3
"""Slice 9 — Medic proposes not-graduated fixes to the board, then self-retracts.

Today, a Medic finding it CANNOT auto-act on — a reversible fix whose action
template Larry has not GRADUATED for hands-free execution — is recorded
outcome='skipped' reason='not-graduated' in the medic ledger and escalated as a
diagnose-only alert. That is alert-noise for durable, work-shaped, GRADUATABLE
work: exactly the class the operator's govern loop wants ON THE BOARD (Larry
approves it -> track record builds -> the template graduates -> Medic auto-acts).

This reconciler converts that noise into ranked board work. It is deterministic
and ledger-driven (no LLM): it reads the medic ledger, proposes a durable capture
(badged 'medic' via the label -> source mapping) for each RECURRING not-graduated
fingerprint, dedups by fingerprint, and SELF-RETRACTS the capture when the
condition clears (the fingerprint stops recurring) or Medic has since acted on it
(graduated). Mirrors scripts/pulse_check_i.py's park_proposals; never crashes a
caller (emit/retract/state I/O are all fail-soft).

Filter = beyond-authority AND durable AND work-shaped:
  * beyond-authority: outcome='skipped', reason='not-graduated' (the graduatable
    class — NOT 'not-permitted' which can never earn autonomy, NOT the transient
    coordination refusals like 'flapping-defer-to-systemd').
  * durable/recurring: the fingerprint produced a not-graduated skip within
    RECUR_WINDOW_H (a one-shot flap that never recurs is never proposed).
  * work-shaped: a concrete (action, target) parsed from the ledger note.

Run on a timer (see systemd/ourliberty-medic-proposal-reconcile.*). Idempotent:
a fingerprint already proposed is skipped; a cleared one is retracted once.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import medic_ledger  # noqa: E402 — sibling module
import emit_capture_impl  # noqa: E402 — sibling module

# The graduatable refusal class this reconciler proposes. Kept a set so the
# proof-case class can widen later (e.g. a never-graduatable 'not-permitted' fix
# is still real work Larry should see — a deliberate follow-up, not this slice).
PROPOSE_REASONS = frozenset({'not-graduated'})

# A fingerprint is "recurring" (propose-eligible) if it produced a qualifying
# skip within this window; "stale" (retract-eligible) if it produced none within
# the (larger) stale window. Stale > recur so a card is proposed fast but
# retracted slow — no thrash on a condition that flaps around the boundary.
RECUR_WINDOW_H = 2.0
STALE_WINDOW_H = 6.0

# Read enough ledger tail to cover STALE_WINDOW_H even on a busy day. A genuinely
# recurring fingerprint re-fires each time Medic re-attempts its live alert (up to
# every 3-min dispatcher tick; in practice closer to hourly), so it always lands
# in this many records — the whole ledger today is well under 1k rows over weeks.
# Absence here therefore means "not recurring" -> safe to retract.
LEDGER_READ_LIMIT = 1000

CAPTURE_LABEL = 'medic-proposal'
CAPTURE_SOURCE = 'agent'  # CAPTURE_ALLOWED_SOURCES; the label carries the 'medic' badge

# retract() dict reasons that are TERMINAL (stop tracking) vs a None return
# (transport error -> unknown -> retry next run).
TERMINAL_RETRACT_REASONS = frozenset(
    {'not-found', 'not-parked', 'not-machine-retractable'}
)

_DEFAULT_STATE_PATH = (
    Path(os.environ.get('OL_AGENTS_ROOT') or (Path.home() / 'agents'))
    / 'state' / 'medic-proposals.json'
)

# "{action} refused for {target}: {reason}"  (see medic_actions._refuse)
_NOTE_RE = re.compile(r'^(?P<action>\S+) refused for (?P<target>.+): (?P<reason>[\w-]+)$')


def _log(msg: str) -> None:
    print(f'[medic-proposal-reconcile] {msg}')


def _parse_note(notes: Any) -> Optional[dict[str, str]]:
    """Parse a refusal note into {action, target, reason}, or None if it is not
    a refusal note in the expected shape."""
    if not isinstance(notes, str):
        return None
    m = _NOTE_RE.match(notes.strip())
    if not m:
        return None
    return {'action': m.group('action'), 'target': m.group('target'),
            'reason': m.group('reason')}


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_h(now: datetime, then: datetime) -> float:
    return (now - then).total_seconds() / 3600.0


def latest_qualifying(records: list[dict], now: datetime) -> dict[str, dict]:
    """fingerprint -> {ts, action, target, subject} for the MOST RECENT
    beyond-authority (skipped + PROPOSE_REASONS) record of each fingerprint.
    Ignores malformed rows and non-qualifying reasons."""
    out: dict[str, dict] = {}
    for r in records or []:
        if not isinstance(r, dict):
            continue
        if r.get('outcome') != 'skipped':
            continue
        parsed = _parse_note(r.get('notes'))
        if not parsed or parsed['reason'] not in PROPOSE_REASONS:
            continue
        fp = r.get('fingerprint')
        if not isinstance(fp, str) or not fp:
            continue
        ts = _parse_ts(r.get('ts'))
        if ts is None:
            continue
        prior = out.get(fp)
        if prior is None or ts > prior['ts']:
            out[fp] = {'ts': ts, 'action': parsed['action'],
                       'target': parsed['target'],
                       'subject': r.get('subject') or ''}
    return out


def _proposal_text(fp: str, meta: dict) -> tuple[str, str]:
    action = meta.get('action') or 'act on'
    target = meta.get('target') or fp
    title = f'Recurring: Medic wants to {action} {target}'
    note = (
        f'Medic keeps hitting a condition it can fix ({action} on {target}) but '
        f'the fix is not graduated for hands-free action, so it escalates instead '
        f'of acting. Approve to let Medic auto-handle this class going forward, or '
        f'delegate to build a durable fix. This card self-retracts if the '
        f'condition clears. (fingerprint {fp})'
    )
    return title, note


def reconcile(
    *,
    now: datetime,
    records: list[dict],
    state: dict[str, dict],
    has_acted: Callable[[str], bool],
    emit: Callable[..., Optional[str]],
    retract: Callable[..., Optional[dict]],
    persist: Optional[Callable[[dict], None]] = None,
) -> dict[str, Any]:
    """Reconcile step (all I/O injected for testability).

    PROPOSE a capture for each recurring not-graduated fingerprint not already
    tracked (and not already acted on by Medic). SELF-RETRACT a tracked capture
    when Medic has since acted on the fingerprint (graduated) or the condition
    cleared (no qualifying skip within STALE_WINDOW_H). Returns the updated state
    plus what changed. Mutates `state` in place (also returned).

    `persist(state)` — if given, called after EACH successful emit/retract so a
    crash mid-pass can never re-propose a card already on the board (the record
    is durable the instant the capture exists). Best-effort; a persist that
    raises would propagate, so callers pass a fail-soft writer."""
    def _persist() -> None:
        if persist is not None:
            persist(state)
    latest = latest_qualifying(records, now)
    proposed: list[str] = []
    retracted: list[tuple[str, str]] = []
    kept: list[str] = []

    # 1) propose newly-recurring fingerprints
    for fp, meta in latest.items():
        if _age_h(now, meta['ts']) > RECUR_WINDOW_H:
            continue  # seen, but not currently recurring
        if state.get(fp, {}).get('capture_id'):
            continue  # already on the board
        if has_acted(fp):
            continue  # Medic already acted (graduated) — nothing to propose
        title, note = _proposal_text(fp, meta)
        cap_id = emit(title=title, note=note)
        if cap_id:
            state[fp] = {'capture_id': cap_id, 'parked_at': now.isoformat(),
                         'title': title}
            proposed.append(fp)
            _persist()  # durable before the next emit — no double-propose on crash
            _log(f'proposed fp={fp} capture_id={cap_id} title={title!r}')
        else:
            _log(f'emit failed for fp={fp}; leaving unproposed (retry next run)')

    # 2) self-retract acted-on / stale tracked fingerprints
    for fp in list(state.keys()):
        cap_id = state.get(fp, {}).get('capture_id')
        if not cap_id:
            state.pop(fp, None)
            continue
        meta = latest.get(fp)
        acted = bool(has_acted(fp))
        stale = (meta is None) or (_age_h(now, meta['ts']) > STALE_WINDOW_H)
        if not (acted or stale):
            kept.append(fp)
            continue
        why = 'medic-acted' if acted else 'condition-cleared'
        resp = retract(cap_id, reason=why)
        if resp is None:
            kept.append(fp)  # transport error — outcome unknown, retry next run
            _log(f'retract unknown (transport) fp={fp}; keeping to retry')
            continue
        if resp.get('retracted') or resp.get('reason') in TERMINAL_RETRACT_REASONS:
            state.pop(fp, None)
            retracted.append((fp, why))
            _persist()  # durable before the next retract
            _log(f'retracted fp={fp} why={why} '
                 f'server={resp.get("reason") or "ok"}')
        else:
            kept.append(fp)  # unexpected non-terminal — keep, do not lose track
            _log(f'retract non-terminal fp={fp} resp={resp}; keeping')

    return {'proposed': proposed, 'retracted': retracted, 'kept': kept,
            'state': state}


def _load_state(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        _log(f'WARN: state read failed ({e}); treating as empty')
        return {}


def _save_state(path: Path, state: dict[str, dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
        tmp.replace(path)
    except OSError as e:
        _log(f'WARN: state write failed ({e})')


def _emit(*, title: str, note: str) -> Optional[str]:
    return emit_capture_impl.emit_capture(
        title=title, note=note, source=CAPTURE_SOURCE, label=CAPTURE_LABEL,
    )


def _noop_emit(*, title: str, note: str) -> Optional[str]:
    _log(f'[dry-run] would propose: {title!r}')
    return None


def _noop_retract(cap_id: str, reason: Optional[str] = None) -> Optional[dict]:
    _log(f'[dry-run] would retract capture_id={cap_id} reason={reason}')
    return None


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description='Medic proposes not-graduated fixes to the board + self-retracts')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would be proposed/retracted; touch nothing')
    ap.add_argument('--state-path', default=str(_DEFAULT_STATE_PATH))
    args = ap.parse_args(argv)

    state_path = Path(args.state_path)
    now = datetime.now(timezone.utc)
    try:
        records = medic_ledger.read_recent(limit=LEDGER_READ_LIMIT)
    except Exception as e:  # noqa: BLE001 — a ledger read must never crash the run
        _log(f'WARN: ledger read failed ({e}); nothing to do')
        return 0

    state = _load_state(state_path)
    # Persist incrementally (fail-soft) after each emit/retract so a crash can't
    # re-propose a card already on the board; plus a final save below.
    persist = None if args.dry_run else (lambda st: _save_state(state_path, st))
    # medic_ledger.has_acted re-parses the whole ledger per call; memoize within
    # the pass so each fingerprint is looked up at most once (preserves has_acted's
    # exact arming semantics — acted/acted-failed — no full-set precompute).
    _acted_cache: dict[str, bool] = {}

    def _has_acted(fp: str) -> bool:
        if fp not in _acted_cache:
            _acted_cache[fp] = medic_ledger.has_acted(fp)
        return _acted_cache[fp]

    result = reconcile(
        now=now, records=records, state=state,
        has_acted=_has_acted,
        emit=_noop_emit if args.dry_run else _emit,
        retract=_noop_retract if args.dry_run else emit_capture_impl.retract_capture,
        persist=persist,
    )
    if not args.dry_run:
        _save_state(state_path, result['state'])

    _log(f'done: proposed={len(result["proposed"])} '
         f'retracted={len(result["retracted"])} tracked={len(result["state"])}'
         + (' [dry-run]' if args.dry_run else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
