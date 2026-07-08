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
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import outbox_notifier as ob  # noqa: E402
import safe_write_inbox  # noqa: E402


class _DedupBase(unittest.TestCase):
    LOST_SUB = f'.archive/{safe_write_inbox.LOST_RESULT_SUBDIR}'

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

    @staticmethod
    def _age(path, seconds):
        """Backdate `path`'s mtime by `seconds`."""
        old = time.time() - seconds
        os.utime(path, (old, old))


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

    # --- revision-round records (the PR #865 triple-dispatch, 2026-07-08) ---
    def test_rev_round_matching_head_blocks(self):
        # A re-review round is filed as review-t-rev<N>.json — a name the
        # exact + `.<i>` scans never see. When it covered the current head it
        # must count as dispatched, else the undispatched-PR backstop
        # re-reviews an already-verdicted head.
        self._write('.archive', 'review-t-rev1.json', head_sha='samehead')
        self.assertTrue(self._check('samehead'))

    def test_rev_round_old_head_does_not_block(self):
        self._write('.archive', 'review-t-rev1.json', head_sha='oldhead')
        self.assertFalse(self._check('newhead'))

    def test_rev_round_without_head_does_not_block(self):
        # Legacy rerun records carried no head at all (the exact #865 record).
        self._write('.archive', 'review-t-rev1.json', head_sha='__omit__')
        self.assertFalse(self._check('anyhead'))

    def test_replan_round_matching_head_blocks(self):
        self._write('.archive', 'review-t-replan2-rev1.json', head_sha='h9')
        self.assertTrue(self._check('h9'))
        self._write('.archive', 'review-t-replan3.json', head_sha='h10')
        self.assertTrue(self._check('h10'))

    def test_uniquified_rev_round_matched(self):
        self._write('.archive', 'review-t-rev1.2.json', head_sha='h9')
        self.assertTrue(self._check('h9'))

    def test_rev_like_sibling_name_not_matched(self):
        # `review-t-revamp.json` / `review-t-rev1-extra.json` are sibling task
        # names, not task t's rounds — must not satisfy t's dedup.
        self._write('.archive', 'review-t-revamp.json', head_sha='h9')
        self._write('.archive', 'review-t-rev1-extra.json', head_sha='h9')
        self.assertFalse(self._check('h9'))

    def test_none_head_ignores_rev_rounds(self):
        # Existence-only callers keep their exact prior contract.
        self._write('.archive', 'review-t-rev1.json', head_sha='x')
        self.assertFalse(self._check(None))

    def test_live_rev_round_matching_head_blocks(self):
        # A rev-round review queued LIVE in the inbox (recording the current
        # head) means a review of THIS head is already pending — an inline
        # re-process must not queue a duplicate round-0 review alongside it
        # (the live-inbox leg was previously exact-name-only).
        self._write('', 'review-t-rev1.json', head_sha='H2')
        self.assertTrue(self._check('H2'))

    def test_live_rev_round_other_head_does_not_block(self):
        self._write('', 'review-t-rev1.json', head_sha='H2')
        self.assertFalse(self._check('H3'))

    def test_glob_metacharacter_task_id_finds_own_variant(self):
        # A task_id with glob metacharacters (validator-permitted) must not turn
        # the variant scan into a character class — else its own uniquified
        # same-head archive is missed and the head is re-dispatched every tick.
        check = ob._review_request_already_dispatched
        self._write('.archive', 'review-cpu[high].json', head_sha='head-A')
        self._write('.archive', 'review-cpu[high].1.json', head_sha='head-B')
        self.assertTrue(check('review-cpu[high].json', 'head-B'))  # variant hit
        self.assertTrue(check('review-cpu[high].json', 'head-A'))  # exact hit
        self.assertFalse(check('review-cpu[high].json', 'head-C'))  # re-review


