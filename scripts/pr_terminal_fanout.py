#!/usr/bin/env python3
"""pr_terminal_fanout.py — terminal-event fan-out sentinel (completeness PR-3).

Spec: agents/beacon/specs/completeness-pr3-fanout-sentinel.md (v2, BUILD-READY).

WHY
---
Several needs-Larry / in-review surfaces are closed by their OWN healers when a
PR reaches a terminal state. But an out-of-band terminal (a desktop merge, the
agent team's auto-merge, a human on github.com) can land AFTER the surface was
authored and BEFORE any of those per-surface healers observe it — and each
healer only reconciles the ONE surface it owns. This sentinel does a single
probe pass over the genuinely PR-backed surfaces (in-review lane + needs-Larry
records + pr_url-stamped mission task_ids), and on IDENTITY-GRADE terminal
evidence (guarded by the five false-terminal guards in § 3) fans a single close
out across the surfaces that share the item's decision coordinate.

CONSERVATIVE POSTURE (non-negotiable, § 1)
------------------------------------------
UNKNOWN / ambiguous ⇒ KEEP. Every guard must pass before any fan-out write. The
sentinel never touches missions.json / projects.json / captures.json /
build-sequences (single-committer discipline, § 4/§ 10.5): mission *closes* stay
owned by heal_missions_card_gc.

ROLLOUT (§ 8)
-------------
Report-only is the DEFAULT. For the first 48h / >= 4 passes the sentinel writes
a would-close artifact and NO live-tree fan-out. On the first pass where the
window is complete it SELF-EMITS exactly one durable arming approval to Larry
(gated by a durable ``arming_requested`` flag so re-runs never re-emit). Approve
flips the durable ``armed`` flag; until ``armed`` is true every pass stays
artifact-only.

Stdlib + sibling modules only; the heavy dashboard_api import is lazy so the
pure decision functions load (and unit-test) without fastapi/uvicorn.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Repo scripts dir on sys.path so sibling imports resolve under systemd / discover.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import chain_event_emit as cee  # noqa: E402
import task_terminal_state as tts  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402
from decision_identity import canonical_decision_key  # noqa: E402

# -------------------- paths + tunables --------------------

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
STATE_DIR = AGENTS_ROOT / 'state'
BLACKBOARD = AGENTS_ROOT / 'blackboard'
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
EMERGENCY_HALT = BLACKBOARD / 'EMERGENCY_HALT'

HEARTBEAT_FILE = BLACKBOARD / 'pr-terminal-fanout.heartbeat'
ARTIFACT_DIR = BLACKBOARD / 'pr-terminal-fanout'
LAST_PASS_FILE = ARTIFACT_DIR / 'last-pass.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pr-terminal-fanout.log'

UNKNOWN_LEDGER_FILE = STATE_DIR / 'pr-fanout-unknown-ledger.json'
TERMINAL_CACHE_FILE = STATE_DIR / 'pr-fanout-terminal-cache.json'
ARM_STATE_FILE = STATE_DIR / 'pr-fanout-arm.json'

# The emitting source string for the self-watch death alarm. Must match the
# never_silence entry in config/alert-translations.json exactly (§ 6).
ALERT_SOURCE = 'pr-terminal-fanout'

# forge is the sole in-review BUILDER; the closing event must land in forge's row
# set to be seen as a terminal closer (mirrors heal_stale_in_review_reconcile).
BUILDER_AGENT = 'forge'
CLOSE_EVENT_TYPE = 'review_obsolete'

# One full sentinel period. The timer fires OnCalendar every 15 min (§ 1); the
# CLOSED-settle guard and the parity metric are both defined in terms of it.
SENTINEL_PERIOD_SEC = 15 * 60

# gh probe cap per pass (the in-review healer's precedent, § 1). Items beyond the
# budget wait for the next tick (self-healing).
MAX_PROBES_PER_CYCLE = 15

# Report-only rollout window (§ 8): both must hold before self-arming.
REPORT_ONLY_MIN_HOURS = 48
REPORT_ONLY_MIN_PASSES = 4

# UNKNOWN no-match declare-dead aging (§ 5).
NO_MATCH_AGE_DAYS = 14
NO_MATCH_MIN_CONSECUTIVE = 3

# Terminal cache bookkeeping (§ 5): prune older than 26 weeks; re-verify a CLOSED
# entry once after 24h (reopen recovery).
CACHE_PRUNE_WEEKS = 26
CLOSED_REVERIFY_HOURS = 24

# Probe-error storm threshold (§ 5): one aggregated health signal when >20% of a
# pass's probes error.
PROBE_ERROR_STORM_FRACTION = 0.20

_GITHUB_PR_RE = re.compile(r'^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+(?:[/?#].*)?$')

# UNKNOWN-ledger causes (§ 5).
CAUSE_NO_MATCH = 'no-match'
CAUSE_PROBE_ERROR = 'probe-error'
CAUSE_AMBIGUOUS = 'ambiguous'
CAUSE_SEQUENCE_OWNED = 'sequence-owned'

# Synthetic sequence-step task_ids never resolve to a PR; they age forever and
# must never escalate (§ 0/§ 5).
_SEQUENCE_TASK_PREFIX = 'seq-'


# -------------------- logging + heartbeat + gates --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [pr_terminal_fanout] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists() or EMERGENCY_HALT.exists()


# -------------------- time helpers --------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 ts (accepts trailing 'Z' + naive) to tz-aware UTC, or
    None. Mirrors dashboard_api / the reconcile healer so the "after" comparisons
    agree exactly with the derivation being reconciled."""
    if not isinstance(ts_str, str):
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# -------------------- JSON state I/O (atomic; fail-safe) --------------------

