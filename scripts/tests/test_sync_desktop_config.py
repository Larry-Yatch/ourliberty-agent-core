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

    def test_duplicate_basenames_are_refused(self):
        # Destinations are keyed on the basename. Two sources sharing one used
        # to collide silently: apply rewrote the file every run (last writer
        # wins) and still exited 0, while --check reported drift forever.
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / 'collide.json'
            bad.write_text(json.dumps({
                'dest_dir': d,
                'sources': ['scripts/emit_capture.sh',
                            'hooks/emit_capture.sh'],
            }), encoding='utf-8')
            with self.assertRaises(ValueError) as ctx:
                sdc.load_manifest(bad)
        msg = str(ctx.exception)
        self.assertIn('emit_capture.sh', msg)
        self.assertIn('hooks/emit_capture.sh', msg)

    def test_trailing_slash_directory_source_is_refused(self):
        # The dangerous spelling of a directory: `git show <ref>:scripts/`
        # SUCCEEDS with a tree listing and `git ls-tree <ref> scripts/` lists
        # the children, so the first child's 100755 used to pass the mode check
        # and the listing text landed as an executable file, exit 0.
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / 'dir.json'
            bad.write_text(json.dumps({'dest_dir': d, 'sources': ['scripts/']}),
                           encoding='utf-8')
            with self.assertRaises(ValueError) as ctx:
                sdc.load_manifest(bad)
        self.assertIn('directory', str(ctx.exception))

    def test_shipped_manifest_names_only_regular_files(self):
        _, sources = sdc.load_manifest()
        for src in sources:
            with self.subTest(source=src):
                self.assertFalse(src.endswith('/'))
                self.assertTrue((_REPO_ROOT / src).is_file())

    def test_shipped_manifest_has_no_basename_collision(self):
        _, sources = sdc.load_manifest()  # raises if it ever gains one
        names = [Path(s).name for s in sources]
        self.assertEqual(len(names), len(set(names)))


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


class ModeParsingTests(unittest.TestCase):
    """The executable bit decides whether a deployed hook runs at all.

    settings.json invokes these files directly (`/Users/.../emit_desktop_session.sh
    desktop_session_start`), not via `bash <file>`, so landing one 0644 breaks
    it with permission-denied on every session start. Anything unexpected in
    the ls-tree output must therefore raise, never quietly mean "not
    executable"."""

    def test_executable_and_regular_modes(self):
        self.assertTrue(sdc.is_executable_mode(
            's', 0, '100755 blob abc123\tscripts/a.sh\n'))
        self.assertFalse(sdc.is_executable_mode(
            's', 0, '100644 blob abc123\tscripts/a.txt\n'))

    def test_nonzero_returncode_raises(self):
        with self.assertRaises(ValueError):
            sdc.is_executable_mode('s', 128, '')

    def test_empty_output_raises_instead_of_defaulting_to_644(self):
        # `git show` succeeding while `git ls-tree` returns nothing means the
        # two disagree about the path — that is a bug to surface, not a 0644.
        with self.assertRaises(ValueError):
            sdc.is_executable_mode('s', 0, '')

    def test_multiple_entries_raise_instead_of_taking_the_first(self):
        # Real `git ls-tree <ref> scripts/` output. Reading the mode off line 1
        # would take a CHILD's executable bit and deploy `git show`'s tree
        # listing as that file, reporting success.
        listing = ('100755 blob 78981922613b2afb6025042ff6bd878ac1994e85\tscripts/a.sh\n'
                   '100644 blob 61780798228d17af2d34fce4cfbdf35556832472\tscripts/b.sh\n')
        with self.assertRaises(ValueError) as ctx:
            sdc.is_executable_mode('scripts/', 0, listing)
        self.assertIn('directory', str(ctx.exception))

    def test_a_single_entry_with_a_trailing_newline_is_fine(self):
        self.assertTrue(sdc.is_executable_mode(
            's', 0, '100755 blob abc123\tscripts/a.sh\n'))

    def test_symlink_and_gitlink_modes_raise(self):
        for mode in ('120000', '160000', '040000'):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    sdc.is_executable_mode('s', 0, f'{mode} blob abc\tp\n')

    def test_a_bad_mode_fails_the_sync_loudly(self):
        # End-to-end: the raise must surface as a non-zero exit, not a file
        # deployed with the wrong bit.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sdc, 'is_executable_mode',
                                   side_effect=ValueError('unsupported mode')):
                rc = sdc.sync(Path(d), ['scripts/emit_capture.sh'])
            self.assertEqual(rc, 2)
            self.assertEqual(list(Path(d).iterdir()), [])


class FetchStatusTests(unittest.TestCase):
    """A failed fetch means the comparison basis is unverified.

    Before this was split out, `_fetch()` returned a bare False for both "the
    caller opted out" and "the fetch broke", and main() only mentioned it under
    --verbose. A ref-lock clash with the automation that shares this checkout
    therefore produced a completely silent exit 0 while the deployed copies sat
    stale — the exact false-clean this script exists to prevent."""

    def _main(self, *args, status, dest):
        with mock.patch.object(sdc, '_fetch', return_value=(status, 'boom')), \
             mock.patch.dict(os.environ, {'OL_SYNC_DEST_DIR': str(dest)},
                             clear=False):
            return sdc.main(list(args))

    def test_skip_env_reports_skipped_not_failed(self):
        with mock.patch.dict(os.environ, {'OL_SYNC_SKIP_FETCH': '1'},
                             clear=False):
            self.assertEqual(sdc._fetch()[0], 'skipped')

    def test_failed_fetch_exits_nonzero_even_when_nothing_drifted(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            # First converge, so the ONLY reason to fail is the bad fetch.
            self.assertEqual(self._main(status='ok', dest=dest), 0)
            rc = self._main(status='failed', dest=dest)
        self.assertEqual(rc, 3)

    def test_failed_fetch_says_so_on_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            self._main(status='ok', dest=dest)
            with mock.patch.object(sdc, '_err') as err:
                self._main(status='failed', dest=dest)
        msgs = ' '.join(str(c.args[0]) for c in err.call_args_list if c.args)
        self.assertIn('fetch failed', msgs)
        self.assertIn('NOT verified', msgs)

    def test_failed_fetch_also_taints_check_mode(self):
        # A --check gate must not go green off a ref it could not refresh.
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            self._main(status='ok', dest=dest)
            self.assertEqual(self._main('--check', status='ok', dest=dest), 0)
            rc = self._main('--check', status='failed', dest=dest)
        self.assertEqual(rc, 3)

    def test_drift_still_outranks_a_failed_fetch(self):
        # rc 2 (could not sync) must survive a concurrent fetch failure.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sdc, '_fetch',
                                   return_value=('failed', 'boom')), \
                 mock.patch.object(sdc, 'load_manifest',
                                   return_value=(Path(d), ['scripts/nope.sh'])):
                self.assertEqual(sdc.main([]), 2)

    def test_successful_fetch_stays_silent_and_zero(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            self.assertEqual(self._main(status='ok', dest=dest), 0)
            self.assertEqual(self._main(status='ok', dest=dest), 0)


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
