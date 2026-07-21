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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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

# Additive "Talk to the team" collapsed-card badge keys, projected onto every
# funnel lane (parked/suggested/orphans). Stripped before the frozen-fixture
# parity comparisons; their presence + values are covered directly in
# test_dashboard_api_team_reply_fields.
_TEAM_REPLY_KEYS = ('latest_team_message_id', 'blocked_on_you')


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
            {k: v for k, v in o.items()
             if k not in _PHASE2_ORPHAN_KEYS and k not in _TEAM_REPLY_KEYS}
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
        stripped = [
            {k: v for k, v in p.items() if k not in _TEAM_REPLY_KEYS}
            for p in self._derive()['parked']
        ]
        self.assertEqual(stripped, expected)

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

    def test_non_buildable_orphan_excluded_from_surfaced_orphans(self) -> None:
        # § 4.8: the surfaced orphans[] is narrowed to buildable initiatives by
        # is_proposable_initiative. desktop-ab12cd34 survives detect_orphans (it is
        # not infrastructure) but is a pure desktop-capture hash → not a buildable
        # mission → it must NOT appear in orphans[], while genuine buildable orphans
        # stay. This view-level gate does not change detect_orphans (autoregister).
        ids = {o['task_id'] for o in self._derive()['orphans']}
        self.assertNotIn('desktop-ab12cd34', ids)
        self.assertTrue(
            da.is_proposable_initiative('orphan-stalled-old', 'forge'))
        self.assertFalse(
            da.is_proposable_initiative('desktop-ab12cd34', 'desktop-claude'))
        for buildable in (
            'orphan-stalled-old', 'orphan-building-now', 'orphan-inreview-now',
        ):
            self.assertIn(buildable, ids)

    def test_non_buildable_orphan_excluded_from_funnel_secondary(self) -> None:
        # The same § 4.8 gate flows through to funnel.secondary (built off the
        # filtered orphans[]): the noise id is gone, a buildable one stays.
        secondary_refs = {
            i['ref'] for i in self._derive()['funnel']['secondary']
        }
        self.assertNotIn('desktop-ab12cd34', secondary_refs)
        self.assertIn('orphan-stalled-old', secondary_refs)


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
            {'schema_version', 'missions', 'orphans', 'parked', 'funnel',
             'pipeline', 'last_synced_at', 'as_of'},
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


class NoPrThreadConcludedTest(unittest.TestCase):
    """A no-PR-by-design thread (triage question / card reply / review hold) is
    terminal on its OWN conclusion evidence, since the PR-shaped derive can never
    conclude it. Live 2026-07-21: all 22 orphans badged `building` had in fact
    finished, several 9 days earlier.

    The standing invariant is UNCHANGED and re-pinned below: any orphan carrying
    a PR (or an unresolved PR state) can never reach terminal through this path,
    at any age. The two rules are disjoint."""

    _NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
    _RECENT = '2026-07-21T09:00:00+00:00'
    _OLD = '2026-06-01T00:00:00+00:00'  # 30d+ quiet

    # inbox_watcher writes success as the STRING 'True' (interpolated from a
    # shell log line); hand-built envelopes use a real bool. Both must count.
    _DONE_STR = {'event_type': 'session_done', 'payload': {'success': 'True'}}
    _DONE_BOOL = {'event_type': 'session_done', 'payload': {'success': True}}
    _APPROVE = {'event_type': 'larry_action', 'payload': {'action': 'approve'}}

    def _derive(self, task_id, events, pr_state=None, pr_url=None,
                last_ts=_RECENT):
        orphan = {'task_id': task_id, 'last_event_ts': last_ts}
        if pr_url:
            orphan['pr_url'] = pr_url
        return da._derive_orphan_readability(
            orphan, events, self._NOW, pr_state=pr_state)

    # ---- the three concluding classes ----

    _REJECT = {'event_type': 'larry_action', 'payload': {'action': 'reject'}}

    def test_each_class_concludes_on_its_own_evidence(self) -> None:
        # A hold never emits session_done at all — Larry's decision IS its end.
        cases = (
            ('direction-ask-entrypoint-blind-heal-001', self._DONE_STR),
            ('direction-ask-x-001', self._DONE_BOOL),
            ('card-message-03c9ecb0', self._DONE_STR),
            ('deep-review-hold-pr966-c5073db8', self._APPROVE),
            ('deep-review-hold-pr1-abc', self._REJECT),
        )
        for task_id, ev in cases:
            with self.subTest(task_id=task_id, event=ev['event_type']):
                out = self._derive(task_id, [ev])
                self.assertEqual(out['state_badge'], 'concluded')
                self.assertTrue(out['terminal'])
                self.assertFalse(out['stalled'])

    # ---- gate 1: only these classes ----

    def test_delegate_envelope_never_concludes(self) -> None:
        # THE critical exclusion. A delegate envelope's session ends when Beacon
        # finishes SCOPING; the build then runs under a fresh id via the ledger
        # bridge. Proven live 2026-07-21: this exact task concluded at 06:08 and
        # its build opened PR #971 at 06:45. Concluding it would hide live work.
        out = self._derive(
            'delegate-cap-route-ourliberty-graph-prs-to-mirror-review-1123',
            [self._DONE_STR])
        self.assertEqual(out['state_badge'], 'building')
        self.assertFalse(out['terminal'])

    def test_unprefixed_task_never_concludes(self) -> None:
        out = self._derive('rebase-pr-860-001-retry1', [self._DONE_STR])
        self.assertFalse(out['terminal'])

    # ---- gate 2: any PR at all defers to the PR-shaped derive ----

    def test_open_pr_on_a_concluding_class_is_never_terminal(self) -> None:
        # The standing invariant, re-pinned through the new code path.
        out = self._derive(
            'direction-ask-x-001', [self._DONE_STR],
            pr_state='OPEN',
            pr_url='https://github.com/o/r/pull/1')
        self.assertFalse(out['terminal'])
        self.assertEqual(out['state_badge'], 'building')

    def test_open_pr_stale_on_a_concluding_class_stays_stalled(self) -> None:
        # Age must never be what concludes a thread: 30d+ quiet AND a successful
        # session_done, but an OPEN PR keeps it visible and stalled.
        out = self._derive(
            'direction-ask-x-001', [self._DONE_STR],
            pr_state='OPEN', pr_url='https://github.com/o/r/pull/1',
            last_ts=self._OLD)
        self.assertFalse(out['terminal'])
        self.assertTrue(out['stalled'])

    def test_pr_url_on_an_event_defers_even_without_resolved_state(self) -> None:
        # No resolver result (token missing / network down) but an event carries
        # a pr_url → the PR-shaped rules own it; fail safe, stay visible.
        out = self._derive('direction-ask-x-001', [
            dict(self._DONE_STR, pr_url='https://github.com/o/r/pull/2'),
        ])
        self.assertFalse(out['terminal'])

    # ---- gate 3: a real conclusion, nothing weaker ----

    def test_started_but_not_done_is_not_concluded(self) -> None:
        out = self._derive(
            'direction-ask-x-001', [{'event_type': 'session_start'}])
        self.assertFalse(out['terminal'])
        self.assertEqual(out['state_badge'], 'building')

    def test_failed_session_is_not_concluded(self) -> None:
        for bad in ('False', 'false', False, None, '', 'yes'):
            with self.subTest(success=bad):
                out = self._derive(
                    'direction-ask-x-001',
                    [{'event_type': 'session_done', 'payload': {'success': bad}}])
                self.assertFalse(
                    out['terminal'],
                    f'success={bad!r} must not conclude the thread')

    def test_malformed_payload_is_not_concluded(self) -> None:
        for payload in (None, 'not-a-dict', [], 42):
            with self.subTest(payload=payload):
                out = self._derive(
                    'direction-ask-x-001',
                    [{'event_type': 'session_done', 'payload': payload}])
                self.assertFalse(out['terminal'])

    def test_no_events_is_not_concluded(self) -> None:
        self.assertFalse(self._derive('direction-ask-x-001', [])['terminal'])

    def test_merged_still_outranks_concluded(self) -> None:
        # `shipped` is checked first: a real merge always wins the badge.
        out = self._derive(
            'direction-ask-x-001',
            [self._DONE_STR, {'event_type': 'auto_merge'}])
        self.assertEqual(out['state_badge'], 'shipped')
        self.assertTrue(out['terminal'])


