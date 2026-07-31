#!/usr/bin/env python3
"""Tests for heal_claude_json_bind_drift.

Covers:
  - the in-namespace write-probe exit-code contract (run for real against a
    writable temp file and a missing path; mapping verified at the
    subprocess boundary for the EROFS case);
  - CLASSIFICATION by `Restart=` (property: the verdict is INVARIANT under
    every TriggeredBy value — that is the old fail-open bug as an invariant);
  - the VERDICT rule table (property over identity x state x pending-job:
    LANDED iff identity changed AND a started state; a pending Job is always
    in-progress; a hung shellout with unchanged identity is NOT_ENQUEUED);
  - the PENDING-VERIFICATION ledger: no combination of post-restart facts may
    produce both "no DM" and "no open obligation" — that combination IS the
    blindness this healer was going permanently blind to;
  - DM honesty: exactly one arm emits healed=True, and only with a confirmed
    new-namespace probe;
  - ephemeral units are PROBED but structurally cannot be restarted;
  - coverage-delta observability (a unit leaving coverage is never silent);
  - the multi-unit tick budget under a virtual clock, at several shellout
    latencies, against the real TimeoutStartSec in systemd/;
  - a static config-lint of the repo systemd units enforcing the #470 invariant
    plus the new Restart=-declaration invariant.

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
import itertools
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
import restart_guard as rg  # noqa: E402

_REPO_ROOT = _REPO_SCRIPTS.parent
_SYSTEMD_DIR = _REPO_ROOT / 'systemd'
_RUNBOOK = _REPO_ROOT / 'runbooks' / 'claude-json-bind-drift.md'

CLAUDE_JSON = '/home/larry/.claude.json'


def _props(type_='simple', active='active', pid='4242',
           rwp=f'/home/larry/.claude {CLAUDE_JSON}', restart='always',
           triggered='', invocation='inv-old') -> dict:
    """A full `systemctl show` property set, in the shape the healer asks for."""
    out = {
        'Type': type_, 'ActiveState': active, 'MainPID': pid,
        'ReadWritePaths': rwp, 'TriggeredBy': triggered,
    }
    if restart is not None:
        out['Restart'] = restart
    if invocation is not None:
        out['InvocationID'] = invocation
    return out


def _facts(**kw) -> h.UnitFacts:
    """Build UnitFacts through the REAL parser (unit_facts) without patching —
    nesting a patch inside a patched unit_facts recurses."""
    unit = kw.pop('unit', 'ourliberty-beacon-bot.service')
    props = _props(**kw)
    pid = None
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
        invocation_id=props.get('InvocationID'),
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

    # -- helpers --
    def subjects(self):
        return [c.get('subject') for c in self.calls]

    def with_subject_prefix(self, prefix):
        return [c for c in self.calls
                if (c.get('subject') or '').startswith(prefix)]


class _IsolatedAgentsRoot(unittest.TestCase):
    """Redirect OURLIBERTY_AGENTS_ROOT to a fresh tmp dir per test so the
    healer's STATE_FILE / LOG_FILE / HEARTBEAT_FILE / KILL_SWITCH derive from a
    throwaway tree."""

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
            self.assertEqual(Path(path).read_bytes(), before)
            self.assertEqual(os.stat(path).st_mtime, before_mtime)
        finally:
            os.unlink(path)

    def test_missing_path_exits_other_not_erofs(self):
        result = self._run('/no/such/claude.json/here')
        self.assertEqual(result.returncode, h._PROBE_OTHER)
        self.assertNotEqual(result.returncode, h._PROBE_EROFS)

    def test_snippet_opens_rdwr_without_create_or_truncate(self):
        self.assertIn('os.O_RDWR', h._PROBE_SNIPPET)
        self.assertNotIn('O_CREAT', h._PROBE_SNIPPET)
        self.assertNotIn('O_TRUNC', h._PROBE_SNIPPET)
        self.assertIn('errno.EROFS', h._PROBE_SNIPPET)


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
        with mock.patch.object(h.subprocess, 'run', self._mock_run(3, stderr='2:No such file')), \
             mock.patch.object(h, '_namespace_probeable', return_value=True):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_OTHER)

    def test_sudo_rejection_rc1_is_other_never_erofs(self):
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
        stderr = 'nsenter: cannot open /proc/123/ns/mnt: No such file or directory'
        with mock.patch.object(h.subprocess, 'run', self._mock_run(1, stderr=stderr)), \
             mock.patch.object(h, '_namespace_probeable', return_value=False):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_GONE)

    def test_genuine_blind_namespace_present_sudo_rejected_is_other(self):
        with mock.patch.object(
                h.subprocess, 'run',
                self._mock_run(1, stderr='sudo: a password is required')), \
             mock.patch.object(h, '_namespace_probeable', return_value=True):
            self.assertEqual(h.probe_namespace_writable(123), h._PROBE_OTHER)


class NamespaceProbeableTests(unittest.TestCase):
    def test_pid_far_above_pid_max_is_not_probeable(self):
        self.assertFalse(h._namespace_probeable(2_000_000_123))

    def test_zombie_proc_dir_present_but_ns_mnt_gone_is_not_probeable(self):
        real_exists = os.path.exists

        def fake_exists(p):
            if p == '/proc/424242':
                return True
            if p == '/proc/424242/ns/mnt':
                return False
            return real_exists(p)

        with mock.patch.object(h.os.path, 'exists', side_effect=fake_exists):
            self.assertFalse(h._namespace_probeable(424242))
            self.assertTrue(h.os.path.exists('/proc/424242'))


class CarveoutPresentTests(unittest.TestCase):
    def test_exact_token_matches(self):
        self.assertTrue(h.carveout_present(
            f'/home/larry/agents /home/larry/.claude {CLAUDE_JSON} /tmp'))

    def test_absent_returns_false(self):
        self.assertFalse(h.carveout_present('/home/larry/agents /home/larry/.claude'))

    def test_substring_does_not_false_match(self):
        self.assertFalse(h.carveout_present('/home/larry/.claude.json.bak'))

    def test_empty_is_false(self):
        self.assertFalse(h.carveout_present(''))


# -------------------- classification: Restart=, not TriggeredBy --------------------

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
        ('cycle: Restart=no + timer',
         dict(restart='no'), 'ephemeral-job'),
        ('Restart=no, no timer',
         dict(restart='no'), 'ephemeral-job'),
        ('beacon: Restart=always, no timer',
         dict(restart='always'), 'monitor'),
        ('beacon + companion kick timer',
         dict(restart='always'), 'monitor'),
        ('outbox-notifier: Restart=on-failure + After=',
         dict(restart='on-failure'), 'monitor'),
        ('oneshot healer + timer',
         dict(type_='oneshot', restart='no'), 'skip-oneshot'),
        ('Restart= empty (systemd default shape)',
         dict(restart=''), 'ephemeral-job'),
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
        against a build that lacks them (a class-body reference to a missing
        symbol collapses every test in the file into one collection error, which
        proves nothing about any of them). This is the one place the literals
        are tied back to the constants, so a rename is a single loud failure
        rather than a table that silently stops asserting."""
        self.assertEqual(h.CLASS_EPHEMERAL, 'ephemeral-job')
        self.assertEqual(h.CLASS_MONITOR, 'monitor')
        self.assertEqual(h.CLASS_SKIP_ONESHOT, 'skip-oneshot')
        self.assertEqual(h.CLASS_SKIP_UNKNOWN, 'skip-unknown')

    def test_disabled_timer_repro_cycle_is_still_ephemeral(self):
        """The repro that motivated the finding: cycle mid-fire with its timer
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
        self.assertIn('InvocationID', m.call_args.args[1])
        _, reason = h.classify_unit(_facts(restart='always',
                                           triggered='ourliberty-x.timer'))
        self.assertIn('TriggeredBy=ourliberty-x.timer', reason)

    def test_unit_facts_parses_the_real_property_dump(self):
        with mock.patch.object(
                h, 'systemctl_show',
                return_value=_props(active='active', pid='4242',
                                    restart='on-failure', invocation='abc123')):
            facts = h.unit_facts('ourliberty-inbox-watcher.service')
        self.assertTrue(facts.present)
        self.assertEqual(facts.main_pid, 4242)
        self.assertEqual(facts.restart_policy, 'on-failure')
        self.assertEqual(facts.invocation_id, 'abc123')
        self.assertTrue(h.carveout_present(facts.read_write_paths))

    def test_unit_facts_has_no_active_only_filter(self):
        """The old blindness was an accessor that returned None for any unit
        whose ActiveState != 'active'. A failed unit must stay READABLE, or a
        restart that never landed is invisible to every later tick."""
        with mock.patch.object(
                h, 'systemctl_show',
                return_value=_props(active='failed', pid='0', invocation='')):
            facts = h.unit_facts('ourliberty-x.service')
        self.assertTrue(facts.present)
        self.assertEqual(facts.active_state, 'failed')
        self.assertIsNone(facts.main_pid)
        self.assertFalse(hasattr(h, 'target_main_pid'),
                         'target_main_pid was the active-only filter that made a '
                         'non-landing restart permanently invisible; it must not '
                         'come back')


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
            text = svc.read_text()
            type_ = ''
            restart = None
            carves = False
            for line in text.splitlines():
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
                 'now classifies them EPHEMERAL and stops repairing them. Add '
                 'Restart=always|on-failure, or add the unit to '
                 f'KNOWN_EPHEMERAL_UNITS with a reason: {offenders}'))



# -------------------- verdict rule table --------------------

class VerifyVerdictTests(unittest.TestCase):
    PRIOR_INV = 'inv-old'
    PRIOR_PID = 111

    def _f(self, active='active', invocation='inv-old', pid='111', present=True):
        if not present:
            return h.UnitFacts('u', False, '', 'unknown', None, '', None, '', None)
        return _facts(active=active, invocation=invocation, pid=pid)

    # Written out literally, NOT read from h._RESTART_STARTED_STATES: an
    # expectation derived from the constant under test moves with the constant
    # and can never catch it changing.
    STARTED_STATES = ('active', 'activating', 'reloading')

    def test_property_landed_iff_identity_changed_and_started_state(self):
        """PROPERTY over identity x state x job. LANDED if and only if the
        identity CHANGED and the unit is in a started state — is-active alone
        (which reads 'active' for the OLD process too) can never conclude."""
        invocations = {
            'unchanged': 'inv-old',
            'changed': 'inv-new',
            'empty-after': '',
        }
        states = ['active', 'activating', 'reloading', 'deactivating',
                  'inactive', 'failed', 'unknown']
        for inv_label, inv in invocations.items():
            for state in states:
                for job in ('', '4242 start'):
                    with self.subTest(inv=inv_label, state=state, job=bool(job)):
                        facts = self._f(active=state, invocation=inv)
                        verdict, _ = h.verify_verdict(
                            self.PRIOR_INV, self.PRIOR_PID, facts, job, False)
                        expected_landed = (
                            inv_label == 'changed'
                            and state in self.STARTED_STATES)
                        self.assertEqual(
                            verdict == h.VERDICT_LANDED, expected_landed)

    def test_unreadable_identity_can_never_be_landed(self):
        for state in ('active', 'activating', 'inactive', 'failed'):
            with self.subTest(state=state):
                # (a) systemctl show unreadable post-restart
                verdict, _ = h.verify_verdict(
                    self.PRIOR_INV, self.PRIOR_PID, self._f(present=False), '', False)
                self.assertEqual(verdict, h.VERDICT_IN_PROGRESS)
                # (b) InvocationID property missing post-restart
                facts = _facts(active=state, invocation=None)
                verdict, _ = h.verify_verdict(
                    self.PRIOR_INV, self.PRIOR_PID, facts, '', False)
                self.assertNotEqual(verdict, h.VERDICT_LANDED)

    def test_no_identity_captured_before_the_restart_can_never_be_landed(self):
        """Both InvocationID and MainPID unreadable pre-restart (systemctl_show
        returned {}) — fail-safe as an explicit branch, not branch order."""
        for state in ('active', 'activating'):
            facts = _facts(active=state, invocation='inv-new', pid='999')
            verdict, _ = h.verify_verdict('', None, facts, '', False)
            self.assertEqual(verdict, h.VERDICT_IN_PROGRESS)

    def test_pending_job_refuses_a_failure_verdict(self):
        """The After=-queued peer: 'inactive' with a job enqueued, for the whole
        window. Never a failure, at any duration."""
        facts = self._f(active='inactive', invocation='', pid='0')
        verdict, detail = h.verify_verdict(
            self.PRIOR_INV, self.PRIOR_PID, facts, '17 start', False)
        self.assertEqual(verdict, h.VERDICT_IN_PROGRESS)
        self.assertIn('17 start', detail)

    def test_pending_job_gone_and_identity_unchanged_is_not_success(self):
        """A displaced/cancelled job: 'deactivating' the whole window, identity
        unchanged, Job EMPTY. Must be in-progress (deferred), never a rebound."""
        facts = self._f(active='deactivating', invocation='inv-old')
        verdict, _ = h.verify_verdict(
            self.PRIOR_INV, self.PRIOR_PID, facts, '', False)
        self.assertEqual(verdict, h.VERDICT_IN_PROGRESS)
        self.assertNotEqual(verdict, h.VERDICT_LANDED)

    def test_a_unit_systemd_is_still_working_on_is_never_not_enqueued(self):
        """PROPERTY over the states that mean 'systemd is demonstrably still
        executing on this unit', with the ONE input combination that makes them
        load-bearing: identity UNCHANGED, Job EMPTY, and the restart shellout
        HUNG.

        Every other combination reaches in-progress by some other rule, so only
        this one discriminates. Without it, deleting a single member from
        _RESTART_IN_PROGRESS_STATES falls through to the not-enqueued rule and
        pages `repair-not-enqueued:` for a unit that is merely still draining —
        which is the exact false-page class this whole change exists to remove.

        The state list is written out LITERALLY on purpose: reading it from
        h._RESTART_IN_PROGRESS_STATES would shrink with the mutation and assert
        nothing."""
        for state in ('deactivating', 'activating', 'reloading'):
            with self.subTest(state=state):
                facts = self._f(active=state, invocation='inv-old')
                verdict, detail = h.verify_verdict(
                    self.PRIOR_INV, self.PRIOR_PID, facts, '', True)
                self.assertEqual(
                    verdict, h.VERDICT_IN_PROGRESS,
                    msg=f'{state!r} + hung shellout resolved as {verdict}: {detail}')
                self.assertNotEqual(verdict, h.VERDICT_NOT_ENQUEUED)

    def test_not_enqueued_requires_a_settled_state(self):
        """The other half of the same boundary: not-enqueued is reserved for a
        unit systemd is demonstrably NOT working on. Pin the states that may
        produce it, so the rule cannot quietly widen either."""
        may_not_enqueue = set()
        for state in ('active', 'activating', 'reloading', 'deactivating',
                      'inactive', 'failed'):
            facts = self._f(active=state, invocation='inv-old')
            verdict, _ = h.verify_verdict(
                self.PRIOR_INV, self.PRIOR_PID, facts, '', True)
            if verdict == h.VERDICT_NOT_ENQUEUED:
                may_not_enqueue.add(state)
        self.assertEqual(may_not_enqueue, {'active', 'inactive', 'failed'})

    def test_hung_shellout_with_unchanged_identity_is_not_enqueued(self):
        facts = self._f(active='active', invocation='inv-old')
        verdict, detail = h.verify_verdict(
            self.PRIOR_INV, self.PRIOR_PID, facts, '', True)
        self.assertEqual(verdict, h.VERDICT_NOT_ENQUEUED)
        self.assertIn('hung', detail)

    def test_hung_shellout_but_a_job_appeared_is_in_progress(self):
        facts = self._f(active='inactive', invocation='', pid='0')
        verdict, _ = h.verify_verdict(
            self.PRIOR_INV, self.PRIOR_PID, facts, '9 start', True)
        self.assertEqual(verdict, h.VERDICT_IN_PROGRESS)

    def test_pid_fallback_when_invocation_unreadable_before_restart(self):
        facts = _facts(active='active', invocation=None, pid='222')
        verdict, _ = h.verify_verdict('', 111, facts, '', False)
        self.assertEqual(verdict, h.VERDICT_LANDED)
        facts_same = _facts(active='active', invocation=None, pid='111')
        verdict, _ = h.verify_verdict('', 111, facts_same, '', False)
        self.assertEqual(verdict, h.VERDICT_IN_PROGRESS)


class UnitPendingJobTests(unittest.TestCase):
    def test_parses_a_pending_job(self):
        with mock.patch.object(
                h.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, 'Job=17 start\n', '')):
            self.assertEqual(h.unit_pending_job('u.service'), '17 start')

    def test_empty_job_is_empty_string(self):
        with mock.patch.object(
                h.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, 'Job=\n', '')):
            self.assertEqual(h.unit_pending_job('u.service'), '')

    def test_failsafe_on_timeout_missing_binary_and_garbage(self):
        """Fail-safe direction: an unreadable probe returns '' so it can never
        manufacture a false in-progress verdict that suppresses a real failure."""
        cases = [
            subprocess.TimeoutExpired('systemctl', 10),
            FileNotFoundError(),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(h.subprocess, 'run', side_effect=exc):
                    self.assertEqual(h.unit_pending_job('u.service'), '')
        with mock.patch.object(
                h.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, 'Nope=1\n', '')):
            self.assertEqual(h.unit_pending_job('u.service'), '')


class RestartAndVerifyTests(unittest.TestCase):
    """The poll around the rule table: fast path, window, and the reserved
    up-front-rejection path."""

    def setUp(self):
        super().setUp()
        self._now = [0.0]
        p = mock.patch.object(
            h.time, 'sleep',
            side_effect=lambda s: self._now.__setitem__(0, self._now[0] + s))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(h.time, 'monotonic', side_effect=lambda: self._now[0])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(
            h.subprocess, 'run',
            return_value=subprocess.CompletedProcess([], 0, '', ''))
        p.start()
        self.addCleanup(p.stop)

    def test_fast_path_lands_after_exactly_one_sample(self):
        with mock.patch.object(h, 'unit_facts',
                               return_value=_facts(active='active', invocation='inv-new')), \
             mock.patch.object(h, 'unit_pending_job', return_value=''):
            outcome = h.restart_and_verify('ourliberty-beacon-bot.service',
                                           'inv-old', 111)
        self.assertEqual(outcome.verdict, h.VERDICT_LANDED)
        self.assertEqual(outcome.samples, 1)

    def test_after_ordered_peer_never_produces_a_failure_at_any_drain_length(self):
        """PROPERTY over ordered pairs: for every predecessor drain length, a
        successor reading 'inactive' WITH a pending job is never a failure."""
        for drain_s in (5, 30, 60, 90, 200):
            with self.subTest(drain_s=drain_s):
                self._now[0] = 0.0
                with mock.patch.object(
                        h, 'unit_facts',
                        return_value=_facts(active='inactive', invocation='', pid='0')), \
                     mock.patch.object(h, 'unit_pending_job', return_value='17 start'):
                    outcome = h.restart_and_verify(
                        'ourliberty-outbox-notifier.service', 'inv-old', 111)
                self.assertEqual(outcome.verdict, h.VERDICT_IN_PROGRESS)
                self.assertNotEqual(outcome.verdict, h.VERDICT_REJECTED)

    def test_never_starts_and_no_job_is_in_progress_not_a_page(self):
        """Window expiry is NOT a verdict any more: it defers to the ledger."""
        with mock.patch.object(
                h, 'unit_facts',
                return_value=_facts(active='inactive', invocation='', pid='0')), \
             mock.patch.object(h, 'unit_pending_job', return_value=''):
            outcome = h.restart_and_verify('ourliberty-broken.service', 'inv-old', 111)
        self.assertEqual(outcome.verdict, h.VERDICT_IN_PROGRESS)
        self.assertGreaterEqual(self._now[0], h.VERIFY_WINDOW_S)

    def test_a_draining_unit_with_a_hung_shellout_defers_and_does_not_page(self):
        """End-to-end at the poll, not just the rule table: the restart shellout
        hangs (TimeoutExpired) while the unit is still SIGTERM-draining with no
        job visible yet. `deactivating` is what a claude-loop unit reads for ~90s
        after a restart, so this is the ordinary case, not the exception — and
        NOT_ENQUEUED here would DM `repair-not-enqueued:` and close the
        obligation, i.e. page for a healthy drain and then stop watching."""
        for state in ('deactivating', 'activating', 'reloading'):
            with self.subTest(state=state):
                self._now[0] = 0.0
                with mock.patch.object(
                        h.subprocess, 'run',
                        side_effect=subprocess.TimeoutExpired('systemctl', 30)), \
                     mock.patch.object(
                        h, 'unit_facts',
                        return_value=_facts(active=state, invocation='inv-old')), \
                     mock.patch.object(h, 'unit_pending_job', return_value=''):
                    outcome = h.restart_and_verify(
                        'ourliberty-inbox-watcher.service', 'inv-old', 111)
                self.assertEqual(outcome.verdict, h.VERDICT_IN_PROGRESS,
                                 msg=outcome.detail)
                self.assertNotEqual(outcome.verdict, h.VERDICT_NOT_ENQUEUED)

    def test_rejected_only_from_upfront_rc_or_missing_binary(self):
        with mock.patch.object(
                h.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 1, '', 'unit not found')), \
             mock.patch.object(h, 'unit_facts') as uf:
            outcome = h.restart_and_verify('nope.service', 'inv-old', 111)
        self.assertEqual(outcome.verdict, h.VERDICT_REJECTED)
        uf.assert_not_called()
        with mock.patch.object(h.subprocess, 'run', side_effect=FileNotFoundError()):
            outcome = h.restart_and_verify('nope.service', 'inv-old', 111)
        self.assertEqual(outcome.verdict, h.VERDICT_REJECTED)

    def test_rejected_is_unreachable_from_every_state_job_combination(self):
        for state in ('active', 'activating', 'reloading', 'deactivating',
                      'inactive', 'failed', 'unknown'):
            for inv in ('inv-old', 'inv-new', ''):
                for job in ('', '17 start'):
                    with self.subTest(state=state, inv=inv, job=bool(job)):
                        self._now[0] = 0.0
                        with mock.patch.object(
                                h, 'unit_facts',
                                return_value=_facts(active=state, invocation=inv)), \
                             mock.patch.object(h, 'unit_pending_job', return_value=job):
                            outcome = h.restart_and_verify('u.service', 'inv-old', 111)
                        self.assertNotEqual(outcome.verdict, h.VERDICT_REJECTED)


# -------------------- pending ledger + reconciliation --------------------

class _RepairFixture(_IsolatedAgentsRoot):
    UNIT = 'ourliberty-beacon-bot.service'

    def setUp(self):
        super().setUp()
        self.alerts = self.capture_alerts()
        # No real sleeping / no real cordon.
        p = mock.patch.object(h.time, 'sleep', lambda s: None)
        p.start()
        self.addCleanup(p.stop)

    def _repair(self, state, *, verdict, detail='d', new_facts=None,
                reprobe=h._PROBE_OK, facts=None, now=None):
        facts = facts or _facts(invocation='inv-old', pid='111')
        outcome = h.RestartOutcome(verdict, detail, 1, new_facts)
        # repair_unit re-reads the unit before restarting (see
        # reconfirm_before_restart). Return the SAME invocation, so the
        # pre-flight is a pass-through and these tests keep testing the
        # post-restart verdict space they were written for. The pre-flight's own
        # behaviour is covered by PreflightReconfirmTests.
        with mock.patch.object(h, 'unit_facts', return_value=facts), \
             mock.patch.object(h, 'restart_and_verify', return_value=outcome), \
             mock.patch.object(h, 'probe_namespace_writable', return_value=reprobe):
            return h.repair_unit(self.UNIT, facts, state, now=now)


class PendingLedgerTests(_RepairFixture):

    def test_property_no_dm_implies_an_open_obligation(self):
        """PROPERTY over the full post-restart state space. For every
        (post-state x identity x job), the healer either emits a healed=True
        record ONLY when a new-namespace probe returned _PROBE_OK, or leaves an
        OPEN pending entry. No combination may produce BOTH no-DM and
        no-pending — that combination is the blindness, as an invariant."""
        states = ['active', 'activating', 'reloading', 'deactivating',
                  'inactive', 'failed', 'unknown']
        invocations = ['inv-new', 'inv-old', None]
        jobs = ['', '17 start']
        for state, inv, job in itertools.product(states, invocations, jobs):
            for reprobe in (h._PROBE_OK, h._PROBE_EROFS, h._PROBE_GONE):
                with self.subTest(state=state, inv=inv, job=bool(job),
                                  reprobe=reprobe):
                    self.alerts.calls.clear()
                    # Each scenario is INDEPENDENT. Without this, a
                    # last_alert_ts persisted by an earlier subTest puts the
                    # next one inside the escalation window and suppresses its
                    # DM, and the property degrades into "the first iteration
                    # passed". (TickBudgetTests unlinks for the same reason.)
                    # Suppression is real behaviour and gets its own test:
                    # test_a_suppressed_still_dangled_dm_is_still_revisited.
                    try:
                        Path(h.STATE_FILE).unlink()
                    except OSError:
                        pass
                    state_d = h.load_state()
                    post = _facts(active=state, invocation=inv, pid='222')
                    # First read is repair_unit's pre-flight re-confirm (same
                    # invocation as detection → pass-through); every read after
                    # it is the POST-restart fact set this property is about.
                    pre = _facts(active='active', invocation='inv-old', pid='111')
                    reads = itertools.chain([pre], itertools.repeat(post))
                    with mock.patch.object(h, 'unit_facts',
                                           side_effect=lambda _u: next(reads)), \
                         mock.patch.object(h, 'unit_pending_job', return_value=job), \
                         mock.patch.object(
                             h.subprocess, 'run',
                             return_value=subprocess.CompletedProcess([], 0, '', '')), \
                         mock.patch.object(h, 'probe_namespace_writable',
                                           return_value=reprobe), \
                         mock.patch.object(h.time, 'monotonic',
                                           side_effect=[0.0, 99.0, 99.0]):
                        h.repair_unit(self.UNIT, _facts(invocation='inv-old',
                                                        pid='111'), state_d)
                    healed = [c for c in self.alerts.calls
                              if (c.get('subject') or '').startswith('rebound:')]
                    if healed:
                        self.assertEqual(reprobe, h._PROBE_OK)
                    open_obligation = h.pending_entry(state_d, self.UNIT) is not None
                    self.assertTrue(
                        bool(self.alerts.calls) or open_obligation,
                        'no DM AND no pending obligation — that is the blindness')

    def test_multi_tick_blindness_regression(self):
        """The exact traced failure. tick1: stuck 'deactivating' — no rebound DM,
        pending entry written. tick2: 'failed' — still pending, no DM. tick3:
        past the landing grace — exactly one repair-did-not-land, routed
        escalate."""
        state = h.load_state()
        outcome = self._repair(state, verdict=h.VERDICT_IN_PROGRESS,
                               detail="unit is 'deactivating'", now=1000.0)
        self.assertEqual(outcome, h.OUTCOME_RESTART_IN_PROGRESS)
        self.assertEqual(self.alerts.calls, [])
        self.assertIsNotNone(h.pending_entry(state, self.UNIT))

        # tick 2 — the unit settled 'failed'. Still inside the grace window.
        with mock.patch.object(h, 'unit_facts',
                               return_value=_facts(active='failed', pid='0',
                                                   invocation='')):
            res = h.reconcile_pending(state, {self.UNIT}, now=1060.0)
        self.assertEqual(res[self.UNIT], h.OUTCOME_AWAITING_VERIFY)
        self.assertEqual(self.alerts.calls, [])
        self.assertIsNotNone(h.pending_entry(state, self.UNIT))

        # tick 3 — past RESTART_LANDING_GRACE_S.
        with mock.patch.object(h, 'unit_facts',
                               return_value=_facts(active='failed', pid='0',
                                                   invocation='')):
            res = h.reconcile_pending(
                state, {self.UNIT}, now=1000.0 + h.RESTART_LANDING_GRACE_S + 1)
        self.assertEqual(res[self.UNIT], h.OUTCOME_REPAIR_DID_NOT_LAND)
        did_not_land = self.alerts.with_subject_prefix('repair-did-not-land:')
        self.assertEqual(len(did_not_land), 1)
        self.assertNotEqual(did_not_land[0].get('route'), 'digest')
        self.assertEqual(
            larry_alerts.classify_route('heal-claude-json-bind-drift',
                                        did_not_land[0]['subject'], healed=False),
            'escalate')
        self.assertIsNone(h.pending_entry(state, self.UNIT))

    def test_reconcile_reads_the_unit_directly_not_through_an_active_only_filter(self):
        """Guard against the fix regressing into the same blindness: even with
        every active-only accessor stubbed to None, reconciliation resolves a
        unit that came back."""
        state = h.load_state()
        h.open_pending(state, self.UNIT, 'inv-old', 111, now=500.0)
        stub = mock.Mock(return_value=None)
        with mock.patch.object(h, 'unit_facts',
                               return_value=_facts(active='active', pid='222',
                                                   invocation='inv-new')), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_OK), \
             mock.patch.object(h, 'unit_active_state', stub):
            res = h.reconcile_pending(state, {self.UNIT}, now=520.0)
        self.assertEqual(res[self.UNIT], h.OUTCOME_REBOUND)
        stub.assert_not_called()

    def test_pending_entry_is_durable_across_a_mid_restart_abort(self):
        """The entry is written and PERSISTED before the restart, so a process
        killed mid-restart still leaves a reconcilable obligation."""
        state = h.load_state()

        class _Boom(Exception):
            pass

        prior = _facts(invocation='inv-old', pid='111')
        with mock.patch.object(h, 'unit_facts', return_value=prior), \
             mock.patch.object(h, 'restart_and_verify', side_effect=_Boom), \
             self.assertRaises(_Boom):
            h.repair_unit(self.UNIT, prior, state)
        # Fresh module instance reading the same on-disk state file.
        reloaded = h.load_state()
        entry = h.pending_entry(reloaded, self.UNIT)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['prior_invocation'], 'inv-old')
        self.assertEqual(entry['prior_pid'], 111)

    def test_gc_drops_a_pending_entry_for_a_vanished_unit(self):
        state = h.load_state()
        h.open_pending(state, 'ourliberty-gone.service', 'inv', 1, now=10.0)
        h.save_state(state)
        before = h.STATE_FILE.read_text()
        res = h.reconcile_pending(state, {'ourliberty-other.service'}, now=20.0)
        self.assertEqual(res['ourliberty-gone.service'], h.OUTCOME_PENDING_GC)
        self.assertEqual(self.alerts.calls, [])
        self.assertEqual(h.load_state()['pending'], {})
        self.assertLess(len(h.STATE_FILE.read_text()), len(before))

    def test_unit_with_an_open_pending_entry_is_skipped_by_the_ordinary_path(self):
        """One DM per repair even when the unit probes EROFS again mid-verify."""
        state = h.load_state()
        h.open_pending(state, self.UNIT, 'inv-old', 111, now=10.0)
        with mock.patch.object(h, 'unit_facts') as uf, \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_EROFS) as probe, \
             mock.patch.object(h, 'restart_and_verify') as restart:
            outcome = h.check_unit(self.UNIT, state)
        self.assertEqual(outcome, h.OUTCOME_AWAITING_VERIFY)
        uf.assert_not_called()
        probe.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(self.alerts.calls, [])

    def test_peer_restart_twin_closes_as_rebound_without_a_page(self):
        """A stranger (systemd Restart=always, watchdog, medic) restarted the
        unit. The invocation changed for a reason that is not us — accept it:
        the mount is rebound either way and the namespace probe is the proof."""
        state = h.load_state()
        h.open_pending(state, self.UNIT, 'inv-old', 111, now=100.0)
        with mock.patch.object(h, 'unit_facts',
                               return_value=_facts(active='active', pid='777',
                                                   invocation='inv-stranger')), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_OK):
            res = h.reconcile_pending(state, {self.UNIT}, now=140.0)
        self.assertEqual(res[self.UNIT], h.OUTCOME_REBOUND)
        self.assertEqual(len(self.alerts.with_subject_prefix('rebound:')), 1)
        self.assertEqual(self.alerts.with_subject_prefix('repair-'), [])
        self.assertIsNone(h.pending_entry(state, self.UNIT))

    def test_not_enqueued_opens_no_obligation_and_burns_no_cooldown(self):
        """The traced hung-sudo repro. Nothing was restarted, so: a
        repair-not-enqueued escalation, no rebound, no cooldown, no pending —
        and the next tick is free to try again."""
        state = h.load_state()
        outcome = self._repair(state, verdict=h.VERDICT_NOT_ENQUEUED,
                               detail='the restart command itself hung',
                               now=1000.0)
        self.assertEqual(outcome, h.OUTCOME_REPAIR_NOT_ENQUEUED)
        self.assertEqual(len(self.alerts.with_subject_prefix('repair-not-enqueued:')), 1)
        self.assertEqual(self.alerts.with_subject_prefix('rebound:'), [])
        self.assertFalse(h.in_restart_cooldown(state, self.UNIT, now=1001.0))
        self.assertIsNone(h.pending_entry(state, self.UNIT))
        # …and the next tick can attempt the repair again.
        with mock.patch.object(h, 'unit_facts',
                               return_value=_facts(invocation='inv-old', pid='111')), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_EROFS), \
             mock.patch.object(h, 'restart_and_verify',
                               return_value=h.RestartOutcome(
                                   h.VERDICT_IN_PROGRESS, 'd', 1, None)) as again:
            h.check_unit(self.UNIT, state, now=1002.0)
        again.assert_called_once()

    def test_not_enqueued_never_reaches_dm_still_dangled(self):
        """Wrong-investigation guard: the still-dangled body sends Larry to
        `grep ReadWritePaths`, which is the wrong path for a wedged sudo/dbus."""
        state = h.load_state()
        with mock.patch.object(h, 'dm_still_dangled') as still:
            self._repair(state, verdict=h.VERDICT_NOT_ENQUEUED, now=5.0)
        still.assert_not_called()

    def test_rejected_burns_the_cooldown_and_pages_repair_failed(self):
        state = h.load_state()
        outcome = self._repair(state, verdict=h.VERDICT_REJECTED,
                               detail='unit not found', now=2000.0)
        self.assertEqual(outcome, h.OUTCOME_REPAIR_FAILED)
        self.assertEqual(len(self.alerts.with_subject_prefix('repair-failed:')), 1)
        self.assertTrue(h.in_restart_cooldown(state, self.UNIT, now=2001.0))
        self.assertIsNone(h.pending_entry(state, self.UNIT))


# -------------------- DM honesty --------------------

class DmHonestyTests(_RepairFixture):

    @staticmethod
    def _reprobe_arms():
        """Built at CALL time, not class-definition time: a class-body reference
        to a module symbol makes this file unimportable against a build that
        lacks it, which turns every test here into one collection error."""
        return [
            ('new pid + OK', h._PROBE_OK, True),
            ('new pid + EROFS', h._PROBE_EROFS, False),
            ('new pid + OTHER', h._PROBE_OTHER, False),
            ('new pid + GONE', h._PROBE_GONE, False),
        ]

    def test_exactly_one_reprobe_arm_emits_healed_true(self):
        healed_arms = []
        for label, probe, _ in self._reprobe_arms():
            with self.subTest(arm=label):
                self.alerts.calls.clear()
                state = h.load_state()
                self._repair(state, verdict=h.VERDICT_LANDED,
                             new_facts=_facts(active='active', pid='222',
                                              invocation='inv-new'),
                             reprobe=probe)
                if self.alerts.with_subject_prefix('rebound:'):
                    healed_arms.append(label)
        self.assertEqual(healed_arms, ['new pid + OK'])

    def test_no_dm_body_claims_something_the_code_did_not_observe(self):
        """Mechanical half: the body may not assert the unit is active unless
        the fake systemd reported ActiveState=='active' in that scenario."""
        for state_str in ('deactivating', 'inactive', 'failed'):
            with self.subTest(state=state_str):
                self.alerts.calls.clear()
                state = h.load_state()
                self._repair(state, verdict=h.VERDICT_IN_PROGRESS,
                             detail=f'unit is {state_str!r}')
                for call in self.alerts.calls:
                    self.assertNotIn('active again', call.get('message', ''))
                    self.assertNotIn('is active', call.get('message', ''))

    def test_proven_failed_repair_is_still_dangled_not_rebound(self):
        """Variant C: the re-probe CONCLUDED EROFS. The old code called that
        'inconclusive' and reported it as an auto-repair success."""
        state = h.load_state()
        with mock.patch.object(h, 'dm_rebound') as rebound:
            outcome = self._repair(
                state, verdict=h.VERDICT_LANDED,
                new_facts=_facts(active='active', pid='222', invocation='inv-new'),
                reprobe=h._PROBE_EROFS)
        self.assertEqual(outcome, h.OUTCOME_STILL_DANGLED)
        rebound.assert_not_called()
        calls = self.alerts.with_subject_prefix('still-dangled:')
        self.assertEqual(len(calls), 1)
        self.assertIsNot(calls[0].get('healed'), True)
        self.assertEqual(
            larry_alerts.classify_route('heal-claude-json-bind-drift',
                                        calls[0]['subject'], healed=False),
            'escalate')

    def test_inconclusive_arms_emit_zero_dms_and_leave_an_open_obligation(self):
        """Variants A/B/D. BOTH halves matter — the no-DM half alone would be
        the old blindness."""
        for label, probe in (('probe gone', h._PROBE_GONE),
                             ('probe blind', h._PROBE_OTHER)):
            with self.subTest(arm=label):
                self.alerts.calls.clear()
                state = h.load_state()
                outcome = self._repair(
                    state, verdict=h.VERDICT_LANDED,
                    new_facts=_facts(active='active', pid='222',
                                     invocation='inv-new'),
                    reprobe=probe)
                self.assertEqual(outcome, h.OUTCOME_AWAITING_VERIFY)
                self.assertEqual(self.alerts.calls, [])
                self.assertIsNotNone(h.pending_entry(state, self.UNIT))
        # …and the 'no new MainPID' arm (unit not active post-restart).
        self.alerts.calls.clear()
        state = h.load_state()
        outcome = self._repair(
            state, verdict=h.VERDICT_LANDED,
            new_facts=_facts(active='activating', pid='0', invocation='inv-new'))
        self.assertEqual(outcome, h.OUTCOME_AWAITING_VERIFY)
        self.assertEqual(self.alerts.calls, [])
        self.assertIsNotNone(h.pending_entry(state, self.UNIT))

    def test_source_never_regains_the_hardcoded_active_again_claim(self):
        src = (_REPO_SCRIPTS / 'heal_claude_json_bind_drift.py').read_text()
        self.assertNotIn('Unit is active again', src)
        self.assertNotIn('re-probe was inconclusive', src)

    def test_rebound_body_states_only_observed_evidence(self):
        state = h.load_state()
        self._repair(state, verdict=h.VERDICT_LANDED,
                     new_facts=_facts(active='active', pid='222',
                                      invocation='inv-new'),
                     reprobe=h._PROBE_OK)
        body = self.alerts.with_subject_prefix('rebound:')[0]['message']
        self.assertIn('ActiveState=active', body)
        self.assertIn('O_RDWR', body)


def _translation(subject: str) -> dict:
    """The REAL translation row Larry reads on his phone. Asserting against the
    rendered row (and against the rendered DM body) is the point: a healer whose
    return value is right and whose words are wrong is still a healer that sent
    Larry to the wrong investigation."""
    import json
    table = json.loads(
        (_REPO_ROOT / 'config' / 'alert-translations.json').read_text())
    return table['heal-claude-json-bind-drift'][subject]


class StillDangledHonestyTests(_RepairFixture):
    """FIX 1. The healer retries a dangled mount every RESTART_COOLDOWN_SEC for
    as long as it stays dangled — measured at 23 restarts over 6 simulated hours
    on a permanently-EROFS unit. The DM said 'Suppressing further restarts to
    avoid a loop' and the translation said 'The healer stopped restarting to
    avoid a loop'. Both described a healer that had given up, so the retries
    were invisible and the reader was told to expect nothing.

    Retrying is CORRECT and stays. These bind the words to it, and bind the
    three arms that can prove a dangle to ONE alert gate."""

    def _body(self) -> str:
        """The real rendered still-dangled DM body."""
        self.alerts.calls.clear()
        h.dm_still_dangled(self.UNIT, 'in the test context')
        return self.alerts.with_subject_prefix('still-dangled:')[0]['message']

    def test_body_and_translation_never_claim_the_healer_stopped_restarting(self):
        body = self._body()
        tr = _translation('still-dangled')
        rendered = f"{tr['plain_language_summary']}\n{tr['recommended_action']}"
        for where, text in (('DM body', body), ('translation', rendered)):
            with self.subTest(where=where):
                low = text.lower()
                for lie in ('stopped restarting', 'suppressing further restarts',
                            'to avoid a loop', 'stopped the restart'):
                    self.assertNotIn(
                        lie, low,
                        f'{where} claims the healer stopped restarting; it '
                        f'retries every {h.RESTART_COOLDOWN_SEC // 60} min')

    def test_body_and_translation_state_the_real_retry_cadence(self):
        body = self._body()
        tr = _translation('still-dangled')
        rendered = f"{tr['plain_language_summary']} {tr['recommended_action']}"
        mins = h.RESTART_COOLDOWN_SEC // 60
        hours = h.ESCALATION_COOLDOWN_SEC // 3600
        self.assertIn(f'{mins} min', body)
        self.assertIn(f'{hours}h', body)
        self.assertIn(f'{mins} minutes', rendered)
        self.assertIn(f'{hours} hours', rendered)
        # …and both must say it is the MESSAGE that is rate-limited, not the fix.
        self.assertIn('rate-limited', body.lower())
        self.assertIn('rate-limited', rendered.lower())

    # ---- one gate, three arms ----

    def _arm_cooldown_probe(self, state, now):
        """inspect_unit: a later tick probes EROFS inside the restart cooldown."""
        h.mark_restarted(state, self.UNIT, now=now)
        facts = _facts(invocation='inv-old', pid='111')
        with mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_EROFS):
            return h.inspect_unit(self.UNIT, facts, h.CLASS_MONITOR, state, now=now)

    def _arm_post_restart_reprobe(self, state, now):
        """repair_unit: the freshly restarted namespace probes EROFS."""
        return self._repair(
            state, verdict=h.VERDICT_LANDED,
            new_facts=_facts(active='active', pid='222', invocation='inv-new'),
            reprobe=h._PROBE_EROFS, now=now)

    def _arm_deferred_reprobe(self, state, now):
        """_reconcile_one: a LATER tick's re-probe of a landed restart is EROFS."""
        h.open_pending(state, self.UNIT, 'inv-old', 111, now=now - 30)
        with mock.patch.object(h, 'unit_facts',
                               return_value=_facts(active='active', pid='222',
                                                   invocation='inv-new')), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_EROFS):
            return h.reconcile_pending(state, {self.UNIT}, now=now)[self.UNIT]

    def _arms(self):
        return (('cooldown probe', self._arm_cooldown_probe,
                 h.OUTCOME_COOLDOWN_DANGLED),
                ('post-restart re-probe', self._arm_post_restart_reprobe,
                 h.OUTCOME_STILL_DANGLED),
                ('deferred re-probe', self._arm_deferred_reprobe,
                 h.OUTCOME_STILL_DANGLED))

    def test_every_arm_dms_once_when_the_window_is_open(self):
        for label, arm, expected in self._arms():
            with self.subTest(arm=label):
                self.alerts.calls.clear()
                state = h.load_state()
                state['services'] = {}
                outcome = arm(state, 10_000.0)
                self.assertEqual(outcome, expected)
                self.assertEqual(
                    len(self.alerts.with_subject_prefix('still-dangled:')), 1,
                    f'{label} did not escalate at all')

    def test_every_arm_is_gated_by_the_same_escalation_window(self):
        """The defect, as a property over the arms: only ONE of the three
        consulted in_alert_cooldown, and the other two stamped mark_alerted —
        so the gated arm was permanently silenced BY the ungated ones and the
        binding constraint became larry_alerts' 60-min window instead of
        ESCALATION_COOLDOWN_SEC."""
        for label, arm, expected in self._arms():
            with self.subTest(arm=label):
                self.alerts.calls.clear()
                state = h.load_state()
                state['services'] = {}
                # Already escalated for this unit, well inside the window.
                h.mark_alerted(state, self.UNIT, now=10_000.0)
                outcome = arm(state, 10_000.0 + h.ESCALATION_COOLDOWN_SEC - 60)
                self.assertEqual(outcome, expected,
                                 f'{label} changed OUTCOME when the DM was '
                                 f'suppressed — suppression must silence the '
                                 f'telling, never the classification')
                self.assertEqual(
                    self.alerts.with_subject_prefix('still-dangled:'), [],
                    f'{label} escalated inside the {h.ESCALATION_COOLDOWN_SEC}s '
                    f'window — it does not share the gate')

    def test_chronic_dangle_keeps_retrying_but_dms_at_the_escalation_rate(self):
        """End to end over 6 simulated hours, the shape the DM now describes:
        restarts keep coming; DMs are capped by ESCALATION_COOLDOWN_SEC (which
        is what makes the module's own 'binding constraint' comment true)."""
        clock = [0.0]
        restarts = []
        state_holder = {}

        def fake_guard(unit, reason, restart_fn, **kw):
            restarts.append(clock[0])
            return rg.GuardedRestart(True, 'test', 0.0, restart_fn())

        def facts_router(unit):
            n = len(restarts)
            return _facts(unit=unit, active='active', pid=str(100 + n),
                          invocation=f'inv-{n}')

        with mock.patch.object(h.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(h.time, 'time', side_effect=lambda: 1e6 + clock[0]), \
             mock.patch.object(h, 'list_ourliberty_services', return_value=[self.UNIT]), \
             mock.patch.object(h, 'unit_facts', side_effect=facts_router), \
             mock.patch.object(h, 'unit_pending_job', return_value=''), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_EROFS), \
             mock.patch.object(h.subprocess, 'run',
                               return_value=subprocess.CompletedProcess([], 0, '', '')), \
             mock.patch.object(h.restart_guard, 'guarded_restart', side_effect=fake_guard):
            for _ in range(180):          # 6 h of 2-minute ticks
                h.main()
                clock[0] += 120
        state_holder['dms'] = self.alerts.with_subject_prefix('still-dangled:')

        # The fix is NOT to stop retrying — a dangled mount is a live outage.
        self.assertGreaterEqual(
            len(restarts), 20,
            'the healer stopped retrying a live outage; that is not the fix')
        # …but the telling is capped by OUR window, not larry_alerts' 60 min.
        expected_max = 6 * 3600 // h.ESCALATION_COOLDOWN_SEC + 1
        self.assertLessEqual(
            len(state_holder['dms']), expected_max,
            f'{len(state_holder["dms"])} still-dangled DMs in 6h — the '
            f'{h.ESCALATION_COOLDOWN_SEC}s escalation window is not binding, so '
            f'larry_alerts\' 60-min window is doing the rate-limiting instead')

    def test_a_suppressed_still_dangled_dm_is_still_revisited(self):
        """Widens PendingLedgerTests' no-DM/no-obligation property to cover
        suppression: a gated DM closes the obligation, so the guarantee that the
        unit is not FORGOTTEN has to come from the ordinary path instead."""
        state = h.load_state()
        h.mark_alerted(state, self.UNIT, now=1000.0)
        outcome = self._arm_post_restart_reprobe(state, 1010.0)
        self.assertEqual(outcome, h.OUTCOME_STILL_DANGLED)
        self.assertEqual(self.alerts.with_subject_prefix('still-dangled:'), [])
        self.assertIsNone(h.pending_entry(state, self.UNIT))
        # No DM and no obligation — so the ordinary path MUST still see it, and
        # must still retry once the restart cooldown expires.
        self.assertTrue(h.in_restart_cooldown(state, self.UNIT, now=1011.0))
        facts = _facts(invocation='inv-new', pid='222')
        with mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_EROFS):
            self.assertEqual(
                h.inspect_unit(self.UNIT, facts, h.CLASS_MONITOR, state, now=1011.0),
                h.OUTCOME_COOLDOWN_DANGLED)
            self.assertEqual(
                h.inspect_unit(self.UNIT, facts, h.CLASS_MONITOR, state,
                               now=1010.0 + h.RESTART_COOLDOWN_SEC + 1),
                h.REPAIR_REQUESTED, 'the retry never came back')


