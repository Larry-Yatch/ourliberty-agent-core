#!/usr/bin/env python3
"""cycle_prime_ledger.py — PRIME DIRECTIVE intervention/systemic-fix ledger.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 6.4 row shape + § 5.7
data sources + § 12.3 Check V/VI substrate references.

OQ1 (locked 2026-05-29): the ledger file is
``~/agents/blackboard/cycle-prime-ledger.jsonl`` — distinct from the
existing auto-fix log ``runbooks/cycle-actions.jsonl``. Naming the file
``cycle-prime-ledger.jsonl`` avoids the prior collision; the auto-fix log
is untouched.

Row schema::

    {
      "ts": "<iso8601 utc>",
      "iter": <int>,
      "tier": <int>,
      "kind": "intervention" | "systemic_fix" | "verification_pending"
              | "iter_clean",
      "intervention_id": "<str>",
      "fix_commit_sha": "<str>"|null,
      "chain_event_id": <int>|null,
      "verifies_at": "<iso8601>"|null,
      "verified_at": "<iso8601>"|null,
      "verification_anchor": "<str>"|null
    }

Decision II semantics (per spec § 4): a ``verification_pending`` row
auto-promotes to ``systemic_fix`` when its verifying signal appears
within 7d; ``promote_verification_pending`` performs the promotion by
appending a new ``systemic_fix`` row that references the original.

Stdlib only. Atomic write semantics for promotion (the append-only base
log can use simple appends; promotions write a new row, never rewrite).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

LEDGER_REL = 'blackboard/cycle-prime-ledger.jsonl'
LOG_REL = 'logs/cycle-prime-ledger.log'

VALID_KINDS = (
    'intervention',
    'systemic_fix',
    'verification_pending',
    # iter_clean is a non-intervention heartbeat: it marks that a cycle iter
    # ran and its checks were clean (no intervention happened). It is recorded
    # so the ledger keeps a per-iter liveness signal, but Check V's ratio +
    # per-template aggregation count only intervention/systemic_fix, so an
    # iter_clean row never pollutes the PRIME DIRECTIVE denominator. Clean iters
    # used to be (wrongly) logged as kind=intervention with an empty id — that
    # is the cause-(b) mislabel this kind fixes.
    'iter_clean',
)

# Recording a row of one of these kinds requires a template (the stable prefix
# of intervention_id). If a caller omits it, the write layer normalizes to the
# reserved UNCATEGORIZED_TEMPLATE rather than writing an untaggable empty-id row
# — so an untagged *real* intervention is preserved as a visible, countable,
# single-bucket data point instead of being silently lost.
TEMPLATE_REQUIRED_KINDS = ('intervention', 'systemic_fix')

# The "classify me" bucket. Never an auto-fix pattern, permanently non-
# graduating (config/auto-fix-patterns.json marks it permanent_guard; Check V
# refuses to graduate it). Every untagged required-kind row buckets here under
# ONE stable template so _template_of does not fragment them per row.
UNCATEGORIZED_TEMPLATE = 'uncategorized'

# An intervention_id is "<template>:<detail>". The template is the stable
# kebab-case prefix Check V aggregates on (pulse_check_v._template_of splits
# on the first ':'); the detail is the free-form variable part. Enforcing the
# template shape here is what makes per-template track record possible — a
# free-form id with no colon (or with the variable part folded into the
# prefix) fragments into a singleton per row and the promotion ladder dies.
TEMPLATE_RE = re.compile(r'^[a-z][a-z0-9-]*$')
RATIO_LOOKBACK_DAYS = 30
TREND_HALF_DAYS = 15        # trailing 15d vs prior 15d for trend direction
PROMOTE_WINDOW_DAYS = 7     # Decision II auto-promote window
TREND_RATIO_DELTA = 0.10    # ±10% band for "flat" classification

# Agent-run always_allowed tools (agents/pulse/TOOLS.md) that are modeled as
# graduatable registry templates (config/auto-fix-patterns.json). These are the
# high-frequency, reversible, benign automations the cycle agent performs by
# hand — unlike Medic's Python-executed actions (restart-daemon etc., recorded
# at act-time in medic_actions._record_template_execution, PR #832), the agent
# is the executor and its at-act-time event IS the intervention row it appends
# here. So the ledger append is the correct single instrument point for their
# graduation track record. The agent appends an intervention/systemic_fix row
# only when the tool SUCCEEDED (a failure is a different slug / carry), so this
# is a success-only signal by construction. Membership is checked BEFORE any
# registry read, so the hot iter_clean heartbeat and every non-graduation
# intervention pay nothing. Auto-merge is deliberately NOT here — its executor
# is Python (outbox_notifier / heal_pr_auto_merge), recorded there, so listing
# it would double-count.
GRADUATION_LEDGER_TEMPLATES = frozenset({
    'ff-main-when-behind',
    'enable-pr-auto-merge',
    'archive-duplicate-inbox-task',
})


def _ledger_path() -> Path:
    root = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
    return root / LEDGER_REL


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


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _atomic_append(record: dict[str, Any]) -> bool:
    """O_APPEND is atomic for JSONL records under PIPE_BUF (4096B on
    Linux); every row here is well under that. Open fresh per call so
    concurrent writers don't share a buffered handle."""
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + '\n'
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(line)
        return True
    except OSError as e:
        _log(f'append failed: {type(e).__name__}: {e}', 'WARN')
        return False


