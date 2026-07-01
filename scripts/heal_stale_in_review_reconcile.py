#!/usr/bin/env python3
"""heal_stale_in_review_reconcile.py — close phantom Forge "In-Review" cards
whose backing PR reached a TERMINAL state (MERGED/CLOSED) out of band, with no
verdict ever recorded.

WHY
---
The Operations -> Forge -> In-Review lane is not stored; it is DERIVED live from
chain_events by ``dashboard_api._derive_in_review``: a task stays In-Review while
its latest ``review_request`` has no LATER closing event. The only closers are a
forge terminal event (``_QUEUE_TERMINAL_EVENT_TYPES``) or a Mirror verdict
(``_QUEUE_VERDICT_EVENT_TYPES``).

When a PR is merged/closed OUT OF BAND *after* that last ``review_request`` —
a desktop merge, the agent team's auto-merge, a human on github.com, or Mirror's
own skip-on-merged guard — no verdict/terminal event is ever written for that
request. The derivation then has nothing to close the card, so it lingers in
In-Review until its events age out of the 14-day query window. Observed live:
PR #698 (Mirror skipped its final review because the PR was already merged — the
very feature #698 shipped) and PR #703 (Mirror ran a session but emitted no
verdict, then the PR merged out of band).

This reconciler closes the gap. Each tick it re-derives the forge In-Review lane
(reusing dashboard_api's exact functions so it can never diverge from what the
dashboard shows), checks GitHub for each card's PR state, and for a PR that
reached MERGED/CLOSED *after* the review_request it emits a ``review_obsolete``
chain_event — a forge terminal type the derivation honors — so the card drops on
the next poll.

CONSERVATIVE — verify before closing
-------------------------------------
- OPEN / gh-unverifiable (UNKNOWN) PRs are left untouched: an in-flight review or
  a gh outage must never be closed as "obsolete".
- Closes ONLY when the PR's terminal timestamp is strictly AFTER the review_request
  ``since`` — the exact phantom signature. If a terminal PR predates the request,
  a real verdict may still be coming, so it is left alone.
- ``ts`` on the emitted row is the PR's terminal timestamp, so the deterministic
  event_id makes re-runs idempotent (they upsert the same row, never duplicate).
- gh probes are capped per cycle so a runaway lane can't blow the unit timeout.

Dry-run by default; pass --apply to emit. stdlib + sibling modules only; no git.

    cd ~/agent-core && python3 scripts/heal_stale_in_review_reconcile.py          # dry-run
    cd ~/agent-core && python3 scripts/heal_stale_in_review_reconcile.py --apply  # emit
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so sibling imports resolve under systemd / discover.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import chain_event_emit as cee  # noqa: E402
import task_terminal_state as tts  # noqa: E402

# The forge terminal event this healer emits to close a phantom card. Registered
# in chain_event_shipper.KNOWN_EVENT_TYPES (so emit_event admits it and the audit
# healer never flags it) and in dashboard_api._QUEUE_TERMINAL_EVENT_TYPES (so the
# in_review derivation treats it as a closer). It is NOT in _derive_done_today's
# inputs, so a closed phantom silently leaves In-Review without wrongly appearing
# as a "merged today" outcome (the merge was days ago, out of band).
CLOSE_EVENT_TYPE = 'review_obsolete'

# Only forge opens PRs / runs the review lifecycle, so it is the sole in_review
# BUILDER (dashboard_api._BUILDER_AGENTS). The closing event must land in forge's
# own row set to be seen as a terminal closer, so we emit as agent='forge'.
BUILDER_AGENT = 'forge'

# A well-formed GitHub PR URL. Cards are derived from our own review_request
# events so pr_url is normally canonical, but a malformed/non-github url can't be
# resolved to a PR — skip it rather than guess.
_GITHUB_PR_RE = re.compile(
    r'^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+(?:[/?#].*)?$'
)

# Cap gh probes per cycle so a pathologically large lane can't exceed the unit's
# TimeoutStartSec at ~15s/probe and get SIGKILLed mid-sweep. Cards past the budget
# are left for the next tick (self-healing). Real In-Review counts are tiny.
_MAX_PROBES_PER_CYCLE = 15


def log(msg: str, level: str = 'INFO') -> None:
    print(f'[heal_stale_in_review_reconcile] [{level}] {msg}', flush=True)


def _parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 ts (accepts a trailing 'Z' and naive strings) into a
    tz-aware UTC datetime, or None if unparseable. Mirrors
    dashboard_api._ts_to_dt so the healer's "later than review_request" test
    agrees exactly with the derivation it is reconciling. (Trailing-'Z'
    parsing needs Python >=3.11; the droplet runs 3.12.)"""
    if not isinstance(ts_str, str):
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pr_terminal(pr_url: str) -> tuple[str, Optional[str]]:
    """Return (state, terminal_ts) for a github.com PR URL.

    state is MERGED / CLOSED / OPEN / UNKNOWN (UNKNOWN on ANY gh failure — an
    outage is "couldn't determine", never a guessed terminal state).
    terminal_ts is the mergedAt (MERGED) / closedAt (CLOSED) ISO timestamp, or
    None for OPEN/UNKNOWN or when GitHub omits it."""
    data = tts.gh_json(
        ['gh', 'pr', 'view', pr_url, '--json', 'state,mergedAt,closedAt']
    )
    if not isinstance(data, dict):
        return tts.UNKNOWN, None
    state = tts.classify_state(data.get('state'))
    if state == tts.MERGED:
        return state, data.get('mergedAt')
    if state == tts.CLOSED:
        return state, data.get('closedAt')
    return state, None


