#!/usr/bin/env python3
"""pulse_check_v.py — Tier-1 action-template trust analyzer.

Spec: agents/beacon/specs/pulse-cycle-upgrade.md § 12.3 (Check V) +
§ 5.2 / § 5.3 (action-template guard list).

Monthly cadence (runs on the first Monday of the month). Data substrate:
``~/agents/blackboard/cycle-prime-ledger.jsonl`` — the PRIME DIRECTIVE
ledger written by ``cycle_prime_ledger.py``. The auto-fix log
``runbooks/cycle-actions.jsonl`` is NOT the substrate (OQ1 resolution).

Two proposal-firing rules per spec § 12.3 bullet for Check V:

  1. ``graduate`` — for each action-template with >= 10 dispatches in
     trailing 90d AND zero Larry-modifications, propose removing from
     the guard list (template has earned trust).
  2. ``add_to_guard`` — for each non-guarded template that triggered a
     Larry-correction within 30d, propose adding to the guard list.

A row's action-template name is read from ``intervention_id``'s prefix
before the first ``:`` (e.g. ``restart-daemon:beacon`` → template
``restart-daemon``). A row counts as a Larry-modification if
``payload.larry_modified`` is true on the ledger row, OR if a separate
``intervention`` row with the same ``intervention_id`` carries
``payload.kind == "larry_correction"``.

Artifact: ``~/agents/blackboard/pulse-check-v-proposals/check-v-<month>.json``
where ``<month>`` is the ISO YYYY-MM of the run. Stdlib only. No LLM.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

AGENTS_ROOT = Path(
    os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents'))
)
LEDGER_FILE = AGENTS_ROOT / 'blackboard' / 'cycle-prime-ledger.jsonl'
PROPOSALS_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-v-proposals'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-v.log'
REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_LIST_CONFIG = REPO_ROOT / 'config' / 'action-template-guard-list.json'

GRADUATE_LOOKBACK_DAYS = 90
GRADUATE_MIN_DISPATCHES = 10
ADD_LOOKBACK_DAYS = 30

# --- Phase C promotion loop (the live graduation path) ---
# The registry is the state-of-record for state / permanent_guard / reversible;
# the executions store is the state-of-record for the per-template track record.
# Check V reconciles the two: it derives clean_streak from executions and writes
# it back to the registry as a cache, then proposes graduation.
REGISTRY_FILE = REPO_ROOT / 'config' / 'auto-fix-patterns.json'
# Authoritative track-record source, written by
# alert_triage_state.record_action_template_execution(). Single reader = this
# module's consecutive_clean_streak(). Path mirrors alert_triage_state's
# ACTION_TEMPLATE_EXEC_REL so the two stay pinned to the same file.
EXECUTIONS_FILE = AGENTS_ROOT / 'state' / 'action-template-executions.json'
# Larry's threshold: ~3 clean consecutive reversible executions. Supersedes the
# legacy GRADUATE_MIN_DISPATCHES=10 dispatch-count rule (spec § 6.6,
# _schema.graduate_min_clean_streak). The legacy run_check path is retained as a
# back-compat shim; the LIVE graduation path is the promotion loop below.
GRADUATE_MIN_CLEAN_STREAK = 3

# Reserved templates that can NEVER graduate, regardless of track record. The
# ledger normalizes any untagged intervention/systemic-fix into the
# ``uncategorized`` bucket (cycle_prime_ledger.UNCATEGORIZED_TEMPLATE); it is a
# "classify me" placeholder, not an auto-fix action, so a graduate proposal for
# it would be meaningless. config/auto-fix-patterns.json also marks it
# permanent_guard=true; this is the enforcement at the Check V layer.
NEVER_GRADUATE_TEMPLATES = frozenset({'uncategorized'})


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
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


def month_anchor(d: date) -> str:
    return f'{d.year:04d}-{d.month:02d}'


def artifact_path_for_month(anchor: str) -> Path:
    return PROPOSALS_DIR / f'check-v-{anchor}.json'


@dataclass
class TemplateStats:
    template: str
    dispatch_count_90d: int = 0
    larry_modifications_90d: int = 0
    larry_corrections_30d: int = 0


@dataclass
class Proposal:
    template: str
    kind: str                            # 'graduate' or 'add_to_guard'
    rationale: str
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckVResult:
    proposals: list[Proposal]
    template_count: int
    as_of_iso: str
    month_anchor: str


def _template_of(row: dict[str, Any]) -> str:
    iid = row.get('intervention_id') or ''
    if not isinstance(iid, str) or not iid:
        return ''
    # Convention: "template[:detail]". Empty detail allowed; the prefix
    # before ":" is the template name.
    return iid.split(':', 1)[0]


def _is_larry_modification(row: dict[str, Any]) -> bool:
    """True iff this row was modified by Larry (set on the ledger row by
    Pulse when she observed the operator-side correction)."""
    payload = row.get('payload') if isinstance(row.get('payload'), dict) else row
    if not isinstance(payload, dict):
        return False
    return bool(payload.get('larry_modified'))


def _is_larry_correction(row: dict[str, Any]) -> bool:
    if row.get('kind') != 'intervention':
        return False
    payload = row.get('payload') if isinstance(row.get('payload'), dict) else row
    if not isinstance(payload, dict):
        return False
    return payload.get('kind') == 'larry_correction'


def load_guard_list(path: Path = GUARD_LIST_CONFIG) -> set[str]:
    """Optional — if the guard-list config file exists, return its
    contents; otherwise return an empty set so every template is treated
    as non-guarded. The config file is owned by Larry / approved
    proposals; β does not create it."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log(f'guard-list config unreadable; assuming empty', 'WARN')
        return set()
    if isinstance(data, list):
        return {str(x) for x in data if isinstance(x, str)}
    if isinstance(data, dict):
        templates = data.get('guarded_templates')
        if isinstance(templates, list):
            return {str(x) for x in templates if isinstance(x, str)}
    return set()


