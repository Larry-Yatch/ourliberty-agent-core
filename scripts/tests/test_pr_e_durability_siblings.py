#!/usr/bin/env python3
"""Tests for the PR-E durability gaps #366 left behind.

#366 ("atomic writes + corrupt-state preservation, audit PR-E subset") fixed
the primary sites but missed three siblings/edge-cases:

  * #7  pulse_check_ix.write_artifact — non-atomic write to the same-week
        idempotency sentinel (the viii sibling), plus the main() gate not
        validating the sentinel is parseable.
  * #54 heal_blocked_inbox_age — plain shutil.move into the archive that
        silently overwrites a same-named prior archived task (the
        heal_restart_dedup sibling).
  * #37 alert_triage_state.read_state — the not-a-dict (valid JSON, wrong
        shape) branch still returned {} without preserving prior rows.

These import only pure modules (no fastapi), so they run on bare system Python.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_ix as p9  # noqa: E402
import heal_blocked_inbox_age as hba  # noqa: E402
import alert_triage_state as ats  # noqa: E402


# ---------- #7 pulse_check_ix sentinel atomicity + validity ----------

class TestIxSentinelValidity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = p9.PROPOSALS_DIR
        p9.PROPOSALS_DIR = self.tmp

    def tearDown(self):
        p9.PROPOSALS_DIR = self._orig

    def _artifact(self):
        return {'week_anchor': '2026-05-25', 'rule_fired': 'none'}

    def test_write_artifact_is_atomic_and_correct(self):
        path = p9.write_artifact(self._artifact())
        self.assertEqual(json.loads(path.read_text())['week_anchor'], '2026-05-25')
        # No leftover temp files from the atomic write.
        leftovers = [q for q in path.parent.iterdir() if q != path]
        self.assertEqual(leftovers, [])

    def test_valid_artifact_is_a_sentinel(self):
        path = p9.write_artifact(self._artifact())
        self.assertTrue(p9._artifact_is_valid_sentinel(path))

    def test_truncated_artifact_is_not_a_sentinel(self):
        path = self.tmp / 'check-ix-2026-05-25.json'
        path.write_text('{"week_anchor": "2026-05-25"')  # truncated
        self.assertFalse(p9._artifact_is_valid_sentinel(path))

    def test_zero_byte_artifact_is_not_a_sentinel(self):
        path = self.tmp / 'check-ix-2026-05-25.json'
        path.write_text('')
        self.assertFalse(p9._artifact_is_valid_sentinel(path))

    def test_json_without_week_anchor_is_not_a_sentinel(self):
        path = self.tmp / 'check-ix-2026-05-25.json'
        path.write_text('{"rule_fired": "none"}')
        self.assertFalse(p9._artifact_is_valid_sentinel(path))


# ---------- #54 heal_blocked_inbox_age no-clobber archive ----------

class TestBlockedInboxAgeNoClobber(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_unique_target_sequence(self):
        d = self.tmp
        name = 'task-foo.json'
        (d / name).write_text('orig')
        t1 = hba._unique_archive_target(d / name)
        self.assertEqual(t1.name, 'task-foo-dup1.json')
        t1.write_text('a')
        t2 = hba._unique_archive_target(d / name)
        self.assertEqual(t2.name, 'task-foo-dup2.json')

    def test_archive_run_preserves_prior_same_named_file(self):
        # A blocked dir with a stale task whose name already exists in .archive.
        blocked = self.tmp / 'inboxes' / 'forge' / 'blocked'
        blocked.mkdir(parents=True, exist_ok=True)
        archive = blocked / '.archive'
        archive.mkdir(parents=True, exist_ok=True)
        name = 'task-foo.json'
        (archive / name).write_text('OLD ARCHIVED')
        stale = blocked / name
        stale.write_text('NEW STALE')
        old = time.time() - (hba.ARCHIVE_AGE_HOURS * 3600) - 60
        os.utime(stale, (old, old))

        orig = {'INBOXES_ROOT': hba.INBOXES_ROOT, 'KILL_SWITCH': hba.KILL_SWITCH,
                'LOG_FILE': hba.LOG_FILE}
        hba.INBOXES_ROOT = self.tmp / 'inboxes'
        hba.KILL_SWITCH = self.tmp / 'healers.disabled'
        hba.LOG_FILE = self.tmp / 'log.log'
        try:
            rc = hba.main()
        finally:
            for k, v in orig.items():
                setattr(hba, k, v)
        self.assertEqual(rc, 0)
        self.assertEqual((archive / name).read_text(), 'OLD ARCHIVED')
        self.assertEqual((archive / 'task-foo-dup1.json').read_text(), 'NEW STALE')
        self.assertFalse(stale.exists())
        # Sidecar tracks the de-collided name, not the original.
        self.assertTrue((archive / 'task-foo-dup1.archive-reason.txt').exists())


# ---------- #37 alert_triage_state not-a-dict preservation ----------

class TestTriageWrongShapePreserved(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._prior = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        importlib.reload(ats)

    def tearDown(self):
        if self._prior is not None:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prior
        else:
            del os.environ['OURLIBERTY_AGENTS_ROOT']
        importlib.reload(ats)

    def _state_path(self) -> Path:
        return self.tmp / ats.STATE_REL

    def test_wrong_shape_json_is_preserved_not_silently_dropped(self):
        p = self._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('["not", "an", "object"]')
        self.assertEqual(ats.read_state(), {})
        # The corrupt-shape file is moved aside to a sidecar, not left to be
        # clobbered by the next write.
        sidecars = list(p.parent.glob(f'{p.name}.corrupt-*'))
        self.assertEqual(len(sidecars), 1)
        self.assertIn('not', sidecars[0].read_text())

    def test_wrong_shape_then_write_keeps_history_in_sidecar(self):
        p = self._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('42')  # valid JSON, not a dict
        ats.record_triage('alert-C', 1, 'autofix', 'rationale')
        self.assertEqual(list(ats.read_state().keys()), ['alert-C'])
        sidecars = list(p.parent.glob(f'{p.name}.corrupt-*'))
        self.assertEqual(len(sidecars), 1)


if __name__ == '__main__':
    unittest.main()