def _record_graduation_execution(kind: str, intervention_id: str) -> None:
    """Record a clean action-template execution for the agent-run graduatable
    tools (see ``GRADUATION_LEDGER_TEMPLATES``) toward Check V's promotion loop.

    This is the streak INPUT the loop was missing for the *agent's* high-frequency
    reversible automations — the mirror of ``medic_actions._record_template_execution``
    (PR #832) for the actions Medic does not execute. Success-only by construction:
    the agent appends an intervention/systemic_fix row only when the tool worked.

    Additive and best-effort by contract: it observes a row that was just written;
    it must NEVER change whether/how the ledger row is written, and must never
    raise (a track-record write failing cannot be allowed to fail an append). It
    is naturally start-clean — only actions recorded after this ships are counted.
    The cheap frozenset guard runs BEFORE any I/O so the hot iter_clean heartbeat
    and every non-graduation intervention pay nothing; the registry-membership
    gate + record + never-raise contract is single-sourced in
    ``alert_triage_state.record_clean_execution_if_registered``.

    Success-only by construction (the agent appends a row only on success). NOTE
    the exactly-one-owner invariant: templates listed here are AGENT-run tools
    that reach the ledger by appending here DIRECTLY. They must never ALSO be
    alert-triage Tier-1/2 templates — ``alert_triage_state.triage_alert`` records
    those itself right after its own ``append_action`` call, which would
    double-count. The two template namespaces are disjoint today; keep them so.
    """
    try:
        if kind not in TEMPLATE_REQUIRED_KINDS:
            return
        template = str(intervention_id or '').split(':', 1)[0]
        if template not in GRADUATION_LEDGER_TEMPLATES:
            return
        import alert_triage_state  # lazy: avoids import coupling at module load
        alert_triage_state.record_clean_execution_if_registered(template)
    except Exception as e:  # noqa: BLE001 — track-record must never break ledger
        _log(f'graduation-execution record for {intervention_id!r} failed: '
             f'{type(e).__name__}: {e}', 'WARN')


def canonical_intervention_id(template: str, detail: str = '') -> str:
    """Build a canonical intervention_id of the form ``<template>:<detail>``.

    ``template`` MUST be kebab-case (``^[a-z][a-z0-9-]*$``): lowercase, starts
    with a letter, no colon or whitespace. ``detail`` is the free-form variable
    part (may be empty, may itself contain ``:`` — only the first ``:`` delimits
    the template) and MUST NOT be folded into ``template``, or per-template
    aggregation in Check V breaks.

    Raises ``ValueError`` on a non-conforming ``template`` so a malformed id is
    never written — callers (the CLI ``--template`` path) surface this as a
    non-zero exit rather than silently appending an untaggable row.
    """
    if not isinstance(template, str) or not TEMPLATE_RE.match(template):
        raise ValueError(
            f'invalid intervention template {template!r}: must match '
            f'{TEMPLATE_RE.pattern} (kebab-case: lowercase, leading letter, '
            'no colon or whitespace). The variable part belongs in --detail.'
        )
    detail = '' if detail is None else str(detail)
    return f'{template}:{detail}'


def _normalize_intervention_id(kind: str, intervention_id: str,
                               *, iter_num: Optional[int],
                               detail: Optional[str]) -> str:
    """Enforce: a row whose kind requires a template MUST carry one.

    If ``kind`` is template-required and ``intervention_id`` is empty (the
    caller omitted ``--template``), normalize to ``uncategorized:<detail-or-iter>``
    so the data point is preserved, buckets under one stable template, and is
    visibly flagged for classification — instead of writing an untaggable
    empty-id row. Non-empty ids (legacy ``--payload`` path) and non-required
    kinds (``iter_clean``, ``verification_pending``) pass through untouched.
    """
    iid = str(intervention_id or '')
    if kind in TEMPLATE_REQUIRED_KINDS and not iid:
        if detail is not None and str(detail) != '':
            fallback = str(detail)
        elif iter_num is not None:
            fallback = f'iter-{int(iter_num)}'
        else:
            fallback = 'iter-0'
        iid = canonical_intervention_id(UNCATEGORIZED_TEMPLATE, fallback)
        _log(f'untagged {kind} row normalized to {iid!r} (no --template '
             'supplied); classify it and re-tag the action', 'WARN')
    return iid


