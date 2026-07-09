#!/usr/bin/env python3
"""suite_guardian_stage.py — staged-autonomy stage machine (spec D3+D4, PR-3).

Spec: agents/beacon/specs/main-suite-green-guardian.md (§5 D3+D4, decisions
L3/L4/L8/L10).

WHAT THIS IS
------------
The Main-Suite Green Guardian earns autonomy in four stages:

    0 shadow    detect + record only
    1 propose   D2 in full; every dispatch consented via the run entry
    2 auto-file fix tasks dispatch without prior consent
    3 auto-merge test-only fix PRs merge on Mirror PASS (SHA-bound + L4 gate)

AUTHORITY & PRECEDENCE (L3). ``config/suite-guardian.json`` `stage` is the ONLY
source of the granted stage ceiling — a repo file, deep-review-held, raised only
by a human-approved config-only PR (the graduation card, Check-V pattern). The
guardian NEVER writes its own stage up. The EFFECTIVE stage is::

    effective = min(config.stage, dial_max_stage(), penalty_cap or 3)

so the live trust-policy dial always wins (conservative->1, balanced->2,
loose->3), and an automatic downgrade (L10) can only ever *lower* the effective
stage below the granted ceiling. On an unreadable/malformed config -> Stage 0.

DOWNGRADES (L10) are proportional and UNGATED (losing trust is never approval-
gated), so they cannot be written into `config.stage` (that needs a human PR).
Instead the guardian owns a SEPARATE penalty state at
``~/agents/state/suite-guardian-stage.json`` — a ceiling (`penalty_cap`) that
caps `effective` below the config grant, plus the evidence-reset anchor
(`penalty_at_run`). A deliberate human graduation (which bumps `config.stage_epoch`)
supersedes a stale penalty: :func:`reconcile_epoch` clears the cap when it sees a
new epoch. This keeps promotion strictly human/config-authored while downgrades
stay automatic — the L3/L10 tension resolved without letting the guardian
self-promote.

State file (guardian-owned, NOT the stage authority)::

    {
      "last_stage_epoch": <int>,    # last config stage_epoch reconciled
      "penalty_cap": <int>|null,    # effective-stage ceiling from a downgrade
      "penalty_cause": <str>|null,  # why (auto-merged-regression|scope-violation|regression)
      "penalty_at": <iso8601>|null,
      "penalty_at_run": <int>|null, # guardian run seq at downgrade (evidence-reset floor)
      "l8_card_filed_at": <iso8601>|null  # D4 one-time tightening card guard
    }

Stdlib + repo siblings only (trust_policy, suite_guardian_ledger). Every reader
is fail-safe: a corrupt/unreadable file degrades to Stage 0 / no penalty, never
an exception into the nightly guardian timer.

CLI:
    python3 scripts/suite_guardian_stage.py apply-graduation <target-stage>
        [--repo-root <PATH>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import atomic_io  # noqa: E402
import suite_guardian_ledger as ledger  # noqa: E402
import trust_policy  # noqa: E402


# --- constants ---------------------------------------------------------------

MIN_STAGE = 0
MAX_STAGE = 3

# The dial->max-stage map (spec L3, explicit so PR-3 tests assert it). The live
# trust-policy dial position caps the effective stage regardless of the config
# grant — conservative pins the guardian at <= Stage 1 by construction.
DIAL_MAX_STAGE = {'conservative': 1, 'balanced': 2, 'loose': 3}

# Downgrade causes (L10). The first two are hard resets to Stage 0; the third is
# a proportional stage-1 decrement.
CAUSE_AUTO_MERGED_REGRESSION = 'auto-merged-regression'
CAUSE_SCOPE_VIOLATION = 'scope-violation'
CAUSE_REGRESSION = 'regression'
_HARD_RESET_CAUSES = (CAUSE_AUTO_MERGED_REGRESSION, CAUSE_SCOPE_VIOLATION)

# D4 / L8: reds - parked == 0 for this many consecutive runs files the one-time
# gate-tightening card.
L8_ZERO_RED_RUNS = 14

# Graduation evidence thresholds (spec D3 stage table). Only CLOSED 14-run
# windows count; evidence resets on downgrade.
GRAD_STAGE0_MIN_RUNS = 7            # 0->1: 7 runs, no classification flip-flops
GRAD_STAGE1_MIN_WINDOWS = 8         # 1->2: >=8 closed windows
GRAD_STAGE2_MIN_AUTO_FILED = 10     # 2->3: >=10 auto-filed closed windows


# --- paths -------------------------------------------------------------------

def _agents_root() -> Path:
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path('/home/larry/agents')


def stage_state_path() -> Path:
    return _agents_root() / 'state' / 'suite-guardian-stage.json'


def _config_path(repo_root: Path) -> Path:
    return Path(repo_root) / 'config' / 'suite-guardian.json'


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


# --- config (stage authority, L3) --------------------------------------------

def read_config_stage(repo_root: Path) -> tuple[int, int]:
    """Read the granted stage + stage_epoch from ``config/suite-guardian.json``.
    L3: this repo file is the ONLY stage source. Returns ``(stage, stage_epoch)``
    clamped to [0, MAX_STAGE]; ``(0, 0)`` on any read/parse error (unreadable
    config -> Stage 0). Never raises."""
    try:
        cfg = json.loads(_config_path(repo_root).read_text('utf-8'))
        stage = cfg.get('stage')
        epoch = cfg.get('stage_epoch', 0)
        if not isinstance(stage, int) or isinstance(stage, bool):
            return (0, 0)
        if not isinstance(epoch, int) or isinstance(epoch, bool):
            epoch = 0
        return (max(MIN_STAGE, min(MAX_STAGE, stage)), epoch)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return (0, 0)


# --- dial (trust-policy, L3) --------------------------------------------------

def current_dial_level() -> str:
    """The live trust-policy dial position, fail-safe to 'conservative' (the
    tightest, so a degraded read never over-permits). Never raises."""
    try:
        return trust_policy.current_autonomy_level()
    except Exception:  # noqa: BLE001 — dial read must never wedge the timer
        return 'conservative'


def dial_max_stage(level: Optional[str] = None) -> int:
    """Max stage the live autonomy dial permits (spec L3). ``level`` defaults to
    the live trust-policy position; an unknown/degraded level reads as the
    conservative floor (Stage 1). Never raises."""
    lvl = level if level is not None else current_dial_level()
    return DIAL_MAX_STAGE.get(lvl, DIAL_MAX_STAGE['conservative'])


# --- penalty state (guardian-owned) ------------------------------------------

def _empty_state() -> dict[str, Any]:
    return {
        'last_stage_epoch': 0,
        'penalty_cap': None,
        'penalty_cause': None,
        'penalty_at': None,
        'penalty_at_run': None,
        'l8_card_filed_at': None,
    }


def load_stage_state(*, path: Optional[Path] = None) -> dict[str, Any]:
    """Load the guardian's stage state. Fail-safe: a missing/corrupt file
    degrades to the empty (no-penalty) state. Never raises."""
    p = path or stage_state_path()
    base = _empty_state()
    try:
        data = json.loads(p.read_text('utf-8'))
        if isinstance(data, dict):
            base.update({k: data.get(k, v) for k, v in base.items()})
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return base


def save_stage_state(state: dict[str, Any], *, path: Optional[Path] = None) -> None:
    p = path or stage_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.atomic_write_json(p, state, indent=2, sort_keys=True,
                                trailing_newline=True)


def reconcile_epoch(repo_root: Path, *, path: Optional[Path] = None) -> bool:
    """Clear a stale downgrade penalty when a deliberate human graduation bumped
    ``config.stage_epoch`` (L10: a human config grant supersedes an automatic
    penalty). Returns True iff a penalty was cleared this call. Never raises."""
    p = path or stage_state_path()
    _, epoch = read_config_stage(repo_root)
    state = load_stage_state(path=p)
    if epoch == state.get('last_stage_epoch') and state.get('penalty_cap') is None:
        return False
    cleared = False
    if epoch != state.get('last_stage_epoch'):
        if state.get('penalty_cap') is not None:
            cleared = True
        state['last_stage_epoch'] = epoch
        state['penalty_cap'] = None
        state['penalty_cause'] = None
        state['penalty_at'] = None
        state['penalty_at_run'] = None
        save_stage_state(state, path=p)
    return cleared


def effective_stage(
    repo_root: Path,
    *,
    level: Optional[str] = None,
    path: Optional[Path] = None,
) -> int:
    """The stage actually in force = min(config grant, dial max, penalty cap).
    Reconciles a superseding graduation first (L10). Never raises; an unreadable
    config yields Stage 0."""
    p = path or stage_state_path()
    reconcile_epoch(repo_root, path=p)
    config_stage, _ = read_config_stage(repo_root)
    dial = dial_max_stage(level)
    state = load_stage_state(path=p)
    cap = state.get('penalty_cap')
    ceiling = cap if isinstance(cap, int) else MAX_STAGE
    return max(MIN_STAGE, min(config_stage, dial, ceiling))


def apply_downgrade(
    *,
    cause: str,
    current_stage: int,
    run_seq: int,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Impose a proportional downgrade penalty (L10). ``auto-merged-regression``
    or ``scope-violation`` -> hard reset to Stage 0; any other regression ->
    ``current_stage - 1`` (floored at 0). Evidence resets: ``penalty_at_run``
    becomes the floor after which graduation evidence must accrue. A more severe
    (lower) cap always wins over a standing one; a less severe downgrade never
    RAISES an existing cap. Returns the updated state. Never raises."""
    p = path or stage_state_path()
    if cause in _HARD_RESET_CAUSES:
        new_cap = 0
    else:
        new_cap = max(MIN_STAGE, int(current_stage) - 1)
    state = load_stage_state(path=p)
    existing = state.get('penalty_cap')
    if isinstance(existing, int) and existing <= new_cap:
        # Already at least this restrictive — keep the tighter cap, but still
        # advance the evidence-reset floor (a fresh regression re-arms the clock).
        new_cap = existing
    state['penalty_cap'] = new_cap
    state['penalty_cause'] = cause
    state['penalty_at'] = _now_iso(now)
    state['penalty_at_run'] = int(run_seq)
    save_stage_state(state, path=p)
    return state


