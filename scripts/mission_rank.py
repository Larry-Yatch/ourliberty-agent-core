#!/usr/bin/env python3
"""mission_rank.py — the Operator's portfolio rank brain (Operator Feed Loop
slice 4).

WHY THIS EXISTS
---------------
The staleness scorer (slice 3) reconciles the proposed-missions pile down to the
~19 cards that are genuinely actionable. This module is the judgment that comes
next — the thing Larry does by hand today: for each surviving card, "is the
benefit worth the build cost, against what we're trying to do right now?" It
scores each card and writes a RANKED list with a plain-language brief + risk
badge, ready for the Approvals surface (slice 5) to render as one-click items.

THE PORTFOLIO MODEL (config/operator-portfolio.json — Larry's dial)
-------------------------------------------------------------------
The factory builds and maintains a PORTFOLIO of projects (factory itself, RSDPM,
then the next product...). Each config entry carries the project's north-star
goal, lifecycle stage, and a percentage-split `weight` (the throughput
allocation Larry chose). A card joins its project via its `repo` field; the
`default: true` project catches unmapped repos. Rank is:

    net_value = benefit − cost          (each 0–10, LLM-scored vs the project's
                                         OWN north star — not a global rubric)
    rank      = net_value × weight × 10 (the portfolio split arbitrates BETWEEN
                                         projects; ties break toward benefit)

so a high-leverage factory card can still outrank a minor product card, but only
by earning it through the weights — exactly the "internal work earns its rank
through the product" doctrine.

SHADOW-FIRST, READ-ONLY
-----------------------
This pass launches nothing and never writes missions.json. It writes ranked
output to state/mission-rank.json (atomic swap) for the Approvals surface to
render; every launch stays a one-click Larry decision until the govern loop
(slice 7) earns wider autonomy from the decision-outcome ledger.

REUSE (all lazy, all optional — the pass degrades, never dies):
  - missions_narrator.claude_json_roundtrip — the SINGLE allowlisted `claude`
    spawn (tier-dispatched, cost-ledgered, test-guarded). LLM unavailable ⇒
    the card still ranks via the deterministic fallback, flagged `scored: false`.
  - missions_narrator.classify_careful + map_risk (+ trust_policy.evaluate via
    capture_to_task) — the same risk badge the board already renders; import
    failure fails toward caution ('careful').
  - state/mission-staleness-candidates.json (slice 3's output) — stale cards are
    excluded from ranking; a missing file just means nothing is excluded yet.

Discipline: stdlib only at import time; never raises; WARN-logged; LLM + risk
functions injected as seams for tests. LLM calls are capped per pass and the cap
is reported (no silent truncation).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
MISSIONS_JSON = AGENTS_ROOT / 'agents' / 'beacon' / 'workspace' / 'missions.json'
STALENESS_FILE = AGENTS_ROOT / 'state' / 'mission-staleness-candidates.json'
RANK_FILE = AGENTS_ROOT / 'state' / 'mission-rank.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'mission-rank.log'
PORTFOLIO_FILE = Path(__file__).resolve().parent.parent / 'config' / 'operator-portfolio.json'

# Bound LLM spend per pass. The live post-staleness pile is ~19 cards; 40 gives
# headroom without letting a junk-flood burn a tier's budget.
DEFAULT_LLM_CAP = 40


def _log(level: str, msg: str) -> None:
    """Append a structured line to the rank log. Never raises."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] mission_rank: {msg}\n')
    except OSError:
        pass


def _read_json(path: Path) -> Optional[Any]:
    """Parse a JSON file; None on any failure. Never raises."""
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        # ValueError covers JSONDecodeError AND UnicodeDecodeError (binary file)
        _log('WARN', f'{path.name} read failed: {type(e).__name__}: {e}')
        return None


# ---------------- portfolio config ----------------

def load_portfolio(path: Optional[Path] = None) -> Optional[list[dict]]:
    """The project entries from operator-portfolio.json, or None when the config
    is missing/unusable (the caller aborts the pass — ranking without Larry's
    dial would silently substitute someone else's priorities)."""
    data = _read_json(path or PORTFOLIO_FILE)
    if not isinstance(data, dict):
        return None
    projects = data.get('projects')
    if not isinstance(projects, list) or not projects:
        _log('WARN', 'portfolio config has no projects[]')
        return None
    return [p for p in projects if isinstance(p, dict) and p.get('key')]


def resolve_project(repo: Any, projects: list[dict]) -> Optional[dict]:
    """Map a card's `repo` field to its portfolio project: exact bare-name match
    against each project's `repos`, else the `default: true` project, else None
    (unrankable — counted, never guessed)."""
    bare = ''
    if isinstance(repo, str) and repo:
        bare = repo.split('/')[-1].strip()
    if bare:
        for p in projects:
            repos = p.get('repos')
            if isinstance(repos, list) and bare in repos:
                return p
    return next((p for p in projects if p.get('default') is True), None)