def _read_ledger(path: Path = LEDGER_FILE) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError as e:
        log(f'read {path} failed: {e}', 'WARN')
        return []
    return out


def compute_template_stats(
    rows: Iterable[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, TemplateStats]:
    """Aggregate rows by template within the relevant lookback windows."""
    cutoff_90 = now - timedelta(days=GRADUATE_LOOKBACK_DAYS)
    cutoff_30 = now - timedelta(days=ADD_LOOKBACK_DAYS)
    by_template: dict[str, TemplateStats] = defaultdict(lambda: TemplateStats(''))
    for row in rows:
        ts = _parse_ts(row.get('ts'))
        if ts is None:
            continue
        template = _template_of(row)
        if not template:
            continue
        stats = by_template[template]
        if not stats.template:
            stats.template = template
        if ts >= cutoff_90 and row.get('kind') in ('intervention', 'systemic_fix'):
            stats.dispatch_count_90d += 1
            if _is_larry_modification(row):
                stats.larry_modifications_90d += 1
        if ts >= cutoff_30 and _is_larry_correction(row):
            stats.larry_corrections_30d += 1
    return dict(by_template)


def run_check(
    *,
    rows: Optional[list[dict[str, Any]]] = None,
    guard_list: Optional[set[str]] = None,
    now: Optional[datetime] = None,
) -> CheckVResult:
    """Pure entry: given ledger rows + guard-list, return the proposals."""
    if now is None:
        now = datetime.now(timezone.utc)
    rows = rows or []
    guard_list = guard_list if guard_list is not None else set()

    by_template = compute_template_stats(rows, now=now)
    proposals: list[Proposal] = []

    for template, stats in sorted(by_template.items()):
        # Rule 1: graduate (template is guarded + trusted).
        if (
            template not in NEVER_GRADUATE_TEMPLATES
            and template in guard_list
            and stats.dispatch_count_90d >= GRADUATE_MIN_DISPATCHES
            and stats.larry_modifications_90d == 0
        ):
            proposals.append(Proposal(
                template=template,
                kind='graduate',
                rationale=(
                    f'{template} dispatched {stats.dispatch_count_90d}x '
                    f'in trailing {GRADUATE_LOOKBACK_DAYS}d with 0 Larry '
                    'modifications — propose removing from guard list.'
                ),
                stats={
                    'dispatch_count_90d': stats.dispatch_count_90d,
                    'larry_modifications_90d': stats.larry_modifications_90d,
                },
            ))
        # Rule 2: add_to_guard (template non-guarded + recent correction).
        if (
            template not in guard_list
            and stats.larry_corrections_30d > 0
        ):
            proposals.append(Proposal(
                template=template,
                kind='add_to_guard',
                rationale=(
                    f'{template} triggered '
                    f'{stats.larry_corrections_30d} Larry-correction(s) '
                    f'in trailing {ADD_LOOKBACK_DAYS}d — propose moving '
                    'INTO the guard list.'
                ),
                stats={
                    'larry_corrections_30d': stats.larry_corrections_30d,
                    'dispatch_count_90d': stats.dispatch_count_90d,
                },
            ))

    return CheckVResult(
        proposals=proposals,
        template_count=len(by_template),
        as_of_iso=now.isoformat(),
        month_anchor=month_anchor(now.date()),
    )


def build_artifact(result: CheckVResult) -> dict[str, Any]:
    return {
        'as_of': result.as_of_iso,
        'month_anchor': result.month_anchor,
        'check': 'V',
        'template_count': result.template_count,
        'graduate_lookback_days': GRADUATE_LOOKBACK_DAYS,
        'graduate_min_dispatches': GRADUATE_MIN_DISPATCHES,
        'add_lookback_days': ADD_LOOKBACK_DAYS,
        'proposals': [
            {
                'template': p.template,
                'kind': p.kind,
                'rationale': p.rationale,
                'stats': p.stats,
            }
            for p in result.proposals
        ],
        'applied': False,
    }


def write_artifact(artifact: dict[str, Any]) -> Path:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = artifact_path_for_month(artifact['month_anchor'])
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(artifact, indent=2))
    tmp.replace(path)
    return path


