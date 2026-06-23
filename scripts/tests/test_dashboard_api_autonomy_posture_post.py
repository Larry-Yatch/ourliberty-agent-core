#!/usr/bin/env python3
"""Tests for POST /api/system/autonomy-posture (the autonomy DIAL write, #7).

Token + actor gated like /api/system/rotation; writes the chosen preset to the
override policy file (trust_policy.OVERRIDE_POLICY_PATH, monkeypatched to a temp
path here) atomically + a larry_action audit row, then echoes the resulting
posture. A recording supabase stub captures the audit upsert.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_autonomy_posture_post
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
import trust_policy as tp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN, 'X-Actor': 'larry@sealteamleaders.com'}


class _RecordingSupabase:
    """Captures the chain_events upsert the handler writes for the audit row."""

    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []

    def table(self, name: str) -> "_RecordingSupabase":
        self._table = name
        return self

    def upsert(self, rows: list[dict[str, Any]], **kw: Any) -> "_RecordingSupabase":
        self._pending = (rows, kw)
        return self

    def execute(self) -> Any:
        rows, kw = self._pending
        self.upserts.append({'table': self._table, 'rows': rows, 'kw': kw})
        return type('R', (), {'data': []})()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.client = TestClient(da.app)
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_override = tp.OVERRIDE_POLICY_PATH
        tp.OVERRIDE_POLICY_PATH = Path(self._tmp.name) / 'trust-policy.override.json'
        self._orig_get_client = da._get_larry_action_supabase_client
        self.supabase = _RecordingSupabase()
        da._get_larry_action_supabase_client = lambda: self.supabase  # type: ignore[assignment]

    def tearDown(self) -> None:
        tp.OVERRIDE_POLICY_PATH = self._orig_override
        da._get_larry_action_supabase_client = self._orig_get_client  # type: ignore[assignment]
        self._tmp.cleanup()


class AuthTest(_Base):
    def test_requires_token(self):
        r = self.client.post('/api/system/autonomy-posture',
                             json={'level': 'balanced'})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(tp.OVERRIDE_POLICY_PATH.exists())

    def test_requires_allowlisted_actor(self):
        # valid token, missing/disallowed actor → 401, no write
        r = self.client.post(
            '/api/system/autonomy-posture',
            headers={'X-Dashboard-Token': TOKEN},
            json={'level': 'balanced'},
        )
        self.assertEqual(r.status_code, 401)
        r2 = self.client.post(
            '/api/system/autonomy-posture',
            headers={'X-Dashboard-Token': TOKEN, 'X-Actor': 'nope@evil.com'},
            json={'level': 'balanced'},
        )
        self.assertEqual(r2.status_code, 401)
        self.assertFalse(tp.OVERRIDE_POLICY_PATH.exists())
        self.assertEqual(self.supabase.upserts, [])


class ValidationTest(_Base):
    def test_unknown_level_400_no_write_no_audit(self):
        r = self.client.post('/api/system/autonomy-posture',
                             headers=AUTH, json={'level': 'yolo'})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(tp.OVERRIDE_POLICY_PATH.exists())
        self.assertEqual(self.supabase.upserts, [])

    def test_supabase_unavailable_503(self):
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        r = self.client.post('/api/system/autonomy-posture',
                             headers=AUTH, json={'level': 'balanced'})
        self.assertEqual(r.status_code, 503)


class WriteTest(_Base):
    def test_each_level_writes_override_and_echoes_posture(self):
        for level in ('conservative', 'balanced', 'loose'):
            r = self.client.post('/api/system/autonomy-posture',
                                 headers=AUTH, json={'level': level})
            self.assertEqual(r.status_code, 200, (level, r.text))
            body = r.json()
            self.assertEqual(body['level'], level)
            self.assertFalse(body['degraded'])
            self.assertTrue(body['gates'])
            # the override file now holds the preset, stamped with _preset
            self.assertTrue(tp.OVERRIDE_POLICY_PATH.exists())
            written = json.loads(tp.OVERRIDE_POLICY_PATH.read_text())
            self.assertEqual(written['_preset'], level)
            self.assertEqual(written['default_action'], 'force_ask')

    def test_loose_is_reported_distinctly(self):
        r = self.client.post('/api/system/autonomy-posture',
                             headers=AUTH, json={'level': 'loose'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['level'], 'loose')

    def test_writes_audit_row_with_actor_and_level(self):
        self.client.post('/api/system/autonomy-posture',
                        headers=AUTH, json={'level': 'conservative'})
        self.assertEqual(len(self.supabase.upserts), 1)
        up = self.supabase.upserts[0]
        self.assertEqual(up['table'], 'chain_events')
        row = up['rows'][0]
        self.assertEqual(row['event_type'], 'larry_action')
        self.assertEqual(row['actor'], 'larry@sealteamleaders.com')
        self.assertEqual(row['task_id'], 'autonomy-posture')
        self.assertEqual(row['payload'].get('control'), 'autonomy_posture')
        self.assertEqual(row['payload'].get('level'), 'conservative')


if __name__ == '__main__':
    unittest.main()
