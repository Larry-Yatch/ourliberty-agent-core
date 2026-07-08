#!/usr/bin/env python3
"""silence_file_auditor.py — G8 standing check over the alert-silence directory.

WHY: a silence (scripts/larry_alerts.py) is a deliberate, durable suppression —
while it holds, `append_alert` drops the matching fingerprint with NO DM. That is
correct for a Medic-confirmed false positive, but it is *invisible* by design:
if the underlying condition later becomes a REAL recurring signal, the silence
keeps swallowing it forever and the only symptom is silence. A permanent
(`until=None`) silence never expires on its own, so nothing ever re-surfaces it.

WHAT: this standing check lists every `~/agents/state/alert-silenced/*` file with
its key, age, TTL disposition, and the suppressed VOLUME (how many alerts it has
eaten, from the sidecar counter `append_alert` bumps at each drop). It DMs Larry
a digest ONLY for silences worth a look — permanent silences that are both aged
past a floor AND still actively eating alerts (a live signal under a forever
silence), or any silence over a hard volume ceiling. A quiet, expired, or
young silence is logged but never DMs (actionable-only).

Fail-open, timer-driven, stdlib only. Never disrupts anything: on any error it
logs and exits 0. Test-isolatable via OURLIBERTY_AGENTS_ROOT (it reads every
path through larry_alerts, which resolves the root at import).
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import larry_alerts

# A silence is "notable" (DM-worthy) when it is PERMANENT (no self-expiry) AND
# both older than this floor AND still actively eating alerts, OR when ANY
# silence's suppressed volume crosses the hard ceiling. Tunable; the digest
# reports the live numbers so these calibrate after the first fire.
STALE_PERMANENT_AGE_SEC = 30 * 24 * 3600   # 30 days
ACTIVE_SUPPRESS_FLOOR = 1                   # "still eating" = ≥1 drop
HARD_VOLUME_CEILING = 25                    # any silence over this always flags


def _load_silence(path: Path) -> Optional[dict[str, Any]]:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def audit(now: Optional[float] = None) -> list[dict[str, Any]]:
    """List every silence file with key, age, TTL disposition, and suppressed
    volume. Pure/read-only; returns rows sorted by suppressed volume (heaviest
    first). A missing directory yields an empty list."""
    base = now if now is not None else time.time()
    root = larry_alerts.SILENCE_ROOT
    rows: list[dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir()) if root.exists() else []
    except OSError:
        entries = []
    for path in entries:
        if not path.is_file():
            continue
        rec = _load_silence(path)
        # The file's key: prefer the recorded raw key, fall back to the on-disk
        # (safe) name so a corrupt file still appears in the listing.
        key = (rec.get('key') if rec else None) or path.name
        ts = rec.get('ts') if rec else None
        until = rec.get('until') if rec else None
        age_sec: Optional[float] = None
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_sec = base - dt.timestamp()
            except (TypeError, ValueError):
                age_sec = None
        permanent = until is None
        expired = False
        if until is not None:
            try:
                expired = base >= float(until)
            except (TypeError, ValueError):
                expired = False
        count = larry_alerts.silence_suppressed_count(key)
        rows.append({
            'key': key,
            'file': path.name,
            'ts': ts,
            'until': until,
            'permanent': permanent,
            'expired': expired,
            'age_sec': age_sec,
            'suppressed_count': count,
            'corrupt': rec is None,
        })
    rows.sort(key=lambda r: r['suppressed_count'], reverse=True)
    return rows


def _is_notable(row: dict[str, Any]) -> bool:
    if row['suppressed_count'] >= HARD_VOLUME_CEILING:
        return True
    if (row['permanent'] and not row['expired']
            and row['suppressed_count'] >= ACTIVE_SUPPRESS_FLOOR
            and row['age_sec'] is not None
            and row['age_sec'] >= STALE_PERMANENT_AGE_SEC):
        return True
    return False


def _fmt_age(age_sec: Optional[float]) -> str:
    if age_sec is None:
        return 'age?'
    days = age_sec / 86400.0
    if days >= 1:
        return f'{days:.1f}d'
    return f'{age_sec / 3600.0:.1f}h'


def _fmt_row(row: dict[str, Any]) -> str:
    disp = 'permanent' if row['permanent'] else ('expired' if row['expired'] else 'ttl')
    tags = []
    if row['corrupt']:
        tags.append('CORRUPT')
    tag = f" [{','.join(tags)}]" if tags else ''
    return (f"  • {row['key']} — {row['suppressed_count']} suppressed, "
            f"{_fmt_age(row['age_sec'])} old, {disp}{tag}")


def render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return 'silence-audit: no active silences.'
    lines = [f'silence-audit: {len(rows)} silence file(s):']
    lines.extend(_fmt_row(r) for r in rows)
    return '\n'.join(lines)


def _dm(body: str) -> bool:
    try:
        return larry_alerts.append_alert(
            source='auditor', severity='warning', message=body,
            subject='silence-file-audit', route='digest',
        )
    except Exception as e:  # noqa: BLE001 — best-effort delivery
        print(f'[silence-audit] WARN: DM send failed ({e}); body follows:\n{body}')
        return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Audit the alert-silence directory (G8).')
    parser.add_argument('--once', action='store_true',
                        help='Run one standing check (the timer entrypoint).')
    parser.add_argument('--json', action='store_true',
                        help='Print the audit rows as JSON instead of a report.')
    args = parser.parse_args(argv)

    rows = audit()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(render(rows))
    notable = [r for r in rows if _is_notable(r)]
    if notable:
        body = (
            'SILENCE AUDIT — silences still actively eating alerts (a look, not a fire)\n'
            '\n'
            + '\n'.join(_fmt_row(r) for r in notable) +
            '\n\n'
            'A silence that keeps suppressing alerts long after it was written may be '
            'sitting on a signal that turned real. Clear it (larry_alerts unsilence <key>) '
            'if the underlying condition is back, or leave it if still a known false '
            'positive. Thresholds in scripts/silence_file_auditor.py.'
        )
        _dm(body)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail-open: a standing check never disrupts the timer
        print(f'[silence-audit] non-fatal error: {e}')
        raise SystemExit(0)
