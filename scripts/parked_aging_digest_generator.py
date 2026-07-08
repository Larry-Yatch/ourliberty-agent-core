#!/usr/bin/env python3
"""parked_aging_digest_generator.py — Missions v2 Phase 2 parked-&-aging digest.

Spec: agents/beacon/specs/missions-v2-phase2-resurfacing-and-derive.md § 6.

A small, structured producer modeled on scripts/ceo_digest_generator.py (timer
cadence + atomic write + fail-safe + heartbeat), but with NO LLM voice: this
digest is a plain structured artifact the dashboard renders as a read-only
"catch me up" card answering "what's parked & aging — promote / drop / snooze?"

It reads captures.json, selects `state == "parked"` captures already flagged
`aging: true`, and writes a digest artifact (parked count, aging count, the
aging items with title + origin repo + age) to a blackboard JSON file the
dashboard reads. Daily cadence via a systemd timer; the same entrypoint regen-
erates the artifact on demand (the artifact is overwritten, never appended).

REUSE THE AGING CLOCK — DO NOT REINVENT IT (spec § 6 enforcement):
The GC healer (scripts/heal_missions_card_gc.py, AGING_BUSINESS_DAYS = 5
business days) is the SINGLE definition of "aging" across the system. This
generator selects on the persisted `cap["aging"]` flag ONLY; it contains no
business-day math and does not recompute the aging decision. The per-item
`age_days` field is a display-only CALENDAR-day count derived from
`last_touched` for the card to render "N days ago" — it never gates selection.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))

# captures.json lives in the agent-core repo (same checkout as this script). The
# GC healer keeps it current on main; we read whatever is on disk here. An env
# override keeps the path test-controllable.
CAPTURES_REL = 'agents/beacon/captures.json'
DIGEST_ARTIFACT = AGENTS_ROOT / 'blackboard' / 'parked-aging-digest.json'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'parked-aging-digest-generator.heartbeat'

# proposed-mission pile inputs (READ-ONLY). The pile lives on the Missions board
# (missions.json), and two existing read-only passes already reason about it:
# mission_staleness (which proposed cards are stale) and mission_rank (the
# canonical portfolio-relevance ordering). Paths mirror those two modules and are
# env-overridable for test isolation, like OURLIBERTY_CAPTURES_FILE above.
MISSIONS_REL = ('agents', 'beacon', 'workspace', 'missions.json')
MISSION_RANK_REL = ('state', 'mission-rank.json')
MISSION_STALENESS_REL = ('state', 'mission-staleness-candidates.json')

SCHEMA_VERSION = 2
VALID_TRIGGERS = ('scheduled', 'on-demand')


def resolve_log_dir() -> Path:
    """Log dir, honoring OURLIBERTY_LOG_DIR (test isolation; see conftest)."""
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    return Path(override) if override else AGENTS_ROOT / 'logs'


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        log_dir = resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / 'parked-aging-digest-generator.log', 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- captures.json (read) --------------------


def resolve_captures_path() -> Path:
    override = os.environ.get('OURLIBERTY_CAPTURES_FILE')
    return Path(override) if override else _SCRIPT_DIR.parent / CAPTURES_REL


def resolve_missions_path() -> Path:
    override = os.environ.get('OURLIBERTY_MISSIONS_FILE')
    return Path(override) if override else AGENTS_ROOT.joinpath(*MISSIONS_REL)


def resolve_mission_rank_path() -> Path:
    override = os.environ.get('OURLIBERTY_MISSION_RANK_FILE')
    return Path(override) if override else AGENTS_ROOT.joinpath(*MISSION_RANK_REL)


def resolve_mission_staleness_path() -> Path:
    override = os.environ.get('OURLIBERTY_MISSION_STALENESS_FILE')
    return Path(override) if override else AGENTS_ROOT.joinpath(*MISSION_STALENESS_REL)


def read_captures_registry(path: Path) -> Optional[dict[str, Any]]:
    """Load captures.json as a registry dict. Missing file → fresh empty
    registry (a valid "nothing parked" state). Malformed/wrong-shape → None so
    the caller reports + skips rather than writing a bogus artifact. Mirrors
    heal_missions_card_gc.read_captures_registry's fail-safe contract."""
    if not path.exists():
        return {'schema_version': 1, 'captures': []}
    try:
        raw = path.read_text()
        data = json.loads(raw) if raw.strip() else {'schema_version': 1, 'captures': []}
    except (OSError, json.JSONDecodeError) as e:
        log(f'captures.json malformed/unreadable ({path}): {e} — skipping this run', 'WARN')
        return None
    if not isinstance(data, dict) or not isinstance(data.get('captures'), list):
        log(f'captures.json shape invalid ({path}) — skipping this run', 'WARN')
        return None
    return data


