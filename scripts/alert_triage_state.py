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

Atomic writes via tmp + replace. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
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
                       subject: Optional[str]) -> Optional[dict[str, Any]]:
    """Return the matched translation entry, or ``None`` if no match.

    Mirrors the table's own lookup_rule: source must match a top-level key
    exactly; then exact subject, else strip trailing ``:``-segments one at a
    time and retry (longest-prefix, first match wins). ``_schema`` is metadata,
    never a source.

    Returns the entry dict so callers can inspect directives such as
    ``never_silence``. A matched entry is always a (truthy) dict; a miss is
    ``None`` — so existing ``assertTrue``/``assertFalse`` callers stay correct."""
    if not source or source == '_schema':
        return None
    by_subject = translations.get(source)
    if not isinstance(by_subject, dict):
        return None
    if subject is None:
        return None
    key = subject
    while key:
        if key in by_subject:
            entry = by_subject[key]
            return entry if isinstance(entry, dict) else {}
        if ':' not in key:
            break
        key = key.rsplit(':', 1)[0]
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
    match = _translation_match(translations, source, subject)
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
    return 2


if __name__ == '__main__':
    sys.exit(main())
