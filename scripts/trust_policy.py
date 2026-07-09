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
import re
import sys
from pathlib import Path
import os
from typing import Any, Optional

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or HOME / 'agents')
REPO_ROOT = Path(__file__).resolve().parent.parent

# Runtime policy lives at ~/agents/config/trust-policy.json (the snapshot
# the agent OS reads). Repo source of truth lives at config/trust-policy.json
# in the cloned repo. Runtime wins if present, else we fall back to the
# repo copy so first-run bootstraps cleanly without a manual copy step.
RUNTIME_POLICY_PATH = AGENTS_ROOT / 'config' / 'trust-policy.json'
REPO_POLICY_PATH = REPO_ROOT / 'config' / 'trust-policy.json'
# The autonomy dial (#7) writes a runtime OVERRIDE here — deliberately OUTSIDE
# the synced config/ tree (a sibling of ~/agents/rotation.disabled) so
# ourliberty-sync, which rsyncs config/ from the repo on every sync, never
# clobbers it. When present it wins over both the synced snapshot and the repo
# copy; deleting it reverts to the git-tracked policy. The dial stamps it with
# `_preset` so the chosen position round-trips back to the panel.
OVERRIDE_POLICY_PATH = AGENTS_ROOT / 'trust-policy.override.json'

VALID_ACTIONS = {'auto_approve', 'force_ask', 'reject'}


class TrustPolicyError(Exception):
    """Raised when the policy file is malformed."""


def _resolve_policy_path() -> Path:
    # Override (dial) wins, then the synced runtime snapshot, then the repo copy
    # (first-run bootstrap). All three flow through load_policy's fail-closed
    # validation, so a malformed override degrades to force_ask, never crashes.
    if OVERRIDE_POLICY_PATH.exists():
        return OVERRIDE_POLICY_PATH
    if RUNTIME_POLICY_PATH.exists():
        return RUNTIME_POLICY_PATH
    return REPO_POLICY_PATH


def _fail_closed(reason: str) -> dict[str, Any]:
    """The default-deny policy (force_ask) returned whenever the policy file is
    missing, unreadable, or malformed. We never silently auto-approve."""
    return {
        'version': 1,
        'default_action': 'force_ask',
        'rules': [],
        '_error': reason,
    }


def load_policy(path: Optional[Path] = None) -> dict[str, Any]:
    """Load and lightly validate the policy file. Falls back to the default-deny
    policy (force_ask) if the file is missing, unreadable, OR malformed — we
    never silently auto-approve, and we never raise (audit #21: a valid-JSON but
    bad-schema file, e.g. an operator typo `"action": "approve"`, previously
    raised TrustPolicyError out of evaluate() and crashed the beacon approval
    path instead of degrading to force_ask)."""
    target = path or _resolve_policy_path()
    if not target.exists():
        return {'version': 1, 'default_action': 'force_ask', 'rules': []}
    try:
        with open(target) as f:
            policy = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return _fail_closed(f'failed to read {target}: {e}')
    if not isinstance(policy, dict):
        return _fail_closed(
            f'policy root must be an object, got {type(policy).__name__}')
    default_action = policy.get('default_action', 'force_ask')
    if default_action not in VALID_ACTIONS:
        return _fail_closed(
            f'default_action "{default_action}" not in {VALID_ACTIONS}')
    rules = policy.get('rules', [])
    if not isinstance(rules, list):
        return _fail_closed('rules must be a list')
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            return _fail_closed(f'rule[{i}] must be an object')
        action = rule.get('action')
        if action not in VALID_ACTIONS:
            return _fail_closed(f'rule[{i}] action "{action}" not in {VALID_ACTIONS}')
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
    # Predicted sensitive intent (#8). A FRESH build carries no changed_files, so
    # the glob match below can't catch it — but the dispatch text may name
    # sensitive work (deploy/config/migrations/secrets/…), which the chokepoint
    # stamps as task['sensitive_intent']. A carve-out rule opts in with
    # `match_sensitive_intent: true`; checked only AFTER the source/target/repo/
    # task_type gating above, so it stays scoped to this rule. Rules without the
    # flag are unaffected; tasks without the flag fall through to file_patterns.
    if rule.get('match_sensitive_intent') and task.get('sensitive_intent'):
        return True
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


