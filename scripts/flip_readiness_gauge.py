#!/usr/bin/env python3
"""flip_readiness_gauge.py — the default-deny -> autonomy "flip-readiness doorbell".

Spec: agents/beacon/specs/flip-readiness-gauge.md (adopted via PR #861).

Larry locked the default-deny -> autonomy flip behind FIVE measurable criteria.
Nothing computed them, so the go-ahead depended on a human remembering to check
each one — the silent-miss failure the completeness program exists to close.
This gauge computes all five weekly from live substrate and rings ONE
approval-shaped doorbell DM the moment they all go green. It MEASURES and
ANNOUNCES only — it changes no config and flips nothing itself (spec § 5/§ 6).

The five criteria (spec § 4; thresholds are Larry-locked constants, read-only):
  1. Escalation precision      >= 90%   over trailing 30d
  2. Backstop-caught misses    == 0      over trailing 4wk
  3. Verified auto-fix templates >= 3 distinct, each >= 20 runs AND >= 95%
                                verifier-confirmed success (post-PR-1 honest ledger)
  4. Over-silence audit        green (no ~100%-silence high-volume signature)
  5. Projected post-flip approval volume <= current ask-rate

Substrate mapping + build-time formula reconciliation (spec § 8). The § 4
thresholds were transcribed from the parked-card note; where the live row shape
forced a formula choice we made the most conservative honest call and document
it here for Mirror to verify against design § 3 intent:

  * Criterion 1 (escalation precision): over decision-outcome-ledger rows whose
    ``outcome`` is ``approved``/``rejected`` in the 30d window,
    precision = approved / (approved + rejected). A rejected escalation is a
    false escalation (the automation surfaced something Larry declined). Zero
    escalations in the window is INDETERMINATE, not vacuously green — you cannot
    assert >= 90% precision from no evidence, and the flip must not proceed on a
    blank window.

  * Criterion 3 (verified templates): the live ``action-template-executions.json``
    rows carry ``outcome:"success"`` + a ``larry_correction_signal`` bool but NO
    explicit ``verifier_confirmed``/``unverified`` field the spec assumed. We
    operationalize "verifier-confirmed success" as
    ``outcome == 'success' AND NOT larry_correction_signal`` (the correction
    signal is the honest post-hoc "this run needed Larry to fix it" marker PR-1
    data carries). Only executions on/after the PR-1 merge (2026-07-08) count.
    A template qualifies at >= MIN_RUNS post-PR-1 executions AND a
    confirmed-success rate >= MIN_SUCCESS_RATE; the criterion is green at
    >= MIN_TEMPLATES qualifying templates.  <-- § 8 flag surfaced in the PR body.

  * Criterion 2 (backstop-caught misses): a "miss" = automation-should-have-
    caught-but-a-backstop-did. The authoritative backstop-catch sources are the
    stall/obligation ledgers under ~/agents/state (BACKSTOP_LEDGERS below); each
    obligation OPENED within the 4wk window is one caught miss. A MISSING ledger
    file legitimately means "no obligations were ever opened" (0 catches, not
    dark); a PRESENT-but-unreadable ledger is dark -> indeterminate.

  * Criterion 5 (projected post-flip approval volume): current_ask_rate is Check
    XIV's fleet ``ask_rate``; the projection is the share of in-window ledger
    decisions that were NOT auto-approved by rule (i.e. still needed a human, so
    would still reach Larry post-flip). Green iff projected <= current — the flip
    must not increase Larry's load. Directional per § 8.3; Mirror verifies intent.

Emission model (spec § 5, order-fragile-gauge precedent — DM ONLY on state
change):
  * Silent (artifact + heartbeat only) while the all-green level is unchanged.
  * not-all-green -> all-green transition: ring the doorbell exactly ONCE (one
    approval-shaped DM). A subsequent still-green run rings nothing.
  * all-green -> regression (a green criterion falls back): one warning DM.
  The last level is persisted in a small state file so transitions are computed,
  never re-fired.

Partial-data contract (spec § 5): every substrate read is try/except'd. A dark
input marks its criterion(s) ``indeterminate`` (NEVER green), records
``substrate.<x> == 'error'`` in the artifact, and the run still writes +
heartbeats + exits 0. ``all_green`` requires every criterion GENUINELY green — an
indeterminate is never green. A substrate dark for 2 consecutive runs escalates
once. Only an artifact-write failure is non-zero.

Stdlib only. No LLM calls. Deterministic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
REPO_ROOT = Path(__file__).resolve().parent.parent

LEDGER_FILE = AGENTS_ROOT / 'state' / 'decision-outcome-ledger.jsonl'
TEMPLATES_FILE = AGENTS_ROOT / 'state' / 'action-template-executions.json'
XIV_ARTIFACT_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-xiv'
STATE_DIR = AGENTS_ROOT / 'state'
ARTIFACT_DIR = AGENTS_ROOT / 'blackboard' / 'flip-readiness'
STATE_FILE = ARTIFACT_DIR / 'gauge-state.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'flip-readiness-gauge.log'

CHECK_ID = 'flip-readiness'

# ------- Larry-locked thresholds (spec § 4; read-only — the gauge never tunes) --
CRIT1_PRECISION_MIN = 0.90
CRIT1_WINDOW_DAYS = 30

CRIT2_WINDOW_DAYS = 28
CRIT2_MAX_MISSES = 0

CRIT3_MIN_TEMPLATES = 3
CRIT3_MIN_RUNS = 20
CRIT3_MIN_SUCCESS_RATE = 0.95

# Criterion 4 is a boolean over Check XIV's over-silence surface (0 findings=green).
# Criterion 5 compares a projection to the current ask-rate (window shared w/ C1).
CRIT5_WINDOW_DAYS = 30

# PR-1 ("turn on what's built") merged 2026-07-08 — the honest-ledger boundary.
# Criterion 3 counts only executions on/after this instant (pre-PR-1 rows carried
# the default-success lie and are excluded from the verified streak).
PR1_CUTOFF = datetime(2026, 7, 8, tzinfo=timezone.utc)

RETENTION_DAYS = 182                 # ~26 weeks self-pruned (XIV precedent)
DARK_ESCALATE_AFTER = 2              # consecutive dark runs on one substrate

# Authoritative backstop-catch event sources (spec § 8.4). Each is a stall/
# obligation ledger keyed by task/PR id -> record carrying an ``opened_at`` (the
# instant the backstop had to catch a miss automation should have handled).
BACKSTOP_LEDGERS: list[tuple[str, str, str]] = [
    ('no-session-revision', 'no-session-revision-ledger.json', 'opened_at'),
    ('rebase-obligation', 'rebase-obligation-ledger.json', 'opened_at'),
]

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from atomic_io import atomic_write_json  # noqa: E402


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


# -------------------- timestamp helper --------------------


def _parse_ts(ts_str: Any) -> Optional[datetime]:
    if not isinstance(ts_str, str) or not ts_str:
        return None
    s = ts_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# -------------------- criterion result --------------------


@dataclass
class CriterionResult:
    """One flip criterion's outcome. ``green`` is tri-state: True (met), False
    (not met), or None (indeterminate — a dark/insufficient substrate; NEVER
    counts toward all_green)."""
    key: str
    label: str
    green: Optional[bool]
    value: Optional[float]
    threshold: float
    gap: Optional[float]
    detail: str
    substrate_status: str  # 'ok' | 'empty' | 'error'

    def as_dict(self) -> dict[str, Any]:
        return {
            'key': self.key,
            'label': self.label,
            'green': self.green,
            'indeterminate': self.green is None,
            'value': self.value,
            'threshold': self.threshold,
            'gap': self.gap,
            'detail': self.detail,
            'substrate_status': self.substrate_status,
        }


# -------------------- substrate loaders --------------------


def load_ledger_rows(
    path: Optional[Path] = None,
) -> tuple[list[dict[str, Any]], str]:
    """Read the decision-outcome ledger (jsonl). Returns (rows, status).

    status: 'ok' (>=1 row), 'empty' (readable, no rows), 'error' (dark: missing
    or unreadable). A missing ledger is dark — we could not observe it at all.
    """
    path = path if path is not None else LEDGER_FILE
    if not path.exists():
        log(f'{path} missing — dark', 'WARN')
        return [], 'error'
    rows: list[dict[str, Any]] = []
    try:
        with open(path, errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError as e:
        log(f'read {path} failed: {e}', 'WARN')
        return [], 'error'
    return rows, ('ok' if rows else 'empty')


def load_templates(
    path: Optional[Path] = None,
) -> tuple[dict[str, Any], str]:
    """Read action-template-executions.json. Returns (action_templates, status)."""
    path = path if path is not None else TEMPLATES_FILE
    if not path.exists():
        log(f'{path} missing — dark', 'WARN')
        return {}, 'error'
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'read {path} failed: {e}', 'WARN')
        return {}, 'error'
    if not isinstance(obj, dict):
        return {}, 'error'
    templates = obj.get('action_templates')
    if not isinstance(templates, dict):
        return {}, 'error'
    return templates, ('ok' if templates else 'empty')


def latest_xiv_artifact(
    xiv_dir: Optional[Path] = None,
) -> tuple[dict[str, Any], str]:
    """Read the most-recent Check XIV artifact. Returns (artifact, status).

    A missing directory / no artifact / unreadable file is dark ('error'). An
    XIV artifact that was itself a dark run (its own ``sources.log == 'error'``)
    carries empty metric shells, so criteria 4/5 cannot be observed -> dark.
    """
    xiv_dir = xiv_dir if xiv_dir is not None else XIV_ARTIFACT_DIR
    try:
        candidates = sorted(xiv_dir.glob('check-xiv-*.json'))
    except OSError:
        return {}, 'error'
    if not candidates:
        log(f'no Check XIV artifact under {xiv_dir} — dark', 'WARN')
        return {}, 'error'
    latest = candidates[-1]
    try:
        obj = json.loads(latest.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'read {latest} failed: {e}', 'WARN')
        return {}, 'error'
    if not isinstance(obj, dict):
        return {}, 'error'
    sources = obj.get('sources')
    if isinstance(sources, dict) and sources.get('log') == 'error':
        log(f'{latest} is itself a dark XIV run — criteria 4/5 unobservable', 'WARN')
        return {}, 'error'
    return obj, 'ok'


def load_backstop_ledgers(
    state_dir: Optional[Path] = None,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Load the enumerated backstop-catch ledgers. Returns (by_label, any_dark).

    Each value is the list of obligation records. A MISSING file yields an empty
    list (legitimately: no obligations were ever opened). A PRESENT-but-unreadable
    file sets any_dark=True (criterion 2 -> indeterminate).
    """
    state_dir = state_dir if state_dir is not None else STATE_DIR
    by_label: dict[str, list[dict[str, Any]]] = {}
    any_dark = False
    for label, fname, _tsfield in BACKSTOP_LEDGERS:
        path = state_dir / fname
        if not path.exists():
            by_label[label] = []
            continue
        try:
            obj = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(f'backstop ledger {path} unreadable: {e} — dark', 'WARN')
            any_dark = True
            by_label[label] = []
            continue
        if isinstance(obj, dict):
            by_label[label] = [v for v in obj.values() if isinstance(v, dict)]
        elif isinstance(obj, list):
            by_label[label] = [v for v in obj if isinstance(v, dict)]
        else:
            any_dark = True
            by_label[label] = []
    return by_label, any_dark


