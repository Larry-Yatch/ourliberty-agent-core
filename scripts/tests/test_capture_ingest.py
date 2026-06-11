#!/usr/bin/env python3
"""Tests for the durable-capture ingest endpoint (Missions v2 Phase 1).

Mirrors test_desktop_session_ingest.py. Mirror focus per the build spec:
  - token reuse: the SAME X-Ingest-Token gates this endpoint; the dashboard
    read token does NOT authorize it.
  - atomic concurrent append: parallel POSTs all land (the lock serializes the
    read-modify-write; no append is lost).
  - idempotency: a re-POST with identical (title, origin.session_id) inside the
    window collapses onto the same id and does not double-park.
  - no PR side effect: a capture appends to captures.json only; it never calls
    the GitHub REST seam (durability/audit come from the GC healer's batch).

captures.json is redirected to a tmpdir via OURLIBERTY_CAPTURES_JSON so the
real `agents/beacon/captures.json` is never touched.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_capture_ingest
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Both tokens are read at request time. We FORCE them (per-test, in setUp) to
# the conventional shared test values and deliberately do NOT restore them in a
# tearDownModule. Restoring the real droplet token — the desktop ingest module's
# pattern — 401s any sibling module that forces 'test-token-value' at its own
# import and runs after us. That module dodges the bug only because it sorts
# after test_dashboard_api; this module sorts BEFORE it, so a restore here would
# clobber the whole dashboard_api suite (KeyError cascades from 401 bodies).
INGEST_TOKEN = 'ingest-test-token'
DASHBOARD_TOKEN = 'test-token-value'
os.environ['DESKTOP_INGEST_TOKEN'] = INGEST_TOKEN
os.environ['DASHBOARD_API_TOKEN'] = DASHBOARD_TOKEN

import dashboard_api as da  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

INGEST_AUTH = {'X-Ingest-Token': INGEST_TOKEN}
DASH_AUTH = {'X-Dashboard-Token': DASHBOARD_TOKEN}
ENDPOINT = '/api/ingest/capture'
GET_ENDPOINT = '/api/missions/captures'


def _force_tokens() -> None:
    """Pin both auth tokens to the shared test values right before each test —
    a preceding module's teardown may have reverted them to the real droplet
    secrets in this same process."""
    os.environ['DESKTOP_INGEST_TOKEN'] = INGEST_TOKEN
    os.environ['DASHBOARD_API_TOKEN'] = DASHBOARD_TOKEN


class CaptureIngestTest(unittest.TestCase):
    def setUp(self):
        _force_tokens()
        self.client = TestClient(da.app)
        self._tmp = tempfile.TemporaryDirectory()
        self.captures_path = Path(self._tmp.name) / 'captures.json'
        os.environ['OURLIBERTY_CAPTURES_JSON'] = str(self.captures_path)

    def tearDown(self):
        os.environ.pop('OURLIBERTY_CAPTURES_JSON', None)
        self._tmp.cleanup()

    def _read_file(self) -> dict:
        return json.loads(self.captures_path.read_text())

    # ---- auth / token reuse ----
    def test_missing_token_401(self):
        r = self.client.post(ENDPOINT, json={'title': 'x'})
        self.assertEqual(r.status_code, 401)

    def test_bad_token_401(self):
        r = self.client.post(ENDPOINT, headers={'X-Ingest-Token': 'nope'},
                             json={'title': 'x'})
        self.assertEqual(r.status_code, 401)

    def test_dashboard_token_does_not_authorize_ingest(self):
        # The read token must NOT open the write endpoint (token reuse: this is
        # the same narrow X-Ingest-Token as desktop-session, not the dash token).
        r = self.client.post(ENDPOINT, headers=DASH_AUTH, json={'title': 'x'})
        self.assertEqual(r.status_code, 401)

    # ---- success shape ----
    def test_success_parks_capture_and_returns_id(self):
        r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
            'title': 'Resurface stale PR comments',
            'note': 'Mirror sometimes drops review comments on rebase.',
            'origin': {'source': 'desktop-chat', 'session_id': 's1',
                       'repo': 'ourliberty-agent-core'},
        })
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['capture_id'].startswith('cap-resurface-stale-pr-comments-'))
        # Persisted to captures.json; a write bumps the registry to v2 (the
        # schema that carries the first-class `label`).
        data = self._read_file()
        self.assertEqual(data['schema_version'], 2)
        self.assertEqual(len(data['captures']), 1)
        cap = data['captures'][0]
        self.assertEqual(cap['id'], body['capture_id'])
        self.assertEqual(cap['state'], 'parked')
        # Absent label → stored as None (back-compat with label-less clients).
        self.assertIsNone(cap['label'])
        self.assertIsNone(cap['promoted_to'])
        self.assertEqual(cap['origin']['source'], 'desktop-chat')
        self.assertEqual(cap['origin']['session_id'], 's1')
        self.assertEqual(cap['origin']['repo'], 'ourliberty-agent-core')
        self.assertIn('captured_at', cap['origin'])
        self.assertEqual(cap['last_touched'], cap['origin']['captured_at'])

    def test_source_pinned_to_fixed_set(self):
        r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
            'title': 'bad source', 'origin': {'source': 'forge-spoof'}})
        self.assertEqual(r.status_code, 400)

    def test_default_source_when_omitted(self):
        r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={'title': 'no origin'})
        self.assertEqual(r.status_code, 202)
        cap = self._read_file()['captures'][0]
        self.assertEqual(cap['origin']['source'], 'desktop-chat')
        self.assertIsNone(cap['origin']['session_id'])

    # ---- validation ----
    def test_empty_title_400(self):
        r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={'title': '   '})
        self.assertEqual(r.status_code, 400)

    def test_oversized_payload_413(self):
        big = 'x' * (da.MAX_CAPTURE_PAYLOAD_BYTES + 1)
        r = self.client.post(ENDPOINT, headers=INGEST_AUTH,
                             json={'title': 'big', 'note': big})
        self.assertEqual(r.status_code, 413)

    # ---- label (first-class, allowlisted) ----
    def test_valid_label_stored(self):
        r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
            'title': 'recurring pulse proposal',
            'label': 'pulse-check-i',
            'origin': {'source': 'agent'},
        })
        self.assertEqual(r.status_code, 202)
        cap = self._read_file()['captures'][0]
        self.assertEqual(cap['label'], 'pulse-check-i')

    def test_unknown_label_400(self):
        # A non-None label outside the allowlist is rejected on the write path —
        # a leaked ingest token must only write known labels.
        r = self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
            'title': 'bad label', 'label': 'totally-made-up'})
        self.assertEqual(r.status_code, 400)
        # Nothing was parked.
        self.assertFalse(self.captures_path.exists()
                         and self._read_file()['captures'])

    def test_absent_label_back_compat_none(self):
        # A label-less POST (desktop/telegram gesture) parks with label=None.
        r = self.client.post(ENDPOINT, headers=INGEST_AUTH,
                             json={'title': 'no label here'})
        self.assertEqual(r.status_code, 202)
        cap = self._read_file()['captures'][0]
        self.assertIsNone(cap['label'])

    def test_label_does_not_enter_idempotency_key(self):
        # A re-POST with the same (title, session_id) collapses onto the same id
        # even though it carries a label — label is not part of the dedup key.
        payload = {'title': 'labelled dup', 'label': 'pulse-check-i',
                   'origin': {'source': 'agent', 'session_id': 'sess-L'}}
        r1 = self.client.post(ENDPOINT, headers=INGEST_AUTH, json=payload)
        r2 = self.client.post(ENDPOINT, headers=INGEST_AUTH, json=payload)
        self.assertEqual(r1.json()['capture_id'], r2.json()['capture_id'])
        self.assertEqual(len(self._read_file()['captures']), 1)

    # ---- idempotency ----
    def test_idempotent_repost_collapses_onto_same_id(self):
        payload = {'title': 'dup follow-up', 'origin': {'session_id': 'sess-9'}}
        r1 = self.client.post(ENDPOINT, headers=INGEST_AUTH, json=payload)
        r2 = self.client.post(ENDPOINT, headers=INGEST_AUTH, json=payload)
        self.assertEqual(r1.status_code, 202)
        self.assertEqual(r2.status_code, 202)
        self.assertEqual(r1.json()['capture_id'], r2.json()['capture_id'])
        # Only ONE capture parked despite two POSTs.
        self.assertEqual(len(self._read_file()['captures']), 1)

    def test_different_session_id_does_not_collapse(self):
        self.client.post(ENDPOINT, headers=INGEST_AUTH,
                         json={'title': 'same title', 'origin': {'session_id': 'a'}})
        self.client.post(ENDPOINT, headers=INGEST_AUTH,
                         json={'title': 'same title', 'origin': {'session_id': 'b'}})
        self.assertEqual(len(self._read_file()['captures']), 2)

    def test_idempotency_window_expires(self):
        # Outside the window, the same (title, session_id) parks a fresh card.
        now = datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc)
        later = now + timedelta(seconds=da.CAPTURE_IDEMPOTENCY_WINDOW_SEC + 1)
        first = da._handle_capture_ingest(
            title='windowed', note=None, origin={'session_id': 'w'},
            captures_path=self.captures_path, now=now)
        second = da._handle_capture_ingest(
            title='windowed', note=None, origin={'session_id': 'w'},
            captures_path=self.captures_path, now=later)
        self.assertNotEqual(first['capture_id'], second['capture_id'])
        self.assertEqual(len(self._read_file()['captures']), 2)

    # ---- atomic concurrent append ----
    def test_concurrent_appends_all_land(self):
        n = 25

        def post(i):
            return self.client.post(ENDPOINT, headers=INGEST_AUTH, json={
                'title': f'concurrent {i}', 'origin': {'session_id': f's{i}'}})

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(post, range(n)))

        self.assertTrue(all(r.status_code == 202 for r in results))
        data = self._read_file()
        # No lost writes: every distinct capture is present, ids are unique.
        self.assertEqual(len(data['captures']), n)
        ids = {c['id'] for c in data['captures']}
        self.assertEqual(len(ids), n)

    # ---- no PR side effect ----
    def test_capture_does_not_open_a_pr(self):
        # The GitHub REST seam must never be touched by a capture POST — the
        # whole design choice is "no PR-per-capture; the GC healer batch-commits."
        with mock.patch.object(
            da, '_github_api_request',
            side_effect=AssertionError('capture must not call GitHub'),
        ):
            r = self.client.post(ENDPOINT, headers=INGEST_AUTH,
                                 json={'title': 'no pr please'})
        self.assertEqual(r.status_code, 202)

    # ---- pure-handler guards (route layer can't always reach these) ----
    def test_handler_rejects_non_object_origin(self):
        with self.assertRaises(HTTPException) as ctx:
            da._handle_capture_ingest(
                title='x', note=None, origin=['not', 'a', 'dict'],
                captures_path=self.captures_path)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_deterministic_suffix_still_unique(self):
        # A colliding suffix generator must not produce duplicate ids. Distinct
        # session_ids dodge the idempotency collapse so we exercise the
        # same-slug + same-suffix disambiguation path directly.
        with mock.patch.object(da, '_gen_capture_suffix', return_value='ffff'):
            a = da._handle_capture_ingest(title='same', note=None,
                                          origin={'session_id': 'a'},
                                          captures_path=self.captures_path)
            b = da._handle_capture_ingest(title='same', note=None,
                                          origin={'session_id': 'b'},
                                          captures_path=self.captures_path)
        self.assertNotEqual(a['capture_id'], b['capture_id'])


class CapturesReaderTest(unittest.TestCase):
    def setUp(self):
        _force_tokens()
        self.client = TestClient(da.app)
        self._tmp = tempfile.TemporaryDirectory()
        self.captures_path = Path(self._tmp.name) / 'captures.json'
        os.environ['OURLIBERTY_CAPTURES_JSON'] = str(self.captures_path)

    def tearDown(self):
        os.environ.pop('OURLIBERTY_CAPTURES_JSON', None)
        self._tmp.cleanup()

    def test_get_requires_dashboard_token(self):
        r = self.client.get(GET_ENDPOINT)
        self.assertEqual(r.status_code, 401)

    def test_get_missing_file_returns_empty(self):
        r = self.client.get(GET_ENDPOINT, headers=DASH_AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['captures'], [])
        self.assertIsNone(body['last_synced_at'])

    def test_get_returns_all_states_verbatim(self):
        # The endpoint passes captures through verbatim (dashboard filters to
        # parked); promoted/dropped are kept in the file for audit.
        self.captures_path.write_text(json.dumps({
            'schema_version': 1,
            'captures': [
                {'id': 'cap-a', 'state': 'parked'},
                {'id': 'cap-b', 'state': 'promoted'},
                {'id': 'cap-c', 'state': 'dropped'},
            ],
        }))
        r = self.client.get(GET_ENDPOINT, headers=DASH_AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body['captures']), 3)
        self.assertEqual(body['schema_version'], 1)
        self.assertIsNotNone(body['last_synced_at'])

    def test_get_malformed_file_500(self):
        self.captures_path.write_text('{not json')
        r = self.client.get(GET_ENDPOINT, headers=DASH_AUTH)
        self.assertEqual(r.status_code, 500)


if __name__ == '__main__':
    unittest.main()
