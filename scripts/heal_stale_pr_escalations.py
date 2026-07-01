#!/usr/bin/env python3
"""heal_stale_pr_escalations.py — clear for-Larry 'Session-less PR needs you'
records whose PR reached a TERMINAL state (MERGED/CLOSED) but was never revisited
by its producer, and reap SYNTHETIC records whose pr_url is not a real GitHub PR.

WHY
---
The mirror-review 'Session-less PR needs you' record is upserted by the
pipeline-stall detector (``heal_pipeline_stall`` red-mirror recovery) and by
``outbox_notifier``. That producer DOES self-clear a record when it re-examines
it and finds the PR MERGED/CLOSED — but ONLY while the PR is still in the stall
detector's input set. Once the PR merges it drops out of that input set, so the
record is never revisited and lingers OPEN forever. Nothing maps a freshly-merged
PR back to its dangling needs-you record, regardless of WHO merged it (Larry from
the desktop, the agent team, or a human on github.com). PRs #749/#751/#766 each
sat MERGED-but-OPEN in the queue this way; #42 lingered ~6 weeks.

This sweeper closes the gap by scanning the OPEN queue DIRECTLY — independent of
any producer re-trigger — and clearing every mirror-review record whose PR is
terminal.

It also reaps SYNTHETIC records: a ``pr_url`` that is not a well-formed
github.com PR URL (a leaked unit-test fixture like ``https://gh/o/r/pull/9``)
can never resolve to a real PR, so ``gh pr view`` on it fails forever and it
would otherwise linger as 'unverifiable'. Such a record is cleared as invalid.

CONSERVATIVE — verify before clearing
--------------------------------------
- An OPEN PR is left untouched (a genuine escalation Larry must act on).
- A gh failure / UNKNOWN state is SKIPPED, never cleared — an outage must never
  masquerade as 'resolved' (the verify-before-alarm posture the healer family
  shares). The next tick re-checks once gh recovers.
- Only a positively MERGED/CLOSED PR, or a provably-synthetic (non-github)
  pr_url, is cleared.
- Scope is limited to ``source == 'mirror-review'`` rows that carry a pr_url.
  Every other needs-you producer owns its own resolve path and is never touched.

Dry-run by default; pass --apply to write. stdlib + sibling modules only; no git.

    cd ~/agent-core && python3 scripts/heal_stale_pr_escalations.py          # dry-run
    cd ~/agent-core && python3 scripts/heal_stale_pr_escalations.py --apply  # write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so sibling imports resolve under systemd / discover.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import for_larry_escalations as fle  # noqa: E402
import task_terminal_state as tts  # noqa: E402

# Only this producer's records are in scope. Other needs-you producers resolve
# through their own path (prefix-sync / decision_resolve) and are never touched.
SOURCE = 'mirror-review'

# A well-formed GitHub PR URL: https://github.com/<owner>/<repo>/pull/<n>, with an
# optional trailing /files, ?query, or #anchor. A pr_url that fails this — a
# leaked fixture like https://gh/o/r/pull/9 — can never resolve to a real PR, so
# it is reaped rather than left to linger forever as gh-'unverifiable'.
_GITHUB_PR_RE = re.compile(
    r'^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+(?:[/?#].*)?$'
)

# Cap gh probes per cycle so a pathologically large queue can never exceed the
# unit's TimeoutStartSec (300s) at ~15s/probe and get SIGKILLed mid-sweep. Any
# rows past the budget are simply left for the next 30-min tick (self-healing);
# synthetic reaps need no probe and are never budget-limited. Real open-row
# counts are tiny (single digits), so this only bites a runaway.
_MAX_PROBES_PER_CYCLE = 15


def log(msg: str, level: str = 'INFO') -> None:
    print(f'[heal_stale_pr_escalations] [{level}] {msg}', flush=True)


def _pr_state(pr_url: str) -> str:
    """MERGED / CLOSED / OPEN / UNKNOWN for a github.com PR URL. Returns UNKNOWN
    on ANY gh failure (missing/timeout/non-zero/unparseable) — an outage is
    'couldn't determine', never a guessed terminal state."""
    data = tts.gh_json(['gh', 'pr', 'view', pr_url, '--json', 'state'])
    if not isinstance(data, dict):
        return tts.UNKNOWN
    return tts.classify_state(data.get('state'))


