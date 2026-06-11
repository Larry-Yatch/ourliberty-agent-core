#!/usr/bin/env python3
"""Tests for validate_deploy_targets (E2.1).

Covers schema enforcement, per-entry type/regex/enum checks, name
uniqueness, empty-deploy_targets regression, and the CLI entry point.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_validate_deploy_targets
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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

import validate_deploy_targets as v  # noqa: E402


def _good_entry(**overrides):
    entry = {
        'name': 'ourliberty-dashboard',
        'github_repo': 'Larry-Yatch/ourliberty-dashboard',
        'vercel_project_id': 'prj_abc123XYZ',
        'vercel_org_id': None,
        'framework': 'nextjs',
        'env_var_keys': ['NEXT_PUBLIC_API_BASE'],
        'branch_filter': None,
        'preview_enabled': True,
        'production_enabled': False,
        'created_at': '2026-05-20',
        'notes': '',
    }
    entry.update(overrides)
    return entry


def _good_registry(entries=None):
    return {
        '$schema_version': 1,
        '_doc': 'test registry',
        'deploy_targets': entries if entries is not None else [_good_entry()],
        'known_frameworks': [
            'nextjs', 'sveltekit', 'vite', 'astro', 'remix', 'other',
        ],
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
        reg['$schema_version'] = 99
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('$schema_version' in r for r in reasons))


class TopLevelShapeTest(unittest.TestCase):
    def test_empty_deploy_targets_is_valid(self):
        """Regression: initial-ship state has zero entries; that must validate."""
        reg = _good_registry(entries=[])
        ok, reasons = v.validate_registry(reg)
        self.assertTrue(ok, msg=reasons)

    def test_deploy_targets_not_a_list_fails(self):
        reg = _good_registry()
        reg['deploy_targets'] = {'not': 'a list'}
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('deploy_targets must be a list' in r for r in reasons))

    def test_missing_known_frameworks_fails(self):
        reg = _good_registry()
        del reg['known_frameworks']
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('known_frameworks' in r for r in reasons))

    def test_empty_known_frameworks_fails(self):
        reg = _good_registry()
        reg['known_frameworks'] = []
        ok, reasons = v.validate_registry(reg)
        self.assertFalse(ok)
        self.assertTrue(any('known_frameworks' in r for r in reasons))

    def test_non_object_payload_fails(self):
        ok, reasons = v.validate_registry(['not', 'a', 'dict'])
        self.assertFalse(ok)
        self.assertTrue(any('JSON object at the top level' in r for r in reasons))


class RequiredFieldsTest(unittest.TestCase):
    def test_missing_each_required_field_emits_distinct_reason(self):
        for field in v.REQUIRED_FIELDS:
            with self.subTest(field=field):
                entry = _good_entry()
                del entry[field]
                ok, reasons = v.validate_registry(_good_registry([entry]))
                self.assertFalse(ok)
                self.assertTrue(any(f"'{field}'" in r for r in reasons),
                                msg=f'expected missing-{field} reason; got {reasons}')


class NameTest(unittest.TestCase):
    def test_kebab_case_name_passes(self):
        ok, _ = v.validate_registry(_good_registry([_good_entry(name='foo-bar-baz')]))
        self.assertTrue(ok)

    def test_uppercase_name_fails(self):
        entry = _good_entry(name='Foo-Bar')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('kebab-case' in r for r in reasons))

    def test_underscore_name_fails(self):
        entry = _good_entry(name='foo_bar')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('kebab-case' in r for r in reasons))

    def test_duplicate_names_fail(self):
        e1 = _good_entry(name='same-name', vercel_project_id='prj_aaa')
        e2 = _good_entry(name='same-name', vercel_project_id='prj_bbb')
        ok, reasons = v.validate_registry(_good_registry([e1, e2]))
        self.assertFalse(ok)
        self.assertTrue(any('duplicate' in r.lower() for r in reasons))


class GithubRepoTest(unittest.TestCase):
    def test_owner_repo_passes(self):
        ok, _ = v.validate_registry(_good_registry(
            [_good_entry(github_repo='octocat/Hello-World')],
        ))
        self.assertTrue(ok)

    def test_bare_name_fails(self):
        entry = _good_entry(github_repo='just-a-name')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('github_repo' in r for r in reasons))


class VercelProjectIdTest(unittest.TestCase):
    def test_valid_prj_id_passes(self):
        entry = _good_entry(vercel_project_id='prj_AbCdEf123456')
        ok, _ = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok)

    def test_missing_prefix_fails(self):
        entry = _good_entry(vercel_project_id='abc123')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('vercel_project_id' in r for r in reasons))

    def test_wrong_prefix_fails(self):
        entry = _good_entry(vercel_project_id='team_abc123')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('vercel_project_id' in r for r in reasons))


class VercelOrgIdTest(unittest.TestCase):
    def test_null_org_id_passes(self):
        entry = _good_entry(vercel_org_id=None)
        ok, _ = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok)

    def test_valid_team_id_passes(self):
        entry = _good_entry(vercel_org_id='team_AbCdEf123')
        ok, _ = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok)

    def test_wrong_prefix_fails(self):
        entry = _good_entry(vercel_org_id='prj_abc123')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('vercel_org_id' in r for r in reasons))


class FrameworkTest(unittest.TestCase):
    def test_each_known_framework_passes(self):
        for fw in ('nextjs', 'sveltekit', 'vite', 'astro', 'remix', 'other'):
            with self.subTest(framework=fw):
                entry = _good_entry(framework=fw)
                ok, reasons = v.validate_registry(_good_registry([entry]))
                self.assertTrue(ok, msg=reasons)

    def test_unknown_framework_fails(self):
        entry = _good_entry(framework='gatsby')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('framework' in r for r in reasons))


class EnvVarKeysTest(unittest.TestCase):
    def test_empty_list_passes(self):
        # Allowed: a project may declare no env vars.
        entry = _good_entry(env_var_keys=[])
        ok, _ = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok)

    def test_non_string_element_fails(self):
        entry = _good_entry(env_var_keys=['OK_KEY', 42])
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('env_var_keys' in r for r in reasons))

    def test_non_list_fails(self):
        entry = _good_entry(env_var_keys='NOT_A_LIST')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('env_var_keys' in r for r in reasons))


class BranchFilterTest(unittest.TestCase):
    def test_null_passes(self):
        ok, _ = v.validate_registry(_good_registry([_good_entry(branch_filter=None)]))
        self.assertTrue(ok)

    def test_glob_string_passes(self):
        ok, _ = v.validate_registry(_good_registry([_good_entry(branch_filter='forge/*')]))
        self.assertTrue(ok)

    def test_empty_string_fails(self):
        entry = _good_entry(branch_filter='')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('branch_filter' in r for r in reasons))

    def test_non_string_fails(self):
        entry = _good_entry(branch_filter=123)
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('branch_filter' in r for r in reasons))


class BoolFieldsTest(unittest.TestCase):
    def test_preview_enabled_must_be_bool(self):
        entry = _good_entry(preview_enabled='true')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('preview_enabled' in r for r in reasons))

    def test_production_enabled_must_be_bool(self):
        entry = _good_entry(production_enabled=1)
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('production_enabled' in r for r in reasons))


class CreatedAtTest(unittest.TestCase):
    def test_valid_iso_date_passes(self):
        ok, _ = v.validate_registry(_good_registry([_good_entry(created_at='2026-01-01')]))
        self.assertTrue(ok)

    def test_invalid_date_fails(self):
        entry = _good_entry(created_at='January 1 2026')
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertFalse(ok)
        self.assertTrue(any('created_at' in r for r in reasons))


class HappyPathTest(unittest.TestCase):
    def test_full_valid_entry_passes_with_no_reasons(self):
        entry = _good_entry(
            vercel_org_id='team_abc123',
            branch_filter='forge/*',
            preview_enabled=True,
            production_enabled=True,
            env_var_keys=['NEXT_PUBLIC_API_BASE', 'API_KEY'],
            notes='production-on',
        )
        ok, reasons = v.validate_registry(_good_registry([entry]))
        self.assertTrue(ok)
        self.assertEqual(reasons, [])


class FileSourceTest(unittest.TestCase):
    def test_missing_file_fails(self):
        ok, reasons = v.validate_registry(Path('/tmp/nonexistent-xyz-deploy.json'))
        self.assertFalse(ok)
        self.assertTrue(any('does not exist' in r for r in reasons))

    def test_malformed_json_fails(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            f.write('{ not valid json')
            tmp = Path(f.name)
        try:
            ok, reasons = v.validate_registry(tmp)
            self.assertFalse(ok)
            self.assertTrue(any('JSON parse error' in r for r in reasons))
        finally:
            tmp.unlink()

    def test_path_string_accepted(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(_good_registry(), f)
            tmp = Path(f.name)
        try:
            ok, reasons = v.validate_registry(str(tmp))
            self.assertTrue(ok, msg=reasons)
        finally:
            tmp.unlink()

    def test_real_registry_validates(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        ok, reasons = v.validate_registry(repo_root / 'config' / 'deploy_targets.json')
        self.assertTrue(ok, msg=reasons)


class CliTest(unittest.TestCase):
    def test_main_returns_zero_on_valid(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(_good_registry(), f)
            tmp = Path(f.name)
        try:
            rc = v.main(['validate_deploy_targets.py', str(tmp)])
            self.assertEqual(rc, 0)
        finally:
            tmp.unlink()

    def test_main_returns_one_on_invalid(self):
        bad = _good_registry()
        bad['$schema_version'] = 99
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(bad, f)
            tmp = Path(f.name)
        try:
            with mock.patch('sys.stderr', new_callable=StringIO):
                rc = v.main(['validate_deploy_targets.py', str(tmp)])
            self.assertEqual(rc, 1)
        finally:
            tmp.unlink()


if __name__ == '__main__':
    unittest.main()
