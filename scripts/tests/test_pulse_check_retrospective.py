#!/usr/bin/env python3
"""Tests for pulse_check_retrospective (Phase 3 Stage A — deterministic
gather / normalize / bucket / resolution-join / probation accounting).

Covers:
  - root-signature normalization (strip -YYYY-MM-DD, #<num>/PR ids, :-tail)
  - elevation extraction incl. the larry_alert route==escalate filter
  - resolution classification + the source_event_id join
  - bucketing: resolution histogram, weeks-recurring, examples, event_types
  - trend (new/rising/falling/steady) + promotion-probation dedup status
  - ledger update: weeks merge, resolved-since-last detection, flag carry
  - artifact summary counts + per-week sentinel validity
  - end-to-end run_retrospective + the --fixture main() path
  - fixture-pattern filter on chain_events task_ids

Supabase / disk / live alerts are never touched — every test drives the
pure-function core with injected inputs (Supabase only via a fake client).

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_pulse_check_retrospective
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import pulse_check_retrospective as pr  # noqa: E402


NOW = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)  # Monday
WEEK = pr.iso_week_monday(NOW.date())


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _elev_row(event_id, event_type, *, hours_ago=1.0, task_id='real-001',
              source='healer', subject='thing-broke', route=None,
              extra_payload=None):
    payload = {'source': source, 'subject': subject}
    if route is not None:
        payload['route'] = route
    if extra_payload:
        payload.update(extra_payload)
    return {
        'event_id': event_id,
        'event_type': event_type,
        'task_id': task_id,
        'agent': 'pulse',
        'ts': _iso(hours_ago),
        'payload': payload,
    }


def _res_row(event_id, event_type, source_event_id, *, hours_ago=0.5,
             task_id='real-001', action=None):
    payload = {'source_event_id': source_event_id}
    if action is not None:
        payload['action'] = action
    return {
        'event_id': event_id,
        'event_type': event_type,
        'task_id': task_id,
        'agent': 'larry',
        'ts': _iso(hours_ago),
        'payload': payload,
    }


# ---------------- normalization ----------------


class TestNormalizeSubject(unittest.TestCase):

    def test_strips_iso_date_suffix(self):
        self.assertEqual(pr.normalize_subject('weekly-2026-06-15'), 'weekly')
        self.assertEqual(
            pr.normalize_subject('check-i-2026-06-15-extra'), 'check-i')

    def test_collapses_colon_tail_to_family(self):
        self.assertEqual(
            pr.normalize_subject('still-stale-after-restart:inbox-watcher'),
            'still-stale-after-restart')
        self.assertEqual(
            pr.normalize_subject('wedged-review-reaped:wt-forge-p2a-medic'),
            'wedged-review-reaped')

    def test_strips_pr_and_issue_ids(self):
        self.assertEqual(pr.normalize_subject('merge PR-123 conflict'),
                         'merge-conflict')
        self.assertEqual(pr.normalize_subject('build failed #4567'),
                         'build-failed')

    def test_two_instances_share_signature(self):
        a = pr.root_signature('healer', 'wedged-review-reaped:wt-forge-p2a')
        b = pr.root_signature('healer', 'wedged-review-reaped:wt-mirror-x')
        self.assertEqual(a, b)

    def test_empty_subject_falls_back(self):
        self.assertEqual(pr.normalize_subject(''), '')
        self.assertEqual(pr.normalize_subject(None), '')
        sig = pr.root_signature('', '')
        self.assertEqual(sig, 'unknown-source::unknown-subject')


# ---------------- elevation extraction ----------------


class TestIsElevationRow(unittest.TestCase):

    def test_larry_alert_requires_escalate_route(self):
        self.assertTrue(pr.is_elevation_row(
            _elev_row('e1', pr.EV_LARRY_ALERT, route='escalate')))
        self.assertFalse(pr.is_elevation_row(
            _elev_row('e2', pr.EV_LARRY_ALERT, route='digest')))
        self.assertFalse(pr.is_elevation_row(
            _elev_row('e3', pr.EV_LARRY_ALERT, route='hold')))
        # No route at all → not an escalate larry_alert.
        self.assertFalse(pr.is_elevation_row(
            _elev_row('e4', pr.EV_LARRY_ALERT)))

    def test_other_elevation_types_count(self):
        for et in (pr.EV_ESCALATION, pr.EV_SENTINEL_ALERT,
                   pr.EV_APPROVAL_REQUEST, pr.EV_NEEDS_ATTENTION):
            self.assertTrue(pr.is_elevation_row(_elev_row('x', et)), et)

    def test_non_elevation_type_excluded(self):
        self.assertFalse(pr.is_elevation_row(_elev_row('x', 'session_done')))


class TestBuildElevation(unittest.TestCase):

    def test_extracts_fields(self):
        ev = pr.build_elevation(
            _elev_row('e1', pr.EV_ESCALATION, source='medic', subject='boom'))
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_id, 'e1')
        self.assertEqual(ev.source, 'medic')
        self.assertEqual(ev.subject, 'boom')

    def test_drops_fixture_task_ids(self):
        self.assertIsNone(pr.build_elevation(
            _elev_row('e1', pr.EV_ESCALATION, task_id='zz-fixture-x')))

    def test_requires_event_id_and_ts(self):
        row = _elev_row('', pr.EV_ESCALATION)
        self.assertIsNone(pr.build_elevation(row))
        row2 = _elev_row('e1', pr.EV_ESCALATION)
        row2['ts'] = 'not-a-timestamp'
        self.assertIsNone(pr.build_elevation(row2))


# ---------------- resolution classification + join ----------------


class TestClassifyResolution(unittest.TestCase):

    def test_clarify_response_is_ack(self):
        self.assertEqual(
            pr.classify_resolution(pr.EV_CLARIFY_RESPONSE, {}), pr.RES_ACK)

    def test_known_verbs_map(self):
        cases = {
            'approve': pr.RES_APPROVE, 'accepted': pr.RES_APPROVE,
            'reject': pr.RES_REJECT, 'dismissed': pr.RES_REJECT,
            'silence': pr.RES_SILENCE, 'snoozed': pr.RES_SILENCE,
            'ack': pr.RES_ACK,
        }
        for verb, bucket in cases.items():
            self.assertEqual(
                pr.classify_resolution(pr.EV_LARRY_ACTION, {'action': verb}),
                bucket, verb)

    def test_unknown_verb_defaults_to_ack(self):
        self.assertEqual(
            pr.classify_resolution(pr.EV_LARRY_ACTION, {'action': 'frobnicate'}),
            pr.RES_ACK)


class TestBuildResolutionAndJoin(unittest.TestCase):

    def test_requires_source_event_id(self):
        self.assertIsNone(pr.build_resolution(
            _res_row('r1', pr.EV_LARRY_ACTION, '')))

    def test_join_indexes_by_source_event_id(self):
        r1 = pr.build_resolution(
            _res_row('r1', pr.EV_LARRY_ACTION, 'e1', action='approve'))
        r2 = pr.build_resolution(
            _res_row('r2', pr.EV_CLARIFY_RESPONSE, 'e2'))
        by_eid = pr.join_resolutions([r1, r2])
        self.assertEqual(by_eid['e1'][0].bucket, pr.RES_APPROVE)
        self.assertEqual(by_eid['e2'][0].bucket, pr.RES_ACK)

    def test_drops_fixture_task_ids(self):
        self.assertIsNone(pr.build_resolution(
            _res_row('r1', pr.EV_LARRY_ACTION, 'e1', task_id='t-fixture')))


# ---------------- bucketing ----------------


class TestBucketElevations(unittest.TestCase):

    def _bucket(self, elevations, resolutions, *, ledger=None,
                alert_weeks=None, live=None):
        return pr.bucket_elevations(
            elevations,
            pr.join_resolutions(resolutions),
            alert_signature_weeks=alert_weeks or {},
            ledger=ledger or {},
            live_unresolved_task_ids=live or set(),
            week_anchor=WEEK,
        )

    def test_histogram_counts_latest_decision_and_none(self):
        e1 = pr.build_elevation(_elev_row('e1', pr.EV_ESCALATION, subject='boom'))
        e2 = pr.build_elevation(_elev_row('e2', pr.EV_ESCALATION, subject='boom'))
        # e1 approved, e2 unresolved.
        r1 = pr.build_resolution(
            _res_row('r1', pr.EV_LARRY_ACTION, 'e1', action='approve'))
        buckets = self._bucket([e1, e2], [r1])
        self.assertEqual(len(buckets), 1)
        b = buckets[0]
        self.assertEqual(b.count_this_period, 2)
        self.assertEqual(b.resolution_histogram[pr.RES_APPROVE], 1)
        self.assertEqual(b.resolution_histogram[pr.RES_NONE], 1)

    def test_latest_action_wins(self):
        e1 = pr.build_elevation(_elev_row('e1', pr.EV_ESCALATION))
        early = pr.build_resolution(
            _res_row('r1', pr.EV_LARRY_ACTION, 'e1', action='ack', hours_ago=5))
        late = pr.build_resolution(
            _res_row('r2', pr.EV_LARRY_ACTION, 'e1', action='approve', hours_ago=1))
        b = self._bucket([e1], [early, late])[0]
        self.assertEqual(b.resolution_histogram[pr.RES_APPROVE], 1)
        self.assertEqual(b.resolution_histogram[pr.RES_ACK], 0)

    def test_weeks_recurring_merges_alerts_ledger_and_current(self):
        e1 = pr.build_elevation(_elev_row('e1', pr.EV_ESCALATION, subject='boom'))
        sig = pr.root_signature('healer', 'boom')
        alert_weeks = {sig: {'2026-06-15'}}
        ledger = {sig: {'weeks': ['2026-06-08'], 'last_period_count': 1}}
        b = self._bucket([e1], [], ledger=ledger, alert_weeks=alert_weeks)[0]
        # 2026-06-08 (ledger) + 2026-06-15 (alerts) + current week == 3.
        self.assertEqual(b.weeks_recurring, 3)

    def test_recurrence_actionable_by_count(self):
        evs = [pr.build_elevation(_elev_row(f'e{i}', pr.EV_ESCALATION,
                                            subject='boom')) for i in range(3)]
        b = self._bucket(evs, [])[0]
        self.assertTrue(b.recurrence_actionable)

    def test_recurrence_actionable_by_weeks(self):
        e1 = pr.build_elevation(_elev_row('e1', pr.EV_ESCALATION, subject='boom'))
        sig = pr.root_signature('healer', 'boom')
        b = self._bucket([e1], [], ledger={sig: {'weeks': ['2026-06-08'],
                                                  'last_period_count': 1}})[0]
        self.assertTrue(b.recurrence_actionable)  # 2 weeks running

    def test_not_actionable_single_fire_single_week(self):
        e1 = pr.build_elevation(_elev_row('e1', pr.EV_ESCALATION, subject='boom'))
        b = self._bucket([e1], [])[0]
        self.assertFalse(b.recurrence_actionable)

    def test_live_unresolved_flag(self):
        e1 = pr.build_elevation(
            _elev_row('e1', pr.EV_ESCALATION, task_id='real-live-1'))
        b = self._bucket([e1], [], live={'real-live-1'})[0]
        self.assertTrue(b.live_unresolved)

    def test_examples_capped_and_sorted(self):
        evs = [pr.build_elevation(_elev_row(f'e{i}', pr.EV_ESCALATION,
                                            subject='boom', hours_ago=i + 1))
               for i in range(8)]
        b = self._bucket(evs, [])[0]
        self.assertEqual(len(b.examples), 5)

    def test_event_types_aggregated(self):
        e1 = pr.build_elevation(_elev_row('e1', pr.EV_ESCALATION, subject='boom'))
        e2 = pr.build_elevation(_elev_row('e2', pr.EV_NEEDS_ATTENTION,
                                          subject='boom'))
        b = self._bucket([e1, e2], [])[0]
        self.assertEqual(b.event_types,
                         sorted([pr.EV_ESCALATION, pr.EV_NEEDS_ATTENTION]))


# ---------------- trend + dedup ----------------


class TestTrend(unittest.TestCase):

    def test_new_when_no_prior_weeks(self):
        self.assertEqual(pr._trend(None, 3), 'new')
        self.assertEqual(pr._trend({'weeks': []}, 3), 'new')

    def test_rising_falling_steady(self):
        prev = {'weeks': ['2026-06-08'], 'last_period_count': 2}
        self.assertEqual(pr._trend(prev, 5), 'rising')
        self.assertEqual(pr._trend(prev, 1), 'falling')
        self.assertEqual(pr._trend(prev, 2), 'steady')


class TestDedupStatus(unittest.TestCase):

    def test_fresh_when_unseen(self):
        self.assertEqual(pr._dedup_status(None, 3), 'fresh')

    def test_already_proposed(self):
        self.assertEqual(
            pr._dedup_status({'proposed': True}, 3), 'already-proposed')

    def test_dismissed_suppressed_below_margin(self):
        prev = {'dismissed': True, 'dismissed_at_count': 4}
        # floor = 4 + DEDUP_REISSUE_MARGIN; count below floor → suppressed.
        self.assertEqual(pr._dedup_status(prev, 4), 'suppressed-dismissed')
        self.assertEqual(pr._dedup_status(prev, 5), 'suppressed-dismissed')

    def test_dismissed_resurfaces_when_recrossed(self):
        prev = {'dismissed': True, 'dismissed_at_count': 4}
        # 4 + 2 margin = 6 → count >= 6 resurfaces.
        self.assertEqual(pr._dedup_status(prev, 6), 'fresh')


# ---------------- ledger update ----------------


class TestUpdateLedger(unittest.TestCase):

    def _bucket(self, sig, count):
        return pr.Bucket(
            root_signature=sig, source='healer', normalized_subject='boom',
            count_this_period=count, weeks_recurring=1,
            resolution_histogram={b: 0 for b in pr.RESOLUTION_BUCKETS},
            examples=[], event_types=[], live_unresolved=False, trend='new',
            last_period_count=0, dedup_status='fresh', recurrence_actionable=False,
        )

    def test_resolved_detection_for_absent_prior_signature(self):
        sig_present = 'healer::boom'
        sig_gone = 'healer::old-noise'
        ledger = {
            sig_gone: {'weeks': ['2026-06-08'], 'last_period_count': 4,
                       'first_seen_ts': _iso(200)},
        }
        new_ledger, resolved = pr.update_ledger(
            ledger, [self._bucket(sig_present, 2)],
            alert_signature_weeks={}, week_anchor=WEEK, now=NOW)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]['root_signature'], sig_gone)
        # The gone signature is carried forward at zero, not dropped.
        self.assertEqual(new_ledger[sig_gone]['last_period_count'], 0)
        self.assertEqual(new_ledger[sig_present]['last_period_count'], 2)

    def test_carries_dedup_flags_forward(self):
        sig = 'healer::boom'
        ledger = {sig: {'weeks': ['2026-06-08'], 'last_period_count': 1,
                        'proposed': True, 'proposed_ts': _iso(100),
                        'dismissed': False, 'first_seen_ts': _iso(300)}}
        new_ledger, _ = pr.update_ledger(
            ledger, [self._bucket(sig, 3)],
            alert_signature_weeks={}, week_anchor=WEEK, now=NOW)
        self.assertTrue(new_ledger[sig]['proposed'])
        self.assertEqual(new_ledger[sig]['first_seen_ts'], _iso(300))
        self.assertIn(WEEK, new_ledger[sig]['weeks'])


# ---------------- artifact ----------------


class TestArtifact(unittest.TestCase):

    def _bucket(self, sig, count, dedup='fresh'):
        return pr.Bucket(
            root_signature=sig, source='healer', normalized_subject=sig,
            count_this_period=count, weeks_recurring=1,
            resolution_histogram={b: 0 for b in pr.RESOLUTION_BUCKETS},
            examples=[], event_types=[], live_unresolved=False, trend='new',
            last_period_count=0, dedup_status=dedup, recurrence_actionable=False,
        )

    def test_summary_counts(self):
        buckets = [
            self._bucket('a', 3, dedup='fresh'),
            self._bucket('b', 2, dedup='suppressed-dismissed'),
            self._bucket('c', 1, dedup='already-proposed'),
        ]
        art = pr.build_artifact(buckets, [{'root_signature': 'z',
                                           'last_period_count': 5}],
                                week_anchor=WEEK, as_of_iso=NOW.isoformat(),
                                total_elevations=6)
        s = art['summary']
        self.assertEqual(s['total_elevations'], 6)
        self.assertEqual(s['total_buckets'], 3)
        self.assertEqual(s['fresh_candidates'], 1)
        self.assertEqual(s['suppressed'], 1)
        self.assertEqual(s['resolved_since_last'], 1)
        self.assertIn('bucket', s['trend_line'])


class TestWriteArtifact(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = pr.ARTIFACT_DIR
        self._orig_canon = pr.CANONICAL_ARTIFACT
        pr.ARTIFACT_DIR = self.tmp / 'pulse-retrospective'
        pr.CANONICAL_ARTIFACT = self.tmp / 'retrospective-candidates.json'

    def tearDown(self):
        pr.ARTIFACT_DIR = self._orig_dir
        pr.CANONICAL_ARTIFACT = self._orig_canon
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_sentinel_and_canonical(self):
        art = pr.build_artifact([], [], week_anchor=WEEK,
                                as_of_iso=NOW.isoformat(), total_elevations=0)
        sentinel, canonical = pr.write_artifact(art)
        self.assertTrue(sentinel.exists())
        self.assertTrue(canonical.exists())
        self.assertTrue(pr._artifact_is_valid_sentinel(sentinel))
        body = json.loads(canonical.read_text())
        self.assertEqual(body['week_anchor'], WEEK)

    def test_truncated_sentinel_is_invalid(self):
        bad = pr.sentinel_path_for_week(WEEK)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text('{ truncated')
        self.assertFalse(pr._artifact_is_valid_sentinel(bad))


# ---------------- recurrence / live loaders ----------------


class TestLoadAlertSignatureWeeks(unittest.TestCase):

    def test_groups_weeks_within_window(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'alerts.jsonl'
            in_a = (NOW - timedelta(days=2)).isoformat()
            in_b = (NOW - timedelta(days=10)).isoformat()
            out = (NOW - timedelta(days=400)).isoformat()
            p.write_text(
                json.dumps({'ts': in_a, 'source': 'healer',
                            'subject': 'boom:x'}) + '\n'
                + json.dumps({'ts': in_b, 'source': 'healer',
                              'subject': 'boom:y'}) + '\n'
                + json.dumps({'ts': out, 'source': 'healer',
                              'subject': 'boom:z'}) + '\n'
            )
            weeks = pr.load_alert_signature_weeks(p, now=NOW)
            sig = pr.root_signature('healer', 'boom')
            # Two in-window fires in distinct weeks; the 400d-old one excluded.
            self.assertEqual(len(weeks[sig]), 2)


class TestLoadLiveUnresolved(unittest.TestCase):

    def test_extracts_task_ids(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'esc.json'
            p.write_text(json.dumps({
                'version': 1,
                'records': {
                    'escalation:abc': {'task_id': 'real-live-1'},
                    'clarify:def': {'task_id': 'real-live-2'},
                    'bad': {'no_task': True},
                },
            }))
            ids = pr.load_live_unresolved_task_ids(p)
            self.assertEqual(ids, {'real-live-1', 'real-live-2'})

    def test_missing_file_is_empty(self):
        self.assertEqual(
            pr.load_live_unresolved_task_ids(Path('/nope/none.json')), set())


# ---------------- end-to-end ----------------


class TestRunRetrospective(unittest.TestCase):

    def test_full_flow_buckets_joins_and_recurs(self):
        e1 = pr.build_elevation(_elev_row('e1', pr.EV_ESCALATION, subject='boom:a'))
        e2 = pr.build_elevation(_elev_row('e2', pr.EV_ESCALATION, subject='boom:b'))
        e3 = pr.build_elevation(
            _elev_row('e3', pr.EV_LARRY_ALERT, subject='noise', route='escalate'))
        # e1 approved; e2/e3 unresolved.
        r1 = pr.build_resolution(
            _res_row('r1', pr.EV_LARRY_ACTION, 'e1', action='approve'))
        sig_boom = pr.root_signature('healer', 'boom')
        artifact, new_ledger = pr.run_retrospective(
            elevations=[e1, e2, e3], resolutions=[r1],
            alert_signature_weeks={sig_boom: {'2026-06-15'}},
            ledger={sig_boom: {'weeks': ['2026-06-08'], 'last_period_count': 1}},
            live_unresolved_task_ids=set(),
            week_anchor=WEEK, now=NOW,
        )
        self.assertEqual(artifact['summary']['total_elevations'], 3)
        # boom:a + boom:b collapse to one signature (count 2); noise is separate.
        boom = next(c for c in artifact['candidates']
                    if c['root_signature'] == sig_boom)
        self.assertEqual(boom['count_this_period'], 2)
        self.assertEqual(boom['resolution_histogram']['approve'], 1)
        self.assertEqual(boom['resolution_histogram']['none'], 1)
        self.assertEqual(boom['weeks_recurring'], 3)
        self.assertEqual(boom['trend'], 'rising')  # 2 > last_period_count 1
        self.assertIn(sig_boom, new_ledger)
        self.assertEqual(new_ledger[sig_boom]['last_period_count'], 2)


class TestMainFixturePath(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_fixture_prints_artifact(self):
        fixture = {
            'elevation_rows': [
                _elev_row('e1', pr.EV_ESCALATION, subject='boom'),
                _elev_row('e2', pr.EV_LARRY_ALERT, subject='digest-noise',
                          route='digest'),  # excluded
            ],
            'resolution_rows': [
                _res_row('r1', pr.EV_LARRY_ACTION, 'e1', action='reject'),
            ],
            'alert_signature_weeks': {},
            'ledger': {},
            'live_unresolved_task_ids': [],
        }
        fpath = self.tmp / 'fixture.json'
        fpath.write_text(json.dumps(fixture))
        rc = pr.main(['--dry-run', '--fixture', str(fpath)])
        self.assertEqual(rc, 0)


# ---------------- G5: expired verification_pending surfacing ----------------


def _cycle_pending_row(intervention_id, *, days_ago, tier=1):
    """A cycle_prime_ledger verification_pending row, aged relative to real
    now (main() uses wall-clock now on the fixture path)."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        'ts': ts, 'iter': 1, 'tier': tier, 'kind': 'verification_pending',
        'intervention_id': intervention_id,
        'fix_commit_sha': None, 'chain_event_id': None,
        'verifies_at': None, 'verified_at': None, 'verification_anchor': None,
    }


