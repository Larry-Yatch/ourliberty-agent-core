"""Tests for scripts/heal_stale_pr_escalations.py.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage:
- evaluate() clears MERGED and CLOSED PRs
- evaluate() LEAVES an OPEN PR (genuine escalation) and an UNKNOWN one (gh
  unverifiable — verify-before-alarm)
- evaluate() reaps a synthetic (non-github) pr_url and skips a rows with no url
- evaluate() ignores non-mirror-review rows (scope guard)
- the github-PR-URL regex accepts real shapes and rejects the leaked fixture
- run_cycle in dry-run does not clear; --apply clears through a real temp feed
- run_cycle skips rows the probe cannot verify
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
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import for_larry_escalations as fle
import heal_stale_pr_escalations as heal
import task_terminal_state as tts

_GH = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/'


def _row(rid, pr_url, source='mirror-review', resolved=False):
    return {
        'id': rid,
        'source': source,
        'pr_url': pr_url,
        'resolved': resolved,
        'headline': f'Session-less PR needs you: {rid}',
        'context': 'x',
    }


class EvaluateTest(unittest.TestCase):
    def _states(self, mapping):
        """Return a pr_state fn backed by a url->state dict (default UNKNOWN)."""
        return lambda url: mapping.get(url, tts.UNKNOWN)

    def test_clears_merged_and_closed(self):
        rows = [_row('a', _GH + '749'), _row('b', _GH + '751')]
        actions = heal.evaluate(rows, self._states({
            _GH + '749': tts.MERGED,
            _GH + '751': tts.CLOSED,
        }))
        self.assertEqual({r['id'] for r, _ in actions}, {'a', 'b'})

    def test_leaves_open_pr(self):
        rows = [_row('a', _GH + '900')]
        actions = heal.evaluate(rows, self._states({_GH + '900': tts.OPEN}))
        self.assertEqual(actions, [])

    def test_skips_unknown_state(self):
        # gh unreachable -> UNKNOWN -> must NOT clear (outage != resolved).
        rows = [_row('a', _GH + '900')]
        actions = heal.evaluate(rows, self._states({}))  # everything UNKNOWN
        self.assertEqual(actions, [])

    def test_reaps_synthetic_pr_url(self):
        rows = [_row('t1', 'https://gh/o/r/pull/9')]
        # Probe would never even be consulted; assert we don't call gh for it.
        def _boom(url):  # pragma: no cover - must not be reached
            raise AssertionError('gh probe called for a synthetic url')
        actions = heal.evaluate(rows, _boom)
        self.assertEqual([r['id'] for r, _ in actions], ['t1'])
        self.assertIn('synthetic', actions[0][1])

    def test_skips_row_without_pr_url(self):
        rows = [_row('a', None), _row('b', '')]
        self.assertEqual(heal.evaluate(rows, self._states({})), [])

    def test_ignores_non_mirror_review_source(self):
        # A MERGED PR on a different source is out of scope — not ours to clear.
        rows = [_row('a', _GH + '749', source='board-drain')]
        actions = heal.evaluate(rows, self._states({_GH + '749': tts.MERGED}))
        self.assertEqual(actions, [])

    def test_ignores_already_resolved(self):
        rows = [_row('a', _GH + '749', resolved=True)]
        actions = heal.evaluate(rows, self._states({_GH + '749': tts.MERGED}))
        self.assertEqual(actions, [])

    def test_raising_probe_is_skipped_not_cleared(self):
        # A probe that raises must NOT abort the sweep and must NOT clear the row
        # (a raise is treated like an outage: unverifiable -> leave it).
        def _raise(url):
            raise RuntimeError('gh exploded')
        rows = [_row('a', _GH + '749'), _row('t1', 'https://gh/o/r/pull/9')]
        actions = heal.evaluate(rows, _raise)
        # The github row is skipped (probe raised); the synthetic one still reaps.
        self.assertEqual([r['id'] for r, _ in actions], ['t1'])

    def test_probe_budget_caps_gh_calls(self):
        rows = [_row(str(i), _GH + str(700 + i)) for i in range(5)]
        calls = []

        def _probe(url):
            calls.append(url)
            return tts.MERGED
        actions = heal.evaluate(rows, _probe, max_probes=2)
        self.assertEqual(len(calls), 2)          # never probes beyond the budget
        self.assertEqual(len(actions), 2)        # only the probed rows clear
        # The un-probed rows are simply left for the next tick, not cleared.


class RegexTest(unittest.TestCase):
    def test_accepts_real_pr_urls(self):
        for u in (
            _GH + '42',
            _GH + '749',
            _GH + '749/files',
            'https://github.com/owner/repo/pull/1?foo=bar',
            'https://github.com/owner/repo/pull/1#issuecomment-5',
        ):
            self.assertRegex(u, heal._GITHUB_PR_RE, u)

    def test_rejects_synthetic_and_malformed(self):
        for u in (
            'https://gh/o/r/pull/9',            # the leaked t1 fixture host
            'http://github.com/o/r/pull/9',     # not https
            'https://github.com/o/r/issues/9',  # issue, not pull
            'https://github.com/o/pull/9',      # missing repo segment
            'https://evil.com/github.com/o/r/pull/9',
            'https://github.com/o/r/pull/abc',  # non-numeric
        ):
            self.assertIsNone(heal._GITHUB_PR_RE.match(u), u)


class RunCycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            suffix='.json', delete=False, mode='w'
        )
        self._tmp.close()
        self._prev = os.environ.get('OURLIBERTY_FOR_LARRY_FEED_FILE')
        os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop('OURLIBERTY_FOR_LARRY_FEED_FILE', None)
        else:
            os.environ['OURLIBERTY_FOR_LARRY_FEED_FILE'] = self._prev
        os.unlink(self._tmp.name)

    def _seed(self):
        fle.upsert('mirror-review:merged', headline='h', context='c',
                   source='mirror-review', pr_url=_GH + '749',
                   dedup_identity=_GH + '749@sha')
        fle.upsert('mirror-review:open', headline='h', context='c',
                   source='mirror-review', pr_url=_GH + '900',
                   dedup_identity=_GH + '900@sha')
        fle.upsert('mirror-review:t1', headline='h', context='c',
                   source='mirror-review', pr_url='https://gh/o/r/pull/9',
                   dedup_identity='pr9@abc')

    def test_dry_run_clears_nothing(self):
        self._seed()
        states = {_GH + '749': tts.MERGED, _GH + '900': tts.OPEN}
        n = heal.run_cycle(apply=False, pr_state=lambda u: states.get(u, tts.UNKNOWN))
        self.assertEqual(n, 0)
        self.assertEqual(len(fle.list_open()), 3)

    def test_apply_clears_merged_and_synthetic_only(self):
        self._seed()
        states = {_GH + '749': tts.MERGED, _GH + '900': tts.OPEN}
        n = heal.run_cycle(apply=True, pr_state=lambda u: states.get(u, tts.UNKNOWN))
        self.assertEqual(n, 2)  # merged + synthetic; open survives
        open_ids = {r['id'] for r in fle.list_open()}
        self.assertEqual(open_ids, {'mirror-review:open'})

    def test_apply_skips_when_all_unverifiable(self):
        self._seed()
        # gh down for everything real; synthetic still reaps, others survive.
        n = heal.run_cycle(apply=True, pr_state=lambda u: tts.UNKNOWN)
        self.assertEqual(n, 1)  # only the synthetic t1
        open_ids = {r['id'] for r in fle.list_open()}
        self.assertEqual(open_ids, {'mirror-review:merged', 'mirror-review:open'})


if __name__ == '__main__':
    unittest.main()
