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
  - Weekday-gate enforcement (skip on Tue/Thu/Sat unless --force)
  - End-to-end main() with --no-dm --no-journal

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_i
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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

    def test_no_signal_mode_when_default_sidecar(self):
        # Default sidecar has no proposals, no anomalies, no repeats, and
        # retry_pct=0 — closed-loop spec § 4 calls this no-signal.
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        self.assertEqual(result["mode"], "no-signal")
        self.assertFalse(result["has_signal"])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["ledger_headline"]["total_usd"], 0.50)
        self.assertEqual(result["ledger_headline"]["anomaly_count"], 0)

    def test_heartbeat_mode_when_sub_threshold_anomaly_present(self):
        # Sub-3σ anomaly: no proposal synthesized, but real_anoms is
        # non-empty → heartbeat (signal present, no actionable proposal).
        s = _sidecar(anomalies=[{
            "task_id": "mild-anom",
            "agent": "forge",
            "task_type": "feature-development",
            "cost_usd": 1.40,
            "baseline_usd": 0.90,
            "sigma_above": 2.2,
            "context": "",
        }])
        result = pci.assemble_check_i(
            sidecar=s, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        self.assertEqual(result["mode"], "heartbeat")
        self.assertTrue(result["has_signal"])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["ledger_headline"]["anomaly_count"], 1)

    def test_digest_mode_when_proposals_synthesized(self):
        s = _sidecar(retry_pct=25.0, retry_usd=4.0)
        result = pci.assemble_check_i(
            sidecar=s, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        self.assertEqual(result["mode"], "digest")
        self.assertTrue(result["has_signal"])
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
            "engineering_signals", "proposals", "has_signal",
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
        # Heartbeat now requires a signal alongside the absent-proposals
        # state — a sub-3σ anomaly qualifies.
        s = _sidecar(
            delta={"absolute_usd": 0.1, "percent": 5.0},
            anomalies=[{
                "task_id": "mild-anom",
                "agent": "forge",
                "task_type": "feature-development",
                "cost_usd": 1.40,
                "baseline_usd": 0.90,
                "sigma_above": 2.2,
                "context": "",
            }],
        )
        result = pci.assemble_check_i(
            sidecar=s, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        self.assertEqual(result["mode"], "heartbeat")
        body = pci.render_dm(result)
        self.assertIn("chain shapes nominal", body)
        self.assertIn("$0.50", body)

    def test_no_signal_dm_body(self):
        # /optimize (--force) on a no-signal week still renders a DM; it
        # should clearly say no signal rather than reuse the heartbeat
        # phrasing.
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        self.assertEqual(result["mode"], "no-signal")
        body = pci.render_dm(result)
        self.assertIn("no signal", body.lower())
        self.assertIn(WEEK_ENDING, body)

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
        s = _sidecar(anomalies=[{
            "task_id": "mild-anom",
            "agent": "forge",
            "task_type": "feature-development",
            "cost_usd": 1.40,
            "baseline_usd": 0.90,
            "sigma_above": 2.2,
            "context": "",
        }])
        result = pci.assemble_check_i(
            sidecar=s, repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        block = pci.render_journal_block(result)
        self.assertIn("heartbeat", block.lower())
        self.assertIn("Ledger total", block)

    def test_no_signal_block(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        block = pci.render_journal_block(result)
        self.assertIn("**Check I", block)
        self.assertIn("no-signal", block)
        self.assertIn("DM suppressed", block)

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

    def _audit_path(self) -> Path:
        # Audit filename now uses firing date (today UTC), not week_ending.
        today = datetime.now(timezone.utc).date().isoformat()
        return self.output_dir / f"check-i-{today}.json"

    def test_e2e_digest_writes_audit_record(self):
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        out = self._audit_path()
        self.assertTrue(out.exists())
        with open(out) as f:
            audit = json.load(f)
        self.assertEqual(audit["mode"], "digest")
        self.assertGreaterEqual(len(audit["proposals"]), 1)

    def test_e2e_skipped_when_sidecar_missing(self):
        os.remove(self.sidecar_dir / f"weekly-{WEEK_ENDING}.json")
        # Auto-refresh would invoke run_ledger.sh for real; mock it as a
        # no-op refresh that doesn't create a sidecar, so the skip path runs.
        with mock.patch.object(pci.subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"",
            )
            rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        out = self._audit_path()
        with open(out) as f:
            audit = json.load(f)
        self.assertEqual(audit["mode"], "skipped")

    def test_e2e_emergency_halt_short_circuits(self):
        halt = self.root / "halt-set"
        halt.write_text("halt")
        rc = pci.main(self._argv() + ["--halt-flag", str(halt)])
        self.assertEqual(rc, 0)
        # No audit record should land
        out = self._audit_path()
        self.assertFalse(out.exists())

    def test_audit_filename_uses_firing_date_not_week_ending(self):
        # Even with --week-ending pointing to 2026-05-18, the audit file
        # name should reflect today's date, so each weekly firing gets
        # its own file.
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        today = datetime.now(timezone.utc).date().isoformat()
        firing_out = self.output_dir / f"check-i-{today}.json"
        week_ending_out = self.output_dir / f"check-i-{WEEK_ENDING}.json"
        self.assertTrue(firing_out.exists())
        if today != WEEK_ENDING:
            self.assertFalse(week_ending_out.exists())


class DmSuppressionTests(unittest.TestCase):
    """Closed-loop spec § 4: scheduled no-signal runs skip the DM but
    still write the audit JSON + journal entry. /optimize (--force)
    bypasses suppression and always DMs.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sidecar_dir = self.root / "ledger"
        self.outbox_root = self.root / "outboxes"
        self.output_dir = self.root / "out"
        self.journal = self.root / "cycle-journal.md"
        self.halt_flag = self.root / "halt-not-set"
        self.sidecar_dir.mkdir(parents=True)

        # Track every append_alert invocation in this list.
        self.append_calls: list[dict] = []

        outer = self

        class _FakeLarryAlerts:
            @staticmethod
            def append_alert(**kwargs):
                outer.append_calls.append(kwargs)
                return True

        self._sys_modules_patch = mock.patch.dict(
            sys.modules, {"larry_alerts": _FakeLarryAlerts}
        )
        self._sys_modules_patch.start()
        self.addCleanup(self._sys_modules_patch.stop)

    def _write_sidecar(self, sidecar: dict) -> None:
        (self.sidecar_dir / f"weekly-{WEEK_ENDING}.json").write_text(
            json.dumps(sidecar)
        )

    def _argv(self, *extra: str) -> list[str]:
        # Note: deliberately omitting --no-dm so the DM path runs (and
        # gets intercepted by the fake larry_alerts).
        return [
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
            "--force",  # bypass weekday gate; suppression still applies
            *extra,
        ]

    def _audit(self) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        with open(self.output_dir / f"check-i-{today}.json") as f:
            return json.load(f)

    def test_scheduled_no_signal_run_suppresses_dm(self):
        # No proposals, no anomalies, no repeats, retry overhead 5% →
        # has_signal=False. Without --force, DM is suppressed; audit
        # JSON + journal still land.
        self._write_sidecar(_sidecar(retry_pct=5.0, retry_usd=0.10))
        # Simulate a scheduled run by passing --week-ending (which is
        # already in _argv) and NOT --force. _argv defaults to --force,
        # so build a scheduled argv explicitly.
        argv = [
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
        ]
        rc = pci.main(argv)
        self.assertEqual(rc, 0)
        self.assertEqual(self.append_calls, [],
                         "DM should be suppressed on scheduled no-signal run")
        audit = self._audit()
        self.assertEqual(audit["mode"], "no-signal")
        self.assertFalse(audit["has_signal"])
        self.assertTrue(self.journal.exists())
        self.assertIn("DM suppressed", self.journal.read_text())

    def test_force_bypasses_no_signal_suppression(self):
        # Same no-signal inputs, but --force → DM fires.
        self._write_sidecar(_sidecar(retry_pct=5.0, retry_usd=0.10))
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.append_calls), 1,
                         "--force should bypass no-signal suppression")
        audit = self._audit()
        self.assertEqual(audit["mode"], "no-signal")

    def test_heartbeat_with_signal_still_dms(self):
        # Sub-3σ anomaly: no proposal but has_signal=True → heartbeat
        # mode, DM fires even without --force.
        self._write_sidecar(_sidecar(anomalies=[{
            "task_id": "mild-anom",
            "agent": "forge",
            "task_type": "feature-development",
            "cost_usd": 1.40,
            "baseline_usd": 0.90,
            "sigma_above": 2.2,
            "context": "",
        }]))
        argv = [
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
        ]
        rc = pci.main(argv)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.append_calls), 1,
                         "heartbeat (signal present, no proposals) should DM")
        audit = self._audit()
        self.assertEqual(audit["mode"], "heartbeat")
        self.assertTrue(audit["has_signal"])

    def test_digest_run_dms(self):
        # Retry overhead 22% → proposal → digest → has_signal → DM.
        self._write_sidecar(_sidecar(retry_pct=22.0, retry_usd=3.0))
        argv = [
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
        ]
        rc = pci.main(argv)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.append_calls), 1)
        audit = self._audit()
        self.assertEqual(audit["mode"], "digest")


class WeekdayGateTests(unittest.TestCase):
    """Fire on Mon/Wed/Fri/Sun, skip on Tue/Thu/Sat unless --force."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sidecar_dir = self.root / "ledger"
        self.outbox_root = self.root / "outboxes"
        self.output_dir = self.root / "out"
        self.journal = self.root / "cycle-journal.md"
        self.halt_flag = self.root / "halt-not-set"
        self.sidecar_dir.mkdir(parents=True)
        # Provide a sidecar so a non-gated run produces an audit file.
        (self.sidecar_dir / f"weekly-{WEEK_ENDING}.json").write_text(
            json.dumps(_sidecar())
        )

    def _argv_no_week_ending(self) -> list[str]:
        # Omit --week-ending so the weekday gate is consulted.
        return [
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
            "--no-dm", "--no-journal",
        ]

    def _patch_now(self, *, year: int, month: int, day: int):
        """Patch pulse_check_i.datetime.now to return a fixed UTC time."""
        fixed = datetime(year, month, day, 8, 0, 0, tzinfo=timezone.utc)

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D401
                return fixed

        return mock.patch.object(pci, "datetime", _FrozenDateTime)

    def _audit_glob(self) -> list[Path]:
        if not self.output_dir.exists():
            return []
        return sorted(self.output_dir.glob("check-i-*.json"))

    def test_fires_on_monday(self):
        # 2026-05-18 is a Monday (weekday 0).
        with self._patch_now(year=2026, month=5, day=18):
            rc = pci.main(self._argv_no_week_ending())
        self.assertEqual(rc, 0)
        self.assertEqual(
            [p.name for p in self._audit_glob()],
            ["check-i-2026-05-18.json"],
        )

    def test_fires_on_wednesday(self):
        # 2026-05-20 is a Wednesday (weekday 2).
        with self._patch_now(year=2026, month=5, day=20):
            rc = pci.main(self._argv_no_week_ending())
        self.assertEqual(rc, 0)
        self.assertEqual(
            [p.name for p in self._audit_glob()],
            ["check-i-2026-05-20.json"],
        )

    def test_fires_on_friday(self):
        # 2026-05-22 is a Friday (weekday 4).
        with self._patch_now(year=2026, month=5, day=22):
            rc = pci.main(self._argv_no_week_ending())
        self.assertEqual(rc, 0)
        self.assertEqual(
            [p.name for p in self._audit_glob()],
            ["check-i-2026-05-22.json"],
        )

    def test_fires_on_sunday(self):
        # 2026-05-24 is a Sunday (weekday 6).
        with self._patch_now(year=2026, month=5, day=24):
            rc = pci.main(self._argv_no_week_ending())
        self.assertEqual(rc, 0)
        self.assertEqual(
            [p.name for p in self._audit_glob()],
            ["check-i-2026-05-24.json"],
        )

    def test_skips_on_tuesday(self):
        # 2026-05-19 is a Tuesday (weekday 1) — off day.
        with self._patch_now(year=2026, month=5, day=19):
            rc = pci.main(self._argv_no_week_ending())
        self.assertEqual(rc, 0)
        self.assertEqual(self._audit_glob(), [])

    def test_skips_on_thursday(self):
        # 2026-05-21 is a Thursday (weekday 3) — off day.
        with self._patch_now(year=2026, month=5, day=21):
            rc = pci.main(self._argv_no_week_ending())
        self.assertEqual(rc, 0)
        self.assertEqual(self._audit_glob(), [])

    def test_skips_on_saturday(self):
        # 2026-05-23 is a Saturday (weekday 5) — off day.
        with self._patch_now(year=2026, month=5, day=23):
            rc = pci.main(self._argv_no_week_ending())
        self.assertEqual(rc, 0)
        self.assertEqual(self._audit_glob(), [])

    def test_force_bypasses_gate_on_off_day(self):
        # Tuesday + --force should still run.
        with self._patch_now(year=2026, month=5, day=19):
            rc = pci.main(self._argv_no_week_ending() + ["--force"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            [p.name for p in self._audit_glob()],
            ["check-i-2026-05-19.json"],
        )


class SidecarRefreshTests(unittest.TestCase):
    """Auto-refresh of stale Ledger sidecar via run_ledger.sh (closed-loop step 3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sidecar_dir = self.root / "ledger"
        self.outbox_root = self.root / "outboxes"
        self.output_dir = self.root / "out"
        self.journal = self.root / "cycle-journal.md"
        self.halt_flag = self.root / "halt-not-set"
        self.sidecar_dir.mkdir(parents=True)
        self.sidecar_path = self.sidecar_dir / f"weekly-{WEEK_ENDING}.json"

    def _write_sidecar(self, age_hours: float = 0.0) -> None:
        self.sidecar_path.write_text(json.dumps(_sidecar()))
        if age_hours > 0:
            past = time.time() - age_hours * 3600.0
            os.utime(self.sidecar_path, (past, past))

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

    def test_fresh_sidecar_does_not_refresh(self):
        self._write_sidecar(age_hours=1.0)
        with mock.patch.object(pci.subprocess, "run") as run_mock:
            rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()

    def test_sidecar_just_under_threshold_does_not_refresh(self):
        self._write_sidecar(age_hours=23.0)
        with mock.patch.object(pci.subprocess, "run") as run_mock:
            rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()

    def test_missing_sidecar_triggers_refresh(self):
        with mock.patch.object(pci.subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"",
            )
            rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()
        call_args = run_mock.call_args[0][0]
        self.assertEqual(call_args[0], "bash")
        self.assertTrue(call_args[1].endswith("run_ledger.sh"))

    def test_stale_25h_sidecar_triggers_refresh(self):
        self._write_sidecar(age_hours=25.0)
        with mock.patch.object(pci.subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"",
            )
            rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        run_mock.assert_called_once()

    def test_8d_stale_sidecar_routes_to_stale_skip(self):
        # 8d old: refresh fires (>24h), mocked refresh doesn't update the
        # file, then the >7d stale-skip path takes over → skipped mode.
        self._write_sidecar(age_hours=8 * 24)
        with mock.patch.object(pci.subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"",
            )
            rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        today = datetime.now(timezone.utc).date().isoformat()
        with open(self.output_dir / f"check-i-{today}.json") as f:
            audit = json.load(f)
        self.assertEqual(audit["mode"], "skipped")

    def test_refresh_failure_does_not_crash(self):
        self._write_sidecar(age_hours=25.0)
        with mock.patch.object(pci.subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"", stderr=b"boom\n",
            )
            rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertTrue((self.output_dir / f"check-i-{today}.json").exists())

    def test_refresh_timeout_does_not_crash(self):
        self._write_sidecar(age_hours=25.0)
        with mock.patch.object(pci.subprocess, "run") as run_mock:
            run_mock.side_effect = subprocess.TimeoutExpired(
                cmd=["bash", "run_ledger.sh"], timeout=120,
            )
            rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertTrue((self.output_dir / f"check-i-{today}.json").exists())


class AutoDispatchTests(unittest.TestCase):
    """Closed-loop step 5: eligible proposals (effort=small + $-quantified
    impact) auto-dispatch to Beacon's inbox via safe_write_inbox. Dedup
    state file prevents re-dispatching the same proposal within
    AUTO_DISPATCH_DEDUP_WINDOW_DAYS.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "dispatched.json"
        self.fired_at = datetime(2026, 5, 24, 8, 0, 0, tzinfo=timezone.utc)
        # Track calls into a fake safe_write_inbox.safe_write_inbox.
        self.calls: list[dict] = []

        def _fake_swi(target_agent, task_dict, source_agent, filename):
            self.calls.append({
                "target_agent": target_agent,
                "task_dict": dict(task_dict),
                "source_agent": source_agent,
                "filename": filename,
            })
            return Path(self.tmp.name) / "inboxes" / target_agent / filename

        self._swi_patch = mock.patch.object(
            pci.safe_write_inbox, "safe_write_inbox", side_effect=_fake_swi,
        )
        self._swi_patch.start()
        self.addCleanup(self._swi_patch.stop)

    def _eligible_proposal(self) -> dict:
        return {
            "title": "Review high-σ anomaly task `burned-task-007`",
            "effort": "small",
            "impact": "$9.20 task vs $1.10 baseline (3.7σ above)",
            "rationale": "Ledger flagged this task at 3.7σ above baseline.",
        }

    def _medium_proposal(self) -> dict:
        return {
            "title": "Investigate retry / clarification cost sources",
            "effort": "medium",
            "impact": "~$2.50/wk reclaimable (20.0% of total spend)",
            "rationale": "Retry overhead is above the 15% threshold.",
        }

    def _small_no_dollar(self) -> dict:
        return {
            "title": "Quick rename - no quantified savings",
            "effort": "small",
            "impact": "modest readability improvement",
            "rationale": "Naming drift in three files; one-pass cleanup.",
        }

    def _check_i(self, proposals: list[dict]) -> dict:
        return {
            "schema_version": "v1",
            "week_ending": WEEK_ENDING,
            "fired_at": self.fired_at.isoformat(),
            "mode": "digest",
            "has_signal": True,
            "proposals": proposals,
            "ledger_headline": {"total_usd": 5.0, "anomaly_count": 1},
            "engineering_signals": {},
        }

    def test_eligible_proposal_dispatches(self):
        p = self._eligible_proposal()
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([p]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["target_agent"], "beacon")
        self.assertEqual(call["source_agent"], "pulse-auto-dispatch")
        env = call["task_dict"]
        self.assertEqual(env["source"], "pulse-auto-dispatch")
        self.assertEqual(env["target_agent"], "beacon")
        self.assertEqual(env["target_repo"], "ourliberty-agent-core")
        self.assertEqual(env["phase"], "preflight")
        self.assertTrue(env["task_id"].startswith("pulse-auto-"))
        self.assertTrue(env["task_id"].endswith("-20260524"))
        self.assertGreaterEqual(len(env["prompt"]), 100)
        self.assertIn("burned-task-007", env["prompt"])
        # Dedup state persists this dispatch.
        self.assertTrue(self.state_path.exists())
        state = json.loads(self.state_path.read_text())
        key = pci._proposal_dedup_key(p)
        self.assertIn(key, state)
        self.assertEqual(state[key]["task_id"], env["task_id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["task_id"], env["task_id"])

    def test_recent_dispatch_skipped(self):
        from datetime import timedelta as _td
        p = self._eligible_proposal()
        key = pci._proposal_dedup_key(p)
        prior_ts = (self.fired_at - _td(days=3)).isoformat()
        self.state_path.write_text(json.dumps({
            key: {"task_id": "pulse-auto-old-20260521", "dispatched_at": prior_ts},
        }))
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([p]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(records, [])
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state[key]["task_id"], "pulse-auto-old-20260521")

    def test_old_dispatch_redispatches(self):
        from datetime import timedelta as _td
        p = self._eligible_proposal()
        key = pci._proposal_dedup_key(p)
        prior_ts = (self.fired_at - _td(days=10)).isoformat()
        self.state_path.write_text(json.dumps({
            key: {"task_id": "pulse-auto-stale-20260514", "dispatched_at": prior_ts},
        }))
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([p]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(records), 1)
        state = json.loads(self.state_path.read_text())
        self.assertNotEqual(state[key]["task_id"], "pulse-auto-stale-20260514")
        self.assertEqual(state[key]["dispatched_at"], self.fired_at.isoformat())

    def test_medium_effort_not_dispatched(self):
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([self._medium_proposal()]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(records, [])
        self.assertFalse(self.state_path.exists())

    def test_small_without_dollar_not_dispatched(self):
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([self._small_no_dollar()]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(records, [])

    def test_dispatch_rejected_does_not_crash(self):
        # Stop the setUp-installed patcher and replace with one whose
        # side_effect raises. Crucially, register addCleanup for the NEW
        # patcher — the setUp addCleanup captured the original patcher's
        # bound .stop, so reassigning self._swi_patch alone would leak
        # the replacement past tearDown and corrupt later tests'
        # safe_write_inbox.safe_write_inbox global.
        self._swi_patch.stop()
        new_patch = mock.patch.object(
            pci.safe_write_inbox, "safe_write_inbox",
            side_effect=pci.safe_write_inbox.DispatchRejected("simulated reject"),
        )
        new_patch.start()
        self.addCleanup(new_patch.stop)
        self._swi_patch = new_patch
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([self._eligible_proposal()]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(records, [])
        self.assertFalse(self.state_path.exists())

    def test_routing_denied_does_not_crash(self):
        # Same pattern as test_dispatch_rejected_does_not_crash: register
        # cleanup for the replacement patcher so it's removed in tearDown.
        self._swi_patch.stop()
        new_patch = mock.patch.object(
            pci.safe_write_inbox, "safe_write_inbox",
            side_effect=pci.safe_write_inbox.RoutingDenied("simulated denial"),
        )
        new_patch.start()
        self.addCleanup(new_patch.stop)
        self._swi_patch = new_patch
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([self._eligible_proposal()]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(records, [])

    def test_eligibility_helper_direct(self):
        self.assertTrue(pci._is_auto_dispatch_eligible(self._eligible_proposal()))
        self.assertFalse(pci._is_auto_dispatch_eligible(self._medium_proposal()))
        self.assertFalse(pci._is_auto_dispatch_eligible(self._small_no_dollar()))

    def test_corrupt_state_file_treated_as_empty(self):
        self.state_path.write_text("{not valid json")
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([self._eligible_proposal()]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(len(self.calls), 1)
        state = json.loads(self.state_path.read_text())
        self.assertIsInstance(state, dict)

    def test_dedup_key_stable_across_runs(self):
        p1 = self._eligible_proposal()
        p2 = self._eligible_proposal()
        self.assertEqual(
            pci._proposal_dedup_key(p1),
            pci._proposal_dedup_key(p2),
        )
        p3 = dict(p1)
        p3["title"] = "Review high-σ anomaly task `different-task-id`"
        self.assertNotEqual(
            pci._proposal_dedup_key(p1),
            pci._proposal_dedup_key(p3),
        )


class AutoDispatchEndToEndTests(unittest.TestCase):
    """Drive `main()` with a sidecar producing a small-effort $-quantified
    proposal; confirm the wiring between main() and auto_dispatch_proposals.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sidecar_dir = self.root / "ledger"
        self.outbox_root = self.root / "outboxes"
        self.output_dir = self.root / "out"
        self.journal = self.root / "cycle-journal.md"
        self.halt_flag = self.root / "halt-not-set"
        self.state_file = self.root / "dispatched.json"
        self.sidecar_dir.mkdir(parents=True)
        (self.sidecar_dir / f"weekly-{WEEK_ENDING}.json").write_text(
            json.dumps(_sidecar(anomalies=[{
                "task_id": "burned-task-007",
                "agent": "forge",
                "task_type": "feature-development",
                "cost_usd": 9.20,
                "baseline_usd": 1.10,
                "sigma_above": 3.7,
                "context": "",
            }]))
        )
        self.calls: list[dict] = []
        outer = self

        def _fake_swi(target_agent, task_dict, source_agent, filename):
            outer.calls.append({
                "target_agent": target_agent,
                "task_dict": dict(task_dict),
                "source_agent": source_agent,
                "filename": filename,
            })
            return outer.root / "inboxes" / target_agent / filename

        self._swi_patch = mock.patch.object(
            pci.safe_write_inbox, "safe_write_inbox", side_effect=_fake_swi,
        )
        self._swi_patch.start()
        self.addCleanup(self._swi_patch.stop)

    def _argv(self, *extra: str) -> list[str]:
        return [
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
            "--dispatch-state-file", str(self.state_file),
            "--no-dm", "--no-journal", "--force",
            *extra,
        ]

    def test_e2e_eligible_proposal_dispatches(self):
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        env = self.calls[0]["task_dict"]
        self.assertEqual(env["source"], "pulse-auto-dispatch")
        self.assertEqual(env["target_agent"], "beacon")
        self.assertTrue(self.state_file.exists())

    def test_e2e_no_auto_dispatch_flag_skips_dispatch(self):
        rc = pci.main(self._argv("--no-auto-dispatch"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state_file.exists())

    def test_e2e_dedup_window_skips_second_run(self):
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1,
                         "second run within dedup window should not dispatch")


if __name__ == "__main__":
    unittest.main()
