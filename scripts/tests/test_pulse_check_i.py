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

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

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

    def test_marker_error_cascade_counts_each_wrap(self):
        # v2 semantics: each marker-error standalone archive entry is one
        # retry event for the unwrapped base task_id.
        self._archive("forge", "task-a.json")  # canonical entry, skipped
        self._archive("forge", "marker-error-task-a-1.json")
        self._archive("forge", "marker-error-marker-error-task-a-1-2.json")
        self._archive(
            "forge",
            "marker-error-marker-error-marker-error-task-a-1-2-3.json",
        )
        repeats = pci.gather_retry_repeats(self.outbox)
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["task_id"], "task-a")
        self.assertEqual(repeats[0]["agent"], "forge")
        # 3 marker-error files for base 'task-a' = 3 retry events.
        self.assertEqual(repeats[0]["retry_count"], 3)

    def test_below_threshold_excluded(self):
        # Two marker-error events for the same base — below threshold (3).
        self._archive("forge", "task-x.json")
        self._archive("forge", "marker-error-task-x-1.json")
        self._archive("forge", "marker-error-marker-error-task-x-1-2.json")
        self.assertEqual(pci.gather_retry_repeats(self.outbox), [])

    def test_sorted_desc(self):
        # Both bases have marker-error cascades of differing depth.
        self._archive("forge", "alpha.json")
        self._archive("forge", "marker-error-alpha-1.json")
        self._archive("forge", "marker-error-marker-error-alpha-1-2.json")
        self._archive(
            "forge",
            "marker-error-marker-error-marker-error-alpha-1-2-3.json",
        )
        self._archive("mirror", "bravo.json")
        self._archive("mirror", "marker-error-bravo-1.json")
        self._archive("mirror", "marker-error-marker-error-bravo-1-2.json")
        self._archive(
            "mirror",
            "marker-error-marker-error-marker-error-bravo-1-2-3.json",
        )
        self._archive(
            "mirror",
            (
                "marker-error-marker-error-marker-error-marker-error-"
                "bravo-1-2-3-4.json"
            ),
        )
        repeats = pci.gather_retry_repeats(self.outbox)
        self.assertEqual(repeats[0]["task_id"], "bravo")  # higher count first
        self.assertEqual(repeats[0]["retry_count"], 4)
        self.assertEqual(repeats[1]["task_id"], "alpha")
        self.assertEqual(repeats[1]["retry_count"], 3)

    def test_plain_rotations_without_marker_error_not_counted(self):
        # v2 semantics: a `.json` + `.1.json` + `.2.json` archive is the
        # `task-34-e4-2-mission-control-migration` false-positive shape —
        # those are chain phases or revision rounds, not retries. Must NOT
        # appear in repeats.
        self._archive("forge", "task-rotonly.json")
        self._archive("forge", "task-rotonly.1.json")
        self._archive("forge", "task-rotonly.2.json")
        self._archive("forge", "task-rotonly.3.json")
        self._archive("forge", "task-rotonly.4.json")
        self._archive("forge", "task-rotonly.5.json")
        self.assertEqual(pci.gather_retry_repeats(self.outbox), [])

    def test_canonical_single_entry_absent(self):
        # A task that landed once and was archived — no retry signal.
        self._archive("forge", "task-once.json")
        self.assertEqual(pci.gather_retry_repeats(self.outbox), [])

    def test_dead_letter_marker_skipped(self):
        # Terminal infra-failure artifact; the cascade preceding it already
        # contributes the retry events. Counting the dead-letter too would
        # double-count. With threshold 3, two marker-errors alone don't
        # flag — confirming the dead-letter wasn't accidentally counted as
        # the third event.
        self._archive("forge", "task-dl.json")
        self._archive("forge", "marker-error-task-dl-1.json")
        self._archive("forge", "marker-error-marker-error-task-dl-1-2.json")
        self._archive(
            "forge",
            "dead-letter-marker-marker-error-marker-error-task-dl-1-2.json",
        )
        self.assertEqual(pci.gather_retry_repeats(self.outbox), [])

    def test_notify_marker_error_skipped(self):
        # notify-* base remains workflow noise even when marker-error wrapped.
        self._archive("beacon", "notify-task-z.json")
        self._archive("beacon", "marker-error-notify-task-z-1.json")
        self._archive(
            "beacon", "marker-error-marker-error-notify-task-z-1-2.json",
        )
        self._archive(
            "beacon",
            "marker-error-marker-error-marker-error-notify-task-z-1-2-3.json",
        )
        self.assertEqual(pci.gather_retry_repeats(self.outbox), [])

    def test_mixed_archive_only_marker_errors_count(self):
        # task-foo has plain rotations + a 3-deep marker-error cascade.
        # task-bar has only plain rotations. Only task-foo should surface.
        self._archive("forge", "task-foo.json")
        self._archive("forge", "task-foo.1.json")
        self._archive("forge", "task-foo.2.json")
        self._archive("forge", "marker-error-task-foo-1.json")
        self._archive("forge", "marker-error-marker-error-task-foo-1-2.json")
        self._archive(
            "forge",
            "marker-error-marker-error-marker-error-task-foo-1-2-3.json",
        )
        self._archive("forge", "task-bar.json")
        self._archive("forge", "task-bar.1.json")
        self._archive("forge", "task-bar.2.json")
        repeats = pci.gather_retry_repeats(self.outbox)
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["task_id"], "task-foo")
        self.assertEqual(repeats[0]["retry_count"], 3)

    def test_missing_outbox_root(self):
        nonexistent = Path(self.tmp.name) / "nope"
        self.assertEqual(pci.gather_retry_repeats(nonexistent), [])

    def test_unwrap_marker_error_base_helper(self):
        # Direct unit coverage on the helper used by gather_retry_repeats.
        self.assertEqual(
            pci._unwrap_marker_error_base("marker-error-foo-1"),
            "foo",
        )
        self.assertEqual(
            pci._unwrap_marker_error_base(
                "marker-error-marker-error-foo-1-2"
            ),
            "foo",
        )
        self.assertEqual(
            pci._unwrap_marker_error_base(
                "marker-error-opmanual-d35-5b-shipped-note-001-1"
            ),
            "opmanual-d35-5b-shipped-note-001",
        )
        self.assertIsNone(pci._unwrap_marker_error_base("plain-task"))
        self.assertIsNone(pci._unwrap_marker_error_base("plain-task.1"))


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


