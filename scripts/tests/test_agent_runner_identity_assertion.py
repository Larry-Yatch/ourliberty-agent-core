#!/usr/bin/env python3
"""Tests for the identity-assertion preamble helper (E1.2).

The helper `agent_runner._maybe_prepend_identity_assertion` centralizes
the gating logic that used to live in `inbox_watcher.process_task`. It
exists so every `run_claude` caller can opt in by passing `expected_agent`
without re-implementing the three idempotency conditions.

The four conditions to verify here:
  1. Prepends when expected_agent is set, session_id is None, and the
     marker is absent from the prompt.
  2. No-op when expected_agent is None / empty.
  3. No-op when session_id is set (resume — preamble was on the original
     turn).
  4. No-op when the marker is already in the prompt (idempotency —
     re-calling does not double the preamble).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_agent_runner_identity_assertion
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import agent_runner as ar  # noqa: E402


SAMPLE_PROMPT = 'Read inbox/abc-001.json and respond.'


class MaybePrependIdentityAssertionTest(unittest.TestCase):
    """Direct coverage of the helper that gates the preamble injection."""

    def test_prepends_when_expected_agent_set_no_session_no_marker(self):
        out = ar._maybe_prepend_identity_assertion(
            prompt=SAMPLE_PROMPT, expected_agent='forge', session_id=None,
        )
        self.assertIn(ar.IDENTITY_ASSERTION_MARKER, out)
        self.assertTrue(out.endswith(SAMPLE_PROMPT))
        # Preamble names the agent in lowercase, per build_expected_agent_assertion.
        self.assertIn('`forge`', out)

    def test_no_prepend_when_expected_agent_is_none(self):
        out = ar._maybe_prepend_identity_assertion(
            prompt=SAMPLE_PROMPT, expected_agent=None, session_id=None,
        )
        self.assertEqual(out, SAMPLE_PROMPT)

    def test_no_prepend_when_expected_agent_is_empty_string(self):
        out = ar._maybe_prepend_identity_assertion(
            prompt=SAMPLE_PROMPT, expected_agent='', session_id=None,
        )
        self.assertEqual(out, SAMPLE_PROMPT)

    def test_no_prepend_when_session_id_set_resume_path(self):
        # On --resume the preamble was already there on the original turn;
        # re-asserting it on every resumed turn is noise.
        out = ar._maybe_prepend_identity_assertion(
            prompt=SAMPLE_PROMPT, expected_agent='forge',
            session_id='sess-abc-123',
        )
        self.assertEqual(out, SAMPLE_PROMPT)
        self.assertNotIn(ar.IDENTITY_ASSERTION_MARKER, out)

    def test_no_prepend_when_marker_already_in_prompt(self):
        # Caller built the prompt with the preamble already present.
        prebuilt = ar.build_expected_agent_assertion('forge') + SAMPLE_PROMPT
        out = ar._maybe_prepend_identity_assertion(
            prompt=prebuilt, expected_agent='forge', session_id=None,
        )
        self.assertEqual(out, prebuilt)
        # Marker present exactly once — the helper did not double-prepend.
        self.assertEqual(out.count(ar.IDENTITY_ASSERTION_MARKER), 1)

    def test_idempotent_under_repeated_calls(self):
        # Calling the helper twice on its own output must not stack
        # preambles. This is the property that keeps the helper safe to
        # call defensively at multiple layers.
        first = ar._maybe_prepend_identity_assertion(
            prompt=SAMPLE_PROMPT, expected_agent='mirror', session_id=None,
        )
        second = ar._maybe_prepend_identity_assertion(
            prompt=first, expected_agent='mirror', session_id=None,
        )
        self.assertEqual(first, second)
        self.assertEqual(second.count(ar.IDENTITY_ASSERTION_MARKER), 1)


if __name__ == '__main__':
    unittest.main()
