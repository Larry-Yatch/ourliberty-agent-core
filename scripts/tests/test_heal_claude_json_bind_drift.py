#!/usr/bin/env python3
"""Tests for heal_claude_json_bind_drift.

Covers:
  - the in-namespace write-probe exit-code contract (run for real against a
    writable temp file and a missing path; mapping verified at the
    subprocess boundary for the EROFS case);
  - target-unit filtering (persistent + active + carves .claude.json);
  - the per-unit check_unit outcome matrix (healthy / rebound / repair-failed /
    cooldown-dangled / probe-error / skip);
  - cooldown discipline;
  - kill-switch short-circuit;
  - a static config-lint of the repo systemd units enforcing the #470 invariant
    (every claude-sandboxed unit that carves the .claude/ dir also carves the
    .claude.json file), plus a guard that THIS healer's own unit is not
    hardened in a way that would block its sudo/nsenter calls.

systemctl/nsenter/restart shellouts and the larry_alerts DMs are stubbed; no
DM ever leaves the test process and no real unit is restarted.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_claude_json_bind_drift
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_claude_json_bind_drift as h  # noqa: E402

_REPO_ROOT = _REPO_SCRIPTS.parent
_SYSTEMD_DIR = _REPO_ROOT / 'systemd'


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test so the
    healer's STATE_FILE / LOG_FILE / HEARTBEAT_FILE / KILL_SWITCH derive from a
    throwaway tree. Reload after the override so module-level constants pick it
    up, and restore on teardown."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp(prefix='bind-drift-root-')
        for sub in ('logs', 'state', 'blackboard'):
            os.makedirs(os.path.join(self._tmp, sub), exist_ok=True)
        self._orig = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = self._tmp
        importlib.reload(h)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._orig
        importlib.reload(h)
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()


# -------------------- probe snippet: end-to-end exit-code contract --------------------

class ProbeSnippetTests(unittest.TestCase):
    """The probe must (1) never modify the file, (2) exit 0 when the file is
    openable O_RDWR, (3) exit 3 (not 2) for a non-EROFS OSError like ENOENT.
    The EROFS→2 leg is unreachable without a real read-only mount, so it's
    asserted structurally here and exercised via mocks in ProbeMappingTests.
    """

    def _run(self, path: str):
        snippet = h._PROBE_SNIPPET.format(path=path)
        return subprocess.run(
            [sys.executable, '-c', snippet],
            capture_output=True, text=True, timeout=10,
        )

    def test_writable_file_exits_ok_and_does_not_modify(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('{"keep":"me"}')
            path = f.name
        try:
            before = Path(path).read_bytes()
            before_mtime = os.stat(path).st_mtime
            result = self._run(path)
            self.assertEqual(result.returncode, h._PROBE_OK)
            # O_RDWR with no O_CREAT/O_TRUNC must leave content + mtime intact.
            self.assertEqual(Path(path).read_bytes(), before)
            self.assertEqual(os.stat(path).st_mtime, before_mtime)
        finally:
            os.unlink(path)

    def test_missing_path_exits_other_not_erofs(self):
        result = self._run('/no/such/claude.json/here')
        self.assertEqual(result.returncode, h._PROBE_OTHER)
        self.assertNotEqual(result.returncode, h._PROBE_EROFS)

    def test_snippet_opens_rdwr_without_create_or_truncate(self):
        # Structural guard: the probe must never be able to create/truncate the
        # config file. (Belt-and-suspenders to the behavioral test above.)
        self.assertIn('os.O_RDWR', h._PROBE_SNIPPET)
        self.assertNotIn('O_CREAT', h._PROBE_SNIPPET)
        self.assertNotIn('O_TRUNC', h._PROBE_SNIPPET)
        self.assertIn('errno.EROFS', h._PROBE_SNIPPET)


# -------------------- probe: returncode -> outcome mapping --------------------

class ProbeMappingTests(unittest.TestCase):
    def _mock_run(self, returncode, stderr='', raises=None):
        def _run(*a, **k):
            if raises is not None:
                raise raises
            return mock.MagicMock(returncode=returncode, stdout='', stderr=stderr)
        return _run

    def test_rc0_is_ok(self):
        with mock.patch.object(h.subprocess, 'run', self._mock_run(0)):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_OK)

    def test_rc2_is_erofs(self):
        with mock.patch.object(h.subprocess, 'run',
                               self._mock_run(2, stderr='30:Read-only file system')):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_EROFS)

    def test_rc3_is_other(self):
        # Namespace present (real in-namespace OSError) → genuine probe-error.
        with mock.patch.object(h.subprocess, 'run', self._mock_run(3, stderr='2:No such file')), \
             mock.patch.object(h, '_namespace_probeable', return_value=True):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_OTHER)

    def test_sudo_rejection_rc1_is_other_never_erofs(self):
        # sudo -n failing (NOPASSWD revoked) with the namespace still present must
        # NOT read as a dangle — and must not read as a benign mid-probe exit.
        with mock.patch.object(h.subprocess, 'run',
                               self._mock_run(1, stderr='sudo: a password is required')), \
             mock.patch.object(h, '_namespace_probeable', return_value=True):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_OTHER)

    def test_timeout_is_other(self):
        with mock.patch.object(
                h.subprocess, 'run',
                self._mock_run(0, raises=subprocess.TimeoutExpired('nsenter', 15))):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_OTHER)

    def test_nsenter_missing_is_other(self):
        with mock.patch.object(h.subprocess, 'run',
                               self._mock_run(0, raises=FileNotFoundError())):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_OTHER)

    def test_ns_mnt_enoent_namespace_gone_is_probe_gone(self):
        # Arm (a): short-lived unit exited mid-probe. nsenter fails ENOENT on
        # /proc/<pid>/ns/mnt AND the namespace handle is gone → benign skip.
        stderr = ('nsenter: cannot open /proc/123/ns/mnt: '
                  'No such file or directory')
        with mock.patch.object(h.subprocess, 'run',
                               self._mock_run(1, stderr=stderr)), \
             mock.patch.object(h, '_namespace_probeable', return_value=False):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_GONE)

    def test_ns_mnt_enoent_zombie_proc_lingers_is_probe_gone(self):
        # Regression for the false positive this PR fixes: same nsenter ENOENT
        # stderr, and even though /proc/<pid> still lingers as a zombie during
        # the reaping window, its /proc/<pid>/ns/mnt is already released →
        # _namespace_probeable False → benign _PROBE_GONE (NOT probe-blind). The
        # old proc-dir liveness check misread this exact case as _PROBE_OTHER.
        stderr = ('nsenter: cannot open /proc/123/ns/mnt: '
                  'No such file or directory')
        with mock.patch.object(h.subprocess, 'run',
                               self._mock_run(1, stderr=stderr)), \
             mock.patch.object(h, '_namespace_probeable', return_value=False):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_GONE)

    def test_genuine_blind_namespace_present_sudo_rejected_is_other(self):
        # A truly blind probe on a LIVE unit: the namespace handle still exists
        # but sudo -n is rejected (NOPASSWD revoked) → genuine _PROBE_OTHER, so a
        # real "healer is blind" incident still escalates.
        with mock.patch.object(
                h.subprocess, 'run',
                self._mock_run(1, stderr='sudo: a password is required')), \
             mock.patch.object(h, '_namespace_probeable', return_value=True):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_OTHER)


class NamespaceProbeableTests(unittest.TestCase):
    def test_current_process_has_namespace(self):
        self.assertTrue(h._namespace_probeable(os.getpid()))

    def test_pid_far_above_pid_max_is_not_probeable(self):
        # A pid far above the kernel's pid_max has no /proc entry at all.
        self.assertFalse(h._namespace_probeable(2_000_000_123))

    def test_zombie_proc_dir_present_but_ns_mnt_gone_is_not_probeable(self):
        # The discriminator must key on /proc/<pid>/ns/mnt, not /proc/<pid>: a
        # zombie/mid-reap process keeps its proc dir while its namespace handle
        # is already released. The helper reports NOT probeable even though the
        # proc dir lingers — the whole point of the fix.
        real_exists = os.path.exists

        def fake_exists(p):
            if p == '/proc/424242':
                return True   # zombie proc dir still present
            if p == '/proc/424242/ns/mnt':
                return False  # namespace handle already gone
            return real_exists(p)

        with mock.patch.object(h.os.path, 'exists', side_effect=fake_exists):
            self.assertFalse(h._namespace_probeable(424242))
            # sanity: a proc-dir-only check WOULD have (wrongly) said alive here.
            self.assertTrue(h.os.path.exists('/proc/424242'))


# -------------------- carveout_present + target filtering --------------------

class CarveoutPresentTests(unittest.TestCase):
    def test_exact_token_matches(self):
        self.assertTrue(h.carveout_present(
            '/home/larry/agents /home/larry/.claude /home/larry/.claude.json /tmp'))

    def test_absent_returns_false(self):
        self.assertFalse(h.carveout_present('/home/larry/agents /home/larry/.claude'))

    def test_substring_does_not_false_match(self):
        # A longer path that merely contains the token as a substring must not
        # count as the carve-out being present.
        self.assertFalse(h.carveout_present('/home/larry/.claude.json.bak'))

    def test_empty_is_false(self):
        self.assertFalse(h.carveout_present(''))


class TargetMainPidTests(unittest.TestCase):
    def _show(self, **props):
        return mock.patch.object(h, 'systemctl_show', return_value=props)

    def test_persistent_active_carveout_returns_pid(self):
        with self._show(Type='simple', ActiveState='active', MainPID='4242',
                        ReadWritePaths='/home/larry/.claude /home/larry/.claude.json'):
            self.assertEqual(h.target_main_pid('ourliberty-beacon-bot.service'), 4242)

    def test_oneshot_is_skipped(self):
        with self._show(Type='oneshot', ActiveState='active', MainPID='4242',
                        ReadWritePaths='/home/larry/.claude.json'):
            self.assertIsNone(h.target_main_pid('ourliberty-heal-x.service'))

    def test_inactive_is_skipped(self):
        with self._show(Type='simple', ActiveState='inactive', MainPID='0',
                        ReadWritePaths='/home/larry/.claude.json'):
            self.assertIsNone(h.target_main_pid('ourliberty-beacon-bot.service'))

    def test_no_carveout_is_skipped(self):
        with self._show(Type='simple', ActiveState='active', MainPID='4242',
                        ReadWritePaths='/home/larry/agents'):
            self.assertIsNone(h.target_main_pid('ourliberty-other.service'))

    def test_zero_pid_is_skipped(self):
        with self._show(Type='simple', ActiveState='active', MainPID='0',
                        ReadWritePaths='/home/larry/.claude.json'):
            self.assertIsNone(h.target_main_pid('ourliberty-beacon-bot.service'))


# -------------------- check_unit outcome matrix --------------------

class CheckUnitTests(_IsolatedAgentsRoot):
    UNIT = 'ourliberty-beacon-bot.service'

    def setUp(self):
        super().setUp()
        # Silence the alert emitters so no DM path is exercised and we can
        # assert call shape.
        for name in ('dm_repaired', 'dm_repair_failed', 'dm_still_dangled',
                     'dm_probe_blind'):
            p = mock.patch.object(h, name, return_value=True)
            setattr(self, f'_m_{name}', p.start())
            self.addCleanup(p.stop)

    def test_skip_when_not_target(self):
        with mock.patch.object(h, 'target_main_pid', return_value=None):
            self.assertEqual(h.check_unit(self.UNIT, h.load_state()), 'skip')

    def test_healthy_when_writable(self):
        with mock.patch.object(h, 'target_main_pid', return_value=111), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_OK):
            self.assertEqual(h.check_unit(self.UNIT, h.load_state()), 'healthy')
        self._m_dm_repaired.assert_not_called()

    def test_probe_error_escalates_once(self):
        state = h.load_state()
        with mock.patch.object(h, 'target_main_pid', return_value=111), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_OTHER):
            self.assertEqual(h.check_unit(self.UNIT, state), 'probe-error')
        self._m_dm_probe_blind.assert_called_once()

    def test_process_gone_is_benign_skip_no_dm(self):
        # Arm (a) at the routing level: _PROBE_GONE → 'skip', no probe-blind DM,
        # no escalation-cooldown state write (so a later real fault still alerts).
        state = h.load_state()
        with mock.patch.object(h, 'target_main_pid', return_value=111), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_GONE):
            self.assertEqual(h.check_unit(self.UNIT, state), 'skip')
        self._m_dm_probe_blind.assert_not_called()
        self.assertFalse(h.in_alert_cooldown(state, self.UNIT))

    def test_rebound_on_erofs_then_successful_restart(self):
        state = h.load_state()
        # First probe (old pid) → EROFS; reprobe (new pid) → OK.
        probe = mock.Mock(side_effect=[h._PROBE_EROFS, h._PROBE_OK])
        # target_main_pid: first call (detection) → 111, second (reverify) → 222.
        tpid = mock.Mock(side_effect=[111, 222])
        with mock.patch.object(h, 'target_main_pid', tpid), \
             mock.patch.object(h, 'probe_namespace_writable', probe), \
             mock.patch.object(h, 'restart_unit', return_value=(0, '')) as r:
            self.assertEqual(h.check_unit(self.UNIT, state), 'rebound')
        r.assert_called_once_with(self.UNIT)
        self._m_dm_repaired.assert_called_once()
        # reverified=True passed through (new namespace probe was OK).
        self.assertTrue(self._m_dm_repaired.call_args.args[1])
        # cooldown stamped so a subsequent tick won't immediately re-restart.
        self.assertTrue(h.in_restart_cooldown(state, self.UNIT))

    def test_repair_failed_when_restart_rc_nonzero(self):
        state = h.load_state()
        with mock.patch.object(h, 'target_main_pid', return_value=111), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_EROFS), \
             mock.patch.object(h, 'restart_unit', return_value=(1, 'unit not found')):
            self.assertEqual(h.check_unit(self.UNIT, state), 'repair-failed')
        self._m_dm_repair_failed.assert_called_once()
        self.assertTrue(h.in_restart_cooldown(state, self.UNIT))

    def test_cooldown_dangled_suppresses_restart(self):
        state = h.load_state()
        h.mark_restarted(state, self.UNIT)  # pretend we just restarted it
        with mock.patch.object(h, 'target_main_pid', return_value=111), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_EROFS), \
             mock.patch.object(h, 'restart_unit') as r:
            self.assertEqual(h.check_unit(self.UNIT, state), 'cooldown-dangled')
        r.assert_not_called()
        self._m_dm_still_dangled.assert_called_once()


# -------------------- main() kill-switch --------------------

class MainKillSwitchTests(_IsolatedAgentsRoot):
    def test_kill_switch_short_circuits(self):
        Path(h.KILL_SWITCH).parent.mkdir(parents=True, exist_ok=True)
        Path(h.KILL_SWITCH).write_text('disabled')
        with mock.patch.object(h, 'list_ourliberty_services') as lst:
            self.assertEqual(h.main(), 0)
        lst.assert_not_called()


# -------------------- static config-lint of the repo systemd units --------------------

def _read_write_paths_of(unit_text: str) -> list[str]:
    """All whitespace-separated tokens across every ReadWritePaths= line."""
    tokens: list[str] = []
    for line in unit_text.splitlines():
        s = line.strip()
        if s.startswith('ReadWritePaths='):
            tokens.extend(s.split('=', 1)[1].split())
    return tokens


def _has_protecthome_readonly(unit_text: str) -> bool:
    for line in unit_text.splitlines():
        if line.strip().replace(' ', '') == 'ProtectHome=read-only':
            return True
    return False


class CarveoutInvariantLintTests(unittest.TestCase):
    """#470 invariant: any sandboxed unit (ProtectHome=read-only) that grants
    the .claude/ DIRECTORY carve-out is a claude-invoking unit and MUST also
    grant the .claude.json FILE carve-out — otherwise the home-root config is
    read-only and claude EROFSes on write. This lint fails loudly if a future
    edit drops the file carve-out from any such unit (the original #470 bug)."""

    CLAUDE_DIR = '/home/larry/.claude'
    CLAUDE_JSON = '/home/larry/.claude.json'

    def test_every_claude_dir_unit_also_carves_the_json_file(self):
        offenders = []
        for svc in sorted(_SYSTEMD_DIR.glob('ourliberty-*.service')):
            text = svc.read_text()
            if not _has_protecthome_readonly(text):
                continue
            rwp = _read_write_paths_of(text)
            if self.CLAUDE_DIR in rwp and self.CLAUDE_JSON not in rwp:
                offenders.append(svc.name)
        self.assertEqual(
            offenders, [],
            msg=('these ProtectHome=read-only units carve the .claude/ dir but '
                 'NOT /home/larry/.claude.json — claude will EROFS on write '
                 f'(re-add the file carve-out per PR #470): {offenders}'),
        )


class HealerUnitPrivilegeLintTests(unittest.TestCase):
    """Guard against a future 'harden every unit' sweep silently breaking this
    healer: it MUST be able to sudo/nsenter, which NoNewPrivileges=true forbids.
    """

    SERVICE = _SYSTEMD_DIR / 'ourliberty-heal-claude-json-bind-drift.service'
    TIMER = _SYSTEMD_DIR / 'ourliberty-heal-claude-json-bind-drift.timer'

    def test_service_and_timer_exist(self):
        self.assertTrue(self.SERVICE.is_file(), self.SERVICE)
        self.assertTrue(self.TIMER.is_file(), self.TIMER)

    def test_service_is_not_nonewprivileges_or_protecthome(self):
        # Consider only ACTIVE directives — a comment that merely mentions
        # NoNewPrivileges/ProtectHome (this unit has an explanatory NOTE) is fine.
        directives = [
            ln.strip().replace(' ', '')
            for ln in self.SERVICE.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith('#')
        ]
        self.assertNotIn('NoNewPrivileges=true', directives)
        self.assertFalse(
            [d for d in directives if d.startswith('ProtectHome=')],
            msg='ProtectHome would break the nsenter namespace view + sudo',
        )

    def test_service_runs_as_larry(self):
        self.assertIn('User=larry', self.SERVICE.read_text())


if __name__ == '__main__':
    unittest.main()
