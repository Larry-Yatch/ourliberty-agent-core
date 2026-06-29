#!/usr/bin/env python3
"""test_alert_routing.py — fix-first / notify-on-outcome routing (2026-06-03).

Larry's policy: don't ping me that something broke, fix it, then go quiet —
fix it first and either tell me it's fixed (only when it mattered) or tell me
you couldn't and I have an action. Every alert resolves to exactly one route:

  - escalate — DM Larry now (failed heal w/ action, or un-healable detection).
               DEFAULT / fail-loud.
  - closure  — DM one line: was broken, fixed it, no action needed. ONLY for
               SIGNIFICANT successful heals.
  - digest   — NOT DM'd; surfaced in the daily CEO digest. Routine heals.

This file pins:
  - is_significant: subject-prefix membership (trailing '*'/':' cosmetic).
  - classify_route: escalate-when-unhealed; closure-if-significant-else-digest.
  - _render_outcome_alert / format_dm: closure+digest render the self-healed
    confirmation with NO imperative, dropping any suggested_action.
  - read_digest_window: only route=digest records inside [start, end).
  - the bot's _check_pending_alerts loop: digest is the only no-DM advance
    besides malformed; closure + escalate DM; a failed non-digest DM does NOT
    advance the offset (M2 per-line-ack invariant).

Run:
    python3 -m unittest scripts.tests.test_alert_routing
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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import larry_alerts  # noqa: E402


class _SignificanceFixture(unittest.TestCase):
    """Points larry_alerts.SIGNIFICANCE_FILE at a tempfile and resets the
    mtime cache around each test."""

    SIGNIFICANT = [
        'heal-credential-registry-drift:*',
        'heal-pipeline-stall:pipeline-stall:',
        'beacon-telegram-bot:claude_tier1_failed',
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._sig = Path(self._tmp.name) / 'alert-significance.json'
        self._sig.write_text(json.dumps({
            'significant_subjects': self.SIGNIFICANT,
        }))
        self._patch = mock.patch.object(
            larry_alerts, 'SIGNIFICANCE_FILE', self._sig)
        self._patch.start()
        larry_alerts._SIGNIFICANCE_CACHE = None  # noqa: SLF001
        larry_alerts._SIGNIFICANCE_MTIME = None  # noqa: SLF001

    def tearDown(self):
        self._patch.stop()
        larry_alerts._SIGNIFICANCE_CACHE = None  # noqa: SLF001
        larry_alerts._SIGNIFICANCE_MTIME = None  # noqa: SLF001
        self._tmp.cleanup()


class IsSignificantTest(_SignificanceFixture):
    def test_wildcard_entry_matches_any_subject_under_source(self):
        self.assertTrue(larry_alerts.is_significant(
            'heal-credential-registry-drift',
            'credential-drift:MISSING_REGISTRY_ENTRY'))

    def test_colon_prefix_matches_dynamic_suffix(self):
        self.assertTrue(larry_alerts.is_significant(
            'heal-pipeline-stall',
            'pipeline-stall:forge-no-pr:wt-forge-foo'))

    def test_bare_prefix_matches(self):
        self.assertTrue(larry_alerts.is_significant(
            'beacon-telegram-bot', 'claude_tier1_failed_rate_limit'))

    def test_unlisted_source_is_routine(self):
        self.assertFalse(larry_alerts.is_significant(
            'heal-systemd-install-drift', 'install-healed:x.service'))

    def test_missing_file_treats_all_as_routine(self):
        with mock.patch.object(larry_alerts, 'SIGNIFICANCE_FILE',
                               Path('/nonexistent/sig.json')):
            larry_alerts._SIGNIFICANCE_CACHE = None  # noqa: SLF001
            larry_alerts._SIGNIFICANCE_MTIME = None  # noqa: SLF001
            self.assertFalse(larry_alerts.is_significant(
                'heal-credential-registry-drift', 'anything'))

    def test_malformed_file_treats_all_as_routine(self):
        self._sig.write_text('{ not valid json')
        os.utime(self._sig, (9000, 9000))
        larry_alerts._SIGNIFICANCE_CACHE = None  # noqa: SLF001
        larry_alerts._SIGNIFICANCE_MTIME = None  # noqa: SLF001
        self.assertFalse(larry_alerts.is_significant(
            'heal-credential-registry-drift', 'anything'))


class ClassifyRouteTest(_SignificanceFixture):
    def test_unhealed_always_escalates_even_if_significant(self):
        # A failed heal of a significant subject must DM Larry the action —
        # NOT route to a closure/digest "it's fine" message.
        self.assertEqual(
            larry_alerts.classify_route(
                'heal-credential-registry-drift',
                'credential-drift:MISSING_REGISTRY_ENTRY', healed=False),
            'escalate')

    def test_unhealed_routine_escalates(self):
        self.assertEqual(
            larry_alerts.classify_route(
                'heal-systemd-install-drift', 'install-drift:x.service',
                healed=False),
            'escalate')

    def test_healed_significant_is_closure(self):
        self.assertEqual(
            larry_alerts.classify_route(
                'heal-credential-registry-drift',
                'credential-drift:MISSING_REGISTRY_ENTRY', healed=True),
            'closure')

    def test_healed_routine_is_digest(self):
        self.assertEqual(
            larry_alerts.classify_route(
                'heal-systemd-install-drift', 'install-healed:x.service',
                healed=True),
            'digest')


class AppendAlertRouteTest(unittest.TestCase):
    """append_alert persists `route`; unknown route fails loud to escalate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              root / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              root / 'alert-cooldown'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _last(self) -> dict:
        return json.loads(
            larry_alerts.ALERTS_FILE.read_text().strip().splitlines()[-1])

    def test_route_round_trips(self):
        # B1: 'hold' is a valid route alongside the existing three.
        for route in ('escalate', 'closure', 'digest', 'hold'):
            larry_alerts.append_alert(
                source='heal-x', severity='warning', message='m',
                subject=f'subj-{route}', route=route)
            self.assertEqual(self._last()['route'], route)

    def test_default_route_is_escalate(self):
        larry_alerts.append_alert(
            source='heal-x', severity='warning', message='m', subject='d')
        self.assertEqual(self._last()['route'], 'escalate')

    def test_unknown_route_degrades_to_escalate(self):
        larry_alerts.append_alert(
            source='heal-x', severity='warning', message='m', subject='u',
            route='banana')
        self.assertEqual(self._last()['route'], 'escalate')

    def test_critical_forces_escalate_even_when_held(self):
        # B2: a critical can never be held/digested — emit-time guarantee.
        for route in ('hold', 'digest', 'closure'):
            larry_alerts.append_alert(
                source='heal-x', severity='critical', message='m',
                subject=f'crit-{route}', route=route)
            self.assertEqual(self._last()['route'], 'escalate')

    def test_critical_default_is_escalate(self):
        larry_alerts.append_alert(
            source='heal-x', severity='critical', message='m', subject='cd')
        self.assertEqual(self._last()['route'], 'escalate')

    def test_warning_hold_is_preserved(self):
        # The B2 force is critical-only; a warning may legitimately be held.
        larry_alerts.append_alert(
            source='heal-x', severity='warning', message='m', subject='wh',
            route='hold')
        self.assertEqual(self._last()['route'], 'hold')


