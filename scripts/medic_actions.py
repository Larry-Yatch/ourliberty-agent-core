#!/usr/bin/env python3
"""medic_actions.py -- the ONE enforcement surface for Medic's reversible
remediation actions (PR2).

This is the only place in the Medic constellation where a *mutating* command
runs. The Medic operator (agents/medic/CLAUDE.md) has raw `systemctl restart`
and friends DENIED in its bash allowlist as defense in depth; to act, it must
call this module, which shells out internally after re-checking every gate.

PR2 exposes two reversible handlers only:

  * restart_daemon(unit, fingerprint, attempt)
      Restart a watchdog-supervised daemon via `sudo -n systemctl restart`.
  * retrigger_inbox(target, fingerprint, attempt)
      Re-trigger a stalled inbox. The existing re-trigger mechanism in this
      repo IS restarting the inbox-watcher (see scripts/watchdog.py
      check_inbox_watcher / the V2 process-memory restart path), so this
      handler performs the same `sudo -n systemctl restart <unit>` + verify.

Each handler, in strict order:

  (a) re-checks ALL THREE gates -- OURLIBERTY_MEDIC_ENABLED truthy +
      ~/agents/medic.disabled absent + ~/agents/healers.disabled absent --
      and refuses (no action) if any fails.
  (b) validates the target against the fail-safe allowlist loaded from
      config/medic-reversible-targets.json; a target not in the allowlist is
      REFUSED (not-permitted, escalate instead) -- Medic never acts on an
      arbitrary unit.
  (c) HARD one-action-per-fingerprint gate -- if the ledger holds any prior
      outcome='acted' record for this fingerprint, refuse (already acted once,
      escalate recurrence). Prevents restart loops.
  (d) performs the action (sudo -n systemctl restart <unit>).
  (e) VERIFIES post-state (`systemctl is-active <unit>` == 'active'); if
      verification fails it returns a FAILURE result -- it does NOT report
      success.
  (f) records the outcome to the ledger (classification='reversible',
      outcome='acted' once the action was attempted on the system /
      'skipped' on any pre-action refusal) with a short notes string.

Discipline:

  - Stdlib only. Never raises -- fail safe, always returns a structured
    result dict. An unexpected exception becomes ok=False, reason='exception'.
  - Subprocess work is funneled through _run_restart / _is_active so tests can
    patch them and assert no subprocess fires on a refusal path.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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

REVERSIBLE_TARGETS_FILE = REPO_DIR / 'config' / 'medic-reversible-targets.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'medic-dispatcher.log'

MEDIC_KILL_SWITCH = AGENTS_ROOT / 'medic.disabled'
HEALERS_KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
ENABLE_ENV_VAR = 'OURLIBERTY_MEDIC_ENABLED'

RESTART_TIMEOUT_SEC = 180
IS_ACTIVE_TIMEOUT_SEC = 10

ACTION_RESTART_DAEMON = 'restart-daemon'
ACTION_RETRIGGER_INBOX = 'retrigger-inbox'
# retrigger-watcher is an alias of retrigger-inbox (same mechanism, same
# allowlist) -- the action-policy lists both action_type names.
ACTION_RETRIGGER_WATCHER = 'retrigger-watcher'


def _log(level: str, msg: str) -> None:
    """Append a structured line to the shared medic-dispatcher log. Never
    raises; matches scripts/medic_ledger.py's log shape so the whole Medic
    constellation is greppable in one file."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] medic_actions: {msg}\n')
    except OSError:
        pass


# ---------- gates ----------


def _enable_flag_ok() -> bool:
    raw = os.environ.get(ENABLE_ENV_VAR, '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _kill_switches_clear() -> bool:
    return not MEDIC_KILL_SWITCH.exists() and not HEALERS_KILL_SWITCH.exists()


def _gates_ok() -> tuple[bool, str]:
    """Re-check all three gates. Returns (ok, reason_code). reason_code is
    '' when ok, else identifies the failing gate."""
    if not _enable_flag_ok():
        return False, 'gate-enable-flag-off'
    if MEDIC_KILL_SWITCH.exists():
        return False, 'gate-medic-kill-switch'
    if HEALERS_KILL_SWITCH.exists():
        return False, 'gate-healers-kill-switch'
    return True, ''


# ---------- allowlist ----------


def _load_reversible_targets() -> dict:
    """Read config/medic-reversible-targets.json. Fail safe: missing or
    malformed -> empty lists (Medic can act on nothing). Never raises."""
    empty = {'restart_daemon_units': [], 'retrigger_inbox_targets': []}
    try:
        with open(REVERSIBLE_TARGETS_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        _log('WARN', f'reversible-targets config missing at '
                     f'{REVERSIBLE_TARGETS_FILE}; treating as empty allowlist')
        return empty
    except (OSError, json.JSONDecodeError) as e:
        _log('WARN', f'reversible-targets config unreadable '
                     f'({type(e).__name__}: {e}); treating as empty allowlist')
        return empty
    if not isinstance(data, dict):
        _log('WARN', 'reversible-targets config root is not an object; '
                     'treating as empty allowlist')
        return empty

    def _clean_list(key: str) -> list:
        raw = data.get(key)
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, str) and item]

    return {
        'restart_daemon_units': _clean_list('restart_daemon_units'),
        'retrigger_inbox_targets': _clean_list('retrigger_inbox_targets'),
    }


