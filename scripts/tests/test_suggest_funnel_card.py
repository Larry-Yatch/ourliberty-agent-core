#!/usr/bin/env python3
"""Tests for suggest_funnel_card (projects-v3 P2 Contract C).

Covers the Mirror-review focus areas:
  * ONE SHARED INTERFACE (no per-agent bespoke) — Beacon, Medic, and Pulse all
    go through the same `suggest_funnel_card(...)`; only the `source` arg differs,
    and an out-of-vocabulary source is refused.
  * SOURCE-TAGGED — the queued entry carries `proposed_by=<source>`, and that tag
    flows through the REAL funnel derive (`dashboard_api._normalize_suggested_source`
    → `_build_funnel`) so the card lands in the PRIMARY (suggested) lane with the
    right `suggested_source`. The funnel module is the real one — no re-port, no
    drift.
  * REUSES SAFE-INBOX/INGEST — the entry is dropped into the new-mission-queue and
    drained by the REAL `heal_orphan_autoregister.drain_new_mission_queue` (the
    single committer), then appears as a `proposed` mission. No bespoke writer.

Effectful edges are confined to a tmp queue dir (OURLIBERTY_AGENTS_ROOT override);
no test touches the live missions.json, Supabase, or gh. Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_suggest_funnel_card
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

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import suggest_funnel_card as s  # noqa: E402
import heal_orphan_autoregister as h  # noqa: E402  (the real drain — single committer)
import dashboard_api as derive  # noqa: E402  (the real funnel derive — no drift)

_NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


class BuildSuggestedEntryTest(unittest.TestCase):
    """The pure entry builder — shape, source-tagging, validation."""

    def test_all_three_sources_build_valid_proposed_entries(self):
        # One shared interface: the only difference between the three agents is
        # the `source` arg, and every result is a well-formed `proposed` entry.
        for src in ('beacon', 'medic', 'pulse'):
            entry = s.build_suggested_entry(
                source=src, name='Reduce orphan churn',
                brief='Repeated re-proposals seen on the board.',
                repo='ourliberty-agent-core', now=_NOW)
            self.assertEqual(entry['phase'], 'proposed')
            self.assertEqual(entry['proposed_by'], src)
            self.assertEqual(entry['id'], 'reduce-orphan-churn')
            self.assertEqual(entry['repo'], 'ourliberty-agent-core')
            # Must satisfy the healer's gate so the drain actually appends it.
            self.assertTrue(h._valid_mission_entry(entry, entry['id']))

    def test_source_is_case_insensitive_and_normalized(self):
        entry = s.build_suggested_entry(
            source='PULSE', name='X', brief='b', now=_NOW)
        self.assertEqual(entry['proposed_by'], 'pulse')

    def test_unknown_source_is_refused(self):
        for bad in ('forge', 'mirror', 'larry', '', 'beaconx', None):
            with self.assertRaises(s.SuggestionError):
                s.build_suggested_entry(source=bad, name='X', brief='b', now=_NOW)

    def test_empty_kebab_name_is_refused(self):
        with self.assertRaises(s.SuggestionError):
            s.build_suggested_entry(source='beacon', name='   ---  ', brief='b')

    def test_optional_lists_default_empty_and_copy_inputs(self):
        specs = ['docs/spec.md']
        tasks = ['task-1']
        entry = s.build_suggested_entry(
            source='medic', name='Y', brief='b',
            spec_docs=specs, task_ids=tasks, now=_NOW)
        self.assertEqual(entry['spec_docs'], ['docs/spec.md'])
        self.assertEqual(entry['task_ids'], ['task-1'])
        # Inputs are copied, not aliased.
        specs.append('mutated')
        tasks.append('mutated')
        self.assertEqual(entry['spec_docs'], ['docs/spec.md'])
        self.assertEqual(entry['task_ids'], ['task-1'])

        bare = s.build_suggested_entry(source='medic', name='Z', brief='b')
        self.assertEqual(bare['spec_docs'], [])
        self.assertEqual(bare['task_ids'], [])


class EnqueueTest(unittest.TestCase):
    """suggest_funnel_card writes one atomic queue file; dedups in-flight dups."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.qd = Path(self.tmp) / 'new-mission-queue'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_enqueue_writes_one_json_file_with_the_entry(self):
        result = s.suggest_funnel_card(
            source='beacon', name='Wire deploy notifier',
            brief='Notify on deploy.', repo='ourliberty-agent-core',
            queue_dir=self.qd, now=_NOW)
        self.assertEqual(result, {
            'mission_id': 'wire-deploy-notifier',
            'status': 'queued',
            'source': 'beacon',
        })
        files = list(self.qd.glob('*.json'))
        self.assertEqual([f.name for f in files], ['wire-deploy-notifier.json'])
        written = json.loads(files[0].read_text())
        self.assertEqual(written['proposed_by'], 'beacon')
        self.assertEqual(written['phase'], 'proposed')

    def test_no_tmp_file_left_behind(self):
        s.suggest_funnel_card(source='pulse', name='A', brief='b',
                              queue_dir=self.qd, now=_NOW)
        self.assertEqual(list(self.qd.glob('*.tmp')), [])

    def test_duplicate_inflight_id_is_refused(self):
        s.suggest_funnel_card(source='pulse', name='Same Card', brief='b',
                              queue_dir=self.qd, now=_NOW)
        with self.assertRaises(s.SuggestionError):
            s.suggest_funnel_card(source='medic', name='same card', brief='b2',
                                  queue_dir=self.qd, now=_NOW)
        # Original file is untouched (the second call clobbered nothing).
        written = json.loads((self.qd / 'same-card.json').read_text())
        self.assertEqual(written['proposed_by'], 'pulse')
        self.assertEqual(written['brief'], 'b')

    def test_queue_dir_resolves_under_agents_root_override(self):
        from unittest import mock
        with mock.patch.dict('os.environ',
                             {'OURLIBERTY_AGENTS_ROOT': '/tmp/agts-suggest'}):
            self.assertEqual(
                str(s.new_mission_queue_dir()),
                '/tmp/agts-suggest/blackboard/new-mission-queue')