class DismissedProjectSuppressionTest(unittest.TestCase):
    """A project promoted from a mission that has since been DISMISSED must stop
    drawing a pipeline card. `dismiss` only edits missions.json, so the promoted
    project stays `active` forever — live 2026-07-21, 2 of 4 pipeline cards were
    dismissed by MERGED PRs (#931, #957) 8-12 days earlier and still rendered."""

    def _proj(self, pid, mission_id):
        return {'id': pid, 'state': 'active',
                'promoted_from': {'kind': 'mission', 'mission_id': mission_id}}

    def test_dismissed_ids_reuses_the_shared_dead_test(self) -> None:
        entries = [
            {'id': 'ack', 'phase': 'proposed', 'acknowledged': True},
            {'id': 'retired', 'phase': 'proposed', 'retired_at': '2026-07-01'},
            {'id': 'closed-phase', 'phase': 'closed'},
            {'id': 'live', 'phase': 'proposed'},
            {'id': 'drafting', 'phase': 'drafting'},
        ]
        out = da._dismissed_mission_ids(entries)
        self.assertEqual(out, {'ack', 'retired', 'closed-phase'})

    def test_dismissed_ids_tolerates_junk_entries(self) -> None:
        self.assertEqual(
            da._dismissed_mission_ids(['x', None, {}, {'id': 42}]), set())

    def test_project_from_dismissed_mission_is_collected(self) -> None:
        self.assertEqual(
            da._dismissed_source_project_ids([self._proj('p1', 'm1')], {'m1'}),
            {'p1'})

    def test_project_from_live_mission_is_not_collected(self) -> None:
        self.assertEqual(
            da._dismissed_source_project_ids(
                [self._proj('p1', 'm1')], {'other'}),
            set())

    def test_only_mission_provenance_counts(self) -> None:
        # kind != 'mission' — a dismissed MISSION id can't speak for a project
        # promoted from a capture or a raw orphan, even on an id collision.
        projects = [
            {'id': 'p-orphan', 'state': 'active',
             'promoted_from': {'kind': 'orphan', 'task_id': 'm1'}},
            {'id': 'p-capture', 'state': 'active',
             'promoted_from': {'kind': 'capture', 'capture_id': 'm1'}},
        ]
        self.assertEqual(
            da._dismissed_source_project_ids(projects, {'m1'}), set())

    def test_junk_never_collects_a_project(self) -> None:
        # Positive evidence only — anything malformed is simply absent from the
        # set, so its card is kept.
        for proj in (
            'not-a-dict', None, {}, {'id': ''}, {'id': 7},
            {'id': 'p1'},
            {'id': 'p1', 'promoted_from': None},
            {'id': 'p1', 'promoted_from': 'junk'},
            {'id': 'p1', 'promoted_from': {}},
            {'id': 'p1', 'promoted_from': {'kind': 'mission'}},
            {'id': 'p1', 'promoted_from': {'kind': 'mission',
                                           'mission_id': None}},
        ):
            with self.subTest(project=proj):
                self.assertEqual(
                    da._dismissed_source_project_ids([proj], {'m1'}), set())


