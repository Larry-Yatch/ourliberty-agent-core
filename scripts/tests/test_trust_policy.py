#!/usr/bin/env python3
"""Fixtures for `trust_policy` — the autonomy-tier evaluator.

Phase D3-prep (2026-05-11). Covers first-match-wins ordering, default
fallback when no rule matches, glob-style repo + file matching, malformed
policy handling (fails closed to force_ask), and the shipped default policy
shape (empty rules → everything force_ask).

Run:
    cd /home/larry/agent-core && python3 -m unittest scripts.tests.test_trust_policy
"""

from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import trust_policy as tp  # noqa: E402


class EvaluateTest(unittest.TestCase):
    """Rule matching + default fallback."""

    def test_empty_policy_falls_back_to_force_ask(self):
        action, rule = tp.evaluate(
            {'source': 'beacon', 'target': 'forge'},
            {'version': 1, 'default_action': 'force_ask', 'rules': []},
        )
        self.assertEqual(action, 'force_ask')
        self.assertIsNone(rule)

    def test_custom_default_used(self):
        action, _ = tp.evaluate(
            {'source': 'beacon', 'target': 'forge'},
            {'version': 1, 'default_action': 'reject', 'rules': []},
        )
        self.assertEqual(action, 'reject')

    def test_exact_match_rule(self):
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [{
                'source': 'pulse', 'target': 'forge', 'task_type': 'doc-only',
                'action': 'auto_approve',
            }],
        }
        action, rule = tp.evaluate(
            {'source': 'pulse', 'target_agent': 'forge', 'task_type': 'doc-only'},
            policy,
        )
        self.assertEqual(action, 'auto_approve')
        self.assertIsNotNone(rule)

    def test_first_match_wins(self):
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [
                {'source': 'pulse', 'target': '*', 'action': 'auto_approve'},
                {'source': '*', 'target': '*', 'action': 'reject'},
            ],
        }
        action, _ = tp.evaluate(
            {'source': 'pulse', 'target_agent': 'forge'}, policy,
        )
        # First rule matches (Pulse), so we get auto_approve, not reject.
        self.assertEqual(action, 'auto_approve')

    def test_no_rule_matches_falls_to_default(self):
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [{'source': 'pulse', 'target': 'forge', 'action': 'auto_approve'}],
        }
        action, rule = tp.evaluate(
            {'source': 'mirror', 'target_agent': 'beacon'}, policy,
        )
        self.assertEqual(action, 'force_ask')
        self.assertIsNone(rule)

    def test_star_wildcard_matches_any(self):
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [{
                'source': '*', 'target': '*', 'task_type': '*',
                'action': 'reject',
            }],
        }
        for source, target, ttype in [
            ('pulse', 'beacon', 'observation'),
            ('beacon', 'forge', 'feature-development'),
            ('mirror', 'beacon', 'code-review'),
        ]:
            action, _ = tp.evaluate(
                {'source': source, 'target_agent': target, 'task_type': ttype},
                policy,
            )
            self.assertEqual(action, 'reject', f'{source}->{target} should reject')

    def test_missing_field_treated_as_match_any(self):
        # Rule has no `source` key → matches any source.
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [{'target': 'forge', 'action': 'auto_approve'}],
        }
        for source in ['pulse', 'beacon', 'mirror']:
            action, _ = tp.evaluate(
                {'source': source, 'target_agent': 'forge'}, policy,
            )
            self.assertEqual(action, 'auto_approve')

    def test_repo_glob_matching(self):
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [{
                'repos': ['TruPath-*'],
                'action': 'reject',
            }, {
                'repos': ['ourliberty-*'],
                'action': 'auto_approve',
            }],
        }
        for repo, expected in [
            ('TruPath-website', 'reject'),
            ('TruPath-app', 'reject'),
            ('ourliberty-agent-core', 'auto_approve'),
            ('ourliberty-website', 'auto_approve'),
            ('rocket-station-presentation', 'force_ask'),  # no match
        ]:
            action, _ = tp.evaluate(
                {'source': 'beacon', 'target_agent': 'forge', 'target_repo': repo},
                policy,
            )
            self.assertEqual(action, expected, f'repo={repo}')

    def test_repo_specified_no_repo_in_task_means_no_match(self):
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [{'repos': ['TruPath-*'], 'action': 'reject'}],
        }
        action, _ = tp.evaluate(
            {'source': 'beacon', 'target_agent': 'forge'},  # no target_repo
            policy,
        )
        self.assertEqual(action, 'force_ask')

    def test_file_pattern_matching(self):
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [{
                'file_patterns': ['docs/**', '*.md'],
                'action': 'auto_approve',
            }],
        }
        # At least one matching file → match
        action, _ = tp.evaluate(
            {'source': 'beacon', 'target_agent': 'forge',
             'changed_files': ['docs/operating-manual.md', 'scripts/x.py']},
            policy,
        )
        self.assertEqual(action, 'auto_approve')
        # No matching files → fall through to default
        action, _ = tp.evaluate(
            {'source': 'beacon', 'target_agent': 'forge',
             'changed_files': ['scripts/inbox_watcher.py']},
            policy,
        )
        self.assertEqual(action, 'force_ask')


