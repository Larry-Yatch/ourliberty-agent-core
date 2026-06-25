#!/usr/bin/env python3
"""Tests for scripts/doorbell_notifier.py — the cooldown-gated "N items need
your call" nudge.

Path-isolation: a tmp OURLIBERTY_AGENTS_ROOT + OURLIBERTY_SYSTEM_STATE_LOG, set
before importlib.reload() so the module's path constants re-resolve. larry_alerts
.append_notification is mocked (so the real test-jail-guarded writer never runs)
and its calls are asserted. `run(now=...)` takes an injected clock so the
reminder window is testable without sleeping.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_doorbell_notifier
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
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
_LARRY_CHAT = 7998341473


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.agents = root / 'agents'
        (self.agents / 'state').mkdir(parents=True)
        (self.agents / 'blackboard').mkdir(parents=True)
        (self.agents / 'logs').mkdir(parents=True)
        self.snapshot_path = self.agents / 'blackboard' / 'system-state-log.json'
        self._env = {
            'OURLIBERTY_AGENTS_ROOT': str(self.agents),
            'OURLIBERTY_SYSTEM_STATE_LOG': str(self.snapshot_path),
            'TELEGRAM_ALLOWED_CHAT_IDS': str(_LARRY_CHAT),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        import doorbell_notifier as dn
        self.dn = importlib.reload(dn)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # ---- fixtures ----

    def write_snapshot(self, *, pending_approvals=0, escalations=0, parked=0,
                       items=None):
        doc = {
            'schema_version': 1,
            'narrative_prose': 'x',
            'structured_snapshot': {
                'waiting_on_larry': {
                    'parked': parked,
                    'pending_approvals': pending_approvals,
                    'escalations': escalations,
                    'total': parked + pending_approvals + escalations,
                    'items': items or [],
                    'truncated': False,
                },
            },
        }
        self.snapshot_path.write_text(json.dumps(doc))

    @staticmethod
    def item(source, title):
        """A minimal blocking waiting-item as the State Log emits them."""
        return {'source': source, 'id': title, 'title': title,
                'why': '', 'severity': 'warning'}

    def write_state(self, **state):
        (self.agents / 'state' / 'doorbell-state.json').write_text(
            json.dumps(state))

    def read_state(self):
        p = self.agents / 'state' / 'doorbell-state.json'
        return json.loads(p.read_text()) if p.exists() else None

    def run_tick(self, now=_NOW):
        with mock.patch.object(
            self.dn.larry_alerts, 'append_notification',
            return_value=True,
        ) as m:
            sent = self.dn.run(now=now)
        return sent, m


class DoorbellFiringTest(_Base):
    def test_increase_from_zero_fires_dm(self):
        self.write_snapshot(pending_approvals=2)
        sent, m = self.run_tick()
        self.assertTrue(sent)
        m.assert_called_once()
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs['intent'], 'doorbell')
        self.assertEqual(kwargs['source'], 'doorbell')
        self.assertEqual(kwargs['chat_id'], _LARRY_CHAT)
        self.assertIn('2 items need your call', kwargs['message'])
        # The dead words "check the board" are now a real clickable link.
        self.assertIn('https://dashboard.ourliberty.dev/approvals', kwargs['message'])
        st = self.read_state()
        self.assertEqual(st['last_count'], 2)
        self.assertEqual(st['last_dm_ts'], _NOW.isoformat())

    def test_same_count_within_window_suppressed(self):
        self.write_state(
            last_count=2, last_dm_ts=(_NOW - timedelta(hours=1)).isoformat())
        self.write_snapshot(pending_approvals=2)
        sent, m = self.run_tick()
        self.assertFalse(sent)
        m.assert_not_called()
        self.assertEqual(self.read_state()['last_count'], 2)

    def test_increase_within_window_fires(self):
        self.write_state(
            last_count=2, last_dm_ts=(_NOW - timedelta(minutes=10)).isoformat())
        self.write_snapshot(pending_approvals=2, escalations=1)
        sent, m = self.run_tick()
        self.assertTrue(sent)  # 3 > 2 — a new item, ping promptly
        self.assertIn('3 items need your call', m.call_args.kwargs['message'])
        # both kinds → light breakdown
        self.assertIn('2 to approve, 1 escalated', m.call_args.kwargs['message'])

    def test_reminder_after_window(self):
        self.write_state(
            last_count=2, last_dm_ts=(_NOW - timedelta(hours=5)).isoformat())
        self.write_snapshot(pending_approvals=2)
        sent, m = self.run_tick()
        self.assertTrue(sent)  # same count but past the 4h reminder window
        self.assertEqual(self.read_state()['last_dm_ts'], _NOW.isoformat())

    def test_zero_resets_and_no_dm(self):
        self.write_state(
            last_count=3, last_dm_ts=(_NOW - timedelta(hours=1)).isoformat())
        self.write_snapshot(pending_approvals=0, escalations=0)
        sent, m = self.run_tick()
        self.assertFalse(sent)
        m.assert_not_called()
        self.assertEqual(self.read_state()['last_count'], 0)


class DoorbellScopeTest(_Base):
    def test_parked_excluded_from_count(self):
        # 20 parked but no approvals/escalations → nothing blocks → no DM.
        self.write_snapshot(pending_approvals=0, escalations=0, parked=20)
        sent, m = self.run_tick()
        self.assertFalse(sent)
        m.assert_not_called()

    def test_escalations_count(self):
        self.write_snapshot(escalations=1)
        sent, m = self.run_tick()
        self.assertTrue(sent)
        self.assertIn('1 item needs your call', m.call_args.kwargs['message'])


class DoorbellFailOpenTest(_Base):
    def test_no_snapshot_no_dm(self):
        sent, m = self.run_tick()
        self.assertFalse(sent)
        m.assert_not_called()

    def test_malformed_snapshot_no_dm(self):
        self.snapshot_path.write_text("{ this is not json")
        sent, m = self.run_tick()
        self.assertFalse(sent)
        m.assert_not_called()

    def test_no_chat_id_skips_but_records_count(self):
        os.environ.pop('TELEGRAM_ALLOWED_CHAT_IDS', None)
        self.dn = importlib.reload(self.dn)  # _primary_chat_id reads env live
        self.write_snapshot(pending_approvals=2)
        sent, m = self.run_tick()
        self.assertFalse(sent)
        m.assert_not_called()
        self.assertEqual(self.read_state()['last_count'], 2)

    def test_main_never_raises(self):
        with mock.patch.object(self.dn, 'run', side_effect=RuntimeError('boom')):
            self.assertEqual(self.dn.main(), 0)

    def test_malformed_reminder_env_does_not_wedge_import(self):
        # A bad OURLIBERTY_DOORBELL_REMINDER_HOURS must NOT raise at import
        # (that would wedge the oneshot before main()'s guard) — fall back to 4h.
        prev = os.environ.get('OURLIBERTY_DOORBELL_REMINDER_HOURS')
        os.environ['OURLIBERTY_DOORBELL_REMINDER_HOURS'] = 'not-a-number'
        try:
            dn = importlib.reload(self.dn)
            self.assertEqual(dn.REMINDER_WINDOW_HOURS, 4.0)
        finally:
            if prev is None:
                os.environ.pop('OURLIBERTY_DOORBELL_REMINDER_HOURS', None)
            else:
                os.environ['OURLIBERTY_DOORBELL_REMINDER_HOURS'] = prev
            self.dn = importlib.reload(self.dn)


class DoorbellMessageTest(_Base):
    BASE = 'https://dashboard.ourliberty.dev'

    # ---- fallback path: counts only, no itemized detail ----

    def test_fallback_singular_vs_plural_and_link(self):
        # Pure approvals → the /approvals surface where you act on them.
        self.assertEqual(
            self.dn.format_message(1, 1, 0),
            f'1 item needs your call.\n→ {self.BASE}/approvals',
        )
        self.assertEqual(
            self.dn.format_message(2, 2, 0),
            f'2 items need your call.\n→ {self.BASE}/approvals',
        )

    def test_fallback_breakdown_and_escalation_routes_to_board(self):
        # Mixed kinds → light breakdown + the umbrella board (/where-we-are).
        self.assertEqual(
            self.dn.format_message(3, 2, 1),
            '3 items need your call. (2 to approve, 1 escalated)\n'
            f'→ {self.BASE}/where-we-are',
        )

    # ---- itemized path: names WHAT + links WHERE ----

    def test_names_items_and_links(self):
        items = [self.item('approval', 'restart dashboard-api healer')]
        msg = self.dn.format_message(1, 1, 0, items)
        self.assertEqual(
            msg,
            '1 item needs your call:\n'
            '• Approve — restart dashboard-api healer\n'
            f'→ {self.BASE}/approvals',
        )

    def test_escalation_present_routes_to_where_we_are(self):
        items = [self.item('approval', 'a'), self.item('escalation', 'b')]
        msg = self.dn.format_message(2, 1, 1, items)
        self.assertIn('• Approve — a', msg)
        self.assertIn('• Escalation — b', msg)
        self.assertTrue(msg.endswith(f'→ {self.BASE}/where-we-are'))

    def test_caps_named_items_with_more(self):
        items = [self.item('approval', f't{i}') for i in range(5)]
        msg = self.dn.format_message(5, 5, 0, items)
        # Exactly MAX_NAMED_ITEMS named, the rest collapse to "+N more".
        self.assertEqual(msg.count('• Approve'), self.dn.MAX_NAMED_ITEMS)
        self.assertIn(f'• +{5 - self.dn.MAX_NAMED_ITEMS} more', msg)

    def test_internal_newline_collapsed_to_one_line(self):
        items = [self.item('approval', 'restart healer\nthen redeploy\n  api')]
        msg = self.dn.format_message(1, 1, 0, items)
        self.assertIn('• Approve — restart healer then redeploy api', msg)
        # The bullet must be a single line (no wrap that reads as a 2nd item).
        bullets = [ln for ln in msg.splitlines() if ln.startswith('• ')]
        self.assertEqual(len(bullets), 1)

    def test_long_title_trimmed(self):
        items = [self.item('approval', 'x' * 200)]
        msg = self.dn.format_message(1, 1, 0, items)
        bullet = [ln for ln in msg.splitlines() if ln.startswith('• ')][0]
        self.assertLessEqual(len(bullet), len('• Approve — ') + self.dn.TITLE_MAXLEN)
        self.assertTrue(bullet.endswith('…'))


class DoorbellItemizedRunTest(_Base):
    def test_run_names_items_from_snapshot(self):
        items = [
            DoorbellMessageTest.item('approval', 'dispatch to forge: build X'),
            DoorbellMessageTest.item('escalation', 'credential rotation due'),
        ]
        self.write_snapshot(pending_approvals=1, escalations=1, items=items)
        sent, m = self.run_tick()
        self.assertTrue(sent)
        msg = m.call_args.kwargs['message']
        self.assertIn('2 items need your call:', msg)
        self.assertIn('• Approve — dispatch to forge: build X', msg)
        self.assertIn('• Escalation — credential rotation due', msg)
        self.assertIn('/where-we-are', msg)

    def test_run_drops_parked_items_from_names(self):
        # Parked items ride along in waiting_on_larry.items but are NOT blocking
        # — the doorbell must not name them (they're excluded from the count too).
        items = [
            DoorbellMessageTest.item('approval', 'the only blocking thing'),
            DoorbellMessageTest.item('parked', 'some funnel suggestion'),
        ]
        self.write_snapshot(pending_approvals=1, items=items)
        sent, m = self.run_tick()
        msg = m.call_args.kwargs['message']
        self.assertIn('1 item needs your call:', msg)
        self.assertIn('• Approve — the only blocking thing', msg)
        self.assertNotIn('some funnel suggestion', msg)


if __name__ == '__main__':
    unittest.main()
