#!/usr/bin/env python3
"""test_marker_drift.py — cross-handler render <-> parse drift detection.

Phase E1.1. The render side (render_marker) and parse side
(parse_X_marker / extract_approval_request) live in the same module per
handler, but a change to one without the other still silently breaks the
contract. This test exercises every (agent, marker_type) tuple end to end:
render a sample payload, parse it back, assert the original payload survives.

If a future change adds a new marker type to any handler without:
  - extending MARKER_KEYWORDS or REQUIRED_FIELDS in the same module, OR
  - adding a sample payload to SAMPLE_PAYLOADS here,
this test fails loudly with a message naming what's missing.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_marker_drift
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import forge_preflight_handler as fph  # noqa: E402
import mirror_review_handler as mrh  # noqa: E402
import beacon_approval_handler as bah  # noqa: E402


# (agent_name, module, parser_callable)
# The parser callable's signature is normalized inside the test (Beacon's
# extract_approval_request returns 2-tuple, the others return 3-tuple).
HANDLERS = [
    ('forge', fph, fph.parse_forge_marker),
    ('mirror', mrh, mrh.parse_mirror_marker),
    ('beacon', bah, bah.extract_approval_request),
]


# Minimal round-trip-safe payloads per (agent, marker_type). When a new
# marker type lands in any handler, add a payload here too.
SAMPLE_PAYLOADS = {
    ('forge', 'proceed'): {
        'task_id': 'drift-001', 'preflight_summary': 'looks buildable',
    },
    ('forge', 'clarify_request'): {
        'task_id': 'drift-001', 'question': 'which file?',
    },
    ('forge', 'reject'): {
        'task_id': 'drift-001', 'reason': 'spec contradicts itself',
    },
    ('mirror', 'review_pass'): {
        'task_id': 'drift-001', 'pr_url': 'https://github.com/x/y/pull/1',
        'summary': 'change is correct',
    },
    ('mirror', 'review_revision'): {
        'task_id': 'drift-001', 'pr_url': 'https://github.com/x/y/pull/1',
        'findings': [{'file': 'a.py', 'line': 1, 'issue': 'typo'}],
        'severity': 'medium', 'confidence': 'high',
    },
    ('mirror', 'review_escalate'): {
        'task_id': 'drift-001', 'pr_url': 'https://github.com/x/y/pull/1',
        'reason': 'spec is internally inconsistent',
        'severity': 'high', 'confidence': 'high',
    },
    ('mirror', 'review_emergency_halt'): {
        'task_id': 'drift-001', 'pr_url': 'https://github.com/x/y/pull/1',
        'reason': 'plaintext credential in diff',
        'evidence': 'OPENAI_API_KEY=sk-... in config.py:12',
    },
    ('beacon', 'approval_request'): {
        'task_id': 'drift-001',
        'summary': 'add render_marker helpers',
        'target_agent': 'forge',
        'prompt': 'GOAL: add render_marker. CONTEXT: see scripts/...',
    },
}


class MarkerSchemaCoverageTest(unittest.TestCase):
    """Every MARKER_TYPE must have keywords, required fields, AND a sample."""

    def test_every_marker_type_is_fully_specified(self):
        for agent, mod, _parser in HANDLERS:
            for mtype in mod.MARKER_TYPES:
                with self.subTest(agent=agent, mtype=mtype):
                    self.assertIn(
                        mtype, mod.MARKER_KEYWORDS,
                        f'{agent}: marker type {mtype!r} is in MARKER_TYPES '
                        f'but missing from MARKER_KEYWORDS. Add it to keep '
                        f'render_marker working.',
                    )
                    self.assertIn(
                        mtype, mod.REQUIRED_FIELDS,
                        f'{agent}: marker type {mtype!r} is in MARKER_TYPES '
                        f'but missing from REQUIRED_FIELDS. Add it to keep '
                        f'render_marker validation working.',
                    )
                    self.assertIn(
                        (agent, mtype), SAMPLE_PAYLOADS,
                        f'{agent}: marker type {mtype!r} has no sample '
                        f'payload in test_marker_drift.SAMPLE_PAYLOADS. Add '
                        f'a minimal payload so the drift detector covers it.',
                    )


class RenderParseRoundTripTest(unittest.TestCase):
    """For every marker type, render <-> parse must be lossless."""

    def test_roundtrip_for_every_handler_and_type(self):
        for agent, mod, parser in HANDLERS:
            for mtype in mod.MARKER_TYPES:
                with self.subTest(agent=agent, mtype=mtype):
                    payload = SAMPLE_PAYLOADS[(agent, mtype)]
                    rendered = mod.render_marker(mtype, **payload)
                    if agent == 'beacon':
                        # extract_approval_request returns (payload, narrative)
                        parsed_payload, narrative = parser(rendered)
                        self.assertEqual(parsed_payload, payload)
                    else:
                        # parse_X_marker returns (type, payload, narrative)
                        parsed_type, parsed_payload, narrative = parser(rendered)
                        self.assertEqual(parsed_type, mtype)
                        self.assertEqual(parsed_payload, payload)
                    self.assertEqual(
                        narrative.strip(), '',
                        'rendered marker should be a complete block with no '
                        'narrative chrome around it',
                    )


if __name__ == '__main__':
    unittest.main()