class GithubTokenTest(unittest.TestCase):
    """`_github_token` env-first, then `gh auth token` fallback (the dashboard-api
    host has gh authed but no token env var)."""

    def setUp(self) -> None:
        self._orig_env = {
            k: os.environ.get(k) for k in ('GITHUB_TOKEN', 'GH_TOKEN')
        }
        for k in ('GITHUB_TOKEN', 'GH_TOKEN'):
            os.environ.pop(k, None)
        self._orig_run = da.subprocess.run
        self._orig_cache = da._GH_CLI_TOKEN_CACHE
        da._GH_CLI_TOKEN_CACHE = da._UNSET
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        da.subprocess.run = self._orig_run
        da._GH_CLI_TOKEN_CACHE = self._orig_cache

    @staticmethod
    def _proc(stdout: str, returncode: int = 0):
        class _P:
            pass
        p = _P()
        p.stdout = stdout
        p.returncode = returncode
        return p

    def test_env_token_wins_and_skips_gh(self) -> None:
        os.environ['GITHUB_TOKEN'] = 'env-tok'

        def _boom(*_a, **_k):
            raise AssertionError('gh should not be called when env token exists')
        da.subprocess.run = _boom
        self.assertEqual(da._github_token(), 'env-tok')

    def test_gh_fallback_when_env_absent(self) -> None:
        calls = []

        def fake(cmd, **_k):
            calls.append(cmd)
            return self._proc('gh-cli-tok\n')
        da.subprocess.run = fake
        self.assertEqual(da._github_token(), 'gh-cli-tok')
        self.assertEqual(calls[0], ['gh', 'auth', 'token'])

    def test_gh_token_cached_no_second_subprocess(self) -> None:
        calls = []

        def fake(cmd, **_k):
            calls.append(cmd)
            return self._proc('gh-cli-tok')
        da.subprocess.run = fake
        self.assertEqual(da._github_token(), 'gh-cli-tok')
        self.assertEqual(da._github_token(), 'gh-cli-tok')
        self.assertEqual(len(calls), 1)  # cached after first success

    def test_gh_nonzero_returns_none(self) -> None:
        da.subprocess.run = lambda *a, **k: self._proc('', returncode=1)
        self.assertIsNone(da._github_token())

    def test_gh_zero_but_empty_stdout_returns_none_not_cached(self) -> None:
        # returncode 0 with empty/whitespace stdout must NOT poison the cache.
        calls = []

        def fake(*_a, **_k):
            calls.append(1)
            return self._proc('  \n', returncode=0)
        da.subprocess.run = fake
        self.assertIsNone(da._github_token())
        self.assertIs(da._GH_CLI_TOKEN_CACHE, da._UNSET)  # not cached
        self.assertIsNone(da._github_token())
        self.assertEqual(len(calls), 2)  # retried, not cached

    def test_gh_missing_returns_none_and_not_cached(self) -> None:
        calls = []

        def fake(*_a, **_k):
            calls.append(1)
            raise FileNotFoundError('gh not installed')
        da.subprocess.run = fake
        self.assertIsNone(da._github_token())
        # A failure is NOT cached — a later call retries (transient hiccup).
        self.assertIsNone(da._github_token())
        self.assertEqual(len(calls), 2)


class IsProposableInitiativeTest(unittest.TestCase):
    """`is_proposable_initiative` — the stricter proposed-lane gate (signal
    hardening). It must sweep every noise category that flooded the proposed
    lane while keeping genuine buildable initiatives proposable, and it must NOT
    collide with buildable ids that merely contain a noise substring."""

    def test_genuine_buildable_initiatives_are_proposable(self) -> None:
        for tid in (
            'p2-derive-endpoint',
            'p2-digest-generator',                       # 'digest' is NOT swept
            'rebase-pr252-digest-generator-001',
            'harden-test-prod-write-isolation-001',      # 'prod' mid-id, not prefix
            'log-dir-test-isolation-leak-001',           # 'test-isolation' mid-id
            'fix-pr-s2-test-isolation-leak-plus-ast-walk-regression-gate',
            'wire-pulse-check-iv-cadence-001',           # 'check-i' mid-id, not prefix
            'pulse-check-iii-tune-forge-stuck-threshold',
            'missions-v2-phase0-desktop-feed',           # 'desktop' mid-id, not a hash
            'auth-setup-token-wiring',
        ):
            self.assertTrue(da.is_proposable_initiative(tid), tid)

    def test_chain_incident_and_alert_artifacts_swept(self) -> None:
        for tid in (
            'pipeline-stall:clarify1-suppress-needed',
            'sequence-invalid:approvals-queue-rework',
            'failure:commit-failed',
            'unreviewed-merge:489',
            'wedged-worktree:wt-mirror-x',
            'no-session-revision:pr412-forge-redispatch-needed',
            'transcript-not-persisted:tier1',
            'approval-request:parked:alert-triage-durable-watermark-001',
            'approval_request:routing-failure:preflight-reminder-enforcement-001',
            'install-drift-timer:ourliberty-heal-missions-card-gc',
            'unreg-approval-8ba8e3b472a8',               # hyphen variant
        ):
            self.assertFalse(da.is_proposable_initiative(tid), tid)

    def test_desktop_capture_hashes_swept(self) -> None:
        for tid in ('desktop-05a159bb', 'desktop-e2e0ab79', 'desktop-bc57cca6'):
            self.assertFalse(da.is_proposable_initiative(tid), tid)

    def test_sequence_step_proposals_swept(self) -> None:
        for tid in (
            'seq-approvals-queue-rework-step-step-autoclear',
            'seq-missions-v2-phase2-step-p2-derive-endpoint',
            'step-alert-promotion',
            'step-digest-generator',
        ):
            self.assertFalse(da.is_proposable_initiative(tid), tid)

    def test_translation_rule_digest_artifacts_swept(self) -> None:
        for tid in (
            'alert-translation-dispatch-branch-cleanup-summary-001',
            'missions-autoregister-alert-translation-001',
            'g-rule-dispatch-branch-cleanup-alert-translation-20260612T163000Z',
            'sync-push-failure-g-rule-117',
            'dispatch-translations-entry-20260610',
            'ceo-digest-daily-2026-06-11',
            'weekly-2026-06-08',
            'check-i-2026-06-08',
        ):
            self.assertFalse(da.is_proposable_initiative(tid), tid)

    def test_test_fixture_shaped_ids_swept(self) -> None:
        for tid in (
            'real-clr', 'real-pf-ok', 'real-cascade',
            'prod-clr', 'test-isolation-v2', 'test-isolation-v3',
        ):
            self.assertFalse(da.is_proposable_initiative(tid), tid)

    def test_degenerate_and_empty_ids_swept(self) -> None:
        for tid in ('20', 'summary', '', '   '):
            self.assertFalse(da.is_proposable_initiative(tid), tid)

    def test_preflight_approval_redispatch_ledger_artifacts_swept(self) -> None:
        # The noise classes that still leaked onto the proposed lane and never
        # drained (P1 follow-up): DAG-preflight runs, approval-thread hashes,
        # redispatch artifacts, and ops-cleanup ledger snapshots.
        for tid in (
            'dag-preflight-park-the-nudge',
            'dag-preflight-missions-v2-phase2',
            'dag-preflight-projects-v3-p3-followup2-v2',
            'larry-approval-8aa50bfa8aec52113f9405772937021ca0fa7689',
            'larry-approval-45a1464c4cea2a71e1291bd5b6e96830c5175f12',
            'p3-dashboard-proposed-lane-redispatch-20260612T132800Z',
            'ops-cleanup2-s1-dedup-ledger-20260611',
            'ops-cleanup-ccds1-ledger-20260610',
        ):
            self.assertFalse(da.is_proposable_initiative(tid), tid)

    def test_new_rules_do_not_sweep_genuine_buildables(self) -> None:
        # The anchored rules must NOT collide with buildables that merely look
        # similar — a partial approval hash, a non-dated ledger initiative, a
        # 'preflight' mid-id, or the pinned rebase/pulse ids.
        for tid in (
            'larry-approval-flow-redesign',              # not a 40-hex hash
            'redispatch-guard-hardening-001',            # 'redispatch' but no -<ts> tail
            'add-preflight-clean-exit-detection',        # 'preflight' mid-id, not prefix
            'ops-cleanup-healer-design',                 # 'ops-cleanup' but no -ledger-<date>
            'rebase-pr252-digest-generator-001',
            'pulse-check-iii-tune-forge-stuck-threshold',
            'capture-label-contract',
        ):
            self.assertTrue(da.is_proposable_initiative(tid), tid)

    def test_delegates_to_infrastructure_classification(self) -> None:
        # An id/agent that is_infrastructure_task already rejects is never proposable.
        self.assertFalse(da.is_proposable_initiative('notify-routing-xyz'))
        self.assertFalse(da.is_proposable_initiative('anything', agent='deploy-notifier'))


