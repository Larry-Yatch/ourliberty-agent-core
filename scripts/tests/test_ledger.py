#!/usr/bin/env python3
"""Tests for `ledger_weekly` — synthetic costs.jsonl + archives -> JSON sidecar.

Validates:
  - Window filtering on the 7-day boundary (inclusive start, exclusive end)
  - task_type attribution via outbox-archive join (with `unknown` fallback)
  - JSON sidecar schema conforms to spec § 7 (field names, types, shapes)
  - Ramp-up posture (< RAMP_UP_WEEKS prior sidecars → synthetic notice, no σ flags)
  - σ-flagging when enough prior sidecars exist
  - Week-over-week delta (null when no prior week; populated when present)
  - Atomic write + sentinel touch
  - End-to-end main() invocation with --no-dm

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_ledger
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import ledger_weekly as lw  # noqa: E402


WEEK_ENDING = datetime(2026, 5, 18, 0, 0, 0, tzinfo=timezone.utc)


def _write_costs(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_archive(outbox_root: Path, agent: str, task_id: str, task_type: str) -> None:
    archive = outbox_root / agent / ".archive"
    archive.mkdir(parents=True, exist_ok=True)
    with open(archive / f"{task_id}.json", "w", encoding="utf-8") as f:
        json.dump({"task_id": task_id, "agent": agent, "task_type": task_type}, f)


def _write_prior_sidecar(
    output_dir: Path, week_ending_date: str, total_usd: float,
    by_task_type: dict[str, dict[str, float | int]] | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    sidecar = {
        "schema_version": "v1",
        "week_ending": week_ending_date,
        "total_usd": total_usd,
        "delta_vs_prior_week": None,
        "by_agent": {},
        "by_task_type": by_task_type or {},
        "anomalies": [],
        "retry_overhead": {"total_retry_cost_usd": 0.0, "percent_of_total": 0.0},
        "top_5_tasks": [],
    }
    with open(output_dir / f"weekly-{week_ending_date}.json", "w", encoding="utf-8") as f:
        json.dump(sidecar, f)


class WindowFilterTests(unittest.TestCase):
    """Window is [week_ending - 7 days, week_ending). Inclusive start, exclusive end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_filters_rows_outside_window(self):
        costs = self.root / "costs.jsonl"
        _write_costs(costs, [
            {  # exactly at window start — included
                "ts": "2026-05-11T00:00:00+00:00", "agent": "forge",
                "task_id": "t-in-start", "model": "claude-opus-4-7",
                "cost_usd": 0.10, "source": "inbox-watcher",
            },
            {  # one second before window end — included
                "ts": "2026-05-17T23:59:59+00:00", "agent": "beacon",
                "task_id": "t-in-end", "model": "claude-opus-4-7",
                "cost_usd": 0.20, "source": "inbox-watcher",
            },
            {  # at window end — excluded
                "ts": "2026-05-18T00:00:00+00:00", "agent": "pulse",
                "task_id": "t-out-end", "model": "claude-sonnet-4-6",
                "cost_usd": 0.30, "source": "inbox-watcher",
            },
            {  # before window start — excluded
                "ts": "2026-05-10T23:59:59+00:00", "agent": "mirror",
                "task_id": "t-out-start", "model": "claude-opus-4-7",
                "cost_usd": 0.40, "source": "inbox-watcher",
            },
            {  # naive ts (no TZ) — treated as UTC, in window
                "ts": "2026-05-12T12:00:00", "agent": "forge",
                "task_id": "t-naive", "model": "claude-opus-4-7",
                "cost_usd": 0.50, "source": "run_cycle.sh",
            },
        ])
        rows, skipped = lw.load_cost_rows(
            costs, WEEK_ENDING - lw.timedelta(days=7), WEEK_ENDING,
        )
        ids = sorted(r.task_id for r in rows)
        self.assertEqual(ids, ["t-in-end", "t-in-start", "t-naive"])
        self.assertEqual(skipped, 0)

    def test_skips_malformed_lines(self):
        costs = self.root / "costs.jsonl"
        costs.parent.mkdir(parents=True, exist_ok=True)
        with open(costs, "w") as f:
            f.write('{"this is not": valid json}\n')
            f.write('\n')  # blank — silently skipped
            f.write(json.dumps({  # valid
                "ts": "2026-05-12T00:00:00+00:00", "agent": "forge",
                "task_id": "t-ok", "model": "claude-opus-4-7",
                "cost_usd": 0.10, "source": "inbox-watcher",
            }) + "\n")
            f.write(json.dumps({  # missing cost_usd — counts as skip
                "ts": "2026-05-13T00:00:00+00:00", "agent": "forge",
                "task_id": "t-no-cost", "model": "claude-opus-4-7",
                "source": "inbox-watcher",
            }) + "\n")
        rows, skipped = lw.load_cost_rows(
            costs, WEEK_ENDING - lw.timedelta(days=7), WEEK_ENDING,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, 2)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            lw.load_cost_rows(
                self.root / "nope.jsonl", WEEK_ENDING - lw.timedelta(days=7), WEEK_ENDING,
            )


class TaskTypeAttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_join_against_archive(self):
        outbox = self.root / "outboxes"
        _write_archive(outbox, "forge", "task-a", "feature-development")
        _write_archive(outbox, "beacon", "task-b", "doc-only")
        rows = [
            lw.CostRow(
                ts=WEEK_ENDING, agent="forge", task_id="task-a",
                model="m", cost_usd=1.0, duration_sec=10, source="inbox-watcher",
            ),
            lw.CostRow(
                ts=WEEK_ENDING, agent="beacon", task_id="task-b",
                model="m", cost_usd=2.0, duration_sec=20, source="inbox-watcher",
            ),
            lw.CostRow(
                ts=WEEK_ENDING, agent="pulse", task_id="cycle-x",
                model="m", cost_usd=0.5, duration_sec=5, source="run_cycle.sh",
            ),
        ]
        lw.attribute_task_types(rows, outbox)
        self.assertEqual(rows[0].task_type, "feature-development")
        self.assertEqual(rows[1].task_type, "doc-only")
        # PR #34: when archive lookup misses, fall back to infer_task_type
        # by prefix. "cycle-x" -> "cycle" (was "unknown" pre-PR-#34).
        self.assertEqual(rows[2].task_type, "cycle")

    def test_handles_malformed_archive(self):
        outbox = self.root / "outboxes"
        archive_dir = outbox / "forge" / ".archive"
        archive_dir.mkdir(parents=True)
        (archive_dir / "task-bad.json").write_text("{not json")
        rows = [lw.CostRow(
            ts=WEEK_ENDING, agent="forge", task_id="task-bad",
            model="m", cost_usd=1.0, duration_sec=10, source="inbox-watcher",
        )]
        lw.attribute_task_types(rows, outbox)
        # PR #34: archive unreadable -> infer_task_type fallback. "task-bad"
        # matches no known prefix -> "unclassified" (was "unknown" pre-PR-#34).
        self.assertEqual(rows[0].task_type, "unclassified")


