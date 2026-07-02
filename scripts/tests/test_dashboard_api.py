#!/usr/bin/env python3
"""Tests for dashboard_api (E3.1).

Covers all 7 endpoints, auth, CORS, and the per-endpoint reader functions
end-to-end against synthetic AGENTS_ROOT trees. Path-isolation pattern
mirrors test_deploy_notifier.py: OURLIBERTY_AGENTS_ROOT is monkeypatched
to a tmpdir BEFORE importing the module so AGENTS_ROOT resolves into the
tmpdir for the lifetime of the test process.

Dependencies installed at the same step as the service itself
(`pip3 install --user fastapi 'uvicorn[standard]' httpx` — see
systemd/INSTALL.md "Dashboard API (E3.1)" subsection). httpx ships
transitively with the fastapi TestClient.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_dashboard_api
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Module-level tmpdir reserved for any test that wants a stable scratch
# space across multiple TestClient instances. Per-test setUps create
# their own subdirs.
_TMP_BASE = tempfile.TemporaryDirectory(prefix='dashboard-api-test-')


# We deliberately do NOT mutate OURLIBERTY_AGENTS_ROOT — sibling test
# modules (test_deploy_notifier) capture it at their import time and
# assert it stays stable. Each TestClient monkeypatches
# `da._agents_root` directly instead. DASHBOARD_API_TOKEN is set
# because FastAPI reads it at request time and there's no second test
# module that touches it.
_ORIGINAL_TOKEN = os.environ.get('DASHBOARD_API_TOKEN')
os.environ['DASHBOARD_API_TOKEN'] = 'test-token-value'

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = 'test-token-value'
AUTH = {'X-Dashboard-Token': TOKEN}


def tearDownModule():  # noqa: N802 — unittest API
    if _ORIGINAL_TOKEN is None:
        os.environ.pop('DASHBOARD_API_TOKEN', None)
    else:
        os.environ['DASHBOARD_API_TOKEN'] = _ORIGINAL_TOKEN


# ---------- fixture helpers ----------

def _fresh_root(tmp: Path) -> Path:
    """Build the canonical agents-root subdirs under `tmp`."""
    for sub in (
        'blackboard', 'logs', 'state',
        'inboxes/beacon', 'inboxes/forge', 'inboxes/mirror', 'inboxes/pulse',
        'inboxes/beacon/.archive', 'inboxes/forge/.archive',
        'inboxes/mirror/.archive', 'inboxes/pulse/.archive',
        'outboxes/beacon', 'outboxes/forge', 'outboxes/mirror', 'outboxes/pulse',
        'outboxes/beacon/.archive', 'outboxes/forge/.archive',
        'outboxes/mirror/.archive', 'outboxes/pulse/.archive',
    ):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    return tmp


def _write_costs(tmp: Path, rows: list[dict]) -> None:
    p = tmp / 'blackboard' / 'costs.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')


def _client(tmp: Path, testcase: unittest.TestCase) -> TestClient:
    """Build a TestClient with AGENTS_ROOT pointed at `tmp`.

    We monkeypatch `da._agents_root` rather than mutating the global env,
    because sibling test modules (e.g. test_deploy_notifier) capture
    `OURLIBERTY_AGENTS_ROOT` at their import time and assert it stays
    stable. Each TestClient gets its own AGENTS_ROOT closure-bound to the
    tmpdir the test is using.

    The rebind is scoped to the calling test: `mock.patch.object` restores
    the original `da._agents_root` at teardown via `addCleanup`, so no
    dashboard test leaks a tmpdir-bound `_agents_root` into sibling test
    modules run in the same process (e.g. test_heal_orphan_autoregister's
    queue-dir parity check).
    """
    p = mock.patch.object(da, '_agents_root', lambda: tmp)
    p.start()
    testcase.addCleanup(p.stop)
    return TestClient(da.app)


# ==================== auth ====================

class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-auth-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_health_missing_token_401(self):
        r = self.c.get('/health')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {'detail': 'missing X-Dashboard-Token'})

    def test_health_wrong_token_401(self):
        r = self.c.get('/health', headers={'X-Dashboard-Token': 'nope'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {'detail': 'invalid X-Dashboard-Token'})

    def test_health_correct_token_200(self):
        r = self.c.get('/health', headers=AUTH)
        self.assertEqual(r.status_code, 200)

    def test_every_endpoint_requires_token(self):
        for path in (
            '/health', '/agents/status', '/tasks/recent',
            '/costs/today', '/costs/week',
            '/cycle-journal/recent', '/healers/status',
            '/docs', '/openapi.json',
        ):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 401, msg=f'no-auth {path}')
            r = self.c.get(path, headers={'X-Dashboard-Token': 'wrong'})
            self.assertEqual(r.status_code, 401, msg=f'wrong-auth {path}')

    def test_auth_uses_constant_time_compare(self):
        # Source-level check: secrets.compare_digest is referenced.
        src = Path(da.__file__).read_text()
        self.assertIn('secrets.compare_digest', src)

    def test_server_misconfigured_no_token_set_returns_401(self):
        # An empty DASHBOARD_API_TOKEN env should refuse to claim auth passed.
        prev = os.environ.pop('DASHBOARD_API_TOKEN', None)
        try:
            r = self.c.get('/health', headers={'X-Dashboard-Token': 'whatever'})
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json(), {'detail': 'invalid X-Dashboard-Token'})
        finally:
            if prev is not None:
                os.environ['DASHBOARD_API_TOKEN'] = prev


# ==================== CORS ====================

class CorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-cors-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_cors_preflight_allowed_origin(self):
        r = self.c.options('/agents/status', headers={
            'Origin': 'https://dashboard.ourliberty.dev',
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'X-Dashboard-Token',
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.headers.get('access-control-allow-origin'),
            'https://dashboard.ourliberty.dev',
        )

    def test_cors_preflight_disallowed_origin_no_headers(self):
        r = self.c.options('/agents/status', headers={
            'Origin': 'https://evil.example.com',
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'X-Dashboard-Token',
        })
        # FastAPI's CORSMiddleware returns 400 on disallowed-origin preflight
        # and crucially does NOT set the allow-origin header.
        self.assertNotIn('access-control-allow-origin', {k.lower() for k in r.headers.keys()})

    def test_cors_simple_get_allowed_origin(self):
        r = self.c.get('/health', headers={
            **AUTH,
            'Origin': 'https://dashboard.ourliberty.dev',
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.headers.get('access-control-allow-origin'),
            'https://dashboard.ourliberty.dev',
        )


# ==================== /health ====================

class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-h-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_shape(self):
        r = self.c.get('/health', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['agents_root'], str(self.tmp))
        self.assertIn('version', body)
        self.assertIn('timestamp', body)

    def test_no_filesystem_reads_under_real_root(self):
        # Sanity: /health must not write to /home/larry/agents/...
        prod_log = Path('/home/larry/agents/logs/dashboard-api.log')
        before = prod_log.exists()
        self.c.get('/health', headers=AUTH)
        after = prod_log.exists()
        self.assertEqual(before, after, 'health test polluted prod path')

    def test_perf_under_100ms(self):
        import time
        t0 = time.perf_counter()
        r = self.c.get('/health', headers=AUTH)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(r.status_code, 200)
        self.assertLess(elapsed_ms, 100,
                        msg=f'/health took {elapsed_ms:.1f}ms')


# ==================== /agents/status ====================

class AgentsStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-a-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_empty_root_returns_four_agents(self):
        with mock.patch.object(da, '_systemctl_is_active', return_value=None):
            r = self.c.get('/agents/status', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        names = [a['name'] for a in r.json()['agents']]
        self.assertEqual(names, ['beacon', 'forge', 'mirror', 'pulse'])
        for a in r.json()['agents']:
            self.assertEqual(a['in_flight_count'], 0)
            self.assertEqual(a['in_flight_task_ids'], [])

    def test_in_flight_counts(self):
        (self.tmp / 'inboxes/forge/task-29.json').write_text('{}')
        (self.tmp / 'inboxes/forge/task-30.json').write_text('{}')
        # marker-error-* is a real dispatch type and a non-prefixed id is the
        # common case (safe_write_inbox writes caller-supplied filenames), so
        # both must count, matching inbox_watcher.scan_inbox.
        (self.tmp / 'inboxes/forge/marker-error-7.json').write_text('{}')
        (self.tmp / 'inboxes/forge/build-install-drift-emission-fix-001.json').write_text('{}')
        # Dotfiles (e.g. partial writes) must not count.
        (self.tmp / 'inboxes/forge/.partial.json').write_text('{}')
        with mock.patch.object(da, '_systemctl_is_active', return_value=True):
            r = self.c.get('/agents/status', headers=AUTH)
        forge = next(a for a in r.json()['agents'] if a['name'] == 'forge')
        self.assertEqual(forge['in_flight_count'], 4)
        self.assertEqual(
            forge['in_flight_task_ids'],
            [
                'build-install-drift-emission-fix-001',
                'marker-error-7',
                'task-29',
                'task-30',
            ],
        )

    def test_bot_model_disambiguates_mirror_pulse(self):
        with mock.patch.object(da, '_systemctl_is_active', return_value=True):
            r = self.c.get('/agents/status', headers=AUTH)
        by_name = {a['name']: a for a in r.json()['agents']}
        self.assertEqual(by_name['beacon']['bot_model'], 'systemd-bot')
        self.assertEqual(by_name['forge']['bot_model'], 'systemd-bot')
        self.assertEqual(by_name['mirror']['bot_model'], 'inbox-watcher')
        self.assertEqual(by_name['pulse']['bot_model'], 'inbox-watcher')
        self.assertIsNone(by_name['mirror']['bot_active'])
        self.assertIsNone(by_name['pulse']['bot_active'])

    def test_last_activity_uses_max_mtime_across_archives(self):
        outbox = self.tmp / 'outboxes/forge/.archive/task-1.json'
        inbox = self.tmp / 'inboxes/forge/.archive/task-1.json'
        outbox.write_text('{}')
        inbox.write_text('{}')
        # Make inbox newer.
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
        new = datetime.now(timezone.utc).timestamp()
        os.utime(outbox, (old, old))
        os.utime(inbox, (new, new))
        with mock.patch.object(da, '_systemctl_is_active', return_value=True):
            r = self.c.get('/agents/status', headers=AUTH)
        forge = next(a for a in r.json()['agents'] if a['name'] == 'forge')
        self.assertIsNotNone(forge['last_activity_at'])
        self.assertIsNotNone(forge['last_outbox_archive_at'])
        # last_activity should be > last_outbox (because inbox is newer).
        self.assertGreater(forge['last_activity_at'], forge['last_outbox_archive_at'])


# ==================== /tasks/recent ====================

class TasksRecentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-t-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_empty_costs_returns_empty(self):
        r = self.c.get('/tasks/recent', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['tasks'], [])
        self.assertEqual(body['returned'], 0)
        self.assertEqual(body['limit'], 20)

    def test_orders_most_recent_first(self):
        rows = [
            {'ts': '2026-05-20T00:00:00+00:00', 'agent': 'forge', 'task_id': 'task-old', 'cost_usd': 0.1, 'duration_sec': 5},
            {'ts': '2026-05-20T05:00:00+00:00', 'agent': 'forge', 'task_id': 'task-new', 'cost_usd': 0.2, 'duration_sec': 5},
        ]
        _write_costs(self.tmp, rows)
        r = self.c.get('/tasks/recent', headers=AUTH)
        ids = [t['task_id'] for t in r.json()['tasks']]
        self.assertEqual(ids[0], 'task-new')

    def test_limit_cap_at_100(self):
        r = self.c.get('/tasks/recent?limit=101', headers=AUTH)
        self.assertEqual(r.status_code, 422)

    def test_limit_below_1_rejected(self):
        r = self.c.get('/tasks/recent?limit=0', headers=AUTH)
        self.assertEqual(r.status_code, 422)

    def test_in_flight_detection(self):
        (self.tmp / 'inboxes/forge/task-42.json').write_text('{}')
        # No matching outbox archive, no costs row → outcome=in_flight.
        r = self.c.get('/tasks/recent', headers=AUTH)
        rows = r.json()['tasks']
        in_flight = [t for t in rows if t['task_id'] == 'task-42']
        self.assertEqual(len(in_flight), 1)
        self.assertEqual(in_flight[0]['outcome'], 'in_flight')

    def test_in_flight_detection_non_prefixed_task_id(self):
        # Real dispatched inbox files have no task- prefix.
        (self.tmp / 'inboxes/forge/build-install-drift-emission-fix-001.json').write_text('{}')
        r = self.c.get('/tasks/recent', headers=AUTH)
        rows = r.json()['tasks']
        in_flight = [t for t in rows if t['task_id'] == 'build-install-drift-emission-fix-001']
        self.assertEqual(len(in_flight), 1)
        self.assertEqual(in_flight[0]['outcome'], 'in_flight')

    def test_archived_outbox_classifies_review_pass(self):
        archive_path = self.tmp / 'outboxes/mirror/.archive/task-99.json'
        archive_path.write_text(json.dumps({
            'intent': 'review-pass',
            'pr_url': 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/99',
            'summary': 'Mirror approved PR #99',
        }))
        _write_costs(self.tmp, [{
            'ts': '2026-05-20T01:00:00+00:00', 'agent': 'forge',
            'task_id': 'task-99', 'cost_usd': 0.5, 'duration_sec': 10,
        }])
        r = self.c.get('/tasks/recent', headers=AUTH)
        rows = r.json()['tasks']
        match = [t for t in rows if t['task_id'] == 'task-99'][0]
        self.assertEqual(match['outcome'], 'review_pass')
        self.assertEqual(
            match['pr_url'],
            'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/99',
        )
        self.assertEqual(match['cost_usd'], 0.5)
        self.assertAlmostEqual(match['duration_seconds'], 10.0)

    def test_multi_row_cost_aggregation(self):
        rows = [
            {'ts': '2026-05-20T01:00:00+00:00', 'agent': 'forge', 'task_id': 'task-multi', 'cost_usd': 0.5, 'duration_sec': 2},
            {'ts': '2026-05-20T01:05:00+00:00', 'agent': 'forge', 'task_id': 'task-multi', 'cost_usd': 0.5, 'duration_sec': 3},
        ]
        _write_costs(self.tmp, rows)
        r = self.c.get('/tasks/recent', headers=AUTH)
        match = [t for t in r.json()['tasks'] if t['task_id'] == 'task-multi'][0]
        self.assertAlmostEqual(match['cost_usd'], 1.0)
        self.assertAlmostEqual(match['duration_seconds'], 5.0)


# ==================== /costs/today ====================

class CostsTodayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-ct-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_empty_costs(self):
        r = self.c.get('/costs/today', headers=AUTH)
        body = r.json()
        self.assertEqual(body['total_usd'], 0.0)
        self.assertEqual(body['by_agent'], {})
        self.assertEqual(body['task_count'], 0)

    def test_filters_to_today_utc(self):
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        rows = [
            {'ts': now.isoformat(), 'agent': 'forge', 'task_id': 't1', 'cost_usd': 1.0},
            {'ts': yesterday.isoformat(), 'agent': 'forge', 'task_id': 't0', 'cost_usd': 9.0},
        ]
        _write_costs(self.tmp, rows)
        r = self.c.get('/costs/today', headers=AUTH)
        body = r.json()
        self.assertAlmostEqual(body['total_usd'], 1.0)
        self.assertEqual(body['by_agent'], {'forge': 1.0})
        self.assertEqual(body['task_count'], 1)

    def test_utc_day_boundary_inclusive_end(self):
        # An entry at 23:59:59Z today must count toward today, not tomorrow.
        now = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=0)
        rows = [{'ts': now.isoformat(), 'agent': 'pulse', 'task_id': 'late', 'cost_usd': 0.5}]
        _write_costs(self.tmp, rows)
        r = self.c.get('/costs/today', headers=AUTH)
        self.assertAlmostEqual(r.json()['total_usd'], 0.5)


# ==================== /costs/week ====================

class CostsWeekTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-cw-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_returns_7_days(self):
        r = self.c.get('/costs/week', headers=AUTH)
        body = r.json()
        self.assertEqual(len(body['by_day']), 7)

    def test_window_inclusive(self):
        now = datetime.now(timezone.utc)
        six_ago = now - timedelta(days=6)
        seven_ago = now - timedelta(days=7)
        rows = [
            {'ts': now.isoformat(), 'agent': 'forge', 'task_id': 'today', 'cost_usd': 1.0},
            {'ts': six_ago.isoformat(), 'agent': 'beacon', 'task_id': 'edge_in', 'cost_usd': 2.0},
            {'ts': seven_ago.isoformat(), 'agent': 'mirror', 'task_id': 'edge_out', 'cost_usd': 99.0},
        ]
        _write_costs(self.tmp, rows)
        r = self.c.get('/costs/week', headers=AUTH)
        body = r.json()
        self.assertAlmostEqual(body['total_usd'], 3.0)
        # mirror shouldn't appear at all (entry fell outside window).
        self.assertNotIn('mirror', body['by_agent'])

    def test_by_day_buckets_aggregate_correctly(self):
        now = datetime.now(timezone.utc)
        rows = [
            {'ts': now.isoformat(), 'agent': 'forge', 'task_id': 'a', 'cost_usd': 1.0},
            {'ts': now.isoformat(), 'agent': 'forge', 'task_id': 'b', 'cost_usd': 2.0},
        ]
        _write_costs(self.tmp, rows)
        r = self.c.get('/costs/week', headers=AUTH)
        body = r.json()
        today_bucket = [d for d in body['by_day'] if d['date_utc'] == now.date().isoformat()][0]
        self.assertAlmostEqual(today_bucket['total_usd'], 3.0)
        self.assertEqual(today_bucket['task_count'], 2)


# ==================== /cycle-journal/recent ====================

class CycleJournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-cj-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def _patch_journal(self, text: str):
        path = self.tmp / 'cycle-journal.md'
        path.write_text(text)
        # Redirect reader to our tmp file by monkeypatching _repo_root.
        return mock.patch.object(da, '_repo_root', return_value=self.tmp / '_repo_subdir')

    def test_missing_journal_returns_empty_with_warning(self):
        # Point _repo_root to a path with no runbooks/cycle-journal.md.
        with mock.patch.object(da, '_repo_root', return_value=self.tmp):
            r = self.c.get('/cycle-journal/recent', headers=AUTH)
        body = r.json()
        self.assertEqual(body['entries'], [])
        self.assertGreater(len(body['parse_warnings']), 0)

    def test_parses_iteration_header(self):
        rr = self.tmp / 'runbooks'
        rr.mkdir(exist_ok=True)
        (rr / 'cycle-journal.md').write_text(
            '## Iteration 58 — 2026-05-21 00:41 UTC (interactive)\n\n'
            '**Health:** OK\n\n'
            '- **(A) Repo discipline:** nominal\n'
            '- **(B) Sync health:** nominal\n'
        )
        with mock.patch.object(da, '_repo_root', return_value=self.tmp):
            r = self.c.get('/cycle-journal/recent?n=5', headers=AUTH)
        body = r.json()
        self.assertGreater(len(body['entries']), 0)
        entry = body['entries'][0]
        self.assertEqual(entry['started_at'], '2026-05-21T00:41:00+00:00')
        self.assertEqual(entry['findings_count'], 2)

    def test_n_cap_at_50(self):
        r = self.c.get('/cycle-journal/recent?n=51', headers=AUTH)
        self.assertEqual(r.status_code, 422)

    def test_body_truncation_at_4kb(self):
        rr = self.tmp / 'runbooks'
        rr.mkdir(exist_ok=True)
        big = 'x' * 8192
        (rr / 'cycle-journal.md').write_text(
            '## Iteration 1 — 2026-05-20 10:00 UTC\n' + big + '\n'
        )
        with mock.patch.object(da, '_repo_root', return_value=self.tmp):
            r = self.c.get('/cycle-journal/recent?n=5', headers=AUTH)
        body_md = r.json()['entries'][0]['body_markdown']
        # Allow truncation marker overhead but ensure we capped near 4KB.
        self.assertLess(len(body_md.encode('utf-8')),
                        da.JOURNAL_BODY_CAP_BYTES + 64)
        self.assertIn('[truncated]', body_md)


# ==================== /healers/status ====================

class HealersStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-he-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_empty_blackboard_returns_empty(self):
        r = self.c.get('/healers/status', headers=AUTH)
        body = r.json()
        self.assertEqual(body['healers'], [])

    def test_lists_heartbeat_files(self):
        (self.tmp / 'blackboard/deploy-notifier.heartbeat').write_text('x')
        (self.tmp / 'blackboard/heal-pr-auto-merge.heartbeat').write_text('x')
        with mock.patch.object(da, '_list_timer_next', return_value=None):
            r = self.c.get('/healers/status', headers=AUTH)
        names = [h['name'] for h in r.json()['healers']]
        self.assertIn('deploy-notifier', names)
        self.assertIn('heal-pr-auto-merge', names)

    def test_stale_classification(self):
        hb = self.tmp / 'blackboard/heal-pr-auto-merge.heartbeat'
        hb.write_text('x')
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
        os.utime(hb, (old, old))
        with mock.patch.object(da, '_list_timer_next', return_value=None):
            r = self.c.get('/healers/status', headers=AUTH)
        healer = next(h for h in r.json()['healers'] if h['name'] == 'heal-pr-auto-merge')
        self.assertEqual(healer['last_result'], 'stale')

    def test_log_classification_error(self):
        (self.tmp / 'blackboard/deploy-notifier.heartbeat').write_text('x')
        (self.tmp / 'logs/deploy-notifier.log').write_text(
            '[2026-05-20T01:00:00+00:00] [INFO] tick: ok\n'
            '[2026-05-20T02:00:00+00:00] [ERROR] auth failed\n'
        )
        with mock.patch.object(da, '_list_timer_next', return_value=None):
            r = self.c.get('/healers/status', headers=AUTH)
        h = next(h for h in r.json()['healers'] if h['name'] == 'deploy-notifier')
        self.assertEqual(h['last_result'], 'error')

    def test_kill_switch_active(self):
        (self.tmp / 'healers.disabled').write_text('')
        (self.tmp / 'blackboard/deploy-notifier.heartbeat').write_text('x')
        with mock.patch.object(da, '_list_timer_next', return_value=None):
            r = self.c.get('/healers/status', headers=AUTH)
        h = r.json()['healers'][0]
        self.assertTrue(h['kill_switch_active'])


# ==================== path isolation ====================

class PathIsolationTest(unittest.TestCase):
    """Belt-and-suspenders check: nothing under /home/larry/agents is
    touched by the entire test module run."""

    def test_no_writes_to_prod_logs(self):
        prod = Path('/home/larry/agents/logs/dashboard-api.log')
        # If module init or any earlier test polluted this, fail loudly.
        if not prod.exists():
            return  # nothing to compare; fine.
        before_mtime = prod.stat().st_mtime
        tmp = Path(tempfile.mkdtemp(prefix='dash-iso-'))
        _fresh_root(tmp)
        c = _client(tmp, self)
        for path in ('/health', '/agents/status', '/tasks/recent', '/costs/today'):
            with mock.patch.object(da, '_systemctl_is_active', return_value=None), \
                 mock.patch.object(da, '_list_timer_next', return_value=None):
                c.get(path, headers=AUTH)
        self.assertEqual(prod.stat().st_mtime, before_mtime)


# ==================== docs/openapi auth-gating ====================

class DocsGatingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-d-'))
        _fresh_root(self.tmp)
        self.c = _client(self.tmp, self)

    def test_docs_requires_auth(self):
        r = self.c.get('/docs')
        self.assertEqual(r.status_code, 401)

    def test_docs_with_auth_serves_swagger(self):
        r = self.c.get('/docs', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertIn('swagger', r.text.lower())

    def test_openapi_requires_auth(self):
        r = self.c.get('/openapi.json')
        self.assertEqual(r.status_code, 401)

    def test_openapi_with_auth(self):
        r = self.c.get('/openapi.json', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        spec = r.json()
        self.assertIn('paths', spec)
        # All 7 documented endpoints present in the spec.
        for path in (
            '/health', '/agents/status', '/tasks/recent',
            '/costs/today', '/costs/week',
            '/cycle-journal/recent', '/healers/status',
        ):
            self.assertIn(path, spec['paths'], msg=f'missing {path} in OpenAPI spec')


# ==================== pure-reader unit tests ====================

class ReaderUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-r-'))
        _fresh_root(self.tmp)

    def test_reader_health(self):
        out = da._reader_health(self.tmp)
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(out['agents_root'], str(self.tmp))

    def test_reader_agents_status_pure(self):
        out = da._reader_agents_status(
            self.tmp, is_active_fn=lambda u: True,
        )
        self.assertEqual(len(out['agents']), 4)

    def test_reader_costs_today_pure(self):
        _write_costs(self.tmp, [{
            'ts': datetime.now(timezone.utc).isoformat(),
            'agent': 'forge', 'task_id': 't1', 'cost_usd': 0.25,
        }])
        out = da._reader_costs_today(self.tmp)
        self.assertAlmostEqual(out['total_usd'], 0.25)

    def test_outcome_from_outbox_marker(self):
        self.assertEqual(da._outcome_from_outbox({'result_marker': 'REVIEW_PASS'}), 'review_pass')
        self.assertEqual(da._outcome_from_outbox({'result_marker': 'REVIEW_REVISION'}), 'review_revision')
        self.assertEqual(da._outcome_from_outbox({'intent': 'review-escalate'}), 'review_escalate')
        self.assertEqual(da._outcome_from_outbox({}), 'unknown')

    def test_pr_url_extraction_from_summary(self):
        payload = {'summary': 'Mirror approved https://github.com/foo/bar/pull/12.'}
        self.assertEqual(
            da._pr_url_from_outbox(payload),
            'https://github.com/foo/bar/pull/12.',
        )

    def test_load_costs_jsonl_skips_malformed(self):
        p = self.tmp / 'blackboard' / 'costs.jsonl'
        p.write_text('{"ok":1}\nnot json\n{"ok":2}\n')
        rows = da._load_costs_jsonl(self.tmp)
        self.assertEqual(len(rows), 2)

    def test_agent_inbox_pending_counts_all_json_excludes_dotfiles(self):
        (self.tmp / 'inboxes/forge/task-1.json').write_text('{}')
        # marker-error-* and non-prefixed ids are real dispatch filenames.
        (self.tmp / 'inboxes/forge/marker-error-foo.json').write_text('{}')
        (self.tmp / 'inboxes/forge/build-install-drift-emission-fix-001.json').write_text('{}')
        # Non-.json and dotfiles must be skipped.
        (self.tmp / 'inboxes/forge/random.txt').write_text('')
        (self.tmp / 'inboxes/forge/.partial.json').write_text('{}')
        count, ids = da._agent_inbox_pending(self.tmp, 'forge')
        self.assertEqual(count, 3)
        self.assertEqual(
            ids,
            [
                'build-install-drift-emission-fix-001',
                'marker-error-foo',
                'task-1',
            ],
        )


class SystemStateLogReaderTest(unittest.TestCase):
    """GET /api/system/state-log reader (system self-awareness Slice 1 § D3).
    Fail-safe like the projects reader: missing/malformed → present=False, never
    a 500; a fresh, well-formed log carries present=True + a staleness flag."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-sl-'))

    def test_missing_file_present_false_not_500(self):
        out = da._reader_system_state_log(self.tmp / 'absent.json')
        self.assertFalse(out['present'])
        self.assertTrue(out['stale'])
        self.assertIsNone(out['narrative_prose'])

    def test_malformed_file_present_false(self):
        p = self.tmp / 'state.json'
        p.write_text('{ broken')
        out = da._reader_system_state_log(p)
        self.assertFalse(out['present'])

    def test_fresh_log_present_and_not_stale(self):
        p = self.tmp / 'state.json'
        p.write_text(json.dumps({
            'schema_version': 1,
            'as_of': '2026-06-19T12:00:00+00:00',
            'narrative_prose': 'Two missions progressing; nothing stuck.',
            'structured_snapshot': {'missions_active': []},
            'provenance': {'by': 'system-state-narrator', 'fallback': False},
        }))
        out = da._reader_system_state_log(p)
        self.assertTrue(out['present'])
        self.assertFalse(out['stale'])  # just written
        self.assertEqual(out['schema_version'], 1)
        self.assertEqual(
            out['narrative_prose'], 'Two missions progressing; nothing stuck.')
        self.assertIsNotNone(out['last_synced_at'])

    def test_path_resolver_honors_env_override(self):
        target = self.tmp / 'custom-state.json'
        with mock.patch.dict(
            os.environ, {'OURLIBERTY_SYSTEM_STATE_LOG': str(target)}
        ):
            self.assertEqual(da._state_log_json_path(), target)


if __name__ == '__main__':
    unittest.main()
