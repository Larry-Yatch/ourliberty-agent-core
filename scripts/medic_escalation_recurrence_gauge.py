#!/usr/bin/env python3
"""medic_escalation_recurrence_gauge.py — "is a Medic escalation recurring
often enough to be worth collapsing into one board card?" gauge.

PURPOSE
-------
A deterministic, self-firing oneshot (systemd timer, ~daily) that DMs Larry
when a single Medic *escalation* fingerprint keeps re-paging — the signal that
the parked "recurring-escalation → one self-retracting board card" fan-out
(spec: agents/beacon/specs/medic-escalation-recurrence-fanout.md, a Slice 9
reconciler extension) is now worth building. It is the *un-park trigger* for
that parked build: the fan-out stays a spec until a real recurring signal
appears, and this gauge is what notices.

This gauge does NOT build the fan-out and does NOT propose a board card itself.
It only reads one ledger + its own state file and rings a bell. The fan-out
(the actual collapse-N-pages-into-one-card behavior) is a separate build Larry
authorizes when this fires.

SIGNAL SOURCE
-------------
The Medic handled-ledger, one JSON object per line:
    ~/agents/state/medic-handled-ledger.jsonl
Each row Medic appends: {ts, fingerprint, source, subject, classification,
outcome, attempt, notes}. We read only outcome == 'escalated' rows (the pages
that reached Larry) and group them by fingerprint.

THE FIRING RULE (and why each clause is necessary)
--------------------------------------------------
FIRE for a fingerprint when, over the recent window:
    count(escalated, this fingerprint, last WINDOW_DAYS) >= MIN_RECUR
    AND  most-recent escalation of it is within FRESH_DAYS

  - COUNT-over-days (not the 2h-recency RECUR gate the sibling
    medic_proposal_reconcile.py uses for not-graduated findings): escalations
    recur on a slow, ~daily cadence, so a fast recency gate would never see
    three of them "at once". Durability is proven by count across days, not by
    two hits inside two hours.
  - MIN_RECUR (3): a one-off must NEVER trigger the fan-out, and two could be
    coincidence. Three distinct escalations of the *same* fingerprint in a week
    is a genuine recurring pager, not noise.
  - FRESH_DAYS (2): the freshness clause is the whole reason this gauge exists
    rather than a raw 2-week count. On 2026-07-09 the two fingerprints that had
    ~16x and ~7x over a trailing fortnight had ALREADY STOPPED (last fired
    2026-06-27 / 2026-06-30) — a dying burst still fat inside a 14d window. A
    count gate alone would have fired on a problem that was already fixed.
    Requiring the most-recent hit to be fresh distinguishes a live recurrer
    from a trailing-edge corpse.

Plus a self-managed PER-FINGERPRINT cooldown (FIRE_COOLDOWN_SEC) so a
persistently-hot fingerprint re-DMs at most every few days, never every tick;
and a self-clear: a fingerprint that drops back below threshold (or goes stale)
is removed from state, so a genuine *later* recurrence can fire again. A kill
switch (~/agents/healers.disabled) hard-disables the gauge.

This gauge only READS the ledger + its own state file; the one file it writes is
its state. No dispatch, no board card, no config change — it is a readiness
SIGNAL, exactly like mirror_queue_wait_gauge / readiness_trip_wire.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# -------------------- paths (resolved at call time, honor sandbox) --------------------

def agents_root() -> Path:
    """Agents runtime root. Honors OURLIBERTY_AGENTS_ROOT (test bootstrap /
    sandbox); defaults to ~/agents. Resolved at call time so a test redirect
    lands rather than being frozen at import."""
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def ledger_path() -> Path:
    return agents_root() / 'state' / 'medic-handled-ledger.jsonl'


def state_file() -> Path:
    return agents_root() / 'state' / 'medic-escalation-recurrence-gauge.json'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def log_file() -> Path:
    return agents_root() / 'logs' / 'medic-escalation-recurrence-gauge.log'


# -------------------- thresholds (module constants, env-overridable) --------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log(f'{name}={raw!r} is not an integer; using default {default}', 'WARN')
        return default


# Lookback window (days) over which escalations are counted per fingerprint. 7d
# matches the ~daily escalation cadence. Env: OURLIBERTY_MEDIC_RECUR_WINDOW_DAYS.
WINDOW_DAYS = _env_int('OURLIBERTY_MEDIC_RECUR_WINDOW_DAYS', 7)

# A fingerprint must have escalated at least this many times in the window to be
# "recurring". 3 => a one-off (1) and a coincidence (2) never fire. Env:
# OURLIBERTY_MEDIC_RECUR_MIN_COUNT.
MIN_RECUR = _env_int('OURLIBERTY_MEDIC_RECUR_MIN_COUNT', 3)

# The most-recent escalation of a qualifying fingerprint must be within this many
# days, or it's a dying/dead burst (already root-caused) and must NOT fire. This
# is the clause that separates a live recurrer from a trailing-edge corpse. Env:
# OURLIBERTY_MEDIC_RECUR_FRESH_DAYS.
FRESH_DAYS = _env_int('OURLIBERTY_MEDIC_RECUR_FRESH_DAYS', 2)

# Self-managed per-fingerprint cooldown (seconds) between fires. 7 days default —
# a still-hot fingerprint re-DMs at most weekly. Env:
# OURLIBERTY_MEDIC_RECUR_COOLDOWN_SEC.
FIRE_COOLDOWN_SEC = _env_int('OURLIBERTY_MEDIC_RECUR_COOLDOWN_SEC', 7 * 24 * 3600)

ALERT_SOURCE = 'medic-escalation-recurrence-gauge'
ALERT_SUBJECT = 'medic-escalation-fanout-readiness'

# Where the parked fan-out spec lives (named in the DM so the reader can un-park
# it immediately).
FANOUT_SPEC = 'agents/beacon/specs/medic-escalation-recurrence-fanout.md'


# -------------------- logging --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, file=sys.stderr, flush=True)
    try:
        lf = log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


# -------------------- kill switch + state --------------------

def kill_switch_active() -> bool:
    return kill_switch().exists()


def load_state() -> dict[str, Any]:
    """State dict: {'fired': {fingerprint: last_fired_iso}}. Missing/corrupt =>
    fresh empty (safe direction: re-baseline rather than crash)."""
    try:
        data = json.loads(state_file().read_text(encoding='utf-8'))
        if isinstance(data, dict):
            data.setdefault('fired', {})
            if not isinstance(data['fired'], dict):
                data['fired'] = {}
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {'fired': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        sf = state_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(state, indent=2, sort_keys=True), encoding='utf-8')
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw)
    # A date-only ts ('2026-07-09') parses to naive midnight, silently backdating
    # the escalation by up to a day and distorting the freshness gate near its
    # boundary. Require a time-of-day component ('T' or space separator) — the
    # Medic ledger always writes full ISO, so this only rejects malformed rows.
    if 'T' not in s and ' ' not in s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# -------------------- sampling --------------------

def load_escalations(now: Optional[datetime] = None) -> dict[str, list[datetime]]:
    """Return {fingerprint: [escalation timestamps within WINDOW_DAYS]} from the
    Medic ledger. Only outcome == 'escalated' rows with a parseable ts and a
    non-empty fingerprint are counted. Malformed lines are skipped (never crash
    the gauge on a partial write). Exact-duplicate timestamps for a fingerprint
    are de-duplicated (a Medic append-retry / replayed row is one incident, not
    two) so the count gate reflects distinct escalations, not raw rows."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    seen: dict[str, set[datetime]] = {}
    lp = ledger_path()
    try:
        raw = lp.read_text(encoding='utf-8')
    except OSError:
        return {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get('outcome') != 'escalated':
            continue
        fp = row.get('fingerprint')
        if not isinstance(fp, str) or not fp.strip():
            continue
        ts = _parse_iso(row.get('ts'))
        if ts is None or ts < cutoff:
            continue
        seen.setdefault(fp, set()).add(ts)
    return {fp: sorted(times) for fp, times in seen.items()}


def qualifying(escalations: dict[str, list[datetime]],
               now: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """Fingerprints that meet BOTH the count and freshness gates.

    Returns {fingerprint: {'count': n, 'last': datetime, 'first': datetime}}
    for each fingerprint with count >= MIN_RECUR AND most-recent within
    FRESH_DAYS. A fingerprint with a fat count but a stale most-recent (a dying
    burst) is deliberately excluded."""
    now = now or datetime.now(timezone.utc)
    fresh_cutoff = now - timedelta(days=FRESH_DAYS)
    out: dict[str, dict[str, Any]] = {}
    for fp, times in escalations.items():
        if len(times) < MIN_RECUR:
            continue
        last = max(times)
        if last < fresh_cutoff:
            continue  # recurring historically, but the burst is dead — not live
        out[fp] = {'count': len(times), 'last': last, 'first': min(times)}
    return out


# -------------------- evaluation --------------------

def evaluate(escalations: dict[str, list[datetime]], state: dict[str, Any],
             now: Optional[datetime] = None
             ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Decide which qualifying fingerprints to fire on this tick, and compute the
    next `fired` state.

    Returns (to_fire, new_fired) where:
      - to_fire: {fingerprint: meta} for fingerprints that qualify AND are not in
        cooldown — the ones this DM should name. Empty => no fire.
      - new_fired: the next {'fired': {fp: iso}} map — qualifying fingerprints in
        cooldown are RETAINED (stamp preserved); non-qualifying fingerprints are
        DROPPED (self-clear so a later recurrence can re-fire). Callers stamp
        to_fire entries with `now` only after a successful DM.
    """
    now = now or datetime.now(timezone.utc)
    quals = qualifying(escalations, now=now)
    prior = state.get('fired', {})
    if not isinstance(prior, dict):
        prior = {}

    new_fired: dict[str, str] = {}
    to_fire: dict[str, dict[str, Any]] = {}
    for fp, meta in quals.items():
        last_fired = _parse_iso(prior.get(fp))
        if last_fired is not None and (now - last_fired).total_seconds() < FIRE_COOLDOWN_SEC:
            new_fired[fp] = prior[fp]  # still in cooldown — keep the stamp, don't re-DM
            continue
        to_fire[fp] = meta
    # non-qualifying fingerprints simply fall out of new_fired (self-clear).
    return to_fire, {'fired': new_fired}


def _render_alert(fp: str, meta: dict[str, Any],
                  now: datetime) -> tuple[str, str]:
    age_h = (now - meta['last']).total_seconds() / 3600.0
    message = (
        f'A recurring Medic escalation is still paging — worth collapsing into '
        f'one self-retracting board card instead of N repeat pages.\n'
        f'\n'
        f'Fingerprint: {fp}\n'
        f'  {meta["count"]}x escalated in the last {WINDOW_DAYS}d '
        f'(>= {MIN_RECUR}), most recent {age_h:.0f}h ago (<= {FRESH_DAYS}d).\n'
        f'\n'
        f'This is the un-park trigger for the parked "recurring-escalation -> one '
        f'card" fan-out (a Slice 9 reconciler extension). The build is spec\'d and '
        f'ship-ready; it was parked because the signal had gone quiet. This '
        f'fingerprint is live again now. This gauge stays silent below threshold '
        f'and will not re-fire for this fingerprint for '
        f'{FIRE_COOLDOWN_SEC / (24 * 3600.0):.0f} days.'
    )
    suggested = (
        f'Decide whether to un-park + build the medic-escalation fan-out '
        f'(spec: {FANOUT_SPEC}). Alternatively, root-causing this specific '
        f'failure directly may be faster than the general fan-out. No automatic '
        f'action is taken; this is a readiness signal only.'
    )
    return message, suggested


def fire(fp: str, meta: dict[str, Any], now: datetime) -> bool:
    """Emit ONE readiness DM for a single fingerprint via larry_alerts. Returns
    True on append, False on cooldown/error. Never raises.

    The subject is per-fingerprint (`ALERT_SUBJECT:<fp>`) so larry_alerts'
    dedup/cooldown bucket (keyed on `source:subject`) is independent per
    fingerprint — a constant subject would collapse every fingerprint into one
    shared cooldown bucket, silently swallowing a newly-qualifying fingerprint
    whenever a sibling kept the bucket warm."""
    message, suggested = _render_alert(fp, meta, now)
    try:
        import larry_alerts
        return larry_alerts.append_alert(
            source=ALERT_SOURCE,
            severity='warning',
            message=message,
            subject=f'{ALERT_SUBJECT}:{fp}',
            suggested_action=suggested,
        )
    except Exception as e:  # noqa: BLE001
        log(f'fire: append_alert failed for {fp}: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- tick orchestration --------------------

def run_once(now: Optional[datetime] = None, dry_run: bool = False,
             persist: bool = True) -> dict[str, Any]:
    """One tick: load escalations → evaluate → (maybe) fire.

    Args:
        now: time anchor (tests pass a fixed time).
        dry_run: compute + decide but NEVER emit and NEVER advance state stamps.
        persist: write the state file at the end (tests may suppress).

    Returns {qualifying, to_fire, fired, dry_run}.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        'qualifying': {}, 'to_fire': [], 'fired': False, 'dry_run': dry_run,
    }

    if kill_switch_active():
        result['reason'] = f'kill-switch active at {kill_switch()}; no-op'
        log(result['reason'])
        return result

    state = load_state()
    escalations = load_escalations(now=now)
    to_fire, new_state = evaluate(escalations, state, now=now)
    result['qualifying'] = {fp: m['count']
                            for fp, m in qualifying(escalations, now=now).items()}
    result['to_fire'] = sorted(to_fire)

    if to_fire and not dry_run:
        stamp = now.isoformat()
        fired_fps: list[str] = []
        for fp in sorted(to_fire):
            if fire(fp, to_fire[fp], now):
                # stamp only on a successful append, per-fingerprint, so a
                # suppressed sibling never advances (or blocks) another's stamp.
                new_state['fired'][fp] = stamp
                fired_fps.append(fp)
            else:
                log(f'medic-escalation-recurrence gauge: append_alert suppressed '
                    f'for {fp} (cooldown or write error); stamp NOT advanced',
                    'WARN')
        result['fired'] = bool(fired_fps)
        if fired_fps:
            log(f'FIRED medic-escalation-recurrence gauge for '
                f'{len(fired_fps)} fingerprint(s): {fired_fps}')
    else:
        log(f'tick: dry_run={dry_run} to_fire={sorted(to_fire)} '
            f'qualifying={result["qualifying"]}')

    if persist and not dry_run:
        save_state(new_state)
    return result


def _status_text(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    state = load_state()
    escalations = load_escalations(now=now)
    quals = qualifying(escalations, now=now)
    to_fire, _ = evaluate(escalations, state, now=now)
    lines = [
        'medic-escalation-recurrence-gauge status',
        f'  window            : last {WINDOW_DAYS}d',
        f'  min recurrence    : >= {MIN_RECUR} escalations',
        f'  freshness         : most-recent <= {FRESH_DAYS}d',
        f'  distinct escalated: {len(escalations)} fingerprint(s) in window',
        '',
        'qualifying (count + fresh):',
    ]
    if quals:
        for fp in sorted(quals, key=lambda k: -quals[k]['count']):
            m = quals[fp]
            age_h = (now - m['last']).total_seconds() / 3600.0
            lines.append(f'  {m["count"]}x  last={age_h:.0f}h ago  {fp}')
    else:
        lines.append('  (none)')
    lines += [
        '',
        f'would_fire: {sorted(to_fire)}',
        f'fired-state: {state.get("fired", {})}',
    ]
    return '\n'.join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Medic escalation-recurrence gauge — the un-park trigger for '
                    'the recurring-escalation fan-out.')
    parser.add_argument('--once', action='store_true',
                        help='run one tick (default action).')
    parser.add_argument('--dry-run', action='store_true',
                        help='decide but never DM or advance state.')
    parser.add_argument('--status', action='store_true',
                        help='print current window + would-fire, no side effects.')
    args = parser.parse_args(argv)

    if args.status:
        print(_status_text())
        return 0

    result = run_once(dry_run=args.dry_run)
    log(f'result: to_fire={result["to_fire"]} fired={result["fired"]} '
        f'qualifying={result["qualifying"]}')
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
