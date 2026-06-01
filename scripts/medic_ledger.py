#!/usr/bin/env python3
"""medic_ledger.py — handled-ledger for the Medic operator.

Records what Medic has seen + acted on, keyed by alert *fingerprint*
(source + subject dedup key). PR1 ships escalate-only, so the ledger
only records escalations; PR2 will add action outcomes. The
one-action-per-fingerprint guard that PR2 enforces relies on the
attempt counter recorded here.

Storage: append-only JSONL at ~/agents/state/medic-handled-ledger.jsonl.
One JSON object per line.

Record shape:

    {
      "ts": "<ISO 8601>",
      "fingerprint": "<source>:<subject>",
      "source": "<source>",
      "subject": "<subject or ''>",
      "classification": "reversible|privileged|judgment|unknown",
      "outcome": "escalated|acted|skipped",
      "attempt": <int starting at 1>,
      "notes": "<optional short string>"
    }

Discipline:

  - Stdlib only. Never raises on IO / JSON-decode error -- callers
    fire-and-forget. WARN-logs on every fallback so misconfig surfaces
    in the medic-dispatcher log.
  - Fingerprint stability: same (source, subject) -> same key. None /
    missing subject collapses to an empty-suffix form (`source:`)
    rather than crashing, so unowned-but-classified records still
    round-trip.
  - Attempt counter: callers ask `attempt_count(fingerprint)` BEFORE
    appending so they can record `attempt = prior + 1`. The append
    helper does not auto-increment; that's the dispatcher's call (PR2
    will need to make policy decisions on attempt > N).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
LEDGER_FILE = AGENTS_ROOT / 'state' / 'medic-handled-ledger.jsonl'
LOG_FILE = AGENTS_ROOT / 'logs' / 'medic-dispatcher.log'

VALID_CLASSIFICATIONS = ('reversible', 'privileged', 'judgment', 'unknown')
VALID_OUTCOMES = ('escalated', 'acted', 'skipped')


def _log(level: str, msg: str) -> None:
    """Append a structured line to the shared medic-dispatcher log.

    Never raises; if the log directory is unwritable the line is dropped.
    Matches the shape used by scripts/heal_blocked_inbox_age.py so the
    log is human-greppable alongside the rest of the healer constellation.
    """
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] medic_ledger: {msg}\n')
    except OSError:
        pass


def fingerprint(source: Optional[str], subject: Optional[str]) -> str:
    """Stable dedup key for an alert. Mirrors the shape used by
    larry_alerts cooldown (source + ':' + subject) so the two systems
    stay legible together. Missing source becomes '?' (so we never
    silently collapse distinct sources); missing subject becomes ''.
    """
    src = source if isinstance(source, str) and source else '?'
    subj = subject if isinstance(subject, str) else ''
    return f'{src}:{subj}'


def _iter_records():
    """Yield each parsed record from the ledger file. Malformed lines
    are skipped with a WARN; an unreadable / missing file yields nothing.
    Never raises.
    """
    if not LEDGER_FILE.exists():
        return
    try:
        with open(LEDGER_FILE, encoding='utf-8') as f:
            for idx, raw in enumerate(f):
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    _log('WARN', f'ledger line {idx} malformed; skipping')
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError as e:
        _log('WARN', f'ledger read failed: {e}; treating as empty')
        return


def attempt_count(fp: str) -> int:
    """Number of prior entries recorded for this fingerprint. Returns 0
    on missing / malformed file. Used by the dispatcher to compute the
    next `attempt` value (PR2 will gate on this).
    """
    if not isinstance(fp, str) or not fp:
        return 0
    count = 0
    for rec in _iter_records():
        if rec.get('fingerprint') == fp:
            count += 1
    return count


def append_record(
    source: Optional[str],
    subject: Optional[str],
    classification: str,
    outcome: str,
    attempt: int,
    notes: Optional[str] = None,
) -> bool:
    """Append one ledger entry. Returns True on success, False on any
    failure (full-disk, bad permissions, etc.). Never raises.

    `classification` and `outcome` are normalized to known values; an
    unrecognized value is coerced to 'unknown' / the first valid outcome
    rather than dropped, so the audit trail is preserved even when the
    caller miscoded the row.
    """
    cls = classification if classification in VALID_CLASSIFICATIONS else 'unknown'
    out = outcome if outcome in VALID_OUTCOMES else VALID_OUTCOMES[0]
    if not isinstance(attempt, int) or attempt < 1:
        attempt = 1
    fp = fingerprint(source, subject)
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'fingerprint': fp,
        'source': source if isinstance(source, str) else '',
        'subject': subject if isinstance(subject, str) else '',
        'classification': cls,
        'outcome': out,
        'attempt': attempt,
    }
    if notes:
        record['notes'] = str(notes)[:500]
    try:
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError as e:
        _log('WARN', f'ledger append failed for {fp}: {e}')
        return False
    return True


def read_recent(limit: int = 50) -> list[dict]:
    """Return up to `limit` most-recent ledger records (oldest first
    among the returned slice). Fail safe: missing / malformed file
    yields []. Used by PR4 observability and by ad-hoc inspection.
    """
    if limit <= 0:
        return []
    all_recs: list[dict] = list(_iter_records())
    if not all_recs:
        return []
    return all_recs[-limit:]


if __name__ == '__main__':
    # Tiny CLI for ad-hoc inspection during development.
    if len(sys.argv) >= 2 and sys.argv[1] == 'count' and len(sys.argv) >= 3:
        print(attempt_count(sys.argv[2]))
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == 'recent':
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 10
        for r in read_recent(n):
            print(json.dumps(r, ensure_ascii=False))
        sys.exit(0)
    print('usage: medic_ledger.py {count <fingerprint> | recent [N]}', file=sys.stderr)
    sys.exit(2)
