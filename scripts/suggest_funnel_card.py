#!/usr/bin/env python3
"""suggest_funnel_card.py — the ONE shared interface by which a team agent
(Beacon / Medic / Pulse) proposes a funnel card (projects-v3 P2 Contract C).

A suggestion is just a `phase: "proposed"` missions.json entry tagged with
`proposed_by=<source>`, dropped into the EXISTING new-mission-queue — the same
safe-inbox/ingest path the dashboard's "+New mission" flow uses. The owning
healer (`heal_orphan_autoregister.drain_new_mission_queue`) is the single
committer that appends queued entries into `missions.json`; nothing here writes
or commits missions.json directly. Because `proposed_by` normalizes to a
suggesting agent, the funnel derive (`dashboard_api._normalize_suggested_source`
→ `_build_funnel`) lands the entry in the PRIMARY (suggested) lane, and the
Narrator (Contract A) briefs it on its GC tick.

There is deliberately NO per-agent path: Beacon, Medic, and Pulse all call the
same `suggest_funnel_card(...)`; the only difference is the `source` argument.

Reuse, not reinvention:
  * queue dir + atomic-write + kebab-id semantics mirror
    `dashboard_api._handle_new_mission` (the proven ingest producer);
  * the drain + single-committer + dedup-by-id live in
    `heal_orphan_autoregister`;
  * the suggesting-agent vocabulary is the funnel derive's `_SUGGESTED_AGENTS`.

CLI (so a shell-launched agent can suggest without importing the module):
    python3 scripts/suggest_funnel_card.py \
        --source pulse --name "Reduce orphan churn" \
        --brief "Pulse noticed repeated re-proposals..." [--repo ourliberty-agent-core]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# The canonical suggesting sources — the same set the funnel derive recognizes
# (`dashboard_api._SUGGESTED_AGENTS`). A `proposed_by` outside this set would be
# treated as orphan/unknown provenance and routed to the SECONDARY lane, so the
# interface refuses it up front rather than silently mis-laning the card.
# `closeout` is a non-agent source (projects-v3 P4): the phase-closeout pass
# drops the loose ends it finds here, alongside the team agents.
SUGGESTING_AGENTS: tuple[str, ...] = ('beacon', 'medic', 'pulse', 'closeout')

# Where queued `<id>.json` entries are dropped for the healer to drain. Mirrors
# `dashboard_api._new_mission_queue_dir` / `heal_orphan_autoregister`'s
# NEWMISSION_QUEUE_SUBPATH so all three resolve to the SAME directory and one
# OURLIBERTY_AGENTS_ROOT override redirects every side in tests.
NEWMISSION_QUEUE_SUBPATH = ('blackboard', 'new-mission-queue')

# Lowercase + collapse non-alphanumerics to single hyphens (mirrors
# dashboard_api._kebab_case so a suggested id matches the +New mission shape).
_KEBAB_RE = re.compile(r'[^a-z0-9]+')


class SuggestionError(ValueError):
    """A suggestion that cannot be enqueued as written (bad source, empty name,
    or a colliding/in-flight id). Raised before any file is written."""


def _agents_root() -> Path:
    return Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))


def new_mission_queue_dir() -> Path:
    """The new-mission-queue directory, resolved at call time so tests redirect
    it via OURLIBERTY_AGENTS_ROOT (same path the dashboard writes + the healer
    drains)."""
    return _agents_root().joinpath(*NEWMISSION_QUEUE_SUBPATH)


def _kebab_case(name: str) -> str:
    return _KEBAB_RE.sub('-', name.strip().lower()).strip('-')


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Delegates to the shared guarded atomic_io writer. Byte-identical to the
    prior inline writer: pretty JSON (indent=2) + trailing newline."""
    import atomic_io
    atomic_io.atomic_write_json(path, obj, trailing_newline=True)