def _load_json(path: Path, default: Any) -> Any:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return data


def _save_json(path: Path, data: Any) -> None:
    try:
        atomic_write_json(path, data, indent=2)
    except OSError as e:
        log(f'save {path.name} failed: {e}', 'WARN')


# -------------------- terminal probe (gh) --------------------

def _probe_prs(coord_or_url: str, *, repos: Optional[list[str]] = None,
               timeout: float = tts.DEFAULT_GH_TIMEOUT_SEC) -> Optional[list[dict[str, Any]]]:
    """Return ALL PRs on a PR url's head branch across DEFAULT_REPOS, each with
    the fields the guards need (§ 3). None on total gh failure (probe-error).

    Enumerating the FULL PR set on the branch is guard 1 (OPEN-beats-terminal):
    a single ``gh pr view`` would hide a live re-attempt PR sharing the branch.
    """
    repo_list = repos if repos is not None else list(tts.DEFAULT_REPOS)
    fields = 'number,state,headRefName,headRefOid,mergedAt,closedAt,mergeCommit,url'
    # Resolve the head branch from the recorded PR url, then list every PR on it.
    view = tts.gh_json(['gh', 'pr', 'view', coord_or_url, '--json', fields],
                       timeout=timeout)
    if not isinstance(view, dict):
        return None
    branch = view.get('headRefName')
    if not isinstance(branch, str) or not branch:
        return [view]
    out: list[dict[str, Any]] = []
    any_ok = False
    for repo in repo_list:
        data = tts.gh_json(
            ['gh', 'pr', 'list', '--repo', repo, '--head', branch,
             '--state', 'all', '--json', fields, '--limit', '30'],
            timeout=timeout,
        )
        if isinstance(data, list):
            any_ok = True
            out.extend(pr for pr in data if isinstance(pr, dict))
    if not any_ok:
        # branch-list failed everywhere; fall back to the single viewed PR so a
        # partial outage still yields the coordinate we already resolved.
        return [view]
    # Ensure the recorded PR itself is represented (it may live in a repo the
    # --head list under-covered) — dedup by number+url.
    seen = {(pr.get('number'), pr.get('url')) for pr in out}
    if (view.get('number'), view.get('url')) not in seen:
        out.append(view)
    return out


