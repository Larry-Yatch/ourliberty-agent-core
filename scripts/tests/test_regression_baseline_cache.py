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

    def tearDown(self):
        if self._orig is None:
            os.environ.pop('OL_REGRESSION_BASELINE_DIR', None)
        else:
            os.environ['OL_REGRESSION_BASELINE_DIR'] = self._orig
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


if __name__ == '__main__':
    unittest.main()
