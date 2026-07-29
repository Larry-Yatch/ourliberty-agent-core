#!/usr/bin/env python3
"""Tests for scripts/sync_desktop_config.py — the ~/.config/ourliberty sync.

Validates both directions, because a sync that silently does nothing is the
exact defect this script exists to prevent:
  - every source in config/desktop-config-sync.json really exists in the repo
    (a typo'd path would otherwise just never sync, forever)
  - --check DETECTS a stale, a missing, and an in-sync deployed file
  - apply fixes stale/missing and preserves the executable bit (a hook that
    lands 644 does not run)
  - unmanaged files in the destination are never touched — that directory also
    holds hand-authored local tools and the ingest-token secret
  - failures are LOUD: a missing source and an unwritable destination both
    exit non-zero rather than reporting success

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_sync_desktop_config
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import sync_desktop_config as sdc  # noqa: E402

_REPO_ROOT = _REPO_SCRIPTS.parent


class ManifestTests(unittest.TestCase):
    """config/desktop-config-sync.json is the sole source of truth."""

    def test_every_source_exists_in_the_repo(self):
        # A path typo'd here fails open: the file simply never syncs and the
        # deployed copy rots silently. Catch it at test time instead.
        _, sources = sdc.load_manifest()
        self.assertTrue(sources)
        for src in sources:
            with self.subTest(source=src):
                self.assertTrue((_REPO_ROOT / src).is_file(),
                                f'{src} listed in the manifest but not in the repo')

    def test_manifest_covers_the_deployed_capture_gesture(self):
        # These four are what ~/.config/ourliberty actually executes; dropping
        # one from the manifest silently re-opens the drift hole.
        _, sources = sdc.load_manifest()
        for expected in ('scripts/emit_capture.sh',
                         'scripts/emit_capture_impl.py',
                         'scripts/emit_desktop_session.sh',
                         'scripts/emit_desktop_session_impl.py'):
            self.assertIn(expected, sources)

    def test_dest_dir_is_expanded(self):
        env = {k: v for k, v in os.environ.items()}
        env.pop('OL_SYNC_DEST_DIR', None)
        with mock.patch.dict(os.environ, env, clear=True):
            dest, _ = sdc.load_manifest()
        self.assertTrue(dest.is_absolute(), dest)
        self.assertNotIn('~', str(dest))

    def test_empty_manifest_raises_rather_than_reporting_clean(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / 'empty.json'
            bad.write_text(json.dumps({'dest_dir': d, 'sources': []}),
                           encoding='utf-8')
            with self.assertRaises(ValueError):
                sdc.load_manifest(bad)


class _SyncCase(unittest.TestCase):
    """A throwaway dest dir + a real source path resolved from origin/main."""

    SOURCE = 'scripts/emit_capture.sh'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name)
        self.name = Path(self.SOURCE).name
        self.target = self.dest / self.name
        # What origin/main actually holds — the content the sync must converge on.
        self.blob, self.executable = sdc._blob(self.SOURCE)

    def _sync(self, **kw):
        return sdc.sync(self.dest, [self.SOURCE], **kw)


class CheckModeTests(_SyncCase):
    def test_check_flags_a_missing_file_and_writes_nothing(self):
        rc = self._sync(check=True)
        self.assertEqual(rc, 1)
        self.assertFalse(self.target.exists(), 'check mode must not write')

    def test_check_flags_a_stale_file_and_writes_nothing(self):
        self.target.write_bytes(b'#!/usr/bin/env bash\n# ancient hand-placed copy\n')
        before = self.target.read_bytes()
        rc = self._sync(check=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.target.read_bytes(), before)

    def test_check_passes_when_in_sync(self):
        self.target.write_bytes(self.blob)
        self.assertEqual(self._sync(check=True), 0)


class ApplyModeTests(_SyncCase):
    def test_installs_a_missing_file_executable(self):
        self.assertEqual(self._sync(), 0)
        self.assertEqual(self.target.read_bytes(), self.blob)
        # A hook or gesture that lands non-executable simply never runs.
        self.assertTrue(self.executable, 'fixture assumption: source is 100755')
        self.assertTrue(os.stat(self.target).st_mode & stat.S_IXUSR,
                        'deployed copy must keep its executable bit')

    def test_updates_a_stale_file(self):
        self.target.write_bytes(b'stale\n')
        self.assertEqual(self._sync(), 0)
        self.assertEqual(self.target.read_bytes(), self.blob)

    def test_is_idempotent_and_a_second_run_is_a_noop(self):
        self.assertEqual(self._sync(), 0)
        mtime = os.stat(self.target).st_mtime_ns
        self.assertEqual(self._sync(), 0)
        self.assertEqual(os.stat(self.target).st_mtime_ns, mtime,
                         'an in-sync file must not be rewritten')

    def test_never_touches_unmanaged_files(self):
        # ~/.config/ourliberty also holds the ingest token and hand-authored
        # local-only tools. Clobbering either would be far worse than drift.
        secret = self.dest / 'ingest-token'
        secret.write_bytes(b'super-secret\n')
        local_only = self.dest / 'pr-routing-gate.py'
        local_only.write_bytes(b'# hand-authored, no repo source\n')

        self.assertEqual(self._sync(), 0)

        self.assertEqual(secret.read_bytes(), b'super-secret\n')
        self.assertEqual(local_only.read_bytes(),
                         b'# hand-authored, no repo source\n')

    def test_leaves_no_temp_files_behind(self):
        self.assertEqual(self._sync(), 0)
        strays = [p.name for p in self.dest.iterdir()
                  if p.name.endswith('.sync-tmp')]
        self.assertEqual(strays, [])


class LoudFailureTests(_SyncCase):
    def test_missing_source_exits_nonzero(self):
        rc = sdc.sync(self.dest, ['scripts/does_not_exist_anywhere.sh'])
        self.assertEqual(rc, 2)

    def test_missing_source_fails_even_alongside_a_good_one(self):
        # The good file still syncs, but the run must NOT report success.
        rc = sdc.sync(self.dest, [self.SOURCE, 'scripts/nope.sh'])
        self.assertEqual(rc, 2)
        self.assertEqual(self.target.read_bytes(), self.blob)

    def test_unwritable_destination_exits_nonzero(self):
        os.chmod(self.dest, 0o500)  # r-x: cannot create the temp file
        self.addCleanup(os.chmod, self.dest, 0o700)
        self.assertEqual(self._sync(), 2)


class CliTests(unittest.TestCase):
    """The entrypoint the hook actually invokes."""

    SCRIPT = _REPO_SCRIPTS / 'sync_desktop_config.py'

    def _run(self, *args, dest: Path):
        env = dict(os.environ)
        env['OL_SYNC_DEST_DIR'] = str(dest)
        env['OL_SYNC_SKIP_FETCH'] = '1'  # no network in tests
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, timeout=120,
                              env=env)

    def test_check_then_apply_then_check_converges(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            drifted = self._run('--check', dest=dest)
            self.assertEqual(drifted.returncode, 1, drifted.stderr)

            applied = self._run(dest=dest)
            self.assertEqual(applied.returncode, 0, applied.stderr)

            clean = self._run('--check', dest=dest)
            self.assertEqual(clean.returncode, 0, clean.stderr)

    def test_apply_names_what_it_changed(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._run(dest=Path(d))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('emit_capture.sh', out.stdout)

    def test_quiet_when_already_in_sync(self):
        # This runs on every session start; an in-sync run must not spam.
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            self._run(dest=dest)
            second = self._run(dest=dest)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
