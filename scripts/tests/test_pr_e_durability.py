#!/usr/bin/env python3
"""PR-E durability fixes — cross-module regression tests.

2026-06-05 full-codebase audit:
  * #7  pulse_check_viii — corrupt artifact must NOT satisfy the same-week
        idempotency sentinel (else the check is suppressed for the ISO week).
  * #58 chain_event_shipper.compute_event_id — an optional disambiguator so the
        dashboard larry_action audit id keys on source_event_id too.
  * #54 heal_restart_dedup_obsolete — archiving must never clobber a same-named
        prior archived file.

Run::

    cd ~/agent-core && python3 -m unittest scripts.tests.test_pr_e_durability
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_viii as p8  # noqa: E402
import chain_event_shipper as ces  # noqa: E402
import heal_restart_dedup_obsolete as hrd  # noqa: E402


# ----------------------------- #7 -----------------------------

class TestViiiSentinelValidity(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_valid_artifact_is_a_sentinel(self):
        p = self.tmp / 'check-viii-2026-06-01.json'
        p.write_text(json.dumps({'week_anchor': '2026-06-01', 'rule_fired': 'x'}))
        self.assertTrue(p8._artifact_is_valid_sentinel(p))

    def test_truncated_artifact_is_not_a_sentinel(self):
        p = self.tmp / 'check-viii-2026-06-01.json'
        p.write_text('{"week_anchor": "2026-06-01", "rule_fi')  # truncated
        self.assertFalse(p8._artifact_is_valid_sentinel(p))

    def test_zero_byte_artifact_is_not_a_sentinel(self):
        p = self.tmp / 'check-viii-2026-06-01.json'
        p.write_text('')
        self.assertFalse(p8._artifact_is_valid_sentinel(p))

    def test_json_without_week_anchor_is_not_a_sentinel(self):
        p = self.tmp / 'check-viii-2026-06-01.json'
        p.write_text(json.dumps({'something': 'else'}))
        self.assertFalse(p8._artifact_is_valid_sentinel(p))

    def test_missing_file_is_not_a_sentinel(self):
        self.assertFalse(p8._artifact_is_valid_sentinel(self.tmp / 'nope.json'))

    def test_write_artifact_is_atomic_and_correct(self):
        # Reroute the proposals dir into the tmp dir and confirm a clean write
        # with no stray tmp sibling.
        p8.PROPOSALS_DIR = self.tmp / 'proposals'
        artifact = {'week_anchor': '2026-06-08', 'rule_fired': 'burn_rate'}
        out = p8.write_artifact(artifact)
        self.assertEqual(json.loads(out.read_text()), artifact)
        self.assertEqual(
            sorted(p.name for p in out.parent.iterdir()),
            ['check-viii-2026-06-08.json'],
        )


# ----------------------------- #58 -----------------------------

class TestComputeEventIdExtra(unittest.TestCase):

    def test_no_extra_is_backward_compatible(self):
        # The hash must be byte-identical to the pre-PR formula when extra is
        # omitted, so existing chain_events PKs do not shift.
        import hashlib
        expected = hashlib.sha1(b'task-1|larry_action|2026-06-05T00:00:00').hexdigest()
        self.assertEqual(
            ces.compute_event_id('task-1', 'larry_action', '2026-06-05T00:00:00'),
            expected,
        )

    def test_none_and_empty_extra_match_no_extra(self):
        base = ces.compute_event_id('t', 'larry_action', 'ts')
        self.assertEqual(ces.compute_event_id('t', 'larry_action', 'ts', None), base)
        self.assertEqual(ces.compute_event_id('t', 'larry_action', 'ts', ''), base)

    def test_extra_disambiguates_same_task_and_microsecond(self):
        ts = '2026-06-05T12:00:00.123456+00:00'
        a = ces.compute_event_id('task-1', 'larry_action', ts, extra='evt-A')
        b = ces.compute_event_id('task-1', 'larry_action', ts, extra='evt-B')
        self.assertNotEqual(a, b)

    def test_same_extra_is_deterministic(self):
        ts = '2026-06-05T12:00:00.123456+00:00'
        self.assertEqual(
            ces.compute_event_id('t', 'larry_action', ts, extra='e'),
            ces.compute_event_id('t', 'larry_action', ts, extra='e'),
        )


# ----------------------------- #54 -----------------------------

class TestRestartDedupNoClobber(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_unique_target_sequence(self):
        d = self.tmp
        name = '20260504231311-build-foo.json'
        (d / name).write_text('first')
        t1 = hrd._unique_archive_target(d / name)
        self.assertEqual(t1.name, '20260504231311-build-foo-dup1.json')
        t1.write_text('second')
        t2 = hrd._unique_archive_target(d / name)
        self.assertEqual(t2.name, '20260504231311-build-foo-dup2.json')

    def test_archive_run_preserves_prior_same_named_file(self):
        # Two restart-dedup events mint the identical 14-digit prefix for the
        # same task. The second archive must NOT destroy the first.
        inbox = self.tmp / 'inbox'
        archive = inbox / 'archive'
        inbox.mkdir(parents=True)
        hrd.MAIN_INBOX = inbox
        hrd.ARCHIVE_DIR = archive
        hrd.KILL_SWITCH = self.tmp / 'healers.disabled'
        hrd.LOG_FILE = self.tmp / 'log.log'
        hrd.STALE_HOURS = 0.0  # archive anything older than "now"

        name = '20260504231311-build-foo.json'
        old = time.time() - 3600

        # First event + run.
        f1 = inbox / name
        f1.write_text('CONTENT-A')
        import os as _os
        _os.utime(f1, (old, old))
        self.assertEqual(hrd.main(), 0)

        # Second event re-creates the identical filename; run again.
        f2 = inbox / name
        f2.write_text('CONTENT-B')
        _os.utime(f2, (old, old))
        self.assertEqual(hrd.main(), 0)

        archived = sorted(p.name for p in archive.iterdir())
        self.assertEqual(
            archived,
            ['20260504231311-build-foo-dup1.json', '20260504231311-build-foo.json'],
        )
        bodies = {(archive / n).read_text() for n in archived}
        self.assertEqual(bodies, {'CONTENT-A', 'CONTENT-B'})


if __name__ == '__main__':
    unittest.main()
