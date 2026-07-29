#!/usr/bin/env python3
"""Regression test for the State Log escalations undercount (Approval-Sync
Phase 2, Change C — spec docs/approval-sync-phase2-spec.md §2 Change C / §3).

THE BUG (live 2026-06-30): `waiting_on_larry.escalations` read 0 while three
records (#749 / #751 / #766) sat unresolved. Root cause: the State Log read
Source 1 from `for_larry_signal.active_entries()` (a `{'records': {...}}` shape)
while the live producer — the outbox_notifier mirror-review routing site — writes
the `{'escalations': [...]}` shape via `for_larry_escalations.upsert`. The two
writers collide on the SAME file path; `for_larry_signal` could not see the
`for_larry_escalations` rows, so they counted as zero.

The fix repoints Source 1 at the actual producer's `for_larry_escalations
.list_open()`. This test seeds three OPEN `for_larry: true` records into that
store and asserts the State Log now sees all three — both via
`load_for_larry_escalations()` and through the `waiting_on_larry.escalations`
count the /where-we-are panel reads.

Run:
    python3 -m unittest scripts.tests.test_system_state_log_escalation_count
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import for_larry_escalations as fle  # noqa: E402
import system_state_log as ssl  # noqa: E402


class EscalationCountRegressionTest(unittest.TestCase):
    # `load_for_larry_escalations` reads TWO producers off the SAME file (the
    # 1a `escalations` list and the 1b `records` map — see its docstring), and
    # each has its own override var. Pinning only the 1a var left 1b resolving
    # through OURLIBERTY_AGENTS_ROOT at call time, so this test's result
    # depended on ambient process state: when a preceding module dropped the
    # sandbox root, 1b read `/home/larry/agents` and these counts picked up
    # production's real open rows (2026-07-29). Pin BOTH, at one path, the way
    # production has them.
    _FEED_ENV_KEYS = (
        'OURLIBERTY_FOR_LARRY_FEED_FILE',    # for_larry_escalations (1a)
        'OURLIBERTY_FOR_LARRY_SIGNAL_FILE',  # for_larry_signal      (1b)
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._feed_path = Path(self._tmp.name) / 'for-larry-escalations.json'
        self._saved = {k: os.environ.get(k) for k in self._FEED_ENV_KEYS}
        for k in self._FEED_ENV_KEYS:
            os.environ[k] = str(self._feed_path)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _seed_three_open_records(self):
        for n in (749, 751, 766):
            row = fle.upsert(
                f'mirror-review:pr-{n}',
                headline=f'PR #{n} needs you',
                context='review the revision',
                pr_url=f'https://github.com/ourliberty/ourliberty-agent-core/pull/{n}',
            )
            self.assertIsNotNone(row)
            self.assertIs(row['for_larry'], True)

    def test_three_open_records_counted(self):
        self._seed_three_open_records()

        items = ssl.load_for_larry_escalations()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i['source'] == 'escalation' for i in items))
        # Each waiting-item carries the canonical decision key (Change A stamp).
        self.assertTrue(all(i.get('decision_key') for i in items))

        waiting = ssl._build_waiting_on_larry(
            parked=0, parked_items=[], pending_approvals=[],
            escalations=items, sequences=[], now=datetime.now(timezone.utc),
        )
        self.assertEqual(waiting['escalations'], 3)
        self.assertEqual(waiting['total'], 3)

    def test_resolved_records_not_counted(self):
        self._seed_three_open_records()
        fle.clear('mirror-review:pr-749')

        items = ssl.load_for_larry_escalations()
        self.assertEqual(len(items), 2)

    def test_empty_store_counts_zero(self):
        items = ssl.load_for_larry_escalations()
        self.assertEqual(items, [])


if __name__ == '__main__':
    unittest.main()
