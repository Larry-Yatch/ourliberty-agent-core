#!/usr/bin/env python3
"""test_no_production_path_leaks.py — regression gate for test isolation.

Tests under `scripts/tests/*.py` MUST NOT write to or read from production
agent state — specifically, anything under these roots that a live daemon
polls:

  - ~/agents/inboxes/*          (inbox_watcher → dispatches Claude sessions)
  - ~/agents/blackboard/*       (build-sequence-advancer, etc.)
  - ~/agents/state/*            (in-flight, approvals, auto-merge-queue, ...)

When a test writes to one of those paths, the production daemon picks up
the fixture envelope and burns real Claude credits trying to dispatch it.
This has happened twice:

  - 2026-05-13: smoke test archived to outbox-not-archive path → $0.65 burn
    (see memory `feedback_smoke_test_archive_not_outbox`).
  - 2026-05-26 23:29-23:31 MDT: ~30 fixture envelopes with fake session_ids
    (`sess-abc-123`, `sess-preflight-xyz`, `forge-build-sess-19`) landed in
    `~/agents/inboxes/forge/`; inbox_watcher retry-looped on them →
    ~$15-20 of Opus burn before manual cleanup.

Two strikes warrants a permanent regression gate. This test scans every
file under `scripts/tests/*.py` for string literals matching the leak
patterns. Docstrings (operator-facing prose explaining the test's purpose,
which may reference prod paths) and explicitly whitelisted call sites
(documented input fixtures / defensive anti-leak assertions) are exempt;
everything else fails the test with file:line and the canonical fix.

Pattern: AST literal walk; docstrings are detected as the first
`ast.Constant` string in a `Module` / `ClassDef` / `FunctionDef` /
`AsyncFunctionDef` body. Comments are not in the AST and are therefore
ignored by construction. Adapted from
`scripts/tests/test_alert_translations.py` (PR #121 forward-looking
scanner shape).

Canonical fix: see `runbooks/test-isolation-discipline.md`.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_no_production_path_leaks
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent.parent

# Put scripts/ on sys.path so the regression gate below can import the
# production module it guards (chain_event_emit), same as the handler tests.
import sys as _sys
_REPO_SCRIPTS = _TESTS_DIR.parent
if str(_REPO_SCRIPTS) not in _sys.path:
    _sys.path.insert(0, str(_REPO_SCRIPTS))

# String-literal patterns that indicate a test is touching production
# agent state. Each pattern matches a substring — a literal containing
# any of these triggers the gate unless whitelisted below.
LEAK_PATTERNS: tuple[str, ...] = (
    '~/agents/inboxes/',
    '~/agents/blackboard/',
    '~/agents/state/',
    '/home/larry/agents/inboxes/',
    '/home/larry/agents/blackboard/',
    '/home/larry/agents/state/',
)

# Whitelist: (filename, leak_literal) → one-line reason. Each entry must
# be justified — input fixture metadata, defensive anti-leak assertion,
# etc. Adding a new entry requires the reason to be readable by the next
# operator who runs the test. Prefer refactoring (tmpdir + monkeypatch)
# over whitelisting; whitelist only when the literal is not a write
# target (input metadata, assertion-of-absence, or canonical example).
#
# Keyed on CONTENT (the exact leak literal), NOT line number. Line-number
# keying is brittle: any edit that shifts lines in a scanned test file
# (e.g. PR #321) breaks the gate even though nothing leaked. Content keys
# are immune to line shifts.
#
# Tradeoff of content-keying: if the SAME literal appeared twice in one
# file — one a real leak, one whitelisted — a content key would exempt
# both. Guarded by test_whitelisted_literals_unique_per_file below, which
# asserts every whitelisted literal occurs exactly once in its file, so a
# second (leaking) occurrence forces a deliberate re-review.
WHITELIST: dict[tuple[str, str], str] = {
    # test_beacon_plan_synthesis_discipline.py — INPUT fixtures: the
    # test feeds command/path strings to a discipline-checker parser
    # under test. The strings are inputs (parser arguments), not paths
    # the test reads/writes. Refactoring would defeat the test's
    # purpose (it must check the parser's handling of these exact
    # strings).
    ('test_beacon_plan_synthesis_discipline.py',
     'ls ~/agents/state/in-flight/'): (
        'input fixture: bash-command string fed to parser-under-test'
    ),
    ('test_beacon_plan_synthesis_discipline.py',
     '/home/larry/agents/state/beacon-pending-approvals.json'): (
        'input fixture: bash-command string fed to parser-under-test'
    ),
    # test_outbox_notifier.py — INPUT fixtures: source_task_file fields
    # in outbox-shape dicts passed to the notifier as test input. The
    # notifier reads the FIELD as metadata for the dispatched envelope
    # but does NOT open the path (the file's content isn't loaded;
    # only the agent name is parsed from it). Refactoring to a tmpdir
    # would not change behavior; the string is a label, not a target.
    ('test_outbox_notifier.py',
     '/home/larry/agents/inboxes/beacon/real-001.json'): (
        'input fixture: source_task_file metadata field (not opened)'
    ),
    ('test_outbox_notifier.py',
     '/home/larry/agents/inboxes/beacon/notify-prior.json'): (
        'input fixture: source_task_file metadata field (not opened)'
    ),
}


def _scan_file_for_literals(path: Path) -> list[dict]:
    """Return [{file, line, literal, in_docstring}] for every string
    literal in `path` matching a LEAK_PATTERN.

    Docstrings are detected by walking each container body (Module,
    ClassDef, FunctionDef, AsyncFunctionDef) and marking the first
    statement's string constant — if it's an `Expr` wrapping a `Constant`
    of type str, it's a docstring and the inner Constant is exempt."""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (OSError, SyntaxError):
        return []

    # Collect ids of Constant nodes that are docstrings.
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if not body:
            continue
        if not isinstance(node, (
            ast.Module, ast.ClassDef,
            ast.FunctionDef, ast.AsyncFunctionDef,
        )):
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_ids.add(id(first.value))

    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        for pattern in LEAK_PATTERNS:
            if pattern in node.value:
                out.append({
                    'file': path.name,
                    'line': node.lineno,
                    'literal': node.value,
                    'pattern': pattern,
                    'in_docstring': id(node) in docstring_ids,
                })
                break
    return out


