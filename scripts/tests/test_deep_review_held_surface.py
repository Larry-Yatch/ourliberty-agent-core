#!/usr/bin/env python3
"""Tests for deep-review-held-surface-on-tab (deep-review-held-surface-on-tab-001).

Spec: the merge gate HOLDS critical-path PRs that PASS'd Mirror but lack the
`deep-review-passed` stamp (persisted in DEEP_REVIEW_HELD_FILE) — but nothing
surfaced them to Larry, so held PRs sat parked. `_reconcile_deep_review_held_
approvals` closes the gap: every held entry gets exactly one live, non-null-chat
binary approval_request on the Approvals tab; APPROVE mechanically stamps
`deep-review-passed` so the existing gate auto-merges; a cleared held entry
resolves its approval off the tab.

Covered:
  - held file with entries → one approval_request per entry (add_pending +
    emit_event), non-null chat via mocked TELEGRAM_ALLOWED_CHAT_IDS; re-run is
    an idempotent no-op;
  - no primary chat id → nothing surfaced (never a null-chat approval);
  - approve → the PR gets `deep-review-passed` (mock gh); idempotent (label
    present → no second gh edit);
  - reject → no label, approval stays resolved-rejected, PR stays held, not
    re-surfaced;
  - held entry clears (merged) → the approval resolves off the tab;
  - new head → old approval resolves, a fresh approval surfaces;
  - label-apply failure on approve → exactly one actionable repair alert; happy
    path fires none;
  - approved but the LIVE head has moved past the approved head → no stamp, no
    merge; our stale stamp is revoked and the hold clears so Mirror re-reviews
    at the new head (an unresolvable live head defers instead of merging).

Run:
    python3 -m unittest scripts.tests.test_deep_review_held_surface
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
from typing import Optional
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
    _AGENTS_ROOT_TMPDIR = tempfile.mkdtemp(prefix='deep-review-surface-test-')
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
CHAT_ID = 424242


def _pr_url(repo: str, n: int) -> str:
    return f'https://github.com/{repo}/pull/{n}'


class _CompletedProc:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _GhRouter:
    """gh stand-in: per-PR labels/files reads + `pr edit --add-label` recorder.

    `edit_fail` forces `gh pr edit` to a non-zero exit so the label-apply
    failure path (repair alert) can be exercised. A successful `--add-label`
    mutates `pr_labels` so idempotency (label already present) is observable.
    """

    def __init__(self):
        self.pr_files: dict[tuple[str, int], list[str]] = {}
        self.pr_labels: dict[tuple[str, int], list[str]] = {}
        # PR liveness for the reconcile self-heal; default OPEN so entries that
        # aren't explicitly resolved behave as still-held (the prior behavior).
        self.pr_state: dict[tuple[str, int], str] = {}
        # Live head per PR (headRefOid). `_seed_held` registers the held head by
        # default; a test that simulates a new push overwrites it.
        self.pr_heads: dict[tuple[str, int], Optional[str]] = {}
        self.edit_calls: list[tuple[str, int, str]] = []
        self.remove_label_calls: list[tuple[str, int, str]] = []
        self.edit_fail = False

    def __call__(self, cmd, capture_output=True, text=True, timeout=None,
                 **kwargs):
        if not cmd or cmd[0] != 'gh':
            return _CompletedProc(stdout='', stderr='not gh', returncode=1)
        if cmd[1] == 'pr' and cmd[2] == 'view':
            pr_n = int(cmd[3])
            repo = cmd[cmd.index('--repo') + 1]
            fields = cmd[cmd.index('--json') + 1].split(',')
            payload: dict = {}
            if 'files' in fields:
                payload['files'] = [
                    {'path': p} for p in self.pr_files.get((repo, pr_n), [])
                ]
            if 'labels' in fields:
                payload['labels'] = [
                    {'name': n} for n in self.pr_labels.get((repo, pr_n), [])
                ]
            if 'state' in fields:
                payload['state'] = self.pr_state.get((repo, pr_n), 'OPEN')
            if 'headRefOid' in fields:
                head = self.pr_heads.get((repo, pr_n))
                if head is not None:
                    payload['headRefOid'] = head
            return _CompletedProc(stdout=json.dumps(payload), returncode=0)
        if cmd[1] == 'pr' and cmd[2] == 'edit' and '--remove-label' in cmd:
            pr_n = int(cmd[3])
            repo = cmd[cmd.index('--repo') + 1]
            label = cmd[cmd.index('--remove-label') + 1]
            self.remove_label_calls.append((repo, pr_n, label))
            labels = self.pr_labels.get((repo, pr_n))
            if labels and label in labels:
                labels.remove(label)
            return _CompletedProc(returncode=0)
        if cmd[1] == 'pr' and cmd[2] == 'edit':
            pr_n = int(cmd[3])
            repo = cmd[cmd.index('--repo') + 1]
            label = cmd[cmd.index('--add-label') + 1]
            self.edit_calls.append((repo, pr_n, label))
            if self.edit_fail:
                return _CompletedProc(stdout='', stderr='label boom',
                                      returncode=1)
            self.pr_labels.setdefault((repo, pr_n), []).append(label)
            return _CompletedProc(returncode=0)
        return _CompletedProc(stdout='', stderr='unknown gh', returncode=1)


class _SurfaceTestBase(unittest.TestCase):
    def setUp(self):
        import outbox_notifier as on
        import larry_alerts as la
        import beacon_approval_handler as approval
        self.on = on
        self.la = la
        self.approval = approval
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._orig = {
            'AGENTS_ROOT': on.AGENTS_ROOT,
            'DEEP_REVIEW_HELD_FILE': on.DEEP_REVIEW_HELD_FILE,
            'LOG_FILE': on.LOG_FILE,
            'BLACKBOARD': on.BLACKBOARD,
            # Recheck live PR state on EVERY reconcile call in tests (production
            # throttles to a slow cadence); reset the throttle clock too.
            '_DEEP_REVIEW_HELD_STATE_RECHECK_INTERVAL_S':
                on._DEEP_REVIEW_HELD_STATE_RECHECK_INTERVAL_S,
        }
        on._DEEP_REVIEW_HELD_STATE_RECHECK_INTERVAL_S = 0
        on._last_deep_review_held_state_recheck_ts = 0.0
        on.AGENTS_ROOT = self._root
        on.DEEP_REVIEW_HELD_FILE = self._root / 'state' / 'deep-review-held-prs.json'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.BLACKBOARD = self._root / 'blackboard'
        for sub in ('state', 'logs', 'blackboard'):
            (self._root / sub).mkdir(parents=True, exist_ok=True)
        # Approval state → tmp (state file + its lock sidecar both derive from
        # PENDING_APPROVALS_PATH, so patching the one attr redirects both).
        self._approval_orig = approval.PENDING_APPROVALS_PATH
        approval.PENDING_APPROVALS_PATH = (
            self._root / 'state' / 'beacon-pending-approvals.json')
        # larry_alerts → tmp so the repair-alert assertions read our file.
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
        self.gh = _GhRouter()
        self._patcher = mock.patch(
            'outbox_notifier.subprocess.run', side_effect=self.gh)
        self._patcher.start()
        # Non-null primary chat via the bot's allow-list env.
        self._env = mock.patch.dict(
            os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': str(CHAT_ID)})
        self._env.start()
        # Keep the Supabase tab feed out of the test; record calls to assert on.
        self._emit = mock.patch('outbox_notifier.chain_event_emit.emit_event')
        self.emit = self._emit.start()
        # Stub the merge chokepoint the approved-branch now drives, so stamp-
        # focused tests stay hermetic (the real gate is exercised elsewhere).
        # `merge_outcome` is per-test tunable; every call is recorded.
        self.merge_calls: list[dict] = []
        self.merge_outcome = 'merged'

        def _fake_merge(*, pr_url, repo_coords, pr_number, task_id, summary,
                        chat_id, changed_files, **kwargs):
            self.merge_calls.append({
                'pr_url': pr_url, 'repo': repo_coords,
                'pr_number': pr_number, 'task_id': task_id,
            })
            return {
                'merge_outcome': self.merge_outcome,
                'pr_number': pr_number, 'repo_coords': repo_coords,
            }

        self._merge = mock.patch(
            'outbox_notifier._attempt_auto_merge_with_gates',
            side_effect=_fake_merge)
        self._merge.start()
        # Per-process drive-tracking set: reset so tests never leak drive-state.
        on._deep_review_merge_driven.clear()
        on._invalidate_deep_review_paths_cache()

    def tearDown(self):
        self._merge.stop()
        self._emit.stop()
        self._env.stop()
        self._patcher.stop()
        self.on._deep_review_merge_driven.clear()
        on = self.on
        for k, v in self._orig.items():
            setattr(on, k, v)
        self.approval.PENDING_APPROVALS_PATH = self._approval_orig
        for k, v in self._la_orig.items():
            setattr(self.la, k, v)
        on._invalidate_deep_review_paths_cache()
        self._tmp.cleanup()

    # ---- helpers ----
    def _held_entry(self, pr_n: int, head_sha: str, repo=REPO) -> dict:
        return {
            'repo': repo,
            'pr_number': pr_n,
            'pr_url': _pr_url(repo, pr_n),
            'task_id': f'task-{pr_n}',
            'head_sha': head_sha,
            'held_at': '2026-07-10T00:00:00+00:00',
        }

    def _seed_held(self, *entries: dict) -> None:
        self.on._save_deep_review_held(list(entries))
        # Default the live head to the held head, so "nothing moved" is the
        # baseline and a test opts INTO a new push by overwriting `pr_heads`.
        for e in entries:
            self.gh.pr_heads[(e['repo'], e['pr_number'])] = e.get('head_sha')

    def _pending(self) -> list[dict]:
        return self.approval.load_state().get('pending', [])

    def _history(self) -> list[dict]:
        return self.approval.load_state().get('history', [])

    def _alerts(self) -> list[dict]:
        p = self.la.ALERTS_FILE
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


class SurfaceTest(_SurfaceTestBase):
    def test_surfaces_one_approval_per_held_entry(self):
        self._seed_held(
            self._held_entry(823, 'aaaaaaaa11112222'),
            self._held_entry(847, 'bbbbbbbb33334444'),
        )
        self.on._reconcile_deep_review_held_approvals()
        pending = self._pending()
        ids = sorted(e['id'] for e in pending)
        self.assertEqual(
            ids,
            ['deep-review-hold-pr823-aaaaaaaa', 'deep-review-hold-pr847-bbbbbbbb'],
        )
        for e in pending:
            self.assertEqual(e['chat_id'], CHAT_ID)               # non-null
            self.assertEqual(e['target_agent'], 'beacon')
            self.assertIn('PASSED Mirror', e['plan_summary'])
        # Tab feed emitted once per surfaced approval.
        self.assertEqual(self.emit.call_count, 2)

    def test_summary_lists_critical_path_files(self):
        self.gh.pr_files[(REPO, 823)] = [
            'scripts/outbox_notifier.py', 'docs/notes.md']
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        summary = self._pending()[0]['plan_summary']
        self.assertIn('scripts/outbox_notifier.py', summary)
        self.assertNotIn('docs/notes.md', summary)   # non-critical excluded

    def test_rerun_is_idempotent(self):
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(len(self._pending()), 1)     # still exactly one
        self.assertEqual(self.emit.call_count, 1)     # not re-emitted

    def test_no_primary_chat_never_surfaces_null_chat(self):
        with mock.patch.dict(os.environ, {'TELEGRAM_ALLOWED_CHAT_IDS': ''}):
            self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
            self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self._pending(), [])
        self.assertEqual(self.emit.call_count, 0)


class ApproveActionTest(_SurfaceTestBase):
    def _surface_and_approve(self, pr_n=823, head='aaaaaaaa11112222'):
        self._seed_held(self._held_entry(pr_n, head))
        self.on._reconcile_deep_review_held_approvals()
        approval_id = f'deep-review-hold-pr{pr_n}-{head[:8]}'
        self.approval.resolve(approval_id, 'approved', note='larry approved')
        return approval_id

    def test_approve_applies_deep_review_passed_label(self):
        self._surface_and_approve()
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(
            self.gh.edit_calls, [(REPO, 823, 'deep-review-passed')])
        self.assertIn('deep-review-passed', self.gh.pr_labels[(REPO, 823)])

    def test_approve_label_apply_is_idempotent(self):
        self._surface_and_approve()
        self.on._reconcile_deep_review_held_approvals()   # applies the label
        self.on._reconcile_deep_review_held_approvals()   # label present → noop
        self.assertEqual(len(self.gh.edit_calls), 1)      # not re-applied
        self.assertEqual(self._alerts(), [])              # happy path: no alert

    def test_label_apply_failure_fires_one_repair_alert(self):
        self.gh.edit_fail = True
        self._surface_and_approve()
        self.on._reconcile_deep_review_held_approvals()
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('deep-review-passed', alerts[0]['message'])
        self.assertIn('gh pr edit 823', alerts[0]['message'])


class RejectActionTest(_SurfaceTestBase):
    def test_reject_no_label_pr_stays_held_not_resurfaced(self):
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        self.approval.resolve(
            'deep-review-hold-pr823-aaaaaaaa', 'rejected', note='keep holding')
        # Held entry stays; a re-run must NOT stamp a label and must NOT
        # re-surface a fresh approval for the same head.
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self.gh.edit_calls, [])
        self.assertEqual(self._pending(), [])
        rejected = [
            e for e in self._history()
            if e['id'] == 'deep-review-hold-pr823-aaaaaaaa'
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['status'], 'rejected')


class ResolveOnClearTest(_SurfaceTestBase):
    def test_merged_pr_resolves_approval_off_tab(self):
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(len(self._pending()), 1)
        # PR merged → gate cleared the held entry.
        self._seed_held()  # empty held list
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self._pending(), [])          # left the tab
        hist = [e for e in self._history()
                if e['id'] == 'deep-review-hold-pr823-aaaaaaaa']
        self.assertEqual(hist[0]['status'], 'expired')

    def test_cleared_with_label_resolves_approved(self):
        self.gh.pr_labels[(REPO, 823)] = ['deep-review-passed']
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        self._seed_held()  # cleared after the label-driven merge
        self.on._reconcile_deep_review_held_approvals()
        hist = [e for e in self._history()
                if e['id'] == 'deep-review-hold-pr823-aaaaaaaa']
        self.assertEqual(hist[0]['status'], 'approved')

    def test_new_head_resolves_old_and_surfaces_new(self):
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        # A genuine revision: same PR, new head → the gate re-recorded the
        # held entry under the new head key.
        self._seed_held(self._held_entry(823, 'cccccccc55556666'))
        self.on._reconcile_deep_review_held_approvals()
        pending_ids = [e['id'] for e in self._pending()]
        self.assertEqual(pending_ids, ['deep-review-hold-pr823-cccccccc'])
        old = [e for e in self._history()
               if e['id'] == 'deep-review-hold-pr823-aaaaaaaa']
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]['status'], 'expired')


class MergedOutOfBandTest(_SurfaceTestBase):
    """The reconcile must reconcile against LIVE PR state, not just the held
    file. A PR merged/closed out-of-band (desktop merge_reviewed_pr.sh,
    auto-merge, manual close) leaves its held entry lingering — nothing removes
    it, because the suppress-dispatch self-heal only runs when a Mirror review is
    attempted, which never happens for a closed PR. Its approval then sits stale
    on the tab (the #823/#830/#833/#904 backlog). The reconcile prunes it.
    """

    def test_merged_out_of_band_prunes_lingering_entry_and_approves(self):
        self.gh.pr_labels[(REPO, 823)] = ['deep-review-passed']
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(len(self._pending()), 1)          # surfaced
        # PR merges out-of-band; its held entry STILL lingers in the file.
        self.gh.pr_state[(REPO, 823)] = 'MERGED'
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self.on._load_deep_review_held(), [])   # entry pruned
        self.assertEqual(self._pending(), [])                    # left the tab
        hist = [e for e in self._history()
                if e['id'] == 'deep-review-hold-pr823-aaaaaaaa']
        self.assertEqual(hist[0]['status'], 'approved')          # stamped+merged

    def test_closed_out_of_band_prunes_and_expires(self):
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(len(self._pending()), 1)
        self.gh.pr_state[(REPO, 823)] = 'CLOSED'           # closed, not merged
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self.on._load_deep_review_held(), [])
        self.assertEqual(self._pending(), [])
        hist = [e for e in self._history()
                if e['id'] == 'deep-review-hold-pr823-aaaaaaaa']
        self.assertEqual(hist[0]['status'], 'expired')

    def test_unknown_state_keeps_entry_held_failopen(self):
        # A gh hiccup / backoff (state undeterminable → None) must NOT drop a
        # legit hold: the entry stays and the approval stays surfaced.
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        self.gh.pr_state[(REPO, 823)] = 'SOMETHING_WEIRD'  # -> None -> fail-open
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(len(self.on._load_deep_review_held()), 1)   # kept
        self.assertEqual(len(self._pending()), 1)                    # still on tab

    def test_open_pr_is_not_pruned(self):
        # The common path: an OPEN held PR is untouched by the self-heal.
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()
        self.gh.pr_state[(REPO, 823)] = 'OPEN'
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(len(self.on._load_deep_review_held()), 1)
        self.assertEqual(len(self._pending()), 1)


class MergeDriveTest(_SurfaceTestBase):
    """After a dashboard APPROVE stamps `deep-review-passed`, the reconcile must
    ACTUALLY drive the merge — stamping alone left the PR OPEN forever because
    `held_deep_review` is by-design NOT queued / NOT retried and the queue sweep
    only retries QUEUE entries (deep-review-stamp-triggers-automerge-001).
    """

    def _surface_and_approve(self, pr_n=823, head='aaaaaaaa11112222'):
        self._seed_held(self._held_entry(pr_n, head))
        self.on._reconcile_deep_review_held_approvals()
        approval_id = f'deep-review-hold-pr{pr_n}-{head[:8]}'
        self.approval.resolve(approval_id, 'approved', note='larry approved')

    def test_approved_clean_pr_stamps_then_merges_and_clears(self):
        # (1) approved + CLEAN/MERGEABLE + stampable → stamp AND invoke the merge
        # gate (stub returns 'merged'); the held entry clears.
        self.merge_outcome = 'merged'
        self._surface_and_approve()
        self.on._reconcile_deep_review_held_approvals()
        # Stamped.
        self.assertIn('deep-review-passed', self.gh.pr_labels[(REPO, 823)])
        # Drove the real merge chokepoint exactly once, for this PR.
        self.assertEqual(len(self.merge_calls), 1)
        self.assertEqual(self.merge_calls[0]['pr_number'], 823)
        self.assertEqual(self.merge_calls[0]['repo'], REPO)
        # Held entry cleared on the 'merged' outcome.
        self.assertEqual(self.on._load_deep_review_held(), [])

    def test_merged_pr_fires_no_second_gh_merge(self):
        # (2) idempotency: once merged (PR no longer open / held entry gone), a
        # subsequent reconcile tick fires NO second merge.
        self.merge_outcome = 'merged'
        self._surface_and_approve()
        self.on._reconcile_deep_review_held_approvals()   # merges, clears entry
        self.gh.pr_state[(REPO, 823)] = 'MERGED'          # realistic: PR closed
        self.on._reconcile_deep_review_held_approvals()   # must NOT re-merge
        self.assertEqual(len(self.merge_calls), 1)

    def test_overlap_blocker_enqueues_once_no_per_tick_redrive(self):
        # (3) overlap blocker → outcome 'held_for_blocker' → entry now owned by
        # the auto-merge queue; the reconcile does NOT re-drive the merge every
        # tick (only on the throttled state-recheck cadence).
        self.merge_outcome = 'held_for_blocker'
        # Production-like throttle: first tick rechecks, the next does not.
        self.on._DEEP_REVIEW_HELD_STATE_RECHECK_INTERVAL_S = 600
        self.on._last_deep_review_held_state_recheck_ts = 0.0
        self._surface_and_approve()
        self.on._reconcile_deep_review_held_approvals()   # recheck tick → drives
        self.on._reconcile_deep_review_held_approvals()   # throttled → no re-drive
        self.assertEqual(len(self.merge_calls), 1)
        # Held entry stays (queue sweep owns the retry; nothing cleared it here).
        self.assertEqual(len(self.on._load_deep_review_held()), 1)

    def test_pending_approval_never_drives_a_merge(self):
        # (4) approval still 'pending' → only surfacing happens; NO stamp, NO
        # merge attempt.
        self._seed_held(self._held_entry(823, 'aaaaaaaa11112222'))
        self.on._reconcile_deep_review_held_approvals()   # surfaces (pending)
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self.gh.edit_calls, [])
        self.assertEqual(self.merge_calls, [])

    def test_label_stamp_failure_alerts_and_drives_no_merge(self):
        # (5) label-stamp gh failure → the existing repair-failure alert fires and
        # NO merge is attempted (never merge onto a still-gated PR).
        self.gh.edit_fail = True
        self._surface_and_approve()
        self.on._reconcile_deep_review_held_approvals()
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('deep-review-passed', alerts[0]['message'])
        self.assertEqual(self.merge_calls, [])


class HeadMovedSinceHoldTest(_SurfaceTestBase):
    """Larry approves a SPECIFIC head. The held entry's `head_sha` is persisted,
    not live, so a non-terminal first drive (held_for_blocker) that re-drives on
    the throttled cadence could otherwise stamp + merge a commit that landed
    after the approval — never deep-reviewed, never approved.
    """

    def _surface_and_approve(self, pr_n=823, head='aaaaaaaa11112222'):
        self._seed_held(self._held_entry(pr_n, head))
        self.on._reconcile_deep_review_held_approvals()
        self.approval.resolve(
            f'deep-review-hold-pr{pr_n}-{head[:8]}', 'approved', note='ok')

    def test_head_moved_since_hold_drives_no_merge(self):
        # New commit lands after the approval → NO merge, and the hold clears so
        # Mirror re-reviews + re-holds at the new head.
        self._surface_and_approve()
        self.gh.pr_heads[(REPO, 823)] = 'ffffffff99998888'
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self.merge_calls, [])
        self.assertEqual(self.on._load_deep_review_held(), [])

    def test_head_moved_after_stamp_revokes_the_stale_stamp(self):
        # Non-terminal first drive (held_for_blocker) leaves the entry stamped
        # and in place; a push in that window must revoke OUR stamp, else the
        # label outlives it and the normal Mirror path merges the un-reviewed
        # head.
        self.merge_outcome = 'held_for_blocker'
        self._surface_and_approve()
        self.on._reconcile_deep_review_held_approvals()   # stamps + drives once
        self.assertEqual(len(self.merge_calls), 1)
        self.assertIn('deep-review-passed', self.gh.pr_labels[(REPO, 823)])
        self.gh.pr_heads[(REPO, 823)] = 'ffffffff99998888'
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(len(self.merge_calls), 1)          # no second drive
        self.assertEqual(
            self.gh.remove_label_calls, [(REPO, 823, 'deep-review-passed')])
        self.assertNotIn('deep-review-passed', self.gh.pr_labels[(REPO, 823)])
        self.assertEqual(self.on._load_deep_review_held(), [])

    def test_head_moved_without_our_stamp_leaves_labels_alone(self):
        # We only ever revoke a stamp WE applied for the stale head — a stamp
        # from a human `/code-review high` at the new head must survive.
        self._surface_and_approve()
        self.gh.pr_labels[(REPO, 823)] = ['deep-review-passed']
        self.gh.pr_heads[(REPO, 823)] = 'ffffffff99998888'
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self.gh.remove_label_calls, [])
        self.assertEqual(self.merge_calls, [])

    def test_hold_recorded_with_no_head_never_revokes_a_stamp(self):
        # An entry held when gh couldn't resolve the head records head_sha=None,
        # and an entry we never stamped has stamped_head_sha=None. Those two
        # Nones must not compare equal into a revoke of someone else's stamp.
        self._seed_held(self._held_entry(823, None))
        self.on._reconcile_deep_review_held_approvals()
        self.approval.resolve('deep-review-hold-pr823', 'approved', note='ok')
        self.gh.pr_labels[(REPO, 823)] = ['deep-review-passed']
        self.gh.pr_heads[(REPO, 823)] = 'ffffffff99998888'
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self.gh.remove_label_calls, [])
        self.assertEqual(self.merge_calls, [])

    def test_unresolvable_head_defers_the_merge(self):
        # gh can't resolve the head → cannot prove it is the approved commit.
        # Fail CLOSED: no stamp, no merge, hold preserved for the next tick.
        self._surface_and_approve()
        self.gh.pr_heads[(REPO, 823)] = None
        self.on._reconcile_deep_review_held_approvals()
        self.assertEqual(self.merge_calls, [])
        self.assertEqual(self.gh.edit_calls, [])
        self.assertEqual(len(self.on._load_deep_review_held()), 1)


if __name__ == '__main__':
    unittest.main()
