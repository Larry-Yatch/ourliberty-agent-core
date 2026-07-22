#!/usr/bin/env python3
"""forge-preflight-marker-self-validate-gate-001.

A bounded SAME-PROCESS preflight-marker self-validation gate lives in
inbox_watcher.process_task, in FRONT of the (slow, cross-process)
outbox_notifier MalformedForgeMarker cascade. When a Forge phase=preflight
output ends in prose with no PROCEED/CLARIFY_REQUEST/REJECT block (the dense-
spec case — RSDPM sequence preflights especially), the worker re-invokes
run_claude under --resume with a terse correction BEFORE the outbox is written
— so the common case resolves with zero cross-process marker-error round-trips.

Coverage (per the spec's success criteria):
  (i)   prose-only-then-clean re-emit resolves in-process;
  (ii)  exhaust falls through to the existing outbox write unchanged (the
        outbox_notifier cascade is the outer backstop);
  (iii) the gate does not fire for non-forge / non-preflight tasks or when
        run_claude returned non-success.
Plus the config loader and the parse-based detector.

Exact structural mirror of test_inbox_watcher_mirror_marker_self_validate.py.
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
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_watcher as iw  # noqa: E402
import forge_preflight_handler as fph  # noqa: E402


def _clean_marker(task_id: str = "t1") -> str:
    """A canonical PROCEED block with narrative above it."""
    blk = fph.render_marker(
        "proceed", task_id=task_id, preflight_summary="ok",
    )
    return f"Looks buildable.\n\n{blk}"


# A bold prose verdict with no delimiter at all — the exact dense-spec shape
# parse_forge_marker returns marker_type None for (no marker block).
_PROSE_NO_BLOCK = (
    "Preflight decision: **PROCEED**.\n\n"
    "The spec is coherent and every touch point exists. The build phase arrives "
    "as a separate dispatch automatically."
)
# A `=== PROCEED ===` delimiter with a prose body (no JSON) — parse_forge_marker
# raises MalformedForgeMarker.
_DELIM_NO_JSON = (
    "=== PROCEED ===\nLooks buildable, shipping it.\n=== END_PROCEED ==="
)


class MarkerDetectorTest(unittest.TestCase):
    def test_clean_marker_is_clean(self):
        ok, err = iw._forge_preflight_marker_is_clean(_clean_marker())
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_missing_marker_is_not_clean(self):
        ok, err = iw._forge_preflight_marker_is_clean(_PROSE_NO_BLOCK)
        self.assertFalse(ok)
        self.assertIn("preflight marker", err)

    def test_delim_without_json_is_not_clean(self):
        ok, err = iw._forge_preflight_marker_is_clean(_DELIM_NO_JSON)
        self.assertFalse(ok)
        self.assertTrue(err)  # carries the parser's own diagnostic

    def test_multiple_markers_is_not_clean(self):
        ok, err = iw._forge_preflight_marker_is_clean(
            _clean_marker() + "\n" + _clean_marker()
        )
        self.assertFalse(ok)
        self.assertIn("marker", err.lower())


class RetryCapLoaderTest(unittest.TestCase):
    def test_default_when_missing(self):
        self.assertEqual(
            iw._load_forge_preflight_marker_self_validate_retries({}),
            iw.DEFAULT_FORGE_PREFLIGHT_MARKER_SELF_VALIDATE_RETRIES,
        )

    def test_default_when_loop_bounds_absent(self):
        self.assertEqual(
            iw._load_forge_preflight_marker_self_validate_retries({"agents": {}}),
            iw.DEFAULT_FORGE_PREFLIGHT_MARKER_SELF_VALIDATE_RETRIES,
        )

    def test_reads_configured_value(self):
        cfg = {"loop_bounds": {"forge_preflight_marker_self_validate_retries": 5}}
        self.assertEqual(
            iw._load_forge_preflight_marker_self_validate_retries(cfg), 5
        )

    def test_zero_is_honored(self):
        cfg = {"loop_bounds": {"forge_preflight_marker_self_validate_retries": 0}}
        self.assertEqual(
            iw._load_forge_preflight_marker_self_validate_retries(cfg), 0
        )

    def test_invalid_values_fall_back(self):
        for bad in (True, -1, "two", 1.5, None):
            cfg = {"loop_bounds": {"forge_preflight_marker_self_validate_retries": bad}}
            self.assertEqual(
                iw._load_forge_preflight_marker_self_validate_retries(cfg),
                iw.DEFAULT_FORGE_PREFLIGHT_MARKER_SELF_VALIDATE_RETRIES,
                msg=f"value {bad!r} should fall back to default",
            )


class SelfValidateHelperTest(unittest.TestCase):
    """Unit-level coverage of the bounded re-prompt loop itself."""

    def _call(self, *, agent, phase, output_text, models_config, run_claude):
        task = {"task_id": "tid", "phase": phase}
        with mock.patch.object(iw.agent_runner, "run_claude", side_effect=run_claude) as rc:
            out, sess = iw._forge_preflight_marker_self_validate(
                agent=agent,
                task=task,
                output_text=output_text,
                session_id="sess-0",
                working_dir="/tmp/wd",
                model="claude-opus-4-8",
                timeout=60,
                task_id="tid",
                meta={},
                models_config=models_config,
            )
        return out, sess, rc

    def test_clean_first_no_reprompt(self):
        # (iii-ish) A clean marker on the first try never re-invokes run_claude.
        out, sess, rc = self._call(
            agent="forge", phase="preflight",
            output_text=_clean_marker(),
            models_config={"loop_bounds": {"forge_preflight_marker_self_validate_retries": 2}},
            run_claude=lambda **k: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        rc.assert_not_called()
        self.assertEqual(sess, "sess-0")

    def test_prose_only_then_clean_resolves_in_process(self):
        # (i) Prose-no-block first → one correction re-prompt returns a clean
        # marker.
        clean = _clean_marker()

        def run_claude(**k):
            # Resume continuity: the correction resumes the returned session id.
            self.assertEqual(k["session_id"], "sess-0")
            self.assertEqual(k["agent_id"], "forge")
            self.assertEqual(k["phase"], "preflight")
            return (True, clean, "sess-1")

        out, sess, rc = self._call(
            agent="forge", phase="preflight",
            output_text=_PROSE_NO_BLOCK,
            models_config={"loop_bounds": {"forge_preflight_marker_self_validate_retries": 2}},
            run_claude=run_claude,
        )
        self.assertEqual(rc.call_count, 1)
        self.assertEqual(out, clean)
        self.assertEqual(sess, "sess-1")  # latest session carried forward

    def test_exhaust_returns_best_effort_and_caps_calls(self):
        # (ii) Always malformed → loop caps at the configured retries, then
        # returns best-effort output (still malformed) for the single outbox
        # write; the outbox_notifier cascade is the outer backstop.
        calls = {"n": 0}

        def run_claude(**k):
            calls["n"] += 1
            return (True, _PROSE_NO_BLOCK, f"sess-{calls['n']}")

        out, sess, rc = self._call(
            agent="forge", phase="preflight",
            output_text=_PROSE_NO_BLOCK,
            models_config={"loop_bounds": {"forge_preflight_marker_self_validate_retries": 2}},
            run_claude=run_claude,
        )
        self.assertEqual(rc.call_count, 2)  # capped, never unbounded
        ok, _ = iw._forge_preflight_marker_is_clean(out)
        self.assertFalse(ok)  # best-effort output is still malformed
        self.assertEqual(sess, "sess-2")  # latest session carried forward

    def test_reprompt_non_success_breaks_keeping_prior_output(self):
        # A run_claude non-success mid-loop must not overwrite output with an
        # error string — it breaks and the prior (malformed) output flows on.
        def run_claude(**k):
            return (False, "claude error text", "sess-err")

        out, sess, rc = self._call(
            agent="forge", phase="preflight",
            output_text=_PROSE_NO_BLOCK,
            models_config={"loop_bounds": {"forge_preflight_marker_self_validate_retries": 3}},
            run_claude=run_claude,
        )
        self.assertEqual(rc.call_count, 1)  # broke after first non-success
        self.assertEqual(out, _PROSE_NO_BLOCK)  # prior output kept, not the error
        self.assertEqual(sess, "sess-err")  # new session still carried forward

    def test_zero_cap_disables_gate(self):
        out, sess, rc = self._call(
            agent="forge", phase="preflight",
            output_text=_PROSE_NO_BLOCK,
            models_config={"loop_bounds": {"forge_preflight_marker_self_validate_retries": 0}},
            run_claude=lambda **k: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        rc.assert_not_called()
        self.assertEqual(out, _PROSE_NO_BLOCK)  # untouched best-effort

    def test_does_not_fire_for_non_forge_agent(self):
        # (iii) Mirror/Beacon outputs are never preflight-marker-validated here
        # even with a malformed body and preflight phase.
        out, sess, rc = self._call(
            agent="mirror", phase="preflight",
            output_text=_PROSE_NO_BLOCK,
            models_config={"loop_bounds": {"forge_preflight_marker_self_validate_retries": 2}},
            run_claude=lambda **k: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        rc.assert_not_called()
        self.assertEqual(out, _PROSE_NO_BLOCK)

    def test_does_not_fire_for_non_preflight_forge_task(self):
        # (iii) A forge task in a non-preflight phase (e.g. build) is untouched.
        out, sess, rc = self._call(
            agent="forge", phase="build",
            output_text=_PROSE_NO_BLOCK,
            models_config={"loop_bounds": {"forge_preflight_marker_self_validate_retries": 2}},
            run_claude=lambda **k: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        rc.assert_not_called()
        self.assertEqual(out, _PROSE_NO_BLOCK)


class ProcessTaskIntegrationTest(unittest.TestCase):
    """End-to-end through process_task: prove the gate is wired and substitutes
    output_text/session_id before the single existing outbox write."""

    AGENT = "forge"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig = {
            "INBOXES_ROOT": iw.INBOXES_ROOT,
            "OUTBOXES_ROOT": iw.OUTBOXES_ROOT,
            "BLACKBOARD": iw.BLACKBOARD,
            "COSTS_FILE": iw.COSTS_FILE,
            "LOG_FILE": iw.LOG_FILE,
        }
        iw.INBOXES_ROOT = self.root / "inboxes"
        iw.OUTBOXES_ROOT = self.root / "outboxes"
        iw.BLACKBOARD = self.root / "blackboard"
        iw.COSTS_FILE = iw.BLACKBOARD / "costs.jsonl"
        iw.LOG_FILE = self.root / "logs" / "inbox_watcher.log"
        (iw.INBOXES_ROOT / self.AGENT).mkdir(parents=True, exist_ok=True)
        (iw.INBOXES_ROOT / self.AGENT / ".archive").mkdir(parents=True, exist_ok=True)
        (iw.OUTBOXES_ROOT / self.AGENT).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(iw, k, v)
        self.tmp.cleanup()

    def _write_task(self):
        task = {
            "task_id": "forge-preflight-selfvalidate-001",
            "prompt": "preflight the spec",
            "source": "beacon",
            "phase": "preflight",
        }
        p = iw.INBOXES_ROOT / self.AGENT / "forge-preflight-selfvalidate-001.json"
        p.write_text(json.dumps(task))
        return p

    def _models_config(self, retries):
        # No worktree_enabled → process_task skips worktree creation and runs
        # against AGENTS_DIR/forge, keeping the integration test hermetic.
        return {
            "agents": {self.AGENT: {"inbox_model": "claude-opus-4-8"}},
            "loop_bounds": {"forge_preflight_marker_self_validate_retries": retries},
        }

    def _run(self, run_claude_side_effect, retries):
        task_file = self._write_task()
        with mock.patch.object(
            iw.agent_runner, "run_claude", side_effect=run_claude_side_effect,
        ) as rc, mock.patch.object(
            iw, "dispatch_validator",
        ) as dv, mock.patch.object(
            iw, "routing_validator",
        ) as rvm, mock.patch.object(
            iw, "_rotation_gate_block_reason", return_value=None,
        ):
            dv.validate_task.return_value = (True, "")
            rvm.check_hard_topology.return_value = (True, "")
            rvm.check_target_repo.return_value = (True, "")
            iw.process_task(self.AGENT, task_file, self._models_config(retries))
        outboxes = list((iw.OUTBOXES_ROOT / self.AGENT).glob("*.json"))
        self.assertEqual(len(outboxes), 1)  # idempotent: exactly one outbox
        return rc, json.loads(outboxes[0].read_text())

    def test_prose_only_then_clean_writes_corrected_outbox(self):
        clean = _clean_marker("forge-preflight-selfvalidate-001")
        outputs = [
            (True, _PROSE_NO_BLOCK, "sess-A"),  # initial run: prose, no block
            (True, clean, "sess-B"),            # in-process correction: clean
        ]

        def run_claude(**k):
            return outputs.pop(0)

        rc, outbox = self._run(run_claude, retries=2)
        self.assertEqual(rc.call_count, 2)  # initial + 1 correction
        self.assertEqual(outbox["exit_code"], 0)  # run succeeded
        self.assertEqual(outbox["result"], clean)  # corrected output written
        self.assertEqual(outbox["claude_session_id"], "sess-B")  # latest session

    def test_exhaust_writes_best_effort_outbox_unchanged_path(self):
        def run_claude(**k):
            return (True, _PROSE_NO_BLOCK, "sess-X")

        rc, outbox = self._run(run_claude, retries=2)
        self.assertEqual(rc.call_count, 3)  # initial + 2 capped corrections
        self.assertEqual(outbox["exit_code"], 0)  # run succeeded; only the marker is bad
        ok, _ = iw._forge_preflight_marker_is_clean(outbox["result"])
        self.assertFalse(ok)  # best-effort output flows to the outbox_notifier net


if __name__ == "__main__":
    unittest.main()
