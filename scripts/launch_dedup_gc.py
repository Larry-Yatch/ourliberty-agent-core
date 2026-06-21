#!/usr/bin/env python3
"""launch_dedup_gc.py — operational hygiene for the board-Launch duplicate-work
guard (PR #609 follow-up; the housekeeping half of ``launch_dedup_guard.py``).

#609 added a cheap author-time duplicate-work guard at the board-Launch
chokepoint. It parks a de-duplicated launch request under
``<queue>/.deduped/`` and appends each cross-identity deliverable CLAIM to
``<agents>/blackboard/deliverable-claims.jsonl``. Both grow unbounded and the
24h claim window is applied only at READ time, so without retention:

  * a ``.deduped/`` hold that was a FALSE POSITIVE (the work was NOT actually a
    duplicate) sits parked forever, silently orphaned — the launch never builds;
  * the claims ledger accumulates one line per cross-identity marker for the life
    of the system.

This daily job closes both — pure hygiene, not correctness (the #609 alert +
reversible hold are the interim mitigation; this just keeps them tidy):

  (A) ``.deduped/`` REAPER. For each held launch older than the claim window,
      re-evaluate the claim that justified the hold:
        * a FRESH matching claim still exists → the duplicate is genuinely still
          in flight; leave the hold in place (it is correct).
        * NO fresh claim → the justification has expired. RE-SURFACE the request
          (stamp ``gc_resurfaced_at`` + move it back to the queue) so the drain
          re-evaluates it: a false positive then builds normally, while a genuine
          duplicate is caught again — by the drain's own guard if its claim
          reappears, or by the advancer's pre-dispatch already-merged reconcile
          (#609 follow-up #1) / the #610 post-build no-delta bridge.
        * a request ALREADY re-surfaced once (carries ``gc_resurfaced_at``) that
          got re-held → it keeps flapping as a duplicate; ARCHIVE it to
          ``.deduped/.archive/`` and roll it into a single Larry alert (this one
          needs a human decision) rather than re-surface it forever.

  (B) CLAIMS-LEDGER ROTATION. Archive-then-rewrite ``deliverable-claims.jsonl``,
      keeping entries within ``claims_retention_days`` OR the last
      ``claims_min_retained_lines`` (whichever retains MORE), mirroring
      ``larry_alerts_retention``'s retention math. No consumer tracks an offset
      into this ledger (``launch_dedup_guard`` reads the newest lines within a
      recency window statelessly), so no cursor-safety machinery is needed — a
      plain archive-first + atomic rewrite suffices.

POLICY is config-driven (``config/launch-dedup-gc.json``), never hardcoded at the
call site; a missing / malformed config, or any individually invalid field,
falls back to conservative defaults and never raises. Every step is fail-safe:
the job can only re-surface / archive / rotate, never lose a launch request or a
claim irrecoverably (held files move to an archive; rotated claims are archived
before the rewrite).

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

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import atomic_io  # noqa: E402 — shared durable-write helper (PR-E #366)
import launch_dedup_guard as guard  # noqa: E402 — claim ledger path + window + matcher
import launch_queue_drain as drain  # noqa: E402 — queue dir + .deduped/ dirname (single source)

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'launch-dedup-gc.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'launch-dedup-gc.heartbeat'
# Claims-rotation archive shares the existing cold-archive dir.
ARCHIVE_DIR = AGENTS_ROOT / 'blackboard' / 'archive'
# Held-launch archive lives beside the held files so a recovery `mv` is obvious.
DEDUPED_ARCHIVE_DIRNAME = '.archive'

REPO_ROOT = _SCRIPTS_DIR.parent
GC_CONFIG = REPO_ROOT / 'config' / 'launch-dedup-gc.json'

# Per-component env kill-switch, consistent with the other healers/retention jobs
# (OURLIBERTY_LARRY_ALERTS_RETENTION_ENABLED, ...). Set to a falsey value to
# disable just this job without touching the blanket healers.disabled.
ENABLE_ENV_VAR = 'OURLIBERTY_LAUNCH_DEDUP_GC_ENABLED'

# Conservative defaults (config-overridable). The claim window mirrors the
# guard's own read window so the reaper re-surfaces a hold only once its
# justifying claim has aged out of what the drain would still act on.
DEFAULT_CLAIM_WINDOW_HOURS = guard.DEFAULT_CLAIM_WINDOW_SEC // 3600  # 24
DEFAULT_CLAIMS_RETENTION_DAYS = 7
DEFAULT_CLAIMS_MIN_RETAINED_LINES = 500

# Stamp added to a re-surfaced launch entry so a second re-hold is detectable
# (and archived, not re-surfaced forever).
RESURFACED_FIELD = 'gc_resurfaced_at'

_ALERT_SOURCE = 'launch-dedup-gc'


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
    """Either the blanket healers.disabled file OR a falsey enable env var stops
    the tick. The env var defaults to enabled (ships active)."""
    if KILL_SWITCH.exists():
        return True
    raw = os.environ.get(ENABLE_ENV_VAR)
    if raw is not None and raw.strip().lower() not in ('1', 'true', 'yes', 'on'):
        return True
    return False


# -------------------- config --------------------

def load_gc_config(path: Path = GC_CONFIG) -> tuple[int, int, int]:
    """Return (claim_window_hours, claims_retention_days, claims_min_retained_lines).

    All three come from config, never hardcoded at the call site. A missing /
    malformed file, or any individually invalid field, falls back to the
    conservative default for that field and never raises.
    """
    window_h = DEFAULT_CLAIM_WINDOW_HOURS
    days = DEFAULT_CLAIMS_RETENTION_DAYS
    min_lines = DEFAULT_CLAIMS_MIN_RETAINED_LINES
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f'gc config unreadable ({type(e).__name__}); using defaults '
            f'(window_h={window_h}, days={days}, min_lines={min_lines})', 'WARN')
        return window_h, days, min_lines
    if not isinstance(data, dict):
        log(f'gc config is not an object ({type(data).__name__}); using defaults',
            'WARN')
        return window_h, days, min_lines

    def _pos_int(key: str, default: int, *, allow_zero: bool = False) -> int:
        raw = data.get(key)
        ok = isinstance(raw, int) and not isinstance(raw, bool) and (
            raw >= 0 if allow_zero else raw > 0
        )
        if ok:
            return raw
        if raw is not None:
            log(f'{key} invalid ({raw!r}); using default {default}', 'WARN')
        return default

    window_h = _pos_int('claim_window_hours', window_h)
    days = _pos_int('claims_retention_days', days)
    min_lines = _pos_int('claims_min_retained_lines', min_lines, allow_zero=True)
    return window_h, days, min_lines


# -------------------- (A) .deduped/ reaper --------------------

def _deduped_dir(queue_dir: Path) -> Path:
    return queue_dir / drain._DEDUPED_DIRNAME


def _list_held_files(deduped_dir: Path) -> list[Path]:
    """Held `*.json` launch requests directly under `.deduped/`, skipping
    dotfiles and the `.archive/` sub-dir. Oldest first (mtime)."""
    if not deduped_dir.is_dir():
        return []
    files = [
        p for p in deduped_dir.iterdir()
        if p.is_file() and p.suffix == '.json' and not p.name.startswith('.')
    ]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return files


def _held_age_sec(path: Path, now: datetime) -> Optional[float]:
    try:
        return now.timestamp() - path.stat().st_mtime
    except OSError:
        return None


def _archive_held(path: Path) -> Optional[Path]:
    """Move a persistently-flapping held request into `.deduped/.archive/`
    (preserved, recoverable). Returns the new path, or None on failure."""
    archive_dir = path.parent / DEDUPED_ARCHIVE_DIRNAME
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / path.name
        os.replace(path, dest)
        return dest
    except OSError as e:
        log(f'could not archive held launch {path.name}: {e}', 'WARN')
        return None


def _resurface_held(path: Path, queue_dir: Path, now: datetime) -> Optional[Path]:
    """Stamp `gc_resurfaced_at` and move the held request back to the queue dir so
    the drain re-evaluates it. Returns the new queue path, or None on failure.

    Write-then-move: we rewrite the file IN the `.deduped/` dir (atomic) with the
    added stamp, then `os.replace` it into the queue. A crash between the two
    leaves a stamped-but-still-held file, which the next pass re-handles
    idempotently (a stamped held file that is still flapping is archived)."""
    try:
        entry = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'could not read held launch {path.name} to re-surface: {e}', 'WARN')
        return None
    if not isinstance(entry, dict):
        log(f'held launch {path.name} is not a JSON object; leaving in place',
            'WARN')
        return None
    entry[RESURFACED_FIELD] = now.astimezone(timezone.utc).isoformat()
    try:
        atomic_io.atomic_write_json(path, entry)
        dest = queue_dir / path.name
        os.replace(path, dest)
        return dest
    except OSError as e:
        log(f'could not re-surface held launch {path.name}: {e}', 'WARN')
        return None


def reap_deduped(
    *,
    queue_dir: Path,
    now: datetime,
    claim_window_sec: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reap the `.deduped/` hold dir. Returns a counts dict + the list of
    archived held-file names (for the roll-up alert)."""
    deduped_dir = _deduped_dir(queue_dir)
    # Derive the claims-ledger location from the SAME root as queue_dir (its
    # parent blackboard) rather than the module-frozen AGENTS_ROOT, so the claim
    # re-check can never read a DIFFERENT root's (empty) ledger and wrongly
    # re-surface a hold whose justifying duplicate is genuinely still in flight.
    # find_matching_claims derives `<sequences_dir>.parent / claims-file`, so
    # point sequences_dir at <queue_dir.parent>/build-sequences.
    claims_sequences_dir = queue_dir.parent / 'build-sequences'
    counts: dict[str, Any] = {
        'scanned': 0, 'too_fresh': 0, 'still_claimed': 0,
        'resurfaced': 0, 'archived': 0, 'archived_files': [],
    }
    for path in _list_held_files(deduped_dir):
        counts['scanned'] += 1
        age = _held_age_sec(path, now)
        # Give a freshly-held request its full claim window before acting — its
        # justifying claim may still be valid.
        if age is None or age < claim_window_sec:
            counts['too_fresh'] += 1
            continue
        try:
            entry = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f'held launch {path.name} unreadable ({e}); leaving in place',
                'WARN')
            continue
        if not isinstance(entry, dict):
            continue
        # Still actively claimed elsewhere? Then the hold is still correct.
        try:
            claims = guard.find_matching_claims(
                entry, sequences_dir=claims_sequences_dir,
                window_sec=claim_window_sec, now=now,
            )
        except Exception as e:  # noqa: BLE001 — fail safe: treat as no claim
            log(f'claim re-check for held {path.name} raised {type(e).__name__}: '
                f'{e}; treating as no fresh claim', 'WARN')
            claims = []
        if claims:
            counts['still_claimed'] += 1
            log(f'held launch {path.name} still has a fresh matching claim; '
                f'leaving held (duplicate work genuinely in flight)')
            continue
        # Justification expired. Re-surface once; archive a repeat flapper.
        already_resurfaced = bool(entry.get(RESURFACED_FIELD))
        if dry_run:
            action = 'archive' if already_resurfaced else 're-surface'
            log(f'DRY-RUN would {action} held launch {path.name}')
            if already_resurfaced:
                counts['archived'] += 1
                counts['archived_files'].append(path.name)
            else:
                counts['resurfaced'] += 1
            continue
        if already_resurfaced:
            dest = _archive_held(path)
            if dest is not None:
                counts['archived'] += 1
                counts['archived_files'].append(path.name)
                log(f'archived persistently-held launch {path.name} → {dest} '
                    f'(re-surfaced once already; keeps flapping as a duplicate)')
        else:
            dest = _resurface_held(path, queue_dir, now)
            if dest is not None:
                counts['resurfaced'] += 1
                log(f're-surfaced held launch {path.name} → {dest} (claim window '
                    f'expired; the drain will re-evaluate it)')
    return counts


