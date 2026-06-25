#!/usr/bin/env python3
"""Dispatch-time half of the merged/closed-PR Mirror-review guard.

`_dispatch_mirror_review` (first review) and `_dispatch_mirror_review_rerun`
(re-review) must not even queue a Mirror review for a PR that has already left
OPEN — the review gates nothing. This is the cheap early skip; the
execution-time guard in inbox_watcher catches the post-dispatch merge race.

Both guards FAIL OPEN: an undeterminable state (gh hiccup -> `_gh_pr_is_open`
returns None) proceeds with the dispatch so a transient gh error never silently
drops a legitimate review. Only a positively observed MERGED/CLOSED skips.

Covers success criteria 1 (skip terminal) + 4 (idempotent + fail-open).

Run:
    cd /home/larry/agent-core && python3 -m unittest \
        scripts.tests.test_outbox_notifier_review_terminal_guard
"""

from __future__ import annotations

try:
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import importlib
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

import outbox_notifier as on  # noqa: E402
import safe_write_inbox as swi  # noqa: E402

try:
    from . import _chokepoint_optout
except ImportError:
    import _chokepoint_optout

_PR_URL = 'https://github.com/x/y/pull/1'

_AGENTS_ROOT_BACKUP = None
_AGENTS_ROOT_TMPDIR = None
_LIVE_EMIT_BACKUP = None
_CHOKEPOINT_SAVED_SENTINEL = None


def setUpModule():  # noqa: N802 — unittest hook name
    # Drive the inbox-write chokepoint against a rerouted tmpdir tree; opt out
    # of the Layer B guards so dispatch writes reach the (sandboxed) inbox
    # instead of tripping the test-isolation breach. Mirrors test_outbox_notifier.
    global _AGENTS_ROOT_BACKUP, _AGENTS_ROOT_TMPDIR, _LIVE_EMIT_BACKUP
    global _CHOKEPOINT_SAVED_SENTINEL
    _CHOKEPOINT_SAVED_SENTINEL = _chokepoint_optout.disengage_guards()
    _AGENTS_ROOT_BACKUP = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    _AGENTS_ROOT_TMPDIR = tempfile.mkdtemp(prefix='review-terminal-guard-mod-')
    os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_TMPDIR
    # Review dispatches push-emit a chain event; force the kill-switch so a
    # shell with SUPABASE_* sourced never upserts fixture rows.
    _LIVE_EMIT_BACKUP = os.environ.get('OURLIBERTY_DISABLE_LIVE_EMIT')
    os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = '1'
    for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
        Path(_AGENTS_ROOT_TMPDIR, sub).mkdir(exist_ok=True)
    importlib.reload(swi)
    importlib.reload(on)


def tearDownModule():  # noqa: N802 — unittest hook name
    _chokepoint_optout.reengage_guards(_CHOKEPOINT_SAVED_SENTINEL)
    if _AGENTS_ROOT_BACKUP is None:
        os.environ.pop('OURLIBERTY_AGENTS_ROOT', None)
    else:
        os.environ['OURLIBERTY_AGENTS_ROOT'] = _AGENTS_ROOT_BACKUP
    if _LIVE_EMIT_BACKUP is None:
        os.environ.pop('OURLIBERTY_DISABLE_LIVE_EMIT', None)
    else:
        os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = _LIVE_EMIT_BACKUP
    if _AGENTS_ROOT_TMPDIR:
        shutil.rmtree(_AGENTS_ROOT_TMPDIR, ignore_errors=True)
    importlib.reload(swi)
    importlib.reload(on)


