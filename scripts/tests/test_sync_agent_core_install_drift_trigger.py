#!/usr/bin/env python3
"""Tests for sync_agent_core.sh's Step 7b post-merge install-drift trigger.

post-merge-install-drift-trigger-001 (2026-06-11): the systemd install-drift
healer only installs repo units to /etc/systemd/system/ on its 12h timer, so a
new or changed systemd/*.service|*.timer can sit on disk up to 12h before
install while the RUNNING unit uses stale config (PR #438 transcript-persist
CRITICALs). Sync now fires the healer in --triggered mode within the sync cycle
(<=1h) when this sync's OLD_HEAD..NEW_HEAD diff touched a unit file.

Behavior under test:
  - a changed systemd/*.service (or *.timer)  → healer invoked once, with
    --triggered and OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED=true; sync succeeds.
  - a changed non-systemd file (no unit)      → healer NOT invoked; sync
    succeeds.
  - the healer exits non-zero                 → non-fatal: a WARN is logged and
    sync still completes (the 12h timer stays the backstop).

Each subtest drives the REAL sync_agent_core.sh end-to-end in a tmpdir-rooted
fake agent-core repo wired to a local bare origin, with origin advanced one
commit ahead so OLD_HEAD != NEW_HEAD and the ff-pull lands the incoming unit
change. The install-drift healer is replaced by a recording shim in the fake
scripts dir so the test observes the invocation without touching real systemd.

Run:
    python3 -m unittest scripts.tests.test_sync_agent_core_install_drift_trigger
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Import the shared helper by bare name. The regression gate invokes
# `python3 -m unittest discover -s scripts/tests`, which imports each test as a
# TOP-LEVEL module — a `from .` relative import raises there and the file would
# show as a net-new _FailedTest load error. Putting this file's dir on sys.path
# and importing absolutely loads cleanly under BOTH discover and the
# `python3 -m unittest scripts.tests.<module>` module-path invocation.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _runtime_script_test_support import (  # noqa: E402
    copy_larry_alerts_cli,
    install_timeout_shim,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SYNC_SCRIPT = _REPO_ROOT / 'scripts' / 'sync_agent_core.sh'
_LIB_PULSE = _REPO_ROOT / 'scripts' / '_lib_pulse_runtime.sh'
_LIB_PUSH = _REPO_ROOT / 'scripts' / '_lib_push_with_rebase.sh'

# A recording shim standing in for heal_systemd_install_drift.py. It appends one
# line per invocation to $TRIGGER_SENTINEL capturing argv + the healer-enable env
# flag, then exits with $TRIGGER_SHIM_RC (default 0). This lets the trigger /
# non-trigger / error-non-fatal branches be asserted without real systemd.
_HEALER_SHIM = (
    '#!/usr/bin/env python3\n'
    'import os, sys\n'
    'sentinel = os.environ.get("TRIGGER_SENTINEL")\n'
    'if sentinel:\n'
    '    with open(sentinel, "a") as f:\n'
    '        f.write(repr(sys.argv[1:]) + "|enabled=" +\n'
    '                os.environ.get("OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED", "") + "\\n")\n'
    'sys.exit(int(os.environ.get("TRIGGER_SHIM_RC", "0")))\n'
)


class _SyncInstallDriftBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.home = self.tmp_path / 'home'
        self.home.mkdir()
        self.repo_dir = self.tmp_path / 'agent-core'
        self.repo_dir.mkdir()
        self.scripts_dir = self.repo_dir / 'scripts'
        self.scripts_dir.mkdir()

        (self.scripts_dir / 'sync_agent_core.sh').write_bytes(
            _SYNC_SCRIPT.read_bytes(),
        )
        copy_larry_alerts_cli(self.scripts_dir)
        (self.scripts_dir / '_lib_pulse_runtime.sh').write_bytes(
            _LIB_PULSE.read_bytes(),
        )
        (self.scripts_dir / '_lib_push_with_rebase.sh').write_bytes(
            _LIB_PUSH.read_bytes(),
        )
        os.chmod(self.scripts_dir / 'sync_agent_core.sh', 0o755)

        # The recording healer shim — the unit under integration test reaches it
        # via "${SCRIPTS_DIR}/heal_systemd_install_drift.py".
        self.healer_shim = self.scripts_dir / 'heal_systemd_install_drift.py'
        self.healer_shim.write_text(_HEALER_SHIM)
        os.chmod(self.healer_shim, 0o755)
        self.sentinel = self.tmp_path / 'trigger-sentinel.txt'

        self._git('init', '-q', '--initial-branch=main')
        self._git('config', 'user.email', 'test@example.com')
        self._git('config', 'user.name', 'Test')

        # A pre-existing tracked unit so a later modification is tracked-modified.
        systemd_dir = self.repo_dir / 'systemd'
        systemd_dir.mkdir()
        (systemd_dir / 'ourliberty-existing.service').write_text(
            '[Service]\nExecStart=/bin/true\n',
        )
        (self.repo_dir / 'README').write_text('seed\n')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'seed')

        self.origin = self.tmp_path / 'origin.git'
        subprocess.run(
            ['git', 'init', '--bare', '-q', '--initial-branch=main',
             str(self.origin)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._git('remote', 'add', 'origin', f'file://{self.origin}')
        self._git('push', '-q', 'origin', 'main')
        self._git('branch', '--set-upstream-to=origin/main', 'main')

        self.live_root = self.tmp_path / 'agents'
        self.staging_root = self.live_root / '.sync-staging'
        self.backup_root = self.live_root / '.sync-backup'
        self.live_root.mkdir()
        (self.live_root / 'blackboard').mkdir()

        self.shim_bin = self.tmp_path / 'bin'
        self.shim_bin.mkdir()
        install_timeout_shim(self.shim_bin)

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args, cwd=None):
        subprocess.run(
            ['git', *args],
            cwd=cwd or self.repo_dir, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _advance_origin(self, rel_path: str, content: str) -> None:
        """Commit a change touching `rel_path` and push it to origin, then move
        local HEAD back one commit so origin/main is one ahead — the shape sync
        sees after a PR merge (OLD_HEAD behind, ff-pull lands the change)."""
        target = self.repo_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self._git('add', '-A')
        self._git('commit', '-q', '-m', f'incoming change to {rel_path}')
        self._git('push', '-q', 'origin', 'main')
        self._git('reset', '--hard', 'HEAD~1', '--quiet')

    def _run_sync(self, shim_rc: int = 0):
        env = os.environ.copy()
        env['HOME'] = str(self.home)
        env['REPO_DIR'] = str(self.repo_dir)
        env['LIVE_ROOT'] = str(self.live_root)
        env['STAGING_ROOT'] = str(self.staging_root)
        env['BACKUP_ROOT'] = str(self.backup_root)
        env['TRIGGER_SENTINEL'] = str(self.sentinel)
        env['TRIGGER_SHIM_RC'] = str(shim_rc)
        env['PATH'] = f"{self.shim_bin}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run(
            ['bash', str(self.scripts_dir / 'sync_agent_core.sh')],
            env=env, cwd=self.repo_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60,
        )

    def _sentinel_lines(self) -> list[str]:
        if not self.sentinel.exists():
            return []
        return [ln for ln in self.sentinel.read_text().splitlines() if ln]


class UnitChangeTriggersHealerTest(_SyncInstallDriftBase):
    def test_changed_service_invokes_triggered_healer_once(self):
        self._advance_origin(
            'systemd/ourliberty-existing.service',
            '[Service]\nExecStart=/bin/true\nReadWritePaths=/home/larry\n',
        )
        result = self._run_sync()
        self.assertEqual(
            result.returncode, 0,
            f'sync should succeed; stdout={result.stdout!r} '
            f'stderr={result.stderr!r}',
        )
        lines = self._sentinel_lines()
        self.assertEqual(
            len(lines), 1,
            f'healer should be invoked exactly once; got {lines!r}',
        )
        # Invoked with --triggered and the remediation env forced on.
        self.assertIn("'--triggered'", lines[0])
        self.assertIn('enabled=true', lines[0])

    def test_new_timer_triggers_healer(self):
        self._advance_origin(
            'systemd/ourliberty-brand-new.timer',
            '[Timer]\nOnCalendar=hourly\n',
        )
        result = self._run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._sentinel_lines()), 1)


class NonUnitChangeSkipsHealerTest(_SyncInstallDriftBase):
    def test_non_systemd_change_does_not_invoke_healer(self):
        self._advance_origin('README', 'seed\nincoming non-unit change\n')
        result = self._run_sync()
        self.assertEqual(
            result.returncode, 0,
            f'sync should succeed; stderr={result.stderr!r}',
        )
        self.assertEqual(
            self._sentinel_lines(), [],
            'healer must NOT fire when no systemd unit file changed',
        )
        self.assertIn('no systemd unit file changed', result.stdout.decode())

    def test_systemd_non_unit_file_does_not_invoke_healer(self):
        # A change under systemd/ that is NOT a .service/.timer (e.g. a README)
        # must not match the trigger regex.
        self._advance_origin('systemd/NOTES.md', 'install notes\n')
        result = self._run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._sentinel_lines(), [])


class HealerErrorIsNonFatalTest(_SyncInstallDriftBase):
    def test_healer_nonzero_exit_is_warned_and_sync_proceeds(self):
        self._advance_origin(
            'systemd/ourliberty-existing.service',
            '[Service]\nExecStart=/bin/true\nNice=5\n',
        )
        result = self._run_sync(shim_rc=1)
        # Non-fatal: sync still completes successfully.
        self.assertEqual(
            result.returncode, 0,
            f'a healer error must not fail the sync; stderr={result.stderr!r}',
        )
        # The healer was attempted (once) and the WARN line was logged.
        self.assertEqual(len(self._sentinel_lines()), 1)
        out = result.stdout.decode()
        self.assertIn('healer (--triggered) exited non-zero', out)
        self.assertIn('the 12h timer remains the backstop', out)
        # Sync ran to completion past Step 7b.
        self.assertIn('Sync complete', out)


if __name__ == '__main__':
    unittest.main()
