#!/usr/bin/env python3
"""heal_unregistered_approval.py — reconciliation net for stranded direction-asks.

The Approvals tab is fed ONLY by `approval_request` chain_events, which exist
only when Beacon emits a canonical `=== APPROVAL_REQUEST ===` marker. A decision
that needs Larry's DIRECTION (choose between two options before a dispatch) is
supposed to be emitted as a binary approval_request (see agents/beacon/CLAUDE.md
"Direction-asks are APPROVAL_REQUESTs"). When that emission is missed and the ask
is written as a `pulse/beacon-result` larry-alert instead, no event is registered
and the decision never reaches the tab — it strands in the Telegram stream. That
is exactly what happened to the 2026-06-03 deploy-notifier ask.

This healer is the ENFORCEMENT NET behind the emission guidance. Each run it:

  1. SCANS `blackboard/larry-alerts.jsonl` over a trailing window (default 24h)
     for APPROVAL-CLASS escalations: `route == "escalate"` AND a decision signal
     (suggested_action starts with a decision verb, or message/subject contains a
     decision phrase). The heuristic is conservative and config-driven
     (config/unregistered-approval-heuristics.json) — a false positive is a
     dismissible tab card; a false negative is the bug we are killing.
  2. MATCHES each candidate against already-registered approvals
     (beacon-pending-approvals.json pending + history) by a stable dedup_identity
     derived from the alert subject, so a marker Beacon DID emit is never
     duplicated.
  3. PROMOTES each UNMATCHED candidate by registering an `approval_request`
     (target_agent="beacon") via the same `add_pending` + `emit_event` pair the
     bot uses, so it lands on the tab. Binary options are reconstructed from the
     alert's suggested_action where parseable; otherwise a single "needs-triage"
     approval_request carries the message + suggested_action verbatim, with
     approve/reject both routing back to Beacon to formalize.
  4. DEDUPS via a state file (state/heal-unregistered-approval-promoted.json) so
     each source alert is promoted at most once; idempotent across ticks.
  5. HEARTBEATS each run; on its own failure emits a larry-alert (it is itself
     covered by the daemon-liveness watchers).

SECOND SCAN SOURCE (blackboard/for-larry-escalations.json): a session-less
Mirror review escalate that never got an APPROVAL_REQUEST marker still lands as
an OPEN record in the for-Larry "needs you" feed — visible in the Telegram
stream but never on the Approvals tab, and the larry-alerts scan never sees it
(the 2026-07-08 PR #854 blind spot). Alongside the alert scan, each tick also
promotes every OPEN, DECISION-class for-Larry record (source=='mirror-review')
that has no matching registered approval, reusing the SAME add_pending / emit /
dedup machinery (a namespaced ledger key + colon/hyphen id normalization keep
the two sources from colliding or double-registering). The larry-alerts scan
behavior is unchanged.

Stdlib + the existing chain_event_emit / beacon_approval_handler helpers only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import beacon_approval_handler as approval  # noqa: E402
import chain_event_emit  # noqa: E402
import task_terminal_state as tts  # noqa: E402 — shared terminal-state probe kernel

CONFIG_FILE = _SCRIPT_DIR.parent / 'config' / 'unregistered-approval-heuristics.json'

# Built-in defaults. The healer NEVER silently disables itself: if the config
# file is missing or malformed it falls back to these (and logs a WARN), so a
# bad edit cannot turn the net off without anyone noticing.
DEFAULT_SCAN_WINDOW_HOURS = 24
DEFAULT_SUGGESTED_ACTION_PREFIXES = ('Reply', 'Tell Beacon', 'Choose', 'Pick')
DEFAULT_DECISION_PHRASES = (
    'holding APPROVAL_REQUEST',
    'needs your call',
    'your direction',
    'which option',
)
# Resolution-announcement phrases for signal (c): a LATER alert about the same
# decision carrying one of these means the ask was settled out-of-band. Kept
# conservative — must co-occur with a decision-identity match, never alone.
DEFAULT_RESOLUTION_PHRASES = (
    'resolved', 'merged', 'shipped', 'closed', 'landed',
    'no longer needed', 'already done', 'superseded',
)
# Second scan source (blackboard/for-larry-escalations.json): the `source`
# values whose OPEN records are a genuine binary DECISION for Larry (approve =
# formalize/act, reject = dismiss) rather than an action-needed/FYI item. Kept
# CONSERVATIVE — only Mirror review escalates qualify by default. Config-driven
# via `for_larry_decision_sources` in the heuristics file (no code change to
# widen/narrow). A record also has to carry the `<source>:` id prefix the
# no-session router writes, so a mislabeled row can't slip through.
DEFAULT_FORLARRY_DECISION_SOURCES = ('mirror-review',)

# Source label stamped on promoted approvals + self-failure alerts.
HEALER_SOURCE = 'heal-unregistered-approval'

# Deterministic task_id prefix for promoted approvals. Keeps the healer's own
# registrations recognizable and lets the dedup hold even if the state file is
# lost (the deterministic id already lives in pending/history).
PROMOTED_TASK_PREFIX = 'unreg-approval'

# Namespace prefix for for-larry-escalations dedup-ledger keys, so a promoted
# for-Larry record id can never collide with a promoted larry-alert identity in
# the shared state/heal-unregistered-approval-promoted.json ledger.
FORLARRY_LEDGER_PREFIX = 'forlarry:'

# Per-tick cap on archived Beacon outboxes parsed during marker recovery, so an
# unpruned outbox archive can't make the scan unbounded. Only in-window files
# are eligible, newest-first; recovery is a best-effort enrichment, so a deeper
# tail being skipped just falls back to the alert-derived card.
MAX_ARCHIVE_FILES_READ = 200

# Known dispatch targets. A recovered marker's target_agent is trusted only if it
# names one of these; anything else falls back to 'beacon' (the always-safe
# mediator), so a typo'd/exotic target can't advertise a bogus route on the card.
KNOWN_TARGET_AGENTS = frozenset({'beacon', 'forge', 'mirror', 'pulse'})

# "Choose A or B" / "Pick A vs B" / "Reply A or B" splitter for binary options.
_BINARY_SPLIT_RE = re.compile(r'\s+(?:or|vs\.?|versus)\s+', re.IGNORECASE)
_LEADING_VERB_RE = re.compile(
    r'^\s*(?:reply|tell\s+beacon|choose|pick|whether\s+to|between)\b[:\s]*',
    re.IGNORECASE,
)


# -------------------- paths / env --------------------

def agents_root() -> Path:
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def blackboard() -> Path:
    return agents_root() / 'blackboard'


def state_dir() -> Path:
    return agents_root() / 'state'


def alerts_file() -> Path:
    return blackboard() / 'larry-alerts.jsonl'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def healer_heartbeat() -> Path:
    return blackboard() / 'heal-unregistered-approval.heartbeat'


def promoted_state_file() -> Path:
    return state_dir() / 'heal-unregistered-approval-promoted.json'


def log_file() -> Path:
    return agents_root() / 'logs' / 'heal-unregistered-approval.log'


def _primary_chat_id() -> Optional[int]:
    """Larry's primary Telegram chat — the lowest id in TELEGRAM_ALLOWED_CHAT_IDS
    (mirrors doorbell_notifier / outbox_notifier / pulse_check _primary_chat_id).
    None only when the allow-list is unset/empty."""
    raw = os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS', '')
    ids = []
    for tok in raw.replace(',', ' ').split():
        try:
            ids.append(int(tok))
        except ValueError:
            continue
    return min(ids) if ids else None


def _chat_id() -> Optional[int]:
    """Chat id stamped on a promoted pending entry so the promoted ask carries a
    real recipient (the DM path) instead of chat_id=None.

    An explicit override (OURLIBERTY_APPROVAL_HEALER_CHAT_ID) wins; otherwise
    fall back to Larry's primary allowed chat via _primary_chat_id() — the #812
    null-chat fix pattern — so a daemon-originated promotion never registers a
    chat_id=None approval. Returns None ONLY when NEITHER an override NOR any
    allowed chat exists; main() then SKIPS the promotion (with an actionable
    alert) rather than register a broken null-chat row.
    """
    raw = os.environ.get('OURLIBERTY_APPROVAL_HEALER_CHAT_ID')
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _primary_chat_id()


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        lf = log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        hb = healer_heartbeat()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({
            'ts': datetime.now(timezone.utc).isoformat(),
            'healer': HEALER_SOURCE,
        }))
    except OSError:
        pass


# -------------------- config --------------------

def load_heuristics(path: Optional[Path] = None) -> dict[str, Any]:
    """Return the heuristic config, falling back to built-in defaults on a
    missing/malformed file (never disables the net). The returned dict always
    has the three keys the evaluator reads."""
    defaults = {
        'scan_window_hours': DEFAULT_SCAN_WINDOW_HOURS,
        'suggested_action_prefixes': list(DEFAULT_SUGGESTED_ACTION_PREFIXES),
        'decision_phrases': list(DEFAULT_DECISION_PHRASES),
        'resolution_phrases': list(DEFAULT_RESOLUTION_PHRASES),
        'for_larry_decision_sources': list(DEFAULT_FORLARRY_DECISION_SOURCES),
    }
    cfg_path = path or CONFIG_FILE
    try:
        data = json.loads(cfg_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    out = dict(defaults)
    window = data.get('scan_window_hours')
    if isinstance(window, (int, float)) and window > 0:
        out['scan_window_hours'] = float(window)
    prefixes = data.get('suggested_action_prefixes')
    if isinstance(prefixes, list):
        clean = [p for p in prefixes if isinstance(p, str) and p.strip()]
        if clean:
            out['suggested_action_prefixes'] = clean
    phrases = data.get('decision_phrases')
    if isinstance(phrases, list):
        clean = [p for p in phrases if isinstance(p, str) and p.strip()]
        if clean:
            out['decision_phrases'] = clean
    res_phrases = data.get('resolution_phrases')
    if isinstance(res_phrases, list):
        clean = [p for p in res_phrases if isinstance(p, str) and p.strip()]
        if clean:
            out['resolution_phrases'] = clean
    fl_sources = data.get('for_larry_decision_sources')
    if isinstance(fl_sources, list):
        clean = [s for s in fl_sources if isinstance(s, str) and s.strip()]
        if clean:
            out['for_larry_decision_sources'] = clean
    return out


# -------------------- alert scanning (pure) --------------------

def parse_alert_lines(lines: list[str]) -> list[dict[str, Any]]:
    """Parse JSONL alert lines into dicts, skipping blanks + malformed."""
    out: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_alerts(path: Optional[Path] = None) -> list[dict[str, Any]]:
    p = path or alerts_file()
    try:
        with open(p, encoding='utf-8') as fh:
            return parse_alert_lines(fh.readlines())
    except OSError:
        return []


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def within_window(record: dict[str, Any], now: datetime, window_hours: float) -> bool:
    """True if the record's ts is within the trailing window ending at `now`.
    A record with a missing/unparseable ts is INCLUDED (fail toward catching a
    real decision rather than dropping it on a bad timestamp)."""
    ts = _parse_ts(record.get('ts'))
    if ts is None:
        return True
    age_h = (now - ts).total_seconds() / 3600.0
    return -1.0 <= age_h <= window_hours  # small negative tolerance for clock skew


def is_approval_class(record: dict[str, Any], heuristics: dict[str, Any]) -> bool:
    """Conservative decision-signal test. Requires route == 'escalate' AND
    either a decision-verb suggested_action prefix or a decision phrase in the
    message/subject. Notifications / non-escalate routes never qualify."""
    if record.get('kind') in ('notification', 'approval_request'):
        return False
    if record.get('route', approval_default_route()) != 'escalate':
        return False
    suggested = record.get('suggested_action')
    if isinstance(suggested, str):
        stripped = suggested.lstrip()
        for prefix in heuristics['suggested_action_prefixes']:
            if stripped[:len(prefix) + 1].lower().startswith(prefix.lower()) and (
                len(stripped) == len(prefix)
                or not stripped[len(prefix):len(prefix) + 1].isalnum()
            ):
                return True
    haystack = ' '.join(
        str(record.get(field, '')) for field in ('message', 'subject')
    ).lower()
    for phrase in heuristics['decision_phrases']:
        if phrase.lower() in haystack:
            return True
    return False


def approval_default_route() -> str:
    """Default route an alert without an explicit `route` is treated as. The
    queue's own default is 'escalate' (larry_alerts.DEFAULT_ROUTE); mirror it so
    a legacy alert written before routing existed is still eligible."""
    return 'escalate'


# -------------------- dedup identity + payload (pure) --------------------

def alert_dedup_key(record: dict[str, Any]) -> str:
    """Stable key for a source alert. Prefers the subject (the brief's
    dedup_identity basis); falls back to a hash of source+message so an alert
    with no subject still dedups deterministically."""
    subject = record.get('subject')
    if isinstance(subject, str) and subject.strip():
        return subject.strip()
    basis = f"{record.get('source', '')}|{record.get('message', '')}"
    return 'nosubject:' + hashlib.sha256(basis.encode('utf-8')).hexdigest()[:16]


def derive_task_id(dedup_key: str) -> str:
    digest = hashlib.sha256(dedup_key.encode('utf-8')).hexdigest()[:12]
    return f'{PROMOTED_TASK_PREFIX}-{digest}'


# -------------------- decision identity (dedup of rephrasings) --------------------

# Phrasing tokens stripped before computing a decision identity, so two alerts
# that say the SAME thing in different words ("Beacon needs your call: X" vs
# "X — Beacon needs your call") collapse to one card. Kept SPECIFIC to ask-
# phrasing words; content words (the actual subject of the decision) are never
# in here, or distinct decisions would wrongly merge.
_IDENTITY_STOPWORDS = frozenset({
    'beacon', 'needs', 'need', 'your', 'you', 'call', 'direction', 'ask',
    'asks', 'question', 'ready', 'please', 'now', 'the', 'for', 'and', 'a',
    'an', 'to', 'of', 're', 'is', 'are', 'on', 'or', 'vs',
})
_IDENTITY_TOKEN_RE = re.compile(r'[a-z0-9]+')

# Pull PR/issue numbers out of a subject — but ONLY where a '#<n>' sits in a
# clearly-REFERENTIAL position, so an incidental cross-mention is never treated
# as this decision's resolution anchor (audit §4). Alert records carry no
# structured `pr`/`issue` field (append_alert only writes source/severity/
# message/subject/suggested_action/route), so the subject is the only place a
# reference can live — but not every '#<n>' in it is one. A genuine reference is
# either:
#   * the subject LEADS with it ('#294: rebase or close?'), or
#   * it follows a PR/issue keyword ('PR #294', 'issue #5', or a GitHub closing
#     keyword: closes/fixes/resolves/merged/landed #<n>).
# Anything else — a number buried mid-subject, or any '#<n>' inside parentheses
# / after 're:' (e.g. 'pick A vs B (re: design #5)') — is INCIDENTAL and yields
# no ref, so an unrelated PR #5 merging can't skip-before-promote or auto-retire
# a live direction-ask. Conservative by construction: an ambiguous ref is no
# resolution signal, favoring surfacing/keeping a real decision over hiding it.
_REF_KEYWORD = (
    r'(?:prs?|pull[\s_-]?requests?|issues?|'
    r'close[sd]?|fix(?:e[sd])?|resolve[sd]?|merge[sd]?|land(?:s|ed)?)'
)
# Subject-LEADING '#<n>' ('#294: rebase or close?'). Matched against the raw
# subject, NOT the paren-stripped one, so blanking a parenthetical aside can
# never promote a buried/trailing ref into leading position (e.g. '(foo) #5').
_LEADING_REF_RE = re.compile(r'^\s*#(\d+)')
# '#<n>' right after a PR/issue/closing keyword, anywhere in the subject.
_KEYWORD_REF_RE = re.compile(
    r'\b' + _REF_KEYWORD + r'\b[\s:_-]*#(\d+)',
    re.IGNORECASE,
)
# Parenthetical spans are blanked before the keyword scan: a ref inside '(...)'
# is a side note, not the decision's anchor.
_PAREN_RE = re.compile(r'\([^)]*\)')


def parse_ref_numbers(text: Any) -> list[int]:
    """Return the PR/issue numbers genuinely referenced in `text` (subject).

    Only '#<n>' tokens in a clearly-referential position count: subject-leading
    (checked on the raw subject), or right after a PR/issue/closing keyword
    (checked on the subject with parenthetical asides blanked). Parenthetical
    and other incidental mentions are ignored — this is the anchor for
    skip-before-promote and retire-on-resolution, so an incidental ref must
    never qualify."""
    if not isinstance(text, str):
        return []
    nums: list[int] = []
    lead = _LEADING_REF_RE.match(text)
    if lead:
        nums.append(int(lead.group(1)))
    scannable = _PAREN_RE.sub(' ', text)
    nums.extend(int(m) for m in _KEYWORD_REF_RE.findall(scannable))
    return list(dict.fromkeys(nums))  # dedup, preserve order


def decision_identity(record: dict[str, Any]) -> str:
    """Normalized identity for the DECISION an alert is about (decision 2).

    Not the exact alert string: two phrasings of one decision must yield the
    same identity so they collapse to a single card. Priority:
      1. A referenced PR/issue number ('ref:<n>') — the most stable anchor.
      2. The sorted set of content tokens from the subject (phrasing words
         stripped), joined with '-'.
      3. The raw subject lowercased (no content tokens survived stripping).
    Falls back to the no-subject hash key when there is no subject at all, so
    identity is always deterministic.
    """
    subject = record.get('subject')
    if not isinstance(subject, str) or not subject.strip():
        return alert_dedup_key(record)
    s = subject.strip()
    refs = parse_ref_numbers(s)
    if refs:
        return f'ref:{min(refs)}'
    tokens = [
        t for t in _IDENTITY_TOKEN_RE.findall(s.lower())
        if t not in _IDENTITY_STOPWORDS and not t.isdigit()
    ]
    if not tokens:
        return s.lower()
    return '-'.join(sorted(set(tokens)))


# -------------------- resolution signals (skip-before-promote / retire) -----

# Beacon-pending-approvals statuses that mean a decision was actually resolved
# (mirrors beacon_approval_handler.resolve's accepted statuses).
RESOLVED_STATUSES = frozenset({'approved', 'rejected', 'modified', 'expired'})

# Default GitHub repo the referenced PR/issue numbers belong to. The healer
# scans alerts produced by ourliberty-agent-core work, so refs resolve there
# unless an env override points elsewhere.
DEFAULT_REF_REPO = 'Larry-Yatch/ourliberty-agent-core'
GH_VIEW_TIMEOUT_SEC = 15


def ref_repo() -> str:
    return os.environ.get('OURLIBERTY_HEAL_APPROVAL_REPO') or DEFAULT_REF_REPO


def _gh_state(kind: str, number: int, repo: str, timeout: float) -> Optional[str]:
    """Return the `state` field from `gh <kind> view <number>` or None on any
    failure (timeout, gh missing, non-PR/issue number, bad JSON).

    Uses the shared kernel (task_terminal_state.gh_json) for the bounded `gh`
    call + None-on-error handling; the return contract is unchanged."""
    data = tts.gh_json(
        ['gh', kind, 'view', str(number), '--repo', repo, '--json', 'state'],
        timeout=timeout,
    )
    state = data.get('state') if isinstance(data, dict) else None
    return state if isinstance(state, str) else None


def gh_ref_resolved(number: int) -> Optional[bool]:
    """True if PR/issue #number is MERGED or CLOSED, False if open, None if
    undetermined (gh unavailable / no auth / number is neither). Mirrors
    build_sequence_advancer.gh_pr_says_merged's tri-state contract so an
    undetermined probe is a SOFT result — never a wrongful skip/retire."""
    repo = ref_repo()
    for kind in ('pr', 'issue'):
        state = _gh_state(kind, number, repo, GH_VIEW_TIMEOUT_SEC)
        if state is None:
            continue
        return state in ('MERGED', 'CLOSED')
    return None


def _decision_content_tokens(subject: str, identity: str) -> set[str]:
    """Content tokens that identify a decision, for matching against prose.

    A ref-based identity contributes just its number ('ref:294' -> {'294'}).
    Otherwise the subject's content tokens (phrasing words stripped). Empty
    when there is nothing specific to anchor on."""
    if identity.startswith('ref:'):
        return {identity.split(':', 1)[1]}
    return {
        t for t in _IDENTITY_TOKEN_RE.findall((subject or '').lower())
        if t not in _IDENTITY_STOPWORDS and not t.isdigit()
    }


def history_resolution_match(
    subject: str, identity: str, state: dict[str, Any],
) -> bool:
    """True iff beacon-pending-approvals `history` holds a RESOLVED entry for
    the same decision (signal b). Conservative: matches only when ALL of the
    decision's content tokens (>=2, so a single common word can't over-fire)
    appear in a resolved entry's searchable text — or, for a single-token /
    ref-based identity, when the exact subject or '#<n>' appears. Never matches
    a no-subject key by substring (too weak to anchor)."""
    needle = (subject or '').strip().lower()
    tokens = _decision_content_tokens(subject, identity)
    ref_token = identity.split(':', 1)[1] if identity.startswith('ref:') else None
    for entry in state.get('history', []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get('status') not in RESOLVED_STATUSES:
            continue
        payload = entry.get('dispatch_payload') or {}
        hay = ' '.join(str(p) for p in [
            entry.get('id'),
            entry.get('plan_summary'),
            payload.get('summary') if isinstance(payload, dict) else None,
            payload.get('prompt') if isinstance(payload, dict) else None,
            payload.get('promoted_from_alert') if isinstance(payload, dict) else None,
        ] if p).lower()
        if ref_token is not None:
            if f'#{ref_token}' in hay:
                return True
            continue
        if len(tokens) >= 2 and all(tok in hay for tok in tokens):
            return True
        if needle and not needle.startswith('nosubject:') and needle in hay:
            return True
    return False


def later_resolution_alert(
    subject: str,
    identity: str,
    alerts: list[dict[str, Any]],
    after_ts: Optional[str],
    heuristics: dict[str, Any],
) -> bool:
    """True iff another alert about the SAME decision, dated after `after_ts`,
    announces a resolution (signal c). Conservative: requires BOTH a decision-
    identity match AND a resolution phrase, so an unrelated later alert never
    hides a live ask."""
    phrases = heuristics.get('resolution_phrases') or list(DEFAULT_RESOLUTION_PHRASES)
    after = _parse_ts(after_ts) if after_ts else None
    for rec in alerts:
        if decision_identity(rec) != identity:
            continue
        rec_ts = _parse_ts(rec.get('ts'))
        if after is not None and rec_ts is not None and rec_ts <= after:
            continue
        text = ' '.join(
            str(rec.get(f, '')) for f in ('message', 'subject', 'suggested_action')
        ).lower()
        if any(p.lower() in text for p in phrases):
            return True
    return False


def resolution_signal(
    record_or_key: Any,
    state: dict[str, Any],
    alerts: list[dict[str, Any]],
    heuristics: dict[str, Any],
    *,
    after_ts: Optional[str] = None,
    gh_probe: Any = gh_ref_resolved,
) -> Optional[str]:
    """Return a human-readable reason a decision is RESOLVED, else None.

    `record_or_key` may be a full alert dict (promote path) or a bare subject
    string (retire path, where only the ledger key survives). Conservative by
    construction: an undetermined gh probe (None) is NOT a signal, so the
    caller favors surfacing/keeping a real decision over hiding it.
    """
    if isinstance(record_or_key, dict):
        subject = alert_dedup_key(record_or_key)
        identity = decision_identity(record_or_key)
    else:
        subject = str(record_or_key)
        identity = decision_identity({'subject': subject})
    # (a) referenced PR/issue merged or closed
    for n in parse_ref_numbers(subject):
        if gh_probe(n) is True:
            return f'referenced #{n} is merged/closed'
    # (b) a resolved entry for the same decision in beacon history
    if history_resolution_match(subject, identity, state):
        return 'resolved entry in beacon-pending-approvals history'
    # (c) an explicit later resolution alert for the same subject
    if later_resolution_alert(subject, identity, alerts, after_ts, heuristics):
        return 'later resolution alert for same subject'
    return None


def parse_binary_options(suggested_action: Any) -> Optional[tuple[str, str]]:
    """Reconstruct (option_a, option_b) from a suggested_action like
    'Choose ship-now or scope-the-fix'. Returns None when it does not parse
    cleanly into exactly two options (caller falls back to needs-triage)."""
    if not isinstance(suggested_action, str) or not suggested_action.strip():
        return None
    body = _LEADING_VERB_RE.sub('', suggested_action.strip())
    # Only split on the first line to avoid swallowing multi-line shell hints.
    body = body.splitlines()[0].strip().rstrip('.')
    if not body:
        return None
    parts = _BINARY_SPLIT_RE.split(body)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if not a or not b:
        return None
    return a, b


def build_approval_payload(
    record: dict[str, Any], dedup_key: str,
) -> dict[str, Any]:
    """Build the approval_request payload for add_pending + the chain_event
    builder. target_agent is always 'beacon' (the action handler routes
    approve/reject there). Both options are stated in plain language so the
    tab's approve/reject buttons are self-explanatory."""
    task_id = derive_task_id(dedup_key)
    message = str(record.get('message', '')).strip()
    suggested = record.get('suggested_action')
    subject = record.get('subject') or dedup_key
    options = parse_binary_options(suggested)
    if options is not None:
        option_a, option_b = options
        summary = (
            f'Direction needed (promoted from a missed marker): '
            f'Approve = {option_a}; Reject = {option_b}.'
        )
        prompt = (
            'This direction-ask was raised as a larry-alert without an '
            'APPROVAL_REQUEST marker, so it never reached the Approvals tab; '
            f'{HEALER_SOURCE} promoted it.\n\n'
            f'Source alert subject: {subject}\n'
            f'Original message: {message}\n\n'
            f'Approve = option A: {option_a}\n'
            f'Reject  = option B: {option_b}\n\n'
            'On Larry\'s click the dashboard routes his choice back to Beacon; '
            'Beacon shapes + dispatches the chosen option.'
        )
    else:
        summary = (
            'Decision needs your direction (promoted from a missed marker; '
            'could not be parsed into two options — needs triage).'
        )
        suggested_text = suggested if isinstance(suggested, str) else '(none)'
        prompt = (
            'This decision was raised as a larry-alert without an '
            'APPROVAL_REQUEST marker, so it never reached the Approvals tab; '
            f'{HEALER_SOURCE} promoted it as a needs-triage item.\n\n'
            f'Source alert subject: {subject}\n'
            f'Original message: {message}\n'
            f'Suggested action: {suggested_text}\n\n'
            'Approve OR Reject both route back to Beacon to formalize this into '
            'a proper binary approval_request (or resolve in chat).'
        )
    return {
        'task_id': task_id,
        'summary': summary,
        'target_agent': 'beacon',
        'prompt': prompt,
        'task_type': 'direction-ask',
        'promoted_from_alert': dedup_key,
        # Seam audit H2: this is a healer/dashboard-routed card, NOT something
        # Larry was DMed an approve-grammar prompt for. It reaches the Approvals
        # tab and is resolved by id via the tab's approve/reject buttons. Stamp
        # it so the bare-approve path (beacon_approval_handler.most_recent_pending
        # → _is_operator_dispatchable) never steals a bare `approve` Larry meant
        # for a genuinely DMed plan and dispatches this card to the wrong target.
        'origin': HEALER_SOURCE,
        'bare_approvable': False,
        # Transient: the raw subject, stashed so main() can record it in the
        # promoted ledger (for the retire pass's subject-based signals).
        # Stripped before the payload reaches add_pending / the chain helper.
        '_subject': subject,
    }


