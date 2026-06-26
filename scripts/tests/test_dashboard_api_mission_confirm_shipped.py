#!/usr/bin/env python3
"""Tests for the `confirm_shipped` mission action (one-click confirm of a
"looks shipped" off-board mission surfaced by heal_merged_pr_board_reconcile).

Unlike defer/resume (PR-backed), confirm_shipped is an ON-DISK delta — it flips
the mission to phase 'shipped' on the LOCAL missions.json (heal_missions_card_gc
commits the delta on its next tick) and resolves the for-Larry signal so the
needs-you row clears. So these tests need NO GitHub stub; they read the missions
file back directly and assert the signal record resolved.

Also covers system_state_log._escalation_to_waiting_item passing the
`mission_id` + `actions:['confirm_shipped']` affordance through to the
Where-are-we needs-you row.

Run (needs the fastapi+httpx venv):
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_mission_confirm_shipped
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

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
import for_larry_signal  # noqa: E402
import heal_merged_pr_board_reconcile as reconcile  # noqa: E402
import system_state_log as ssl  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ACTOR = next(iter(da.LARRY_ACTION_ALLOWED_EMAILS))
AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': ACTOR}


def _endpoint(mission_id: str) -> str:
    return f'/api/system/missions/{mission_id}/action'


def _mission(mid='off-1', *, phase='drafting', name='Off-board mission',
             task_ids=None, **extra):
    m = {'id': mid, 'name': name, 'phase': phase, 'brief': 'b',
         'spec_docs': [], 'task_ids': list(task_ids or []),
         'repo': 'ourliberty-agent-core', 'created': '2026-06-01'}
    m.update(extra)
    return m


class ConfirmShippedTest(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-confirm-shipped-'))
        self.missions_path = self.tmp / 'agents' / 'beacon' / 'missions.json'
        self.signal_path = self.tmp / 'for-larry.json'

        self._orig_missions_path = da._missions_json_path
        da._missions_json_path = lambda: self.missions_path  # type: ignore[assignment]
        self._prev_signal_env = os.environ.get('OURLIBERTY_FOR_LARRY_SIGNAL_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = str(self.signal_path)

        self.client = TestClient(da.app)

    def tearDown(self):
        da._missions_json_path = self._orig_missions_path  # type: ignore[assignment]
        if self._prev_signal_env is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_SIGNAL_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = self._prev_signal_env

    def _seed(self, *missions):
        self.missions_path.parent.mkdir(parents=True, exist_ok=True)
        self.missions_path.write_text(
            json.dumps({'schema_version': 1, 'missions': list(missions)},
                       indent=2) + '\n')

    def _seed_signal(self, mid):
        for_larry_signal.upsert_record(
            reconcile.SIGNAL_PREFIX + mid,
            {'headline': f'Mission looks shipped: {mid}', 'mission_id': mid,
             'source': reconcile.SIGNAL_BY},
            path=self.signal_path)

    def _read_mission(self, mid):
        reg = json.loads(self.missions_path.read_text())
        return next(m for m in reg['missions'] if m['id'] == mid)

    def _active_signal_keys(self):
        return {e['key']
                for e in for_larry_signal.active_entries(path=self.signal_path)}

    # ---- the happy path ----
    def test_confirms_flips_phase_and_resolves_signal(self):
        self._seed(_mission('off-1', phase='drafting'))
        self._seed_signal('off-1')
        self.assertIn(reconcile.SIGNAL_PREFIX + 'off-1', self._active_signal_keys())

        r = self.client.post(_endpoint('off-1'), headers=AUTH,
                             json={'action': 'confirm_shipped'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'applied': True, 'status': 'shipped'})

        m = self._read_mission('off-1')
        self.assertEqual(m['phase'], 'shipped')
        self.assertEqual(m['prior_phase'], 'drafting')
        self.assertEqual(m['shipped_by'], ACTOR)  # the confirming actor, audited
        self.assertIn('shipped_at', m)
        # the surfaced needs-you row clears immediately
        self.assertNotIn(reconcile.SIGNAL_PREFIX + 'off-1', self._active_signal_keys())

    def test_no_git_pr_fields_in_response(self):
        # On-disk delta, not PR-backed — never returns a pr_url/branch.
        self._seed(_mission('off-1'))
        r = self.client.post(_endpoint('off-1'), headers=AUTH,
                             json={'action': 'confirm_shipped'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('pr_url', r.json())
        self.assertNotIn('branch', r.json())

    def test_double_click_returns_409(self):
        self._seed(_mission('off-1', phase='shipped'))
        r = self.client.post(_endpoint('off-1'), headers=AUTH,
                             json={'action': 'confirm_shipped'})
        self.assertEqual(r.status_code, 409)

    def test_unknown_mission_returns_404(self):
        self._seed(_mission('off-1'))
        r = self.client.post(_endpoint('does-not-exist'), headers=AUTH,
                             json={'action': 'confirm_shipped'})
        self.assertEqual(r.status_code, 404)

    def test_signal_resolve_is_fail_soft(self):
        # No signal seeded — the flip still applies and returns 200 (resolve is
        # a no-op, never fails the already-applied write).
        self._seed(_mission('off-1', phase='drafting'))
        r = self.client.post(_endpoint('off-1'), headers=AUTH,
                             json={'action': 'confirm_shipped'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._read_mission('off-1')['phase'], 'shipped')

    def test_actor_default_when_blank(self):
        # Direct handler call (bypasses auth) — exercises the actor-default audit
        # branch (`shipped_by = actor or 'dashboard:confirm_shipped'`) + the full
        # stamp shape, which the 401 auth test can't reach.
        self._seed(_mission('off-1', phase='drafting'))
        out = da._handle_mission_confirm_shipped(
            mission_id='off-1', missions_path=self.missions_path, actor='')
        self.assertEqual(out, {'applied': True, 'status': 'shipped'})
        m = self._read_mission('off-1')
        self.assertEqual(m['shipped_by'], 'dashboard:confirm_shipped')
        self.assertEqual(m['prior_phase'], 'drafting')
        self.assertIn('shipped_at', m)
        self.assertIn('shipped_note', m)

    def test_missing_actor_returns_401(self):
        self._seed(_mission('off-1'))
        r = self.client.post(_endpoint('off-1'),
                             headers={'X-Dashboard-Token': TOKEN},
                             json={'action': 'confirm_shipped'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self._read_mission('off-1')['phase'], 'drafting')  # untouched


class EscalationActionPassthroughTest(unittest.TestCase):
    """system_state_log surfaces the confirm_shipped affordance only for the
    reconcile producer's records."""

    def test_reconcile_record_carries_mission_id_and_action(self):
        item = ssl._escalation_to_waiting_item({
            'headline': 'Mission looks shipped: off-1',
            'context': 'PR #9 looks merged',
            'mission_id': 'off-1',
            'source': 'heal_merged_pr_board_reconcile',
        })
        self.assertEqual(item['mission_id'], 'off-1')
        self.assertEqual(item['mission_actions'], ['confirm_shipped'])
        self.assertEqual(item['action_hint'], 'confirm shipped')

    def test_other_escalation_has_no_action(self):
        item = ssl._escalation_to_waiting_item({
            'headline': 'Some other escalation',
            'context': 'unrelated',
            'source': 'promote_alerts',
        })
        self.assertNotIn('mission_actions', item)
        self.assertNotIn('mission_id', item)

    def test_reconcile_record_without_mission_id_has_no_action(self):
        item = ssl._escalation_to_waiting_item({
            'headline': 'no mission id',
            'source': 'heal_merged_pr_board_reconcile',
        })
        self.assertNotIn('mission_actions', item)


if __name__ == '__main__':
    unittest.main()