class PreflightReconfirmTests(_RepairFixture):
    """FIX 2. main() probes the whole fleet, THEN repairs in a second ordered
    pass with the agent-hosting unit last — measured at 408s of staleness — and
    guarded_restart may then drain for AGENT_DRAIN_CEILING_SEC more before the
    restart lands. Peer restarters rebind this same mount, so the decision could
    be ~20 minutes old and describe a unit that had been healthy for most of
    them, then send a `rebound` DM for a dangle that no longer existed."""

    def _repair_with(self, state, fresh, *, reprobe=h._PROBE_OK, then=None):
        """`reprobe` is what the PRE-FLIGHT probe sees; `then` (default: same)
        is what the post-restart re-probe sees, so the two are separable."""
        stale = _facts(invocation='inv-old', pid='111')
        probes = itertools.chain([reprobe],
                                 itertools.repeat(reprobe if then is None else then))
        guard = mock.Mock(return_value=rg.GuardedRestart(
            True, 'test', 0.0,
            h.RestartOutcome(h.VERDICT_LANDED, 'd', 1,
                             _facts(active='active', pid='999',
                                    invocation='inv-newest'))))
        with mock.patch.object(h, 'unit_facts', return_value=fresh), \
             mock.patch.object(h, 'probe_namespace_writable',
                               side_effect=lambda _p: next(probes)), \
             mock.patch.object(h.restart_guard, 'guarded_restart', guard):
            outcome = h.repair_unit(self.UNIT, stale, state, now=500.0)
        return outcome, guard

    def test_a_peer_rebind_between_detection_and_repair_is_not_restarted(self):
        state = h.load_state()
        fresh = _facts(active='active', pid='777', invocation='inv-peer')
        outcome, guard = self._repair_with(state, fresh, reprobe=h._PROBE_OK)
        self.assertEqual(outcome, h.OUTCOME_HEALED_BY_PEER)
        guard.assert_not_called()
        self.assertEqual(self.alerts.calls, [],
                         'DM\'d about a repair it did not perform')
        self.assertIsNone(h.pending_entry(state, self.UNIT))
        self.assertFalse(h.in_restart_cooldown(state, self.UNIT, now=501.0))

    def test_healed_by_peer_is_not_byte_identical_to_healthy(self):
        """A mount a PEER rebound mid-tick and a mount that was never dangled
        are the same end state reached by different systems; only one of them
        means the fleet had a simultaneous dangle."""
        self.assertNotEqual(h.OUTCOME_HEALED_BY_PEER, h.OUTCOME_HEALTHY)
        self.assertIn(h.OUTCOME_HEALED_BY_PEER, h.OUTCOMES)

    def test_a_peer_restart_that_did_not_fix_it_still_restarts(self):
        """The pre-flight must not become a way to SKIP a real repair: a peer
        restarted the unit, but its new namespace still probes EROFS, so ours
        is still needed."""
        state = h.load_state()
        fresh = _facts(active='active', pid='777', invocation='inv-peer')
        outcome, guard = self._repair_with(state, fresh,
                                           reprobe=h._PROBE_EROFS,
                                           then=h._PROBE_OK)
        guard.assert_called_once()
        self.assertEqual(outcome, h.OUTCOME_REBOUND)

    def test_an_unchanged_invocation_restarts_without_paying_a_probe(self):
        """The cheap half. A dangle is a property of the mount NAMESPACE, fixed
        for the life of an invocation — so an unchanged invocation is still
        dangled by construction and must not buy a second nsenter for it."""
        state = h.load_state()
        same = _facts(active='active', pid='111', invocation='inv-old')
        probe = mock.Mock(return_value=h._PROBE_OK)
        guard = mock.Mock(return_value=rg.GuardedRestart(
            True, 'test', 0.0, h.RestartOutcome(h.VERDICT_IN_PROGRESS, 'd', 1, None)))
        with mock.patch.object(h, 'unit_facts', return_value=same), \
             mock.patch.object(h, 'probe_namespace_writable', probe), \
             mock.patch.object(h.restart_guard, 'guarded_restart', guard):
            outcome = h.repair_unit(self.UNIT, same, state, now=500.0)
        guard.assert_called_once()
        probe.assert_not_called()
        self.assertEqual(outcome, h.OUTCOME_RESTART_IN_PROGRESS)

    def test_a_unit_that_left_active_between_detection_and_repair_is_not_restarted(self):
        for label, fresh in (
                ('unreadable', h.UnitFacts(self.UNIT, False, '', 'unknown', None,
                                           '', None, '', None)),
                ('inactive', _facts(active='inactive', pid='0')),
                ('failed', _facts(active='failed', pid='0'))):
            with self.subTest(now=label):
                self.alerts.calls.clear()
                state = h.load_state()
                outcome, guard = self._repair_with(state, fresh)
                guard.assert_not_called()
                self.assertIn(outcome, (h.OUTCOME_SKIP_UNKNOWN,
                                        h.OUTCOME_SKIP_INACTIVE))
                self.assertIsNone(
                    h.pending_entry(state, self.UNIT),
                    'opened an obligation for a restart it never issued — the '
                    'next tick would page repair-did-not-land for nothing')

    def test_the_budget_pays_for_every_shellout_a_peer_repair_makes(self):
        """#1000's lesson as arithmetic: a step that costs capped shellouts and
        is NOT in the cost model is a step systemd can SIGTERM the healer in the
        middle of, silently leaving the rest of the fleet unrepaired.

        MEASURED, not restated. Drive a real peer repair through the real
        restart_and_verify, add up the TIMEOUT CAP of every capped shellout it
        actually made, and require PEER_REPAIR_BUDGET_S to cover the total.
        Re-deriving the published formula here would pass against any formula,
        including one that forgot a step."""
        charged = []
        pre = _facts(active='active', pid='111', invocation='inv-old')
        post = _facts(active='active', pid='222', invocation='inv-new')
        reads = itertools.chain([pre], itertools.repeat(post))

        def facts(_u):
            charged.append(h.SYSTEMCTL_TIMEOUT_S)
            return next(reads)

        def job(_u):
            charged.append(h.SYSTEMCTL_TIMEOUT_S)
            return ''

        def probe(_p):
            charged.append(h.NSENTER_TIMEOUT_S)
            return h._PROBE_OK

        def run(*_a, **_k):
            charged.append(h.RESTART_TIMEOUT_S)
            return subprocess.CompletedProcess([], 0, '', '')

        state = h.load_state()
        with mock.patch.object(h, 'unit_facts', side_effect=facts), \
             mock.patch.object(h, 'unit_pending_job', side_effect=job), \
             mock.patch.object(h, 'probe_namespace_writable', side_effect=probe), \
             mock.patch.object(h.subprocess, 'run', side_effect=run):
            outcome = h.repair_unit(self.UNIT, pre, state, now=500.0)
        self.assertEqual(outcome, h.OUTCOME_REBOUND)
        # …plus the verify poll's own sleeping, which is wall-clock the shellout
        # caps do not include.
        needed = sum(charged) + h.VERIFY_WINDOW_S
        self.assertGreaterEqual(
            h.PEER_REPAIR_BUDGET_S, needed,
            f'a peer repair can cost {needed}s of capped shellouts but is '
            f'budgeted at {h.PEER_REPAIR_BUDGET_S}s — main() will start one it '
            f'cannot finish, and systemd kills the healer mid-fleet (#1000)')


