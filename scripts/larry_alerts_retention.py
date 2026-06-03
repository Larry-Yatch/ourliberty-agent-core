#!/usr/bin/env python3
"""larry_alerts_retention.py — cursor-safe retention on the larry-alerts queue.

~/agents/blackboard/larry-alerts.jsonl is append-only with no retention, so it
grows unbounded (1226 lines / ~770KB on 2026-06-03). This job, on a daily
timer, ARCHIVES the oldest lines to a dated file then atomically rewrites the
live file with the survivors — a true rotation, not a truncate.

Three consumers read this file and MUST NOT be stranded or skipped:

  - beacon_telegram_bot, via larry_alerts.read_offset/write_offset →
    ~/agents/state/beacon-alerts-offset.txt (LINE index: last-delivered line + 1).
  - medic_dispatcher, via ~/agents/state/medic-alerts-offset.txt (same scheme).
  - chain_event_shipper, FileCursor keyed 'larry_alerts' in
    ~/agents/state/chain-event-cursors.json (BYTE offset + inode + first-bytes
    fingerprint; already detects truncation/rotation and absorbs double-inserts
    via deterministic event_id dedup).

CURSOR-SAFE ALGORITHM (the load-bearing guarantee):

  retention_cut = min(days_cut, lines_cut)   # "whichever retains MORE"
      days_cut  = index of the first line whose ts is within retention_days
      lines_cut = max(0, N - min_retained_lines)
  safe_cut = min(retention_cut, beacon_offset, medic_offset)

  safe_cut is the number of leading lines we may remove. Capping at the two
  line-based offsets means we NEVER archive a line at or after a consumer's
  offset — no alert that beacon or medic has not yet delivered can be archived.

  Then, backup-first + atomic tmp+rename:
    1. append lines [0, safe_cut) to ~/agents/blackboard/archive/
       larry-alerts-<UTCdate>.jsonl (create dir if missing).
    2. atomically rewrite the live file with lines [safe_cut, end).
    3. decrement beacon + medic offsets by safe_cut (atomic, clamped ≥ 0).
    4. refresh the chain_event_shipper 'larry_alerts' cursor to offset 0 against
       the rewritten file (see _refresh_shipper_cursor for why this is safe).

  Idempotent: if safe_cut <= 0 (nothing eligible, or a consumer is behind) the
  tick is a no-op — no archive, no rewrite, offsets untouched. Re-running after
  a successful pass is also a no-op because the rewritten file's eligible
  prefix is gone.

  Safe-resume on crash: each step is individually durable. Archive append is
  idempotent-by-content for a given pass only in the sense that a re-run starts
  from the current (already-rotated) file; a crash mid-rewrite leaves either the
  old file (tmp not yet renamed → re-run repeats cleanly) or the new file (rename
  done → offsets/cursor refresh re-run as a near-no-op). The live file is never
  left partially written because the rewrite goes through tmp+rename.

POLICY is config-driven + Pulse-tunable (config/larry-alerts-retention.json),
never hardcoded at the call site. A missing / malformed config, or any
individually invalid field, falls back to conservative defaults and never
raises.

Applies by default (this is a scheduled job). Pass --dry-run to inspect without
writing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'larry-alerts-retention.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts-retention.heartbeat'

ALERTS_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
ARCHIVE_DIR = AGENTS_ROOT / 'blackboard' / 'archive'
BEACON_OFFSET_FILE = AGENTS_ROOT / 'state' / 'beacon-alerts-offset.txt'
MEDIC_OFFSET_FILE = AGENTS_ROOT / 'state' / 'medic-alerts-offset.txt'
SHIPPER_CURSORS_FILE = AGENTS_ROOT / 'state' / 'chain-event-cursors.json'
SHIPPER_CURSOR_KEY = 'larry_alerts'

REPO_ROOT = Path(__file__).resolve().parent.parent
RETENTION_CONFIG = REPO_ROOT / 'config' / 'larry-alerts-retention.json'

DEFAULT_RETENTION_DAYS = 14
DEFAULT_MIN_RETAINED_LINES = 500

# Per-component env kill-switch, consistent with the other healers/daemons
# (OURLIBERTY_MEDIC_ENABLED, OURLIBERTY_CHAIN_SHIPPER_ENABLED). Set to a falsey
# value to disable this one job without touching the blanket healers.disabled.
ENABLE_ENV_VAR = 'OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED'


# -------------------- logging + heartbeat --------------------

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
    """Either the blanket healers.disabled file OR a falsey enable env var
    stops the tick. The env var defaults to enabled (ships active)."""
    if KILL_SWITCH.exists():
        return True
    raw = os.environ.get(ENABLE_ENV_VAR)
    if raw is not None and raw.strip().lower() not in ('1', 'true', 'yes', 'on'):
        return True
    return False


# -------------------- config --------------------

def load_retention_config(
    path: Path = RETENTION_CONFIG,
) -> tuple[int, int]:
    """Return (retention_days, min_retained_lines) from config.

    Window and floor come from config, never hardcoded at the call site. A
    missing / malformed file, or any individually invalid field, falls back to
    the conservative defaults for that field and never raises.
    """
    days = DEFAULT_RETENTION_DAYS
    min_lines = DEFAULT_MIN_RETAINED_LINES
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f'retention config unreadable ({type(e).__name__}); using defaults '
            f'(days={days}, min_lines={min_lines})', 'WARN')
        return days, min_lines

    raw_days = data.get('retention_days')
    if isinstance(raw_days, int) and not isinstance(raw_days, bool) and raw_days > 0:
        days = raw_days
    else:
        log(f'retention_days invalid ({raw_days!r}); using default {days}', 'WARN')

    raw_min = data.get('min_retained_lines')
    if isinstance(raw_min, int) and not isinstance(raw_min, bool) and raw_min >= 0:
        min_lines = raw_min
    else:
        log(f'min_retained_lines invalid ({raw_min!r}); using default '
            f'{min_lines}', 'WARN')

    return days, min_lines


# -------------------- offset helpers (line-index; mirrors larry_alerts) --------------------

def _read_offset(path: Path) -> int:
    """Read a line-index offset. 0 if missing / malformed."""
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or '0')
    except (OSError, ValueError):
        log(f'offset unreadable at {path}; treating as 0', 'WARN')
        return 0


def _write_offset(path: Path, offset: int) -> None:
    """Atomically persist a line-index offset (tmp + rename). Never negative."""
    value = max(0, int(offset))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(str(value))
    tmp.rename(path)


# -------------------- retention math --------------------

def _parse_ts(line: str) -> Optional[datetime]:
    """Parse the 'ts' field of one JSONL line into an aware datetime.

    Returns None if the line is blank, not JSON, has no parseable ts, or the
    ts isn't ISO8601. Callers treat None as "can't prove this line is old".
    """
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict):
        return None
    raw = rec.get('ts')
    if not isinstance(raw, str):
        return None
    s = raw[:-1] + '+00:00' if raw.endswith('Z') else raw
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_retention_cut(
    lines: list[str], retention_days: int, min_retained_lines: int,
    now: datetime,
) -> int:
    """Number of leading lines eligible to drop by POLICY alone (before the
    cursor-safety cap).

    Keep entries newer than retention_days OR the last min_retained_lines,
    WHICHEVER RETAINS MORE → cut at the SMALLER index:

        days_cut  = first index whose ts is within the window (older lines
                    precede it). A line whose ts can't be parsed is treated as
                    NOT-old (can't prove it's expired) → it halts days_cut, so
                    we never archive a line we can't date.
        lines_cut = max(0, N - min_retained_lines)
        retention_cut = min(days_cut, lines_cut)
    """
    n = len(lines)
    cutoff = now - timedelta(days=retention_days)

    days_cut = 0
    for line in lines:
        ts = _parse_ts(line)
        if ts is not None and ts < cutoff:
            days_cut += 1
        else:
            break

    lines_cut = max(0, n - min_retained_lines)
    return min(days_cut, lines_cut)


# -------------------- chain-shipper cursor refresh --------------------

def _refresh_shipper_cursor(cursors_file: Path, key: str, alerts_file: Path) -> bool:
    """Re-sync the chain_event_shipper FileCursor for `key` to the rewritten
    live file so the shipper re-reads it from the start.

    Why offset=0 (full re-read) is the safe choice, not advancing to EOF:
      - The shipper computes a deterministic event_id (sha1 of task_id|type|ts)
        and upserts with ignore_duplicates, so re-reading lines it already
        shipped is absorbed — no duplicate rows.
      - Advancing to EOF would risk SKIPPING any line the shipper hadn't yet
        reached. A full re-read can never skip; the cost is a bounded re-read
        of at most the retained line count (≤ min_retained_lines or a 14d
        window).
      - This is robust even if the shipper overwrites our cursor write during a
        concurrent drain: its own rotation detection (inode change from the
        atomic rename, first-bytes fingerprint change, or offset > new size)
        independently forces the same re-read-from-0 outcome.

    We set {inode: <new inode>, offset: 0, fp_sha: <new fingerprint>} — a clean
    cursor whose offset is 0, so the shipper reads from the top regardless of
    rotation detection. Best-effort: if the shipper module or cursor file isn't
    available, log and continue (the shipper's own rotation detection still
    catches the rewrite). Returns True if the cursor was refreshed.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import chain_event_shipper as ces  # noqa: E402
    except Exception as e:  # noqa: BLE001
        log(f'could not import chain_event_shipper to refresh cursor '
            f'({type(e).__name__}); relying on shipper rotation detection', 'WARN')
        return False

    try:
        # Point the shipper module's constant at OUR (possibly test-overridden)
        # cursor file so load/save land in the right place.
        ces.LOG_CURSORS_FILE = cursors_file
        cursors = ces.load_log_cursors()
        try:
            inode = alerts_file.stat().st_ino
        except OSError:
            inode = 0
        fp = ces._file_fingerprint(alerts_file)
        cursors[key] = ces.FileCursor(inode=inode, offset=0, fp_sha=fp)
        ces.save_log_cursors(cursors)
        log(f'refreshed chain-shipper cursor {key!r}: inode={inode} offset=0 '
            f'(full re-read; dedup absorbs)')
        return True
    except Exception as e:  # noqa: BLE001
        log(f'shipper cursor refresh failed ({type(e).__name__}: {e}); relying '
            f'on shipper rotation detection', 'WARN')
        return False


