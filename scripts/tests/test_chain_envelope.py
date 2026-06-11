#!/usr/bin/env python3
"""test_chain_envelope.py — contract pins for the M1 envelope constructor
(``scripts/chain_envelope.py``), the foundation the rest of the Chain Context
Durability sequence (``agents/beacon/specs/chain-context-durability.md``)
builds on.

The builder's job is to make context-field propagation a *visible, code-
reviewed decision* at every dispatch site: each whitelisted field must be
resolved (CARRY from the inbound envelope, an explicit value, or an explicit
DROP), and the per-field guard must preserve the historical
conditional-copy semantics exactly — including the subtlety that a meaningful
``0`` (e.g. ``replan_count == 0`` = "first leg") survives the ``not_none``
guard while a falsy value under a ``truthy`` guard is omitted.
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

import chain_envelope  # noqa: E402
from chain_envelope import (  # noqa: E402
    BACKFILLABLE_FIELDS,
    CARRY,
    CHAIN_CONTEXT_FIELDS,
    DROP,
    backfill_pr_url,
    backfill_target_repo,
    build_chain_envelope,
)


def _all_drop(**overrides):
    """Carry mapping that DROPs every whitelisted field except the given
    overrides — keeps each test focused on one field."""
    carry = {field: DROP for field in CHAIN_CONTEXT_FIELDS}
    carry.update(overrides)
    return carry


class CarryResolutionTest(unittest.TestCase):
    def test_carry_passes_value_through_from_source(self):
        env = build_chain_envelope(
            {'task_id': 't1'},
            {'target_repo': 'Larry-Yatch/agent-core'},
            carry=_all_drop(target_repo=CARRY),
        )
        self.assertEqual(env['target_repo'], 'Larry-Yatch/agent-core')

    def test_carry_with_none_source_omits_field(self):
        env = build_chain_envelope(
            {'task_id': 't1'}, None, carry=_all_drop(target_repo=CARRY),
        )
        self.assertNotIn('target_repo', env)

    def test_explicit_value_wins_over_source(self):
        env = build_chain_envelope(
            {'task_id': 't1'},
            {'target_repo': 'from-source'},
            carry=_all_drop(target_repo='explicit'),
        )
        self.assertEqual(env['target_repo'], 'explicit')

    def test_drop_omits_even_when_source_has_it(self):
        env = build_chain_envelope(
            {'task_id': 't1'},
            {'pr_url': 'https://example/pull/1'},
            carry=_all_drop(pr_url=DROP),
        )
        self.assertNotIn('pr_url', env)


class GuardSemanticsTest(unittest.TestCase):
    def test_truthy_guard_omits_empty_string(self):
        env = build_chain_envelope(
            {'task_id': 't1'}, None, carry=_all_drop(target_repo=''),
        )
        self.assertNotIn('target_repo', env)

    def test_not_none_guard_keeps_zero(self):
        # replan_count == 0 means "first leg" — it must survive.
        env = build_chain_envelope(
            {'task_id': 't1'}, None, carry=_all_drop(replan_count=0),
        )
        self.assertEqual(env['replan_count'], 0)

    def test_not_none_guard_omits_none(self):
        env = build_chain_envelope(
            {'task_id': 't1'}, None, carry=_all_drop(reply_chat_id=None),
        )
        self.assertNotIn('reply_chat_id', env)

    def test_carry_zero_count_survives_not_none_guard(self):
        env = build_chain_envelope(
            {'task_id': 't1'}, {'replan_count': 0},
            carry=_all_drop(replan_count=CARRY),
        )
        self.assertEqual(env['replan_count'], 0)


class ForcingFunctionTest(unittest.TestCase):
    """The whole point of M1: an unresolved or mis-typed field is a hard error,
    not a silent drop."""

    def test_missing_whitelist_key_raises(self):
        carry = {field: DROP for field in CHAIN_CONTEXT_FIELDS}
        carry.pop('pr_url')
        with self.assertRaises(ValueError):
            build_chain_envelope({'task_id': 't1'}, None, carry=carry)

    def test_unknown_carry_key_raises(self):
        with self.assertRaises(ValueError):
            build_chain_envelope(
                {'task_id': 't1'}, None,
                carry=_all_drop(not_a_field='x'),
            )

    def test_whitelisted_field_in_base_raises(self):
        with self.assertRaises(ValueError):
            build_chain_envelope(
                {'task_id': 't1', 'target_repo': 'x'}, None,
                carry=_all_drop(),
            )

    def test_missing_task_id_raises(self):
        with self.assertRaises(ValueError):
            build_chain_envelope({'prompt': 'hi'}, None, carry=_all_drop())

    def test_non_mapping_base_raises(self):
        with self.assertRaises(ValueError):
            build_chain_envelope(['task_id'], None, carry=_all_drop())

    def test_non_mapping_source_raises(self):
        with self.assertRaises(ValueError):
            build_chain_envelope(
                {'task_id': 't1'}, 'not-a-mapping', carry=_all_drop(),
            )


class IsolationTest(unittest.TestCase):
    def test_returns_fresh_dict_not_base(self):
        base = {'task_id': 't1', 'prompt': 'p'}
        env = build_chain_envelope(base, None, carry=_all_drop())
        self.assertIsNot(env, base)
        env['mutated'] = True
        self.assertNotIn('mutated', base)

    def test_base_fields_passthrough(self):
        env = build_chain_envelope(
            {'task_id': 't1', 'prompt': 'p', 'source': 'beacon'},
            None, carry=_all_drop(),
        )
        self.assertEqual(env['task_id'], 't1')
        self.assertEqual(env['prompt'], 'p')
        self.assertEqual(env['source'], 'beacon')


def _write_registry(dir_path: Path, missions: list) -> Path:
    """Write a minimal missions.json fixture and return its path."""
    path = dir_path / 'missions.json'
    path.write_text(json.dumps({'schema_version': 1, 'missions': missions}))
    return path


class BackfillTargetRepoTest(unittest.TestCase):
    """M3: target_repo <- mission registry. The mission that owns a task_id
    names the repo the work lands in; a dropped target_repo is recoverable
    from it rather than a dead-end."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        registry = _write_registry(self.dir, [
            {'id': 'm1', 'task_ids': ['alpha-1', 'alpha-2'],
             'repo': 'ourliberty-agent-core'},
            {'id': 'm2', 'task_ids': ['dash-1'],
             'repo': 'ourliberty-dashboard'},
        ])
        patcher = mock.patch.dict(
            os.environ, {'OURLIBERTY_MISSIONS_JSON': str(registry)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resolver_finds_repo_of_owning_mission(self):
        self.assertEqual(backfill_target_repo('alpha-2'), 'ourliberty-agent-core')
        self.assertEqual(backfill_target_repo('dash-1'), 'ourliberty-dashboard')

    def test_resolver_returns_none_for_unowned_task(self):
        self.assertIsNone(backfill_target_repo('not-a-task'))

    def test_resolver_returns_none_for_missing_registry(self):
        with mock.patch.dict(
            os.environ,
            {'OURLIBERTY_MISSIONS_JSON': str(self.dir / 'nope.json')},
        ):
            self.assertIsNone(backfill_target_repo('alpha-1'))

    def test_resolver_returns_none_for_empty_task_id(self):
        self.assertIsNone(backfill_target_repo(''))
        self.assertIsNone(backfill_target_repo(None))

    def test_resolver_survives_malformed_registry(self):
        bad = self.dir / 'bad.json'
        bad.write_text('{not json')
        with mock.patch.dict(
            os.environ, {'OURLIBERTY_MISSIONS_JSON': str(bad)},
        ):
            self.assertIsNone(backfill_target_repo('alpha-1'))

    def test_builder_backfills_missing_target_repo(self):
        env = build_chain_envelope(
            {'task_id': 'alpha-1'}, None,
            carry=_all_drop(target_repo=DROP),
            backfill={'target_repo'},
        )
        self.assertEqual(env['target_repo'], 'ourliberty-agent-core')

    def test_builder_does_not_overwrite_resolved_target_repo(self):
        # An explicitly-resolved value wins; backfill only fills a MISSING field.
        env = build_chain_envelope(
            {'task_id': 'alpha-1'}, None,
            carry=_all_drop(target_repo='explicit-repo'),
            backfill={'target_repo'},
        )
        self.assertEqual(env['target_repo'], 'explicit-repo')

    def test_builder_omits_when_unrecoverable(self):
        # No owning mission → field stays absent (the M2 fall-through case).
        env = build_chain_envelope(
            {'task_id': 'orphan-task'}, None,
            carry=_all_drop(target_repo=DROP),
            backfill={'target_repo'},
        )
        self.assertNotIn('target_repo', env)

    def test_no_backfill_request_leaves_field_absent(self):
        # Default (backfill=None) does no I/O and never fills the field.
        env = build_chain_envelope(
            {'task_id': 'alpha-1'}, None,
            carry=_all_drop(target_repo=DROP),
        )
        self.assertNotIn('target_repo', env)


class BackfillPrUrlTest(unittest.TestCase):
    """M3: pr_url <- gh. The Forge build PR's head branch is the
    `forge/<task_id>` convention; a dropped pr_url is recoverable by asking
    GitHub directly rather than dead-ending."""

    def test_resolver_queries_gh_with_forge_branch(self):
        seen = {}

        def fake_gh(repo_full, branch):
            seen['repo_full'] = repo_full
            seen['branch'] = branch
            return 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/77'

        with mock.patch.object(chain_envelope, '_gh_pr_url_for_branch', fake_gh):
            url = backfill_pr_url('alpha-1', target_repo='ourliberty-agent-core')
        self.assertEqual(
            url, 'https://github.com/Larry-Yatch/ourliberty-agent-core/pull/77',
        )
        self.assertEqual(seen['repo_full'], 'Larry-Yatch/ourliberty-agent-core')
        self.assertEqual(seen['branch'], 'forge/alpha-1')

    def test_resolver_honors_explicit_branch(self):
        with mock.patch.object(
            chain_envelope, '_gh_pr_url_for_branch',
            lambda repo_full, branch: f'url-for:{branch}',
        ):
            url = backfill_pr_url(
                'alpha-1', target_repo='ourliberty-agent-core',
                branch='forge/custom-branch',
            )
        self.assertEqual(url, 'url-for:forge/custom-branch')

    def test_resolver_returns_none_without_resolvable_repo(self):
        # No target_repo passed and no registry entry → no repo to scope gh.
        with mock.patch.object(chain_envelope, 'backfill_target_repo',
                               lambda task_id: None):
            self.assertIsNone(backfill_pr_url('alpha-1'))

    def test_resolver_returns_none_on_gh_failure(self):
        with mock.patch.object(chain_envelope, '_gh_pr_url_for_branch',
                               lambda repo_full, branch: None):
            self.assertIsNone(
                backfill_pr_url('alpha-1', target_repo='ourliberty-agent-core'),
            )

    def test_builder_backfills_missing_pr_url_using_branch(self):
        with mock.patch.object(
            chain_envelope, '_gh_pr_url_for_branch',
            lambda repo_full, branch: f'https://gh/pr#{branch}',
        ):
            env = build_chain_envelope(
                {'task_id': 'alpha-1', 'branch': 'forge/alpha-1'},
                None,
                carry=_all_drop(target_repo='ourliberty-agent-core', pr_url=DROP),
                backfill={'pr_url'},
            )
        self.assertEqual(env['pr_url'], 'https://gh/pr#forge/alpha-1')

    def test_builder_backfills_both_repo_then_pr_url(self):
        # target_repo is registry-derived first, then the pr_url resolver scopes
        # its gh query to it — verifying the deterministic ordering invariant.
        seen = {}

        def fake_gh(repo_full, branch):
            seen['repo_full'] = repo_full
            return 'https://gh/pr/9'

        with tempfile.TemporaryDirectory() as td:
            registry = _write_registry(Path(td), [
                {'id': 'm', 'task_ids': ['alpha-1'],
                 'repo': 'ourliberty-agent-core'},
            ])
            with mock.patch.dict(
                os.environ, {'OURLIBERTY_MISSIONS_JSON': str(registry)},
            ), mock.patch.object(
                chain_envelope, '_gh_pr_url_for_branch', fake_gh,
            ):
                env = build_chain_envelope(
                    {'task_id': 'alpha-1', 'branch': 'forge/alpha-1'}, None,
                    carry=_all_drop(target_repo=DROP, pr_url=DROP),
                    backfill={'target_repo', 'pr_url'},
                )
        self.assertEqual(env['target_repo'], 'ourliberty-agent-core')
        self.assertEqual(env['pr_url'], 'https://gh/pr/9')
        self.assertEqual(seen['repo_full'], 'Larry-Yatch/ourliberty-agent-core')


class BackfillContractTest(unittest.TestCase):
    def test_backfill_must_be_subset_of_backfillable(self):
        with self.assertRaises(ValueError):
            build_chain_envelope(
                {'task_id': 't1'}, None,
                carry=_all_drop(), backfill={'reply_chat_id'},
            )

    def test_backfillable_fields_are_whitelisted(self):
        self.assertTrue(BACKFILLABLE_FIELDS <= set(CHAIN_CONTEXT_FIELDS))


if __name__ == '__main__':
    unittest.main()