class DidNotLandNarrativeTests(_RepairFixture):
    """FIX 3. `repair-did-not-land` hardcoded 'with no new invocation — the stop
    half ran but the start half has not' and sent it for every failure to land,
    including the exhausted-StartLimitBurst case its OWN text names (where a new
    invocation demonstrably appeared and then died), and including the case
    where systemctl was simply unreadable — for which the translation Larry
    reads asserts the unit 'is down'."""

    def _reconcile_at_grace(self, facts):
        self.alerts.calls.clear()
        state = h.load_state()
        h.open_pending(state, self.UNIT, 'inv-old', 111, now=0.0)
        with mock.patch.object(h, 'unit_facts', return_value=facts), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_EROFS):
            outcome = h.reconcile_pending(
                state, {self.UNIT}, now=h.RESTART_LANDING_GRACE_S + 1)[self.UNIT]
        return outcome, self.alerts.calls

    def test_never_came_back_keeps_the_original_narrative(self):
        outcome, calls = self._reconcile_at_grace(
            _facts(active='failed', pid='0', invocation=''))
        self.assertEqual(outcome, h.OUTCOME_REPAIR_DID_NOT_LAND)
        body = calls[0]['message']
        self.assertIn('no new invocation', body)
        self.assertIn('the start half has not', body)

    def test_came_back_and_died_is_not_reported_as_never_came_back(self):
        """A NEW InvocationID is present: the start half demonstrably ran."""
        outcome, calls = self._reconcile_at_grace(
            _facts(active='failed', pid='0', invocation='inv-new'))
        self.assertEqual(outcome, h.OUTCOME_REPAIR_DID_NOT_LAND)
        body = calls[0]['message']
        self.assertNotIn(
            'no new invocation', body,
            'the body denies an invocation systemd reported')
        self.assertNotIn('the start half has not', body)
        self.assertIn('NEW systemd invocation DID appear', body)
        self.assertIn('did not stay up', body)

    def test_the_translation_covers_both_narratives(self):
        tr = _translation('repair-did-not-land')
        rendered = f"{tr['plain_language_summary']} {tr['recommended_action']}"
        self.assertIn('no new systemd invocation', rendered.lower())
        self.assertIn('did not stay up', rendered.lower())

    def test_an_unreadable_read_is_not_reported_as_the_unit_being_down(self):
        """A failed READ is not absence. Its own subject, because the
        repair-did-not-land translation tells Larry the unit 'is down' — which
        was never observed here, and sends him to the unit's ExecStart instead
        of to systemd/dbus."""
        unreadable = h.UnitFacts(self.UNIT, False, '', 'unknown', None, '',
                                 None, '', None)
        outcome, calls = self._reconcile_at_grace(unreadable)
        self.assertEqual(outcome, h.OUTCOME_VERIFY_UNREADABLE)
        self.assertEqual([c['subject'] for c in calls],
                         [f'verify-unreadable:{self.UNIT}'])
        body = calls[0]['message']
        self.assertIn('unreadable', body.lower())
        self.assertIn('says nothing about the unit', body.lower())
        # …and the row Larry actually reads must not assert the unit is down.
        tr = _translation('verify-unreadable')
        rendered = f"{tr['plain_language_summary']} {tr['recommended_action']}"
        low = rendered.lower()
        # It must say the opposite of the claim it used to inherit, in words —
        # a bare absence of the phrase would also pass on an empty translation.
        self.assertIn('not a report that the bot is down', low)
        self.assertIn('may be running perfectly', low)
        self.assertIn('read path', low)
        self.assertNotIn('the bot is down and nothing is retrying it', low)
        # The old subject's translation DOES assert exactly that, which is the
        # whole reason this narrative needed its own subject.
        self.assertIn(
            'the bot is down and nothing is retrying it',
            _translation('repair-did-not-land')['plain_language_summary'].lower())

    def test_verify_unreadable_is_escalated_not_digested(self):
        unreadable = h.UnitFacts(self.UNIT, False, '', 'unknown', None, '',
                                 None, '', None)
        _outcome, calls = self._reconcile_at_grace(unreadable)
        self.assertIsNot(calls[0].get('healed'), True)
        self.assertEqual(
            larry_alerts.classify_route('heal-claude-json-bind-drift',
                                        calls[0]['subject'], healed=False),
            'escalate')