class SchemaConformanceTests(unittest.TestCase):
    """JSON sidecar shape matches spec § 7 byte-for-byte for field names + types."""

    REQUIRED_TOP_LEVEL = {
        "schema_version", "week_ending", "total_usd", "delta_vs_prior_week",
        "by_agent", "by_task_type", "anomalies", "retry_overhead", "top_5_tasks",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.costs = self.root / "costs.jsonl"
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "ledger"
        _write_costs(self.costs, [
            {
                "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
                "task_id": "t-feat", "model": "claude-opus-4-7",
                "cost_usd": 0.50, "input_tokens": 5, "output_tokens": 100,
                "cache_read": 1000, "cache_creation": 200, "duration_sec": 4.5,
                "source": "inbox-watcher",
            },
            {
                "ts": "2026-05-13T11:00:00+00:00", "agent": "beacon",
                "task_id": "t-doc", "model": "claude-opus-4-7",
                "cost_usd": 0.25, "source": "inbox-watcher",
            },
            {
                "ts": "2026-05-14T12:00:00+00:00", "agent": "pulse",
                "task_id": "notify-retry-001", "model": "claude-sonnet-4-6",
                "cost_usd": 0.10, "source": "inbox-watcher",
            },
        ])
        _write_archive(self.outbox, "forge", "t-feat", "feature-development")
        _write_archive(self.outbox, "beacon", "t-doc", "doc-only")

    def test_top_level_keys_match_spec(self):
        _, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        self.assertEqual(set(sidecar.keys()), self.REQUIRED_TOP_LEVEL)
        self.assertEqual(sidecar["schema_version"], "v1")
        self.assertEqual(sidecar["week_ending"], "2026-05-18")

    def test_types_match_spec(self):
        _, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        self.assertIsInstance(sidecar["total_usd"], float)
        self.assertIsInstance(sidecar["by_agent"], dict)
        self.assertIsInstance(sidecar["by_task_type"], dict)
        self.assertIsInstance(sidecar["anomalies"], list)
        self.assertIsInstance(sidecar["retry_overhead"], dict)
        self.assertIsInstance(sidecar["top_5_tasks"], list)
        # delta is None when no prior week
        self.assertIsNone(sidecar["delta_vs_prior_week"])
        # by_agent bucket shape
        for bucket in sidecar["by_agent"].values():
            self.assertIn("usd", bucket)
            self.assertIn("task_count", bucket)
            self.assertIsInstance(bucket["usd"], float)
            self.assertIsInstance(bucket["task_count"], int)
        # retry_overhead keys
        self.assertEqual(
            set(sidecar["retry_overhead"].keys()),
            {"total_retry_cost_usd", "percent_of_total"},
        )
        # top_5_tasks element shape
        for t in sidecar["top_5_tasks"]:
            self.assertEqual(set(t.keys()), {"task_id", "agent", "cost_usd"})

    def test_totals_and_aggregations(self):
        _, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        self.assertAlmostEqual(sidecar["total_usd"], 0.85, places=10)
        self.assertEqual(sidecar["by_agent"]["forge"]["task_count"], 1)
        self.assertAlmostEqual(sidecar["by_agent"]["forge"]["usd"], 0.50)
        self.assertAlmostEqual(sidecar["by_task_type"]["doc-only"]["usd"], 0.25)
        # `notify-retry-001` row → task_type "notification" (PR #34: inferred
        # from the "notify-" prefix; pre-PR-#34 this landed in "unknown").
        self.assertEqual(sidecar["by_task_type"]["notification"]["task_count"], 1)

    def test_retry_overhead_heuristic(self):
        _, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        # PR #33: notify-* rows are intentionally excluded from the retry
        # overhead heuristic — they're inter-agent workflow plumbing, not
        # retries. With only `notify-retry-001` in the fixture's retry-ish
        # set, the new total is $0.00 (was $0.10 pre-PR-#33).
        ro = sidecar["retry_overhead"]
        self.assertAlmostEqual(ro["total_retry_cost_usd"], 0.0)
        self.assertAlmostEqual(ro["percent_of_total"], 0.0)

    def test_top_5_sorted_desc(self):
        _, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        costs = [t["cost_usd"] for t in sidecar["top_5_tasks"]]
        self.assertEqual(costs, sorted(costs, reverse=True))
        self.assertEqual(sidecar["top_5_tasks"][0]["task_id"], "t-feat")


class RampUpTests(unittest.TestCase):
    """< RAMP_UP_WEEKS prior sidecars → suspended σ-flagging + ramp-up notice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.costs = self.root / "costs.jsonl"
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "ledger"
        # one in-window row, expensive
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t-big", "model": "claude-opus-4-7",
            "cost_usd": 99.99, "source": "inbox-watcher",
        }])
        _write_archive(self.outbox, "forge", "t-big", "feature-development")

    def test_no_prior_sidecars_emits_ramp_up(self):
        report, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        self.assertFalse(report.sigma_flagging_active)
        self.assertEqual(len(sidecar["anomalies"]), 1)
        self.assertEqual(sidecar["anomalies"][0]["task_id"], "_ramp_up_notice")

    def test_three_prior_still_ramp_up(self):
        # 3 priors < RAMP_UP_WEEKS (4) → still suspended
        for i in range(1, 4):
            d = (WEEK_ENDING - lw.timedelta(days=7 * i)).date().isoformat()
            _write_prior_sidecar(
                self.output_dir, d, 10.0,
                by_task_type={"feature-development": {"usd": 4.0, "task_count": 2}},
            )
        report, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        self.assertFalse(report.sigma_flagging_active)
        self.assertEqual(sidecar["anomalies"][0]["task_id"], "_ramp_up_notice")


class SigmaFlaggingTests(unittest.TestCase):
    """With ≥ RAMP_UP_WEEKS priors and variance, σ-flagging emits real anomalies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.costs = self.root / "costs.jsonl"
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "ledger"
        # 4 prior weeks of stable feature-development at ~$1/task
        for i, mean_cost in enumerate([1.0, 1.1, 0.95, 1.05], start=1):
            d = (WEEK_ENDING - lw.timedelta(days=7 * i)).date().isoformat()
            _write_prior_sidecar(
                self.output_dir, d, mean_cost * 2,
                by_task_type={"feature-development": {"usd": mean_cost * 2, "task_count": 2}},
            )

    def test_expensive_task_flags(self):
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t-spike", "model": "claude-opus-4-7",
            "cost_usd": 10.0,  # ~10x baseline
            "source": "inbox-watcher",
        }])
        _write_archive(self.outbox, "forge", "t-spike", "feature-development")
        report, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        self.assertTrue(report.sigma_flagging_active)
        real_anoms = [a for a in sidecar["anomalies"] if a["task_id"] != "_ramp_up_notice"]
        self.assertEqual(len(real_anoms), 1)
        a = real_anoms[0]
        self.assertEqual(a["task_id"], "t-spike")
        self.assertGreaterEqual(a["sigma_above"], lw.SIGMA_THRESHOLD)
        self.assertIn("baseline", a["context"])

    def test_normal_task_does_not_flag(self):
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t-normal", "model": "claude-opus-4-7",
            "cost_usd": 1.02,
            "source": "inbox-watcher",
        }])
        _write_archive(self.outbox, "forge", "t-normal", "feature-development")
        _, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        real_anoms = [a for a in sidecar["anomalies"] if a["task_id"] != "_ramp_up_notice"]
        self.assertEqual(real_anoms, [])


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.costs = self.root / "costs.jsonl"
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "ledger"
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t-1", "model": "claude-opus-4-7",
            "cost_usd": 12.0, "source": "inbox-watcher",
        }])

    def test_null_when_no_prior(self):
        _, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        self.assertIsNone(sidecar["delta_vs_prior_week"])

    def test_populated_when_prior_exists(self):
        prior_date = (WEEK_ENDING - lw.timedelta(days=7)).date().isoformat()
        _write_prior_sidecar(self.output_dir, prior_date, 10.0)
        _, sidecar, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        d = sidecar["delta_vs_prior_week"]
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d["absolute_usd"], 2.0)
        self.assertAlmostEqual(d["percent"], 20.0)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_atomic_write_and_sentinel(self):
        target = self.root / "subdir" / "file.txt"
        lw.atomic_write(target, "hello")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "hello")
        # no leftover temp files
        tmps = list(target.parent.glob(".*.tmp"))
        self.assertEqual(tmps, [])

        sentinel = self.root / "subdir" / "ledger-ready-2026-05-18"
        lw.touch_sentinel(sentinel)
        self.assertTrue(sentinel.exists())


