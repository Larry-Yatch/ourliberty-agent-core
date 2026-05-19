#!/usr/bin/env python3
"""forge_preflight_handler.py — preflight-marker library for Forge dispatches.

Phase D3 commit 4a (`D3-forge` part 1). Pure logic. No I/O, no daemons,
no claude spawning. The outbox notifier owns I/O; this module owns the
marker grammar + the clarification-budget state machine.

What Forge does:

  After reading a dispatched spec, Forge ends her response with EXACTLY one of:

    === PROCEED ===
    {"task_id": "<id>", "preflight_summary": "<1-3 sentence read of the spec>"}
    === END_PROCEED ===

    === CLARIFY_REQUEST ===
    {"task_id": "<id>", "question": "<single specific question>"}
    === END_CLARIFY_REQUEST ===

    === REJECT ===
    {"task_id": "<id>", "reason": "<why this is not buildable as specified>"}
    === END_REJECT ===

The notifier calls `parse_forge_marker` on Forge's claude output. The
returned `(marker_type, payload, narrative_without_marker)` drives routing:

  - PROCEED         → notify Beacon as `forge-result`, intent `preflight-proceed`.
                      In commit 4b this becomes the trigger to re-dispatch
                      the same task with `phase=build` + worktree + --resume.
  - CLARIFY_REQUEST → notify Beacon as `forge-question`, intent
                      `clarification-request`. Beacon answers; the notifier
                      routes the answer back to Forge as `beacon-clarification`
                      with `clarification_count` bumped + session_id preserved.
                      If incrementing the count would exceed `max_clarifications`
                      on the envelope, the request is converted to a
                      `preflight-rejection` notify instead (budget exhausted).
  - REJECT          → notify Beacon as `forge-result`, intent
                      `preflight-rejection`. Beacon journals; no auto-retry.
  - no marker       → caller's choice (notifier treats as legacy result).

Mirrors `beacon_approval_handler` shape: strict block-delimited grammar,
required-field validation, malformed markers raise an explicit exception
that the notifier surfaces as a dead-letter back to Beacon (so Forge gets
told to retry with a corrected marker, instead of the dispatch vanishing).

Budget enforcement is envelope-driven: the count + max live on the inbox
task JSON (HANDSHAKE schema fields added in D3-prep). This module is
stateless — pass the envelope in, get a routing decision back.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


# -------------------- marker grammar --------------------

# Block-delimited markers, mirrors beacon_approval_handler's APPROVAL_MARKER_RE.
# Strict on closing delimiter to prevent silent acceptance of unterminated blocks.
# Case-sensitive: the marker keywords are part of the contract, not free-form text.
PROCEED_MARKER_RE = re.compile(
    r'===\s*PROCEED\s*===\s*(\{.*?\})\s*===\s*END_PROCEED\s*===',
    re.DOTALL,
)
CLARIFY_MARKER_RE = re.compile(
    r'===\s*CLARIFY_REQUEST\s*===\s*(\{.*?\})\s*===\s*END_CLARIFY_REQUEST\s*===',
    re.DOTALL,
)
REJECT_MARKER_RE = re.compile(
    r'===\s*REJECT\s*===\s*(\{.*?\})\s*===\s*END_REJECT\s*===',
    re.DOTALL,
)

# D3.5 5b-followup Bug A: loose-delimiter detector for diagnostic error.
# When the strict regex doesn't match but a Forge response DOES contain
# marker-opening delimiters, the failure mode is "delimiters present but
# content between them isn't JSON" — diagnostically different from "no
# delimiters at all." Surface that distinction so Forge's retry knows to
# put JSON between the delimiters, not prose.
_LOOSE_FORGE_DELIMITER_RE = re.compile(
    r'===\s*(PROCEED|CLARIFY_REQUEST|REJECT)\s*===',
)

MARKER_TYPES = ('proceed', 'clarify_request', 'reject')

# Canonical KEYWORD for each marker type. The render path uses this to build
# the block delimiters; the parse path uses regex constants above. Both sides
# must agree — the round-trip tests in scripts/tests/test_forge_preflight_handler.py
# (TestRenderMarker) fail loudly if MARKER_KEYWORDS drifts from the regexes.
MARKER_KEYWORDS = {
    'proceed': 'PROCEED',
    'clarify_request': 'CLARIFY_REQUEST',
    'reject': 'REJECT',
}

# Required fields per marker type.
REQUIRED_FIELDS = {
    'proceed': {'task_id', 'preflight_summary'},
    'clarify_request': {'task_id', 'question'},
    'reject': {'task_id', 'reason'},
}

# Default clarification budget when the dispatch envelope doesn't set one.
# Matches the documented default in agents/beacon/CLAUDE.md.
DEFAULT_MAX_CLARIFICATIONS = 3


# -------------------- exceptions --------------------

class PreflightHandlerError(Exception):
    """Base for handler errors that the notifier should surface to Beacon."""


class MalformedForgeMarker(PreflightHandlerError):
    """Forge emitted a marker block whose JSON is unparseable or missing fields."""


class MultipleForgeMarkers(PreflightHandlerError):
    """Forge emitted more than one marker — ambiguous which to act on."""


# -------------------- marker rendering --------------------

def render_marker(marker_type: str, **payload: Any) -> str:
    """Build a canonical preflight-marker block from a payload dict.

    Returns the exact text Forge should paste into her response — block
    delimiters + pretty-printed JSON payload + trailing newline. Output is
    guaranteed parseable by `parse_forge_marker`; the round-trip test
    `TestRenderMarker.test_render_then_parse_roundtrip` enforces that.

    Raises ValueError (NOT MalformedForgeMarker — render-time errors are
    caller programming errors, not runtime parse failures) if:
      - marker_type is not in MARKER_TYPES
      - payload is missing any field in REQUIRED_FIELDS[marker_type]

    Extra fields beyond REQUIRED_FIELDS are allowed and preserved (matches
    the existing optional-field pattern — e.g., PROCEED may carry an
    optional `session_id`, REJECT may carry optional `next_step`).
    """
    if marker_type not in MARKER_TYPES:
        raise ValueError(
            f'unknown marker type: {marker_type!r}. '
            f'Valid types: {sorted(MARKER_TYPES)}'
        )
    required = REQUIRED_FIELDS[marker_type]
    missing = required - set(payload.keys())
    if missing:
        raise ValueError(
            f'render_marker({marker_type!r}): missing required fields: '
            f'{sorted(missing)}. Required: {sorted(required)}'
        )
    keyword = MARKER_KEYWORDS[marker_type]
    json_body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    return f'=== {keyword} ===\n{json_body}\n=== END_{keyword} ===\n'


# -------------------- marker extraction --------------------

def parse_forge_marker(
    forge_response: str,
) -> tuple[Optional[str], Optional[dict[str, Any]], str]:
    """Find the one valid preflight marker block in Forge's claude output.

    Returns `(marker_type, payload, narrative_without_marker)` where:
      - marker_type ∈ {'proceed', 'clarify_request', 'reject'} or None
      - payload is the parsed JSON object or None
      - narrative_without_marker is the response with the marker block excised
        (so the notifier doesn't echo the raw marker in the notify prompt)

    Raises MalformedForgeMarker if a marker delimiter pair is present but the
    JSON is unparseable, not an object, or missing required fields.

    Raises MultipleForgeMarkers if Forge emitted two or more marker blocks
    in the same response — including TWO OF THE SAME TYPE (e.g., two
    CLARIFY_REQUEST blocks). The protocol requires exactly one marker per
    response so the routing decision is unambiguous. The notifier
    dead-letters multi-marker outputs back to Forge so she can re-emit.
    """
    if not forge_response:
        return None, None, forge_response or ''

    # Find ALL marker blocks (every type, every occurrence). Multiple markers
    # of any kind is an error — the routing decision must be unambiguous.
    all_matches: list[tuple[str, re.Match]] = []
    for mtype, regex in (
        ('proceed', PROCEED_MARKER_RE),
        ('clarify_request', CLARIFY_MARKER_RE),
        ('reject', REJECT_MARKER_RE),
    ):
        for m in regex.finditer(forge_response):
            all_matches.append((mtype, m))

    if not all_matches:
        # D3.5 5b-followup Bug A: distinguish "no delimiters at all" from
        # "delimiters present but no JSON inside." On 2026-05-13 Forge
        # emitted `=== PROCEED ===\n<prose>\n=== END_PROCEED ===` — the
        # delimiters were there but JSON wasn't, the strict regex didn't
        # match, and the runtime gate reported "none found" — which is
        # technically true but doesn't tell Forge what she actually did
        # wrong. The diagnostic error here is more actionable.
        loose_matches = _LOOSE_FORGE_DELIMITER_RE.findall(forge_response)
        if loose_matches:
            kinds = list(dict.fromkeys(loose_matches))  # dedupe preserve order
            raise MalformedForgeMarker(
                f'Marker opening delimiter(s) found ({kinds}) but no valid '
                f'JSON object between them and the closing delimiter. The '
                f'content INSIDE `=== XXX ===` and `=== END_XXX ===` MUST '
                f'be a single JSON object — e.g. '
                f'`{{"task_id": "...", "preflight_summary": "..."}}` — NOT '
                f'prose, NOT a sentence, NOT an explanation. Put narrative '
                f'ABOVE the marker block (Beacon reads it). Put strict JSON '
                f'INSIDE the block (the parser reads it). If you want to '
                f'explain your reasoning, do it in the narrative section, '
                f'not the marker payload.'
            )
        return None, None, forge_response

    if len(all_matches) > 1:
        kinds = sorted({k for k, _ in all_matches})
        kind_counts = {k: sum(1 for kk, _ in all_matches if kk == k) for k in kinds}
        kinds_desc = ', '.join(f'{k}={kind_counts[k]}' for k in kinds)
        raise MultipleForgeMarkers(
            f'Forge emitted {len(all_matches)} marker blocks ({kinds_desc}); '
            f'expected exactly one. Re-emit a single PROCEED, CLARIFY_REQUEST, '
            f'or REJECT block.'
        )

    marker_type, match = all_matches[0]
    raw_json = match.group(1)
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise MalformedForgeMarker(
            f'{marker_type} marker has invalid JSON: {e}'
        ) from e

    if not isinstance(payload, dict):
        raise MalformedForgeMarker(
            f'{marker_type} marker payload must be a JSON object, '
            f'got {type(payload).__name__}'
        )

    required = REQUIRED_FIELDS[marker_type]
    missing = required - set(payload.keys())
    if missing:
        raise MalformedForgeMarker(
            f'{marker_type} marker missing required fields: {sorted(missing)}'
        )

    narrative = (
        forge_response[: match.start()] + forge_response[match.end():]
    ).strip()
    return marker_type, payload, narrative


# -------------------- clarification-budget decision --------------------

def evaluate_clarification_budget(
    task_envelope: dict[str, Any],
) -> tuple[str, int, int]:
    """Decide whether another CLARIFY_REQUEST round is permitted.

    Returns `(decision, next_count, max_count)` where:
      - decision ∈ {'allow', 'exhausted'}
      - next_count is the value the count would take after this round
      - max_count is the envelope's max_clarifications (or default)

    'allow' means the notifier routes the clarification to Beacon and the
    next dispatch back to Forge carries `clarification_count = next_count`.

    'exhausted' means we've hit the budget — the notifier should convert
    the request to a preflight-rejection and tell Beacon the budget ran out.
    """
    current = task_envelope.get('clarification_count', 0)
    if not isinstance(current, int) or current < 0:
        current = 0
    max_count = task_envelope.get('max_clarifications', DEFAULT_MAX_CLARIFICATIONS)
    if not isinstance(max_count, int) or max_count < 0:
        max_count = DEFAULT_MAX_CLARIFICATIONS
    next_count = current + 1
    if next_count > max_count:
        return 'exhausted', next_count, max_count
    return 'allow', next_count, max_count


def clarifications_remaining(task_envelope: dict[str, Any]) -> int:
    """How many CLARIFY_REQUEST rounds Forge has left, given current count.

    Used by the notify-prompt template when telling Forge "you have N
    clarifications left" inside a clarification-response delivery.
    """
    current = task_envelope.get('clarification_count', 0)
    if not isinstance(current, int) or current < 0:
        current = 0
    max_count = task_envelope.get('max_clarifications', DEFAULT_MAX_CLARIFICATIONS)
    if not isinstance(max_count, int) or max_count < 0:
        max_count = DEFAULT_MAX_CLARIFICATIONS
    return max(0, max_count - current)


# -------------------- routing helpers --------------------

def derive_notify_source(marker_type: str, processing_agent: str) -> str:
    """Source field for the notify task built from a marker.

    PROCEED + REJECT → `<agent>-result` (Beacon's normal result channel).
    CLARIFY_REQUEST   → `<agent>-question` (Beacon's clarification inbox lane).
    """
    if marker_type == 'clarify_request':
        return f'{processing_agent}-question'
    return f'{processing_agent}-result'


def derive_intent(marker_type: str, budget_decision: Optional[str] = None) -> str:
    """Map (marker_type, budget_decision) to the notify intent string.

    Returns one of dispatch_validator's ALLOWED_INTENTS (D3-prep vocabulary):

      proceed              → 'ack-proceed'
      reject               → 'reject'
      clarify_request      → 'clarify' (under budget)
                           → 'clarification-exhausted' (budget hit)

    The notify-prompt template uses this to pick the right action block.
    Budget exhaustion converts a clarify_request into 'clarification-exhausted'
    so Beacon sees the specific termination reason rather than a generic reject.
    """
    if marker_type == 'proceed':
        return 'ack-proceed'
    if marker_type == 'reject':
        return 'reject'
    if marker_type == 'clarify_request':
        if budget_decision == 'exhausted':
            return 'clarification-exhausted'
        return 'clarify'
    return 'result-notification'


def build_exhausted_reason(
    payload: dict[str, Any], next_count: int, max_count: int,
) -> str:
    """Frame budget-exhaustion in Beacon-readable terms for a rejection notify.

    Preserves the actual question Forge asked so Beacon sees why we ran out —
    e.g. the spec was too ambiguous in N places, not Forge being lazy.
    """
    question = payload.get('question') or '(no question text recorded)'
    return (
        f'Forge would have asked clarification #{next_count} but the budget '
        f'is {max_count}. Final question that hit the cap:\n\n  {question}'
    )