class RunbookConsistencyTests(unittest.TestCase):
    """Docs are the second copy here — they documented a re-probe step that was
    unreachable on two exits. Keep them from re-diverging."""

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
        subjects = ['rebound', 'still-dangled', 'repair-failed',
                    'repair-not-enqueued', 'repair-did-not-land',
                    'verify-unreadable', 'repair-deferred', 'probe-blind']
        for subject in subjects:
            with self.subTest(subject=subject):
                # emitted, documented, AND translated — a subject present in
                # only two of the three is either an undocumented page or a
                # translation for a page nothing can send.
                self.assertIn(f"{subject}:", src)
                self.assertIn(subject, text)
                self.assertIn(subject, table)


# -------------------- ephemeral: probe, never repair --------------------

class EphemeralProbeTests(_IsolatedAgentsRoot):
    UNIT = 'ourliberty-cycle.service'

    def setUp(self):
        super().setUp()
        self.alerts = self.capture_alerts()

    def _check(self, probe_result, restart_sentinel=None):
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
        """LOAD-BEARING for the tick budget: only ACTIVE persistent-or-ephemeral
        carve-out units are ever probed. A fleet of ~98 oneshot healers costs
        zero probes."""
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

    def test_baseline_then_departure_then_arrival(self):
        """PROPERTY over a synthetic set: departures WARN with the classifier
        reason, arrivals INFO, a stable set says neither."""
        state = h.load_state()
        h.coverage_delta(state, {'a.service', 'b.service'}, {})
        self.assertTrue(any('coverage baseline' in ln for ln in self._lines()))
        self.assertFalse(any('LEFT coverage' in ln for ln in self._lines()))

        Path(h.LOG_FILE).write_text('')
        h.coverage_delta(state, {'a.service', 'b.service'}, {})
        self.assertFalse(any('LEFT coverage' in ln for ln in self._lines()))
        self.assertFalse(any('ENTERED coverage' in ln for ln in self._lines()))

        Path(h.LOG_FILE).write_text('')
        h.coverage_delta(state, {'a.service', 'c.service'},
                         {'b.service': 'EPHEMERAL_JOB (Restart=no)'})
        lines = self._lines()
        left = [ln for ln in lines if 'LEFT coverage' in ln]
        entered = [ln for ln in lines if 'ENTERED coverage' in ln]
        self.assertEqual(len(left), 1)
        self.assertIn('b.service', left[0])
        self.assertIn('EPHEMERAL_JOB (Restart=no)', left[0])
        self.assertIn('[WARN]', left[0])
        self.assertEqual(len(entered), 1)
        self.assertIn('c.service', entered[0])

    def _clear_log(self):
        Path(h.LOG_FILE).write_text('')

    def test_a_departed_unit_stays_named_on_every_later_tick(self):
        """THE BLIND SPOT THIS CLOSES. A departure used to be announced in
        exactly one WARN, after which `state['coverage']` had already been
        rewritten to the shrunken set — so every subsequent tick read as normal
        and the unit sat permanently outside repair coverage with no standing
        signal anywhere."""
        state = h.load_state()
        reasons = {'a.service': 'Restart=always', 'b.service': 'Restart=always'}
        h.coverage_delta(state, {'a.service', 'b.service'}, reasons)

        self._clear_log()
        h.coverage_delta(state, {'a.service'},
                         {**reasons, 'b.service': 'EPHEMERAL_JOB (Restart=no)'})
        self.assertTrue(any('LEFT coverage' in ln for ln in self._lines()))

        # ...and is still named on the NEXT three ticks, which used to be silent.
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

        unreadable = {'a.service': 'systemctl show unreadable (timeout/missing)',
                      'b.service': 'systemctl show unreadable (timeout/missing)'}
        self._clear_log()
        h.coverage_delta(state, set(), unreadable)
        self.assertEqual(len([ln for ln in self._lines() if 'LEFT coverage' in ln]), 2)

        # The tick AFTER the collapse must not re-announce a baseline.
        self._clear_log()
        h.coverage_delta(state, set(), unreadable)
        lines = self._lines()
        self.assertFalse(any('coverage baseline' in ln for ln in lines),
                         'an empty baseline must not read as a missing one')
        coverage = [ln for ln in lines if 'coverage=' in ln][0]
        self.assertIn('coverage=(none)', coverage)
        self.assertIn('departed=a.service,b.service', coverage)

    def test_empty_baseline_does_not_spray_departures(self):
        state = h.load_state()
        state['coverage'] = []
        h.coverage_delta(state, {'a.service'}, {})
        self.assertFalse(any('LEFT coverage' in ln for ln in self._lines()))

    def test_a_daemon_with_a_companion_timer_stays_in_coverage(self):
        """The finding's scenario, which must now be a NON-event: under the
        merged PR this returned None and the unit silently left coverage."""
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
                   h.OUTCOME_SKIP_UNKNOWN, h.OUTCOME_SKIP_NSGONE,
                   h.OUTCOME_AWAITING_VERIFY]
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
        self.assertIn('deferred=0', tick)