def format_digest(artifact: dict[str, Any]) -> str:
    proposals = artifact.get('proposals') or []
    date_str = artifact['as_of'][:10]
    if not proposals:
        return (
            f'Check V ({date_str}) — no action-template trust changes '
            f'this month (templates evaluated: {artifact["template_count"]}).'
        )
    lines = [
        f'Check V ({date_str}) — '
        f'{len(proposals)} action-template proposal(s):',
        '',
    ]
    for p in proposals:
        lines.append(f'- {p["template"]} ({p["kind"]}): {p["rationale"]}')
    lines.append('')
    lines.append(
        f'Approve: reply `approve check-v-update-{date_str}` on '
        f'Telegram, or `reject check-v-update-{date_str} <reason>`.'
    )
    return '\n'.join(lines)


def dm_digest(artifact: dict[str, Any]) -> bool:
    if not artifact.get('proposals'):
        return False
    body = format_digest(artifact)
    date_str = artifact['as_of'][:10]
    try:
        import larry_alerts as la
        return la.append_alert(
            source='pulse-check-v',
            severity='warning',
            message=body,
            subject=f'check-v-update:{date_str}',
            suggested_action=(
                f'Review proposals; reply `approve check-v-update-{date_str}` or '
                f'`reject check-v-update-{date_str} <reason>`.'
            ),
        )
    except Exception as e:
        log(f'dm_digest failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- Phase C: the promotion loop --------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def consecutive_clean_streak(executions: Optional[list[dict[str, Any]]]) -> int:
    """The per-template streak = consecutive CLEAN executions from the TAIL of
    the list. A "clean" execution is ``outcome == "success"`` AND
    ``larry_correction_signal`` falsy. A failure or a Larry-correction breaks
    the streak. This is the single reader of the track record (the brief's
    enforcement: the streak is never read from the vestigial
    ``payload.larry_modified`` ledger fields)."""
    if not executions:
        return 0
    streak = 0
    for ex in reversed(executions):
        if not isinstance(ex, dict):
            break
        if ex.get('outcome') == 'success' and not ex.get('larry_correction_signal'):
            streak += 1
        else:
            break
    return streak


def load_executions_store() -> dict[str, list[dict[str, Any]]]:
    """Return ``{template: [execution, ...]}`` from the authoritative store.

    Lazily delegates to ``alert_triage_state.load_executions`` so the file
    location has a single source of truth (the recorder owns the path). A
    missing/corrupt store degrades to ``{}`` — nothing graduates on absent
    data (the conservative direction)."""
    try:
        import alert_triage_state
        return alert_triage_state.load_executions()
    except Exception as e:  # noqa: BLE001 — degrade to "no track record"
        log(f'load_executions_store failed: {type(e).__name__}: {e}', 'WARN')
        return {}


def load_registry(path: Optional[Path] = None) -> dict[str, Any]:
    """Load the full ``config/auto-fix-patterns.json`` document (preserving
    ``_schema`` so a round-trip save doesn't drop it). Returns
    ``{'patterns': []}`` on a missing/corrupt file. ``path`` resolves the
    module-level REGISTRY_FILE at CALL time (not def time) so tests can repoint
    it."""
    path = path or REGISTRY_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log(f'registry {path} unreadable; treating as empty', 'WARN')
        return {'patterns': []}
    if not isinstance(data, dict) or not isinstance(data.get('patterns'), list):
        return {'patterns': []}
    return data


def save_registry(doc: dict[str, Any], path: Optional[Path] = None) -> None:
    """Atomically write the registry document via the shared atomic_io helper.

    Uses a UNIQUE tmp (not the fixed ``<name>.tmp``) so a concurrent writer
    cannot clobber the temp mid-write — now reachable since Medic's adverse-
    execution demote (the record_action_template_execution hook) can run
    alongside the monthly promotion loop's reconcile_streaks. Byte-identical
    output to the prior fixed-tmp write (indent=2, trailing newline)."""
    from atomic_io import atomic_write_json  # scripts/ is on sys.path at runtime
    path = path or REGISTRY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, doc, indent=2, trailing_newline=True)