class OrphanLabelTitleFallbackTest(unittest.TestCase):
    """A prompt-blob desktop title must not pre-empt repo/branch in the Orphans
    lane label; short meaningful titles still win; the resolver stays graceful
    when fields are absent."""

    _BLOB = (
        'You are characterizing a software component into a v2 Component '
        'Descriptor for a...'
    )

    @staticmethod
    def _desktop(title=None, repo=None, branch=None):
        payload = {}
        if title is not None:
            payload['title'] = title
        if repo is not None:
            payload['repo'] = repo
        if branch is not None:
            payload['branch'] = branch
        return [{'event_type': 'desktop_session_active', 'payload': payload}]

    def test_is_prompt_blob_long_title(self) -> None:
        self.assertTrue(da._is_prompt_blob_title('x' * 61))
        self.assertFalse(da._is_prompt_blob_title('x' * 60))

    def test_is_prompt_blob_ellipsis(self) -> None:
        self.assertTrue(da._is_prompt_blob_title('Short but truncated...'))
        self.assertTrue(da._is_prompt_blob_title('Short but truncated…'))

    def test_short_meaningful_title_is_not_blob(self) -> None:
        self.assertFalse(da._is_prompt_blob_title('Scene graph interface'))

    def test_blob_title_does_not_preempt_repo_branch(self) -> None:
        label, repo, branch = da._orphan_label_and_location(
            self._desktop(title=self._BLOB, repo='ourliberty-graph',
                          branch='feat/scene'),
            'desktop-blobrepo01',
        )
        self.assertEqual(label, 'ourliberty-graph/feat/scene')
        self.assertEqual(repo, 'ourliberty-graph')
        self.assertEqual(branch, 'feat/scene')

    def test_blob_title_prefers_repo_when_no_branch(self) -> None:
        label, _repo, _branch = da._orphan_label_and_location(
            self._desktop(title=self._BLOB, repo='ourliberty-graph'),
            'desktop-blobreponobranch',
        )
        self.assertEqual(label, 'ourliberty-graph')

    def test_short_meaningful_title_still_wins_over_repo(self) -> None:
        label, _repo, _branch = da._orphan_label_and_location(
            self._desktop(title='Scene graph interface', repo='ourliberty-graph',
                          branch='main'),
            'desktop-ab12cd34',
        )
        self.assertEqual(label, 'Scene graph interface')

    def test_blob_title_used_when_no_repo(self) -> None:
        label, repo, branch = da._orphan_label_and_location(
            self._desktop(title=self._BLOB),
            'desktop-blobonly01',
        )
        self.assertEqual(label, self._BLOB)
        self.assertIsNone(repo)
        self.assertIsNone(branch)

    def test_no_title_no_repo_falls_back_to_humanized_task_id(self) -> None:
        label, _repo, _branch = da._orphan_label_and_location(
            [], 'orphan-inreview-now',
        )
        self.assertEqual(label, 'Orphan Inreview Now')


# ---------- Phase S (S1/S2): spawned-ref + derived in-flight phase ----------