# ---------- subprocess shims (patched in tests) ----------


def _run_restart(unit: str) -> int:
    """Run `sudo -n systemctl restart <unit>` and return the exit code.
    Mirrors scripts/watchdog.py's restart path (larry has NOPASSWD sudo).
    Returns a non-zero sentinel on subprocess error rather than raising."""
    try:
        proc = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', unit],
            capture_output=True, timeout=RESTART_TIMEOUT_SEC,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        _log('WARN', f'restart of {unit} timed out after {RESTART_TIMEOUT_SEC}s')
        return 124
    except OSError as e:
        _log('WARN', f'restart of {unit} failed to launch: {e}')
        return 1


def _is_active(unit: str) -> str:
    """Return the trimmed stdout of `systemctl is-active <unit>` (e.g.
    'active', 'failed', 'inactive', 'activating'). Empty string on error."""
    try:
        proc = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=IS_ACTIVE_TIMEOUT_SEC,
        )
        return (proc.stdout or '').strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        _log('WARN', f'is-active probe of {unit} failed: {e}')
        return ''


# ---------- result helpers ----------


def _fp_parts(fp: str) -> tuple[str, str]:
    """Split a fingerprint back into (source, subject) so a ledger record
    round-trips to the SAME fingerprint string. Fingerprints are
    'source:subject' (subject may itself contain ':', source does not), so
    we split on the first ':'. This keeps the record medic_actions writes
    keyed identically to the alert fingerprint the operator passes in, which
    is what has_acted() (recurrence gate) and the dispatcher's no-double-
    record guard rely on."""
    if ':' in fp:
        src, subj = fp.split(':', 1)
        return src, subj
    return fp, ''


def _result(action: str, target: str, fingerprint: str, ok: bool,
            outcome: str, reason: str, detail: str) -> dict:
    return {
        'ok': ok,
        'action': action,
        'target': target,
        'fingerprint': fingerprint,
        'outcome': outcome,
        'reason': reason,
        'detail': detail,
    }


def _refuse(action: str, target: str, fingerprint: str, attempt: int,
            reason: str, detail: str) -> dict:
    """Record a pre-action refusal as outcome='skipped' and return the
    structured refusal result. No subprocess has run at this point."""
    rec_source, rec_subject = _fp_parts(fingerprint)
    medic_ledger.append_record(
        source=rec_source, subject=rec_subject, classification='reversible',
        outcome='skipped', attempt=attempt,
        notes=f'{action} refused for {target}: {reason}',
    )
    _log('INFO', f'{action} refused for {target} (fp={fingerprint}): {reason}')
    return _result(action, target, fingerprint, ok=False,
                   outcome='skipped', reason=reason, detail=detail)


# ---------- core handler ----------


