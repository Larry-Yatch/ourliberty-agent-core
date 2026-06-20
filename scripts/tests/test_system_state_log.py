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
        self.assertEqual(snap['waiting_on_larry'], {'parked': 4})
        self.assertIsNone(snap['health'])
        self.assertEqual(snap['as_of'], _NOW.isoformat())


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


if __name__ == '__main__':
    unittest.main()
