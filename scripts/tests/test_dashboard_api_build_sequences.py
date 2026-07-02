#!/usr/bin/env python3
"""Tests for the /api/system/build-sequences endpoint (PR-S3a).

Spec: agents/beacon/specs/build-sequence-orchestrator.md § 5.6 + § 5.8.

Path-isolation pattern mirrors test_dashboard_api_system.py: a synthetic
AGENTS_ROOT under a tmpdir; the FastAPI TestClient monkeypatches
`da._agents_root` directly onto a closure over the tmpdir so each test
case sees its own blackboard tree without touching `~/agents/`.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_dashboard_api_build_sequences
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
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Set the token BEFORE importing dashboard_api so the auth dependency
# resolves at request time. Sibling test modules pop this env var in
# their own tearDownModule; each TestCase re-sets in setUp to survive
# cross-module teardown ordering under `unittest discover`.
TOKEN = 'test-token-value'
os.environ.setdefault('DASHBOARD_API_TOKEN', TOKEN)

import dashboard_api as da  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {'X-Dashboard-Token': TOKEN}
ENDPOINT = '/api/system/build-sequences'


# ---------- fixture helpers ----------


def _make_seq(seq_id: str, status: str, **overrides) -> dict:
    """Build a minimal valid sequence-file dict.

    Field names match spec § 5.1 verbatim; tests assert pass-through, so
    drift here would surface as test failures alongside the real schema.
    """
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


def _write_seq(blackboard_root: Path, seq_id: str, status: str, **overrides) -> Path:
    blackboard_root.mkdir(parents=True, exist_ok=True)
    path = blackboard_root / f'{seq_id}.json'
    path.write_text(json.dumps(_make_seq(seq_id, status, **overrides), indent=2))
    return path


def _write_archived_seq(blackboard_root: Path, ym: str, seq_id: str, **overrides) -> Path:
    archive_dir = blackboard_root / '.archive' / ym
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f'{seq_id}.json'
    payload = _make_seq(seq_id, 'complete', **overrides)
    path.write_text(json.dumps(payload, indent=2))
    return path


def _client(testcase: unittest.TestCase, agents_root: Path) -> TestClient:
    """Build a TestClient with da._agents_root pointed at the test's tmpdir.

    Patched via ``mock.patch.object`` and restored via
    ``testcase.addCleanup`` so the original resolver is put back at the
    end of each test. A bare ``da._agents_root = lambda: ...`` with no
    restore leaks the (soon-deleted) tmpdir into every later-discovered
    module under ``unittest discover`` (e.g. heal_orphan_autoregister's
    queue-dir parity check would read the stale path instead of its env).
    """
    p = mock.patch.object(da, '_agents_root', lambda: agents_root)
    p.start()
    testcase.addCleanup(p.stop)
    return TestClient(da.app)


class _TokenSetMixin:
    """Re-set DASHBOARD_API_TOKEN in every setUp, mirroring
    test_dashboard_api_system._TokenSetMixin: another test module may
    pop the env var in its tearDownModule, and re-setting per-test is
    the simplest shape that survives any ordering."""

    def setUp(self):  # noqa: D401 — unittest hook
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        super().setUp()


class _SequenceTestBase(_TokenSetMixin, unittest.TestCase):
    """Common per-test scaffold. Each test owns a fresh tmpdir + agents
    root so cross-test state can't leak."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix='dash-bsq-'))
        self.agents_root = self.tmp / 'agents'
        self.agents_root.mkdir(parents=True, exist_ok=True)
        self.blackboard = self.agents_root / 'blackboard' / 'build-sequences'
        self.client = _client(self, self.agents_root)


# ==================== auth ====================


class AuthTest(_SequenceTestBase):
    def test_missing_token_returns_401(self):
        r = self.client.get(ENDPOINT)
        self.assertEqual(r.status_code, 401)

    def test_bad_token_returns_401(self):
        r = self.client.get(ENDPOINT, headers={'X-Dashboard-Token': 'nope'})
        self.assertEqual(r.status_code, 401)

    def test_correct_token_returns_200(self):
        r = self.client.get(ENDPOINT, headers=AUTH)
        self.assertEqual(r.status_code, 200)


# ==================== empty states ====================


