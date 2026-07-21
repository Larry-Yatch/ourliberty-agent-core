#!/usr/bin/env python3
"""test_alert_tier_stamp.py — the operator tier is stamped at WRITE time.

The NOW/SOON/FYI taxonomy shipped in #177, but emitted rows carried no tier,
so the (source, subject) → tier join had to be reconstructed after the fact —
recoverable for only ~15% of live rows (a source whose translation block holds
several subjects with different tiers is ambiguous once the row is on disk),
and the queue is trimmed at 14 days, so every unstamped day is tier history
permanently gone. These tests pin the stamp and, critically, that the stamp and
the DM glyph come from ONE resolution (`resolve_tier`) so they cannot disagree.

Run:
    python3 -m unittest scripts.tests.test_alert_tier_stamp
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import larry_alerts  # noqa: E402


# A fixture table, not the live config: these tests pin the STAMPING mechanism,
# not any particular source's classification (which Pulse re-tunes freely).
_FIXTURE = {
    'watchdog': {
        'disk-full': {
            'tier': 'NOW',
            'severity': 'URGENT',
            'plain_language_summary': 'Disk is full.',
            'recommended_action': 'Free space.',
        },
        'cache-warm': {
            'tier': 'SOON',
            'severity': 'WARNING',
            'plain_language_summary': 'Cache is cold.',
            'recommended_action': 'Rewarm it.',
        },
        'no-tier-key': {
            'severity': 'WARNING',
            'plain_language_summary': 'Entry predates the taxonomy.',
        },
        'bogus-tier': {
            'tier': 'LATER',  # typo'd value that is not NOW/SOON/FYI
            'severity': 'WARNING',
            'plain_language_summary': 'Typo in the config.',
        },
    },
}


class _TierStampTest(unittest.TestCase):
    """Isolated queue + a fixture translations table."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        translations = tmp_path / 'alert-translations.json'
        translations.write_text(json.dumps(_FIXTURE))
        self._patches = [
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', tmp_path),
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              tmp_path / 'blackboard' / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              tmp_path / 'state' / 'alert-cooldown'),
            mock.patch.object(larry_alerts, 'SILENCE_ROOT',
                              tmp_path / 'state' / 'alert-silenced'),
            mock.patch.object(larry_alerts, 'SILENCE_COUNTER_ROOT',
                              tmp_path / 'state' / 'alert-silenced-counts'),
            mock.patch.object(larry_alerts, 'OFFSET_FILE',
                              tmp_path / 'state' / 'beacon-alerts-offset.txt'),
            mock.patch.object(larry_alerts, 'TRANSLATIONS_FILE', translations),
            mock.patch.object(larry_alerts, '_TRANSLATIONS_CACHE', None),
            mock.patch.object(larry_alerts, '_TRANSLATIONS_MTIME', None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _rows(self) -> list:
        text = larry_alerts.ALERTS_FILE.read_text().strip()
        return [json.loads(line) for line in text.splitlines() if line]

    def _emit(self, subject, source='watchdog', severity='warning'):
        self.assertTrue(larry_alerts.append_alert(
            source=source, severity=severity, message='m', subject=subject,
        ))
        return self._rows()[-1]


class ResolveTierTest(_TierStampTest):
    def test_matched_entry_reports_translation_provenance(self):
        self.assertEqual(
            larry_alerts.resolve_tier({'tier': 'NOW'}), ('NOW', 'translation'))

    def test_no_translation_falls_back_to_fyi_default(self):
        self.assertEqual(larry_alerts.resolve_tier(None), ('FYI', 'default'))

    def test_entry_without_tier_is_default_not_translation(self):
        # Provenance is the whole point: an entry predating the taxonomy is
        # FYI-by-fallback, not a deliberate FYI classification.
        self.assertEqual(
            larry_alerts.resolve_tier({'severity': 'WARNING'}),
            ('FYI', 'default'))

    def test_unknown_tier_value_degrades_to_default(self):
        self.assertEqual(
            larry_alerts.resolve_tier({'tier': 'LATER'}), ('FYI', 'default'))
        self.assertEqual(
            larry_alerts.resolve_tier({'tier': 123}), ('FYI', 'default'))

    def test_resolve_tier_for_never_raises_on_broken_config(self):
        with mock.patch.object(larry_alerts, 'translate_alert',
                               side_effect=RuntimeError('boom')):
            self.assertEqual(
                larry_alerts.resolve_tier_for('watchdog', 'disk-full'),
                ('FYI', 'default'))


class AppendAlertTierStampTest(_TierStampTest):
    def test_known_now_source_is_stamped_now(self):
        row = self._emit('disk-full')
        self.assertEqual(row['tier'], 'NOW')
        self.assertEqual(row['tier_source'], 'translation')

    def test_known_soon_source_is_stamped_soon(self):
        row = self._emit('cache-warm')
        self.assertEqual(row['tier'], 'SOON')
        self.assertEqual(row['tier_source'], 'translation')

    def test_unclassified_source_is_stamped_default_fyi(self):
        row = self._emit('never-heard-of-it')
        self.assertEqual(row['tier'], 'FYI')
        self.assertEqual(row['tier_source'], 'default')

    def test_entry_missing_tier_stamps_default_provenance(self):
        row = self._emit('no-tier-key')
        self.assertEqual((row['tier'], row['tier_source']), ('FYI', 'default'))

    def test_subjectless_alert_still_carries_a_tier(self):
        # translate_alert can't match a subjectless alert at all; the row must
        # still be stamped rather than dropping the field (a missing field and
        # a fallback FYI are indistinguishable downstream otherwise).
        self.assertTrue(larry_alerts.append_alert(
            source='watchdog', severity='warning', message='m'))
        row = self._rows()[-1]
        self.assertEqual((row['tier'], row['tier_source']), ('FYI', 'default'))

    def test_longest_prefix_subject_inherits_the_entry_tier(self):
        row = self._emit('disk-full:sda1:97pct')
        self.assertEqual((row['tier'], row['tier_source']), ('NOW', 'translation'))

    def test_stamp_is_additive_existing_fields_untouched(self):
        row = self._emit('disk-full', severity='critical')
        for field in ('ts', 'source', 'severity', 'message', 'route', 'subject'):
            self.assertIn(field, row)
        self.assertEqual(row['source'], 'watchdog')
        self.assertEqual(row['severity'], 'critical')
        self.assertEqual(row['route'], 'escalate')

    def test_promotion_line_is_stamped_too(self):
        self.assertTrue(larry_alerts.append_promotion(
            source='watchdog', severity='warning', message='m',
            subject='disk-full',
        ))
        row = self._rows()[-1]
        self.assertTrue(row['subject'].endswith(
            larry_alerts.PROMOTION_SUBJECT_SUFFIX))
        # The ::promoted suffix must not lose the tier — translate_alert's
        # prefix-strip recovers the original entry.
        self.assertEqual((row['tier'], row['tier_source']),
                         ('NOW', 'translation'))


class StampMatchesGlyphTest(_TierStampTest):
    """The stamp and the DM glyph must never disagree — one resolution."""

    def _dm_tier(self, row):
        dm = larry_alerts.format_dm(row)
        header = dm.splitlines()[0]
        for tier, glyph in larry_alerts._TIER_GLYPHS.items():
            if header.startswith(glyph):
                return tier
        return None

    def test_glyph_agrees_with_stamp_across_subjects(self):
        for subject in ('disk-full', 'cache-warm', 'no-tier-key', 'bogus-tier'):
            with self.subTest(subject=subject):
                row = self._emit(subject)
                self.assertEqual(self._dm_tier(row), row['tier'])


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # Same opt-out test_larry_alerts.py uses: this module drives the Layer
    # B-guarded larry-alerts chokepoint against an already-isolated tempdir
    # queue, so the guard would breach before the test's own mocks apply. The
    # #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


if __name__ == '__main__':
    unittest.main()