def _find_pattern(doc: dict[str, Any], template: str) -> Optional[dict[str, Any]]:
    for rec in doc.get('patterns', []):
        if isinstance(rec, dict) and rec.get('template') == template:
            return rec
    return None


def _can_graduate(rec: dict[str, Any]) -> bool:
    """The permanent_guard floor + reversibility gate (code, not just docs):
    credentials / money / irreversible patterns NEVER graduate, and only a
    reversible probation pattern is eligible."""
    template = rec.get('template')
    if template in NEVER_GRADUATE_TEMPLATES:
        return False
    if rec.get('permanent_guard'):
        return False
    if not rec.get('reversible'):
        return False
    return rec.get('state') == 'probation'


def reconcile_streaks(
    doc: dict[str, Any],
    store: dict[str, list[dict[str, Any]]],
) -> bool:
    """Write the executions-derived clean_streak back into each registry record
    as a cache. Returns True iff any record's clean_streak changed (so the
    caller knows whether to persist)."""
    changed = False
    for rec in doc.get('patterns', []):
        if not isinstance(rec, dict):
            continue
        template = rec.get('template')
        streak = consecutive_clean_streak(store.get(template, []))
        if rec.get('clean_streak') != streak:
            rec['clean_streak'] = streak
            changed = True
    return changed