# -------------------- criterion computations (pure) --------------------


def compute_criterion_1(
    rows: list[dict[str, Any]], status: str, *, now: datetime,
) -> CriterionResult:
    """Escalation precision >= 90% over trailing 30d."""
    key, label, thr = 'escalation_precision', 'Escalation precision', CRIT1_PRECISION_MIN
    if status == 'error':
        return CriterionResult(key, label, None, None, thr, None,
                               'decision ledger dark', status)
    cutoff = now - timedelta(days=CRIT1_WINDOW_DAYS)
    approved = rejected = 0
    for r in rows:
        outcome = r.get('outcome')
        if outcome not in ('approved', 'rejected'):
            continue
        ts = _parse_ts(r.get('ts'))
        if ts is None or ts < cutoff:
            continue
        if outcome == 'approved':
            approved += 1
        else:
            rejected += 1
    denom = approved + rejected
    if denom == 0:
        return CriterionResult(
            key, label, None, None, thr, None,
            'no escalation decisions in the 30d window — precision unobservable',
            status)
    precision = round(approved / denom, 4)
    return CriterionResult(
        key, label, precision >= thr, precision, thr, round(precision - thr, 4),
        f'{approved} approved / {rejected} rejected over {CRIT1_WINDOW_DAYS}d '
        f'(precision {precision:.1%})',
        status)


