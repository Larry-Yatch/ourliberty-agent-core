#!/usr/bin/env python3
"""Regression gate for mirror_queue_wait_gauge — the "need a third review slot?"
sibling gauge (mirror-two-slot-review-spec §4 PR3, the "or sibling gauge" branch).

Covers:
  1. load_samples window filtering + malformed-line tolerance.
  2. _percentile nearest-rank behavior.
  3. evaluate: thin window holds; below-threshold holds; over-threshold fires;
     cooldown suppresses a repeat fire.
  4. run_once: kill-switch no-op; dry-run never advances state / never DMs.

Run:
    cd /home/larry/agent-core && \
        python3 -m unittest scripts.tests.test_mirror_queue_wait_gauge
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import mirror_queue_wait_gauge as gauge  # noqa: E402


NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


class _GaugeTmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qw-gauge-test-"))
        (self.tmp / "blackboard").mkdir(parents=True, exist_ok=True)
        (self.tmp / "state").mkdir(parents=True, exist_ok=True)
        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(
            "os.environ", {"OURLIBERTY_AGENTS_ROOT": str(self.tmp)}
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_ledger(self, rows: list[str]) -> None:
        self.ledger = self.tmp / "blackboard" / "mirror-queue-wait.jsonl"
        self.ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def _sample(self, minutes_ago: float, wait_sec: float, slot: int = 0) -> str:
        ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        return json.dumps({
            "ts": ts, "task_id": "t", "pr_url": "u",
            "review_slot": slot, "queue_wait_sec": wait_sec,
        })


class PercentileTest(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(gauge._percentile([], 95), 0.0)

    def test_single(self):
        self.assertEqual(gauge._percentile([42.0], 95), 42.0)

    def test_nearest_rank_p95(self):
        vals = [float(i) for i in range(1, 101)]  # 1..100
        self.assertEqual(gauge._percentile(vals, 95), 95.0)

    def test_p95_robust_to_one_outlier(self):
        vals = [60.0] * 19 + [100000.0]  # p95 of 20 → 19th ordered value
        self.assertEqual(gauge._percentile(vals, 95), 60.0)


class LoadSamplesTest(_GaugeTmpBase):
    def test_window_filters_old_rows(self):
        self._write_ledger([
            self._sample(minutes_ago=10, wait_sec=100),          # in window
            self._sample(minutes_ago=60 * 25, wait_sec=999),     # 25h ago — out
        ])
        samples = gauge.load_samples(now=NOW)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["queue_wait_sec"], 100)

    def test_malformed_and_missing_fields_skipped(self):
        self._write_ledger([
            "not json",
            json.dumps({"ts": NOW.isoformat()}),                 # no queue_wait_sec
            json.dumps({"ts": "bad-ts", "queue_wait_sec": 5}),   # unparseable ts
            self._sample(minutes_ago=5, wait_sec=42),            # good
        ])
        samples = gauge.load_samples(now=NOW)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["queue_wait_sec"], 42)

    def test_missing_ledger_is_empty(self):
        self.assertEqual(gauge.load_samples(now=NOW), [])


class EvaluateTest(_GaugeTmpBase):
    def _samples(self, n: int, wait_sec: float) -> list[dict]:
        return [json.loads(self._sample(minutes_ago=i, wait_sec=wait_sec))
                for i in range(n)]

    def test_thin_window_holds(self):
        samples = self._samples(gauge.MIN_SAMPLES - 1, wait_sec=99999)
        fire, reason, stats = gauge.evaluate(samples, {}, now=NOW)
        self.assertFalse(fire)
        self.assertIn("thin window", reason)

    def test_below_threshold_holds(self):
        # Enough samples but low wait → no fire.
        samples = self._samples(gauge.MIN_SAMPLES + 2,
                                wait_sec=gauge.P95_THRESHOLD_SEC - 600)
        fire, reason, stats = gauge.evaluate(samples, {}, now=NOW)
        self.assertFalse(fire)
        self.assertEqual(stats["sample_count"], gauge.MIN_SAMPLES + 2)

    def test_over_threshold_fires(self):
        samples = self._samples(gauge.MIN_SAMPLES + 2,
                                wait_sec=gauge.P95_THRESHOLD_SEC + 600)
        fire, reason, stats = gauge.evaluate(samples, {}, now=NOW)
        self.assertTrue(fire)
        self.assertIn("FIRE", reason)

    def test_cooldown_suppresses_repeat(self):
        samples = self._samples(gauge.MIN_SAMPLES + 2,
                                wait_sec=gauge.P95_THRESHOLD_SEC + 600)
        recent = (NOW - timedelta(hours=1)).isoformat()
        state = {"last_fired_at": recent}
        fire, reason, _ = gauge.evaluate(samples, state, now=NOW)
        self.assertFalse(fire)
        self.assertIn("cooldown", reason)

    def test_expired_cooldown_fires_again(self):
        samples = self._samples(gauge.MIN_SAMPLES + 2,
                                wait_sec=gauge.P95_THRESHOLD_SEC + 600)
        old = (NOW - timedelta(seconds=gauge.FIRE_COOLDOWN_SEC + 10)).isoformat()
        state = {"last_fired_at": old}
        fire, _, _ = gauge.evaluate(samples, state, now=NOW)
        self.assertTrue(fire)


class RunOnceTest(_GaugeTmpBase):
    def _hot_ledger(self):
        rows = [self._sample(minutes_ago=i, wait_sec=gauge.P95_THRESHOLD_SEC + 600)
                for i in range(gauge.MIN_SAMPLES + 2)]
        self._write_ledger(rows)

    def test_kill_switch_no_op(self):
        (self.tmp / "healers.disabled").write_text("x")
        self._hot_ledger()
        with mock.patch.object(gauge, "fire") as f:
            res = gauge.run_once(now=NOW)
        self.assertFalse(res["fired"])
        f.assert_not_called()

    def test_dry_run_decides_but_never_fires_or_persists(self):
        self._hot_ledger()
        with mock.patch.object(gauge, "fire") as f:
            res = gauge.run_once(now=NOW, dry_run=True)
        self.assertTrue(res["should_fire"])
        self.assertFalse(res["fired"])
        f.assert_not_called()
        self.assertFalse(gauge.state_file().exists())

    def test_fire_advances_state(self):
        self._hot_ledger()
        with mock.patch.object(gauge, "fire", return_value=True) as f:
            res = gauge.run_once(now=NOW)
        self.assertTrue(res["fired"])
        f.assert_called_once()
        state = json.loads(gauge.state_file().read_text())
        self.assertTrue(state.get("last_fired_at"))

    def test_fire_suppressed_does_not_advance_state(self):
        # append_alert cooldown → fire() returns False → last_fired_at NOT set.
        self._hot_ledger()
        with mock.patch.object(gauge, "fire", return_value=False):
            res = gauge.run_once(now=NOW)
        self.assertFalse(res["fired"])
        state = json.loads(gauge.state_file().read_text())
        self.assertNotIn("last_fired_at", state)


if __name__ == "__main__":
    unittest.main()
