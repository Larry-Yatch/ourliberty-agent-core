"""scripts/id_match.py — shared identifier-match discipline.

PR-B (2026-06-05 full-codebase audit; findings #9, #19, #26, #41).

Background: several healers/advancers correlated an id (a sequence step_id, a
task_id, a loop slug, a sanitized worktree stem) to a haystack (a merged-PR
title, a live worktree basename, an inbox filename) with a bare
``needle in haystack`` substring test. With no token boundary and no length
floor, a short or common id false-matched unrelated work:

  * ``step_id='api'`` matched a PR titled ``feat(api): unrelated change`` and
    advanced the wrong sequence step (audit #9);
  * ``task_id='b'`` matched the worktree ``wt-forge-rebuild-...`` and reported a
    live worker, so a genuinely-abandoned task was never recovered (#26);
  * a short task_id substring-matched an unrelated merged PR title and suppressed
    legitimate recovery (#19);
  * a truncated loop slug substring-matched an unrelated processed artifact and
    masked a dead loop (#41).

This helper centralizes the discipline so every call site matches the same way.
Two guards, both required:

  * **token boundary** — the needle must be delimited on both sides by a
    non-alphanumeric char or a string edge, so ``api`` matches ``feat(api): x``
    and ``wt-forge-api-001`` but never ``apiserver`` or ``rapid``.
  * **length floor** — an id shorter than ``min_len`` is too common to trust as
    a substring signal, so it does not match at all. The default mirrors the
    repo's existing ``heal_pipeline_stall._PREFLIGHT_FAMILY_MIN_LEN`` (12);
    every named-dangerous token in the audit (``api``, ``auth``, ``rotation``)
    is below it. Call sites whose haystack is structured rather than free text
    may pass a lower ``min_len``.

In every wired call site the match is a *fallback* signal (stronger signals —
exact pr_url, exact branch — run first), and a non-match fails safe: the step is
left un-advanced, the recovery proceeds, the dead loop is surfaced. Tightening
these matches therefore only ever errs toward surfacing/recovering, never toward
a silent wrong-advance.
"""
from __future__ import annotations

import re

# Mirrors heal_pipeline_stall._PREFLIGHT_FAMILY_MIN_LEN: an id shorter than this
# is treated as too short/common to disambiguate by substring. Every token the
# audit named as dangerous (api=3, auth=4, rotation=8) is below it.
DEFAULT_MIN_LEN = 12


def id_matches(needle: object, haystack: object, *, min_len: int = DEFAULT_MIN_LEN) -> bool:
    """True iff ``needle`` occurs in ``haystack`` as a whole, boundary-delimited
    token AND is at least ``min_len`` characters long.

    "Boundary-delimited" means the needle is bordered on each side by a
    non-alphanumeric character or a string edge, so ``api`` matches ``(api)``
    and ``-api-`` but not ``apiserver``. Matching is case-insensitive.

    Empty/None inputs and needles shorter than ``min_len`` return False.
    """
    if not needle or not haystack:
        return False
    n = str(needle).lower()
    h = str(haystack).lower()
    if len(n) < min_len:
        return False
    # Boundaries are alnum<->non-alnum transitions, so '-', '_', '.', '/',
    # whitespace and punctuation all delimit a token. re.escape() neutralizes
    # any regex metacharacters in the id itself.
    pattern = r'(?<![a-z0-9])' + re.escape(n) + r'(?![a-z0-9])'
    return re.search(pattern, h) is not None
