#!/usr/bin/env python3
"""missions_narrator.py — the Missions v2 Phase 4 Narrator pass (spec § 5).

Beacon authors the **meaning layer** on parked captures so Larry reads a card in
his terms — *what it is · why it matters · how careful to be · what to do* —
instead of machine metadata. One Beacon-owned step, reusing the
`ceo_digest_generator` read pattern (LLM voice with a deterministic raw
fallback) and `trust_policy.evaluate` for the risk dial.

Field contract authored here (spec § 4), all optional on a capture:

    "briefing": { "what": ..., "why": ..., "suggest": ... },
    "risk": "safe" | "medium" | "careful",
    "risk_note": "<one sentence>",          # required for medium/careful
    "recommended_action": "delegate" | "promote" | "drop" | "snooze",
    "briefing_provenance": { "by": "beacon", "model": ..., "at": <iso>,
                             "from_state": "parked" }

INVARIANTS (spec § 5):

  - SINGLE CAPTURES COMMITTER. The Narrator writes captures.json to disk
    ATOMICALLY (reusing heal_missions_card_gc.atomic_write_captures — the same
    tmp+rename primitive the ingest endpoint and GC healer use), but it NEVER
    git-commits. The Phase 1 GC healer (`heal_missions_card_gc.py`) is the sole
    committer; it batches the Narrator's disk delta into its next commit. There
    is no second writer to git — the invariant holds.

  - ABSENCE RENDERS NEUTRAL. A capture with no briefing surfaces as None on the
    card (the dashboard's `_meaning_layer_fields` validator enforces this); the
    Narrator only ever ADDS valid fields, never partial garbage.

  - RISK IS A VIEW OF trust_policy. `derive_risk` builds a dispatch-shaped task
    from the capture and runs `trust_policy.evaluate`, mapping
    auto_approve ⇒ safe, force_ask(reversible) ⇒ medium,
    force_ask(irreversible/outward/spendy) or reject ⇒ careful (§ 5). The risk
    LEVEL is always deterministic (testable with an injected policy); only the
    briefing prose + risk_note are LLM-authored, and both have a deterministic
    fallback so the pass runs head-less and under test.

  - VOICE IS ALWAYS BEACON (single voice, decision #5).

Trigger (spec § 5): event-driven on capture create / state change PLUS a
periodic sweep (same cadence family as the GC healer) so nothing stays
un-briefed. This module is the sweep; `needs_briefing` is idempotent so the
event-driven and periodic paths converge on the same result.

stdlib + supabase-py (via chain_event_emit, lazily/optionally) + the `claude`
CLI (optional; deterministic fallback when absent).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Sibling scripts/ on path so imports resolve when run by systemd or in tests.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import trust_policy  # noqa: E402
from heal_missions_card_gc import (  # noqa: E402
    atomic_write_captures,
    captures_path,
    load_repo_paths,
    log,
    read_captures_registry,
)
from test_isolation_guard import refuse_under_test  # noqa: E402

# Beacon is the single voice (decision #5). Opus for the briefing authoring —
# the meaning layer is the operator's whole read of the card, worth the better
# model (the spec's provenance example pins claude-opus-4-8).
NARRATOR_MODEL = 'claude-opus-4-8'
NARRATOR_BY = 'beacon'
CLAUDE_TIMEOUT_SEC = 180

# Per-tick authoring bound (spec § 3). The folded sweep (run by the GC healer's
# timer) authors at most this many captures per tick so an LLM-slow tick can't
# run unboundedly; the remainder briefs on the next tick (needs_briefing is
# idempotent, so deferral is safe).
NARRATOR_MAX_PER_TICK = 8

VALID_RISKS = ('safe', 'medium', 'careful')

# Keywords in a capture's title/note that mark the proposed work as
# irreversible / outward-facing / spendy — the § 5 escalator that pushes a
# force_ask from medium up to careful. Matched on WORD BOUNDARIES (\b…\b), not
# bare substrings: bare-substring matching made "drops comments" trip "drop" and
# would have escalated most captures to careful (also colliding with the `drop`
# recommended-action vocabulary). A false positive only over-warns (careful
# instead of medium), never under-warns.
_CAREFUL_KEYWORDS = (
    'delete', 'drop', 'remove', 'destroy', 'wipe', 'purge', 'truncate',
    'rm -rf', 'production', 'deploy', 'release', 'migrate', 'migration',
    'credential', 'secret', 'token', 'password', 'api key', 'restart',
    'reboot', 'shutdown', 'kill', 'send', 'email', 'notify', 'customer',
    'payment', 'charge', 'refund', 'spend', 'purchase', 'billing',
    'irreversible', 'force-push', 'force push', 'rotate',
)
_CAREFUL_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(kw) for kw in _CAREFUL_KEYWORDS) + r')\b',
    re.IGNORECASE,
)


# ---------------- risk derivation (trust_policy view) ----------------


def capture_to_task(capture: dict[str, Any]) -> dict[str, Any]:
    """Build the dispatch-shaped task `trust_policy.evaluate` consumes from a
    capture. A parked capture proposes work the team would take on (the
    recommended_action delegates it), so the task is shaped like the dispatch
    that delegation would emit: Beacon → Forge in the capture's origin repo."""
    origin = capture.get('origin') if isinstance(capture.get('origin'), dict) else {}
    return {
        'source': 'beacon',
        'target_agent': 'forge',
        'task_type': capture.get('task_type') or 'feature-development',
        'target_repo': origin.get('repo'),
        'changed_files': capture.get('changed_files') or [],
    }