def compute_criterion_2(
    by_label: dict[str, list[dict[str, Any]]], any_dark: bool, *, now: datetime,
) -> CriterionResult:
    """Backstop-caught misses == 0 over trailing 4wk."""
    key, label, thr = 'backstop_caught_misses', 'Backstop-caught misses', CRIT2_MAX_MISSES
    if any_dark:
        return CriterionResult(key, label, None, None, thr, None,
                               'a backstop ledger was present but unreadable', 'error')
    cutoff = now - timedelta(days=CRIT2_WINDOW_DAYS)
    tsfield_by_label = {lab: ts for lab, _f, ts in BACKSTOP_LEDGERS}
    misses = 0
    hits: list[str] = []
    for lab, records in by_label.items():
        tsfield = tsfield_by_label.get(lab, 'opened_at')
        for rec in records:
            ts = _parse_ts(rec.get(tsfield))
            if ts is not None and ts >= cutoff:
                misses += 1
                hits.append(lab)
    detail = (f'{misses} backstop catch(es) in {CRIT2_WINDOW_DAYS}d'
              + (f' ({", ".join(sorted(set(hits)))})' if hits else ''))
    return CriterionResult(key, label, misses <= thr, misses, thr, float(misses),
                           detail, 'ok')


def compute_criterion_3(
    templates: dict[str, Any], status: str,
) -> CriterionResult:
    """Verified auto-fix templates: >= 3 distinct, each >= 20 post-PR-1 runs AND
    >= 95% verifier-confirmed success (correction-signal proxy — see module doc)."""
    key, label, thr = 'verified_templates', 'Verified auto-fix templates', CRIT3_MIN_TEMPLATES
    if status == 'error':
        return CriterionResult(key, label, None, None, thr, None,
                               'action-template-executions dark', status)
    qualifying: list[str] = []
    per_template: list[str] = []
    for name, body in templates.items():
        if not isinstance(body, dict):
            continue
        execs = body.get('executions')
        if not isinstance(execs, list):
            continue
        total = 0
        confirmed = 0
        for ex in execs:
            if not isinstance(ex, dict):
                continue
            ts = _parse_ts(ex.get('ts'))
            if ts is None or ts < PR1_CUTOFF:
                continue  # pre-PR-1 rows excluded from the honest streak
            total += 1
            if ex.get('outcome') == 'success' and not ex.get('larry_correction_signal'):
                confirmed += 1
        if total == 0:
            continue
        rate = confirmed / total
        meets = total >= CRIT3_MIN_RUNS and rate >= CRIT3_MIN_SUCCESS_RATE
        per_template.append(f'{name}: {confirmed}/{total} conf ({rate:.0%})'
                            + ('*' if meets else ''))
        if meets:
            qualifying.append(name)
    n = len(qualifying)
    detail = (f'{n} template(s) qualify (>= {CRIT3_MIN_RUNS} post-PR-1 runs & '
              f'>= {CRIT3_MIN_SUCCESS_RATE:.0%} confirmed): '
              + '; '.join(per_template) if per_template
              else 'no post-PR-1 template executions')
    return CriterionResult(key, label, n >= thr, n, thr, float(n - thr), detail, status)