def find_graduation_candidates(
    doc: dict[str, Any],
    store: dict[str, list[dict[str, Any]]],
    *,
    min_streak: int = GRADUATE_MIN_CLEAN_STREAK,
) -> list[dict[str, Any]]:
    """Probation, reversible, NOT permanent_guard patterns whose executions-
    derived clean streak has reached the threshold. Each candidate carries the
    record, the streak, and the executions tail (for the track-record render)."""
    out: list[dict[str, Any]] = []
    for rec in doc.get('patterns', []):
        if not isinstance(rec, dict) or not _can_graduate(rec):
            continue
        template = rec.get('template')
        execs = store.get(template, [])
        streak = consecutive_clean_streak(execs)
        if streak >= min_streak:
            out.append({'template': template, 'clean_streak': streak,
                        'record': rec, 'executions': execs})
    return out


def _track_record_phrase(executions: list[dict[str, Any]], streak: int) -> str:
    """Plain-English track record, e.g. "4/4 clean over 11 days". Spans the
    clean tail; degrades to a count-only phrase when timestamps are absent."""
    clean_tail = executions[-streak:] if streak and len(executions) >= streak else []
    base = f'{streak}/{streak} clean'
    tss = [_parse_ts(ex.get('ts')) for ex in clean_tail if isinstance(ex, dict)]
    tss = [t for t in tss if t is not None]
    if len(tss) >= 2:
        days = (max(tss) - min(tss)).days
        if days >= 1:
            return f'{base} over {days} day{"s" if days != 1 else ""}'
    return f'{base} execution{"s" if streak != 1 else ""}'


