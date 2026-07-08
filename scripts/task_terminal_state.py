#!/usr/bin/env python3
"""task_terminal_state.py — the shared terminal-state probe.

Adopted from the terminal-state-reconciliation spec
(`agents/beacon/specs/terminal-state-reconciliation.md` § 2). The invariant the
spec locks: *no in-flight bookkeeping record may outlive its work's terminal
state*. Every reconciler that retires a `pending`/`dispatched`/`active`/
`in_flight` record past a grace window must check the work's terminal ground
truth — and a tri-state `gh` probe for that already existed, duplicated three
ways:

  * `heal_unregistered_approval.gh_ref_resolved` (by PR/issue number),
  * `heal_recovery_already_merged.query_merged_pr` (by `[tag]` in title), and
  * `build_sequence_advancer.gh_pr_says_merged` (by PR url).

This module extracts the ONE shared mechanism the spec mandates. Two layers:

  1. The canonical `task_terminal_state(task_id, variants=[]) -> MERGED | CLOSED
     | OPEN | UNKNOWN` (§ 2) — searches merged/closed/open PRs for the task_id
     (plus known re-dispatch variants) in branch/title across the sandbox
     repos. This is what the § 3 reconcilers (pending approvals, alert-triage,
     missions, sequence steps, in-flight sentinels, the CEO digest) consume.
  2. The kernel those three ad-hoc probes were each re-implementing — a bounded
     `gh` runner (`gh_json`) and a state classifier (`classify_state`), both of
     which return the indeterminate signal on ANY error so a probe NEVER guesses
     terminal. The three existing probes are refactored to call this kernel,
     which removes the duplication while preserving each probe's exact query
     shape and return contract (no behavior regression).

**Conservative posture (non-negotiable, spec § 1):** an OPEN or UNKNOWN signal
means KEEP. `task_terminal_state` only returns a terminal verdict (MERGED /
CLOSED) when it positively matched a terminal PR and saw no still-open PR for the
same id; any gh failure, ambiguity, or no-match collapses to UNKNOWN. Callers
retire on MERGED/CLOSED and keep on OPEN/UNKNOWN, so erring toward UNKNOWN can
only ever leave a phantom in place for another cycle — never falsely retire live
work.

Stdlib + `gh` only; bounded timeout; no Supabase dependency.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from id_match import id_matches  # noqa: E402 — shared boundary-match discipline

# -------------------- terminal-state constants --------------------

MERGED = 'MERGED'
CLOSED = 'CLOSED'
OPEN = 'OPEN'
UNKNOWN = 'UNKNOWN'

# Terminal states retire a record; OPEN/UNKNOWN keep it. Exported so reconcilers
# can write `if state in TERMINAL_STATES: retire(...)` rather than re-listing.
TERMINAL_STATES = frozenset({MERGED, CLOSED})

# Bounded `gh` timeout. The CLI usually answers in under a second; this gives the
# network layer headroom without letting a wedged subprocess stall a caller.
DEFAULT_GH_TIMEOUT_SEC = 15

# How many recent PRs to scan per repo. The advancer's ad-hoc reconcile pass used
# 20; the spec (§ 3.4) calls for widening that, so the shared default is more
# generous. Callers may override per probe.
DEFAULT_PR_LOOKBACK = 50

# Minimum task_id length to trust as a boundary-token match against a PR
# title/branch. Shorter ids are too common to disambiguate; a probe for one
# yields UNKNOWN (KEEP) rather than risk a false terminal match. Real chain
# task_ids (kebab slugs with several segments) comfortably clear this.
MATCH_MIN_LEN = 6

# Default repos with active dispatch chains — mirrors the multi-repo convention
# in heal_recovery_already_merged / heal_pipeline_stall.
DEFAULT_REPOS = (
    'Larry-Yatch/ourliberty-agent-core',
    'Larry-Yatch/ourliberty-dashboard',
)


# -------------------- repo / env helpers --------------------

def _gh_owner() -> str:
    return os.environ.get('OURLIBERTY_GH_OWNER', 'Larry-Yatch')


def default_repos() -> list[str]:
    """Repos to search, honoring a comma-separated `OURLIBERTY_TERMINAL_STATE_REPOS`
    override (test injection / droplet config); otherwise the tracked sandbox
    repos. Returns a fresh list so a caller mutating it can't corrupt the
    constant."""
    override = os.environ.get('OURLIBERTY_TERMINAL_STATE_REPOS')
    if override:
        repos = [r.strip() for r in override.split(',') if r.strip()]
        if repos:
            return repos
    return list(DEFAULT_REPOS)


def _qualify_repo(repo: str) -> str:
    """Return `repo` in the OWNER/REPO form `gh --repo` requires. A bare name
    (`ourliberty-agent-core`) is prefixed with the gh owner; an already-qualified
    or falsy value passes through untouched.

    Delegates to the SINGLE shared resolver `sequence_shortcut_helpers.qualify_repo`
    so the owner default lives in one place. The import is lazy (call-time, not
    module-level) so this low-level probe kernel keeps importing even where the
    heavier ssh dependency chain isn't available; the local fallback preserves the
    exact prior behavior in that case."""
    try:
        from sequence_shortcut_helpers import qualify_repo
        return qualify_repo(repo)
    except Exception:
        if not repo:
            return repo
        return repo if '/' in repo else f'{_gh_owner()}/{repo}'


# -------------------- shared kernel (the de-duplicated probe core) --------------------

def gh_json(args: list[str], timeout: float = DEFAULT_GH_TIMEOUT_SEC) -> Optional[Any]:
    """Run a `gh` command and return its parsed JSON stdout, or None on ANY
    failure — timeout, `gh` missing, non-zero exit, or unparseable output.

    This is the UNKNOWN-on-error kernel every terminal-state probe shares: a
    None return is the "couldn't determine" signal, never an assumption about
    the work's state. Never raises."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


