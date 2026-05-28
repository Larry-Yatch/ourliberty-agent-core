#!/usr/bin/env python3
"""Regression test for the inbox-watcher dispatch-boundary fixture gate.

Closes the 2026-05-28 inbox-watcher fixture-leak loop (104 dispatches /
$33.44 burn in 3h). Pairs with
`docs/inbox-watcher-fixture-gate-brief.md` and the gate landed in
`scripts/inbox_watcher.process_task`.

Properties asserted:

  1. Fixture envelopes — bare AND wrapper-prefixed (brief-named
     `marker-error-notify-t-pf-1.json` + `notify-dead-letter-notify-q-1.18.json`)
     — are moved to `.archive/` WITHOUT a `run_claude` invocation. No
     outbox is written; no cost is appended. The `fixture-suppressed`
     log line is emitted.

  2. Real envelopes are NOT suppressed by the gate — `run_claude` is
     called as today. Guards against an accidental gate broadening that
     would suppress real work.

  3. Mixed batch (3 fixture + 2 real) — exactly the 2 real envelopes
     reach `run_claude`, all 5 land in `.archive/`. The gate doesn't
     bleed fixture work into real dispatch or vice versa.

Test isolation: tmp_path-rooted INBOXES_ROOT / OUTBOXES_ROOT /
BLACKBOARD / LOG_FILE. No mutation of `~/agents/`. `run_claude` is
mocked at the module attribute (no subprocess fires).

Run:
    python3 -m unittest scripts.tests.test_inbox_watcher_fixture_gate
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_watcher  # noqa: E402


_REAL_TASK = {
    "task_id": "build-real-thing-v1",
    "agent_id": "forge",
    "source": "beacon",
    "intent": "build",
    "prompt": "do the thing",
    "target_repo": "ourliberty-agent-core",
}


def _write_envelope(inbox_dir: Path, name: str, body: dict) -> Path:
    inbox_dir.mkdir(parents=True, exist_ok=True)
    p = inbox_dir / name
    p.write_text(json.dumps(body))
    return p


class _GateTestBase(unittest.TestCase):
    """tmp_path-isolated inbox-watcher harness. Subclasses get a stub
    inbox + a mocked `run_claude` + a redirected logs/blackboard tree."""

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._tmp = TemporaryDirectory(prefix="inbox-fixture-gate-")
        self.tmp_path = Path(self._tmp.name)
        self.agents_root = self.tmp_path / "agents"
        self.inboxes_root = self.agents_root / "inboxes"
        self.outboxes_root = self.agents_root / "outboxes"
        self.blackboard = self.agents_root / "blackboard"
        self.logs_dir = self.agents_root / "logs"
        self.log_file = self.logs_dir / "inbox_watcher.log"
        for d in (
            self.inboxes_root / "forge",
            self.inboxes_root / "forge" / ".archive",
            self.inboxes_root / "forge" / ".invalid",
            self.outboxes_root / "forge",
            self.blackboard,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.costs_file = self.blackboard / "costs.jsonl"

        # Patch module constants onto tmp_path so process_task hits our tree.
        self._patches = [
            mock.patch.object(inbox_watcher, "INBOXES_ROOT", self.inboxes_root),
            mock.patch.object(inbox_watcher, "OUTBOXES_ROOT", self.outboxes_root),
            mock.patch.object(inbox_watcher, "BLACKBOARD", self.blackboard),
            mock.patch.object(inbox_watcher, "LOG_FILE", self.log_file),
            mock.patch.object(inbox_watcher, "COSTS_FILE", self.costs_file),
            # Validators: pass-through. Real-envelope dispatch path needs
            # them to succeed so we can assert run_claude IS called.
            mock.patch.object(
                inbox_watcher.dispatch_validator,
                "validate_task",
                return_value=(True, ""),
            ),
            mock.patch.object(
                inbox_watcher.routing_validator,
                "check_hard_topology",
                return_value=(True, ""),
            ),
            mock.patch.object(
                inbox_watcher.routing_validator,
                "check_target_repo",
                return_value=(True, ""),
            ),
            # run_claude: success, no real subprocess fires. out_meta is
            # filled by the real runner; provide a dict patch via side_effect.
            mock.patch.object(inbox_watcher.agent_runner, "run_claude"),
        ]
        self._started = [p.start() for p in self._patches]
        self.run_claude = self._started[-1]

        def _fake_run_claude(**kwargs):
            meta = kwargs.get("out_meta")
            if isinstance(meta, dict):
                meta.update({
                    "started_at": "2026-05-28T00:00:00+00:00",
                    "completed_at": "2026-05-28T00:00:01+00:00",
                    "duration_sec": 1.0,
                    "model": kwargs.get("model_override") or "claude-sonnet-4-6",
                    "attempts": 1,
                    "cost_usd": 0.01,
                    "usage": {},
                })
            return (True, "ok", "session-stub")

        self.run_claude.side_effect = _fake_run_claude

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _read_log(self) -> str:
        return self.log_file.read_text() if self.log_file.exists() else ""


class FixtureEnvelopeIsSuppressedTest(_GateTestBase):
    """Single-envelope cases for each canonical fixture shape."""

    def _assert_suppressed(self, envelope_name: str, body: dict | None = None) -> None:
        body = body if body is not None else {"task_id": "ignored-by-gate"}
        inbox = self.inboxes_root / "forge"
        env = _write_envelope(inbox, envelope_name, body)

        inbox_watcher.process_task("forge", env, models_config={})

        # No run_claude invocation.
        self.run_claude.assert_not_called()

        # Envelope was moved to .archive/ (filename may collide-rename to .N).
        archived = list((inbox / ".archive").glob(f"{Path(envelope_name).stem}*"))
        self.assertTrue(
            archived,
            f"expected {envelope_name} archived; archive={list((inbox / '.archive').iterdir())}",
        )
        self.assertFalse(
            env.exists(),
            f"original {envelope_name} should be gone after archive",
        )

        # No outbox file written (no dispatch happened).
        outbox_files = list((self.outboxes_root / "forge").iterdir())
        self.assertEqual(
            outbox_files, [],
            f"unexpected outbox files for suppressed envelope: {outbox_files}",
        )

        # No cost row appended.
        self.assertFalse(
            self.costs_file.exists() and self.costs_file.read_text().strip(),
            f"cost row appended for suppressed envelope: "
            f"{self.costs_file.read_text() if self.costs_file.exists() else ''}",
        )

        # `fixture-suppressed` log line present.
        log = self._read_log()
        self.assertIn("fixture-suppressed", log)
        self.assertIn(envelope_name, log)
        self.assertIn("cost=$0", log)

    def test_bare_fixture_task_id_suppressed(self):
        self._assert_suppressed("notify-t-pf.json")

    def test_brief_named_marker_error_notify_suppressed(self):
        # Brief acceptance criterion — wrapper-prefixed form.
        self._assert_suppressed("marker-error-notify-t-pf-1.json")

    def test_brief_named_dead_letter_notify_q_suppressed(self):
        # Brief acceptance criterion — doubled-prefix + trailing seq.
        self._assert_suppressed("notify-dead-letter-notify-q-1.18.json")

    def test_task_legacy_wrapped_form_suppressed(self):
        # The 2026-05-28 loop observation — `task-legacy` was specifically
        # called out in the brief as the open question; this asserts the
        # wrapped form is suppressed once the helper covers it.
        self._assert_suppressed("dead-letter-notify-notify-task-legacy.18.json")

    def test_malformed_json_fixture_still_suppressed(self):
        # The gate fires BEFORE json.loads — malformed fixture envelopes
        # are still suppressed cleanly instead of escalating to .invalid/.
        inbox = self.inboxes_root / "forge"
        env = inbox / "notify-t-malformed.json"
        env.write_text("{not valid json")

        inbox_watcher.process_task("forge", env, models_config={})

        self.run_claude.assert_not_called()
        archived = list((inbox / ".archive").glob("notify-t-malformed*"))
        self.assertTrue(archived, "malformed fixture envelope should archive, not .invalid")
        invalid = list((inbox / ".invalid").iterdir())
        self.assertEqual(invalid, [], f"malformed fixture leaked to .invalid: {invalid}")


class RealEnvelopeStillDispatchesTest(_GateTestBase):
    """A real envelope must NOT be suppressed by the gate."""

    def test_real_envelope_proceeds_to_run_claude(self):
        inbox = self.inboxes_root / "forge"
        env = _write_envelope(inbox, "build-real-thing-v1.json", _REAL_TASK)

        inbox_watcher.process_task("forge", env, models_config={})

        # Real dispatch happened: run_claude called exactly once.
        self.run_claude.assert_called_once()
        # Envelope archived after the dispatch path (normal flow).
        archived = list((inbox / ".archive").glob("build-real-thing-v1*"))
        self.assertTrue(archived, "real envelope should archive after dispatch")
        # Outbox written.
        outbox_files = list((self.outboxes_root / "forge").iterdir())
        self.assertEqual(len(outbox_files), 1)


class MixedBatchTest(_GateTestBase):
    """3 fixture + 2 real — exactly the 2 real reach run_claude."""

    def test_mixed_batch_partitions_correctly(self):
        inbox = self.inboxes_root / "forge"
        fixture_names = [
            "notify-t-pf.json",
            "marker-error-notify-t-pf-1.json",
            "notify-dead-letter-notify-q-1.18.json",
        ]
        real_envelopes = [
            ("build-real-thing-v1.json", _REAL_TASK),
            ("build-real-thing-v2.json", {**_REAL_TASK, "task_id": "build-real-thing-v2"}),
        ]
        for name in fixture_names:
            _write_envelope(inbox, name, {"task_id": "ignored"})
        for name, body in real_envelopes:
            _write_envelope(inbox, name, body)

        # Process all 5. Order matches inbox_watcher.scan_inbox (mtime),
        # but we drive process_task directly per envelope to keep the
        # surface tight (no thread loop, no lease).
        for p in sorted(inbox.glob("*.json")):
            inbox_watcher.process_task("forge", p, models_config={})

        # Exactly 2 dispatches.
        self.assertEqual(
            self.run_claude.call_count, 2,
            f"expected 2 real dispatches; got {self.run_claude.call_count}",
        )
        # All 5 envelopes archived.
        archived = list((inbox / ".archive").iterdir())
        self.assertEqual(
            len(archived), 5,
            f"expected 5 archived envelopes; got {[p.name for p in archived]}",
        )
        # Original inbox is empty (only `.archive` + `.invalid` subdirs remain).
        leftovers = [p for p in inbox.iterdir() if p.is_file()]
        self.assertEqual(leftovers, [], f"inbox should be empty; got {leftovers}")

        # Log carries 3 fixture-suppressed lines.
        log = self._read_log()
        self.assertEqual(
            log.count("fixture-suppressed"), 3,
            f"expected 3 fixture-suppressed log lines; log={log}",
        )


if __name__ == "__main__":
    unittest.main()
