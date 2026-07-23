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
DASHBOARD_REPO = 'Larry-Yatch/ourliberty-dashboard'


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
        self.files_fail = False
        # deep-review-sha-bound-approval slice 1. `commit_statuses` maps a head
        # SHA → the list of `deep-review`-context states GitHub would return for
        # that commit; default empty → not stamped (all prior tests unaffected).
        # `status_read_calls` / `status_post_calls` let tests assert the
        # zero-cost short-circuit and the dual-write.
        self.commit_statuses: dict[str, list[str]] = {}
        self.status_read_calls: list[str] = []
        self.status_post_calls: list[tuple[str, str, str]] = []
        self.status_post_fail = False
        self.status_read_fail = False

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
            if 'files' in fields and self.files_fail:
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
        # deep-review-sha-bound-approval slice 1 — commit-status READ:
        #   gh api repos/{repo}/commits/{sha}/status --jq '[...]'
        # The router returns the post-jq array of states directly.
        if (cmd[1] == 'api' and len(cmd) > 2
                and '/commits/' in cmd[2] and cmd[2].endswith('/status')):
            sha = cmd[2].split('/commits/')[1].rsplit('/status', 1)[0]
            self.status_read_calls.append(sha)
            if self.status_read_fail:
                return _CompletedProc(stdout='', stderr='api boom',
                                      returncode=1)
            return _CompletedProc(
                stdout=json.dumps(self.commit_statuses.get(sha, [])),
                returncode=0)
        # commit-status WRITE:  gh api repos/{repo}/statuses/{sha} -f state=...
        if (cmd[1] == 'api' and len(cmd) > 2 and '/statuses/' in cmd[2]):
            sha = cmd[2].split('/statuses/')[1]
            state = next((cmd[i + 1].split('=', 1)[1]
                          for i, a in enumerate(cmd)
                          if a == '-f' and cmd[i + 1].startswith('state=')),
                         '')
            self.status_post_calls.append((sha, state, cmd[2]))
            if self.status_post_fail:
                return _CompletedProc(stdout='', stderr='status boom',
                                      returncode=1)
            self.commit_statuses.setdefault(sha, [])
            if state and state not in self.commit_statuses[sha]:
                self.commit_statuses[sha].append(state)
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
            'DEEP_REVIEW_HELD_FILE': on.DEEP_REVIEW_HELD_FILE,
            'LOG_FILE': on.LOG_FILE,
            'BLACKBOARD': on.BLACKBOARD,
            'CFG_PATH': on._DEEP_REVIEW_PATHS_CONFIG_PATH,
        }
        on.AGENTS_ROOT = self._root
        on.AUTO_MERGE_QUEUE_FILE = self._root / 'state' / 'auto-merge-queue.json'
        on.DEEP_REVIEW_HELD_FILE = self._root / 'state' / 'deep-review-held-prs.json'
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

    def test_files_fetch_failure_holds_conservatively(self):
        # The durable fileset trigger must fail CLOSED: if changed_files is
        # empty (coalesced from a gh files-fetch failure) AND the re-fetch also
        # fails, the PR is held rather than silently merged unreviewed.
        self.gh.files_fail = True
        r = self._attempt(53, changed_files=[])
        self.assertEqual(r['merge_outcome'], 'held_deep_review')
        self.assertEqual(self.gh.merge_calls, [])

    def test_files_fetch_failure_but_stamped_merges(self):
        # ...but a stamped PR still flows even when files are unresolvable —
        # the human review already happened.
        self.gh.files_fail = True
        self.gh.pr_labels[(REPO, 54)] = ['deep-review-passed']
        r = self._attempt(54, changed_files=[])
        self.assertEqual(r['merge_outcome'], 'merged')

    def test_conflicting_critical_pr_is_held_conflict_not_deep_review(self):
        # Ordering: the deep-review gate sits AFTER the mergeable gate, so a
        # CONFLICTING critical-path PR surfaces as held_conflict (rebase DM),
        # not held_deep_review — Larry gets the right instruction.
        self.gh.pr_mergeable[(REPO, 55)] = 'CONFLICTING'
        r = self._attempt(55, changed_files=['scripts/decision_resolve.py'])
        self.assertEqual(r['merge_outcome'], 'held_conflict')
        self.assertEqual(self.gh.merge_calls, [])

    def test_dashboard_repo_label_only_hold(self):
        # The fileset globs are agent-core paths, so a dashboard-repo PR can
        # only be held via the explicit label. Verify that path works cross-repo.
        self.gh.pr_labels[(DASHBOARD_REPO, 56)] = [
            'auto-review', 'deep-review-required']
        r = self._attempt(56, repo=DASHBOARD_REPO,
                          changed_files=['app/api/larry/route.ts'])
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

    def test_missing_enabled_key_honors_paths(self):
        # A config that supplies `paths` but omits `enabled` must be HONORED
        # (enabled defaults True) — else a Pulse-Check edit silently no-ops.
        self._cfg.write_text(json.dumps(
            {'paths': ['scripts/only_this_one.py']}), encoding='utf-8')
        self.on._invalidate_deep_review_paths_cache()
        self.assertEqual(self.on._load_deep_review_paths(),
                         ('scripts/only_this_one.py',))
        # And it takes effect: a former-default path now merges...
        r = self._attempt(75, changed_files=['scripts/larry_alerts.py'])
        self.assertEqual(r['merge_outcome'], 'merged')
        # ...while the configured path holds.
        r2 = self._attempt(76, changed_files=['scripts/only_this_one.py'])
        self.assertEqual(r2['merge_outcome'], 'held_deep_review')


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


