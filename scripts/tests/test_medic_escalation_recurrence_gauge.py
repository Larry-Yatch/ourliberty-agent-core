#!/usr/bin/env python3
"""Regression gate for medic_escalation_recurrence_gauge — the un-park trigger
for the recurring-escalation fan-out (parked spec
agents/beacon/specs/medic-escalation-recurrence-fanout.md).

Covers:
  1. load_escalations: window filtering, outcome!=escalated skipped, malformed
     lines / missing fingerprint tolerated, grouped by fingerprint.
  2. qualifying: count gate (one-off / coincidence never qualify), freshness gate
     (a fat-but-dead burst is excluded — the 2026-07-09 trailing-edge trap).
  3. evaluate: to_fire selection, per-fingerprint cooldown suppression, self-clear
     of a fingerprint that drops below threshold.
  4. run_once: kill-switch no-op; dry-run never fires / never persists; a fired
     fingerprint is stamped; a suppressed fire leaves it un-stamped for retry.

Run:
    cd /home/larry/agent-core && \
        python3 -m unittest scripts.tests.test_medic_escalation_recurrence_gauge
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
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import medic_escalation_recurrence_gauge as gauge  # noqa: E402


NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


class _GaugeTmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="medic-recur-gauge-test-"))
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
        self.ledger = self.tmp / "state" / "medic-handled-ledger.jsonl"
        self.ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def _row(self, fp: str, hours_ago: float = 1.0, outcome: str = "escalated",
             extra: dict | None = None) -> str:
        ts = (NOW - timedelta(hours=hours_ago)).isoformat()
        row = {"ts": ts, "fingerprint": fp, "source": "heal-pipeline-stall",
               "subject": "x", "outcome": outcome, "attempt": 1}
        if extra:
            row.update(extra)
        return json.dumps(row)

    def _escalations(self, fp: str, ages_days: list[float]) -> list[str]:
        return [self._row(fp, hours_ago=d * 24) for d in ages_days]


# -------------------- load_escalations --------------------

class LoadEscalationsTest(_GaugeTmpBase):
    def test_groups_by_fingerprint_within_window(self):
        self._write_ledger(
            self._escalations("fp-a", [0.5, 1.0, 2.0]) +
            self._escalations("fp-b", [0.1])
        )
        esc = gauge.load_escalations(now=NOW)
        self.assertEqual(len(esc["fp-a"]), 3)
        self.assertEqual(len(esc["fp-b"]), 1)

    def test_window_filters_old_rows(self):
        self._write_ledger(
            self._escalations("fp-a", [1.0]) +          # in 7d window
            self._escalations("fp-a", [30.0])           # 30d ago — out
        )
        esc = gauge.load_escalations(now=NOW)
        self.assertEqual(len(esc["fp-a"]), 1)

    def test_non_escalated_outcomes_skipped(self):
        self._write_ledger([
            self._row("fp-a", hours_ago=1, outcome="escalated"),
            self._row("fp-a", hours_ago=2, outcome="skipped"),
            self._row("fp-a", hours_ago=3, outcome="acted"),
        ])
        esc = gauge.load_escalations(now=NOW)
        self.assertEqual(len(esc["fp-a"]), 1)

    def test_malformed_and_missing_fingerprint_skipped(self):
        self._write_ledger([
            "not json",
            json.dumps({"ts": NOW.isoformat(), "outcome": "escalated"}),  # no fp
            json.dumps({"ts": "bad-ts", "fingerprint": "fp-a",
                        "outcome": "escalated"}),                          # bad ts
            self._row("fp-a", hours_ago=1),                               # good
        ])
        esc = gauge.load_escalations(now=NOW)
        self.assertEqual(len(esc.get("fp-a", [])), 1)

    def test_date_only_ts_rejected(self):
        # A bare date has no time-of-day -> would backdate to midnight and skew
        # the freshness gate; it must be treated as unparseable (skipped).
        self._write_ledger([
            json.dumps({"ts": "2026-07-09", "fingerprint": "fp-a",
                        "outcome": "escalated"}),
            self._row("fp-a", hours_ago=1),
        ])
        esc = gauge.load_escalations(now=NOW)
        self.assertEqual(len(esc.get("fp-a", [])), 1)

    def test_duplicate_timestamps_deduped(self):
        # A replayed/append-retry row with an identical ts is one incident, not
        # two — the count gate must not be inflated by it.
        ts_row = self._row("fp-a", hours_ago=5)
        self._write_ledger([
            ts_row, ts_row,                       # same ts twice (replay)
            self._row("fp-a", hours_ago=1),       # a genuinely distinct one
        ])
        esc = gauge.load_escalations(now=NOW)
        self.assertEqual(len(esc["fp-a"]), 2)     # 3 rows -> 2 distinct incidents

    def test_missing_ledger_is_empty(self):
        self.assertEqual(gauge.load_escalations(now=NOW), {})


# -------------------- qualifying (the durability + freshness gates) --------------------

class QualifyingTest(_GaugeTmpBase):
    def _esc(self, ages_days: list[float]) -> dict:
        rows = self._escalations("fp", ages_days)
        self._write_ledger(rows)
        return gauge.load_escalations(now=NOW)

    def test_one_off_never_qualifies(self):
        quals = gauge.qualifying(self._esc([0.5]), now=NOW)
        self.assertEqual(quals, {})

    def test_two_is_coincidence_not_recurring(self):
        quals = gauge.qualifying(self._esc([0.5, 1.5]), now=NOW)
        self.assertEqual(quals, {})

    def test_three_fresh_qualifies(self):
        quals = gauge.qualifying(self._esc([0.2, 1.0, 2.5]), now=NOW)
        self.assertIn("fp", quals)
        self.assertEqual(quals["fp"]["count"], 3)

    def test_fat_but_dead_burst_excluded(self):
        # 5 escalations, all 5-9 days ago in a 7d window? No — put them just
        # inside the window but ALL older than FRESH_DAYS: the trailing-edge
        # corpse. Count passes, freshness fails => excluded.
        quals = gauge.qualifying(self._esc([3.0, 4.0, 5.0, 6.0]), now=NOW)
        self.assertEqual(quals, {}, "a recurring-but-stale burst must not qualify")

    def test_fresh_hit_rescues_a_recurring_fingerprint(self):
        # Same fat history, but with one fresh hit within FRESH_DAYS => live again.
        quals = gauge.qualifying(self._esc([0.5, 4.0, 5.0, 6.0]), now=NOW)
        self.assertIn("fp", quals)
        self.assertEqual(quals["fp"]["count"], 4)

    def test_boundary_min_recur_exact(self):
        # Exactly MIN_RECUR fresh hits qualifies (>= not >).
        ages = [0.1 * i for i in range(gauge.MIN_RECUR)]
        quals = gauge.qualifying(self._esc(ages), now=NOW)
        self.assertIn("fp", quals)


# -------------------- evaluate (cooldown + self-clear) --------------------

class EvaluateTest(_GaugeTmpBase):
    def _quals_ledger(self, fp="fp"):
        self._write_ledger(self._escalations(fp, [0.2, 1.0, 2.5]))
        return gauge.load_escalations(now=NOW)

    def test_fires_when_qualifying_and_no_prior(self):
        esc = self._quals_ledger()
        to_fire, new_state = gauge.evaluate(esc, {"fired": {}}, now=NOW)
        self.assertIn("fp", to_fire)
        # not stamped until fire() succeeds (run_once stamps), so new_state empty
        self.assertNotIn("fp", new_state["fired"])

    def test_cooldown_suppresses_repeat(self):
        esc = self._quals_ledger()
        recent = (NOW - timedelta(hours=1)).isoformat()
        to_fire, new_state = gauge.evaluate(esc, {"fired": {"fp": recent}}, now=NOW)
        self.assertNotIn("fp", to_fire)                 # suppressed
        self.assertEqual(new_state["fired"]["fp"], recent)  # stamp retained

    def test_expired_cooldown_fires_again(self):
        esc = self._quals_ledger()
        old = (NOW - timedelta(seconds=gauge.FIRE_COOLDOWN_SEC + 10)).isoformat()
        to_fire, _ = gauge.evaluate(esc, {"fired": {"fp": old}}, now=NOW)
        self.assertIn("fp", to_fire)

    def test_self_clear_when_no_longer_qualifying(self):
        # fingerprint 'gone' has an old stamp but only ONE escalation now (below
        # threshold) => dropped from new_state so a genuine later burst re-fires.
        self._write_ledger(self._escalations("gone", [1.0]))
        esc = gauge.load_escalations(now=NOW)
        prior = {"fired": {"gone": (NOW - timedelta(days=1)).isoformat()}}
        to_fire, new_state = gauge.evaluate(esc, prior, now=NOW)
        self.assertEqual(to_fire, {})
        self.assertNotIn("gone", new_state["fired"])

    def test_multiple_fingerprints_independent(self):
        self._write_ledger(
            self._escalations("fp-a", [0.2, 1.0, 2.5]) +
            self._escalations("fp-b", [0.3, 1.1, 2.2])
        )
        esc = gauge.load_escalations(now=NOW)
        recent = (NOW - timedelta(hours=1)).isoformat()
        # fp-a in cooldown, fp-b fresh
        to_fire, _ = gauge.evaluate(esc, {"fired": {"fp-a": recent}}, now=NOW)
        self.assertNotIn("fp-a", to_fire)
        self.assertIn("fp-b", to_fire)


# -------------------- run_once --------------------

class RunOnceTest(_GaugeTmpBase):
    def _hot_ledger(self, fp="fp"):
        self._write_ledger(self._escalations(fp, [0.2, 1.0, 2.5]))

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
        self.assertEqual(res["to_fire"], ["fp"])
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
        self.assertIn("fp", state["fired"])

    def test_fire_suppressed_does_not_stamp(self):
        # append_alert cooldown => fire() returns False => fp NOT stamped, retried.
        self._hot_ledger()
        with mock.patch.object(gauge, "fire", return_value=False):
            res = gauge.run_once(now=NOW)
        self.assertFalse(res["fired"])
        state = json.loads(gauge.state_file().read_text())
        self.assertNotIn("fp", state.get("fired", {}))

    def test_no_qualifying_no_fire(self):
        self._write_ledger(self._escalations("fp", [0.5]))  # one-off
        with mock.patch.object(gauge, "fire") as f:
            res = gauge.run_once(now=NOW)
        self.assertFalse(res["fired"])
        f.assert_not_called()

    def test_render_alert_names_fingerprint_and_spec(self):
        self._hot_ledger()
        to_fire, _ = gauge.evaluate(gauge.load_escalations(now=NOW),
                                    {"fired": {}}, now=NOW)
        fp = next(iter(to_fire))
        msg, suggested = gauge._render_alert(fp, to_fire[fp], NOW)
        self.assertIn(fp, msg)
        self.assertIn(gauge.FANOUT_SPEC, suggested)

    def test_fire_uses_per_fingerprint_subject(self):
        # Regression guard: the larry_alerts dedup/cooldown bucket is keyed on
        # `source:subject`; a constant subject would collapse all fingerprints
        # into one bucket and swallow a newly-qualifying sibling. The subject
        # MUST embed the fingerprint.
        captured: dict = {}

        def _fake_append(**kw):
            captured.update(kw)
            return True

        fake = types.SimpleNamespace(append_alert=_fake_append)
        meta = {"count": 3, "last": NOW, "first": NOW - timedelta(days=2)}
        with mock.patch.dict(sys.modules, {"larry_alerts": fake}):
            ok = gauge.fire("fp-xyz", meta, NOW)
        self.assertTrue(ok)
        self.assertIn("fp-xyz", captured["subject"])
        self.assertNotEqual(captured["subject"], gauge.ALERT_SUBJECT)

    def test_staggered_fingerprints_stamp_independently(self):
        # fp-a in cooldown, fp-b fresh: fp-b must fire+stamp even though fp-a
        # doesn't (the per-fingerprint independence the batch bug broke).
        self._write_ledger(
            self._escalations("fp-a", [0.2, 1.0, 2.5]) +
            self._escalations("fp-b", [0.3, 1.1, 2.2])
        )
        recent = (NOW - timedelta(hours=1)).isoformat()
        seed = {"fired": {"fp-a": recent}}
        gauge.save_state(seed)
        with mock.patch.object(gauge, "fire", return_value=True):
            res = gauge.run_once(now=NOW)
        self.assertTrue(res["fired"])
        state = json.loads(gauge.state_file().read_text())
        self.assertEqual(state["fired"]["fp-a"], recent)     # untouched (cooldown)
        self.assertEqual(state["fired"]["fp-b"], NOW.isoformat())  # freshly stamped


if __name__ == "__main__":
    unittest.main()