def evaluate(
    rows: list[dict[str, Any]],
    pr_state: Callable[[str], str],
    max_probes: int = _MAX_PROBES_PER_CYCLE,
) -> list[tuple[dict[str, Any], str]]:
    """Pure decision pass over the OPEN feed. Returns the (row, reason) pairs
    that SHOULD be cleared. The only side effect is the injected ``pr_state``
    probe, so this is unit-testable with no file/network I/O.

    A row is cleared iff it is a mirror-review record with a pr_url that is
    either (a) not a real github.com PR URL — synthetic — or (b) a PR the probe
    positively reports MERGED/CLOSED. OPEN and UNKNOWN both leave the row alone.
    At most ``max_probes`` gh probes run per cycle; rows beyond that budget are
    left for the next tick (never falsely cleared).
    """
    actions: list[tuple[dict[str, Any], str]] = []
    probes = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get('source') != SOURCE:
            continue
        if row.get('resolved') is True:  # defensive: list_open already filters
            continue
        pr_url = row.get('pr_url')
        if not isinstance(pr_url, str) or not pr_url:
            continue  # a non-PR mirror-review row — nothing to verify, not ours
        if not _GITHUB_PR_RE.match(pr_url):
            actions.append((row, f'synthetic pr_url {pr_url!r} (not a github.com PR)'))
            continue
        if probes >= max_probes:
            continue  # probe budget spent — leave for the next tick (self-heals)
        probes += 1
        try:
            state = pr_state(pr_url)
        except Exception:  # noqa: BLE001 - a probe must never abort the sweep
            # Treat an unexpected raise like an outage: UNKNOWN, skip, retry next
            # tick. Clearing on a probe error would be the one unsafe outcome.
            continue
        if state in (tts.MERGED, tts.CLOSED):
            actions.append((row, f'PR {state}'))
        # OPEN  -> genuine escalation, leave it.
        # UNKNOWN -> gh unverifiable, skip; the next tick re-checks once gh recovers.
    return actions


def run_cycle(
    *,
    apply: bool,
    pr_state: Callable[[str], str] = _pr_state,
    list_open: Optional[Callable[[], list[dict[str, Any]]]] = None,
    clear: Optional[Callable[[str], bool]] = None,
) -> int:
    """Scan the OPEN feed and clear stale/synthetic mirror-review records.
    Returns the number of records cleared (0 in dry-run)."""
    rows = (list_open or fle.list_open)()
    clear_fn = clear or fle.clear
    actions = evaluate(rows, pr_state)
    if not actions:
        log('no stale/synthetic PR escalations found')
        return 0
    cleared = 0
    for row, reason in actions:
        rid = row.get('id')
        if not isinstance(rid, str) or not rid:
            continue
        if not apply:
            log(f'WOULD CLEAR {rid} — {reason}')
            continue
        if clear_fn(rid):
            cleared += 1
            log(f'CLEARED {rid} — {reason}')
        else:
            # Nothing open to clear (already resolved by a producer between the
            # snapshot read and now) — a benign race, not a failure.
            log(f'no-op {rid} — already resolved — {reason}')
    log(f'done: {cleared} cleared / {len(actions)} candidate(s)'
        + ('' if apply else ' (dry-run)'))
    return cleared


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description='Clear for-Larry PR escalations whose PR is terminal or synthetic.'
    )
    p.add_argument(
        '--apply', action='store_true',
        help='write clears to the feed (default: dry-run, log only)',
    )
    args = p.parse_args(argv)
    run_cycle(apply=args.apply)
    return 0


if __name__ == '__main__':
    sys.exit(main())
