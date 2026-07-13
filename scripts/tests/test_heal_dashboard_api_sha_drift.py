#!/usr/bin/env python3
"""Tests for heal_dashboard_api_sha_drift (dashboard-api-deploy-race-001).

Covers the decision matrix: fresh (no restart), SHA drift (restart), a
running process that predates the git_sha field or the route entirely
(restart), unreachable / auth / no-token (no restart), the restart cooldown
+ still-stale escalation, and a failed restart escalation.

All subprocess + HTTP + DM side-effects are injected via monkeypatch so no
real systemctl / network / alert leaves the test process.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_heal_dashboard_api_sha_drift
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import heal_dashboard_api_sha_drift as h  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix='sha-drift-'))
        # Point every side-effecting path at the per-test tmpdir.
        for attr, name in (
            ('STATE_FILE', 'state.json'),
            ('LOG_FILE', 'healer.log'),
            ('HEARTBEAT_FILE', 'heartbeat'),
            ('KILL_SWITCH', 'healers.disabled'),
        ):
            p = mock.patch.object(h, attr, tmp / name)
            p.start()
            self.addCleanup(p.stop)
        # Captured DM calls: list of kwargs dicts.
        self.dms: list[dict] = []
        pdm = mock.patch.object(
            h, '_dm',
            side_effect=lambda **kw: self.dms.append(kw) or True,
        )
        pdm.start()
        self.addCleanup(pdm.stop)
        self.tmp = tmp

    def _patch(self, **kw):
        for name, val in kw.items():
            p = mock.patch.object(h, name, val)
            p.start()
            self.addCleanup(p.stop)


class DecisionMatrixTest(_Base):
    def test_fresh_no_restart(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'a' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(), 'fresh')
        self.assertEqual(restarts, [])
        self.assertEqual(self.dms, [])

    def test_sha_drift_restarts(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(now=1000.0), 'restarted')
        self.assertEqual(len(restarts), 1)
        # Success routes to the digest lane (no page).
        self.assertEqual(len(self.dms), 1)
        self.assertEqual(self.dms[0]['route'], 'digest')

    def test_field_missing_is_drift(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_FIELD_MISSING, None),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(now=1000.0), 'restarted')
        self.assertEqual(len(restarts), 1)

    def test_route_missing_is_drift(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_ROUTE_MISSING, None),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(now=1000.0), 'restarted')
        self.assertEqual(len(restarts), 1)

    def test_unreachable_no_restart(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_UNREACHABLE, None),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(), 'unreachable')
        self.assertEqual(restarts, [])
        self.assertEqual(self.dms, [])

    def test_auth_no_restart(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_AUTH, None),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(), 'auth')
        self.assertEqual(restarts, [])

    def test_no_disk_head_no_restart(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: None,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(), 'no-disk-head')
        self.assertEqual(restarts, [])

    def test_kill_switch_short_circuits(self):
        (self.tmp / 'healers.disabled').write_text('')
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(), 'kill-switch')
        self.assertEqual(restarts, [])


class CooldownTest(_Base):
    def test_second_drift_within_cooldown_escalates_not_restarts(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        # First tick restarts.
        self.assertEqual(h.run_once(now=1000.0), 'restarted')
        # Second tick 60s later, still drifted → no new restart, escalate DM.
        self.assertEqual(h.run_once(now=1060.0), 'stuck-in-cooldown')
        self.assertEqual(len(restarts), 1)
        escalations = [d for d in self.dms if d.get('route') == 'escalate']
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]['subject'], 'dashboard-api-sha-drift-stuck')

    def test_drift_after_cooldown_expires_restarts_again(self):
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: restarts.append(1) or (0, ''),
        )
        self.assertEqual(h.run_once(now=1000.0), 'restarted')
        later = 1000.0 + h.RESTART_COOLDOWN_SEC + 1
        self.assertEqual(h.run_once(now=later), 'restarted')
        self.assertEqual(len(restarts), 2)

    def test_new_deploy_within_cooldown_restarts_not_stuck(self):
        # A NEW (different) on-disk HEAD landing inside the restart cooldown is a
        # fresh back-to-back deploy, not a stuck loop → restart for it, no
        # 'stuck' escalation.
        disk = {'sha': 'b' * 40}
        restarts = []
        self._patch(
            read_disk_head=lambda *a, **k: disk['sha'],
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: restarts.append(disk['sha']) or (0, ''),
        )
        self.assertEqual(h.run_once(now=1000.0), 'restarted')
        disk['sha'] = 'c' * 40  # a newer deploy lands 60s later
        self.assertEqual(h.run_once(now=1060.0), 'restarted')
        self.assertEqual(len(restarts), 2)
        self.assertEqual(
            [d for d in self.dms if d.get('route') == 'escalate'], [])

    def test_failed_restart_does_not_double_dm(self):
        # A persistently failing restart pages ONCE (restart-failed), then the
        # next tick (same target, in cooldown) suppresses the 'stuck' page.
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: (-1, 'unit inactive after settle'),
        )
        self.assertEqual(h.run_once(now=1000.0), 'restart-failed')
        self.assertEqual(h.run_once(now=1060.0), 'stuck-in-cooldown')
        escalations = [d for d in self.dms if d.get('route') == 'escalate']
        self.assertEqual(len(escalations), 1)
        self.assertEqual(
            escalations[0]['subject'], 'dashboard-api-sha-drift-restart-failed')


class RestartFailureTest(_Base):
    def test_failed_restart_escalates(self):
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: (-1, 'unit inactive after settle'),
        )
        self.assertEqual(h.run_once(now=1000.0), 'restart-failed')
        escalations = [d for d in self.dms if d.get('route') == 'escalate']
        self.assertEqual(len(escalations), 1)
        self.assertEqual(
            escalations[0]['subject'], 'dashboard-api-sha-drift-restart-failed')


class ProbeParsingTest(_Base):
    """probe_running_sha's status classification from HTTP shapes."""

    def _opener(self, *, status=200, body='', http_error_code=None,
                url_error=False):
        import io
        import urllib.error

        class _Resp:
            def __init__(self, status, body):
                self.status = status
                self._body = body.encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            if url_error:
                raise urllib.error.URLError('connection refused')
            if http_error_code is not None:
                raise urllib.error.HTTPError(
                    'u', http_error_code, 'e', {}, io.BytesIO(b''))
            return _Resp(status, body)

        return opener

    def test_ok_with_sha(self):
        st, sha = h.probe_running_sha(
            token='t', opener=self._opener(body='{"git_sha": "abc123"}'))
        self.assertEqual((st, sha), (h.PROBE_OK, 'abc123'))

    def test_200_without_sha_is_field_missing(self):
        st, sha = h.probe_running_sha(
            token='t', opener=self._opener(body='{"level": "green"}'))
        self.assertEqual((st, sha), (h.PROBE_FIELD_MISSING, None))

    def test_404_is_route_missing(self):
        st, sha = h.probe_running_sha(
            token='t', opener=self._opener(http_error_code=404))
        self.assertEqual((st, sha), (h.PROBE_ROUTE_MISSING, None))

    def test_401_is_auth(self):
        st, sha = h.probe_running_sha(
            token='t', opener=self._opener(http_error_code=401))
        self.assertEqual((st, sha), (h.PROBE_AUTH, None))

    def test_500_is_unreachable(self):
        st, sha = h.probe_running_sha(
            token='t', opener=self._opener(http_error_code=500))
        self.assertEqual((st, sha), (h.PROBE_UNREACHABLE, None))

    def test_conn_refused_is_unreachable(self):
        st, sha = h.probe_running_sha(token='t', opener=self._opener(url_error=True))
        self.assertEqual((st, sha), (h.PROBE_UNREACHABLE, None))

    def test_no_token(self):
        import os
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DASHBOARD_API_TOKEN', None)
            st, sha = h.probe_running_sha(token=None)
        self.assertEqual((st, sha), (h.PROBE_NO_TOKEN, None))


