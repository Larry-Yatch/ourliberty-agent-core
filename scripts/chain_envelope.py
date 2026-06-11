#!/usr/bin/env python3
"""chain_envelope.py — the single sanctioned constructor for dispatch/notify
envelopes in the Beacon→Forge→Mirror automation chain.

Spec: ``agents/beacon/specs/chain-context-durability.md`` §4 (M1), §5 (S1).

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

from typing import Any, Mapping, Optional


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


def build_chain_envelope(
    base: Mapping[str, Any],
    source: Optional[Mapping[str, Any]],
    *,
    carry: Mapping[str, Any],
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

    env: dict[str, Any] = dict(base)
    src = source or {}
    for field, guard in CHAIN_CONTEXT_FIELDS.items():
        decision = carry[field]
        if decision is DROP:
            continue
        value = src.get(field) if decision is CARRY else decision
        if _passes_guard(value, guard):
            env[field] = value

    return env
