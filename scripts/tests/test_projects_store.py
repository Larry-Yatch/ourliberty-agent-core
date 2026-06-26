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
from datetime import datetime, timedelta, timezone
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

    def test_brainstorm_prefill_projects_to_flat_card_shape(self):
        # The author STORES a nested brainstorm dict (8-section draft dict +
        # fork strings + handoff); the card consumes a FLAT shape. build_pipeline
        # must project the stored fields into draft (headed string) + decisions
        # (objects) + spec_target_path so step1 (#611) and step2 (#72) meet.
        projects = self._norm([{
            'id': 'proj-bs', 'state': 'active',
            'phases': [{
                'id': 'ph-bs', 'title': 'Where are we', 'lifecycle_state': 'brainstorm',
                'brainstorm': {
                    'is_ai_draft': True,
                    'draft': {
                        'end_state': 'When done, the board shows where we are.',
                        'why_now': 'The blank page is the last friction.',
                        'scope': '',  # empty section is skipped
                        'breakdown': 'One step: wire the card.',
                    },
                    'decisions': ['Scope: cut anything?', 'Risk: how much polish?'],
                    'decisions_overflow': True,
                    'handoff': {
                        'spec_target_path': 'agents/beacon/specs/ph-bs.md',
                        'target_repo': 'ourliberty-agent-core',
                        'directive': 'Author the spec.',
                    },
                },
            }],
        }])
        card = ps.build_pipeline(projects, NOW)[0]['phases'][0]
        # draft: flattened to one headed string, in canonical order, empties skipped
        self.assertIsInstance(card['draft'], str)
        self.assertIn('Desired end state\nWhen done, the board shows where we are.',
                      card['draft'])
        self.assertIn('Breakdown\nOne step: wire the card.', card['draft'])
        self.assertNotIn('Scope & non-goals', card['draft'])  # empty section dropped
        self.assertLess(card['draft'].index('Desired end state'),
                        card['draft'].index('Why now'))  # canonical order
        # decisions: fork strings → {id,title,decision} objects + overflow note
        titles = [d['title'] for d in card['decisions']]
        self.assertIn('Scope: cut anything?', titles)
        self.assertTrue(all(d['decision'] == '' and d['id'] for d in card['decisions']))
        self.assertTrue(any('surface' in d['title'] for d in card['decisions']),
                        'decisions_overflow must add a trailing note item')
        # spec_target_path lifted to the top level the card reads
        self.assertEqual(card['spec_target_path'], 'agents/beacon/specs/ph-bs.md')
        # the nested brainstorm dict is NOT what the card reads — flat is the contract
        self.assertNotIn('brainstorm', card)

    def test_brainstorm_absent_or_thin_no_prefill_keys(self):
        # Graceful no-prefill: a phase with no brainstorm (or an all-empty draft)
        # carries none of the prefill keys, so the card simply no-prefills.
        projects = self._norm([{
            'id': 'proj-none', 'state': 'active',
            'phases': [
                {'id': 'ph-plain', 'lifecycle_state': 'brainstorm'},
                {'id': 'ph-thin', 'lifecycle_state': 'brainstorm',
                 'brainstorm': {'draft': {'end_state': '   '}, 'decisions': []}},
            ],
        }])
        cards = {c['id']: c for c in ps.build_pipeline(projects, NOW)[0]['phases']}
        for cid in ('ph-plain', 'ph-thin'):
            self.assertNotIn('draft', cards[cid])
            self.assertNotIn('decisions', cards[cid])
            self.assertNotIn('spec_target_path', cards[cid])

    def test_brainstorm_draft_sections_match_author_key_set(self):
        # Drift guard: the card-view section list is duplicated from the author's
        # BRAINSTORM_SECTION_KEYS (kept local so the read surface doesn't import
        # the author's heavy narrator chain). If the author adds/renames a
        # section and this list isn't updated, that section's prose would silently
        # never reach the card. Pin the two key SETS in lockstep here.
        import projects_brainstorm_author as author
        self.assertEqual(
            tuple(k for k, _ in ps._BRAINSTORM_DRAFT_SECTIONS),
            author.BRAINSTORM_SECTION_KEYS,
        )

    def test_brainstorm_draft_already_string_passes_through(self):
        # Defensive: a draft stored as a plain string (not the 8-section dict) is
        # surfaced trimmed rather than dropped.
        projects = self._norm([{
            'id': 'proj-s', 'state': 'active',
            'phases': [{'id': 'ph-s', 'lifecycle_state': 'brainstorm',
                        'brainstorm': {'draft': '  a plain draft  '}}],
        }])
        card = ps.build_pipeline(projects, NOW)[0]['phases'][0]
        self.assertEqual(card['draft'], 'a plain draft')

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


