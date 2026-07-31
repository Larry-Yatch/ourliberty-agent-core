#!/usr/bin/env python3
"""Every scripts/**/*.sh must parse. `bash -n` over the whole tree.

WHY THIS EXISTS, AND WHY IT IS REQUIRED RATHER THAN NICE-TO-HAVE
----------------------------------------------------------------
run_cycle.sh and run_medic.sh now guard their `source` of
``_lib_cost_capture.sh`` with a ``bash -n`` precheck, so a malformed lib
degrades to a logged cost-capture skip instead of killing the tick under
``set -e``. That converts a LOUD failure into a QUIET one — exactly the trade a
best-effort cost row wants, and exactly the trade that needs a second line of
defence, or a syntax error committed into the lib would ship to the droplet and
silently stop every cost row without anything going red.

It also covers the two sources run_cycle.sh still makes UNGUARDED
(``_lib_push_with_rebase.sh``, ``_lib_pulse_runtime.sh``). Those are deliberate:
PULSE_RUNTIME_PATHS decides which paths the cycle auto-commits, so degrading a
broken load to a skip would let the cycle commit the wrong path set. Failing
loud is right there — and this gate is what keeps the loud failure from
happening in production rather than in CI.

The repo has no shell linting anywhere else (no shellcheck, no workflow), so
until now a shell syntax error had no gate at all.

Run::

    python3 -m unittest scripts.tests.test_shell_scripts_parse
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import shutil
import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _REPO_ROOT / 'scripts'

# Files whose absence would mean the glob below silently stopped matching. A
# property over an EMPTY set is green, so the set is pinned by name.
_MUST_BE_COVERED = (
    'run_cycle.sh',
    'run_medic.sh',
    '_lib_cost_capture.sh',
    '_lib_pulse_runtime.sh',
    '_lib_push_with_rebase.sh',
)


def _shell_scripts():
    return sorted(_SCRIPTS.rglob('*.sh'))


@unittest.skipUnless(shutil.which('bash'), 'bash not on PATH')
class ShellScriptsParseTest(unittest.TestCase):

    def test_the_enumeration_is_not_empty(self):
        """ANTI-VACUITY: a glob regression must fail loudly, not pass empty."""
        found = {p.name for p in _shell_scripts()}
        self.assertTrue(found, f'no *.sh found under {_SCRIPTS}')
        missing = [n for n in _MUST_BE_COVERED if n not in found]
        self.assertFalse(
            missing,
            f'these shell files must be covered by the parse gate: {missing}')

    def test_every_shell_script_parses(self):
        broken = []
        checked = 0
        for path in _shell_scripts():
            with self.subTest(script=str(path.relative_to(_REPO_ROOT))):
                result = subprocess.run(
                    ['bash', '-n', str(path)],
                    capture_output=True, text=True, timeout=60)
                checked += 1
                if result.returncode != 0:
                    broken.append((str(path.relative_to(_REPO_ROOT)),
                                   result.stderr.strip()[:400]))
                self.assertEqual(
                    result.returncode, 0,
                    f'{path.name} does not parse — under `set -e` a wrapper '
                    f'that sources it dies before it can log anything:\n'
                    f'{result.stderr[:600]}')
        self.assertEqual(broken, [])
        self.assertGreaterEqual(checked, len(_MUST_BE_COVERED))


if __name__ == '__main__':
    unittest.main()
