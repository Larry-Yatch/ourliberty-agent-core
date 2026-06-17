"""Tests for projects_store — the Project+Phase schema, normalization, lifecycle
model, and the additive 'Actively working' pipeline derive (projects-v3 P3,
step p3-project-store)."""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import projects_store as ps  # noqa: E402

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


class LifecycleModelTest(unittest.TestCase):
    def test_canonical_states(self):
        self.assertEqual(
            ps.LIFECYCLE_STATES, ('brainstorm', 'spec', 'building', 'done'))

    def test_valid_state_predicate(self):
        for s in ps.LIFECYCLE_STATES:
            self.assertTrue(ps.is_valid_lifecycle_state(s))
        for bad in ('Brainstorm', 'shipped', '', None, 3):
            self.assertFalse(ps.is_valid_lifecycle_state(bad))

    def test_next_state(self):
        self.assertEqual(ps.next_lifecycle_state('brainstorm'), 'spec')
        self.assertEqual(ps.next_lifecycle_state('spec'), 'building')
        self.assertEqual(ps.next_lifecycle_state('building'), 'done')
        self.assertIsNone(ps.next_lifecycle_state('done'))      # terminal
        self.assertIsNone(ps.next_lifecycle_state('bogus'))

    def test_can_transition_forward_one_step_only(self):
        self.assertTrue(ps.can_transition('brainstorm', 'spec'))
        self.assertTrue(ps.can_transition('spec', 'building'))
        # skipping a stage forward is illegal — the gate between is the point
        self.assertFalse(ps.can_transition('brainstorm', 'building'))
        self.assertFalse(ps.can_transition('spec', 'done'))

    def test_can_transition_backward_and_noop(self):
        # a checkpoint "refine" can send a phase back; reversibility (spec § 5)
        self.assertTrue(ps.can_transition('building', 'spec'))
        self.assertTrue(ps.can_transition('done', 'brainstorm'))
        # no-op is idempotent-legal
        self.assertTrue(ps.can_transition('spec', 'spec'))

    def test_can_transition_rejects_unknown(self):
        self.assertFalse(ps.can_transition('brainstorm', 'shipped'))
        self.assertFalse(ps.can_transition('bogus', 'spec'))


class NormalizeRegistryTest(unittest.TestCase):
    def test_empty_registry_shape(self):
        reg = ps.empty_registry()
        self.assertEqual(reg, {'schema_version': 1, 'projects': []})

    def test_non_dict_input_degrades(self):
        reg, dropped = ps.normalize_registry(['not', 'a', 'dict'], now=NOW)
        self.assertEqual(reg, ps.empty_registry())
        self.assertEqual(dropped, ['<non-dict-registry>'])

    def test_missing_projects_key(self):
        reg, dropped = ps.normalize_registry({'schema_version': 1}, now=NOW)
        self.assertEqual(reg['projects'], [])
        self.assertEqual(dropped, [])

    def test_backfills_defaults(self):
        reg, dropped = ps.normalize_registry(
            {'projects': [{'id': 'proj-a', 'phases': [{'id': 'ph-1'}]}]}, now=NOW)
        self.assertEqual(dropped, [])
        proj = reg['projects'][0]
        self.assertEqual(proj['title'], 'proj-a')        # backfilled from id
        self.assertEqual(proj['state'], 'active')        # default
        self.assertIsNone(proj['north_star_ref'])
        self.assertIsNone(proj['repo'])
        self.assertTrue(proj['one_off'])                 # single phase
        ph = proj['phases'][0]
        self.assertEqual(ph['lifecycle_state'], 'brainstorm')   # default
        self.assertEqual(ph['desired_end_state'], '')
        self.assertIsNone(ph['spec_ref'])
        self.assertIsNone(ph['sequence_ref'])
        self.assertIn('created_at', ph)
        self.assertIn('updated_at', ph)

    def test_phases_sorted_by_order(self):
        reg, _ = ps.normalize_registry({'projects': [{
            'id': 'proj-a',
            'phases': [
                {'id': 'ph-2', 'order': 1},
                {'id': 'ph-0', 'order': 0},
                {'id': 'ph-3', 'order': 2},
            ],
        }]}, now=NOW)
        self.assertEqual(
            [p['id'] for p in reg['projects'][0]['phases']],
            ['ph-0', 'ph-2', 'ph-3'])

    def test_invalid_lifecycle_state_coerced_to_default(self):
        reg, _ = ps.normalize_registry({'projects': [{
            'id': 'proj-a',
            'phases': [{'id': 'ph-1', 'lifecycle_state': 'shipped'}],
        }]}, now=NOW)
        self.assertEqual(
            reg['projects'][0]['phases'][0]['lifecycle_state'], 'brainstorm')

    def test_invalid_project_state_coerced(self):
        reg, _ = ps.normalize_registry(
            {'projects': [{'id': 'proj-a', 'state': 'frozen'}]}, now=NOW)
        self.assertEqual(reg['projects'][0]['state'], 'active')

    def test_explicit_one_off_flag_honored(self):
        # a 2-phase project explicitly flagged one_off stays flagged
        reg, _ = ps.normalize_registry({'projects': [{
            'id': 'proj-a', 'one_off': True,
            'phases': [{'id': 'ph-1'}, {'id': 'ph-2'}],
        }]}, now=NOW)
        self.assertTrue(reg['projects'][0]['one_off'])

    def test_drops_malformed_entries(self):
        reg, dropped = ps.normalize_registry({'projects': [
            {'id': 'proj-ok'},
            'a string, not a dict',
            {'no_id': True},
            {'id': 'proj-2', 'phases': [{'no_id': 1}, {'id': 'ph-keep'}]},
        ]}, now=NOW)
        ids = [p['id'] for p in reg['projects']]
        self.assertEqual(ids, ['proj-ok', 'proj-2'])
        # the id-less project and the string both dropped
        self.assertEqual(len(dropped), 2)
        # the id-less phase was dropped, the good one kept
        self.assertEqual(
            [p['id'] for p in reg['projects'][1]['phases']], ['ph-keep'])

    def test_idempotent(self):
        raw = {'projects': [{
            'id': 'proj-a', 'title': 'A', 'state': 'active',
            'north_star_ref': 'docs/ns.md', 'repo': 'ourliberty-agent-core',
            'phases': [{
                'id': 'ph-1', 'title': 'P1', 'desired_end_state': 'why',
                'lifecycle_state': 'spec', 'order': 0,
                'spec_ref': 'specs/x.md', 'sequence_ref': 'seq-1',
            }],
        }]}
        once, d1 = ps.normalize_registry(raw, now=NOW)
        twice, d2 = ps.normalize_registry(once, now=NOW)
        self.assertEqual(once, twice)
        self.assertEqual(d1, [])
        self.assertEqual(d2, [])


