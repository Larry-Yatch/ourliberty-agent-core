"""Cross-process locking tests for scripts/beacon_approval_handler.py (PR-E2 #48).

unittest (repo convention; pytest isn't installed on the droplet).

beacon_telegram_bot and outbox_notifier are two daemons that both add/resolve
pending approvals via the state=None API (each does its own load→mutate→save).
With no lock an interleaved pair drops one side's mutation. These tests drive the
two RMW paths from real concurrent processes and assert nothing is lost.
"""
from __future__ import annotations

import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import beacon_approval_handler as ah  # noqa: E402
import file_lock  # noqa: E402


# Top-level workers (picklable for the 'spawn' start method).
def _add_worker(path_str: str, ids) -> None:
    sys.path.insert(0, str(_SCRIPTS_DIR))
    import beacon_approval_handler as h
    h.PENDING_APPROVALS_PATH = Path(path_str)
    for i in ids:
        h.add_pending({'task_id': i, 'summary': f'plan {i}'}, chat_id=1)


def _resolve_worker(path_str: str, ids) -> None:
    sys.path.insert(0, str(_SCRIPTS_DIR))
    import beacon_approval_handler as h
    h.PENDING_APPROVALS_PATH = Path(path_str)
    for i in ids:
        h.resolve(i, 'approved')


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / 'pending.json'
        self._orig = ah.PENDING_APPROVALS_PATH
        ah.PENDING_APPROVALS_PATH = self.path

    def tearDown(self):
        ah.PENDING_APPROVALS_PATH = self._orig
        self._tmp.cleanup()


class ExplicitStateContractTest(_Base):
    def test_explicit_state_does_not_persist(self):
        """When the caller passes state, the mutator must NOT load/save (the
        caller owns the transaction) — so no file is written."""
        st = {'version': 1, 'pending': [], 'history': []}
        entry = ah.add_pending({'task_id': 't1'}, chat_id=1, state=st)
        self.assertEqual(entry['id'], 't1')
        self.assertEqual(len(st['pending']), 1)
        self.assertFalse(self.path.exists(), 'explicit-state add must not persist')


@unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
class ConcurrentMutationTest(_Base):
    def test_concurrent_adds_lose_nothing(self):
        n_procs, per = 3, 120
        ctx = multiprocessing.get_context()
        id_sets = [[f'p{k}-{i}' for i in range(per)] for k in range(n_procs)]
        procs = [ctx.Process(target=_add_worker, args=(str(self.path), ids))
                 for ids in id_sets]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0)

        state = ah.load_state()
        got = {e['id'] for e in state['pending']}
        expected = {i for ids in id_sets for i in ids}
        self.assertEqual(
            len(state['pending']), n_procs * per,
            'lost-update: concurrent add_pending dropped entries')
        self.assertEqual(got, expected)

    def test_concurrent_resolves_lose_nothing(self):
        # Pre-populate K pending entries, then resolve them from two processes
        # over a disjoint split — every entry must end up in history, none stuck.
        k = 200
        st = ah.load_state()
        for i in range(k):
            ah.add_pending({'task_id': f'r{i}'}, chat_id=1, state=st)
        ah.save_state(st)

        ids = [f'r{i}' for i in range(k)]
        half = k // 2
        ctx = multiprocessing.get_context()
        procs = [
            ctx.Process(target=_resolve_worker, args=(str(self.path), ids[:half])),
            ctx.Process(target=_resolve_worker, args=(str(self.path), ids[half:])),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0)

        state = ah.load_state()
        self.assertEqual(state['pending'], [],
                         'lost-update: a resolution was dropped (entry stuck pending)')
        resolved = {e['id'] for e in state['history']}
        self.assertEqual(resolved, set(ids))


if __name__ == '__main__':
    unittest.main()