class EmptyStateTest(_SequenceTestBase):
    def test_blackboard_dir_missing(self):
        # Don't create the dir at all.
        r = self.client.get(ENDPOINT, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['active'], [])
        self.assertEqual(body['archived'], [])
        self.assertEqual(body['parse_warnings'], [])
        self.assertIn('as_of', body)

    def test_blackboard_dir_empty(self):
        self.blackboard.mkdir(parents=True, exist_ok=True)
        r = self.client.get(ENDPOINT, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['active'], [])
        self.assertEqual(body['archived'], [])
        self.assertEqual(body['parse_warnings'], [])

    def test_archive_dir_present_but_empty(self):
        (self.blackboard / '.archive').mkdir(parents=True, exist_ok=True)
        r = self.client.get(ENDPOINT, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['active'], [])
        self.assertEqual(body['archived'], [])


# ==================== single-sequence shapes ====================


class SingleSequenceTest(_SequenceTestBase):
    def test_active_status_in_active_array(self):
        _write_seq(self.blackboard, 'seq-active-1', 'active')
        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body['active']), 1)
        self.assertEqual(body['archived'], [])
        self.assertEqual(body['active'][0]['seq_id'], 'seq-active-1')
        self.assertEqual(body['active'][0]['status'], 'active')

    def test_paused_status_in_active_array(self):
        _write_seq(self.blackboard, 'seq-paused-1', 'paused')
        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body['active']), 1)
        self.assertEqual(body['active'][0]['status'], 'paused')

    def test_pending_status_in_active_array(self):
        # Spec § 5.1 lists `pending` as a sequence-level status (created
        # but not started). The reader's conservative fallback puts it
        # in the active panel so it isn't dropped on the floor.
        _write_seq(self.blackboard, 'seq-pending-1', 'pending')
        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body['active']), 1)
        self.assertEqual(body['active'][0]['status'], 'pending')

    def test_complete_status_in_archived_array(self):
        # Until the 30d archiver lands, completed sequences sit in the
        # main dir with status=complete; the reader routes them to the
        # archived array per the 2026-05-27 CLARIFY contract.
        _write_seq(self.blackboard, 'seq-complete-1', 'complete')
        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(body['active'], [])
        self.assertEqual(len(body['archived']), 1)
        self.assertEqual(body['archived'][0]['status'], 'complete')

    def test_failed_status_in_archived_array(self):
        _write_seq(self.blackboard, 'seq-failed-1', 'failed')
        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body['archived']), 1)
        self.assertEqual(body['archived'][0]['status'], 'failed')

    def test_archived_subdir_file_in_archived_array(self):
        _write_archived_seq(self.blackboard, '2026-05', 'seq-arc-1')
        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(body['active'], [])
        self.assertEqual(len(body['archived']), 1)
        self.assertEqual(body['archived'][0]['seq_id'], 'seq-arc-1')


# ==================== multi-sequence layout ====================


class MultiSequenceTest(_SequenceTestBase):
    def test_three_active_five_archived(self):
        _write_seq(self.blackboard, 'a-1', 'active')
        _write_seq(self.blackboard, 'a-2', 'paused')
        _write_seq(self.blackboard, 'a-3', 'pending')
        _write_seq(self.blackboard, 'c-1', 'complete')
        _write_seq(self.blackboard, 'c-2', 'failed')
        _write_archived_seq(self.blackboard, '2026-04', 'arc-1')
        _write_archived_seq(self.blackboard, '2026-04', 'arc-2')
        _write_archived_seq(self.blackboard, '2026-05', 'arc-3')

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        active_ids = {s['seq_id'] for s in body['active']}
        archived_ids = {s['seq_id'] for s in body['archived']}
        self.assertEqual(active_ids, {'a-1', 'a-2', 'a-3'})
        self.assertEqual(archived_ids, {'c-1', 'c-2', 'arc-1', 'arc-2', 'arc-3'})
        # Same seq_id should never appear in both arrays (V1 invariant).
        self.assertEqual(active_ids & archived_ids, set())

    def test_full_sequence_dict_is_passed_through_verbatim(self):
        # Snapshot: verify every field on a written sequence comes back
        # unchanged. This is the contract PR-S3b consumes — silent
        # field-name churn here would break the UI on the next deploy.
        seq = _make_seq('shape-1', 'active')
        # Add a custom field that's not in the schema to confirm
        # pass-through is verbatim (no projection / no allowlist).
        seq['custom_metadata'] = {'note': 'arbitrary client metadata'}
        (self.blackboard).mkdir(parents=True, exist_ok=True)
        (self.blackboard / 'shape-1.json').write_text(json.dumps(seq))

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body['active']), 1)
        got = body['active'][0]
        # Every original key must survive the round-trip.
        for key in seq:
            self.assertIn(key, got, f'missing key after round-trip: {key}')
        # Including the custom field — proves no field projection.
        self.assertEqual(got['custom_metadata'], {'note': 'arbitrary client metadata'})