# -------------------- selection (pure; aging-flag-only) --------------------


def select_aging_parked(
    registry: dict[str, Any], now: datetime,
) -> list[dict[str, Any]]:
    """The parked-&-aging set: captures with `state == "parked"` AND the GC
    healer's persisted `aging` flag set true. PURE and flag-only — no date math,
    no business-day computation, no aging recompute (spec § 6 enforcement).

    A capture snoozed past `now` is suppressed from the digest (Phase 3 § 4.3)
    — it isn't surfaced as aging while the snooze holds."""
    out: list[dict[str, Any]] = []
    for cap in registry.get('captures', []):
        if not isinstance(cap, dict):
            continue
        if is_snoozed(cap, now):
            continue
        if cap.get('state') == 'parked' and cap.get('aging') is True:
            out.append(cap)
    return out


def count_parked(registry: dict[str, Any], now: datetime) -> int:
    """Total parked captures (aging or not) — the denominator on the card.
    Snoozed captures are excluded so the count matches the suppressed card
    (Phase 3 § 4.3)."""
    return sum(
        1 for cap in registry.get('captures', [])
        if isinstance(cap, dict)
        and cap.get('state') == 'parked'
        and not is_snoozed(cap, now)
    )


# -------------------- rendering --------------------


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_snoozed(cap: dict[str, Any], now: datetime) -> bool:
    """True iff the capture is snoozed past `now` (`snoozed_until` in the
    future). Null / absent / unparseable / past → not snoozed (fail-open to
    visible). Mirrors dashboard_api._is_snoozed so the digest and the derive
    suppress the same set (Missions v2 Phase 3 § 4.3)."""
    until = _parse_iso_utc(cap.get('snoozed_until'))
    return until is not None and until > now


def _origin_repo(cap: dict[str, Any]) -> Optional[str]:
    origin = cap.get('origin')
    if isinstance(origin, dict):
        repo = origin.get('repo')
        if isinstance(repo, str) and repo:
            return repo
    return None


def _age_days(cap: dict[str, Any], now: datetime) -> Optional[int]:
    """Calendar days since `last_touched`, for display only ("N days ago"). NOT
    the aging criterion — that is the GC healer's business-day flag, already
    decided. Returns None when last_touched is missing/unparseable."""
    touched = _parse_iso_utc(cap.get('last_touched'))
    if touched is None:
        return None
    return max(0, (now.date() - touched.date()).days)


def build_item(cap: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        'capture_id': cap.get('id'),
        'title': cap.get('title'),
        'repo': _origin_repo(cap),
        'age_days': _age_days(cap, now),
        'last_touched': cap.get('last_touched'),
    }


