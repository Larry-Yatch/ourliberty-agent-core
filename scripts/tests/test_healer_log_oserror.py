"""Sibling-healer log() fail-closed regressions (PR-F follow-up).

PR-F (#361) fixed audit #42 by wrapping heal_recovery_already_merged.log() so a
full / read-only / permission-denied log filesystem can't crash the healer. A
code-review found sibling healers with the identical unguarded log() shape —
mkdir + open(LOG_FILE, "a") + write with no try/except — each invoked as the
final HEARTBEAT statement of main() with no surrounding guard. An OSError there
exits the systemd oneshot non-zero and records the tick as failed (the exact
failure mode #42 fixed). Each test mirrors
test_pr_f_fail_closed.HealRecoveryLogOSErrorTest: log() must swallow OSError.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# Module name -> imported as. Every entry must expose a log(level, msg) that is
# best-effort: an OSError from the log filesystem must not propagate.
_HEALERS = (
    "heal_abandoned_inbox_tasks",
    "heal_blocked_inbox_age",
    "heal_empty_inbox_files",
    "heal_restart_dedup_obsolete",
    "heal_silent_loop_death",
    "heal_zombie_main_workers",
)


class HealerLogOSErrorTest(unittest.TestCase):
    """Each sibling healer's log() must swallow OSError (audit #42 shape)."""

    def test_log_swallows_oserror(self):
        for name in _HEALERS:
            with self.subTest(healer=name):
                mod = __import__(name)
                # mkdir is inside the same try, so patch it too — a real mkdir of
                # the (absent) droplet log dir would itself raise OSError on the
                # test box; we want the open() OSError to be the thing exercised.
                with mock.patch.object(mod.Path, "mkdir", return_value=None), \
                        mock.patch("builtins.open", side_effect=OSError("disk full")):
                    mod.log("HEARTBEAT", "should not raise")  # must not raise


if __name__ == "__main__":
    unittest.main()
