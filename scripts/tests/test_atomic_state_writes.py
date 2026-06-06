#!/usr/bin/env python3
"""Caller-level atomicity tests for the PR-E2 sibling-sweep durability fixes.

The hardening arc (scripts/atomic_io.py, PR-E) made the shared state writers
crash-atomic, but a set of single-writer state files were left as raw
``json.dump`` / ``.write_text(json.dumps(...))``. A crash mid-write left a
*truncated* file; the readers catch ``JSONDecodeError`` and reset to ``{}``,
which for the dedup/cooldown markers silently drops a "already paged" record →
Larry gets re-paged for an already-reported issue.

This module asserts, at the *caller* layer (not just atomic_io itself, which has
its own suite), that the converted writers are now atomic. The fix is uniform —
``atomic_io.atomic_write_json``/``atomic_write_text`` — so rather than one test
per file we cover the three representative shapes:

  * a healer cooldown ``STATE_FILE`` (``heal_droplet_git_drift.save_state``),
  * the ``sort_keys`` deploy-notifier ``STATE_FILE`` (``deploy_notifier``),
  * a ``json.dump``-family writer whose torn write would reset a paging window
    (``watchdog._write_reconcile_marker``), and
  * ``post_merge_verifier.save_state`` (formerly an ``open()`` + ``json.dump``).

For each: a normal write round-trips, and a write that crashes mid-flight (the
final ``os.replace`` raises) leaves the destination either (a) holding the prior
*complete, valid* JSON when one existed, or (b) absent when none existed — never
a truncated file — and never a stray ``.tmp`` sibling.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_atomic_state_writes
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_droplet_git_drift  # noqa: E402
import post_merge_verifier  # noqa: E402
import watchdog  # noqa: E402

# NB: deploy_notifier is imported lazily inside its test's setUp, not here.
# test_deploy_notifier sets OURLIBERTY_AGENTS_ROOT then imports the module
# without reloading, so it relies on being the *first* importer; importing it
# at collection time here would poison that module's AGENTS_ROOT.


# os.replace is the final, publish step of atomic_io.atomic_write_bytes; making
# it raise simulates a crash *after* the durable tmp exists but *before* it is
# renamed onto the destination — the exact window a torn write would occupy.
_REPLACE_BOOM = mock.patch('atomic_io.os.replace', side_effect=OSError('boom'))


class _AtomicCallerCase(unittest.TestCase):
    """Mixin: a tmp dir and assertions shared by every caller's test."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='atomic-state-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _assert_no_tmp_leftovers(self, dest: Path) -> None:
        leftovers = [p.name for p in dest.parent.iterdir() if p != dest]
        self.assertEqual(
            leftovers, [],
            f'crash left stray temp files beside {dest.name}: {leftovers}',
        )

    def _assert_intact_or_absent(self, dest: Path, prior) -> None:
        """After a crashed write, dest is either the prior valid JSON or gone."""
        if prior is None:
            self.assertFalse(
                dest.exists(),
                'crash with no prior state left a (truncated) file behind',
            )
        else:
            # Must still parse — a truncated write would raise here.
            self.assertEqual(json.loads(dest.read_text()), prior)
        self._assert_no_tmp_leftovers(dest)


class TestHealerCooldownStateFile(_AtomicCallerCase):
    """heal_droplet_git_drift.save_state — representative healer cooldown."""

    def setUp(self):
        super().setUp()
        self._orig = heal_droplet_git_drift.STATE_FILE
        heal_droplet_git_drift.STATE_FILE = self.tmp / 'state' / 'cooldowns.json'
        self.addCleanup(setattr, heal_droplet_git_drift, 'STATE_FILE', self._orig)

    def test_roundtrip(self):
        state = {'subjects': {'repo:behind': {'dm_count': 1}}}
        heal_droplet_git_drift.save_state(state)
        self.assertEqual(heal_droplet_git_drift.load_state(), state)

    def test_crash_preserves_prior_state(self):
        old = {'subjects': {'repo:behind': {'dm_count': 1, 'paged': True}}}
        heal_droplet_git_drift.save_state(old)
        with _REPLACE_BOOM:
            # save_state swallows OSError (logs WARN) — it must not re-page.
            heal_droplet_git_drift.save_state({'subjects': {}})
        self._assert_intact_or_absent(heal_droplet_git_drift.STATE_FILE, old)
        # The dedup record survives the crash, so load still reports it.
        self.assertEqual(heal_droplet_git_drift.load_state(), old)

    def test_crash_with_no_prior_leaves_no_torn_file(self):
        with _REPLACE_BOOM:
            heal_droplet_git_drift.save_state({'subjects': {'a': {}}})
        self._assert_intact_or_absent(heal_droplet_git_drift.STATE_FILE, None)


