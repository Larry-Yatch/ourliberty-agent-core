#!/usr/bin/env python3
"""Regression tests for the Beacon malformed-APPROVAL_REQUEST kickback
(fix-mirror-verdict-marker-gate-001, 2026-06-03).

Sibling surface to the notifier's Forge/Mirror marker-error cascade. Before
this fix, `_send_beacon_response` caught `MalformedApprovalMarker` and only
forwarded the raw reply + a warning — Beacon never got a chance to re-emit a
clean canonical block (parallel to the pre-fix Mirror silent-drop). Now it
re-prompts Beacon in-process, capped at `MAX_APPROVAL_MARKER_RETRIES`, then
falls back to forward+warn on exhaust.

Because the telegram bot is a SYNCHRONOUS call_beacon request/response loop
(not the file-based outbox daemon), the kickback is an in-loop re-call rather
than a dead-letter inbox write — so these tests monkeypatch `call_beacon`,
`telegram_send`, and `approval.extract_approval_request` and assert on the
in-process behavior.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_beacon_approval_kickback
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


class BeaconApprovalKickbackTest(unittest.TestCase):
    def setUp(self):
        # Defer import so an in-progress edit doesn't blow up at collection.
        self.bot = importlib.import_module('beacon_telegram_bot')
        self.approval = self.bot.approval

        # Capture all outbound telegram sends + beacon re-calls.
        self._sent: list[tuple[int, str]] = []
        self._beacon_calls: list[tuple[str, object]] = []

        self._orig_telegram_send = self.bot.telegram_send
        self._orig_call_beacon = self.bot.call_beacon
        self._orig_extract = self.approval.extract_approval_request
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

    def tearDown(self):
        self.bot.telegram_send = self._orig_telegram_send
        self.bot.call_beacon = self._orig_call_beacon
        self.approval.extract_approval_request = self._orig_extract
        self.approval.trust_decision = self._orig_trust
        self.approval.format_policy_rejection = self._orig_reject
        self.bot.log = self._orig_log
        self.bot.save_sessions = self._orig_save
        self.bot._bot_state = self._orig_state

    # ---- recovery: one malformed marker, then a clean re-emit ----------

    def test_malformed_marker_kicks_back_and_recovers(self):
        """A first malformed marker re-prompts Beacon; the clean re-emit then
        flows through the normal approval path (no raw-reply forward)."""
        chat_id = 999
        bad_reply = "Here is my plan. **APPROVAL_REQUEST** (typed, not a block)"
        good_reply = "narrative\n=== APPROVAL_REQUEST ===\n{}\n=== END_APPROVAL_REQUEST ==="

        # extract: raise once, then return a valid payload on the re-emit.
        calls = {'n': 0}

        def fake_extract(reply):
            calls['n'] += 1
            if calls['n'] == 1:
                raise self.approval.MalformedApprovalMarker('no canonical block')
            return ({'task_id': 'plan-001'}, 'narrative')

        self.approval.extract_approval_request = fake_extract

        def fake_call_beacon(prompt, session_id):
            self._beacon_calls.append((prompt, session_id))
            return good_reply, 'sess-new'

        self.bot.call_beacon = fake_call_beacon

        # Land in the simple reject branch so we don't exercise dispatch.
        self.approval.trust_decision = lambda payload: ('reject', {})
        self.approval.format_policy_rejection = lambda payload, rule: 'REJECTED'

        self.bot._send_beacon_response(chat_id, bad_reply)

        # Beacon was re-prompted exactly once with a correction referencing the
        # canonical block, carrying the existing session id.
        self.assertEqual(len(self._beacon_calls), 1)
        correction, sess = self._beacon_calls[0]
        self.assertIn('APPROVAL_REQUEST', correction)
        self.assertIn('canonical', correction.lower())
        self.assertEqual(sess, 'sess-original')

        # Session was rotated to the re-emit's new session id.
        self.assertEqual(self.bot._bot_state['sessions']['999'], 'sess-new')

        # The recovered payload flowed through the approval path (reject
        # branch) — the raw malformed reply was NOT forwarded to Larry.
        sent_texts = [t for _, t in self._sent]
        self.assertIn('REJECTED', sent_texts)
        self.assertNotIn(bad_reply, sent_texts)

    # ---- exhaustion: malformed every time → forward+warn, never silent --

    def test_malformed_marker_exhaustion_falls_back_loud(self):
        """After MAX_APPROVAL_MARKER_RETRIES failed re-emits, fall back to
        forwarding the raw reply + a loud warning — never a silent drop."""
        chat_id = 999
        bad_reply = "Verdict: approve. (no canonical block at all)"

        def always_bad(reply):
            raise self.approval.MalformedApprovalMarker('still no block')

        self.approval.extract_approval_request = always_bad

        def fake_call_beacon(prompt, session_id):
            self._beacon_calls.append((prompt, session_id))
            return bad_reply, session_id

        self.bot.call_beacon = fake_call_beacon

        self.bot._send_beacon_response(chat_id, bad_reply)

        # Re-prompted exactly MAX times (the (n+1)-th parse failure is the
        # one that trips the exhaustion fallback, so no further re-call).
        self.assertEqual(
            len(self._beacon_calls), self.bot.MAX_APPROVAL_MARKER_RETRIES
        )

        # Loud fallback: the raw reply is forwarded AND a malformed-marker
        # warning is sent. Never silently dropped.
        sent_texts = [t for _, t in self._sent]
        self.assertIn(bad_reply, sent_texts)
        self.assertTrue(
            any('malformed' in t.lower() for t in sent_texts),
            f"expected a loud malformed-marker warning, got: {sent_texts}",
        )

    # ---- happy path: a clean marker never triggers a kickback ----------

    def test_clean_marker_no_kickback(self):
        """A well-formed canonical block parses on the first try — Beacon is
        never re-prompted."""
        chat_id = 999
        good_reply = "n\n=== APPROVAL_REQUEST ===\n{}\n=== END_APPROVAL_REQUEST ==="

        self.approval.extract_approval_request = lambda reply: (
            {'task_id': 'plan-002'}, 'narrative'
        )
        self.bot.call_beacon = lambda prompt, sid: (_ for _ in ()).throw(
            AssertionError('call_beacon must not be invoked on a clean marker')
        )
        self.approval.trust_decision = lambda payload: ('reject', {})
        self.approval.format_policy_rejection = lambda payload, rule: 'REJECTED'

        self.bot._send_beacon_response(chat_id, good_reply)

        self.assertEqual(self._beacon_calls, [])


if __name__ == '__main__':
    unittest.main()
