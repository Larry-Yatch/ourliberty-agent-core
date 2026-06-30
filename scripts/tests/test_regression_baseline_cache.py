#!/usr/bin/env python3
"""Tests for regression_baseline_cache — the per-SHA baseline cache that lets
the review regression gate skip re-running the parent suite.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_regression_baseline_cache
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

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import regression_baseline_cache as c  # noqa: E402

SHA_A = 'a' * 40
SHA_B = 'b' * 40


class BaselineCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='regbaseline-test-')
        self._orig = os.environ.get('OL_REGRESSION_BASELINE_DIR')
        os.environ['OL_REGRESSION_BASELINE_DIR'] = self._tmp
        # Isolate the single-flight host lock from the live warmer and from
        # other tests (this suite also runs inside the gate's discover pass).
        self._orig_lock = os.environ.get('OL_REGBASELINE_LOCK_PATH')
        os.environ['OL_REGBASELINE_LOCK_PATH'] = str(
            Path(self._tmp) / 'warm.lock')
        # warm() exports REGBASELINE_WARMING=1 around its discover pass and
        # build_sandbox_env copies os.environ into the jailed suite, so this
        # suite can itself run with the flag set inside a host warm. The
        # re-entrancy guard would then short-circuit the warm-executing tests
        # (phantom baseline failures). Clear it so they exercise the real path.
        self._saved_warming = os.environ.pop('REGBASELINE_WARMING', None)

    def tearDown(self):
        if self._saved_warming is not None:
            os.environ['REGBASELINE_WARMING'] = self._saved_warming
        if self._orig is None:
            os.environ.pop('OL_REGRESSION_BASELINE_DIR', None)
        else:
            os.environ['OL_REGRESSION_BASELINE_DIR'] = self._orig
        if self._orig_lock is None:
            os.environ.pop('OL_REGBASELINE_LOCK_PATH', None)
        else:
            os.environ['OL_REGBASELINE_LOCK_PATH'] = self._orig_lock
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_store_load_roundtrip(self):
        failing = {'scripts.tests.test_x.T.test_a', 'scripts.tests.test_y.T.test_b'}
        p = c.store(SHA_A, failing)
        self.assertIsNotNone(p)
        self.assertEqual(c.load(SHA_A), failing)

    def test_load_miss_when_absent(self):
        self.assertIsNone(c.load(SHA_A))

    def test_load_miss_for_different_sha(self):
        c.store(SHA_A, {'t'})
        self.assertIsNone(c.load(SHA_B))

    def test_load_rejects_inner_sha_mismatch(self):
        # A file whose key SHA and inner sha disagree must be treated as a miss
        # (defensive against a corrupted/renamed cache file).
        p = Path(self._tmp) / f'{SHA_A}.json'
        p.write_text(json.dumps(
            {'schema': c.SCHEMA_VERSION, 'sha': SHA_B, 'failing_tests': ['t']}
        ))
        self.assertIsNone(c.load(SHA_A))

    def test_load_rejects_wrong_schema(self):
        p = Path(self._tmp) / f'{SHA_A}.json'
        p.write_text(json.dumps(
            {'schema': 999, 'sha': SHA_A, 'failing_tests': ['t']}
        ))
        self.assertIsNone(c.load(SHA_A))

    def test_load_rejects_malformed_json(self):
        p = Path(self._tmp) / f'{SHA_A}.json'
        p.write_text('{not valid json')
        self.assertIsNone(c.load(SHA_A))

    def test_store_rejects_non_canonical_sha(self):
        # Never cache an abbreviated/symbolic ref — the key must be exact.
        self.assertIsNone(c.store('HEAD', {'t'}))
        self.assertIsNone(c.store('abcdef', {'t'}))
        self.assertIsNone(c.store('A' * 40, {'t'}))  # uppercase not canonical
        self.assertEqual(list(Path(self._tmp).glob('*.json')), [])

    def test_load_rejects_non_canonical_sha(self):
        self.assertIsNone(c.load('HEAD'))
        self.assertIsNone(c.load(''))

    def test_empty_failing_set_roundtrips(self):
        # A clean commit (no failures) is a valid, cacheable baseline — distinct
        # from a cache miss.
        c.store(SHA_A, set())
        self.assertEqual(c.load(SHA_A), set())

    def test_gc_keeps_newest(self):
        import time
        shas = [f'{i:040x}' for i in range(5)]
        for s in shas:
            c.store(s, {'t'})
            time.sleep(0.01)  # stagger mtimes so "newest" is well-defined
        removed = c.gc(keep=2)
        self.assertEqual(removed, 3)
        remaining = {p.stem for p in Path(self._tmp).glob('*.json')}
        self.assertEqual(remaining, set(shas[-2:]))  # the 2 most recent

    def test_gc_noop_under_keep(self):
        c.store(SHA_A, {'t'})
        self.assertEqual(c.gc(keep=10), 0)
        self.assertIsNotNone(c.load(SHA_A))

    def test_baseline_dir_ignores_sandbox_redirect(self):
        # The most load-bearing claim: the cache lands on the REAL tree, never
        # the gate's per-run sandbox. With the explicit override removed,
        # baseline_dir() must NOT incorporate OURLIBERTY_AGENTS_ROOT.
        sentinel = '/tmp/SANDBOX-SENTINEL-must-not-appear'
        saved_override = os.environ.pop('OL_REGRESSION_BASELINE_DIR', None)
        saved_sandbox = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = sentinel
        try:
            d = str(c.baseline_dir())
            self.assertNotIn(sentinel, d)
            self.assertTrue(d.endswith('regression-baselines'))
        finally:
            if saved_override is not None:
                os.environ['OL_REGRESSION_BASELINE_DIR'] = saved_override
            if saved_sandbox is None:
                os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
            else:
                os.environ['OURLIBERTY_AGENTS_ROOT'] = saved_sandbox

    def test_warm_short_circuits_when_already_cached(self):
        from unittest import mock
        c.store(SHA_A, {'t'})
        with mock.patch('test_regression_check.resolve_sha', return_value=SHA_A), \
             mock.patch('test_regression_check.collect_failures_at_sha') as collect:
            rc = c.warm(Path('/tmp/fake-repo'), SHA_A, 900)
        self.assertEqual(rc, 0)
        collect.assert_not_called()  # already cached → no full-suite run

    def test_warm_runs_and_caches_on_miss(self):
        from unittest import mock
        with mock.patch('test_regression_check.resolve_sha', return_value=SHA_B), \
             mock.patch('test_regression_check.collect_failures_at_sha',
                        return_value={'x.Y.test_z'}) as collect:
            rc = c.warm(Path('/tmp/fake-repo'), SHA_B, 900)
        self.assertEqual(rc, 0)
        collect.assert_called_once()
        self.assertEqual(c.load(SHA_B), {'x.Y.test_z'})

    def test_warm_refuses_to_nest_when_already_warming(self):
        from unittest import mock
        saved = os.environ.get('REGBASELINE_WARMING')
        os.environ['REGBASELINE_WARMING'] = '1'
        try:
            with mock.patch('test_regression_check.resolve_sha',
                            return_value=SHA_B), \
                 mock.patch('test_regression_check.collect_failures_at_sha') \
                    as collect:
                rc = c.warm(Path('/tmp/fake-repo'), SHA_B, 900)
            self.assertEqual(rc, 0)
            collect.assert_not_called()  # re-entrancy guard -> no nested suite
        finally:
            if saved is None:
                os.environ.pop('REGBASELINE_WARMING', None)
            else:
                os.environ['REGBASELINE_WARMING'] = saved

    def test_warm_single_flights_when_host_lock_held(self):
        import fcntl
        from unittest import mock
        lock_path = Path(os.environ['OL_REGBASELINE_LOCK_PATH'])
        held = open(lock_path, 'w')
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch('test_regression_check.resolve_sha',
                            return_value=SHA_B), \
                 mock.patch.object(c, 'load', return_value=None), \
                 mock.patch('test_regression_check.collect_failures_at_sha') \
                    as collect:
                rc = c.warm(Path('/tmp/fake-repo'), SHA_B, 900)
            self.assertEqual(rc, 0)
            collect.assert_not_called()  # lock held -> single-flight skip
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()


if __name__ == '__main__':
    unittest.main()
