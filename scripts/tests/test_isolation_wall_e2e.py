"""End-to-end test that a real interpreter invocation is walled: after
reexec_under_wall(), the process runs inside the bwrap namespace and the REAL
~/agents tree is read-only — for the process itself AND any child it spawns.

Skipped when bwrap is unavailable (macOS dev / hosts without bubblewrap): there
is no kernel wall to assert there, and the fail-open path is covered by the unit
tests. Writes are attempted only in the spawned child and are self-cleaning, so
a broken wall fails loudly without leaving state in ~/agents.
"""
try:
    from . import _bootstrap  # noqa: F401  bootstrap-first-import
except ImportError:  # pragma: no cover
    import _bootstrap  # noqa: F401

import os
import shutil
import subprocess
import sys
import unittest

import test_isolation_wall as wall

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Driver executed as `python3 -c <DRIVER>`. It calls the real reexec_under_wall
# (which re-execs itself under bwrap when not already walled), then — running in
# the walled process — reports whether the wall is active and whether a write to
# the REAL ~/agents raises EROFS. Any created file on the (broken-wall) leak
# branch is removed immediately, so this never leaves artifacts behind.
DRIVER = (
    "import os, sys\n"
    "sys.path.insert(0, 'scripts')\n"
    "import test_isolation_wall as w\n"
    "w.reexec_under_wall()\n"
    "walled = os.environ.get('OURLIBERTY_TEST_WALL_ACTIVE')\n"
    "target = os.path.join(os.path.expanduser('~'), 'agents', '__wall_e2e_probe__')\n"
    "ro = None\n"
    "try:\n"
    "    fh = open(target, 'w'); fh.write('x'); fh.close()\n"
    "    ro = False\n"
    "    os.remove(target)\n"
    "except OSError:\n"
    "    ro = True\n"
    "print('RESULT', 'walled=' + str(walled), 'ro=' + str(ro))\n"
)


@unittest.skipIf(shutil.which('bwrap') is None,
                 'bwrap unavailable — no kernel wall to assert')
class WallEndToEndTest(unittest.TestCase):
    def test_reexec_makes_real_agents_readonly(self):
        # Scrub the guards so the child does a FRESH wall decision (unless it is
        # already inside an inherited namespace, in which case it is walled
        # anyway — both outcomes satisfy the assertion below).
        env = dict(os.environ)
        env.pop('OURLIBERTY_TEST_WALL_ACTIVE', None)
        env.pop("OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT", None)
        env.pop("OURLIBERTY_DISABLE_TEST_WALL", None)
        env.pop('OURLIBERTY_TEST_BOOTSTRAP_ENGAGED', None)

        result = subprocess.run(
            [sys.executable, '-c', DRIVER],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, env=env,
        )
        out = (result.stdout or '') + '\n' + (result.stderr or '')
        self.assertIn('RESULT', out, f'driver did not report:\n{out}')
        # ro=True is THE safety invariant: the kernel makes ~/agents read-only
        # for a real interpreter invocation. It holds via the child's own
        # re-exec when this test is run cold, and via the inherited namespace
        # when run inside the already-walled suite (a nested bwrap can't be
        # created, so the child rides the parent's mount — still read-only).
        # Either way a write to ~/agents must be refused.
        self.assertIn('ro=True', out,
                      f'~/agents was WRITABLE for a real invocation (breach):\n{out}')
        self.assertNotIn('ro=False', out)

    def test_no_probe_artifact_left_behind(self):
        # Belt: whatever happened above, the self-cleaning driver must not have
        # left the sentinel in the real tree.
        leaked = os.path.join(wall.real_home() or os.path.expanduser('~'),
                              'agents', '__wall_e2e_probe__')
        self.assertFalse(os.path.exists(leaked))


if __name__ == '__main__':
    unittest.main()