def evidence_floor_run(state: dict[str, Any]) -> Optional[int]:
    """The run seq after which graduation evidence must have accrued (L10 reset),
    or None if no downgrade is standing."""
    r = state.get('penalty_at_run')
    return r if isinstance(r, int) else None


# --- graduation evidence (pure) ----------------------------------------------

def evaluate_graduation(
    *,
    config_stage: int,
    dial_level: str,
    completed_runs: int,
    flip_flop_count: int,
    closed_windows: int,
    auto_filed_closed_windows: int,
    same_leak_regressions: int,
    scope_violations: int,
) -> Optional[dict[str, Any]]:
    """Decide whether the guardian has EARNED graduation from ``config_stage`` to
    ``config_stage + 1`` (spec D3 stage table). Pure — the caller supplies the
    evidence tallies (registry-derived for the 0->1 gate, ledger-derived for the
    rest, already scoped past the L10 evidence-reset floor). Returns a candidate
    dict (target_stage + evidence + dial note) if eligible, else None.

    Not eligible when: already at MAX_STAGE, or the dial map would pin the target
    below what's requested (the card would be inert — Stage 2 needs dial>=balanced,
    Stage 3 needs dial>=loose), or the per-stage bar isn't met."""
    if config_stage >= MAX_STAGE:
        return None
    target = config_stage + 1
    dial_ceiling = DIAL_MAX_STAGE.get(dial_level, DIAL_MAX_STAGE['conservative'])

    evidence: dict[str, Any] = {}
    if target == 1:
        if completed_runs < GRAD_STAGE0_MIN_RUNS or flip_flop_count > 0:
            return None
        evidence = {'completed_runs': completed_runs,
                    'classification_flip_flops': flip_flop_count}
    elif target == 2:
        if (closed_windows < GRAD_STAGE1_MIN_WINDOWS
                or same_leak_regressions > 0 or scope_violations > 0):
            return None
        evidence = {'closed_windows': closed_windows,
                    'same_leak_regressions': same_leak_regressions,
                    'scope_violations': scope_violations}
    elif target == 3:
        if (auto_filed_closed_windows < GRAD_STAGE2_MIN_AUTO_FILED
                or same_leak_regressions > 0):
            return None
        evidence = {'auto_filed_closed_windows': auto_filed_closed_windows,
                    'same_leak_regressions': same_leak_regressions}

    # The dial would pin the newly-granted stage below the target — a graduation
    # card now would be inert until Larry also raises the dial. Don't emit; the
    # card is only actionable once the dial permits the target.
    dial_note = None
    if target > dial_ceiling:
        need = 'balanced' if target == 2 else 'loose'
        dial_note = f'requires dial >= {need}'
        return None

    return {'target_stage': target, 'evidence': evidence, 'dial_note': dial_note}


