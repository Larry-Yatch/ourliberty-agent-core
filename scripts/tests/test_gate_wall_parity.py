"""Parity guard: the regression gate's discover-subprocess wall and the ad-hoc
re-exec wall (scripts/test_isolation_wall) must agree on the coordination env
var and on which trees are walled read-only. If they drift, either the gate
child would nest a second bwrap, or an ad-hoc run and the gate would jail
different trees — both silent isolation regressions.
"""
try:
    from . import _bootstrap  # noqa: F401  bootstrap-first-import
except ImportError:  # pragma: no cover
    import _bootstrap  # noqa: F401

import unittest

import test_isolation_wall as wall
import test_regression_check as gate


class GateWallParityTest(unittest.TestCase):
    def test_wall_active_env_matches(self):
        # The gate keeps a literal copy (no import dependency); it MUST equal the
        # wall module's canonical name or the gate child will not recognize that
        # it is already walled and will try to nest a second bwrap.
        self.assertEqual(gate._WALL_ACTIVE_ENV, wall.WALL_ACTIVE_ENV)

    def test_gate_walls_the_same_trees(self):
        # Both wall the real ~/agents and ~/agent-worktrees. The gate names them
        # REAL_AGENTS / REAL_WORKTREES; the wall derives them from the passwd
        # home. Compare the resolved paths.
        gate_targets = {str(gate.REAL_AGENTS), str(gate.REAL_WORKTREES)}
        home = wall.real_home()
        wall_expected = {f'{home}/agents', f'{home}/agent-worktrees'}
        self.assertEqual(gate_targets, wall_expected)

    def test_gate_prefix_sets_wall_active(self):
        # The gate's discover wall prefix must stamp the wall-active flag so the
        # child _bootstrap skips its own re-exec. Exercise the argv construction
        # DETERMINISTICALLY: force a fake bwrap + a passing probe so the bwrap
        # branch builds its prefix regardless of whether a real (nestable)
        # sandbox is available. Without this, running the suite UNDER the wall
        # would make _discover_wall_prefix probe a NESTED bwrap (which fails) and
        # return [], so this assertion would silently skip in exactly the armed
        # environment it is meant to protect (green check that proves nothing).
        from pathlib import Path
        old_which = gate.shutil.which
        old_probe = gate._probe
        gate.shutil.which = lambda name: '/fake/bwrap' if name == 'bwrap' else old_which(name)
        gate._probe = lambda cmd: True
        try:
            prefix = gate._discover_wall_prefix(Path('.'))
        finally:
            gate.shutil.which = old_which
            gate._probe = old_probe
        joined = ' '.join(str(x) for x in prefix)
        self.assertIn(f'--setenv {gate._WALL_ACTIVE_ENV} 1', joined)


if __name__ == '__main__':
    unittest.main()
