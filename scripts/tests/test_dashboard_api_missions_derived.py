#!/usr/bin/env python3
"""Tests for GET /api/missions/derived (Missions v2 Phase 2 § 3-4).

Spec: `agents/beacon/specs/missions-v2-phase2-resurfacing-and-derive.md`
§ 3 (relocated derive + parked[] + orphan-readability + filters) and § 4
(parity gate).

The mission-phase derive (phase / aggregate / orphan) is ported byte-faithfully
from the dashboard's `lib/mission-queries.ts`. § 4 pins that the Python derive's
`missions[]` + `orphans[]` (the pre-Phase-2 fields) deep-equal a hand-written
expected output computed from the rules — NOT generated from the code under test,
so the parity gate is non-circular. The additive Phase-2 fields (parked[] and the
orphan-readability keys) are asserted separately.

Path-isolation pattern mirrors `test_dashboard_api_missions.py`: each test owns a
fresh tmpdir with the committed fixtures copied in; `da._missions_json_path` /
`da._captures_json_path` are rebound onto it so the live registry is never
touched. chain_events come from an in-process Supabase stub honouring the
`.table().select().in_()/.gte().order().execute()` chain — no live network, no
credential.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_missions_derived
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN}
ENDPOINT = '/api/missions/derived'
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'missions_derive'

# Fixed clock for deterministic stale/aging derivation. Stale cutoff =
# now - 14d = 2026-05-27T12:00:00Z; orphan window = now - 30d.
NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)

# Additive orphan-readability keys, stripped before the § 4 parity comparison.
_PHASE2_ORPHAN_KEYS = (
    'derived_phase', 'state_badge', 'terminal', 'stalled', 'label', 'repo',
    'branch',
)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------- in-process Supabase stub ----------


class _Resp:
    def __init__(self, data: list[dict]) -> None:
        self.data = data


class _Query:
    """Honours the subset of the supabase-py builder the derive uses."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = list(rows)

    def select(self, *_a, **_k) -> '_Query':
        return self

    def in_(self, col: str, values: list) -> '_Query':
        allowed = set(values)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def gte(self, col: str, value) -> '_Query':
        self._rows = [
            r for r in self._rows
            if r.get(col) is not None and r.get(col) >= value
        ]
        return self

    def order(self, *_a, **_k) -> '_Query':
        return self

    def execute(self) -> _Resp:
        return _Resp(list(self._rows))


class _StubClient:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.calls = 0

    def table(self, _name: str) -> _Query:
        self.calls += 1
        return _Query(self._rows)


class _BoomClient:
    """Raises on use — proves the derive degrades instead of 500-ing."""

    def table(self, _name: str):  # noqa: ANN001
        raise RuntimeError('supabase unavailable')


# ---------- base harness ----------


class _DerivedTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # _expected_token() resolves DASHBOARD_API_TOKEN at request time; a real
        # token may already be exported in the environment, so pin it to the
        # test value (and restore on teardown) — mirrors _TokenSetMixin.
        self._orig_env_token = os.environ.get('DASHBOARD_API_TOKEN')
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.addCleanup(self._restore_env_token)

        self._tmp = Path(
            __import__('tempfile').mkdtemp(prefix='missions_derived_'),
        )
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self._tmp, ignore_errors=True),
        )
        self._missions_path = self._tmp / 'missions.json'
        self._captures_path = self._tmp / 'captures.json'
        self._missions_path.write_text((FIXTURES / 'missions.json').read_text())
        self._captures_path.write_text((FIXTURES / 'captures.json').read_text())

        self._events = _load('chain_events.json')['chain_events']
        self._stub = _StubClient(self._events)

        self._orig_missions = da._missions_json_path
        self._orig_captures = da._captures_json_path
        self._orig_client = da._get_larry_action_supabase_client
        # Network-safety: stub the live PR-state resolver to {} so the route
        # (EndpointTest) never reaches GitHub even if a real GITHUB_TOKEN is in
        # the environment. Tests exercising the PR-state path inject their own
        # resolver into _handle_missions_derived directly (see OrphanPrStateTest).
        self._orig_resolver = da._resolve_orphan_pr_states
        da._missions_json_path = lambda: self._missions_path
        da._captures_json_path = lambda: self._captures_path
        da._get_larry_action_supabase_client = lambda: self._stub
        da._resolve_orphan_pr_states = lambda _urls: {}
        self.addCleanup(self._restore)

        self.client = TestClient(da.app)

    def _restore(self) -> None:
        da._missions_json_path = self._orig_missions
        da._captures_json_path = self._orig_captures
        da._get_larry_action_supabase_client = self._orig_client
        da._resolve_orphan_pr_states = self._orig_resolver

    def _restore_env_token(self) -> None:
        if self._orig_env_token is None:
            os.environ.pop('DASHBOARD_API_TOKEN', None)
        else:
            os.environ['DASHBOARD_API_TOKEN'] = self._orig_env_token

    def _derive(self, **kwargs) -> dict:
        """Call the pure handler with the fixed NOW (the route can't inject it)."""
        return da._handle_missions_derived(
            missions_path=self._missions_path,
            captures_path=self._captures_path,
            supabase_client=self._stub,
            repo=kwargs.get('repo'),
            task_id=kwargs.get('task_id'),
            now=NOW,
        )


