#!/usr/bin/env python3
"""held_alert_escalation.py — promote stale `hold` alerts into DMs.

Spec: agents/beacon/specs/alert-pipeline-rework.md Part B (B5 + B6).

The hybrid DM gate (B1) lets an emitter `hold` an alert: it lands on the
dashboard but is NOT DM'd to Larry. A held line is meant to be a *pending
judgment*, not a silent drop — so something must eventually decide "this has
sat unresolved long enough; Larry should see it." That is this module. Both
escalation paths PROMOTE by appending a fresh `route='escalate'` line via
``larry_alerts.append_promotion`` (B3) — the bot's offset cursor is forward-only
(it skip-and-advances past hold lines), so rewriting a held line in place is
invisible; an APPEND is the only way to turn a hold into a DM.

Two independent, deliberately-redundant escalation paths (defense in depth):

  B5 — persistence rule (Pulse-cycle-driven). Runs each Pulse cycle (~10 min).
       Keeps a cross-cycle cycle counter per held fingerprint in a DEDICATED
       state file (held-alert-probation.json, modeled on promote_alerts.py's
       first_seen_ts + promote-once). A fingerprint still open after
       ``N_CYCLES_BEFORE_PROMOTE`` cycles is promoted once.

  B6 — Pulse-independent backstop. Runs on its own timer and is STATELESS: it
       reads the held line's OWN ``ts`` and promotes any hold open past
       ``BACKSTOP_SEC`` (30 min) of wall-clock age. It needs neither the state
       file nor a healthy Pulse — if persistence (B5) or Pulse itself is dead,
       this still fires. That independence is the whole point of B6.

Promote-once (shared by BOTH paths, queue-authoritative): a promotion appends an
escalate line carrying ``promoted_from=<source>:<subject>``. On the next scan
that line is the most-recent event for the fingerprint, so the fingerprint is no
longer an "open hold" (see ``compute_open_holds``) and neither path re-promotes
it. The append-only queue is the source of truth; B5's state ``promoted`` flag is
a belt-and-suspenders optimization, not the guard. A truly-simultaneous double
fire of the two timers is bounded at-least-once (a rare duplicate DM), matching
the bot's existing delivery semantics.

A held fingerprint is "open" (still pending) iff the most-recent queue event for
its ``source:subject`` key is a `hold` — i.e. no later `escalate` / `closure` /
promotion line superseded it — AND it is not durably silenced. A closure (self-
heal) or any escalate for the same key means it was already dealt with out of
band, so it is dropped from probation, never promoted.

Runs via two systemd timers. Stdlib only (imports sibling larry_alerts).
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'held-alert-escalation.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'held-alert-escalation.heartbeat'
# Dedicated state file — NOT promote_alerts' promotion-probation.json. That file
# tracks a different stream (pulse-escalations.json); commingling the two cycle
# counters would let one stream's churn reset/promote the other's.
STATE_FILE = AGENTS_ROOT / 'state' / 'held-alert-probation.json'

# Repo scripts dir on sys.path so we can import sibling modules by bare name
# (matches promote_alerts.py / the rest of scripts/).
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import larry_alerts  # noqa: E402
import task_terminal_state as tts  # noqa: E402 — shared gh-probe kernel (gh_json/classify_state)

# Bounded wall-clock cap for the live-PR-state gate's `gh pr view` subprocess.
# Matches build_sequence_advancer's GH_PR_VIEW_TIMEOUT_SEC — a gh call that runs
# long must fail-open (None), never wedge the promotion sweep.
GH_PR_VIEW_TIMEOUT_SEC = 30

# B5: promote a held fingerprint after it has been seen open for this many
# consecutive persistence cycles. At the Pulse 10-min cadence, 3 cycles ≈ 30 min
# — deliberately aligned with B6's wall-clock backstop so the two paths agree on
# "stale" while reaching it by independent mechanisms.
N_CYCLES_BEFORE_PROMOTE = 3

# B6: promote any hold open past this much wall-clock age, regardless of cycle
# count or Pulse health. Uses the hold record's own `ts`.
BACKSTOP_SEC = 30 * 60


# -------------------- logging + heartbeat --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
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
    return KILL_SWITCH.exists()


# -------------------- queue scan (pure-ish) --------------------

def _parse_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _record_key(rec: dict) -> Optional[str]:
    """The `source:subject` cooldown/dedup key for a record (bare source if no
    subject). None when there is no usable string source."""
    source = rec.get('source')
    if not isinstance(source, str) or not source:
        return None
    subject = rec.get('subject')
    return f'{source}:{subject}' if subject else source


def _is_alert_record(rec: dict) -> bool:
    """False for notification / approval-request records (they carry a `kind`
    and are 1:1 with a task — never the held infra noise this module promotes)
    and for malformed markers."""
    if not isinstance(rec, dict):
        return False
    if rec.get('_malformed'):
        return False
    if rec.get('kind') in ('notification', 'approval_request'):
        return False
    return True


@dataclass
class HoldInfo:
    """An open (still-pending) held fingerprint."""
    key: str
    source: str
    subject: str
    severity: str
    message: str
    suggested_action: Optional[str]
    ts: str
    age_sec: float


def read_all_records() -> list[dict]:
    """Every parsed record in the alert queue, oldest first.

    Unlike ``larry_alerts.read_pending`` (which starts at the bot's offset — and
    the bot has already skip-advanced PAST every hold line), this reads the whole
    file: the held lines we promote live BEHIND the bot's cursor. Malformed lines
    are skipped (they can't be a hold we track). Never raises — [] on read error.
    """
    path = larry_alerts.ALERTS_FILE
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with open(path, encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


# -------------------- live-PR-state gate --------------------

# `auto-merge-<type>:<owner/repo>:<pr_number>` — the shape outbox_notifier emits
# for held auto-merge alerts (scripts/outbox_notifier.py). The `<owner/repo>`
# segment carries a `/` but no `:`, so a colon-split leaves the PR number as the
# trailing segment and the repo as the one before it.
_AUTO_MERGE_SUBJECT_RE = re.compile(
    r'^auto-merge-[a-z0-9-]+:(?P<repo>[^:/\s]+/[^:/\s]+):(?P<num>\d+)$'
)
# Defensive: a full github.com PR URL anywhere in the subject.
_GITHUB_PR_URL_RE = re.compile(
    r'github\.com/(?P<repo>[^/\s]+/[^/\s]+)/pull/(?P<num>\d+)'
)


def extract_pr_ref(subject: Any) -> Optional[str]:
    """Return an ``owner/repo#number`` ref for `gh pr view`, or None.

    Matches ONLY the `auto-merge-*` held-alert subject shape carrying a
    `<owner/repo>:<pr_number>` tail (and, defensively, a full github.com PR URL).
    Any other subject — a heal fingerprint, a bare source, a non-PR alert —
    returns None, so the live-PR-state gate is a no-op (zero gh calls) for it.
    """
    if not isinstance(subject, str) or not subject:
        return None
    m = _AUTO_MERGE_SUBJECT_RE.match(subject.strip())
    if m is None:
        m = _GITHUB_PR_URL_RE.search(subject)
    if m is None:
        return None
    return f"{m.group('repo')}#{m.group('num')}"


def default_pr_state(ref: str) -> Optional[str]:
    """Live PR state for a ref via a bounded ``gh pr view``: 'OPEN' | 'MERGED' |
    'CLOSED', or None on ANY failure (timeout, missing auth, non-zero exit,
    unparseable output, unrecognized state).

    None is the fail-open signal: the gate suppresses a promotion ONLY on a
    positive MERGED/CLOSED result, so a flaky/slow gh call can never swallow a
    genuine still-open escalation. Reuses the shared ``task_terminal_state``
    probe kernel (gh_json + classify_state) — the same UNKNOWN-on-error core
    build_sequence_advancer's merged-check rides."""
    if not ref:
        return None
    data = tts.gh_json(
        ['gh', 'pr', 'view', ref, '--json', 'state'],
        timeout=GH_PR_VIEW_TIMEOUT_SEC,
    )
    if not isinstance(data, dict):
        return None
    state = tts.classify_state(data.get('state'))
    return state if state != tts.UNKNOWN else None


def compute_open_holds(
    records: list[dict],
    *,
    now: Optional[datetime] = None,
    is_silenced_fn: Callable[[str], bool] = larry_alerts.is_silenced,
    pr_state_fn: Callable[[str], Optional[str]] = default_pr_state,
) -> dict[str, HoldInfo]:
    """Map of still-open held fingerprints from a full queue snapshot.

    A fingerprint is OPEN iff the most-recent event (highest line index) for its
    key is a `hold` — no later `escalate` / `closure` / promotion line superseded
    it — AND it is not durably silenced. Walking in index order and letting the
    last event win gives free promote-once: once we append a promotion (escalate,
    ``promoted_from=key``), that line becomes the latest event and the key drops
    out of this map on the next scan.

    A hold WITHOUT a subject is skipped: ``append_promotion`` needs a subject to
    build the distinct ``::promoted`` marker, and without one we cannot reliably
    detect our own promotion line (promote-once would break). These are
    discouraged anyway (a subject is the dedup key).

    Live-PR-state gate: a hold whose subject references a PR (auto-merge-*
    subjects) is EXCLUDED when ``pr_state_fn(ref)`` reports the PR is MERGED or
    CLOSED — the promotion window is wide enough that a held PR often merges
    before it fires, and a `::promoted` DM about an already-merged PR is noise.
    The gate is FAIL-OPEN: it suppresses only on a positive MERGED/CLOSED signal;
    a None result (gh timeout / error / unparseable) or a subject with no
    extractable PR ref leaves the hold PROMOTABLE and issues no gh call in the
    latter case. Suppression requires proof the PR is done, never absence of
    proof it is open.
    """
    now = now or datetime.now(timezone.utc)
    # key -> (index, HoldInfo-ish dict) for the latest hold; and key -> latest
    # resolution index. "Latest event wins" decides open vs resolved.
    latest_hold: dict[str, tuple[int, dict]] = {}
    latest_resolution: dict[str, int] = {}

    for idx, rec in enumerate(records):
        if not _is_alert_record(rec):
            continue
        # A promotion line resolves the fingerprint it was promoted FROM.
        if rec.get('promotion') and isinstance(rec.get('promoted_from'), str):
            pf = rec['promoted_from']
            latest_resolution[pf] = idx
        key = _record_key(rec)
        if key is None:
            continue
        route = rec.get('route', larry_alerts.DEFAULT_ROUTE)
        if route == 'hold':
            latest_hold[key] = (idx, rec)
        elif route in ('escalate', 'closure'):
            latest_resolution[key] = idx

    out: dict[str, HoldInfo] = {}
    for key, (hold_idx, rec) in latest_hold.items():
        if latest_resolution.get(key, -1) > hold_idx:
            continue  # superseded by a later escalate/closure/promotion
        if is_silenced_fn(key):
            continue  # durably suppressed (Medic-confirmed false positive)
        subject = rec.get('subject')
        if not isinstance(subject, str) or not subject:
            log(f'open hold {key!r} has no subject; cannot promote-once, skipping',
                'WARN')
            continue
        pr_ref = extract_pr_ref(subject)
        if pr_ref is not None and pr_state_fn(pr_ref) in (tts.MERGED, tts.CLOSED):
            log(f'open hold {key!r} PR {pr_ref} is done; gating promotion')
            continue  # PR already merged/closed — promoting is stale noise
        hold_dt = _parse_ts(rec.get('ts'))
        age = (now - hold_dt).total_seconds() if hold_dt else 0.0
        out[key] = HoldInfo(
            key=key,
            source=rec['source'],
            subject=subject,
            severity=rec.get('severity', 'warning'),
            message=rec.get('message', ''),
            suggested_action=rec.get('suggested_action'),
            ts=rec.get('ts', ''),
            age_sec=age,
        )
    return out


# -------------------- state (B5 cycle counter) --------------------

def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    holds = data.get('holds')
    return holds if isinstance(holds, dict) else {}


def write_state(holds: dict[str, Any], path: Path = STATE_FILE) -> None:
    """Atomic write (tmp + rename) of the probation state."""
    payload = {
        'version': 1,
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'holds': holds,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.rename(path)
    except OSError as e:
        log(f'state write failed: {type(e).__name__}: {e}', 'WARN')


# -------------------- promotion --------------------

def _default_append(hold: HoldInfo, reason: str) -> bool:
    return larry_alerts.append_promotion(
        source=hold.source,
        severity=hold.severity,
        message=hold.message,
        subject=hold.subject,
        suggested_action=hold.suggested_action,
        reason=reason,
    )


# -------------------- B5: persistence cycle --------------------

def run_cycle(
    *,
    records: Optional[list[dict]] = None,
    state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    append_fn: Callable[[HoldInfo, str], bool] = _default_append,
    is_silenced_fn: Callable[[str], bool] = larry_alerts.is_silenced,
    pr_state_fn: Callable[[str], Optional[str]] = default_pr_state,
    persist: bool = True,
) -> dict[str, Any]:
    """One persistence cycle (B5). Promote holds open >= N cycles.

    All IO seams (records, state, now, append_fn, is_silenced_fn, pr_state_fn)
    are injectable so the whole flow is unit-testable without the filesystem or
    network. Returns a summary.
    """
    now = now or datetime.now(timezone.utc)
    records = read_all_records() if records is None else records
    prior = load_state() if state is None else state
    open_holds = compute_open_holds(
        records, now=now, is_silenced_fn=is_silenced_fn, pr_state_fn=pr_state_fn)

    new_state: dict[str, Any] = {}
    promoted: list[str] = []
    waiting: list[str] = []

    for key, hold in open_holds.items():
        prev = prior.get(key) if isinstance(prior, dict) else None
        # Already promoted earlier and somehow still open (e.g. append landed but
        # the scan ran before it): keep the flag, never re-promote.
        if prev and prev.get('promoted'):
            entry = dict(prev)
            entry['last_seen_ts'] = now.isoformat()
            new_state[key] = entry
            continue

        cycles = int((prev or {}).get('cycles', 0)) + 1
        entry = {
            'first_seen_ts': (prev or {}).get('first_seen_ts') or now.isoformat(),
            'last_seen_ts': now.isoformat(),
            'hold_ts': hold.ts,
            'severity': hold.severity,
            'cycles': cycles,
            'promoted': False,
        }

        if cycles >= N_CYCLES_BEFORE_PROMOTE:
            ok = False
            try:
                ok = append_fn(hold, f'persistence:{cycles}-cycles')
            except Exception as e:  # noqa: BLE001 — promotion must not crash cycle
                log(f'promote-append failed for {key!r}: {type(e).__name__}: {e}',
                    'WARN')
            if ok:
                entry['promoted'] = True
                entry['promoted_ts'] = now.isoformat()
                promoted.append(key)
                log(f'PROMOTED (persistence) {key!r} after {cycles} cycles '
                    f'severity={hold.severity}')
            else:
                # Append failed → leave un-promoted; next cycle retries.
                waiting.append(key)
                log(f'promote-append failed for {key!r}; retry next cycle', 'WARN')
        else:
            waiting.append(key)

        new_state[key] = entry

    # Keys no longer open (resolved / silenced / gone) are simply not carried
    # into new_state → they self-clear from probation.
    if persist:
        write_state(new_state)

    return {
        'open': len(open_holds),
        'promoted': promoted,
        'waiting': waiting,
    }


# -------------------- B6: Pulse-independent backstop --------------------

def run_backstop(
    *,
    records: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
    append_fn: Callable[[HoldInfo, str], bool] = _default_append,
    is_silenced_fn: Callable[[str], bool] = larry_alerts.is_silenced,
    pr_state_fn: Callable[[str], Optional[str]] = default_pr_state,
    backstop_sec: float = BACKSTOP_SEC,
) -> dict[str, Any]:
    """Stateless 30-min backstop (B6). Promote any hold whose own ``ts`` is older
    than ``backstop_sec``, regardless of cycle count or Pulse/persistence health.

    No state file: the queue is the only input, and promote-once rides
    ``compute_open_holds`` (a promoted hold stops being open). This is why B6
    survives a dead persistence timer or a corrupt state file — it is the
    independent safety net under B5.
    """
    now = now or datetime.now(timezone.utc)
    records = read_all_records() if records is None else records
    open_holds = compute_open_holds(
        records, now=now, is_silenced_fn=is_silenced_fn, pr_state_fn=pr_state_fn)

    promoted: list[str] = []
    for key, hold in open_holds.items():
        if hold.age_sec < backstop_sec:
            continue
        ok = False
        try:
            ok = append_fn(hold, f'backstop:{int(hold.age_sec)}s')
        except Exception as e:  # noqa: BLE001 — promotion must not crash sweep
            log(f'backstop append failed for {key!r}: {type(e).__name__}: {e}',
                'WARN')
        if ok:
            promoted.append(key)
            log(f'PROMOTED (backstop) {key!r} age={int(hold.age_sec)}s '
                f'severity={hold.severity}')
        else:
            log(f'backstop append failed for {key!r}; retry next sweep', 'WARN')

    return {'open': len(open_holds), 'promoted': promoted}


# -------------------- entrypoint --------------------

def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else 'persistence'
    if mode not in ('persistence', 'backstop'):
        log(f'unknown mode {mode!r}; expected persistence|backstop', 'ERROR')
        return 2
    if kill_switch_active():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()
    if mode == 'persistence':
        summary = run_cycle()
        log('tick(persistence): open={open} promoted={p} waiting={w}'.format(
            open=summary['open'], p=len(summary['promoted']),
            w=len(summary['waiting'])))
    else:
        summary = run_backstop()
        log('tick(backstop): open={open} promoted={p}'.format(
            open=summary['open'], p=len(summary['promoted'])))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
