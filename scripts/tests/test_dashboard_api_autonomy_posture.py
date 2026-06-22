#!/usr/bin/env python3
"""Tests for GET /api/system/autonomy-posture (the autonomy POSTURE — system
self-awareness, the *prospective* dial-in surface, companion to the Automated
Work feed).

The endpoint is a pure read of the live trust policy via
`trust_policy.summarize_policy(trust_policy.load_policy())`. Both functions
fail-closed and never raise, so the lane never 500s — a bad/missing policy file
reads as degraded "everything asks you". The tests patch `trust_policy.load_policy`
to drive the stance, plus a no-patch smoke test that exercises the real wiring.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_autonomy_posture
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
import trust_policy as tp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN}

# A balanced policy: one auto_approve rule (Pulse doc-only on agent-core) plus a
# force_ask rule (TruPath). Mirrors trust_policy._self_test's fixture.
_BALANCED_POLICY = {
    'version': 1,
    'default_action': 'force_ask',
    'rules': [
        {
            'source': 'pulse', 'target': 'forge', 'task_type': 'doc-only',
            'repos': ['ourliberty-agent-core'], 'action': 'auto_approve',
        },
        {
            'source': '*', 'target': '*', 'repos': ['TruPath-*'],
            'action': 'force_ask',
        },
    ],
}

# Conservative: no auto_approve rule anywhere → nothing starts on its own.
_CONSERVATIVE_POLICY = {
    'version': 1, 'default_action': 'force_ask', 'rules': [],
}

# Fail-closed: what load_policy() returns when the file is unreadable/malformed.
_DEGRADED_POLICY = {
    'version': 1, 'default_action': 'force_ask', 'rules': [],
    '_error': 'failed to read trust-policy.json: boom',
}


class _Base(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.client = TestClient(da.app)


class AuthTest(_Base):
    def test_requires_token(self):
        self.assertEqual(
            self.client.get('/api/system/autonomy-posture').status_code, 401)
        self.assertEqual(
            self.client.get(
                '/api/system/autonomy-posture',
                headers={'X-Dashboard-Token': 'wrong'}).status_code, 401)


class PostureTest(_Base):
    def test_balanced_surfaces_what_auto_starts(self):
        with patch.object(tp, 'load_policy', return_value=_BALANCED_POLICY):
            r = self.client.get('/api/system/autonomy-posture', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['level'], 'balanced')
        self.assertFalse(body['degraded'])
        # the auto_approve rule shows up as a plain-language "starts on its own"
        self.assertEqual(len(body['auto_starts']), 1)
        self.assertIn('Pulse', body['auto_starts'][0])
        # the force_ask rule + the default land in "still asks you"
        self.assertTrue(any('TruPath' in s for s in body['still_asks']))
        self.assertTrue(
            any('default is to ask you' in s for s in body['still_asks']))
        # the always-on gates are always present
        self.assertTrue(body['gates'])
        self.assertTrue(any('Mirror' in g for g in body['gates']))

    def test_conservative_when_nothing_auto_starts(self):
        with patch.object(tp, 'load_policy', return_value=_CONSERVATIVE_POLICY):
            r = self.client.get('/api/system/autonomy-posture', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['level'], 'conservative')
        self.assertEqual(body['auto_starts'], [])
        self.assertFalse(body['degraded'])
        self.assertIn('asks you', body['headline'].lower())

    def test_degraded_policy_fails_closed_without_500(self):
        with patch.object(tp, 'load_policy', return_value=_DEGRADED_POLICY):
            r = self.client.get('/api/system/autonomy-posture', headers=AUTH)
        # the whole point: a broken policy file degrades, never 500s
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['degraded'])
        self.assertEqual(body['level'], 'conservative')
        self.assertEqual(body['auto_starts'], [])

    def test_smoke_real_policy_path_returns_full_shape(self):
        # No patch — exercise the real load_policy() → summarize_policy() wiring
        # and the response_model. Whatever policy the sandbox has, the shape and
        # 200 must hold.
        r = self.client.get('/api/system/autonomy-posture', headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ('level', 'headline', 'auto_starts', 'still_asks',
                    'gates', 'degraded'):
            self.assertIn(key, body)
        self.assertIn(body['level'], ('conservative', 'balanced'))
        self.assertIsInstance(body['auto_starts'], list)
        self.assertTrue(body['gates'])  # gates are constant, always present


if __name__ == '__main__':
    unittest.main()