# ---------- § 4 parity gate ----------


class ParityTest(_DerivedTestBase):
    def test_missions_match_expected_byte_for_byte(self) -> None:
        expected = _load('expected_parity.json')
        actual = self._derive()
        self.assertEqual(actual['missions'], expected['missions'])

    def test_orphans_pre_phase2_fields_match_expected(self) -> None:
        expected = _load('expected_parity.json')
        actual = self._derive()
        stripped = [
            {k: v for k, v in o.items() if k not in _PHASE2_ORPHAN_KEYS}
            for o in actual['orphans']
        ]
        self.assertEqual(stripped, expected['orphans'])

    def test_schema_version_is_one(self) -> None:
        self.assertEqual(self._derive()['schema_version'], 1)


# ---------- additive Phase-2 fields ----------


class Phase2FieldsTest(_DerivedTestBase):
    def test_orphan_readability_fields_match_expected(self) -> None:
        expected = _load('expected_phase2.json')['orphan_readability']
        actual = self._derive()
        by_id = {o['task_id']: o for o in actual['orphans']}
        for task_id, exp in expected.items():
            got = {k: by_id[task_id][k] for k in _PHASE2_ORPHAN_KEYS}
            self.assertEqual(got, exp, f'orphan readability mismatch: {task_id}')

    def test_parked_array_matches_expected(self) -> None:
        expected = _load('expected_phase2.json')['parked']
        self.assertEqual(self._derive()['parked'], expected)

    def test_stalled_orphan_stays_visible_not_terminal(self) -> None:
        # The core invariant: a quiet-but-unmerged orphan is `stalled` (visible),
        # never `terminal` (hidden). Hiding is driven only by a real shipped
        # signal, never by a clock.
        orphans = {o['task_id']: o for o in self._derive()['orphans']}
        stalled = orphans['orphan-stalled-old']
        self.assertTrue(stalled['stalled'])
        self.assertFalse(stalled['terminal'])
        self.assertEqual(stalled['state_badge'], 'stalled')

    def test_shipped_orphan_is_terminal(self) -> None:
        orphans = {o['task_id']: o for o in self._derive()['orphans']}
        shipped = orphans['orphan-shipped-pr']
        self.assertTrue(shipped['terminal'])
        self.assertFalse(shipped['stalled'])
        self.assertEqual(shipped['state_badge'], 'shipped')


# ---------- orphan filtering (infrastructure / alerts / fixtures) ----------


