"""Self-checks for the atomic_io real-tree-write ledger (Beacon Gap-2 closure).

The session-end tripwire's content-sentinel scan is blind to JSON state writes
(they never carry the log-line sentinel). The ledger closes that: it records any
atomic_io write that LANDS under the real ~/agents tree during a test, so the
session-end report attributes it even though the file content carries no
sentinel. It sits BEHIND the #816 refuse_live_state_write guard (which raises
before such a write), so these tests simulate that guard being bypassed/regressed
to prove the backstop fires — and prove it does NOT fire for sandbox writes.
"""
try:
    from . import _bootstrap  # noqa: F401  bootstrap-first-import
except ImportError:  # pragma: no cover
    import _bootstrap  # noqa: F401

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import atomic_io
import test_isolation_guard as guard
import test_no_production_writes_runtime as trip

_SENTINEL = 'OURLIBERTY_TEST_RUN_SENTINEL'


class AtomicIoLedgerSelfCheck(unittest.TestCase):
    def setUp(self):
        trip._drain_atomic_io_ledger()  # start clean
        self._old_sentinel = os.environ.get(_SENTINEL)
        os.environ[_SENTINEL] = 'OL-TEST-RUN-SENTINEL-ledgercheck'

    def tearDown(self):
        if self._old_sentinel is None:
            os.environ.pop(_SENTINEL, None)
        else:
            os.environ[_SENTINEL] = self._old_sentinel
        trip._drain_atomic_io_ledger()

    def _fake_real_tree(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        real = (base / 'agents').resolve()
        real.mkdir(parents=True)
        return real

    def test_records_landed_real_tree_write_when_guard_bypassed(self):
        real = self._fake_real_tree()
        # Point the guard's "real roots" at our fake tree and NEUTER #816 so the
        # write actually lands (simulating the guard being regressed/removed).
        with mock.patch.object(guard, '_real_agents_roots', return_value=[real]), \
             mock.patch.object(guard, 'refuse_live_state_write', lambda p, c: None):
            undo = trip._instrument_atomic_io('OL-TEST-RUN-SENTINEL-ledgercheck')
            try:
                target = real / 'state' / 'beacon-pending-approvals.json'
                atomic_io.atomic_write_json(target, {'chat_id': '12345'})
            finally:
                for fn in undo:
                    fn()
            hits = trip._drain_atomic_io_ledger()
        self.assertTrue(
            any(h['path'].endswith('beacon-pending-approvals.json') for h in hits),
            f'ledger failed to record a landed real-tree state write: {hits}',
        )
        self.assertTrue(all(h.get('via') == 'atomic_io' for h in hits))

    def test_ignores_sandbox_write(self):
        real = self._fake_real_tree()
        sandbox = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        with mock.patch.object(guard, '_real_agents_roots', return_value=[real]), \
             mock.patch.object(guard, 'refuse_live_state_write', lambda p, c: None):
            undo = trip._instrument_atomic_io('OL-TEST-RUN-SENTINEL-ledgercheck')
            try:
                atomic_io.atomic_write_json(sandbox / 'state.json', {'ok': 1})
            finally:
                for fn in undo:
                    fn()
            hits = trip._drain_atomic_io_ledger()
        self.assertEqual(hits, [], f'sandbox write must not be recorded: {hits}')

    def test_ignores_write_when_no_sentinel(self):
        # Outside a test run (no sentinel) the ledger records nothing even for a
        # real-tree write — mirrors the guard's prod pass-through.
        real = self._fake_real_tree()
        os.environ.pop(_SENTINEL, None)
        with mock.patch.object(guard, '_real_agents_roots', return_value=[real]), \
             mock.patch.object(guard, 'refuse_live_state_write', lambda p, c: None):
            undo = trip._instrument_atomic_io('unused')
            try:
                atomic_io.atomic_write_json(real / 'state' / 'x.json', {'ok': 1})
            finally:
                for fn in undo:
                    fn()
            hits = trip._drain_atomic_io_ledger()
        self.assertEqual(hits, [])

    def test_session_end_reports_ledger_paths(self):
        # End-to-end: a landed real-tree write surfaces in run_session_end_tripwire's
        # failure message even though the file carries no content sentinel.
        real = self._fake_real_tree()
        with mock.patch.object(guard, '_real_agents_roots', return_value=[real]), \
             mock.patch.object(guard, 'refuse_live_state_write', lambda p, c: None):
            undo = trip._instrument_atomic_io('OL-TEST-RUN-SENTINEL-ledgercheck')
            target = real / 'blackboard' / 'for-larry-escalations.json'
            atomic_io.atomic_write_json(target, {'id': 't1'})
            # session-end: scans the REAL tree (finds nothing) + drains ledger.
            hits, message = trip.run_session_end_tripwire(
                'OL-TEST-RUN-SENTINEL-ledgercheck', 0.0, undo, runner='ledger-selfcheck',
            )
        self.assertIsNotNone(message, 'ledgered leak must produce a failure message')
        self.assertIn('for-larry-escalations.json', message)


if __name__ == '__main__':
    unittest.main()
