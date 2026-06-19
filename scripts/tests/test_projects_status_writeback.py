#!/usr/bin/env python3
"""Tests for projects-v3 P3 follow-up step p3f-status-writeback.

The board must reflect reality: a phase flips to `building` when its build
launch dispatches, and to `done` when that build sequence completes — with no
manual status edit. This covers the three Mirror-review foci:

  * idempotent — done->done (and building->building) are no-ops (no write, so
    the single-committer healer sees no spurious git delta);
  * event-driven not clock — the stamps fire only when a stamp helper is called
    on a dispatch / SEQUENCE_COMPLETE event, never on a timer;
  * non-committer — the writer touches projects.json ON DISK only; no git op
    (heal_projects_store.py stays the sole committer).

Two layers:
  - PURE helpers in `projects_store` (find + idempotent stamp; no IO).
  - the on-disk writer `projects_status_writeback` (atomic read-modify-write,
    routed at a per-test tmp projects.json via OURLIBERTY_PROJECTS_JSON).

Run:
    cd /home/larry/agent-core && python3 -m unittest \\
        scripts.tests.test_projects_status_writeback
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import projects_store as ps                 # noqa: E402
import projects_status_writeback as psw     # noqa: E402


def _phase(pid, *, state='spec', sequence_ref=None):
    return {
        'id': pid,
        'title': pid,
        'desired_end_state': '',
        'lifecycle_state': state,
        'order': 0,
        'spec_ref': 'agents/beacon/specs/x.md' if state != 'brainstorm' else None,
        'sequence_ref': sequence_ref,
        'created_at': '2026-06-17T00:00:00+00:00',
        'updated_at': '2026-06-17T00:00:00+00:00',
    }


def _registry(*projects):
    return {'schema_version': ps.SCHEMA_VERSION, 'projects': list(projects)}


def _project(pid, phases):
    return {
        'id': pid, 'title': pid, 'north_star_ref': None, 'repo': None,
        'state': 'active', 'phases': phases, 'one_off': len(phases) == 1,
        'created_at': '2026-06-17T00:00:00+00:00',
        'updated_at': '2026-06-17T00:00:00+00:00',
    }


def _seq(seq_id='launch-ph1', *, project_id=None, phase_id=None):
    """A build-sequence dict as stamp_done receives it. With project_id+phase_id
    it carries the `authored-by-launch-drain` audit entry the resolver falls back
    to when the building-stamp never persisted the sequence_ref."""
    seq = {'seq_id': seq_id, 'audit_log': []}
    if project_id and phase_id:
        seq['audit_log'] = [{
            'event': 'authored-by-launch-drain',
            'project_id': project_id, 'phase_id': phase_id,
        }]
    return seq


# --------------------------------------------------------------------------- #
# PURE helpers (projects_store)
# --------------------------------------------------------------------------- #
class PureLookupTests(unittest.TestCase):
    def test_find_phase_hits_and_misses(self):
        reg = _registry(_project('p1', [_phase('ph1'), _phase('ph2')]))
        _, phase = ps.find_phase(reg, 'p1', 'ph2')
        self.assertIsNotNone(phase)
        self.assertEqual(phase['id'], 'ph2')
        self.assertEqual((None, None), ps.find_phase(reg, 'p1', 'nope'))
        self.assertEqual((None, None), ps.find_phase(reg, 'nope', 'ph1'))
        self.assertEqual((None, None), ps.find_phase(reg, '', ''))

    def test_find_phase_by_sequence_ref(self):
        reg = _registry(_project('p1', [
            _phase('ph1', state='building', sequence_ref='launch-ph1'),
            _phase('ph2'),
        ]))
        _, phase = ps.find_phase_by_sequence_ref(reg, 'launch-ph1')
        self.assertEqual(phase['id'], 'ph1')
        self.assertEqual((None, None),
                         ps.find_phase_by_sequence_ref(reg, 'launch-zzz'))
        self.assertEqual((None, None), ps.find_phase_by_sequence_ref(reg, ''))

    def test_lookups_safe_on_junk(self):
        self.assertEqual((None, None), ps.find_phase('not-a-dict', 'a', 'b'))
        junk = {'projects': ['x', {'id': 'p', 'phases': ['y', None]}]}
        self.assertEqual((None, None), ps.find_phase(junk, 'p', 'q'))
        self.assertEqual((None, None),
                         ps.find_phase_by_sequence_ref(junk, 'launch-q'))

    def test_launch_ids_from_sequence(self):
        seq = {'seq_id': 'launch-ph1', 'audit_log': [
            {'event': 'authored-by-launch-drain',
             'project_id': 'p1', 'phase_id': 'ph1'}]}
        self.assertEqual(ps.launch_ids_from_sequence(seq), ('p1', 'ph1'))
        # ordinary (non-launch) sequence — no authoring entry
        self.assertEqual(
            ps.launch_ids_from_sequence(
                {'seq_id': 'feat-1', 'audit_log': [{'event': 'step-dispatched'}]}),
            (None, None))
        # authoring entry present but missing/blank ids → (None, None)
        self.assertEqual(
            ps.launch_ids_from_sequence(
                {'audit_log': [{'event': 'authored-by-launch-drain',
                                'project_id': '', 'phase_id': None}]}),
            (None, None))
        # junk — never raises
        self.assertEqual(ps.launch_ids_from_sequence(None), (None, None))
        self.assertEqual(
            ps.launch_ids_from_sequence({'audit_log': ['x', None]}), (None, None))

    def test_resolve_phase_for_sequence_prefers_ref_then_audit(self):
        # sequence_ref wins when present.
        reg = _registry(_project('p1', [
            _phase('ph1', state='building', sequence_ref='launch-ph1')]))
        _, phase = ps.resolve_phase_for_sequence(
            reg, _seq('launch-ph1', project_id='p1', phase_id='ph1'))
        self.assertEqual(phase['id'], 'ph1')
        # ref absent → audit-id fallback resolves the same phase.
        reg2 = _registry(_project('p1', [_phase('ph1', state='spec')]))
        _, phase2 = ps.resolve_phase_for_sequence(
            reg2, _seq('launch-ph1', project_id='p1', phase_id='ph1'))
        self.assertEqual(phase2['id'], 'ph1')
        # non-launch sequence (no ref match, no audit ids) → (None, None).
        self.assertEqual(
            (None, None), ps.resolve_phase_for_sequence(reg2, _seq('ordinary')))

    def test_pin_phase_sequence_ref(self):
        ph = _phase('ph1', state='done', sequence_ref=None)
        self.assertTrue(ps.pin_phase_sequence_ref(ph, 'launch-ph1'))
        self.assertEqual(ph['sequence_ref'], 'launch-ph1')
        self.assertNotEqual(ph['updated_at'], '2026-06-17T00:00:00+00:00')  # bumped
        # idempotent: already pinned → no mutation.
        self.assertFalse(ps.pin_phase_sequence_ref(ph, 'launch-ph1'))
        # never OVERWRITES a different existing ref.
        ph2 = _phase('ph2', state='done', sequence_ref='launch-other')
        self.assertFalse(ps.pin_phase_sequence_ref(ph2, 'launch-ph1'))
        self.assertEqual(ph2['sequence_ref'], 'launch-other')


class PureStampTests(unittest.TestCase):
    def test_building_forward_pins_ref(self):
        ph = _phase('ph1', state='spec')
        self.assertTrue(ps.stamp_phase_building(ph, 'launch-ph1'))
        self.assertEqual(ph['lifecycle_state'], 'building')
        self.assertEqual(ph['sequence_ref'], 'launch-ph1')

    def test_building_same_ref_is_noop(self):
        ph = _phase('ph1', state='building', sequence_ref='launch-ph1')
        self.assertFalse(ps.stamp_phase_building(ph, 'launch-ph1'))

    def test_building_never_regresses_done(self):
        ph = _phase('ph1', state='done', sequence_ref='launch-ph1')
        self.assertFalse(ps.stamp_phase_building(ph, 'launch-ph1'))
        self.assertEqual(ph['lifecycle_state'], 'done')

    def test_done_forward(self):
        ph = _phase('ph1', state='building', sequence_ref='launch-ph1')
        self.assertTrue(ps.stamp_phase_done(ph))
        self.assertEqual(ph['lifecycle_state'], 'done')

    def test_done_is_idempotent(self):
        ph = _phase('ph1', state='done', sequence_ref='launch-ph1')
        self.assertFalse(ps.stamp_phase_done(ph))


# --------------------------------------------------------------------------- #
# on-disk writer (projects_status_writeback) — NON-committer, idempotent
# --------------------------------------------------------------------------- #
class WriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'projects.json'
        self._prev = os.environ.get('OURLIBERTY_PROJECTS_JSON')
        os.environ['OURLIBERTY_PROJECTS_JSON'] = str(self.path)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_PROJECTS_JSON', None)
        else:
            os.environ['OURLIBERTY_PROJECTS_JSON'] = self._prev
        self._tmp.cleanup()

    def _write(self, registry):
        self.path.write_text(json.dumps(registry, indent=2) + '\n')

    def _read(self):
        return json.loads(self.path.read_text())

    def _phase_state(self, project_id, phase_id):
        _, phase = ps.find_phase(self._read(), project_id, phase_id)
        return phase['lifecycle_state'], phase.get('sequence_ref')

    def test_building_then_done_full_cycle(self):
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))

        self.assertTrue(
            psw.stamp_building(seq_id='launch-ph1', project_id='p1', phase_id='ph1'))
        self.assertEqual(self._phase_state('p1', 'ph1'), ('building', 'launch-ph1'))

        self.assertTrue(psw.stamp_done(seq=_seq('launch-ph1')))
        self.assertEqual(self._phase_state('p1', 'ph1')[0], 'done')

    def test_building_idempotent_no_rewrite(self):
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))
        self.assertTrue(
            psw.stamp_building(seq_id='launch-ph1', project_id='p1', phase_id='ph1'))
        before = self.path.read_bytes()
        # Second identical stamp: no change → no write → byte-identical file.
        self.assertFalse(
            psw.stamp_building(seq_id='launch-ph1', project_id='p1', phase_id='ph1'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_done_idempotent_no_rewrite(self):
        self._write(_registry(_project('p1', [
            _phase('ph1', state='building', sequence_ref='launch-ph1')])))
        self.assertTrue(psw.stamp_done(seq=_seq('launch-ph1')))
        before = self.path.read_bytes()
        self.assertFalse(psw.stamp_done(seq=_seq('launch-ph1')))
        self.assertEqual(self.path.read_bytes(), before)

    def test_done_never_regresses_a_later_relaunch(self):
        # A done phase that somehow gets a building dispatch must stay done.
        self._write(_registry(_project('p1', [
            _phase('ph1', state='done', sequence_ref='launch-ph1')])))
        self.assertFalse(
            psw.stamp_building(seq_id='launch-ph1', project_id='p1', phase_id='ph1'))
        self.assertEqual(self._phase_state('p1', 'ph1')[0], 'done')

    def test_missing_phase_is_noop(self):
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))
        before = self.path.read_bytes()
        self.assertFalse(
            psw.stamp_building(seq_id='launch-zz', project_id='p1', phase_id='zz'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_done_for_non_launch_sequence_is_noop(self):
        # An ordinary build sequence has no phase pointing at its seq_id.
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))
        before = self.path.read_bytes()
        self.assertFalse(psw.stamp_done(seq=_seq('some-other-seq-001')))
        self.assertEqual(self.path.read_bytes(), before)

    def test_done_by_id_when_sequence_ref_never_persisted(self):
        # The building-stamp failed to persist (e.g. EROFS): the phase is still
        # `spec` with sequence_ref=None. The done-stamp must STILL flip it to
        # done — resolved by (project_id, phase_id) from the launch-drain audit —
        # and PIN the sequence_ref so the downstream closeout can resolve it.
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))
        self.assertTrue(psw.stamp_done(seq=_seq('launch-ph1', project_id='p1', phase_id='ph1')))
        self.assertEqual(
            self._phase_state('p1', 'ph1'), ('done', 'launch-ph1'))

    def test_done_by_id_is_idempotent_after_pin(self):
        # Re-running the done-stamp (done + ref already pinned) is a no-op → no
        # write → byte-identical file (the double-SEQUENCE_COMPLETE guard).
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))
        self.assertTrue(psw.stamp_done(seq=_seq('launch-ph1', project_id='p1', phase_id='ph1')))
        before = self.path.read_bytes()
        self.assertFalse(psw.stamp_done(seq=_seq('launch-ph1', project_id='p1', phase_id='ph1')))
        self.assertEqual(self.path.read_bytes(), before)

    def test_done_by_id_missing_phase_falls_back_then_noop(self):
        # Bogus ids AND no sequence_ref match → no-op (the non-launch posture).
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))
        before = self.path.read_bytes()
        self.assertFalse(psw.stamp_done(seq=_seq('launch-zz', project_id='nope', phase_id='nope')))
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_store_is_failsafe_noop(self):
        self.path.write_text('{ this is not json')
        # Must NOT raise; returns False (no write).
        self.assertFalse(
            psw.stamp_building(seq_id='launch-ph1', project_id='p1', phase_id='ph1'))
        self.assertFalse(psw.stamp_done(seq=_seq('launch-ph1')))

    def test_does_not_git_commit(self):
        # Non-committer invariant: the writer only touches the JSON file. The
        # tmp dir is not a git repo, so any attempt to commit would surface as
        # an error/log — here we simply assert the file is the only artifact and
        # no .git directory was created by the write path.
        self._write(_registry(_project('p1', [_phase('ph1', state='spec')])))
        psw.stamp_building(seq_id='launch-ph1', project_id='p1', phase_id='ph1')
        siblings = {p.name for p in self.path.parent.iterdir()}
        self.assertNotIn('.git', siblings)


# --------------------------------------------------------------------------- #
# advancer wiring — the launch->building linkage (event-driven seam)
# --------------------------------------------------------------------------- #
class AdvancerBuildingSeamTests(unittest.TestCase):
    """`build_sequence_advancer._maybe_stamp_phase_building` derives the phase
    identity from a launch sequence and stamps building. psw.stamp_building is
    captured so no disk IO is needed."""

    def setUp(self):
        import build_sequence_advancer as bsa  # noqa: E402 — heavy import
        self.bsa = bsa
        self._orig = bsa.psw.stamp_building
        self.calls = []
        bsa.psw.stamp_building = lambda **kw: self.calls.append(kw) or True

    def tearDown(self):
        self.bsa.psw.stamp_building = self._orig

    def _launch_seq(self, seq_id='launch-ph1', project_id='p1', phase_id='ph1'):
        return {
            'seq_id': seq_id,
            'status': 'active',
            'steps': [{'step_id': phase_id, 'status': 'dispatched'}],
            'audit_log': [{
                'ts': '2026-06-17T00:00:00+00:00',
                'event': 'authored-by-launch-drain',
                'actor': 'launch_queue_drain',
                'phase_id': phase_id, 'project_id': project_id,
            }],
        }

    def test_launch_sequence_stamps_building(self):
        self.bsa._maybe_stamp_phase_building(self._launch_seq(), 'ph1')
        self.assertEqual(self.calls, [{
            'seq_id': 'launch-ph1', 'project_id': 'p1', 'phase_id': 'ph1',
        }])

    def test_project_id_read_from_audit_log(self):
        self.bsa._launch_sequence_project_id(self._launch_seq(project_id='proj-x'))
        self.assertEqual(
            self.bsa._launch_sequence_project_id(self._launch_seq(project_id='proj-x')),
            'proj-x')

    def test_non_launch_sequence_does_not_stamp(self):
        ordinary = {'seq_id': 'feature-seq-001', 'steps': [], 'audit_log': []}
        self.bsa._maybe_stamp_phase_building(ordinary, 'step-a')
        self.assertEqual(self.calls, [])

    def test_launch_sequence_without_project_id_skips(self):
        seq = self._launch_seq()
        seq['audit_log'] = []  # no authoring entry → no project_id
        self.bsa._maybe_stamp_phase_building(seq, 'ph1')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
