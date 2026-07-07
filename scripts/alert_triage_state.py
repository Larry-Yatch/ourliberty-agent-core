#!/usr/bin/env python3
"""alert_triage_state.py — per-alert triage lifecycle state.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 3.0 (Check 0 alert
triage) + α₂ brief § 6.10 (lifecycle).

State file: ``~/agents/state/alert-triage.json``. One JSON object whose
keys are alert IDs and whose values are per-alert lifecycle rows.

Per-alert row schema::

    {
      "alert_id": "<str>",
      "tier": 1|2|3,
      "decision": "<str>",                # e.g. "dispatch", "snooze", "noop"
      "rationale": "<str>",
      "status": "pending"|"triaged-tier-N"|"action-dispatched"|"resolved",
      "triaged_at": <iso8601>|null,
      "dispatched_at": <iso8601>|null,
      "dispatch_target_agent": "<str>"|null,
      "dispatch_task_id": "<str>"|null,
      "resolved_at": <iso8601>|null,
      "resolution": "<str>"|null,
      "last_updated": <iso8601>
    }

Lifecycle: ``pending → triaged-tier-N → action-dispatched → resolved``
(per α₂ § 3.0). The functions below advance one transition each and are
intentionally additive: an unknown alert_id is created on the first
``record_triage`` call; ``mark_dispatched`` / ``mark_resolved`` no-op (
return False) if the prior state isn't present.

Phase B (durable Check 0 triage, ``docs/pulse-triage-phase-b-brief.md``) layers
a data-driven classifier on top of those lifecycle primitives: ``classify()``
reads ``config/auto-fix-patterns.json`` (the registry) + ``config/
alert-translations.json`` (the Tier-3 known-pattern table) and returns a tier
(1-4), a delivery ``route`` (stamped via ``larry_alerts.classify_route``), and a
decision. ``triage_alert()`` orchestrates classify → persist → (Tier-1) record a
tagged ``cycle_prime_ledger`` intervention so per-pattern track record accrues
for Check V (the B→C link), with an idempotency guard so a re-run on an
already-handled alert never double-acts.

Check 0's last-claimed ``larry-alerts.jsonl`` line watermark lives in its OWN
file (``~/agents/state/alert-triage-watermark.json``), NOT a field inside
alert-triage.json: ``read_state`` drops non-dict top-level keys and
``_write_state`` rewrites the whole object, so a co-located scalar watermark is
silently filtered then clobbered by the next lifecycle write. ``read_watermark``
/ ``write_watermark`` + the ``get-watermark`` / ``set-watermark`` CLI own that
separate store.

Atomic writes via tmp + replace. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import cycle_prime_ledger as cpl
import larry_alerts
from atomic_io import atomic_write_json

STATE_REL = 'state/alert-triage.json'
LOG_REL = 'logs/alert-triage-state.log'

# Check 0's last-claimed larry-alerts.jsonl line watermark. Deliberately a
# PHYSICALLY-SEPARATE file from STATE_REL: read_state() keeps only top-level keys
# whose value is a dict (the alert_id-keyed lifecycle rows), and _write_state
# rewrites the whole object — so a scalar watermark co-located inside
# alert-triage.json is silently dropped on read and clobbered on the next
# lifecycle write. Its own store means the lifecycle writes can never touch it.
WATERMARK_REL = 'state/alert-triage-watermark.json'
WATERMARK_KEY = 'last_claimed_line'

# Trailing rotating ISO-date suffix on a subject (e.g. weekly-2026-06-15,
# check-i-2026-06-15). Stripped in _translation_match so a stable prefix key
# (weekly / check-i) can match a date-rotating subject. Anchored to a leading
# '-' and the end of string so it only fires on a genuine -YYYY-MM-DD suffix,
# never on a ':'-delimited subject or an interior date.
_ISO_DATE_SUFFIX_RE = re.compile(r'-\d{4}-\d{2}-\d{2}.*$')

# Phase C — the per-template execution track-record store. Distinct from the
# per-alert lifecycle state above: keyed by action-template, it accrues one
# record per acted fix so Check V can compute a per-template clean streak. This
# is the streak INPUT that Phase B left unbuilt (docs/pulse-triage-phase-c-brief.md
# "the missing streak INPUT"). Shape:
#   {"action_templates": {"<template>": {"executions": [
#       {"outcome": "success"|"failure", "larry_correction_signal": bool, "ts": iso8601}
#   ]}}}
ACTION_TEMPLATE_EXEC_REL = 'state/action-template-executions.json'

# A recorded execution's outcome is one of these. "success" + a falsy
# larry_correction_signal is the only "clean" combination (the streak input).
VALID_OUTCOMES = ('success', 'failure')

# Config inputs for the data-driven § 6.6 decision table. Both are the same
# files Phase A (#279) shipped + #277 added; read at classify time, never copied.
_CONFIG_DIR = Path(__file__).resolve().parent.parent / 'config'
AUTO_FIX_PATTERNS_FILE = _CONFIG_DIR / 'auto-fix-patterns.json'
ALERT_TRANSLATIONS_FILE = _CONFIG_DIR / 'alert-translations.json'

# Tier 4 (novel/ambiguous) is added in Phase B; the lifecycle helper previously
# only knew the 3 acted tiers. record_triage accepts all four.
VALID_TIERS = (1, 2, 3, 4)

# Agent name stamped on a Tier-1 auto-fix dispatch row. The remediation is
# acted by Pulse / existing healers (Phase B does not build a dispatcher); this
# names the actor for the audit trail.
AUTO_FIX_AGENT = 'pulse-auto-fix'


def _state_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / STATE_REL


def _watermark_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / WATERMARK_REL


def _log_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / LOG_REL


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str, level: str = 'INFO') -> None:
    line = f'[{_now_iso()}] [{level}] {msg}'
    print(line, flush=True)
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def _preserve_corrupt_state(path: Path, raw: bytes) -> Optional[Path]:
    """Move a corrupt state file aside to a unique timestamped sidecar.

    Audit #37: returning {} on a parse error lets the very next _write_state
    atomically clobber every other alert's lifecycle row. Instead, rename the
    corrupt file to ``<name>.corrupt-<utc>`` (history recoverable for manual
    repair) and ALERT, then let the caller treat the live state as empty so
    triage isn't permanently stuck. Renaming (rather than copying) also means
    the next read sees a missing file → {} cleanly, so only ONE backup is made
    per corruption rather than one per tick. Best-effort: never raises.
    """
    backup: Optional[str] = None
    try:
        stamp = _now_iso().replace(':', '').replace('-', '')
        # mkstemp reserves a guaranteed-unique name even within the same second.
        fd, backup = tempfile.mkstemp(
            prefix=f'{path.name}.corrupt-{stamp}-', suffix='',
            dir=str(path.parent),
        )
        os.close(fd)
        os.replace(path, backup)
        _log(f'{path.name} CORRUPT ({len(raw)} bytes); preserved prior triage '
             f'state at {Path(backup).name} before treating as empty', 'ERROR')
        return Path(backup)
    except OSError as e:
        # Don't leave the empty mkstemp reservation behind if the rename failed.
        if backup is not None:
            try:
                os.unlink(backup)
            except OSError:
                pass
        _log(f'{path.name} corrupt and could not be preserved: {e}', 'ERROR')
        return None


def read_state() -> dict[str, dict[str, Any]]:
    """Atomic read. Returns {} on missing or corrupt.

    Audit #37: a single transient corruption used to return {} silently, and
    the next write then clobbered every other alert's lifecycle row. On a
    parse error we now PRESERVE the corrupt file to a timestamped sidecar and
    alert before returning {}, so prior history is recoverable while triage
    still makes forward progress (refusing to write would leave the triage
    state permanently stuck after one bad write)."""
    path = _state_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
    except OSError:
        # Transient I/O failure — the file may be intact on disk. Treat as
        # empty for this read but do NOT move it aside (we couldn't read its
        # bytes to preserve them); the next successful read recovers it.
        _log(f'{path.name} unreadable (I/O); treating as empty', 'WARN')
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _preserve_corrupt_state(path, raw)
        return {}
    if not isinstance(data, dict):
        # Valid JSON of the wrong shape (a top-level list/number/null from a
        # partial or buggy external write) is just as much a corruption as a
        # parse error: returning {} here would let the next _write_state clobber
        # every prior row. Preserve + alert on this path too (audit #37 gap
        # left by #366, which only handled JSONDecodeError).
        _preserve_corrupt_state(path, raw)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[k] = v
    return out


def _write_state(state: dict[str, dict[str, Any]]) -> None:
    """Atomic write via a unique tmp + fsync + os.replace. Never partial-write
    and never shares a tmp name with a concurrent writer (audit #62 class)."""
    atomic_write_json(_state_path(), state, indent=2, sort_keys=True)


def record_triage(alert_id: str, tier: int, decision: str,
                  rationale: str, route: Optional[str] = None,
                  template: Optional[str] = None) -> dict[str, Any]:
    """Move (or initialize) ``alert_id`` to ``triaged-tier-N``.
    Overwrites prior triage decisions (re-triage is expected when an
    alert resurfaces). ``route`` is the delivery destination
    (escalate|closure|digest) stamped by Check 0 per § 6.6; ``template`` is the
    registry action-template the signal mapped to (None for Tier 3/4). Returns
    the post-mutation row."""
    if not isinstance(alert_id, str) or not alert_id:
        raise ValueError('alert_id must be a non-empty string')
    if tier not in VALID_TIERS:
        raise ValueError(f'invalid tier={tier!r}')
    state = read_state()
    now = _now_iso()
    existing = state.get(alert_id, {})
    row: dict[str, Any] = {
        'alert_id': alert_id,
        'tier': int(tier),
        'decision': str(decision),
        'rationale': str(rationale),
        'route': route,
        'template': template,
        'status': f'triaged-tier-{tier}',
        'triaged_at': now,
        'dispatched_at': existing.get('dispatched_at'),
        'dispatch_target_agent': existing.get('dispatch_target_agent'),
        'dispatch_task_id': existing.get('dispatch_task_id'),
        'resolved_at': existing.get('resolved_at'),
        'resolution': existing.get('resolution'),
        'last_updated': now,
    }
    state[alert_id] = row
    _write_state(state)
    return row


def mark_dispatched(alert_id: str, dispatch_ts: str,
                    target_agent: str, task_id: str) -> bool:
    """Transition ``triaged-tier-N → action-dispatched``. Returns False
    (no-op) if the alert hasn't been triaged yet."""
    state = read_state()
    row = state.get(alert_id)
    if not row:
        _log(f'mark_dispatched: unknown alert_id={alert_id!r}', 'WARN')
        return False
    row['status'] = 'action-dispatched'
    row['dispatched_at'] = dispatch_ts
    row['dispatch_target_agent'] = target_agent
    row['dispatch_task_id'] = task_id
    row['last_updated'] = _now_iso()
    _write_state(state)
    return True


def mark_resolved(alert_id: str, resolved_ts: str,
                  resolution: str) -> bool:
    """Transition to ``resolved``. Returns False (no-op) if the alert
    isn't present at all. We do NOT enforce a strict
    triaged→dispatched→resolved ordering — Larry may resolve an alert
    directly (e.g., manual fix) without a dispatch step."""
    state = read_state()
    row = state.get(alert_id)
    if not row:
        _log(f'mark_resolved: unknown alert_id={alert_id!r}', 'WARN')
        return False
    row['status'] = 'resolved'
    row['resolved_at'] = resolved_ts
    row['resolution'] = resolution
    row['last_updated'] = _now_iso()
    _write_state(state)
    return True


# -------------------- Check 0 line watermark (dedicated store) --------------------


def read_watermark() -> Optional[int]:
    """Return the last-claimed larry-alerts.jsonl line, or None.

    None is the 'MISSING → claim trailing 100 lines as catchup' signal Check 0
    already handles, so EVERY failure mode degrades to None rather than raising:
    missing file, corrupt JSON, wrong top-level shape, or a missing/non-int
    ``last_claimed_line`` key. A bool is rejected (``isinstance(True, int)`` is
    True in Python, but a boolean watermark is meaningless)."""
    path = _watermark_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _log(f'{path.name} unreadable; treating watermark as missing', 'WARN')
        return None
    if not isinstance(data, dict):
        return None
    line = data.get(WATERMARK_KEY)
    if isinstance(line, bool) or not isinstance(line, int):
        return None
    return line


def write_watermark(line: int) -> None:
    """Persist ``line`` as the last-claimed watermark (read-modify-write).

    Loads the existing object if present so any other top-level keys survive,
    sets ``last_claimed_line``, then atomically replaces the file. Lives in its
    own store (``WATERMARK_REL``) that the alert_id-keyed lifecycle writes never
    touch, so it can't be clobbered by ``_write_state``."""
    if isinstance(line, bool) or not isinstance(line, int):
        raise ValueError(f'watermark line must be an int, got {line!r}')
    path = _watermark_path()
    doc: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if isinstance(existing, dict):
                doc = existing
        except (OSError, json.JSONDecodeError):
            # A corrupt watermark file shouldn't block a fresh write — overwrite
            # it with a clean object carrying the new line (read returns None on
            # corrupt anyway, so nothing of value is lost).
            _log(f'{path.name} corrupt on read-modify-write; overwriting', 'WARN')
    doc[WATERMARK_KEY] = int(line)
    atomic_write_json(path, doc, indent=2, sort_keys=True)


def _alerts_file_length() -> int:
    """Count newline-terminated lines of ``larry_alerts.ALERTS_FILE``.

    Returns 0 if the file is missing or unreadable — consistent with
    ``read_watermark``'s degrade-to-None philosophy: a length we cannot observe
    is treated as 0, which (since a concrete watermark is always >= 0) makes
    ``repair_watermark`` a safe no-op rather than a spurious reset on a transient
    read error. Referenced via the module attribute so tests can repoint it."""
    path = larry_alerts.ALERTS_FILE
    try:
        if not path.exists():
            return 0
        with path.open(encoding='utf-8') as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def repair_watermark() -> dict[str, Any]:
    """Self-heal a stale (too-large) watermark after alert-log compaction.

    The retention/compaction job periodically removes OLD lines from
    ``larry-alerts.jsonl``, shrinking the file. The watermark tracks ABSOLUTE
    line numbers, so after compaction ``watermark > file_length`` and Check 0's
    'read lines AFTER the watermark' yields nothing — every new alert is silently
    skipped until manual repair. This guard detects that rotation gap and resets
    the watermark to ``file_length`` exactly (Larry's explicit choice — NOT a
    trailing-N re-claim), so the next iter reads the new alerts.

    No-op unless the watermark is a concrete int strictly greater than the file
    length. A MISSING watermark (``read_watermark`` -> None) is left alone — the
    existing 'claim trailing 100 lines as catchup' path owns that case.

    Returns a machine-readable dict Pulse branches + journals on:
      - repaired: ``{"repaired": True, "old_watermark": int, "file_length": int,
        "new_watermark": int}``
      - no-op:    ``{"repaired": False, "old_watermark": int|None,
        "file_length": int}``
    """
    old = read_watermark()
    file_length = _alerts_file_length()
    if old is not None and old > file_length:
        # NOTE: do NOT _log() here — _log writes to stdout, and the repair-watermark
        # CLI's stdout MUST be a single parseable JSON object Pulse branches on.
        # The repair is self-reported in the returned dict and journaled by Pulse.
        write_watermark(file_length)
        return {
            'repaired': True,
            'old_watermark': old,
            'file_length': file_length,
            'new_watermark': file_length,
        }
    return {
        'repaired': False,
        'old_watermark': old,
        'file_length': file_length,
    }


# -------------------- per-template execution track record (Phase C) --------------------


def _exec_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / ACTION_TEMPLATE_EXEC_REL


def _read_executions_doc() -> dict[str, Any]:
    """Read the raw executions document. Returns the canonical empty shape on a
    missing/corrupt file — a lost track record degrades to "no streak" (the
    conservative direction: nothing graduates on missing data) rather than
    crashing the recorder."""
    path = _exec_path()
    if not path.exists():
        return {'action_templates': {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _log(f'{path.name} unreadable; treating executions as empty', 'WARN')
        return {'action_templates': {}}
    if not isinstance(data, dict) or not isinstance(data.get('action_templates'), dict):
        return {'action_templates': {}}
    return data


def _write_executions_doc(doc: dict[str, Any]) -> None:
    # Same unique-tmp + fsync atomic write as _write_state (audit #62 class):
    # avoid a fixed <path>.tmp two writers could collide on.
    atomic_write_json(_exec_path(), doc, indent=2, sort_keys=True)


def load_executions() -> dict[str, list[dict[str, Any]]]:
    """Return ``{template: [execution, ...]}`` — the authoritative track-record
    source Check V reads to compute per-template clean streaks. Empty dict on a
    missing/corrupt store."""
    doc = _read_executions_doc()
    out: dict[str, list[dict[str, Any]]] = {}
    for template, rec in doc.get('action_templates', {}).items():
        if not isinstance(template, str) or not isinstance(rec, dict):
            continue
        execs = rec.get('executions')
        out[template] = [e for e in execs if isinstance(e, dict)] \
            if isinstance(execs, list) else []
    return out


def record_action_template_execution(
    template: str, *, outcome: str = 'success',
    larry_correction_signal: bool = False,
    ts: Optional[str] = None,
) -> dict[str, Any]:
    """Append one execution record for ``template`` and return it.

    This is the streak INPUT (docs/pulse-triage-phase-c-brief.md): Check 0 calls
    it for BOTH Tier-1 auto-fixes AND Tier-2 approved-probation fixes so a
    probation pattern accrues a track record at all. A "clean" execution is
    ``outcome == "success"`` AND ``larry_correction_signal`` falsy.

    Side effect — auto-demotion (single-sourced in ``pulse_check_v``): when this
    is an ADVERSE execution (``outcome == "failure"`` OR ``larry_correction_signal``)
    against a currently-``graduated`` registry template, the recorder triggers an
    immediate, ungated demotion ``graduated → probation``. Losing trust is never
    gated, so it fires here at record time rather than waiting for the next Check V
    cycle. The demotion is lazy-imported to avoid an import cycle and is
    best-effort — a recording must never fail because the demotion hook errored."""
    if not isinstance(template, str) or not template:
        raise ValueError('template must be a non-empty string')
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f'invalid outcome={outcome!r}; expected one of {VALID_OUTCOMES}')
    record = {
        'outcome': outcome,
        'larry_correction_signal': bool(larry_correction_signal),
        'ts': ts or _now_iso(),
    }
    doc = _read_executions_doc()
    templates = doc.setdefault('action_templates', {})
    rec = templates.setdefault(template, {})
    execs = rec.setdefault('executions', [])
    if not isinstance(execs, list):
        execs = rec['executions'] = []
    execs.append(record)
    _write_executions_doc(doc)

    adverse = outcome == 'failure' or bool(larry_correction_signal)
    if adverse:
        try:
            import pulse_check_v
            pulse_check_v.demote_on_adverse_execution(
                template, reason=('failed execution' if outcome == 'failure'
                                  else 'Larry-correction'),
                correction=bool(larry_correction_signal))
        except Exception as e:  # never let the demotion hook break recording
            _log(f'demotion hook for {template!r} failed: '
                 f'{type(e).__name__}: {e}', 'WARN')
    return record


def record_clean_execution_if_registered(template: str, *,
                                         verified: bool = True) -> None:
    """Registry-gated, best-effort, never-raise wrapper around
    ``record_action_template_execution`` for ACT-TIME executors.

    This is the single shared home for the "observe an action that already
    happened, feed its outcome to Check V's graduation streak, but never affect
    whether/how the action runs" contract (the PR #832 pattern). Every act-time
    recorder — Medic (restart/retrigger/silence), the auto-merge paths
    (outbox_notifier / heal_pr_auto_merge), and the agent-run ledger tools
    (cycle_prime_ledger) — delegates here so the load-bearing rules live in ONE
    place instead of drifting across copies:

      * The registry-membership gate MUST run before recording, because
        ``record_action_template_execution`` appends unconditionally — a
        non-registry action must be a silent no-op, not a streak row.
      * ``verified`` maps to the outcome vocabulary (success/failure). An adverse
        (``verified=False``) execution of an already-graduated template triggers
        the immediate ungated demotion inside the recorder — so pass
        ``verified=False`` ONLY for a RELIABLE action-quality failure (a restart
        that didn't come back active, a silence file that didn't persist), never
        for a transient/infra failure (a network blip, a still-running required
        check) which would revoke earned trust on noise.
      * It must never raise: a track-record write failing cannot be allowed to
        fail the action it is merely observing.
    """
    try:
        if template not in load_registry():
            return
        record_action_template_execution(
            template, outcome='success' if verified else 'failure')
    except Exception as e:  # noqa: BLE001 — track-record must never break caller
        _log(f'clean-execution record for {template!r} failed: '
             f'{type(e).__name__}: {e}', 'WARN')


# -------------------- data-driven § 6.6 classification (Phase B) --------------------


def load_registry(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Load ``config/auto-fix-patterns.json`` as ``{template: record}``.

    Returns ``{}`` on a missing/corrupt file — the conservative default: with no
    registry, no signal maps to a template, so registry patterns fall through to
    Tier 4 (ask) rather than being silently auto-fixed."""
    p = path or AUTO_FIX_PATTERNS_FILE
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        _log(f'{p.name} unreadable; treating registry as empty', 'WARN')
        return {}
    out: dict[str, dict[str, Any]] = {}
    for rec in data.get('patterns', []) if isinstance(data, dict) else []:
        if isinstance(rec, dict) and isinstance(rec.get('template'), str):
            out[rec['template']] = rec
    return out


def load_translations(path: Optional[Path] = None) -> dict[str, Any]:
    """Load ``config/alert-translations.json`` (raw source→subject→meta map).

    Returns ``{}`` on a missing/corrupt file — no Tier-3 silences then, so
    everything falls through to the registry / Tier-4 ask. Fail toward asking,
    never toward silently swallowing a signal."""
    p = path or ALERT_TRANSLATIONS_FILE
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        _log(f'{p.name} unreadable; treating translations as empty', 'WARN')
        return {}
    return data if isinstance(data, dict) else {}


def _translation_match(translations: dict[str, Any], source: str,
                       subject: Optional[str],
                       intent: Optional[str] = None,
                       kind: Optional[str] = None
                       ) -> Optional[dict[str, Any]]:
    """Return the matched translation entry, or ``None`` if no match.

    Mirrors the table's own lookup_rule: source must match a top-level key
    exactly; then exact subject, else strip trailing ``:``-segments one at a
    time and retry (longest-prefix, first match wins). If that loop also
    misses, strip a trailing ``-YYYY-MM-DD`` ISO-date suffix from the derived
    key and retry once — this lets a stable prefix key (e.g. ``weekly`` /
    ``check-i``) match a date-rotating subject (``weekly-2026-06-15``) without
    enumerating every date. The step is inert unless a ``-YYYY-MM-DD`` suffix
    is present AND the stripped key (which must DIFFER from the original) exists
    in the source's ``by_subject`` map, so no existing match is altered.
    ``_schema`` is metadata, never a source.

    Some producers (e.g. outbox-notifier success alerts) carry the pattern in
    ``intent`` and leave ``subject`` None. When ``subject is None`` we fall back
    to ``intent`` as the lookup key, then apply the same longest-prefix match.
    Other producers (e.g. outbox-notifier ``approval_request`` delivery
    confirmations) carry the pattern only in ``kind`` and leave BOTH ``subject``
    and ``intent`` None. When both are None we fall back to ``kind`` as the
    lookup key. Precedence is subject -> intent -> kind (most-specific first):
    a present ``subject`` stays authoritative and never falls through to
    ``intent`` or ``kind``.

    Final step: if exact + ':'-prefix-strip both miss, consult a source-level
    ``'*'`` catch-all entry that matches ANY subject under that source. It is
    consulted only AFTER the loop misses, so more-specific subject entries
    (e.g. ``pulse-cycle/cycle-blocked``, reached via the prefix-strip loop)
    still win. ``never_silence`` semantics are preserved identically: a ``'*'``
    entry tagged ``never_silence`` is returned as a truthy dict and classify
    Gate 1 routes it to surface, not mute. The ``'*'`` fallback applies to
    whichever source's ``by_subject`` map was selected, independent of the
    derived key — so a source with no ``'*'`` entry still returns ``None`` on a
    miss, preserving the existing 3-arg callers' behavior.

    Returns the entry dict so callers can inspect directives such as
    ``never_silence``. A matched entry is always a (truthy) dict; a miss is
    ``None`` — so existing ``assertTrue``/``assertFalse`` callers stay correct."""
    if not source or source == '_schema':
        return None
    by_subject = translations.get(source)
    if not isinstance(by_subject, dict):
        return None
    key = subject if subject is not None \
        else (intent if intent is not None else kind)
    while key:
        if key in by_subject:
            entry = by_subject[key]
            return entry if isinstance(entry, dict) else {}
        if ':' not in key:
            break
        key = key.rsplit(':', 1)[0]
    # Trailing rotating ISO-date strip: a stable prefix key matches a
    # date-rotating subject (weekly-<date>, check-i-<date>). Re-derive from the
    # original key (subject -> intent -> kind), not the ':'-stripped remainder.
    base = subject if subject is not None \
        else (intent if intent is not None else kind)
    if base:
        stripped = _ISO_DATE_SUFFIX_RE.sub('', base)
        if stripped != base and stripped in by_subject:
            entry = by_subject[stripped]
            return entry if isinstance(entry, dict) else {}
    wildcard = by_subject.get('*')
    if wildcard is not None:
        return wildcard if isinstance(wildcard, dict) else {}
    return None


def classify(alert: dict[str, Any], *, registry: dict[str, dict[str, Any]],
             translations: dict[str, Any],
             route_fn: Optional[Callable[[str, Optional[str], bool], str]] = None
             ) -> dict[str, Any]:
    """Pure § 6.6 classification — no side effects.

    Evaluates the gates in order (first match wins, per spec § 3.0 / § 6.6):

      1. (source, subject) in ``translations``        → Tier 3 (silence→digest)
      2. registry template, permanent_guard OR
         state != "graduated"                         → Tier 2 (ask→escalate)
      3. registry template, state == "graduated" AND
         NOT permanent_guard                          → Tier 1 (auto-fix→route)
      4. fallthrough (no template, no translation)    → Tier 4 (novel→escalate)

    A signal maps to a registry template via its explicit ``template`` tag (the
    producing healer's canonical remediation id), NOT a subject matcher — the
    registry has no subject field and Phase B invents none.

    Returns ``{tier, route, decision, rationale, template}``. ``route`` comes
    from ``route_fn`` (default ``larry_alerts.classify_route``) for Tiers 1/2/4;
    Tier 3 is hard-set to ``digest`` (a silenced known pattern is journal-only
    and must never produce a closure DM, even on a significant subject)."""
    rf = route_fn or larry_alerts.classify_route
    source = str(alert.get('source') or '')
    subject = alert.get('subject')
    subject = str(subject) if subject is not None else None
    template = alert.get('template')
    template = str(template) if template is not None else None

    # Gate 1 — Tier 3: Larry already approved silence on this known pattern.
    # Exception: an entry tagged ``never_silence`` is a known pattern Larry wants
    # *translated but still surfaced* (e.g. a dark pulse check). Such an entry
    # must not be muted to digest — it falls through to Tier 4 so it escalates
    # with its translation intact.
    # ``subject`` stays untouched below (route_fn / is_significant / rationale);
    # the intent fallback is scoped to this translation lookup only.
    match = _translation_match(translations, source, subject,
                               alert.get('intent'), alert.get('kind'))
    if match is not None and not match.get('never_silence'):
        return {
            'tier': 3,
            'route': 'digest',
            'decision': 'silence',
            'rationale': 'known-pattern match in alert-translations.json',
            'template': None,
        }

    rec = registry.get(template) if template else None
    if rec is not None:
        guarded = bool(rec.get('permanent_guard')) \
            or rec.get('state') != 'graduated'
        if guarded:
            # Gate 2 — Tier 2: probation or a permanent_guard floor → ask.
            why = 'permanent_guard floor' if rec.get('permanent_guard') \
                else f"state={rec.get('state')!r} (not graduated)"
            return {
                'tier': 2,
                'route': rf(source, subject, False),
                'decision': 'ask',
                'rationale': f'registry template {template!r} guarded: {why}',
                'template': template,
            }
        # Gate 3 — Tier 1: graduated + reversible-non-guarded → auto-fix.
        return {
            'tier': 1,
            'route': rf(source, subject, True),
            'decision': 'auto-fix',
            'rationale': f'registry template {template!r} graduated (non-guarded)',
            'template': template,
        }

    # Gate 4 — Tier 4: novel/ambiguous → ask Larry for triage guidance.
    # A ``never_silence`` translation match lands here too: known pattern, but
    # deliberately surfaced rather than muted.
    if match is not None:
        rationale = ('known never-silence pattern in alert-translations.json: '
                     'translated but surfaced, not muted')
    else:
        rationale = 'novel: no registry template and no translation match'
    return {
        'tier': 4,
        'route': rf(source, subject, False),
        'decision': 'ask',
        'rationale': rationale,
        'template': None,
    }


def triage_alert(alert_id: str, alert: dict[str, Any], *, iter_num: int = 0,
                 registry: Optional[dict[str, dict[str, Any]]] = None,
                 translations: Optional[dict[str, Any]] = None,
                 route_fn: Optional[Callable[[str, Optional[str], bool], str]] = None,
                 outcome: str = 'success',
                 larry_correction_signal: bool = False,
                 apply_approved_fix: bool = False,
                 ) -> dict[str, Any]:
    """Durable, idempotent Check 0 orchestration for one signal.

    classify → persist the lifecycle row → act per tier:
      - Tier 1 (auto-fix): record a TAGGED ``cycle_prime_ledger`` intervention
        (``template = pattern id`` — the B→C track-record link), record a Phase-C
        execution (``outcome`` / ``larry_correction_signal``) so the per-template
        streak accrues, and advance the row to ``action-dispatched``. The
        remediation itself is acted by Pulse / existing healers.
      - Tier 2 WITH ``apply_approved_fix`` (Larry approved this probation
        proposal and the fix is being applied THIS call): record a Phase-C
        execution + advance to ``action-dispatched``. This is what lets a
        probation pattern earn a clean streak toward graduation.
      - Tier 2 WITHOUT ``apply_approved_fix`` / Tier 4: leave at
        ``triaged-tier-N`` awaiting Larry (the default Phase-B behavior).
      - Tier 3: silence IS the resolution → advance directly to ``resolved``.

    ``outcome`` (``success``|``failure``) and ``larry_correction_signal`` describe
    the acted fix's result; the defaults (success, uncorrected) are the clean
    common case. A recorded failure/correction of a graduated template
    auto-demotes it immediately (handled inside
    ``record_action_template_execution``).

    Idempotency: if the alert is already ``action-dispatched`` or ``resolved``,
    return the existing row unchanged — no re-classify, no re-record, no second
    ledger/execution write. Re-running an iter never double-acts.

    ``registry`` / ``translations`` default to the live config files; tests
    inject fixtures. Returns the post-mutation row."""
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f'invalid outcome={outcome!r}; expected one of {VALID_OUTCOMES}')
    existing = read_state().get(alert_id)
    if existing and existing.get('status') in ('action-dispatched', 'resolved'):
        return existing

    reg = registry if registry is not None else load_registry()
    trans = translations if translations is not None else load_translations()
    result = classify(alert, registry=reg, translations=trans, route_fn=route_fn)

    record_triage(alert_id, tier=result['tier'], decision=result['decision'],
                  rationale=result['rationale'], route=result['route'],
                  template=result['template'])

    now = _now_iso()
    if result['tier'] == 1:
        detail = str(alert.get('subject') or alert_id)
        iid = cpl.canonical_intervention_id(result['template'], detail)
        cpl.append_action(tier=1, kind='intervention',
                          payload={'intervention_id': iid}, iter_num=iter_num)
        record_action_template_execution(
            result['template'], outcome=outcome,
            larry_correction_signal=larry_correction_signal, ts=now)
        mark_dispatched(alert_id, dispatch_ts=now,
                        target_agent=AUTO_FIX_AGENT, task_id=iid)
    elif result['tier'] == 2 and apply_approved_fix and result['template']:
        record_action_template_execution(
            result['template'], outcome=outcome,
            larry_correction_signal=larry_correction_signal, ts=now)
        mark_dispatched(alert_id, dispatch_ts=now,
                        target_agent=AUTO_FIX_AGENT,
                        task_id=cpl.canonical_intervention_id(
                            result['template'], str(alert.get('subject') or alert_id)))
    elif result['tier'] == 3:
        mark_resolved(alert_id, resolved_ts=now,
                      resolution='tier-3 silence (known pattern)')

    return read_state()[alert_id]


# -------------------- CLI --------------------


def _cli_read(_args) -> int:
    print(json.dumps(read_state(), indent=2))
    return 0


def _cli_triage(args) -> int:
    row = record_triage(args.alert_id, args.tier, args.decision, args.rationale)
    print(json.dumps(row))
    return 0


def _cli_dispatch(args) -> int:
    ok = mark_dispatched(args.alert_id, args.dispatch_ts,
                         args.target_agent, args.task_id)
    return 0 if ok else 1


def _cli_resolve(args) -> int:
    ok = mark_resolved(args.alert_id, args.resolved_ts, args.resolution)
    return 0 if ok else 1


def _cli_classify(args) -> int:
    """Read-only: print the § 6.6 classification for an alert JSON (no writes)."""
    alert = json.loads(args.alert)
    print(json.dumps(classify(alert, registry=load_registry(),
                              translations=load_translations())))
    return 0


def _cli_triage_alert(args) -> int:
    """Durable + idempotent: classify, persist, and (Tier-1) record the ledger
    link for one alert JSON. This is the backend Check 0 calls per iter."""
    alert = json.loads(args.alert)
    row = triage_alert(args.alert_id, alert, iter_num=args.iter)
    print(json.dumps(row))
    return 0


def _cli_get_watermark(_args) -> int:
    """Print the watermark int, or ``MISSING`` (catchup signal) when absent."""
    wm = read_watermark()
    print('MISSING' if wm is None else wm)
    return 0


def _cli_set_watermark(args) -> int:
    write_watermark(args.line)
    return 0


def _cli_repair_watermark(_args) -> int:
    """Print ONE JSON object describing the rotation-gap repair (or no-op) so
    Pulse can branch + journal. Runs FIRST in Check 0, before get-watermark."""
    print(json.dumps(repair_watermark()))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='alert_triage_state.py',
                                     description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('read', help='Print current state JSON.')
    p_t = sub.add_parser('triage', help='Record a triage decision.')
    p_t.add_argument('--alert-id', required=True)
    p_t.add_argument('--tier', required=True, type=int, choices=list(VALID_TIERS))
    p_t.add_argument('--decision', required=True)
    p_t.add_argument('--rationale', required=True)
    p_c = sub.add_parser('classify',
                         help='Print the § 6.6 tier+route for an alert (no writes).')
    p_c.add_argument('--alert', required=True, help='Alert object as JSON.')
    p_ta = sub.add_parser('triage-alert',
                          help='Durable+idempotent classify→persist→ledger-link.')
    p_ta.add_argument('--alert-id', required=True)
    p_ta.add_argument('--alert', required=True, help='Alert object as JSON.')
    p_ta.add_argument('--iter', type=int, default=0)
    p_d = sub.add_parser('dispatched', help='Mark an alert as dispatched.')
    p_d.add_argument('--alert-id', required=True)
    p_d.add_argument('--dispatch-ts', required=True)
    p_d.add_argument('--target-agent', required=True)
    p_d.add_argument('--task-id', required=True)
    p_r = sub.add_parser('resolved', help='Mark an alert as resolved.')
    p_r.add_argument('--alert-id', required=True)
    p_r.add_argument('--resolved-ts', required=True)
    p_r.add_argument('--resolution', required=True)
    sub.add_parser('get-watermark',
                   help='Print the last-claimed line watermark (or MISSING).')
    p_sw = sub.add_parser('set-watermark',
                          help='Set the last-claimed line watermark.')
    p_sw.add_argument('--line', required=True, type=int)
    sub.add_parser('repair-watermark',
                   help='Reset a stale (too-large) watermark after log '
                        'compaction; prints a JSON repair/no-op report.')
    args = parser.parse_args(argv)
    if args.cmd == 'read':
        return _cli_read(args)
    if args.cmd == 'triage':
        return _cli_triage(args)
    if args.cmd == 'classify':
        return _cli_classify(args)
    if args.cmd == 'triage-alert':
        return _cli_triage_alert(args)
    if args.cmd == 'dispatched':
        return _cli_dispatch(args)
    if args.cmd == 'resolved':
        return _cli_resolve(args)
    if args.cmd == 'get-watermark':
        return _cli_get_watermark(args)
    if args.cmd == 'set-watermark':
        return _cli_set_watermark(args)
    if args.cmd == 'repair-watermark':
        return _cli_repair_watermark(args)
    return 2


if __name__ == '__main__':
    sys.exit(main())
