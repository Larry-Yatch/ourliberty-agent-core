"""Unit tests for scripts/test_isolation_wall.py — the kernel-enforced wall.

These are pure-logic tests (no bwrap required): they exercise the fail-open
decision branches, the invocation reconstruction, and the prefix shape with
``shutil.which`` / ``sys.orig_argv`` monkeypatched. The end-to-end "a real
``python3 -m unittest`` gets ~/agents read-only" behavior lives in
test_isolation_wall_e2e.py (guarded on a real bwrap).
"""
try:
    from . import _bootstrap  # noqa: F401  bootstrap-first-import
except ImportError:  # pragma: no cover - top-level (discover) import path
    import _bootstrap  # noqa: F401

import os
import sys
import unittest

import test_isolation_wall as wall


class ConstantsTest(unittest.TestCase):
    def test_env_var_names_are_stable(self):
        # These names are the coordination contract with _bootstrap and the
        # regression gate; changing them silently would un-wall the gate child.
        self.assertEqual(wall.WALL_ACTIVE_ENV, 'OURLIBERTY_TEST_WALL_ACTIVE')
        # The disable hatch is DELIBERATELY not the #820 redirect opt-out.
        self.assertEqual(wall.WALL_DISABLE_ENV, 'OURLIBERTY_DISABLE_TEST_WALL')
        self.assertFalse(hasattr(wall, 'LIVE_OPT_OUT_ENV'),
                         'wall must not key its opt-out on the #820 redirect var')


class RealHomeAndTargetsTest(unittest.TestCase):
    def test_real_home_ignores_HOME_override(self):
        # The HOME-jailing tests set $HOME under /tmp; the wall must still find
        # the REAL home from the passwd db, or it would wall a jailed tree.
        real = wall.real_home()
        self.assertTrue(real)
        old = os.environ.get('HOME')
        try:
            os.environ['HOME'] = '/tmp/some-jailed-home'
            self.assertEqual(wall.real_home(), real)
        finally:
            if old is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = old

    def test_ro_targets_are_existing_dirs(self):
        for t in wall.ro_targets():
            self.assertTrue(os.path.isdir(t), f'{t} is not a dir')
            self.assertTrue(
                os.path.basename(t) in ('agents', 'agent-worktrees'),
                f'unexpected wall target {t}',
            )


class ReconstructCmdTest(unittest.TestCase):
    def _with_orig_argv(self, argv):
        old = getattr(sys, 'orig_argv', None)
        sys.orig_argv = argv
        self.addCleanup(lambda: setattr(sys, 'orig_argv', old))

    def test_python_invocation_pins_the_interpreter(self):
        self._with_orig_argv(['python3', '-m', 'unittest', 'discover'])
        cmd = wall._reconstruct_cmd()
        self.assertEqual(cmd, [sys.executable, '-m', 'unittest', 'discover'])

    def test_empty_orig_argv_degrades(self):
        self._with_orig_argv([])
        self.assertIsNone(wall._reconstruct_cmd())

    def test_empty_argv0_degrades(self):
        # A populated list whose first element is '' must fail-open, not pass an
        # empty argv0 to bwrap (which would hard-fail in a grandchild).
        self._with_orig_argv(['', 'discover'])
        self.assertIsNone(wall._reconstruct_cmd())

    def test_unresolvable_console_script_degrades(self):
        # A non-python entry point that isn't on PATH must degrade to None
        # (unwalled) rather than hand bwrap an unrunnable argv0.
        self._with_orig_argv(['definitely-not-a-real-binary-xyz', 'x'])
        self.assertIsNone(wall._reconstruct_cmd())

    def test_resolvable_console_script_is_resolved(self):
        old = wall.shutil.which
        wall.shutil.which = lambda name: '/usr/bin/' + name
        self.addCleanup(lambda: setattr(wall.shutil, 'which', old))
        self._with_orig_argv(['mytool', 'run'])
        self.assertEqual(wall._reconstruct_cmd(), ['/usr/bin/mytool', 'run'])


class BwrapPrefixTest(unittest.TestCase):
    def test_none_when_bwrap_missing(self):
        old = wall.shutil.which
        wall.shutil.which = lambda name: None
        self.addCleanup(lambda: setattr(wall.shutil, 'which', old))
        self.assertIsNone(wall.bwrap_prefix())

    def test_prefix_shape_with_fake_bwrap(self):
        # Force a fake bwrap path and a probe that always passes, so we test the
        # ARGV shape without needing (or invoking) a real sandbox.
        old_which = wall.shutil.which
        old_probe = wall._probe
        wall.shutil.which = lambda name: '/fake/bwrap'
        wall._probe = lambda cmd: True
        self.addCleanup(lambda: setattr(wall.shutil, 'which', old_which))
        self.addCleanup(lambda: setattr(wall, '_probe', old_probe))

        prefix = wall.bwrap_prefix(targets=['/home/x/agents'], workdir='/w')
        self.assertEqual(prefix[0], '/fake/bwrap')
        self.assertIn('--dev-bind', prefix)
        self.assertEqual(prefix[-1], '--')
        # workdir -> --chdir, target -> --ro-bind pair, wall-active setenv.
        self.assertIn('--chdir', prefix)
        joined = ' '.join(prefix)
        self.assertIn('--ro-bind /home/x/agents /home/x/agents', joined)
        self.assertIn(f'--setenv {wall.WALL_ACTIVE_ENV} 1', joined)

    def test_none_when_probe_fails(self):
        old_which = wall.shutil.which
        old_probe = wall._probe
        wall.shutil.which = lambda name: '/fake/bwrap'
        wall._probe = lambda cmd: False
        self.addCleanup(lambda: setattr(wall.shutil, 'which', old_which))
        self.addCleanup(lambda: setattr(wall, '_probe', old_probe))
        self.assertIsNone(wall.bwrap_prefix(targets=[]))


