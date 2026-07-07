#!/usr/bin/env python3
"""mission_staleness.py — read-only staleness scorer over the proposed-missions
pile (Operator Feed Loop slice 3).

WHY THIS EXISTS
---------------
The Missions board's "proposed" lane has grown to ~232 cards, but most are not
deliberate proposals Larry would ever choose to build — they are auto-registered
orphan-task fragments (heal_orphan_autoregister mints a card from any orphaned
task id), long-dead ideas, or duplicates. Before the operator can present Larry a
RANKED, one-click Approvals queue (slice 5), the pile has to be reconciled down to
what is genuinely still actionable. This pass is the "is it still applicable?"
judgment, mechanized.

It is deliberately READ-ONLY over missions.json. That file has many healer
writers (heal_missions_card_gc, heal_missions_board_drain, heal_orphan_autoregister,
dashboard_api, ...); this scorer must never join that write set. It reads the
board, scores each proposed card for staleness with human-readable reasons, and
writes a RANKED stale-candidate list to its own state file. Acting on a candidate
(archiving it) stays a one-click Larry decision downstream — nothing is
auto-archived (park-don't-decay).

Signals (each contributes a weighted score + a plain reason):
  - already_terminal — the work this card tracks already MERGED/CLOSED on GitHub.
    Definitive "already done". Reuses task_terminal_state (conservative: UNKNOWN
    on any doubt, never guesses terminal). BOUNDED: only checked for real-looking
    cards (not orphan/degenerate junk, whose ids never match a PR) and capped per
    pass to bound gh calls.
  - orphan_autoproposed — brief says "Auto-proposed from orphan task". The bulk
    of the noise; deterministic, no external call.
  - degenerate_name — the card name carries no information beyond the raw task id
    (name de-slugifies to the task id) or is purely numeric. Auto-mint tell.
  - aging — created > AGING_DAYS ago and still un-advanced (all proposed cards
    are un-advanced by definition).
  - duplicate — normalized name collides with another proposed card or a shipped
    mission.

Discipline (mirrors the ledger / task_terminal_state modules): stdlib + gh only,
never raises (a bad row / gh hiccup logs and the pass continues), WARN-logged.
The terminal-check fn is injected as a seam so tests never shell out.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
MISSIONS_JSON = AGENTS_ROOT / 'agents' / 'beacon' / 'workspace' / 'missions.json'
CANDIDATES_FILE = AGENTS_ROOT / 'state' / 'mission-staleness-candidates.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'mission-staleness.log'

AGING_DAYS = 21
_ORPHAN_BRIEF = 'auto-proposed from orphan task'
# Bound gh calls per pass — real-looking cards only, capped. A capped pass logs
# what it skipped (no silent truncation).
DEFAULT_TERMINAL_CAP = 60

# Signal weights. already_terminal is definitive; the deterministic tells stack.
_W_TERMINAL = 100
_W_ORPHAN = 40
_W_DEGENERATE = 30
_W_DUPLICATE = 20
_W_AGING_BASE = 10


def _log(level: str, msg: str) -> None:
    """Append a structured line to the scorer log. Never raises."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] mission_staleness: {msg}\n')
    except OSError:
        pass


def _norm(s: Any) -> str:
    """Lowercase + strip everything but [a-z0-9] — so a de-slugified card name
    (`P3 Mission Proposed Actions API`) and its task id
    (`p3-mission-proposed-actions-api`) normalize equal."""
    if not isinstance(s, str):
        return ''
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _age_days(mission: dict, today: date) -> Optional[int]:
    """Whole days since the card was created. None if `created` is unparseable."""
    raw = mission.get('created')
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        created = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return (today - created).days


def _first_task_id(mission: dict) -> Optional[str]:
    """The card's first task id, if any."""
    tids = mission.get('task_ids')
    if isinstance(tids, list):
        for t in tids:
            if isinstance(t, str) and t:
                return t
    return None


def _is_orphan_autoproposed(mission: dict) -> bool:
    brief = mission.get('brief')
    return isinstance(brief, str) and brief.strip().lower().startswith(_ORPHAN_BRIEF)


