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

import json
import os
import re
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
import chain_event_emit             # noqa: E402  # E4.4e PR-A: push writer
import dispatch_validator         # noqa: E402
import fixture_patterns             # noqa: E402  # outbox-side fixture gate
import forge_preflight_handler as fph  # noqa: E402
import larry_alerts                # noqa: E402
import mirror_review_handler as mrh  # noqa: E402
import safe_write_inbox             # noqa: E402
import sequence_shortcut_helpers as ssh  # noqa: E402  # V6: step-merged signal
import trust_policy                 # noqa: E402

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
_PR_URL_RE = re.compile(
    # HIGH-2 (PR #10 review): use `[ \t]` not `\s` so newlines between `PR`
    # and the verb DON'T match. `\s+` would let Forge accidentally split
    # `PR\nopened: <url>` across lines and still satisfy the regex even
    # though it violates the CLAUDE.md "FIRST LINE unconditional" rule.
    r'^[ \t]*PR[ \t]+(?:opened|updated):[ \t]*(https://github\.com/[^\s]+/pull/\d+)',
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

# Structural pr_url validator (2026-05-29 — structural-pr-url-validator).
# Replaces the prior name-based repo-coords allowlist + canonical-form
# rewrite table. The AUTO_MERGE gate now validates two intrinsic
# properties of the pr_url: (1) shape — does it match the canonical
# `https://github.com/Larry-Yatch/<allowed-repo>/pull/<N>` form with N>=1,
# and (2) existence — does the PR actually exist and have state=OPEN
# (Layer 2, `gh pr view`). Anchored start-and-end so trailing junk
# (anchors, query strings, doctored fragments) is rejected — at this
# layer we want the exact form `gh pr merge` needs, nothing else.
# Hardcodes the two operating-environment repos rather than reading
# config: Larry-Yatch + the two repos are ground truth, not configurable
# at runtime (env vars / extra config files would just be another moving
# part that can drift).
_PR_URL_STRUCTURAL_RE = re.compile(
    r'^https://github\.com/Larry-Yatch/'
    r'(ourliberty-agent-core|ourliberty-dashboard)/pull/([1-9]\d*)$'
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

# Fallback when config/agent-models.json doesn't specify
# auto_merge_queue.watchdog_dm_hours. 24h is Larry's default; raise via
# config if he's away for a known-long stretch.
DEFAULT_AUTO_MERGE_WATCHDOG_HOURS = 24

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
    current=$X.XX cap=$5.00 dispatch=<label>`` — load-bearing for watchdog
    scanning. Same pattern as the BEACON_REPLAN_ALERT_WRITE_FAILED sentinel
    from 5c.
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
        + ('' if already_dmed else ' + queueing closing DM'),
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
    'review-pass': (
        'Mirror has APPROVED PR `{pr_url}` on task `{task_id}`. Summary: '
        '{summary}. Auto-merge has fired automatically (D3.5 5d) — Larry '
        'sees the actual merge outcome in his closing DM. Journal the '
        'approval; no further action from you.'
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
}

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
            'blocker_pr_number', 'overlap_files',
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
        if merge_outcome in ('deferred_unknown', 'held_conflict'):
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


def log(msg: str, level: str = 'INFO') -> None:
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
    recovered = _recover_forge_marker_text_from_session_log(
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
            raise fph.MalformedForgeMarker(
                'phase=preflight requires ONE marker block at end of response '
                '(PROCEED / CLARIFY_REQUEST / REJECT) — none found. Re-read '
                "agents/forge/CLAUDE.md 'Preflight discipline' — preflight "
                'decides, it does not act.'
            )
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
    # D3.5 5b-followup Bug B: keep envelope task_id as the ORIGINAL task_id
    # across retries. The previous wrapped form (`marker-error-<orig>-<N>`)
    # broke the 4b task_id-mismatch check: Forge correctly emits her marker
    # with the original task_id (that's the actual semantic task), but the
    # envelope had the wrapper name, so the mismatch check rejected every
    # retry. Cascade never recovered from real preflight failures.
    # Retry tracking lives in `marker_error_count`; filename uses
    # `-{new_count}` suffix for uniqueness. Now Forge's marker contract
    # (task_id matches envelope) holds consistently across retries.
    notify_task: dict[str, Any] = {
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
        notify_task['clarification_count'] = data['clarification_count']
    if data.get('max_clarifications') is not None:
        notify_task['max_clarifications'] = data['max_clarifications']
    if data.get('claude_session_id'):
        notify_task['session_id'] = data['claude_session_id']
    # Phase D3 commit 4b: propagate target_repo + branch so the retry task
    # passes the watcher's worktree gate. Without these, an agent with
    # `worktree_enabled: true` (Forge) rejects the marker-error retry as
    # `target_repo: no canonical path` and the malformed-marker recovery
    # silently dies — same shape as the 4a marker-error black-hole bug.
    if data.get('target_repo'):
        notify_task['target_repo'] = data['target_repo']
    if data.get('branch'):
        notify_task['branch'] = data['branch']
    # D3.5 5b M-2 review fix: propagate revision-phase envelope fields so
    # a marker-error retry triggered by the revision-preamble strict gate
    # doesn't lose Forge's revision context. Without these the retry task
    # arrives with no `phase` (watcher resume gate refuses --resume),
    # no `forge_build_session_id` (next revision-dispatch can't thread),
    # no `revision_count` / `max_revisions` (budget eval starts fresh),
    # no `pr_url` (Forge has nothing to point Mirror at). Without these
    # the revision-phase marker-error path is a dead end.
    if data.get('phase'):
        notify_task['phase'] = data['phase']
    if data.get('forge_build_session_id'):
        notify_task['forge_build_session_id'] = data['forge_build_session_id']
    if data.get('revision_count') is not None:
        notify_task['revision_count'] = data['revision_count']
    if data.get('max_revisions') is not None:
        notify_task['max_revisions'] = data['max_revisions']
    if data.get('pr_url'):
        notify_task['pr_url'] = data['pr_url']
    # D3.5 5b-followup Bug E (live re-test): propagate reply_chat_id so a
    # marker-error retry doesn't drop the originating Telegram chat thread.
    # _notify_mirror_marker_error has this; _notify_forge_marker_error was
    # missing it — discovered when the 2026-05-13 re-test completed PR #5
    # successfully but Larry got no closing DM because reply_chat_id went
    # to None on the first marker-error retry and never recovered through
    # the chain.
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']

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
    notify_task: dict[str, Any] = {
        'task_id': f'dead-letter-marker-{task_id}',
        'prompt': prompt,
        'source': 'outbox-notifier',
        'intent': 'dead-letter',
        '_notify_depth': 1,
    }
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']

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

    build_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': build_prompt,
        'source': 'beacon',
        'phase': 'build',
        'session_id': preflight_session,
        'dispatched_by': 'outbox-notifier',
    }
    if target_repo:
        build_task['target_repo'] = target_repo
    if branch:
        build_task['branch'] = branch
    if pr_title:
        build_task['pr_title'] = pr_title
    if pr_body:
        build_task['pr_body'] = pr_body
    if max_clar is not None:
        build_task['max_clarifications'] = max_clar
    if data.get('reply_chat_id') is not None:
        build_task['reply_chat_id'] = data['reply_chat_id']
    # D3.5 5c C-1 review fix: propagate replan_count + max_replans through
    # the preflight→build hop. Without this, an approval emitted by the 5c
    # replan path (which set replan_count > 0 on the original Forge task
    # envelope) would land in Forge's preflight outbox correctly but get
    # reset to 0 on the build dispatch — breaking the budget enforcement
    # on the next Mirror REVIEW_ESCALATE leg. Symmetric with how
    # revision_count rides through _dispatch_revision_to_forge.
    if data.get('replan_count') is not None:
        build_task['replan_count'] = data['replan_count']
    if data.get('max_replans') is not None:
        build_task['max_replans'] = data['max_replans']

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
    # Idempotency check (4b review fix): if the build task is already
    # present in Forge's inbox OR was already archived, skip re-dispatch.
    # Guards against the notifier crashing between dispatch and archive
    # of the preflight outbox: on restart, re-processing the same outbox
    # would otherwise write a second build task that would resume an
    # already-terminated session against potentially-dirty worktree state.
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
    if (
        (forge_inbox / build_filename).exists()
        or (forge_inbox / '.archive' / build_filename).exists()
        # D3.5 5a M-1 review fix: also check .invalid/ — a prior dispatch that
        # was validator-rejected lives there, and we shouldn't re-dispatch a
        # duplicate that will hit the same rejection.
        or (forge_inbox / '.invalid' / build_filename).exists()
    ):
        log(
            f'build-phase already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
        )
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
) -> Optional[str]:
    """Walk a Claude session log and return the latest assistant-turn text
    that parses as a valid marker under ``parser``.

    Returns the combined text of the LATEST assistant turn whose parse returns
    a non-None marker_type (so a revised verdict still wins over an earlier
    one), or None when the session log is missing, unreadable, or carries no
    parseable marker. Intermediate turns whose text raises one of
    ``skip_exceptions`` are skipped — that's mid-session noise (e.g. an agent
    reasoning about the marker grammar in prose), not her final verdict.

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
        return None
    try:
        candidates = list(CLAUDE_PROJECTS_ROOT.glob(f'*/{session_id}.jsonl'))
    except OSError:
        return None
    if not candidates:
        return None
    log_path = candidates[0]
    last_marker_text: Optional[str] = None
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
    except OSError:
        return None
    return last_marker_text


def _recover_marker_text_from_session_log(
    session_id: Optional[str],
) -> Optional[str]:
    """Mirror-parser binding of `_scan_session_log_for_latest_marker_text`.

    Retained for back-compat with prior multi-turn-recovery call sites.
    """
    return _scan_session_log_for_latest_marker_text(
        session_id,
        mrh.parse_mirror_marker,
        (mrh.MalformedMirrorMarker, mrh.MultipleMirrorMarkers),
    )


def _recover_forge_marker_text_from_session_log(
    session_id: Optional[str],
) -> Optional[str]:
    """Forge-parser binding of `_scan_session_log_for_latest_marker_text`."""
    return _scan_session_log_for_latest_marker_text(
        session_id,
        fph.parse_forge_marker,
        (fph.MalformedForgeMarker, fph.MultipleForgeMarkers),
    )


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
    recovered = _recover_marker_text_from_session_log(
        data.get('claude_session_id'),
    )
    if recovered:
        marker_type, payload, _narrative = mrh.parse_mirror_marker(recovered)
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
      * REVISION — DM Larry with the verdict + the human-readable
        review body (which carries Mirror's reasons) + the sequence
        file path. Larry reads the reasons, amends the sequence file,
        and re-dispatches the review.
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
        msg = (
            f'Mirror DAG-preflight REVISION for sequence `{seq_id}`. '
            f'Amend the sequence file at `{seq_path}` (or the spec it '
            f'references) per Mirror\'s findings below, then '
            f're-dispatch the review.\n\n--- Mirror\'s verdict ---\n'
            f'{body_snippet}'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'mirror-dag-revision:{seq_id}',
        )
        log(
            f'MIRROR_DAG_PREFLIGHT seq={seq_id} verdict=REVISION '
            f'task={task_id}; DMed Larry',
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
    notify_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': prompt,
        'source': 'outbox-notifier',
        'intent': 'marker-error',
        '_notify_depth': 1,
        'original_source': original_source,
        'marker_error_count': new_count,
    }
    # Propagate envelope fields the agent needs to keep working on the same
    # task (session_id for --resume, target_repo + branch for worktree gating).
    # Same shape as the Forge marker-error path.
    if data.get('claude_session_id'):
        notify_task['session_id'] = data['claude_session_id']
    if data.get('target_repo'):
        notify_task['target_repo'] = data['target_repo']
    if data.get('branch'):
        notify_task['branch'] = data['branch']
    if data.get('pr_url'):
        notify_task['pr_url'] = data['pr_url']
    # Revision counters propagate too — a marker-error round shouldn't
    # reset the revision budget mid-review.
    if data.get('revision_count') is not None:
        notify_task['revision_count'] = data['revision_count']
    if data.get('max_revisions') is not None:
        notify_task['max_revisions'] = data['max_revisions']
    # D3.5 5a M-3 review fix: propagate reply_chat_id so a Telegram-initiated
    # review whose marker errors three-strikes still closes the chat thread
    # via the eventual dead-letter to Beacon. Without this the user-facing
    # DM thread silently ends.
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']
    # D3.5 5b M-7 (second-pass review fix): propagate forge_build_session_id
    # and phase. Without these, a Mirror marker-error retry that emits a
    # clean REVIEW_REVISION on the second try would have no
    # forge_build_session_id on the envelope — `_build_outbox` propagates
    # only what's on the task — and `_dispatch_revision_to_forge` would
    # silently skip (no session to --resume). Same shape as the C-1
    # propagation gap, on Mirror's side. `phase` is also missing — without
    # it, Mirror's retry task arrives with no phase context.
    if data.get('forge_build_session_id'):
        notify_task['forge_build_session_id'] = data['forge_build_session_id']
    if data.get('phase'):
        notify_task['phase'] = data['phase']

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
    Returns the first matching line's URL, or None on empty/None input or
    no match.
    """
    if not isinstance(result_text, str) or not result_text:
        return None
    m = _PR_URL_RE.search(result_text)
    if not m:
        return None
    return m.group(1)


def _dispatch_mirror_review(data: dict[str, Any], pr_url: str) -> None:
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
    """
    task_id = data.get('task_id') or 'unknown'
    target_repo = data.get('target_repo')
    if not target_repo:
        # Without target_repo, Mirror's worktree gate (now active per 5a's
        # agent-models.json change) rejects the review task as "no canonical
        # path." Surface the gap to Larry rather than silently dropping.
        log(
            f'PR opened on task {task_id} but no target_repo on envelope; '
            f'cannot dispatch review (Mirror requires target_repo for '
            f'worktree gating). Larry must manually re-dispatch.',
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

    review_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': review_prompt,
        'source': 'beacon',
        'phase': 'review',
        'pr_url': pr_url,
        'target_repo': target_repo,
        'revision_count': 0,
        'max_revisions': max_revisions,
        'dispatched_by': 'outbox-notifier',
    }
    if branch:
        review_task['branch'] = branch
    if data.get('reply_chat_id') is not None:
        review_task['reply_chat_id'] = data['reply_chat_id']
    # D3.5 5a M-2 review fix: propagate the same envelope fields
    # _dispatch_build_phase does. Without these, a future Mirror REVIEW_QUESTION
    # round-trip (5b) loses the PR metadata when answering back to Beacon,
    # and the worktree gate rejects with "no canonical path" — same shape as
    # the 4a marker-error black hole.
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            review_task[f_name] = data[f_name]
    # D3.5 5b: thread Forge's build session_id through Mirror's envelope as
    # `forge_build_session_id` so a downstream REVIEW_REVISION can resume
    # Forge's session for the revision dispatch. Without this, the revision
    # task can't --resume the right conversation and Forge starts fresh,
    # losing all the build context she'd already loaded.
    if data.get('claude_session_id'):
        review_task['forge_build_session_id'] = data['claude_session_id']
    # D3.5 5c C-1 review fix: propagate replan_count + max_replans through
    # the build→review hop. Without this, Mirror's REVIEW_ESCALATE outbox
    # would carry replan_count=0 on the second-loop iteration, defeating
    # the budget cap. Symmetric with the preflight→build propagation.
    if data.get('replan_count') is not None:
        review_task['replan_count'] = data['replan_count']
    if data.get('max_replans') is not None:
        review_task['max_replans'] = data['max_replans']

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
    # Idempotency check (same pattern as _dispatch_build_phase): if the
    # review task is already in Mirror's inbox OR archived, skip. Guards
    # against the notifier crashing between dispatch and archive of the
    # build-phase outbox — re-processing would otherwise spawn a duplicate
    # Mirror review of a PR she's already started reviewing.
    mirror_inbox = safe_write_inbox.INBOXES_ROOT / 'mirror'
    if (
        (mirror_inbox / review_filename).exists()
        or (mirror_inbox / '.archive' / review_filename).exists()
        # D3.5 5a M-1 review fix: also check .invalid/ — a prior dispatch
        # that was validator-rejected lives there. Don't re-dispatch a
        # duplicate that will hit the same rejection.
        or (mirror_inbox / '.invalid' / review_filename).exists()
    ):
        log(
            f'review-request already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
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


def _alert_no_session_revision_broadcast(
    data: dict[str, Any], decision: dict[str, Any],
    routing_source: Optional[str],
) -> None:
    """Broadcast a Larry alert for a no-session REVIEW_REVISION when the
    chat-targeted DM path can't fire.

    Chain discipline v3 GAP 1. Complements `_dm_larry_no_session_revision`
    (which targets a specific chat_id when source='larry'). This variant
    uses `larry_alerts.append_alert` so the rejection reaches Larry via
    the broadcast bot sweep when routing was via Beacon or there's no
    chat_id on the envelope. Subject keys on task_id so the per-subject
    60-min cooldown doesn't suppress genuinely-different rejections.
    """
    task_id = data.get('task_id') or 'unknown'
    payload = decision.get('payload') or {}
    pr_url = data.get('pr_url') or (
        payload.get('pr_url') if isinstance(payload, dict) else None
    ) or '(no pr_url)'
    branch = data.get('branch') or '(branch unknown)'
    summary = payload.get('summary') or payload.get('reason') or ''
    findings = payload.get('findings')

    body_lines = [
        f'Mirror requested revision on {pr_url} (task `{task_id}`).',
        f'No Forge build session on envelope (routing_source={routing_source!r}); '
        f'auto-resume cannot fire — chain protocol assumes Forge owns the build '
        f'session. Externally-authored PR needs manual reconciliation.',
    ]
    if summary:
        body_lines.append(f'Summary: {summary}')
    if isinstance(findings, list) and findings:
        body_lines.append('Findings:')
        for i, f in enumerate(findings[:5], 1):
            if isinstance(f, dict):
                sev = f.get('severity', '?')
                file_ref = f.get('file', '?')
                line_ref = f.get('line_range', '')
                desc = f.get('description', '(no description)')
                loc = f'{file_ref} {line_ref}'.strip()
                body_lines.append(f'  {i}. [{sev}] {loc} — {desc}')
            else:
                body_lines.append(f'  {i}. {f}')
        if len(findings) > 5:
            body_lines.append(f'  … and {len(findings) - 5} more')
    message = '\n'.join(body_lines)

    suggested_action = (
        f'Forge built this PR outside the dispatch chain — apply Mirror\'s '
        f'revisions manually on branch `{branch}`, or re-dispatch via Beacon '
        f'with a fresh task_id to thread a new forge_build_session_id.'
    )
    try:
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=message,
            subject=f'no-session-revision:{task_id}',
            suggested_action=suggested_action,
        )
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'no-session revision broadcast alert raised for task {task_id}: '
            f'{type(e).__name__}: {e}',
            'WARN',
        )


def _dispatch_revision_to_forge(
    data: dict[str, Any], decision: dict[str, Any],
) -> None:
    """Write a revision-task to Forge's inbox after Mirror's REVIEW_REVISION.

    D3.5 commit 5b. Parallel to `_dispatch_build_phase`: same task_id, same
    branch, --resume against Forge's build session. The marker's findings
    serialize into the prompt so Forge has structured input on what to fix.

    Pulls `forge_build_session_id` from Mirror's outbox envelope (threaded
    through 5a's `_dispatch_mirror_review` → propagated via _build_outbox).
    Without it, the revision task can't --resume the right conversation;
    Forge starts fresh and loses her build context.

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
    if not forge_session:
        # Chain-gap #6 (observed 2026-05-20 on PR #59). When Larry opens a
        # Claude-as-Forge PR (trivial config/docs edits — source='larry',
        # no Forge build session), Mirror's REVIEW_REVISION has no Forge
        # session to --resume against. The auto-resume chain doesn't apply;
        # instead, surface the findings to Larry via Telegram DM so the
        # rejection isn't silent. Without this branch, Larry only learns
        # about the revision rejection if he's watching the chat live.
        #
        # Gated on source='larry' (the Claude-as-Forge marker) AND an
        # int reply_chat_id (a DM target). For source!='beacon' but also
        # not 'larry' (system sources without chats), keep the original
        # WARN — there's no DM target to escalate to.
        routing_source = data.get('original_source') or data.get('source')
        chat_id = data.get('reply_chat_id')
        if routing_source == 'larry' and isinstance(chat_id, int):
            _dm_larry_no_session_revision(data, decision, chat_id)
            return
        # Chain discipline v3 GAP 1 (2026-05-26): the chat-targeted DM path
        # above can't fire when routing_source != 'larry' (e.g. Beacon
        # dispatched the revision) or there's no reply_chat_id on the
        # envelope. The original WARN-only fallthrough was silent today on
        # `feedback_claude_as_forge_boundaries`. Broadcast a per-task alert
        # via larry_alerts so the rejection isn't invisible until manual
        # log inspection. Healer Check 6 covers any escape from this path.
        _alert_no_session_revision_broadcast(data, decision, routing_source)
        log(
            f'REVIEW_REVISION on task {task_id} has no forge_build_session_id '
            f'(routing_source={routing_source!r}, chat_id={chat_id!r}); '
            f'revision dispatch would have no session to --resume — skipping. '
            f'Broadcast alert queued for manual re-dispatch.',
            'WARN',
        )
        return

    target_repo = data.get('target_repo')
    if not target_repo:
        log(
            f'REVIEW_REVISION on task {task_id} has no target_repo on envelope; '
            f'Forge worktree gate would reject revision dispatch — skipping.',
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

    revision_prompt_lines = [
        f'Revision phase. Mirror has reviewed your build on task `{task_id}` '
        f'and requested changes (revision {next_count} of {max_revisions}).',
        '',
        f'Task: `{task_id}`',
    ]
    if branch:
        revision_prompt_lines.append(f'Branch: `{branch}`')
    pr_url = data.get('pr_url') or payload.get('pr_url')
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
    ])
    revision_prompt = '\n'.join(revision_prompt_lines)

    revision_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': revision_prompt,
        'source': 'beacon',          # logical dispatcher (Beacon's spec authorized this)
        'phase': 'revision',
        'session_id': forge_session,  # --resume Forge's build session
        # C-1 review fix (D3.5 5b): also propagate forge_build_session_id
        # on the revision-task envelope itself. _build_outbox propagates
        # this onward to Forge's revision outbox, so round-2's
        # _dispatch_mirror_review_rerun → next REVIEW_REVISION →
        # _dispatch_revision_to_forge can still resolve the build session.
        # Without this, the loop stalls silently at round 2.
        'forge_build_session_id': forge_session,
        'target_repo': target_repo,
        'revision_count': next_count,
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
    if branch:
        revision_task['branch'] = branch
    if pr_url:
        revision_task['pr_url'] = pr_url
    if data.get('reply_chat_id') is not None:
        revision_task['reply_chat_id'] = data['reply_chat_id']
    # Propagate the same envelope fields _dispatch_build_phase does so a
    # future REVIEW_QUESTION (deferred) round-trip would preserve PR
    # metadata. Same shape as 5a M-2 review fix.
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            revision_task[f_name] = data[f_name]
    # D3.5 5c C-X1 (second-pass review fix): propagate replan_count +
    # max_replans through the revision-loop dispatches too. Without this,
    # a task that goes through ANY revision round before re-escalating has
    # replan_count reset to 0 on the resulting REVIEW_ESCALATE notify —
    # silently defeating the max_replans cap. The first C-1 fix covered
    # preflight→build and build→review; this completes coverage of the
    # remaining two dispatch sites (_dispatch_revision_to_forge here +
    # _dispatch_mirror_review_rerun below).
    if data.get('replan_count') is not None:
        revision_task['replan_count'] = data['replan_count']
    if data.get('max_replans') is not None:
        revision_task['max_replans'] = data['max_replans']

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
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
    if (
        (forge_inbox / revision_filename).exists()
        or (forge_inbox / '.archive' / revision_filename).exists()
        or (forge_inbox / '.invalid' / revision_filename).exists()
    ):
        log(
            f'revision-{next_count} already dispatched for task {task_id} '
            f'(file or archive or .invalid present); skipping duplicate write'
        )
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
        log(
            f'revision-{next_count} dispatched forge <- beacon '
            f'(task={task_id}, file={dest.name}, '
            f'resume={forge_session[:12]}...)'
        )
    except (
        safe_write_inbox.DispatchRejected,
        safe_write_inbox.RoutingDenied,
    ) as e:
        log(
            f'revision dispatch FAILED for task {task_id} round {next_count}: '
            f'{type(e).__name__}: {e}. Beacon already notified of REVISION; '
            f'Larry must manually re-dispatch.',
            'WARN',
        )


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
        log(
            f'Forge revision-{round_num} on task {task_id} has no target_repo; '
            f'cannot dispatch re-review — skipping.',
            'WARN',
        )
        return

    branch = data.get('branch')
    pr_url = data.get('pr_url')
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
            f'on envelope; cannot dispatch re-review — skipping. Larry '
            f'should manually re-dispatch.',
            'WARN',
        )
        return

    review_task: dict[str, Any] = {
        'task_id': task_id,
        'prompt': review_prompt,
        'source': 'beacon',
        'phase': 'review',
        'pr_url': pr_url,
        'target_repo': target_repo,
        'revision_count': round_num,
        'max_revisions': max_revisions,
        'dispatched_by': 'outbox-notifier',
    }
    if branch:
        review_task['branch'] = branch
    if data.get('reply_chat_id') is not None:
        review_task['reply_chat_id'] = data['reply_chat_id']
    # forge_build_session_id propagates forward unchanged so the NEXT
    # revision (if Mirror flags more findings) can resume Forge's session.
    if data.get('forge_build_session_id'):
        review_task['forge_build_session_id'] = data['forge_build_session_id']
    # M-8 second-pass fix: also propagate previous_findings forward so the
    # NEXT round's REVIEW_REVISION (if any) carries findings through.
    if isinstance(data.get('previous_findings'), list):
        review_task['previous_findings'] = data['previous_findings']
    for f_name in ('pr_title', 'pr_body', 'max_clarifications'):
        if data.get(f_name) is not None:
            review_task[f_name] = data[f_name]
    # D3.5 5c C-X1 (second-pass review fix): also propagate replan_count +
    # max_replans through the re-review dispatch. Closes the second seam in
    # the revision-loop replan-budget propagation chain (the partner fix is
    # in _dispatch_revision_to_forge above).
    if data.get('replan_count') is not None:
        review_task['replan_count'] = data['replan_count']
    if data.get('max_replans') is not None:
        review_task['max_replans'] = data['max_replans']

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
    """Layer 1 of the AUTO_MERGE pr_url validator — pure regex shape check.

    Returns `(repo_coords, pr_number, reason)`. On valid shape:
    `('Larry-Yatch/<repo>', <int>, 'ok')`. On invalid shape:
    `(None, None, '<short diagnostic>')`.

    No shell-out; no network. The whole point of Layer 1 is to fail fast
    on garbage URLs (`pull/0`, wrong-owner spoofs, fixture-generated
    pointers to nonexistent repos) before any shell-out to `gh pr view`
    or `gh pr merge`. Anchored start-and-end so trailing junk after the
    PR number is rejected — at the AUTO_MERGE layer we want the exact
    canonical form, nothing fuzzy.
    """
    if not isinstance(pr_url, str) or not pr_url:
        return None, None, 'empty-or-non-string'
    m = _PR_URL_STRUCTURAL_RE.match(pr_url)
    if not m:
        return None, None, 'shape-mismatch'
    repo = m.group(1)
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
    """
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        log(
            f'AUTO_MERGE task={task_id} pr={pr_url!r} outcome=failed '
            f'reason=malformed-pr-url (no shell-out attempted)',
            'WARN',
        )
        return {
            'merge_outcome': 'failed',
            'merge_reason': f'malformed PR URL: {pr_url!r}',
            'pr_number': '?',
            'repo_coords': '?',
        }
    repo_coords, pr_number = parsed
    try:
        proc = subprocess.run(
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
            f'reason=timeout after {_AUTO_MERGE_TIMEOUT_S}s ({e})',
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
            f'reason=gh-cli-missing ({e})',
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
            f'reason=os-error ({type(e).__name__}: {e})',
            'WARN',
        )
        return {
            'merge_outcome': 'failed',
            'merge_reason': f'{type(e).__name__}: {e}',
            'pr_number': pr_number,
            'repo_coords': repo_coords,
        }

    if proc.returncode == 0:
        log(
            f'AUTO_MERGE task={task_id} pr={pr_url} outcome=merged '
            f'(--squash --delete-branch)',
        )
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
        log(
            f'AUTO_MERGE task={task_id} pr={pr_url} outcome=already_merged '
            f'(gh exit={proc.returncode} but state=MERGED — resume from '
            f'prior crash; treating as success)',
        )
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
        f'stderr={stderr_text[:300]!r})',
        'WARN',
    )
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
            return seq_id
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge
        log(
            f'sequence-step-merged scan raised {type(e).__name__}: {e}; '
            f'swallowing — DM path still fires',
            'WARN',
        )
    return None


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
    global _AUTO_MERGE_QUEUE_FAIL_CLOSED
    _AUTO_MERGE_QUEUE_FAIL_CLOSED = False
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


def _gh_pr_mergeable_status(repo_coords: str, pr_number: int) -> str:
    """Return 'mergeable' / 'conflicting' / 'unknown'.

    Wraps `gh pr view --json mergeable,mergeStateStatus`. Maps GitHub's
    `mergeable` field (MERGEABLE / CONFLICTING / UNKNOWN). Returns
    'unknown' on timeout / parse error / unrecognized value — callers
    treat 'unknown' with defer-then-proceed semantics so transient API
    quirks don't stall the queue forever.
    """
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


def _gh_open_prs_for_repo(repo_coords: str) -> list[dict[str, Any]]:
    """Return open PRs for `repo_coords` with number/createdAt/headRefName.

    Empty list on error. Used by `_find_overlap_blocker` to catch PRs
    that have already opened but haven't reached Mirror PASS yet — those
    can still merge before this one and create overlap conflicts.
    """
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
) -> dict[str, Any]:
    """Run both serializer gates then (if both pass) fire `_auto_merge_pr`.

    Returns a merge_result dict with one of these outcome values:
      - 'merged' / 'already_merged' / 'failed'  (from _auto_merge_pr)
      - 'held_for_blocker'  (gate 1 hit, entry pushed to queue)
      - 'held_conflict'     (gate 2 hit, DM fired, NOT queued)
      - 'deferred_unknown'  (gate 2 = UNKNOWN, first attempt; queued for retry)
      - 'held_fail_closed'  (queue file corrupt; never call _auto_merge_pr)

    `second_attempt_on_unknown=True` makes UNKNOWN proceed to the merge
    shell-out (per spec: "let git be the authority on the second attempt").
    """
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

    # Gate 2 — mergeable status.
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
        result = _attempt_auto_merge_with_gates(
            pr_url=entry.get('pr_url') or '',
            repo_coords=repo,
            pr_number=pr_number,
            task_id=entry.get('task_id') or 'unknown',
            summary=entry.get('summary') or '',
            chat_id=entry.get('reply_chat_id'),
            changed_files=entry.get('changed_files') or [],
            second_attempt_on_unknown=False,
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


def _route_beacon_pulse_auto_dispatch_approval(data: dict[str, Any]) -> bool:
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
    if marker_task_id != envelope_task_id:
        log(
            f'beacon pulse-auto-dispatch APPROVAL_REQUEST task_id mismatch '
            f'(envelope={envelope_task_id}, marker={marker_task_id!r}); '
            f'falling through to default routing',
            'WARN',
        )
        return False

    reply_chat_id = data.get('reply_chat_id')
    is_valid_chat = isinstance(reply_chat_id, int)
    if not is_valid_chat:
        log(
            f'beacon pulse-auto-dispatch APPROVAL_REQUEST for task {task_id} '
            f'has no valid reply_chat_id (got {reply_chat_id!r}); cannot '
            f'route approval DM, falling through',
            'WARN',
        )
        return False

    # Trust policy — source='pulse-auto-dispatch' so the policy file can
    # carve out per-source rules independently of beacon-sourced dispatches.
    # Bypass approval.trust_decision (which hardcodes source='beacon') and
    # call trust_policy.evaluate directly with the Pulse source.
    policy_task = {
        'source': 'pulse-auto-dispatch',
        'target_agent': payload.get('target_agent', 'forge'),
        'task_type': payload.get('task_type'),
        'target_repo': payload.get('target_repo'),
        'changed_files': payload.get('changed_files', []),
    }
    try:
        action_str, rule = trust_policy.evaluate(policy_task)
    except Exception as e:  # noqa: BLE001 — daemon-never-wedge invariant
        log(
            f'trust_policy.evaluate raised on beacon pulse-auto-dispatch '
            f'for task {task_id}: {type(e).__name__}: {e}; falling through',
            'WARN',
        )
        return False

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
                note=f'auto_approved by rule (pulse-auto-dispatch): {rule}',
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

    forge_task: dict[str, Any] = {
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
    if target_repo:
        forge_task['target_repo'] = target_repo
    pr_title = payload.get('pr_title') or data.get('pr_title')
    if pr_title:
        forge_task['pr_title'] = pr_title
    max_clar = payload.get('max_clarifications')
    if max_clar is None:
        max_clar = data.get('max_clarifications')
    if isinstance(max_clar, int) and max_clar >= 0:
        forge_task['max_clarifications'] = max_clar
    for field in ('task_type', 'summary', 'changed_files'):
        if payload.get(field) is not None:
            forge_task[field] = payload[field]
    if data.get('reply_chat_id') is not None:
        forge_task['reply_chat_id'] = data['reply_chat_id']

    # Idempotency — same shape as _dispatch_mirror_review and
    # _dispatch_build_phase. Guards against re-processing the same outbox
    # if the notifier crashes between dispatch and archive.
    forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
    filename = f'{marker_task_id}.json'
    for candidate in (
        forge_inbox / filename,
        forge_inbox / '.archive' / filename,
        forge_inbox / '.invalid' / filename,
    ):
        if candidate.exists():
            log(
                f'headless-approval-request already dispatched for task '
                f'{marker_task_id} (file or archive or .invalid present); '
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

    # Extract seq_id from the marker's prompt field. Per spec § 5.5
    # discipline 2 the canonical wording is `kickoff <seq-id>`. We also
    # accept the marker's `task_id` of shape `kickoff-<seq-id>` as a
    # fallback so a Beacon session that puts the seq_id in either place
    # still routes correctly.
    seq_id: Optional[str] = None
    prompt_text = payload.get('prompt')
    if isinstance(prompt_text, str):
        m = re.match(r'^\s*kickoff\s+([A-Za-z0-9._-]+)\s*$', prompt_text)
        if m:
            seq_id = m.group(1)
    if not seq_id:
        marker_task_id = payload.get('task_id')
        if isinstance(marker_task_id, str) and marker_task_id.startswith('kickoff-'):
            seq_id = marker_task_id[len('kickoff-'):].strip() or None
    if not seq_id:
        log(
            f'sequence-kickoff marker on task {task_id} has no parseable '
            f'seq_id (prompt={prompt_text!r}); skipping',
            'WARN',
        )
        # PR-S4 rectification (L5): loud failure beats silent. If Beacon
        # mis-emits the kickoff marker (e.g., `prompt: "approve <id>"`
        # instead of `kickoff <id>`), the handler used to archive
        # silently and the sequence stayed `pending` forever. DM Larry
        # so the malformed dispatch surfaces immediately.
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=(
                f'Build-sequence kickoff marker on task `{task_id}` has '
                f'no parseable seq_id (prompt={prompt_text!r}). Beacon '
                f'likely emitted the marker with the wrong prompt shape; '
                f'expected `kickoff <seq-id>`. Sequence remains in its '
                f'prior status; re-dispatch the kickoff with the correct '
                f'prompt.'
            ),
            subject=f'kickoff-malformed-prompt:{task_id}',
        )
        # Treat as handled — the marker explicitly addressed the advancer,
        # so falling through to Forge would be wrong. Archive without
        # writing.
        return f'sequence-kickoff:no-seq-id:{task_id}'

    seq_path = AGENTS_ROOT / 'blackboard' / 'build-sequences' / f'{seq_id}.json'
    if not seq_path.is_file():
        msg = (
            f'Sequence `{seq_id}` kickoff failed: sequence file missing at '
            f'{seq_path}. Author the sequence file (Beacon discipline 2) '
            f'before re-dispatching the kickoff.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED file-missing '
            f'task={task_id}',
            'WARN',
        )
        return f'sequence-kickoff:missing:{seq_id}'

    try:
        raw_text = seq_path.read_text()
    except OSError as e:
        msg = (
            f'Sequence `{seq_id}` kickoff failed: cannot read sequence file '
            f'at {seq_path} ({e}). Investigate filesystem/permissions.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED read-error '
            f'task={task_id}: {e}',
            'WARN',
        )
        return f'sequence-kickoff:read-error:{seq_id}'

    try:
        seq = json.loads(raw_text)
    except json.JSONDecodeError as e:
        msg = (
            f'Sequence `{seq_id}` kickoff failed: sequence file is not valid '
            f'JSON ({e}). Fix the file before re-dispatching the kickoff.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED invalid-json '
            f'task={task_id}: {e}',
            'WARN',
        )
        return f'sequence-kickoff:invalid-json:{seq_id}'

    # Lazy import: keeps build_sequence_validator out of the notifier's
    # import-time graph for environments that don't have it on sys.path
    # (e.g., minimal test fixtures that exercise unrelated handlers).
    try:
        from scripts import build_sequence_validator as bsv  # type: ignore  # noqa: E402
    except ImportError:
        try:
            import build_sequence_validator as bsv  # type: ignore  # noqa: E402
        except ImportError as e:
            log(
                f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED validator-import '
                f'task={task_id}: {e}',
                'WARN',
            )
            return f'sequence-kickoff:no-validator:{seq_id}'

    result = bsv.validate_dag(seq)
    if not result.valid:
        # PR-S4 rectification (M2): enrich the alert and append a
        # side-channel ops audit trail so the failure mode is fully
        # reconstructable. The original alert dropped only the first
        # error — that's load-bearing detail when validation rejected
        # for multiple reasons.
        errs = list(result.errors or [])
        marker_task_id = payload.get('task_id')
        first_three = errs[:3]
        more = max(0, len(errs) - 3)
        more_suffix = f' (+{more} more)' if more else ''
        formatted = '\n'.join(f'  - {e}' for e in first_three) if first_three else '  - unspecified validator error'
        msg = (
            f'Sequence `{seq_id}` kickoff failed: schema/DAG validation '
            f'failed. Marker task_id: `{marker_task_id}`. Sequence file: '
            f'`{seq_path}`.\n\nFirst validator errors{more_suffix}:\n'
            f'{formatted}\n\nRun `python3 scripts/build_sequence_validator.py '
            f'validate {seq_id}` to see all errors, then re-dispatch the '
            f'kickoff.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        # Side-channel ops audit trail. Append-only; one JSON line per
        # rejected kickoff. Survives Larry-DM history rotation.
        try:
            failures_path = (
                AGENTS_ROOT / 'blackboard' / 'build-sequences'
                / '.kickoff-failures.jsonl'
            )
            failures_path.parent.mkdir(parents=True, exist_ok=True)
            with failures_path.open('a', encoding='utf-8') as fail_f:
                fail_f.write(json.dumps({
                    'ts': datetime.now(timezone.utc).isoformat(),
                    'seq_id': seq_id,
                    'task_id': task_id,
                    'marker_task_id': marker_task_id,
                    'sequence_path': str(seq_path),
                    'errors': errs,
                }) + '\n')
        except OSError as e:
            log(
                f'kickoff-failures.jsonl append failed for seq={seq_id} '
                f'task={task_id}: {e}',
                'WARN',
            )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED validation '
            f'task={task_id}: {errs[:3]}',
            'WARN',
        )
        return f'sequence-kickoff:invalid:{seq_id}'

    current_status = seq.get('status')
    if current_status != 'pending':
        # Idempotent no-op per preflight Q2 option b: re-emitting kickoff
        # on a sequence past `pending` must NOT duplicate the
        # `kickoff-acknowledged` event or trigger a second daemon-side
        # dispatch.
        #
        # PR-S4 rectification (M3): the prior implementation archived
        # silently — if a duplicate ever appeared (Larry double-tapping
        # `approve sequence X` plus a crash mid-archive), the trail
        # vanished. Append a `kickoff-duplicate-suppressed` entry to
        # keep the audit log honest. This is a DIFFERENT event from
        # `kickoff-acknowledged`, so the existing "no duplicate
        # kickoff-acknowledged" invariant still holds.
        original_kickoff = next(
            (
                e for e in (seq.get('audit_log') or [])
                if isinstance(e, dict) and e.get('event') == 'kickoff-acknowledged'
            ),
            None,
        )
        original_task_id = (
            original_kickoff.get('task_id') if isinstance(original_kickoff, dict)
            else None
        )
        dedup_entry = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': 'kickoff-duplicate-suppressed',
            'actor': 'outbox-notifier',
            'original_task_id': original_task_id,
            'duplicate_task_id': task_id,
            'status_at_suppression': current_status,
        }
        if not isinstance(seq.get('audit_log'), list):
            seq['audit_log'] = []
        seq['audit_log'].append(dedup_entry)
        # Atomic-write the dedup entry. Errors are logged but not
        # fatal — the duplicate-suppression itself is the safety
        # property; the audit trail is a nice-to-have on top.
        dedup_tmp = seq_path.with_suffix(seq_path.suffix + '.tmp')
        try:
            with open(dedup_tmp, 'w', encoding='utf-8') as f:
                json.dump(seq, f, indent=2)
                f.write('\n')
            os.replace(dedup_tmp, seq_path)
        except OSError as e:
            try:
                dedup_tmp.unlink()
            except OSError:
                pass
            log(
                f'BUILD_SEQUENCE_KICKOFF seq={seq_id} dedup audit append '
                f'failed task={task_id}: {e}',
                'WARN',
            )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} WARN already-kicked-off '
            f'status={current_status} task={task_id}; no-op (dedup audit '
            f'entry appended)',
            'WARN',
        )
        return f'sequence-kickoff:already-active:{seq_id}'

    # Transition pending → active and append audit_log entry. Use the
    # advancer's atomic-write convention (tmp + os.replace via stdlib).
    #
    # PR-S4 rectification (L4): actor is `outbox-notifier`, not
    # `advancer` — the notifier wrote the entry; calling it `advancer`
    # was misleading for ops debugging.
    audit_entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': 'kickoff-acknowledged',
        'actor': 'outbox-notifier',
        'task_id': task_id,
    }
    seq['status'] = 'active'
    if not isinstance(seq.get('audit_log'), list):
        # Validator guarantees this is a list, but defend against schema
        # drift in case a future validator change relaxes the rule.
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
            f'Sequence `{seq_id}` kickoff failed: cannot write sequence file '
            f'at {seq_path} ({e}). Investigate filesystem/permissions.'
        )
        larry_alerts.append_alert(
            source='outbox-notifier',
            severity='warning',
            message=msg,
            subject=f'sequence-kickoff-{seq_id}',
        )
        log(
            f'BUILD_SEQUENCE_KICKOFF seq={seq_id} FAILED write-error '
            f'task={task_id}: {e}',
            'WARN',
        )
        return f'sequence-kickoff:write-error:{seq_id}'

    log(
        f'BUILD_SEQUENCE_KICKOFF seq={seq_id} status=pending->active '
        f'task={task_id} (next advancer tick will dispatch the first step)'
    )
    return str(seq_path)


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

    forge_task: dict[str, Any] = {
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
    if data.get('reply_chat_id') is not None:
        forge_task['reply_chat_id'] = data['reply_chat_id']
    # Propagate clarification budget so Forge knows how many CLARIFY_REQUESTs
    # remain. Beacon's response is one round; count is what the envelope
    # already carries (incremented by the marker handler when Forge first
    # emitted CLARIFY_REQUEST, propagated through Beacon's round-trip).
    if data.get('clarification_count') is not None:
        forge_task['clarification_count'] = data['clarification_count']
    if data.get('max_clarifications') is not None:
        forge_task['max_clarifications'] = data['max_clarifications']
    # Propagate target_repo/branch/pr_title/pr_body so Forge's worktree
    # gate accepts the continuation envelope. Same shape as the default
    # routing path which propagates these via the
    # `4b post-test-2 fix` block.
    for f_name in ('target_repo', 'branch', 'pr_title', 'pr_body', 'pr_url'):
        if data.get(f_name):
            forge_task[f_name] = data[f_name]

    # Filename — `resume-<task>-r<count>.json`. The clarification_count
    # discriminator makes the filename unique per round, so multi-round
    # cascades (Forge clarifies, Beacon answers, Forge clarifies again,
    # Beacon answers again) produce distinct files in inbox/.archive.
    # Idempotent on retry: if the daemon crashes between dispatch and
    # archive, the next poll re-processes the outbox; the second write
    # hits the inbox+archive+invalid existence check and skips. Same
    # idempotency shape as _handle_beacon_headless_approval_request.
    count_for_filename = data.get('clarification_count', 0) or 0
    filename = f'resume-{original_task_id}-r{count_for_filename}.json'
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
        pr_url = _extract_pr_url_from_build_result(data.get('result', ''))
        if pr_url:
            _dispatch_mirror_review(data, pr_url)
        # No marker, so marker classification below returns None and the
        # default routing path takes over (Beacon notify with the full
        # build result narrative).

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
            _notify_forge_marker_error(
                data,
                'phase=revision requires response to START with '
                '"Revision N applied: <one-line summary>" preamble — none '
                'found. Re-read agents/forge/CLAUDE.md Revision phase '
                'protocol — the preamble is the structural signal that '
                'revision completed; the rest of the response is narrative '
                'underneath.',
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
    marker_decision: Optional[dict[str, Any]] = None
    if agent == 'forge':
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
        prompt = build_notify_prompt(
            intent=marker_decision['intent'],
            sender=agent,
            task_id=task_id,
            success=data.get('exit_code', 0) == 0,
            output=_marker_output_for_prompt(data, marker_decision),
            error=data.get('error') or '',
            intent_kwargs=marker_decision['intent_kwargs'],
        )
        notify_task: dict[str, Any] = {
            'task_id': f'notify-{task_id}',
            'prompt': prompt,
            'source': marker_decision['notify_source'],
            'intent': marker_decision['intent'],
            # Depth still tracked for telemetry; budget supersedes the cap.
            '_notify_depth': _current_notify_depth(data) + 1,
        }
        if data.get('reply_chat_id') is not None:
            notify_task['reply_chat_id'] = data['reply_chat_id']
        if data.get('claude_session_id'):
            notify_task['session_id'] = data['claude_session_id']
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
            notify_task['forge_session_id'] = data['claude_session_id']
        # Propagate clarification budget so the next leg has the counter.
        if marker_decision['next_clarification_count'] is not None:
            notify_task['clarification_count'] = marker_decision['next_clarification_count']
        if data.get('max_clarifications') is not None:
            notify_task['max_clarifications'] = data['max_clarifications']
        # Phase D3 commit 4b post-test-2 fix: propagate target_repo + branch
        # + pr_title/pr_body forward across the full clarification cascade
        # (forge→beacon question, then beacon→forge answer, then forge
        # re-preflight). Without these on the notify task, _build_outbox
        # on Beacon's side has nothing to propagate, the answer leg arrives
        # at Forge with target_repo=None, and the watcher's worktree gate
        # refuses with "no canonical path". Same shape as the marker-error
        # black hole the 4b review caught — different code path.
        for f_name in ('target_repo', 'branch', 'pr_title', 'pr_body',
                       'pr_url'):
            if data.get(f_name):
                notify_task[f_name] = data[f_name]
        # D3.5 5c — when the marker decision is review-escalate (any of three
        # sub-flavors: direct REVIEW_ESCALATE, auto-promoted from low-
        # confidence REVISION, or budget-exhausted REVISION), surface the
        # replan budget + Mirror's reason on the notify task so Beacon's
        # CLAUDE.md decision tree has the data it needs without re-reading
        # her inbox archive. `mirror_escalate_reason` rides forward through
        # _build_outbox propagation so the notifier can apply the level-3
        # discipline gate when Beacon emits her replan APPROVAL_REQUEST.
        if marker_decision['intent'] == 'review-escalate':
            notify_task['replan_count'] = data.get('replan_count', 0) or 0
            max_replans = data.get('max_replans')
            if not isinstance(max_replans, int) or max_replans < 0:
                max_replans = _load_max_replans_from_config()
            notify_task['max_replans'] = max_replans
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
            if reason:
                notify_task['mirror_escalate_reason'] = reason

        # task-19 (2026-05-19) — gate ONLY the back-leg inter-agent notify
        # on `not larry_direct`. The dispatch helpers below
        # (`_dispatch_build_phase`, `_dispatch_revision_to_forge`) write
        # into a different agent's inbox to advance the chain; they don't
        # depend on having an upstream agent to notify, so they MUST fire
        # for source='larry' too. PR #46 incorrectly hid them under the
        # `not larry_direct` gate, which is what caused Forge's PROCEED on
        # task-17's larry-direct preflight to silently skip build-phase
        # dispatch (Larry had to manually bridge the build envelope).
        if not larry_direct:
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
                log(
                    f'marker notify failed for {outbox_file.name}: '
                    f'{type(e).__name__}: {e}',
                    'WARN',
                )
                _archive_outbox(outbox_file)
                return 'notify-failed'

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

        # D3.5 5d — auto-merge on Mirror REVIEW_PASS. Order per Larry's
        # sign-off: merge fires BEFORE the closing DM renders so the DM
        # body accurately reflects what happened (merged / already_merged /
        # failed). `merge_result` is attached to marker_decision so
        # `_render_dm_message` can pick the outcome-aware DM variant. The
        # outbox archives last so a daemon crash between merge and archive
        # leads to re-processing the same outbox; the second call gets
        # `already_merged` from `_gh_pr_state` and the same success DM body.
        if (
            marker_decision['marker_type'] == 'review_pass'
            and agent == 'mirror'
        ):
            payload = marker_decision.get('payload') or {}
            pr_url = payload.get('pr_url') if isinstance(payload, dict) else None
            if pr_url:
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
                        f'({shape_reason})',
                        'WARN',
                    )
                    _archive_outbox(outbox_file)
                    return 'auto-merge-skipped'
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
                        f'({exist_reason})',
                        'WARN',
                    )
                    _archive_outbox(outbox_file)
                    return 'auto-merge-skipped'
                if pr_state != 'OPEN':
                    log(
                        f'AUTO_MERGE task={task_id_log} pr={pr_url!r} '
                        f'outcome=skipped reason=pr-state-{pr_state} '
                        f'(already terminal)',
                    )
                    _archive_outbox(outbox_file)
                    return 'auto-merge-skipped'
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

        # D3.5 5a-followup: chain-completion DM to the originating Telegram
        # thread. Fires only for terminal-from-Larry's-perspective intents
        # (review-pass/revision/escalate/emergency, plus Forge preflight
        # reject/clarification-exhausted) and only when reply_chat_id is
        # propagated through the chain. Non-fatal on failure.
        # D3.5 5d: review-pass DM body now reflects the merge_outcome
        # attached above; the render pipeline picks the correct variant.
        _maybe_dm_larry(data, marker_decision)

        _archive_outbox(outbox_file)
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
    notify_task = {
        'task_id': f'notify-{task_id}',
        'prompt': prompt,
        'source': notify_source,
        'intent': intent,
        '_notify_depth': next_depth,
    }
    if data.get('reply_chat_id') is not None:
        notify_task['reply_chat_id'] = data['reply_chat_id']
    # Propagate session_id so clarification-response delivery can resume
    # the original Forge session (commit 4b wires the watcher to honor it).
    if data.get('claude_session_id'):
        notify_task['session_id'] = data['claude_session_id']
    # Carry clarification budget across the cascade so it reaches Forge with
    # the correct count on the resume leg.
    if data.get('clarification_count') is not None:
        notify_task['clarification_count'] = data['clarification_count']
    if data.get('max_clarifications') is not None:
        notify_task['max_clarifications'] = data['max_clarifications']
    # Phase D3 commit 4b post-test-2 fix: propagate target_repo + branch +
    # pr_title/pr_body so the clarification-answer leg back to Forge passes
    # the worktree gate. See the matching block in the marker-decision path
    # above for the full explanation.
    for f_name in ('target_repo', 'branch', 'pr_title', 'pr_body'):
        if data.get(f_name):
            notify_task[f_name] = data[f_name]

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
            notify_task: dict[str, Any] = {
                'task_id': f'dead-letter-{invalid_file.stem}',
                'prompt': dl_prompt,
                'source': f'{agent}-result',
                'intent': 'dead-letter',
                '_notify_depth': 1,  # this IS a depth-1 message; further loops cap
            }
            if task_data.get('reply_chat_id') is not None:
                notify_task['reply_chat_id'] = task_data['reply_chat_id']

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

        # Sleep in short slices so SIGTERM is responsive.
        slept = 0.0
        while _running and slept < POLL_INTERVAL_SECONDS:
            time.sleep(0.5)
            slept += 0.5

    log('outbox-notifier exiting')
    return 0


if __name__ == '__main__':
    sys.exit(main_loop())