class DmRouteTests(unittest.TestCase):
    """G-rule check-i-repeat-dm: the first scheduled run of a week DMs
    (route='escalate'); later same-week scheduled runs are silenced to
    route='digest' (no repeat DM). --force always escalates.
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

        # Signal-bearing sidecar → digest mode → DM fires (not suppressed).
        (self.sidecar_dir / f"weekly-{WEEK_ENDING}.json").write_text(
            json.dumps(_sidecar(retry_pct=22.0, retry_usd=3.0))
        )

    def _scheduled_argv(self, *extra: str) -> list[str]:
        # No --force → exercises the journal-peek routing branch.
        return [
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
            *extra,
        ]

    def test_first_scheduled_run_escalates_and_journals(self):
        # Case (a): week not yet in the journal → escalate, block written.
        rc = pci.main(self._scheduled_argv())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.append_calls), 1)
        self.assertEqual(self.append_calls[0].get("route"), "escalate")
        self.assertTrue(self.journal.exists())
        self.assertIn(f"**Check I ({WEEK_ENDING}):**",
                      self.journal.read_text())

    def test_second_same_week_scheduled_run_routes_digest(self):
        # Case (b): the second scheduled firing of the same week sees the
        # header already journaled → digest (no repeat DM).
        self.assertEqual(pci.main(self._scheduled_argv()), 0)
        self.assertEqual(pci.main(self._scheduled_argv()), 0)
        self.assertEqual(len(self.append_calls), 2)
        self.assertEqual(self.append_calls[0].get("route"), "escalate")
        self.assertEqual(self.append_calls[1].get("route"), "digest")

    def test_force_always_escalates_even_when_week_journaled(self):
        # Case (c): journal the week first, then a --force run must still
        # escalate — /optimize callers expect a reply regardless.
        self.assertEqual(pci.main(self._scheduled_argv()), 0)
        self.assertEqual(self.append_calls[0].get("route"), "escalate")
        self.assertIn(f"**Check I ({WEEK_ENDING}):**",
                      self.journal.read_text())
        self.assertEqual(pci.main(self._scheduled_argv("--force")), 0)
        self.assertEqual(len(self.append_calls), 2)
        self.assertEqual(self.append_calls[1].get("route"), "escalate")

    def test_no_journal_run_escalates(self):
        # Case (d): --no-journal means the journal isn't the source of
        # truth → preserve current behavior (escalate).
        rc = pci.main(self._scheduled_argv("--no-journal"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.append_calls), 1)
        self.assertEqual(self.append_calls[0].get("route"), "escalate")


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

    def test_medium_effort_now_dispatched(self):
        # Widened 2026-06-22: medium-effort proposals auto-dispatch.
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([self._medium_proposal()]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(records), 1)
        self.assertTrue(self.state_path.exists())

    def test_small_without_dollar_now_dispatched(self):
        # Widened 2026-06-22: a non-cost-framed small win auto-dispatches.
        records = pci.auto_dispatch_proposals(
            check_i=self._check_i([self._small_no_dollar()]),
            fired_at=self.fired_at,
            state_path=self.state_path,
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(records), 1)

    def test_large_or_impactless_not_dispatched(self):
        # Still excluded: large effort (Larry's call) + impact-less proposals.
        for p in (
            {"title": "Big refactor", "effort": "large",
             "impact": "$50/wk reclaimable", "rationale": "x"},
            {"title": "Vague idea", "effort": "small", "impact": "",
             "rationale": "x"},
        ):
            self.calls.clear()
            records = pci.auto_dispatch_proposals(
                check_i=self._check_i([p]), fired_at=self.fired_at,
                state_path=self.state_path,
            )
            self.assertEqual(self.calls, [], p["effort"])
            self.assertEqual(records, [], p["effort"])

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
        # Widened 2026-06-22: small + medium with any non-empty impact are eligible.
        self.assertTrue(pci._is_auto_dispatch_eligible(self._eligible_proposal()))
        self.assertTrue(pci._is_auto_dispatch_eligible(self._medium_proposal()))
        self.assertTrue(pci._is_auto_dispatch_eligible(self._small_no_dollar()))
        # Still ineligible: large effort, and any effort with no/blank impact.
        self.assertFalse(pci._is_auto_dispatch_eligible(
            {"effort": "large", "impact": "$5/wk reclaimable"}))
        self.assertFalse(pci._is_auto_dispatch_eligible(
            {"effort": "small", "impact": "   "}))
        self.assertFalse(pci._is_auto_dispatch_eligible({"effort": "medium"}))

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


class ManualDispatchTests(unittest.TestCase):
    """Larry-driven `--dispatch <N>` path. Bypasses _is_auto_dispatch_eligible
    (any-effort accepted), warns-but-proceeds on dedup hit, and surfaces
    errors via non-zero exit code so /dispatch gives clear feedback.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output_dir = self.root / "pulse-check-i"
        self.output_dir.mkdir(parents=True)
        self.state_path = self.root / "dispatched.json"

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

    def _medium_proposal(self) -> dict:
        return {
            "title": "Investigate retry / clarification cost sources",
            "effort": "medium",
            "impact": "~$2.50/wk reclaimable (20.0% of total spend)",
            "rationale": "Retry overhead is above the 15% threshold.",
        }

    def _small_proposal(self) -> dict:
        return {
            "title": "Quick rename - no quantified savings",
            "effort": "small",
            "impact": "modest readability improvement",
            "rationale": "Naming drift in three files; one-pass cleanup.",
        }

    def _write_audit(self, proposals: list[dict], firing_date: str = "2026-05-24") -> Path:
        path = self.output_dir / f"check-i-{firing_date}.json"
        path.write_text(json.dumps({
            "schema_version": "v1",
            "week_ending": "2026-05-18",
            "mode": "digest",
            "has_signal": True,
            "proposals": proposals,
            "ledger_headline": {"total_usd": 5.0, "anomaly_count": 1},
            "engineering_signals": {},
        }))
        return path

    def _argv(self, *extra: str) -> list[str]:
        return [
            "--output-dir", str(self.output_dir),
            "--dispatch-state-file", str(self.state_path),
            *extra,
        ]

    def test_dispatch_medium_proposal_succeeds(self):
        self._write_audit([self._medium_proposal()])
        rc = pci.main(self._argv("--dispatch", "1"))
        self.assertEqual(rc, 0)
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
        self.assertTrue(self.state_path.exists())
        state = json.loads(self.state_path.read_text())
        key = pci._proposal_dedup_key(self._medium_proposal())
        self.assertIn(key, state)
        self.assertEqual(state[key]["task_id"], env["task_id"])

    def test_dispatch_out_of_range_exits_1(self):
        self._write_audit([self._medium_proposal()])
        rc = pci.main(self._argv("--dispatch", "3"))
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state_path.exists())

    def test_dispatch_zero_exits_1(self):
        self._write_audit([self._medium_proposal()])
        rc = pci.main(self._argv("--dispatch", "0"))
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [])

    def test_dispatch_dedup_hit_proceeds_anyway(self):
        from datetime import timedelta as _td
        p = self._medium_proposal()
        self._write_audit([p])
        key = pci._proposal_dedup_key(p)
        # Seed dedup state with a recent dispatch (within 7d window).
        prior_ts = (datetime.now(timezone.utc) - _td(days=2)).isoformat()
        self.state_path.write_text(json.dumps({
            key: {"task_id": "pulse-auto-prior-20260522", "dispatched_at": prior_ts},
        }))
        rc = pci.main(self._argv("--dispatch", "1"))
        self.assertEqual(rc, 0)
        # Manual override: SWI was still called.
        self.assertEqual(len(self.calls), 1)
        # State row was overwritten with the new dispatch (not the prior).
        state = json.loads(self.state_path.read_text())
        self.assertNotEqual(state[key]["task_id"], "pulse-auto-prior-20260522")

    def test_dispatch_audit_override_reads_specified_file(self):
        older = self._write_audit(
            [self._medium_proposal()], firing_date="2026-05-21",
        )
        newer = self._write_audit(
            [self._small_proposal()], firing_date="2026-05-24",
        )
        # Force mtimes so older is genuinely older than newer; the default
        # "most-recent" resolver would otherwise pick `newer`.
        old_t = time.time() - 3600
        os.utime(older, (old_t, old_t))
        os.utime(newer, (time.time(), time.time()))
        rc = pci.main(self._argv("--dispatch", "1", "--audit", str(older)))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 1)
        # We dispatched the medium proposal from `older`, not the small
        # one from `newer`.
        self.assertIn("retry", self.calls[0]["task_dict"]["prompt"].lower())

    def test_dispatch_no_audit_files_exits_1(self):
        # output_dir exists but is empty.
        rc = pci.main(self._argv("--dispatch", "1"))
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [])

    def test_dispatch_rejected_exits_1_state_unchanged(self):
        self._write_audit([self._medium_proposal()])
        self._swi_patch.stop()
        new_patch = mock.patch.object(
            pci.safe_write_inbox, "safe_write_inbox",
            side_effect=pci.safe_write_inbox.DispatchRejected("simulated reject"),
        )
        new_patch.start()
        self.addCleanup(new_patch.stop)
        self._swi_patch = new_patch
        rc = pci.main(self._argv("--dispatch", "1"))
        self.assertEqual(rc, 1)
        self.assertFalse(self.state_path.exists())

    def test_dispatch_picks_most_recent_audit_by_default(self):
        older = self._write_audit(
            [self._small_proposal()], firing_date="2026-05-21",
        )
        newer = self._write_audit(
            [self._medium_proposal()], firing_date="2026-05-24",
        )
        old_t = time.time() - 3600
        os.utime(older, (old_t, old_t))
        os.utime(newer, (time.time(), time.time()))
        rc = pci.main(self._argv("--dispatch", "1"))
        self.assertEqual(rc, 0)
        # The medium proposal from `newer` has "retry" in the prompt; the
        # small proposal from `older` does not.
        self.assertIn("retry", self.calls[0]["task_dict"]["prompt"].lower())

    def test_dispatch_audit_file_missing_exits_1(self):
        rc = pci.main(self._argv(
            "--dispatch", "1", "--audit", str(self.root / "does-not-exist.json"),
        ))
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [])