# -------------------- check_unit outcome matrix --------------------

class CheckUnitTests(_IsolatedAgentsRoot):
    UNIT = 'ourliberty-beacon-bot.service'

    def setUp(self):
        super().setUp()
        self.alerts = self.capture_alerts()

    def test_healthy_when_writable(self):
        with mock.patch.object(h, 'unit_facts', return_value=_facts()), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_OK):
            self.assertEqual(h.check_unit(self.UNIT, h.load_state()),
                             h.OUTCOME_HEALTHY)
        self.assertEqual(self.alerts.calls, [])

    def test_probe_error_escalates_once(self):
        state = h.load_state()
        with mock.patch.object(h, 'unit_facts', return_value=_facts()), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_OTHER):
            self.assertEqual(h.check_unit(self.UNIT, state), h.OUTCOME_PROBE_ERROR)
            self.assertEqual(h.check_unit(self.UNIT, state), h.OUTCOME_PROBE_ERROR)
        self.assertEqual(len(self.alerts.with_subject_prefix('probe-blind:')), 1)

    def test_process_gone_is_benign_skip_no_dm(self):
        state = h.load_state()
        with mock.patch.object(h, 'unit_facts', return_value=_facts()), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_GONE):
            self.assertEqual(h.check_unit(self.UNIT, state), h.OUTCOME_SKIP_NSGONE)
        self.assertEqual(self.alerts.calls, [])
        self.assertFalse(h.in_alert_cooldown(state, self.UNIT))

    def test_rebound_on_erofs_then_landed_restart(self):
        state = h.load_state()
        probe = mock.Mock(side_effect=[h._PROBE_EROFS, h._PROBE_OK])
        new_facts = _facts(active='active', pid='222', invocation='inv-new')
        with mock.patch.object(h, 'unit_facts',
                               return_value=_facts(pid='111', invocation='inv-old')), \
             mock.patch.object(h, 'probe_namespace_writable', probe), \
             mock.patch.object(
                 h, 'restart_and_verify',
                 return_value=h.RestartOutcome(h.VERDICT_LANDED, 'd', 1,
                                               new_facts)) as r:
            self.assertEqual(h.check_unit(self.UNIT, state), h.OUTCOME_REBOUND)
        r.assert_called_once_with(self.UNIT, 'inv-old', 111)
        self.assertEqual(len(self.alerts.with_subject_prefix('rebound:')), 1)
        self.assertTrue(h.in_restart_cooldown(state, self.UNIT))

    def test_cooldown_dangled_suppresses_restart(self):
        state = h.load_state()
        h.mark_restarted(state, self.UNIT)
        with mock.patch.object(h, 'unit_facts', return_value=_facts()), \
             mock.patch.object(h, 'probe_namespace_writable',
                               return_value=h._PROBE_EROFS), \
             mock.patch.object(h, 'restart_and_verify') as r:
            self.assertEqual(h.check_unit(self.UNIT, state),
                             h.OUTCOME_COOLDOWN_DANGLED)
        r.assert_not_called()
        self.assertEqual(len(self.alerts.with_subject_prefix('still-dangled:')), 1)


