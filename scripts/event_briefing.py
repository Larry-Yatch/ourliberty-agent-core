#!/usr/bin/env python3
"""event_briefing.py — deterministic plain-language meaning layer for chain_events.

Sibling of ``missions_narrator.py`` (which briefs parked captures + funnel
missions): this module briefs **chain_events** rows — ALERT (``larry_alert`` /
``sentinel_alert``) and ``autonomy_decision`` — so the dashboard's
Operations/Alerts panel and Automated Work feed read a row in Larry's terms
(*what · why it matters · what to do*, plus a risk badge) instead of a terse
machine subject. It is the agent-core #5 backend half of the autonomy roadmap.

Field contract is identical to the narrator's (so the SAME frontend renderer —
``BriefingBlock`` / ``RiskBadge`` — renders every surface)::

    {"briefing": {"what": ..., "why": ..., "suggest": ...},
     "risk": "safe" | "medium" | "careful",
     "risk_note": "<one sentence>"}      # omitted for safe

DESIGN (why this lives here and not in the narrator):

  - DETERMINISTIC + STDLIB-ONLY. NO LLM, NO network. Both call sites are on a
    latency-sensitive path — the shipper's per-line emit loop and the dashboard
    reader's hot API path — so a blocking LLM round-trip (the narrator's Opus
    voice) is the wrong tool. The narrator's own deterministic
    ``render_raw_briefing`` is the model: honest plain English with zero I/O.
    (LLM polish on alerts is a deliberate non-goal for v1 — alert subjects are
    terse machine strings, briefings age out of the panel in 7 days, and the
    translation layer below already supplies curated human prose.)

  - SELF-CONTAINED RISK PRIMITIVES. ``map_risk`` + the careful-class keyword
    escalator are intentionally a small co-maintained copy of the narrator's
    (missions_narrator.map_risk / classify_careful), NOT a shared import: the
    shipper is a long-running daemon and importing the narrator would pull its
    heavy capture/GC dependency chain into the daemon's import surface. The
    keyword list mirrors the narrator's by intent (irreversible/outward/spendy).

  - ALERTS REUSE THE TRANSLATION LAYER. ``config/alert-translations.json`` is
    already the curated plain-language layer for healer alerts (keyed by
    ``(source, subject)``). ``alert_briefing`` reuses ``alert_triage_state``'s
    canonical ``_translation_match`` resolver (lazily imported so its heavier
    deps never load at shipper start, and a failure degrades to "no briefing"
    rather than crashing the daemon) — so the lookup rule has ONE
    implementation. ``escalation`` (pulse) is intentionally NOT briefed: it is
    the un-translatable source and the panel falls back to its raw headline.

stdlib only.
"""
from __future__ import annotations

import re
from typing import Any, Optional

VALID_RISKS = ('safe', 'medium', 'careful')

# Irreversible / outward-facing / spendy markers — the escalator that pushes a
# row up to ``careful`` regardless of its base risk. Co-maintained with
# missions_narrator._CAREFUL_KEYWORDS (same intent). Matched on WORD BOUNDARIES
# so "drops" doesn't trip "drop"; a false positive only over-warns, never under.
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

# Translation-table display vocab (config/alert-translations.json _schema):
# severity URGENT/WARNING/INFO, tier NOW/SOON/FYI. Each maps cleanly onto the
# three-level risk dial. Severity is the primary signal; tier is the fallback.
_SEVERITY_RISK = {'URGENT': 'careful', 'WARNING': 'medium', 'INFO': 'safe'}
_TIER_RISK = {'NOW': 'careful', 'SOON': 'medium', 'FYI': 'safe'}

# The producing alert record's own queue-severity field (distinct from the
# translation's display severity) — used only when no translation matched.
_RECORD_SEVERITY_RISK = {
    'red': 'careful', 'high': 'careful', 'critical': 'careful', 'urgent': 'careful',
    'yellow': 'medium', 'medium': 'medium', 'warning': 'medium',
    'green': 'safe', 'low': 'safe', 'info': 'safe',
}


def classify_careful_text(text: str) -> bool:
    """True if free text names irreversible / outward-facing / spendy work."""
    return bool(text) and _CAREFUL_PATTERN.search(text) is not None


def map_risk(action: Optional[str], careful: bool) -> str:
    """Map a trust action + careful flag to a risk level (mirrors
    missions_narrator.map_risk): careful wins over everything; auto_approve ⇒
    safe; force_ask ⇒ medium; reject / anything unknown ⇒ careful (fail toward
    caution)."""
    if careful:
        return 'careful'
    if action == 'auto_approve':
        return 'safe'
    if action == 'force_ask':
        return 'medium'
    return 'careful'


# ------------------------- alert briefing -------------------------

# Which chain-event types carry an operator alert we brief. ``escalation``
# (pulse) is deliberately excluded — it is the un-translatable source.
ALERT_EVENT_TYPES = ('larry_alert', 'sentinel_alert')

# Per-risk urgency line (the briefing's "why it matters") + the catch note.
_ALERT_WHY = {
    'careful': 'This is flagged urgent — it needs your attention now.',
    'medium': 'Worth a look soon — not an emergency, but don’t let it sit.',
    'safe': 'Informational — no action needed.',
}
_ALERT_RISK_NOTE = {
    'careful': 'Urgent: the chain may be blocked until this is handled.',
    'medium': 'Needs attention within hours, before it compounds.',
}