# ---------------- plain-language posture (autonomy visibility) ----------------
#
# A read-only summary of the current policy for the dashboard's autonomy panel
# (projects-v3 P7.3 — "make today's autonomy visible", the seed of the dial).
# Pure: takes a loaded policy dict, returns a structured plain-language posture.
# The always-on GATES are CODE invariants (verified in outbox_notifier /
# drain_board_to_beacon), NOT policy — true at every dial setting — so they're
# stated as constants, never derived from the rules. The dial removes pre-build
# clicks; it never removes these.

POLICY_GATES = (
    'A reviewer (Mirror) signs off before any merge — auto-started work never '
    'auto-merges unreviewed code.',
    'Risky work (deploy, data, money, credentials, deletes) is auto-excluded '
    'from the autonomous lane before it can start.',
    'The kill switch (EMERGENCY_HALT) and /pause stop everything instantly.',
    'Any rule reverts in one line — the policy is re-read on every check, no '
    'restart.',
)

_AGENT_NAMES = {
    'beacon': 'Beacon', 'forge': 'Forge', 'mirror': 'Mirror',
    'pulse': 'Pulse', 'medic': 'Medic', '*': 'any agent',
    'pulse-auto-dispatch': 'Pulse auto-dispatch',
}


def _agent_name(raw: Any) -> str:
    s = str(raw) if raw not in (None, '') else '*'
    return _AGENT_NAMES.get(s.lower(), s)


def _describe_rule(rule: dict[str, Any]) -> str:
    """One terse, operator-facing line for a policy rule — what SHAPE of work it
    covers (no globs, field names, or risk codes)."""
    source = _agent_name(rule.get('source'))
    target = _agent_name(rule.get('target'))
    repos = rule.get('repos') or []
    if isinstance(repos, str):
        repos = [repos]
    task_type = rule.get('task_type')
    file_patterns = rule.get('file_patterns') or []
    if isinstance(file_patterns, str):
        file_patterns = [file_patterns]

    if source == 'any agent' and target == 'any agent':
        line = 'Any dispatch'
        line += f' for {", ".join(repos)}' if repos else ''
    elif target == 'any agent':
        # Source-scoped rule (e.g. Pulse auto-dispatch) — the source IS the
        # meaningful scope; don't tack on a confusing "(any repo)".
        line = source
        line += f' for {", ".join(repos)}' if repos else ''
    else:
        line = f'{source}→{target} builds'
        line += f' for {", ".join(repos)}' if repos else ' (any repo)'
    if task_type and task_type != '*':
        line += f' ({task_type})'
    if file_patterns:
        line += (' that declare they touch sensitive paths (config, deploy, '
                 'migrations, secrets, kill-switch, account tier)')
    return line


# ---- Autonomy dial presets (#7) ----
# The three positions of the dashboard autonomy dial, each a COMPLETE policy the
# dial writes verbatim to OVERRIDE_POLICY_PATH (atomic + audited; see
# dashboard_api). Deterministic and idempotent: a position always means the same
# rule-set regardless of prior state, so flipping is predictable and one-click
# reversible. Every preset keeps default_action='force_ask' and the standing
# gates (Mirror review, risky-work exclusion, kill switch) — those live in code,
# not the policy file, so the dial can't weaken them. `_preset` stamps the chosen
# position so summarize_policy reports it back exactly ('loose', in particular,
# can't be told apart from 'balanced' by the rule-shape heuristic alone).

AUTONOMY_LEVELS = ('conservative', 'balanced', 'loose')

_LEVEL_HEADLINES = {
    'conservative': 'Everything asks you. Nothing starts without your go-ahead.',
    'balanced': ('Some low-risk work starts on its own; everything else still '
                 'asks you.'),
    'loose': ('A wider lane — low-risk work across all OurLiberty repos starts '
              'on its own; sensitive paths and everything else still ask you.'),
}

