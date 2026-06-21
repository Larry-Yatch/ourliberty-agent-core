#!/usr/bin/env python3
"""Tests for launch_dedup_guard.py — the pure-filesystem duplicate-work guard
consulted by the board Launch drain (the 2026-06-20 cross-identity redundant-
build incident).

Three behaviours, each pinned below:

  * ClaimLedgerTest — record_claim appends a well-formed row (and refuses junk);
    find_matching_claims matches this launch's id forms, respects the recency
    window, and fails safe on a missing/garbage ledger.
  * InflightOverlapTest — find_inflight_spec_overlap flags only OTHER, LIVE
    sequences that share the same spec_doc; self / terminal / different-spec are
    excluded; a missing dir is safe.
  * EvaluateTest — a matching claim → skip_duplicate; an overlap alone →
    proceed-with-advisory; neither → clean proceed; a claim outranks an overlap.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_launch_dedup_guard
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import launch_dedup_guard as ldg  # noqa: E402

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _entry(phase_id='slice-1-state-log', *, project_id=None,
           spec_ref='agents/beacon/specs/slice-1.md', seq_id=None):
    return {
        'phase_id': phase_id,
        'project_id': project_id if project_id is not None else phase_id,
        'seq_id': seq_id or f'launch-{phase_id}',
        'spec_ref': spec_ref,
    }


def _seq(seq_id, *, status='active', spec_doc='agents/beacon/specs/other.md'):
    """A minimal sequence file body (only the fields the overlap scan reads)."""
    return {'seq_id': seq_id, 'status': status, 'spec_doc': spec_doc, 'steps': []}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dedup-guard-'))
        self.sequences_dir = self.tmp / 'build-sequences'
        self.sequences_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = self.tmp / 'deliverable-claims.jsonl'

    def _write_seq(self, seq: dict) -> Path:
        p = self.sequences_dir / f'{seq["seq_id"]}.json'
        p.write_text(json.dumps(seq, indent=2) + '\n')
        return p


# ==================== claims ledger ====================


class ClaimLedgerTest(_Base):
    def test_record_claim_appends_wellformed_row(self):
        ok = ldg.record_claim(
            claimed_task_id='slice-1-state-log',
            envelope_task_id='the-standing-brain',
            agent='forge', target_repo='ourliberty-agent-core',
            source='marker-task_id-mismatch', path=self.ledger, now=_NOW,
        )
        self.assertTrue(ok)
        rows = [json.loads(ln) for ln in self.ledger.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['claimed_task_id'], 'slice-1-state-log')
        self.assertEqual(rows[0]['envelope_task_id'], 'the-standing-brain')
        self.assertEqual(rows[0]['agent'], 'forge')
        self.assertIn('ts', rows[0])

    def test_record_claim_refuses_non_string_claimed_id(self):
        self.assertFalse(ldg.record_claim(
            claimed_task_id=None, envelope_task_id='x', path=self.ledger))
        self.assertFalse(ldg.record_claim(
            claimed_task_id='   ', envelope_task_id='x', path=self.ledger))
        self.assertFalse(self.ledger.exists())

    def test_find_matching_claims_matches_phase_project_and_seq_forms(self):
        # A claim naming the bare phase/project id matches a launch of that id.
        ldg.record_claim(claimed_task_id='slice-1-state-log',
                         envelope_task_id='the-standing-brain',
                         path=self.ledger, now=_NOW)
        m = ldg.find_matching_claims(_entry('slice-1-state-log'),
                                     path=self.ledger, now=_NOW)
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]['envelope_task_id'], 'the-standing-brain')

    def test_find_matching_claims_matches_seq_stripped_of_launch_prefix(self):
        # A claim naming the `launch-<phase>` seq form also matches.
        ldg.record_claim(claimed_task_id='launch-slice-1-state-log',
                         envelope_task_id='umbrella', path=self.ledger, now=_NOW)
        m = ldg.find_matching_claims(_entry('slice-1-state-log'),
                                     path=self.ledger, now=_NOW)
        self.assertEqual(len(m), 1)

    def test_find_matching_claims_ignores_unrelated_and_envelope_ids(self):
        # The launch's OWN id is `slice-1-state-log`; a claim for a different id,
        # or one only matching the *envelope* id, must NOT match.
        ldg.record_claim(claimed_task_id='some-other-phase',
                         envelope_task_id='slice-1-state-log',
                         path=self.ledger, now=_NOW)
        m = ldg.find_matching_claims(_entry('slice-1-state-log'),
                                     path=self.ledger, now=_NOW)
        self.assertEqual(m, [])

    def test_find_matching_claims_does_not_match_project_id_only(self):
        # A claim naming a multi-phase project's PROJECT id (distinct from the
        # phase id) must NOT match — matching it would hold unrelated projects
        # that reuse the slug as a phase id (the cross-project collision risk).
        ldg.record_claim(claimed_task_id='umbrella-project',
                         envelope_task_id='x', path=self.ledger, now=_NOW)
        e = _entry('a-distinct-phase', project_id='umbrella-project')
        self.assertEqual(ldg.find_matching_claims(e, path=self.ledger, now=_NOW), [])

    def test_find_matching_claims_excludes_stale_claims(self):
        old = _NOW - timedelta(hours=48)
        ldg.record_claim(claimed_task_id='slice-1-state-log',
                         envelope_task_id='umbrella', path=self.ledger, now=old)
        m = ldg.find_matching_claims(
            _entry('slice-1-state-log'), path=self.ledger,
            window_sec=ldg.DEFAULT_CLAIM_WINDOW_SEC, now=_NOW)
        self.assertEqual(m, [])

    def test_find_matching_claims_failsafe_on_missing_and_garbage(self):
        self.assertEqual(
            ldg.find_matching_claims(_entry(), path=self.tmp / 'nope.jsonl', now=_NOW),
            [])
        self.ledger.write_text('not json\n{"ts": "bad"}\n\n')
        self.assertEqual(
            ldg.find_matching_claims(_entry(), path=self.ledger, now=_NOW), [])

    def test_find_matching_claims_derives_ledger_from_sequences_dir(self):
        # No explicit path → derive `<blackboard>/deliverable-claims.jsonl`.
        derived = ldg.claims_path_for_sequences_dir(self.sequences_dir)
        ldg.record_claim(claimed_task_id='slice-1-state-log',
                         envelope_task_id='umbrella', path=derived, now=_NOW)
        m = ldg.find_matching_claims(_entry('slice-1-state-log'),
                                     sequences_dir=self.sequences_dir, now=_NOW)
        self.assertEqual(len(m), 1)


# ==================== in-flight spec overlap ====================


class InflightOverlapTest(_Base):
    def test_flags_other_live_sequence_with_same_spec(self):
        self._write_seq(_seq('launch-other-phase', status='active',
                             spec_doc='agents/beacon/specs/slice-1.md'))
        out = ldg.find_inflight_spec_overlap(
            _entry('slice-1-state-log', spec_ref='agents/beacon/specs/slice-1.md'),
            sequences_dir=self.sequences_dir)
        self.assertEqual(out, ['launch-other-phase'])

    def test_excludes_self(self):
        self._write_seq(_seq('launch-slice-1-state-log', status='active',
                             spec_doc='agents/beacon/specs/slice-1.md'))
        out = ldg.find_inflight_spec_overlap(
            _entry('slice-1-state-log', spec_ref='agents/beacon/specs/slice-1.md'),
            sequences_dir=self.sequences_dir)
        self.assertEqual(out, [])

    def test_excludes_terminal_status(self):
        for st in ('complete', 'failed', 'archived', 'paused'):
            self._write_seq(_seq(f'launch-{st}-phase', status=st,
                                 spec_doc='agents/beacon/specs/slice-1.md'))
        out = ldg.find_inflight_spec_overlap(
            _entry('slice-1-state-log', spec_ref='agents/beacon/specs/slice-1.md'),
            sequences_dir=self.sequences_dir)
        self.assertEqual(out, [])

    def test_excludes_different_spec(self):
        self._write_seq(_seq('launch-other', status='active',
                             spec_doc='agents/beacon/specs/unrelated.md'))
        out = ldg.find_inflight_spec_overlap(
            _entry('slice-1-state-log', spec_ref='agents/beacon/specs/slice-1.md'),
            sequences_dir=self.sequences_dir)
        self.assertEqual(out, [])

    def test_no_spec_ref_or_missing_dir_is_safe(self):
        self.assertEqual(
            ldg.find_inflight_spec_overlap(
                _entry(spec_ref=''), sequences_dir=self.sequences_dir), [])
        self.assertEqual(
            ldg.find_inflight_spec_overlap(
                _entry(), sequences_dir=self.tmp / 'gone'), [])


# ==================== evaluate ====================


class EvaluateTest(_Base):
    def test_matching_claim_yields_skip_duplicate(self):
        ldg.record_claim(claimed_task_id='slice-1-state-log',
                         envelope_task_id='the-standing-brain',
                         path=ldg.claims_path_for_sequences_dir(self.sequences_dir),
                         now=_NOW)
        v = ldg.evaluate(_entry('slice-1-state-log'),
                         sequences_dir=self.sequences_dir, now=_NOW)
        self.assertEqual(v.action, 'skip_duplicate')
        self.assertTrue(v.has_signal)
        self.assertIn('the-standing-brain', v.reason)

    def test_overlap_only_yields_proceed_with_advisory(self):
        self._write_seq(_seq('launch-other', status='active',
                             spec_doc='agents/beacon/specs/slice-1.md'))
        v = ldg.evaluate(
            _entry('slice-1-state-log', spec_ref='agents/beacon/specs/slice-1.md'),
            sequences_dir=self.sequences_dir, now=_NOW)
        self.assertEqual(v.action, 'proceed')
        self.assertTrue(v.has_signal)
        self.assertEqual(v.overlapping_seqs, ['launch-other'])

    def test_clean_launch_proceeds_with_no_signal(self):
        v = ldg.evaluate(_entry('slice-1-state-log'),
                         sequences_dir=self.sequences_dir, now=_NOW)
        self.assertEqual(v.action, 'proceed')
        self.assertFalse(v.has_signal)

    def test_claim_outranks_overlap(self):
        self._write_seq(_seq('launch-other', status='active',
                             spec_doc='agents/beacon/specs/slice-1.md'))
        ldg.record_claim(claimed_task_id='slice-1-state-log',
                         envelope_task_id='umbrella',
                         path=ldg.claims_path_for_sequences_dir(self.sequences_dir),
                         now=_NOW)
        v = ldg.evaluate(
            _entry('slice-1-state-log', spec_ref='agents/beacon/specs/slice-1.md'),
            sequences_dir=self.sequences_dir, now=_NOW)
        self.assertEqual(v.action, 'skip_duplicate')
        self.assertEqual(v.overlapping_seqs, ['launch-other'])


if __name__ == '__main__':
    unittest.main()