class EndToEndFunnelTest(unittest.TestCase):
    """The contract: a suggestion lands in the funnel PRIMARY (suggested) lane,
    source-tagged, after the REAL drain — exercising producer + ingest + derive
    together with no re-implementation of any of them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.qd = Path(self.tmp) / 'new-mission-queue'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_source_lands_in_primary_suggested_lane(self):
        for src in ('beacon', 'medic', 'pulse'):
            with self.subTest(source=src):
                s.suggest_funnel_card(
                    source=src, name=f'{src} idea',
                    brief='A team suggestion.', repo='ourliberty-agent-core',
                    queue_dir=self.qd, now=_NOW)

                # Drain via the REAL single-committer into a fresh registry.
                registry = {'schema_version': 1, 'missions': []}
                drained, _ = h.drain_new_mission_queue(registry, self.qd)
                self.assertIn(f'{src}-idea', drained)

                mission = registry['missions'][-1]
                self.assertEqual(mission['phase'], 'proposed')
                self.assertEqual(mission['proposed_by'], src)

                # Feed the drained mission through the REAL funnel derive.
                funnel = derive._build_funnel(
                    missions=registry['missions'], orphans=[], parked=[],
                    now=_NOW)
                primary_refs = {it['ref']: it for it in funnel['primary']}
                self.assertIn(f'{src}-idea', primary_refs)
                item = primary_refs[f'{src}-idea']
                self.assertEqual(item['kind'], 'suggested')
                self.assertEqual(item['suggested_source'], src)
                # Never lands in the secondary (orphan) lane.
                self.assertNotIn(
                    f'{src}-idea', {it['ref'] for it in funnel['secondary']})

                # Reset the queue dir for the next source.
                for f in self.qd.glob('*'):
                    f.unlink()


if __name__ == '__main__':
    unittest.main()
