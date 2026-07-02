"""Guard: every agents/ state-root construction must honor the
OURLIBERTY_AGENTS_ROOT override, so a per-tier HOME swap (the Claude CLI
auth HOME) never blinds app-state path resolution.

Background: agent_runner swaps HOME to the active tier's account home so the
Claude CLI finds that tier's OAuth. Any module that resolves agents/ state via
a bare `Path.home() / 'agents'` (or `HOME / 'agents'`) therefore points at the
wrong tree under Tier 2 — both reading blind and writing state to the fallback
home. The fix pins OURLIBERTY_AGENTS_ROOT to the real account home in the child
env; this test stops the gap reopening by requiring every state-root line to
read that override on the same line (the `... or Path.home()/'agents'` idiom,
or the `Path(override) if override else ...` form).
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
BARE = re.compile(r"(?:Path\.home\(\)|(?<!\w)HOME)\s*/\s*['\"]agents['\"]")
# A bare line is OK only if it also carries the override on the same line.
ALLOW_TOKENS = ("OURLIBERTY_AGENTS_ROOT", "override", "root")


class TestAgentsRootOverride(unittest.TestCase):
    def test_no_bare_agents_root_without_override(self):
        offenders = []
        for py in sorted(SCRIPTS.glob("*.py")):
            if py.name.startswith("test_"):
                continue
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if BARE.search(line) and not any(t in line for t in ALLOW_TOKENS):
                    offenders.append(f"{py.name}:{i}: {line.strip()}")
        self.assertFalse(
            offenders,
            "agents/ state-root must honor OURLIBERTY_AGENTS_ROOT "
            "(a per-tier HOME swap would otherwise blind these). Offenders:\n  - "
            + "\n  - ".join(offenders),
        )

    def test_modules_resolve_under_override(self):
        """Behavioral: with OURLIBERTY_AGENTS_ROOT set and HOME pointed at a
        bogus tier-2 home, lightweight modules resolve their root under the
        override, not under HOME."""
        import importlib
        override = "/tmp/ol-test-agents-root"
        old = {k: os.environ.get(k) for k in ("OURLIBERTY_AGENTS_ROOT", "HOME")}
        os.environ["OURLIBERTY_AGENTS_ROOT"] = override
        os.environ["HOME"] = "/tmp/ol-bogus-tier2-home"
        try:
            for modname, attr in [
                ("larry_alerts", "AGENTS_ROOT"),
                ("concurrency_guard", "AGENTS_ROOT"),
                ("dispatch_lease", "AGENTS_ROOT"),
                ("kill_switch", "AGENTS_ROOT"),
                ("active_tier", "AGENTS_ROOT"),
            ]:
                mod = importlib.import_module(modname)
                importlib.reload(mod)
                val = str(getattr(mod, attr))
                self.assertTrue(
                    val.startswith(override),
                    f"{modname}.{attr} = {val!r} did not honor override {override!r}",
                )
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
