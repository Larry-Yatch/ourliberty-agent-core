#!/usr/bin/env python3
"""Regression GUARD — every droplet ``claude`` *inference* spawn MUST authenticate
via the durable per-tier setup-token, never HOME's auto-refreshing
``~/.claude/.credentials.json``.

WHY THIS EXISTS
---------------
The default OAuth credential rots headlessly: its ~14h access token expires and
nothing refreshes it on the fleet, so a direct ``claude`` call 401s every tick.
That silently broke the State Log narrator ("(basic summary — AI write
unavailable)"); the audit found Medic + the CEO digest + the dashboard
cleanup-verify on the same footing. The durable fix bridges the bare
``CLAUDE_CODE_OAUTH_TOKEN`` (the var the CLI reads) from the per-tier setup-token
(``CLAUDE_CODE_OAUTH_TOKEN_TIER{1,2}``) — via ``active_tier.durable_claude_env``
(Python) / ``agent_runner._apply_tier_auth`` / an ``export`` from
``active_tier.py active-setup-token`` (shell).

This test makes that the ENFORCED default (Mirror runs the suite on every PR), so
neither we nor the team can reintroduce the OAuth-rot class. Two checks:
  (1) file-level — any scripts/ file with a claude inference spawn must reference
      a durable bridge (catches a whole new file/script with no bridge);
  (2) per-call — an inline ``subprocess.run(['claude','--print',...])`` must pass
      ``env=`` in that very call (catches a NEW spawn dropped into an already-
      bridged file without the env= — the exact copy-paste regression).

KNOWN LIMITS (documented, not silent): a string spawn (``os.system("claude -p")``
/ ``bash -c "claude --print"``) and a CLAUDE_BIN build-then-spawn that drops its
bridge >0 inline are caught only by the file-level check, not per-call. The
high-probability regression — a fresh ``subprocess.run(['claude','--print'])`` —
is caught by both. Scope: ``scripts/`` (the droplet fleet). Desktop-only jobs
(under ``review/`` and other repos, Max account auto-refreshes) are out of scope.

Run:
    cd ~/agent-core && python3 -m unittest \\
        scripts.tests.test_claude_durable_auth_guard
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import re
import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# A `claude` INFERENCE spawn as a subprocess arg list: the binary (literal
# 'claude' OR the CLAUDE_BIN variable the bots use) immediately followed by
# --print / -p. Precise enough to ignore prose ("runs claude --print" in a
# docstring has no comma) and non-inference (`['claude','auth','status']`).
_PY_INFERENCE = re.compile(
    r"""(?:['"]claude['"]|\bCLAUDE_BIN\b)\s*,\s*['"](?:--print|-p)['"]""")
# Shell inference: `claude --print` / `claude -p` (incl. after `timeout ...`).
_SH_INFERENCE = re.compile(r"\bclaude\s+(?:--print|-p)\b")
# An INLINE literal-list spawn (the copy-paste shape) for the per-call check.
_INLINE_LITERAL = re.compile(r"""['"]claude['"]\s*,\s*['"](?:--print|-p)['"]""")

# Proof a file BRIDGES the bare CLAUDE_CODE_OAUTH_TOKEN from a setup-token.
# Function markers require a call `(` (so a bare mention in a comment is weaker),
# and the assignment branch uses =(?!=) so an `==` comparison is NOT a match.
_DURABLE = re.compile(
    r"durable_claude_env\("
    r"|_apply_tier_auth\("
    r"|active_setup_token\("
    r"|active-setup-token"
    r"|CLAUDE_CODE_OAUTH_TOKEN['\"]?\]?\s*=(?!=)"
    r"|export\s+CLAUDE_CODE_OAUTH_TOKEN"
)

# The known inference-spawn surface — pinned so a detection-regex regression that
# silently drops a file fails LOUDLY (anti-vacuity) instead of passing vacuously.
_EXPECTED_SURFACE = {
    "missions_narrator.py", "ceo_digest_generator.py", "dashboard_api.py",
    "agent_runner.py", "agent_telegram_bot.py", "beacon_telegram_bot.py",
    "heal_tier2_weekly_health_probe.py", "run_cycle.sh", "run_medic.sh",
}

# Files allowed to spawn `claude` WITHOUT the droplet durable bridge. Each entry
# MUST carry a written justification. Keep EMPTY unless a genuinely-exempt
# droplet spawn appears (desktop-only jobs live outside scripts/, not scanned).
EXEMPT: dict[str, str] = {}


def _scan_files():
    """{filename: source} for every scripts/ .py (non-test) or .sh with a claude
    inference spawn."""
    found = {}
    for path in sorted(_SCRIPTS.glob("*.py")) + sorted(_SCRIPTS.glob("*.sh")):
        if path.name.startswith("test_"):  # top-level test helpers / fixtures
            continue
        text = path.read_text(errors="replace")
        rx = _PY_INFERENCE if path.suffix == ".py" else _SH_INFERENCE
        if rx.search(text):
            found[path.name] = text
    return found


def _subprocess_calls(text):
    """Yield the source of each subprocess.run(...) / Popen(...) call body
    (paren-balanced), so a per-call check sees args + kwargs together."""
    for m in re.finditer(r"(?:subprocess\.run|subprocess\.Popen|\bPopen)\s*\(", text):
        depth, start = 0, m.end() - 1
        for j in range(start, min(len(text), start + 6000)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    yield text[start:j + 1]
                    break


class ClaudeDurableAuthGuard(unittest.TestCase):
    def test_canonical_helper_exists(self):
        import active_tier  # noqa: PLC0415
        self.assertTrue(
            callable(getattr(active_tier, "durable_claude_env", None)),
            "active_tier.durable_claude_env must exist — the canonical "
            "durable-token bridge every direct claude spawn should use.",
        )

    def test_audit_sees_the_full_known_spawn_surface(self):
        # Anti-vacuity: if a regex change silently stops matching a known spawn
        # file, fail loudly here rather than letting the real check pass empty.
        seen = set(_scan_files())
        missing = sorted(_EXPECTED_SURFACE - seen)
        self.assertEqual(
            missing, [],
            "The inference-spawn detector no longer sees these known spawn "
            "files (regex regression — the real guard would pass vacuously):\n  "
            + "\n  ".join(missing),
        )

    def test_every_inference_spawn_file_bridges_the_durable_token(self):
        offenders = [
            name for name, text in _scan_files().items()
            if name not in EXEMPT and not _DURABLE.search(text)
        ]
        self.assertEqual(
            offenders, [],
            "These scripts/ files spawn `claude` for inference but never bridge "
            "the durable setup-token, so they inherit HOME's rotting "
            "~/.claude/.credentials.json and 401 every ~14h:\n  "
            + "\n  ".join(offenders)
            + "\n\nFix — Python: subprocess.run(..., "
            "env=active_tier.durable_claude_env()).  Shell: export "
            "CLAUDE_CODE_OAUTH_TOKEN from `active_tier.py active-setup-token` "
            "(see run_cycle.sh / run_medic.sh).  Or add to EXEMPT with a reason.",
        )

    def test_inline_literal_spawns_pass_env_per_call(self):
        # Per-call: a `subprocess.run(['claude','--print',...])` (the copy-paste
        # shape) MUST set env= in that very call — catches a new unbridged spawn
        # dropped into a file that bridges some OTHER call.
        offenders = []
        for name, text in _scan_files().items():
            if name.endswith(".sh") or name in EXEMPT:
                continue
            for call in _subprocess_calls(text):
                if _INLINE_LITERAL.search(call) and "env=" not in call:
                    offenders.append(name)
                    break
        self.assertEqual(
            offenders, [],
            "These files have an inline `subprocess.run(['claude','--print',...])`"
            " call with NO env= (so it uses the default rotting credential):\n  "
            + "\n  ".join(offenders)
            + "\n\nPass env=active_tier.durable_claude_env() to the call.",
        )


if __name__ == "__main__":
    unittest.main()