class SequenceRollupTest(unittest.TestCase):
    """projects-v3 sequence-rollup-done-flip: a phase whose work is a build
    SEQUENCE has no PR of its own, so the board rolls the sequence's completion
    up to the parent. `rollup_lifecycle_from_sequence` is the pure mapping;
    `build_pipeline(..., sequence_status_by_id=...)` is the read-time derive."""

    def _norm(self, projects):
        reg, _ = ps.normalize_registry({'projects': projects}, now=NOW)
        return reg['projects']

    # ---- the pure mapping ----
    def test_rollup_complete_flips_to_done(self):
        self.assertEqual(
            ps.rollup_lifecycle_from_sequence('spec', 'complete'), 'done')
        self.assertEqual(
            ps.rollup_lifecycle_from_sequence('building', 'complete'), 'done')

    def test_rollup_active_or_paused_reads_building(self):
        self.assertEqual(
            ps.rollup_lifecycle_from_sequence('spec', 'active'), 'building')
        self.assertEqual(
            ps.rollup_lifecycle_from_sequence('brainstorm', 'paused'), 'building')

    def test_rollup_pending_or_unknown_or_missing_degrades_to_stored(self):
        for status in ('pending', 'failed', 'archived', 'bogus', None):
            self.assertEqual(
                ps.rollup_lifecycle_from_sequence('spec', status), 'spec',
                f'{status!r} must degrade to the stored state')

    def test_rollup_is_forward_only_never_regresses(self):
        # a stored `done` (the P4 closeout writeback already flipped it) must
        # NOT be dragged back to building by a stale/active linked sequence.
        self.assertEqual(
            ps.rollup_lifecycle_from_sequence('done', 'active'), 'done')
        self.assertEqual(
            ps.rollup_lifecycle_from_sequence('done', 'paused'), 'done')

    # ---- the read-time pipeline derive ----
    def _proj_with_seq(self, lifecycle, seq_ref='launch-ph'):
        return self._norm([{
            'id': 'proj', 'state': 'active',
            'phases': [{'id': 'ph', 'lifecycle_state': lifecycle,
                        'sequence_ref': seq_ref}],
        }])

    def test_pipeline_complete_sequence_reads_done(self):
        projects = self._proj_with_seq('building')
        out = ps.build_pipeline(projects, NOW, {'launch-ph': 'complete'})
        proj = out[0]
        self.assertEqual(proj['phases'][0]['lifecycle_state'], 'done')
        # the coarse project header agrees with the flipped phase (P5 rollup)
        self.assertEqual(proj['status'], 'done')

    def test_pipeline_active_sequence_reads_building(self):
        projects = self._proj_with_seq('spec')
        out = ps.build_pipeline(projects, NOW, {'launch-ph': 'active'})
        self.assertEqual(out[0]['phases'][0]['lifecycle_state'], 'building')
        self.assertEqual(out[0]['status'], 'building')

    def test_pipeline_missing_sequence_degrades_to_stored(self):
        # phase carries a sequence_ref but no live sequence file → empty/absent
        # status map: render the stored lane, no error, no false Done.
        projects = self._proj_with_seq('spec')
        out = ps.build_pipeline(projects, NOW, {})  # empty map (no live file)
        self.assertEqual(out[0]['phases'][0]['lifecycle_state'], 'spec')
        # and with no map at all (pre-rollup callers) behaviour is identical
        out2 = ps.build_pipeline(projects, NOW)
        self.assertEqual(out2[0]['phases'][0]['lifecycle_state'], 'spec')

    def test_pipeline_phase_without_sequence_ref_untouched(self):
        projects = self._norm([{
            'id': 'proj', 'state': 'active',
            'phases': [{'id': 'ph', 'lifecycle_state': 'building'}],
        }])
        out = ps.build_pipeline(projects, NOW, {'launch-ph': 'complete'})
        self.assertEqual(out[0]['phases'][0]['lifecycle_state'], 'building')

    def test_pipeline_no_map_is_byte_identical_to_pre_rollup(self):
        projects = self._proj_with_seq('building')
        self.assertEqual(
            ps.build_pipeline(projects, NOW),
            ps.build_pipeline(projects, NOW, None),
        )


class SlugifyTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(ps.slugify('Hello World!'), 'hello-world')
        self.assertEqual(ps.slugify('  A  B  '), 'a-b')
        self.assertEqual(ps.slugify(''), 'untitled')
        self.assertEqual(ps.slugify('***'), 'untitled')


class NewSinglePhaseProjectTest(unittest.TestCase):
    """The Promote builder (p3-promote-endpoint): a funnel item becomes a NEW
    single-phase project at Brainstorm, state active, one_off=True."""

    def test_shape_and_defaults(self):
        proj = ps.new_single_phase_project(title='Aging idea', now=NOW)
        self.assertEqual(proj['id'], 'aging-idea')        # slugified title
        self.assertEqual(proj['title'], 'Aging idea')
        self.assertEqual(proj['state'], 'active')         # shows in pipeline now
        self.assertTrue(proj['one_off'])
        self.assertIsNone(proj['north_star_ref'])
        self.assertIsNone(proj['repo'])
        self.assertNotIn('promoted_from', proj)           # absent when not given
        self.assertEqual(len(proj['phases']), 1)
        phase = proj['phases'][0]
        self.assertEqual(phase['id'], 'aging-idea')       # defaults to project id
        self.assertEqual(phase['lifecycle_state'], 'brainstorm')   # enters at Brainstorm
        self.assertEqual(phase['order'], 0)
        self.assertEqual(phase['desired_end_state'], '')
        self.assertIsNone(phase['spec_ref'])
        self.assertIsNone(phase['sequence_ref'])

    def test_brief_seeds_phase_desired_end_state(self):
        proj = ps.new_single_phase_project(
            title='Ship X', desired_end_state='so the loop closes', now=NOW)
        self.assertEqual(proj['phases'][0]['desired_end_state'], 'so the loop closes')

    def test_explicit_ids_and_provenance_carried(self):
        pf = {'kind': 'capture', 'capture_id': 'cap-1'}
        proj = ps.new_single_phase_project(
            title='T', project_id='proj-7', phase_id='phase-7',
            north_star_ref='docs/ns.md', repo='ourliberty-dashboard',
            promoted_from=pf, now=NOW)
        self.assertEqual(proj['id'], 'proj-7')
        self.assertEqual(proj['phases'][0]['id'], 'phase-7')
        self.assertEqual(proj['north_star_ref'], 'docs/ns.md')
        self.assertEqual(proj['repo'], 'ourliberty-dashboard')
        self.assertEqual(proj['promoted_from'], pf)
        # a COPY, not the caller's dict (no aliasing back into the registry)
        self.assertIsNot(proj['promoted_from'], pf)

    def test_normalize_registry_is_a_noop(self):
        # The builder returns an already-normalized project: feeding it back
        # through normalize_registry must produce an equal registry (so the
        # single-committer healer sees no spurious delta).
        proj = ps.new_single_phase_project(
            title='Aging idea', desired_end_state='why',
            promoted_from={'kind': 'mission', 'mission_id': 'm-1'}, now=NOW)
        reg, dropped = ps.normalize_registry({'projects': [proj]}, now=NOW)
        self.assertEqual(dropped, [])
        self.assertEqual(reg['projects'][0], proj)

    def test_empty_title_falls_back_to_untitled_slug(self):
        proj = ps.new_single_phase_project(title='', now=NOW)
        self.assertEqual(proj['id'], 'untitled')
        self.assertEqual(proj['title'], 'untitled')

    def test_mapped_lifecycle_and_refs(self):
        # The migration passes a mapped lifecycle + the mission's spec/sequence
        # refs so the migrated phase reflects reality, not a fresh Brainstorm.
        proj = ps.new_single_phase_project(
            title='In-flight work', lifecycle_state='building',
            spec_ref='agents/beacon/specs/x.md', sequence_ref='pulse-upgrade-001',
            now=NOW)
        ph = proj['phases'][0]
        self.assertEqual(ph['lifecycle_state'], 'building')
        self.assertEqual(ph['spec_ref'], 'agents/beacon/specs/x.md')
        self.assertEqual(ph['sequence_ref'], 'pulse-upgrade-001')

    def test_invalid_lifecycle_state_degrades_to_default(self):
        proj = ps.new_single_phase_project(title='X', lifecycle_state='bogus', now=NOW)
        self.assertEqual(proj['phases'][0]['lifecycle_state'], 'brainstorm')


