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

import sys
import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

from chain_envelope import (  # noqa: E402
    CARRY,
    CHAIN_CONTEXT_FIELDS,
    DROP,
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


if __name__ == '__main__':
    unittest.main()