class SpawnedRefDeriveTest(unittest.TestCase):
    """The parked lane surfaces a delegated card's linked-work phase through the
    EXISTING chain_events derive (S2), keyed on the `spawned` ref's task_id (S1
    join key). No new state machine — `derive_phase_for_task` is reused verbatim,
    so the four event shapes map to the same vocabulary the missions/orphan lanes
    use (in_flight / awaiting_merge / shipped); a card whose work hasn't emitted
    an event yet stays neutral (spawned_phase=None)."""

    NOW = datetime(2026, 6, 16, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self._orig_env_token = os.environ.get('DASHBOARD_API_TOKEN')
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.addCleanup(
            lambda: os.environ.__setitem__('DASHBOARD_API_TOKEN', self._orig_env_token)
            if self._orig_env_token is not None
            else os.environ.pop('DASHBOARD_API_TOKEN', None),
        )
        self._tmp = Path(__import__('tempfile').mkdtemp(prefix='spawned_ref_'))
        self.addCleanup(
            lambda: __import__('shutil').rmtree(self._tmp, ignore_errors=True),
        )
        self._missions_path = self._tmp / 'missions.json'
        self._captures_path = self._tmp / 'captures.json'
        # No missions — isolate the parked-lane spawned-ref behaviour.
        self._missions_path.write_text(
            json.dumps({'schema_version': 1, 'missions': [],
                        'last_synced_at': None}) + '\n')

    def _seed_captures(self, *caps: dict) -> None:
        self._captures_path.write_text(
            json.dumps({'schema_version': 2, 'captures': list(caps)}) + '\n')

    @staticmethod
    def _parked_cap(cid: str, *, spawned=None, repo='ourliberty-agent-core') -> dict:
        cap = {
            'id': cid,
            'title': f'card {cid}',
            'state': 'parked',
            'origin': {'repo': repo},
            'last_touched': '2026-06-10T00:00:00+00:00',
        }
        if spawned is not None:
            cap['spawned'] = spawned
        return cap

    def _derive(self, rows: list[dict]) -> dict:
        return da._handle_missions_derived(
            missions_path=self._missions_path,
            captures_path=self._captures_path,
            supabase_client=_StubClient(rows),
            repo=None,
            task_id=None,
            now=self.NOW,
        )

    def _parked_by_id(self, rows: list[dict]) -> dict:
        return {p['capture_id']: p for p in self._derive(rows)['parked']}

    def test_no_spawned_ref_is_neutral_none(self) -> None:
        self._seed_captures(self._parked_cap('cap-plain'))
        p = self._parked_by_id([])['cap-plain']
        self.assertIsNone(p['spawned'])
        self.assertIsNone(p['spawned_phase'])
        self.assertIsNone(p['spawned_pr_url'])

    def test_spawned_ref_no_events_yet_stays_neutral(self) -> None:
        # A freshly-delegated card whose work has emitted no event surfaces the
        # ref (for traceability) but a None phase — never a misleading 'ready'.
        ref = {'kind': 'delegate', 'task_id': 'delegate-cap-x',
               'stamped_at': '2026-06-15T00:00:00+00:00'}
        self._seed_captures(self._parked_cap('cap-x', spawned=ref))
        p = self._parked_by_id([])['cap-x']
        self.assertEqual(p['spawned'], ref)
        self.assertIsNone(p['spawned_phase'])
        self.assertIsNone(p['spawned_pr_url'])

    def test_building_phase_from_forge_session_start(self) -> None:
        ref = {'kind': 'delegate', 'task_id': 'delegate-cap-x'}
        self._seed_captures(self._parked_cap('cap-x', spawned=ref))
        rows = [{'event_type': 'session_start', 'task_id': 'delegate-cap-x',
                 'agent': 'forge', 'pr_url': None,
                 'ts': '2026-06-15T10:00:00+00:00', 'payload': {}}]
        p = self._parked_by_id(rows)['cap-x']
        self.assertEqual(p['spawned_phase'], 'in_flight')
        self.assertIsNone(p['spawned_pr_url'])

    def test_in_review_phase_and_pr_url_from_review_pass(self) -> None:
        ref = {'kind': 'delegate', 'task_id': 'delegate-cap-x'}
        self._seed_captures(self._parked_cap('cap-x', spawned=ref))
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/777'
        rows = [
            {'event_type': 'marker_emit', 'task_id': 'delegate-cap-x',
             'agent': 'mirror', 'pr_url': url, 'ts': '2026-06-15T12:00:00+00:00',
             'payload': {'marker_type': 'REVIEW_PASS'}},
            {'event_type': 'session_start', 'task_id': 'delegate-cap-x',
             'agent': 'forge', 'pr_url': None, 'ts': '2026-06-15T10:00:00+00:00',
             'payload': {}},
        ]
        p = self._parked_by_id(rows)['cap-x']
        self.assertEqual(p['spawned_phase'], 'awaiting_merge')
        self.assertEqual(p['spawned_pr_url'], url)

    def test_shipped_phase_from_auto_merge(self) -> None:
        ref = {'kind': 'delegate', 'task_id': 'delegate-cap-x'}
        self._seed_captures(self._parked_cap('cap-x', spawned=ref))
        url = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/777'
        rows = [{'event_type': 'auto_merge', 'task_id': 'delegate-cap-x',
                 'agent': 'forge', 'pr_url': url,
                 'ts': '2026-06-15T13:00:00+00:00', 'payload': {}}]
        p = self._parked_by_id(rows)['cap-x']
        self.assertEqual(p['spawned_phase'], 'shipped')
        self.assertEqual(p['spawned_pr_url'], url)

    def test_garbage_spawned_ref_degrades_to_neutral(self) -> None:
        self._seed_captures(self._parked_cap('cap-x', spawned='not-a-dict'))
        p = self._parked_by_id([])['cap-x']
        self.assertIsNone(p['spawned'])
        self.assertIsNone(p['spawned_phase'])

    def test_spawned_events_are_fetched_alongside_missions(self) -> None:
        # The handler must union the parked captures' spawned task_ids into the
        # events fetch — otherwise the .in_() stub would filter them out and the
        # phase would never surface.
        ref = {'kind': 'delegate', 'task_id': 'delegate-cap-x'}
        self._seed_captures(self._parked_cap('cap-x', spawned=ref))
        rows = [{'event_type': 'auto_merge', 'task_id': 'delegate-cap-x',
                 'agent': 'forge', 'pr_url': None,
                 'ts': '2026-06-15T13:00:00+00:00', 'payload': {}}]
        self.assertEqual(
            self._parked_by_id(rows)['cap-x']['spawned_phase'], 'shipped')

    # ---------- S6: the work's estimated cost on the card ----------

    def test_spawned_expected_cost_surfaces_on_the_card(self) -> None:
        ref = {'kind': 'delegate', 'task_id': 'delegate-cap-x',
               'expected_cost_usd': 2.5}
        self._seed_captures(self._parked_cap('cap-x', spawned=ref))
        p = self._parked_by_id([])['cap-x']
        self.assertEqual(p['spawned_expected_cost_usd'], 2.5)

    def test_missing_cost_is_none_not_zero(self) -> None:
        ref = {'kind': 'delegate', 'task_id': 'delegate-cap-x'}
        self._seed_captures(self._parked_cap('cap-x', spawned=ref))
        self.assertIsNone(
            self._parked_by_id([])['cap-x']['spawned_expected_cost_usd'])

    def test_non_numeric_or_bool_cost_degrades_to_none(self) -> None:
        for bad in ('lots', True, None, [1]):
            with self.subTest(cost=bad):
                ref = {'kind': 'delegate', 'task_id': 'delegate-cap-x',
                       'expected_cost_usd': bad}
                self._seed_captures(self._parked_cap('cap-x', spawned=ref))
                self.assertIsNone(
                    self._parked_by_id([])['cap-x']['spawned_expected_cost_usd'])

    def test_no_spawned_ref_carries_none_cost(self) -> None:
        self._seed_captures(self._parked_cap('cap-plain'))
        self.assertIsNone(
            self._parked_by_id([])['cap-plain']['spawned_expected_cost_usd'])


# ---------- P1 C4: additive funnel grouping ----------


class FunnelDeriveTest(_DerivedTestBase):
    """C4 — the additive primary/secondary funnel over the existing intake.
    Built off the same fixtures as the parity gate, so it also proves the funnel
    is purely additive (the byte-for-byte missions/orphans parity tests above
    still pass with the funnel key present)."""

    def test_funnel_key_is_additive_and_shaped(self) -> None:
        funnel = self._derive()['funnel']
        self.assertEqual(set(funnel), {'primary', 'secondary'})
        for lane in (funnel['primary'], funnel['secondary']):
            for item in lane:
                self.assertEqual(
                    set(item),
                    {'kind', 'ref', 'label', 'repo', 'suggested_source'},
                )

    def test_parked_captures_are_primary(self) -> None:
        primary = self._derive()['funnel']['primary']
        refs = {i['ref'] for i in primary}
        self.assertEqual(refs, {'cap-aging-one', 'cap-fresh-two'})
        # No agent provenance on these fixtures → plain parked, no source tag.
        for i in primary:
            self.assertEqual(i['kind'], 'parked')
            self.assertIsNone(i['suggested_source'])

    def test_live_orphans_are_secondary(self) -> None:
        secondary = self._derive()['funnel']['secondary']
        refs = {i['ref'] for i in secondary}
        # § 4.8: desktop-ab12cd34 (a pure desktop-capture hash) is non-buildable
        # noise → filtered from orphans[] upstream, so it never reaches secondary.
        self.assertEqual(refs, {
            'orphan-inreview-now', 'desktop-blobrepo01',
            'desktop-blobonly01', 'orphan-building-now', 'orphan-stalled-old',
        })
        self.assertTrue(all(i['kind'] == 'orphan' for i in secondary))

    def test_terminal_orphan_is_auto_filtered_out(self) -> None:
        # orphan-shipped-pr is terminal (shipped) → cleared from the funnel,
        # though it still rides in the existing orphans[] section (unchanged).
        out = self._derive()
        self.assertIn('orphan-shipped-pr',
                      {o['task_id'] for o in out['orphans']})
        self.assertNotIn('orphan-shipped-pr',
                         {i['ref'] for i in out['funnel']['secondary']})

    def test_funnel_tracks_repo_filter(self) -> None:
        out = self._derive(repo='ourliberty-agent-core')
        primary = {i['ref'] for i in out['funnel']['primary']}
        secondary = {i['ref'] for i in out['funnel']['secondary']}
        self.assertEqual(primary, {'cap-aging-one'})
        # Only non-terminal agent-core orphans remain.
        self.assertEqual(secondary, {'orphan-building-now', 'orphan-stalled-old'})


class BuildFunnelUnitTest(unittest.TestCase):
    """Direct tests of the pure `_build_funnel` classifier over synthetic intake
    — proves the mission-source rules that the fixtures (no `proposed` missions)
    don't exercise."""

    def test_team_suggested_mission_is_primary_with_source(self) -> None:
        missions = [{
            'id': 'm-pulse', 'name': 'Pulse idea', 'repo': 'r',
            'phase': 'proposed', 'proposed_by': 'pulse',
        }]
        out = da._build_funnel(missions, [], [])
        self.assertEqual(len(out['primary']), 1)
        item = out['primary'][0]
        self.assertEqual(item['kind'], 'suggested')
        self.assertEqual(item['suggested_source'], 'pulse')
        self.assertEqual(item['ref'], 'm-pulse')
        self.assertEqual(out['secondary'], [])

    def test_orphan_autoregistered_mission_is_secondary(self) -> None:
        missions = [{
            'id': 'm-orphan', 'name': 'Auto idea', 'repo': 'r',
            'phase': 'proposed', 'proposed_by': 'heal_orphan_autoregister',
        }]
        out = da._build_funnel(missions, [], [])
        self.assertEqual(out['primary'], [])
        self.assertEqual(len(out['secondary']), 1)
        self.assertEqual(out['secondary'][0]['kind'], 'orphan')
        self.assertIsNone(out['secondary'][0]['suggested_source'])

    def test_dead_proposed_mission_is_auto_filtered(self) -> None:
        for dead in (
            {'acknowledged': True},
            {'retired_at': '2026-06-16T00:00:00+00:00'},
            {'phase': 'archived'},
        ):
            m = {'id': 'm', 'name': 'n', 'repo': 'r',
                 'phase': 'proposed', 'proposed_by': 'heal_orphan_autoregister'}
            m.update(dead)
            out = da._build_funnel([m], [], [])
            self.assertEqual(out['secondary'], [], dead)

    def test_established_missions_excluded_from_funnel(self) -> None:
        missions = [
            {'id': 'a', 'name': 'A', 'repo': 'r', 'phase': 'in_flight'},
            {'id': 'b', 'name': 'B', 'repo': 'r', 'phase': 'drafting'},
            {'id': 'c', 'name': 'C', 'repo': 'r', 'phase': 'ready'},
            {'id': 'd', 'name': 'D', 'repo': 'r', 'phase': 'deferred'},
        ]
        out = da._build_funnel(missions, [], [])
        self.assertEqual(out['primary'], [])
        self.assertEqual(out['secondary'], [])

    def test_capture_label_tags_suggested_source(self) -> None:
        parked = [{'capture_id': 'cap-1', 'label': 'pulse-check-i',
                   'title': 't', 'repo': 'r'}]
        out = da._build_funnel([], [], parked)
        item = out['primary'][0]
        self.assertEqual(item['kind'], 'suggested')
        self.assertEqual(item['suggested_source'], 'pulse')


# ---------- projects-v3 P3: promote suppression + reversibility ----------


class PromotedMissionIdsUnitTest(unittest.TestCase):
    """`_promoted_mission_ids` reads ONLY active projects' mission provenance —
    archived projects drop out so the mission returns to the funnel (reversible)."""

    def test_active_mission_project_contributes_its_mission_id(self) -> None:
        projects = [{
            'id': 'p1', 'state': 'active',
            'promoted_from': {'kind': 'mission', 'mission_id': 'm-1'},
        }]
        self.assertEqual(da._promoted_mission_ids(projects), {'m-1'})

    def test_archived_project_is_excluded(self) -> None:
        projects = [{
            'id': 'p1', 'state': 'archived',
            'promoted_from': {'kind': 'mission', 'mission_id': 'm-1'},
        }]
        self.assertEqual(da._promoted_mission_ids(projects), set())

    def test_capture_provenance_and_junk_ignored(self) -> None:
        projects = [
            {'id': 'p1', 'state': 'active',
             'promoted_from': {'kind': 'capture', 'capture_id': 'cap-1'}},
            {'id': 'p2', 'state': 'active', 'promoted_from': None},
            {'id': 'p3', 'state': 'active'},
            'not-a-dict',
        ]
        self.assertEqual(da._promoted_mission_ids(projects), set())

    def test_state_defaults_to_active_when_missing(self) -> None:
        projects = [{
            'id': 'p1',
            'promoted_from': {'kind': 'mission', 'mission_id': 'm-1'},
        }]
        self.assertEqual(da._promoted_mission_ids(projects), {'m-1'})


class FunnelPromoteSuppressionUnitTest(unittest.TestCase):
    """`_build_funnel` drops a proposed mission whose id was promoted into an
    active project, and never touches the mission record itself."""

    def _proposed(self, mid: str, by: str = 'pulse') -> dict:
        return {'id': mid, 'name': mid.title(), 'repo': 'r',
                'phase': 'proposed', 'proposed_by': by}

    def test_promoted_proposed_mission_leaves_primary(self) -> None:
        m = self._proposed('m-pulse')
        out = da._build_funnel([m], [], [], None, {'m-pulse'})
        self.assertEqual(out['primary'], [])
        self.assertEqual(out['secondary'], [])

    def test_promoted_orphan_derived_mission_leaves_secondary(self) -> None:
        m = self._proposed('m-orphan', by='heal_orphan_autoregister')
        out = da._build_funnel([m], [], [], None, {'m-orphan'})
        self.assertEqual(out['secondary'], [])

    def test_unpromoted_sibling_still_surfaces(self) -> None:
        kept = self._proposed('m-keep')
        gone = self._proposed('m-gone')
        out = da._build_funnel([kept, gone], [], [], None, {'m-gone'})
        refs = {i['ref'] for i in out['primary']}
        self.assertEqual(refs, {'m-keep'})

    def test_empty_promoted_set_suppresses_nothing(self) -> None:
        m = self._proposed('m-pulse')
        out = da._build_funnel([m], [], [], None, set())
        self.assertEqual({i['ref'] for i in out['primary']}, {'m-pulse'})

    def test_input_mission_not_mutated(self) -> None:
        m = self._proposed('m-pulse')
        snapshot = dict(m)
        da._build_funnel([m], [], [], None, {'m-pulse'})
        self.assertEqual(m, snapshot)


class FunnelPromoteReversibilityTest(_DerivedTestBase):
    """End-to-end through `_handle_missions_derived`: a proposed mission MOVED
    into an active project is suppressed from the funnel; archiving the project
    returns it (reversible, no data loss); the mission file is never rewritten."""

    def _seed(self) -> None:
        self._missions_path.write_text(json.dumps({
            'schema_version': 1,
            'missions': [{
                'id': 'm-pulse', 'name': 'Pulse Idea', 'repo': 'r',
                'phase': 'proposed', 'proposed_by': 'pulse', 'task_ids': [],
            }],
        }))
        # No captures/orphans to keep the funnel scoped to this one mission.
        self._captures_path.write_text(json.dumps(
            {'schema_version': 1, 'captures': []}))
        self._projects_path = self._tmp / 'projects.json'

    def _write_projects(self, state: str) -> None:
        self._projects_path.write_text(json.dumps({'projects': [{
            'id': 'pulse-idea', 'title': 'Pulse Idea', 'state': state,
            'promoted_from': {'kind': 'mission', 'mission_id': 'm-pulse'},
        }]}))

    def _funnel_refs(self) -> set[str]:
        out = da._handle_missions_derived(
            missions_path=self._missions_path,
            captures_path=self._captures_path,
            supabase_client=self._stub,
            repo=None, task_id=None, now=NOW,
            projects_path=self._projects_path,
        )
        return ({i['ref'] for i in out['funnel']['primary']}
                | {i['ref'] for i in out['funnel']['secondary']})

    def test_active_project_suppresses_then_archive_restores(self) -> None:
        self._seed()
        before = dict(json.loads(self._missions_path.read_text()))

        self._write_projects('active')
        self.assertNotIn('m-pulse', self._funnel_refs())

        self._write_projects('archived')
        self.assertIn('m-pulse', self._funnel_refs())

        # Reversibility means NO data loss: the mission record is byte-identical.
        self.assertEqual(json.loads(self._missions_path.read_text()), before)

    def test_no_projects_file_leaves_mission_in_funnel(self) -> None:
        self._seed()  # _projects_path points at a non-existent file
        self.assertIn('m-pulse', self._funnel_refs())


class SequenceRollupDoneFlipTest(_DerivedTestBase):
    """End-to-end through `_handle_missions_derived` (projects-v3
    sequence-rollup-done-flip): a phase whose linked build sequence is COMPLETE
    reads `done` in the pipeline AND its child step-cards collapse out of the
    orphan surface — driven entirely by the on-disk build-sequences dir, with no
    write to projects.json (read-time derive)."""

    def setUp(self) -> None:
        super().setUp()
        # isolate: no missions, no captures.
        self._missions_path.write_text(json.dumps(
            {'schema_version': 1, 'missions': [], 'last_synced_at': None}) + '\n')
        self._captures_path.write_text(json.dumps(
            {'schema_version': 1, 'captures': []}) + '\n')
        self._projects_path = self._tmp / 'projects.json'
        self._seq_root = self._tmp / 'build-sequences'
        self._seq_root.mkdir()

    def _write_projects(self, lifecycle: str, seq_ref: str) -> None:
        self._projects_path.write_text(json.dumps({'projects': [{
            'id': 'rollup-demo', 'title': 'Rollup Demo', 'state': 'active',
            'phases': [{'id': 'phase-1', 'title': 'Phase 1',
                        'lifecycle_state': lifecycle, 'sequence_ref': seq_ref}],
        }]}))

    def _write_sequence(self, seq_id: str, status: str, step_ids: list[str]) -> None:
        (self._seq_root / f'{seq_id}.json').write_text(json.dumps({
            'seq_id': seq_id, 'status': status,
            'steps': [{'step_id': s, 'status': 'merged'} for s in step_ids],
        }))

    def _derive(self, rows: list[dict]) -> dict:
        return da._handle_missions_derived(
            missions_path=self._missions_path,
            captures_path=self._captures_path,
            supabase_client=_StubClient(rows),
            repo=None, task_id=None, now=NOW,
            projects_path=self._projects_path,
            build_sequences_root=self._seq_root,
        )

    def _phase_card(self, out: dict) -> dict:
        return out['pipeline'][0]['phases'][0]

    def test_complete_sequence_flips_parent_to_done(self) -> None:
        self._write_projects('building', 'launch-rollup-demo')
        self._write_sequence(
            'launch-rollup-demo', 'complete',
            ['rollup-step-cleanup', 'rollup-step-postmerge'])
        # a step's chain-event surfaces under its step_id; without collapse it
        # would float as a loose Shipped orphan.
        rows = [{'event_type': 'auto_merge', 'task_id': 'rollup-step-cleanup',
                 'agent': 'forge',
                 'pr_url': 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/901',
                 'ts': '2026-06-09T10:00:00+00:00', 'payload': {}}]
        out = self._derive(rows)
        # 1. parent phase flipped to done + project header agrees
        self.assertEqual(self._phase_card(out)['lifecycle_state'], 'done')
        self.assertEqual(out['pipeline'][0]['status'], 'done')
        # 2. the step did NOT float as a loose orphan card
        self.assertNotIn('rollup-step-cleanup',
                         {o['task_id'] for o in out['orphans']})

    def test_active_sequence_reads_building(self) -> None:
        self._write_projects('spec', 'launch-rollup-demo')
        self._write_sequence('launch-rollup-demo', 'active', ['rollup-step-1'])
        out = self._derive([])
        self.assertEqual(self._phase_card(out)['lifecycle_state'], 'building')

    def test_missing_sequence_file_degrades_to_stored(self) -> None:
        # phase carries a sequence_ref but the file was archived/never written:
        # render the stored lane, no error, no false Done.
        self._write_projects('spec', 'launch-rollup-demo')  # no sequence file
        out = self._derive([])
        self.assertEqual(self._phase_card(out)['lifecycle_state'], 'spec')


class NormalizeSuggestedSourceUnitTest(unittest.TestCase):
    def test_known_agents_normalize(self) -> None:
        self.assertEqual(da._normalize_suggested_source('beacon'), 'beacon')
        self.assertEqual(da._normalize_suggested_source('Medic'), 'medic')
        self.assertEqual(da._normalize_suggested_source('pulse-cycle'), 'pulse')

    def test_capture_label_maps(self) -> None:
        self.assertEqual(da._normalize_suggested_source('pulse-check-i'), 'pulse')

    def test_unknown_and_non_str_return_none(self) -> None:
        for bad in ('forge', 'mirror', 'heal_orphan_autoregister', '', None, 5):
            self.assertIsNone(da._normalize_suggested_source(bad), bad)

    def test_first_identifiable_candidate_wins(self) -> None:
        self.assertEqual(
            da._normalize_suggested_source(None, 'forge', 'beacon'), 'beacon')


class TTLCacheUnitTest(unittest.TestCase):
    """The ~10s in-process derive cache (p2fix-derive-cache).

    Uses an injectable clock so hit + expiry are deterministic without sleeping.
    """

    def _make(self, ttl=10.0):
        clock = {'t': 1000.0}
        cache = da._TTLCache(ttl_seconds=ttl, clock=lambda: clock['t'])
        return cache, clock

    def test_hit_within_ttl_skips_recompute(self) -> None:
        cache, clock = self._make(ttl=10.0)
        calls = {'n': 0}

        def compute():
            calls['n'] += 1
            return f'v{calls["n"]}'

        first = cache.get_or_compute('k', compute)
        # Advance the clock, but stay inside the TTL window.
        clock['t'] += 9.0
        second = cache.get_or_compute('k', compute)

        self.assertEqual(first, 'v1')
        self.assertEqual(second, 'v1')  # served from cache, not recomputed
        self.assertEqual(calls['n'], 1)

    def test_expiry_after_ttl_recomputes(self) -> None:
        cache, clock = self._make(ttl=10.0)
        calls = {'n': 0}

        def compute():
            calls['n'] += 1
            return f'v{calls["n"]}'

        first = cache.get_or_compute('k', compute)
        # Step past the TTL window: the entry is stale and must be recomputed.
        clock['t'] += 10.0
        second = cache.get_or_compute('k', compute)

        self.assertEqual(first, 'v1')
        self.assertEqual(second, 'v2')
        self.assertEqual(calls['n'], 2)

    def test_distinct_keys_cached_independently(self) -> None:
        cache, _clock = self._make(ttl=10.0)
        calls = {'n': 0}

        def compute():
            calls['n'] += 1
            return calls['n']

        self.assertEqual(cache.get_or_compute(('r', None), compute), 1)
        self.assertEqual(cache.get_or_compute(('r', 't'), compute), 2)
        # Re-fetching each key inside the window returns the per-key value.
        self.assertEqual(cache.get_or_compute(('r', None), compute), 1)
        self.assertEqual(cache.get_or_compute(('r', 't'), compute), 2)
        self.assertEqual(calls['n'], 2)


if __name__ == '__main__':
    unittest.main()
