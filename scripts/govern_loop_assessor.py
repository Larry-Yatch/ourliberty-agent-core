#!/usr/bin/env python3
"""govern_loop_assessor.py — read-only, shadow-first scorer that ranks WHERE
Larry's approvals pay off (Operator Feed Loop, slice 3 — the core slice).

WHY THIS EXISTS
---------------
Larry approves/rejects a stream of needs-Larry decisions (dashboard, Telegram,
auto-rules, larry-email). decision_outcome_ledger.py records every click and
decision_outcome_reconcile.py joins the downstream build result (merged /
merged_regressed / closed_unmerged) onto each decision keyed on decision_key.
Nothing yet READS that joined record to tell Larry WHERE his approvals actually
pay off. Without that, autonomy-widening and attention-allocation are guesswork.

This module is that read. It groups the ledger's decision rows, resolves each
decision_key's EFFECTIVE (latest) build outcome via the ledger's own read API,
scores each joined decision with named weight constants, buckets by actor +
auto-vs-human (with a derivable-repo breakdown), ranks the buckets best-payoff
to worst with a plain-language reason each, and atomically writes the ranking to
its own state file. It auto-acts on NOTHING.

WHAT IT READS  : ~/agents/state/decision-outcome-ledger.jsonl (via
                 decision_outcome_ledger's read API — never a direct file read).
WHAT IT WRITES : ~/agents/state/govern-loop-assessment.json (its own state file,
                 atomic tmp+replace) and its own log.
WHAT IT NEVER DOES : write the ledger, write missions.json, call GitHub, dispatch,
                 or propose autonomy widening. Shadow-first means observe + rank
                 only; turning a ranking into a proposal is a later, separately
                 approved slice (spec §5).

Discipline (mirrors scripts/mission_staleness.py — the read-only-scorer pattern):
stdlib only, never raises (a missing/malformed ledger yields an EMPTY assessment,
not an exception — same never-raise contract the ledger module keeps), WARN-logged
on every fallback. No external calls: the ledger already carries GitHub-derived
build outcomes, so the assessor does not re-query GitHub.

LATEST-ROW-WINS: the effective outcome per decision_key comes from the ledger's
`latest_build_outcomes()` — a later `merged` supersedes an earlier
`closed_unmerged` for the same key (reopen+merge beats abandonment), by any path,
at read time (spec §6, §7.1).

EMPTY-KEY ROWS: a decision row with decision_key '' can never be joined to a build
outcome (ledger KNOWN LIMITATIONS). It is COUNTED in totals but EXCLUDED from
payoff scoring — count-don't-join (spec §5).

DIMENSION GRANULARITY (v1, spec §8): buckets are keyed by (actor, approval_mode)
— coarse on purpose, because the ledger is low-volume today and finer buckets
shatter into untrustworthy singletons. `repo` (derivable from a `pr-<repo>-<n>`
key) is surfaced as a per-bucket breakdown rather than a bucket dimension; task_type
is not derivable from current decision_key shapes and is deliberately omitted until
the reconciler's task-keyed join lands. approval_mode is a heuristic over the
decision row's `notes` — shadow-mode and tunable in one place below.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import decision_outcome_ledger as dol  # noqa: E402  (the read API)

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
ASSESSMENT_FILE = AGENTS_ROOT / 'state' / 'govern-loop-assessment.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'govern-loop-assessor.log'

# ---------------------------------------------------------------------------
# Payoff weights — the ONE place the shadow-mode starting values are declared.
# Tunable: Larry reviews the first weeks of shadow rankings before any decision
# leans on them (spec §8). merged is a clean win; merged_regressed is worse than
# not shipping (shipped-but-bad); closed_unmerged is a soft-negative (abandoned,
# not catastrophic).
# ---------------------------------------------------------------------------
WEIGHT_MERGED = 1.0
WEIGHT_MERGED_REGRESSED = -1.0
WEIGHT_CLOSED_UNMERGED = -0.5

OUTCOME_WEIGHTS = {
    'merged': WEIGHT_MERGED,
    'merged_regressed': WEIGHT_MERGED_REGRESSED,
    'closed_unmerged': WEIGHT_CLOSED_UNMERGED,
}

# `notes` tokens that mark an approval as auto-by-rule rather than a human click.
# Heuristic (spec §4: "derivable from the decision row's notes"); centralized so
# the auto-vs-human cut is tunable in one place as real notes shapes accrue.
_AUTO_APPROVAL_NOTE_TOKENS = (
    'auto-approve', 'auto_approve', 'auto approved', 'autoapprove',
    'auto-approved', 'auto-rule', 'auto rule', 'rule-approved',
    'trust-policy', 'trust policy', 'carve-out', 'carve out',
    'auto-dispatch', 'auto dispatch',
)

# `pr-<repo>-<n>`: repo may contain dashes; the PR number is the trailing digits
# (mirrors decision_outcome_reconcile._PR_COORD_RE). A bare task_id key does not
# match, so repo stays underivable for it.
_PR_COORD_RE = re.compile(r'^pr-(.+)-(\d+)$')

# Reads the whole ledger via the public read API. The ledger is low-volume; a
# large limit returns every row (read_recent returns all_recs[-limit:]).
_READ_ALL = 10 ** 9

# Label for a per-bucket repo tally when the decision_key is a bare task_id
# (not a PR coordinate), so repo cannot be derived.
_REPO_UNDERIVABLE = '(non-pr-key)'


def _log(level: str, msg: str) -> None:
    """Append a structured line to the assessor's own log. Never raises; if the
    log directory is unwritable the line is dropped. Greppable shape matching the
    healer/ledger/scorer constellation."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] [{level}] govern_loop_assessor: {msg}\n')
    except OSError:
        pass