class TestDeployNotifierStateFile(_AtomicCallerCase):
    """deploy_notifier.save_state — sort_keys variant."""

    def setUp(self):
        super().setUp()
        import deploy_notifier  # lazy — see note at module top
        self.dn = deploy_notifier
        self._orig = deploy_notifier.STATE_FILE
        deploy_notifier.STATE_FILE = self.tmp / 'state' / 'deploy-notifier.json'
        self.addCleanup(setattr, deploy_notifier, 'STATE_FILE', self._orig)

    def test_roundtrip_and_sorted(self):
        # load_state() layers in defaults ($schema_version, _meta, ...), so
        # compare the bytes save_state actually persisted.
        state = {'b': 2, 'a': 1}
        self.dn.save_state(state)
        text = self.dn.STATE_FILE.read_text()
        self.assertEqual(json.loads(text), state)
        # sort_keys=True is preserved through the atomic conversion.
        self.assertLess(text.index('"a"'), text.index('"b"'))

    def test_crash_preserves_prior_state(self):
        old = {'last_seen_sha': 'deadbeef', 'notified': True}
        self.dn.save_state(old)
        with _REPLACE_BOOM:
            self.dn.save_state({'last_seen_sha': 'cafe'})
        self._assert_intact_or_absent(self.dn.STATE_FILE, old)


class TestWatchdogReconcileMarker(_AtomicCallerCase):
    """watchdog._write_reconcile_marker — torn write would reset paging window."""

    def setUp(self):
        super().setUp()
        self._orig = watchdog.AGENTS_ROOT
        watchdog.AGENTS_ROOT = self.tmp
        (self.tmp / 'state').mkdir(parents=True, exist_ok=True)
        self.addCleanup(setattr, watchdog, 'AGENTS_ROOT', self._orig)

    def test_roundtrip(self):
        state = {'window_start': 1000.0, 'count': 3, 'paged': True}
        watchdog._write_reconcile_marker('beacon', state)
        path = watchdog._reconcile_marker_path('beacon')
        self.assertEqual(json.loads(path.read_text()), state)

    def test_crash_preserves_paged_flag(self):
        path = watchdog._reconcile_marker_path('beacon')
        old = {'window_start': 1000.0, 'count': 3, 'paged': True}
        watchdog._write_reconcile_marker('beacon', old)
        with _REPLACE_BOOM:
            # A torn write here previously reset the window → re-paging.
            watchdog._write_reconcile_marker(
                'beacon', {'window_start': 2000.0, 'count': 0, 'paged': False})
        self._assert_intact_or_absent(path, old)
        # The window survives intact: still within budget, still marked paged.
        self.assertTrue(json.loads(path.read_text())['paged'])


class TestPostMergeVerifierStateFile(_AtomicCallerCase):
    """post_merge_verifier.save_state — formerly open() + json.dump."""

    def setUp(self):
        super().setUp()
        self._orig = post_merge_verifier.STATE_FILE
        post_merge_verifier.STATE_FILE = self.tmp / 'bb' / 'verifications.json'
        self.addCleanup(setattr, post_merge_verifier, 'STATE_FILE', self._orig)

    def test_roundtrip(self):
        state = {'42': {'status': 'pass', 'attempt': 1}}
        post_merge_verifier.save_state(state)
        self.assertEqual(post_merge_verifier.load_state(), state)

    def test_crash_preserves_prior_state(self):
        old = {'42': {'status': 'pass'}}
        post_merge_verifier.save_state(old)
        with _REPLACE_BOOM:
            post_merge_verifier.save_state({'42': {'status': 'fail'}})
        self._assert_intact_or_absent(post_merge_verifier.STATE_FILE, old)


if __name__ == '__main__':
    unittest.main()