# ---------------- LLM benefit/cost scoring ----------------

def build_rank_prompt(mission: dict, project: dict) -> str:
    """The scoring prompt: judge THIS card against ITS project's north star.
    JSON-out so claude_json_roundtrip can parse; plain operator language for the
    brief (the Approvals card renders these strings verbatim)."""
    facts = {
        'card_name': mission.get('name'),
        'card_brief': (mission.get('brief') or '')[:600],
        'project': project.get('name'),
        'project_stage': project.get('stage'),
        'project_north_star': project.get('north_star'),
    }
    return (
        'You are the Operator scoring one proposed mission card for a build '
        'portfolio. Judge it ONLY against its own project\'s north star below.\n\n'
        f'FACTS:\n{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n'
        'Return STRICT JSON (no prose outside it) with exactly these keys:\n'
        '  "benefit": integer 0-10 — contribution to the project north star '
        '(0 = none/obsolete, 10 = directly unlocks it)\n'
        '  "cost": integer 0-10 — rough build cost/risk (0 = trivial tweak, '
        '10 = multi-week rework with wide blast radius)\n'
        '  "what": one plain-English sentence: what this is\n'
        '  "why": one plain-English sentence: why it does/does not matter for '
        'the north star right now\n'
        '  "suggest": one plain-English sentence: what to do (build now / '
        'park / drop and why)\n'
        'Plain operator language — no task ids, branch names, or agent jargon.'
    )


def _coerce_score(v: Any) -> Optional[int]:
    """0–10 int or None. Strings that parse ('7') are accepted; out-of-range
    clamps; anything else is None (treated as not-scored, never guessed)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(0, min(10, n))


def score_card(
    mission: dict,
    project: dict,
    *,
    llm: Optional[Callable[[str], Optional[dict]]],
    risk_fn: Callable[[dict], str],
) -> dict:
    """Score one card. Always returns a rank entry — LLM failure degrades to the
    deterministic fallback (`scored: false`, neutral 5/5) rather than dropping
    the card; a card the brain can't see is worse than one it scored roughly."""
    name = mission.get('name') or '(unnamed)'
    shaped = {'title': name, 'note': mission.get('brief') or '',
              'origin': {'repo': mission.get('repo')}}
    try:
        risk = risk_fn(shaped)
    except Exception as e:  # noqa: BLE001 — seam contract: never-raises; fail
        _log('WARN', f'risk_fn raised for {name!r}: {type(e).__name__}: {e}')
        risk = 'careful'    # toward caution, matching the module doctrine

    benefit = cost = None
    brief = None
    if llm is not None:
        try:
            out = llm(build_rank_prompt(mission, project))
        except Exception as e:  # noqa: BLE001 — LLM blow-up is not fatal
            _log('WARN', f'llm raised for {name!r}: {type(e).__name__}: {e}')
            out = None
        if isinstance(out, dict):
            benefit = _coerce_score(out.get('benefit'))
            cost = _coerce_score(out.get('cost'))
            what = out.get('what'); why = out.get('why'); suggest = out.get('suggest')
            if all(isinstance(s, str) and s.strip() for s in (what, why, suggest)):
                brief = {'what': what.strip(), 'why': why.strip(),
                         'suggest': suggest.strip()}

    scored = benefit is not None and cost is not None
    if not scored:
        benefit, cost = 5, 5  # neutral: surfaces mid-rank, flagged unscored
    if brief is None:
        brief = {
            'what': name,
            'why': f"A proposed card in {project.get('name')} awaiting a decision.",
            'suggest': 'Scoring voice unavailable this pass — judge by hand or '
                       'wait for the next pass.',
        }

    weight = project.get('weight')
    w = weight if isinstance(weight, (int, float)) and weight >= 0 else 0.0
    rank_score = round((benefit - cost) * w * 10, 2)
    # Ties on rank_score break toward higher benefit at SORT time (tuple key in
    # rank()) — an in-score epsilon could invert genuinely-close float weights.

    return {
        'id': mission.get('id'),
        'name': name,
        'repo': mission.get('repo'),
        'project': project.get('key'),
        'stage': project.get('stage'),
        'weight': w,
        'benefit': benefit,
        'cost': cost,
        'rank_score': rank_score,
        'scored': scored,
        'risk': risk,
        'brief': brief,
    }


# ---------------- reuse seams (narrator voice + risk badge) ----------------

def _default_llm() -> Optional[Callable[[str], Optional[dict]]]:
    """The narrator's single allowlisted claude spawn, or None (rank falls back
    to deterministic neutral scoring)."""
    try:
        import missions_narrator as mn
        return mn.claude_json_roundtrip
    except Exception as e:  # noqa: BLE001
        _log('WARN', f'missions_narrator voice unavailable ({type(e).__name__})')
        return None


