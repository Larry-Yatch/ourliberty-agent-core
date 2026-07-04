#!/usr/bin/env python3
"""prod_write_guard.py — refuse operator-state writes to the REAL ~/agents
tree while a pytest run is active.

WHY THIS EXISTS
---------------
The two operator-facing state files —
``~/agents/state/beacon-pending-approvals.json`` (the Approvals tab / doorbell,
written by ``beacon_approval_handler.save_state``) and
``~/agents/blackboard/for-larry-escalations.json`` (the "Waiting on You" panel,
written by ``for_larry_escalations._save``) — resolve their production path from
``OURLIBERTY_AGENTS_ROOT`` AT IMPORT. A test that imports either module (freezing
the real prod path) and then reaches the write path WITHOUT isolating it (no
``OURLIBERTY_AGENTS_ROOT``=<tmp>, no monkeypatch of the module path constant)
writes fixture rows straight into live operator state. That is exactly the
2026-07-02 leak: ghost approvals with fixture task_ids (``real-rev``, ``t1``)
and a placeholder ``chat_id=12345`` appearing in the live doorbell.

This guard makes that failure LOUD at test time instead of silent in prod: at
each writer's chokepoint, just before the atomic write, if a pytest run is
active AND the resolved target is inside the real ~/agents tree, raise. A
correctly-isolated test targets a tmp dir OUTSIDE that tree, so it never trips.

Inert in production: pytest sets ``PYTEST_CURRENT_TEST`` during test execution
and it is unset in the live daemons, so ``guard_no_prod_write_under_test`` is a
no-op there — zero behavior change for the running bot.

Scope is deliberately the two files that actually leaked; this is NOT wired into
the generic ``atomic_io`` writer (that would guard every state file in the repo).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Union


def guard_no_prod_write_under_test(path: Union[str, Path]) -> None:
    """Raise ``RuntimeError`` if a pytest run would write ``path`` into the real
    ``~/agents`` production tree.

    No-op unless ``PYTEST_CURRENT_TEST`` is set (i.e. only during pytest test
    execution). ``Path.home()`` honors ``$HOME``; the guard compares against the
    real invoking user's home, which the test bootstrap deliberately does NOT
    swap, so ``~/agents`` resolves to the live tree while an isolated test's tmp
    target does not.
    """
    if not os.environ.get('PYTEST_CURRENT_TEST'):
        return

    prod_root = (Path.home() / 'agents').resolve()
    # strict=False (default): the target file may not exist yet — resolve the
    # path lexically/symlink-wise anyway so we compare real locations.
    target = Path(path).resolve()

    if target == prod_root or target.is_relative_to(prod_root):
        raise RuntimeError(
            'prod-write-guard: refusing to write operator state to the real '
            f'production tree under pytest: {target}\n'
            'A test reached this write without isolating the path. Fix it: set '
            'OURLIBERTY_AGENTS_ROOT to a temp dir or monkeypatch the module '
            'state path before writing under pytest.'
        )
