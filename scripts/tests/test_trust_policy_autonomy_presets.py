#!/usr/bin/env python3
"""Tests for the autonomy-dial presets (#7) in trust_policy: policy_for_level,
the OVERRIDE_POLICY_PATH precedence layer, and summarize_policy honoring the
`_preset` stamp (so 'loose' round-trips). Pure stdlib — runs without fastapi.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_trust_policy_autonomy_presets
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import trust_policy as tp  # noqa: E402


class PolicyForLevelTest(unittest.TestCase):
    def test_each_preset_is_a_valid_loadable_policy(self):
        for level in tp.AUTONOMY_LEVELS:
            pol = tp.policy_for_level(level)
            # load_policy validates schema; a bad preset would fail-close to
            # default-deny with an _error. Round-trip through a temp file.
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "p.json"
                p.write_text(json.dumps(pol))
                loaded = tp.load_policy(p)
            self.assertNotIn("_error", loaded, level)
            self.assertEqual(loaded["default_action"], "force_ask", level)
            self.assertEqual(loaded.get("_preset"), level, level)

    def test_conservative_has_no_auto_approve_rules(self):
        pol = tp.policy_for_level("conservative")
        self.assertEqual(pol["rules"], [])

    def test_balanced_is_agent_core_only(self):
        pol = tp.policy_for_level("balanced")
        auto = [r for r in pol["rules"] if r.get("action") == "auto_approve"]
        # pulse-auto-dispatch (any) + beacon->forge agent-core
        repos = [r.get("repos") for r in auto if r.get("source") == "beacon"]
        self.assertEqual(repos, [["ourliberty-agent-core"]])

    def test_loose_widens_to_all_ourliberty_repos_with_carveout_extended(self):
        pol = tp.policy_for_level("loose")
        auto_beacon = next(
            r for r in pol["rules"]
            if r.get("source") == "beacon" and r.get("action") == "auto_approve"
        )
        carveout = next(
            r for r in pol["rules"]
            if r.get("source") == "beacon" and r.get("action") == "force_ask"
        )
        # the widened auto lane and the carve-out cover the SAME repo set
        self.assertEqual(set(auto_beacon["repos"]), set(tp._LOOSE_REPOS))
        self.assertEqual(set(carveout["repos"]), set(tp._LOOSE_REPOS))
        # carve-out must precede the broad auto rule (first-match-wins)
        self.assertLess(
            pol["rules"].index(carveout), pol["rules"].index(auto_beacon)
        )

    def test_unknown_level_raises(self):
        with self.assertRaises(ValueError):
            tp.policy_for_level("yolo")


class OverrideResolutionTest(unittest.TestCase):
    def setUp(self):
        self._orig = tp.OVERRIDE_POLICY_PATH
        self._tmp = tempfile.TemporaryDirectory()
        tp.OVERRIDE_POLICY_PATH = Path(self._tmp.name) / "trust-policy.override.json"

    def tearDown(self):
        tp.OVERRIDE_POLICY_PATH = self._orig
        self._tmp.cleanup()

    def test_override_wins_and_each_level_round_trips(self):
        for level in tp.AUTONOMY_LEVELS:
            tp.OVERRIDE_POLICY_PATH.write_text(
                json.dumps(tp.policy_for_level(level))
            )
            summ = tp.summarize_policy(tp.load_policy())
            self.assertEqual(summ["level"], level, level)
            self.assertFalse(summ["degraded"], level)
            self.assertTrue(summ["gates"])  # gates always present

    def test_loose_round_trips_distinctly_from_balanced(self):
        # both have auto_approve rules, so only the `_preset` stamp separates
        # them — this is the reason _preset exists.
        tp.OVERRIDE_POLICY_PATH.write_text(json.dumps(tp.policy_for_level("loose")))
        self.assertEqual(tp.summarize_policy(tp.load_policy())["level"], "loose")

    def test_removing_the_override_reverts_to_the_git_policy(self):
        tp.OVERRIDE_POLICY_PATH.write_text(
            json.dumps(tp.policy_for_level("conservative"))
        )
        self.assertTrue(tp.OVERRIDE_POLICY_PATH.exists())
        tp.OVERRIDE_POLICY_PATH.unlink()
        # resolution must no longer point at the override
        self.assertNotEqual(tp._resolve_policy_path(), tp.OVERRIDE_POLICY_PATH)

    def test_malformed_override_degrades_fail_closed(self):
        # The highest-priority safety path: a corrupt override must NOT crash the
        # gate — load_policy fail-closes to force_ask and the panel shows degraded.
        tp.OVERRIDE_POLICY_PATH.write_text("{ not valid json ")
        summ = tp.summarize_policy(tp.load_policy())
        self.assertTrue(summ["degraded"])
        self.assertEqual(summ["level"], "conservative")
        # and the gate itself denies by default
        act, _ = tp.evaluate(
            {"source": "beacon", "target_agent": "forge",
             "target_repo": "ourliberty-agent-core"},
            tp.load_policy(),
        )
        self.assertEqual(act, "force_ask")

    def test_gate_honors_loose_override(self):
        tp.OVERRIDE_POLICY_PATH.write_text(json.dumps(tp.policy_for_level("loose")))
        pol = tp.load_policy()
        # widened lane: a dashboard build auto-approves...
        act, _ = tp.evaluate(
            {"source": "beacon", "target_agent": "forge",
             "target_repo": "ourliberty-dashboard"},
            pol,
        )
        self.assertEqual(act, "auto_approve")
        # ...but a declared sensitive path still asks (carve-out extended)
        act2, _ = tp.evaluate(
            {"source": "beacon", "target_agent": "forge",
             "target_repo": "ourliberty-dashboard",
             "changed_files": ["config/x.json"]},
            pol,
        )
        self.assertEqual(act2, "force_ask")


class SummarizePresetFallbackTest(unittest.TestCase):
    def test_hand_edited_policy_without_preset_still_infers_level(self):
        # no _preset stamp → fall back to the auto_starts heuristic
        balanced_like = {
            "version": 1, "default_action": "force_ask",
            "rules": [{"source": "beacon", "target": "forge",
                       "action": "auto_approve"}],
        }
        self.assertEqual(
            tp.summarize_policy(balanced_like)["level"], "balanced"
        )
        empty = {"version": 1, "default_action": "force_ask", "rules": []}
        self.assertEqual(tp.summarize_policy(empty)["level"], "conservative")

    def test_unknown_preset_value_falls_back_to_heuristic(self):
        weird = {
            "version": 1, "default_action": "force_ask", "_preset": "bogus",
            "rules": [],
        }
        # bogus _preset is ignored; empty rules → conservative
        self.assertEqual(tp.summarize_policy(weird)["level"], "conservative")


if __name__ == "__main__":
    unittest.main()