# -------------------- tick budget (multi-unit) --------------------

CARVEOUT_DAEMONS = [
    'ourliberty-beacon-bot.service',
    'ourliberty-forge-bot.service',
    'ourliberty-inbox-watcher.service',
    'ourliberty-mirror-bot.service',
    'ourliberty-outbox-notifier.service',
    'ourliberty-pulse-bot.service',
    'ourliberty-spec-review-runner.service',
]


def _timeout_start_sec() -> int:
    unit = _SYSTEMD_DIR / 'ourliberty-heal-claude-json-bind-drift.service'
    for line in unit.read_text().splitlines():
        line = line.strip()
        if line.startswith('TimeoutStartSec='):
            return int(line.split('=', 1)[1].strip())
    raise AssertionError('TimeoutStartSec not found')


class TickBudgetTests(_IsolatedAgentsRoot):
    """A single atomic replace of the config dangles EVERY carve-out unit in the
    same tick. Simulated under a virtual clock against the REAL TimeoutStartSec."""

    def _run_tick(self, dangled: set[str], latency: float):
        """Simulate one tick. Every shellout costs `latency`; the agent-hosting
        unit pays the full drain ceiling. Returns (simulated wall-time, the
        order units were restarted in)."""
        # Each simulated tick starts from a clean ledger: a leftover restart
        # cooldown from a previous subTest would suppress every repair and make
        # the budget assertion vacuous.
        try:
            Path(h.STATE_FILE).unlink()
        except OSError:
            pass
        clock = [0.0]
        order: list[str] = []
        restarted: set[str] = set()

        def advance(dt):
            clock[0] += dt

        def fake_run(*a, **k):
            advance(latency)
            return subprocess.CompletedProcess([], 0, '', '')

        def fake_guard(unit, reason, restart_fn, **kw):
            order.append(unit)
            if unit in rg.AGENT_HOSTING_UNITS:
                advance(h.AGENT_DRAIN_CEILING_SEC)
            restarted.add(unit)
            return rg.GuardedRestart(True, 'test', 0.0, restart_fn())

        def fake_probe_router(pid):
            advance(latency)
            return h._PROBE_EROFS if pid == 111 else h._PROBE_OK

        def facts_router(unit):
            advance(latency)
            if unit not in dangled:
                return _facts(active='active', pid='999', restart='always',
                              invocation='inv-old')
            pid = '222' if unit in restarted else '111'
            inv = 'inv-new' if unit in restarted else 'inv-old'
            return _facts(active='active', pid=pid, restart='always',
                          invocation=inv)

        with mock.patch.object(h.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(h.time, 'sleep', side_effect=advance), \
             mock.patch.object(h.time, 'time', side_effect=lambda: 1_000_000 + clock[0]), \
             mock.patch.object(h, 'list_ourliberty_services',
                               return_value=list(CARVEOUT_DAEMONS)), \
             mock.patch.object(h, 'unit_facts', side_effect=facts_router), \
             mock.patch.object(h, 'unit_pending_job',
                               side_effect=lambda u: advance(latency) or ''), \
             mock.patch.object(h, 'probe_namespace_writable',
                               side_effect=fake_probe_router), \
             mock.patch.object(h.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(h.restart_guard, 'guarded_restart',
                               side_effect=fake_guard), \
             mock.patch.object(h, '_import_larry_alerts',
                               return_value=_FakeLarryAlerts()):
            h.main()
        return clock[0], order

    def test_property_tick_fits_the_unit_timeout_for_every_subset_and_latency(self):
        timeout = _timeout_start_sec()
        for size in (1, 3, len(CARVEOUT_DAEMONS)):
            dangled = set(CARVEOUT_DAEMONS[:size])
            for latency in (0.05, 0.5, 1.0, 5.0, float(h.SYSTEMCTL_TIMEOUT_S)):
                with self.subTest(dangled=size, latency=latency):
                    elapsed, _ = self._run_tick(dangled, latency)
                    self.assertLessEqual(
                        elapsed, timeout,
                        f'simulated tick {elapsed:.0f}s > TimeoutStartSec '
                        f'{timeout}s with {size} dangled at {latency}s/shellout')

    def test_pass_through_peers_are_repaired_before_the_drain_payer(self):
        _, order = self._run_tick(set(CARVEOUT_DAEMONS), 0.05)
        self.assertEqual(order[-1], 'ourliberty-inbox-watcher.service')
        self.assertEqual(len(order), len(CARVEOUT_DAEMONS))

    def test_a_budget_too_small_for_the_drain_defers_it_and_keeps_the_peers(self):
        """The exhaustion path, end to end: with a budget that cannot cover the
        900s drain, all six pass-through peers are still repaired and the
        agent-hosting unit is DEFERRED — guarded_restart is never called for it,
        so its ceiling is never reduced to make it fit."""
        with mock.patch.object(h, 'TICK_BUDGET_S', h.PEER_REPAIR_BUDGET_S * 6 + 50):
            _, order = self._run_tick(set(CARVEOUT_DAEMONS), 0.05)
        self.assertNotIn('ourliberty-inbox-watcher.service', order)
        self.assertEqual(len(order), len(CARVEOUT_DAEMONS) - 1)
        tick = [ln for ln in Path(h.LOG_FILE).read_text().splitlines()
                if 'tick: ' in ln][-1]
        self.assertIn('deferred=1', tick)

    def test_fit_check_defers_the_drain_payer_and_never_shortens_its_ceiling(self):
        """When the remaining budget cannot cover the drain, the agent-hosting
        unit is DEFERRED — its ceiling is never passed a reduced value."""
        state = h.load_state()
        agent = 'ourliberty-inbox-watcher.service'
        outcome = h.defer_repair(state, agent, remaining=10.0,
                                 cost=h.AGENT_REPAIR_BUDGET_S, now=1.0)
        self.assertEqual(outcome, h.OUTCOME_DEFERRED)
        self.assertIsNone(h.pending_entry(state, agent))
        src = (_REPO_SCRIPTS / 'heal_claude_json_bind_drift.py').read_text()
        self.assertIn('ceiling_sec=AGENT_DRAIN_CEILING_SEC', src)
        self.assertEqual(src.count('ceiling_sec='), 1,
                         'the drain ceiling must have exactly one, constant, '
                         'call site — a reduced ceiling force-restarts a live '
                         'Claude session early')

    def test_deferral_is_silent_at_first_then_escalates_once_per_window(self):
        state = h.load_state()
        alerts = self.capture_alerts()
        unit = 'ourliberty-pulse-bot.service'
        for tick in range(1, h.DEFER_ESCALATE_TICKS):
            h.defer_repair(state, unit, 1.0, 99, now=float(tick))
            self.assertEqual(alerts.calls, [], f'DM on deferral #{tick}')
        h.defer_repair(state, unit, 1.0, 99, now=float(h.DEFER_ESCALATE_TICKS))
        self.assertEqual(len(alerts.with_subject_prefix('repair-deferred:')), 1)
        # …and not again inside the escalation window.
        h.defer_repair(state, unit, 1.0, 99, now=float(h.DEFER_ESCALATE_TICKS) + 1)
        self.assertEqual(len(alerts.with_subject_prefix('repair-deferred:')), 1)
        # …until it is finally repaired, which resets the counter.
        h.clear_deferral(state, unit)
        self.assertEqual(state['services'][unit]['deferred_ticks'], 0)

    def test_deferred_units_create_no_pending_obligation(self):
        state = h.load_state()
        self.capture_alerts()
        h.defer_repair(state, 'ourliberty-forge-bot.service', 1.0, 99, now=1.0)
        self.assertEqual(state['pending'], {})


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
    the .claude/ DIRECTORY carve-out MUST also grant the .claude.json FILE
    carve-out."""

    CLAUDE_DIR = '/home/larry/.claude'
    CLAUDE_JSON = CLAUDE_JSON

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
                 f'(re-add the file carve-out per PR #470): {offenders}'))


class HealerUnitPrivilegeLintTests(unittest.TestCase):
    SERVICE = _SYSTEMD_DIR / 'ourliberty-heal-claude-json-bind-drift.service'
    TIMER = _SYSTEMD_DIR / 'ourliberty-heal-claude-json-bind-drift.timer'

    def test_service_and_timer_exist(self):
        self.assertTrue(self.SERVICE.is_file(), self.SERVICE)
        self.assertTrue(self.TIMER.is_file(), self.TIMER)

    def test_service_is_not_nonewprivileges_or_protecthome(self):
        directives = [
            ln.strip().replace(' ', '')
            for ln in self.SERVICE.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith('#')
        ]
        self.assertNotIn('NoNewPrivileges=true', directives)
        self.assertFalse(
            [d for d in directives if d.startswith('ProtectHome=')],
            msg='ProtectHome would break the nsenter namespace view + sudo')

    def test_service_runs_as_larry(self):
        self.assertIn('User=larry', self.SERVICE.read_text())

    def test_module_mirrors_the_units_timeoutstartsec(self):
        """TICK_BUDGET_S derives from this; a drifted copy silently un-bounds
        the tick."""
        self.assertEqual(h.HEALER_TIMEOUT_START_SEC, _timeout_start_sec())
        self.assertLess(h.TICK_BUDGET_S, h.HEALER_TIMEOUT_START_SEC)


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
