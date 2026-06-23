#!/usr/bin/env python3
"""Tests for the #8 chokepoint in beacon_approval_handler: predict_sensitive_intent
and trust_decision stamping task['sensitive_intent'] so a FRESH sensitive build
(no declared changed_files) force_asks instead of auto-starting.

Determinism: a balanced preset is written to a temp override so trust_decision's
load_policy() reads a known policy regardless of the host's live config.

Run:
    cd ~/agent-core && \\
        python3 -m unittest scripts.tests.test_beacon_approval_handler_sensitive_intent
"""
from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import trust_policy as tp  # noqa: E402
import beacon_approval_handler as approval  # noqa: E402


class PredictSensitiveIntentTest(unittest.TestCase):
    def test_true_when_text_names_sensitive_work(self):
        self.assertTrue(approval.predict_sensitive_intent({
            "summary": "Tidy the board",
            "prompt": "Add a supabase migration and rotate the deploy token",
        }))

    def test_false_for_benign_text(self):
        self.assertFalse(approval.predict_sensitive_intent({
            "summary": "Tooltip polish",
            "prompt": "Add a hover tooltip to the funnel card",
        }))

    def test_false_when_no_text(self):
        self.assertFalse(approval.predict_sensitive_intent({}))


class TrustDecisionFreshBuildTest(unittest.TestCase):
    def setUp(self):
        self._orig = tp.OVERRIDE_POLICY_PATH
        self._tmp = tempfile.TemporaryDirectory()
        ov = Path(self._tmp.name) / "trust-policy.override.json"
        ov.write_text(json.dumps(tp.policy_for_level("balanced")))
        tp.OVERRIDE_POLICY_PATH = ov

    def tearDown(self):
        tp.OVERRIDE_POLICY_PATH = self._orig
        self._tmp.cleanup()

    def _decide(self, payload):
        return approval.trust_decision(payload)[0]

    def test_fresh_sensitive_build_force_asks(self):
        act = self._decide({
            "target_agent": "forge", "target_repo": "ourliberty-agent-core",
            "changed_files": [],
            "summary": "Pipeline tidy",
            "prompt": "Update the systemd service config and migrate the schema",
        })
        self.assertEqual(act, "force_ask")

    def test_fresh_benign_build_auto_approves(self):
        act = self._decide({
            "target_agent": "forge", "target_repo": "ourliberty-agent-core",
            "changed_files": [],
            "summary": "Funnel polish",
            "prompt": "Improve the mission card hover state",
        })
        self.assertEqual(act, "auto_approve")

    def test_declared_files_are_authoritative_text_not_consulted(self):
        # When a build DECLARES its files, we trust the declaration — the intent
        # scan is only a fallback for empty changed_files. Non-sensitive declared
        # files + sensitive-sounding prose → auto_approve (no false force_ask).
        act = self._decide({
            "target_agent": "forge", "target_repo": "ourliberty-agent-core",
            "changed_files": ["app/components/Card.tsx"],
            "summary": "Card tweak",
            "prompt": "Mentions deploy and migration in passing, but only edits a component",
        })
        self.assertEqual(act, "auto_approve")

    def test_declared_sensitive_file_still_force_asks(self):
        act = self._decide({
            "target_agent": "forge", "target_repo": "ourliberty-agent-core",
            "changed_files": ["config/trust-policy.json"],
            "summary": "x", "prompt": "y",
        })
        self.assertEqual(act, "force_ask")


if __name__ == "__main__":
    unittest.main()
