#!/usr/bin/env python3
"""heal_wedged_review_sessions.py — reap Mirror/Forge review `claude -p`
sessions that wedge AFTER their work is done.

Incident (2026-06-03)
---------------------
Mirror reviewed PR #297, emitted the canonical `=== REVIEW_PASS ===` marker
(the PR auto-merged), then her `claude -p` process froze for ~30 min in a
harness background-Bash poll loop — the classic self-matching
`until ! kill -0 $(cat <vanished-file>; echo $$)` pattern that never exits.
She held a fleet slot + a live Opus session long after her work was in hand.

`heal_zombie_main_workers.py` PATTERN B describes this exact failure shape,
but it is scoped to main/pulse-class workers (sysprompt-main filter, cwd
under `/tmp/wt-main-*`, a 4h etime floor), so it never sees review agents
living in `~/agent-worktrees/wt-{mirror,forge}-*`. This healer closes that
gap for the review tier.

Scope
-----
`claude` processes whose cwd is `~/agent-worktrees/wt-mirror-*` or
`wt-forge-*`. For each, the session's activity log is the Claude Code
session JSONL under `~/.claude/projects/<slug>/<session>.jsonl` (slug =
cwd with `/` → `-`). The JSONL mtime is last-activity; the JSONL body is
grepped for the canonical terminal marker for that agent tier:

  - Mirror: REVIEW_PASS / REVIEW_REVISION / REVIEW_ESCALATE /
            REVIEW_EMERGENCY_HALT  (mirror_review_handler.MARKER_KEYWORDS)
  - Forge:  PROCEED / CLARIFY_REQUEST / REJECT
            (forge_preflight_handler.MARKER_KEYWORDS) PLUS the build/
            revision terminal preambles `PR opened:` / `PR updated:` /
            `Revision N applied:` (the post-marker exit signals from
            agents/forge/CLAUDE.md).

Two cases
---------
CASE 1 — provably-done (AUTO-REAP, day one):
  terminal marker present in the JSONL AND process alive AND JSONL idle
  > marker_grace_seconds (default 300). The marker is proof the work is in
  hand, so reaping is zero-false-positive: SIGTERM→SIGKILL the process
  tree, `git worktree remove --force` + prune the stale worktree (guarded
  on the canonical repo being on `main`), emit HEALED + a CLOSURE notify.

CASE 2 — silent (NO marker):
  JSONL idle > silent_grace_seconds (default 900) AND process alive AND no
  marker. Starts in ALERT-ONLY mode — escalate, do NOT kill. A confidence
  ladder (reusing the Pulse promotion-ladder shape — a tail-read
  consecutive-success streak over a JSON execution store) tracks outcomes:
  a session that later emits a marker or resumes activity was a FALSE
  positive (live work) → reset streak + demote to alert-only; a session
  that stayed dead with no marker was a TRUE positive → increment streak.
  After streak_to_promote (default 3) consecutive true positives, Case 2
  graduates to auto-reap (one-time graduation CLOSURE notify). Any miss
  auto-demotes back to alert-only.

  HARD backstop (deterministic, mode-independent): a no-marker session idle
  past hard_silent_grace_seconds (default 3600 = 60 min) is auto-reaped
  immediately, regardless of the confidence ladder. The justification is
  arithmetic, not statistical: the longest legitimate silent operation is the
  foreground regression gate (test_regression_check.py, ~10 min wall for both
  SHAs at the 300s/SHA default), so 60 min of silence is 4–6× anything a live
  review could be doing — it is provably wedged. This closes the gap Pulse
  flagged on PR #334 (2026-06-05): the healer could WARN at 15 min but not act
  until the streak graduated, so the first wedges (71 min PR #101, 102 min
  PR #334) needed a manual kill. The hard path lets it act on the FIRST wedge
  while the 15-min streak path stays conservative. If the session was already
  alerted (it crossed silent_grace in an earlier sweep, so it has a pending
  entry), the hard reap is credited as a TRUE positive by verify_pending on the
  next sweep, nudging the 15-min path toward graduation; a first-sight hard reap
  (the healer was down while the session wedged, so it never passed through the
  alert path and has no pending entry) kills it without a streak credit — which
  is fine, the hard path doesn't depend on the streak. The same fresh
  resumed-at-gate recheck as the graduated path guards it, so a session that
  resumed between scan and kill is never reaped.

Config (Pulse-Check-tunable; NOT hardcoded)
-------------------------------------------
`config/review-reaper-rules.json`: marker_grace_seconds, silent_grace_seconds,
hard_silent_grace_seconds, streak_to_promote, build_handoff_grace_seconds,
wedge_progress_grace_seconds, enabled. Missing/malformed → conservative built-in
defaults (never raises). Pulse adjusts the JSON; no constant is hand-picked in
this file.

Progress-staleness deadlock-break (2026-06-24 forge-post-open-mergeable-rebase-001)
----------------------------------------------------------------------------------
A Forge BUILD session can wedge AFTER its work is in hand (PR opened, Mirror
passed) — e.g. on a self-referential shell wait-loop that never exits. Its PR is
OPEN-but-not-merged and its worktree is INTACT (the process stayed alive), so the
two decisive handoff-release signals (`_forge_handoff_is_live`: worktree-deleted /
PR-merged-or-closed) both miss; meanwhile the `build-<task>.json` the wedged
process never cleanly archived sits in the inbox, so the queued-build spare
condition protects it forever. Self-sustaining deadlock: the wedge holds the
single Forge slot, the build file can't be consumed while it does, and the file
keeps the spare alive — so the slot leaked for ~3.9h until the 4h timeout. The
escape hatch is a forward-PROGRESS signal: `_forge_spare_reason` releases the
spare (bypassing BOTH the queued-build and open-unreviewed-PR guards, which only
protect work genuinely IN FLIGHT) once the session has made no new commit AND no
new session-log activity for `wedge_progress_grace_seconds` (~25 min, well under
4h). The reap then kills the PID to free the slot but PRESERVES the worktree, so
the watcher's in-process `--resume` retry finishes the task cleanly — the proven
manual recovery. Requiring BOTH progress signals stale keeps it conservative; an
unreadable commit age fails safe toward sparing.

Coexistence with heal_zombie_main_workers.py (no double-kill)
------------------------------------------------------------
The two healers' domains are disjoint by construction: the zombie healer
acts only on sysprompt-main procs whose cwd is `/tmp/wt-main-*` or
`(deleted)`; this healer acts only on cwds under
`~/agent-worktrees/wt-{mirror,forge}-*`. We additionally hard-skip any
cwd in the zombie healer's domain so a future config change can't make
both target the same PID.

Self-protection
---------------
Zero LLM / `claude` subprocess calls — pure /proc reads, file greps, signals,
and `git worktree`. A reaper that spun up a model to decide would defeat
its own purpose and burn the very slot it is trying to free.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import forge_preflight_handler  # noqa: E402
import mirror_review_handler  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-wedged-review-sessions.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-wedged-review-sessions.heartbeat'
# Confidence ladder state-of-record (Case 2). Same family as
# promote_alerts' promotion-probation.json + pulse_check_v's clean_streak
# registry: a JSON doc holding a tail-read execution streak + mode.
STATE_FILE = AGENTS_ROOT / 'state' / 'review-reaper-confidence.json'
CLAUDE_PROJECTS_DIR = HOME / '.claude' / 'projects'
# Tier2 sessions (Mirror, and Forge under the auth HOME-swap) run with
# HOME=/home/larry/.claude-larry-personal (agent_runner.TIER2_HOME), so their
# Claude Code session JSONL lands under THAT home's projects dir — NOT the login
# HOME's. A reaper that searched only CLAUDE_PROJECTS_DIR found no JSONL for any
# tier2 session, fell into scan_candidates' `jsonl is None -> idle_secs=0.0`
# branch, and SKIPped it forever: the reaper was structurally blind to Mirror
# wedges (every one needed a manual kill). Search BOTH roots so the existing
# machinery (hard 60-min backstop, confidence ladder, progress-staleness) finally
# applies to tier2; the newest-mtime JSONL across roots is the live session.
TIER2_HOME = Path(os.environ.get('OURLIBERTY_TIER2_HOME', '/home/larry/.claude-larry-personal'))
TIER2_CLAUDE_PROJECTS_DIR = TIER2_HOME / '.claude' / 'projects'
CLAUDE_PROJECTS_DIRS: tuple[Path, ...] = (CLAUDE_PROJECTS_DIR, TIER2_CLAUDE_PROJECTS_DIR)
CONFIG_FILE = _SCRIPTS_DIR.parent / 'config' / 'review-reaper-rules.json'

# Review-tier worktrees this healer owns.
WORKTREE_PREFIXES = (
    str(HOME / 'agent-worktrees' / 'wt-mirror-'),
    str(HOME / 'agent-worktrees' / 'wt-forge-'),
)
# heal_zombie_main_workers.py's domain — hard-skip so the two healers can
# never target the same PID even if conventions drift later.
ZOMBIE_HEALER_CWD_PREFIX = '/tmp/wt-main-'

# Conservative built-in defaults — used verbatim when the config file is
# missing or malformed, so a config mishap can never make the healer kill
# more aggressively than intended.
DEFAULT_CONFIG: dict[str, Any] = {
    'enabled': True,
    'marker_grace_seconds': 300,         # 5 min  — Case 1 post-marker idle floor
    'silent_grace_seconds': 900,         # 15 min — Case 2 no-marker idle floor (alert)
    'hard_silent_grace_seconds': 3600,   # 60 min — Case 2 deterministic auto-reap backstop
    'streak_to_promote': 3,              # consecutive true positives → auto-reap
    # Per-task build-handoff grace, keyed to the build-sequence advancer's
    # cadence (ourliberty-build-sequence-advancer.timer / outbox dispatch runs
    # on a 300s OnCalendar=*:0/5). A Forge session that has emitted a terminal
    # marker (e.g. PROCEED) sits idle in the preflight→build handoff window while
    # the advancer dispatches its next phase; this is ADDED on top of
    # marker_grace_seconds so a marker-present Forge session isn't reaped before
    # the advancer has had a full cycle to dispatch — closing the false-positive
    # reaping that stranded ccd-s1 (2026-06-10).
    'build_handoff_grace_seconds': 300,
    # Progress-staleness grace (2026-06-24 forge-post-open-mergeable-rebase-001
    # incident). A Forge build session that wedges AFTER its work is in hand —
    # PR opened, Mirror passed — keeps an OPEN (not-yet-merged) PR and an INTACT
    # worktree, so neither decisive handoff-release signal (worktree-deleted /
    # PR-merged-or-closed) ever fires; meanwhile the build-<task>.json it never
    # cleanly archived sits in the inbox, so the queued-build spare condition
    # protects it forever. That is a self-sustaining deadlock: the wedge holds
    # the single Forge slot, its build file can't be consumed, the file keeps
    # the spare alive. This grace is the escape hatch — a session that has made
    # NO forward progress (no new commit in its worktree AND no new session-log
    # activity) for this long is provably wedged: well past the ~10-min
    # regression gate (the longest legitimate silent operation) and far under
    # the 4h timeout=14400s backstop that used to be the only floor. When it
    # trips, the slot is freed (kill the claude PID; the worktree is PRESERVED
    # so agent_runner.run_claude's in-process --resume retry can finish the task
    # cleanly, exactly as the manual recovery did). Requiring BOTH signals stale
    # keeps it conservative — a live build mid-test keeps its JSONL warm between
    # tool calls, so it is never falsely declared wedged.
    'wedge_progress_grace_seconds': 1500,  # 25 min
}

# Sentinel the hard backstop is pushed to when config is mistuned (hard <=
# silent): an idle window no real session can ever reach (~31,000 years), so
# the hard path is effectively disabled and only the conservative alert ladder
# runs. Far larger than any plausible idle_secs.
_HARD_BACKSTOP_DISABLED = 10 ** 12

# Tree-kill: how long to wait after SIGTERM before escalating to SIGKILL.
SIGTERM_GRACE_SECONDS = 5

GIT_TIMEOUT_SEC = 60

# For the Forge-PR reap guard: a Forge session that has already opened a PR has
# delivered its work, and reaping it before that PR's Mirror review is dispatched
# risks orphaning the PR (the #412 incident). We check GitHub directly for an open
# PR on the session's branch. The durable recovery is heal_undispatched_pr_review
# (GitHub-truth, runs independently), so this guard is a best-effort optimization
# that fails SAFE toward normal reaping on any gh error — the healer nets it.
GH_TIMEOUT_SEC = 30
REPO = 'Larry-Yatch/ourliberty-agent-core'
FORGE_WORKTREE_NAME_PREFIX = 'wt-forge-'
# The kernel appends this to /proc/<pid>/cwd when a process's working directory
# (here, a Forge worktree) was removed out from under the still-running process —
# the exact fingerprint of a wedged worker whose worktree was already torn down
# (the 2026-06-11 pulse-park-and-silence deadlock).
_DELETED_CWD_SUFFIX = ' (deleted)'
# Upper bound on how long the Forge-PR guard will protect a session from reaping.
# The guard releases as soon as a review is dispatched (the normal case, within
# ~the healer's grace+interval), but a case1 (marker-present) build session never
# transitions to the 60-min hard backstop, so without an explicit cap a session
# whose review never dispatches (e.g. heal_undispatched_pr_review disabled, or an
# exotic non-round-tripping task_id) would be protected forever and leak a fleet
# slot. After this age the deliverable (the PR) is long safe; reap and let
# heal_undispatched_pr_review remain the net for the review.
GUARD_MAX_PROTECT_HOURS = 2

# --- classification verdicts ---
REAP_CASE1 = 'reap_case1'              # provably done (marker present)
SILENT_CASE2 = 'silent_case2'          # no marker, idle past silent grace (alert/ladder)
REAP_CASE2_HARD = 'reap_case2_hard'    # no marker, idle past HARD grace (deterministic reap)
SKIP = 'skip'

# --- confidence-ladder modes ---
MODE_ALERT_ONLY = 'alert_only'
MODE_AUTO_REAP = 'auto_reap'

# --- execution outcomes (tail-read streak vocabulary) ---
TRUE_POSITIVE = 'true_positive'
FALSE_POSITIVE = 'false_positive'


# ==================== logging / heartbeat ====================

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat() + '\n')
    except OSError as e:
        log(f'heartbeat write failed: {e}', 'WARN')


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


# ==================== config ====================

def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    """Read review-reaper-rules.json over the conservative defaults.

    Missing file / malformed JSON / wrong types → defaults (never raises).
    Each known key is validated independently so one bad value doesn't
    discard the rest of the file.
    """
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text())
    except OSError:
        return cfg
    except json.JSONDecodeError as e:
        log(f'config malformed ({e}); using conservative defaults', 'WARN')
        return cfg
    if not isinstance(data, dict):
        log('config top-level not an object; using defaults', 'WARN')
        return cfg
    if isinstance(data.get('enabled'), bool):
        cfg['enabled'] = data['enabled']
    for key in ('marker_grace_seconds', 'silent_grace_seconds',
                'hard_silent_grace_seconds', 'streak_to_promote',
                'build_handoff_grace_seconds', 'wedge_progress_grace_seconds'):
        raw = data.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            cfg[key] = int(raw)
        elif key in data:
            log(f'config {key}={raw!r} invalid; keeping default {cfg[key]}', 'WARN')
    # Safety invariant — this file's contract is "a config mishap can never make
    # the healer kill more aggressively than intended." The hard deterministic-
    # reap ceiling MUST sit above the soft alert floor; otherwise a mistuned JSON
    # (hard <= silent) would make every silent session cross the hard gate first
    # and be auto-reaped immediately, bypassing the alert ladder + streak
    # graduation. If that happens, disable the hard backstop (push it out of
    # reach) and warn — a config error must make us LESS aggressive, never more.
    if cfg['hard_silent_grace_seconds'] <= cfg['silent_grace_seconds']:
        log(f'config hard_silent_grace_seconds={cfg["hard_silent_grace_seconds"]} '
            f'<= silent_grace_seconds={cfg["silent_grace_seconds"]}; disabling the '
            f'hard backstop (it would bypass the alert ladder)', 'WARN')
        cfg['hard_silent_grace_seconds'] = _HARD_BACKSTOP_DISABLED
    return cfg


# ==================== process scan (/proc) ====================

def claude_pids() -> list[int]:
    """All live `claude` PIDs (broad match; cwd filtering happens later)."""
    try:
        out = subprocess.run(
            ['pgrep', '-f', 'claude'],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0:
        return []
    pids: list[int] = []
    for s in out.stdout.split():
        if s.isdigit():
            pids.append(int(s))
    return pids


def proc_cwd(pid: int) -> Optional[str]:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except OSError:
        return None


def proc_etime_secs(pid: int) -> Optional[int]:
    try:
        out = subprocess.run(
            ['ps', '-o', 'etimes=', '-p', str(pid)],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return int(out.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def agent_tier_for_cwd(cwd: str) -> Optional[str]:
    """'mirror' / 'forge' for an owned review worktree, else None.

    A cwd in the zombie healer's domain (or otherwise outside our prefixes)
    returns None so we never act on it (no-double-kill guard).
    """
    if cwd.startswith(ZOMBIE_HEALER_CWD_PREFIX):
        return None
    mirror_prefix, forge_prefix = WORKTREE_PREFIXES
    if cwd.startswith(mirror_prefix):
        return 'mirror'
    if cwd.startswith(forge_prefix):
        return 'forge'
    return None


# ==================== session JSONL ====================

def cwd_to_slug(cwd: str) -> str:
    """Claude Code's project-dir slug: the absolute cwd with every '/'
    replaced by '-' (e.g. /home/larry/agent-worktrees/wt-mirror-x →
    -home-larry-agent-worktrees-wt-mirror-x)."""
    return cwd.replace('/', '-')


def session_jsonl_for_cwd(
    cwd: str, projects_dir: Optional[Path] = None,
) -> Optional[Path]:
    """The most-recently-modified *.jsonl for the cwd's project-slug, across ALL
    Claude projects roots, or None if none exists.

    Searches both the login HOME's projects dir AND the tier2 personal HOME's
    (``CLAUDE_PROJECTS_DIRS``): tier2 review sessions (Mirror, Forge under the
    auth HOME-swap) write their JSONL under the personal HOME, so a single-root
    search of the login HOME missed every tier2 session — the reaper's blind
    spot. The newest-mtime JSONL across roots is the session the live process is
    writing to.

    ``projects_dir`` (a single Path) is honored for back-compat with callers/
    tests that pin one root; the default (None) searches all roots.
    """
    roots = (projects_dir,) if projects_dir is not None else CLAUDE_PROJECTS_DIRS
    slug = cwd_to_slug(cwd)
    candidates: list[Path] = []
    for root in roots:
        slug_dir = root / slug
        try:
            candidates.extend(p for p in slug_dir.glob('*.jsonl') if p.is_file())
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# Forge build/revision terminal preambles — MIRROR the outbox-notifier's
# canonical grammar (outbox_notifier._PR_URL_RE / _REVISION_APPLIED_RE) so the
# reaper treats as "work in hand" exactly what the notifier treats as a terminal
# signal: a FIRST-LINE 'PR [#N ]opened:/updated: <pull-url>' or
# 'Revision N applied:'. Two precision details are load-bearing:
#   - the PR form REQUIRES the github pull URL, so ordinary prose
#     ('I considered PR opened: ... last week') without a URL does NOT match
#     (the old bare 'PR opened:' substring did), AND a real 'PR #303 opened:
#     <url>' (the actual shape Forge writes) IS matched (the old 'PR opened:'
#     literal MISSED the '#303' token — a false negative);
#   - the revision form requires the 'applied:' colon, so 'the Revision phase'
#     or 'Revision 2 applied cleanly' prose does NOT match.
# Compiled once at module load; matched per stripped assistant line via .match
# (line-anchored). An optional leading clause ('Done. ', 'Result: ') is allowed
# exactly as the notifier allows it.
_FORGE_PREAMBLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r'(?:[^\n]*?[.:][ \t]+)?'          # optional leading clause ('Done. ')
        r'PR[ \t]+(?:#\d+[ \t]+)?'         # 'PR ' + optional '#303 '
        r'(?:opened|updated):[ \t]*'
        r'https://github\.com/[^\s]+/pull/\d+'
    ),
    re.compile(r'Revision\s+\d+\s+applied:', re.IGNORECASE),
)


def terminal_markers_for_tier(
    tier: str,
) -> tuple[tuple[str, ...], tuple[re.Pattern[str], ...]]:
    """Return ``(substr_markers, line_anchored_patterns)`` whose presence in a
    session JSONL proves the agent's work is in hand for that tier.

    ``substr_markers`` — distinctive block delimiters ('=== REVIEW_PASS ===',
    etc.) safe to match anywhere in an assistant message.

    ``line_anchored_patterns`` — compiled regexes that must match at the START
    of a stripped assistant line. Forge's build/revision terminal signals are
    short English phrases ('PR opened:', 'Revision N applied:'); as bare
    substrings they false-match ordinary prose — e.g. a live agent writing 'the
    Revision phase' or a recap mentioning 'PR opened:'. A false match here is
    not cosmetic: it sets marker_present=True, and once the JSONL is idle past
    marker_grace (e.g. during the foreground regression gate) classify() returns
    REAP_CASE1, which SIGKILLs the process and force-removes its worktree — a
    destructive false positive on LIVE work. Anchoring to line-start plus the
    precise 'Revision <N> applied:' form (not bare 'Revision ') closes that.

    Reuses the handler modules' MARKER_KEYWORDS rather than re-listing the
    grammar, so a marker rename propagates automatically.
    """
    if tier == 'mirror':
        substrs = tuple(
            f'=== {kw} ===' for kw in mirror_review_handler.MARKER_KEYWORDS.values()
        )
        return substrs, ()
    if tier == 'forge':
        substrs = tuple(
            f'=== {kw} ===' for kw in forge_preflight_handler.MARKER_KEYWORDS.values()
        )
        return substrs, _FORGE_PREAMBLE_PATTERNS
    return (), ()


def _assistant_text_from_jsonl_line(obj: Any) -> str:
    """Return assistant-authored text from one parsed JSONL record, or '' for
    any non-assistant record (user, tool_result, system, summary).

    The session JSONL is one JSON object per line. The emitted terminal marker
    lives in an *assistant* message; everything else — including the agent's
    own CLAUDE.md read back as a user/tool_result at startup — must be ignored.
    """
    if not isinstance(obj, dict):
        return ''
    msg = obj.get('message')
    role = msg.get('role') if isinstance(msg, dict) else None
    if obj.get('type') != 'assistant' and role != 'assistant':
        return ''
    content = msg.get('content') if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block['text']
            for block in content
            if isinstance(block, dict)
            and block.get('type') == 'text'
            and isinstance(block.get('text'), str)
        ]
        return '\n'.join(parts)
    return ''


def jsonl_has_terminal_marker(jsonl: Path, tier: str) -> bool:
    """True if an ASSISTANT-authored message in the session JSONL contains any
    terminal marker for the tier.

    Role filtering is load-bearing, not cosmetic: every Mirror/Forge session
    reads its own CLAUDE.md at startup, and that operating manual contains the
    literal marker delimiters (e.g. '=== REVIEW_PASS ===', 'PR opened:') in its
    examples. The Read tool-result persists into the JSONL as a user line, so a
    whole-file substring grep matches from startup onward — long before any
    verdict is emitted, which would let a benignly-idle review (e.g. running the
    foreground regression gate) be auto-reaped mid-flight. The emitted marker
    lives in an assistant line; only those count as proof of completion.

    Parses line-by-line; tolerates non-JSON / decode errors. On read failure
    returns False (conservative: no proof of done → not a Case-1 reap)."""
    substrs, line_patterns = terminal_markers_for_tier(tier)
    if not substrs and not line_patterns:
        return False
    try:
        with jsonl.open('r', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                text = _assistant_text_from_jsonl_line(obj)
                if not text:
                    continue
                # Distinctive block delimiters: match anywhere.
                if substrs and any(m in text for m in substrs):
                    return True
                # Forge exit preambles: only a LINE that STARTS with the
                # preamble counts (a bare-substring match would reap a live
                # agent merely discussing 'the Revision phase').
                if line_patterns:
                    for textline in text.splitlines():
                        stripped = textline.strip()
                        if stripped and any(p.match(stripped) for p in line_patterns):
                            return True
    except OSError:
        return False
    return False


# ==================== pure classification ====================

def classify(
    *,
    marker_present: bool,
    idle_secs: float,
    cfg: dict[str, Any],
) -> str:
    """Pure verdict for one live review session.

    REAP_CASE1      — marker present and idle past marker_grace (work is in hand).
    REAP_CASE2_HARD — no marker and idle past hard_silent_grace (provably wedged:
                      longer than any legitimate silent operation; deterministic
                      auto-reap, independent of the confidence ladder).
    SILENT_CASE2    — no marker and idle past silent_grace (possible wedge; the
                      alert/confidence-ladder path).
    SKIP            — still fresh / actively working.

    The hard gate is checked before the soft gate so a session past the hard
    ceiling always reaps regardless of how the two thresholds are configured.
    """
    if marker_present:
        if idle_secs > cfg['marker_grace_seconds']:
            return REAP_CASE1
        return SKIP
    if idle_secs > cfg['hard_silent_grace_seconds']:
        return REAP_CASE2_HARD
    if idle_secs > cfg['silent_grace_seconds']:
        return SILENT_CASE2
    return SKIP


# ==================== confidence ladder (Pulse promotion-ladder shape) ====================

def consecutive_true_positive_streak(executions: Optional[list[dict[str, Any]]]) -> int:
    """Consecutive TRUE_POSITIVE outcomes counted from the TAIL of the
    execution list. A FALSE_POSITIVE (or any non-true-positive row) breaks
    the streak. This is the single reader of the Case-2 track record —
    mirrors pulse_check_v.consecutive_clean_streak so the graduation
    semantics are identical to the rest of the constellation."""
    if not executions:
        return 0
    streak = 0
    for ex in reversed(executions):
        if isinstance(ex, dict) and ex.get('outcome') == TRUE_POSITIVE:
            streak += 1
        else:
            break
    return streak


def mode_for_streak(streak: int, *, threshold: int, current_mode: str) -> str:
    """Graduate to auto-reap once the streak reaches the threshold; otherwise
    stay alert-only. A streak of 0 (just-reset by a false positive) is always
    alert-only, which is how a miss auto-demotes."""
    if streak >= threshold:
        return MODE_AUTO_REAP
    return MODE_ALERT_ONLY


@dataclass
class ConfidenceState:
    mode: str = MODE_ALERT_ONLY
    executions: list[dict[str, Any]] = field(default_factory=list)
    # session_id -> {first_alert_ts, jsonl_path, jsonl_mtime_at_alert, cwd, tier}
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def streak(self) -> int:
        return consecutive_true_positive_streak(self.executions)


def load_state(path: Path = STATE_FILE) -> ConfidenceState:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ConfidenceState()
    if not isinstance(data, dict):
        return ConfidenceState()
    execs = data.get('executions')
    pending = data.get('pending')
    mode = data.get('mode')
    return ConfidenceState(
        mode=mode if mode in (MODE_ALERT_ONLY, MODE_AUTO_REAP) else MODE_ALERT_ONLY,
        executions=execs if isinstance(execs, list) else [],
        pending=pending if isinstance(pending, dict) else {},
    )


def save_state(state: ConfidenceState, path: Path = STATE_FILE) -> None:
    """Atomic tmp+rename write. Caps the execution log to the last 50 rows so
    it can't grow unbounded — the streak only reads the tail anyway."""
    payload = {
        'version': 1,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'mode': state.mode,
        'streak': state.streak,
        'executions': state.executions[-50:],
        'pending': state.pending,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.rename(path)
    except OSError as e:
        log(f'state write failed: {type(e).__name__}: {e}', 'WARN')


# ==================== reap action ====================

def child_pids(pid: int) -> list[int]:
    try:
        out = subprocess.run(
            ['pgrep', '-P', str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0:
        return []
    return [int(s) for s in out.stdout.split() if s.isdigit()]


def process_tree(pid: int) -> list[int]:
    """Depth-first list of pid + all descendants (children before parent, so
    a SIGKILL sweep takes leaves first)."""
    out: list[int] = []
    for child in child_pids(pid):
        out.extend(process_tree(child))
    out.append(pid)
    return out


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def kill_process_tree(pid: int, *, grace: int = SIGTERM_GRACE_SECONDS,
                      sleep_fn: Callable[[float], None] = time.sleep) -> bool:
    """SIGTERM the whole tree, give it `grace` seconds, then SIGKILL any
    survivor. Returns True if the root PID is gone afterward."""
    tree = process_tree(pid)
    for p in tree:
        _signal(p, signal.SIGTERM)
    sleep_fn(grace)
    for p in tree:
        _signal(p, signal.SIGKILL)
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _canonical_repo_for_worktree(worktree: str) -> Optional[Path]:
    """The main checkout backing this linked worktree, via
    `git rev-parse --path-format=absolute --git-common-dir` (its parent is
    the canonical repo root)."""
    try:
        out = subprocess.run(
            ['git', '-c', f'safe.directory={worktree}', '-C', worktree,
             'rev-parse', '--path-format=absolute', '--git-common-dir'],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    common_dir = Path(out.stdout.strip())
    # common_dir is <canonical>/.git ; its parent is the repo root.
    return common_dir.parent if common_dir.name == '.git' else common_dir


def _repo_on_main(repo: Path) -> bool:
    try:
        out = subprocess.run(
            ['git', '-C', str(repo), 'branch', '--show-current'],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return out.returncode == 0 and out.stdout.strip() == 'main'


def _worktree_has_uncommitted_changes(worktree: str) -> Optional[bool]:
    """True iff the worktree has uncommitted changes (`git status --porcelain`
    non-empty). None if the status can't be read (don't guess — the caller
    treats unknown as 'do not force-destroy'). Committed-but-unpushed work is
    NOT flagged: `git worktree remove` leaves the branch ref (and its commits)
    intact in the repo; only uncommitted working-tree edits are lost to
    --force, so that is what we protect."""
    try:
        out = subprocess.run(
            ['git', '-c', f'safe.directory={worktree}', '-C', worktree,
             'status', '--porcelain'],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return bool(out.stdout.strip())


def remove_worktree(worktree: str) -> bool:
    """`git worktree remove --force` + prune, guarded on the canonical repo
    being on `main` AND the worktree having no uncommitted changes. Never
    removes the canonical repo itself, and never `--force`-discards uncommitted
    work. Returns True on success / nothing-to-do; False (skip) otherwise."""
    canonical = _canonical_repo_for_worktree(worktree)
    if canonical is None:
        log(f'worktree-remove skipped: cannot resolve canonical repo for {worktree}',
            'WARN')
        return False
    if str(canonical) == worktree:
        log(f'worktree-remove skipped: {worktree} IS the canonical repo', 'WARN')
        return False
    if not _repo_on_main(canonical):
        log(f'worktree-remove skipped: canonical repo {canonical} not on main',
            'WARN')
        return False
    # Data-loss guard: `git worktree remove --force` discards uncommitted
    # changes. A Case-2 (no-marker) reap means the work was never proven pushed,
    # so refuse to force-destroy a dirty worktree — preserve it for inspection
    # and let the process kill alone free the fleet slot. (The previous guard
    # only checked the canonical repo's branch, which is always 'main' and tells
    # us nothing about THIS worktree's state.)
    dirty = _worktree_has_uncommitted_changes(worktree)
    if dirty is not False:  # True (dirty) or None (unknown) → do not force
        log(f'worktree-remove skipped: {worktree} has uncommitted changes '
            f'(dirty={dirty!r}); preserving to avoid --force data loss', 'WARN')
        return False
    try:
        subprocess.run(
            ['git', '-C', str(canonical), 'worktree', 'remove', '--force', worktree],
            capture_output=True, text=True, check=True, timeout=GIT_TIMEOUT_SEC,
        )
        subprocess.run(
            ['git', '-C', str(canonical), 'worktree', 'prune'],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC,
        )
        log(f'removed worktree {worktree}')
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log(f'worktree remove failed for {worktree}: {str(e)[:200]}', 'WARN')
        return False


# ==================== forward-progress (commit) probe ====================

def _worktree_head_commit_age_secs(
    worktree: str, *, now: Optional[float] = None,
) -> Optional[float]:
    """Seconds since the worktree HEAD's last commit (committer timestamp), or
    None if it can't be read.

    Read-only `git log -1 --format=%ct` against the worktree. None on ANY git
    error (no commits yet, not a worktree, timeout, bad output). The caller
    treats None as 'progress unknown' and therefore NEVER declares a session
    wedged on the commit signal alone — a fail-safe toward sparing, matching
    this module's discipline that a probe failure must make us LESS aggressive,
    never more."""
    try:
        out = subprocess.run(
            ['git', '-c', f'safe.directory={worktree}', '-C', worktree,
             'log', '-1', '--format=%ct'],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        commit_epoch = int(out.stdout.strip())
    except ValueError:
        return None
    now = now if now is not None else time.time()
    return max(0.0, now - commit_epoch)


def _session_progress_stale(cand: Candidate, cfg: dict[str, Any]) -> bool:
    """True iff the session has made NO forward progress — neither a new commit
    in its worktree NOR new session-log activity — for longer than
    wedge_progress_grace_seconds.

    The two progress signals are ANDed deliberately (the 2026-06-24
    forge-post-open-mergeable-rebase-001 incident definition: "no new git commit
    AND no new session output file mtime"). Requiring BOTH stale is what keeps
    it conservative: a live build inside a long foreground operation (e.g. the
    ~10-min regression gate) writes no commit but keeps its JSONL warm between
    tool calls, so idle_secs stays small and it is never falsely declared
    wedged; a session frozen on BOTH for 25 min is past anything legitimate.

    commit_age_secs is None (unreadable / not applicable — mirror tier, deleted
    cwd) ⇒ progress is UNKNOWN ⇒ NOT declared stale (fail-safe toward sparing),
    so this can only ever ADD reaps the existing decisive signals already miss,
    never remove a spare the old logic granted on a readable-but-fresh repo."""
    if cand.commit_age_secs is None:
        return False
    grace = cfg['wedge_progress_grace_seconds']
    return cand.idle_secs > grace and cand.commit_age_secs > grace


# ==================== forge-PR reap guard ====================

def _open_pr_created_at(branch: str) -> Optional[str]:
    """The createdAt (ISO string) of the OPEN PR whose head is `branch`, or None
    if there is no open PR for it. Read-only. Returns None on ANY gh failure
    (fail-safe toward normal reaping — the heal_undispatched_pr_review healer is
    the durable net that recovers a review even if a reap proceeds)."""
    try:
        out = subprocess.run(
            ['gh', 'pr', 'list', '--repo', REPO, '--head', branch,
             '--state', 'open', '--json', 'number,createdAt'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh pr list (open PR check for {branch}) failed: '
            f'{type(e).__name__}: {e}; assuming no open PR', 'WARN')
        return None
    if out.returncode != 0:
        log(f'gh pr list (open PR check for {branch}) returned '
            f'{out.returncode}: {out.stderr.strip()[:200]}; assuming no open PR',
            'WARN')
        return None
    try:
        rows = json.loads(out.stdout or '[]')
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(rows, list) or not rows:
        return None
    created = rows[0].get('createdAt') if isinstance(rows[0], dict) else None
    return created if isinstance(created, str) and created else None


def _pr_age_hours(created_at: str, now: Optional[datetime] = None) -> Optional[float]:
    """Hours since `created_at` (ISO-8601, tolerating a trailing Z). None if
    unparseable."""
    try:
        dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def _review_already_dispatched_for_task(task_id: str) -> bool:
    """True iff a Mirror review task for `task_id` already exists (inbox / archive
    / .invalid), reusing the EXACT predicate the inline dispatch + outbox
    reconcile + heal_undispatched_pr_review use, so the three can't drift. On any
    import failure returns False (treat as not-dispatched) — combined with an open
    PR that makes the guard protect the session, the safe direction."""
    try:
        import outbox_notifier as notifier
        import safe_write_inbox
    except Exception as e:  # noqa: BLE001
        log(f'could not import dispatch deps for review-presence check: '
            f'{type(e).__name__}: {e}; treating task {task_id} as undispatched',
            'WARN')
        return False
    try:
        fname = safe_write_inbox.canonical_inbox_name(f'review-{task_id}.json')
        return bool(notifier._review_request_already_dispatched(fname))
    except Exception as e:  # noqa: BLE001
        log(f'review-presence check raised for task {task_id}: '
            f'{type(e).__name__}: {e}; treating as undispatched', 'WARN')
        return False


def _forge_task_id_for_cwd(cwd: str) -> Optional[str]:
    """The task_id embedded in a Forge worktree cwd (`wt-forge-<task_id>`), or
    None for a non-Forge / non-conventional cwd. A trailing ` (deleted)` (the
    kernel's marker for a torn-down worktree still held open by a wedged process)
    is stripped first, so inbox / PR-state lookups resolve the real task_id even
    for a dead session."""
    name = Path(cwd).name
    if name.endswith(_DELETED_CWD_SUFFIX):
        name = name[: -len(_DELETED_CWD_SUFFIX)].rstrip()
    if not name.startswith(FORGE_WORKTREE_NAME_PREFIX):
        return None
    task_id = name[len(FORGE_WORKTREE_NAME_PREFIX):]
    return task_id or None


def _cwd_worktree_deleted(cwd: str) -> bool:
    """True iff this process's cwd resolves to a removed directory. The kernel
    appends ` (deleted)` to /proc/<pid>/cwd when the worktree was torn down out
    from under a still-running process. A live preflight→build handoff has an
    intact worktree; a `(deleted)` cwd proves the session is dead/wedged and its
    inbox build file is a leftover, NOT a pending handoff (decisive release)."""
    return cwd.endswith(_DELETED_CWD_SUFFIX)


def _forge_branch_pr_merged_or_closed(task_id: str) -> bool:
    """True iff the task's branch (`forge/<task_id>`) has a MERGED or CLOSED PR —
    a DECISIVE release: a merged PR means the build's deliverable already landed,
    a closed PR means it was abandoned; either way a leftover build file is no
    longer a live handoff. Read-only gh; returns False on ANY gh failure
    (fail-SAFE toward KEEPING the spare — a transient gh miss must never flip a
    genuine handoff into a reap; the worktree-deleted signal remains the local
    proof of a wedge)."""
    branch = f'forge/{task_id}'
    try:
        out = subprocess.run(
            ['gh', 'pr', 'list', '--repo', REPO, '--head', branch,
             '--state', 'all', '--json', 'number,state'],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh pr list (merged/closed check for {branch}) failed: '
            f'{type(e).__name__}: {e}; assuming not merged/closed', 'WARN')
        return False
    if out.returncode != 0:
        log(f'gh pr list (merged/closed check for {branch}) returned '
            f'{out.returncode}: {out.stderr.strip()[:200]}; assuming not '
            f'merged/closed', 'WARN')
        return False
    try:
        rows = json.loads(out.stdout or '[]')
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(rows, list):
        return False
    for row in rows:
        if (isinstance(row, dict)
                and str(row.get('state', '')).upper() in ('MERGED', 'CLOSED')):
            return True
    return False


def _forge_handoff_is_live(cand: Candidate, task_id: str) -> bool:
    """Distinguish a GENUINE preflight→build handoff (keep sparing) from a wedged
    session whose queued build file is a stale leftover (release the guard).

    A queued `build-<task_id>.json` in Forge's inbox alone is NOT proof of a live
    handoff — the 2026-06-11 pulse-park-and-silence deadlock: the build had
    finished + merged and the worktree was torn down, yet the leftover inbox file
    made the guard spare the wedged worker for ~1h40m (self-sustaining: the file
    isn't archived because the wedged owner never cleanly exits, and the worker
    isn't reaped because the file is present). The handoff is live ONLY when
    NEITHER of these decisive wedge signals holds, checked cheapest-first:

      DECISIVE — the worktree is deleted (cwd is a `(deleted)` path): the session
          is dead, so the queued file is a leftover, not a pending handoff.
      DECISIVE — the branch's PR is already merged/closed: the build's work
          already landed (or was abandoned), so the queued file is stale.

    Age is deliberately NOT a signal here: a genuinely queued dispatch can sit
    past any window while the Forge slot is busy, so releasing on age alone would
    re-create the ccd-s1 active-build reap that #441 fixed.

    (A third signal — in-flight-registry OWNERSHIP — was removed in the descope
    of PR #457: it globbed the registry for `build-*.json`, but the runner files
    every in-flight worker under the BARE `<task_stem>.json`
    (agent_runner._register_in_flight), so the glob never matched and ownership
    was always False. Re-deriving ownership from the bare stem would reintroduce
    the #441 active-build reap, since preflight and build register under the same
    stem with no phase signal to tell a wedged build worker from a live one — that
    needs a registry phase-signal change tracked as a separate follow-up.)"""
    if _cwd_worktree_deleted(cand.cwd):
        return False
    if _forge_branch_pr_merged_or_closed(task_id):
        return False
    return True


def _forge_inbox_has_queued_build(task_id: str) -> bool:
    """True iff a next-phase BUILD dispatch for `task_id` is QUEUED in Forge's
    LIVE inbox — the preflight→build handoff window. Reaping a session in this
    state removes the worktree the queued build is about to `--resume` into,
    stranding the whole build sequence (the 2026-06-10 ccd-s1 incident).

    We deliberately consult only the LIVE inbox (`~/agents/inboxes/forge/`), not
    `.archive/`/`.invalid/`: a live file means the build is queued and not yet
    consumed (the strongest 'spare me' signal); an archived file means the build
    was already dispatched-and-consumed, so the session is no longer in the
    handoff window. The filename is derived through the SAME
    `canonical_inbox_name` the outbox notifier writes with
    (`_dispatch_build_phase`), so a task_id carrying a path-structural byte can't
    make the lookup miss. Replan iterations (`build-<task_id>-replanN.json`) are
    covered too. Read-only; lazy-imports safe_write_inbox so the supabase-free
    test path is unaffected. Any import/IO failure returns False (the handoff
    grace window remains the baseline cushion, so a transient miss here never
    strands a fresh handoff)."""
    try:
        import safe_write_inbox
    except Exception as e:  # noqa: BLE001
        log(f'could not import safe_write_inbox for queued-build check: '
            f'{type(e).__name__}: {e}; treating task {task_id} as un-queued', 'WARN')
        return False
    try:
        forge_inbox = safe_write_inbox.INBOXES_ROOT / 'forge'
        base = safe_write_inbox.canonical_inbox_name(f'build-{task_id}.json')
        if (forge_inbox / base).exists():
            return True
        replan_prefix = safe_write_inbox.canonical_inbox_name(f'build-{task_id}-replan')
        for p in forge_inbox.glob('build-*-replan*.json'):
            if p.name.startswith(replan_prefix):
                return True
    except OSError as e:
        log(f'queued-build check IO error for task {task_id}: '
            f'{type(e).__name__}: {e}; treating as un-queued', 'WARN')
        return False
    return False


def _forge_branch_has_open_unreviewed_pr(cwd: str) -> bool:
    """True iff this Forge worktree's branch has an OPEN PR for which no Mirror
    review has been dispatched yet — the state in which reaping would orphan the
    PR. Once a review IS dispatched the deliverable is captured and this returns
    False (the session is safe to reap). Non-Forge / non-conventional cwds return
    False (no protection)."""
    task_id = _forge_task_id_for_cwd(cwd)
    if not task_id:
        return False
    branch = f'forge/{task_id}'
    created_at = _open_pr_created_at(branch)
    if created_at is None:
        return False  # no open PR (or gh unknown) → nothing to protect
    # Bound the protection so a session whose review never dispatches can't be
    # held forever (a case1 build session never reaches the hard backstop).
    age_h = _pr_age_hours(created_at)
    if age_h is not None and age_h > GUARD_MAX_PROTECT_HOURS:
        log(f'forge PR on {branch} is {age_h:.1f}h old (> '
            f'{GUARD_MAX_PROTECT_HOURS}h protection cap); allowing reap — '
            f'heal_undispatched_pr_review remains the net for the review', 'INFO')
        return False
    if _review_already_dispatched_for_task(task_id):
        return False  # review already in flight → safe to reap
    return True


def _forge_spare_reason(cand: Candidate, cfg: dict[str, Any]) -> Optional[str]:
    """The reason this Forge session must be SPARED from reaping because its build
    sequence is still legitimately in flight, or None if no spare-condition holds.

    A session is reaped ONLY when it fails ALL of these AND is idle past
    threshold. The conditions, checked cheapest-first so the gh call is the last
    resort:

      (c) build-handoff grace window — the session is idle no longer than
          marker_grace + build_handoff_grace (the build-sequence advancer's
          cadence). Until a full advancer cycle has elapsed past the normal
          marker grace, the advancer hasn't had its chance to dispatch the next
          phase, so a freshly-marker'd session in the preflight→build handoff
          must not be reaped. Pure idle math — protects the window even if the
          probes below can't run.
      (a) a next-phase BUILD dispatch is QUEUED in Forge's live inbox AND the
          handoff is still LIVE (`_forge_handoff_is_live`) — the build is about to
          `--resume` into this worktree; removing it strands the sequence (the
          ccd-s1 incident). The liveness check is what closes the
          pulse-park-and-silence deadlock (-002): a leftover build file whose
          worktree is deleted, or whose PR is merged-or-closed, is a WEDGE, not a
          live handoff — so the guard releases and the normal reap proceeds.
          Deliberately NOT bounded by age: a genuinely queued dispatch can sit
          past any window while the slot is busy, and age-alone release would
          re-create the ccd-s1 active-build reap #441 fixed.
      (b) the branch has an OPEN PR with no Mirror review dispatched yet — reaping
          would orphan the PR/review (the #412 guard).

    Caller invokes this only for tier 'forge'. All reads are local files /
    GitHub-truth — no model call."""
    grace_floor = cfg['marker_grace_seconds'] + cfg['build_handoff_grace_seconds']
    if cand.idle_secs <= grace_floor:
        return (f'within build-handoff grace window (idle {int(cand.idle_secs)}s '
                f'<= marker_grace {cfg["marker_grace_seconds"]}s + advancer cadence '
                f'{cfg["build_handoff_grace_seconds"]}s) — advancer has not yet had '
                f'a full cycle to dispatch the next phase')
    # Progress-staleness release (2026-06-24 forge-post-open-mergeable-rebase-001
    # deadlock-break). A session that has made NO forward progress — no new
    # commit AND no session-log activity — for wedge_progress_grace_seconds is
    # WEDGED and holding the single Forge slot. None of the in-flight
    # protections below can apply in that state: a queued build can't be
    # consumed WHILE this session holds the only slot (the self-sustaining
    # deadlock), and an open PR with no review yet is netted independently by
    # heal_undispatched_pr_review (GitHub-truth). So this release deliberately
    # bypasses BOTH (a) the queued-build spare and (b) the open-unreviewed-PR
    # spare — those guards exist to protect work still IN FLIGHT, and a
    # provably-no-progress session is, by definition, not in flight. Checked
    # before the (a)/(b) probes so a wedge is released without even paying their
    # file/gh cost. Conservative by construction (_session_progress_stale needs
    # BOTH signals stale and a readable commit age), so it only ever adds reaps
    # the decisive worktree-deleted / PR-merged signals already miss.
    if _session_progress_stale(cand, cfg):
        log(f'forge spare RELEASED (progress-stale): {Path(cand.cwd).name} idle '
            f'{int(cand.idle_secs)}s + last commit {int(cand.commit_age_secs)}s ago, '
            f'both > wedge_progress_grace {cfg["wedge_progress_grace_seconds"]}s — '
            f'no forward progress, the session is wedged and holding the build '
            f'slot; allowing reap (worktree preserved for --resume retry)', 'INFO')
        return None
    task_id = _forge_task_id_for_cwd(cand.cwd)
    if task_id and _forge_inbox_has_queued_build(task_id):
        if _forge_handoff_is_live(cand, task_id):
            return (f'queued next-phase build dispatch present in Forge inbox for '
                    f'task {task_id}, handoff still live (worktree intact, '
                    f'PR not merged/closed) — preflight→build handoff in flight')
        log(f'build-handoff guard RELEASED for task {task_id} '
            f'({Path(cand.cwd).name}): queued build file is a stale leftover '
            f'(worktree-deleted or merged/closed PR) — not a live handoff, '
            f'allowing normal reap', 'INFO')
    if _forge_branch_has_open_unreviewed_pr(cand.cwd):
        return ('branch has an OPEN PR with no Mirror review dispatched yet — '
                'reaping would orphan the review (#412 guard)')
    return None


# ==================== notify seams ====================

def _emit_healed_event(*, tier: str, cwd: str, case: str, reason: str) -> None:
    """Best-effort chain_event row recording the reap (event_type
    'healer_fire'). Lazy import keeps supabase out of the import path for
    tests / the no-network case."""
    try:
        import chain_event_emit
        chain_event_emit.emit_event(
            event_type='healer_fire',
            agent='heal-wedged-review-sessions',
            task_id=Path(cwd).name,
            payload={'tier': tier, 'cwd': cwd, 'case': case, 'reason': reason},
        )
    except Exception as e:  # noqa: BLE001 — telemetry must never crash the reap
        log(f'chain_event emit failed: {type(e).__name__}: {e}', 'WARN')


def _closure_notify(message: str, subject: str) -> None:
    """One-line self-healed confirmation via the actionable-alert channel
    (route='closure' — a confirmation DM, not an escalation)."""
    try:
        import larry_alerts
        larry_alerts.append_alert(
            source='heal-wedged-review-sessions',
            severity='warning',
            message=message,
            subject=subject,
            route='closure',
        )
    except Exception as e:  # noqa: BLE001
        log(f'closure notify failed: {type(e).__name__}: {e}', 'WARN')


def _escalate_notify(message: str, subject: str, suggested_action: str) -> None:
    """Escalation DM for a Case-2 silent-wedge candidate (alert-only mode)."""
    try:
        import larry_alerts
        larry_alerts.append_alert(
            source='heal-wedged-review-sessions',
            severity='warning',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
            route='escalate',
        )
    except Exception as e:  # noqa: BLE001
        log(f'escalate notify failed: {type(e).__name__}: {e}', 'WARN')


# ==================== orchestration ====================

@dataclass
class Candidate:
    pid: int
    cwd: str
    tier: str
    jsonl: Optional[Path]
    session_id: Optional[str]
    idle_secs: float
    marker_present: bool
    # Seconds since the worktree HEAD's last commit (committer timestamp), or
    # None when it can't be read / isn't applicable (mirror tier, deleted cwd).
    # The second forward-progress signal alongside idle_secs (JSONL mtime): a
    # genuinely live build is committing or writing its transcript; a session
    # silent on BOTH past wedge_progress_grace is provably wedged.
    commit_age_secs: Optional[float] = None


def scan_candidates(now: Optional[float] = None) -> list[Candidate]:
    """Discover live review-tier sessions and gather their state. Pure-ish:
    only reads /proc, the project dirs, and JSONL mtimes."""
    now = now if now is not None else time.time()
    self_pid = os.getpid()
    out: list[Candidate] = []
    for pid in claude_pids():
        if pid == self_pid:
            continue
        cwd = proc_cwd(pid)
        if cwd is None:
            continue
        tier = agent_tier_for_cwd(cwd)
        if tier is None:
            continue
        jsonl = session_jsonl_for_cwd(cwd)
        if jsonl is None:
            idle_secs = 0.0
            session_id = None
            marker_present = False
        else:
            try:
                idle_secs = now - jsonl.stat().st_mtime
            except OSError:
                idle_secs = 0.0
            session_id = jsonl.stem
            marker_present = jsonl_has_terminal_marker(jsonl, tier)
        # Second forward-progress signal (forge tier only): how long since the
        # worktree's last commit. Skipped for mirror (no slot-holding build to
        # rescue) and for a torn-down ('(deleted)') cwd, where there's no live
        # worktree to query — the worktree-deleted signal already releases those.
        commit_age_secs: Optional[float] = None
        if tier == 'forge' and not _cwd_worktree_deleted(cwd):
            commit_age_secs = _worktree_head_commit_age_secs(cwd, now=now)
        out.append(Candidate(
            pid=pid, cwd=cwd, tier=tier, jsonl=jsonl, session_id=session_id,
            idle_secs=idle_secs, marker_present=marker_present,
            commit_age_secs=commit_age_secs,
        ))
    return out


def _live_cwds(candidates: list[Candidate]) -> set[str]:
    return {c.cwd for c in candidates}


def verify_pending(
    state: ConfidenceState,
    candidates: list[Candidate],
    cfg: dict[str, Any],
    *,
    now_iso: str,
) -> tuple[int, int]:
    """Resolve outstanding Case-2 alerts against current reality.

    For each pending session: a marker now present OR the JSONL having
    advanced past its alert-time mtime ⇒ FALSE positive (it was live work).
    Otherwise, if no live process holds that cwd anymore ⇒ TRUE positive
    (it stayed dead). Still-alive-and-idle ⇒ leave pending.

    Returns (true_positives, false_positives) recorded this sweep.
    """
    live_cwds = _live_cwds(candidates)
    tp = fp = 0
    for session_id in list(state.pending.keys()):
        entry = state.pending[session_id]
        cwd = entry.get('cwd', '')
        tier = entry.get('tier', '')
        jsonl_path = Path(entry['jsonl_path']) if entry.get('jsonl_path') else None
        baseline_mtime = entry.get('jsonl_mtime_at_alert')

        marker_now = bool(jsonl_path and jsonl_path.exists()
                          and jsonl_has_terminal_marker(jsonl_path, tier))
        resumed = False
        if jsonl_path and jsonl_path.exists() and isinstance(baseline_mtime, (int, float)):
            try:
                resumed = jsonl_path.stat().st_mtime > baseline_mtime + 1.0
            except OSError:
                resumed = False

        if marker_now or resumed:
            state.executions.append({
                'outcome': FALSE_POSITIVE, 'session_id': session_id,
                'cwd': cwd, 'ts': now_iso,
                'note': 'marker-emitted' if marker_now else 'resumed-activity',
            })
            del state.pending[session_id]
            fp += 1
            log(f'CASE2 verify: {session_id} FALSE positive '
                f'({"marker" if marker_now else "resumed"}) — streak reset', 'INFO')
        elif cwd not in live_cwds:
            state.executions.append({
                'outcome': TRUE_POSITIVE, 'session_id': session_id,
                'cwd': cwd, 'ts': now_iso,
            })
            del state.pending[session_id]
            tp += 1
            log(f'CASE2 verify: {session_id} TRUE positive (stayed dead)', 'INFO')
        # else: still alive + idle + no marker → remains pending.
    return tp, fp


def _resumed_since_scan(cand: Candidate, *, now: Optional[float] = None) -> bool:
    """True iff the session's JSONL has advanced (still no marker) since the
    scan — i.e. the process resumed real work and an auto-reap would be a
    miss. A marker appearing is NOT a resume: that's a completed session and
    reaping it is still correct. Unreadable/unknown → False (don't block the
    reap on a transient stat error)."""
    if cand.jsonl is None:
        return False
    try:
        fresh_idle = (now if now is not None else time.time()) - cand.jsonl.stat().st_mtime
    except OSError:
        return False
    if jsonl_has_terminal_marker(cand.jsonl, cand.tier):
        return False
    # mtime advanced enough that idle fell well below the original scan idle.
    return fresh_idle < cand.idle_secs - 1.0


def reap(cand: Candidate, *, case: str, reason: str,
         remove_worktree_after_kill: bool = True,
         reaper: Callable[[int], bool] = kill_process_tree,
         worktree_remover: Callable[[str], bool] = remove_worktree,
         closure_notify: Callable[[str, str], None] = _closure_notify,
         event_emitter: Callable[..., None] = _emit_healed_event,
         verify_pid_fn: Callable[[int], Optional[str]] = proc_cwd) -> bool:
    """Kill the session tree, optionally remove its worktree (guarded), and notify.

    Returns True iff the kill was actually issued. Before signalling, re-verify
    the PID still belongs to the same review worktree via its /proc/<pid>/cwd —
    the SAME identity signal the scan used. Between scan and now the wedged
    process can exit and the OS recycle its PID; this recheck shrinks (does not
    fully close — a microsecond TOCTOU vs the actual signal remains) the window
    in which we'd SIGKILL an unrelated process that inherited the number. A cwd
    mismatch (incl. the process having exited → cwd None) aborts the WHOLE reap,
    including worktree removal: we don't tear down a worktree whose process we
    couldn't confirm we own. A genuinely-orphaned worktree from a clean exit is
    then swept by the hourly cleanup_stale_worktrees GC instead of here.

    ``remove_worktree_after_kill=False`` kills the PID to free the slot but LEAVES
    the worktree in place. Used for the progress-stale Forge wedge: the
    build-<task>.json is still queued in the inbox, so once the slot frees the
    watcher's in-process ``--resume`` retry (agent_runner.run_claude, returncode!=0
    branch) re-enters THIS worktree and finishes the task cleanly — exactly the
    proven manual recovery. If no retry materializes the worktree is a benign
    orphan swept by the hourly cleanup_stale_worktrees GC."""
    current_cwd = verify_pid_fn(cand.pid)
    if current_cwd != cand.cwd:
        log(f'reap ABORTED pid={cand.pid} tier={cand.tier} case={case}: pid cwd '
            f'{current_cwd!r} != expected {cand.cwd!r} (process exited or PID '
            f'reused since scan); not killing', 'WARN')
        return False
    killed = reaper(cand.pid)
    removed = worktree_remover(cand.cwd) if remove_worktree_after_kill else False
    wt_note = (f'Worktree removed: {removed}.' if remove_worktree_after_kill
               else 'Worktree left intact so the watcher can --resume and finish '
                    'the task; the GC sweeps it if no retry runs.')
    log(f'HEALED pid={cand.pid} tier={cand.tier} case={case} '
        f'killed={killed} worktree_removed={removed} '
        f'remove_worktree={remove_worktree_after_kill} reason={reason} cwd={cand.cwd}')
    event_emitter(tier=cand.tier, cwd=cand.cwd, case=case, reason=reason)
    closure_notify(
        (f'Reaped wedged {cand.tier} review session (pid {cand.pid}) — '
         f'{reason}. {wt_note}'),
        f'wedged-review-reaped:{Path(cand.cwd).name}',
    )
    return True


def run_cycle(
    *,
    candidates: Optional[list[Candidate]] = None,
    state: Optional[ConfidenceState] = None,
    cfg: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    reaper: Callable[[int], bool] = kill_process_tree,
    worktree_remover: Callable[[str], bool] = remove_worktree,
    closure_notify: Callable[[str, str], None] = _closure_notify,
    escalate_notify: Callable[[str, str, str], None] = _escalate_notify,
    event_emitter: Callable[..., None] = _emit_healed_event,
    verify_pid_fn: Callable[[int], Optional[str]] = proc_cwd,
    persist: bool = True,
) -> dict[str, Any]:
    """One sweep. All IO seams are injectable so the whole flow is unit-
    testable without /proc, signals, git, or the network.

    Order matters: verify outstanding Case-2 alerts FIRST (so a graduation
    that crosses the threshold this sweep takes effect for the new Case-2
    candidates below), then classify + act on the live candidates.
    """
    cfg = cfg or load_config()
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    candidates = scan_candidates(now=now.timestamp()) if candidates is None else candidates
    state = load_state() if state is None else state

    summary: dict[str, Any] = {
        'scanned': len(candidates), 'case1_reaped': 0, 'case2_alerted': 0,
        'case2_auto_reaped': 0, 'case2_hard_reaped': 0, 'case2_demoted_at_gate': 0,
        'true_positives': 0, 'false_positives': 0, 'forge_spared': 0,
        'worktree_preserved': 0, 'mode_before': state.mode,
    }

    # 1) Resolve prior Case-2 alerts → update streak.
    tp, fp = verify_pending(state, candidates, cfg, now_iso=now_iso)
    summary['true_positives'] = tp
    summary['false_positives'] = fp

    prior_mode = state.mode
    new_mode = mode_for_streak(
        state.streak, threshold=cfg['streak_to_promote'], current_mode=state.mode)
    state.mode = new_mode
    if new_mode == MODE_AUTO_REAP and prior_mode != MODE_AUTO_REAP:
        log(f'GRADUATED: Case 2 → auto-reap (streak={state.streak} >= '
            f'{cfg["streak_to_promote"]})', 'INFO')
        closure_notify(
            f'Wedged-review reaper graduated Case 2 (silent sessions) to '
            f'auto-reap after {state.streak} consecutive correct alerts. '
            f'Silent wedged review sessions will now be reaped automatically.',
            'wedged-review-case2-graduated',
        )
    elif new_mode == MODE_ALERT_ONLY and prior_mode == MODE_AUTO_REAP:
        log('DEMOTED: Case 2 → alert-only (false positive reset the streak)', 'WARN')

    # 2) Classify + act on the current live candidates.
    for cand in candidates:
        if cand.jsonl is None:
            continue  # no activity log → cannot assess
        verdict = classify(
            marker_present=cand.marker_present, idle_secs=cand.idle_secs, cfg=cfg)
        # Forge build-sequence spare guard: never destroy (or alert on) a Forge
        # session whose build is still legitimately in flight — a queued
        # next-phase build dispatch (preflight→build handoff), an open PR awaiting
        # its Mirror review (#412), or a session still inside the advancer-cadence
        # handoff grace window. Reaping any of these strands the build sequence
        # (the 2026-06-10 ccd-s1 false-positive). The guard self-resolves: each
        # condition clears as the sequence advances (dispatch consumed, review
        # dispatched, grace elapsed), and all are bounded so a genuinely-wedged
        # session is still reaped. Only incurred when a reap/alert would actually
        # fire, so the gh cost is ~never paid in steady state.
        if verdict in (REAP_CASE1, REAP_CASE2_HARD, SILENT_CASE2) and cand.tier == 'forge':
            spare_reason = _forge_spare_reason(cand, cfg)
            if spare_reason:
                log(f'reap/alert skipped for {cand.session_id} '
                    f'({Path(cand.cwd).name}): {spare_reason}', 'INFO')
                summary['forge_spared'] += 1
                continue
        # A Forge wedge released by progress-staleness still has its
        # build-<task>.json queued in the inbox; PRESERVE the worktree on the
        # kill so the watcher's in-process --resume retry can finish the task.
        # (Mirror tier / unreadable commit age → False → remove as before.)
        keep_worktree = cand.tier == 'forge' and _session_progress_stale(cand, cfg)
        if verdict == REAP_CASE1:
            if reap(cand, case='case1',
                    reason=(f'terminal marker present, idle {int(cand.idle_secs)}s '
                            f'> grace {cfg["marker_grace_seconds"]}s'),
                    remove_worktree_after_kill=not keep_worktree,
                    reaper=reaper, worktree_remover=worktree_remover,
                    closure_notify=closure_notify, event_emitter=event_emitter,
                    verify_pid_fn=verify_pid_fn):
                summary['case1_reaped'] += 1
                if keep_worktree:
                    summary['worktree_preserved'] += 1
        elif verdict == REAP_CASE2_HARD:
            # Deterministic backstop: silent far longer than any legitimate
            # review operation could run, so reap regardless of the confidence
            # ladder/mode — this is the provably-wedged fast path that lets the
            # healer act on the FIRST wedge instead of waiting for the streak to
            # graduate. Same fresh resumed-at-gate recheck as the graduated path:
            # if the session resumed between scan and now it was live work, so
            # abort the kill (a benign skip — not a streak-affecting outcome).
            if _resumed_since_scan(cand):
                log(f'CASE2-HARD reap aborted: {cand.session_id} resumed at gate '
                    f'— live work, not killing', 'WARN')
                continue
            if reap(cand, case='case2-hard',
                    reason=(f'no marker, idle {int(cand.idle_secs)}s > HARD silent grace '
                            f'{cfg["hard_silent_grace_seconds"]}s (deterministic wedge '
                            f'backstop — provably past any legitimate review operation)'),
                    remove_worktree_after_kill=not keep_worktree,
                    reaper=reaper, worktree_remover=worktree_remover,
                    closure_notify=closure_notify, event_emitter=event_emitter,
                    verify_pid_fn=verify_pid_fn):
                summary['case2_hard_reaped'] += 1
                if keep_worktree:
                    summary['worktree_preserved'] += 1
        elif verdict == SILENT_CASE2:
            if state.mode == MODE_AUTO_REAP:
                # Final fresh recheck right before an irreversible kill: if the
                # session emitted a marker or resumed activity since the scan,
                # it was live work — a MISS. Record a false positive (which
                # auto-demotes Case 2 back to alert-only) instead of killing.
                if _resumed_since_scan(cand):
                    state.executions.append({
                        'outcome': FALSE_POSITIVE, 'session_id': cand.session_id,
                        'cwd': cand.cwd, 'ts': now_iso, 'note': 'resumed-at-gate',
                    })
                    state.mode = MODE_ALERT_ONLY
                    summary['case2_demoted_at_gate'] += 1
                    log(f'CASE2 auto-reap aborted: {cand.session_id} resumed at '
                        f'gate — FALSE positive, demoting to alert-only', 'WARN')
                    continue
                if reap(cand, case='case2-auto',
                        reason=(f'no marker, idle {int(cand.idle_secs)}s > silent grace '
                                f'{cfg["silent_grace_seconds"]}s (auto-reap, graduated)'),
                        remove_worktree_after_kill=not keep_worktree,
                        reaper=reaper, worktree_remover=worktree_remover,
                        closure_notify=closure_notify, event_emitter=event_emitter,
                        verify_pid_fn=verify_pid_fn):
                    summary['case2_auto_reaped'] += 1
                    if keep_worktree:
                        summary['worktree_preserved'] += 1
            else:
                # Alert-only: escalate + record pending for later verification.
                if cand.session_id and cand.session_id not in state.pending:
                    try:
                        baseline = cand.jsonl.stat().st_mtime
                    except OSError:
                        baseline = now.timestamp() - cand.idle_secs
                    state.pending[cand.session_id] = {
                        'first_alert_ts': now_iso,
                        'jsonl_path': str(cand.jsonl),
                        'jsonl_mtime_at_alert': baseline,
                        'cwd': cand.cwd,
                        'tier': cand.tier,
                    }
                    escalate_notify(
                        (f'Possible wedged {cand.tier} review session (pid '
                         f'{cand.pid}, {Path(cand.cwd).name}): idle '
                         f'{int(cand.idle_secs)}s with no terminal marker. '
                         f'Alert-only (Case 2 not yet graduated). Not killing.'),
                        f'wedged-review-silent:{Path(cand.cwd).name}',
                        (f'Inspect: `ls -la {cand.cwd}` and the session log at '
                         f'{cand.jsonl}. If genuinely wedged, kill pid {cand.pid}.'),
                    )
                    summary['case2_alerted'] += 1

    summary['mode_after'] = state.mode
    summary['streak'] = state.streak
    if persist:
        save_state(state)
    return summary


def main() -> int:
    if kill_switch_active():
        log('kill switch present (healers.disabled) — exiting', 'INFO')
        return 0
    heartbeat()
    cfg = load_config()
    if not cfg.get('enabled', True):
        log('disabled via config (enabled=false) — exiting', 'INFO')
        return 0
    summary = run_cycle(cfg=cfg)
    log('HEARTBEAT scanned={scanned} case1_reaped={case1_reaped} '
        'case2_alerted={case2_alerted} case2_auto_reaped={case2_auto_reaped} '
        'case2_hard_reaped={case2_hard_reaped} forge_spared={forge_spared} '
        'worktree_preserved={worktree_preserved} '
        'tp={true_positives} fp={false_positives} mode={mode_after} '
        'streak={streak}'.format(**summary))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
