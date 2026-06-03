#!/usr/bin/env python3
"""Tests for cycle_prime_ledger (PR-β PRIME DIRECTIVE ledger).

Covers: append, trailing-30d ratio + trend, Decision II auto-promote,
atomic-append safety, OQ1 path correctness.

Run::

    cd ~/agent-core && python3 -m pytest scripts/tests/test_cycle_prime_ledger.py
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import cycle_prime_ledger as cpl  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


def _row(*, kind: str, hours_ago: float, intervention_id: str = '',
         tier: int = 1) -> dict:
    return {
        'ts': (NOW - timedelta(hours=hours_ago)).isoformat(),
        'iter': 1, 'tier': tier, 'kind': kind,
        'intervention_id': intervention_id,
        'fix_commit_sha': None, 'chain_event_id': None,
        'verifies_at': None, 'verified_at': None,
        'verification_anchor': None,
    }


class _LedgerTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._prior_root = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.tmp)
        importlib.reload(cpl)

    def tearDown(self):
        if self._prior_root is not None:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._prior_root
        else:
            del os.environ['OURLIBERTY_AGENTS_ROOT']
        importlib.reload(cpl)

    def _ledger_path(self) -> Path:
        return self.tmp / cpl.LEDGER_REL


class TestPathResolution(_LedgerTestBase):

    def test_oq1_path_uses_cycle_prime_ledger_jsonl(self):
        """OQ1 resolution: the ledger MUST be cycle-prime-ledger.jsonl,
        NOT cycle-actions.jsonl. Guards Mirror review focus #6."""
        path = self._ledger_path()
        self.assertTrue(str(path).endswith('cycle-prime-ledger.jsonl'),
                        f'unexpected path: {path!s}')
        self.assertNotIn('cycle-actions.jsonl', str(path))


class TestAppendAction(_LedgerTestBase):

    def test_append_writes_one_row(self):
        cpl.append_action(tier=1, kind='intervention',
                          payload={'intervention_id': 'abc'})
        text = self._ledger_path().read_text()
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'intervention')
        self.assertEqual(rows[0]['intervention_id'], 'abc')

    def test_append_rejects_invalid_kind(self):
        with self.assertRaises(ValueError):
            cpl.append_action(tier=1, kind='nonsense', payload={})

    def test_append_rejects_invalid_tier(self):
        with self.assertRaises(ValueError):
            cpl.append_action(tier=99, kind='intervention', payload={})

    def test_multiple_appends_preserved(self):
        for i in range(3):
            cpl.append_action(tier=1, kind='intervention',
                              payload={'intervention_id': f'id{i}'})
        rows = [json.loads(l) for l in self._ledger_path().read_text().splitlines()
                if l.strip()]
        self.assertEqual(len(rows), 3)