def _is_decision_row(rec: dict) -> bool:
    """A row is a DECISION row when kind == 'decision' OR kind is absent (a
    pre-`kind` row is read as a decision, matching the ledger's own contract)."""
    kind = rec.get('kind')
    return kind == 'decision' or kind is None


def _approval_mode(drow: Optional[dict]) -> str:
    """'auto' if the decision row's notes carry an auto-by-rule tell, else
    'human'. A missing row / missing notes defaults to 'human' (a click with no
    auto marker is treated as a human decision)."""
    notes = drow.get('notes') if isinstance(drow, dict) else None
    hay = notes.lower() if isinstance(notes, str) else ''
    if any(tok in hay for tok in _AUTO_APPROVAL_NOTE_TOKENS):
        return 'auto'
    return 'human'


def _repo_from_key(decision_key: Any) -> str:
    """The repo name from a `pr-<repo>-<n>` key, or _REPO_UNDERIVABLE for a bare
    task_id key (repo not encoded)."""
    if isinstance(decision_key, str):
        m = _PR_COORD_RE.match(decision_key)
        if m:
            return m.group(1)
    return _REPO_UNDERIVABLE


def _bucket_reason(outcome_counts: dict, score: float, scored: int) -> str:
    """Plain-language verdict for one bucket, from its outcome mix + mean payoff.
    scored is guaranteed > 0 by the caller."""
    merged = outcome_counts.get('merged', 0)
    regressed = outcome_counts.get('merged_regressed', 0)
    closed = outcome_counts.get('closed_unmerged', 0)
    mean = score / scored
    tally = (f'{merged} merged clean, {regressed} shipped-but-regressed, '
             f'{closed} abandoned across {scored} scored decision(s)')
    if mean >= 0.75:
        verdict = ('approvals here reliably ship clean — a strong candidate area '
                   'for wider autonomy')
    elif mean > 0:
        verdict = ('mixed: most approvals ship but some fizzle — watch this area '
                   'before widening autonomy')
    elif mean == 0:
        verdict = 'net-neutral payoff — the wins and the fizzles cancel out'
    else:
        verdict = ('approvals here are NOT paying off (shipped-but-regressed or '
                   'abandoned) — keep a close hand here')
    return f'{tally}. {verdict}.'


