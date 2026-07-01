#!/usr/bin/env python3
"""decision_identity.py — the one join key every needs-Larry store shares.

Spec: docs/approval-sync-phase2-spec.md §1.1 / Change A. The same logical
"needs-Larry" item is authored into four stores (P pending-approvals,
C chain_events, E for-larry-escalations, A larry-alerts) under different id
shapes — a wrapped ``mirror-review-…`` id, a bare ``pr-<repo>-<num>``
coordinate, a raw ``task_id``, or only a ``pr_url``. Without a shared identity,
resolving the item in one store can't fan out to the others.

``canonical_decision_key`` normalizes any of those representations to ONE
string so the resolve fan-out (decision_resolve.resolve_decision) can match
the same item across all four stores. It is a PURE function — no I/O, no env,
cheap to import on a cold read path.

Precedence (deliberate): a derivable PR coordinate from ``pr_url`` wins over the
``task_id`` form. The live mirror-review records key their ``id`` as
``mirror-review:<stem>`` (a colon, not the dash the wrapper-prefix list strips)
but carry a ``pr_url`` — so the PR coordinate is the only stable cross-surface
identity that lets a PR-keyed resolve (from Telegram or the dashboard) clear the
escalation record. A coordinate-shaped ``task_id`` with no ``pr_url`` is
returned verbatim, so the wrapped/bare/url forms of the SAME PR all collapse to
``pr-<repo>-<num>`` (spec §3).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Reuse the Phase 1 wrapper-prefix list rather than re-listing it (spec §1.1):
# the same strip set that task_terminal_state.expand_variants applies, so the
# canonical key and the terminal-state probe agree on what a "wrapper" is.
from task_terminal_state import _STRIP_PREFIXES  # noqa: E402

# A GitHub PR URL → its (repo_short, number). No lightweight shared parser
# exists (dashboard_api._parse_pr_url is the heavyweight web-module sibling and
# would pull a circular/expensive import into this pure helper), so the URL
# grammar lives here. The coordinate it builds, ``pr-<repo>-<num>``, matches the
# canonical ``heal_pipeline_stall._PR_TASK_ID_RE`` shape by construction.
_PR_URL_RE = re.compile(r'github\.com/[^/\s]+/([^/\s]+)/pull/(\d+)')


def _coord_from_pr_url(pr_url: Optional[str]) -> Optional[str]:
    """``https://github.com/<owner>/<repo>/pull/<n>`` → ``pr-<repo>-<n>``."""
    if not isinstance(pr_url, str):
        return None
    m = _PR_URL_RE.search(pr_url)
    if not m:
        return None
    return f'pr-{m.group(1)}-{m.group(2)}'


def _normalize_task_id(task_id: str) -> Optional[str]:
    """Strip ONE known wrapper prefix (conditional on a non-empty stem so a bare
    prefix never collapses to an over-matching ``''``); otherwise return the id
    verbatim. Mirrors task_terminal_state.expand_variants' strip discipline."""
    s = task_id.strip()
    if not s:
        return None
    for prefix in _STRIP_PREFIXES:
        if s.startswith(prefix):
            stem = s[len(prefix):]
            if stem:
                return stem
            break
    return s


def canonical_decision_key(
    task_id: Optional[str],
    pr_url: Optional[str] = None,
) -> Optional[str]:
    """The single join key across the P/C/E/A needs-Larry stores.

    Returns ``pr-<repo>-<num>`` when a PR coordinate is derivable from
    ``pr_url`` (the most stable cross-surface identity); else the
    wrapper-prefix-stripped ``task_id``; else ``None`` when neither a task_id
    nor a derivable PR coordinate exists (caller falls back to today's
    behavior — no key, no cross-store join). Pure; no I/O.
    """
    coord = _coord_from_pr_url(pr_url)
    if coord:
        return coord
    if task_id:
        return _normalize_task_id(task_id)
    return None