class TestDiedVerdictlessRedispatch(_DedupBase):
    """died-verdictless-review-redispatch (post-#850).

    Pre-#850, a review session pushed a [WIP][session-start] commit at pickup,
    so a session that died verdict-less moved the PR head and the head-aware
    dedup re-dispatched it by accident. Post-#850 the head never moves; the
    deliberate replacement is inbox_watcher's POSITIVE lost-result marker
    (`.archive/.lost-result/`, written when a run's outbox could not be
    persisted). A same-head lost-result envelope must NOT dedup — after the
    grace debounce and under the attempts cap — while a PLAIN archived
    same-head envelope keeps deduping forever (fail closed).
    """

    STALE = ob.REVIEW_REDISPATCH_GRACE_SECONDS + 600

    def _check(self, head='h1'):
        return ob._review_request_already_dispatched('review-t.json', head)

    def _lost(self, name='review-t.json', head='h1', age=None):
        p = self._write(self.LOST_SUB, name, head_sha=head)
        self._age(p, self.STALE if age is None else age)
        return p

    # --- the fix: same-head lost-result marker → re-dispatchable ---
    def test_lost_result_past_grace_redispatches(self):
        self._lost()
        self.assertFalse(self._check())

    def test_lost_result_within_grace_debounces(self):
        self._lost(age=60)  # failed round dispatched a minute ago
        self.assertTrue(self._check())

    def test_lost_result_other_head_does_not_redispatch_this_head(self):
        # A lost round of an OLDER head neither dedups nor re-dispatches the
        # current head — absent any current-head record the answer is simply
        # "never dispatched" (False), same as before this change.
        self._lost(head='old')
        self.assertFalse(self._check('new'))

    def test_uniquified_lost_variants_counted(self):
        # move_to() uniquifies repeat losses: review-t.json, review-t.1.json.
        self._lost()
        self._lost(name='review-t.1.json')
        self.assertFalse(self._check())  # 2 < cap, past grace → re-dispatch

    # --- the cap: repeated lost rounds return to permanent dedup ---
    def test_lost_rounds_at_cap_dedup_permanently(self):
        for i in range(ob.REVIEW_REDISPATCH_MAX_ATTEMPTS):
            self._lost(name=f'review-t.{i}.json' if i else 'review-t.json')
        self.assertTrue(self._check())

    def test_cap_counts_only_matching_head(self):
        # Two lost rounds of an old head + one of the current head: the
        # current head has 1 lost round (< cap) → still re-dispatchable.
        self._lost(name='review-t.json', head='old')
        self._lost(name='review-t.1.json', head='old')
        self._lost(name='review-t.2.json', head='h1')
        self.assertFalse(self._check('h1'))

    # --- fail-closed semantics preserved everywhere else ---
    def test_plain_archived_same_head_dedups_forever(self):
        # No lost-result marker → a concluded review dedups regardless of age
        # (the pre-fix guarantee; naming drift / retention can't cause loops).
        p = self._write('.archive', 'review-t.json', head_sha='h1')
        self._age(p, self.STALE * 10)
        self.assertTrue(self._check())

    def test_archived_envelope_wins_over_lost_marker(self):
        # A later round CONCLUDED (plain archive) → its dedup outranks an
        # earlier round's lost marker for the same head.
        self._lost()
        p = self._write('.archive', 'review-t.1.json', head_sha='h1')
        self._age(p, self.STALE)
        self.assertTrue(self._check())

    def test_invalid_leg_still_blocks(self):
        # Validator rejection is deterministic; re-dispatch would re-reject.
        p = self._write('.invalid', 'review-t.json', head_sha='h1')
        self._age(p, self.STALE)
        self.assertTrue(self._check())

    def test_live_inbox_blocks_regardless_of_lost_marker(self):
        self._lost()
        self._write('', 'review-t.json', head_sha='h1')
        self.assertTrue(self._check())

    def test_none_head_existence_only_ignores_lost_marker(self):
        # Existence-only callers (reconcile sweep, replan rounds) keep their
        # exact prior contract: the lost-result subdir is invisible to them.
        self._lost()
        self.assertFalse(
            ob._review_request_already_dispatched('review-t.json', None))

    def test_lost_sibling_task_not_matched(self):
        # review-t-extra.json in .lost-result must not re-dispatch task t...
        self._lost(name='review-t-extra.json', head='h1')
        self.assertFalse(self._check('h1'))
        # ...nor be miscounted toward t's cap (name grammar excludes it).
        for i in range(1, ob.REVIEW_REDISPATCH_MAX_ATTEMPTS + 1):
            self._lost(name=f'review-t-extra.{i}.json', head='h1')
        self.assertFalse(self._check('h1'))

    # --- lost RE-REVIEW rounds are paced too (PR #865 companion, 2026-07-08) ---
    def test_lost_rev_round_within_grace_debounces(self):
        # A died-verdictless RE-REVIEW (`review-t-rev1.json`) recording head h1
        # must be paced by the same debounce as round-0 — the round names were
        # taught to the archive scan but the lost-result scan originally globbed
        # only `review-t[.<i>].json`, so a lost rerun re-dispatched with NO
        # debounce and never counted toward the cap.
        self._lost(name='review-t-rev1.json', head='h1', age=60)
        self.assertTrue(self._check('h1'))

    def test_lost_rev_round_past_grace_redispatches(self):
        self._lost(name='review-t-rev1.json', head='h1')  # STALE (past grace)
        self.assertFalse(self._check('h1'))

    def test_lost_rev_round_counts_toward_cap(self):
        # base + rev1 + replan1-rev1 all lost at h1 == MAX_ATTEMPTS → permanent.
        assert ob.REVIEW_REDISPATCH_MAX_ATTEMPTS == 3
        self._lost(name='review-t.json', head='h1')
        self._lost(name='review-t-rev1.json', head='h1')
        self._lost(name='review-t-replan1-rev1.json', head='h1')
        self.assertTrue(self._check('h1'))  # at cap → dedup permanently


class TestReviewRecordNameRe(_DedupBase):
    """The shared review-record name grammar — one source of truth for the
    head-aware dedup (all legs) and the heal-undispatched-pr-review recency
    scan, so they cannot disagree about which records exist (the #865 drift)."""

    def _matches(self, stem, name):
        return bool(ob.review_record_name_re(stem).fullmatch(name))

    def test_matches_all_round_shapes(self):
        for name in (
            'review-t.json', 'review-t.2.json',
            'review-t-rev1.json', 'review-t-rev1.3.json',
            'review-t-replan2.json', 'review-t-replan2-rev1.json',
            'review-t-replan2-rev1.4.json',
        ):
            self.assertTrue(self._matches('review-t', name), name)

    def test_rejects_sibling_and_malformed(self):
        for name in (
            'review-t-extra.json',      # sibling task
            'review-t-revamp.json',     # not a rev round
            'review-textra.json',       # different stem
            'review-t-rev.json',        # rev without digits
            'review-t.json.bak',        # wrong suffix
        ):
            self.assertFalse(self._matches('review-t', name), name)

    def test_metacharacter_stem_matches_only_own(self):
        self.assertTrue(self._matches('review-cpu[hi]', 'review-cpu[hi]-rev1.json'))
        self.assertFalse(self._matches('review-cpu[hi]', 'review-cpuh-rev1.json'))


if __name__ == '__main__':
    unittest.main()
