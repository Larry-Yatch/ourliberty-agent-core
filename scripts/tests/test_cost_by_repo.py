#!/usr/bin/env python3
"""Tests for `cost_by_repo` — the per-repo cost attribution report.

Validates:
  - 3-bucket precedence: INFRA (by source) > PER-REPO (target_repo) > UNATTRIBUTED
  - Reconciliation identity: sum(per-repo) + infra + unattributed == total
  - Config read (sources / source_suffixes / monthly_usd) and its fail-safe
  - None cost_usd coalesces to 0 (never breaks the identity)

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_cost_by_repo
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

import cost_by_repo as cbr  # noqa: E402


INFRA_SOURCES = ["missions-narrator", "ceo_digest_generator.py"]
INFRA_SUFFIXES = ["-telegram-bot"]


class BucketPrecedenceTest(unittest.TestCase):
    def _rows(self):
        return [
            # per-repo builds
            {"source": "inbox-watcher", "target_repo": "ourliberty-agent-core",
             "cost_usd": 0.10},
            {"source": "inbox-watcher", "target_repo": "ourliberty-agent-core",
             "cost_usd": 0.05},
            {"source": "inbox-watcher", "target_repo": "ourliberty-dashboard",
             "cost_usd": 0.20},
            # infra by exact source
            {"source": "missions-narrator", "target_repo": None,
             "cost_usd": 0.01},
            {"source": "ceo_digest_generator.py", "cost_usd": 0.02},
            # infra by suffix family — even though it carries a target_repo,
            # INFRA precedence wins and it is NOT counted per-repo.
            {"source": "forge-telegram-bot",
             "target_repo": "ourliberty-agent-core", "cost_usd": 0.03},
            # unattributed: non-infra, no repo (pre-change history)
            {"source": "inbox-watcher", "target_repo": None, "cost_usd": 0.07},
            {"source": "some-other-source", "cost_usd": 0.04},
        ]

    def test_strict_precedence_buckets(self):
        b = cbr.bucket_costs(self._rows(), INFRA_SOURCES, INFRA_SUFFIXES)
        self.assertAlmostEqual(b["per_repo"]["ourliberty-agent-core"], 0.15)
        self.assertAlmostEqual(b["per_repo"]["ourliberty-dashboard"], 0.20)
        # infra = 0.01 + 0.02 + 0.03 (the telegram-bot row, despite its repo)
        self.assertAlmostEqual(b["infra"], 0.06)
        # unattributed = 0.07 + 0.04
        self.assertAlmostEqual(b["unattributed"], 0.11)

    def test_infra_row_counted_once_not_double(self):
        # The forge-telegram-bot row carries a target_repo but must land ONLY
        # in infra — never also in per-repo.
        b = cbr.bucket_costs(self._rows(), INFRA_SOURCES, INFRA_SUFFIXES)
        # ourliberty-agent-core per-repo total excludes the 0.03 telegram row.
        self.assertAlmostEqual(b["per_repo"]["ourliberty-agent-core"], 0.15)

    def test_reconciliation_identity(self):
        rows = self._rows()
        b = cbr.bucket_costs(rows, INFRA_SOURCES, INFRA_SUFFIXES)
        reassembled = sum(b["per_repo"].values()) + b["infra"] + b["unattributed"]
        raw_total = sum(
            0.0 if r.get("cost_usd") is None else float(r["cost_usd"])
            for r in rows
        )
        self.assertAlmostEqual(reassembled, raw_total)
        self.assertAlmostEqual(b["total"], raw_total)

    def test_none_cost_coalesces_to_zero(self):
        rows = [
            {"source": "inbox-watcher", "target_repo": "repo-a", "cost_usd": None},
            {"source": "missions-narrator", "cost_usd": None},
            {"source": "inbox-watcher", "cost_usd": None},
            {"source": "inbox-watcher", "target_repo": "repo-a", "cost_usd": 0.5},
        ]
        b = cbr.bucket_costs(rows, INFRA_SOURCES, INFRA_SUFFIXES)
        self.assertAlmostEqual(b["per_repo"]["repo-a"], 0.5)
        self.assertAlmostEqual(b["infra"], 0.0)
        self.assertAlmostEqual(b["unattributed"], 0.0)
        self.assertAlmostEqual(b["total"], 0.5)
        # Identity still holds with None rows present.
        self.assertAlmostEqual(
            sum(b["per_repo"].values()) + b["infra"] + b["unattributed"],
            b["total"],
        )

    def test_empty_rows(self):
        b = cbr.bucket_costs([], INFRA_SOURCES, INFRA_SUFFIXES)
        self.assertEqual(b["per_repo"], {})
        self.assertAlmostEqual(b["infra"], 0.0)
        self.assertAlmostEqual(b["unattributed"], 0.0)
        self.assertAlmostEqual(b["total"], 0.0)


class ConfigReadTest(unittest.TestCase):
    def test_load_config_reads_all_fields(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "recurring-infra.json"
            cfg_path.write_text(json.dumps({
                "sources": ["missions-narrator"],
                "source_suffixes": ["-telegram-bot"],
                "monthly_usd": {"supabase_pro": 25.0, "vercel": 20.0},
            }))
            cfg = cbr.load_config(cfg_path)
            self.assertEqual(cfg["sources"], ["missions-narrator"])
            self.assertEqual(cfg["source_suffixes"], ["-telegram-bot"])
            self.assertEqual(cfg["monthly_usd"], {"supabase_pro": 25.0, "vercel": 20.0})

    def test_load_config_failsafe_on_missing_file(self):
        cfg = cbr.load_config(Path("/nonexistent/recurring-infra.json"))
        self.assertEqual(cfg["sources"], [])
        self.assertEqual(cfg["source_suffixes"], [])
        self.assertEqual(cfg["monthly_usd"], {})

    def test_shipped_config_is_valid(self):
        # The real config file must parse and expose the expected keys.
        cfg = cbr.load_config(cbr.DEFAULT_CONFIG_FILE)
        self.assertIsInstance(cfg["sources"], list)
        self.assertIsInstance(cfg["source_suffixes"], list)
        self.assertIsInstance(cfg["monthly_usd"], dict)
        self.assertIn("-telegram-bot", cfg["source_suffixes"])


class IsInfraSourceTest(unittest.TestCase):
    def test_exact_and_suffix_and_none(self):
        self.assertTrue(cbr.is_infra_source(
            "missions-narrator", INFRA_SOURCES, INFRA_SUFFIXES))
        self.assertTrue(cbr.is_infra_source(
            "beacon-telegram-bot", INFRA_SOURCES, INFRA_SUFFIXES))
        self.assertFalse(cbr.is_infra_source(
            "inbox-watcher", INFRA_SOURCES, INFRA_SUFFIXES))
        self.assertFalse(cbr.is_infra_source(
            None, INFRA_SOURCES, INFRA_SUFFIXES))


class BuildReportTest(unittest.TestCase):
    def test_end_to_end_windowed_report(self):
        now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
        in_window = (now - timedelta(days=5)).isoformat()
        out_of_window = (now - timedelta(days=45)).isoformat()
        with tempfile.TemporaryDirectory() as d:
            costs = Path(d) / "costs.jsonl"
            cfg_path = Path(d) / "recurring-infra.json"
            cfg_path.write_text(json.dumps({
                "sources": ["missions-narrator"],
                "source_suffixes": ["-telegram-bot"],
                "monthly_usd": {"supabase_pro": 25.0, "vercel": 20.0},
            }))
            rows = [
                {"ts": in_window, "source": "inbox-watcher",
                 "target_repo": "ourliberty-agent-core", "cost_usd": 0.10},
                {"ts": in_window, "source": "missions-narrator", "cost_usd": 0.02},
                {"ts": in_window, "source": "inbox-watcher", "cost_usd": 0.05},
                # outside window — excluded
                {"ts": out_of_window, "source": "inbox-watcher",
                 "target_repo": "ourliberty-agent-core", "cost_usd": 9.99},
            ]
            costs.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

            report = cbr.build_report(costs, cfg_path, window_days=30, now=now)
            self.assertEqual(report["row_count"], 3)
            llm = report["llm_costs"]
            self.assertAlmostEqual(llm["per_repo"]["ourliberty-agent-core"], 0.10)
            self.assertAlmostEqual(llm["infra"], 0.02)
            self.assertAlmostEqual(llm["unattributed"], 0.05)
            self.assertAlmostEqual(llm["total"], 0.17)
            self.assertAlmostEqual(
                report["recurring_infra_monthly_usd"]["total"], 45.0)
            # render_text should not raise on the assembled report.
            self.assertIn("Per-repo cost attribution", cbr.render_text(report))

    def test_missing_costs_file_is_empty_report(self):
        with tempfile.TemporaryDirectory() as d:
            report = cbr.build_report(
                Path(d) / "nope.jsonl", cbr.DEFAULT_CONFIG_FILE, window_days=30)
            self.assertEqual(report["row_count"], 0)
            self.assertAlmostEqual(report["llm_costs"]["total"], 0.0)


if __name__ == "__main__":
    unittest.main()
