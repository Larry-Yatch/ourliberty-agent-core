"""Unit tests for heal_stale_in_review_reconcile.

Cover the pure ``evaluate`` decision matrix (the risky logic) and ``run_cycle``
wiring with injected fetch/derive/emit so no Supabase/gh/fastapi is touched.
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import heal_stale_in_review_reconcile as heal  # noqa: E402
import task_terminal_state as tts  # noqa: E402

_GH = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/'
_REQ = '2026-06-25T07:30:27.288107+00:00'          # a review_request ts
_AFTER = '2026-06-25T07:30:53Z'                     # 26s later (gh Z-format)
_BEFORE = '2026-06-25T07:00:00+00:00'               # before the request


def _card(task_id, pr_num, since=_REQ):
    return {'task_id': task_id, 'pr_url': _GH + str(pr_num), 'since': since}


def _terminal(mapping):
    """Build a pr_terminal probe from {pr_url: (state, close_ts)}."""
    return lambda url: mapping[url]


class EvaluateTests(unittest.TestCase):
    def test_merged_after_request_is_closed(self):
        cards = [_card('a', 698)]
        actions = heal.evaluate(cards, _terminal({_GH + '698': (tts.MERGED, _AFTER)}))
        self.assertEqual(len(actions), 1)
        card, state, close_ts = actions[0]
        self.assertEqual(card['task_id'], 'a')
        self.assertEqual(state, tts.MERGED)
        # close_ts is normalized ISO and strictly after the request.
        self.assertEqual(close_ts, '2026-06-25T07:30:53+00:00')

    def test_closed_after_request_is_closed(self):
        cards = [_card('a', 703)]
        actions = heal.evaluate(cards, _terminal({_GH + '703': (tts.CLOSED, _AFTER)}))
        self.assertEqual([s for _, s, _ in actions], [tts.CLOSED])

    def test_open_is_left_alone(self):
        cards = [_card('a', 1)]
        actions = heal.evaluate(cards, _terminal({_GH + '1': (tts.OPEN, None)}))
        self.assertEqual(actions, [])

    def test_unknown_is_left_alone(self):
        cards = [_card('a', 1)]
        actions = heal.evaluate(cards, _terminal({_GH + '1': (tts.UNKNOWN, None)}))
        self.assertEqual(actions, [])

    def test_merged_before_request_is_left_alone(self):
        # Terminal ts predates the review_request -> not the out-of-band phantom;
        # a real verdict may still be coming. Never close.
        cards = [_card('a', 1, since=_AFTER)]
        actions = heal.evaluate(cards, _terminal({_GH + '1': (tts.MERGED, _BEFORE)}))
        self.assertEqual(actions, [])

    def test_merged_without_ts_is_left_alone(self):
        cards = [_card('a', 1)]
        actions = heal.evaluate(cards, _terminal({_GH + '1': (tts.MERGED, None)}))
        self.assertEqual(actions, [])

    def test_malformed_pr_url_is_skipped_without_probe(self):
        def _boom(url):
            raise AssertionError('must not probe a non-github url')
        cards = [{'task_id': 'a', 'pr_url': 'https://gh/o/r/pull/9', 'since': _REQ}]
        self.assertEqual(heal.evaluate(cards, _boom), [])

    def test_missing_since_is_skipped(self):
        cards = [{'task_id': 'a', 'pr_url': _GH + '1', 'since': None}]
        actions = heal.evaluate(cards, _terminal({_GH + '1': (tts.MERGED, _AFTER)}))
        self.assertEqual(actions, [])

    def test_raising_probe_does_not_abort_sweep(self):
        def _probe(url):
            if url.endswith('/1'):
                raise RuntimeError('gh exploded')
            return (tts.MERGED, _AFTER)
        cards = [_card('a', 1), _card('b', 2)]
        actions = heal.evaluate(cards, _probe)
        # The raising card is skipped; the healthy one still closes.
        self.assertEqual([c['task_id'] for c, _, _ in actions], ['b'])

    def test_probe_budget_caps_gh_calls(self):
        cards = [_card(str(i), 700 + i) for i in range(5)]
        calls = []

        def _probe(url):
            calls.append(url)
            return (tts.MERGED, _AFTER)
        actions = heal.evaluate(cards, _probe, max_probes=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(actions), 2)


class RunCycleTests(unittest.TestCase):
    def _wire(self, cards, terminal, emitted, apply=True):
        return heal.run_cycle(
            apply=apply,
            get_client=lambda: object(),
            fetch=lambda client, agent: [],          # rows/verdicts unused by fake derive
            derive=lambda rows, verdicts: cards,
            pr_terminal=terminal,
            emit=lambda **kw: emitted.append(kw) or True,
        )

    def test_emits_review_obsolete_with_terminal_ts(self):
        emitted = []
        n = self._wire([_card('a', 698)],
                       _terminal({_GH + '698': (tts.MERGED, _AFTER)}), emitted)
        self.assertEqual(n, 1)
        self.assertEqual(len(emitted), 1)
        kw = emitted[0]
        self.assertEqual(kw['event_type'], heal.CLOSE_EVENT_TYPE)
        self.assertEqual(kw['agent'], 'forge')
        self.assertEqual(kw['task_id'], 'a')
        self.assertEqual(kw['pr_url'], _GH + '698')
        # Idempotency hinges on a stable ts == the PR terminal timestamp.
        self.assertEqual(kw['ts'], '2026-06-25T07:30:53+00:00')

    def test_dry_run_emits_nothing(self):
        emitted = []
        n = self._wire([_card('a', 698)],
                       _terminal({_GH + '698': (tts.MERGED, _AFTER)}),
                       emitted, apply=False)
        self.assertEqual(n, 0)
        self.assertEqual(emitted, [])

    def test_open_card_emits_nothing(self):
        emitted = []
        n = self._wire([_card('a', 1)], _terminal({_GH + '1': (tts.OPEN, None)}), emitted)
        self.assertEqual((n, emitted), (0, []))

    def test_failed_fetch_skips_cycle(self):
        emitted = []
        n = heal.run_cycle(
            apply=True,
            get_client=lambda: object(),
            fetch=lambda client, agent: None,        # None = fetch failed
            derive=lambda rows, verdicts: (_ for _ in ()).throw(
                AssertionError('derive must not run on a failed fetch')),
            pr_terminal=_terminal({}),
            emit=lambda **kw: emitted.append(kw) or True,
        )
        self.assertEqual((n, emitted), (0, []))

    def test_no_client_skips_cycle(self):
        emitted = []
        n = heal.run_cycle(
            apply=True,
            get_client=lambda: None,
            fetch=lambda client, agent: [],
            derive=lambda rows, verdicts: [],
            emit=lambda **kw: emitted.append(kw) or True,
        )
        self.assertEqual((n, emitted), (0, []))


if __name__ == '__main__':
    unittest.main()