# ==================== uncached behaviour ====================


class UncachedBehaviourTest(_SequenceTestBase):
    def test_file_mutation_visible_on_next_request(self):
        # Spec § 5.6: page polls every 10s; the endpoint must re-read on
        # every request. No lru_cache, no TTL.
        path = _write_seq(self.blackboard, 'mut-1', 'active')

        body1 = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body1['active']), 1)
        self.assertEqual(len(body1['archived']), 0)

        # Flip status complete → moves to archived on next read.
        seq = json.loads(path.read_text())
        seq['status'] = 'complete'
        path.write_text(json.dumps(seq))

        body2 = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body2['active']), 0)
        self.assertEqual(len(body2['archived']), 1)
        self.assertEqual(body2['archived'][0]['seq_id'], 'mut-1')

    def test_new_file_visible_without_restart(self):
        body1 = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(body1['active'], [])
        _write_seq(self.blackboard, 'mut-2', 'active')
        body2 = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body2['active']), 1)


# ==================== corrupted / abnormal files ====================


class CorruptedFileTest(_SequenceTestBase):
    def test_invalid_json_active_dir_omitted_with_warning(self):
        _write_seq(self.blackboard, 'ok-1', 'active')
        self.blackboard.mkdir(parents=True, exist_ok=True)
        (self.blackboard / 'bad-1.json').write_text('{this is not json')

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        # Valid sequence still surfaces.
        self.assertEqual(len(body['active']), 1)
        self.assertEqual(body['active'][0]['seq_id'], 'ok-1')
        # Bad file is omitted from both arrays AND surfaced in warnings.
        self.assertEqual(len(body['parse_warnings']), 1)
        self.assertIn('bad-1.json', body['parse_warnings'][0])

    def test_invalid_json_archive_dir_omitted_with_warning(self):
        _write_archived_seq(self.blackboard, '2026-05', 'arc-ok')
        bad = self.blackboard / '.archive' / '2026-05' / 'bad-arc.json'
        bad.write_text('{nope')

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        archived_ids = {s['seq_id'] for s in body['archived']}
        self.assertEqual(archived_ids, {'arc-ok'})
        # Warning references the archive path so two `bad.json` files
        # in different YYYY-MM dirs are distinguishable.
        self.assertEqual(len(body['parse_warnings']), 1)
        self.assertIn('2026-05', body['parse_warnings'][0])
        self.assertIn('bad-arc.json', body['parse_warnings'][0])

    def test_top_level_array_omitted_with_warning(self):
        # JSON that parses but isn't a dict — defense against confusing
        # the {active, archived} shape with a sequence-as-array file.
        (self.blackboard).mkdir(parents=True, exist_ok=True)
        (self.blackboard / 'arr-1.json').write_text('[1, 2, 3]')

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(body['active'], [])
        self.assertEqual(body['archived'], [])
        self.assertEqual(len(body['parse_warnings']), 1)


# ==================== archive-layout discipline ====================