def build_suggested_entry(
    *,
    source: str,
    name: str,
    brief: str,
    repo: Optional[str] = None,
    spec_docs: Optional[list[str]] = None,
    task_ids: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Construct the queued `phase: "proposed"` missions.json entry for one
    suggestion. Pure (no I/O) so it's trivially testable and so callers can
    inspect the shape before enqueuing.

    The entry is shaped like a normal registry entry (id/name/phase/brief/
    spec_docs/task_ids/repo/created/deferred_reason) — so it passes the healer's
    `_valid_mission_entry` — plus the additive provenance fields the funnel +
    Narrator read (`proposed_by`, `proposed_at`). `proposed_by=<source>` is what
    tags the card to its suggesting agent and routes it to the suggested lane.
    """
    source_norm = source.strip().lower() if isinstance(source, str) else ''
    if source_norm not in SUGGESTING_AGENTS:
        raise SuggestionError(
            f'source must be one of {SUGGESTING_AGENTS}, got {source!r}')

    mission_id = _kebab_case(name)
    if not mission_id:
        raise SuggestionError('name kebab-cases to empty string')

    now = now or datetime.now(timezone.utc)
    return {
        'id': mission_id,
        'name': name,
        'phase': 'proposed',
        'brief': brief,
        'spec_docs': list(spec_docs) if spec_docs else [],
        'task_ids': list(task_ids) if task_ids else [],
        'repo': repo,
        'created': now.date().isoformat(),
        'deferred_reason': None,
        # Additive provenance — ignored by existing reads, consumed by the funnel
        # derive (suggested_source) and the Narrator (Contract A).
        'proposed_by': source_norm,
        'proposed_at': now.isoformat(),
    }


def suggest_funnel_card(
    *,
    source: str,
    name: str,
    brief: str,
    repo: Optional[str] = None,
    spec_docs: Optional[list[str]] = None,
    task_ids: Optional[list[str]] = None,
    queue_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Enqueue a source-tagged suggested funnel card via the new-mission-queue.

    Builds the `proposed` entry (validating `source`), then atomically drops
    `<queue_dir>/<id>.json`. Returns `{mission_id, status: 'queued', source}`.

    Idempotency: if a file for the same id is already queued (an in-flight dup
    not yet drained), raises SuggestionError rather than clobbering it — the
    healer dedups by id on drain anyway, so this only guards a double-submit.
    Does NOT inspect missions.json: the healer's drain is the authority on
    already-registered ids (it drops a redundant queue file as deletable).
    """
    entry = build_suggested_entry(
        source=source, name=name, brief=brief, repo=repo,
        spec_docs=spec_docs, task_ids=task_ids, now=now)
    mission_id = entry['id']
    qdir = queue_dir if queue_dir is not None else new_mission_queue_dir()
    queue_path = qdir / f'{mission_id}.json'
    if queue_path.exists():
        raise SuggestionError(
            f'a suggestion with id {mission_id!r} is already queued '
            f'(awaiting drain into missions.json)')
    _atomic_write_json(queue_path, entry)
    return {
        'mission_id': mission_id,
        'status': 'queued',
        'source': entry['proposed_by'],
    }


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Propose a suggested funnel card (P2 Contract C).')
    p.add_argument('--source', required=True,
                   help=f'suggesting agent, one of {SUGGESTING_AGENTS}')
    p.add_argument('--name', required=True, help='human-readable card name')
    p.add_argument('--brief', required=True,
                   help='plain-language brief shown on the card')
    p.add_argument('--repo', default=None, help='optional target repo')
    p.add_argument('--spec-doc', action='append', dest='spec_docs', default=None,
                   help='spec doc reference (repeatable)')
    p.add_argument('--task-id', action='append', dest='task_ids', default=None,
                   help='associated task_id (repeatable)')
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        result = suggest_funnel_card(
            source=args.source, name=args.name, brief=args.brief,
            repo=args.repo, spec_docs=args.spec_docs, task_ids=args.task_ids)
    except SuggestionError as e:
        print(f'suggest-funnel-card: refused — {e}', file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