def evaluate(
    cards: list[dict[str, Any]],
    pr_terminal: Callable[[str], tuple[str, Optional[str]]],
    max_probes: int = _MAX_PROBES_PER_CYCLE,
) -> list[tuple[dict[str, Any], str, str]]:
    """Pure decision pass over the derived in_review lane. Returns the
    (card, pr_state, close_ts_iso) triples that SHOULD be closed. The only side
    effect is the injected ``pr_terminal`` probe, so this is unit-testable with
    no network I/O.

    A card is closed iff its PR is positively MERGED/CLOSED AND that terminal
    state is strictly LATER than the card's ``since`` (its latest review_request
    ts) — the phantom signature. OPEN, UNKNOWN, a missing/earlier terminal ts,
    and a malformed pr_url all leave the card alone. At most ``max_probes`` gh
    probes run per cycle; cards beyond the budget wait for the next tick.
    """
    actions: list[tuple[dict[str, Any], str, str]] = []
    probes = 0
    for card in cards:
        if not isinstance(card, dict):
            continue
        pr_url = card.get('pr_url')
        since = card.get('since')
        task_id = card.get('task_id')
        if not (isinstance(pr_url, str) and pr_url and isinstance(task_id, str)
                and task_id):
            continue
        if not _GITHUB_PR_RE.match(pr_url):
            continue  # can't resolve a non-github url to a PR — not ours to close
        since_dt = _parse_ts(since)
        if since_dt is None:
            continue  # no anchorable request ts — can't prove "merged after"
        if probes >= max_probes:
            continue  # probe budget spent — leave for the next tick (self-heals)
        probes += 1
        try:
            state, close_ts = pr_terminal(pr_url)
        except Exception:  # noqa: BLE001 - a probe must never abort the sweep
            continue  # treat a raise like an outage: skip, retry next tick
        if state not in (tts.MERGED, tts.CLOSED):
            continue  # OPEN -> genuine review; UNKNOWN -> gh unverifiable
        close_dt = _parse_ts(close_ts)
        if close_dt is None or close_dt <= since_dt:
            # No terminal ts, or the PR was already terminal when the review was
            # requested — not the out-of-band-after-request phantom. A real
            # verdict may still land, so leave it.
            continue
        actions.append((card, state, close_dt.isoformat()))
    return actions


def run_cycle(
    *,
    apply: bool,
    get_client: Optional[Callable[[], Any]] = None,
    fetch: Optional[Callable[[Any, str], Optional[list[dict[str, Any]]]]] = None,
    derive: Optional[Callable[..., list[dict[str, Any]]]] = None,
    pr_terminal: Callable[[str], tuple[str, Optional[str]]] = _pr_terminal,
    emit: Optional[Callable[..., bool]] = None,
    max_probes: int = _MAX_PROBES_PER_CYCLE,
) -> int:
    """Re-derive the forge in_review lane and close phantom cards whose PR is
    terminal-after-request. Returns the number of closing events emitted (0 in
    dry-run). dashboard_api is imported lazily so the module (and its evaluate()
    tests) load without the fastapi/uvicorn web-app dependency."""
    if get_client is None or fetch is None or derive is None:
        import dashboard_api as dapi  # noqa: E402 - heavy import, deferred
        get_client = get_client or dapi._get_larry_action_supabase_client
        fetch = fetch or dapi._fetch_chain_events_for_agent
        derive = derive or dapi._derive_in_review
    emit_fn = emit or cee.emit_event

    client = get_client()
    if client is None:
        log('no Supabase client (missing creds?) — cannot reconcile', 'WARN')
        return 0
    rows = fetch(client, BUILDER_AGENT)
    verdict_rows = fetch(client, 'mirror')
    if rows is None or verdict_rows is None:
        # None = fetch FAILED (vs [] = no rows). Deriving on a failed fetch would
        # resurrect phantoms; skip this tick, exactly like the dashboard reader.
        log('chain_events fetch failed — skipping this cycle', 'WARN')
        return 0
    cards = derive(rows, verdict_rows)
    actions = evaluate(cards, pr_terminal, max_probes=max_probes)
    if not actions:
        log(f'no stale in_review cards ({len(cards)} in lane)')
        return 0

    emitted = 0
    for card, state, close_ts in actions:
        tid = card['task_id']
        pr_url = card['pr_url']
        reason = (f'PR {state} out of band at {close_ts} after review_request '
                  f'{card.get("since")}; no verdict recorded')
        if not apply:
            log(f'WOULD CLOSE {tid} ({pr_url}) — {reason}')
            continue
        ok = emit_fn(
            event_type=CLOSE_EVENT_TYPE,
            agent=BUILDER_AGENT,
            task_id=tid,
            pr_url=pr_url,
            ts=close_ts,
            payload={
                'pr_state': state,
                'reason': reason,
                'source': 'heal-stale-in-review-reconcile',
            },
        )
        if ok:
            emitted += 1
            log(f'CLOSED {tid} ({pr_url}) — emitted {CLOSE_EVENT_TYPE} — {reason}')
        else:
            log(f'emit FAILED for {tid} ({pr_url}) — will retry next tick', 'WARN')
    log(f'done: {emitted} closed / {len(actions)} candidate(s)'
        + ('' if apply else ' (dry-run)'))
    return emitted


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description='Close phantom Forge in_review cards whose PR merged/closed '
                    'out of band with no verdict recorded.'
    )
    p.add_argument(
        '--apply', action='store_true',
        help='emit closing events (default: dry-run, log only)',
    )
    args = p.parse_args(argv)
    run_cycle(apply=args.apply)
    return 0


if __name__ == '__main__':
    sys.exit(main())
