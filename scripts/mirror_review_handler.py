#!/usr/bin/env python3
"""mirror_review_handler.py — review-marker library for Mirror dispatches.

Phase D3.5 commit 5a (`D3.5-mirror-review` part 1). Pure logic. No I/O, no
daemons, no claude spawning. The outbox notifier owns I/O; this module
owns the marker grammar + the revision-budget state machine.

Mirrors `forge_preflight_handler.py` shape line-for-line — block-delimited
grammar, required-field validation, multi-marker rejection, stateless
budget evaluation, routing-helper derivations. The Forge file is the
template; this is the second instance of the pattern.

What Mirror does:

  After a `review-request` task lands in her inbox (written by the outbox
  notifier when Forge's build outbox carries "PR opened: <url>"), Mirror
  reads the spec from the envelope, reads the PR diff via `gh pr diff <N>`,
  optionally checks out the branch and runs tests, then ends her response
  with EXACTLY one of:

    === REVIEW_PASS ===
    {"task_id": "<id>", "pr_url": "<url>",
     "summary": "<1-3 sentence approval rationale>"}
    === END_REVIEW_PASS ===

    === REVIEW_REVISION ===
    {"task_id": "<id>", "pr_url": "<url>",
     "findings": [{"file": "<path>", "line_range": "<L1-L2>",
                   "severity": "low|medium", "description": "<what to fix>"}],
     "severity": "low|medium",
     "confidence": "high|medium|low"}
    === END_REVIEW_REVISION ===

    === REVIEW_ESCALATE ===
    {"task_id": "<id>", "pr_url": "<url>",
     "reason": "<why this needs Beacon replan, not just Forge revision>",
     "severity": "high",
     "confidence": "high|medium|low"}
    === END_REVIEW_ESCALATE ===

    === REVIEW_EMERGENCY_HALT ===
    {"task_id": "<id>", "pr_url": "<url>",
     "reason": "<what triggered the halt — security, destructive migration,
                allowlist breach>",
     "evidence": "<quoted-from-diff string pointing at the artifact>"}
    === END_REVIEW_EMERGENCY_HALT ===

The notifier calls `parse_mirror_marker` on Mirror's claude output. The
returned `(marker_type, payload, narrative_without_marker)` drives routing:

  - REVIEW_PASS            → notify Beacon as `mirror-result`, intent
                             `review-pass`. In 5a Beacon just journals;
                             5d wires this to `gh pr merge --squash`.
  - REVIEW_REVISION        → notify Beacon as `mirror-result`, intent
                             `review-revision`. In 5a Beacon journals + Larry
                             handles manually; 5b wires this to a revision-task
                             dispatch back to Forge under --resume.
  - REVIEW_ESCALATE        → notify Beacon as `mirror-result`, intent
                             `review-escalate`. In 5a Beacon journals; 5c
                             wires Beacon's replan flow.
  - REVIEW_EMERGENCY_HALT  → notify Beacon as `mirror-result`, intent
                             `review-emergency-halt`. In 5a Beacon journals
                             + WARN log; 5d wires EMERGENCY_HALT file trip +
                             priority DM to Larry.

Confidence-promote rule (5a): a REVIEW_REVISION with `confidence: low` is
auto-promoted to REVIEW_ESCALATE-shaped routing. Rationale: don't trust the
auto-revision loop when Mirror herself is uncertain — kick it up to Beacon
instead. The classifier signals this with `auto_promoted: True` in its
return dict so downstream logging records that Mirror said "revise" but
the system routed as escalate.

Severity rubric (declared here for parse-time validation; Mirror's CLAUDE.md
owns the human-readable rubric):

  - low      : nit / suggestion (defer, doesn't block)
  - medium   : should-fix (regression risk, missing test, scope creep)
  - high     : must-fix that changes the plan (spec is incomplete or wrong)
  - critical : safety issue (credentials, destructive ops, allowlist breach)

Confidence rubric:

  - high   : Mirror is sure the finding is real and the fix is well-defined
  - medium : Mirror is sure of the finding but the fix is judgment-loaded
  - low    : Mirror is uncertain whether the finding is real; auto-promotes
             a REVISION to ESCALATE so Beacon can decide

Loop bounds (forward-compat for 5b/5c): the dispatch envelope carries
`revision_count` (0 on first review) and `max_revisions` (default 3 from
agent-models.json `loop_bounds`). 5a includes `evaluate_revision_budget`
for parity with forge_preflight_handler's clarification budget; 5b is
where the budget actually gates further revision dispatches.

Budget enforcement is envelope-driven (same shape as preflight): the count
+ max live on the inbox task JSON. This module is stateless — pass the
envelope in, get a routing decision back.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


# -------------------- marker grammar --------------------

# Block-delimited markers, mirrors forge_preflight_handler's *_MARKER_RE shape.
# Strict on closing delimiter to prevent silent acceptance of unterminated
# blocks. Case-sensitive: marker keywords are part of the contract.
REVIEW_PASS_MARKER_RE = re.compile(
    r'===\s*REVIEW_PASS\s*===\s*(\{.*?\})\s*===\s*END_REVIEW_PASS\s*===',
    re.DOTALL,
)
REVIEW_REVISION_MARKER_RE = re.compile(
    r'===\s*REVIEW_REVISION\s*===\s*(\{.*?\})\s*===\s*END_REVIEW_REVISION\s*===',
    re.DOTALL,
)
REVIEW_ESCALATE_MARKER_RE = re.compile(
    r'===\s*REVIEW_ESCALATE\s*===\s*(\{.*?\})\s*===\s*END_REVIEW_ESCALATE\s*===',
    re.DOTALL,
)
REVIEW_EMERGENCY_HALT_MARKER_RE = re.compile(
    r'===\s*REVIEW_EMERGENCY_HALT\s*===\s*(\{.*?\})\s*'
    r'===\s*END_REVIEW_EMERGENCY_HALT\s*===',
    re.DOTALL,
)

# D3.5 5b-followup Bug A: loose-delimiter detector for diagnostic error.
# Symmetric with the Forge handler's _LOOSE_FORGE_DELIMITER_RE. When a Mirror
# response contains marker-opening delimiters but the strict regex didn't
# match, the failure is "delimiters present but no JSON inside" — surface
# that distinction so Mirror's retry knows to put JSON between the
# delimiters, not prose.
_LOOSE_MIRROR_DELIMITER_RE = re.compile(
    r'===\s*(REVIEW_PASS|REVIEW_REVISION|REVIEW_ESCALATE|REVIEW_EMERGENCY_HALT)\s*===',
)

MARKER_TYPES = (
    'review_pass', 'review_revision', 'review_escalate', 'review_emergency_halt',
)

# Required fields per marker type. Keep aligned with agents/mirror/CLAUDE.md's
# Marker formats section — if either side drifts, the contract is broken.
REQUIRED_FIELDS = {
    'review_pass':            {'task_id', 'pr_url', 'summary'},
    'review_revision':        {'task_id', 'pr_url', 'findings', 'severity', 'confidence'},
    'review_escalate':        {'task_id', 'pr_url', 'reason', 'severity', 'confidence'},
    'review_emergency_halt':  {'task_id', 'pr_url', 'reason', 'evidence'},
}

# Severity vocabulary — Dial 3 per the 5a sign-off. The full set; individual
# markers narrow this further (see ALLOWED_SEVERITY_BY_MARKER). EMERGENCY_HALT
# has no `severity` field (the marker type itself encodes critical+safety).
ALLOWED_SEVERITY = ('low', 'medium', 'high', 'critical')

# D3.5 5a m-1 review fix: per-marker severity narrowing. The marker grammar
# in agents/mirror/CLAUDE.md says REVISION top-level severity is low|medium
# only (high → ESCALATE; critical → EMERGENCY_HALT), and ESCALATE is high
# only. Without this gate the handler accepts a REVISION at severity=critical,
# which routes to Forge for inline auto-fix when it should have been an
# EMERGENCY_HALT.
ALLOWED_SEVERITY_BY_MARKER = {
    'review_revision': ('low', 'medium'),
    'review_escalate': ('high',),
}

# Confidence vocabulary — applies to REVIEW_REVISION and REVIEW_ESCALATE.
# REVIEW_PASS implicitly has confidence: high (PASS requires it; see
# agents/mirror/CLAUDE.md "What REVIEW_PASS requires"). EMERGENCY_HALT
# carries `evidence` instead of a confidence value.
ALLOWED_CONFIDENCE = ('high', 'medium', 'low')

# Default revision budget when the dispatch envelope doesn't set one.
# Read from config/agent-models.json `loop_bounds.max_revisions` at dispatch
# time; the default here matches that file but is local to keep this module
# config-free.
DEFAULT_MAX_REVISIONS = 3


# -------------------- exceptions --------------------

class ReviewHandlerError(Exception):
    """Base for handler errors that the notifier should surface to Beacon."""


class MalformedMirrorMarker(ReviewHandlerError):
    """Mirror emitted a marker block whose JSON is unparseable or invalid."""


class MultipleMirrorMarkers(ReviewHandlerError):
    """Mirror emitted more than one marker — ambiguous which to act on."""


# -------------------- marker extraction --------------------

def parse_mirror_marker(
    mirror_response: str,
) -> tuple[Optional[str], Optional[dict[str, Any]], str]:
    """Find the one valid review marker block in Mirror's claude output.

    Returns `(marker_type, payload, narrative_without_marker)` where:
      - marker_type ∈ {'review_pass', 'review_revision',
                       'review_escalate', 'review_emergency_halt'} or None
      - payload is the parsed JSON object or None
      - narrative_without_marker is the response with the marker block excised
        (so the notifier doesn't echo the raw marker in the notify prompt)

    Raises MalformedMirrorMarker if a marker delimiter pair is present but the
    JSON is unparseable, not an object, missing required fields, or carries
    an invalid severity/confidence value.

    Raises MultipleMirrorMarkers if Mirror emitted two or more marker blocks
    in the same response — including TWO OF THE SAME TYPE. Routing must be
    unambiguous; the notifier dead-letters multi-marker outputs back to Mirror
    so she can re-emit cleanly.
    """
    if not mirror_response:
        return None, None, mirror_response or ''

    # Find ALL marker blocks (every type, every occurrence). Multiple markers
    # of any kind is an error — the routing decision must be unambiguous.
    all_matches: list[tuple[str, re.Match]] = []
    for mtype, regex in (
        ('review_pass', REVIEW_PASS_MARKER_RE),
        ('review_revision', REVIEW_REVISION_MARKER_RE),
        ('review_escalate', REVIEW_ESCALATE_MARKER_RE),
        ('review_emergency_halt', REVIEW_EMERGENCY_HALT_MARKER_RE),
    ):
        for m in regex.finditer(mirror_response):
            all_matches.append((mtype, m))

    if not all_matches:
        # D3.5 5b-followup Bug A: same diagnostic-error pattern as the
        # Forge handler. Distinguish "no delimiters" from "delimiters
        # present, JSON missing." If Mirror writes prose between
        # `=== REVIEW_PASS ===` and `=== END_REVIEW_PASS ===` instead of
        # a JSON object, the strict regex misses; this loose check fires
        # the more actionable error.
        loose_matches = _LOOSE_MIRROR_DELIMITER_RE.findall(mirror_response)
        if loose_matches:
            kinds = list(dict.fromkeys(loose_matches))
            raise MalformedMirrorMarker(
                f'Marker opening delimiter(s) found ({kinds}) but no valid '
                f'JSON object between them and the closing delimiter. The '
                f'content INSIDE `=== XXX ===` and `=== END_XXX ===` MUST '
                f'be a single JSON object with the required fields for that '
                f'marker type (see agents/mirror/CLAUDE.md). NOT prose. NOT '
                f'a sentence. Put your review narrative ABOVE the marker '
                f'block (Beacon reads it). Put strict JSON INSIDE.'
            )
        return None, None, mirror_response

    if len(all_matches) > 1:
        kinds = sorted({k for k, _ in all_matches})
        kind_counts = {k: sum(1 for kk, _ in all_matches if kk == k) for k in kinds}
        kinds_desc = ', '.join(f'{k}={kind_counts[k]}' for k in kinds)
        raise MultipleMirrorMarkers(
            f'Mirror emitted {len(all_matches)} marker blocks ({kinds_desc}); '
            f'expected exactly one. Re-emit a single REVIEW_PASS, REVIEW_REVISION, '
            f'REVIEW_ESCALATE, or REVIEW_EMERGENCY_HALT block.'
        )

    marker_type, match = all_matches[0]
    raw_json = match.group(1)
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise MalformedMirrorMarker(
            f'{marker_type} marker has invalid JSON: {e}'
        ) from e

    if not isinstance(payload, dict):
        raise MalformedMirrorMarker(
            f'{marker_type} marker payload must be a JSON object, '
            f'got {type(payload).__name__}'
        )

    required = REQUIRED_FIELDS[marker_type]
    missing = required - set(payload.keys())
    if missing:
        raise MalformedMirrorMarker(
            f'{marker_type} marker missing required fields: {sorted(missing)}'
        )

    # Severity validation — only markers that carry a severity field. The
    # per-marker narrowing (ALLOWED_SEVERITY_BY_MARKER) enforces the routing
    # contract: REVISION can only be low/medium; ESCALATE only high; critical
    # severity belongs in EMERGENCY_HALT, not the severity field.
    if marker_type in ('review_revision', 'review_escalate'):
        severity = payload.get('severity')
        allowed_for_marker = ALLOWED_SEVERITY_BY_MARKER.get(
            marker_type, ALLOWED_SEVERITY,
        )
        if severity not in allowed_for_marker:
            raise MalformedMirrorMarker(
                f'{marker_type} marker has severity {severity!r}; must be '
                f'one of {list(allowed_for_marker)} (critical severity '
                f'belongs in REVIEW_EMERGENCY_HALT, not the severity field; '
                f'high severity belongs in REVIEW_ESCALATE, not REVISION)'
            )
        confidence = payload.get('confidence')
        if confidence not in ALLOWED_CONFIDENCE:
            raise MalformedMirrorMarker(
                f'{marker_type} marker has invalid confidence {confidence!r}; '
                f'must be one of {list(ALLOWED_CONFIDENCE)}'
            )

    # REVIEW_REVISION findings: must be a list of dicts. Each finding's
    # internal structure is documented in CLAUDE.md but we don't validate
    # individual finding shape here — Mirror owns that contract. We do
    # require findings be a non-empty list (an empty findings list means
    # the marker should have been REVIEW_PASS instead).
    if marker_type == 'review_revision':
        findings = payload.get('findings')
        if not isinstance(findings, list) or len(findings) == 0:
            raise MalformedMirrorMarker(
                'review_revision marker `findings` must be a non-empty list; '
                'a revision with no findings should have been REVIEW_PASS'
            )

    narrative = (
        mirror_response[: match.start()] + mirror_response[match.end():]
    ).strip()
    return marker_type, payload, narrative


# -------------------- confidence-promote decision --------------------

def should_auto_promote(
    marker_type: str, payload: dict[str, Any],
) -> bool:
    """REVIEW_REVISION with `confidence: low` auto-promotes to ESCALATE.

    Rationale: if Mirror herself is uncertain whether her finding is real,
    the right move is "kick to Beacon" not "kick to Forge for auto-fix."
    The auto-fix loop assumes Mirror's confidence; without it the loop
    risks Forge revising against a false-positive finding.

    Other marker types never auto-promote. REVIEW_PASS implicitly has
    high confidence (PASS requires it). REVIEW_ESCALATE is already at
    the escalation tier. EMERGENCY_HALT bypasses confidence entirely.
    """
    if marker_type != 'review_revision':
        return False
    return payload.get('confidence') == 'low'


# -------------------- revision-budget decision (forward-compat for 5b) --------------------

def evaluate_revision_budget(
    task_envelope: dict[str, Any],
) -> tuple[str, int, int]:
    """Decide whether another revision round is permitted.

    Returns `(decision, next_count, max_count)` where:
      - decision ∈ {'allow', 'exhausted'}
      - next_count is the value the count would take after this round
      - max_count is the envelope's max_revisions (or DEFAULT_MAX_REVISIONS)

    'allow' means 5b can route a revision-task back to Forge with
    `revision_count = next_count`.

    'exhausted' means we've hit the budget — 5b should convert the
    revision request to an ESCALATE-shaped notify (Beacon takes over).

    5a doesn't call this — included for parity with
    forge_preflight_handler.evaluate_clarification_budget and so 5b's
    integration is a one-line wire-up rather than a fresh design pass.
    """
    current = task_envelope.get('revision_count', 0)
    if not isinstance(current, int) or current < 0:
        current = 0
    max_count = task_envelope.get('max_revisions', DEFAULT_MAX_REVISIONS)
    if not isinstance(max_count, int) or max_count < 0:
        max_count = DEFAULT_MAX_REVISIONS
    next_count = current + 1
    if next_count > max_count:
        return 'exhausted', next_count, max_count
    return 'allow', next_count, max_count


def revisions_remaining(task_envelope: dict[str, Any]) -> int:
    """How many revision rounds Forge has left, given current count.

    Used by the 5b notify-prompt template when telling Forge "you have N
    revisions left" inside a revision-task dispatch.
    """
    current = task_envelope.get('revision_count', 0)
    if not isinstance(current, int) or current < 0:
        current = 0
    max_count = task_envelope.get('max_revisions', DEFAULT_MAX_REVISIONS)
    if not isinstance(max_count, int) or max_count < 0:
        max_count = DEFAULT_MAX_REVISIONS
    return max(0, max_count - current)


# -------------------- routing helpers --------------------

def derive_notify_source(processing_agent: str = 'mirror') -> str:
    """Source field for the notify task built from a Mirror marker.

    All four Mirror markers are terminal verdicts (no clarification leg in
    5a — REVIEW_QUESTION lands in 5b per the sign-off), so they all route
    via the `mirror-result` channel regardless of marker type.
    """
    return f'{processing_agent}-result'


def derive_intent(
    marker_type: str,
    auto_promoted: bool = False,
    budget_exhausted: bool = False,
) -> str:
    """Map (marker_type, auto_promoted, budget_exhausted) to the notify intent string.

    Returns one of dispatch_validator's ALLOWED_INTENTS:

      review_pass            → 'review-pass'
      review_revision        → 'review-revision'  (under budget, high confidence)
                             → 'review-escalate'  (auto_promoted=True; low confidence)
                             → 'review-escalate'  (budget_exhausted=True; D3.5 5b)
      review_escalate        → 'review-escalate'
      review_emergency_halt  → 'review-emergency-halt'

    The notify-prompt template uses this to pick the right Beacon-side
    action block. Auto-promote converts revision → escalate so Beacon sees
    the actual routing decision (and so 5b/5c don't have to disambiguate
    "low-confidence revision" from "escalation" in their handlers).

    D3.5 commit 5b: budget_exhausted (revision_count + 1 > max_revisions)
    also downgrades to escalate. Signed-off path: no new intent vocabulary;
    Beacon's existing escalate handler renders it; the reason field carries
    "revision budget exhausted on round N" via build_auto_promote_reason's
    cousin (build_budget_exhausted_reason). Larry gets the DM via 5a-followup
    auto-DM pipe (escalate is a terminal intent).
    """
    if marker_type == 'review_pass':
        return 'review-pass'
    if marker_type == 'review_revision':
        if auto_promoted or budget_exhausted:
            return 'review-escalate'
        return 'review-revision'
    if marker_type == 'review_escalate':
        return 'review-escalate'
    if marker_type == 'review_emergency_halt':
        return 'review-emergency-halt'
    return 'result-notification'


def build_budget_exhausted_reason(
    payload: dict[str, Any], next_count: int, max_count: int,
) -> str:
    """Frame revision-budget-exhaustion in Beacon-readable terms.

    Used when REVIEW_REVISION auto-downgrades to ESCALATE because the
    revision_count + 1 would exceed max_revisions. Preserves Mirror's
    finding context so Beacon sees what she was asking for + that the
    auto-fix loop ran out of attempts (not that Mirror's findings were
    bad — just that they couldn't be resolved inline within budget).
    """
    findings = payload.get('findings') or []
    n = len(findings) if isinstance(findings, list) else 0
    severity = payload.get('severity', '?')
    return (
        f'Mirror emitted REVIEW_REVISION (severity={severity}, {n} finding(s)) '
        f'but revision_count would reach {next_count}, exceeding the budget '
        f'of {max_count}. Routing as ESCALATE: the Forge↔Mirror auto-fix '
        f'loop has exhausted attempts on this task. Decide: revise the spec '
        f'(new APPROVAL_REQUEST with a cleaner approach) or accept the PR '
        f'as-is with caveats noted.'
    )


def build_auto_promote_reason(
    payload: dict[str, Any],
) -> str:
    """Frame a low-confidence revision in Beacon-readable terms.

    Used when REVIEW_REVISION auto-promotes to ESCALATE: Beacon sees the
    rationale ("Mirror said revise but flagged low confidence; routing as
    escalate") so she can decide whether to push back or accept.
    """
    findings = payload.get('findings') or []
    n = len(findings) if isinstance(findings, list) else 0
    return (
        f'Mirror emitted REVIEW_REVISION with confidence: low across {n} '
        f'finding(s). Auto-promoted to ESCALATE because the revision loop '
        f'should not run against findings Mirror herself is uncertain about. '
        f'Decide: revise the spec (new APPROVAL_REQUEST) or push back to '
        f'Mirror with "actually proceed as-is" (clarify-back leg lands in 5b).'
    )
