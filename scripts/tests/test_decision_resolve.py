#!/usr/bin/env python3
"""Integration tests for decision_resolve.resolve_decision (Approval-Sync
Phase 2, Change B — spec docs/approval-sync-phase2-spec.md §2 / §3).

resolve_decision is the SINGLE fan-out that clears the same needs-Larry decision
from all four stores synchronously:

  P — beacon-pending-approvals.json   (via beacon_approval_handler.resolve)
  C — Supabase chain_events           (via chain_event_emit.clear_decision)
  E — for-larry-escalations.json      (via for_larry_escalations.clear)
  A — larry-alerts.jsonl              (via larry_alerts.resolve_alert_by_decision_key)

These tests seed ONE decision under ONE canonical key into all four stores, call
resolve_decision once, and assert every leg cleared. Then a second call proves
idempotency (a re-resolve clears nothing). A per-leg failure-isolation test
proves one unreachable store never aborts the other three (spec §6).

Store isolation:
  - P: PENDING_APPROVALS_PATH patched to a tmp file.
  - C: a custom in-memory chainable fake passed as `chain_client` (the real
    _fake_supabase only covers create_client, not the .update().in_().is_().eq()
    chain clear_decision drives).
  - E: OURLIBERTY_FOR_LARRY_FEED_FILE env override to a tmp file.
  - A: larry_alerts AGENTS_ROOT/ALERTS_FILE/COOLDOWN/SILENCE/OFFSET patched to a
    tmpdir. larry_alerts is a Layer-B guarded chokepoint, so the module opts out
    of the run-sentinel guard via _chokepoint_optout (the blessed pattern from
    test_larry_alerts) — the #428 real-tree leak scanner still runs.

Run:
    python3 -m unittest scripts.tests.test_decision_resolve
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import beacon_approval_handler as bah  # noqa: E402
import decision_resolve  # noqa: E402
import for_larry_escalations as fle  # noqa: E402
import larry_alerts  # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # The A-leg drives larry_alerts (a Layer-B guarded chokepoint) against
    # already-isolated tmp state, so the run-sentinel guard would breach before
    # the test's own path mocks. Opt the module out (the #428 leak scanner still
    # runs) — the same pattern test_larry_alerts uses.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)


# ---------------- in-memory chainable Supabase fake (C-leg) ----------------

class _FakeChainTable:
    """Records the .update().in_().is_().eq() chain clear_decision builds and
    applies it against a shared in-memory row list on .execute()."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._update: dict | None = None
        self._filters: list[tuple] = []

    def update(self, values, count=None):  # noqa: ARG002 — count is accepted/ignored
        self._update = values
        return self

    def in_(self, column, values):
        self._filters.append(('in', column, values))
        return self

    def is_(self, column, value):
        self._filters.append(('is', column, value))
        return self

    def eq(self, column, value):
        self._filters.append(('eq', column, value))
        return self

    def _matches(self, row: dict) -> bool:
        for kind, column, value in self._filters:
            if kind == 'in':
                if row.get(column) not in value:
                    return False
            elif kind == 'is':
                # only is_(col, 'null') is used
                if value == 'null' and row.get(column) is not None:
                    return False
            elif kind == 'eq':
                if column == 'payload->>decision_key':
                    payload = row.get('payload') or {}
                    if payload.get('decision_key') != value:
                        return False
                elif row.get(column) != value:
                    return False
        return True

    def execute(self):
        matched = [r for r in self._rows if self._matches(r)]
        for row in matched:
            row.update(self._update or {})
        return types.SimpleNamespace(data=list(matched), count=len(matched))


class _FakeChainClient:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def table(self, name: str):
        assert name == 'chain_events'
        return _FakeChainTable(self._rows)


# ---------------- the integration fixture ----------------

