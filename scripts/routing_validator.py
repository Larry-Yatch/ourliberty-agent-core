#!/usr/bin/env python3
"""routing_validator.py — role-boundary enforcement for inbox writes (Phase D3, Gap 3).

Two-layer check:

1. **Hard topology** — `(source, target)` pairs are explicitly allow-listed.
   Fresh dispatches (Pulse → Beacon, Beacon → Forge, etc.) must be in the
   FRESH_DISPATCH_ROUTES table. Dialogue-leg suffixes (`-result`,
   `-clarification`, `-answer`) bypass this check — their original direction
   was already validated. System sources (`telegram-webhook`, `cron`, etc.)
   can target any agent. Self-dispatch is always denied.

2. **Soft reroute via IDENTITY.md** — adapted from upstream's pattern. If a
   target agent's IDENTITY.md declares a `## Routing Constraints` section,
   we honor `accepts_from_user`, `accepts_task_types`, and `escalation_target`.
   Failure here REROUTES rather than rejects.

Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core
(2026-05-11, Phase D3-prep — Gap 3). Upstream's validator was purely the soft
layer; the hard topology is a Larry-shaped addition to enforce the 4-agent
role boundaries the audit identified.

Usage (library mode):

    from routing_validator import RoutingDenied, validate_route
    try:
        final_target, was_rerouted, reason = validate_route(target, task)
    except RoutingDenied as e:
        # Hard topology violation — refuse the write.
        ...
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Optional

# Repo root is the parent of scripts/. Identity files live at
# `<repo_root>/agents/<agent_id>/IDENTITY.md`.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Hard topology — allowed (source, target) pairs for FRESH dispatches.
# A "fresh dispatch" is a new task initiated by an agent toward another agent,
# as distinct from a reply to an existing dialogue (which carries a -result /
# -clarification / -answer suffix on the source field).
#
# Mirror's outgoing route is wired now so D3.5 only adds review behavior, not
# routes. Forge → Beacon covers both clarification questions (D3) and
# result-shaped acks; the `intent` field on the task envelope distinguishes.
FRESH_DISPATCH_ROUTES: dict[str, set[str]] = {
    'pulse': {'beacon'},
    # PR-S4 rectification (H4): `build_sequence_advancer` added as a
    # permitted Beacon target so the chat-mode kickoff path
    # (`approve sequence <seq-id>` → bot → `dispatch_approved` →
    # `safe_write_inbox` → `validate_task`) doesn't reject the kickoff
    # envelope on routing topology. The advancer's inbox is drained by
    # the outbox-notifier's kickoff handler (status transition only); no
    # Claude session is spawned for the advancer — it's a daemon, not an
    # agent, but a registered IDENTITY.md keeps the convention symmetric.
    'beacon': {'pulse', 'forge', 'mirror', 'build_sequence_advancer'},
    'forge': {'beacon'},
    # D3.5 commit 5a: mirror → forge added as forward-compat for 5b's revision
    # loop. 5a itself doesn't dispatch on this route (REVIEW_REVISION just
    # journals to Beacon in 5a); 5b lights it up when the notifier writes
    # revision-tasks to Forge's inbox in response to Mirror's REVIEW_REVISION.
    'mirror': {'beacon', 'forge'},
    'larry': {'beacon', 'forge', 'mirror', 'pulse'},
    # Clarification protocol forward legs (Phase D3). A `<agent>-question`
    # source is a structured ask-before-build query. Reply legs use the
    # `-clarification` suffix which is in DIALOGUE_SUFFIXES below and
    # bypasses this table.
    'forge-question': {'beacon'},
    'mirror-question': {'beacon'},
    # Closed-loop step 5 (2026-05-24). Pulse Check I auto-dispatches small,
    # dollar-quantified proposals to Beacon's inbox under a distinct source
    # so policy / extraction can carve out per-source rules (step 4 wired
    # the Beacon-outbox extraction path keyed on this exact source).
    'pulse-auto-dispatch': {'beacon'},
    # E4.4 dashboard UI Approve/Reject actions route to Beacon's inbox.
    # Layer-1 source validation (dispatch_validator.ALLOWED_SOURCES) gained
    # 'dashboard' on 2026-05-28, but this layer-2 routing table was never
    # given the matching entry — so every dashboard action envelope passed
    # source validation, then died here with 'source "dashboard" has no
    # allowed routes' and was silently dropped to beacon/.invalid while the
    # API returned 200 (two of Larry's rejections lost this way on
    # 2026-06-10). The test_dispatch_route_parity regression guard now fails
    # if a future ALLOWED_SOURCES addition forgets its route the same way.
    'dashboard': {'beacon'},
}

# Source suffixes that mark dialogue-leg messages. The notifier writes these
# when routing an outbox back to its originator; they bypass FRESH_DISPATCH_ROUTES
# because the original outbound direction was already validated.
DIALOGUE_SUFFIXES = ('-result', '-clarification', '-answer')

# Infrastructure / system sources that may target any agent. These are not
# subject to the hard topology check.
SYSTEM_SOURCES = {
    'telegram-webhook', 'webhook', 'system-test', 'parallel-test',
    'cron', 'continuation', 'orchestrator', 'watchdog', 'auto-retry',
    'auto-iterate', 'system-sweep', 'cycle-recovery', 'ship_completion_watcher',
    'backlog-promoter', 'blackboard', 'unknown',
    # D3-forge (commit 4a): notifier-originated marker-error dispatches.
    'outbox-notifier',
}

# IDENTITY cache for soft-reroute parsing.
IDENTITY_CACHE: dict[str, dict[str, Any]] = {}
_IDENTITY_LOCK = threading.Lock()

USER_DM_SOURCES = {'telegram-webhook'}
DEFAULT_ESCALATION_TARGET = 'beacon'

# Pragmatic task-type classifier — adapted from upstream with terms relevant
# to our 4-agent topology. Used only when an agent's IDENTITY declares an
# `accepts_task_types` allow-list.
_TASK_TYPE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ('observation', ('observe', 'cycle', 'health check', 'pattern', 'journal',
                       'investigate')),
    ('feature-development', ('feature', 'implement', 'build', 'migration',
                              'refactor', 'pr #', 'fix pr', 'bug fix', 'patch')),
    ('code-review', ('review', 'critique', 'adversarial', 'audit', 'diff')),
    ('strategy', ('plan', 'design', 'architecture', 'roadmap', 'priorities')),
    ('doc-only', ('docs', 'documentation', 'readme', 'comment', 'operating manual')),
]


class RoutingDenied(Exception):
    """Raised when hard-topology check denies a (source, target) pair."""


def _identity_path(agent_id: str) -> Path:
    return REPO_ROOT / 'agents' / agent_id / 'IDENTITY.md'


def _parse_constraints_block(text: str) -> dict[str, Any]:
    """Parse the `## Routing Constraints` section of an IDENTITY.md.

    Supported keys:

      accepts_from_user: <bool>
      accepts_task_types: [type1, type2]
      escalation_target: <agent_id>

    Missing keys mean "no constraint" — validator fails open per key.
    """
    constraints: dict[str, Any] = {}
    header_re = re.compile(r'^##\s+Routing\s+Constraints\s*$', re.MULTILINE)
    m = header_re.search(text)
    if not m:
        return constraints
    start = m.end()
    next_h = re.search(r'^##\s+', text[start:], re.MULTILINE)
    section = text[start:start + next_h.start()] if next_h else text[start:]

    afu = re.search(r'accepts_from_user\s*:\s*(true|false)', section, re.IGNORECASE)
    if afu:
        constraints['accepts_from_user'] = (afu.group(1).lower() == 'true')

    att = re.search(r'accepts_task_types\s*:\s*\[?([^\]\n]+)\]?', section, re.IGNORECASE)
    if att:
        raw = att.group(1).strip().strip('`').strip()
        types = [t.strip() for t in raw.split(',') if t.strip()]
        if types:
            constraints['accepts_task_types'] = types

    esc = re.search(r'escalation_target\s*:\s*([a-z0-9_\-]+)', section, re.IGNORECASE)
    if esc:
        constraints['escalation_target'] = esc.group(1).strip().lower()

    return constraints


def load_constraints(agent_id: str) -> dict[str, Any]:
    """Return parsed routing constraints for an agent, cached."""
    with _IDENTITY_LOCK:
        cached = IDENTITY_CACHE.get(agent_id)
        if cached is not None:
            return cached
        p = _identity_path(agent_id)
        try:
            text = p.read_text()
        except Exception:
            IDENTITY_CACHE[agent_id] = {}
            return {}
        parsed = _parse_constraints_block(text)
        IDENTITY_CACHE[agent_id] = parsed
        return parsed


def invalidate_cache(agent_id: Optional[str] = None) -> None:
    """Drop cached constraints. Tests use this between runs."""
    with _IDENTITY_LOCK:
        if agent_id is None:
            IDENTITY_CACHE.clear()
        else:
            IDENTITY_CACHE.pop(agent_id, None)


# ---- Repo-scope check (Phase D3 commit 4b — allowed_repos enforcement) ----

# agent-models.json lives under <repo_root>/config/. We lazy-load it the
# first time check_target_repo fires and cache it; tests call
# invalidate_models_cache() between runs.
MODELS_CONFIG_PATH = REPO_ROOT / 'config' / 'agent-models.json'
_MODELS_CACHE: dict[str, Any] = {}
_MODELS_LOCK = threading.Lock()


def _load_models_config() -> dict[str, Any]:
    """Lazy-load + cache config/agent-models.json. Returns {} on any error.

    Caching is intentional — this fires on every safe_write_inbox call and
    every inbox_watcher.process_task dispatch. Tests must invalidate
    between runs.
    """
    with _MODELS_LOCK:
        if 'config' in _MODELS_CACHE:
            return _MODELS_CACHE['config']
        try:
            _MODELS_CACHE['config'] = json.loads(MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            _MODELS_CACHE['config'] = {}
        return _MODELS_CACHE['config']


def invalidate_models_cache() -> None:
    """Drop the cached agent-models.json. Tests use this between runs."""
    with _MODELS_LOCK:
        _MODELS_CACHE.pop('config', None)


def allowed_repos_for(agent_id: str) -> list[str]:
    """Return the agent's allowed_repos list. Empty if unconfigured."""
    config = _load_models_config()
    agents_block = config.get('agents', {})
    agent_block = agents_block.get(agent_id, {})
    raw = agent_block.get('allowed_repos')
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, str) and r]


