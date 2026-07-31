#!/usr/bin/env python3
"""Property matrix for the costs.jsonl cost row, run against BOTH wrappers.

WHAT IS UNDER TEST
------------------
``scripts/_lib_cost_capture.sh`` is the single definition of the cost row that
``run_cycle.sh`` and ``run_medic.sh`` append to ~/agents/blackboard/costs.jsonl
— the factory's cost/attribution source of truth. Until 2026-07-30 the row's jq
program lived as two byte-identical hand-copies, one per wrapper, and only one
of them had any test at all.

WHY A MATRIX RATHER THAN FIXTURES
---------------------------------
The predecessor module (test_run_cycle_cost_model_attribution.py) pinned a
single fixture in a single key order, and its green was POSITIONAL: jq's
``max_by`` returns the LAST maximal entry, so breaking the ``outputTokens``
lookup entirely tied every entry at 0 and the fixture still recorded the right
model because the right model happened to be listed second. Mutating the field
name left all three tests green; swapping the two fixture keys turned the same
mutant red. A fixture that exists in only one key order cannot distinguish
"highest output wins" from "last key wins".

So every magnitude case here appears in BOTH key orders, every tie case appears
in BOTH key orders, and every case runs against BOTH wrappers. The
discriminating magnitude cases deliberately use ``claude-opus-5`` — a model the
wrappers do NOT dispatch — so the selector's name-match rule cannot fire and
only a working ``outputTokens`` lookup can satisfy both orders.

THE PROPERTY
------------
For any PARSEABLE envelope, exactly one row is appended, every non-model field
survives intact (cost, all four token counts, duration, success, account,
agent, task_type, task_id prefix, source), and the recorded model is one of:
a key prefixed by the dispatched model, the uniquely-maximal positive-output
key, or the dispatched-model literal — never anything else, and never null.

Asserting the WHOLE row matters: the sharpest defect in this area was a
non-object ``modelUsage`` value aborting the jq program, which silently dropped
cost_usd and all four token counts along with the model. A model-only
assertion cannot see that.

WHAT COUNTS AS A TEST HERE
--------------------------
Every test method in this module must be one that a broken selector, a broken
field lookup, or a lost caller can turn RED. Three kinds of check deliberately
do NOT get a method of their own, because a check that is green no matter what
production does reads as evidence while carrying none:

* no-regression pins (the unparseable-capture skip) live as a case IN the
  matrix, so the contract is still asserted but inside a test that a real
  defect fails;
* per-wrapper plumbing that predates the change (medic's pool-selected
  ``account``) is asserted on every matrix row via ``ACCOUNT`` rather than in a
  standalone pin;
* self-checks over this module's own case list run as a PRECONDITION of the
  matrix test (``_assert_matrix_is_discriminating``).

The one exception is ``MissingLibDegradesGracefullyTest``, whose hazard is
created by the extraction itself and can only be killed by a mutation of the
fix; its docstring says so rather than letting a reader assume otherwise.

Run::

    python3 -m unittest scripts.tests.test_cost_row_model_attribution
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

try:  # dotted/pytest path: relative import within the scripts.tests package
    from .test_run_cycle_wrong_branch_guard import _RunCycleTestBase
    from .test_run_medic_tier_pool import _MedicTierPoolBase, _RUN_MEDIC
except ImportError:  # discover loads this module top-level (no package parent)
    from test_run_cycle_wrong_branch_guard import _RunCycleTestBase
    from test_run_medic_tier_pool import _MedicTierPoolBase, _RUN_MEDIC

_HAS_JQ = shutil.which('jq') is not None

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _REPO_ROOT / 'scripts'

# The model BOTH wrappers pass to `claude --model` (their WORK_MODEL), and
# therefore the $wm the selector falls back to.
WORK_MODEL = 'claude-sonnet-4-6'
# The live utility model Claude Code runs alongside the work model.
UTILITY = 'claude-haiku-4-5-20251001'
# A model neither wrapper dispatches. Used wherever the case must exercise the
# OUTPUT-MAGNITUDE rule: with a non-dispatched model the name-match rule cannot
# fire, so the case can only pass if the outputTokens lookup actually works.
FOREIGN = 'claude-opus-5'

# Cost/usage payload shared by every case, so the whole-row assertions below
# are identical across the matrix and a dropped field is visible everywhere.
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

_ABSENT = object()


def _envelope(model_usage, *, is_error=False):
    env = {}
    if is_error:
        env['is_error'] = True
    if model_usage is not _ABSENT:
        env['modelUsage'] = model_usage
    env.update(_PAYLOAD)
    return env


class Case:
    """One (stub output, expected model, wrapper exit code) row of the matrix.

    ``expect_row=False`` cases carry a raw stub body instead of an envelope and
    assert the NEGATIVE contract (no row, a named log line, an unchanged exit
    code). They live in the same list on purpose: a no-regression pin standing
    alone as its own test method is green before and after any fix, so it reads
    as evidence while carrying none. Folded into the matrix it still guards the
    contract, and the test it lives in is one a broken selector turns red.
    """

    def __init__(self, name, model_usage, expected_model, *,
                 exit_code=0, is_error=False, why='',
                 raw_stub=None, expect_log=None, expect_row=None):
        self.name = name
        # A raw stub usually carries the NEGATIVE contract, but not always:
        # the multi-document cases below hand-build a capture holding several
        # JSON documents and still expect exactly ONE row, so expect_row is
        # overridable.
        self.expect_row = (raw_stub is None) if expect_row is None else expect_row
        self.raw_stub = raw_stub
        self.envelope = (_envelope(model_usage, is_error=is_error)
                         if raw_stub is None else None)
        self.expected_model = expected_model
        self.exit_code = exit_code
        self.expect_log = expect_log
        self.why = why

    @property
    def success(self):
        return self.exit_code == 0


_STUB_TMPL = """#!/usr/bin/env bash
cat <<'CLAUDE_STUB_JSON'
{body}
CLAUDE_STUB_JSON
exit {exit_code}
"""

# Not JSON at all — the `2>&1` plain-text wall the wrappers can capture.
_PLAIN_TEXT_STUB = """#!/usr/bin/env bash
echo 'Claude Code exited: rate limit reached. Not JSON at all.'
exit 0
"""

# A structured stderr diagnostic: valid JSON, no cost/usage shape. The `2>&1`
# in both wrappers is how one lands in the same capture as the result envelope.
_STRAY_LOG_DOC = {'level': 'warn', 'msg': 'mcp server restarted', 'attempt': 2}


def _multi_doc_stub(*docs, exit_code=0):
    """A claude stub whose capture holds MORE THAN ONE JSON document.

    This is the shape a streaming `jq -c` turned into N appended burn rows —
    same ``ts``, therefore the same ``task_id``, and nothing downstream dedups.
    The matrix asserts exactly one row for every one of these, at BOTH wrapper
    surfaces, so the invariant is proven where it actually has to hold.
    """
    return _STUB_TMPL.format(
        body='\n'.join(json.dumps(d) for d in docs), exit_code=exit_code)


_CASES = [
    # --- rule 1: the dispatched model is named in modelUsage --------------
    Case('happy_utility_first', {UTILITY: {'outputTokens': 16},
                                 WORK_MODEL: {'outputTokens': 3841}},
         WORK_MODEL,
         why='live shape: haiku out=16 vs sonnet out=3841'),
    Case('happy_utility_second', {WORK_MODEL: {'outputTokens': 3841},
                                  UTILITY: {'outputTokens': 16}},
         WORK_MODEL,
         why='same data, other key order — the answer must not move'),
    Case('versioned_key', {UTILITY: {'outputTokens': 16},
                           WORK_MODEL + '-20260101': {'outputTokens': 3841}},
         WORK_MODEL + '-20260101',
         why='live keys are versioned; recorded VERBATIM, not collapsed to the '
             'bare literal. Guards against simplifying startswith to =='),

    # --- rule 1's tie-break: evidence first, determinism LAST ---------------
    # Two keys of the DISPATCHED family in one envelope is what a mid-window
    # model-snapshot rollover produces. `sort_by(.key) | first` as rule 1's
    # first choice records the alphabetically-first one — routinely the idle
    # snapshot that burned nothing — over the one that burned everything.
    # Both key orders, because a fix that leans on `first`/`last` position
    # passes one order and fails the other.
    Case('named_versioned_pair_order_a',
         {WORK_MODEL + '-20260101': {'outputTokens': 0},
          WORK_MODEL + '-20260701': {'outputTokens': 3841}},
         WORK_MODEL + '-20260701',
         why='two snapshots of the dispatched family: the one that produced '
             'output wins, not the alphabetically-first one'),
    Case('named_versioned_pair_order_b',
         {WORK_MODEL + '-20260701': {'outputTokens': 3841},
          WORK_MODEL + '-20260101': {'outputTokens': 0}},
         WORK_MODEL + '-20260701',
         why='same data, other key order — the answer must not move'),
    Case('named_alias_and_versioned_order_a',
         {WORK_MODEL: {'outputTokens': 0},
          WORK_MODEL + '-20260701': {'outputTokens': 3841}},
         WORK_MODEL + '-20260701',
         why='the bare alias sorts first but burned nothing; the versioned '
             'snapshot that burned wins'),
    Case('named_alias_and_versioned_order_b',
         {WORK_MODEL + '-20260701': {'outputTokens': 3841},
          WORK_MODEL: {'outputTokens': 0}},
         WORK_MODEL + '-20260701',
         why='same data, other key order'),
    Case('named_pair_tied_positive_order_a',
         {WORK_MODEL + '-20260101': {'outputTokens': 3841},
          WORK_MODEL + '-20260701': {'outputTokens': 3841}},
         WORK_MODEL + '-20260101',
         why='no evidence separates them, so the sorted key breaks the tie — '
             'the determinism the header promises, kept as the LAST resort'),
    Case('named_pair_tied_positive_order_b',
         {WORK_MODEL + '-20260701': {'outputTokens': 3841},
          WORK_MODEL + '-20260101': {'outputTokens': 3841}},
         WORK_MODEL + '-20260101',
         why='same tie, other key order — the sorted-key answer must not move'),
    Case('named_pair_both_zero_order_a',
         {WORK_MODEL + '-20260101': {'outputTokens': 0},
          WORK_MODEL + '-20260701': {'outputTokens': 0}},
         WORK_MODEL + '-20260101',
         why='nothing burned inside the name match either; sorted key again'),
    Case('named_pair_both_zero_order_b',
         {WORK_MODEL + '-20260701': {'outputTokens': 0},
          WORK_MODEL + '-20260101': {'outputTokens': 0}},
         WORK_MODEL + '-20260101',
         why='same all-zero pair, other key order'),
    Case('named_pair_nonnumeric_output',
         {WORK_MODEL + '-20260101': {'outputTokens': '9999'},
          WORK_MODEL + '-20260701': {'outputTokens': 12}},
         WORK_MODEL + '-20260701',
         why='jq sorts strings ABOVE every number, so the type filter must '
             'guard the NAME-MATCHED branch too, not only the global one'),

    # --- rule 2: highest positive output, uniquely -------------------------
    # FOREIGN is deliberately NOT the dispatched model, so rule 1 cannot fire
    # and these two are the cases that discriminate a working field lookup.
    Case('magnitude_foreign_first', {FOREIGN: {'outputTokens': 3841},
                                     UTILITY: {'outputTokens': 16}},
         FOREIGN,
         why='position and magnitude AGREE here'),
    Case('magnitude_foreign_second', {UTILITY: {'outputTokens': 16},
                                      FOREIGN: {'outputTokens': 3841}},
         FOREIGN,
         why='position and magnitude DISAGREE here; only a real outputTokens '
             'lookup satisfies both this and magnitude_foreign_first'),
    Case('type_confusion', {'aaa-model': {'outputTokens': '16'},
                            'bbb-model': {'outputTokens': 3841}},
         'bbb-model',
         why='jq sorts strings ABOVE all numbers, so a bare max would pick the '
             '"16". The object/positive-number filter must run BEFORE max'),
    Case('mixed_nonobject_and_object', {'m': 123,
                                        FOREIGN: {'outputTokens': 9}},
         FOREIGN,
         why='a non-object sibling must be filtered, not fatal'),

    # --- rule 3: the dispatched-model fallback ----------------------------
    Case('tie_zero_order_a', {UTILITY: {'outputTokens': 0},
                              'zz-utility-model': {'outputTokens': 0}},
         WORK_MODEL,
         why='no positive output anywhere -> the tie is DETECTED, not resolved '
             'by key position'),
    Case('tie_zero_order_b', {'zz-utility-model': {'outputTokens': 0},
                              UTILITY: {'outputTokens': 0}},
         WORK_MODEL,
         why='same tie, other key order — pre-fix these two disagreed'),
    Case('rename_higher_first', {FOREIGN: {'output_tokens': 3841},
                                 UTILITY: {'output_tokens': 16}},
         WORK_MODEL,
         why='an upstream field rename must fall back to the dispatched model, '
             'not degrade to positional selection'),
    Case('rename_higher_second', {UTILITY: {'output_tokens': 16},
                                  FOREIGN: {'output_tokens': 3841}},
         WORK_MODEL,
         why='same rename, other key order'),
    Case('rename_with_work_model_named', {WORK_MODEL: {'output_tokens': 3841},
                                          UTILITY: {'output_tokens': 16}},
         WORK_MODEL,
         why='rule 1 survives a schema drift that kills rule 2'),
    Case('work_model_named_but_outproduced', {UTILITY: {'outputTokens': 3841},
                                              WORK_MODEL: {'outputTokens': 16}},
         WORK_MODEL,
         why='rule 1 is a NAME match, not a magnitude match: the model this '
             'wrapper DISPATCHED is the work model even when a sibling '
             'out-produced it. Deleting rule 1 turns this red'),
    Case('tie_positive_order_a', {'aaa-model': {'outputTokens': 3841},
                                  'zzz-model': {'outputTokens': 3841}},
         WORK_MODEL,
         why='a POSITIVE tie must be detected, not resolved by array position'),
    Case('tie_positive_order_b', {'zzz-model': {'outputTokens': 3841},
                                  'aaa-model': {'outputTokens': 3841}},
         WORK_MODEL,
         why='same positive tie, other key order — relaxing the uniqueness '
             'check to >= 1 makes these two disagree'),
    Case('single_zero_output_foreign', {'zz-idle-model': {'outputTokens': 0}},
         WORK_MODEL,
         why='a ZERO output count must never be a DECIDING value: one idle '
             'model is not evidence it did the work. Dropping the > 0 filter '
             'turns this red'),

    # --- degenerate modelUsage: must never abort the jq program ------------
    # Pre-fix, the first four of these exited jq non-zero and the ENTIRE row —
    # cost, tokens, duration, success, account — was silently never appended.
    Case('degenerate_number_value', {'m': 123}, WORK_MODEL),
    Case('degenerate_array', [1, 2], WORK_MODEL),
    Case('degenerate_string', 'x', WORK_MODEL),
    Case('degenerate_null', None, WORK_MODEL),
    Case('degenerate_empty_object', {}, WORK_MODEL),
    Case('modelusage_absent', _ABSENT, WORK_MODEL),

    # --- failure path: the wrapper exits non-zero but still writes a row ---
    Case('failure_work_model_zero_output',
         {UTILITY: {'outputTokens': 16}, WORK_MODEL: {'outputTokens': 0}},
         WORK_MODEL, exit_code=1, is_error=True,
         why='the wall class: the work model produced nothing, the utility '
             'model burned 16. Rule 1 stamps it correctly anyway'),
    Case('failure_work_model_null_output',
         {UTILITY: {'outputTokens': 16}, WORK_MODEL: {'outputTokens': None}},
         WORK_MODEL, exit_code=1, is_error=True),
    # DOCUMENTED RESIDUAL — intentional, do NOT "fix" this to WORK_MODEL.
    # The dispatched model is absent from modelUsage entirely, so the only
    # model that burned tokens is the utility one. Attributing its burn to a
    # model that never appeared would be the larger lie. See the lib header.
    Case('failure_work_model_absent_records_the_burner',
         {UTILITY: {'outputTokens': 16}},
         UTILITY, exit_code=1, is_error=True,
         why='documented residual: record the model that actually burned'),
    Case('failure_all_zero_output_work_model_absent',
         {UTILITY: {'outputTokens': 0}},
         WORK_MODEL, exit_code=1, is_error=True,
         why='nothing burned and the dispatched model is absent -> there is no '
             'evidence to attribute, so fall back to what we dispatched '
             'rather than to whoever happens to be listed'),

    # --- NOT an envelope: the negative half of the property ----------------
    # No-regression pin, deliberately inside the matrix rather than standing as
    # its own test (see the Case docstring). Hardening the selector must not
    # weaken the outer `if COST_LINE=$(jq ...)` guard: a capture that is not
    # JSON at all still skips the row, logs, and leaves the exit code alone —
    # the contract test_run_medic_timeout also relies on.
    Case('unparseable_plain_text', None, None,
         raw_stub=_PLAIN_TEXT_STUB, exit_code=0,
         expect_log='cost-capture: jq parse failed; skipping',
         why='a plain-text wall must skip, not append a garbage row'),

    # --- MORE THAN ONE JSON document in the capture ------------------------
    # The capture is the child's merged stdout+stderr, so this is reachable.
    # Streaming jq emitted one output per document and the appender wrote them
    # all: N burn rows with one `ts` and therefore one `task_id`, which nothing
    # downstream can undo. The matrix's "exactly one appended row" assertion is
    # what these cases exist to reach — proven at BOTH wrapper surfaces, not
    # only against the lib in isolation.
    Case('multidoc_log_then_envelope', None, WORK_MODEL, expect_row=True,
         raw_stub=_multi_doc_stub(
             _STRAY_LOG_DOC,
             _envelope({WORK_MODEL: {'outputTokens': 3841}})),
         why='the row must come from the ENVELOPE, not the stray log line — '
             'and there must be exactly one of it'),
    Case('multidoc_envelope_then_log', None, WORK_MODEL, expect_row=True,
         raw_stub=_multi_doc_stub(
             _envelope({WORK_MODEL: {'outputTokens': 3841}}),
             _STRAY_LOG_DOC),
         why='other document order — `head -1` passes one of this pair and '
             'fails the other, `tail -1` the reverse'),
    Case('multidoc_envelope_twice', None, WORK_MODEL, expect_row=True,
         raw_stub=_multi_doc_stub(
             _envelope({WORK_MODEL: {'outputTokens': 3841}}),
             _envelope({WORK_MODEL: {'outputTokens': 3841}})),
         why='a CLI that prints its result twice must still cost one row'),
]

# Names the matrix MUST keep. Deleting one of these silently removes the only
# case able to discriminate its rule, so the matrix checks its own membership
# before running (_assert_matrix_is_discriminating).
_REQUIRED_CASE_NAMES = {
    'magnitude_foreign_first', 'magnitude_foreign_second',
    'tie_zero_order_a', 'tie_zero_order_b',
    'tie_positive_order_a', 'tie_positive_order_b',
    'rename_higher_first', 'rename_higher_second',
    'type_confusion', 'degenerate_number_value',
    'work_model_named_but_outproduced', 'single_zero_output_foreign',
    'failure_work_model_zero_output', 'unparseable_plain_text',
    # rule 1's tie-break: the only cases with TWO prefix-matching keys, so the
    # only ones that can tell "positive output first" from "sorted key first".
    'named_versioned_pair_order_a', 'named_versioned_pair_order_b',
    'named_alias_and_versioned_order_a', 'named_alias_and_versioned_order_b',
    'named_pair_tied_positive_order_a', 'named_pair_tied_positive_order_b',
    'named_pair_both_zero_order_a', 'named_pair_both_zero_order_b',
    'named_pair_nonnumeric_output',
    # the only cases whose capture holds more than one JSON document.
    'multidoc_log_then_envelope', 'multidoc_envelope_then_log',
    'multidoc_envelope_twice',
    # a VALID document with no usage data must still append a row: the
    # empty-output gate has to discriminate "zero documents" from "one empty
    # document", or it silently starts dropping real failure-path rows.
    'degenerate_empty_object', 'modelusage_absent',
}


def _assert_matrix_is_discriminating(t):
    """Self-check on the matrix, run INSIDE each half before the cases.

    This is scaffolding integrity, not evidence: no change to the production
    scripts can turn it red, so it is deliberately NOT a test method of its own
    — a green-no-matter-what test method reads as evidence while carrying none.
    As a precondition of the test that DOES carry the evidence, it still fails
    loudly the moment someone deletes a discriminating case or one key order.
    """
    names = [c.name for c in _CASES]
    t.assertEqual(len(names), len(set(names)), f'duplicate case names: {names}')
    missing = _REQUIRED_CASE_NAMES - set(names)
    t.assertFalse(missing, f'discriminating cases deleted: {sorted(missing)}')
    # Both key orders must exist for every ordering-sensitive pair — a case in
    # only one order cannot tell "highest output wins" from "last key wins".
    for stem in ('magnitude_foreign', 'tie_zero_order',
                 'tie_positive_order', 'rename_higher',
                 'named_versioned_pair', 'named_alias_and_versioned',
                 'named_pair_tied_positive', 'named_pair_both_zero'):
        pair = sorted(n for n in names if n.startswith(stem))
        t.assertEqual(len(pair), 2,
                      f'{stem} must appear in BOTH key orders: {pair}')
    # Both wrappers must be registered: the run_medic.sh half is the whole
    # point of the extraction, and a matrix that quietly dropped it would still
    # look busy.
    t.assertEqual({CycleCostRowMatrixTest.SOURCE, MedicCostRowMatrixTest.SOURCE},
                  {'run_cycle.sh', 'run_medic.sh'})


class _CostRowMatrixMixin:
    """Shared assertions. Subclasses supply the wrapper-specific plumbing."""

    # Per-wrapper identity, asserted on every row so the lib extraction cannot
    # silently homogenise the two callers.
    AGENT = None
    TASK_TYPE = None
    TASK_ID_PREFIX = None
    SOURCE = None
    # The account the wrapper's own tier plumbing must stamp, when the harness
    # pins one. None => assert only that SOME account was stamped.
    ACCOUNT = None

    # --- plumbing the concrete classes must provide ---------------------
    def _install_claude(self, body: str):
        raise NotImplementedError

    def _invoke(self):
        """Run the wrapper once; return the CompletedProcess."""
        raise NotImplementedError

    def _costs_path(self) -> Path:
        raise NotImplementedError

    # --- shared helpers --------------------------------------------------
    def _read_rows(self):
        costs = self._costs_path()
        if not costs.exists():
            return []
        return [json.loads(ln)
                for ln in costs.read_text().splitlines() if ln.strip()]

    def _reset_costs(self):
        costs = self._costs_path()
        if costs.exists():
            costs.unlink()

    @staticmethod
    def _stdout(result):
        out = result.stdout
        return out.decode() if isinstance(out, bytes) else (out or '')

    def _run_case(self, case: Case):
        body = case.raw_stub if case.raw_stub is not None else _STUB_TMPL.format(
            body=json.dumps(case.envelope), exit_code=case.exit_code)
        self._install_claude(body)
        self._reset_costs()
        return self._invoke()

    def _assert_whole_row(self, row, case: Case):
        # The model — the field this matrix exists for.
        self.assertEqual(row['model'], case.expected_model, case.why)
        self.assertIsInstance(row['model'], str)
        # ...and the nine fields a jq abort would have taken with it.
        self.assertEqual(row['cost_usd'], 0.42)
        self.assertEqual(row['input_tokens'], 3)
        self.assertEqual(row['output_tokens'], 3857)
        self.assertEqual(row['cache_read'], 900000)
        self.assertEqual(row['cache_creation'], 84000)
        self.assertEqual(row['duration_sec'], 306)
        self.assertIs(row['success'], case.success)
        # Account attribution moved into the lib with the row; this is where
        # the extraction would show up if it had homogenised the two callers'
        # tier plumbing (medic stamps the POOL-SELECTED tier).
        self.assertTrue(row['account'], 'account must always be stamped')
        if self.ACCOUNT is not None:
            self.assertEqual(row['account'], self.ACCOUNT)
        # Per-wrapper identity.
        self.assertEqual(row['agent'], self.AGENT)
        self.assertEqual(row['task_type'], self.TASK_TYPE)
        self.assertTrue(row['task_id'].startswith(self.TASK_ID_PREFIX),
                        f"task_id {row['task_id']!r} lost its wrapper prefix")
        self.assertEqual(row['source'], self.SOURCE)

    # --- the matrix ------------------------------------------------------
    def test_cost_row_matrix(self):
        _assert_matrix_is_discriminating(self)
        observed = []
        for case in _CASES:
            with self.subTest(case=case.name):
                result = self._run_case(case)
                rows = self._read_rows()
                if case.expect_row:
                    self.assertEqual(
                        len(rows), 1,
                        f'{case.name}: expected exactly one appended row, '
                        f'got {rows}')
                    self._assert_whole_row(rows[0], case)
                else:
                    self.assertEqual(
                        rows, [],
                        f'{case.name}: unparseable output must append no row')
                    self.assertIn(case.expect_log, self._stdout(result),
                                  f'{case.name}: the skip must be logged')
                    self.assertEqual(
                        result.returncode, case.exit_code,
                        f'{case.name}: a best-effort cost skip must not change '
                        f'the exit code')
                observed.append(case.name)
        # Anti-vacuity: a harness regression that stops invoking this wrapper
        # (or skips cases) must fail loudly rather than pass empty. `source`
        # is asserted per-row above, so these rows provably came from THIS
        # wrapper and not from the other half of the matrix.
        self.assertEqual(len(observed), len(_CASES))
        self.assertEqual(set(observed), {c.name for c in _CASES})


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class CycleCostRowMatrixTest(_CostRowMatrixMixin, _RunCycleTestBase):
    """run_cycle.sh half of the matrix."""

    AGENT = 'pulse'
    TASK_TYPE = 'cycle'
    TASK_ID_PREFIX = 'cycle-'
    SOURCE = 'run_cycle.sh'

    def _install_claude(self, body: str):
        stub = self.stub_bin / 'claude'
        stub.write_text(body)
        os.chmod(stub, 0o755)

    def _costs_path(self):
        return self.home / 'agents' / 'blackboard' / 'costs.jsonl'

    def _invoke(self):
        # run_cycle.sh self-throttles on the tier window (300s for tier 1), so
        # clear the idempotency anchor before each repeat run or every case
        # after the first would skip before reaching the cost block.
        flag = self.home / 'agents' / 'state' / 'cycle-last-run.flag'
        if flag.exists():
            flag.unlink()
        return self._run_cycle()


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class MedicCostRowMatrixTest(_CostRowMatrixMixin, _MedicTierPoolBase):
    """run_medic.sh half of the matrix — the copy that had zero evidence."""

    AGENT = 'medic'
    TASK_TYPE = 'medic-escalate'
    TASK_ID_PREFIX = 'medic-'
    SOURCE = 'run_medic.sh'
    # The tier the stubbed pool selects below. Asserted on EVERY row rather
    # than in a pin of its own: medic's account plumbing is what the lib
    # extraction was most likely to homogenise, and the check is only worth
    # anything on the same rows whose model the matrix is discriminating.
    ACCOUNT = 'tier3'

    def setUp(self):
        super().setUp()
        self._install_active_tier_stub(
            select_out='TIER=tier3\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-t3\n',
            select_code=0, report_out='')

    def _install_claude(self, body: str):
        stub = self.fake_bin / 'claude'
        stub.write_text(body)
        os.chmod(stub, 0o755)

    def _costs_path(self):
        return self.fake_home / 'agents' / 'blackboard' / 'costs.jsonl'

    def _invoke(self):
        return self._run()


_UNPARSEABLE_LIB = """#!/usr/bin/env bash
# A truncated lib: the function body is never closed, so `bash -n` fails.
capture_cost_row() {
    echo 'never reached'
"""

# The lib STATES a wrapper can meet, and the log line each one must produce.
# ABSENCE was the only member the guard originally covered; the other three
# failure members all killed the tick under `set -e` before the wrapper could
# write a single line anywhere. `healthy` is the anti-vacuity member.
#
# The strings are per-state ON PURPOSE: routing every failure into the existing
# "lib missing" arm would be a fresh honesty defect of exactly the class this
# batch closes — a log naming a cause that is not the cause.
_LIB_STATES = {
    'absent': 'cost-capture: lib missing; skipping',
    'path_is_a_directory': 'cost-capture: lib missing; skipping',
    'mode_000': 'could not be loaded',
    'syntax_error': 'could not be loaded',
    'empty_file': 'lib loaded but defines no capture_cost_row',
    'healthy': 'cost record appended',
}
# Members that root can't exercise (root ignores the mode bits).
_ROOT_BLIND_STATES = {'mode_000'}


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class LibStateDegradesGracefullyTest(_RunCycleTestBase):
    """PROPERTY over the SET of _lib_cost_capture.sh states, BOTH wrappers.

    For EVERY member: the wrapper reaches its normal exit code, writes at least
    one line to its own log file, and logs a cost-capture outcome whose text
    names the ACTUAL state.

    The lib is sourced under `set -e`. `[ -f ]` alone covers only ABSENCE — a
    lib that exists but cannot be read or parsed aborted the wrapper at the
    `source` line, which sits above `log()`, above the lock write and above the
    EXIT trap, so the tick died with zero lines in cycle.log/medic.log and the
    only evidence was a journal entry. Note `source X || …` does NOT survive a
    parse error (bash still exits 2 through the `||`), which is why the fix
    needs the `bash -n` precheck and why deleting it turns the syntax-error
    member red.

    EVIDENCE THIS TEST CAN AND CANNOT CARRY: the hazard is created by the
    extraction itself, so there is no pre-extraction tree in which the lib is
    present but unsourceable. It is killed by mutations OF THE FIX — drop the
    `bash -n` precheck, drop `[ -r ]`, drop `command -v capture_cost_row`,
    collapse the tri-state back into one `else` — which is the only class of
    mutation that can reach it. Kept because the failure it guards is a dead
    /cycle, not a dropped row.
    """

    def _lib_copy(self):
        """The harness's copy of the lib — asserted present before use.

        assertTrue rather than a bare unlink(): against a tree without the lib
        this must read as "the single definition is missing", not as a
        FileNotFoundError that looks like a broken test.
        """
        lib = self.scripts_dir / '_lib_cost_capture.sh'
        self.assertTrue(
            lib.exists() or lib.is_dir(),
            'the single cost-row definition (scripts/_lib_cost_capture.sh) is '
            'not in the wrapper directory — nothing to mutate, so this test '
            'cannot say anything about the guarded source')
        return lib

    def _reset_lib(self):
        """Restore a healthy lib so each member starts from the same state."""
        lib = self.scripts_dir / '_lib_cost_capture.sh'
        if lib.is_dir():
            shutil.rmtree(lib)
        elif lib.exists():
            os.chmod(lib, 0o600)
            lib.unlink()
        shutil.copy2(_SCRIPTS / '_lib_cost_capture.sh', lib)

    def _apply_state(self, state):
        lib = self._lib_copy()
        if state == 'healthy':
            return
        if state == 'absent':
            lib.unlink()
        elif state == 'path_is_a_directory':
            lib.unlink()
            lib.mkdir()
        elif state == 'mode_000':
            os.chmod(lib, 0o000)
            self.addCleanup(lambda: os.chmod(lib, 0o600) if lib.exists() else None)
        elif state == 'syntax_error':
            lib.write_text(_UNPARSEABLE_LIB)
        elif state == 'empty_file':
            lib.write_text('')
        else:  # pragma: no cover - guarded by the membership assert below
            raise AssertionError(f'unknown lib state {state!r}')

    def _install_stub(self):
        stub = self.stub_bin / 'claude'
        stub.write_text(_STUB_TMPL.format(
            body=json.dumps(_envelope({WORK_MODEL: {'outputTokens': 3841}})),
            exit_code=0))
        os.chmod(stub, 0o755)

    def _costs(self):
        return self.home / 'agents' / 'blackboard' / 'costs.jsonl'

    def _prepare(self, state):
        self._reset_lib()
        self._install_stub()
        if self._costs().exists():
            self._costs().unlink()
        # run_cycle.sh self-throttles on the tier window (300s for tier 1), so
        # clear the idempotency anchor or every member after the first skips
        # before it can reach the cost block — and the property would then be
        # green for a wrapper that never ran.
        flag = self.home / 'agents' / 'state' / 'cycle-last-run.flag'
        if flag.exists():
            flag.unlink()
        self._apply_state(state)

    def _assert_property(self, state, returncode, stdout, log_file):
        expected = _LIB_STATES[state]
        self.assertEqual(
            returncode, 0,
            f'{state}: the wrapper must reach its normal exit code; '
            f'out={stdout[-800:]}')
        self.assertIn(
            expected, stdout,
            f'{state}: the log must name the ACTUAL state; out={stdout[-800:]}')
        self.assertTrue(
            log_file.exists() and log_file.read_text().strip(),
            f'{state}: the wrapper wrote nothing to {log_file.name} — a tick '
            f'that dies above log() leaves no evidence anywhere but the '
            f'journal')
        rows = ([ln for ln in self._costs().read_text().splitlines() if ln.strip()]
                if self._costs().exists() else [])
        if state == 'healthy':
            # ANTI-VACUITY: without this member the whole property is
            # satisfied by a harness that never reaches the cost block.
            self.assertEqual(len(rows), 1,
                             f'healthy lib must append exactly one row: {rows}')
        else:
            self.assertEqual(rows, [],
                             f'{state}: no row can be written')
            self.assertIn('cost-capture:', stdout,
                          f'{state}: the skip must be logged')

    def test_the_state_set_is_covered(self):
        """Scaffolding integrity: the runnable set must not be empty, and the
        two members that carry the NEW direction must be in it."""
        runnable = set(_LIB_STATES) - (
            _ROOT_BLIND_STATES if os.geteuid() == 0 else set())
        self.assertIn('healthy', runnable)
        self.assertIn('syntax_error', runnable)
        self.assertIn('absent', runnable)
        self.assertGreaterEqual(len(runnable), 5)

    def test_cycle_survives_every_lib_state(self):
        for state in sorted(_LIB_STATES):
            if state in _ROOT_BLIND_STATES and os.geteuid() == 0:
                continue
            with self.subTest(lib_state=state):
                self._prepare(state)
                log_file = self.home / 'agents' / 'logs' / 'cycle.log'
                if log_file.exists():
                    log_file.unlink()
                result = self._run_cycle()
                self._assert_property(state, result.returncode,
                                      result.stdout.decode(), log_file)

    def test_medic_survives_every_lib_state(self):
        """Same property on the medic wrapper, run from the harness dir.

        run_medic.sh normally resolves the lib out of the real repo, so to
        mutate the lib we run a COPY from the harness's fake scripts dir. Note
        run_medic.sh had NO `source` on main, so this class enters the medic
        wrapper for the first time with this PR.
        """
        shutil.copy2(_RUN_MEDIC, self.scripts_dir / 'run_medic.sh')
        # run_medic.sh chdirs into ${HOME}/agent-core/agents/medic under set -e.
        (self.repo_dir / 'agents' / 'medic').mkdir(parents=True, exist_ok=True)
        batch = self.tmp_path / 'batch.json'
        batch.write_text(json.dumps({'alerts': []}))
        for state in sorted(_LIB_STATES):
            if state in _ROOT_BLIND_STATES and os.geteuid() == 0:
                continue
            with self.subTest(lib_state=state):
                self._prepare(state)
                log_file = self.home / 'agents' / 'logs' / 'medic.log'
                if log_file.exists():
                    log_file.unlink()
                env = os.environ.copy()
                env['HOME'] = str(self.home)
                # This harness sandboxes by SWAPPING HOME, but os.environ.copy()
                # inherits conftest's OURLIBERTY_AGENTS_ROOT (the outer sandbox
                # root), and run_medic.sh now resolves THAT ahead of $HOME. Pin
                # it to the sandboxed HOME so the swap stays authoritative and
                # the wrapper's log/costs land where this test reads them —
                # exactly what _RunCycleTestBase._run_cycle does for the cycle
                # wrapper. See [[home-swap-tier-hazards]].
                env['OURLIBERTY_AGENTS_ROOT'] = str(self.home / 'agents')
                env['PATH'] = f'{self.stub_bin}:{env["PATH"]}'
                result = subprocess.run(
                    ['bash', str(self.scripts_dir / 'run_medic.sh'), str(batch)],
                    env=env, capture_output=True, text=True, timeout=30)
                self._assert_property(state, result.returncode,
                                      result.stdout, log_file)


class StructuralAntiDriftTest(unittest.TestCase):
    """One definition, two callers — asserted structurally, not by hope.

    A shared lib EXISTING is not evidence it is USED. These greps plus the
    both-wrappers matrix above are.
    """

    WRAPPERS = ('run_cycle.sh', 'run_medic.sh')

    def _wrapper_sources(self):
        return {name: (_SCRIPTS / name).read_text() for name in self.WRAPPERS}

    def test_model_selector_exists_exactly_once_in_the_repo(self):
        hits = {p.name: p.read_text().count('startswith($wm)')
                for p in sorted(_SCRIPTS.glob('*.sh'))}
        total = sum(hits.values())
        self.assertEqual(total, 1, f'model selector must have ONE copy: {hits}')
        self.assertEqual(hits['_lib_cost_capture.sh'], 1)
        defs = {p.name: p.read_text().count('def work_model(')
                for p in sorted(_SCRIPTS.glob('*.sh'))}
        self.assertEqual(sum(defs.values()), 1, f'work_model defined once: {defs}')

    def test_neither_wrapper_carries_an_inline_cost_row_jq_program(self):
        for name, text in self._wrapper_sources().items():
            self.assertNotIn('model: (', text,
                             f'{name} still has an inline model: jq fragment')
            self.assertNotIn('to_entries', text,
                             f'{name} still builds the cost row inline')
            self.assertNotIn('modelUsage', text,
                             f'{name} still reads modelUsage directly')

    def test_each_wrapper_calls_capture_cost_row_exactly_once(self):
        for name, text in self._wrapper_sources().items():
            calls = re.findall(r'^\s*capture_cost_row\b', text, re.M)
            self.assertEqual(len(calls), 1,
                             f'{name} must call capture_cost_row exactly once')
            self.assertIn('command -v capture_cost_row', text,
                          f'{name} must guard on the lib being loaded')

    def test_the_call_passes_the_dispatched_model_not_a_literal(self):
        """The $wm the selector falls back to must be the SAME variable the
        wrapper passed to `claude --model`. A literal here would re-open the
        two-copies drift one layer down — the lib would take a work-model
        argument each caller could hardcode differently from its own --model.
        """
        for name, text in self._wrapper_sources().items():
            call = re.search(
                r'^[ \t]*capture_cost_row\b(?:[^\n]*\\\n)*[^\n]*', text, re.M)
            self.assertIsNotNone(call, f'{name}: no capture_cost_row call')
            body = call.group(0)
            self.assertIn('"$WORK_MODEL"', body,
                          f'{name} must pass $WORK_MODEL to capture_cost_row')
            self.assertNotIn('claude-', body,
                             f'{name} passes a hardcoded model literal')

    def test_each_wrapper_has_a_single_work_model_literal(self):
        for name, text in self._wrapper_sources().items():
            defs = re.findall(r'^WORK_MODEL=', text, re.M)
            self.assertEqual(len(defs), 1,
                             f'{name} must define WORK_MODEL exactly once')
            self.assertNotIn('--model claude-', text,
                             f'{name} has a second hardcoded model literal')
            self.assertEqual(text.count('--model "$WORK_MODEL"'), 1,
                             f'{name} must dispatch on $WORK_MODEL')
            self.assertIn('"$WORK_MODEL"', text)
        # Both wrappers dispatch the SAME model this matrix assumes.
        for name, text in self._wrapper_sources().items():
            self.assertIn(f'WORK_MODEL="{WORK_MODEL}"', text,
                          f'{name} dispatches a model this matrix does not '
                          f'expect; update WORK_MODEL here too')

    # --- the rc surface: one lib, two hand-maintained case blocks ---------
    def _capture_fn_body(self):
        text = (_SCRIPTS / '_lib_cost_capture.sh').read_text()
        start = text.index('capture_cost_row() {')
        end = text.index('\n}\n', start)
        return text[start:end]

    def _cost_rc_case_block(self, text, name):
        block = re.search(r'case "\$COST_RC" in\n(.*?)\n[ \t]*esac',
                          text, re.S)
        self.assertIsNotNone(block, f'{name}: no `case "$COST_RC"` block')
        return block.group(1)

    def test_every_lib_return_code_has_an_arm_in_both_wrappers(self):
        """The rc surface is six-wide and lives in TWO hand-copied case blocks.

        This is the guard the two-copies history says gets missed next: a code
        added to the lib and wired into only one wrapper is invisible until the
        other wrapper hits it in production and logs the wrong cause.
        """
        codes = set(re.findall(r'\breturn\s+(\d+)', self._capture_fn_body()))
        codes.add('0')  # the success path is `return 0` too, but pin it anyway
        # Anti-vacuity: if the extraction regex ever stops matching, an empty
        # `codes` would make the loop below pass without asserting anything.
        self.assertGreaterEqual(
            len(codes), 6,
            f'capture_cost_row should expose at least six outcomes: {codes}')
        for name, text in self._wrapper_sources().items():
            block = self._cost_rc_case_block(text, name)
            for code in sorted(codes):
                self.assertRegex(
                    block, rf'(?m)^\s*{code}\)\s',
                    f'{name} has no `case` arm for rc {code}, which '
                    f'_lib_cost_capture.sh can return')

    def test_neither_wrappers_catch_all_arm_asserts_a_cause(self):
        """`*)` used to say "jq missing or empty output; skipping" — which
        would MISLABEL every code added since. A catch-all may report the rc;
        it may not name a cause it did not check."""
        for name, text in self._wrapper_sources().items():
            block = self._cost_rc_case_block(text, name)
            catch_all = re.search(r'(?m)^\s*\*\)\s*(.*)$', block)
            self.assertIsNotNone(catch_all, f'{name}: no catch-all arm')
            arm = catch_all.group(1)
            self.assertIn('rc=$COST_RC', arm,
                          f'{name}: the catch-all must report the rc it got')
            for claim in ('jq', 'missing', 'parse', 'lib'):
                self.assertNotIn(
                    claim, arm,
                    f'{name}: the catch-all asserts a cause ({claim!r}) it '
                    f'never checked: {arm}')

    def test_the_lib_source_block_sits_below_log_and_above_the_lock(self):
        """Placement is load-bearing in BOTH directions.

        BELOW `log()`: an unloadable lib must be reportable in cycle.log /
        medic.log, not only in the journal. ABOVE the lock write: sourcing
        after the lock would change abort semantics and could strand a lock
        file, which the "never abort the tick" contract depends on not
        happening.
        """
        for name, text in self._wrapper_sources().items():
            log_def = text.index('log() {')
            source_block = text.index('_COST_LIB=')
            lock_write = text.index('echo "$$" > "$LOCK_FILE"')
            self.assertLess(log_def, source_block,
                            f'{name}: the lib source block moved ABOVE log(), '
                            f'so an unloadable lib is unreportable')
            self.assertLess(source_block, lock_write,
                            f'{name}: the lib source block moved BELOW the '
                            f'lock write, changing abort semantics')

    def test_wrappers_resolve_the_lib_relative_to_themselves(self):
        """NOT via $HOME: run_medic.sh is invoked from the real repo path with
        a faked HOME, and systemd invokes both by absolute path."""
        for name, text in self._wrapper_sources().items():
            self.assertIn('BASH_SOURCE[0]', text, f'{name} lib resolution')
            self.assertIn('_lib_cost_capture.sh', text)
            self.assertNotIn('${HOME}/agent-core/scripts/_lib_cost_capture.sh',
                             text, f'{name} must not resolve the lib via $HOME')

    # DELIBERATELY NOT HERE — three checks that used to live in this class:
    #
    #   * "the lib must not absorb the OAuth export or the `claude --print`
    #     spawn". Already enforced repo-wide by
    #     scripts/tests/test_claude_durable_auth_guard.py, which scans every
    #     scripts/*.sh for the spawn regex, cross-checks the hits against a
    #     pinned _EXPECTED_SURFACE, and requires each spawning file to carry
    #     the durable-token bridge. A spawn appearing in the lib is an unpinned
    #     surface member there; the export leaving a wrapper strips that
    #     wrapper's bridge marker there. Restating it here added a second copy
    #     of a guard — the exact habit these findings are about.
    #   * "the matrix keeps its discriminating cases" and "both wrappers are
    #     exercised": self-checks over this module's own _CASES list, which no
    #     production change can turn red. They now run as a precondition of the
    #     matrix test itself (_assert_matrix_is_discriminating), where they
    #     guard the same thing without posing as evidence.


if __name__ == '__main__':
    unittest.main()
