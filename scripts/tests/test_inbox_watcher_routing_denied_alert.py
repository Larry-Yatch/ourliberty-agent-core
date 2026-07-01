#!/usr/bin/env python3
"""Regression gate: a routing-denied inbox drop must surface a larry-alert.

Background: 2026-06-10 — dashboard Approve/Reject envelopes passed layer-1
source validation but had no layer-2 FRESH_DISPATCH_ROUTES entry, so the
inbox-watcher dropped them to beacon/.invalid SILENTLY while the API returned
200. Two of Larry's rejections were lost. The fix (a) restores the route; this
test guards the second half of the fix: even when a routing denial is correct
(a genuinely unlisted source), the drop must emit exactly one warning alert so a
broken control surface is never invisible again.

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_inbox_watcher_routing_denied_alert
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_watcher  # noqa: E402


class RoutingDeniedAlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="routing-denied-alert-test-"))
        self.inboxes = self.tmp / "inboxes"
        self.outboxes = self.tmp / "outboxes"
        self.blackboard = self.tmp / "blackboard"
        for agent in ("beacon", "forge", "mirror", "pulse"):
            (self.inboxes / agent).mkdir(parents=True, exist_ok=True)
        self.blackboard.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch.object(inbox_watcher, "INBOXES_ROOT", self.inboxes),
            mock.patch.object(inbox_watcher, "OUTBOXES_ROOT", self.outboxes),
            mock.patch.object(inbox_watcher, "BLACKBOARD", self.blackboard),
            mock.patch.object(inbox_watcher, "COSTS_FILE", self.blackboard / "costs.jsonl"),
            mock.patch.object(inbox_watcher, "LOG_FILE", self.tmp / "watcher.log"),
            # A denied route must never reach an LLM dispatch.
            mock.patch.object(
                inbox_watcher.agent_runner, "run_claude",
                side_effect=AssertionError("run_claude must not run for a denied route"),
            ),
            # Open the tier-pool gate: these tests exercise the ROUTING check,
            # which sits after the gate. Without a usable tier the gate would
            # hold the task before routing runs (§ 10-G).
            mock.patch.object(
                inbox_watcher, "_rotation_gate_block_reason", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # The dispatch_validator enforces a 100-char minimum prompt BEFORE the
    # routing check, so test prompts must clear that bar to exercise routing.
    LONG_PROMPT = (
        "This is a deliberately long task prompt so it clears the "
        "dispatch_validator minimum-length gate and reaches the routing check "
        "where the denial path under test lives."
    )

    def _write_envelope(self, agent: str, name: str, task: dict) -> Path:
        f = self.inboxes / agent / f"{name}.json"
        f.write_text(json.dumps(task))
        return f

    def test_denial_emits_exactly_one_warning_alert(self):
        # pulse->forge is a real denied route: 'pulse' passes layer-1 source
        # validation (it's in ALLOWED_SOURCES) but is not permitted to dispatch
        # work to forge, so the routing check denies it.
        agent = "forge"
        task_file = self._write_envelope(agent, "rogue-dispatch-1", {
            "task_id": "rogue-dispatch-1", "prompt": self.LONG_PROMPT,
            "source": "pulse",
        })
        with mock.patch.object(
            inbox_watcher.larry_alerts, "append_alert", return_value=True,
        ) as appended:
            inbox_watcher.process_task(agent, task_file, models_config={})

        # Dropped to .invalid, not dispatched.
        self.assertFalse(task_file.exists())
        self.assertEqual(
            len(list((self.inboxes / agent / ".invalid").glob("*.json"))), 1)

        # Exactly one alert, with the expected source/subject/severity.
        self.assertEqual(appended.call_count, 1)
        kwargs = appended.call_args.kwargs
        self.assertEqual(kwargs["source"], "inbox-watcher")
        self.assertEqual(kwargs["severity"], "warning")
        self.assertEqual(kwargs["subject"], "routing-denied:pulse->forge")
        # The body identifies the dropped envelope so Larry can re-issue it.
        self.assertIn("rogue-dispatch-1", kwargs["message"])

    def test_allowed_route_emits_no_alert(self):
        # A permitted route (pulse->beacon) must NOT trip the denial alert. We
        # stub the NEXT gate (target_repo) to reject so process_task stops just
        # past the routing check without an LLM call — proving the routing check
        # passed (no alert) while the target_repo drop (which has no alert) ends
        # the path cleanly.
        agent = "beacon"
        task_file = self._write_envelope(agent, "real-proposal-1", {
            "task_id": "real-proposal-1", "prompt": self.LONG_PROMPT,
            "source": "pulse",
        })
        with mock.patch.object(
            inbox_watcher.larry_alerts, "append_alert", return_value=True,
        ) as appended, mock.patch.object(
            inbox_watcher.routing_validator, "check_target_repo",
            return_value=(False, "stubbed stop-after-routing"),
        ):
            inbox_watcher.process_task(agent, task_file, models_config={})
        # Reached the target_repo gate (so routing passed) and dropped there.
        self.assertEqual(appended.call_count, 0)
        self.assertEqual(
            len(list((self.inboxes / agent / ".invalid").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
