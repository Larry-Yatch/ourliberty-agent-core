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
        self._original_runtime = tp.RUNTIME_POLICY_PATH
        self._original_repo = tp.REPO_POLICY_PATH
        tp.RUNTIME_POLICY_PATH = self._root / 'agents' / 'config' / 'trust-policy.json'
        tp.REPO_POLICY_PATH = self._root / 'repo' / 'config' / 'trust-policy.json'

    def tearDown(self):
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
    cleanly and produce force_ask for every shape we care about."""

    def test_repo_default_policy_loads_and_is_strict(self):
        # Path: <repo>/config/trust-policy.json
        repo_default = _REPO_SCRIPTS.parent / 'config' / 'trust-policy.json'
        self.assertTrue(repo_default.exists(), f'missing default policy at {repo_default}')
        policy = tp.load_policy(repo_default)
        self.assertEqual(policy['default_action'], 'force_ask')
        # Shipped policy carries exactly one documentary rule: the
        # closed-loop step-4 pulse-auto-dispatch force_ask carve-out.
        # It is intentionally redundant with default_action so the
        # operator sees the source has been considered.
        self.assertEqual(len(policy['rules']), 1)
        self.assertEqual(policy['rules'][0]['source'], 'pulse-auto-dispatch')
        self.assertEqual(policy['rules'][0]['action'], 'force_ask')

        # The documentary rule resolves to force_ask via the evaluator.
        action, _ = tp.evaluate(
            {'source': 'pulse-auto-dispatch', 'target_agent': 'beacon',
             'task_type': 'doc-only', 'target_repo': 'ourliberty-agent-core'},
            policy,
        )
        self.assertEqual(action, 'force_ask')

        # Even for the dispatch shape we most want to auto-approve later, default policy says force_ask.
        for source, target in [
            ('pulse', 'beacon'), ('beacon', 'forge'), ('mirror', 'beacon'),
        ]:
            action, _ = tp.evaluate(
                {'source': source, 'target_agent': target,
                 'task_type': 'doc-only', 'target_repo': 'ourliberty-agent-core'},
                policy,
            )
            self.assertEqual(action, 'force_ask')


if __name__ == '__main__':
    unittest.main()