def classify_careful(capture: dict[str, Any]) -> bool:
    """True if the proposed work looks irreversible / outward-facing / spendy —
    the § 5 escalator from medium to careful. Scans title + note + label."""
    haystack = ' '.join(
        str(capture.get(k) or '') for k in ('title', 'note', 'label')
    )
    return _CAREFUL_PATTERN.search(haystack) is not None


def map_risk(action: str, careful: bool) -> str:
    """Map a trust_policy action + careful-class flag to a risk level (§ 5).

      auto_approve                         ⇒ safe
      force_ask (reversible)               ⇒ medium
      force_ask (irreversible/outward/...) ⇒ careful
      reject (or anything unknown)         ⇒ careful  (fail toward caution)
    """
    if action == 'auto_approve':
        return 'safe'
    if action == 'force_ask':
        return 'careful' if careful else 'medium'
    # reject — or any unexpected action — is the most-cautious bucket.
    return 'careful'


def derive_risk(
    capture: dict[str, Any], policy: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    """Return (risk, careful) for a capture. `policy` defaults to a fresh
    `trust_policy.load_policy()` (empty → force_ask), so edits to
    config/trust-policy.json take effect without a restart; tests inject one."""
    action, _rule = trust_policy.evaluate(capture_to_task(capture), policy)
    careful = classify_careful(capture)
    return map_risk(action, careful), careful


# ---------------- recommended action ----------------


def derive_recommended_action(capture: dict[str, Any], risk: str) -> str:
    """The card's primary one-click suggestion (§ 4 / § 7). Recommend-first:
    the team proposes; nothing auto-fires. Default is `delegate` (hand the work
    to the team); an aging capture nobody has acted on is more likely a `drop`
    candidate, surfaced for Larry's one-click — but never auto-executed."""
    if capture.get('aging') is True:
        return 'drop'
    return 'delegate'


# ---------------- briefing authoring (LLM voice + raw fallback) ----------------


def render_raw_briefing(
    capture: dict[str, Any], risk: str, careful: bool,
) -> dict[str, str]:
    """Deterministic plain-English briefing — the fallback when the LLM voice is
    unavailable (and the value tests assert against). Plain operator language;
    no task ids / branch names / agent jargon."""
    title = (capture.get('title') or 'this idea').strip()
    note = (capture.get('note') or '').strip()
    origin = capture.get('origin') if isinstance(capture.get('origin'), dict) else {}
    repo = origin.get('repo')

    what = title if not note else f'{title} — {note}'
    where = f' (in {repo})' if repo else ''
    why = (
        f"It's been parked{where} and is worth a decision before it goes stale."
        if capture.get('aging') is True
        else f"A captured idea{where} waiting on a decision."
    )
    if risk == 'careful':
        suggest = 'Have the team look at this carefully before any change — it could be hard to undo.'
    elif risk == 'medium':
        suggest = 'Have the team run it down and propose a fix.'
    else:
        suggest = 'Low-risk — let the team take it from here.'
    return {'what': what, 'why': why, 'suggest': suggest}


def build_risk_note(
    capture: dict[str, Any], risk: str, careful: bool,
) -> Optional[str]:
    """One-sentence "the catch" note. Required for medium/careful (§ 4); None for
    safe (nothing to caution about)."""
    if risk == 'safe':
        return None
    if risk == 'careful':
        if careful:
            return ("This touches something hard to undo (data, deploys, money, "
                    "or outside contact) — worth a careful look before acting.")
        return "The team can't auto-approve this class of change — it needs your eyes."
    return "A change here has wider effects than it first appears — worth a quick check."


def build_briefing_prompt(
    capture: dict[str, Any], events: list[dict[str, Any]], risk: str,
) -> str:
    """CEO-voice authoring prompt for the briefing (mirrors ceo_digest's
    build_prompt shape). Beacon voice, plain outcomes, JSON-out for parse."""
    origin = capture.get('origin') if isinstance(capture.get('origin'), dict) else {}
    facts = {
        'title': capture.get('title'),
        'note': capture.get('note'),
        'repo': origin.get('repo'),
        'risk_level': risk,
        'context_events': [
            {'type': e.get('event_type'), 'summary': (
                e.get('payload', {}).get('summary')
                if isinstance(e.get('payload'), dict) else None)}
            for e in events[:8]
        ],
    }
    return (
        "You are Beacon, the operator's manager. Write a 3-part briefing about a "
        "parked idea so a busy CEO can decide on it in seconds — in his terms, "
        "never engineering jargon.\n\n"
        "Return ONLY a JSON object with exactly these keys (each one short, "
        "plain English, no markdown):\n"
        '  "what":    one sentence — what this is.\n'
        '  "why":     one sentence — why it matters to the business.\n'
        '  "suggest": one sentence — what you recommend doing.\n\n'
        "Never mention task ids, branches, PRs, commits, agents, or risk codes. "
        f"The risk level is already computed as \"{risk}\"; reflect its caution "
        "in the suggestion's tone but don't name it.\n\n"
        "Here are the facts (JSON):\n"
        f"{json.dumps(facts, indent=2)}\n\n"
        "Write the briefing now. Output ONLY the JSON object."
    )


def _strip_code_fences(text: str) -> str:
    """Return the inner content of the first markdown code fence (```json … ```
    or bare ``` … ```) if one is present, else the text stripped. The model
    sometimes wraps its JSON in a fence; the strict parse choked on the
    backticks (spec § 5)."""
    fence = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text.strip()


def _first_balanced_object(text: str) -> Optional[str]:
    """Return the first balanced ``{…}`` substring, tracking string literals and
    backslash escapes so braces inside strings don't miscount. None if there is
    no balanced object — this is what lets a JSON object wrapped in leading/
    trailing prose still be extracted."""
    start = text.find('{')
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _strip_trailing_commas(text: str) -> str:
    """Drop a trailing comma before a closing ``}``/``]`` — a common model JSON
    slip that strict json.loads rejects."""
    return re.sub(r',(\s*[}\]])', r'\1', text)


def parse_briefing_json(text: str) -> Optional[dict[str, Any]]:
    """Tolerant parse of a model briefing reply (spec § 5). Accepts raw JSON,
    fenced JSON, a JSON object wrapped in prose, and a trailing-comma slip.
    Returns the parsed dict, or None when no JSON object is extractable — only
    then does the caller fall through to the deterministic raw briefing."""
    if not text or not text.strip():
        return None
    stripped = _strip_code_fences(text)
    candidates = [stripped]
    obj = _first_balanced_object(stripped)
    if obj is not None:
        candidates.append(obj)
        candidates.append(_strip_trailing_commas(obj))
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def generate_briefing_voice(prompt: str) -> Optional[dict[str, str]]:
    """Invoke the claude CLI to author the briefing. Returns the parsed
    {what,why,suggest} dict, or None on any failure (timeout, non-zero exit,
    parse failure, missing key) so the caller falls through to the raw
    rendering. Never raises. Mirrors ceo_digest_generator.generate_ceo_voice."""
    refuse_under_test('claude-spawn')
    try:
        proc = subprocess.run(
            ['claude', '--print', '--model', NARRATOR_MODEL,
             '--output-format', 'json', prompt],
            capture_output=True, text=True,
            timeout=CLAUDE_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f'narrator: claude invocation failed: {type(e).__name__}: {e}')
        return None
    if proc.returncode != 0:
        log(f'narrator: claude exited {proc.returncode}; using raw briefing')
        return None
    try:
        envelope = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError:
        log('narrator: claude output was not JSON; using raw briefing')
        return None
    text = (envelope.get('result') or '').strip() if isinstance(envelope, dict) else ''
    if not text:
        return None
    briefing = parse_briefing_json(text)
    if not isinstance(briefing, dict):
        log('narrator: claude result had no extractable JSON briefing; using raw briefing')
        return None
    out = {}
    for k in ('what', 'why', 'suggest'):
        v = briefing.get(k)
        if not (isinstance(v, str) and v.strip()):
            return None
        out[k] = v.strip()
    return out


# ---------------- the authoring pass ----------------


def author_meaning_layer(
    capture: dict[str, Any],
    events: Optional[list[dict[str, Any]]] = None,
    *,
    now: Optional[datetime] = None,
    policy: Optional[dict[str, Any]] = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Author the full meaning layer for one capture and return the field dict
    (briefing/risk/risk_note/recommended_action/briefing_provenance). Pure —
    does not mutate the capture or touch disk; the caller decides whether to
    write it back. `use_llm=False` forces the deterministic raw briefing (the
    test path and the head-less fallback)."""
    events = events or []
    now = now or datetime.now(timezone.utc)

    risk, careful = derive_risk(capture, policy)
    recommended_action = derive_recommended_action(capture, risk)

    briefing = None
    if use_llm:
        briefing = generate_briefing_voice(
            build_briefing_prompt(capture, events, risk))
    if briefing is None:
        briefing = render_raw_briefing(capture, risk, careful)

    fields: dict[str, Any] = {
        'briefing': briefing,
        'risk': risk,
        'recommended_action': recommended_action,
        'briefing_provenance': {
            'by': NARRATOR_BY,
            'model': NARRATOR_MODEL if use_llm else 'raw',
            'at': now.isoformat(),
            'from_state': capture.get('state'),
        },
    }
    risk_note = build_risk_note(capture, risk, careful)
    if risk_note is not None:
        fields['risk_note'] = risk_note
    return fields


def needs_briefing(capture: dict[str, Any]) -> bool:
    """True if a parked capture needs (re)briefing — missing briefing, or its
    provenance was stamped from a different state than the capture's current one
    (§ 4: fields regenerate when state/context changes). Idempotent: a capture
    already briefed for its current state returns False, so the periodic sweep
    and the event-driven path converge."""
    if capture.get('state') != 'parked':
        return False
    if not isinstance(capture.get('briefing'), dict):
        return True
    provenance = capture.get('briefing_provenance')
    if not isinstance(provenance, dict):
        return True
    return provenance.get('from_state') != capture.get('state')


def author_captures_in_registry(
    registry: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    client: Optional[Any] = None,
    use_llm: bool = True,
    policy: Optional[dict[str, Any]] = None,
    max_per_tick: int = NARRATOR_MAX_PER_TICK,
) -> tuple[int, int]:
    """Author the meaning layer onto captures in ``registry`` that need briefing,
    bounded by ``max_per_tick`` (spec § 3). Mutates the registry IN PLACE — the
    caller (the GC healer) owns the single atomic write + git commit, so this
    never becomes a second writer of captures.json (single-committer invariant).

    Returns ``(briefed, deferred)``: how many captures were authored this tick
    and how many still need briefing afterward (the remainder briefs next tick;
    needs_briefing is idempotent). Fail-safe per capture: an author error on one
    capture is logged and skipped — never aborts the sweep, never corrupts the
    registry (the deterministic fallback already guarantees a usable briefing).

    The per-tick bound counts AUTHORING ATTEMPTS, not successes, so a tick can't
    spend more than ``max_per_tick`` LLM round-trips even if some error out."""
    now = now or datetime.now(timezone.utc)
    captures = registry.get('captures')
    if not isinstance(captures, list):
        return (0, 0)
    pending = [c for c in captures if isinstance(c, dict) and needs_briefing(c)]

    attempted = 0
    briefed = 0
    for cap in pending:
        if attempted >= max_per_tick:
            break
        cid = cap.get('id')
        if not cid:
            continue  # no stable id — can't author a meaningful card; skip.
        attempted += 1
        try:
            events = fetch_capture_events(cid, client=client)
            fields = author_meaning_layer(
                cap, events, now=now, policy=policy, use_llm=use_llm)
        except Exception as e:  # noqa: BLE001 — per-capture fail-safe
            log(f'narrator: author failed for capture {cid}: '
                f'{type(e).__name__}: {e} — skipped (retries next tick)')
            continue
        # risk_note is optional (absent for safe) — clear any stale one so a
        # capture dropping medium→safe doesn't keep a dangling note.
        cap.pop('risk_note', None)
        cap.update(fields)
        briefed += 1

    deferred = max(0, len(pending) - briefed)
    log(f'narrator sweep: briefed {briefed} capture(s); deferred {deferred} '
        f'(max {max_per_tick}/tick)')
    return (briefed, deferred)


def fetch_capture_events(
    capture_id: str, client: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Best-effort fetch of a capture's chain_events context (keyed by task_id
    == capture id). Any failure (no client, no creds, query error) yields [] —
    the briefing degrades to capture-only context rather than crashing. Lazy
    import so the module loads without supabase configured."""
    try:
        import chain_event_emit as cee  # noqa: E402
    except ImportError:
        return []
    cli = client if client is not None else cee._get_client()
    if cli is None:
        return []
    try:
        resp = (
            cli.table('chain_events')
            .select('event_type,task_id,agent,ts,payload')
            .eq('task_id', capture_id)
            .order('ts', desc=True)
            .execute()
        )
    except Exception as e:  # noqa: BLE001 — context fetch is best-effort
        log(f'narrator: chain_events fetch for {capture_id} failed: '
            f'{type(e).__name__}: {e}')
        return []
    return list(getattr(resp, 'data', None) or [])


def run(
    *,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    client: Optional[Any] = None,
    use_llm: bool = True,
    policy: Optional[dict[str, Any]] = None,
    captures_file: Optional[Path] = None,
) -> int:
    """Sweep parked captures, author the meaning layer on any that need it, and
    write the delta to disk ATOMICALLY (never git-commit — the GC healer batches
    the commit, preserving the single-committer invariant).

    Returns the count of captures (re)briefed this run. A missing/malformed
    captures.json is fail-safe: log + return 0 (never corrupts the file)."""
    now = now or datetime.now(timezone.utc)
    path = captures_file or captures_path(load_repo_paths())
    if path is None:
        log('narrator: no captures.json path (agent-core not configured); skip')
        return 0

    registry = read_captures_registry(path)
    if registry is None:
        # Malformed — read_captures_registry already logged; never write onto it.
        return 0

    # Author phase (slow): each capture may invoke claude up to CLAUDE_TIMEOUT_SEC,
    # so the whole sweep can hold the process for N*180s. We accumulate the
    # meaning-layer fields into a delta map keyed by capture id WITHOUT writing
    # this stale snapshot back. The fresh re-read below is what we actually mutate
    # and persist, so concurrent writers (snooze/ingest endpoints, the GC healer)
    # are never clobbered by a stale full-registry write.
    deltas: dict[str, dict[str, Any]] = {}
    for cap in registry.get('captures', []):
        if not isinstance(cap, dict) or not needs_briefing(cap):
            continue
        cid = cap.get('id')
        if not cid:
            continue  # no stable key to re-match across the fresh read — skip.
        events = fetch_capture_events(cid, client=client)
        deltas[cid] = author_meaning_layer(
            cap, events, now=now, policy=policy, use_llm=use_llm)

    if not deltas:
        log('narrator: nothing to brief this sweep')
        return 0

    if dry_run:
        log(f'narrator: dry-run — would brief {len(deltas)} capture(s)')
        return len(deltas)

    # Write phase: re-read the registry FRESH immediately before writing and
    # apply only the meaning-layer deltas to captures still present and still
    # needs_briefing() in the fresh copy. This collapses the lost-update window
    # from the whole author sweep (N*180s) down to this read→write span, so any
    # state another writer landed mid-sweep (a snooze's snoozed_until, a capture
    # ingested late, a GC age) survives instead of being silently reverted.
    fresh = read_captures_registry(path)
    if fresh is None:
        # File went malformed/unreadable mid-sweep — never write onto it.
        log('narrator: captures.json unreadable at write time; skipping write')
        return 0

    briefed = 0
    for cap in fresh.get('captures', []):
        if not isinstance(cap, dict):
            continue
        fields = deltas.get(cap.get('id'))
        if fields is None or not needs_briefing(cap):
            continue
        # risk_note is optional (absent for safe) — clear any stale one so a
        # capture that drops from medium→safe doesn't keep a dangling note.
        cap.pop('risk_note', None)
        cap.update(fields)
        briefed += 1

    if briefed:
        atomic_write_captures(path, fresh)
        log(f'narrator: briefed {briefed} capture(s) → {path} (no commit; '
            f'GC healer batches the commit)')
    else:
        log('narrator: deltas superseded by concurrent writes; nothing written')
    return briefed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Author the Missions v2 meaning layer on parked captures.')
    parser.add_argument('--dry-run', action='store_true',
                        help='compute briefings but do not write captures.json')
    parser.add_argument('--no-llm', action='store_true',
                        help='use the deterministic raw briefing (no claude CLI)')
    args = parser.parse_args(argv)
    n = run(dry_run=args.dry_run, use_llm=not args.no_llm)
    log(f'narrator: done ({n} briefed)')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'narrator FATAL: {type(exc).__name__}: {exc}')
        sys.exit(1)
