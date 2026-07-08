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
  (b2) GRADUATION gate (Phase-C unify, Stage 2) -- when the ACTION name IS a
      registry template (config/auto-fix-patterns.json), Medic auto-acts only
      once Larry has GRADUATED that template (state='graduated', not a
      permanent_guard). A probation / permanent_guard template is REFUSED
      (not-graduated, escalate diagnose-only) so Larry decides -- graduation
      is what earns hands-free trust, and it now GOVERNS Medic instead of
      Medic self-governing via its allowlist alone. Actions that are not
      registry templates (retrigger-inbox) and an unreadable registry both
      fall through to current behavior (fail-safe: a registry read error must
      never block a legitimate Medic action). See _graduation_gate.
  (c) HARD one-action-per-fingerprint gate -- if the ledger holds any prior
      outcome='acted' record for this fingerprint, refuse (already acted once,
      escalate recurrence). Prevents restart loops.
  (d) performs the action (sudo -n systemctl restart <unit>).
  (e) VERIFIES post-state (`systemctl is-active <unit>` == 'active'); if
      verification fails it returns a FAILURE result -- it does NOT report
      success.
  (f) records the outcome to the ledger (classification='reversible';
      outcome='acted' on a VERIFIED success / 'acted-failed' when the action
      ran but verification failed / 'skipped' on any pre-action refusal) with
      a short notes string. Both 'acted' and 'acted-failed' arm the recurrence
      gate (no retry loop), but only 'acted' is counted as *handled* by the
      dispatcher -- an 'acted-failed' restart still escalates (audit M2).

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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import medic_ledger  # noqa: E402
import larry_alerts  # noqa: E402
import marker_paths  # noqa: E402  # shared restart-coordination marker paths (watchdog parity)

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
# silence-false-positive: the reversible "quiet a confirmed benign alert"
# handler. Unlike the restart handlers it mutates NOTHING on the system --
# it writes a durable suppression file (larry_alerts.silence) so a recurring
# false-positive fingerprint stops DMing Larry. Gated by the
# `silenceable_subjects` allowlist so Medic can only silence proven-benign
# alert CLASSES, never an arbitrary alert. Reversible via larry_alerts.unsilence.
ACTION_SILENCE = 'silence-false-positive'


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
    empty = {'restart_daemon_units': [], 'retrigger_inbox_targets': [],
             'silenceable_subjects': []}
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
        'silenceable_subjects': _clean_list('silenceable_subjects'),
    }


# ---------- watchdog/systemd coordination (Stage-2 restart guard) ----------
#
# Three other actors already restart the two allowlisted watcher units
# (ourliberty-inbox-watcher.service, ourliberty-outbox-notifier.service):
#   1. systemd `Restart=on-failure` (+ StartLimit backoff).
#   2. scripts/watchdog.py `_check_auto_restart`: on a sustained auto-restart
#      streak it DELIBERATELY does not actuate -- it emits a flapping critical
#      and defers to systemd's backoff (watchdog.py ~:258 "deferring to systemd").
#   3. watchdog's V2 process-memory restart, gated by a 15-min cooldown marker.
#
# Medic acts on the watchdog's alerts, so an enabled restart_daemon would land
# ON TOP of these. The recurrence gate + the M2 acted-failed-escalates fix
# already bound Medic to ~1 restart per fingerprint (no loop), and a Medic
# restart adds real value when systemd has GIVEN UP (unit failed /
# StartLimit-exhausted with no peer actively restarting it). The gap this guard
# closes: Medic today has NO awareness of a recent watchdog/systemd restart or
# an active flap, so it could reset systemd's backoff on a flapping unit --
# undercutting the watchdog's deliberate defer-on-flap discipline. So AFTER the
# allowlist + recurrence gates and BEFORE actuating, _act_restart consults the
# markers below and refuses+escalates (diagnose-only) when a peer is already
# managing the unit's restart.
#
# These marker PATHS are now built by the shared scripts/marker_paths.py module
# imported by BOTH watchdog.py and this file, so the path shape lives in exactly
# one place and the two agents cannot silently drift. The builders take an
# agents_root argument rather than importing watchdog's hardcoded AGENTS_ROOT,
# precisely so medic's OURLIBERTY_AGENTS_ROOT override (which the env contract
# and test isolation depend on) is honored -- we pass our own AGENTS_ROOT.
# The fresh/cooldown WINDOWS below are medic-local read-side thresholds (how
# long a marker is treated as live); they remain here.