def classify_state(raw: Any) -> str:
    """Map a `gh` PR/issue `state` field to a terminal-state constant. An
    unrecognized / missing value classifies as UNKNOWN (never guessed
    terminal)."""
    if isinstance(raw, str):
        return {'MERGED': MERGED, 'CLOSED': CLOSED, 'OPEN': OPEN}.get(
            raw.strip().upper(), UNKNOWN)
    return UNKNOWN


# -------------------- variant expansion + matching --------------------

# Wrapper prefixes a task_id can carry while the actual work ships under a PR
# whose branch/title hold only the *stem* — e.g. `mirror-review-p3a-retro-prep`
# wraps work on branch `forge/p3a-retro-prep`, and healer-minted `heal-<stem>` /
# `fix-<stem>` ids wrap a stem the PR carries. Without stripping these, the probe
# never matches such an id to its own merged/closed PR, so terminal-reconcile
# KEEPS the phantom approval forever (the prefix-blindness behind merged PR #747
# nagging Larry 24h+). Order-independent; boundary matching + `min_len` in
# `id_matches` guard against a short stem false-matching, and the conservative
# `_combine` (OPEN wins → KEEP) means a wrong match can never falsely retire
# live work — at worst it leaves a phantom one more cycle.
_STRIP_PREFIXES = ('mirror-review-', 'heal-', 'fix-')


def expand_variants(task_id: str, variants: Iterable[str] = ()) -> list[str]:
    """Return the candidate id-strings a task_id's PR may carry in its branch or
    title: the task_id itself, the standard re-dispatch variant shapes
    (`<id>-001`, `<id>-002`, `fix-<id>-revisions`, `<id>-redispatch`), the
    wrapper-prefix-stripped stem (`mirror-review-<stem>` → `<stem>`), and any
    caller-supplied `variants`. Deduplicated, order-preserving.

    The variant shapes are prefixes — boundary matching (`id_matches`) catches
    their suffixed forms too (a branch `fix-<id>-revisions-2` boundary-matches
    the `fix-<id>-revisions` candidate because the trailing `-` is a token
    boundary), so the wildcard tails (`fix-<id>-revisions-*`, `<id>-redispatch-*`)
    in the spec are covered without enumerating every number."""
    base = [
        task_id,
        f'{task_id}-001',
        f'{task_id}-002',
        f'fix-{task_id}-revisions',
        f'{task_id}-redispatch',
    ]
    # Strip a known wrapper prefix so the underlying work's PR (which carries only
    # the stem) is matchable. Conditional on a non-empty stem so a bare prefix
    # (e.g. the literal `heal-`) never collapses to an empty, over-matching ''.
    for prefix in _STRIP_PREFIXES:
        if task_id.startswith(prefix):
            stem = task_id[len(prefix):]
            if stem:
                base.append(stem)
    base.extend(v for v in (variants or ()) if isinstance(v, str) and v)
    seen: set[str] = set()
    out: list[str] = []
    for cand in base:
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def _pr_matches(pr: dict[str, Any], candidates: list[str], min_len: int) -> bool:
    """True if any candidate id appears as a boundary-delimited token in the PR's
    title OR head branch. Boundary matching (not bare substring) prevents a short
    id from false-matching an unrelated PR."""
    haystack = f"{pr.get('title') or ''} {pr.get('headRefName') or ''}"
    return any(id_matches(cand, haystack, min_len=min_len) for cand in candidates)


