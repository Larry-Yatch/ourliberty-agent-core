#!/usr/bin/env python3
"""Fixtures for `inbox_watcher._load_repo_paths` — task-30 refactor.

Verifies the new helper that resolves target_repo → canonical filesystem
Path via the top-level ``repo_paths`` block in ``config/agent-models.json``.
Uses a tmpdir-isolated agent-models.json and monkeypatches the module's
``_MODELS_CONFIG_PATH`` + cache sentinel between cases.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_inbox_watcher_repo_paths
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

import inbox_watcher  # noqa: E402


class LoadRepoPathsTest(unittest.TestCase):
    """`_load_repo_paths` reads the repo_paths block, validates it, caches."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='inbox-repo-paths-test-')
        self.cfg_path = Path(self.tmpdir) / 'agent-models.json'
        # Patch the module-level config path + reset cache.
        self._patches = [
            mock.patch.object(inbox_watcher, '_MODELS_CONFIG_PATH', self.cfg_path),
            mock.patch.object(inbox_watcher, '_REPO_PATHS_CACHE', None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        # Reset module cache for any later in-process callers.
        inbox_watcher._REPO_PATHS_CACHE = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_config(self, cfg: dict) -> None:
        self.cfg_path.write_text(json.dumps(cfg))

    def test_valid_block_returns_expected_dict(self):
        self._write_config({
            'repo_paths': {
                'ourliberty-agent-core': '/home/larry/agent-core',
                'ourliberty-dashboard': '/home/larry/ourliberty-dashboard',
            },
        })
        result = inbox_watcher._load_repo_paths()
        self.assertEqual(result, {
            'ourliberty-agent-core': Path('/home/larry/agent-core'),
            'ourliberty-dashboard': Path('/home/larry/ourliberty-dashboard'),
        })

    def test_missing_block_raises_runtime_error(self):
        self._write_config({'agents': {}, 'loop_bounds': {}})
        with self.assertRaises(RuntimeError) as ctx:
            inbox_watcher._load_repo_paths()
        self.assertIn("repo_paths", str(ctx.exception))

    def test_empty_block_raises_runtime_error(self):
        self._write_config({'repo_paths': {}})
        with self.assertRaises(RuntimeError) as ctx:
            inbox_watcher._load_repo_paths()
        self.assertIn("repo_paths", str(ctx.exception))

    def test_non_absolute_path_raises_runtime_error(self):
        self._write_config({
            'repo_paths': {'bad-repo': 'relative/path'},
        })
        with self.assertRaises(RuntimeError) as ctx:
            inbox_watcher._load_repo_paths()
        self.assertIn("bad-repo", str(ctx.exception))
        self.assertIn("/home/larry/", str(ctx.exception))

    def test_outside_home_larry_raises_runtime_error(self):
        self._write_config({
            'repo_paths': {'sneaky': '/etc/passwd'},
        })
        with self.assertRaises(RuntimeError) as ctx:
            inbox_watcher._load_repo_paths()
        self.assertIn("sneaky", str(ctx.exception))
        self.assertIn("/home/larry/", str(ctx.exception))

    def test_returns_path_objects_not_strings(self):
        self._write_config({
            'repo_paths': {'ourliberty-agent-core': '/home/larry/agent-core'},
        })
        result = inbox_watcher._load_repo_paths()
        for name, value in result.items():
            self.assertIsInstance(value, Path, f"{name} should be Path")
            self.assertNotIsInstance(value, str)

    def test_cache_honored_on_second_call(self):
        self._write_config({
            'repo_paths': {'ourliberty-agent-core': '/home/larry/agent-core'},
        })
        first = inbox_watcher._load_repo_paths()
        # Mutate file on disk; cached call should NOT re-read.
        self._write_config({
            'repo_paths': {
                'ourliberty-agent-core': '/home/larry/agent-core',
                'newer-repo': '/home/larry/newer',
            },
        })
        second = inbox_watcher._load_repo_paths()
        self.assertIs(first, second)
        self.assertNotIn('newer-repo', second)

    def test_malformed_json_raises_runtime_error(self):
        self.cfg_path.write_text('{not valid json')
        with self.assertRaises(RuntimeError) as ctx:
            inbox_watcher._load_repo_paths()
        self.assertIn("agent-models.json", str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