def compute_criterion_4(
    xiv: dict[str, Any], status: str,
) -> CriterionResult:
    """Over-silence audit green: no ~100%-silence high-volume signature flagged."""
    key, label = 'over_silence_audit', 'Over-silence audit'
    if status == 'error':
        return CriterionResult(key, label, None, None, 0.0, None,
                               'Check XIV over-silence surface dark', status)
    findings = xiv.get('over_silence_findings')
    findings = findings if isinstance(findings, list) else []
    n = len(findings)
    named = ', '.join(
        f'{f.get("source")}/"{f.get("signature")}"'
        for f in findings[:3] if isinstance(f, dict))
    detail = (f'{n} over-silence finding(s)' + (f': {named}' if named else '')
              if n else 'no over-silence findings')
    return CriterionResult(key, label, n == 0, n, 0.0, float(n), detail, status)


def compute_criterion_5(
    xiv: dict[str, Any], xiv_status: str,
    rows: list[dict[str, Any]], ledger_status: str, *, now: datetime,
) -> CriterionResult:
    """Projected post-flip approval volume <= current ask-rate.

    current_ask_rate = Check XIV fleet ask_rate. projected = share of in-window
    ledger decisions NOT auto-approved by rule (still need a human -> still reach
    Larry post-flip). Green iff projected <= current. Directional per § 8.3.
    """
    key, label = 'projected_approval_volume', 'Projected post-flip approval volume'
    if xiv_status == 'error' or ledger_status == 'error':
        return CriterionResult(key, label, None, None, 0.0, None,
                               'Check XIV ask_rate or decision ledger dark', 'error')
    fleet = xiv.get('fleet')
    current = fleet.get('ask_rate') if isinstance(fleet, dict) else None
    if not isinstance(current, (int, float)):
        return CriterionResult(key, label, None, None, 0.0, None,
                               'Check XIV fleet ask_rate absent', 'error')
    cutoff = now - timedelta(days=CRIT5_WINDOW_DAYS)
    total = needs_human = 0
    for r in rows:
        if r.get('outcome') not in ('approved', 'rejected'):
            continue
        ts = _parse_ts(r.get('ts'))
        if ts is None or ts < cutoff:
            continue
        total += 1
        if 'auto_approved' not in str(r.get('notes') or ''):
            needs_human += 1
    if total == 0:
        return CriterionResult(
            key, label, None, None, round(float(current), 4), None,
            'no in-window decisions — post-flip projection unobservable', 'ok')
    projected = round(needs_human / total, 4)
    current = round(float(current), 4)
    return CriterionResult(
        key, label, projected <= current, projected, current,
        round(projected - current, 4),
        f'projected ask-rate {projected:.1%} ({needs_human}/{total} needed a '
        f'human) vs current {current:.1%}',
        'ok')