def _known_repos() -> list[str]:
    """Every canonical repo name any agent is allowed to target (deduped, ordered)."""
    config = _load_models_config()
    agents_block = config.get('agents', {})
    seen: dict[str, None] = {}
    if isinstance(agents_block, dict):
        for agent_block in agents_block.values():
            if not isinstance(agent_block, dict):
                continue
            for r in agent_block.get('allowed_repos') or []:
                if isinstance(r, str) and r:
                    seen.setdefault(r, None)
    return list(seen)


def _bare_repo(name: str) -> str:
    """Strip the spellings that mean the same repo: owner prefix, worktree suffix,
    and the `ourliberty-` namespace prefix. `Larry-Yatch/ourliberty-agent-core`,
    `agent-core.wt-forge-x` and `ourliberty-agent-core` all reduce to `agent-core`."""
    bare = name.rsplit('/', 1)[-1]      # owner/name -> name
    bare = bare.split('.', 1)[0]        # worktree suffix -> base (mirrors
                                        # emit_capture_impl._normalize_repo)
    prefix = 'ourliberty-'
    return bare[len(prefix):] if bare.startswith(prefix) else bare


def canonical_repo(name: Optional[str]) -> Optional[str]:
    """Resolve a repo name to the canonical spelling used in agent-models.json.

    WHY THIS EXISTS: captures carry `origin.repo` derived from the local CHECKOUT
    DIRECTORY NAME (emit_capture_impl._normalize_repo), which differs per machine —
    the droplet checkout is `agent-core`, the desktop one is `ourliberty-agent-core`.
    Same repo, two spellings. Three consumers need the canonical form:
      * drain_board_to_beacon eligibility compares against AUTODISPATCH_REPO
      * missions_narrator maps origin.repo -> the envelope's `target_repo`
      * dashboard_api's delegate path does the same on the manual click
    and safe_write_inbox's repo-scope check compares `target_repo` against
    allowed_repos, which holds FULL names only. A droplet-emitted capture therefore
    produced `target_repo='agent-core'`, failed that scope check, and the dispatch
    was rejected — silently, since the card just stayed parked.

    Resolution is against the CONFIGURED allowlist rather than a hardcoded alias
    table, so it cannot drift as repos are onboarded.

    This normalizes SPELLING ONLY. An unrecognised or ambiguous name is returned
    unchanged, so this never invents a repo and never widens what any agent is
    authorized to target — that gate remains check_target_repo/allowed_repos.
    """
    if not name or not isinstance(name, str):
        return name
    known = _known_repos()
    if name in known:
        return name
    target = _bare_repo(name)
    matches = [r for r in known if _bare_repo(r) == target]
    # Exactly one match, or leave it alone: ambiguity must not be guessed at.
    return matches[0] if len(matches) == 1 else name