class ShaBoundTokenSlice1Test(_GateTestBase):
    """deep-review-sha-bound-approval slice 1 — the gate accepts a SHA-bound
    `deep-review` success commit status as a stamp equivalent to the legacy
    `deep-review-passed` label (dual-read), the label short-circuits so the
    common path pays no extra gh cost, the status reader fails CLOSED, and the
    approval path writes both tokens (dual-write, status POST checked).

    The router models the head SHA as `head{pr:012d}`, so a status seeded under
    that key is what the lazily-resolved head will read.
    """

    FILE = 'scripts/for_larry_signal.py'  # on the code-default fileset

    def _head(self, pr_n: int) -> str:
        return f'head{pr_n:012d}'

    # ---- dual-read: status is a stamp-equivalent -----------------------------

    def test_sha_status_stamps_a_critical_pr_without_the_label(self):
        # File-critical, NO `deep-review-passed` label, but the head carries a
        # success `deep-review` status → NOT required (flows through to merge).
        self.gh.commit_statuses[self._head(1)] = ['success']
        self.assertFalse(self.on._deep_review_required(
            REPO, 1, [self.FILE], labels=['auto-review']))

    def test_no_token_at_all_still_holds(self):
        # Behavior-preserving: file-critical, no label, no status → HELD.
        self.assertTrue(self.on._deep_review_required(
            REPO, 1, [self.FILE], labels=['auto-review']))
        # And it did pay for one status read on the hold path.
        self.assertEqual(self.gh.status_read_calls, [self._head(1)])

    def test_label_short_circuits_no_status_read(self):
        # The legacy label present → stamped WITHOUT any commit-status gh call.
        self.assertFalse(self.on._deep_review_required(
            REPO, 1, [self.FILE], labels=['deep-review-passed']))
        self.assertEqual(self.gh.status_read_calls, [])

    def test_passed_head_sha_avoids_a_second_head_resolve(self):
        # When a caller already has the head, the gate uses it verbatim.
        self.gh.commit_statuses[self._head(1)] = ['success']
        self.assertFalse(self.on._deep_review_required(
            REPO, 1, [self.FILE], labels=['auto-review'],
            head_sha=self._head(1)))
        self.assertEqual(self.gh.status_read_calls, [self._head(1)])

    # ---- reader fails CLOSED --------------------------------------------------

    def test_non_success_status_does_not_stamp(self):
        for bad in (['failure'], ['pending'], ['error'], []):
            with self.subTest(states=bad):
                self.gh.commit_statuses[self._head(2)] = bad
                self.assertTrue(self.on._deep_review_required(
                    REPO, 2, [self.FILE], labels=[]))

    def test_reader_false_on_gh_error(self):
        self.gh.status_read_fail = True
        self.gh.commit_statuses[self._head(3)] = ['success']  # ignored on error
        self.assertTrue(self.on._deep_review_required(
            REPO, 3, [self.FILE], labels=[]))

    def test_reader_false_on_unresolvable_head_no_call(self):
        self.assertFalse(
            self.on._head_has_deep_review_pass_status(REPO, ''))
        self.assertEqual(self.gh.status_read_calls, [])

    def test_reader_true_only_on_success(self):
        self.gh.commit_statuses['abc'] = ['success']
        self.assertTrue(
            self.on._head_has_deep_review_pass_status(REPO, 'abc'))

    # ---- dual-write: checked status POST -------------------------------------

    def test_post_status_writes_success_and_returns_true(self):
        ok = self.on._post_deep_review_pass_status(
            REPO, 4, _pr_url(REPO, 4), self._head(4))
        self.assertTrue(ok)
        self.assertEqual(len(self.gh.status_post_calls), 1)
        sha, state, _ = self.gh.status_post_calls[0]
        self.assertEqual((sha, state), (self._head(4), 'success'))
        # Round-trips: the reader now sees it.
        self.assertTrue(
            self.on._head_has_deep_review_pass_status(REPO, self._head(4)))

    def test_post_status_failure_returns_false_and_alerts(self):
        self.gh.status_post_fail = True
        ok = self.on._post_deep_review_pass_status(
            REPO, 5, _pr_url(REPO, 5), self._head(5))
        self.assertFalse(ok)
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        rec = alerts[0]
        # Status-POST-specific alert: its OWN subject key, so it never
        # subject-dedupes against a genuine label-apply failure.
        self.assertEqual(
            rec['subject'], f'deep-review-hold-status-post:{REPO}:5')
        msg = rec['message']
        # Accurate description of a STATUS-post failure — not a label failure.
        self.assertIn('commit status', msg)
        self.assertNotIn('--add-label', msg)
        # Correct, non-no-op remediation: the real gh api statuses one-liner.
        self.assertIn(
            f'gh api repos/{REPO}/statuses/{self._head(5)}', msg)
        self.assertIn('-f state=success', msg)
        self.assertIn('-f context=deep-review', msg)

    def test_status_post_subject_distinct_from_label_apply(self):
        """A status-POST failure and a label-apply failure on the SAME PR carry
        different subjects, so the two never collapse into one DM."""
        self.gh.status_post_fail = True
        self.on._post_deep_review_pass_status(
            REPO, 9, _pr_url(REPO, 9), self._head(9))
        status_subj = self._read_alerts()[-1]['subject']
        # `gh pr edit --add-label` is unhandled by the router → exit 1 → the
        # label-apply failure path fires (the PR has no pre-existing label).
        self.on._apply_deep_review_pass_label(
            REPO, 9, _pr_url(REPO, 9), 'task-9')
        label_subj = self._read_alerts()[-1]['subject']
        self.assertNotEqual(status_subj, label_subj)
        self.assertEqual(status_subj, f'deep-review-hold-status-post:{REPO}:9')
        self.assertEqual(label_subj, f'deep-review-hold-label-apply:{REPO}:9')

    def test_post_status_unresolvable_head_alerts_no_call(self):
        ok = self.on._post_deep_review_pass_status(
            REPO, 6, _pr_url(REPO, 6), '')
        self.assertFalse(ok)
        self.assertEqual(self.gh.status_post_calls, [])
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        rec = alerts[0]
        self.assertEqual(
            rec['subject'], f'deep-review-hold-status-post:{REPO}:6')
        # No head → no manual post is possible; no bogus gh command handed out.
        self.assertNotIn('gh api repos/', rec['message'])