# A flap marker is only treated as an ACTIVE flap if touched within this window;
# an older marker is considered stale (watchdog stopped updating it) so a
# forgotten marker can never permanently wedge a legitimate restart. Generously
# covers watchdog's minutes-apart cadence and aligns with the reconcile window.
_FLAP_FRESH_WINDOW_SEC = 30 * 60
# watchdog.py PROC_MEM_RESTART_COOLDOWN_SEC -- V2 process-memory restart cooldown
# (inbox-watcher only writes it, as 'inbox-watcher-mem-restart-cooldown').
_MEM_RESTART_COOLDOWN_SEC = 15 * 60
# watchdog.py RECONCILE_WINDOW_SEC -- desired-state reconciler rolling window.
# (The inbox/outbox units are not currently in bot-liveness-policy.json, so this
# marker would not exist for them today; checked defensively for consistency in
# case they are added later -- the two agents then agree by construction.)
_RECONCILE_WINDOW_SEC = 30 * 60

# Coordination refusal reasons. All escalate diagnose-only and DELIBERATELY do
# NOT reset systemd backoff -- a human sees a genuinely-stuck unit instead.
REASON_FLAPPING = 'flapping-defer-to-systemd'
REASON_RECENTLY_RESTARTED = 'recently-restarted'

# Graduation-gate refusal reason (Phase-C unify, Stage 2). Diagnose-only
# escalation: the action's class has not (yet) earned -- or can never earn, for
# a permanent_guard floor -- hands-free trust, so Medic asks instead of acting.
REASON_NOT_GRADUATED = 'not-graduated'


# Name transforms are delegated to the shared module so watchdog and medic
# derive the same marker keys from a unit name.
_flap_marker_name = marker_paths.flap_marker_name
_unit_short_name = marker_paths.unit_short_name


def _recent_peer_restart(unit: str) -> tuple[bool, str, str]:
    """Coordinate with watchdog/systemd BEFORE Medic restarts an allowlisted
    unit. Returns (block, reason, detail). block=True means another actor is
    actively managing this unit's restart -- an active watchdog flap streak or
    a peer restart still within its cooldown window -- so Medic must NOT
    restart: doing so would reset systemd's StartLimit backoff and undercut the
    watchdog's deliberate defer-on-flap discipline. Medic instead escalates
    diagnose-only so a human sees a genuinely-stuck unit.

    Fail-safe stance: every marker read defaults to NOT blocking on ANY error
    (missing/unreadable/corrupt marker -> proceed). An unreadable marker must
    never permanently wedge a legitimate restart; the recurrence gate +
    verify/acted-failed already bound Medic to ~1 restart per fingerprint, so
    proceeding-on-read-error is safe."""
    now = time.time()

    # 1. Active watchdog flap streak. watchdog counts consecutive systemd
    #    auto-restart ticks and DEFERS to systemd while the streak is live
    #    (it never actuates during auto-restart). A freshly-touched marker =>
    #    a flap is in progress; restarting now would reset systemd's backoff.
    try:
        flap = marker_paths.flap_streak_path(AGENTS_ROOT, _flap_marker_name(unit))
        if flap.exists() and (now - flap.stat().st_mtime) < _FLAP_FRESH_WINDOW_SEC:
            try:
                streak = flap.read_text().strip() or '?'
            except OSError:
                streak = '?'
            return (True, REASON_FLAPPING,
                    f'watchdog reports {unit} flapping in systemd auto-restart '
                    f'(streak={streak}); deferring to systemd backoff. Escalate '
                    f'diagnose-only -- do NOT reset backoff on a flapping unit.')
    except OSError:
        pass  # fail-safe: unreadable marker -> do not block

    short = _unit_short_name(unit)

    # 2a. V2 process-memory restart cooldown. watchdog touches this marker on a
    #     mem-restart (15-min window); a peer just restarted the unit.
    try:
        mem = marker_paths.mem_restart_cooldown_path(AGENTS_ROOT, short)
        if mem.exists():
            age = now - mem.stat().st_mtime
            if age < _MEM_RESTART_COOLDOWN_SEC:
                return (True, REASON_RECENTLY_RESTARTED,
                        f'watchdog mem-restarted {unit} {int(age)}s ago '
                        f'(< {_MEM_RESTART_COOLDOWN_SEC}s cooldown); a peer '
                        f'restart is in flight. Escalate diagnose-only.')
    except OSError:
        pass  # fail-safe

    # 2b. Desired-state reconcile cooldown. watchdog writes JSON
    #     {window_start, count, paged}; a recent reconcile restart means a peer
    #     is managing recovery within the rolling window.
    try:
        rec = marker_paths.reconcile_marker_path(AGENTS_ROOT, short)
        if rec.exists():
            data = json.loads(rec.read_text())
            window_start = float(data['window_start'])
            count = int(data['count'])
            if count >= 1 and (now - window_start) <= _RECONCILE_WINDOW_SEC:
                return (True, REASON_RECENTLY_RESTARTED,
                        f'watchdog reconciler restarted {unit} {count}x in the '
                        f'current {_RECONCILE_WINDOW_SEC // 60}m window; a peer '
                        f'is managing recovery. Escalate diagnose-only.')
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass  # fail-safe: missing/corrupt marker -> do not block

    return (False, '', '')


