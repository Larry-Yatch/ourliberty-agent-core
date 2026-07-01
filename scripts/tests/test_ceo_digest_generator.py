#!/usr/bin/env python3
"""Tests for ceo_digest_generator.py — N6 daily/weekly CEO digest.

Covers:
  - period_window: daily + weekly bounds computed in Larry-local tz.
  - activity buckets: auto-cleared classification, attention dedup, spend
    window-sum, decisions-waiting parse, commit-prefix stripping.
  - render_raw: jargon-free plain outcomes containing the key figures.
  - build_prompt: carries the jargon ban + the facts.
  - generate_ceo_voice: success + every failure path (timeout, non-zero,
    bad JSON, empty result) falls through to (None, None).
  - write_digest: emits a ceo_digest row with the expected payload shape.
  - run: end-to-end voice path AND fallback path.
  - ceo_digest is registered in chain_event_shipper.KNOWN_EVENT_TYPES.

Run:
    python3 -m unittest scripts.tests.test_ceo_digest_generator
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import ceo_digest_generator as cdg  # noqa: E402
import chain_event_shipper as ces  # noqa: E402
import larry_alerts  # noqa: E402

DENVER = ZoneInfo('America/Denver')


class _FakeQuery:
    def __init__(self, parent):
        self.parent = parent
        self._lo = 0
        self._hi = 10 ** 9
        self._is_upsert = False

    def select(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def lt(self, *a, **k):
        return self

    def range(self, lo, hi):
        self._lo, self._hi = lo, hi
        return self

    def upsert(self, rows, **k):
        self._is_upsert = True
        self.parent.upserted.append({'rows': rows, 'kwargs': k})
        return self

    def execute(self):
        if self._is_upsert:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=self.parent.events[self._lo:self._hi + 1])


class _FakeClient:
    """Supports both the chain_events read chain and the upsert chain."""

    def __init__(self, events=None):
        self.events = events or []
        self.upserted: list[dict] = []

    def table(self, name):
        self.table_name = name
        return _FakeQuery(self)


class PeriodWindowTest(unittest.TestCase):
    def test_daily_window_is_prior_local_day(self):
        # 07:00 MDT on a given day → window = the prior local day [00:00,00:00).
        now = datetime(2026, 6, 2, 13, 0, tzinfo=timezone.utc)  # 07:00 MDT
        start, end, label = cdg.period_window('daily', now, DENVER)
        self.assertEqual(end - start, timedelta(days=1))
        end_local = end.astimezone(DENVER)
        start_local = start.astimezone(DENVER)
        self.assertEqual((end_local.hour, end_local.minute), (0, 0))
        self.assertEqual(end_local.date(), now.astimezone(DENVER).date())
        self.assertEqual(start_local.date(), end_local.date() - timedelta(days=1))
        self.assertIn(start_local.strftime('%B'), label)

    def test_weekly_window_is_prior_local_week_ending_monday(self):
        now = datetime(2026, 6, 2, 13, 0, tzinfo=timezone.utc)
        start, end, label = cdg.period_window('weekly', now, DENVER)
        self.assertEqual(end - start, timedelta(days=7))
        end_local = end.astimezone(DENVER)
        self.assertEqual(end_local.weekday(), 0)  # Monday
        self.assertEqual((end_local.hour, end_local.minute), (0, 0))
        self.assertLessEqual(end_local, now.astimezone(DENVER))
        self.assertTrue(label)

    def test_invalid_period_raises(self):
        with self.assertRaises(ValueError):
            cdg.period_window('hourly', datetime.now(timezone.utc), DENVER)


class ActivityBucketTest(unittest.TestCase):
    def test_strip_commit_prefix(self):
        self.assertEqual(cdg._strip_commit_prefix('feat(scope): add thing'), 'add thing')
        self.assertEqual(cdg._strip_commit_prefix('fix: bug'), 'bug')
        self.assertEqual(cdg._strip_commit_prefix('plain title'), 'plain title')

    def test_count_auto_cleared(self):
        events = [
            {'event_type': 'clarify_response'},
            {'event_type': 'clarify_response'},
            {'event_type': 'larry_action', 'payload': {'actor': 'system'}},
            {'event_type': 'larry_action', 'payload': {'actor': 'larry'}},  # excluded
            {'event_type': 'session_start'},  # ignored
        ]
        self.assertEqual(cdg.count_auto_cleared(events), 3)

    def test_collect_attention_dedups(self):
        events = [
            {'event_type': 'escalation', 'payload': {'subject': 'disk full'}},
            {'event_type': 'escalation', 'payload': {'subject': 'disk full'}},
            {'event_type': 'larry_alert', 'payload': {'subject': 'token expiring'}},
            {'event_type': 'session_done', 'payload': {'subject': 'noise'}},
        ]
        self.assertEqual(cdg.collect_attention(events), ['disk full', 'token expiring'])

    def test_collect_attention_suppresses_merged_fix(self):
        # §3.6: an item whose fix PR has MERGED is suppressed, not re-surfaced.
        events = [
            {'event_type': 'escalation', 'task_id': 'watermark-fix-001',
             'payload': {'subject': 'check-0 watermark loss'}},
            {'event_type': 'larry_alert',
             'payload': {'subject': 'still-open thing', 'task_id': 'open-001'}},
        ]
        probe = lambda tid: cdg.tts.MERGED if tid == 'watermark-fix-001' else cdg.tts.OPEN
        self.assertEqual(
            cdg.collect_attention(events, probe_fn=probe), ['still-open thing'])

    def test_collect_attention_keeps_open_and_unknown(self):
        # OPEN / UNKNOWN probe ⇒ conservative keep (never hide an open problem).
        events = [
            {'event_type': 'escalation', 'task_id': 'a-001',
             'payload': {'subject': 'open problem'}},
            {'event_type': 'escalation', 'task_id': 'b-001',
             'payload': {'subject': 'indeterminate problem'}},
        ]
        def probe(tid):
            return {'a-001': cdg.tts.OPEN, 'b-001': cdg.tts.UNKNOWN}[tid]
        self.assertEqual(
            cdg.collect_attention(events, probe_fn=probe),
            ['open problem', 'indeterminate problem'])

    def test_collect_attention_closed_fix_not_suppressed(self):
        # A CLOSED (abandoned, not merged) fix PR ⇒ problem likely still open;
        # only MERGED suppresses.
        events = [
            {'event_type': 'escalation', 'task_id': 'c-001',
             'payload': {'subject': 'abandoned-fix problem'}},
        ]
        self.assertEqual(
            cdg.collect_attention(events, probe_fn=lambda tid: cdg.tts.CLOSED),
            ['abandoned-fix problem'])

    def test_collect_attention_no_task_id_not_probed(self):
        # No derivable fix task_id ⇒ conservative: never probe, never suppress.
        events = [
            {'event_type': 'escalation', 'payload': {'subject': 'no-id problem'}},
        ]
        calls = []
        def probe(tid):
            calls.append(tid)
            return cdg.tts.MERGED
        self.assertEqual(
            cdg.collect_attention(events, probe_fn=probe), ['no-id problem'])
        self.assertEqual(calls, [], 'must not probe when no task_id is derivable')

    def test_sum_spend_window_filter(self):
        with tempfile.TemporaryDirectory() as d:
            costs = Path(d) / 'costs.jsonl'
            costs.write_text('\n'.join([
                json.dumps({'ts': '2026-06-01T12:00:00+00:00', 'cost_usd': 1.50}),
                json.dumps({'ts': '2026-06-01T18:00:00+00:00', 'cost_usd': 2.25}),
                json.dumps({'ts': '2026-05-30T00:00:00+00:00', 'cost_usd': 9.99}),  # before
                json.dumps({'ts': 'garbage'}),  # skipped
            ]) + '\n')
            with mock.patch.object(cdg, 'COSTS_FILE', costs):
                total = cdg.sum_spend(
                    datetime(2026, 6, 1, tzinfo=timezone.utc),
                    datetime(2026, 6, 2, tzinfo=timezone.utc),
                )
        self.assertEqual(total, 3.75)

    def test_fetch_decisions_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            approvals = Path(d) / 'beacon-pending-approvals.json'
            approvals.write_text(json.dumps({'pending': [
                {'id': 'a1', 'plan_summary': 'ship the thing'},
                {'id': 'a2', 'summary': 'review the other thing'},
            ]}))
            with mock.patch.object(cdg, 'APPROVALS_FILE', approvals):
                waiting = cdg.fetch_decisions_waiting()
        self.assertEqual(waiting, ['ship the thing', 'review the other thing'])

    def test_fetch_events_in_window_paginates(self):
        client = _FakeClient(events=[{'event_type': 'clarify_response'}] * 3)
        rows = cdg.fetch_events_in_window(
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 2, tzinfo=timezone.utc),
            client=client,
        )
        self.assertEqual(len(rows), 3)


class RenderRawTest(unittest.TestCase):
    def _activity(self, **over):
        base = {
            'shipped': [{'number': 5, 'title': 'faster checkout'}],
            'shipped_count': 1,
            'auto_cleared_count': 2,
            'decisions_waiting': ['approve the budget'],
            'spend_usd': 4.20,
            'attention': ['token expiring'],
            'self_healed': [],
        }
        base.update(over)
        return base

    def test_raw_contains_figures_and_no_jargon(self):
        raw = cdg.render_raw('daily', 'Monday, June 1', self._activity())
        self.assertIn('shipped', raw)
        self.assertIn('$4.20', raw)
        self.assertIn('waiting on you', raw)
        self.assertIn('faster checkout', raw)
        lowered = raw.lower()
        for banned in ('pull request', ' pr ', 'healer', 'marker', 'merge', 'commit'):
            self.assertNotIn(banned, lowered, f'jargon leaked: {banned!r}')

    def test_raw_handles_empty(self):
        raw = cdg.render_raw('weekly', 'May 25–May 31', self._activity(
            shipped=[], shipped_count=0, auto_cleared_count=0,
            decisions_waiting=[], spend_usd=0.0, attention=[], self_healed=[],
        ))
        self.assertIn('Nothing new shipped', raw)
        self.assertIn('Nothing waiting on you', raw)
        self.assertIn('$0.00', raw)

    def test_raw_renders_self_healed_section(self):
        raw = cdg.render_raw('daily', 'Monday, June 1', self._activity(
            self_healed=[
                'A background service had stale code and restarted itself.',
                'The shared workspace had drifted and was re-synced.',
            ],
        ))
        self.assertIn('broke and got fixed automatically', raw)
        self.assertIn('no action needed', raw.lower())
        self.assertIn('restarted itself', raw)
        self.assertIn('re-synced', raw)
        # Outcome language only — no imperative leaking into the digest.
        lowered = raw.lower()
        for banned in ('healer', 'systemd', 'sudo ', 'run `'):
            self.assertNotIn(banned, lowered, f'jargon/imperative leaked: {banned!r}')

    def test_raw_omits_self_healed_section_when_empty(self):
        raw = cdg.render_raw('daily', 'Monday, June 1', self._activity(
            self_healed=[]))
        self.assertNotIn('broke and got fixed automatically', raw)

    def test_raw_truncates_long_self_healed_list(self):
        raw = cdg.render_raw('weekly', 'May 25–May 31', self._activity(
            self_healed=[f'self-heal {i}' for i in range(8)]))
        self.assertIn('(+3 more)', raw)


class CollectSelfHealedTest(unittest.TestCase):
    """collect_self_healed reads route=digest alerts in the window, dedups,
    and yields each producer's own outcome message — never a DM."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._alerts = Path(self._tmp.name) / 'larry-alerts.jsonl'
        self._patch = mock.patch.object(larry_alerts, 'ALERTS_FILE', self._alerts)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, records):
        self._alerts.write_text(
            '\n'.join(json.dumps(r) for r in records) + '\n')

    def test_collects_only_digest_messages_in_window(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        end = datetime(2026, 6, 2, tzinfo=timezone.utc)
        self._write([
            {'ts': '2026-06-01T08:00:00+00:00', 'route': 'digest',
             'message': 'service A self-healed'},
            {'ts': '2026-06-01T09:00:00+00:00', 'route': 'closure',
             'message': 'significant heal — should be DM not digest'},
            {'ts': '2026-06-01T10:00:00+00:00', 'route': 'escalate',
             'message': 'failed heal — should escalate'},
            {'ts': '2026-05-30T10:00:00+00:00', 'route': 'digest',
             'message': 'before the window'},
        ])
        out = cdg.collect_self_healed(start, end)
        self.assertEqual(out, ['service A self-healed'])

    def test_dedups_repeated_messages(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        end = datetime(2026, 6, 2, tzinfo=timezone.utc)
        self._write([
            {'ts': '2026-06-01T08:00:00+00:00', 'route': 'digest',
             'message': 'same message'},
            {'ts': '2026-06-01T09:00:00+00:00', 'route': 'digest',
             'message': 'same message'},
        ])
        self.assertEqual(cdg.collect_self_healed(start, end), ['same message'])

    def test_missing_file_yields_empty(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        end = datetime(2026, 6, 2, tzinfo=timezone.utc)
        self.assertEqual(cdg.collect_self_healed(start, end), [])


class PromptTest(unittest.TestCase):
    def test_prompt_bans_jargon_and_includes_facts(self):
        activity = {
            'shipped': [{'title': 'thing'}], 'shipped_count': 1,
            'auto_cleared_count': 0, 'decisions_waiting': [],
            'spend_usd': 1.0, 'attention': [],
            'self_healed': ['a background service fixed itself'],
        }
        prompt = cdg.build_prompt('daily', 'Monday, June 1', activity)
        self.assertIn('CEO', prompt)
        self.assertIn('NEVER use', prompt)
        self.assertIn('"healer"', prompt)
        self.assertIn('amount_spent_usd', prompt)
        self.assertIn('things_that_broke_and_self_fixed', prompt)
        self.assertIn('a background service fixed itself', prompt)


class GenerateCeoVoiceTest(unittest.TestCase):
    def setUp(self):
        # W4: generate_ceo_voice now selects a tier via select_durable_claude_env
        # before spawning; provide a usable (env, tier) so these tests exercise
        # the subprocess path rather than short-circuiting on "no tier".
        p = mock.patch.object(cdg.active_tier, 'select_durable_claude_env',
                              return_value=({}, 'tier1'))
        p.start()
        self.addCleanup(p.stop)

    def test_success_returns_text_and_cost(self):
        out = json.dumps({'result': 'We shipped a faster checkout.', 'total_cost_usd': 0.03})
        with mock.patch.object(cdg.subprocess, 'run',
                               return_value=SimpleNamespace(returncode=0, stdout=out, stderr='')):
            text, cost = cdg.generate_ceo_voice('prompt')
        self.assertEqual(text, 'We shipped a faster checkout.')
        self.assertEqual(cost, 0.03)

    def test_no_tier_skips_run(self):
        # W4 None-safe: no dispatch tier -> skip the spawn, fall through to raw.
        with mock.patch.object(cdg.active_tier, 'select_durable_claude_env',
                               return_value=(None, None)), \
             mock.patch.object(cdg.subprocess, 'run') as run:
            result = cdg.generate_ceo_voice('p')
        run.assert_not_called()
        self.assertEqual(result, (None, None))

    def test_cost_attributed_to_selected_tier(self):
        # §6: the cost row's account is the tier THIS run selected, not the
        # global active tier.
        out = json.dumps({'result': 'x', 'total_cost_usd': 0.01})
        with mock.patch.object(cdg.active_tier, 'select_durable_claude_env',
                               return_value=({}, 'tier3')), \
             mock.patch.object(cdg.subprocess, 'run',
                               return_value=SimpleNamespace(returncode=0, stdout=out, stderr='')):
            cdg.generate_ceo_voice('p')
        self.assertEqual(cdg._account_tier(), 'tier3')

    def test_nonzero_exit_falls_through(self):
        with mock.patch.object(cdg.subprocess, 'run',
                               return_value=SimpleNamespace(returncode=1, stdout='', stderr='err')):
            self.assertEqual(cdg.generate_ceo_voice('p'), (None, None))

    def test_timeout_falls_through(self):
        with mock.patch.object(cdg.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('claude', 1)):
            self.assertEqual(cdg.generate_ceo_voice('p'), (None, None))

    def test_bad_json_falls_through(self):
        with mock.patch.object(cdg.subprocess, 'run',
                               return_value=SimpleNamespace(returncode=0, stdout='not json', stderr='')):
            self.assertEqual(cdg.generate_ceo_voice('p'), (None, None))

    def test_empty_result_falls_through(self):
        out = json.dumps({'result': '   ', 'total_cost_usd': 0.01})
        with mock.patch.object(cdg.subprocess, 'run',
                               return_value=SimpleNamespace(returncode=0, stdout=out, stderr='')):
            self.assertEqual(cdg.generate_ceo_voice('p'), (None, None))


class WriteDigestTest(unittest.TestCase):
    def test_emits_ceo_digest_row(self):
        client = _FakeClient()
        activity = {
            'shipped': [], 'shipped_count': 0, 'auto_cleared_count': 1,
            'decisions_waiting': ['x'], 'spend_usd': 2.5, 'attention': [],
        }
        ok = cdg.write_digest(
            'daily', 'Monday, June 1',
            datetime(2026, 6, 1, 6, tzinfo=timezone.utc),
            datetime(2026, 6, 2, 6, tzinfo=timezone.utc),
            activity, 'CEO note here', used_fallback=False, client=client,
        )
        self.assertTrue(ok)
        self.assertEqual(len(client.upserted), 1)
        row = client.upserted[0]['rows'][0]
        self.assertEqual(row['event_type'], 'ceo_digest')
        self.assertEqual(row['agent'], 'ceo-digest')
        self.assertEqual(row['payload']['period'], 'daily')
        self.assertEqual(row['payload']['summary'], 'CEO note here')
        self.assertFalse(row['payload']['used_fallback'])
        self.assertIn('raw', row['payload'])
        self.assertEqual(row['payload']['metrics']['spend_usd'], 2.5)
        # The window's reported spend rollup belongs in payload.metrics only.
        # cost_usd must stay null so SUM(cost_usd) consumers (e.g. the
        # dashboard day-over-day spend panel) don't double-count it as
        # newly-incurred spend.
        self.assertIsNone(row.get('cost_usd'))


class RunEndToEndTest(unittest.TestCase):
    def _patch_sources(self, stack, *, costs=None, approvals_missing=True):
        stack.enter_context(mock.patch.object(
            cdg, 'fetch_shipped',
            return_value=[{'number': 7, 'title': 'faster checkout'}]))
        if approvals_missing:
            stack.enter_context(mock.patch.object(
                cdg, 'APPROVALS_FILE', Path('/nonexistent/approvals.json')))
        stack.enter_context(mock.patch.object(
            cdg, 'COSTS_FILE', costs or Path('/nonexistent/costs.jsonl')))
        stack.enter_context(mock.patch.object(
            cdg, 'ACTIVE_TIER_FILE', Path('/nonexistent/tier.json')))
        # Isolate the self-healed digest read from the live alerts queue.
        stack.enter_context(mock.patch.object(
            larry_alerts, 'ALERTS_FILE', Path('/nonexistent/larry-alerts.jsonl')))
        # run() calls heartbeat(), which writes the import-frozen
        # HEARTBEAT_FILE — un-patched, a bare un-sandboxed run freshens the
        # REAL ceo-digest-generator.heartbeat and masks genuine staleness
        # from the liveness watchdog. A writable tmp path (not /nonexistent)
        # on purpose: heartbeat() swallows OSError, so only an observable
        # write lets tests assert the liveness signal still fires.
        hb_dir = stack.enter_context(tempfile.TemporaryDirectory())
        self._hb_file = Path(hb_dir) / 'hb'
        stack.enter_context(mock.patch.object(
            cdg, 'HEARTBEAT_FILE', self._hb_file))

    def test_voice_path(self):
        client = _FakeClient(events=[{'event_type': 'clarify_response'}])
        with tempfile.TemporaryDirectory() as d:
            costs = Path(d) / 'costs.jsonl'
            costs.write_text('')
            with ExitStack() as stack:
                self._patch_sources(stack, costs=costs)
                stack.enter_context(mock.patch.object(
                    cdg, 'generate_ceo_voice', return_value=('CEO voice note', 0.02)))
                rc = cdg.run('daily',
                             now_utc=datetime(2026, 6, 2, 13, tzinfo=timezone.utc),
                             client=client)
                # The liveness heartbeat must actually land — heartbeat()
                # swallows OSError, so a write regression is otherwise
                # invisible until the production watchdog pages. (Inside the
                # stack: the tmp dir holding it is removed on exit.)
                self.assertTrue(self._hb_file.exists())
            self.assertEqual(rc, 0)
            row = client.upserted[0]['rows'][0]
            self.assertEqual(row['payload']['summary'], 'CEO voice note')
            self.assertFalse(row['payload']['used_fallback'])
            # Voice path records its own LLM spend.
            cost_lines = [l for l in costs.read_text().splitlines() if l.strip()]
            self.assertEqual(len(cost_lines), 1)
            self.assertEqual(json.loads(cost_lines[0])['agent'], 'ceo-digest')

    def test_fallback_path(self):
        client = _FakeClient(events=[])
        with ExitStack() as stack:
            self._patch_sources(stack)
            stack.enter_context(mock.patch.object(
                cdg, 'generate_ceo_voice', return_value=(None, None)))
            rc = cdg.run('daily',
                         now_utc=datetime(2026, 6, 2, 13, tzinfo=timezone.utc),
                         client=client)
        self.assertEqual(rc, 0)
        row = client.upserted[0]['rows'][0]
        self.assertTrue(row['payload']['used_fallback'])
        # Fallback summary equals the raw rendering.
        self.assertEqual(row['payload']['summary'], row['payload']['raw'])


class RegistrationTest(unittest.TestCase):
    def test_ceo_digest_in_known_event_types(self):
        self.assertIn('ceo_digest', ces.KNOWN_EVENT_TYPES)


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
