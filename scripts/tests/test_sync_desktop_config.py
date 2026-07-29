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

HERMETICITY: every test that needs deployed CONTENT reads it from a fixture
git repo built by setUpModule, never from this checkout — see _build_fixture_repo.

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
import shutil
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


# ---------- the hermetic fixture repo ----------
#
# WHY THIS EXISTS
# ---------------
# sync_desktop_config resolves every byte it deploys with
# `git show origin/main:<path>` run against `sdc._REPO_ROOT` — the REAL
# checkout. So each content test read live machine state: this clone's
# remote-tracking ref, the invoking user's git config, and whatever the ~8k
# tests ahead of this module (it sorts 340th of 364, near the very end) left
# behind in the process environment.
#
# That is the shape that passes alone and fails inside a full run, and it did:
# on the droplet this module passed 42/42 in isolation while the full suite
# failed exactly the 20 tests whose verdict depends on `_blob()` succeeding
# (14 in _SyncCase, 3 in FetchStatusTests, 3 in CliTests — the other 22 never
# touch origin/main and stayed green). The regression gate compares failing
# NAMES at head against the parent, so a failure that only sometimes occurs
# reads as a brand-new regression: it BLOCKED agent-core #1047 and #1053,
# neither of which touches this module, and escalated a decision card.
#
# The fix is the one the #866 batch used on the check_spec_doc family — the
# test owns its state instead of reading the machine's. setUpModule builds a
# throwaway git repo carrying a real `refs/remotes/origin/main`, and every
# content test points the production module at THAT. Nothing here can be
# perturbed by what ran before it.
#
# Deliberately NOT covered by the fixture: that the manifest's paths resolve at
# the real origin/main. Such a test would fail on every PR that ADDS a manifest
# entry (the path is not merged yet, by construction) — the working-tree checks
# in ManifestTests are the right level, and the script itself already exits 2
# loudly on a source it cannot resolve.

# Ambient git config must not reach the fixture — a global hook, a core.*
# default or a safe.directory rule read from the invoking user's ~/.gitconfig
# is exactly the machine state this fixture exists to shut out. Applied to
# os.environ for the whole test too, because the production `git` calls
# inherit the environment rather than taking one from us.
_HERMETIC_GIT_ENV = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_SYSTEM': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_AUTHOR_NAME': 'sync-fixture', 'GIT_AUTHOR_EMAIL': 'sync@fixture',
    'GIT_COMMITTER_NAME': 'sync-fixture', 'GIT_COMMITTER_EMAIL': 'sync@fixture',
    'GIT_TERMINAL_PROMPT': '0',
}

# `git -C <repo>` does NOT win over these: every one of them re-points git at
# another repository, index or object store regardless of -C. A single one left
# in os.environ by an earlier test would aim the fixture's own git calls — and
# the production ones, which inherit the environment — somewhere else entirely,
# which is the very defect class the fixture closes. Cleared, not just
# overridden, because the meaningful value of each is "unset".
_GIT_PLUMBING_ENV = (
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
    'GIT_CEILING_DIRECTORIES', 'GIT_NAMESPACE',
)

_FIXTURE_ROOT: Path | None = None


def _fixture() -> Path:
    if _FIXTURE_ROOT is None:  # pragma: no cover - setUpModule always runs
        raise RuntimeError('fixture repo missing: setUpModule did not run')
    return _FIXTURE_ROOT


