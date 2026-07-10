"""Cross-process no-lost-update proofs for the read-modify-write ledgers.

no_session_ledger / rebase_obligation_ledger / suite_guardian_ledger each do a
load→mutate→atomic_write_json. ``atomic_write_json`` makes the swap atomic but
does NOT serialize the RMW, so overlapping writers (e.g. an outbox_notifier
restart-rescan racing the main loop) could lost-update: writer B reads the state
before writer A's write lands, then B's write clobbers A's row. ``locked_update``
(now wrapping every mutator) closes that.

Each test spawns N processes that each OPEN a disjoint block of task_ids into the
SAME ledger file. Every id is distinct, so a correct serialized RMW ends with
exactly N*M rows; a lost update drops some. The lock makes this deterministic —
without it the assertion fails intermittently under real contention.

stdlib unittest + multiprocessing only (pytest is not installed on the droplet).
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import file_lock  # noqa: E402

# Kept modest: a lost update reproduces at low counts, and this runs on every
# regression pass. On the droplet (Linux 'fork') the whole file is sub-second;
# the visible cost is macOS-dev 'spawn' re-imports.
_N_PROCS = 4
_PER_PROC = 30


# Top-level workers (importable/picklable for the 'spawn' start method). Each
# re-points its own process's ledger path to the shared file and opens a disjoint
# block of ids, so the union across workers is every id exactly once.

def _no_session_worker(ledger_file: str, base: int, count: int) -> None:
    import no_session_ledger as nsl
    nsl.LEDGER_FILE = Path(ledger_file)
    for i in range(base, base + count):
        nsl.open_obligation(f'task-{i}', pr_url=f'https://x/{i}')


def _rebase_worker(ledger_file: str, base: int, count: int) -> None:
    import rebase_obligation_ledger as rol
    rol.LEDGER_FILE = Path(ledger_file)
    for i in range(base, base + count):
        rol.open_obligation(f'task-{i}', pr_url=f'https://x/{i}')


def _guardian_worker(ledger_file: str, base: int, count: int) -> None:
    import suite_guardian_ledger as sgl
    p = Path(ledger_file)
    for i in range(base, base + count):
        sgl.open_proposal(f'test-{i}', run_task_id=f'r-{i}',
                          poison_test_name=f'poison-{i}', path=p)


def _run(worker, ledger_file: Path) -> int:
    ctx = multiprocessing.get_context()
    procs = [
        ctx.Process(target=worker,
                    args=(str(ledger_file), k * _PER_PROC, _PER_PROC))
        for k in range(_N_PROCS)
    ]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(timeout=60)
    return sum(pr.exitcode == 0 for pr in procs)


@unittest.skipUnless(file_lock.have_flock(), 'requires fcntl.flock')
class LedgerConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = Path(self._tmp.name) / 'ledger.json'

    def _assert_all_rows(self):
        rows = json.loads(self.ledger.read_text())
        self.assertEqual(len(rows), _N_PROCS * _PER_PROC,
                         'lost update: fewer rows than concurrent writes')

    def test_no_session_no_lost_updates(self):
        ok = _run(_no_session_worker, self.ledger)
        self.assertEqual(ok, _N_PROCS)
        self._assert_all_rows()

    def test_rebase_obligation_no_lost_updates(self):
        ok = _run(_rebase_worker, self.ledger)
        self.assertEqual(ok, _N_PROCS)
        self._assert_all_rows()

    def test_suite_guardian_no_lost_updates(self):
        ok = _run(_guardian_worker, self.ledger)
        self.assertEqual(ok, _N_PROCS)
        self._assert_all_rows()


class DecoratorNeverRaisesTest(unittest.TestCase):
    """The @_serialized decorator takes the lock OUTSIDE each mutator's own
    try/except OSError. If locked_update let an acquisition OSError escape, every
    guardian mutator would raise and abort the nightly cycle. Prove it degrades
    (writes unserialized, returns its normal value) instead."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = Path(self._tmp.name) / 'guardian.json'

    def test_open_proposal_degrades_on_acquire_oserror(self):
        import suite_guardian_ledger as sgl
        import atomic_io

        orig = atomic_io.file_lock.exclusive_lock

        def _boom(*a, **k):
            raise OSError('EROFS: sidecar unwritable')

        atomic_io.file_lock.exclusive_lock = _boom
        try:
            row = sgl.open_proposal('test-x', run_task_id='r', poison_test_name='p',
                                    path=self.ledger)
        finally:
            atomic_io.file_lock.exclusive_lock = orig
        self.assertIsNotNone(row)                 # did not raise; degraded write
        self.assertTrue(self.ledger.exists())     # row was actually persisted
        self.assertIn('test-x', json.loads(self.ledger.read_text()))


if __name__ == '__main__':
    unittest.main()
