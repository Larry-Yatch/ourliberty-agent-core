#!/usr/bin/env python3
"""Tests for ci_runner_watchdog — the spare-Mac CI runner watchdog.

Every GitHub and Telegram call is injected; nothing here reaches the network
or the live ~/agents tree (OURLIBERTY_AGENTS_ROOT is redirected per test).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_ci_runner_watchdog
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import ci_runner_watchdog as w  # noqa: E402

MAC, A, B = 23, 21, 22
T0 = 1_800_000_000.0
TICK = 300.0


def runners(mac='online', a='online', b='online', mac_labels=('proofs', 'vitest'),
            a_labels=('db-proofs',), b_labels=('app', 'python'), drop_mac=False):
    out = {
        'mac-2019': {'id': MAC, 'status': mac, 'busy': False, 'labels': set(mac_labels)},
        'droplet-a': {'id': A, 'status': a, 'busy': False, 'labels': set(a_labels)},
        'droplet-b': {'id': B, 'status': b, 'busy': False, 'labels': set(b_labels)},
    }
    if drop_mac:
        del out['mac-2019']
    return out


class Fake:
    """Records every injected call; failures are opt-in per call kind."""

    def __init__(self, add_ok=True, remove_ok=True, alert_ok=True):
        self.added, self.removed, self.alerts = [], [], []
        self.add_ok, self.remove_ok, self.alert_ok = add_ok, remove_ok, alert_ok

    def add(self, rid, label):
        self.added.append((rid, label))
        return self.add_ok

    def remove(self, rid, label):
        self.removed.append((rid, label))
        return self.remove_ok

    def alert(self, kind, name, since, now, pairs):
        self.alerts.append((kind, name, since, now, list(pairs)))
        return self.alert_ok

    def tick(self, rs, state, now):
        return w.tick(rs, state, now, add_label=self.add, remove_label=self.remove, alert=self.alert)


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test and reload
    the module so LOG_FILE / STATE_FILE / KILL_SWITCH point there."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix='agents-root-')
        for sub in ('logs', 'state', 'blackboard'):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        self._orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self.root
        importlib.reload(w)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._orig
        importlib.reload(w)
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()


# -------------------- the outage path --------------------

class OutageTest(unittest.TestCase):

    def test_zeroth_tick_all_online_leaves_no_state(self):
        f = Fake()
        st = f.tick(runners(), {}, T0)
        self.assertEqual(st, {})
        self.assertEqual((f.added, f.removed, f.alerts), ([], [], []))

    def test_first_offline_tick_does_nothing_but_count(self):
        f = Fake()
        st = f.tick(runners(mac='offline'), {}, T0)
        self.assertEqual(st['mac-2019']['offline_ticks'], 1)
        self.assertEqual(st['mac-2019']['offline_since'], T0)
        self.assertEqual(f.added, [], 'no label moves on a single offline reading')
        self.assertEqual(f.alerts, [], 'no DM on a single offline reading')

    def test_second_offline_tick_moves_labels_and_alerts_once(self):
        f = Fake()
        st = f.tick(runners(mac='offline'), {}, T0)
        st = f.tick(runners(mac='offline'), st, T0 + TICK)
        self.assertEqual(sorted(f.added), [(A, 'proofs'), (B, 'vitest')])
        self.assertEqual(sorted(st['mac-2019']['fallback']), [['droplet-a', 'proofs'], ['droplet-b', 'vitest']])
        self.assertEqual(len(f.alerts), 1)
        kind, name, since, now, pairs = f.alerts[0]
        self.assertEqual((kind, name, since, now), ('offline', 'mac-2019', T0, T0 + TICK))
        self.assertEqual(sorted(pairs), [['droplet-a', 'proofs'], ['droplet-b', 'vitest']])
        self.assertEqual(st['mac-2019']['alerted_at'], T0 + TICK)

    def test_no_repeat_alert_inside_six_hours(self):
        f = Fake()
        st = {}
        for i in range(4):
            st = f.tick(runners(mac='offline'), st, T0 + i * TICK)
        self.assertEqual(len(f.alerts), 1)
        self.assertEqual(len(f.added), 2, 'labels are moved once, not per tick')

    def test_reminder_after_six_hours(self):
        f = Fake()
        st = f.tick(runners(mac='offline'), {}, T0)
        st = f.tick(runners(mac='offline'), st, T0 + TICK)
        st = f.tick(runners(mac='offline'), st, T0 + TICK + w.REMIND_SEC - 1)
        self.assertEqual(len(f.alerts), 1)
        st = f.tick(runners(mac='offline'), st, T0 + TICK + w.REMIND_SEC)
        self.assertEqual(len(f.alerts), 2)
        self.assertEqual(f.alerts[1][0], 'offline')
        self.assertEqual(f.alerts[1][2], T0, 'the reminder still names the outage start')

    def test_unregistered_runner_counts_as_offline(self):
        f = Fake()
        st = f.tick(runners(drop_mac=True), {}, T0)
        st = f.tick(runners(drop_mac=True), st, T0 + TICK)
        self.assertEqual(sorted(f.added), [(A, 'proofs'), (B, 'vitest')])
        self.assertEqual(len(f.alerts), 1)

    def test_only_roles_the_mac_carries_are_moved(self):
        f = Fake()
        rs = runners(mac='offline', mac_labels=('proofs',))
        st = f.tick(rs, {}, T0)
        st = f.tick(rs, st, T0 + TICK)
        self.assertEqual(f.added, [(A, 'proofs')])
        self.assertEqual(st['mac-2019']['fallback'], [['droplet-a', 'proofs']])

    def test_never_touches_the_watched_runners_own_labels(self):
        f = Fake()
        st = {}
        for i in range(3):
            st = f.tick(runners(mac='offline'), st, T0 + i * TICK)
        st = f.tick(runners(), st, T0 + 3 * TICK)
        self.assertFalse(any(rid == MAC for rid, _ in f.added + f.removed))


# -------------------- the fallback's own guards --------------------

class FallbackGuardTest(unittest.TestCase):

    def _outage(self, f, rs):
        st = f.tick(rs, {}, T0)
        return f.tick(rs, st, T0 + TICK)

    def test_hand_added_label_is_not_recorded(self):
        f = Fake()
        st = self._outage(f, runners(mac='offline', a_labels=('db-proofs', 'proofs')))
        self.assertEqual(f.added, [(B, 'vitest')], 'proofs was already on droplet-a')
        self.assertEqual(st['mac-2019']['fallback'], [['droplet-b', 'vitest']])

    def test_hand_added_label_is_not_removed_on_recovery(self):
        f = Fake()
        st = self._outage(f, runners(mac='offline', a_labels=('db-proofs', 'proofs')))
        st = f.tick(runners(a_labels=('db-proofs', 'proofs')), st, T0 + 2 * TICK)
        self.assertEqual(f.removed, [(B, 'vitest')])
        self.assertNotIn('mac-2019', st)

    def test_fallback_skipped_when_droplet_offline_then_retried(self):
        f = Fake()
        st = self._outage(f, runners(mac='offline', a='offline'))
        self.assertEqual(f.added, [(B, 'vitest')])
        self.assertEqual(len(f.alerts), 1, 'Larry is told even when the fallback is partial')
        st = f.tick(runners(mac='offline'), st, T0 + 2 * TICK)
        self.assertEqual(sorted(f.added), [(A, 'proofs'), (B, 'vitest')], 'retried once droplet-a is back')
        self.assertEqual(len(f.alerts), 1, 'the retry is silent')

    def test_add_failure_is_not_recorded_and_is_retried(self):
        f = Fake(add_ok=False)
        st = self._outage(f, runners(mac='offline'))
        self.assertEqual(st['mac-2019']['fallback'], [])
        f.add_ok = True
        st = f.tick(runners(mac='offline'), st, T0 + 2 * TICK)
        self.assertEqual(sorted(st['mac-2019']['fallback']), [['droplet-a', 'proofs'], ['droplet-b', 'vitest']])

    def test_alert_failure_retries_next_tick(self):
        f = Fake(alert_ok=False)
        st = self._outage(f, runners(mac='offline'))
        self.assertIsNone(st['mac-2019']['alerted_at'])
        f.alert_ok = True
        st = f.tick(runners(mac='offline'), st, T0 + 2 * TICK)
        self.assertEqual(len(f.alerts), 2)
        self.assertEqual(st['mac-2019']['alerted_at'], T0 + 2 * TICK)


# -------------------- recovery --------------------

class RecoveryTest(unittest.TestCase):

    def _outage(self, f):
        st = f.tick(runners(mac='offline'), {}, T0)
        return f.tick(runners(mac='offline'), st, T0 + TICK)

    def test_recovery_removes_only_recorded_labels_and_says_back(self):
        f = Fake()
        st = self._outage(f)
        st = f.tick(runners(a_labels=('db-proofs', 'proofs'), b_labels=('app', 'python', 'vitest')), st, T0 + 2 * TICK)
        self.assertEqual(sorted(f.removed), [(A, 'proofs'), (B, 'vitest')])
        self.assertEqual(f.alerts[-1][0], 'back')
        self.assertEqual(f.alerts[-1][2], T0)
        self.assertEqual(sorted(f.alerts[-1][4]), [['droplet-a', 'proofs'], ['droplet-b', 'vitest']])
        self.assertNotIn('mac-2019', st)

    def test_blip_before_action_recovers_silently(self):
        f = Fake()
        st = f.tick(runners(mac='offline'), {}, T0)
        st = f.tick(runners(), st, T0 + TICK)
        self.assertEqual(st, {})
        self.assertEqual(f.alerts, [])
        st = f.tick(runners(mac='offline'), st, T0 + 2 * TICK)
        self.assertEqual(st['mac-2019']['offline_ticks'], 1, 'the count restarted')

    def test_remove_failure_keeps_state_and_retries(self):
        f = Fake(remove_ok=False)
        st = self._outage(f)
        st = f.tick(runners(), st, T0 + 2 * TICK)
        self.assertIn('mac-2019', st)
        self.assertEqual(sorted(st['mac-2019']['fallback']), [['droplet-a', 'proofs'], ['droplet-b', 'vitest']])
        self.assertTrue(st['mac-2019']['back_sent'], 'the back DM went out once')
        f.remove_ok = True
        st = f.tick(runners(), st, T0 + 3 * TICK)
        self.assertNotIn('mac-2019', st)
        self.assertEqual([a[0] for a in f.alerts], ['offline', 'back'], 'back is not repeated')

    def test_back_alert_failure_is_retried_without_re_removing(self):
        f = Fake(alert_ok=True)
        st = self._outage(f)
        f.alert_ok = False
        st = f.tick(runners(), st, T0 + 2 * TICK)
        self.assertFalse(st['mac-2019']['back_sent'])
        self.assertEqual(len(f.removed), 2)
        f.alert_ok = True
        st = f.tick(runners(), st, T0 + 3 * TICK)
        self.assertEqual(len(f.removed), 2, 'labels were not removed twice')
        self.assertEqual(f.alerts[-1][0], 'back')
        self.assertNotIn('mac-2019', st)

    def test_new_outage_after_recovery_alerts_again(self):
        f = Fake(remove_ok=False)
        st = self._outage(f)
        st = f.tick(runners(), st, T0 + 2 * TICK)      # back DM sent; labels still on
        self.assertTrue(st['mac-2019']['back_sent'])
        st = f.tick(runners(mac='offline'), st, T0 + 3 * TICK)
        self.assertEqual(st['mac-2019']['offline_ticks'], 1)
        self.assertEqual(st['mac-2019']['offline_since'], T0 + 3 * TICK)
        self.assertEqual(sorted(st['mac-2019']['fallback']), [['droplet-a', 'proofs'], ['droplet-b', 'vitest']],
                         'the labels still on the droplet stay recorded as ours')
        st = f.tick(runners(mac='offline'), st, T0 + 4 * TICK)
        self.assertEqual([a[0] for a in f.alerts], ['offline', 'back', 'offline'])
        self.assertEqual(len(f.added), 2, 'labels we still hold are not added again')

    def test_second_outage_after_muted_back_dm_pages(self):
        """Review finding: a recovery whose 'back' DM was swallowed by the
        alert cooldown used to leave the old outage open, so the next outage
        inherited its alerted_at and never paged until the 6 h reminder."""
        f = Fake()
        st = self._outage(f)
        f.alert_ok = False
        st = f.tick(runners(), st, T0 + 2 * TICK)      # labels removed, back DM muted
        self.assertIn('mac-2019', st)
        self.assertFalse(st['mac-2019']['back_sent'])
        f.alert_ok = True
        st = f.tick(runners(mac='offline'), st, T0 + 3 * TICK)
        self.assertEqual(st['mac-2019']['offline_since'], T0 + 3 * TICK, 'a fresh outage clock')
        st = f.tick(runners(mac='offline'), st, T0 + 4 * TICK)
        self.assertEqual([a[0] for a in f.alerts], ['offline', 'back', 'offline'])
        self.assertEqual(st['mac-2019']['alerted_at'], T0 + 4 * TICK)

    def test_vanished_runner_label_counts_as_removed(self):
        """Review finding: a label recorded on a runner that is no longer
        registered has nothing left to remove; it must not block recovery."""
        f = Fake()
        st = self._outage(f)
        rs = runners()
        del rs['droplet-b']
        st = f.tick(rs, st, T0 + 2 * TICK)
        self.assertEqual(f.removed, [(A, 'proofs')])
        self.assertNotIn('mac-2019', st)
        self.assertEqual(f.alerts[-1][0], 'back')


# -------------------- a blind watchdog --------------------

class FetchFailureTest(unittest.TestCase):

    def test_failures_below_threshold_are_quiet(self):
        f = Fake()
        st = {'mac-2019': {'offline_ticks': 1, 'offline_since': T0, 'alerted_at': None,
                           'fallback': [], 'back_sent': False}}
        for i in range(w.FETCH_FAILURES_BEFORE_ALERT - 1):
            st = w.note_fetch_result(st, False, T0 + i * TICK, f.alert)
        self.assertEqual(f.alerts, [])
        self.assertEqual(st['mac-2019']['offline_ticks'], 1, 'an unreadable GitHub is not an offline tick')

    def test_blind_alert_once_then_reminder(self):
        f = Fake()
        st = {}
        for i in range(w.FETCH_FAILURES_BEFORE_ALERT + 3):
            st = w.note_fetch_result(st, False, T0 + i * TICK, f.alert)
        self.assertEqual([a[0] for a in f.alerts], ['blind'])
        st = w.note_fetch_result(st, False, T0 + w.REMIND_SEC + 100 * TICK, f.alert)
        self.assertEqual([a[0] for a in f.alerts], ['blind', 'blind'])
        st = w.note_fetch_result(st, True, T0 + w.REMIND_SEC + 101 * TICK, f.alert)
        self.assertNotIn('_fetch', st)


# -------------------- wiring: main() --------------------

class MainTest(_IsolatedAgentsRoot):

    def test_kill_switch_exits_without_fetch(self):
        Path(self.root, 'healers.disabled').touch()
        with patch.object(w, 'fetch_runners') as fetch:
            self.assertEqual(w.main(), 0)
        fetch.assert_not_called()

    def test_fetch_failure_leaves_state_file_untouched(self):
        before = {'mac-2019': {'offline_ticks': 1, 'offline_since': T0, 'alerted_at': None,
                               'fallback': [], 'back_sent': False}}
        w.STATE_FILE.write_text(json.dumps(before))
        with patch.object(w, 'fetch_runners', return_value=None), \
                patch.object(w, 'send_alert', return_value=True), \
                patch.object(w, 'add_label') as add:
            self.assertEqual(w.main(), 0)
        add.assert_not_called()
        after = json.loads(w.STATE_FILE.read_text())
        self.assertEqual(after['mac-2019'], before['mac-2019'])
        self.assertEqual(after.get('_fetch', {}).get('failures'), 1, 'the failed read was counted')

    def test_corrupt_state_file_is_the_zeroth_run(self):
        w.STATE_FILE.write_text('{not json')
        with patch.object(w, 'fetch_runners', return_value=runners()), \
                patch.object(w, 'send_alert', return_value=True):
            self.assertEqual(w.main(), 0)
        self.assertEqual(json.loads(w.STATE_FILE.read_text()), {})
        self.assertTrue(w.HEARTBEAT_FILE.exists())

    def test_main_runs_the_tick_and_persists(self):
        w.STATE_FILE.write_text(json.dumps({'mac-2019': {
            'offline_ticks': 1, 'offline_since': T0, 'alerted_at': None, 'fallback': [], 'back_sent': False}}))
        sent = []
        with patch.object(w, 'fetch_runners', return_value=runners(mac='offline')), \
                patch.object(w, 'send_alert', side_effect=lambda *a: sent.append(a) or True), \
                patch.object(w, 'add_label', return_value=True) as add:
            self.assertEqual(w.main(), 0)
        self.assertEqual(sorted(c.args for c in add.call_args_list), [(A, 'proofs'), (B, 'vitest')])
        self.assertEqual([s[0] for s in sent], ['offline'])
        st = json.loads(w.STATE_FILE.read_text())
        self.assertEqual(sorted(st['mac-2019']['fallback']), [['droplet-a', 'proofs'], ['droplet-b', 'vitest']])


# -------------------- the words --------------------

class MessageTest(unittest.TestCase):

    def test_offline_message_names_the_runner_duration_and_the_fallback(self):
        m = w.offline_message('mac-2019', T0, T0 + 2 * 3600 + 5 * 60, [['droplet-a', 'proofs'], ['droplet-b', 'vitest']])
        self.assertIn('mac-2019', m)
        self.assertIn('2 h 05 min', m)
        self.assertIn('proofs → droplet-a', m)
        self.assertIn('vitest → droplet-b', m)

    def test_every_subject_has_a_translation_and_the_offline_one_says_what_to_do(self):
        """The DM renders the translation ABOVE the message, so the walk-to-
        the-Mac instruction lives there; this pins the three entries to the
        three subjects send_alert uses."""
        w.larry_alerts._TRANSLATIONS_CACHE = None  # noqa: SLF001
        for subject in ('offline', 'back', 'blind'):
            self.assertIsNotNone(w.larry_alerts.translate_alert(w.SOURCE, subject), subject)
        offline = w.larry_alerts.translate_alert(w.SOURCE, 'offline')
        self.assertIn('power button', offline['recommended_action'])
        self.assertIn('launchctl', offline['recommended_action'])
        self.assertEqual(offline['tier'], 'NOW')

    def test_offline_message_without_fallback_says_checks_are_waiting(self):
        m = w.offline_message('mac-2019', T0, T0 + 600, [])
        self.assertIn('could not be applied yet', m)
        self.assertNotIn('moved them', m)

    def test_back_message(self):
        self.assertIn('fallback is removed', w.back_message('mac-2019', T0, T0 + 60, [['droplet-a', 'proofs']]))
        self.assertTrue(w.back_message('mac-2019', T0, T0 + 60, []).endswith('on it again.'))

    def test_offline_alert_routes_to_larry(self):
        with patch.object(w.larry_alerts, 'append_alert', return_value=True) as aa:
            self.assertTrue(w.send_alert('offline', 'mac-2019', T0, T0 + 600, []))
        kw = aa.call_args.kwargs
        self.assertEqual((aa.call_args.args[0], aa.call_args.args[1]), (w.SOURCE, 'warning'))
        self.assertEqual((kw.get('route'), kw.get('needs_larry'), kw.get('subject')),
                         ('escalate', True, 'offline'))
        self.assertEqual(kw.get('suggested_action'), w.MAC_SERVICE_CMD)

    def test_back_alert_is_a_dm_too(self):
        with patch.object(w.larry_alerts, 'append_alert', return_value=True) as aa:
            w.send_alert('back', 'mac-2019', T0, T0 + 600, [])
        self.assertEqual(aa.call_args.kwargs.get('route'), 'escalate', 'an info alert defaults to digest; this one must DM')
        self.assertEqual(aa.call_args.args[1], 'warning',
                         "info's 6 h cooldown would swallow the back DM of a second outage the same afternoon")
        self.assertEqual(aa.call_args.kwargs.get('subject'), 'back')


# -------------------- gh plumbing --------------------

class GhTest(_IsolatedAgentsRoot):

    def _run(self, rc, out='', err=''):
        class R:
            returncode, stdout, stderr = rc, out, err
        return R()

    def test_fetch_runners_parses_the_payload(self):
        payload = {'runners': [
            {'id': 23, 'name': 'mac-2019', 'status': 'offline', 'busy': False,
             'labels': [{'name': 'proofs'}, {'name': 'vitest'}]},
            {'id': 21, 'name': 'droplet-a', 'status': 'online', 'busy': True, 'labels': [{'name': 'db-proofs'}]},
        ]}
        with patch.object(w.subprocess, 'run', return_value=self._run(0, json.dumps(payload))):
            rs = w.fetch_runners()
        self.assertEqual(rs['mac-2019'], {'id': 23, 'status': 'offline', 'busy': False, 'labels': {'proofs', 'vitest'}})
        self.assertEqual(rs['droplet-a']['busy'], True)

    def test_fetch_runners_returns_none_on_gh_failure(self):
        with patch.object(w.subprocess, 'run', return_value=self._run(1, '', 'HTTP 401')):
            self.assertIsNone(w.fetch_runners())
        with patch.object(w.subprocess, 'run', return_value=self._run(0, 'not json')):
            self.assertIsNone(w.fetch_runners())
        with patch.object(w.subprocess, 'run', return_value=self._run(0, json.dumps({'total_count': 0}))):
            self.assertIsNone(w.fetch_runners())

    def test_remove_label_treats_404_as_gone(self):
        with patch.object(w, 'gh_write', return_value=self._run(1, '', 'gh: Not Found (HTTP 404)')):
            self.assertTrue(w.remove_label(21, 'proofs'))
        with patch.object(w, 'gh_write', return_value=self._run(1, '', 'HTTP 500')):
            self.assertFalse(w.remove_label(21, 'proofs'))

    def test_add_label_command_shape(self):
        with patch.object(w, 'gh_write', return_value=self._run(0, '{}')) as gw:
            self.assertTrue(w.add_label(21, 'proofs'))
        self.assertEqual(gw.call_args.args[0],
                         ['gh', 'api', '-X', 'POST', f'repos/{w.REPO}/actions/runners/21/labels', '-f', 'labels[]=proofs'])
        with patch.object(w, 'gh_write', return_value=self._run(1, '', 'HTTP 403')):
            self.assertFalse(w.add_label(21, 'proofs'))

    def test_label_writes_go_through_the_guarded_wrapper(self):
        """The chokepoint census (test_chokepoint_census) requires every
        destructive gh write to go through test_isolation_guard.gh_write,
        which refuses to run from a test process. Pin it from this side too:
        a bare subprocess.run must never carry a POST or DELETE."""
        with patch.object(w, 'gh_write', return_value=self._run(0, '{}')) as gw, \
                patch.object(w.subprocess, 'run') as run:
            w.add_label(21, 'proofs')
            w.remove_label(22, 'vitest')
        self.assertEqual([c.args[0][2:4] for c in gw.call_args_list], [['-X', 'POST'], ['-X', 'DELETE']])
        run.assert_not_called()

    def test_gh_write_subprocess_failure_is_false(self):
        with patch.object(w, 'gh_write', side_effect=OSError('no gh')):
            self.assertFalse(w.add_label(21, 'proofs'))
            self.assertFalse(w.remove_label(21, 'proofs'))


if __name__ == '__main__':
    unittest.main()
