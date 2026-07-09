#!/usr/bin/env python3
"""Tests for system_resource_watch — the transition logic that decides when the
watcher pages Larry (DM + top of Approvals) vs stays quiet.

The emit path (larry_alerts) refuses under the test sandbox by design, so these
tests drive the pure `evaluate()` transition table and `run(dry_run=True)`.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_system_resource_watch
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import sys
import tempfile
import types
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import system_resource_watch as srw  # noqa: E402


def _snap(level: str) -> dict:
    return {'verdict': {'level': level, 'headline': f'{level} headline',
                        'reasons': [], 'suggested_actions': []},
            'signals': {}}


class TransitionTests(unittest.TestCase):
    def test_green_to_critical_escalates(self) -> None:
        d = srw.evaluate(_snap('critical'), {'alerted_level': 'green'})
        self.assertEqual(d['action'], 'escalate')
        self.assertEqual(d['next_state']['alerted_level'], 'critical')

    def test_warning_to_critical_escalates(self) -> None:
        d = srw.evaluate(_snap('critical'), {'alerted_level': 'warning'})
        self.assertEqual(d['action'], 'escalate')

    def test_sustained_critical_does_not_repage(self) -> None:
        d = srw.evaluate(_snap('critical'), {'alerted_level': 'critical'})
        self.assertEqual(d['action'], 'none')
        self.assertEqual(d['next_state']['alerted_level'], 'critical')

    def test_critical_to_green_recovers(self) -> None:
        d = srw.evaluate(_snap('green'), {'alerted_level': 'critical'})
        self.assertEqual(d['action'], 'recover')
        self.assertEqual(d['next_state']['alerted_level'], 'green')

    def test_warning_never_pages(self) -> None:
        d = srw.evaluate(_snap('warning'), {'alerted_level': 'green'})
        self.assertEqual(d['action'], 'none')
        # warning is NOT recorded as an alerted level (gauge-only)
        self.assertEqual(d['next_state']['alerted_level'], 'green')

    def test_critical_down_to_warning_holds_no_repage_no_clear(self) -> None:
        # critical -> warning: still being handled, must not re-page nor clear.
        d = srw.evaluate(_snap('warning'), {'alerted_level': 'critical'})
        self.assertEqual(d['action'], 'none')
        self.assertEqual(d['next_state']['alerted_level'], 'critical')

    def test_first_ever_run_green_no_action(self) -> None:
        d = srw.evaluate(_snap('green'), {})
        self.assertEqual(d['action'], 'none')
        self.assertEqual(d['next_state']['alerted_level'], 'green')


class StateIoTests(unittest.TestCase):
    def test_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'sub' / 'state.json'
            srw._write_state({'alerted_level': 'critical', 'since': 't'}, p)
            self.assertEqual(srw._load_state(p)['alerted_level'], 'critical')

    def test_load_missing_state_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(srw._load_state(Path(td) / 'nope.json'), {})


class DryRunTests(unittest.TestCase):
    def _proc(self, td: Path, meminfo: str) -> Path:
        proc = td / 'proc'
        proc.mkdir()
        (proc / 'meminfo').write_text(meminfo)
        (proc / 'loadavg').write_text('0.1 0.1 0.1 1/1 1')
        d = proc / '1'
        d.mkdir()
        (d / 'status').write_text('Name:\tinit\nVmRSS:\t1000 kB\n')
        (d / 'cmdline').write_text('init\x00')
        return proc

    def test_dry_run_reports_action_without_writing_state(self) -> None:
        tight = ('MemTotal: 8000000 kB\nMemAvailable: 400000 kB\n'
                 'SwapTotal: 0 kB\nSwapFree: 0 kB\n')
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            proc = self._proc(tdp, tight)
            cfg = tdp / 'cfg.json'
            cfg.write_text('{}')
            state = tdp / 'state.json'
            out = srw.run(proc_root=proc, config_path=cfg, state_path=state,
                          dry_run=True)
            self.assertEqual(out['snapshot']['verdict']['level'], 'critical')
            self.assertEqual(out['action'], 'escalate')
            # dry-run must not persist
            self.assertFalse(state.exists())


class FanoutTests(unittest.TestCase):
    """The DM and the Approvals / Needs-You card are separate stores. On a
    critical the watcher must BOTH DM (larry_alerts) AND raise a card
    (for_larry_signal.upsert_record); on recovery it must resolve the card.
    Stub both modules so we assert the fan-out without touching real stores."""

    def setUp(self) -> None:
        self._saved = {k: sys.modules.get(k) for k in ('larry_alerts', 'for_larry_signal')}
        self.alerts: list[dict] = []
        self.cards: list[tuple] = []

        la = types.ModuleType('larry_alerts')
        la.append_alert = lambda **kw: (self.alerts.append(kw) or True)  # type: ignore[attr-defined]
        fls = types.ModuleType('for_larry_signal')
        fls.upsert_record = lambda key, record, **kw: self.cards.append(('upsert', key, record))  # type: ignore[attr-defined]
        fls.resolve_record = lambda key, **kw: (self.cards.append(('resolve', key)) or True)  # type: ignore[attr-defined]
        sys.modules['larry_alerts'] = la
        sys.modules['for_larry_signal'] = fls

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def _proc(self, td: Path, meminfo: str) -> Path:
        proc = td / 'proc'
        proc.mkdir()
        (proc / 'meminfo').write_text(meminfo)
        (proc / 'loadavg').write_text('0.1 0.1 0.1 1/1 1')
        d = proc / '202'
        d.mkdir()
        (d / 'status').write_text('Name:\tgit\nVmRSS:\t6000000 kB\n')
        (d / 'cmdline').write_text('git\x00repack\x00')
        return proc

    _TIGHT = ('MemTotal: 8000000 kB\nMemAvailable: 400000 kB\n'
              'SwapTotal: 0 kB\nSwapFree: 0 kB\n')
    _HEALTHY = ('MemTotal: 8000000 kB\nMemAvailable: 6400000 kB\n'
                'SwapTotal: 0 kB\nSwapFree: 0 kB\n')

    def test_escalate_fires_dm_and_raises_card(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            proc = self._proc(tdp, self._TIGHT)
            cfg = tdp / 'cfg.json'; cfg.write_text('{}')
            state = tdp / 'state.json'  # empty -> alerted green
            out = srw.run(proc_root=proc, config_path=cfg, state_path=state)
            self.assertEqual(out['action'], 'escalate')
            self.assertTrue(any(a.get('severity') == 'critical' for a in self.alerts))
            self.assertIn(('upsert', srw.NEEDS_YOU_KEY),
                          [(c[0], c[1]) for c in self.cards])
            # the card carries the culprit + a suggested action
            card = next(c[2] for c in self.cards if c[0] == 'upsert')
            self.assertTrue(card['needs_larry'])
            self.assertIn('git', card['summary'])
            self.assertTrue(card['suggested_action'])

    def test_recover_resolves_card_and_sends_closure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            proc = self._proc(tdp, self._HEALTHY)
            cfg = tdp / 'cfg.json'; cfg.write_text('{}')
            state = tdp / 'state.json'
            srw._write_state({'alerted_level': 'critical', 'since': 't'}, state)
            out = srw.run(proc_root=proc, config_path=cfg, state_path=state)
            self.assertEqual(out['action'], 'recover')
            self.assertIn(('resolve', srw.NEEDS_YOU_KEY), self.cards)
            # closure DM uses a distinct subject so a re-flap isn't cooldown-swallowed
            closure = [a for a in self.alerts if a.get('route') == 'closure']
            self.assertTrue(closure)
            self.assertEqual(closure[0].get('subject'), 'resource-recovered')

    def test_healthy_run_raises_no_card_no_dm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            proc = self._proc(tdp, self._HEALTHY)
            cfg = tdp / 'cfg.json'; cfg.write_text('{}')
            state = tdp / 'state.json'  # empty -> green
            out = srw.run(proc_root=proc, config_path=cfg, state_path=state)
            self.assertEqual(out['action'], 'none')
            self.assertEqual(self.alerts, [])
            self.assertEqual(self.cards, [])


if __name__ == '__main__':
    unittest.main()