def append_action(tier: int, kind: str, payload: dict[str, Any],
                  *, iter_num: Optional[int] = None,
                  detail: Optional[str] = None) -> dict[str, Any]:
    """Append one ledger row. Returns the written record (caller may keep
    the intervention_id for later promote_verification_pending calls).

    ``detail`` is an optional fallback used only when normalizing an untagged
    template-required row to the uncategorized bucket (see
    ``_normalize_intervention_id``)."""
    if kind not in VALID_KINDS:
        raise ValueError(f'invalid kind={kind!r}; expected one of {VALID_KINDS}')
    if not isinstance(tier, int) or tier < 1 or tier > 3:
        raise ValueError(f'invalid tier={tier!r}')
    intervention_id = _normalize_intervention_id(
        kind, str(payload.get('intervention_id') or ''),
        iter_num=iter_num, detail=detail)
    record: dict[str, Any] = {
        'ts': _now_iso(),
        'iter': int(iter_num) if iter_num is not None else 0,
        'tier': int(tier),
        'kind': kind,
        'intervention_id': intervention_id,
        'fix_commit_sha': payload.get('fix_commit_sha'),
        'chain_event_id': payload.get('chain_event_id'),
        'verifies_at': payload.get('verifies_at'),
        'verified_at': payload.get('verified_at'),
        'verification_anchor': payload.get('verification_anchor'),
    }
    if _atomic_append(record):
        _record_graduation_execution(kind, intervention_id)
    return record


def _read_rows(path: Optional[Path] = None) -> list[dict[str, Any]]:
    p = path or _ledger_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(p, encoding='utf-8') as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError as e:
        _log(f'read failed: {type(e).__name__}: {e}', 'WARN')
        return []
    return out


def _filter_window(rows: list[dict[str, Any]],
                   *, now: datetime,
                   days: int) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = _parse_ts(r.get('ts'))
        if ts is None:
            continue
        if ts >= cutoff:
            out.append(r)
    return out


def compute_ratio_30d(
    *,
    now: Optional[datetime] = None,
    rows: Optional[list[dict[str, Any]]] = None,
    lookback_days: int = RATIO_LOOKBACK_DAYS,
    half_days: int = TREND_HALF_DAYS,
) -> dict[str, Any]:
    """Trailing-30d ratio per α₁ § 6. Returns::

        {interventions, systemic_fixes, verification_pending,
         ratio, trend}

    Where:
      - ``ratio = interventions / systemic_fixes`` (string ``"N/A"`` if
        denominator is zero — explicit per spec).
      - ``trend in {improving, flat, worsening}`` from a 15d/15d split
        (improving = recent ratio < older by > 10%).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if rows is None:
        rows = _read_rows()

    in_window = _filter_window(rows, now=now, days=lookback_days)

    def _count(kind: str, recs: Iterable[dict[str, Any]]) -> int:
        return sum(1 for r in recs if r.get('kind') == kind)

    interventions = _count('intervention', in_window)
    systemic = _count('systemic_fix', in_window)
    pending = _count('verification_pending', in_window)
    ratio: Any
    if systemic == 0:
        ratio = 'N/A'
    else:
        ratio = interventions / systemic

    # Trend: 15d/15d split.
    recent = _filter_window(rows, now=now, days=half_days)
    older = [
        r for r in in_window
        if _parse_ts(r.get('ts')) is not None
        and _parse_ts(r.get('ts')) < now - timedelta(days=half_days)
    ]
    recent_i = _count('intervention', recent)
    recent_s = _count('systemic_fix', recent)
    older_i = _count('intervention', older)
    older_s = _count('systemic_fix', older)

    def _half_ratio(i: int, s: int) -> Optional[float]:
        return (i / s) if s > 0 else None

    rr = _half_ratio(recent_i, recent_s)
    or_ = _half_ratio(older_i, older_s)

    if rr is None or or_ is None or or_ == 0:
        trend = 'flat'
    else:
        delta = (rr - or_) / or_
        if delta < -TREND_RATIO_DELTA:
            trend = 'improving'   # fewer interventions per fix
        elif delta > TREND_RATIO_DELTA:
            trend = 'worsening'
        else:
            trend = 'flat'

    return {
        'interventions': interventions,
        'systemic_fixes': systemic,
        'verification_pending': pending,
        'ratio': ratio,
        'trend': trend,
    }


def promote_verification_pending(
    row_id: str,
    *,
    now: Optional[datetime] = None,
    rows: Optional[list[dict[str, Any]]] = None,
    window_days: int = PROMOTE_WINDOW_DAYS,
) -> Optional[dict[str, Any]]:
    """Decision II auto-promote: if a ``verification_pending`` row with
    ``intervention_id == row_id`` exists and its ``ts`` is within
    ``window_days`` of now, append a new ``systemic_fix`` row that
    references the original. Returns the new row, or None if no eligible
    pending row was found (already promoted, expired, or never existed).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if rows is None:
        rows = _read_rows()

    pending: Optional[dict[str, Any]] = None
    already_promoted = False
    cutoff = now - timedelta(days=window_days)
    for r in rows:
        if r.get('intervention_id') != row_id:
            continue
        kind = r.get('kind')
        if kind == 'verification_pending':
            ts = _parse_ts(r.get('ts'))
            if ts is None or ts < cutoff:
                continue
            pending = r
        elif kind == 'systemic_fix':
            # A later systemic_fix with the same id means we've already
            # promoted; don't double-write.
            already_promoted = True

    if pending is None or already_promoted:
        return None

    payload = {
        'intervention_id': row_id,
        'fix_commit_sha': pending.get('fix_commit_sha'),
        'chain_event_id': pending.get('chain_event_id'),
        'verifies_at': pending.get('verifies_at'),
        'verified_at': now.isoformat(),
        'verification_anchor': pending.get('verification_anchor'),
    }
    return append_action(
        tier=int(pending.get('tier', 1)),
        kind='systemic_fix',
        payload=payload,
        iter_num=pending.get('iter'),
    )


