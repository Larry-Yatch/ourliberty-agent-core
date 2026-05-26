#!/usr/bin/env python3
"""heal_chain_event_type_audit.py — weekly belt-and-suspenders audit.

Phase E4.4d PR-B. Spec: agents/beacon/specs/e4-4d-system-tab.md § 5.1.

Catches unknown event_types that landed in chain_events despite the
shipper's application-side KNOWN_EVENT_TYPES gate. Sources include:

  - A future push-instrumented writer that bypasses the shipper.
  - Hot-patched daemon code that loosened the allowlist locally.
  - Direct SQL inserts (manual debugging that forgot to be cleaned up).
  - Schema drift if a migration accidentally introduces an event_type
    that the application-side allowlist doesn't yet cover.

Query shape:
  SELECT event_type, COUNT(*)
    FROM chain_events
   WHERE ts > now() - interval '7 days'
   GROUP BY event_type;

Any event_type not in chain_event_shipper.KNOWN_EVENT_TYPES is reported
to Larry with counts so the constant can be updated (or the offending
writer fixed).

Runs every Sunday morning via systemd timer.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-chain-event-type-audit.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-chain-event-type-audit.heartbeat'

# Repo scripts dir on sys.path so we can import chain_event_shipper.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as fh:
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


def _dm_larry(message: str, subject: str, suggested_action: str) -> bool:
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-chain-event-type-audit',
            severity='warning',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


def _connect_supabase():
    """Return a Supabase client or raise with a clear message."""
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — '
            'cannot run audit.'
        )
    from supabase import create_client  # type: ignore
    return create_client(url, key)


def fetch_event_type_counts(client, lookback_days: int = 7) -> dict[str, int]:
    """Query chain_events for event_type counts in the lookback window.

    PostgREST doesn't expose GROUP BY directly; we paginate through rows
    and count in Python. 7d × ~500 events/day = ~3500 rows — well within
    one or two round trips.
    """
    cutoff = (datetime.now(timezone.utc).timestamp()
              - lookback_days * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    counts: dict[str, int] = {}
    page = 0
    page_size = 1000
    while True:
        res = (
            client.table('chain_events')
                  .select('event_type')
                  .gte('ts', cutoff_iso)
                  .range(page * page_size, (page + 1) * page_size - 1)
                  .execute()
        )
        rows = getattr(res, 'data', None) or []
        for row in rows:
            et = row.get('event_type')
            if et:
                counts[et] = counts.get(et, 0) + 1
        if len(rows) < page_size:
            break
        page += 1
    return counts


def find_unknown_types(counts: dict[str, int]) -> dict[str, int]:
    """Filter counts to event_types not in KNOWN_EVENT_TYPES."""
    import chain_event_shipper as ces  # noqa: E402
    return {et: n for et, n in counts.items()
            if et not in ces.KNOWN_EVENT_TYPES}


def main() -> int:
    if kill_switch_active():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    try:
        client = _connect_supabase()
    except Exception as e:
        log(f'cannot connect to Supabase: {e}', 'WARN')
        return 0

    try:
        counts = fetch_event_type_counts(client)
    except Exception as e:
        log(f'query failed: {type(e).__name__}: {e}', 'WARN')
        return 0

    unknown = find_unknown_types(counts)
    log(f'tick: 7d distinct types={len(counts)} unknown={len(unknown)}')

    if not unknown:
        return 0

    body_lines = [
        'Weekly chain_events audit found event_type values not in '
        'KNOWN_EVENT_TYPES:',
        '',
    ]
    for et, n in sorted(unknown.items(), key=lambda kv: -kv[1]):
        body_lines.append(f'  - {et!r} × {n} rows')
    body_lines.extend([
        '',
        'These rows are in chain_events but their event_type would have '
        'been REJECTED by chain_event_shipper. Either a non-shipper writer '
        'bypassed the gate, or KNOWN_EVENT_TYPES needs to be updated.',
    ])
    delivered = _dm_larry(
        message='\n'.join(body_lines),
        subject='chain-event-type-audit',
        suggested_action=(
            'Investigate the writer (recent migrations, hot-patched '
            'daemons, manual INSERTs). If legitimate: add the type to '
            'KNOWN_EVENT_TYPES in scripts/chain_event_shipper.py via a '
            'small Forge PR. If illegitimate: clean up with '
            "DELETE FROM chain_events WHERE event_type IN (...)."
        ),
    )
    log(f'DM dispatched={delivered}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
