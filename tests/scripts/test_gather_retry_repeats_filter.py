"""Unit tests for the notify-* exclusion in pulse_check_i.gather_retry_repeats.

Mirror of PR #33's bugfix one level down: gather_retry_repeats counts outbox
archive filename rotations (.1.json, .2.json), which are the workflow shape
for notify-* tasks, not retries. Excluding notify-* leaves only real retries
(marker-error cascades, cycle-fix re-dispatches, Forge revision rounds) in
the high-repeat list Check I surfaces.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from pulse_check_i import gather_retry_repeats  # noqa: E402


def write_archive_files(outbox_root: Path, agent: str, base_task_id: str, n: int) -> None:
    """Create n files in <outbox_root>/<agent>/.archive/ simulating safe_write_inbox
    rotation: base.json, base.1.json, base.2.json, ..."""
    archive = outbox_root / agent / ".archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{base_task_id}.json").write_text("{}")
    for i in range(1, n):
        (archive / f"{base_task_id}.{i}.json").write_text("{}")


class GatherRetryRepeatsFilterTests(unittest.TestCase):

    def test_notify_with_rotations_is_excluded(self):
        """notify-build-foo with .json + .1.json + .2.json (3 files) -> NOT in repeats."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_archive_files(root, "beacon", "notify-build-foo-001", 3)
            result = gather_retry_repeats(root)
            tids = [r["task_id"] for r in result]
            self.assertNotIn("notify-build-foo-001", tids)

    def test_real_retry_not_excluded(self):
        """A real retry (marker-error-foo with rotations) IS in repeats."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_archive_files(root, "forge", "marker-error-foo-1", 3)
            result = gather_retry_repeats(root)
            tids = [r["task_id"] for r in result]
            self.assertIn("marker-error-foo-1", tids)

    def test_mixed_only_real_retries_surface(self):
        """A folder of mixed workflow + real-retry archives surfaces only real retries."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 4 workflow notifies (would have shown up as 3 retries each in v1)
            write_archive_files(root, "beacon", "notify-task-a", 3)
            write_archive_files(root, "beacon", "notify-task-b", 3)
            write_archive_files(root, "beacon", "notify-task-c", 3)
            write_archive_files(root, "beacon", "notify-task-d", 3)
            # 1 real marker-error retry cascade
            write_archive_files(root, "forge", "marker-error-real-1", 3)
            # 1 real cycle-fix re-dispatch
            write_archive_files(root, "pulse", "cycle-fix-real-001", 3)
            # 1 plain feature task with 3 rotations (real retry — Forge revision
            # rounds use this shape on the original task_id)
            write_archive_files(root, "forge", "build-feature-foo", 3)
            result = gather_retry_repeats(root)
            tids = sorted(r["task_id"] for r in result)
            self.assertEqual(
                tids,
                ["build-feature-foo", "cycle-fix-real-001", "marker-error-real-1"],
                "Only the 3 real retries should surface; the 4 notify-* workflow rotations should be excluded",
            )

    def test_notify_below_threshold_does_not_leak(self):
        """notify with only 2 files (below HIGH_REPEAT_COUNT_THRESHOLD=3) was
        already excluded by threshold; confirm the new filter doesn't break
        that path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_archive_files(root, "beacon", "notify-foo", 2)
            result = gather_retry_repeats(root)
            self.assertEqual(result, [])

    def test_empty_outbox_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(gather_retry_repeats(root), [])

    def test_nonexistent_outbox_root_returns_empty(self):
        self.assertEqual(gather_retry_repeats(Path("/nonexistent")), [])


if __name__ == "__main__":
    unittest.main()