def _combine(states: list[str]) -> str:
    """Reduce the states of all PRs matching a task_id to one verdict.

    Precedence is conservative: a still-OPEN PR for the id wins over any terminal
    one (the work is still live under a variant — KEEP, never falsely retire);
    otherwise MERGED (work shipped) beats CLOSED (abandoned). No matched PR — or
    only unclassifiable states — collapses to UNKNOWN (KEEP)."""
    if OPEN in states:
        return OPEN
    if MERGED in states:
        return MERGED
    if CLOSED in states:
        return CLOSED
    return UNKNOWN


# -------------------- direct PR-coordinate probe --------------------

# A session-less PR's review is tracked under the synthetic task_id
# `pr-<repo>-<num>` (the shape `heal_pipeline_stall._PR_TASK_ID_RE` canonicalizes
# and `decision_identity.canonical_decision_key` normalizes wrapped ids to). Such
# an id NAMES its PR by NUMBER — the number never appears as a token in the PR's
# branch or title, so the token-search `task_terminal_state` below is
# structurally blind to it and stayed UNKNOWN forever after a coordinate PR
# merged out-of-band (the 2026-07-08 six-phantom-reminder incident). These two
# helpers are the direct probe for that shape. They are a DELIBERATELY SEPARATE
# primitive from `task_terminal_state`, not wired into it: the two answer
# different questions — `task_terminal_state` asks "is this task's work live
# under ANY branch/variant?" (OPEN-variant-wins, for stall/mission liveness),
# while `pr_coordinate_state` asks "is THIS specific PR terminal?" (the right
# question for "is the decision on PR #N moot?"). Merging them would force one
# semantics onto both callers.

# The numeric-plus-optional-head8 tail of a coordinate stem: `<num>` or
# `<num>-<head8>`. The head8 suffix is `(head_sha or '')[:8]` (hex), appended by
# outbox_notifier._emit_no_session_decision_approval when a head SHA is present —
# so both `pr-<repo>-<num>` and `pr-<repo>-<num>-<head8>` are live id shapes and
# the parser must tolerate the suffix (a `[0-9]+$` anchor would silently miss
# every head8-suffixed card, re-opening the phantom class for that shape).
_PR_NUM_TAIL_RE = re.compile(r'^([0-9]+)(?:-[0-9a-fA-F]{4,40})?$')

# The wrapper these session-less PR-decision approvals carry (outbox_notifier
# mints `mirror-review-<task_id>[-<head8>]`). Stripped before coordinate parsing.
_MIRROR_REVIEW_WRAPPER = 'mirror-review-'


