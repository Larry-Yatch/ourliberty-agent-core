#!/usr/bin/env python3
"""Tests for the fresh-build sensitive-path soft spot fix (#8): the tight
intent scanner (text_signals_sensitive), the `match_sensitive_intent` rule
hook in _rule_matches/evaluate, and the carve-outs the presets emit. Pure
stdlib — runs without fastapi.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_trust_policy_sensitive_intent
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import trust_policy as tp  # noqa: E402


class ScannerTest(unittest.TestCase):
    def test_hits_sensitive_infra_text(self):
        for t in (
            "migrate the database schema",
            "rotate the deploy credentials",
            "update the systemd config",
            "change a production secret",
            "edit an environment variable",
            "touch the kill switch",
        ):
            self.assertTrue(tp.text_signals_sensitive(t), t)

    def test_misses_benign_text(self):
        for t in (
            "add a tooltip to the funnel card",
            "fix a typo in the README",
            "refactor the mission list rendering",
            "improve test coverage for the panel",
        ):
            self.assertFalse(tp.text_signals_sensitive(t), t)

    def test_is_tight_not_the_broad_careful_set(self):
        # the broad classify_careful words (send/email/notify/delete) must NOT
        # trip the tight gate — that was the whole point of choosing 'tight'
        # (fewer false-asks). 'release'/'deploy' etc. ARE in, by design.
        self.assertFalse(tp.text_signals_sensitive("send a notify email"))
        self.assertFalse(tp.text_signals_sensitive("delete the stale card"))
        self.assertFalse(tp.text_signals_sensitive("remove an unused import"))

    def test_empty_text_is_false(self):
        self.assertFalse(tp.text_signals_sensitive(""))
        self.assertFalse(tp.text_signals_sensitive(None))  # type: ignore[arg-type]


class EvaluateIntentTest(unittest.TestCase):
    def setUp(self):
        self.balanced = tp.policy_for_level("balanced")

    def _act(self, task, policy=None):
        return tp.evaluate(task, policy or self.balanced)[0]

    def test_fresh_sensitive_beacon_build_force_asks(self):
        # the bug: no changed_files, but predicted sensitive → must force_ask
        act = self._act({
            "source": "beacon", "target_agent": "forge",
            "target_repo": "ourliberty-agent-core",
            "changed_files": [], "sensitive_intent": True,
        })
        self.assertEqual(act, "force_ask")

    def test_fresh_benign_beacon_build_auto_approves(self):
        act = self._act({
            "source": "beacon", "target_agent": "forge",
            "target_repo": "ourliberty-agent-core", "changed_files": [],
        })
        self.assertEqual(act, "auto_approve")

    def test_revision_with_declared_sensitive_file_still_force_asks(self):
        # the pre-existing path-glob behavior must be unchanged
        act = self._act({
            "source": "beacon", "target_agent": "forge",
            "target_repo": "ourliberty-agent-core",
            "changed_files": ["config/trust-policy.json"],
        })
        self.assertEqual(act, "force_ask")

    def test_pulse_sensitive_dispatch_force_asks(self):
        act = self._act({
            "source": "pulse-auto-dispatch", "target_agent": "beacon",
            "target_repo": "ourliberty-agent-core",
            "changed_files": [], "sensitive_intent": True,
        })
        self.assertEqual(act, "force_ask")

    def test_pulse_benign_dispatch_auto_approves(self):
        act = self._act({
            "source": "pulse-auto-dispatch", "target_agent": "beacon",
            "target_repo": "ourliberty-agent-core", "changed_files": [],
        })
        self.assertEqual(act, "auto_approve")

    def test_loose_fresh_sensitive_widened_repo_force_asks(self):
        loose = tp.policy_for_level("loose")
        act = self._act({
            "source": "beacon", "target_agent": "forge",
            "target_repo": "ourliberty-dashboard",
            "changed_files": [], "sensitive_intent": True,
        }, loose)
        self.assertEqual(act, "force_ask")


class RuleMatchScopingTest(unittest.TestCase):
    def test_intent_flag_ignored_by_rules_that_dont_opt_in(self):
        # a plain auto_approve rule must not be tripped by sensitive_intent —
        # only rules with match_sensitive_intent: true honor it.
        policy = {
            "version": 1, "default_action": "force_ask",
            "rules": [{"source": "beacon", "target": "forge",
                       "action": "auto_approve"}],
        }
        act = tp.evaluate({
            "source": "beacon", "target_agent": "forge",
            "changed_files": [], "sensitive_intent": True,
        }, policy)[0]
        self.assertEqual(act, "auto_approve")

    def test_intent_rule_respects_source_target_repo_gating(self):
        # match_sensitive_intent only fires AFTER source/target/repo match — a
        # forge-targeted intent rule must not trip on a mirror-targeted task.
        policy = {
            "version": 1, "default_action": "force_ask",
            "rules": [
                {"source": "beacon", "target": "forge",
                 "match_sensitive_intent": True, "action": "force_ask"},
                {"source": "beacon", "target": "mirror",
                 "action": "auto_approve"},
            ],
        }
        act = tp.evaluate({
            "source": "beacon", "target_agent": "mirror",
            "changed_files": [], "sensitive_intent": True,
        }, policy)[0]
        self.assertEqual(act, "auto_approve")


class PresetStructureTest(unittest.TestCase):
    def test_balanced_and_loose_carry_the_carveouts(self):
        for lvl in ("balanced", "loose"):
            pol = tp.policy_for_level(lvl)
            pulse_carveout = [
                r for r in pol["rules"]
                if r.get("source") == "pulse-auto-dispatch"
                and r.get("action") == "force_ask"
            ]
            beacon_carveout = next(
                r for r in pol["rules"]
                if r.get("source") == "beacon" and r.get("action") == "force_ask"
            )
            self.assertEqual(len(pulse_carveout), 1, lvl)
            self.assertTrue(beacon_carveout.get("match_sensitive_intent"), lvl)
            # ordering: each carve-out precedes its auto_approve sibling
            idx_pulse_ask = pol["rules"].index(pulse_carveout[0])
            idx_pulse_ok = next(
                i for i, r in enumerate(pol["rules"])
                if r.get("source") == "pulse-auto-dispatch"
                and r.get("action") == "auto_approve"
            )
            self.assertLess(idx_pulse_ask, idx_pulse_ok, lvl)

    def test_conservative_has_no_rules(self):
        self.assertEqual(tp.policy_for_level("conservative")["rules"], [])


class LiveConfigTest(unittest.TestCase):
    """The shipped config/trust-policy.json (what runs with no override) must
    also close the hole — not just the dial presets."""

    def setUp(self):
        cfg_path = Path(__file__).resolve().parent.parent.parent / "config" / "trust-policy.json"
        self.cfg = json.loads(cfg_path.read_text())

    def test_live_config_force_asks_fresh_sensitive_build(self):
        act = tp.evaluate({
            "source": "beacon", "target_agent": "forge",
            "target_repo": "ourliberty-agent-core",
            "changed_files": [], "sensitive_intent": True,
        }, self.cfg)[0]
        self.assertEqual(act, "force_ask")

    def test_live_config_still_auto_approves_benign_build(self):
        act = tp.evaluate({
            "source": "beacon", "target_agent": "forge",
            "target_repo": "ourliberty-agent-core", "changed_files": [],
        }, self.cfg)[0]
        self.assertEqual(act, "auto_approve")


if __name__ == "__main__":
    unittest.main()