def _alert_archived_holds(archived_files: list[str], deduped_dir: Path) -> None:
    """One roll-up alert when persistently-held launches were archived (these
    need a human decision — they keep being flagged as duplicate). Names the
    one-command recovery. Best-effort; never raises."""
    if not archived_files:
        return
    archive_dir = deduped_dir / DEDUPED_ARCHIVE_DIRNAME
    queue_dir = deduped_dir.parent
    sample = archived_files[0]
    try:
        import larry_alerts  # noqa: E402 — local import keeps the GC import-light
        larry_alerts.append_alert(
            source=_ALERT_SOURCE,
            severity='warning',
            message=(
                f'{len(archived_files)} de-duplicated board-Launch request(s) kept '
                f'flapping as duplicate work and were archived to {archive_dir} '
                f'after a re-surface attempt: {archived_files}. If any was a false '
                f'positive, re-queue it: `mv {archive_dir / sample} '
                f'{queue_dir / sample}`.'
            ),
            subject='launch-dedup-gc-archived',
            route='escalate',
        )
    except Exception as e:  # noqa: BLE001 — alerting is best-effort
        log(f'could not emit archived-holds roll-up alert ({type(e).__name__}: '
            f'{e})', 'WARN')


# -------------------- (B) claims-ledger rotation --------------------
#
# Retention math mirrors larry_alerts_retention.compute_retention_cut (copied,
# not imported, to keep this GC dependency-light): keep entries newer than
# retention_days OR the last min_retained_lines, WHICHEVER RETAINS MORE → cut at
# the SMALLER index. An undated line halts the days-cut so we never archive a
# line we can't prove is old. There are no offset-tracking consumers of this
# ledger, so no cursor-safety cap is needed.