class ConfigConstantSyncTest(unittest.TestCase):
    """The shipped config/deep-review-paths.json and the code-default constant
    are maintained by hand ("the config file IS this list"). Pin them together
    so a one-sided edit can't silently let the fallback default drift from the
    active config."""

    def test_default_constant_matches_shipped_config(self):
        import outbox_notifier as on
        cfg_path = (Path(__file__).resolve().parent.parent.parent
                    / 'config' / 'deep-review-paths.json')
        data = json.loads(cfg_path.read_text(encoding='utf-8'))
        self.assertEqual(data.get('paths'),
                         list(on._DEFAULT_DEEP_REVIEW_PATHS))


class DeepReviewHeldDmOnceTest(_GateTestBase):
    """review-dispatch-post-auto-merge-held: a repeat hold of the SAME head
    records the held state but does NOT re-DM Larry; a new head re-notifies."""

    def test_dm_fires_once_per_head(self):
        cf = ['scripts/decision_resolve.py']  # code-default critical fileset
        # First hold -> held, DM #1.
        r1 = self._attempt(70, changed_files=cf)
        self.assertEqual(r1['merge_outcome'], 'held_deep_review')
        self.assertEqual(len(self._read_alerts()), 1)
        held = self.on._find_deep_review_held(REPO, 70)
        self.assertIsNotNone(held)
        self.assertEqual(held['head_sha'], 'head000000000070')  # fake gh head
        # Second hold at the SAME (unchanged) head -> still held, but no re-DM.
        r2 = self._attempt(70, changed_files=cf)
        self.assertEqual(r2['merge_outcome'], 'held_deep_review')
        self.assertEqual(len(self._read_alerts()), 1)          # NOT 2
        self.assertIn('repeat hold', self._log_text())
        self.assertEqual(self.gh.merge_calls, [])              # never merged