# Note: a live `systemctl is-active` transitional-state probe (activating/
# reloading == "restart already underway") is DELIBERATELY omitted here. It is
# explicitly optional, and the Medic-triggering alerts for these units already
# imply marker presence (a flapping critical fires only with the flap marker
# set; a genuine down/start-FAILED alert fires only after watchdog confirmed the
# unit NOT in auto-restart -- exactly when Medic should restart). Adding it would
# double the `systemctl is-active` calls on every happy-path restart (one here,
# one for the post-restart verify) for a case the flap marker already covers.


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


# ---------- graduation track-record (Phase-C unify, Stage 1) ----------


def _record_template_execution(action: str, *, verified: bool) -> None:
    """Record one action-template execution toward Check V's graduation streak.

    This is the streak INPUT the promotion loop was missing: Medic is the actual
    executor of the reversible auto-fixes the registry graduates, so a verified
    Medic action is the clean-execution signal (docs/pulse-triage-phase-c-brief).
    Recorded ONLY when ``action`` names a real registry template
    (config/auto-fix-patterns.json); a non-registry action is a silent no-op.
    Once an action IS enrolled it accrues a track record here — e.g.
    ``silence-false-positive`` (from ``silence_false_positive``) now that it is a
    registry template. (``retrigger-inbox`` is deliberately NOT enrolled: it runs
    through ``_act_restart``, which Stage 2's ``_graduation_gate`` (#837) would
    then REFUSE while probation, downgrading a working autonomous retrigger to an
    ask — so it stays a non-template action until approved-probation recording
    exists.)

    Thin delegate: the registry-gate + record + never-raise contract is
    single-sourced in ``alert_triage_state.record_clean_execution_if_registered``
    so it can't drift across the executors that share it. ``verified=False`` is
    passed only for a RELIABLE action-quality failure (a restart that didn't come
    back active, a silence file that didn't persist), never for infra flakiness.

    Additive and best-effort by contract: it observes an action that already
    happened; it must NEVER change whether/how Medic acts, and must never raise
    (a track-record write failing cannot be allowed to fail a restart). It is
    naturally start-clean — only acts performed after this ships are recorded.

    NOTE (Stage 2 of the unify is now live -- see _graduation_gate): graduation
    now GOVERNS whether Medic auto-acts. Because the gate refuses a probation
    template BEFORE the action runs, this recorder is in practice only reached
    for a GRADUATED template (a verified/failed act) -- a graduated template's
    failed act records a 'failure' here, which the recorder's own adverse hook
    demotes graduated->probation. The ``action not in registry`` guard is still
    the defensive floor for Medic's non-template actions (retrigger-inbox).
    """
    try:
        import alert_triage_state  # lazy: avoids import coupling at module load
        alert_triage_state.record_clean_execution_if_registered(
            action, verified=verified)
    except Exception as e:  # noqa: BLE001 — track-record must never break Medic
        try:
            _log('WARN', f'graduation-execution record for {action!r} failed: '
                         f'{type(e).__name__}: {e}')
        except Exception:  # even the log must not surface into Medic's act path
            pass