# -------------------- marker recovery from Beacon's outbox archive --------

def beacon_outbox_archive() -> Path:
    return agents_root() / 'outboxes' / 'beacon' / '.archive'


def load_beacon_outbox_markers(
    now: datetime,
    window_hours: float,
    archive_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Recover the APPROVAL_REQUEST payloads Beacon actually emitted, by reading
    her archived outbox records.

    The bug this serves (the #412 class): Beacon emits a valid APPROVAL_REQUEST
    in a result-notification / cycle-finding context (source not in the
    notifier's gated approval sources), so the inline marker scanner never
    registers it and it is stripped as narrative. The marker still lives,
    verbatim, in the `result` field of her archived outbox record. Recovering it
    here lets the healer register the REAL approval instead of synthesizing a
    lossy needs-triage card from the downstream larry-alert.

    Bounded + fail-safe: each file is stat'd ONCE; only in-window files are read,
    most-recent first (so the freshest marker wins a match); at most
    MAX_ARCHIVE_FILES_READ in-window files are parsed per tick so an unpruned
    archive can't make this O(huge); any read/parse error skips that file; a
    malformed marker is ignored. Returns the recovered marker payload dicts.
    Never raises."""
    arch = archive_dir or beacon_outbox_archive()
    cutoff = now.timestamp() - window_hours * 3600
    try:
        if not arch.exists():
            return []
        # Stat each file once: build (mtime, path), then filter to the window and
        # sort newest-first. (Previously stat ran up to 3x per file.)
        stamped: list[tuple[float, Path]] = []
        for p in arch.glob('*.json'):
            if p.name.startswith('.'):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                stamped.append((mtime, p))
        stamped.sort(key=lambda t: t[0], reverse=True)  # most recent first
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for _mtime, f in stamped:
        if len(out) >= MAX_ARCHIVE_FILES_READ:
            break
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        result_text = data.get('result')
        if not isinstance(result_text, str) or 'APPROVAL_REQUEST' not in result_text:
            continue
        try:
            payload, _narrative = approval.extract_approval_request(result_text)
        except Exception:  # noqa: BLE001 — malformed marker: ignore, never crash
            continue
        if isinstance(payload, dict) and payload.get('task_id'):
            out.append(dict(payload))
    return out


def match_marker_for_record(
    record: dict[str, Any], markers: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Pure: pick the recovered marker that corresponds to this alert, or None.

    Primary signal (reliable): the marker's task_id appears verbatim in the
    alert text — the pipeline-stall alert that triggers promotion names the
    un-dispatched task (e.g. '...APPROVAL_REQUEST for mirror-review-pr412-001
    but ...'). Secondary: a PR/issue ref the alert anchors on also appears in the
    marker's summary/prompt. `markers` is most-recent-first, so the first match
    is the freshest. Conservative — no signal → None (caller falls back to the
    alert-derived card), so a wrong marker is never grafted onto a decision."""
    if not markers:
        return None
    alert_text = ' '.join(
        str(record.get(k, '')) for k in ('subject', 'message', 'suggested_action')
    ).lower()
    if not alert_text.strip():
        return None
    # Primary: marker task_id named in the alert, matched as a whole token (a
    # word-boundary check, not a bare substring, so a short/compound id can't
    # false-match inside an unrelated longer token).
    for marker in markers:
        tid = marker.get('task_id')
        if not isinstance(tid, str) or len(tid) < 6:
            continue
        if re.search(r'(?<![a-z0-9])' + re.escape(tid.lower()) + r'(?![a-z0-9])',
                     alert_text):
            return marker
    # Secondary: shared PR/issue ref anchor.
    alert_refs = set(parse_ref_numbers(record.get('subject'))) | set(
        parse_ref_numbers(record.get('message')))
    if not alert_refs:
        return None
    for marker in markers:
        marker_text = ' '.join(
            str(marker.get(k, '')) for k in ('summary', 'prompt', 'task_id'))
        if alert_refs & set(parse_ref_numbers(marker_text)):
            return marker
    return None


def build_approval_payload_from_marker(
    marker: dict[str, Any], record: dict[str, Any], dedup_key: str,
) -> dict[str, Any]:
    """Build a CLEAN approval payload from a recovered APPROVAL_REQUEST marker.

    Structurally identical to `build_approval_payload` (same dedup/ledger/retire
    keys: healer-deterministic `task_id`, `promoted_from_alert`, `_subject`,
    `origin`, `bare_approvable=False`), but the user-facing summary/prompt and
    `target_agent` come from what Beacon actually proposed — not a guess parsed
    from the downstream alert. The full marker is preserved under
    `recovered_marker` so the approve path has Beacon's original plan."""
    task_id = derive_task_id(dedup_key)
    subject = record.get('subject') or dedup_key
    marker_summary = str(marker.get('summary') or '').strip() or '(no summary)'
    raw_target = marker.get('target_agent')
    # Trust the marker's target only if it's a real route; otherwise fall back to
    # 'beacon', the always-safe mediator (a typo'd target can't advertise a bogus
    # route on the card).
    marker_target = raw_target if raw_target in KNOWN_TARGET_AGENTS else 'beacon'
    marker_task = marker.get('task_id') or '(unknown)'
    marker_type = marker.get('task_type') or 'direction-ask'
    summary = f'Approval recovered from a missed marker: {marker_summary}'
    prompt = (
        'Beacon emitted this APPROVAL_REQUEST but it was never registered into '
        'the approvals queue (it was emitted in a result-notification / '
        'cycle-finding context that the inline marker scanner does not cover), '
        f'so it never reached the Approvals tab. {HEALER_SOURCE} recovered the '
        "original marker from Beacon's outbox archive and registered it here.\n\n"
        f'Source alert subject: {subject}\n'
        f'Proposed task: {marker_task}\n'
        f'Target agent: {marker_target}\n'
        f'Task type: {marker_type}\n'
        f'Summary: {marker_summary}\n\n'
        'Approve to let Beacon dispatch the proposed task; Reject to decline. '
        'Resolved via the Approvals tab buttons.'
    )
    return {
        'task_id': task_id,
        'summary': summary,
        'target_agent': marker_target,
        'prompt': prompt,
        'task_type': marker_type,
        # The verbatim marker Beacon proposed, preserved in the local pending
        # entry for reference and for any consumer that reads dispatch_payload.
        # NOTE: the Approvals-tab chain_event carries only summary/target/prompt,
        # so on approve Beacon re-shapes the dispatch from those fields (which
        # name the real target + task_id + summary) — this is NOT an automatic
        # verbatim re-dispatch, just a faithful, readable card.
        'recovered_marker': {k: v for k, v in marker.items()
                             if not k.startswith('_')},
        'promoted_from_alert': dedup_key,
        'origin': HEALER_SOURCE,
        'bare_approvable': False,
        '_subject': subject,
    }


# -------------------- registration matching (pure) --------------------

def registered_identities(state: dict[str, Any]) -> tuple[set[str], list[str]]:
    """Return (ids, haystacks) from pending + history. `ids` is the set of
    entry ids (for exact match against the healer's deterministic task_id).
    `haystacks` is a list of lowercased searchable strings (id + plan_summary +
    dispatch_payload prompt/summary/promoted_from_alert) for the subject-based
    collision guard against a marker Beacon already emitted."""
    ids: set[str] = set()
    haystacks: list[str] = []
    for bucket in ('pending', 'history'):
        for entry in state.get(bucket, []) or []:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get('id')
            if isinstance(entry_id, str):
                ids.add(entry_id)
            payload = entry.get('dispatch_payload') or {}
            parts = [
                entry.get('id'),
                entry.get('plan_summary'),
                payload.get('summary') if isinstance(payload, dict) else None,
                payload.get('prompt') if isinstance(payload, dict) else None,
                payload.get('promoted_from_alert') if isinstance(payload, dict) else None,
            ]
            haystacks.append(' '.join(str(p) for p in parts if p).lower())
    return ids, haystacks


def is_already_registered(
    dedup_key: str, task_id: str, state: dict[str, Any],
) -> bool:
    """True if this ask is already on the tab. Two ways to match:
      1. Our own deterministic task_id is already an entry id (idempotent
         re-run, even if the promoted-state file was lost).
      2. The alert subject text appears in any registered entry — the
         collision guard so a marker Beacon DID emit for the same decision is
         never duplicated.
    """
    ids, haystacks = registered_identities(state)
    if task_id in ids:
        return True
    needle = dedup_key.strip().lower()
    if not needle or needle.startswith('nosubject:'):
        return False
    return any(needle in hay for hay in haystacks)


# -------------------- promoted-state dedup --------------------

def load_promoted(path: Optional[Path] = None) -> dict[str, str]:
    p = path or promoted_state_file()
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    promoted = data.get('promoted') if isinstance(data, dict) else None
    if not isinstance(promoted, dict):
        return {}
    return {k: v for k, v in promoted.items() if isinstance(k, str)}


def save_promoted(promoted: dict[str, str], path: Optional[Path] = None) -> None:
    p = path or promoted_state_file()
    payload = {
        '_schema': {
            'version': 1,
            'purpose': (
                'Dedup ledger for heal_unregistered_approval.py: maps each '
                'promoted source-alert dedup_key to the ISO ts it was first '
                'promoted, so each alert lands on the Approvals tab at most '
                'once across ticks.'
            ),
        },
        'promoted': promoted,
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                       encoding='utf-8')
        os.replace(tmp, p)
    except OSError:
        pass


# -------------------- evaluation (pure) --------------------

def evaluate(
    alerts: list[dict[str, Any]],
    heuristics: dict[str, Any],
    state: dict[str, Any],
    promoted: dict[str, Any],
    now: Optional[datetime] = None,
    resolution_check: Any = None,
    marker_lookup: Any = None,
) -> list[dict[str, Any]]:
    """Return the list of approval payloads to register this tick.

    An alert is promoted iff: in-window, approval-class, not already promoted
    (by decision identity OR legacy subject key), not already registered
    (pending/history match), AND not already resolved out-of-band
    (skip-before-promote, decision 1). Rephrasings collapse to one card via
    `decision_identity` (decision 2). Pure — no I/O, no side effects; main()
    does the registration + persistence.

    `resolution_check` is an injected `callable(record) -> Optional[str]` that
    returns a reason when the decision is already resolved (so it is skipped),
    else None. Defaults to a no-op so existing pure-logic callers keep
    promoting; main() wires the live gh/history/alert probe.

    `marker_lookup` is an injected `callable(record) -> Optional[dict]` that
    returns the original APPROVAL_REQUEST marker Beacon emitted for this decision
    (recovered from her outbox archive), if found. When it returns a marker the
    card is built from Beacon's real proposal (clean summary/target) instead of a
    lossy reconstruction from the alert. Defaults to a no-op so existing callers
    keep the alert-derived behavior.
    """
    now = now or datetime.now(timezone.utc)
    window = heuristics['scan_window_hours']
    check = resolution_check or (lambda rec: None)
    find_marker = marker_lookup or (lambda rec: None)
    used_marker_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for record in alerts:
        if not within_window(record, now, window):
            continue
        if not is_approval_class(record, heuristics):
            continue
        subject = alert_dedup_key(record)
        identity = decision_identity(record)
        if identity in promoted or subject in promoted or identity in seen_keys:
            continue
        task_id = derive_task_id(identity)
        if is_already_registered(subject, task_id, state):
            continue
        # Skip-before-promote: a decision that has already resolved out-of-band
        # (referenced PR merged, resolved in beacon history, later resolution
        # alert) must NOT be promoted. Probed last so gh is only consulted for
        # genuinely unregistered candidates.
        reason = check(record)
        if reason:
            log(f'skip-before-promote: {identity!r} ({reason})')
            seen_keys.add(identity)
            continue
        marker = None
        try:
            marker = find_marker(record)
        except Exception as e:  # noqa: BLE001 — recovery is best-effort; fall back
            log(f'marker recovery raised for {identity!r}: '
                f'{type(e).__name__}: {e}; using alert-derived card', 'WARN')
        # One recovered marker backs at most one card per tick: if two distinct
        # alerts both correlate to the same marker, only the first gets the clean
        # card; the second falls back to its alert-derived form rather than
        # proposing the same dispatch twice.
        if marker is not None:
            m_id = marker.get('task_id')
            if isinstance(m_id, str) and m_id in used_marker_ids:
                marker = None
            elif isinstance(m_id, str):
                used_marker_ids.add(m_id)
        if marker is not None:
            payload = build_approval_payload_from_marker(marker, record, identity)
        else:
            payload = build_approval_payload(record, identity)
        payload['_source_ts'] = record.get('ts')
        out.append(payload)
        seen_keys.add(identity)
    return out


# -------------------- for-larry-escalations scan (second source) -----------
#
# The Approvals tab is fed only by approval_request chain_events. A session-less
# Mirror review escalate that never got an APPROVAL_REQUEST marker still lands as
# an OPEN record in blackboard/for-larry-escalations.json (the "needs you" feed)
# — visible in the Telegram/needs-you stream but never on the Approvals tab, and
# the larry-alerts scan above never sees it. This second source closes that blind
# spot: promote each OPEN, DECISION-class for-Larry record that has no matching
# registered approval, reusing the SAME add_pending/emit/dedup machinery.


def read_for_larry_records() -> list[dict[str, Any]]:
    """Return the OPEN for-Larry records (fail-safe: [] on any import/read
    error). Delegates to for_larry_escalations.list_open so the feed's own
    reader + schema stay the single source of truth."""
    try:
        import for_larry_escalations as fle  # noqa: E402
        return fle.list_open()
    except Exception as e:  # noqa: BLE001 — a bad feed read must never crash the tick
        log(f'for-larry feed read failed: {type(e).__name__}: {e}', 'WARN')
        return []


def is_forlarry_decision_class(
    record: dict[str, Any], heuristics: dict[str, Any],
) -> bool:
    """Conservative DECISION-class test for a for-Larry record. True only when
    the record is OPEN (resolved is not True) AND its source is a configured
    decision source (default: mirror-review) AND its id carries the matching
    `<source>:` prefix the no-session router writes. Action-needed / FYI records
    from other sources never qualify — a false positive is a dismissible tab
    card, but action-needed items are not binary decisions and must not promote.
    """
    if record.get('resolved') is True:
        return False
    sources = heuristics.get('for_larry_decision_sources') or list(
        DEFAULT_FORLARRY_DECISION_SOURCES)
    source = record.get('source')
    if source not in sources:
        return False
    rec_id = record.get('id')
    if not isinstance(rec_id, str) or not rec_id:
        return False
    return rec_id.startswith(str(source) + ':')


def forlarry_norm_id(record_id: str) -> str:
    """Normalize a for-Larry record id to the id shape a registered decision
    approval uses, so the two dedup against each other. The feed writes
    `mirror-review:<task>` (COLON, _no_session_record_id); a decision approval
    registered via _emit_no_session_decision_approval uses
    `mirror-review-<task>[-<head8>]` (HYPHEN). Swap only the FIRST ':' so the
    task portion (which itself never contains ':') is untouched."""
    return record_id.replace(':', '-', 1)


def is_forlarry_registered(norm_id: str, state: dict[str, Any]) -> bool:
    """True if a decision approval for this for-Larry record is already on the
    tab. Matches the normalized id exactly (bare form) OR as a `<norm_id>-`
    prefix (the `-<head8>` variant). Uses the same `-`-delimited boundary as
    outbox_notifier._reconcile_no_session_decision_on_merge, so PR #42 never
    false-matches PR #421."""
    if not norm_id:
        return False
    ids, _haystacks = registered_identities(state)
    return any(i == norm_id or i.startswith(norm_id + '-') for i in ids)


def forlarry_dedup_key(record_id: str) -> str:
    """Namespaced dedup-ledger key for a for-Larry record (kept distinct from
    larry-alert identities in the shared promoted ledger)."""
    return FORLARRY_LEDGER_PREFIX + record_id


def _healer_task_registered(task_id: str, state: dict[str, Any]) -> bool:
    """True if the healer's OWN deterministic promoted task_id is already an entry
    id in pending or history.

    Idempotency backstop for for-Larry promotions (defect 1): a for-Larry card is
    registered under the derived `unreg-approval-<hash>` id, NOT the hyphen
    decision-approval id, so is_forlarry_registered can never see the healer's own
    registration. The promoted ledger can also be churned by the retire pass. With
    the source record left OPEN (before the resolve-on-promote fix) that combined
    to re-promote the SAME record every tick (the sentinel-every-15-min defect).
    The derived task_id is stable over the dedup_key, so matching it here holds
    across ticks even when the ledger entry is gone — the healer never re-appends a
    card it already registered."""
    ids, _ = registered_identities(state)
    return task_id in ids


def build_for_larry_approval_payload(
    record: dict[str, Any], dedup_key: str,
) -> dict[str, Any]:
    """Build the approval_request payload for a stranded for-Larry decision
    record. target_agent is always 'beacon': on approve/reject the dashboard
    routes Larry's choice back to Beacon to formalize (act) or dismiss. Mirrors
    build_approval_payload's dedup/ledger/retire keys (deterministic task_id,
    promoted_from_alert, _subject, origin, bare_approvable=False) so the shared
    promote batch handles it identically; `_forlarry_norm_id` lets the batch's
    concurrency re-check use the colon/hyphen-aware match."""
    record_id = record.get('id') or dedup_key
    task_id = derive_task_id(dedup_key)
    pr_url = record.get('pr_url')
    headline = str(record.get('headline') or '').strip()
    context = str(record.get('context') or '').strip()
    task_label = record_id.split(':', 1)[1] if ':' in record_id else record_id
    summary = (
        f'Stranded Mirror review escalation for `{task_label}` needs your '
        'direction (promoted from the for-Larry feed; no APPROVAL_REQUEST was '
        'ever registered, so it never reached the Approvals tab). '
        'Approve = formalize and act on it; Reject = dismiss.'
        + (f'\nPR: {pr_url}' if pr_url else '')
    )
    prompt = (
        'This Mirror review escalation was written to the for-Larry needs-you '
        'feed (blackboard/for-larry-escalations.json) but never emitted as an '
        f'APPROVAL_REQUEST marker, so it never reached the Approvals tab; '
        f'{HEALER_SOURCE} promoted it.\n\n'
        f'Record id: {record_id}\n'
        + (f'Headline: {headline}\n' if headline else '')
        + (f'Context: {context}\n' if context else '')
        + (f'PR: {pr_url}\n' if pr_url else '')
        + '\nApprove = formalize this stranded Mirror escalation and act on it; '
        'Reject = dismiss it. Both route back to Beacon.'
    )
    return {
        'task_id': task_id,
        'summary': summary,
        'target_agent': 'beacon',
        'prompt': prompt,
        'task_type': 'direction-ask',
        'promoted_from_alert': dedup_key,
        'origin': HEALER_SOURCE,
        'bare_approvable': False,
        # Transient helper keys (stripped before add_pending): the record id is
        # recorded in the promoted ledger; the normalized id lets the promote
        # batch's concurrency re-check dedup against a hyphen approval Beacon may
        # register between this tick's snapshot and the locked append.
        '_subject': record_id,
        '_forlarry_norm_id': forlarry_norm_id(str(record_id)),
    }


def evaluate_for_larry(
    records: list[dict[str, Any]],
    heuristics: dict[str, Any],
    state: dict[str, Any],
    promoted: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the approval payloads to register from the for-Larry feed this
    tick. A record is promoted iff: OPEN + DECISION-class, not already promoted
    (namespaced ledger key), and not already registered as a decision approval
    (colon/hyphen-normalized match against pending+history). Pure — no I/O;
    main() does the registration + persistence via the shared promote batch."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not is_forlarry_decision_class(record, heuristics):
            continue
        record_id = str(record.get('id'))
        dedup_key = forlarry_dedup_key(record_id)
        if dedup_key in promoted or dedup_key in seen:
            continue
        # Idempotency (defect 1): skip when a matching approval already stands —
        # EITHER the hyphen decision approval Beacon's own no-session path
        # registers, OR this healer's OWN deterministic promoted approval. The
        # latter guard is what stops the sentinel-every-15-min re-promotion: the
        # retire pass can drop the ledger entry and is_forlarry_registered can't
        # see the `unreg-approval-<hash>` id the healer registers under.
        if is_forlarry_registered(forlarry_norm_id(record_id), state) or \
                _healer_task_registered(derive_task_id(dedup_key), state):
            continue
        payload = build_for_larry_approval_payload(record, dedup_key)
        payload['_source_ts'] = record.get('ts') or record.get('updated_at')
        out.append(payload)
        seen.add(dedup_key)
    return out


# -------------------- retire-on-resolution (decision 3) --------------------

def _ledger_entry_fields(key: str, value: Any) -> tuple[str, str, Optional[str]]:
    """Unpack a promoted-ledger entry into (subject, task_id, promoted_at),
    tolerating BOTH the legacy `{key: iso_ts}` shape (key is the raw subject)
    and the richer `{key: {task_id, subject, promoted_at}}` shape."""
    if isinstance(value, dict):
        subject = value.get('subject') or key
        task_id = value.get('task_id') or derive_task_id(key)
        return subject, task_id, value.get('promoted_at')
    # Legacy: the key WAS the dedup key (raw subject), value is the promote ts.
    return key, derive_task_id(key), (value if isinstance(value, str) else None)


def reconcile_retire(
    promoted: dict[str, Any],
    state: dict[str, Any],
    alerts: list[dict[str, Any]],
    heuristics: dict[str, Any],
    now: Optional[datetime] = None,
    gh_probe: Any = gh_ref_resolved,
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """Retire previously-promoted cards whose decision is now resolved.

    For each ledger entry, if a resolution signal (decision 1) now exists,
    `resolve` its pending beacon entry to history (drop-from-pending) and mark
    its task_id for a read_at clear; drop it from the ledger. An entry with NO
    signal is left untouched — a still-live unresolved ask is NEVER retired.

    Mutates `state` in place (via approval.resolve with state passed, so it does
    not self-save) and returns `(retired, remaining_ledger)` where `retired` is
    a list of `(task_id, reason)`. The caller persists state + ledger and clears
    read_at for the retired task_ids.
    """
    retired: list[tuple[str, str]] = []
    remaining: dict[str, Any] = {}
    for key, value in promoted.items():
        if not isinstance(key, str):
            continue
        subject, task_id, promoted_at = _ledger_entry_fields(key, value)
        reason = resolution_signal(
            subject, state, alerts, heuristics,
            after_ts=promoted_at, gh_probe=gh_probe,
        )
        if not reason:
            remaining[key] = value
            continue
        # Drop from pending -> history (status 'expired' = auto-retired, not a
        # user reject). resolve returns None if it was never in pending (e.g.
        # already cleared); we still retire it from the ledger + clear read_at.
        approval.resolve(
            task_id, 'expired',
            note=f'auto-retired by {HEALER_SOURCE}: {reason}',
            state=state,
        )
        retired.append((task_id, reason))
        log(f'retire-on-resolution: {key!r} task={task_id} ({reason})')
    return retired, remaining


def _clear_retired_read_at(task_ids: list[str]) -> int:
    """Set read_at on the chain_events rows for retired task_ids, reusing the
    existing heal_stale_approvals clear path (backup-first, batched). Returns
    the count cleared; 0 (with a WARN) if Supabase is unreachable — the
    heal_stale_approvals timer then finishes the clear on its next tick, since
    each retired entry is now resolved in beacon history. No raw Supabase write
    lives here."""
    if not task_ids:
        return 0
    import heal_stale_approvals as stale
    try:
        client = stale._connect_supabase()
    except Exception as e:  # noqa: BLE001
        log(f'retire read_at clear deferred (Supabase unavailable: '
            f'{type(e).__name__}: {e}); heal_stale_approvals will finish it',
            'WARN')
        return 0
    try:
        return stale.clear_resolved_by_task_id(client, task_ids)
    except Exception as e:  # noqa: BLE001
        log(f'retire read_at clear failed: {type(e).__name__}: {e}; '
            f'heal_stale_approvals will finish it', 'WARN')
        return 0


# -------------------- registration (side-effectful) --------------------

def _strip_helper_keys(payload: dict[str, Any]) -> Optional[str]:
    """Pop the transient helper keys in-place and return ``_source_ts``.

    ``_subject`` is dropped (recorded separately in the promoted ledger);
    ``promoted_from_alert`` stays on the payload so add_pending persists it
    under dispatch_payload, where the collision guard finds it on later ticks.
    """
    source_ts = payload.pop('_source_ts', None)
    payload.pop('_subject', None)
    payload.pop('_forlarry_norm_id', None)
    return source_ts


def emit_approval_event(payload: dict[str, Any], source_ts: Optional[str]) -> bool:
    """Slow side of registration: the chain_event tab-feed upsert. Pure-ish
    network I/O — runs OUTSIDE the shared approval-state lock. Returns True if
    the upsert succeeded; the pending write is best-effort and not gating."""
    kwargs = approval.build_approval_request_chain_event(payload, ts=source_ts)
    return chain_event_emit.emit_event(**kwargs)


def register_approval(payload: dict[str, Any], chat_id: Optional[int]) -> bool:
    """Register one approval_request: add_pending (Beacon state) + emit_event
    (the tab feed). Mirrors the bot's force_ask path. Returns True if the
    chain_event upsert succeeded (the tab write); the pending write is
    best-effort and not gating. Strips the internal helper keys before handing
    the payload to the helpers.

    Takes add_pending's own per-call state lock. `main()` does NOT use this for
    the promote batch — it holds the lock once around a fresh-reload re-check +
    add_pending (seam audit L2) and emits via :func:`emit_approval_event`
    afterwards. Retained for single-shot callers and tests.
    """
    source_ts = _strip_helper_keys(payload)
    approval.add_pending(payload, chat_id=chat_id)
    return emit_approval_event(payload, source_ts)


# -------------------- main --------------------

def _emit_self_failure(message: str, suggested_action: str) -> None:
    try:
        sys.path.insert(0, str(_SCRIPT_DIR))
        import larry_alerts as la  # noqa: E402
        la.append_alert(
            source=HEALER_SOURCE,
            severity='warning',
            message=message,
            subject=f'{HEALER_SOURCE}:self-failure',
            suggested_action=suggested_action,
        )
    except Exception as e:  # noqa: BLE001
        log(f'self-failure alert emit failed: {type(e).__name__}: {e}', 'WARN')


# -------------------- doorbell invariant reconcile (behavior A) -------------

def doorbell_counts() -> Optional[tuple[int, int]]:
    """(pending_approvals, escalations) the DOORBELL would count, read from the
    SAME State Log `waiting_on_larry` snapshot doorbell_notifier reads.

    The doorbell ('N items need your call') and the Approvals tab are fed by
    different paths; this healer enforces the invariant that every doorbell-
    counted item has a live, rendered approval_request on the tab. Measuring
    against the doorbell's OWN source of truth (not a re-derivation) is what makes
    the reconcile faithful. Returns None when the snapshot is unreadable/absent
    (no signal — never nags on a broken snapshot). Never raises."""
    try:
        import doorbell_notifier as dbell  # noqa: E402
        waiting = dbell.load_waiting()
    except Exception as e:  # noqa: BLE001
        log(f'doorbell snapshot read failed: {type(e).__name__}: {e}', 'WARN')
        return None
    if not isinstance(waiting, dict):
        return None

    def _n(v: Any) -> int:
        return v if isinstance(v, int) and v > 0 else 0

    return _n(waiting.get('pending_approvals')), _n(waiting.get('escalations'))


def _log_doorbell_reconcile(
    *, promoted_count: int, repair_failures: int, retired: int,
) -> None:
    """Reconcile heartbeat: log the doorbell's needs-your-call counts (from the
    State Log snapshot) alongside this tick's repair outcome.

    Actionable-only: NO alert fires here. A genuine unrepairable gap has already
    alerted via the null-chat / repair-failure paths; this line is observability
    so the doorbell total and the tab-repair activity can be reconciled from the
    log without a routine 'reconciled N items' nag."""
    counts = doorbell_counts()
    if counts is None:
        log(f'reconcile: doorbell snapshot unavailable; '
            f'promoted={promoted_count} repair_failures={repair_failures} '
            f'retired={retired}')
        return
    approvals, escalations = counts
    log(f'reconcile: doorbell counts {approvals} approval(s) + {escalations} '
        f'escalation(s) = {approvals + escalations} needs-your-call; this tick '
        f'promoted={promoted_count} repair_failures={repair_failures} '
        f'retired={retired}')


def main() -> int:
    if kill_switch().exists():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    heuristics = load_heuristics()
    if not CONFIG_FILE.exists():
        log(f'config {CONFIG_FILE.name} missing; using built-in defaults', 'WARN')

    alerts = read_alerts()
    state = approval.load_state()
    promoted = load_promoted()

    # Resolution probe shared by skip-before-promote (decision 1) and retire
    # (decision 3): consults gh for referenced PRs/issues, beacon history, and
    # later resolution alerts. Conservative — an undetermined probe is no signal.
    def _resolution_check(record: dict[str, Any]) -> Optional[str]:
        return resolution_signal(
            record, state, alerts, heuristics, after_ts=record.get('ts'),
        )

    # --- RETIRE-ON-RESOLUTION (decision 3) ---
    # Runs every tick regardless of whether anything is promoted, so a card
    # whose decision resolved out-of-band is cleared promptly.
    ledger_changed = False
    try:
        retired, promoted = reconcile_retire(promoted, state, alerts, heuristics)
    except Exception as e:  # noqa: BLE001
        log(f'retire pass failed: {type(e).__name__}: {e}', 'ERROR')
        retired = []
    if retired:
        ledger_changed = True
        # PR-E2 #48: the retire decisions above used slow gh probes against the
        # `state` snapshot WITHOUT the lock. Persist them under the shared lock by
        # re-loading FRESH state inside it and re-applying only the resolutions —
        # so we never clobber a concurrent add/resolve from the bot or notifier.
        # resolve() is idempotent (an entry already moved to history → no-op).
        with approval.state_lock():
            fresh = approval.load_state()
            for tid, reason in retired:
                approval.resolve(
                    tid, 'expired',
                    note=f'auto-retired by {HEALER_SOURCE}: {reason}',
                    state=fresh,
                )
            approval.save_state(fresh)
        _clear_retired_read_at([tid for tid, _ in retired])
        log(f'retired {len(retired)} resolved card(s) off the tab')

    # Recover the real APPROVAL_REQUEST markers Beacon emitted (read once,
    # outside the promote loop) so a promoted card carries Beacon's actual
    # proposal instead of a needs-triage reconstruction. Best-effort: a failure
    # here just means the alert-derived fallback is used.
    now = datetime.now(timezone.utc)
    try:
        recovered_markers = load_beacon_outbox_markers(
            now, heuristics['scan_window_hours'])
    except Exception as e:  # noqa: BLE001
        log(f'marker recovery scan failed: {type(e).__name__}: {e}; '
            f'falling back to alert-derived cards', 'WARN')
        recovered_markers = []

    def _marker_lookup(record: dict[str, Any]) -> Optional[dict[str, Any]]:
        return match_marker_for_record(record, recovered_markers)

    # --- SKIP-BEFORE-PROMOTE + PROMOTE (decisions 1 + 2) ---
    try:
        to_promote = evaluate(
            alerts, heuristics, state, promoted,
            resolution_check=_resolution_check,
            marker_lookup=_marker_lookup,
        )
    except Exception as e:  # noqa: BLE001
        log(f'evaluate failed: {type(e).__name__}: {e}', 'ERROR')
        if ledger_changed:
            save_promoted(promoted)
        _emit_self_failure(
            message=(
                f'{HEALER_SOURCE} failed while scanning larry-alerts for '
                f'unregistered direction-asks: {type(e).__name__}: {e}. '
                'Stranded approval-class alerts may not be reaching the tab.'
            ),
            suggested_action=(
                'Check ~/agents/logs/heal-unregistered-approval.log and run '
                'python3 ~/agent-core/scripts/heal_unregistered_approval.py.'
            ),
        )
        return 1

    # --- SECOND SOURCE: for-larry-escalations (stranded Mirror decisions) ---
    # Additive: the larry-alerts scan above is untouched. Read the OPEN for-Larry
    # records and promote the DECISION-class ones with no matching registered
    # approval, folding them into the same to_promote batch so the shared locked
    # add_pending/emit/ledger machinery below handles both sources uniformly.
    for_larry_records = read_for_larry_records()
    try:
        to_promote_for_larry = evaluate_for_larry(
            for_larry_records, heuristics, state, promoted)
    except Exception as e:  # noqa: BLE001
        log(f'for-larry evaluate failed: {type(e).__name__}: {e}', 'ERROR')
        to_promote_for_larry = []
    to_promote = to_promote + to_promote_for_larry

    if not to_promote:
        if ledger_changed:
            save_promoted(promoted)
        _log_doorbell_reconcile(
            promoted_count=0, repair_failures=0, retired=len(retired))
        log(f'tick: scanned {len(alerts)} alert(s) + '
            f'{len(for_larry_records)} for-larry record(s); nothing to promote; '
            f'retired {len(retired)}')
        return 0

    # NULL-CHAT FALLBACK (defect 2): resolve a real recipient BEFORE registering
    # anything. _chat_id() falls back to _primary_chat_id() (lowest allowed chat);
    # only when NEITHER an override NOR any allowed chat exists is it None. In that
    # case we must NOT register chat_id=None approvals (they never reach the DM
    # path and the card renders broken) — skip the whole promote batch and raise
    # ONE actionable alert (auto-repair genuinely failed; F).
    chat_id = _chat_id()
    if chat_id is None:
        if ledger_changed:
            save_promoted(promoted)
        log(f'no chat resolvable (OURLIBERTY_APPROVAL_HEALER_CHAT_ID unset and '
            f'TELEGRAM_ALLOWED_CHAT_IDS empty); skipping {len(to_promote)} '
            f'promotion(s) rather than register null-chat approvals', 'ERROR')
        _emit_self_failure(
            message=(
                f'{HEALER_SOURCE} could not promote {len(to_promote)} stranded '
                'needs-your-call item(s) to the Approvals tab: no Telegram chat '
                'is resolvable (OURLIBERTY_APPROVAL_HEALER_CHAT_ID unset and '
                'TELEGRAM_ALLOWED_CHAT_IDS empty), so registering them would '
                'create broken chat_id=None approvals. They stay counted by the '
                'doorbell but are NOT on the tab until a chat is configured.'
            ),
            suggested_action=(
                'Set TELEGRAM_ALLOWED_CHAT_IDS (or '
                'OURLIBERTY_APPROVAL_HEALER_CHAT_ID) in the healer environment, '
                'then re-run '
                'python3 ~/agent-core/scripts/heal_unregistered_approval.py.'
            ),
        )
        _log_doorbell_reconcile(
            promoted_count=0, repair_failures=len(to_promote),
            retired=len(retired))
        return 1

    promoted_count = 0
    # Seam audit L2 (symmetric twin of the retire pass / PR-E2 #48): evaluate()'s
    # is_already_registered check ran against the lock-free `state` snapshot
    # loaded at the top of this tick. Between that snapshot and the append below,
    # Beacon can emit the REAL APPROVAL_REQUEST for the same decision (its
    # add_pending writes under the lock) — and a narrow per-call lock would let
    # us append a duplicate card. Mirror the retire fix: take the shared lock
    # ONCE around { fresh load_state() → per-entry is_already_registered re-check
    # → add_pending(state=fresh) → save_state }, so the dedup check and the
    # append share one critical section. Keep the SLOW work (the chain_event
    # tab-feed upsert) OUTSIDE the lock and run it after the batch commits.
    registered: list[dict[str, Any]] = []
    with approval.state_lock():
        fresh = approval.load_state()
        for payload in to_promote:
            identity = payload.get('promoted_from_alert', '')
            subject = payload.get('_subject', identity)
            task_id = payload['task_id']
            # For-larry payloads carry a colon/hyphen-normalized id; their
            # concurrency re-check must use the same normalized match the
            # for-larry dedup uses (plus the healer's-own-task_id guard so a
            # churned ledger can't slip a duplicate through), NOT the alert
            # subject-substring guard.
            norm_id = payload.get('_forlarry_norm_id')
            if norm_id:
                already = is_forlarry_registered(norm_id, fresh) or \
                    _healer_task_registered(task_id, fresh)
            else:
                already = is_already_registered(subject, task_id, fresh)
            if already:
                # Beacon (or a concurrent healer) registered this decision after
                # our snapshot; appending now would double the card. Skip — the
                # real entry stands. Recording nothing in the promoted ledger is
                # safe: the now-present entry makes the re-check short-circuit on
                # the next tick too.
                log(f'promote-skip: {task_id} registered concurrently '
                    f'(identity={identity!r}); not duplicating the card')
                continue
            p = dict(payload)
            source_ts = _strip_helper_keys(p)
            try:
                approval.add_pending(p, chat_id=chat_id, state=fresh)
            except Exception as e:  # noqa: BLE001
                log(f'add_pending failed for {task_id}: {type(e).__name__}: {e}',
                    'ERROR')
                continue
            registered.append({
                'payload': p, 'source_ts': source_ts,
                'identity': identity, 'subject': subject, 'task_id': task_id,
                # For-larry payloads carry the norm-id marker; the raw record id
                # (subject) is what for_larry_escalations.clear() resolves on a
                # CONFIRMED render (defect 3). None for larry-alert-sourced cards.
                'forlarry_record_id': subject if norm_id else None,
            })
        approval.save_state(fresh)

    # Slow side OUTSIDE the lock: the chain_event tab-feed upsert. VERIFY RENDERED,
    # NOT JUST WRITTEN (defect 4): treat a promotion as successful ONLY when the
    # tab render is CONFIRMED — a non-null chat (guaranteed above) AND
    # emit_approval_event returning True. On a confirmed render, resolve the source
    # for-Larry escalation (defect 3) so the same underlying item stops being
    # double-counted (once as escalation, once as the promoted approval). On a
    # failed/again-unrendered write, do NOT resolve the source and do NOT record
    # the ledger — surface it as a repair-failure (F), never a silent 'ok'.
    repair_failures: list[str] = []
    for item in registered:
        task_id = item['task_id']
        identity = item['identity']
        try:
            emitted = emit_approval_event(item['payload'], item['source_ts'])
        except Exception as e:  # noqa: BLE001
            log(f'tab-write raised for {task_id}: {type(e).__name__}: {e}',
                'ERROR')
            emitted = False
        if not emitted:
            # The pending entry exists but the tab row is NOT confirmed. Leave the
            # source escalation OPEN (still doorbell-counted, so the item is not
            # silently lost) and do NOT record the ledger. The evaluate_for_larry
            # task_id guard prevents a duplicate re-append next tick; the
            # aggregated alert below makes this actionable.
            repair_failures.append(task_id)
            log(f'promote-repair-failure {task_id}: tab render NOT confirmed '
                f'(chain_event upsert failed) from identity={identity!r}', 'WARN')
            continue
        # CONFIRMED RENDER. Resolve the source for-Larry escalation so the
        # doorbell counts this item once (as the promoted approval) instead of
        # twice (escalation + approval) — defect 3.
        rec_id = item.get('forlarry_record_id')
        if rec_id:
            try:
                import for_larry_escalations as fle  # noqa: E402
                cleared = fle.clear(rec_id)
                log(f'resolve-on-promote: for-larry record {rec_id!r} '
                    f'{"cleared" if cleared else "already resolved"} for '
                    f'{task_id}')
            except Exception as e:  # noqa: BLE001
                log(f'resolve-on-promote failed for {rec_id!r}: '
                    f'{type(e).__name__}: {e}', 'WARN')
        # Record the promotion only on a CONFIRMED render. Keyed on the decision
        # identity; the entry carries task_id + subject so the retire pass can
        # later resolve + clear it without re-deriving from the raw alert.
        promoted[identity] = {
            'task_id': task_id,
            'subject': item['subject'],
            'promoted_at': datetime.now(timezone.utc).isoformat(),
        }
        promoted_count += 1
        log(f'promoted {task_id} (tab-write=ok) from decision '
            f'identity={identity!r}')

    save_promoted(promoted)
    # ACTIONABLE-ONLY (F): one aggregated alert when the tab render could not be
    # confirmed for one or more cards; nothing on the happy path.
    if repair_failures:
        _emit_self_failure(
            message=(
                f'{HEALER_SOURCE} registered {len(repair_failures)} promoted '
                'approval_request(s) in Beacon state but could NOT confirm the '
                'Approvals-tab render (chain_event upsert failed): '
                f'{", ".join(repair_failures)}. These needs-your-call items are '
                'counted by the doorbell but may not show on the tab.'
            ),
            suggested_action=(
                'Check ~/agents/logs/heal-unregistered-approval.log and Supabase '
                'chain_events connectivity, then re-run '
                'python3 ~/agent-core/scripts/heal_unregistered_approval.py.'
            ),
        )
    _log_doorbell_reconcile(
        promoted_count=promoted_count, repair_failures=len(repair_failures),
        retired=len(retired))
    log(f'done: promoted {promoted_count} direction-ask(s); '
        f'retired {len(retired)}; repair-failures {len(repair_failures)}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
