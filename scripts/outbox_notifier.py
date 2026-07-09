#!/usr/bin/env python3
"""outbox_notifier.py — back-channel routing for completed work + dead-letters.

Phase D3, commit 2 (`D3-notifier`). Long-running daemon. 5s poll. Two scans:

  1. **Outbox results.** For each completed `outboxes/<agent>/<task-id>.json`:
     - If the original `source` is another agent OR a `*-question` source,
       write a `notify-<stem>.json` to that agent's inbox via
       `safe_write_inbox`. Bare-agent sources notify back as `*-result`;
       `*-question` sources notify back as `*-clarification`. Depth-capped
       at 1 (matches upstream `orchestrator.process_outbox_notifications`
       line 1878).
     - Failed tasks (exit_code != 0) take the same path — the dispatcher
       learns about the failure too (Gap 4 dead-letter for in-flight
       failures). Prompt frames it as `FAILED` + error text.
     - System sources (`larry`, `telegram-webhook`, `cron`, etc.) and reply-leg
       sources (`*-result`, `*-clarification`, `*-answer`) are archive-only —
       no back-channel.
     - All processed outboxes archive to `outboxes/<agent>/.archive/`
       (audit trail; cheap; matches inbox watcher convention).

  2. **Dead-letter .invalid scan.** Validator-rejected tasks today land in
     `inboxes/<agent>/.invalid/<stem>.json` with a `.reason` sidecar and
     nobody is notified. This scan finds new .invalid entries since last
     run and writes a dead-letter notify back to the source agent. State
     persisted in `state/outbox-notifier-dead-letter.json` so we don't
     re-notify on every cycle. GC'd when underlying .invalid file goes away.

What this daemon does NOT do:
  - Send Telegram DMs to Larry — that's the per-agent bot's job (the bot
    reads its own outboxes and DMs the user). Commit 3 (D3-approval) extends
    Beacon's bot to route certain notifies as approval DMs.
  - Routing-validate fresh dispatches — that's handled at write time by
    `safe_write_inbox.safe_write_inbox` (commit 1).
  - Process clarification-related outbox shapes beyond routing them. The
    Forge preflight protocol that produces `forge-question` outboxes is
    commit 4 (`D3-forge`); this daemon just routes them when they arrive.

Adapted from GrowthMastery-ai/gm-agent-core
`orchestrator.process_outbox_notifications` lines 1869–1947, with GM-specific
ship-tracking + briefing-fallback + milestone bypass stripped (audit Section 4).

EMERGENCY_HALT honored — same flag as the inbox watcher; touching it stops
both daemons cleanly.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Import sibling scripts as modules.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import beacon_approval_handler as approval  # noqa: E402
import build_sequence_kickoff       # noqa: E402  # shared kickoff transition (single-sourced)
import chain_event_emit             # noqa: E402  # E4.4e PR-A: push writer
import chain_event_shipper as ces    # noqa: E402  # S-4: auto_merge push parity
from chain_envelope import (        # noqa: E402  # M1: sole envelope constructor
    CARRY,
    DROP,
    backfill_pr_url,        # M3: pr_url <- gh
    backfill_target_repo,   # M3: target_repo <- chain_events
    build_chain_envelope,
)
import dispatch_validator         # noqa: E402
import fixture_patterns             # noqa: E402  # outbox-side fixture gate
import for_larry_signal             # noqa: E402  # §5.2 canonical for-Larry signal
import for_larry_escalations        # noqa: E402  # mirror-review-visibility: action-needed feed
import forge_preflight_handler as fph  # noqa: E402
import larry_alerts                # noqa: E402
import mirror_review_handler as mrh  # noqa: E402
import no_session_ledger            # noqa: E402  # S1: cold-start obligation ledger
import rebase_obligation_ledger     # noqa: E402  # post-open auto-rebase obligation ledger
import routing_validator            # noqa: E402  # allowed_repos source of truth
import safe_write_inbox             # noqa: E402
import sequence_shortcut_helpers as ssh  # noqa: E402  # V6: step-merged signal
import trust_policy                 # noqa: E402
import worktree_manager             # noqa: E402  # auto-merge worktree teardown
from test_isolation_guard import gh_write  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
INBOXES_ROOT = AGENTS_ROOT / 'inboxes'
OUTBOXES_ROOT = AGENTS_ROOT / 'outboxes'
BLACKBOARD = AGENTS_ROOT / 'blackboard'
LOG_FILE = AGENTS_ROOT / 'logs' / 'outbox-notifier.log'
DEAD_LETTER_STATE = AGENTS_ROOT / 'state' / 'outbox-notifier-dead-letter.json'
EMERGENCY_HALT_FLAG = BLACKBOARD / 'EMERGENCY_HALT'
# D3.5 commit 5d — cost-budget gate reads cumulative per-task spend from
# the costs.jsonl ledger that inbox_watcher.process_task writes after every
# claude invocation. Field shape: `{ts, task_id, agent, cost_usd, ...}` —
# one JSON object per line. Field-name drift from upstream noted in
# docs/upstream-audit.md Pattern G (we use `cost_usd`, upstream uses
# `total_cost_usd`); the gate stays on the local name.
COSTS_FILE = BLACKBOARD / 'costs.jsonl'

# Multi-turn marker recovery (mirror-multiturn-outbox-overwrite-001 +
# chain-discipline-marker-parser-and-regression-check-001):
# the outbox `result` field holds only the final assistant turn from `claude -p`,
# so if the agent's session keeps running after emitting a marker (e.g. a
# Monitor tool started before the marker fires its timeout event later, waking
# the agent for an extra turn; or a poll loop misbehaves after a REVIEW_PASS),
# the marker gets clobbered. The full session transcript lives on disk under
# Claude Code's per-project session-log tree; `_scan_session_log_for_latest_marker_text`
# walks every assistant turn and picks the LATEST one that parses as a valid
# marker — this is the authoritative source, with `result` used only as a
# fallback when the session log is unavailable. Overridable via env so tests
# can point at a fixture dir.
CLAUDE_PROJECTS_ROOT = Path(
    os.environ.get('CLAUDE_PROJECTS_ROOT', str(HOME / '.claude' / 'projects'))
)

AGENT_IDS = ['beacon', 'forge', 'mirror', 'pulse']

POLL_INTERVAL_SECONDS = 5
DEAD_LETTER_STATE_CAP = 1000

# fix-notifier-review-dispatch-reliability (Part B): self-healing reconciliation
# sweep for Forge build-phase outboxes that opened a PR but never got a Mirror
# review-request dispatched (the PR #303 incident). The sweep re-scans
# forge/.archive for recent build results and idempotently re-dispatches any
# missed review. Bounded two ways so it's a cheap no-op in steady state:
#   - WINDOW: only archive files modified within the last RECONCILE_WINDOW_HOURS
#     are considered (the archive holds ~700 files; the window keeps the scan
#     small and avoids re-examining long-settled history).
#   - CADENCE: the sweep runs at most once per RECONCILE_INTERVAL_SECONDS, not
#     every POLL_INTERVAL_SECONDS poll, tracked via _last_reconcile_ts.
RECONCILE_WINDOW_HOURS = 6
RECONCILE_INTERVAL_SECONDS = 60
MAX_RESULT_TEXT_CHARS = 8000  # truncate huge claude outputs in notify prompts

# Maximum notify-cascade depth. Hop 0 = original dispatch; hop 1 = first notify
# back (the answer). Anything > 1 means we'd be writing notify-X-result-result,
# which is the double-nest upstream's 2026-04-15 incident caught.
#
# Exception: Forge preflight markers bypass this cap because the clarification
# protocol is intentionally multi-hop (Beacon → Forge → Beacon → Forge → ...
# until PROCEED/REJECT/budget-exhausted). The `max_clarifications` budget on
# the task envelope replaces the depth cap as the termination guard for
# marker-driven cascades.
MAX_NOTIFY_DEPTH = 1

# Cap on consecutive marker-error retries to Forge OR Mirror. Defense
# against a wedge loop where the agent keeps producing malformed markers
# (e.g., a CLAUDE.md bug or a session that's lost track of the grammar).
# When exceeded, the notifier dead-letters back to Beacon instead of
# retrying the agent again.
MAX_MARKER_ERROR_RETRIES = 3

# D3.5 commit 5c — inbound-intent values on a Beacon outbox that trigger
# the auto-replan extraction path. Today only `review-escalate` qualifies
# (Mirror flagged a finding that needs spec revision); future commits may
# add others (e.g., a Pulse digest-driven replan intent). The check is
# narrow on purpose — Beacon's chat-mode APPROVAL_REQUEST flow runs on
# her telegram bot and must NOT also fire here.
_BEACON_REPLAN_INBOUND_INTENTS = frozenset({'review-escalate'})

# Closed-loop step 4 (2026-05-24) — `source` values on a Beacon outbox that
# trigger the Pulse-auto-dispatch extraction path. When Pulse decides a
# weekly proposal is worth pursuing, she drops a dispatch envelope into
# Beacon's inbox with this source. Beacon processes the brief and emits a
# clean APPROVAL_REQUEST marker; the notifier extracts it, runs it through
# trust_policy (NOT implicit Larry approval — Pulse's judgment is distinct
# from a Larry-session dispatch), and routes to the standard approval-DM
# pipeline. Step 5 wires Pulse to write the envelope.
_BEACON_AUTO_DISPATCH_SOURCES = frozenset({'pulse-auto-dispatch'})

# D3.5 commit 5a — extract Forge's PR URL from a build-phase outbox result.
# Forge's CLAUDE.md mandates the build response include either
# `PR opened: <url>` (new PR opened this dispatch) OR `PR updated: <url>`
# (commit added to a PR that was already open — e.g., a replan iteration,
# a fill-in dispatch, or any subsequent build on an existing PR's branch).
# Anchored to start-of-LINE (re.MULTILINE) so a PR URL discussed mid-paragraph
# ("I considered PR opened: <stale-url> from last week") doesn't false-match,
# while still catching a real `PR opened: <url>` line that lands at the end
# of a narrative paragraph (Forge's lenient build-phase shape: status bullets
# then the URL on its own line).
#
# D3.5 5d-followup-2 (PR #20 sibling): relaxed the anchor from \A (start-of-
# string) to ^ + re.MULTILINE. The strict start-of-string form matched PR #17's
# build result (`PR opened:` on line 1) but silently dropped PR #20's build
# result, which led with narrative ("Build phase contract is already satisfied
# on this branch:\n- Branch ...") and put `PR opened: <url>` on its own line
# at the end — causing the Mirror review-request dispatch to be skipped while
# the default Beacon notify still fired. The m-2 review's mid-paragraph
# false-match protection is preserved by the line anchor: `i considered PR
# opened: <stale>` still doesn't match (no line-start prefix).
#
# D3.5 5c-followup-2 (Miss #3): added the `updated` alternative. Prior version
# only matched `PR opened:`, which broke the auto-Mirror-review trigger on
# existing-PR-update flows — Forge naturally led with status narrative ("Commit
# X pushed to the head branch of PR #N (OPEN)...") and put `PR opened: <url>`
# as paragraph 2, never matching the start-of-string anchor. Surfaced by the
# 5c fill-in dispatch live test 2026-05-14. Forge's CLAUDE.md updated in the
# same commit with the explicit `PR updated:` example so the discipline rule
# matches the regex contract.
#
# fix-notifier-review-dispatch-reliability (Part A): broadened to recognize
# Forge's natural phrasing without weakening the false-match protection.
# Two additions over the prior line-start-anchored form:
#   1. An OPTIONAL short leading clause before `PR`, BUT only when that clause
#      ends in a clause terminator (`.` or `:`) immediately followed by
#      whitespace and then `PR`. This catches `Done. PR #303 opened: <url>`
#      and `Result: PR opened: <url>` (the literal shape of the PR #303
#      incident outbox, which the strict line-start anchor dropped) while
#      STILL rejecting `I considered PR opened: <stale> from last week` — that
#      mid-sentence form has no clause terminator directly before `PR`, so the
#      optional group can't bridge to it. The m-2 false-match guarantee holds.
#   2. An OPTIONAL `#<digits>` issue/PR-number token between `PR` and the verb,
#      so `PR #303 opened:` / `PR #12 updated:` match.
# Canonical `PR opened: <url>` / `PR updated: <url>` lines (with or without
# leading horizontal whitespace, on any line) match with byte-for-byte
# identical group(1) results to the prior regex — both new groups are
# optional and the regex engine skips them when the line already starts with
# `PR`. Verified against the existing PrUrlRegexAnchored/AcceptsBothPrefixes
# suites plus the new Part A cases.
_PR_URL_RE = re.compile(
    # HIGH-2 (PR #10 review): use `[ \t]` not `\s` so newlines between `PR`
    # and the verb DON'T match. `\s+` would let Forge accidentally split
    # `PR\nopened: <url>` across lines and still satisfy the regex even
    # though it violates the CLAUDE.md "FIRST LINE unconditional" rule.
    r'^[ \t]*'
    # Optional short leading clause, terminated by `.`/`:` + whitespace so it
    # only bridges to `PR` across a real clause boundary (`Done. `, `Result: `)
    # — never across a mid-sentence run like `I considered `. `[^\n]` keeps it
    # on one line.
    r'(?:[^\n]*?[.:][ \t]+)?'
    r'PR[ \t]+'
    # Optional issue/PR-number token, e.g. `#303 `.
    r'(?:#\d+[ \t]+)?'
    r'(?:opened|updated):[ \t]*(https://github\.com/[^\s]+/pull/\d+)',
    re.MULTILINE,
)

# D3.5 commit 5b — extract Forge's revision-applied summary from a revision-
# phase outbox result. Forge's CLAUDE.md mandates revision responses START
# with `Revision N applied: <one-line summary>`. Strict per Larry's signoff:
# unlike the build-phase regex (which falls through to default routing when
# the prefix is missing, preserving the blocker-paragraph path), the revision
# phase has no documented blocker path — missing prefix triggers a
# marker-error dead-letter cascade back to Forge.
_REVISION_APPLIED_RE = re.compile(
    r'\A\s*Revision\s+(\d+)\s+applied:\s*(.+?)(?:\n|\Z)',
    re.IGNORECASE,
)

# D3.5 5b — read `loop_bounds.max_revisions` from config/agent-models.json so
# retuning the dial in config actually takes effect (M-4 review fix). Cached
# at module level since this fires on every Mirror review-request dispatch.
# Tests reset the cache via _invalidate_loop_bounds_cache.
_MODELS_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'agent-models.json'
_LOOP_BOUNDS_CACHE: dict[str, Any] = {}

# merge-gate-deep-review-hold — PRs touching these paths (or carrying a
# `deep-review-required` label) pass Mirror review but are HELD for a human
# `/code-review high` + manual merge instead of auto-merging. This is the code
# default; `config/deep-review-paths.json` (when present + enabled) overrides
# it. Globs are fnmatch'd against `gh pr view --json files` paths. Kept in sync
# with config/deep-review-paths.json — the config file IS this list.
_DEFAULT_DEEP_REVIEW_PATHS: tuple[str, ...] = (
    'scripts/beacon_approval_handler.py',
    'scripts/decision_*.py',
    'scripts/resolve*.py',
    'scripts/for_larry_*.py',
    'scripts/larry_alerts.py',
    'scripts/chain_event_emit.py',
    'scripts/trust_policy.py',
    'scripts/outbox_notifier.py',
    'config/trust-policy.json',
    'config/suite-guardian.json',
)
_DEEP_REVIEW_PATHS_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / 'config' / 'deep-review-paths.json'
)
# The stamp Claude's pre-handoff `/code-review high` applies to mark a
# critical-path PR as deep-reviewed. v1 form is a PR label; the stamp check
# reuses the SAME `gh pr view --json labels` read as the `deep-review-required`
# trigger, so the stamp costs no gh call beyond that one labels read.
# KNOWN LIMITATION (follow-up): a label is not SHA-bound, so it persists across
# a force-push — a stamped PR re-pushed with new commits before auto-merge would
# pass the gate on the unreviewed head. The robust form is a SHA-bound comment
# marker (`=== DEEP_REVIEW_PASS sha=<head> ===`, mirroring merge_reviewed_pr.sh's
# LOCAL_REVIEW_PASS so it self-invalidates on a new push). Deferred per spec
# (§0 "the simplest form is a PR label"); low real risk since in this pipeline
# the stamp is applied immediately before hand-off with no third-party push.
_DEEP_REVIEW_REQUIRED_LABEL = 'deep-review-required'
_DEEP_REVIEW_PASSED_LABEL = 'deep-review-passed'

# D3.5 commit 5d — extract repo coords + PR number from a github.com PR URL.
# Mirror's REVIEW_PASS marker carries `pr_url`; auto-merge needs the
# (owner/repo, PR#) split to feed `gh pr merge`. Tolerant of trailing
# slashes, query strings, fragments — anything after the PR digits is
# discarded.
# Anchored to start-of-string so an attacker-influenced URL embedded in a
# Mirror outbox payload (e.g. via a future prompt-injection path or a
# marker-error retry mis-shape) can't smuggle alternate repo coords. Only
# matches genuine `https://github.com/<owner>/<repo>/pull/<N>` prefixes;
# the post-PR-number path is discarded.
_GH_PR_URL_RE = re.compile(
    r'^https?://github\.com/([^/]+/[^/]+)/pull/(\d+)',
)

# Structural pr_url validator (2026-05-29 — structural-pr-url-validator;
# repo allowlist sourced from config 2026-06-13 —
# notifier-autopr-allowlist-from-config-001).
# Replaces the prior name-based repo-coords allowlist + canonical-form
# rewrite table. The AUTO_MERGE gate validates two intrinsic properties
# of the pr_url: (1) shape — does it match the canonical
# `https://github.com/Larry-Yatch/<repo>/pull/<N>` form with N>=1, AND the
# captured `<repo>` slug is on the agent's allowlist; and (2) existence —
# does the PR actually exist and have state=OPEN (Layer 2, `gh pr view`).
#
# The regex captures a GENERIC repo slug rather than a hardcoded
# alternation. The closed-set anti-spoofing boundary (still Larry-Yatch
# only, still a FINITE allowlist, NO wildcard) is preserved by checking
# the captured slug for membership in
# `routing_validator.allowed_repos_for('forge')` inside
# `_pr_url_shape_check` — i.e. the SAME `config/agent-models.json`
# `allowed_repos` that already gates dispatch. That config is the SINGLE
# SOURCE OF TRUTH for the repo allowlist: do NOT re-hardcode an
# alternation here. The prior hardcoded
# `(ourliberty-agent-core|ourliberty-dashboard)` drifted when
# ourliberty-graph onboarded (config gained the repo, this regex did
# not), so a clean Mirror REVIEW_PASS on a graph PR was skipped as
# `pr-url-shape-invalid`. Config-sourcing removes that drift class.
#
# Anchored start-and-end so trailing junk (anchors, query strings,
# doctored fragments) is rejected — at this layer we want the exact form
# `gh pr merge` needs, nothing else. The slug class `[A-Za-z0-9._-]+`
# cannot contain a slash, so the owner anchor (`Larry-Yatch/`) still binds
# and `x/y`-style owner spoofs fail as shape-mismatch.
_PR_URL_STRUCTURAL_RE = re.compile(
    r'^https://github\.com/Larry-Yatch/'
    r'([A-Za-z0-9._-]+)/pull/([1-9]\d*)$'
)

# Existence-check timeout. Tighter than _AUTO_MERGE_TIMEOUT_S (30s) because
# this is the cheap "does the PR exist" probe; if `gh pr view` doesn't
# answer in 10s the right behavior is to skip the merge attempt entirely,
# not hold the notifier's poll loop. Treated structurally identically to
# a 404 — the discipline is "don't shell out to gh pr merge unless we
# already know the PR is real and open".
_PR_URL_EXISTENCE_TIMEOUT_S = 10

# D3.5 commit 5d — fallback cap when config is missing/malformed. The
# config (`loop_bounds.cost_per_task_usd`) has been pre-staged since 5a at
# $5.00; the live value Larry signed off on at Q7=3 is what gets enforced.
# This constant is the safety net (config file missing or unparseable).
DEFAULT_COST_PER_TASK_USD_CAP = 5.0

# D3.5 commit 5d — `gh pr merge` and `gh pr view` shell-out timeout. 30s
# allows for slow GitHub API + auth-token refresh; longer would risk the
# notifier blocking past its 5s poll cadence on degraded gh CLI behavior.
_AUTO_MERGE_TIMEOUT_S = 30

# ── GitHub API rate-limit backoff gate ──────────────────────────────────────
# outbox-notifier-gh-ratelimit-backoff-001. When a `gh` shell-out returns the
# GitHub rate-limit signature (exit != 0 + stderr containing "rate limit
# already exceeded", seen as both the REST and GraphQL wording), the daemon
# used to re-hit the API on the very next 5s scan cycle with zero delay — a
# WARN storm that compounds the exhaustion and stalls auto-merge. This gate
# holds a module-level backoff window that every `gh` wrapper consults at
# entry: while the window is open the wrapper short-circuits WITHOUT shelling
# out (returning the same degraded value it already returns on a transport
# error), and a completed call routes its outcome through the note fns so a
# rate-limit hit arms/extends the window (exponential, jittered) and any
# success clears it. Non-rate-limit failures (timeout, 404, auth) never arm.
_GH_RATE_LIMIT_FLOOR_S = 60.0     # first backoff after a single rate-limit hit
_GH_RATE_LIMIT_CEILING_S = 300.0  # max window regardless of consecutive count
_GH_RATE_LIMIT_JITTER_S = 15.0    # +/- up to this many seconds of jitter

# The rate-limit signature. GitHub emits this for both REST ("API rate limit
# already exceeded ...") and GraphQL ("GraphQL: API rate limit already
# exceeded for user ID ..."); the shared "rate limit already exceeded"
# substring matches both. Case-insensitive for robustness.
_GH_RATE_LIMIT_SIGNATURE_RE = re.compile(
    r'rate limit already exceeded', re.IGNORECASE,
)

# Injectable seams for tests. Production leaves these at the stdlib defaults;
# tests swap in a fake monotonic clock + fixed jitter so the backoff math is
# deterministic and no real time passes / no real gh is hit.
_gh_backoff_clock = time.monotonic       # () -> float seconds (monotonic)
_gh_backoff_jitter = random.uniform      # (low, high) -> float

# Module state. `_gh_backoff_until` is the monotonic timestamp before which no
# `gh` call should shell out (0.0 == no active backoff). The consecutive count
# drives the exponential growth. `_gh_backoff_skip_logged` throttles the
# "skipping gh call" line to at most one per backoff window.
_gh_backoff_until: float = 0.0
_gh_consecutive_rate_limit: int = 0
_gh_backoff_skip_logged: bool = False


def _gh_is_rate_limit_error(returncode: int, stderr: Optional[str]) -> bool:
    """True iff a *completed* `gh` call is the GitHub rate-limit signature.

    Requires a non-zero exit AND stderr matching the rate-limit wording, so a
    plain 404 / auth failure / conflict (non-zero exit, different stderr) does
    NOT count — those must not arm the backoff (success criterion 4).
    """
    if returncode == 0:
        return False
    if not stderr:
        return False
    return bool(_GH_RATE_LIMIT_SIGNATURE_RE.search(stderr))


def _gh_backoff_remaining() -> float:
    """Seconds until the current backoff window expires (0.0 if none)."""
    return max(0.0, _gh_backoff_until - _gh_backoff_clock())


def _gh_backoff_active() -> bool:
    """True while a rate-limit backoff window is open.

    Wrappers call this at entry and short-circuit (skip the shell-out) when it
    returns True, so the daemon stops re-hitting an exhausted API every poll.
    """
    return _gh_backoff_clock() < _gh_backoff_until


def _gh_note_rate_limit(stderr: Optional[str]) -> None:
    """Arm/extend the backoff window after a rate-limit hit.

    Grows exponentially from a 60s floor (min(300, 60 * 2**count)) with +/- up
    to ~15s of jitter, clamped to the 300s ceiling, and increments the
    consecutive-hit count. Idempotent w.r.t. logging via the skip-log reset so
    each fresh window emits its skip line exactly once.
    """
    global _gh_backoff_until, _gh_consecutive_rate_limit, _gh_backoff_skip_logged
    base = _GH_RATE_LIMIT_FLOOR_S * (2 ** _gh_consecutive_rate_limit)
    window = min(_GH_RATE_LIMIT_CEILING_S, base)
    jitter = _gh_backoff_jitter(-_GH_RATE_LIMIT_JITTER_S, _GH_RATE_LIMIT_JITTER_S)
    # Clamp the jittered window to [0, ceiling]; never negative, never over cap.
    window = max(0.0, min(_GH_RATE_LIMIT_CEILING_S, window + jitter))
    _gh_backoff_until = _gh_backoff_clock() + window
    _gh_consecutive_rate_limit += 1
    _gh_backoff_skip_logged = False
    log(
        f'gh rate-limit hit #{_gh_consecutive_rate_limit}; backing off '
        f'{window:.0f}s (stderr: {(stderr or "").strip()[:160]})',
        'WARN',
    )


def _gh_note_success() -> None:
    """Clear the backoff window and reset the consecutive count.

    Any 2xx `gh` result routes here, so a single success resets the gate to
    zero (success criterion 3).
    """
    global _gh_backoff_until, _gh_consecutive_rate_limit, _gh_backoff_skip_logged
    if _gh_backoff_until or _gh_consecutive_rate_limit:
        _gh_backoff_until = 0.0
        _gh_consecutive_rate_limit = 0
        _gh_backoff_skip_logged = False


def _gh_note_result(returncode: int, stderr: Optional[str]) -> None:
    """Route a *completed* `gh` call through the note fns.

    Success (exit 0) clears the window; a rate-limit signature arms/extends it;
    any other non-zero exit is left alone (neither arms nor clears — a 404 or
    auth error is not a throttling signal, and the still-open window from an
    earlier rate-limit hit must persist).
    """
    if returncode == 0:
        _gh_note_success()
    elif _gh_is_rate_limit_error(returncode, stderr):
        _gh_note_rate_limit(stderr)


def _gh_backoff_skip(context: str) -> bool:
    """Entry gate for a `gh` wrapper: True == skip the shell-out.

    Logs a single throttled 'skipping gh call' line per backoff window (not one
    per wrapper per poll — success criterion 5). Returns False when no window is
    open, so the caller proceeds to shell out normally.
    """
    global _gh_backoff_skip_logged
    if not _gh_backoff_active():
        return False
    if not _gh_backoff_skip_logged:
        _gh_backoff_skip_logged = True
        log(
            f'skipping gh call ({context}); in rate-limit backoff for '
            f'{_gh_backoff_remaining():.0f}s more '
            f'(consecutive={_gh_consecutive_rate_limit})',
            'WARN',
        )
    return True


# regression-gate-steady-state-warmer (spec PR 2). The canonical regression-
# baseline cache dir. The post-merge warmer (here) and the Mirror review gate
# (agent_runner pins the SAME value for phase=review dispatches) MUST agree on
# this path or the cache never hits: `regression_baseline_cache.baseline_dir()`
# otherwise defaults to ``$HOME/agents/blackboard/regression-baselines``, and
# the warmer (notifier HOME=/home/larry) and the gate (Mirror's tier HOME)
# resolve different dirs. Pinning both sides here closes that gap.
REGRESSION_BASELINE_CANONICAL_DIR = (
    '/home/larry/agents/blackboard/regression-baselines'
)

# Repo checkout the warm CLI operates against (the notifier's real tree). The
# detached child fetches origin/main into this checkout, then warms FETCH_HEAD.
_WARM_REPO_ROOT = os.environ.get('OURLIBERTY_REPO_ROOT', '/home/larry/agent-core')

# Full-suite per-SHA budget for the warm run — fits under Mirror's 900s ceiling
# so a warmed parent lets a first review run ONE suite pass (head only).
_WARM_TIMEOUT_PER_SHA_S = 900

# D3.5 commit 5d — test-mode override for `_auto_merge_pr`. Set by unit
# tests (via monkey-patch) to a callable matching `_auto_merge_pr`'s
# signature, returning the same dict shape — this prevents shell-out to
# the real `gh pr merge` against the real GitHub repo when an existing
# Mirror-marker integration test naturally exercises `process_outbox`'s
# review-pass branch. Production keeps it as None (the real shell-out
# fires). Defensive: a buggy/missing override returns through the real
# path, never wedging the daemon.
_AUTO_MERGE_FN_OVERRIDE: Optional[Any] = None

# D3.5 5d-prime — test-mode bypass for the serializer gates. When True,
# `_attempt_auto_merge_with_gates` skips both gates and invokes the merge
# function directly (preserving the D3.5 5d test-fixture contract that
# existing tests rely on — they exercise the merge-outcome rendering
# pipeline without mocking the new `gh pr view --json mergeable` /
# `gh pr list` calls the gates depend on). Production keeps it False;
# tests that want to exercise the gates explicitly do NOT set it.
_AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST: bool = False

# D3.5 commit 5d — daemon-lifetime dedup of cost-budget-cap-fired DMs per
# task_id. First cap-fire for a task queues a closing DM to Larry; later
# fires for the same task within the same daemon process just log a
# sentinel and skip the DM. Cleared on daemon restart (intentional —
# Larry may have raised the cap in config or accepted the work; either
# way a fresh restart resets the gate). Per code-review finding #4
# (avoids DM flooding when multiple dispatch sites refuse in rapid
# succession on the same task).
_COST_BUDGET_DMED_TASKS: set[str] = set()

# D3.5 5d-prime — AUTO_MERGE serializer state. The 2026-05-26 incident
# (PR #112 + PR #109 both appending to docs/operating-manual.md; PR #112's
# auto-merge fired at 13:53Z and failed with "not mergeable: merge commit
# cannot be cleanly created") demonstrated that two Mirror-passed PRs with
# overlapping changed_files can't both auto-merge — the second loses to
# git, and Larry gets a "Merge manually" DM that's fundamentally a
# serializable problem. The serializer catches this upstream: when an
# overlap exists, the second PR's merge is queued behind the first.
#
# Queue file is per-process-restart-safe (atomic temp + rename, parsed at
# every read). Fail-closed on parse error: a corrupt queue refuses all
# subsequent AUTO_MERGE attempts (safer than fail-open — never merge
# unverified PRs) and DMs Larry once with the parse error.
AUTO_MERGE_QUEUE_FILE = AGENTS_ROOT / 'state' / 'auto-merge-queue.json'
AUTO_MERGE_QUEUE_VERSION = 1

# review-dispatch-post-auto-merge-held — a PR parked in AUTO_MERGE_HELD_DEEP_REVIEW
# (PASS'd Mirror, critical-path, awaiting a human `/code-review high`) is pulled
# from the auto-merge queue, so nothing records that it's held. Without a record,
# every review-dispatch path re-reviews it → Mirror re-PASSes → the merge gate
# re-holds → Larry is re-DMed, burning an Opus session per cycle with zero merge
# progress (PR #812: 6 REVIEW_PASS, 0 merge). This state file persists the held
# state, keyed by (repo_coords, pr_number, head_sha), so review dispatch can be
# suppressed for the UNCHANGED held head. It self-heals: a new head (genuine
# revision) clears the entry and is re-reviewed; a merged/closed PR clears too.
DEEP_REVIEW_HELD_FILE = AGENTS_ROOT / 'state' / 'deep-review-held-prs.json'
DEEP_REVIEW_HELD_VERSION = 1

# Fallback when config/agent-models.json doesn't specify
# auto_merge_queue.watchdog_dm_hours. 24h is Larry's default; raise via
# config if he's away for a known-long stretch.
DEFAULT_AUTO_MERGE_WATCHDOG_HOURS = 24

# fix-auto-merge-freshness-revalidation (2026-06-11) — a held auto-merge
# fires on a STALE approval. The 2026-06-11 incident: PR #455 passed Mirror
# review, then its auto-merge was HELD ~6h behind overlapping PR #454. When
# #454 merged (08:11:45Z), #455's auto-merge fired 11s later on its now
# ~6h-old approval — landing a real regression (+13 test failures under
# `python3 -m unittest discover -s scripts/tests`) that a fresh regression-
# gate run against the NEW main would have caught. mergeable=CLEAN did not
# catch it: the regression was semantic (a HOME-swap bootstrap hid Python
# user-site), not a textual conflict. So the release path now re-validates
# freshness against CURRENT main — re-confirm mergeable + re-run the
# regression gate against the moved base — before trusting the pre-hold
# approval. A stale/conflicting/regressing PR is NOT auto-merged; it's
# pulled from the queue and Larry is DMed to re-review/rebase.
#
# Config key `auto_merge_queue.revalidate_regression_on_release` (default
# True) gates the regression re-run; the cheap mergeable re-confirm always
# runs. `auto_merge_queue.release_regression_timeout_per_sha_s` bounds the
# inline `test_regression_check.py` shell-out so a stuck suite can't wedge
# the notifier poll loop indefinitely (holds are rare, so this fires
# infrequently, but it MUST stay bounded).
DEFAULT_REVALIDATE_REGRESSION_ON_RELEASE = True
DEFAULT_RELEASE_REGRESSION_TIMEOUT_PER_SHA_S = 600

# Test seam (mirrors _AUTO_MERGE_FN_OVERRIDE). When set, replaces the real
# `test_regression_check.py` shell-out in `_revalidate_held_merge_before_fire`
# so serializer tests exercise the freshness guard without a real ~10-min
# regression run. The override receives
# (repo_coords, pr_number, base_sha, head_sha) and returns one of
# 'pass' | 'block' | 'error'. Production leaves this None.
_RELEASE_REGRESSION_GATE_FN_OVERRIDE: Optional[Any] = None

# `gh pr view --json mergeable` returns one of MERGEABLE / CONFLICTING /
# UNKNOWN (UNKNOWN = GitHub still computing). Map to the gate's tri-state.
_GH_MERGEABLE_TO_GATE_STATUS = {
    'MERGEABLE': 'mergeable',
    'CONFLICTING': 'conflicting',
    'UNKNOWN': 'unknown',
}

# Module-level fail-closed sentinel. Flipped True by _load_auto_merge_queue
# when the on-disk JSON is corrupt; once set, every gate-check refuses
# AUTO_MERGE until daemon restart (operator clears the file, restarts).
# The DM to Larry is one-shot per daemon process via this same flag.
_AUTO_MERGE_QUEUE_FAIL_CLOSED = False

# Daemon-lifetime dedup of watchdog-DMs across queue entries. Distinct from
# the per-entry `watchdog_dm_sent` flag on disk: this set guards against
# double-DM within a single sweep iteration (defense-in-depth — the on-disk
# flag is the authoritative dedup).
_WATCHDOG_DMED_PRS: set[tuple[str, int]] = set()


def _reset_cost_budget_dmed_tasks() -> None:
    """Test helper — wipe the cost-budget-dedup set between cases."""
    _COST_BUDGET_DMED_TASKS.clear()


def _invalidate_loop_bounds_cache() -> None:
    """Drop the cached loop_bounds. Tests use this between runs."""
    _LOOP_BOUNDS_CACHE.pop('config', None)


def _load_max_revisions_from_config() -> int:
    """Return `loop_bounds.max_revisions` from config/agent-models.json.

    Falls back to mrh.DEFAULT_MAX_REVISIONS when the config file is missing,
    malformed, or doesn't define the field. Cached — the file changes only
    when Larry retunes the dial + restarts the daemon (sync.timer pulls;
    daemon restart picks up the new value via this lazy load).

    D3.5 5b M-4 review fix: before this, _dispatch_mirror_review hardcoded
    `mrh.DEFAULT_MAX_REVISIONS` so config retuning had zero effect on actual
    behavior. The docs/tunables.md promise that the dial is tunable depends
    on this function reading the config file.
    """
    if 'config' not in _LOOP_BOUNDS_CACHE:
        try:
            cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        _LOOP_BOUNDS_CACHE['config'] = cfg
    cfg = _LOOP_BOUNDS_CACHE['config']
    loop_bounds = cfg.get('loop_bounds') if isinstance(cfg, dict) else None
    if not isinstance(loop_bounds, dict):
        return mrh.DEFAULT_MAX_REVISIONS
    raw = loop_bounds.get('max_revisions')
    if not isinstance(raw, int) or raw < 0:
        return mrh.DEFAULT_MAX_REVISIONS
    return raw


def _load_max_replans_from_config() -> int:
    """Return `loop_bounds.max_replans` from config/agent-models.json.

    D3.5 5c. Same shape as `_load_max_revisions_from_config`. Falls back to
    `approval.DEFAULT_MAX_REPLANS` (=2) when the config file is missing,
    malformed, or doesn't define the field. Reads the same cached config so
    the two budgets stay synchronized to the same on-disk state.
    """
    if 'config' not in _LOOP_BOUNDS_CACHE:
        try:
            cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        _LOOP_BOUNDS_CACHE['config'] = cfg
    cfg = _LOOP_BOUNDS_CACHE['config']
    loop_bounds = cfg.get('loop_bounds') if isinstance(cfg, dict) else None
    if not isinstance(loop_bounds, dict):
        return approval.DEFAULT_MAX_REPLANS
    raw = loop_bounds.get('max_replans')
    if not isinstance(raw, int) or raw < 0:
        return approval.DEFAULT_MAX_REPLANS
    return raw


_DEEP_REVIEW_PATHS_CACHE: dict[str, Any] = {}


def _invalidate_deep_review_paths_cache() -> None:
    """Test hook — drop the cached deep-review-paths config so a test that
    writes a fresh config file sees it on the next `_load_deep_review_paths`."""
    _DEEP_REVIEW_PATHS_CACHE.clear()


def _load_deep_review_paths() -> tuple[str, ...]:
    """Return the critical-path glob list for the deep-review merge hold.

    merge-gate-deep-review-hold. Reads `config/deep-review-paths.json`
    ({"enabled": bool, "paths": [glob, ...]}). Returns `paths` when the file
    exists, is well-formed, and is not explicitly disabled; otherwise falls
    back to `_DEFAULT_DEEP_REVIEW_PATHS`. Same graceful-degradation shape as the
    other `_load_*_from_config` helpers: a missing/malformed file, a non-list
    `paths`, or `"enabled": false` all yield the code default so the fileset
    trigger keeps working even if the config is deleted or Pulse-Check writes a
    bad edit.

    `enabled` DEFAULTS TO TRUE: a config that supplies `paths` but omits
    `enabled` is honored (only an explicit `"enabled": false` disables the
    override). Requiring the key would silently no-op an operator's edit — the
    exact foot-gun for the Pulse-Check tuner this file exists for.

    Cached at module level (fires on every merge attempt); tests reset via
    `_invalidate_deep_review_paths_cache`.
    """
    if 'paths' not in _DEEP_REVIEW_PATHS_CACHE:
        try:
            cfg = json.loads(_DEEP_REVIEW_PATHS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = None
        resolved: tuple[str, ...] = _DEFAULT_DEEP_REVIEW_PATHS
        if isinstance(cfg, dict) and cfg.get('enabled', True) is not False:
            raw = cfg.get('paths')
            if isinstance(raw, list):
                globs = tuple(p for p in raw if isinstance(p, str) and p)
                if globs:
                    resolved = globs
        _DEEP_REVIEW_PATHS_CACHE['paths'] = resolved
    return _DEEP_REVIEW_PATHS_CACHE['paths']


def _load_auto_merge_watchdog_hours_from_config() -> int:
    """Return `auto_merge_queue.watchdog_dm_hours` from config/agent-models.json.

    D3.5 5d-prime. Same shape as the `loop_bounds` loaders — reads the
    cached config and falls back to `DEFAULT_AUTO_MERGE_WATCHDOG_HOURS`
    (=24) when the config file is missing, malformed, or doesn't define
    the field. Tunable via `auto_merge_queue.watchdog_dm_hours`; raise
    when Larry's away for a known-long stretch so the queue doesn't spam
    him during planned absences.
    """
    if 'config' not in _LOOP_BOUNDS_CACHE:
        try:
            cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        _LOOP_BOUNDS_CACHE['config'] = cfg
    cfg = _LOOP_BOUNDS_CACHE['config']
    block = cfg.get('auto_merge_queue') if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return DEFAULT_AUTO_MERGE_WATCHDOG_HOURS
    raw = block.get('watchdog_dm_hours')
    if not isinstance(raw, int) or raw <= 0:
        return DEFAULT_AUTO_MERGE_WATCHDOG_HOURS
    return raw


def _load_revalidate_regression_on_release_from_config() -> bool:
    """Return `auto_merge_queue.revalidate_regression_on_release`.

    fix-auto-merge-freshness-revalidation. Gates the inline regression
    re-run on the held-merge release path. Defaults to
    `DEFAULT_REVALIDATE_REGRESSION_ON_RELEASE` (=True) — the guard is ON in
    production so the PR #455 stale-approval class can't recur. Set to
    False only as an emergency escape hatch (e.g. the regression runner is
    itself broken and blocking legitimate releases); the cheap mergeable
    re-confirm still runs even when this is off.
    """
    if 'config' not in _LOOP_BOUNDS_CACHE:
        try:
            cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        _LOOP_BOUNDS_CACHE['config'] = cfg
    cfg = _LOOP_BOUNDS_CACHE['config']
    block = cfg.get('auto_merge_queue') if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return DEFAULT_REVALIDATE_REGRESSION_ON_RELEASE
    raw = block.get('revalidate_regression_on_release')
    if not isinstance(raw, bool):
        return DEFAULT_REVALIDATE_REGRESSION_ON_RELEASE
    return raw


def _load_release_regression_timeout_per_sha_s_from_config() -> int:
    """Return `auto_merge_queue.release_regression_timeout_per_sha_s`.

    fix-auto-merge-freshness-revalidation. Per-SHA wall-clock bound for the
    inline `test_regression_check.py` shell-out on the release path. Falls
    back to `DEFAULT_RELEASE_REGRESSION_TIMEOUT_PER_SHA_S` (=600s). Bounds
    how long a release re-validation can block the notifier poll loop — a
    stuck/hung suite trips the timeout and the gate fails closed (treated as
    'error' → held, NOT merged) rather than wedging the daemon.
    """
    if 'config' not in _LOOP_BOUNDS_CACHE:
        try:
            cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        _LOOP_BOUNDS_CACHE['config'] = cfg
    cfg = _LOOP_BOUNDS_CACHE['config']
    block = cfg.get('auto_merge_queue') if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return DEFAULT_RELEASE_REGRESSION_TIMEOUT_PER_SHA_S
    raw = block.get('release_regression_timeout_per_sha_s')
    if not isinstance(raw, int) or raw <= 0:
        return DEFAULT_RELEASE_REGRESSION_TIMEOUT_PER_SHA_S
    return raw


def _load_cost_per_task_cap_usd_from_config() -> float:
    """Return `loop_bounds.cost_per_task_usd` from config/agent-models.json.

    D3.5 5d. Same shape as `_load_max_revisions_from_config` /
    `_load_max_replans_from_config` — reads the cached `loop_bounds` block.
    Falls back to `DEFAULT_COST_PER_TASK_USD_CAP` (=5.0) when the config
    file is missing, malformed, or the field is absent/non-numeric/negative.

    Activated in 5d per Q7=3 sign-off — the field has been pre-staged in
    config since 5a but wasn't read by any code (the config's `_note` line
    claiming "Read by outbox_notifier when classifying Mirror markers" was
    aspirational). 5d wires the actual gate at the four dispatch sites.
    """
    if 'config' not in _LOOP_BOUNDS_CACHE:
        try:
            cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
        _LOOP_BOUNDS_CACHE['config'] = cfg
    cfg = _LOOP_BOUNDS_CACHE['config']
    loop_bounds = cfg.get('loop_bounds') if isinstance(cfg, dict) else None
    if not isinstance(loop_bounds, dict):
        return DEFAULT_COST_PER_TASK_USD_CAP
    raw = loop_bounds.get('cost_per_task_usd')
    if isinstance(raw, bool):
        # bool is an int subclass — exclude before the int/float check.
        return DEFAULT_COST_PER_TASK_USD_CAP
    if not isinstance(raw, (int, float)) or raw < 0:
        return DEFAULT_COST_PER_TASK_USD_CAP
    return float(raw)


def _check_cost_budget(
    task_id: str,
    cap_usd: Optional[float] = None,
) -> tuple[bool, float, float]:
    """Sum cumulative ``cost_usd`` by ``task_id`` from ``costs.jsonl`` to enforce the per-task cap.
    Tolerates a missing or malformed ledger by degrading to "allow dispatch".

    Return ``(at_cap, current_usd, cap_usd)`` for the per-task budget gate.

    D3.5 commit 5d. Reads the JSONL cost ledger written by
    ``inbox_watcher.process_task`` after every claude invocation; sums the
    ``cost_usd`` field across all records matching ``task_id``. Returns
    ``at_cap=True`` when the current sum is at or above the cap (next
    dispatch would push past the budget).

    Field-name choice (`cost_usd` not upstream's `total_cost_usd`) matches
    Larry's D2-era local schema — see ``docs/upstream-audit.md`` Pattern G.

    Tolerates a missing ledger (returns ``(False, 0.0, cap)``) and malformed
    JSON lines (skips them; daemon-never-wedge). The whole gate degrades to
    "allow dispatch" on read errors — the worst case is one extra Opus call,
    not a wedged loop.

    Cap is read from config via ``_load_cost_per_task_cap_usd_from_config``
    when not passed explicitly. Tests pass an explicit value to avoid
    touching the on-disk config.
    """
    if cap_usd is None:
        cap_usd = _load_cost_per_task_cap_usd_from_config()
    current = 0.0
    try:
        with open(COSTS_FILE, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get('task_id') != task_id:
                    continue
                cost = rec.get('cost_usd')
                if isinstance(cost, bool):
                    continue
                if isinstance(cost, (int, float)) and cost >= 0:
                    current += float(cost)
    except FileNotFoundError:
        return False, 0.0, cap_usd
    except OSError:
        # Don't wedge on transient read errors — allow dispatch, log
        # elsewhere if the caller cares.
        return False, 0.0, cap_usd
    at_cap = current >= cap_usd
    return at_cap, current, cap_usd


def _enforce_cost_budget(
    task_id: str,
    dispatch_label: str,
    data: dict[str, Any],
) -> bool:
    """Pre-dispatch cost-budget gate. Returns True if dispatch should proceed.

    D3.5 commit 5d. Called inside each dispatch helper (`_dispatch_build_phase`,
    `_dispatch_revision_to_forge`, `_dispatch_mirror_review`,
    `_dispatch_mirror_review_rerun`) BEFORE the actual `safe_write_inbox`.
    When ``at_cap``, refuses the dispatch and queues a closing DM to the
    originating chat (if reply_chat_id present) so Larry sees the cap fire
    instead of a silent stall.

    Sentinel log line on cap-fire: ``COST_BUDGET_EXHAUSTED task=<id>
    current=$X.XX cap=$5.00 dispatch=<label>; ... agent=forge`` —
    load-bearing for watchdog scanning. Same pattern as the
    BEACON_REPLAN_ALERT_WRITE_FAILED sentinel from 5c. The trailing
    `agent=forge` kv is read by chain_event_shipper (ships the sentinel as
    a `cost_budget` chain event; the dashboard's Forge-queue lanes only
    fetch agent='forge' rows). The plain per-dispatch ``COST_BUDGET ...
    (allowed)`` INFO line below is deliberately NOT a chain event.
    """
    at_cap, current, cap = _check_cost_budget(task_id)
    if not at_cap:
        # Log at INFO (terse) so the cost trajectory is visible to the
        # operator over time without spamming the log.
        log(
            f'COST_BUDGET task={task_id} current=${current:.2f} '
            f'cap=${cap:.2f} dispatch={dispatch_label} (allowed)',
        )
        return True
    # Per-task daemon-lifetime dedup. Multiple dispatch sites can refuse
    # on the same task in rapid succession (e.g. Forge revision outbox
    # then Mirror re-review outbox both hit the gate within seconds);
    # without dedup, Larry's phone gets pinged once per dispatch attempt.
    # First fire DMs Larry; subsequent fires log the suppression only.
    already_dmed = task_id in _COST_BUDGET_DMED_TASKS
    log(
        f'COST_BUDGET_EXHAUSTED task={task_id} current=${current:.2f} '
        f'cap=${cap:.2f} dispatch={dispatch_label}; refusing dispatch'
        + ('' if already_dmed else ' + queueing closing DM')
        + ' agent=forge',
        'WARN',
    )
    if already_dmed:
        return False
    reply_chat_id = data.get('reply_chat_id')
    if isinstance(reply_chat_id, int):
        # Build a synthetic decision so _render_dm_message has the same
        # shape as marker-driven DMs.
        synthetic_decision = {
            'intent': 'cost-budget-exhausted',
            'payload': {
                'task_id': task_id,
            },
            'intent_kwargs': {
                'task_id': task_id,
                'current_usd': f'{current:.2f}',
                'cap_usd': f'{cap:.2f}',
                'dispatch_label': dispatch_label,
            },
        }
        message = _render_dm_message('cost-budget-exhausted', synthetic_decision)
        if message is not None:
            if larry_alerts.append_notification(
                source='outbox-notifier',
                intent='cost-budget-exhausted',
                message=message,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                _COST_BUDGET_DMED_TASKS.add(task_id)
            else:
                log(
                    f'COST_BUDGET_DM_WRITE_FAILED task={task_id} '
                    f'(disk full?); cap-fire DM did not queue',
                    'WARN',
                )
    return False

# -------------------- notify-prompt template (D3-forge commit 4a) --------------------
# Refined notify framing — Option C hybrid: one skeleton, per-intent action
# block. Replaces the D3-notifier naked "Task result from <agent>: SUCCESS\n..."
# framing that caused the Pulse over-run during commit 2's smoke test (receiver
# agent interpreted the notify as new work and burned $0.59 of Sonnet tokens).
#
# Receivers' CLAUDE.md only needs one rule: "when you receive a notify, follow
# the inline action block." Centralizing the imperative in the notifier — not
# in each receiver's CLAUDE.md — prevents drift.

NOTIFY_TEMPLATE = (
    '[Inter-agent notify | intent={intent} | from={sender} | task={task_id} | status={status}]\n\n'
    'This is an automatic delivery of an outbox result, not a new task request.\n\n'
    '{intent_block}\n\n'
    "Sender's output:\n"
    '---\n'
    '{output}\n'
    '---\n'
)

# Intent vocabulary mirrors dispatch_validator.ALLOWED_INTENTS so notify tasks
# pass schema validation. D3-prep names + D3-forge additions; see validator.
INTENT_ACTION_BLOCKS = {
    'result-notification': (
        'Read the sender output. Journal it in your activity log if material. '
        'Do not generate new work unless the sender output explicitly asks you to.'
    ),
    'clarification-response': (
        'This is the answer to your earlier CLARIFY_REQUEST on task `{task_id}`. '
        'Re-read the original spec with this new context. Decide one of: PROCEED, '
        'CLARIFY_REQUEST (you have {remaining} clarification(s) left), or REJECT. '
        'Emit exactly one marker block.'
    ),
    'clarify': (
        'Forge has asked a clarification question on task `{task_id}` '
        '(clarification {next_count} of {max_count}). Decide: answer in-scope '
        '(your reply becomes the clarification-response delivered back to Forge '
        'with `--resume`), or escalate to Larry as a plan modification by '
        'emitting a new APPROVAL_REQUEST with the revised plan.'
    ),
    'ack-proceed': (
        'Forge has emitted PROCEED on task `{task_id}` preflight. The build '
        'phase will dispatch automatically once worktree + gh pr machinery '
        'lands (commit 4b). For now, journal the proceed and stand by.'
    ),
    'reject': (
        'Forge has REJECTED task `{task_id}` at preflight. Reason: {reason}. '
        'Journal the rejection. Do not retry without addressing the reason — '
        'either revise the spec and re-emit APPROVAL_REQUEST, or set this aside.'
    ),
    'clarification-exhausted': (
        'Forge ran out of clarification budget on task `{task_id}` ({max_count} '
        'clarifications used). Final question: {reason}. The dispatch is closed; '
        'either revise the spec to remove the ambiguity and re-emit APPROVAL_REQUEST, '
        'or set this aside.'
    ),
    'dead-letter': (
        'A dispatch you originated was rejected and could not be delivered. '
        'Reason: {reason}. Journal the failure. Do not retry without addressing '
        'the rejection cause.'
    ),
    'marker-error': (
        'Your previous output on task `{task_id}` could not be parsed as a valid '
        'marker (retry {retry_count} of {max_retries}). Error: {reason}. '
        'Re-read your CLAUDE.md marker-discipline section and re-emit EXACTLY one '
        'valid marker block with the required fields and matching '
        '`=== END_XXX ===` delimiter. After {max_retries} retries the dispatch '
        'will be closed and dead-lettered back to Beacon.'
    ),
    # D3.5 commit 5a — Mirror review marker intents. D3.5 closed with 5d:
    # revision dispatch (5b), replan flow (5c), auto-merge + EMERGENCY_HALT
    # trip + cost-budget gate (5d) are all live.
    # false-success-notify-fix (2026-06-11): the merge-status line is
    # GitHub-truth-gated, NOT a hardcoded "auto-merge has fired". The old
    # template asserted the merge fired the instant Mirror PASSed — but the
    # merge runs AFTER and can be HELD (queued behind an overlapping PR),
    # CONFLICTING, or FAILED. PR #455 was held behind #454, yet Beacon was
    # told "auto-merge fired" and reported a merge that never happened.
    # `{merge_status_line}` is rendered from the gh-confirmed `merge_outcome`
    # by `_render_review_pass_merge_status_line` and only says MERGED when
    # `gh pr merge`/`gh pr view --json state` confirmed it. The auto-merge
    # now runs BEFORE this notify is built (see process_outbox) so the line
    # reflects reality. approved != auto-merge-requested != merged.
    'review-pass': (
        'Mirror has APPROVED PR `{pr_url}` on task `{task_id}`. Summary: '
        '{summary}. {merge_status_line} Journal the approval. Report the '
        'merge state EXACTLY as stated above — do NOT say the PR is merged '
        'unless that line says MERGED. Larry sees the authoritative merge '
        'outcome in his closing DM. No further action from you.'
    ),
    'review-revision': (
        'Mirror has requested REVISION on PR `{pr_url}` for task `{task_id}` '
        '({finding_count} finding(s), severity={severity}, confidence={confidence}). '
        'The revision has been auto-dispatched to Forge — she will apply '
        'the findings in her existing build session, commit + push to the '
        'same branch (PR auto-updates), and Mirror will re-review. Journal '
        'the dispatch and await the re-review outcome. No manual action '
        'from you. (D3.5 5b: revision loop auto-wired; budget enforced by '
        'max_revisions in agent-models.json loop_bounds.)'
    ),
    'review-escalate': (
        'Mirror has ESCALATED PR `{pr_url}` on task `{task_id}` '
        '(severity={severity}, confidence={confidence}). Reason: {reason}. '
        'D3.5 5c wired the auto-replan flow — apply your Shape 8 decision '
        'tree (auto-replan via fresh APPROVAL_REQUEST, push back with prose, '
        'or stand down if budget exhausted). Larry already received the '
        'closing DM via the 5a-followup pipe; your APPROVAL_REQUEST decides '
        'whether he gets a second DM with the revised plan. Bounded by '
        'max_replans on the envelope.'
    ),
    'review-emergency-halt': (
        'Mirror has flagged EMERGENCY_HALT on PR `{pr_url}` for task `{task_id}`. '
        'Reason: {reason}. Evidence: {evidence}. EMERGENCY_HALT has been '
        'tripped automatically (D3.5 5d) at ~/agents/blackboard/EMERGENCY_HALT '
        '— all four agents pause dispatching on next 5s poll. Larry has been '
        'priority-DMed via the broadcast alert channel. Journal the halt + '
        "reason; do NOT attempt any further dispatch — the halt is sticky "
        'until Larry runs `kill_switch.py resume`.'
    ),
    'cost-budget-exhausted': (
        'Cost-budget gate fired on task `{task_id}` (current ${current_usd} '
        '>= cap ${cap_usd}). Dispatch refused at: {dispatch_label}. Journal '
        'the refusal. The task is stalled until Larry decides — revise the '
        'plan, raise the cap (config loop_bounds.cost_per_task_usd), or '
        'accept the work as-is. No further automatic action from you.'
    ),
    'replan-request': (
        'A downstream agent has requested a replan on task `{task_id}`. '
        'Reason: {reason}. Decide: revise the plan inline (new APPROVAL_REQUEST '
        'with adjusted scope) or push back to the requester. Bounded by '
        'max_replans on the envelope.'
    ),
    # PR-S4 follow-up — Mirror DAG-preflight REVISION routed to Beacon for
    # autonomous self-heal (symmetric with the PASS auto-activate path). The
    # raw verdict is an agent-to-agent routing signal, NOT a Larry decision.
    'dag-preflight-revision': (
        'Mirror returned REVISION on the DAG-preflight for build sequence '
        '`{seq_id}` (sequence file: `{seq_path}`). This is an agent-to-agent '
        'routing signal, NOT a Larry decision — handle it yourself per your '
        'CLAUDE.md § "How you handle a Mirror DAG-preflight REVISION". Read '
        "Mirror's verdict below. If it is a MECHANICAL DAG fix (the common "
        'Check-3 parallel-file-overlap case → add a `depends_on` edge '
        'serializing the flagged step behind the steps it might collide '
        'with), amend the sequence file (atomic write + audit_log entry) and '
        're-dispatch the DAG-preflight (marker.py, --phase routing-signal) '
        'WITHOUT pinging Larry. If it is a SCOPE/SPEC problem you cannot '
        'mechanically resolve, escalate to Larry as a one-line binary (never '
        'the raw checks), per your escalation discipline.'
    ),
    # S2 (chain-context-durability M2) — a code-review REVIEW_REVISION whose
    # envelope carries no `forge_build_session_id` (the PR #412 class: a
    # heal-rebuilt envelope that necessarily dropped the build session) has no
    # Forge conversation to `--resume`. Instead of dead-ending in a broadcast
    # "reconcile manually" Larry alert, route it to Beacon for autonomous
    # re-dispatch with a fresh task_id — symmetric with the dag-preflight-
    # revision self-heal above. Agent-to-agent routing signal, NOT a Larry
    # decision.
    'code-review-revision-no-session': (
        'Mirror requested REVISION on PR `{pr_url}` for task `{task_id}`, but '
        'the envelope carries no `forge_build_session_id` — there is no Forge '
        'build session to `--resume` (the PR #412 class: a heal-rebuilt '
        'envelope that dropped the session). This is an agent-to-agent routing '
        'signal, NOT a Larry decision. Re-dispatch Forge with a FRESH task_id '
        "to apply Mirror's findings (below) to the EXISTING PR branch "
        '`{branch}` — a new build session that updates the SAME PR, exactly '
        'the manual recovery performed for PR #412. Emit a standard '
        'APPROVAL_REQUEST marker (target_agent: forge) whose prompt carries '
        'the findings + the existing branch/PR so Forge commits onto the same '
        'branch rather than opening a new PR. Escalate to Larry only for a '
        'genuine scope/values decision, or if the re-dispatch itself fails.'
    ),
}

# D3.5 5a-followup: per-intent DM templates for chain-completion DMs to Larry.
# Fires only for terminal-from-Larry's-perspective intents (his task ends here,
# either successfully or needing his attention). Mid-chain intents like
# ack-proceed / clarify / clarification-response do not DM because they're
# mechanics Larry doesn't need to see.
#
# Template fields drawn from the marker payload + envelope data (see
# `_render_dm_message`).
# D3.5 5d — review-pass DM is outcome-aware: the body reflects whether
# auto-merge succeeded, was already-merged (resume case), or failed. The
# `merge_outcome` value is set on the marker_decision by the auto-merge
# call site in process_outbox; _render_dm_message reads it and picks the
# correct variant via _REVIEW_PASS_DM_VARIANTS below.
DM_TEMPLATES = {
    'review-pass': (
        # Default template used when no merge_outcome is set (e.g. unit
        # tests of the render-pipeline that don't simulate the auto-merge
        # call). The marker-routing path always populates merge_outcome
        # before _maybe_dm_larry fires, so production reads from the
        # variant map below.
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Merge outcome: {merge_outcome}.'
    ),
    'review-escalate': (
        # m-6 review fix: lead line is cause-agnostic. The intent fires from
        # three scenarios — Mirror direct ESCALATE, low-confidence auto-
        # promote, budget exhausted — and the {reason} body carries the
        # specific cause. The old "Mirror escalated PR ..." lead was wrong
        # for the budget-exhaust case (Mirror said revision; system
        # downgraded). On phone, the lead is what Larry sees in the
        # notification preview.
        'Review escalated on PR {pr_url} (task `{task_id}`).\n'
        'Reason: {reason}\n'
        'Decide: revise the spec (new APPROVAL_REQUEST to Beacon) or push back.'
    ),
    'review-emergency-halt': (
        'EMERGENCY_HALT on PR {pr_url} (task `{task_id}`).\n'
        'Reason: {reason}\n'
        'Evidence: {evidence}\n'
        'Review the PR immediately and decide whether to close without merge.'
    ),
    'reject': (
        'Forge REJECTED task `{task_id}` at preflight.\n'
        'Reason: {reason}\n'
        'Either revise the spec and re-emit APPROVAL_REQUEST, or set aside.'
    ),
    'clarification-exhausted': (
        'Forge exhausted clarification budget on task `{task_id}`.\n'
        'Final question: {reason}\n'
        'Rewrite the spec more completely before re-dispatching.'
    ),
    # D3.5 5b-followup Bug C: dead-letter is now a terminal-from-Larry's-
    # perspective intent. The 5a-followup auto-DM pipe needs to surface
    # cascade-exhaust events (Forge/Mirror marker-error retries hit the
    # cap; dispatch closed; no PR opened) so the chat thread that initiated
    # the work gets a closing notification — not silence. Without this,
    # Larry approved-then-nothing for the failed 5b live test.
    'dead-letter': (
        'Dispatch on task `{task_id}` failed after {retry_count} '
        'marker-error retries. The dispatch is closed; no PR was opened.\n'
        'Reason: {reason}\n'
        'Either revise the spec (sharper instructions or a different '
        'approach) and re-emit APPROVAL_REQUEST to Beacon, or set this '
        'task aside.'
    ),
    # D3.5 5d — cost-budget cap fired before dispatch. Terminal-from-Larry's-
    # perspective: the task is stalled and won't progress without his
    # intervention (revise plan, raise cap, or accept as-is).
    'cost-budget-exhausted': (
        'Cost-budget cap fired on task `{task_id}` (current ${current_usd} '
        '>= cap ${cap_usd}).\n'
        'Refused dispatch: {dispatch_label}\n'
        'The task is stalled. Revise the spec to scope down, raise the cap '
        '(config/agent-models.json loop_bounds.cost_per_task_usd), or accept '
        'the work-so-far as-is.'
    ),
}

# D3.5 5d — outcome-aware DM body variants for review-pass. Selected in
# `_render_dm_message` based on `decision['merge_outcome']`. The variants
# fall back to DM_TEMPLATES['review-pass'] on missing/unknown outcome so
# the render pipeline never crashes mid-merge — degrade to silent-or-vague
# rather than wedge the daemon.
#
# KEEP IN SYNC with `_render_review_pass_merge_status_line` below, which maps
# the SAME `merge_outcome` values to the one-sentence merge line in the Beacon
# inter-agent notify (this dict is Larry's phone DM body; that function is the
# agent journal line). A new outcome must be added in BOTH places or one
# surface will render the generic fallback while the other is specific.
_REVIEW_PASS_DM_VARIANTS: dict[str, str] = {
    'merged': (
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Auto-merged + branch deleted.'
    ),
    'already_merged': (
        # Resume-after-crash success path. Identical user-visible message
        # to `merged` — the resume distinction is in the log/audit trail,
        # not load-bearing for Larry.
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Auto-merged + branch deleted.'
    ),
    'failed': (
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Auto-merge FAILED: {merge_reason}\n'
        'Merge manually: gh pr merge {pr_number} --repo {repo_coords} '
        '--squash --delete-branch'
    ),
    # D3.5 5d-prime — serializer gate 1 held the merge behind an
    # overlapping in-flight PR. The retry fires automatically when the
    # blocker resolves (post-merge release pass OR the 5-min queue sweep
    # if the blocker was closed externally). fix-review-pass-dm-await-
    # merge-outcome (2026-05-26): body now names the overlap files so
    # Larry can see WHY the hold fired, and a second DM fires on the
    # release with the final outcome (merged/failed) so the chain isn't
    # silent after the queue clears.
    'held_for_blocker': (
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Auto-merge HELD behind PR #{blocker_pr_number} on overlap files: '
        '{overlap_files}.\n'
        'Will retry automatically when the blocker resolves.'
    ),
    # Spec alias for `held_for_blocker` — same body. Kept so the variant
    # map matches the spec vocabulary even though the gates fn always
    # emits `held_for_blocker` as the outcome value.
    'queued_behind_serializer': (
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Auto-merge HELD behind PR #{blocker_pr_number} on overlap files: '
        '{overlap_files}.\n'
        'Will retry automatically when the blocker resolves.'
    ),
    # D3.5 5d-prime — serializer gate 2 saw `gh pr view --json mergeable`
    # = CONFLICTING. We did NOT fire `gh pr merge` (that's guaranteed to
    # fail); instead surface the canonical rebase command so Larry can
    # resolve in one shot. The healer (heal_pr_auto_merge.py) won't fire
    # either because there's no AUTO_MERGE_FAILED log line to scan.
    'held_conflict': (
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Auto-merge BLOCKED: PR has merge conflicts with main.\n'
        'Rebase manually: gh pr checkout {pr_number} && git fetch origin '
        '&& git rebase origin/main && git push --force-with-lease'
    ),
    # fix-auto-merge-freshness-revalidation — a held PR's approval went
    # stale when its blocker merged (main moved), and the release-path
    # re-validation against current main failed (regression or unverifiable).
    # The canonical closing DM is `_dm_larry_stale_revalidation` (fired in
    # the gate) and `_maybe_dm_larry` is suppressed for this outcome; this
    # variant is the render-pipeline fallback so the body never degrades to
    # a misleading "auto-merged" message.
    'held_stale_regression': (
        'Mirror approved PR {pr_url} on task `{task_id}`, but that approval '
        'predates a base change.\n'
        'Summary: {summary}\n'
        'Auto-merge HELD: re-validation against current main failed — '
        '{regression_detail}.\n'
        'Rebase + re-review before merging: gh pr checkout {pr_number} && '
        'git fetch origin && git rebase origin/main && git push '
        '--force-with-lease'
    ),
    # merge-gate-deep-review-hold — a critical-path PR (approval/resolve
    # fan-out or the trust/merge machinery) PASS'd Mirror but reached
    # auto-merge WITHOUT a `deep-review-passed` stamp, so the `/code-review
    # high` step was skipped. NOT merged. The canonical closing DM is
    # `_dm_larry_deep_review_hold` (fired in the gate) and `_maybe_dm_larry`
    # is suppressed for this outcome; this variant is the render-pipeline
    # fallback so the body never degrades to a misleading "auto-merged".
    'held_deep_review': (
        'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        'Summary: {summary}\n'
        'Auto-merge HELD: critical-path change with no deep-review stamp — '
        'needs a `/code-review high` first.\n'
        'Review, then merge: scripts/merge_reviewed_pr.sh {pr_number}'
    ),
}


def _render_review_pass_merge_status_line(
    merge_result: Optional[dict[str, Any]],
) -> str:
    """GitHub-truth merge-status sentence for the review-pass inter-agent notify.

    false-success-notify-fix (2026-06-11). The review-pass notify to Beacon
    is informational, but it MUST NOT claim a merge that hasn't happened.
    This renders a single sentence from the gh-confirmed `merge_outcome`
    produced by `_attempt_auto_merge_with_gates` — the same outcome that
    drives Larry's outcome-aware closing DM (`_REVIEW_PASS_DM_VARIANTS`).

    The word "MERGED" appears ONLY for `merged` / `already_merged`, which the
    merge path returns ONLY after `gh pr merge` exits 0 or `gh pr view --json
    state` confirms MERGED. Every held/queued/failed/pending outcome says, in
    plain words, that the PR is NOT merged — so the receiver can never echo
    "auto-merge fired" for a PR that is only queued behind a blocker (the
    PR #455-held-behind-#454 incident). A missing/unknown outcome degrades to
    "requested; outcome in Larry's DM" — never a success claim.

    Distinguishes the three states the incident conflated:
      approved        — Mirror PASSed (always true on this path)
      auto-merge-requested / queued / held — merge attempted, not done
      merged          — gh confirmed the merge
    """
    outcome = merge_result.get('merge_outcome') if isinstance(merge_result, dict) else None
    if outcome in ('merged', 'already_merged'):
        return 'Auto-merge fired and the PR is now MERGED (branch deleted).'
    if outcome in ('held_for_blocker', 'queued_behind_serializer'):
        blocker = merge_result.get('blocker_pr_number', '?')
        overlap = merge_result.get('overlap_files', 'overlapping files')
        return (
            f'Auto-merge is QUEUED behind PR #{blocker} (overlap on '
            f'{overlap}) — it will merge automatically once that PR clears. '
            f'The PR is NOT merged yet.'
        )
    if outcome == 'held_conflict':
        return (
            'Auto-merge is BLOCKED — the PR conflicts with main and needs a '
            'manual rebase. The PR is NOT merged.'
        )
    if outcome == 'held_deep_review':
        return (
            'Auto-merge is HELD — this is a critical-path change and needs a '
            '`/code-review high` before it can merge. The PR is NOT merged.'
        )
    if outcome == 'held_fail_closed':
        return (
            'Auto-merge is HELD — the merge queue is fail-closed on a corrupt '
            'state file and an operator must clear it. The PR is NOT merged.'
        )
    if outcome == 'deferred_unknown':
        return (
            'Auto-merge is PENDING — GitHub is still computing mergeability; '
            'the queue sweep will retry. The PR is NOT merged yet.'
        )
    if outcome == 'failed':
        reason = merge_result.get('merge_reason', 'see logs') if isinstance(merge_result, dict) else 'see logs'
        return (
            f'Auto-merge FAILED ({reason}) — the PR is NOT merged and needs '
            f'manual attention.'
        )
    # No merge_result attached (defensive — the marker-routing path attaches
    # one before this notify renders) or an unrecognized outcome: never assert
    # success. State it as requested-and-pending and defer to Larry's DM.
    return (
        "Auto-merge has been REQUESTED; its outcome is reported in Larry's "
        'closing DM. The PR is NOT confirmed merged by this notify.'
    )


# Subset of intents that produce a closing DM to the originating chat. Other
# intents are mid-chain mechanics that don't warrant Larry's attention.
# D3.5 5d: `review-emergency-halt` is intentionally excluded from this set
# even though it has a DM_TEMPLATES entry — the priority broadcast alert
# fired by `_trip_emergency_halt` is strictly more informative (it carries
# the recovery command + broadcasts to all authorized chats, not just the
# originating one). A targeted closing DM on top would be a duplicate
# notification with stale "decide whether to close without merge" wording
# (the halt is sticky; the action is `kill_switch.py resume`, not closing
# the PR). The DM_TEMPLATES['review-emergency-halt'] entry is kept for
# backwards-compat with any future code path that wants to render the body
# explicitly (e.g. an operating-manual sample), but the auto-pipe skips it.
TERMINAL_DM_INTENTS = frozenset(DM_TEMPLATES.keys()) - {'review-emergency-halt'}


def _render_dm_message(intent: str, decision: dict[str, Any]) -> Optional[str]:
    """Render the per-intent DM body. Returns None on missing template or
    unrenderable fields (degrades to silence rather than crashing the daemon).

    D3.5 5d: when intent == 'review-pass', the body picks an outcome-aware
    variant from `_REVIEW_PASS_DM_VARIANTS` based on
    `decision['merge_outcome']`. Falls back to the canonical
    `DM_TEMPLATES['review-pass']` template if no outcome is set or the
    outcome key is unknown — defensive against tests/mid-rollout state
    where the auto-merge path hasn't populated the field.
    """
    template = DM_TEMPLATES.get(intent)
    if template is None:
        return None
    # D3.5 5d — outcome-aware review-pass template selection.
    if intent == 'review-pass':
        merge_outcome = decision.get('merge_outcome')
        if isinstance(merge_outcome, str):
            variant = _REVIEW_PASS_DM_VARIANTS.get(merge_outcome)
            if variant is not None:
                template = variant
    payload = decision.get('payload') or {}
    intent_kwargs = decision.get('intent_kwargs') or {}
    # Merge envelope payload + intent_kwargs so the template can pull from
    # either. intent_kwargs wins on conflicts (the classifier already
    # normalized fields like pr_url + finding_count + reason).
    fields: dict[str, Any] = {
        'task_id': payload.get('task_id', '?'),
        'pr_url': payload.get('pr_url', '?'),
        'summary': payload.get('summary', '?'),
        'reason': payload.get('reason', '?'),
        'evidence': payload.get('evidence', '?'),
        'severity': payload.get('severity', '?'),
        'confidence': payload.get('confidence', '?'),
        'finding_count': 0,
        # D3.5 5d — auto-merge outcome fields; the `failed` variant needs
        # all three. Default '?' so missing-field formatting doesn't
        # raise — degrade to a vague body, never crash the daemon.
        'merge_outcome': '?',
        'merge_reason': '?',
        'pr_number': '?',
        'repo_coords': '?',
        # D3.5 5d — cost-budget DM fields.
        'current_usd': '?',
        'cap_usd': '?',
        'dispatch_label': '?',
        # D3.5 5d-prime — serializer hold-for-blocker DM field.
        'blocker_pr_number': '?',
        # fix-review-pass-dm-await-merge-outcome — overlap files for the
        # serializer-hold DM body so Larry sees WHICH files collided.
        'overlap_files': '?',
        # fix-auto-merge-freshness-revalidation — stale-release detail.
        'regression_detail': '?',
    }
    findings = payload.get('findings')
    if isinstance(findings, list):
        fields['finding_count'] = len(findings)
    # D3.5 5d — surface merge_result fields if attached by the auto-merge
    # call site. Same shape as intent_kwargs: classifier-populated values
    # win over payload defaults.
    merge_result = decision.get('merge_result')
    if isinstance(merge_result, dict):
        for k in (
            'merge_outcome', 'merge_reason', 'pr_number', 'repo_coords',
            'blocker_pr_number', 'overlap_files', 'regression_detail',
        ):
            v = merge_result.get(k)
            if v is not None:
                fields[k] = v
    # intent_kwargs from classifier may have pre-rendered values (e.g.
    # auto-promote reason). Apply on top.
    fields.update({k: v for k, v in intent_kwargs.items() if v is not None})
    try:
        return template.format(**fields)
    except (KeyError, IndexError):
        # Missing field — degrade rather than crash.
        return None


def _maybe_dm_larry(
    data: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    """Append a chain-completion DM to larry-alerts.jsonl when the marker
    intent is terminal-from-Larry's-perspective AND the source envelope
    carries reply_chat_id.

    Phase D3.5 5a-followup. Fires AFTER the marker-driven inter-agent
    notify has been written; this is purely the Larry-facing side. Failure
    here is non-fatal (log + continue) — the inter-agent chain has already
    completed, the DM is the courtesy notification.

    The reply_chat_id propagation through every hop (per dispatch_build_phase
    + dispatch_mirror_review + marker-routing + default-routing blocks above)
    keeps the chat thread reachable from the terminal hop.
    """
    intent = decision.get('intent')
    if intent not in TERMINAL_DM_INTENTS:
        return
    # fix-review-pass-dm-await-merge-outcome (2026-05-26):
    # Suppress the closing review-pass DM when the merge step hasn't
    # produced a final outcome yet, OR when the conflict path already
    # fired the canonical rebase DM. The eventual final outcome reaches
    # Larry from the queue-sweep retry / release path via
    # `_fire_review_pass_outcome_dm`.
    #   * deferred_unknown — mergeable=UNKNOWN on first attempt; sweep
    #     retries with second_attempt_on_unknown and DMs on resolution.
    #   * held_conflict — `_dm_larry_rebase_needed` already queued the
    #     rebase recipe (includes Mirror summary); a second DM here
    #     would be a duplicate.
    if intent == 'review-pass':
        merge_outcome = decision.get('merge_outcome')
        # held_stale_regression — fix-auto-merge-freshness-revalidation: the
        # canonical closing DM is `_dm_larry_stale_revalidation`, fired in the
        # gate; a second DM here would duplicate it (same pairing as
        # held_conflict / `_dm_larry_rebase_needed`).
        #   * release_already_merged — fix-auto-merge-already-merged-skip: the
        #     released PR was ALREADY merged/closed, so Larry already got the
        #     `merged` closing DM when it actually merged; a second DM on this
        #     skip path would be duplicate noise (actionable-only discipline).
        #   * held_deep_review — merge-gate-deep-review-hold: the canonical
        #     closing DM is `_dm_larry_deep_review_hold` (fired in the gate);
        #     a second DM here would duplicate it (same pairing as
        #     held_conflict / `_dm_larry_rebase_needed`).
        if merge_outcome in (
            'deferred_unknown', 'held_conflict', 'held_stale_regression',
            'release_already_merged', 'held_deep_review',
        ):
            log(
                f'review-pass closing DM suppressed (outcome='
                f'{merge_outcome}); final DM fires on retry/conflict '
                f'path (task={data.get("task_id", "?")})',
            )
            return
    chat_id = data.get('reply_chat_id')
    if chat_id is None:
        # No originating chat thread (autonomous Pulse-initiated runs, or
        # Bug A unfixed). Silent skip — no DM target.
        return
    if not isinstance(chat_id, int):
        log(
            f'reply_chat_id is not an int ({type(chat_id).__name__}) on task '
            f'{data.get("task_id", "?")}; skipping completion DM',
            'WARN',
        )
        return
    message = _render_dm_message(intent, decision)
    if message is None:
        log(
            f'DM template missing or unrenderable for intent={intent} on task '
            f'{data.get("task_id", "?")}; skipping completion DM',
            'WARN',
        )
        return
    task_id = data.get('task_id', 'unknown')
    ok = larry_alerts.append_notification(
        source='outbox-notifier',
        intent=intent,
        message=message,
        chat_id=chat_id,
        task_id=task_id,
    )
    if ok:
        log(
            f'queued completion DM to chat {chat_id} for intent={intent} '
            f'(task={task_id})',
        )
    else:
        log(
            f'completion DM append failed for chat {chat_id} (intent={intent}, '
            f'task={task_id}); inter-agent chain completed normally, DM dropped',
            'WARN',
        )


def _maybe_dm_larry_direct_synth(
    data: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    """Emit a synthesized closing DM for larry-direct dispatches whose
    intent isn't in `TERMINAL_DM_INTENTS` and has no follow-up handler.

    Normal beacon-sourced flow either has a dispatcher to notify (Beacon)
    or a follow-up dispatch helper (build_phase, revision_to_forge) that
    advances the chain. Larry-direct dispatches with neither need a DM as
    the only closing signal.

    task-19 (2026-05-19): body branches on intent. Earlier hardcoded
    "Mirror requested revision" wording rendered for every non-terminal
    intent — including Forge `ack-proceed` — because PR #46's larry-direct
    branch fired for all markers, not just the ones lacking a follow-up.
    The caller now gates this on `marker_type` so PROCEED / clean
    REVIEW_REVISION never reach here (their dispatch helpers carry the
    chain forward). The intent-specific bodies below cover the residual
    cases (today: REVIEW_REVISION auto-promoted/budget-exhausted is
    re-routed to escalate → terminal DM, so nothing actually reaches the
    `review-revision` arm in production; arm kept for defensive parity).

    Failure here is non-fatal — auto-merge has either fired or not, the
    archive happens regardless.
    """
    intent = decision.get('intent') or 'unknown'
    chat_id = data.get('reply_chat_id')
    task_id = data.get('task_id', 'unknown')
    payload = decision.get('payload') or {}
    pr_url = payload.get('pr_url') if isinstance(payload, dict) else None
    findings = payload.get('findings') if isinstance(payload, dict) else None
    n_findings = len(findings) if isinstance(findings, list) else 0
    if intent == 'review-revision':
        message = (
            f'Mirror requested revision on PR {pr_url or "(no pr_url)"} '
            f'on task `{task_id}`. {n_findings} finding(s). Larry-direct '
            f'dispatch: no Forge target — review the findings in the PR '
            f'and decide whether to revise the spec or apply the fix '
            f'manually.'
        )
    elif intent == 'ack-proceed':
        message = (
            f'Forge accepted preflight on task `{task_id}` (PROCEED). '
            f'Larry-direct dispatch: build phase auto-dispatched; await '
            f'the PR-opened DM when build completes.'
        )
    elif intent == 'clarify':
        question = None
        if isinstance(payload, dict):
            question = payload.get('question')
        message = (
            f'Forge needs clarification on task `{task_id}` but there is '
            f'no upstream dispatcher to answer. '
            f'Question: {question or "(see Forge outbox)"}. Either '
            f're-dispatch via Beacon or answer manually.'
        )
    else:
        message = (
            f'Larry-direct dispatch result on task `{task_id}` '
            f'(intent={intent}). No upstream dispatcher and no closing '
            f'template for this intent — review the agent outbox manually.'
        )
    try:
        ok = larry_alerts.append_notification(
            source='outbox-notifier',
            intent=intent,
            message=message,
            chat_id=chat_id,
            task_id=task_id,
        )
        if ok:
            log(
                f'queued larry-direct synth DM to chat {chat_id} '
                f'for intent={intent} (task={task_id})',
            )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'larry-direct synth DM append failed for chat {chat_id} '
            f'(intent={intent}): {type(e).__name__}: {e}',
            'WARN',
        )


_running = True

# Monotonic-ish last-run marker for the reconciliation sweep throttle (Part B).
# 0.0 means "never run", so the first poll after startup runs the sweep once.
_last_reconcile_ts = 0.0


def log(msg: str, level: str = 'INFO', ts: Optional[str] = None) -> None:
    # The stamp is NAIVE HOST-LOCAL time (the droplet runs America/Denver,
    # not UTC). chain_event_shipper._normalize_iso_ts and
    # heal_pr_auto_merge._to_utc interpret naive timestamps as host-local —
    # don't switch this to UTC without migrating every log reader at once.
    #
    # `ts` lets a caller pin the exact stamp written here. The auto_merge
    # push-emit (S-4) needs the log line's ts and its own pushed row to share
    # one stamp so the shipper's later poll of this same line computes an
    # identical event_id (PK dedup) instead of double-writing the merge event.
    if ts is None:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] [notifier] [{level}] {msg}\n'
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line)
    except OSError:
        pass
    sys.stderr.write(line)


def _emergency_halt_active() -> bool:
    return EMERGENCY_HALT_FLAG.exists()


def ensure_dirs() -> None:
    for agent in AGENT_IDS:
        (OUTBOXES_ROOT / agent / '.archive').mkdir(parents=True, exist_ok=True)
        (INBOXES_ROOT / agent).mkdir(parents=True, exist_ok=True)
        (INBOXES_ROOT / agent / '.invalid').mkdir(parents=True, exist_ok=True)
    (AGENTS_ROOT / 'state').mkdir(parents=True, exist_ok=True)
    (AGENTS_ROOT / 'logs').mkdir(parents=True, exist_ok=True)


def _primary_agent_id(source: str) -> Optional[str]:
    """Return the bare agent name from a source field, or None if not an agent.

    Examples:
      'pulse'                -> 'pulse'
      'forge-result'         -> 'forge'
      'forge-question'       -> 'forge'
      'beacon-clarification' -> 'beacon'
      'telegram-webhook'     -> None
      'cron'                 -> None
    """
    for agent in AGENT_IDS:
        if source == agent or source.startswith(f'{agent}-'):
            return agent
    return None


def _should_notify_back(source: str, processing_agent: str) -> bool:
    """Decide whether an outbox warrants a notify back to its source.

    True iff source is a bare agent name OR a `*-question` source, AND
    the source's primary agent is not the processing agent itself
    (self-dispatch loops don't notify).
    """
    primary = _primary_agent_id(source)
    if primary is None:
        return False
    if primary == processing_agent:
        return False
    is_bare = source == primary
    is_question = source.endswith('-question')
    return is_bare or is_question


def _notify_back_source(processing_agent: str, original_source: str) -> str:
    """Derive the source field on the notify task.

    `<agent>-clarification` for `*-question` originals (we answered a question),
    `<agent>-result` for everything else (we completed work).
    """
    if original_source.endswith('-question'):
        return f'{processing_agent}-clarification'
    return f'{processing_agent}-result'


def _current_notify_depth(outbox_data: dict[str, Any]) -> int:
    """Compute the current cascade depth for a result being processed.

    Two signals:
      - Explicit `_notify_depth` field (propagated through prior notify hops).
      - `source_task_file` filename starts with `notify-` (the inbox task that
        produced this outbox was itself a notify cascade).
    """
    depth = outbox_data.get('_notify_depth')
    if isinstance(depth, int):
        return depth
    src_task_file = outbox_data.get('source_task_file', '')
    if src_task_file and Path(src_task_file).stem.startswith('notify-'):
        return 1
    return 0


def _truncate(text: str, limit: int = MAX_RESULT_TEXT_CHARS) -> str:
    if not text:
        return ''
    if len(text) <= limit:
        return text
    return text[:limit] + f'\n\n[... truncated, {len(text) - limit} more chars]'


def build_notify_prompt(
    *,
    intent: str,
    sender: str,
    task_id: str,
    success: bool,
    output: str,
    error: str = '',
    intent_kwargs: Optional[dict[str, Any]] = None,
) -> str:
    """Render the notify prompt for a receiver agent.

    Uses NOTIFY_TEMPLATE (header + framing) + INTENT_ACTION_BLOCKS[intent]
    (the action verb the receiver should apply). Falls back to
    `result-notification` action block if the intent is unrecognized.

    Output is the sender's claude output (truncated to MAX_RESULT_TEXT_CHARS);
    error appended if present. Result is padded to dispatch_validator's
    MIN_PROMPT_LEN floor.
    """
    intent_kwargs = dict(intent_kwargs or {})
    intent_kwargs.setdefault('task_id', task_id)
    action_template = INTENT_ACTION_BLOCKS.get(
        intent, INTENT_ACTION_BLOCKS['result-notification'],
    )
    try:
        intent_block = action_template.format(**intent_kwargs)
    except (KeyError, IndexError):
        # Missing format key — degrade gracefully instead of crashing the daemon.
        intent_block = action_template
        log(
            f'notify-prompt intent_kwargs incomplete for intent={intent}; '
            f'rendered without substitution',
            'WARN',
        )

    body_output = _truncate(output or '')
    if error:
        body_output = (body_output + '\n\n' if body_output else '') + f'Error: {error}'

    status = 'SUCCESS' if success else 'FAILED'
    prompt = NOTIFY_TEMPLATE.format(
        intent=intent,
        sender=sender,
        task_id=task_id,
        status=status,
        intent_block=intent_block,
        output=body_output or '(no output captured)',
    )
    # dispatch_validator requires MIN_PROMPT_LEN chars. The template + intent
    # block usually clears it, but truncated/empty outputs can still fall short.
    if len(prompt.strip()) < dispatch_validator.MIN_PROMPT_LEN:
        prompt += '\n\n— end of notify (auto-padded to clear validator floor) —'
    return prompt


def _archive_outbox(outbox_file: Path) -> None:
    archive_dir = outbox_file.parent / '.archive'
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / outbox_file.name
    # Don't clobber prior archives; suffix with a counter on collision.
    counter = 1
    while target.exists():
        target = archive_dir / f'{outbox_file.stem}.{counter}{outbox_file.suffix}'
        counter += 1
    shutil.move(str(outbox_file), str(target))


def _emit_clarify_request_chain_event(
    data: dict[str, Any],
    marker_decision: dict[str, Any],
    *,
    agent: str,
) -> None:
    """Push a `clarify_request` chain_event for spec § 4 source #5.

    Payload per spec § 4:
      - asking_agent: 'forge' or (future) 'mirror'
      - task_id: the dispatch task being clarified
      - question: the marker payload's question text
      - resume_session_id: the session_id the clarification answer must
        --resume against (Forge resumes her preflight with the answer)

    Best-effort: chain_event_emit.emit_event logs WARN and returns False
    on Supabase outage; the daemon's marker-routing path does NOT depend
    on the row landing, so failure here doesn't affect Forge's resume
    cascade. The clarify question itself still reaches Beacon through the
    inter-agent notify path immediately below.
    """
    payload = marker_decision.get('payload') or {}
    if not isinstance(payload, dict):
        payload = {}
    chain_payload = {
        'asking_agent': agent,
        'task_id': data.get('task_id', ''),
        'question': payload.get('question', ''),
        'resume_session_id': data.get('claude_session_id', '') or '',
    }
    try:
        chain_event_emit.emit_event(
            event_type='clarify_request',
            agent=agent,
            task_id=data.get('task_id'),
            payload=chain_payload,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'clarify_request chain_event emit raised unexpectedly for '
            f'task {data.get("task_id")!r}: {type(e).__name__}: {e}',
            'WARN',
        )


def _emit_clarify_response_chain_event(
    *,
    task_id: str,
    question: str,
    answer: str,
    clarification_round: int,
) -> None:
    """Push a `clarify_response` chain_event for clarify-round-visibility § 6.

    Sibling to `_emit_clarify_request_chain_event`. Fires from
    `_handle_beacon_clarification_response` after Beacon's answer has been
    successfully written to Forge's inbox as the resume continuation.

    Payload:
      - task_id: original Forge task (stripped of `notify-` prefix)
      - clarification_round: 1-indexed, sourced from envelope's
        `clarification_count` (incremented when Forge first emitted
        CLARIFY_REQUEST, propagated through Beacon's round-trip)
      - question: the inbound clarify prompt from Forge (full notify body;
        the dashboard renderer extracts what it needs)
      - answer: Beacon's verbatim response text
      - responded_at: ISO-8601 UTC at emit time

    Best-effort: emit_event logs WARN and returns False on Supabase outage;
    the resume cascade does NOT depend on the row landing. Same daemon-
    never-wedge invariant as the request-side helper.
    """
    chain_payload = {
        'task_id': task_id,
        'clarification_round': clarification_round,
        'question': question,
        'answer': answer,
        'responded_at': datetime.now(timezone.utc).isoformat(),
    }
    try:
        chain_event_emit.emit_event(
            event_type='clarify_response',
            agent='beacon',
            task_id=task_id,
            payload=chain_payload,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'clarify_response chain_event emit raised unexpectedly for '
            f'task {task_id!r}: {type(e).__name__}: {e}',
            'WARN',
        )


# check-x-verdict-emission: map a classified Forge preflight marker to one of
# the three preflight_* chain_event types. `preflight_proceed/clarify/reject`
# already exist in chain_event_shipper.KNOWN_EVENT_TYPES but no writer emitted
# them; this closes that gap. The budget-exhausted clarify is structurally a
# reject (routed back via the reject channel), so it maps to preflight_reject —
# same discipline as the clarify_request emit, which fires only on the in-
# budget `intent == 'clarify'` path.
def _preflight_outcome_event_type(marker_decision: dict[str, Any]) -> Optional[str]:
    marker_type = marker_decision.get('marker_type')
    if marker_type == 'proceed':
        return 'preflight_proceed'
    if marker_type == 'reject':
        return 'preflight_reject'
    if marker_type == 'clarify_request':
        return (
            'preflight_clarify'
            if marker_decision.get('intent') == 'clarify'
            else 'preflight_reject'
        )
    return None


def _sync_clarify_exhausted_signal(
    data: dict[str, Any], marker_decision: dict[str, Any],
) -> None:
    """§5.2: maintain the durable for-Larry record for a CLARIFY-exhausted build.

    Rides the existing terminal-intent handler (no new poll):

      * intent == 'clarification-exhausted' → write a self-clearing record
        (task_id, last clarification question, repo) so the build appears on the
        Waiting-on-You panel + rings the doorbell.
      * any OTHER classified Forge marker for the same task_id → a fresh dispatch
        was observed (Beacon re-dispatched, or Larry dropped + re-asked), so the
        record self-clears (decision d). A no-op when no record exists.

    Best-effort: a durable-signal failure must never block notify routing.
    """
    task_id = data.get('task_id')
    if not task_id:
        return
    key = for_larry_signal.CLARIFY_EXHAUSTED_KEY_PREFIX + str(task_id)
    try:
        if marker_decision.get('intent') == 'clarification-exhausted':
            payload = marker_decision.get('payload') or {}
            question = payload.get('question') or '(no question text recorded)'
            for_larry_signal.upsert_record(key, {
                'id': str(task_id),
                'task_id': task_id,
                'headline': f'Forge is stuck on `{task_id}`',
                'context': 'Exhausted its questions — needs a scope decision',
                'suggested_action': f'Last question: {question}',
                'repo': data.get('target_repo'),
                'severity': 'warning',
                'source_kind': 'clarify-exhausted',
            })
        else:
            for_larry_signal.resolve_record(key)
    except Exception as e:  # noqa: BLE001 — durable signal is best-effort
        log(
            f'for-larry clarify-exhausted signal sync failed for '
            f'{task_id!r}: {type(e).__name__}: {e}',
            'WARN',
        )


def _emit_preflight_outcome_chain_event(
    data: dict[str, Any],
    marker_decision: dict[str, Any],
    *,
    agent: str,
) -> None:
    """Push a `preflight_<outcome>` chain_event for a classified Forge marker.

    Sibling to `_emit_clarify_request_chain_event` — fires at the same
    classification site, immediately after `_classify_forge_marker` succeeds.
    Records the Forge preflight outcome (proceed / clarify / reject) so Check X
    and the dashboard can read the preflight-outcome mix from chain_events.

    Payload:
      - agent: 'forge'
      - task_id: the dispatch task
      - marker_type: the raw classified marker_type (forensics)
      - intent: the routed intent (forensics; e.g. clarification-exhausted)

    Best-effort: chain_event_emit.emit_event logs WARN and returns False on
    Supabase outage. ADDITIVE — the daemon's marker-routing / notify / build-
    dispatch path does NOT depend on the row landing, so failure here never
    blocks, delays, or alters the existing flow. Same daemon-never-wedge
    invariant as the clarify helpers.
    """
    event_type = _preflight_outcome_event_type(marker_decision)
    if event_type is None:
        return
    chain_payload = {
        'agent': agent,
        'task_id': data.get('task_id', ''),
        'marker_type': marker_decision.get('marker_type', ''),
        'intent': marker_decision.get('intent', ''),
    }
    try:
        chain_event_emit.emit_event(
            event_type=event_type,
            agent=agent,
            task_id=data.get('task_id'),
            payload=chain_payload,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'{event_type} chain_event emit raised unexpectedly for '
            f'task {data.get("task_id")!r}: {type(e).__name__}: {e}',
            'WARN',
        )


# check-x-verdict-emission: map a classified Mirror review marker to one of the
# three verdict chain_event types, keyed on the ROUTED intent rather than the
# raw marker_type. This deliberately folds the two REVISION→ESCALATE paths
# (low-confidence auto-promote and budget-exhaustion) into review_escalate:
# from the chain's perspective both terminate the self-heal loop and hand off
# to Beacon, which is the quality signal Check X's escalate_rate watches. A
# clean in-budget REVISION keeps intent 'review-revision' and is recorded as
# such. REVIEW_EMERGENCY_HALT (intent not one of the three) is a non-PASS
# terminal safety trip and folds into review_escalate; the raw marker_type
# rides in the payload for forensics.
def _mirror_verdict_event_type(marker_decision: dict[str, Any]) -> Optional[str]:
    intent = marker_decision.get('intent')
    if intent == 'review-pass':
        return 'review_pass'
    if intent == 'review-revision':
        return 'review_revision'
    if intent == 'review-escalate':
        return 'review_escalate'
    # Defensive: any other terminal Mirror verdict (e.g. emergency-halt) is a
    # non-PASS outcome — bucket it as an escalation for the verdict mix.
    marker_type = marker_decision.get('marker_type')
    if marker_type in ('review_escalate', 'review_emergency_halt'):
        return 'review_escalate'
    return None


def _emit_mirror_verdict_chain_event(
    data: dict[str, Any],
    marker_decision: dict[str, Any],
    *,
    agent: str,
) -> None:
    """Push a `review_<verdict>` chain_event for a classified Mirror marker.

    Sibling to `_emit_preflight_outcome_chain_event`. Fires at the Mirror
    classification site, immediately after `_classify_mirror_marker` succeeds
    (and the emergency-halt trip), BEFORE the routing / auto-merge block — so a
    PASS verdict is recorded at the verdict moment even when the subsequent
    merge is held in the auto-merge queue behind a blocker.

    Payload:
      - agent: 'mirror'
      - task_id: the reviewed task
      - verdict: 'pass' | 'revision' | 'escalate' (the verdict-mix bucket)
      - marker_type: the raw classified marker_type (forensics)
      - auto_promoted / budget_exhausted: REVISION→ESCALATE provenance
      - pr_url: from the marker payload when present

    Best-effort + ADDITIVE: emit_event logs WARN and returns False on Supabase
    outage; the notify / auto-merge / escalation flow does NOT depend on the
    row landing, so failure here never blocks, delays, or alters it. Same
    daemon-never-wedge invariant as the clarify helpers.
    """
    event_type = _mirror_verdict_event_type(marker_decision)
    if event_type is None:
        return
    payload = marker_decision.get('payload') or {}
    if not isinstance(payload, dict):
        payload = {}
    chain_payload = {
        'agent': agent,
        'task_id': data.get('task_id', ''),
        'verdict': event_type.split('_', 1)[1],  # pass | revision | escalate
        'marker_type': marker_decision.get('marker_type', ''),
        'auto_promoted': bool(marker_decision.get('auto_promoted')),
        'budget_exhausted': bool(marker_decision.get('budget_exhausted')),
        'pr_url': payload.get('pr_url', '') or data.get('pr_url', '') or '',
    }
    try:
        chain_event_emit.emit_event(
            event_type=event_type,
            agent=agent,
            task_id=data.get('task_id'),
            payload=chain_payload,
            pr_url=chain_payload['pr_url'] or None,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'{event_type} chain_event emit raised unexpectedly for '
            f'task {data.get("task_id")!r}: {type(e).__name__}: {e}',
            'WARN',
        )


def _emit_review_request_chain_event(
    task_id: str,
    pr_url: str,
    *,
    revision_count: int,
    replan_count: int,
) -> None:
    """Push a `review_request` chain_event after a Mirror review dispatch.

    forge-queue-in-review-lane: the dashboard's Forge queue derives its
    "In review" lane from chain_events rows with agent='forge' and
    event_type='review_request' (dashboard_api._derive_in_review). The
    shipper defined a REVIEW_REQUEST log-line format for this event but no
    producer was ever written — and the shipper's log parser can't consume
    this module's `[ts] [notifier] [LEVEL]` line shape anyway — so the lane
    sat permanently empty. Push-emit at the dispatch site instead, the same
    proven pathway as `_emit_mirror_verdict_chain_event`.

    Fires from `_dispatch_mirror_review` (revision_count=0) and
    `_dispatch_mirror_review_rerun` (revision_count=round N), AFTER the
    inbox write succeeds — a rejected/duplicate dispatch emits nothing.
    `agent='forge'` deliberately: the event marks the FORGE build entering
    review (the dashboard fetch filters on the building agent), even though
    the writer is this notifier and the reviewer is Mirror.

    Best-effort + ADDITIVE: emit_event logs WARN and returns False on
    Supabase outage; the review dispatch itself never depends on the row
    landing. A lost row only means the task skips the in_review lane
    visually until the verdict lands. The same display-only loss happens if
    the daemon dies between the inbox write and this emit: re-processing
    hits the idempotency presence check (which fires before the dispatch
    try-block) and skips both — accepted, the lane is a view, not state.
    Same daemon-never-wedge invariant as the sibling emit helpers.
    """
    chain_payload = {
        'agent': 'forge',
        'task_id': task_id,
        'revision_count': revision_count,
        'replan_count': replan_count,
    }
    try:
        chain_event_emit.emit_event(
            event_type='review_request',
            agent='forge',
            task_id=task_id,
            payload=chain_payload,
            pr_url=pr_url,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'review_request chain_event emit raised unexpectedly for '
            f'task {task_id!r}: {type(e).__name__}: {e}',
            'WARN',
        )


# Canonical "preflight response carried no marker block at all" error. Defined
# as a constant (not an inline literal) so the none-found marker-error retry
# enrichment in `_notify_forge_marker_error` can detect THIS specific failure
# shape by exact match — distinct from malformed-JSON / missing-field / multi-
# marker / task_id-mismatch errors, which already carry actionable messages and
# must NOT get the fill-in grammar.
PREFLIGHT_NO_MARKER_ERROR_MSG = (
    'phase=preflight requires ONE marker block at end of response '
    '(PROCEED / CLARIFY_REQUEST / REJECT) — none found. Re-read '
    "agents/forge/CLAUDE.md 'Preflight discipline' — preflight "
    'decides, it does not act.'
)


def _build_preflight_marker_grammar(task_id: str) -> str:
    """Fill-in-the-blank grammar block for a none-found preflight retry.

    Embeds the canonical block for all three preflight marker types
    (PROCEED / CLARIFY_REQUEST / REJECT) — delimiters + a required-field JSON
    skeleton — sourced from `fph.render_all_marker_skeletons` so the example
    stays in lockstep with the parser. The real `task_id` is injected so the
    pasted-back marker already satisfies the task_id-match check.
    """
    skeletons = fph.render_all_marker_skeletons(field_values={'task_id': task_id})
    return (
        'FILL-IN-THE-BLANK — your response omitted the marker block entirely. '
        'End your response with EXACTLY ONE of the blocks below, with the JSON '
        'values filled in (replace each <...> placeholder). The content between '
        'the delimiters MUST be a single JSON object — put narrative ABOVE the '
        'block, never inside it.\n\n'
        f'{skeletons}\n'
        'Choose PROCEED if the spec is buildable, CLARIFY_REQUEST if you need '
        'one specific answer from Beacon, or REJECT if it is not buildable as '
        'written. Paste exactly one block.'
    )


def _record_deliverable_claim(
    *, claimed_task_id: Any, envelope_task_id: Any,
    agent: Optional[str] = None, target_repo: Any = None,
) -> None:
    """Best-effort bridge to the launch dedup guard: when a build session emits a
    marker whose task_id differs from its envelope (the marker-task_id-mismatch
    class), record the CLAIMED task_id so a later board Launch of that same id can
    recognise the work is already in flight elsewhere — the 2026-06-20
    cross-identity redundant-build incident. NEVER raises (a claim-record failure
    must not perturb the marker-error cascade)."""
    try:
        import launch_dedup_guard  # local import: optional, pure-stdlib dep
        launch_dedup_guard.record_claim(
            claimed_task_id=claimed_task_id,
            envelope_task_id=envelope_task_id,
            agent=agent,
            target_repo=target_repo if isinstance(target_repo, str) else None,
            source='marker-task_id-mismatch',
        )
    except Exception as e:  # noqa: BLE001 — best-effort; never break the notifier
        log(f'deliverable-claim record failed (non-fatal): '
            f'{type(e).__name__}: {e}', 'WARN')


def _classify_forge_marker(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Inspect a Forge outbox for a preflight marker. Returns routing decision or None.

    Returned dict shape (when a marker is found):
      {
        'marker_type': 'proceed' | 'clarify_request' | 'reject',
        'payload':     parsed marker JSON,
        'intent':      one of INTENT_ACTION_BLOCKS keys (after budget check),
        'notify_source': source field for the resulting notify task,
        'intent_kwargs': dict of fields for the intent action-block template,
        'next_clarification_count': int — what the next dispatch's count should be,
                                    or None if irrelevant for this marker type,
      }

    Raises `fph.MalformedForgeMarker` or `fph.MultipleForgeMarkers` if the
    marker block is present but unparseable. The caller dead-letters those
    back to Forge so she can re-emit cleanly.

    Returns None if the outbox has no marker (legacy / non-forge agents take
    the default routing path).
    """
    result_text = data.get('result', '')
    if not isinstance(result_text, str):
        result_text = ''

    # chain-discipline-marker-parser-and-regression-check-001 (2026-05-25):
    # scan the session log FIRST and pick the latest valid marker across all
    # assistant turns. Same rationale as `_classify_mirror_marker` — the
    # outbox `result` captures only the final `claude -p` turn, so any
    # post-marker chatter from Forge would mask her decision. Fall back to
    # final-turn parsing only when the session log is unavailable.
    marker_type, payload, _narrative = (None, None, '')
    recovered, _recovered_by_type = _recover_forge_marker_text_from_session_log(
        data.get('claude_session_id'),
    )
    if recovered:
        marker_type, payload, _narrative = fph.parse_forge_marker(recovered)
        if marker_type is not None:
            log(
                f'classified forge {marker_type} marker from session log scan '
                f'(session={(data.get("claude_session_id") or "")[:12]}..., '
                f'task={data.get("task_id")!r})'
            )

    if marker_type is None and result_text.strip():
        marker_type, payload, _narrative = fph.parse_forge_marker(result_text)

    if marker_type is None:
        # D3.5 commit 5a — preflight-discipline runtime gate (deferred from
        # 4b's followup-2 doc). A `phase=preflight` outbox MUST end with one
        # of PROCEED / CLARIFY_REQUEST / REJECT. If Forge fast-pathed past
        # the marker block (e.g., started writing code during preflight),
        # the dispatch has no decision recorded — dead-letter back via the
        # marker-error cascade with a sharper "decide, don't act" prompt.
        # 5a ships strict mode (per the d3-5-plan VALUES sign-off — soft
        # warning lets Forge keep fast-pathing; strict costs one extra
        # invocation, cheap insurance until Forge has 20+ disciplined runs).
        if data.get('phase') == 'preflight':
            raise fph.MalformedForgeMarker(PREFLIGHT_NO_MARKER_ERROR_MSG)
        return None

    # Phase D3 commit 4b: tighten marker discipline. Forge's marker payload
    # MUST carry the same task_id as the envelope. The 4a smoke surfaced a
    # drift where Forge emitted a non-matching task_id; routing happened to
    # work via the outbox filename stem, but Forge cannot rely on that path.
    # Raise as MalformedForgeMarker so the marker-error cascade fires and
    # Forge re-emits with the correct task_id.
    envelope_task_id = data.get('task_id')
    marker_task_id = (
        payload.get('task_id') if isinstance(payload, dict) else None
    )
    if (
        envelope_task_id is not None
        and marker_task_id is not None
        and marker_task_id != envelope_task_id
    ):
        # Record the cross-identity claim BEFORE raising so a later board Launch
        # of `marker_task_id` can de-duplicate against this in-flight work.
        _record_deliverable_claim(
            claimed_task_id=marker_task_id,
            envelope_task_id=envelope_task_id,
            agent=data.get('agent', 'forge'),
            target_repo=data.get('target_repo'),
        )
        raise fph.MalformedForgeMarker(
            f'marker task_id ({marker_task_id!r}) does not match envelope '
            f'task_id ({envelope_task_id!r})'
        )

    agent = data.get('agent', 'forge')
    if marker_type == 'clarify_request':
        decision, next_count, max_count = fph.evaluate_clarification_budget(data)
        if decision == 'exhausted':
            # Budget exhausted — clarification-exhausted intent so Beacon
            # sees the specific termination reason (not a generic reject).
            # Source goes back via the reject channel (forge-result) so the
            # protocol terminates rather than continuing the question loop.
            reason = fph.build_exhausted_reason(payload, next_count, max_count)
            return {
                'marker_type': marker_type,
                'payload': payload,
                'intent': fph.derive_intent(marker_type, budget_decision=decision),
                'notify_source': fph.derive_notify_source('reject', agent),
                'intent_kwargs': {
                    'reason': reason,
                    'max_count': max_count,
                },
                'next_clarification_count': None,
            }
        return {
            'marker_type': marker_type,
            'payload': payload,
            'intent': fph.derive_intent(marker_type, budget_decision=decision),
            'notify_source': fph.derive_notify_source(marker_type, agent),
            'intent_kwargs': {
                'next_count': next_count,
                'max_count': max_count,
            },
            'next_clarification_count': next_count,
        }

    if marker_type == 'proceed':
        return {
            'marker_type': marker_type,
            'payload': payload,
            'intent': fph.derive_intent(marker_type),
            'notify_source': fph.derive_notify_source(marker_type, agent),
            'intent_kwargs': {},
            'next_clarification_count': None,
        }

    # marker_type == 'reject'
    return {
        'marker_type': marker_type,
        'payload': payload,
        'intent': fph.derive_intent(marker_type),
        'notify_source': fph.derive_notify_source(marker_type, agent),
        'intent_kwargs': {'reason': payload.get('reason', '(no reason given)')},
        'next_clarification_count': None,
    }


def _is_fixture_emission(task_id: object) -> bool:
    """True iff a re-emission id/name resolves to a reserved fixture pattern.

    Emission-path gate (mission #2, 2026-05-30). The marker-error and dead-
    letter re-emit sites below bypass the read-time outbox gate
    (matched_fixture_envelope at process_outbox time), so a fixture envelope
    that slips past archival could be re-injected into an inbox and re-
    dispatched -- burning real Opus credits (2026-05-28/29 cost-loop
    incidents). Mirror the read-time gate's wrapper-peeling check at every
    emission point. Gates the side-effect, not the detection helper -- the
    discipline from commit 88b0d1a.
    """
    return fixture_patterns.is_fixture_envelope_name(task_id)


def _notify_forge_marker_error(data: dict[str, Any], err_msg: str) -> None:
    """Write a marker-error notify back to Forge so she can re-emit a clean marker.

    Carries forward two propagated fields from the source outbox so the
    recovered marker's routing target survives across the retry round-trip:

      - `original_source` — the dispatcher's source (`beacon` on first round,
        or itself on subsequent rounds). Lets the notifier route Forge's
        recovered output back to the right agent (Beacon, not a dead-end).
      - `marker_error_count` — incremented each retry. When > MAX_MARKER_ERROR_RETRIES
        the notifier dead-letters to Beacon instead of looping back to Forge.

    Best-effort — daemon must not crash on a failed notify-write here. The
    underlying outbox is archived by the caller regardless.
    """
    agent = data.get('agent', 'forge')
    task_id = data.get('task_id', 'unknown')

    # Trace original dispatcher through the cascade. On the first malformed-
    # marker round, `original_source` isn't set on the outbox — Forge's outbox
    # source IS the original dispatcher. On subsequent rounds, `original_source`
    # was propagated by the prior _notify_forge_marker_error call.
    original_source = data.get('original_source') or data.get('source') or 'beacon'

    prev_count = data.get('marker_error_count', 0)
    if not isinstance(prev_count, int) or prev_count < 0:
        prev_count = 0
    new_count = prev_count + 1

    if new_count > MAX_MARKER_ERROR_RETRIES:
        # Defense against runaway loop — dead-letter to the original dispatcher
        # so a human can intervene. Don't keep asking Forge to retry.
        log(
            f'marker-error retries exhausted ({new_count}/{MAX_MARKER_ERROR_RETRIES}) '
            f'for task {task_id}; dead-lettering to {original_source}',
            'WARN',
        )
        _dead_letter_marker_error_to_dispatcher(
            data, original_source, err_msg, new_count,
        )
        # push-signal-and-substatus (B): a dead-lettered marker-error cascade is
        # terminal for a build-sequence step (the dispatch is closed; no PR will
        # open) — fail the step + pause the sequence now instead of stranding it
        # `dispatched` for the 4h stall backstop. No-op for non-sequence tasks.
        _signal_sequence_step_failed(
            task_id,
            f'Forge marker-error retries exhausted '
            f'({new_count}/{MAX_MARKER_ERROR_RETRIES}); dead-lettered to '
            f'{original_source}: {err_msg}',
        )
        return

    prompt = build_notify_prompt(
        intent='marker-error',
        sender='outbox-notifier',
        task_id=task_id,
        success=False,
        output=(data.get('result') or '')[:1000],
        intent_kwargs={
            'reason': err_msg,
            'task_id': task_id,
            'retry_count': new_count,
            'max_retries': MAX_MARKER_ERROR_RETRIES,
        },
    )
    # Fill-in-the-blank enrichment for a Forge PREFLIGHT response that omitted
    # the marker block entirely. The shared marker-error template is a scold
    # ("re-emit a valid marker"), which Forge frequently answers with another
    # omission — ~35% of these escalate to a 2nd retry, each a full preflight
    # re-run. Embedding the parser-synced grammar for all three preflight
    # markers turns the bounce into a paste-and-fill task. Scoped strictly to
    # the none-found case (exact-match on the canonical message) so malformed-
    # JSON / missing-field / multi-marker / task_id-mismatch retries are
    # unchanged, and to phase=preflight so Mirror review and Forge revision
    # marker-errors are untouched.
    if data.get('phase') == 'preflight' and err_msg == PREFLIGHT_NO_MARKER_ERROR_MSG:
        prompt = f'{prompt}\n\n{_build_preflight_marker_grammar(task_id)}'
    # D3.5 5b-followup Bug B: keep envelope task_id as the ORIGINAL task_id
    # across retries. The previous wrapped form (`marker-error-<orig>-<N>`)
    # broke the 4b task_id-mismatch check: Forge correctly emits her marker
    # with the original task_id (that's the actual semantic task), but the
    # envelope had the wrapper name, so the mismatch check rejected every
    # retry. Cascade never recovered from real preflight failures.
    # Retry tracking lives in `marker_error_count`; filename uses
    # `-{new_count}` suffix for uniqueness. Now Forge's marker contract
    # (task_id matches envelope) holds consistently across retries.
    notify_base: dict[str, Any] = {
        'task_id': task_id,
        'prompt': prompt,
        'source': 'outbox-notifier',
        'intent': 'marker-error',
        '_notify_depth': 1,
        'original_source': original_source,
        'marker_error_count': new_count,
    }
    # Propagate clarification budget too — a marker-error round shouldn't reset
    # the count (Forge might re-emit a CLARIFY_REQUEST after recovering, and
    # Beacon needs the right budget for the next round).
    if data.get('clarification_count') is not None:
        notify_base['clarification_count'] = data['clarification_count']
    if data.get('max_clarifications') is not None:
        notify_base['max_clarifications'] = data['max_clarifications']
    if data.get('claude_session_id'):
        notify_base['session_id'] = data['claude_session_id']
    if data.get('branch'):
        notify_base['branch'] = data['branch']
    if data.get('phase'):
        notify_base['phase'] = data['phase']
    if data.get('max_revisions') is not None:
        notify_base['max_revisions'] = data['max_revisions']
    # Chain context routed through the sole constructor (M1). Whitelisted
    # fields propagate from the inbound envelope so the malformed-marker retry
    # keeps Forge's worktree/session/budget context:
    #   - target_repo/branch (branch above): worktree gate, else the retry
    #     rejects as `target_repo: no canonical path` (the 4a black-hole shape).
    #   - phase/forge_build_session_id/revision_count/pr_url (D3.5 5b M-2): a
    #     revision-phase marker-error retry without these dead-ends (no
    #     --resume, no findings thread, fresh budget, nothing to point Mirror at).
    #   - reply_chat_id (5b-followup Bug E): else the originating Telegram
    #     thread silently ends with no closing DM.
    # replan_count/max_replans are not part of the marker-error retry context.
    notify_task = build_chain_envelope(
        notify_base,
        data,
        carry={
            'target_repo': CARRY,
            'forge_build_session_id': CARRY,
            'revision_count': CARRY,
            'pr_url': CARRY,
            'reply_chat_id': CARRY,
            'replan_count': DROP,
            'max_replans': DROP,
        },
    )

    if _is_fixture_emission(task_id):
        log(
            f'suppressing marker-error notify for fixture task {task_id} '
            f'(reserved fixture namespace)'
        )
        return

    try:
        safe_write_inbox.safe_write_inbox(
            target_agent=agent,
            task_dict=notify_task,
            source_agent='outbox-notifier',
            filename=f'marker-error-{task_id}-{new_count}.json',
        )
        log(
            f'marker-error notify written to {agent} for task {task_id} '
            f'(retry {new_count}/{MAX_MARKER_ERROR_RETRIES})'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'marker-error notify failed for {agent}/{task_id}: '
            f'{type(e).__name__}: {e}',
            'WARN',
        )


def _dead_letter_marker_error_to_dispatcher(
    data: dict[str, Any],
    original_source: str,
    err_msg: str,
    error_count: int,
) -> None:
    """Final-stop dead-letter when marker-error retries are exhausted.

    Sends a `dead-letter` intent notify to the original dispatcher (Beacon
    in the normal case) so a human can decide what to do next. The dispatch
    is closed — Forge will NOT see another retry from the notifier.
    """
    target_agent = _primary_agent_id(original_source)
    if target_agent is None:
        # Original source was a system entity — log and stop. No human-routable
        # endpoint to dead-letter to.
        log(
            f'marker-error dead-letter has no routable target '
            f'(original_source={original_source}); giving up on task '
            f'{data.get("task_id", "?")}',
            'ERROR',
        )
        return

    task_id = data.get('task_id', 'unknown')
    reason = (
        f'Forge produced {error_count} consecutive malformed markers on task '
        f'`{task_id}` (cap={MAX_MARKER_ERROR_RETRIES}). Final parse error: {err_msg}. '
        f'The dispatch is closed; inspect Forge\'s outputs in '
        f'~/agents/outboxes/forge/.archive/ and either fix her CLAUDE.md '
        f'marker discipline or revise the spec and re-dispatch.'
    )
    prompt = build_notify_prompt(
        intent='dead-letter',
        sender='outbox-notifier',
        task_id=task_id,
        success=False,
        output=(data.get('result') or '')[:1000],
        intent_kwargs={'reason': reason},
    )
    notify_base: dict[str, Any] = {
        'task_id': f'dead-letter-marker-{task_id}',
        'prompt': prompt,
        'source': 'outbox-notifier',
        'intent': 'dead-letter',
        '_notify_depth': 1,
    }
    # Terminal dead-letter: only reply_chat_id rides forward so Beacon's
    # closing DM reaches the originating Telegram thread (M1).
    notify_task = build_chain_envelope(
        notify_base,
        data,
        carry={
            'reply_chat_id': CARRY,
            'target_repo': DROP,
            'pr_url': DROP,
            'forge_build_session_id': DROP,
            'revision_count': DROP,
            'replan_count': DROP,
            'max_replans': DROP,
        },
    )

    if _is_fixture_emission(task_id):
        log(
            f'suppressing dead-letter notify for fixture task {task_id} '
            f'(reserved fixture namespace)'
        )
        return

    try:
        safe_write_inbox.safe_write_inbox(
            target_agent=target_agent,
            task_dict=notify_task,
            source_agent='outbox-notifier',
            filename=f'dead-letter-marker-{task_id}.json',
        )
        log(
            f'marker-error dead-letter written to {target_agent} for task {task_id}'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'marker-error dead-letter failed for {target_agent}/{task_id}: '
            f'{type(e).__name__}: {e}',
            'ERROR',
        )

    # D3.5 5b-followup Bug C: also DM Larry on cascade exhaust. The chat
    # thread that initiated the dispatch deserves a closing notification
    # — without this, "Larry approved, then silence" (the failed live
    # test 2026-05-13 was exactly this shape). Build a synthetic decision
    # dict so _maybe_dm_larry's render pipeline works.
    synthetic_decision = {
        'intent': 'dead-letter',
        'payload': {'task_id': task_id},
        'intent_kwargs': {
            'task_id': task_id,
            'reason': reason,
            'retry_count': error_count,
        },
    }
    _maybe_dm_larry(data, synthetic_decision)


def _prior_build_was_spawn_failure(task_id: str) -> bool:
    """Return True only on a DEFINITIVE terminal build-phase spawn-failure.

    Build-dedup wedge fix (2026-06-09). The dedup in ``_dispatch_build_phase``
    skips re-dispatch when ``build-<task_id>.json`` is present in Forge's inbox
    archive. That artifact gets archived even when the build worker FAILED TO
    SPAWN — agent_runner records a terminal Forge outbox result with
    ``exit_code == -1`` / ``'All retries exhausted'`` / ~0 duration / no PR.
    Such a task could then never be re-dispatched under the same task_id (it
    could only be unblocked by minting a fresh task_id — the footgun that hit
    ``register-ol-db-ro-url-credential`` on 2026-06-09: failed twice, only the
    v2 task_id landed PR #401).

    This helper distinguishes 'never ran' from 'ran or in-flight' by scanning
    the Forge outbox archive for the MOST RECENT build-phase RESULT envelope
    for this task_id and returning True ONLY on the definitive spawn-failure
    shape:
      - ``exit_code == -1``
      - error/result indicates a non-run (``'All retries exhausted'``)
      - ``duration_sec`` is ~0 (None or < 2s — the real artifact records null)
      - NO pr_url (neither a top-level field nor extractable from result text)

    SAFETY INVARIANT: the override fires ONLY on this definitive terminal
    failure. Absence of a terminal result (build still running, or the notifier
    crashed mid-flight before archiving the result) must NEVER return True —
    when in doubt, return False so the dedup keeps its conservative crash-
    recovery skip. Any read/parse error → False (defensive: keep the skip).
    """
    try:
        archive_dir = OUTBOXES_ROOT / 'forge' / '.archive'
        if not archive_dir.exists():
            return False
        # Outbox result envelopes are keyed `<task_id>.json` / `<task_id>.N.json`
        # (NOT `build-<task_id>`). Anchor the match so a task_id that is a
        # prefix of another (e.g. 'foo' vs 'foo-v2') can't cross-match.
        name_re = re.compile(rf'^{re.escape(task_id)}(\.\d+)?\.json$')
        candidates: list[tuple[float, dict[str, Any]]] = []
        for f in archive_dir.glob('*.json'):
            if f.name.startswith('.') or not name_re.match(f.name):
                continue
            try:
                envelope = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(envelope, dict):
                continue
            if envelope.get('agent') != 'forge' or envelope.get('phase') != 'build':
                continue
            candidates.append((_result_epoch(f, envelope), envelope))
        if not candidates:
            return False

        # Most recent build-phase result wins.
        _, latest = max(candidates, key=lambda item: item[0])

        if latest.get('exit_code') != -1:
            return False
        # A PR (top-level field OR a `PR opened:`/`PR updated:` line in the
        # result text) means a build actually ran — never override.
        if latest.get('pr_url'):
            return False
        if _extract_pr_url_from_build_result(latest.get('result', '')):
            return False
        # Substantive duration means it ran; None/0/~0 is the never-spawned
        # signal (the real spawn-failure artifact records duration_sec=null).
        duration = latest.get('duration_sec')
        if duration is not None:
            try:
                if float(duration) >= 2:
                    return False
            except (TypeError, ValueError):
                return False
        # Terminal non-run signal in error/result.
        signals = (str(latest.get('error') or ''), str(latest.get('result') or ''))
        if not any('all retries exhausted' in s.lower() for s in signals):
            return False
        return True
    except Exception:
        return False


def _result_epoch(f: Path, envelope: dict[str, Any]) -> float:
    """Sortable recency for an outbox result: completed_at, else file mtime."""
    ts = envelope.get('completed_at')
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    try:
        return f.stat().st_mtime
    except OSError:
        return 0.0


def _prior_dispatch_was_definitive_non_run(task_id: str) -> bool:
    """Return True only when the MOST RECENT Forge outbox result for this
    task_id was a DEFINITIVE non-run (worker produced no real work and no PR).

    Headless-approval dedup-wedge fix (2026-06-11), mirroring PR #403's build-
    phase carve-out (``_prior_build_was_spawn_failure``). The headless-approval-
    request dispatch keys its dedup on the archived preflight task file
    ``<task_id>.json``; that file is archived even when the prior attempt was a
    definitive non-run — e.g. ccd-s1 (2026-06-10), where identity resolved to
    BEACON instead of FORGE and the worker emitted an IDENTITY_MISMATCH reject
    with no real work and no PR. The stale artifact then blocked every retry.

    Two definitive-non-run shapes are recognized, BOTH requiring no PR:
      - spawn-failure: ``exit_code == -1`` + ~0 duration (None or < 2s) + an
        ``'All retries exhausted'`` signal — the worker never spawned.
      - identity-mismatch reject: the result/error text carries the
        ``IDENTITY_MISMATCH`` token — the worker bailed at the identity gate
        before doing any work (this is the ccd-s1 regression shape).

    SAFETY INVARIANT (same as #403): fires ONLY on a determinable definitive
    non-run. A genuine REJECT (spec not buildable) has exit 0 and no IDENTITY_
    MISMATCH token, so it is correctly NOT matched and stays deduped. A
    completed attempt (any PR) or an in-flight one (no terminal result) returns
    False so dedup keeps its conservative skip. Any read/parse error → False.

    Unlike the build helper this does NOT filter on ``phase == 'build'`` — the
    headless wedge is keyed on the preflight task file, so the prior attempt's
    terminal result is a preflight (or later) result. The MOST RECENT result
    of any phase wins, so a task that later completed (with a PR) still dedups.
    """
    try:
        archive_dir = OUTBOXES_ROOT / 'forge' / '.archive'
        if not archive_dir.exists():
            return False
        # Result envelopes are keyed `<task_id>.json` / `<task_id>.N.json`.
        # Anchor the match so a task_id that is a prefix of another (e.g.
        # 'foo' vs 'foo-v2') can't cross-match.
        name_re = re.compile(rf'^{re.escape(task_id)}(\.\d+)?\.json$')
        candidates: list[tuple[float, dict[str, Any]]] = []
        for f in archive_dir.glob('*.json'):
            if f.name.startswith('.') or not name_re.match(f.name):
                continue
            try:
                envelope = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(envelope, dict):
                continue
            if envelope.get('agent') != 'forge':
                continue
            candidates.append((_result_epoch(f, envelope), envelope))
        if not candidates:
            return False

        # Most recent result of any phase wins.
        _, latest = max(candidates, key=lambda item: item[0])

        # A PR (top-level field OR a `PR opened:`/`PR updated:` line in the
        # result text) means real work ran — never override.
        if latest.get('pr_url'):
            return False
        if _extract_pr_url_from_build_result(latest.get('result', '')):
            return False

        signals = (
            str(latest.get('error') or ''),
            str(latest.get('result') or ''),
        )

        # Shape 1 — definitive spawn-failure (worker never ran).
        if latest.get('exit_code') == -1:
            duration = latest.get('duration_sec')
            duration_ok = duration is None
            if duration is not None:
                try:
                    duration_ok = float(duration) < 2
                except (TypeError, ValueError):
                    duration_ok = False
            if duration_ok and any(
                'all retries exhausted' in s.lower() for s in signals
            ):
                return True

        # Shape 2 — identity-mismatch reject (worker bailed at identity gate).
        if any('identity_mismatch' in s.lower() for s in signals):
            return True

        return False
    except Exception:
        return False


def _notify_beacon_phantom_build_suppressed(
    data: dict[str, Any], branch: str, pr_state: str,
) -> None:
    """Best-effort: journal a phantom-build suppression to Beacon's inbox.

    Informational only — the terminal guard has already skipped the dispatch and
    logged PHANTOM_BUILD_SUPPRESSED. This adds a Beacon journal entry so the
    suppression is visible in the chain record, not just the daemon log. Any
    write failure is swallowed (the log line is the durable signal); this MUST
    NEVER wedge the dispatch path, so the whole body is wrapped.
    """
    task_id = data.get('task_id') or 'unknown'
    try:
        notify_prompt = build_notify_prompt(
            intent='result-notification',
            sender='outbox-notifier',
            task_id=task_id,
            success=True,
            output=(
                f'Phantom build-phase dispatch suppressed for task '
                f'`{task_id}`: build branch `{branch}` already has a '
                f'{pr_state} PR, so this PROCEED is a stale-marker '
                f're-discovery (resumed-session transcript), not fresh work. '
                f'No build task was written to Forge. Journal only — no '
                f'action needed.'
            ),
        )
        notify_base = {
            'task_id': f'notify-phantom-build-suppressed-{task_id}',
            'prompt': notify_prompt,
            'source': 'outbox-notifier',
            'intent': 'result-notification',
            'original_task_id': task_id,
            '_notify_depth': _current_notify_depth(data) + 1,
        }
        notify_task = build_chain_envelope(
            notify_base,
            data,
            carry={
                'target_repo': CARRY,
                'pr_url': DROP,
                'forge_build_session_id': DROP,
                'reply_chat_id': CARRY,
                'revision_count': DROP,
                'replan_count': CARRY,
                'max_replans': CARRY,
            },
        )
        notify_filename = f'notify-phantom-build-suppressed-{task_id}.json'
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='beacon',
            task_dict=notify_task,
            source_agent='outbox-notifier',
            filename=notify_filename,
        )
        log(
            f'PHANTOM_BUILD_SUPPRESSED task={task_id}; journaled suppression '
            f'notify to beacon (file={dest.name})'
        )
    except Exception as e:
        # Informational only — a notify failure must never wedge the dispatch.
        log(
            f'phantom-build suppression notify to beacon failed for task '
            f'{task_id}: {type(e).__name__}: {e}; suppression still applied '
            f'(the PHANTOM_BUILD_SUPPRESSED log line is the durable signal)',
            'WARN',
        )


def _dispatch_build_phase(data: dict[str, Any]) -> None:
    """Write a build-phase task to Forge's inbox after a PROCEED marker.

    Phase D3 commit 4b. The signed-off design is two invocations with
    ``--resume``: preflight first, then build. The notify-to-Beacon (above)
    is informational (Beacon journals "Forge is proceeding"); this is what
    actually triggers code work.

    ``source`` is ``beacon`` because Beacon is the logical dispatcher —
    her APPROVAL_REQUEST + Larry's approval authorized the work. The
    notifier acts as Beacon's mechanical extension. Audit field
    ``dispatched_by: 'outbox-notifier'`` records the actual writer.

    Failure to write the build-phase task is logged WARN and non-fatal.
    The notify-to-Beacon above has already informed her that Forge
    ack-proceeded; Larry sees the gap in the log and can manually
    re-dispatch.
    """
    task_id = data.get('task_id') or 'unknown'
    preflight_session = data.get('claude_session_id')
    if not preflight_session:
        log(
            f'PROCEED marker for task {task_id} has no claude_session_id; '
            f'build-phase dispatch would have nothing to --resume — '
            f'skipping. Larry should manually re-dispatch.',
            'WARN',
        )
        return

    target_repo = data.get('target_repo')
    branch = data.get('branch')
    pr_title = data.get('pr_title')
    pr_body = data.get('pr_body')
    max_clar = data.get('max_clarifications')

    # The build prompt is the next user turn in the resumed claude session;
    # the actual `target_repo` value is in the worktree's git config and
    # Forge already read it during preflight. Keep this terse — the
    # protocol details live in agents/forge/CLAUDE.md's Build phase section.
    build_prompt_lines = [
        'Build phase. Your preflight returned PROCEED; the plan you '
        'confirmed is the contract. Execute it now in this worktree.',
        '',
        f'Task: `{task_id}`',
    ]
    if branch:
        build_prompt_lines.append(f'Branch: `{branch}`')
    if pr_title:
        build_prompt_lines.append(f'PR title: `{pr_title}`')
    build_prompt_lines.extend([
        '',
        'Follow the Build phase protocol in your CLAUDE.md: implement '
        'changes → git add / git commit (conventional-commit style) → '
        'git push -u origin <branch> → gh pr create. Emit a single '
        'result block at the end starting with `PR opened: <url>`.',
    ])
    build_prompt = '\n'.join(build_prompt_lines)

    build_base: dict[str, Any] = {
        'task_id': task_id,
        'prompt': build_prompt,
        'source': 'beacon',
        'phase': 'build',
        'session_id': preflight_session,
        'dispatched_by': 'outbox-notifier',
    }
    if branch:
        build_base['branch'] = branch
    if pr_title:
        build_base['pr_title'] = pr_title
    if pr_body:
        build_base['pr_body'] = pr_body
    if max_clar is not None:
        build_base['max_clarifications'] = max_clar
    # Chain context (M1). target_repo (truthy local) gates Forge's worktree.
    # replan_count + max_replans propagate through the preflight→build hop
    # (D3.5 5c C-1): without them, a 5c-replan approval (replan_count > 0 on
    # the original envelope) resets to 0 on the build dispatch, breaking the
    # budget cap on the next Mirror REVIEW_ESCALATE leg. reply_chat_id keeps
    # the Telegram thread. pr_url/forge_build_session_id/revision_count are
    # not yet known at build dispatch (no PR, no review).
    build_task = build_chain_envelope(
        build_base,
        data,
        carry={
            'target_repo': target_repo,
            'reply_chat_id': CARRY,
            'replan_count': CARRY,
            'max_replans': CARRY,
            'pr_url': DROP,
            'forge_build_session_id': DROP,
            'revision_count': DROP,
        },
    )

    # D3.5 5c-followup-2 (audit C-1): key the build-task filename by
    # replan_count when this is a replan iteration. The dedup check below
    # is filename-based; without the round suffix, the round-1 archive
    # entry collides with every subsequent replan iteration's build
    # dispatch — the dedup returns true on the .archive/ hit and silently
    # drops the dispatch. Symmetric with how _dispatch_revision_to_forge
    # keys revision filenames on revision_count. Surfaced by 5c deeper
    # audit 2026-05-14 — same "canonical scenario assumed" pattern as
    # Miss #3 (this is the structural-collision sibling of the regex-
    # anchoring miss).
    replan_count = data.get('replan_count', 0)
    if not isinstance(replan_count, int) or replan_count < 0:
        replan_count = 0
    if replan_count > 0:
        build_filename = f'build-{task_id}-replan{replan_count}.json'
    else:
        build_filename = f'build-{task_id}.json'
    # Match the on-disk name the writer produces (sanitize + length cap) so the
    # dedup can't miss for a task_id carrying a path-structural byte.
    build_filename = safe_write_inbox.canonical_inbox_name(build_filename)
    # Idempotency check (4b review fix): if the build task is already
    # present in Forge's inbox OR was already archived, skip re-dispatch.
    # Guards against the notifier crashing between dispatch and archive
    # of the preflight outbox: on restart, re-processing the same outbox
    # would otherwise write a second build task that would resume an
    # already-terminated session against potentially-dirty worktree state.
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
    # A build currently in the LIVE inbox is in-flight — always skip, never
    # override (don't double-dispatch a queued/running build).
    if (forge_inbox / build_filename).exists():
        log(
            f'build-phase already dispatched for task {task_id} '
            f'(live inbox file present); skipping duplicate write'
        )
        return
    # A stale `build-<task_id>.json` in .archive/ or .invalid/ normally means a
    # build was already dispatched (crash-recovery protection: the notifier may
    # have died between dispatch and archive of the preflight outbox, so a
    # re-process must NOT resume an already-terminated session against dirty
    # worktree state). EXCEPTION (build-dedup wedge fix, 2026-06-09): if the
    # prior build was a DEFINITIVE spawn-failure (worker never ran, no PR), the
    # archive artifact is a phantom that would permanently wedge the task_id.
    # In that case allow the re-dispatch. The override fires ONLY on a definitive
    # terminal failure result — absence of a terminal result (still running /
    # notifier crashed mid-flight) returns False and keeps the conservative skip.
    if (
        (forge_inbox / '.archive' / build_filename).exists()
        # D3.5 5a M-1 review fix: also check .invalid/ — a prior dispatch that
        # was validator-rejected lives there, and we shouldn't re-dispatch a
        # duplicate that will hit the same rejection.
        or (forge_inbox / '.invalid' / build_filename).exists()
    ):
        if _prior_build_was_spawn_failure(task_id):
            log(
                f'BUILD_DEDUP_OVERRIDE task={task_id} — prior build was a '
                f'spawn-failure (exit=-1, no PR, ~0s); allowing re-dispatch'
            )
        else:
            log(
                f'build-phase already dispatched for task {task_id} '
                f'(archive or .invalid present); skipping duplicate write'
            )
            return

    # phantom-build-phase terminal guard (cap-phantom-build-phase-after-marker-
    # error-retry-pr-4d78). The filename-dedup above is a local-filesystem
    # heuristic, not ground truth — and its _prior_build_was_spawn_failure
    # override (BUILD_DEDUP_OVERRIDE) can additionally re-OPEN a phantom. A
    # marker-error-retry envelope carries phase=preflight, so the resumed-
    # session stale-`=== PROCEED ===` re-discovery inside _classify_forge_marker
    # is NOT excluded by the build/revision guard at the classify call site —
    # it re-classifies as a fresh proceed and re-dispatches a build for an
    # ALREADY-TERMINAL task (2026-06-10: build-fix-classifier-session-lost-002
    # re-fired for PR #435 *after* it had merged; a human cancelled it). Ground-
    # truth check: if this build branch already has a PR in a TERMINAL state
    # (MERGED, or CLOSED-without-merge) the dispatch is a phantom — skip the
    # inbox write, log it, and journal an informational notify to Beacon.
    #
    # FAIL-OPEN on every uncertainty: no branch (the `if branch:` guards above
    # show it's optional) / gh transport error / timeout / non-zero exit /
    # unparseable output -> proceed with the dispatch. A phantom is the rare
    # exception; a gh outage must NEVER wedge every legitimate build. An OPEN PR
    # is likewise NOT a phantom — it's the legitimate replan/revision re-dispatch
    # (replan_count > 0, build-<task_id>-replan<N>.json) — so only a
    # DEFINITIVELY terminal state skips. Wrapped in try/except so the guard can
    # never raise into the daemon (daemon-never-wedge).
    if branch and target_repo:
        try:
            pr_state = _gh_terminal_pr_state_for_branch(target_repo, branch)
        except Exception as e:
            log(
                f'phantom-build terminal guard raised for task {task_id} '
                f'(branch={branch}, repo={target_repo}): '
                f'{type(e).__name__}: {e}; failing open (proceeding)',
                'WARN',
            )
            pr_state = None
        if pr_state in ('MERGED', 'CLOSED'):
            log(
                f'PHANTOM_BUILD_SUPPRESSED task={task_id} — build branch '
                f'{branch} already has a {pr_state} PR in {target_repo}; '
                f'this PROCEED is a stale-marker re-discovery, not fresh work. '
                f'Skipping build-phase dispatch (GitHub-truth terminal guard).'
            )
            _notify_beacon_phantom_build_suppressed(data, branch, pr_state)
            return

    # D3.5 5d cost-budget gate. AFTER the idempotency check (second-pass
    # review finding 2-#1): on a crash-recovery re-process where the
    # downstream dispatch already landed, the idempotency check above
    # returns first — the cost gate shouldn't fire a false-alarm DM for
    # work that isn't being attempted. Gate refuses + DMs Larry only on
    # genuine new dispatch attempts.
    if not _enforce_cost_budget(task_id, 'build-phase', data):
        return

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='forge',
            task_dict=build_task,
            source_agent='beacon',
            filename=build_filename,
        )
        log(
            f'build-phase dispatched forge <- beacon '
            f'(task={task_id}, file={dest.name}, '
            f'resume={preflight_session[:12]}...)'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'build-phase dispatch FAILED for task {task_id}: '
            f'{type(e).__name__}: {e}. Beacon was already notified of '
            f'PROCEED; Larry must manually re-dispatch build phase.',
            'WARN',
        )


def _scan_session_log_for_latest_marker_text(
    session_id: Optional[str],
    parser,
    skip_exceptions: tuple,
) -> tuple[Optional[str], dict[str, str]]:
    """Walk a Claude session log for the latest assistant-turn marker text.

    Returns ``(latest_marker_text, latest_text_by_type)``:

    * ``latest_marker_text`` — the combined text of the LATEST assistant turn
      whose parse returns a non-None marker_type (so a revised verdict still
      wins over an earlier one), or None when the session log is missing,
      unreadable, or carries no parseable marker.
    * ``latest_text_by_type`` — maps each distinct ``marker_type`` seen across
      the session to the LATEST text that parsed as that type. Empty when
      there's no marker. When it has MORE THAN ONE key the session emitted
      conflicting verdicts across turns (e.g. a later turn ECHOING a
      `=== REVIEW_PASS ===` block over a real earlier `REVIEW_REVISION`);
      plain last-wins would silently pick the echo. The Mirror classifier
      uses this to apply a conservative-priority rule instead of last-wins so
      a PASS can't override a co-occurring non-PASS verdict and auto-merge a
      PR Mirror wanted revised — nervous-system-audit #14 (2026-06-05).

    Intermediate turns whose text raises one of ``skip_exceptions`` are
    skipped — that's mid-session noise (e.g. an agent reasoning about the
    marker grammar in prose), not her final verdict.

    ``parser`` is one of ``mrh.parse_mirror_marker`` / ``fph.parse_forge_marker``
    and must return ``(marker_type, payload, narrative)`` or raise. The caller
    passes the parser's own Malformed/Multiple exception types as
    ``skip_exceptions``.

    Per chain-discipline-marker-parser-and-regression-check-001 (2026-05-25)
    this is now the AUTHORITATIVE classification path, called BEFORE the
    outbox `result` fallback. The outbox `result` field only captures the
    final `claude -p` assistant turn; if the agent's session continued past
    the marker emit (Monitor timeout firing late, a misbehaving poll loop
    after REVIEW_PASS, etc.) the marker is invisible to a final-turn parser.
    """
    if not session_id:
        return None, {}
    try:
        candidates = list(CLAUDE_PROJECTS_ROOT.glob(f'*/{session_id}.jsonl'))
    except OSError:
        return None, {}
    if not candidates:
        return None, {}
    log_path = candidates[0]
    last_marker_text: Optional[str] = None
    latest_text_by_type: dict[str, str] = {}
    try:
        with log_path.open('r', encoding='utf-8') as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get('message')
                if not isinstance(msg, dict) or msg.get('role') != 'assistant':
                    continue
                content = msg.get('content')
                if not isinstance(content, list):
                    continue
                text_parts = [
                    c.get('text', '') for c in content
                    if isinstance(c, dict) and c.get('type') == 'text'
                ]
                combined = '\n'.join(t for t in text_parts if t)
                if not combined.strip():
                    continue
                try:
                    marker_type, _payload, _narrative = parser(combined)
                except skip_exceptions:
                    continue
                if marker_type is not None:
                    last_marker_text = combined
                    latest_text_by_type[marker_type] = combined
    except OSError:
        return None, {}
    return last_marker_text, latest_text_by_type


def _recover_marker_text_from_session_log(
    session_id: Optional[str],
) -> tuple[Optional[str], dict[str, str]]:
    """Mirror-parser binding of `_scan_session_log_for_latest_marker_text`.

    Returns ``(latest_marker_text, latest_text_by_type)`` — see the scanner
    docstring. The Mirror classifier uses the per-type map to apply the
    conservative-priority verdict rule when a session emits more than one
    verdict type across turns (#14).
    """
    return _scan_session_log_for_latest_marker_text(
        session_id,
        mrh.parse_mirror_marker,
        (mrh.MalformedMirrorMarker, mrh.MultipleMirrorMarkers),
    )


def _recover_forge_marker_text_from_session_log(
    session_id: Optional[str],
) -> tuple[Optional[str], dict[str, str]]:
    """Forge-parser binding of `_scan_session_log_for_latest_marker_text`.

    Returns ``(latest_marker_text, latest_text_by_type)`` — see the scanner
    docstring. Forge routing keeps plain last-wins (uses ``latest_marker_text``);
    the per-type map is unused here.
    """
    return _scan_session_log_for_latest_marker_text(
        session_id,
        fph.parse_forge_marker,
        (fph.MalformedForgeMarker, fph.MultipleForgeMarkers),
    )


# mirror-prose-verdict-fallback-001 (2026-06-17). A STRICT, line-anchored
# prose-verdict declaration (e.g. `**Verdict: PASS.**`). Modeled on the
# anchoring discipline of `_BARE_MIRROR_KEYWORD_RE` (mirror_review_handler.py):
# the verdict must be essentially the WHOLE line (only surrounding markdown
# emphasis / leading quote-bullet and trailing punctuation allowed), so a
# mid-sentence mention ("I considered REVISION but...") or the marker-discipline
# reminder text never matches. Token set mirrors the canonical verdicts;
# `EMERGENCY HALT` (space) and `EMERGENCY_HALT` (underscore) both match.
_MIRROR_PROSE_VERDICT_RE = re.compile(
    r'(?im)^[\s>*_~`#-]*verdict[\s*_~`]*:[\s*_~`]*'
    r'(PASS|REVISION|ESCALATE|EMERGENCY[\s_]HALT)\b[\s*_~`.!)\]]*$'
)


def _scan_mirror_prose_verdicts(text: Optional[str]) -> set[str]:
    """Return the set of DISTINCT prose verdict tokens declared in ``text``.

    Tokens are normalized to the canonical set
    {PASS, REVISION, ESCALATE, EMERGENCY_HALT}. Only strict, line-anchored
    `Verdict: <TOKEN>` declarations count (see ``_MIRROR_PROSE_VERDICT_RE``);
    loose mid-sentence mentions do not. Empty set when ``text`` is empty or
    declares no verdict.
    """
    if not isinstance(text, str) or not text:
        return set()
    found: set[str] = set()
    for m in _MIRROR_PROSE_VERDICT_RE.finditer(text):
        found.add(re.sub(r'\s+', '_', m.group(1).upper()))
    return found


def _recover_full_session_text(session_id: Optional[str]) -> str:
    """Concatenate ALL assistant text turns from a session log.

    Unlike `_recover_marker_text_from_session_log` — which retains only the
    text of turns that PARSED as a canonical marker, and is therefore empty
    for a session that emitted no marker at all — this returns every
    assistant turn's text joined. The prose-verdict fallback in
    `_classify_mirror_marker` needs the WHOLE session to apply its
    no-contradiction gate (synthesize a PASS only when no OTHER verdict is
    declared anywhere). Returns '' when the log is unavailable; the caller
    then falls back to the outbox `result` (final-turn) text.
    """
    if not session_id:
        return ''
    try:
        candidates = list(CLAUDE_PROJECTS_ROOT.glob(f'*/{session_id}.jsonl'))
    except OSError:
        return ''
    if not candidates:
        return ''
    parts: list[str] = []
    try:
        with candidates[0].open('r', encoding='utf-8') as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get('message')
                if not isinstance(msg, dict) or msg.get('role') != 'assistant':
                    continue
                content = msg.get('content')
                if not isinstance(content, list):
                    continue
                for c in content:
                    if isinstance(c, dict) and c.get('type') == 'text':
                        t = c.get('text', '')
                        if t:
                            parts.append(t)
    except OSError:
        return ''
    return '\n'.join(parts)


def _maybe_synthesize_timeout_escalate(
    data: dict[str, Any],
) -> Optional[tuple[str, dict[str, Any]]]:
    """Synthesize a REVIEW_ESCALATE for a phase=review session the HARNESS
    killed at the wall-clock ceiling (agent_runner.REVIEW_SESSION_CEILING_SECONDS).

    A timed-out review emits no canonical verdict marker, so without this it
    falls into the marker-error net — 3 retries that re-prompt a session which
    no longer exists, then a dead-letter — or, in the worst case observed, gets
    force-merged with NO review (#713, 2026-06-26). A wall-clock kill is
    unambiguous and terminal: route it to Beacon as an INCONCLUSIVE escalate
    (the safe, recoverable action — escalate auto-routes, never dead-letters to
    Larry) with the timeout as the reason. Gated strictly on the `timed_out`
    outbox flag that run_claude sets; absent/false → None and normal marker
    classification proceeds.

    Returns ``('review_escalate', payload)`` on synthesis, else ``None``.
    """
    if not data.get('timed_out'):
        return None
    secs = data.get('timeout_seconds')
    reason = (
        'Mirror review exceeded the harness wall-clock ceiling'
        + (f' ({secs}s)' if secs else '')
        + ' and was killed (review_session_timeout). No verdict marker was '
        'emitted; routing as an inconclusive escalate so the PR is neither '
        'force-merged nor left to a marker-error retry on a dead session.'
    )
    payload = {
        'task_id': data.get('task_id'),
        'pr_url': data.get('pr_url'),
        'reason': reason,
        # Stay within the marker contract (mirror_review_handler:
        # ALLOWED_SEVERITY_BY_MARKER['review_escalate'] == ('high',);
        # ALLOWED_CONFIDENCE == ('high','medium','low')) so a future
        # check_marker_semantics on synthesized payloads can't reject this and
        # re-create the marker-error storm. An unfinished review IS plan-
        # blocking (severity high); the "inconclusive" nature is in `reason`.
        'severity': 'high',
        'confidence': 'low',
    }
    log(
        f'REVIEW_TIMEOUT_ESCALATE_SYNTHESIZED task={data.get("task_id")!r} '
        f'pr_url={data.get("pr_url")!r} timeout_seconds={secs!r} — review '
        f'session was harness-killed at the ceiling; synthesized REVIEW_ESCALATE.'
    )
    return 'review_escalate', payload


def _maybe_synthesize_prose_pass(
    data: dict[str, Any], result_text: str,
) -> Optional[tuple[str, dict[str, Any]]]:
    """PASS-only prose-verdict fallback for a phase=review Mirror session.

    When Mirror finishes a review successfully but states her verdict in
    prose (`**Verdict: PASS.**`) instead of the canonical
    `=== REVIEW_PASS ===` marker, synthesize the REVIEW_PASS payload from
    the review envelope so the EXISTING auto-merge path fires — instead of
    burning a marker-error retry round (~$1 / ~7 min) that re-runs the whole
    review. Strict, PASS-only, irreversibility-guarded:

      * SCOPE — synthesize ONLY PASS. REVIEW_PASS's required fields
        (task_id, pr_url, summary) are fully derivable from the envelope
        with zero fabrication. REVISION/ESCALATE/EMERGENCY_HALT require
        findings/severity/confidence/reason that cannot be faithfully
        reconstructed from prose, so those keep raising (a retry yields a
        real structured marker, not a fabricated one).
      * NO-CONTRADICTION GATE — the set of prose verdicts declared across
        the WHOLE session must be EXACTLY {PASS}. Zero declarations, PASS
        co-occurring with any other verdict, or any non-PASS verdict → no
        synthesis (fall through to the existing raise/retry). A PASS routes
        to irreversible auto-merge; never guess it when any contradicting
        signal is present (same conservative-priority philosophy as the
        cross-turn verdict-conflict rule).
      * Requires a non-empty envelope `pr_url` (the PR to merge against).
        Absent/empty → no synthesis.

    Returns ``('review_pass', payload)`` on synthesis, else ``None``.
    """
    pr_url = data.get('pr_url')
    if not isinstance(pr_url, str) or not pr_url.strip():
        return None
    session_text = _recover_full_session_text(data.get('claude_session_id'))
    scan_text = session_text or (
        result_text if isinstance(result_text, str) else ''
    )
    verdicts = _scan_mirror_prose_verdicts(scan_text)
    if verdicts != {'PASS'}:
        return None
    task_id = data.get('task_id')
    payload = {
        'task_id': task_id,
        'pr_url': pr_url,
        'summary': (
            'Verdict recovered from an unambiguous prose PASS '
            '("Verdict: PASS"); no canonical REVIEW_PASS marker was emitted, '
            'so it was synthesized at classification to proceed with '
            'auto-merge.'
        ),
    }
    match = _MIRROR_PROSE_VERDICT_RE.search(scan_text)
    snippet = match.group(0).strip() if match is not None else ''
    log(
        f'MIRROR_PROSE_VERDICT_SYNTHESIZED task={task_id!r} '
        f'synthesized REVIEW_PASS from prose verdict {snippet!r} '
        f'(pr_url={pr_url!r})'
    )
    return 'review_pass', payload


def _classify_mirror_marker(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Inspect a Mirror outbox for a review marker. Returns routing decision or None.

    Phase D3.5 commit 5a. Parallel to `_classify_forge_marker` — same return
    shape, same envelope-vs-marker task_id discipline, same dead-letter
    cascade behavior on malformed markers.

    The confidence-promote rule is applied internally: a REVIEW_REVISION
    with `confidence: low` returns the routing decision shaped as an
    ESCALATE (intent=`review-escalate`) but with `auto_promoted=True` so
    downstream logging records that Mirror said revise + we routed escalate.

    Raises `mrh.MalformedMirrorMarker` or `mrh.MultipleMirrorMarkers` if the
    marker block is present but unparseable. The caller dead-letters those
    back to Mirror so she can re-emit cleanly.

    Returns None if the outbox has no marker (Mirror's chat-mode outputs
    take the default routing path).
    """
    # PR-S4 rectification (M4): defensive gate for Mirror DAG-preflight
    # sessions. Per `agents/mirror/CLAUDE.md:362-368`, DAG preflight
    # responses MUST emit only `result: PASS/REVISION` — not the
    # PR-review REVIEW_* markers — because they have no `pr_url` to
    # anchor against and would route through auto-merge / replan paths
    # that don't apply. If Mirror accidentally emits a stray REVIEW_PASS
    # / REVIEW_REVISION marker in her DAG-preflight session log (habit
    # carryover from regular review work), this classifier picks it up
    # and the auto-merge path fires against a fictional PR. Defense in
    # depth: short-circuit on the envelope's prompt prefix and let
    # `_handle_mirror_dag_preflight_result` own the routing decision.
    envelope_prompt = data.get('prompt', '')
    if (
        isinstance(envelope_prompt, str)
        and envelope_prompt.lstrip().startswith('review-sequence-dag')
    ):
        return None

    result_text = data.get('result', '')
    if not isinstance(result_text, str):
        result_text = ''

    # chain-discipline-marker-parser-and-regression-check-001 (2026-05-25):
    # scan the session log FIRST and pick the latest valid marker across all
    # assistant turns. The outbox `result` field captures only the final
    # `claude -p` assistant turn — if Mirror's session ran past the marker
    # (Monitor timeout firing after REVIEW_PASS; a misbehaving poll loop
    # holding the session open; any post-marker chatter), final-turn parsing
    # can either miss the marker entirely or, worse, return a different
    # verdict from a later non-marker turn. Always-scan-latest-wins is the
    # authoritative path; result_text is a fallback for when the session log
    # is unavailable (no session_id, missing file, etc.).
    marker_type, payload, _narrative = (None, None, '')

    # review-timeout-escalate-001 (2026-06-26) — AUTHORITATIVE harness-kill
    # override, evaluated BEFORE the session-log recovery below. A timed-out
    # review (the harness hit agent_runner.REVIEW_SESSION_CEILING_SECONDS and
    # killed the session) did NOT complete, so any marker recoverable from its
    # PARTIAL transcript — notably an early `=== REVIEW_PASS ===` it printed
    # before wedging (the documented #101/#334 'Monitor timeout firing after
    # REVIEW_PASS' shape) — must NOT be trusted for the irreversible auto-merge.
    # Synthesize an inconclusive REVIEW_ESCALATE and SKIP recovery so a killed
    # review routes to Beacon instead of force-merging on a stale PASS (#713).
    if data.get('phase') == 'review':
        _timeout_escalate = _maybe_synthesize_timeout_escalate(data)
        if _timeout_escalate is not None:
            marker_type, payload = _timeout_escalate

    recovered, recovered_by_type = (
        _recover_marker_text_from_session_log(data.get('claude_session_id'))
        if marker_type is None else ('', {})
    )
    if recovered:
        # nervous-system-audit #14 (2026-06-05) — conservative-priority
        # verdict selection when a session emits MORE THAN ONE verdict type
        # across turns. Plain last-wins lets a later turn that merely ECHOES a
        # `=== REVIEW_PASS ===` block override a real earlier REVIEW_REVISION
        # — and a REVIEW_PASS routes straight to auto-merge (irreversible). We
        # can't tell a genuine mid-session re-verdict from an echo by type
        # alone, so we don't try: a PASS is honored ONLY when it is the sole
        # verdict type. If any non-PASS verdict co-occurs, the most
        # conservative (highest-severity) verdict present wins — the safe
        # action (don't merge; route revision/escalate/halt) is always
        # recoverable, so this stays auto-routed and never dead-letters to
        # Larry (alert-toil discipline). Single-verdict sessions (the normal
        # case) are unaffected: last-wins == the one type's latest text.
        chosen_text = recovered
        if len(recovered_by_type) > 1:
            # Severity order, most-conservative first. A type not in this
            # list (future marker) falls through to last-wins via `recovered`.
            for _t in (
                'review_emergency_halt',
                'review_escalate',
                'review_revision',
                'review_pass',
            ):
                if _t in recovered_by_type:
                    chosen_text = recovered_by_type[_t]
                    break
            if chosen_text is not recovered:
                log(
                    f'mirror verdict conflict across session turns '
                    f'({sorted(recovered_by_type)}); conservative-priority '
                    f'selection overrode last-wins to avoid an echoed-PASS '
                    f'auto-merge (task={data.get("task_id")!r})',
                    'WARN',
                )
        marker_type, payload, _narrative = mrh.parse_mirror_marker(chosen_text)
        if marker_type is not None:
            log(
                f'classified mirror {marker_type} marker from session log scan '
                f'(session={(data.get("claude_session_id") or "")[:12]}..., '
                f'task={data.get("task_id")!r})'
            )

    if marker_type is None and result_text.strip():
        # Session log unavailable or carried no marker — fall back to
        # final-turn parsing on the outbox `result`. This preserves the
        # malformed-marker dead-letter behavior when the only marker the
        # agent emitted is broken JSON.
        marker_type, payload, _narrative = mrh.parse_mirror_marker(result_text)

    if marker_type is None:
        # fix-mirror-verdict-marker-gate-001 (2026-06-03) — review-discipline
        # runtime gate, SYMMETRIC with `_classify_forge_marker`'s preflight
        # gate above. A `phase=review` dispatch MUST end with one canonical
        # verdict marker. When Mirror emits a PROSE verdict instead
        # (`**Verdict: PASS.**`), `parse_mirror_marker` correctly returns no
        # marker_type — but pre-fix this fell through to `return None`,
        # default routing skipped the auto-merge block (which keys on the
        # canonical REVIEW_PASS marker), and the PR sat open indefinitely
        # while heal_pr_auto_merge missed it (no merge was ever ATTEMPTED).
        # PR #277 was exactly this shape. Raise into the EXISTING
        # marker-error kickback (caught in process_outbox →
        # _notify_mirror_marker_error → 3 retries → dead-letter to Beacon +
        # DM Larry) so Mirror re-emits a canonical marker and auto-merge
        # fires. We fix at the source (classification); we do NOT teach the
        # merger to read prose.
        #
        # Scope: ONLY `phase=review`. The DAG-preflight path
        # (`review-sequence-dag` prompt) legitimately uses `result: PASS`
        # not markers and carries no `phase` field — it already short-circuits
        # to None at the top of this function and is consumed by
        # _handle_mirror_dag_preflight_result before we get here. Mirror's
        # chat-mode outputs (no phase=review) also keep returning None.
        if data.get('phase') == 'review':
            # mirror-prose-verdict-fallback-001 (2026-06-17). Before the
            # retry-triggering raise, try to RECOVER an unambiguous prose
            # PASS. When Mirror states `**Verdict: PASS.**` (no canonical
            # marker), synthesizing REVIEW_PASS from the envelope lets the
            # existing auto-merge path fire and skips the ~$1/~7min
            # marker-error retry. PASS-only + no-contradiction gated; any
            # ambiguity or non-PASS verdict falls through to the raise.
            # (A harness-timed-out review never reaches here — it is short-
            # circuited to REVIEW_ESCALATE at the top of this function.)
            synthesized = _maybe_synthesize_prose_pass(data, result_text)
            if synthesized is None:
                raise mrh.MalformedMirrorMarker(
                    'phase=review requires ONE canonical verdict marker at end '
                    'of response (=== REVIEW_PASS === / REVIEW_REVISION / '
                    'REVIEW_ESCALATE / REVIEW_EMERGENCY_HALT) — none found. A '
                    'PROSE verdict (e.g. "Verdict: PASS") is silently invisible '
                    'to auto-merge and leaves the PR stuck. Re-emit via marker.py '
                    "(see agents/mirror/CLAUDE.md 'Marker formats')."
                )
            marker_type, payload = synthesized
        else:
            return None

    # Marker discipline: payload task_id MUST match envelope task_id
    # (same shape as Forge handler's 4b check). Drift here means Mirror
    # reviewed the wrong PR; route as marker-error so she re-emits.
    envelope_task_id = data.get('task_id')
    marker_task_id = (
        payload.get('task_id') if isinstance(payload, dict) else None
    )
    if (
        envelope_task_id is not None
        and marker_task_id is not None
        and marker_task_id != envelope_task_id
    ):
        # Record the cross-identity claim BEFORE raising (same bridge as Forge):
        # Mirror reviewed/approved a PR whose marker task_id drifted from the
        # envelope, so a later Launch of that task_id can de-duplicate.
        _record_deliverable_claim(
            claimed_task_id=marker_task_id,
            envelope_task_id=envelope_task_id,
            agent=data.get('agent', 'mirror'),
            target_repo=data.get('target_repo'),
        )
        raise mrh.MalformedMirrorMarker(
            f'marker task_id ({marker_task_id!r}) does not match envelope '
            f'task_id ({envelope_task_id!r})'
        )

    agent = data.get('agent', 'mirror')
    auto_promoted = mrh.should_auto_promote(marker_type, payload)

    # D3.5 5b: evaluate revision budget for REVIEW_REVISION markers. If the
    # next dispatch would exceed max_revisions, downgrade to ESCALATE-shaped
    # routing (Beacon's existing handler) with a budget-exhausted reason
    # field. No new intent vocabulary per Larry's 5b signoff (option A).
    # m-5 review fix: evaluate even when auto_promoted, so the combined
    # case (low confidence AND budget exhausted) renders the budget reason
    # (the stronger termination signal) rather than just the auto-promote
    # reason. derive_intent handles both flags identically (escalate); only
    # the reason text differs.
    budget_exhausted = False
    if marker_type == 'review_revision':
        decision_str, _next_count, _max_count = mrh.evaluate_revision_budget(data)
        if decision_str == 'exhausted':
            budget_exhausted = True

    # Per-marker intent_kwargs for INTENT_ACTION_BLOCKS rendering. Keep
    # aligned with the templates in INTENT_ACTION_BLOCKS — missing keys
    # degrade gracefully via build_notify_prompt's exception handler, but
    # cleaner to supply them upfront.
    pr_url = payload.get('pr_url', '(no PR URL)') if isinstance(payload, dict) else '(no PR URL)'

    if marker_type == 'review_pass':
        intent_kwargs = {
            'pr_url': pr_url,
            'summary': payload.get('summary', '(no summary)'),
            # false-success-notify-fix (2026-06-11): default GitHub-truth
            # merge-status line = "requested/pending". The marker-routing
            # block in process_outbox runs the auto-merge BEFORE the notify
            # and overwrites this with the real gh-confirmed outcome line so
            # the notify never claims a merge that hasn't happened.
            'merge_status_line': _render_review_pass_merge_status_line(None),
        }
    elif marker_type == 'review_revision':
        # m-5 review fix: budget_exhausted is the stronger termination
        # signal — render its reason even when auto_promoted is also True
        # (low-confidence revision at round 3+ → user should see "loop
        # exhausted on round N", not "Mirror was uncertain"; the latter is
        # implied by the former).
        if budget_exhausted:
            current = data.get('revision_count', 0)
            if not isinstance(current, int) or current < 0:
                current = 0
            max_count = data.get('max_revisions', mrh.DEFAULT_MAX_REVISIONS)
            if not isinstance(max_count, int) or max_count < 0:
                max_count = mrh.DEFAULT_MAX_REVISIONS
            reason = mrh.build_budget_exhausted_reason(
                payload, current + 1, max_count,
            )
            if auto_promoted:
                # Append a note that the low-confidence promote ALSO would
                # have triggered escalate — preserves the audit trail.
                reason = (
                    reason
                    + ' (Mirror also flagged this REVISION with '
                    'confidence: low — the auto-promote rule would have '
                    'routed it to ESCALATE regardless of budget.)'
                )
            intent_kwargs = {
                'pr_url': pr_url,
                'severity': payload.get('severity', '?'),
                'confidence': payload.get('confidence', '?'),
                'reason': reason,
            }
        elif auto_promoted:
            intent_kwargs = {
                'pr_url': pr_url,
                'severity': payload.get('severity', '?'),
                'confidence': payload.get('confidence', '?'),
                'reason': mrh.build_auto_promote_reason(payload),
            }
        else:
            findings = payload.get('findings') or []
            finding_count = len(findings) if isinstance(findings, list) else 0
            intent_kwargs = {
                'pr_url': pr_url,
                'finding_count': finding_count,
                'severity': payload.get('severity', '?'),
                'confidence': payload.get('confidence', '?'),
            }
    elif marker_type == 'review_escalate':
        intent_kwargs = {
            'pr_url': pr_url,
            'severity': payload.get('severity', '?'),
            'confidence': payload.get('confidence', '?'),
            'reason': payload.get('reason', '(no reason)'),
        }
    elif marker_type == 'review_emergency_halt':
        intent_kwargs = {
            'pr_url': pr_url,
            'reason': payload.get('reason', '(no reason)'),
            'evidence': payload.get('evidence', '(no evidence)'),
        }
    else:
        # Defensive: future marker types should at minimum render the pr_url.
        intent_kwargs = {'pr_url': pr_url}

    return {
        'marker_type': marker_type,
        'payload': payload,
        'intent': mrh.derive_intent(
            marker_type,
            auto_promoted=auto_promoted,
            budget_exhausted=budget_exhausted,
        ),
        'notify_source': mrh.derive_notify_source(agent),
        'intent_kwargs': intent_kwargs,
        'auto_promoted': auto_promoted,
        'budget_exhausted': budget_exhausted,
        # Parallel field to Forge's clarification_count for cascade plumbing.
        'next_clarification_count': None,
    }


_DAG_RESULT_VERDICT_RE = re.compile(
    r'(?im)^\s*[\*\-]?\s*"?result"?\s*[:=]\s*"?(PASS|REVISION)"?',
)
_DAG_PROMPT_RE = re.compile(r'^\s*review-sequence-dag\s+(\S+)')


def _parse_dag_preflight_verdict(result_text: str) -> Optional[str]:
    """Scan a Mirror DAG-preflight outbox result body for the verdict.

    Mirror's DAG-preflight protocol (agents/mirror/CLAUDE.md:362-368)
    requires emitting `result: PASS` or `result: REVISION` in the final
    chat body — that's the entire automation surface for this dispatch
    (no REVIEW_* marker, no pr_url). Liberal in what we accept: any
    line, anywhere in the body, of the shape `result: PASS|REVISION`
    (case-insensitive, with optional leading bullet/dash/quote). The
    first match wins.

    Returns 'PASS' or 'REVISION' on match, None on no match (caller
    treats no-match as malformed → DMs Larry per the H1 contract).
    """
    if not isinstance(result_text, str) or not result_text:
        return None
    m = _DAG_RESULT_VERDICT_RE.search(result_text)
    if m is None:
        return None
    return m.group(1).upper()


def _append_dag_revision_audit(
    seq_path: Path, seq_id: str, mirror_task_id: str,
) -> None:
    """Append a `dag-preflight-revision-routed` entry to the sequence's
    audit_log (best-effort, atomic write).

    Gives the heal_pipeline_stall stalled-pending-sequence backstop a
    durable signal of an UNRESOLVED REVISION that does not depend on log
    retention. Parallels the PASS path's `dag-preflight-pass-kickoff`
    entry. Does NOT change `status` — the sequence stays `pending` until
    Beacon amends + re-dispatches and Mirror PASSes (which the existing
    PASS branch records as `dag-preflight-pass-kickoff`, resolving the
    stall signal). Idempotent on `mirror_task_id` so a re-processed Mirror
    outbox doesn't double-record the same REVISION round.
    """
    try:
        seq = json.loads(seq_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=REVISION; could not '
            f'read sequence file to append audit entry ({type(e).__name__}: '
            f'{e}); Beacon notify already routed',
            'WARN',
        )
        return
    if not isinstance(seq.get('audit_log'), list):
        seq['audit_log'] = []
    already = any(
        isinstance(e, dict)
        and e.get('event') == 'dag-preflight-revision-routed'
        and e.get('mirror_task_id') == mirror_task_id
        for e in seq['audit_log']
    )
    if already:
        return
    seq['audit_log'].append({
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': 'dag-preflight-revision-routed',
        'actor': 'outbox-notifier',
        'mirror_task_id': mirror_task_id,
    })
    tmp_path = seq_path.with_suffix(seq_path.suffix + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(seq, f, indent=2)
            f.write('\n')
        os.replace(tmp_path, seq_path)
    except OSError as e:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=REVISION; could not '
            f'write audit entry to sequence file ({e}); Beacon notify '
            f'already routed',
            'WARN',
        )


def _handle_mirror_dag_preflight_result(data: dict[str, Any]) -> Optional[str]:
    """PR-S4 rectification (H1) — handle Mirror's DAG-preflight session result.

    The DAG-preflight dispatch is keyed on the envelope prompt
    `review-sequence-dag <seq-id>`. Mirror's response carries a
    `result: PASS|REVISION` verdict in the chat body (NOT a REVIEW_*
    marker, per `agents/mirror/CLAUDE.md:362-368` — those expect
    pr_url context that doesn't apply to a sequence-file review).

    Routing decision per verdict:
      * PASS — the sequence's DAG is sound. Transition the sequence
        file from `status: pending` to `status: active` (same shape as
        the kickoff handler, atomic write + audit_log entry). The next
        advancer tick dispatches the first step. No Larry approval gate
        here: Larry already implicitly approved by chatting Beacon to
        author the sequence; the DAG-preflight gate IS the approval.
      * REVISION — route an inter-agent `dag-preflight-revision` notify
        to Beacon's inbox (reusing the code-review notify-writer), so the
        bot resumes Beacon and she amends the sequence file + re-dispatches
        the DAG-preflight autonomously — symmetric with the PASS self-heal.
        Record the routing as a `dag-preflight-revision-routed` audit_log
        entry (the stalled-sequence healer's durable signal). NO Larry DM
        on the happy path (the raw verdict is agent-to-agent, not a Larry
        decision); only a notify-write FAILURE DMs Larry (protocol broke).
      * malformed (no PASS/REVISION verdict parsed) — DM Larry with a
        `mirror-dag-malformed-result:<seq-id>` alert so the failure is
        loud, not silent.

    Returns:
      * str sentinel when the handler claimed the outbox (PASS / REVISION
        / malformed paths). Caller archives without falling through.
      * None when the envelope doesn't look like a DAG-preflight
        response (wrong prompt prefix, missing prompt field). Caller
        falls through to the existing marker classifier path.
    """
    if data.get('agent') != 'mirror':
        return None
    envelope_prompt = data.get('prompt', '')
    if not isinstance(envelope_prompt, str):
        return None
    m = _DAG_PROMPT_RE.match(envelope_prompt)
    if not m:
        return None
    seq_id = m.group(1)
    task_id = data.get('task_id') or f'review-sequence-dag-{seq_id}'
    result_text = data.get('result', '') or ''
    verdict = _parse_dag_preflight_verdict(result_text)

    seq_path = AGENTS_ROOT / 'blackboard' / 'build-sequences' / f'{seq_id}.json'

    if verdict is None:
        # Malformed Mirror DAG-preflight response — loud failure.
        msg = (
            f'Mirror DAG-preflight result for sequence `{seq_id}` is '
            f'malformed: no `result: PASS` or `result: REVISION` verdict '
            f'found in the chat body. Re-dispatch the review (Mirror '
            f'CLAUDE.md § DAG preflight `result:` shape) or inspect '
            f'`{seq_path}` directly. Mirror task_id: `{task_id}`.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'mirror-dag-malformed-result:{seq_id}',
        )
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=MALFORMED '
            f'task={task_id}; DMed Larry',
            'WARN',
        )
        return f'mirror-dag-preflight:malformed:{seq_id}'

    if verdict == 'REVISION':
        body_snippet = result_text.strip()
        if len(body_snippet) > 1500:
            body_snippet = body_snippet[:1500] + '\n…(truncated)'
        # Self-heal symmetric with the PASS auto-activate path: route an
        # inter-agent notify to Beacon's inbox, reusing the SAME
        # safe_write_inbox + build_notify_prompt helpers the code-review
        # result path uses to reach Beacon (Shapes 6-9). The inbox_watcher
        # dispatches Beacon on arrival; she amends the sequence file +
        # re-dispatches the DAG-preflight autonomously. Mirror's raw verdict
        # is an agent-to-agent message, NOT a Larry decision — so per the
        # actionable-only discipline we do NOT DM Larry here. Larry only
        # hears about a REVISION via (a) Beacon escalating a genuine
        # scope/spec call as a one-line binary, or (b) the heal_pipeline_
        # stall stalled-pending-sequence backstop.
        notify_source = 'mirror-result'
        notify_prompt = build_notify_prompt(
            intent='dag-preflight-revision',
            sender='mirror',
            task_id=task_id,
            success=True,
            output=body_snippet,
            intent_kwargs={'seq_id': seq_id, 'seq_path': str(seq_path)},
        )
        notify_base = {
            'task_id': f'notify-dag-revision-{seq_id}',
            'prompt': notify_prompt,
            'source': notify_source,
            'intent': 'dag-preflight-revision',
            'seq_id': seq_id,
            'seq_path': str(seq_path),
            '_notify_depth': _current_notify_depth(data) + 1,
        }
        # A sequence-DAG routing signal carries no per-task chain context — the
        # seq_id/seq_path above are the payload. Every whitelisted field is an
        # explicit DROP (M1).
        notify_task = build_chain_envelope(
            notify_base,
            data,
            carry={
                'target_repo': DROP,
                'pr_url': DROP,
                'forge_build_session_id': DROP,
                'reply_chat_id': DROP,
                'revision_count': DROP,
                'replan_count': DROP,
                'max_replans': DROP,
            },
        )
        # Deterministic filename keyed on seq_id: re-processing the same
        # Mirror outbox overwrites the pending notify (atomic same-path
        # write) rather than writing a duplicate. Mirrors the code-review
        # notify dedup, which keys its filename on the outbox stem.
        notify_filename = f'notify-dag-revision-{seq_id}.json'
        try:
            dest = safe_write_inbox.safe_write_inbox(
                target_agent='beacon',
                task_dict=notify_task,
                source_agent=notify_source,
                filename=notify_filename,
            )
            log(
                f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=REVISION '
                f'task={task_id}; routed dag-preflight-revision notify to '
                f'beacon (file={dest.name})',
            )
        except (
            safe_write_inbox.DispatchRejected,
            safe_write_inbox.RoutingDenied,
        ) as e:
            # The autonomous path broke — Beacon won't self-heal this round.
            # That IS Larry-actionable (protocol failure), so DM him rather
            # than drop the REVISION silently. The stalled-sequence backstop
            # is the second line of defense.
            larry_alerts.append_alert(
                source='outbox-notifier',
                severity='warning',
                message=(
                    f'Mirror DAG-preflight REVISION for sequence `{seq_id}` '
                    f'could not be routed to Beacon ({type(e).__name__}: '
                    f'{e}). Amend `{seq_path}` per Mirror\'s findings below '
                    f'and re-dispatch the review manually.\n\n'
                    f'--- Mirror\'s verdict ---\n{body_snippet}'
                ),
                subject=f'mirror-dag-revision-route-failed:{seq_id}',
            )
            log(
                f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=REVISION '
                f'task={task_id}; FAILED to route notify to beacon: '
                f'{type(e).__name__}: {e}; DMed Larry',
                'WARN',
            )
            return f'mirror-dag-preflight:revision-route-failed:{seq_id}'

        # Record the routing in the sequence's audit_log so the stalled-
        # pending-sequence healer has a durable, log-retention-independent
        # signal of an UNRESOLVED REVISION (parallels the PASS path's
        # `dag-preflight-pass-kickoff` entry). Does NOT change `status`.
        _append_dag_revision_audit(seq_path, seq_id, task_id)

        # Actionable-only: the raw Mirror verdict is now an inter-agent
        # message, not a Larry DM. Log-only — no append_alert.
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=REVISION '
            f'task={task_id}; Larry DM suppressed (routed to Beacon for '
            f'autonomous amend)',
        )
        return f'mirror-dag-preflight:revision:{seq_id}'

    # verdict == 'PASS' — transition the sequence file pending → active.
    # Shape parallels `_handle_build_sequence_advancer_kickoff`: read,
    # validate JSON+DAG, idempotency-gate on status, atomic-write, audit.
    if not seq_path.is_file():
        msg = (
            f'Mirror DAG-preflight PASS for sequence `{seq_id}` but the '
            f'sequence file is missing at `{seq_path}`. Investigate — '
            f'this should not happen mid-protocol.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'mirror-dag-pass-file-missing:{seq_id}',
        )
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=PASS FAILED '
            f'file-missing task={task_id}',
            'WARN',
        )
        return f'mirror-dag-preflight:file-missing:{seq_id}'

    try:
        raw_text = seq_path.read_text()
        seq = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as e:
        msg = (
            f'Mirror DAG-preflight PASS for sequence `{seq_id}` but the '
            f'sequence file cannot be read/parsed at `{seq_path}` ({e}).'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'mirror-dag-pass-unreadable:{seq_id}',
        )
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=PASS FAILED '
            f'read/parse task={task_id}: {e}',
            'WARN',
        )
        return f'mirror-dag-preflight:unreadable:{seq_id}'

    current_status = seq.get('status')
    if current_status != 'pending':
        # Idempotent no-op — sequence already moved on. Mirror's PASS
        # arriving late on a sequence that's already active/complete/
        # etc. is not an error; just log + archive.
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=PASS WARN '
            f'already-kicked-off status={current_status} task={task_id}; '
            f'no-op',
            'WARN',
        )
        return f'mirror-dag-preflight:already-active:{seq_id}'

    seq['status'] = 'active'
    audit_entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': 'dag-preflight-pass-kickoff',
        'actor': 'outbox-notifier',
        'mirror_task_id': task_id,
    }
    if not isinstance(seq.get('audit_log'), list):
        seq['audit_log'] = []
    seq['audit_log'].append(audit_entry)

    tmp_path = seq_path.with_suffix(seq_path.suffix + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(seq, f, indent=2)
            f.write('\n')
        os.replace(tmp_path, seq_path)
    except OSError as e:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        msg = (
            f'Mirror DAG-preflight PASS for sequence `{seq_id}` but '
            f'cannot write the sequence file at `{seq_path}` ({e}).'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'mirror-dag-pass-write-error:{seq_id}',
        )
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=PASS FAILED '
            f'write-error task={task_id}: {e}',
            'WARN',
        )
        return f'mirror-dag-preflight:write-error:{seq_id}'

    larry_alerts.append_alert(
        source='outbox-notifier',
        severity='warning',
        message=(
            f'Mirror DAG-preflight PASS for sequence `{seq_id}`. '
            f'Sequence transitioned `pending` → `active`; the build '
            f'sequence advancer will dispatch the first step on its next '
            f'tick (≤5 min).'
        ),
        subject=f'mirror-dag-pass:{seq_id}',
    )
    log(
        f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=PASS status=pending'
        f'->active task={task_id}',
    )
    return f'mirror-dag-preflight:active:{seq_id}'


def _notify_mirror_marker_error(data: dict[str, Any], err_msg: str) -> None:
    """Write a marker-error notify back to Mirror so she can re-emit a clean marker.

    Phase D3.5 commit 5a. Parallel to `_notify_forge_marker_error` — same
    retry counter on the envelope (`marker_error_count`), same MAX cap,
    same dead-letter-to-original-dispatcher behavior on exhaust.

    The notify-prompt template (`marker-error` intent) is shared with Forge
    — agent-agnostic wording so both agents see the same retry shape. The
    receiver decides which marker grammar to re-emit by reading their own
    CLAUDE.md marker-discipline section.
    """
    agent = data.get('agent', 'mirror')
    task_id = data.get('task_id', 'unknown')

    # Trace original dispatcher (same pattern as Forge handler — on the
    # first malformed-marker round, source IS the original dispatcher; on
    # subsequent rounds it propagated via original_source).
    original_source = data.get('original_source') or data.get('source') or 'beacon'

    prev_count = data.get('marker_error_count', 0)
    if not isinstance(prev_count, int) or prev_count < 0:
        prev_count = 0
    new_count = prev_count + 1

    if new_count > MAX_MARKER_ERROR_RETRIES:
        log(
            f'marker-error retries exhausted ({new_count}/{MAX_MARKER_ERROR_RETRIES}) '
            f'for task {task_id} on agent {agent}; dead-lettering to {original_source}',
            'WARN',
        )
        _dead_letter_marker_error_to_dispatcher(
            data, original_source, err_msg, new_count,
        )
        return

    prompt = build_notify_prompt(
        intent='marker-error',
        sender='outbox-notifier',
        task_id=task_id,
        success=False,
        output=(data.get('result') or '')[:1000],
        intent_kwargs={
            'reason': err_msg,
            'task_id': task_id,
            'retry_count': new_count,
            'max_retries': MAX_MARKER_ERROR_RETRIES,
        },
    )
    # D3.5 5b-followup Bug B: same fix as Forge equivalent — keep envelope
    # task_id as the ORIGINAL task_id across retries (was wrapped before;
    # broke Mirror's marker contract). Retry tracking via marker_error_count;
    # filename uniqueness via the `-{new_count}` filename suffix.
    notify_base: dict[str, Any] = {
        'task_id': task_id,
        'prompt': prompt,
        'source': 'outbox-notifier',
        'intent': 'marker-error',
        '_notify_depth': 1,
        'original_source': original_source,
        'marker_error_count': new_count,
    }
    # Propagate envelope fields the agent needs to keep working on the same
    # task (session_id for --resume, branch for worktree gating). Same shape
    # as the Forge marker-error path.
    if data.get('claude_session_id'):
        notify_base['session_id'] = data['claude_session_id']
    if data.get('branch'):
        notify_base['branch'] = data['branch']
    if data.get('max_revisions') is not None:
        notify_base['max_revisions'] = data['max_revisions']
    if data.get('phase'):
        notify_base['phase'] = data['phase']
    # Chain context (M1). target_repo gates worktree; revision_count keeps the
    # review budget mid-retry; reply_chat_id closes the Telegram thread on
    # three-strike dead-letter (5a M-3); forge_build_session_id (5b M-7) lets a
    # retry that emits a clean REVIEW_REVISION still resolve Forge's session in
    # _dispatch_revision_to_forge. replan_count/max_replans aren't part of the
    # Mirror marker-error retry context.
    notify_task = build_chain_envelope(
        notify_base,
        data,
        carry={
            'target_repo': CARRY,
            'pr_url': CARRY,
            'revision_count': CARRY,
            'reply_chat_id': CARRY,
            'forge_build_session_id': CARRY,
            'replan_count': DROP,
            'max_replans': DROP,
        },
    )

    if _is_fixture_emission(task_id):
        log(
            f'suppressing marker-error notify for fixture task {task_id} '
            f'(reserved fixture namespace)'
        )
        return

    try:
        safe_write_inbox.safe_write_inbox(
            target_agent=agent,
            task_dict=notify_task,
            source_agent='outbox-notifier',
            filename=f'marker-error-{task_id}-{new_count}.json',
        )
        log(
            f'marker-error notify written to {agent} for task {task_id} '
            f'(retry {new_count}/{MAX_MARKER_ERROR_RETRIES})'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'marker-error notify failed for {agent}/{task_id}: '
            f'{type(e).__name__}: {e}',
            'WARN',
        )


def _extract_pr_url_from_build_result(result_text: str) -> Optional[str]:
    """Return the PR URL from a Forge build-phase outbox, or None if absent.

    Phase D3.5 commit 5a. Forge's CLAUDE.md asks the build response to lead
    with `PR opened: <url>` / `PR updated: <url>`, but the regex is line-
    anchored (not string-anchored) per 5d-followup-2 so the URL is accepted
    on its own line anywhere in the response — Forge's lenient build-phase
    shape often narrates status bullets and puts the URL at the end. Mid-
    paragraph URLs ("I considered PR opened: <stale> last week") still don't
    match — the line anchor preserves the m-2 false-match protection.

    Returns the single PR URL when the result names exactly one (possibly
    repeated identical) PR, or None on empty/None input, no match, OR an
    AMBIGUOUS result that names more than one DISTINCT PR URL.

    nervous-system-audit #16 (2026-06-05): the prior first-match `.search`
    dispatched/merged the WRONG PR when a stale `PR opened: <old>` line
    preceded the real one (e.g. Forge narrating a superseded attempt before
    announcing the fresh PR). Last-match has the mirror failure (a stale line
    AFTER the real one — which is the contract-following case, since Forge is
    asked to lead with the canonical `PR opened:` line). We can't reliably
    tell which of two distinct URLs is real, so we refuse to guess: distinct
    multiples return None + a WARN, leaving the inline build→review dispatch
    skipped so the reconciliation sweep / Larry resolves it rather than
    auto-dispatching the wrong PR in EITHER direction. Repeated identical
    lines are not ambiguous — that single URL is returned.
    """
    if not isinstance(result_text, str) or not result_text:
        return None
    urls = [m.group(1) for m in _PR_URL_RE.finditer(result_text)]
    if not urls:
        return None
    distinct = set(urls)
    if len(distinct) > 1:
        log(
            f'_extract_pr_url_from_build_result: {len(distinct)} distinct PR '
            f'URLs in build result {urls!r}; refusing to guess which is real '
            f'(returning None — reconcile sweep / Larry resolves)',
            'WARN',
        )
        return None
    return urls[0]


def _recorded_review_head_sha(path: Path) -> Optional[str]:
    """Read the `head_sha` a review-request was dispatched for, or None.

    Looks top-level first, then under `context` (chain envelope nesting). A
    review-request written before head_sha was recorded returns None — the
    caller treats that as "doesn't cover the current head" so the PR gets a
    fresh review (the safe direction)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    v = data.get('head_sha')
    if isinstance(v, str) and v:
        return v
    ctx = data.get('context')
    if isinstance(ctx, dict):
        v = ctx.get('head_sha')
        if isinstance(v, str) and v:
            return v
    return None


# died-verdictless-review-redispatch (2026-07-07, post-#850). Bounds on
# re-dispatching a review whose run's result was positively marked LOST
# (inbox_watcher archived the envelope under `.archive/.lost-result/` because
# the run's outbox could not be persisted — see LOST_RESULT_SUBDIR in
# safe_write_inbox). The grace is measured on the lost envelope's mtime, which
# `move_to()`'s rename preserves from dispatch time — so "one re-dispatch per
# grace per (task, head)" is really "no re-dispatch until the failed round is
# at least this old". The attempts cap counts lost-result copies for the same
# (task, head): past it the predicate returns to PERMANENT dedup (the pre-fix
# fail-safe) with an ERROR log, so a persistently-failing environment (e.g.
# disk-full making every outbox write fail) costs at most
# REVIEW_REDISPATCH_MAX_ATTEMPTS paid review runs, never an unbounded loop.
REVIEW_REDISPATCH_GRACE_SECONDS = 3600
REVIEW_REDISPATCH_MAX_ATTEMPTS = 3


def _review_request_already_dispatched(
    review_filename: str, current_head_sha: Optional[str] = None,
) -> bool:
    """True if a review-request with this filename is already in Mirror's
    inbox, its `.archive/`, or its `.invalid/`.

    Single source of truth for the review-request idempotency presence check
    so `_dispatch_mirror_review` (inline build-phase dispatch) and the
    reconciliation sweep can't diverge. Guards against re-dispatching a Mirror
    review for a PR she's already (or previously) reviewing — including a
    prior dispatch that was validator-rejected into `.invalid/`.

    Head-awareness (default OFF): when `current_head_sha` is given, an ARCHIVED
    or `.invalid` review only counts as "already dispatched" if it recorded
    THAT head. A review of an older head no longer blocks re-review of new
    commits — the gap that left a PR pushed-after-its-first-review stuck
    forever (the dedup keyed on task-id, not commit, so commits after the first
    reviewed head never got re-reviewed). A LIVE review in the inbox still
    blocks regardless of head — Mirror is actively on it; never pile on.
    Callers that pass None (the reconcile sweep) keep the exact prior
    existence-only behavior. `move_to()` uniquifies archive collisions as
    `<stem>.<i><suffix>`, so a task accrues one archived copy per reviewed head
    — all are scanned for a head match.

    Died-verdictless recovery (2026-07-07, post-#850): a same-head envelope in
    `.archive/.lost-result/` — inbox_watcher's POSITIVE marker that the run's
    outbox could not be persisted, so the review produced no verdict and no
    downstream recovery (marker-error cascade, timeout escalate) could fire —
    does NOT dedup: after a REVIEW_REDISPATCH_GRACE_SECONDS debounce the head
    is re-dispatchable, bounded by REVIEW_REDISPATCH_MAX_ATTEMPTS lost rounds
    before the predicate returns to permanent dedup. Before #850 a review
    session pushed a `[WIP][session-start]` commit at pickup, so a died-
    verdictless review moved the PR head past the envelope's recorded head_sha
    and this head-aware dedup re-dispatched it — an ACCIDENTAL retry backstop.
    #850's read-only detached review checkout removed that push; the
    lost-result marker replaces it deliberately. Only the marked class
    re-dispatches — a PLAIN archived same-head envelope still dedups forever
    (fail CLOSED: naming drift, retention, or any unforeseen state keeps the
    old at-most-one-dispatch guarantee rather than looping paid reviews)."""
    mirror_inbox = safe_write_inbox.INBOXES_ROOT / 'mirror'
    # A live in-flight review blocks regardless of head (anti-storm).
    if (mirror_inbox / review_filename).exists():
        return True
    if current_head_sha is None:
        # Back-compat: pure existence in archive / .invalid.
        return (
            (mirror_inbox / '.archive' / review_filename).exists()
            or (mirror_inbox / '.invalid' / review_filename).exists()
        )
    # Head-aware: a prior review counts only if it covered the current head.
    stem = (
        review_filename[:-len('.json')]
        if review_filename.endswith('.json') else review_filename
    )
    for sub in ('.archive', '.invalid'):
        d = mirror_inbox / sub
        if not d.exists():
            continue
        # The exact name plus the `<stem>.<i>.json` uniquified collisions.
        # glob.escape so a task_id with glob metacharacters can't turn the
        # variant scan into a character class (which would miss its own
        # archives → re-dispatch storm) or match a sibling task.
        candidates = [
            d / review_filename,
            *sorted(d.glob(f'{glob.escape(stem)}.*.json')),
        ]
        for p in candidates:
            if p.exists() and _recorded_review_head_sha(p) == current_head_sha:
                return True
    # No live/archived/.invalid envelope covers this head. A same-head
    # LOST-RESULT envelope (run happened, outbox unpersistable, verdict lost)
    # deliberately does not dedup — but it debounces and caps the re-dispatch.
    lost_dir = mirror_inbox / '.archive' / safe_write_inbox.LOST_RESULT_SUBDIR
    if not lost_dir.exists():
        return False
    lost = [
        p for p in (
            lost_dir / review_filename,
            *sorted(lost_dir.glob(f'{glob.escape(stem)}.*.json')),
        )
        if p.exists() and _recorded_review_head_sha(p) == current_head_sha
    ]
    if not lost:
        return False
    if len(lost) >= REVIEW_REDISPATCH_MAX_ATTEMPTS:
        # Every allowed retry round ALSO lost its result — almost certainly an
        # environment fault (e.g. outboxes/ unwritable), not review flakiness.
        # Return to the pre-fix permanent dedup so a broken environment can't
        # loop paid Mirror runs; the ERROR line is the operator signal.
        log(
            f'died-verdictless review CAPPED for {review_filename} '
            f'(head {current_head_sha[:12]}): {len(lost)} lost-result rounds '
            f'>= {REVIEW_REDISPATCH_MAX_ATTEMPTS}; deduping permanently — '
            f'investigate why mirror outbox writes keep failing, then '
            f're-dispatch manually',
            'ERROR',
        )
        return True
    try:
        newest = max(p.stat().st_mtime for p in lost)
    except OSError:
        return True  # unreadable state → fail toward dedup (conservative)
    if time.time() - newest < REVIEW_REDISPATCH_GRACE_SECONDS:
        return True  # debounce: latest lost round dispatched under 1h ago
    log(
        f'died-verdictless review detected for {review_filename} '
        f'(head {current_head_sha[:12]}): {len(lost)} lost-result round(s), '
        f'no verdict; treating as NOT dispatched (re-dispatch attempt '
        f'{len(lost) + 1}/{REVIEW_REDISPATCH_MAX_ATTEMPTS})',
        'WARN',
    )
    return False


def _dispatch_mirror_review(
    data: dict[str, Any], pr_url: str, *, pr_state_known_open: bool = False,
) -> None:
    """Write a review-request task to Mirror's inbox after Forge opens a PR.

    Phase D3.5 commit 5a. Fires inside `process_outbox` when Forge's
    `phase=build` outbox carries `PR opened: <url>` in result. Same shape
    as `_dispatch_build_phase`: a single new dispatch keyed by the same
    task_id; the notify-to-Beacon from the default routing path still
    fires alongside (Beacon journals "Forge opened PR #N"; this dispatch
    starts Mirror's review).

    `source` is `beacon` because Beacon is the logical dispatcher (her
    APPROVAL_REQUEST authorized the work that produced the PR). The
    `dispatched_by: 'outbox-notifier'` audit field records the actual
    writer.

    Failure to write the review-request is logged WARN and non-fatal —
    the notify-to-Beacon above has already informed her of the PR; Larry
    sees the gap and can manually re-dispatch.

    `pr_state_known_open`: set by callers that have ALREADY confirmed the PR
    is OPEN this tick (the reconcile sweep), so the merged/closed dispatch-time
    guard skips a redundant `gh pr view`. Default False — the inline
    build-phase path performs its own cheap check.
    """
    task_id = data.get('task_id') or 'unknown'
    target_repo = data.get('target_repo')
    if not target_repo:
        # M3 (chain-context-durability §4): a missing target_repo is usually
        # *recoverable* — the task's chain_events carry the repo in their
        # payload. Backfill from the source of truth BEFORE concluding we must
        # dead-end; only a task with no repo-bearing event falls through to the
        # WARN below.
        target_repo = backfill_target_repo(task_id)
        if target_repo:
            log(
                f'target_repo backfilled to `{target_repo}` for task {task_id} '
                f'from chain_events (M3); proceeding with review dispatch',
                'INFO',
            )
    if not target_repo:
        # Without target_repo, Mirror's worktree gate (now active per 5a's
        # agent-models.json change) rejects the review task as "no canonical
        # path." Genuinely unrecoverable (no owning mission) — surface the gap
        # to Larry rather than silently dropping.
        log(
            f'PR opened on task {task_id} but no target_repo on envelope and '
            f'none derivable from chain_events; cannot dispatch review '
            f'(Mirror requires target_repo for worktree gating). Larry must '
            f'manually re-dispatch.',
            'WARN',
        )
        return

    branch = data.get('branch')
    # max_revisions sourced from config/agent-models.json `loop_bounds` —
    # propagated through the dispatch envelope so the budget is consistent
    # across the full review/revision cascade. M-4 review fix: actually
    # reads the config file (was hardcoded to DEFAULT_MAX_REVISIONS in 5a).
    max_revisions = _load_max_revisions_from_config()

    review_prompt_lines = [
        f'Review phase. Forge has opened PR `{pr_url}` for task `{task_id}`. '
        f'Your job: verify this PR against the spec from the original '
        f'APPROVAL_REQUEST and emit one marker block (PASS / REVISION / '
        f'ESCALATE / EMERGENCY_HALT).',
        '',
        f'Task: `{task_id}`',
        f'PR: {pr_url}',
    ]
    if branch:
        review_prompt_lines.append(f'Branch: `{branch}`')
    if target_repo:
        review_prompt_lines.append(f'Target repo: `{target_repo}`')
    review_prompt_lines.extend([
        '',
        'Follow the Review protocol in your CLAUDE.md: read the spec from '
        'the dispatch context, fetch the PR diff (`gh pr diff <N>`), '
        'optionally `gh pr checkout <N>` and run tests in your worktree '
        'if you need to verify behavior. Emit a single marker block at '
        'the end (REVIEW_PASS / REVIEW_REVISION / REVIEW_ESCALATE / '
        f'REVIEW_EMERGENCY_HALT). You have {max_revisions} revision rounds '
        'budget — set confidence thoughtfully (low-confidence revisions '
        'auto-promote to escalate).',
    ])
    review_prompt = '\n'.join(review_prompt_lines)

    review_base: dict[str, Any] = {
        'task_id': task_id,
        'prompt': review_prompt,
        'source': 'beacon',
        'phase': 'review',
        'max_revisions': max_revisions,
        'dispatched_by': 'outbox-notifier',
    }
    if branch:
        review_base['branch'] = branch
    # D3.5 5a M-2 review fix: propagate the same envelope fields
    # _dispatch_build_phase does. Without these, a future Mirror REVIEW_QUESTION
    # round-trip (5b) loses the PR metadata when answering back to Beacon,
    # and the worktree gate rejects with "no canonical path" — same shape as
    # the 4a marker-error black hole.
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            review_base[f_name] = data[f_name]
    # Record the PR head commit this review covers, so the round-0 dedup (and
    # the heal-undispatched-pr-review backstop) can distinguish "this commit
    # was reviewed" from "an older commit was reviewed". A PR pushed-to after
    # its first review must be re-reviewed, not skipped. Prefer a head the
    # caller already resolved (the healer threads the PR's headRefOid through
    # `data`, so no second gh call); fall back to a direct lookup for the
    # inline build-phase path. Best-effort: a gh hiccup leaves it unset and the
    # dedup falls back to existence-only (the prior behavior).
    review_head_sha: Optional[str] = None
    _cand = data.get('head_sha')
    if isinstance(_cand, str) and _cand:
        review_head_sha = _cand
    else:
        _pr_coords = _parse_pr_url(pr_url)
        if _pr_coords is not None:
            review_head_sha = _gh_pr_head_sha(_pr_coords[0], _pr_coords[1])
    if review_head_sha:
        review_base['head_sha'] = review_head_sha
    # Deep-review-hold suppression (review-dispatch-post-auto-merge-held). If
    # this PR is parked in AUTO_MERGE_HELD_DEEP_REVIEW at this SAME head, a new
    # review would only re-PASS and re-arm the merge gate — the wasteful loop.
    # Skip it. The guard self-heals: a merged/closed PR or an advanced head
    # clears the stale record and lets the review through (see the helper).
    if _deep_review_hold_suppresses_dispatch(pr_url, review_head_sha):
        log(
            f'MIRROR_REVIEW_SUPPRESSED_DEEP_REVIEW_HELD task={task_id} '
            f'pr={pr_url} head={review_head_sha} — PR is held for '
            f'/code-review high at this head; not re-dispatching a review',
            'INFO',
        )
        return
    # Chain context (M1). pr_url/target_repo are the PR under review; first
    # review starts the revision budget at 0. forge_build_session_id threads
    # Forge's build session (D3.5 5b) so a downstream REVIEW_REVISION can
    # --resume her for the revision dispatch instead of starting fresh.
    # replan_count + max_replans ride the build→review hop (5c C-1), else
    # Mirror's REVIEW_ESCALATE outbox carries replan_count=0 and defeats the cap.
    review_task = build_chain_envelope(
        review_base,
        data,
        carry={
            'pr_url': pr_url,
            'target_repo': target_repo,
            'revision_count': 0,
            'forge_build_session_id': data.get('claude_session_id'),
            'reply_chat_id': CARRY,
            'replan_count': CARRY,
            'max_replans': CARRY,
        },
    )

    # D3.5 5c-followup-2 (audit C-2): key the review-task filename by
    # replan_count when this is a replan iteration's first review. Same
    # rationale as the _dispatch_build_phase C-1 sibling — without the
    # round suffix, round-1's archive entry collides and every replan's
    # first Mirror review silently drops. (Re-reviews within a single
    # replan iteration are already keyed by revision_count in
    # _dispatch_mirror_review_rerun; this gate is just for the first
    # review per replan round.)
    replan_count = data.get('replan_count', 0)
    if not isinstance(replan_count, int) or replan_count < 0:
        replan_count = 0
    if replan_count > 0:
        review_filename = f'review-{task_id}-replan{replan_count}.json'
    else:
        review_filename = f'review-{task_id}.json'
    review_filename = safe_write_inbox.canonical_inbox_name(review_filename)
    # Idempotency check (same pattern as _dispatch_build_phase): if the
    # review task is already in Mirror's inbox OR archived OR .invalid, skip.
    # Guards against the notifier crashing between dispatch and archive of the
    # build-phase outbox — re-processing would otherwise spawn a duplicate
    # Mirror review of a PR she's already started reviewing. The .invalid/
    # leg (D3.5 5a M-1 review fix) catches a prior dispatch that was
    # validator-rejected — don't re-dispatch a duplicate that hits the same
    # rejection. Shared with the reconciliation sweep via the helper so the
    # two checks can't drift. Round-0 passes the head so a review of an OLDER
    # head doesn't block re-review of new commits; replan rounds keep the
    # existence check (their filename already carries the round suffix).
    _dedup_head = review_head_sha if replan_count == 0 else None
    if _review_request_already_dispatched(review_filename, _dedup_head):
        log(
            f'review-request already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
        )
        return

    # Merged/closed-PR guard (dispatch-time half). Don't even queue a review
    # for a PR that's already left OPEN — it gates nothing. Cheap early skip
    # for the common case; the execution-time guard in inbox_watcher catches
    # the race where the merge lands AFTER this dispatch. Fail-OPEN: `None`
    # (gh hiccup / unparseable url) proceeds with the dispatch so a transient
    # error never drops a legitimate review. Skipped when the caller already
    # confirmed OPEN this tick (reconcile sweep) to avoid a redundant gh call.
    if not pr_state_known_open and _mirror_review_target_is_terminal(pr_url):
        log(
            f'PR {pr_url} (task {task_id}) is already merged/closed; '
            f'skipping Mirror review dispatch (review gates nothing)'
        )
        return

    # D3.5 5d cost-budget gate. AFTER the idempotency check (second-pass
    # review finding 2-#1) — see _dispatch_build_phase for rationale.
    if not _enforce_cost_budget(task_id, 'mirror-review', data):
        return

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='mirror',
            task_dict=review_task,
            source_agent='beacon',
            filename=review_filename,
        )
        log(
            f'review-request dispatched mirror <- beacon '
            f'(task={task_id}, file={dest.name}, pr={pr_url})'
        )
        _emit_review_request_chain_event(
            task_id, pr_url,
            revision_count=0, replan_count=replan_count,
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'review-request dispatch FAILED for task {task_id}: '
            f'{type(e).__name__}: {e}. Beacon was already notified of '
            f'PR opened; Larry must manually re-dispatch review.',
            'WARN',
        )


def _reconcile_missed_mirror_reviews() -> None:
    """Self-healing sweep: re-dispatch Mirror reviews the inline path dropped.

    fix-notifier-review-dispatch-reliability (Part B). The inline dispatch in
    `process_outbox` fires the Forge->Mirror review-request exactly once, when
    the build-phase outbox is first read. If PR-URL extraction returned None
    (the PR #303 incident — non-canonical phrasing the old regex missed) or
    the dispatch otherwise failed, nothing ever re-examines the outbox and the
    PR stalls unreviewed. This sweep is the defense-in-depth net: it re-scans
    recently-archived Forge build outboxes and idempotently re-dispatches any
    review that's genuinely missing.

    Bounded so it's a cheap no-op in steady state:
      - Only archive files with mtime within RECONCILE_WINDOW_HOURS are read.
      - Candidate filter is all-cheap (JSON parse, agent/phase/target_repo,
        the now-robust extractor) BEFORE any gh shell-out.
      - The idempotency presence check (shared with `_dispatch_mirror_review`)
        runs before the gh open-state check, so a PR whose review already
        exists costs zero gh calls.
      - With zero in-window misses the sweep does zero dispatches and zero
        gh calls.

    Never raises into the caller's expectation of cleanliness, but the
    main_loop hook also wraps this in try/except as a daemon-never-wedge
    backstop.
    """
    archive_dir = OUTBOXES_ROOT / 'forge' / '.archive'
    if not archive_dir.exists():
        return

    cutoff = time.time() - RECONCILE_WINDOW_HOURS * 3600
    for outbox_file in sorted(archive_dir.glob('*.json')):
        if outbox_file.name.startswith('.'):
            continue
        try:
            if outbox_file.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue

        try:
            data = json.loads(outbox_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue

        # Cheap candidate filters first — no gh, no inbox stat until these pass.
        if data.get('agent') != 'forge' or data.get('phase') != 'build':
            continue
        target_repo = data.get('target_repo')
        if not target_repo:
            continue
        pr_url = _extract_pr_url_from_build_result(data.get('result', ''))
        if not pr_url:
            continue

        task_id = data.get('task_id') or 'unknown'
        # Idempotency: reuse the EXACT presence check _dispatch_mirror_review
        # performs. The incident PR's review file now exists (manual recovery),
        # so it's correctly skipped here. We key on the unkeyed review filename
        # — a missed FIRST dispatch is the failure shape this sweep heals;
        # replan re-reviews are dispatched/keyed elsewhere.
        review_filename = safe_write_inbox.canonical_inbox_name(f'review-{task_id}.json')
        if _review_request_already_dispatched(review_filename):
            continue

        # Don't review merged/closed PRs. Only reached for genuine misses
        # (review file absent), so the gh cost is incurred ~never in steady
        # state. Unknown state (None) -> skip this tick; a later sweep retries.
        parsed = _parse_pr_url(pr_url)
        if parsed is None:
            log(
                f'reconcile: candidate task={task_id} has unparseable '
                f'pr_url={pr_url}; skipping',
                'INFO',
            )
            continue
        repo_coords, pr_number = parsed
        is_open = _gh_pr_is_open(repo_coords, pr_number)
        if is_open is None:
            log(
                f'reconcile: could not determine state of PR {pr_url} '
                f'(task={task_id}); leaving for next sweep',
                'INFO',
            )
            continue
        if not is_open:
            log(
                f'reconcile: PR {pr_url} (task={task_id}) is not OPEN '
                f'(merged/closed); skipping review re-dispatch',
                'INFO',
            )
            continue

        # Genuine miss: loud sentinel BEFORE dispatching so the gap is
        # greppable in the notifier log.
        log(
            f'RECONCILE_MISSING_REVIEW task={task_id} pr={pr_url} — notifier '
            f'dropped the build-phase review-request; re-dispatching',
            'WARN',
        )
        # The sweep just confirmed is_open above; tell the dispatcher so its
        # own merged/closed guard doesn't repeat the gh pr view it already did.
        _dispatch_mirror_review(data, pr_url, pr_state_known_open=True)


def _extract_revision_summary_from_result(
    result_text: str,
) -> Optional[tuple[int, str]]:
    """Parse Forge's revision-phase outbox preamble. Returns (round_num, summary) or None.

    D3.5 commit 5b. Forge's CLAUDE.md mandates revision responses START with
    `Revision N applied: <summary>` (case-insensitive, N is integer round
    number). Anchored to start-of-string with leading whitespace tolerance,
    same shape as `_extract_pr_url_from_build_result`. Returns None when
    the prefix is missing OR malformed — the caller treats None as a
    discipline-violation and dead-letters via marker-error cascade (strict
    mode per Larry's 5b signoff).
    """
    if not isinstance(result_text, str) or not result_text:
        return None
    m = _REVISION_APPLIED_RE.search(result_text)
    if not m:
        return None
    try:
        round_num = int(m.group(1))
    except (TypeError, ValueError):
        return None
    summary = m.group(2).strip()
    return round_num, summary


def _render_no_session_revision_dm(
    data: dict[str, Any], decision: dict[str, Any],
) -> str:
    """Compose the Larry-DM body for a no-session REVIEW_REVISION.

    Chain-gap #6: Claude-as-Forge PRs (source='larry', no Forge build
    session) can't auto-resume on revision. The DM surfaces Mirror's
    findings + a clear manual next-step. Phrased for Telegram-on-phone
    reading: terse, leads with the action, body under ~500 chars before
    findings expand.
    """
    task_id = data.get('task_id') or 'unknown'
    payload = decision.get('payload') or {}
    pr_url = data.get('pr_url') or payload.get('pr_url') or '(no pr_url)'
    branch = data.get('branch') or '(branch unknown)'
    summary = payload.get('summary') or payload.get('reason') or ''
    findings = payload.get('findings')

    lines = [
        f'Mirror requested revision on {pr_url} (task `{task_id}`).',
        'Claude-as-Forge PR — no Forge session to auto-resume.',
    ]
    if summary:
        lines.append(f'Summary: {summary}')
    if isinstance(findings, list) and findings:
        lines.append('Findings:')
        for i, f in enumerate(findings, 1):
            if isinstance(f, dict):
                sev = f.get('severity', '?')
                file_ref = f.get('file', '?')
                line_ref = f.get('line_range', '')
                desc = f.get('description', '(no description)')
                loc = f'{file_ref} {line_ref}'.strip()
                lines.append(f'  {i}. [{sev}] {loc} — {desc}')
            else:
                lines.append(f'  {i}. {f}')
    lines.append(
        f'Next step: push the fix to `{branch}`, then ask Claude to '
        f'dispatch a fresh Mirror review (no auto-resume available).'
    )
    return '\n'.join(lines)


def _dm_larry_no_session_revision(
    data: dict[str, Any], decision: dict[str, Any], chat_id: int,
) -> None:
    """Queue a Larry DM for a Claude-as-Forge REVIEW_REVISION with no session.

    Chain-gap #6 (2026-05-20). Same shape as `_maybe_dm_larry` but fires
    from inside the revision-dispatch handler when the auto-resume path
    can't run. Uses `append_notification` (1:1 with a task event, no
    cooldown — losing this DM is exactly the symptom we're fixing).
    """
    task_id = data.get('task_id') or 'unknown'
    pr_url = data.get('pr_url') or (
        (decision.get('payload') or {}).get('pr_url') if isinstance(
            decision.get('payload'), dict
        ) else None
    ) or '(no pr_url)'
    message = _render_no_session_revision_dm(data, decision)
    try:
        ok = larry_alerts.append_notification(
            source='outbox-notifier',
            intent='review-revision',
            message=message,
            chat_id=chat_id,
            task_id=task_id,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'no-session revision DM append raised for chat {chat_id} '
            f'(task={task_id}): {type(e).__name__}: {e}',
            'WARN',
        )
        return
    if ok:
        log(
            f'queued no-session revision DM to chat {chat_id} for PR {pr_url} '
            f'(task={task_id}); Claude-as-Forge — no Forge session to resume'
        )
    else:
        log(
            f'no-session revision DM append failed for chat {chat_id} '
            f'(task={task_id}); Larry will not be notified of the revision',
            'WARN',
        )


def _fetch_pr_body(pr_url: Optional[str]) -> Optional[str]:
    """Return the PR description body via `gh pr view --json body`, or None.

    The cold-start revision brief needs the PR's stated intent, which a
    heal/auto-routed review envelope does not carry (only `pr_title` rides the
    chain). Best-effort: any transport / non-zero exit / parse error, or an
    empty body, returns None and the brief degrades to "read the diff to infer
    intent." Read-only `gh` (mirrors `_gh_pr_head_sha`)."""
    if not pr_url:
        return None
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        return None
    repo_coords, pr_number = parsed
    if _gh_backoff_skip('pr-body-fetch'):
        return None
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number),
             '--repo', repo_coords, '--json', 'body'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f'gh pr view {pr_number} ({repo_coords}) body lookup FAILED: '
            f'{type(e).__name__}: {e}', 'WARN')
        return None
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        log(f'gh pr view {pr_number} ({repo_coords}) body returned '
            f'{proc.returncode}: {(proc.stderr or "").strip()[:200]}', 'WARN')
        return None
    try:
        payload = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return None
    body = payload.get('body')
    return body if isinstance(body, str) and body.strip() else None


def _build_cold_start_revision_prompt(
    *, task_id: str, branch: Optional[str], pr_url: Optional[str],
    next_count: int, max_revisions: int, findings_block: str,
    pr_body: Optional[str],
) -> str:
    """Build the round-1 brief for a Forge that has NO build session for this PR.

    A resumed Forge carries its whole build conversation implicitly; a blind
    Forge carries nothing. So the brief hand-delivers everything that
    conversation would have held — provenance (this is NOT your build, read
    first), the PR's intent (its description), an instruction to read the diff
    + git log, the findings, and the same-branch/no-scope-creep constraints —
    or Forge will "fix" a nit in a way that defeats the PR's purpose. See
    agents/beacon/specs/forge-cold-start-revision.md (S2 / M2)."""
    intent = pr_body.strip() if isinstance(pr_body, str) and pr_body.strip() else (
        '(PR description unavailable — read the diff and `git log` to infer the '
        'PR\'s intent before editing.)'
    )
    lines = [
        f'Revision phase — COLD START (revision {next_count} of {max_revisions}). '
        f'Mirror reviewed this PR and requested changes.',
        '',
        'IMPORTANT: this PR was authored by Claude Code on the laptop — it is '
        'NOT your build. You have no prior session or memory of it. Before '
        'editing you MUST read:',
        '  1. the PR intent below (what this PR is for),',
        '  2. the diff — `gh pr diff <N>` (or your checked-out branch), and',
        '  3. the commit log — `git log` on the branch.',
        'Apply ONLY Mirror\'s findings. Preserve the PR\'s stated intent. Do '
        'not expand scope.',
        '',
        f'Task: `{task_id}`',
    ]
    if branch:
        lines.append(f'Branch: `{branch}`')
    if pr_url:
        lines.append(f'PR: {pr_url}')
    lines.extend([
        '',
        'PR intent (from the PR description):',
        '--- BEGIN PR DESCRIPTION ---',
        intent,
        '--- END PR DESCRIPTION ---',
        '',
        findings_block,
        '',
        'Apply each finding as a targeted edit, commit with a '
        'conventional-commit revision message, push to the SAME branch (no new '
        'PR — it auto-updates), and emit a one-line result starting with '
        f'`Revision {next_count} applied: <summary>` (strict — a missing prefix '
        'dead-letters back to you).',
        '',
        f'The preamble `Revision {next_count} applied:` must be the VERY FIRST '
        'characters of THIS response — no acknowledgement, greeting, or preface '
        'before it. The gate is anchored to the start of your response. If '
        f'`Revision {next_count - 1} applied:` (a prior round) appears earlier in '
        'this conversation, that does NOT satisfy the gate: THIS response must '
        f'still START with `Revision {next_count} applied:`.',
        '',
        'If a finding is a judgment/values call you cannot resolve from the PR '
        'intent (e.g. an ambiguous spec contradiction), do NOT guess: leave '
        'that finding unapplied and say so explicitly in your result summary so '
        'it routes to Beacon/Larry for a decision.',
    ])
    return '\n'.join(lines)


def _dispatch_revision_to_forge(
    data: dict[str, Any], decision: dict[str, Any],
) -> None:
    """Write a revision-task to Forge's inbox after Mirror's REVIEW_REVISION.

    D3.5 commit 5b. Parallel to `_dispatch_build_phase`: same task_id, same
    branch, --resume against Forge's build session. The marker's findings
    serialize into the prompt so Forge has structured input on what to fix.

    Pulls `forge_build_session_id` from Mirror's outbox envelope (threaded
    through 5a's `_dispatch_mirror_review` → propagated via _build_outbox) and
    --resumes that conversation.

    COLD START (forge-cold-start-revision.md, S2/M2): when there is NO
    `forge_build_session_id` — a `claude/` PR auto-routed to Mirror, or a
    heal-rebuilt envelope (the #645 / #653 / PR #412 class) — Forge never built
    this PR, so there is nothing to --resume. Instead of dead-ending, dispatch a
    FRESH Forge run (session_id omitted) carrying a full cold-start brief
    (`_build_cold_start_revision_prompt`: provenance + PR intent + read-the-diff
    + findings) and open a durable obligation in `no_session_ledger`. The one
    exception is an interactive `source='larry'` PR with a live chat, which
    keeps its direct DM (Larry owns and drives that fix).

    Caller responsibility: only invoke when `decision['marker_type'] ==
    'review_revision'`, `not decision['auto_promoted']`, `not
    decision['budget_exhausted']`. The classifier handles the
    auto-promote/budget-exhaust downgrades to escalate routing.

    Failure to write is logged WARN and non-fatal — the notify to Beacon
    above has already informed her of the REVIEW_REVISION; Larry sees the
    gap and can manually re-dispatch.
    """
    task_id = data.get('task_id') or 'unknown'
    forge_session = data.get('forge_build_session_id')
    cold_start = not forge_session
    if cold_start:
        # No Forge build session to --resume. Two sub-cases:
        routing_source = data.get('original_source') or data.get('source')
        chat_id = data.get('reply_chat_id')
        if routing_source == 'larry' and isinstance(chat_id, int):
            # Interactive Claude-as-Forge PR with a live Telegram thread
            # (chain-gap #6, the PR #59 class): Larry is watching and owns this
            # PR, so surface Mirror's findings to him directly — he drives the
            # fix. Unchanged.
            _dm_larry_no_session_revision(data, decision, chat_id)
            return
        # Otherwise — a `claude/` PR auto-routed to Mirror, or a heal-rebuilt
        # envelope (the #645 / #653 / PR #412 class) with no chat target. Forge
        # never built this, so there is no session to --resume. Rather than the
        # old LLM-mediated Beacon route (which silently dead-ended when the
        # Beacon turn no-op'd — see agents/beacon/specs/forge-cold-start-revision.md),
        # fall through and dispatch a FRESH, fully-briefed Forge revision. Each
        # cold-start round re-briefs from scratch (provenance + PR intent +
        # read-the-diff + findings); a blind Forge needs context, not session
        # continuity, so there is no session to thread forward.

    target_repo = data.get('target_repo')
    if not target_repo:
        # M3: derive target_repo from chain_events before dead-ending —
        # symmetric with _dispatch_mirror_review / _dispatch_mirror_review_rerun.
        target_repo = backfill_target_repo(task_id)
        if target_repo:
            log(
                f'target_repo backfilled to `{target_repo}` for task {task_id} '
                f'from chain_events (M3); proceeding with revision dispatch',
                'INFO',
            )
    if not target_repo:
        log(
            f'REVIEW_REVISION on task {task_id} has no target_repo on envelope '
            f'and none derivable from chain_events; Forge worktree gate would '
            f'reject revision dispatch — skipping.',
            'WARN',
        )
        return

    branch = data.get('branch')
    payload = decision.get('payload') or {}
    findings = payload.get('findings') or []

    # Increment revision counter for the dispatch envelope. The cousin in
    # Mirror's envelope is incremented when the re-review dispatches; this
    # one is the count of revision attempts Forge has made so far.
    current_count = data.get('revision_count', 0)
    if not isinstance(current_count, int) or current_count < 0:
        current_count = 0
    next_count = current_count + 1
    max_revisions = data.get('max_revisions', mrh.DEFAULT_MAX_REVISIONS)
    if not isinstance(max_revisions, int) or max_revisions < 0:
        max_revisions = mrh.DEFAULT_MAX_REVISIONS

    # Serialize findings into a human-readable block for the prompt.
    findings_lines = ['Mirror\'s findings on this PR:', '']
    if isinstance(findings, list):
        for i, f in enumerate(findings, 1):
            if not isinstance(f, dict):
                findings_lines.append(f'  {i}. {f}')
                continue
            sev = f.get('severity', '?')
            file_ref = f.get('file', '?')
            line_ref = f.get('line_range', '?')
            desc = f.get('description', '(no description)')
            findings_lines.append(
                f'  {i}. [{sev}] {file_ref} {line_ref} — {desc}'
            )
    else:
        findings_lines.append(f'  (raw findings: {findings})')
    findings_block = '\n'.join(findings_lines)

    pr_url = data.get('pr_url') or payload.get('pr_url')
    if cold_start and not pr_url:
        # M3: the brief + the obligation ledger need the PR; derive it via gh
        # when a heal-rebuilt envelope didn't carry one.
        pr_url = backfill_pr_url(task_id, target_repo=target_repo, branch=branch)

    if cold_start:
        revision_prompt = _build_cold_start_revision_prompt(
            task_id=task_id, branch=branch, pr_url=pr_url,
            next_count=next_count, max_revisions=max_revisions,
            findings_block=findings_block,
            pr_body=data.get('pr_body') or _fetch_pr_body(pr_url),
        )
    else:
        revision_prompt_lines = [
            f'Revision phase. Mirror has reviewed your build on task `{task_id}` '
            f'and requested changes (revision {next_count} of {max_revisions}).',
            '',
            f'Task: `{task_id}`',
        ]
        if branch:
            revision_prompt_lines.append(f'Branch: `{branch}`')
        if pr_url:
            revision_prompt_lines.append(f'PR: {pr_url}')
        revision_prompt_lines.extend([
            '',
            findings_block,
            '',
            'Follow the Revision phase protocol in your CLAUDE.md: apply each '
            'finding as a targeted edit (no scope creep), commit with a '
            'conventional-commit revision message, push to the same branch '
            '(PR auto-updates), and emit a one-line result starting with '
            f'`Revision {next_count} applied: <summary>` (strict per 5b — '
            'missing prefix dead-letters back to you).',
            '',
            'This is a resumed conversation. Do NOT open with a conversational '
            'acknowledgement of the new findings — the preamble '
            f'`Revision {next_count} applied:` must be the VERY FIRST characters '
            'of THIS response, before any other text. The gate is anchored to '
            'the start of THIS response only.',
        ])
        if next_count >= 2:
            revision_prompt_lines.append(
                f'ROUND-{next_count} TRAP: a prior round\'s '
                f'`Revision {next_count - 1} applied:` line already exists '
                'earlier in this session. That earlier preamble does NOT count '
                f'— THIS response must still START with `Revision {next_count} '
                'applied:`.'
            )
        revision_prompt = '\n'.join(revision_prompt_lines)

    revision_base: dict[str, Any] = {
        'task_id': task_id,
        'prompt': revision_prompt,
        'source': 'beacon',          # logical dispatcher (Beacon's spec authorized this)
        'phase': 'revision',
        'max_revisions': max_revisions,
        'dispatched_by': 'outbox-notifier',
        # D3.5 5b M-8 (second-pass review fix): thread Mirror's findings
        # forward so the re-review prompt can include them. Mirror's
        # re-review session is FRESH (no claude --resume); without these
        # findings on the envelope, her re-review prompt has to direct her
        # to reconstruct findings from sources that aren't reliably
        # available (Forge's commit message body, Beacon's journal). On
        # round 2 she'd re-derive different findings, breaking the loop's
        # coherence. Findings round-trip: here → _build_outbox propagates
        # → Forge's revision outbox carries → _dispatch_mirror_review_rerun
        # reads + injects into Mirror's re-review prompt.
        'previous_findings': findings if isinstance(findings, list) else [],
    }
    if not cold_start:
        # --resume Forge's build session. Omitted on a cold start so
        # agent_runner runs Forge fresh (it only threads --resume when
        # session_id is truthy).
        revision_base['session_id'] = forge_session
    if branch:
        revision_base['branch'] = branch
    # Propagate the same envelope fields _dispatch_build_phase does so a
    # future REVIEW_QUESTION (deferred) round-trip would preserve PR
    # metadata. Same shape as 5a M-2 review fix.
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            revision_base[f_name] = data[f_name]
    # Chain context (M1). forge_build_session_id (C-1 5b) is set explicitly on
    # the revision envelope so round-2's _dispatch_revision_to_forge can still
    # resolve the build session — without it the loop stalls silently at round
    # 2. target_repo/pr_url point at the PR; revision_count is this round's
    # number. replan_count + max_replans ride the revision dispatches (5c C-X1)
    # so a task that revises before re-escalating doesn't reset replan_count=0
    # and defeat the cap.
    revision_task = build_chain_envelope(
        revision_base,
        data,
        carry={
            'forge_build_session_id': forge_session,
            'target_repo': target_repo,
            'revision_count': next_count,
            'pr_url': pr_url,
            'reply_chat_id': CARRY,
            'replan_count': CARRY,
            'max_replans': CARRY,
        },
    )

    # Idempotency check: revision-task filename is keyed on round number so
    # multiple revision rounds for the same task_id don't collide. Re-process
    # on notifier crash skips if already in inbox/archive/invalid.
    # D3.5 5c-followup-2 HIGH-1 (combined-state fix): also key by replan_count
    # when this is a replan iteration. Without this, the second replan
    # iteration's revision_count=1 collides with the first iteration's
    # archived revision-{task}-1.json — same shape as C-1/C-2 but on the
    # inner revision loop. Surfaced by the PR #10 independent reviewer.
    replan_count = data.get('replan_count', 0)
    if not isinstance(replan_count, int) or replan_count < 0:
        replan_count = 0
    if replan_count > 0:
        revision_filename = (
            f'revision-{task_id}-replan{replan_count}-{next_count}.json'
        )
    else:
        revision_filename = f'revision-{task_id}-{next_count}.json'
    revision_filename = safe_write_inbox.canonical_inbox_name(revision_filename)
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'

    def _open_cold_start_obligation() -> None:
        # Record/refresh the durable obligation so the backstop (heal Check 6)
        # can verify this session-less revision actually closes (Mirror PASS /
        # merge) instead of trusting that the dispatch fired (S1/M3). Idempotent
        # + fail-safe; called on BOTH a fresh write and the idempotency
        # early-return below — the file already existing means a revision IS in
        # flight, and a prior dispatch may have crashed before opening the
        # obligation, which would otherwise leave the backstop blind to it.
        no_session_ledger.open_obligation(
            task_id,
            pr_url=pr_url or '(no pr_url)',
            branch=branch,
            target_repo=target_repo,
            head_sha=data.get('head_sha'),
            round_num=next_count,
        )

    if (
        (forge_inbox / revision_filename).exists()
        or (forge_inbox / '.archive' / revision_filename).exists()
        or (forge_inbox / '.invalid' / revision_filename).exists()
    ):
        log(
            f'revision-{next_count} already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
        )
        if cold_start:
            _open_cold_start_obligation()
        return

    # D3.5 5d cost-budget gate. AFTER the idempotency check (second-pass
    # review finding 2-#1) — see _dispatch_build_phase for rationale.
    if not _enforce_cost_budget(task_id, 'revision-to-forge', data):
        return

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='forge',
            task_dict=revision_task,
            source_agent='beacon',
            filename=revision_filename,
        )
        resume_note = (
            'fresh (cold start — no Forge session)'
            if cold_start else f'resume={forge_session[:12]}...'
        )
        log(
            f'revision-{next_count} dispatched forge <- beacon '
            f'(task={task_id}, file={dest.name}, {resume_note})'
        )
        if cold_start:
            _open_cold_start_obligation()
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'revision dispatch FAILED for task {task_id} round {next_count}: '
            f'{type(e).__name__}: {e}. '
            + ('Cold-start (no session). ' if cold_start
               else 'Beacon already notified of REVISION. ')
            + 'Larry must manually re-dispatch.',
            'WARN',
        )


# forge-post-open-mergeable-rebase-001 — Layer-2 mechanical guarantee.
# Cap on how many times the notifier will re-dispatch a rebase for one task. A
# clean rebase that comes back still-CONFLICTING means main advanced AGAIN
# between Forge's rebase and our re-check (a real but rare race); a few retries
# absorb it. Past the cap we stop re-dispatching and leave the obligation OPEN so
# the healer surfaces it to Larry — never an unbounded rebase loop.
_REBASE_MAX_ROUNDS = 3


def _dispatch_rebase_to_forge(
    data: dict[str, Any], pr_url: str, *, round_num: int = 1,
) -> bool:
    """Write a `phase=rebase` task to Forge after a PR opens CONFLICTING.

    forge-post-open-mergeable-rebase-001 (Layer 2). Called from the build-phase
    PR-opened path INSTEAD of `_dispatch_mirror_review` when the freshly-opened
    PR's mergeable state is CONFLICTING — main advanced during the build, so the
    PR is doomed and Mirror must not be dispatched onto it (success criterion 3).
    Forge re-runs under `--resume` of its build session (worktree intact), rebases
    the branch onto origin/main, force-pushes with --force-with-lease, and
    re-emits `PR updated:` — which re-enters the notifier's phase=rebase handler,
    re-checks mergeability, and dispatches Mirror once MERGEABLE.

    A durable obligation is opened in `rebase_obligation_ledger` so the healer can
    verify the loop actually closed even if Forge's in-session step never ran or
    the re-emitted `PR updated:` was dropped (success criterion 4).

    Returns True if a rebase dispatch was written (or was already in flight —
    idempotent), False if it could not be dispatched (no target_repo). Failure to
    write is logged WARN and non-fatal; the obligation + healer remain the
    backstop.
    """
    task_id = data.get('task_id') or 'unknown'
    target_repo = data.get('target_repo')
    if not target_repo:
        target_repo = backfill_target_repo(task_id)
        if target_repo:
            log(
                f'target_repo backfilled to `{target_repo}` for task {task_id} '
                f'from chain_events (rebase dispatch)',
                'INFO',
            )
    if not target_repo:
        # Without target_repo, Forge's worktree gate rejects the dispatch. Surface
        # the gap rather than silently dropping — the PR is CONFLICTING and will
        # otherwise strand on the human-visible held_conflict backstop.
        log(
            f'PR {pr_url} on task {task_id} is CONFLICTING but no target_repo on '
            f'envelope and none derivable from chain_events; cannot dispatch '
            f'rebase. Larry must rebase manually.',
            'WARN',
        )
        return False

    branch = data.get('branch')
    forge_session = data.get('claude_session_id')

    # Resolve the PR head this rebase targets, for the obligation row + dedup.
    head_sha: Optional[str] = None
    _cand = data.get('head_sha')
    if isinstance(_cand, str) and _cand:
        head_sha = _cand
    else:
        _pr_coords = _parse_pr_url(pr_url)
        if _pr_coords is not None:
            head_sha = _gh_pr_head_sha(_pr_coords[0], _pr_coords[1])

    rebase_prompt = '\n'.join([
        f'Rebase phase. The PR you just opened for task `{task_id}` is '
        f'CONFLICTING with main — main advanced during your build, so the PR '
        f'cannot merge as-is. Rebase it onto current main BEFORE Mirror reviews '
        f'it (a CONFLICTING PR is never dispatched to Mirror).',
        '',
        f'Task: `{task_id}`',
        f'PR: {pr_url}',
        *([f'Branch: `{branch}`'] if branch else []),
        '',
        'Follow the Rebase phase protocol in your CLAUDE.md:',
        '  1. `git fetch origin` then `git rebase origin/main` in this worktree.',
        '  2. CLEAN rebase (exit 0, no conflict markers) → `git push '
        '--force-with-lease` and emit a result starting with '
        f'`PR updated: {pr_url}` (the notifier re-checks mergeability and '
        'dispatches Mirror once it is MERGEABLE).',
        '  3. CONFLICTED rebase → `git rebase --abort` (never leave a '
        'half-rebased worktree or push a broken branch), then end with a '
        'plain BLOCKER PARAGRAPH (NOT a marker) naming the conflicting files '
        'and the upstream change that moved main. The notifier routes that '
        'blocker to Beacon, who decides fresh-rebased-build vs sequencing vs '
        'escalation.',
    ])

    rebase_base: dict[str, Any] = {
        'task_id': task_id,
        'prompt': rebase_prompt,
        'source': 'beacon',
        'phase': 'rebase',
        'dispatched_by': 'outbox-notifier',
    }
    if forge_session:
        # --resume the build session so Forge's worktree + build context are
        # intact. Omitted only if somehow absent (the worktree is keyed on
        # task_id either way, so a fresh run still lands in the right tree).
        rebase_base['session_id'] = forge_session
    if branch:
        rebase_base['branch'] = branch
    if head_sha:
        rebase_base['head_sha'] = head_sha
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            rebase_base[f_name] = data[f_name]

    rebase_task = build_chain_envelope(
        rebase_base,
        data,
        carry={
            'forge_build_session_id': data.get('claude_session_id'),
            'target_repo': target_repo,
            'pr_url': pr_url,
            'reply_chat_id': CARRY,
            'replan_count': CARRY,
            'max_replans': CARRY,
            'revision_count': CARRY,
        },
    )

    rebase_filename = safe_write_inbox.canonical_inbox_name(
        f'rebase-{task_id}-{round_num}.json'
    )
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'

    def _open_obligation() -> None:
        # Record/refresh the durable obligation BEFORE concluding the dispatch is
        # in flight — the healer must see it even if a prior dispatch crashed
        # after writing the inbox file but before opening the obligation.
        rebase_obligation_ledger.open_obligation(
            task_id,
            pr_url=pr_url or '(no pr_url)',
            branch=branch,
            target_repo=target_repo,
            head_sha=head_sha,
            round_num=round_num,
        )

    if (
        (forge_inbox / rebase_filename).exists()
        or (forge_inbox / '.archive' / rebase_filename).exists()
        or (forge_inbox / '.invalid' / rebase_filename).exists()
    ):
        log(
            f'rebase round {round_num} already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
        )
        _open_obligation()
        return True

    if not _enforce_cost_budget(task_id, 'rebase-to-forge', data):
        return False

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='forge',
            task_dict=rebase_task,
            source_agent='beacon',
            filename=rebase_filename,
        )
        resume_note = (
            f'resume={forge_session[:12]}...' if forge_session else 'fresh'
        )
        log(
            f'rebase round {round_num} dispatched forge <- beacon '
            f'(task={task_id}, file={dest.name}, pr={pr_url}, {resume_note})'
        )
        _open_obligation()
        return True
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'rebase dispatch FAILED for task {task_id} round {round_num}: '
            f'{type(e).__name__}: {e}. PR {pr_url} is CONFLICTING and will '
            f'strand on the held_conflict backstop; Larry must rebase manually.',
            'WARN',
        )
        return False


def _handle_pr_mergeable_before_review(
    data: dict[str, Any], pr_url: str, *, is_rebase_phase: bool = False,
) -> bool:
    """Gate Mirror dispatch on the PR's mergeable state (Layer 2).

    forge-post-open-mergeable-rebase-001. Polls the PR's mergeable state past
    GitHub's async UNKNOWN. Returns True when the caller should proceed to
    dispatch Mirror (MERGEABLE, or UNKNOWN-after-poll treated optimistically),
    False when it should NOT (CONFLICTING — a rebase was dispatched instead, or
    the rebase round cap was hit and the obligation was left open for the healer).

    `is_rebase_phase=False` is the build-phase PR-opened call: a CONFLICTING PR
    opens a fresh round-1 rebase obligation. `is_rebase_phase=True` is the
    re-check after Forge's rebase re-emits `PR updated:`: a now-MERGEABLE PR
    RESOLVES the obligation (and proceeds to Mirror); a still-CONFLICTING PR means
    main re-advanced — re-dispatch a higher rebase round up to `_REBASE_MAX_ROUNDS`,
    after which we stop and leave the obligation open (healer surfaces it)."""
    task_id = data.get('task_id') or 'unknown'
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        # Can't check mergeability without coords; don't block the cascade —
        # proceed to Mirror (prior behavior). The auto-merge gate still re-checks.
        log(
            f'mergeable-gate: unparseable pr_url={pr_url} (task={task_id}); '
            f'proceeding to Mirror without rebase check',
            'INFO',
        )
        return True
    repo_coords, pr_number = parsed
    status = _poll_pr_mergeable(repo_coords, pr_number)

    if status == 'conflicting':
        # Determine the round. A fresh build-phase conflict starts at round 1; a
        # rebase-phase conflict (main re-advanced) bumps the existing round.
        existing = rebase_obligation_ledger.get_obligation(task_id)
        prev_round = existing.get('round', 0) if isinstance(existing, dict) else 0
        if not isinstance(prev_round, int) or prev_round < 0:
            prev_round = 0
        next_round = prev_round + 1
        if next_round > _REBASE_MAX_ROUNDS:
            log(
                f'mergeable-gate: PR {pr_url} (task={task_id}) STILL CONFLICTING '
                f'after {prev_round} rebase round(s) — main keeps advancing. '
                f'Hit cap {_REBASE_MAX_ROUNDS}; leaving obligation OPEN for the '
                f'healer + held_conflict backstop. NOT dispatching Mirror.',
                'WARN',
            )
            # Keep the obligation open + fresh so the healer's grace window is
            # measured from now (we did act this tick).
            rebase_obligation_ledger.open_obligation(
                task_id,
                pr_url=pr_url or '(no pr_url)',
                branch=data.get('branch'),
                target_repo=data.get('target_repo'),
                head_sha=data.get('head_sha'),
                round_num=prev_round,
            )
            return False
        log(
            f'mergeable-gate: PR {pr_url} (task={task_id}) is CONFLICTING — '
            f'dispatching rebase round {next_round} to Forge; NOT dispatching '
            f'Mirror onto a doomed PR',
            'WARN',
        )
        _dispatch_rebase_to_forge(data, pr_url, round_num=next_round)
        return False

    # MERGEABLE (or UNKNOWN-after-poll). If this is the rebase-phase re-check, a
    # mergeable PR means the rebase landed — resolve the obligation. (A still-
    # UNKNOWN result also clears: GitHub couldn't confirm a conflict, so we let
    # Mirror + the auto-merge gate carry it rather than re-rebasing on a guess.)
    if is_rebase_phase:
        resolved = rebase_obligation_ledger.resolve_obligation(
            task_id,
            resolution='mergeable' if status == 'mergeable' else 'mergeable-unknown',
        )
        if resolved:
            log(
                f'mergeable-gate: PR {pr_url} (task={task_id}) is {status} after '
                f'rebase — resolved obligation, dispatching Mirror',
                'INFO',
            )
    return True


# ---- mirror-review-visibility: classify + route session-less review outcomes ----
# Spec: agents/beacon/specs/mirror-review-visibility.md (Contracts B+C+D).
# Classify on WIRE signals only (marker_type + session/ledger state), never on
# finding prose — the routing site cannot read findings. Route the human-needed
# buckets to Larry's surfaces; self-healing stays silent. One artifact per
# escalation, idempotent on PR + head SHA.

NO_SESSION_SELF_HEALING = 'self_healing'
NO_SESSION_ACTION_NEEDED = 'action_needed'
NO_SESSION_DECISION_NEEDED = 'decision_needed'


def _no_session_record_id(task_id: Optional[str]) -> str:
    """Stable for-Larry record id for a session-less PR (one active record per
    task; cleared when the trigger clears)."""
    return f'mirror-review:{task_id or "unknown"}'


def _no_session_dedup_identity(
    data: dict[str, Any], marker_decision: dict[str, Any],
) -> str:
    """PR + head SHA — the Contract D idempotency key. Falls back to task_id
    when no head SHA is on the envelope (still stable across notifier
    reprocesses of the same outbox)."""
    task_id = data.get('task_id') or 'unknown'
    payload = marker_decision.get('payload') or {}
    pr = data.get('pr_url') or payload.get('pr_url') or task_id
    head = data.get('head_sha') or ''
    return f'{pr}@{head}'


def _classify_no_session_review(
    data: dict[str, Any], marker_decision: dict[str, Any],
) -> Optional[str]:
    """Bucket a session-less Mirror review outcome (spec §5 Contract B), or None
    when out of scope. Signals only: marker_type + forge_build_session_id
    presence + whether the mechanical re-dispatch can proceed (target_repo
    derivable) + routing source. No finding-prose inspection."""
    mtype = marker_decision.get('marker_type')
    # Only the session-less review path is in scope. A live build session means
    # the normal in-loop revision cascade handles it (findings go to Forge).
    if data.get('forge_build_session_id'):
        return None

    # Decision-needed: Mirror's explicit "a human must decide" verdict, or a
    # revision the loop can't auto-fix (low-confidence auto-promote / budget
    # exhausted) — both reduce to approve-the-fix vs reject.
    if mtype == 'review_escalate':
        return NO_SESSION_DECISION_NEEDED
    if mtype == 'review_revision' and (
        marker_decision.get('auto_promoted')
        or marker_decision.get('budget_exhausted')
    ):
        return NO_SESSION_DECISION_NEEDED

    if mtype != 'review_revision':
        return None

    # A no-session REVIEW_REVISION on a Larry-owned interactive PR is out of
    # scope: _dispatch_revision_to_forge DMs Larry directly and he drives it.
    routing_source = data.get('original_source') or data.get('source')
    chat_id = data.get('reply_chat_id')
    if routing_source == 'larry' and isinstance(chat_id, int):
        return None

    # Mechanical recovery proceeds only if there's a chain envelope to
    # re-dispatch onto — i.e. a derivable target_repo. An off-chain PR with no
    # target_repo (the #653 class) cannot self-heal → action-needed.
    task_id = data.get('task_id') or 'unknown'
    target_repo = data.get('target_repo') or backfill_target_repo(task_id)
    if not target_repo:
        return NO_SESSION_ACTION_NEEDED
    return NO_SESSION_SELF_HEALING


def _emit_no_session_decision_approval(
    data: dict[str, Any], marker_decision: dict[str, Any],
) -> None:
    """Decision bucket (spec §6): a BINARY approval_request on the Approvals tab
    (never a plain larry_alert — that strands the decision, the 2026-06-03
    deploy-notifier incident). add_pending is the durable store; the chain_event
    is the tab feed (best-effort). Idempotent on PR + head SHA."""
    task_id = data.get('task_id') or 'unknown'
    head = (data.get('head_sha') or '')[:8]
    approval_task_id = f'mirror-review-{task_id}' + (f'-{head}' if head else '')
    # Contract D: one approval per PR+head SHA. A re-review of the same head
    # finds the existing row (pending or history) and skips.
    if approval.find_by_id_any_state(approval_task_id) is not None:
        return
    payload_in = marker_decision.get('payload') or {}
    pr_url = data.get('pr_url') or payload_in.get('pr_url')
    reason = (marker_decision.get('intent_kwargs') or {}).get('reason', '') or (
        'Mirror flagged this session-less PR for a human decision.'
    )
    summary = (
        f'Session-less PR `{task_id}` needs your decision. {reason}\n\n'
        'Approve = accept Mirror\'s verdict and dispatch a fresh Forge revision '
        'to fix it. Reject = stand down (close/abandon the PR).'
        + (f'\nPR: {pr_url}' if pr_url else '')
    )
    payload = {
        'task_id': approval_task_id,
        'summary': summary,
        'target_agent': 'forge',
        'prompt': summary,
    }
    # Null-chat fallback (2026-07-02 PR #805 incident): a session-less
    # escalation whose envelope carries no valid reply_chat_id was stored with
    # chat_id=None, so the DM never reached Larry — only the tab fired. Fall
    # back to the primary chat (mirrors the post-merge-finish-step emit ~L7215).
    # If _primary_chat_id() is also None (env unset), keep None: durable
    # tab-only, preserving today's behavior rather than crashing.
    reply_chat = data.get('reply_chat_id')
    chat_id = reply_chat if isinstance(reply_chat, int) else _primary_chat_id()
    try:
        approval.add_pending(payload, chat_id=chat_id)
    except Exception as e:  # noqa: BLE001 — best-effort durable store
        log(
            f'no-session decision add_pending failed (task={task_id}): '
            f'{type(e).__name__}: {e}',
            'WARN',
        )
    try:
        kwargs = approval.build_approval_request_chain_event(payload)
        chain_event_emit.emit_event(**kwargs)
    except Exception as e:  # noqa: BLE001 — best-effort tab feed (Supabase)
        log(
            f'no-session decision emit_event failed (task={task_id}): '
            f'{type(e).__name__}: {e}',
            'WARN',
        )
    log(
        f'no-session decision-needed → approval_request emitted '
        f'(task={task_id}, approval={approval_task_id})'
    )


def _reconcile_no_session_decision_on_merge(task_id: str) -> None:
    """Resolve any still-pending session-less decision approval for a PR that
    just merged (2026-07-02 PR #805 incident).

    A session-less Mirror escalation emits a `mirror-review-<task_id>[-<head8>]`
    decision approval (see `_emit_no_session_decision_approval`). If the PR is
    later merged by ANY path (a second Mirror PASS auto-merge, or a manual
    merge), that first approval was never resolved and ghosted on the Approvals
    tab / doorbell forever. On the auto-merge success path we resolve any
    matching pending entry to 'expired' — NOT approved/rejected: Larry never
    acted, the merge made the decision moot.

    Best-effort + idempotent: no matching pending entry ⇒ silent no-op; a second
    merge event finds nothing. NEVER raises — must not abort the merge path.
    """
    if not task_id or task_id == 'unknown':
        return
    # Word-boundary match, NOT an unbounded prefix: task_id is PR-number-suffixed
    # (e.g. pr-ourliberty-agent-core-42), so a bare startswith would let PR #42's
    # merge expire PR #421's still-pending approval ('421' startswith '42') —
    # silently erasing a legitimate decision, the exact ghosting this fix
    # prevents. Approval ids are `mirror-review-{task_id}[-{head8}]`: the head8
    # form always carries the '-' delimiter, the bare form is an exact match.
    prefix = f'mirror-review-{task_id}'
    try:
        pending = approval.load_state().get('pending', [])
        stale_ids = [
            e.get('id') for e in pending
            if isinstance(e.get('id'), str)
            and (e['id'] == prefix or e['id'].startswith(prefix + '-'))
        ]
        for approval_id in stale_ids:
            resolved = approval.resolve(
                approval_id, 'expired', note='PR merged; decision moot',
            )
            if resolved is not None:
                log(
                    f'no-session decision reconciled on merge → expired '
                    f'(task={task_id}, approval={approval_id})'
                )
    except Exception as e:  # noqa: BLE001 — reconcile must never abort the merge
        log(
            f'no-session decision merge-reconcile failed (task={task_id}): '
            f'{type(e).__name__}: {e}',
            'WARN',
        )


def _route_no_session_review(
    data: dict[str, Any], marker_decision: dict[str, Any],
) -> Optional[str]:
    """Route a classified session-less review outcome to Larry's surfaces
    (spec §6/§7 Contracts C+D). Exactly one artifact per escalation; self-healing
    emits nothing (and clears any stale for-Larry record — a fresh dispatch was
    observed, decision d). Returns the bucket chosen, or None when out of scope.
    Never raises — routing must not abort the chain."""
    try:
        bucket = _classify_no_session_review(data, marker_decision)
    except Exception as e:  # noqa: BLE001 — classification must never crash the chain
        log(
            f'no-session review classify failed: {type(e).__name__}: {e}',
            'WARN',
        )
        return None
    if bucket is None:
        return None
    task_id = data.get('task_id') or 'unknown'
    record_id = _no_session_record_id(task_id)

    if bucket == NO_SESSION_SELF_HEALING:
        # Mechanical re-dispatch fired + obligation open: zero Larry artifacts.
        # A prior action-needed record (if any) is now stale → clear it.
        for_larry_escalations.clear(record_id)
        return bucket

    if bucket == NO_SESSION_ACTION_NEEDED:
        payload_in = marker_decision.get('payload') or {}
        pr_url = data.get('pr_url') or payload_in.get('pr_url')
        written = for_larry_escalations.upsert(
            record_id,
            source='mirror-review',
            headline=f'Session-less PR needs you: `{task_id}`',
            context=(
                'Mirror wants changes but the auto-fix loop cannot proceed '
                '(no chain session to re-dispatch). Go unstick it: re-dispatch '
                f'a Forge build for `{task_id}` or close the PR.'
                + (f' PR: {pr_url}' if pr_url else '')
            ),
            severity='warning',
            pr_url=pr_url,
            head_sha=data.get('head_sha'),
            dedup_identity=_no_session_dedup_identity(data, marker_decision),
        )
        if written is not None:
            log(
                f'no-session action-needed → for-Larry record written '
                f'(task={task_id})'
            )
        return bucket

    # decision_needed
    _emit_no_session_decision_approval(data, marker_decision)
    return bucket


def _dispatch_mirror_review_rerun(
    data: dict[str, Any], round_num: int, summary: str,
) -> None:
    """Write a fresh review-request to Mirror after Forge's revision lands.

    D3.5 commit 5b. Parallel to `_dispatch_mirror_review` but for re-review
    rounds: Mirror's previous session is closed, so she starts fresh on the
    now-updated PR. The envelope carries `revision_count` (so her CLAUDE.md
    knows this is round N, not round 0).

    Triggered from `process_outbox` when Forge's `phase=revision` outbox
    arrives WITH a valid `Revision N applied:` preamble. Missing preamble
    is handled separately (strict gate → marker-error dead-letter).

    `forge_build_session_id` propagates forward unchanged — the next
    REVIEW_REVISION (if any) can dispatch another revision task to the
    same session.
    """
    task_id = data.get('task_id') or 'unknown'
    target_repo = data.get('target_repo')
    if not target_repo:
        # M3: derive target_repo from the task's chain_events before dead-ending.
        target_repo = backfill_target_repo(task_id)
        if target_repo:
            log(
                f'target_repo backfilled to `{target_repo}` for task {task_id} '
                f'from chain_events (M3); proceeding with re-review',
                'INFO',
            )
    if not target_repo:
        log(
            f'Forge revision-{round_num} on task {task_id} has no target_repo '
            f'and none derivable from chain_events; '
            f'cannot dispatch re-review — skipping.',
            'WARN',
        )
        return

    branch = data.get('branch')
    pr_url = data.get('pr_url')
    if not pr_url:
        # M3: derive the PR URL via gh (head branch = forge/<task_id>) before
        # the dead-end check below. The repo just resolved scopes the gh query.
        pr_url = backfill_pr_url(task_id, target_repo=target_repo, branch=branch)
        if pr_url:
            log(
                f'pr_url backfilled to `{pr_url}` for task {task_id} via gh '
                f'(M3); proceeding with re-review',
                'INFO',
            )
    max_revisions = data.get('max_revisions', mrh.DEFAULT_MAX_REVISIONS)
    if not isinstance(max_revisions, int) or max_revisions < 0:
        max_revisions = mrh.DEFAULT_MAX_REVISIONS
    # D3.5 5b second-pass M-8 fix: pull previous_findings from envelope
    # (threaded through _dispatch_revision_to_forge → _build_outbox →
    # Forge's revision outbox → here). Mirror's re-review session is fresh
    # — without these findings her CLAUDE.md tells her to find them in
    # "the PR's commit history or Beacon's journal," neither of which is
    # reliably accessible. Inject them directly into the prompt instead.
    previous_findings = data.get('previous_findings')
    if not isinstance(previous_findings, list):
        previous_findings = []

    review_prompt_lines = [
        f'Re-review phase. Forge has applied revision {round_num} on task '
        f'`{task_id}` per your earlier REVIEW_REVISION findings.',
        '',
        f"Forge's revision summary: {summary}",
        '',
        f'Task: `{task_id}`',
    ]
    if pr_url:
        review_prompt_lines.append(f'PR: {pr_url}')
    if branch:
        review_prompt_lines.append(f'Branch: `{branch}`')
    if previous_findings:
        review_prompt_lines.extend(['', 'Your findings from the previous round:'])
        for i, f in enumerate(previous_findings, 1):
            if not isinstance(f, dict):
                review_prompt_lines.append(f'  {i}. {f}')
                continue
            sev = f.get('severity', '?')
            file_ref = f.get('file', '?')
            line_ref = f.get('line_range', '?')
            desc = f.get('description', '(no description)')
            review_prompt_lines.append(
                f'  {i}. [{sev}] {file_ref} {line_ref} — {desc}'
            )
    review_prompt_lines.extend([
        '',
        f'You are on revision round {round_num} of {max_revisions}. Read '
        'the updated PR diff (`gh pr diff <N>` — same PR, now with Forge\'s '
        'revision commit on top). Verify each finding above is resolved '
        'in the new diff AND no new issues were introduced. Emit one marker: '
        'REVIEW_PASS (if findings resolved cleanly), REVIEW_REVISION (if '
        'more changes needed; budget is round-aware — next would be round '
        f'{round_num + 1}), REVIEW_ESCALATE (if revision approach is wrong), '
        'or REVIEW_EMERGENCY_HALT (safety issue).',
    ])
    review_prompt = '\n'.join(review_prompt_lines)

    # D3.5 5b second-pass m-9 fix: don't substitute the literal string
    # '(unknown)' when pr_url is missing — it propagates as a marker value
    # into Forge's next revision prompt (`PR: (unknown)`). If pr_url is
    # missing, log + skip the dispatch; same shape as the target_repo gate.
    if not pr_url:
        log(
            f'Forge revision-{round_num} on task {task_id} has no pr_url '
            f'on envelope and none derivable via gh; cannot dispatch '
            f're-review — skipping. Larry should manually re-dispatch.',
            'WARN',
        )
        return

    # Deep-review-hold suppression (review-dispatch-post-auto-merge-held) — the
    # re-review sibling of the guard in `_dispatch_mirror_review`. If this PR is
    # parked in AUTO_MERGE_HELD_DEEP_REVIEW at this SAME head, a re-review only
    # re-PASSes and re-arms the merge gate. Unlike the first-review path, this
    # fn doesn't already carry the head — prefer one on the envelope, else
    # resolve it via gh (best-effort; None simply won't suppress, fail-OPEN).
    _rerun_head = data.get('head_sha')
    if not (isinstance(_rerun_head, str) and _rerun_head):
        _rerun_coords = _parse_pr_url(pr_url)
        _rerun_head = (
            _gh_pr_head_sha(_rerun_coords[0], _rerun_coords[1])
            if _rerun_coords is not None else None
        )
    if _deep_review_hold_suppresses_dispatch(pr_url, _rerun_head):
        log(
            f'MIRROR_REVIEW_SUPPRESSED_DEEP_REVIEW_HELD task={task_id} '
            f'pr={pr_url} head={_rerun_head} round={round_num} — PR is held '
            f'for /code-review high at this head; not re-dispatching a review',
            'INFO',
        )
        return

    review_base: dict[str, Any] = {
        'task_id': task_id,
        'prompt': review_prompt,
        'source': 'beacon',
        'phase': 'review',
        'max_revisions': max_revisions,
        'dispatched_by': 'outbox-notifier',
    }
    if branch:
        review_base['branch'] = branch
    # M-8 second-pass fix: also propagate previous_findings forward so the
    # NEXT round's REVIEW_REVISION (if any) carries findings through.
    if isinstance(data.get('previous_findings'), list):
        review_base['previous_findings'] = data['previous_findings']
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            review_base[f_name] = data[f_name]
    # forge_build_session_id propagates forward unchanged (CARRY) so the NEXT
    # revision (if Mirror flags more findings) can resume Forge's session.
    # D3.5 5c C-X1: replan_count + max_replans also propagate through the
    # re-review dispatch (CARRY), closing the second seam in the revision-loop
    # replan-budget chain (partner fix in _dispatch_revision_to_forge above).
    review_task = build_chain_envelope(
        review_base, data,
        carry={
            'pr_url': pr_url,
            'target_repo': target_repo,
            'revision_count': round_num,
            'forge_build_session_id': CARRY,
            'reply_chat_id': CARRY,
            'replan_count': CARRY,
            'max_replans': CARRY,
        },
    )

    # Idempotency: keyed on round number so re-process on notifier crash
    # doesn't double-dispatch a re-review.
    # D3.5 5c-followup-2 HIGH-1 (combined-state fix): also key by replan_count
    # when this is a replan iteration. The bare `rev{N}` filename collides
    # across replan iterations because `revision_count` resets to 0 on each
    # replan's first review dispatch (see _dispatch_mirror_review's
    # hardcoded `revision_count: 0`). Without replan_count keying, the
    # second replan's first revision re-review file collides with the first
    # replan's archive — same shape as C-1/C-2.
    replan_count = data.get('replan_count', 0)
    if not isinstance(replan_count, int) or replan_count < 0:
        replan_count = 0
    if replan_count > 0:
        review_filename = (
            f'review-{task_id}-replan{replan_count}-rev{round_num}.json'
        )
    else:
        review_filename = f'review-{task_id}-rev{round_num}.json'
    review_filename = safe_write_inbox.canonical_inbox_name(review_filename)
    mirror_inbox = safe_write_inbox.INBOXES_ROOT / 'mirror'
    if (
        (mirror_inbox / review_filename).exists()
        or (mirror_inbox / '.archive' / review_filename).exists()
        or (mirror_inbox / '.invalid' / review_filename).exists()
    ):
        log(
            f're-review for revision {round_num} already dispatched for '
            f'task {task_id}; skipping duplicate write'
        )
        return

    # Merged/closed-PR guard (dispatch-time half) — same rationale as the
    # first-review path. A re-review of a PR that already merged/closed gates
    # nothing. Fail-OPEN on an undeterminable state.
    if _mirror_review_target_is_terminal(pr_url):
        log(
            f'PR {pr_url} (task {task_id}, revision {round_num}) is already '
            f'merged/closed; skipping Mirror re-review dispatch'
        )
        return

    # D3.5 5d cost-budget gate. AFTER the idempotency check (second-pass
    # review finding 2-#1) — see _dispatch_build_phase for rationale.
    if not _enforce_cost_budget(task_id, 'mirror-review-rerun', data):
        return

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='mirror',
            task_dict=review_task,
            source_agent='beacon',
            filename=review_filename,
        )
        log(
            f're-review dispatched mirror <- beacon (task={task_id}, '
            f'round={round_num}, file={dest.name})'
        )
        _emit_review_request_chain_event(
            task_id, pr_url,
            revision_count=round_num, replan_count=replan_count,
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f're-review dispatch FAILED for task {task_id} round {round_num}: '
            f'{type(e).__name__}: {e}. Forge already wrote her revision; '
            f'Larry must manually re-dispatch.',
            'WARN',
        )


def _pr_url_shape_check(
    pr_url: Any,
) -> tuple[Optional[str], Optional[int], str]:
    """Layer 1 of the AUTO_MERGE pr_url validator — shape + allowlist check.

    Returns `(repo_coords, pr_number, reason)`. On a valid, allowlisted
    URL: `('Larry-Yatch/<repo>', <int>, 'ok')`. On rejection:
    `(None, None, '<short diagnostic>')`, where the diagnostic is one of:
      - `empty-or-non-string` — pr_url is not a non-empty str.
      - `shape-mismatch` — doesn't match the canonical
        `https://github.com/Larry-Yatch/<slug>/pull/<N>` form (wrong
        owner/scheme, `pull/0`, trailing junk after the PR number).
      - `repo-not-allowlisted` — shape is valid but the repo slug is not
        in `routing_validator.allowed_repos_for('forge')` (the
        `config/agent-models.json` `allowed_repos` that gates dispatch).

    No shell-out; no network. The whole point of Layer 1 is to fail fast
    before any shell-out to `gh pr view` or `gh pr merge`. The allowlist
    is sourced from config (NOT a second hardcoded set) so it tracks the
    same source of truth as the dispatch gate — forge is the PR-opener
    and mirror's list is identical by construction, so checking forge's
    list covers both AUTO_MERGE and MIRROR_REVIEW_STATUS callers.
    """
    if not isinstance(pr_url, str) or not pr_url:
        return None, None, 'empty-or-non-string'
    m = _PR_URL_STRUCTURAL_RE.match(pr_url)
    if not m:
        return None, None, 'shape-mismatch'
    repo = m.group(1)
    if repo not in routing_validator.allowed_repos_for('forge'):
        return None, None, 'repo-not-allowlisted'
    return f'Larry-Yatch/{repo}', int(m.group(2)), 'ok'


def _pr_url_existence_state(
    repo_coords: str, pr_number: int,
) -> tuple[Optional[str], str]:
    """Layer 2 of the AUTO_MERGE pr_url validator — gh-backed existence check.

    Returns `(state, reason)`. On success: `('OPEN'|'CLOSED'|'MERGED', 'ok')`.
    On failure: `(None, '<short diagnostic>')` — covers 404 (PR doesn't
    exist), timeout, gh-cli missing, parse error.

    Treats timeout the same as not-found by collapsing both into a `None`
    state at the call site. The discipline is "don't shell out to
    `gh pr merge` unless we already know the PR exists" — a degraded gh
    that can't answer in `_PR_URL_EXISTENCE_TIMEOUT_S` is structurally
    equivalent to not-found for safety purposes (the next poll cycle
    retries naturally if the outbox hasn't archived; it has, in our
    case — but a real PR's REVIEW_PASS marker doesn't re-arrive
    spontaneously, so the cost of a transient gh outage skipping a real
    merge is bounded to "Larry runs `gh pr merge` manually" which is the
    same cost as the existing `_AUTO_MERGE_FN_OVERRIDE`-less failure path).
    """
    if _gh_backoff_skip('pr-url-existence'):
        return None, 'gh rate-limit backoff active'
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number),
             '--repo', repo_coords, '--json', 'state'],
            capture_output=True, text=True,
            timeout=_PR_URL_EXISTENCE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, f'timeout after {_PR_URL_EXISTENCE_TIMEOUT_S}s'
    except (FileNotFoundError, OSError) as e:
        return None, f'{type(e).__name__}: {e}'
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        stderr = (proc.stderr or '').strip().replace('\n', ' ')[:200]
        return None, f'gh exit={proc.returncode}: {stderr or "no stderr"}'
    try:
        data = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return None, 'parse-error'
    state = data.get('state')
    if not isinstance(state, str):
        return None, 'no-state-field'
    return state, 'ok'


def _parse_pr_url(pr_url: str) -> Optional[tuple[str, int]]:
    """Return (repo_coords, pr_number) from a github.com PR URL, or None.

    D3.5 commit 5d. Tolerant of trailing slashes, query strings, fragments;
    anything after the PR digits is discarded. `repo_coords` is the
    `owner/repo` form `gh pr merge --repo` expects.

    Returns None when the URL doesn't match the github PR shape — the
    caller treats None as a render-only failure (DM body will say "failed:
    malformed pr_url") and skips the gh shell-out.
    """
    if not isinstance(pr_url, str) or not pr_url:
        return None
    m = _GH_PR_URL_RE.search(pr_url)
    if not m:
        return None
    repo_coords = m.group(1)
    try:
        pr_number = int(m.group(2))
    except (TypeError, ValueError):
        return None
    if pr_number <= 0:
        return None
    return repo_coords, pr_number


def _gh_pr_state(repo_coords: str, pr_number: int) -> Optional[str]:
    """Return the PR's `state` field via `gh pr view --json state`, or None.

    D3.5 commit 5d. Used by `_auto_merge_pr` on non-zero exit from
    `gh pr merge` to distinguish *already-merged* (resume-after-crash
    success path) from *real failure* (conflict, branch-protection,
    auth-expired, network). Returns None on transport error — the caller
    treats that as "couldn't disambiguate; report the original failure."

    State values per GitHub API: 'OPEN', 'CLOSED', 'MERGED'.
    """
    if _gh_backoff_skip('pr-state-recheck'):
        return None
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number),
             '--repo', repo_coords,
             '--json', 'state'],
            capture_output=True,
            text=True,
            timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'gh pr view {pr_number} ({repo_coords}) FAILED during merge-state '
            f'recheck: {type(e).__name__}: {e}',
            'WARN',
        )
        return None
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        log(
            f'gh pr view {pr_number} ({repo_coords}) returned {proc.returncode} '
            f'during merge-state recheck: {(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return None
    try:
        data = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return None
    state = data.get('state')
    if isinstance(state, str):
        return state
    return None


def _gh_terminal_pr_state_for_branch(
    repo_coords: str, branch: str,
) -> Optional[str]:
    """Collapse every PR opened from `branch` to one terminal-aware signal.

    phantom-build-phase terminal guard (cap-phantom-build-phase-after-marker-
    error-retry-pr-4d78). Looks up all PRs whose head is `branch`
    (`gh pr list --head <branch> --state all`) and reduces them to:

      'OPEN'   — at least one PR is still open. OPEN wins over any terminal
                 sibling so a reopened/replanned branch is NEVER mis-skipped;
                 this is the legitimate replan/revision re-dispatch case.
      'MERGED' — no open PR, but one already merged (the phantom signature).
      'CLOSED' — no open/merged PR, but one closed-without-merge (terminal too).
      None     — no PR for the branch OR any gh transport/exit/parse failure.
                 The dispatch guard treats both identically: fail open + proceed.

    Never raises — every failure path returns None so the caller fails open.
    Reuses the `gh` subprocess + `_AUTO_MERGE_TIMEOUT_S` convention of the
    sibling `_gh_pr_state` / `_gh_open_prs_for_repo` helpers. State enum per
    GitHub: 'OPEN' | 'CLOSED' | 'MERGED'.
    """
    if _gh_backoff_skip('terminal-state-for-branch'):
        return None
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'list', '--repo', repo_coords,
             '--head', branch, '--state', 'all',
             '--json', 'number,state'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'gh pr list --head {branch} ({repo_coords}) FAILED during '
            f'phantom-build terminal check: {type(e).__name__}: {e}',
            'WARN',
        )
        return None
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        log(
            f'gh pr list --head {branch} ({repo_coords}) returned '
            f'{proc.returncode} during phantom-build terminal check: '
            f'{(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return None
    try:
        payload = json.loads(proc.stdout or '[]')
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    states = {
        item.get('state')
        for item in payload
        if isinstance(item, dict)
    }
    if 'OPEN' in states:
        return 'OPEN'
    if 'MERGED' in states:
        return 'MERGED'
    if 'CLOSED' in states:
        return 'CLOSED'
    return None


# build-mirror-review-status — Mirror verdict -> GitHub commit status.
# A required `mirror-review` status check on `main` makes Mirror's pass
# physically enforceable: no actor (even the admin identity) can merge a PR
# unless this notifier posted state=success for it. We post at the verdict
# moment, BEFORE the auto-merge shell-out, so a Mirror-passed PR satisfies
# the (soon-to-be) required check at merge time; REVISION / ESCALATE /
# EMERGENCY_HALT post state=failure so those PRs stay blocked (the #303
# hole). Best-effort + idempotent: GitHub keeps the latest status per
# (sha, context), and any gh error is logged and swallowed — this MUST
# NEVER crash the notifier or block the merge flow.
_MIRROR_REVIEW_STATUS_CONTEXT = 'mirror-review'

# marker_type -> (commit-status state, description).
_MIRROR_REVIEW_STATUS_BY_MARKER: dict[str, tuple[str, str]] = {
    'review_pass': ('success', 'Mirror review passed'),
    'review_revision': ('failure', 'REVIEW_REVISION'),
    'review_escalate': ('failure', 'REVIEW_ESCALATE'),
    'review_emergency_halt': ('failure', 'REVIEW_EMERGENCY_HALT'),
}

# Test seam (mirrors _AUTO_MERGE_FN_OVERRIDE). When set, replaces the whole
# `_post_mirror_review_commit_status` body so integration tests routing
# Mirror verdicts through process_outbox don't shell out to real `gh`.
# Production leaves this None.
_POST_STATUS_FN_OVERRIDE: Optional[Any] = None


def _gh_pr_head_sha(repo_coords: str, pr_number: int) -> Optional[str]:
    """Return the PR head commit SHA via `gh pr view --json headRefOid`, or None.

    None on any transport error / non-zero exit / parse failure — the caller
    treats that as "couldn't resolve the head SHA; skip the status POST."
    """
    if _gh_backoff_skip('pr-head-sha'):
        return None
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number),
             '--repo', repo_coords, '--json', 'headRefOid'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'gh pr view {pr_number} ({repo_coords}) headRefOid lookup '
            f'FAILED: {type(e).__name__}: {e}',
            'WARN',
        )
        return None
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        log(
            f'gh pr view {pr_number} ({repo_coords}) headRefOid returned '
            f'{proc.returncode}: {(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return None
    try:
        payload = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return None
    sha = payload.get('headRefOid')
    if isinstance(sha, str) and sha:
        return sha
    return None


def _post_mirror_review_commit_status(
    data: dict[str, Any], marker_decision: dict[str, Any],
) -> Optional[str]:
    """POST a `mirror-review` commit status for a classified Mirror verdict.

    REVIEW_PASS -> state=success; REVIEW_REVISION / REVIEW_ESCALATE /
    REVIEW_EMERGENCY_HALT -> state=failure. Targets the PR head SHA (the
    commit branch-protection gates) via `gh api repos/{repo}/statuses/{sha}`.
    Returns the posted state on success, or None when skipped/failed.

    Best-effort: never raises into the caller and never blocks the merge
    flow. Idempotent — re-posting the same (sha, context, state) is harmless
    (GitHub keeps the latest status per context).
    """
    if _POST_STATUS_FN_OVERRIDE is not None:
        try:
            return _POST_STATUS_FN_OVERRIDE(data, marker_decision)
        except Exception as e:  # noqa: BLE001 — test seam must not wedge daemon
            log(
                f'MIRROR_REVIEW_STATUS override raised: {type(e).__name__}: {e}',
                'WARN',
            )
            return None

    marker_type = marker_decision.get('marker_type')
    mapping = _MIRROR_REVIEW_STATUS_BY_MARKER.get(marker_type)
    if mapping is None:
        return None
    state, description = mapping
    payload = marker_decision.get('payload') or {}
    pr_url = payload.get('pr_url') if isinstance(payload, dict) else None
    task_id_log = data.get('task_id', '?')

    repo_coords, pr_number, shape_reason = _pr_url_shape_check(pr_url)
    if repo_coords is None:
        log(
            f'MIRROR_REVIEW_STATUS task={task_id_log} pr={pr_url!r} '
            f'skipped reason=pr-url-shape-invalid ({shape_reason})',
            'WARN',
        )
        return None

    head_sha = _gh_pr_head_sha(repo_coords, pr_number)
    if not head_sha:
        log(
            f'MIRROR_REVIEW_STATUS task={task_id_log} pr={pr_url} '
            f'skipped reason=no-head-sha',
            'WARN',
        )
        return None

    try:
        proc = gh_write(
            ['gh', 'api', f'repos/{repo_coords}/statuses/{head_sha}',
             '-f', f'state={state}',
             '-f', f'context={_MIRROR_REVIEW_STATUS_CONTEXT}',
             '-f', f'description={description}'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'MIRROR_REVIEW_STATUS task={task_id_log} pr={pr_url} '
            f'sha={head_sha[:12]} POST FAILED: {type(e).__name__}: {e}',
            'WARN',
        )
        return None
    if proc.returncode != 0:
        log(
            f'MIRROR_REVIEW_STATUS task={task_id_log} pr={pr_url} '
            f'sha={head_sha[:12]} POST gh exit={proc.returncode}: '
            f'{(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return None
    log(
        f'MIRROR_REVIEW_STATUS task={task_id_log} pr={pr_url} '
        f'sha={head_sha[:12]} context={_MIRROR_REVIEW_STATUS_CONTEXT} '
        f'state={state} posted'
    )
    return state


# build-mirror-findings-comment — Contract A of
# agents/beacon/specs/mirror-review-visibility.md § 4. Every non-PASS Mirror
# verdict (REVIEW_REVISION / REVIEW_ESCALATE) posts its findings as a durable
# PR comment IN ADDITION to the `mirror-review` commit status, regardless of
# whether a live Forge session exists ("session or not"). The findings are
# then for-the-record + consumable by Beacon/Forge without digging into agent
# inboxes. We post mechanically here — at the same marker-classification site
# that posts the commit status — rather than relying on Mirror's review session
# to shell out, because an unenforced LLM turn is not a durable guarantee
# (the doctrine the chain-context-durability line of work hardened).
#
# Idempotent: the body leads with a stable hidden anchor; a re-review UPDATES
# the existing Mirror findings comment instead of appending a new one (no
# comment spam across revision rounds). Best-effort + daemon-never-wedge, same
# posture as `_post_mirror_review_commit_status`: any gh error is logged and
# swallowed — this MUST NEVER crash the notifier or block the merge flow.
_MIRROR_FINDINGS_ANCHOR = '<!-- mirror-findings -->'

# Marker types that yield a findings comment. REVIEW_PASS posts nothing (there
# is nothing to fix); REVIEW_EMERGENCY_HALT routes via the halt-file trip +
# broadcast priority DM, not a PR comment — Contract A's scope is the
# revise/escalate verdicts (§ 4).
_MIRROR_FINDINGS_MARKERS = ('review_revision', 'review_escalate')

# Test seam (mirrors _POST_STATUS_FN_OVERRIDE). When set, replaces the whole
# `_post_mirror_findings_comment` body so process_outbox integration tests
# don't shell out to real `gh`. Production leaves this None.
_POST_FINDINGS_FN_OVERRIDE: Optional[Any] = None


def _render_mirror_findings_comment_body(
    marker_type: str, payload: dict[str, Any],
) -> str:
    """Render the durable PR findings comment for a non-PASS Mirror verdict.

    The body leads with the hidden anchor (`_MIRROR_FINDINGS_ANCHOR`) so the
    upsert can find + update its own prior comment on re-review. Pure — no I/O;
    unit-tested directly.
    """
    lines = [_MIRROR_FINDINGS_ANCHOR, '']
    if marker_type == 'review_revision':
        findings = payload.get('findings')
        findings = findings if isinstance(findings, list) else []
        severity = payload.get('severity', '?')
        confidence = payload.get('confidence', '?')
        lines.append(
            f'## Mirror review: REVIEW_REVISION '
            f'({len(findings)} finding(s), severity={severity}, '
            f'confidence={confidence})'
        )
        lines.append('')
        for i, finding in enumerate(findings, 1):
            if not isinstance(finding, dict):
                continue
            file = finding.get('file', '?')
            line_range = finding.get('line_range', '?')
            fsev = finding.get('severity', '?')
            desc = finding.get('description', '')
            lines.append(f'{i}. **`{file}`** ({line_range}) — _{fsev}_')
            if desc:
                lines.append(f'   {desc}')
    else:  # review_escalate
        severity = payload.get('severity', '?')
        confidence = payload.get('confidence', '?')
        reason = payload.get('reason', '')
        lines.append(
            f'## Mirror review: REVIEW_ESCALATE '
            f'(severity={severity}, confidence={confidence})'
        )
        lines.append('')
        if reason:
            lines.append(str(reason))
    lines.append('')
    lines.append(
        '_Posted by the outbox notifier on Mirror\'s verdict; updates in place '
        'on re-review (Contract A, mirror-review-visibility)._'
    )
    return '\n'.join(lines)


def _gh_find_mirror_findings_comment(
    repo_coords: str, pr_number: int,
) -> Optional[int]:
    """Return the id of the existing Mirror findings comment on the PR, or None.

    Identifies the comment by the hidden anchor in its body. Read-only `gh api`
    GET (NOT routed through gh_write). Best-effort: any transport/parse failure
    returns None, so the caller posts a fresh comment — at worst a duplicate on
    a degraded gh, never a crash.
    """
    if _gh_backoff_skip('mirror-findings-comment-lookup'):
        return None
    try:
        proc = subprocess.run(
            ['gh', 'api', '--paginate',
             f'repos/{repo_coords}/issues/{pr_number}/comments',
             '--jq', '.[] | {id: .id, body: .body}'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        return None
    # `--paginate --jq` emits one JSON object per line (per matched element).
    for line in (proc.stdout or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        body = obj.get('body') or ''
        if _MIRROR_FINDINGS_ANCHOR in body:
            cid = obj.get('id')
            if isinstance(cid, int):
                return cid
    return None


def _post_mirror_findings_comment(
    data: dict[str, Any], marker_decision: dict[str, Any],
) -> Optional[str]:
    """Upsert Mirror's findings as a durable PR comment (Contract A).

    Fires for REVIEW_REVISION / REVIEW_ESCALATE only. Idempotent via the hidden
    anchor: updates the existing Mirror findings comment on re-review instead of
    appending a new one. Returns 'created' / 'updated' on success, or None when
    skipped/failed.

    Best-effort: never raises into the caller and never blocks the merge flow,
    same contract as `_post_mirror_review_commit_status`.
    """
    if _POST_FINDINGS_FN_OVERRIDE is not None:
        try:
            return _POST_FINDINGS_FN_OVERRIDE(data, marker_decision)
        except Exception as e:  # noqa: BLE001 — test seam must not wedge daemon
            log(
                f'MIRROR_FINDINGS_COMMENT override raised: '
                f'{type(e).__name__}: {e}',
                'WARN',
            )
            return None

    marker_type = marker_decision.get('marker_type')
    if marker_type not in _MIRROR_FINDINGS_MARKERS:
        return None
    payload = marker_decision.get('payload') or {}
    if not isinstance(payload, dict):
        return None
    pr_url = payload.get('pr_url')
    task_id_log = data.get('task_id', '?')

    repo_coords, pr_number, shape_reason = _pr_url_shape_check(pr_url)
    if repo_coords is None:
        log(
            f'MIRROR_FINDINGS_COMMENT task={task_id_log} pr={pr_url!r} '
            f'skipped reason=pr-url-shape-invalid ({shape_reason})',
            'WARN',
        )
        return None

    body = _render_mirror_findings_comment_body(marker_type, payload)
    existing_id = _gh_find_mirror_findings_comment(repo_coords, pr_number)

    try:
        if existing_id is not None:
            proc = gh_write(
                ['gh', 'api', '-X', 'PATCH',
                 f'repos/{repo_coords}/issues/comments/{existing_id}',
                 '-f', f'body={body}'],
                capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
            )
            action = 'updated'
        else:
            proc = gh_write(
                ['gh', 'api',
                 f'repos/{repo_coords}/issues/{pr_number}/comments',
                 '-f', f'body={body}'],
                capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
            )
            action = 'created'
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'MIRROR_FINDINGS_COMMENT task={task_id_log} pr={pr_url} '
            f'POST FAILED: {type(e).__name__}: {e}',
            'WARN',
        )
        return None
    if proc.returncode != 0:
        log(
            f'MIRROR_FINDINGS_COMMENT task={task_id_log} pr={pr_url} '
            f'{action} gh exit={proc.returncode}: '
            f'{(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return None
    log(
        f'MIRROR_FINDINGS_COMMENT task={task_id_log} pr={pr_url} '
        f'marker={marker_type} comment {action}'
    )
    return action


def _emit_auto_merge_chain_event(
    *,
    task_id: str,
    pr_url: str,
    outcome: str,
    log_msg: str,
    log_ts: str,
) -> None:
    """Push the verified-merge `auto_merge` chain_event at the merge moment.

    S-4 freshness (spec § S5). The `auto_merge` row is what the board reads to
    flip a linked card to "shipped" and what `build_sequence_advancer`'s
    belt-and-suspenders merge gate reads to advance a sequence. Before S-4 it
    reached Supabase ONLY via the shipper's log-tail poll of this very line —
    a 30-60s lag, and zero rows whenever the shipper is down. Push-emitting it
    here closes that lag to one short cycle with no restart/sync dependency.

    ADDITIVE accelerator, NOT a replacement: the log line still ships via the
    poll path, which stays the durable backstop (the push has no local spill,
    so a Supabase blip drops the push — the shipper's EventBuffer then carries
    the same event when it recovers). To keep the pair from double-writing, the
    push reproduces the EXACT event_id the shipper's `parse_log_line` derives
    for this line: same normalized ts (`ces._normalize_iso_ts(log_ts)`, where
    `log_ts` is the stamp this line was actually written with) and same
    `id_extra` (the full message `rest`, i.e. `log_msg`). The deterministic
    event_id then collides and `ignore_duplicates=True` drops whichever lands
    second. `payload.outcome` is set because the advancer's merge gate requires
    `payload.get('outcome') in ('merged', 'already_merged')`.

    Best-effort + daemon-never-wedge, same contract as the sibling push
    helpers (`_emit_mirror_verdict_chain_event` et al.): emit_event logs WARN
    and returns False on any Supabase failure; the merge / DM / archive flow
    never depends on the row landing.
    """
    norm_ts = ces._normalize_iso_ts(log_ts)
    chain_payload = {
        'agent': 'forge',
        'task_id': task_id,
        'outcome': outcome,
        'pr_url': pr_url,
    }
    try:
        chain_event_emit.emit_event(
            event_type='auto_merge',
            agent='forge',
            task_id=task_id,
            payload=chain_payload,
            ts=norm_ts,
            pr_url=pr_url,
            id_extra=log_msg,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'auto_merge chain_event emit raised unexpectedly for '
            f'task {task_id!r}: {type(e).__name__}: {e}',
            'WARN',
        )


def _spawn_post_merge_baseline_warm(task_id: str, pr_url: str) -> None:
    """Fire-and-forget: warm the regression baseline for the new main HEAD.

    regression-gate-steady-state-warmer (spec PR 2). Called right after a
    confirmed auto-merge. The squash-merge just advanced main, so the commit a
    *future* PR will fork from (its ``baseRefOid``, the gate's ``--parent-sha``)
    now exists on origin/main. We precompute + cache that parent baseline here,
    OFF the review critical path, so the next PR's FIRST review HITS the cache
    and the gate runs ONE suite pass (head only) — concluding under the 900s
    bounded-step ceiling instead of timing out on a cold two-run (#733/#736/#749).

    The detached child does the network + CPU work (``git fetch`` then the full
    suite via ``regression_baseline_cache.py warm``), so the notifier's poll
    loop never blocks. ``OL_REGRESSION_BASELINE_DIR`` is pinned to the canonical
    dir the Mirror gate reads, so the warmed files are the ones the gate looks
    up. NEVER blocks, delays, or fails the merge: the whole body is wrapped so
    any spawn error is swallowed + logged, never propagated into the merge path.
    ``warm`` is idempotent (no-op on a cache hit), so a duplicate or a killed
    run self-heals on the next merge — and PR 1's lazy warm-on-miss remains the
    backstop if the warm never lands.
    """
    # Re-entrancy guard (regbaseline fork-bomb, 2026-06-29): never spawn a warm
    # from inside a warm's own suite run. warm() exports REGBASELINE_WARMING=1
    # around its discover pass and build_sandbox_env propagates it into the
    # jailed suite; a warmer-fixture (or any merge-path) test that reaches this
    # real detached Popen during that pass would fork ANOTHER production warm
    # -> unbounded recursion. Skip when already warming.
    if os.environ.get('REGBASELINE_WARMING') == '1':
        log(
            f'BASELINE_WARM task={task_id} pr={pr_url} '
            f'outcome=skipped_reentrant (already warming) agent=forge',
        )
        return
    try:
        repo_root = _WARM_REPO_ROOT
        warm_py = str(_SCRIPT_DIR / 'regression_baseline_cache.py')
        # Shell string contains ONLY internal constants (no task_id/pr_url, no
        # external input) — the two steps must run sequentially in the detached
        # child so the fetch (which makes the new HEAD a local object) precedes
        # the worktree-based warm. FETCH_HEAD is the just-fetched origin/main tip
        # = the squash-merge commit = the future PR's parent SHA.
        warm_cmd = (
            f'git -C {shlex.quote(repo_root)} fetch --quiet origin main && '
            f'{shlex.quote(sys.executable)} {shlex.quote(warm_py)} warm '
            f'--sha FETCH_HEAD --repo-root {shlex.quote(repo_root)} '
            f'--timeout-per-sha {_WARM_TIMEOUT_PER_SHA_S}'
        )
        env = os.environ.copy()
        env['OL_REGRESSION_BASELINE_DIR'] = REGRESSION_BASELINE_CANONICAL_DIR
        # start_new_session detaches the child into its own process group so it
        # outlives this notifier turn; output is discarded (warm logs its own
        # outcome, and a regression-gate timeout self-heals on the next merge).
        subprocess.Popen(
            ['bash', '-c', warm_cmd],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log(
            f'BASELINE_WARM task={task_id} pr={pr_url} outcome=spawned '
            f'(detached warm of post-merge origin/main; dir='
            f'{REGRESSION_BASELINE_CANONICAL_DIR}) agent=forge',
        )
    except Exception as exc:  # never let a warmer error touch the merge outcome
        log(
            f'BASELINE_WARM task={task_id} pr={pr_url} outcome=spawn_failed '
            f'({type(exc).__name__}: {exc}) agent=forge',
            'WARN',
        )


# The registry template (config/auto-fix-patterns.json) this executor is the
# act-time recorder for. Auto-merge is by far the highest-frequency reversible
# automation the system performs; recording its verified successes/failures is
# the streak INPUT Check V's promotion loop needs to ever surface an auto-merge
# graduation proposal.
_AUTO_MERGE_TEMPLATE = 'auto-merge-clean-pr'


def _record_auto_merge_success() -> None:
    """Record one clean ``auto-merge-clean-pr`` execution toward Check V's
    graduation streak (delegates to the shared, registry-gated, never-raise
    ``alert_triage_state.record_clean_execution_if_registered``, PR #832 pattern).

    SUCCESS-ONLY by design. A ``gh pr merge`` failure is recorded NOWHERE: gh
    returns non-zero for transient/infra causes too (a still-running required
    check, a GitHub 5xx, a mid-flight rebase race), and recording those as a
    graduation ``failure`` would ungatedly demote a graduated auto-merge template
    on noise. The reliable "this auto-merge was a mistake" signal is a
    Larry-correction (a rollback of a bad merge), not a gh exit code — so the
    streak only ever grows on a verified landed merge. Additive/best-effort: it
    observes a merge that already happened and must never affect it.
    """
    try:
        import alert_triage_state  # lazy: keep the module's import surface light
        alert_triage_state.record_clean_execution_if_registered(
            _AUTO_MERGE_TEMPLATE)
    except Exception:  # never let a track-record write surface into the merge
        pass


def _auto_merge_pr(pr_url: str, task_id: str) -> dict[str, Any]:
    """Fire `gh pr merge <N> --squash --delete-branch` for a Mirror-PASSed PR.

    D3.5 commit 5d. Returns a result dict the DM-render pipeline reads:

      {
        'merge_outcome': 'merged' | 'already_merged' | 'failed',
        'merge_reason':  str,        # short — fits in a phone DM
        'pr_number':     int | str,  # int when parsed, '?' on URL-parse fail
        'repo_coords':   str,        # e.g. 'Larry-Yatch/ourliberty-agent-core'
      }

    The order in `process_outbox`'s marker-routing block is
    **merge → render DM → DM queue → archive** (per Larry's sign-off):
    the merge is the irreversible action; render-DM after so the body
    accurately reflects what actually happened. The outbox archives last
    so a daemon crash mid-merge re-processes the same outbox on restart;
    the second call gets `already_merged` from `_gh_pr_state` and renders
    the success body (`gh pr merge` on a merged PR returns non-zero, but
    the state recheck disambiguates).

    Patterns: upstream `merge_watcher.merge_pr` (line 117) shape for the
    gh shell-out, extended with --delete-branch (per d3-5-plan Item 6
    sign-off) and the state-recheck disambiguation that resume-safety
    requires. Stderr capture mirrors how `subprocess.run` is used
    throughout the codebase (see worktree_manager.py).

    Failure modes the gh CLI can return:
      - merge conflict (mergeable=CONFLICTING) — `failed`
      - branch protection denial — `failed`
      - PR closed without merge — `failed`
      - auth expired / token revoked — `failed`
      - network timeout / unreachable — `failed`
      - already-merged (resume after crash) — `already_merged`
      - malformed PR URL (couldn't parse) — `failed` (no shell-out)
      - gh CLI missing entirely (FileNotFoundError) — `failed`

    Every AUTO_MERGE log line (here and in the process_outbox validation
    paths) ends with `agent=forge`: chain_event_shipper ships these lines
    to Supabase chain_events, and the dashboard's Forge-queue lanes only
    fetch rows with agent='forge' — a line without the kv ships as
    agent='notifier' and stays invisible there. The kv must stay at the
    END of the line: heal_pr_auto_merge._AUTO_MERGE_FAILED_RE requires
    `task= pr= outcome=` to remain adjacent.
    """
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        log(
            f'AUTO_MERGE task={task_id} pr={pr_url!r} outcome=failed '
            f'reason=malformed-pr-url (no shell-out attempted) agent=forge',
            'WARN',
        )
        return {
            'merge_outcome': 'failed',
            'merge_reason': f'malformed PR URL: {pr_url!r}',
            'pr_number': '?',
            'repo_coords': '?',
        }
    repo_coords, pr_number = parsed
    if _gh_backoff_skip('auto-merge'):
        # Defer the merge (not fail it): the GH API is exhausted, a merge
        # attempt would just re-hit the rate limit and re-arm the window. The
        # auto-merge healer (heal_pr_auto_merge) retries a 'failed' outcome
        # once the window expires, so the PR still merges — just not this poll.
        return {
            'merge_outcome': 'failed',
            'merge_reason': (
                f'deferred: gh rate-limit backoff active '
                f'({_gh_backoff_remaining():.0f}s remaining)'
            ),
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }
    try:
        proc = gh_write(
            ['gh', 'pr', 'merge', str(pr_number),
             '--repo', repo_coords,
             '--squash',
             '--delete-branch'],
            capture_output=True,
            text=True,
            timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        log(
            f'AUTO_MERGE task={task_id} pr={pr_url} outcome=failed '
            f'reason=timeout after {_AUTO_MERGE_TIMEOUT_S}s ({e}) agent=forge',
            'WARN',
        )
        return {
            'merge_outcome': 'failed',
            'merge_reason': f'gh pr merge timed out after {_AUTO_MERGE_TIMEOUT_S}s',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }
    except FileNotFoundError as e:
        log(
            f'AUTO_MERGE task={task_id} pr={pr_url} outcome=failed '
            f'reason=gh-cli-missing ({e}) agent=forge',
            'WARN',
        )
        return {
            'merge_outcome': 'failed',
            'merge_reason': 'gh CLI not found on PATH',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }
    except OSError as e:
        log(
            f'AUTO_MERGE task={task_id} pr={pr_url} outcome=failed '
            f'reason=os-error ({type(e).__name__}: {e}) agent=forge',
            'WARN',
        )
        return {
            'merge_outcome': 'failed',
            'merge_reason': f'{type(e).__name__}: {e}',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    # Route the completed merge through the backoff gate: a rate-limit stderr
    # arms the window (so the state-recheck below and the next poll skip gh),
    # a clean merge clears it; a plain conflict/branch-protection exit is left
    # alone (not a throttling signal).
    _gh_note_result(proc.returncode, proc.stderr)

    if proc.returncode == 0:
        merge_msg = (
            f'AUTO_MERGE task={task_id} pr={pr_url} outcome=merged '
            f'(--squash --delete-branch) agent=forge'
        )
        merge_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log(merge_msg, ts=merge_ts)
        _emit_auto_merge_chain_event(
            task_id=task_id, pr_url=pr_url, outcome='merged',
            log_msg=merge_msg, log_ts=merge_ts,
        )
        # Off-path: warm the new main HEAD's regression baseline so the next
        # PR's first review hits the cache (spec PR 2). Fire-and-forget.
        _spawn_post_merge_baseline_warm(task_id, pr_url)
        _record_auto_merge_success()
        return {
            'merge_outcome': 'merged',
            'merge_reason': 'squash-merged + branch deleted',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    # Non-zero exit — could be a real failure OR a resume-after-crash where
    # the PR was already merged on the prior pass. Disambiguate via state
    # recheck before reporting failure.
    stderr_text = (proc.stderr or '').strip()
    state = _gh_pr_state(repo_coords, pr_number)
    if state == 'MERGED':
        already_msg = (
            f'AUTO_MERGE task={task_id} pr={pr_url} outcome=already_merged '
            f'(gh exit={proc.returncode} but state=MERGED — resume from '
            f'prior crash; treating as success) agent=forge'
        )
        already_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log(already_msg, ts=already_ts)
        _emit_auto_merge_chain_event(
            task_id=task_id, pr_url=pr_url, outcome='already_merged',
            log_msg=already_msg, log_ts=already_ts,
        )
        # Same off-path warm as the merged branch — idempotent, so warming
        # again on a resume-after-crash is a cheap no-op if already cached.
        _spawn_post_merge_baseline_warm(task_id, pr_url)
        return {
            'merge_outcome': 'already_merged',
            'merge_reason': 'PR was already merged (resume from prior dispatch)',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }
    # Real failure. Trim stderr to keep the DM body sane on phone.
    reason_short = stderr_text[:200] if stderr_text else f'gh exit {proc.returncode}'
    log(
        f'AUTO_MERGE task={task_id} pr={pr_url} outcome=failed '
        f'(gh exit={proc.returncode}, state={state}, '
        f'stderr={stderr_text[:300]!r}) agent=forge',
        'WARN',
    )
    # No graduation record on failure — see _record_auto_merge_success: a gh
    # non-zero exit is an unreliable action-quality signal (transient / infra),
    # so the streak is success-only and demotion comes from a Larry-correction.
    return {
        'merge_outcome': 'failed',
        'merge_reason': reason_short,
        'pr_number': pr_number,
        'repo_coords': repo_coords,
    }


# ============================================================================
# V6 (orchestrator-rectification-v2) — step-merged signal for active sequences
# ============================================================================

def _signal_sequence_step_merged(
    task_id: str, pr_url: str, merged_at_iso: str,
) -> Optional[str]:
    """If `task_id` matches a step_id in any active sequence file, call
    `sequence_shortcut_helpers.apply_step_merged()` to flip the step's
    status `dispatched → merged`, record `pr_url` + `merged_at`, and
    remove the step from `current_steps`.

    Fired from inside `_attempt_auto_merge_with_gates` whenever
    `merge_outcome ∈ {merged, already_merged}`, regardless of which
    caller drove the gate (marker-routing for fresh Mirror PASSes,
    `_queue_release` for blocked-then-released PRs, or
    `_auto_merge_queue_sweep` for UNKNOWN-retry releases). Centralizing
    here closes the 2026-05-29 silent-miss: previously the hook lived
    only at the marker-routing call site, so PRs that merged via the
    release/retry paths (e.g. `step-rescue-runbook` PR #21 on
    `operator-ux-rollout`) succeeded without ever signalling the
    sequence — sequence.current_steps stayed stale until manual
    `apply_step_merged`.

    Without this signal the build_sequence_advancer never observes
    the merge — bootstrap-002 V6 surfaced the original wedge (step
    merged at 17:36 MDT; sequence.current_steps still listed it 9 hours
    later until manual cancel).

    Returns the seq_id of the sequence whose step was updated, or None if
    no active sequence claimed this task_id. Defensive: any read /
    write / mutation error is logged as WARN and swallowed — the caller
    (the marker-routing block) MUST NOT crash on a sequence-file
    accident; the post-merge DM is the safety-critical surface, the
    sequence-state propagation is best-effort.

    Active-sequence definition: status ∈ {pending, active}. Terminal
    sequences (complete / failed / archived / paused) are skipped — a
    matching task_id there is either historical reconcilliation or a
    re-fire after pause and would be wrong to mutate without operator
    intent.

    Idempotency: `apply_step_merged` itself returns `applied=False` when
    the step is already merged, so a re-fire from outbox re-processing
    is a clean no-op.
    """
    try:
        seq_dir = AGENTS_ROOT / 'blackboard' / 'build-sequences'
        if not seq_dir.is_dir():
            return None
        for seq_path in sorted(seq_dir.glob('*.json')):
            # Skip jsonl side-channels (.kickoff-failures.jsonl etc).
            if not seq_path.suffix == '.json':
                continue
            try:
                seq = json.loads(seq_path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                log(
                    f'sequence-step-merged: skipping {seq_path.name} '
                    f'(unreadable: {type(e).__name__}: {e})',
                    'WARN',
                )
                continue
            if not isinstance(seq, dict):
                continue
            if seq.get('status') not in ('pending', 'active'):
                continue
            steps = seq.get('steps') or []
            if not any(
                isinstance(s, dict) and s.get('step_id') == task_id
                for s in steps
            ):
                continue
            seq_id = seq.get('seq_id')
            if not isinstance(seq_id, str) or not seq_id:
                continue
            try:
                result = ssh.apply_step_merged(
                    seq_id=seq_id,
                    step_id=task_id,
                    pr_url=pr_url,
                    merged_at=merged_at_iso,
                    actor='notifier',
                )
            except Exception as e:  # noqa: BLE001 — daemon-never-wedge
                log(
                    f'SEQUENCE_STEP_MERGED seq={seq_id} step={task_id} '
                    f'pr={pr_url} apply_step_merged raised '
                    f'{type(e).__name__}: {e}',
                    'WARN',
                )
                return seq_id
            if result.error:
                log(
                    f'SEQUENCE_STEP_MERGED seq={seq_id} step={task_id} '
                    f'pr={pr_url} hard-error: {result.reason}',
                    'WARN',
                )
                return seq_id
            if result.applied:
                log(
                    f'SEQUENCE_STEP_MERGED seq={seq_id} step={task_id} '
                    f'pr={pr_url}',
                )
            else:
                log(
                    f'SEQUENCE_STEP_MERGED seq={seq_id} step={task_id} '
                    f'pr={pr_url} no-op ({result.reason})',
                )
            # Contract A (p4-complete-signal): if THIS merge was the last step,
            # apply_step_merged flipped the sequence to `complete`. Emit the
            # one-time completion signal (chain event + Larry DM). Runs on the
            # no-op branch too: a crash-resume re-fire of an already-merged last
            # step re-detects completion, and the signal's own idempotency guard
            # (sequence-complete-signaled marker) ensures no double-DM.
            _maybe_signal_sequence_complete(seq_id)
            return seq_id
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'sequence-step-merged scan raised {type(e).__name__}: {e}; '
            f'swallowing — DM path still fires',
            'WARN',
        )
    return None


def _active_sequence_id_for_step(task_id: str) -> Optional[str]:
    """Return the seq_id of the single active (status ∈ {pending, active})
    sequence that contains a step whose `step_id == task_id`, or None.

    Shared active-sequence scan for the push-signal helpers below
    (`_signal_sequence_step_failed`, `_signal_sequence_step_pr_opened`),
    using the same convention as `_signal_sequence_step_merged`: match a step
    by `step_id == task_id` (not by parsing the id string) and skip terminal
    sequences (complete / failed / archived / paused). Best-effort: an
    unreadable / malformed file is logged WARN and skipped; the caller's notify
    path never depends on the scan. Returns the FIRST matching seq_id."""
    seq_dir = AGENTS_ROOT / 'blackboard' / 'build-sequences'
    if not seq_dir.is_dir():
        return None
    for seq_path in sorted(seq_dir.glob('*.json')):
        if seq_path.suffix != '.json':
            continue
        try:
            seq = json.loads(seq_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log(
                f'sequence-step-scan: skipping {seq_path.name} '
                f'(unreadable: {type(e).__name__}: {e})',
                'WARN',
            )
            continue
        if not isinstance(seq, dict):
            continue
        if seq.get('status') not in ('pending', 'active'):
            continue
        seq_id = seq.get('seq_id')
        if not isinstance(seq_id, str) or not seq_id:
            continue
        steps = seq.get('steps') or []
        if any(
            isinstance(s, dict) and s.get('step_id') == task_id
            for s in steps
        ):
            return seq_id
    return None


def _signal_sequence_step_failed(
    task_id: str, failure_reason: str,
) -> Optional[str]:
    """Push-signal a build-sequence step FAILED the moment the notifier
    classifies a terminal non-merge outcome — a Forge preflight REJECT, a
    marker-error / dead-letter exhaustion, or a build crash — instead of
    leaving the step `dispatched` for the advancer's gate poll + 4h stall
    backstop to (mis)handle.

    Sibling to `_signal_sequence_step_merged`. Same active-sequence scan and
    step-match (`step_id == task_id`), same daemon-never-wedge posture: every
    read / mutation error is logged WARN and swallowed so the notify /
    dead-letter path the caller is on never crashes on a sequence-file
    accident. Calls `ssh.apply_step_failed`, which flips the step `→ failed`,
    records `failure_reason`, keeps it in `current_steps` for visibility, and
    pauses the sequence.

    On a real transition (`applied=True`) raises ONE Larry doorbell alert via
    `larry_alerts.append_alert` — the paused sequence has no reply-chat thread,
    so this is the same sink the advancer's pause-DM and
    `_maybe_signal_sequence_complete` use. Because the sequence is now
    `paused`, the advancer skips it on its next tick, so there is no
    double-alert. The idempotent no-op branch (already failed / already merged)
    alerts nothing.

    Why this matters (slice-2b incident): a non-merge terminal otherwise leaves
    the step `dispatched`, where `_escalate_stranded_dispatched_steps`
    eventually MISATTRIBUTES the stall as "Forge may never have picked it up."
    Flipping the step terminal here removes it from `dispatched` at once.

    Returns the seq_id whose step was failed, or None when no active sequence
    claimed this task_id (the common case — most outboxes are not sequence
    steps)."""
    try:
        seq_id = _active_sequence_id_for_step(task_id)
        if seq_id is None:
            return None
        try:
            result = ssh.apply_step_failed(
                seq_id=seq_id,
                step_id=task_id,
                failure_reason=failure_reason,
                actor='notifier',
            )
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge
            log(
                f'SEQUENCE_STEP_FAILED seq={seq_id} step={task_id} '
                f'apply_step_failed raised {type(e).__name__}: {e}',
                'WARN',
            )
            return seq_id
        if result.error:
            log(
                f'SEQUENCE_STEP_FAILED seq={seq_id} step={task_id} '
                f'hard-error: {result.reason}',
                'WARN',
            )
            return seq_id
        if not result.applied:
            log(
                f'SEQUENCE_STEP_FAILED seq={seq_id} step={task_id} '
                f'no-op ({result.reason})',
            )
            return seq_id
        log(
            f'SEQUENCE_STEP_FAILED seq={seq_id} step={task_id}: '
            f'{failure_reason}',
        )
        try:
            larry_alerts.append_alert(
                source='outbox-notifier',
                severity='warning',
                message=(
                    f'Build sequence `{seq_id}` paused — step `{task_id}` '
                    f'failed:\n\n{failure_reason}\n\n'
                    f'Recovery shortcuts: `resume sequence {seq_id}` / '
                    f'`cancel sequence {seq_id}` / `retry sequence {seq_id} '
                    f'step {task_id}` (after fixing the underlying issue).'
                ),
                subject=f'sequence-paused:{seq_id}',
                route='escalate',
            )
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge
            log(
                f'SEQUENCE_STEP_FAILED seq={seq_id} step={task_id} '
                f'pause-alert raised {type(e).__name__}: {e}; '
                f'state already transitioned',
                'WARN',
            )
        return seq_id
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'sequence-step-failed scan raised {type(e).__name__}: {e}; '
            f'swallowing — caller notify path still fires',
            'WARN',
        )
    return None


def _signal_sequence_step_pr_opened(
    task_id: str, pr_url: str,
) -> Optional[str]:
    """Record `pr_url` + flip a build-sequence step to the `reviewing`
    substatus the moment Forge's build PR opens (the notifier dispatches
    Mirror's review at the same site), rather than recording `pr_url` only at
    merge.

    Sibling scan to `_signal_sequence_step_merged`; calls
    `ssh.apply_step_pr_opened`. No Larry alert — an in-flight PR-open is normal
    progress, not an operator event. daemon-never-wedge throughout: a
    sequence-file accident is logged WARN and swallowed, and the Mirror-review
    dispatch the caller already performed is unaffected.

    Recording `pr_url` at OPEN restores (1) the advancer's dual-gate `gh` leg
    during review — `gh_pr_says_merged(step.pr_url)` is computed only
    `if pr_url`, so an unset value left it dark until merge — and (2) the stall
    backstop's never-opened-a-PR vs. open-PR-in-review distinguisher.

    Returns the seq_id updated, or None when no active sequence claims this
    task_id."""
    try:
        seq_id = _active_sequence_id_for_step(task_id)
        if seq_id is None:
            return None
        try:
            result = ssh.apply_step_pr_opened(
                seq_id=seq_id,
                step_id=task_id,
                pr_url=pr_url,
                actor='notifier',
            )
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge
            log(
                f'SEQUENCE_STEP_PR_OPENED seq={seq_id} step={task_id} '
                f'pr={pr_url} apply_step_pr_opened raised '
                f'{type(e).__name__}: {e}',
                'WARN',
            )
            return seq_id
        if result.error:
            log(
                f'SEQUENCE_STEP_PR_OPENED seq={seq_id} step={task_id} '
                f'pr={pr_url} hard-error: {result.reason}',
                'WARN',
            )
            return seq_id
        if result.applied:
            log(
                f'SEQUENCE_STEP_PR_OPENED seq={seq_id} step={task_id} '
                f'pr={pr_url}',
            )
        else:
            log(
                f'SEQUENCE_STEP_PR_OPENED seq={seq_id} step={task_id} '
                f'pr={pr_url} no-op ({result.reason})',
            )
        return seq_id
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'sequence-step-pr-opened scan raised {type(e).__name__}: {e}; '
            f'swallowing — Mirror-review dispatch still fired',
            'WARN',
        )
    return None


def _maybe_reconcile_already_merged_build(data: dict[str, Any]) -> Optional[str]:
    """Terminal-reconcile a build outbox that opened no PR because its work
    already merged.

    Fires from the `phase=build` branch of `process_outbox` when
    `_extract_pr_url_from_build_result` finds no `PR opened:` line. If the build
    result narrates an already-merged / no-delta outcome and names a single PR
    that `gh` confirms is MERGED, signal the sequence step merged — reusing
    `_signal_sequence_step_merged` (matched by step_id == task_id), which flips
    the step `dispatched → merged`, records the PR + merge time, removes it from
    `current_steps`, and fires the sequence-complete signal if it was the last
    step. The step never strands; the 4h stall backstop never fires.

    Three gates + gh-truth (so prose alone never flips a step against the wrong
    PR): (1) the build EXITED CLEANLY (exit_code == 0) — an honest no-delta
    refusal is a successful turn, while a crash / genuine failure exits non-zero;
    (2) a named PR from `ssh.parse_already_merged_pr_ref` — which PREFERS Forge's
    canonical structured contract line `NO PR — already merged: #<N>` (durable
    against narration rewording) and falls back to the prose cue + single-PR
    heuristic for results that pre-date the contract; (3) `gh` confirms that PR is
    MERGED. A build that misses any gate returns None and the caller falls through
    to the default Beacon notify, leaving the stall backstop to escalate a real
    failure.
    Erring toward None is the safe direction — it falls back to today's behavior.

    Best-effort: any missing field / gh failure / sequence accident returns None
    and the build outbox routes exactly as before. Returns the reconciled seq_id
    or None."""
    task_id = data.get('task_id')
    target_repo = data.get('target_repo')
    if not isinstance(task_id, str) or not task_id:
        return None
    if not isinstance(target_repo, str) or not target_repo:
        return None
    # A genuine build crash/failure exits non-zero; only a clean turn (Forge
    # deciding NOT to fabricate a PR) is an already-merged refusal. Gating here
    # keeps a failing build that merely mentions a merged PR from being flipped.
    if data.get('exit_code') != 0:
        return None
    pr_number = ssh.parse_already_merged_pr_ref(data.get('result', ''))
    if pr_number is None:
        return None
    repo_coords = ssh.qualify_repo(target_repo)
    info = ssh.gh_pr_merge_info(repo_coords, pr_number)
    if info is None:
        log(
            f'BUILD_ALREADY_MERGED task={task_id} pr=#{pr_number} '
            f'({repo_coords}) — build opened no PR and named an already-merged '
            f'PR, but gh did not confirm it MERGED; leaving to default routing.',
            'WARN',
        )
        return None
    pr_url, merged_at = info
    log(
        f'BUILD_ALREADY_MERGED task={task_id} pr=#{pr_number} ({repo_coords}) — '
        f'build opened no PR; work already shipped via {pr_url}. Reconciling the '
        f'sequence step to merged instead of stranding it.'
    )
    return _signal_sequence_step_merged(task_id, pr_url, merged_at)


def _sequence_cancelled(task_id: Optional[str]) -> bool:
    """True iff `task_id` belongs to a build sequence that has been ABORTED
    (board-abort-dispatched-build): `apply_cancel` sets the sequence
    `status: 'failed'` and appends an `audit_log` entry `event == 'cancelled'`.
    Used to block auto-merge of any PR from an aborted build — the guarantee
    that "once aborted, nothing from that build lands on main".

    **FAIL-OPEN.** Any uncertainty — no task_id, no sequences dir, an
    unreadable/malformed file, or no sequence claiming this step — returns
    False, so this check NEVER blocks a legitimate merge; it only ever ADDS a
    skip for a CONFIRMED cancellation. Mirrors `_signal_sequence_step_merged`'s
    scan convention (match a step by `step_id == task_id`) rather than parsing
    the task_id string, so it stays correct if the id format changes.
    """
    if not isinstance(task_id, str) or not task_id:
        return False
    try:
        seq_dir = AGENTS_ROOT / 'blackboard' / 'build-sequences'
        if not seq_dir.is_dir():
            return False
        for seq_path in sorted(seq_dir.glob('*.json')):
            if seq_path.suffix != '.json':
                continue
            try:
                seq = json.loads(seq_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue  # unreadable sibling — fail-open, keep scanning
            if not isinstance(seq, dict):
                continue
            steps = seq.get('steps') or []
            if not any(
                isinstance(s, dict) and s.get('step_id') == task_id
                for s in steps
            ):
                continue
            # Found the sequence that owns this step. It is "cancelled" only
            # under apply_cancel's exact contract: status failed + the audit
            # event (a `failed` from a build error is NOT a cancel).
            if seq.get('status') != 'failed':
                return False
            audit = seq.get('audit_log') or []
            return any(
                isinstance(e, dict) and e.get('event') == 'cancelled'
                for e in audit
            )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge: fail-open
        log(
            f'sequence-cancelled-check raised {type(e).__name__}: {e}; '
            f'fail-open (allowing merge)',
            'WARN',
        )
    return False


# ============================================================================
# projects-v3 P4 Contract A (p4-complete-signal) — one-time completion signal
# ============================================================================

def _sequence_complete_gh_veto(seq: dict[str, Any]) -> Optional[str]:
    """Belt-and-suspenders gh check for a `complete` sequence: return a short
    veto reason if any step's PR is observably NOT merged on GitHub, else None.

    The completion signal trusts the chain_events `auto_merge` signal (which is
    what flipped every step to `merged` in the first place). This is the second
    belt: before DMing Larry "everything shipped", confirm GitHub agrees. It is
    VETO-ONLY — only an authoritative `OPEN` or `CLOSED` state vetoes. A `None`
    from `_gh_pr_state` (transport error / rate-limit / gh missing), a `MERGED`,
    or an unparseable pr_url all PASS. Rationale (spec § 5): the detect site is
    the merge chokepoint, not a periodic poll, so transient gh flakiness must
    not permanently strand the completion DM — at worst we trust the auto_merge
    signal we already acted on.

    Returns the veto reason string (for the log line) or None to proceed.
    """
    for step in seq.get('steps') or []:
        if not isinstance(step, dict):
            continue
        pr_url = step.get('pr_url')
        parsed = _parse_pr_url(pr_url) if pr_url else None
        if parsed is None:
            continue
        repo_coords, pr_number = parsed
        state = _gh_pr_state(repo_coords, pr_number)
        if state in ('OPEN', 'CLOSED'):
            return (
                f'step `{step.get("step_id")}` pr={pr_url} gh-state={state} '
                f'(expected MERGED)'
            )
    return None


def _finish_step_slug(command: str) -> str:
    """Stable, filesystem/id-safe slug from a post_merge command — used to build
    a deterministic approval task_id so a re-detect never double-proposes the
    same gated finish-step."""
    safe = ''.join(c if c.isalnum() else '-' for c in command).strip('-')
    return (safe[:48] or 'step').lower()


def _propose_gated_finish_step(seq_id: str, kind: str, command: str) -> str:
    """Register one risky post_merge finish-step on the human-approval-gate as a
    one-tap (Contract B). A `restart` / non-fail-safe `run` step is NEVER
    executed here — it's proposed so Larry taps Approve; the approve-envelope
    routes the execution prompt to forge (the existing gate's job).

    Returns a short note (the approval task_id) for the completion DM. Dedup by
    a stable task_id so a re-tick / crash-resume never double-proposes. This is
    invoked from inside `ssh.execute_post_merge`, which wraps it — but we also
    keep it self-contained and best-effort so a propose failure surfaces as a
    note rather than blocking the build.
    """
    task_id = f'postmerge-{seq_id}-{kind}-{_finish_step_slug(command)}'
    existing = approval.find_by_id_any_state(task_id)
    if existing is not None:
        return f'awaiting tap (already proposed: {task_id})'
    verb = 'restart service' if kind == 'restart' else 'run cleanup'
    summary = f'Build `{seq_id}` finished — approve to {verb}: `{command}`'
    prompt = (
        f'Post-merge finish-step for completed build sequence `{seq_id}`.\n'
        f'Approve to {verb}:\n    {command}\n\n'
        f'This step is human-gated because it is a `{kind}` (not a fail-safe '
        f'idempotent auto-run). On approve, execute exactly that command in '
        f'the deploy context and report the result back to Larry.'
    )
    payload = {
        'task_id': task_id,
        'summary': summary,
        'target_agent': 'forge',
        'target_repo': 'ourliberty-agent-core',
        'prompt': prompt,
        'kind': 'post_merge_finish_step',
        'seq_id': seq_id,
    }
    chat = _primary_chat_id()
    approval.add_pending(payload, chat_id=chat if chat is not None else 0)
    chain_event_emit.emit_event(
        **approval.build_approval_request_chain_event(payload),
    )
    if chat is not None:
        larry_alerts.append_approval_request(
            chat_id=chat,
            approval_id=task_id,
            body=summary,
            source='outbox-notifier',
        )
    return f'awaiting tap ({task_id})'


def _render_sequence_complete_dm(
    seq: dict[str, Any],
    report: Optional[Any] = None,
    closeout: Optional[dict] = None,
) -> str:
    """Plain-language completion DM body for Larry: what the build was, the PRs
    that shipped, and a one-line summary. When a post_merge `report` is given
    (Contract C), append the verified go-live result: what auto-ran, what was
    verified, and what's awaiting a one-tap. A failed verify check is surfaced
    loudly at the top (blocked-on-you doorbell). When `closeout` outputs are
    given (p4-closeout-outputs), append the phase-closeout handoff: 'Phase X
    done: [summary]. Next up: Phase Y — ready for you to brainstorm', any
    decision-needed flags, and a count of follow-ups dropped into the funnel's
    Suggested lane. No markdown chrome beyond the simple bullets the alert DM
    renderer already handles.
    """
    seq_id = seq.get('seq_id') or '?'
    label = seq.get('label') or seq.get('title') or seq_id
    steps = [s for s in (seq.get('steps') or []) if isinstance(s, dict)]
    n = len(steps)
    lines = [
        f'✅ Build complete: {label}',
        f'All {n} step{"s" if n != 1 else ""} merged. PRs that shipped:',
    ]
    for step in steps:
        step_id = step.get('step_id') or '?'
        pr_url = step.get('pr_url') or '(no pr_url recorded)'
        lines.append(f'  • {step_id}: {pr_url}')
    summary = seq.get('summary') or seq.get('description')
    if summary:
        lines.append(f'Summary: {summary}')
    else:
        lines.append(
            f'Summary: the `{label}` build sequence finished — '
            f'all {n} step{"s" if n != 1 else ""} are merged to main.'
        )
    if report is not None and getattr(report, 'has_steps', False):
        _append_post_merge_report_lines(lines, report)
    if closeout:
        _append_closeout_lines(lines, closeout)
    return '\n'.join(lines)


def _append_closeout_lines(lines: list[str], closeout: dict) -> None:
    """Append the p4-closeout-outputs handoff to the completion DM body: the
    'Phase X done: [summary]' headline, the 'Next up: Phase Y — ready for you to
    brainstorm' next-phase handoff, a loud '⚠️ Needs your call' block when the
    closeout flagged a decision (done-gate missed / build diverged from spec / a
    risky follow-up), and a count of the loose ends dropped into the funnel's
    Suggested lane. All fields are optional/defensive — a partial outputs dict
    degrades to just the lines it can render."""
    phase_title = (closeout.get('phase_title') or '').strip()
    summary = (closeout.get('summary') or '').strip()
    next_title = (closeout.get('next_phase_title') or '').strip()
    flags = [f for f in (closeout.get('flags') or []) if str(f).strip()]
    queued = [q for q in (closeout.get('follow_ups_queued') or []) if q]

    if not phase_title and not summary and not next_title and not flags \
            and not queued:
        return
    lines.append('')
    head = f'Phase {phase_title} done' if phase_title else 'Phase done'
    if summary:
        head = f'{head}: {summary}'
    lines.append(head)
    if next_title:
        lines.append(
            f'Next up: {next_title} — ready for you to brainstorm.'
        )
    if flags:
        lines.append('')
        lines.append('⚠️ Needs your call:')
        for flag in flags:
            lines.append(f'  • {flag}')
    if queued:
        n = len(queued)
        lines.append('')
        lines.append(
            f'Dropped {n} follow-up{"s" if n != 1 else ""} into the funnel’s '
            f'Suggested lane for you to triage.'
        )


def _append_post_merge_report_lines(lines: list[str], report: Any) -> None:
    """Append the Contract C go-live sections to the completion DM body."""
    auto = report.auto_results
    verify = [r for r in auto if r.kind == 'verify']
    ran = [r for r in auto if r.kind != 'verify']
    gated = report.gated_results
    failures = report.verify_failures
    if failures:
        lines.append('')
        lines.append(
            f'⚠️ {len(failures)} go-live check(s) FAILED — needs your eyes:'
        )
        for r in failures:
            lines.append(f'  • {r.command} → {r.detail}')
    if ran:
        lines.append('')
        lines.append('Auto-ran:')
        for r in ran:
            mark = '✓' if r.ok else '✗'
            lines.append(f'  {mark} {r.command} → {r.detail}')
    if verify:
        lines.append('')
        lines.append('Verified:')
        for r in verify:
            mark = '✓' if r.ok else '✗'
            lines.append(f'  {mark} {r.command} → {r.detail}')
    if gated:
        lines.append('')
        lines.append('Awaiting your tap (one-tap approval):')
        for r in gated:
            lines.append(f'  • [{r.kind}] {r.command} — {r.detail}')


def _emit_sequence_complete_chain_event(seq: dict[str, Any]) -> None:
    """Push-emit the one `sequence_complete` chain event. Best-effort +
    daemon-never-wedge — emit_event logs WARN and returns False on any
    Supabase failure; nothing downstream depends on the row landing.
    """
    seq_id = seq.get('seq_id') or '?'
    steps = [s for s in (seq.get('steps') or []) if isinstance(s, dict)]
    pr_urls = [s.get('pr_url') for s in steps if s.get('pr_url')]
    payload = {
        'agent': 'build_sequence_advancer',
        'seq_id': seq_id,
        'label': seq.get('label') or seq.get('title') or seq_id,
        'spec_doc': seq.get('spec_doc'),
        'pr_urls': pr_urls,
        'steps': [s.get('step_id') for s in steps],
        'completed_at': ces.datetime.now(ces.timezone.utc).isoformat(),
    }
    try:
        chain_event_emit.emit_event(
            event_type='sequence_complete',
            agent='build_sequence_advancer',
            task_id=seq_id,
            payload=payload,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'sequence_complete chain_event emit raised unexpectedly for '
            f'seq {seq_id!r}: {type(e).__name__}: {e}',
            'WARN',
        )


def _stamp_phase_done_for_sequence(seq: dict) -> None:
    """p3f-status-writeback: stamp the just-completed build sequence's phase to
    ``done`` (the board reflects reality once the build sequence completes).

    Passes the whole sequence dict to ``stamp_done``, which resolves the phase via
    the shared ``sequence_ref``-then-``authored-by-launch-drain``-audit-ids policy
    — so the done-stamp lands even if the building-stamp never persisted the
    ``sequence_ref`` (e.g. an EROFS write failure in the advancer) — and pins the
    ref when missing so the downstream closeout resolves too. The writer is a
    NON-committer (heal_projects_store commits), idempotent + fail-safe; this
    wrapper adds a daemon-never-wedge guard. A non-launch sequence (no matching
    phase) is a silent no-op."""
    try:
        import projects_status_writeback as psw  # local import: optional dep
        if psw.stamp_done(seq=seq):
            log(f'phase status: stamped done for seq={seq.get("seq_id")}')
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'phase done-stamp for seq={seq.get("seq_id") if isinstance(seq, dict) else seq} '
            f'raised {type(e).__name__}: {e}; swallowing',
            'WARN',
        )


def _author_phase_closeout_for_sequence(seq: dict) -> Optional[dict]:
    """p4-closeout-author + p4-closeout-outputs: on SEQUENCE_COMPLETE, author the
    phase's closeout — a plain-language summary + structured schema (shipped /
    changed-vs-spec / learnings / cost / done-gate) — onto its card, tick the
    North Star tracker, and drop the loose ends it finds into the funnel's
    Suggested lane (source='closeout', deduped). Runs right after the done-stamp
    so the just-completed phase writes its own story. The author does only
    NON-committer writes (heal_projects_store commits the card, the ticked doc,
    and the drained funnel cards); this wrapper adds a daemon-never-wedge guard so
    closeout authoring can NEVER block or corrupt the completion signal.

    Returns the closeout OUTPUTS dict the completion DM renders (summary +
    next-phase handoff + needs-you flags + queued follow-ups), or None for a
    non-launch sequence (no matching phase) or any swallowed failure."""
    try:
        import projects_closeout_author as closeout  # local import: optional dep
        outputs = closeout.run_closeout_for_sequence(seq)
        if outputs:
            log(
                f'phase closeout: authored for seq={seq.get("seq_id")} '
                f'(follow_ups={len(outputs.get("follow_ups_queued") or [])}, '
                f'flags={len(outputs.get("flags") or [])})'
            )
        return outputs
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'phase closeout for seq={seq.get("seq_id")} raised '
            f'{type(e).__name__}: {e}; swallowing',
            'WARN',
        )
        return None


def _maybe_signal_sequence_complete(seq_id: str) -> None:
    """Emit the one-time completion signal (chain event + Larry DM) for a
    sequence that has just reached `complete`, exactly once.

    Flow: re-read the sequence fresh; bail unless status is `complete`; cheap
    pre-check the `sequence-complete-signaled` marker (skip a finished+already-
    signaled sequence without a gh round-trip); run the belt-and-suspenders gh
    veto; then ATOMICALLY claim the signal via `ssh.claim_completion_signal`
    (writes the marker before returning applied=True). Only the claim winner
    emits the chain event + DM, so a re-tick / crash-resume never double-DMs.

    Best-effort + daemon-never-wedge: any failure is logged and swallowed; the
    step-merged signal and the rest of the merge flow never depend on it. The
    DM goes via `larry_alerts.append_alert` (the doorbell) because a sequence
    has no reply-chat thread to post into — same sink the advancer uses.
    """
    try:
        seq, err = ssh._read_sequence(seq_id)
        if err is not None or not isinstance(seq, dict):
            return
        if seq.get('status') != 'complete':
            return
        # Cheap pre-filter: skip the gh veto + claim entirely if already signaled.
        if ssh.is_completion_signaled(seq):
            return
        veto = _sequence_complete_gh_veto(seq)
        if veto is not None:
            log(
                f'SEQUENCE_COMPLETE seq={seq_id} gh-veto: {veto}; '
                f'deferring completion signal (will retry on next merge tick)',
                'WARN',
            )
            return
        result = ssh.claim_completion_signal(seq_id, actor='notifier')
        if result.error:
            log(
                f'SEQUENCE_COMPLETE seq={seq_id} claim hard-error: '
                f'{result.reason}',
                'WARN',
            )
            return
        if not result.applied:
            # Lost the race / already signaled — exactly-once guard held.
            log(
                f'SEQUENCE_COMPLETE seq={seq_id} no-op ({result.reason})',
            )
            return
        # We won the claim — run the post_merge finish-steps (Contract B),
        # then emit the one chain event + the one verified go-live DM
        # (Contract C). execute_post_merge never raises and never mutates the
        # sequence, so a failing cleanup/verify reports loudly but can't block
        # or corrupt completion.
        report = ssh.execute_post_merge(
            seq, propose_gated=_propose_gated_finish_step,
        )
        # p3f-status-writeback: SEQUENCE_COMPLETE is the phase's done event.
        # Stamp the phase to `done` on disk (non-committer; heal_projects_store
        # commits), resolving it by the launch-drain audit ids on `seq` (robust
        # to a never-persisted sequence_ref). Idempotent (done->done no-op),
        # event-driven, fail-safe — a non-launch sequence has no matching phase
        # and this is a logged no-op.
        _stamp_phase_done_for_sequence(seq)
        # p4-closeout-author + p4-closeout-outputs: the just-done phase authors
        # its own closeout onto the card + ticks the North Star tracker, and
        # returns the OUTPUTS the completion DM renders (summary + next-phase
        # handoff + needs-you flags + queued follow-ups). Non-committer writes
        # (the healer commits). Fail-safe — never wedges the completion signal.
        closeout_outputs = _author_phase_closeout_for_sequence(seq)
        _emit_sequence_complete_chain_event(seq)
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=_render_sequence_complete_dm(
                seq, report=report, closeout=closeout_outputs),
            subject=f'sequence-complete:{seq_id}',
            route='escalate',
        )
        n = len([s for s in (seq.get('steps') or []) if isinstance(s, dict)])
        log(
            f'SEQUENCE_COMPLETE seq={seq_id} signaled (steps={n}) '
            f'event+DM emitted',
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'sequence-complete signal for seq={seq_id} raised '
            f'{type(e).__name__}: {e}; swallowing',
            'WARN',
        )


# ============================================================================
# stale-worktree-teardown-001 — event-driven worktree teardown at auto-merge
# ============================================================================
#
# When a PR auto-merges, the forge AND mirror worktrees that produced it are
# dead weight: the branch is deleted (gh pr merge --delete-branch), the task
# is terminal. The daily-now-hourly cleanup_stale_worktrees GC is the backstop
# for tasks that never merge (REJECT/ESCALATE/abandonment); this hook is the
# primary path that reaps merged-task worktrees within the same notifier cycle
# rather than waiting up to the next GC sweep.
#
# Lives here (not in Forge/Mirror self-removal) because an agent can't reliably
# `git worktree remove` the tree it's running inside, and the auto-merge
# chokepoint deterministically knows the task_id and resolves both worktree
# paths. Mirrors the _signal_sequence_step_merged side-effect pattern.


def _canonical_repo_for_coords(repo_coords: str) -> Optional[Path]:
    """Map a `owner/repo` coords string to its canonical filesystem Path.

    Reads the same top-level `repo_paths` block of config/agent-models.json
    that cleanup_stale_worktrees._load_canonical_repos consumes, so the two
    teardown mechanisms agree on where each repo lives. `repo_coords` is the
    `owner/repo` form `gh` uses; `repo_paths` keys are bare repo names, so we
    match on the segment after the last slash. Returns None when the repo is
    not configured (caller logs + skips).
    """
    if not repo_coords:
        return None
    try:
        cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    block = cfg.get('repo_paths') if isinstance(cfg, dict) else None
    if not isinstance(block, dict):
        return None
    repo_name = repo_coords.rsplit('/', 1)[-1]
    raw = block.get(repo_name)
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw)


def _active_task_stems() -> set[str]:
    """In-flight task stems from `AGENTS_ROOT/state/in-flight/*.json`.

    Mirrors cleanup_stale_worktrees.load_active_task_stems' stem-matching so
    teardown never removes a worktree that a live agent_runner subprocess is
    still using. Should never overlap with a just-merged task at merge time,
    but it's cheap insurance against removing an active tree.
    """
    stems: set[str] = set()
    in_flight_dir = AGENTS_ROOT / 'state' / 'in-flight'
    if not in_flight_dir.exists():
        return stems
    for f in in_flight_dir.glob('*.json'):
        try:
            entry = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        stem = entry.get('task_stem') or f.stem
        if stem:
            stems.add(stem)
    return stems


def _teardown_worktrees_for_task(task_id: str, repo_coords: str) -> None:
    """Remove the forge AND mirror worktrees for a just-merged task.

    Best-effort and never raises (the caller is the safety-critical merge
    chokepoint). Fired only on merged/already_merged outcomes. Skips either
    agent's worktree if the task_id still appears in the in-flight registry.
    """
    if not task_id:
        return
    canonical_repo = _canonical_repo_for_coords(repo_coords)
    if canonical_repo is None:
        log(
            f'AUTO_MERGE_WORKTREE_TEARDOWN task={task_id} skipped — no '
            f'canonical repo path for coords={repo_coords!r}',
            'WARN',
        )
        return
    active_stems = _active_task_stems()
    for agent_id in ('forge', 'mirror'):
        try:
            wt_path = worktree_manager.worktree_path_for(agent_id, task_id)
            if any(stem and stem in wt_path.name for stem in active_stems):
                log(
                    f'AUTO_MERGE_WORKTREE_TEARDOWN task={task_id} '
                    f'agent={agent_id} skipped — task still in-flight',
                )
                continue
            on_disk = wt_path.exists()
            registered = worktree_manager._is_worktree_registered(
                canonical_repo, wt_path,
            )
            if not on_disk and not registered:
                continue
            worktree_manager._remove_worktree(
                canonical_repo, wt_path, log_fn=log,
            )
            log(
                f'AUTO_MERGE_WORKTREE_TEARDOWN task={task_id} '
                f'agent={agent_id} path={wt_path} repo={canonical_repo}',
            )
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge
            log(
                f'AUTO_MERGE_WORKTREE_TEARDOWN task={task_id} '
                f'agent={agent_id} raised {type(e).__name__}: {e}; '
                f'swallowing — GC backstop will reap it',
                'WARN',
            )


# ============================================================================
# D3.5 5d-prime — AUTO_MERGE serializer (overlap-aware merge gating)
# ============================================================================
#
# Two gates inserted BEFORE `_auto_merge_pr`'s shell-out in the review-pass
# handler:
#
#   Gate 1 (serializer queue): If another open PR in the same repo touches
#   any of the same files, queue this PR's merge behind it. FIFO release
#   when the blocker resolves (merged OR closed without merge).
#
#   Gate 2 (mergeable status): If `gh pr view --json mergeable` returns
#   CONFLICTING, do NOT fire `gh pr merge` (guaranteed-to-fail); DM Larry
#   the rebase command. UNKNOWN defers one sweep tick then proceeds (let
#   git be the authority on the second attempt).
#
# Eliminates the 2026-05-26 incident class. The E1.3 healer
# (heal_pr_auto_merge.py) remains in place as the post-hoc safety net.


def _reset_auto_merge_queue_state() -> None:
    """Test helper — wipe the in-process serializer state between cases."""
    global _AUTO_MERGE_QUEUE_FAIL_CLOSED, _RELEASE_REGRESSION_GATE_FN_OVERRIDE
    _AUTO_MERGE_QUEUE_FAIL_CLOSED = False
    _RELEASE_REGRESSION_GATE_FN_OVERRIDE = None
    _WATCHDOG_DMED_PRS.clear()


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON to `path` atomically (temp file in same dir + rename).

    Same-directory tempfile guarantees the rename is atomic on POSIX
    filesystems. Caller ensures parent dir exists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _load_auto_merge_queue() -> list[dict[str, Any]]:
    """Read the serializer queue. Returns [] on missing file (cold start).

    Fail-closed on parse error: sets module-level
    `_AUTO_MERGE_QUEUE_FAIL_CLOSED = True`, DMs Larry once (broadcast
    `kind: alert` via larry_alerts.append_alert with severity='critical'
    so it reaches every authorized chat; the queue is daemon-state and
    Larry-direct review-pass DMs aren't reliable when the queue is
    corrupt). Once tripped, every subsequent AUTO_MERGE attempt refuses
    until daemon restart (operator fixes the file).
    """
    global _AUTO_MERGE_QUEUE_FAIL_CLOSED
    if _AUTO_MERGE_QUEUE_FAIL_CLOSED:
        return []
    if not AUTO_MERGE_QUEUE_FILE.exists():
        return []
    try:
        raw = AUTO_MERGE_QUEUE_FILE.read_text(encoding='utf-8')
        data = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        _AUTO_MERGE_QUEUE_FAIL_CLOSED = True
        log(
            f'AUTO_MERGE_QUEUE_CORRUPT at {AUTO_MERGE_QUEUE_FILE}: '
            f'{type(e).__name__}: {e}; refusing all subsequent '
            f'AUTO_MERGE attempts until daemon restart',
            'ERROR',
        )
        try:
            larry_alerts.append_alert(
                source='outbox-notifier',
                severity='critical',
                message=(
                    f'AUTO_MERGE queue file is corrupt at '
                    f'{AUTO_MERGE_QUEUE_FILE}: {type(e).__name__}: {e}. '
                    f'All subsequent auto-merges are HELD until you '
                    f'inspect/repair the file and restart the notifier. '
                    f'Recovery: see runbooks/auto-merge-queue.md.'
                ),
                subject='auto-merge-queue-corrupt',
                suggested_action=(
                    f'cat {AUTO_MERGE_QUEUE_FILE} ; '
                    f'mv {AUTO_MERGE_QUEUE_FILE} {AUTO_MERGE_QUEUE_FILE}.broken'
                ),
            )
        except Exception:  # noqa: BLE001 — daemon-never-wedge on DM failure
            pass
        return []
    if not isinstance(data, dict):
        # Treat structural surprises the same as a parse error — fail closed.
        _AUTO_MERGE_QUEUE_FAIL_CLOSED = True
        log(
            f'AUTO_MERGE_QUEUE_MALFORMED at {AUTO_MERGE_QUEUE_FILE}: '
            f'top-level is {type(data).__name__}, expected object',
            'ERROR',
        )
        return []
    queue = data.get('queue')
    if not isinstance(queue, list):
        return []
    return [entry for entry in queue if isinstance(entry, dict)]


def _save_auto_merge_queue(entries: list[dict[str, Any]]) -> None:
    """Atomically persist the queue. Caller passes the full list."""
    payload = {
        'version': AUTO_MERGE_QUEUE_VERSION,
        'queue': entries,
    }
    _atomic_write_json(AUTO_MERGE_QUEUE_FILE, payload)


def _queue_push(entry: dict[str, Any]) -> None:
    """Append `entry` to the queue (FIFO). Caller fills required fields."""
    entries = _load_auto_merge_queue()
    entries.append(entry)
    _save_auto_merge_queue(entries)


def _queue_remove_pr(pr_number: int, repo_coords: str) -> Optional[dict[str, Any]]:
    """Remove the queue entry matching (pr_number, repo). Returns the
    removed entry or None if not present.
    """
    entries = _load_auto_merge_queue()
    kept = []
    removed = None
    for e in entries:
        if (
            removed is None
            and e.get('pr_number') == pr_number
            and e.get('repo') == repo_coords
        ):
            removed = e
            continue
        kept.append(e)
    if removed is not None:
        _save_auto_merge_queue(kept)
    return removed


def _load_deep_review_held() -> list[dict[str, Any]]:
    """Read the deep-review-held records. Returns [] on missing/corrupt file.

    Mirrors `_load_auto_merge_queue`'s versioned-dict shape but is
    fail-OPEN on a parse error (returns []) rather than fail-closed: this
    file only SUPPRESSES redundant re-reviews, so losing it degrades to the
    prior (pre-fix) behavior — an extra review — never to a wrong merge. A
    corrupt file is logged once at WARN and treated as empty.
    """
    if not DEEP_REVIEW_HELD_FILE.exists():
        return []
    try:
        data = json.loads(DEEP_REVIEW_HELD_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log(
            f'DEEP_REVIEW_HELD_CORRUPT at {DEEP_REVIEW_HELD_FILE}: '
            f'{type(e).__name__}: {e}; treating as empty (re-review not '
            f'suppressed until the file is repaired)',
            'WARN',
        )
        return []
    if not isinstance(data, dict):
        return []
    held = data.get('held')
    if not isinstance(held, list):
        return []
    return [e for e in held if isinstance(e, dict)]


def _save_deep_review_held(entries: list[dict[str, Any]]) -> None:
    """Atomically persist the held records. Caller passes the full list."""
    payload = {
        'version': DEEP_REVIEW_HELD_VERSION,
        'held': entries,
    }
    _atomic_write_json(DEEP_REVIEW_HELD_FILE, payload)


def _find_deep_review_held(
    repo_coords: str, pr_number: int,
) -> Optional[dict[str, Any]]:
    """Return the held record for (repo, pr), or None if not held."""
    for e in _load_deep_review_held():
        if e.get('repo') == repo_coords and e.get('pr_number') == pr_number:
            return e
    return None


def _record_deep_review_held(
    repo_coords: str,
    pr_number: int,
    pr_url: str,
    task_id: str,
    head_sha: Optional[str],
) -> bool:
    """Record/refresh the held entry for (repo, pr) at `head_sha`.

    Returns True when this is the FIRST hold for this exact (repo, pr,
    head) — i.e. no prior entry, or a prior entry recorded a DIFFERENT
    head (a genuine new push that will legitimately re-hold). Returns
    False when an entry for this same head already exists (a repeat hold
    of the unchanged head). Callers gate the Larry-DM on the True return
    so a re-hold of the same head never re-notifies.
    """
    entries = _load_deep_review_held()
    kept: list[dict[str, Any]] = []
    prior: Optional[dict[str, Any]] = None
    for e in entries:
        if e.get('repo') == repo_coords and e.get('pr_number') == pr_number:
            prior = e
            continue
        kept.append(e)
    first_for_head = prior is None or prior.get('head_sha') != head_sha
    kept.append({
        'repo': repo_coords,
        'pr_number': pr_number,
        'pr_url': pr_url,
        'task_id': task_id,
        'head_sha': head_sha,
        'held_at': datetime.now(timezone.utc).isoformat(),
    })
    _save_deep_review_held(kept)
    return first_for_head


def _clear_deep_review_held(repo_coords: str, pr_number: int) -> bool:
    """Drop any held entry for (repo, pr). Returns True if one was removed."""
    entries = _load_deep_review_held()
    kept = [
        e for e in entries
        if not (e.get('repo') == repo_coords and e.get('pr_number') == pr_number)
    ]
    if len(kept) != len(entries):
        _save_deep_review_held(kept)
        return True
    return False


def _deep_review_hold_suppresses_dispatch(
    pr_url: Optional[str], current_head_sha: Optional[str],
) -> bool:
    """True iff a Mirror review for `pr_url` should be SKIPPED because the PR
    is parked in deep-review-hold at this SAME `current_head_sha`.

    The keystone suppression check shared by `_dispatch_mirror_review` and
    `_dispatch_mirror_review_rerun`. Self-healing, in priority order:

      1. No held entry → not held → allow (return False).
      2. PR no longer OPEN (merged/closed) → clear the stale entry, allow.
         (Verified with a live gh check so a since-merged PR never stays
         suppressed; fail-OPEN — an undeterminable state does NOT clear and
         does NOT suppress, so a transient gh hiccup can't strand a review.)
      3. Held head != current head → a genuine new push deserves re-review;
         clear the stale entry, allow. The deep-review gate will legitimately
         re-hold (and re-record) at merge time for the new head.
      4. Held head == current head → suppress (return True). Re-reviewing an
         unchanged head only re-PASSes and re-holds; that is the wasteful
         loop this fix breaks.

    A `current_head_sha` of None (caller couldn't resolve it) never
    suppresses — without a head to compare we cannot prove it's the same
    parked commit, so fail-OPEN and let the existing dedup handle it.
    """
    if not pr_url:
        return False
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        return False
    repo_coords, pr_number = parsed
    held = _find_deep_review_held(repo_coords, pr_number)
    if held is None:
        return False
    # Self-heal: a merged/closed PR gates nothing; drop the record and allow.
    if _gh_pr_is_open(repo_coords, pr_number) is False:
        _clear_deep_review_held(repo_coords, pr_number)
        log(
            f'deep-review-held entry cleared for {pr_url} '
            f'(PR no longer OPEN); review dispatch not suppressed',
            'INFO',
        )
        return False
    if not current_head_sha:
        return False
    # Self-heal: a new head is a genuine revision — clear + allow re-review.
    if held.get('head_sha') != current_head_sha:
        _clear_deep_review_held(repo_coords, pr_number)
        log(
            f'deep-review-held entry cleared for {pr_url} (head advanced '
            f'{held.get("head_sha")} -> {current_head_sha}); re-review allowed',
            'INFO',
        )
        return False
    return True


def _queue_update_entry(pr_number: int, repo_coords: str, updates: dict[str, Any]) -> None:
    """In-place merge `updates` onto the matching queue entry. No-op if
    the entry is absent (caller may have already removed it).
    """
    entries = _load_auto_merge_queue()
    changed = False
    for e in entries:
        if e.get('pr_number') == pr_number and e.get('repo') == repo_coords:
            e.update(updates)
            changed = True
            break
    if changed:
        _save_auto_merge_queue(entries)


def _gh_pr_changed_files(repo_coords: str, pr_number: int) -> Optional[list[str]]:
    """Fetch the file paths the PR changed via `gh pr view --json files`.

    Returns the path list on success, None on any error (gh missing,
    timeout, non-zero exit, parse fail). Callers treat None as
    "couldn't determine overlap — fail safe by NOT holding" since the
    spec's failure mode here is "miss an overlap" (the healer is the
    safety net), not "block forever on a gh outage".
    """
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number),
             '--repo', repo_coords, '--json', 'files'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'gh pr view {pr_number} ({repo_coords}) --json files failed: '
            f'{type(e).__name__}: {e}',
            'WARN',
        )
        return None
    if proc.returncode != 0:
        log(
            f'gh pr view {pr_number} ({repo_coords}) --json files exit='
            f'{proc.returncode}: {(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return None
    try:
        data = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return None
    files = data.get('files')
    if not isinstance(files, list):
        return None
    out = []
    for f in files:
        if isinstance(f, dict):
            path = f.get('path')
            if isinstance(path, str):
                out.append(path)
    return out


def _gh_pr_labels(repo_coords: str, pr_number: int) -> Optional[list[str]]:
    """Fetch the PR's label names via `gh pr view --json labels`.

    merge-gate-deep-review-hold. Returns the label-name list on success, None
    on any error (gh missing, timeout, non-zero exit, parse fail). Distinct
    from `[]` (PR genuinely has no labels): the deep-review gate treats None as
    "couldn't read labels" and, per conservative-fail doctrine, a file-critical
    PR with unreadable labels is HELD (stamp can't be confirmed).
    """
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number),
             '--repo', repo_coords, '--json', 'labels'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'gh pr view {pr_number} ({repo_coords}) --json labels failed: '
            f'{type(e).__name__}: {e}',
            'WARN',
        )
        return None
    if proc.returncode != 0:
        log(
            f'gh pr view {pr_number} ({repo_coords}) --json labels exit='
            f'{proc.returncode}: {(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return None
    try:
        data = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return None
    labels = data.get('labels')
    if not isinstance(labels, list):
        return None
    out = []
    for lbl in labels:
        if isinstance(lbl, dict):
            name = lbl.get('name')
            if isinstance(name, str):
                out.append(name)
    return out


def _deep_review_required(
    repo_coords: str,
    pr_number: int,
    changed_files: Optional[list[str]],
    labels: Optional[list[str]] = None,
) -> bool:
    """Return True (→ HOLD the auto-merge for a human `/code-review high`) when
    a PASS'd PR is a critical-path change that has NOT been deep-reviewed.

    merge-gate-deep-review-hold. Two OR'd triggers make a PR critical:
      (a) any changed file matches a glob from `_load_deep_review_paths()`
          (the durable default — no one has to remember to label);
      (b) the PR carries the `deep-review-required` label (a manual override
          for a risky change outside the fileset).
    A critical PR is HELD unless it carries the `deep-review-passed` stamp
    (Claude's pre-handoff `/code-review high` applies it). A stamped critical
    PR returns False → flows through Mirror to auto-merge normally; a
    non-critical PR always returns False.

    Conservative-fail (doctrine): if `labels` can't be read (gh failure →
    None), the label trigger and the stamp both read as ABSENT. So a
    FILE-critical PR with unreadable labels is still HELD (we can't confirm the
    stamp — a false hold costs one manual merge; a false merge is the whole
    thing this gate prevents). A PR that is critical ONLY via the manual label
    degrades to non-critical on a labels-fetch blip — the durable fileset
    trigger is the guarantee; the manual override is best-effort.

    The FILESET side fails closed too: `changed_files` reaches the gate as
    `_gh_pr_changed_files(...) or []`, so a files-fetch failure upstream is
    indistinguishable from a (never-real) empty PR. Gate 1 tolerates that
    fail-OPEN (misses an overlap), but this gate must NOT — a critical-path PR
    silently reading as "no files → not critical" would merge unreviewed on a
    gh blip. So an empty `changed_files` is re-fetched here; if the re-fetch
    also can't resolve the fileset, the PR is HELD unless already stamped.
    Mirror has already PASS'd in every case here.
    """
    if labels is None:
        labels = _gh_pr_labels(repo_coords, pr_number)
    # None (gh failure) → empty for trigger/stamp reads (conservative: absent).
    label_names = labels or []
    label_critical = _DEEP_REVIEW_REQUIRED_LABEL in label_names
    stamped = _DEEP_REVIEW_PASSED_LABEL in label_names

    files = changed_files
    if not files:
        # Empty is ambiguous (a mergeable PR always changes ≥1 file), so almost
        # always means the upstream files fetch failed and was coalesced to [].
        # Re-fetch to disambiguate; a confirmed-unresolvable fileset must fail
        # CLOSED (hold) rather than degrade the durable trigger to fail-open.
        files = _gh_pr_changed_files(repo_coords, pr_number)
        if files is None:
            log(
                f'DEEP_REVIEW files unresolved for pr={pr_number} '
                f'({repo_coords}); holding conservatively unless stamped',
                'WARN',
            )
            return not stamped

    paths = _load_deep_review_paths()
    file_critical = any(
        fnmatch.fnmatch(f, glob) for f in files for glob in paths
    )
    if not (file_critical or label_critical):
        return False
    return not stamped


def _gh_pr_mergeable_status(repo_coords: str, pr_number: int) -> str:
    """Return 'mergeable' / 'conflicting' / 'unknown'.

    Wraps `gh pr view --json mergeable,mergeStateStatus`. Maps GitHub's
    `mergeable` field (MERGEABLE / CONFLICTING / UNKNOWN). Returns
    'unknown' on timeout / parse error / unrecognized value — callers
    treat 'unknown' with defer-then-proceed semantics so transient API
    quirks don't stall the queue forever.
    """
    if _gh_backoff_skip('pr-mergeable-status'):
        return 'unknown'
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number),
             '--repo', repo_coords,
             '--json', 'mergeable,mergeStateStatus'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'gh pr view {pr_number} ({repo_coords}) --json mergeable '
            f'failed: {type(e).__name__}: {e}; treating as unknown',
            'WARN',
        )
        return 'unknown'
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        log(
            f'gh pr view {pr_number} ({repo_coords}) --json mergeable exit='
            f'{proc.returncode}: {(proc.stderr or "").strip()[:200]}; '
            f'treating as unknown',
            'WARN',
        )
        return 'unknown'
    try:
        data = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return 'unknown'
    raw = data.get('mergeable')
    if not isinstance(raw, str):
        return 'unknown'
    return _GH_MERGEABLE_TO_GATE_STATUS.get(raw.upper(), 'unknown')


# forge-post-open-mergeable-rebase-001 — bounded poll over `gh pr view --json
# mergeable`. GitHub computes mergeability asynchronously, so a PR queried
# immediately after `gh pr create` (or after a force-push) usually returns
# UNKNOWN for a few seconds before settling to MERGEABLE / CONFLICTING. The
# auto-rebase gate must distinguish a definitive CONFLICTING (dispatch a rebase)
# from a transient UNKNOWN (wait, then proceed). Cap the wait so a wedged GitHub
# can never block the notifier's poll loop indefinitely.
_MERGEABLE_POLL_MAX = 6        # ~6 attempts
_MERGEABLE_POLL_INTERVAL_S = 5.0  # ~5s apart → ~30s worst-case bounded wait


def _poll_pr_mergeable(
    repo_coords: str,
    pr_number: int,
    *,
    max_polls: int = _MERGEABLE_POLL_MAX,
    interval_s: float = _MERGEABLE_POLL_INTERVAL_S,
    sleep=time.sleep,
) -> str:
    """Poll `_gh_pr_mergeable_status` past GitHub's async UNKNOWN.

    Returns 'mergeable' / 'conflicting' as soon as GitHub settles, or 'unknown'
    if the poll budget exhausts while still UNKNOWN (or every attempt hit a
    transport error). Callers treat a settled 'conflicting' as the auto-rebase
    trigger; a still-'unknown' result is treated optimistically (proceed to
    Mirror — the final auto-merge gate re-checks mergeability and the
    `held_conflict` path remains the human-visible backstop), so a slow GitHub
    never strands a PR.

    `sleep` is injectable so tests exercise the poll/settle logic without real
    delay. No sleep after the final attempt.

    Honors the GH rate-limit backoff gate: if a window is open the loop returns
    'unknown' immediately rather than burning its poll budget (and sleeping
    ~30s) against an exhausted API — the underlying `_gh_pr_mergeable_status`
    would short-circuit to 'unknown' every attempt anyway."""
    status = 'unknown'
    attempts = max(1, max_polls)
    for attempt in range(attempts):
        if _gh_backoff_active():
            return status
        status = _gh_pr_mergeable_status(repo_coords, pr_number)
        if status in ('mergeable', 'conflicting'):
            return status
        if attempt < attempts - 1:
            sleep(interval_s)
    return status


def _gh_pr_merge_freshness(
    repo_coords: str, pr_number: int,
) -> Optional[dict[str, Any]]:
    """Fetch the live freshness fields for a held PR about to merge.

    fix-auto-merge-freshness-revalidation. One `gh pr view` carrying every
    field the release-path re-validation needs, so it doesn't fan out into
    multiple round-trips:

        {
          'mergeable':    'mergeable' | 'conflicting' | 'unknown',
          'merge_state':  <raw mergeStateStatus, e.g. 'CLEAN'/'BEHIND'/'DIRTY'>,
          'state':        <raw OPEN | MERGED | CLOSED terminal state>,
          'base_sha':     <current base-branch (main) tip OID>,
          'head_sha':     <PR head commit OID>,
        }

    `state` is the OPEN/MERGED/CLOSED terminal state. The release-path gate
    uses it to skip re-queuing a released PR that is ALREADY MERGED/CLOSED:
    such a PR permanently reports mergeable=UNKNOWN post-base-move, which
    would otherwise route into the UNKNOWN defer branch and loop forever.

    Returns None on any transport error / non-zero exit / parse failure —
    the caller treats None as "couldn't confirm freshness", which fails
    closed (the PR is NOT auto-merged on a stale approval we can't verify).
    `mergeable` reuses the same MERGEABLE/CONFLICTING/UNKNOWN → tri-state map
    as `_gh_pr_mergeable_status`; `base_sha`/`head_sha` are GitHub's
    baseRefOid/headRefOid (baseRefOid tracks main's tip, so comparing it to
    the SHA recorded when the PR was held tells us whether the base moved).
    """
    if _gh_backoff_skip('pr-merge-freshness'):
        return None
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'view', str(pr_number),
             '--repo', repo_coords,
             '--json', 'mergeable,mergeStateStatus,state,baseRefOid,headRefOid'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'gh pr view {pr_number} ({repo_coords}) freshness lookup '
            f'FAILED: {type(e).__name__}: {e}',
            'WARN',
        )
        return None
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        log(
            f'gh pr view {pr_number} ({repo_coords}) freshness lookup exit='
            f'{proc.returncode}: {(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return None
    try:
        data = json.loads(proc.stdout or '{}')
    except (ValueError, json.JSONDecodeError):
        return None
    raw_mergeable = data.get('mergeable')
    mergeable = (
        _GH_MERGEABLE_TO_GATE_STATUS.get(raw_mergeable.upper(), 'unknown')
        if isinstance(raw_mergeable, str) else 'unknown'
    )
    return {
        'mergeable': mergeable,
        'merge_state': data.get('mergeStateStatus'),
        'state': data.get('state'),
        'base_sha': data.get('baseRefOid'),
        'head_sha': data.get('headRefOid'),
    }


def _gh_open_prs_for_repo(repo_coords: str) -> list[dict[str, Any]]:
    """Return open PRs for `repo_coords` with number/createdAt/headRefName.

    Empty list on error. Used by `_find_overlap_blocker` to catch PRs
    that have already opened but haven't reached Mirror PASS yet — those
    can still merge before this one and create overlap conflicts.
    """
    if _gh_backoff_skip('open-prs-for-repo'):
        return []
    try:
        proc = subprocess.run(
            ['gh', 'pr', 'list', '--repo', repo_coords, '--state', 'open',
             '--json', 'number,createdAt,headRefName'],
            capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'gh pr list {repo_coords} --state open failed: '
            f'{type(e).__name__}: {e}',
            'WARN',
        )
        return []
    _gh_note_result(proc.returncode, proc.stderr)
    if proc.returncode != 0:
        log(
            f'gh pr list {repo_coords} --state open exit={proc.returncode}: '
            f'{(proc.stderr or "").strip()[:200]}',
            'WARN',
        )
        return []
    try:
        data = json.loads(proc.stdout or '[]')
    except (ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _gh_pr_is_open(repo_coords: str, pr_number: int) -> Optional[bool]:
    """True/False for OPEN-ness, or None on any error (treat as unknown,
    leave queue entry in place; sweep will retry next tick).

    Wraps `_gh_pr_state` (already exists for the auto-merge resume-safety
    recheck). 'OPEN' → True; 'MERGED'/'CLOSED' → False; anything else → None.
    """
    state = _gh_pr_state(repo_coords, pr_number)
    if state == 'OPEN':
        return True
    if state in ('MERGED', 'CLOSED'):
        return False
    return None


def _mirror_review_target_is_terminal(pr_url: Optional[str]) -> bool:
    """True only when the PR at `pr_url` is positively MERGED or CLOSED.

    The dispatch-time half of the merged/closed-review guard (used by
    `_dispatch_mirror_review` + `_dispatch_mirror_review_rerun`). Returns
    False — "proceed with the review" — for EVERY uncertain case: a
    missing/unparseable url, or any gh error that leaves the state unknown
    (`_gh_pr_is_open` → None). Fail-OPEN by construction so a transient gh
    failure never silently drops a legitimate review; only a positively
    observed terminal state short-circuits a dispatch."""
    if not isinstance(pr_url, str) or not pr_url:
        return False
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        return False
    repo_coords, pr_number = parsed
    return _gh_pr_is_open(repo_coords, pr_number) is False


def _review_revision_pr_is_merged(
    data: dict[str, Any], marker_decision: dict[str, Any],
) -> bool:
    """Result-time half of the merged/closed-PR Mirror-review guard (the #764
    race, 2026-06-30). True only when a REVIEW_REVISION's PR is positively
    MERGED/CLOSED on GitHub.

    Complements the dispatch-time `_mirror_review_target_is_terminal`: that one
    skips QUEUING a review for an already-terminal PR, but cannot help when a
    review was dispatched while the PR was still OPEN and only RAN after a
    desktop `merge_reviewed_pr.sh` merge (the queue-backed-up #764 shape) — by
    then the review exists and its REVIEW_REVISION is in hand. This is the guard
    for that window: query GitHub before escalating to Larry or dispatching a
    Forge revision.

    Resolves the PR from the envelope's `pr_url` (or the marker payload's), and
    — for a heal-rebuilt / off-chain envelope carrying none — best-effort derives
    it from chain_events via `backfill_pr_url`, the same path the revision
    dispatch uses. Repo-agnostic: the PR coordinates come from the URL, never a
    hardcoded repo, so it covers ourliberty-agent-core AND ourliberty-dashboard.

    Fail-OPEN by construction (delegates to `_mirror_review_target_is_terminal`,
    which returns False for a missing/unparseable url or any gh error): only a
    positively observed terminal state returns True, so a transient gh failure
    never silently drops a legitimate revision/escalation."""
    payload = marker_decision.get('payload') or {}
    pr_url = data.get('pr_url') or (
        payload.get('pr_url') if isinstance(payload, dict) else None
    )
    if not pr_url:
        task_id = data.get('task_id')
        if task_id:
            try:
                pr_url = backfill_pr_url(
                    task_id,
                    target_repo=data.get('target_repo'),
                    branch=data.get('branch'),
                )
            except Exception:  # noqa: BLE001 — backfill is best-effort; fail open
                pr_url = None
    return _mirror_review_target_is_terminal(pr_url)


def _find_overlap_blocker(
    self_pr_number: int,
    repo_coords: str,
    changed_files: list[str],
) -> Optional[int]:
    """Return the PR number that should block `self_pr_number`'s merge,
    or None if no overlap is in flight.

    Scans live open PRs via `gh pr list` and returns the
    LOWEST-createdAt PR whose changed_files intersect ours. Queued PRs
    are intentionally NOT considered as blockers — they're already
    waiting for THEIR blocker to resolve, so they cannot precede us in
    the merge order. Including them would create a cycle (PR-A and PR-B
    blocking each other) when A's gates run after B was queued behind A.

    Cross-repo isolation: a PR in repo A never blocks a PR in repo B —
    `repo` field comparison is strict.

    Empty/None changed_files means we can't detect overlap; return None
    to let the merge fire (the E1.3 healer is the post-hoc safety net).

    File changed_files lookup: prefers the queue entry's cached list
    (avoids redundant `gh pr view` calls on retries), falls back to
    `gh pr view --json files` for PRs we haven't seen yet.
    """
    if not changed_files:
        return None
    self_files = set(changed_files)

    queue_entries = _load_auto_merge_queue()
    queued_prs = {
        e.get('pr_number') for e in queue_entries
        if e.get('repo') == repo_coords
    }
    queued_files_cache: dict[int, list[str]] = {
        e['pr_number']: list(e.get('changed_files') or [])
        for e in queue_entries
        if e.get('repo') == repo_coords and isinstance(e.get('pr_number'), int)
    }

    candidates: list[tuple[str, int]] = []  # (createdAt, pr_number)
    for pr_info in _gh_open_prs_for_repo(repo_coords):
        pr = pr_info.get('number')
        if not isinstance(pr, int) or pr == self_pr_number:
            continue
        if pr in queued_prs:
            # Already waiting — won't merge ahead of us. Skip to avoid
            # the A↔B cycle when A's gates run after B was queued.
            continue
        files = queued_files_cache.get(pr)
        if files is None:
            files = _gh_pr_changed_files(repo_coords, pr)
        if not files:
            continue
        if self_files & set(files):
            candidates.append((str(pr_info.get('createdAt') or ''), pr))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][1]


def _format_overlap_files(files: Optional[list[str]], limit: int = 3) -> str:
    """Render an overlap-files list for the held_for_blocker DM body.

    Sorted + de-duped; truncated with a `+N more` tail when over `limit`
    so the body stays phone-readable.
    """
    if not files:
        return '(unknown)'
    uniq = sorted({f for f in files if isinstance(f, str)})
    if not uniq:
        return '(unknown)'
    if len(uniq) <= limit:
        return ', '.join(uniq)
    return ', '.join(uniq[:limit]) + f' +{len(uniq) - limit} more'


def _fire_review_pass_outcome_dm(
    entry: dict[str, Any],
    merge_result: dict[str, Any],
) -> None:
    """Queue-sweep-side closing DM for a review-pass.

    fix-review-pass-dm-await-merge-outcome (2026-05-26). Called from
    `_queue_release` and `_auto_merge_queue_sweep` Pass-1 (UNKNOWN
    retry) after `_attempt_auto_merge_with_gates` returns a final
    outcome for a queued PR — `process_outbox` is long gone for those
    paths, so the closing DM has to fire from the sweep side.

    Synthesizes a (data, decision) shape and routes through
    `_maybe_dm_larry` so the outcome-aware variant selection +
    suppression rules match the process_outbox path. The suppression
    in `_maybe_dm_larry` (deferred_unknown / held_conflict) still
    applies — the conflict path's canonical DM is
    `_dm_larry_rebase_needed`, and deferred_unknown waits for the next
    sweep tick.
    """
    data = {
        'task_id': entry.get('task_id') or 'unknown',
        'reply_chat_id': entry.get('reply_chat_id'),
    }
    decision = {
        'marker_type': 'review_pass',
        'intent': 'review-pass',
        'payload': {
            'task_id': entry.get('task_id') or 'unknown',
            'pr_url': entry.get('pr_url') or '?',
            'summary': entry.get('summary') or '(no summary)',
        },
        'merge_result': merge_result,
        'merge_outcome': merge_result.get('merge_outcome'),
        'intent_kwargs': {},
    }
    try:
        _maybe_dm_larry(data, decision)
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge on DM failure
        log(
            f'queue-sweep closing DM raised for pr={entry.get("pr_url")} '
            f'task={entry.get("task_id")}: {type(e).__name__}: {e}',
            'WARN',
        )


def _dm_larry_rebase_needed(
    pr_url: str,
    pr_number: int,
    repo_coords: str,
    task_id: str,
    chat_id: Optional[int],
    summary: str = '',
) -> None:
    """Queue the merge_conflict_manual_rebase DM. Uses append_notification
    when we have a reply_chat_id (closes the chain in the originating
    thread), append_alert as a fallback broadcast.

    fix-review-pass-dm-await-merge-outcome (2026-05-26): now carries the
    Mirror review summary in the body so Larry has context for the
    rebase ask without paging back to the PR. This is the canonical
    closing DM for the held_conflict outcome; `_maybe_dm_larry` is
    suppressed for held_conflict to avoid a duplicate.
    """
    rebase_cmd = (
        f'gh pr checkout {pr_number} --repo {repo_coords} && '
        f'git fetch origin && git rebase origin/main && '
        f'git push --force-with-lease'
    )
    summary_line = (
        f'Summary: {summary}\n' if summary else ''
    )
    body = (
        f'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        f'{summary_line}'
        f'Auto-merge BLOCKED: PR has merge conflicts with main.\n'
        f'Rebase manually: {rebase_cmd}'
    )
    if isinstance(chat_id, int):
        try:
            larry_alerts.append_notification(
                source='outbox-notifier',
                intent='merge_conflict_manual_rebase',
                message=body,
                chat_id=chat_id,
                task_id=task_id,
            )
            return
        except Exception:  # noqa: BLE001 — daemon-never-wedge on DM failure
            pass
    # No targeted chat available (queued-retry path from the sweep often
    # has no envelope context) — broadcast via append_alert.
    try:
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=body,
            subject=f'auto-merge-conflict:{repo_coords}:{pr_number}',
        )
    except Exception:  # noqa: BLE001 — daemon-never-wedge
        pass


def _dm_larry_deep_review_hold(
    pr_url: str,
    pr_number: int,
    repo_coords: str,
    task_id: str,
    chat_id: Optional[int],
    summary: str = '',
) -> None:
    """Closing DM for the `held_deep_review` outcome.

    merge-gate-deep-review-hold. A critical-path PR (approval/resolve fan-out
    or the trust/merge machinery itself) PASS'd Mirror but reached auto-merge
    WITHOUT the `deep-review-passed` stamp — meaning the pre-handoff
    `/code-review high` was skipped. We DID NOT merge; this DM routes Larry to
    run that deep review and merge via `merge_reviewed_pr.sh <PR>` (which
    stamps LOCAL_REVIEW_PASS so `heal_unreviewed_merge_detector` stays quiet).

    Canonical closing DM for this outcome; `_maybe_dm_larry` is suppressed for
    `held_deep_review` to avoid a duplicate (same pairing as the
    `held_conflict` / `_dm_larry_rebase_needed` seam). Uses
    `append_notification` when a reply chat is known (closes the chain in the
    originating thread), `append_alert` as a broadcast fallback. Best-effort +
    never raises into the caller (daemon-never-wedge).
    """
    merge_cmd = f'scripts/merge_reviewed_pr.sh {pr_number}'
    summary_line = f'Summary: {summary}\n' if summary else ''
    body = (
        f'Mirror approved PR {pr_url} on task `{task_id}`.\n'
        f'{summary_line}'
        f'Auto-merge HELD: this is a critical-path change (approval/merge '
        f'machinery) that reached merge WITHOUT a deep-review stamp — the '
        f'`/code-review high` step was skipped.\n'
        f'Run `/code-review high` on it, then merge: {merge_cmd}'
    )
    if isinstance(chat_id, int):
        try:
            larry_alerts.append_notification(
                source='outbox-notifier',
                intent='merge_held_deep_review',
                message=body,
                chat_id=chat_id,
                task_id=task_id,
            )
            return
        except Exception:  # noqa: BLE001 — daemon-never-wedge on DM failure
            pass
    try:
        # Explicit route='escalate' overrides the graduated default for this
        # source ('outbox-notifier' migrated to 'hold'), which the bot skips at
        # read-time. severity='warning' won't trip the critical-forces-escalate
        # guard, so without this the deep-review hold would silently not DM.
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=body,
            subject=f'auto-merge-deep-review-hold:{repo_coords}:{pr_number}',
            route='escalate',
        )
    except Exception:  # noqa: BLE001 — daemon-never-wedge
        pass


def _dm_larry_stale_revalidation(
    pr_url: str,
    pr_number: int,
    repo_coords: str,
    task_id: str,
    chat_id: Optional[int],
    detail: str,
    summary: str = '',
) -> None:
    """Closing DM for a held auto-merge that FAILED freshness re-validation.

    fix-auto-merge-freshness-revalidation. The PR was held behind a blocker
    that has since merged (main moved underneath the pre-hold approval), and
    the release-path re-validation against CURRENT main found a regression
    (or couldn't confirm freshness). The auto-merge was refused and the PR
    pulled from the queue. This DM routes Larry back to re-review/rebase
    rather than letting the stale approval merge a regression — the PR #455
    lesson.

    Canonical closing DM for the `held_stale_regression` outcome;
    `_maybe_dm_larry` is suppressed for that outcome to avoid a duplicate
    (mirrors the `held_conflict` / `_dm_larry_rebase_needed` pairing).
    `detail` is the human-readable reason ("regression: +N new failing
    tests" or "could not re-validate: <why>"). Best-effort + idempotent;
    never raises into the caller (daemon-never-wedge).
    """
    rebase_cmd = (
        f'gh pr checkout {pr_number} --repo {repo_coords} && '
        f'git fetch origin && git rebase origin/main && '
        f'git push --force-with-lease'
    )
    summary_line = f'Summary: {summary}\n' if summary else ''
    body = (
        f'Mirror approved PR {pr_url} on task `{task_id}`, but that approval '
        f'predates a base change (an overlapping PR merged while this one was '
        f'held).\n'
        f'{summary_line}'
        f'Auto-merge HELD: re-validation against current main failed — '
        f'{detail}.\n'
        f'Not auto-merging on the stale approval. Rebase + re-review before '
        f'merging: {rebase_cmd}'
    )
    if isinstance(chat_id, int):
        try:
            larry_alerts.append_notification(
                source='outbox-notifier',
                intent='auto_merge_stale_revalidation',
                message=body,
                chat_id=chat_id,
                task_id=task_id,
            )
            return
        except Exception:  # noqa: BLE001 — daemon-never-wedge on DM failure
            pass
    # No targeted chat (sweep-release path often has no envelope context) —
    # broadcast via append_alert so the held-stale PR is still surfaced.
    try:
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=body,
            subject=f'auto-merge-stale-revalidation:{repo_coords}:{pr_number}',
        )
    except Exception:  # noqa: BLE001 — daemon-never-wedge
        pass


def _dm_larry_queue_stale(entry: dict[str, Any]) -> None:
    """Watchdog DM: queue entry older than watchdog_dm_hours. One-shot
    per entry (gated on the on-disk `watchdog_dm_sent` flag).
    """
    pr_number = entry.get('pr_number')
    blocker = entry.get('blocker_pr_number')
    pr_url = entry.get('pr_url') or '?'
    task_id = entry.get('task_id') or '?'
    queued_at = entry.get('queued_at') or '?'
    chat_id = entry.get('reply_chat_id')
    hours = _load_auto_merge_watchdog_hours_from_config()
    body = (
        f'AUTO_MERGE queue entry stale: PR {pr_url} (task `{task_id}`) has '
        f'been HELD behind PR #{blocker} since {queued_at} '
        f'(>{hours}h). Manual review needed — either merge PR #{blocker} '
        f'or close the queued PR.'
    )
    if isinstance(chat_id, int):
        try:
            larry_alerts.append_notification(
                source='outbox-notifier',
                intent='auto_merge_queue_stale',
                message=body,
                chat_id=chat_id,
                task_id=task_id,
            )
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=body,
            subject=f'auto-merge-queue-stale:{entry.get("repo")}:{pr_number}',
        )
    except Exception:  # noqa: BLE001
        pass


def _run_release_regression_gate(
    repo_coords: str,
    pr_number: int,
    base_sha: Optional[str],
    head_sha: Optional[str],
) -> str:
    """Re-run the regression gate for a held PR against CURRENT main.

    fix-auto-merge-freshness-revalidation. Returns one of:
      - 'pass'  — head introduces no new test failures vs current main.
      - 'block' — head introduces ≥1 regression vs current main.
      - 'skip'  — the regression gate doesn't apply to this repo (no
        `scripts/tests` suite — test_regression_check runs `unittest
        discover -s scripts/tests`, so e.g. the TS dashboard can't be
        measured). Caller proceeds on the mergeable re-confirm alone rather
        than false-blocking every held release in a non-python repo.
      - 'error' — couldn't analyze (repo not configured/resolvable, SHA
        missing locally, runner timeout/crash). Treated as fail-closed by
        the caller (don't auto-merge on an approval we can't re-validate).

    Honors `_RELEASE_REGRESSION_GATE_FN_OVERRIDE` (test seam) so unit tests
    exercise the freshness guard without a real ~10-min `test_regression_
    check.py` run. In production it resolves the repo's local checkout via
    `_canonical_repo_for_coords`, best-effort fetches the PR head + base so
    the runner's `git rev-parse` can resolve them, then shells out to
    `test_regression_check.py --parent-sha <current-main> --head-sha
    <pr-head>` bounded by a config timeout (so a hung suite can't wedge the
    notifier poll loop — it trips the timeout and returns 'error').
    """
    override = _RELEASE_REGRESSION_GATE_FN_OVERRIDE
    if override is not None:
        try:
            verdict = override(repo_coords, pr_number, base_sha, head_sha)
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge
            log(
                f'release regression-gate override raised for '
                f'pr=#{pr_number} ({repo_coords}): {type(e).__name__}: {e}',
                'WARN',
            )
            return 'error'
        return (verdict if verdict in ('pass', 'block', 'skip', 'error')
                else 'error')

    if not base_sha or not head_sha:
        log(
            f'release regression-gate for pr=#{pr_number} ({repo_coords}): '
            f'missing base/head SHA (base={base_sha!r} head={head_sha!r}); '
            f'cannot re-validate',
            'WARN',
        )
        return 'error'
    repo_root = _canonical_repo_for_coords(repo_coords)
    if repo_root is None or not repo_root.exists():
        log(
            f'release regression-gate for pr=#{pr_number} ({repo_coords}): '
            f'no local checkout configured (repo_paths); cannot re-validate',
            'WARN',
        )
        return 'error'
    # test_regression_check discovers `scripts/tests`; a repo without that
    # suite (e.g. the TS dashboard) can't be measured by this gate. Skip
    # rather than fail-closed — false-blocking every held release in a
    # non-python repo would be pure toil; the mergeable re-confirm still ran.
    if not (repo_root / 'scripts' / 'tests').is_dir():
        log(
            f'release regression-gate for pr=#{pr_number} ({repo_coords}): '
            f'no scripts/tests suite in {repo_root}; regression re-check N/A '
            f'(proceeding on mergeable re-confirm)',
        )
        return 'skip'

    # Best-effort: ensure the PR head + current base objects are present
    # locally so test_regression_check's `git rev-parse` resolves them. The
    # notifier's clone auto-pulls main, but a forge/* PR branch may not be
    # fetched; refs/pull/<N>/head is GitHub's stable PR-head ref. Failures
    # here are swallowed — a still-missing SHA surfaces as 'error' below.
    for fetch_args in (['origin', 'main'], ['origin', f'pull/{pr_number}/head']):
        try:
            subprocess.run(
                ['git', '-C', str(repo_root), 'fetch', '--quiet', *fetch_args],
                capture_output=True, text=True, timeout=_AUTO_MERGE_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    script = Path(__file__).resolve().parent / 'test_regression_check.py'
    timeout_per_sha = _load_release_regression_timeout_per_sha_s_from_config()
    # test_regression_check runs the two SHAs sequentially; bound the whole
    # shell-out at ~2x per-SHA plus worktree-setup slack.
    total_timeout = timeout_per_sha * 2 + 120
    try:
        proc = subprocess.run(
            [sys.executable, str(script),
             '--parent-sha', base_sha,
             '--head-sha', head_sha,
             '--repo-root', str(repo_root),
             '--timeout-per-sha', str(timeout_per_sha),
             '--output', 'json'],
            capture_output=True, text=True, timeout=total_timeout,
        )
    except subprocess.TimeoutExpired:
        log(
            f'release regression-gate for pr=#{pr_number} ({repo_coords}) '
            f'TIMED OUT after {total_timeout}s; failing closed (held)',
            'WARN',
        )
        return 'error'
    except (FileNotFoundError, OSError) as e:
        log(
            f'release regression-gate for pr=#{pr_number} ({repo_coords}) '
            f'could not run: {type(e).__name__}: {e}; failing closed (held)',
            'WARN',
        )
        return 'error'
    if proc.returncode == 0:
        return 'pass'
    if proc.returncode == 1:
        return 'block'
    log(
        f'release regression-gate for pr=#{pr_number} ({repo_coords}) '
        f'analysis failed (exit {proc.returncode}): '
        f'{(proc.stderr or "").strip()[:200]}; failing closed (held)',
        'WARN',
    )
    return 'error'


def _revalidate_held_merge_before_fire(
    pr_url: str,
    repo_coords: str,
    pr_number: int,
    task_id: str,
    summary: str,
    chat_id: Optional[int],
    changed_files: Optional[list[str]],
    release_entry: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Freshness gate for a held auto-merge whose hold just cleared.

    fix-auto-merge-freshness-revalidation. Returns None when the PR is
    still safe to merge on its existing approval (caller proceeds to fire
    `_auto_merge_pr`). Returns a non-merge merge_result dict when the merge
    must NOT fire — the approval went stale and the PR now conflicts with /
    regresses against / can't be re-validated against CURRENT main. The
    caller returns that dict instead of merging.

    `release_entry` is the ORIGINAL queue entry being released (its
    `approved_base_sha`, `blocker_pr_number`, `queued_at`, `watchdog_dm_sent`
    are read here and on the defer re-queue so the stale-queue watchdog clock
    survives transient re-defers).

    Decision matrix (all checks against CURRENT main, NOT the pre-hold base):
      - freshness lookup fails (transient gh)        → DEFER (re-queue)
      - mergeable == CONFLICTING                     → BLOCK (held_conflict)
      - mergeable == UNKNOWN (GitHub recomputing —   → DEFER (re-queue)
        the exact post-base-move race)
      - base did NOT move since approval, OR the     → MERGE (return None)
        regression re-run is disabled by config
      - regression gate PASS                         → MERGE (return None)
      - regression gate BLOCK / ERROR                → BLOCK (held_stale_regression)

    DEFER re-queues the entry with its (now-merged) blocker preserved so the
    sweep's blocker-resolution pass re-releases and re-validates it next
    tick — transient signals retry instead of stranding the PR. A defer is
    only safe when the entry's blocker is known; otherwise it fails closed
    (BLOCK). BLOCK fires the canonical closing DM here and is suppressed in
    `_maybe_dm_larry` to avoid a duplicate (mirrors held_conflict). The
    fail-closed bias is deliberate: a false hold costs Larry a manual merge;
    a false pass is the PR #455 regression-on-main incident.
    """
    approved_base_sha = release_entry.get('approved_base_sha')
    fresh = _gh_pr_merge_freshness(repo_coords, pr_number)
    if fresh is None:
        # Transient gh failure — can't confirm anything. Retry next sweep.
        return _defer_held_revalidation(
            pr_url, repo_coords, pr_number, task_id, summary, chat_id,
            changed_files, release_entry,
            reason='freshness lookup failed (transient gh error)',
        )

    # fix-auto-merge-already-merged-skip: a released PR that is ALREADY
    # MERGED/CLOSED (the blocker merged, moved main under it, and GitHub
    # auto-merged it via base-move) permanently reports mergeable=UNKNOWN,
    # so it would fall into the UNKNOWN defer branch below and re-queue
    # behind the now-merged blocker forever (~5s sweep loop). It is already
    # resolved — remove it from the queue and stop. `_queue_release` already
    # `_queue_remove_pr`-ed this entry before invoking the gate, so returning
    # without re-queuing leaves it removed and breaks the loop. Do NOT call
    # `_defer_held_revalidation` here.
    terminal_state = fresh.get('state')
    if terminal_state in ('MERGED', 'CLOSED'):
        resolved = 'merged' if terminal_state == 'MERGED' else 'closed'
        log(
            f'AUTO_MERGE_SKIP_ALREADY_MERGED task={task_id} pr={pr_url} '
            f'(state={terminal_state}; released PR already {resolved}, '
            f'removing from queue — not deferring)',
        )
        return {
            'merge_outcome': 'release_already_merged',
            'merge_reason': f'released PR already {resolved}; '
                            'removed from queue',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    mergeable = fresh.get('mergeable')
    if mergeable == 'conflicting':
        _dm_larry_rebase_needed(
            pr_url, pr_number, repo_coords, task_id, chat_id, summary,
        )
        log(
            f'AUTO_MERGE_HELD_STALE_CONFLICT task={task_id} pr={pr_url} '
            f'(mergeable=CONFLICTING against current main on release; '
            f'DMed Larry rebase command, NOT merging stale approval)',
            'WARN',
        )
        return {
            'merge_outcome': 'held_conflict',
            'merge_reason': 'mergeable=CONFLICTING against current main; '
                            'manual rebase required',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }
    if mergeable == 'unknown':
        # GitHub hasn't recomputed mergeability since the base moved (the
        # ~11s-after-blocker-merge race from the incident). Don't strand or
        # merge blind — retry on the next sweep release pass.
        return _defer_held_revalidation(
            pr_url, repo_coords, pr_number, task_id, summary, chat_id,
            changed_files, release_entry,
            reason='mergeable=UNKNOWN (GitHub recomputing post-base-move)',
        )

    # mergeable == 'mergeable': no textual conflict. The semantic check is
    # the regression gate — but only run it when the base actually moved
    # since approval (else the pre-hold approval is still valid). An unknown
    # approval base is treated as "assume moved" (conservative).
    current_base = fresh.get('base_sha')
    base_moved = (not approved_base_sha) or (current_base != approved_base_sha)
    if not base_moved:
        log(
            f'AUTO_MERGE_RELEASE_FRESH task={task_id} pr={pr_url} '
            f'(base unchanged since approval @ {approved_base_sha[:12] if approved_base_sha else "?"}; '
            f'merging on still-valid approval)',
        )
        return None
    if not _load_revalidate_regression_on_release_from_config():
        log(
            f'AUTO_MERGE_RELEASE_REVALIDATE_DISABLED task={task_id} '
            f'pr={pr_url} (base moved but regression re-run disabled via '
            f'config; merging on mergeable re-confirm only)',
            'WARN',
        )
        return None

    verdict = _run_release_regression_gate(
        repo_coords, pr_number, current_base, fresh.get('head_sha'),
    )
    if verdict in ('pass', 'skip'):
        log(
            f'AUTO_MERGE_RELEASE_REVALIDATED task={task_id} pr={pr_url} '
            f'(regression gate {verdict} against current main @ '
            f'{current_base[:12] if current_base else "?"}; merging)',
        )
        return None
    if verdict == 'block':
        detail = 'a regression against current main (new failing tests)'
    else:  # 'error'
        detail = 'could not re-validate against current main (analysis failed)'
    _dm_larry_stale_revalidation(
        pr_url, pr_number, repo_coords, task_id, chat_id, detail, summary,
    )
    log(
        f'AUTO_MERGE_HELD_STALE_REGRESSION task={task_id} pr={pr_url} '
        f'verdict={verdict} (held approval went stale when base moved '
        f'{approved_base_sha} -> {current_base}; NOT merging — {detail})',
        'WARN',
    )
    return {
        'merge_outcome': 'held_stale_regression',
        'merge_reason': f'release re-validation failed: {detail}',
        'pr_number': pr_number,
        'repo_coords': repo_coords,
        'regression_detail': detail,
    }


def _defer_held_revalidation(
    pr_url: str,
    repo_coords: str,
    pr_number: int,
    task_id: str,
    summary: str,
    chat_id: Optional[int],
    changed_files: Optional[list[str]],
    release_entry: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Re-queue a held PR whose release-path re-validation hit a TRANSIENT
    signal, so the next sweep's blocker-resolution pass re-releases and
    re-validates it. Keeps the (now-merged) blocker on the entry so it
    routes through `_queue_release` (which re-validates) rather than the
    UNKNOWN-defer pass (which would bypass re-validation).

    CRITICAL: preserve the ORIGINAL `queued_at` + `watchdog_dm_sent` from
    `release_entry` across re-queues. The stale-queue watchdog (sweep Pass-3)
    ages off `queued_at`; resetting it every defer would reset the clock each
    sweep tick, so a PR stuck in a persistent-UNKNOWN / persistent-transient
    defer loop would NEVER trip the watchdog DM — a silent strand, the exact
    failure the watchdog exists to catch. With the original timestamp
    preserved, a genuinely-stuck release ages out and surfaces to Larry.

    Fails closed to held_stale_regression when the entry's blocker is
    unknown — without it there's no safe retry route, and merging blind
    would reopen the hole.
    """
    release_blocker_pr = release_entry.get('blocker_pr_number')
    if not isinstance(release_blocker_pr, int):
        detail = f'could not re-validate against current main ({reason})'
        _dm_larry_stale_revalidation(
            pr_url, pr_number, repo_coords, task_id, chat_id, detail, summary,
        )
        log(
            f'AUTO_MERGE_HELD_STALE_NORETRY task={task_id} pr={pr_url} '
            f'({reason}; no blocker to re-queue behind — failing closed, '
            f'NOT merging)',
            'WARN',
        )
        return {
            'merge_outcome': 'held_stale_regression',
            'merge_reason': f'release re-validation deferred with no retry '
                            f'route: {reason}',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
            'regression_detail': detail,
        }
    entry = {
        'pr_number': pr_number,
        'task_id': task_id,
        'repo': repo_coords,
        'pr_url': pr_url,
        'changed_files': list(changed_files or []),
        # Preserve the original queue timestamp + watchdog flag so the
        # stale-queue watchdog clock survives repeated transient re-defers.
        'queued_at': release_entry.get(
            'queued_at', datetime.now(timezone.utc).isoformat(),
        ),
        # Keep the merged blocker so sweep Pass-2 re-releases -> re-validates.
        'blocker_pr_number': release_blocker_pr,
        'watchdog_dm_sent': release_entry.get('watchdog_dm_sent', False),
        'unknown_attempts': 0,
        'reply_chat_id': chat_id,
        'summary': summary,
        'approved_base_sha': release_entry.get('approved_base_sha'),
    }
    _queue_push(entry)
    log(
        f'AUTO_MERGE_RELEASE_DEFERRED task={task_id} pr={pr_url} '
        f'({reason}; re-queued behind #{release_blocker_pr} for sweep retry)',
    )
    return {
        'merge_outcome': 'deferred_unknown',
        'merge_reason': f'release re-validation deferred: {reason}',
        'pr_number': pr_number,
        'repo_coords': repo_coords,
    }


# --- L4 guardian-fix scope gate (spec main-suite-green-guardian.md L4) --------
#
# The Main-Suite Green Guardian's fix PRs must touch ONLY `scripts/tests/**`.
# This is enforced MECHANICALLY (against the ACTUAL diff at the current head SHA,
# not the self-declared changed_files trust-policy matches), re-checked
# immediately pre-merge in the same merge-eligibility seam as the deep-review
# hold. A guardian-lane PR is identified by the ledger `fix_task_id` join — the
# task_id equals a ledger row's fix_task_id — NOT by a label. Fail-closed on any
# fetch failure (can't confirm the diff -> hold). A violation blocks the merge,
# stamps the ledger scope-violation (a hard graduation disqualifier), drives a
# Stage-0 downgrade (L10), and escalates.

_GUARDIAN_FIX_SCOPE_PREFIX = 'scripts/tests/'


def _guardian_fix_ledger_row(task_id: str) -> Optional[dict[str, Any]]:
    """Return the guardian ledger row whose `fix_task_id` == ``task_id`` (the L4
    identity join), or None if this PR is not a guardian-lane fix. Fail-safe: any
    ledger read error returns None (not a guardian fix -> gate doesn't apply)."""
    if not task_id:
        return None
    try:
        import suite_guardian_ledger as _sgl
        for row in _sgl.list_by_status(_sgl.OPEN) + _sgl.list_by_status(_sgl.RESOLVED):
            if row.get('fix_task_id') == task_id:
                return row
    except Exception:  # noqa: BLE001 — a ledger hiccup must not wedge the merge
        return None
    return None


def _guardian_completed_runs() -> int:
    """Best-effort read of the guardian's completed-run count from the registry
    `_meta` (the evidence-reset floor for an L10 downgrade). 0 on any error."""
    try:
        import main_suite_guardian as _msg
        reg = json.loads(_msg.default_registry_path().read_text('utf-8'))
        val = (reg.get('_meta') or {}).get('completed_runs')
        return int(val) if isinstance(val, int) else 0
    except Exception:  # noqa: BLE001
        return 0


def _l4_guardian_scope_gate(
    repo_coords: str,
    pr_number: int,
    task_id: str,
    pr_url: str,
    chat_id: Optional[int],
) -> Optional[dict[str, Any]]:
    """The L4 pre-merge scope gate. Returns a merge-outcome dict when the PR must
    be HELD (fail-closed fetch failure or a genuine scope violation), or None when
    the gate does not apply (not a guardian fix) or the diff is clean. Never
    raises."""
    row = _guardian_fix_ledger_row(task_id)
    if row is None:
        return None  # not a guardian-lane fix — gate does not apply
    test_id = row.get('test_id') or task_id

    # SHA-bind: resolve the CURRENT head, then read the diff at that head. A miss
    # on either fetch is fail-closed (we cannot confirm the diff is in-scope).
    head_sha = _gh_pr_head_sha(repo_coords, pr_number)
    if not head_sha:
        log(
            f'AUTO_MERGE_HELD_SCOPE_FAIL_CLOSED task={task_id} pr={pr_url} '
            f'reason=head-sha-unresolved (guardian fix; cannot bind diff to a '
            f'head SHA — holding) agent=forge',
            'WARN',
        )
        return {
            'merge_outcome': 'held_scope_fail_closed',
            'merge_reason': (
                'guardian fix: head SHA unresolved; cannot confirm scope — held'
            ),
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }
    changed = _gh_pr_changed_files(repo_coords, pr_number)
    if changed is None:
        log(
            f'AUTO_MERGE_HELD_SCOPE_FAIL_CLOSED task={task_id} pr={pr_url} '
            f'head={head_sha} reason=changed-files-unreadable (guardian fix; '
            f'holding) agent=forge',
            'WARN',
        )
        return {
            'merge_outcome': 'held_scope_fail_closed',
            'merge_reason': (
                'guardian fix: changed files unreadable; cannot confirm scope — held'
            ),
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    out_of_scope = [f for f in changed
                    if not f.startswith(_GUARDIAN_FIX_SCOPE_PREFIX)]
    if not out_of_scope:
        return None  # clean — every path under scripts/tests/**

    # Genuine scope violation. Block, stamp the ledger, downgrade (L10), escalate.
    log(
        f'AUTO_MERGE_HELD_SCOPE_VIOLATION task={task_id} pr={pr_url} '
        f'head={head_sha} out_of_scope={out_of_scope[:10]} (guardian fix touched '
        f'paths outside {_GUARDIAN_FIX_SCOPE_PREFIX}** — blocked, Stage-0 '
        f'downgrade) agent=forge',
        'WARN',
    )
    try:
        import suite_guardian_ledger as _sgl
        _sgl.mark_scope_violation(test_id)
    except Exception as e:  # noqa: BLE001
        log(f'L4 gate: ledger scope-violation stamp failed for {test_id}: '
            f'{type(e).__name__}: {e}', 'WARN')
    try:
        import suite_guardian_stage as _sgs
        _sgs.apply_downgrade(
            cause=_sgs.CAUSE_SCOPE_VIOLATION,
            current_stage=int(row.get('filed_stage') or 0),
            run_seq=_guardian_completed_runs(),
        )
    except Exception as e:  # noqa: BLE001
        log(f'L4 gate: Stage-0 downgrade failed for {test_id}: '
            f'{type(e).__name__}: {e}', 'WARN')
    try:
        import larry_alerts as _la
        _la.append_notification(
            'suite-guardian',
            'scope-violation',
            f'🛑 Guardian fix PR {pr_url} touched paths outside '
            f'{_GUARDIAN_FIX_SCOPE_PREFIX}** ({", ".join(out_of_scope[:5])}). '
            f'Merge blocked; guardian autonomy reset to Stage 0 (L10).',
            chat_id or 0,
            task_id=task_id,
        )
    except Exception as e:  # noqa: BLE001
        log(f'L4 gate: escalation DM failed: {type(e).__name__}: {e}', 'WARN')

    _queue_remove_pr(pr_number, repo_coords)
    return {
        'merge_outcome': 'held_scope_violation',
        'merge_reason': (
            f'guardian fix touched paths outside {_GUARDIAN_FIX_SCOPE_PREFIX}**; '
            f'merge blocked + Stage-0 downgrade (L4/L10)'
        ),
        'pr_number': pr_number,
        'repo_coords': repo_coords,
    }


def _attempt_auto_merge_with_gates(
    pr_url: str,
    repo_coords: str,
    pr_number: int,
    task_id: str,
    summary: str,
    chat_id: Optional[int],
    changed_files: Optional[list[str]],
    *,
    second_attempt_on_unknown: bool = False,
    revalidate_freshness: bool = False,
    release_entry: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run both serializer gates then (if both pass) fire `_auto_merge_pr`.

    Returns a merge_result dict with one of these outcome values:
      - 'merged' / 'already_merged' / 'failed'  (from _auto_merge_pr)
      - 'held_for_blocker'  (gate 1 hit, entry pushed to queue)
      - 'held_conflict'     (gate 2 hit, DM fired, NOT queued)
      - 'held_deep_review'  (critical-path change lacking a deep-review stamp;
        DM fired, NOT queued, NOT retried — merge-gate-deep-review-hold)
      - 'deferred_unknown'  (gate 2 = UNKNOWN, first attempt; queued for retry)
      - 'held_fail_closed'  (queue file corrupt; never call _auto_merge_pr)
      - 'held_stale_regression'  (release-path freshness gate hit: a held
        approval went stale when the base moved, and the PR now regresses
        against / can't be re-validated against current main; DM fired,
        NOT merged — fix-auto-merge-freshness-revalidation)

    `second_attempt_on_unknown=True` makes UNKNOWN proceed to the merge
    shell-out (per spec: "let git be the authority on the second attempt").

    `revalidate_freshness=True` (set by `_queue_release`) marks this as a
    HELD-merge release: the approving Mirror review predates the current
    main (the blocker merged underneath it). Before firing the merge on
    that stale approval, re-validate against CURRENT main — re-confirm
    mergeable and (when the base actually moved) re-run the regression
    gate. `release_entry` is the original queue entry being released — its
    `approved_base_sha` (base tip the approval was against), merged
    `blocker_pr_number`, and `queued_at`/`watchdog_dm_sent` (so a transient
    re-validation defer can re-queue for the next sweep's release pass
    WITHOUT resetting the stale-queue watchdog clock). The 2026-06-11 PR
    #455 incident is the motivating case.
    """
    # board-abort-dispatched-build: HIGHEST-PRIORITY gate. If this build's
    # sequence was aborted (apply_cancel set status:failed + audit cancelled),
    # NEVER merge its PR — the guarantee that an aborted build lands nothing on
    # main. Runs before the test-bypass and the fail-closed gate. Fail-open
    # (see _sequence_cancelled): only a CONFIRMED cancellation skips here, so a
    # transient read error can never block a legitimate merge. Backstops the
    # _queue_release + sweep-retry callers that don't pass through
    # _run_review_pass_auto_merge's own check.
    if _sequence_cancelled(task_id):
        log(
            f'AUTO_MERGE task={task_id} pr={pr_url} outcome=skipped '
            f'reason=sequence-cancelled (build aborted) agent=forge',
            'WARN',
        )
        return {
            'merge_outcome': 'skipped_sequence_cancelled',
            'merge_reason': (
                'build sequence was cancelled (aborted); auto-merge blocked'
            ),
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    # Test-bypass: existing D3.5 5d tests assert merge-outcome rendering
    # via _AUTO_MERGE_FN_OVERRIDE without mocking the gate's gh calls.
    # When the bypass is set, fire the merge directly (preserve old
    # contract). The bypass is OFF in production and OFF in serializer
    # tests (which mock subprocess.run end-to-end).
    if _AUTO_MERGE_SKIP_SERIALIZER_FOR_TEST:
        merge_fn = _AUTO_MERGE_FN_OVERRIDE or _auto_merge_pr
        try:
            bypass_result = merge_fn(pr_url, task_id)
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge
            bypass_result = {
                'merge_outcome': 'failed',
                'merge_reason': f'override raised: {type(e).__name__}: {e}',
                'pr_number': pr_number,
                'repo_coords': repo_coords,
            }
        # V6 hook also fires on the test-bypass path so the chokepoint
        # contract is uniform. The helper is a no-op when no sequences
        # directory exists (the case for the D3.5 5d render tests that
        # use this bypass), so existing tests are unaffected.
        if bypass_result.get('merge_outcome') in ('merged', 'already_merged'):
            _signal_sequence_step_merged(
                task_id=task_id,
                pr_url=pr_url,
                merged_at_iso=datetime.now(timezone.utc).isoformat(),
            )
            _teardown_worktrees_for_task(
                task_id=task_id, repo_coords=repo_coords,
            )
            _reconcile_no_session_decision_on_merge(task_id)
        return bypass_result

    # Fail-closed gate (highest priority — never merge when queue is corrupt).
    if _AUTO_MERGE_QUEUE_FAIL_CLOSED:
        log(
            f'AUTO_MERGE_HELD_FAIL_CLOSED task={task_id} pr={pr_url} '
            f'(queue corrupt; refusing all auto-merges until daemon restart)',
            'WARN',
        )
        return {
            'merge_outcome': 'held_fail_closed',
            'merge_reason': 'auto-merge queue file corrupt; daemon restart required',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    # Resolve changed_files (caller may have it from the envelope; else fetch).
    if not changed_files:
        changed_files = _gh_pr_changed_files(repo_coords, pr_number) or []

    # Gate 1 — serializer queue.
    blocker = _find_overlap_blocker(pr_number, repo_coords, changed_files)
    if blocker is not None:
        # Push (or update) the queue entry. Idempotent on (pr_number, repo).
        existing = _queue_remove_pr(pr_number, repo_coords)
        # fix-auto-merge-freshness-revalidation: snapshot the base-branch tip
        # the approval is effectively against, so the release path can later
        # tell whether main moved (blocker merged) and a regression re-check
        # is owed. Preserved across re-queues; one cheap gh call, only when a
        # PR is actually held (rare). None on lookup failure — the release
        # path treats "unknown approval base" as "assume the base moved".
        approved_base = (existing or {}).get('approved_base_sha')
        if approved_base is None:
            approved_base = (_gh_pr_merge_freshness(repo_coords, pr_number)
                             or {}).get('base_sha')
        entry = {
            'pr_number': pr_number,
            'task_id': task_id,
            'repo': repo_coords,
            'pr_url': pr_url,
            'changed_files': changed_files,
            'queued_at': (existing or {}).get(
                'queued_at',
                datetime.now(timezone.utc).isoformat(),
            ),
            'blocker_pr_number': blocker,
            'watchdog_dm_sent': (existing or {}).get('watchdog_dm_sent', False),
            'unknown_attempts': (existing or {}).get('unknown_attempts', 0),
            'reply_chat_id': chat_id,
            'summary': summary,
            'approved_base_sha': approved_base,
        }
        _queue_push(entry)
        log(
            f'AUTO_MERGE_HELD task={task_id} pr={pr_url} '
            f'blocker=#{blocker} (overlap on {sorted(set(changed_files))[:5]}'
            f'{"..." if len(changed_files) > 5 else ""})',
        )
        return {
            'merge_outcome': 'held_for_blocker',
            'merge_reason': f'queued behind PR #{blocker}',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
            'blocker_pr_number': blocker,
            'overlap_files': _format_overlap_files(changed_files),
        }

    # Gate 2 — mergeable status. SKIPPED on the release path
    # (revalidate_freshness=True): the freshness gate below re-confirms
    # mergeable against CURRENT main with base-movement awareness and fully
    # supersedes Gate 2 for held releases. Running Gate 2 here too would let
    # its UNKNOWN-defer branch (blocker_pr_number=None) re-queue the entry
    # onto the sweep's UNKNOWN-retry pass — which proceeds to merge WITHOUT
    # re-validation, reopening the stale-approval hole. fix-auto-merge-
    # freshness-revalidation.
    if not revalidate_freshness:
        status = _gh_pr_mergeable_status(repo_coords, pr_number)
        if status == 'conflicting':
            _queue_remove_pr(pr_number, repo_coords)  # clear if it was queued
            _dm_larry_rebase_needed(
                pr_url, pr_number, repo_coords, task_id, chat_id, summary,
            )
            log(
                f'AUTO_MERGE_SKIPPED_CONFLICTING task={task_id} pr={pr_url} '
                f'(mergeable=CONFLICTING; DMed Larry rebase command)',
                'WARN',
            )
            return {
                'merge_outcome': 'held_conflict',
                'merge_reason': 'mergeable=CONFLICTING; manual rebase required',
                'pr_number': pr_number,
                'repo_coords': repo_coords,
            }
        if status == 'unknown' and not second_attempt_on_unknown:
            # First UNKNOWN — queue with no blocker, increment attempts counter.
            existing = _queue_remove_pr(pr_number, repo_coords)
            entry = {
                'pr_number': pr_number,
                'task_id': task_id,
                'repo': repo_coords,
                'pr_url': pr_url,
                'changed_files': changed_files,
                'queued_at': (existing or {}).get(
                    'queued_at',
                    datetime.now(timezone.utc).isoformat(),
                ),
                # blocker_pr_number=None signals "deferred for UNKNOWN" to the
                # sweep; on next sweep, this entry retries with
                # second_attempt_on_unknown=True.
                'blocker_pr_number': None,
                'watchdog_dm_sent': (existing or {}).get('watchdog_dm_sent', False),
                'unknown_attempts': (existing or {}).get('unknown_attempts', 0) + 1,
                'reply_chat_id': chat_id,
                'summary': summary,
            }
            _queue_push(entry)
            log(
                f'AUTO_MERGE_DEFERRED_UNKNOWN task={task_id} pr={pr_url} '
                f'(mergeable=UNKNOWN; retry on next sweep)',
            )
            return {
                'merge_outcome': 'deferred_unknown',
                'merge_reason': 'mergeable=UNKNOWN; deferred one tick',
                'pr_number': pr_number,
                'repo_coords': repo_coords,
            }

    # fix-auto-merge-freshness-revalidation — held-merge freshness gate.
    # Gate 1 passed (no overlapping in-flight blocker), but if this fire is
    # the RELEASE of a held PR (its blocker merged, so main moved under a
    # now-stale approval), re-validate against CURRENT main before trusting
    # that approval. This is the release path's mergeable + regression
    # authority (Gate 2 was skipped above). A stale/conflicting/regressing
    # PR is NOT auto-merged; it's routed back to Larry (held_conflict /
    # held_stale_regression) or deferred for the next sweep (transient). The
    # first-attempt path leaves `revalidate_freshness` False (its approval is
    # fresh, Gate 2 already ran) and is unaffected.
    if revalidate_freshness and release_entry is not None:
        revalidation = _revalidate_held_merge_before_fire(
            pr_url=pr_url,
            repo_coords=repo_coords,
            pr_number=pr_number,
            task_id=task_id,
            summary=summary,
            chat_id=chat_id,
            changed_files=changed_files,
            release_entry=release_entry,
        )
        if revalidation is not None:
            # Block (DM already fired) or defer (entry re-queued) — either
            # way, do NOT fire the merge on the stale approval.
            return revalidation

    # L4 guardian-fix scope gate (spec main-suite-green-guardian.md L4). Sits in
    # the same merge-eligibility chokepoint as the deep-review hold below, just
    # before the merge fires, so every merge-fire caller re-checks the ACTUAL diff
    # at the current head SHA. Applies ONLY to guardian-lane fixes (ledger
    # fix_task_id join); a no-op for every other PR. Fail-closed, and a violation
    # blocks + downgrades + escalates inside the helper.
    _l4 = _l4_guardian_scope_gate(repo_coords, pr_number, task_id, pr_url, chat_id)
    if _l4 is not None:
        return _l4

    # Deep-review hold (merge-gate-deep-review-hold). Placed here — after every
    # mergeability/freshness gate and just before the merge fires — so it is the
    # single chokepoint every merge-fire caller passes through (first-attempt,
    # held-blocker release, second-UNKNOWN sweep-retry). A critical-path change
    # (approval/resolve fan-out or the trust/merge machinery itself) that PASS'd
    # Mirror but lacks a `deep-review-passed` stamp is HELD for a human
    # `/code-review high` + manual merge instead of auto-merging. The WARN log
    # deliberately AVOIDS `outcome=failed` so `heal_pr_auto_merge` (which retries
    # only on `outcome=failed`) leaves the held PR alone — it mirrors the
    # held_conflict seam exactly (surface to Larry, no auto-retry, no reap).
    if _deep_review_required(repo_coords, pr_number, changed_files):
        # Defensively clear any queue entry (mirrors the held_conflict seam at
        # gate 2). Both re-attempt callers (_queue_release, the UNKNOWN-retry
        # sweep) already remove the entry before invoking this fn, so today this
        # is a no-op on those paths — but it guarantees a held_deep_review PR
        # can never be left queued (which would re-fire the gate + DM every
        # sweep) regardless of caller, exactly as held_conflict does.
        _queue_remove_pr(pr_number, repo_coords)
        # Persist the held state (keyed by the reviewed head) so every
        # review-dispatch path can suppress a re-review of THIS unchanged head
        # — the wasteful loop (review -> PASS -> re-hold -> re-DM) this fix
        # closes. `_record_deep_review_held` returns True only on the FIRST
        # hold for (repo, pr, head): a repeat hold of the same head records the
        # refreshed entry but does NOT re-DM Larry. A genuine new push (a
        # different head) counts as a first hold — it re-notifies, as it should.
        held_head_sha = _gh_pr_head_sha(repo_coords, pr_number)
        first_hold_for_head = _record_deep_review_held(
            repo_coords, pr_number, pr_url, task_id, held_head_sha,
        )
        if first_hold_for_head:
            _dm_larry_deep_review_hold(
                pr_url, pr_number, repo_coords, task_id, chat_id, summary,
            )
        else:
            log(
                f'AUTO_MERGE_HELD_DEEP_REVIEW repeat hold for pr={pr_url} at '
                f'unchanged head={held_head_sha}; Larry already DMed for this '
                f'head — not re-notifying',
                'INFO',
            )
        log(
            f'AUTO_MERGE_HELD_DEEP_REVIEW task={task_id} pr={pr_url} '
            f'(critical-path change with no deep-review stamp; held for '
            f'/code-review high) agent=forge',
            'WARN',
        )
        return {
            'merge_outcome': 'held_deep_review',
            'merge_reason': (
                'critical-path change; held for /code-review high before merge'
            ),
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    # Both gates pass (or second UNKNOWN attempt) — fire the merge.
    _queue_remove_pr(pr_number, repo_coords)  # clear queue if retrying
    merge_fn = _AUTO_MERGE_FN_OVERRIDE or _auto_merge_pr
    try:
        result = merge_fn(pr_url, task_id)
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'AUTO_MERGE override raised on task {task_id}: '
            f'{type(e).__name__}: {e}; synthesizing failed outcome',
            'WARN',
        )
        result = {
            'merge_outcome': 'failed',
            'merge_reason': f'override raised: {type(e).__name__}: {e}',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    # V6 (orchestrator-rectification-v2): propagate the merge to any active
    # sequence whose step_id matches `task_id`. Centralized here so all
    # three callers of this chokepoint — the marker-routing block,
    # _queue_release (post-blocker), and _auto_merge_queue_sweep
    # (UNKNOWN-retry) — fire the hook exactly once per merge. Fired
    # before _queue_release so the merged step's sequence-state
    # propagation lands before any chained release work begins.
    if result.get('merge_outcome') in ('merged', 'already_merged'):
        _signal_sequence_step_merged(
            task_id=task_id,
            pr_url=pr_url,
            merged_at_iso=datetime.now(timezone.utc).isoformat(),
        )
        # stale-worktree-teardown-001: reap the forge + mirror worktrees the
        # instant the PR merges — the branch is gone and the task is terminal.
        # Best-effort; the hourly GC sweep is the backstop if this misses.
        _teardown_worktrees_for_task(task_id=task_id, repo_coords=repo_coords)
        # null-chat-escalation-reconcile: if a prior session-less Mirror
        # escalation left a pending decision approval for this PR, the merge
        # made it moot — resolve it to 'expired' so it leaves the doorbell.
        _reconcile_no_session_decision_on_merge(task_id)

    # Post-merge release: if this PR merged successfully, re-attempt every
    # queue entry that was blocked behind it.
    if result.get('merge_outcome') in ('merged', 'already_merged'):
        _queue_release(pr_number, repo_coords)
    return result


def _queue_release(merged_pr_number: int, repo_coords: str) -> None:
    """Re-attempt queue entries blocked behind `merged_pr_number`.

    Called immediately after a successful `_auto_merge_pr` (post-merge
    release pass) AND from the periodic sweep (catches external closes /
    manual merges). Each released entry re-runs both gates from scratch
    — if gate 2 fires on the retry, Larry sees the rebase DM.

    Entries are released one at a time in FIFO order. If a released
    entry hits gate 1 again (chained blocker A < B < C scenario), the
    re-queue is idempotent; the next release pass handles it.
    """
    entries = _load_auto_merge_queue()
    released_entries = [
        e for e in entries
        if e.get('blocker_pr_number') == merged_pr_number
        and e.get('repo') == repo_coords
    ]
    if not released_entries:
        return
    log(
        f'AUTO_MERGE_QUEUE_RELEASE blocker=#{merged_pr_number} '
        f'releasing {len(released_entries)} entr'
        f'{"y" if len(released_entries) == 1 else "ies"}',
    )
    for entry in released_entries:
        pr_number = entry.get('pr_number')
        repo = entry.get('repo')
        if not isinstance(pr_number, int) or not isinstance(repo, str):
            continue
        # Remove first so the gate re-check sees a clean queue.
        _queue_remove_pr(pr_number, repo)
        # fix-auto-merge-freshness-revalidation: this is a HELD-merge release
        # — the blocker (`merged_pr_number`) just merged, so main moved under
        # the pre-hold approval. Mark it for freshness re-validation and pass
        # the snapshotted approval base + the merged blocker so the gate can
        # detect base movement and (on a transient defer) re-queue for retry.
        result = _attempt_auto_merge_with_gates(
            pr_url=entry.get('pr_url') or '',
            repo_coords=repo,
            pr_number=pr_number,
            task_id=entry.get('task_id') or 'unknown',
            summary=entry.get('summary') or '',
            chat_id=entry.get('reply_chat_id'),
            changed_files=entry.get('changed_files') or [],
            second_attempt_on_unknown=False,
            revalidate_freshness=True,
            # The full original entry — carries approved_base_sha, the merged
            # blocker, and queued_at/watchdog_dm_sent so a transient re-validation
            # defer re-queues without resetting the stale-queue watchdog clock.
            release_entry=entry,
        )
        outcome = result.get('merge_outcome')
        log(
            f'AUTO_MERGE_QUEUE_RELEASED pr={entry.get("pr_url")} '
            f'task={entry.get("task_id")} outcome={outcome}',
        )
        # fix-review-pass-dm-await-merge-outcome (2026-05-26):
        # `process_outbox` is long gone for this entry — it produced the
        # first DM ("queued behind PR #Y") and archived its outbox. The
        # release-side DM is the final outcome (merged / already_merged /
        # failed / held_for_blocker on chained blocker). Skips for
        # deferred_unknown / held_conflict are handled in _maybe_dm_larry.
        _fire_review_pass_outcome_dm(entry, result)


def _auto_merge_queue_sweep() -> None:
    """Periodic sweep: handle UNKNOWN-defer retries, blocker-resolution
    detection, and watchdog DMs.

    Cheap when queue is empty (one stat call). When the queue has
    entries, makes O(N) `gh pr view` calls for blocker-state and
    UNKNOWN-retry mergeable checks. Called from the notifier main loop
    after the outbox + dead-letter scans.

    Three classes of action per entry:
      1. entry.blocker_pr_number is None AND unknown_attempts >= 1 →
         retry merge with second_attempt_on_unknown=True (one defer,
         then proceed per spec).
      2. entry.blocker_pr_number is set → check `gh pr view` state for
         that PR; if MERGED/CLOSED, re-attempt via _queue_release.
      3. entry.queued_at older than watchdog_dm_hours AND not yet
         DMed → DM Larry once, set watchdog_dm_sent=True.

    Fail-closed entries skip all retries (queue corrupt; nothing to do
    until restart).
    """
    if _AUTO_MERGE_QUEUE_FAIL_CLOSED:
        return
    entries = _load_auto_merge_queue()
    if not entries:
        return
    now = datetime.now(timezone.utc)
    watchdog_hours = _load_auto_merge_watchdog_hours_from_config()
    watchdog_threshold_sec = watchdog_hours * 3600

    # Pass 1: UNKNOWN-defer retries.
    for entry in list(entries):
        if (
            entry.get('blocker_pr_number') is None
            and (entry.get('unknown_attempts') or 0) >= 1
        ):
            pr_number = entry.get('pr_number')
            repo = entry.get('repo')
            if not isinstance(pr_number, int) or not isinstance(repo, str):
                continue
            _queue_remove_pr(pr_number, repo)
            result = _attempt_auto_merge_with_gates(
                pr_url=entry.get('pr_url') or '',
                repo_coords=repo,
                pr_number=pr_number,
                task_id=entry.get('task_id') or 'unknown',
                summary=entry.get('summary') or '',
                chat_id=entry.get('reply_chat_id'),
                changed_files=entry.get('changed_files') or [],
                second_attempt_on_unknown=True,
            )
            log(
                f'AUTO_MERGE_QUEUE_UNKNOWN_RETRY pr={entry.get("pr_url")} '
                f'task={entry.get("task_id")} '
                f'outcome={result.get("merge_outcome")}',
            )
            # fix-review-pass-dm-await-merge-outcome (2026-05-26):
            # the deferred path's process_outbox call suppressed the
            # closing DM (deferred_unknown is in the skip set); fire it
            # now with the real outcome. Skips inside _maybe_dm_larry
            # still apply for held_conflict (rebase DM handles it).
            _fire_review_pass_outcome_dm(entry, result)

    # Pass 2 + 3 re-read because Pass 1 mutated the queue.
    entries = _load_auto_merge_queue()
    for entry in list(entries):
        blocker = entry.get('blocker_pr_number')
        repo = entry.get('repo')
        if not isinstance(blocker, int) or not isinstance(repo, str):
            continue
        is_open = _gh_pr_is_open(repo, blocker)
        if is_open is False:
            # Blocker resolved (merged or closed) — release this entry.
            _queue_release(blocker, repo)
            # Don't break; other entries may share this blocker.

    # Pass 3: watchdog DMs (re-read again — releases above may have
    # cleared entries).
    entries = _load_auto_merge_queue()
    changed = False
    for entry in entries:
        if entry.get('watchdog_dm_sent'):
            continue
        queued_at = entry.get('queued_at')
        if not isinstance(queued_at, str):
            continue
        try:
            queued_dt = datetime.fromisoformat(queued_at.replace('Z', '+00:00'))
        except ValueError:
            continue
        if queued_dt.tzinfo is None:
            queued_dt = queued_dt.replace(tzinfo=timezone.utc)
        age = (now - queued_dt).total_seconds()
        if age < watchdog_threshold_sec:
            continue
        dedup_key = (entry.get('repo') or '', entry.get('pr_number') or 0)
        if dedup_key in _WATCHDOG_DMED_PRS:
            continue
        _WATCHDOG_DMED_PRS.add(dedup_key)
        _dm_larry_queue_stale(entry)
        entry['watchdog_dm_sent'] = True
        changed = True
    if changed:
        _save_auto_merge_queue(entries)


def _trip_emergency_halt(
    data: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """Write the EMERGENCY_HALT flag file + queue the priority broadcast DM.

    D3.5 commit 5d. Activates the halt-file trip that 5a deferred — the
    polling on both `inbox_watcher.py` and `outbox_notifier.py` has been
    in place since the kill_switch.py adapter landed (audit-pulled from
    upstream); 5d closes the loop by giving Mirror's REVIEW_EMERGENCY_HALT
    marker authority to TRIP the file (vs only an operator running
    `kill_switch.py halt`).

    Flag envelope shape parallels `kill_switch.halt()` so an operator-
    triggered halt and a Mirror-triggered halt are indistinguishable to
    downstream readers. Idempotent: if the file already exists (operator
    halt OR prior Mirror trip), the trip is a no-op for the file but the
    priority DM still queues (different task, same Mirror urgency).

    Priority DM uses `larry_alerts.append_alert(severity='critical')` —
    BROADCASTS to all authorized chats (not targeted like the 5a-followup
    closing-DM pipe). The 10-min cooldown is keyed on
    `outbox-notifier:emergency-halt:<task_id>` so each task_id gets its
    own bucket and same-task re-emits within 10 min are suppressed
    (matches the kill_switch design: a tripped halt is sticky).

    Halt scope: per Q5=A sign-off, ALL four agents stop dispatching. The
    inbox-watcher's main-loop poll exits cleanly on next iteration; the
    notifier-side poll exits cleanly too (loop checked at lines 538 + the
    notifier's main poll respectively).
    """
    task_id = data.get('task_id') or 'unknown'
    pr_url = payload.get('pr_url') if isinstance(payload, dict) else None
    reason = payload.get('reason') if isinstance(payload, dict) else None
    evidence = payload.get('evidence') if isinstance(payload, dict) else None

    # Idempotent file trip.
    if EMERGENCY_HALT_FLAG.exists():
        log(
            f'EMERGENCY_HALT already tripped at {EMERGENCY_HALT_FLAG} '
            f'(operator or prior Mirror dispatch); not overwriting. '
            f'Re-emitting priority DM for task={task_id} anyway so the '
            f'new evidence reaches Larry.',
            'WARN',
        )
    else:
        try:
            BLACKBOARD.mkdir(parents=True, exist_ok=True)
            envelope = {
                'activated_at': datetime.now(timezone.utc).isoformat(),
                'activated_by': 'mirror-marker',
                'reason': reason or '(no reason)',
                'evidence': evidence or '(no evidence)',
                'task_id': task_id,
                'pr_url': pr_url or '(no PR URL)',
            }
            EMERGENCY_HALT_FLAG.write_text(
                json.dumps(envelope, indent=2, ensure_ascii=False) + '\n',
            )
            log(
                f'EMERGENCY_HALT TRIPPED by Mirror REVIEW_EMERGENCY_HALT '
                f'on task={task_id} (pr={pr_url}); halt file written at '
                f'{EMERGENCY_HALT_FLAG}; all dispatches will pause on '
                f'next 5s poll. Recovery: `python3 ~/agent-core/scripts/'
                f'kill_switch.py resume`',
                'CRITICAL',
            )
        except OSError as e:
            # Halt-file write failure is critical — log loud for watchdog
            # scanning. Continue to the priority DM so Larry still hears
            # about the safety event even if the file write failed.
            log(
                f'EMERGENCY_HALT_FILE_WRITE_FAILED task={task_id} '
                f'(disk full? permissions?): {type(e).__name__}: {e}; '
                f'halt is NOT enforced — manual intervention required',
                'WARN',
            )

    # Priority broadcast DM via larry_alerts.append_alert (kind: alert,
    # broadcasts to all authorized chats). Per-task cooldown bucket via
    # `subject` so different tasks don't suppress each other.
    body_lines = [
        f'EMERGENCY_HALT tripped on task `{task_id}`.',
        f'Reason: {reason or "(no reason)"}',
        f'Evidence: {evidence or "(no evidence)"}',
    ]
    if pr_url:
        body_lines.append(f'PR: {pr_url}')
    body_lines.append('All four agents halt on next 5s poll. The halt is sticky.')
    # Recovery command is carried by `suggested_action` below; don't
    # duplicate in the body (5d code-review finding #15).
    body = '\n'.join(body_lines)
    if not larry_alerts.append_alert(
        source='outbox-notifier',
        severity='critical',
        message=body,
        subject=f'emergency-halt:{task_id}',
        suggested_action='python3 ~/agent-core/scripts/kill_switch.py resume',
    ):
        # Cooldown suppressed OR write failed — distinguish in the log.
        # 10-min cooldown on a per-task_id basis means a re-emit on the
        # SAME task within 10 min is intentional suppression; a NEW task
        # halt would have a different subject and would not suppress.
        log(
            f'EMERGENCY_HALT priority DM not queued for task={task_id} '
            f'(cooldown suppressed within 10 min OR alert-file write '
            f'failed); halt file write above is the load-bearing action',
            'WARN',
        )


def _route_beacon_replan_approval(data: dict[str, Any]) -> bool:
    """Handle Beacon's auto-replan APPROVAL_REQUEST emitted in response to a
    review-escalate inbox dispatch.

    D3.5 commit 5c. Bridges the gap between Beacon-via-inbox-watcher (her
    APPROVAL_REQUEST in result text) and the bot's existing chat-mode
    APPROVAL_REQUEST flow (which only fires on Telegram chat replies, not
    on outbox-derived markers). The notifier acts as the bot's impersonator
    here — calls `approval.extract_approval_request`, `trust_policy.evaluate`,
    `approval.add_pending`, and queues the formatted approval DM through the
    larry-alerts pipeline so the bot's existing alerts poll surfaces it to
    Larry without any chat round-trip.

    Returns:
      True if the replan path took over (caller should archive the outbox
        and NOT fall through to default routing).
      False if the path declined (no marker, or marker failed the discipline
        gate). Caller falls through to default routing so Beacon's narrative
        still reaches Mirror as informational result-notification.

    Decision tree (in order):

      1. No APPROVAL_REQUEST present in result → return False.
      2. Malformed APPROVAL_REQUEST (e.g., missing required fields) → log
         WARN + return False (fall through; level-3 discipline doesn't run
         marker-error cascade per 5c sign-off).
      3. Discipline gate fail (task_id mismatch OR insufficient Mirror-reason
         reference) → log WARN + return False.
      4. Budget exhausted (replan_count+1 > max_replans) → queue Larry-
         notification "loop exhausted" + return True (suppress dispatch).
      5. Trust policy:
           - reject → queue Larry-notification with policy rejection +
             return True.
           - auto_approve → add_pending(replan_count=next) +
             dispatch_approved + queue Larry-notification with auto-approve
             confirmation + resolve(approved) + return True.
           - force_ask → add_pending(replan_count=next) + queue
             approval-request alert + return True.

    No marker-error cascade on the Beacon side (per 5c sign-off, level-3
    discipline). If you want strict-5 dial behavior, the notifier would
    need a `_notify_beacon_marker_error` parallel to the Forge/Mirror
    paths plus a Beacon CLAUDE.md retry contract. Deferred.
    """
    task_id = data.get('task_id') or 'unknown'
    result_text = data.get('result') or ''
    if not isinstance(result_text, str) or not result_text:
        return False

    try:
        payload, _narrative = approval.extract_approval_request(result_text)
    except approval.MalformedApprovalMarker as e:
        log(
            f'beacon replan APPROVAL_REQUEST malformed for task {task_id}: '
            f'{e}; falling through to default routing',
            'WARN',
        )
        return False

    if payload is None:
        # Beacon chose to push back with prose only — no marker emitted.
        # Default routing will notify-back informationally.
        return False

    # Level-3 discipline gate. Failures log WARN and fall through; no
    # cascade per 5c sign-off.
    mirror_reason = data.get('mirror_escalate_reason', '') or ''
    ok, err = approval.validate_replan_discipline(
        payload, data, mirror_reason,
    )
    if not ok:
        log(
            f'beacon replan APPROVAL_REQUEST failed discipline gate for '
            f'task {task_id}: {err}; falling through to default routing',
            'WARN',
        )
        return False

    # Budget gate (system-side backstop — Beacon's CLAUDE.md is first line).
    decision, next_count, max_count = approval.evaluate_replan_budget(data)
    reply_chat_id = data.get('reply_chat_id')
    is_valid_chat = isinstance(reply_chat_id, int)

    if decision == 'exhausted':
        log(
            f'beacon replan budget exhausted for task {task_id} '
            f'(would be round {next_count} of {max_count}); '
            f'suppressing APPROVAL_REQUEST and DMing Larry',
            'WARN',
        )
        if is_valid_chat:
            msg = (
                f'Beacon replan loop exhausted on task `{task_id}` '
                f'(round {next_count - 1} of {max_count} used). '
                f'She emitted another revised plan but the budget cap fired. '
                f'Decide manually: chat with her to take a different '
                f'approach, or accept the prior PR as-is with caveats.'
            )
            if not larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-escalate',
                message=msg,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                # M-5 review fix: surface alert-write failure so it's not
                # silent. Beacon's reminder timer at 6/24/72h will catch
                # the hung pending state, but for now log the gap loud.
                log(
                    f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
                    f'budget-exhausted DM did not queue (disk full?); '
                    f'manual intervention required',
                    'WARN',
                )
        else:
            # M-3 review fix: budget-exhausted with no chat_id is the
            # cascade-exhaust-silent-to-Larry failure mode. Without a
            # chat_id we can't DM, but we DO want a load-bearing sentinel
            # the watchdog can scan for. The dispatch is closed either
            # way; the silent loop is the part to avoid.
            log(
                f'BEACON_REPLAN_EXHAUSTED_NO_CHAT task={task_id} '
                f'(round {next_count - 1} of {max_count}); cannot DM '
                f'(no reply_chat_id on envelope); manual intervention '
                f'required',
                'WARN',
            )
        return True

    # Trust policy decision.
    try:
        action_str, rule = approval.trust_decision(payload)
    except Exception as e:  # noqa: BLE001 — defensive; never wedge daemon
        log(
            f'trust_policy.evaluate raised on beacon replan for task '
            f'{task_id}: {type(e).__name__}: {e}; falling through',
            'WARN',
        )
        return False

    # add_pending requires a chat_id (the bot uses it to route the DM).
    # Without one, we can't queue the approval-request alert because the
    # bot has no destination. Fall through so the narrative still reaches
    # Mirror via default routing.
    if not is_valid_chat:
        log(
            f'beacon replan APPROVAL_REQUEST for task {task_id} has no '
            f'valid reply_chat_id (got {reply_chat_id!r}); cannot route '
            f'approval DM, falling through',
            'WARN',
        )
        return False

    # Record the autonomy decision (powers the Automated Work + needs-Larry views).
    chain_event_emit.emit_event(
        **approval.build_autonomy_decision_chain_event(
            payload, decision=action_str, rule=rule, source='beacon'),
    )

    if action_str == 'reject':
        log(
            f'trust_policy rejected beacon replan for task {task_id} '
            f'(rule={rule}); DMing Larry the rejection',
        )
        msg = approval.format_policy_rejection(payload, rule or {})
        if not larry_alerts.append_notification(
            source='outbox-notifier',
            intent='reject',
            message=msg,
            chat_id=reply_chat_id,
            task_id=task_id,
        ):
            log(
                f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
                f'trust-policy reject DM did not queue (disk full?)',
                'WARN',
            )
        return True

    # Med-10 review fix: dedup by task_id. Replay scenarios (notifier
    # processes a duplicate outbox; bot replays after a crash; synthetic
    # smoke test re-drops the same file) would otherwise create N pending
    # entries and queue N alerts, DMing Larry N times for the same plan.
    # Med-X1 second-pass fix: search BOTH pending AND history. The original
    # find_pending_by_id missed entries that completed auto-approve (entry
    # moved to history) but the outbox wasn't archived due to a crash —
    # replay would re-dispatch the Forge task, overwriting the prior one.
    existing = approval.find_by_id_any_state(payload['task_id'])
    if existing is not None:
        log(
            f'beacon replan APPROVAL_REQUEST for task {task_id} already '
            f'has an entry (id={existing["id"]}, status={existing.get("status", "pending")}); '
            f'skipping duplicate add_pending + alert queue',
        )
        return True

    # D3.5 5c-followup-3 (audit 5.A): respect the /pause contract. When
    # approvals are paused, the bot's chat-mode flow correctly creates
    # the entry with queued_during_pause=True and skips the DM — the
    # backlog surfaces on /resume. The notifier-side replan path was
    # missing this check, so Beacon's auto-replan APPROVAL_REQUEST would
    # DM Larry during a /pause, violating the contract. Mirror the bot's
    # chat-mode shape: durably add the entry as queued, skip the alert.
    is_paused = approval.is_paused()
    entry = approval.add_pending(
        payload,
        chat_id=reply_chat_id,
        replan_count=next_count,
        max_replans=max_count,
        queued_during_pause=is_paused,
    )
    chain_event_emit.emit_event(
        **approval.build_approval_request_chain_event(payload),
    )
    if is_paused:
        log(
            f'beacon replan APPROVAL_REQUEST queued during /pause for task '
            f'{task_id} (replan_count={next_count}); will be DMed on /resume',
        )
        return True

    if action_str == 'auto_approve':
        # Med-9 review fix: bare-Exception coverage matches the trust_decision
        # block above. Either both narrow OR both wide — daemon-never-wedge is
        # the actual invariant. M-4 added OSError + JSONEncodeError to the
        # original DispatchRejected/RoutingDenied pair.
        try:
            approval.dispatch_approved(entry)
            approval.resolve(
                entry['id'], 'approved',
                note=f'auto_approved by rule (5c replan): {rule}',
            )
            log(
                f'beacon replan auto-approved + dispatched: task={task_id}, '
                f'replan_count={next_count}, rule={rule}',
            )
            msg = approval.format_auto_approve_confirmation(entry, rule or {})
            if not larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-pass',  # informational; reuse closest emoji
                message=msg,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                log(
                    f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
                    f'auto-approve confirmation did not queue (disk full?)',
                    'WARN',
                )
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge invariant
            log(
                f'beacon replan auto-approve dispatch FAILED for task '
                f'{task_id}: {type(e).__name__}: {e}',
                'WARN',
            )
            # Entry is still in pending — Larry can resolve manually.
            msg = (
                f'Beacon replan auto-approve dispatch failed for task '
                f'`{task_id}`: {type(e).__name__}: {e}. Entry remains '
                f'pending; reply `reject: ...` to clear or retry manually.'
            )
            if not larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-escalate',
                message=msg,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                log(
                    f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
                    f'auto-approve failure DM did not queue (disk full?)',
                    'WARN',
                )
        return True

    # force_ask path — queue the approval-request alert; bot DMs the
    # formatted approval body on its next sweep.
    body = approval.format_approval_dm(entry)
    if not larry_alerts.append_approval_request(
        chat_id=reply_chat_id,
        approval_id=entry['id'],
        body=body,
    ):
        # M-5 review fix: if the alert queue write fails, the pending entry
        # still exists (durable) but Larry won't get the DM until the
        # reminder timer fires at 6h. Log loud for watchdog scanning.
        log(
            f'BEACON_REPLAN_ALERT_WRITE_FAILED task={task_id} '
            f'force_ask approval-request did not queue (disk full?); '
            f'pending entry exists; reminder timer will surface at 6h',
            'WARN',
        )
    log(
        f'beacon replan APPROVAL_REQUEST queued for force_ask: task={task_id}, '
        f'replan_count={next_count}, chat_id={reply_chat_id}',
    )
    return True


def _primary_chat_id() -> Optional[int]:
    """Resolve Larry's primary Telegram chat from TELEGRAM_ALLOWED_CHAT_IDS
    (the bot's own allow-list), returning the lowest authorized id — the
    default Larry chat for daemon-originated approvals that carry no
    originating chat. Mirrors `pulse_check_v._primary_chat_id`. Returns None
    if the env var is unset/empty; callers then fall through rather than
    drop the approval into a broadcast.
    """
    raw = os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS', '')
    ids = []
    for tok in raw.replace(',', ' ').split():
        try:
            ids.append(int(tok))
        except ValueError:
            continue
    return min(ids) if ids else None


def _route_beacon_pulse_auto_dispatch_approval(
    data: dict[str, Any],
    *,
    policy_source: str = 'pulse-auto-dispatch',
    chat_id_fallback: Optional[int] = None,
    enforce_task_id_match: bool = True,
) -> bool:
    """Handle Beacon's APPROVAL_REQUEST emitted in response to a
    Pulse-auto-dispatch inbox envelope (closed-loop step 4).

    Pulse-auto-dispatch is the third Beacon-outbox APPROVAL_REQUEST trigger,
    sibling to:
      - `_route_beacon_replan_approval` (Mirror REVIEW_ESCALATE → replan)
      - `_handle_beacon_headless_approval_request` (Larry-session dispatch)

    Key shape differences from those siblings:
      - No replan budget (this is initial dispatch, not a revision round).
      - No Mirror-reason discipline gate (no Mirror finding to reference).
      - Trust policy IS consulted (unlike the headless source='larry' path);
        Pulse's auto-dispatch judgment is NOT implicit Larry approval. v1
        config ships only `force_ask` for this source so every Pulse-driven
        dispatch still pings Larry — auto-approve carve-outs are a future
        dial per closed-loop spec § 5.

    Discipline gate: payload `task_id` must match envelope `task_id`. Beacon's
    Shape 8 guidance keys the marker's task_id to the upstream envelope so a
    drift here signals she's responding to the wrong dispatch.

    Keyword parameters (all default to the pulse-auto-dispatch behavior, so
    the original caller is unchanged; the source='pulse' direction-ask caller
    overrides them — fix-depth1-pulse-approval-extraction-001, 2026-06-12):
      - `policy_source`: the `source` stamped on the trust_policy task so the
        policy file can carve out per-source rules. Pulse direction-asks pass
        `'pulse'` to stay distinct from `'pulse-auto-dispatch'`.
      - `chat_id_fallback`: when the envelope's `reply_chat_id` is not an int
        (direction-ask envelopes carry `reply_chat_id: null`), this int is
        used instead of dropping the approval. `None` preserves the original
        "no valid chat → fall through" behavior.
      - `enforce_task_id_match`: when False, skip the marker/envelope task_id
        discipline gate. A direction-ask proposes a NEW task, so the marker's
        task_id legitimately differs from the question envelope's task_id
        (mirrors `_handle_beacon_headless_approval_request`, where the
        marker's task_id is authoritative).

    Returns:
      True if the auto-dispatch path took over (caller archives the outbox
        and does NOT fall through to default routing).
      False if the path declined (no marker, malformed marker, discipline
        gate failure, missing chat_id, or trust_policy raised). Caller
        falls through to default routing so Beacon's narrative still
        reaches downstream consumers.

    No marker-error cascade (mirrors the 5c replan path's behavior — log
    WARN and fall through; Beacon's CLAUDE.md is the first-line gate).
    """
    task_id = data.get('task_id') or 'unknown'
    result_text = data.get('result') or ''
    if not isinstance(result_text, str) or not result_text:
        return False

    try:
        payload, _narrative = approval.extract_approval_request(result_text)
    except approval.MalformedApprovalMarker as e:
        log(
            f'beacon pulse-auto-dispatch APPROVAL_REQUEST malformed for task '
            f'{task_id}: {e}; falling through to default routing',
            'WARN',
        )
        return False

    if payload is None:
        # No marker emitted — Beacon's push-back narrative reaches Mirror
        # via default routing.
        return False

    # Discipline gate: marker task_id must match envelope task_id. Strip
    # any `notify-` prefix on the envelope side (the notifier prefixes
    # filenames for disambiguation; Beacon's marker uses the original id).
    envelope_task_id = task_id
    if envelope_task_id.startswith('notify-'):
        envelope_task_id = envelope_task_id[len('notify-'):]
    marker_task_id = payload.get('task_id')
    if enforce_task_id_match and marker_task_id != envelope_task_id:
        log(
            f'beacon pulse-auto-dispatch APPROVAL_REQUEST task_id mismatch '
            f'(envelope={envelope_task_id}, marker={marker_task_id!r}); '
            f'falling through to default routing',
            'WARN',
        )
        return False

    reply_chat_id = data.get('reply_chat_id')
    if not isinstance(reply_chat_id, int):
        if isinstance(chat_id_fallback, int):
            log(
                f'beacon pulse-auto-dispatch APPROVAL_REQUEST for task '
                f'{task_id} has no valid reply_chat_id (got '
                f'{reply_chat_id!r}); falling back to default Larry chat '
                f'{chat_id_fallback}',
            )
            reply_chat_id = chat_id_fallback
        else:
            log(
                f'beacon pulse-auto-dispatch APPROVAL_REQUEST for task '
                f'{task_id} has no valid reply_chat_id (got '
                f'{reply_chat_id!r}); cannot route approval DM, falling '
                f'through',
                'WARN',
            )
            return False

    # Trust policy — source='pulse-auto-dispatch' so the policy file can
    # carve out per-source rules independently of beacon-sourced dispatches.
    # Bypass approval.trust_decision (which hardcodes source='beacon') and
    # call trust_policy.evaluate directly with the Pulse source.
    policy_task = {
        'source': policy_source,
        'target_agent': payload.get('target_agent', 'forge'),
        'task_type': payload.get('task_type'),
        'target_repo': payload.get('target_repo'),
        'changed_files': payload.get('changed_files', []),
    }
    # #8: predict sensitive intent for a fresh pulse dispatch (no changed_files)
    # so the pulse sensitive carve-out can force_ask it — same chokepoint logic
    # as approval.trust_decision, shared via approval.predict_sensitive_intent.
    if not policy_task['changed_files'] and approval.predict_sensitive_intent(payload):
        policy_task['sensitive_intent'] = True
    try:
        action_str, rule = trust_policy.evaluate(policy_task)
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge invariant
        log(
            f'trust_policy.evaluate raised on beacon pulse-auto-dispatch '
            f'for task {task_id}: {type(e).__name__}: {e}; falling through',
            'WARN',
        )
        return False

    # Record the autonomy decision (powers the Automated Work + needs-Larry views).
    chain_event_emit.emit_event(
        **approval.build_autonomy_decision_chain_event(
            payload, decision=action_str, rule=rule, source=policy_source),
    )

    if action_str == 'reject':
        log(
            f'trust_policy rejected beacon pulse-auto-dispatch for task '
            f'{task_id} (rule={rule}); DMing Larry the rejection',
        )
        msg = approval.format_policy_rejection(payload, rule or {})
        if not larry_alerts.append_notification(
            source='outbox-notifier',
            intent='reject',
            message=msg,
            chat_id=reply_chat_id,
            task_id=task_id,
        ):
            log(
                f'BEACON_PULSE_AUTO_DISPATCH_ALERT_WRITE_FAILED task={task_id} '
                f'trust-policy reject DM did not queue (disk full?)',
                'WARN',
            )
        return True

    # Replay dedup — same shape as the replan path (Med-X1). Same outbox
    # processed twice (notifier crash between resolve() and archive, or a
    # retry from upstream) must not double-dispatch.
    existing = approval.find_by_id_any_state(marker_task_id)
    if existing is not None:
        log(
            f'beacon pulse-auto-dispatch APPROVAL_REQUEST for task {task_id} '
            f'already has an entry (id={existing["id"]}, '
            f'status={existing.get("status", "pending")}); skipping duplicate '
            f'add_pending + alert queue',
        )
        return True

    # Honor /pause — mirror the 5c-followup-3 pattern: queue durably with
    # queued_during_pause=True, skip the DM, surface on /resume.
    is_paused = approval.is_paused()
    entry = approval.add_pending(
        payload,
        chat_id=reply_chat_id,
        queued_during_pause=is_paused,
    )
    chain_event_emit.emit_event(
        **approval.build_approval_request_chain_event(payload),
    )
    if is_paused:
        log(
            f'beacon pulse-auto-dispatch APPROVAL_REQUEST queued during '
            f'/pause for task {task_id}; will be DMed on /resume',
        )
        return True

    if action_str == 'auto_approve':
        try:
            approval.dispatch_approved(entry)
            approval.resolve(
                entry['id'], 'approved',
                note=f'auto_approved by rule ({policy_source}): {rule}',
            )
            log(
                f'beacon pulse-auto-dispatch auto-approved + dispatched: '
                f'task={task_id}, rule={rule}',
            )
            msg = approval.format_auto_approve_confirmation(entry, rule or {})
            if not larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-pass',
                message=msg,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                log(
                    f'BEACON_PULSE_AUTO_DISPATCH_ALERT_WRITE_FAILED '
                    f'task={task_id} auto-approve confirmation did not '
                    f'queue (disk full?)',
                    'WARN',
                )
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge invariant
            log(
                f'beacon pulse-auto-dispatch auto-approve dispatch FAILED '
                f'for task {task_id}: {type(e).__name__}: {e}',
                'WARN',
            )
            msg = (
                f'Beacon pulse-auto-dispatch auto-approve dispatch failed '
                f'for task `{task_id}`: {type(e).__name__}: {e}. Entry '
                f'remains pending; reply `reject: ...` to clear or retry '
                f'manually.'
            )
            if not larry_alerts.append_notification(
                source='outbox-notifier',
                intent='review-escalate',
                message=msg,
                chat_id=reply_chat_id,
                task_id=task_id,
            ):
                log(
                    f'BEACON_PULSE_AUTO_DISPATCH_ALERT_WRITE_FAILED '
                    f'task={task_id} auto-approve failure DM did not '
                    f'queue (disk full?)',
                    'WARN',
                )
        return True

    # force_ask path — queue the approval-request alert.
    body = approval.format_approval_dm(entry)
    if not larry_alerts.append_approval_request(
        chat_id=reply_chat_id,
        approval_id=entry['id'],
        body=body,
    ):
        log(
            f'BEACON_PULSE_AUTO_DISPATCH_ALERT_WRITE_FAILED task={task_id} '
            f'force_ask approval-request did not queue (disk full?); '
            f'pending entry exists; reminder timer will surface at 6h',
            'WARN',
        )
    log(
        f'beacon pulse-auto-dispatch APPROVAL_REQUEST queued for force_ask: '
        f'task={task_id}, chat_id={reply_chat_id}',
    )
    return True


def _handle_beacon_headless_approval_request(
    data: dict[str, Any], result_text: str,
) -> Optional[str]:
    """Handle a headless Beacon APPROVAL_REQUEST emission (Claude-driven).

    Task #17 (2026-05-19) — fourth architectural finding from the E1.5
    session. Beacon's APPROVAL_REQUEST had two existing handlers:
      1. Chat-mode (beacon_telegram_bot.py): Larry chats Beacon on Telegram,
         bot intercepts the marker, consults trust_policy, DMs Larry, on
         approval writes Forge's preflight envelope.
      2. Auto-replan-after-Mirror-ESCALATE (_route_beacon_replan_approval
         in this file): Mirror REVIEW_ESCALATE → Beacon emits a revised
         APPROVAL_REQUEST in her outbox response.

    Neither path covers the third case: Claude in a Larry-session drops a
    dispatch envelope into Beacon's inbox (source='larry', non-chat); Beacon
    emits a clean APPROVAL_REQUEST in her result text; nothing processes it.
    This handler closes that gap by translating the marker into a Forge
    preflight task, mirroring the chat-mode path through
    `beacon_approval_handler.dispatch_approved` without the Telegram
    round-trip.

    Trust policy is NOT consulted on this path. The assumption is that the
    upstream Larry-session that dropped the envelope into Beacon's inbox
    already had Larry's explicit approval — Claude doesn't dispatch Beacon
    headlessly without it. (Future approval-gate dial: check an optional
    `pre_approved` envelope field and fall back to trust_policy.evaluate
    when missing. Deferred — implicit-via-source=larry is sufficient for
    the one-operator system today.)

    Args:
        data: the Beacon outbox envelope.
        result_text: Beacon's result string (carries the marker block).

    Returns:
        Path string of the written Forge preflight task on a successful
        dispatch, or the existing path on an idempotent skip — both signal
        "handled; caller should archive without falling through to default
        notify routing."
        None when the path declined (gate failed, no marker, malformed
        marker, write failure) so the caller falls through to default
        routing and Beacon's narrative still reaches downstream consumers.
    """
    if (
        data.get('agent') != 'beacon'
        or data.get('source') not in _BEACON_TRUSTED_DISPATCH_SOURCES
    ):
        return None
    task_id = data.get('task_id') or 'unknown'
    if not isinstance(result_text, str) or not result_text:
        return None

    try:
        payload, _narrative = approval.extract_approval_request(result_text)
    except approval.MalformedApprovalMarker as e:
        log(
            f'beacon headless APPROVAL_REQUEST malformed for task '
            f'{task_id}: {e}; falling through to default routing',
            'WARN',
        )
        return None

    if payload is None:
        # No marker emitted — let default routing notify back informationally.
        return None

    # Marker's task_id is authoritative for the downstream Forge work; the
    # envelope's task_id was the upstream Larry-session dispatch ticket and
    # may differ. Mirrors chat-mode's `dispatch_approved`, which keys the
    # written filename on `payload["task_id"]`.
    marker_task_id = payload.get('task_id') or task_id

    forge_base: dict[str, Any] = {
        'task_id': marker_task_id,
        'prompt': payload.get('prompt') or '',
        'source': 'beacon',
        'target_agent': 'forge',
        'phase': 'preflight',
        'dispatched_by': 'outbox-notifier',
    }
    # Propagate envelope/marker fields the way chat-mode does — marker
    # values win when both are present (Beacon's marker is the spec).
    target_repo = payload.get('target_repo') or data.get('target_repo')
    pr_title = payload.get('pr_title') or data.get('pr_title')
    if pr_title:
        forge_base['pr_title'] = pr_title
    max_clar = payload.get('max_clarifications')
    if max_clar is None:
        max_clar = data.get('max_clarifications')
    if isinstance(max_clar, int) and max_clar >= 0:
        forge_base['max_clarifications'] = max_clar
    for field in ('task_type', 'summary', 'changed_files'):
        if payload.get(field) is not None:
            forge_base[field] = payload[field]
    forge_task = build_chain_envelope(
        forge_base, data,
        carry={
            'target_repo': target_repo,
            'reply_chat_id': CARRY,
            'forge_build_session_id': DROP,
            'replan_count': DROP,
            'max_replans': DROP,
            'revision_count': DROP,
            'pr_url': DROP,
        },
    )

    # Idempotency — same shape as _dispatch_mirror_review and
    # _dispatch_build_phase. Guards against re-processing the same outbox
    # if the notifier crashes between dispatch and archive.
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
    filename = safe_write_inbox.canonical_inbox_name(f'{marker_task_id}.json')
    # A task currently in the LIVE inbox is queued/in-flight — always skip,
    # never override (don't double-dispatch a running preflight).
    live = forge_inbox / filename
    if live.exists():
        log(
            f'headless-approval-request already dispatched for task '
            f'{marker_task_id} (live inbox file present); skipping '
            f'duplicate write',
        )
        return str(live)
    # A stale `<task_id>.json` in .archive/ or .invalid/ normally means the
    # task was already dispatched (crash-recovery protection). EXCEPTION
    # (headless dedup-wedge fix, 2026-06-11, mirroring PR #403): if the prior
    # attempt was a DEFINITIVE non-run (spawn-failure / identity-mismatch
    # reject, no PR), the artifact is a phantom that would permanently wedge
    # the task_id — allow the re-dispatch. The override fires ONLY on a
    # determinable definitive non-run; in-flight / completed work returns
    # False and keeps the conservative skip.
    for candidate in (
        forge_inbox / '.archive' / filename,
        forge_inbox / '.invalid' / filename,
    ):
        if candidate.exists():
            if _prior_dispatch_was_definitive_non_run(marker_task_id):
                log(
                    f'HEADLESS_DEDUP_OVERRIDE task={marker_task_id} — prior '
                    f'attempt was a definitive non-run (spawn-failure / '
                    f'identity-reject, no PR); allowing re-dispatch'
                )
                break
            log(
                f'headless-approval-request already dispatched for task '
                f'{marker_task_id} (archive or .invalid present); '
                f'skipping duplicate write',
            )
            return str(candidate)

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='forge',
            task_dict=forge_task,
            source_agent='beacon',
            filename=filename,
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'headless-approval-request dispatch FAILED for task '
            f"{marker_task_id}: {type(e).__name__}: {e}. Beacon's "
            f'APPROVAL_REQUEST marker was emitted but Forge was not '
            f'dispatched; Larry must manually re-dispatch.',
            'WARN',
        )
        return None

    log(
        f'headless-approval-request dispatched forge <- beacon '
        f'(task={marker_task_id}, file={dest.name})'
    )
    return str(dest)


_SEQUENCE_KICKOFF_TARGET_AGENT = 'build_sequence_advancer'

# PR-S4 rectification (H3): sources whose Beacon outboxes are eligible
# for the headless-approval / kickoff handlers. `larry` covers the
# original Claude-in-Larry-session dispatch (task #17 from 2026-05-19).
# `orchestrator` covers the build_sequence_advancer daemon's step
# envelopes — when the advancer dispatches step N to Beacon's inbox, the
# envelope carries `source: 'orchestrator'` (see
# `scripts/build_sequence_advancer.py:468,490`), and Beacon's response
# must route through the same headless-approval translation to reach
# Forge. Without `orchestrator` in this set, advancer-dispatched steps
# silently archive as dead-end notifies and the orchestrator chain
# stalls — the empirical failure observed running
# `orchestrator-bootstrap-001` on 2026-05-27.
_BEACON_TRUSTED_DISPATCH_SOURCES = frozenset({'larry', 'orchestrator'})


def _handle_build_sequence_advancer_kickoff(
    data: dict[str, Any], result_text: str,
) -> Optional[str]:
    """Handle a Beacon kickoff APPROVAL_REQUEST for the build sequence advancer.

    PR-S4 (orchestrator workstream finale). Spec:
    agents/beacon/specs/build-sequence-orchestrator.md § 5.5 discipline 2
    ("Emit a single APPROVAL_REQUEST with task_id: kickoff-<seq-id>,
    target_agent: build_sequence_advancer, prompt: kickoff <seq-id>. The
    bot routes this to the advancer rather than Forge.").

    Routing collision discipline: this handler fires ONLY when the marker
    payload's `target_agent == 'build_sequence_advancer'`. Markers with
    target_agent in {forge, mirror, beacon, pulse, None} fall through to
    `_handle_beacon_headless_approval_request` unchanged. Existing dispatch
    paths are untouched — the new handler is purely additive and gates on
    a single string-equality check.

    Scope (per preflight Q6 option a — route-only, NOT inline first-step
    dispatch): the handler transitions the sequence from `status=pending`
    to `status=active` and appends a `kickoff-acknowledged` audit_log
    entry. The next `build_sequence_advancer.tick()` (within 5 min per the
    systemd timer) discovers the dispatchable step(s) via the existing
    pending→deps-resolved logic and dispatches them. This keeps step
    dispatch single-sourced in the advancer per spec § 5.2.

    Idempotency (per preflight Q2 option b — uses existing `status` field,
    no `applied_kickoff` invention): re-emitting the kickoff marker on a
    sequence with `status != 'pending'` (i.e., in {active, paused,
    complete, failed, archived}) is a WARN no-op — no audit_log entry, no
    sequence-file write, no Larry DM.

    Failure modes:
      - prompt missing / not `kickoff <seq-id>` → log WARN, return None
        (fall through; this isn't a sequence kickoff marker even though
        target_agent matches).
      - sequence file missing → DM Larry, log WARN, return sentinel so the
        outbox is archived (we handled it; just unsuccessfully).
      - sequence file malformed JSON → DM Larry, return sentinel.
      - schema/DAG validation fails → DM Larry with the validator's first
        error, return sentinel. (PR-S2's validator is the single source of
        truth for what's a structurally-valid sequence file.)

    Args:
        data: the Beacon outbox envelope.
        result_text: Beacon's result string (carries the APPROVAL_REQUEST
            marker block).

    Returns:
        - str(sequence-file path) when the handler took action OR
          intentionally no-op'd (idempotent re-apply, missing/malformed
          file with DM fired). Caller archives the outbox and skips
          default routing.
        - None when the marker is absent, unparseable, or its target_agent
          isn't `build_sequence_advancer` — caller falls through to the
          existing headless-approval handler (which routes to Forge).
    """
    if (
        data.get('agent') != 'beacon'
        or data.get('source') not in _BEACON_TRUSTED_DISPATCH_SOURCES
    ):
        return None
    if not isinstance(result_text, str) or not result_text:
        return None

    try:
        payload, _narrative = approval.extract_approval_request(result_text)
    except approval.MalformedApprovalMarker:
        # Headless-approval handler will log its own diagnostic on the
        # same marker; we silently fall through.
        return None
    if payload is None:
        return None
    if payload.get('target_agent') != _SEQUENCE_KICKOFF_TARGET_AGENT:
        return None

    task_id = data.get('task_id') or payload.get('task_id') or 'unknown'

    # Delegate the actual pending->active transition (seq_id parse, file
    # locate/read/validate, idempotency no-op, spec_doc guard, atomic write,
    # Larry-DM failure modes) to the shared helper. The CHAT/dashboard-approve
    # path (`beacon_approval_handler.dispatch_approved`) invokes the SAME
    # helper, so both entry paths converge on one transition implementation
    # (kickoff-approve-routing-gap-001). Everything above this line — the
    # trust gate, marker extraction, and target_agent collision check — is
    # the notifier-path-specific preprocessing that stays here.
    outcome = build_sequence_kickoff.apply_kickoff_transition(
        prompt=payload.get('prompt'),
        marker_task_id=payload.get('task_id'),
        dispatch_task_id=task_id,
        agents_root=AGENTS_ROOT,
        log=log,
    )
    return outcome.sentinel


def _handle_beacon_clarification_response(
    data: dict[str, Any],
) -> Optional[str]:
    """Route Beacon's clarification-response as `--resume` of Forge's original task.

    task-25 (2026-05-20) — fifth chain-routing gap. When Beacon answers
    Forge's CLARIFY_REQUEST in headless mode, her outbox has source
    `forge-question` and her task_id is `notify-<original>`. The default
    notify-routing path turns that into `notify-notify-{task}.json` in
    Forge's inbox with envelope task_id=`notify-notify-{task}` — the
    watcher then spawns a brand-new worktree on a doubled-prefix branch
    (`forge/notify-notify-{task}-...`), Forge re-runs preflight in a
    fresh session without her original context, and the cascade
    depth-multiplies on each subsequent round (`notify-notify-notify-...`
    awareness notifies).

    The chat-mode bot (beacon_telegram_bot.py) doesn't have this bug
    because Larry's chat reply is routed differently. The headless
    path needs the parallel handler — same pattern as PR #46/#48's
    fixes for gaps #1–#4.

    This handler writes a continuation envelope to Forge's inbox keyed on
    the ORIGINAL task_id with:
      * task_id = original (stripped of `notify-` prefix)
      * source = 'beacon-clarification' (existing dialogue-leg suffix)
      * intent = 'clarification-response'
      * phase = 'preflight' (Forge re-runs preflight with the answer)
      * resume_session_id = forge_session_id (threaded through Beacon's
        round-trip via inbox_watcher._build_outbox propagation; the
        watcher's task-25 gate consumes this regardless of phase)
      * filename = `resume-<task>-r<count>.json` — unique per round
        (clarification_count discriminates) and idempotent on retry

    The default notify-{task}.json path is skipped, so no doubled-prefix
    file ever lands in any inbox and no `forge/notify-notify-*` branch
    ever gets created. Depth-multiplication is inherently prevented
    because the cascade stops at the continuation envelope.

    Returns the str(dest) path on dispatch (or skip-because-already-there),
    or None when the path declined (missing forge_session_id, write failure,
    or shape mismatch). None falls through to the default routing path so
    Beacon's response still reaches Forge as an informational notify (a
    legacy-compatible fallback — pre-task-25 chains will still complete,
    just with the doubled-prefix cosmetic bug until the next dispatch
    crosses this code path).
    """
    if data.get('agent') != 'beacon':
        return None
    source = data.get('source', '')
    if not source.endswith('-question'):
        return None

    # Without Forge's session, we can't --resume — fall through to default
    # routing (legacy behavior) so the chain still progresses, just without
    # the resume optimization. Logging at INFO so this case is visible if
    # the upstream forge_session_id propagation breaks.
    forge_session_id = data.get('forge_session_id')
    if not forge_session_id:
        log(
            f'clarification-response on task {data.get("task_id", "?")} '
            f'has no forge_session_id (Forge\'s preflight session not '
            f'threaded through); falling through to default notify routing — '
            f'the resume optimization will be skipped this round.',
        )
        return None

    # `data['task_id']` is the task_id Beacon's inbox file used, which is
    # `notify-<original>` because the previous hop wrote the notify with
    # that prefix. Strip it back to the original; if the prefix is absent
    # for some reason, accept the value as-is (defensive).
    notify_task_id = data.get('task_id') or ''
    if notify_task_id.startswith('notify-'):
        original_task_id = notify_task_id[len('notify-'):]
    else:
        original_task_id = notify_task_id
    if not original_task_id:
        log(
            f'clarification-response with empty task_id (source={source}); '
            f'cannot derive original — falling through to default routing',
            'WARN',
        )
        return None

    # Compose the resume-envelope prompt using the existing
    # clarification-response template + the original task_id (so the
    # narrative says "your earlier CLARIFY_REQUEST on `task-X`", NOT the
    # `notify-task-X` form the default-routing path would have rendered).
    remaining = fph.clarifications_remaining(data)
    prompt = build_notify_prompt(
        intent='clarification-response',
        sender='beacon',
        task_id=original_task_id,
        success=data.get('exit_code', 0) == 0,
        output=data.get('result', '') or '',
        error=data.get('error') or '',
        intent_kwargs={'remaining': remaining},
    )

    forge_base: dict[str, Any] = {
        'task_id': original_task_id,
        'prompt': prompt,
        'source': 'beacon-clarification',
        'intent': 'clarification-response',
        'phase': 'preflight',
        'resume_session_id': forge_session_id,
        # _notify_depth captures the hop position for telemetry; we reset
        # to 1 because the doubled-prefix cascade is what this handler
        # exists to stop.
        '_notify_depth': 1,
    }
    # Propagate clarification budget so Forge knows how many CLARIFY_REQUESTs
    # remain. Beacon's response is one round; count is what the envelope
    # already carries (incremented by the marker handler when Forge first
    # emitted CLARIFY_REQUEST, propagated through Beacon's round-trip).
    if data.get('clarification_count') is not None:
        forge_base['clarification_count'] = data['clarification_count']
    if data.get('max_clarifications') is not None:
        forge_base['max_clarifications'] = data['max_clarifications']
    # Propagate branch/pr_title/pr_body so Forge's worktree gate accepts the
    # continuation envelope. target_repo/pr_url are whitelisted context and
    # carry through below. Same shape as the default routing path which
    # propagates these via the `4b post-test-2 fix` block.
    for f_name in ('branch', 'pr_title', 'pr_body'):
        if data.get(f_name):
            forge_base[f_name] = data[f_name]
    forge_task = build_chain_envelope(
        forge_base, data,
        carry={
            'target_repo': CARRY,
            'pr_url': CARRY,
            'reply_chat_id': CARRY,
            'forge_build_session_id': DROP,
            'replan_count': DROP,
            'max_replans': DROP,
            'revision_count': DROP,
        },
    )

    # Filename — `resume-<task>-r<count>.json`. The clarification_count
    # discriminator makes the filename unique per round, so multi-round
    # cascades (Forge clarifies, Beacon answers, Forge clarifies again,
    # Beacon answers again) produce distinct files in inbox/.archive.
    # Idempotent on retry: if the daemon crashes between dispatch and
    # archive, the next poll re-processes the outbox; the second write
    # hits the inbox+archive+invalid existence check and skips. Same
    # idempotency shape as _handle_beacon_headless_approval_request.
    count_for_filename = data.get('clarification_count', 0) or 0
    filename = safe_write_inbox.canonical_inbox_name(f'resume-{original_task_id}-r{count_for_filename}.json')
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
    for candidate in (
        forge_inbox / filename,
        forge_inbox / '.archive' / filename,
        forge_inbox / '.invalid' / filename,
    ):
        if candidate.exists():
            log(
                f'clarification-response continuation already dispatched '
                f'for task {original_task_id} round {count_for_filename} '
                f'(file or archive or .invalid present); skipping duplicate '
                f'write',
            )
            return str(candidate)

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent='forge',
            task_dict=forge_task,
            source_agent='beacon-clarification',
            filename=filename,
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'clarification-response continuation dispatch FAILED for task '
            f'{original_task_id}: {type(e).__name__}: {e}. Falling through '
            f'to default notify routing so the response still reaches '
            f'Forge (just without the resume optimization).',
            'WARN',
        )
        return None

    log(
        f'clarification-response continuation dispatched forge <- beacon '
        f'(task={original_task_id}, round={count_for_filename}, '
        f'resume={forge_session_id[:12]}..., file={dest.name})'
    )
    # clarify-round-visibility § 6: emit clarify_response chain_event so the
    # dashboard can render the Forge↔Beacon Q+A round-trip. Sibling pattern
    # to _emit_clarify_request_chain_event (fired from Forge's CLARIFY
    # classification at line 6151). Best-effort; failures log WARN.
    _emit_clarify_response_chain_event(
        task_id=original_task_id,
        question=data.get('prompt', '') or '',
        answer=data.get('result', '') or '',
        clarification_round=count_for_filename,
    )
    return str(dest)


def _run_review_pass_auto_merge(
    data: dict[str, Any],
    marker_decision: dict[str, Any],
    outbox_file: Path,
) -> Optional[str]:
    """Run the auto-merge for a Mirror REVIEW_PASS and record the gh-truth outcome.

    false-success-notify-fix (2026-06-11). Called from process_outbox's
    marker-routing block BEFORE the back-leg inter-agent notify is built, so
    the notify reports the ACTUAL merge state (merged / queued-behind-blocker /
    conflict / failed) instead of an optimistic "auto-merge fired" claim. This
    extends Larry's original D3.5 5d sign-off ("merge BEFORE the closing DM
    renders") to the Beacon notify too, so BOTH the agent journal and Larry's
    DM are GitHub-truth. Incident: PR #455 was held behind #454 yet Beacon was
    told the merge fired.

    Sets `marker_decision['merge_result']`, `['merge_outcome']`, and the
    `merge_status_line` in `['intent_kwargs']` (read by the review-pass notify
    template). Returns:
      * 'auto-merge-skipped' — degenerate PR (shape-invalid url / 404 /
        already-terminal). The caller STILL sends the now-truthful notify and
        archives, but suppresses Larry's closing DM and returns this string
        verbatim — preserving the pre-fix skip semantics (notify yes, DM no)
        while replacing the old bogus "auto-merge fired" notify text with an
        accurate "skipped / not merged" (or "already MERGED") line.
      * None — a merge was attempted and the outcome recorded; the caller
        continues to the notify + closing DM as normal.

    Does NOT archive the outbox — the caller owns the single archive at the
    end of the marker block (so the notify dispatches first; resume-safety
    unchanged: a crash re-processes the outbox and gets `already_merged`).
    """
    payload = marker_decision.get('payload') or {}
    pr_url = payload.get('pr_url') if isinstance(payload, dict) else None

    def _skip(outcome: str, merge_reason: str, status_line: str) -> str:
        """Record a non-merge (skipped / already-terminal) review-pass
        outcome and return the 'auto-merge-skipped' signal. The notify
        renders `status_line`; the caller suppresses the DM on this signal."""
        marker_decision['merge_result'] = {
            'merge_outcome': outcome,
            'merge_reason': merge_reason,
            'pr_number': '?',
            'repo_coords': '?',
        }
        marker_decision['merge_outcome'] = outcome
        marker_decision['intent_kwargs'] = {
            **(marker_decision.get('intent_kwargs') or {}),
            'merge_status_line': status_line,
        }
        return 'auto-merge-skipped'

    # board-abort-dispatched-build: if this build was aborted, do not merge its
    # PR. Checked here on the primary review-pass path for a clean notify (DM
    # suppressed via _skip); the shared gate in _attempt_auto_merge_with_gates
    # backstops the queue-release + sweep-retry paths. FAIL-OPEN
    # (see _sequence_cancelled): only a confirmed cancellation skips.
    if _sequence_cancelled(data.get('task_id')):
        log(
            f'AUTO_MERGE task={data.get("task_id", "?")} pr={pr_url!r} '
            f'outcome=skipped reason=sequence-cancelled (build aborted) '
            f'agent=forge',
            'WARN',
        )
        return _skip(
            'skipped_sequence_cancelled',
            'build sequence was cancelled (aborted); auto-merge blocked',
            'Auto-merge was SKIPPED — the build was cancelled; the PR is '
            'NOT merged.',
        )

    # nervous-system-audit #15 (2026-06-05): the marker's `pr_url` is
    # agent-authored and must agree with the PR the chain actually
    # dispatched Mirror to review — `data['pr_url']`, set on the
    # review envelope by `_dispatch_mirror_review` and propagated
    # through the outbox. Without this gate the shape+OPEN validators
    # below would happily merge ANY structurally-valid OPEN PR the
    # marker names, even a different one than Mirror reviewed. Same
    # discipline as the marker-vs-envelope task_id check at
    # classification time, applied at the irreversible merge boundary.
    # Both-present-and-differ is the only fail case: a legacy chain
    # with no envelope pr_url falls through to the existing validators
    # (graceful degradation), and an absent / unparseable marker
    # pr_url is handled by the no-pr_url / shape-check branches below.
    # Compare NORMALIZED (owner/repo, number) coords via _GH_PR_URL_RE
    # rather than raw strings: the marker pr_url is agent-authored, so
    # cosmetic variants of the same PR (trailing slash, `/files`,
    # `?query`, `#frag`) must NOT read as a mismatch and block a
    # legitimate merge.
    envelope_pr_url = data.get('pr_url')
    _marker_m = (
        _GH_PR_URL_RE.search(pr_url) if isinstance(pr_url, str) else None
    )
    _env_m = (
        _GH_PR_URL_RE.search(envelope_pr_url)
        if isinstance(envelope_pr_url, str) else None
    )
    pr_url_mismatch = (
        _marker_m is not None
        and _env_m is not None
        and (_marker_m.group(1), _marker_m.group(2))
        != (_env_m.group(1), _env_m.group(2))
    )
    if pr_url_mismatch:
        log(
            f'AUTO_MERGE task={data.get("task_id", "?")} '
            f'outcome=failed reason=marker-envelope-pr-url-mismatch '
            f'marker_pr={pr_url!r} envelope_pr={envelope_pr_url!r} — '
            f'refusing to merge a PR other than the one Mirror was '
            f'dispatched to review agent=forge',
            'WARN',
        )
        marker_decision['merge_result'] = {
            'merge_outcome': 'failed',
            'merge_reason': (
                f'marker pr_url ({pr_url}) does not match the '
                f'dispatched review pr_url ({envelope_pr_url}); '
                f'auto-merge refused'
            ),
            'pr_number': '?',
            'repo_coords': '?',
        }
        marker_decision['merge_outcome'] = 'failed'
    elif pr_url:
        # Structural pr_url validator (2026-05-29 —
        # structural-pr-url-validator). Two layers run BEFORE the
        # serializer gates / `gh pr merge` shell-out:
        #   Layer 1 (shape) — regex match against
        #     `https://github.com/Larry-Yatch/<allowed>/pull/<N>`
        #     with N>=1. Cheap, deterministic, rejects garbage
        #     (`pull/0`, wrong-owner spoofs, fixture pointers).
        #   Layer 2 (existence) — `gh pr view --json state` with
        #     10s timeout. Confirms the PR is real AND state=OPEN.
        #     404 / timeout / non-OPEN state → skip without
        #     shell-out to `gh pr merge`.
        # Either skip is a SKIP outcome, not a failed outcome:
        # log a clean line, archive, and continue. No DM to Larry,
        # no marker-error notify back to Mirror — these are
        # operating-environment ground-truth violations (the URL
        # was structurally invalid or pointed at nothing), not
        # marker-discipline failures.
        task_id_log = data.get('task_id', '?')
        shape_repo, shape_pr_number, shape_reason = _pr_url_shape_check(pr_url)
        if shape_repo is None:
            log(
                f'AUTO_MERGE task={task_id_log} pr={pr_url!r} '
                f'outcome=skipped reason=pr-url-shape-invalid '
                f'({shape_reason}) agent=forge',
                'WARN',
            )
            return _skip(
                'skipped_shape_invalid',
                f'pr url shape invalid: {shape_reason}',
                'Auto-merge was SKIPPED — the PR URL is not a valid GitHub '
                'PR reference; the PR is NOT merged.',
            )
        # Layer 2 bypass when `_AUTO_MERGE_FN_OVERRIDE` is installed:
        # the integration-test classes use the override to mock the
        # merge path end-to-end and pre-date this validator; they
        # use known-good fixture URLs and don't expect a real
        # `gh pr view` shell-out. Production never sets the
        # override, so the existence check always runs.
        if _AUTO_MERGE_FN_OVERRIDE is not None:
            pr_state, exist_reason = 'OPEN', 'ok (test-override bypass)'
        else:
            pr_state, exist_reason = _pr_url_existence_state(
                shape_repo, shape_pr_number,
            )
        if pr_state is None:
            log(
                f'AUTO_MERGE task={task_id_log} pr={pr_url!r} '
                f'outcome=skipped reason=pr-not-found '
                f'({exist_reason}) agent=forge',
                'WARN',
            )
            return _skip(
                'skipped_not_found',
                f'pr not found / unreachable: {exist_reason}',
                'Auto-merge was SKIPPED — the PR could not be confirmed on '
                'GitHub (not found / timeout); the PR is NOT merged.',
            )
        if pr_state != 'OPEN':
            log(
                f'AUTO_MERGE task={task_id_log} pr={pr_url!r} '
                f'outcome=skipped reason=pr-state-{pr_state} '
                f'(already terminal) agent=forge',
            )
            if pr_state == 'MERGED':
                # Resume-after-crash: the PR was already merged on a prior
                # pass. gh confirms state=MERGED, so the notify may truthfully
                # say MERGED. Still a skip (no re-merge, no duplicate DM).
                return _skip(
                    'already_merged',
                    'PR already merged (resume from prior dispatch)',
                    'The PR is already MERGED on GitHub (no re-merge needed).',
                )
            return _skip(
                'skipped_terminal',
                f'pr state={pr_state} (not open)',
                f'Auto-merge was SKIPPED — the PR is {pr_state}, not open; '
                f'the PR is NOT merged.',
            )
        repo_coords, pr_number = shape_repo, shape_pr_number
        envelope_changed_files = data.get('changed_files')
        if not isinstance(envelope_changed_files, list):
            envelope_changed_files = None
        try:
            merge_result = _attempt_auto_merge_with_gates(
                pr_url=pr_url,
                repo_coords=repo_coords,
                pr_number=pr_number,
                task_id=data.get('task_id') or 'unknown',
                summary=(payload.get('summary') if isinstance(payload, dict) else '') or '',
                chat_id=data.get('reply_chat_id'),
                changed_files=envelope_changed_files,
            )
        except Exception as e:  # noqa: BLE001 — daemon-never-wedge
            log(
                f'AUTO_MERGE serializer raised on task '
                f'{data.get("task_id", "?")}: '
                f'{type(e).__name__}: {e}; rendering failed '
                f'outcome',
                'WARN',
            )
            merge_result = {
                'merge_outcome': 'failed',
                'merge_reason': (
                    f'serializer raised: {type(e).__name__}: {e}'
                ),
                'pr_number': pr_number,
                'repo_coords': repo_coords,
            }
        marker_decision['merge_result'] = merge_result
        marker_decision['merge_outcome'] = merge_result['merge_outcome']
        # V6 step-merged signal fires inside
        # `_attempt_auto_merge_with_gates` so every merge path
        # (marker-routing here, _queue_release, sweep
        # UNKNOWN-retry) propagates exactly once.
    else:
        # Mirror PASS without a pr_url is malformed; the marker
        # parser would normally catch this, but defensive — render
        # a failed-outcome DM so Larry sees the gap.
        log(
            f'mirror REVIEW_PASS on task {data.get("task_id", "?")} '
            f'has no pr_url on payload; cannot auto-merge — DM will '
            f'reflect this as a failed outcome.',
            'WARN',
        )
        marker_decision['merge_result'] = {
            'merge_outcome': 'failed',
            'merge_reason': 'Mirror PASS marker had no pr_url',
            'pr_number': '?',
            'repo_coords': '?',
        }
        marker_decision['merge_outcome'] = 'failed'
    # false-success-notify-fix (2026-06-11): render the GitHub-truth merge-
    # status line for the review-pass notify from the outcome computed above.
    # Overwrites the "requested/pending" default set at classification time.
    marker_decision['intent_kwargs'] = {
        **(marker_decision.get('intent_kwargs') or {}),
        'merge_status_line': _render_review_pass_merge_status_line(
            marker_decision.get('merge_result'),
        ),
    }
    return None


def process_outbox(outbox_file: Path) -> str:
    """Process one result outbox. Returns one of:
       'notified' | 'notified-marker' | 'archived-no-notify' | 'depth-cap' |
       'skip-self' | 'partial-json' | 'notify-failed' | 'marker-error' |
       'notified-replan' | 'notified-pulse-auto-dispatch' |
       'headless-approval-dispatched' | 'clarification-resume-dispatched' |
       'fixture-quarantined'.
    """
    # Outbox-side fixture gate. Mirrors inbox_watcher.py:415, Check III/VIII/IX,
    # and run_cycle.sh. The inbox-side gate archives the marker-error response
    # AFTER outbox_notifier has already parsed it and burned an Opus cycle
    # writing a fresh marker-error notify; this gate short-circuits the same
    # filename pattern BEFORE the marker parser runs. Closes the 23-burn
    # `t-bad-rev.*` and 54-burn `envelope-id.*` retry-loop cost leak documented
    # in the extend-fixture-gate-outbox-side dispatch. The match is filename-
    # only — `matched_fixture_envelope` peels `.<N>` collision suffixes and
    # routing-wrapper prefixes (`marker-error-`, `notify-`, `dead-letter-`),
    # so an `envelope-id.54.json` outbox or a `notify-t-pf-answer.json` wrapper
    # both quarantine without reading the file.
    if fixture_patterns.matched_fixture_envelope(outbox_file.stem) is not None:
        quarantine_dir = outbox_file.parent / '.fixture-quarantine'
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        dest = quarantine_dir / outbox_file.name
        outbox_file.rename(dest)
        log(
            f'FIXTURE_QUARANTINE outbox={outbox_file.name} '
            f'→ {quarantine_dir.name}/'
        )
        return 'fixture-quarantined'

    try:
        data = json.loads(outbox_file.read_text())
    except (OSError, json.JSONDecodeError):
        return 'partial-json'

    agent = data.get('agent', '')
    source = data.get('source', '')
    if not agent or not source:
        _archive_outbox(outbox_file)
        return 'archived-no-notify'

    # D3.5 commit 5a — Forge build-phase outbox carrying "PR opened: <url>"
    # triggers a `review-request` dispatch to Mirror. Fires BEFORE marker
    # classification so the dispatch happens even though Forge's build
    # response has no marker (markers are preflight-only per her CLAUDE.md
    # Build phase protocol). This is an additive dispatch — the notify to
    # Beacon below still fires via the default routing path, so Beacon
    # journals "PR opened" while Mirror starts her review.
    if agent == 'forge' and data.get('phase') == 'build':
        build_task_id = data.get('task_id', '')
        pr_url = _extract_pr_url_from_build_result(data.get('result', ''))
        if pr_url:
            # forge-post-open-mergeable-rebase-001 (Layer 2): a PR that opened
            # CONFLICTING because main advanced during the build must NOT go to
            # Mirror onto a doomed PR. Gate the review dispatch on mergeability —
            # on CONFLICTING this dispatches a phase=rebase back to Forge (+ opens
            # a durable obligation) and returns False so Mirror is skipped; Mirror
            # is dispatched on the rebase phase's re-check once MERGEABLE.
            if _handle_pr_mergeable_before_review(data, pr_url):
                _dispatch_mirror_review(data, pr_url)
            # push-signal-and-substatus (C): record `pr_url` + flip the
            # sequence step to the `reviewing` substatus at PR-OPEN, not only at
            # merge. Lights up the advancer's dual-gate `gh` leg during review
            # and the stall backstop's open-PR-in-review distinguisher. No-op
            # for non-sequence tasks. Fires regardless of the rebase gate — the
            # PR is open either way; the rebase obligation covers the stall case.
            _signal_sequence_step_pr_opened(build_task_id, pr_url)
        else:
            # No `PR opened:` line. Forge may have HONESTLY refused to open one
            # because this slice already merged via another path (a concurrent
            # session, a manual merge): `git diff main..HEAD` is empty, no delta
            # to commit. Recognize that outcome, gh-verify the PR she names is
            # genuinely MERGED, and flip the sequence step terminal NOW — instead
            # of letting it strand `dispatched` until the 4h stall backstop pages
            # Larry to reconcile by hand (the 2026-06-20 incident). A genuine
            # build failure (no merged PR named) falls through unchanged and the
            # stall backstop still escalates it as a real failure.
            reconciled = _maybe_reconcile_already_merged_build(data)
            # push-signal-and-substatus (B): a build that opened NO PR and is
            # NOT an honest already-merged no-delta (reconciled is None) AND
            # exited non-zero is a genuine build crash — fail the step + pause
            # the sequence now rather than stranding it `dispatched` for the 4h
            # stall backstop (which would misattribute "never picked up").
            # exit_code == 0 with no merged PR is an ambiguous clean refusal,
            # left to fall through unchanged (conservative).
            if reconciled is None and data.get('exit_code') != 0:
                _signal_sequence_step_failed(
                    build_task_id,
                    f'Forge build crashed (exit_code='
                    f'{data.get("exit_code")}) and opened no PR',
                )
        # Build phase emits no marker (preflight-only); the guarded classifier
        # below leaves marker_decision None and the default routing path takes
        # over (Beacon notify with the full build result narrative).

    # forge-post-open-mergeable-rebase-001 (Layer 2) — Forge rebase-phase outbox.
    # Forge re-ran under --resume after the build-phase mergeable gate found the
    # PR CONFLICTING, rebased onto origin/main, force-pushed, and re-emitted
    # `PR updated:`. Re-check mergeability: dispatch Mirror once MERGEABLE
    # (resolving the durable obligation), or re-dispatch a higher rebase round if
    # main re-advanced (bounded by _REBASE_MAX_ROUNDS). When Forge instead
    # aborted a conflicted rebase and emitted a BLOCKER PARAGRAPH (no
    # `PR updated:` line), there's no pr_url here — the obligation stays OPEN
    # (healer backstop) and the default routing path below returns the blocker to
    # Beacon, exactly like a build-phase blocker.
    if agent == 'forge' and data.get('phase') == 'rebase':
        rebase_task_id = data.get('task_id', '')
        pr_url = _extract_pr_url_from_build_result(data.get('result', ''))
        if pr_url:
            if _handle_pr_mergeable_before_review(data, pr_url, is_rebase_phase=True):
                _dispatch_mirror_review(data, pr_url)
            _signal_sequence_step_pr_opened(rebase_task_id, pr_url)
        # No `PR updated:` line → Forge surfaced a rebase-conflict blocker. Leave
        # the obligation OPEN; the default routing path returns the blocker to
        # Beacon and the healer fires if it fails to route.

    # D3.5 commit 5b — Forge revision-phase outbox. Strict gate per Larry's
    # signoff (Option 3 — strict on revision, lenient on build):
    #   - "Revision N applied: <summary>" preamble found → dispatch a
    #     re-review to Mirror with revision_count incremented.
    #   - Preamble missing → raise MalformedForgeMarker so the marker-error
    #     cascade fires and Forge gets a sharp "use the required preamble"
    #     prompt back. Unlike build phase, revision phase has no documented
    #     blocker-paragraph alternative; the structure is mandatory.
    # The Beacon notify still fires via the default routing path below
    # (Beacon journals the revision narrative); the re-review dispatch is
    # additive.
    if agent == 'forge' and data.get('phase') == 'revision':
        parsed = _extract_revision_summary_from_result(data.get('result', ''))
        if parsed is None:
            # Strict gate: revision phase MUST start with the preamble.
            log(
                f'forge revision-phase outbox without "Revision N applied:" '
                f'preamble: {outbox_file.name}; treating as marker-error',
                'WARN',
            )
            _round = data.get('revision_count')
            _n = _round if isinstance(_round, int) and _round >= 1 else None
            _round2_trap = (
                'ROUND-2+ TRAP: this is a resumed conversation. Even though a '
                + (f'"Revision {_n - 1} applied:" ' if _n and _n > 1
                   else '"Revision N-1 applied:" ')
                + 'line appears earlier in this conversation, THIS response must '
                + (f'START with "Revision {_n} applied:". ' if _n
                   else 'START with "Revision N applied:". ')
                + 'The gate is anchored to the start of THIS response only — '
                'the earlier round\'s preamble does NOT satisfy it. Do NOT open '
                'with a conversational acknowledgement of the findings; the '
                'preamble must be the VERY FIRST characters of your reply.'
            )
            _notify_forge_marker_error(
                data,
                'phase=revision requires response to START with '
                '"Revision N applied: <one-line summary>" preamble — none '
                'found. Re-read agents/forge/CLAUDE.md Revision phase '
                'protocol — the preamble is the structural signal that '
                'revision completed; the rest of the response is narrative '
                'underneath. ' + _round2_trap,
            )
            _archive_outbox(outbox_file)
            return 'marker-error'
        round_num, summary = parsed
        # D3.5 5b M-3 review fix: validate round_num against the envelope's
        # expected revision_count. Forge writing "Revision 0 applied:" (no
        # such round exists; first revision is round 1), "Revision 99
        # applied:" (force-exhaust the budget via misreport), or repeating
        # "Revision 1 applied:" twice (round drift) all reach this gate.
        # Mismatch dead-letters via marker-error with a precise message
        # so Forge re-emits with the correct number from the envelope.
        envelope_round = data.get('revision_count')
        if round_num < 1:
            log(
                f'forge revision-phase outbox with non-positive round '
                f'(N={round_num}) in preamble: {outbox_file.name}; '
                f'treating as marker-error',
                'WARN',
            )
            _notify_forge_marker_error(
                data,
                f'Revision preamble round number must be ≥ 1 (you wrote '
                f'"Revision {round_num} applied:"). The first revision is '
                f'round 1; consecutive revisions increment. Read the '
                f'envelope\'s `revision_count` field and use that number.',
            )
            _archive_outbox(outbox_file)
            return 'marker-error'
        if isinstance(envelope_round, int) and round_num != envelope_round:
            log(
                f'forge revision-phase outbox round mismatch: preamble '
                f'says N={round_num}, envelope says revision_count='
                f'{envelope_round} ({outbox_file.name}); treating as '
                f'marker-error',
                'WARN',
            )
            _notify_forge_marker_error(
                data,
                f'Revision preamble round number ({round_num}) does not '
                f'match the envelope\'s `revision_count` ({envelope_round}). '
                f'Use the envelope number — that\'s the round the dispatch '
                f'is for. Re-emit "Revision {envelope_round} applied: ..." '
                f'with the same summary content.',
            )
            _archive_outbox(outbox_file)
            return 'marker-error'
        _dispatch_mirror_review_rerun(data, round_num, summary)

    # D3.5 commit 5c — Beacon auto-replan check. Beacon's outbox in response
    # to a review-escalate inbox dispatch may contain a `=== APPROVAL_REQUEST
    # ===` marker (her revised plan). The notifier impersonates the bot's
    # chat-mode approval flow (extract → trust policy → add_pending → queue
    # alert) so the bot's existing alerts poll surfaces the approval DM to
    # Larry. If no marker or marker fails discipline, falls through to
    # default routing — Beacon's narrative still reaches Mirror as
    # informational. The auto-DM Larry received on the Mirror→Beacon hop
    # (via 5a-followup's review-escalate DM_TEMPLATE) already gave him the
    # initial signal; this just adds the auto-replan approval prompt on
    # top when Beacon decides to revise.
    if (
        agent == 'beacon'
        and data.get('inbound_intent') in _BEACON_REPLAN_INBOUND_INTENTS
    ):
        if _route_beacon_replan_approval(data):
            _archive_outbox(outbox_file)
            return 'notified-replan'

    # Closed-loop step 4 (2026-05-24) — Beacon outbox responding to a
    # Pulse-auto-dispatch envelope. Pulse drops a dispatch brief into
    # Beacon's inbox with source='pulse-auto-dispatch'; Beacon emits an
    # APPROVAL_REQUEST in her outbox; this branch extracts it and routes
    # through the trust_policy → larry-DM approval pipeline so Larry sees
    # the spec marker and replies approve/modify/reject. Distinct from the
    # 5c replan branch (no Mirror reason, no replan budget) and the
    # headless source='larry' branch (trust policy IS consulted because
    # Pulse's judgment is not implicit Larry approval).
    if agent == 'beacon' and source in _BEACON_AUTO_DISPATCH_SOURCES:
        if _route_beacon_pulse_auto_dispatch_approval(data):
            _archive_outbox(outbox_file)
            return 'notified-pulse-auto-dispatch'

    # fix-depth1-pulse-approval-extraction-001 (2026-06-12) — Beacon outbox
    # responding to a Pulse *direction-ask* (a depth=1 beacon-result with
    # source='pulse', NOT 'pulse-auto-dispatch'). When Pulse asks Beacon a
    # question and Beacon answers by proposing a task via an APPROVAL_REQUEST
    # marker, that marker previously matched NEITHER the auto-dispatch set
    # (above) NOR the trusted set (larry/orchestrator, below), so it fell
    # through to a plain notify-back-to-Pulse and was silently dropped — the
    # gap that stranded `fix-alert-triage-watermark-durability-001`. Route it
    # through the SAME extraction + trust_policy + add_pending pipeline as
    # pulse-auto-dispatch, with two direction-ask accommodations: (1) the
    # marker proposes a NEW task so its task_id legitimately differs from the
    # question envelope's (enforce_task_id_match=False, mirroring the headless
    # path); (2) direction-ask envelopes carry reply_chat_id=null, so fall
    # back to the default Larry chat rather than dropping the approval.
    # Trust policy is still consulted (source='pulse'), so a force_ask still
    # reaches Larry — no auto-approve. A markerless / malformed result returns
    # False and falls through to default Pulse-notify routing unchanged.
    if agent == 'beacon' and source == 'pulse':
        if _route_beacon_pulse_auto_dispatch_approval(
            data,
            policy_source='pulse',
            chat_id_fallback=_primary_chat_id(),
            enforce_task_id_match=False,
        ):
            _archive_outbox(outbox_file)
            return 'notified-pulse-direction-ask'

    # Board-delegate dispatch route (autonomy-visibility keystone, 2026-06-21)
    # — Beacon outbox responding to a board "Delegate to team" envelope
    # (source='dashboard'). The dashboard delegate endpoint
    # (_handle_capture_delegate) and the board-drain (drain_board_to_beacon)
    # both drop a proposal into Beacon's inbox with source='dashboard'; Beacon
    # scopes it and emits an APPROVAL_REQUEST marker targeting Forge. Before
    # this branch that marker matched NEITHER the auto-dispatch set (pulse)
    # NOR the trusted set (larry/orchestrator, below), so it fell through to a
    # dead-end notify and the proposal NEVER reached Forge — the gap that left
    # the board-drain scoping work that never built (found live 2026-06-21)
    # and the manual Delegate button incomplete (missions-v2-delegate-fix.md).
    #
    # Route it through the SAME trust-gated extraction pipeline as the pulse
    # paths, with three dashboard accommodations:
    #   - policy_source='beacon': evaluate trust as a Beacon→Forge dispatch so
    #     the live agent-core auto_approve rule (source=beacon) applies — the
    #     board's whole purpose is to feed the team's autonomous lane. This
    #     mirrors approval.trust_decision, which hardcodes source='beacon' for
    #     the chat path; the autonomy_decision chain_event is recorded with
    #     source='beacon' too, consistent with the chat dispatch it matches.
    #   - chat_id_fallback=_primary_chat_id(): board envelopes carry
    #     reply_chat_id=null (no chat thread), so a force_ask DM / auto-approve
    #     confirmation / rejection falls back to the default Larry chat rather
    #     than dropping the approval.
    #   - enforce_task_id_match=False: the marker proposes a NEW scoped task
    #     whose task_id legitimately differs from the `delegate-{capture_id}`
    #     envelope task_id (mirrors the direction-ask + headless paths).
    # A markerless / non-Forge dashboard result returns False and falls through
    # to default routing unchanged.
    if agent == 'beacon' and source == 'dashboard':
        if _route_beacon_pulse_auto_dispatch_approval(
            data,
            policy_source='beacon',
            chat_id_fallback=_primary_chat_id(),
            enforce_task_id_match=False,
        ):
            _archive_outbox(outbox_file)
            return 'notified-board-delegate'

    # Task #17 (2026-05-19) — headless Beacon APPROVAL_REQUEST handler.
    # When Claude in a Larry-session drops a dispatch envelope into Beacon's
    # inbox (source='larry'), her result text may contain an APPROVAL_REQUEST
    # marker. The chat-mode bot path doesn't fire on outbox-derived markers,
    # and the 5c replan path is gated to inbound_intent=review-escalate, so
    # without this handler the marker would sit in Beacon's archive doing
    # nothing — the failure shape that required three manual bridges in PR
    # #46 + PR #47 on 2026-05-19. The handler auto-translates Beacon's
    # marker into a Forge preflight task. Trust policy is NOT consulted —
    # implicit Larry-session approval covers the headless case. Fires
    # BEFORE marker classification so Beacon's non-Forge-marker result text
    # doesn't fall through to default notify-to-self routing.
    if agent == 'beacon' and source in _BEACON_TRUSTED_DISPATCH_SOURCES:
        # PR-S4 (orchestrator workstream) — multi-step build sequence
        # kickoff. Fires BEFORE the headless-approval-request handler
        # because the kickoff marker targets the build_sequence_advancer
        # daemon (status transition only), not Forge. Routing is keyed on
        # `payload.target_agent == 'build_sequence_advancer'`; markers with
        # any other target_agent (including the default `forge`) return
        # None here and fall through unchanged.
        #
        # PR-S4 rectification (H3): source gate widened from `larry`-only
        # to `{larry, orchestrator}` so the advancer's step envelopes
        # (source='orchestrator') reach the headless-approval translator.
        kickoff_dispatched = _handle_build_sequence_advancer_kickoff(
            data, data.get('result', '') or '',
        )
        if kickoff_dispatched is not None:
            _archive_outbox(outbox_file)
            return 'sequence-kickoff-handled'

        dispatched = _handle_beacon_headless_approval_request(
            data, data.get('result', '') or '',
        )
        if dispatched is not None:
            _archive_outbox(outbox_file)
            return 'headless-approval-dispatched'

    # Forge preflight marker check. Markers override default routing rules
    # because the preflight protocol is intentionally multi-hop and the
    # clarification budget on the envelope guards termination.
    #
    # resumed-session-stale-marker guard: markers are PREFLIGHT-only (per
    # agents/forge/CLAUDE.md — build opens a PR or narrates a blocker; revision
    # leads with "Revision N applied:"; neither emits a PROCEED/CLARIFY/REJECT
    # block). Both the build AND revision phases RESUME the preflight `claude`
    # session (revision --resumes forge_build_session_id, which itself resumed
    # preflight), so the session-log scan inside `_classify_forge_marker` would
    # re-discover the STALE preflight `=== PROCEED ===` still sitting earlier in
    # that resumed transcript and mis-route the outbox as a fresh PROCEED —
    # re-dispatching the build (idempotency-skipped) and emitting NO terminal
    # signal, the exact strand the 2026-06-20 incident hit. Skip classification
    # for the resumed phases: a build outbox is handled above (PR opened ->
    # Mirror review; no PR -> already-merged reconcile) and a revision outbox is
    # handled above (re-review dispatch); both must fall through to the default
    # Beacon notify.
    marker_decision: Optional[dict[str, Any]] = None
    if agent == 'forge' and data.get('phase') not in ('build', 'revision'):
        try:
            marker_decision = _classify_forge_marker(data)
        except (fph.MalformedForgeMarker, fph.MultipleForgeMarkers) as e:
            log(
                f'forge marker error in {outbox_file.name}: '
                f'{type(e).__name__}: {e}',
                'WARN',
            )
            _notify_forge_marker_error(data, str(e))
            _archive_outbox(outbox_file)
            return 'marker-error'
        # E4.4e PR-A — emit a `clarify_request` chain_event when Forge's
        # marker classified as an in-budget clarify. Gate is `intent ==
        # 'clarify'` (NOT marker_type == 'clarify_request') so the over-
        # budget `clarification-exhausted` case — which is structurally a
        # reject — doesn't generate a clarify_request row. Emission lives
        # past the classifier so partial/unparseable markers (caught
        # above as MalformedForgeMarker) never reach here, satisfying the
        # spec § 4 source #5 contract that clarify_request fires only on
        # classified markers.
        if marker_decision and marker_decision.get('intent') == 'clarify':
            _emit_clarify_request_chain_event(data, marker_decision, agent='forge')
        # check-x-verdict-emission — record the preflight OUTCOME mix
        # (proceed/clarify/reject) for Check X + the dashboard. Additive +
        # best-effort; fires for every classified marker (the clarify_request
        # emit above is narrower — only the in-budget clarify question).
        if marker_decision:
            _emit_preflight_outcome_chain_event(data, marker_decision, agent='forge')
            # §5.2: feed (or self-clear) the durable for-Larry signal for a
            # CLARIFY-exhausted build. Rides this same classified-marker path
            # (write on exhausted; clear on any other marker = re-dispatch
            # observed) so no second poll is introduced.
            _sync_clarify_exhausted_signal(data, marker_decision)
            # push-signal-and-substatus (B): a terminal preflight REJECT fails
            # the build-sequence step + pauses the sequence NOW, instead of
            # stranding it `dispatched` until the advancer poll / 4h backstop.
            # `_preflight_outcome_event_type` folds the over-budget
            # clarification-exhausted case (structurally a reject) into
            # `preflight_reject`; a PROCEED or in-budget CLARIFY is not a
            # failure. No-op for non-sequence tasks.
            if _preflight_outcome_event_type(marker_decision) == 'preflight_reject':
                _signal_sequence_step_failed(
                    data.get('task_id', ''),
                    f'Forge preflight REJECT '
                    f'(marker_type={marker_decision.get("marker_type")}, '
                    f'intent={marker_decision.get("intent")})',
                )

    # PR-S4 rectification (H1) — Mirror DAG-preflight result handler.
    # Fires BEFORE the regular Mirror marker classifier because DAG
    # preflight outputs `result: PASS|REVISION` in the chat body and
    # never emit REVIEW_* markers (per agents/mirror/CLAUDE.md:362-368).
    # The handler discriminates on the envelope's `prompt` field
    # (`review-sequence-dag <seq-id>` prefix) and consumes the outbox
    # on match — caller archives, no fall-through.
    if agent == 'mirror':
        dag_result = _handle_mirror_dag_preflight_result(data)
        if dag_result is not None:
            _archive_outbox(outbox_file)
            return 'mirror-dag-preflight-handled'

    # D3.5 commit 5a — Mirror review marker check. Parallel to the Forge
    # branch above; same return shape from the classifier so the marker-
    # decision routing block below handles both transparently.
    if agent == 'mirror':
        try:
            marker_decision = _classify_mirror_marker(data)
        except (mrh.MalformedMirrorMarker, mrh.MultipleMirrorMarkers) as e:
            log(
                f'mirror marker error in {outbox_file.name}: '
                f'{type(e).__name__}: {e}',
                'WARN',
            )
            _notify_mirror_marker_error(data, str(e))
            _archive_outbox(outbox_file)
            return 'marker-error'
        # Auto-promoted REVIEW_REVISION (low confidence → escalate) — log
        # so the audit trail captures Mirror's original verdict alongside
        # the system's routing decision.
        if marker_decision and marker_decision.get('auto_promoted'):
            log(
                f'mirror REVIEW_REVISION auto-promoted to ESCALATE for task '
                f'{data.get("task_id", "?")} (confidence=low)',
            )
        # D3.5 5d — REVIEW_EMERGENCY_HALT now TRIPS the halt-file +
        # priority-DMs Larry via the broadcast `kind: alert` channel.
        # Fires BEFORE the routine notify-to-Beacon below so the halt
        # file is present before any other dispatch on the next poll
        # (which would honor _emergency_halt_active() and exit cleanly).
        # Beacon's notify still goes out for the journal entry; she sees
        # the halt was tripped automatically via Shape 9 wording.
        if marker_decision and marker_decision['marker_type'] == 'review_emergency_halt':
            _trip_emergency_halt(data, marker_decision.get('payload') or {})
        # check-x-verdict-emission — record the Mirror verdict mix
        # (PASS/REVISION/ESCALATE) for Check X + the dashboard. Placed here
        # (before the routing/auto-merge block) so a PASS is recorded at the
        # verdict moment even when the merge is later held in the auto-merge
        # queue. Additive + best-effort; never blocks the flow below.
        if marker_decision:
            _emit_mirror_verdict_chain_event(data, marker_decision, agent='mirror')
            # build-mirror-review-status — POST the `mirror-review` commit
            # status for this verdict. Placed here (same spot that classifies
            # the marker) so a REVIEW_PASS success status is on the head SHA
            # BEFORE the auto-merge block below fires; REVISION / ESCALATE /
            # EMERGENCY_HALT post a failure status that keeps the PR blocked.
            # Best-effort — never raises, never blocks the merge flow.
            _post_mirror_review_commit_status(data, marker_decision)
            # build-mirror-findings-comment — Contract A. On a non-PASS verdict
            # (REVIEW_REVISION / REVIEW_ESCALATE), also post/update Mirror's
            # findings as a durable PR comment so they're visible session-or-not
            # (mirror-review-visibility.md § 4). Idempotent; best-effort.
            _post_mirror_findings_comment(data, marker_decision)

        # merged-PR REVIEW_REVISION guard (the #764 desktop-merge race,
        # 2026-06-30). A REVIEW_REVISION whose PR was MERGED/CLOSED between the
        # review's dispatch and its (queue-delayed) run is moot: escalating it
        # pages Larry for a decision that's already settled (the live incident:
        # "Session-less PR … needs your decision"), and dispatching a Forge
        # revision is wasted work on a merged branch. The dispatch-time
        # `_mirror_review_target_is_terminal` guard can't catch this — the review
        # was QUEUED while the PR was still OPEN and only RAN after a desktop
        # `merge_reviewed_pr.sh` merge. Terminally reconcile (resolve any open
        # no-session obligation + clear any stale for-Larry record) and skip ALL
        # routing below — the no-session escalate, the Forge revision dispatch,
        # AND the (now-misleading) Beacon back-leg notify.
        #
        # Fail-OPEN: `_review_revision_pr_is_merged` is True ONLY on a positively
        # observed MERGED/CLOSED; a gh error or missing pr_url returns False and
        # falls through to the existing escalate/dispatch path, so a flaky probe
        # never silently swallows a real revision. Multi-repo: the PR identity is
        # taken from the task's pr_url/target_repo, never a hardcoded repo.
        if (
            marker_decision is not None
            and marker_decision.get('marker_type') == 'review_revision'
            and _review_revision_pr_is_merged(data, marker_decision)
        ):
            _merged_task_id = data.get('task_id') or 'unknown'
            _merged_payload = marker_decision.get('payload') or {}
            _merged_pr_url = data.get('pr_url') or (
                _merged_payload.get('pr_url')
                if isinstance(_merged_payload, dict) else None
            )
            log(
                f'REVIEW_REVISION_ALREADY_MERGED_SKIP task={_merged_task_id} '
                f'pr={_merged_pr_url} — PR is MERGED/CLOSED on GitHub; not '
                f'escalating to Larry and not dispatching a Forge revision (the '
                f'#764 queue-delayed-review-after-desktop-merge race). '
                f'Reconciling the review task: resolving any open no-session '
                f'obligation + clearing any stale for-Larry record.',
            )
            # Both idempotent no-ops when nothing is open; neither raises (mirror
            # of the terminal-verdict reconcile path further below).
            no_session_ledger.resolve_obligation(
                _merged_task_id, resolution='already-merged',
            )
            for_larry_escalations.clear(_no_session_record_id(_merged_task_id))
            _archive_outbox(outbox_file)
            return 'review-revision-already-merged'

    if marker_decision is not None:
        # Marker-driven routing. Always targets the original dispatcher
        # (Beacon today). If this outbox came from a marker-error retry,
        # `source` is the infra source `outbox-notifier` which has no
        # primary_agent_id — fall back to the propagated `original_source`
        # so the recovered marker reaches the right agent.
        routing_source = data.get('original_source') or source
        target_agent = _primary_agent_id(routing_source)
        # E1.5.2 source-routing fix: when Larry dispatches an agent directly
        # (no upstream Beacon hop) and propagates reply_chat_id, the marker
        # has no agent target but should still (a) DM Larry the result and
        # (b) fire auto-merge if the marker is review_pass. Without this
        # branch, larry-direct review dispatches silently archive and the
        # E1.3 heal_pr_auto_merge healer has to clean up — exactly what
        # PR #45 surfaced live on 2026-05-19.
        chat_id = data.get('reply_chat_id')
        larry_direct = (
            target_agent is None
            and routing_source == 'larry'
            and isinstance(chat_id, int)
        )
        if (target_agent is None or target_agent == agent) and not larry_direct:
            # Can't route back (system source with no original_source) or
            # self-loop — archive.
            log(
                f'marker present but no routable target '
                f'(source={source}, original_source={data.get("original_source")}, '
                f'agent={agent}); archiving',
                'WARN',
            )
            _archive_outbox(outbox_file)
            return 'archived-no-notify'

        if larry_direct:
            log(
                f'larry-direct dispatch (source={source}, '
                f'intent={marker_decision["intent"]}, '
                f'chat={chat_id}); skipping inter-agent notify, '
                f'continuing to dispatch helpers + auto-merge + Larry DM',
            )
            # task-19 (2026-05-19) — narrow synth-DM trigger.
            # PR #46's source-routing fix originally synth-DM'd every non-
            # terminal intent for source='larry', which hijacked Forge's
            # `ack-proceed` (PROCEED) and Mirror's clean `review-revision`
            # by rendering a wrong-template DM AND skipping the existing
            # dispatch helpers (`_dispatch_build_phase`,
            # `_dispatch_revision_to_forge`).
            #
            # Marker → existing handler matrix when source='larry':
            #   PROCEED                  → _dispatch_build_phase (no synth)
            #   REVIEW_REVISION (clean)  → _dispatch_revision_to_forge (no synth)
            #   REVIEW_REVISION auto/exh → intent overridden to review-escalate
            #                              (terminal DM via _maybe_dm_larry)
            #   REVIEW_PASS              → auto-merge + terminal DM
            #   REVIEW_ESCALATE          → terminal DM via _maybe_dm_larry
            #   REVIEW_EMERGENCY_HALT    → priority broadcast via
            #                              _trip_emergency_halt
            #   REJECT / CLARIFY_EXHAUST → terminal DM via _maybe_dm_larry
            #   CLARIFY_REQUEST          → no dispatcher; synth DM with
            #                              clarify-specific body
            mtype = marker_decision['marker_type']
            has_followup_dispatch = (
                (mtype == 'proceed' and agent == 'forge')
                or (
                    mtype == 'review_revision' and agent == 'mirror'
                    and not marker_decision.get('auto_promoted')
                    and not marker_decision.get('budget_exhausted')
                )
            )
            if (
                marker_decision['intent'] not in TERMINAL_DM_INTENTS
                and isinstance(chat_id, int)
                and not has_followup_dispatch
            ):
                _maybe_dm_larry_direct_synth(data, marker_decision)

        task_id = data.get('task_id', outbox_file.stem)

        # false-success-notify-fix (2026-06-11): for a Mirror REVIEW_PASS,
        # attempt the auto-merge BEFORE building the back-leg notify so the
        # notify reports the gh-confirmed merge state (merged / queued-behind-
        # blocker / conflict / failed), never an optimistic "auto-merge fired".
        # Incident: PR #455 was held behind #454, yet Beacon was told the merge
        # fired and reported a merge that never happened. `_run_review_pass_
        # auto_merge` attaches `merge_result` + the rendered `merge_status_line`
        # to marker_decision; the notify template + Larry's closing DM both read
        # from it. A 'auto-merge-skipped' return (degenerate PR: shape-invalid
        # url / 404 / already-terminal) preserves the pre-fix skip semantics —
        # the notify STILL fires (now truthful), but the closing DM is
        # suppressed and process_outbox returns 'auto-merge-skipped' below. The
        # proceed/revision dispatch helpers never fire for review_pass, so this
        # reorder changes nothing for other marker types.
        review_pass_skip: Optional[str] = None
        if (
            marker_decision['marker_type'] == 'review_pass'
            and agent == 'mirror'
        ):
            review_pass_skip = _run_review_pass_auto_merge(
                data, marker_decision, outbox_file,
            )

        # nervous-system-audit #12 (2026-06-05): the back-leg inter-agent
        # notify is INFORMATIONAL (the upstream agent journals the result).
        # A notify failure must NOT abort the chain's substantive actions —
        # auto-merge, build/revision dispatch — which run further below. Track
        # the failure in a flag and surface it in the final return value
        # instead of early-returning before those blocks.
        notify_failed = False
        prompt = build_notify_prompt(
            intent=marker_decision['intent'],
            sender=agent,
            task_id=task_id,
            success=data.get('exit_code', 0) == 0,
            output=_marker_output_for_prompt(data, marker_decision),
            error=data.get('error') or '',
            intent_kwargs=marker_decision['intent_kwargs'],
        )
        notify_base: dict[str, Any] = {
            'task_id': f'notify-{task_id}',
            'prompt': prompt,
            'source': marker_decision['notify_source'],
            'intent': marker_decision['intent'],
            # Depth still tracked for telemetry; budget supersedes the cap.
            '_notify_depth': _current_notify_depth(data) + 1,
        }
        if data.get('claude_session_id'):
            notify_base['session_id'] = data['claude_session_id']
        # task-25 (2026-05-20) — Forge's session is also stashed under a
        # distinct field that survives Beacon's round-trip via
        # inbox_watcher._build_outbox propagation. The `session_id` field
        # above goes to Beacon's notify but `_build_outbox` doesn't include
        # `session_id` in its envelope_fields list, so it's lost on Beacon's
        # outbox. Without `forge_session_id` riding through, the
        # clarification-response leg back to Forge has no way to --resume
        # her original preflight session, and the watcher creates a fresh
        # `notify-notify-{task}` worktree (chain-routing gap #5). Only set
        # when the agent emitting the marker is Forge; Mirror markers don't
        # need this hop since her revision cascade uses
        # `forge_build_session_id` instead.
        if agent == 'forge' and data.get('claude_session_id'):
            notify_base['forge_session_id'] = data['claude_session_id']
        # Propagate clarification budget so the next leg has the counter.
        if marker_decision['next_clarification_count'] is not None:
            notify_base['clarification_count'] = marker_decision['next_clarification_count']
        if data.get('max_clarifications') is not None:
            notify_base['max_clarifications'] = data['max_clarifications']
        # Phase D3 commit 4b post-test-2 fix: propagate branch + pr_title/pr_body
        # forward across the full clarification cascade (forge→beacon question,
        # then beacon→forge answer, then forge re-preflight). target_repo/pr_url
        # are whitelisted context and carry through the builder below. Without
        # these on the notify task, _build_outbox on Beacon's side has nothing
        # to propagate, the answer leg arrives at Forge with target_repo=None,
        # and the watcher's worktree gate refuses with "no canonical path".
        for f_name in ('branch', 'pr_title', 'pr_body'):
            if data.get(f_name):
                notify_base[f_name] = data[f_name]
        # D3.5 5c — when the marker decision is review-escalate (any of three
        # sub-flavors: direct REVIEW_ESCALATE, auto-promoted from low-
        # confidence REVISION, or budget-exhausted REVISION), surface the
        # replan budget + Mirror's reason on the notify task so Beacon's
        # CLAUDE.md decision tree has the data it needs without re-reading
        # her inbox archive. replan_count/max_replans are whitelisted context
        # resolved through the builder (DROP unless this is an escalate);
        # `mirror_escalate_reason` is non-whitelisted narrative set after the
        # build, riding forward through _build_outbox propagation so the
        # notifier can apply the level-3 discipline gate when Beacon emits her
        # replan APPROVAL_REQUEST.
        escalate_replan_count: Any = DROP
        escalate_max_replans: Any = DROP
        escalate_reason = ''
        if marker_decision['intent'] == 'review-escalate':
            escalate_replan_count = data.get('replan_count', 0) or 0
            max_replans = data.get('max_replans')
            if not isinstance(max_replans, int) or max_replans < 0:
                max_replans = _load_max_replans_from_config()
            escalate_max_replans = max_replans
            reason = marker_decision.get('intent_kwargs', {}).get('reason', '')
            # C-2 review fix: when the underlying marker was REVIEW_REVISION
            # (auto_promoted or budget_exhausted), the `reason` text built by
            # mrh.build_auto_promote_reason / build_budget_exhausted_reason is
            # PROCEDURAL framing ("Mirror emitted REVISION with low confidence
            # ... auto-promoted to ESCALATE"), not semantic finding content.
            # Beacon's level-3 discipline gate compares her summary tokens
            # against `mirror_escalate_reason` — without finding text, her
            # good-faith replan summary will always fail. Augment the reason
            # with finding descriptions so the gate has semantic signal.
            payload = marker_decision.get('payload') or {}
            if (
                (marker_decision.get('auto_promoted')
                 or marker_decision.get('budget_exhausted'))
                and isinstance(payload.get('findings'), list)
            ):
                finding_descs = []
                for f in payload['findings']:
                    if isinstance(f, dict) and f.get('description'):
                        finding_descs.append(str(f['description']))
                if finding_descs:
                    reason = (
                        f'{reason} Findings: ' + ' | '.join(finding_descs)
                    )
            escalate_reason = reason
        notify_task = build_chain_envelope(
            notify_base, data,
            carry={
                'reply_chat_id': CARRY,
                'target_repo': CARRY,
                'pr_url': CARRY,
                'replan_count': escalate_replan_count,
                'max_replans': escalate_max_replans,
                'revision_count': DROP,
                'forge_build_session_id': DROP,
            },
        )
        if escalate_reason:
            notify_task['mirror_escalate_reason'] = escalate_reason

        # task-19 (2026-05-19) — gate ONLY the back-leg inter-agent notify
        # on `not larry_direct`. The dispatch helpers below
        # (`_dispatch_build_phase`, `_dispatch_revision_to_forge`) write
        # into a different agent's inbox to advance the chain; they don't
        # depend on having an upstream agent to notify, so they MUST fire
        # for source='larry' too. PR #46 incorrectly hid them under the
        # `not larry_direct` gate, which is what caused Forge's PROCEED on
        # task-17's larry-direct preflight to silently skip build-phase
        # dispatch (Larry had to manually bridge the build envelope).
        #
        # forge-cold-start-revision S2: when a Mirror REVIEW_REVISION carries
        # no forge_build_session_id, `_dispatch_revision_to_forge` (below)
        # mechanically dispatches a FRESH Forge revision (no `--resume`) with a
        # full cold-start brief — there is no session to resume, and Beacon is
        # intentionally kept OUT of the cold-start loop (the old LLM-mediated
        # Beacon route was removed). Suppress THIS generic back-leg notify in
        # that case: its body says "revision auto-dispatched to Forge, just
        # journal," which would now double-signal the mechanical dispatch the
        # cold-start path already performs.
        # Only the genuine revision-dispatch path is suppressed — an
        # auto_promoted / budget_exhausted REVISION downgrades to
        # review-escalate (intent override above) and keeps its back-leg
        # notify, since no cold-start dispatch fires for it.
        suppress_no_session_backleg = (
            marker_decision['marker_type'] == 'review_revision'
            and agent == 'mirror'
            and not marker_decision.get('auto_promoted')
            and not marker_decision.get('budget_exhausted')
            and not data.get('forge_build_session_id')
        )
        if not larry_direct and not suppress_no_session_backleg:
            notify_filename = f'notify-{outbox_file.stem}.json'
            try:
                dest = safe_write_inbox.safe_write_inbox(
                    target_agent=target_agent,
                    task_dict=notify_task,
                    source_agent=marker_decision['notify_source'],
                    filename=notify_filename,
                )
                log(
                    f'marker-notified {target_agent} <- {agent} '
                    f'({marker_decision["notify_source"]}, '
                    f'intent={marker_decision["intent"]}, file={dest.name})'
                )
            except (
                safe_write_inbox.DispatchRejected,
                safe_write_inbox.RoutingDenied,
            ) as e:
                # #12: do NOT archive + return here — that dead-ended the
                # outbox BEFORE the auto-merge block, so a review_pass PR
                # never merged AND no `AUTO_MERGE ... failed` line was logged,
                # leaving heal_pr_auto_merge nothing to retry. Record the
                # failure and fall through; the merge attempt (and its
                # AUTO_MERGE log line) now run regardless of notify outcome,
                # and the archive happens once at the end of the block.
                log(
                    f'marker notify failed for {outbox_file.name}: '
                    f'{type(e).__name__}: {e}; continuing to dispatch + '
                    f'auto-merge (notify is informational)',
                    'WARN',
                )
                notify_failed = True

        # Phase D3 commit 4b: PROCEED → write a build-phase task to
        # Forge's inbox. The notify-to-Beacon above is informational
        # (Beacon journals "Forge is proceeding"); the build-phase dispatch
        # below is what actually triggers code work. Two-invocation
        # preflight→build with --resume per signed-off design.
        # task-19: fires regardless of larry_direct so Larry-direct Forge
        # preflights still auto-advance to build.
        if marker_decision['marker_type'] == 'proceed' and agent == 'forge':
            _dispatch_build_phase(data)

        # D3.5 5b: REVIEW_REVISION with budget remaining + high confidence
        # → dispatch a revision task to Forge's inbox. Beacon's notify
        # above is informational (Shape 7, now mid-chain in 5b); the
        # revision dispatch is what actually triggers Forge's fix. Skipped
        # if auto_promoted (low confidence → escalate) or budget_exhausted
        # (over max_revisions → escalate); both downgrade to Beacon-only
        # routing via the intent override above.
        # task-19: fires regardless of larry_direct so Larry-direct Mirror
        # reviews still drive Forge through the revision loop.
        if (
            marker_decision['marker_type'] == 'review_revision'
            and agent == 'mirror'
            and not marker_decision.get('auto_promoted')
            and not marker_decision.get('budget_exhausted')
        ):
            _dispatch_revision_to_forge(data, marker_decision)

        # forge-cold-start-revision (S2/S3): a terminal Mirror verdict closes
        # any open no-session obligation for this task. Terminal = PASS /
        # ESCALATE / EMERGENCY_HALT, plus a REVISION that downgraded to escalate
        # (auto-promoted / budget-exhausted — the loop ended, Larry was pinged).
        # An ACTIVE revision re-dispatch (the branch above) keeps it open. Match
        # the EXPLICIT terminal set (not a `review_` prefix) so a future
        # non-terminal `review_*` marker can't resolve an obligation mid-loop.
        # resolve_obligation no-ops when no obligation exists → safe for every PR.
        if agent == 'mirror':
            _mtype = marker_decision['marker_type']
            _downgraded_revision = (
                _mtype == 'review_revision'
                and (marker_decision.get('auto_promoted')
                     or marker_decision.get('budget_exhausted'))
            )
            if _mtype in (
                'review_pass', 'review_escalate', 'review_emergency_halt',
            ) or _downgraded_revision:
                no_session_ledger.resolve_obligation(
                    data.get('task_id') or 'unknown',
                    resolution=str(_mtype).replace('review_', ''),
                )
                # mirror-review-visibility (Contract C/D, decision d): the
                # trigger for a no-session action-needed record is the OPEN
                # obligation / unrecovered PR. A terminal verdict clears that
                # trigger, so retract any stale for-Larry record — the PR
                # either passed (self-heal landed) or moved to a decision
                # artifact (escalate). Idempotent no-op when none exists.
                for_larry_escalations.clear(
                    _no_session_record_id(data.get('task_id'))
                )

        # mirror-review-visibility (Contracts B+C+D): classify the session-less
        # review outcome on wire signals and route the human-needed buckets to
        # Larry's surfaces (decision → binary approval_request; action → durable
        # self-clearing for-Larry record; self-healing → silent). One artifact
        # per escalation, idempotent on PR + head SHA. Runs after the dispatch +
        # obligation bookkeeping above so the self-healing case is observable.
        if agent == 'mirror':
            _route_no_session_review(data, marker_decision)

        # false-success-notify-fix (2026-06-11): the Mirror REVIEW_PASS
        # auto-merge now runs EARLIER — before the back-leg notify — via
        # `_run_review_pass_auto_merge`, so the notify reports the gh-truth
        # merge state. By here, marker_decision already carries
        # merge_result + merge_outcome; the closing DM below reads them.

        # D3.5 5a-followup: chain-completion DM to the originating Telegram
        # thread. Fires only for terminal-from-Larry's-perspective intents
        # (review-pass/revision/escalate/emergency, plus Forge preflight
        # reject/clarification-exhausted) and only when reply_chat_id is
        # propagated through the chain. Non-fatal on failure.
        # D3.5 5d: review-pass DM body now reflects the merge_outcome
        # attached above; the render pipeline picks the correct variant.
        # false-success-notify-fix (2026-06-11): suppress the closing DM for a
        # review-pass auto-merge SKIP (degenerate PR), preserving the pre-fix
        # behavior where skip cases sent no DM (the notify above still fired).
        if review_pass_skip is None:
            _maybe_dm_larry(data, marker_decision)

        _archive_outbox(outbox_file)
        # false-success-notify-fix (2026-06-11): a review-pass merge SKIP
        # returns 'auto-merge-skipped' (its truthful notify already went out
        # above) — surfaced before the notify-failed telemetry status so the
        # skip outcome stays the canonical return for these degenerate PRs.
        if review_pass_skip is not None:
            return review_pass_skip
        # #12: preserve the 'notify-failed' status for telemetry, but only
        # AFTER the substantive actions (auto-merge, dispatch, DM) and the
        # archive above have run.
        if notify_failed:
            return 'notify-failed'
        return 'larry-direct-marker' if larry_direct else 'notified-marker'

    # task-25 (2026-05-20) — headless Beacon clarification-response handler.
    # Fires BEFORE default notify routing for `agent=beacon AND
    # source=*-question` outboxes (Beacon answering Forge's CLARIFY_REQUEST
    # in headless mode). Writes a resume envelope to Forge's inbox keyed on
    # the ORIGINAL task_id so Forge --resumes her preflight session in her
    # original worktree on her original branch. Closes chain-routing gap #5
    # (the `notify-notify-{task}` doubled-prefix branch + depth-multiplied
    # awareness notifies surfaced live 2026-05-20 on task-22's dispatch).
    # Returning a non-None path means "handled — don't fall through". None
    # falls through to default routing for graceful degradation when
    # forge_session_id failed to propagate (legacy chains).
    clar_resume = _handle_beacon_clarification_response(data)
    if clar_resume is not None:
        _archive_outbox(outbox_file)
        return 'clarification-resume-dispatched'

    # ---- Default (non-marker) routing path — unchanged from D3-notifier ----

    if not _should_notify_back(source, agent):
        _archive_outbox(outbox_file)
        return 'archived-no-notify'

    if _primary_agent_id(source) == agent:
        _archive_outbox(outbox_file)
        return 'skip-self'

    current_depth = _current_notify_depth(data)
    next_depth = current_depth + 1
    # `-question` source = clarification cascade leg (the agent we're processing
    # is answering Forge's CLARIFY_REQUEST). The `max_clarifications` budget on
    # the envelope guards termination; the depth cap would block a legitimate
    # multi-hop clarification flow, so bypass it for this leg only.
    is_clarification_answer = source.endswith('-question')
    if next_depth > MAX_NOTIFY_DEPTH and not is_clarification_answer:
        log(
            f'NOTIFY_CASCADE_DEPTH_EXCEEDED: {outbox_file.name} '
            f'(depth={current_depth}); skipping notify, archiving',
            'WARN',
        )
        _archive_outbox(outbox_file)
        return 'depth-cap'

    target_agent = _primary_agent_id(source)
    if target_agent is None:
        _archive_outbox(outbox_file)
        return 'archived-no-notify'

    notify_source = _notify_back_source(agent, source)
    # Pick intent + kwargs for the action block.
    if source.endswith('-question'):
        intent = 'clarification-response'
        remaining = fph.clarifications_remaining(data)
        intent_kwargs = {'remaining': remaining}
    else:
        intent = 'result-notification'
        intent_kwargs = {}

    task_id = data.get('task_id', outbox_file.stem)
    prompt = build_notify_prompt(
        intent=intent,
        sender=agent,
        task_id=task_id,
        success=data.get('exit_code', 0) == 0,
        output=data.get('result', '') or '',
        error=data.get('error') or '',
        intent_kwargs=intent_kwargs,
    )
    # Notify tasks omit `timeout` — they're not fresh dispatches with a
    # work budget, they're the receiver picking up an update. Our
    # dispatch_validator's `timeout` check is range-bounded
    # [MIN_TIMEOUT, MAX_TIMEOUT] when present, no-op when absent. Upstream's
    # `timeout: 0` ("no timeout") would fail our validator's 60s floor.
    notify_base = {
        'task_id': f'notify-{task_id}',
        'prompt': prompt,
        'source': notify_source,
        'intent': intent,
        '_notify_depth': next_depth,
    }
    # Propagate session_id so clarification-response delivery can resume
    # the original Forge session (commit 4b wires the watcher to honor it).
    if data.get('claude_session_id'):
        notify_base['session_id'] = data['claude_session_id']
    # Carry clarification budget across the cascade so it reaches Forge with
    # the correct count on the resume leg.
    if data.get('clarification_count') is not None:
        notify_base['clarification_count'] = data['clarification_count']
    if data.get('max_clarifications') is not None:
        notify_base['max_clarifications'] = data['max_clarifications']
    # Phase D3 commit 4b post-test-2 fix: propagate branch + pr_title/pr_body
    # so the clarification-answer leg back to Forge passes the worktree gate.
    # target_repo is whitelisted context and carries through the builder below.
    # See the matching block in the marker-decision path above for the full
    # explanation.
    for f_name in ('branch', 'pr_title', 'pr_body'):
        if data.get(f_name):
            notify_base[f_name] = data[f_name]
    notify_task = build_chain_envelope(
        notify_base, data,
        carry={
            'target_repo': CARRY,
            'reply_chat_id': CARRY,
            'forge_build_session_id': DROP,
            'replan_count': DROP,
            'max_replans': DROP,
            'revision_count': DROP,
            'pr_url': DROP,
        },
    )

    notify_filename = f'notify-{outbox_file.stem}.json'
    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent=target_agent,
            task_dict=notify_task,
            source_agent=notify_source,
            filename=notify_filename,
        )
        log(
            f'notified {target_agent} <- {agent} ({notify_source}, '
            f'depth={next_depth}, file={dest.name})'
        )
    except (safe_write_inbox.DispatchRejected, safe_write_inbox.RoutingDenied) as e:
        log(
            f'notify failed for {outbox_file.name}: {type(e).__name__}: {e}',
            'WARN',
        )
        _archive_outbox(outbox_file)
        return 'notify-failed'

    _archive_outbox(outbox_file)
    return 'notified'


def _marker_output_for_prompt(
    data: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    """Pick the output text to put in the notify prompt body for a marker case.

    For clarification-request: just the question text (Beacon doesn't need
    Forge's full preflight ramble, only the specific ask).

    For ack-proceed/reject: the marker payload's summary/reason, so Beacon
    sees the context behind the decision.

    D3.5 commit 5a — Mirror marker types. PASS body = summary; REVISION body
    = finding summary; ESCALATE body = reason; EMERGENCY_HALT body = reason +
    evidence. Without these branches the trailing fallback returns '(no
    reason)' for PASS and (non-auto-promoted) REVISION since their
    intent_kwargs carry no `reason` key — the C-1 review-pass-body-blank bug.
    """
    payload = decision['payload']
    marker_type = decision['marker_type']
    if marker_type == 'clarify_request' and decision['intent'] == 'clarify':
        return payload.get('question', '(no question text)')
    if marker_type == 'proceed':
        return payload.get('preflight_summary', '(no summary)')
    if marker_type == 'reject':
        return payload.get('reason', '(no reason)')
    if marker_type == 'review_pass':
        return payload.get('summary', '(no summary)')
    if marker_type == 'review_revision':
        findings = payload.get('findings') or []
        if isinstance(findings, list) and findings:
            first = findings[0]
            first_desc = (
                first.get('description', '(no description)')
                if isinstance(first, dict)
                else str(first)
            )
            return (
                f'{len(findings)} finding(s). First: {first_desc}'
            )
        return '(revision marker with no findings — should have been PASS)'
    if marker_type == 'review_escalate':
        return payload.get('reason', '(no reason)')
    if marker_type == 'review_emergency_halt':
        return (
            f'Reason: {payload.get("reason", "(no reason)")}\n'
            f'Evidence: {payload.get("evidence", "(no evidence)")}'
        )
    # Budget-exhausted clarify converted to clarification-exhausted
    return decision['intent_kwargs'].get('reason', '(no reason)')


def _load_dead_letter_state() -> dict[str, Any]:
    if not DEAD_LETTER_STATE.exists():
        return {'processed': []}
    try:
        return json.loads(DEAD_LETTER_STATE.read_text())
    except (json.JSONDecodeError, OSError):
        return {'processed': []}


def _save_dead_letter_state(state: dict[str, Any]) -> None:
    DEAD_LETTER_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEAD_LETTER_STATE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    tmp.rename(DEAD_LETTER_STATE)


def scan_dead_letters() -> int:
    """Notify dispatchers when their tasks land in `.invalid/`. Returns count notified."""
    state = _load_dead_letter_state()
    processed = set(state.get('processed', []))
    notified = 0
    seen_now = set()

    for agent in AGENT_IDS:
        invalid_dir = INBOXES_ROOT / agent / '.invalid'
        if not invalid_dir.exists():
            continue
        for invalid_file in invalid_dir.glob('*.json'):
            key = f'{agent}:{invalid_file.name}'
            seen_now.add(key)
            if key in processed:
                continue

            try:
                task_data = json.loads(invalid_file.read_text())
            except (OSError, json.JSONDecodeError):
                processed.add(key)
                continue

            if _is_fixture_emission(invalid_file.stem):
                log(
                    f'skipping dead-letter notify for fixture envelope '
                    f'{invalid_file.name} (reserved fixture namespace)'
                )
                processed.add(key)
                continue

            original_source = task_data.get('source', '')
            target_agent = _primary_agent_id(original_source)
            if target_agent is None or target_agent == agent:
                # No dispatcher to notify (system source) or self-dispatch.
                processed.add(key)
                continue

            reason_file = invalid_file.with_suffix('.reason')
            reason_text = ''
            if reason_file.exists():
                try:
                    reason_text = reason_file.read_text().strip()
                except OSError:
                    pass

            original_prompt_excerpt = (task_data.get('prompt', '') or '')[:500]
            dl_reason = (
                f'your dispatch to {agent} (filename {invalid_file.name}) was '
                f'rejected by dispatch_validator and never reached the agent. '
                f'Validator reason: {reason_text or "(no reason recorded)"}. '
                f'No retry will happen automatically — inspect the task, fix '
                f'the issue (prompt length, schema, source enum, etc.), and '
                f're-dispatch if appropriate.'
            )
            dl_prompt = build_notify_prompt(
                intent='dead-letter',
                sender=agent,
                task_id=task_data.get('task_id', invalid_file.stem),
                success=False,
                output=f'Original prompt (first 500 chars):\n{original_prompt_excerpt}',
                intent_kwargs={'reason': dl_reason},
            )
            notify_base: dict[str, Any] = {
                'task_id': f'dead-letter-{invalid_file.stem}',
                'prompt': dl_prompt,
                'source': f'{agent}-result',
                'intent': 'dead-letter',
                '_notify_depth': 1,  # this IS a depth-1 message; further loops cap
            }
            notify_task = build_chain_envelope(
                notify_base, task_data,
                carry={
                    'reply_chat_id': CARRY,
                    'forge_build_session_id': DROP,
                    'replan_count': DROP,
                    'max_replans': DROP,
                    'revision_count': DROP,
                    'target_repo': DROP,
                    'pr_url': DROP,
                },
            )

            notify_filename = f'notify-dead-letter-{invalid_file.stem}.json'
            try:
                safe_write_inbox.safe_write_inbox(
                    target_agent=target_agent,
                    task_dict=notify_task,
                    source_agent=f'{agent}-result',
                    filename=notify_filename,
                )
                log(
                    f'dead-letter notified {target_agent} <- {agent} for '
                    f'{invalid_file.name} (reason: {reason_text[:80]})'
                )
                notified += 1
            except (safe_write_inbox.DispatchRejected, safe_write_inbox.RoutingDenied) as e:
                log(
                    f'dead-letter notify failed for {invalid_file.name}: '
                    f'{type(e).__name__}: {e}',
                    'WARN',
                )
            # Mark as processed regardless of notify outcome — we never want
            # to retry-notify and amplify a real bug.
            processed.add(key)

    # GC: drop processed keys whose underlying .invalid file no longer exists.
    processed = {k for k in processed if k in seen_now}
    # Cap state file size with FIFO truncation.
    processed_list = sorted(processed)[-DEAD_LETTER_STATE_CAP:]
    state['processed'] = processed_list
    state['last_run'] = datetime.now(timezone.utc).isoformat()
    state['last_notified_count'] = notified
    _save_dead_letter_state(state)
    return notified


def _handle_sigterm(signum, frame):
    global _running
    _running = False
    log(f'received signal {signum}, exiting cleanly')


def main_loop() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    ensure_dirs()
    log('outbox-notifier starting')
    while _running:
        if _emergency_halt_active():
            log('EMERGENCY_HALT active — exiting cleanly')
            return 0

        # Outbox results scan
        for agent in AGENT_IDS:
            outbox_dir = OUTBOXES_ROOT / agent
            if not outbox_dir.exists():
                continue
            for outbox_file in sorted(outbox_dir.glob('*.json')):
                if outbox_file.name.startswith('.'):
                    continue
                # D3.5 5d — honor mid-iteration EMERGENCY_HALT trips.
                # _trip_emergency_halt (called from process_outbox below
                # when Mirror emits REVIEW_EMERGENCY_HALT) writes the
                # halt file synchronously. Without this re-check, the
                # outer loop's halt gate (line ~3638) only fires on the
                # NEXT 5s poll — meaning subsequent files in this poll
                # can still trigger dispatches AFTER the halt fired.
                # Halt is intended to stop the world; checking per-file
                # closes the window.
                if _emergency_halt_active():
                    log(
                        'EMERGENCY_HALT detected mid-iteration — '
                        'aborting remaining outbox scan; will exit '
                        'cleanly on next outer poll',
                        'WARN',
                    )
                    return 0
                try:
                    process_outbox(outbox_file)
                except Exception as e:
                    log(
                        f'unexpected error processing {outbox_file.name}: '
                        f'{type(e).__name__}: {e}',
                        'ERROR',
                    )

        # D3.5 5d second-pass review-fix 2-#2: also gate the dead-letter
        # scan on halt. Without this, if EMERGENCY_HALT trips on the very
        # last file of the very last agent (so the inner-loop check from
        # 2-#3 doesn't fire) the dead-letter scan still calls
        # safe_write_inbox to notify originating agents — i.e. dispatches
        # that the halt is meant to pause. The next outer poll's halt gate
        # at the top of the while-loop catches this on the second pass,
        # but the within-poll dead-letter scan is a halt-leak.
        if _emergency_halt_active():
            log(
                'EMERGENCY_HALT detected before dead-letter scan — '
                'aborting remaining poll work; exiting cleanly',
                'WARN',
            )
            return 0

        # Dead-letter scan
        try:
            scan_dead_letters()
        except Exception as e:
            log(f'dead-letter scan error: {type(e).__name__}: {e}', 'ERROR')

        # D3.5 5d-prime — AUTO_MERGE queue sweep. Cheap when queue empty
        # (one Path.exists() call). When entries are present, makes O(N)
        # `gh pr view` calls per poll to detect blocker resolution +
        # retry UNKNOWN-deferred entries + watchdog-DM stale entries.
        # Daemon-never-wedge: any exception in the sweep is logged and
        # the loop continues.
        try:
            _auto_merge_queue_sweep()
        except Exception as e:  # noqa: BLE001
            log(
                f'auto-merge queue sweep error: {type(e).__name__}: {e}',
                'ERROR',
            )

        # fix-notifier-review-dispatch-reliability (Part B) — reconciliation
        # sweep for dropped Forge->Mirror review-requests. Throttled to at most
        # once per RECONCILE_INTERVAL_SECONDS (not every poll). Gated on the
        # same halt check as the sweeps above and wrapped in try/except so any
        # error logs and the loop continues — never wedge the daemon.
        global _last_reconcile_ts
        now = time.time()
        if now - _last_reconcile_ts >= RECONCILE_INTERVAL_SECONDS:
            _last_reconcile_ts = now
            try:
                _reconcile_missed_mirror_reviews()
            except Exception as e:  # noqa: BLE001
                log(
                    f'reconcile sweep error: {type(e).__name__}: {e}',
                    'ERROR',
                )

        # Sleep in short slices so SIGTERM is responsive.
        slept = 0.0
        while _running and slept < POLL_INTERVAL_SECONDS:
            time.sleep(0.5)
            slept += 0.5

    log('outbox-notifier exiting')
    return 0


if __name__ == '__main__':
    sys.exit(main_loop())
