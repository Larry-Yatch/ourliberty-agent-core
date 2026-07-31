#!/usr/bin/env python3
"""The WRITE contract of scripts/_lib_cost_capture.sh: rc 0 means it landed.

WHAT THIS MODULE IS FOR
-----------------------
``test_cost_row_model_attribution`` asserts what the row SAYS. This module
asserts whether the row EXISTS — the half that was silently true-by-assertion:

* the append's exit status was ignored and ``return 0`` fired regardless, so an
  EACCES/EROFS/ENOSPC write that landed nothing was reported to both wrappers
  as ``cost record appended`` (a regression against the pre-extraction shape,
  where the same append sat under ``set -e`` and aborted loudly);
* jq ran in STREAMING mode, so a capture holding N JSON documents appended N
  burn rows — same ``ts``, therefore the same ``task_id`` — while the wrapper
  logged one append and nothing downstream dedups;
* jq exiting 0 with NO output appended a bare newline and still returned 0.

All three are the same defect wearing different clothes: the caller's log
asserts an outcome the code never checked. So every assertion here is on the
OBSERVABLE pair (rc, bytes actually appended) — never on "did we take the
branch we meant to".

WHY PROPERTIES OVER SETS RATHER THAN FIXTURES
---------------------------------------------
Each fix has a twin that a single fixture cannot separate:

* an unwritable destination fails at OPEN (parent dir mode 500) or at WRITE
  (ENOSPC, a read-only file, a path that is a directory) — one fixture proves
  only one of them;
* document selection must be right in BOTH capture orders: ``head -1`` gets
  ``[envelope, stray-log]`` right and ``[stray-log, envelope]`` wrong, and
  ``tail -1`` the reverse, so a one-order test is green for a wrong fix;
* "no row could be built" must be distinguished from "one empty row was built"
  — a stricter guard that failed to would start dropping real failure-path
  rows, which is why the matrix module keeps ``degenerate_empty_object`` green.

WHAT WOULD TURN THESE RED (checked by mutation)
-----------------------------------------------
drop ``|| return 50``; revert ``jq -s`` + ``pick_envelope`` to streaming jq;
drop the ``[ -n "$cost_line" ]`` gate; drop the arity/empty-path gate; make
rc 40 return 0 instead.

NOT verified by reading the file back on purpose: costs.jsonl has concurrent
appenders (both wrappers plus active_tier.append_cost_row from several Python
processes), so a last-line read-back would report another writer's row as ours.
The lib keeps exactly one ``>>`` per row for the same reason.

Run::

    python3 -m unittest scripts.tests.test_cost_capture_write_contract
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:  # dotted/pytest path: relative import within the scripts.tests package
    from .test_run_cycle_wrong_branch_guard import _RunCycleTestBase
    from .test_run_medic_tier_pool import _MedicTierPoolBase
except ImportError:  # discover loads this module top-level (no package parent)
    from test_run_cycle_wrong_branch_guard import _RunCycleTestBase
    from test_run_medic_tier_pool import _MedicTierPoolBase

_HAS_JQ = shutil.which('jq') is not None
_IS_ROOT = hasattr(os, 'geteuid') and os.geteuid() == 0

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_LIB = _REPO_ROOT / 'scripts' / '_lib_cost_capture.sh'

WORK_MODEL = 'claude-sonnet-4-6'

# A payload-bearing result envelope, byte-identical in shape to the one the
# matrix module asserts on, so a row built from it is recognisable.
_PAYLOAD = {
    'total_cost_usd': 0.42,
    'duration_ms': 306000,
    'usage': {
        'input_tokens': 3,
        'output_tokens': 3857,
        'cache_read_input_tokens': 900000,
        'cache_creation_input_tokens': 84000,
    },
}
_ENVELOPE = dict(_PAYLOAD, modelUsage={WORK_MODEL: {'outputTokens': 3841}})
# A structured stderr diagnostic: valid JSON, no cost/usage shape. The `2>&1`
# in both wrappers is how this ends up in the same capture as the envelope.
_STRAY_LOG = {'level': 'warn', 'msg': 'mcp server restarted', 'attempt': 2}

# Mirrors the wrappers' own call shape EXACTLY — `set -e` in force, the call as
# the left operand of a `||` list (which suspends errexit inside the function).
# A driver that dropped `set -e` could not see an abort; one that dropped the
# `||` could not see a non-zero rc.
_DRIVER = """#!/usr/bin/env bash
set -e
source "$1"
shift
RC=0
capture_cost_row "$@" || RC=$?
echo "RC=$RC"
echo "REACHED_END"
"""


class _LibHarness(unittest.TestCase):
    """Calls the REAL lib in a bash subprocess. No mock sits at the seam."""

    def setUp(self):
        if shutil.which('bash') is None:
            raise unittest.SkipTest('bash not on PATH')
        self._tmp = tempfile.TemporaryDirectory(prefix='cost-write-contract-')
        self.addCleanup(self._cleanup)
        self.tmp = Path(self._tmp.name)
        self.driver = self.tmp / 'driver.sh'
        self.driver.write_text(_DRIVER)
        os.chmod(self.driver, 0o755)
        self.capture = self.tmp / 'capture.json'
        self.costs = self.tmp / 'out' / 'costs.jsonl'
        self.costs.parent.mkdir(parents=True)

    def _cleanup(self):
        # Restore any mode we tightened so TemporaryDirectory can unlink.
        for path in sorted(Path(self._tmp.name).rglob('*'), reverse=True):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        self._tmp.cleanup()

    def _call(self, *args, costs_file=None):
        """Run capture_cost_row with the standard eight leading arguments."""
        if costs_file is None:
            costs_file = str(self.costs)
        argv = list(args) or [
            str(self.capture), 'pulse', 'cycle', 'cycle-', '1',
            'tier2', 'run_cycle.sh', WORK_MODEL, costs_file,
        ]
        result = subprocess.run(
            ['bash', str(self.driver), str(_LIB), *argv],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, HOME=str(self.tmp)))
        return result

    @staticmethod
    def _rc(result):
        for line in result.stdout.splitlines():
            if line.startswith('RC='):
                return int(line[3:])
        raise AssertionError(
            f'driver never reported an rc; stdout={result.stdout!r} '
            f'stderr={result.stderr[:400]!r}')

    def _raw(self, path=None):
        path = Path(path) if path is not None else self.costs
        if not path.exists() or path.is_dir():
            return b''
        return path.read_bytes()

    def _rows(self):
        raw = self._raw().decode()
        return [json.loads(ln) for ln in raw.splitlines() if ln.strip()]

    def _write_capture(self, text):
        self.capture.write_text(text)


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class UnwritableDestinationTest(_LibHarness):
    """PROPERTY over the SET of destinations that cannot take the row.

    For EVERY member: rc is non-zero, zero bytes are appended, and the caller
    still reaches the statement after the call. Dropping ``|| return 50`` (or
    the arity/empty-path gate) turns members of this set red — before the fix
    every one of them returned 0 and the wrapper logged "cost record appended".
    """

    def _assert_refused(self, label, result, dest, before):
        rc = self._rc(result)
        self.assertNotEqual(
            rc, 0,
            f'{label}: a write that could not land must NOT report success '
            f'(stdout={result.stdout!r} stderr={result.stderr[:300]!r})')
        self.assertIn('REACHED_END', result.stdout,
                      f'{label}: the caller must survive — nothing here may '
                      f'abort a tick whose paid work already happened')
        self.assertEqual(
            self._raw(dest), before,
            f'{label}: destination changed despite the refusal')

    def test_the_row_is_never_reported_as_appended_when_it_cannot_land(self):
        cases = []

        # (1) parent directory not writable — the open fails (EACCES).
        blocked_dir = self.tmp / 'blocked'
        blocked_dir.mkdir()
        cases.append(('parent dir mode 500', blocked_dir / 'costs.jsonl',
                      dict(costs_file=str(blocked_dir / 'costs.jsonl')),
                      lambda: os.chmod(blocked_dir, 0o500)))

        # (2) the costs file itself is read-only — the open fails on an
        #     EXISTING file, the B-then-A twin of (1).
        ro = self.tmp / 'ro-costs.jsonl'
        ro.write_text('{"pre":"existing"}\n')
        cases.append(('costs.jsonl mode 444', ro,
                      dict(costs_file=str(ro)),
                      lambda: os.chmod(ro, 0o444)))

        # (3) the costs path is a DIRECTORY — EISDIR, which no mode change can
        #     make writable, so it also covers the euid-0 case.
        as_dir = self.tmp / 'costs-as-dir'
        as_dir.mkdir()
        cases.append(('costs path is a directory', as_dir,
                      dict(costs_file=str(as_dir)), None))

        # (4) empty costs-file argument — the `>> ""` variant.
        cases.append(('empty costs_file argument', self.costs,
                      dict(costs_file=''), None))

        exercised = []
        for label, dest, kwargs, prepare in cases:
            if prepare is not None and _IS_ROOT:
                continue  # root ignores mode bits; (3) still covers the class
            with self.subTest(destination=label):
                self._write_capture(json.dumps(_ENVELOPE) + '\n')
                if prepare is not None:
                    prepare()
                before = self._raw(dest)
                result = self._call(**kwargs)
                self._assert_refused(label, result, dest, before)
                exercised.append(label)

        # (5) wrong arity — eight arguments, so `$9` is unset. Passed
        #     positionally rather than through the standard helper because the
        #     defect IS the missing ninth argument.
        with self.subTest(destination='8-argument call'):
            self._write_capture(json.dumps(_ENVELOPE) + '\n')
            before = self._raw()
            result = self._call(
                str(self.capture), 'pulse', 'cycle', 'cycle-', '1',
                'tier2', 'run_cycle.sh', WORK_MODEL)
            self._assert_refused('8-argument call', result, self.costs, before)
            exercised.append('8-argument call')

        # Anti-vacuity: the loop above must have run, and `continue` on root
        # must not silently empty it.
        self.assertGreaterEqual(len(exercised), 2, f'exercised={exercised}')
        self.assertIn('costs path is a directory', exercised)
        self.assertIn('8-argument call', exercised)

    def test_a_writable_destination_still_appends_exactly_one_row(self):
        """ANTI-VACUITY twin. Without it the property above is satisfied by a
        lib that refuses every write, including the ones that should land."""
        self._write_capture(json.dumps(_ENVELOPE) + '\n')
        result = self._call()
        self.assertEqual(self._rc(result), 0,
                         f'stderr={result.stderr[:400]!r}')
        rows = self._rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['cost_usd'], 0.42)
        self.assertEqual(rows[0]['model'], WORK_MODEL)


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class OneRowPerCallTest(_LibHarness):
    """PROPERTY over the SET of capture shapes: NEVER more than one row.

    The capture is the child's merged stdout+stderr, so multiple JSON documents
    are reachable. Reverting the slurp + ``pick_envelope`` rewrite to streaming
    jq turns the multi-document members red.
    """

    # (label, capture text, how many rows are acceptable)
    def _shapes(self):
        env = json.dumps(_ENVELOPE)
        log = json.dumps(_STRAY_LOG)
        other = json.dumps(dict(_ENVELOPE, total_cost_usd=0.99))
        return [
            ('one document', env + '\n'),
            ('stray log then envelope', log + '\n' + env + '\n'),
            ('envelope then stray log', env + '\n' + log + '\n'),
            ('envelope twice', env + '\n' + env + '\n'),
            ('two payload-bearing envelopes', env + '\n' + other + '\n'),
            ('three documents', log + '\n' + env + '\n' + log + '\n'),
            ('zero documents (whitespace)', '   \n\n'),
            ('non-object top level: number', '5\n'),
            ('non-object top level: string', '"x"\n'),
            ('non-object top level: array', '[1,2]\n'),
            ('envelope then plain text', env + '\nrate limit reached\n'),
            ('plain text only', 'Claude Code exited: not JSON at all\n'),
        ]

    def test_never_more_than_one_row_for_any_capture_shape(self):
        seen = []
        for label, text in self._shapes():
            with self.subTest(shape=label):
                self._write_capture(text)
                if self.costs.exists():
                    self.costs.unlink()
                result = self._call()
                raw = self._raw().decode()
                lines = raw.splitlines()
                self.assertLessEqual(
                    len(lines), 1,
                    f'{label}: {len(lines)} rows appended — one paid '
                    f'invocation may never produce more than one burn row '
                    f'(nothing downstream dedups by task_id): {lines}')
                for line in lines:
                    self.assertTrue(line.strip(),
                                    f'{label}: appended a blank line')
                    row = json.loads(line)
                    self.assertIsInstance(row, dict)
                    self.assertIn('ts', row)
                # rc and file must agree in BOTH directions.
                rc = self._rc(result)
                self.assertEqual(
                    rc == 0, len(lines) == 1,
                    f'{label}: rc={rc} but {len(lines)} rows on disk — rc 0 '
                    f'must mean the row landed, and nothing else may')
                seen.append(label)
        self.assertEqual(len(seen), len(self._shapes()))

    def test_document_selection_prefers_the_payload_bearing_envelope(self):
        """BOTH orders. `head -1` passes one and fails the other; `tail -1`
        the reverse. Only shape-based selection satisfies both."""
        env = json.dumps(_ENVELOPE)
        log = json.dumps(_STRAY_LOG)
        for label, text in (('log first', log + '\n' + env + '\n'),
                            ('envelope first', env + '\n' + log + '\n')):
            with self.subTest(order=label):
                self._write_capture(text)
                if self.costs.exists():
                    self.costs.unlink()
                result = self._call()
                self.assertEqual(self._rc(result), 0,
                                 f'{label}: stderr={result.stderr[:300]!r}')
                rows = self._rows()
                self.assertEqual(len(rows), 1, f'{label}: {rows}')
                row = rows[0]
                self.assertEqual(row['cost_usd'], 0.42,
                                 f'{label}: the row was built from the stray '
                                 f'log, not the result envelope')
                self.assertEqual(row['input_tokens'], 3, label)
                self.assertEqual(row['output_tokens'], 3857, label)
                self.assertEqual(row['cache_read'], 900000, label)
                self.assertEqual(row['cache_creation'], 84000, label)
                self.assertEqual(row['model'], WORK_MODEL, label)


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class ContentFreeCaptureTest(_LibHarness):
    """PROPERTY over captures that are non-empty but hold no JSON document.

    Asserted on RAW BYTES, not on parsed rows: the bug appended a bare newline,
    and every reader (and every whitespace-stripping assertion) skips blank
    lines, so a parsed-row assertion cannot see it.
    """

    CONTENT_FREE = ('\n', '\n\n   \n', '\t', ' ', '\r\n')

    def test_a_content_free_capture_writes_nothing_and_says_so(self):
        for text in self.CONTENT_FREE:
            with self.subTest(capture=repr(text)):
                self._write_capture(text)
                if self.costs.exists():
                    self.costs.unlink()
                result = self._call()
                self.assertEqual(
                    self._rc(result), 40,
                    f'{text!r}: a capture with no JSON document must report '
                    f'"no row built" — returning 0 here is the same lie as '
                    f'reporting a failed append as appended '
                    f'(stderr={result.stderr[:300]!r})')
                self.assertEqual(
                    self._raw(), b'',
                    f'{text!r}: nothing may be appended — not even a newline')

    def test_the_zero_byte_boundary_stays_on_the_other_code(self):
        """rc 10 vs rc 40 is the size>0 boundary; pin it so a future guard
        cannot quietly collapse the two and lose the distinction."""
        self._write_capture('')
        result = self._call()
        self.assertEqual(self._rc(result), 10)
        self.assertEqual(self._raw(), b'')


# ---------------------------------------------------------------------------
# END-TO-END: the same contract at the two wrapper surfaces.
# The lib's rc is worthless unless the wrapper's `case` block names it, so
# these assert on the WRAPPER's log and exit code, not on the lib's rc.
# ---------------------------------------------------------------------------

_ENVELOPE_STUB = """#!/usr/bin/env bash
cat <<'CLAUDE_STUB_JSON'
{body}
CLAUDE_STUB_JSON
exit 0
"""

# size > 0, no JSON document — the rc-40 shape at the wrapper surface.
_WHITESPACE_STUB = """#!/usr/bin/env bash
printf '   \\n\\n'
exit 0
"""


class _WrapperWriteContractMixin:
    """Assertions shared by the run_cycle.sh and run_medic.sh halves."""

    def _install_claude(self, body):
        raise NotImplementedError

    def _invoke(self):
        raise NotImplementedError

    def _blackboard(self) -> Path:
        raise NotImplementedError

    def _costs_path(self) -> Path:
        return self._blackboard() / 'costs.jsonl'

    @staticmethod
    def _out(result):
        out = result.stdout
        return out.decode() if isinstance(out, bytes) else (out or '')

    def _install_envelope_stub(self):
        self._install_claude(_ENVELOPE_STUB.format(body=json.dumps(_ENVELOPE)))

    def _seal_blackboard(self):
        bb = self._blackboard()
        bb.mkdir(parents=True, exist_ok=True)
        os.chmod(bb, 0o500)

        def _unseal():
            # Cleanups run AFTER tearDown, which already removed the tmp tree
            # in the run_cycle harness — a missing dir here is not a failure.
            try:
                os.chmod(bb, 0o700)
            except OSError:
                pass

        self.addCleanup(_unseal)

    # --- the property ----------------------------------------------------
    @unittest.skipIf(_IS_ROOT, 'root ignores directory mode bits')
    def test_a_lost_row_is_reported_as_lost(self):
        self._install_envelope_stub()
        self._seal_blackboard()

        result = self._invoke()
        out = self._out(result)

        self.assertEqual(result.returncode, self.NORMAL_EXIT,
                         f'a best-effort cost failure must not change the '
                         f'exit code; out={out[-800:]}')
        self.assertFalse(
            self._costs_path().exists(),
            'no row can exist on an unwritable destination')
        self.assertNotIn(
            'cost record appended', out,
            'the wrapper claimed an append that never landed')
        self.assertIn('FAILED (row LOST)', out,
                      f'the wrapper must NAME the lost row; out={out[-800:]}')

    def test_a_content_free_capture_is_reported_as_no_row_built(self):
        self._install_claude(_WHITESPACE_STUB)

        result = self._invoke()
        out = self._out(result)

        self.assertEqual(result.returncode, self.NORMAL_EXIT,
                         f'out={out[-800:]}')
        costs = self._costs_path()
        self.assertEqual(
            costs.read_bytes() if costs.exists() else b'', b'',
            'a capture with no JSON document must append nothing at all — '
            'not even the blank line the old `printf %s\\n ""` wrote')
        self.assertNotIn('cost record appended', out)
        self.assertIn('no cost row could be built', out,
                      f'out={out[-800:]}')

    def test_the_happy_path_still_appends_exactly_one_row(self):
        """ANTI-VACUITY. Without this, both properties above are satisfied by
        a harness that never reaches the cost block at all."""
        self._install_envelope_stub()

        result = self._invoke()
        out = self._out(result)

        self.assertEqual(result.returncode, self.NORMAL_EXIT,
                         f'out={out[-800:]}')
        self.assertIn('cost record appended', out)
        rows = [json.loads(ln)
                for ln in self._costs_path().read_text().splitlines()
                if ln.strip()]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['cost_usd'], 0.42)
        self.assertEqual(rows[0]['source'], self.SOURCE)


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class CycleWrapperWriteContractTest(_WrapperWriteContractMixin,
                                    _RunCycleTestBase):
    NORMAL_EXIT = 0
    SOURCE = 'run_cycle.sh'

    def _install_claude(self, body):
        stub = self.stub_bin / 'claude'
        stub.write_text(body)
        os.chmod(stub, 0o755)

    def _blackboard(self):
        return self.home / 'agents' / 'blackboard'

    def _invoke(self):
        # run_cycle.sh self-throttles on the tier window; clear the anchor.
        flag = self.home / 'agents' / 'state' / 'cycle-last-run.flag'
        if flag.exists():
            flag.unlink()
        return self._run_cycle()


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class MedicWrapperWriteContractTest(_WrapperWriteContractMixin,
                                    _MedicTierPoolBase):
    NORMAL_EXIT = 0
    SOURCE = 'run_medic.sh'

    def setUp(self):
        super().setUp()
        self._install_active_tier_stub(
            select_out='TIER=tier3\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-t3\n',
            select_code=0, report_out='')

    def _install_claude(self, body):
        stub = self.fake_bin / 'claude'
        stub.write_text(body)
        os.chmod(stub, 0o755)

    def _blackboard(self):
        return self.fake_home / 'agents' / 'blackboard'

    def _invoke(self):
        return self._run()


if __name__ == '__main__':
    unittest.main()
