#!/usr/bin/env python3
"""Tests for parked_aging_digest_generator.py — Missions v2 parked-&-aging digest.

Covers:
  - select_aging_parked: selects EXACTLY the state==parked AND aging==true set,
    with NO independent date/business-day math (spec § 6 enforcement).
  - count_parked: counts all parked captures (aging or not).
  - build_item / build_digest: artifact shape — counts + aging items carrying
    title + origin repo + age.
  - age_days: display-only calendar age, never gates selection.
  - read_captures_registry: missing → empty registry; malformed → None.
  - run: end-to-end happy path writes the artifact; unreadable captures.json
    leaves the prior artifact untouched.

Run:
    python3 -m unittest scripts.tests.test_parked_aging_digest_generator
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
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import parked_aging_digest_generator as pad  # noqa: E402

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def _cap(cid, *, state='parked', aging=None, title='t', repo='ourliberty-agent-core',
         last_touched='2026-06-01T00:00:00+00:00'):
    cap = {
        'id': cid,
        'title': title,
        'state': state,
        'last_touched': last_touched,
        'origin': {'repo': repo},
    }
    if aging is not None:
        cap['aging'] = aging
    return cap


def _registry(*caps):
    return {'schema_version': 1, 'captures': list(caps)}


class SelectAgingParkedTest(unittest.TestCase):
    def test_selects_exactly_parked_and_aging_true(self):
        reg = _registry(
            _cap('a', state='parked', aging=True),    # in
            _cap('b', state='parked', aging=False),   # out (not aging)
            _cap('c', state='parked'),                # out (no aging flag)
            _cap('d', state='promoted', aging=True),  # out (not parked)
            _cap('e', state='parked', aging=True),    # in
        )
        ids = [c['id'] for c in pad.select_aging_parked(reg, NOW)]
        self.assertEqual(ids, ['a', 'e'])

    def test_aging_must_be_boolean_true_not_truthy(self):
        # Defensive: only the literal `True` flag counts, mirroring the GC
        # healer's `cap.get('aging') is True` write contract.
        reg = _registry(
            _cap('truthy', state='parked', aging='yes'),
            _cap('one', state='parked', aging=1),
        )
        self.assertEqual(pad.select_aging_parked(reg, NOW), [])

    def test_skips_non_dict_entries(self):
        reg = {'captures': ['junk', None, _cap('ok', aging=True)]}
        self.assertEqual(
            [c['id'] for c in pad.select_aging_parked(reg, NOW)], ['ok'])

    def test_empty_registry(self):
        self.assertEqual(pad.select_aging_parked(_registry(), NOW), [])

    def test_snoozed_aging_capture_suppressed(self):
        # A parked+aging capture snoozed into the future is hidden from the
        # digest until the snooze elapses (Phase 3 § 4.3).
        snoozed = _cap('snz', state='parked', aging=True)
        snoozed['snoozed_until'] = '2026-06-20T00:00:00+00:00'  # after NOW
        reg = _registry(_cap('a', state='parked', aging=True), snoozed)
        self.assertEqual(
            [c['id'] for c in pad.select_aging_parked(reg, NOW)], ['a'])

    def test_past_snooze_does_not_suppress(self):
        # A snooze that already elapsed no longer hides the capture.
        woke = _cap('woke', state='parked', aging=True)
        woke['snoozed_until'] = '2026-06-01T00:00:00+00:00'  # before NOW
        reg = _registry(woke)
        self.assertEqual(
            [c['id'] for c in pad.select_aging_parked(reg, NOW)], ['woke'])


class CountParkedTest(unittest.TestCase):
    def test_counts_all_parked_regardless_of_aging(self):
        reg = _registry(
            _cap('a', state='parked', aging=True),
            _cap('b', state='parked', aging=False),
            _cap('c', state='parked'),
            _cap('d', state='promoted'),
        )
        self.assertEqual(pad.count_parked(reg, NOW), 3)

    def test_excludes_snoozed_from_count(self):
        snoozed = _cap('snz', state='parked')
        snoozed['snoozed_until'] = '2026-06-20T00:00:00+00:00'  # after NOW
        reg = _registry(
            _cap('a', state='parked'),
            _cap('b', state='parked'),
            snoozed,
        )
        self.assertEqual(pad.count_parked(reg, NOW), 2)


class AgeDaysTest(unittest.TestCase):
    def test_calendar_days_since_last_touched(self):
        cap = _cap('a', last_touched='2026-06-03T00:00:00+00:00')
        self.assertEqual(pad._age_days(cap, NOW), 7)

    def test_missing_last_touched_is_none(self):
        cap = {'id': 'a'}
        self.assertIsNone(pad._age_days(cap, NOW))

    def test_unparseable_last_touched_is_none(self):
        cap = _cap('a', last_touched='garbage')
        self.assertIsNone(pad._age_days(cap, NOW))

    def test_future_last_touched_clamps_to_zero(self):
        cap = _cap('a', last_touched='2026-07-01T00:00:00+00:00')
        self.assertEqual(pad._age_days(cap, NOW), 0)


class BuildDigestTest(unittest.TestCase):
    def test_artifact_shape_counts_and_items(self):
        reg = _registry(
            _cap('a', state='parked', aging=True, title='Idea A',
                 repo='ourliberty-agent-core', last_touched='2026-06-03T00:00:00+00:00'),
            _cap('b', state='parked', aging=False),
            _cap('c', state='promoted', aging=True),
        )
        digest = pad.build_digest(reg, NOW, trigger='scheduled')
        self.assertEqual(digest['schema_version'], pad.SCHEMA_VERSION)
        self.assertEqual(digest['generated_at'], NOW.isoformat())
        self.assertEqual(digest['trigger'], 'scheduled')
        self.assertEqual(digest['parked_count'], 2)
        self.assertEqual(digest['aging_count'], 1)
        self.assertEqual(len(digest['items']), 1)
        item = digest['items'][0]
        self.assertEqual(item['capture_id'], 'a')
        self.assertEqual(item['title'], 'Idea A')
        self.assertEqual(item['repo'], 'ourliberty-agent-core')
        self.assertEqual(item['age_days'], 7)
        self.assertEqual(item['last_touched'], '2026-06-03T00:00:00+00:00')

    def test_origin_repo_missing_is_none(self):
        cap = {'id': 'a', 'title': 't', 'state': 'parked', 'aging': True}
        item = pad.build_item(cap, NOW)
        self.assertIsNone(item['repo'])


class ReadRegistryTest(unittest.TestCase):
    def test_missing_file_yields_empty_registry(self):
        reg = pad.read_captures_registry(Path('/nonexistent/captures.json'))
        self.assertEqual(reg, {'schema_version': 1, 'captures': []})

    def test_malformed_yields_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'captures.json'
            p.write_text('{ not json')
            self.assertIsNone(pad.read_captures_registry(p))

    def test_wrong_shape_yields_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'captures.json'
            p.write_text(json.dumps({'captures': 'not a list'}))
            self.assertIsNone(pad.read_captures_registry(p))


class RunEndToEndTest(unittest.TestCase):
    def test_writes_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            caps = Path(d) / 'captures.json'
            caps.write_text(json.dumps(_registry(
                _cap('a', state='parked', aging=True, title='Aging idea'),
                _cap('b', state='parked', aging=False),
            )))
            out = Path(d) / 'parked-aging-digest.json'
            rc = pad.run(now=NOW, trigger='on-demand',
                         captures_path=caps, artifact_path=out)
            self.assertEqual(rc, 0)
            digest = json.loads(out.read_text())
            self.assertEqual(digest['parked_count'], 2)
            self.assertEqual(digest['aging_count'], 1)
            self.assertEqual(digest['trigger'], 'on-demand')
            self.assertEqual(digest['items'][0]['capture_id'], 'a')

    def test_unreadable_captures_leaves_prior_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            caps = Path(d) / 'captures.json'
            caps.write_text('{ corrupt')
            out = Path(d) / 'parked-aging-digest.json'
            out.write_text('{"prior": true}')
            rc = pad.run(now=NOW, captures_path=caps, artifact_path=out)
            self.assertEqual(rc, 1)
            # Prior artifact untouched — a bad read never clobbers good data.
            self.assertEqual(json.loads(out.read_text()), {'prior': True})

    def test_invalid_trigger_returns_2(self):
        self.assertEqual(pad.run(now=NOW, trigger='hourly'), 2)


class RealCapturesFixtureTest(unittest.TestCase):
    """Selection holds against the committed captures.json (no aging items
    today, but the parked count is real) — proves the flag-only contract on
    live-shaped data."""

    def test_against_repo_captures(self):
        path = _REPO_SCRIPTS.parent / pad.CAPTURES_REL
        reg = pad.read_captures_registry(path)
        self.assertIsNotNone(reg)
        aging = pad.select_aging_parked(reg, NOW)
        # Every selected item is parked AND flagged aging — no exceptions.
        for cap in aging:
            self.assertEqual(cap.get('state'), 'parked')
            self.assertIs(cap.get('aging'), True)


if __name__ == '__main__':
    unittest.main()
