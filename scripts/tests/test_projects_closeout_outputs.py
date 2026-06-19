#!/usr/bin/env python3
"""Tests for projects-v3 P4 step p4-closeout-outputs.

When a phase reaches Done, the closeout doesn't just author its card — it feeds
the next phase. This step adds two outputs on top of p4-closeout-author:

  (a) the SEQUENCE_COMPLETE DM becomes a closeout handoff — "Phase X done:
      [summary]. Next up: Phase Y — ready for you to brainstorm" — and flags
      when Larry must DECIDE (done-gate missed / build diverged from spec / a
      risky follow-up exists);
  (b) the closeout's loose ends are dropped into the funnel's Suggested lane as
      `source='closeout'` cards, DEDUPED + IDEMPOTENT.

Mirror-review focus:
  * DM FLAGS DECISION-NEEDED CASES — compute_needs_decision_flags + the DM
    renderer surface the three trigger conditions and stay quiet for a clean
    completion.
  * FOLLOW-UPS DEDUPED INTO SUGGESTED — drop_follow_ups_to_funnel skips
    within-batch dups, already-registered ids, and in-flight queue files; cards
    carry proposed_by='closeout' (the suggested lane).
  * IDEMPOTENT — a re-run on the same closeout drops NO duplicate cards.

Effectful edges (funnel drops) are confined to a tmp queue dir; no test touches
the live missions.json, Supabase, gh, or claude (use_llm=False everywhere). Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_projects_closeout_outputs
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

import projects_closeout_author as author   # noqa: E402
import suggest_funnel_card as suggest        # noqa: E402
import outbox_notifier as on                 # noqa: E402
from test_isolation_guard import TestIsolationBreach  # noqa: E402

_NOW = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)


def _phase(pid, *, title=None, state='building', order=0):
    return {
        'id': pid,
        'title': title if title is not None else pid,
        'desired_end_state': 'the thing is done and live',
        'lifecycle_state': state,
        'order': order,
        'spec_ref': None,
        'sequence_ref': None,
    }


def _project(pid, phases):
    return {'id': pid, 'title': pid, 'repo': None, 'phases': phases}


# --------------------------------------------------------------------------- #
# analysis normalization — the deterministic shape + empty fallback
# --------------------------------------------------------------------------- #
class NormalizeAnalysisTests(unittest.TestCase):
    def test_none_yields_clean_empty_default(self):
        out = author._normalize_analysis(None)
        self.assertEqual(
            out, {'follow_ups': [], 'diverged': False, 'diverged_note': None})

    def test_objects_and_bare_strings_both_normalize(self):
        raw = {
            'follow_ups': [
                {'text': 'wire the retry path', 'risky': True},
                'document the new flag',          # bare string → risky False
                {'summary': 'add a backfill job'},  # alt key, no risky
                {'text': '   '},                  # empty → dropped
                42,                               # junk → dropped
            ],
            'diverged': True,
            'diverged_note': 'shipped a smaller cache than specced',
        }
        out = author._normalize_analysis(raw)
        self.assertEqual(
            out['follow_ups'],
            [
                {'text': 'wire the retry path', 'risky': True},
                {'text': 'document the new flag', 'risky': False},
                {'text': 'add a backfill job', 'risky': False},
            ])
        self.assertTrue(out['diverged'])
        self.assertEqual(
            out['diverged_note'], 'shipped a smaller cache than specced')

    def test_diverged_note_dropped_when_not_diverged(self):
        out = author._normalize_analysis(
            {'diverged': False, 'diverged_note': 'stray note'})
        self.assertIsNone(out['diverged_note'])

    def test_follow_ups_capped(self):
        raw = {'follow_ups': [{'text': f'item {i}'} for i in range(50)]}
        out = author._normalize_analysis(raw)
        self.assertEqual(len(out['follow_ups']), author._MAX_FOLLOW_UPS)

    def test_use_llm_false_is_empty_analysis(self):
        out = author.author_closeout_analysis(
            _phase('p4'), _project('p', []), {}, use_llm=False)
        self.assertEqual(out['follow_ups'], [])
        self.assertFalse(out['diverged'])

    def test_use_llm_true_under_test_is_guarded(self):
        with self.assertRaises(TestIsolationBreach):
            author.author_closeout_analysis(
                _phase('p4'), _project('p', []), {}, use_llm=True)


# --------------------------------------------------------------------------- #
# needs-decision flags — the DM's "needs your call" triggers
# --------------------------------------------------------------------------- #
class NeedsDecisionFlagsTests(unittest.TestCase):
    def _empty(self):
        return {'follow_ups': [], 'diverged': False, 'diverged_note': None}

    def test_clean_completion_has_no_flags(self):
        self.assertEqual(
            author.compute_needs_decision_flags(True, self._empty()), [])

    def test_done_gate_missed_flags(self):
        flags = author.compute_needs_decision_flags(False, self._empty())
        self.assertEqual(len(flags), 1)
        self.assertIn('done-gate', flags[0])

    def test_diverged_flags_with_note(self):
        analysis = {'follow_ups': [], 'diverged': True,
                    'diverged_note': 'used a cron instead of a webhook'}
        flags = author.compute_needs_decision_flags(True, analysis)
        self.assertEqual(len(flags), 1)
        self.assertIn('diverged', flags[0])
        self.assertIn('used a cron instead of a webhook', flags[0])

    def test_diverged_flags_without_note(self):
        flags = author.compute_needs_decision_flags(
            True, {'follow_ups': [], 'diverged': True, 'diverged_note': None})
        self.assertEqual(len(flags), 1)
        self.assertIn('diverged', flags[0])

    def test_risky_follow_up_flags_and_names_it(self):
        analysis = {
            'follow_ups': [
                {'text': 'tidy the README', 'risky': False},
                {'text': 'rotate the leaked token', 'risky': True},
            ],
            'diverged': False, 'diverged_note': None,
        }
        flags = author.compute_needs_decision_flags(True, analysis)
        self.assertEqual(len(flags), 1)
        self.assertIn('rotate the leaked token', flags[0])

    def test_all_three_triggers_stack(self):
        analysis = {
            'follow_ups': [{'text': 'drop the prod table', 'risky': True}],
            'diverged': True, 'diverged_note': 'scope grew',
        }
        flags = author.compute_needs_decision_flags(False, analysis)
        self.assertEqual(len(flags), 3)


# --------------------------------------------------------------------------- #
# next-phase handoff — the "Next up: Phase Y" the DM names
# --------------------------------------------------------------------------- #
class NextPhaseAfterTests(unittest.TestCase):
    def test_returns_next_by_order(self):
        a = _phase('p1', order=0)
        b = _phase('p2', title='Second', order=1)
        c = _phase('p3', title='Third', order=2)
        nxt = author.next_phase_after(_project('p', [a, b, c]), a)
        self.assertEqual(nxt['id'], 'p2')

    def test_skips_already_done_phases(self):
        a = _phase('p1', order=0)
        b = _phase('p2', order=1, state='done')
        c = _phase('p3', title='Third', order=2)
        nxt = author.next_phase_after(_project('p', [a, b, c]), a)
        self.assertEqual(nxt['id'], 'p3')

    def test_last_phase_returns_none(self):
        a = _phase('p1', order=0)
        b = _phase('p2', order=1)
        self.assertIsNone(author.next_phase_after(_project('p', [a, b]), b))

    def test_only_phase_returns_none(self):
        a = _phase('solo', order=0)
        self.assertIsNone(author.next_phase_after(_project('p', [a]), a))

    def test_ties_broken_by_list_position(self):
        a = _phase('p1', order=0)
        b = _phase('p2', title='Same order B', order=0)
        c = _phase('p3', title='Same order C', order=0)
        # a is first; the next same-order phase by list position is b.
        nxt = author.next_phase_after(_project('p', [a, b, c]), a)
        self.assertEqual(nxt['id'], 'p2')


# --------------------------------------------------------------------------- #
# drop loose ends into the funnel's Suggested lane — deduped + idempotent
# --------------------------------------------------------------------------- #
class DropFollowUpsToFunnelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.qdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _queued_files(self):
        return sorted(p.name for p in self.qdir.iterdir())

    def test_drops_cards_tagged_closeout_to_suggested_lane(self):
        follow_ups = [
            {'text': 'Wire the retry path', 'risky': False},
            {'text': 'Add a backfill job', 'risky': True},
        ]
        queued = author.drop_follow_ups_to_funnel(
            follow_ups, phase=_phase('p4', title='Closeout'),
            queue_dir=self.qdir, now=_NOW)
        self.assertEqual(queued, ['wire-the-retry-path', 'add-a-backfill-job'])
        self.assertEqual(
            self._queued_files(),
            ['add-a-backfill-job.json', 'wire-the-retry-path.json'])
        # Each card carries the closeout source tag → the suggested lane.
        entry = json.loads((self.qdir / 'wire-the-retry-path.json').read_text())
        self.assertEqual(entry['proposed_by'], 'closeout')
        self.assertEqual(entry['phase'], 'proposed')
        self.assertIn('Closeout', entry['brief'])

    def test_within_batch_dedup(self):
        follow_ups = [
            {'text': 'Wire the retry path'},
            {'text': 'wire the retry path'},   # same kebab id
        ]
        queued = author.drop_follow_ups_to_funnel(
            follow_ups, phase=_phase('p4'), queue_dir=self.qdir, now=_NOW)
        self.assertEqual(queued, ['wire-the-retry-path'])
        self.assertEqual(len(self._queued_files()), 1)

    def test_skips_already_registered_ids(self):
        queued = author.drop_follow_ups_to_funnel(
            [{'text': 'Wire the retry path'}, {'text': 'New work item'}],
            phase=_phase('p4'),
            existing_ids={'wire-the-retry-path'},
            queue_dir=self.qdir, now=_NOW)
        self.assertEqual(queued, ['new-work-item'])

    def test_rerun_is_idempotent_no_duplicate_cards(self):
        follow_ups = [{'text': 'Wire the retry path'}]
        first = author.drop_follow_ups_to_funnel(
            follow_ups, phase=_phase('p4'), queue_dir=self.qdir, now=_NOW)
        self.assertEqual(first, ['wire-the-retry-path'])
        # A second run sees the in-flight queue file → SuggestionError → skip.
        second = author.drop_follow_ups_to_funnel(
            follow_ups, phase=_phase('p4'), queue_dir=self.qdir, now=_NOW)
        self.assertEqual(second, [])
        self.assertEqual(len(self._queued_files()), 1)

    def test_empty_follow_ups_drops_nothing(self):
        self.assertEqual(
            author.drop_follow_ups_to_funnel(
                [], phase=_phase('p4'), queue_dir=self.qdir), [])
        self.assertEqual(self._queued_files(), [])

    def test_blank_text_items_skipped(self):
        queued = author.drop_follow_ups_to_funnel(
            [{'text': '   '}, {'risky': True}, {'text': 'Real one'}],
            phase=_phase('p4'), queue_dir=self.qdir, now=_NOW)
        self.assertEqual(queued, ['real-one'])

    def test_closeout_is_a_recognized_suggesting_source(self):
        # The drop tags cards `proposed_by='closeout'`; that source must be in
        # the shared vocab or the card would mis-lane (secondary/orphan).
        self.assertIn('closeout', suggest.SUGGESTING_AGENTS)


# --------------------------------------------------------------------------- #
# the SEQUENCE_COMPLETE DM — closeout handoff + decision flags
# --------------------------------------------------------------------------- #
class CloseoutDMTests(unittest.TestCase):
    def _seq(self):
        return {
            'seq_id': 'launch-abc',
            'label': 'Closeout outputs',
            'steps': [
                {'step_id': 's1', 'pr_url': 'https://github.com/o/r/pull/5'},
            ],
        }

    def test_clean_completion_renders_handoff_without_flags(self):
        closeout = {
            'attached': True,
            'summary': 'The closeout DM now feeds the next phase.',
            'phase_title': 'Closeout outputs',
            'next_phase_title': 'DAG view',
            'done_gate_met': True,
            'flags': [],
            'follow_ups_queued': [],
        }
        dm = on._render_sequence_complete_dm(self._seq(), closeout=closeout)
        self.assertIn(
            'Phase Closeout outputs done: The closeout DM now feeds the next '
            'phase.', dm)
        self.assertIn(
            'Next up: DAG view — ready for you to brainstorm.', dm)
        self.assertNotIn('Needs your call', dm)
        self.assertNotIn('Dropped', dm)

    def test_decision_flags_render_loudly(self):
        closeout = {
            'summary': 'Shipped, but with caveats.',
            'phase_title': 'P4',
            'next_phase_title': 'P5',
            'done_gate_met': False,
            'flags': [
                "the done-gate wasn't met — not every build step merged cleanly",
                'a risky follow-up needs your eyes — rotate the leaked token',
            ],
            'follow_ups_queued': ['rotate-the-leaked-token'],
        }
        dm = on._render_sequence_complete_dm(self._seq(), closeout=closeout)
        self.assertIn('⚠️ Needs your call:', dm)
        self.assertIn("the done-gate wasn't met", dm)
        self.assertIn('rotate the leaked token', dm)

    def test_follow_up_count_rendered(self):
        closeout = {
            'summary': 's', 'phase_title': 'P4', 'next_phase_title': 'P5',
            'done_gate_met': True, 'flags': [],
            'follow_ups_queued': ['a', 'b', 'c'],
        }
        dm = on._render_sequence_complete_dm(self._seq(), closeout=closeout)
        self.assertIn('Dropped 3 follow-ups into', dm)

    def test_single_follow_up_is_singular(self):
        closeout = {
            'summary': 's', 'phase_title': 'P4', 'next_phase_title': None,
            'done_gate_met': True, 'flags': [], 'follow_ups_queued': ['a'],
        }
        dm = on._render_sequence_complete_dm(self._seq(), closeout=closeout)
        self.assertIn('Dropped 1 follow-up into', dm)
        # No next phase → no "Next up" line.
        self.assertNotIn('Next up:', dm)

    def test_no_closeout_renders_base_dm_unchanged(self):
        base = on._render_sequence_complete_dm(self._seq())
        self.assertNotIn('Needs your call', base)
        self.assertNotIn('ready for you to brainstorm', base)
        # The base completion DM still renders its PR list.
        self.assertIn('Build complete', base)

    def test_empty_closeout_dict_adds_nothing(self):
        base = on._render_sequence_complete_dm(self._seq())
        with_empty = on._render_sequence_complete_dm(
            self._seq(),
            closeout={'phase_title': '', 'summary': '', 'next_phase_title': None,
                      'flags': [], 'follow_ups_queued': []})
        self.assertEqual(base, with_empty)


if __name__ == '__main__':
    unittest.main()