class MirrorMissionsAsProjectsTest(unittest.TestCase):
    """retire-missions-kanban: mirror each ACTIVE (non-shipped) mission as a
    single-phase pipeline project. Idempotent + reversible; never writes missions."""

    def _mission(self, mid, phase='in_flight', **extra):
        m = {'id': mid, 'name': mid.replace('-', ' ').title(), 'phase': phase,
             'brief': f'brief for {mid}', 'task_ids': [], 'repo': 'ourliberty-agent-core'}
        m.update(extra)
        return m

    def test_mirrors_active_non_shipped_with_mapped_lifecycle(self):
        reg = {'projects': []}
        minted = ps.mirror_missions_as_projects(reg, [
            self._mission('draft-x', phase='drafting'),
            self._mission('ready-x', phase='ready'),
            self._mission('flight-x', phase='in_flight'),
        ], now=NOW)
        self.assertEqual(sorted(minted), ['draft-x', 'flight-x', 'ready-x'])
        by_id = {p['id']: p for p in reg['projects']}
        self.assertEqual(by_id['draft-x']['phases'][0]['lifecycle_state'], 'brainstorm')
        self.assertEqual(by_id['ready-x']['phases'][0]['lifecycle_state'], 'spec')
        self.assertEqual(by_id['flight-x']['phases'][0]['lifecycle_state'], 'building')
        self.assertEqual(by_id['flight-x']['promoted_from'],
                         {'kind': 'mission', 'mission_id': 'flight-x'})
        self.assertEqual(by_id['flight-x']['phases'][0]['desired_end_state'],
                         'brief for flight-x')

    def test_skips_shipped_deferred_proposed_archived_acknowledged(self):
        reg = {'projects': []}
        minted = ps.mirror_missions_as_projects(reg, [
            self._mission('shipped-x', phase='shipped'),
            self._mission('deferred-x', phase='deferred'),
            self._mission('proposed-x', phase='proposed'),
            self._mission('archived-x', phase='drafting', archived=True),
            self._mission('ackd-x', phase='in_flight', acknowledged=True),
        ], now=NOW)
        self.assertEqual(minted, [])
        self.assertEqual(reg['projects'], [])

    def test_idempotent_skips_already_mirrored_by_id(self):
        reg = {'projects': []}
        m = [self._mission('flight-x', phase='in_flight')]
        ps.mirror_missions_as_projects(reg, m, now=NOW)
        self.assertEqual(ps.mirror_missions_as_projects(reg, m, now=NOW), [])
        self.assertEqual(len(reg['projects']), 1)

    def test_skips_mission_already_promoted_by_back_ref(self):
        reg = {'projects': [{'id': 'other-id', 'state': 'active',
                             'promoted_from': {'kind': 'mission', 'mission_id': 'flight-x'},
                             'phases': [{'id': 'p'}]}]}
        self.assertEqual(ps.mirror_missions_as_projects(
            reg, [self._mission('flight-x', phase='in_flight')], now=NOW), [])

    def test_dropped_project_never_resurrects(self):
        # An ARCHIVED project for the mission (a drop-back) keeps it dropped.
        reg = {'projects': [{'id': 'flight-x', 'state': 'archived',
                             'phases': [{'id': 'flight-x'}]}]}
        self.assertEqual(ps.mirror_missions_as_projects(
            reg, [self._mission('flight-x', phase='in_flight')], now=NOW), [])

    def test_derives_sequence_ref_and_spec_ref(self):
        reg = {'projects': []}
        ps.mirror_missions_as_projects(reg, [self._mission(
            'pulse-cycle-upgrade', phase='in_flight',
            spec_docs=['agents/beacon/specs/pulse.md'],
            task_ids=['alpha-1', 'seq-pulse-upgrade-001-step-alpha-1'])], now=NOW)
        ph = reg['projects'][0]['phases'][0]
        self.assertEqual(ph['sequence_ref'], 'pulse-upgrade-001')
        self.assertEqual(ph['spec_ref'], 'agents/beacon/specs/pulse.md')

    def test_no_sequence_step_leaves_null_ref(self):
        reg = {'projects': []}
        ps.mirror_missions_as_projects(reg, [self._mission(
            'flight-x', phase='in_flight', task_ids=['a', 'b'])], now=NOW)
        self.assertIsNone(reg['projects'][0]['phases'][0]['sequence_ref'])

    def test_safe_on_junk_inputs(self):
        self.assertEqual(ps.mirror_missions_as_projects({}, 'not-a-list'), [])
        self.assertEqual(ps.mirror_missions_as_projects('not-a-dict', []), [])
        reg = {'projects': []}
        ps.mirror_missions_as_projects(
            reg, [None, 42, {'id': 'flight-x', 'phase': 'in_flight'}], now=NOW)
        self.assertEqual([p['id'] for p in reg['projects']], ['flight-x'])

    def test_normalize_is_a_noop_on_mirrored(self):
        # A mirrored project is already normalized → the committer sees no spurious
        # delta beyond the mint itself.
        reg = {'projects': []}
        ps.mirror_missions_as_projects(reg, [self._mission(
            'flight-x', phase='in_flight', task_ids=['seq-s-step-a'],
            spec_docs=['s.md'])], now=NOW)
        norm, dropped = ps.normalize_registry(reg, now=NOW)
        self.assertEqual(dropped, [])
        self.assertEqual(norm['projects'][0], reg['projects'][0])