class GraduationRegistryTest(unittest.TestCase):
    """B4: the graduation registry is the incremental-migration control surface.

    A migrated source's routine (non-critical) alerts default to its migrated
    route (`hold`) instead of escalate; an un-migrated source is untouched; a
    migrated source's critical still forces escalate; an explicit caller route
    still wins; and a malformed/missing registry fails loud (un-migrated)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._reg = root / 'alert-graduation-registry.json'
        self._reg.write_text(json.dumps({
            'migrated_sources': {'outbox-notifier': 'hold'},
        }))
        self._patches = [
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              root / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              root / 'alert-cooldown'),
            mock.patch.object(larry_alerts, 'GRADUATION_FILE', self._reg),
        ]
        for p in self._patches:
            p.start()
        larry_alerts._GRADUATION_CACHE = None  # noqa: SLF001
        larry_alerts._GRADUATION_MTIME = None  # noqa: SLF001

    def tearDown(self):
        for p in self._patches:
            p.stop()
        larry_alerts._GRADUATION_CACHE = None  # noqa: SLF001
        larry_alerts._GRADUATION_MTIME = None  # noqa: SLF001
        self._tmp.cleanup()

    def _last(self) -> dict:
        return json.loads(
            larry_alerts.ALERTS_FILE.read_text().strip().splitlines()[-1])

    def test_graduated_route_lookup(self):
        self.assertEqual(
            larry_alerts.graduated_route('outbox-notifier'), 'hold')
        self.assertIsNone(larry_alerts.graduated_route('watchdog'))

    def test_migrated_source_warning_defaults_to_hold(self):
        # Acceptance: a migrated-source warning does NOT DM next sweep (hold),
        # instead of the escalate default an un-migrated source would get.
        larry_alerts.append_alert(
            source='outbox-notifier', severity='warning', message='m',
            subject='mirror-dag-malformed-result:seq')
        self.assertEqual(self._last()['route'], 'hold')

    def test_migrated_source_info_defaults_to_hold(self):
        # A migrated source holds its routine info too (would otherwise digest).
        larry_alerts.append_alert(
            source='outbox-notifier', severity='info', message='m',
            subject='routine')
        self.assertEqual(self._last()['route'], 'hold')

    def test_migrated_source_critical_still_escalates(self):
        # Acceptance: critical DMs next sweep regardless of route — the B2
        # force fires after the graduation default is applied.
        larry_alerts.append_alert(
            source='outbox-notifier', severity='critical', message='m',
            subject='crit')
        self.assertEqual(self._last()['route'], 'escalate')

    def test_unmigrated_source_untouched(self):
        # An un-migrated source keeps the severity-based default (escalate).
        larry_alerts.append_alert(
            source='watchdog', severity='warning', message='m', subject='w')
        self.assertEqual(self._last()['route'], 'escalate')

    def test_explicit_route_wins_over_graduation(self):
        # Graduation only sets the DEFAULT; an explicit caller route is honored.
        larry_alerts.append_alert(
            source='outbox-notifier', severity='warning', message='m',
            subject='explicit', route='escalate')
        self.assertEqual(self._last()['route'], 'escalate')

    def test_missing_registry_treats_source_as_unmigrated(self):
        with mock.patch.object(larry_alerts, 'GRADUATION_FILE',
                               Path('/nonexistent/grad.json')):
            larry_alerts._GRADUATION_CACHE = None  # noqa: SLF001
            larry_alerts._GRADUATION_MTIME = None  # noqa: SLF001
            larry_alerts.append_alert(
                source='outbox-notifier', severity='warning', message='m',
                subject='nofile')
            self.assertEqual(self._last()['route'], 'escalate')

    def test_malformed_registry_treats_source_as_unmigrated(self):
        self._reg.write_text('{ not valid json')
        os.utime(self._reg, (9000, 9000))
        larry_alerts._GRADUATION_CACHE = None  # noqa: SLF001
        larry_alerts._GRADUATION_MTIME = None  # noqa: SLF001
        larry_alerts.append_alert(
            source='outbox-notifier', severity='warning', message='m',
            subject='bad')
        self.assertEqual(self._last()['route'], 'escalate')

    def test_unrecognized_route_value_ignored(self):
        self._reg.write_text(json.dumps({
            'migrated_sources': {'outbox-notifier': 'banana'},
        }))
        os.utime(self._reg, (9001, 9001))
        larry_alerts._GRADUATION_CACHE = None  # noqa: SLF001
        larry_alerts._GRADUATION_MTIME = None  # noqa: SLF001
        self.assertIsNone(larry_alerts.graduated_route('outbox-notifier'))
        larry_alerts.append_alert(
            source='outbox-notifier', severity='warning', message='m',
            subject='typo')
        self.assertEqual(self._last()['route'], 'escalate')


class AppendPromotionTest(unittest.TestCase):
    """B3: append_promotion writes a fresh escalate line with a distinct
    ::promoted subject marker + promoted_from fingerprint, bypassing cooldown."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              root / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              root / 'alert-cooldown'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _last(self) -> dict:
        return json.loads(
            larry_alerts.ALERTS_FILE.read_text().strip().splitlines()[-1])

    def test_promotion_is_escalate_with_marker(self):
        ok = larry_alerts.append_promotion(
            source='heal-x', severity='warning', message='m', subject='stuck')
        self.assertTrue(ok)
        rec = self._last()
        self.assertEqual(rec['route'], 'escalate')
        self.assertEqual(rec['subject'], 'stuck' + larry_alerts.PROMOTION_SUBJECT_SUFFIX)
        self.assertEqual(rec['promoted_from'], 'heal-x:stuck')
        self.assertTrue(rec['promotion'])

    def test_promotion_bypasses_cooldown(self):
        # Mark the original fingerprint's cooldown, then promote: the distinct
        # ::promoted bucket means the promotion is never swallowed.
        larry_alerts._mark_cooldown('warning', 'heal-x:stuck')
        ok = larry_alerts.append_promotion(
            source='heal-x', severity='warning', message='m', subject='stuck')
        self.assertTrue(ok)
        self.assertEqual(self._last()['promoted_from'], 'heal-x:stuck')