class LoadPolicyTest(unittest.TestCase):
    """File loading semantics — runtime > repo > default-deny fallback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._original_override = tp.OVERRIDE_POLICY_PATH
        self._original_runtime = tp.RUNTIME_POLICY_PATH
        self._original_repo = tp.REPO_POLICY_PATH
        # Stub ALL THREE resolution layers into the tmpdir. The override layer
        # (~/agents/trust-policy.override.json, added after this suite) is checked
        # FIRST by _resolve_policy_path. Left unstubbed it resolves to the real
        # dial file on any machine where the dial has been used (e.g. the droplet),
        # so load_policy() reads that instead of the per-test tmp files and every
        # runtime>repo>default-deny assertion below fails. None of these tests
        # write an override, so pointing it at a non-existent tmp path keeps the
        # resolver on the runtime>repo>default-deny chain they exercise.
        tp.OVERRIDE_POLICY_PATH = self._root / 'agents' / 'trust-policy.override.json'
        tp.RUNTIME_POLICY_PATH = self._root / 'agents' / 'config' / 'trust-policy.json'
        tp.REPO_POLICY_PATH = self._root / 'repo' / 'config' / 'trust-policy.json'

    def tearDown(self):
        tp.OVERRIDE_POLICY_PATH = self._original_override
        tp.RUNTIME_POLICY_PATH = self._original_runtime
        tp.REPO_POLICY_PATH = self._original_repo
        self._tmp.cleanup()

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f)

    def test_missing_files_returns_default_deny(self):
        policy = tp.load_policy()
        self.assertEqual(policy['default_action'], 'force_ask')
        self.assertEqual(policy['rules'], [])

    def test_runtime_path_wins_over_repo(self):
        self._write(
            tp.REPO_POLICY_PATH,
            {'version': 1, 'default_action': 'reject', 'rules': []},
        )
        self._write(
            tp.RUNTIME_POLICY_PATH,
            {'version': 1, 'default_action': 'auto_approve', 'rules': []},
        )
        policy = tp.load_policy()
        self.assertEqual(policy['default_action'], 'auto_approve')

    def test_falls_back_to_repo_when_runtime_missing(self):
        self._write(
            tp.REPO_POLICY_PATH,
            {'version': 1, 'default_action': 'reject', 'rules': []},
        )
        policy = tp.load_policy()
        self.assertEqual(policy['default_action'], 'reject')

    def test_malformed_rule_action_falls_to_default_deny(self):
        # audit #21: a bad-schema (but valid-JSON) policy must fail CLOSED to
        # force_ask, not raise TrustPolicyError out of load_policy()/evaluate()
        # and crash the approval path.
        self._write(
            tp.RUNTIME_POLICY_PATH,
            {'version': 1, 'rules': [{'source': 'pulse', 'action': 'bogus'}]},
        )
        policy = tp.load_policy()
        self.assertEqual(policy['default_action'], 'force_ask')
        self.assertEqual(policy['rules'], [])
        self.assertIn('_error', policy)

    def test_malformed_default_action_falls_to_default_deny(self):
        self._write(
            tp.RUNTIME_POLICY_PATH,
            {'version': 1, 'default_action': 'bogus', 'rules': []},
        )
        policy = tp.load_policy()
        self.assertEqual(policy['default_action'], 'force_ask')
        self.assertIn('_error', policy)

    def test_malformed_policy_evaluates_to_force_ask(self):
        # end-to-end: evaluate() on a bad-schema policy returns force_ask, never
        # raises (the contract the beacon approval path relies on).
        self._write(
            tp.RUNTIME_POLICY_PATH,
            {'version': 1, 'default_action': 'bogus', 'rules': []},
        )
        action, rule = tp.evaluate({'source': 'pulse', 'target': 'forge'})
        self.assertEqual(action, 'force_ask')
        self.assertIsNone(rule)

    def test_bad_json_falls_to_default_deny(self):
        tp.RUNTIME_POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tp.RUNTIME_POLICY_PATH.write_text('{ malformed json')
        policy = tp.load_policy()
        # Failed read → safe default (force_ask), with error noted internally.
        self.assertEqual(policy['default_action'], 'force_ask')
        self.assertIn('_error', policy)


class ShippedDefaultPolicyTest(unittest.TestCase):
    """The default config/trust-policy.json that ships in the repo must parse
    cleanly and encode the intended autonomy gate: Beacon->Forge agent-core
    auto_approve (2026-06-21 rung 1) + pulse-auto-dispatch auto_approve
    (2026-06-22 rung 2), with sensitive paths + every other shape held at
    force_ask."""

    def test_repo_default_policy_loads_and_encodes_gate(self):
        # Path: <repo>/config/trust-policy.json
        repo_default = _REPO_SCRIPTS.parent / 'config' / 'trust-policy.json'
        self.assertTrue(repo_default.exists(), f'missing default policy at {repo_default}')
        policy = tp.load_policy(repo_default)
        self.assertEqual(policy['default_action'], 'force_ask')

        # Five rules, first-match-wins order. Each force_ask carve-out MUST stay
        # ordered before its broad auto_approve sibling:
        #   0: pulse-auto-dispatch sensitive-intent carve-out -> force_ask (#658, 2026-06-23)
        #   1: pulse-auto-dispatch                            -> auto_approve (2026-06-22)
        #   2: beacon->forge agent-core sensitive paths       -> force_ask (carve-out)
        #   3: suite-guardian->forge scripts/tests/**         -> auto_approve (PR-3 L4)
        #   4: beacon->forge agent-core                       -> auto_approve (gate)
        self.assertEqual(len(policy['rules']), 5)
        self.assertEqual(policy['rules'][0]['source'], 'pulse-auto-dispatch')
        self.assertEqual(policy['rules'][0]['action'], 'force_ask')
        self.assertEqual(policy['rules'][1]['source'], 'pulse-auto-dispatch')
        self.assertEqual(policy['rules'][1]['action'], 'auto_approve')
        self.assertEqual(policy['rules'][2]['source'], 'beacon')
        self.assertEqual(policy['rules'][2]['action'], 'force_ask')
        self.assertEqual(policy['rules'][3]['source'], 'suite-guardian')
        self.assertEqual(policy['rules'][3]['action'], 'auto_approve')
        self.assertEqual(policy['rules'][3]['file_patterns'], ['scripts/tests/**'])
        self.assertEqual(policy['rules'][4]['source'], 'beacon')
        self.assertEqual(policy['rules'][4]['action'], 'auto_approve')

        def ev(**task):
            action, _ = tp.evaluate(task, policy)
            return action

        # pulse-auto-dispatch now auto-fires (2026-06-22 carve-out): the envelope
        # only asks Beacon to draft a spec, and Pulse only auto-dispatches small
        # $-quantified agent-core optimizations. Mirror still gates the merge.
        self.assertEqual(ev(source='pulse-auto-dispatch', target_agent='beacon',
                            target_repo='ourliberty-agent-core'), 'auto_approve')

        # THE GATE: a fresh Beacon->Forge agent-core build auto-fires, including
        # a code revision (changed_files that aren't sensitive).
        self.assertEqual(ev(source='beacon', target_agent='forge',
                            target_repo='ourliberty-agent-core'), 'auto_approve')
        self.assertEqual(ev(source='beacon', target_agent='forge',
                            target_repo='ourliberty-agent-core',
                            changed_files=['scripts/inbox_watcher.py']), 'auto_approve')

        # PR-3 L4: a suite-guardian fix declaring a scripts/tests/** file
        # auto-approves at the POLICY layer. This rule is any-match on declared
        # files (the permissive half); the mechanical SHA-bound diff gate in
        # outbox_notifier is what enforces ALL-files-in-scope at merge time. A
        # suite-guardian dispatch touching NO test file matches no rule -> the
        # default force_ask.
        self.assertEqual(ev(source='suite-guardian', target_agent='forge',
                            changed_files=['scripts/tests/test_x.py']),
                         'auto_approve')
        self.assertEqual(ev(source='suite-guardian', target_agent='forge',
                            changed_files=['scripts/outbox_notifier.py']),
                         'force_ask')

        # Sensitive-path revisions still ask (carve-out bites when files known).
        for f in ('config/agent-models.json', 'systemd/x.service',
                  'supabase/migrations/001.sql', 'deploy/x.sh', '.env.larry',
                  'migrations/001.sql'):
            self.assertEqual(ev(source='beacon', target_agent='forge',
                                target_repo='ourliberty-agent-core',
                                changed_files=[f]), 'force_ask', f)

        # Other repos and non-(beacon->forge) shapes still ask.
        self.assertEqual(ev(source='beacon', target_agent='forge',
                            target_repo='ourliberty-dashboard'), 'force_ask')
        for source, target in [('pulse', 'beacon'), ('mirror', 'beacon')]:
            self.assertEqual(ev(source=source, target_agent=target,
                                target_repo='ourliberty-agent-core'), 'force_ask')


class SummarizePolicyTest(unittest.TestCase):
    """summarize_policy — the plain-language posture read for the dashboard's
    read-only autonomy panel (projects-v3 P7.3)."""

    def test_empty_policy_reads_conservative(self):
        s = tp.summarize_policy(
            {'version': 1, 'default_action': 'force_ask', 'rules': []})
        self.assertEqual(s['level'], 'conservative')
        self.assertEqual(s['auto_starts'], [])
        # The default-ask line is always present under force_ask.
        self.assertTrue(any('default' in x for x in s['still_asks']))
        self.assertEqual(len(s['gates']), 4)
        self.assertFalse(s['degraded'])

    def test_auto_rule_reads_balanced_and_describes_in_plain_language(self):
        policy = {
            'version': 1, 'default_action': 'force_ask',
            'rules': [
                {'source': 'pulse-auto-dispatch', 'target': '*',
                 'action': 'auto_approve'},
                {'source': 'beacon', 'target': 'forge',
                 'repos': ['ourliberty-agent-core'],
                 'file_patterns': ['config/**'], 'action': 'force_ask'},
                {'source': 'beacon', 'target': 'forge',
                 'repos': ['ourliberty-agent-core'], 'action': 'auto_approve'},
            ],
        }
        s = tp.summarize_policy(policy)
        self.assertEqual(s['level'], 'balanced')
        self.assertIn('Pulse auto-dispatch', s['auto_starts'])
        self.assertIn('Beacon→Forge builds for ourliberty-agent-core',
                      s['auto_starts'])
        # The sensitive-path carve-out reads as a still-asks line.
        self.assertTrue(any('sensitive paths' in x for x in s['still_asks']))
        # No raw machine fields / globs leak into the plain-language lines.
        joined = ' '.join(s['auto_starts'] + s['still_asks'])
        self.assertNotIn('config/**', joined)
        self.assertNotIn('auto_approve', joined)

    def test_degraded_policy_flag(self):
        s = tp.summarize_policy(tp._fail_closed('boom'))
        self.assertTrue(s['degraded'])
        self.assertEqual(s['level'], 'conservative')


if __name__ == '__main__':
    unittest.main()