# ---------- graduation gate (Phase-C unify, Stage 2) ----------


def _graduation_gate(action: str) -> tuple[bool, str, str]:
    """Consult the auto-fix registry (config/auto-fix-patterns.json) BEFORE
    Medic auto-acts on an action whose name IS a registry template. Returns
    ``(block, reason, detail)`` -- ``block=True`` means REFUSE + escalate.

    This is Stage 2 of the Check-V unify: the graduation loop now GOVERNS what
    Medic does hands-free (Stage 1 only fed it a track record). Policy:

      * ``action`` not in the registry            -> ``(False, ...)`` keep
        current behavior. Medic self-governs via its own allowlist; a
        non-template action (retrigger-inbox) is not a graduatable class.
      * ``state == 'graduated'`` AND not
        ``permanent_guard``                        -> ``(False, ...)`` AUTO-ACT
        -- Larry has approved this class for hands-free execution.
      * otherwise (probation, or a permanent_guard
        floor even if mislabeled graduated)        -> ``(True, reason, detail)``
        do NOT auto-act; escalate diagnose-only so Larry decides. A probation
        class has not yet EARNED hands-free trust; a permanent_guard class can
        NEVER earn it.

    FAIL-SAFE: ANY error reading the registry -> ``(False, ...)`` KEEP CURRENT
    BEHAVIOR. A registry read failure must NEVER block a legitimate Medic
    action -- Medic's allowlist + recurrence + watchdog-coordination gates
    remain the active brakes. ``alert_triage_state.load_registry`` already
    degrades an unreadable/corrupt file to ``{}`` (-> action-not-found ->
    proceed); the try/except is the belt-and-suspenders guarantee that even an
    unexpected error can never raise into Medic's act path."""
    try:
        import alert_triage_state  # lazy: avoids import coupling at module load
        registry = alert_triage_state.load_registry()
        rec = registry.get(action)
        if not isinstance(rec, dict):
            return (False, '', '')  # not a governed template -> current behavior
        # Canonical auto-fix-eligible predicate -- the exact negation of Check
        # 0's guarded test in alert_triage_state.classify() (state != 'graduated'
        # OR permanent_guard -> Tier 2/ask). Kept in lock-step with that twin so
        # Medic's hands-free boundary never disagrees with Check 0's. Like
        # classify(), this does NOT re-check `reversible`: apply_graduation is
        # the sole path to 'graduated' and already enforces the reversibility /
        # permanent_guard floor, so a graduated record is reversible by
        # construction.
        graduated = (rec.get('state') == 'graduated'
                     and not rec.get('permanent_guard'))
        if graduated:
            return (False, '', '')  # Larry approved hands-free -> auto-act
        state = rec.get('state', 'probation')
        guard = ', permanent_guard floor' if rec.get('permanent_guard') else ''
        detail = (
            f'Action template {action!r} is not graduated for hands-free '
            f'execution (registry state={state!r}{guard}); Medic will NOT '
            f'auto-act. Escalate diagnose-only so Larry decides -- this class '
            f'earns hands-free trust via Check V graduation '
            f'(config/auto-fix-patterns.json), not here.')
        return (True, REASON_NOT_GRADUATED, detail)
    except Exception as e:  # never raise into Medic's act path -> fail-safe
        try:
            _log('WARN', f'graduation gate for {action!r} failed '
                         f'({type(e).__name__}: {e}); proceeding per allowlist '
                         f'(fail-safe to current behavior)')
        except Exception:  # even the log must not surface into the act path
            pass
        return (False, '', '')


# ---------- core handler ----------


