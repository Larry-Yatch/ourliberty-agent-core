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
        self.assertEqual(kw['subject'], 'dead-letter:dashboard->forge')
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
        silent. Pin that it is inside write_invalid, so a seventh drop branch
        cannot be born silent."""
        import inspect
        src = inspect.getsource(iw.write_invalid)
        self.assertIn('_alert_if_human_action_lost', src)


if __name__ == '__main__':
    unittest.main()
