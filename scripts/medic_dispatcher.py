#!/usr/bin/env python3
"""medic_dispatcher.py -- cheap, non-Claude trigger for the Medic operator.

Runs on a frequent systemd timer (every 2-3 min). On each tick:

  1. Gate: OURLIBERTY_MEDIC_ENABLED env + ~/agents/medic.disabled +
     ~/agents/healers.disabled + rate-window check. Any gate failing
     stops the tick (no Claude, no batch write).
  2. Read the non-allowlisted tail of ~/agents/blackboard/larry-alerts.jsonl
     starting at Medic's OWN offset file (~/agents/state/medic-alerts-offset.txt).
     The dispatcher NEVER touches Beacon's offset file.
  3. Filter to owned (source, subject_prefix [, severity]) per
     config/medic-owned-classes.json. Unowned alerts pass through --
     they remain in the file for Beacon, and Medic's offset advances
     past them so future ticks do not re-scan.
  4. If no qualifying alerts in the batch: advance the offset and exit
     in milliseconds (no operator invocation).
  5. Otherwise write the batch to ~/agents/state/medic-batch-<ts>.json
     and invoke scripts/run_medic.sh (which runs `claude -p` in
     agents/medic/ with the batch path as input).
  6. After the operator returns, advance the offset to the last line
     consumed and record one ledger entry per owned alert. PR1 ships
     escalate-only: every owned alert is recorded as outcome=escalated.

Discipline:

  - Stdlib only. Mirrors the existing scripts/heal_*.py logging /
    state / offset patterns.
  - Fail safe: missing / malformed config or ledger files WARN and
    are treated as empty / unknown -- never raise.
  - Offset never regresses. The advance helper takes max(current, new).
  - All escalation writes happen inside the Medic operator via
    scripts/larry_alerts.py append_notification / append_approval_request;
    the dispatcher itself NEVER writes to the alert queue.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import medic_ledger  # noqa: E402

HOME = Path.home()
REPO_DIR = Path(os.environ.get('OURLIBERTY_REPO_DIR', str(HOME / 'agent-core')))
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))

ALERTS_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
OFFSET_FILE = AGENTS_ROOT / 'state' / 'medic-alerts-offset.txt'
BATCH_DIR = AGENTS_ROOT / 'state' / 'medic-batches'
LOG_FILE = AGENTS_ROOT / 'logs' / 'medic-dispatcher.log'

MEDIC_KILL_SWITCH = AGENTS_ROOT / 'medic.disabled'
HEALERS_KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'

OWNED_CLASSES_FILE = REPO_DIR / 'config' / 'medic-owned-classes.json'
ACTION_POLICY_FILE = REPO_DIR / 'config' / 'medic-action-policy.json'

RUN_MEDIC_SH = REPO_DIR / 'scripts' / 'run_medic.sh'
RUN_MEDIC_TIMEOUT_SEC = 20 * 60

ENABLE_ENV_VAR = 'OURLIBERTY_MEDIC_ENABLED'

# Rate-window gate. When the rolling-5h Tier 1 window is hot the dispatcher
# defers this tick rather than spinning a Claude session that would compete
# with Forge / Mirror / Pulse for the same window. "Hot" is detected via two
# orthogonal signals already maintained elsewhere in the repo:
#
#   * `heal_claude_max_burn_rate.recent_rate_limit_event_count()` -- count
#     of HTTP 429 / rate-limit events recorded in the trailing
#     RATE_LIMIT_RECENT_HOURS (2h today). When this exceeds the configurable
#     threshold (env OURLIBERTY_MEDIC_RATE_WINDOW_MAX_EVENTS, default 2),
#     defer.
#   * `active_tier.cooldown_until(tier)` -- per-tier cooldown set by the
#     rotation scheduler when a rate_limit fires. If EITHER tier has an
#     active cooldown, defer.
#
# Fail-open: on any read / import error from the gauges, log WARN and return
# True. A gauge bug must not permanently silence Medic; the per-session
# timeout in run_medic.sh plus the concurrency lock bound the blast radius.
RATE_WINDOW_MAX_EVENTS_ENV_VAR = 'OURLIBERTY_MEDIC_RATE_WINDOW_MAX_EVENTS'
DEFAULT_RATE_WINDOW_MAX_EVENTS = 2

# Cap how many alerts go into a single batch. The Medic operator
# investigates each one with read-only bash + writes an escalation,
# so a runaway batch (e.g. 500 stalled inboxes after a long outage)
# would blow past the per-cycle cost / time budget. Excess alerts
# remain in the queue; the next dispatcher tick picks them up.
MAX_BATCH_SIZE = 25


def log(level: str, msg: str) -> None:
    """Append a structured line to the dispatcher log. Never raises."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] medic_dispatcher: {msg}\n')
    except OSError:
        pass