def _act_restart(action: str, target: str, fingerprint: str, attempt: int,
                 allowed_units: list) -> dict:
    """Shared body for both reversible handlers: gates -> allowlist ->
    recurrence -> restart -> verify -> record. Never raises."""
    try:
        if not isinstance(target, str) or not target:
            return _result(action, str(target), str(fingerprint), ok=False,
                           outcome='skipped', reason='no-target',
                           detail='No target unit supplied; refusing.')
        fp = fingerprint if isinstance(fingerprint, str) and fingerprint \
            else medic_ledger.fingerprint('medic', target)
        try:
            attempt_int = int(attempt)
        except (TypeError, ValueError):
            attempt_int = 1

        # (a) gates
        ok, gate_reason = _gates_ok()
        if not ok:
            return _refuse(action, target, fp, attempt_int, gate_reason,
                           'A Medic gate (enable flag or a kill switch) '
                           'is not satisfied; no action taken. Escalate '
                           'manually if the issue persists.')

        # (b) allowlist
        if target not in allowed_units:
            return _refuse(
                action, target, fp, attempt_int, 'not-permitted',
                f'Target {target} is not in the reversible-targets '
                f'allowlist for {action}; refusing to act. Escalate '
                f'diagnose-only instead.')

        # (c) one-action-per-fingerprint recurrence gate
        if medic_ledger.has_acted(fp):
            return _refuse(
                action, target, fp, attempt_int, 'already-acted',
                f'Medic already acted once on fingerprint {fp}; refusing a '
                f'second action to avoid a restart loop. Escalate the '
                f'recurrence.')

        # (d) perform
        _log('INFO', f'{action}: restarting {target} (fp={fp}, '
                     f'attempt={attempt_int})')
        rc = _run_restart(target)

        # (e) verify
        active_state = _is_active(target)
        verified = (rc == 0 and active_state == 'active')

        # (f) record. Once the restart subprocess was invoked an action was
        # attempted on the system, so we record outcome='acted' regardless of
        # verification -- this arms the recurrence gate so a failed restart is
        # NOT retried in a loop. ok reflects verified success only.
        rec_source, rec_subject = _fp_parts(fp)
        if verified:
            notes = f'{action} ok: {target} verified active (rc={rc})'
            medic_ledger.append_record(
                source=rec_source, subject=rec_subject,
                classification='reversible', outcome='acted',
                attempt=attempt_int, notes=notes)
            _log('INFO', notes)
            return _result(action, target, fp, ok=True, outcome='acted',
                           reason='acted',
                           detail=f'{target} restarted and verified active.')
        # action ran but did not verify
        reason = 'restart-error' if rc != 0 else 'verify-failed'
        state_str = active_state or 'unknown'
        notes = (f'{action} FAILED: {target} rc={rc} is-active={state_str}')
        medic_ledger.append_record(
            source=rec_source, subject=rec_subject, classification='reversible',
            outcome='acted', attempt=attempt_int, notes=notes)
        _log('WARN', notes)
        return _result(
            action, target, fp, ok=False, outcome='acted', reason=reason,
            detail=(f'Restart of {target} ran (rc={rc}) but verification '
                    f'failed (is-active={state_str}). Escalate diagnose-only; '
                    f'do not retry -- the recurrence gate is now armed.'))
    except Exception as e:  # never raise -- fail safe
        _log('ERROR', f'{action} unexpected exception for {target}: '
                      f'{type(e).__name__}: {e}')
        return _result(action, str(target), str(fingerprint), ok=False,
                       outcome='skipped', reason='exception',
                       detail=f'Unexpected error: {type(e).__name__}: {e}')


# ---------- public handlers ----------


def restart_daemon(unit: str, fingerprint: str, attempt: int = 1) -> dict:
    """Restart a watchdog-supervised daemon unit. See module docstring for
    the gate / allowlist / recurrence / verify contract."""
    allowed = _load_reversible_targets().get('restart_daemon_units', [])
    return _act_restart(ACTION_RESTART_DAEMON, unit, fingerprint, attempt,
                        allowed)


def retrigger_inbox(target: str, fingerprint: str, attempt: int = 1) -> dict:
    """Re-trigger a stalled inbox by restarting the inbox-watcher (the
    repo's existing re-trigger mechanism). Same contract as restart_daemon,
    validated against the retrigger_inbox_targets allowlist."""
    allowed = _load_reversible_targets().get('retrigger_inbox_targets', [])
    return _act_restart(ACTION_RETRIGGER_INBOX, target, fingerprint, attempt,
                        allowed)


# ---------- CLI ----------


def _main(argv: Optional[list] = None) -> int:
    """CLI the Medic operator invokes. Only the two reversible action types
    are wired; a privileged / judgment / unknown action type is rejected
    here so it can never reach a mutating handler. Prints the result dict as
    JSON to stdout and exits 0 on verified success, 1 otherwise."""
    parser = argparse.ArgumentParser(prog='medic_actions.py')
    parser.add_argument(
        'action',
        help='reversible action type: restart-daemon | retrigger-inbox | '
             'retrigger-watcher')
    parser.add_argument('--unit', '--target', dest='target', default='',
                        help='the daemon unit / inbox-watcher target')
    parser.add_argument('--fingerprint', default='', help='alert fingerprint')
    parser.add_argument('--attempt', type=int, default=1,
                        help='attempt number (prior_attempts + 1)')
    args = parser.parse_args(argv)

    if args.action == ACTION_RESTART_DAEMON:
        result = restart_daemon(args.target, args.fingerprint, args.attempt)
    elif args.action in (ACTION_RETRIGGER_INBOX, ACTION_RETRIGGER_WATCHER):
        result = retrigger_inbox(args.target, args.fingerprint, args.attempt)
    else:
        # Privileged / judgment / unknown action types never reach a handler.
        result = _result(
            args.action, args.target, args.fingerprint, ok=False,
            outcome='skipped', reason='unsupported-action',
            detail=(f'Action type {args.action!r} is not a PR2 reversible '
                    f'handler; medic_actions.py only performs restart-daemon '
                    f'and retrigger-inbox/retrigger-watcher. Escalate via '
                    f'larry_alerts.py instead.'))
        _log('INFO', f'rejected unsupported action type {args.action!r}')

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
