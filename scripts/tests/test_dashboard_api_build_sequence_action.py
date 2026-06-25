#!/usr/bin/env python3
"""Tests for POST /api/system/build-sequences/{seq_id}/action.

Spec: agents/beacon/specs/operator-needs-you-feed.md § 5.5. The steering
write companion to the read-only GET /api/system/build-sequences: it
validates the verb (resume|skip|cancel|retry) against the allowlist,
delegates to the matching sequence_shortcut_helpers.apply_* helper, and
writes an audited larry_action chain_events row.

Same token+actor gate as /api/system/rotation. The supabase client is a
recording stub; sequence_shortcut_helpers.AGENTS_ROOT is monkeypatched onto
a tmpdir so the helper reads/writes the synthetic blackboard tree, never
``~/agents/``.

Mirror focus: unknown action → 400, missing sequence → 404, a valid action
runs the helper (file mutates) AND writes the audit row.

Run::

    cd /home/larry/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_build_sequence_action
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
from pathlib import Path
from typing import Any, Optional

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
import sequence_shortcut_helpers as ssh  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ALLOWED_ACTOR = 'larry@sealteamleaders.com'
AUTH = {
    'X-Dashboard-Token': TOKEN,
    'X-Actor': ALLOWED_ACTOR,
    'Content-Type': 'application/json',
}


class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


class _RecordingClient:
    """Captures the upsert chain the action POST writes (audit row only)."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self._table: Optional[str] = None
        self._upsert_rows: Optional[list[dict[str, Any]]] = None
        self._upsert_kwargs: Optional[dict[str, Any]] = None

    def table(self, name: str):
        self._table = name
        self._upsert_rows = None
        self._upsert_kwargs = None
        return self

    def upsert(self, rows: list[dict[str, Any]], **kwargs):
        self._upsert_rows = rows
        self._upsert_kwargs = kwargs
        return self

    def execute(self):
        self.calls.append({
            'table': self._table,
            'upsert_rows': self._upsert_rows,
            'upsert_kwargs': self._upsert_kwargs,
        })
        return _Resp([])


def _make_seq(seq_id: str, status: str, **overrides) -> dict:
    seq = {
        'seq_id': seq_id,
        'label': f'test sequence {seq_id}',
        'spec_doc': 'agents/beacon/specs/test.md',
        'created_at': '2026-05-20T15:00:00-06:00',
        'created_by': 'beacon',
        'status': status,
        'current_steps': [],
        'steps': [
            {
                'step_id': 'step-1',
                'label': 'first step',
                'depends_on': [],
                'dispatch_text': 'do the thing',
                'target_repo': 'ourliberty-agent-core',
                'task_type': 'feature-development',
                'expected_cost_usd': 3,
                'status': 'pending',
                'dispatched_at': None,
                'merged_at': None,
                'pr_url': None,
                'current_actor': None,
                'failure_reason': None,
            },
        ],
        'audit_log': [
            {
                'ts': '2026-05-20T15:00:00-06:00',
                'event': 'sequence-created',
                'actor': 'beacon',
            },
        ],
    }
    seq.update(overrides)
    return seq


class _ActionBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-bsq-action-'))
        self.agents_root = self.tmp / 'agents'
        self.blackboard = self.agents_root / 'blackboard' / 'build-sequences'
        self.blackboard.mkdir(parents=True, exist_ok=True)

        # The handler and the apply_* helpers both resolve the sequence file
        # from sequence_shortcut_helpers.AGENTS_ROOT — single-source the path.
        self._orig_ssh_root = ssh.AGENTS_ROOT
        ssh.AGENTS_ROOT = self.agents_root  # type: ignore[assignment]

        self._orig_get_client = da._get_larry_action_supabase_client
        self.client_stub = _RecordingClient()
        da._get_larry_action_supabase_client = (  # type: ignore[assignment]
            lambda: self.client_stub
        )
        self.c = TestClient(da.app)

    def tearDown(self):
        ssh.AGENTS_ROOT = self._orig_ssh_root  # type: ignore[assignment]
        da._get_larry_action_supabase_client = (  # type: ignore[assignment]
            self._orig_get_client
        )

    def _write_seq(self, seq_id: str, status: str, **overrides) -> Path:
        path = self.blackboard / f'{seq_id}.json'
        path.write_text(json.dumps(_make_seq(seq_id, status, **overrides), indent=2))
        return path

    def _url(self, seq_id: str) -> str:
        return f'/api/system/build-sequences/{seq_id}/action'


# ==================== validation: unknown action → 400 ====================


