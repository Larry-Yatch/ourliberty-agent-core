#!/usr/bin/env python3
"""Fixtures for `cleanup_stale_worktrees._load_canonical_repos` — task-30.

Verifies the new helper that returns the canonical repo Paths from the
top-level ``repo_paths`` block in ``config/agent-models.json``. Tests 1, 2,
5, 6 from the inbox_watcher test plan; the absolute-path / under-``/home/larry/``
validation rules share identical implementation across the two consumers and
are exercised in ``test_inbox_watcher_repo_paths``.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_cleanup_stale_worktrees_repo_paths
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import cleanup_stale_worktrees as csw  # noqa: E402


class LoadCanonicalReposTest(unittest.TestCase):
    """`_load_canonical_repos` reads repo_paths.values(), validates, caches."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cleanup-repo-paths-test-')
        self.cfg_path = Path(self.tmpdir) / 'agent-models.json'
        self._patches = [
            mock.patch.object(csw, '_MODELS_CONFIG_PATH', self.cfg_path),
            mock.patch.object(csw, '_REPO_PATHS_CACHE', None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        csw._REPO_PATHS_CACHE = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_config(self, cfg: dict) -> None:
        self.cfg_path.write_text(json.dumps(cfg))

    def test_valid_block_returns_expected_paths(self):
        self._write_config({
            'repo_paths': {
                'ourliberty-agent-core': '/home/larry/agent-core',
                'ourliberty-dashboard': '/home/larry/ourliberty-dashboard',
            },
        })
        result = csw._load_canonical_repos()
        self.assertEqual(
            sorted(result),
            sorted([
                Path('/home/larry/agent-core'),
                Path('/home/larry/ourliberty-dashboard'),
            ]),
        )

    def test_missing_block_raises_runtime_error(self):
        self._write_config({'agents': {}})
        with self.assertRaises(RuntimeError) as ctx:
            csw._load_canonical_repos()
        self.assertIn("repo_paths", str(ctx.exception))

    def test_returns_path_objects_not_strings(self):
        self._write_config({
            'repo_paths': {'ourliberty-agent-core': '/home/larry/agent-core'},
        })
        result = csw._load_canonical_repos()
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], Path)
        self.assertNotIsInstance(result[0], str)

    def test_cache_honored_on_second_call(self):
        self._write_config({
            'repo_paths': {'ourliberty-agent-core': '/home/larry/agent-core'},
        })
        first = csw._load_canonical_repos()
        # Mutate the file; cached call should NOT re-read.
        self._write_config({
            'repo_paths': {
                'ourliberty-agent-core': '/home/larry/agent-core',
                'newer-repo': '/home/larry/newer',
            },
        })
        second = csw._load_canonical_repos()
        self.assertIs(first, second)
        self.assertEqual(len(second), 1)


if __name__ == '__main__':
    unittest.main()
