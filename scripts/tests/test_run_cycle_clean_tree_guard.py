#!/usr/bin/env python3
"""Tests for run_cycle.sh's clean-tree guard.

Closes the 2026-06-26 sync-wedge class: the Pulse /cycle agent runs INSIDE the
live repo, so it can edit a TRACKED file outside its own write-set (e.g.
config/alert-translations.json). The scoped auto-commit stages only
PULSE_RUNTIME_PATHS, so such an edit is left uncommitted and wedges
sync_agent_core.sh + agent_core_health_check.py (paging Larry every ~30 min).

The guard runs AFTER the Pulse-owned auto-commit, gated to main:
  - tracked edit outside SYNC_AUTOCOMMIT_PATHS  → revert (archive diff first) +
    emit pulse-cycle/cycle:stray-tree-edit-reverted (FYI)
  - Pulse-owned edit (PULSE_RUNTIME_PATHS)      → committed by auto-commit, kept
  - healer-owned edit (SYNC_EXTRA_RUNTIME_PATHS) → tolerated, NOT reverted
  - runs even when the cycle itself failed (CYCLE_OK=0)

Reuses the subprocess harness from test_run_cycle_wrong_branch_guard
(_RunCycleTestBase): a tmpdir-rooted fake agent-core + bare origin + a stub
`claude` on PATH. Here each test installs a stub that makes the relevant edit.

Run:
    python3 -m unittest scripts.tests.test_run_cycle_clean_tree_guard
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import subprocess
import unittest

try:  # dotted/pytest path: relative import within the scripts.tests package
    from .test_run_cycle_wrong_branch_guard import _RunCycleTestBase
except ImportError:  # discover loads this module top-level (no package parent)
    from test_run_cycle_wrong_branch_guard import _RunCycleTestBase


# Mirrors PULSE_RUNTIME_PATHS in scripts/_lib_pulse_runtime.sh. run_cycle.sh's
# auto-commit does `git add -- "${PULSE_RUNTIME_PATHS[@]}"`, which is all-or-
# nothing: if ANY pathspec matches no file, git stages nothing and the
# "Pulse cycle" commit never happens. In production all these exist; the sandbox
# must seed them so the auto-commit path is exercised faithfully (a dir entry
# needs a file under it to resolve).
_PULSE_RUNTIME_SEED = (
    'runbooks/cycle-journal.md',
    'runbooks/cycle-actions.jsonl',
    'agents/pulse/MEMORY.md',
    'agents/pulse/memory/.keep',
    'agents/pulse/.claude/settings.json',
)


class _CleanTreeGuardBase(_RunCycleTestBase):
    """Adds clean-tree-guard fixtures on top of the run_cycle.sh harness."""

    def setUp(self):
        super().setUp()
        # Seed every PULSE_RUNTIME_PATHS pathspec so the auto-commit's
        # all-or-nothing `git add` resolves (see _PULSE_RUNTIME_SEED).
        for rel in _PULSE_RUNTIME_SEED:
            p = self.repo_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text('{}\n' if rel.endswith('.json') else '')
        self._git('add', '-A')
        self._git('commit', '-q', '-m', 'seed pulse runtime paths')
        self._git('push', '-q', 'origin', 'main')

    def _install_stub(self, body: str) -> None:
        stub = self.stub_bin / 'claude'
        stub.write_text(body)
        os.chmod(stub, 0o755)

    def _seed_and_commit(self, files: dict[str, str], msg: str) -> None:
        for rel, content in files.items():
            p = self.repo_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            self._git('add', rel)
        self._git('commit', '-q', '-m', msg)
        self._git('push', '-q', 'origin', 'main')

    def _stray_diffs(self):
        logs = self.home / 'agents' / 'logs'
        return sorted(logs.glob('stray-cycle-edits-*.diff')) if logs.exists() else []

    def _clean(self, rel: str) -> bool:
        return subprocess.run(
            ['git', '-C', str(self.repo_dir), 'diff', '--quiet', '--', rel],
        ).returncode == 0

    def _stray_alerts(self):
        return [
            a for a in self._read_alerts()
            if a.get('subject') == 'cycle:stray-tree-edit-reverted'
        ]


class StrayEditRevertedTest(_CleanTreeGuardBase):
    """A stray edit to a governed tracked file is reverted + archived + alerted;
    a concurrent Pulse-owned edit is still committed (not reverted)."""

    def test_revert_stray_keep_pulse_owned(self):
        base_cfg = '{"_schema": {}}\n'
        self._seed_and_commit(
            {
                'config/alert-translations.json': base_cfg,
                'runbooks/cycle-journal.md': 'baseline\n',
            },
            'seed config + journal',
        )
        # Stub /cycle: edits a governed tracked file (stray) AND a Pulse-owned one.
        self._install_stub(
            '#!/usr/bin/env bash\n'
            'printf "STRAY\\n" >> "$HOME/agent-core/config/alert-translations.json"\n'
            'printf "cycle note\\n" >> "$HOME/agent-core/runbooks/cycle-journal.md"\n'
            "echo '{\"total_cost_usd\": 0.0, \"duration_ms\": 1}'\n"
            'exit 0\n'
        )

        result = self._run_cycle()
        self.assertEqual(result.returncode, 0, f'stderr={result.stderr.decode()[:600]}')

        # Stray file reverted to its committed baseline + clean.
        self.assertEqual(
            (self.repo_dir / 'config/alert-translations.json').read_text(), base_cfg)
        self.assertTrue(self._clean('config/alert-translations.json'),
                        'stray file must be clean after revert')

        # Diff archived before revert (nothing lost).
        diffs = self._stray_diffs()
        self.assertEqual(len(diffs), 1, f'expected one archived stray diff, got {diffs}')
        self.assertIn('alert-translations.json', diffs[0].read_text())

        # FYI self-heal alert emitted, naming the file.
        alerts = self._stray_alerts()
        self.assertEqual(len(alerts), 1, f'alerts={self._read_alerts()}')
        self.assertEqual(alerts[0]['source'], 'pulse-cycle')
        self.assertIn('config/alert-translations.json', alerts[0]['message'])

        # Pulse-owned journal change was COMMITTED (not reverted).
        head_subj = subprocess.run(
            ['git', '-C', str(self.repo_dir), 'log', '-1', '--format=%s'],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertTrue(head_subj.startswith('Pulse cycle '), f'HEAD subject={head_subj!r}')
        self.assertIn('cycle note',
                      (self.repo_dir / 'runbooks/cycle-journal.md').read_text())


class ToleratedRuntimeNotRevertedTest(_CleanTreeGuardBase):
    """A healer-owned runtime file (SYNC_EXTRA_RUNTIME_PATHS) modified during a
    cycle is TOLERATED — left for its own committer, never reverted, no alert."""

    def test_captures_json_left_for_its_owner(self):
        self._seed_and_commit({'agents/beacon/captures.json': '[]\n'}, 'seed captures')
        self._install_stub(
            '#!/usr/bin/env bash\n'
            'printf "[1]\\n" > "$HOME/agent-core/agents/beacon/captures.json"\n'
            "echo '{\"total_cost_usd\": 0.0, \"duration_ms\": 1}'\n"
            'exit 0\n'
        )

        result = self._run_cycle()
        self.assertEqual(result.returncode, 0, f'stderr={result.stderr.decode()[:600]}')

        # Tolerated file keeps the cycle's write — NOT reverted.
        self.assertEqual(
            (self.repo_dir / 'agents/beacon/captures.json').read_text(), '[1]\n')
        # No stray alert, no archived diff.
        self.assertEqual(self._stray_alerts(), [])
        self.assertEqual(self._stray_diffs(), [])


class StrayRevertedOnFailedCycleTest(_CleanTreeGuardBase):
    """The guard runs even when the cycle itself failed (CYCLE_OK=0), so a stray
    left by a crashing cycle is still cleaned."""

    def test_revert_runs_when_cycle_fails(self):
        base_cfg = '{"_schema": {}}\n'
        self._seed_and_commit({'config/alert-translations.json': base_cfg}, 'seed config')
        # Stub makes the stray edit then exits non-zero → CYCLE_OK=0.
        self._install_stub(
            '#!/usr/bin/env bash\n'
            'printf "STRAY\\n" >> "$HOME/agent-core/config/alert-translations.json"\n'
            "echo '{\"error\": \"boom\"}'\n"
            'exit 1\n'
        )

        result = self._run_cycle()
        self.assertNotEqual(result.returncode, 0, 'cycle should report failure')

        # Stray still reverted despite the failed cycle.
        self.assertEqual(
            (self.repo_dir / 'config/alert-translations.json').read_text(), base_cfg)
        self.assertTrue(self._clean('config/alert-translations.json'))
        self.assertEqual(len(self._stray_alerts()), 1, f'alerts={self._read_alerts()}')


if __name__ == '__main__':
    unittest.main()
