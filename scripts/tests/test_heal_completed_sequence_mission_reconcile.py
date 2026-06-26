"""Tests for heal_completed_sequence_mission_reconcile — the build-sequence →
mission shipped reconciler.

All IO is sandboxed: the build-sequences dir, missions.json, and the for-Larry
signal are tmp paths injected into run_cycle. Nothing touches live ~/agents,
Supabase, gh, or git (this healer needs none of those — sequence files are the
local source of truth).

    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_completed_sequence_mission_reconcile
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
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

import for_larry_signal  # noqa: E402
import heal_completed_sequence_mission_reconcile as h  # noqa: E402

_NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


# ---------- fixture builders ----------


def _step(step_id, *, status='merged', pr_url=None):
    return {'step_id': step_id, 'label': f'Step {step_id}', 'status': status,
            'pr_url': pr_url, 'merged_at': None, 'depends_on': []}


def _sequence(seq_id, *, status='complete', steps=None):
    return {
        'seq_id': seq_id,
        'label': f'Seq {seq_id}',
        'spec_doc': f'agents/beacon/specs/{seq_id}.md',
        'status': status,
        'current_steps': [],
        'steps': steps if steps is not None else [_step('alpha'), _step('beta')],
        'audit_log': [],
    }


def _mission(mid, *, phase='in_flight', task_ids=None, **extra):
    m = {'id': mid, 'name': mid.replace('-', ' '), 'phase': phase,
         'task_ids': task_ids if task_ids is not None else []}
    m.update(extra)
    return m


def _seq_steps(seq_id, *names):
    """task_ids for a mission whose work runs as sequence ``seq_id``."""
    return [f'seq-{seq_id}-step-{n}' for n in (names or ('alpha', 'beta'))]


# ---------- referenced_sequence_ids (pure) ----------


class ReferencedSequenceIds(unittest.TestCase):
    def test_single_sequence(self):
        self.assertEqual(
            h.referenced_sequence_ids(_seq_steps('seqfoo', 'a', 'b')), {'seqfoo'})

    def test_multiple_sequences(self):
        ids = ['seq-one-step-a', 'seq-two-step-b', 'seq-one-step-c']
        self.assertEqual(h.referenced_sequence_ids(ids), {'one', 'two'})

    def test_dashed_seq_id_non_greedy_first_step(self):
        # seq id with dashes + a step id that itself contains '-step-': the
        # non-greedy split must take the seq up to the FIRST '-step-'.
        self.assertEqual(
            h.referenced_sequence_ids(['seq-my-feature-step-rotate-step-two']),
            {'my-feature'})

    def test_non_sequence_ids_ignored(self):
        self.assertEqual(
            h.referenced_sequence_ids(['forge-build-123', 'review-x', '']), set())

    def test_non_list_safe(self):
        self.assertEqual(h.referenced_sequence_ids(None), set())


# ---------- load_complete_sequences (file reads, fail-safe) ----------


class LoadCompleteSequences(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _write(self, seq_id, **kw):
        (self.dir / f'{seq_id}.json').write_text(json.dumps(_sequence(seq_id, **kw)))

    def test_only_complete_loaded(self):
        self._write('done-seq', status='complete')
        self._write('active-seq', status='active')
        self._write('paused-seq', status='paused')
        loaded = h.load_complete_sequences(self.dir)
        self.assertEqual(set(loaded), {'done-seq'})

    def test_missing_dir_safe(self):
        self.assertEqual(h.load_complete_sequences(self.dir / 'nope'), {})

    def test_malformed_file_skipped(self):
        (self.dir / 'broken.json').write_text('{not json')
        self._write('good-seq')
        self.assertEqual(set(h.load_complete_sequences(self.dir)), {'good-seq'})

    def test_seq_id_falls_back_to_stem(self):
        seq = _sequence('x')
        del seq['seq_id']
        (self.dir / 'stem-id.json').write_text(json.dumps(seq))
        self.assertEqual(set(h.load_complete_sequences(self.dir)), {'stem-id'})


# ---------- classify_mission (pure — the heart of the umbrella safety) ----------


class ClassifyMission(unittest.TestCase):
    def _ship(self, mission, complete=('seqfoo',)):
        return h.classify_mission(mission, set(complete))

    def test_single_owner_complete_ships(self):
        m = _mission('single-owner', task_ids=_seq_steps('seqfoo', 'a', 'b'))
        d = self._ship(m)
        self.assertEqual(d.action, 'ship')
        self.assertEqual(d.seq_id, 'seqfoo')

    def test_multi_sequence_umbrella_surfaces_not_ships(self):
        # The KEY safety case: a mission spanning two sequences is NOT auto-shipped
        # even when both are complete — it is surfaced for a human confirm.
        m = _mission('umbrella', task_ids=['seq-one-step-a', 'seq-two-step-b'])
        d = h.classify_mission(m, {'one', 'two'})
        self.assertEqual(d.action, 'surface')
        self.assertIn('spans 2 sequences', d.reason)

    def test_single_sequence_with_extra_pr_task_surfaces(self):
        # One sequence complete, but a standalone non-sequence PR-backed task also
        # in scope -> umbrella -> surface, never ship.
        m = _mission('mixed', task_ids=[*_seq_steps('seqfoo', 'a'), 'forge-build-999'])
        d = self._ship(m)
        self.assertEqual(d.action, 'surface')
        self.assertIn('non-sequence', d.reason)

    def test_sequence_not_complete_keeps(self):
        m = _mission('waiting', task_ids=_seq_steps('seqfoo', 'a'))
        self.assertEqual(h.classify_mission(m, set()).action, 'keep')

    def test_no_sequence_task_ids_keeps(self):
        m = _mission('plain', task_ids=['forge-build-1'])
        self.assertEqual(self._ship(m).action, 'keep')

    def test_review_shaped_extra_task_does_not_block_ship(self):
        # review-/notify-/dag-preflight- ids are not PR-backed, so they don't make
        # a single-owner mission look umbrella.
        m = _mission('single', task_ids=[*_seq_steps('seqfoo', 'a'),
                                         'review-x', 'notify-y', 'dag-preflight-z'])
        self.assertEqual(self._ship(m).action, 'ship')

    def test_non_reconcilable_phase_keeps(self):
        for phase in ('proposed', 'shipped', 'deferred'):
            m = _mission('m', phase=phase, task_ids=_seq_steps('seqfoo', 'a'))
            self.assertEqual(self._ship(m).action, 'keep')

    def test_archived_ack_retired_keeps(self):
        base = dict(task_ids=_seq_steps('seqfoo', 'a'))
        self.assertEqual(self._ship(_mission('m', archived=True, **base)).action, 'keep')
        self.assertEqual(self._ship(_mission('m', acknowledged=True, **base)).action, 'keep')
        self.assertEqual(
            self._ship(_mission('m', retired_at='2026-06-01T00:00:00Z', **base)).action,
            'keep')

    def test_idless_mission_keeps(self):
        self.assertEqual(
            h.classify_mission({'phase': 'in_flight',
                                'task_ids': _seq_steps('seqfoo', 'a')},
                               {'seqfoo'}).action, 'keep')


# ---------- apply_ship (mutation shape) ----------


class ApplyShip(unittest.TestCase):
    def test_audit_preserved_flip(self):
        m = _mission('m', phase='in_flight')
        seq = _sequence('seqfoo', steps=[_step('a', pr_url='https://github.com/o/r/pull/42'),
                                         _step('b', pr_url='https://github.com/o/r/pull/43')])
        prior = h.apply_ship(m, 'seqfoo', seq, _NOW)
        self.assertEqual(prior, 'in_flight')
        self.assertEqual(m['phase'], 'shipped')
        self.assertEqual(m['prior_phase'], 'in_flight')
        self.assertEqual(m['shipped_at'], _NOW.isoformat())
        self.assertEqual(m['shipped_by'], h.SHIPPED_BY)
        self.assertIn('seqfoo', m['shipped_note'])
        self.assertIn('#42', m['shipped_note'])  # merged-step PR refs surfaced
        self.assertIn('#43', m['shipped_note'])


# ---------- run_cycle (integration: tmp board + sequences + signal) ----------


class RunCycle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.seq_dir = root / 'build-sequences'
        self.seq_dir.mkdir()
        self.miss_path = root / 'missions.json'
        self.signal_path = root / 'for-larry.json'

    def tearDown(self):
        self._tmp.cleanup()

    def _write_sequences(self, *sequences):
        for seq in sequences:
            (self.seq_dir / f"{seq['seq_id']}.json").write_text(json.dumps(seq))

    def _write_missions(self, *missions):
        self.miss_path.write_text(
            json.dumps({'schema_version': 1, 'missions': list(missions)}))

    def _read_missions(self):
        return json.loads(self.miss_path.read_text())['missions']

    def _run(self, **kw):
        return h.run_cycle(
            apply=kw.pop('apply', True), now=_NOW,
            sequences_dir=self.seq_dir, missions_reg_path=self.miss_path,
            signal_path=self.signal_path, **kw)

    def _active_keys(self):
        return {e['key'] for e in for_larry_signal.active_entries(path=self.signal_path)}

    def test_completed_sequence_flips_single_owner_mission_to_shipped(self):
        # seq_id deliberately != mission id, to prove the join is the seq-*-step-*
        # task_id convention, not id equality.
        self._write_sequences(_sequence('escalation-feed-seq'))
        self._write_missions(
            _mission('operator-needs-you-feed', phase='in_flight',
                     task_ids=_seq_steps('escalation-feed-seq', 'one', 'two')))

        res = self._run()

        self.assertEqual([(m, p, s) for m, p, s in res.shipped],
                         [('operator-needs-you-feed', 'in_flight', 'escalation-feed-seq')])
        m = self._read_missions()[0]
        self.assertEqual(m['phase'], 'shipped')
        self.assertEqual(m['prior_phase'], 'in_flight')
        self.assertEqual(m['shipped_by'], h.SHIPPED_BY)
        self.assertIn('escalation-feed-seq', m['shipped_note'])
        # not surfaced — it shipped cleanly
        self.assertEqual(self._active_keys(), set())

    def test_umbrella_mission_not_auto_retired_but_surfaced(self):
        # A mission spanning TWO sequences (both complete) must NOT be flipped —
        # it is surfaced to needs-you instead.
        self._write_sequences(_sequence('seq-one'), _sequence('seq-two'))
        self._write_missions(
            _mission('umbrella-mission', phase='in_flight',
                     task_ids=['seq-seq-one-step-a', 'seq-seq-two-step-b']))

        res = self._run()

        self.assertEqual(res.shipped, [])               # NOT auto-retired
        m = self._read_missions()[0]
        self.assertEqual(m['phase'], 'in_flight')        # phase untouched
        self.assertNotIn('shipped_at', m)
        # surfaced to needs-you instead
        self.assertIn(h.SIGNAL_PREFIX + 'umbrella-mission', self._active_keys())

    def test_incomplete_sequence_is_noop(self):
        self._write_sequences(_sequence('pending-seq', status='active'))
        self._write_missions(
            _mission('m', phase='in_flight', task_ids=_seq_steps('pending-seq', 'a')))
        res = self._run()
        self.assertEqual(res.shipped, [])
        self.assertEqual(self._read_missions()[0]['phase'], 'in_flight')

    def test_dry_run_writes_nothing(self):
        self._write_sequences(_sequence('seqfoo'))
        self._write_missions(
            _mission('m', phase='in_flight', task_ids=_seq_steps('seqfoo', 'a')))
        before = self.miss_path.read_text()
        res = self._run(apply=False)
        self.assertEqual(len(res.shipped), 1)            # planned
        self.assertEqual(self.miss_path.read_text(), before)  # but unwritten
        self.assertEqual(self._active_keys(), set())

    def test_idempotent_second_tick_noop(self):
        self._write_sequences(_sequence('seqfoo'))
        self._write_missions(
            _mission('m', phase='in_flight', task_ids=_seq_steps('seqfoo', 'a')))
        self._run()
        self.assertEqual(self._read_missions()[0]['phase'], 'shipped')
        # Second tick: already shipped (non-reconcilable) -> nothing to do.
        res2 = self._run()
        self.assertEqual(res2.shipped, [])

    def test_umbrella_signal_self_clears_when_mission_advances(self):
        self._write_sequences(_sequence('seq-one'), _sequence('seq-two'))
        self._write_missions(
            _mission('umbrella-mission', phase='in_flight',
                     task_ids=['seq-seq-one-step-a', 'seq-seq-two-step-b']))
        self._run()
        self.assertIn(h.SIGNAL_PREFIX + 'umbrella-mission', self._active_keys())

        # The operator confirms/advances it: phase leaves the reconcilable set.
        self._write_missions(
            _mission('umbrella-mission', phase='shipped',
                     task_ids=['seq-seq-one-step-a', 'seq-seq-two-step-b']))
        self._run()
        self.assertNotIn(h.SIGNAL_PREFIX + 'umbrella-mission', self._active_keys())

    def test_ship_write_failure_does_not_self_clear_stale_umbrella(self):
        # Regression: a mission surfaced as an umbrella on a prior tick, whose
        # scope later collapses to one complete sequence, must NOT lose its
        # needs-you row when this tick's ship write fails — else it vanishes from
        # BOTH surfaces (unshipped on the board AND no nudge).
        import unittest.mock as mock

        # Tick 1: umbrella (two complete sequences) -> surfaced.
        self._write_sequences(_sequence('seq-one'), _sequence('seq-two'))
        self._write_missions(
            _mission('m', phase='in_flight',
                     task_ids=['seq-seq-one-step-a', 'seq-seq-two-step-b']))
        self._run()
        self.assertIn(h.SIGNAL_PREFIX + 'm', self._active_keys())

        # Scope collapses to the single completed seq-one -> classifies `ship`,
        # but the board write fails this tick.
        self._write_missions(
            _mission('m', phase='in_flight', task_ids=['seq-seq-one-step-a']))
        with mock.patch.object(h.gc, '_atomic_write_json',
                               side_effect=OSError('EROFS')):
            res = self._run()

        self.assertEqual(len(res.shipped), 1)                 # classified ship
        self.assertEqual(self._read_missions()[0]['phase'], 'in_flight')  # write failed
        # The stale umbrella row SURVIVES — not falsely cleared.
        self.assertIn(h.SIGNAL_PREFIX + 'm', self._active_keys())

    def test_unresolved_paths_safe(self):
        # No agent-core configured / no missions path -> degrade, never raise.
        res = h.run_cycle(apply=True, now=_NOW, sequences_dir=self.seq_dir,
                          missions_reg_path=None, signal_path=self.signal_path)
        self.assertEqual(res.shipped, [])


if __name__ == '__main__':
    unittest.main()