# ---------- gates ----------


def _enable_flag_ok() -> bool:
    """OURLIBERTY_MEDIC_ENABLED env must be a truthy value. systemd's
    service file sets this to '1' by default (ships active)."""
    raw = os.environ.get(ENABLE_ENV_VAR, '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _kill_switches_clear() -> bool:
    """Both Medic-specific and shared healer kill switches must be
    absent. Either present stops the tick."""
    return not MEDIC_KILL_SWITCH.exists() and not HEALERS_KILL_SWITCH.exists()


def _rate_window_max_events() -> int:
    """Resolve the configurable rate-limit-events threshold. Bad/missing
    env value falls back to the default and WARN-logs."""
    raw = os.environ.get(RATE_WINDOW_MAX_EVENTS_ENV_VAR, '').strip()
    if not raw:
        return DEFAULT_RATE_WINDOW_MAX_EVENTS
    try:
        value = int(raw)
    except ValueError:
        log('WARN', f'{RATE_WINDOW_MAX_EVENTS_ENV_VAR}={raw!r} not an int; '
                    f'using default {DEFAULT_RATE_WINDOW_MAX_EVENTS}')
        return DEFAULT_RATE_WINDOW_MAX_EVENTS
    if value < 0:
        log('WARN', f'{RATE_WINDOW_MAX_EVENTS_ENV_VAR}={value} negative; '
                    f'using default {DEFAULT_RATE_WINDOW_MAX_EVENTS}')
        return DEFAULT_RATE_WINDOW_MAX_EVENTS
    return value


def _recent_rate_limit_events() -> int:
    """Read the trailing-window 429 count from the burn-rate healer.
    Lazy import so dispatcher startup does not depend on the healer
    module loading; exceptions propagate to the caller (which fails
    open). Wrapped as a thin shim so tests can patch it cleanly."""
    import heal_claude_max_burn_rate as _healer
    return int(_healer.recent_rate_limit_event_count())


def _active_tier_in_rate_limit_cooldown() -> bool:
    """Return True if either OAuth tier has an active rate_limit cooldown.
    The dispatcher does not need to know which tier the next Medic run
    will land on -- either tier in cooldown is reason to defer. Lazy
    import for the same reason as _recent_rate_limit_events()."""
    import active_tier as _at
    for tier in ('tier1', 'tier2'):
        if _at.cooldown_until(tier):
            return True
    return False


def _rate_window_ok() -> bool:
    """Rolling rate-limit window gate. Returns False (defer this tick)
    when recent rate-limit events exceed the configured threshold OR an
    OAuth tier is in a rate_limit cooldown. Fail-open on gauge error:
    a broken gauge must not permanently silence Medic; the per-session
    timeout in run_medic.sh and the lock bound the blast radius.
    """
    threshold = _rate_window_max_events()
    try:
        events = _recent_rate_limit_events()
    except Exception as e:
        log('WARN', f'rate-window event gauge read failed '
                    f'({type(e).__name__}: {e}); failing open')
        return True
    if events > threshold:
        log('INFO', f'rate-window hot: {events} recent rate-limit events '
                    f'> threshold {threshold}; deferring')
        return False
    try:
        in_cooldown = _active_tier_in_rate_limit_cooldown()
    except Exception as e:
        log('WARN', f'active-tier cooldown gauge read failed '
                    f'({type(e).__name__}: {e}); failing open')
        return True
    if in_cooldown:
        log('INFO', 'rate-window hot: an OAuth tier is in rate-limit '
                    'cooldown; deferring')
        return False
    return True


# ---------- config loaders ----------


def _load_owned_classes() -> list[dict]:
    """Read config/medic-owned-classes.json. Fail safe: missing or
    malformed file -> [] (no alerts owned, every alert passes through
    to Beacon). Each entry is validated; entries missing the required
    `source` field are dropped with a WARN."""
    try:
        with open(OWNED_CLASSES_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        log('WARN', f'owned-classes config missing at {OWNED_CLASSES_FILE}; '
                    'treating as empty (no alerts will be owned by Medic)')
        return []
    except (OSError, json.JSONDecodeError) as e:
        log('WARN', f'owned-classes config unreadable ({type(e).__name__}: {e}); '
                    'treating as empty')
        return []
    if not isinstance(data, dict):
        log('WARN', 'owned-classes config root is not an object; treating as empty')
        return []
    entries = data.get('owned_classes')
    if not isinstance(entries, list):
        log('WARN', 'owned-classes config has no owned_classes list; treating as empty')
        return []
    cleaned: list[dict] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            log('WARN', f'owned-classes entry {idx} not an object; skipping')
            continue
        source = entry.get('source')
        if not isinstance(source, str) or not source:
            log('WARN', f'owned-classes entry {idx} missing source; skipping')
            continue
        cleaned.append({
            'source': source,
            'subject_prefix': entry.get('subject_prefix') if isinstance(
                entry.get('subject_prefix'), str) else '',
            'severity': entry.get('severity') if isinstance(
                entry.get('severity'), str) else None,
            'action_tier_hint': entry.get('action_tier_hint') if isinstance(
                entry.get('action_tier_hint'), str) else 'judgment',
        })
    return cleaned


def _load_action_policy() -> dict:
    """Read config/medic-action-policy.json. Fail safe: missing or
    malformed -> {'default_tier': 'judgment', 'tiers': {}} so unknown
    action_types resolve to the safest default."""
    fallback = {'default_tier': 'judgment', 'tiers': {}}
    try:
        with open(ACTION_POLICY_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        log('WARN', f'action-policy config missing at {ACTION_POLICY_FILE}; '
                    'using fallback (default_tier=judgment)')
        return fallback
    except (OSError, json.JSONDecodeError) as e:
        log('WARN', f'action-policy config unreadable ({type(e).__name__}: {e}); '
                    'using fallback')
        return fallback
    if not isinstance(data, dict):
        log('WARN', 'action-policy config root is not an object; using fallback')
        return fallback
    tiers = data.get('tiers')
    if not isinstance(tiers, dict):
        tiers = {}
    default_tier = data.get('default_tier') if isinstance(
        data.get('default_tier'), str) else 'judgment'
    return {'default_tier': default_tier, 'tiers': tiers}


# ---------- offset machinery (Medic's OWN file) ----------


def _read_offset() -> int:
    """Read Medic's offset. 0 if missing / malformed. NEVER touches
    Beacon's offset at ~/agents/state/beacon-alerts-offset.txt."""
    if not OFFSET_FILE.exists():
        return 0
    try:
        raw = OFFSET_FILE.read_text().strip() or '0'
        return int(raw)
    except (OSError, ValueError):
        log('WARN', 'medic offset unreadable; resetting to 0')
        return 0


def _write_offset(offset: int) -> None:
    """Persist Medic's offset atomically (tmp + rename). Mirrors
    larry_alerts.write_offset's shape. Caller is responsible for
    enforcing monotonicity via _advance_offset()."""
    try:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OFFSET_FILE.with_suffix('.tmp')
        tmp.write_text(str(int(offset)))
        tmp.rename(OFFSET_FILE)
    except OSError as e:
        log('WARN', f'medic offset write failed: {e}')


def _advance_offset(new_offset: int) -> None:
    """Move the offset forward to new_offset. Never regresses: if the
    new offset is below the current, the on-disk value is unchanged.
    """
    current = _read_offset()
    if new_offset > current:
        _write_offset(new_offset)


# ---------- queue reader ----------


def _read_pending(offset: int) -> list[tuple[int, dict]]:
    """Return [(line_index, parsed_dict)] from larry-alerts.jsonl at or
    beyond `offset`. Malformed lines surface as {'_malformed': True}
    records so the dispatcher can still advance past them.
    """
    if not ALERTS_FILE.exists():
        return []
    out: list[tuple[int, dict]] = []
    try:
        with open(ALERTS_FILE, encoding='utf-8') as f:
            for idx, raw_line in enumerate(f):
                if idx < offset:
                    continue
                line = raw_line.strip()
                if not line:
                    out.append((idx, {'_malformed': True, 'raw': ''}))
                    continue
                try:
                    out.append((idx, json.loads(line)))
                except json.JSONDecodeError:
                    out.append((idx, {'_malformed': True, 'raw': line[:200]}))
    except OSError as e:
        log('WARN', f'queue read failed: {e}')
        return []
    return out


# ---------- owned-class match ----------


def _is_alert_record(rec: dict) -> bool:
    """Notifications and approval_requests share the same file but are
    NOT alerts -- Medic must not own them. Records missing `kind` are
    treated as alerts (the larry_alerts.append_alert path omits kind).
    """
    kind = rec.get('kind')
    return kind is None or kind == 'alert'


def _match_owned(rec: dict, owned: list[dict]) -> Optional[dict]:
    """Return the matching owned-class entry for an alert record, or
    None if none match. Match rules: source equal; subject startswith
    subject_prefix (empty prefix matches any subject); severity equal
    if the class entry specifies one.
    """
    if not _is_alert_record(rec):
        return None
    src = rec.get('source')
    subj = rec.get('subject') or ''
    sev = rec.get('severity')
    if not isinstance(src, str):
        return None
    for entry in owned:
        if entry['source'] != src:
            continue
        prefix = entry.get('subject_prefix') or ''
        if prefix and not (isinstance(subj, str) and subj.startswith(prefix)):
            continue
        required_sev = entry.get('severity')
        if required_sev and sev != required_sev:
            continue
        return entry
    return None


# ---------- operator invocation ----------


def _write_batch(batch: list[dict], ts_str: str) -> Optional[Path]:
    """Write the batch file the operator will consume. Returns the
    path on success, None on failure (so the caller can refuse to
    invoke Claude without input)."""
    try:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        path = BATCH_DIR / f'medic-batch-{ts_str}.json'
        payload = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'count': len(batch),
            'alerts': batch,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return path
    except OSError as e:
        log('WARN', f'batch write failed: {e}')
        return None


def _invoke_operator(batch_path: Path) -> int:
    """Invoke scripts/run_medic.sh with the batch path. Returns the
    subprocess exit code. Times out at RUN_MEDIC_TIMEOUT_SEC; on
    timeout returns 124 (sysexits-ish 'command not found' analog;
    callers only branch on zero vs non-zero).
    """
    if not RUN_MEDIC_SH.exists():
        log('WARN', f'run_medic.sh not found at {RUN_MEDIC_SH}; skipping invocation')
        return 127
    try:
        proc = subprocess.run(
            ['/usr/bin/bash', str(RUN_MEDIC_SH), str(batch_path)],
            timeout=RUN_MEDIC_TIMEOUT_SEC,
            check=False,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        log('WARN', f'run_medic.sh timed out after {RUN_MEDIC_TIMEOUT_SEC}s')
        return 124
    except OSError as e:
        log('WARN', f'run_medic.sh invocation failed: {e}')
        return 1


# ---------- main loop ----------


def main(argv: Optional[list[str]] = None) -> int:
    log('INFO', f'tick start (pid={os.getpid()})')

    if not _enable_flag_ok():
        log('INFO', f'gate: {ENABLE_ENV_VAR} not set/truthy; no-op')
        return 0
    if not _kill_switches_clear():
        which = []
        if MEDIC_KILL_SWITCH.exists():
            which.append(str(MEDIC_KILL_SWITCH))
        if HEALERS_KILL_SWITCH.exists():
            which.append(str(HEALERS_KILL_SWITCH))
        log('INFO', f'gate: kill switch present ({", ".join(which)}); no-op')
        return 0
    if not _rate_window_ok():
        log('INFO', 'gate: rate-window not ok; deferring')
        return 0

    owned = _load_owned_classes()
    if not owned:
        log('INFO', 'no owned classes configured; advancing offset and exiting')
        # Even with no owned classes the offset still needs to drain past
        # the current tail so we don't accumulate work for a future config.
        offset = _read_offset()
        pending = _read_pending(offset)
        if pending:
            _advance_offset(pending[-1][0] + 1)
        return 0

    # The action policy is loaded for fail-safe symmetry; PR1's
    # escalate-only path does not consume it (the act-branch is
    # stubbed), but loading here surfaces config breakage early.
    _load_action_policy()

    offset = _read_offset()
    pending = _read_pending(offset)
    if not pending:
        log('INFO', f'no pending alerts at offset={offset}; fast exit')
        return 0

    batch: list[dict] = []
    last_consumed_idx: Optional[int] = None
    for idx, rec in pending:
        last_consumed_idx = idx
        if rec.get('_malformed'):
            log('WARN', f'line {idx} malformed; skipping')
            continue
        match = _match_owned(rec, owned)
        if match is None:
            # Unowned: leave for Beacon. Offset still advances past it.
            continue
        if len(batch) >= MAX_BATCH_SIZE:
            # Don't consume this line yet -- back the cursor up by one so
            # the next tick picks up where we paused.
            last_consumed_idx = idx - 1
            log('INFO', f'batch cap {MAX_BATCH_SIZE} reached at line {idx}; '
                        'deferring remaining alerts to next tick')
            break
        batch.append({
            'line_index': idx,
            'source': rec.get('source'),
            'subject': rec.get('subject'),
            'severity': rec.get('severity'),
            'message': rec.get('message'),
            'suggested_action': rec.get('suggested_action'),
            'ts': rec.get('ts'),
            'owned_class': {
                'source': match['source'],
                'subject_prefix': match.get('subject_prefix') or '',
                'action_tier_hint': match.get('action_tier_hint') or 'judgment',
            },
            'fingerprint': medic_ledger.fingerprint(
                rec.get('source'), rec.get('subject')),
            'prior_attempts': medic_ledger.attempt_count(
                medic_ledger.fingerprint(rec.get('source'), rec.get('subject'))),
        })

    if not batch:
        log('INFO', f'no qualifying alerts in {len(pending)} pending lines; '
                    'fast exit (no operator invocation)')
        if last_consumed_idx is not None:
            _advance_offset(last_consumed_idx + 1)
        return 0

    ts_str = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    batch_path = _write_batch(batch, ts_str)
    if batch_path is None:
        log('WARN', 'batch write failed; not advancing offset, not invoking operator')
        return 1

    # PR2 no-double-record guard: snapshot which fingerprints already have an
    # 'acted' ledger record BEFORE the operator runs. medic_actions.py writes
    # its authoritative 'acted' record at action time; any fingerprint that
    # becomes acted during this run must NOT also get an 'escalated' record
    # from the dispatcher below (no double-recording for that fingerprint+run).
    acted_before = medic_ledger.acted_fingerprints()

    log('INFO', f'invoking run_medic.sh with batch={batch_path.name} '
                f'count={len(batch)}')
    rc = _invoke_operator(batch_path)
    log('INFO', f'run_medic.sh returned rc={rc}')

    newly_acted = medic_ledger.acted_fingerprints() - acted_before

    # Advance offset past everything we consumed, regardless of operator
    # exit code -- the ledger fingerprint dedup (PR2) handles re-trips,
    # and leaving the offset behind would re-burn cost on the same batch.
    if last_consumed_idx is not None:
        _advance_offset(last_consumed_idx + 1)

    # Record one ledger entry per owned alert as outcome=escalated -- EXCEPT
    # any fingerprint medic_actions.py recorded as acted during this run (its
    # action-time record is authoritative; the dispatcher must not duplicate
    # or overwrite it). Attempt = prior_attempts + 1 so the
    # one-action-per-fingerprint guard has the data.
    for entry in batch:
        fp = entry['fingerprint']
        if fp in newly_acted:
            log('INFO', f'fingerprint {fp} recorded as acted by medic_actions '
                        'this run; skipping dispatcher post-record')
            continue
        attempt = int(entry.get('prior_attempts', 0)) + 1
        medic_ledger.append_record(
            source=entry.get('source'),
            subject=entry.get('subject'),
            classification=entry['owned_class']['action_tier_hint'],
            outcome='escalated',
            attempt=attempt,
            notes=f'escalated (not acted this run); batch={batch_path.name}',
        )

    return 0 if rc == 0 else 1


if __name__ == '__main__':
    # Keep the import-side state consistent under test reload by
    # avoiding any module-level mutation here.
    start = time.monotonic()
    try:
        rc = main(sys.argv[1:])
    finally:
        log('INFO', f'tick done in {time.monotonic() - start:.2f}s')
    sys.exit(rc)