# -------------------- archive + atomic rewrite --------------------

def _archive_lines(lines: list[str], now: datetime, archive_dir: Path) -> Path:
    """Append `lines` (verbatim, including their newlines) to a dated archive
    file. Append-or-create so multiple passes on the same UTC day accumulate."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime('%Y%m%d')
    path = archive_dir / f'larry-alerts-{stamp}.jsonl'
    with open(path, 'a', encoding='utf-8') as fh:
        fh.writelines(lines)
    return path


def _atomic_rewrite(path: Path, lines: list[str]) -> None:
    """Rewrite `path` with exactly `lines` via tmp + os.replace (atomic on the
    same filesystem). The rename swaps the inode, which the shipper's rotation
    detection keys on."""
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.writelines(lines)
    os.replace(tmp, path)


# -------------------- orchestration --------------------

def run_once(
    *,
    config: Optional[tuple[int, int]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    alerts_file: Path = ALERTS_FILE,
    archive_dir: Path = ARCHIVE_DIR,
    beacon_offset_file: Path = BEACON_OFFSET_FILE,
    medic_offset_file: Path = MEDIC_OFFSET_FILE,
    shipper_cursors_file: Path = SHIPPER_CURSORS_FILE,
) -> dict[str, Any]:
    """Single cursor-safe retention tick. Returns a counts dict. Paths + config
    are injectable for tests; in production they come from the environment."""
    now = now or datetime.now(timezone.utc)
    retention_days, min_retained_lines = (
        config if config is not None else load_retention_config()
    )

    counts: dict[str, Any] = {
        'retention_days': retention_days,
        'min_retained_lines': min_retained_lines,
        'total_lines': 0,
        'beacon_offset': 0,
        'medic_offset': 0,
        'retention_cut': 0,
        'safe_cut': 0,
        'archived': 0,
        'remaining': 0,
        'cursor_refreshed': False,
    }

    if not alerts_file.exists():
        log('alerts file does not exist; nothing to do')
        log('tick: ' + ('DRY-RUN ' if dry_run else '')
            + ' '.join(f'{k}={v}' for k, v in counts.items()))
        return counts

    with open(alerts_file, encoding='utf-8') as fh:
        lines = fh.readlines()
    n = len(lines)
    counts['total_lines'] = n

    beacon_offset = _read_offset(beacon_offset_file)
    medic_offset = _read_offset(medic_offset_file)
    counts['beacon_offset'] = beacon_offset
    counts['medic_offset'] = medic_offset

    retention_cut = compute_retention_cut(
        lines, retention_days, min_retained_lines, now,
    )
    counts['retention_cut'] = retention_cut

    # Cursor-safety cap: never archive a line at or after a line-based
    # consumer's offset. A pending (undelivered) alert can never be archived.
    safe_cut = min(retention_cut, beacon_offset, medic_offset)
    safe_cut = max(0, safe_cut)
    counts['safe_cut'] = safe_cut
    counts['remaining'] = n - safe_cut

    if safe_cut <= 0:
        log(f'no cursor-safe lines to archive (retention_cut={retention_cut}, '
            f'beacon={beacon_offset}, medic={medic_offset}); no-op')
        log('tick: ' + ('DRY-RUN ' if dry_run else '')
            + ' '.join(f'{k}={v}' for k, v in counts.items()))
        return counts

    to_archive = lines[:safe_cut]
    survivors = lines[safe_cut:]

    if dry_run:
        log(f'DRY-RUN would archive {safe_cut} line(s), keep {len(survivors)}')
        log('tick: DRY-RUN '
            + ' '.join(f'{k}={v}' for k, v in counts.items()))
        return counts

    # 1. Archive FIRST (the rotation is reversible from the dated archive).
    archive_path = _archive_lines(to_archive, now, archive_dir)
    counts['archived'] = safe_cut

    # 2. Atomically rewrite the live file with the survivors.
    _atomic_rewrite(alerts_file, survivors)

    # 3. Decrement both line-based offsets by safe_cut (atomic, clamped ≥ 0).
    #    The two consumers' read logic is unchanged; we only shift their
    #    offset VALUES so they keep pointing at the same logical position.
    _write_offset(beacon_offset_file, beacon_offset - safe_cut)
    _write_offset(medic_offset_file, medic_offset - safe_cut)

    # 4. Re-sync the chain-shipper byte cursor to the rewritten file.
    counts['cursor_refreshed'] = _refresh_shipper_cursor(
        shipper_cursors_file, SHIPPER_CURSOR_KEY, alerts_file,
    )

    log(f'archived {safe_cut} line(s) → {archive_path}; kept {len(survivors)}; '
        f'beacon {beacon_offset}→{beacon_offset - safe_cut}, '
        f'medic {medic_offset}→{medic_offset - safe_cut}')
    log('tick: ' + ' '.join(f'{k}={v}' for k, v in counts.items()))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='compute + report but write nothing')
    args = ap.parse_args()

    if kill_switch_active():
        log('kill-switch active (healers.disabled or '
            f'{ENABLE_ENV_VAR} falsey); exiting cleanly')
        return 0
    heartbeat()

    try:
        run_once(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