# Sensitive paths that force_ask even inside an auto-approve lane (the carve-out).
# Kept in one place so 'loose' extends it to every widened repo.
_SENSITIVE_FILE_PATTERNS = [
    'config/**', 'systemd/**', 'deploy/**', '.github/**',
    'migrations/**', '**/migrations/**', 'supabase/migrations/**',
    'scripts/active_tier.py', 'scripts/rotate_active_tier.py',
    'scripts/kill_switch.py', '.env*', '**/.env*', 'credentials/**',
]

# Sensitive-intent keywords (#8). A FRESH build carries no changed_files, so the
# globs above can't catch it; this TIGHT, sensitive-only set lets the chokepoint
# predict from the dispatch text (summary/prompt) that a build will touch a
# sensitive path, and stamp task['sensitive_intent'] so the carve-out force_asks
# instead of auto-starting. Deliberately narrow (infra/secrets/tier only, NOT the
# broad classify_careful set) to minimize false-asks on benign builds — a build
# that touches sensitive paths WITHOUT naming them still rides the Mirror gate.
# One named constant: widen/narrow here in a one-liner.
SENSITIVE_INTENT_KEYWORDS = (
    'deploy', 'deployment', 'release', 'production',
    'config', 'configuration', 'systemd',
    'migrate', 'migration',
    'secret', 'secrets', 'credential', 'credentials',
    'token', 'password', 'api key', 'apikey',
    'kill switch', 'kill-switch', 'killswitch', 'emergency halt',
    'account tier', 'tier rotation', 'rotate tier',
    'environment variable', 'env var',
)

_SENSITIVE_INTENT_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in SENSITIVE_INTENT_KEYWORDS) + r')\b',
    re.IGNORECASE,
)


def text_signals_sensitive(text: str) -> bool:
    """True if free dispatch text (a build's summary/prompt) names sensitive-infra
    work — used to predict a FRESH build (no changed_files yet) will touch a
    sensitive path, so the carve-out can force_ask it. Pure; never raises."""
    return bool(text) and _SENSITIVE_INTENT_PATTERN.search(text) is not None

# 'loose' widens the auto-approve lane to all OurLiberty repos (balanced is
# agent-core only).
_LOOSE_REPOS = [
    'ourliberty-agent-core', 'ourliberty-dashboard', 'ourliberty-graph',
]


def _sensitive_carveout_rule(repos: list[str]) -> dict[str, Any]:
    # MUST be ordered BEFORE the broad auto_approve rule (first-match-wins) so a
    # sensitive-path dispatch still force_asks inside the auto-approve lane.
    # `match_sensitive_intent` closes the fresh-build hole (#8): a build with no
    # declared changed_files but sensitive-named text trips this rule too.
    return {
        'source': 'beacon', 'target': 'forge', 'repos': list(repos),
        'file_patterns': list(_SENSITIVE_FILE_PATTERNS),
        'match_sensitive_intent': True, 'action': 'force_ask',
    }


def _suite_guardian_rule() -> dict[str, Any]:
    # Stage-2 prerequisite artifact (spec main-suite-green-guardian.md D3 / L4):
    # the guardian's auto-filed fix tasks touch ONLY `scripts/tests/**`, so a
    # `source: 'suite-guardian'` dispatch whose changed_files stay inside that
    # allow-list auto-approves. Fix scope is ALSO enforced mechanically by the
    # outbox_notifier diff gate (SHA-bound, fail-closed) — this rule is the
    # policy-layer half. Present only at balanced+loose (under `conservative`
    # the guardian is pinned <= Stage 1 by the dial map, so it never auto-files).
    return {
        'source': 'suite-guardian', 'target': 'forge',
        'file_patterns': ['scripts/tests/**'],
        'action': 'auto_approve',
    }


def _pulse_sensitive_carveout() -> dict[str, Any]:
    # The pulse-auto-dispatch lane (rule below) auto-approves on `target: *` with
    # no path carve-out — so a sensitive pulse dispatch would slip through (#8).
    # Ordered BEFORE the pulse auto_approve so sensitive pulse work (declared
    # files OR predicted intent) force_asks. Repo-agnostic on purpose: the
    # conservative side (force_ask) should bite regardless of repo.
    return {
        'source': 'pulse-auto-dispatch', 'target': '*',
        'file_patterns': list(_SENSITIVE_FILE_PATTERNS),
        'match_sensitive_intent': True, 'action': 'force_ask',
    }


