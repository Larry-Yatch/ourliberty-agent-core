"""Tests for the head-aware review-request dedup in outbox_notifier.

Covers `_recorded_review_head_sha` and the `current_head_sha` branch of
`_review_request_already_dispatched`: an ARCHIVED review of an OLDER head must
not block re-review of new commits (the head-drift fix), while a same-head, a
live in-flight, or a `move_to()`-uniquified archive of the current head still
dedups (no storm). `current_head_sha=None` must preserve the exact prior
existence-only behavior used by the reconcile sweep.

unittest (repo convention; pytest isn't installed on the droplet).
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import outbox_notifier as ob  # noqa: E402
import safe_write_inbox  # noqa: E402


class _DedupBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'mirror' / '.archive').mkdir(parents=True)
        (self.root / 'mirror' / '.invalid').mkdir(parents=True)
        self._orig_root = safe_write_inbox.INBOXES_ROOT
        safe_write_inbox.INBOXES_ROOT = self.root
        self.addCleanup(self._restore)

    def _restore(self):
        safe_write_inbox.INBOXES_ROOT = self._orig_root
        self._tmp.cleanup()

    def _write(self, sub, name, head_sha='__omit__', raw=None):
        d = self.root / 'mirror' / sub if sub else self.root / 'mirror'
        d.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            (d / name).write_text(raw)
            return d / name
        body = {'task_id': 't'}
        if head_sha != '__omit__':
            body['head_sha'] = head_sha
        (d / name).write_text(json.dumps(body))
        return d / name


class TestRecordedReviewHeadSha(_DedupBase):
    def test_top_level(self):
        p = self._write('.archive', 'review-t.json', head_sha='abc123')
        self.assertEqual(ob._recorded_review_head_sha(p), 'abc123')

    def test_nested_context(self):
        p = self._write('.archive', 'review-t.json',
                        raw=json.dumps({'context': {'head_sha': 'nested9'}}))
        self.assertEqual(ob._recorded_review_head_sha(p), 'nested9')

    def test_absent_unreadable_or_nonstring(self):
        p = self._write('.archive', 'noheader.json', head_sha='__omit__')
        self.assertIsNone(ob._recorded_review_head_sha(p))
        self.assertIsNone(
            ob._recorded_review_head_sha(self.root / 'mirror' / 'missing.json'))
        bad = self._write('.archive', 'bad.json', raw='not json')
        self.assertIsNone(ob._recorded_review_head_sha(bad))


class TestHeadAwareDedup(_DedupBase):
    def _check(self, head=None):
        return ob._review_request_already_dispatched('review-t.json', head)

    # --- back-compat: current_head_sha=None → existence-only (unchanged) ---
    def test_none_head_existence_archive(self):
        self._write('.archive', 'review-t.json', head_sha='x')
        self.assertTrue(self._check(None))

    def test_none_head_absent_is_false(self):
        self.assertFalse(self._check(None))

    def test_none_head_invalid_leg(self):
        self._write('.invalid', 'review-t.json', head_sha='x')
        self.assertTrue(self._check(None))

    # --- head-aware ---
    def test_live_blocks_regardless_of_head(self):
        # Mirror is actively reviewing (live inbox file) — never pile on, even
        # though the recorded head differs from the current head.
        self._write('', 'review-t.json', head_sha='oldhead')
        self.assertTrue(self._check('newhead'))

    def test_archived_old_head_does_not_block(self):
        self._write('.archive', 'review-t.json', head_sha='oldhead')
        self.assertFalse(self._check('newhead'))  # the head-drift fix

    def test_archived_matching_head_blocks(self):
        self._write('.archive', 'review-t.json', head_sha='samehead')
        self.assertTrue(self._check('samehead'))

    def test_uniquified_variant_matched_no_storm(self):
        # head-A archived as review-t.json; head-B as the move_to()-uniquified
        # review-t.1.json. Current head-B must be recognised in the variant.
        self._write('.archive', 'review-t.json', head_sha='head-A')
        self._write('.archive', 'review-t.1.json', head_sha='head-B')
        self.assertTrue(self._check('head-B'))
        self.assertTrue(self._check('head-A'))
        self.assertFalse(self._check('head-C'))  # never reviewed → re-review

    def test_legacy_archive_without_head_does_not_block(self):
        # A pre-fix review recorded no head_sha → must not mask a needed review.
        self._write('.archive', 'review-t.json', head_sha='__omit__')
        self.assertFalse(self._check('anyhead'))

    def test_invalid_leg_matching_head_blocks(self):
        self._write('.invalid', 'review-t.json', head_sha='inv1')
        self.assertTrue(self._check('inv1'))

    def test_no_prefix_collision_with_sibling_task(self):
        # A different task whose id starts with this one must not be mistaken
        # for a head match of task 't'.
        ob_check = ob._review_request_already_dispatched
        self._write('.archive', 'review-t-extra.json', head_sha='zzz')
        self.assertFalse(ob_check('review-t.json', 'zzz'))


if __name__ == '__main__':
    unittest.main()