def build_assessment(records: list[dict], latest_build: dict) -> dict[str, Any]:
    """Pure builder: from the ledger's rows + the effective (latest) build outcome
    per key, produce the ranked assessment payload. No IO. Never raises.

    `records` is every ledger row oldest-first (decision + build_outcome).
    `latest_build` is {decision_key: latest build_outcome VALUE} — the ledger's
    latest-row-wins resolution (empty keys already excluded there).
    """
    # Latest decision row per non-empty key (for bucket dims), + the empty-key
    # tally (counted, never scored).
    decision_row_by_key: dict[str, dict] = {}
    decision_rows = 0
    empty_key_decisions = 0
    for rec in records:
        if not _is_decision_row(rec):
            continue
        decision_rows += 1
        key = rec.get('decision_key')
        if not isinstance(key, str) or not key:
            empty_key_decisions += 1  # count-don't-join (spec §5)
            continue
        decision_row_by_key[key] = rec  # oldest-first -> last write wins

    # Score every key that has an effective build outcome. latest_build only
    # carries non-empty keys, so empty-key rows are structurally excluded.
    buckets: dict[tuple, dict] = {}
    scored_keys = 0
    for key, outcome in latest_build.items():
        weight = OUTCOME_WEIGHTS.get(outcome)
        if weight is None:
            # Defensive: the ledger only records VALID_BUILD_OUTCOMES, but never
            # crash on an unexpected value — skip it and log.
            _log('WARN', f'key {key!r}: unknown build_outcome {outcome!r}; skipped')
            continue
        drow = decision_row_by_key.get(key)
        actor = drow.get('actor') if isinstance(drow, dict) else None
        actor = actor if isinstance(actor, str) and actor else '?'
        mode = _approval_mode(drow)
        repo = _repo_from_key(key)

        bkey = (actor, mode)
        b = buckets.get(bkey)
        if b is None:
            b = {'actor': actor, 'approval_mode': mode, 'payoff_score': 0.0,
                 'scored': 0, 'outcome_counts': {}, 'repos': {}}
            buckets[bkey] = b
        b['payoff_score'] += weight
        b['scored'] += 1
        b['outcome_counts'][outcome] = b['outcome_counts'].get(outcome, 0) + 1
        b['repos'][repo] = b['repos'].get(repo, 0) + 1
        scored_keys += 1

    ranking: list[dict] = []
    for b in buckets.values():
        b['payoff_score'] = round(b['payoff_score'], 4)
        b['payoff_per_decision'] = round(b['payoff_score'] / b['scored'], 4)
        b['reason'] = _bucket_reason(b['outcome_counts'], b['payoff_score'],
                                     b['scored'])
        ranking.append(b)

    # Best payoff to worst: mean payoff first (payoff density), then total score,
    # then sample size — a deterministic, higher-is-better ordering.
    ranking.sort(
        key=lambda b: (b['payoff_per_decision'], b['payoff_score'], b['scored']),
        reverse=True,
    )

    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'weights': dict(OUTCOME_WEIGHTS),
        'totals': {
            'decision_rows': decision_rows,
            'scored_decisions': scored_keys,
            'unjoined_keys': max(len(decision_row_by_key) - scored_keys, 0),
            'empty_key_excluded': empty_key_decisions,
            'buckets': len(ranking),
        },
        'ranking': ranking,
    }


def assess(*, write: bool = True) -> dict[str, Any]:
    """One read-only assessment pass. Reads the ledger via its read API, builds
    the ranked payload, atomically writes it to ASSESSMENT_FILE, and returns it.
    Never raises; never writes the ledger or missions.json.

    A missing / malformed ledger yields an EMPTY assessment (empty ranking), not
    an exception — the read API swallows IO/JSON errors and returns []/{}.
    """
    records = dol.read_recent(_READ_ALL)
    latest_build = dol.latest_build_outcomes()
    payload = build_assessment(records, latest_build)

    if write:
        try:
            ASSESSMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = ASSESSMENT_FILE.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(ASSESSMENT_FILE)  # atomic swap, never a half-written file
        except OSError as e:
            _log('WARN', f'assessment write failed: {type(e).__name__}: {e}')

    _log('INFO', f'pass: {json.dumps(payload["totals"])}')
    return payload


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--once':
        print(json.dumps(assess(), ensure_ascii=False))
        sys.exit(0)
    print('usage: govern_loop_assessor.py --once', file=sys.stderr)
    sys.exit(2)