def check_target_repo(
    target_agent: str,
    target_repo: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Verify ``target_repo`` falls within ``target_agent``'s allowed_repos.

    Fails open in two cases (preserving back-compat for non-worktree agents):

      - ``target_repo`` is None or empty (most pre-D3-forge tasks).
      - The target agent has no ``allowed_repos`` configured.

    Fails closed only when ``target_repo`` is set AND the target agent's
    allow-list is configured AND ``target_repo`` is not in it.

    Returns ``(ok, reason_or_None)``. Callers raise RoutingDenied on False.
    """
    if not target_repo:
        return True, None
    allowed = allowed_repos_for(target_agent)
    if not allowed:
        return True, None
    if target_repo not in allowed:
        return False, (
            f'target_repo "{target_repo}" not in allowed_repos for '
            f'{target_agent} (allowed: {sorted(allowed)})'
        )
    return True, None


def classify_task_type(prompt: str) -> Optional[str]:
    """Best-effort classifier. Returns the first matching type or None."""
    if not prompt:
        return None
    low = prompt[:2000].lower()
    for type_name, keywords in _TASK_TYPE_KEYWORDS:
        if any(k in low for k in keywords):
            return type_name
    return None


def _is_user_dm(task: dict[str, Any]) -> bool:
    source = task.get('source', '')
    if source not in USER_DM_SOURCES:
        return False
    chat = task.get('reply_chat_id')
    if chat is None:
        return False
    try:
        return int(chat) > 0
    except (TypeError, ValueError):
        return False


def _is_dialogue_leg(source: str) -> bool:
    return any(source.endswith(suffix) for suffix in DIALOGUE_SUFFIXES)


def check_hard_topology(source: str, target: str) -> tuple[bool, Optional[str]]:
    """Return (ok, reason). Reason is populated only on denial."""
    if not source:
        return False, 'source missing'
    if not target:
        return False, 'target missing'
    if source == target:
        return False, f'self-dispatch denied ({source} -> {target})'
    if _is_dialogue_leg(source):
        return True, None
    if source in SYSTEM_SOURCES:
        return True, None
    allowed = FRESH_DISPATCH_ROUTES.get(source)
    if allowed is None:
        return False, f'source "{source}" has no allowed routes'
    if target not in allowed:
        return False, (
            f'route {source} -> {target} not allowed '
            f'(allowed from {source}: {sorted(allowed)})'
        )
    return True, None


def validate_inbox_write(
    target_agent: str,
    task: dict[str, Any],
    source_agent: Optional[str] = None,
) -> tuple[str, bool, Optional[str]]:
    """Soft-reroute layer: consults target agent's IDENTITY.md.

    Returns (final_target, was_rerouted, reason). If the target has no
    constraints, no-op. Fails open on parse error to preserve routing.
    """
    constraints = load_constraints(target_agent)
    if not constraints:
        return target_agent, False, None

    if constraints.get('accepts_from_user') is False and _is_user_dm(task):
        fallback = constraints.get('escalation_target', DEFAULT_ESCALATION_TARGET)
        return fallback, True, (
            f'accepts_from_user=false: user DM to {target_agent} '
            f'rerouted to {fallback}'
        )

    allowed_types = constraints.get('accepts_task_types')
    if allowed_types:
        prompt = task.get('prompt', '')
        classified = classify_task_type(prompt)
        if classified and classified not in allowed_types:
            fallback = constraints.get('escalation_target', DEFAULT_ESCALATION_TARGET)
            return fallback, True, (
                f'task-type={classified} not in accepts_task_types={allowed_types} '
                f'for {target_agent}; rerouted to {fallback}'
            )

    return target_agent, False, None


def validate_route(
    target_agent: str,
    task: dict[str, Any],
    source_agent: Optional[str] = None,
) -> tuple[str, bool, Optional[str]]:
    """Combined hard topology + soft reroute. Raises RoutingDenied on hard fail.

    Returns (final_target, was_rerouted, reason).
    """
    source = source_agent if source_agent is not None else task.get('source', '')
    ok, reason = check_hard_topology(source, target_agent)
    if not ok:
        raise RoutingDenied(reason or 'hard topology denied')
    return validate_inbox_write(target_agent, task, source_agent)


def _self_test() -> int:
    """Smoke test runnable as `python3 routing_validator.py`."""
    import tempfile

    # Hard topology checks
    assert check_hard_topology('pulse', 'beacon') == (True, None)
    assert check_hard_topology('beacon', 'forge') == (True, None)
    ok, reason = check_hard_topology('pulse', 'forge')
    assert not ok and 'not allowed' in reason, (ok, reason)
    ok, reason = check_hard_topology('forge', 'forge')
    assert not ok and 'self-dispatch' in reason, (ok, reason)
    # Dialogue legs bypass
    assert check_hard_topology('beacon-result', 'pulse') == (True, None)
    assert check_hard_topology('forge-clarification', 'beacon') == (True, None)
    # System sources
    assert check_hard_topology('telegram-webhook', 'beacon') == (True, None)
    assert check_hard_topology('cron', 'pulse') == (True, None)

    # Soft reroute via fake IDENTITY.md
    with tempfile.TemporaryDirectory() as td:
        global REPO_ROOT
        original_root = REPO_ROOT
        REPO_ROOT = Path(td)  # type: ignore
        try:
            p = REPO_ROOT / 'agents' / 'xyz'
            p.mkdir(parents=True)
            (p / 'IDENTITY.md').write_text(
                '# Identity\n\n## Routing Constraints\n\n'
                '- accepts_from_user: false\n'
                '- accepts_task_types: [feature-development, code-review]\n'
                '- escalation_target: beacon\n\n'
                '## Other\n'
            )
            invalidate_cache('xyz')
            c = load_constraints('xyz')
            assert c == {
                'accepts_from_user': False,
                'accepts_task_types': ['feature-development', 'code-review'],
                'escalation_target': 'beacon',
            }, c
            target, rerouted, _ = validate_inbox_write(
                'xyz',
                {'source': 'telegram-webhook', 'reply_chat_id': 12345, 'prompt': 'hi'},
            )
            assert rerouted and target == 'beacon', (target, rerouted)
            target, rerouted, _ = validate_inbox_write(
                'xyz',
                {'source': 'beacon', 'prompt': 'observe the cycle for patterns'},
            )
            assert rerouted and target == 'beacon', (target, rerouted)
        finally:
            REPO_ROOT = original_root  # type: ignore

    # Full validate_route raises on hard fail
    raised = False
    try:
        validate_route('forge', {'source': 'pulse', 'prompt': 'do work'})
    except RoutingDenied:
        raised = True
    assert raised, 'expected RoutingDenied for pulse->forge'

    print('routing_validator self-test: OK')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_self_test())