class _ReviewDispatchSandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._originals = {}
        for name in [
            'AGENTS_ROOT', 'INBOXES_ROOT', 'OUTBOXES_ROOT', 'BLACKBOARD',
            'LOG_FILE', 'DEAD_LETTER_STATE', 'EMERGENCY_HALT_FLAG',
        ]:
            self._originals[name] = getattr(on, name)
        on.AGENTS_ROOT = self._root
        on.INBOXES_ROOT = self._root / 'inboxes'
        on.OUTBOXES_ROOT = self._root / 'outboxes'
        on.BLACKBOARD = self._root / 'blackboard'
        on.LOG_FILE = self._root / 'logs' / 'outbox-notifier.log'
        on.DEAD_LETTER_STATE = self._root / 'state' / 'dead-letter.json'
        on.EMERGENCY_HALT_FLAG = on.BLACKBOARD / 'EMERGENCY_HALT'
        self._swi_originals = {
            'AGENTS_ROOT': swi.AGENTS_ROOT,
            'INBOXES_ROOT': swi.INBOXES_ROOT,
        }
        swi.AGENTS_ROOT = self._root
        swi.INBOXES_ROOT = self._root / 'inboxes'
        on.ensure_dirs()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(on, name, value)
        for name, value in self._swi_originals.items():
            setattr(swi, name, value)
        self._tmp.cleanup()

    def _reviews(self):
        return list((on.INBOXES_ROOT / 'mirror').glob('review-*.json'))


class FirstReviewDispatchTerminalGuardTest(_ReviewDispatchSandbox):
    def _data(self):
        return {
            'task_id': 'real-task',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-task',
            # head_sha present -> _dispatch skips its own gh head lookup, so the
            # only gh-touching call left is our guard (which we patch).
            'head_sha': 'abc123',
        }

    def test_merged_pr_skips_dispatch(self):
        with mock.patch.object(on, '_gh_pr_is_open', return_value=False) as g:
            on._dispatch_mirror_review(self._data(), _PR_URL)
        g.assert_called_once()
        self.assertEqual(self._reviews(), [])

    def test_open_pr_dispatches(self):
        with mock.patch.object(on, '_gh_pr_is_open', return_value=True):
            on._dispatch_mirror_review(self._data(), _PR_URL)
        self.assertEqual(len(self._reviews()), 1)

    def test_unknown_state_fails_open_and_dispatches(self):
        # gh hiccup -> None -> proceed (never drop a legitimate review).
        with mock.patch.object(on, '_gh_pr_is_open', return_value=None):
            on._dispatch_mirror_review(self._data(), _PR_URL)
        self.assertEqual(len(self._reviews()), 1)


class RereviewDispatchTerminalGuardTest(_ReviewDispatchSandbox):
    def _data(self):
        return {
            'task_id': 'real-task',
            'target_repo': 'ourliberty-agent-core',
            'branch': 'forge/real-task',
            'pr_url': _PR_URL,
            'max_revisions': 3,
        }

    def test_merged_pr_skips_rereview(self):
        with mock.patch.object(on, '_gh_pr_is_open', return_value=False) as g:
            on._dispatch_mirror_review_rerun(self._data(), 1, 'fixed it')
        g.assert_called_once()
        self.assertEqual(self._reviews(), [])

    def test_open_pr_dispatches_rereview(self):
        with mock.patch.object(on, '_gh_pr_is_open', return_value=True):
            on._dispatch_mirror_review_rerun(self._data(), 1, 'fixed it')
        self.assertEqual(len(self._reviews()), 1)

    def test_unknown_state_fails_open_and_dispatches_rereview(self):
        with mock.patch.object(on, '_gh_pr_is_open', return_value=None):
            on._dispatch_mirror_review_rerun(self._data(), 1, 'fixed it')
        self.assertEqual(len(self._reviews()), 1)


class TargetIsTerminalHelperTest(unittest.TestCase):
    """`_mirror_review_target_is_terminal` fail-open contract, in isolation."""

    def test_missing_and_unparseable_url_return_false(self):
        self.assertFalse(on._mirror_review_target_is_terminal(None))
        self.assertFalse(on._mirror_review_target_is_terminal(''))
        self.assertFalse(on._mirror_review_target_is_terminal('not-a-url'))

    def test_terminal_only_on_definite_not_open(self):
        with mock.patch.object(on, '_gh_pr_is_open', return_value=False):
            self.assertTrue(on._mirror_review_target_is_terminal(_PR_URL))
        with mock.patch.object(on, '_gh_pr_is_open', return_value=True):
            self.assertFalse(on._mirror_review_target_is_terminal(_PR_URL))
        with mock.patch.object(on, '_gh_pr_is_open', return_value=None):
            self.assertFalse(on._mirror_review_target_is_terminal(_PR_URL))


if __name__ == "__main__":
    unittest.main()
