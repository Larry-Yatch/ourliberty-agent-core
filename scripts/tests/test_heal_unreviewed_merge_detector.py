#!/usr/bin/env python3
"""Tests for heal_unreviewed_merge_detector (Build B of the Mirror-review
merge gate).

Covers the brief's acceptance set:
  - merged-with-pass -> no alert
  - merged-without-pass -> alert
  - cursor advances
  - idempotent (no duplicate alerts on re-scan)
plus: the review-pass log-evidence parser (merged/already_merged/failed/held
outcomes, repr'd + bare pr= shapes), the gh-json parser, baseline-first-run,
dry-run mode, gh-failure tolerance, and main() routing through a stubbed
larry_alerts.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_unreviewed_merge_detector
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_unreviewed_merge_detector as h  # noqa: E402


def _pr(number: int, merged_at: str, login: str = 'Larry-Yatch',
        title: str = 't') -> dict:
    return {
        'number': number,
        'mergedAt': merged_at,
        'url': f'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/{number}',
        'author_login': login,
        'title': title,
    }


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test so the
    module-level LOG/STATE/HEARTBEAT/KILL_SWITCH paths don't touch prod
    ~/agents/. Reload the module so its constants pick up the override."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='agents-root-umd-')
        for sub in ('logs', 'state', 'blackboard'):
            os.makedirs(os.path.join(self._tmp, sub), exist_ok=True)
        self._orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmp
        importlib.reload(h)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._orig
        importlib.reload(h)
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()


# -------------------- review-pass evidence parser --------------------

class ReviewPassEvidenceTest(unittest.TestCase):
    URL = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/42'

    def test_bare_pr_merged_outcome_is_evidence(self):
        log = (f'[2026-06-04 14:28:58] [notifier] [INFO] AUTO_MERGE '
               f'task=t-001 pr={self.URL} outcome=merged (--squash)')
        self.assertIn(self.URL, h.review_passed_pr_urls(log))

    def test_repr_quoted_pr_is_evidence(self):
        log = (f"[ts] [notifier] [WARN] AUTO_MERGE task=t-001 pr='{self.URL}' "
               f"outcome=failed reason=timeout after 30s")
        self.assertIn(self.URL, h.review_passed_pr_urls(log))

    def test_already_merged_outcome_is_evidence(self):
        log = (f'[ts] AUTO_MERGE task=t pr={self.URL} '
               f'outcome=already_merged')
        self.assertIn(self.URL, h.review_passed_pr_urls(log))

    def test_held_line_is_evidence(self):
        # A held/deferred merge still means a PASS was classified.
        log = (f'[ts] AUTO_MERGE_HELD task=t pr={self.URL} '
               f'reason=conflicting-overlap')
        self.assertIn(self.URL, h.review_passed_pr_urls(log))

    def test_non_automerge_line_is_not_evidence(self):
        log = (f'[ts] [notifier] [INFO] some other line mentioning {self.URL} '
               f'but not a merge')
        self.assertNotIn(self.URL, h.review_passed_pr_urls(log))

    def test_trailing_slash_normalized(self):
        log = f'[ts] AUTO_MERGE task=t pr={self.URL}/ outcome=merged'
        self.assertIn(self.URL, h.review_passed_pr_urls(log))

    def test_empty_log(self):
        self.assertEqual(h.review_passed_pr_urls(''), set())


# -------------------- gh json parser --------------------

class ParseMergedPrsTest(unittest.TestCase):
    def test_parses_rows(self):
        raw = json.dumps([
            {'number': 5, 'mergedAt': '2026-06-04T18:00:00Z',
             'url': 'https://x/pull/5', 'author': {'login': 'bob'},
             'title': 'hi'},
        ])
        rows = h.parse_merged_prs(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['number'], 5)
        self.assertEqual(rows[0]['author_login'], 'bob')

    def test_missing_author_defaults_unknown(self):
        raw = json.dumps([
            {'number': 5, 'mergedAt': '2026-06-04T18:00:00Z',
             'url': 'https://x/pull/5'},
        ])
        self.assertEqual(h.parse_merged_prs(raw)[0]['author_login'], 'unknown')

    def test_skips_incomplete_rows(self):
        raw = json.dumps([
            {'number': 5},  # no url/mergedAt -> skipped
            {'number': 6, 'mergedAt': '2026-06-04T18:00:00Z',
             'url': 'https://x/pull/6'},
        ])
        rows = h.parse_merged_prs(raw)
        self.assertEqual([r['number'] for r in rows], [6])

    def test_malformed_json_returns_empty(self):
        self.assertEqual(h.parse_merged_prs('{ not json'), [])
        self.assertEqual(h.parse_merged_prs(''), [])


# -------------------- core detection (run_once) --------------------

class RunOnceTest(unittest.TestCase):
    def test_merged_with_pass_no_alert(self):
        pr = _pr(42, '2026-06-04T18:00:00Z')
        passed = {pr['url']}
        alerts = h.run_once([pr], passed, alerted_prs={})
        self.assertEqual(alerts, [])

    def test_merged_without_pass_alerts(self):
        pr = _pr(324, '2026-06-04T18:00:00Z', login='Larry-Yatch')
        alerts = h.run_once([pr], passed_urls=set(), alerted_prs={})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['number'], 324)
        self.assertEqual(alerts[0]['subject'], 'unreviewed-merge:324')
        self.assertIn('PR #324 merged without Mirror review', alerts[0]['message'])
        self.assertIn('actor=Larry-Yatch', alerts[0]['message'])

    def test_already_alerted_is_skipped(self):
        pr = _pr(324, '2026-06-04T18:00:00Z')
        alerted = {pr['url']: '2026-06-04T18:01:00Z'}
        self.assertEqual(h.run_once([pr], set(), alerted), [])

    def test_mixed_batch_only_unreviewed_alerts(self):
        good = _pr(40, '2026-06-04T17:00:00Z')
        bad = _pr(41, '2026-06-04T17:30:00Z')
        alerts = h.run_once([good, bad], passed_urls={good['url']}, alerted_prs={})
        self.assertEqual([a['number'] for a in alerts], [41])


# -------------------- cursor advance --------------------

class AdvanceCursorTest(unittest.TestCase):
    def test_advances_to_newest(self):
        prs = [
            _pr(1, '2026-06-04T10:00:00Z'),
            _pr(2, '2026-06-04T12:00:00Z'),
        ]
        new = h.advance_cursor(prs, '2026-06-04T09:00:00Z')
        self.assertEqual(h._parse_iso(new), h._parse_iso('2026-06-04T12:00:00Z'))

    def test_never_moves_backward(self):
        prs = [_pr(1, '2026-06-04T08:00:00Z')]
        new = h.advance_cursor(prs, '2026-06-04T12:00:00Z')
        self.assertEqual(h._parse_iso(new), h._parse_iso('2026-06-04T12:00:00Z'))

    def test_empty_batch_keeps_cursor(self):
        new = h.advance_cursor([], '2026-06-04T12:00:00Z')
        self.assertEqual(h._parse_iso(new), h._parse_iso('2026-06-04T12:00:00Z'))


# -------------------- gh search cursor flooring --------------------

class GhSearchCursorTest(unittest.TestCase):
    def test_floors_stale_cursor_to_lookback(self):
        now = datetime(2026, 6, 4, 18, 0, 0, tzinfo=timezone.utc)
        # Cursor 30 days back -> floored to MAX_LOOKBACK_DAYS.
        out = h._gh_search_cursor('2026-05-05T00:00:00Z', now)
        floor = (now - timedelta(days=h.MAX_LOOKBACK_DAYS))
        self.assertEqual(out, floor.strftime('%Y-%m-%dT%H:%M:%S+00:00'))

    def test_recent_cursor_preserved(self):
        now = datetime(2026, 6, 4, 18, 0, 0, tzinfo=timezone.utc)
        out = h._gh_search_cursor('2026-06-04T17:00:00Z', now)
        self.assertEqual(out, '2026-06-04T17:00:00+00:00')


# -------------------- state load/save round-trip --------------------

class StateTest(_IsolatedAgentsRoot):
    def test_round_trip(self):
        h.save_state({'cursor_iso': '2026-06-04T18:00:00Z',
                      'alerted_prs': {'u': 't'}})
        st = h.load_state()
        self.assertEqual(st['cursor_iso'], '2026-06-04T18:00:00Z')
        self.assertEqual(st['alerted_prs'], {'u': 't'})

    def test_missing_file_defaults(self):
        st = h.load_state()
        self.assertIsNone(st['cursor_iso'])
        self.assertEqual(st['alerted_prs'], {})

    def test_ledger_trimmed(self):
        big = {f'u{i}': f'2026-06-04T{i % 24:02d}:00:00Z'
               for i in range(h.MAX_ALERTED_LEDGER + 50)}
        h.save_state({'cursor_iso': None, 'alerted_prs': big})
        st = h.load_state()
        self.assertLessEqual(len(st['alerted_prs']), h.MAX_ALERTED_LEDGER)


# -------------------- main() orchestration --------------------

class _FakeAlerts:
    def __init__(self):
        self.calls = []

    def append_alert(self, **kw):
        self.calls.append(kw)
        return True


class MainTest(_IsolatedAgentsRoot):
    def setUp(self):
        super().setUp()
        self._fake = _FakeAlerts()
        patch = mock.patch.dict(sys.modules, {'larry_alerts': self._fake})
        patch.start()
        self.addCleanup(patch.stop)
        # Default: detector enabled (not dry-run).
        env = mock.patch.dict(os.environ,
                              {h.ENV_ENABLED: 'true'})
        env.start()
        self.addCleanup(env.stop)

    def _seed_cursor(self, iso: str) -> None:
        h.save_state({'cursor_iso': iso, 'alerted_prs': {}})

    def test_first_run_establishes_baseline_no_alert(self):
        # No cursor -> baseline, no gh call, no alert.
        with mock.patch.object(h, 'fetch_merged_prs') as fetch:
            rc = h.main()
        self.assertEqual(rc, 0)
        fetch.assert_not_called()
        self.assertEqual(self._fake.calls, [])
        self.assertIsNotNone(h.load_state()['cursor_iso'])

    def test_merged_without_pass_emits_alert(self):
        self._seed_cursor('2026-06-04T00:00:00Z')
        pr = _pr(324, '2026-06-04T18:00:00Z')
        with mock.patch.object(h, 'fetch_merged_prs', return_value=[pr]), \
                mock.patch.object(h, 'read_notifier_log', return_value=''):
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._fake.calls), 1)
        call = self._fake.calls[0]
        self.assertEqual(call['source'], 'heal-unreviewed-merge-detector')
        self.assertEqual(call['severity'], 'critical')
        self.assertEqual(call['route'], 'escalate')
        self.assertEqual(call['subject'], 'unreviewed-merge:324')

    def test_merged_with_pass_no_alert(self):
        self._seed_cursor('2026-06-04T00:00:00Z')
        pr = _pr(42, '2026-06-04T18:00:00Z')
        log = f'AUTO_MERGE task=t pr={pr["url"]} outcome=merged'
        with mock.patch.object(h, 'fetch_merged_prs', return_value=[pr]), \
                mock.patch.object(h, 'read_notifier_log', return_value=log):
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._fake.calls, [])

    def test_cursor_advances_after_scan(self):
        self._seed_cursor('2026-06-04T00:00:00Z')
        pr = _pr(50, '2026-06-04T18:00:00Z')
        log = f'AUTO_MERGE task=t pr={pr["url"]} outcome=merged'
        with mock.patch.object(h, 'fetch_merged_prs', return_value=[pr]), \
                mock.patch.object(h, 'read_notifier_log', return_value=log):
            h.main()
        cur = h.load_state()['cursor_iso']
        self.assertEqual(h._parse_iso(cur), h._parse_iso('2026-06-04T18:00:00Z'))

    def test_idempotent_no_duplicate_alert_on_rescan(self):
        self._seed_cursor('2026-06-04T00:00:00Z')
        pr = _pr(324, '2026-06-04T18:00:00Z')
        # Re-presenting the same PR (cursor uses >= so the boundary PR recurs).
        with mock.patch.object(h, 'fetch_merged_prs', return_value=[pr]), \
                mock.patch.object(h, 'read_notifier_log', return_value=''):
            h.main()
            h.main()
        self.assertEqual(len(self._fake.calls), 1)
        self.assertIn(pr['url'], h.load_state()['alerted_prs'])

    def test_dry_run_detects_but_does_not_emit(self):
        with mock.patch.dict(os.environ, {h.ENV_ENABLED: 'false'}):
            self._seed_cursor('2026-06-04T00:00:00Z')
            pr = _pr(324, '2026-06-04T18:00:00Z')
            with mock.patch.object(h, 'fetch_merged_prs', return_value=[pr]), \
                    mock.patch.object(h, 'read_notifier_log', return_value=''):
                rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._fake.calls, [])
        # Dry-run leaves the PR un-recorded so a later live run still pages.
        self.assertNotIn(pr['url'], h.load_state()['alerted_prs'])

    def test_kill_switch_exits_clean(self):
        (h.AGENTS_ROOT / 'healers.disabled').write_text('')
        with mock.patch.object(h, 'fetch_merged_prs') as fetch:
            rc = h.main()
        self.assertEqual(rc, 0)
        fetch.assert_not_called()

    def test_gh_failure_tolerated(self):
        # fetch returns [] on gh error; main must not crash and emit nothing.
        self._seed_cursor('2026-06-04T00:00:00Z')
        with mock.patch.object(h, 'fetch_merged_prs', return_value=[]), \
                mock.patch.object(h, 'read_notifier_log', return_value=''):
            rc = h.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._fake.calls, [])


# -------------------- fetch gh-failure tolerance (subprocess seam) --------------------

class FetchToleranceTest(_IsolatedAgentsRoot):
    def test_nonzero_returncode_returns_empty(self):
        now = datetime.now(timezone.utc)
        fake = mock.Mock(returncode=1, stdout='', stderr='boom')
        with mock.patch.object(h.subprocess, 'run', return_value=fake):
            self.assertEqual(h.fetch_merged_prs('2026-06-04T00:00:00Z', now), [])

    def test_timeout_returns_empty(self):
        now = datetime.now(timezone.utc)
        with mock.patch.object(
                h.subprocess, 'run',
                side_effect=h.subprocess.TimeoutExpired('gh', 30)):
            self.assertEqual(h.fetch_merged_prs('2026-06-04T00:00:00Z', now), [])


if __name__ == '__main__':
    unittest.main()