class ArchiveLayoutTest(_SequenceTestBase):
    def test_non_yyyy_mm_subdir_under_archive_ignored(self):
        # `.archive/scratch/` and `.archive/temp-2026/` should both be
        # ignored — only `YYYY-MM` subdirs are walked.
        _write_archived_seq(self.blackboard, '2026-05', 'good-1')
        scratch = self.blackboard / '.archive' / 'scratch'
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / 'looks-valid.json').write_text(
            json.dumps(_make_seq('should-not-appear', 'complete'))
        )
        temp = self.blackboard / '.archive' / 'temp-2026'
        temp.mkdir(parents=True, exist_ok=True)
        (temp / 'also.json').write_text(
            json.dumps(_make_seq('also-not', 'complete'))
        )

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        archived_ids = {s['seq_id'] for s in body['archived']}
        self.assertEqual(archived_ids, {'good-1'})

    def test_invalid_month_in_yyyy_mm_ignored(self):
        # `2026-13` and `2026-00` are not real months → ignored.
        for bad_month in ('2026-13', '2026-00', '2026-1'):
            d = self.blackboard / '.archive' / bad_month
            d.mkdir(parents=True, exist_ok=True)
            (d / 'x.json').write_text(
                json.dumps(_make_seq(f'x-{bad_month}', 'complete'))
            )

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(body['archived'], [])

    def test_hidden_file_in_main_dir_ignored(self):
        # `.gitkeep` from PR-S2 lives in the blackboard dir; the reader
        # must NOT trip over hidden files or descend into `.archive` as
        # a top-level file.
        self.blackboard.mkdir(parents=True, exist_ok=True)
        (self.blackboard / '.gitkeep').write_text('')
        (self.blackboard / '.hidden.json').write_text('garbage')
        _write_seq(self.blackboard, 'visible-1', 'active')

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body['active']), 1)
        self.assertEqual(body['active'][0]['seq_id'], 'visible-1')
        # Hidden files contribute no parse warnings.
        self.assertEqual(body['parse_warnings'], [])

    def test_non_json_file_in_main_dir_ignored(self):
        self.blackboard.mkdir(parents=True, exist_ok=True)
        (self.blackboard / 'README.md').write_text('not a sequence')
        _write_seq(self.blackboard, 'visible-2', 'active')

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body['active']), 1)
        self.assertEqual(body['parse_warnings'], [])


# ==================== security guards ====================


class SecurityGuardTest(_SequenceTestBase):
    def test_symlink_in_main_dir_skipped(self):
        # Defense against path-traversal: even though the blackboard dir
        # is hardcoded, a symlink to /etc/passwd is the canonical fuzz
        # pattern. The reader skips it; /etc/passwd is never read.
        self.blackboard.mkdir(parents=True, exist_ok=True)
        link = self.blackboard / 'evil.json'
        link.symlink_to('/etc/passwd')
        _write_seq(self.blackboard, 'real-1', 'active')

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(len(body['active']), 1)
        self.assertEqual(body['active'][0]['seq_id'], 'real-1')
        # No warning either — the symlink is skipped entirely.
        self.assertEqual(body['parse_warnings'], [])

    def test_symlink_in_archive_subdir_skipped(self):
        archive_dir = self.blackboard / '.archive' / '2026-05'
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / 'evil.json').symlink_to('/etc/passwd')
        _write_archived_seq(self.blackboard, '2026-05', 'real-arc')

        body = self.client.get(ENDPOINT, headers=AUTH).json()
        archived_ids = {s['seq_id'] for s in body['archived']}
        self.assertEqual(archived_ids, {'real-arc'})

    def test_response_does_not_echo_environment_variables(self):
        # Set a fake token-shaped env var and confirm it does NOT appear
        # anywhere in the response. The endpoint should expose ONLY the
        # contents of the sequence files, not server state.
        os.environ['DASHBOARD_API_TOKEN'] = TOKEN
        os.environ['SUPABASE_SERVICE_ROLE_KEY'] = 'eyJsupersecretrolekey'
        try:
            _write_seq(self.blackboard, 'pii-1', 'active')
            body = self.client.get(ENDPOINT, headers=AUTH).text
            self.assertNotIn(TOKEN, body)
            self.assertNotIn('eyJsupersecretrolekey', body)
            self.assertNotIn('SUPABASE_SERVICE_ROLE_KEY', body)
            self.assertNotIn('DASHBOARD_API_TOKEN', body)
        finally:
            os.environ.pop('SUPABASE_SERVICE_ROLE_KEY', None)


# ==================== response shape stability ====================


class ResponseShapeTest(_SequenceTestBase):
    def test_top_level_keys_are_stable(self):
        # PR-S3b consumes this contract; silently dropping or renaming a
        # top-level key would break the dashboard on the next deploy.
        _write_seq(self.blackboard, 'shape-1', 'active')
        body = self.client.get(ENDPOINT, headers=AUTH).json()
        self.assertEqual(
            sorted(body.keys()),
            ['active', 'archived', 'as_of', 'parse_warnings'],
        )
        self.assertIsInstance(body['active'], list)
        self.assertIsInstance(body['archived'], list)
        self.assertIsInstance(body['parse_warnings'], list)
        self.assertIsInstance(body['as_of'], str)


if __name__ == '__main__':
    unittest.main()