class MarkdownRenderTests(unittest.TestCase):
    """Markdown renders without error for empty + populated + ramp-up shapes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.costs = self.root / "costs.jsonl"
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "ledger"

    def test_renders_with_data(self):
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t", "model": "claude-opus-4-7",
            "cost_usd": 1.23456, "source": "inbox-watcher",
        }])
        _write_archive(self.outbox, "forge", "t", "feature-development")
        report, _, skipped = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        md = lw.render_markdown(report, skipped)
        self.assertIn("# Ledger — Weekly Report (2026-05-18)", md)
        self.assertIn("**Total spend:** $1.23", md)  # rounded to 2dp
        self.assertIn("feature-development", md)
        self.assertIn("`t`", md)

    def test_renders_empty_week(self):
        _write_costs(self.costs, [])
        report, _, skipped = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        md = lw.render_markdown(report, skipped)
        self.assertIn("**Total spend:** $0.00", md)
        self.assertIn("_no rows_", md)


class DmHeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.costs = self.root / "costs.jsonl"
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "ledger"

    def test_heartbeat_shape_during_rampup(self):
        # During ramp-up, no real anomalies, no delta → heartbeat
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t", "model": "claude-opus-4-7",
            "cost_usd": 1.0, "source": "inbox-watcher",
        }])
        report, _, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        msg, is_anom = lw.render_dm_headline(report, Path("/tmp/r.md"))
        self.assertFalse(is_anom)
        self.assertIn("all within baseline", msg)
        self.assertIn("📒 Week of 2026-05-18", msg)

    def test_anomaly_shape_when_sigma_flag(self):
        # 4 priors of stable cost; then a spike
        for i, mean_cost in enumerate([1.0, 1.1, 0.95, 1.05], start=1):
            d = (WEEK_ENDING - lw.timedelta(days=7 * i)).date().isoformat()
            _write_prior_sidecar(
                self.output_dir, d, mean_cost * 2,
                by_task_type={"feature-development": {"usd": mean_cost * 2, "task_count": 2}},
            )
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t-spike", "model": "claude-opus-4-7",
            "cost_usd": 10.0, "source": "inbox-watcher",
        }])
        _write_archive(self.outbox, "forge", "t-spike", "feature-development")
        report, _, _ = lw.compute_weekly_report(
            WEEK_ENDING, self.costs, self.outbox, self.output_dir,
        )
        msg, is_anom = lw.render_dm_headline(report, Path("/tmp/r.md"))
        self.assertTrue(is_anom)
        self.assertIn("top anomaly", msg)
        self.assertIn("t-spike", msg)


class MainEndToEndTests(unittest.TestCase):
    """Drive main() with --no-dm; verify outputs land + sentinel exists + journal grows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.costs = self.root / "costs.jsonl"
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "ledger"
        self.journal = self.root / "ledger-journal.md"
        self.halt = self.root / "EMERGENCY_HALT"
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t-e2e", "model": "claude-opus-4-7",
            "cost_usd": 0.42, "source": "inbox-watcher",
        }])
        _write_archive(self.outbox, "forge", "t-e2e", "feature-development")

    def _argv(self) -> list[str]:
        return [
            "--week-ending", "2026-05-18",
            "--costs-file", str(self.costs),
            "--outbox-root", str(self.outbox),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt),
            "--no-dm",
        ]

    def test_end_to_end_writes_all_artifacts(self):
        rc = lw.main(self._argv())
        self.assertEqual(rc, 0)
        md = self.output_dir / "weekly-2026-05-18.md"
        js = self.output_dir / "weekly-2026-05-18.json"
        sentinel = self.output_dir / "ledger-ready-2026-05-18"
        self.assertTrue(md.exists())
        self.assertTrue(js.exists())
        self.assertTrue(sentinel.exists())
        # JSON round-trips and conforms to the spec schema
        with open(js) as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["schema_version"], "v1")
        self.assertEqual(sidecar["week_ending"], "2026-05-18")
        self.assertAlmostEqual(sidecar["total_usd"], 0.42)
        # journal has an iteration 1 entry
        self.assertTrue(self.journal.exists())
        text = self.journal.read_text()
        self.assertIn("## Iteration 1", text)
        self.assertIn("**Week ending:** 2026-05-18", text)

    def test_iteration_monotonic(self):
        lw.main(self._argv())
        lw.main(self._argv())
        text = self.journal.read_text()
        self.assertIn("## Iteration 1", text)
        self.assertIn("## Iteration 2", text)

    def test_emergency_halt_skips_run(self):
        self.halt.parent.mkdir(parents=True, exist_ok=True)
        self.halt.touch()
        rc = lw.main(self._argv())
        self.assertEqual(rc, 0)
        # no outputs
        self.assertFalse((self.output_dir / "weekly-2026-05-18.md").exists())
        self.assertFalse((self.output_dir / "weekly-2026-05-18.json").exists())
        self.assertFalse((self.output_dir / "ledger-ready-2026-05-18").exists())

    def test_missing_costs_file_exits_nonzero(self):
        os.unlink(self.costs)
        rc = lw.main(self._argv())
        self.assertEqual(rc, 1)
        # journal still gets a failure entry
        self.assertTrue(self.journal.exists())
        self.assertIn("🔴 Failed", self.journal.read_text())


