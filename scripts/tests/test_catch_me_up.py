#!/usr/bin/env python3
"""Tests for catch_me_up.py — Beacon's catch-me-up Telegram shortcut.

Covers:
  - Shortcut recognition vocabulary + normalization edge cases.
  - Checkpoint persistence (round-trip + atomic write).
  - Section formatting bounds (row cap + empty + overflow).
  - Alert tier filtering (NOW/SOON surface; FYI suppressed).
  - End-to-end synthesize() advances the checkpoint.

Production gh subprocess is patched out — tests never reach the network.

Run:
    python3 -m unittest scripts.tests.test_catch_me_up
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import catch_me_up  # noqa: E402
import larry_alerts  # noqa: E402


class RecognitionTest(unittest.TestCase):
    def test_canonical_phrases_match(self):
        for phrase in [
            "catch me up", "status", "what's happening",
            "whats happening", "where are we", "update me", "summary",
        ]:
            self.assertTrue(
                catch_me_up.is_catch_me_up_shortcut(phrase),
                f"phrase {phrase!r} should match",
            )

    def test_case_insensitive(self):
        self.assertTrue(catch_me_up.is_catch_me_up_shortcut("Status"))
        self.assertTrue(catch_me_up.is_catch_me_up_shortcut("CATCH ME UP"))

    def test_trailing_punctuation_stripped(self):
        self.assertTrue(catch_me_up.is_catch_me_up_shortcut("status?"))
        self.assertTrue(catch_me_up.is_catch_me_up_shortcut("catch me up!"))
        self.assertTrue(catch_me_up.is_catch_me_up_shortcut("where are we..."))

    def test_leading_slash_stripped(self):
        self.assertTrue(catch_me_up.is_catch_me_up_shortcut("/status"))

    def test_non_matching_messages_pass_through(self):
        for phrase in [
            "approve", "modify: needs more context", "tell me a joke",
            "catch", "status report please", "",
        ]:
            self.assertFalse(
                catch_me_up.is_catch_me_up_shortcut(phrase),
                f"phrase {phrase!r} should NOT match",
            )


class _IsolatedStateTest(unittest.TestCase):
    """Base — points module-level paths at a tempdir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._tmp_path = tmp
        self._patches = [
            mock.patch.object(catch_me_up, "AGENTS_ROOT", tmp),
            mock.patch.object(catch_me_up, "CHECKPOINT_FILE",
                              tmp / "state" / "beacon-catch-me-up-checkpoint.json"),
            mock.patch.object(catch_me_up, "APPROVALS_FILE",
                              tmp / "state" / "beacon-pending-approvals.json"),
            mock.patch.object(catch_me_up, "ALERTS_FILE",
                              tmp / "blackboard" / "larry-alerts.jsonl"),
            mock.patch.object(catch_me_up, "SEQUENCES_DIR",
                              tmp / "blackboard" / "build-sequences"),
        ]
        for p in self._patches:
            p.start()
        # The synthesizer also calls into larry_alerts.translate_alert
        # which reads from the real config; that's fine — it's a pure
        # read against the in-repo config/alert-translations.json.

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class CheckpointTest(_IsolatedStateTest):
    def test_load_returns_none_when_missing(self):
        self.assertIsNone(catch_me_up.load_checkpoint(7998341473))

    def test_round_trip(self):
        ts = "2026-05-28T12:00:00+00:00"
        catch_me_up.save_checkpoint(7998341473, ts)
        self.assertEqual(catch_me_up.load_checkpoint(7998341473), ts)

    def test_per_chat_isolation(self):
        catch_me_up.save_checkpoint(111, "2026-05-28T12:00:00+00:00")
        catch_me_up.save_checkpoint(222, "2026-05-28T13:00:00+00:00")
        self.assertEqual(
            catch_me_up.load_checkpoint(111), "2026-05-28T12:00:00+00:00")
        self.assertEqual(
            catch_me_up.load_checkpoint(222), "2026-05-28T13:00:00+00:00")

    def test_malformed_file_recovers(self):
        catch_me_up.CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        catch_me_up.CHECKPOINT_FILE.write_text("not json")
        self.assertIsNone(catch_me_up.load_checkpoint(111))
        # Save still works — overwrites malformed file.
        catch_me_up.save_checkpoint(111, "2026-05-28T12:00:00+00:00")
        self.assertEqual(
            catch_me_up.load_checkpoint(111), "2026-05-28T12:00:00+00:00")


class SectionFormatTest(unittest.TestCase):
    def test_merged_empty(self):
        self.assertEqual(catch_me_up._render_merged_section([]),
                         ["📦 Merged: none"])

    def test_merged_under_cap(self):
        prs = [{"number": 1, "title": "fix"}, {"number": 2, "title": "feat"}]
        lines = catch_me_up._render_merged_section(prs)
        self.assertIn("#1 fix", lines[1])
        self.assertIn("#2 feat", lines[2])
        self.assertNotIn("more", "\n".join(lines))

    def test_merged_overflow(self):
        prs = [{"number": i, "title": f"t{i}"} for i in range(7)]
        lines = catch_me_up._render_merged_section(prs)
        # Header + cap + overflow line.
        self.assertEqual(len(lines), 1 + catch_me_up.SECTION_ROW_CAP + 1)
        self.assertIn("(+4 more)", lines[-1])

    def test_alerts_tier_filter(self):
        alerts = [
            {"_tier": "NOW", "subject": "x:y", "source": "s"},
            {"_tier": "SOON", "subject": "a:b", "source": "s"},
        ]
        lines = catch_me_up._render_alerts_section(alerts)
        joined = "\n".join(lines)
        self.assertIn("NOW", joined)
        self.assertIn("SOON", joined)

    def test_truncation_caps_long_titles(self):
        long_title = "x" * 200
        out = catch_me_up._truncate(long_title, max_len=60)
        self.assertEqual(len(out), 60)
        self.assertTrue(out.endswith("…"))