class EditPhaseBrainstormTest(unittest.TestCase):
    """projects-v3 P6.1 — the pure Larry-edit helper edit_phase_brainstorm."""

    def _phase(self, **over):
        ph = {
            'id': 'ph', 'lifecycle_state': 'brainstorm',
            'brainstorm': {
                'is_ai_draft': True,
                'draft': {'end_state': 'authored'},
                'decisions': ['a fork'],
                'decisions_overflow': True,
                'handoff': {'spec_target_path': 'agents/beacon/specs/ph.md'},
            },
            'brainstorm_provenance': {'by': 'beacon', 'model': 'claude',
                                      'from_state': 'brainstorm'},
        }
        ph.update(over)
        return ph

    def test_edit_draft_persists_string_and_marks_larry(self):
        ph = self._phase()
        self.assertTrue(ps.edit_phase_brainstorm(ph, draft='my own words', now=NOW))
        self.assertEqual(ph['brainstorm']['draft'], 'my own words')
        self.assertIs(ph['brainstorm']['is_ai_draft'], False)
        self.assertEqual(ph['brainstorm_provenance']['by'], 'larry')
        self.assertEqual(ph['brainstorm_provenance']['from_state'], 'brainstorm')
        self.assertIn('edited_at', ph['brainstorm_provenance'])

    def test_edited_string_draft_round_trips_to_the_card(self):
        ph = self._phase()
        ps.edit_phase_brainstorm(ph, draft='my own words', now=NOW)
        card = ps._phase_card(ph)
        self.assertEqual(card['draft'], 'my own words')

    def test_edit_decisions_clears_overflow_and_cleans(self):
        ph = self._phase()
        self.assertTrue(ps.edit_phase_brainstorm(
            ph, decisions=['  keep this ', '', 42, 'and this'], now=NOW))
        self.assertEqual(ph['brainstorm']['decisions'], ['keep this', 'and this'])
        self.assertIs(ph['brainstorm']['decisions_overflow'], False)

    def test_no_fields_is_noop(self):
        ph = self._phase()
        self.assertFalse(ps.edit_phase_brainstorm(ph, now=NOW))

    def test_idempotent_resave(self):
        ph = self._phase()
        self.assertTrue(ps.edit_phase_brainstorm(ph, draft='same', now=NOW))
        self.assertFalse(ps.edit_phase_brainstorm(ph, draft='same', now=NOW))

    def test_safe_on_junk(self):
        self.assertFalse(ps.edit_phase_brainstorm('nope', draft='x'))

    def test_no_prior_brainstorm_creates_minimal(self):
        ph = {'id': 'ph', 'lifecycle_state': 'brainstorm'}
        self.assertTrue(ps.edit_phase_brainstorm(ph, draft='seed', now=NOW))
        self.assertEqual(ph['brainstorm']['draft'], 'seed')
        self.assertIs(ph['brainstorm']['is_ai_draft'], False)


