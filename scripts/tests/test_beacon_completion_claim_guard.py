#!/usr/bin/env python3
"""Integration tests for the Beacon completion-claim prose guard
(harden-authoritative-dispatch-confirmation, 2026-06-04).

On 2026-06-03 Beacon DM'd Larry "Approved — `deploy-notifier-ready-logonly`
dispatches to Forge now" in a reply with NO APPROVAL_REQUEST marker behind it,
so nothing actually dispatched — yet Larry saw a completed-dispatch claim. The
guard, wired into `_send_beacon_response`, intercepts a no-marker reply that
asserts a COMPLETED dispatch and re-prompts Beacon for a real marker (bounded by
MAX_COMPLETION_CLAIM_RETRIES) instead of forwarding the phantom to Larry.

Sibling surface to the malformed-marker kickback (test_beacon_approval_kickback);
same in-process re-call machinery. These tests exercise the REAL
extract_approval_request + is_completion_claim (only telegram_send / call_beacon /
log / save_sessions / _bot_state are monkeypatched).

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_beacon_completion_claim_guard
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# A canonical, parseable APPROVAL_REQUEST block (all required fields present)
# Beacon can re-emit to recover from a kickback.
GOOD_MARKER = (
    "Here's the plan.\n"
    "=== APPROVAL_REQUEST ===\n"
    '{"task_id": "plan-x", "summary": "do x", '
    '"target_agent": "forge", "prompt": "build x"}\n'
    "=== END_APPROVAL_REQUEST ==="
)

PHANTOM = "Approved — `deploy-notifier-ready-logonly` dispatches to Forge now"


class CompletionClaimGuardTest(unittest.TestCase):
    def setUp(self):
        self.bot = importlib.import_module('beacon_telegram_bot')
        self.approval = self.bot.approval

        self._sent: list[tuple[int, str]] = []
        self._beacon_calls: list[tuple[str, object]] = []

        self._orig_telegram_send = self.bot.telegram_send
        self._orig_call_beacon = self.bot.call_beacon
        self._orig_trust = self.approval.trust_decision
        self._orig_reject = self.approval.format_policy_rejection
        self._orig_log = self.bot.log
        self._orig_save = self.bot.save_sessions
        self._orig_state = self.bot._bot_state

        self.bot.telegram_send = lambda chat_id, text: self._sent.append(
            (chat_id, text)
        )
        self.bot.log = lambda *a, **k: None
        self.bot.save_sessions = lambda *a, **k: None
        self.bot._bot_state = {'sessions': {'999': 'sess-original'}}
        # Land any recovered marker in the reject branch so we never exercise a
        # real safe_write_inbox dispatch.
        self.approval.trust_decision = lambda payload: ('reject', {})
        self.approval.format_policy_rejection = lambda payload, rule: 'REJECTED'

    def tearDown(self):
        self.bot.telegram_send = self._orig_telegram_send
        self.bot.call_beacon = self._orig_call_beacon
        self.approval.trust_decision = self._orig_trust
        self.approval.format_policy_rejection = self._orig_reject
        self.bot.log = self._orig_log
        self.bot.save_sessions = self._orig_save
        self.bot._bot_state = self._orig_state

    # ---- recovery: phantom claim, then a clean re-emit ----------------------

    def test_phantom_kicks_back_and_recovers(self):
        """A no-marker completion claim re-prompts Beacon; the clean re-emit
        then flows through the normal approval path (phantom never forwarded)."""
        def fake_call_beacon(prompt, session_id):
            self._beacon_calls.append((prompt, session_id))
            return GOOD_MARKER, 'sess-new'

        self.bot.call_beacon = fake_call_beacon
        self.bot._send_beacon_response(999, PHANTOM)

        # Re-prompted exactly once, with a correction naming the marker + the
        # "completed/dispatch" problem, carrying the existing session id.
        self.assertEqual(len(self._beacon_calls), 1)
        correction, sess = self._beacon_calls[0]
        self.assertIn('APPROVAL_REQUEST', correction)
        self.assertIn('dispatch', correction.lower())
        self.assertEqual(sess, 'sess-original')
        self.assertEqual(self.bot._bot_state['sessions']['999'], 'sess-new')

        # The phantom prose was NOT forwarded; the recovered marker reached the
        # reject branch instead.
        sent_texts = [t for _, t in self._sent]
        self.assertNotIn(PHANTOM, sent_texts)
        self.assertIn('REJECTED', sent_texts)

    # ---- exhaustion: claim every time → loud interception, never the phantom -

    def test_phantom_exhaustion_intercepts_loud(self):
        """After MAX_COMPLETION_CLAIM_RETRIES claim-only re-emits, intercept with
        a loud notice — and never forward the phantom prose to Larry."""
        def always_claim(prompt, session_id):
            self._beacon_calls.append((prompt, session_id))
            return PHANTOM, session_id

        self.bot.call_beacon = always_claim
        self.bot._send_beacon_response(999, PHANTOM)

        self.assertEqual(
            len(self._beacon_calls), self.bot.MAX_COMPLETION_CLAIM_RETRIES
        )

        sent_texts = [t for _, t in self._sent]
        # The phantom is never forwarded as-is...
        self.assertNotIn(PHANTOM, sent_texts)
        # ...and Larry gets a loud "nothing dispatched / intercepted" notice.
        self.assertTrue(
            any(
                'dispatched' in t.lower() and 'intercepted' in t.lower()
                for t in sent_texts
            ),
            f'expected a loud interception notice, got: {sent_texts}',
        )

    # ---- escape hatch: rephrase to intent → forwarded, no further kickback ---

    def test_rephrase_to_intent_is_forwarded(self):
        """If Beacon rephrases to plain intent on re-prompt, the guard lets it
        through (no completion claim, no marker needed)."""
        intent_reply = "I'm dispatching deploy-notifier now — will confirm when it lands."

        def rephrase(prompt, session_id):
            self._beacon_calls.append((prompt, session_id))
            return intent_reply, session_id

        self.bot.call_beacon = rephrase
        self.bot._send_beacon_response(999, PHANTOM)

        # One kickback, then the rephrased intent forwarded verbatim.
        self.assertEqual(len(self._beacon_calls), 1)
        sent_texts = [t for _, t in self._sent]
        self.assertIn(intent_reply, sent_texts)
        self.assertNotIn(PHANTOM, sent_texts)

    # ---- normal talk: no marker, no claim → forwarded, no kickback ----------

    def test_normal_no_marker_reply_forwarded(self):
        normal = "Forge handles deploys; Mirror reviews the PR. What's next?"
        self.bot.call_beacon = lambda p, s: (_ for _ in ()).throw(
            AssertionError('call_beacon must not fire on normal talk')
        )
        self.bot._send_beacon_response(999, normal)

        self.assertEqual(self._beacon_calls, [])
        self.assertIn(normal, [t for _, t in self._sent])


if __name__ == '__main__':
    unittest.main()