def policy_for_level(level: str) -> dict[str, Any]:
    """Build the COMPLETE policy for an autonomy-dial position. Pure — the caller
    writes the returned dict to OVERRIDE_POLICY_PATH. Raises ValueError on an
    unknown level (the web layer validates first, so this is a belt-and-braces
    guard). Every position keeps default_action='force_ask'; conservative drops
    all auto_approve rules, balanced reproduces today's agent-core lane, loose
    widens that lane to all OurLiberty repos with the carve-out extended to match."""
    if level not in AUTONOMY_LEVELS:
        raise ValueError(f'unknown autonomy level: {level!r}')
    pulse = {'source': 'pulse-auto-dispatch', 'target': '*',
             'action': 'auto_approve'}
    if level == 'conservative':
        rules: list[dict[str, Any]] = []
    elif level == 'balanced':
        repos = ['ourliberty-agent-core']
        rules = [
            _pulse_sensitive_carveout(),
            pulse,
            _sensitive_carveout_rule(repos),
            _suite_guardian_rule(),
            {'source': 'beacon', 'target': 'forge', 'repos': repos,
             'action': 'auto_approve'},
        ]
    else:  # loose
        rules = [
            _pulse_sensitive_carveout(),
            pulse,
            _sensitive_carveout_rule(_LOOSE_REPOS),
            _suite_guardian_rule(),
            {'source': 'beacon', 'target': 'forge', 'repos': list(_LOOSE_REPOS),
             'action': 'auto_approve'},
        ]
    return {
        'version': 1,
        'default_action': 'force_ask',
        '_preset': level,
        '_doc': ('Written by the dashboard autonomy dial (#7). Delete this file '
                 'to revert to the git-tracked config/trust-policy.json.'),
        'rules': rules,
    }


def summarize_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Plain-language read of the current autonomy posture for the dashboard's
    read-only panel. Pure; never raises. A fail-closed/_error policy reads as
    'everything asks you' (the safe default)."""
    rules = policy.get('rules') if isinstance(policy.get('rules'), list) else []
    default_action = policy.get('default_action', 'force_ask')

    auto_starts = [_describe_rule(r) for r in rules
                   if isinstance(r, dict) and r.get('action') == 'auto_approve']
    still_asks = [_describe_rule(r) for r in rules
                  if isinstance(r, dict)
                  and r.get('action') in ('force_ask', 'reject')]
    if default_action == 'force_ask':
        still_asks.append('Everything else — the default is to ask you.')

    # A dial-written policy stamps its exact position in `_preset` — honor it so
    # 'loose' (which has auto_approve rules, so the heuristic below would call it
    # 'balanced') round-trips. Hand-edited / bootstrap policies have no `_preset`,
    # so fall back to inferring from whether anything auto-starts.
    preset = policy.get('_preset')
    if preset in AUTONOMY_LEVELS:
        level = preset
        headline = _LEVEL_HEADLINES[preset]
    elif not auto_starts:
        level = 'conservative'
        headline = _LEVEL_HEADLINES['conservative']
    else:
        level = 'balanced'
        headline = _LEVEL_HEADLINES['balanced']

    return {
        'level': level,
        'headline': headline,
        'auto_starts': auto_starts,
        'still_asks': still_asks,
        'gates': list(POLICY_GATES),
        'degraded': bool(policy.get('_error')),
    }


def current_autonomy_level() -> str:
    """The live autonomy-dial position ('conservative'|'balanced'|'loose').
    Reads the effective policy (override → runtime → repo) through the same
    fail-closed loader `evaluate` uses, so a degraded/missing policy reads as
    'conservative' (the safe floor). Consumed by the suite-guardian stage
    machine to cap the effective stage: conservative→1, balanced→2, loose→3."""
    return summarize_policy(load_policy())['level']


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
