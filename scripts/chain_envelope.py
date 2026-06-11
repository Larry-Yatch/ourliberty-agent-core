#!/usr/bin/env python3
"""chain_envelope.py — the single sanctioned constructor for dispatch/notify
envelopes in the Beacon→Forge→Mirror automation chain.

Spec: ``agents/beacon/specs/chain-context-durability.md`` §4 (M1 + M3),
§5 (S1 + S3).

Every inter-agent envelope carries *chain context* that lets the next step
fire automatically: the build session to ``--resume`` (``forge_build_session_id``),
who a fallback DMs (``reply_chat_id`` / the lineage source fields), the loop
budgets (``replan_count`` / ``max_replans`` / ``revision_count``), and the
repo/PR under work (``target_repo`` / ``pr_url``). Historically each dispatch
site in ``outbox_notifier.py`` hand-rolled its envelope and hand-copied this
context with ad-hoc ``if data.get(X): task[X] = data[X]`` lines — so each site
was an independent chance to forget a field, and every recovery/healer path
that rebuilt an envelope from external truth started from nothing. Two
production context-drops (the DAG-preflight REVISION dead-end and the PR #412
no-session REVISION) came from exactly this class of silent omission.

``build_chain_envelope`` centralizes the whitelist. It forces each caller to
resolve EVERY whitelisted context field explicitly — pass it through from the
inbound envelope (``CARRY``), give it an explicit value, or explicitly drop it
(``DROP``) — turning a silent omission into a visible, code-reviewed decision.

Scope of the carry-enforced whitelist (``CHAIN_CONTEXT_FIELDS`` below): the
drop-prone context fields the incidents were about. The remaining canonical
chain fields the spec lists are managed elsewhere by construction and are NOT
re-litigated at every call site:

  - ``source`` — set/validated by ``safe_write_inbox`` against the declared
    ``source_agent`` on every write, so it can never be silently dropped.
  - ``original_source`` / ``_notify_depth`` — task-lineage fields set as
    explicit literals in ``base`` (``original_source`` is the dispatcher
    identity, ``_notify_depth`` is computed per hop). ``routing_source`` is
    derived downstream from ``original_source``/``source`` and is never stored
    on the envelope.

Imported by ``outbox_notifier.py`` (S1), and — per the spec — by the healers
and ``inbox_watcher.py`` in later steps of the sequence.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


class _Sentinel:
    __slots__ = ('_name',)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return self._name


# Pass the field through from ``source`` (the inbound envelope), subject to the
# field's inclusion guard. Equivalent to the historical
# ``if source.get(field) <guard>: env[field] = source[field]``.
CARRY = _Sentinel('CARRY')

# Explicitly omit the field. The historical sites that simply never set a given
# context field become an explicit ``DROP`` — the omission is now a visible
# decision rather than a silent gap a future reader has to notice.
DROP = _Sentinel('DROP')


# The canonical context-field whitelist + each field's inclusion guard:
#   'truthy'   -> included iff bool(value) is True   (was ``if data.get(X):``)
#   'not_none' -> included iff value is not None     (was ``if ... is not None``)
# The guard preserves historical behavior exactly: a 0/'' never silently flips
# a truthy-guarded field on, and a meaningful 0 (``replan_count == 0`` means
# "first leg") is kept by the not_none guard. A whitelisted field that fails
# its guard is omitted entirely (key absent), so downstream ``.get(field)``
# readers are unchanged.
CHAIN_CONTEXT_FIELDS: dict[str, str] = {
    'forge_build_session_id': 'truthy',
    'reply_chat_id': 'not_none',
    'replan_count': 'not_none',
    'max_replans': 'not_none',
    'revision_count': 'not_none',
    'target_repo': 'truthy',
    'pr_url': 'truthy',
}


def _passes_guard(value: Any, guard: str) -> bool:
    if guard == 'not_none':
        return value is not None
    return bool(value)


# ---------------------------------------------------------------------------
# M3 — derivable-context backfill (spec §4 M3, §5 S3).
#
# Several dead-ends in the chain fire because a whitelisted field is *missing*,
# not *unrecoverable*: ``target_repo`` is derivable from the mission registry
# (the mission that owns the task names its repo); ``pr_url`` is derivable via
# ``gh`` (the Forge build PR's head branch is the ``forge/<task_id>``
# convention). The builder backfills these from their source of truth so a
# handler never concludes it must dead-end on a field that was recoverable all
# along. Only genuinely unrecoverable context (e.g. a reaped build session)
# falls through to M2's agent-route.
#
# Backfill is OPT-IN per call (``backfill=`` arg, default off). The resolvers
# do real I/O — a registry read, a ``gh`` shell-out — so the happy path that
# already carries the field pays nothing; only the recovery/dead-end paths that
# ask for backfill incur the lookup. Every resolver is fail-safe: it returns
# ``None`` (→ field stays absent → the genuinely-unrecoverable case) rather
# than raising, so a missing/malformed registry or a ``gh`` outage can never
# crash envelope construction.
# ---------------------------------------------------------------------------

# Subset of CHAIN_CONTEXT_FIELDS that has a source of truth to derive from.
BACKFILLABLE_FIELDS: frozenset[str] = frozenset({'target_repo', 'pr_url'})

# Env-overridable so tests redirect the registry read to a fixture without
# touching the real ``agents/beacon/missions.json``. Mirrors dashboard_api's
# ``_missions_json_path`` convention.
_MISSIONS_JSON_ENV = 'OURLIBERTY_MISSIONS_JSON'
# Owner prefix used to expand a short repo name (``ourliberty-agent-core``) into
# the ``owner/repo`` coordinates ``gh`` expects. Env-overridable for parity with
# the rest of the chain's gh helpers.
_GH_OWNER_ENV = 'OURLIBERTY_GH_OWNER'
_DEFAULT_GH_OWNER = 'Larry-Yatch'
_GH_TIMEOUT_S = 20


def _missions_json_path() -> Path:
    override = os.environ.get(_MISSIONS_JSON_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / 'agents' / 'beacon' / 'missions.json'


def backfill_target_repo(task_id: Optional[str]) -> Optional[str]:
    """Derive ``target_repo`` from the mission registry.

    Returns the ``repo`` of the mission whose ``task_ids`` contains ``task_id``,
    or ``None`` when unresolvable (no registry, malformed JSON, or no mission
    owns the task). Never raises — an unreadable registry degrades to "absent",
    which is exactly the genuinely-unrecoverable case M2 handles.
    """
    if not task_id:
        return None
    try:
        data = json.loads(_missions_json_path().read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    missions = data.get('missions') if isinstance(data, dict) else None
    if not isinstance(missions, list):
        return None
    for mission in missions:
        if not isinstance(mission, dict):
            continue
        task_ids = mission.get('task_ids')
        if isinstance(task_ids, list) and task_id in task_ids:
            repo = mission.get('repo')
            if isinstance(repo, str) and repo:
                return repo
    return None


def _full_repo(target_repo: Optional[str]) -> Optional[str]:
    """Expand a short repo name to ``owner/repo`` for ``gh``. A value that
    already carries an owner (``foo/bar``) is returned unchanged."""
    if not target_repo or not isinstance(target_repo, str):
        return None
    if '/' in target_repo:
        return target_repo
    owner = os.environ.get(_GH_OWNER_ENV) or _DEFAULT_GH_OWNER
    return f'{owner}/{target_repo}'


def _gh_pr_url_for_branch(repo_full: str, branch: str) -> Optional[str]:
    """Return the URL of the PR whose head is ``branch`` via ``gh pr list``, or
    ``None`` on any gh failure. Isolated as a mockable seam so tests exercise
    the backfill without shelling out."""
    cmd = [
        'gh', 'pr', 'list',
        '--repo', repo_full,
        '--head', branch,
        '--state', 'all',
        '--json', 'url',
        '--limit', '1',
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_GH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        rows = json.loads(proc.stdout or '[]')
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get('url'):
                return row['url']
    return None


def backfill_pr_url(
    task_id: Optional[str],
    *,
    target_repo: Optional[str] = None,
    branch: Optional[str] = None,
) -> Optional[str]:
    """Derive the Forge build PR's ``pr_url`` via ``gh``.

    The PR's head branch is the worktree convention ``forge/<task_id>`` unless
    ``branch`` is given explicitly. ``gh`` is scoped to the repo, so the lookup
    needs ``target_repo`` — passed in if the caller already resolved it, else
    derived from the mission registry. Returns ``None`` when unresolvable (no
    repo, gh outage, or no matching PR). Never raises.
    """
    if not task_id:
        return None
    repo_full = _full_repo(target_repo or backfill_target_repo(task_id))
    if not repo_full:
        return None
    head = branch or f'forge/{task_id}'
    return _gh_pr_url_for_branch(repo_full, head)


# field -> resolver(task_id, env) used by the builder's ``backfill=`` path.
# ``target_repo`` is ordered before ``pr_url`` in CHAIN_CONTEXT_FIELDS, so when
# both are requested the registry-derived repo is already on ``env`` and the
# pr_url resolver can scope its gh query to it.
_BACKFILL_RESOLVERS: dict[str, Callable[[Optional[str], Mapping[str, Any]], Optional[str]]] = {
    'target_repo': lambda task_id, env: backfill_target_repo(task_id),
    'pr_url': lambda task_id, env: backfill_pr_url(
        task_id, target_repo=env.get('target_repo'), branch=env.get('branch'),
    ),
}


def build_chain_envelope(
    base: Mapping[str, Any],
    source: Optional[Mapping[str, Any]],
    *,
    carry: Mapping[str, Any],
    backfill: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Construct a dispatch/notify envelope with chain context resolved.

    Args:
        base: the envelope's non-context fields — ``task_id`` (required),
            ``prompt``, ``source``, ``intent``/``phase``, and any site-specific
            extras. Must NOT contain a whitelisted context key; those are
            resolved through ``carry`` so the decision is explicit.
        source: the inbound envelope to pass context through from when a carry
            value is ``CARRY``. May be ``None`` — recovery paths that rebuild an
            envelope from external truth (GitHub, the sequence file) carry
            nothing through and resolve every field explicitly or to ``DROP``.
        carry: resolution for EVERY whitelisted context field. Keys must be
            exactly ``CHAIN_CONTEXT_FIELDS``. Each value is one of:
              ``CARRY`` -> ``source.get(field)``, included iff it passes guard.
              ``DROP``  -> omit (an explicit "not propagated" decision).
              value     -> use ``value``, included iff it passes the field's
                           guard (so an explicit ``None``/falsy value drops,
                           matching the historical ``if value:`` /
                           ``if value is not None:`` conditional-copy form).
        backfill: optional collection of whitelisted field names to derive from
            their source of truth (M3) when, after ``carry`` resolution, the
            field is still absent. Must be a subset of ``BACKFILLABLE_FIELDS``
            (``target_repo`` ← mission registry, ``pr_url`` ← gh). Backfill only
            fills a *missing* field — a value an explicit/``CARRY`` resolution
            already produced is never overwritten. Default ``None`` → no
            backfill (the happy path that already carries the field pays no
            I/O). Resolvers are fail-safe: an unresolvable field simply stays
            absent (the genuinely-unrecoverable case that M2 routes).

    Returns:
        A fresh ``dict`` — ``base`` plus the resolved context fields.

    Raises:
        ValueError: ``base``/``carry`` violate the contract. This is the
            forcing function that converts silent drops into visible decisions:
            a missing whitelist key, an unknown carry key (typo), a missing
            ``task_id``, or a whitelisted field set in ``base`` all raise.
    """
    if not isinstance(base, Mapping):
        raise ValueError('build_chain_envelope: base must be a mapping')
    if 'task_id' not in base:
        raise ValueError('build_chain_envelope: base must include task_id')
    if source is not None and not isinstance(source, Mapping):
        raise ValueError(
            'build_chain_envelope: source must be a mapping or None'
        )

    overlap = set(base) & set(CHAIN_CONTEXT_FIELDS)
    if overlap:
        raise ValueError(
            'build_chain_envelope: whitelisted context fields must be resolved '
            f'via carry, not set in base: {sorted(overlap)}'
        )

    carry_keys = set(carry)
    expected = set(CHAIN_CONTEXT_FIELDS)
    missing = expected - carry_keys
    unknown = carry_keys - expected
    if missing or unknown:
        raise ValueError(
            'build_chain_envelope: carry must resolve exactly the whitelist '
            f'(missing={sorted(missing)}, unknown={sorted(unknown)})'
        )

    backfill_fields = set(backfill or ())
    unknown_backfill = backfill_fields - BACKFILLABLE_FIELDS
    if unknown_backfill:
        raise ValueError(
            'build_chain_envelope: backfill must be a subset of '
            f'{sorted(BACKFILLABLE_FIELDS)} '
            f'(unknown={sorted(unknown_backfill)})'
        )

    env: dict[str, Any] = dict(base)
    src = source or {}
    for field, guard in CHAIN_CONTEXT_FIELDS.items():
        decision = carry[field]
        if decision is DROP:
            continue
        value = src.get(field) if decision is CARRY else decision
        if _passes_guard(value, guard):
            env[field] = value

    # M3 backfill: for each requested field still ABSENT after carry
    # resolution, derive it from its source of truth. Iterate the whitelist
    # (not the request set) so the order is deterministic — target_repo before
    # pr_url — letting the pr_url resolver see a just-backfilled target_repo.
    if backfill_fields:
        task_id = env.get('task_id')
        for field in CHAIN_CONTEXT_FIELDS:
            if field not in backfill_fields or field in env:
                continue
            value = _BACKFILL_RESOLVERS[field](task_id, env)
            if _passes_guard(value, CHAIN_CONTEXT_FIELDS[field]):
                env[field] = value

    return env
