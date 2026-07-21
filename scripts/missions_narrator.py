#!/usr/bin/env python3
"""missions_narrator.py — the Missions v2 Phase 4 Narrator pass (spec § 5).

Beacon authors the **meaning layer** on parked captures so Larry reads a card in
his terms — *what it is · why it matters · how careful to be · what to do* —
instead of machine metadata. One Beacon-owned step, reusing the
`ceo_digest_generator` read pattern (LLM voice with a deterministic raw
fallback) and `trust_policy.evaluate` for the risk dial.

Projects-v3 P2 (Contract A) extends the same meaning layer to the rest of the
funnel: orphan-derived and team-suggested intake items, which live as `proposed`
missions in missions.json. Those funnel missions get the identical
briefing/risk field contract via `author_missions_in_registry` — same GC-tick
schedule, same single-committer invariant, same fail-safe raw fallback — so an
orphan/suggested card is no longer raw machine names. See the
"funnel-mission meaning layer" section below.

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

import active_tier  # noqa: E402
import routing_validator  # noqa: E402  # canonical_repo (origin.repo spelling drift)
import trust_policy  # noqa: E402
from heal_missions_card_gc import (  # noqa: E402
    atomic_write_captures,
    captures_path,
    load_repo_paths,
    log,
    pr_number_from_url,
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

# Terminal completion states (Phase S S3). A card the GC healer has closed
# (`done`) or moved to closeout review (`review_close`) is past the meaning-layer
# lifecycle: its `briefing` was authored for `parked`, so a naive state-mismatch
# check would re-brief it forever. Skip these states so a closed card is never
# re-authored (the closeout briefing on a `review_close` card is owned by
# `author_closeout`, not this parked-card sweep).
_NARRATOR_SKIP_STATES = frozenset({'done', 'review_close'})

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
        # Canonicalized: origin.repo is derived from the emitting machine's checkout
        # DIRECTORY NAME, so a droplet-emitted capture carries 'agent-core' while the
        # desktop emits 'ourliberty-agent-core'. safe_write_inbox's repo-scope check
        # compares target_repo against allowed_repos (full names only), so the bare
        # form was rejected at dispatch. See routing_validator.canonical_repo.
        'target_repo': routing_validator.canonical_repo(origin.get('repo')),
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

      careful-class (irreversible/outward/spendy) ⇒ careful  (ALWAYS — even when
          the repo's rule auto_approves: an auto-firing deploy/migrate/credential
          card must still READ as careful so the board never under-warns)
      auto_approve (reversible)            ⇒ safe
      force_ask (reversible)               ⇒ medium
      reject (or anything unknown)         ⇒ careful  (fail toward caution)
    """
    # The careful escalator wins over the trust action. Once an auto_approve rule
    # exists (e.g. agent-core code), a careful card would otherwise mislabel as
    # `safe` — map_risk used to read `auto_approve ⇒ safe` unconditionally, which
    # was written when no auto_approve rules existed. Surfacing careful work as
    # `careful` keeps the board honest, and is the same signal the board-drain
    # uses (classify_careful) to keep such work OUT of the autonomous path.
    if careful:
        return 'careful'
    if action == 'auto_approve':
        return 'safe'
    if action == 'force_ask':
        return 'medium'
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


