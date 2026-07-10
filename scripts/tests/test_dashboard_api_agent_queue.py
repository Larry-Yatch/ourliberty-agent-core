#!/usr/bin/env python3
"""Tests for GET /api/system/agent-queue (Forge Queue panel, Phase 1).

Path-isolation pattern matches test_dashboard_api_system.py: a synthetic
AGENTS_ROOT + WORKTREES_ROOT tree under a tmpdir, monkeypatched directly
onto da._agents_root / da._worktrees_root. The supabase client is injected
via da._get_larry_action_supabase_client (a recording stub that answers the
chain_events select(...).eq('agent', ...) chain), or left as the None path
for the degradation test.

Covers docs/forge-queue-brief.md § Tests:
  - queued: count, oldest-first order, waited_seconds from mtime
  - building: filtered to agent + is_in_flight
  - in_review: review_request with no terminal event appears; one WITH a
    later auto_merge does not
  - done_today: the mirror-attribution join — the building agent's
    session_start/session_done define today's taskset; Mirror's review_pass =>
    merged, review_revision / review_escalate => changes_requested, the
    building agent's marker_error / preflight_reject / cost_budget => failed; a
    review_pass whose task_id is NOT in the taskset is excluded; an event from
    yesterday is excluded (UTC boundary); dedup keeps the latest ts per task
  - supabase-None degradation: in_review / done_today empty, no 500
  - auth: 401 without the token

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_agent_queue
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Set the token BEFORE importing dashboard_api so the auth dependency can
# resolve it at request time. _TokenSetMixin re-sets it in setUp so the fix
# survives sibling modules' teardown ordering under `unittest discover`.
TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN}


class _TokenSetMixin:
    def setUp(self):  # noqa: D401 — unittest hook
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        super().setUp()


# ---------- supabase stub ----------

class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


class _ChainEventsClient:
    """Answers the dashboard's chain_events read chain:
    table('chain_events').select(cols).eq('agent', agent).gte('ts', cutoff)
    .execute().

    Pre-seeded with a flat list of rows; execute() filters by the eq('agent')
    and gte('ts') predicates and then projects each row to EXACTLY the
    selected columns when cols != '*'. Honoring the projection is deliberate:
    the WHERE clause runs against full rows (like Postgres) but the result
    only carries the selected columns, so a query that forgets to select a
    column the deriver depends on fails here the same way it fails in
    production. (This is the flaw that let PR #303 ship done_today broken —
    the prior stub returned full rows, so the missing 'agent' column went
    unnoticed.) The gte filter compares ISO-8601 UTC strings
    lexicographically, same outcome as Postgres timestamptz ordering.
    """

    def __init__(self, rows: Optional[list[dict[str, Any]]] = None):
        self.rows = rows or []
        self._table: Optional[str] = None
        self._filters: dict[str, Any] = {}
        self._gte: dict[str, Any] = {}
        self._cols: str = '*'
        self._order: list[tuple[str, bool]] = []
        self._range: Optional[tuple[int, int]] = None

    def table(self, name: str):
        self._table = name
        self._filters = {}
        self._gte = {}
        self._cols = '*'
        self._order = []
        self._range = None
        return self

    def select(self, cols: str = '*'):
        self._cols = cols
        return self

    def eq(self, col: str, val: Any):
        self._filters[col] = val
        return self

    def gte(self, col: str, val: Any):
        self._gte[col] = val
        return self

    def order(self, col: str, desc: bool = False):
        self._order.append((col, desc))
        return self

    def range(self, lo: int, hi: int):
        # PostgREST .range is an inclusive [lo, hi] window.
        self._range = (lo, hi)
        return self

    def execute(self):
        agent = self._filters.get('agent')
        data = [r for r in self.rows if r.get('agent') == agent]
        for col, val in self._gte.items():
            data = [r for r in data
                    if r.get(col) is not None and r.get(col) >= val]
        # Apply the (ts, event_id) total order the paginated fetch relies on.
        # Sort on FULL rows before projection (event_id is not selected) and
        # apply the least-significant key first so stable sort composes them.
        # The key puts None last so a column absent from the fixtures (e.g.
        # event_id) never trips a None-vs-value comparison.
        for col, desc in reversed(self._order):
            data.sort(
                key=lambda r, c=col: (r.get(c) is None, r.get(c)),
                reverse=desc,
            )
        if self._range is not None:
            lo, hi = self._range
            data = data[lo:hi + 1]
        if self._cols != '*':
            keep = [c.strip() for c in self._cols.split(',')]
            data = [{k: r.get(k) for k in keep} for r in data]
        return _Resp(data)


class _MirrorFetchFailsClient(_ChainEventsClient):
    """Forge fetch succeeds, mirror fetch raises — the partial-outage shape
    the in_review lane must degrade on (not flood with phantom entries)."""

    def execute(self):
        if self._filters.get('agent') == 'mirror':
            raise RuntimeError('transient supabase error')
        return super().execute()


# ---------- fixtures ----------

def _today_noon() -> datetime:
    """Noon-UTC anchor for done-today chain_events fixtures. Using real now()
    makes ``now - timedelta(hours=N)`` cross into yesterday in the 00:00-0N:00
    UTC window; the done-today lane filters by EXACT calendar date, so those
    rows are dropped and the test fails only between midnight and ~05:00 UTC.
    Noon today keeps every ``now - hours`` fixture on today's date while
    preserving relative ordering (the filter is date-based, not ``<= now``, so a
    fixture stamped at noon is still included when the real clock is pre-noon)."""
    return datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0)


def _build_agents_root(tmp: Path) -> Path:
    (tmp / 'state' / 'in-flight').mkdir(parents=True, exist_ok=True)
    for a in ('beacon', 'forge', 'mirror', 'pulse'):
        (tmp / 'inboxes' / a).mkdir(parents=True, exist_ok=True)
    return tmp


def _build_worktrees_root(tmp: Path) -> Path:
    wt = tmp / 'worktrees'
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def _write_inbox_file(agents_root: Path, agent: str, name: str,
                      *, mtime: Optional[float] = None) -> Path:
    p = agents_root / 'inboxes' / agent / name
    p.write_text('{}')
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _write_in_flight(agents_root: Path, *, task_stem: str, agent_id: str,
                     pid: int = 4242,
                     started_at: str = '2026-06-03T07:00:00+00:00',
                     worktree_stem: Optional[str] = None) -> None:
    import json
    p = agents_root / 'state' / 'in-flight' / f'{task_stem}.json'
    entry = {
        'task_stem': task_stem, 'agent_id': agent_id,
        'pid': pid, 'started_at': started_at,
    }
    # Mirror agent_runner._register_in_flight, which stamps the sanitized
    # worktree dir-stem. Omit it to exercise the task_stem fallback path
    # (pre-field sentinels / clean slugs).
    if worktree_stem is not None:
        entry['worktree_stem'] = worktree_stem
    p.write_text(json.dumps(entry))


def _make_worktree(worktrees_root: Path, name: str) -> None:
    (worktrees_root / name).mkdir(parents=True, exist_ok=True)


def _client(agents_root: Path, worktrees_root: Path,
            supabase_client: Any = None) -> TestClient:
    da._agents_root = lambda: agents_root  # type: ignore[assignment]
    da._worktrees_root = lambda: worktrees_root  # type: ignore[assignment]
    da._get_larry_action_supabase_client = lambda: supabase_client  # type: ignore[assignment]
    return TestClient(da.app)


class _Base(_TokenSetMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._orig_agents_root = da._agents_root
        self._orig_worktrees_root = da._worktrees_root
        self._orig_get_client = da._get_larry_action_supabase_client
        # The mirror done-today merged badge calls _resolve_orphan_pr_states
        # (a live GitHub GraphQL read) through the endpoint. Stub it out by
        # default so no test touches the network; the merged-badge test class
        # overrides this with a canned mapping.
        self._orig_pr_state_resolver = da._resolve_orphan_pr_states
        da._resolve_orphan_pr_states = lambda _urls: {}  # type: ignore[assignment]
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-agent-queue-'))
        self.agents_root = _build_agents_root(self.tmp / 'agents')
        self.worktrees_root = _build_worktrees_root(self.tmp)

    def tearDown(self):
        da._agents_root = self._orig_agents_root  # type: ignore[assignment]
        da._worktrees_root = self._orig_worktrees_root  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_get_client  # type: ignore[assignment]
        da._resolve_orphan_pr_states = self._orig_pr_state_resolver  # type: ignore[assignment]


# ==================== auth ====================

class AuthTest(_Base):
    def test_requires_token(self):
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue')
        self.assertEqual(r.status_code, 401)
        r = c.get('/api/system/agent-queue',
                  headers={'X-Dashboard-Token': 'wrong'})
        self.assertEqual(r.status_code, 401)


# ==================== agent param validation ====================

class AgentParamTest(_Base):
    def test_default_agent_is_forge(self):
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['agent'], 'forge')

    def test_unknown_agent_rejected(self):
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue',
                  headers=AUTH, params={'agent': 'bogus'})
        self.assertEqual(r.status_code, 400)

    def test_archetype_per_agent_and_all_lane_keys_present(self):
        # forge is the only builder; the other three are workers. Every
        # response carries all five lanes regardless of archetype.
        c = _client(self.agents_root, self.worktrees_root)
        lanes = {'queued', 'building', 'in_review', 'active', 'done_today'}
        for agent, archetype in (
            ('forge', 'builder'), ('mirror', 'worker'),
            ('beacon', 'worker'), ('pulse', 'worker'),
        ):
            r = c.get('/api/system/agent-queue',
                      headers=AUTH, params={'agent': agent})
            self.assertEqual(r.status_code, 200, agent)
            body = r.json()
            self.assertEqual(body['agent'], agent)
            self.assertEqual(body['archetype'], archetype, agent)
            self.assertTrue(lanes.issubset(body.keys()), agent)


# ==================== queued lane ====================

class QueuedLaneTest(_Base):
    def test_oldest_first_order_and_waited_seconds(self):
        now = time.time()
        # Three real dispatches WITHOUT a `task-` prefix (the gotcha that
        # _agent_inbox_pending would miss). Distinct mtimes, out of order.
        _write_inbox_file(self.agents_root, 'forge', 'aaa.json', mtime=now - 30)
        _write_inbox_file(self.agents_root, 'forge', 'bbb.json', mtime=now - 300)
        _write_inbox_file(self.agents_root, 'forge', 'ccc.json', mtime=now - 90)
        # Noise the matcher must skip: dotfile + non-json.
        _write_inbox_file(self.agents_root, 'forge', '.hidden.json', mtime=now - 999)
        (self.agents_root / 'inboxes' / 'forge' / 'note.txt').write_text('x')

        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        queued = r.json()['queued']
        self.assertEqual([q['task_id'] for q in queued], ['bbb', 'ccc', 'aaa'])
        # waited_seconds derived from mtime; oldest has the largest wait.
        self.assertGreater(queued[0]['waited_seconds'], queued[1]['waited_seconds'])
        self.assertGreater(queued[0]['waited_seconds'], 250)

    def test_other_agents_inbox_not_leaked(self):
        _write_inbox_file(self.agents_root, 'mirror', 'mmm.json')
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.json()['queued'], [])


class QueuedLaneInFlightFilterTest(_Base):
    """A task already building (in the in-flight registry for this agent)
    must NOT also appear in the queued lane — it belongs to the building lane."""

    def test_building_task_excluded_from_queued(self):
        _write_inbox_file(self.agents_root, 'forge', 'building-1.json')
        _write_inbox_file(self.agents_root, 'forge', 'queued-1.json')
        _write_in_flight(self.agents_root, task_stem='building-1', agent_id='forge')
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([q['task_id'] for q in r.json()['queued']], ['queued-1'])

    def test_building_matched_by_explicit_task_id(self):
        # inbox filename differs from the payload's task_id (the in-flight key)
        import json
        (self.agents_root / 'inboxes' / 'forge' / 'file-stem.json').write_text(
            json.dumps({'task_id': 'real-id', 'prompt': 'x'}))
        _write_in_flight(self.agents_root, task_stem='real-id', agent_id='forge')
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.json()['queued'], [])

    def test_other_agent_in_flight_does_not_exclude(self):
        _write_inbox_file(self.agents_root, 'forge', 'shared.json')
        _write_in_flight(self.agents_root, task_stem='shared', agent_id='mirror')
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual([q['task_id'] for q in r.json()['queued']], ['shared'])


# ==================== building lane ====================

class BuildingLaneTest(_Base):
    def test_filtered_to_agent_and_in_flight(self):
        # Worktree names follow `wt-<agent>-<task_id>` with the lowercase
        # task_id the reader's regex accepts.
        # forge in-flight worktree -> appears.
        _make_worktree(self.worktrees_root, 'wt-forge-build-thing-001')
        _write_in_flight(self.agents_root,
                         task_stem='build-thing-001', agent_id='forge')
        # forge worktree NOT in-flight -> excluded.
        _make_worktree(self.worktrees_root, 'wt-forge-stale-002')
        # mirror in-flight worktree -> excluded (wrong agent).
        _make_worktree(self.worktrees_root, 'wt-mirror-review-003')
        _write_in_flight(self.agents_root,
                         task_stem='review-003', agent_id='mirror')

        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        building = r.json()['building']
        self.assertEqual(len(building), 1)
        self.assertEqual(building[0]['task_id'], 'build-thing-001')
        self.assertIn('branch', building[0])
        self.assertIn('age_seconds', building[0])


class BuildingLaneSanitizedTaskIdTest(_Base):
    """Regression: a genuinely-building task whose id is NOT a clean lowercase
    slug must still appear in the building lane.

    The worktree dir bakes in the SANITIZED stem (`worktree_manager.
    _sanitize_task_id`: non-`[A-Za-z0-9_-]` -> `-`, capped at 50), while the
    in-flight sentinel is keyed by the RAW task_stem. The old reader compared
    the parsed dir stem against the raw stems and used a lowercase-only regex,
    so any non-slug id silently vanished from the lane while genuinely
    building. Two failure shapes are covered:

      * uppercase/`_` — preserved by the sanitizer (raw == sanitized), so the
        only break was the lowercase-only `_WORKTREE_RE` failing to PARSE the
        dir; the widened charset fixes it via the `task_stem` fallback.
      * a char mapped to `-` (`foo:bar` -> `foo-bar`) — raw != sanitized, so
        matching needs the sentinel's `worktree_stem`; the canonical RAW id is
        then surfaced as the lane's task_id.
    """

    def test_uppercase_and_underscore_id_appears(self):
        # `_sanitize_task_id('Build_Thing_07')` == 'Build_Thing_07' (uppercase
        # and `_` are preserved), so the dir carries the raw stem verbatim and
        # the `task_stem` fallback matches even without a `worktree_stem` field
        # — the fix here is purely the widened `_WORKTREE_RE` charset.
        _make_worktree(self.worktrees_root, 'wt-forge-Build_Thing_07')
        _write_in_flight(self.agents_root,
                         task_stem='Build_Thing_07', agent_id='forge')

        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        building = r.json()['building']
        self.assertEqual([b['task_id'] for b in building], ['Build_Thing_07'])
        self.assertEqual(building[0]['branch'], 'forge/Build_Thing_07')

    def test_mapped_char_id_matches_via_worktree_stem(self):
        # `foo:bar` sanitizes to `foo-bar` for the dir, so raw != sanitized.
        # The sentinel records the sanitized `worktree_stem`; membership must
        # match on it, and the lane must surface the canonical RAW id.
        _make_worktree(self.worktrees_root, 'wt-forge-foo-bar')
        _write_in_flight(self.agents_root,
                         task_stem='foo:bar', agent_id='forge',
                         worktree_stem='foo-bar')

        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        building = r.json()['building']
        self.assertEqual([b['task_id'] for b in building], ['foo:bar'])

    def test_over_length_id_matches_via_worktree_stem(self):
        # An id longer than the sanitizer's 50-char cap: the dir stem is the
        # truncation, raw != sanitized, so the `worktree_stem` link is required.
        raw = 'b' * 60
        truncated = 'b' * 50
        _make_worktree(self.worktrees_root, f'wt-forge-{truncated}')
        _write_in_flight(self.agents_root,
                         task_stem=raw, agent_id='forge',
                         worktree_stem=truncated)

        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        building = r.json()['building']
        self.assertEqual([b['task_id'] for b in building], [raw])

    def test_wrong_agent_sanitized_stem_does_not_match(self):
        # A mirror sentinel sharing the sanitized stem must NOT light up a
        # forge worktree — membership is keyed by (agent_id, worktree_stem).
        _make_worktree(self.worktrees_root, 'wt-forge-foo-bar')
        _write_in_flight(self.agents_root,
                         task_stem='foo:bar', agent_id='mirror',
                         worktree_stem='foo-bar')

        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['building'], [])


# ============= building <-> in_review dedup (overlap fix) =============

class BuildingInReviewDedupTest(_Base):
    """A forge build keeps its worktree/in-flight sentinel through review, so a
    task awaiting a verdict reads as in-flight (building lane) AND has an open
    review_request (in_review lane). The later lane wins: it must show ONLY in
    in_review, never both. Mirrors QueuedLaneInFlightFilterTest one step down
    the lifecycle."""

    # task_ids are lowercase slugs: only a task whose sanitized worktree id
    # equals its in-flight task_stem (i.e. a clean slug) ever reaches the
    # building lane, and the review_request carries that same id — so these
    # are exactly the ids that can double-list.
    def _building_worktree(self, task_stem: str) -> None:
        _make_worktree(self.worktrees_root, f'wt-forge-{task_stem}')
        _write_in_flight(self.agents_root,
                         task_stem=task_stem, agent_id='forge')

    def test_in_review_task_excluded_from_building(self):
        # Same task is both in-flight (building) and under review.
        self._building_worktree('task-a')
        rows = [
            {'agent': 'forge', 'task_id': 'task-a',
             'event_type': 'review_request', 'pr_url': 'https://pr/A',
             'ts': datetime.now(timezone.utc).isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual([x['task_id'] for x in body['in_review']], ['task-a'])
        self.assertEqual([x['task_id'] for x in body['building']], [])

    def test_still_building_task_stays_in_building(self):
        # In-flight but NOT yet under review -> still belongs in building.
        self._building_worktree('task-a')
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient([]))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual([x['task_id'] for x in r.json()['building']],
                         ['task-a'])

    def test_closed_review_task_stays_in_building(self):
        # review_request THEN a verdict -> not in_review (closed); the worktree
        # is mid-rebuild, so it must remain in the building lane.
        self._building_worktree('task-a')
        now = datetime.now(timezone.utc)
        rows = [
            {'agent': 'forge', 'task_id': 'task-a',
             'event_type': 'review_request', 'pr_url': 'https://pr/A',
             'ts': (now - timedelta(minutes=20)).isoformat()},
            {'agent': 'mirror', 'task_id': 'task-a',
             'event_type': 'review_revision', 'pr_url': 'https://pr/A',
             'ts': (now - timedelta(minutes=5)).isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        body = r.json()
        self.assertEqual(body['in_review'], [])
        self.assertEqual([x['task_id'] for x in body['building']], ['task-a'])

    def test_fetch_failure_does_not_drop_from_building(self):
        # Mirror verdict fetch raises -> in_review degrades to []. The task
        # must NOT vanish from both lanes; it stays visible in building.
        self._building_worktree('task-a')
        rows = [
            {'agent': 'forge', 'task_id': 'task-a',
             'event_type': 'review_request', 'pr_url': 'https://pr/A',
             'ts': datetime.now(timezone.utc).isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _MirrorFetchFailsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        body = r.json()
        self.assertEqual(body['in_review'], [])
        self.assertEqual([x['task_id'] for x in body['building']], ['task-a'])

    def test_other_in_review_task_does_not_drop_building(self):
        # A different task under review must not evict an unrelated in-flight
        # build from the building lane.
        self._building_worktree('task-a')
        rows = [
            {'agent': 'forge', 'task_id': 'task-b',
             'event_type': 'review_request', 'pr_url': 'https://pr/B',
             'ts': datetime.now(timezone.utc).isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual([x['task_id'] for x in r.json()['building']],
                         ['task-a'])


# ==================== in_review lane ====================

class InReviewLaneTest(_Base):
    def _rows(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return [
            # task A: review_request, no terminal -> in_review.
            {'agent': 'forge', 'task_id': 'taskA',
             'event_type': 'review_request', 'pr_url': 'https://pr/A',
             'ts': (now - timedelta(minutes=10)).isoformat()},
            # task B: review_request THEN auto_merge -> NOT in_review.
            {'agent': 'forge', 'task_id': 'taskB',
             'event_type': 'review_request', 'pr_url': 'https://pr/B',
             'ts': (now - timedelta(minutes=20)).isoformat()},
            {'agent': 'forge', 'task_id': 'taskB',
             'event_type': 'auto_merge', 'pr_url': 'https://pr/B',
             'ts': (now - timedelta(minutes=5)).isoformat()},
        ]

    def test_review_request_without_terminal_appears(self):
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(self._rows()))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        in_review = r.json()['in_review']
        ids = [x['task_id'] for x in in_review]
        self.assertEqual(ids, ['taskA'])
        self.assertEqual(in_review[0]['pr_url'], 'https://pr/A')
        self.assertIsNotNone(in_review[0]['since'])

    def test_only_requested_agent_rows(self):
        rows = self._rows() + [
            {'agent': 'mirror', 'task_id': 'taskZ',
             'event_type': 'review_request', 'pr_url': 'https://pr/Z',
             'ts': datetime.now(timezone.utc).isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        ids = [x['task_id'] for x in r.json()['in_review']]
        self.assertEqual(ids, ['taskA'])

    def test_mirror_verdict_closes_entry(self):
        # forge-queue-in-review-lane: Mirror's verdict rides agent='mirror'
        # rows — the forge fetch never sees it. The verdict join (by
        # task_id) must close the entry, else every reviewed task sits in
        # the lane forever (auto_merge & co. never land in forge's rows).
        now = datetime.now(timezone.utc)
        rows = [
            {'agent': 'forge', 'task_id': 'taskC',
             'event_type': 'review_request', 'pr_url': 'https://pr/C',
             'ts': (now - timedelta(minutes=30)).isoformat()},
            {'agent': 'mirror', 'task_id': 'taskC',
             'event_type': 'review_pass', 'pr_url': 'https://pr/C',
             'ts': (now - timedelta(minutes=5)).isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.json()['in_review'], [])

    def test_rerun_review_request_after_verdict_reopens(self):
        # REVISION closes the first entry; the re-review dispatch emits a
        # fresh review_request and the task re-enters the lane with the
        # new `since`.
        now = datetime.now(timezone.utc)
        rerun_ts = (now - timedelta(minutes=10)).isoformat()
        rows = [
            {'agent': 'forge', 'task_id': 'taskD',
             'event_type': 'review_request', 'pr_url': 'https://pr/D',
             'ts': (now - timedelta(minutes=30)).isoformat()},
            {'agent': 'mirror', 'task_id': 'taskD',
             'event_type': 'review_revision', 'pr_url': 'https://pr/D',
             'ts': (now - timedelta(minutes=20)).isoformat()},
            {'agent': 'forge', 'task_id': 'taskD',
             'event_type': 'review_request', 'pr_url': 'https://pr/D',
             'ts': rerun_ts},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        in_review = r.json()['in_review']
        self.assertEqual([x['task_id'] for x in in_review], ['taskD'])
        self.assertEqual(in_review[0]['since'], rerun_ts)

    def test_unrelated_mirror_rows_do_not_close(self):
        # A verdict for a DIFFERENT task and a non-verdict mirror event for
        # the SAME task must both leave the entry open.
        now = datetime.now(timezone.utc)
        rows = [
            {'agent': 'forge', 'task_id': 'taskE',
             'event_type': 'review_request', 'pr_url': 'https://pr/E',
             'ts': (now - timedelta(minutes=30)).isoformat()},
            {'agent': 'mirror', 'task_id': 'taskF',
             'event_type': 'review_pass', 'pr_url': 'https://pr/F',
             'ts': (now - timedelta(minutes=5)).isoformat()},
            {'agent': 'mirror', 'task_id': 'taskE',
             'event_type': 'session_done', 'pr_url': None,
             'ts': (now - timedelta(minutes=5)).isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        ids = [x['task_id'] for x in r.json()['in_review']]
        self.assertEqual(ids, ['taskE'])

    def test_mirror_fetch_failure_degrades_lane_to_empty(self):
        # Forge fetch OK, mirror (verdict) fetch raises: deriving anyway
        # would resurrect every open review_request with nothing able to
        # close it. The lane must degrade to [] for this poll, not flood.
        c = _client(self.agents_root, self.worktrees_root,
                    _MirrorFetchFailsClient(self._rows()))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['in_review'], [])

    def test_review_request_outside_window_ages_out(self):
        # A review_request older than _QUEUE_EVENTS_WINDOW_DAYS is excluded
        # by the fetch cutoff — a review that died without any closing event
        # (wedged session, dropped verdict emit) must not ghost forever.
        now = datetime.now(timezone.utc)
        ancient = now - timedelta(days=da._QUEUE_EVENTS_WINDOW_DAYS + 1)
        rows = [
            {'agent': 'forge', 'task_id': 'taskGhost',
             'event_type': 'review_request', 'pr_url': 'https://pr/G',
             'ts': ancient.isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.json()['in_review'], [])


# ==================== done_today lane ====================

class DoneTodayLaneTest(_Base):
    def _session(self, task_id: str, ts) -> dict[str, Any]:
        return {'agent': 'forge', 'task_id': task_id,
                'event_type': 'session_start', 'pr_url': None,
                'ts': ts.isoformat()}

    def test_mirror_join_three_outcomes_and_exclusions(self):
        now = _today_noon()
        yesterday = now - timedelta(days=1)
        rows = [
            # forge's sessions today define the taskset.
            self._session('merged1', now - timedelta(hours=3)),
            self._session('changed1', now - timedelta(hours=3)),
            self._session('escalated1', now - timedelta(hours=3)),
            self._session('failed1', now - timedelta(hours=3)),
            self._session('failed2', now - timedelta(hours=3)),
            self._session('boundary1', now - timedelta(hours=3)),

            # MERGED: mirror review_pass for a forge task. An earlier
            # review_revision on the SAME task must lose to the later pass.
            {'agent': 'mirror', 'task_id': 'merged1',
             'event_type': 'review_revision', 'pr_url': 'https://pr/1',
             'ts': (now - timedelta(hours=1)).isoformat()},
            {'agent': 'mirror', 'task_id': 'merged1',
             'event_type': 'review_pass', 'pr_url': 'https://pr/1',
             'ts': now.isoformat()},
            # CHANGES_REQUESTED via review_revision and review_escalate.
            {'agent': 'mirror', 'task_id': 'changed1',
             'event_type': 'review_revision', 'pr_url': 'https://pr/2',
             'ts': now.isoformat()},
            {'agent': 'mirror', 'task_id': 'escalated1',
             'event_type': 'review_escalate', 'pr_url': 'https://pr/3',
             'ts': now.isoformat()},
            # FAILED: the building agent's own failure markers.
            {'agent': 'forge', 'task_id': 'failed1',
             'event_type': 'marker_error', 'pr_url': None,
             'ts': now.isoformat()},
            {'agent': 'forge', 'task_id': 'failed2',
             'event_type': 'cost_budget', 'pr_url': None,
             'ts': now.isoformat()},

            # EXCLUDED: review_pass whose task_id is NOT in forge's taskset
            # (e.g. a build mirror reviewed for another agent).
            {'agent': 'mirror', 'task_id': 'notmine',
             'event_type': 'review_pass', 'pr_url': 'https://pr/x',
             'ts': now.isoformat()},
            # EXCLUDED: yesterday's verdict for an in-set task (UTC boundary).
            {'agent': 'mirror', 'task_id': 'boundary1',
             'event_type': 'review_pass', 'pr_url': 'https://pr/b',
             'ts': yesterday.isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        done = r.json()['done_today']
        by_task = {d['task_id']: d for d in done}
        self.assertEqual(
            set(by_task),
            {'merged1', 'changed1', 'escalated1', 'failed1', 'failed2'},
        )
        self.assertEqual(by_task['merged1']['outcome'], 'merged')
        self.assertEqual(by_task['merged1']['reason'], 'review_pass')
        self.assertEqual(by_task['merged1']['pr_url'], 'https://pr/1')
        self.assertEqual(by_task['changed1']['outcome'], 'changes_requested')
        self.assertEqual(by_task['changed1']['reason'], 'review_revision')
        self.assertEqual(by_task['escalated1']['outcome'], 'changes_requested')
        self.assertEqual(by_task['escalated1']['reason'], 'review_escalate')
        self.assertEqual(by_task['failed1']['outcome'], 'failed')
        self.assertEqual(by_task['failed1']['reason'], 'marker_error')
        self.assertEqual(by_task['failed2']['outcome'], 'failed')
        self.assertEqual(by_task['failed2']['reason'], 'cost_budget')
        # sorted by `at` descending: merged1's review_pass (now) is newest.
        self.assertEqual(done[0]['task_id'], 'merged1')

    def test_populates_through_production_column_projection(self):
        # Regression guard for PR #303: done_today only populates if the
        # production select(...) projection carries the 'agent' column the
        # taskset/failure gating depends on. The stub now honors the
        # projection, so a query that drops 'agent' yields an empty taskset
        # and this test fails — exactly as production did.
        now = _today_noon()
        rows = [
            self._session('m1', now - timedelta(hours=2)),
            self._session('c1', now - timedelta(hours=2)),
            self._session('f1', now - timedelta(hours=2)),
            # merged: a review_pass whose task_id IS in forge's taskset.
            {'agent': 'mirror', 'task_id': 'm1',
             'event_type': 'review_pass', 'pr_url': 'https://pr/m1',
             'ts': now.isoformat()},
            # changes_requested.
            {'agent': 'mirror', 'task_id': 'c1',
             'event_type': 'review_revision', 'pr_url': 'https://pr/c1',
             'ts': now.isoformat()},
            # failed: forge's own marker.
            {'agent': 'forge', 'task_id': 'f1',
             'event_type': 'preflight_reject', 'pr_url': None,
             'ts': now.isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        done = r.json()['done_today']
        by_task = {d['task_id']: d['outcome'] for d in done}
        self.assertEqual(by_task, {
            'm1': 'merged',
            'c1': 'changes_requested',
            'f1': 'failed',
        })

    def test_stub_honors_column_projection(self):
        # Direct contract check on the fixture: select(cols) must drop columns
        # not in `cols`, and select('*') must keep them. If this regresses, the
        # done_today tests stop exercising the real fetch path.
        rows = [{'agent': 'forge', 'event_type': 'session_start',
                 'task_id': 't', 'pr_url': None, 'ts': '2026-06-04T00:00:00+00:00'}]
        stub = _ChainEventsClient(rows)
        projected = stub.table('chain_events').select(
            'agent,event_type,task_id,pr_url,ts').eq('agent', 'forge').execute()
        self.assertEqual(set(projected.data[0]), {
            'agent', 'event_type', 'task_id', 'pr_url', 'ts'})
        dropped = stub.table('chain_events').select(
            'event_type,task_id,pr_url,ts').eq('agent', 'forge').execute()
        self.assertNotIn('agent', dropped.data[0])
        full = stub.table('chain_events').select('*').eq(
            'agent', 'forge').execute()
        self.assertIn('agent', full.data[0])

    def test_review_pass_not_in_taskset_excluded(self):
        # A lone mirror review_pass with no matching forge session today must
        # NOT appear — the taskset join is what attributes builds to forge.
        now = datetime.now(timezone.utc)
        rows = [
            {'agent': 'mirror', 'task_id': 'orphan',
             'event_type': 'review_pass', 'pr_url': 'https://pr/o',
             'ts': now.isoformat()},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['done_today'], [])


# ==================== builder byte-compat regression ====================

class BuilderRegressionTest(_Base):
    """Forge's lane SHAPES must stay byte-for-byte compatible with Phase 1
    (UI Phase B ships separately). The only additive top-level changes are
    `archetype` and the always-present (empty for builder) `active` lane."""

    def test_forge_done_item_shape_unchanged_and_active_empty(self):
        now = _today_noon()
        rows = [
            {'agent': 'forge', 'task_id': 'm1', 'event_type': 'session_start',
             'pr_url': None, 'ts': (now - timedelta(hours=2)).isoformat()},
            {'agent': 'mirror', 'task_id': 'm1', 'event_type': 'review_pass',
             'pr_url': 'https://pr/m1', 'ts': now.isoformat()},
        ]
        # A worker in-flight entry that must NOT leak into forge's (empty)
        # active lane.
        _write_in_flight(self.agents_root, task_stem='pulse-job',
                         agent_id='pulse')
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH,
                  params={'agent': 'forge'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['archetype'], 'builder')
        self.assertEqual(body['active'], [])
        done = body['done_today']
        self.assertEqual(len(done), 1)
        # Exact builder done-item key set — no worker `message` field bleeds in.
        self.assertEqual(set(done[0]), {'task_id', 'pr_url', 'outcome',
                                        'reason', 'at'})
        self.assertEqual(done[0]['outcome'], 'merged')


# ==================== worker active lane ====================

class WorkerActiveLaneTest(_Base):
    def test_in_flight_entries_parsed_filtered_and_newest_first(self):
        # REAL registry parse (no mock): write JSON sentinels under
        # state/in-flight/ and assert the active lane reads them.
        now = datetime.now(timezone.utc)
        _write_in_flight(self.agents_root, task_stem='pulse-old',
                         agent_id='pulse',
                         started_at=(now - timedelta(minutes=30)).isoformat())
        _write_in_flight(self.agents_root, task_stem='pulse-new',
                         agent_id='pulse',
                         started_at=(now - timedelta(minutes=2)).isoformat())
        # Wrong-agent entry must be filtered out.
        _write_in_flight(self.agents_root, task_stem='forge-build',
                         agent_id='forge')
        c = _client(self.agents_root, self.worktrees_root)
        r = c.get('/api/system/agent-queue', headers=AUTH,
                  params={'agent': 'pulse'})
        self.assertEqual(r.status_code, 200)
        active = r.json()['active']
        # Newest-started first; forge entry excluded.
        self.assertEqual([a['task_id'] for a in active],
                         ['pulse-new', 'pulse-old'])
        self.assertGreater(active[1]['age_seconds'], active[0]['age_seconds'])
        self.assertGreater(active[1]['age_seconds'], 1500)


# ==================== worker done_today lane ====================

class WorkerDoneTodayLaneTest(_Base):
    def _done(self, task_id: str, success: bool, ts,
              message: str = 'ok') -> dict[str, Any]:
        return {'agent': 'pulse', 'task_id': task_id,
                'event_type': 'session_done', 'pr_url': None,
                'ts': ts.isoformat(),
                'payload': {'success': success, 'message': message}}

    def test_succeeded_and_failed_via_payload_success(self):
        now = _today_noon()
        yesterday = now - timedelta(days=1)
        rows = [
            self._done('jobS', True, now - timedelta(hours=1), 'all good'),
            self._done('jobF', False, now, 'boom'),
            # A dupe for jobS earlier the same day must lose to the later ts.
            self._done('jobS', False, now - timedelta(hours=5), 'stale'),
            # Yesterday's session_done is excluded (UTC boundary).
            self._done('jobOld', True, yesterday, 'old'),
            # Another agent's row must not leak.
            {'agent': 'mirror', 'task_id': 'notpulse',
             'event_type': 'session_done', 'pr_url': None,
             'ts': now.isoformat(), 'payload': {'success': True}},
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH,
                  params={'agent': 'pulse'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['archetype'], 'worker')
        done = body['done_today']
        by_task = {d['task_id']: d for d in done}
        self.assertEqual(set(by_task), {'jobS', 'jobF'})
        self.assertEqual(by_task['jobS']['outcome'], 'succeeded')
        self.assertEqual(by_task['jobS']['message'], 'all good')
        self.assertEqual(by_task['jobF']['outcome'], 'failed')
        # Exact worker done-item key set — no builder `reason` field.
        self.assertEqual(
            set(done[0]),
            {'task_id', 'outcome', 'at', 'message', 'verdict', 'pr_url',
             'pr_state'},
        )
        # Non-mirror workers never look up merge state.
        self.assertIsNone(by_task['jobS']['pr_state'])
        # Non-mirror workers never emit review verdicts: fields stay None.
        self.assertIsNone(by_task['jobS']['verdict'])
        # sorted by `at` desc: jobF (now) is newest.
        self.assertEqual(done[0]['task_id'], 'jobF')

    def test_mirror_verdict_joined_onto_done_card(self):
        # A Mirror review session that SUCCEEDED but sent the PR back for
        # fixes must carry verdict=review_revision (+ the PR url) so the
        # card doesn't read as contradictory next to the queued -revN
        # re-review of the same PR.
        now = _today_noon()
        pr = 'https://github.com/x/y/pull/841'

        def ev(event_type, ts, task_id='pr-841', pr_url=pr, payload=None):
            return {'agent': 'mirror', 'task_id': task_id,
                    'event_type': event_type, 'pr_url': pr_url,
                    'ts': ts.isoformat(), 'payload': payload}

        rows = [
            # round-0: session succeeded, verdict = revision.
            ev('review_revision', now - timedelta(hours=2)),
            ev('session_done', now - timedelta(hours=2),
               payload={'success': True, 'message': 'round 0 done'}),
            # a verdict-less mirror session on another task stays bare.
            ev('session_done', now, task_id='audit-1', pr_url=None,
               payload={'success': True, 'message': 'no verdict here'}),
            # yesterday's verdict for pr-841 must NOT leak into today.
            ev('review_pass', now - timedelta(days=1),
               task_id='pr-other'),
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH,
                  params={'agent': 'mirror'})
        self.assertEqual(r.status_code, 200)
        by_task = {d['task_id']: d for d in r.json()['done_today']}
        self.assertEqual(by_task['pr-841']['verdict'], 'review_revision')
        self.assertEqual(by_task['pr-841']['pr_url'], pr)
        self.assertIsNone(by_task['audit-1']['verdict'])

    def test_mirror_latest_verdict_today_wins(self):
        # Two rounds in one day (round-0 REVISION then rev1 PASS): the kept
        # card is the latest session, and the verdict must match that round.
        now = _today_noon()
        pr = 'https://github.com/x/y/pull/841'

        def ev(event_type, ts, payload=None):
            return {'agent': 'mirror', 'task_id': 'pr-841',
                    'event_type': event_type, 'pr_url': pr,
                    'ts': ts.isoformat(), 'payload': payload}

        rows = [
            ev('review_revision', now - timedelta(hours=3)),
            ev('session_done', now - timedelta(hours=3),
               payload={'success': True}),
            ev('review_pass', now - timedelta(hours=1)),
            ev('session_done', now - timedelta(hours=1),
               payload={'success': True}),
        ]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH,
                  params={'agent': 'mirror'})
        by_task = {d['task_id']: d for d in r.json()['done_today']}
        self.assertEqual(by_task['pr-841']['verdict'], 'review_pass')

    def test_message_truncated(self):
        now = datetime.now(timezone.utc)
        long_msg = 'x' * 5000
        rows = [self._done('jobL', True, now, long_msg)]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH,
                  params={'agent': 'pulse'})
        msg = r.json()['done_today'][0]['message']
        self.assertEqual(len(msg), da._WORKER_DONE_MESSAGE_MAXLEN)


# ============ mirror done-today merged/not-merged badge ============
#
# A review_pass card gets a live GitHub state (MERGED | OPEN | CLOSED) so the
# operator can spot a PR that passed review but never landed. The lookup runs
# ONLY for review_pass cards carrying a pr_url; revision/escalate cards and
# non-mirror workers stay pr_state=None. Fail-safe: a resolver error → None.

class MirrorDoneMergeBadgeTest(_Base):
    PR = 'https://github.com/x/y/pull/841'
    PR2 = 'https://github.com/x/y/pull/842'

    def _ev(self, event_type, ts, task_id='pr-841', pr_url=None, payload=None):
        return {'agent': 'mirror', 'task_id': task_id,
                'event_type': event_type, 'pr_url': pr_url,
                'ts': ts.isoformat(), 'payload': payload}

    def _run(self, rows, resolver):
        da._resolve_orphan_pr_states = resolver  # type: ignore[assignment]
        c = _client(self.agents_root, self.worktrees_root,
                    _ChainEventsClient(rows))
        r = c.get('/api/system/agent-queue', headers=AUTH,
                  params={'agent': 'mirror'})
        self.assertEqual(r.status_code, 200)
        return {d['task_id']: d for d in r.json()['done_today']}

    def test_review_pass_card_gets_live_state(self):
        now = _today_noon()
        rows = [
            self._ev('review_pass', now, task_id='pr-841', pr_url=self.PR),
            self._ev('session_done', now, task_id='pr-841', pr_url=self.PR,
                     payload={'success': True, 'message': 'passed'}),
        ]
        by_task = self._run(rows, lambda urls: {self.PR: 'MERGED'})
        self.assertEqual(by_task['pr-841']['verdict'], 'review_pass')
        self.assertEqual(by_task['pr-841']['pr_state'], 'MERGED')

    def test_passed_but_not_merged_open_and_closed(self):
        # The whole point: a passed PR still OPEN, and one CLOSED unmerged.
        now = _today_noon()
        rows = [
            self._ev('review_pass', now, task_id='pr-841', pr_url=self.PR),
            self._ev('session_done', now, task_id='pr-841', pr_url=self.PR,
                     payload={'success': True}),
            self._ev('review_pass', now - timedelta(minutes=5),
                     task_id='pr-842', pr_url=self.PR2),
            self._ev('session_done', now - timedelta(minutes=5),
                     task_id='pr-842', pr_url=self.PR2,
                     payload={'success': True}),
        ]
        by_task = self._run(
            rows, lambda urls: {self.PR: 'OPEN', self.PR2: 'CLOSED'})
        self.assertEqual(by_task['pr-841']['pr_state'], 'OPEN')
        self.assertEqual(by_task['pr-842']['pr_state'], 'CLOSED')

    def test_revision_card_gets_no_lookup(self):
        # A revision card is not expected to merge — no badge, and its url is
        # never handed to the resolver.
        now = _today_noon()
        seen: list[list[str]] = []

        def resolver(urls):
            seen.append(list(urls))
            return {u: 'MERGED' for u in urls}

        rows = [
            self._ev('review_revision', now, task_id='pr-841', pr_url=self.PR),
            self._ev('session_done', now, task_id='pr-841', pr_url=self.PR,
                     payload={'success': True}),
        ]
        by_task = self._run(rows, resolver)
        self.assertEqual(by_task['pr-841']['verdict'], 'review_revision')
        self.assertIsNone(by_task['pr-841']['pr_state'])
        # The revision url must not have been looked up at all.
        self.assertEqual(seen, [])

    def test_unresolved_url_stays_none(self):
        # Resolver returns nothing for the url (degraded / not found) → None,
        # not a crash and not a stale value.
        now = _today_noon()
        rows = [
            self._ev('review_pass', now, task_id='pr-841', pr_url=self.PR),
            self._ev('session_done', now, task_id='pr-841', pr_url=self.PR,
                     payload={'success': True}),
        ]
        by_task = self._run(rows, lambda urls: {})
        self.assertIsNone(by_task['pr-841']['pr_state'])

    def test_resolver_raises_is_fail_safe(self):
        now = _today_noon()

        def boom(_urls):
            raise RuntimeError('github down')

        rows = [
            self._ev('review_pass', now, task_id='pr-841', pr_url=self.PR),
            self._ev('session_done', now, task_id='pr-841', pr_url=self.PR,
                     payload={'success': True}),
        ]
        by_task = self._run(rows, boom)
        self.assertIsNone(by_task['pr-841']['pr_state'])


# ==================== supabase-None degradation ====================

class SupabaseNoneDegradationTest(_Base):
    def test_review_and_done_empty_no_500(self):
        # queued + building still populated; supabase lanes degrade to [].
        _write_inbox_file(self.agents_root, 'forge', 'qqq.json')
        c = _client(self.agents_root, self.worktrees_root, supabase_client=None)
        r = c.get('/api/system/agent-queue', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body['queued']), 1)
        self.assertEqual(body['in_review'], [])
        self.assertEqual(body['done_today'], [])
        self.assertIn('captured_at', body)

    def test_worker_supabase_none_and_missing_in_flight_dir(self):
        # Worker with no supabase client AND no in-flight dir: active +
        # done_today degrade to [], never 500. queued still works.
        import shutil
        shutil.rmtree(self.agents_root / 'state' / 'in-flight')
        _write_inbox_file(self.agents_root, 'pulse', 'ppp.json')
        c = _client(self.agents_root, self.worktrees_root, supabase_client=None)
        r = c.get('/api/system/agent-queue', headers=AUTH,
                  params={'agent': 'pulse'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['archetype'], 'worker')
        self.assertEqual(len(body['queued']), 1)
        self.assertEqual(body['active'], [])
        self.assertEqual(body['done_today'], [])
        self.assertEqual(body['building'], [])
        self.assertEqual(body['in_review'], [])


if __name__ == '__main__':
    unittest.main()