def _git(repo: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update(_HERMETIC_GIT_ENV)
    for var in _GIT_PLUMBING_ENV:
        env.pop(var, None)
    subprocess.run(['git', '-C', str(repo), *args], env=env,
                   capture_output=True, text=True, check=True, timeout=60)


def _fixture_bytes(source: str) -> bytes:
    """Distinctive per-path content, so a mix-up shows up as a diff rather
    than as two files that happen to compare equal."""
    return (f'#!/usr/bin/env bash\n'
            f'# fixture stand-in for {source}\n'
            f'echo {Path(source).name}\n').encode('utf-8')


def _build_fixture_repo(root: Path) -> None:
    """A miniature of this repo carrying a real origin/main.

    Holds the manifest's own source PATHS (so the shipped manifest still
    drives the sync) with synthetic CONTENT (so no assertion depends on what
    the checkout happens to hold). Every source lands 100755 — the deployed
    hooks are invoked as bare executables, and _SyncCase asserts the bit
    survives the copy."""
    _git(root, 'init', '-q', '-b', 'main')

    _, sources = sdc.load_manifest()
    for src in sources:
        path = root / src
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_fixture_bytes(src))
        path.chmod(0o755)

    # The CLI resolves _REPO_ROOT from its OWN __file__, so CliTests must run a
    # copy that lives INSIDE the fixture; it reads config/ from there too.
    (root / 'config').mkdir(parents=True, exist_ok=True)
    (root / 'scripts').mkdir(parents=True, exist_ok=True)
    shutil.copy2(_REPO_ROOT / 'config' / 'desktop-config-sync.json',
                 root / 'config' / 'desktop-config-sync.json')
    shutil.copy2(_REPO_SCRIPTS / 'sync_desktop_config.py',
                 root / 'scripts' / 'sync_desktop_config.py')

    _git(root, 'add', '-A')
    _git(root, 'commit', '-q', '-m', 'fixture')
    # Synthesize the remote-tracking ref without a remote: `git show
    # origin/main:<path>` resolves refs/remotes/origin/main directly, so the
    # fixture needs no network and no fetch.
    _git(root, 'update-ref', 'refs/remotes/origin/main', 'main')


def setUpModule():
    global _FIXTURE_ROOT
    _FIXTURE_ROOT = Path(tempfile.mkdtemp(prefix='sync-desktop-fixture-'))
    _build_fixture_repo(_FIXTURE_ROOT)


def tearDownModule():
    global _FIXTURE_ROOT
    if _FIXTURE_ROOT is not None:
        shutil.rmtree(_FIXTURE_ROOT, ignore_errors=True)
        _FIXTURE_ROOT = None


