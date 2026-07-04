"""Tests for prod_write_guard — refuse operator-state writes to the real
~/agents tree while a pytest run is active (fixture-leak guard).

Covers:
  - prod_write_guard.guard_no_prod_write_under_test: inert when
    PYTEST_CURRENT_TEST is unset (production), raises when set AND the target
    resolves inside the real ~/agents tree, no-op when the target is a tmp dir.
  - beacon_approval_handler.save_state: end-to-end, refuses a write whose path
    points at the real prod tree under a simulated pytest env; writes normally
    to a tmp path.
  - for_larry_escalations write (_save via upsert): writes normally to a tmp
    feed file under the same simulated env.

The env is *simulated* by setting PYTEST_CURRENT_TEST ourselves, so the guard
is exercised identically whether the suite runs under stdlib unittest (the
droplet default) or pytest.

stdlib unittest only — pytest is NOT installed in the droplet test env.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import beacon_approval_handler as approval  # noqa: E402
import for_larry_escalations as fle  # noqa: E402
import prod_write_guard  # noqa: E402

_PYTEST_ENV = 'PYTEST_CURRENT_TEST'
# A path that lives inside the REAL production tree (Path.home() is NOT swapped
# by the test bootstrap), used to prove the guard trips.
_PROD_ROOT = Path.home() / 'agents'


class _SimEnvMixin(unittest.TestCase):
    """Save/restore PYTEST_CURRENT_TEST so each test controls the harness signal
    deterministically regardless of the actual runner."""

    def setUp(self):
        super().setUp()
        self._orig_pytest_env = os.environ.get(_PYTEST_ENV)
        self.addCleanup(self._restore_pytest_env)

    def _restore_pytest_env(self):
        if self._orig_pytest_env is None:
            os.environ.pop(_PYTEST_ENV, None)
        else:
            os.environ[_PYTEST_ENV] = self._orig_pytest_env

    def _sim_pytest_on(self):
        os.environ[_PYTEST_ENV] = 'test_prod_write_guard::sim (call)'

    def _sim_pytest_off(self):
        os.environ.pop(_PYTEST_ENV, None)


class GuardFunctionTest(_SimEnvMixin):
    def test_inert_when_pytest_env_unset(self):
        # Production posture: no PYTEST_CURRENT_TEST → guard must be a no-op even
        # for a path squarely inside the real prod tree. (No write happens; we
        # only assert the guard does not raise.)
        self._sim_pytest_off()
        prod_write_guard.guard_no_prod_write_under_test(
            _PROD_ROOT / 'state' / 'beacon-pending-approvals.json')

    def test_raises_on_prod_path_under_pytest(self):
        self._sim_pytest_on()
        target = _PROD_ROOT / 'state' / 'beacon-pending-approvals.json'
        with self.assertRaises(RuntimeError) as ctx:
            prod_write_guard.guard_no_prod_write_under_test(target)
        msg = str(ctx.exception)
        # Message names the offending path and tells the author how to fix it.
        self.assertIn(str(target.resolve()), msg)
        self.assertIn('OURLIBERTY_AGENTS_ROOT', msg)
        self.assertIn('monkeypatch', msg)

    def test_ok_on_tmp_path_under_pytest(self):
        self._sim_pytest_on()
        with tempfile.TemporaryDirectory() as tmp:
            # A correctly-isolated test targets a tmp dir OUTSIDE ~/agents.
            prod_write_guard.guard_no_prod_write_under_test(
                Path(tmp) / 'state' / 'beacon-pending-approvals.json')


class SaveStateWriterTest(_SimEnvMixin):
    def setUp(self):
        super().setUp()
        self._orig_path = approval.PENDING_APPROVALS_PATH
        self.addCleanup(self._restore_path)

    def _restore_path(self):
        approval.PENDING_APPROVALS_PATH = self._orig_path

    def test_save_state_refuses_prod_path_under_pytest(self):
        self._sim_pytest_on()
        approval.PENDING_APPROVALS_PATH = (
            _PROD_ROOT / 'state' / 'beacon-pending-approvals.json')
        before = (approval.PENDING_APPROVALS_PATH.read_bytes()
                  if approval.PENDING_APPROVALS_PATH.exists() else None)
        with self.assertRaises(RuntimeError):
            approval.save_state({'pending': [], 'history': []})
        # The guard fires BEFORE the atomic write, so the live file is untouched.
        after = (approval.PENDING_APPROVALS_PATH.read_bytes()
                 if approval.PENDING_APPROVALS_PATH.exists() else None)
        self.assertEqual(before, after)

    def test_save_state_writes_tmp_path_under_pytest(self):
        self._sim_pytest_on()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state' / 'beacon-pending-approvals.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            approval.PENDING_APPROVALS_PATH = path
            approval.save_state({'pending': [], 'history': [{'x': 1}]})
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text())['history'], [{'x': 1}])


class EscalationsWriterTest(_SimEnvMixin):
    def setUp(self):
        super().setUp()
        self._orig_feed = os.environ.get('OURLIBERTY_FOR_LARRY_FEED_FILE')
        self.addCleanup(self._restore_feed)

    def _restore_feed(self):
        if self._orig_feed is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_FEED_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = self._orig_feed

    def test_upsert_writes_tmp_feed_under_pytest(self):
        self._sim_pytest_on()
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / 'for-larry-escalations.json'
            os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = str(feed)
            row = fle.upsert(
                'mirror-review:t1', headline='Needs you', context='go unstick',
                pr_url='https://gh/o/r/pull/9', head_sha='abc',
                dedup_identity='pr9@abc')
            self.assertIsNotNone(row)
            self.assertTrue(feed.exists())
            doc = json.loads(feed.read_text())
            self.assertEqual([r['id'] for r in doc['escalations']],
                             ['mirror-review:t1'])

    def test_upsert_refuses_prod_feed_under_pytest(self):
        self._sim_pytest_on()
        prod_feed = _PROD_ROOT / 'blackboard' / 'for-larry-escalations.json'
        os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = str(prod_feed)
        before = prod_feed.read_bytes() if prod_feed.exists() else None
        with self.assertRaises(RuntimeError):
            fle.upsert('mirror-review:t1', headline='leak', context='leak',
                       dedup_identity='pr9@abc')
        after = prod_feed.read_bytes() if prod_feed.exists() else None
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