# -------------------- artifact + state --------------------


def build_artifact(
    criteria: list[CriterionResult], *, now: datetime,
    substrate_status: dict[str, str],
) -> dict[str, Any]:
    all_green = all(c.green is True for c in criteria)
    return {
        'as_of': now.isoformat(),
        'all_green': all_green,
        'window': {
            'escalation_precision_days': CRIT1_WINDOW_DAYS,
            'backstop_misses_days': CRIT2_WINDOW_DAYS,
            'projected_approval_days': CRIT5_WINDOW_DAYS,
            'pr1_cutoff': PR1_CUTOFF.isoformat(),
        },
        'criteria': {c.key: c.as_dict() for c in criteria},
        'substrate': substrate_status,
    }


def artifact_path_for(now: datetime) -> Path:
    return ARTIFACT_DIR / f'flip-readiness-{now.date().isoformat()}.json'


def _artifact_is_valid_sentinel(path: Path) -> bool:
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(obj, dict) and bool(obj.get('as_of'))


def read_state() -> dict[str, Any]:
    try:
        obj = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def write_state(state: dict[str, Any]) -> None:
    try:
        atomic_write_json(STATE_FILE, state, indent=2)
    except OSError as e:
        log(f'state write failed: {e}', 'WARN')


def prune_old_artifacts(*, now: datetime, retention_days: int = RETENTION_DAYS) -> int:
    cutoff = now.date() - timedelta(days=retention_days)
    removed = 0
    try:
        entries = list(ARTIFACT_DIR.glob('flip-readiness-*.json'))
    except OSError:
        return 0
    for p in entries:
        stem = p.name[len('flip-readiness-'):-len('.json')]
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue
        if d < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# -------------------- emission --------------------


