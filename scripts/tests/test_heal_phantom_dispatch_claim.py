#!/usr/bin/env python3
"""Tests for heal_phantom_dispatch_claim (the phantom-dispatch watcher).

Covers the brief's acceptance set:
  - the exact 2026-06-03 message with no matching artifact -> one
    phantom-dispatch:deploy-notifier-ready-logonly alert after the grace window
  - a claim WITH a matching artifact (live inbox, build- prefix, .archive,
    .invalid, in-flight, worktree, outbox-notifier line) -> no alert
  - a claim still inside the grace window -> no alert yet
  - run twice -> alerted exactly once (idempotent)
  - a routine non-dispatch message -> no alert
plus line parsing (repr decode + tz ts), task_id extraction, config fail-loud,
the shipped config, and main() routing through a stubbed larry_alerts.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_phantom_dispatch_claim
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_phantom_dispatch_claim as h  # noqa: E402


NOW = 1_750_000_000.0  # fixed epoch for deterministic grace math
MIN = 60.0
PHANTOM_TASK = 'deploy-notifier-ready-logonly'
# The real 2026-06-03 message (as the bot truncates + repr's it).
PHANTOM_TEXT = (
    'Approved — `deploy-notifier-ready-logonly` dispatches to Forge now. '
    'Once it lands, Vercel READY deploys stop paging you'
)
CHAT_ID = '7998341473'


def _log_line(text: str, age_min: float, now: float = NOW,
              chat: str = CHAT_ID) -> str:
    ts = datetime.fromtimestamp(now - age_min * MIN, tz=timezone.utc)
    return f"[{ts.strftime('%Y-%m-%dT%H:%M:%S%z')}] -> {chat}: {text!r}"


def _load_cfg() -> dict:
    return h.load_config()  # the shipped config drives the real phrase list


class LineParseTest(unittest.TestCase):
    def test_parses_exact_phantom_line(self):
        line = _log_line(PHANTOM_TEXT, age_min=15)
        parsed = h.parse_outbound_line(line)
        self.assertIsNotNone(parsed)
        claim_ts, chat, text = parsed
        self.assertEqual(chat, CHAT_ID)
        self.assertAlmostEqual(claim_ts, NOW - 15 * MIN, delta=2)
        self.assertEqual(text, PHANTOM_TEXT)

    def test_double_quoted_repr_decodes(self):
        # repr() picks double quotes when the string contains a single quote.
        text = "It's dispatched to Forge now `some-real-task-id`"
        line = _log_line(text, age_min=12)
        parsed = h.parse_outbound_line(line)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[2], text)

    def test_non_outbound_line_ignored(self):
        # The bot's own dispatch-confirmation line has no `-> <digits>:`.
        line = ('[2026-06-03T19:45:28-0600] approved orchestrator-spec -> '
                'dispatched to /tmp/inboxes/forge/x.json')
        self.assertIsNone(h.parse_outbound_line(line))

    def test_bad_timestamp_dropped(self):
        self.assertIsNone(
            h.parse_outbound_line("[not-a-ts] -> 123: 'dispatched to Forge'"))


class ClaimDetectionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _load_cfg()

    def test_phantom_is_dispatch_claim(self):
        self.assertTrue(h.is_dispatch_claim(PHANTOM_TEXT, self.cfg))
        self.assertTrue(h.is_strong_claim(PHANTOM_TEXT, self.cfg))

    def test_extract_task_id_prefers_backtick(self):
        self.assertEqual(
            h.extract_task_id(PHANTOM_TEXT, self.cfg), PHANTOM_TASK)

    def test_extract_task_id_bare_kebab_fallback(self):
        text = 'dispatched to Forge: build-the-thing-now'
        self.assertEqual(
            h.extract_task_id(text, self.cfg), 'build-the-thing-now')

    def test_extract_task_id_ignores_short_hyphenated_words(self):
        # 'log-only' is one hyphen; min_bare_kebab_hyphens defaults to 2 so it
        # is not mistaken for a task_id.
        self.assertIsNone(
            h.extract_task_id('it goes to Forge now, log-only', self.cfg))

    def test_routine_message_not_a_claim(self):
        for text in (
            'Morning — here is the digest. 3 PRs merged, 0 stalls.',
            'I looked at the build-sequence-advancer logs; all green.',
            'Pulse cycle complete. Nothing needs your attention.',
        ):
            self.assertFalse(h.is_dispatch_claim(text, self.cfg), text)


class DispatchExistsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.env = mock.patch.dict(os.environ, {
            'OURLIBERTY_AGENTS_ROOT': str(self.root / 'agents'),
            'OURLIBERTY_LOG_DIR': str(self.root / 'agents' / 'logs'),
            'OURLIBERTY_WORKTREES_ROOT': str(self.root / 'worktrees'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        h.forge_inbox_dir().mkdir(parents=True, exist_ok=True)
        (h.forge_inbox_dir() / '.archive').mkdir(exist_ok=True)
        (h.forge_inbox_dir() / '.invalid').mkdir(exist_ok=True)
        h.in_flight_dir().mkdir(parents=True, exist_ok=True)
        h.worktrees_root().mkdir(parents=True, exist_ok=True)
        h.log_dir().mkdir(parents=True, exist_ok=True)

    def test_no_artifact_is_false(self):
        exists, _ = h.dispatch_exists(PHANTOM_TASK)
        self.assertFalse(exists)

    def test_live_inbox_file(self):
        (h.forge_inbox_dir() / f'{PHANTOM_TASK}.json').write_text('{}')
        self.assertTrue(h.dispatch_exists(PHANTOM_TASK)[0])

    def test_build_prefixed_inbox_file(self):
        (h.forge_inbox_dir() / f'build-{PHANTOM_TASK}.json').write_text('{}')
        self.assertTrue(h.dispatch_exists(PHANTOM_TASK)[0])

    def test_archived_inbox_file(self):
        (h.forge_inbox_dir() / '.archive' / f'{PHANTOM_TASK}.json').write_text('{}')
        self.assertTrue(h.dispatch_exists(PHANTOM_TASK)[0])

    def test_invalid_inbox_file(self):
        (h.forge_inbox_dir() / '.invalid' / f'{PHANTOM_TASK}.json').write_text('{}')
        self.assertTrue(h.dispatch_exists(PHANTOM_TASK)[0])

    def test_in_flight_file(self):
        (h.in_flight_dir() / f'{PHANTOM_TASK}.json').write_text('{}')
        self.assertTrue(h.dispatch_exists(PHANTOM_TASK)[0])

    def test_worktree_dir(self):
        (h.worktrees_root() / f'wt-forge-{PHANTOM_TASK}').mkdir()
        self.assertTrue(h.dispatch_exists(PHANTOM_TASK)[0])

    def test_outbox_log_line(self):
        h.outbox_log_file().write_text(
            f'[2026-06-03 23:44:46] [notifier] [INFO] build-phase dispatched '
            f'forge <- beacon (task={PHANTOM_TASK}, file=build-{PHANTOM_TASK}.json)\n'
        )
        self.assertTrue(h.dispatch_exists(PHANTOM_TASK)[0])


class _Env:
    """Temp env with the three roots set + a writable beacon log."""

    def __init__(self, case: unittest.TestCase):
        self._td = tempfile.TemporaryDirectory()
        case.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        patch = mock.patch.dict(os.environ, {
            'OURLIBERTY_AGENTS_ROOT': str(self.root / 'agents'),
            'OURLIBERTY_LOG_DIR': str(self.root / 'agents' / 'logs'),
            'OURLIBERTY_WORKTREES_ROOT': str(self.root / 'worktrees'),
        })
        patch.start()
        case.addCleanup(patch.stop)
        for d in (h.forge_inbox_dir(), h.in_flight_dir(),
                  h.worktrees_root(), h.log_dir(), h.blackboard()):
            d.mkdir(parents=True, exist_ok=True)

    def write_log(self, *lines: str) -> None:
        h.beacon_log_file().write_text('\n'.join(lines) + '\n')


class EvaluateAndRunOnceTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _load_cfg()
        self.env = _Env(self)

    def test_phantom_after_grace_alerts_once(self):
        self.env.write_log(_log_line(PHANTOM_TEXT, age_min=15))
        alerts = h.run_once(NOW, self.cfg, alerted=set())
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'], f'phantom-dispatch:{PHANTOM_TASK}')
        self.assertIn(PHANTOM_TASK, alerts[0]['message'])

    def test_inside_grace_no_alert(self):
        self.env.write_log(_log_line(PHANTOM_TEXT, age_min=3))  # < 10-min grace
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_real_dispatch_suppresses(self):
        self.env.write_log(_log_line(PHANTOM_TEXT, age_min=15))
        (h.worktrees_root() / f'wt-forge-{PHANTOM_TASK}').mkdir()
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_dedup_skips_already_alerted(self):
        self.env.write_log(_log_line(PHANTOM_TEXT, age_min=15))
        first = h.run_once(NOW, self.cfg, alerted=set())
        self.assertEqual(len(first), 1)
        already = {first[0]['key']}
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=already), [])

    def test_routine_message_no_alert(self):
        self.env.write_log(
            _log_line('Digest is out — 3 PRs merged, nothing for you.', 15))
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])

    def test_outside_scan_window_ignored(self):
        # 45 min old > 30-min scan window: not even parsed as a claim.
        self.env.write_log(_log_line(PHANTOM_TEXT, age_min=45))
        self.assertEqual(h.run_once(NOW, self.cfg, alerted=set()), [])


class ConfigTest(unittest.TestCase):
    def test_shipped_config_loads_and_has_phrases(self):
        cfg = h.load_config()
        self.assertIn('dispatches to forge',
                      [p.lower() for p in cfg['dispatch_claim_phrases']])
        self.assertTrue(cfg['strong_dispatch_phrases'])

    def test_bad_json_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'bad.json'
            p.write_text('{ not json')
            with self.assertRaises(ValueError):
                h.load_config(p)

    def test_missing_phrases_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'noPhrases.json'
            p.write_text('{"_schema": {}}')
            with self.assertRaises(ValueError):
                h.load_config(p)


class _FakeAlerts:
    def __init__(self):
        self.calls = []

    def append_alert(self, **kw):
        self.calls.append(kw)
        return True


class MainEmitTest(unittest.TestCase):
    def setUp(self):
        self.env = _Env(self)
        self._fake = _FakeAlerts()
        mod_patch = mock.patch.dict(sys.modules, {'larry_alerts': self._fake})
        mod_patch.start()
        self.addCleanup(mod_patch.stop)

    def _write_phantom(self, age_min: float):
        import time
        h.beacon_log_file().write_text(
            _log_line(PHANTOM_TEXT, age_min=age_min, now=time.time()) + '\n')

    def test_main_emits_one_phantom_alert(self):
        self._write_phantom(age_min=15)
        rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._fake.calls), 1)
        call = self._fake.calls[0]
        self.assertEqual(call['source'], 'heal-phantom-dispatch-claim')
        self.assertEqual(call['severity'], 'warning')
        self.assertEqual(call['route'], 'escalate')
        self.assertEqual(call['subject'], f'phantom-dispatch:{PHANTOM_TASK}')

    def test_main_idempotent_across_runs(self):
        self._write_phantom(age_min=15)
        self.assertEqual(h.main(), 0)
        self.assertEqual(h.main(), 0)
        self.assertEqual(len(self._fake.calls), 1,
                         'phantom must alert exactly once across ticks')

    def test_main_quiet_on_routine(self):
        import time
        h.beacon_log_file().write_text(
            _log_line('Digest out — nothing for you.', 15, now=time.time()) + '\n')
        self.assertEqual(h.main(), 0)
        self.assertEqual(self._fake.calls, [])

    def test_kill_switch_short_circuits(self):
        self._write_phantom(age_min=15)
        h.kill_switch().write_text('')
        self.assertEqual(h.main(), 0)
        self.assertEqual(self._fake.calls, [])


if __name__ == '__main__':
    unittest.main()