def _scan_all_test_files() -> list[dict]:
    """Walk scripts/tests/*.py, return all leak-literal hits."""
    out: list[dict] = []
    for py in sorted(_TESTS_DIR.glob('test_*.py')):
        # The regression test file itself necessarily mentions the leak
        # patterns in its LEAK_PATTERNS constant — skip by filename.
        if py.name == Path(__file__).name:
            continue
        out.extend(_scan_file_for_literals(py))
    return out


class ProductionPathLeakTest(unittest.TestCase):
    """Fails when any non-whitelisted test contains a string literal
    pointing at a production agent-state path. Comments and docstrings
    are exempt by construction (comments aren't in the AST; docstrings
    are marked and skipped). Whitelist entries are explicit and
    justified above."""

    def test_no_unwhitelisted_leak_patterns(self):
        violations: list[str] = []
        for hit in _scan_all_test_files():
            if hit['in_docstring']:
                continue
            key = (hit['file'], hit['literal'])
            if key in WHITELIST:
                continue
            violations.append(
                f"{hit['file']}:{hit['line']} — string literal "
                f"{hit['literal']!r} contains production-path pattern "
                f"{hit['pattern']!r}. "
                'Refactor to use tmp_path/tempfile.mkdtemp() + monkeypatched '
                'module constants (or HOME env redirect). See '
                'runbooks/test-isolation-discipline.md.'
            )
        if violations:
            self.fail(
                'Test isolation discipline violation — '
                f'{len(violations)} test(s) contain raw production paths:\n'
                '  - ' + '\n  - '.join(violations)
            )

    def test_whitelist_entries_still_exist(self):
        """Stale-whitelist guard: if a whitelisted (file, literal) no
        longer matches a leak literal in the source file (because the
        file was refactored or the literal removed), the whitelist entry
        must be removed. Otherwise the whitelist drifts and silently
        allows a future leak that reuses the same literal. Keyed on
        content, so a pure line shift does NOT make an entry stale."""
        current_hits = {
            (h['file'], h['literal']) for h in _scan_all_test_files()
            if not h['in_docstring']
        }
        stale = sorted(
            f'{f}: {lit!r} (reason: {WHITELIST[(f, lit)]})'
            for (f, lit) in WHITELIST
            if (f, lit) not in current_hits
        )
        if stale:
            self.fail(
                'WHITELIST contains entries that no longer match a leak '
                'literal in the source file (the literal may have been '
                'removed or edited). Remove or update:\n'
                '  - ' + '\n  - '.join(stale)
            )

    def test_whitelisted_literals_unique_per_file(self):
        """Content-keying tradeoff guard: a (file, literal) whitelist key
        exempts EVERY occurrence of that literal in the file. If the same
        literal appeared twice — one a real leak — both would be silently
        allowed. Assert each whitelisted literal occurs exactly once in
        its file, so a second occurrence trips this test and forces a
        deliberate re-review rather than slipping through."""
        from collections import Counter
        counts: Counter = Counter(
            (h['file'], h['literal']) for h in _scan_all_test_files()
            if not h['in_docstring']
        )
        dupes = sorted(
            f'{f}: {lit!r} occurs {counts[(f, lit)]}x (reason: {reason})'
            for (f, lit), reason in WHITELIST.items()
            if counts[(f, lit)] > 1
        )
        if dupes:
            self.fail(
                'Whitelisted literal appears more than once in its file — '
                'content-keying would exempt all occurrences, including a '
                'possible real leak. Disambiguate (rename the fixture) or '
                'refactor:\n  - ' + '\n  - '.join(dupes)
            )

    def test_gate_survives_line_shift_of_whitelisted_literal(self):
        """Robustness proof (PR #321 class of breakage): inserting blank
        lines above whitelisted literals shifts their line numbers but
        MUST keep the gate green, because the whitelist is keyed on
        content, not line. Clone a real scanned file with a leading blank
        line and confirm every leak literal still resolves to a WHITELIST
        entry at its new (shifted) line."""
        import tempfile
        src = _TESTS_DIR / 'test_outbox_notifier.py'
        orig_hits = [
            h for h in _scan_file_for_literals(src)
            if not h['in_docstring']
        ]
        self.assertTrue(orig_hits, 'expected leak literals in fixture file')
        with tempfile.TemporaryDirectory() as td:
            clone = Path(td) / 'test_outbox_notifier.py'
            clone.write_text(
                '\n' + src.read_text(encoding='utf-8'), encoding='utf-8'
            )
            shifted_hits = [
                h for h in _scan_file_for_literals(clone)
                if not h['in_docstring']
            ]
        self.assertEqual(
            len(shifted_hits), len(orig_hits),
            'line shift changed the number of detected literals',
        )
        for h in shifted_hits:
            self.assertIn(
                (h['file'], h['literal']), WHITELIST,
                msg=(
                    f'line-shifted literal {h["literal"]!r} at line '
                    f'{h["line"]} fell out of WHITELIST — keying is not '
                    'line-independent'
                ),
            )
        # Confirm the shift actually moved lines (else the test is vacuous).
        self.assertNotEqual(
            sorted(h['line'] for h in orig_hits),
            sorted(h['line'] for h in shifted_hits),
        )


