#!/usr/bin/env python3
"""Tests for merge-gate-deep-review-hold.

Spec: docs/merge-gate-deep-review-spec.md (PR #786). A PASS'd PR that touches a
configured critical-path fileset (or carries a `deep-review-required` label)
AND lacks a `deep-review-passed` stamp is HELD for a human `/code-review high` +
manual merge instead of auto-merging. It reuses the `held_conflict` seam:
terminal `held_deep_review` outcome, a closing DM, a status-line variant, no
`outcome=failed` log (so no healer retry).

Acceptance criteria covered (spec §3):
  - fileset-touching PR does NOT merge → held_deep_review, one DM, no
    `outcome=failed`, no merge call;
  - `deep-review-required`-labeled PR holds even off the fileset;
  - non-fileset unlabeled PR merges exactly as today (no behavior change);
  - malformed/missing config → code-default fileset still holds;
  - a `deep-review-passed`-stamped fileset PR flows through to merge;
  - the status line + DM never say "MERGED" for held_deep_review;
  - labels-fetch failure holds a file-critical PR (conservative-fail).

Run:
    python3 -m unittest scripts.tests.test_merge_gate_deep_review
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_AGENTS_ROOT_BACKUP = None
_AGENTS_ROOT_TMPDIR = None
_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook
    global _AGENTS_ROOT_BACKUP, _AGENTS_ROOT_TMPDIR, _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()
    _AGENTS_ROOT_BACKUP = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    _AGENTS_ROOT_TMPDIR = tempfile.mkdtemp(prefix='deep-review-gate-test-')
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_TMPDIR
    for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
        Path(_AGENTS_ROOT_TMPDIR, sub).mkdir(exist_ok=True)
    import outbox_notifier
    importlib.reload(outbox_notifier)


def tearDownModule():  # noqa: N802
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)
    if _AGENTS_ROOT_BACKUP is None:
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
    else:
        os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_BACKUP
    if _AGENTS_ROOT_TMPDIR:
        shutil.rmtree(_AGENTS_ROOT_TMPDIR, ignore_errors=True)
    import outbox_notifier
    importlib.reload(outbox_notifier)


REPO = 'Larry-Yatch/ourliberty-agent-core'


def _pr_url(repo: str, n: int) -> str:
    return f'https://github.com/{repo}/pull/{n}'


class _CompletedProc:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _GhRouter:
    """Minimal gh stand-in: per-PR files/mergeable/labels + merge recorder.

    `labels_fail` forces `gh pr view --json labels` to a non-zero exit so the
    conservative-fail path (unreadable labels) can be exercised.
    """

    def __init__(self):
        self.pr_files: dict[tuple[str, int], list[str]] = {}
        self.pr_mergeable: dict[tuple[str, int], str] = {}
        self.pr_labels: dict[tuple[str, int], list[str]] = {}
        self.open_prs: dict[str, list[dict]] = {}
        self.merge_calls: list[tuple[str, int]] = []
        self.labels_fail = False

    def __call__(self, cmd, capture_output=True, text=True, timeout=None,
                 **kwargs):
        if not cmd or cmd[0] != 'gh':
            return _CompletedProc(stdout='', stderr='not gh', returncode=1)
        if cmd[1] == 'pr' and cmd[2] == 'view':
            pr_n = int(cmd[3])
            repo = cmd[cmd.index('--repo') + 1]
            fields = cmd[cmd.index('--json') + 1].split(',')
            if 'labels' in fields and self.labels_fail:
                return _CompletedProc(stdout='', stderr='api boom',
                                      returncode=1)
            payload: dict = {}
            if 'files' in fields:
                payload['files'] = [
                    {'path': p} for p in self.pr_files.get((repo, pr_n), [])
                ]
            if 'mergeable' in fields:
                m = self.pr_mergeable.get((repo, pr_n), 'MERGEABLE')
                payload['mergeable'] = m
                payload['mergeStateStatus'] = (
                    'CLEAN' if m == 'MERGEABLE' else
                    'DIRTY' if m == 'CONFLICTING' else 'UNKNOWN'
                )
            if 'labels' in fields:
                payload['labels'] = [
                    {'name': n} for n in self.pr_labels.get((repo, pr_n), [])
                ]
            if 'state' in fields:
                payload['state'] = 'OPEN'
            if 'baseRefOid' in fields:
                payload['baseRefOid'] = 'base000000000000'
            if 'headRefOid' in fields:
                payload['headRefOid'] = f'head{pr_n:012d}'
            return _CompletedProc(stdout=json.dumps(payload), returncode=0)
        if cmd[1] == 'pr' and cmd[2] == 'list':
            repo = cmd[cmd.index('--repo') + 1]
            return _CompletedProc(
                stdout=json.dumps(self.open_prs.get(repo, [])), returncode=0)
        if cmd[1] == 'pr' and cmd[2] == 'merge':
            pr_n = int(cmd[3])
            repo = cmd[cmd.index('--repo') + 1]
            self.merge_calls.append((repo, pr_n))
            return _CompletedProc(returncode=0)
        return _CompletedProc(stdout='', stderr='unknown gh', returncode=1)


class _GateTestBase(unittest.TestCase):
    def setUp(self):
        import outbox_notifier as on
        import larry_alerts as la
        self.on = on
        self.la = la
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._orig = {
            'AGENTS_ROOT': on.AGENTS_ROOT,
            'AUTO_MERGE_QUEUE_FILE': on.AUTO_MERGE_QUEUE_FILE,
            'LOG_FILE': on.LOG_FILE,
            'BLACKBOARD': on.BLACKBOARD,
            'CFG_PATH': on._DEEP_REVIEW_PATHS_CONFIG_PATH,
        }
        on.AGENTS_ROOT = self._root
        on.AUTO_MERGE_QUEUE_FILE = self._root / 'state' / 'auto-merge-queue.json'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.BLACKBOARD = self._root / 'blackboard'
        for sub in ('state', 'logs', 'blackboard'):
            (self._root / sub).mkdir(parents=True, exist_ok=True)
        self._la_orig = {
            'AGENTS_ROOT': la.AGENTS_ROOT,
            'ALERTS_FILE': la.ALERTS_FILE,
            'COOLDOWN_ROOT': la.COOLDOWN_ROOT,
            'OFFSET_FILE': la.OFFSET_FILE,
        }
        la.AGENTS_ROOT = self._root
        la.ALERTS_FILE = self._root / 'blackboard' / 'larry-alerts.jsonl'
        la.COOLDOWN_ROOT = self._root / 'state' / 'alert-cooldown'
        la.OFFSET_FILE = self._root / 'state' / 'beacon-alerts-offset.txt'
        # Point the deep-review config at a tmp path so tests own it; default
        # = absent → code-default fileset. Individual tests write it if needed.
        self._cfg = self._root / 'config' / 'deep-review-paths.json'
        self._cfg.parent.mkdir(parents=True, exist_ok=True)
        on._DEEP_REVIEW_PATHS_CONFIG_PATH = self._cfg
        self.gh = _GhRouter()
        self._patcher = mock.patch(
            'outbox_notifier.subprocess.run', side_effect=self.gh)
        self._patcher.start()
        on._reset_auto_merge_queue_state()
        on._invalidate_loop_bounds_cache()
        on._invalidate_deep_review_paths_cache()

    def tearDown(self):
        self._patcher.stop()
        on = self.on
        for k, v in self._orig.items():
            if k == 'CFG_PATH':
                on._DEEP_REVIEW_PATHS_CONFIG_PATH = v
            else:
                setattr(on, k, v)
        for k, v in self._la_orig.items():
            setattr(self.la, k, v)
        on._reset_auto_merge_queue_state()
        on._invalidate_loop_bounds_cache()
        on._invalidate_deep_review_paths_cache()
        self._tmp.cleanup()

    def _read_alerts(self) -> list[dict]:
        path = self.la.ALERTS_FILE
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def _log_text(self) -> str:
        p = self.on.LOG_FILE
        return p.read_text(encoding='utf-8') if p.exists() else ''

    def _attempt(self, pr_n: int, *, changed_files, repo=REPO, **kw) -> dict:
        return self.on._attempt_auto_merge_with_gates(
            pr_url=_pr_url(repo, pr_n),
            repo_coords=repo,
            pr_number=pr_n,
            task_id=kw.get('task_id', f'task-{pr_n}'),
            summary=kw.get('summary', f'summary #{pr_n}'),
            chat_id=kw.get('chat_id', 999),
            changed_files=changed_files,
        )


class HoldTest(_GateTestBase):
    def test_fileset_pr_is_held_not_merged(self):
        # scripts/decision_*.py is in the code-default fileset.
        r = self._attempt(50, changed_files=['scripts/decision_resolve.py'])
        self.assertEqual(r['merge_outcome'], 'held_deep_review')
        self.assertEqual(self.gh.merge_calls, [])          # NOT merged
        # Exactly one DM, and it hands the merge_reviewed_pr.sh command.
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('merge_reviewed_pr.sh 50', alerts[0]['message'])
        self.assertNotIn('MERGED', alerts[0]['message'])
        # The WARN log must NOT carry `outcome=failed` (else heal_pr_auto_merge
        # would retry it). It DOES carry the held marker.
        log = self._log_text()
        self.assertIn('AUTO_MERGE_HELD_DEEP_REVIEW', log)
        self.assertNotIn('outcome=failed', log)

    def test_label_forces_hold_off_fileset(self):
        self.gh.pr_labels[(REPO, 51)] = ['auto-review', 'deep-review-required']
        r = self._attempt(51, changed_files=['docs/whatever.md'])
        self.assertEqual(r['merge_outcome'], 'held_deep_review')
        self.assertEqual(self.gh.merge_calls, [])

    def test_labels_fetch_failure_holds_file_critical_pr(self):
        # Conservative-fail: gh labels read errors → stamp unconfirmable →
        # a file-critical PR still holds.
        self.gh.labels_fail = True
        r = self._attempt(52, changed_files=['scripts/outbox_notifier.py'])
        self.assertEqual(r['merge_outcome'], 'held_deep_review')
        self.assertEqual(self.gh.merge_calls, [])


class PassThroughTest(_GateTestBase):
    def test_non_fileset_unlabeled_pr_merges(self):
        r = self._attempt(60, changed_files=['docs/readme.md', 'app/x.tsx'])
        self.assertEqual(r['merge_outcome'], 'merged')
        self.assertEqual(self.gh.merge_calls, [(REPO, 60)])

    def test_stamped_fileset_pr_merges(self):
        # A critical-path PR Claude already /code-review high'd + stamped flows
        # straight through Mirror to auto-merge.
        self.gh.pr_labels[(REPO, 61)] = ['auto-review', 'deep-review-passed']
        r = self._attempt(61, changed_files=['scripts/decision_resolve.py'])
        self.assertEqual(r['merge_outcome'], 'merged')
        self.assertEqual(self.gh.merge_calls, [(REPO, 61)])
        self.assertEqual(self._read_alerts(), [])          # no hold DM


class ConfigTest(_GateTestBase):
    def test_missing_config_uses_code_default(self):
        # No config file written → default fileset still holds a decision_*.py.
        self.assertFalse(self._cfg.exists())
        r = self._attempt(70, changed_files=['scripts/trust_policy.py'])
        self.assertEqual(r['merge_outcome'], 'held_deep_review')

    def test_malformed_config_uses_code_default(self):
        self._cfg.write_text('{not valid json', encoding='utf-8')
        self.on._invalidate_deep_review_paths_cache()
        r = self._attempt(71, changed_files=['scripts/larry_alerts.py'])
        self.assertEqual(r['merge_outcome'], 'held_deep_review')

    def test_config_override_narrows_fileset(self):
        # A config that only lists a made-up path lets a former-default path
        # (scripts/larry_alerts.py) merge — proves the file overrides default.
        self._cfg.write_text(json.dumps(
            {'enabled': True, 'paths': ['scripts/only_this_one.py']}),
            encoding='utf-8')
        self.on._invalidate_deep_review_paths_cache()
        r = self._attempt(72, changed_files=['scripts/larry_alerts.py'])
        self.assertEqual(r['merge_outcome'], 'merged')
        # ...but the configured path holds.
        r2 = self._attempt(73, changed_files=['scripts/only_this_one.py'])
        self.assertEqual(r2['merge_outcome'], 'held_deep_review')

    def test_disabled_config_falls_back_to_default(self):
        # enabled:false → code default (NOT "no paths"): a decision_*.py holds.
        self._cfg.write_text(json.dumps(
            {'enabled': False, 'paths': ['scripts/only_this_one.py']}),
            encoding='utf-8')
        self.on._invalidate_deep_review_paths_cache()
        r = self._attempt(74, changed_files=['scripts/decision_resolve.py'])
        self.assertEqual(r['merge_outcome'], 'held_deep_review')


class RenderInvariantTest(_GateTestBase):
    def test_status_line_never_says_merged(self):
        line = self.on._render_review_pass_merge_status_line(
            {'merge_outcome': 'held_deep_review'})
        self.assertNotIn('MERGED', line)
        self.assertIn('HELD', line)
        self.assertIn('/code-review high', line)

    def test_dm_variant_registered_and_no_merged_word(self):
        self.assertIn('held_deep_review', self.on._REVIEW_PASS_DM_VARIANTS)
        body = self.on._REVIEW_PASS_DM_VARIANTS['held_deep_review']
        self.assertNotIn('MERGED', body)
        self.assertIn('merge_reviewed_pr.sh', body)


class PredicateUnitTest(_GateTestBase):
    """Direct `_deep_review_required` / `_load_deep_review_paths` unit tests
    (labels passed explicitly → no gh call)."""

    def test_predicate_true_on_fileset_no_stamp(self):
        self.assertTrue(self.on._deep_review_required(
            REPO, 1, ['scripts/for_larry_signal.py'], labels=['auto-review']))

    def test_predicate_false_when_stamped(self):
        self.assertFalse(self.on._deep_review_required(
            REPO, 1, ['scripts/for_larry_signal.py'],
            labels=['deep-review-passed']))

    def test_predicate_false_non_critical(self):
        self.assertFalse(self.on._deep_review_required(
            REPO, 1, ['docs/x.md'], labels=[]))

    def test_predicate_true_on_required_label(self):
        self.assertTrue(self.on._deep_review_required(
            REPO, 1, ['docs/x.md'], labels=['deep-review-required']))

    def test_required_and_passed_both_present_holds(self):
        # A stamp that's stale-but-present would pass; but if BOTH labels are
        # on the PR, `deep-review-passed` wins (stamped) — documents the
        # label-precedence so a reviewer can un-hold by stamping.
        self.assertFalse(self.on._deep_review_required(
            REPO, 1, ['docs/x.md'],
            labels=['deep-review-required', 'deep-review-passed']))

    def test_loader_default_on_missing(self):
        self.assertFalse(self._cfg.exists())
        self.on._invalidate_deep_review_paths_cache()
        self.assertEqual(self.on._load_deep_review_paths(),
                         self.on._DEFAULT_DEEP_REVIEW_PATHS)


if __name__ == '__main__':
    unittest.main()
