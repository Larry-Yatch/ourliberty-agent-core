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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from unittest import mock
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
        #
        # The exact set is pinned (not just membership) so widening stays a
        # DELIBERATE, reviewed act. It was {'beacon'} until 2026-08-26, when two
        # further losses of the same class proved the dashboard builds envelopes
        # for two other targets and neither was deliverable:
        #   forge   — the clarify_request branch returns `asking_agent`
        #             ('forge' today per outbox_notifier) and names its file
        #             `resume-<task>-r<n>.json`; `resume-m3-pr1-r1.json` was
        #             denied 2026-07-22 and Larry's answer was discarded.
        #   mirror  — `_build_recheck_envelope` returns a hardcoded 'mirror' for
        #             the recheck button and for Approve on a promoted
        #             stranded-escalation card; two such envelopes were denied
        #             2026-08-26 and Larry's approvals did nothing.
        #
        # THIS test only ever guarded the SOURCE dimension, which is why it was
        # green through all three incidents. The TARGET dimension is guarded by
        # test_dashboard_api.DashboardActionRouteLegalityTest, which drives the
        # real builder for every (event_type, action) and asserts the target it
        # returns is in this set. Widen this set only alongside that test.
        self.assertIn('dashboard', dv.ALLOWED_SOURCES)
        self.assertIn('dashboard', rv.FRESH_DISPATCH_ROUTES)
        self.assertEqual(rv.FRESH_DISPATCH_ROUTES['dashboard'],
                         {'beacon', 'forge', 'mirror'})

    def test_human_sources_are_allowed_and_routed(self):
        """HUMAN_SOURCES is the machine-readable answer to "did a person author
        this?" — the third guard this file now carries, and it exists because
        that property lived only in a `# Humans` COMMENT until 2026-08-27.

        Being a comment is why agent-core #1112 hand-copied a third source list
        into inbox_watcher instead of importing one: prose can only be restated,
        and the restatement shipped containing 'telegram-webhook', a service
        decommissioned 2026-05-12. A human source that is not allowed, or has no
        route, cannot deliver Larry's action at all.
        """
        self.assertTrue(dv.HUMAN_SOURCES, 'the human set must not be empty')
        for source in sorted(dv.HUMAN_SOURCES):
            with self.subTest(source=source):
                self.assertIn(source, dv.ALLOWED_SOURCES)
                self.assertIn(source, rv.FRESH_DISPATCH_ROUTES)
                self.assertTrue(rv.FRESH_DISPATCH_ROUTES[source])

    def test_human_sources_have_no_hand_copied_twin(self):
        """The defect this guard was born from: a SECOND hand-maintained human
        list living in a consumer, free to drift.

        Asserted BEHAVIOURALLY, not as a tombstone for one retired name — a list
        re-grown under any other name, or inlined, passed the name check. Patch
        the canonical set to a sentinel and require every consumer's answer to
        follow it; only a live lookup can.
        """
        import inbox_watcher as iw  # noqa: PLC0415
        with mock.patch.object(dv, 'HUMAN_SOURCES', frozenset({'sentinel-src'})):
            self.assertIn('sentinel-src', dv.HUMAN_SOURCES)
            self.assertNotIn('dashboard', dv.HUMAN_SOURCES)
            # inbox_watcher must resolve through the module, holding no copy.
            self.assertIs(iw.dispatch_validator, dv)
            self.assertEqual(iw.dispatch_validator.HUMAN_SOURCES,
                             frozenset({'sentinel-src'}))

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