def _default_risk_fn() -> Callable[[dict], str]:
    """The board's own risk derivation — missions_narrator.derive_risk, which
    already does the classify_careful + trust_policy.evaluate + map_risk dance
    (and returns `(risk, careful)`; we take [0]). Import/eval failure fails
    toward caution — the card reads 'careful' rather than under-warning."""
    try:
        import missions_narrator as mn

        def risk(shaped: dict) -> str:
            try:
                return mn.derive_risk(shaped)[0]
            except Exception:  # noqa: BLE001 — fail toward caution
                return 'careful'
        return risk
    except Exception as e:  # noqa: BLE001
        _log('WARN', f'risk derivation unavailable ({type(e).__name__}); '
                     'all cards read careful')
        return lambda shaped: 'careful'


# ---------------- the pass ----------------

def rank(
    *,
    portfolio_path: Optional[Path] = None,
    llm: Optional[Callable[[str], Optional[dict]]] = None,
    risk_fn: Optional[Callable[[dict], str]] = None,
    llm_cap: int = DEFAULT_LLM_CAP,
    write: bool = True,
) -> dict[str, Any]:
    """One shadow-mode rank pass. Reads the board (READ-ONLY), excludes slice-3
    stale candidates, scores the survivors against Larry's portfolio dial, and
    writes the ranked list to RANK_FILE. Returns a summary. Never raises.

    `llm`/`risk_fn` are test seams; production resolves them from
    missions_narrator/trust_policy lazily. LLM calls stop at `llm_cap`; capped
    cards still rank via the deterministic fallback and the cap is reported.
    """
    summary = {'proposed': 0, 'stale_excluded': 0, 'ranked': 0,
               'unmapped': 0, 'llm_scored': 0, 'llm_capped': False}

    projects = load_portfolio(portfolio_path)
    if projects is None:
        _log('WARN', 'no usable portfolio config — pass aborted (ranking '
                     "without Larry's dial would substitute made-up priorities)")
        return summary

    board = _read_json(MISSIONS_JSON)
    missions = board.get('missions') if isinstance(board, dict) else None
    if not isinstance(missions, list):
        _log('WARN', 'missions.json unusable')
        return summary
    proposed = [m for m in missions
                if isinstance(m, dict)
                and m.get('phase') == 'proposed' and not m.get('archived')]
    summary['proposed'] = len(proposed)

    stale_ids: set = set()
    staleness = _read_json(STALENESS_FILE)
    if isinstance(staleness, dict):
        cands = staleness.get('candidates')
        if isinstance(cands, list):  # guard the CONTAINER too (null/int ≠ list)
            stale_ids = {c.get('id') for c in cands
                         if isinstance(c, dict) and c.get('id')}
    survivors = [m for m in proposed if m.get('id') not in stale_ids]
    summary['stale_excluded'] = len(proposed) - len(survivors)

    the_llm = llm if llm is not None else _default_llm()
    the_risk = risk_fn if risk_fn is not None else _default_risk_fn()

    entries: list[dict] = []
    llm_used = 0
    capped_out = 0  # cards that WANTED an LLM call but the cap was spent
    for m in survivors:
        project = resolve_project(m.get('repo'), projects)
        if project is None:
            summary['unmapped'] += 1
            _log('WARN', f"card {m.get('id')!r} maps to no project and config "
                         'has no default — skipped')
            continue
        use_llm = None
        if the_llm is not None:
            if llm_used < llm_cap:
                use_llm = the_llm
                llm_used += 1
            else:
                capped_out += 1
        entry = score_card(m, project, llm=use_llm, risk_fn=the_risk)
        if entry['scored']:
            summary['llm_scored'] += 1
        entries.append(entry)

    # rank_score desc; ties break toward higher benefit (tuple key — see
    # score_card note on why the tiebreak is not baked into the score).
    entries.sort(key=lambda e: (e['rank_score'], e['benefit']), reverse=True)
    summary['ranked'] = len(entries)
    summary['llm_capped'] = capped_out > 0
    if summary['llm_capped']:
        _log('INFO', f'LLM cap {llm_cap} hit; {capped_out} card(s) ranked on '
                     'fallback this pass')

    if write:
        payload = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'mode': 'shadow',   # slice 4 never launches; Approvals renders this
            'summary': summary,
            'ranked': entries,
        }
        try:
            RANK_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = RANK_FILE.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(RANK_FILE)
        except OSError as e:
            _log('WARN', f'rank write failed: {type(e).__name__}: {e}')

    _log('INFO', f'pass: {json.dumps(summary)}')
    return summary


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--once':
        print(json.dumps(rank(), ensure_ascii=False))
        sys.exit(0)
    print('usage: mission_rank.py --once', file=sys.stderr)
    sys.exit(2)