class AppendJournalIdempotencyTests(unittest.TestCase):
    """append_journal skips a Check I block whose week is already present."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self._tmp.name) / "cycle-journal.md"

    def tearDown(self):
        self._tmp.cleanup()

    def _block(self, week):
        result = pci.assemble_check_i(
            sidecar=None, repeats=[], week_ending=week,
            sidecar_filename=None, fired_at=FIRED_AT,
        )
        return pci.render_journal_block(result)

    def test_appends_when_absent(self):
        pci.append_journal(self.journal, self._block(WEEK_ENDING))
        text = self.journal.read_text(encoding="utf-8")
        self.assertEqual(text.count(f"**Check I ({WEEK_ENDING}):**"), 1)

    def test_skips_duplicate_same_week(self):
        block = self._block(WEEK_ENDING)
        pci.append_journal(self.journal, block)
        pci.append_journal(self.journal, block)
        text = self.journal.read_text(encoding="utf-8")
        self.assertEqual(text.count(f"**Check I ({WEEK_ENDING}):**"), 1)

    def test_appends_different_week(self):
        pci.append_journal(self.journal, self._block("2026-05-11"))
        pci.append_journal(self.journal, self._block(WEEK_ENDING))
        text = self.journal.read_text(encoding="utf-8")
        self.assertEqual(text.count("**Check I (2026-05-11):**"), 1)
        self.assertEqual(text.count(f"**Check I ({WEEK_ENDING}):**"), 1)

    def test_non_check_i_block_always_appends(self):
        pci.append_journal(self.journal, "\nplain note\n")
        pci.append_journal(self.journal, "\nplain note\n")
        text = self.journal.read_text(encoding="utf-8")
        self.assertEqual(text.count("plain note"), 2)


class MarkerErrorRetryDepthTests(unittest.TestCase):
    """The retry-depth helper recovers N from both flat and legacy-nested
    marker-error stems (the trailing integer is the cumulative count in both).
    """

    def test_flat_form_depths(self):
        self.assertEqual(pci._marker_error_retry_depth("marker-error-task-a-1"), 1)
        self.assertEqual(pci._marker_error_retry_depth("marker-error-task-a-2"), 2)
        self.assertEqual(pci._marker_error_retry_depth("marker-error-task-a-3"), 3)

    def test_nested_legacy_form_depths(self):
        self.assertEqual(
            pci._marker_error_retry_depth("marker-error-marker-error-task-a-1-2"),
            2,
        )
        self.assertEqual(
            pci._marker_error_retry_depth(
                "marker-error-marker-error-marker-error-task-a-1-2-3"
            ),
            3,
        )

    def test_no_trailing_int_returns_none(self):
        self.assertIsNone(pci._marker_error_retry_depth("marker-error-task-a"))
        self.assertIsNone(pci._marker_error_retry_depth("plain-task"))


# Window math anchors: WEEK_ENDING 2026-05-18 is a Monday; the Check-I window
# is [2026-05-11, 2026-05-18).
_WE_DT = datetime(2026, 5, 18, tzinfo=timezone.utc)
_IN_WINDOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
_OUT_WINDOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)


class MarkerDisciplineComputeTests(unittest.TestCase):
    """Lock the preflight-marker-discipline computation: windowed retry-depth
    distribution, escalation rate, trailing-week baseline + alert, and trend.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "out"
        self.output_dir.mkdir(parents=True)

    def _marker_error(self, base: str, depth: int, when: datetime,
                      agent: str = "forge") -> None:
        archive = self.outbox / agent / ".archive"
        archive.mkdir(parents=True, exist_ok=True)
        path = archive / f"marker-error-{base}-{depth}.json"
        path.write_text("{}")
        ts = when.timestamp()
        os.utime(path, (ts, ts))

    def _write_prior_audit(self, week_ending: str, misses: int) -> None:
        # Firing-date filename is arbitrary for prior weeks; the loader keys on
        # the persisted top-level week_ending.
        path = self.output_dir / f"check-i-{week_ending}.json"
        path.write_text(json.dumps({
            "week_ending": week_ending,
            "engineering_signals": {
                "marker_discipline": {"misses": misses},
            },
        }))

    def _compute(self):
        return pci.compute_marker_discipline(
            outbox_root=self.outbox,
            week_ending=_WE_DT,
            output_dir=self.output_dir,
        )

    def test_distribution_and_escalation_rate(self):
        # base-a escalated all the way (depth 1/2/3); base-b and base-c only
        # tripped the first retry. depth-1 files = 3 misses; depth-2 = 1; depth-3 = 1.
        for d in (1, 2, 3):
            self._marker_error("base-a", d, _IN_WINDOW)
        self._marker_error("base-b", 1, _IN_WINDOW)
        self._marker_error("base-c", 1, _IN_WINDOW)
        md = self._compute()
        self.assertEqual(md["misses"], 3)
        self.assertEqual(md["retry_depth_distribution"], {"1": 3, "2": 1, "3": 1})
        self.assertEqual(md["total_events"], 5)
        self.assertEqual(md["retry_2_plus"], 1)
        self.assertAlmostEqual(md["escalation_rate"], round(1 / 3, 4))
        self.assertEqual(md["near_forfeit"], 1)
        self.assertEqual(md["agent"], "forge")
        self.assertEqual(md["window_start"], "2026-05-11")
        self.assertEqual(md["window_end"], "2026-05-18")

    def test_out_of_window_and_non_forge_excluded(self):
        self._marker_error("in-win", 1, _IN_WINDOW)
        self._marker_error("too-old", 1, _OUT_WINDOW)  # before window
        self._marker_error("mirror-task", 1, _IN_WINDOW, agent="mirror")
        md = self._compute()
        self.assertEqual(md["misses"], 1)
        self.assertEqual(md["total_events"], 1)

    def test_excludes_dead_letter_notify_and_fixtures(self):
        # dead-letter terminal artifact, notify-* workflow, and fixture task_ids
        # must not contaminate the signal (shared parser exclusions).
        self._marker_error("real-task", 1, _IN_WINDOW)
        # notify-* base
        self._marker_error("notify-thing", 1, _IN_WINDOW)
        # dead-letter prefix
        archive = self.outbox / "forge" / ".archive"
        dl = archive / "dead-letter-marker-marker-error-real-task-1-2.json"
        dl.write_text("{}")
        ts = _IN_WINDOW.timestamp()
        os.utime(dl, (ts, ts))
        md = self._compute()
        self.assertEqual(md["misses"], 1)

    def test_ramp_up_suspends_alert(self):
        # Three prior weeks only (< RAMP_UP_WEEKS) → baseline inactive, no alert
        # even with a big current spike.
        for d in (1, 2, 3):
            self._marker_error(f"spike-{d}", 1, _IN_WINDOW)
        self._marker_error("spike-4", 1, _IN_WINDOW)
        self._write_prior_audit("2026-05-11", 0)
        self._write_prior_audit("2026-05-04", 0)
        self._write_prior_audit("2026-04-27", 0)
        md = self._compute()
        self.assertEqual(md["misses"], 4)
        self.assertFalse(md["baseline"]["active"])
        self.assertEqual(md["baseline"]["weeks_observed"], 3)
        self.assertFalse(md["alert"])

    def test_alert_off_flat_zero_baseline(self):
        # Four clean prior weeks (mean 0, stdev 0) → any current regression fires.
        self._marker_error("regress-a", 1, _IN_WINDOW)
        self._marker_error("regress-b", 1, _IN_WINDOW)
        for wk in ("2026-05-11", "2026-05-04", "2026-04-27", "2026-04-20"):
            self._write_prior_audit(wk, 0)
        md = self._compute()
        self.assertEqual(md["misses"], 2)
        self.assertTrue(md["baseline"]["active"])
        self.assertTrue(md["alert"])
        self.assertIn("flat baseline", md["alert_reason"])

    def test_no_alert_within_variance(self):
        # Noisy baseline [2,3,2,3]: mean 2.5, stdev≈0.577, threshold≈3.65.
        # current 3 → below threshold → no alert.
        self._marker_error("m1", 1, _IN_WINDOW)
        self._marker_error("m2", 1, _IN_WINDOW)
        self._marker_error("m3", 1, _IN_WINDOW)
        for wk, m in (("2026-05-11", 2), ("2026-05-04", 3),
                      ("2026-04-27", 2), ("2026-04-20", 3)):
            self._write_prior_audit(wk, m)
        md = self._compute()
        self.assertEqual(md["misses"], 3)
        self.assertTrue(md["baseline"]["active"])
        self.assertFalse(md["alert"])

    def test_alert_above_variance_threshold(self):
        # Same noisy baseline, current 5 → above threshold≈3.65 → alert.
        for i in range(5):
            self._marker_error(f"big-{i}", 1, _IN_WINDOW)
        for wk, m in (("2026-05-11", 2), ("2026-05-04", 3),
                      ("2026-04-27", 2), ("2026-04-20", 3)):
            self._write_prior_audit(wk, m)
        md = self._compute()
        self.assertEqual(md["misses"], 5)
        self.assertTrue(md["alert"])
        self.assertIn("σ", md["alert_reason"])

    def test_trend_vs_prior_week(self):
        # Current 2 misses; immediately prior week recorded 10 → trend down -8.
        self._marker_error("t1", 1, _IN_WINDOW)
        self._marker_error("t2", 1, _IN_WINDOW)
        for wk, m in (("2026-05-11", 10), ("2026-05-04", 8),
                      ("2026-04-27", 9), ("2026-04-20", 11)):
            self._write_prior_audit(wk, m)
        md = self._compute()
        self.assertIsNotNone(md["trend"])
        self.assertEqual(md["trend"]["prior_week"], "2026-05-11")
        self.assertEqual(md["trend"]["prior_misses"], 10)
        self.assertEqual(md["trend"]["misses_delta"], -8)
        self.assertEqual(md["trend"]["direction"], "down")

    def test_empty_archive_no_alert(self):
        md = self._compute()
        self.assertEqual(md["misses"], 0)
        self.assertEqual(md["retry_depth_distribution"], {"1": 0, "2": 0, "3": 0})
        self.assertFalse(md["alert"])
        self.assertIsNone(md["trend"])


