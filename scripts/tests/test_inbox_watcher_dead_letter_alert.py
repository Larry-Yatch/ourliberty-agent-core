#!/usr/bin/env python3
"""A dead-lettered envelope that was LARRY'S action must never be silent.

The 2026-08-26 audit of the live droplet found six drop branches in
inbox_watcher and exactly ONE of them (routing) alerting. Counted across
`~/agents/inboxes/*/.invalid` since 2026-05-11:

    27  SILENT drops from a human control surface   <- 24 dashboard schema
                                                       rejections, 2 larry, 1 worktree
    64  silent drops from an agent                  <- pulse/beacon plumbing churn
    45  routing drops, which DID alert

Every one of those 27 was an action Larry took that the API accepted and then
discarded. This pins the alert, and pins that agent churn stays quiet — paging
on those 64 is the alert toil the priorities file calls Tier 3.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_inbox_watcher_dead_letter_alert
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import inbox_watcher as iw  # noqa: E402
import dispatch_validator as dv  # noqa: E402

SCHEMA_REASON = 'validator: prompt too short (43 chars, min 100)'


class DeadLetterAlertTest(unittest.TestCase):

    def _drop(self, source, reason=SCHEMA_REASON, agent='forge', body=None,
              raw=None):
        inbox = iw.INBOXES_ROOT / agent
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / f'resume-{source}-{agent}-r1.json'
        if raw is not None:
            f.write_text(raw)
        else:
            f.write_text(json.dumps(body or {
                'task_id': 't1', 'source': source,
                'prompt': 'Yes — go with option B, skip the migration.'}))
        with mock.patch.object(iw.larry_alerts, 'append_alert') as al:
            iw.write_invalid(f, reason)
        return al, iw.INBOXES_ROOT / agent / '.invalid' / f.name

    # ---- the defect ----

    def test_dashboard_schema_reject_alerts(self):
        """THE REGRESSION. 24 of these were binned in silence."""
        al, _ = self._drop('dashboard')
        self.assertTrue(al.called)
        kw = al.call_args.kwargs
        self.assertEqual(kw['severity'], 'warning')
        self.assertTrue(kw['subject'].startswith('dead-letter:dashboard->forge:'),
                        kw['subject'])
        self.assertIn('DISCARDED', kw['message'])
        self.assertIn('NO auto-replay', kw['message'])

    def test_larry_source_alerts(self):
        al, _ = self._drop('larry')
        self.assertTrue(al.called)

    def test_operator_text_reads_the_minimum_from_the_gate(self):
        """The advice must not restate the rule — a restated number drifts."""
        al, _ = self._drop('dashboard')
        self.assertIn(str(dv.MIN_PROMPT_LEN),
                      al.call_args.kwargs['suggested_action'])

    # ---- the blast radius: agent churn must NOT become toil ----

    def test_agent_sources_stay_quiet(self):
        for source in ('pulse', 'beacon', 'mirror', 'beacon-clarification'):
            with self.subTest(source=source):
                al, _ = self._drop(source)
                self.assertFalse(al.called)

    def test_routing_denial_does_not_double_alert(self):
        """The routing branch emits its OWN richer, source-agnostic tripwire.
        Alerting again here would double-page on all 42 dashboard denials."""
        al, _ = self._drop('dashboard', reason='routing: route dashboard -> x')
        self.assertFalse(al.called)

    # ---- failure paths: alerting must never break the drop ----

    def test_unparseable_envelope_does_not_crash_or_alert(self):
        al, dest = self._drop('dashboard', raw='{not json at all')
        self.assertFalse(al.called)
        self.assertTrue(dest.exists(), 'the drop must still be recorded')

    def test_missing_source_does_not_alert(self):
        al, _ = self._drop('dashboard', body={'task_id': 't1', 'prompt': 'x'})
        self.assertFalse(al.called)

    def test_alert_failure_still_records_the_drop(self):
        """An alerting problem must never cost us the dead-letter itself."""
        inbox = iw.INBOXES_ROOT / 'forge'
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / 'boom.json'
        f.write_text(json.dumps({'task_id': 't', 'source': 'dashboard',
                                 'prompt': 'p'}))
        with mock.patch.object(iw.larry_alerts, 'append_alert',
                               side_effect=RuntimeError('telegram down')):
            iw.write_invalid(f, SCHEMA_REASON)  # must not raise
        dest = iw.INBOXES_ROOT / 'forge' / '.invalid' / 'boom.json'
        self.assertTrue(dest.exists())
        self.assertTrue(dest.with_suffix('.json.reason').exists())

    # ---- the structural claim ----

    def test_alert_lives_in_write_invalid_so_new_branches_inherit_it(self):
        """Putting this at CALL SITES is what let five of six branches stay
        silent. Pin BEHAVIOURALLY that write_invalid itself invokes it, so a
        seventh drop branch cannot be born silent.

        The previous version asserted the helper NAME appeared in
        `inspect.getsource(write_invalid)` — which a COMMENTED-OUT call keeps
        green and a correct inline refactor turns red. Exactly the
        test-the-restatement defect this file's own subject matter is about.
        """
        inbox = iw.INBOXES_ROOT / 'forge'
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / 'placement.json'
        f.write_text(json.dumps({'task_id': 't', 'source': 'dashboard',
                                 'prompt': 'p'}))
        with mock.patch.object(iw, '_alert_if_human_action_lost') as helper:
            iw.write_invalid(f, SCHEMA_REASON)
        helper.assert_called_once()
        self.assertEqual(helper.call_args.args[1], 'forge')


    # ---- finding 1: the burst. THE defect this round exists to close. ----

    def test_two_losses_in_one_hour_both_page(self):
        """MEASURED REGRESSION. The 60-min warning cooldown is keyed
        `source:subject`; with a constant subject the second loss inside an hour
        was swallowed — append_alert returns False BEFORE writing, so no DM, no
        digest, no row, no trace. On the live ledger 17 of 26 human losses fell
        inside another's hour, and 2026-05-30T23:00 lost FIFTEEN and would have
        paged ONCE. Distinct envelopes must produce distinct cooldown keys.
        """
        subjects = []
        for n in (1, 2, 3):
            inbox = iw.INBOXES_ROOT / 'forge'
            inbox.mkdir(parents=True, exist_ok=True)
            f = inbox / f'burst-{n}.json'
            f.write_text(json.dumps({'task_id': f't{n}', 'source': 'dashboard',
                                     'prompt': 'p'}))
            with mock.patch.object(iw.larry_alerts, 'append_alert') as al:
                iw.write_invalid(f, SCHEMA_REASON)
            self.assertTrue(al.called, f'loss {n} did not page')
            subjects.append(al.call_args.kwargs['subject'])
        self.assertEqual(len(set(subjects)), 3,
                         f'distinct losses share a cooldown key: {subjects}')

    def test_subject_still_tiers_via_the_colon_strip_prefix(self):
        """The per-envelope subject must not become an untranslatable novelty —
        that is the #1093 defect that produced #1108. `translate_alert` strips
        trailing ':'-segments, so every one of these reduces to the stable
        `dead-letter` prefix."""
        al, _ = self._drop('dashboard')
        subject = al.call_args.kwargs['subject']
        self.assertEqual(subject.split(':')[0], 'dead-letter')

    # ---- finding 2/4: the list is DERIVED, not declared ----

    def test_human_sources_are_derived_not_restated(self):
        """The hand-maintained set is gone; this reads dispatch_validator's own
        data. A restated list is what shipped a decommissioned lane."""
        self.assertFalse(hasattr(iw, 'HUMAN_CONTROL_SURFACES'),
                         'the hand-maintained list is back')
        self.assertEqual(dv.HUMAN_SOURCES, frozenset({'larry', 'dashboard'}))
        self.assertTrue(dv.HUMAN_SOURCES <= dv.ALLOWED_SOURCES)

    def test_decommissioned_telegram_webhook_is_not_claimed(self):
        """It was in the old set; the service was decommissioned 2026-05-12 and
        no producer stamps it, so claiming it asserted empty coverage."""
        self.assertNotIn('telegram-webhook', dv.HUMAN_SOURCES)

    # ---- finding 6: a non-hashable source must not escape ----

    def test_non_hashable_source_does_not_escape(self):
        inbox = iw.INBOXES_ROOT / 'forge'
        inbox.mkdir(parents=True, exist_ok=True)
        f = inbox / 'listsource.json'
        f.write_text(json.dumps({'task_id': 't', 'source': ['dashboard'],
                                 'prompt': ''}))
        with mock.patch.object(iw.larry_alerts, 'append_alert') as al:
            iw.write_invalid(f, SCHEMA_REASON)  # must not raise
        self.assertFalse(al.called)
        self.assertTrue((iw.INBOXES_ROOT / 'forge' / '.invalid'
                         / 'listsource.json').exists())

    # ---- finding 7: advice only for the reason it describes ----

    def test_length_advice_only_on_prompt_too_short(self):
        al, _ = self._drop('dashboard', reason=SCHEMA_REASON)
        self.assertIn('greater length', al.call_args.kwargs['suggested_action'])
        al2, _ = self._drop('dashboard',
                            reason='validator: prompt too long (60000 chars, max 50000)')
        self.assertNotIn('greater length',
                         al2.call_args.kwargs['suggested_action'])

    # ---- finding 8: the path is derived from dest, not hardcoded ----

    def test_advice_path_is_the_real_resolved_path(self):
        al, dest = self._drop('dashboard')
        self.assertIn(str(dest), al.call_args.kwargs['suggested_action'])

    # ---- finding 5: the routing prefix is named once ----

    def test_routing_prefix_is_a_shared_constant(self):
        self.assertTrue(iw.ROUTING_REASON_PREFIX.startswith('routing:'))


if __name__ == '__main__':
    unittest.main()
