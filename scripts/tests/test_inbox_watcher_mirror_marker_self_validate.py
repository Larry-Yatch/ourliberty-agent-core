#!/usr/bin/env python3
"""mirror-marker-self-validate-gate-001.

A bounded SAME-PROCESS verdict-marker self-validation gate lives in
inbox_watcher.process_task, in FRONT of the (slow, cross-process)
outbox_notifier marker-error net. When Mirror's first phase=review output
within ~10-25 min of a mirror-bot restart ends with a malformed/missing
verdict marker, the worker re-invokes run_claude under --resume with a terse
correction BEFORE the outbox is written — so the common first-after-restart
case resolves with zero cross-process marker-error round-trips.

Coverage (per the spec's success criteria):
  (i)   malformed-then-clean re-emit resolves in-process;
  (ii)  exhaust falls through to the existing outbox write unchanged (the
        notifier net is the outer backstop);
  (iii) the gate does not fire for non-mirror / non-review tasks or when
        run_claude returned non-success.
Plus the config loader and the parse-based detector.
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
import mirror_review_handler as mrh  # noqa: E402


def _clean_marker(task_id: str = "t1") -> str:
    """A canonical REVIEW_PASS block with narrative above it."""
    blk = mrh.render_marker(
        "review_pass", pr_url="https://x/pull/1", summary="ok", task_id=task_id,
    )
    return f"Looks good.\n\n{blk}"


# A `=== REVIEW_PASS ===` delimiter with a prose body (no JSON) — the exact
# post-restart shape parse_mirror_marker raises MalformedMirrorMarker on.
_DELIM_NO_JSON = (
    "=== REVIEW_PASS ===\nLooks good to me, shipping it.\n=== END_REVIEW_PASS ==="
)
# A prose verdict with no delimiter at all — parser returns marker_type None.
_PROSE_NO_DELIM = "I reviewed the PR and it passes. Nice work."


class MarkerDetectorTest(unittest.TestCase):
    def test_clean_marker_is_clean(self):
        ok, err = iw._mirror_marker_is_clean(_clean_marker())
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_missing_marker_is_not_clean(self):
        ok, err = iw._mirror_marker_is_clean(_PROSE_NO_DELIM)
        self.assertFalse(ok)
        self.assertIn("verdict marker", err)

    def test_delim_without_json_is_not_clean(self):
        ok, err = iw._mirror_marker_is_clean(_DELIM_NO_JSON)
        self.assertFalse(ok)
        self.assertTrue(err)  # carries the parser's own diagnostic

    def test_multiple_markers_is_not_clean(self):
        ok, err = iw._mirror_marker_is_clean(_clean_marker() + "\n" + _clean_marker())
        self.assertFalse(ok)
        self.assertIn("marker", err.lower())


class RetryCapLoaderTest(unittest.TestCase):
    def test_default_when_missing(self):
        self.assertEqual(
            iw._load_marker_self_validate_retries({}),
            iw.DEFAULT_MIRROR_MARKER_SELF_VALIDATE_RETRIES,
        )

    def test_default_when_loop_bounds_absent(self):
        self.assertEqual(
            iw._load_marker_self_validate_retries({"agents": {}}),
            iw.DEFAULT_MIRROR_MARKER_SELF_VALIDATE_RETRIES,
        )

    def test_reads_configured_value(self):
        cfg = {"loop_bounds": {"mirror_marker_self_validate_retries": 5}}
        self.assertEqual(iw._load_marker_self_validate_retries(cfg), 5)

    def test_zero_is_honored(self):
        cfg = {"loop_bounds": {"mirror_marker_self_validate_retries": 0}}
        self.assertEqual(iw._load_marker_self_validate_retries(cfg), 0)

    def test_invalid_values_fall_back(self):
        for bad in (True, -1, "two", 1.5, None):
            cfg = {"loop_bounds": {"mirror_marker_self_validate_retries": bad}}
            self.assertEqual(
                iw._load_marker_self_validate_retries(cfg),
                iw.DEFAULT_MIRROR_MARKER_SELF_VALIDATE_RETRIES,
                msg=f"value {bad!r} should fall back to default",
            )


class SelfValidateHelperTest(unittest.TestCase):
    """Unit-level coverage of the bounded re-prompt loop itself."""

    def _call(self, *, agent, phase, output_text, models_config, run_claude):
        task = {"task_id": "tid", "phase": phase}
        with mock.patch.object(iw.agent_runner, "run_claude", side_effect=run_claude) as rc:
            out, sess = iw._mirror_marker_self_validate(
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
            agent="mirror", phase="review",
            output_text=_clean_marker(),
            models_config={"loop_bounds": {"mirror_marker_self_validate_retries": 2}},
            run_claude=lambda **k: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        rc.assert_not_called()
        self.assertEqual(sess, "sess-0")

    def test_malformed_then_clean_resolves_in_process(self):
        # (i) Malformed first → one correction re-prompt returns a clean marker.
        clean = _clean_marker()

        def run_claude(**k):
            # Resume continuity: the correction resumes the returned session id.
            self.assertEqual(k["session_id"], "sess-0")
            self.assertEqual(k["agent_id"], "mirror")
            self.assertEqual(k["phase"], "review")
            return (True, clean, "sess-1")

        out, sess, rc = self._call(
            agent="mirror", phase="review",
            output_text=_DELIM_NO_JSON,
            models_config={"loop_bounds": {"mirror_marker_self_validate_retries": 2}},
            run_claude=run_claude,
        )
        self.assertEqual(rc.call_count, 1)
        self.assertEqual(out, clean)
        self.assertEqual(sess, "sess-1")  # latest session carried forward

    def test_exhaust_returns_best_effort_and_caps_calls(self):
        # (ii) Always malformed → loop caps at the configured retries, then
        # returns best-effort output (still malformed) for the single outbox
        # write; the outbox_notifier marker-error net is the outer backstop.
        calls = {"n": 0}

        def run_claude(**k):
            calls["n"] += 1
            return (True, _DELIM_NO_JSON, f"sess-{calls['n']}")

        out, sess, rc = self._call(
            agent="mirror", phase="review",
            output_text=_DELIM_NO_JSON,
            models_config={"loop_bounds": {"mirror_marker_self_validate_retries": 2}},
            run_claude=run_claude,
        )
        self.assertEqual(rc.call_count, 2)  # capped, never unbounded
        ok, _ = iw._mirror_marker_is_clean(out)
        self.assertFalse(ok)  # best-effort output is still malformed
        self.assertEqual(sess, "sess-2")  # latest session carried forward

    def test_reprompt_non_success_breaks_keeping_prior_output(self):
        # A run_claude non-success mid-loop must not overwrite output with an
        # error string — it breaks and the prior (malformed) output flows on.
        def run_claude(**k):
            return (False, "claude error text", "sess-err")

        out, sess, rc = self._call(
            agent="mirror", phase="review",
            output_text=_DELIM_NO_JSON,
            models_config={"loop_bounds": {"mirror_marker_self_validate_retries": 3}},
            run_claude=run_claude,
        )
        self.assertEqual(rc.call_count, 1)  # broke after first non-success
        self.assertEqual(out, _DELIM_NO_JSON)  # prior output kept, not the error
        self.assertEqual(sess, "sess-err")  # new session still carried forward

    def test_zero_cap_disables_gate(self):
        out, sess, rc = self._call(
            agent="mirror", phase="review",
            output_text=_DELIM_NO_JSON,
            models_config={"loop_bounds": {"mirror_marker_self_validate_retries": 0}},
            run_claude=lambda **k: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        rc.assert_not_called()
        self.assertEqual(out, _DELIM_NO_JSON)  # untouched best-effort

    def test_does_not_fire_for_non_mirror_agent(self):
        # (iii) Forge/Beacon outputs are never marker-validated here even with a
        # malformed body and review phase.
        out, sess, rc = self._call(
            agent="forge", phase="review",
            output_text=_DELIM_NO_JSON,
            models_config={"loop_bounds": {"mirror_marker_self_validate_retries": 2}},
            run_claude=lambda **k: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        rc.assert_not_called()
        self.assertEqual(out, _DELIM_NO_JSON)

    def test_does_not_fire_for_non_review_mirror_task(self):
        # (iii) A mirror task in a non-review phase (e.g. chat) is untouched.
        out, sess, rc = self._call(
            agent="mirror", phase="chat",
            output_text=_DELIM_NO_JSON,
            models_config={"loop_bounds": {"mirror_marker_self_validate_retries": 2}},
            run_claude=lambda **k: (_ for _ in ()).throw(AssertionError("should not run")),
        )
        rc.assert_not_called()
        self.assertEqual(out, _DELIM_NO_JSON)


class ProcessTaskIntegrationTest(unittest.TestCase):
    """End-to-end through process_task: prove the gate is wired and substitutes
    output_text/session_id before the single existing outbox write."""

    AGENT = "mirror"

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
            "task_id": "mirror-review-selfvalidate-001",
            "prompt": "review the PR",
            "source": "beacon",
            "phase": "review",
            "pr_url": "https://github.com/x/y/pull/9",
        }
        p = iw.INBOXES_ROOT / self.AGENT / "mirror-review-selfvalidate-001.json"
        p.write_text(json.dumps(task))
        return p

    def _models_config(self, retries):
        # No worktree_enabled → process_task skips worktree creation and runs
        # against AGENTS_DIR/mirror, keeping the integration test hermetic.
        return {
            "agents": {self.AGENT: {"inbox_model": "claude-opus-4-8"}},
            "loop_bounds": {"mirror_marker_self_validate_retries": retries},
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
        ), mock.patch.object(
            iw, "_mirror_review_pr_terminal_state", return_value=None,
        ):
            dv.validate_task.return_value = (True, "")
            rvm.check_hard_topology.return_value = (True, "")
            rvm.check_target_repo.return_value = (True, "")
            iw.process_task(self.AGENT, task_file, self._models_config(retries))
        outboxes = list((iw.OUTBOXES_ROOT / self.AGENT).glob("*.json"))
        self.assertEqual(len(outboxes), 1)  # idempotent: exactly one outbox
        return rc, json.loads(outboxes[0].read_text())

    def test_malformed_then_clean_writes_corrected_outbox(self):
        clean = _clean_marker("mirror-review-selfvalidate-001")
        outputs = [
            (True, _DELIM_NO_JSON, "sess-A"),  # initial run: malformed
            (True, clean, "sess-B"),           # in-process correction: clean
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
            return (True, _DELIM_NO_JSON, "sess-X")

        rc, outbox = self._run(run_claude, retries=2)
        self.assertEqual(rc.call_count, 3)  # initial + 2 capped corrections
        self.assertEqual(outbox["exit_code"], 0)  # run succeeded; only the marker is bad
        ok, _ = iw._mirror_marker_is_clean(outbox["result"])
        self.assertFalse(ok)  # best-effort malformed output flows to the notifier net


if __name__ == "__main__":
    unittest.main()