def emit_alert(
    *, severity: str, message: str, subject: str,
    route: str = 'escalate', suggested_action: Optional[str] = None,
    needs_larry: bool = False,
) -> bool:
    """Thin wrapper over larry_alerts.append_alert (tests monkeypatch this)."""
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='flip-readiness-gauge',
            severity=severity,
            message=message,
            subject=subject,
            route=route,
            suggested_action=suggested_action,
            needs_larry=needs_larry,
        )
    except Exception as e:  # noqa: BLE001 — a DM failure must never crash the gauge
        log(f'emit_alert failed: {type(e).__name__}: {e}', 'WARN')
        return False


def format_doorbell(artifact: dict[str, Any]) -> str:
    date_str = artifact['as_of'][:10]
    lines = [
        f'Flip-readiness: all 5 gates GREEN as of {date_str}. '
        'Consider the default-deny -> autonomy flip.',
        '',
    ]
    for c in artifact['criteria'].values():
        lines.append(f'  ✓ {c["label"]}: {c["detail"]}')
    lines.append('')
    lines.append('The gauge measures only — YOU make the flip. Nothing has been '
                 'changed.')
    return '\n'.join(lines)


def format_regression(artifact: dict[str, Any], regressed: list[CriterionResult]) -> str:
    date_str = artifact['as_of'][:10]
    lines = [
        f'Flip-readiness REGRESSION as of {date_str}: a gate that was green has '
        'slipped. The all-green state we reported earlier no longer holds.',
        '',
    ]
    for c in regressed:
        status = 'indeterminate' if c.green is None else 'red'
        lines.append(f'  ✗ {c.label} ({status}): {c.detail}')
    lines.append('')
    lines.append('No config changed — this is a measurement warning; re-check '
                 'before acting on any earlier flip-ready signal.')
    return '\n'.join(lines)


def handle_transition(
    artifact: dict[str, Any], criteria: list[CriterionResult],
    prev_state: dict[str, Any], *, now: datetime,
) -> dict[str, Any]:
    """Compute the doorbell/regression DM from the state transition + return the
    new state. Silent while the all-green level is unchanged."""
    all_green = artifact['all_green']
    prev_all_green = bool(prev_state.get('last_all_green'))

    if all_green and not prev_all_green:
        emit_alert(
            severity='info', route='escalate', needs_larry=True,
            subject='flip-readiness-doorbell',
            message=format_doorbell(artifact),
            suggested_action=(
                'Review ~/agents/blackboard/flip-readiness/ and decide whether '
                'to make the default-deny -> autonomy flip. The gauge only rings.'),
        )
        log('DOORBELL rung — all 5 criteria green (transition).')
    elif prev_all_green and not all_green:
        regressed = [c for c in criteria if c.green is not True]
        emit_alert(
            severity='warning', route='escalate', needs_larry=True,
            subject='flip-readiness-regression',
            message=format_regression(artifact, regressed),
            suggested_action='Inspect ~/agents/blackboard/flip-readiness/ for the slipped gate.')
        log('REGRESSION warning — all-green slipped back.')
    else:
        log(f'No transition (all_green={all_green}, prev={prev_all_green}); silent.')

    new_state = dict(prev_state)
    new_state['last_all_green'] = all_green
    new_state['last_run_as_of'] = now.isoformat()
    if all_green and not prev_all_green:
        new_state['last_doorbell_as_of'] = now.isoformat()
    return new_state


