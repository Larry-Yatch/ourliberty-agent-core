#!/usr/bin/env python3
"""missions_doorbell.py — the Parked-card conversation doorbell (Missions v2
Phase 4, step 1b).

Spec: agents/beacon/specs/missions-v2-phase4-meaning-layer.md § 9 ("Contract
F — the doorbell"). A capture card's notification loudness is gated by **risk +
blocked-state**:

  - **FYI** (quiet, awareness): a new/aging *briefed* card — surfaced in the
    daily CEO digest, no Telegram ping.
  - **blocked-on-you** (loud): the team posted a question on the card and is
    waiting on Larry — a Telegram ping that deep-links to the card. Severity
    rises to ``critical`` when the card's risk is ``careful`` so the loudest
    cards reach Larry hardest.

This module is a thin, reusable rail over the two existing primitives the spec's
reuse map names (§ 3): ``larry_alerts`` (the doorbell routing: escalate vs
digest, cooldown, retraction) and ``alert_triage_state`` (durable per-card
triage lifecycle). It adds NO bespoke notification store — the Telegram message
is just a larry-alert that deep-links to the card; no decision-making happens in
Telegram (§ 9).

Two sides of the card conversation drive the doorbell:

  - **team → Larry** (Beacon posts a question): ``ring_doorbell(blocked=True,
    ...)`` rings loud. Beacon's runtime calls this on her ``team_to_larry``
    post.
  - **Larry → team** (Larry answers on the card): ``resolve_doorbell(...)``
    clears the blocked-on-you state — retracts the stale escalate line and marks
    the triage row resolved. The dashboard's ``POST .../message`` route calls
    this so a reply silences the doorbell immediately.

``decide_doorbell`` is a pure function (no side effects) so the loudness policy
is unit-testable in isolation. ``ring_doorbell`` / ``resolve_doorbell`` perform
the side effects through the guarded ``larry_alerts`` / ``alert_triage_state``
sinks (which ``refuse_under_test`` so a test that forgets to mock fails loud).

Stdlib + the two sibling modules only.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Allow imports of sibling modules in scripts/ regardless of CWD.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import alert_triage_state  # noqa: E402
import larry_alerts  # noqa: E402

# The larry_alerts source tag for every doorbell line. `source:subject` (where
# subject is the capture_id) is the cooldown key AND the triage alert_id, so the
# ring side and the resolve side address the exact same record.
SOURCE = 'missions-doorbell'

# A card is "briefed" (eligible for an FYI) iff its meaning-layer risk is one of
# these — mirrors dashboard_api._VALID_RISKS. An un-briefed card (risk absent)
# renders the neutral state (§ 4) and does NOT ring: there's nothing to say yet.
_BRIEFED_RISKS = ('safe', 'medium', 'careful')

# Triage tiers (alert_triage_state.VALID_TIERS == (1, 2, 3, 4)). A loud
# blocked-on-you card needs Larry (tier 3); a quiet FYI is awareness-only
# (tier 4). The tier makes the durable triage row legible to Check 0 / the
# triage dashboard without inventing a parallel state.
_TIER_BLOCKED = 3
_TIER_FYI = 4


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def doorbell_key(capture_id: str) -> str:
    """The shared larry_alerts cooldown key + alert_triage_state alert_id for a
    capture's doorbell. Ring and resolve both address this exact string."""
    return f'{SOURCE}:{capture_id}'


def decide_doorbell(
    blocked: bool, risk: Optional[str],
) -> Optional[dict[str, str]]:
    """Pure loudness decision (§ 9). Returns the routing plan, or None when the
    card should not ring at all.

    - ``blocked`` (team waiting on Larry) → loud **blocked-on-you**: route
      ``escalate`` (DM now); severity ``critical`` iff risk is ``careful``,
      else ``warning``.
    - not blocked but **briefed** (risk set) → quiet **FYI**: route ``digest``
      (no DM; surfaced in the CEO digest). severity ``warning`` (carried for
      the alert record; digest route never DMs regardless).
    - not blocked and un-briefed → **None** (neutral state; nothing to ring).
    """
    if blocked:
        severity = 'critical' if risk == 'careful' else 'warning'
        return {
            'loudness': 'blocked-on-you',
            'route': 'escalate',
            'severity': severity,
        }
    if risk in _BRIEFED_RISKS:
        return {
            'loudness': 'fyi',
            'route': 'digest',
            'severity': 'warning',
        }
    return None


def _format_message(
    loudness: str, capture_id: str,
    title: Optional[str], deep_link: Optional[str],
) -> str:
    label = title or capture_id
    if loudness == 'blocked-on-you':
        body = f'The team has a question on "{label}" and is waiting on you.'
    else:
        body = f'Briefed parked card "{label}" is ready for your review.'
    if deep_link:
        body += f' {deep_link}'
    return body


def ring_doorbell(
    *,
    capture_id: str,
    blocked: bool,
    risk: Optional[str],
    title: Optional[str] = None,
    deep_link: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ring the doorbell for a capture card per the risk + blocked-state policy.

    Appends a larry-alert (route gates the loudness) AND records a durable
    triage row so the blocked-on-you state can later be resolved. A no-ring
    decision (un-briefed, not blocked) is a clean no-op — no alert, no triage.

    Returns a result dict: ``{rung, capture_id, loudness, route?, severity?,
    appended?, alert_id?}``. ``appended`` is whether the alert line was written
    (False when larry_alerts cooldown/silence suppressed it); the triage row is
    still recorded so resolve has something to clear.

    Side effects go through ``larry_alerts`` / ``alert_triage_state``, which
    ``refuse_under_test`` — a test that reaches here un-mocked fails loud.
    """
    plan = decide_doorbell(blocked, risk)
    if plan is None:
        return {'rung': False, 'capture_id': capture_id, 'loudness': None}

    message = _format_message(plan['loudness'], capture_id, title, deep_link)
    appended = larry_alerts.append_alert(
        source=SOURCE,
        severity=plan['severity'],
        message=message,
        subject=capture_id,
        suggested_action=deep_link,
        route=plan['route'],
    )
    alert_id = doorbell_key(capture_id)
    tier = _TIER_BLOCKED if plan['loudness'] == 'blocked-on-you' else _TIER_FYI
    alert_triage_state.record_triage(
        alert_id=alert_id,
        tier=tier,
        decision=f"ring-{plan['loudness']}",
        rationale=message,
        route=plan['route'],
    )
    return {
        'rung': True,
        'capture_id': capture_id,
        'appended': bool(appended),
        'alert_id': alert_id,
        **plan,
    }


def resolve_doorbell(
    *,
    capture_id: str,
    resolution: str = 'larry-replied',
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Clear a card's blocked-on-you state — call when Larry answers on the card.

    Retracts the stale ``escalate`` larry-alert line (so it stops nagging) and
    marks the triage row ``resolved``. Both are no-ops when there's nothing
    pending (no matching alert line / no triage row), so this is safe to call on
    every operator message regardless of whether the card was actually blocked.

    Returns ``{capture_id, removed, resolved}``: ``removed`` is the count of
    retracted alert lines; ``resolved`` is whether a triage row transitioned.
    """
    alert_id = doorbell_key(capture_id)
    removed = larry_alerts.resolve_alert(alert_id)
    resolved = alert_triage_state.mark_resolved(
        alert_id, _now(now).isoformat(), resolution,
    )
    return {
        'capture_id': capture_id,
        'removed': int(removed or 0),
        'resolved': bool(resolved),
    }
