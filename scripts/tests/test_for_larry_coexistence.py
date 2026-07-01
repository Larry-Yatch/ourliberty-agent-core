#!/usr/bin/env python3
"""Phase 2.1 FIX 2 — the two producers of `for-larry-escalations.json` coexist.

`for_larry_escalations` (the `{'escalations': [...]}` schema, mirror-review
routing) and `for_larry_signal` (the `{'records': {...}}` schema, promote_alerts
+ outbox_notifier) write the SAME file in production. Before FIX 2,
`for_larry_escalations._save` wrote `{'escalations': ...}` from scratch and took
no lock — so it clobbered the sibling's `records` key, and the Phase 2 Change-C
reader (escalations-only) went blind to the `records` producers.

These tests point BOTH modules at ONE file (production reality — the existing
suites hide the bug by giving each module its own tmp file) and prove:
  1. an escalations write preserves a pre-existing `records` key (no clobber);
  2. a records write preserves a pre-existing `escalations` key;
  3. system_state_log.load_for_larry_escalations UNIONS both stores;
  4. clear_many resolves in one pass.

Run:
    python3 -m unittest scripts.tests.test_for_larry_coexistence
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


class ForLarryCoexistenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._feed = Path(self._tmp.name) / 'for-larry-escalations.json'
        # BOTH modules → the one file (production reality); Source 2 → empty.
        self._saved = {k: os.environ.get(k) for k in (
            'OURLIBERTY_FOR_LARRY_FEED_FILE',
            'OURLIBERTY_FOR_LARRY_SIGNAL_FILE',
            'OURLIBERTY_ESCALATIONS_FILE',
        )}
        os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = str(self._feed)
        os.environ['OURLIBERTY_FOR_LARRY_SIGNAL_FILE'] = str(self._feed)
        os.environ['OURLIBERTY_ESCALATIONS_FILE'] = str(
            Path(self._tmp.name) / 'pulse-none.json')
        self.fle = importlib.import_module('for_larry_escalations')
        self.fls = importlib.import_module('for_larry_signal')

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _raw(self) -> dict:
        return json.loads(self._feed.read_text())

    def test_escalations_write_preserves_records_key(self):
        # Seed a for_larry_signal record, then an escalations upsert must NOT wipe it.
        self.fls.upsert_record('promote:crit-1', {'headline': 'critical unhandled'})
        self.assertIn('promote:crit-1', self._raw().get('records', {}))

        self.fle.upsert('mirror-review:pr-9', headline='needs you',
                        context='review', pr_url='https://github.com/o/r/pull/9')

        raw = self._raw()
        self.assertIn('escalations', raw)                       # our write landed
        self.assertIn('promote:crit-1', raw.get('records', {})) # sibling survived
        self.assertEqual(len(raw['escalations']), 1)

    def test_records_write_preserves_escalations_key(self):
        self.fle.upsert('mirror-review:pr-9', headline='needs you', context='c',
                        pr_url='https://github.com/o/r/pull/9')
        self.assertEqual(len(self._raw().get('escalations', [])), 1)

        self.fls.upsert_record('promote:crit-2', {'headline': 'another'})

        raw = self._raw()
        self.assertIn('promote:crit-2', raw.get('records', {}))
        self.assertEqual(len(raw.get('escalations', [])), 1)    # sibling survived

    def test_state_log_unions_both_stores(self):
        # One record via each producer; the reader must surface BOTH.
        self.fls.upsert_record('promote:crit-3',
                               {'headline': 'signal-side needs you'})
        self.fle.upsert('mirror-review:pr-9', headline='escalation-side needs you',
                        context='c', pr_url='https://github.com/o/r/pull/9')

        ssl = importlib.import_module('system_state_log')
        items = ssl.load_for_larry_escalations()
        titles = {it['title'] for it in items}
        self.assertIn('signal-side needs you', titles)      # 1b (was blind pre-FIX2)
        self.assertIn('escalation-side needs you', titles)  # 1a
        self.assertEqual(len(items), 2)

    def test_two_distinct_signal_records_same_task_both_surface(self):
        # Phase 2.1 review Finding 1: two DIFFERENT for_larry_signal signals about
        # one task (same pr_url → same derived decision_key) must BOTH surface. 1b
        # must not dedup against itself, or a real needs-you row is dropped.
        pr = 'https://github.com/o/r/pull/9'
        self.fls.upsert_record('escalation:pr9',
                               {'pr_url': pr, 'headline': 'critical-unhandled'})
        self.fls.upsert_record('clarify-exhausted:pr9',
                               {'pr_url': pr, 'headline': 'forge-stuck'})
        ssl = importlib.import_module('system_state_log')
        titles = {it['title'] for it in ssl.load_for_larry_escalations()}
        self.assertIn('critical-unhandled', titles)
        self.assertIn('forge-stuck', titles)

    def test_clear_many_one_pass(self):
        self.fle.upsert('e1', headline='h1', context='c')
        self.fle.upsert('e2', headline='h2', context='c')
        self.fle.upsert('e3', headline='h3', context='c')
        cleared = self.fle.clear_many(['e1', 'e3', 'missing'])
        self.assertEqual(cleared, 2)
        open_ids = {r['id'] for r in self.fle.list_open()}
        self.assertEqual(open_ids, {'e2'})


if __name__ == '__main__':
    unittest.main()