def parse_pr_coordinate(
    task_id: str, repos: Optional[Iterable[str]] = None,
) -> Optional[tuple[str, int]]:
    """Parse a `pr-<repo>-<num>` coordinate — bare, `mirror-review-`-wrapped, or
    with a trailing `-<head8>` SHA suffix — to (qualified_repo, pr_number), else
    None.

    Anchored on the KNOWN repo short name rather than a greedy regex: for each
    tracked repo (`repos` or `default_repos()`) we test the exact prefix
    `pr-<short>-` and parse only the remainder as `<num>[-<head8>]`. Anchoring on
    the known repo removes the greedy-split ambiguity a monolithic regex has when
    the head8 tail is itself all-digits (`pr-<repo>-845-00001234`), and scopes the
    later gh lookup to tracked repos. An id whose repo segment matches no tracked
    repo returns None (never guessed).

    NOTE (single-owner assumption): the short->full map keys on the repo's short
    name, so two tracked repos sharing a short name across owners would collide
    (last wins). All tracked repos are single-owner today; revisit if a fork is
    ever tracked."""
    if not isinstance(task_id, str) or not task_id:
        return None
    s = task_id.strip()
    if s.startswith(_MIRROR_REVIEW_WRAPPER):
        s = s[len(_MIRROR_REVIEW_WRAPPER):]
    repo_list = list(repos) if repos is not None else default_repos()
    for repo in repo_list:
        full = _qualify_repo(repo)
        if not full:
            continue
        short = full.split('/', 1)[-1]
        prefix = f'pr-{short}-'
        if not s.startswith(prefix):
            continue
        m = _PR_NUM_TAIL_RE.match(s[len(prefix):])
        if m:
            return full, int(m.group(1))
    return None


def pr_coordinate_state(
    repo: str,
    number: int,
    *,
    timeout: float = DEFAULT_GH_TIMEOUT_SEC,
) -> tuple[str, Optional[str]]:
    """Direct gh lookup for one PR by (qualified `repo`, `number`): returns
    (state, merge_sha). `(UNKNOWN, None)` on ANY gh failure — the conservative
    kernel posture: a lookup failure is never a terminal verdict, so a reconciler
    consuming this can only ever KEEP on error, never falsely retire. merge_sha is
    the merge commit oid when GitHub surfaces one (MERGED PRs), else None.

    Takes an already-parsed (repo, number) — the caller parses once via
    `parse_pr_coordinate` — so the coordinate is not re-derived here."""
    data = gh_json(
        ['gh', 'pr', 'view', str(number), '--repo', repo,
         '--json', 'state,mergeCommit'],
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return UNKNOWN, None
    state = classify_state(data.get('state'))
    merge_commit = data.get('mergeCommit')
    sha = merge_commit.get('oid') if isinstance(merge_commit, dict) else None
    return state, (sha if isinstance(sha, str) and sha else None)


# -------------------- the canonical probe (spec § 2) --------------------

def task_terminal_state(
    task_id: str,
    variants: Iterable[str] = (),
    repos: Optional[Iterable[str]] = None,
    *,
    timeout: float = DEFAULT_GH_TIMEOUT_SEC,
    limit: int = DEFAULT_PR_LOOKBACK,
    min_len: int = MATCH_MIN_LEN,
) -> str:
    """Return the terminal state of the work tracked by `task_id`:
    `MERGED` | `CLOSED` | `OPEN` | `UNKNOWN`.

    Searches recently-touched PRs (all states) in each repo for the task_id or a
    known re-dispatch variant appearing as a boundary token in the PR branch or
    title, then reduces the matches via `_combine`. Returns UNKNOWN on any `gh`
    failure, on no match, or on ambiguity — never guesses terminal (spec § 2).

    Args:
        task_id: the work's stable task id.
        variants: extra id-strings to also match (beyond the standard re-dispatch
            shapes `expand_variants` adds).
        repos: repos to search (OWNER/REPO or bare name); defaults to
            `default_repos()`.
        timeout: per-`gh`-call timeout in seconds.
        limit: how many recent PRs to scan per repo.
        min_len: minimum id length to trust as a boundary-token match.
    """
    if not task_id or not isinstance(task_id, str):
        return UNKNOWN
    repo_list = list(repos) if repos is not None else default_repos()
    candidates = expand_variants(task_id, variants)
    matched_states: list[str] = []
    for repo in repo_list:
        data = gh_json(
            [
                'gh', 'pr', 'list', '--repo', _qualify_repo(repo),
                '--state', 'all', '--limit', str(limit),
                '--json', 'number,state,title,headRefName,url',
            ],
            timeout=timeout,
        )
        if not isinstance(data, list):
            # gh failed for this repo (None) or returned an unexpected shape;
            # skip it. If EVERY repo fails, matched_states stays empty -> UNKNOWN.
            continue
        for pr in data:
            if isinstance(pr, dict) and _pr_matches(pr, candidates, min_len):
                matched_states.append(classify_state(pr.get('state')))
    return _combine(matched_states)