class RetireCompletedProjectsTest(unittest.TestCase):
    """Project-level GC reconciliation (projects-stale-gc-archive-completed):
    an ACTIVE project with every phase done retires off the board; anything with
    a non-done or zero phases, and anything already terminal, is left untouched."""

    # Default phase done-stamp: well past the 48h window vs NOW, so the existing
    # "retires" cases still retire under the default window. The window-specific
    # cases pass a custom ``done_at`` (a recent datetime, None, or junk).
    OLD = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def _project(cls, pid, state, phase_states, *, done_at=None):
        """``done_at`` stamps every phase's ``updated_at``: a datetime (isoformatted),
        a raw string (e.g. junk for the unparseable case), or None to OMIT the key
        (the missing-stamp case). Defaults to ``OLD`` — past the default window."""
        stamp = cls.OLD if done_at is None else done_at
        phases = []
        for i, s in enumerate(phase_states):
            ph = {'id': f'{pid}-ph{i}', 'lifecycle_state': s}
            if done_at is not False:  # False ⇒ omit updated_at entirely
                ph['updated_at'] = stamp.isoformat() if hasattr(stamp, 'isoformat') else stamp
            phases.append(ph)
        return {'id': pid, 'state': state, 'phases': phases}

    def test_all_phases_done_active_project_retires(self):
        reg = {'projects': [self._project('p', 'active', ['done', 'done'])]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, ['p'])
        proj = reg['projects'][0]
        self.assertEqual(proj['state'], 'retired')  # retired, NOT archived
        # audit trail: prior_state + retired_at + retired_by + window provenance.
        self.assertEqual(proj['gc']['prior_state'], 'active')
        self.assertEqual(proj['gc']['retired_by'], ps.GC_RETIRED_BY)
        self.assertEqual(proj['gc']['retired_at'], NOW.isoformat())
        self.assertEqual(proj['gc']['done_at'], self.OLD.isoformat())
        self.assertEqual(proj['gc']['window_sec'], ps.DONE_RETIRE_VISIBILITY_SEC)
        self.assertEqual(proj['updated_at'], NOW.isoformat())

    def test_done_inside_window_keeps_project_active(self):
        # Reached Done 1h ago — inside the 48h window → linger as a visible win.
        recent = NOW - timedelta(hours=1)
        reg = {'projects': [self._project('p', 'active', ['done'], done_at=recent)]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'active')
        self.assertNotIn('gc', reg['projects'][0])

    def test_done_past_window_retires(self):
        old = NOW - timedelta(hours=49)  # just past the default 48h window
        reg = {'projects': [self._project('p', 'active', ['done'], done_at=old)]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, ['p'])
        self.assertEqual(reg['projects'][0]['gc']['done_at'], old.isoformat())

    def test_done_at_window_boundary_retires(self):
        # age == window: NOT inside the window (strict `<`), so it retires.
        edge = NOW - timedelta(seconds=ps.DONE_RETIRE_VISIBILITY_SEC)
        reg = {'projects': [self._project('p', 'active', ['done'], done_at=edge)]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, ['p'])

    def test_zero_window_retires_immediately(self):
        recent = NOW - timedelta(seconds=1)
        reg = {'projects': [self._project('p', 'active', ['done'], done_at=recent)]}
        _, retired = ps.retire_completed_projects(reg, now=NOW, min_done_age_sec=0)
        self.assertEqual(retired, ['p'])
        self.assertEqual(reg['projects'][0]['gc']['window_sec'], 0)

    def test_unparseable_done_at_keeps_project_active(self):
        reg = {'projects': [self._project('p', 'active', ['done'], done_at='not-a-date')]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'active')

    def test_missing_done_at_keeps_project_active(self):
        # done_at=False ⇒ phases carry no updated_at at all (un-normalized input).
        reg = {'projects': [self._project('p', 'active', ['done'], done_at=False)]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'active')

    def test_done_at_is_max_over_phases(self):
        # Multi-phase: done_at = the LATEST phase stamp. Latest = 1h ago (inside
        # window) ⇒ kept, even though another phase finished long ago.
        proj = self._project('p', 'active', ['done', 'done'])
        proj['phases'][0]['updated_at'] = (NOW - timedelta(days=10)).isoformat()
        proj['phases'][1]['updated_at'] = (NOW - timedelta(hours=1)).isoformat()
        reg = {'projects': [proj]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'active')

    def test_brainstorm_phase_keeps_project_active(self):
        # The slice-2b shape: a single brainstorm phase, no closeout → MUST stay.
        reg = {'projects': [self._project('slice-2b', 'active', ['brainstorm'])]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'active')
        self.assertNotIn('gc', reg['projects'][0])

    def test_partial_done_keeps_project_active(self):
        reg = {'projects': [self._project('p', 'active', ['done', 'building'])]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'active')

    def test_zero_phase_project_untouched(self):
        reg = {'projects': [self._project('empty', 'active', [])]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'active')
        self.assertNotIn('gc', reg['projects'][0])

    def test_already_archived_is_noop(self):
        reg = {'projects': [self._project('p', 'archived', ['done'])]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'archived')

    def test_already_retired_is_noop(self):
        reg = {'projects': [self._project('p', 'retired', ['done'])]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, [])
        self.assertEqual(reg['projects'][0]['state'], 'retired')

    def test_idempotent_second_pass_retires_nothing(self):
        reg = {'projects': [self._project('p', 'active', ['done'])]}
        ps.retire_completed_projects(reg, now=NOW)
        _, retired_again = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired_again, [])
        self.assertEqual(reg['projects'][0]['state'], 'retired')

    def test_mixed_registry_retires_only_completed_active(self):
        reg = {'projects': [
            self._project('done-active', 'active', ['done']),
            self._project('brainstorm-active', 'active', ['brainstorm']),
            self._project('done-archived', 'archived', ['done']),
        ]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, ['done-active'])
        states = {p['id']: p['state'] for p in reg['projects']}
        self.assertEqual(states, {
            'done-active': 'retired',
            'brainstorm-active': 'active',
            'done-archived': 'archived',
        })

    def test_fail_safe_on_malformed_project(self):
        reg = {'projects': ['junk', None, self._project('p', 'active', ['done'])]}
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, ['p'])  # bad entries skipped, never raises

    def test_non_dict_registry_safe(self):
        reg, retired = ps.retire_completed_projects(None, now=NOW)
        self.assertIsNone(reg)
        self.assertEqual(retired, [])

    def test_returns_same_registry_object(self):
        reg = {'projects': [self._project('p', 'active', ['done'])]}
        out, _ = ps.retire_completed_projects(reg, now=NOW)
        self.assertIs(out, reg)  # mutated in place




