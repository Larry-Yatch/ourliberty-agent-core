#!/usr/bin/env python3
"""Tests for GET/POST /api/system/rotation (dashboard-rotation-switch-001).

The Auto/Off switch for account-tier rotation. GET resolves the effective
mode from the runtime override file (~/agents/rotation.disabled) + the
config rotation.enabled default. POST toggles that override file and writes
a larry_action audit row — same token+actor gate as /api/larry/action.

The supabase client is stubbed via a recording stub that captures the
upsert chain. Tests monkeypatch da._get_larry_action_supabase_client,
da._agents_root (override-file dir), and da._agent_models_json_path
(config default) so nothing touches the real droplet state.

Run::

    cd /home/larry/agent-core && \\
        python3 -m unittest scripts.tests.test_dashboard_api_rotation
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
from fastapi.testclient import TestClient  # noqa: E402


ALLOWED_ACTOR = 'larry@sealteamleaders.com'
AUTH = {
    'X-Dashboard-Token': TOKEN,
    'X-Actor': ALLOWED_ACTOR,
    'Content-Type': 'application/json',
}
TOKEN_ONLY = {'X-Dashboard-Token': TOKEN}


class _RecordingClient:
    """Captures the upsert chain the rotation POST uses (no select/update)."""

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


class _Resp:
    def __init__(self, data: list[Any]):
        self.data = data


class _RotationBase(unittest.TestCase):
    def setUp(self):
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-rotation-'))
        self.models_path = self.tmp / 'agent-models.json'
        self._write_config(enabled=True)
        self._orig_agents_root = da._agents_root
        self._orig_models_path = da._agent_models_json_path
        self._orig_get_client = da._get_larry_action_supabase_client
        da._agents_root = lambda: self.tmp  # type: ignore[assignment]
        da._agent_models_json_path = lambda: self.models_path  # type: ignore[assignment]
        self.client_stub = _RecordingClient()
        da._get_larry_action_supabase_client = lambda: self.client_stub  # type: ignore[assignment]
        self.c = TestClient(da.app)

    def tearDown(self):
        da._agents_root = self._orig_agents_root  # type: ignore[assignment]
        da._agent_models_json_path = self._orig_models_path  # type: ignore[assignment]
        da._get_larry_action_supabase_client = self._orig_get_client  # type: ignore[assignment]

    def _write_config(self, *, enabled: bool):
        self.models_path.write_text(json.dumps({'rotation': {'enabled': enabled}}))

    def _override_path(self) -> Path:
        return self.tmp / 'rotation.disabled'

    def _write_active_tier(self, tier: str):
        # Mirror the live state file rotate_active_tier maintains via the
        # active_tier helpers: blackboard/active-tier.json with a "tier" key.
        state = self.tmp / 'blackboard' / 'active-tier.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'tier': tier}))


# ---------- GET ----------

class GetRotationTest(_RotationBase):

    def test_auto_when_config_enabled_and_no_override(self):
        r = self.c.get('/api/system/rotation', headers=TOKEN_ONLY)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['mode'], 'auto')
        self.assertFalse(body['override_active'])
        self.assertTrue(body['config_enabled'])
        self.assertIn('as_of', body)

    def test_off_when_override_present(self):
        self._override_path().touch()
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['mode'], 'off')
        self.assertTrue(body['override_active'])
        self.assertTrue(body['config_enabled'])

    def test_off_when_config_disabled_even_without_override(self):
        self._write_config(enabled=False)
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['mode'], 'off')
        self.assertFalse(body['override_active'])
        self.assertFalse(body['config_enabled'])

    # ---- manual-pin (spec § 6.5): pinned_tier surfaced on GET ----

    def test_auto_pinned_tier_is_null(self):
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['mode'], 'auto')
        self.assertIsNone(body['pinned_tier'])

    def test_off_override_tier2_reports_pinned_tier2(self):
        self._override_path().write_text('tier2')
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['mode'], 'off')
        self.assertEqual(body['pinned_tier'], 'tier2')

    def test_off_empty_override_reports_tier1(self):
        # Legacy empty touch → tier1 (historical Off behavior).
        self._override_path().touch()
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['mode'], 'off')
        self.assertEqual(body['pinned_tier'], 'tier1')

    def test_off_config_disabled_reports_tier1(self):
        # Off purely because config disabled (no override file) → tier1,
        # matching the scheduler's config-disabled force.
        self._write_config(enabled=False)
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['pinned_tier'], 'tier1')

    # ---- current_tier: the live tier Auto is actually running on ----

    def test_auto_reports_current_tier_from_state_file(self):
        # Auto owns the tier via the load gate; current_tier reflects the live
        # state file even though pinned_tier stays null.
        self._write_active_tier('tier2')
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['mode'], 'auto')
        self.assertIsNone(body['pinned_tier'])
        self.assertEqual(body['current_tier'], 'tier2')

    def test_current_tier_defaults_tier1_when_state_missing(self):
        # No state file yet → tier1 fallback (matches active_tier.read).
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['current_tier'], 'tier1')

    def test_current_tier_independent_of_pinned_tier(self):
        # Off-pinned tier1, but the scheduler hasn't switched off tier2 yet:
        # current_tier reports the live tier, not the pin.
        self._override_path().write_text('tier1')
        self._write_active_tier('tier2')
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['mode'], 'off')
        self.assertEqual(body['pinned_tier'], 'tier1')
        self.assertEqual(body['current_tier'], 'tier2')

    def test_current_tier_malformed_state_defaults_tier1(self):
        state = self.tmp / 'blackboard' / 'active-tier.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{not json')
        body = self.c.get('/api/system/rotation', headers=TOKEN_ONLY).json()
        self.assertEqual(body['current_tier'], 'tier1')

    def test_missing_token_401(self):
        r = self.c.get('/api/system/rotation')
        self.assertEqual(r.status_code, 401)

    def test_bad_token_401(self):
        r = self.c.get('/api/system/rotation',
                       headers={'X-Dashboard-Token': 'wrong'})
        self.assertEqual(r.status_code, 401)


# ---------- POST ----------

class PostRotationTest(_RotationBase):

    def test_off_creates_override_and_audits(self):
        self.assertFalse(self._override_path().exists())
        r = self.c.post('/api/system/rotation', headers=AUTH, json={'mode': 'off'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['mode'], 'off')
        self.assertTrue(body['override_active'])
        self.assertIn('action_event_id', body)
        # Override file now present.
        self.assertTrue(self._override_path().exists())
        # Exactly one audit upsert, shaped like a larry_action row.
        self.assertEqual(len(self.client_stub.calls), 1)
        call = self.client_stub.calls[0]
        self.assertEqual(call['table'], 'chain_events')
        row = call['upsert_rows'][0]
        self.assertEqual(row['event_type'], 'larry_action')
        self.assertEqual(row['actor'], ALLOWED_ACTOR)
        self.assertEqual(row['agent'], 'dashboard')
        self.assertEqual(row['payload']['control'], 'rotation_mode')
        self.assertEqual(row['payload']['mode'], 'off')
        self.assertEqual(call['upsert_kwargs'].get('on_conflict'), 'event_id')

    def test_post_response_carries_current_tier(self):
        # The POST result reuses _reader_rotation_mode, so current_tier rides
        # through on the update response too.
        self._write_active_tier('tier2')
        body = self.c.post('/api/system/rotation', headers=AUTH,
                           json={'mode': 'auto'}).json()
        self.assertEqual(body['current_tier'], 'tier2')

    def test_auto_removes_override(self):
        self._override_path().touch()
        r = self.c.post('/api/system/rotation', headers=AUTH, json={'mode': 'auto'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['mode'], 'auto')
        self.assertFalse(body['override_active'])
        self.assertFalse(self._override_path().exists())

    def test_auto_when_no_override_is_idempotent(self):
        # Removing an absent override file must not error.
        r = self.c.post('/api/system/rotation', headers=AUTH, json={'mode': 'auto'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['mode'], 'auto')

    def test_off_then_auto_round_trip(self):
        self.c.post('/api/system/rotation', headers=AUTH, json={'mode': 'off'})
        self.assertTrue(self._override_path().exists())
        self.c.post('/api/system/rotation', headers=AUTH, json={'mode': 'auto'})
        self.assertFalse(self._override_path().exists())

    def test_invalid_mode_400(self):
        r = self.c.post('/api/system/rotation', headers=AUTH, json={'mode': 'on'})
        self.assertEqual(r.status_code, 400)
        # No override file created on the rejected path.
        self.assertFalse(self._override_path().exists())

    # ---- manual-pin (spec § 6.5): pinned_tier on POST ----

    def test_off_with_pinned_tier2_writes_contents_and_audits(self):
        r = self.c.post('/api/system/rotation', headers=AUTH,
                        json={'mode': 'off', 'pinned_tier': 'tier2'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['mode'], 'off')
        self.assertEqual(body['pinned_tier'], 'tier2')
        # The override file carries the pinned tier as its contents.
        self.assertEqual(self._override_path().read_text().strip(), 'tier2')
        # Audit payload records the pinned tier.
        row = self.client_stub.calls[0]['upsert_rows'][0]
        self.assertEqual(row['payload']['pinned_tier'], 'tier2')

    def test_off_default_pins_tier1_when_pinned_tier_omitted(self):
        # Back-compat: {mode:'off'} with no pinned_tier pins tier1.
        r = self.c.post('/api/system/rotation', headers=AUTH, json={'mode': 'off'})
        self.assertEqual(r.json()['pinned_tier'], 'tier1')
        self.assertEqual(self._override_path().read_text().strip(), 'tier1')

    def test_off_with_pinned_tier3_writes_contents_and_audits(self):
        # #765 widened ROTATION_VALID_TIERS to include tier3 (droplet became a
        # valid pin target), so {mode:'off',pinned_tier:'tier3'} now succeeds —
        # mirrors the tier2 case above.
        r = self.c.post('/api/system/rotation', headers=AUTH,
                        json={'mode': 'off', 'pinned_tier': 'tier3'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['mode'], 'off')
        self.assertEqual(body['pinned_tier'], 'tier3')
        self.assertEqual(self._override_path().read_text().strip(), 'tier3')
        row = self.client_stub.calls[0]['upsert_rows'][0]
        self.assertEqual(row['payload']['pinned_tier'], 'tier3')

    def test_off_invalid_pinned_tier_400_no_file(self):
        # A genuinely invalid tier (not in ROTATION_VALID_TIERS) is still
        # rejected 400 with no override file written — the guard is preserved.
        r = self.c.post('/api/system/rotation', headers=AUTH,
                        json={'mode': 'off', 'pinned_tier': 'tier4'})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self._override_path().exists())

    def test_auto_ignores_pinned_tier(self):
        self._override_path().write_text('tier2')
        r = self.c.post('/api/system/rotation', headers=AUTH,
                        json={'mode': 'auto', 'pinned_tier': 'tier2'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['mode'], 'auto')
        self.assertIsNone(body['pinned_tier'])
        self.assertFalse(self._override_path().exists())

    def test_pin_tier2_then_tier1_rewrites_contents(self):
        self.c.post('/api/system/rotation', headers=AUTH,
                    json={'mode': 'off', 'pinned_tier': 'tier2'})
        self.assertEqual(self._override_path().read_text().strip(), 'tier2')
        self.c.post('/api/system/rotation', headers=AUTH,
                    json={'mode': 'off', 'pinned_tier': 'tier1'})
        self.assertEqual(self._override_path().read_text().strip(), 'tier1')

    def test_missing_token_401(self):
        r = self.c.post('/api/system/rotation',
                        headers={'X-Actor': ALLOWED_ACTOR,
                                 'Content-Type': 'application/json'},
                        json={'mode': 'off'})
        self.assertEqual(r.status_code, 401)

    def test_missing_actor_401(self):
        r = self.c.post('/api/system/rotation',
                        headers={'X-Dashboard-Token': TOKEN,
                                 'Content-Type': 'application/json'},
                        json={'mode': 'off'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {'detail': 'unauthorized'})

    def test_bad_actor_401_no_leak(self):
        r = self.c.post('/api/system/rotation',
                        headers={'X-Dashboard-Token': TOKEN,
                                 'X-Actor': 'someone-else@gmail.com',
                                 'Content-Type': 'application/json'},
                        json={'mode': 'off'})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn('someone-else', r.text)

    def test_supabase_unavailable_503(self):
        da._get_larry_action_supabase_client = lambda: None  # type: ignore[assignment]
        r = self.c.post('/api/system/rotation', headers=AUTH, json={'mode': 'off'})
        self.assertEqual(r.status_code, 503)


# ---------- _read_rotation_config_enabled strict coercion (Audit #22) ----------

class ReadRotationConfigEnabledTest(unittest.TestCase):
    """The master kill switch must fail safe: rotation reads as enabled ONLY
    when ``rotation.enabled`` is the JSON boolean ``true``. Any other type —
    notably the quoted-bool typo {"enabled": "false"}, whose ``bool()`` is
    truthy — must collapse to False (disabled), matching the scheduler's
    rotate_active_tier._load_rotation_config after PR #368."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-rotcfg-'))
        self.models_path = self.tmp / 'agent-models.json'

    def _write(self, enabled_value: Any):
        # Embed the raw JSON for ``enabled`` so we can exercise non-bool types
        # (strings, ints, null) exactly as they'd appear on disk.
        self.models_path.write_text(
            '{"rotation": {"enabled": %s}}' % json.dumps(enabled_value))

    def test_real_bool_true_enables(self):
        self._write(True)
        self.assertTrue(da._read_rotation_config_enabled(self.models_path))

    def test_real_bool_false_disables(self):
        self._write(False)
        self.assertFalse(da._read_rotation_config_enabled(self.models_path))

    def test_quoted_false_string_disables(self):
        # The exact Audit #22 typo: bool("false") is True, must NOT enable.
        self._write('false')
        self.assertFalse(da._read_rotation_config_enabled(self.models_path))

    def test_quoted_true_string_disables(self):
        # A string is not the JSON boolean true → safe default (off).
        self._write('true')
        self.assertFalse(da._read_rotation_config_enabled(self.models_path))

    def test_int_one_disables(self):
        self._write(1)
        self.assertFalse(da._read_rotation_config_enabled(self.models_path))

    def test_missing_key_disables(self):
        self.models_path.write_text('{"rotation": {}}')
        self.assertFalse(da._read_rotation_config_enabled(self.models_path))

    def test_missing_block_disables(self):
        self.models_path.write_text('{}')
        self.assertFalse(da._read_rotation_config_enabled(self.models_path))

    def test_missing_file_disables(self):
        self.assertFalse(
            da._read_rotation_config_enabled(self.tmp / 'nope.json'))

    def test_malformed_json_disables(self):
        self.models_path.write_text('{not json')
        self.assertFalse(da._read_rotation_config_enabled(self.models_path))


if __name__ == '__main__':
    unittest.main()
