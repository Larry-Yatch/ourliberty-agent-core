#!/usr/bin/env python3
"""Tests for scripts/drain_board_to_beacon.py — the Parked-board → Beacon
auto-drainer. Run: python3 -m unittest tests.test_drain_board_to_beacon -v
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts'))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import drain_board_to_beacon as drain  # noqa: E402
from missions_narrator import classify_careful  # noqa: E402  (real classifier)


def _cap(cid, **over):
    base = {
        'id': cid,
        'title': f'card {cid}',
        'state': 'parked',
        'recommended_action': 'delegate',
        'origin': {'repo': 'ourliberty-agent-core', 'captured_at': f'2026-06-2{cid[-1]}T00:00:00+00:00'},
        'briefing': {'what': 'do a thing', 'why': 'because', 'suggest': 'start here'},
    }
    base.update(over)
    return base


class DrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.captures_path = Path(self.tmp) / 'captures.json'
        self.beacon_inbox = Path(self.tmp) / 'inboxes' / 'beacon'
        self.beacon_inbox.mkdir(parents=True, exist_ok=True)
        # Redirect the drain's state file into the tmp sandbox.
        self._orig_state = drain.STATE_PATH
        drain.STATE_PATH = Path(self.tmp) / 'state' / 'board-drain-state.json'
        self._orig_swi = sys.modules.get('safe_write_inbox')

    def tearDown(self):
        drain.STATE_PATH = self._orig_state
        if self._orig_swi is not None:
            sys.modules['safe_write_inbox'] = self._orig_swi
        else:
            sys.modules.pop('safe_write_inbox', None)

    def _write(self, caps):
        self.captures_path.write_text(json.dumps(
            {'schema_version': 2, 'captures': caps}, indent=2))

    def _run(self, dry_run=True, limit=5):
        return drain.run(captures_path=self.captures_path,
                         beacon_inbox=self.beacon_inbox,
                         classify_careful=classify_careful,
                         dry_run=dry_run, limit=limit)

    def _install_fake_swi(self):
        calls = []
        fake = types.ModuleType('safe_write_inbox')

        def _swi(*, target_agent, task_dict, source_agent, filename):
            calls.append({'target_agent': target_agent, 'task_dict': task_dict,
                          'source_agent': source_agent, 'filename': filename})
            return Path(self.beacon_inbox) / filename
        fake.safe_write_inbox = _swi
        sys.modules['safe_write_inbox'] = fake
        return calls

    # ---- eligibility ----
    def test_eligible_basic(self):
        self._write([_cap('c1')])
        res = self._run(dry_run=True)
        self.assertEqual(res['eligible_selected'], 1)
        self.assertTrue(res['delegated'][0].startswith('WOULD delegate c1'))

    def test_careful_keyword_skipped(self):
        self._write([_cap('c1', title='deploy the migration to production')])
        self.assertEqual(self._run()['eligible_selected'], 0)

    def test_non_core_repo_skipped(self):
        self._write([_cap('c1', origin={'repo': 'ourliberty-dashboard'})])
        self.assertEqual(self._run()['eligible_selected'], 0)

    def test_not_delegate_skipped(self):
        self._write([_cap('c1', recommended_action='promote')])
        self.assertEqual(self._run()['eligible_selected'], 0)

    def test_not_parked_skipped(self):
        self._write([_cap('c1', state='promoted')])
        self.assertEqual(self._run()['eligible_selected'], 0)

    def test_already_spawned_skipped(self):
        self._write([_cap('c1', spawned={'kind': 'delegate', 'task_id': 'delegate-c1'})])
        self.assertEqual(self._run()['eligible_selected'], 0)

    def test_inbox_proposal_skipped(self):
        self._write([_cap('c1')])
        (self.beacon_inbox / 'delegate-c1.json').write_text('{}')
        self.assertEqual(self._run()['eligible_selected'], 0)

    def test_limit_caps(self):
        self._write([_cap('c1'), _cap('c2'), _cap('c3')])
        self.assertEqual(self._run(limit=2)['eligible_selected'], 2)

    # ---- dry-run touches nothing ----
    def test_dry_run_no_side_effects(self):
        self._write([_cap('c1')])
        calls = self._install_fake_swi()
        before = self.captures_path.read_text()
        self._run(dry_run=True)
        self.assertEqual(calls, [])
        self.assertEqual(self.captures_path.read_text(), before)  # no spawned stamp
        self.assertFalse(drain.STATE_PATH.exists())  # no state write

    # ---- live path delegates, stamps, dedups ----
    def test_live_delegates_and_dedups(self):
        self._write([_cap('c1')])
        calls = self._install_fake_swi()
        res = self._run(dry_run=False)
        self.assertEqual(res['eligible_selected'], 1)
        # one inbox write, correct shape
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['target_agent'], 'beacon')
        self.assertEqual(calls[0]['filename'], 'delegate-c1.json')
        self.assertEqual(calls[0]['task_dict']['task_id'], 'delegate-c1')
        self.assertEqual(calls[0]['task_dict']['dedup_identity'], 'delegate:c1')
        # spawned ref stamped on the capture
        reg = json.loads(self.captures_path.read_text())
        self.assertEqual(reg['captures'][0]['spawned']['task_id'], 'delegate-c1')
        # state recorded
        st = json.loads(drain.STATE_PATH.read_text())
        self.assertIn('c1', st['delegated'])
        # second run: deduped (spawned + state + inbox-existence all block)
        calls.clear()
        res2 = self._run(dry_run=False)
        self.assertEqual(res2['eligible_selected'], 0)
        self.assertEqual(calls, [])

    # ---- main is a no-op when disabled ----
    def test_main_disabled_noop(self):
        os.environ.pop('OURLIBERTY_BOARD_DRAIN_ENABLED', None)
        rc = drain.main(['--once'])
        self.assertEqual(rc, 0)
        self.assertFalse(drain.STATE_PATH.exists())


if __name__ == '__main__':
    unittest.main()