def build_digest(registry: dict[str, Any], now: datetime, trigger: str,
                 proposed_pile: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Assemble the structured artifact: counts + the aging items (§ 6). The
    optional `proposed_pile` block is purely additive — existing top-level fields
    stay byte-compatible; the key is omitted when no block is supplied."""
    aging = select_aging_parked(registry, now)
    items = [build_item(cap, now) for cap in aging]
    digest: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'generated_at': now.isoformat(),
        'trigger': trigger,
        'parked_count': count_parked(registry, now),
        'aging_count': len(aging),
        'items': items,
    }
    if proposed_pile is not None:
        digest['proposed_pile'] = proposed_pile
    return digest


# -------------------- proposed-mission pile (monthly) --------------------


def _read_json_safe(path: Path) -> Optional[Any]:
    """Parse a JSON file; None on any failure (missing, malformed, unreadable).
    Never raises — the pile block degrades fail-open when an upstream input is
    absent, it never fails the run or the parked/aging half."""
    try:
        if not path.exists():
            return None
        raw = path.read_text()
    except OSError as e:
        log(f'pile input unreadable ({path}): {e}', 'WARN')
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log(f'pile input malformed ({path}): {e}', 'WARN')
        return None


def _proposed_missions(missions_data: Any) -> list[dict[str, Any]]:
    """Live proposed cards: phase == 'proposed', not archived, and NOT already
    retired (dismissed / healer retire-with-audit). Reuses the one shared
    retire semantic (mission_staleness.proposed_is_retired) so this pile counts
    exactly the set mission_staleness scores — otherwise actionable_count
    (proposed − candidates) inflates, since candidates already excludes retired
    rows but proposed would still include them."""
    if not isinstance(missions_data, dict):
        return []
    missions = missions_data.get('missions')
    if not isinstance(missions, list):
        return []
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    try:
        import mission_staleness as ms
        is_retired = ms.proposed_is_retired
    except Exception:  # noqa: BLE001 — degrade to the pre-filter behavior
        is_retired = lambda m: False  # noqa: E731
    return [m for m in missions
            if isinstance(m, dict)
            and m.get('phase') == 'proposed' and not m.get('archived')
            and not is_retired(m)]


def _created_age_days(mission: dict[str, Any], now: datetime) -> Optional[int]:
    """Calendar-day age from the card's `created` (date-only: created[:10] parsed
    as an ISO date). None when `created` is missing/unparseable — such cards are
    skipped for the age calc (spec). Mirrors mission_staleness._age_days."""
    raw = mission.get('created')
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        created = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return (now.date() - created).days


def _top_relevant(rank_data: Any, proposed: list[dict[str, Any]],
                  now: datetime) -> tuple[list[dict[str, Any]], str]:
    """Up to 3 most-relevant proposed cards + the basis used.

    Canonical ordering is mission-rank.json's `ranked[]` (pre-sorted best-first);
    each item is {name, rank_score, what} where `what` is the rank entry's
    brief.what one-liner. When the rank file is missing/empty/unusable, fall back
    to the 3 OLDEST proposed cards as {name, age_days} with basis 'age_fallback'."""
    ranked = rank_data.get('ranked') if isinstance(rank_data, dict) else None
    if isinstance(ranked, list):
        top: list[dict[str, Any]] = []
        for entry in ranked[:3]:
            if not isinstance(entry, dict):
                continue
            brief = entry.get('brief')
            what = brief.get('what') if isinstance(brief, dict) else None
            top.append({
                'name': entry.get('name'),
                'rank_score': entry.get('rank_score'),
                'what': what,
            })
        if top:
            return top, 'portfolio_rank'

    # age_fallback: 3 oldest proposed cards with a parseable `created`.
    aged = [(m, _created_age_days(m, now)) for m in proposed]
    aged = [(m, a) for m, a in aged if a is not None]
    aged.sort(key=lambda pair: pair[1], reverse=True)
    fallback = [{'name': m.get('name'), 'age_days': a} for m, a in aged[:3]]
    return fallback, 'age_fallback'


def build_proposed_pile(
    now: datetime, *,
    missions_path: Optional[Path] = None,
    rank_path: Optional[Path] = None,
    staleness_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Compute the proposed-mission pile status block for the current month.

    Fail-open by construction: a missing/malformed missions.json, rank file, or
    staleness file DEGRADES the block (empty/None fields) but never raises. Stamps
    `as_of_month` with the current UTC YYYY-MM so the caller can enforce the
    once-a-month recompute cadence."""
    m_path = missions_path or resolve_missions_path()
    r_path = rank_path or resolve_mission_rank_path()
    s_path = staleness_path or resolve_mission_staleness_path()

    proposed = _proposed_missions(_read_json_safe(m_path))
    proposed_count = len(proposed)

    # actionable_count: proposed_count minus the staleness candidates, ONLY when
    # the staleness file is present and usable; null otherwise (fail-open).
    actionable_count: Optional[int] = None
    staleness = _read_json_safe(s_path)
    if isinstance(staleness, dict):
        cands = staleness.get('candidates')
        if isinstance(cands, list):
            actionable_count = max(0, proposed_count - len(cands))

    # oldest_age_days / oldest_name: max calendar age over proposed cards with a
    # parseable `created`; None/None when none qualify.
    oldest_age_days: Optional[int] = None
    oldest_name: Optional[str] = None
    for m in proposed:
        age = _created_age_days(m, now)
        if age is None:
            continue
        if oldest_age_days is None or age > oldest_age_days:
            oldest_age_days = age
            oldest_name = m.get('name')

    top_relevant, relevance_basis = _top_relevant(_read_json_safe(r_path), proposed, now)

    return {
        'as_of_month': now.strftime('%Y-%m'),
        'proposed_count': proposed_count,
        'actionable_count': actionable_count,
        'oldest_age_days': oldest_age_days,
        'oldest_name': oldest_name,
        'relevance_basis': relevance_basis,
        'top_relevant': top_relevant,
    }