def render_graduation_approval(
    record: dict[str, Any],
    clean_streak: int,
    executions: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Larry-facing plain-language graduation ask. Four sections: WHAT it is,
    WHAT Pulse will now do automatically, WHY it's safe, TRACK RECORD. The
    prose comes from the registry's ``plain_language`` block; if a field is
    missing the render falls through to a safe raw form (the render-layer-
    human-translation rule) rather than crashing or leaking an enum."""
    template = record.get('template', '(unknown)')
    pl = record.get('plain_language') if isinstance(
        record.get('plain_language'), dict) else {}
    what = pl.get('what') or f'the "{template}" self-heal'
    action = pl.get('action') or (
        f'automatically run the "{template}" fix instead of asking you first')
    why_safe = pl.get('why_safe') or (
        'this fix is reversible — it can be undone if it ever misbehaves')
    track = _track_record_phrase(executions or [], clean_streak)
    return (
        f'Pulse would like to start handling this one on her own.\n\n'
        f'WHAT IT IS: {what}\n\n'
        f'WHAT PULSE WOULD NOW DO AUTOMATICALLY: {action}\n\n'
        f'WHY IT\'S SAFE: {why_safe}\n\n'
        f'TRACK RECORD: {track} — she\'s handled this cleanly every time, '
        f'with no corrections from you.\n\n'
        f'Reply `approve graduation {template}` to let her, or just ignore '
        f'this to leave it as ask-first.'
    )


def _primary_chat_id() -> Optional[int]:
    """Resolve Larry's primary Telegram chat from TELEGRAM_ALLOWED_CHAT_IDS
    (the bot's own allow-list). The promotion loop runs as a daemon with no
    originating chat, so it DMs the lowest authorized chat id. None if unset —
    the pending entry is still created so the bot can surface it on next poll."""
    raw = os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS', '')
    ids = []
    for tok in raw.replace(',', ' ').split():
        try:
            ids.append(int(tok))
        except ValueError:
            continue
    return min(ids) if ids else None


def emit_graduation_proposals(
    candidates: list[dict[str, Any]],
    *,
    chat_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Emit one graduation APPROVAL_REQUEST per ready pattern through the
    EXISTING approval machinery: ``beacon_approval_handler.add_pending`` creates
    the pending entry (carrying ``kind="graduation"`` + the template), and
    ``larry_alerts.append_approval_request`` DMs Larry the plain-language render.
    Returns the list of pending entries created (empty on no candidates)."""
    if not candidates:
        return []
    chat = chat_id if chat_id is not None else _primary_chat_id()
    try:
        import beacon_approval_handler as approval
        import larry_alerts as la
    except Exception as e:  # noqa: BLE001
        log(f'graduation emit deps unavailable: {type(e).__name__}: {e}', 'WARN')
        return []
    created: list[dict[str, Any]] = []
    for cand in candidates:
        template = cand['template']
        # Dedup guard: run_promotion_loop runs monthly and add_pending does not
        # dedup by id, so a proposal Larry has left unactioned would otherwise be
        # re-emitted (and re-DMd) on the next cycle under the same
        # `graduation-<template>` id. Skip if one is already pending.
        if approval.find_graduation_pending(template) is not None:
            log(f'graduation proposal for {template} already pending — skipping '
                f're-emit')
            continue
        record = cand['record']
        streak = cand['clean_streak']
        body = render_graduation_approval(record, streak, cand.get('executions'))
        task_id = f'graduation-{template}'
        payload = {
            'task_id': task_id,
            'summary': body,
            'target_agent': 'forge',
            # target_repo is REQUIRED: forge is worktree_enabled, so
            # inbox_watcher refuses any task whose target_repo has no canonical
            # path (it can't create the worktree). Without this the approval DM
            # claims success but no config PR ever opens. task_type mirrors the
            # Check III/VIII config-only PR pattern the brief cites.
            'target_repo': 'ourliberty-agent-core',
            'task_type': 'doc-only',
            'pr_title': f'chore(pulse): graduate auto-fix pattern {template}',
            'kind': 'graduation',
            'template': template,
            'prompt': (
                f'Larry approved graduating the auto-fix pattern "{template}". '
                f'Open a config-only PR that flips its registry record from '
                f'ask-first to auto-fix by running:\n'
                f'    python3 scripts/pulse_check_v.py apply-graduation {template}\n'
                f'Commit ONLY config/auto-fix-patterns.json. No other changes.'
            ),
        }
        try:
            entry = approval.add_pending(payload, chat_id=chat or 0)
            if chat is not None:
                la.append_approval_request(chat, task_id, body,
                                           source='pulse-check-v')
            created.append(entry)
            log(f'graduation proposal emitted for {template} '
                f'(clean_streak={streak})')
        except Exception as e:  # noqa: BLE001 — one bad emit must not abort the rest
            log(f'graduation emit for {template} failed: '
                f'{type(e).__name__}: {e}', 'WARN')
    return created


def apply_graduation(template: str, *, path: Optional[Path] = None,
                     now: Optional[str] = None) -> bool:
    """Flip a registry record ``probation → graduated`` and stamp
    ``graduated_at``. Single source of the registry-mutation logic (the bot
    never mutates the registry directly). Refuses (returns False) if the
    template is unknown or fails the permanent_guard/reversibility floor — a
    floor pattern can never be graduated even by an explicit CLI call."""
    doc = load_registry(path)
    rec = _find_pattern(doc, template)
    if rec is None:
        log(f'apply_graduation: unknown template {template!r}', 'WARN')
        return False
    if template in NEVER_GRADUATE_TEMPLATES or rec.get('permanent_guard') \
            or not rec.get('reversible'):
        log(f'apply_graduation: {template!r} is a permanent_guard/irreversible '
            f'floor — refusing to graduate', 'WARN')
        return False
    rec['state'] = 'graduated'
    rec['graduated_at'] = now or _iso_now()
    save_registry(doc, path)
    log(f'graduated {template} (state=graduated)')
    return True


def demote(template: str, *, reason: str = '', correction: bool = False,
           path: Optional[Path] = None, now: Optional[str] = None) -> bool:
    """Flip a registry record ``graduated → probation``, reset
    ``clean_streak = 0``, and stamp ``last_larry_correction_at``. Automatic and
    UNGATED — losing trust is never approval-gated. Returns False on an unknown
    template."""
    doc = load_registry(path)
    rec = _find_pattern(doc, template)
    if rec is None:
        log(f'demote: unknown template {template!r}', 'WARN')
        return False
    rec['state'] = 'probation'
    rec['clean_streak'] = 0
    rec['last_larry_correction_at'] = now or _iso_now()
    save_registry(doc, path)
    log(f'demoted {template} -> probation '
        f'(reason={reason!r}, correction={correction})')
    return True


def demote_on_adverse_execution(template: str, *, reason: str = '',
                                correction: bool = False) -> bool:
    """Recorder hook (called from
    ``alert_triage_state.record_action_template_execution``): on a failure or
    Larry-correction recorded against a CURRENTLY-graduated template, demote it
    immediately. A no-op (returns False) if the template is not currently
    graduated — an adverse execution on an already-probation pattern just breaks
    its streak naturally via consecutive_clean_streak()."""
    doc = load_registry()
    rec = _find_pattern(doc, template)
    if rec is None or rec.get('state') != 'graduated':
        return False
    return demote(template, reason=reason, correction=correction)


def run_promotion_loop(*, chat_id: Optional[int] = None) -> dict[str, Any]:
    """The live Phase-C path: reconcile executions-derived streaks into the
    registry cache, find graduation-ready patterns, and emit batched proposals.
    Returns a summary dict for the artifact/log."""
    doc = load_registry()
    store = load_executions_store()
    if reconcile_streaks(doc, store):
        save_registry(doc)
    candidates = find_graduation_candidates(doc, store)
    created = emit_graduation_proposals(candidates, chat_id=chat_id)
    return {
        'candidates': [c['template'] for c in candidates],
        'proposals_emitted': len(created),
    }


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',
                        help='Read ledger rows + guard-list from a JSON '
                             'fixture file instead of the live ledger.')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true',
                        help='Bypass monthly idempotency.')
    sub = parser.add_subparsers(dest='cmd')
    p_ag = sub.add_parser(
        'apply-graduation',
        help='Flip a registry record probation->graduated (run by Forge from '
             'a Larry-approved config-only PR).')
    p_ag.add_argument('template')
    p_dm = sub.add_parser(
        'demote',
        help='Flip a registry record graduated->probation (manual override; '
             'the automatic path is demote_on_adverse_execution).')
    p_dm.add_argument('template')
    args = parser.parse_args(argv)

    if args.cmd == 'apply-graduation':
        return 0 if apply_graduation(args.template) else 1
    if args.cmd == 'demote':
        return 0 if demote(args.template, reason='manual cli demote') else 1

    now = datetime.now(timezone.utc)
    anchor = month_anchor(now.date())
    target_path = artifact_path_for_month(anchor)

    if target_path.exists() and not args.force and not args.dry_run:
        log(f'Check V already ran this month ({anchor}); '
            'skipping (use --force to re-run).')
        return 0

    if args.fixture:
        with open(args.fixture) as fh:
            raw = json.load(fh)
        rows = raw.get('rows', [])
        guard_list = set(raw.get('guard_list', []))
    else:
        rows = _read_ledger()
        guard_list = load_guard_list()

    result = run_check(rows=rows, guard_list=guard_list, now=now)
    artifact = build_artifact(result)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    write_artifact(artifact)
    # Phase C live path: the registry promotion loop. The legacy guard-list
    # digest (run_check/dm_digest) is retained as a back-compat shim; graduation
    # itself now flows from the executions-derived clean streak, not the
    # dispatch-count rule. Run after the artifact write so a promotion-loop
    # failure can't block the monthly idempotency marker.
    try:
        promo = run_promotion_loop()
        log(f'promotion loop: candidates={promo["candidates"]} '
            f'proposals_emitted={promo["proposals_emitted"]}')
    except Exception as e:  # noqa: BLE001 — never wedge the monthly check
        log(f'promotion loop failed: {type(e).__name__}: {e}', 'WARN')
    log(f'Check V complete: proposals={len(result.proposals)} '
        f'templates_evaluated={result.template_count}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check as _hb_run_check
    sys.exit(_hb_run_check('v', main, log_fn=log))
