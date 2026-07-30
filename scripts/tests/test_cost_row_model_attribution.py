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
_LIB_COST = _SCRIPTS / '_lib_cost_capture.sh'
_RUN_CYCLE = _SCRIPTS / 'run_cycle.sh'

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
    """One (envelope, expected model, wrapper exit code) row of the matrix."""

    def __init__(self, name, model_usage, expected_model, *,
                 exit_code=0, is_error=False, why=''):
        self.name = name
        self.envelope = _envelope(model_usage, is_error=is_error)
        self.expected_model = expected_model
        self.exit_code = exit_code
        self.why = why

    @property
    def success(self):
        return self.exit_code == 0


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
]

# Names the matrix MUST keep. Deleting one of these silently removes the only
# case able to discriminate its rule, so pin them (see StructuralAntiDriftTest).
_REQUIRED_CASE_NAMES = {
    'magnitude_foreign_first', 'magnitude_foreign_second',
    'tie_zero_order_a', 'tie_zero_order_b',
    'tie_positive_order_a', 'tie_positive_order_b',
    'rename_higher_first', 'rename_higher_second',
    'type_confusion', 'degenerate_number_value',
    'work_model_named_but_outproduced', 'single_zero_output_foreign',
    'failure_work_model_zero_output',
}

_STUB_TMPL = """#!/usr/bin/env bash
cat <<'CLAUDE_STUB_JSON'
{body}
CLAUDE_STUB_JSON
exit {exit_code}
"""

_PLAIN_TEXT_STUB = """#!/usr/bin/env bash
echo 'Claude Code exited: rate limit reached. Not JSON at all.'
exit 0
"""


