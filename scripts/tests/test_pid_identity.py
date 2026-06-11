#!/usr/bin/env python3
"""Tests for scripts/pid_identity.py — the PID-reuse guard (2026-06-05 audit
PR-G, findings #14 + #27).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pid_identity
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pid_identity  # noqa: E402


class ProcStarttimeTest(unittest.TestCase):
    def _stat_bytes(self, starttime: str) -> bytes:
        # comm (field 2) deliberately contains spaces AND nested parens, so the
        # parser must split on the LAST ')'. After it: state(3) .. starttime(22).
        after = ["S"] + ["0"] * 18 + [starttime]  # 20 tokens; rest[19] == field 22
        return b"4242 (my (weird) comm) " + " ".join(after).encode()

    def test_parses_starttime_past_tricky_comm(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=self._stat_bytes("987654"))):
            self.assertEqual(pid_identity.proc_starttime(4242), 987654)

    def test_returns_none_when_unreadable(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertIsNone(pid_identity.proc_starttime(4242))

    def test_returns_none_on_malformed(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=b"garbage-no-paren")):
            self.assertIsNone(pid_identity.proc_starttime(4242))

    @unittest.skipUnless(os.path.exists("/proc/self/stat"), "requires Linux /proc")
    def test_field_index_against_real_proc_self(self):
        import os as _os
        st = pid_identity.proc_starttime(_os.getpid())
        self.assertIsInstance(st, int)
        self.assertGreater(st, 0)
        with open("/proc/self/stat", "rb") as f:
            data = f.read()
        field22 = int(data[data.rfind(b")") + 1:].split()[19])
        self.assertEqual(st, field22)


class StillSameProcessTest(unittest.TestCase):
    def test_false_when_pid_gone(self):
        with mock.patch.object(pid_identity, "proc_starttime", return_value=None):
            self.assertFalse(pid_identity.still_same_process(123, 555))

    def test_false_on_starttime_mismatch_pid_reused(self):
        with mock.patch.object(pid_identity, "proc_starttime", return_value=999):
            self.assertFalse(pid_identity.still_same_process(123, 555))

    def test_true_on_starttime_match(self):
        with mock.patch.object(pid_identity, "proc_starttime", return_value=555):
            self.assertTrue(pid_identity.still_same_process(123, 555))

    def test_none_expected_skips_starttime_but_requires_liveness(self):
        with mock.patch.object(pid_identity, "proc_starttime", return_value=12):
            self.assertTrue(pid_identity.still_same_process(123, None))
        with mock.patch.object(pid_identity, "proc_starttime", return_value=None):
            self.assertFalse(pid_identity.still_same_process(123, None))

    def test_cmdline_substr_required(self):
        with mock.patch.object(pid_identity, "proc_starttime", return_value=555), \
             mock.patch.object(pid_identity, "proc_cmdline", return_value="claude --x sysprompt-main"):
            self.assertTrue(pid_identity.still_same_process(
                123, 555, require_cmdline_substr="sysprompt-main"))
        with mock.patch.object(pid_identity, "proc_starttime", return_value=555), \
             mock.patch.object(pid_identity, "proc_cmdline", return_value="vim notes.txt"):
            self.assertFalse(pid_identity.still_same_process(
                123, 555, require_cmdline_substr="sysprompt-main"))


if __name__ == "__main__":
    unittest.main()