class MarkerDisciplineProposalTests(unittest.TestCase):
    """Alert → digest proposal, but NOT auto-dispatch eligible."""

    def _alert_md(self) -> dict:
        return {
            "misses": 30,
            "retry_depth_distribution": {"1": 30, "2": 11, "3": 3},
            "escalation_rate": 0.3667,
            "baseline": {"weeks_observed": 4, "mean_misses": 5.0,
                         "stdev_misses": 1.2},
            "alert": True,
            "alert_reason": "30 misses ≥ baseline mean 5.0 + 2σ (7.4)",
        }

    def test_alert_yields_proposal(self):
        proposals = pci.synthesize_proposals(
            _sidecar(), repeats=[], marker_discipline=self._alert_md(),
        )
        self.assertEqual(len(proposals), 1)
        self.assertIn("marker-discipline", proposals[0]["title"].lower())

    def test_no_alert_no_proposal(self):
        md = self._alert_md()
        md["alert"] = False
        self.assertEqual(
            pci.synthesize_proposals(_sidecar(), repeats=[], marker_discipline=md),
            [],
        )

    def test_proposal_is_now_auto_dispatch_eligible(self):
        # Widened 2026-06-22: a small-effort marker-discipline proposal with a
        # clear impact now auto-dispatches even with no `$<digit>` token (the
        # dollar-quantified requirement was dropped).
        proposals = pci.synthesize_proposals(
            _sidecar(), repeats=[], marker_discipline=self._alert_md(),
        )
        p = proposals[0]
        self.assertEqual(p["effort"], "small")
        self.assertNotRegex(p["impact"], r"\$\d")
        self.assertTrue(pci._is_auto_dispatch_eligible(p))

    def test_none_marker_discipline_is_safe(self):
        self.assertEqual(
            pci.synthesize_proposals(_sidecar(), repeats=[], marker_discipline=None),
            [],
        )


