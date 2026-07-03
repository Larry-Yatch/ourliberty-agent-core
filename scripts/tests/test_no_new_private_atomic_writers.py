"""test_no_new_private_atomic_writers.py — census ratchet (test-jail Layer C).

The class-closing seal routes state writes through atomic_io.atomic_write_*,
which carries the destination-aware test-jail guard (refuse_live_state_write).
13 modules still carry a hand-rolled private `_atomic_write`/`_atomic_write_json`
(a copy-paste of the tmp + os.replace pattern) that bypasses that chokepoint —
they are the migration backlog. This gate FREEZES that backlog: a NEW file that
adds such a helper fails here, forcing new code onto atomic_io instead of
growing a 14th unguarded copy. As each backlog file is migrated, drop it from
the allowlist.

AST-based (a `def _atomic_write*` is rare and unambiguous), mirroring the
chokepoint census's design.
"""
import _bootstrap  # noqa: F401
import ast
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent  # scripts/
_HELPER_NAMES = {'_atomic_write', '_atomic_write_json'}

# Pre-existing private atomic-write helpers (migration targets). A new file must
# NOT appear here — route its state writes through atomic_io.atomic_write_*.
_ALLOWLIST = {
    'active_tier.py', 'cycle_tier_state.py', 'dashboard_api.py',
    'dispatch_lease.py', 'heal_missions_card_gc.py', 'heal_projects_store.py',
    'launch_queue_drain.py', 'outbox_notifier.py', 'projects_status_writeback.py',
    'pulse_check_i.py', 'safe_write_inbox.py', 'sequence_shortcut_helpers.py',
    'suggest_funnel_card.py',
}


def _private_writer_defs(text: str) -> list[str]:
    return [
        n.name for n in ast.walk(ast.parse(text))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name in _HELPER_NAMES
    ]


class NoNewPrivateAtomicWritersTest(unittest.TestCase):
    def test_no_unallowlisted_private_atomic_writer(self):
        violations: list[str] = []
        for p in sorted(_SCRIPTS.glob('*.py')):
            if p.name in _ALLOWLIST:
                continue
            try:
                names = _private_writer_defs(p.read_text())
            except SyntaxError:  # pragma: no cover
                continue
            for nm in sorted(set(names)):
                violations.append(f'{p.name}: def {nm}(...)')
        if violations:
            self.fail(
                'New private atomic-write helper(s) found — route state writes '
                'through atomic_io.atomic_write_* (it carries the test-jail '
                'state guard) instead of copy-pasting a tmp + os.replace '
                'helper:\n  - ' + '\n  - '.join(violations))

    def test_allowlist_has_no_stale_entries(self):
        """Every allowlisted file must still define such a helper — otherwise it
        was migrated and should be dropped from the allowlist (keeps the ratchet
        tightening, never loosening)."""
        stale = []
        for name in sorted(_ALLOWLIST):
            p = _SCRIPTS / name
            if not p.exists():
                stale.append(f'{name} (file gone)')
                continue
            if not _private_writer_defs(p.read_text()):
                stale.append(f'{name} (no private helper left — migrated?)')
        self.assertEqual(stale, [], f'Stale allowlist entries: {stale}')

    def test_detector_flags_synthetic(self):
        self.assertEqual(
            _private_writer_defs('def _atomic_write_json(p, o):\n    pass\n'),
            ['_atomic_write_json'])

    def test_detector_ignores_public_atomic_io_name(self):
        self.assertEqual(
            _private_writer_defs('def atomic_write_json(p, o):\n    pass\n'), [])


if __name__ == '__main__':
    unittest.main()