class _CostRowMatrixMixin:
    """Shared assertions. Subclasses supply the wrapper-specific plumbing."""

    # Per-wrapper identity, asserted on every row so the lib extraction cannot
    # silently homogenise the two callers.
    AGENT = None
    TASK_TYPE = None
    TASK_ID_PREFIX = None
    SOURCE = None

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

    def _run_case(self, case: Case):
        self._install_claude(_STUB_TMPL.format(
            body=json.dumps(case.envelope), exit_code=case.exit_code))
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
        self.assertTrue(row['account'], 'account must always be stamped')
        # Per-wrapper identity.
        self.assertEqual(row['agent'], self.AGENT)
        self.assertEqual(row['task_type'], self.TASK_TYPE)
        self.assertTrue(row['task_id'].startswith(self.TASK_ID_PREFIX),
                        f"task_id {row['task_id']!r} lost its wrapper prefix")
        self.assertEqual(row['source'], self.SOURCE)

    # --- the matrix ------------------------------------------------------
    def test_cost_row_matrix(self):
        observed = []
        for case in _CASES:
            with self.subTest(case=case.name):
                self._run_case(case)
                rows = self._read_rows()
                self.assertEqual(
                    len(rows), 1,
                    f'{case.name}: expected exactly one appended row, got {rows}')
                self._assert_whole_row(rows[0], case)
                observed.append(case.name)
        # Anti-vacuity: a harness regression that stops invoking this wrapper
        # (or skips cases) must fail loudly rather than pass empty. `source`
        # is asserted per-row above, so these rows provably came from THIS
        # wrapper and not from the other half of the matrix.
        self.assertEqual(len(observed), len(_CASES))
        self.assertEqual(set(observed), {c.name for c in _CASES})

    def test_unparseable_output_skips_without_writing(self):
        """A plain-text wall must skip the row and leave the exit code alone.

        Pins that hardening the selector did NOT weaken the outer jq guard —
        the same contract test_run_medic_timeout relies on.
        """
        self._install_claude(_PLAIN_TEXT_STUB)
        self._reset_costs()
        result = self._invoke()
        self.assertEqual(self._read_rows(), [],
                         'unparseable output must append no row')
        self.assertIn('cost-capture: jq parse failed; skipping',
                      result.stdout.decode() if isinstance(result.stdout, bytes)
                      else result.stdout)
        self.assertEqual(result.returncode, 0,
                         'a best-effort cost skip must not change the exit code')


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

    def test_account_comes_from_the_pool_selected_tier(self):
        """Medic's own account plumbing survived the lib extraction."""
        self._run_case(_CASES[0])
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['account'], 'tier3')


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class MissingLibDegradesGracefullyTest(_RunCycleTestBase):
    """A missing _lib_cost_capture.sh must skip the row, NOT abort the run.

    The lib is sourced under `set -e`. An unguarded `source` of a missing file
    would kill the whole /cycle (or medic tick) — far worse than the
    best-effort cost row the lib exists to write.
    """

    def test_cycle_completes_without_the_lib(self):
        (self.scripts_dir / '_lib_cost_capture.sh').unlink()
        stub = self.stub_bin / 'claude'
        stub.write_text(_STUB_TMPL.format(
            body=json.dumps(_envelope({WORK_MODEL: {'outputTokens': 3841}})),
            exit_code=0))
        os.chmod(stub, 0o755)

        result = self._run_cycle()

        self.assertEqual(result.returncode, 0,
                         f'stderr={result.stderr.decode()[:800]}')
        self.assertIn('cost-capture: lib missing; skipping',
                      result.stdout.decode())
        costs = self.home / 'agents' / 'blackboard' / 'costs.jsonl'
        self.assertFalse(costs.exists() and costs.read_text().strip(),
                         'no row can be written without the lib')

    def test_medic_completes_without_the_lib(self):
        """Same guard on the medic wrapper, run from a lib-less directory.

        run_medic.sh normally resolves the lib out of the real repo, so to
        exercise its absence we run a COPY from the harness's fake scripts dir
        (whose copy of the lib we remove first).
        """
        lib_less = self.scripts_dir
        (lib_less / '_lib_cost_capture.sh').unlink()
        shutil.copy2(_RUN_MEDIC, lib_less / 'run_medic.sh')
        # run_medic.sh chdirs into ${HOME}/agent-core/agents/medic under set -e.
        (self.repo_dir / 'agents' / 'medic').mkdir(parents=True, exist_ok=True)
        batch = self.tmp_path / 'batch.json'
        batch.write_text(json.dumps({'alerts': []}))
        stub = self.stub_bin / 'claude'
        stub.write_text(_STUB_TMPL.format(
            body=json.dumps(_envelope({WORK_MODEL: {'outputTokens': 3841}})),
            exit_code=0))
        os.chmod(stub, 0o755)

        env = os.environ.copy()
        env['HOME'] = str(self.home)
        env['PATH'] = f'{self.stub_bin}:{env["PATH"]}'
        result = subprocess.run(
            ['bash', str(lib_less / 'run_medic.sh'), str(batch)],
            env=env, capture_output=True, text=True, timeout=30)

        self.assertEqual(result.returncode, 0,
                         f'stderr={result.stderr[:800]}')
        self.assertIn('cost-capture: lib missing; skipping', result.stdout)
        costs = self.home / 'agents' / 'blackboard' / 'costs.jsonl'
        self.assertFalse(costs.exists() and costs.read_text().strip(),
                         'no row can be written without the lib')


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

    def test_wrappers_resolve_the_lib_relative_to_themselves(self):
        """NOT via $HOME: run_medic.sh is invoked from the real repo path with
        a faked HOME, and systemd invokes both by absolute path."""
        for name, text in self._wrapper_sources().items():
            self.assertIn('BASH_SOURCE[0]', text, f'{name} lib resolution')
            self.assertIn('_lib_cost_capture.sh', text)
            self.assertNotIn('${HOME}/agent-core/scripts/_lib_cost_capture.sh',
                             text, f'{name} must not resolve the lib via $HOME')

    def test_lib_does_not_absorb_the_auth_export_or_the_spawn(self):
        """The lib must stay OUT of the auth-guard's inference-spawn surface.

        test_claude_durable_auth_guard scans every scripts/*.sh with
        ``\\bclaude\\s+(?:--print|-p)\\b`` and requires each hit to carry the
        durable-token bridge, cross-checked against a pinned _EXPECTED_SURFACE.
        If the lib matched that regex it would be a new, unpinned member of the
        surface; if the token export moved in here, the wrappers would lose
        their bridge marker. Neither may happen — not even in a comment.
        """
        lib = _LIB_COST.read_text()
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', lib)
        self.assertIsNone(re.search(r'\bclaude\s+(?:--print|-p)\b', lib),
                          'the lib now matches the auth guard spawn regex')
        for name, text in self._wrapper_sources().items():
            self.assertRegex(text, r'\bclaude\s+(?:--print|-p)\b',
                             f'{name} must keep its own spawn')
            self.assertIn('CLAUDE_CODE_OAUTH_TOKEN', text,
                          f'{name} must keep its own token bridge')

    def test_matrix_keeps_its_discriminating_cases(self):
        names = {c.name for c in _CASES}
        missing = _REQUIRED_CASE_NAMES - names
        self.assertFalse(missing, f'discriminating cases deleted: {missing}')
        # Both key orders must exist for every ordering-sensitive pair.
        for stem in ('magnitude_foreign', 'tie_zero_order',
                     'tie_positive_order', 'rename_higher'):
            pair = [n for n in names if n.startswith(stem)]
            self.assertEqual(len(pair), 2,
                             f'{stem} must appear in BOTH key orders: {pair}')

    def test_the_two_wrappers_are_both_exercised(self):
        """Anti-vacuity on the matrix itself: both halves must be registered."""
        sources = {CycleCostRowMatrixTest.SOURCE, MedicCostRowMatrixTest.SOURCE}
        self.assertEqual(sources, {'run_cycle.sh', 'run_medic.sh'})
        self.assertTrue(_RUN_CYCLE.exists() and _RUN_MEDIC.exists())


if __name__ == '__main__':
    unittest.main()
