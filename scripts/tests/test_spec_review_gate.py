#!/usr/bin/env python3
"""Tests for the spec-gauntlet intercept + deferred-stamp machinery.

Covers agents/beacon/specs/spec-gauntlet-gate.md §3.1/§3.5 — the host-agnostic
half of the gate that ``spec_review_gate`` owns:
  - ``intercept`` spools payload + routing sidecar and returns spooled/duplicate/
    disabled (AC-3 live-off, AC-6 host-restart replay guard),
  - the challenge digest builder + summary-append,
  - ``collect_concluded`` / ``mark_stamped`` deferred pickup.

Everything runs against a per-test tmp state tree (the module dir attrs are
redirected in setUp) with ``config.is_enabled`` / ``config.gated_sites`` stubbed
— no runner, no daemon, no live state.

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_spec_review_gate
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import spec_review_config as config  # noqa: E402
import spec_review_gate as gate  # noqa: E402


def _payload(task_id='t-1', summary='do the thing', **extra):
    p = {'task_id': task_id, 'summary': summary, 'target_agent': 'forge'}
    p.update(extra)
    return p


class GateTestBase(unittest.TestCase):
    """Redirect the gate's five state dirs at a fresh tmp tree and stub config
    to enabled + all three sites gated. Each test starts from an empty spool."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_dirs = (
            gate.STATE_DIR, gate.PENDING_DIR, gate.CONCLUDED_DIR,
            gate.ROUTING_DIR, gate.STAMPED_DIR)
        gate.STATE_DIR = root
        gate.PENDING_DIR = root / 'pending'
        gate.CONCLUDED_DIR = root / 'concluded'
        gate.ROUTING_DIR = root / 'routing'
        gate.STAMPED_DIR = root / 'stamped'

        self._orig_cfg = (config.is_enabled, config.gated_sites)
        config.is_enabled = lambda: True
        config.gated_sites = lambda: ['bot_chat', 'replan', 'pulse_auto_dispatch']

        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self):
        (gate.STATE_DIR, gate.PENDING_DIR, gate.CONCLUDED_DIR,
         gate.ROUTING_DIR, gate.STAMPED_DIR) = self._orig_dirs
        (config.is_enabled, config.gated_sites) = self._orig_cfg

    def _write_conclusion(self, task_id, **fields):
        gate.CONCLUDED_DIR.mkdir(parents=True, exist_ok=True)
        conc = {'task_id': task_id, 'terminal_state': 'passed', 'rounds': 1}
        conc.update(fields)
        (gate.CONCLUDED_DIR / f'{task_id}.json').write_text(json.dumps(conc))
        return conc


class InterceptTest(GateTestBase):
    def test_spooled_writes_pending_and_routing(self):
        p = _payload('task-a')
        outcome = gate.intercept(
            p, 'bot_chat', chat_id=42,
            meta={'inherited_replan_count': 1})
        self.assertEqual(outcome, 'spooled')

        pending = json.loads((gate.PENDING_DIR / 'task-a.json').read_text())
        self.assertEqual(pending['payload'], p)
        self.assertEqual(pending['site'], 'bot_chat')
        self.assertEqual(pending['payload_hash'], gate.payload_hash(p))

        routing = json.loads((gate.ROUTING_DIR / 'task-a.json').read_text())
        self.assertEqual(routing['chat_id'], 42)
        self.assertEqual(routing['site'], 'bot_chat')
        self.assertEqual(routing['meta'], {'inherited_replan_count': 1})

    def test_disabled_when_gate_off(self):
        config.is_enabled = lambda: False
        self.assertEqual(gate.intercept(_payload(), 'bot_chat'), 'disabled')
        self.assertFalse(gate.PENDING_DIR.exists() and any(gate.PENDING_DIR.iterdir()))

    def test_disabled_when_site_not_gated(self):
        # A live gate still doesn't touch an ungated site (e.g. no_session).
        self.assertEqual(gate.intercept(_payload(), 'no_session'), 'disabled')

    def test_duplicate_when_already_pending(self):
        p = _payload('task-dup')
        self.assertEqual(gate.intercept(p, 'replan'), 'spooled')
        # Same (task_id, body) again — host-restart replay (AC-6): no re-spool.
        self.assertEqual(gate.intercept(p, 'replan'), 'duplicate')

    def test_changed_body_is_not_masked_by_stale_pending(self):
        self.assertEqual(gate.intercept(_payload('task-x', 'v1'), 'replan'), 'spooled')
        # A genuinely different spec under the same id must re-spool, not dedup.
        self.assertEqual(gate.intercept(_payload('task-x', 'v2'), 'replan'), 'spooled')

    def test_duplicate_when_concluded_matches(self):
        p = _payload('task-c')
        self._write_conclusion(
            'task-c', payload_hash=gate.payload_hash(p),
            terminal_state='passed')
        self.assertEqual(gate.intercept(p, 'bot_chat'), 'duplicate')

    def test_concluded_wrong_hash_does_not_dedup(self):
        p = _payload('task-c2', 'new body')
        self._write_conclusion(
            'task-c2', payload_hash='stale-hash', terminal_state='passed')
        self.assertEqual(gate.intercept(p, 'bot_chat'), 'spooled')

    def test_task_id_sanitized_for_spool_filename(self):
        outcome = gate.intercept(_payload('../../etc/passwd'), 'bot_chat')
        self.assertEqual(outcome, 'spooled')
        spooled = list(gate.PENDING_DIR.glob('*.json'))
        self.assertEqual(len(spooled), 1)
        self.assertNotIn('/', spooled[0].name)