# -------------------- CLI --------------------


def _cli_ratio(_args) -> int:
    out = compute_ratio_30d()
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cli_append(args) -> int:
    payload = json.loads(args.payload) if args.payload else {}
    # Preferred path: separate --template / --detail flags. They are routed
    # through canonical_intervention_id, which validates the template shape and
    # rejects (non-zero exit, no row written) anything non-conforming. This is
    # the deterministic tagging the cycle prompt must use so Check V gets a real
    # per-template streak. The legacy --payload path stays lenient/untouched
    # (promote_verification_pending and other callers depend on append_action's
    # leniency).
    if args.template is not None:
        try:
            payload['intervention_id'] = canonical_intervention_id(
                args.template, args.detail or '')
        except ValueError as exc:
            print(f'error: {exc}', file=sys.stderr)
            return 2
    elif args.detail is not None and args.kind not in TEMPLATE_REQUIRED_KINDS:
        # For non-required kinds a bare --detail has nowhere to go; keep the
        # strict error. For required kinds it feeds the uncategorized fallback
        # below, so a real intervention that only knew its detail is preserved.
        print('error: --detail requires --template', file=sys.stderr)
        return 2
    rec = append_action(tier=args.tier, kind=args.kind, payload=payload,
                        iter_num=args.iter, detail=args.detail)
    print(json.dumps(rec))
    return 0


def _cli_promote(args) -> int:
    out = promote_verification_pending(args.row_id)
    if out is None:
        print(json.dumps({'promoted': False}))
        return 1
    print(json.dumps({'promoted': True, 'row': out}))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='cycle_prime_ledger.py',
                                     description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('ratio',
                   help='Print the trailing-30d ratio metric as JSON.')
    p_app = sub.add_parser('append', help='Append one row.')
    p_app.add_argument('--tier', required=True, type=int, choices=[1, 2, 3])
    p_app.add_argument('--kind', required=True, choices=list(VALID_KINDS))
    p_app.add_argument('--iter', type=int, default=0)
    p_app.add_argument('--template',
                       help='Action-template id (kebab-case, '
                            '^[a-z][a-z0-9-]*$). MANDATORY for --kind '
                            'intervention/systemic_fix: it is validated + '
                            'joined with --detail into "<template>:<detail>" so '
                            'Check V can aggregate a per-template streak. A '
                            'non-conforming template exits non-zero and writes '
                            'nothing. If omitted on a required kind, the row is '
                            'NOT dropped — it normalizes to '
                            '"uncategorized:<detail-or-iter>" (a visible, single '
                            'classify-me bucket). Clean iters use --kind '
                            'iter_clean and need no template.')
    p_app.add_argument('--detail', default=None,
                       help='Free-form variable part of the intervention_id '
                            '(e.g. the affected unit name). Requires --template.')
    p_app.add_argument('--payload', default='{}',
                       help='JSON object with intervention_id, '
                            'fix_commit_sha, chain_event_id, etc. Legacy path; '
                            'prefer --template/--detail for tagging.')
    p_prom = sub.add_parser('promote',
                            help='Auto-promote a verification_pending row.')
    p_prom.add_argument('row_id')
    args = parser.parse_args(argv)
    if args.cmd == 'ratio':
        return _cli_ratio(args)
    if args.cmd == 'append':
        return _cli_append(args)
    if args.cmd == 'promote':
        return _cli_promote(args)
    return 2


if __name__ == '__main__':
    sys.exit(main())