def _is_ancestor(head_sha: Optional[str], *, repo_root: Optional[Path] = None) -> Optional[bool]:
    """True iff ``head_sha`` is an ancestor of origin/main (MERGED-ancestry guard,
    § 3.4). None when it can't be determined (missing sha / git error) so the
    caller treats it as ambiguous, never a pass."""
    if not isinstance(head_sha, str) or not head_sha:
        return None
    root = str(repo_root or os.environ.get('OURLIBERTY_REPO_ROOT', '/home/larry/agent-core'))
    try:
        proc = subprocess.run(
            ['git', '-C', root, 'merge-base', '--is-ancestor', head_sha, 'origin/main'],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None  # 128 / other → indeterminate (bad object, no origin/main)


def _live_in_flight(task_id: Optional[str]) -> bool:
    """True iff a live in-flight claim exists for the task (liveness guard,
    § 3.3): state/in-flight/<task>.json present with a signalable pid."""
    if not task_id:
        return False
    claim = AGENTS_ROOT / 'state' / 'in-flight' / f'{task_id}.json'
    data = _load_json(claim, None)
    if not isinstance(data, dict):
        return False
    pid = data.get('pid')
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return isinstance(pid, int) and pid > 0 and _pid_perm_err(pid)
    except OSError:
        return False


def _pid_perm_err(pid: int) -> bool:
    """A PermissionError from os.kill means the pid exists but is owned by
    another user — still a live process. Split out so _live_in_flight stays
    readable."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# -------------------- evidence grading (§ 3) --------------------

def grade_evidence(item: dict[str, Any], prs: list[dict[str, Any]]) -> str:
    """'identity' (fan-out permitted) or 'search' (fan-out FORBIDDEN).

    Identity-grade requires exact provenance: the item carries a recorded PR
    coordinate AND that PR's headRefName is exactly ``forge/<task_id>`` (a
    coordinate written by earlier search-grade reconciliation must NOT launder
    into fan-out authority). Anything else — a task-id/variant search match, a
    coordinate we can't cross-check to an exact forge branch — is search-grade.
    """
    task_id = item.get('task_id')
    pr_url = item.get('pr_url')
    if not (isinstance(task_id, str) and task_id and isinstance(pr_url, str) and pr_url):
        return 'search'
    if not _GITHUB_PR_RE.match(pr_url):
        return 'search'
    expected_branch = f'forge/{task_id}'
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        if pr.get('url') == pr_url or _same_pr(pr, pr_url):
            if pr.get('headRefName') == expected_branch:
                return 'identity'
    return 'search'


def _same_pr(pr: dict[str, Any], pr_url: str) -> bool:
    """True if this PR object is the one the recorded url names (number match)."""
    m = re.search(r'/pull/(\d+)', pr_url)
    if not m:
        return False
    try:
        return int(pr.get('number')) == int(m.group(1))
    except (TypeError, ValueError):
        return False


# -------------------- the five guards (§ 3) --------------------

class Decision:
    """The per-item verdict the guard pass produces."""

    __slots__ = ('action', 'terminal', 'terminal_ts', 'ancestry_verified',
                 'grade', 'cause', 'reason')

    def __init__(self, action: str, *, terminal: Optional[str] = None,
                 terminal_ts: Optional[str] = None, ancestry_verified: bool = False,
                 grade: str = 'search', cause: Optional[str] = None, reason: str = ''):
        self.action = action              # 'fanout' | 'keep' | 'unknown'
        self.terminal = terminal          # MERGED | CLOSED | None
        self.terminal_ts = terminal_ts
        self.ancestry_verified = ancestry_verified
        self.grade = grade
        self.cause = cause                # for action='unknown'
        self.reason = reason

    def as_dict(self) -> dict[str, Any]:
        return {
            'action': self.action, 'terminal': self.terminal,
            'terminal_ts': self.terminal_ts,
            'ancestry_verified': self.ancestry_verified, 'grade': self.grade,
            'cause': self.cause, 'reason': self.reason,
        }


def _terminal_pr(prs: list[dict[str, Any]], state: str) -> Optional[dict[str, Any]]:
    for pr in prs:
        if isinstance(pr, dict) and tts.classify_state(pr.get('state')) == state:
            return pr
    return None


def _pr_terminal_ts(pr: dict[str, Any], state: str) -> Optional[str]:
    return pr.get('mergedAt') if state == tts.MERGED else pr.get('closedAt')


def evaluate_item(
    item: dict[str, Any],
    prs: Optional[list[dict[str, Any]]],
    *,
    now: Optional[datetime] = None,
    period_sec: int = SENTINEL_PERIOD_SEC,
    prior_closed_seen: bool = False,
    is_ancestor_fn: Callable[[Optional[str]], Optional[bool]] = _is_ancestor,
    liveness_fn: Callable[[Optional[str]], bool] = _live_in_flight,
) -> Decision:
    """Run the § 3 grade + five guards over ONE item's PR set. Pure given the
    injected ``is_ancestor_fn`` / ``liveness_fn`` seams — no network I/O here.

    Returns a Decision. Only ``action=='fanout'`` permits a live close; every
    guard failure collapses to KEEP or UNKNOWN (conservative posture, § 1)."""
    now = now or _now()
    if prs is None:
        return Decision('unknown', cause=CAUSE_PROBE_ERROR, reason='gh probe failed')
    grade = grade_evidence(item, prs)

    # Guard 1 — OPEN-beats-terminal over the FULL PR set (defeats branch-reuse /
    # one-task-two-PRs). Reduce with tts._combine (OPEN > MERGED > CLOSED).
    combined = tts._combine([tts.classify_state(pr.get('state')) for pr in prs
                             if isinstance(pr, dict)])
    if combined == tts.OPEN:
        return Decision('keep', grade=grade, reason='live OPEN PR on branch')
    if combined not in tts.TERMINAL_STATES:
        return Decision('unknown', grade=grade, cause=CAUSE_NO_MATCH,
                        reason='no terminal PR matched')

    # Search-grade may NEVER fan out (§ 3). It can retire only its single
    # originating record — surfaced as KEEP here (the owning healer does that).
    if grade != 'identity':
        return Decision('keep', grade=grade, terminal=combined,
                        reason='search-grade evidence — fan-out forbidden')

    chosen = _terminal_pr(prs, combined)
    if chosen is None:
        return Decision('unknown', grade=grade, cause=CAUSE_NO_MATCH,
                        reason='combined terminal but no matching PR object')
    terminal_ts = _pr_terminal_ts(chosen, combined)
    term_dt = _parse_ts(terminal_ts)

    # Guard 3 — Liveness: a live in-flight claim means the work is being re-run.
    if liveness_fn(item.get('task_id')):
        return Decision('keep', grade=grade, terminal=combined,
                        reason='live in-flight claim')

    # Guard 2 — Temporal anchor: terminal ts strictly AFTER the item's last
    # dispatch/activity ts. A terminal predating the item's activity is not the
    # out-of-band-after phantom (a real verdict may still be coming).
    since_dt = _parse_ts(item.get('since'))
    if term_dt is None:
        return Decision('unknown', grade=grade, cause=CAUSE_AMBIGUOUS,
                        reason='terminal PR carries no timestamp')
    if since_dt is not None and term_dt <= since_dt:
        return Decision('keep', grade=grade, terminal=combined,
                        reason='terminal ts not after item activity')

    if combined == tts.MERGED:
        # Guard 4 — MERGED ancestry: the merge must be reachable from origin/main
        # (defeats stacked-PR-orphan: MERGED while content never landed). Either
        # the branch tip OR the merge commit reachable satisfies 3.4 — for a
        # squash/rebase merge (the system default) headRefOid is the pre-merge
        # branch tip and is NOT an ancestor, so mergeCommit.oid is the only proof
        # that lands.
        candidate_shas = [
            chosen.get('headRefOid'),
            (chosen.get('mergeCommit') or {}).get('oid'),
        ]
        anc = any(sha and is_ancestor_fn(sha) is True for sha in candidate_shas)
        if not anc:
            return Decision('unknown', grade=grade, cause=CAUSE_AMBIGUOUS,
                            reason='MERGED but ancestry unverified')
        return Decision('fanout', terminal=tts.MERGED, terminal_ts=term_dt.isoformat(),
                        ancestry_verified=True, grade=grade,
                        reason='identity-grade MERGED, ancestry verified')

    # combined == CLOSED — Guard 5 — CLOSED settle: closedAt older than one full
    # period AND observed CLOSED on two consecutive passes (close→reopen gap).
    age_sec = (now - term_dt).total_seconds()
    if age_sec < period_sec:
        return Decision('keep', grade=grade, terminal=tts.CLOSED,
                        reason='CLOSED not yet settled (within one period)')
    if not prior_closed_seen:
        return Decision('keep', grade=grade, terminal=tts.CLOSED,
                        reason='CLOSED first observation — need two consecutive')
    return Decision('fanout', terminal=tts.CLOSED, terminal_ts=term_dt.isoformat(),
                    ancestry_verified=False, grade=grade,
                    reason='identity-grade CLOSED, settled two passes')


# -------------------- outcome mapping + fan-out close (§ 4) --------------------

def outcome_for_terminal(terminal: str) -> str:
    """MERGED → approved (out-of-band); CLOSED → expired. Never 'approved' for
    abandoned work — the decision ledger's analytics depend on the distinction."""
    return 'approved' if terminal == tts.MERGED else 'expired'


