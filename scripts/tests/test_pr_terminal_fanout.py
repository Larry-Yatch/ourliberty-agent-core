"""Unit tests for the terminal-event fan-out sentinel (completeness PR-3).

Covers acceptance § 10 items 1-11: the five § 3 guards each with a § 10.2
unreachability proof, partial-failure fan-out completion (§ 10.3), the
decision-resolve legs (§ 10.4), single-writer ordering (§ 10.5), the UNKNOWN
ledger + probe-error storm (§ 10.6), parity true-miss vs lost-race (§ 10.7),
R1 wedged-committer (§ 10.8), R2 retire-vs-ship (§ 10.9), report-only +
self-firing arming (§ 10.10), and the unittest/sentinel-armed/zero-live-writes
harness contract (§ 10.11). All gh/Supabase/alert I/O is injected — no network,
no live-tree writes.
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pr_terminal_fanout as ptf  # noqa: E402
import task_terminal_state as tts  # noqa: E402
import heal_droplet_git_drift as hdg  # noqa: E402
import heal_missions_card_gc as gc  # noqa: E402

_GH = 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/'
_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _item(task_id='t-1', pr_num=42, since=None, store='inreview', entry_id=None):
    since = since if since is not None else _iso(_NOW - timedelta(hours=6))
    url = _GH + str(pr_num)
    return {'item_key': f'{store}:{task_id}', 'store': store, 'task_id': task_id,
            'pr_url': url, 'since': since, 'entry_id': entry_id}


def _pr(number=42, state='OPEN', head_ref='forge/t-1', head_oid='sha-1',
        merged_at=None, closed_at=None, merge_oid=None):
    return {'number': number, 'state': state, 'headRefName': head_ref,
            'headRefOid': head_oid, 'mergedAt': merged_at, 'closedAt': closed_at,
            'mergeCommit': {'oid': merge_oid} if merge_oid else None,
            'url': _GH + str(number)}


def _yes_ancestor(_sha):
    return True


def _no_liveness(_tid):
    return False


# ====================================================================
# § 10.1 / grade — identity-grade gate (search-grade fan-out FORBIDDEN)
# ====================================================================
class GradeEvidenceTest(unittest.TestCase):
    def test_identity_requires_exact_forge_branch(self):
        item = _item(task_id='t-1', pr_num=42)
        prs = [_pr(number=42, head_ref='forge/t-1')]
        self.assertEqual(ptf.grade_evidence(item, prs), 'identity')

    def test_branch_mismatch_is_search_grade(self):
        item = _item(task_id='t-1', pr_num=42)
        prs = [_pr(number=42, head_ref='feature/somethingelse')]
        self.assertEqual(ptf.grade_evidence(item, prs), 'search')

    def test_missing_pr_url_is_search_grade(self):
        item = _item()
        item['pr_url'] = ''
        self.assertEqual(ptf.grade_evidence(item, [_pr()]), 'search')

    def test_search_grade_terminal_never_fans_out(self):
        # § 3: a search-grade coordinate must NOT launder into fan-out authority.
        item = _item(task_id='t-1', pr_num=42)
        prs = [_pr(number=42, state='MERGED', head_ref='feature/x',
                   merged_at=_iso(_NOW))]
        d = ptf.evaluate_item(item, prs, now=_NOW, is_ancestor_fn=_yes_ancestor,
                              liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'keep')
        self.assertEqual(d.grade, 'search')


# ====================================================================
# § 10.2 — the FIVE guards, each with a fan-out case AND an
# unreachability (guard-blocks) case.
# ====================================================================
class GuardsTest(unittest.TestCase):
    def test_merged_happy_path_fans_out(self):
        item = _item()
        prs = [_pr(state='MERGED', merged_at=_iso(_NOW))]
        d = ptf.evaluate_item(item, prs, now=_NOW, is_ancestor_fn=_yes_ancestor,
                              liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'fanout')
        self.assertEqual(d.terminal, tts.MERGED)
        self.assertTrue(d.ancestry_verified)

    def test_squash_merge_uses_merge_commit_ancestry(self):
        # § 3.4: for a squash/rebase merge (system default) the branch tip
        # (headRefOid) is NOT an ancestor of origin/main — only mergeCommit.oid
        # is. Ancestry must verify off the merge commit, else the real MERGED
        # path never fans out.
        item = _item()
        prs = [_pr(state='MERGED', merged_at=_iso(_NOW),
                   head_oid='branch-tip', merge_oid='merge-sha')]
        d = ptf.evaluate_item(
            item, prs, now=_NOW,
            is_ancestor_fn=lambda sha: sha == 'merge-sha',
            liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'fanout')
        self.assertEqual(d.terminal, tts.MERGED)
        self.assertTrue(d.ancestry_verified)

    # Guard 1 — OPEN beats terminal over the full PR set.
    def test_guard1_open_pr_blocks_fanout(self):
        item = _item()
        prs = [_pr(number=42, state='MERGED', merged_at=_iso(_NOW)),
               _pr(number=43, state='OPEN', head_ref='forge/t-1')]
        d = ptf.evaluate_item(item, prs, now=_NOW, is_ancestor_fn=_yes_ancestor,
                              liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'keep')
        self.assertIn('OPEN', d.reason)

    # Guard 2 — temporal anchor: terminal must be strictly after item activity.
    def test_guard2_terminal_predating_activity_blocks(self):
        item = _item(since=_iso(_NOW))
        prs = [_pr(state='MERGED', merged_at=_iso(_NOW - timedelta(hours=1)))]
        d = ptf.evaluate_item(item, prs, now=_NOW, is_ancestor_fn=_yes_ancestor,
                              liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'keep')
        self.assertIn('not after', d.reason)

    # Guard 3 — liveness: an in-flight claim means work is being re-run.
    def test_guard3_live_in_flight_blocks(self):
        item = _item()
        prs = [_pr(state='MERGED', merged_at=_iso(_NOW))]
        d = ptf.evaluate_item(item, prs, now=_NOW, is_ancestor_fn=_yes_ancestor,
                              liveness_fn=lambda _tid: True)
        self.assertEqual(d.action, 'keep')
        self.assertIn('in-flight', d.reason)

    # Guard 4 — MERGED ancestry unverified ⇒ UNKNOWN, never fan-out.
    def test_guard4_merged_without_ancestry_is_unknown(self):
        item = _item()
        prs = [_pr(state='MERGED', merged_at=_iso(_NOW))]
        d = ptf.evaluate_item(item, prs, now=_NOW,
                              is_ancestor_fn=lambda _sha: False,
                              liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'unknown')
        self.assertEqual(d.cause, ptf.CAUSE_AMBIGUOUS)

    # Guard 5 — CLOSED settle: needs > one period AND two consecutive passes.
    def test_guard5_closed_within_period_blocks(self):
        item = _item()
        prs = [_pr(state='CLOSED', closed_at=_iso(_NOW - timedelta(minutes=5)))]
        d = ptf.evaluate_item(item, prs, now=_NOW, is_ancestor_fn=_yes_ancestor,
                              liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'keep')
        self.assertIn('not yet settled', d.reason)

    def test_guard5_closed_first_observation_blocks(self):
        item = _item()
        prs = [_pr(state='CLOSED', closed_at=_iso(_NOW - timedelta(hours=1)))]
        d = ptf.evaluate_item(item, prs, now=_NOW, prior_closed_seen=False,
                              is_ancestor_fn=_yes_ancestor, liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'keep')
        self.assertIn('two consecutive', d.reason)

    def test_guard5_closed_settled_two_passes_fans_out(self):
        item = _item()
        prs = [_pr(state='CLOSED', closed_at=_iso(_NOW - timedelta(hours=1)))]
        d = ptf.evaluate_item(item, prs, now=_NOW, prior_closed_seen=True,
                              is_ancestor_fn=_yes_ancestor, liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'fanout')
        self.assertEqual(d.terminal, tts.CLOSED)
        self.assertFalse(d.ancestry_verified)

    def test_probe_error_is_unknown(self):
        d = ptf.evaluate_item(_item(), None, now=_NOW)
        self.assertEqual(d.action, 'unknown')
        self.assertEqual(d.cause, ptf.CAUSE_PROBE_ERROR)

    def test_no_terminal_pr_is_unknown_no_match(self):
        item = _item()
        prs = [_pr(state='UNKNOWN')]
        d = ptf.evaluate_item(item, prs, now=_NOW, is_ancestor_fn=_yes_ancestor,
                              liveness_fn=_no_liveness)
        self.assertEqual(d.action, 'unknown')
        self.assertEqual(d.cause, ptf.CAUSE_NO_MATCH)


# ====================================================================
# § 10.3 / § 10.4 / § 10.5 — fan-out close ordering, partial failure,
# resolve legs, single-writer.
# ====================================================================
class FanoutCloseTest(unittest.TestCase):
    def _merged_decision(self):
        return ptf.Decision('fanout', terminal=tts.MERGED,
                            terminal_ts=_iso(_NOW), ancestry_verified=True,
                            grade='identity', reason='merged')

    def test_emit_before_cache_and_cache_last(self):
        order = []
        def emit(**kw):
            order.append('emit')
            return True
        cache = {'entries': {}}
        item = _item(store='inreview')
        res = ptf.fanout_close(item, self._merged_decision(), cache, _NOW,
                               emit_fn=emit)
        self.assertTrue(res['emitted'])
        self.assertTrue(res['cached'])
        self.assertEqual(order, ['emit'])
        # cache is the LAST durable write — present only after emit succeeded.
        self.assertTrue(ptf.cache_hit(cache, item, self._merged_decision(), _NOW))

    def test_emit_failure_does_not_cache(self):
        # § 10.3 partial failure: a failed emit must leave NO cache so the next
        # run re-executes the idempotent close.
        cache = {'entries': {}}
        item = _item()
        res = ptf.fanout_close(item, self._merged_decision(), cache, _NOW,
                               emit_fn=lambda **kw: False)
        self.assertFalse(res['emitted'])
        self.assertFalse(res['cached'])
        self.assertEqual(cache.get('entries'), {})

    def test_decision_store_resolves_with_entry_id(self):
        # § 10.4: the P-leg is entry_id-exact.
        calls = {}
        def resolve(**kw):
            calls.update(kw)
            return {'ok': True}
        item = _item(store='decision', entry_id='pending-77')
        res = ptf.fanout_close(item, self._merged_decision(), {'entries': {}}, _NOW,
                               emit_fn=lambda **kw: True, resolve_fn=resolve)
        self.assertEqual(calls['entry_id'], 'pending-77')
        self.assertEqual(calls['outcome'], 'approved')  # MERGED → approved
        self.assertNotIn('p_leg_delegated', res)

    def test_decision_store_without_entry_id_delegates_p_leg(self):
        item = _item(store='decision', entry_id=None)
        res = ptf.fanout_close(item, self._merged_decision(), {'entries': {}}, _NOW,
                               emit_fn=lambda **kw: True, resolve_fn=lambda **kw: {})
        self.assertTrue(res.get('p_leg_delegated'))

    def test_inreview_store_does_not_resolve(self):
        # § 10.5 single-writer: an inreview item never touches the decision leg.
        called = {'n': 0}
        def resolve(**kw):
            called['n'] += 1
            return {}
        res = ptf.fanout_close(_item(store='inreview'), self._merged_decision(),
                               {'entries': {}}, _NOW, emit_fn=lambda **kw: True,
                               resolve_fn=resolve)
        self.assertEqual(called['n'], 0)
        self.assertIsNone(res['resolved'])

    def test_closed_outcome_is_expired_not_approved(self):
        d = ptf.Decision('fanout', terminal=tts.CLOSED, terminal_ts=_iso(_NOW),
                        grade='identity')
        calls = {}
        ptf.fanout_close(_item(store='decision', entry_id='e1'), d, {'entries': {}},
                         _NOW, emit_fn=lambda **kw: True,
                         resolve_fn=lambda **kw: calls.update(kw) or {})
        self.assertEqual(calls['outcome'], 'expired')


# ====================================================================
# § 5 — terminal cache idempotency + CLOSED re-verify.
# ====================================================================
class CacheTest(unittest.TestCase):
    def test_merged_cache_permanent_hit(self):
        cache = {'entries': {}}
        item = _item()
        d = ptf.Decision('fanout', terminal=tts.MERGED, terminal_ts=_iso(_NOW),
                        ancestry_verified=True)
        ptf.cache_put(cache, item, d, _NOW)
        self.assertTrue(ptf.cache_hit(cache, item, d, _NOW + timedelta(weeks=4)))

    def test_closed_cache_reverifies_after_24h(self):
        cache = {'entries': {}}
        item = _item()
        d = ptf.Decision('fanout', terminal=tts.CLOSED, terminal_ts=_iso(_NOW))
        ptf.cache_put(cache, item, d, _NOW)
        self.assertTrue(ptf.cache_hit(cache, item, d, _NOW + timedelta(hours=1)))
        self.assertFalse(ptf.cache_hit(cache, item, d, _NOW + timedelta(hours=25)))

    def test_prune_drops_old_entries(self):
        cache = {'entries': {}}
        item = _item()
        d = ptf.Decision('fanout', terminal=tts.MERGED, terminal_ts=_iso(_NOW))
        ptf.cache_put(cache, item, d, _NOW - timedelta(weeks=30))
        self.assertEqual(ptf.prune_cache(cache, _NOW), 1)


# ====================================================================
# § 10.6 — UNKNOWN ledger, no-match declare-dead, probe-error storm.
# ====================================================================
class UnknownLedgerTest(unittest.TestCase):
    def test_consecutive_count_increments_same_cause(self):
        led = {'rows': {}}
        ptf.update_unknown_ledger(led, 'k1', ptf.CAUSE_NO_MATCH, _NOW)
        row = ptf.update_unknown_ledger(led, 'k1', ptf.CAUSE_NO_MATCH,
                                        _NOW + timedelta(hours=1))
        self.assertEqual(row['consecutive_count'], 2)

    def test_cause_change_resets_count(self):
        led = {'rows': {}}
        ptf.update_unknown_ledger(led, 'k1', ptf.CAUSE_NO_MATCH, _NOW)
        row = ptf.update_unknown_ledger(led, 'k1', ptf.CAUSE_AMBIGUOUS, _NOW)
        self.assertEqual(row['consecutive_count'], 1)

    def test_declare_dead_ripe_only_after_14d_and_3_probes(self):
        row = {'cause': ptf.CAUSE_NO_MATCH, 'consecutive_count': 3,
               'first_seen': _iso(_NOW - timedelta(days=14))}
        self.assertTrue(ptf.no_match_declare_dead(row, _NOW))

    def test_declare_dead_not_ripe_too_few_probes(self):
        row = {'cause': ptf.CAUSE_NO_MATCH, 'consecutive_count': 2,
               'first_seen': _iso(_NOW - timedelta(days=20))}
        self.assertFalse(ptf.no_match_declare_dead(row, _NOW))

    def test_declare_dead_never_for_non_no_match(self):
        row = {'cause': ptf.CAUSE_SEQUENCE_OWNED, 'consecutive_count': 9,
               'first_seen': _iso(_NOW - timedelta(days=90))}
        self.assertFalse(ptf.no_match_declare_dead(row, _NOW))

    def test_bound_drops_departed_keys(self):
        led = {'rows': {'k1': {}, 'k2': {}}}
        dropped = ptf.bound_unknown_ledger(led, {'k1'})
        self.assertEqual(dropped, 1)
        self.assertNotIn('k2', led['rows'])


# ====================================================================
# § 10.7 — parity miss vs lost race.
# ====================================================================
class ParityTest(unittest.TestCase):
    def test_true_miss_beyond_threshold(self):
        term = _iso(_NOW)
        close = _iso(_NOW + timedelta(minutes=25))  # > 15min period + 5min slack
        self.assertTrue(ptf.is_parity_miss(term, close, True))

    def test_lost_race_within_threshold_not_a_miss(self):
        term = _iso(_NOW)
        close = _iso(_NOW + timedelta(minutes=10))
        self.assertFalse(ptf.is_parity_miss(term, close, True))

    def test_not_appeared_in_pass_is_never_a_miss(self):
        term = _iso(_NOW)
        close = _iso(_NOW + timedelta(hours=3))
        self.assertFalse(ptf.is_parity_miss(term, close, False))


# ====================================================================
# § 10.10 — report-only default + self-firing arming (durable, once).
# ====================================================================
class ArmingTest(unittest.TestCase):
    def test_default_is_report_only(self):
        self.assertFalse(ptf.is_armed(ptf.default_arm_state()))

    def test_window_incomplete_before_48h_or_4_passes(self):
        arm = ptf.default_arm_state()
        ptf.record_pass(arm, _NOW)
        self.assertFalse(ptf.window_complete(arm, _NOW + timedelta(hours=72)))
        arm2 = ptf.default_arm_state()
        for i in range(4):
            ptf.record_pass(arm2, _NOW)
        self.assertFalse(ptf.window_complete(arm2, _NOW))  # < 48h

    def test_window_complete_after_48h_and_4_passes(self):
        arm = ptf.default_arm_state()
        ptf.record_pass(arm, _NOW)
        for i in range(3):
            ptf.record_pass(arm, _NOW + timedelta(hours=50))
        self.assertTrue(ptf.window_complete(arm, _NOW + timedelta(hours=50)))

    def test_request_arming_emits_exactly_once(self):
        arm = ptf.default_arm_state()
        emits = []
        first = ptf.request_arming(arm, 3, 0, _NOW,
                                   emit_fn=lambda **kw: emits.append(kw) or True)
        second = ptf.request_arming(arm, 3, 0, _NOW,
                                    emit_fn=lambda **kw: emits.append(kw) or True)
        self.assertTrue(first)
        self.assertFalse(second)  # idempotent on the durable flag
        self.assertEqual(len(emits), 1)
        self.assertTrue(arm['arming_requested'])

    def test_arming_recommends_against_on_divergence(self):
        arm = ptf.default_arm_state()
        captured = {}
        ptf.request_arming(arm, 5, 2, _NOW,
                           emit_fn=lambda **kw: captured.update(kw) or True)
        self.assertIn('AGAINST', captured['payload']['summary'])

    def test_apply_approve_flips_armed(self):
        arm = ptf.default_arm_state()
        ptf.apply_arming_decision(arm, True, _NOW)
        self.assertTrue(ptf.is_armed(arm))

    def test_apply_reject_stays_report_only(self):
        arm = ptf.default_arm_state()
        ptf.apply_arming_decision(arm, False, _NOW, reason='not yet')
        self.assertFalse(ptf.is_armed(arm))
        self.assertEqual(arm['reject_reason'], 'not yet')


# ====================================================================
# § 10.11 — run_cycle: report-only writes NO fan-out; armed does; all
# injected (zero live-tree writes, persist=False). Uses the CLOSED-settle
# path (no git ancestry seam runs) with a pre-seeded prior-CLOSED cache so
# the two-consecutive-pass guard is satisfied in a single call.
# ====================================================================
class RunCycleTest(unittest.TestCase):
    def setUp(self):
        # Seed the durable cache so the CLOSED item is on its SECOND observation
        # (prior_closed_seen True). Written to the sandbox-redirected path only.
        ptf._save_json(ptf.TERMINAL_CACHE_FILE,
                       {'entries': {}, 'closed_seen': {'inreview:t-1': _iso(_NOW)}})
        ptf._save_json(ptf.ARM_STATE_FILE, ptf.default_arm_state())

    def tearDown(self):
        for f in (ptf.TERMINAL_CACHE_FILE, ptf.ARM_STATE_FILE,
                  ptf.UNKNOWN_LEDGER_FILE):
            try:
                f.unlink()
            except OSError:
                pass

    def _closed_enumerator(self):
        return lambda: [_item(store='inreview')]

    def _closed_probe(self, _url):
        return [_pr(state='CLOSED', closed_at=_iso(_NOW - timedelta(hours=1)))]

    def test_report_only_default_no_fanout(self):
        emits = []
        art = ptf.run_cycle(
            enumerators=[self._closed_enumerator()],
            probe_fn=self._closed_probe,
            emit_fn=lambda **kw: emits.append(kw) or True,
            now=_NOW, persist=False,
        )
        self.assertTrue(art.get('report_only'))
        self.assertEqual(art['closed'], [])
        self.assertEqual(len(art['would_close']), 1)
        # no review_obsolete emitted while report-only
        self.assertFalse(any(e.get('event_type') == ptf.CLOSE_EVENT_TYPE
                             for e in emits))

    def test_armed_fans_out(self):
        emits = []
        arm = ptf.default_arm_state()
        arm['armed'] = True
        ptf._save_json(ptf.ARM_STATE_FILE, arm)
        art = ptf.run_cycle(
            enumerators=[self._closed_enumerator()],
            probe_fn=self._closed_probe,
            emit_fn=lambda **kw: emits.append(kw) or True,
            now=_NOW, persist=False,
        )
        self.assertEqual(len(art['closed']), 1)
        self.assertTrue(any(e.get('event_type') == ptf.CLOSE_EVENT_TYPE
                            for e in emits))

    def test_sequence_owned_item_is_not_probed(self):
        probed = {'n': 0}
        def probe(_url):
            probed['n'] += 1
            return [_pr(state='CLOSED', closed_at=_iso(_NOW - timedelta(hours=1)))]
        art = ptf.run_cycle(
            enumerators=[lambda: [_item(task_id='seq-abc', store='inreview')]],
            probe_fn=probe, emit_fn=lambda **kw: True,
            now=_NOW, persist=False,
        )
        self.assertEqual(probed['n'], 0)
        self.assertEqual(art['probed'], 0)


# ====================================================================
# § 10.8 — R1: wedged healer-committer detection (delta-age, not mtime).
# ====================================================================
class WedgedCommitterTest(unittest.TestCase):
    def setUp(self):
        self._orig_committers = hdg.healer_managed_committers
        self._orig_save = hdg.save_state
        self._orig_emit = hdg.emit_alert
        hdg.healer_managed_committers = lambda: [
            {'path': 'agents/beacon/missions.json',
             'committer': 'heal_missions_card_gc', 'cadence_min': 10.0}]
        hdg.save_state = lambda *a, **k: None
        self.fired = []
        hdg.emit_alert = (lambda state, subject, message, suggested_action,
                          now=None: (self.fired.append(subject) or True))

    def tearDown(self):
        hdg.healer_managed_committers = self._orig_committers
        hdg.save_state = self._orig_save
        hdg.emit_alert = self._orig_emit

    def test_clean_path_never_fires(self):
        state = {}
        fired = hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0, dirty_paths=[], in_deploy_grace=False)
        self.assertFalse(fired)
        self.assertEqual(self.fired, [])

    def test_dirty_under_threshold_does_not_fire(self):
        # threshold = max(3*10min, 45min) = 45min. First observed at t0.
        state = {}
        hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0,
            dirty_paths=['agents/beacon/missions.json'], in_deploy_grace=False)
        fired = hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0 + 30 * 60,
            dirty_paths=['agents/beacon/missions.json'], in_deploy_grace=False)
        self.assertFalse(fired)

    def test_dirty_past_threshold_fires_once(self):
        state = {}
        hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0,
            dirty_paths=['agents/beacon/missions.json'], in_deploy_grace=False)
        fired = hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0 + 46 * 60,
            dirty_paths=['agents/beacon/missions.json'], in_deploy_grace=False)
        self.assertTrue(fired)
        self.assertEqual(self.fired,
                         ['managed-committer-wedged:heal_missions_card_gc'])

    def test_deploy_grace_suppresses(self):
        state = {}
        hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0,
            dirty_paths=['agents/beacon/missions.json'], in_deploy_grace=False)
        fired = hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0 + 90 * 60,
            dirty_paths=['agents/beacon/missions.json'], in_deploy_grace=True)
        self.assertFalse(fired)

    def test_going_clean_forgets_first_observed(self):
        state = {}
        hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0,
            dirty_paths=['agents/beacon/missions.json'], in_deploy_grace=False)
        # committer commits → clean → bookkeeping forgets it
        hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0 + 20 * 60, dirty_paths=[],
            in_deploy_grace=False)
        self.assertNotIn('agents/beacon/missions.json',
                         state.get('managed_dirty', {}))
        # dirty again → clock restarts, so 46min-since-original does NOT fire
        fired = hdg.evaluate_managed_committer_wedged(
            'main', state, now=1000.0 + 46 * 60,
            dirty_paths=['agents/beacon/missions.json'], in_deploy_grace=False)
        self.assertFalse(fired)


# ====================================================================
# § 10.9 — R2: mission GC ship vs retire vs keep.
# ====================================================================
class MissionRetireVsShipTest(unittest.TestCase):
    def test_all_merged_ships(self):
        d = gc.classify_mission('in_flight', ['t-1', 't-2'],
                                {'t-1': tts.MERGED, 't-2': tts.MERGED})
        self.assertEqual(d.action, 'ship')

    def test_any_closed_unmerged_retires(self):
        d = gc.classify_mission('in_flight', ['t-1', 't-2'],
                                {'t-1': tts.MERGED, 't-2': tts.CLOSED})
        self.assertEqual(d.action, 'retire')
        self.assertIn('t-2', d.reason)

    def test_non_terminal_keeps(self):
        d = gc.classify_mission('in_flight', ['t-1', 't-2'],
                                {'t-1': tts.MERGED, 't-2': tts.OPEN})
        self.assertEqual(d.action, 'keep')

    def test_missing_task_state_keeps(self):
        d = gc.classify_mission('in_flight', ['t-1', 't-2'], {'t-1': tts.MERGED})
        self.assertEqual(d.action, 'keep')

    def test_non_reconcilable_phase_keeps(self):
        d = gc.classify_mission('shipped', ['t-1'], {'t-1': tts.MERGED})
        self.assertEqual(d.action, 'keep')

    def test_no_probeable_tasks_keeps(self):
        d = gc.classify_mission('in_flight', ['review-1', 'dag-preflight-2'], {})
        self.assertEqual(d.action, 'keep')

    def test_review_shaped_ignored_but_prs_ship(self):
        d = gc.classify_mission('in_flight', ['t-1', 'review-9'],
                                {'t-1': tts.MERGED})
        self.assertEqual(d.action, 'ship')


if __name__ == '__main__':
    unittest.main()
