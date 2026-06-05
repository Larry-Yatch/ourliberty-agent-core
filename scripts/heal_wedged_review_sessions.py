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
hard_silent_grace_seconds, streak_to_promote, enabled. Missing/malformed →
conservative built-in defaults (never raises). Pulse adjusts the JSON; no
constant is hand-picked in this file.

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
}

# Sentinel the hard backstop is pushed to when config is mistuned (hard <=
# silent): an idle window no real session can ever reach (~31,000 years), so
# the hard path is effectively disabled and only the conservative alert ladder
# runs. Far larger than any plausible idle_secs.
_HARD_BACKSTOP_DISABLED = 10 ** 12

# Tree-kill: how long to wait after SIGTERM before escalating to SIGKILL.
SIGTERM_GRACE_SECONDS = 5

GIT_TIMEOUT_SEC = 60

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
                'hard_silent_grace_seconds', 'streak_to_promote'):
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
    cwd: str, projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> Optional[Path]:
    """The most-recently-modified *.jsonl in the cwd's project-slug dir, or
    None if the dir is absent/empty. The newest JSONL is the session the
    live process is writing to."""
    slug_dir = projects_dir / cwd_to_slug(cwd)
    try:
        candidates = [p for p in slug_dir.glob('*.jsonl') if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def terminal_markers_for_tier(tier: str) -> tuple[str, ...]:
    """Canonical terminal-marker substrings whose presence in a session JSONL
    proves the agent's work is in hand for that tier.

    Reuses the handler modules' MARKER_KEYWORDS rather than re-listing the
    grammar here, so a marker rename in either handler propagates
    automatically. Forge additionally has the build/revision exit preambles.
    """
    if tier == 'mirror':
        return tuple(
            f'=== {kw} ===' for kw in mirror_review_handler.MARKER_KEYWORDS.values()
        )
    if tier == 'forge':
        preflight = tuple(
            f'=== {kw} ===' for kw in forge_preflight_handler.MARKER_KEYWORDS.values()
        )
        # Build/revision phases emit no marker block; their terminal signal is
        # a first-line preamble (agents/forge/CLAUDE.md "Post-marker exit
        # discipline"). These are the literal prefixes Forge writes.
        preambles = ('PR opened:', 'PR updated:', 'Revision ')
        return preflight + preambles
    return ()


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
    markers = terminal_markers_for_tier(tier)
    if not markers:
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
                if text and any(m in text for m in markers):
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


def remove_worktree(worktree: str) -> bool:
    """`git worktree remove --force` + prune, guarded on the canonical repo
    being on `main`. Never removes the canonical repo itself or a worktree
    whose own branch is `main`. Returns True on success / nothing-to-do."""
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
        out.append(Candidate(
            pid=pid, cwd=cwd, tier=tier, jsonl=jsonl, session_id=session_id,
            idle_secs=idle_secs, marker_present=marker_present,
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
         reaper: Callable[[int], bool] = kill_process_tree,
         worktree_remover: Callable[[str], bool] = remove_worktree,
         closure_notify: Callable[[str, str], None] = _closure_notify,
         event_emitter: Callable[..., None] = _emit_healed_event) -> None:
    """Kill the session tree, remove its worktree (guarded), and notify."""
    killed = reaper(cand.pid)
    removed = worktree_remover(cand.cwd)
    log(f'HEALED pid={cand.pid} tier={cand.tier} case={case} '
        f'killed={killed} worktree_removed={removed} reason={reason} cwd={cand.cwd}')
    event_emitter(tier=cand.tier, cwd=cand.cwd, case=case, reason=reason)
    closure_notify(
        (f'Reaped wedged {cand.tier} review session (pid {cand.pid}) — '
         f'{reason}. Worktree removed: {removed}.'),
        f'wedged-review-reaped:{Path(cand.cwd).name}',
    )


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
        'true_positives': 0, 'false_positives': 0,
        'mode_before': state.mode,
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
        if verdict == REAP_CASE1:
            reap(cand, case='case1',
                 reason=(f'terminal marker present, idle {int(cand.idle_secs)}s '
                         f'> grace {cfg["marker_grace_seconds"]}s'),
                 reaper=reaper, worktree_remover=worktree_remover,
                 closure_notify=closure_notify, event_emitter=event_emitter)
            summary['case1_reaped'] += 1
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
            reap(cand, case='case2-hard',
                 reason=(f'no marker, idle {int(cand.idle_secs)}s > HARD silent grace '
                         f'{cfg["hard_silent_grace_seconds"]}s (deterministic wedge '
                         f'backstop — provably past any legitimate review operation)'),
                 reaper=reaper, worktree_remover=worktree_remover,
                 closure_notify=closure_notify, event_emitter=event_emitter)
            summary['case2_hard_reaped'] += 1
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
                reap(cand, case='case2-auto',
                     reason=(f'no marker, idle {int(cand.idle_secs)}s > silent grace '
                             f'{cfg["silent_grace_seconds"]}s (auto-reap, graduated)'),
                     reaper=reaper, worktree_remover=worktree_remover,
                     closure_notify=closure_notify, event_emitter=event_emitter)
                summary['case2_auto_reaped'] += 1
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
        'case2_hard_reaped={case2_hard_reaped} '
        'tp={true_positives} fp={false_positives} mode={mode_after} '
        'streak={streak}'.format(**summary))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