class _HermeticRepoMixin:
    """Point the production module at the fixture repo for the whole test.

    Patched per-test rather than once per class so a test that dies mid-way
    still hands the next one an unpatched module — the failure mode this file
    exists to stop is precisely one test's leftovers deciding another's
    verdict."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.dict(os.environ, _HERMETIC_GIT_ENV,
                                        clear=False),
                        mock.patch.object(sdc, '_REPO_ROOT', _fixture())):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Safe after patch.dict has started: its restore replaces os.environ's
        # whole contents from the saved copy, so these come back on cleanup.
        for var in _GIT_PLUMBING_ENV:
            os.environ.pop(var, None)


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


class _SyncCase(_HermeticRepoMixin, unittest.TestCase):
    """A throwaway dest dir + a manifest source resolved from the FIXTURE's
    origin/main (never this checkout's — see the fixture notes above)."""

    SOURCE = 'scripts/emit_capture.sh'

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name)
        self.name = Path(self.SOURCE).name
        self.target = self.dest / self.name
        # What the fixture's origin/main holds — what the sync must converge on.
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

    def test_concurrent_runs_do_not_share_a_temp_path(self):
        # A shared temp name let a second process's truncating open() land a
        # PARTIAL file under the first process's rename, while the sync still
        # reported "installed". Asserted deterministically on the paths rather
        # than by racing threads, which would be flaky either way.
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(str(src))
            return real_replace(src, dst)

        with mock.patch.object(sdc.os, 'replace', spy):
            self._sync()
            self.target.unlink()
            self._sync()

        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1],
                            'two runs must not write through one temp path')
        for p in seen:
            self.assertTrue(p.endswith('.sync-tmp'), p)

    def test_a_failed_rename_leaves_no_temp_behind(self):
        # The temp is written before the rename; if the rename blows up, the
        # next run must not find a stray half-file sitting in the config dir.
        with mock.patch.object(sdc.os, 'replace',
                               side_effect=OSError('rename failed')):
            self.assertEqual(self._sync(), 2)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_deployed_file_is_not_left_group_or_world_writable(self):
        self.assertEqual(self._sync(), 0)
        mode = os.stat(self.target).st_mode
        self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH))

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

    @unittest.skipIf(os.geteuid() == 0,
                     'root writes through a 0500 directory, so the mode bit '
                     'cannot express "unwritable" for this user')
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
            'scripts/a.sh', 0, '100755 blob abc123\tscripts/a.sh\n'))
        self.assertFalse(sdc.is_executable_mode(
            'scripts/a.txt', 0, '100644 blob abc123\tscripts/a.txt\n'))

    def test_nonzero_returncode_raises(self):
        with self.assertRaises(ValueError):
            sdc.is_executable_mode('s', 128, '')

    def test_empty_output_raises_instead_of_defaulting_to_644(self):
        # `git show` succeeding while `git ls-tree` returns nothing means the
        # two disagree about the path — that is a bug to surface, not a 0644.
        with self.assertRaises(ValueError):
            sdc.is_executable_mode('s', 0, '')

    def test_single_child_directory_raises(self):
        # The case a line COUNT cannot catch: `git ls-tree <ref> solo/` on a
        # directory holding exactly one file returns ONE line, for the child.
        # Its 100755 used to pass, and `git show <ref>:solo/` hands back a tree
        # listing — so the listing text deployed as a 0755 file, exit 0.
        with self.assertRaises(ValueError) as ctx:
            sdc.is_executable_mode(
                'solo/', 0, '100755 blob abc123\tsolo/only.sh\n')
        self.assertIn('solo/only.sh', str(ctx.exception))

    def test_dot_slash_prefixed_source_is_still_accepted(self):
        # The path check must not reject a legitimate './'-spelled entry:
        # PurePosixPath normalises './scripts/a.sh' to 'scripts/a.sh'.
        self.assertTrue(sdc.is_executable_mode(
            './scripts/a.sh', 0, '100755 blob abc123\tscripts/a.sh\n'))

    def test_entry_for_a_different_path_raises(self):
        with self.assertRaises(ValueError):
            sdc.is_executable_mode(
                'scripts/a.sh', 0, '100755 blob abc123\tscripts/OTHER.sh\n')

    def test_missing_tab_separator_fails_safe(self):
        with self.assertRaises(ValueError):
            sdc.is_executable_mode('scripts/a.sh', 0, '100755 blob abc123\n')

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
            'scripts/a.sh', 0, '100755 blob abc123\tscripts/a.sh\n'))

    def test_symlink_and_gitlink_modes_raise(self):
        # Source and entry paths MATCH here on purpose, so each case reaches
        # the mode check and fails for the reason under test rather than
        # passing vacuously on the path mismatch above it.
        for mode in ('120000', '160000', '040000'):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as ctx:
                    sdc.is_executable_mode(
                        'scripts/a.sh', 0, f'{mode} blob abc\tscripts/a.sh\n')
                self.assertIn('unsupported git mode', str(ctx.exception))

    def test_a_bad_mode_fails_the_sync_loudly(self):
        # End-to-end: the raise must surface as a non-zero exit, not a file
        # deployed with the wrong bit.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sdc, 'is_executable_mode',
                                   side_effect=ValueError('unsupported mode')):
                rc = sdc.sync(Path(d), ['scripts/emit_capture.sh'])
            self.assertEqual(rc, 2)
            self.assertEqual(list(Path(d).iterdir()), [])


class FetchStatusTests(_HermeticRepoMixin, unittest.TestCase):
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


class CliTests(_HermeticRepoMixin, unittest.TestCase):
    """The entrypoint the hook actually invokes.

    Runs the FIXTURE's copy of the script, not this checkout's: the script
    resolves _REPO_ROOT from its own __file__, so where the file sits is the
    only way to choose which repo a subprocess reads origin/main from."""

    def _run(self, *args, dest: Path):
        env = dict(os.environ)
        env['OL_SYNC_DEST_DIR'] = str(dest)
        env['OL_SYNC_SKIP_FETCH'] = '1'  # no network in tests
        script = _fixture() / 'scripts' / 'sync_desktop_config.py'
        return subprocess.run([sys.executable, str(script), *args],
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