class MarkerDisciplineAssembleRenderTests(unittest.TestCase):
    def _alert_md(self) -> dict:
        return {
            "misses": 30,
            "retry_depth_distribution": {"1": 30, "2": 11, "3": 3},
            "escalation_rate": 0.3667,
            "total_events": 44,
            "retry_2_plus": 11,
            "trend": {"prior_week": "2026-05-11", "prior_misses": 5,
                      "misses_delta": 25, "direction": "up"},
            "baseline": {"weeks_observed": 4, "mean_misses": 5.0,
                         "stdev_misses": 1.2, "active": True},
            "alert": True,
            "alert_reason": "elevated",
        }

    def _quiet_md(self) -> dict:
        md = self._alert_md()
        md["alert"] = False
        md["misses"] = 1
        md["retry_depth_distribution"] = {"1": 1, "2": 0, "3": 0}
        md["escalation_rate"] = 0.0
        return md

    def test_assemble_persists_marker_discipline(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
            marker_discipline=self._quiet_md(),
        )
        md = result["engineering_signals"]["marker_discipline"]
        self.assertEqual(md["misses"], 1)
        # Quiet (non-alert) signal does not by itself force a digest.
        self.assertEqual(result["mode"], "no-signal")

    def test_assemble_alert_forces_digest(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
            marker_discipline=self._alert_md(),
        )
        self.assertEqual(result["mode"], "digest")
        self.assertTrue(result["has_signal"])
        titles = [p["title"] for p in result["proposals"]]
        self.assertTrue(any("marker-discipline" in t.lower() for t in titles))

    def test_journal_block_includes_discipline_line(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
            marker_discipline=self._alert_md(),
        )
        block = pci.render_journal_block(result)
        self.assertIn("Forge marker-discipline", block)
        self.assertIn("[ELEVATED]", block)

    def test_dm_digest_includes_elevated_line(self):
        result = pci.assemble_check_i(
            sidecar=_sidecar(retry_pct=25.0, retry_usd=3.0), repeats=[],
            week_ending=WEEK_ENDING, sidecar_filename="weekly-2026-05-18.json",
            fired_at=FIRED_AT, marker_discipline=self._alert_md(),
        )
        body = pci.render_dm(result)
        self.assertIn("marker-discipline ELEVATED", body)

    def test_backward_compatible_without_marker_discipline(self):
        # Existing callers that omit marker_discipline get a None entry, no crash.
        result = pci.assemble_check_i(
            sidecar=_sidecar(), repeats=[], week_ending=WEEK_ENDING,
            sidecar_filename="weekly-2026-05-18.json", fired_at=FIRED_AT,
        )
        self.assertIsNone(result["engineering_signals"]["marker_discipline"])
        block = pci.render_journal_block(result)
        self.assertNotIn("Forge marker-discipline", block)