class ReexecFailOpenTest(unittest.TestCase):
    """reexec_under_wall must RETURN (never exec, never raise) on every
    fail-open branch. If any of these accidentally exec'd, the test process
    would be replaced and the run would look like a hang/crash."""

    def test_returns_when_already_walled(self):
        old = os.environ.get(wall.WALL_ACTIVE_ENV)
        os.environ[wall.WALL_ACTIVE_ENV] = '1'
        self.addCleanup(
            lambda: os.environ.__setitem__(wall.WALL_ACTIVE_ENV, old)
            if old is not None else os.environ.pop(wall.WALL_ACTIVE_ENV, None))
        self.assertIsNone(wall.reexec_under_wall())

    def test_returns_when_dedicated_disable_set(self):
        # Not already walled, but the dedicated disable hatch is set.
        old_active = os.environ.pop(wall.WALL_ACTIVE_ENV, None)
        old_opt = os.environ.get(wall.WALL_DISABLE_ENV)
        os.environ[wall.WALL_DISABLE_ENV] = '1'

        def restore():
            if old_active is not None:
                os.environ[wall.WALL_ACTIVE_ENV] = old_active
            if old_opt is None:
                os.environ.pop(wall.WALL_DISABLE_ENV, None)
            else:
                os.environ[wall.WALL_DISABLE_ENV] = old_opt
        self.addCleanup(restore)
        self.assertIsNone(wall.reexec_under_wall())

    def test_820_redirect_optout_does_NOT_disable_the_wall(self):
        # Regression guard for the decoupling: setting only the #820 redirect
        # opt-out must NOT short-circuit the wall. We prove it by asserting the
        # call proceeds PAST the opt-out check to the bwrap-availability check
        # (which we force to "missing" so it returns without exec-ing). If the
        # wall still keyed on the #820 var, we'd get the silent opt-out return
        # instead of the loud bwrap-missing warning.
        old_active = os.environ.pop(wall.WALL_ACTIVE_ENV, None)
        old_disable = os.environ.pop(wall.WALL_DISABLE_ENV, None)
        old_live = os.environ.get('OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT')
        os.environ['OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT'] = '1'
        old_which = wall.shutil.which
        wall.shutil.which = lambda name: None

        def restore():
            if old_active is not None:
                os.environ[wall.WALL_ACTIVE_ENV] = old_active
            if old_disable is not None:
                os.environ[wall.WALL_DISABLE_ENV] = old_disable
            if old_live is None:
                os.environ.pop('OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT', None)
            else:
                os.environ['OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT'] = old_live
            wall.shutil.which = old_which
        self.addCleanup(restore)

        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(wall.reexec_under_wall())
        # Reached the bwrap-missing branch => it did NOT early-return on #820 var.
        self.assertIn('bwrap not found', buf.getvalue())

    def test_returns_when_bwrap_missing(self):
        old_active = os.environ.pop(wall.WALL_ACTIVE_ENV, None)
        old_opt = os.environ.pop(wall.WALL_DISABLE_ENV, None)
        old_which = wall.shutil.which
        wall.shutil.which = lambda name: None

        def restore():
            if old_active is not None:
                os.environ[wall.WALL_ACTIVE_ENV] = old_active
            if old_opt is not None:
                os.environ[wall.WALL_DISABLE_ENV] = old_opt
            wall.shutil.which = old_which
        self.addCleanup(restore)
        self.assertIsNone(wall.reexec_under_wall())

    def test_returns_when_cannot_reconstruct(self):
        old_active = os.environ.pop(wall.WALL_ACTIVE_ENV, None)
        old_opt = os.environ.pop(wall.WALL_DISABLE_ENV, None)
        old_which = wall.shutil.which
        old_argv = getattr(sys, 'orig_argv', None)
        wall.shutil.which = lambda name: '/fake/bwrap'
        sys.orig_argv = []

        def restore():
            if old_active is not None:
                os.environ[wall.WALL_ACTIVE_ENV] = old_active
            if old_opt is not None:
                os.environ[wall.WALL_DISABLE_ENV] = old_opt
            wall.shutil.which = old_which
            sys.orig_argv = old_argv
        self.addCleanup(restore)
        self.assertIsNone(wall.reexec_under_wall())


if __name__ == '__main__':
    unittest.main()
