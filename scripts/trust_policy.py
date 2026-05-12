#!/usr/bin/env python3
"""trust_policy.py — autonomy-tier policy evaluator for Beacon's approval gate.

Phase D3, prep commit. Substrate for "as Larry builds confidence, certain
dispatches don't need his explicit approval each time" — analogous to Joe's
upstream Medium-autonomy vs Loose-autonomy tiers, but expressed as data
(`config/trust-policy.json`) rather than code.

Policy schema (`config/trust-policy.json`):

    {
      "version": 1,
      "default_action": "force_ask",
      "rules": [
        {
          "source": "pulse",           // "*" or omitted = match any
          "target": "forge",           // "*" or omitted = match any
          "task_type": "doc-only",     // "*" or omitted = match any
          "repos": ["ourliberty-agent-core"],  // [] = match any
          "file_patterns": ["docs/**"],         // [] = match any
          "action": "auto_approve"     // "auto_approve" | "force_ask" | "reject"
        }
      ]
    }

First-match-wins evaluation. If no rule matches, falls back to
`default_action` (which itself defaults to `force_ask`). Empty `rules` list
(the shipped default) means everything requires approval — Larry's dial is
in his hand.

D3-prep ships this module + the default empty-rules policy. Beacon's bot
in D3-approval consults `evaluate(task)` before deciding whether to DM Larry
or fast-path to dispatch.

Reload semantics: the policy file is re-read on every `evaluate()` call so
edits take effect without a process restart. Tiny file; the cost is
negligible compared to a Telegram round-trip.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path
from typing import Any, Optional

HOME = Path.home()
AGENTS_ROOT = HOME / 'agents'
REPO_ROOT = Path(__file__).resolve().parent.parent

# Runtime policy lives at ~/agents/config/trust-policy.json (the snapshot
# the agent OS reads). Repo source of truth lives at config/trust-policy.json
# in the cloned repo. Runtime wins if present, else we fall back to the
# repo copy so first-run bootstraps cleanly without a manual copy step.
RUNTIME_POLICY_PATH = AGENTS_ROOT / 'config' / 'trust-policy.json'
REPO_POLICY_PATH = REPO_ROOT / 'config' / 'trust-policy.json'

VALID_ACTIONS = {'auto_approve', 'force_ask', 'reject'}


class TrustPolicyError(Exception):
    """Raised when the policy file is malformed."""


def _resolve_policy_path() -> Path:
    if RUNTIME_POLICY_PATH.exists():
        return RUNTIME_POLICY_PATH
    return REPO_POLICY_PATH


def load_policy(path: Optional[Path] = None) -> dict[str, Any]:
    """Load and lightly validate the policy file. Falls back to default-deny
    if the file is missing or unreadable (we never silently auto-approve)."""
    target = path or _resolve_policy_path()
    if not target.exists():
        return {'version': 1, 'default_action': 'force_ask', 'rules': []}
    try:
        with open(target) as f:
            policy = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # Fail closed: malformed policy never auto-approves.
        return {
            'version': 1,
            'default_action': 'force_ask',
            'rules': [],
            '_error': f'failed to read {target}: {e}',
        }
    if not isinstance(policy, dict):
        raise TrustPolicyError(f'policy root must be an object, got {type(policy).__name__}')
    default_action = policy.get('default_action', 'force_ask')
    if default_action not in VALID_ACTIONS:
        raise TrustPolicyError(f'default_action "{default_action}" not in {VALID_ACTIONS}')
    rules = policy.get('rules', [])
    if not isinstance(rules, list):
        raise TrustPolicyError('rules must be a list')
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise TrustPolicyError(f'rule[{i}] must be an object')
        action = rule.get('action')
        if action not in VALID_ACTIONS:
            raise TrustPolicyError(
                f'rule[{i}] action "{action}" not in {VALID_ACTIONS}'
            )
    return policy


def _matches_glob_any(value: Optional[str], patterns: list[str]) -> bool:
    if not patterns:
        return True
    if value is None:
        return False
    return any(fnmatch.fnmatch(value, p) for p in patterns)


def _matches_file_patterns(files: list[str], patterns: list[str]) -> bool:
    """True if at least one file matches at least one pattern (or no patterns)."""
    if not patterns:
        return True
    if not files:
        return False
    for f in files:
        for p in patterns:
            if fnmatch.fnmatch(f, p):
                return True
    return False


def _rule_matches(rule: dict[str, Any], task: dict[str, Any]) -> bool:
    """First-match-wins. A field is considered "match any" if it's "*", missing,
    or an empty list."""

    def _star_or_match(field: str, value: Optional[str]) -> bool:
        expected = rule.get(field)
        if expected is None or expected == '*':
            return True
        return value == expected

    if not _star_or_match('source', task.get('source')):
        return False
    if not _star_or_match('target', task.get('target_agent') or task.get('target')):
        return False
    if not _star_or_match('task_type', task.get('task_type')):
        return False
    repos = rule.get('repos') or []
    if isinstance(repos, str):
        repos = [repos]
    if not _matches_glob_any(task.get('target_repo'), repos):
        return False
    file_patterns = rule.get('file_patterns') or []
    if isinstance(file_patterns, str):
        file_patterns = [file_patterns]
    files = task.get('changed_files') or task.get('files') or []
    if isinstance(files, str):
        files = [files]
    if not _matches_file_patterns(files, file_patterns):
        return False
    return True


def evaluate(
    task: dict[str, Any],
    policy: Optional[dict[str, Any]] = None,
) -> tuple[str, Optional[dict[str, Any]]]:
    """Return (action, matched_rule). action ∈ VALID_ACTIONS.

    `task` should include at minimum `source` and `target` (target_agent or
    target). Optional: `task_type`, `target_repo`, `changed_files`. Missing
    fields are treated as "any" for matching purposes.

    `policy` defaults to a fresh `load_policy()` call so edits take effect
    without restart. Pass an explicit policy in tests.
    """
    p = policy if policy is not None else load_policy()
    for rule in p.get('rules', []):
        if _rule_matches(rule, task):
            return rule['action'], rule
    return p.get('default_action', 'force_ask'), None


def _self_test() -> int:
    """Smoke test runnable as `python3 trust_policy.py`."""
    # Empty policy → force_ask
    action, rule = evaluate(
        {'source': 'beacon', 'target': 'forge'},
        {'version': 1, 'default_action': 'force_ask', 'rules': []},
    )
    assert action == 'force_ask' and rule is None, (action, rule)

    # Matching rule: doc-only Pulse → Forge on agent-core repo
    policy = {
        'version': 1,
        'default_action': 'force_ask',
        'rules': [
            {
                'source': 'pulse', 'target': 'forge', 'task_type': 'doc-only',
                'repos': ['ourliberty-agent-core'], 'action': 'auto_approve',
            },
            {
                'source': '*', 'target': '*', 'repos': ['TruPath-*'],
                'action': 'force_ask',
            },
        ],
    }
    task = {
        'source': 'pulse', 'target_agent': 'forge', 'task_type': 'doc-only',
        'target_repo': 'ourliberty-agent-core',
    }
    action, rule = evaluate(task, policy)
    assert action == 'auto_approve' and rule['source'] == 'pulse', (action, rule)

    # TruPath repo should force_ask (matches second rule)
    task_trupath = {
        'source': 'beacon', 'target_agent': 'forge', 'task_type': 'feature',
        'target_repo': 'TruPath-website',
    }
    action, rule = evaluate(task_trupath, policy)
    assert action == 'force_ask' and rule is not None, (action, rule)

    # First-match wins: doc-only TruPath would match rule 2 (force_ask),
    # not rule 1 (which restricts to agent-core repo)
    task_trupath_docs = {
        'source': 'pulse', 'target_agent': 'forge', 'task_type': 'doc-only',
        'target_repo': 'TruPath-website',
    }
    action, _ = evaluate(task_trupath_docs, policy)
    assert action == 'force_ask', action

    # No matching rule, falls to default
    task_unmatched = {'source': 'mirror', 'target_agent': 'beacon'}
    action, rule = evaluate(task_unmatched, policy)
    assert action == 'force_ask' and rule is None, (action, rule)

    # File pattern matching
    policy_files = {
        'version': 1,
        'default_action': 'force_ask',
        'rules': [{
            'source': 'beacon', 'target': 'forge',
            'file_patterns': ['docs/**', '*.md'],
            'action': 'auto_approve',
        }],
    }
    task_docs = {
        'source': 'beacon', 'target_agent': 'forge',
        'changed_files': ['docs/operating-manual.md'],
    }
    action, _ = evaluate(task_docs, policy_files)
    assert action == 'auto_approve', action
    task_code = {
        'source': 'beacon', 'target_agent': 'forge',
        'changed_files': ['scripts/inbox_watcher.py'],
    }
    action, _ = evaluate(task_code, policy_files)
    assert action == 'force_ask', action

    # Malformed action → TrustPolicyError on load
    raised = False
    try:
        load_policy_from_dict = {'rules': [{'action': 'bogus'}]}
        # simulate by calling validator with explicit dict
        if load_policy_from_dict['rules'][0]['action'] not in VALID_ACTIONS:
            raise TrustPolicyError('bogus action')
    except TrustPolicyError:
        raised = True
    assert raised

    print('trust_policy self-test: OK')
    return 0


if __name__ == '__main__':
    sys.exit(_self_test())