def _is_degenerate_name(mission: dict) -> bool:
    """The name is intrinsically contentless — blank, purely numeric, or a single
    tiny token (`20`, `x`). An auto-mint tell, not a human-authored proposal.

    NOTE: deliberately does NOT flag `_norm(name) == _norm(task_id)` — the task
    id is the SLUGIFIED name for legitimate cards too (`Wire Health Notify` ->
    `wire-health-notify`), so that test flags real proposals as junk. The
    orphan-brief signal is the reliable junk detector; this one only catches
    names that carry no words at all."""
    name = mission.get('name')
    if not isinstance(name, str) or not name.strip():
        return True  # a blank name IS degenerate
    s = name.strip()
    if s.isdigit():
        return True
    # a single tiny non-word token ('x1', 'a_'); `not s.isalpha()` SPARES short
    # acronym proposals (a deliberate "KYC" / "CEO" / "Ops" card is real, not junk).
    return ' ' not in s and len(s) <= 3 and not s.isalpha()


def score_mission(
    mission: dict,
    *,
    name_counts: dict,
    shipped_names: set,
    today: date,
    terminal_fn: Optional[Callable[[str], str]] = None,
) -> Optional[dict]:
    """Score one proposed card. Returns a candidate dict (id, name, score,
    reasons, age_days, terminal_state) if ANY staleness signal fires, else None
    (nothing stale to surface). `terminal_fn`, when given, is called ONLY for
    real-looking cards (not orphan/degenerate) — the caller is responsible for
    bounding how many cards reach here with a terminal_fn set."""
    reasons: list[str] = []
    score = 0
    terminal_state: Optional[str] = None

    orphan = _is_orphan_autoproposed(mission)
    degenerate = _is_degenerate_name(mission)

    if orphan:
        reasons.append('auto-proposed from an orphan task (not a deliberate proposal)')
        score += _W_ORPHAN
    if degenerate:
        reasons.append('name carries no information beyond the raw task id')
        score += _W_DEGENERATE

    age = _age_days(mission, today)
    if age is not None and age > AGING_DAYS:
        reasons.append(f'aging: {age} days old and never advanced')
        score += _W_AGING_BASE + min(age, 60) // 6

    nm = _norm(mission.get('name'))
    if nm:
        if name_counts.get(nm, 0) > 1:
            reasons.append('duplicate: another proposed card shares this name')
            score += _W_DUPLICATE
        elif nm in shipped_names:
            reasons.append('duplicate: a shipped mission already has this name')
            score += _W_DUPLICATE

    # already_terminal — only for real-looking cards (orphan/degenerate junk ids
    # never match a PR, so a probe is wasted and its UNKNOWN is meaningless).
    if terminal_fn is not None and not orphan and not degenerate:
        tid = _first_task_id(mission)
        if tid:
            try:
                state = terminal_fn(tid)
            except Exception as e:  # noqa: BLE001 — a probe blow-up is not fatal
                _log('WARN', f'terminal_fn raised for {tid!r}: '
                             f'{type(e).__name__}: {e}')
                state = 'UNKNOWN'
            if state in ('MERGED', 'CLOSED'):
                terminal_state = state
                reasons.append(f'work already {state.lower()} on GitHub')
                score += _W_TERMINAL

    if not reasons:
        return None
    out = {
        'id': mission.get('id'),
        'name': mission.get('name'),
        'score': score,
        'reasons': reasons,
        'age_days': age,
    }
    if terminal_state:
        out['terminal_state'] = terminal_state
    return out


