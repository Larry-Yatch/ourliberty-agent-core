#!/usr/bin/env python3
"""Tests for the secret-handling hardening in the OAuth orchestrators
(scripts/auth_orchestrator.py + scripts/auth_orchestrator_tier2.py).

Covers the 2026-06-05 audit findings:
  - #1/#13  _write_private write-side TOCTOU: O_EXCL create + refuse a
            pre-planted symlink OR a pre-planted foreign-owned regular file,
            so token material never lands in a file an attacker controls.
  - #20     auth_orchestrator._maybe_restore_credentials puts the moved-aside
            Tier 1 credentials back when a run aborts before a fresh login.

Import must be side-effect-free (executable flow lives under main()/__main__),
so importing the modules here does NOT spawn `claude auth login`.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_auth_orchestrator_secret_handling
"""
from __future__ import annotations

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

import auth_orchestrator  # noqa: E402
import auth_orchestrator_tier2  # noqa: E402

# Both modules carry an identical _write_private; exercise the security branches
# against each.
_WRITE_MODULES = [auth_orchestrator, auth_orchestrator_tier2]


class WritePrivateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _path(self, name="secret.txt"):
        return str(self.tmp / name)

    def test_fresh_create_writes_at_0600(self):
        for mod in _WRITE_MODULES:
            with self.subTest(module=mod.__name__):
                p = self._path(f"fresh-{mod.__name__}.txt")
                mod._write_private(p, "token-material")
                self.assertEqual(Path(p).read_text(), "token-material")
                mode = stat.S_IMODE(os.stat(p).st_mode)
                self.assertEqual(mode, 0o600, f"{oct(mode)} != 0o600")

    def test_overwrites_preexisting_file_we_own(self):
        # A stale file we own from a prior run is fine to truncate+rewrite.
        for mod in _WRITE_MODULES:
            with self.subTest(module=mod.__name__):
                p = self._path(f"stale-{mod.__name__}.txt")
                Path(p).write_text("OLD-LONGER-CONTENT")
                mod._write_private(p, "new")
                self.assertEqual(Path(p).read_text(), "new")

    def test_append_appends(self):
        for mod in _WRITE_MODULES:
            with self.subTest(module=mod.__name__):
                p = self._path(f"append-{mod.__name__}.txt")
                mod._write_private(p, "a")
                mod._write_private(p, "b", append=True)
                mod._write_private(p, "c", append=True)
                self.assertEqual(Path(p).read_text(), "abc")

    def test_refuses_preplanted_symlink_and_does_not_write_target(self):
        # The classic predictable-path attack: a symlink at our path pointing at
        # a victim file. We must refuse (ELOOP mapped to PermissionError — assert
        # the specific type so a regression to a bare OSError is caught) and
        # leave the victim untouched.
        for mod in _WRITE_MODULES:
            with self.subTest(module=mod.__name__):
                victim = self._path(f"victim-{mod.__name__}.txt")
                Path(victim).write_text("VICTIM_UNCHANGED")
                link = self._path(f"link-{mod.__name__}.txt")
                os.symlink(victim, link)
                with self.assertRaises(PermissionError):
                    mod._write_private(link, "SECRET")
                self.assertEqual(Path(victim).read_text(), "VICTIM_UNCHANGED")

    def test_refuses_hardlink_swap_and_does_not_overwrite_victim(self):
        # Finding from SEC1 self-review: O_NOFOLLOW does NOT block a hardlink. A
        # same-uid hardlink of one of our own files onto the predictable path
        # passes the regular+owner checks; the st_nlink==1 guard must catch it
        # and leave the victim intact.
        for mod in _WRITE_MODULES:
            with self.subTest(module=mod.__name__):
                victim = self._path(f"hl-victim-{mod.__name__}.txt")
                Path(victim).write_text("VICTIM_UNCHANGED")
                link = self._path(f"hl-{mod.__name__}.txt")
                os.link(victim, link)  # same-uid hardlink => nlink == 2
                with self.assertRaises(PermissionError):
                    mod._write_private(link, "TOKEN_MATERIAL")
                self.assertEqual(Path(victim).read_text(), "VICTIM_UNCHANGED")

    def test_reopen_reasserts_0600_on_stale_world_readable_file(self):
        # The reopen-existing branch must re-assert 0600 (fchmod), so a secret
        # rewritten into a stale file left world-readable by a prior run does not
        # stay readable.
        for mod in _WRITE_MODULES:
            with self.subTest(module=mod.__name__):
                p = self._path(f"stale600-{mod.__name__}.txt")
                Path(p).write_text("old")
                os.chmod(p, 0o644)
                mod._write_private(p, "secret")  # hits the EEXIST reopen branch
                self.assertEqual(Path(p).read_text(), "secret")
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_refuses_foreign_owned_regular_file(self):
        # Finding #1: O_NOFOLLOW alone would happily write into an attacker-owned
        # regular file pre-planted at the path. Simulate "owned by someone else"
        # by making our own uid look different from the file owner's.
        for mod in _WRITE_MODULES:
            with self.subTest(module=mod.__name__):
                p = self._path(f"planted-{mod.__name__}.txt")
                Path(p).write_text("ATTACKER_KEEPS_THIS")
                real_uid = os.stat(p).st_uid
                with mock.patch.object(mod.os, "getuid", return_value=real_uid + 12345):
                    with self.assertRaises(PermissionError):
                        mod._write_private(p, "TOKEN_MATERIAL")
                # Secret must not have landed in the foreign-owned file.
                self.assertEqual(Path(p).read_text(), "ATTACKER_KEEPS_THIS")