class OrphanFilteringTest(_DerivedTestBase):
    def test_infrastructure_and_noise_excluded(self) -> None:
        ids = {o['task_id'] for o in self._derive()['orphans']}
        for excluded in (
            'orphan-deploy-thing', 'orphan-heal-thing', 'notify-routing-xyz',
            'this is an alert message', 't-zero', 'review-pr-501',
        ):
            self.assertNotIn(excluded, ids)

    def test_registered_tasks_never_surface_as_orphans(self) -> None:
        ids = {o['task_id'] for o in self._derive()['orphans']}
        for registered in (
            'p2-derive-endpoint', 'p2-digest-generator', 'p2-dashboard-cutover',
            'p2-orphan-readability',
        ):
            self.assertNotIn(registered, ids)

    def test_orphans_sorted_newest_first(self) -> None:
        ts = [o['last_event_ts'] for o in self._derive()['orphans']]
        self.assertEqual(ts, sorted(ts, reverse=True))


# ---------- ?repo= / ?task_id= filters (§ 3.2) ----------


class FilterTest(_DerivedTestBase):
    def test_repo_filter_narrows_missions_orphans_parked(self) -> None:
        out = self._derive(repo='ourliberty-agent-core')
        self.assertTrue(
            all(m['repo'] == 'ourliberty-agent-core' for m in out['missions']),
        )
        self.assertTrue(
            all(o['repo'] == 'ourliberty-agent-core' for o in out['orphans']),
        )
        self.assertTrue(
            all(p['repo'] == 'ourliberty-agent-core' for p in out['parked']),
        )
        self.assertEqual(
            [p['capture_id'] for p in out['parked']], ['cap-aging-one'],
        )

    def test_repo_filter_unknown_repo_empties_everything(self) -> None:
        out = self._derive(repo='no-such-repo')
        self.assertEqual(out['missions'], [])
        self.assertEqual(out['orphans'], [])
        self.assertEqual(out['parked'], [])

    def test_task_id_filter_keeps_target_and_active_collisions(self) -> None:
        # p2-dashboard-cutover lives in ourliberty-dashboard alongside
        # p2-orphan-readability (also awaiting_merge → active). Both kept.
        out = self._derive(task_id='p2-dashboard-cutover')
        kept_tasks = {
            t['task_id'] for m in out['missions'] for t in m['tasks']
        }
        self.assertIn('p2-dashboard-cutover', kept_tasks)
        self.assertIn('p2-orphan-readability', kept_tasks)
        self.assertTrue(
            all(m['repo'] == 'ourliberty-dashboard' for m in out['missions']),
        )

    def test_task_id_filter_drops_shipped_collisions(self) -> None:
        # agent-core repo: p2-derive-endpoint is shipped, p2-digest-generator is
        # in_flight. Targeting the shipped task keeps it (the target is always
        # kept) but the OTHER shipped tasks on the repo would be dropped; here
        # the in_flight collision survives.
        out = self._derive(task_id='p2-derive-endpoint')
        kept_tasks = {
            t['task_id'] for m in out['missions'] for t in m['tasks']
        }
        self.assertIn('p2-derive-endpoint', kept_tasks)
        self.assertIn('p2-digest-generator', kept_tasks)

    def test_task_id_and_repo_mismatch_yields_empty(self) -> None:
        out = self._derive(task_id='p2-derive-endpoint', repo='ourliberty-dashboard')
        self.assertEqual(out['missions'], [])
        self.assertEqual(out['orphans'], [])

    def test_unknown_task_id_yields_empty_missions_orphans(self) -> None:
        out = self._derive(task_id='does-not-exist')
        self.assertEqual(out['missions'], [])
        self.assertEqual(out['orphans'], [])


# ---------- endpoint integration (auth, route, shape) ----------