class DigestTest(unittest.TestCase):
    def test_header_format(self):
        conc = {
            'terminal_state': 'contested', 'rounds': 2,
            'blocking_resolved_count': 1, 'contested_count': 1,
            'contested_findings': [
                {'lens': 'S-A', 'claim': 'will collide'}],
            'advisory_findings': [{'lens': 'S-C', 'claim': 'simpler'}],
        }
        digest = gate.build_digest(conc)
        lines = digest.splitlines()
        self.assertEqual(
            lines[0],
            'Gauntlet: contested · 2 rounds · 2 blocking → 1 resolved, '
            '1 contested · 1 advisory')
        self.assertIn('· contested [S-A] will collide', digest)
        self.assertIn('· advisory [S-C] simpler', digest)

    def test_errored_appends_reason(self):
        conc = {'terminal_state': 'errored', 'reason': 'runner died'}
        self.assertIn('(runner died)', gate.build_digest(conc))

    def test_with_digest_appends_and_does_not_mutate(self):
        p = {'summary': 'original'}
        out = gate.with_digest(p, 'DIGEST')
        self.assertEqual(out['summary'], 'original\n\nDIGEST')
        self.assertEqual(p['summary'], 'original')  # caller dict untouched

    def test_with_digest_empty_summary(self):
        out = gate.with_digest({}, 'DIGEST')
        self.assertEqual(out['summary'], 'DIGEST')

    def test_disabled_label(self):
        out = gate.with_disabled_label({'summary': 's'})
        self.assertIn(gate.DISABLED_LABEL, out['summary'])


class CollectConcludedTest(GateTestBase):
    def _seed(self, task_id, site, *, terminal_state='passed', stamped=False):
        self._write_conclusion(task_id, terminal_state=terminal_state,
                               final_payload=_payload(task_id))
        gate.ROUTING_DIR.mkdir(parents=True, exist_ok=True)
        (gate.ROUTING_DIR / f'{task_id}.json').write_text(
            json.dumps({'task_id': task_id, 'site': site, 'meta': {}}))
        if stamped:
            gate.mark_stamped(task_id)

    def test_returns_matching_site(self):
        self._seed('r-1', 'replan')
        self._seed('p-1', 'pulse_auto_dispatch')
        self._seed('b-1', 'bot_chat')
        got = gate.collect_concluded(['replan', 'pulse_auto_dispatch'])
        ids = sorted(t for t, _, _ in got)
        self.assertEqual(ids, ['p-1', 'r-1'])

    def test_skips_already_stamped(self):
        self._seed('r-2', 'replan', stamped=True)
        self.assertEqual(gate.collect_concluded(['replan']), [])

    def test_skips_invalid_terminal_state(self):
        self._seed('r-3', 'replan', terminal_state='in-progress')
        self.assertEqual(gate.collect_concluded(['replan']), [])

    def test_mark_stamped_is_idempotent_gate(self):
        self._seed('r-4', 'replan')
        self.assertEqual(len(gate.collect_concluded(['replan'])), 1)
        gate.mark_stamped('r-4')
        self.assertEqual(gate.collect_concluded(['replan']), [])


if __name__ == '__main__':
    unittest.main()
