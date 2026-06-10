#!/usr/bin/env python3
"""Regression guard: every ALLOWED_SOURCES member that originates a FRESH
agent->agent dispatch MUST have a routing_validator.FRESH_DISPATCH_ROUTES entry.

This is the test that would have caught the 2026-05-28 gap (and its 2026-06-10
recurrence): 'dashboard' was added to dispatch_validator.ALLOWED_SOURCES (layer-1
source validation) but never given a FRESH_DISPATCH_ROUTES entry (layer-2
routing). Result — dashboard Approve/Reject envelopes passed source validation,
then died at routing with 'source "dashboard" has no allowed routes' and were
silently dropped to beacon/.invalid while the API returned 200.

The two layers are hand-maintained in different files; nothing structurally
forces them to agree. This test makes them agree-or-explain: a source is OK iff
it falls into one of the bypass categories the routing layer already honors
(dialogue legs, system sources) OR it is in FRESH_DISPATCH_ROUTES OR it is a
deliberately-documented non-dispatch source listed below.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_dispatch_route_parity
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_validator as dv  # noqa: E402
import routing_validator as rv  # noqa: E402

# Sources present in ALLOWED_SOURCES that legitimately never originate a fresh
# agent->agent dispatch, so they need NO FRESH_DISPATCH_ROUTES entry. Keep this
# set SMALL and documented — adding to it is a conscious "this source can't
# dispatch work" decision, not a way to silence the guard. If a source here
# starts dispatching, give it a real route instead of leaving it exempt.
DOCUMENTED_NON_DISPATCH_SOURCES = {
    # Placeholder agents reserved in the allowlist so dispatch tests don't fail
    # as they come online; none of them originate dispatches yet. When one goes
    # live, add its FRESH_DISPATCH_ROUTES entry and drop it from here.
    'aide',
    'scout',
    'compass',
}


def _is_dialogue_leg(source: str) -> bool:
    return any(source.endswith(suffix) for suffix in rv.DIALOGUE_SUFFIXES)


class DispatchRouteParityTest(unittest.TestCase):
    def test_every_fresh_dispatch_source_has_a_route(self):
        unrouted = []
        for source in dv.ALLOWED_SOURCES:
            if _is_dialogue_leg(source):
                continue  # reply legs bypass the hard topology (already validated)
            if source in rv.SYSTEM_SOURCES:
                continue  # infra/system sources may target any agent
            if source in DOCUMENTED_NON_DISPATCH_SOURCES:
                continue  # explicitly not a dispatch originator
            if source in rv.FRESH_DISPATCH_ROUTES:
                continue  # has a route — good
            unrouted.append(source)

        self.assertEqual(
            sorted(unrouted), [],
            'Sources in dispatch_validator.ALLOWED_SOURCES that pass layer-1 '
            'source validation but have NO layer-2 FRESH_DISPATCH_ROUTES entry '
            '(they will be silently dropped to .invalid — the 2026-05-28 '
            'dashboard gap). Either add a route in routing_validator.py or, if '
            'the source genuinely never originates a dispatch, add it to '
            f'DOCUMENTED_NON_DISPATCH_SOURCES with a reason: {sorted(unrouted)}')

    def test_dashboard_specifically_is_routed(self):
        # The incident source — pin it directly so a regression is unambiguous.
        self.assertIn('dashboard', dv.ALLOWED_SOURCES)
        self.assertIn('dashboard', rv.FRESH_DISPATCH_ROUTES)
        self.assertEqual(rv.FRESH_DISPATCH_ROUTES['dashboard'], {'beacon'})

    def test_documented_exemptions_are_real_allowed_sources(self):
        # Guard the guard: an exemption for a source that isn't even in
        # ALLOWED_SOURCES is dead weight that hides the real list drifting.
        for source in DOCUMENTED_NON_DISPATCH_SOURCES:
            self.assertIn(
                source, dv.ALLOWED_SOURCES,
                f'{source!r} is exempted but not in ALLOWED_SOURCES — stale '
                f'exemption; remove it.')
            self.assertNotIn(
                source, rv.FRESH_DISPATCH_ROUTES,
                f'{source!r} now has a real route — drop it from '
                f'DOCUMENTED_NON_DISPATCH_SOURCES.')


if __name__ == '__main__':
    unittest.main()