class UnknownActionTest(_ActionBase):
    def test_unknown_action_returns_400(self):
        self._write_seq('seq-paused', 'paused')
        r = self.c.post(self._url('seq-paused'), headers=AUTH,
                        json={'action': 'frobnicate'})
        self.assertEqual(r.status_code, 400)
        # No audit row written on the rejected path.
        self.assertEqual(self.client_stub.calls, [])

    def test_unknown_action_rejected_before_existence_check(self):
        # Even with no sequence file, an unknown verb 400s (validation first).
        r = self.c.post(self._url('does-not-exist'), headers=AUTH,
                        json={'action': 'nope'})
        self.assertEqual(r.status_code, 400)

    def test_step_action_missing_step_id_returns_400(self):
        self._write_seq('seq-active', 'active')
        r = self.c.post(self._url('seq-active'), headers=AUTH,
                        json={'action': 'skip'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client_stub.calls, [])


# ==================== missing sequence → 404 ====================


class MissingSequenceTest(_ActionBase):
    def test_missing_sequence_returns_404(self):
        r = self.c.post(self._url('ghost-seq'), headers=AUTH,
                        json={'action': 'resume'})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client_stub.calls, [])

    def test_missing_sequence_step_action_returns_404(self):
        # step_id present (passes the 400 gate) but file absent → 404.
        r = self.c.post(self._url('ghost-seq'), headers=AUTH,
                        json={'action': 'skip', 'step_id': 'step-1'})
        self.assertEqual(r.status_code, 404)


# ==================== valid action → helper runs + audit row ====================


class ValidActionTest(_ActionBase):
    def test_resume_runs_helper_and_writes_audit(self):
        path = self._write_seq('seq-paused', 'paused')
        r = self.c.post(self._url('seq-paused'), headers=AUTH,
                        json={'action': 'resume'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['applied'])
        self.assertEqual(body['action'], 'resume')
        self.assertEqual(body['seq_id'], 'seq-paused')
        self.assertIsNone(body['step_id'])
        self.assertTrue(body['audit_persisted'])
        self.assertIsNotNone(body['action_event_id'])

        # Helper actually mutated the file: paused → active.
        seq = json.loads(path.read_text())
        self.assertEqual(seq['status'], 'active')

        # Exactly one audit upsert, shaped like a larry_action row keyed on
        # the sequence id.
        self.assertEqual(len(self.client_stub.calls), 1)
        call = self.client_stub.calls[0]
        self.assertEqual(call['table'], 'chain_events')
        row = call['upsert_rows'][0]
        self.assertEqual(row['event_type'], 'larry_action')
        self.assertEqual(row['actor'], ALLOWED_ACTOR)
        self.assertEqual(row['agent'], 'dashboard')
        self.assertEqual(row['task_id'], 'seq-paused')
        self.assertEqual(row['payload']['control'], 'build_sequence_action')
        self.assertEqual(row['payload']['action'], 'resume')
        self.assertEqual(row['payload']['seq_id'], 'seq-paused')
        self.assertEqual(call['upsert_kwargs'].get('on_conflict'), 'event_id')
        self.assertTrue(call['upsert_kwargs'].get('ignore_duplicates'))

    def test_cancel_forwards_reason_and_audits(self):
        self._write_seq('seq-active', 'active')
        r = self.c.post(self._url('seq-active'), headers=AUTH,
                        json={'action': 'cancel', 'reason': 'no longer needed'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['action'], 'cancel')
        self.assertTrue(body['audit_persisted'])
        row = self.client_stub.calls[0]['upsert_rows'][0]
        self.assertEqual(row['payload']['reason'], 'no longer needed')

    def test_idempotent_noop_still_audits(self):
        # Resuming an already-active sequence is a helper no-op (applied=False,
        # not an error). It still returns 200 and writes an audit row.
        self._write_seq('seq-active', 'active')
        r = self.c.post(self._url('seq-active'), headers=AUTH,
                        json={'action': 'resume'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body['applied'])
        self.assertTrue(body['audit_persisted'])
        self.assertEqual(len(self.client_stub.calls), 1)

    def test_skip_unknown_step_returns_404(self):
        # File exists but the step doesn't — the helper hard-errors with a
        # "not found" reason, which maps to 404.
        self._write_seq('seq-active', 'active')
        r = self.c.post(self._url('seq-active'), headers=AUTH,
                        json={'action': 'skip', 'step_id': 'no-such-step'})
        self.assertEqual(r.status_code, 404)


# ==================== auth gate ====================


class AuthTest(_ActionBase):
    def test_missing_token_401(self):
        self._write_seq('seq-paused', 'paused')
        r = self.c.post(self._url('seq-paused'),
                        headers={'X-Actor': ALLOWED_ACTOR,
                                 'Content-Type': 'application/json'},
                        json={'action': 'resume'})
        self.assertEqual(r.status_code, 401)

    def test_missing_actor_401(self):
        self._write_seq('seq-paused', 'paused')
        r = self.c.post(self._url('seq-paused'),
                        headers={'X-Dashboard-Token': TOKEN,
                                 'Content-Type': 'application/json'},
                        json={'action': 'resume'})
        self.assertEqual(r.status_code, 401)

    def test_supabase_unavailable_503(self):
        self._write_seq('seq-paused', 'paused')
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        r = self.c.post(self._url('seq-paused'), headers=AUTH,
                        json={'action': 'resume'})
        self.assertEqual(r.status_code, 503)


if __name__ == '__main__':
    unittest.main()
