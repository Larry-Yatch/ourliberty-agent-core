#!/usr/bin/env python3
"""Tests for the cost-row MODEL attribution in run_cycle.sh / run_medic.sh.

Claude Code reports a *utility* model (haiku, used for background chores like
title generation) in `.modelUsage` alongside the model that actually did the
work. The original expression picked the model with

    .modelUsage | keys | first

but `jq`'s `keys` SORTS, so ``claude-haiku-4-5-*`` always sorted ahead of
``claude-opus-*`` / ``claude-sonnet-*`` and won every time. Every /cycle and
every medic run has been stamped ``claude-haiku-4-5-20251001`` in
costs.jsonl since the field was added, while the real work model was
whatever the wrapper passed to ``--model``.

That is a silent-wrong-write: the row is present and well-formed, so nothing
alarms, but ~57% of the fleet's recorded burn is attributed to the wrong
model — and Pulse Check X reads this ledger to watch for a chain-quality
regression after a model cutover, so its per-model view was reading a label
that could never change.

The fix picks the highest-OUTPUT model instead. These tests pin the
discriminating behaviour: a fixture where the utility model sorts first but
the work model generated the output must record the WORK model.

Run::

    python3 -m unittest scripts.tests.test_run_cycle_cost_model_attribution
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import shutil
import unittest

try:  # dotted/pytest path: relative import within the scripts.tests package
    from .test_run_cycle_wrong_branch_guard import _RunCycleTestBase
except ImportError:  # discover loads this module top-level (no package parent)
    from test_run_cycle_wrong_branch_guard import _RunCycleTestBase

_HAS_JQ = shutil.which('jq') is not None

# Stub `claude` emitting a REAL-SHAPED modelUsage: the utility model sorts
# alphabetically first and carries a trivial output count; the work model
# sorts later and carries essentially all the output. Observed live on the
# droplet as haiku out=16 vs sonnet out=3841.
_CLAUDE_MODELUSAGE_STUB = '''#!/usr/bin/env bash
cat <<'JSON'
{{
  "modelUsage": {{
    "claude-haiku-4-5-20251001": {{"outputTokens": 16}},
    "{work_model}": {{"outputTokens": 3841}}
  }},
  "total_cost_usd": 0.42,
  "duration_ms": 306000,
  "usage": {{
    "input_tokens": 3,
    "output_tokens": 3857,
    "cache_read_input_tokens": 900000,
    "cache_creation_input_tokens": 84000
  }}
}}
JSON
exit 0
'''

# Degenerate shape: no modelUsage at all. Must fall back, not crash or emit
# a null model.
_CLAUDE_NO_MODELUSAGE_STUB = '''#!/usr/bin/env bash
echo '{"total_cost_usd": 0.1, "duration_ms": 1000, "usage": {"output_tokens": 5}}'
exit 0
'''


@unittest.skipUnless(_HAS_JQ, 'jq required for the cost-record parse')
class CycleCostModelAttributionTest(_RunCycleTestBase):
    """run_cycle.sh must stamp the model that produced the output."""

    def _install_claude(self, body: str):
        stub = self.stub_bin / 'claude'
        stub.write_text(body)
        os.chmod(stub, 0o755)

    def _read_cost_rows(self):
        costs = self.home / 'agents' / 'blackboard' / 'costs.jsonl'
        if not costs.exists():
            return []
        return [json.loads(ln)
                for ln in costs.read_text().splitlines() if ln.strip()]

    def test_utility_model_sorting_first_does_not_win(self):
        """The regression: haiku sorts first but did 16 of 3857 output tokens."""
        self._install_claude(
            _CLAUDE_MODELUSAGE_STUB.format(work_model='claude-sonnet-4-6'))
        self._run_cycle()
        rows = self._read_cost_rows()
        self.assertTrue(rows, 'expected a cost row to be appended')
        self.assertEqual(rows[0]['model'], 'claude-sonnet-4-6')
        self.assertNotEqual(
            rows[0]['model'], 'claude-haiku-4-5-20251001',
            'utility model was recorded as the work model (keys|first regression)')

    def test_work_model_is_read_not_hardcoded(self):
        """A different work model must be recorded verbatim.

        Guards against 'fixing' the bug by hardcoding the fallback: this row
        must say opus, which is neither the sorted-first key nor the
        `//` fallback literal.
        """
        self._install_claude(
            _CLAUDE_MODELUSAGE_STUB.format(work_model='claude-opus-5'))
        self._run_cycle()
        rows = self._read_cost_rows()
        self.assertTrue(rows, 'expected a cost row to be appended')
        self.assertEqual(rows[0]['model'], 'claude-opus-5')

    def test_missing_modelusage_falls_back(self):
        """No modelUsage → the documented fallback, never null/empty."""
        self._install_claude(_CLAUDE_NO_MODELUSAGE_STUB)
        self._run_cycle()
        rows = self._read_cost_rows()
        self.assertTrue(rows, 'expected a cost row to be appended')
        self.assertEqual(rows[0]['model'], 'claude-sonnet-4-6')
        self.assertIsNotNone(rows[0]['model'])


if __name__ == '__main__':
    unittest.main()