def claude_json_roundtrip(
    prompt: str, *, model: str = NARRATOR_MODEL,
) -> Optional[dict[str, Any]]:
    """One guarded ``claude`` CLI round-trip → the JSON object the model returned
    (ANY shape: prose dict, or a dict carrying lists/booleans), or None on any
    failure (timeout, non-zero exit, non-JSON envelope, no extractable JSON).
    Never raises. This is the SINGLE allowlisted ``claude`` spawn for the
    narrator family; callers that need a typed/validated shape layer their own
    checks on top of it (e.g. ``generate_briefing_voice`` enforces flat string
    keys; the closeout author normalizes a follow-ups list). ``model`` lets a
    caller pick its own model (e.g. the closeout author's ``CLOSEOUT_MODEL``)
    while reusing this one chokepoint sink."""
    refuse_under_test('claude-spawn')
    # Per-task tier dispatch (spec §10-W4): round-robin a tier for this run and
    # skip cleanly when none is available (fall through to the raw briefing).
    env, tier = active_tier.select_durable_claude_env()
    if env is None:
        log('narrator: no dispatch tier available; using raw briefing')
        return None
    try:
        proc = subprocess.run(
            ['claude', '--print', '--model', model,
             '--output-format', 'json', prompt],
            capture_output=True, text=True,
            env=env,
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
    # §8/§10-W4: stamp an account-tagged cost row so the narrator's burn is
    # visible per-tier (this path does not route through inbox_watcher).
    if isinstance(envelope, dict):
        try:
            active_tier.append_cost_row(
                tier, model=model,
                cost_usd=envelope.get('total_cost_usd'),
                usage=envelope.get('usage'),
                agent='missions-narrator', source='missions-narrator')
        except Exception:  # noqa: BLE001 — cost ledger must never break a run
            pass
    return parse_briefing_json(text)


def generate_briefing_voice(
    prompt: str, *, keys: tuple[str, ...] = ('what', 'why', 'suggest'),
) -> Optional[dict[str, str]]:
    """Invoke the claude CLI to author a JSON object with exactly ``keys``.
    Returns the parsed dict (all keys present, non-empty strings), or None on any
    failure (timeout, non-zero exit, parse failure, missing key) so the caller
    falls through to the deterministic raw rendering. Never raises. Mirrors
    ceo_digest_generator.generate_ceo_voice. ``keys`` is parameterized so both the
    parked-card briefing ({what,why,suggest}) and the S3 closeout
    ({what,outcome,note}) share one CLI round-trip path."""
    briefing = claude_json_roundtrip(prompt)
    if not isinstance(briefing, dict):
        log('narrator: claude result had no extractable JSON briefing; using raw briefing')
        return None
    out = {}
    for k in keys:
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


# ---------------- closeout authoring (Phase S S3) ----------------
#
# When a medium/careful card's spawned work reaches verified-merged, the GC
# healer routes it to closeout (not auto-close): the Narrator authors a
# plain-language *closeout briefing* (what we did · the outcome · anything to
# know) and the card moves to `review_close` awaiting Larry's ack. This reuses
# the Phase 4.1 fold — same claude round-trip + deterministic raw fallback — with
# closeout-shaped keys.

CLOSEOUT_KEYS = ('what', 'outcome', 'note')


def render_raw_closeout(
    capture: dict[str, Any], pr_url: Optional[str],
) -> dict[str, str]:
    """Deterministic plain-English closeout — the fallback when the LLM voice is
    unavailable (and the value tests assert against). Operator language; the PR
    reference is the only identifier (a number Larry can click), never task ids or
    branches."""
    title = (capture.get('title') or 'this idea').strip()
    n = pr_number_from_url(pr_url)
    pr_ref = f' (PR #{n})' if n else ''
    what = f'The team picked up "{title}" and carried it through to a shipped change.'
    outcome = (f'It merged{pr_ref} and is live now.' if pr_ref
               else 'It merged and is live now.')
    note = ('This one was above the auto-close bar, so it is parked for your '
            'okay before it closes — give it a quick look and clear it.')
    return {'what': what, 'outcome': outcome, 'note': note}


def build_closeout_prompt(
    capture: dict[str, Any], events: list[dict[str, Any]], pr_url: Optional[str],
) -> str:
    """CEO-voice authoring prompt for the closeout (mirrors build_briefing_prompt).
    Beacon voice, plain outcomes, JSON-out for parse."""
    origin = capture.get('origin') if isinstance(capture.get('origin'), dict) else {}
    facts = {
        'title': capture.get('title'),
        'note': capture.get('note'),
        'repo': origin.get('repo'),
        'pr_number': pr_number_from_url(pr_url),
        'context_events': [
            {'type': e.get('event_type'), 'summary': (
                e.get('payload', {}).get('summary')
                if isinstance(e.get('payload'), dict) else None)}
            for e in events[:8]
        ],
    }
    return (
        "You are Beacon, the operator's manager. A piece of work the operator "
        "captured has just shipped (merged). Write a 3-part closeout so a busy CEO "
        "can review and sign off in seconds — in his terms, never engineering "
        "jargon.\n\n"
        "Return ONLY a JSON object with exactly these keys (each one short, plain "
        "English, no markdown):\n"
        '  "what":    one sentence — what the team did.\n'
        '  "outcome": one sentence — the result (that it shipped / is live).\n'
        '  "note":    one sentence — anything to know before closing it out.\n\n'
        "You MAY reference the PR number if present, but never task ids, branches, "
        "commits, agents, or risk codes.\n\n"
        "Here are the facts (JSON):\n"
        f"{json.dumps(facts, indent=2)}\n\n"
        "Write the closeout now. Output ONLY the JSON object."
    )


def author_closeout(
    capture: dict[str, Any],
    pr_url: Optional[str],
    now: Optional[datetime] = None,
    *,
    events: Optional[list[dict[str, Any]]] = None,
    client: Optional[Any] = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Author the S3 closeout for one merged card and return the field dict
    (``closeout`` + ``closeout_provenance``) for the GC healer's reconcile to
    merge onto the capture. Pure — does not mutate the capture or touch disk; the
    healer owns the single write + commit (single-committer invariant). Signature
    matches the healer's ``closeout_fn(capture, pr_url, now)`` seam.

    ``use_llm=True`` (production) tries the claude voice and falls back to the
    deterministic raw closeout on any failure, so the author never raises and a
    head-less/test run still produces a usable closeout. Context events are
    fetched best-effort (keyed by capture id) when not injected."""
    now = now or datetime.now(timezone.utc)
    if events is None:
        cid = capture.get('id')
        events = fetch_capture_events(cid, client=client) if cid else []

    closeout = None
    if use_llm:
        closeout = generate_briefing_voice(
            build_closeout_prompt(capture, events, pr_url), keys=CLOSEOUT_KEYS)
    authored_by_llm = closeout is not None
    if closeout is None:
        closeout = render_raw_closeout(capture, pr_url)

    return {
        'closeout': closeout,
        'closeout_provenance': {
            'by': NARRATOR_BY,
            'model': NARRATOR_MODEL if authored_by_llm else 'raw',
            'at': now.isoformat(),
            'from_state': capture.get('state'),
            'pr_url': pr_url,
        },
    }


def needs_briefing(capture: dict[str, Any]) -> bool:
    """True if a capture needs (re)briefing — missing briefing, or its provenance
    was stamped from a different state than the capture's current one (§ 4: fields
    regenerate when state/context changes). The state comparison is what makes a
    promote/drop re-author: those endpoints change the capture's state, so the
    next folded sweep sees state != briefing_provenance.from_state and rewrites the
    meaning layer to match the new state. Idempotent: a capture already briefed for
    its current state returns False, so the periodic sweep and the event-driven
    path converge and an unchanged card is never re-authored.

    (Snooze keeps state == 'parked' — it only defers resurfacing via
    `snoozed_until` — so it is intentionally NOT a re-brief trigger here: the
    card's meaning is unchanged by a snooze.)

    A card in a terminal completion state (`done`/`review_close`, Phase S S3) is
    never re-briefed: its parked-state briefing is final, and a `review_close`
    card carries its own closeout authored by `author_closeout`. Without this
    guard the state-mismatch check below would re-brief every closed card on
    every sweep (its from_state is `parked`, its state is now terminal)."""
    if capture.get('state') in _NARRATOR_SKIP_STATES:
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


# ---------------- funnel-mission meaning layer (orphan + suggested) ----------
#
# Contract A (projects-v3 P2 § 4): the Narrator briefs not only parked captures
# but the orphan-derived and team-suggested funnel items, which live as
# `proposed` missions in missions.json (`heal_orphan_autoregister` registers an
# orphan task as a proposed mission; Beacon/Medic/Pulse land a suggestion the
# same way). Same field contract (briefing/risk/risk_note/recommended_action/
# briefing_provenance), same fail-safe authoring (deterministic raw fallback —
# never a raw-metadata leak), same single-committer invariant: this only mutates
# the in-memory registry; the GC healer is the SOLE missions.json committer and
# batches the briefing delta into its next commit.
#
# A funnel mission's lifecycle "state" is its `phase`; only a `proposed` mission
# is an un-triaged funnel card worth briefing. Once accepted it leaves the
# intake lane (phase != proposed); once dead (acknowledged/retired) it is
# auto-filtered out of the funnel (dashboard `_proposed_mission_is_dead`).
# `mission_needs_briefing` is idempotent so the event-driven and periodic paths
# converge, mirroring `needs_briefing` for captures.

_MISSION_FUNNEL_PHASE = 'proposed'

# Canonical suggesting sources — mirrors dashboard_api._SUGGESTED_AGENTS. A
# proposed mission whose `proposed_by` maps to one of these is a team
# *suggestion*; anything else (the orphan-autoregister healer, or an
# unidentifiable proposer) is *orphan-derived*. We replicate the small
# normalizer here rather than import dashboard_api (a heavy FastAPI module the
# headless narrator must not pull in). `closeout` is the non-agent P4 source.
_SUGGESTED_AGENTS = ('beacon', 'medic', 'pulse', 'closeout')


def mission_suggested_source(mission: dict[str, Any]) -> Optional[str]:
    """Map a mission's `proposed_by` to a canonical suggesting agent
    {beacon, medic, pulse}, or None when no team agent is identifiable (an
    orphan-derived auto-register, e.g. proposed_by='heal_orphan_autoregister')."""
    raw = mission.get('proposed_by')
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    for agent in _SUGGESTED_AGENTS:
        if v == agent or v.startswith(agent + '-') or v.startswith(agent + '_'):
            return agent
    return None


def mission_is_dead(mission: dict[str, Any]) -> bool:
    """A proposed mission the orphan-drain has acknowledged / retired / archived
    is filtered out of the funnel (mirrors dashboard_api._proposed_mission_is_dead)
    — never brief it: it shows on no card. Keeps the Narrator and the funnel
    agreeing on what is live."""
    if mission.get('acknowledged') is True:
        return True
    if mission.get('retired_at'):
        return True
    return (mission.get('phase') or '') in ('retired', 'archived', 'closed')


def mission_to_task(mission: dict[str, Any]) -> dict[str, Any]:
    """Build the dispatch-shaped task `trust_policy.evaluate` consumes from a
    funnel mission. A proposed mission proposes work the team would take on, so
    the task is shaped like the dispatch that accepting it would emit
    (Beacon → Forge in the mission's repo). Mirrors `capture_to_task`."""
    return {
        'source': 'beacon',
        'target_agent': 'forge',
        'task_type': mission.get('task_type') or 'feature-development',
        'target_repo': mission.get('repo'),
        'changed_files': mission.get('changed_files') or [],
    }


def classify_mission_careful(mission: dict[str, Any]) -> bool:
    """True if the proposed work looks irreversible / outward-facing / spendy —
    the § 5 escalator from medium to careful. Scans name + brief. (Only the
    boolean flag is derived from these; the raw `brief` text is NEVER surfaced on
    the card.)"""
    haystack = ' '.join(str(mission.get(k) or '') for k in ('name', 'brief'))
    return _CAREFUL_PATTERN.search(haystack) is not None


def derive_mission_risk(
    mission: dict[str, Any], policy: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    """Return (risk, careful) for a funnel mission. Mirrors `derive_risk` for
    captures: the risk LEVEL is deterministic (trust_policy view), testable with
    an injected policy."""
    action, _rule = trust_policy.evaluate(mission_to_task(mission), policy)
    careful = classify_mission_careful(mission)
    return map_risk(action, careful), careful


def render_raw_mission_briefing(
    mission: dict[str, Any], risk: str, careful: bool, *, suggested: bool,
) -> dict[str, str]:
    """Deterministic plain-English briefing for a funnel mission — the fallback
    when the LLM voice is unavailable (and the value tests assert against). Plain
    operator language ONLY: the human-facing `name`, never the machine `brief` /
    id / task_ids (the no-raw-metadata-leak guard, § 5)."""
    name = (mission.get('name') or 'this item').strip()
    repo = mission.get('repo')
    where = f' (in {repo})' if repo else ''
    what = name
    if suggested:
        why = f"The team flagged this{where} as worth a look."
    else:
        why = (f"Work the team started{where} that isn't tied to a tracked "
               "project yet — worth a decision.")
    if risk == 'careful':
        suggest = ('Have the team look at this carefully before any change — '
                   'it could be hard to undo.')
    elif risk == 'medium':
        suggest = 'Have the team run it down and propose next steps.'
    else:
        suggest = 'Low-risk — let the team take it from here.'
    return {'what': what, 'why': why, 'suggest': suggest}


def build_mission_briefing_prompt(
    mission: dict[str, Any], events: list[dict[str, Any]], risk: str,
    *, suggested: bool,
) -> str:
    """CEO-voice authoring prompt for a funnel-mission briefing (mirrors
    `build_briefing_prompt`). Beacon voice, plain outcomes, JSON-out for parse.
    Passes the human-facing `name` + lane kind ONLY — never the machine `brief`,
    id, or task_ids — so the model has no raw metadata to echo."""
    facts = {
        'title': mission.get('name'),
        'kind': ('a suggestion the team surfaced for you' if suggested
                 else "work the team started that isn't tied to a project yet"),
        'repo': mission.get('repo'),
        'risk_level': risk,
        'context_events': [
            {'type': e.get('event_type'), 'summary': (
                e.get('payload', {}).get('summary')
                if isinstance(e.get('payload'), dict) else None)}
            for e in events[:8]
        ],
    }
    return (
        "You are Beacon, the operator's manager. Write a 3-part briefing about an "
        "item on the operator's funnel so a busy CEO can decide on it in seconds — "
        "in his terms, never engineering jargon.\n\n"
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


def author_mission_meaning_layer(
    mission: dict[str, Any],
    events: Optional[list[dict[str, Any]]] = None,
    *,
    now: Optional[datetime] = None,
    policy: Optional[dict[str, Any]] = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Author the full meaning layer for one funnel mission and return the field
    dict (briefing/risk/risk_note/recommended_action/briefing_provenance). Pure —
    does not mutate the mission or touch disk; the caller (the GC healer) decides
    whether to write it back. Mirrors `author_meaning_layer` for captures, with a
    mission-shaped raw fallback. `use_llm=False` forces the deterministic raw
    briefing (the test path and the head-less fallback)."""
    events = events or []
    now = now or datetime.now(timezone.utc)

    risk, careful = derive_mission_risk(mission, policy)
    suggested = mission_suggested_source(mission) is not None
    # A mission carries no `aging` flag, so derive_recommended_action returns the
    # recommend-first default (`delegate`) — the right one-click for a funnel card.
    recommended_action = derive_recommended_action(mission, risk)

    briefing = None
    if use_llm:
        briefing = generate_briefing_voice(
            build_mission_briefing_prompt(mission, events, risk, suggested=suggested))
    if briefing is None:
        briefing = render_raw_mission_briefing(
            mission, risk, careful, suggested=suggested)

    fields: dict[str, Any] = {
        'briefing': briefing,
        'risk': risk,
        'recommended_action': recommended_action,
        'briefing_provenance': {
            'by': NARRATOR_BY,
            'model': NARRATOR_MODEL if use_llm else 'raw',
            'at': now.isoformat(),
            'from_state': mission.get('phase'),
        },
    }
    risk_note = build_risk_note(mission, risk, careful)
    if risk_note is not None:
        fields['risk_note'] = risk_note
    return fields


def mission_needs_briefing(mission: dict[str, Any]) -> bool:
    """True if a funnel mission needs (re)briefing — a live `proposed` mission
    (orphan-derived or suggested) that is either un-briefed or whose provenance
    was stamped from a different phase than it now carries. Idempotent: a mission
    already briefed for its current phase returns False, so the periodic sweep and
    the event-driven path converge and an unchanged card is never re-authored.

    Non-`proposed` missions are skipped: an accepted mission (drafting/ready/…)
    has left the funnel intake lane and renders via the established missions view,
    not as a funnel card; a dead (acknowledged/retired/archived) mission is
    auto-filtered out of the funnel entirely."""
    if not isinstance(mission, dict):
        return False
    if (mission.get('phase') or '') != _MISSION_FUNNEL_PHASE:
        return False
    if mission_is_dead(mission):
        return False
    if not isinstance(mission.get('briefing'), dict):
        return True
    provenance = mission.get('briefing_provenance')
    if not isinstance(provenance, dict):
        return True
    return provenance.get('from_state') != mission.get('phase')


def author_missions_in_registry(
    registry: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    client: Optional[Any] = None,
    use_llm: bool = True,
    policy: Optional[dict[str, Any]] = None,
    max_per_tick: int = NARRATOR_MAX_PER_TICK,
) -> tuple[int, int]:
    """Author the meaning layer onto funnel missions (orphan-derived + suggested)
    in ``registry`` that need briefing, bounded by ``max_per_tick`` (spec § 3).
    Mutates the registry IN PLACE — the caller (the GC healer) owns the single
    atomic write + git commit, so this never becomes a second writer of
    missions.json (single-committer invariant). Mirrors
    `author_captures_in_registry`.

    Returns ``(briefed, deferred)``. Fail-safe per mission: an author error on one
    mission is logged and skipped — never aborts the sweep, never corrupts the
    registry (the deterministic fallback already guarantees a usable briefing).
    The per-tick bound counts AUTHORING ATTEMPTS, not successes."""
    now = now or datetime.now(timezone.utc)
    missions = registry.get('missions')
    if not isinstance(missions, list):
        return (0, 0)
    pending = [m for m in missions if mission_needs_briefing(m)]

    attempted = 0
    briefed = 0
    for m in pending:
        if attempted >= max_per_tick:
            break
        mid = m.get('id')
        if not mid:
            continue  # no stable id — can't author a meaningful card; skip.
        attempted += 1
        try:
            # Context lives under the orphan task_id (chain_events key), not the
            # mission id; use the first task_id when present (best-effort, []).
            task_ids = m.get('task_ids')
            tid = task_ids[0] if isinstance(task_ids, list) and task_ids else None
            events = fetch_capture_events(tid, client=client) if tid else []
            fields = author_mission_meaning_layer(
                m, events, now=now, policy=policy, use_llm=use_llm)
        except Exception as e:  # noqa: BLE001 — per-mission fail-safe
            log(f'narrator: author failed for mission {mid}: '
                f'{type(e).__name__}: {e} — skipped (retries next tick)')
            continue
        # risk_note is optional (absent for safe) — clear any stale one so a
        # mission dropping medium→safe doesn't keep a dangling note.
        m.pop('risk_note', None)
        m.update(fields)
        briefed += 1

    deferred = max(0, len(pending) - briefed)
    log(f'narrator mission sweep: briefed {briefed} funnel mission(s); '
        f'deferred {deferred} (max {max_per_tick}/tick)')
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


# ---------------- approval-card meaning layer (projects-v3 P7.2a) -------------
#
# Contract A extended to the Approvals queue. Unlike captures/missions (JSON
# registries), the dashboard reads pending decisions DIRECTLY from Supabase
# `chain_events` (select * over the approvable event types), so the meaning layer
# can't ride a registry — it must live ON the chain_events row. The Narrator
# authors the SAME field contract (briefing/risk/risk_note/briefing_provenance,
# minus recommended_action — the queue uses Approve/Reject, not delegate/drop)
# and writes it into the row's `payload` JSONB (payload.briefing, …) so the
# dashboard's `select *` surfaces it with no schema migration.
#
# This is the Narrator's ONLY write to chain_events. It writes ONLY the briefing
# keys; the emitter owns the rest of payload and row resolution only ever flips
# the top-level `read_at` column (never payload — verified in dashboard_api), so
# there is no lost-update race. Author-once: an approval is point-in-time (no
# lifecycle state to re-key on), so we brief a row once (briefing absent) and
# never re-author. Same fail-safe per row + per-tick bound + deterministic
# fallback as the sibling sweeps.

# The approvable types we brief. approval_request + clarify_request are the
# decisions Larry must act on; an escalation is briefed only when it carries no
# summary of its own. larry_alert/sentinel_alert already carry a message and are
# read-not-decided, so they are NOT briefed.
_APPROVAL_BRIEF_TYPES = ('approval_request', 'clarify_request', 'escalation')

# Severity → risk when trust_policy has no rule to classify the approval (its
# payload carries no repo/file dispatch context). Mirrors the dashboard's
# normalizeSeverity buckets.
_SEVERITY_RISK = {'critical': 'careful', 'warning': 'medium', 'info': 'safe'}

# Risk ordering for the "severity may escalate but never downgrade the policy
# default" rule in derive_approval_risk.
_RISK_ORDER = {'safe': 0, 'medium': 1, 'careful': 2}


def _approval_payload(event: dict[str, Any]) -> dict[str, Any]:
    p = event.get('payload')
    return p if isinstance(p, dict) else {}


def _normalize_severity(raw: Any) -> str:
    """Coarse severity → {critical, warning, info}; unknown/absent → info."""
    s = str(raw or '').strip().lower()
    if s in ('critical', 'crit', 'high', 'error', 'fatal', 'red'):
        return 'critical'
    if s in ('warning', 'warn', 'medium', 'med', 'yellow'):
        return 'warning'
    return 'info'


def approval_text(event: dict[str, Any]) -> str:
    """The human request prose the emitter wrote on an approval event — the
    prompt/question/message/summary. Drives the careful-keyword scan + the
    briefing fallback. Empty string when none present."""
    p = _approval_payload(event)
    for k in ('prompt', 'question', 'message', 'summary', 'headline'):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ''


def approval_to_task(event: dict[str, Any]) -> dict[str, Any]:
    """Build the trust_policy task from an approval event. An approval_request
    carries proposing/target agent but NOT the dispatch repo/file context, so the
    task is coarse — trust_policy usually falls through to its default, handled by
    derive_approval_risk's severity fallback."""
    p = _approval_payload(event)
    return {
        'source': p.get('proposing_agent') or event.get('agent') or 'beacon',
        'target_agent': p.get('target_agent') or 'forge',
        'task_type': p.get('task_type') or 'feature-development',
        'target_repo': p.get('target_repo') or p.get('repo'),
        'changed_files': p.get('changed_files') or [],
    }


def classify_approval_careful(event: dict[str, Any]) -> bool:
    """True if the approval's request prose looks irreversible/outward/spendy —
    the § 5 escalator to careful. Scans the request text."""
    return _CAREFUL_PATTERN.search(approval_text(event)) is not None


def derive_approval_risk(
    event: dict[str, Any], policy: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    """Return (risk, careful) for an approval. When a real trust_policy rule
    matches, trust it (map_risk). When no rule matches, the policy DEFAULT applies
    (e.g. force_ask → medium) as the FLOOR, and the event's severity may only
    ESCALATE it (critical → careful), never drop it below that default — an
    unclassified approval is still a decision the policy wants a human to weigh,
    so it never reads `safe` just because its severity is low (an approval payload
    carries no repo/file context for finer matching; § P7.2a). The careful-keyword
    escalator always wins (it's folded into map_risk)."""
    careful = classify_approval_careful(event)
    action, rule = trust_policy.evaluate(approval_to_task(event), policy)
    policy_risk = map_risk(action, careful)
    if rule is not None:
        return policy_risk, careful  # an explicit rule classified it — trust it
    # No rule matched: policy_risk is the default-action floor; severity escalates
    # it but never downgrades below the floor.
    sev_risk = _SEVERITY_RISK[_normalize_severity(
        _approval_payload(event).get('severity'))]
    risk = (policy_risk if _RISK_ORDER[policy_risk] >= _RISK_ORDER[sev_risk]
            else sev_risk)
    return risk, careful


def render_raw_approval_briefing(
    event: dict[str, Any], risk: str, careful: bool,
) -> dict[str, str]:
    """Deterministic plain-English briefing — the fallback when the LLM voice is
    unavailable (and the value tests assert against). Operator language; never
    echoes the raw request blob (the card already shows the full prompt on
    expand — this is the plain summary layered on top). The `suggest` verb tracks
    the event type's ACTUAL action (approve/reject, answer, or acknowledge) so it
    never tells Larry to "approve" a card that has no Approve button — a
    clarify_request is answered, an escalation is looked-at/acknowledged."""
    etype = event.get('event_type')
    if etype == 'clarify_request':
        what = 'A teammate needs your answer to keep moving.'
        why = 'Work is paused until you reply.'
        if risk == 'careful':
            suggest = 'Answer carefully — your call here is hard to walk back.'
        elif risk == 'medium':
            suggest = 'Give it a quick look, then answer.'
        else:
            suggest = 'A quick answer unblocks the team.'
    elif etype == 'escalation':
        what = 'The team escalated something for your attention.'
        why = 'It was flagged as needing a person, not the auto-path.'
        if risk == 'careful':
            suggest = 'Look carefully before you act — it could be hard to undo.'
        elif risk == 'medium':
            suggest = 'Give it a quick look and decide how to handle it.'
        else:
            suggest = 'A quick look and an acknowledgement clears it.'
    else:  # approval_request
        what = 'The team is asking for your go-ahead.'
        why = "It's blocked on your approval before it can proceed."
        if risk == 'careful':
            suggest = 'Look carefully before approving — it could be hard to undo.'
        elif risk == 'medium':
            suggest = 'Give it a quick check, then approve or send it back.'
        else:
            suggest = 'Low-risk — approve if it looks right.'
    return {'what': what, 'why': why, 'suggest': suggest}


def build_approval_briefing_prompt(event: dict[str, Any], risk: str) -> str:
    """CEO-voice authoring prompt for an approval briefing (mirrors
    build_briefing_prompt). Beacon voice, plain outcomes, JSON-out for parse."""
    p = _approval_payload(event)
    facts = {
        'kind': event.get('event_type'),
        'from_agent': event.get('agent') or p.get('proposing_agent'),
        'request': approval_text(event)[:2000],
        'risk_level': risk,
    }
    return (
        "You are Beacon, the operator's manager. A teammate has put a decision in "
        "front of the operator — an approval to give, a question to answer, or an "
        "escalation to weigh. Write a 3-part briefing so a busy CEO can decide in "
        "seconds — in his terms, never engineering jargon.\n\n"
        "Return ONLY a JSON object with exactly these keys (each one short, plain "
        "English, no markdown):\n"
        '  "what":    one sentence — what he is being asked to decide.\n'
        '  "why":     one sentence — why it matters / what is blocked on it.\n'
        '  "suggest": one sentence — what you recommend he do.\n\n'
        "Never mention task ids, branches, PRs, commits, agents, or risk codes. "
        f"The risk level is already computed as \"{risk}\"; reflect its caution in "
        "the suggestion's tone but don't name it.\n\n"
        "Here are the facts (JSON):\n"
        f"{json.dumps(facts, indent=2)}\n\n"
        "Write the briefing now. Output ONLY the JSON object."
    )


def author_approval_meaning_layer(
    event: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    policy: Optional[dict[str, Any]] = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Author the meaning layer for one approval event and return the field dict
    (briefing/risk/risk_note/briefing_provenance) to merge into the row's payload.
    Pure — does not mutate the event or touch Supabase; the sweep owns the write.
    Mirrors author_meaning_layer, minus recommended_action (the Approvals queue
    uses Approve/Reject, not the delegate/drop vocabulary)."""
    now = now or datetime.now(timezone.utc)
    risk, careful = derive_approval_risk(event, policy)

    briefing = None
    if use_llm:
        briefing = generate_briefing_voice(
            build_approval_briefing_prompt(event, risk))
    authored_by_llm = briefing is not None
    if briefing is None:
        briefing = render_raw_approval_briefing(event, risk, careful)

    fields: dict[str, Any] = {
        'briefing': briefing,
        'risk': risk,
        'briefing_provenance': {
            'by': NARRATOR_BY,
            'model': NARRATOR_MODEL if authored_by_llm else 'raw',
            'at': now.isoformat(),
            'source': 'approval',
        },
    }
    risk_note = build_risk_note(event, risk, careful)
    if risk_note is not None:
        fields['risk_note'] = risk_note
    return fields


def approval_needs_briefing(event: dict[str, Any]) -> bool:
    """True if a pending approval row needs briefing — a briefable, unresolved
    event whose payload carries no briefing yet. Author-once (approvals are
    point-in-time): a row already carrying a briefing returns False, so the sweep
    is idempotent. Resolved rows (read_at set) and an escalation that already
    carries its own summary are skipped."""
    if not isinstance(event, dict):
        return False
    if event.get('event_type') not in _APPROVAL_BRIEF_TYPES:
        return False
    if event.get('read_at'):
        return False
    p = _approval_payload(event)
    if (event.get('event_type') == 'escalation'
            and isinstance(p.get('summary'), str) and p['summary'].strip()):
        return False
    return not isinstance(p.get('briefing'), dict)


def _approval_client(client: Optional[Any] = None) -> Optional[Any]:
    """The Supabase client for the approval sweep — injected (tests) or the
    shared chain_event_emit client. None when supabase isn't configured."""
    if client is not None:
        return client
    try:
        import chain_event_emit as cee  # noqa: PLC0415
    except ImportError:
        return None
    return cee._get_client()


def _fetch_pending_approvals(client: Any) -> list[dict[str, Any]]:
    """Best-effort fetch of recent approvable chain_events (read_at filtered in
    Python by approval_needs_briefing). Any failure → [] (the sweep no-ops)."""
    try:
        resp = (
            client.table('chain_events')
            .select('event_id,event_type,agent,ts,read_at,payload')
            .in_('event_type', list(_APPROVAL_BRIEF_TYPES))
            .order('ts', desc=True)
            .limit(200)
            .execute()
        )
    except Exception as e:  # noqa: BLE001 — fetch is best-effort
        log(f'narrator: pending-approvals fetch failed: '
            f'{type(e).__name__}: {e}')
        return []
    return list(getattr(resp, 'data', None) or [])


def author_pending_approvals(
    *,
    client: Optional[Any] = None,
    now: Optional[datetime] = None,
    use_llm: bool = True,
    policy: Optional[dict[str, Any]] = None,
    max_per_tick: int = NARRATOR_MAX_PER_TICK,
) -> tuple[int, int]:
    """Author the meaning layer onto pending approval chain_events that need it,
    writing the briefing fields into each row's `payload` JSONB via a single
    per-row update — the Narrator's only write to chain_events (briefing keys
    only; read_at-based resolution never touches payload, so no lost-update
    race). Bounded by ``max_per_tick``; fail-safe per row (an error on one row is
    logged + skipped). Returns ``(briefed, deferred)``. No supabase client
    (unconfigured) → ``(0, 0)``."""
    now = now or datetime.now(timezone.utc)
    cli = _approval_client(client)
    if cli is None:
        return (0, 0)
    pending = [e for e in _fetch_pending_approvals(cli)
               if approval_needs_briefing(e)]

    attempted = 0
    briefed = 0
    for ev in pending:
        if attempted >= max_per_tick:
            break
        eid = ev.get('event_id')
        if not eid:
            continue  # no stable key to update — skip.
        attempted += 1
        try:
            fields = author_approval_meaning_layer(
                ev, now=now, policy=policy, use_llm=use_llm)
            new_payload = {**_approval_payload(ev), **fields}
            (
                cli.table('chain_events')
                .update({'payload': new_payload})
                .eq('event_id', eid)
                .execute()
            )
        except Exception as e:  # noqa: BLE001 — per-row fail-safe
            log(f'narrator: approval brief failed for {eid}: '
                f'{type(e).__name__}: {e} — skipped (retries next tick)')
            continue
        briefed += 1

    deferred = max(0, len(pending) - briefed)
    log(f'narrator approval sweep: briefed {briefed} approval(s); '
        f'deferred {deferred} (max {max_per_tick}/tick)')
    return (briefed, deferred)


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
    parser.add_argument('--approvals', action='store_true',
                        help='brief the Approvals queue (pending chain_events) '
                             'instead of parked captures (projects-v3 P7.2a)')
    args = parser.parse_args(argv)
    if args.approvals:
        # Approvals are Supabase chain_events the dashboard reads directly; the
        # sweep writes the briefing into each row's payload (no captures.json).
        # --dry-run skips the write (the sweep can't author without writing).
        if args.dry_run:
            log('narrator: --approvals --dry-run is a no-op '
                '(the approval sweep writes Supabase when it briefs); skipping')
            return 0
        briefed, deferred = author_pending_approvals(use_llm=not args.no_llm)
        log(f'narrator: done ({briefed} approval(s) briefed, {deferred} deferred)')
        return 0
    n = run(dry_run=args.dry_run, use_llm=not args.no_llm)
    log(f'narrator: done ({n} briefed)')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'narrator FATAL: {type(exc).__name__}: {exc}')
        sys.exit(1)
