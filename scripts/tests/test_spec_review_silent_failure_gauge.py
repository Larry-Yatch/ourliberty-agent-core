#!/usr/bin/env python3
"""Regression gate for spec_review_silent_failure_gauge — the spec-gauntlet
silent-failure gauge (spec-gauntlet-gate.md §3.5, the trailing measurer that
makes a persistently fail-open gate visible).

Covers:
  1. load_conclusions: concluded_at ordering, malformed/partial tolerance,
     missing-dir empty, task_id fallback to filename stem.
  2. compute_streak: trailing contiguous failure run; a healthy verdict breaks
     the streak; empty ledger.
  3. evaluate: empty holds; below-threshold holds; at/above fires; an already-
     surfaced tail holds (idempotent).
  4. run_once: kill-switch no-op; dry-run decides but never emits/persists;
     a successful surface advances surfaced_tail + last_fired_at; a dropped
     emit does NOT advance; self-clear resets surfaced_tail below threshold.
  5. surface: emits the info-only spec_review_silent_failure chain_event and
     never touches larry_alerts / needs_attention / for_larry (no-DM north star).

Run:
    cd /home/larry/agent-core && \
        python3 -m unittest scripts.tests.test_spec_review_silent_failure_gauge
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

import spec_review_silent_failure_gauge as gauge  # noqa: E402


NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


class _GaugeTmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sfg-gauge-test-"))
        self.concluded = self.tmp / "state" / "spec-review" / "concluded"
        self.concluded.mkdir(parents=True, exist_ok=True)
        (self.tmp / "logs").mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(
            "os.environ", {"OURLIBERTY_AGENTS_ROOT": str(self.tmp)}
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_conclusion(self, task_id: str, terminal_state: str,
                          minutes_ago: float = 0.0,
                          filename: str | None = None,
                          concluded_at: str | None = "auto") -> None:
        body: dict = {"task_id": task_id, "terminal_state": terminal_state,
                      "payload_hash": "h"}
        if concluded_at == "auto":
            body["concluded_at"] = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        elif concluded_at is not None:
            body["concluded_at"] = concluded_at
        name = filename or f"{task_id}.json"
        (self.concluded / name).write_text(json.dumps(body), encoding="utf-8")

    def _fail_ledger(self, n: int) -> None:
        """n consecutive errored conclusions, newest last (t0 oldest)."""
        for i in range(n):
            self._write_conclusion(f"t{i}", "errored", minutes_ago=(n - i) * 10)


class LoadConclusionsTest(_GaugeTmpBase):
    def test_missing_dir_is_empty(self):
        shutil.rmtree(self.concluded)
        self.assertEqual(gauge.load_conclusions(), [])

    def test_orders_by_concluded_at_oldest_first(self):
        self._write_conclusion("newest", "errored", minutes_ago=1)
        self._write_conclusion("oldest", "passed", minutes_ago=100)
        self._write_conclusion("middle", "incomplete", minutes_ago=50)
        got = [r["task_id"] for r in gauge.load_conclusions()]
        self.assertEqual(got, ["oldest", "middle", "newest"])

    def test_malformed_and_partial_skipped(self):
        (self.concluded / "bad.json").write_text("not json", encoding="utf-8")
        (self.concluded / "list.json").write_text("[]", encoding="utf-8")
        self._write_conclusion("nostate", "errored", concluded_at=None)  # has state
        (self.concluded / "nostate2.json").write_text(
            json.dumps({"task_id": "x"}), encoding="utf-8")  # no terminal_state
        self._write_conclusion("good", "passed", minutes_ago=5)
        got = {r["task_id"] for r in gauge.load_conclusions()}
        self.assertIn("good", got)
        self.assertIn("nostate", got)          # missing concluded_at is tolerated
        self.assertNotIn("x", got)             # missing terminal_state dropped

    def test_task_id_falls_back_to_stem(self):
        (self.concluded / "from-name.json").write_text(
            json.dumps({"terminal_state": "errored",
                        "concluded_at": NOW.isoformat()}), encoding="utf-8")
        recs = gauge.load_conclusions()
        self.assertEqual(recs[0]["task_id"], "from-name")


class ComputeStreakTest(_GaugeTmpBase):
    def test_empty(self):
        info = gauge.compute_streak([])
        self.assertEqual(info["streak"], 0)
        self.assertIsNone(info["tail_task_id"])

    def test_trailing_contiguous_only(self):
        recs = [
            {"task_id": "a", "terminal_state": "errored"},
            {"task_id": "b", "terminal_state": "passed"},
            {"task_id": "c", "terminal_state": "errored"},
            {"task_id": "d", "terminal_state": "incomplete"},
        ]
        info = gauge.compute_streak(recs)
        self.assertEqual(info["streak"], 2)               # c, d — not a
        self.assertEqual(info["tail_task_id"], "d")
        self.assertEqual(info["streak_task_ids"], ["c", "d"])

    def test_healthy_verdict_breaks_streak(self):
        recs = [
            {"task_id": "a", "terminal_state": "errored"},
            {"task_id": "b", "terminal_state": "contested"},   # healthy tail
        ]
        info = gauge.compute_streak(recs)
        self.assertEqual(info["streak"], 0)
        self.assertEqual(info["tail_task_id"], "b")


class EvaluateTest(_GaugeTmpBase):
    def test_empty_holds(self):
        fire, reason, _ = gauge.evaluate([], {})
        self.assertFalse(fire)
        self.assertIn("nothing to measure", reason)

    def test_below_threshold_holds(self):
        self._fail_ledger(gauge.MIN_STREAK - 1)
        fire, reason, info = gauge.evaluate(gauge.load_conclusions(), {})
        self.assertFalse(fire)
        self.assertEqual(info["streak"], gauge.MIN_STREAK - 1)

    def test_at_threshold_fires(self):
        self._fail_ledger(gauge.MIN_STREAK)
        fire, reason, info = gauge.evaluate(gauge.load_conclusions(), {})
        self.assertTrue(fire)
        self.assertIn("FIRE", reason)
        self.assertEqual(info["streak"], gauge.MIN_STREAK)

    def test_already_surfaced_tail_holds(self):
        self._fail_ledger(gauge.MIN_STREAK)
        conclusions = gauge.load_conclusions()
        tail = conclusions[-1]["task_id"]
        fire, reason, _ = gauge.evaluate(conclusions, {"surfaced_tail": tail})
        self.assertFalse(fire)
        self.assertIn("already surfaced", reason)


class RunOnceTest(_GaugeTmpBase):
    def test_kill_switch_no_op(self):
        (self.tmp / "healers.disabled").write_text("x")
        self._fail_ledger(gauge.MIN_STREAK)
        with mock.patch.object(gauge, "surface") as s:
            res = gauge.run_once(now=NOW)
        self.assertFalse(res["fired"])
        s.assert_not_called()

    def test_dry_run_decides_but_never_surfaces_or_persists(self):
        self._fail_ledger(gauge.MIN_STREAK)
        with mock.patch.object(gauge, "surface") as s:
            res = gauge.run_once(now=NOW, dry_run=True)
        self.assertTrue(res["should_fire"])
        self.assertFalse(res["fired"])
        s.assert_not_called()
        self.assertFalse(gauge.state_file().exists())

    def test_successful_surface_advances_state(self):
        self._fail_ledger(gauge.MIN_STREAK)
        tail = gauge.load_conclusions()[-1]["task_id"]
        with mock.patch.object(gauge, "surface", return_value=True) as s:
            res = gauge.run_once(now=NOW)
        self.assertTrue(res["fired"])
        s.assert_called_once()
        state = json.loads(gauge.state_file().read_text())
        self.assertEqual(state["surfaced_tail"], tail)
        self.assertTrue(state["last_fired_at"])

    def test_dropped_emit_does_not_advance(self):
        self._fail_ledger(gauge.MIN_STREAK)
        with mock.patch.object(gauge, "surface", return_value=False):
            res = gauge.run_once(now=NOW)
        self.assertFalse(res["fired"])
        self.assertFalse(gauge.state_file().exists())

    def test_idempotent_no_resurface_same_tail(self):
        self._fail_ledger(gauge.MIN_STREAK)
        with mock.patch.object(gauge, "surface", return_value=True):
            gauge.run_once(now=NOW)
        # Second tick, ledger unchanged → tail already surfaced → no re-surface.
        with mock.patch.object(gauge, "surface", return_value=True) as s2:
            res2 = gauge.run_once(now=NOW)
        self.assertFalse(res2["fired"])
        s2.assert_not_called()

    def test_self_clear_resets_surfaced_tail(self):
        # Surface on a stuck streak...
        self._fail_ledger(gauge.MIN_STREAK)
        with mock.patch.object(gauge, "surface", return_value=True):
            gauge.run_once(now=NOW)
        self.assertEqual(
            json.loads(gauge.state_file().read_text())["surfaced_tail"],
            gauge.load_conclusions()[-1]["task_id"])
        # ...then a healthy verdict lands at the tail → streak breaks → clear.
        self._write_conclusion("recovered", "passed", minutes_ago=0)
        with mock.patch.object(gauge, "surface") as s:
            res = gauge.run_once(now=NOW)
        s.assert_not_called()
        self.assertFalse(res["fired"])
        self.assertNotIn("surfaced_tail",
                         json.loads(gauge.state_file().read_text()))

    def test_grown_streak_resurfaces_new_tail(self):
        self._fail_ledger(gauge.MIN_STREAK)
        with mock.patch.object(gauge, "surface", return_value=True):
            gauge.run_once(now=NOW)
        # A new failure lands → tail changes → surface once more.
        self._write_conclusion("t_new", "incomplete", minutes_ago=0)
        with mock.patch.object(gauge, "surface", return_value=True) as s:
            res = gauge.run_once(now=NOW)
        self.assertTrue(res["fired"])
        s.assert_called_once()
        self.assertEqual(
            json.loads(gauge.state_file().read_text())["surfaced_tail"], "t_new")


class SurfaceTest(_GaugeTmpBase):
    def test_emits_info_only_chain_event_no_dm(self):
        info = {
            "streak": 3, "tail_task_id": "tail-x",
            "streak_task_ids": ["a", "b", "tail-x"], "total": 3,
        }
        fake = mock.MagicMock(return_value=True)
        with mock.patch.dict(sys.modules,
                             {"chain_event_emit": mock.MagicMock(emit_event=fake)}):
            self.assertTrue(gauge.surface(info))
        fake.assert_called_once()
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "spec_review_silent_failure")
        self.assertEqual(kwargs["agent"], "spec-review")
        self.assertEqual(kwargs["task_id"], "tail-x")
        # No-DM north star: the payload carries no escalation/for_larry keys.
        payload = kwargs["payload"]
        self.assertNotIn("for_larry", payload)
        self.assertNotIn("needs_attention", payload)
        self.assertEqual(payload["streak"], 3)

    def test_emit_failure_returns_false_never_raises(self):
        info = {"streak": 3, "tail_task_id": "t", "streak_task_ids": ["t"],
                "total": 3}
        boom = mock.MagicMock(side_effect=RuntimeError("supabase down"))
        with mock.patch.dict(sys.modules,
                             {"chain_event_emit": mock.MagicMock(emit_event=boom)}):
            self.assertFalse(gauge.surface(info))


if __name__ == "__main__":
    unittest.main()