class TestExpiredVerificationArtifact(unittest.TestCase):

    def test_expired_list_and_summary_present(self):
        entries = [
            {'intervention_id': 'restart-daemon:beacon', 'ts': _iso(240),
             'tier': 1, 'iter': 5, 'age_days': 10},
            {'intervention_id': 'retry-sync-push:iter-9', 'ts': _iso(300),
             'tier': 2, 'iter': 9, 'age_days': 12},
        ]
        art = pr.build_artifact([], [], week_anchor=WEEK,
                                as_of_iso=NOW.isoformat(), total_elevations=0,
                                expired_verifications=entries)
        self.assertEqual(art['expired_verifications'], entries)
        self.assertEqual(art['summary']['expired_verifications'], 2)
        line = art['summary']['expired_verifications_line']
        self.assertIn('restart-daemon:beacon', line)
        self.assertIn('2', line)

    def test_empty_expired_gives_blank_line(self):
        art = pr.build_artifact([], [], week_anchor=WEEK,
                                as_of_iso=NOW.isoformat(), total_elevations=0)
        self.assertEqual(art['expired_verifications'], [])
        self.assertEqual(art['summary']['expired_verifications'], 0)
        self.assertEqual(art['summary']['expired_verifications_line'], '')

    def test_run_retrospective_embeds_expired_with_age(self):
        raw = {'ts': _iso(9 * 24), 'iter': 3, 'tier': 1,
               'kind': 'verification_pending', 'intervention_id': 'x1'}
        artifact, _ = pr.run_retrospective(
            elevations=[], resolutions=[], alert_signature_weeks={},
            ledger={}, live_unresolved_task_ids=set(),
            week_anchor=WEEK, now=NOW, expired_verifications=[raw],
        )
        self.assertEqual(len(artifact['expired_verifications']), 1)
        entry = artifact['expired_verifications'][0]
        self.assertEqual(entry['intervention_id'], 'x1')
        self.assertEqual(entry['age_days'], 9)


