"""test_migrated_atomic_writers_format.py — lock the on-disk byte format of the
9 private writers migrated to atomic_io (PR-4).

Each migration was proven byte-identical to its prior inline writer before the
swap. This test makes that permanent: it asserts each writer still emits the
exact documented format (indent / trailing-newline / text). The full suite and
the modules' own tests would NOT catch a trailing-newline drift — they read back
via json.load, which ignores it — so this byte-level contract is the guard
against a future edit silently changing a consumed state file's format.

Writes go to a tmpdir (never under the real agents tree), so the destination-
aware state guard passes through.
"""
import _bootstrap  # noqa: F401
import importlib
import json
import tempfile
import unittest
from pathlib import Path

_JSON_PAYLOAD = {'tier': 't2', 'n': 3, 'ok': True, 'nil': None,
                 'nest': {'a': [1, 2, {'b': 'x'}]}}
_TEXT_PAYLOAD = 'line1\nline2\n'


def _json_nl(o):
    return (json.dumps(o, indent=2) + '\n').encode()


def _json_no_nl(o):
    return json.dumps(o, indent=2).encode()


def _json_compact(o):
    return json.dumps(o).encode()


def _text(s):
    return s.encode()


# (module, private_fn, kind, expected_bytes_builder)
_CASES = [
    ('dashboard_api', '_atomic_write_json', 'json', _json_nl),
    ('heal_missions_card_gc', '_atomic_write_json', 'json', _json_nl),
    ('heal_projects_store', '_atomic_write_json', 'json', _json_nl),
    ('projects_status_writeback', '_atomic_write', 'json', _json_nl),
    ('suggest_funnel_card', '_atomic_write_json', 'json', _json_nl),
    ('launch_queue_drain', '_atomic_write_json', 'json', _json_nl),
    ('safe_write_inbox', '_atomic_write_json', 'json', _json_no_nl),
    ('dispatch_lease', '_atomic_write', 'json', _json_compact),
    ('pulse_check_i', '_atomic_write', 'text', _text),
]


def _make_test(modname, fnname, kind, expfn):
    def test(self):
        mod = importlib.import_module(modname)
        fn = getattr(mod, fnname)
        payload = _TEXT_PAYLOAD if kind == 'text' else _JSON_PAYLOAD
        dest = Path(tempfile.mkdtemp()) / 'out'
        fn(dest, payload)
        self.assertEqual(
            dest.read_bytes(), expfn(payload),
            f'{modname}.{fnname} on-disk byte format drifted from the '
            f'pre-migration contract')
    return test


class MigratedWriterFormatContractTest(unittest.TestCase):
    pass


for _c in _CASES:
    setattr(MigratedWriterFormatContractTest, f'test_{_c[0]}_format',
            _make_test(*_c))


if __name__ == '__main__':
    unittest.main()