def _load_missions() -> Optional[dict]:
    """Read missions.json (READ-ONLY). Returns the parsed dict, or None on any
    failure. Never raises."""
    if not MISSIONS_JSON.exists():
        _log('WARN', f'missions.json not found at {MISSIONS_JSON}')
        return None
    try:
        with open(MISSIONS_JSON, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _log('WARN', f'missions.json read failed: {type(e).__name__}: {e}')
        return None
    return data if isinstance(data, dict) else None


def reconcile(
    *,
    today: Optional[date] = None,
    terminal_fn: Optional[Callable[[str], str]] = None,
    terminal_cap: int = DEFAULT_TERMINAL_CAP,
    write: bool = True,
) -> dict[str, Any]:
    """One read-only staleness pass. Scores every live proposed card, writes a
    ranked stale-candidate list to CANDIDATES_FILE, and returns a summary.
    Never raises; never writes missions.json.

    `terminal_fn` defaults to task_terminal_state.task_terminal_state (the shared
    'is this work already merged/closed?' probe); pass a stub in tests. It is
    called for at most `terminal_cap` real-looking cards per pass (bounds gh
    cost); the cap and how many cards were skipped by it are reported + logged.
    """
    day = today or date.today()
    summary = {'proposed': 0, 'candidates': 0, 'terminal_checks': 0,
               'terminal_capped': False}

    data = _load_missions()
    if data is None:
        return summary
    missions = data.get('missions')
    if not isinstance(missions, list):
        _log('WARN', 'missions.json has no missions[] list')
        return summary

    proposed = [m for m in missions
                if isinstance(m, dict)
                and m.get('phase') == 'proposed' and not m.get('archived')]
    summary['proposed'] = len(proposed)

    # Cross-card context (one scan, no external calls).
    name_counts: dict = {}
    for m in proposed:
        nm = _norm(m.get('name'))
        if nm:
            name_counts[nm] = name_counts.get(nm, 0) + 1
    shipped_names = {
        _norm(m.get('name')) for m in missions
        if isinstance(m, dict) and m.get('phase') == 'shipped' and _norm(m.get('name'))
    }

    # Resolve the terminal probe once; bound how many cards may use it.
    if terminal_fn is None:
        try:
            import task_terminal_state as tts
            terminal_fn = tts.task_terminal_state
        except Exception as e:  # noqa: BLE001 — probe optional; scorer still runs
            _log('WARN', f'task_terminal_state unavailable ({type(e).__name__}); '
                         'skipping terminal signal this pass')
            terminal_fn = None

    checks_used = 0
    candidates: list[dict] = []
    for m in proposed:
        # Gate the (bounded, gh-calling) terminal probe: real-looking cards only,
        # and only until the cap is hit — everything else scores deterministically.
        use_terminal = None
        if terminal_fn is not None and checks_used < terminal_cap \
                and not _is_orphan_autoproposed(m) and not _is_degenerate_name(m):
            use_terminal = terminal_fn
            checks_used += 1
        cand = score_mission(
            m, name_counts=name_counts, shipped_names=shipped_names,
            today=day, terminal_fn=use_terminal,
        )
        if cand is not None:
            candidates.append(cand)

    candidates.sort(key=lambda c: (c['score'], c.get('age_days') or 0), reverse=True)
    summary['candidates'] = len(candidates)
    summary['terminal_checks'] = checks_used
    # Capped if we stopped probing before every real-looking card was checked.
    real_looking = sum(1 for m in proposed
                       if not _is_orphan_autoproposed(m)
                       and not _is_degenerate_name(m))
    summary['terminal_capped'] = (terminal_fn is not None
                                  and real_looking > terminal_cap)
    if summary['terminal_capped']:
        _log('INFO', f'terminal probe capped at {terminal_cap}; '
                     f'{real_looking - terminal_cap} real-looking card(s) unprobed '
                     'this pass')

    if write:
        payload = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'total_proposed': summary['proposed'],
            'terminal_checks': checks_used,
            'terminal_capped': summary['terminal_capped'],
            'candidates': candidates,
        }
        try:
            CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CANDIDATES_FILE.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(CANDIDATES_FILE)  # atomic swap, never a half-written file
        except OSError as e:
            _log('WARN', f'candidates write failed: {type(e).__name__}: {e}')

    _log('INFO', f'pass: {json.dumps(summary)}')
    return summary


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--once':
        print(json.dumps(reconcile(), ensure_ascii=False))
        sys.exit(0)
    print('usage: mission_staleness.py --once', file=sys.stderr)
    sys.exit(2)