class BuildPipelineTest(unittest.TestCase):
    def _norm(self, projects):
        reg, _ = ps.normalize_registry({'projects': projects}, now=NOW)
        return reg['projects']

    def test_empty(self):
        self.assertEqual(ps.build_pipeline([], NOW), [])

    def test_excludes_archived(self):
        projects = self._norm([
            {'id': 'proj-live', 'state': 'active', 'phases': [{'id': 'p1'}]},
            {'id': 'proj-gone', 'state': 'archived', 'phases': [{'id': 'p2'}]},
        ])
        out = ps.build_pipeline(projects, NOW)
        self.assertEqual([p['id'] for p in out], ['proj-live'])

    def test_phase_cards_expose_lifecycle_and_des(self):
        projects = self._norm([{
            'id': 'proj-a', 'state': 'active',
            'phases': [{
                'id': 'ph-1', 'title': 'Build it',
                'desired_end_state': 'so the pipeline has a place to live',
                'lifecycle_state': 'building',
                'spec_ref': 'specs/p3.md', 'sequence_ref': 'seq-42',
            }],
        }])
        card = ps.build_pipeline(projects, NOW)[0]['phases'][0]
        self.assertEqual(card['lifecycle_state'], 'building')
        self.assertEqual(card['desired_end_state'],
                         'so the pipeline has a place to live')
        self.assertEqual(card['spec_ref'], 'specs/p3.md')
        self.assertEqual(card['sequence_ref'], 'seq-42')

    def test_coarse_status_rollup(self):
        # all done → done
        done = self._norm([{'id': 'p', 'phases': [
            {'id': 'a', 'lifecycle_state': 'done'},
            {'id': 'b', 'lifecycle_state': 'done'}]}])
        self.assertEqual(ps.build_pipeline(done, NOW)[0]['status'], 'done')
        # any building (or mixed building/done) → building
        building = self._norm([{'id': 'p', 'phases': [
            {'id': 'a', 'lifecycle_state': 'building'},
            {'id': 'b', 'lifecycle_state': 'done'}]}])
        self.assertEqual(ps.build_pipeline(building, NOW)[0]['status'], 'building')
        # all spec/done but none building → spec
        spec = self._norm([{'id': 'p', 'phases': [
            {'id': 'a', 'lifecycle_state': 'spec'},
            {'id': 'b', 'lifecycle_state': 'done'}]}])
        self.assertEqual(ps.build_pipeline(spec, NOW)[0]['status'], 'spec')
        # any brainstorm → brainstorm
        brainstorm = self._norm([{'id': 'p', 'phases': [
            {'id': 'a', 'lifecycle_state': 'brainstorm'},
            {'id': 'b', 'lifecycle_state': 'spec'}]}])
        self.assertEqual(ps.build_pipeline(brainstorm, NOW)[0]['status'],
                         'brainstorm')

    def test_one_off_flag_passes_through(self):
        projects = self._norm([
            {'id': 'single', 'phases': [{'id': 'p1'}]},
            {'id': 'multi', 'phases': [{'id': 'p1'}, {'id': 'p2'}]},
        ])
        by_id = {p['id']: p for p in ps.build_pipeline(projects, NOW)}
        self.assertTrue(by_id['single']['one_off'])
        self.assertFalse(by_id['multi']['one_off'])

    def test_safe_on_junk_projects(self):
        # a non-dict project entry is skipped, never raises
        out = ps.build_pipeline(['junk', {'id': 'ok', 'state': 'active',
                                          'phases': []}], NOW)
        self.assertEqual([p['id'] for p in out], ['ok'])


class SlugifyTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(ps.slugify('Hello World!'), 'hello-world')
        self.assertEqual(ps.slugify('  A  B  '), 'a-b')
        self.assertEqual(ps.slugify(''), 'untitled')
        self.assertEqual(ps.slugify('***'), 'untitled')


if __name__ == '__main__':
    unittest.main()