class DmSeverityRoutingTests(unittest.TestCase):
    """Drive main() WITHOUT --no-dm; capture the severity passed into
    larry_alerts.append_alert. Routine weeks emit `info` (append_alert maps
    info->digest, no DM); anomaly weeks emit `warning` (->escalate, DMs Larry).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.costs = self.root / "costs.jsonl"
        self.outbox = self.root / "outboxes"
        self.output_dir = self.root / "ledger"
        self.journal = self.root / "ledger-journal.md"
        self.halt = self.root / "EMERGENCY_HALT"

    def _argv(self) -> list[str]:
        return [
            "--week-ending", "2026-05-18",
            "--costs-file", str(self.costs),
            "--outbox-root", str(self.outbox),
            "--output-dir", str(self.output_dir),
            "--journal", str(self.journal),
            "--halt-flag", str(self.halt),
        ]

    def _run_and_capture_severity(self) -> str:
        import larry_alerts  # noqa: E402

        captured = {}

        def _fake_append_alert(*args, **kwargs):
            captured.update(kwargs)
            return True

        with mock.patch.object(larry_alerts, "append_alert", _fake_append_alert):
            rc = lw.main(self._argv())
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("source"), "ledger")
        self.assertEqual(captured.get("subject"), "weekly-2026-05-18")
        return captured["severity"]

    def test_routine_week_emits_info_severity(self):
        # Single cheap cost, no priors → heartbeat shape, is_anomaly False.
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t-routine", "model": "claude-opus-4-7",
            "cost_usd": 0.42, "source": "inbox-watcher",
        }])
        _write_archive(self.outbox, "forge", "t-routine", "feature-development")
        self.assertEqual(self._run_and_capture_severity(), "info")

    def test_anomaly_week_emits_warning_severity(self):
        # 4 stable priors then a spike → real σ anomaly, is_anomaly True.
        for i, mean_cost in enumerate([1.0, 1.1, 0.95, 1.05], start=1):
            d = (WEEK_ENDING - lw.timedelta(days=7 * i)).date().isoformat()
            _write_prior_sidecar(
                self.output_dir, d, mean_cost * 2,
                by_task_type={"feature-development": {"usd": mean_cost * 2, "task_count": 2}},
            )
        _write_costs(self.costs, [{
            "ts": "2026-05-12T10:00:00+00:00", "agent": "forge",
            "task_id": "t-spike", "model": "claude-opus-4-7",
            "cost_usd": 10.0, "source": "inbox-watcher",
        }])
        _write_archive(self.outbox, "forge", "t-spike", "feature-development")
        self.assertEqual(self._run_and_capture_severity(), "warning")


if __name__ == "__main__":
    unittest.main()