class MarkerDisciplineEndToEndTests(unittest.TestCase):
    """main() persists the marker_discipline block to the audit sidecar."""

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
        (self.sidecar_dir / f"weekly-{WEEK_ENDING}.json").write_text(
            json.dumps(_sidecar())
        )
        # Two forge marker-error events inside the Check-I window.
        archive = self.outbox_root / "forge" / ".archive"
        archive.mkdir(parents=True, exist_ok=True)
        for base, depth in (("base-a", 1), ("base-a", 2), ("base-b", 1)):
            p = archive / f"marker-error-{base}-{depth}.json"
            p.write_text("{}")
            ts = _IN_WINDOW.timestamp()
            os.utime(p, (ts, ts))

    def _argv(self) -> list[str]:
        return [
            "--week-ending", WEEK_ENDING,
            "--sidecar-dir", str(self.sidecar_dir),
            "--outbox-root", str(self.outbox_root),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt_flag),
            "--no-dm", "--no-journal", "--no-auto-dispatch", "--no-park",
            "--force",
        ]

    def test_e2e_persists_marker_discipline(self):
        rc = pci.main(self._argv())
        self.assertEqual(rc, 0)
        today = datetime.now(timezone.utc).date().isoformat()
        with open(self.output_dir / f"check-i-{today}.json") as f:
            audit = json.load(f)
        md = audit["engineering_signals"]["marker_discipline"]
        self.assertEqual(md["misses"], 2)  # base-a depth1 + base-b depth1
        self.assertEqual(md["retry_depth_distribution"], {"1": 2, "2": 1, "3": 0})
        self.assertEqual(md["window_end"], WEEK_ENDING)


