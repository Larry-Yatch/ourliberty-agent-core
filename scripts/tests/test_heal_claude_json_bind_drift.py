#!/usr/bin/env python3
"""Tests for heal_claude_json_bind_drift.

Covers:
  - the in-namespace write-probe exit-code contract (run for real against a
    writable temp file and a missing path; mapping verified at the
    subprocess boundary for the EROFS case);
  - CLASSIFICATION by `Restart=` (property: the verdict is INVARIANT under
    every TriggeredBy value — that is the old fail-open bug as an invariant);
  - ephemeral units are PROBED but structurally cannot be restarted;
  - coverage-delta observability (a unit leaving coverage is never silent, and
    never goes quiet again while it is still out);
  - the per-unit check_unit outcome matrix (healthy / rebound / repair-failed /
    cooldown-dangled / probe-error / the distinct skip reasons);
  - cooldown discipline;
  - kill-switch short-circuit;
  - a static config-lint of the repo systemd units enforcing the #470 invariant
    (every claude-sandboxed unit that carves the .claude/ dir also carves the
    .claude.json file) plus the new Restart=-declaration invariant, and a guard
    that THIS healer's own unit is not hardened in a way that would block its
    sudo/nsenter calls.

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
import larry_alerts  # noqa: E402

_REPO_ROOT = _REPO_SCRIPTS.parent
_SYSTEMD_DIR = _REPO_ROOT / 'systemd'
_RUNBOOK = _REPO_ROOT / 'runbooks' / 'claude-json-bind-drift.md'

CLAUDE_JSON = '/home/larry/.claude.json'


def _props(type_='simple', active='active', pid='4242',
           rwp=f'/home/larry/.claude {CLAUDE_JSON}', restart='always',
           triggered='') -> dict:
    """A full `systemctl show` property set, in the shape the healer asks for."""
    out = {
        'Type': type_, 'ActiveState': active, 'MainPID': pid,
        'ReadWritePaths': rwp, 'TriggeredBy': triggered,
    }
    if restart is not None:
        out['Restart'] = restart
    return out


def _facts(**kw) -> h.UnitFacts:
    """Build UnitFacts through the same parse the REAL parser does, without
    patching — nesting a patch inside a patched unit_facts recurses."""
    unit = kw.pop('unit', 'ourliberty-beacon-bot.service')
    props = _props(**kw)
    try:
        pid = int(props.get('MainPID', '0')) or None
    except ValueError:
        pid = None
    return h.UnitFacts(
        unit=unit, present=True, type_=props.get('Type', ''),
        active_state=props.get('ActiveState', '') or 'unknown',
        main_pid=pid, read_write_paths=props.get('ReadWritePaths', ''),
        restart_policy=props.get('Restart'),
        triggered_by=props.get('TriggeredBy', ''),
    )


class _FakeLarryAlerts:
    """Records append_alert kwargs; delegates classify_route to the real one so
    routing assertions test the real routing table, not a restatement of it."""

    def __init__(self):
        self.calls: list[dict] = []

    def classify_route(self, source, subject, healed):
        return larry_alerts.classify_route(source, subject, healed)

    def append_alert(self, **kw):
        self.calls.append(kw)
        return True

    def subjects(self):
        return [c.get('subject') for c in self.calls]


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

    def capture_alerts(self) -> _FakeLarryAlerts:
        fake = _FakeLarryAlerts()
        p = mock.patch.object(h, '_import_larry_alerts', return_value=fake)
        p.start()
        self.addCleanup(p.stop)
        return fake


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


class ClassificationTests(unittest.TestCase):
    """The verdict must come from the unit file's `Restart=`, which no manager
    link-state change can move — NOT from TriggeredBy, a reverse dependency
    that vanishes when the timer is disabled/masked/unloaded."""

    # (label, props kwargs, expected class). The expected values are LITERALS,
    # not h.CLASS_* references, for two reasons: an expectation read from the
    # constant under test moves with it, and a class-body reference to a symbol
    # would make this whole module fail to IMPORT against any build that lacks
    # it — collapsing every test in the file into one collection error.
    SHAPES = [
        ('cycle: Restart=no + timer', dict(restart='no'), 'ephemeral-job'),
        ('Restart=no, no timer', dict(restart='no'), 'ephemeral-job'),
        ('beacon: Restart=always, no timer', dict(restart='always'), 'monitor'),
        ('beacon + companion kick timer', dict(restart='always'), 'monitor'),
        ('outbox-notifier: Restart=on-failure + After=',
         dict(restart='on-failure'), 'monitor'),
        ('oneshot healer + timer',
         dict(type_='oneshot', restart='no'), 'skip-oneshot'),
        ('Restart= empty (systemd default shape)', dict(restart=''),
         'ephemeral-job'),
    ]

    TRIGGERED_BY_VALUES = ['', 'ourliberty-cycle.timer', 'ourliberty-other.timer',
                           'ourliberty-x.socket', 'ourliberty-x.path',
                           'ourliberty-x.socket ourliberty-x.timer']

    def test_classification_is_invariant_under_triggeredby(self):
        """PROPERTY: the same unit shape classifies identically for EVERY
        TriggeredBy value. That is the old fail-open bug expressed as an
        invariant — disabling a timer emptied TriggeredBy and silently returned
        ourliberty-cycle to the repair path."""
        for label, kw, expected in self.SHAPES:
            for triggered in self.TRIGGERED_BY_VALUES:
                with self.subTest(shape=label, triggered_by=triggered):
                    klass, reason = h.classify_unit(_facts(triggered=triggered, **kw))
                    self.assertEqual(klass, expected, msg=reason)

    def test_the_class_constants_hold_the_values_the_tables_pin(self):
        """SHAPES spells the classes out literally so this module still IMPORTS
        against a build that lacks them. This is the one place the literals are
        tied back to the constants, so a rename is a single loud failure rather
        than a table that silently stops asserting."""
        self.assertEqual(h.CLASS_EPHEMERAL, 'ephemeral-job')
        self.assertEqual(h.CLASS_MONITOR, 'monitor')
        self.assertEqual(h.CLASS_SKIP_ONESHOT, 'skip-oneshot')
        self.assertEqual(h.CLASS_SKIP_UNKNOWN, 'skip-unknown')

    def test_disabled_timer_repro_cycle_is_still_ephemeral(self):
        """The repro that motivated the change: cycle mid-fire with its timer
        DISABLED (TriggeredBy empty). Under the old TriggeredBy skip this
        returned a pid and the unit was restarted mid-`/cycle`."""
        facts = _facts(type_='simple', active='active', pid='386050',
                       restart='no', triggered='',
                       unit='ourliberty-cycle.service')
        klass, _ = h.classify_unit(facts)
        self.assertEqual(klass, h.CLASS_EPHEMERAL)

    def test_unreadable_unit_is_skip_unknown_never_ephemeral_or_monitor(self):
        with mock.patch.object(h, 'systemctl_show', return_value={}):
            facts = h.unit_facts('ourliberty-x.service')
        klass, _ = h.classify_unit(facts)
        self.assertEqual(klass, h.CLASS_SKIP_UNKNOWN)
        self.assertNotIn(klass, (h.CLASS_EPHEMERAL, h.CLASS_MONITOR))

    def test_missing_restart_property_is_skip_unknown(self):
        """A partial read (Restart absent from the property dump) must not be
        read as 'no' — an unreadable unit is neither descoped nor restarted."""
        facts = _facts(restart=None)
        self.assertEqual(h.classify_unit(facts)[0], h.CLASS_SKIP_UNKNOWN)

    def test_unrecognised_restart_value_is_skip_unknown(self):
        self.assertEqual(h.classify_unit(_facts(restart='sometimes'))[0],
                         h.CLASS_SKIP_UNKNOWN)

    def test_triggeredby_is_still_requested_and_reported_as_corroboration(self):
        with mock.patch.object(h, 'systemctl_show', return_value={}) as m:
            h.unit_facts('ourliberty-cycle.service')
        self.assertIn('TriggeredBy', m.call_args.args[1])
        self.assertIn('Restart', m.call_args.args[1])
        _, reason = h.classify_unit(_facts(restart='always',
                                           triggered='ourliberty-x.timer'))
        self.assertIn('TriggeredBy=ourliberty-x.timer', reason)

    def test_unit_facts_parses_the_real_property_dump(self):
        with mock.patch.object(
                h, 'systemctl_show',
                return_value=_props(active='active', pid='4242',
                                    restart='on-failure')):
            facts = h.unit_facts('ourliberty-inbox-watcher.service')
        self.assertTrue(facts.present)
        self.assertEqual(facts.main_pid, 4242)
        self.assertEqual(facts.restart_policy, 'on-failure')
        self.assertTrue(h.carveout_present(facts.read_write_paths))

    def test_unit_facts_has_no_active_only_filter(self):
        """The old accessor returned None for any unit whose ActiveState !=
        'active', collapsing 'oneshot', 'inactive', 'no carve-out' and
        'unreadable' into one indistinguishable skip. Every reason must now be
        readable by the caller."""
        with mock.patch.object(
                h, 'systemctl_show',
                return_value=_props(active='failed', pid='0')):
            facts = h.unit_facts('ourliberty-x.service')
        self.assertTrue(facts.present)
        self.assertEqual(facts.active_state, 'failed')
        self.assertIsNone(facts.main_pid)
        self.assertFalse(hasattr(h, 'target_main_pid'),
                         'target_main_pid was the accessor that made every skip '
                         'reason indistinguishable; it must not come back')


class RestartDeclarationLintTests(unittest.TestCase):
    """Build-time half of the coverage guard: classifying on `Restart=` means a
    persistent carve-out daemon that OMITS Restart= would silently become an
    EPHEMERAL_JOB and leave coverage. Fail CI instead.

    KNOWN PROSPECTIVE GAP (documented in the healer's module docstring): this
    reads the unit FILE, so a unit that omits `Type=` entirely is skipped even
    though systemd defaults it to Type=simple and the runtime classifier does
    see it. No unit in systemd/ omits Type= today, and the runtime `departed=`
    detector in coverage_delta covers the case in the meantime."""

    KNOWN_EPHEMERAL_UNITS = {'ourliberty-cycle.service'}

    def test_every_persistent_carveout_unit_declares_restart(self):
        # Guard the guard, inside the assertion it guards rather than as its own
        # test: an allowlist naming a unit that no longer exists is an excuse
        # that silently outlives its reason.
        for name in self.KNOWN_EPHEMERAL_UNITS:
            self.assertTrue((_SYSTEMD_DIR / name).is_file(),
                            f'{name} is allowlisted as ephemeral but does not exist')
        offenders = []
        for svc in sorted(_SYSTEMD_DIR.glob('ourliberty-*.service')):
            type_ = ''
            restart = None
            carves = False
            for line in svc.read_text().splitlines():
                s = line.strip()
                if s.startswith('Type='):
                    type_ = s.split('=', 1)[1].strip()
                elif s.startswith('Restart='):
                    restart = s.split('=', 1)[1].strip()
                elif s.startswith('ReadWritePaths='):
                    carves = carves or CLAUDE_JSON in s.split('=', 1)[1].split()
            if not carves or type_ not in h.PERSISTENT_TYPES:
                continue
            if svc.name in self.KNOWN_EPHEMERAL_UNITS:
                continue
            if restart not in h.SUPERVISED_RESTART_POLICIES:
                offenders.append(f'{svc.name} (Restart={restart!r})')
        self.assertEqual(
            offenders, [],
            msg=('these persistent units carve the claude config but do not '
                 'declare a supervised Restart=, so heal_claude_json_bind_drift '
                 'classifies them EPHEMERAL and stops repairing them. Add '
                 'Restart=always|on-failure, or add the unit to '
                 f'KNOWN_EPHEMERAL_UNITS with a reason: {offenders}'))


# -------------------- ephemeral: probe, never repair --------------------

class EphemeralProbeTests(_IsolatedAgentsRoot):
    UNIT = 'ourliberty-cycle.service'

    def setUp(self):
        super().setUp()
        self.alerts = self.capture_alerts()

    def _check(self, probe_result):
        facts = _facts(type_='simple', active='active', pid='386050',
                       restart='no', triggered='ourliberty-cycle.timer')
        with mock.patch.object(h, 'unit_facts', return_value=facts), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=probe_result) as probe, \
             mock.patch.object(h.restart_guard, 'guarded_restart',
                               side_effect=AssertionError(
                                   'the ephemeral path must not be able to '
                                   'reach guarded_restart')) as guard:
            outcome = h.check_unit(self.UNIT, h.load_state())
        return outcome, probe, guard

    def test_property_every_probe_result_is_detected_and_never_repaired(self):
        expected = {
            h._PROBE_OK: h.OUTCOME_HEALTHY,
            h._PROBE_EROFS: h.OUTCOME_EPHEMERAL_DANGLED,
            h._PROBE_OTHER: h.OUTCOME_EPHEMERAL_PROBE_ERROR,
            h._PROBE_GONE: h.OUTCOME_SKIP_NSGONE,
        }
        for probe_result, outcome_expected in expected.items():
            with self.subTest(probe=probe_result):
                self.alerts.calls.clear()
                outcome, probe, guard = self._check(probe_result)
                self.assertEqual(outcome, outcome_expected)
                probe.assert_called_once()          # detection ALWAYS runs
                guard.assert_not_called()           # repair NEVER runs
                self.assertEqual(self.alerts.calls, [])  # and never DMs

    def test_in_flight_cycle_dangle_is_recorded_not_silent(self):
        outcome, _, _ = self._check(h._PROBE_EROFS)
        self.assertEqual(outcome, h.OUTCOME_EPHEMERAL_DANGLED)
        self.assertIn('EPHEMERAL DANGLE', Path(h.LOG_FILE).read_text())

    def test_inactive_ephemeral_unit_is_never_probed(self):
        facts = _facts(type_='simple', active='inactive', pid='0', restart='no')
        with mock.patch.object(h, 'unit_facts', return_value=facts), \
             mock.patch.object(h, 'probe_namespace_writable') as probe:
            outcome = h.check_unit(self.UNIT, h.load_state())
        self.assertEqual(outcome, h.OUTCOME_SKIP_EPHEMERAL)
        probe.assert_not_called()

    def test_probe_cost_bound_over_a_synthetic_fleet(self):
        """Only ACTIVE persistent-or-ephemeral carve-out units are ever probed.
        A fleet of ~95 oneshot healers costs zero probes."""
        oneshots = [f'ourliberty-heal-x{i}.service' for i in range(95)]
        actives = ['ourliberty-beacon-bot.service', 'ourliberty-cycle.service']
        inactive = ['ourliberty-mirror-bot.service']
        units = oneshots + actives + inactive

        def fake_facts(unit):
            if unit in oneshots:
                return _facts(type_='oneshot', active='inactive', pid='0',
                              restart='no')
            if unit in inactive:
                return _facts(active='inactive', pid='0', restart='always')
            if unit == 'ourliberty-cycle.service':
                return _facts(active='active', pid='5', restart='no')
            return _facts(active='active', pid='6', restart='always')

        with mock.patch.object(h, 'list_ourliberty_services', return_value=units), \
             mock.patch.object(h, 'unit_facts', side_effect=fake_facts), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_OK) as probe:
            h.main()
        self.assertLessEqual(probe.call_count, len(actives))


# -------------------- coverage observability --------------------

class CoverageDeltaTests(_IsolatedAgentsRoot):

    def _lines(self):
        return Path(h.LOG_FILE).read_text().splitlines()

    def _clear_log(self):
        Path(h.LOG_FILE).write_text('')

    def test_baseline_then_departure_then_arrival(self):
        """PROPERTY over a synthetic set: departures WARN with the classifier
        reason, arrivals INFO, a stable set says neither."""
        state = h.load_state()
        h.coverage_delta(state, {'a.service', 'b.service'},
                         {'a.service': 'Restart=always', 'b.service': 'Restart=always'})
        self.assertTrue(any('coverage baseline' in ln for ln in self._lines()))
        self.assertFalse(any('LEFT coverage' in ln for ln in self._lines()))

        self._clear_log()
        h.coverage_delta(state, {'a.service', 'b.service'},
                         {'a.service': 'Restart=always', 'b.service': 'Restart=always'})
        self.assertFalse(any('LEFT coverage' in ln for ln in self._lines()))
        self.assertFalse(any('ENTERED coverage' in ln for ln in self._lines()))

        self._clear_log()
        h.coverage_delta(state, {'a.service', 'c.service'},
                         {'a.service': 'Restart=always',
                          'b.service': 'EPHEMERAL_JOB (Restart=no)',
                          'c.service': 'Restart=always'})
        lines = self._lines()
        left = [ln for ln in lines if 'LEFT coverage' in ln]
        entered = [ln for ln in lines if 'ENTERED coverage' in ln]
        self.assertEqual(len(left), 1)
        self.assertIn('b.service', left[0])
        self.assertIn('EPHEMERAL_JOB (Restart=no)', left[0])
        self.assertIn('[WARN]', left[0])
        self.assertEqual(len(entered), 1)
        self.assertIn('c.service', entered[0])

    def test_a_departed_unit_stays_named_on_every_later_tick(self):
        """THE BLIND SPOT THIS EXISTS TO CLOSE. A departure used to be announced
        in exactly one WARN, after which `state['coverage']` had already been
        rewritten to the shrunken set — so every subsequent tick read as normal
        and the unit sat permanently outside repair coverage with no standing
        signal anywhere."""
        state = h.load_state()
        reasons = {'a.service': 'Restart=always', 'b.service': 'Restart=always'}
        h.coverage_delta(state, {'a.service', 'b.service'}, reasons)

        # b leaves.
        self._clear_log()
        h.coverage_delta(state, {'a.service'},
                         {**reasons, 'b.service': 'EPHEMERAL_JOB (Restart=no)'})
        self.assertTrue(any('LEFT coverage' in ln for ln in self._lines()))

        # ...and is still named on the NEXT three ticks, which is the part that
        # used to be silent.
        for tick in range(3):
            with self.subTest(tick=tick):
                self._clear_log()
                h.coverage_delta(state, {'a.service'},
                                 {**reasons, 'b.service': 'EPHEMERAL_JOB (Restart=no)'})
                coverage = [ln for ln in self._lines() if 'coverage=' in ln]
                self.assertEqual(len(coverage), 1)
                self.assertIn('departed=b.service', coverage[0])

    def test_a_returning_unit_clears_the_departed_list(self):
        state = h.load_state()
        reasons = {'a.service': 'Restart=always', 'b.service': 'Restart=always'}
        h.coverage_delta(state, {'a.service', 'b.service'}, reasons)
        h.coverage_delta(state, {'a.service'},
                         {**reasons, 'b.service': 'EPHEMERAL_JOB (Restart=no)'})
        self._clear_log()
        h.coverage_delta(state, {'a.service', 'b.service'}, reasons)
        line = [ln for ln in self._lines() if 'coverage=' in ln][0]
        self.assertNotIn('departed=', line)
        self.assertEqual(state['coverage_departed'], {})

    def test_a_unit_gone_from_systemd_is_retired_out_loud(self):
        state = h.load_state()
        reasons = {'a.service': 'Restart=always', 'b.service': 'Restart=always'}
        h.coverage_delta(state, {'a.service', 'b.service'}, reasons)
        h.coverage_delta(state, {'a.service'},
                         {**reasons, 'b.service': 'EPHEMERAL_JOB (Restart=no)'})
        # b.service is no longer listed by systemd at all.
        self._clear_log()
        h.coverage_delta(state, {'a.service'}, {'a.service': 'Restart=always'})
        self.assertTrue(any('retiring it from the departed list' in ln
                            for ln in self._lines()))
        self.assertEqual(state['coverage_departed'], {})

    def test_total_collapse_to_zero_is_not_reported_as_a_fresh_baseline(self):
        """`state['coverage'] == []` is an EMPTY baseline, not a MISSING one.
        Reading both as falsey made the loudest signal this healer can produce —
        every monitored unit going unreadable at once — print the reassuring
        'coverage baseline recorded (0 monitored)' line on every tick, forever,
        with no departure ever named."""
        state = h.load_state()
        reasons = {'a.service': 'Restart=always', 'b.service': 'Restart=always'}
        h.coverage_delta(state, {'a.service', 'b.service'}, reasons)

        self._clear_log()
        h.coverage_delta(state, set(),
                         {'a.service': 'systemctl show unreadable (timeout/missing)',
                          'b.service': 'systemctl show unreadable (timeout/missing)'})
        lines = self._lines()
        self.assertEqual(len([ln for ln in lines if 'LEFT coverage' in ln]), 2)

        # The tick AFTER the collapse must not re-announce a baseline.
        self._clear_log()
        h.coverage_delta(state, set(),
                         {'a.service': 'systemctl show unreadable (timeout/missing)',
                          'b.service': 'systemctl show unreadable (timeout/missing)'})
        lines = self._lines()
        self.assertFalse(any('coverage baseline' in ln for ln in lines),
                         'an empty baseline must not read as a missing one')
        coverage = [ln for ln in lines if 'coverage=' in ln][0]
        self.assertIn('coverage=(none)', coverage)
        self.assertIn('departed=a.service,b.service', coverage)

    def test_empty_baseline_does_not_spray_departures(self):
        state = h.load_state()
        state['coverage'] = []
        h.coverage_delta(state, {'a.service'}, {'a.service': 'Restart=always'})
        self.assertFalse(any('LEFT coverage' in ln for ln in self._lines()))

    def test_a_daemon_with_a_companion_timer_stays_in_coverage(self):
        """The scenario the TriggeredBy classifier got wrong, which must now be
        a NON-event: the unit stays monitored and is probed."""
        facts = _facts(type_='simple', active='active', pid='4242',
                       restart='always', triggered='ourliberty-beacon-kick.timer')
        with mock.patch.object(
                h, 'list_ourliberty_services',
                return_value=['ourliberty-beacon-bot.service']), \
             mock.patch.object(h, 'unit_facts', return_value=facts), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_OK) as probe:
            h.main()
        probe.assert_called_once()
        self.assertIn('coverage=ourliberty-beacon-bot.service',
                      Path(h.LOG_FILE).read_text())

    def test_every_skip_reason_has_a_distinct_tick_counter(self):
        reasons = [h.OUTCOME_SKIP_ONESHOT, h.OUTCOME_SKIP_EPHEMERAL,
                   h.OUTCOME_SKIP_INACTIVE, h.OUTCOME_SKIP_NOCARVE,
                   h.OUTCOME_SKIP_UNKNOWN, h.OUTCOME_SKIP_NSGONE]
        self.assertEqual(len(set(reasons)), len(reasons))
        for r in reasons:
            self.assertIn(r, h.OUTCOMES)
        self.assertNotIn('skip', h.OUTCOMES,
                         'the old single aggregate skip bucket must be gone')

    def test_tick_line_reports_each_skip_reason_separately(self):
        units = ['ourliberty-oneshot.service', 'ourliberty-nocarve.service',
                 'ourliberty-down.service']

        def fake_facts(unit):
            if unit == 'ourliberty-oneshot.service':
                return _facts(type_='oneshot', restart='no')
            if unit == 'ourliberty-nocarve.service':
                return _facts(rwp='/home/larry/agents', restart='always')
            return _facts(active='inactive', pid='0', restart='always')

        with mock.patch.object(h, 'list_ourliberty_services', return_value=units), \
             mock.patch.object(h, 'unit_facts', side_effect=fake_facts):
            h.main()
        tick = [ln for ln in Path(h.LOG_FILE).read_text().splitlines()
                if 'tick: ' in ln][-1]
        self.assertIn('skip-oneshot=1', tick)
        self.assertIn('skip-nocarve=1', tick)
        self.assertIn('skip-inactive=1', tick)


# -------------------- docs stay in step with the code --------------------

class RunbookConsistencyTests(unittest.TestCase):
    """Docs are the second copy here. Keep them from diverging."""

    def test_every_outcome_is_documented(self):
        text = _RUNBOOK.read_text()
        missing = [o for o in h.OUTCOMES if o not in text]
        self.assertEqual(missing, [], f'undocumented outcomes: {missing}')

    def test_every_alert_subject_is_documented_and_translated(self):
        import json
        text = _RUNBOOK.read_text()
        translations = json.loads(
            (_REPO_ROOT / 'config' / 'alert-translations.json').read_text())
        table = translations['heal-claude-json-bind-drift']
        src = (_REPO_SCRIPTS / 'heal_claude_json_bind_drift.py').read_text()
        for subject in ('rebound', 'still-dangled', 'repair-failed',
                        'probe-blind'):
            with self.subTest(subject=subject):
                # emitted, documented, AND translated — a subject present in
                # only two of the three is either an undocumented page or a
                # translation for a page nothing can send.
                self.assertIn(f'{subject}:', src)
                self.assertIn(subject, text)
                self.assertIn(subject, table)


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

    MONITOR = dict(type_='simple', active='active', pid='111', restart='always')

    def _facts_for(self, **over):
        return _facts(**{**self.MONITOR, **over})

    def test_oneshot_is_skipped_with_its_own_reason(self):
        with mock.patch.object(h, 'unit_facts',
                               return_value=self._facts_for(type_='oneshot')):
            self.assertEqual(h.check_unit(self.UNIT, h.load_state()),
                             h.OUTCOME_SKIP_ONESHOT)

    def test_missing_carveout_is_skipped_with_its_own_reason(self):
        with mock.patch.object(
                h, 'unit_facts',
                return_value=self._facts_for(rwp='/home/larry/agents')):
            self.assertEqual(h.check_unit(self.UNIT, h.load_state()),
                             h.OUTCOME_SKIP_NOCARVE)

    def test_inactive_monitor_is_skipped_with_its_own_reason(self):
        with mock.patch.object(
                h, 'unit_facts',
                return_value=self._facts_for(active='inactive', pid='0')):
            self.assertEqual(h.check_unit(self.UNIT, h.load_state()),
                             h.OUTCOME_SKIP_INACTIVE)

    def test_unreadable_unit_is_skipped_with_its_own_reason(self):
        with mock.patch.object(h, 'systemctl_show', return_value={}):
            self.assertEqual(h.check_unit(self.UNIT, h.load_state()),
                             h.OUTCOME_SKIP_UNKNOWN)

    def test_healthy_when_writable(self):
        with mock.patch.object(h, 'unit_facts', return_value=self._facts_for()), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_OK):
            self.assertEqual(h.check_unit(self.UNIT, h.load_state()),
                             h.OUTCOME_HEALTHY)
        self._m_dm_repaired.assert_not_called()

    def test_probe_error_escalates_once(self):
        state = h.load_state()
        with mock.patch.object(h, 'unit_facts', return_value=self._facts_for()), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_OTHER):
            self.assertEqual(h.check_unit(self.UNIT, state), h.OUTCOME_PROBE_ERROR)
        self._m_dm_probe_blind.assert_called_once()

    def test_process_gone_is_benign_skip_no_dm(self):
        # _PROBE_GONE → its own skip bucket, no probe-blind DM, no escalation-
        # cooldown state write (so a later real fault still alerts).
        state = h.load_state()
        with mock.patch.object(h, 'unit_facts', return_value=self._facts_for()), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_GONE):
            self.assertEqual(h.check_unit(self.UNIT, state), h.OUTCOME_SKIP_NSGONE)
        self._m_dm_probe_blind.assert_not_called()
        self.assertFalse(h.in_alert_cooldown(state, self.UNIT))

    def test_rebound_on_erofs_then_successful_restart(self):
        state = h.load_state()
        # First probe (old pid) → EROFS; reprobe (new pid) → OK.
        probe = mock.Mock(side_effect=[h._PROBE_EROFS, h._PROBE_OK])
        # unit_facts: detection → pid 111, post-restart reverify → pid 222.
        facts = mock.Mock(side_effect=[self._facts_for(),
                                       self._facts_for(pid='222')])
        with mock.patch.object(h, 'unit_facts', facts), \
             mock.patch.object(h, 'probe_namespace_writable', probe), \
             mock.patch.object(h, 'restart_unit', return_value=(0, '')) as r:
            self.assertEqual(h.check_unit(self.UNIT, state), h.OUTCOME_REBOUND)
        r.assert_called_once_with(self.UNIT)
        self._m_dm_repaired.assert_called_once()
        # reverified=True passed through (new namespace probe was OK).
        self.assertTrue(self._m_dm_repaired.call_args.args[1])
        # cooldown stamped so a subsequent tick won't immediately re-restart.
        self.assertTrue(h.in_restart_cooldown(state, self.UNIT))

    def test_repair_failed_when_restart_rc_nonzero(self):
        state = h.load_state()
        with mock.patch.object(h, 'unit_facts', return_value=self._facts_for()), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_EROFS), \
             mock.patch.object(h, 'restart_unit', return_value=(1, 'unit not found')):
            self.assertEqual(h.check_unit(self.UNIT, state),
                             h.OUTCOME_REPAIR_FAILED)
        self._m_dm_repair_failed.assert_called_once()
        self.assertTrue(h.in_restart_cooldown(state, self.UNIT))

    def test_cooldown_dangled_suppresses_restart(self):
        state = h.load_state()
        h.mark_restarted(state, self.UNIT)  # pretend we just restarted it
        with mock.patch.object(h, 'unit_facts', return_value=self._facts_for()), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=h._PROBE_EROFS), \
             mock.patch.object(h, 'restart_unit') as r:
            self.assertEqual(h.check_unit(self.UNIT, state),
                             h.OUTCOME_COOLDOWN_DANGLED)
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


class CoverageStateShapeTests(_IsolatedAgentsRoot):
    """load_state must not default an ABSENT 'coverage' to [] — that would undo
    the missing-vs-empty distinction coverage_delta depends on."""

    def test_absent_coverage_stays_absent(self):
        self.assertNotIn('coverage', h.load_state())

    def test_empty_coverage_is_preserved_as_an_empty_baseline(self):
        Path(h.STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(h.STATE_FILE).write_text('{"services": {}, "coverage": []}')
        self.assertEqual(h.load_state()['coverage'], [])

    def test_corrupt_coverage_is_dropped_not_emptied_and_never_raises(self):
        Path(h.STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(h.STATE_FILE).write_text('{"services": {}, "coverage": 5,'
                                      ' "coverage_departed": ["not-a-dict"]}')
        state = h.load_state()
        self.assertNotIn('coverage', state)
        self.assertEqual(state['coverage_departed'], {})
        # and the tick that reads it does not die
        h.coverage_delta(state, {'a.service'}, {'a.service': 'Restart=always'})
        self.assertTrue(any('coverage baseline' in ln
                            for ln in Path(h.LOG_FILE).read_text().splitlines()))