class DeepReviewHoldBroadcastRouteTest(_GateTestBase):
    """merge-held-deep-review-notifier-tier4-001: the broadcast fallback (no
    reply chat_id, e.g. autonomous build-sequence / Pulse-cycle PRs) must emit
    route='escalate' so the Telegram bot DMs Larry instead of skipping the
    'outbox-notifier'-graduated 'hold' route at read-time."""

    def test_broadcast_fallback_emits_escalate_route(self):
        self.on._dm_larry_deep_review_hold(
            pr_url=_pr_url(REPO, 823),
            pr_number=823,
            repo_coords=REPO,
            task_id='autonomous-seq-step-823',
            chat_id=None,               # autonomous PR → broadcast fallback
            summary='fan-out change',
        )
        alerts = self._read_alerts()
        self.assertEqual(len(alerts), 1)
        rec = alerts[0]
        self.assertEqual(rec['route'], 'escalate')      # the fix: not 'hold'
        self.assertEqual(rec['severity'], 'warning')    # severity unchanged
        # subject dedup key preserved (per-(repo,PR) cooldown still works).
        self.assertEqual(
            rec['subject'], f'auto-merge-deep-review-hold:{REPO}:823')
        self.assertIn('merge_reviewed_pr.sh 823', rec['message'])

    def test_reply_chat_path_untouched_no_broadcast_alert(self):
        # chat_id is an int → the append_notification path fires and returns;
        # the broadcast append_alert must NOT run (no route/severity record).
        self.on._dm_larry_deep_review_hold(
            pr_url=_pr_url(REPO, 830),
            pr_number=830,
            repo_coords=REPO,
            task_id='chat-initiated-830',
            chat_id=4242,
            summary='',
        )
        records = self._read_alerts()
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.get('kind'), 'notification')   # not a broadcast
        self.assertNotIn('route', rec)
        self.assertEqual(rec.get('chat_id'), 4242)


if __name__ == '__main__':
    unittest.main()
