#!/usr/bin/env python3
"""larry_alerts.py — shared append-only alert queue for watchdog + sentinel.

Infra-monitoring scripts post alerts here when something should reach Larry's
phone. Beacon's Telegram bot polls the queue on its periodic sweep (alongside
the approval-reminder sweep) and DMs each new entry to every authorized chat.

Design notes:

  - Queue file: ~/agents/blackboard/larry-alerts.jsonl (append-only, UTF-8).
    One JSON object per line.
  - Per-subject cooldown gating: separate dirs for critical (10 min) and
    warning (60 min). The cooldown key is `source:subject` (subject-specific
    so e.g. "bots:mirror" and "bots:forge" each have their own bucket; M3
    fix from the design review).
  - Every appended alert row carries `tier` (NOW/SOON/FYI) and `tier_source`
    ("translation" when the alert-translations entry classified it,
    "default" for the conservative FYI fallback), resolved at WRITE time by
    the same `resolve_tier` the DM glyph header uses. Stamped at emit because
    the (source, subject) → tier join is not reliably recoverable later — a
    source whose translation block spans several tiers is ambiguous once the
    row is on disk — and retention trims the queue at 14 days.
  - The bot's offset file lives at ~/agents/state/beacon-alerts-offset.txt
    and is read/written by the bot — this module never advances it. Per-line
    ack on the bot side ensures at-least-once delivery (M2 fix).

Adapted from D3.5-prep design (2026-05-12).

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import atomic_io
import file_lock
from test_isolation_guard import refuse_under_test


def _decision_key_for(task_id: Optional[str], pr_url: Optional[str]) -> Optional[str]:
    """Canonical cross-store join key for an alert (Phase 2 Change A). Lazy +
    fail-safe import — decision_identity is pure, but stamping must never turn a
    fire-and-forget alert append into an exception."""
    try:
        from decision_identity import canonical_decision_key
        return canonical_decision_key(task_id, pr_url)
    except Exception:  # noqa: BLE001 — stamping is best-effort, never fatal
        return None


AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or Path.home() / 'agents')
ALERTS_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
COOLDOWN_ROOT = AGENTS_ROOT / 'state' / 'alert-cooldown'
OFFSET_FILE = AGENTS_ROOT / 'state' / 'beacon-alerts-offset.txt'
# The two line-index consumer cursors that resolve_alert must keep consistent
# when it removes lines (mirrors larry_alerts_retention's offset bookkeeping):
# beacon (OFFSET_FILE above) + medic. Both are ABSOLUTE last-delivered+1 counts.
MEDIC_OFFSET_FILE = AGENTS_ROOT / 'state' / 'medic-alerts-offset.txt'

# PR-E2 (#16): larry_alerts_retention rewrites this file (read snapshot →
# os.replace with survivors). An append that lands between the snapshot and the
# replace is silently dropped. Every appender below takes the SAME sidecar flock
# the retention RMW takes, so an append is either fully inside the snapshot (kept)
# or strictly after the replace (lands in the new file) — never lost.
#
# The wait is bounded so a fire-and-forget appender can never hang a daemon
# thread if retention (or a wedged process) holds the lock: on timeout we fall
# back to a plain append — strictly no worse than the pre-lock behaviour, and the
# retention RMW window is sub-second to a few seconds on a daily bounded prefix.
_APPEND_LOCK_TIMEOUT_SEC = float(
    os.environ.get('OURLIBERTY_ALERTS_APPEND_LOCK_TIMEOUT', '10') or '10'
)

# Translation layer (stopgap until Pulse cycle upgrade ships healer-alert
# triage; see docs/operating-manual.md Part II #68). Lookup by (source,
# subject) at format_dm time; matched alerts get a plain-language layered
# render; unmatched alerts get the raw body + a "[no translation]" footer
# so silence-on-unmatched is impossible.
TRANSLATIONS_FILE = Path(__file__).resolve().parent.parent / 'config' / 'alert-translations.json'

# Significance table (fix-first/notify-on-outcome routing, 2026-06-03). Decides
# whether a SUCCESSFUL heal earns a `closure` DM (significant subject) or routes
# `digest` (routine). Subject-prefix keyed; default = routine. Pulse-tunable.
SIGNIFICANCE_FILE = Path(__file__).resolve().parent.parent / 'config' / 'alert-significance.json'

# Graduation registry (alert-pipeline-rework B4). The incremental-migration
# control surface for the hybrid DM gate: a source listed in `migrated_sources`
# has its routine (non-critical) alerts defaulted to its migrated route (`hold`)
# instead of the global escalate default. Only `outbox-notifier` is migrated
# initially (S1); P3 names the rest. Source-keyed exact match; Pulse-tunable.
GRADUATION_FILE = Path(__file__).resolve().parent.parent / 'config' / 'alert-graduation-registry.json'

CRITICAL_COOLDOWN_SEC = 10 * 60       # 10 min — terse and load-bearing
WARNING_COOLDOWN_SEC = 60 * 60        # 60 min — Larry's Dial 3 pick
INFO_COOLDOWN_SEC = 6 * 60 * 60       # 6 hr — routine housekeeping; longest window

VALID_SEVERITIES = ('info', 'warning', 'critical')

# Routing destinations (fix-first / notify-on-outcome, 2026-06-03). Orthogonal
# to severity (severity still buckets cooldown; route decides destination):
#   escalate — DM Larry now. DEFAULT (fail-loud: a missed migration over-notifies
#              rather than silently dropping). Carries the action Larry takes.
#   closure  — DM Larry one line: was broken, fixed it, no action needed. Only for
#              SIGNIFICANT successful heals.
#   digest   — NOT DM'd; the daily CEO digest surfaces it as a self-healed line.
#              Routine successful heals.
#   hold     — NOT DM'd at read-time (the bot skips it, same as digest), but UNLIKE
#              digest it is NOT a closed/self-healed line — it is a pending judgment
#              the hybrid DM gate (alert-pipeline-rework B1) is deliberately holding
#              back from Larry's phone. It still lands on the dashboard via the
#              shipper. A held line is promoted into a DM by APPENDING a fresh
#              escalate line (B3) — either by Pulse Check 0, the persistence rule
#              (B5, after N cycles), or the Pulse-independent backstop (B6, after
#              30 min). A `critical` alert is NEVER held: append_alert forces
#              escalate for critical (B2), and the bot re-checks severity at read
#              time so a mis-routed critical hold still DMs.
VALID_ROUTES = ('escalate', 'closure', 'digest', 'hold')
DEFAULT_ROUTE = 'escalate'


# ---------- cooldown machinery ----------


# Cap the sanitized filename well under the common 255-byte filesystem
# NAME_MAX, leaving headroom for the `.tmp.<pid>` suffix `silence()` adds during
# its atomic write (and the 11-byte hash suffix below). A key longer than this
# is truncated and disambiguated by the hash, so an over-long fingerprint can't
# make `open()`/`write` raise (which would read as a silence-write failure).
_MAX_SAFE_KEY_LEN = 200


def _safe_key(key: str) -> str:
    """Filesystem-safe form of a cooldown/silence key.

    Sanitization (mapping every disallowed char to `_`) is lossy: distinct raw
    keys like `forge:a/b` and `forge:a b` both collapse to `forge:a_b`, so one
    alert's cooldown/silence would suppress the other. To keep the mapping
    injective, append a short stable hash of the RAW key whenever sanitization
    changed something OR the key is over-length. Keys that are already
    filesystem-safe AND short (the common `source:subject` case where subject is
    a task-id) are returned unchanged, so there is no churn for existing
    cooldown/silence files.
    """
    safe = ''.join(c if (c.isalnum() or c in '-._:') else '_' for c in key)
    if safe == key and len(safe) <= _MAX_SAFE_KEY_LEN:
        return safe
    digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]
    if len(safe) > _MAX_SAFE_KEY_LEN:
        safe = safe[:_MAX_SAFE_KEY_LEN]
    return f'{safe}.{digest}'


def _cooldown_path(severity: str, key: str) -> Path:
    return COOLDOWN_ROOT / severity / _safe_key(key)


def _cooldown_window(severity: str) -> int:
    if severity == 'critical':
        return CRITICAL_COOLDOWN_SEC
    if severity == 'info':
        return INFO_COOLDOWN_SEC
    return WARNING_COOLDOWN_SEC


def in_cooldown(severity: str, key: str, now: Optional[float] = None) -> bool:
    """Public-ish: True if the (severity, key) pair is still inside its window."""
    path = _cooldown_path(severity, key)
    if not path.exists():
        return False
    try:
        age = (now or time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age < _cooldown_window(severity)


def _mark_cooldown(severity: str, key: str) -> None:
    path = _cooldown_path(severity, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


# ---------- durable silence layer (Medic self-silence backstop) ----------
#
# Distinct from cooldown. Cooldown is a short, self-expiring throttle (10/60
# min) that every repeat alert refreshes -- it slows a recurring alert but
# never stops it. A *silence* is a deliberate, durable suppression written by
# Medic (scripts/medic_actions.py) AFTER its read-only investigation confirmed
# a specific fingerprint is a benign false positive -- e.g. a forge-no-pr
# alert whose build already shipped under a re-keyed branch (the 2026-06-04
# case). While a silence is in force `append_alert` drops the matching alert
# with NO DM, so Larry never sees attempt N+1 of a stall that is already
# resolved. Silences are keyed by the same `source:subject` cooldown key, are
# reversible (delete the file / call `unsilence` to restore), and may carry an
# optional TTL. `ttl_sec=None` means "until manually cleared" -- the correct
# default for task-id-specific subjects, which can never legitimately recur.

SILENCE_ROOT = AGENTS_ROOT / 'state' / 'alert-silenced'
# G8 silence auditor: a silence is invisible by design — while it holds,
# `append_alert` drops the matching alert with no DM, so nobody sees how much it
# is suppressing. A silence written for a transient false positive that has
# since become a REAL recurring signal would keep swallowing it forever, and the
# only symptom is silence. To make that observable, every suppression bumps a
# per-key counter here (sidecar to the silence file); the standing auditor
# (scripts/silence_file_auditor.py) reads both to report each silence's key,
# age, and how many alerts it has eaten.
SILENCE_COUNTER_ROOT = AGENTS_ROOT / 'state' / 'alert-silenced-counts'


def _silence_path(key: str) -> Path:
    return SILENCE_ROOT / _safe_key(key)


def _silence_counter_path(key: str) -> Path:
    return SILENCE_COUNTER_ROOT / _safe_key(key)


def silence_suppressed_count(key: str) -> int:
    """How many alerts a silence for `key` has dropped (0 if none/unreadable).
    Fail-safe: a missing or corrupt counter reads as 0, never raises."""
    try:
        with open(_silence_counter_path(key), encoding='utf-8') as f:
            data = json.load(f)
        return int(data.get('count', 0)) if isinstance(data, dict) else 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _note_silenced_suppression(key: str, now: Optional[float] = None) -> None:
    """Best-effort bump of the per-key suppressed counter at the moment a silence
    drops an alert. Read-modify-write under the shared sidecar flock so
    concurrent producers don't lose increments; degrades to a no-op on any error
    (this is telemetry — it must never turn a fire-and-forget append into a
    raise, and a lost tick is strictly better than a dropped/duplicated alert)."""
    path = _silence_counter_path(key)
    try:
        SILENCE_COUNTER_ROOT.mkdir(parents=True, exist_ok=True)
        lock_path = file_lock.sidecar_lock_path(path)
        try:
            with file_lock.exclusive_lock(lock_path, timeout=_APPEND_LOCK_TIMEOUT_SEC):
                _bump_counter_file(path, key, now)
        except file_lock.LockTimeout:
            _bump_counter_file(path, key, now)
    except OSError:
        return


def _bump_counter_file(path: Path, key: str, now: Optional[float]) -> None:
    count = 0
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            count = int(data.get('count', 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        count = 0
    ts = datetime.now(timezone.utc).isoformat()
    atomic_io.atomic_write_json(
        path,
        {'key': key, 'count': count + 1, 'last_suppressed': ts},
    )


def is_silenced(key: str, now: Optional[float] = None) -> bool:
    """True if `key` has an active silence (no TTL, or TTL not yet expired)."""
    path = _silence_path(key)
    if not path.exists():
        return False
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        # A corrupt/unreadable silence file must NOT suppress permanently.
        # Failing-quiet here means a possibly-real, recurring alert is silently
        # dropped forever with no recovery path -- the irreversible-bad
        # direction. Fail LOUD instead: let the alert through so Larry (or
        # Medic) sees it and can re-silence cleanly. `silence()` writes
        # atomically, so a corrupt file is an external/partial-write anomaly,
        # not a normal state.
        return False
    if not isinstance(data, dict):
        return False
    until = data.get('until')
    # `until is None` => permanent silence (until manually cleared). A numeric
    # `until` is an absolute epoch deadline; 0 (or any past value) is simply
    # expired -- NOT permanent (the old `until in (None, 0)` treated a 0
    # deadline as permanent, which made an immediate-expiry silence eternal).
    if until is None:
        return True
    try:
        return (now if now is not None else time.time()) < float(until)
    except (TypeError, ValueError):
        # A non-numeric, non-None `until` is malformed -> don't suppress.
        return False


def silence(key: str, reason: str = '', ttl_sec: Optional[float] = None,
            by: str = 'medic', now: Optional[float] = None) -> bool:
    """Write a durable silence for `key`. Returns True on success, never
    raises. `ttl_sec=None` -> permanent (until the file is removed);
    `ttl_sec=0` -> expires immediately (a no-op silence), NOT permanent."""
    path = _silence_path(key)
    base = now if now is not None else time.time()
    tmp = Path(f'{path}.tmp.{os.getpid()}')
    try:
        # ttl_sec=0 must stay distinct from ttl_sec=None: `if ttl_sec` treated
        # 0 as falsy -> None -> a permanent silence, so a caller asking for a
        # zero-duration silence got an eternal one. Gate on `is not None`.
        until = None if ttl_sec is None else base + float(ttl_sec)
        record = {
            'key': key,
            'reason': reason,
            'by': by,
            'ts': datetime.now(timezone.utc).isoformat(),
            'until': until,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: a crash/partial write mid-`json.dump` would leave a
        # corrupt silence file; with the fail-loud `is_silenced` that only
        # costs a re-fire, but atomic replace avoids even that and keeps a
        # legitimate silence intact.
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def unsilence(key: str) -> bool:
    """Remove a silence (un-suppress). Returns True iff a file was removed."""
    try:
        _silence_path(key).unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


# ---------- writer side ----------


def _locked_append(line: str) -> bool:
    """Append one already-serialized ``line`` (including its trailing newline) to
    the alerts file under the shared retention flock (PR-E2 #16).

    The flock excludes ``larry_alerts_retention``'s read-snapshot→``os.replace``
    rewrite for the duration of the append, so a concurrent retention pass can
    never drop this line: the append is either captured in the snapshot or lands
    in the freshly-rewritten file. ``O_APPEND`` still guarantees the write itself
    doesn't tear.

    Never raises — returns False on any ``OSError`` so callers can fire-and-forget.
    On a bounded-wait timeout (a wedged lock holder) or a platform without
    ``fcntl``, fall back to a plain append: strictly no worse than the pre-lock
    behaviour, reintroducing the original race only for this one line and only
    while the lock stays contended past the timeout.
    """
    try:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_path = file_lock.sidecar_lock_path(ALERTS_FILE)
        try:
            with file_lock.exclusive_lock(
                lock_path, timeout=_APPEND_LOCK_TIMEOUT_SEC,
            ):
                with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
                    f.write(line)
        except file_lock.LockTimeout:
            with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
    except OSError:
        return False
    return True


def append_alert(
    source: str,
    severity: str,
    message: str,
    subject: Optional[str] = None,
    suggested_action: Optional[str] = None,
    route: Optional[str] = None,
    decision_key: Optional[str] = None,
    task_id: Optional[str] = None,
    pr_url: Optional[str] = None,
    needs_larry: bool = False,
) -> bool:
    """Append one alert if not in cooldown.

    Returns True if appended, False if suppressed (cooldown) or if the
    underlying append failed. Never raises — callers can fire-and-forget.

    Args:
        source: usually 'watchdog' or 'sentinel'.
        severity: 'info', 'warning', or 'critical'. An 'info' alert defaults to
            the digest lane (no DM) unless route is given explicitly.
        message: short human-readable description for the DM body.
        subject: optional dedup-key suffix. Recommended — without it, all
            alerts from one source share a single cooldown bucket.
        suggested_action: optional shell command the operator can run.
        route: 'escalate' (DM now), 'closure' (DM a one-line self-healed
            confirmation), 'digest' (no DM; surfaced in the daily CEO digest),
            or 'hold' (no DM at read-time, but a pending judgment the hybrid DM
            gate is holding back — promoted to a DM later by an appended escalate
            line; see B1/B3). Defaults to 'digest' for severity=='info' and
            'escalate' otherwise. A 'critical' severity FORCES 'escalate' (B2),
            overriding any supplied route — a critical can never be held/digested.
            An unknown value falls back to 'escalate' so a mistake over-notifies
            rather than silently drops.
        needs_larry: producer-side "needs-you" classification (approval-sync
            Phase 3a §3a.2). Default FALSE. Only a genuinely irreversible /
            ambiguous emitter — one whose alert Larry alone must act on — sets
            this True; the ~150 healer alerts leave it False so they never reach
            the "Needs You" surface (they stay healer-owned in the Healers tab).
            This is the producer-side anti-toil gate: a read-time "has an action"
            filter is a no-op because the benign healer alerts all carry a
            `suggested_action`, so the discriminator must be stamped where the
            meaning is known — here. Only stamped onto the record when True (an
            absent field reads as False downstream), keeping the queue lean.
    """
    refuse_under_test('larry-alerts')
    if severity not in VALID_SEVERITIES:
        # Don't raise — surface as a no-op with a stderr hint.
        try:
            import sys as _sys
            _sys.stderr.write(
                f'[larry_alerts] invalid severity={severity!r}, dropping\n'
            )
        except Exception:
            pass
        return False
    # Route default depends on severity: routine `info` defaults to the digest
    # lane (no DM); everything else defaults to escalate (fail-loud). A caller
    # that passes an explicit route overrides this — an info emitter that wants
    # a DM passes route='escalate' itself.
    #
    # B4 (alert-pipeline-rework): a GRADUATED source's routine alerts default to
    # its migrated route (`hold`) instead — the incremental-migration control
    # surface. This applies only to the default (route is None); an explicit
    # caller route still wins, and the critical-forces-escalate guard below (B2)
    # still fires, so a migrated source's `critical` alerts always DM.
    if route is None:
        graduated = graduated_route(source)
        if graduated is not None:
            route = graduated
        else:
            route = 'digest' if severity == 'info' else DEFAULT_ROUTE
    # B2 (alert-pipeline-rework): a `critical` alert ALWAYS DMs. Force escalate
    # regardless of any caller-supplied route, so a caller can never accidentally
    # `hold` or `digest` a critical and silence it. This is the emit-time half of
    # the guarantee; the bot's read-time `severity != 'critical'` check in the
    # hold/digest skip branch is the second line of defense.
    if severity == 'critical':
        route = 'escalate'
    if route not in VALID_ROUTES:
        # Fail-loud: an invalid route degrades to escalate (a DM), never to a
        # silent drop.
        route = DEFAULT_ROUTE
    key = f'{source}:{subject}' if subject else source
    # Durable silence (Medic-confirmed false positive) takes precedence over
    # the short cooldown: a silenced fingerprint never reaches Larry. G8: record
    # the suppression so the standing auditor can tell how much this silence eats
    # (a silence over a now-real signal is otherwise invisible).
    if is_silenced(key):
        _note_silenced_suppression(key)
        return False
    if in_cooldown(severity, key):
        return False
    # Stamp the resolved operator tier at WRITE time. The (source, subject) →
    # tier join is not reliably recoverable after the fact — a source whose
    # translation block holds several subjects with different tiers is
    # ambiguous once the row is on disk — and the queue is trimmed at 14 days,
    # so an unstamped day is a day of tier history permanently gone. Same
    # `resolve_tier` the DM glyph uses, so glyph and stamp always agree.
    tier, tier_source = resolve_tier_for(source, subject)
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'severity': severity,
        'message': message,
        'route': route,
        'tier': tier,
        'tier_source': tier_source,
    }
    if subject:
        record['subject'] = subject
    if suggested_action:
        record['suggested_action'] = suggested_action
    # Phase 3a §3a.2: stamp the producer-side needs-you classification. Only
    # when True (absent == False downstream) so the queue and the shipped
    # chain_event payload stay lean; the "Needs You" read query gates on this.
    if needs_larry:
        record['needs_larry'] = True
    # Change A: stamp the canonical decision key onto escalate lines so the
    # resolve fan-out can retract this alert by key (resolve_alert_by_decision_key)
    # when its decision resolves elsewhere — keeping the alert feed and its
    # already-shipped chain_event row in agreement (spec §2 Change B step 4).
    # Only escalate lines are retractable, so only they need the key. The caller
    # may pass `decision_key` directly, or `task_id`/`pr_url` to derive it.
    if route == 'escalate':
        dk = decision_key or _decision_key_for(task_id, pr_url)
        if dk:
            record['decision_key'] = dk
    # O_APPEND is atomic for writes <= PIPE_BUF (4096 on Linux) so the line
    # itself never tears; the shared flock (see _locked_append) additionally
    # excludes the retention rewrite so the line is never lost.
    if not _locked_append(json.dumps(record, ensure_ascii=False) + '\n'):
        return False
    _mark_cooldown(severity, key)
    return True


# ---------- held-alert promotion (alert-pipeline-rework B3) ----------

# Subject marker appended to a promoted held alert's subject. The promotion line
# MUST land in a distinct cooldown bucket from the original held line (whose key
# is `source:subject`), or the original line's cooldown would swallow it and the
# promotion would never DM. The marker also makes promotions visually distinct on
# the dashboard / in the queue.
PROMOTION_SUBJECT_SUFFIX = '::promoted'


def append_promotion(
    source: str,
    severity: str,
    message: str,
    subject: str,
    suggested_action: Optional[str] = None,
    reason: str = 'promoted',
) -> bool:
    """Append a fresh ``route='escalate'`` line that promotes a previously-held
    alert into a DM (alert-pipeline-rework B3).

    Promotion is APPEND-only: the bot's offset cursor is forward-only, so the
    only way to turn a ``hold`` line into a DM is to append a NEW escalate line —
    rewriting the held line in place is invisible to the bot. The promotion line
    carries a DISTINCT subject marker (``<subject>::promoted``) so it lands in
    its own cooldown bucket and is never swallowed by the original held line's
    cooldown, plus ``promotion: True`` and ``promoted_from`` (the original
    ``source:subject`` fingerprint) so the promote-once machinery (B5/B6) can
    detect from the queue that this fingerprint was already promoted.

    Cooldown and silence gates are intentionally bypassed (a direct locked
    append, not ``append_alert``): the caller — the persistence rule (B5) or the
    Pulse-independent backstop (B6) — has already confirmed the hold is unresolved
    (not silenced, not already promoted), and a promotion must always reach Larry.

    Never raises — returns True on success, False on a write failure, so callers
    can treat a False as "leave un-promoted, retry next cycle".
    """
    refuse_under_test('larry-alerts')
    promoted_subject = f'{subject}{PROMOTION_SUBJECT_SUFFIX}'
    # Resolve against the SUFFIXED subject the record actually carries — that
    # is what the DM renderer looks up, and translate_alert's prefix-strip
    # recovers the original entry — so glyph and stamp agree here too.
    tier, tier_source = resolve_tier_for(source, promoted_subject)
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'severity': severity,
        'message': message,
        'route': 'escalate',
        'tier': tier,
        'tier_source': tier_source,
        'subject': promoted_subject,
        'promotion': True,
        'promoted_from': f'{source}:{subject}',
        'promotion_reason': reason,
    }
    if suggested_action:
        record['suggested_action'] = suggested_action
    return _locked_append(json.dumps(record, ensure_ascii=False) + '\n')


# ---------- alert retraction (resolve a stale escalate after out-of-band fix) ----------


def _line_matches_resolution(rec: dict, key: str) -> bool:
    """True iff `rec` is a pending escalate alert whose `source:subject` == key.

    Only `route == 'escalate'` lines are retractable — a closure/digest
    self-healed line for the same key is harmless and must be left in place. A
    legacy line with no `route` rendered as escalate (DEFAULT_ROUTE), so a
    missing route counts as escalate too. Notification / approval-request
    records (which carry a `kind` and no severity) never match: they are 1:1
    with a task and are not the infra-noise this retracts."""
    if not isinstance(rec, dict):
        return False
    if rec.get('kind') in ('notification', 'approval_request'):
        return False
    if rec.get('route', DEFAULT_ROUTE) != 'escalate':
        return False
    source = rec.get('source')
    if not isinstance(source, str):
        return False
    subject = rec.get('subject')
    rec_key = f'{source}:{subject}' if subject else source
    return rec_key == key


def _line_matches_decision_key(rec: dict, key: str) -> bool:
    """True iff `rec` carries a stamped `decision_key` == `key` (exact match).

    The A-leg predicate for resolve_alert_by_decision_key. Retracts both escalate
    alert lines and `approval_request` records stamped with the key — anything
    whose decision resolved elsewhere. Exact-key only: a mis-join would silently
    drop an unrelated decision's alert (spec §6)."""
    if not isinstance(rec, dict) or not key:
        return False
    return rec.get('decision_key') == key


def _read_line_offset(path: Path) -> int:
    """Read an absolute line-index offset (next-to-deliver). 0 if missing /
    unreadable — mirrors read_offset but for an arbitrary consumer file."""
    try:
        return int(path.read_text().strip() or '0')
    except (FileNotFoundError, OSError, ValueError):
        return 0


def resolve_alert(
    key: str,
    consumer_offset_files: Optional[list] = None,
    alerts_file: Optional[Path] = None,
) -> int:
    """Retract pending `escalate` alert line(s) matching `key` from the queue.

    The append-only queue has no retraction primitive, so when a drift resolves
    out-of-band the original 🔴 escalate line stays in larry-alerts.jsonl
    forever (a producer's reconciliation GC prunes only its own dedup state,
    never the emitted alert). `resolve_alert` removes the stale line(s) AND
    keeps the line-index consumer cursors consistent.

    `key` is the `source:subject` cooldown key (or bare `source`). Returns the
    number of lines removed (0 = no-op, including the no-match and error cases).

    Cursor bookkeeping (load-bearing): beacon + medic offsets are ABSOLUTE line
    counts (next line to deliver). Removing a line at original index i shifts
    every later line down by one, so each cursor decrements by the count of
    removed lines whose original index was < that cursor — otherwise the next
    real alert is silently skipped (the cursor would point one line too far).
    A removed line at index >= a cursor was still undelivered, so that cursor is
    untouched: the retraction merely guarantees it is never delivered.

    The whole read → backup → cursor-decrement → rewrite runs under the SAME
    sidecar flock every appender (`_locked_append`) and the retention rewrite
    take, so it can never race an append or a retention pass — a concurrent
    append lands strictly after the rewrite. Cursors are decremented BEFORE the
    file rewrite so a crash between the two leaves them pointing into the intact
    file (re-deliver, never skip — see _resolve_alert_locked). A full backup of
    the pre-rewrite file is written first (recoverable, mirroring retention's
    archive-before-rewrite).

    Never raises — returns 0 on any error so callers can fire-and-forget.
    """
    refuse_under_test('larry-alerts')
    af = alerts_file if alerts_file is not None else ALERTS_FILE
    if consumer_offset_files is None:
        consumer_offset_files = [OFFSET_FILE, MEDIC_OFFSET_FILE]
    removed: list = []
    try:
        if not af.exists():
            return 0
        lock_path = file_lock.sidecar_lock_path(af)
        try:
            with file_lock.exclusive_lock(
                lock_path, timeout=_APPEND_LOCK_TIMEOUT_SEC,
            ):
                removed = _resolve_alert_locked(
                    lambda rec: _line_matches_resolution(rec, key),
                    af, consumer_offset_files,
                )
        except file_lock.LockTimeout:
            # A wedged lock holder must not strand the retraction forever, but
            # rewriting the file unlocked could race a concurrent append. The
            # safe degrade is a no-op: the stale line survives to the next call,
            # strictly better than risking a lost append.
            return 0
    except OSError:
        return 0
    # §3a.2 retraction gap: clear the already-shipped chain_event rows for the
    # lines we just removed. Runs OUTSIDE the flock — a Supabase stall must never
    # serialize every appender behind this best-effort network write.
    _retract_shipped_alert_events(removed)
    return len(removed)


def retract_with_standdown(
    key: str,
    standdown_message: str,
    subject: Optional[str] = None,
) -> int:
    """Retract a pending red alert AND emit a visible closure stand-down for it.

    `resolve_alert` silently removes the stale escalate line(s), but it cannot
    un-send the 🔴 DM already on Larry's phone. A retraction of something Larry
    actually SAW must itself be visible — otherwise a wrongful retraction is an
    invisible event (the red simply vanishes with no trail to dispute). This
    helper makes every real retraction AUDITABLE: it removes the line(s) via
    `resolve_alert(key)`, and ONLY when that removed >= 1 pending escalate line
    (proof a real 🔴 had been delivered) appends exactly one closure stand-down
    line via `append_alert(severity='info', route='closure', ...)`.

    On a 0-removal no-match — the drift never paged, so there is nothing to
    stand down — it appends nothing and returns 0. This mirrors the
    install-drift exemplar (`heal_systemd_install_drift`: retract, then a
    one-line closure DM gated on `removed`), generalized so any positive-clear
    detector can adopt it.

    `key` is the `source:subject` cooldown key handed to `resolve_alert`.
    `subject` is the closure line's own dedup suffix (defaults to `key` so the
    closure is scoped to the same incident). Returns the number of lines
    removed (0 = no-op). Never raises — fire-and-forget, matching
    `resolve_alert` and `append_alert`.
    """
    try:
        removed = resolve_alert(key)
        if removed:
            # A real 🔴 was in the queue and is now retracted. Emit one closure
            # stand-down so the alert Larry saw visibly closes (and a wrongful
            # retraction surfaces as a disputable DM, never a silent vanish).
            append_alert(
                source='alert-retraction',
                severity='info',
                message=standdown_message,
                subject=subject if subject is not None else key,
                route='closure',
            )
        return removed
    except Exception:
        # resolve_alert / append_alert are both contractually no-raise, so this
        # is defense in depth — but the fire-and-forget contract must hold hard,
        # never propagating into a caller's clear-branch tick.
        return 0


def resolve_alert_by_decision_key(
    key: str,
    consumer_offset_files: Optional[list] = None,
    alerts_file: Optional[Path] = None,
) -> int:
    """Retract alert line(s) whose stamped `decision_key` == `key` (the A-leg of
    decision_resolve.resolve_decision, spec §2 Change B step 4).

    Identical cursor/backup/locking machinery to `resolve_alert` — only the
    match predicate differs (`_line_matches_decision_key` instead of the
    `source:subject` matcher). Retracts both escalate alert lines and
    `approval_request` records that carry the key, so the alert feed agrees with
    the already-shipped chain_event row once a decision resolves. Exact-key
    only; never raises (returns 0 on no-match / lock-timeout / error)."""
    refuse_under_test('larry-alerts')
    if not key:
        return 0
    af = alerts_file if alerts_file is not None else ALERTS_FILE
    if consumer_offset_files is None:
        consumer_offset_files = [OFFSET_FILE, MEDIC_OFFSET_FILE]
    removed: list = []
    try:
        if not af.exists():
            return 0
        lock_path = file_lock.sidecar_lock_path(af)
        try:
            with file_lock.exclusive_lock(
                lock_path, timeout=_APPEND_LOCK_TIMEOUT_SEC,
            ):
                removed = _resolve_alert_locked(
                    lambda rec: _line_matches_decision_key(rec, key),
                    af, consumer_offset_files,
                )
        except file_lock.LockTimeout:
            return 0
    except OSError:
        return 0
    # §3a.2 retraction gap (also idempotent with decision_resolve's C-leg
    # clear_decision): clear the shipped chain_event rows for the removed lines,
    # OUTSIDE the flock. Harmless if the fan-out already cleared them by key.
    _retract_shipped_alert_events(removed)
    return len(removed)


def _resolve_alert_locked(
    match_fn, af: Path, consumer_offset_files: list,
) -> list:
    """The locked body of resolve_alert (see its docstring). MUST run under the
    alerts-file sidecar flock. `match_fn(rec) -> bool` selects the lines to
    retract.

    Returns the list of removed record dicts (the parsed matched lines). The
    public callers use `len(...)` for the removed count AND hand the records to
    `_retract_shipped_alert_events` — OUTSIDE the flock — to clear the matching
    already-shipped chain_event rows (§3a.2 retraction gap). Returning the
    records (not just a count) is what lets the caller correlate each removed
    line to its shipped row without re-reading the rewritten file."""
    with open(af, encoding='utf-8') as f:
        lines = f.readlines()

    removed_indices: list = []
    removed_recs: list = []
    survivors: list = []
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        matched = False
        rec = None
        if stripped:
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                rec = None
            if rec is not None and match_fn(rec):
                matched = True
        if matched:
            removed_indices.append(idx)
            removed_recs.append(rec)
        else:
            survivors.append(raw)

    if not removed_indices:
        return []

    # Backup the pre-rewrite file first (recoverable) — same backup-before-
    # rewrite shape as retention's archive step.
    backup_path = af.parent / (af.name + '.resolve.bak')
    try:
        atomic_io.atomic_write_text(backup_path, ''.join(lines))
    except OSError:
        # A failed backup must not block the retraction (fire-and-forget);
        # proceed with rewrite.
        pass

    # Crash-safe ORDERING (decrement cursors BEFORE the file rewrite): the
    # cursor decrements and the file rewrite are separate disk ops, so a crash
    # between them must not strand a consumer. Decrementing FIRST means any
    # crash-intermediate state leaves the cursors pointing into the STILL-INTACT
    # (un-rewritten) file, so a consumer re-reads — at worst re-delivering an
    # already-delivered line (a duplicate DM, including possibly the stale alert
    # we are retracting), which is the safe at-least-once direction. The reverse
    # order would leave cursors stale-HIGH against a shortened file and silently
    # SKIP the next real alert — the exact failure this primitive exists to
    # prevent. (retention needs a journal for the same rewrite+decrement because
    # it is a bulk idempotent op; this targeted single rewrite gets crash-safety
    # for free from fail-safe ordering.) A smaller offset is always skip-safe.
    #
    # This shares the same unlocked-consumer race profile as retention: beacon /
    # medic read + advance their offset without this flock, so a removal that
    # shifts a line a consumer is mid-delivering can still race. In practice the
    # retracted line is an already-DELIVERED stale alert (drift resolved
    # out-of-band well after the DM), so it sits far below both cursors and the
    # decrement is the common, benign case.
    for off_path in consumer_offset_files:
        cur = _read_line_offset(off_path)
        before = sum(1 for i in removed_indices if i < cur)
        if before:
            atomic_io.atomic_write_text(off_path, str(max(0, cur - before)))

    # Rewrite the live file with the survivors LAST. The atomic_io rename swaps
    # the inode, which the third consumer (chain_event_shipper, a BYTE cursor)
    # detects as a rotation and re-reads from 0; its deterministic event_id +
    # ignore_duplicates upsert absorbs the re-read, so — unlike beacon/medic — it
    # needs no explicit cursor adjustment here.
    atomic_io.atomic_write_text(af, ''.join(survivors))

    return removed_recs


def _retract_shipped_alert_events(removed_recs: list, *, clear_fn=None) -> int:
    """Clear the shipped `larry_alert` chain_event rows for retracted lines.

    The chain_event_shipper polls larry-alerts.jsonl and ships each line as a
    `larry_alert` row, keyed by `chain_event_shipper.alert_event_task_id(rec)`.
    When `resolve_alert` removes a line — e.g. a healer auto-fix resolving drift
    out-of-band — that already-shipped row otherwise keeps reading as unread
    (`read_at IS NULL`) FOREVER: the shipper only re-ships the surviving lines
    and never emits a retraction, so the alert feed and the chain_events read
    model silently drift apart (§3a.2 retraction gap — the bug that made
    auto-fixed alerts render live on the dashboard). This mirrors the removal
    into Supabase by clearing the correlated row via the SHARED shipper-key
    helper, so the clear can never drift from the stamp.

    Correlation + column are kept strictly aligned with how the row was stamped:
    a line the shipper keyed by task_id/subject is cleared on the `task_id`
    column; the degenerate line the shipper keyed by nothing (no
    task_id/subject/intent) but that still carries a `decision_key` in its
    payload is cleared on the `decision_key` column only. `clear_larry_alert`
    matches ONE column, so we never fire a task_id match on a decision_key value
    (a PR-coordinate key is reused as both, and that cross-match would wrongly
    clear an unrelated live alert — worse than a missed join).

    Caveats this does NOT cover (documented, low-risk, both strictly better than
    the pre-fix always-live state): (1) a clear that fails on a Supabase blip is
    swallowed and the row stays live — there is NO larry_alert-specific healer
    backstop (heal_stale_approvals only reconciles approval_request/clarify), so
    the miss persists until the same key is resolved again; (2) a resolve that
    races AHEAD of the shipper's first ship of that line clears nothing (no row
    yet) and the shipper then strands the row — negligible in practice because a
    retraction fires when drift resolves out-of-band, long after the line was
    DMed and thus shipped.

    Best-effort, fire-and-forget — never raises. Its callers release the alerts
    flock first, so this network write can't serialize appenders. Under test or
    without Supabase creds, the clear degrades to a no-op. Returns the total
    rows cleared. `clear_fn` is a test seam (defaults to the real clear)."""
    if not removed_recs:
        return 0
    if clear_fn is None:
        try:
            import chain_event_emit
        except Exception:  # noqa: BLE001 — retraction must not crash the resolve
            return 0
        clear_fn = chain_event_emit.clear_larry_alert
    try:
        import chain_event_shipper
    except Exception:  # noqa: BLE001 — retraction must not crash the resolve
        return 0
    # Build the (key, column) targets, deduped so N lines sharing a key issue
    # ONE clear. Prefer the shipper's task_id; fall back to the payload
    # decision_key only when the shipper had no task_id to stamp.
    targets: list = []
    seen: set = set()
    for rec in removed_recs:
        if not isinstance(rec, dict):
            continue
        tid = chain_event_shipper.alert_event_task_id(rec)
        if tid:
            target = (tid, 'task_id')
        elif rec.get('decision_key'):
            target = (rec['decision_key'], 'decision_key')
        else:
            continue
        if target in seen:
            continue
        seen.add(target)
        targets.append(target)
    total = 0
    for key, by in targets:
        try:
            total += int(clear_fn(key, by=by) or 0)
        except Exception:  # noqa: BLE001 — a failed clear leaves the row for a
            pass           # future resolve of the same key (no crash into caller)
    return total


# ---------- significance gate + route classification (fix-first routing) ----------


_SIGNIFICANCE_CACHE: Optional[dict] = None
_SIGNIFICANCE_MTIME: Optional[float] = None


def _load_significance() -> dict:
    """Read config/alert-significance.json, reloading on mtime change (same
    long-running-process rationale as the translations cache). Returns the
    parsed dict, or {} if the file is missing/malformed — in which case every
    subject is treated as routine (the conservative default: routine heals go
    to the digest, not a DM)."""
    global _SIGNIFICANCE_CACHE, _SIGNIFICANCE_MTIME
    try:
        mtime: Optional[float] = SIGNIFICANCE_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if _SIGNIFICANCE_CACHE is not None and mtime == _SIGNIFICANCE_MTIME:
        return _SIGNIFICANCE_CACHE
    try:
        with open(SIGNIFICANCE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    _SIGNIFICANCE_CACHE = data
    _SIGNIFICANCE_MTIME = mtime
    return data


def is_significant(source: str, subject: Optional[str]) -> bool:
    """True if (source, subject) is in the significant set.

    Match rule: each entry in `significant_subjects` is a prefix tested against
    the composite key `f'{source}:{subject}'` (or bare `source` when subject is
    None). A trailing '*' or ':' on an entry is cosmetic and stripped before the
    prefix test, so 'heal-credential-registry-drift:*' and
    'heal-credential-registry-drift' both match any subject under that source.
    Default (no match) = routine."""
    data = _load_significance()
    entries = data.get('significant_subjects')
    if not isinstance(entries, list):
        return False
    composite = f'{source}:{subject}' if subject else source
    for raw in entries:
        if not isinstance(raw, str) or not raw:
            continue
        prefix = raw.rstrip('*').rstrip(':')
        if not prefix:
            continue
        if composite == prefix or composite.startswith(prefix + ':') \
                or composite.startswith(prefix):
            return True
    return False


def classify_route(source: str, subject: Optional[str], healed: bool) -> str:
    """Single-source route decision for outcome-aware emitters.

    - A heal that did NOT succeed (healed=False) always routes 'escalate' — it
      must DM Larry with the action he takes (and un-healable detections land
      here too).
    - A SUCCESSFUL heal (healed=True) routes 'closure' if its subject is
      significant (would have stalled/broken the chain, touched
      money/credentials/secrets, or was user-facing) and 'digest' otherwise.

    Keep this the only place the significance list is consulted so no per-healer
    copy of the membership logic can drift."""
    if not healed:
        return 'escalate'
    return 'closure' if is_significant(source, subject) else 'digest'


# ---------- graduation registry (alert-pipeline-rework B4: incremental migration) ----------


_GRADUATION_CACHE: Optional[dict] = None
_GRADUATION_MTIME: Optional[float] = None


def _load_graduation() -> dict:
    """Read config/alert-graduation-registry.json, reloading on mtime change
    (same long-running-process rationale as the significance/translation caches).
    Returns the parsed dict, or {} if the file is missing/malformed — in which
    case every source is treated as un-migrated (the conservative default: an
    un-migrated source keeps its DM-by-default escalate behavior, so a missing
    or broken registry fails loud, never silently holding an alert)."""
    global _GRADUATION_CACHE, _GRADUATION_MTIME
    try:
        mtime: Optional[float] = GRADUATION_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if _GRADUATION_CACHE is not None and mtime == _GRADUATION_MTIME:
        return _GRADUATION_CACHE
    try:
        with open(GRADUATION_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    _GRADUATION_CACHE = data
    _GRADUATION_MTIME = mtime
    return data


def graduated_route(source: str) -> Optional[str]:
    """Return the migrated default route for `source`, or None if un-migrated.

    The graduation registry (config/alert-graduation-registry.json) is the
    incremental-migration control surface (alert-pipeline-rework B4): only the
    sources listed under `migrated_sources` have their routine (non-critical)
    alerts defaulted into the hybrid DM gate (`hold`) instead of the global
    escalate default. A source absent from the registry is un-migrated and keeps
    the severity-based default.

    The registry's mapped value must be a recognized route (VALID_ROUTES) —
    typically `hold`; an unrecognized value (a typo) returns None so the source
    falls back to its DM-by-default behavior rather than silently mis-routing.
    `append_alert` consults this only when the caller passes no explicit route,
    and the critical-forces-escalate guard still fires afterward, so a migrated
    source's critical alerts always DM."""
    data = _load_graduation()
    migrated = data.get('migrated_sources')
    if not isinstance(migrated, dict):
        return None
    route = migrated.get(source)
    if isinstance(route, str) and route in VALID_ROUTES:
        return route
    return None


# ---------- notification writer (D3.5 5a-followup: chain-completion DMs) ----------


def append_notification(
    source: str,
    intent: str,
    message: str,
    chat_id: int,
    task_id: Optional[str] = None,
) -> bool:
    """Append one notification (closure DM for a chat-initiated task).

    Different shape from `append_alert` in three ways:

    1. **No cooldown gating.** Notifications are 1:1 with task completions
       (one PASS → one DM), not infra-noise. Repeating the same intent for
       different task_ids is normal; suppressing would lose closure DMs.
    2. **Targeted to a specific chat_id**, not broadcast to all authorized
       chats. The bot reads `chat_id` from the record and DMs only there.
       (Future-proofing for multi-user; today single-chat means same
       behavior either way.)
    3. **Carries `intent`**, not `severity`. The bot renders intent-specific
       emoji (✓ for review-pass, ⚠ for revision/escalate, 🛑 for emergency,
       ✗ for reject/clarification-exhausted) via `format_dm`.

    Records persist to the same `larry-alerts.jsonl` file as alerts, with
    `kind: "notification"` field distinguishing the two. The bot's reader
    side (`read_pending` + `read_offset` + `format_dm`) handles both.

    Returns True on successful append, False on failure. Never raises —
    callers fire-and-forget.
    """
    refuse_under_test('larry-alerts')
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'kind': 'notification',
        'intent': intent,
        'message': message,
        'chat_id': chat_id,
    }
    if task_id:
        record['task_id'] = task_id
    return _locked_append(json.dumps(record, ensure_ascii=False) + '\n')


# ---------- approval-request writer (D3.5 5c: Beacon's auto-replan path) ----------


def append_approval_request(
    chat_id: int,
    approval_id: str,
    body: str,
    source: str = 'outbox-notifier',
    decision_key: Optional[str] = None,
    pr_url: Optional[str] = None,
) -> bool:
    """Append one approval-request record (Beacon-replan path).

    Different shape from `append_alert` and `append_notification`:

    1. **No cooldown gating.** Each replan is 1:1 with a task; suppressing
       would silently drop the auto-replan's approval prompt.
    2. **Targeted to a specific chat_id** (same as notifications) — the
       bot DMs only the originating thread.
    3. **Carries `approval_id` + `body`** — `approval_id` is the pending-
       approvals entry key (the bot looks it up to render the latest
       formatted prompt + reminder schedule + dispatch on approve); `body`
       is the pre-rendered fallback if the entry has been resolved before
       the bot polls (race protection — degrade to "stale approval-request
       record" rather than crashing the daemon).

    Used by `outbox_notifier._route_beacon_replan_approval` when the
    notifier extracts Beacon's auto-replan APPROVAL_REQUEST from her
    outbox and the trust policy says `force_ask`. The bot's existing
    chat-mode APPROVAL_REQUEST path (`_send_beacon_response` in
    `beacon_telegram_bot.py`) is unchanged — that handles markers Beacon
    emits in chat replies; this handles markers she emits via the inbox-
    watcher dispatch.

    Returns True on successful append, False on failure. Never raises —
    callers fire-and-forget.
    """
    refuse_under_test('larry-alerts')
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'kind': 'approval_request',
        'approval_id': approval_id,
        'chat_id': chat_id,
        'body': body,
    }
    # Change A: an approval_request record is a needs-Larry item, so always stamp
    # the canonical key (derived from approval_id+pr_url when not passed) — the
    # resolve fan-out's A-leg retracts it by key when the decision resolves.
    dk = decision_key or _decision_key_for(approval_id, pr_url)
    if dk:
        record['decision_key'] = dk
    return _locked_append(json.dumps(record, ensure_ascii=False) + '\n')


# ---------- reader side (bot owns this) ----------


def read_pending(offset: int) -> list[tuple[int, dict]]:
    """Return [(line_index, parsed_dict)] for entries at or beyond offset.

    Malformed JSON or blank lines surface as {'_malformed': True, 'raw': ...}
    so the bot can advance past them rather than wedging forever.
    """
    if not ALERTS_FILE.exists():
        return []
    out: list[tuple[int, dict]] = []
    try:
        with open(ALERTS_FILE, encoding='utf-8') as f:
            for idx, raw_line in enumerate(f):
                if idx < offset:
                    continue
                line = raw_line.strip()
                if not line:
                    out.append((idx, {'_malformed': True, 'raw': ''}))
                    continue
                try:
                    out.append((idx, json.loads(line)))
                except json.JSONDecodeError:
                    out.append((idx, {'_malformed': True, 'raw': line[:200]}))
    except OSError:
        return []
    return out


def read_digest_window(start, end) -> list[dict]:
    """Return route=='digest' alert records with `ts` in [start, end).

    Used by the CEO digest to surface self-healed-no-action events that the bot
    deliberately did NOT DM. `start`/`end` are timezone-aware UTC datetimes
    (the digest's period_window returns UTC bounds; the records' `ts` is UTC
    ISO-8601). Records with a missing/unparseable `ts` are skipped (they can't
    be windowed). Never raises — returns [] on any read error."""
    if not ALERTS_FILE.exists():
        return []
    out: list[dict] = []
    try:
        with open(ALERTS_FILE, encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get('route') != 'digest':
                    continue
                ts_raw = rec.get('ts')
                if not isinstance(ts_raw, str):
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if start <= ts < end:
                    out.append(rec)
    except OSError:
        return []
    return out


def read_offset() -> int:
    """The bot's last-delivered line index + 1. 0 if file missing."""
    if not OFFSET_FILE.exists():
        return 0
    try:
        return int(OFFSET_FILE.read_text().strip() or '0')
    except (OSError, ValueError):
        return 0


def write_offset(offset: int) -> None:
    """Atomically persist the bot's offset (tmp + rename)."""
    try:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OFFSET_FILE.with_suffix('.tmp')
        tmp.write_text(str(offset))
        tmp.rename(OFFSET_FILE)
    except OSError:
        # Best-effort. Worst case: at-least-once delivery → duplicate DM.
        pass


# ---------- translation layer (stopgap; see operating-manual.md #68) ----------


_TRANSLATIONS_CACHE: Optional[dict] = None
_TRANSLATIONS_MTIME: Optional[float] = None


def _load_translations() -> dict:
    """Read config/alert-translations.json, reloading whenever the file's
    mtime changes. Returns the nested-by-source dict, or {} if the file is
    missing/malformed (the caller falls back to raw-body + [no translation]
    footer in that case).

    Cache-by-mtime, not cache-once: the Beacon bot is a long-running process
    (uptime measured in days), so a once-per-process cache meant a freshly
    added translation entry stayed invisible until the next bot restart — an
    entry could be live in the file yet still render `[no translation]`.
    Keying on mtime lets a config edit take effect on the next alert.
    """
    global _TRANSLATIONS_CACHE, _TRANSLATIONS_MTIME
    try:
        mtime: Optional[float] = TRANSLATIONS_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if _TRANSLATIONS_CACHE is not None and mtime == _TRANSLATIONS_MTIME:
        return _TRANSLATIONS_CACHE
    try:
        with open(TRANSLATIONS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    _TRANSLATIONS_CACHE = data
    _TRANSLATIONS_MTIME = mtime
    return data


def translate_alert(source: str, subject: Optional[str]) -> Optional[dict]:
    """Look up a translation for (source, subject). Returns the translation
    entry (dict with severity / plain_language_summary / recommended_action)
    or None on miss.

    Lookup rule (per dispatch CLARIFY response):
      1. Exact match on the full subject.
      2. Longest-prefix match: strip trailing ':'-segments from subject one
         at a time, retrying after each strip until a key matches.

    Source must match exactly. Subject=None never matches (alerts without a
    subject can't be translated under V1 — there's no key shape for them)."""
    if not subject:
        return None
    translations = _load_translations()
    source_entries = translations.get(source)
    if not isinstance(source_entries, dict):
        return None
    if subject in source_entries:
        entry = source_entries[subject]
        return entry if isinstance(entry, dict) else None
    # Longest-prefix: strip trailing ':'-segments and retry.
    parts = subject.split(':')
    for trim in range(1, len(parts)):
        candidate = ':'.join(parts[:-trim])
        if candidate in source_entries:
            entry = source_entries[candidate]
            return entry if isinstance(entry, dict) else None
    return None


# Tier glyphs per spec agents/beacon/specs/operator-ux-alert-taxonomy.md § 2.
# Conservative default: an entry missing a `tier` field renders as FYI rather
# than NOW (mirror-review focus from the dispatch).
_TIER_GLYPHS = {'NOW': '🔴', 'SOON': '🟡', 'FYI': '⚪'}
_TIER_DEFAULT = 'FYI'

# `tier_source` values stamped alongside the tier at write time.
TIER_SOURCE_TRANSLATION = 'translation'
TIER_SOURCE_DEFAULT = 'default'


def resolve_tier(translation: Optional[dict]) -> tuple:
    """Resolve a translation entry to ``(tier, tier_source)``.

    THE single tier resolution for both the DM glyph header and the tier
    stamped onto the appended row — one lookup means the rendered glyph and
    the recorded tier can never disagree.

    Returns the entry's `tier` with ``tier_source='translation'`` when it is
    one of NOW/SOON/FYI. Everything else — no translation matched, entry
    missing `tier`, or an unrecognized value (a typo in the config) — is the
    conservative FYI default with ``tier_source='default'``, so a
    misclassification under-notifies rather than crying wolf. Downstream
    consumers (XIV-b's lapse-window tuning) gate on `tier_source` to tell a
    real classification apart from the fallback."""
    if isinstance(translation, dict):
        tier = translation.get('tier')
        if isinstance(tier, str) and tier in _TIER_GLYPHS:
            return tier, TIER_SOURCE_TRANSLATION
    return _TIER_DEFAULT, TIER_SOURCE_DEFAULT


def resolve_tier_for(source: str, subject: Optional[str]) -> tuple:
    """``resolve_tier`` over a fresh (source, subject) translation lookup.

    Write-path entry point: never raises (a broken/unreadable translations
    config degrades to the FYI default), because `append_alert`'s contract is
    fire-and-forget."""
    try:
        return resolve_tier(translate_alert(source, subject))
    except Exception:
        return _TIER_DEFAULT, TIER_SOURCE_DEFAULT


def _render_translated_alert(record: dict, translation: dict) -> str:
    """Render a matched alert with the new layered shape:

        <GLYPH> <TIER> · <subject>            (operator-triage header)
        <SEVERITY_WORD>                       (plain text, no emoji prefix)
        <plain-language summary>

        <recommended action>

        ---technical detail---
        <original raw header + body verbatim>

    The tier line is prepended per the operator-UX alert taxonomy spec; the
    technical-detail block preserves the pre-translation render so the
    operator can still see source, subject, original message, and any
    suggested_action that the producer wrote."""
    severity_label = translation.get('severity', 'WARNING')
    tier, _tier_source = resolve_tier(translation)
    glyph = _TIER_GLYPHS[tier]
    subject = record.get('subject') or ''
    tier_line = f'{glyph} {tier}'
    if subject:
        tier_line += f' · {subject}'
    summary = translation.get('plain_language_summary', '').strip()
    action = translation.get('recommended_action', '').strip()
    raw_body = _render_raw_alert_body(record)
    lines: list[str] = [tier_line, severity_label]
    if summary:
        lines.append('')
        lines.append(summary)
    if action:
        lines.append('')
        lines.append(action)
    lines.append('')
    lines.append('---technical detail---')
    lines.append(raw_body)
    return '\n'.join(lines)


def _render_raw_alert_body(record: dict) -> str:
    """The original pre-translation render shape (severity emoji + source +
    subject + message + suggested_action). Kept verbatim for the technical-
    detail footer of matched alerts AND for the fallback render of unmatched
    alerts."""
    severity = record.get('severity', 'warning')
    if severity == 'critical':
        emoji = '🚨'
    elif severity == 'info':
        emoji = 'ℹ'
    else:
        emoji = '⚠'
    source = record.get('source', '?')
    subject = record.get('subject')
    header = f'{emoji} {source}'
    if subject:
        header += f' [{subject}]'
    lines = [header, record.get('message', '')]
    sa = record.get('suggested_action')
    if sa:
        lines.append(f'Run: {sa}')
    return '\n'.join(line for line in lines if line)


_NO_TRANSLATION_FOOTER = (
    '[no translation; needs entry in config/alert-translations.json '
    'or Pulse triage scope]'
)


def _render_outcome_alert(record: dict) -> str:
    """Render a route=closure|digest alert as a self-healed confirmation.

    Outcome language ONLY — this is a heal that already succeeded, so the DM
    must never carry a "go run <command>" imperative. The summary is the
    producer's own outcome message (written at emit time, when the real outcome
    is known and accurate); a translation-table summary is only a fallback for
    producers that emit no message. Any `suggested_action` on the record is
    deliberately dropped."""
    subject = record.get('subject') or ''
    source = record.get('source', '?')
    summary = (record.get('message') or '').strip()
    if not summary:
        translation = translate_alert(source, subject) if source != '?' else None
        if translation is not None:
            summary = (translation.get('plain_language_summary') or '').strip()
    head = '✅ Self-healed'
    if subject:
        head += f' — {subject}'
    lines = [head]
    if summary:
        lines.append(summary)
    lines.append('')
    lines.append('No action needed.')
    return '\n'.join(lines)


_NOTIFICATION_INTENT_EMOJI = {
    'review-pass': '✓',
    'review-revision': '⚠',
    'review-escalate': '⚠',
    'review-emergency-halt': '🛑',
    'reject': '✗',
    'clarification-exhausted': '✗',
    # Auto-merge holds that need a manual step from Larry — action items, so ⚠
    # rather than the neutral 📬 fallback. `merge_held_deep_review` is the
    # deep-review hold (run /code-review high, then merge_reviewed_pr.sh);
    # `merge_conflict_manual_rebase` is its older twin (rebase then it retries).
    'merge_held_deep_review': '⚠',
    'merge_conflict_manual_rebase': '⚠',
    # System self-awareness: the doorbell nudge (scripts/doorbell_notifier.py) —
    # a calm "N items need your call — check the board", deliberately NOT a ⚠
    # alert (it's a reminder, not an infra failure).
    'doorbell': '🔔',
}


def format_dm(record: dict) -> str:
    """Render an alert OR notification OR approval-request for Telegram DM.

    Three record shapes share this file:

    - Alerts (`kind: "alert"` or missing) — render with source + subject +
      severity emoji + message + optional suggested-action.
    - Notifications (`kind: "notification"`) — render as `<emoji> <message>`
      with the emoji chosen by intent.
    - Approval requests (`kind: "approval_request"`, D3.5 5c) — render is
      done by the BOT (it looks up the pending-approvals entry by
      `approval_id` and calls `approval.format_approval_dm`). This function
      returns the pre-rendered `body` as a fallback for the race where the
      entry has been resolved between append and read.
    """
    if record.get('_malformed'):
        return f'⚠ Bad alert in queue (skipped): {record.get("raw", "")!r}'
    # Notification rendering (D3.5 5a-followup).
    if record.get('kind') == 'notification':
        intent = record.get('intent', '?')
        emoji = _NOTIFICATION_INTENT_EMOJI.get(intent, '📬')
        return f'{emoji} {record.get("message", "")}'
    # Approval-request rendering (D3.5 5c). Bot reads `approval_id` to find
    # the pending entry and render via approval.format_approval_dm; this is
    # the degraded-fallback path (entry vanished between append and read).
    if record.get('kind') == 'approval_request':
        return record.get('body', '🪔 (approval request — entry not found)')
    # Outcome routing (fix-first, 2026-06-03): a closure/digest alert is a heal
    # that already succeeded — render the self-healed confirmation with NO
    # imperative. (digest alerts are normally skipped by the bot and surfaced in
    # the daily digest instead; this render is the fallback / closure path.)
    if record.get('route') in ('closure', 'digest'):
        return _render_outcome_alert(record)
    # Alert rendering. First try the translation layer (stopgap; see
    # operating-manual.md #68): if (source, subject) matches an entry in
    # config/alert-translations.json, render the layered form with severity
    # word + plain-language summary + recommended action + technical-detail
    # footer. On miss, fall back to the original render shape with a
    # `[no translation]` footer so silence-on-unmatched is impossible.
    source = record.get('source', '?')
    subject = record.get('subject')
    translation = translate_alert(source, subject) if source != '?' else None
    if translation is not None:
        return _render_translated_alert(record, translation)
    raw = _render_raw_alert_body(record)
    return f'{raw}\n\n{_NO_TRANSLATION_FOOTER}'


# ---------- CLI (shell-callable from sync_agent_core.sh, run_cycle.sh, etc.) ----------


def _cli_append_alert(args) -> int:
    ok = append_alert(
        source=args.source,
        severity=args.severity,
        message=args.message,
        subject=args.subject,
        suggested_action=args.suggested_action,
        route=args.route,
    )
    return 0 if ok else 1


def _cli_append_notification(args) -> int:
    ok = append_notification(
        source=args.source,
        intent=args.intent,
        message=args.message,
        chat_id=args.chat_id,
        task_id=args.task_id,
    )
    return 0 if ok else 1


def _cli_append_approval_request(args) -> int:
    ok = append_approval_request(
        chat_id=args.chat_id,
        approval_id=args.approval_id,
        body=args.body,
        source=args.source,
    )
    return 0 if ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog='larry_alerts.py',
        description='Shell-callable CLI for the larry-alerts queue.',
    )
    sub = parser.add_subparsers(dest='cmd', required=True)
    aa = sub.add_parser(
        'append_alert',
        help='Append one alert (subject to per-source:subject cooldown).',
    )
    aa.add_argument('--source', required=True)
    aa.add_argument('--severity', required=True, choices=list(VALID_SEVERITIES))
    aa.add_argument('--message', required=True)
    aa.add_argument('--subject', default=None)
    aa.add_argument('--suggested-action', dest='suggested_action', default=None)
    aa.add_argument(
        '--route', default=None, choices=list(VALID_ROUTES),
        help='escalate (DM now — default for warning/critical), closure '
             '(one-line self-healed DM), or digest (no DM; surfaced in the '
             'daily digest — the default for info severity).',
    )

    # Notification + approval-request subcommands let the Medic operator
    # escalate via a stable CLI shape its bash allowlist can match, instead of
    # the fragile inline `python3 -c "..."` form. append_alert is deliberately
    # NOT reachable for Medic (it would loop back into Medic's own batch); its
    # allowlist permits only these two subcommands.
    an = sub.add_parser(
        'append_notification',
        help='Append one notification (no cooldown; targeted to chat_id).',
    )
    an.add_argument('--source', required=True)
    an.add_argument('--intent', required=True)
    an.add_argument('--message', required=True)
    an.add_argument('--chat-id', dest='chat_id', type=int, required=True)
    an.add_argument('--task-id', dest='task_id', default=None)

    ar = sub.add_parser(
        'append_approval_request',
        help='Append one approval-request (no cooldown; targeted to chat_id).',
    )
    ar.add_argument('--chat-id', dest='chat_id', type=int, required=True)
    ar.add_argument('--approval-id', dest='approval_id', required=True)
    ar.add_argument('--body', required=True)
    ar.add_argument('--source', default='outbox-notifier')

    args = parser.parse_args(argv)
    if args.cmd == 'append_alert':
        return _cli_append_alert(args)
    if args.cmd == 'append_notification':
        return _cli_append_notification(args)
    if args.cmd == 'append_approval_request':
        return _cli_append_approval_request(args)
    return 2


if __name__ == '__main__':
    import sys
    sys.exit(main())
