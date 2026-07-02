"""Tests for the Tier-2 provisioning-parity check folded into the weekly
Tier-2 health probe. Pure read-only inspection — exercised against tmp
homes/units so it never touches the real droplet state."""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_tier  # noqa: E402
import heal_tier2_weekly_health_probe as probe  # noqa: E402


class TestTier2ProvisioningParity(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tier2-parity-")
        self.t1 = os.path.join(self.tmp, "tier1")
        self.t2 = os.path.join(self.tmp, "tier2")
        for h in (self.t1, self.t2):
            os.makedirs(os.path.join(h, ".claude"), exist_ok=True)
        # Tier-2 creds present by default.
        Path(self.t2, ".claude", ".credentials.json").write_text("{}")
        # MCP at parity by default.
        for h in (self.t1, self.t2):
            Path(h, ".claude.json").write_text(
                json.dumps({"mcpServers": {"workspace-mcp": {}}}))
        # Repo systemd dir with all session units carrying TIER2_HOME in RWP.
        self.sysd = Path(self.tmp, "systemd")
        self.sysd.mkdir()
        for unit in probe.SESSION_UNITS:
            (self.sysd / unit).write_text(
                "[Service]\nReadWritePaths=/home/larry/agents " + self.t2 + "\n")
        # Point the module at the tmp world.
        self._saved = (active_tier.TIER1_HOME, probe.TIER2_HOME, probe.REPO_SYSTEMD)
        active_tier.TIER1_HOME = self.t1
        probe.TIER2_HOME = self.t2
        probe.REPO_SYSTEMD = self.sysd
        # Default: NO tier-2 setup token, so the parity check RUNS (these tests
        # exercise the creds-fallback path where TIER2_HOME parity applies).
        # test_setup_token_skips_parity overrides this.
        _p = mock.patch.object(active_tier, '_setup_token_for_tier',
                               return_value=None)
        _p.start()
        self.addCleanup(_p.stop)

    def tearDown(self):
        active_tier.TIER1_HOME, probe.TIER2_HOME, probe.REPO_SYSTEMD = self._saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parity_clean(self):
        self.assertEqual(probe.check_provisioning_parity(), [])

    def test_missing_tier2_mcp_server_is_drift(self):
        Path(self.t2, ".claude.json").write_text(json.dumps({"mcpServers": {}}))
        drift = probe.check_provisioning_parity()
        self.assertTrue(any("workspace-mcp" in d and "MCP" in d for d in drift), drift)

    def test_missing_tier2_credentials_is_drift(self):
        (Path(self.t2, ".claude", ".credentials.json")).unlink()
        drift = probe.check_provisioning_parity()
        self.assertTrue(any("OAuth credentials missing" in d for d in drift), drift)

    def test_unit_without_tier2_rwp_is_drift(self):
        bad = self.sysd / probe.SESSION_UNITS[0]
        bad.write_text("[Service]\nReadWritePaths=/home/larry/agents\n")
        drift = probe.check_provisioning_parity()
        self.assertTrue(
            any(probe.SESSION_UNITS[0] in d and "ReadWritePaths" in d for d in drift),
            drift)

    def test_setup_token_skips_parity(self):
        # With a Tier-2 setup token, a Tier-2 dispatch runs under TIER1_HOME
        # (decoupled), so TIER2_HOME parity is moot — the check returns no drift
        # even when MCP is missing / creds absent / RWP dropped (all obsolete).
        Path(self.t2, ".claude.json").write_text(json.dumps({"mcpServers": {}}))
        (Path(self.t2, ".claude", ".credentials.json")).unlink()
        with mock.patch.object(active_tier, '_setup_token_for_tier',
                               return_value='sk-ant-oat01-t2'):
            self.assertEqual(probe.check_provisioning_parity(), [])

    def test_check_never_raises(self):
        # Point at a nonexistent systemd dir + unreadable homes: still a list.
        probe.REPO_SYSTEMD = Path(self.tmp, "does-not-exist")
        self.assertIsInstance(probe.check_provisioning_parity(), list)


if __name__ == "__main__":
    unittest.main()