def carry_or_recompute_pile(
    now: datetime, prior_artifact: Optional[dict[str, Any]], *,
    missions_path: Optional[Path] = None,
    rank_path: Optional[Path] = None,
    staleness_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Monthly cadence gate. If the prior artifact already carries a proposed_pile
    block stamped with the current UTC month, carry it forward UNCHANGED (no
    recompute — on-demand refreshes within a month must not force one). Otherwise
    (no prior, no prior block, or a stale month) recompute and re-stamp."""
    current_month = now.strftime('%Y-%m')
    if isinstance(prior_artifact, dict):
        prior_pile = prior_artifact.get('proposed_pile')
        if isinstance(prior_pile, dict) and prior_pile.get('as_of_month') == current_month:
            return prior_pile
    return build_proposed_pile(
        now, missions_path=missions_path, rank_path=rank_path,
        staleness_path=staleness_path)


# -------------------- write (atomic) --------------------


def atomic_write_digest(path: Path, digest: dict[str, Any]) -> None:
    """tmp-in-same-dir + os.replace so the dashboard never reads a partial
    file. Mirrors heal_missions_card_gc.atomic_write_captures."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(digest, indent=2) + '\n')
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# -------------------- run --------------------


def run(now: Optional[datetime] = None, *, trigger: str = 'scheduled',
        captures_path: Optional[Path] = None,
        artifact_path: Optional[Path] = None,
        missions_path: Optional[Path] = None,
        rank_path: Optional[Path] = None,
        staleness_path: Optional[Path] = None) -> int:
    """Generate and persist one parked-&-aging digest. Returns a process exit
    code (0 ok; 1 captures.json unreadable → nothing written this run).

    The proposed_pile block refreshes once per calendar month: the prior artifact
    is read first, and a block already stamped with the current UTC month is
    carried forward unchanged (on-demand refreshes within a month don't force a
    mid-month recompute). The parked/aging half keeps refreshing every run."""
    if trigger not in VALID_TRIGGERS:
        log(f'invalid trigger {trigger!r}; expected one of {VALID_TRIGGERS}', 'ERROR')
        return 2
    heartbeat()
    now = now or datetime.now(timezone.utc)
    cap_path = captures_path or resolve_captures_path()
    out_path = artifact_path or DIGEST_ARTIFACT

    registry = read_captures_registry(cap_path)
    if registry is None:
        log('captures.json unreadable; leaving prior artifact in place', 'WARN')
        return 1

    prior_artifact = _read_json_safe(out_path)
    pile = carry_or_recompute_pile(
        now, prior_artifact if isinstance(prior_artifact, dict) else None,
        missions_path=missions_path, rank_path=rank_path,
        staleness_path=staleness_path)

    digest = build_digest(registry, now, trigger, proposed_pile=pile)
    try:
        atomic_write_digest(out_path, digest)
    except OSError as e:
        log(f'digest write failed ({out_path}): {type(e).__name__}: {e}', 'ERROR')
        return 1
    log(f'{trigger} digest written: parked={digest["parked_count"]} '
        f'aging={digest["aging_count"]} pile={pile["proposed_count"]}'
        f'@{pile["as_of_month"]} → {out_path}')
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Generate the Missions v2 parked-&-aging dashboard digest.')
    parser.add_argument(
        '--trigger', choices=VALID_TRIGGERS, default='scheduled',
        help="how this run was invoked (scheduled timer vs on-demand refresh); "
             "recorded in the artifact. Default: scheduled.")
    args = parser.parse_args(argv)
    return run(trigger=args.trigger)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