class FourStoreFanOutTest(unittest.TestCase):
    PR_URL = 'https://github.com/ourliberty/ourliberty-agent-core/pull/777'
    KEY = 'pr-ourliberty-agent-core-777'
    TASK_ID = 'mirror-review:ourliberty-agent-core-777'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)

        # P store — beacon-pending-approvals.json at a tmp path.
        self._pending_path = tmp / 'beacon-pending-approvals.json'
        self._p_patches = [
            mock.patch.object(bah, 'PENDING_APPROVALS_PATH', self._pending_path),
        ]

        # A store — larry_alerts paths.
        self._a_patches = [
            mock.patch.object(larry_alerts, 'AGENTS_ROOT', tmp),
            mock.patch.object(larry_alerts, 'ALERTS_FILE',
                              tmp / 'blackboard' / 'larry-alerts.jsonl'),
            mock.patch.object(larry_alerts, 'COOLDOWN_ROOT',
                              tmp / 'state' / 'alert-cooldown'),
            mock.patch.object(larry_alerts, 'SILENCE_ROOT',
                              tmp / 'state' / 'alert-silenced'),
            mock.patch.object(larry_alerts, 'OFFSET_FILE',
                              tmp / 'state' / 'beacon-alerts-offset.txt'),
        ]
        for p in (*self._p_patches, *self._a_patches):
            p.start()

        # E store — for-larry-escalations.json via env override.
        self._feed_path = tmp / 'for-larry-escalations.json'
        self._saved_feed_env = os.environ.get('OURLIBERTY_FOR_LARRY_FEED_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = str(self._feed_path)

        # C store — in-memory chain_events rows, addressed by the canonical key.
        self._chain_rows = [{
            'event_type': 'approval_request',
            'task_id': 'mirror-review-777-raw',
            'payload': {'decision_key': self.KEY},
            'read_at': None,
        }]
        self._chain_client = _FakeChainClient(self._chain_rows)

    def tearDown(self):
        for p in (*self._p_patches, *self._a_patches):
            p.stop()
        if self._saved_feed_env is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_FEED_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = self._saved_feed_env
        self._tmp.cleanup()

    # -- seeding helpers --------------------------------------------------

    def _seed_pending(self):
        bah.add_pending(
            {'task_id': self.TASK_ID, 'pr_url': self.PR_URL, 'summary': 's'},
            chat_id=42,
        )
        entry = bah.find_pending_by_id(self.TASK_ID)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get('decision_key'), self.KEY)

    def _seed_escalation(self):
        row = fle.upsert(
            self.TASK_ID, headline='needs you', context='do the thing',
            pr_url=self.PR_URL,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.get('decision_key'), self.KEY)

    def _seed_alert(self):
        ok = larry_alerts.append_alert(
            source='mirror', severity='warning', message='needs review',
            subject='pr777', route='escalate', decision_key=self.KEY,
        )
        self.assertTrue(ok)

    def _seed_all(self):
        self._seed_pending()
        self._seed_escalation()
        self._seed_alert()

    # -- the assertions ---------------------------------------------------

    def test_single_call_clears_all_four_stores(self):
        self._seed_all()

        result = decision_resolve.resolve_decision(
            self.KEY, 'approved', entry_id=self.TASK_ID, actor='telegram',
            note='lgtm', chain_client=self._chain_client,
        )

        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['chain_events'], 1)
        self.assertEqual(result['escalations'], 1)
        self.assertEqual(result['alerts'], 1)
        self.assertEqual(result['total'], 4)

        # P — entry moved out of pending into history with the outcome.
        self.assertIsNone(bah.find_pending_by_id(self.TASK_ID))
        moved = bah.find_by_id_any_state(self.TASK_ID)
        self.assertEqual(moved['status'], 'approved')

        # C — the chain row is now read (read_at stamped).
        self.assertIsNotNone(self._chain_rows[0]['read_at'])

        # E — no open escalation rows remain for this key.
        self.assertEqual(fle.list_open(), [])

        # A — the escalate alert line is retracted.
        self.assertFalse(larry_alerts.ALERTS_FILE.exists()
                         and larry_alerts.ALERTS_FILE.read_text().strip())

    def test_second_call_is_noop_idempotent(self):
        self._seed_all()
        first = decision_resolve.resolve_decision(
            self.KEY, 'approved', entry_id=self.TASK_ID,
            chain_client=self._chain_client)
        self.assertEqual(first['total'], 4)

        second = decision_resolve.resolve_decision(
            self.KEY, 'approved', entry_id=self.TASK_ID,
            chain_client=self._chain_client)
        self.assertEqual(second['pending'], 0)
        self.assertEqual(second['chain_events'], 0)
        self.assertEqual(second['escalations'], 0)
        self.assertEqual(second['alerts'], 0)
        self.assertEqual(second['total'], 0)

    def test_resolve_via_entry_coordinates(self):
        # The convenience wrapper that derives the key from task_id/pr_url.
        self._seed_all()
        result = decision_resolve.resolve_decision_for_entry(
            task_id=self.TASK_ID, pr_url=self.PR_URL, entry_id=self.TASK_ID,
            outcome='rejected', chain_client=self._chain_client,
        )
        self.assertEqual(result['key'], self.KEY)
        self.assertEqual(result['total'], 4)

    def test_none_key_skips_fanout(self):
        result = decision_resolve.resolve_decision(None, 'approved')
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['pending'], 0)

    def test_invalid_outcome_skips_fanout(self):
        self._seed_all()
        result = decision_resolve.resolve_decision(
            self.KEY, 'bogus', chain_client=self._chain_client)
        self.assertEqual(result['total'], 0)
        # Nothing was touched — the pending entry survives.
        self.assertIsNotNone(bah.find_pending_by_id(self.TASK_ID))

    def test_one_failing_leg_does_not_abort_others(self):
        # Spec §6: best-effort per leg, total intent. A C-leg client that raises
        # must not stop P/E/A from clearing.
        self._seed_all()

        class _BoomClient:
            def table(self, name):  # noqa: ARG002
                raise RuntimeError('supabase down')

        result = decision_resolve.resolve_decision(
            self.KEY, 'approved', entry_id=self.TASK_ID,
            chain_client=_BoomClient())

        self.assertEqual(result['chain_events'], 0)   # the failed leg
        self.assertEqual(result['pending'], 1)        # the other three cleared
        self.assertEqual(result['escalations'], 1)
        self.assertEqual(result['alerts'], 1)
        self.assertEqual(result['total'], 3)

    def test_resolves_only_acted_on_entry_not_sibling(self):
        # Phase 2.1 FIX 1: two DISTINCT pending approvals for the SAME PR collapse
        # to one canonical key (pr_url precedence). Resolving one must pop ONLY
        # that entry; the sibling stays pending (Phase 2 wrongly retired both,
        # silently moving the undispatched sibling to history as 'approved').
        id_a = 'mirror-review:agent-core-777-passA'
        id_b = 'mirror-review:agent-core-777-passB'
        bah.add_pending({'task_id': id_a, 'pr_url': self.PR_URL, 'summary': 'A'},
                        chat_id=1)
        bah.add_pending({'task_id': id_b, 'pr_url': self.PR_URL, 'summary': 'B'},
                        chat_id=2)
        entry_a = bah.find_pending_by_id(id_a)
        entry_b = bah.find_pending_by_id(id_b)
        # Both derive the SAME cross-store key — the collision that caused the bug.
        self.assertEqual(entry_a.get('decision_key'), self.KEY)
        self.assertEqual(entry_b.get('decision_key'), self.KEY)

        result = decision_resolve.resolve_decision_for_pending_entry(
            entry_a, 'approved', chain_client=self._chain_client)

        # Exactly ONE pending entry resolved (entry A), not both.
        self.assertEqual(result['pending'], 1)
        self.assertIsNone(bah.find_pending_by_id(id_a))         # acted-on: gone
        self.assertIsNotNone(bah.find_pending_by_id(id_b))      # sibling: STILL pending
        self.assertEqual(bah.find_by_id_any_state(id_a)['status'], 'approved')

    def test_mark_done_on_escalation_does_not_pop_pending(self):
        # Phase 2.1 FIX 1: a resolve that acts on a non-approval surface (entry_id
        # absent) must clear C/E/A by key but NEVER pop a pending approval that
        # merely shares that key — the dashboard mark_done-on-escalation case.
        self._seed_pending()                       # a genuine pending approval
        self.assertEqual(
            bah.find_pending_by_id(self.TASK_ID).get('decision_key'), self.KEY)

        result = decision_resolve.resolve_decision(
            self.KEY, 'approved', entry_id=None, chain_client=self._chain_client)

        self.assertEqual(result['pending'], 0)                  # P untouched
        self.assertIsNotNone(bah.find_pending_by_id(self.TASK_ID))  # still pending


if __name__ == '__main__':
    unittest.main()