class ReconcileTerminalMissionProjectsTest(unittest.TestCase):
    """reconcile_terminal_mission_projects: the mirror<->ship-race backstop. An
    ACTIVE project mirrored from a mission that has SINCE shipped/retired gets its
    phases Done-stamped (at the mission's OWN terminal time) so the GC retire can
    sweep it. Closes the gap where a project born `building` after its build already
    merged never gets a building->done event."""

    SHIP = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def _project(cls, pid, mission_id, lifecycle="building", state="active"):
        return {
            "id": pid, "state": state,
            "promoted_from": {"kind": "mission", "mission_id": mission_id},
            "phases": [{"id": pid, "lifecycle_state": lifecycle,
                        "updated_at": cls.SHIP.isoformat()}],
        }

    @staticmethod
    def _mission(mid, phase="shipped", **extra):
        m = {"id": mid, "phase": phase}
        m.update(extra)
        return m

    def test_shipped_mission_done_stamps_at_ship_time(self):
        reg = {"projects": [self._project("m1", "m1")]}
        out = ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "shipped", shipped_at=self.SHIP.isoformat())],
            now=NOW)
        self.assertEqual(out, ["m1"])
        ph = reg["projects"][0]["phases"][0]
        self.assertEqual(ph["lifecycle_state"], "done")
        # updated_at carries the mission's TRUE ship time, not NOW.
        self.assertEqual(ph["updated_at"], self.SHIP.isoformat())
        self.assertEqual(reg["projects"][0]["updated_at"], NOW.isoformat())

    def test_retired_mission_is_terminal_too(self):
        reg = {"projects": [self._project("m1", "m1")]}
        out = ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "retired", retired_at=self.SHIP.isoformat())],
            now=NOW)
        self.assertEqual(out, ["m1"])
        self.assertEqual(reg["projects"][0]["phases"][0]["lifecycle_state"], "done")

    def test_then_retire_sweeps_it_in_same_window(self):
        # End-to-end: reconcile Done-stamps at SHIP (well past 48h vs NOW), so the
        # existing retire then clears it off the board in the same pipeline.
        reg = {"projects": [self._project("m1", "m1")]}
        ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "shipped", shipped_at=self.SHIP.isoformat())],
            now=NOW)
        _, retired = ps.retire_completed_projects(reg, now=NOW)
        self.assertEqual(retired, ["m1"])
        self.assertEqual(reg["projects"][0]["state"], "retired")
        self.assertEqual(reg["projects"][0]["gc"]["done_at"], self.SHIP.isoformat())

    def test_in_flight_mission_left_untouched(self):
        reg = {"projects": [self._project("m1", "m1")]}
        out = ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "in_flight")], now=NOW)
        self.assertEqual(out, [])
        self.assertEqual(reg["projects"][0]["phases"][0]["lifecycle_state"], "building")

    def test_already_done_project_skipped(self):
        reg = {"projects": [self._project("m1", "m1", lifecycle="done")]}
        out = ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "shipped", shipped_at=self.SHIP.isoformat())],
            now=NOW)
        self.assertEqual(out, [])  # all-done already -> the GC owns it

    def test_archived_project_skipped(self):
        reg = {"projects": [self._project("m1", "m1", state="archived")]}
        out = ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "shipped", shipped_at=self.SHIP.isoformat())],
            now=NOW)
        self.assertEqual(out, [])

    def test_non_mission_promotion_skipped(self):
        proj = self._project("m1", "m1")
        proj["promoted_from"] = {"kind": "capture", "capture_id": "c1"}
        reg = {"projects": [proj]}
        out = ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "shipped", shipped_at=self.SHIP.isoformat())],
            now=NOW)
        self.assertEqual(out, [])

    def test_unparseable_ship_stamp_falls_back_to_now(self):
        reg = {"projects": [self._project("m1", "m1")]}
        out = ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "shipped", shipped_at="not-a-date")], now=NOW)
        self.assertEqual(out, ["m1"])
        self.assertEqual(reg["projects"][0]["phases"][0]["updated_at"], NOW.isoformat())

    def test_junk_inputs_safe(self):
        self.assertEqual(ps.reconcile_terminal_mission_projects({}, "not-a-list"), [])
        self.assertEqual(ps.reconcile_terminal_mission_projects("nope", []), [])
        self.assertEqual(
            ps.reconcile_terminal_mission_projects(
                {"projects": ["junk", None, 3]},
                [self._mission("m1", "shipped")], now=NOW),
            [])

    def test_multi_phase_all_stamped_no_short_circuit(self):
        proj = self._project("m1", "m1")
        proj["phases"].append({"id": "m1-ph2", "lifecycle_state": "building",
                               "updated_at": self.SHIP.isoformat()})
        reg = {"projects": [proj]}
        out = ps.reconcile_terminal_mission_projects(
            reg, [self._mission("m1", "shipped", shipped_at=self.SHIP.isoformat())],
            now=NOW)
        self.assertEqual(out, ["m1"])
        self.assertTrue(all(ph["lifecycle_state"] == "done"
                            for ph in reg["projects"][0]["phases"]))


if __name__ == '__main__':
    unittest.main()
