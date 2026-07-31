"""restart_guard — cordon-and-drain shared across every restarter (PR 3).

PR 2 proved the mechanism inside one healer (2026-07-22 logged the real thing:
`RESTART_DRAINED unit=ourliberty-inbox-watcher.service waited=135s`). PR 3 moves
it out so `medic_actions`, `heal_systemd_install_drift` and
`heal_claude_json_bind_drift` share it instead of each growing a copy.

Two NEW failure modes come with the move, and they are what most of this file
tests:

  1. MULTI-PRODUCER RACE. With one restarter, cordon-then-clear was safe. With
     four it is not: A cordons, B overwrites, A finishes and CLEARS, and B keeps
     draining against a cordon that no longer exists — the watcher resumes
     taking work and B restarts into a live session. That is the original bug,
     reintroduced by sharing the file. The per-unit lock is the fix, and
     `test_peer_cordon_survives_our_skip` is the test that would catch its
     removal.

  2. CEILING vs THE CALLER'S OWN TIME BUDGET. #1000: a 3600s drain under a 60s
     TimeoutStartSec meant systemd SIGTERMed the healer mid-drain and — Python's
     `finally` not running on SIGTERM — the restart silently never happened. It
     failed safe and did nothing. Each caller now has its own ceiling, and each
     ceiling must fit inside that caller's own outer bound. Medic's bound is not
     systemd at all but `timeout 10m claude` in run_medic.sh, which is exactly
     the kind of mismatch that ships unnoticed.
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import file_lock  # noqa: E402
import restart_guard as rg  # noqa: E402

_REPO_ROOT = _SCRIPTS_DIR.parent
AGENT_UNIT = 'ourliberty-inbox-watcher.service'
OTHER_UNIT = 'ourliberty-beacon-bot.service'


class _GuardFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='ol-pr3-'))
        self._saved = os.environ.get('OURLIBERTY_AGENTS_ROOT')
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(self.root)
        (self.root / 'state' / 'restart-cordon').mkdir(parents=True, exist_ok=True)
        (self.root / 'state' / 'in-flight').mkdir(parents=True, exist_ok=True)
        self.logs = []
        self.log = lambda msg, level='INFO': self.logs.append(f'{level}:{msg}')
        self.restarted = []

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
        else:
            os.environ['OURLIBERTY_AGENTS_ROOT'] = self._saved

    def restart_fn(self, result=(0, '')):
        def _fn():
            self.restarted.append(True)
            return result
        return _fn

    def cordon_exists(self, unit=AGENT_UNIT):
        return rg.cordon_file(unit).exists()

    def make_work_live(self):
        """A live in-flight marker — agent_work_in_flight() now reports busy."""
        marker = self.root / 'state' / 'in-flight' / 'busy-task.json'
        marker.write_text(json.dumps({'pid': os.getpid(), 'agent': 'mirror'}))

    def logged(self, needle):
        return any(needle in line for line in self.logs)


class PassThroughTests(_GuardFixture):
    """Only units that HOST Claude sessions pay the drain. Everything else —
    every .timer, the *-bot front-ends, dashboard-api — must restart with no
    added latency, or a needless wait just delays a fix."""

    def test_non_agent_unit_restarts_immediately_even_when_work_is_live(self):
        self.make_work_live()
        out = rg.guarded_restart(OTHER_UNIT, 'test', self.restart_fn(),
                                 log=self.log)
        self.assertTrue(out.performed)
        self.assertEqual(out.outcome, rg.OUTCOME_NOT_AGENT_HOSTING)
        self.assertEqual(len(self.restarted), 1)
        self.assertFalse(self.cordon_exists(OTHER_UNIT))

    def test_result_is_passed_through_untouched(self):
        # Callers keep their own rc contracts; the guard must not reinterpret.
        sentinel = (-1, 'restart issued but unit is inactive')
        out = rg.guarded_restart(OTHER_UNIT, 'test', self.restart_fn(sentinel),
                                 log=self.log)
        self.assertEqual(out.result, sentinel)


class MutualExclusionTests(_GuardFixture):
    """The race PR 3 introduces by giving one cordon file four writers."""

    def _hold_peer_lock(self):
        """Hold the unit's cordon lock the way a peer restarter would.

        flock treats separate fds independently even inside one process, so this
        contends for real rather than simulating contention."""
        import fcntl
        path = file_lock.sidecar_lock_path(rg.cordon_file(AGENT_UNIT))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd

    def test_peer_holding_the_lock_causes_a_skip_not_a_restart(self):
        self._hold_peer_lock()
        out = rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                                 log=self.log)
        self.assertFalse(out.performed)
        self.assertEqual(out.outcome, rg.OUTCOME_SKIPPED_PEER_ACTIVE)
        self.assertEqual(self.restarted, [],
                         'restarted while a peer was mid-drain on the same unit')
        self.assertTrue(self.logged('RESTART_SKIPPED_PEER_ACTIVE'))

    def test_peer_cordon_survives_our_skip(self):
        # THE regression this lock exists for. Without it we would run the
        # cordon→drain→clear sequence concurrently with the peer and delete the
        # cordon it is still draining behind, so the watcher would resume taking
        # work and the peer's restart would kill a live session.
        self._hold_peer_lock()
        rg.write_cordon(AGENT_UNIT, 'peer-drain-in-progress')
        before = rg.cordon_file(AGENT_UNIT).read_text()

        rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(), log=self.log)

        self.assertTrue(self.cordon_exists(),
                        "cleared a peer restarter's live cordon; its drain now "
                        "proceeds unprotected and will kill running work")
        self.assertEqual(rg.cordon_file(AGENT_UNIT).read_text(), before,
                         "overwrote a peer's cordon")

    def test_lock_is_released_so_a_later_restart_proceeds(self):
        # A lock we fail to release is a permanent restart stall — strictly
        # worse than the bug it prevents.
        first = rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                                   log=self.log)
        second = rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                                    log=self.log)
        self.assertTrue(first.performed)
        self.assertTrue(second.performed)
        self.assertEqual(len(self.restarted), 2)

    def test_lock_is_released_even_when_the_restart_raises(self):
        def boom():
            raise RuntimeError('systemctl exploded')

        with self.assertRaises(RuntimeError):
            rg.guarded_restart(AGENT_UNIT, 'test', boom, log=self.log)
        # The lock must be free and no cordon left standing.
        self.assertFalse(self.cordon_exists())
        out = rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                                 log=self.log)
        self.assertTrue(out.performed)


class CeilingPolicyTests(_GuardFixture):
    """`force` restarts anyway when work never drains; `refuse` stands down."""

    def test_refuse_does_not_restart_when_work_never_drains(self):
        self.make_work_live()
        out = rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                                 ceiling_sec=0, on_ceiling='refuse',
                                 log=self.log)
        self.assertFalse(out.performed)
        self.assertEqual(out.outcome, rg.OUTCOME_REFUSED_WORK_IN_FLIGHT)
        self.assertEqual(self.restarted, [])
        self.assertTrue(self.logged('RESTART_REFUSED_WORK_IN_FLIGHT'))

    def test_refuse_still_clears_the_cordon(self):
        # Standing down must not leave the queue cordoned — that would turn a
        # protected restart into a silent dispatch stall.
        self.make_work_live()
        rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                           ceiling_sec=0, on_ceiling='refuse', log=self.log)
        self.assertFalse(self.cordon_exists())

    def test_force_restarts_when_work_never_drains(self):
        self.make_work_live()
        out = rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                                 ceiling_sec=0, on_ceiling='force',
                                 log=self.log)
        self.assertTrue(out.performed)
        self.assertEqual(out.outcome, rg.OUTCOME_FORCED_OVER_CEILING)
        self.assertEqual(len(self.restarted), 1)
        self.assertTrue(self.logged('RESTART_FORCED_OVER_CEILING'))

    def test_force_is_the_default(self):
        # A caller that forgets the argument must get the PR-2 behavior, not a
        # silent new refusal that leaves a stale daemon running forever.
        self.make_work_live()
        out = rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                                 ceiling_sec=0, log=self.log)
        self.assertTrue(out.performed)

    def test_quiet_system_drains_and_restarts(self):
        out = rg.guarded_restart(AGENT_UNIT, 'test', self.restart_fn(),
                                 log=self.log)
        self.assertTrue(out.performed)
        self.assertEqual(out.outcome, rg.OUTCOME_DRAINED)
        self.assertTrue(self.logged('RESTART_DRAINED'))
        self.assertFalse(self.cordon_exists())


class CallerCeilingBudgetTests(unittest.TestCase):
    """#1000, generalized: every caller's drain ceiling must fit inside that
    caller's OWN outer time bound, or systemd/timeout kills it mid-drain and the
    restart silently never happens.

    These read the real unit files and the real shell script, so they fail if
    someone tunes a ceiling up without moving the bound that contains it."""

    def _timeout_start_sec(self, unit_name: str) -> int:
        unit = _REPO_ROOT / 'systemd' / unit_name
        for line in unit.read_text().splitlines():
            line = line.strip()
            if line.startswith('TimeoutStartSec='):
                return int(line.split('=', 1)[1].strip())
        self.fail(f'TimeoutStartSec not found in {unit_name}')

    def _carveout_daemon_units(self) -> list[str]:
        """Every persistent unit in systemd/ that carves the claude config.

        Computed by SCANNING the repo, not hardcoded, so adding an eighth
        carve-out daemon moves the assertion automatically instead of silently
        invalidating it."""
        import heal_claude_json_bind_drift as bd
        units = []
        for svc in sorted((_REPO_ROOT / 'systemd').glob('ourliberty-*.service')):
            text = svc.read_text()
            type_ = ''
            carves = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith('Type='):
                    type_ = s.split('=', 1)[1].strip()
                elif s.startswith('ReadWritePaths='):
                    carves = carves or bd.CLAUDE_JSON_PATH in s.split('=', 1)[1].split()
            if carves and type_ in bd.PERSISTENT_TYPES:
                units.append(svc.name)
        return units

    def test_bind_drift_unit_timeout_covers_the_WHOLE_FLEET_not_one_unit(self):
        """MULTI-UNIT, deliberately. This is the only caller in this class that
        loops over many units in a single invocation: one atomic replacement of
        /home/larry/.claude.json dangles EVERY carve-out unit in the same tick,
        so a budget sized for one repaired unit is not a budget at all. The
        siblings below restart exactly one unit per invocation and correctly
        keep the single-unit form — do not "harmonise" them into this shape."""
        import heal_claude_json_bind_drift as bd
        timeout = self._timeout_start_sec(
            'ourliberty-heal-claude-json-bind-drift.service')
        fleet = self._carveout_daemon_units()
        self.assertGreater(
            len(fleet), 1,
            'the fleet scan degenerated to <=1 unit — the multi-unit invariant '
            'would be silently equivalent to the old single-unit one')
        # One unit pays the cordon-and-drain ceiling; the rest are pass-throughs.
        needed = ((len(fleet) - 1) * bd.PEER_REPAIR_BUDGET_S
                  + bd.AGENT_DRAIN_CEILING_SEC + bd.RESTART_TIMEOUT_S
                  + bd.VERIFY_WINDOW_S)
        self.assertGreaterEqual(
            timeout, needed,
            f'TimeoutStartSec={timeout}s < {needed}s needed to repair all '
            f'{len(fleet)} dangled carve-out units in one tick ({fleet}): '
            f'systemd SIGTERMs the healer mid-loop, Python\'s `finally` does '
            f'not run on SIGTERM, and the units it had not reached are silently '
            f'never repaired with no alert (#1000)')

    def test_bind_drift_module_and_unit_agree_on_the_timeout(self):
        import heal_claude_json_bind_drift as bd
        timeout = self._timeout_start_sec(
            'ourliberty-heal-claude-json-bind-drift.service')
        self.assertEqual(bd.HEALER_TIMEOUT_START_SEC, timeout)
        self.assertLess(bd.TICK_BUDGET_S, timeout,
                        'the healer must stop STARTING repairs before systemd '
                        'stops the healer')

    def test_install_drift_unit_timeout_covers_its_ceiling(self):
        import heal_systemd_install_drift as idr
        timeout = self._timeout_start_sec(
            'ourliberty-heal-systemd-install-drift.service')
        needed = rg.RESTART_DEFER_CEILING_SEC + idr.RESTART_TIMEOUT_S
        self.assertGreaterEqual(
            timeout, needed,
            f'TimeoutStartSec={timeout}s < {needed}s needed for a full-ceiling '
            f'drain plus the restart (#1000)')

    def test_medic_ceiling_fits_inside_its_claude_session_timeout(self):
        # Medic's binding constraint is NOT systemd: medic_actions runs inside
        # `timeout $CLAUDE_TIMEOUT claude` in run_medic.sh. A ceiling copied from
        # heal_stale_daemon_code (3600s) would be killed by timeout(1) long
        # before it finished — #1000 in a place systemd cannot protect.
        import medic_actions
        script = (_REPO_ROOT / 'scripts' / 'run_medic.sh').read_text()
        m = re.search(r'CLAUDE_TIMEOUT="\$\{MEDIC_CLAUDE_TIMEOUT:-(\d+)([smh])\}"',
                      script)
        self.assertIsNotNone(
            m, 'could not parse CLAUDE_TIMEOUT from run_medic.sh — if its shape '
               'changed, this budget check must be updated, not deleted')
        value, unit = int(m.group(1)), m.group(2)
        budget = value * {'s': 1, 'm': 60, 'h': 3600}[unit]
        self.assertLess(
            medic_actions.AGENT_DRAIN_CEILING_SEC, budget,
            f'Medic drain ceiling {medic_actions.AGENT_DRAIN_CEILING_SEC}s >= '
            f'its {budget}s claude session budget: timeout(1) kills the session '
            f'mid-drain and the restart never happens')

    def test_medic_ceiling_leaves_room_for_the_rest_of_its_work(self):
        # The drain is one step inside a session that also investigates, acts,
        # verifies and notifies. Half the budget is the most it may consume.
        import medic_actions
        self.assertLessEqual(medic_actions.AGENT_DRAIN_CEILING_SEC, 300)


class InstallDriftScopeTests(_GuardFixture):
    """Only the long-running .service path can hit an agent-hosting unit; the
    timer paths must stay direct (a timer hosts no sessions)."""

    def test_timer_content_drift_does_not_cordon(self):
        import heal_systemd_install_drift as idr
        with mock.patch.object(idr, '_cp_and_reload', return_value=(0, '')), \
                mock.patch.object(idr, 'subprocess') as sp, \
                mock.patch.object(idr, '_systemctl_show', return_value={
                    'ActiveState': 'active', 'NextElapseUSecRealtime': '123'}), \
                mock.patch.object(rg, 'guarded_restart') as guard:
            sp.run.return_value = mock.Mock(returncode=0, stderr='')
            rc, _ = idr._remediate_content_drift('ourliberty-cycle.timer')
        self.assertEqual(rc, 0)
        guard.assert_not_called()

    def test_long_running_service_content_drift_is_guarded(self):
        import heal_systemd_install_drift as idr
        with mock.patch.object(idr, '_cp_and_reload', return_value=(0, '')), \
                mock.patch.object(idr, '_service_is_long_running',
                                  return_value=True), \
                mock.patch.object(idr, '_restart_now',
                                  return_value=(0, '')) as restart:
            rc, _ = idr._remediate_content_drift(AGENT_UNIT)
        self.assertEqual(rc, 0)
        restart.assert_called_once_with(AGENT_UNIT)
        self.assertFalse(self.cordon_exists(), 'cordon left behind')

    def test_service_restart_is_skipped_when_a_peer_holds_the_lock(self):
        import fcntl
        import heal_systemd_install_drift as idr
        path = file_lock.sidecar_lock_path(rg.cordon_file(AGENT_UNIT))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)

        with mock.patch.object(idr, '_cp_and_reload', return_value=(0, '')), \
                mock.patch.object(idr, '_service_is_long_running',
                                  return_value=True), \
                mock.patch.object(idr, '_restart_now') as restart, \
                mock.patch.object(idr, 'log'):
            rc, _ = idr._remediate_content_drift(AGENT_UNIT)
        # The unit file was still copied + reloaded; only the restart deferred,
        # and the peer's restart loads that same file. Reported as success so
        # the healer does not DM a failure for work that did land.
        self.assertEqual(rc, 0)
        restart.assert_not_called()


class BindDriftIntegrationTests(_GuardFixture):
    """A skipped repair must not burn the per-unit restart cooldown — the next
    2-minute tick has to be free to repair if the peer's restart didn't land."""

    def test_peer_skip_does_not_mark_the_restart_cooldown(self):
        import fcntl
        import heal_claude_json_bind_drift as bd
        path = file_lock.sidecar_lock_path(rg.cordon_file(AGENT_UNIT))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)

        state = bd.load_state()
        facts = bd.UnitFacts(
            unit=AGENT_UNIT, present=True, type_='simple',
            active_state='active', main_pid=123,
            read_write_paths=bd.CLAUDE_JSON_PATH, restart_policy='on-failure',
            triggered_by='', invocation_id='inv-old')
        with mock.patch.object(bd, 'unit_facts', return_value=facts), \
                mock.patch.object(bd, '_namespace_probeable', return_value=True), \
                mock.patch.object(bd, 'probe_namespace_writable',
                                  return_value=bd._PROBE_EROFS), \
                mock.patch.object(bd, 'restart_and_verify') as restart, \
                mock.patch.object(bd, 'mark_restarted') as mark, \
                mock.patch.object(bd, 'save_state'), \
                mock.patch.object(bd, 'log'):
            outcome = bd.check_unit(AGENT_UNIT, state)

        self.assertEqual(outcome, 'repair-skipped-peer-active')
        restart.assert_not_called()
        mark.assert_not_called()
        # …and no pending-verification obligation is left behind: nothing was
        # restarted, so a later grace-expiry must not page repair-did-not-land.
        self.assertIsNone(bd.pending_entry(state, AGENT_UNIT))


try:  # reuse the proven medic isolation (env redirect + registry ownership)
    from .test_medic_actions import _IsolatedActions, ALLOWED_DAEMON, ALERT_FP
except ImportError:
    from test_medic_actions import _IsolatedActions, ALLOWED_DAEMON, ALERT_FP


class MedicGuardTests(_IsolatedActions):
    """Medic must stand down rather than kill a review, and a stand-down must
    leave the recurrence gate UN-armed so it can act on a later cycle."""

    def _make_work_live(self):
        import medic_actions
        d = medic_actions.AGENTS_ROOT / 'state' / 'in-flight'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'busy.json').write_text(json.dumps({'pid': os.getpid()}))

    def test_live_agent_work_refuses_the_restart(self):
        import medic_actions
        import medic_ledger
        self._make_work_live()
        with mock.patch.object(medic_actions, 'AGENT_DRAIN_CEILING_SEC', 0), \
                mock.patch.object(medic_actions, '_run_restart') as restart, \
                mock.patch.object(medic_actions, '_is_active') as active:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP,
                                               attempt=1)
        self.assertFalse(res['ok'])
        self.assertEqual(res['outcome'], 'skipped')
        self.assertEqual(res['reason'], 'agent-work-in-flight')
        restart.assert_not_called()
        active.assert_not_called()
        # Un-armed: a refusal is not an action, so Medic may act next cycle.
        self.assertFalse(medic_ledger.has_acted(ALERT_FP))

    def test_peer_restarter_refuses_with_its_own_reason(self):
        import fcntl
        import medic_actions
        import medic_ledger
        path = file_lock.sidecar_lock_path(rg.cordon_file(ALLOWED_DAEMON))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)

        with mock.patch.object(medic_actions, '_run_restart') as restart:
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP,
                                               attempt=1)
        self.assertEqual(res['outcome'], 'skipped')
        self.assertEqual(res['reason'], 'peer-restart-in-flight')
        restart.assert_not_called()
        self.assertFalse(medic_ledger.has_acted(ALERT_FP))

    def test_quiet_system_still_restarts_normally(self):
        # The guard must not become a blanket refusal: Medic's whole job is to
        # restart a unit that systemd gave up on, and that unit is usually down
        # (so nothing is in flight in it).
        import medic_actions
        with mock.patch.object(medic_actions, '_run_restart',
                               return_value=0) as restart, \
                mock.patch.object(medic_actions, '_is_active',
                                  return_value='active'):
            res = medic_actions.restart_daemon(ALLOWED_DAEMON, ALERT_FP,
                                               attempt=1)
        self.assertTrue(res['ok'])
        self.assertEqual(res['outcome'], 'acted')
        restart.assert_called_once_with(ALLOWED_DAEMON)


if __name__ == '__main__':
    unittest.main()