class RestoreCredentialsTest(unittest.TestCase):
    """auth_orchestrator._maybe_restore_credentials — finding #20."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cred = self.tmp / ".credentials.json"
        self.backup = self.tmp / ".credentials.json.pre-orchestrator-123"
        self._noop_log = lambda *a, **k: None

    def test_restores_when_run_aborted_without_fresh_creds(self):
        self.backup.write_text("ORIGINAL_CREDS")  # we moved the live creds aside
        # cred file absent => login never completed
        restored = auth_orchestrator._maybe_restore_credentials(
            str(self.backup), str(self.cred), log_fn=self._noop_log
        )
        self.assertTrue(restored)
        self.assertTrue(self.cred.exists())
        self.assertEqual(self.cred.read_text(), "ORIGINAL_CREDS")
        self.assertFalse(self.backup.exists())

    def test_keeps_valid_fresh_creds_from_successful_login(self):
        self.backup.write_text('{"orig": true}')
        # A real login writes a complete JSON credentials file.
        self.cred.write_text('{"accessToken": "fresh", "refreshToken": "r"}')
        restored = auth_orchestrator._maybe_restore_credentials(
            str(self.backup), str(self.cred), log_fn=self._noop_log
        )
        self.assertFalse(restored)
        self.assertIn("fresh", self.cred.read_text())  # not clobbered
        self.assertTrue(self.backup.exists())  # backup preserved as artifact

    def test_restores_over_truncated_fresh_creds(self):
        # A login killed mid-write leaves a corrupt/partial JSON file. The old
        # existence-only check treated that as success and stranded the account;
        # validity-gating must restore over it.
        self.backup.write_text('{"orig": true}')
        self.cred.write_text('{"accessToken": "fre')  # truncated JSON
        restored = auth_orchestrator._maybe_restore_credentials(
            str(self.backup), str(self.cred), log_fn=self._noop_log
        )
        self.assertTrue(restored)
        self.assertEqual(json.loads(self.cred.read_text()), {"orig": True})
        self.assertFalse(self.backup.exists())

    def test_restores_over_empty_fresh_creds(self):
        self.backup.write_text('{"orig": true}')
        self.cred.write_text("")  # zero-byte remnant
        restored = auth_orchestrator._maybe_restore_credentials(
            str(self.backup), str(self.cred), log_fn=self._noop_log
        )
        self.assertTrue(restored)
        self.assertEqual(json.loads(self.cred.read_text()), {"orig": True})
        self.assertFalse(self.backup.exists())

    def test_symlinked_cred_file_does_not_suppress_restore(self):
        # A symlink at the credentials path pointing to valid JSON must NOT count
        # as a finished login (O_NOFOLLOW), so the backup is still restored and
        # the decoy target is left untouched.
        self.backup.write_text('{"orig": true}')
        decoy = self.tmp / "decoy.json"
        decoy.write_text('{"accessToken": "decoy"}')
        os.symlink(str(decoy), str(self.cred))
        restored = auth_orchestrator._maybe_restore_credentials(
            str(self.backup), str(self.cred), log_fn=self._noop_log
        )
        self.assertTrue(restored)
        self.assertFalse(self.cred.is_symlink())  # symlink name was replaced
        self.assertEqual(json.loads(self.cred.read_text()), {"orig": True})
        self.assertFalse(self.backup.exists())
        self.assertEqual(json.loads(decoy.read_text()), {"accessToken": "decoy"})

    def test_empty_json_object_is_not_a_finished_login(self):
        # A real credentials file always carries tokens; an empty object is not a
        # usable login, so restoring the backup over it is the safe choice.
        self.backup.write_text('{"orig": true}')
        self.cred.write_text("{}")
        restored = auth_orchestrator._maybe_restore_credentials(
            str(self.backup), str(self.cred), log_fn=self._noop_log
        )
        self.assertTrue(restored)
        self.assertEqual(json.loads(self.cred.read_text()), {"orig": True})

    def test_restore_survives_a_failing_logger(self):
        # The handler runs at atexit and must never raise: a logger that throws
        # (e.g. a hostile /tmp) must not mask a successful restore.
        def boom(*_a, **_k):
            raise PermissionError("logger down")
        self.backup.write_text('{"orig": true}')
        # no fresh cred file => restore path
        restored = auth_orchestrator._maybe_restore_credentials(
            str(self.backup), str(self.cred), log_fn=boom
        )
        self.assertTrue(restored)
        self.assertEqual(json.loads(self.cred.read_text()), {"orig": True})

    def test_noop_when_no_backup_was_made(self):
        # No pre-existing creds => no backup => nothing to restore.
        restored = auth_orchestrator._maybe_restore_credentials(
            None, str(self.cred), log_fn=self._noop_log
        )
        self.assertFalse(restored)


class ImportIsSideEffectFreeTest(unittest.TestCase):
    def test_modules_expose_helpers(self):
        for mod in _WRITE_MODULES:
            self.assertTrue(callable(mod._write_private))
            self.assertTrue(callable(mod.main))
            self.assertTrue(callable(mod._read_code_if_present))

    def test_import_does_not_change_process_umask(self):
        # The old code called os.umask(0o077) at module scope — proving import is
        # side-effect-free means a *fresh* interpreter's umask is unchanged after
        # importing both modules. Run in a subprocess so we observe a clean
        # process, not this already-imported one.
        code = (
            "import os, sys;"
            "before = os.umask(0o22); os.umask(before);"
            "import auth_orchestrator, auth_orchestrator_tier2;"
            "after = os.umask(0o22); os.umask(after);"
            "sys.exit(0 if before == after else 7)"
        )
        env = dict(os.environ, PYTHONPATH=str(_REPO_SCRIPTS))
        proc = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"import changed process umask: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
