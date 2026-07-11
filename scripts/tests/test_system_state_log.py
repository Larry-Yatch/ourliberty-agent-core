#!/usr/bin/env python3
"""Tests for system_state_log.py — the work-in-flight State Log (system
self-awareness Slice 1) — and state_log_query.py — Beacon's read side (§ D4).

Covers the spec's acceptance criteria that are unit-testable without the
droplet or the network:

  - build_snapshot is accurate: pipeline status mapping (building / in_review /
    merged / stuck / unknown), per-mission rollups, aggregate counts, the stuck
    list, in-flight count, sequences, parked passthrough. The gh probe is
    injected (no network).
  - The deterministic fallback narrative is never empty and reflects the
    snapshot, on a populated AND an idle system.
  - author_narrative uses the LLM voice when it yields prose, and falls back
    (fallback=True) when the voice raises OR returns nothing — never raises,
    never empty (the forced-fallback acceptance criterion).
  - write_state_log assembles the documented doc shape + provenance, and the
    `write=False` path never touches disk; the live default never mutates
    missions/captures (it only reads).
  - state_log_query: recognition vocabulary is distinct from catch_me_up;
    the reader degrades missing/malformed to present=False; format_reply
    degrades honestly when absent/stale.

Run:
    python3 -m unittest scripts.tests.test_system_state_log
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import state_log_query  # noqa: E402
import system_state_log as ssl  # noqa: E402
import task_terminal_state as tts  # noqa: E402

_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _mission(mid, name, phase, repo, task_ids):
    return {
        'id': mid, 'name': name, 'phase': phase, 'repo': repo,
        'task_ids': list(task_ids),
    }


class BuildSnapshotTest(unittest.TestCase):
    def test_status_mapping_and_rollups(self):
        missions = [
            _mission('m1', 'Alpha', 'in_flight', 'repo-a',
                     ['t-merged', 't-open', 't-building']),
            _mission('m2', 'Beta', 'in_flight', 'repo-b', ['t-closed']),
        ]
        probe_states = {
            't-merged': tts.MERGED,
            't-open': tts.OPEN,
            't-building': tts.UNKNOWN,   # + in-flight -> building
            't-closed': tts.CLOSED,      # -> stuck
        }
        snap = ssl.build_snapshot(
            missions=missions,
            in_flight=['t-building'],     # makes t-building -> building
            sequences=[],
            parked=0,
            now=_NOW,
            pipeline_probe=lambda ids: probe_states,
        )

        by_id = {m['id']: m for m in snap['missions_active']}
        alpha_status = {t['task_id']: t['status'] for t in by_id['m1']['tasks']}
        self.assertEqual(alpha_status['t-merged'], ssl.ST_MERGED)
        self.assertEqual(alpha_status['t-open'], ssl.ST_IN_REVIEW)
        self.assertEqual(alpha_status['t-building'], ssl.ST_BUILDING)
        self.assertEqual(by_id['m1']['rollup'],
                         '1 building / 1 in-review / 1 merged')

        # Aggregate pipeline counts across all missions.
        self.assertEqual(snap['pipeline']['building'], 1)
        self.assertEqual(snap['pipeline']['in_review'], 1)
        self.assertEqual(snap['pipeline']['merged_recent'], 1)

        # The closed-without-merge task is the only stuck entry.
        stuck = snap['pipeline']['stuck']
        self.assertEqual([s['task_id'] for s in stuck], ['t-closed'])
        self.assertEqual(stuck[0]['why'], 'PR closed without merging')

    def test_unknown_not_in_flight_is_unknown_not_building(self):
        missions = [_mission('m1', 'Alpha', 'in_flight', 'r', ['t-x'])]
        snap = ssl.build_snapshot(
            missions=missions, in_flight=[], sequences=[], parked=0,
            now=_NOW, pipeline_probe=lambda ids: {'t-x': tts.UNKNOWN},
        )
        status = snap['missions_active'][0]['tasks'][0]['status']
        self.assertEqual(status, ssl.ST_UNKNOWN)
        self.assertEqual(snap['pipeline']['building'], 0)

    def test_dedups_task_ids_before_probe(self):
        missions = [
            _mission('m1', 'A', 'in_flight', 'r', ['dup', 'dup']),
            _mission('m2', 'B', 'in_flight', 'r', ['dup']),
        ]
        seen_calls = {}

        def probe(ids):
            seen_calls['ids'] = list(ids)
            return {i: tts.MERGED for i in ids}

        ssl.build_snapshot(
            missions=missions, in_flight=[], sequences=[], parked=0,
            now=_NOW, pipeline_probe=probe,
        )
        self.assertEqual(seen_calls['ids'], ['dup'])  # deduped, single probe arg

    def test_passthrough_fields(self):
        seqs = [{'seq_id': 's1', 'step': '1/3', 'status': 'active'}]
        snap = ssl.build_snapshot(
            missions=[], in_flight=['a', 'b'], sequences=seqs, parked=4,
            now=_NOW, pipeline_probe=lambda ids: {},
        )
        self.assertEqual(snap['in_flight_now'], 2)
        self.assertEqual(snap['sequences_active'], seqs)
        # parked retained for back-compat; new cross-source fields default empty.
        waiting = snap['waiting_on_larry']
        self.assertEqual(waiting['parked'], 4)
        self.assertEqual(waiting['pending_approvals'], 0)
        self.assertEqual(waiting['escalations'], 0)
        self.assertEqual(waiting['total'], 4)
        self.assertEqual(waiting['items'], [])
        self.assertFalse(waiting['truncated'])
        self.assertIsNone(snap['health'])
        self.assertEqual(snap['as_of'], _NOW.isoformat())


class BulkTerminalStatesSnapshotTest(unittest.TestCase):
    """gh-api-burn phase 2: _bulk_terminal_states now reads the shared cached
    snapshot (gh_pr_snapshot.all_prs) instead of shelling `gh pr list` per repo.
    The reduce (match+classify+combine per task id) is unchanged; these tests
    inject at the snapshot seam."""

    def _pr(self, state, *, branch='', title='', number=1):
        return {'number': number, 'state': state, 'title': title,
                'headRefName': branch, 'url': f'https://x/pull/{number}'}

    def test_reduces_each_task_against_the_snapshot(self):
        import gh_pr_snapshot
        prs = [
            self._pr('MERGED', branch='forge/tsr-demo-task-alpha', number=1),
            self._pr('OPEN', branch='forge/tsr-demo-task-beta', number=2),
        ]
        with mock.patch.object(gh_pr_snapshot, 'all_prs', return_value=prs):
            out = ssl._bulk_terminal_states(
                ['tsr-demo-task-alpha', 'tsr-demo-task-beta', 'tsr-demo-missing'],
                repos=['owner/repo'],
            )
        self.assertEqual(out['tsr-demo-task-alpha'], tts.MERGED)
        self.assertEqual(out['tsr-demo-task-beta'], tts.OPEN)
        # No matching PR -> UNKNOWN (KEEP; conservative posture preserved).
        self.assertEqual(out['tsr-demo-missing'], tts.UNKNOWN)

    def test_snapshot_read_failure_leaves_all_unknown(self):
        import gh_pr_snapshot
        with mock.patch.object(gh_pr_snapshot, 'all_prs', return_value=[]):
            out = ssl._bulk_terminal_states(
                ['tsr-demo-task-alpha'], repos=['owner/repo'])
        self.assertEqual(out['tsr-demo-task-alpha'], tts.UNKNOWN)

    def test_reads_snapshot_once_per_repo_not_per_task(self):
        import gh_pr_snapshot
        with mock.patch.object(gh_pr_snapshot, 'all_prs',
                               return_value=[]) as m:
            ssl._bulk_terminal_states(
                ['a-task-one', 'a-task-two', 'a-task-three'],
                repos=['owner/a', 'owner/b'],
            )
        # One read per repo (2), independent of the 3 task ids.
        self.assertEqual(m.call_count, 2)

    def test_empty_task_ids_short_circuits(self):
        import gh_pr_snapshot
        with mock.patch.object(gh_pr_snapshot, 'all_prs',
                               side_effect=AssertionError('must not read')) as m:
            self.assertEqual(ssl._bulk_terminal_states([]), {})
        m.assert_not_called()


class FallbackNarrativeTest(unittest.TestCase):
    def test_idle_system_nonempty(self):
        snap = ssl.build_snapshot(
            missions=[], in_flight=[], sequences=[], parked=0,
            now=_NOW, pipeline_probe=lambda ids: {},
        )
        prose = ssl.render_fallback_narrative(snap)
        self.assertTrue(prose.strip())
        self.assertIn('No active missions', prose)
        self.assertIn('nothing parked', prose)

    def test_populated_reflects_snapshot(self):
        missions = [_mission('m1', 'Alpha', 'in_flight', 'repo-a', ['t1'])]
        snap = ssl.build_snapshot(
            missions=missions, in_flight=['t1'], sequences=[], parked=2,
            now=_NOW, pipeline_probe=lambda ids: {'t1': tts.OPEN},
        )
        prose = ssl.render_fallback_narrative(snap)
        self.assertIn('Alpha', prose)
        self.assertIn('repo-a', prose)
        self.assertIn('2 parked', prose)


class AuthorNarrativeTest(unittest.TestCase):
    def _snap(self):
        return ssl.build_snapshot(
            missions=[], in_flight=[], sequences=[], parked=0,
            now=_NOW, pipeline_probe=lambda ids: {},
        )

    def test_uses_llm_voice_when_present(self):
        def voice(prompt, keys=()):
            return {'narrative': 'All quiet on the western front.'}

        prose, fallback = ssl.author_narrative(
            self._snap(), use_llm=True, voice_fn=voice)
        self.assertEqual(prose, 'All quiet on the western front.')
        self.assertFalse(fallback)

    def test_falls_back_when_voice_raises(self):
        def boom(prompt, keys=()):
            raise RuntimeError('claude unavailable')

        prose, fallback = ssl.author_narrative(
            self._snap(), use_llm=True, voice_fn=boom)
        self.assertTrue(fallback)
        self.assertTrue(prose.strip())  # deterministic fallback, never empty

    def test_falls_back_when_voice_empty(self):
        for empty in (None, {}, {'narrative': '   '}):
            prose, fallback = ssl.author_narrative(
                self._snap(), use_llm=True, voice_fn=lambda p, keys=(): empty)
            self.assertTrue(fallback)
            self.assertTrue(prose.strip())

    def test_no_llm_skips_voice(self):
        sentinel = {'called': False}

        def voice(prompt, keys=()):
            sentinel['called'] = True
            return {'narrative': 'x'}

        prose, fallback = ssl.author_narrative(
            self._snap(), use_llm=False, voice_fn=voice)
        self.assertTrue(fallback)
        self.assertFalse(sentinel['called'])


class WriteStateLogTest(unittest.TestCase):
    def test_doc_shape_and_provenance_fallback(self):
        with mock.patch.object(ssl, 'load_inputs', return_value={
            'missions': [], 'in_flight': [], 'sequences': [], 'parked': 0,
        }):
            doc = ssl.write_state_log(
                now=_NOW, use_llm=False, write=False,
                pipeline_probe=lambda ids: {},
            )
        self.assertEqual(doc['schema_version'], ssl.SCHEMA_VERSION)
        self.assertEqual(doc['as_of'], _NOW.isoformat())
        self.assertTrue(doc['narrative_prose'].strip())
        self.assertIn('structured_snapshot', doc)
        prov = doc['provenance']
        self.assertEqual(prov['by'], ssl.NARRATOR_BY)
        self.assertTrue(prov['fallback'])
        self.assertEqual(prov['model'], 'raw')  # fallback -> raw, not the model

    def test_write_false_touches_no_disk(self):
        with mock.patch.object(ssl, 'load_inputs', return_value={
            'missions': [], 'in_flight': [], 'sequences': [], 'parked': 0,
        }), mock.patch.object(ssl, 'atomic_write_json') as writer:
            ssl.write_state_log(
                now=_NOW, use_llm=False, write=False,
                pipeline_probe=lambda ids: {},
            )
            writer.assert_not_called()

    def test_write_true_calls_atomic_writer_once(self):
        with mock.patch.object(ssl, 'load_inputs', return_value={
            'missions': [], 'in_flight': [], 'sequences': [], 'parked': 0,
        }), mock.patch.object(ssl, 'atomic_write_json') as writer:
            ssl.write_state_log(
                now=_NOW, use_llm=False, write=True,
                pipeline_probe=lambda ids: {},
            )
            self.assertEqual(writer.call_count, 1)


class StateLogQueryRecognitionTest(unittest.TestCase):
    def test_work_in_flight_phrases_match(self):
        for phrase in [
            'work in flight', 'where are we on work in flight?',
            'state of work', '/state', 'whats in flight',
        ]:
            self.assertTrue(
                state_log_query.is_state_log_query(phrase),
                f'{phrase!r} should match the state-log query',
            )

    def test_catch_me_up_phrases_do_not_collide(self):
        # These belong to catch_me_up's delta synthesizer, NOT the State Log.
        for phrase in ['status', 'where are we', 'summary', 'catch me up']:
            self.assertFalse(
                state_log_query.is_state_log_query(phrase),
                f'{phrase!r} must NOT be hijacked by the state-log query',
            )

    def test_empty_is_not_a_query(self):
        self.assertFalse(state_log_query.is_state_log_query(''))
        self.assertFalse(state_log_query.is_state_log_query('   '))


class StateLogQueryReaderTest(unittest.TestCase):
    def test_missing_file_degrades_present_false(self):
        with tempfile.TemporaryDirectory() as d:
            doc = state_log_query.read_state_log(Path(d) / 'nope.json')
        self.assertFalse(doc['present'])
        self.assertTrue(doc['stale'])

    def test_malformed_file_degrades_present_false(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'state.json'
            p.write_text('{not json')
            doc = state_log_query.read_state_log(p)
        self.assertFalse(doc['present'])

    def test_fresh_file_present_not_stale(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'state.json'
            p.write_text(json.dumps({
                'schema_version': 1,
                'as_of': _NOW.isoformat(),
                'narrative_prose': 'Two missions are progressing.',
                'structured_snapshot': {'missions_active': []},
                'provenance': {'by': 'system-state-narrator'},
            }))
            doc = state_log_query.read_state_log(p)
        self.assertTrue(doc['present'])
        self.assertFalse(doc['stale'])  # just written -> fresh
        self.assertEqual(doc['narrative_prose'], 'Two missions are progressing.')


class StateLogQueryFormatTest(unittest.TestCase):
    def test_absent_degrades_honestly(self):
        reply = state_log_query.format_reply({'present': False})
        self.assertIn("don't have a work-in-flight picture", reply)

    def test_stale_warns(self):
        reply = state_log_query.format_reply({
            'present': True, 'stale': True, 'age_sec': 3600,
            'narrative_prose': 'Things are happening.',
        })
        self.assertIn('Things are happening.', reply)
        self.assertIn('out of date', reply)

    def test_fresh_includes_narrative_and_freshness(self):
        reply = state_log_query.format_reply({
            'present': True, 'stale': False, 'age_sec': 120,
            'narrative_prose': 'Alpha shipped; Beta in review.',
        })
        self.assertIn('Alpha shipped; Beta in review.', reply)
        self.assertIn('as of', reply)


def _wi(source, ident, ts, severity=None):
    """A raw waiting-item as a source reader would emit it (carries `_ts`, which
    build_snapshot turns into `age_seconds`)."""
    return {
        'source': source, 'id': ident, 'title': f'{source}-{ident}',
        'why': 'because', 'severity': severity, 'action_hint': 'do', '_ts': ts,
    }


class WaitingOnLarryAggregationTest(unittest.TestCase):
    """Slice 2a — the itemized cross-source waiting_on_larry block."""

    def test_aggregates_three_sources_with_counts_and_order(self):
        snap = ssl.build_snapshot(
            missions=[], in_flight=[], sequences=[], parked=2,
            now=_NOW, pipeline_probe=lambda ids: {},
            parked_items=[
                _wi('parked', 'p-old', '2026-06-18T12:00:00+00:00'),   # 24h
                _wi('parked', 'p-new', '2026-06-19T11:30:00+00:00'),   # 30m
            ],
            pending_approvals=[
                _wi('approval', 'a1', '2026-06-19T10:00:00+00:00', 'warning'),
            ],
            escalations=[
                _wi('escalation', 'e1', '2026-06-19T11:00:00+00:00', 'critical'),
            ],
        )
        w = snap['waiting_on_larry']
        self.assertEqual(w['parked'], 2)            # back-compat field retained
        self.assertEqual(w['pending_approvals'], 1)
        self.assertEqual(w['escalations'], 1)
        self.assertEqual(w['total'], 4)
        self.assertFalse(w['truncated'])
        # Ordered escalations -> approvals -> aged parked (oldest parked first).
        self.assertEqual([i['source'] for i in w['items']],
                         ['escalation', 'approval', 'parked', 'parked'])
        self.assertEqual([i['id'] for i in w['items']],
                         ['e1', 'a1', 'p-old', 'p-new'])
        # age_seconds is computed from _ts against `now`, and _ts is stripped.
        self.assertEqual(w['items'][0]['age_seconds'], 3600)
        self.assertEqual(w['items'][2]['age_seconds'], 86400)
        self.assertNotIn('_ts', w['items'][0])

    def test_escalations_ordered_by_severity_then_age(self):
        snap = ssl.build_snapshot(
            missions=[], in_flight=[], sequences=[], parked=0,
            now=_NOW, pipeline_probe=lambda ids: {},
            escalations=[
                _wi('escalation', 'warn-new', '2026-06-19T11:30:00+00:00', 'warning'),
                _wi('escalation', 'crit', '2026-06-19T11:00:00+00:00', 'critical'),
                _wi('escalation', 'warn-old', '2026-06-19T09:00:00+00:00', 'warning'),
            ],
        )
        ids = [i['id'] for i in snap['waiting_on_larry']['items']]
        self.assertEqual(ids, ['crit', 'warn-old', 'warn-new'])

    def test_truncation_caps_items_but_not_counts(self):
        parked = [
            _wi('parked', f'p{i:02d}', '2026-06-18T12:00:00+00:00')
            for i in range(30)
        ]
        snap = ssl.build_snapshot(
            missions=[], in_flight=[], sequences=[], parked=30,
            now=_NOW, pipeline_probe=lambda ids: {},
            parked_items=parked,
        )
        w = snap['waiting_on_larry']
        self.assertEqual(len(w['items']), ssl.WAITING_ITEMS_CAP)
        self.assertTrue(w['truncated'])
        self.assertEqual(w['total'], 30)      # totals reflect all, not just shown

    def test_empty_sources_yield_empty_items_not_truncated(self):
        snap = ssl.build_snapshot(
            missions=[], in_flight=[], sequences=[], parked=0,
            now=_NOW, pipeline_probe=lambda ids: {},
        )
        w = snap['waiting_on_larry']
        self.assertEqual(w['items'], [])
        self.assertEqual(w['total'], 0)
        self.assertFalse(w['truncated'])

    def test_narrative_reflects_cross_source_total(self):
        snap = ssl.build_snapshot(
            missions=[], in_flight=[], sequences=[], parked=1,
            now=_NOW, pipeline_probe=lambda ids: {},
            parked_items=[_wi('parked', 'p1', '2026-06-18T12:00:00+00:00')],
            pending_approvals=[
                _wi('approval', 'a1', '2026-06-19T11:00:00+00:00', 'warning')],
            escalations=[
                _wi('escalation', 'e1', '2026-06-19T11:00:00+00:00', 'critical')],
        )
        prose = ssl.render_fallback_narrative(snap)
        self.assertIn('Waiting on you: 3 item(s)', prose)
        self.assertIn('1 approval(s)', prose)
        self.assertIn('1 escalation(s)', prose)
        self.assertIn('1 parked', prose)

    def test_narrative_idle_says_nothing_parked(self):
        snap = ssl.build_snapshot(
            missions=[], in_flight=[], sequences=[], parked=0,
            now=_NOW, pipeline_probe=lambda ids: {},
        )
        self.assertIn('nothing parked', ssl.render_fallback_narrative(snap))


class WaitingSourceReadersTest(unittest.TestCase):
    """Each source reader is independently fail-open (spec § 3 D4)."""

    def setUp(self):
        # Hermetic for-Larry sources (regression-gate false-BLOCK class,
        # 2026-07-08). `load_for_larry_escalations` folds in THREE sources; the
        # tests below override the escalations feed + legacy file but not the
        # `for_larry_signal` source (1b), which — with no override — reads the
        # DEFAULT `OURLIBERTY_AGENTS_ROOT/blackboard/for-larry-escalations.json`.
        # `_bootstrap` pins one shared agents-root for the whole process, so an
        # unresolved record written there by ANY earlier test in the full-suite
        # run leaks into these readers and flips their strict assertions — a
        # full-suite-only failure that passes in isolation and false-BLOCKed the
        # regression gate. Point every for-Larry source at a fresh, nonexistent
        # tmp path so each reader starts empty; a test that populates one source
        # re-overrides just that key (mock.patch.dict merges, per-test wins).
        iso = tempfile.TemporaryDirectory()
        self.addCleanup(iso.cleanup)
        root = Path(iso.name)
        env = mock.patch.dict(os.environ, {
            'OURLIBERTY_FOR_LARRY_SIGNAL_FILE': str(root / 'signal.json'),
            'OURLIBERTY_FOR_LARRY_FEED_FILE': str(root / 'feed.json'),
            'OURLIBERTY_ESCALATIONS_FILE': str(root / 'esc.json'),
            'OURLIBERTY_PENDING_APPROVALS': str(root / 'pending.json'),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_pending_approvals_maps_entries(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'pending.json'
            p.write_text(json.dumps({'pending': [
                {'id': 'task-1', 'plan_summary': 'Ship the thing',
                 'target_agent': 'forge', 'status': 'pending',
                 'created_at': '2026-06-19T11:00:00+00:00'},
            ]}))
            with mock.patch.dict(os.environ,
                                 {'OURLIBERTY_PENDING_APPROVALS': str(p)}):
                items = ssl.load_pending_approvals()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['source'], 'approval')
        self.assertEqual(items[0]['id'], 'task-1')
        self.assertEqual(items[0]['title'], 'Ship the thing')
        self.assertEqual(items[0]['severity'], 'warning')
        self.assertEqual(items[0]['action_hint'], 'approve/reject in Approvals')

    def test_pending_approvals_missing_file_fails_open(self):
        with mock.patch.dict(
                os.environ,
                {'OURLIBERTY_PENDING_APPROVALS': '/no/such/pending.json'}):
            self.assertEqual(ssl.load_pending_approvals(), [])

    def test_pending_approvals_malformed_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'bad.json'
            p.write_text('{not json')
            with mock.patch.dict(os.environ,
                                 {'OURLIBERTY_PENDING_APPROVALS': str(p)}):
                self.assertEqual(ssl.load_pending_approvals(), [])

    def test_escalations_conservative_requires_for_larry_flag(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'esc.json'
            p.write_text(json.dumps([
                {'headline': 'ops-internal noise', 'severity': 'high'},
                {'headline': 'needs you', 'severity': 'critical',
                 'for_larry': True, 'ts': '2026-06-19T10:00:00+00:00'},
                {'headline': 'already handled', 'for_larry': True,
                 'resolved': True, 'ts': '2026-06-19T09:00:00+00:00'},
            ]))
            with mock.patch.dict(os.environ,
                                 {'OURLIBERTY_ESCALATIONS_FILE': str(p),
                                  'OURLIBERTY_FOR_LARRY_FEED_FILE':
                                      '/no/such/feed.json'}):
                items = ssl.load_for_larry_escalations()
        # Only the unresolved, explicitly-flagged entry folds in.
        self.assertEqual([i['id'] for i in items], ['needs you'])
        self.assertEqual(items[0]['source'], 'escalation')
        self.assertEqual(items[0]['severity'], 'critical')

    def test_escalations_missing_file_fails_open(self):
        with mock.patch.dict(
                os.environ,
                {'OURLIBERTY_ESCALATIONS_FILE': '/no/such/esc.json',
                 'OURLIBERTY_FOR_LARRY_FEED_FILE': '/no/such/feed.json'}):
            self.assertEqual(ssl.load_for_larry_escalations(), [])

    def test_one_bad_source_does_not_break_the_others(self):
        # A missing pending file degrades to [] while a good escalations file
        # still aggregates — sources fail open independently.
        with tempfile.TemporaryDirectory() as d:
            good_esc = Path(d) / 'esc.json'
            good_esc.write_text(json.dumps([
                {'headline': 'needs you', 'severity': 'critical',
                 'for_larry': True, 'ts': '2026-06-19T10:00:00+00:00'},
            ]))
            env = {
                'OURLIBERTY_PENDING_APPROVALS': '/no/such/pending.json',
                'OURLIBERTY_ESCALATIONS_FILE': str(good_esc),
                'OURLIBERTY_FOR_LARRY_FEED_FILE': '/no/such/feed.json',
            }
            with mock.patch.dict(os.environ, env):
                pending = ssl.load_pending_approvals()
                escalations = ssl.load_for_larry_escalations()
        self.assertEqual(pending, [])
        self.assertEqual(len(escalations), 1)

    def test_feed_records_fold_in(self):
        # Source 1 (Phase 2 Change C): the durable for-Larry escalations feed
        # (`for_larry_escalations.list_open`) — the actual mirror-review producer.
        import for_larry_escalations as fle
        with tempfile.TemporaryDirectory() as d:
            feed = Path(d) / 'for-larry-escalations.json'
            with mock.patch.dict(
                    os.environ,
                    {'OURLIBERTY_FOR_LARRY_FEED_FILE': str(feed),
                     'OURLIBERTY_ESCALATIONS_FILE': '/no/such/esc.json'}):
                fle.upsert(
                    'zz-fixture-a', headline='critical unhandled',
                    context='crossed the bar', severity='critical')
                fle.upsert(
                    'zz-fixture-b', headline='Forge is stuck',
                    context='clarify exhausted', severity='warning')
                fle.clear('zz-fixture-b')
                items = ssl.load_for_larry_escalations()
        # Only the unresolved record folds in; the cleared one is filtered out.
        self.assertEqual([i['id'] for i in items], ['zz-fixture-a'])
        self.assertEqual(items[0]['source'], 'escalation')
        self.assertEqual(items[0]['severity'], 'critical')

    def test_feed_read_failure_does_not_break_legacy_source(self):
        # Source 1 failing open must still let Source 2 (legacy) aggregate.
        import for_larry_escalations as fle
        with tempfile.TemporaryDirectory() as d:
            legacy = Path(d) / 'esc.json'
            legacy.write_text(json.dumps([
                {'headline': 'needs you', 'severity': 'critical',
                 'for_larry': True, 'ts': '2026-06-19T10:00:00+00:00'}]))
            with mock.patch.dict(
                    os.environ,
                    {'OURLIBERTY_ESCALATIONS_FILE': str(legacy),
                     'OURLIBERTY_FOR_LARRY_FEED_FILE': '/no/such/feed.json'}), \
                    mock.patch.object(
                        fle, 'list_open', side_effect=RuntimeError('boom')):
                items = ssl.load_for_larry_escalations()
        self.assertEqual([i['id'] for i in items], ['needs you'])


class WaitingSequencesReaderTest(unittest.TestCase):
    """load_waiting_sequences — paused sequences + stuck-dispatched steps as
    source='sequence' waiting items (operator-needs-you-feed §5.3 + §5.4)."""

    def _write_seq(self, d: Path, seq_id: str, seq: dict) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{seq_id}.json').write_text(json.dumps(seq))

    def _run(self, build):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / 'build-sequences'
            build(d)
            with mock.patch.dict(
                    os.environ, {'OURLIBERTY_BUILD_SEQUENCES_DIR': str(d)}):
                return ssl.load_waiting_sequences(_NOW)

    def test_paused_sequence_yields_one_item(self):
        def build(d):
            self._write_seq(d, 'seq-1', {
                'seq_id': 'seq-1', 'status': 'paused', 'steps': [],
                'audit_log': [
                    {'ts': '2026-06-19T11:00:00+00:00', 'event': 'paused',
                     'actor': 'beacon'},
                ],
            })
        items = self._run(build)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it['source'], 'sequence')
        self.assertEqual(it['id'], 'seq-1')
        self.assertEqual(it['seq_id'], 'seq-1')
        self.assertIsNone(it['step_id'])
        self.assertEqual(it['actions'], ['resume', 'cancel'])
        self.assertIn('Paused', it['why'])
        self.assertEqual(it['_ts'], '2026-06-19T11:00:00+00:00')

    def test_fresh_dispatched_step_yields_no_item(self):
        # Dispatched 5 minutes ago (< 15-min threshold) → not yet stuck.
        def build(d):
            self._write_seq(d, 'seq-1', {
                'seq_id': 'seq-1', 'status': 'active',
                'steps': [
                    {'step_id': 'step-1', 'status': 'dispatched',
                     'pr_url': None,
                     'dispatched_at': '2026-06-19T11:55:00+00:00'},
                ],
            })
        self.assertEqual(self._run(build), [])

    def test_stuck_dispatched_step_yields_one_item(self):
        # Dispatched 16 minutes ago (> 15-min threshold), no PR → stuck.
        def build(d):
            self._write_seq(d, 'seq-1', {
                'seq_id': 'seq-1', 'status': 'active',
                'steps': [
                    {'step_id': 'step-1', 'status': 'dispatched',
                     'pr_url': None,
                     'dispatched_at': '2026-06-19T11:44:00+00:00'},
                ],
            })
        items = self._run(build)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it['source'], 'sequence')
        self.assertEqual(it['id'], 'seq-1/step-1')
        self.assertEqual(it['seq_id'], 'seq-1')
        self.assertEqual(it['step_id'], 'step-1')
        self.assertEqual(it['actions'], ['skip', 'cancel'])
        self.assertIn('16m ago', it['why'])
        self.assertEqual(it['_ts'], '2026-06-19T11:44:00+00:00')

    def test_dispatched_step_with_pr_is_not_stuck(self):
        def build(d):
            self._write_seq(d, 'seq-1', {
                'seq_id': 'seq-1', 'status': 'active',
                'steps': [
                    {'step_id': 'step-1', 'status': 'dispatched',
                     'pr_url': 'https://github.com/o/r/pull/9',
                     'dispatched_at': '2026-06-19T10:00:00+00:00'},
                ],
            })
        self.assertEqual(self._run(build), [])

    def test_step_exactly_at_threshold_is_not_stuck(self):
        # Exactly 15 minutes → boundary is strict (> threshold), so no item.
        def build(d):
            self._write_seq(d, 'seq-1', {
                'seq_id': 'seq-1', 'status': 'active',
                'steps': [
                    {'step_id': 'step-1', 'status': 'dispatched',
                     'pr_url': None,
                     'dispatched_at': '2026-06-19T11:45:00+00:00'},
                ],
            })
        self.assertEqual(self._run(build), [])

    def test_missing_dir_fails_open(self):
        with mock.patch.dict(
                os.environ,
                {'OURLIBERTY_BUILD_SEQUENCES_DIR': '/no/such/build-seqs'}):
            self.assertEqual(ssl.load_waiting_sequences(_NOW), [])

    def test_read_failure_strict_raises_default_soft_empty(self):
        # A transient per-file read hiccup: strict=False degrades to [] (the
        # dashboard read model's fail-safe), but strict=True RAISES so the
        # emit-time projection can skip instead of blind-clearing still-open rows.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / 'build-sequences'
            self._write_seq(d, 'seq-1', {
                'seq_id': 'seq-1', 'status': 'paused', 'steps': [],
                'audit_log': [
                    {'ts': '2026-06-19T11:00:00+00:00', 'event': 'paused'},
                ],
            })
            with mock.patch.dict(
                    os.environ,
                    {'OURLIBERTY_BUILD_SEQUENCES_DIR': str(d)}), \
                 mock.patch.object(
                    Path, 'read_text', side_effect=OSError('transient read')):
                self.assertEqual(ssl.load_waiting_sequences(_NOW), [])
                with self.assertRaises(OSError):
                    ssl.load_waiting_sequences(_NOW, strict=True)

    def test_terminal_sequence_with_stuck_step_yields_nothing(self):
        # An archived/complete/failed/retired sequence whose leftover step is
        # still `dispatched` with no PR (real case: orchestrator-bootstrap-001,
        # archived weeks ago) must NOT surface — it's residue, not stuck work.
        # The ladder hides terminal sequences and the steering verbs no-op on
        # them, so a row here would be a dead end that never self-clears.
        for terminal in ('archived', 'complete', 'failed', 'retired'):
            with self.subTest(status=terminal):
                def build(d):
                    self._write_seq(d, 'seq-old', {
                        'seq_id': 'seq-old', 'status': terminal,
                        'steps': [
                            {'step_id': 'step-root', 'status': 'dispatched',
                             'pr_url': None,
                             'dispatched_at': '2026-06-19T11:00:00+00:00'},
                        ],
                    })
                self.assertEqual(self._run(build), [])

    def test_paused_with_stuck_step_yields_both(self):
        def build(d):
            self._write_seq(d, 'seq-1', {
                'seq_id': 'seq-1', 'status': 'paused',
                'steps': [
                    {'step_id': 'step-1', 'status': 'dispatched',
                     'pr_url': None,
                     'dispatched_at': '2026-06-19T11:40:00+00:00'},
                ],
                'audit_log': [
                    {'ts': '2026-06-19T11:50:00+00:00', 'event': 'paused',
                     'actor': 'beacon'},
                ],
            })
        items = self._run(build)
        self.assertEqual(len(items), 2)
        kinds = {(i['seq_id'], i['step_id']) for i in items}
        self.assertEqual(kinds, {('seq-1', None), ('seq-1', 'step-1')})


if __name__ == '__main__':
    unittest.main()
