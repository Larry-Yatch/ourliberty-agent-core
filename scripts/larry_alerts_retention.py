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

  CONCURRENCY (PR-E2 #16): the live file is appended by ~20 independent
  processes (watchdog/sentinel/healers/...) via larry_alerts.append_alert. The
  whole read-snapshot → archive → rewrite → offset-decrement sequence runs under
  an exclusive advisory flock on <alerts-file>.lock; every appender takes the
  SAME lock for its append. So an append is either fully inside the snapshot
  (kept) or strictly after the os.replace (lands in the rewritten file) — never
  lost to the read-then-rewrite window.

  CRASH-ATOMICITY (PR-E2 #8): the file rewrite (step 2) and the offset
  decrements (step 3) are two separate disk operations; a crash between them
  would otherwise strand alerts (file shifted, offsets stale → consumers skip).
  Before the rewrite we write a JOURNAL (<alerts-file>.retention.journal)
  recording the pre-rewrite inode, the line count to drop, and the ABSOLUTE
  target offsets. On the next tick, _recover_pending (under the same lock)
  completes any interrupted pass idempotently:
    * if the live inode still equals the journal's pre_inode the rewrite did NOT
      happen → drop the recorded leading-line count from the CURRENT file
      (preserving any alerts appended after the crash, which sit at the tail);
    * otherwise the rewrite already landed (inode swapped) → only re-assert the
      offsets.
  Offsets are written as absolute targets, so replaying them is a no-op. The
  archive append happens before the journal; a crash in that sub-second window
  leaves at most a duplicate copy of already-delivered lines in the cold dated
  archive (never a delivery loss), de-duplicated by nothing but harmless.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_io  # noqa: E402  (shared durable-write helper, PR-E #366)
import file_lock  # noqa: E402  (shared advisory flock, PR-E2 #16)

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
    """Durably + atomically persist a line-index offset (unique tmp + fsync +
    rename, via atomic_io). Never negative. Writing an absolute target value is
    idempotent, which is what makes crash recovery (_recover_pending) safe to
    replay."""
    atomic_io.atomic_write_text(path, str(max(0, int(offset))))


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
        # cursor file so load/save (and the lock derived from it) land in the
        # right place.
        ces.LOG_CURSORS_FILE = cursors_file
        try:
            inode = alerts_file.stat().st_ino
        except OSError:
            inode = 0
        fp = ces._file_fingerprint(alerts_file)
        # Read-modify-write the SHARED cursors file under the shipper's own lock,
        # so a concurrent shipper drain can't slip a save between our load and
        # save and have its advanced offsets clobbered by our stale snapshot
        # (audit M3). We mutate only our own key; other keys are preserved.
        with ces.log_cursors_transaction() as cursors:
            cursors[key] = ces.FileCursor(inode=inode, offset=0, fp_sha=fp)
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
    """Rewrite `path` with exactly `lines` via a unique tmp + fsync + os.replace
    (atomic + durable on the same filesystem, via atomic_io). The rename swaps
    the inode, which both the shipper's rotation detection AND crash recovery
    (_recover_pending's pre_inode check) key on."""
    atomic_io.atomic_write_text(path, ''.join(lines))


# -------------------- crash-recovery journal (PR-E2 #8) --------------------

def _journal_path(alerts_file: Path) -> Path:
    """Intent-journal sidecar for the alerts file. Present ⇔ a rewrite/offset
    transaction was started but not yet confirmed complete."""
    return alerts_file.parent / (alerts_file.name + '.retention.journal')


def _write_journal(journal_path: Path, payload: dict[str, Any]) -> None:
    """Durably write the intent journal (fsync + atomic rename). This is the
    commit point: once it is on disk the transition WILL complete, either inline
    below or via _recover_pending on the next tick."""
    atomic_io.atomic_write_json(journal_path, payload)


def _read_journal(journal_path: Path) -> Optional[dict[str, Any]]:
    """Return the pending-transaction journal, or None if absent/unreadable.

    A corrupt journal (truncated by a crash mid-write — though atomic_io makes
    that nearly impossible) is treated as absent: we cannot safely act on a
    journal we can't parse, and the worst case of ignoring it is the original
    pre-PR-E2 behaviour (offsets recomputed from current state next tick)."""
    try:
        data = json.loads(journal_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _clear_journal(journal_path: Path) -> None:
    """Remove the journal — transaction confirmed complete. Best-effort: a
    leftover journal is replayed idempotently next tick, so a failed unlink is
    not fatal."""
    try:
        journal_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f'could not remove retention journal {journal_path} '
            f'({type(e).__name__}); will replay (idempotent) next tick', 'WARN')


def _recover_pending(
    journal_path: Path,
    alerts_file: Path,
    beacon_offset_file: Path,
    medic_offset_file: Path,
    shipper_cursors_file: Path,
    counts: dict[str, Any],
) -> None:
    """Complete an interrupted prior pass, idempotently. MUST run under the
    exclusive lock (so the file is stable) before a fresh tick computes anything.

    The journal records the pre-rewrite inode, the leading-line count to drop,
    and the absolute target offsets. We determine where the prior pass died from
    the live file's inode (robust against alerts appended after the crash, which
    only ever land at the tail):

      * inode == pre_inode → the os.replace never happened. Re-derive survivors
        by dropping `safe_cut` leading lines from the CURRENT file (this keeps
        any post-crash appends) and rewrite.
      * inode != pre_inode → the rewrite already landed. Leave the file alone.

    Then write the absolute target offsets (idempotent), refresh the shipper
    cursor, and clear the journal.
    """
    journal = _read_journal(journal_path)
    if journal is None:
        return

    safe_cut = int(journal.get('safe_cut', 0))
    target_beacon = int(journal.get('target_beacon_offset', 0))
    target_medic = int(journal.get('target_medic_offset', 0))
    pre_inode = journal.get('pre_inode')

    log('found retention journal from an interrupted prior pass; completing it '
        f'(safe_cut={safe_cut}, target beacon={target_beacon}, '
        f'medic={target_medic})', 'WARN')

    if not alerts_file.exists():
        log('alerts file missing during recovery; abandoning journal '
            '(nothing safe to complete)', 'WARN')
        _clear_journal(journal_path)
        return

    try:
        cur_inode = alerts_file.stat().st_ino
    except OSError:
        cur_inode = None

    if pre_inode is not None and cur_inode == pre_inode:
        # The rewrite did NOT happen before the crash. Drop the recorded leading
        # lines from the CURRENT file so any post-crash appends (at the tail) are
        # preserved.
        with open(alerts_file, encoding='utf-8') as fh:
            cur = fh.readlines()
        if not 0 <= safe_cut <= len(cur):
            # safe_cut should always be ≤ the original line count ≤ the current
            # count (appends only grow the file). An out-of-range value means a
            # corrupt/garbage journal; refuse to rewrite rather than risk wiping
            # survivors, and abandon the journal for manual inspection.
            log(f'recovery: journal safe_cut={safe_cut} out of range for '
                f'{len(cur)} current line(s); abandoning journal WITHOUT rewrite '
                '(manual check advised)', 'ERROR')
            _clear_journal(journal_path)
            return
        _atomic_rewrite(alerts_file, cur[safe_cut:])
        log(f'recovery: rewrote live file, dropped {safe_cut} leading line(s), '
            f'kept {len(cur) - safe_cut}')
    else:
        log('recovery: live file already rewritten (inode changed); completing '
            'offset decrement only')

    _write_offset(beacon_offset_file, target_beacon)
    _write_offset(medic_offset_file, target_medic)
    counts['cursor_refreshed'] = _refresh_shipper_cursor(
        shipper_cursors_file, SHIPPER_CURSOR_KEY, alerts_file,
    )
    _clear_journal(journal_path)
    counts['recovered'] = True
    log(f'recovery complete: beacon→{target_beacon}, medic→{target_medic}')


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
        'recovered': False,
    }

    lock_path = file_lock.sidecar_lock_path(alerts_file)
    journal_path = _journal_path(alerts_file)

    # The whole read→archive→rewrite→offset transaction runs under an exclusive
    # advisory flock that every appender (larry_alerts.append_alert) also takes,
    # so a concurrent append can never be lost to the read-then-rewrite window
    # (PR-E2 #16).
    with file_lock.exclusive_lock(lock_path):
        # 0. Finish any prior pass that crashed mid-transaction BEFORE computing
        #    a fresh tick (under the same lock, so the file is stable). Skipped
        #    in dry-run, which must not mutate state (PR-E2 #8).
        if not dry_run:
            _recover_pending(
                journal_path, alerts_file, beacon_offset_file,
                medic_offset_file, shipper_cursors_file, counts,
            )
        elif _read_journal(journal_path) is not None:
            log('DRY-RUN: a retention journal is pending (a prior pass was '
                'interrupted); a real run would complete it first', 'WARN')

        if not alerts_file.exists():
            log('alerts file does not exist; nothing to do')
            log('tick: ' + ('DRY-RUN ' if dry_run else '')
                + ' '.join(f'{k}={v}' for k, v in counts.items()))
            return counts

        with open(alerts_file, encoding='utf-8') as fh:
            lines = fh.readlines()
            try:
                pre_inode = os.fstat(fh.fileno()).st_ino
            except OSError:
                pre_inode = None
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

        if pre_inode is None:
            # The inode is how _recover_pending tells whether a crash landed
            # before or after the rewrite. Without it we cannot journal a
            # crash-safe transaction, so skip this destructive tick entirely
            # (no archive, no rewrite, offsets untouched) and retry next tick —
            # strictly safer than rewriting un-recoverably.
            log('could not determine alerts-file inode; skipping rewrite this '
                'tick to preserve crash-safe recovery (will retry next tick)',
                'ERROR')
            log('tick: ' + ' '.join(f'{k}={v}' for k, v in counts.items()))
            return counts

        target_beacon = beacon_offset - safe_cut
        target_medic = medic_offset - safe_cut

        # 1. Archive FIRST (the rotation is reversible from the dated archive).
        #    Before the journal: a crash in this sub-second window leaves no
        #    journal, so the next tick recomputes the identical pass — at worst a
        #    duplicate copy of already-delivered lines in the cold archive.
        archive_path = _archive_lines(to_archive, now, archive_dir)
        counts['archived'] = safe_cut

        # 2. COMMIT POINT (PR-E2 #8): journal the intent — pre_inode, the leading
        #    line count to drop, and the ABSOLUTE target offsets — durably. Past
        #    here the transition always completes: inline below, or via
        #    _recover_pending on the next tick if we crash.
        _write_journal(journal_path, {
            'version': 1,
            'created_at': now.isoformat(),
            'pre_inode': pre_inode,
            'safe_cut': safe_cut,
            'target_beacon_offset': target_beacon,
            'target_medic_offset': target_medic,
        })

        # 3. Atomically rewrite the live file with the survivors (inode swaps,
        #    which both rotation detection and recovery key on).
        _atomic_rewrite(alerts_file, survivors)

        # 4. Decrement both line-based offsets to their ABSOLUTE targets (atomic,
        #    clamped ≥ 0). The two consumers' read logic is unchanged; we only
        #    shift their offset VALUES so they keep pointing at the same line.
        _write_offset(beacon_offset_file, target_beacon)
        _write_offset(medic_offset_file, target_medic)

        # 5. Re-sync the chain-shipper byte cursor to the rewritten file.
        counts['cursor_refreshed'] = _refresh_shipper_cursor(
            shipper_cursors_file, SHIPPER_CURSOR_KEY, alerts_file,
        )

        # 6. Transaction complete — drop the journal.
        _clear_journal(journal_path)

        log(f'archived {safe_cut} line(s) → {archive_path}; kept {len(survivors)}; '
            f'beacon {beacon_offset}→{target_beacon}, '
            f'medic {medic_offset}→{target_medic}')
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
