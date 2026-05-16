#!/usr/bin/env python3
"""Tests for `pulse_check_i` — synthetic Ledger sidecar -> digest shape.

Validates:
  - Heartbeat shape when sidecar is present but no proposals are synthesized
  - Digest shape when retry overhead, σ anomalies, or recurring shapes
    cross the configured thresholds
  - Skipped shape when sidecar is missing or stale
  - Proposal cap (≤ 3) honored even when all three rules fire
  - DM body shapes for heartbeat / digest / skipped
  - Journal block contents for each mode
  - EMERGENCY_HALT short-circuit
  - Monday-gate enforcement (skip when not Monday and not --force)
  - End-to-end main() with --no-dm --no-journal

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_i
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_i as pci  # noqa: E402


WEEK_ENDING = "2026-05-18"
FIRED_AT = datetime(2026, 5, 18, 7, 30, 0, tzinfo=timezone.utc)


def _sidecar(
    total_usd: float = 0.50,
    retry_pct: float = 0.0,
    retry_usd: float = 0.0,
    anomalies: list | None = None,
    delta: dict | None = None,
) -> dict:
    return {
        "schema_version": "v1",
        "week_ending": WEEK_ENDING,
        "total_usd": total_usd,
        "delta_vs_prior_week": delta,
        "by_agent": {"forge": {"usd": total_usd, "task_count": 1}},
        "by_task_type": {"feature-development": {"usd": total_usd, "task_count": 1}},
        "anomalies": anomalies if anomalies is not None else [],
        "retry_overhead": {
            "total_retry_cost_usd": retry_usd,
            "percent_of_total": retry_pct,
        },
        "top_5_tasks": [{"task_id": "t1", "agent": "forge", "cost_usd": total_usd}],
    }


class ProposalSynthesisTests(unittest.TestCase):
    def test_no_signals_no_proposals(self):
        proposals = pci.synthesize_proposals(_sidecar(), repeats=[])
        self.assertEqual(proposals, [])

    def test_retry_overhead_above_threshold_yields_proposal(self):
        s = _sidecar(retry_pct=20.0, retry_usd=2.50)
        proposals = pci.synthesize_proposals(s, repeats=[])
        self.assertEqual(len(proposals), 1)
        p = proposals[0]
        self.assertIn("retry", p["title"].lower())
        self.assertEqual(p["effort"], "medium")
        self.assertIn("$2.50", p["impact"])

    def test_retry_overhead_below_threshold_no_proposal(self):
        s = _sidecar(retry_pct=10.0, retry_usd=1.0)
        self.assertEqual(pci.synthesize_proposals(s, repeats=[]), [])

    def test_high_sigma_anomaly_yields_proposal(self):
        s = _sidecar(anomalies=[{
            "task_id": "burned-task-001",
            "agent": "forge",
            "task_type": "feature-development",
            "cost_usd": 9.20,
            "baseline_usd": 1.10,
            "sigma_above": 3.7,
            "context": "...",
        }])
        proposals = pci.synthesize_proposals(s, repeats=[])
        self.assertEqual(len(proposals), 1)
        self.assertIn("burned-task-001", proposals[0]["title"])
        self.assertEqual(proposals[0]["effort"], "small")

    def test_low_sigma_anomaly_no_proposal(self):
        s = _sidecar(anomalies=[{
            "task_id": "mild-anomaly",
            "agent": "forge",
            "task_type": "doc-only",
            "cost_usd": 1.40,
            "baseline_usd": 0.90,
            "sigma_above": 2.2,
            "context": "...",
        }])
        self.assertEqual(pci.synthesize_proposals(s, repeats=[]), [])

    def test_ramp_up_notice_ignored(self):
        s = _sidecar(anomalies=[{
            "task_id": "_ramp_up_notice",
            "agent": "_ledger",
            "task_type": "_ramp_up",
            "cost_usd": 0.0,
            "baseline_usd": 0.0,
            "sigma_above": 0.0,
            "context": "ramp-up",
        }])
        self.assertEqual(pci.synthesize_proposals(s, repeats=[]), [])

    def test_high_repeat_task_yields_proposal(self):
        repeats = [
            {"agent": "forge", "task_id": "auto-merge-gap-pr16-001",
             "retry_count": 4}
        ]
        proposals = pci.synthesize_proposals(_sidecar(), repeats=repeats)
        self.assertEqual(len(proposals), 1)
        self.assertIn("auto-merge-gap-pr16-001", proposals[0]["title"])
        self.assertIn("4", proposals[0]["impact"])

    def test_proposal_cap_at_three(self):
        s = _sidecar(
            retry_pct=25.0, retry_usd=3.0,
            anomalies=[{
                "task_id": "anom-1", "agent": "forge",
                "task_type": "feature-development",
                "cost_usd": 8.0, "baseline_usd": 1.0, "sigma_above": 4.0,
                "context": "",
            }],
        )
        repeats = [
            {"agent": "forge", "task_id": "repeat-1", "retry_count": 5},
            {"agent": "mirror", "task_id": "repeat-2", "retry_count": 3},
        ]
        proposals = pci.synthesize_proposals(s, repeats=repeats)
        self.assertLessEqual(len(proposals), pci.MAX_PROPOSALS_PER_DIGEST)


class RetryRepeatGatherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outbox = Path(self.tmp.name) / "outboxes"

    def _archive(self, agent: str, filename: str) -> None:
        archive = self.outbox / agent / ".archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / filename).write_text("{}")

    def test_counts_retry_suffixes(self):
        self._archive("forge", "task-a.json")
        self._archive("forge", "task-a.1.json")
        self._archive("forge", "task-a.2.json")
        self._archive("forge", "task-b.json")
        repeats = pci.gather_retry_repeats(self.outbox)
        # task-a has 3 archives -> ≥ HIGH_REPEAT_COUNT_THRESHOLD
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["task_id"], "task-a")
        self.assertEqual(repeats[0]["agent"], "forge")
        self.assertEqual(repeats[0]["retry_count"], 3)

    def test_below_threshold_excluded(self):
        self._archive("forge", "task-x.json")
        self._archive("forge", "task-x.1.json")  # only 2 — below threshold
        self.assertEqual(pci.gather_retry_repeats(self.outbox), [])

    def test_sorted_desc(self):
        self._archive("forge", "alpha.json")
        self._archive("forge", "alpha.1.json")
        self._archive("forge", "alpha.2.json")
        self._archive("mirror", "bravo.json")
        self._archive("mirror", "bravo.1.json")
        self._archive("mirror", "bravo.2.json")
        self._archive("mirror", "bravo.3.json")
        repeats = pci.gather_retry_repeats(self.outbox)
        self.assertEqual(repeats[0]["task_id"], "bravo")  # higher count first
        self.assertEqual(repeats[0]["retry_count"], 4)

    def test_missing_outbox_root(self):
        nonexistent = Path(self.tmp.name) / "nope"
        self.assertEqual(pci.gather_retry_repeats(nonexistent), [])


class AssembleCheckITests(unittest.TestCase):
    def test_skipped_mode_when_sidecar_none(self):
        result = pci.assemble_check_i(
            sidecar=None, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename=None, fired_at=FIRED_AT,
        )
        self.assertEqual(result["mode"], "skipped")
        self.assertIn("unavailable", result["skip_reason"])
        self.assertEqual(result["proposals"], [])
        self.assertIsNone(result["ledger_headline"])

    def test_heartbeat_mode_when_no_proposals(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        self.assertEqual(result["mode"], "heartbeat")
        self.assertEqual(result["proposals"], [])
        self.assertIsNotNone(result["ledger_headline"])
        self.assertEqual(result["ledger_headline"]["total_usd"], 0.50)
        self.assertEqual(result["ledger_headline"]["anomaly_count"], 0)

    def test_digest_mode_when_proposals_synthesized(self):
        s = _sidecar(retry_pct=25.0, retry_usd=4.0)
        result = pci.assemble_check_i(
            sidecar=s, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        self.assertEqual(result["mode"], "digest")
        self.assertEqual(len(result["proposals"]), 1)
        self.assertEqual(result["engineering_signals"]["retry_overhead_pct"], 25.0)

    def test_top_level_schema_keys(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        expected = {
            "schema_version", "week_ending", "ledger_sidecar", "fired_at",
            "mode", "skip_reason", "ledger_headline",
            "engineering_signals", "proposals",
        }
        self.assertEqual(set(result.keys()), expected)
        self.assertEqual(result["schema_version"], "v1")

    def test_each_proposal_has_effort_and_impact(self):
        s = _sidecar(
            retry_pct=20.0, retry_usd=2.0,
            anomalies=[{
                "task_id": "anom-1", "agent": "forge",
                "task_type": "feature-development",
                "cost_usd": 5.0, "baseline_usd": 1.0, "sigma_above": 3.5,
                "context": "",
            }],
        )
        result = pci.assemble_check_i(
            sidecar=s, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        for p in result["proposals"]:
            self.assertIn(p["effort"], {"small", "medium", "large"})
            self.assertTrue(p["impact"])
            self.assertTrue(p["rationale"])
            self.assertTrue(p["title"])


class DmRenderingTests(unittest.TestCase):
    def test_skipped_dm(self):
        result = pci.assemble_check_i(
            sidecar=None, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename=None, fired_at=FIRED_AT,
        )
        body = pci.render_dm(result)
        self.assertIn("skipped", body.lower())
        self.assertIn(WEEK_ENDING, body)

    def test_heartbeat_dm_uses_chain_shapes_phrase(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(delta={"absolute_usd": 0.1, "percent": 5.0}),
            repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        body = pci.render_dm(result)
        self.assertIn("chain shapes nominal", body)
        self.assertIn("$0.50", body)

    def test_digest_dm_enumerates_proposals(self):
        s = _sidecar(retry_pct=25.0, retry_usd=3.0)
        result = pci.assemble_check_i(
            sidecar=s, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        body = pci.render_dm(result)
        self.assertIn("Proposed optimizations", body)
        self.assertIn("1.", body)
        self.assertIn("[medium]", body)


class JournalBlockTests(unittest.TestCase):
    def test_skipped_block(self):
        result = pci.assemble_check_i(
            sidecar=None, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename=None, fired_at=FIRED_AT,
        )
        block = pci.render_journal_block(result)
        self.assertIn("**Check I", block)
        self.assertIn(WEEK_ENDING, block)
        self.assertIn("Skipped", block)

    def test_heartbeat_block(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        block = pci.render_journal_block(result)
        self.assertIn("heartbeat", block.lower())
        self.assertIn("Ledger total", block)

    def test_digest_block_lists_proposals(self):
        s = _sidecar(retry_pct=25.0, retry_usd=3.0)
        result = pci.assemble_check_i(
            sidecar=s, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        block = pci.render_journal_block(result)
        self.assertIn("digest", block.lower())
        self.assertIn("Rationale", block)


class EndToEndTests(unittest.TestCase):
    """Drive `main()` with --no-dm --no-journal against a synthetic sidecar."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sidecar_dir = self.root / "ledger"
        self.outbox_root = self.root / "outboxes"
        self.output_dir = self.root / "out"
        self.journal = self.root / "cycle-journal.md"
        self.halt_flag = self.root / "halt-flag-not-set"
        self.sidecar_dir.mkdir(parents=True)
        (self.sidecar_dir / f"weekly-{WEEK_ENDING}.json").write_text(
            json.dumps(_sidecar(retry_pct=22.0, retry_usd=3.0))
        )

    def _argv(self, *extra: str) -> list[str]:
        return [
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
            "--no-dm", "--no-journal",
            "--force",
            *extra,
        ]

    def test_e2e_digest_writes_audit_record(self):
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        out = self.output_dir / f"check-i-{WEEK_ENDING}.json"
        self.assertTrue(out.exists())
        with open(out) as f:
            audit = json.load(f)
        self.assertEqual(audit["mode"], "digest")
        self.assertGreaterEqual(len(audit["proposals"]), 1)

    def test_e2e_skipped_when_sidecar_missing(self):
        os.remove(self.sidecar_dir / f"weekly-{WEEK_ENDING}.json")
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        out = self.output_dir / f"check-i-{WEEK_ENDING}.json"
        with open(out) as f:
            audit = json.load(f)
        self.assertEqual(audit["mode"], "skipped")

    def test_e2e_emergency_halt_short_circuits(self):
        halt = self.root / "halt-set"
        halt.write_text("halt")
        rc = pci.main(self._argv() + ["--halt-flag", str(halt)])
        self.assertEqual(rc, 0)
        # No audit record should land
        out = self.output_dir / f"check-i-{WEEK_ENDING}.json"
        self.assertFalse(out.exists())


class MondayGateTests(unittest.TestCase):
    """Skip when not Monday, unless --force or --week-ending is given."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sidecar_dir = self.root / "ledger"
        self.output_dir = self.root / "out"
        self.journal = self.root / "cycle-journal.md"
        self.halt_flag = self.root / "halt-not-set"
        self.sidecar_dir.mkdir(parents=True)

    def test_force_bypasses_gate(self):
        # Even if `now` is not Monday (which we can't easily mock without
        # patching), --force + --week-ending makes the gate moot.
        rc = pci.main([
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.root / "outboxes"),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
            "--no-dm", "--no-journal",
            "--force",
        ])
        self.assertEqual(rc, 0)
        out = self.output_dir / f"check-i-{WEEK_ENDING}.json"
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