class TestComputeRatio30d(_LedgerTestBase):

    def test_basic_ratio(self):
        rows = [
            _row(kind='intervention', hours_ago=1),
            _row(kind='intervention', hours_ago=2),
            _row(kind='systemic_fix', hours_ago=3),
        ]
        out = cpl.compute_ratio_30d(rows=rows, now=NOW)
        self.assertEqual(out['interventions'], 2)
        self.assertEqual(out['systemic_fixes'], 1)
        self.assertEqual(out['ratio'], 2.0)

    def test_ratio_na_when_no_systemic_fix(self):
        rows = [_row(kind='intervention', hours_ago=1)]
        out = cpl.compute_ratio_30d(rows=rows, now=NOW)
        self.assertEqual(out['ratio'], 'N/A')

    def test_filters_outside_30d_window(self):
        rows = [
            _row(kind='intervention', hours_ago=1),
            _row(kind='intervention', hours_ago=24 * 60),   # 60d old
            _row(kind='systemic_fix', hours_ago=2),
        ]
        out = cpl.compute_ratio_30d(rows=rows, now=NOW)
        self.assertEqual(out['interventions'], 1)
        self.assertEqual(out['systemic_fixes'], 1)
        self.assertEqual(out['ratio'], 1.0)

    def test_trend_improving(self):
        # Older half: 4 interventions per 1 fix (ratio 4).
        # Recent half: 1 intervention per 1 fix (ratio 1).
        rows = []
        for _ in range(4):
            rows.append(_row(kind='intervention', hours_ago=20 * 24))
        rows.append(_row(kind='systemic_fix', hours_ago=20 * 24))
        rows.append(_row(kind='intervention', hours_ago=1))
        rows.append(_row(kind='systemic_fix', hours_ago=2))
        out = cpl.compute_ratio_30d(rows=rows, now=NOW)
        self.assertEqual(out['trend'], 'improving')

    def test_trend_worsening(self):
        rows = []
        rows.append(_row(kind='intervention', hours_ago=20 * 24))
        rows.append(_row(kind='systemic_fix', hours_ago=20 * 24))
        for _ in range(4):
            rows.append(_row(kind='intervention', hours_ago=1))
        rows.append(_row(kind='systemic_fix', hours_ago=2))
        out = cpl.compute_ratio_30d(rows=rows, now=NOW)
        self.assertEqual(out['trend'], 'worsening')

    def test_trend_flat_with_equal_halves(self):
        rows = []
        for h in (20 * 24, 1):
            rows.append(_row(kind='intervention', hours_ago=h))
            rows.append(_row(kind='systemic_fix', hours_ago=h))
        out = cpl.compute_ratio_30d(rows=rows, now=NOW)
        self.assertEqual(out['trend'], 'flat')


class TestPromoteVerificationPending(_LedgerTestBase):

    def test_promotes_within_window(self):
        rows = [
            _row(kind='verification_pending', hours_ago=24,
                 intervention_id='x1'),
        ]
        new_row = cpl.promote_verification_pending('x1', rows=rows, now=NOW)
        self.assertIsNotNone(new_row)
        self.assertEqual(new_row['kind'], 'systemic_fix')
        self.assertEqual(new_row['intervention_id'], 'x1')
        self.assertIsNotNone(new_row['verified_at'])
        # New row was written to disk.
        rows_on_disk = [
            json.loads(l) for l in self._ledger_path().read_text().splitlines()
            if l.strip()
        ]
        self.assertEqual(len(rows_on_disk), 1)
        self.assertEqual(rows_on_disk[0]['kind'], 'systemic_fix')

    def test_no_promote_after_7d(self):
        rows = [
            _row(kind='verification_pending', hours_ago=8 * 24,
                 intervention_id='x1'),
        ]
        out = cpl.promote_verification_pending('x1', rows=rows, now=NOW)
        self.assertIsNone(out)

    def test_no_double_promote(self):
        rows = [
            _row(kind='verification_pending', hours_ago=24,
                 intervention_id='x1'),
            _row(kind='systemic_fix', hours_ago=12, intervention_id='x1'),
        ]
        out = cpl.promote_verification_pending('x1', rows=rows, now=NOW)
        self.assertIsNone(out)

    def test_unknown_id_returns_none(self):
        out = cpl.promote_verification_pending('missing', rows=[], now=NOW)
        self.assertIsNone(out)


