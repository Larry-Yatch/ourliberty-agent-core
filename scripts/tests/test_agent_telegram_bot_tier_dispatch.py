#!/usr/bin/env python3
"""W2 (spec docs/specs/tier-dispatch-spec.md §4/§5/§6/§10-W2): the generic
agent telegram bot (forge/mirror/pulse) chooses the dispatch tier per message
via select_dispatch_tier, binds new sessions to their tier, stamps an
account-tagged costs.jsonl row, and holds gracefully when no tier is available.

agent_telegram_bot.py reads required env (AGENT, token, allowed chats, agent
dir) AT IMPORT, so this module presets that env + a tmp HOME before importing
it, then restores HOME so the swap can't leak into sibling suites.
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
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))

# ---- import-time env scaffolding ------------------------------------------
_TMP = tempfile.TemporaryDirectory()
_ROOT = Path(_TMP.name)
(_ROOT / 'agent-core' / 'agents' / 'forge').mkdir(parents=True, exist_ok=True)
(_ROOT / 'agents').mkdir(parents=True, exist_ok=True)

_SAVED_ENV = {k: os.environ.get(k) for k in (
    'HOME', 'AGENT', 'TELEGRAM_BOT_TOKEN_FORGE', 'TELEGRAM_ALLOWED_CHAT_IDS',
    'OURLIBERTY_AGENTS_ROOT', 'OURLIBERTY_CREDENTIALS_ENV_FILE',
    'CLAUDE_CODE_OAUTH_TOKEN_TIER1', 'CLAUDE_CODE_OAUTH_TOKEN_TIER2',
    'CLAUDE_CODE_OAUTH_TOKEN_TIER3')}
os.environ['HOME'] = str(_ROOT)
os.environ['AGENT'] = 'forge'
os.environ['TELEGRAM_BOT_TOKEN_FORGE'] = 'dummy-token'
os.environ['TELEGRAM_ALLOWED_CHAT_IDS'] = '123'
os.environ['OURLIBERTY_AGENTS_ROOT'] = str(_ROOT / 'agents')
os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = str(_ROOT / 'no.env')
os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = 'sk-ant-oat01-t1'
os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER3'] = 'sk-ant-oat01-t3'
os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)

import agent_telegram_bot as bot  # noqa: E402
import active_tier  # noqa: E402

# Restore the FULL import-time env immediately: `bot` and `active_tier` have already frozen
# what they read at import (AGENT_DIR, tokens, AGENTS_ROOT), so these module-scope writes
# must not outlive this import — or they leak into sibling modules that freeze paths at THEIR
# import during `unittest discover` COLLECTION (which runs before any test), flipping
# test_deploy_notifier + test_heal_orphan_autoregister by discovery order. Per-test env is
# re-established in setUp below, so restoring here is safe. See [[test-isolation-hygiene-debt]].
def _restore_import_env():
    for _k, _v in _SAVED_ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


_restore_import_env()

_COSTS = _ROOT / 'agents' / 'blackboard' / 'costs.jsonl'


def _fake_completed(returncode=0, stdout='', stderr=''):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def _ok_json(session_id='sess-new', cost=0.3):
    return json.dumps({'result': 'hi back', 'session_id': session_id,
                       'model': 'claude-opus-4-8', 'total_cost_usd': cost,
                       'usage': {'input_tokens': 10, 'output_tokens': 4,
                                 'cache_creation_input_tokens': 1}})


class _BotBase(unittest.TestCase):
    def setUp(self):
        # Keep the env tokens / agents-root pinned per test (sibling suites may
        # have mutated them); re-assert.
        os.environ['OURLIBERTY_AGENTS_ROOT'] = str(_ROOT / 'agents')
        os.environ['OURLIBERTY_CREDENTIALS_ENV_FILE'] = str(_ROOT / 'no.env')
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER1'] = 'sk-ant-oat01-t1'
        os.environ['CLAUDE_CODE_OAUTH_TOKEN_TIER3'] = 'sk-ant-oat01-t3'
        os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN_TIER2', None)
        if _COSTS.exists():
            _COSTS.unlink()
        # Clear any pin / cooldowns from a prior test.
        for p in (_ROOT / 'agents' / 'rotation.disabled',):
            if p.exists():
                p.unlink()
        for t in ('tier1', 'tier2', 'tier3'):
            try:
                active_tier.clear_cooldown(t)
            except Exception:
                pass

    def _cost_rows(self):
        if not _COSTS.exists():
            return []
        return [json.loads(ln) for ln in _COSTS.read_text().splitlines()
                if ln.strip()]


class CallAgentDispatchTest(_BotBase):
    def test_new_message_selects_primary_and_stamps_cost(self):
        with mock.patch.object(bot.subprocess, 'run',
                               return_value=_fake_completed(0, _ok_json())):
            reply, new_session = bot.call_agent('hello', None)
        self.assertEqual(reply, 'hi back')
        self.assertEqual(new_session, 'sess-new')
        # Session bound to a primary tier; cost row stamped with the same.
        bound = active_tier.lookup_session_tier('sess-new')
        self.assertIn(bound, {'tier1', 'tier3'})
        rows = self._cost_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['account'], bound)
        self.assertEqual(rows[0]['cost_usd'], 0.3)

    def test_resume_dispatches_on_bound_tier(self):
        active_tier.record_session_tier('sess-bound', 'tier3')
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['env'] = kwargs.get('env', {})
            captured['cmd'] = cmd
            return _fake_completed(0, _ok_json(session_id='sess-bound'))

        with mock.patch.object(bot.subprocess, 'run', side_effect=fake_run):
            reply, _ = bot.call_agent('again', 'sess-bound')
        self.assertEqual(reply, 'hi back')
        self.assertIn('--resume', captured['cmd'])
        # Dispatched on tier3 -> tier3's setup-token in the child env.
        self.assertEqual(captured['env'].get('CLAUDE_CODE_OAUTH_TOKEN'),
                         'sk-ant-oat01-t3')
        self.assertEqual(self._cost_rows()[0]['account'], 'tier3')

    def test_creds_fallback_pins_agents_root_and_git_config(self):
        # Force the creds.json path: pin tier2 (no setup-token) so auth_source
        # is credentials_json and HOME swaps -> the I10/I11 pins must fire.
        (_ROOT / 'agents' / 'rotation.disabled').write_text('tier2')
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['env'] = kwargs.get('env', {})
            return _fake_completed(0, _ok_json())

        with mock.patch.object(bot.subprocess, 'run', side_effect=fake_run):
            bot.call_agent('hello', None)
        env = captured['env']
        # HOME swapped to the tier home; gh/git config pinned to the real home
        # so a push survives the swap (I11). OURLIBERTY_AGENTS_ROOT uses
        # setdefault (a no-op here since the sandbox already sets it — matching
        # run_claude); the gh/git pins are the observable proof the block ran.
        self.assertEqual(env['HOME'], active_tier.home_for_tier('tier2'))
        self.assertEqual(env['GH_CONFIG_DIR'],
                         os.path.join(active_tier.TIER1_HOME, '.config', 'gh'))
        self.assertEqual(env['GIT_CONFIG_GLOBAL'],
                         os.path.join(active_tier.TIER1_HOME, '.gitconfig'))

    def test_all_benched_returns_capacity_message_no_cost(self):
        active_tier.set_cooldown('tier1', raw_excerpt='resets 3pm')
        active_tier.set_cooldown('tier3', raw_excerpt='resets 3pm')
        # tier2 has no token -> unusable; nothing available.
        with mock.patch.object(bot.subprocess, 'run') as run:
            reply, sid = bot.call_agent('hello', None)
        run.assert_not_called()
        self.assertIn('rate-limited', reply)
        self.assertEqual(self._cost_rows(), [])


if __name__ == '__main__':
    unittest.main()