def _parse_ts(line: str) -> Optional[datetime]:
    """Parse the `ts` field of one JSONL line into an aware datetime, or None if
    blank / not JSON / no parseable ISO-8601 ts (caller treats None as 'can't
    prove this line is old')."""
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
    lines: list[str], retention_days: int, min_retained_lines: int, now: datetime,
) -> int:
    """Leading lines eligible to archive: min(days_cut, lines_cut)."""
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


def _archive_claims(lines: list[str], now: datetime, archive_dir: Path) -> Path:
    """Append `lines` (verbatim, incl. newlines) to a dated archive file."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime('%Y%m%d')
    path = archive_dir / f'deliverable-claims-{stamp}.jsonl'
    with open(path, 'a', encoding='utf-8') as fh:
        fh.writelines(lines)
    return path


def rotate_claims(
    *,
    claims_file: Path,
    archive_dir: Path,
    retention_days: int,
    min_retained_lines: int,
    now: datetime,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Archive-then-rewrite the claims ledger. Returns a counts dict. Idempotent:
    a tick with nothing eligible is a clean no-op."""
    counts: dict[str, Any] = {
        'total_lines': 0, 'cut': 0, 'archived': 0, 'remaining': 0,
    }
    if not claims_file.exists():
        return counts
    try:
        with open(claims_file, encoding='utf-8') as fh:
            lines = fh.readlines()
    except OSError as e:
        log(f'claims ledger unreadable ({type(e).__name__}: {e}); skipping '
            f'rotation', 'WARN')
        return counts
    counts['total_lines'] = len(lines)
    cut = compute_retention_cut(lines, retention_days, min_retained_lines, now)
    counts['cut'] = cut
    counts['remaining'] = len(lines) - cut
    if cut <= 0:
        return counts
    if dry_run:
        log(f'DRY-RUN would archive {cut} claim line(s), keep {len(lines) - cut}')
        return counts
    # Archive FIRST (reversible), then atomic rewrite of the survivors.
    archive_path = _archive_claims(lines[:cut], now, archive_dir)
    counts['archived'] = cut
    atomic_io.atomic_write_text(claims_file, ''.join(lines[cut:]))
    log(f'rotated claims ledger: archived {cut} line(s) → {archive_path}; kept '
        f'{len(lines) - cut}')
    return counts