class ParkAndSuppressTests(unittest.TestCase):
    """Contract B §5.2/§5.3: each non-auto-dispatched proposal is parked once as
    a durable capture (emit_capture(label='pulse-check-i')); its DM line is then
    shown once as `[parked]` and omitted from subsequent digests. A proposal
    whose park fails (no capture_id) is never silently dropped — it keeps its
    full DM line. Auto-dispatch-eligible proposals are never parked.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "parked.json"
        self.fired_at = datetime(2026, 5, 24, 8, 0, 0, tzinfo=timezone.utc)

    def _eligible_proposal(self) -> dict:
        # effort=small + $-quantified impact → auto-dispatches, never parked.
        return {
            "title": "Review high-σ anomaly task `burned-task-007`",
            "effort": "small",
            "impact": "$9.20 task vs $1.10 baseline (3.7σ above)",
            "rationale": "Ledger flagged this task at 3.7σ above baseline.",
        }

    def _parkable_proposal(self) -> dict:
        # An INELIGIBLE proposal, so it parks rather than auto-dispatches.
        # effort=large keeps it out of the widened (small+medium) auto-dispatch
        # lane (2026-06-22). (Was effort=medium, which now auto-dispatches.)
        return {
            "title": "Investigate retry / clarification cost sources",
            "effort": "large",
            "impact": "~$2.50/wk reclaimable (20.0% of total spend)",
            "rationale": "Retry overhead is above the 15% threshold.",
        }

    # The park tests below were written against a medium-effort fixture; medium
    # now auto-dispatches, so they use the large-effort parkable one via this
    # alias (keeps the call sites unchanged).
    _medium_proposal = _parkable_proposal

    def _check_i(self, proposals: list[dict], signals: dict | None = None) -> dict:
        return {
            "schema_version": "v1",
            "week_ending": WEEK_ENDING,
            "fired_at": self.fired_at.isoformat(),
            "mode": "digest",
            "has_signal": True,
            "proposals": [dict(p) for p in proposals],
            "ledger_headline": {"total_usd": 5.0, "anomaly_count": 1},
            "engineering_signals": signals or {},
        }

    def test_parks_each_non_eligible_once_over_n_cycles(self):
        # Same medium proposal re-pitched across 3 Check I cycles parks exactly
        # once: the first cycle records a capture_id; later cycles see it in the
        # state file and skip the emit.
        p = self._medium_proposal()
        with mock.patch.object(pci, "emit_capture",
                               return_value="cap-001") as emit:
            first = pci.park_proposals([p], self.fired_at, self.state_path)
            second = pci.park_proposals([p], self.fired_at, self.state_path)
            third = pci.park_proposals([p], self.fired_at, self.state_path)

        self.assertEqual(emit.call_count, 1, "park must emit exactly once")
        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["label"], "pulse-check-i")
        self.assertEqual(kwargs["source"], "agent")
        self.assertEqual(kwargs["title"], p["title"])
        self.assertIn(p["impact"], kwargs["note"])
        self.assertIn(p["rationale"], kwargs["note"])

        key = pci._proposal_dedup_key(p)
        self.assertEqual(set(first), {key})
        self.assertEqual(second, {})
        self.assertEqual(third, {})
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state[key]["capture_id"], "cap-001")
        self.assertEqual(state[key]["parked_at"], self.fired_at.isoformat())

    def test_parked_dropped_from_digest_after_first_cycle(self):
        # Cycle 1: the freshly parked proposal is annotated [parked] and stays
        # in the digest (shown once). Cycle 2: same proposal, now in state and
        # not parked_now → omitted; with no other signal the mode collapses to
        # no-signal instead of re-pitching.
        p = self._medium_proposal()
        with mock.patch.object(pci, "emit_capture", return_value="cap-xyz"):
            parked_now = pci.park_proposals([p], self.fired_at, self.state_path)
        parked_state = pci._load_parked_state(self.state_path)

        c1 = self._check_i([p])
        pci.apply_park_suppression(c1, parked_now, parked_state)
        self.assertEqual(len(c1["proposals"]), 1)
        self.assertTrue(c1["proposals"][0]["parked"])
        self.assertEqual(c1["proposals"][0]["capture_id"], "cap-xyz")
        self.assertEqual(c1["mode"], "digest")
        dm = pci.render_dm(c1)
        self.assertIn("[parked]", dm)
        self.assertIn("see dashboard Parked lane", dm)
        self.assertNotIn(p["impact"], dm)

        # Cycle 2: nothing parked this run; prior capture_id earns silence.
        c2 = self._check_i([p])
        pci.apply_park_suppression(c2, {}, parked_state)
        self.assertEqual(c2["proposals"], [])
        self.assertFalse(c2["has_signal"])
        self.assertEqual(c2["mode"], "no-signal")

    def test_park_failure_keeps_proposal_in_dm(self):
        # emit_capture returns None (e.g. token/network failure) → no capture_id
        # recorded → the proposal is NEVER suppressed; its full DM line stays.
        p = self._medium_proposal()
        with mock.patch.object(pci, "emit_capture", return_value=None) as emit:
            parked_now = pci.park_proposals([p], self.fired_at, self.state_path)
        self.assertEqual(emit.call_count, 1)
        self.assertEqual(parked_now, {})
        self.assertFalse(self.state_path.exists(),
                         "no state write when nothing parked")

        parked_state = pci._load_parked_state(self.state_path)
        c = self._check_i([p])
        pci.apply_park_suppression(c, parked_now, parked_state)
        self.assertEqual(len(c["proposals"]), 1)
        self.assertNotIn("parked", c["proposals"][0])
        dm = pci.render_dm(c)
        self.assertNotIn("[parked]", dm)
        self.assertIn(p["impact"], dm)

    def test_park_emit_exception_never_crashes(self):
        # If emit_capture itself raises, park_proposals swallows it (Check I
        # must never crash) and treats the proposal as unparked.
        p = self._medium_proposal()
        with mock.patch.object(pci, "emit_capture",
                               side_effect=RuntimeError("boom")):
            parked_now = pci.park_proposals([p], self.fired_at, self.state_path)
        self.assertEqual(parked_now, {})
        self.assertFalse(self.state_path.exists())

        c = self._check_i([p])
        pci.apply_park_suppression(c, parked_now,
                                   pci._load_parked_state(self.state_path))
        self.assertEqual(len(c["proposals"]), 1)
        self.assertNotIn("parked", c["proposals"][0])

    def test_auto_dispatch_eligible_unaffected(self):
        # An eligible (small + $) proposal is never parked: emit_capture is not
        # called for it and it survives suppression with its full DM line.
        eligible = self._eligible_proposal()
        with mock.patch.object(pci, "emit_capture",
                               return_value="cap-should-not-happen") as emit:
            parked_now = pci.park_proposals(
                [eligible], self.fired_at, self.state_path,
            )
        emit.assert_not_called()
        self.assertEqual(parked_now, {})
        self.assertFalse(self.state_path.exists())

        c = self._check_i([eligible])
        pci.apply_park_suppression(c, parked_now,
                                   pci._load_parked_state(self.state_path))
        self.assertEqual(len(c["proposals"]), 1)
        self.assertNotIn("parked", c["proposals"][0])
        self.assertEqual(c["mode"], "digest")

    def test_mixed_eligible_and_parked_only_ineligible_parked(self):
        # A digest with one eligible + one ineligible (large-effort) proposal:
        # only the ineligible one parks; the eligible one is left for the
        # auto-dispatch path.
        eligible = self._eligible_proposal()
        medium = self._parkable_proposal()
        with mock.patch.object(pci, "emit_capture",
                               return_value="cap-m") as emit:
            parked_now = pci.park_proposals(
                [eligible, medium], self.fired_at, self.state_path,
            )
        self.assertEqual(emit.call_count, 1)
        self.assertEqual(emit.call_args.kwargs["title"], medium["title"])
        self.assertEqual(set(parked_now), {pci._proposal_dedup_key(medium)})

        c = self._check_i([eligible, medium])
        pci.apply_park_suppression(
            c, parked_now, pci._load_parked_state(self.state_path),
        )
        titles = {p["title"]: p for p in c["proposals"]}
        self.assertIn(eligible["title"], titles)
        self.assertIn(medium["title"], titles)
        self.assertNotIn("parked", titles[eligible["title"]])
        self.assertTrue(titles[medium["title"]]["parked"])


class DispatchEnvelopeChatIdTests(unittest.TestCase):
    """#812 null-chat-at-creation fix: the pulse-auto-dispatch envelope stamps a
    reply_chat_id from TELEGRAM_ALLOWED_CHAT_IDS so Beacon's later
    APPROVAL_REQUEST marker carries a real recipient. The key is OMITTED (never
    stamped null) when the allow-list is unset, preserving notifier fallback."""

    def _proposal(self) -> dict:
        return {
            "title": "Review high-σ anomaly task `burned-task-007`",
            "effort": "small",
            "impact": "$9.20 task vs $1.10 baseline (3.7σ above)",
            "rationale": "Ledger flagged this task at 3.7σ above baseline.",
        }

    def test_primary_chat_id_lowest_allowed(self):
        with mock.patch.dict(os.environ,
                             {"TELEGRAM_ALLOWED_CHAT_IDS": "900, 100, 500"}):
            self.assertEqual(pci._primary_chat_id(), 100)

    def test_reply_chat_id_stamped_from_allowed(self):
        with mock.patch.dict(os.environ,
                             {"TELEGRAM_ALLOWED_CHAT_IDS": "7998341473"}):
            env = pci._build_dispatch_envelope(self._proposal(), FIRED_AT)
        self.assertEqual(env["reply_chat_id"], 7998341473)

    def test_reply_chat_id_omitted_when_unset(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_ALLOWED_CHAT_IDS": ""}):
            env = pci._build_dispatch_envelope(self._proposal(), FIRED_AT)
        self.assertNotIn("reply_chat_id", env)


if __name__ == "__main__":
    unittest.main()
