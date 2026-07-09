#!/usr/bin/env python3
"""The hermetic-`gh` guard (gh-quota flake family, PR #884 false BLOCK) is
LIVE for this test run.

_bootstrap.engage() (and conftest.py on the pytest path) PATH-shims `gh` to a
fail-open exit-1 stub so no test — nor any script a test spawns — can reach
the real GitHub API. This module is the runtime proof, in the same spirit as
test_conftest_init_parity's live-in-process checks: if a future edit drops
the shim (or reorders PATH so the real CLI wins again), these tests fail
instead of the suite silently going back to burning the shared account's
5000/hr GraphQL quota and flaking under quota exhaustion.

Run:
    cd scripts/tests && python3 -m unittest test_hermetic_gh_guard
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import shutil
import subprocess
import unittest

_OPTED_OUT = bool(os.environ.get('OURLIBERTY_ALLOW_REAL_GH'))


@unittest.skipIf(_OPTED_OUT, 'OURLIBERTY_ALLOW_REAL_GH=1 — real gh deliberate')
class HermeticGhGuardLiveTest(unittest.TestCase):
    """The PATH shim actually intercepts `gh` in this process."""

    def test_gh_resolves_to_the_shim(self):
        resolved = shutil.which('gh')
        self.assertIsNotNone(resolved, '`gh` not on PATH at all — the shim '
                             'dir was not prepended by the bootstrap.')
        self.assertIn(
            'test-gh-shim-', resolved,
            f'`gh` resolves to {resolved!r}, not the hermetic shim. Real '
            'GitHub API calls (and GraphQL quota burn) are reachable from '
            'tests again.',
        )

    def test_gh_invocation_is_blocked_and_fail_open_shaped(self):
        # The stub must look like a failed gh CLI call (rc!=0, marker on
        # stderr, nothing on stdout) — the shape every production helper
        # already tolerates fail-open.
        proc = subprocess.run(
            ['gh', 'api', 'rate_limit'],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('hermetic-test-guard', proc.stderr)
        self.assertEqual(proc.stdout, '')

    def test_child_processes_inherit_the_shim(self):
        # PATH (not an in-process mock) is the mechanism precisely so scripts
        # spawned BY tests are covered; prove the inheritance.
        proc = subprocess.run(
            ['/bin/sh', '-c', 'gh pr view 1 --json state'],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn('hermetic-test-guard', proc.stderr)


class BaselineWarmForkGuardTest(unittest.TestCase):
    """REGBASELINE_WARMING defaults on in test processes, so a
    mocked-merge-success test can never detach a REAL post-merge warm
    (`git fetch` + full-suite discover) via outbox_notifier's
    _spawn_post_merge_baseline_warm. Warm-fixture classes pop the var in
    setUp (and mock Popen) to exercise the real spawn construction — this
    test tolerates that by asserting the BOOTSTRAP set it, not the current
    instantaneous value some sibling test may have legitimately popped."""

    def test_warming_flag_defaulted_by_bootstrap(self):
        # Same-process ordering hazard: a fixture class that popped the var
        # restores it in tearDown, so by the time this runs the default must
        # hold again. Assert the value directly.
        self.assertEqual(
            os.environ.get('REGBASELINE_WARMING'), '1',
            'REGBASELINE_WARMING is not defaulted to 1 for test runs; a test '
            'that mocks `gh pr merge` to success will fork a REAL detached '
            'baseline warm from inside the suite (the #764 fork-bomb class).',
        )


if __name__ == '__main__':
    unittest.main()
