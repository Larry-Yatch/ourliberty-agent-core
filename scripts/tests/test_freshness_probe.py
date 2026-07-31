#!/usr/bin/env python3
"""Tests for scripts/freshness_probe.py — the falsifiable-premise evaluator.

Uses unittest (repo convention; pytest isn't installed on the droplet).

Coverage (approvals-freshness slice 1):
  - The load-bearing tri-state contract: TRUE=keep, FALSE=moot, INDETERMINATE=keep.
  - Each in-scope kind (pr_state, file_contains/file_lacks, json_path, sql) parses
    and evaluates in both the holds (TRUE) and dead (FALSE) directions.
  - The two REAL worked examples from the spec (both 2026-07-29):
      * a file_contains probe over app/queue/verdict.ts for bulkCovered flips
        FALSE once the substring is gone (RSDPM #151 / 3d66718 made coverage
        ancestor-closed);
      * a sql probe over schema_migration_log flips FALSE once migration 0033 is
        live.
  - Every INDETERMINATE path: unknown kind, git/gh/sql failure, timeout,
    unparseable probe, missing required field — each returns INDETERMINATE (keep).

All side-effecting I/O is injected, so no live git/gh/DB is touched.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_freshness_probe
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import subprocess
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import freshness_probe as fp  # noqa: E402
import task_terminal_state as tts  # noqa: E402


# -------------------- helpers --------------------

def _git_returning(text):
    """A git_show stub that always yields `text` (or None for a git failure)."""
    return lambda repo, ref, path: text


def _git_raising(exc):
    def _raise(repo, ref, path):
        raise exc
    return _raise


def _sql_returning(result):
    return lambda query, dsn=None: result


def _json_returning(data):
    return lambda path: data


# -------------------- tri-state constants / contract --------------------

class TriStateContractTests(unittest.TestCase):
    def test_three_distinct_values(self):
        self.assertEqual(len({fp.TRUE, fp.FALSE, fp.INDETERMINATE}), 3)

    def test_keep_states_are_true_and_indeterminate_not_false(self):
        # The whole safety property: everything that is NOT FALSE is KEEP.
        self.assertIn(fp.TRUE, fp.KEEP_STATES)
        self.assertIn(fp.INDETERMINATE, fp.KEEP_STATES)
        self.assertNotIn(fp.FALSE, fp.KEEP_STATES)

    def test_supported_kinds_exclude_shell(self):
        self.assertNotIn('shell', fp.SUPPORTED_KINDS)
        self.assertEqual(
            fp.SUPPORTED_KINDS,
            frozenset({'pr_state', 'file_contains', 'file_lacks',
                       'json_path', 'sql'}),
        )


# -------------------- pr_state kind (reuses task_terminal_state) --------------------

class PrStateKindTests(unittest.TestCase):
    def _probe(self, **kw):
        p = {'kind': 'pr_state', 'task_id': 'some-real-task-id-001'}
        p.update(kw)
        return p

    def test_open_holds_true_expect_open(self):
        v = fp.evaluate(self._probe(), pr_state_probe=lambda tid: tts.OPEN)
        self.assertEqual(v, fp.TRUE)

    def test_merged_is_false_expect_open(self):
        v = fp.evaluate(self._probe(), pr_state_probe=lambda tid: tts.MERGED)
        self.assertEqual(v, fp.FALSE)

    def test_closed_is_false_expect_open(self):
        v = fp.evaluate(self._probe(), pr_state_probe=lambda tid: tts.CLOSED)
        self.assertEqual(v, fp.FALSE)

    def test_unknown_is_indeterminate(self):
        # A gh failure collapses to UNKNOWN -> INDETERMINATE (keep), never a verdict.
        v = fp.evaluate(self._probe(), pr_state_probe=lambda tid: tts.UNKNOWN)
        self.assertEqual(v, fp.INDETERMINATE)

    def test_expect_terminal_inverts(self):
        # Premise "not yet shipped": FALSE once terminal, TRUE while open.
        merged = fp.evaluate(self._probe(expect='terminal'),
                             pr_state_probe=lambda tid: tts.MERGED)
        openv = fp.evaluate(self._probe(expect='terminal'),
                            pr_state_probe=lambda tid: tts.OPEN)
        self.assertEqual(merged, fp.FALSE)
        self.assertEqual(openv, fp.TRUE)

    def test_bad_expect_is_indeterminate(self):
        v = fp.evaluate(self._probe(expect='sideways'),
                        pr_state_probe=lambda tid: tts.OPEN)
        self.assertEqual(v, fp.INDETERMINATE)

    def test_missing_task_id_is_indeterminate(self):
        v = fp.evaluate({'kind': 'pr_state'}, pr_state_probe=lambda tid: tts.OPEN)
        self.assertEqual(v, fp.INDETERMINATE)

    def test_probe_raising_is_indeterminate(self):
        def _boom(tid):
            raise RuntimeError('gh exploded')
        v = fp.evaluate(self._probe(), pr_state_probe=_boom)
        self.assertEqual(v, fp.INDETERMINATE)


# -------------------- file_contains / file_lacks kinds --------------------

class FileSubstringKindTests(unittest.TestCase):
    def test_file_contains_present_is_true(self):
        probe = {'kind': 'file_contains', 'repo': 'RSDPM',
                 'path': 'app/queue/verdict.ts', 'substring': 'bulkCovered'}
        v = fp.evaluate(probe, git_show=_git_returning('if (bulkCovered) {...}'))
        self.assertEqual(v, fp.TRUE)

    def test_file_contains_absent_is_false(self):
        probe = {'kind': 'file_contains', 'repo': 'RSDPM',
                 'path': 'app/queue/verdict.ts', 'substring': 'bulkCovered'}
        v = fp.evaluate(probe, git_show=_git_returning('ancestor-closed coverage'))
        self.assertEqual(v, fp.FALSE)

    def test_file_lacks_absent_is_true(self):
        probe = {'kind': 'file_lacks', 'repo': 'RSDPM',
                 'path': 'app/queue/verdict.ts', 'substring': 'bulkCovered'}
        v = fp.evaluate(probe, git_show=_git_returning('ancestor-closed coverage'))
        self.assertEqual(v, fp.TRUE)

    def test_file_lacks_present_is_false(self):
        probe = {'kind': 'file_lacks', 'repo': 'RSDPM',
                 'path': 'app/queue/verdict.ts', 'substring': 'bulkCovered'}
        v = fp.evaluate(probe, git_show=_git_returning('if (bulkCovered) {...}'))
        self.assertEqual(v, fp.FALSE)

    def test_git_failure_is_indeterminate(self):
        probe = {'kind': 'file_contains', 'repo': 'RSDPM',
                 'path': 'x.ts', 'substring': 'foo'}
        v = fp.evaluate(probe, git_show=_git_returning(None))
        self.assertEqual(v, fp.INDETERMINATE)

    def test_git_raising_is_indeterminate(self):
        probe = {'kind': 'file_contains', 'repo': 'RSDPM',
                 'path': 'x.ts', 'substring': 'foo'}
        v = fp.evaluate(probe, git_show=_git_raising(OSError('no such repo')))
        self.assertEqual(v, fp.INDETERMINATE)

    def test_missing_substring_is_indeterminate(self):
        probe = {'kind': 'file_contains', 'repo': 'RSDPM', 'path': 'x.ts'}
        v = fp.evaluate(probe, git_show=_git_returning('anything'))
        self.assertEqual(v, fp.INDETERMINATE)

    def test_default_ref_is_origin_main(self):
        seen = {}

        def _spy(repo, ref, path):
            seen['ref'] = ref
            return 'bulkCovered'
        probe = {'kind': 'file_contains', 'repo': 'RSDPM',
                 'path': 'app/queue/verdict.ts', 'substring': 'bulkCovered'}
        fp.evaluate(probe, git_show=_spy)
        self.assertEqual(seen['ref'], 'origin/main')


# -------------------- json_path kind --------------------

class JsonPathKindTests(unittest.TestCase):
    def test_value_equals_expected_is_true(self):
        probe = {'kind': 'json_path', 'path': '/cfg.json',
                 'key': 'a.b', 'expected': 10}
        v = fp.evaluate(probe, read_json=_json_returning({'a': {'b': 10}}))
        self.assertEqual(v, fp.TRUE)

    def test_value_moved_on_is_false(self):
        probe = {'kind': 'json_path', 'path': '/cfg.json',
                 'key': 'a.b', 'expected': 10}
        v = fp.evaluate(probe, read_json=_json_returning({'a': {'b': 33}}))
        self.assertEqual(v, fp.FALSE)

    def test_expected_may_be_falsy(self):
        # `expected` presence (not truthiness) is the required-field check.
        probe = {'kind': 'json_path', 'path': '/cfg.json',
                 'key': 'flag', 'expected': False}
        v = fp.evaluate(probe, read_json=_json_returning({'flag': False}))
        self.assertEqual(v, fp.TRUE)

    def test_missing_expected_field_is_indeterminate(self):
        probe = {'kind': 'json_path', 'path': '/cfg.json', 'key': 'a'}
        v = fp.evaluate(probe, read_json=_json_returning({'a': 1}))
        self.assertEqual(v, fp.INDETERMINATE)

    def test_key_absent_is_indeterminate_not_false(self):
        probe = {'kind': 'json_path', 'path': '/cfg.json',
                 'key': 'a.missing', 'expected': 1}
        v = fp.evaluate(probe, read_json=_json_returning({'a': {'b': 1}}))
        self.assertEqual(v, fp.INDETERMINATE)

    def test_unparseable_config_is_indeterminate(self):
        probe = {'kind': 'json_path', 'path': '/cfg.json',
                 'key': 'a', 'expected': 1}
        v = fp.evaluate(probe, read_json=_json_returning(None))
        self.assertEqual(v, fp.INDETERMINATE)


# -------------------- sql kind --------------------

class SqlKindTests(unittest.TestCase):
    def test_truthy_result_is_true(self):
        probe = {'kind': 'sql', 'query': 'SELECT NOT EXISTS (...)'}
        v = fp.evaluate(probe, sql_bool=_sql_returning(True))
        self.assertEqual(v, fp.TRUE)

    def test_falsey_result_is_false(self):
        probe = {'kind': 'sql', 'query': 'SELECT NOT EXISTS (...)'}
        v = fp.evaluate(probe, sql_bool=_sql_returning(False))
        self.assertEqual(v, fp.FALSE)

    def test_none_result_is_indeterminate(self):
        # The default (inert) executor returns None; an unbound sql probe keeps.
        probe = {'kind': 'sql', 'query': 'SELECT 1'}
        v = fp.evaluate(probe)  # default inert executor
        self.assertEqual(v, fp.INDETERMINATE)

    def test_sql_raising_is_indeterminate(self):
        def _boom(query, dsn=None):
            raise RuntimeError('connection refused')
        probe = {'kind': 'sql', 'query': 'SELECT 1'}
        v = fp.evaluate(probe, sql_bool=_boom)
        self.assertEqual(v, fp.INDETERMINATE)

    def test_non_boolean_shape_is_indeterminate(self):
        probe = {'kind': 'sql', 'query': 'SELECT 5'}
        v = fp.evaluate(probe, sql_bool=_sql_returning(5))
        self.assertEqual(v, fp.INDETERMINATE)

    def test_missing_query_is_indeterminate(self):
        v = fp.evaluate({'kind': 'sql'}, sql_bool=_sql_returning(True))
        self.assertEqual(v, fp.INDETERMINATE)


# -------------------- top-level INDETERMINATE paths --------------------

class IndeterminatePathTests(unittest.TestCase):
    def test_unknown_kind_is_indeterminate(self):
        self.assertEqual(fp.evaluate({'kind': 'shell', 'cmd': 'ls'}),
                         fp.INDETERMINATE)

    def test_missing_kind_is_indeterminate(self):
        self.assertEqual(fp.evaluate({'task_id': 'x'}), fp.INDETERMINATE)

    def test_non_dict_probe_is_indeterminate(self):
        for bad in (None, 'a string', 42, ['kind', 'sql'], object()):
            self.assertEqual(fp.evaluate(bad), fp.INDETERMINATE)

    def test_evaluate_never_raises(self):
        # A dict that trips an inner path must still return, not raise.
        self.assertEqual(fp.evaluate({}), fp.INDETERMINATE)


# -------------------- the two REAL worked examples (2026-07-29) --------------------

class WorkedExamplesTests(unittest.TestCase):
    """Both premises were TRUE when the card was filed and flipped FALSE when the
    real-world fact changed — the exact staleness a freshness_probe must catch."""

    def test_verdict_ts_bulkcovered_flips_false(self):
        # A file_contains probe over app/queue/verdict.ts at origin/main for the
        # CONFIRM_ALL_TIERS / bulkCovered coverage check. RSDPM #151 (3d66718) made
        # coverage ancestor-closed, deleting the bulkCovered branch.
        probe = {
            'kind': 'file_contains',
            'repo': 'RSDPM',
            'path': 'app/queue/verdict.ts',
            'ref': 'origin/main',
            'substring': 'bulkCovered',
        }
        # BEFORE #151 merged: the substring is present -> premise holds -> keep.
        before = fp.evaluate(
            probe, git_show=_git_returning(
                'export function verdict() {\n  if (bulkCovered) return CONFIRM_ALL_TIERS;\n}'))
        self.assertEqual(before, fp.TRUE)
        # AFTER 3d66718: coverage is ancestor-closed, bulkCovered is gone ->
        # premise dead -> the ask is moot.
        after = fp.evaluate(
            probe, git_show=_git_returning(
                'export function verdict() {\n  return ancestorClosedCoverage();\n}'))
        self.assertEqual(after, fp.FALSE)

    def test_schema_migration_log_0033_flips_false(self):
        # A sql probe asking "is migration 0033 NOT yet applied?" over
        # schema_migration_log. The premise (0033 pending) holds until 0033 lands.
        probe = {
            'kind': 'sql',
            'query': ("SELECT NOT EXISTS (SELECT 1 FROM schema_migration_log "
                      "WHERE version = '0033')"),
        }
        # BEFORE 0033 is live: NOT EXISTS -> True -> premise holds -> keep.
        before = fp.evaluate(probe, sql_bool=_sql_returning(True))
        self.assertEqual(before, fp.TRUE)
        # AFTER 0033 is live: the row exists, NOT EXISTS -> False -> premise dead.
        after = fp.evaluate(probe, sql_bool=_sql_returning(False))
        self.assertEqual(after, fp.FALSE)


# -------------------- default executor smoke (fail-toward-INDETERMINATE) --------------------

class DefaultExecutorTests(unittest.TestCase):
    def test_default_git_show_on_bad_repo_returns_none(self):
        # A nonexistent repo dir must yield None (never raise), so the evaluator
        # maps it to INDETERMINATE.
        out = fp._default_git_show('/nonexistent/repo/xyz', 'origin/main', 'a.ts')
        self.assertIsNone(out)

    def test_default_git_timeout_returns_none(self):
        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd='git', timeout=1)
        import unittest.mock as mock
        with mock.patch('freshness_probe.subprocess.run', _timeout):
            out = fp._default_git_show('/repo', 'origin/main', 'a.ts')
        self.assertIsNone(out)

    def test_default_read_json_missing_file_returns_none(self):
        self.assertIsNone(fp._default_read_json('/nonexistent/cfg-xyz.json'))

    def test_default_sql_is_inert_none(self):
        self.assertIsNone(fp._default_sql_bool('SELECT 1'))


if __name__ == '__main__':
    unittest.main()