def _alert_risk(entry: dict[str, Any], record: dict[str, Any]) -> str:
    """Risk for an alert. Primary = the translation's curated severity (then
    tier); fallback = the producing record's own severity; default medium (fail
    toward attention). The careful-keyword escalator can push any of these to
    careful."""
    risk = _SEVERITY_RISK.get(str(entry.get('severity') or '').upper())
    if risk is None:
        risk = _TIER_RISK.get(str(entry.get('tier') or '').upper())
    if risk is None:
        risk = _RECORD_SEVERITY_RISK.get(str(record.get('severity') or '').lower())
    if risk is None:
        risk = 'medium'
    haystack = ' '.join(str(record.get(k) or '') for k in ('subject', 'intent', 'body'))
    if classify_careful_text(haystack):
        risk = 'careful'
    return risk


def alert_briefing(record: dict[str, Any], event_type: str) -> Optional[dict[str, Any]]:
    """Author the meaning layer for one ``larry_alert`` / ``sentinel_alert``
    record (the raw larry-alerts.jsonl / sentinel-alerts.jsonl entry, which
    still carries ``source`` + ``subject``).

    Returns ``{"briefing", "risk", "risk_note"?}`` to merge into the row's
    payload, or ``None`` when there is no honest plain-English briefing to add
    (wrong event type, non-dict record, or no translation match) — the caller
    leaves the payload as-is and the panel falls back to the raw headline.

    NEVER raises: the translation layer is consulted best-effort, so a missing
    config or an import failure degrades to ``None`` and never breaks the
    shipper's emit loop."""
    if event_type not in ALERT_EVENT_TYPES or not isinstance(record, dict):
        return None

    source = record.get('source')
    subject = record.get('subject')
    try:
        # Lazy import: alert_triage_state pulls larry_alerts + cycle_prime_ledger,
        # which we don't want at the shipper's module-load time. Reusing its
        # canonical resolver keeps the lookup_rule single-sourced.
        from alert_triage_state import _translation_match, load_translations
        translations = load_translations()
        entry = _translation_match(
            translations, str(source or ''), subject,
            record.get('intent'), record.get('kind'),
        )
    except Exception:  # noqa: BLE001 — never break emit on a translation hiccup
        return None
    if not isinstance(entry, dict) or not entry:
        return None

    summary = (entry.get('plain_language_summary') or '').strip()
    action = (entry.get('recommended_action') or '').strip()
    if not summary and not action:
        return None  # an empty translation is no better than the headline

    risk = _alert_risk(entry, record)
    briefing = {
        'what': summary or (str(subject).strip() if subject else 'An alert fired.'),
        'why': _ALERT_WHY.get(risk, _ALERT_WHY['medium']),
        'suggest': action or 'Check the alert detail below.',
    }
    fields: dict[str, Any] = {'briefing': briefing, 'risk': risk}
    note = _ALERT_RISK_NOTE.get(risk)
    if note is not None:
        fields['risk_note'] = note
    return fields


# ------------------------- autonomy_decision briefing -------------------------

_DECISION_SUGGEST = {
    'careful': 'Worth a quick look — it ran on its own but touches something '
               'sensitive.',
    'medium': 'A glance is worth it — this has wider effects than it looks.',
    'safe': 'No action needed — low-risk and already moving.',
}
_DECISION_RISK_NOTE = {
    'careful': 'Auto-started despite touching something hard to undo — confirm '
               'it was the right call.',
    'medium': 'Auto-started; a change here can reach further than it appears.',
}


def decision_briefing(item: dict[str, Any]) -> dict[str, Any]:
    """Author the meaning layer for one ``autonomy_decision`` feed item (the
    dict the automated-work reader already builds: summary / target_repo /
    task_type / decision / rule_label …).

    Always returns ``{"briefing", "risk", "risk_note"?}`` — every auto-fired
    decision has an honest deterministic briefing (unlike alerts, there is no
    "no translation" hole). Risk = ``map_risk(decision, careful)`` so an
    auto_approved row that nonetheless touches careful-class work surfaces as
    ``careful`` — exactly the signal worth a second glance."""
    decision = item.get('decision')
    summary = (item.get('summary') or '').strip()
    repo = (item.get('target_repo') or '').strip()
    task_type = (item.get('task_type') or '').strip()
    rule_label = (item.get('rule_label') or '').strip()

    careful = classify_careful_text(f'{summary} {task_type}')
    risk = map_risk(decision, careful)

    if summary:
        what = summary
    elif task_type and repo:
        what = f'{task_type} in {repo}'
    elif task_type:
        what = task_type
    else:
        what = 'The team started a piece of work on its own.'

    rule_clause = f' (rule: {rule_label})' if rule_label else ''
    why = f'The trust policy let this run without asking you{rule_clause}.'

    briefing = {
        'what': what,
        'why': why,
        'suggest': _DECISION_SUGGEST.get(risk, _DECISION_SUGGEST['safe']),
    }
    fields: dict[str, Any] = {'briefing': briefing, 'risk': risk}
    note = _DECISION_RISK_NOTE.get(risk)
    if note is not None:
        fields['risk_note'] = note
    return fields