class TestCanonicalInterventionId(unittest.TestCase):
    """The load-bearing tagging helper: a canonical id is
    '<template>:<detail>' with a kebab-case template that Check V can
    aggregate on. Reject anything that would fragment per-template
    track record."""

    def test_joins_template_and_detail(self):
        self.assertEqual(
            cpl.canonical_intervention_id('restart-daemon', 'beacon'),
            'restart-daemon:beacon')

    def test_empty_detail_allowed(self):
        out = cpl.canonical_intervention_id('reinstall-systemd-unit')
        self.assertEqual(out, 'reinstall-systemd-unit:')

    def test_template_prefix_is_nonempty(self):
        """The whole point: _template_of-style split yields a real
        per-template key, never a singleton."""
        out = cpl.canonical_intervention_id('retry-sync-push', 'iter-117')
        self.assertEqual(out.split(':', 1)[0], 'retry-sync-push')
        self.assertTrue(out.split(':', 1)[0])

    def test_detail_may_contain_colon(self):
        out = cpl.canonical_intervention_id('restart-daemon', 'a:b:c')
        # Only the first ':' delimits; the template prefix is still clean.
        self.assertEqual(out.split(':', 1)[0], 'restart-daemon')
        self.assertEqual(out, 'restart-daemon:a:b:c')

    def test_rejects_empty_template(self):
        with self.assertRaises(ValueError):
            cpl.canonical_intervention_id('', 'x')

    def test_rejects_uppercase(self):
        with self.assertRaises(ValueError):
            cpl.canonical_intervention_id('Restart-Daemon', 'x')

    def test_rejects_whitespace(self):
        with self.assertRaises(ValueError):
            cpl.canonical_intervention_id('restart daemon', 'x')

    def test_rejects_colon_in_template(self):
        with self.assertRaises(ValueError):
            cpl.canonical_intervention_id('restart:daemon', 'x')

    def test_rejects_leading_digit(self):
        with self.assertRaises(ValueError):
            cpl.canonical_intervention_id('1restart', 'x')

    def test_rejects_underscore(self):
        with self.assertRaises(ValueError):
            cpl.canonical_intervention_id('restart_daemon', 'x')


class TestCliTemplateTagging(_LedgerTestBase):
    """The CLI --template/--detail path is what the cycle prompt calls.
    It must write a tagged row on success and write NOTHING (non-zero
    exit) on a malformed template."""

    def test_cli_template_path_writes_tagged_row(self):
        rc = cpl.main(['append', '--tier', '1', '--kind', 'intervention',
                       '--iter', '5', '--template', 'restart-daemon',
                       '--detail', 'beacon'])
        self.assertEqual(rc, 0)
        rows = [json.loads(l) for l in
                self._ledger_path().read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['intervention_id'], 'restart-daemon:beacon')
        # Check-V-style aggregation key is non-empty.
        self.assertTrue(rows[0]['intervention_id'].split(':', 1)[0])

    def test_cli_empty_detail_still_tagged(self):
        rc = cpl.main(['append', '--tier', '1', '--kind', 'intervention',
                       '--template', 'reinstall-systemd-unit'])
        self.assertEqual(rc, 0)
        rows = [json.loads(l) for l in
                self._ledger_path().read_text().splitlines() if l.strip()]
        self.assertEqual(rows[0]['intervention_id'],
                         'reinstall-systemd-unit:')

    def test_cli_bad_template_exits_nonzero_and_writes_nothing(self):
        rc = cpl.main(['append', '--tier', '1', '--kind', 'intervention',
                       '--template', 'Bad Template'])
        self.assertNotEqual(rc, 0)
        self.assertFalse(self._ledger_path().exists(),
                         'a malformed tag must never write a ledger row')

    def test_cli_detail_without_template_exits_nonzero(self):
        rc = cpl.main(['append', '--tier', '1', '--kind', 'intervention',
                       '--detail', 'orphan-detail'])
        self.assertNotEqual(rc, 0)
        self.assertFalse(self._ledger_path().exists())

    def test_cli_legacy_payload_path_still_works(self):
        """The lenient --payload path is untouched for internal callers."""
        rc = cpl.main(['append', '--tier', '1', '--kind', 'intervention',
                       '--payload', '{"intervention_id": "legacy-id"}'])
        self.assertEqual(rc, 0)
        rows = [json.loads(l) for l in
                self._ledger_path().read_text().splitlines() if l.strip()]
        self.assertEqual(rows[0]['intervention_id'], 'legacy-id')


if __name__ == '__main__':
    unittest.main()