class OutcomeRenderTest(unittest.TestCase):
    """closure + digest records render the self-healed confirmation: outcome
    language only, NO imperative, suggested_action dropped."""

    def test_closure_prefers_producer_message(self):
        text = larry_alerts.format_dm({
            'source': 'sync-agent-core',
            'subject': 'auto-restarted:foo.service',
            'severity': 'warning',
            'route': 'closure',
            'message': 'Restarted a background service that had stale code.',
            'suggested_action': 'sudo systemctl restart foo',
        })
        self.assertIn('Self-healed', text)
        self.assertIn('Restarted a background service', text)
        self.assertIn('No action needed', text)
        self.assertNotIn('Run:', text)
        self.assertNotIn('sudo', text.lower())

    def test_digest_render_is_also_outcome_shaped(self):
        # digest is normally skipped by the bot, but if ever rendered it must
        # carry the same no-imperative outcome shape (never a problem-framed DM).
        text = larry_alerts.format_dm({
            'source': 'sync-agent-core',
            'subject': 'sync-blocked:auto-commit',
            'severity': 'warning',
            'route': 'digest',
            'message': 'Cleaned up the shared workspace; it self-heals next tick.',
        })
        self.assertIn('Self-healed', text)
        self.assertIn('No action needed', text)
        self.assertNotIn('Run:', text)

    def test_escalate_keeps_problem_framed_render(self):
        # Regression: escalate must NOT get the self-healed shape — it carries
        # the action Larry takes.
        text = larry_alerts.format_dm({
            'source': 'watchdog',
            'subject': 'bots:mirror',
            'severity': 'critical',
            'route': 'escalate',
            'message': 'Mirror is DOWN.',
            'suggested_action': 'sudo systemctl restart ourliberty-mirror-bot',
        })
        self.assertNotIn('Self-healed', text)
        self.assertIn('Mirror is DOWN', text)


class ReadDigestWindowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._alerts = Path(self._tmp.name) / 'larry-alerts.jsonl'
        self._patch = mock.patch.object(
            larry_alerts, 'ALERTS_FILE', self._alerts)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, records):
        self._alerts.write_text(
            '\n'.join(json.dumps(r) for r in records) + '\n')

    def test_only_digest_in_window(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        end = datetime(2026, 6, 2, tzinfo=timezone.utc)
        self._write([
            {'ts': '2026-06-01T08:00:00+00:00', 'route': 'digest', 'message': 'a'},
            {'ts': '2026-06-01T09:00:00+00:00', 'route': 'closure', 'message': 'b'},
            {'ts': '2026-06-01T10:00:00+00:00', 'route': 'escalate', 'message': 'c'},
            {'ts': '2026-06-01T11:00:00+00:00', 'message': 'no-route'},
            {'ts': '2026-05-31T23:00:00+00:00', 'route': 'digest', 'message': 'before'},
            {'ts': '2026-06-02T00:00:00+00:00', 'route': 'digest', 'message': 'at-end-exclusive'},
        ])
        out = larry_alerts.read_digest_window(start, end)
        msgs = [r['message'] for r in out]
        self.assertEqual(msgs, ['a'])

    def test_zulu_suffix_ts_parsed(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        end = datetime(2026, 6, 2, tzinfo=timezone.utc)
        self._write([
            {'ts': '2026-06-01T08:00:00Z', 'route': 'digest', 'message': 'z'},
        ])
        self.assertEqual(
            [r['message'] for r in larry_alerts.read_digest_window(start, end)],
            ['z'])

    def test_unparseable_ts_skipped(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        end = datetime(2026, 6, 2, tzinfo=timezone.utc)
        self._write([
            {'ts': 'garbage', 'route': 'digest', 'message': 'bad'},
            {'route': 'digest', 'message': 'no-ts'},
        ])
        self.assertEqual(larry_alerts.read_digest_window(start, end), [])

    def test_missing_file_empty(self):
        with mock.patch.object(larry_alerts, 'ALERTS_FILE',
                               Path('/nonexistent/larry-alerts.jsonl')):
            self.assertEqual(
                larry_alerts.read_digest_window(
                    datetime(2026, 6, 1, tzinfo=timezone.utc),
                    datetime(2026, 6, 2, tzinfo=timezone.utc)),
                [])


class _BotImporter:
    """beacon_telegram_bot sys.exit()s at import without these env vars."""

    @staticmethod
    def load_with_env():
        os.environ.setdefault('TELEGRAM_BOT_TOKEN_BEACON', 'TEST_TOKEN')
        os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_IDS', '12345')
        if 'beacon_telegram_bot' in sys.modules:
            del sys.modules['beacon_telegram_bot']
        return importlib.import_module('beacon_telegram_bot')


class BotLoopRoutingTest(unittest.TestCase):
    """The bot's _check_pending_alerts must: skip+advance digest (no DM), DM
    closure + escalate, and never advance the offset on a failed non-digest
    DM (M2 per-line-ack)."""

    def setUp(self):
        self.bot = _BotImporter.load_with_env()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._alerts = root / 'larry-alerts.jsonl'
        self._offset = root / 'beacon-alerts-offset.txt'
        self._patches = [
            mock.patch.object(larry_alerts, 'ALERTS_FILE', self._alerts),
            mock.patch.object(larry_alerts, 'OFFSET_FILE', self._offset),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              root / 'alert-cooldown'),
        ]
        for p in self._patches:
            p.start()
        self.sent: list[tuple[int, str]] = []

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _append(self, route, subject, message='m', severity='warning'):
        ok = larry_alerts.append_alert(
            source='heal-x', severity=severity, message=message,
            subject=subject, route=route)
        self.assertTrue(ok, f'append for {subject} was suppressed')

    def _append_raw(self, record: dict):
        # Bypass append_alert (e.g. to forge a critical+hold line append_alert
        # would never let exist) and write a literal queue line.
        with open(self._alerts, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')

    def _capture_send(self, ok=True):
        def _fake(chat_id, text):
            self.sent.append((chat_id, text))
            return ok
        return _fake

    def test_hold_skipped_no_dm_but_offset_advances(self):
        # B1: a held warning is skip-advanced like digest — no DM, offset moves.
        self._append('hold', 'pending-judgment')
        with mock.patch.object(self.bot, '_send_alert_dm',
                               side_effect=self._capture_send(True)):
            self.bot._check_pending_alerts()
        self.assertEqual(self.sent, [])
        self.assertEqual(larry_alerts.read_offset(), 1)

    def test_critical_hold_still_dms(self):
        # B1 read-time guard: even a (mis-routed) critical hold DMs — the bot
        # re-checks severity before skipping.
        self._append_raw({
            'ts': '2026-06-28T00:00:00+00:00', 'source': 'heal-x',
            'severity': 'critical', 'message': 'critical held by mistake',
            'route': 'hold', 'subject': 'oops',
        })
        with mock.patch.object(self.bot, '_send_alert_dm',
                               side_effect=self._capture_send(True)):
            self.bot._check_pending_alerts()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('critical held by mistake', self.sent[0][1])
        self.assertEqual(larry_alerts.read_offset(), 1)

    def test_digest_skipped_no_dm_but_offset_advances(self):
        self._append('digest', 'auto-restarted:foo')
        with mock.patch.object(self.bot, '_send_alert_dm',
                               side_effect=self._capture_send(True)):
            self.bot._check_pending_alerts()
        self.assertEqual(self.sent, [])
        self.assertEqual(larry_alerts.read_offset(), 1)

    def test_closure_and_escalate_are_dmed(self):
        self._append('closure', 'fixed-something',
                     message='Was broken, fixed it.')
        self._append('escalate', 'still-broken',
                     message='Could not fix; needs you.')
        with mock.patch.object(self.bot, '_send_alert_dm',
                               side_effect=self._capture_send(True)):
            self.bot._check_pending_alerts()
        # Both DM'd to the single authorized chat (12345); offset consumes both.
        self.assertEqual(len(self.sent), 2)
        bodies = '\n'.join(t for _, t in self.sent)
        self.assertIn('Was broken, fixed it.', bodies)
        self.assertIn('Could not fix', bodies)
        self.assertEqual(larry_alerts.read_offset(), 2)

    def test_digest_then_closure_mixed_stream(self):
        self._append('digest', 'routine-1', message='routine heal 1')
        self._append('closure', 'sig-1', message='significant heal 1')
        self._append('digest', 'routine-2', message='routine heal 2')
        with mock.patch.object(self.bot, '_send_alert_dm',
                               side_effect=self._capture_send(True)):
            self.bot._check_pending_alerts()
        # Only the closure was DM'd; both digests skipped; all 3 consumed.
        self.assertEqual([t for _, t in self.sent].__len__(), 1)
        self.assertIn('significant heal 1', self.sent[0][1])
        self.assertEqual(larry_alerts.read_offset(), 3)

    def test_failed_non_digest_dm_does_not_advance_offset(self):
        self._append('escalate', 'needs-you', message='needs you')
        with mock.patch.object(self.bot, '_send_alert_dm',
                               side_effect=self._capture_send(False)):
            self.bot._check_pending_alerts()
        # Delivery failed -> offset stays at 0 so the next sweep retries (M2).
        self.assertEqual(larry_alerts.read_offset(), 0)

    def test_digest_advances_even_when_send_would_fail(self):
        # digest never calls _send_alert_dm, so a (would-be) failing sender
        # must not block the skip+advance.
        self._append('digest', 'routine-x', message='routine')
        with mock.patch.object(self.bot, '_send_alert_dm',
                               side_effect=self._capture_send(False)):
            self.bot._check_pending_alerts()
        self.assertEqual(self.sent, [])
        self.assertEqual(larry_alerts.read_offset(), 1)


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # This module drives a Layer B-guarded chokepoint (larry_alerts / inbox /
    # gh-write / claude-spawn / concurrency) against already-isolated state, so
    # the guard would breach before the test's own mocks. Opt out for the module
    # so the guard is a pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