class EndpointTest(_DerivedTestBase):
    def test_requires_token(self) -> None:
        self.assertEqual(self.client.get(ENDPOINT).status_code, 401)

    def test_returns_200_with_expected_top_level_keys(self) -> None:
        resp = self.client.get(ENDPOINT, headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(
            set(body),
            {'schema_version', 'missions', 'orphans', 'parked',
             'last_synced_at', 'as_of'},
        )
        self.assertEqual(body['schema_version'], 1)

    def test_route_passes_query_filters_through(self) -> None:
        resp = self.client.get(
            ENDPOINT, headers=AUTH, params={'repo': 'ourliberty-dashboard'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            all(m['repo'] == 'ourliberty-dashboard'
                for m in resp.json()['missions']),
        )

    def test_mission_task_pr_state_always_none(self) -> None:
        # MISSION-task pr_state stays None (parity-pinned — matches the dashboard
        # route). The § 3.4 live PR-state read added for orphan terminal
        # detection does NOT populate mission-task pr_state.
        body = self._derive()
        states = [
            t['pr_state'] for m in body['missions'] for t in m['tasks']
        ]
        self.assertTrue(all(s is None for s in states))


# ---------- degradation (no creds / query error) ----------


class DegradationTest(_DerivedTestBase):
    def test_supabase_error_degrades_to_empty_events(self) -> None:
        # A failing client must not 500 the read-only derive: missions still
        # render (all `ready`, no events), orphans empty, parked intact.
        out = da._handle_missions_derived(
            missions_path=self._missions_path,
            captures_path=self._captures_path,
            supabase_client=_BoomClient(),
            repo=None,
            task_id=None,
            now=NOW,
        )
        self.assertEqual(out['orphans'], [])
        self.assertEqual(len(out['parked']), 2)
        for m in out['missions']:
            for t in m['tasks']:
                self.assertEqual(t['derived_phase'], 'ready')

    def test_none_client_degrades_to_empty_events(self) -> None:
        out = da._handle_missions_derived(
            missions_path=self._missions_path,
            captures_path=self._captures_path,
            supabase_client=None,
            repo=None,
            task_id=None,
            now=NOW,
        )
        self.assertEqual(out['orphans'], [])
        self.assertEqual(len(out['parked']), 2)


# ---------- § 3.4 orphan terminal detection via live PR-state ----------


class OrphanPrStateTest(_DerivedTestBase):
    """The real-world bug: a merged/closed orphan with NO `auto_merge` event
    under its task_id (the common case for sequence steps) stayed `building`
    forever, so the lane never collapsed. The injected resolver maps the
    orphan's pr_url → live GitHub state; no network is touched."""

    def _derive_with_states(self, states: dict) -> dict:
        return da._handle_missions_derived(
            missions_path=self._missions_path,
            captures_path=self._captures_path,
            supabase_client=self._stub,
            repo=None,
            task_id=None,
            now=NOW,
            pr_state_resolver=lambda _urls: states,
        )

    def test_merged_pr_without_automerge_event_becomes_terminal(self) -> None:
        # orphan-inreview-now (#505) has a review_pass marker + a pr_url but NO
        # auto_merge event → event-only derive says in-review (terminal=False).
        # When GitHub reports the PR MERGED it must flip to shipped/terminal.
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/505'
        o = {x['task_id']: x for x in
             self._derive_with_states({url: 'MERGED'})['orphans']}
        self.assertEqual(o['orphan-inreview-now']['state_badge'], 'shipped')
        self.assertTrue(o['orphan-inreview-now']['terminal'])
        self.assertFalse(o['orphan-inreview-now']['stalled'])

    def test_closed_pr_is_terminal_with_closed_badge(self) -> None:
        # orphan-stalled-old (#410) closed-unmerged → terminal, distinct badge.
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/410'
        o = {x['task_id']: x for x in
             self._derive_with_states({url: 'CLOSED'})['orphans']}
        self.assertEqual(o['orphan-stalled-old']['state_badge'], 'closed')
        self.assertTrue(o['orphan-stalled-old']['terminal'])

    def test_open_pr_never_terminal(self) -> None:
        # GitHub reports OPEN → orphan stays visible (in-review here), never hidden.
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/505'
        o = {x['task_id']: x for x in
             self._derive_with_states({url: 'OPEN'})['orphans']}
        self.assertFalse(o['orphan-inreview-now']['terminal'])
        self.assertEqual(o['orphan-inreview-now']['state_badge'], 'in-review')

    def test_empty_resolver_falls_back_to_event_only(self) -> None:
        # The fail-safe path: no PR state resolved → pre-fix event-only derive.
        o = {x['task_id']: x for x in self._derive_with_states({})['orphans']}
        self.assertEqual(o['orphan-inreview-now']['state_badge'], 'in-review')
        self.assertFalse(o['orphan-inreview-now']['terminal'])
        # And the shipped-via-auto_merge orphan is still terminal (event path).
        self.assertTrue(o['orphan-shipped-pr']['terminal'])

    def test_invariant_open_orphan_never_terminal_regardless_of_age(self) -> None:
        # orphan-stalled-old is 30d+ quiet; OPEN must keep it stalled/visible,
        # never terminal — no unmerged, un-closed orphan is ever hidden.
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/410'
        o = {x['task_id']: x for x in
             self._derive_with_states({url: 'OPEN'})['orphans']}
        self.assertFalse(o['orphan-stalled-old']['terminal'])
        self.assertTrue(o['orphan-stalled-old']['stalled'])

    def test_raising_resolver_degrades_not_500s(self) -> None:
        # Backstop: even if the resolver raises (it shouldn't), the derive must
        # degrade to event-only, never propagate a 500. orphan-shipped-pr stays
        # terminal via its auto_merge event; the merged-but-eventless orphan
        # falls back to in-review (visible).
        def _boom(_urls):
            raise RuntimeError('resolver blew up')
        out = da._handle_missions_derived(
            missions_path=self._missions_path,
            captures_path=self._captures_path,
            supabase_client=self._stub,
            repo=None, task_id=None, now=NOW,
            pr_state_resolver=_boom,
        )
        o = {x['task_id']: x for x in out['orphans']}
        self.assertTrue(o['orphan-shipped-pr']['terminal'])
        self.assertEqual(o['orphan-inreview-now']['state_badge'], 'in-review')


class ResolvePrStatesTest(unittest.TestCase):
    """Unit tests for the live resolver itself — batched, bounded, fail-safe.
    `_github_api_request` is the module's monkeypatchable test seam (no network)."""

    def setUp(self) -> None:
        self._orig_req = da._github_api_request
        self._orig_tok = da._github_token
        da._github_token = lambda: 'tok'
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        da._github_api_request = self._orig_req
        da._github_token = self._orig_tok

    @staticmethod
    def _resp(body: dict, code: int = 200):
        class _R:
            status_code = code

            def json(self_inner):  # noqa: N805
                return body
        return _R()

    def test_no_token_returns_empty(self) -> None:
        da._github_token = lambda: None
        self.assertEqual(
            da._resolve_orphan_pr_states(
                ['https://github.com/o/r/pull/1'],
            ), {},
        )

    def test_empty_urls_returns_empty(self) -> None:
        self.assertEqual(da._resolve_orphan_pr_states([]), {})

    def test_maps_states_one_batched_call_per_repo(self) -> None:
        calls = []

        def fake(method, url, *, headers=None, json_body=None, timeout=10.0):
            calls.append(json_body['query'])
            nums = re.findall(r'pullRequest\(number: (\d+)\)', json_body['query'])
            return self._resp(
                {'data': {'repository': {
                    f'p{n}': {'state': 'MERGED'} for n in nums
                }}},
            )

        da._github_api_request = fake
        urls = [
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1',
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/2',
            'https://github.com/Larry-Yatch/ourliberty-dashboard/pull/9',
        ]
        out = da._resolve_orphan_pr_states(urls)
        self.assertEqual(out[urls[0]], 'MERGED')
        self.assertEqual(out[urls[2]], 'MERGED')
        # agent-core (#1,#2) + dashboard (#9) = two repos = two batched calls.
        self.assertEqual(len(calls), 2)

    def test_request_exception_is_fail_safe(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError('network down')
        da._github_api_request = boom
        self.assertEqual(
            da._resolve_orphan_pr_states(
                ['https://github.com/o/r/pull/1'],
            ), {},
        )

    def test_non_200_is_skipped(self) -> None:
        da._github_api_request = (
            lambda *a, **k: self._resp({}, code=502)
        )
        self.assertEqual(
            da._resolve_orphan_pr_states(
                ['https://github.com/o/r/pull/1'],
            ), {},
        )

    def test_unparseable_url_is_ignored(self) -> None:
        da._github_api_request = lambda *a, **k: self._resp(
            {'data': {'repository': {}}},
        )
        self.assertEqual(
            da._resolve_orphan_pr_states(['not-a-pr-url']), {},
        )

    def test_null_data_error_shape_is_fail_safe(self) -> None:
        # GitHub returns HTTP 200 with {"data": null, "errors": [...]} on a
        # whole-query failure (bad token, rate-limit). The resolver must return
        # {} — NOT raise AttributeError on None.get(...) and 500 the route.
        da._github_api_request = lambda *a, **k: self._resp(
            {'data': None, 'errors': [{'type': 'NOT_FOUND'}]},
        )
        self.assertEqual(
            da._resolve_orphan_pr_states(
                ['https://github.com/o/r/pull/1'],
            ), {},
        )

    def test_repository_null_is_fail_safe(self) -> None:
        # {"data": {"repository": null}} (repo gone / no access) → {} cleanly.
        da._github_api_request = lambda *a, **k: self._resp(
            {'data': {'repository': None}},
        )
        self.assertEqual(
            da._resolve_orphan_pr_states(
                ['https://github.com/o/r/pull/1'],
            ), {},
        )

    def test_non_dict_body_is_fail_safe(self) -> None:
        # resp.json() returns a non-dict (e.g. a list) → {} cleanly.
        da._github_api_request = lambda *a, **k: self._resp([1, 2, 3])
        self.assertEqual(
            da._resolve_orphan_pr_states(
                ['https://github.com/o/r/pull/1'],
            ), {},
        )

    def test_partial_null_nodes_resolve_the_rest(self) -> None:
        # One PR node null (deleted), the other valid → the valid one maps,
        # the null one is skipped (no crash).
        da._github_api_request = lambda *a, **k: self._resp(
            {'data': {'repository': {'p1': None, 'p2': {'state': 'MERGED'}}}},
        )
        urls = [
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/1',
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/2',
        ]
        out = da._resolve_orphan_pr_states(urls)
        self.assertNotIn(urls[0], out)
        self.assertEqual(out[urls[1]], 'MERGED')


class DeriveOrphanReadabilityUnitTest(unittest.TestCase):
    """Direct tests of the pure badge/terminal mapping for each pr_state."""

    _NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    _RECENT = '2026-06-10T09:00:00+00:00'
    _OLD = '2026-04-01T00:00:00+00:00'  # 30d+ quiet

    def _derive(self, pr_state, last_ts=_RECENT, events=None):
        return da._derive_orphan_readability(
            {'task_id': 'x', 'last_event_ts': last_ts},
            events or [],
            self._NOW,
            pr_state=pr_state,
        )

    def test_merged_is_shipped_terminal(self) -> None:
        out = self._derive('MERGED')
        self.assertEqual(out['state_badge'], 'shipped')
        self.assertTrue(out['terminal'])

    def test_closed_is_terminal(self) -> None:
        out = self._derive('CLOSED')
        self.assertEqual(out['state_badge'], 'closed')
        self.assertTrue(out['terminal'])

    def test_open_recent_is_building_not_terminal(self) -> None:
        out = self._derive('OPEN')
        self.assertEqual(out['state_badge'], 'building')
        self.assertFalse(out['terminal'])

    def test_open_stale_is_stalled_visible(self) -> None:
        out = self._derive('OPEN', last_ts=self._OLD)
        self.assertEqual(out['state_badge'], 'stalled')
        self.assertFalse(out['terminal'])
        self.assertTrue(out['stalled'])

    def test_none_falls_back_to_event_only(self) -> None:
        # No PR state, no auto_merge event → building (pre-fix behavior).
        out = self._derive(None)
        self.assertEqual(out['state_badge'], 'building')
        self.assertFalse(out['terminal'])

    def test_none_with_automerge_event_is_shipped(self) -> None:
        out = self._derive(None, events=[{'event_type': 'auto_merge'}])
        self.assertEqual(out['state_badge'], 'shipped')
        self.assertTrue(out['terminal'])


if __name__ == '__main__':
    unittest.main()