# --- card emission (Check-V pattern, spec L3) --------------------------------

def _graduation_task_id(target_stage: int) -> str:
    return f'suite-guardian-graduation-stage-{target_stage}'


L8_TASK_ID = 'suite-guardian-l8-tightening'


def _render_graduation_body(candidate: dict[str, Any],
                            penalty_cause: Optional[str]) -> str:
    target = candidate['target_stage']
    ev = candidate['evidence']
    lines = [
        f'Main-Suite Green Guardian has earned graduation to Stage {target}.',
        '',
        'Evidence (only CLOSED 14-run windows count):',
    ]
    for k, v in sorted(ev.items()):
        lines.append(f'  • {k.replace("_", " ")}: {v}')
    if penalty_cause:
        lines.append('')
        lines.append(f'Prior downgrade cause (evidence reset from here): '
                     f'{penalty_cause}.')
    lines.append('')
    lines.append(
        f'Approve to open a config-only PR flipping config/suite-guardian.json '
        f'to stage {target} (Check-V pattern; the guardian never self-promotes).'
    )
    return '\n'.join(lines)


def _primary_chat_id() -> Optional[int]:
    raw = os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS', '')
    ids = []
    for tok in raw.replace(',', ' ').split():
        try:
            ids.append(int(tok))
        except ValueError:
            continue
    return min(ids) if ids else None