class TestSurfacedVerificationLedger(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = self.tmp / 'retrospective-ledger.json'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trips_surfaced_set(self):
        pr.write_ledger({'healer::boom': {'weeks': ['2026-06-08']}},
                        self.ledger, surfaced_verifications=['x1', 'x2'])
        self.assertEqual(pr.load_surfaced_verifications(self.ledger),
                         {'x1', 'x2'})
        # signatures survive alongside surfaced ids.
        self.assertIn('healer::boom', pr.load_ledger(self.ledger))

    def test_missing_file_is_empty_set(self):
        self.assertEqual(
            pr.load_surfaced_verifications(self.tmp / 'nope.json'), set())

    def test_absent_key_is_empty_set(self):
        self.ledger.write_text(json.dumps({'version': 1, 'signatures': {}}))
        self.assertEqual(pr.load_surfaced_verifications(self.ledger), set())


class TestMainFixtureExpiredSurfacing(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_dry(self, fixture):
        fpath = self.tmp / 'fixture.json'
        fpath.write_text(json.dumps(fixture))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pr.main(['--dry-run', '--fixture', str(fpath)])
        self.assertEqual(rc, 0)
        # Last JSON object printed is the artifact.
        return json.loads(buf.getvalue())

    def test_expired_row_surfaces_when_unseen(self):
        fixture = {
            'elevation_rows': [], 'resolution_rows': [],
            'alert_signature_weeks': {}, 'ledger': {},
            'live_unresolved_task_ids': [],
            'cycle_ledger_rows': [_cycle_pending_row('x1', days_ago=9)],
            'surfaced_verifications': [],
        }
        art = self._run_dry(fixture)
        ids = [e['intervention_id'] for e in art['expired_verifications']]
        self.assertEqual(ids, ['x1'])
        self.assertEqual(art['summary']['expired_verifications'], 1)

    def test_already_surfaced_row_suppressed(self):
        fixture = {
            'elevation_rows': [], 'resolution_rows': [],
            'alert_signature_weeks': {}, 'ledger': {},
            'live_unresolved_task_ids': [],
            'cycle_ledger_rows': [_cycle_pending_row('x1', days_ago=9)],
            'surfaced_verifications': ['x1'],
        }
        art = self._run_dry(fixture)
        self.assertEqual(art['expired_verifications'], [])
        self.assertEqual(art['summary']['expired_verifications'], 0)

    def test_within_window_row_not_surfaced(self):
        fixture = {
            'elevation_rows': [], 'resolution_rows': [],
            'alert_signature_weeks': {}, 'ledger': {},
            'live_unresolved_task_ids': [],
            'cycle_ledger_rows': [_cycle_pending_row('x1', days_ago=2)],
            'surfaced_verifications': [],
        }
        art = self._run_dry(fixture)
        self.assertEqual(art['expired_verifications'], [])


if __name__ == '__main__':
    unittest.main()
