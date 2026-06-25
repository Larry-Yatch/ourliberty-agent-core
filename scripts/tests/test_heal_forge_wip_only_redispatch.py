#!/usr/bin/env python3
"""Tests for scripts/heal_forge_wip_only_redispatch.py.

Proves the load-bearing contract: exactly-once mechanical re-dispatch of a
WIP-only / dead-session / no-PR / not-already-requeued abandoned Forge build,
and a no-op on every subsequent tick via three independent idempotency gates
(ledger, live-inbox family-root, retry-suffixed task_id). Also proves the
skip cases (live in-flight, open PR, pending manual requeue) and the single
loud retries-exhausted escalation.

Run:
    python3 -m unittest scripts.tests.test_heal_forge_wip_only_redispatch
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_forge_wip_only_redispatch as h  # noqa: E402
import test_isolation_guard  # noqa: E402

_ORIGINAL_ENVELOPE = {
    'task_id': 'synthetic-wip-001',
    'summary': 'synthetic abandoned build',
    'target_agent': 'forge',
    'target_repo': 'ourliberty-agent-core',
    'task_type': 'doc-only',
    'pr_title': 'chore: synthetic',
    'changed_files': ['agents/beacon/missions.json'],
    'prompt': 'GOAL: do the synthetic thing.',
    'phase': 'build',
    'source': 'beacon',
    'reply_chat_id': 7998341473,
}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='heal-wip-redispatch-')
        root = Path(self._tmp)
        self._repo = root / 'repo'
        self._repo.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch.object(h, 'AGENTS_ROOT', root),
            mock.patch.object(h, 'KILL_SWITCH', root / 'healers.disabled'),
            mock.patch.object(h, 'INBOXES_ROOT', root / 'inboxes'),
            mock.patch.object(h, 'IN_FLIGHT_DIR', root / 'state' / 'in-flight'),
            mock.patch.object(h, 'LEDGER_PATH',
                              root / 'state' / 'forge_wip_redispatch_ledger.json'),
            mock.patch.object(h, 'LOG_FILE', root / 'logs' / 'h.log'),
            # Capture alerts instead of reaching the real larry_alerts tree.
            mock.patch.object(h, '_emit_alert'),
            # Canonical repo set + all git/gh helpers are mocked per test.
            mock.patch.object(h.cdb, 'load_canonical_repos',
                              return_value=[self._repo]),
            mock.patch.object(h.cdb, 'fetch_prune', return_value=None),
        ]
        for p in self._patches:
            p.start()
        self._alerts = h._emit_alert
        (h.INBOXES_ROOT / 'forge' / '.archive').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- fixture helpers ----

    def _archive_original(self, env: dict = None):
        env = env or _ORIGINAL_ENVELOPE
        path = h.INBOXES_ROOT / 'forge' / '.archive' / f'{env["task_id"]}.json'
        path.write_text(json.dumps(env))

    def _write_live_inbox(self, task_id: str):
        path = h.INBOXES_ROOT / 'forge' / f'{task_id}.json'
        path.write_text(json.dumps({'task_id': task_id, 'phase': 'build'}))
        return path

    def _write_in_flight(self, task_id: str, pid: int):
        d = h.IN_FLIGHT_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{task_id}.json').write_text(json.dumps({'pid': pid}))

    def _seed_ledger(self, ledger: dict):
        h.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        h.LEDGER_PATH.write_text(json.dumps(ledger))

    @contextlib.contextmanager
    def _git_world(self, branches, net='empty-wip', open_heads=None,
                   merged_heads=None):
        """Mock the cdb git/gh surface. `branches` = list[(name, ts)]."""
        def _list(repo, scope):
            return list(branches) if scope == 'remote' else []

        def _heads(repo, state):
            if state == 'open':
                return set(open_heads or set())
            return set(merged_heads or set())

        with mock.patch.object(h.cdb, 'list_candidate_branches', side_effect=_list), \
             mock.patch.object(h.cdb, 'compute_net_change', return_value=net), \
             mock.patch.object(h.cdb, '_gh_pr_heads', side_effect=_heads):
            yield

    def _run(self):
        # The inbox write is guarded by refuse_under_test; legitimately exercise
        # the real write into the sandbox tree.
        with test_isolation_guard.allow('inbox-write'):
            return h.main()

    def _inbox_files(self):
        return sorted(p.name for p in (h.INBOXES_ROOT / 'forge').glob('*.json'))

    def _ledger(self):
        return json.loads(h.LEDGER_PATH.read_text()) if h.LEDGER_PATH.exists() else {}


class ExactlyOnceTest(_Base):
    def test_redispatch_then_noop_via_ledger_and_inbox(self):
        self._archive_original()
        old = time.time() - 3600  # older than GRACE_SECONDS
        branches = [('forge/synthetic-wip-001', old)]

        # Tick 1: the abandoned WIP-only branch is re-dispatched exactly once.
        with self._git_world(branches):
            self._run()

        files = self._inbox_files()
        self.assertIn('synthetic-wip-001-retry1.json', files)
        env = json.loads(
            (h.INBOXES_ROOT / 'forge' / 'synthetic-wip-001-retry1.json').read_text())
        self.assertEqual(env['task_id'], 'synthetic-wip-001-retry1')
        self.assertEqual(env['phase'], 'build')
        self.assertEqual(env['prompt'], _ORIGINAL_ENVELOPE['prompt'])
        self.assertEqual(env['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(env['reply_chat_id'], 7998341473)
        self.assertEqual(env['redispatch_of'], 'synthetic-wip-001')

        ledger = self._ledger()
        self.assertEqual(ledger['synthetic-wip-001']['attempts'], 1)
        self.assertEqual(ledger['synthetic-wip-001']['last_retry_task_id'],
                         'synthetic-wip-001-retry1')
        # One digest alert, no escalation.
        routes = [c.args[0] for c in self._alerts.call_args_list]
        self.assertEqual(routes, ['digest'])

        # Tick 2: same WIP-only branch, but the retry is now queued live.
        # Every gate must short-circuit — no second envelope, no new alert.
        with self._git_world(branches):
            self._run()

        self.assertEqual(
            self._inbox_files().count('synthetic-wip-001-retry1.json'), 1)
        self.assertEqual(len(self._inbox_files()), 1)
        self.assertEqual(self._alerts.call_count, 1)  # unchanged


class SkipCasesTest(_Base):
    def test_skips_live_in_flight_session(self):
        self._archive_original()
        self._write_in_flight('synthetic-wip-001', os.getpid())  # alive pid
        with self._git_world([('forge/synthetic-wip-001', time.time() - 3600)]):
            self._run()
        self.assertNotIn('synthetic-wip-001-retry1.json', self._inbox_files())
        self.assertEqual(self._alerts.call_count, 0)

    def test_skips_open_pr_head(self):
        self._archive_original()
        with self._git_world([('forge/synthetic-wip-001', time.time() - 3600)],
                             open_heads={'forge/synthetic-wip-001'}):
            self._run()
        self.assertNotIn('synthetic-wip-001-retry1.json', self._inbox_files())
        self.assertEqual(self._alerts.call_count, 0)

    def test_skips_when_retry_already_pending_manual_002(self):
        # A human already requeued as -002; the healer must not pile on.
        self._archive_original()
        self._write_live_inbox('synthetic-wip-002')
        with self._git_world([('forge/synthetic-wip-001', time.time() - 3600)]):
            self._run()
        self.assertNotIn('synthetic-wip-001-retry1.json', self._inbox_files())
        self.assertEqual(self._alerts.call_count, 0)

    def test_skips_too_young_branch(self):
        self._archive_original()
        with self._git_world([('forge/synthetic-wip-001', time.time() - 60)]):
            self._run()
        self.assertNotIn('synthetic-wip-001-retry1.json', self._inbox_files())
        self.assertEqual(self._alerts.call_count, 0)

    def test_skips_non_wip_branch(self):
        self._archive_original()
        with self._git_world([('forge/synthetic-wip-001', time.time() - 3600)],
                             net='unique'):
            self._run()
        self.assertNotIn('synthetic-wip-001-retry1.json', self._inbox_files())
        self.assertEqual(self._alerts.call_count, 0)

    def test_skips_when_no_archived_original(self):
        # No archive file -> never synthesize an envelope from nothing.
        with self._git_world([('forge/synthetic-wip-001', time.time() - 3600)]):
            self._run()
        self.assertNotIn('synthetic-wip-001-retry1.json', self._inbox_files())
        self.assertEqual(self._ledger(), {})

    def test_skips_repo_when_gh_open_list_unavailable(self):
        self._archive_original()
        old = time.time() - 3600
        with mock.patch.object(h.cdb, 'list_candidate_branches',
                               side_effect=lambda r, s: [('forge/synthetic-wip-001', old)] if s == 'remote' else []), \
             mock.patch.object(h.cdb, 'compute_net_change', return_value='empty-wip'), \
             mock.patch.object(h.cdb, '_gh_pr_heads', return_value=None):
            self._run()
        self.assertNotIn('synthetic-wip-001-retry1.json', self._inbox_files())


class ExhaustedEscalationTest(_Base):
    def test_exhausted_escalates_exactly_once(self):
        # Realistic exhaustion: the family is at MAX and the latest RETRY's OWN
        # branch is itself WIP-only abandoned (the retry also died mid-build).
        self._archive_original()
        self._seed_ledger({'synthetic-wip-001': {
            'attempts': h.MAX_AUTO_RETRIES,
            'last_retry_task_id': 'synthetic-wip-001-retry1',
            'escalated': False}})
        branches = [('forge/synthetic-wip-001-retry1', time.time() - 3600)]

        # Tick 1: the retry branch is abandoned -> ONE loud alert.
        with self._git_world(branches):
            self._run()
        routes = [c.args[0] for c in self._alerts.call_args_list]
        self.assertEqual(routes, ['escalate'])
        self.assertTrue(self._ledger()['synthetic-wip-001']['escalated'])
        # No envelope written on the escalation path.
        self.assertNotIn('synthetic-wip-001-retry1-retry1.json', self._inbox_files())

        # Tick 2: escalated flag set -> silent, no repeat alert.
        with self._git_world(branches):
            self._run()
        self.assertEqual(self._alerts.call_count, 1)

    def test_no_false_escalation_on_lingering_original_after_redispatch(self):
        # Regression for Mirror review #693 rev 1: after a successful redispatch
        # the original forge/<base> branch lingers empty-wip (branch GC waits
        # 48h) while the retry is healthily building under <base>-retry1. The
        # next tick must NOT fire a false 'retries-exhausted' escalation off the
        # lingering original.
        self._archive_original()
        old = time.time() - 3600

        # Tick 1: redispatch the abandoned original.
        with self._git_world([('forge/synthetic-wip-001', old)]):
            self._run()
        self.assertIn('synthetic-wip-001-retry1.json', self._inbox_files())
        self.assertEqual([c.args[0] for c in self._alerts.call_args_list],
                         ['digest'])

        # inbox_watcher consumes the retry envelope within seconds and the retry
        # is now building: remove it from the live inbox, register it in-flight,
        # and its own WIP checkpoint branch exists.
        (h.INBOXES_ROOT / 'forge' / 'synthetic-wip-001-retry1.json').unlink()
        self._write_in_flight('synthetic-wip-001-retry1', os.getpid())
        branches = [('forge/synthetic-wip-001', old),
                    ('forge/synthetic-wip-001-retry1', old)]

        # Tick 2: original lingers, retry is live in-flight. NO escalation,
        # NO second redispatch.
        with self._git_world(branches):
            self._run()
        self.assertEqual(self._alerts.call_count, 1)  # still just the digest
        escalate_calls = [c for c in self._alerts.call_args_list
                          if c.args[0] == 'escalate']
        self.assertEqual(escalate_calls, [])
        self.assertFalse(self._ledger()['synthetic-wip-001'].get('escalated'))
        self.assertNotIn('synthetic-wip-001-retry1-retry1.json', self._inbox_files())


class KillSwitchTest(_Base):
    def test_kill_switch_short_circuits(self):
        self._archive_original()
        h.KILL_SWITCH.write_text('')
        with self._git_world([('forge/synthetic-wip-001', time.time() - 3600)]):
            self._run()
        self.assertNotIn('synthetic-wip-001-retry1.json', self._inbox_files())


class HelperUnitTest(unittest.TestCase):
    def test_ledger_base_strips_retry_suffix(self):
        self.assertEqual(h._ledger_base('foo-001'), 'foo-001')
        self.assertEqual(h._ledger_base('foo-001-retry1'), 'foo-001')
        self.assertEqual(h._ledger_base('foo-001-retry12'), 'foo-001')

    def test_family_root_collapses_numeric_and_retry(self):
        self.assertEqual(h._family_root('foo-001'), 'foo')
        self.assertEqual(h._family_root('foo-002'), 'foo')
        self.assertEqual(h._family_root('foo-001-retry1'), 'foo')

    def test_task_from_branch(self):
        self.assertEqual(h._task_from_branch('forge/foo-001'), 'foo-001')
        self.assertEqual(h._task_from_branch('mirror/bar'), 'bar')
        self.assertIsNone(h._task_from_branch('main'))


if __name__ == '__main__':
    unittest.main()