class ConfidenceSeverityLockTest(_Base):
    """Slice 3 lock: this healer ALREADY threads confidence into severity
    (slice 2), so slice 3 makes NO code change here — but the invariant it
    relies on must be locked. Its two red branches are high-confidence,
    CONFIRMED conditions and carry severity='critical' (the value append_alert
    can never hold/downgrade); its self-healed branch is the low-signal case
    and already digests. There is no borderline red to downgrade, so a future
    refactor must NEVER quiet these criticals into the digest lane."""

    def test_confirmed_stuck_is_critical_escalate(self):
        # Restart taken, still stale next tick within cooldown -> confirmed
        # loop -> critical/escalate. Never downgradeable.
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: (0, ''),
        )
        h.run_once(now=1000.0)          # first tick: restart (digest)
        h.run_once(now=1060.0)          # second tick: still stale -> stuck
        stuck = [d for d in self.dms if d.get('subject')
                 == 'dashboard-api-sha-drift-stuck']
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]['severity'], 'critical')
        self.assertEqual(stuck[0]['route'], 'escalate')

    def test_restart_failed_is_critical_escalate(self):
        # The restart itself failed -> confirmed failure -> critical/escalate.
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: (1, 'boom'),
        )
        h.run_once(now=1000.0)
        failed = [d for d in self.dms if d.get('subject')
                  == 'dashboard-api-sha-drift-restart-failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['severity'], 'critical')
        self.assertEqual(failed[0]['route'], 'escalate')

    def test_self_healed_is_warning_digest(self):
        # The low-signal branch (auto-restart succeeded) already digests.
        self._patch(
            read_disk_head=lambda *a, **k: 'b' * 40,
            probe_running_sha=lambda *a, **k: (h.PROBE_OK, 'a' * 40),
            restart_unit=lambda *a, **k: (0, ''),
        )
        self.assertEqual(h.run_once(now=1000.0), 'restarted')
        healed = [d for d in self.dms if d.get('subject')
                  == 'dashboard-api-sha-drift-healed']
        self.assertEqual(len(healed), 1)
        self.assertEqual(healed[0]['severity'], 'warning')
        self.assertEqual(healed[0]['route'], 'digest')


if __name__ == '__main__':
    unittest.main()