class FetchAlertsTest(_IsolatedStateTest):
    def _write_alerts(self, records: list[dict]):
        catch_me_up.ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(catch_me_up.ALERTS_FILE, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_skips_notifications(self):
        since = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self._write_alerts([
            {"ts": "2026-05-28T12:00:00+00:00",
             "kind": "notification", "source": "outbox", "intent": "review-pass",
             "message": "hi"},
        ])
        self.assertEqual(catch_me_up.fetch_live_alerts_since(since), [])

    def test_filters_by_timestamp(self):
        since = datetime(2026, 5, 20, tzinfo=timezone.utc)
        self._write_alerts([
            {"ts": "2026-05-15T00:00:00+00:00", "source": "s",
             "severity": "warning", "message": "old", "subject": "x"},
            {"ts": "2026-05-25T00:00:00+00:00", "source": "s",
             "severity": "warning", "message": "new", "subject": "x"},
        ])
        # Force unmatched-subject default (SOON) so both alerts that pass
        # the time filter actually surface.
        with mock.patch.object(larry_alerts, "translate_alert", return_value=None):
            out = catch_me_up.fetch_live_alerts_since(since)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["message"], "new")

    def test_fyi_alerts_filtered_out(self):
        since = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self._write_alerts([
            {"ts": "2026-05-28T00:00:00+00:00", "source": "s",
             "severity": "warning", "message": "fyi", "subject": "x"},
        ])
        with mock.patch.object(larry_alerts, "translate_alert",
                               return_value={"tier": "FYI"}):
            out = catch_me_up.fetch_live_alerts_since(since)
        self.assertEqual(out, [])

    def test_unmatched_subject_defaults_to_soon(self):
        since = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self._write_alerts([
            {"ts": "2026-05-28T00:00:00+00:00", "source": "unknown-source",
             "severity": "warning", "message": "untranslated", "subject": "?"},
        ])
        out = catch_me_up.fetch_live_alerts_since(since)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["_tier"], "SOON")


class FetchSequencesTest(_IsolatedStateTest):
    def _write_seq(self, name: str, payload: dict):
        catch_me_up.SEQUENCES_DIR.mkdir(parents=True, exist_ok=True)
        with open(catch_me_up.SEQUENCES_DIR / name, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_returns_active_and_paused(self):
        self._write_seq("a.json", {"seq_id": "a", "status": "active",
                                    "current_steps": ["s1"]})
        self._write_seq("b.json", {"seq_id": "b", "status": "paused",
                                    "current_steps": []})
        self._write_seq("c.json", {"seq_id": "c", "status": "complete",
                                    "current_steps": []})
        out = catch_me_up.fetch_active_sequences()
        ids = {s["seq_id"] for s in out}
        self.assertEqual(ids, {"a", "b"})


class FetchApprovalsTest(_IsolatedStateTest):
    def test_returns_pending_list(self):
        catch_me_up.APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(catch_me_up.APPROVALS_FILE, "w", encoding="utf-8") as f:
            json.dump({"version": 1,
                       "pending": [{"id": "x-001",
                                    "plan_summary": "do the thing"}],
                       "history": []}, f)
        out = catch_me_up.fetch_pending_approvals()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "x-001")

    def test_missing_file_returns_empty(self):
        self.assertEqual(catch_me_up.fetch_pending_approvals(), [])


class SinceHeaderTest(unittest.TestCase):
    def test_first_time_falls_back_to_24h(self):
        now = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
        self.assertIn("24h", catch_me_up._format_since(None, now))

    def test_minutes_format(self):
        now = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
        cp = now - timedelta(minutes=15)
        self.assertEqual(catch_me_up._format_since(cp, now), "since 15m ago")

    def test_hours_format(self):
        now = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
        cp = now - timedelta(hours=5)
        self.assertEqual(catch_me_up._format_since(cp, now), "since 5h ago")

    def test_days_format(self):
        now = datetime(2026, 5, 28, 12, tzinfo=timezone.utc)
        cp = now - timedelta(hours=72)
        self.assertEqual(catch_me_up._format_since(cp, now), "since 3d ago")


class SynthesizeTest(_IsolatedStateTest):
    def test_advances_checkpoint(self):
        with mock.patch.object(catch_me_up, "_run_gh", return_value=[]):
            self.assertIsNone(catch_me_up.load_checkpoint(123))
            catch_me_up.synthesize(123)
            after = catch_me_up.load_checkpoint(123)
            self.assertIsNotNone(after)
            # Parseable as ISO.
            self.assertIsNotNone(catch_me_up._parse_iso(after))

    def test_message_contains_all_section_headers(self):
        with mock.patch.object(catch_me_up, "_run_gh", return_value=[]):
            msg = catch_me_up.synthesize(123)
        self.assertIn("Catch-me-up", msg)
        self.assertIn("Merged", msg)
        self.assertIn("Open PRs", msg)
        self.assertIn("Pending approvals", msg)
        self.assertIn("Live alerts", msg)
        self.assertIn("Sequences", msg)

    def test_gh_failure_renders_none_sections(self):
        """Network-unreachable gh → empty result → 'none' rows, not crash."""
        with mock.patch.object(catch_me_up, "_run_gh", return_value=None):
            msg = catch_me_up.synthesize(123)
        self.assertIn("Merged: none", msg)
        self.assertIn("Open PRs: none", msg)


if __name__ == "__main__":
    unittest.main()