def fanout_close(
    item: dict[str, Any],
    decision: Decision,
    cache: dict[str, Any],
    now: datetime,
    *,
    emit_fn: Callable[..., bool] = cee.emit_event,
    resolve_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Execute one identity-grade close. Ordering + partial-failure contract
    (§ 4): emit event → resolve legs → verify → **cache LAST**. Each leg is
    individually idempotent (deterministic event_id; decision_resolve's guarded
    clears), so a crash mid-fan-out is completed by the next run.

    Returns a small result dict for the artifact. The mission surface gets NO
    direct action (single-committer; GC owns it, § 4.3)."""
    task_id = item.get('task_id')
    pr_url = item.get('pr_url')
    terminal = decision.terminal
    result: dict[str, Any] = {'item_key': item.get('item_key'), 'emitted': False,
                              'resolved': None, 'cached': False}

    # 1. Emit review_obsolete (a stray emit for a never-reviewed task is ignored
    #    by _derive_in_review, which only builds cards from review_request rows).
    ok = emit_fn(
        event_type=CLOSE_EVENT_TYPE, agent=BUILDER_AGENT, task_id=task_id,
        pr_url=pr_url, ts=decision.terminal_ts,
        payload={'pr_state': terminal, 'ancestry_verified': decision.ancestry_verified,
                 'reason': f'PR {terminal} out of band; {decision.reason}',
                 'source': ALERT_SOURCE},
    )
    result['emitted'] = bool(ok)
    if not ok:
        # Emit failed — do NOT cache; the next run re-executes (idempotent).
        result['error'] = 'emit failed'
        return result

    # 2. Resolve the needs-Larry legs by the item's decision coordinate. Only
    #    the P-leg is entry_id-exact — without an entry_id that surface stays with
    #    its own healer backstop (note it in the artifact, § 4.2).
    if item.get('store') == 'decision':
        resolve = resolve_fn or _default_resolve
        outcome = outcome_for_terminal(terminal)
        res = resolve(task_id=task_id, pr_url=pr_url, entry_id=item.get('entry_id'),
                      outcome=outcome, actor=ALERT_SOURCE,
                      note=f'{terminal} out of band (sentinel fan-out)')
        result['resolved'] = res
        if not item.get('entry_id'):
            result['p_leg_delegated'] = True

    # 3. (verify per-surface consumption — best-effort; the deterministic ids
    #    make a re-run cheap, so we don't block on read-back here.)

    # 4. Cache LAST (§ 4 ordering + § 5): only now is the close durable.
    cache_put(cache, item, decision, now)
    result['cached'] = True
    return result


def _default_resolve(**kw: Any) -> dict[str, Any]:
    import decision_resolve as dr
    return dr.resolve_decision_for_entry(**kw)


# -------------------- terminal cache (§ 5) --------------------

def _cache_key(item: dict[str, Any], decision: Decision) -> str:
    if decision.terminal == tts.CLOSED:
        # CLOSED keyed to (item_key, closedAt, item.since) so a genuine reopen →
        # new close changes the key and is re-evaluated.
        return f"{item.get('item_key')}|{decision.terminal_ts}|{item.get('since')}"
    return str(item.get('item_key'))


def cache_put(cache: dict[str, Any], item: dict[str, Any], decision: Decision,
              now: datetime) -> None:
    entries = cache.setdefault('entries', {})
    entries[_cache_key(item, decision)] = {
        'item_key': item.get('item_key'), 'terminal': decision.terminal,
        'terminal_ts': decision.terminal_ts,
        'ancestry_verified': decision.ancestry_verified,
        'cached_at': now.isoformat(),
    }


def cache_hit(cache: dict[str, Any], item: dict[str, Any], decision: Decision,
              now: datetime) -> bool:
    """True iff this exact close is already durably cached AND still valid.
    ancestry-verified MERGED is cached permanently; a CLOSED entry is re-verified
    once after 24h (reopen recovery)."""
    entry = cache.get('entries', {}).get(_cache_key(item, decision))
    if not isinstance(entry, dict):
        return False
    if entry.get('terminal') == tts.CLOSED:
        cached_at = _parse_ts(entry.get('cached_at'))
        if cached_at is not None and (now - cached_at) >= timedelta(hours=CLOSED_REVERIFY_HOURS):
            return False  # force a re-verify pass
    return True


def prune_cache(cache: dict[str, Any], now: datetime) -> int:
    entries = cache.get('entries', {})
    cutoff = now - timedelta(weeks=CACHE_PRUNE_WEEKS)
    drop = [k for k, v in entries.items()
            if isinstance(v, dict) and (_parse_ts(v.get('cached_at')) or now) < cutoff]
    for k in drop:
        entries.pop(k, None)
    return len(drop)


# -------------------- UNKNOWN ledger (§ 5) --------------------

def update_unknown_ledger(
    ledger: dict[str, Any], item_key: str, cause: str, now: datetime,
) -> dict[str, Any]:
    """Record/refresh one UNKNOWN row. consecutive_count increments while the
    cause persists; a clean probe (cause=None via drop) resets it elsewhere."""
    rows = ledger.setdefault('rows', {})
    row = rows.get(item_key)
    now_iso = now.isoformat()
    if not isinstance(row, dict) or row.get('cause') != cause:
        row = {'item_key': item_key, 'cause': cause, 'first_seen': now_iso,
               'consecutive_count': 1, 'last_probe': now_iso}
    else:
        row['consecutive_count'] = int(row.get('consecutive_count', 0)) + 1
        row['last_probe'] = now_iso
    rows[item_key] = row
    return row


def bound_unknown_ledger(ledger: dict[str, Any], live_keys: set[str]) -> int:
    """Drop rows whose item left enumeration (bounded, § 5)."""
    rows = ledger.get('rows', {})
    drop = [k for k in rows if k not in live_keys]
    for k in drop:
        rows.pop(k, None)
    return len(drop)


def no_match_declare_dead(row: dict[str, Any], now: datetime) -> bool:
    """True iff a no-match row is ripe for the ONE declare-dead approval:
    cause=no-match, age >= 14d, >= 3 consecutive clean probes. sequence-owned
    NEVER qualifies (handled by cause, never CAUSE_NO_MATCH)."""
    if row.get('cause') != CAUSE_NO_MATCH:
        return False
    if int(row.get('consecutive_count', 0)) < NO_MATCH_MIN_CONSECUTIVE:
        return False
    first = _parse_ts(row.get('first_seen'))
    if first is None:
        return False
    return (now - first) >= timedelta(days=NO_MATCH_AGE_DAYS)


# -------------------- report-only + self-firing arming (§ 8) --------------------

def default_arm_state() -> dict[str, Any]:
    return {'first_pass_at': None, 'pass_count': 0, 'arming_requested': False,
            'arming_approval_id': None, 'armed': False, 'decided_at': None,
            'reject_reason': None}


def record_pass(arm: dict[str, Any], now: datetime) -> None:
    if not arm.get('first_pass_at'):
        arm['first_pass_at'] = now.isoformat()
    arm['pass_count'] = int(arm.get('pass_count', 0)) + 1


def window_complete(arm: dict[str, Any], now: datetime) -> bool:
    """>= 48h since the first recorded pass AND >= 4 passes recorded (§ 8)."""
    if int(arm.get('pass_count', 0)) < REPORT_ONLY_MIN_PASSES:
        return False
    first = _parse_ts(arm.get('first_pass_at'))
    if first is None:
        return False
    return (now - first) >= timedelta(hours=REPORT_ONLY_MIN_HOURS)


def is_armed(arm: dict[str, Any]) -> bool:
    return bool(arm.get('armed'))


def request_arming(
    arm: dict[str, Any], n_would_close: int, m_diverged: int, now: datetime,
    *, add_pending_fn: Optional[Callable[..., Any]] = None,
    emit_fn: Callable[..., bool] = cee.emit_event,
) -> bool:
    """Self-emit EXACTLY ONE durable arming approval (§ 8). Idempotent on the
    durable ``arming_requested`` flag: a second call after the window is a no-op,
    so re-runs never re-emit. Returns True iff it emitted this call."""
    if arm.get('arming_requested'):
        return False
    approval_id = f'pr-fanout-arm-{now.strftime("%Y%m%dT%H%M%SZ")}'
    recommend = 'AGAINST arming' if m_diverged > 0 else 'arming'
    summary = (
        f'PR-terminal fan-out sentinel finished its {REPORT_ONLY_MIN_HOURS}h '
        f'report-only window: {n_would_close} would-closes, of which {m_diverged} '
        f'diverged from what the backstops actually closed. Arm fan-out? '
        f'(approve -> switch to live fan-out; reject -> stay report-only). '
        f'Evidence recommends {recommend}.'
    )
    # Durable queue entry (the queue IS the reminder — no silent memory dependence).
    if add_pending_fn is not None:
        try:
            add_pending_fn(approval_id, summary)
        except Exception as e:  # noqa: BLE001 — durable-queue best-effort
            log(f'arming add_pending failed: {type(e).__name__}: {e}', 'WARN')
    # Approvals-tab event so the decision is evidence-backed and self-explanatory.
    emit_fn(event_type='approval_request', agent='beacon', task_id=approval_id,
            payload={'summary': summary, 'target_agent': 'beacon',
                     'n_would_close': n_would_close, 'm_diverged': m_diverged,
                     'source': ALERT_SOURCE})
    arm['arming_requested'] = True
    arm['arming_approval_id'] = approval_id
    log(f'ARMING approval self-emitted id={approval_id} N={n_would_close} '
        f'M={m_diverged} recommend={recommend}')
    return True


def apply_arming_decision(arm: dict[str, Any], approve: bool, now: datetime,
                          reason: str = '') -> None:
    """Resolution entry point: approve flips the durable ``armed`` flag; reject
    records the reason and stays report-only (a later re-arm is a deliberate
    operator action, not an automatic re-ask)."""
    arm['decided_at'] = now.isoformat()
    if approve:
        arm['armed'] = True
        arm['reject_reason'] = None
    else:
        arm['armed'] = False
        arm['reject_reason'] = reason or 'rejected'


# -------------------- parity metric (§ 7) --------------------

def is_parity_miss(gh_terminal_ts: Optional[str], healer_close_ts: Optional[str],
                   appeared_in_pass_after_terminal: bool,
                   *, period_sec: int = SENTINEL_PERIOD_SEC) -> bool:
    """A backstop "miss" counts ONLY when gh_terminal_ts + one full sentinel
    period + 5min slack < healer_close_ts AND the item appeared in a completed
    pass's enumeration after gh_terminal_ts (§ 7). A lost race is NOT a miss."""
    if not appeared_in_pass_after_terminal:
        return False
    term = _parse_ts(gh_terminal_ts)
    close = _parse_ts(healer_close_ts)
    if term is None or close is None:
        return False
    threshold = term + timedelta(seconds=period_sec + 5 * 60)
    return threshold < close


# -------------------- alerts (self-watch death alarm + health signal) --------------------

def _emit_alert(severity: str, subject: str, message: str, suggested_action: str,
                route: Optional[str] = None) -> bool:
    try:
        import larry_alerts as la
        kw: dict[str, Any] = dict(source=ALERT_SOURCE, severity=severity,
                                  subject=subject, message=message,
                                  suggested_action=suggested_action)
        if route is not None:
            kw['route'] = route
        return bool(la.append_alert(**kw))
    except TypeError:
        # older append_alert without a route kwarg — retry without it (the
        # never_silence translation still escalates the death alarm).
        try:
            import larry_alerts as la
            return bool(la.append_alert(source=ALERT_SOURCE, severity=severity,
                                        subject=subject, message=message,
                                        suggested_action=suggested_action))
        except Exception as e:  # noqa: BLE001
            log(f'alert emit failed: {type(e).__name__}: {e}', 'WARN')
            return False
    except Exception as e:  # noqa: BLE001
        log(f'alert emit failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- enumeration (§ 2) --------------------

def enumerate_in_review(
    *, get_client: Optional[Callable[[], Any]] = None,
    fetch: Optional[Callable[[Any, str], Optional[list[dict[str, Any]]]]] = None,
    derive: Optional[Callable[..., list[dict[str, Any]]]] = None,
) -> Optional[list[dict[str, Any]]]:
    """Reuse the dashboard derivation exactly as heal_stale_in_review_reconcile
    does. A None fetch skips the whole tick (deriving on a failed verdict fetch
    resurrects phantoms, § 2.1). Returns None to signal "skip this tick"."""
    if get_client is None or fetch is None or derive is None:
        import dashboard_api as dapi  # noqa: E402 — heavy import, deferred
        get_client = get_client or dapi._get_larry_action_supabase_client
        fetch = fetch or dapi._fetch_chain_events_for_agent
        derive = derive or dapi._derive_in_review
    client = get_client()
    if client is None:
        return None
    rows = fetch(client, BUILDER_AGENT)
    verdict_rows = fetch(client, 'mirror')
    if rows is None or verdict_rows is None:
        return None
    items: list[dict[str, Any]] = []
    for card in derive(rows, verdict_rows):
        if not isinstance(card, dict):
            continue
        tid = card.get('task_id')
        pr_url = card.get('pr_url')
        if not (isinstance(tid, str) and tid and isinstance(pr_url, str) and pr_url):
            continue
        items.append({'item_key': f'inreview:{tid}', 'store': 'inreview',
                      'task_id': tid, 'pr_url': pr_url, 'since': card.get('since'),
                      'entry_id': None, 'decision_key': canonical_decision_key(tid, pr_url)})
    return items


def enumerate_pending_approvals(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Needs-Larry P records carrying a pr_url (§ 2.2). Each row's ``id`` is the
    entry_id the P-leg pops."""
    path = path or (STATE_DIR / 'beacon-pending-approvals.json')
    data = _load_json(path, None)
    pending = data.get('pending') if isinstance(data, dict) else data
    if not isinstance(pending, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        payload = entry.get('dispatch_payload') if isinstance(entry.get('dispatch_payload'), dict) else {}
        pr_url = payload.get('pr_url') or entry.get('pr_url')
        tid = entry.get('id') or entry.get('task_id')
        if not (isinstance(pr_url, str) and pr_url and isinstance(tid, str) and tid):
            continue
        key = canonical_decision_key(tid, pr_url)
        out.append({'item_key': f'decision:{key}', 'store': 'decision',
                    'task_id': tid, 'pr_url': pr_url,
                    'since': entry.get('created_at') or entry.get('created'),
                    'entry_id': entry.get('id'), 'decision_key': key})
    return out


def is_sequence_owned(task_id: Optional[str]) -> bool:
    return isinstance(task_id, str) and task_id.startswith(_SEQUENCE_TASK_PREFIX)


# -------------------- one pass (orchestration) --------------------

def run_cycle(
    *,
    enumerators: Optional[list[Callable[[], list[dict[str, Any]]]]] = None,
    probe_fn: Callable[[str], Optional[list[dict[str, Any]]]] = _probe_prs,
    emit_fn: Callable[..., bool] = cee.emit_event,
    resolve_fn: Optional[Callable[..., dict[str, Any]]] = None,
    add_pending_fn: Optional[Callable[..., Any]] = None,
    max_probes: int = MAX_PROBES_PER_CYCLE,
    now: Optional[datetime] = None,
    persist: bool = True,
) -> dict[str, Any]:
    """One sentinel pass. Report-only unless the durable ``armed`` flag is set.

    Returns the artifact dict (also written to LAST_PASS_FILE when persist).
    Injectable seams make the whole pass unit-testable with no gh/Supabase.
    """
    now = now or _now()
    heartbeat()
    arm = _load_json(ARM_STATE_FILE, None) or default_arm_state()
    ledger = _load_json(UNKNOWN_LEDGER_FILE, None) or {'rows': {}}
    cache = _load_json(TERMINAL_CACHE_FILE, None) or {'entries': {}}

    # 1. Enumerate. A None from the in-review enumerator means "skip this tick".
    if enumerators is None:
        enumerators = [lambda: enumerate_in_review() or [],
                       enumerate_pending_approvals]
    items: list[dict[str, Any]] = []
    for en in enumerators:
        try:
            got = en()
        except Exception as e:  # noqa: BLE001 — one store never unwatches the rest
            log(f'enumerator failed: {type(e).__name__}: {e}', 'WARN')
            continue
        if got:
            items.extend(got)

    live_keys = {it['item_key'] for it in items if it.get('item_key')}
    artifact: dict[str, Any] = {
        'ts': now.isoformat(), 'armed': is_armed(arm),
        'enumerated': len(items), 'probed': 0,
        'would_close': [], 'closed': [], 'unknown': [], 'kept': 0,
        'probe_errors': 0, 'notes': [],
    }

    # 2. Probe + grade + guard each item (probe budget capped).
    probes = 0
    decisions: list[tuple[dict[str, Any], Decision]] = []
    for item in items:
        if is_sequence_owned(item.get('task_id')):
            update_unknown_ledger(ledger, item['item_key'], CAUSE_SEQUENCE_OWNED, now)
            continue
        pr_url = item.get('pr_url')
        if not (isinstance(pr_url, str) and _GITHUB_PR_RE.match(pr_url)):
            continue
        prior_closed = bool(cache.get('closed_seen', {}).get(item['item_key']))
        if probes >= max_probes:
            artifact['notes'].append(f'probe budget spent; {item["item_key"]} deferred')
            continue
        probes += 1
        try:
            prs = probe_fn(pr_url)
        except Exception:  # noqa: BLE001 — a probe must never abort the sweep
            prs = None
        decision = evaluate_item(item, prs, now=now, prior_closed_seen=prior_closed)
        decisions.append((item, decision))
    artifact['probed'] = probes

    # Track CLOSED first-observations for the two-consecutive-pass settle guard.
    closed_seen = cache.setdefault('closed_seen', {})

    # 3. Classify each decision into the artifact + ledger.
    would_close: list[tuple[dict[str, Any], Decision]] = []
    for item, decision in decisions:
        key = item['item_key']
        if decision.action == 'unknown':
            artifact['unknown'].append({'item_key': key, 'cause': decision.cause,
                                        'reason': decision.reason})
            if decision.cause == CAUSE_PROBE_ERROR:
                artifact['probe_errors'] += 1
            else:
                update_unknown_ledger(ledger, key, decision.cause, now)
        elif decision.action == 'fanout':
            would_close.append((item, decision))
            artifact['would_close'].append({'item_key': key, 'grade': decision.grade,
                                            'terminal': decision.terminal,
                                            'ancestry_verified': decision.ancestry_verified,
                                            'terminal_ts': decision.terminal_ts})
            # clean probe result → clear any stale no-match row
            ledger.get('rows', {}).pop(key, None)
        else:  # keep
            artifact['kept'] += 1
            if decision.terminal == tts.CLOSED:
                closed_seen[key] = now.isoformat()  # remember for next pass
            elif key in closed_seen and decision.terminal != tts.CLOSED:
                closed_seen.pop(key, None)
            ledger.get('rows', {}).pop(key, None)

    # 4. Rollout gate. Report-only unless armed (§ 8). NO fan-out writes while
    #    armed is false — the durable flag gates every live close.
    record_pass(arm, now)
    n_would = len(would_close)
    if is_armed(arm):
        for item, decision in would_close:
            if cache_hit(cache, item, decision, now):
                continue  # already durably closed — idempotent skip
            res = fanout_close(item, decision, cache, now,
                               emit_fn=emit_fn, resolve_fn=resolve_fn)
            artifact['closed'].append(res)
    else:
        artifact['report_only'] = True
        if window_complete(arm, now):
            # M = would-have-been-wrong count (parity divergence). Report-only
            # writes no per-item parity rows here, so M is 0 unless a parity
            # source is wired; the mechanism (self-emit gated by arming_requested)
            # is what acceptance 10.b asserts.
            m_diverged = int(artifact.get('m_diverged', 0))
            emitted = request_arming(arm, n_would, m_diverged, now,
                                     add_pending_fn=add_pending_fn, emit_fn=emit_fn)
            artifact['arming_requested_this_pass'] = emitted

    # 5. Aging escalations for ripe no-match rows (ONE approval each, § 5).
    bound_unknown_ledger(ledger, live_keys)
    for row in list(ledger.get('rows', {}).values()):
        if no_match_declare_dead(row, now) and not row.get('escalated'):
            _emit_alert('warning', f'pr-fanout-declare-dead:{row["item_key"]}',
                        message=(f"Can't find the work for {row['item_key']} anywhere "
                                 f"({row['consecutive_count']} clean probes over "
                                 f">= {NO_MATCH_AGE_DAYS}d). Declare dead / keep?"),
                        suggested_action='Review the item; declare dead or keep tracking.')
            row['escalated'] = True

    # 6. Probe-error storm → ONE aggregated health signal (§ 5).
    if probes > 0 and (artifact['probe_errors'] / probes) > PROBE_ERROR_STORM_FRACTION:
        _emit_alert('warning', 'pr-fanout-probe-health',
                    message=(f'{artifact["probe_errors"]}/{probes} probes errored this '
                             f'pass (>{int(PROBE_ERROR_STORM_FRACTION*100)}%).'),
                    suggested_action='Check gh auth / GitHub API health on the droplet.')

    # 7. Bound + persist state. Cache is the LAST durable write per close, but the
    #    aggregate state files are written once at pass end.
    prune_cache(cache, now)
    if persist:
        _save_json(ARM_STATE_FILE, arm)
        _save_json(UNKNOWN_LEDGER_FILE, ledger)
        _save_json(TERMINAL_CACHE_FILE, cache)
        try:
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            _save_json(LAST_PASS_FILE, artifact)
        except OSError as e:
            log(f'artifact write failed: {e}', 'WARN')
    log(f'pass done: enumerated={artifact["enumerated"]} probed={probes} '
        f'would_close={n_would} closed={len(artifact["closed"])} '
        f'armed={is_armed(arm)}')
    return artifact


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description='Terminal-event fan-out sentinel.')
    p.add_argument('--once', action='store_true', help='run one pass and exit')
    p.parse_args(argv)
    if kill_switch_active():
        log('kill-switch / EMERGENCY_HALT active; exiting cleanly')
        return 0
    run_cycle()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
