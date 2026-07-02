"""Tier-3 N-tier routing: recognition, home resolution, and the
fallback-priority pool (fallback_tier). Pure unit tests over a tmp agents-root."""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import active_tier  # noqa: E402


class TestTier3Routing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tier3-routing-")
        self._prev = os.environ.get("OURLIBERTY_AGENTS_ROOT")
        os.environ["OURLIBERTY_AGENTS_ROOT"] = self.tmp
        (Path(self.tmp) / "blackboard").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("OURLIBERTY_AGENTS_ROOT", None)
        else:
            os.environ["OURLIBERTY_AGENTS_ROOT"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- recognition ---
    def test_tier3_is_valid(self):
        self.assertIn("tier3", active_tier._VALID_TIERS)

    def test_token_env_for_tier3(self):
        self.assertEqual(
            active_tier._SETUP_TOKEN_ENV_BY_TIER["tier3"],
            "CLAUDE_CODE_OAUTH_TOKEN_TIER3",
        )

    def test_set_and_read_tier3(self):
        active_tier.set_tier("tier3")
        self.assertEqual(active_tier.read()["tier"], "tier3")

    # --- home resolution (shared real home) ---
    def test_home_for_tier3_is_real_home(self):
        self.assertEqual(active_tier.home_for_tier("tier3"), active_tier.TIER1_HOME)

    def test_current_home_tier3(self):
        active_tier.set_tier("tier3")
        self.assertEqual(active_tier.current_home(), active_tier.TIER1_HOME)

    # --- fallback priority pool ---
    def test_fallback_tier3_to_tier2(self):
        # tier3 (primary) -> tier2 (laptop/emergency) first.
        self.assertEqual(active_tier.fallback_tier("tier3"), "tier2")

    def test_fallback_preserves_legacy_tier1_to_tier2(self):
        self.assertEqual(active_tier.fallback_tier("tier1"), "tier2")

    def test_fallback_preserves_legacy_tier2_to_tier1(self):
        self.assertEqual(active_tier.fallback_tier("tier2"), "tier1")

    def test_fallback_skips_benched(self):
        # Bench tier2 (unparseable rate-limit -> backoff cooldown in the future).
        active_tier.set_cooldown("tier2", "you have hit your limit", kind="rate_limit")
        self.assertIsNotNone(active_tier.cooldown_until("tier2"))
        # tier3's first choice (tier2) is benched -> next is tier1.
        self.assertEqual(active_tier.fallback_tier("tier3"), "tier1")

    def test_fallback_none_when_all_others_benched(self):
        active_tier.set_cooldown("tier1", "you have hit your limit", kind="rate_limit")
        active_tier.set_cooldown("tier2", "you have hit your limit", kind="rate_limit")
        self.assertIsNone(active_tier.fallback_tier("tier3"))

    def test_other_home_for_active_tier3(self):
        active_tier.set_tier("tier3")
        # other_home() = home of fallback_tier(active=tier3) = tier2's home
        self.assertEqual(active_tier.other_home(), active_tier.TIER2_HOME)


if __name__ == "__main__":
    unittest.main()