def _act_restart(action: str, target: str, fingerprint: str, attempt: int,
                 allowed_units: list) -> dict:
    """Shared body for both reversible handlers: gates -> allowlist ->
    graduation -> recurrence -> restart -> verify -> record. Never raises."""
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

        # (b2) graduation gate (Phase-C unify, Stage 2). Medic auto-acts on an
        # action-template only once Larry has GRADUATED it (state='graduated',
        # not a permanent_guard); a probation template is refused + escalated
        # diagnose-only so graduation actually governs what Medic does hands-
        # free. Actions that are not registry templates (retrigger-inbox) and
        # an unreadable registry both fall through to the existing allowlist-
        # governed behavior (fail-safe). Recorded 'skipped' (no subprocess, the
        # recurrence gate stays un-armed), so Medic can act on a later cycle
        # once Larry graduates the class. See _graduation_gate.
        block, grad_reason, grad_detail = _graduation_gate(action)
        if block:
            return _refuse(action, target, fp, attempt_int, grad_reason,
                           grad_detail)

        # (c) one-action-per-fingerprint recurrence gate
        if medic_ledger.has_acted(fp):
            return _refuse(
                action, target, fp, attempt_int, 'already-acted',
                f'Medic already acted once on fingerprint {fp}; refusing a '
                f'second action to avoid a restart loop. Escalate the '
                f'recurrence.')

        # (c.5) watchdog/systemd coordination gate (Stage-2 restart guard).
        # If another actor (a live watchdog flap or a peer restart within its
        # cooldown window) is already managing this unit's restart, REFUSE and
        # escalate diagnose-only -- Medic must not reset systemd's StartLimit
        # backoff on a flapping unit. Recorded outcome='skipped' (NOT
        # acted/acted-failed), so the recurrence gate stays un-armed and Medic
        # can legitimately act on a later cycle once the peer-restart settles.
        # See _recent_peer_restart for the fail-safe (read errors -> proceed).
        block, peer_reason, peer_detail = _recent_peer_restart(target)
        if block:
            return _refuse(action, target, fp, attempt_int, peer_reason,
                           peer_detail)

        # (d) perform
        _log('INFO', f'{action}: restarting {target} (fp={fp}, '
                     f'attempt={attempt_int})')
        rc = _run_restart(target)

        # (e) verify
        active_state = _is_active(target)
        verified = (rc == 0 and active_state == 'active')

        # (f) record. Once the restart subprocess was invoked an action was
        # attempted on the system, so we arm the recurrence gate either way
        # (a failed restart is NOT retried in a loop). But we record TWO
        # distinct outcomes so the marker does not carry two meanings (audit
        # M2): a VERIFIED success is 'acted' (handled -> dispatcher advances
        # the cursor); an attempt that ran but did not verify is 'acted-failed'
        # (still arms has_acted, but is NOT counted by acted_fingerprints, so
        # the dispatcher does not treat it as handled and instead escalates).
        # ok reflects verified success only.
        rec_source, rec_subject = _fp_parts(fp)
        if verified:
            notes = f'{action} ok: {target} verified active (rc={rc})'
            medic_ledger.append_record(
                source=rec_source, subject=rec_subject,
                classification='reversible', outcome='acted',
                attempt=attempt_int, notes=notes)
            _record_template_execution(action, verified=True)
            _log('INFO', notes)
            return _result(action, target, fp, ok=True, outcome='acted',
                           reason='acted',
                           detail=f'{target} restarted and verified active.')
        # action ran but did not verify -> 'acted-failed' (arms recurrence gate
        # but is not counted as handled, so this still escalates).
        reason = 'restart-error' if rc != 0 else 'verify-failed'
        state_str = active_state or 'unknown'
        notes = (f'{action} FAILED: {target} rc={rc} is-active={state_str}')
        medic_ledger.append_record(
            source=rec_source, subject=rec_subject, classification='reversible',
            outcome='acted-failed', attempt=attempt_int, notes=notes)
        _record_template_execution(action, verified=False)
        _log('WARN', notes)
        return _result(
            action, target, fp, ok=False, outcome='acted-failed', reason=reason,
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


def _emit_silence_decision(fp: str, reason: str, remediation: str) -> bool:
    """Post the silence to Larry's Approvals tab as ONE Approve/Reject
    decision (Supabase chain_events, event_type='approval_request').

    Silencing stops the recurring inbox noise, but Medic can only quiet the
    symptom -- the root cause is still unfixed, and whether to keep or lift
    the silence is Larry's call. So a silence is never truly silent: it is
    coupled with exactly one decision on the Approvals tab:
      * Approve = keep it silenced; Larry will fix the root cause in code.
      * Reject  = the silence is wrong; unsilence the fingerprint so the
        alert fires again.

    The Approvals tab is a decisions-only inbox (only `approval_request` /
    `clarify_request` render there; plain alerts go to the Ops/System tab).
    When Larry clicks, the dashboard writes a generic 'follow the source
    event's suggested_envelope' envelope into Beacon's inbox
    (dashboard_api._build_envelope_for_action), so the two suggested
    envelopes below carry the concrete instruction Beacon executes. The
    shape mirrors the Beacon-originated approval
    (beacon_approval_handler.build_approval_request_chain_event) so the
    dashboard action handler treats a Medic-originated decision identically.

    `remediation` is the operator's specific 'here is the fix' text; when
    empty, a generic instruction is used so the coupling holds even if the
    operator supplied none. One-shot by construction: the caller only
    reaches this on a FRESH silence (the idempotent re-silence path returns
    earlier). Best-effort -- emit_event never raises and returns False if
    Supabase is unavailable, so a missing decision never blocks the
    silence itself."""
    try:
        import chain_event_emit as cee  # lazy: heavy dep, keeps tests light
        import chain_event_shipper as ces
    except Exception:
        return False
    _src, subject = _fp_parts(fp)
    why = reason.strip() if isinstance(reason, str) else ''
    fix = (remediation.strip() if isinstance(remediation, str) else '') or (
        'Investigate the healer/source that emits this fingerprint and patch '
        'it at the source.')
    task_id = f'medic-silence-{subject}'[:120]
    use_ts = datetime.now(timezone.utc).isoformat()
    event_id = ces.compute_event_id(task_id, 'approval_request', use_ts)
    prompt = (
        f'# Medic silenced a false positive — keep it or lift it?\n\n'
        f'Medic durably silenced a recurring alert it confirmed benign, to '
        f'stop it looping in your inbox. Medic can quiet the symptom but not '
        f'patch the code, so the root cause is still unfixed and this is your '
        f'call.\n\n'
        f'**Fingerprint:** `{fp}`\n\n')
    if why:
        prompt += f'**Why Medic judged it benign:** {why}\n\n'
    prompt += (
        f'**Root cause to fix:** {fix}\n\n'
        f'- **Approve** — keep it silenced; you will fix the root cause in '
        f'code.\n'
        f'- **Reject** — this silence is wrong; unsilence it so the alert '
        f'fires again.')
    approve_env = {
        'task_id': f'larry-approval-{event_id}',
        'source': 'dashboard',
        'dedup_identity': f'larry-approval:{event_id}',
        'timeout': 600,
        'prompt': (
            f'Larry approved Medic\'s silence of `{fp}`. No dispatch needed — '
            f'the root-cause fix is Larry\'s to make in code. Acknowledge and '
            f'close the pending item; leave the silence in place (do NOT '
            f'unsilence).'),
    }
    reject_env = {
        'task_id': f'larry-reject-{event_id}',
        'source': 'dashboard',
        'dedup_identity': f'larry-reject:{event_id}',
        'timeout': 600,
        'prompt': (
            f'Larry rejected Medic\'s silence of `{fp}` — the silence is '
            f'wrong. Lift it so the alert can fire again by calling '
            f'larry_alerts.unsilence("{fp}") (verify the silence file under '
            f'state/alert-silenced/ is gone). Do not abort any in-flight '
            f'work.'),
    }
    payload = {
        'proposing_agent': 'medic',
        'target_agent': 'medic',
        # `summary` is in beacon_approval_handler.REQUIRED_FIELDS for
        # approval_request and is what the Approvals-tab renderer shows as the
        # card title (dashboard_api maps it to plan_summary). Without it the card
        # renders as "(no summary)". Keep it short — full context is in `prompt`.
        'summary': f'Medic silenced a false positive — keep or lift? ({subject})',
        # Machine-readable fingerprint so the dashboard can reconcile this
        # decision DIRECTLY (reject -> larry_alerts.unsilence(fp)) instead of
        # routing an imperative prompt to an agent that won't execute it. The
        # reject's only effect is a pure suppression-file operation; the
        # dashboard performs it server-side. See dashboard_api
        # `_medic_silence_fingerprint` / `_reconcile_medic_silence`.
        'fingerprint': fp,
        'prompt': prompt,
        'severity': 'warning',
        'dedup_identity': task_id,
        'suggested_envelope_for_approve': approve_env,
        'suggested_envelope_for_reject': reject_env,
    }
    return cee.emit_event(
        event_type='approval_request',
        agent='medic',
        task_id=task_id,
        payload=payload,
        ts=use_ts,
    )


def silence_false_positive(fingerprint: str, reason: str = '',
                           attempt: int = 1,
                           ttl_sec: Optional[float] = None,
                           remediation: str = '') -> dict:
    """Durably suppress a confirmed benign false-positive alert so it stops
    looping in Larry's inbox, AND emit exactly one actionable fix report so
    the unfixed root cause is never silently masked. Reversible (writes a
    suppression file only; mutates nothing on the system). Strict order,
    mirroring _act_restart:

      (a) gates  -- enable flag + both kill switches.
      (b) allowlist -- the fingerprint must match a `silenceable_subjects`
          pattern (substring). A fingerprint matching no pattern is REFUSED:
          Medic may only auto-silence proven-benign CLASSES, never an
          arbitrary alert.
      (c) idempotence -- if already silenced, succeed as a no-op (and do NOT
          re-emit the fix report; that keeps the notice one-shot).
      (d) perform -- larry_alerts.silence(fingerprint, ...).
      (e) verify  -- larry_alerts.is_silenced(fingerprint) is now True.
      (f) record  -- ledger outcome='acted' (classification='reversible').
      (g) couple  -- post ONE Approve/Reject decision to Larry's Approvals
          tab (event_type='approval_request') with the root cause /
          `remediation`. Silencing stops the symptom; this guarantees the
          bug Medic cannot fix itself still reaches Larry exactly once as an
          actionable decision (keep silenced vs unsilence).

    The silence is logged to medic-dispatcher.log for the audit trail and is
    reversible via larry_alerts.unsilence(fingerprint).
    """
    try:
        fp = fingerprint if isinstance(fingerprint, str) and fingerprint else ''
        if not fp:
            return _result(ACTION_SILENCE, '', str(fingerprint), ok=False,
                           outcome='skipped', reason='no-target',
                           detail='No fingerprint supplied; refusing.')
        try:
            attempt_int = int(attempt)
        except (TypeError, ValueError):
            attempt_int = 1

        # (a) gates
        ok, gate_reason = _gates_ok()
        if not ok:
            return _refuse(ACTION_SILENCE, fp, fp, attempt_int, gate_reason,
                           'A Medic gate (enable flag or a kill switch) is not '
                           'satisfied; no silence written.')

        # (b) allowlist -- only proven-benign false-positive classes
        patterns = _load_reversible_targets().get('silenceable_subjects', [])
        if not any(p in fp for p in patterns):
            return _refuse(
                ACTION_SILENCE, fp, fp, attempt_int, 'not-permitted',
                f'Fingerprint {fp} matches no silenceable_subjects pattern; '
                f'refusing to auto-silence. Escalate diagnose-only instead.')

        # (c) idempotence
        if larry_alerts.is_silenced(fp):
            return _result(ACTION_SILENCE, fp, fp, ok=True, outcome='acted',
                           reason='already-silenced',
                           detail=f'{fp} was already silenced; no-op.')

        # (d) perform -- reversible: writes a suppression file only
        wrote = larry_alerts.silence(
            fp, reason=(reason or 'medic-confirmed false positive'),
            ttl_sec=ttl_sec, by='medic')

        # (e) verify -- the file persisted AND, when the silence is meant to be
        # active right now, is_silenced agrees. A non-positive ttl_sec is an
        # immediately-expired (no-op) silence, so is_silenced is *expected* to
        # be False; only require the write to have succeeded there (don't read a
        # legitimately-expired silence as a write failure).
        expect_active = ttl_sec is None or ttl_sec > 0
        if not wrote or (expect_active and not larry_alerts.is_silenced(fp)):
            # The silence write was attempted and did not persist — a real
            # action failure. Record it toward the graduation streak (a no-op
            # unless silence-false-positive is a live registry template).
            _record_template_execution(ACTION_SILENCE, verified=False)
            return _refuse(
                ACTION_SILENCE, fp, fp, attempt_int, 'silence-write-failed',
                f'Could not persist a silence for {fp}; escalate diagnose-only.')

        # (f) record
        rec_source, rec_subject = _fp_parts(fp)
        notes = f'silenced {fp}: {reason}'[:300]
        medic_ledger.append_record(
            source=rec_source, subject=rec_subject, classification='reversible',
            outcome='acted', attempt=attempt_int, notes=notes)
        # Verified fresh silence -> a clean execution toward the graduation
        # streak. The (c) idempotence branch returned earlier, so this never
        # fires on an already-silenced no-op.
        _record_template_execution(ACTION_SILENCE, verified=True)
        _log('INFO', notes)

        # (g) couple the silence with ONE Approve/Reject decision on Larry's
        # Approvals tab. Silencing stops the inbox loop; this guarantees the
        # unfixed root cause still reaches Larry exactly once as an actionable
        # decision instead of being quietly masked. Only on this fresh-silence
        # path -- the (c) idempotence branch returned earlier, so the decision
        # never repeats on subsequent cycles.
        posted = _emit_silence_decision(fp, reason, remediation)
        _log('INFO', f'silence decision for {fp}: '
                     f'{"posted to Approvals tab" if posted else "not posted (chain_events unavailable)"}')

        # Match larry_alerts.silence semantics: only ttl_sec=None is permanent
        # (ttl_sec=0 expires immediately, it is NOT eternal).
        ttl_note = 'permanent' if ttl_sec is None else f'ttl={ttl_sec}s'
        decision_note = ('Approve/Reject decision posted to Approvals tab'
                         if posted else
                         'Approvals-tab decision NOT posted (chain_events unavailable)')
        return _result(
            ACTION_SILENCE, fp, fp, ok=True, outcome='acted', reason='silenced',
            detail=(f'{fp} silenced ({ttl_note}); {decision_note}. Reversible: '
                    f'larry_alerts.unsilence("{fp}") restores it.'))
    except Exception as e:  # never raise -- fail safe
        _log('ERROR', f'{ACTION_SILENCE} unexpected exception for '
                      f'{fingerprint}: {type(e).__name__}: {e}')
        return _result(ACTION_SILENCE, str(fingerprint), str(fingerprint),
                       ok=False, outcome='skipped', reason='exception',
                       detail=f'Unexpected error: {type(e).__name__}: {e}')


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
             'retrigger-watcher | silence-false-positive')
    parser.add_argument('--unit', '--target', dest='target', default='',
                        help='the daemon unit / inbox-watcher target')
    parser.add_argument('--fingerprint', default='', help='alert fingerprint')
    parser.add_argument('--attempt', type=int, default=1,
                        help='attempt number (prior_attempts + 1)')
    parser.add_argument('--reason', default='',
                        help='silence-false-positive: why this fingerprint is '
                             'a confirmed benign false positive')
    parser.add_argument('--ttl-sec', dest='ttl_sec', type=float, default=None,
                        help='silence-false-positive: optional TTL in seconds; '
                             'omit for a permanent silence')
    parser.add_argument('--remediation', default='',
                        help='silence-false-positive: the specific root-cause '
                             'fix to report to Larry alongside the silence '
                             '(what to patch at the source). Optional; a '
                             'generic instruction is sent if omitted.')
    args = parser.parse_args(argv)

    if args.action == ACTION_RESTART_DAEMON:
        result = restart_daemon(args.target, args.fingerprint, args.attempt)
    elif args.action in (ACTION_RETRIGGER_INBOX, ACTION_RETRIGGER_WATCHER):
        result = retrigger_inbox(args.target, args.fingerprint, args.attempt)
    elif args.action == ACTION_SILENCE:
        result = silence_false_positive(args.fingerprint, args.reason,
                                        args.attempt, args.ttl_sec,
                                        remediation=args.remediation)
    else:
        # Privileged / judgment / unknown action types never reach a handler.
        result = _result(
            args.action, args.target, args.fingerprint, ok=False,
            outcome='skipped', reason='unsupported-action',
            detail=(f'Action type {args.action!r} is not a reversible '
                    f'handler; medic_actions.py only performs restart-daemon, '
                    f'retrigger-inbox/retrigger-watcher, and '
                    f'silence-false-positive. Escalate via larry_alerts.py '
                    f'instead.'))
        _log('INFO', f'rejected unsupported action type {args.action!r}')

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
