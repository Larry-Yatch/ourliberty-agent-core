#!/usr/bin/env python3
"""heal_merged_pr_board_reconcile.py — surface OFF-BOARD missions whose work has
already merged, so a forgotten board-cleanup step never loses an initiative.

WHY
---
``heal_missions_card_gc`` auto-ships a mission only when its PR-backed task_ids
are ALL terminal (MERGED/CLOSED). Missions built OFF-BOARD — hand-seeded specs
with empty or only review-shaped task_ids — carry nothing probeable, so the GC
can never confirm them: they sit in drafting/in_flight/ready forever even after
their work merges. That is exactly the class that needed a manual reconcile in
PRs #703/#704. The gap bites regardless of WHO merged the PR (Larry from the
laptop, the agent team, or a human on github.com), because nothing maps a
freshly-merged PR back to an unlinked board card.

WHAT
----
Each tick this scans the OFF-BOARD active missions (reconcilable phase, not
archived/acknowledged/retired, with NO probeable task_id) and asks GitHub
whether a merged PR plausibly carries that mission's work — boundary-matching
the mission id / spec-doc basename against recent PR titles + branches via the
SAME matcher the GC's ``task_terminal_state`` uses. A mission whose token matches
a MERGED PR (and NO still-open PR anywhere) is surfaced to the for-Larry
needs-you lane with the PR as evidence ("looks shipped — confirm"). It is
SELF-CLEARING via ``for_larry_signal.sync_prefix``: when the mission later ships
/ archives, or the match disappears, its signal auto-resolves.

CONSERVATIVE — NEVER GUESS, NEVER RETIRE
----------------------------------------
This NEVER mutates the board (no phase flip, no retire, no missions.json write).
Auto-advancing an off-board mission is unsafe: a single matched PR cannot prove a
multi-phase mission is fully done (an umbrella mission's slice PR is branded with
the umbrella's id), so the only safe action is to surface the evidence for a
human confirm. The SAFE auto-advance — a task_id-linked mission whose every
PR-backed task is terminal — is already done by ``heal_missions_card_gc``, from
any merger. This healer fills only the un-linkable gap, and only by surfacing.

Any matched OPEN PR ⇒ the mission is still building ⇒ NOT surfaced. A ``gh``
failure on a repo skips that repo (a transient failure never fabricates a
"looks shipped"). Both are the conservative posture the whole GC family shares.

Dry-run by default; pass --apply to write the signal. stdlib + sibling modules
only; no git, no board write.

    cd ~/agent-core && python3 scripts/heal_merged_pr_board_reconcile.py          # dry-run
    cd ~/agent-core && python3 scripts/heal_merged_pr_board_reconcile.py --apply  # write
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so sibling imports resolve under systemd / discover.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Reuse the GC healer's path resolver, fail-safe registry reader, the
# off-board predicate's `probeable_task_ids`, the reconcilable-phase set, and its
# log() — ONE definition of "what the board considers active / off-board".
import heal_missions_card_gc as gc  # noqa: E402
# Reuse the GC's conservative PR matcher: boundary-token match of an id against a
# PR's title + head branch, plus the gh listing + state classifier. Same matcher
# task_terminal_state uses, so this healer can never be looser than the GC.
import task_terminal_state as tts  # noqa: E402
# The sanctioned needs-you producer. sync_prefix upserts the current snapshot and
# auto-resolves any of OUR keys whose trigger left the snapshot (self-clear).
import for_larry_signal  # noqa: E402

log = gc.log

SIGNAL_BY = 'heal_merged_pr_board_reconcile'
# Namespace for every record this healer owns. sync_prefix only ever touches keys
# under this prefix, so other producers' needs-you rows are never disturbed.
SIGNAL_PREFIX = 'merged-pr-reconcile:'

# Minimum length of a token we will trust as a mission↔PR link. Mission ids and
# spec basenames are long, specific slugs; a short token would boundary-match
# unrelated PRs. Gates BOTH which tokens we use and the matcher's min_len, so the
# match is always specific (conservative — fewer false "looks shipped").
MIN_TOKEN_LEN = 12


def _kill_switch_path() -> Path:
    """The shared healer kill switch (`~/agents/healers.disabled`). Honors the
    OURLIBERTY_AGENTS_ROOT test/CI override; mirrors the whole healer family so
    `touch healers.disabled` silences this backstop too."""
    return Path(
        os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'),
    ) / 'healers.disabled'


def _spec_basenames(mission: dict[str, Any]) -> list[str]:
    """The stem of each spec_doc path (dir + `.md` + any `#anchor` stripped).
    A spec basename is a strong, specific token that a build PR's title/branch
    often carries even when the mission id does not."""
    out: list[str] = []
    for sd in mission.get('spec_docs') or []:
        if isinstance(sd, str) and sd:
            stem = Path(sd.split('#', 1)[0]).stem
            if stem:
                out.append(stem)
    return out


def match_tokens(mission: dict[str, Any]) -> list[str]:
    """Specific tokens that identify this mission's work in a PR title/branch:
    its id + spec-doc basenames, filtered to length >= MIN_TOKEN_LEN and
    de-duplicated (order-preserving)."""
    seen: set[str] = set()
    out: list[str] = []
    for t in [mission.get('id'), *_spec_basenames(mission)]:
        if isinstance(t, str) and len(t) >= MIN_TOKEN_LEN and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def is_offboard_active_mission(mission: Any) -> bool:
    """True for a mission this healer should consider: an active-phase
    (drafting/in_flight/ready), non-archived/acknowledged/retired mission that
    the GC auto-shipper can NOT verify — i.e. it has no PR-backed task_id. A
    task-linked mission is the GC's job, not ours (don't double-handle)."""
    if not isinstance(mission, dict):
        return False
    # A surfaced row must be actionable + uniquely keyable by mission id — skip a
    # mission with no usable id (else the signal key collides on ':None').
    if not (isinstance(mission.get('id'), str) and mission.get('id')):
        return False
    if mission.get('phase') not in gc.RECONCILABLE_MISSION_PHASES:
        return False
    if mission.get('archived') is True:
        return False
    if mission.get('acknowledged') is True:
        return False
    if mission.get('retired_at'):
        return False
    # Off-board == nothing PR-backed for the GC to probe (empty / only
    # review-shaped task_ids). If it HAS a probeable task, the GC owns it.
    if gc.probeable_task_ids(mission.get('task_ids')):
        return False
    return True


# A PR lister returns the repo's recent PRs (all states) as a list of dicts, or
# None on a gh failure (so the caller skips that repo rather than fabricating).
PrLister = Callable[..., Optional[list[dict[str, Any]]]]


def _default_pr_lister(
    repo: str, *, limit: int, timeout: float,
) -> Optional[list[dict[str, Any]]]:
    data = tts.gh_json(
        [
            'gh', 'pr', 'list', '--repo', tts._qualify_repo(repo),
            '--state', 'all', '--limit', str(limit),
            '--json', 'number,state,title,headRefName,url',
        ],
        timeout=timeout,
    )
    return data if isinstance(data, list) else None


def probe_merged_match(
    tokens: list[str], prs_by_repo: dict[str, Optional[list[dict[str, Any]]]], *,
    min_len: int,
) -> Optional[dict[str, Any]]:
    """Evidence of a MERGED PR carrying this mission's work, or None.

    Matches `tokens` against an already-fetched per-repo PR cache (so the gh
    listing is done ONCE per tick, not once per mission). Collects EVERY matched
    PR across all repos first, THEN decides — so an OPEN PR in any repo vetoes a
    MERGED match. Returns None if any matched PR is OPEN (still building), or if
    nothing matched. A repo whose gh call failed (None in the cache) is skipped
    (never fabricates a match)."""
    if not tokens:
        return None
    matched: list[tuple[str, str, dict[str, Any]]] = []  # (state, repo, pr)
    for repo, prs in prs_by_repo.items():
        if prs is None:  # gh failure for this repo — skip, don't guess
            continue
        for pr in prs:
            if isinstance(pr, dict) and tts._pr_matches(pr, tokens, min_len):
                matched.append((tts.classify_state(pr.get('state')), repo, pr))
    # Any matched PR that is OPEN (still building) OR UNKNOWN (gh returned an
    # unclassifiable state) vetoes — only surface when every matched PR is a
    # clean terminal (the conservative "never falsely looks-shipped" posture).
    if any(state in (tts.OPEN, tts.UNKNOWN) for state, _, _ in matched):
        return None
    merged = [(repo, pr) for state, repo, pr in matched if state == tts.MERGED]
    if not merged:
        return None
    repo, pr = merged[0]
    return {
        'pr_number': pr.get('number'),
        'pr_url': pr.get('url'),
        'repo': repo,
        'branch': pr.get('headRefName'),
        'title': pr.get('title'),
    }


def build_active_signals(
    registry: Any, *,
    prs_by_repo: dict[str, Optional[list[dict[str, Any]]]], now: datetime,
    min_len: int = MIN_TOKEN_LEN,
) -> dict[str, dict[str, Any]]:
    """The current snapshot of for-Larry records, keyed by SIGNAL_PREFIX+id, for
    every off-board active mission that looks shipped. Pure given the PR cache."""
    out: dict[str, dict[str, Any]] = {}
    missions = registry.get('missions') if isinstance(registry, dict) else None
    if not isinstance(missions, list):
        return out
    for mission in missions:
        if not is_offboard_active_mission(mission):
            continue
        tokens = match_tokens(mission)
        if not tokens:
            continue
        ev = probe_merged_match(tokens, prs_by_repo, min_len=min_len)
        if ev is None:
            continue
        mid = mission.get('id')
        name = mission.get('name') or mid
        phase = mission.get('phase')
        pr_ref = f"PR #{ev['pr_number']}" if ev.get('pr_number') else 'a merged PR'
        where = ev.get('pr_url') or ev.get('repo') or ''
        out[SIGNAL_PREFIX + str(mid)] = {
            'headline': f'Mission looks shipped: {name}',
            'context': (
                f"{pr_ref} ({where}) appears to carry this mission's work, but "
                f"the card is still '{phase}'. Confirm it's done so the board "
                f"reflects it — this is an off-board mission (no task link), so "
                f"the auto-shipper can't advance it on its own."
            ),
            'suggested_action': 'Confirm shipped / dismiss in Missions',
            'severity': 'info',
            'mission_id': mid,
            'pr_number': ev.get('pr_number'),
            'pr_url': ev.get('pr_url'),
            'repo': ev.get('repo'),
            'source': SIGNAL_BY,
            'ts': now.isoformat(),
        }
    return out


def run_cycle(
    *, apply: bool, pr_lister: Optional[PrLister] = None,
    repos: Optional[list[str]] = None, now: Optional[datetime] = None,
    read_registry: Optional[Callable[[], Any]] = None,
    signal_path: Optional[Path] = None,
) -> dict[str, Any]:
    """One reconcile tick. Fail-safe: a missing/unreadable registry or a total gh
    outage degrades to "surface nothing" — never raises, never writes the board.

    apply=False (default) only logs the planned snapshot; apply=True writes the
    for-Larry signal via sync_prefix (which also auto-resolves cleared keys)."""
    now = now or datetime.now(timezone.utc)
    repos = repos if repos is not None else tts.default_repos()
    pr_lister = pr_lister or _default_pr_lister

    if read_registry is None:
        def read_registry() -> Any:
            mpath = gc.missions_path(gc.load_repo_paths())
            if mpath is None:
                return None
            return gc.read_missions_registry(mpath)

    registry = read_registry()
    if not isinstance(registry, dict):
        log('merged-pr-reconcile: missions registry unavailable; nothing to do')
        return {'active': {}, 'written': [], 'resolved': []}

    # Lazy gh gate: only hit GitHub when there's at least one off-board mission
    # worth probing. On the common steady-state tick (none) we skip the gh
    # listings entirely and just self-clear any stale rows. Mirrors board-drain's
    # build-the-gate-only-when-there-is-work convention.
    missions = registry.get('missions')
    has_candidates = isinstance(missions, list) and any(
        is_offboard_active_mission(m) and match_tokens(m) for m in missions
    )
    # Fetch each repo's recent PRs ONCE per tick, then match every mission
    # against the cache (not one gh listing per mission).
    prs_by_repo: dict[str, Optional[list[dict[str, Any]]]] = (
        {
            repo: pr_lister(
                repo, limit=tts.DEFAULT_PR_LOOKBACK,
                timeout=tts.DEFAULT_GH_TIMEOUT_SEC,
            )
            for repo in repos
        }
        if has_candidates else {}
    )
    active = build_active_signals(registry, prs_by_repo=prs_by_repo, now=now)
    ids = sorted(k[len(SIGNAL_PREFIX):] for k in active)
    if not apply:
        log(f'merged-pr-reconcile DRY-RUN: {len(active)} mission(s) look shipped '
            f'(would surface): {", ".join(ids) or "(none)"}')
        return {'active': active, 'written': [], 'resolved': []}

    res = for_larry_signal.sync_prefix(active, SIGNAL_PREFIX, path=signal_path)
    log(f'merged-pr-reconcile: surfaced {len(res["written"])} '
        f'looks-shipped mission(s), auto-resolved {len(res["resolved"])} '
        f'(now-cleared): wrote={ids}')
    return {'active': active, **res}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--apply', action='store_true',
        help='write the for-Larry signal (default: dry-run, log only)',
    )
    args = parser.parse_args(argv)
    if _kill_switch_path().exists():
        log('KILLED_BY_SWITCH: healers.disabled present, exiting')
        return 0
    try:
        run_cycle(apply=args.apply)
    except Exception as exc:  # noqa: BLE001 — a healer tick must never crash-loop
        # Degrade QUIETLY (exit 0): this is a self-healing backstop, and a hard
        # exit would mark the oneshot unit failed and page Larry for something
        # the next tick retries. The error is logged for journald/log scanners.
        log(f'merged-pr-reconcile: tick failed (degraded, no board change): '
            f'{type(exc).__name__}: {exc}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