class GateSelfCheckTest(unittest.TestCase):
    """Self-check: synthesize a tiny in-memory test file with a known
    leak literal and confirm the scanner flags it. This proves the gate
    isn't accidentally too permissive (would catch a regression where
    the scanner silently stopped flagging anything)."""

    def test_scanner_flags_synthetic_leak(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            synth = Path(td) / 'test_synthetic_leak.py'
            synth.write_text(
                'leaked = "~/agents/inboxes/forge/synth-fixture.json"\n'
            )
            hits = _scan_file_for_literals(synth)
            self.assertEqual(len(hits), 1, msg=str(hits))
            self.assertFalse(hits[0]['in_docstring'])
            self.assertIn('inboxes', hits[0]['pattern'])

    def test_scanner_skips_synthetic_docstring(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            synth = Path(td) / 'test_synthetic_docstring.py'
            synth.write_text(
                '"""Module docstring mentioning ~/agents/inboxes/forge/."""\n'
                'x = 1\n'
            )
            hits = _scan_file_for_literals(synth)
            # The docstring contains the pattern — it must be reported
            # by the scanner BUT marked as in_docstring=True so the
            # main test loop skips it.
            self.assertEqual(len(hits), 1)
            self.assertTrue(hits[0]['in_docstring'])

    def test_scanner_ignores_comments(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            synth = Path(td) / 'test_synthetic_comment.py'
            synth.write_text(
                '# A comment mentioning ~/agents/inboxes/forge/.\n'
                'x = 1\n'
            )
            hits = _scan_file_for_literals(synth)
            self.assertEqual(hits, [])



class LiveChainEventEmitGuardTest(unittest.TestCase):
    """Regression gate (2026-06-02 live-DB leak): a test run must never be
    able to build a live Supabase client in chain_event_emit, or handler
    tests transitively upsert fixture rows into the production chain_events
    table. The path-literal scanner above is blind to this network sink."""

    def test_disable_env_blocks_live_client(self):
        import os as _os
        import chain_event_emit
        prev = _os.environ.get('OURLIBERTY_DISABLE_LIVE_EMIT')
        _os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = '1'
        try:
            chain_event_emit.reset_client_for_testing()
            self.assertIsNone(
                chain_event_emit._get_client(),
                'chain_event_emit._get_client() returned a live client with '
                'OURLIBERTY_DISABLE_LIVE_EMIT set — the test-isolation guard '
                'is broken; emit_event would write production chain_events.',
            )
        finally:
            if prev is None:
                _os.environ.pop('OURLIBERTY_DISABLE_LIVE_EMIT', None)
            else:
                _os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = prev
            chain_event_emit.reset_client_for_testing()

    def test_pytest_ambient_guard_blocks_live_client(self):
        import os as _os
        import chain_event_emit
        if not _os.environ.get('PYTEST_CURRENT_TEST'):
            self.skipTest('not under pytest; ambient PYTEST_CURRENT_TEST guard N/A')
        chain_event_emit.reset_client_for_testing()
        self.assertIsNone(chain_event_emit._get_client())


if __name__ == '__main__':
    unittest.main()
