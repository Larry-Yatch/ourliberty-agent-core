#!/usr/bin/env python3
"""Tests for validate_token_rotation_schedule (E1.5.2).

Covers schema enforcement (schema_version, required fields), semantic rules
(rotation_type/cadence/date consistency, storage_location format, runbook
resolution, calendar URL prefix, duplicate names, severity enum), and the
CLI entry point.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_validate_token_rotation_schedule
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import copy
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import validate_token_rotation_schedule as v  # noqa: E402


def _good_entry(**overrides):
    entry = {
        'name': 'TEST_TOKEN',
        'storage_location': 'env_file:/home/larry/credentials/.env.larry',
        'credential_type': 'api_token',
        'purpose': 'Test purpose',
        'rotation_type': 'scheduled',
        'cadence_days': 365,
        'created_at': '2026-05-19',
        'last_rotated_at': '2026-05-19',
        'next_rotation_due': '2027-05-19',
        'calendar_event_url': None,
        'runbook_path': 'docs/runbooks/rotate-vercel-token.md',
        'severity_if_lapsed': 'high',
        'owner_role': 'larry',
        'scopes': ['full-account'],
        'notes': 'Test',
    }
    entry.update(overrides)
    return entry


def _good_registry(entries=None):
    return {
        '$schema_version': 1,
        'credentials': entries or [_good_entry()],
        'known_storage_locations': {
            'env_file:/home/larry/credentials/.env.larry': {
                'description': 'primary env file',
                'scanner_strategy': 'parse KEY=value lines',
            },
            'gh_cli:/home/larry/.config/gh/hosts.yml': {
                'description': 'gh CLI keychain',
                'scanner_strategy': 'gh auth status',
            },
            'claude_cli:/home/larry/.claude/.credentials.json': {
                'description': 'Claude OAuth',
                'scanner_strategy': 'JSON parse',
            },
            'workspace_mcp:/home/larry/.google_workspace_mcp/credentials/': {
                'description': 'workspace-mcp OAuth refresh tokens',
                'scanner_strategy': 'ls credentials/',
            },
        },
    }


class SchemaVersionTest(unittest.TestCase):
    def test_correct_schema_version_passes(self):
        ok, reasons = v.validate_registry(_good_registry())
        self.assertTrue(ok, msg=reasons)

    def test_missing_schema_version_fails(self):
        reg = _good_registry()
        del reg['$schema_version']
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('$schema_version' in r for r in reasons))

    def test_wrong_schema_version_fails(self):
        reg = _good_registry()
        reg['$schema_version'] = 999
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('$schema_version' in r for r in reasons))


class RequiredFieldsTest(unittest.TestCase):
    def test_all_required_fields_present_passes(self):
        ok, _ = v.validate_registry(_good_registry())
        self.assertTrue(ok)

    def test_missing_name_fails(self):
        entry = _good_entry()
        del entry['name']
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any("'name'" in r for r in reasons))

    def test_missing_scopes_fails(self):
        entry = _good_entry()
        del entry['scopes']
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any("'scopes'" in r for r in reasons))


class RotationTypeTest(unittest.TestCase):
    def test_scheduled_passes(self):
        ok, _ = v.validate_registry(_good_registry())
        self.assertTrue(ok)

    def test_scope_audit_passes(self):
        entry = _good_entry(rotation_type='scope_audit')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok, msg=reasons)

    def test_auto_refresh_passes(self):
        entry = _good_entry(rotation_type='auto_refresh')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok, msg=reasons)

    def test_revocation_only_requires_null_cadence(self):
        entry = _good_entry(
            rotation_type='revocation_only',
            cadence_days=365,
            next_rotation_due='2027-05-19',
        )
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('cadence_days=null' in r for r in reasons))

    def test_revocation_only_requires_null_next_rotation(self):
        entry = _good_entry(
            rotation_type='revocation_only',
            cadence_days=None,
            next_rotation_due='2027-05-19',
        )
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('next_rotation_due=null' in r for r in reasons))

    def test_revocation_only_with_nulls_passes(self):
        entry = _good_entry(
            rotation_type='revocation_only',
            cadence_days=None,
            next_rotation_due=None,
        )
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok, msg=reasons)

    def test_unknown_rotation_type_fails(self):
        entry = _good_entry(rotation_type='made-up')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('rotation_type=' in r for r in reasons))

    def test_scheduled_with_negative_cadence_fails(self):
        entry = _good_entry(cadence_days=-1)
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)

    def test_scheduled_with_non_int_cadence_fails(self):
        entry = _good_entry(cadence_days='365')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)

    def test_scheduled_with_boolean_cadence_fails(self):
        # Python bool is an int subclass — must explicitly reject.
        entry = _good_entry(cadence_days=True)
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)


class DateConsistencyTest(unittest.TestCase):
    def test_exact_anniversary_passes(self):
        entry = _good_entry(
            last_rotated_at='2026-01-15',
            next_rotation_due='2027-01-15',
            cadence_days=365,
        )
        ok, _ = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok)

    def test_off_by_one_day_passes_leap_tolerance(self):
        entry = _good_entry(
            last_rotated_at='2026-02-28',
            next_rotation_due='2027-03-01',
            cadence_days=365,
        )
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok, msg=reasons)

    def test_off_by_two_days_fails(self):
        entry = _good_entry(
            last_rotated_at='2026-05-19',
            next_rotation_due='2027-05-22',
            cadence_days=365,
        )
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('next_rotation_due' in r for r in reasons))

    def test_bad_date_format_fails(self):
        entry = _good_entry(last_rotated_at='May 19 2026')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('last_rotated_at' in r for r in reasons))


class StorageLocationTest(unittest.TestCase):
    def test_known_env_file_passes(self):
        ok, _ = v.validate_registry(_good_registry())
        self.assertTrue(ok)

    def test_unknown_prefix_fails(self):
        entry = _good_entry(storage_location='vault:/secrets/foo')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('storage_location' in r for r in reasons))

    def test_workspace_mcp_prefix_match_passes(self):
        # workspace_mcp is directory-based — exact match needn't be the dir
        # itself, can be a file inside.
        entry = _good_entry(
            storage_location=(
                'workspace_mcp:/home/larry/.google_workspace_mcp/'
                'credentials/agent.beacon.ourliberty@gmail.com.json'
            ),
        )
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok, msg=reasons)


class IgnoredKeysTest(unittest.TestCase):
    _ENV_LOC = 'env_file:/home/larry/credentials/.env.larry'

    def test_valid_ignored_keys_passes(self):
        reg = _good_registry()
        reg['known_storage_locations'][self._ENV_LOC]['ignored_keys'] = [
            'OURLIBERTY_NEWMISSION_INGEST_ENABLED',
        ]
        ok, reasons = v.validate_registry(reg)
        self.assertTrue(ok, msg=reasons)

    def test_empty_ignored_keys_passes(self):
        reg = _good_registry()
        reg['known_storage_locations'][self._ENV_LOC]['ignored_keys'] = []
        ok, reasons = v.validate_registry(reg)
        self.assertTrue(ok, msg=reasons)

    def test_ignored_keys_not_a_list_fails(self):
        reg = _good_registry()
        reg['known_storage_locations'][self._ENV_LOC]['ignored_keys'] = (
            'OURLIBERTY_NEWMISSION_INGEST_ENABLED'
        )
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('ignored_keys' in r for r in reasons))

    def test_ignored_keys_non_string_element_fails(self):
        reg = _good_registry()
        reg['known_storage_locations'][self._ENV_LOC]['ignored_keys'] = [
            'OK_KEY', 123,
        ]
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('ignored_keys' in r for r in reasons))

    def test_ignored_keys_empty_string_element_fails(self):
        reg = _good_registry()
        reg['known_storage_locations'][self._ENV_LOC]['ignored_keys'] = ['']
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('ignored_keys' in r for r in reasons))

    def test_ignored_keys_on_non_env_file_location_fails(self):
        reg = _good_registry()
        reg['known_storage_locations'][
            'gh_cli:/home/larry/.config/gh/hosts.yml'
        ]['ignored_keys'] = ['SOME_KEY']
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('ignored_keys' in r for r in reasons))


class CalendarUrlTest(unittest.TestCase):
    def test_null_calendar_url_passes(self):
        entry = _good_entry(calendar_event_url=None)
        ok, _ = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok)

    def test_valid_google_calendar_url_passes(self):
        entry = _good_entry(
            calendar_event_url='https://www.google.com/calendar/event?eid=abc',
        )
        ok, _ = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok)

    def test_non_google_url_fails(self):
        entry = _good_entry(
            calendar_event_url='https://outlook.com/calendar/event/abc',
        )
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('calendar_event_url' in r for r in reasons))


class SeverityTest(unittest.TestCase):
    def test_all_severities_pass(self):
        for sev in ('critical', 'high', 'medium', 'low'):
            entry = _good_entry(severity_if_lapsed=sev)
            ok, reasons = v.validate_registry(_good_registry([entry]))
            self.assertTrue(ok, msg=(sev, reasons))

    def test_unknown_severity_fails(self):
        entry = _good_entry(severity_if_lapsed='catastrophic')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)


class DuplicateNameTest(unittest.TestCase):
    def test_duplicate_names_fail(self):
        e1 = _good_entry(name='SAME')
        e2 = _good_entry(name='SAME')
        ok, reasons = v.validate_registry(_good_registry([e1, e2]))
        self.assertFalse(ok)
        self.assertTrue(any('duplicate' in r.lower() for r in reasons))


class RunbookResolutionTest(unittest.TestCase):
    def test_missing_runbook_fails(self):
        entry = _good_entry(runbook_path='docs/runbooks/does-not-exist.md')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('does not resolve' in r for r in reasons))

    def test_alt_repo_root_param(self):
        # When repo_root is overridden, resolution happens relative to it.
        with tempfile.TemporaryDirectory() as td:
            alt = Path(td)
            (alt / 'docs' / 'runbooks').mkdir(parents=True)
            (alt / 'docs' / 'runbooks' / 'custom.md').write_text('x')
            entry = _good_entry(runbook_path='docs/runbooks/custom.md')
            ok, reasons = v.validate_registry(
                _good_registry([entry]), repo_root=alt,
            )
            self.assertTrue(ok, msg=reasons)


class ScopesTest(unittest.TestCase):
    def test_empty_scopes_fails(self):
        entry = _good_entry(scopes=[])
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)

    def test_non_string_scope_fails(self):
        entry = _good_entry(scopes=['ok', 42])
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)


class FileSourceTest(unittest.TestCase):
    def test_missing_file_fails(self):
        ok, reasons = v.validate_registry(Path('/tmp/nonexistent-xyzzy-123.json'))
        self.assertFalse(ok)
        self.assertTrue(any('does not exist' in r for r in reasons))

    def test_malformed_json_fails(self):
        with tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False,
        ) as f:
            f.write('{ not valid json')
            tmp = Path(f.name)
        try:
            ok, reasons = v.validate_registry(tmp)
            self.assertFalse(ok)
            self.assertTrue(any('JSON parse error' in r for r in reasons))
        finally:
            tmp.unlink()

    def test_path_string_accepted(self):
        with tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False,
        ) as f:
            json.dump(_good_registry(), f)
            tmp = Path(f.name)
        try:
            ok, reasons = v.validate_registry(str(tmp))
            self.assertTrue(ok, msg=reasons)
        finally:
            tmp.unlink()


class CliTest(unittest.TestCase):
    def test_main_returns_zero_on_valid(self):
        with tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False,
        ) as f:
            json.dump(_good_registry(), f)
            tmp = Path(f.name)
        try:
            rc = v.main(['validate_token_rotation_schedule.py', str(tmp)])
            self.assertEqual(rc, 0)
        finally:
            tmp.unlink()

    def test_main_returns_one_on_invalid(self):
        bad = _good_registry()
        bad['$schema_version'] = 99
        with tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False,
        ) as f:
            json.dump(bad, f)
            tmp = Path(f.name)
        try:
            with mock.patch('sys.stderr', new_callable=StringIO):
                rc = v.main(['validate_token_rotation_schedule.py', str(tmp)])
            self.assertEqual(rc, 1)
        finally:
            tmp.unlink()


if __name__ == '__main__':
    unittest.main()