def handle_dark_escalation(
    substrate_status: dict[str, str], prev_state: dict[str, Any],
    new_state: dict[str, Any],
) -> None:
    """Track per-substrate consecutive dark runs; escalate once at the threshold."""
    prev_dark = prev_state.get('consecutive_dark') or {}
    dark: dict[str, int] = {}
    for name, status in substrate_status.items():
        if status == 'error':
            n = int(prev_dark.get(name, 0)) + 1
            dark[name] = n
            if n == DARK_ESCALATE_AFTER:
                emit_alert(
                    severity='warning', route='escalate',
                    subject=f'flip-readiness-dark:{name}',
                    message=(
                        f'Flip-readiness gauge: substrate `{name}` has been dark '
                        f'for {n} consecutive runs — the criteria it feeds cannot '
                        'be computed, so the gauge cannot confirm flip-readiness. '
                        'Investigate before the next firing.'),
                    suggested_action=f'ls -l ~/agents/state ~/agents/blackboard/pulse-check-xiv; tail {LOG_FILE}')
        else:
            dark[name] = 0
    new_state['consecutive_dark'] = dark


# -------------------- main --------------------


def run_gauge(*, now: datetime, prev_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure-ish orchestration: read substrates, compute criteria, build artifact,
    compute the transition. Returns (artifact, new_state). No artifact/DM IO for
    the artifact itself is done here — the caller persists. DMs fire via
    emit_alert (monkeypatched in tests)."""
    ledger_rows, ledger_status = load_ledger_rows()
    templates, templates_status = load_templates()
    xiv, xiv_status = latest_xiv_artifact()
    backstops, backstops_dark = load_backstop_ledgers()

    c1 = compute_criterion_1(ledger_rows, ledger_status, now=now)
    c2 = compute_criterion_2(backstops, backstops_dark, now=now)
    c3 = compute_criterion_3(templates, templates_status)
    c4 = compute_criterion_4(xiv, xiv_status)
    c5 = compute_criterion_5(xiv, xiv_status, ledger_rows, ledger_status, now=now)
    criteria = [c1, c2, c3, c4, c5]

    substrate_status = {
        'ledger': ledger_status,
        'templates': templates_status,
        'xiv': xiv_status,
        'backstops': 'error' if backstops_dark else 'ok',
    }
    artifact = build_artifact(criteria, now=now, substrate_status=substrate_status)
    new_state = handle_transition(artifact, criteria, prev_state, now=now)
    handle_dark_escalation(substrate_status, prev_state, new_state)
    return artifact, new_state


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print; write no artifact/state, ring no DM.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass same-day idempotency (re-run for today).')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    target_path = artifact_path_for(now)

    if (target_path.exists() and _artifact_is_valid_sentinel(target_path)
            and not args.force and not args.dry_run):
        log(f'flip-readiness gauge already ran today ({now.date().isoformat()}); '
            'skipping (use --force to re-run).')
        return 0

    prev_state = read_state()

    if args.dry_run:
        # Dry run must not mutate state or fire DMs. Compute against a copy and
        # print; suppress emission by neutralizing emit_alert for this call.
        global emit_alert
        real_emit = emit_alert
        emit_alert = lambda **kw: True  # noqa: E731
        try:
            artifact, _new_state = run_gauge(now=now, prev_state=dict(prev_state))
        finally:
            emit_alert = real_emit
        print(json.dumps(artifact, indent=2))
        return 0

    artifact, new_state = run_gauge(now=now, prev_state=prev_state)

    try:
        atomic_write_json(target_path, artifact, indent=2)
    except OSError as e:
        log(f'artifact write failed: {e}', 'ERROR')
        return 1

    write_state(new_state)
    prune_old_artifacts(now=now)

    green = sum(1 for c in artifact['criteria'].values() if c['green'] is True)
    log(f'flip-readiness gauge complete: {green}/5 green, '
        f'all_green={artifact["all_green"]}, substrate={artifact["substrate"]}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check as _hb_run_check
    sys.exit(_hb_run_check(CHECK_ID, main, log_fn=log))
