#!/usr/bin/env python3
"""Tests for the desktop-session ingest endpoint (Missions v2 Phase 0).

Covers auth (dedicated X-Ingest-Token), event_type validation, the
agent='desktop-claude' pin (a caller cannot spoof another agent), the
success 202 shape, and the emit-failure 502 path. The emitter is injected
via the `_get_desktop_emit` seam so these run without supabase-py.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_desktop_session_ingest
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Ingest token is read at request time; set before any request. A dashboard
# token is also set so app import / unrelated deps stay happy.
_ORIG_INGEST = os.environ.get('DESKTOP_INGEST_TOKEN')
_ORIG_DASH = os.environ.get('DASHBOARD_API_TOKEN')
os.environ['DESKTOP_INGEST_TOKEN'] = 'ingest-test-token'
os.environ.setdefault('DASHBOARD_API_TOKEN', 'test-token-value')

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

INGEST_AUTH = {'X-Ingest-Token': 'ingest-test-token'}
ENDPOINT = '/api/ingest/desktop-session'


def tearDownModule():  # noqa: N802 — unittest API
    for key, orig in (('DESKTOP_INGEST_TOKEN', _ORIG_INGEST),
                      ('DASHBOARD_API_TOKEN', _ORIG_DASH)):
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig


def _fake_emit_resolver(emit_return=True, recorder=None):
    """Return a (emit_event, compute_event_id) pair for the _get_desktop_emit
    seam. `recorder` (a dict) captures the kwargs emit_event was called with."""
    def fake_emit(**kwargs):
        if recorder is not None:
            recorder.update(kwargs)
        return emit_return

    def fake_compute(task_id, event_type, ts):
        return f'evt-{task_id}-{event_type}-{ts}'

    return lambda: (fake_emit, fake_compute)


class DesktopSessionIngestTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(da.app)

    # ---- auth ----
    def test_missing_token_401(self):
        r = self.client.post(ENDPOINT, json={
            'event_type': 'desktop_session_start', 'task_id': 'x', 'payload': {}})
        self.assertEqual(r.status_code, 401)

    def test_bad_token_401(self):
        r = self.client.post(ENDPOINT, headers={'X-Ingest-Token': 'nope'}, json={
            'event_type': 'desktop_session_start', 'task_id': 'x', 'payload': {}})
        self.assertEqual(r.status_code, 401)

    def test_dashboard_token_does_not_authorize_ingest(self):
        # The read token must NOT open the write endpoint.
        r = self.client.post(ENDPOINT, headers={'X-Dashboard-Token': 'test-token-value'},
                             json={'event_type': 'desktop_session_start',
                                   'task_id': 'x', 'payload': {}})
        self.assertEqual(r.status_code, 401)

    # ---- validation ----
    def test_non_desktop_event_type_rejected_400(self):
        with mock.patch.object(da, '_get_desktop_emit', _fake_emit_resolver()):
            r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
                'event_type': 'session_start',  # a real type, but not a desktop one
                'task_id': 'x', 'payload': {}})
        self.assertEqual(r.status_code, 400)

    # ---- success + agent pin ----
    def test_success_pins_agent_and_returns_event_id(self):
        rec: dict = {}
        with mock.patch.object(da, '_get_desktop_emit',
                               _fake_emit_resolver(emit_return=True, recorder=rec)):
            r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
                'event_type': 'desktop_session_start',
                'task_id': 'desktop-abc12345',
                'payload': {'repo': 'ourliberty-agent-core', 'branch': 'feat/x',
                            'agent': 'forge'}})  # body 'agent' must be ignored
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['event_id'])
        # agent is PINNED server-side regardless of anything in the body.
        self.assertEqual(rec['agent'], 'desktop-claude')
        self.assertEqual(rec['event_type'], 'desktop_session_start')
        self.assertEqual(rec['task_id'], 'desktop-abc12345')
        self.assertIn('ts', rec)

    def test_oversized_payload_413(self):
        big = {'blob': 'x' * (da.MAX_DESKTOP_PAYLOAD_BYTES + 1)}
        with mock.patch.object(da, '_get_desktop_emit', _fake_emit_resolver()):
            r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
                'event_type': 'desktop_session_start',
                'task_id': 'x', 'payload': big})
        self.assertEqual(r.status_code, 413)

    def test_done_event_ok(self):
        with mock.patch.object(da, '_get_desktop_emit', _fake_emit_resolver()):
            r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
                'event_type': 'desktop_session_done',
                'task_id': 'desktop-abc12345', 'payload': {}})
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.json()['ok'])

    # ---- emit failure ----
    def test_emit_failure_returns_502(self):
        with mock.patch.object(da, '_get_desktop_emit',
                               _fake_emit_resolver(emit_return=False)):
            r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
                'event_type': 'desktop_session_active',
                'task_id': 'desktop-abc12345', 'payload': {}})
        self.assertEqual(r.status_code, 502)
        body = r.json()
        self.assertFalse(body['ok'])
        self.assertIsNone(body['event_id'])

    # ---- pure-handler 400 on non-object payload (route layer can't hit this;
    #      pydantic coerces, but the handler guards direct callers) ----
    def test_handler_rejects_non_object_payload(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            da._handle_desktop_session_ingest(
                event_type='desktop_session_start', task_id='x',
                payload=['not', 'a', 'dict'],
                emit_resolver=_fake_emit_resolver())
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