# -------------------- orchestration --------------------

def run_once(
    *,
    config: Optional[tuple[int, int, int]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    queue_dir: Optional[Path] = None,
    claims_file: Optional[Path] = None,
    archive_dir: Path = ARCHIVE_DIR,
) -> dict[str, Any]:
    """One GC tick: reap `.deduped/`, then rotate the claims ledger. Paths +
    config are injectable for tests; in production they come from the
    environment. Each half is isolated so a failure in one never blocks the
    other."""
    now = now or datetime.now(timezone.utc)
    window_h, days, min_lines = config if config is not None else load_gc_config()
    queue_dir = queue_dir or drain._launch_queue_dir()
    claims_file = claims_file or guard.claims_path()
    claim_window_sec = window_h * 3600

    result: dict[str, Any] = {'dry_run': dry_run}
    try:
        reap = reap_deduped(
            queue_dir=queue_dir, now=now,
            claim_window_sec=claim_window_sec, dry_run=dry_run,
        )
    except Exception as e:  # noqa: BLE001 — one half must not block the other
        log(f'reap_deduped raised {type(e).__name__}: {e}; continuing to '
            f'claims rotation', 'ERROR')
        reap = {'error': f'{type(e).__name__}: {e}'}
    result['reap'] = reap
    if not dry_run and reap.get('archived_files'):
        _alert_archived_holds(reap['archived_files'], _deduped_dir(queue_dir))

    try:
        rot = rotate_claims(
            claims_file=claims_file, archive_dir=archive_dir,
            retention_days=days, min_retained_lines=min_lines,
            now=now, dry_run=dry_run,
        )
    except Exception as e:  # noqa: BLE001
        log(f'rotate_claims raised {type(e).__name__}: {e}', 'ERROR')
        rot = {'error': f'{type(e).__name__}: {e}'}
    result['rotate'] = rot

    log('tick: ' + ('DRY-RUN ' if dry_run else '')
        + f'reap={reap} rotate={rot}')
    return result


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