def _emit_card(payload: dict[str, Any], *, chat_id: Optional[int]) -> Optional[dict[str, Any]]:
    """Emit ONE approval card through the existing Beacon approval machinery
    (add_pending + larry_alerts render), deduped by task_id. Returns the pending
    entry, or None if a duplicate is already pending or deps are unavailable.
    Never raises."""
    try:
        import beacon_approval_handler as approval
        import larry_alerts as la
    except Exception as e:  # noqa: BLE001
        print(f'suite_guardian_stage: card deps unavailable: '
              f'{type(e).__name__}: {e}', file=sys.stderr)
        return None
    task_id = payload['task_id']
    if approval.find_pending_by_id(task_id) is not None:
        return None
    chat = chat_id if chat_id is not None else _primary_chat_id()
    try:
        entry = approval.add_pending(payload, chat_id=chat or 0)
        if chat is not None:
            la.append_approval_request(chat, task_id, payload.get('summary', ''),
                                       source='suite-guardian')
        return entry
    except Exception as e:  # noqa: BLE001 — one bad emit must not wedge the timer
        print(f'suite_guardian_stage: card emit failed for {task_id}: '
              f'{type(e).__name__}: {e}', file=sys.stderr)
        return None


def emit_graduation(
    candidate: dict[str, Any],
    *,
    penalty_cause: Optional[str] = None,
    chat_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Emit the graduation approval card for ``candidate`` (from
    :func:`evaluate_graduation`). The card cites the ledger evidence and any prior
    downgrade cause (L10). On approval the dispatched Forge task runs
    ``apply-graduation`` to open the config-only PR."""
    target = candidate['target_stage']
    body = _render_graduation_body(candidate, penalty_cause)
    payload = {
        'task_id': _graduation_task_id(target),
        'summary': body,
        'target_agent': 'forge',
        'target_repo': 'ourliberty-agent-core',
        'task_type': 'doc-only',
        'pr_title': f'chore(suite-guardian): graduate to autonomy stage {target}',
        'kind': 'graduation',
        'target_stage': target,
        'prompt': (
            f'Larry approved graduating the Main-Suite Green Guardian to '
            f'autonomy stage {target}. Open a config-only PR that raises the '
            f'stage by running:\n'
            f'    python3 scripts/suite_guardian_stage.py apply-graduation {target}\n'
            f'Commit ONLY config/suite-guardian.json (stage + stage_epoch). No '
            f'other changes.'
        ),
    }
    return _emit_card(payload, chat_id=chat_id)


def _render_l8_body(evidence: dict[str, Any], parked: list[str]) -> str:
    lines = [
        'Main-Suite Green Guardian: the suite has held '
        f'reds − parked == 0 for {evidence.get("zero_red_runs", L8_ZERO_RED_RUNS)} '
        'consecutive runs — the payoff bar (L8) is met.',
        '',
        'Proposal (never auto-applied): tighten the main-suite gate so a head '
        'failure BLOCKs only after surviving infra-retry + isolation '
        'classification as non-flake (or via ABSOLUTE_INVARIANT_TESTS '
        'expansion) — never raw any-failure.',
        '',
        'Evidence:',
    ]
    for k, v in sorted(evidence.items()):
        lines.append(f'  • {k.replace("_", " ")}: {v}')
    lines.append('')
    if parked:
        lines.append('Parked items to re-decide once (L9):')
        for tid in parked:
            lines.append(f'  • {tid}')
    else:
        lines.append('Parked items to re-decide: none.')
    return '\n'.join(lines)


def maybe_emit_l8_card(
    *,
    zero_red_streak: int,
    evidence: dict[str, Any],
    parked: list[str],
    chat_id: Optional[int] = None,
    state_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """File the ONE-TIME L8 gate-tightening card (D4) once the suite has held
    reds − parked == 0 for L8_ZERO_RED_RUNS runs. Guarded twice: the persisted
    ``l8_card_filed_at`` flag makes it fire at most once ever, and the pending
    dedup covers a same-cycle retry. Never auto-applies. Returns the pending
    entry, or None. Never raises."""
    if zero_red_streak < L8_ZERO_RED_RUNS:
        return None
    p = state_path or stage_state_path()
    state = load_stage_state(path=p)
    if state.get('l8_card_filed_at'):
        return None
    ev = dict(evidence)
    ev.setdefault('zero_red_runs', zero_red_streak)
    payload = {
        'task_id': L8_TASK_ID,
        'summary': _render_l8_body(ev, parked),
        'target_agent': 'beacon',
        'task_type': 'proposal',
        'kind': 'gate-tightening',
        'bare_approvable': False,
        'parked': list(parked),
    }
    entry = _emit_card(payload, chat_id=chat_id)
    if entry is not None:
        state['l8_card_filed_at'] = _now_iso(now)
        save_stage_state(state, path=p)
    return entry


# --- apply-graduation (config mutation, CLI-only) ----------------------------

def apply_graduation(
    target_stage: int,
    *,
    repo_root: Optional[Path] = None,
) -> bool:
    """Raise ``config/suite-guardian.json`` `stage` to ``target_stage`` and bump
    `stage_epoch` (so the diff is never empty and the guardian detects the grant
    to clear a stale penalty). The SINGLE place the stage config is mutated —
    invoked only by the approved graduation PR (Check-V pattern). Refuses an
    out-of-range target or a non-increment. Returns True on success."""
    root = Path(repo_root) if repo_root else Path.home() / 'agent-core'
    if not (MIN_STAGE < target_stage <= MAX_STAGE):
        print(f'apply_graduation: target stage {target_stage} out of range '
              f'({MIN_STAGE + 1}..{MAX_STAGE})', file=sys.stderr)
        return False
    cfg_path = _config_path(root)
    try:
        cfg = json.loads(cfg_path.read_text('utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        print(f'apply_graduation: cannot read config: {e}', file=sys.stderr)
        return False
    current = cfg.get('stage', 0)
    if not isinstance(current, int) or isinstance(current, bool):
        current = 0
    if target_stage <= current:
        print(f'apply_graduation: target {target_stage} does not raise current '
              f'stage {current}', file=sys.stderr)
        return False
    cfg['stage'] = target_stage
    epoch = cfg.get('stage_epoch', 0)
    cfg['stage_epoch'] = (epoch + 1) if isinstance(epoch, int) else 1
    cfg_path.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')
    print(f'apply_graduation: config/suite-guardian.json -> stage {target_stage} '
          f'(stage_epoch {cfg["stage_epoch"]})')
    return True


# --- CLI ---------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog='suite_guardian_stage')
    sub = parser.add_subparsers(dest='cmd', required=True)
    ag = sub.add_parser('apply-graduation',
                        help='raise config stage (approved graduation PR)')
    ag.add_argument('target_stage', type=int)
    ag.add_argument('--repo-root', default=None)
    args = parser.parse_args(argv)
    if args.cmd == 'apply-graduation':
        root = Path(args.repo_root) if args.repo_root else None
        ok = apply_graduation(args.target_stage, repo_root=root)
        return 0 if ok else 1
    return 2


if __name__ == '__main__':
    sys.exit(main())
