#!/usr/bin/env python3
"""Tests for pulse_envelope_builder — the Pulse-facing canonical envelope builder
that kills the F24 body-vs-prompt dead-letter class (task pulse-envelope-builder-001).

The builder OWNS the `prompt` field name: a caller passes the prompt TEXT and can
never name the key `body`. Construction fail-fast validates via
dispatch_validator.validate_task; the write path delegates to safe_write_inbox.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_pulse_envelope_builder
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import dispatch_validator as dv      # noqa: E402
import pulse_envelope_builder as peb  # noqa: E402
import routing_validator as rv       # noqa: E402
import safe_write_inbox as swi       # noqa: E402

# A prompt comfortably over MIN_PROMPT_LEN (100 chars).
_GOOD_PROMPT = (
    'Pulse observed the cycle-fix dead-letter recurrence across iters 1542-1543. '
    'Evidence: three envelopes used body instead of prompt. Proposed fix: route '
    'construction through a builder that owns the prompt field. Acceptance: F24 '
    'cannot recur from the builder path.'
)


class BuildEnvelopeConstructionTest(unittest.TestCase):
    """Pure construction — no filesystem, so the inbox-write guard never fires."""

    def test_output_carries_prompt_never_body(self):
        env = peb.build_envelope('cycle-finding-foo-001', _GOOD_PROMPT)
        self.assertEqual(env['prompt'], _GOOD_PROMPT)
        self.assertNotIn('body', env)
        # Canonical field set + default source.
        self.assertEqual(env['task_id'], 'cycle-finding-foo-001')
        self.assertEqual(env['source'], 'pulse')

    def test_optional_fields_only_present_when_given(self):
        env = peb.build_envelope('t-001', _GOOD_PROMPT)
        self.assertNotIn('dedup_identity', env)
        self.assertNotIn('timeout', env)
        env2 = peb.build_envelope(
            't-002', _GOOD_PROMPT, dedup_identity='cycle-finding:foo', timeout=3600,
        )
        self.assertEqual(env2['dedup_identity'], 'cycle-finding:foo')
        self.assertEqual(env2['timeout'], 3600)

    def test_well_formed_envelope_passes_validate_task(self):
        env = peb.build_envelope(
            't-003', _GOOD_PROMPT, dedup_identity='cycle-finding:foo', timeout=3600,
        )
        ok, reason = dv.validate_task(env)
        self.assertTrue(ok, reason)

    def test_short_prompt_rejected_at_construction(self):
        with self.assertRaises(peb.EnvelopeValidationError) as ctx:
            peb.build_envelope('t-004', 'too short')
        # The F24 diagnostic surfaces the real reason.
        self.assertIn('too short', str(ctx.exception))

    def test_bad_source_rejected_at_construction(self):
        with self.assertRaises(peb.EnvelopeValidationError):
            peb.build_envelope('t-005', _GOOD_PROMPT, source='not-a-real-source')


class ReplyChatIdStampTest(unittest.TestCase):
    """Direction-ask envelopes must resolve reply_chat_id at creation so the
    outbox-notifier null-reply-chat-id fallback (the G-rule log line) never
    fires. Mirrors PR #933's coverage for the sibling pulse_check_i builder."""

    def test_reply_chat_id_stamped_when_allow_list_set(self):
        with mock.patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '7998341473'}):
            env = peb.build_envelope('t-chat-001', _GOOD_PROMPT)
        self.assertEqual(env['reply_chat_id'], 7998341473)

    def test_reply_chat_id_key_absent_when_allow_list_empty(self):
        with mock.patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': ''}):
            env = peb.build_envelope('t-chat-002', _GOOD_PROMPT)
        # Never present-but-null — the key is omitted so the notifier fallback
        # stays intact for the degenerate (unset allow-list) case.
        self.assertNotIn('reply_chat_id', env)

    def test_reply_chat_id_key_absent_when_allow_list_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TELEGRAM_ALLOWED_CHAT_IDS', None)
            env = peb.build_envelope('t-chat-003', _GOOD_PROMPT)
        self.assertNotIn('reply_chat_id', env)

    def test_lowest_id_wins_with_multiple_allow_listed(self):
        # Parity with _primary_chat_id: comma-or-space separated, min() wins.
        with mock.patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': '8000000000, 7998341473 9000000000'}):
            env = peb.build_envelope('t-chat-004', _GOOD_PROMPT)
        self.assertEqual(env['reply_chat_id'], 7998341473)


class WriteEnvelopeSmokeTest(unittest.TestCase):
    """Exercises the full build -> safe_write_inbox path against a temp inbox
    root, the way safe_write_inbox._self_test does. The module-level
    setUpModule disengages the Layer-B chokepoint guard so the real write runs;
    the #428 real-tree leak scanner still asserts nothing escaped to ~/agents."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._orig = (swi.AGENTS_ROOT, swi.INBOXES_ROOT, swi.ROUTING_EVENTS_LOG)
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        swi.ROUTING_EVENTS_LOG = self._root / 'logs' / 'routing-events.jsonl'
        self._orig_rv = rv.REPO_ROOT
        rv.REPO_ROOT = self._root / 'repo'
        rv.invalidate_cache()

    def tearDown(self):
        swi.AGENTS_ROOT, swi.INBOXES_ROOT, swi.ROUTING_EVENTS_LOG = self._orig
        rv.REPO_ROOT = self._orig_rv
        rv.invalidate_cache()
        self._tmp.cleanup()

    def test_write_to_beacon_uses_task_id_filename(self):
        # pulse -> beacon is the valid topology route for a Pulse source.
        dest = peb.write_envelope(
            'beacon',
            'cycle-finding-bar-20260612T000000Z',
            _GOOD_PROMPT,
            dedup_identity='cycle-finding:bar',
            timeout=3600,
        )
        self.assertEqual(dest.name, 'cycle-finding-bar-20260612T000000Z.json')
        self.assertEqual(dest.parent.name, 'beacon')
        self.assertTrue(dest.exists())
        import json
        written = json.loads(dest.read_text())
        self.assertEqual(written['prompt'], _GOOD_PROMPT)
        self.assertNotIn('body', written)

    def test_short_prompt_writes_no_inbox_file(self):
        with self.assertRaises(peb.EnvelopeValidationError):
            peb.write_envelope('beacon', 'cycle-finding-baz-001', 'tiny')
        # No file landed anywhere under the temp inbox root.
        self.assertEqual(list((self._root).rglob('*.json')), [])


if __name__ == '__main__':
    unittest.main()


try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    # WriteEnvelopeSmokeTest drives the Layer-B-guarded inbox-write chokepoint
    # against already-isolated (temp-dir) state, so the guard would breach before
    # the test's own redirection. Opt out for the module so the guard is a
    # pass-through; the #428 real-tree leak scanner still runs.
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()


def tearDownModule():  # noqa: N802 — unittest hook
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)
