#!/usr/bin/env python3
"""heal_approvals_surface_drift.py — OBSERVE-ONLY parity sentinel for the decide tab.

INVARIANT UNDER TEST: every item awaiting Larry appears on the decide tab, and
clears when resolved.

`heal_unregistered_approval` is the REMEDIATOR for that invariant — it promotes
stranded items onto the tab and retires them on resolution. But a reconciler with
no checker is one regression away from silent: a promoter whose predicate is
re-narrowed, or a new direct-register flow nobody wired into it, looks EXACTLY
like a working one. That is not hypothetical — an actionable escalation
(`pipeline-stall:unrouted-pr:PR#1084`) and a directly-registered approval
(`suite-guardian-graduation-stage-1`) both failed to reach the tab and went
unnoticed until Larry spotted the counts disagreeing.

This sentinel is the CHECKER. Each tick it computes two sets and reports the
symmetric difference:

  A = items AWAITING LARRY, read from the authoritative stores:
      * `beacon-pending-approvals.json` pending[] with status == 'pending'
      * unresolved records in `for-larry-escalations.json`
      * `larry-alerts.jsonl` rows with route == 'escalate' AND needs_larry
      minus anything a resolution signal or a `premise_stale` marker says is
      already moot.
  B = OPEN decide-tab cards — the `approval_request` chain_events with
      read_at IS NULL, i.e. exactly what the tab renders.

  A \\ B  -> 'awaiting you but not on the tab'  (the promoter gap; #1084 class)
  B \\ A  -> 'tab card with nothing awaiting'   (a retire the reconciler missed)

Deliberately INDEPENDENT of the promoter's own classifier. Reusing
`is_approval_class` would make the check circular — a re-narrowed predicate would
shrink A and B together and the drift would stay invisible. A is derived straight
from the stores; that independence is the whole point.

OBSERVE-ONLY (hard requirement): this module NEVER promotes, retires, resolves,
or otherwise writes to any approval store or to chain_events. The only state it
owns is its own grace/dedup ledger. A bug in here can therefore never corrupt the
tab — the worst it can do is alert wrongly.

ACTIONABLE-ONLY discipline:
  * GRACE — a divergence must persist across N consecutive ticks before it
    alerts. A promote or retire caught mid-flight is normal churn, not drift.
  * DEDUP — at most ONE alert per divergent item, keyed on its identity in the
    ledger. The entry is dropped when the item reconciles, so a LATER recurrence
    can alert again.
  * Every alert NAMES the item, the direction, and the remedy — never a count.

Stdlib + the existing store readers / identity helpers only.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import beacon_approval_handler as approval  # noqa: E402
import heal_unregistered_approval as heal  # noqa: E402 — store readers + identity
from decision_identity import canonical_decision_key  # noqa: E402

HEALER_SOURCE = 'heal-approvals-surface-drift'

# Divergence directions. The string is part of the ledger key and the alert
# subject, so it is named once and never spelled inline.
MISSING_CARD = 'missing_card'   # in A, not in B — awaiting Larry, not on the tab
ORPHAN_CARD = 'orphan_card'     # in B, not in A — a card with nothing awaiting

# Consecutive ticks a divergence must survive before it alerts. At the ~15-min
# timer cadence this is ~45 min of persistence — comfortably longer than any
# promote/retire round trip, so in-flight churn never pages Larry.
DEFAULT_GRACE_TICKS = 3


# -------------------- paths / env --------------------
#
# Path helpers delegate to the remediator's so the two healers can never disagree
# about where the stores live.

def agents_root() -> Path:
    return heal.agents_root()


def state_file() -> Path:
    return heal.state_dir() / 'heal-approvals-surface-drift.json'


def healer_heartbeat() -> Path:
    return heal.blackboard() / 'heal-approvals-surface-drift.heartbeat'


def kill_switch() -> Path:
    return heal.kill_switch()


def log_file() -> Path:
    return agents_root() / 'logs' / 'heal-approvals-surface-drift.log'


def grace_ticks() -> int:
    """Consecutive-tick grace before a divergence alerts. Env-overridable for
    operational tuning; a malformed or non-positive value falls back to the
    default rather than disabling the grace (which would make the sentinel
    noisy, the one failure mode that gets a checker ignored)."""
    raw = os.environ.get('OURLIBERTY_APPROVAL_DRIFT_GRACE_TICKS')
    if raw:
        try:
            val = int(raw)
        except ValueError:
            return DEFAULT_GRACE_TICKS
        if val > 0:
            return val
    return DEFAULT_GRACE_TICKS


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


# -------------------- identity (pure) --------------------

def _key(value: Any) -> Optional[str]:
    """Normalize one id-ish string to the shared cross-store decision key."""
    if not isinstance(value, str) or not value.strip():
        return None
    return canonical_decision_key(value.strip()) or value.strip()


def _key_from_pr_url(pr_url: Any) -> Optional[str]:
    if not isinstance(pr_url, str) or not pr_url.strip():
        return None
    return canonical_decision_key(None, pr_url.strip())


def _aliases(*values: Any) -> list[str]:
    """Dedup + drop-None helper for an item's alias list, order preserved."""
    out: list[str] = []
    for v in values:
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


def _promoter_card_key(record: dict[str, Any]) -> Optional[str]:
    """The key the card WOULD have if `heal_unregistered_approval` promoted this
    alert — `derive_task_id(decision_identity(record))`, normalized.

    Load-bearing for apples-to-apples: an alert that carries a `pr_url` keys as
    `pr-<repo>-<n>`, but the card the promoter mints for it is titled
    `unreg-approval-<hash>`. Without this alias the two surfaces could never
    match and every promoted alert would read as permanent drift."""
    try:
        return _key(heal.derive_task_id(heal.decision_identity(record)))
    except Exception:  # noqa: BLE001 — an identity failure is never a divergence
        return None


# -------------------- set A: items awaiting Larry (pure) --------------------

def _awaiting_item(
    key: str, aliases: list[str], origin: str, label: str, remedy: str,
) -> dict[str, Any]:
    return {
        'key': key,
        'aliases': aliases,
        'origin': origin,
        'label': label,
        'remedy': remedy,
    }


_REMEDY_PENDING = (
    'A pending approval has no open card. Check '
    'heal_unregistered_approval.reconcile_beacon_pending_mint (the local-pending '
    'backfill) and the chain_events upsert path.'
)
_REMEDY_FORLARRY = (
    'An open for-Larry escalation has no open card. Check '
    'heal_unregistered_approval.is_forlarry_decision_class — the promote '
    'predicate may be narrower than the set of items that actually await you.'
)
_REMEDY_ALERT = (
    'An actionable (route=escalate, needs_larry) alert has no open card. Check '
    'heal_unregistered_approval.is_approval_class / evaluate — the promote '
    'predicate may have been re-narrowed, or the tab write may be failing.'
)
_REMEDY_ORPHAN = (
    'An open decide-tab card has nothing awaiting you behind it. Check the '
    'retire paths (heal_unregistered_approval.reconcile_retire / '
    'reconcile_beacon_pending_retire, heal_stale_approvals) — a resolved item '
    "left its card's read_at uncleared."
)


def collect_pending(state: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """Set-A contribution from the local pending store, plus the MUTED keys.

    An entry marked `premise_stale` by freshness_probe is being demoted BY
    DESIGN, so it is neither an awaiting item nor evidence that its card is an
    orphan — its aliases are muted out of BOTH directions."""
    items: list[dict[str, Any]] = []
    muted: set[str] = set()
    for entry in (state.get('pending') or []):
        if not isinstance(entry, dict):
            continue
        if entry.get('status') != 'pending':
            continue
        entry_id = entry.get('id') or entry.get('decision_key')
        aliases = _aliases(
            _key(entry_id),
            _key(entry.get('decision_key')),
            _key_from_pr_url(entry.get('pr_url')),
        )
        if not aliases:
            continue
        if 'premise_stale' in entry:
            muted.update(aliases)
            continue
        items.append(_awaiting_item(
            key=aliases[0], aliases=aliases, origin='pending',
            label=str(entry_id), remedy=_REMEDY_PENDING,
        ))
    return items, muted


def collect_for_larry(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set-A contribution from the for-Larry feed: every UNRESOLVED record.

    Deliberately wider than the promoter's `is_forlarry_decision_class` (which
    only promotes mirror-review rows). If the two disagree, that disagreement is
    exactly the drift worth surfacing — measuring A with the promoter's own
    predicate would make the check circular."""
    items: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict) or record.get('resolved') is True:
            continue
        rec_id = record.get('id')
        if not isinstance(rec_id, str) or not rec_id:
            continue
        aliases = _aliases(
            _key(rec_id),
            _key(heal.forlarry_norm_id(rec_id)),
            _key_from_pr_url(record.get('pr_url')),
            _promoter_card_key({'subject': rec_id}),
        )
        if not aliases:
            continue
        items.append(_awaiting_item(
            key=aliases[0], aliases=aliases, origin='for-larry',
            label=rec_id, remedy=_REMEDY_FORLARRY,
        ))
    return items


def is_actionable_alert(record: dict[str, Any]) -> bool:
    """True for an alert that is unambiguously AWAITING LARRY: route=escalate
    AND an explicit needs_larry stamp. Its OWN alerts are excluded — a sentinel
    that counts its own output as an awaiting item flags itself forever."""
    if not isinstance(record, dict):
        return False
    if record.get('source') == HEALER_SOURCE:
        return False
    if record.get('route', heal.approval_default_route()) != 'escalate':
        return False
    return record.get('needs_larry') is True


def collect_alerts(
    alerts: list[dict[str, Any]],
    heuristics: dict[str, Any],
    now: datetime,
    resolution_check: Callable[[dict[str, Any]], Optional[str]],
) -> list[dict[str, Any]]:
    """Set-A contribution from larry-alerts: in-window, actionable, unresolved."""
    items: list[dict[str, Any]] = []
    window = heuristics.get('scan_window_hours', heal.DEFAULT_SCAN_WINDOW_HOURS)
    for record in alerts or []:
        if not is_actionable_alert(record):
            continue
        if not heal.within_window(record, now, window):
            continue
        try:
            if resolution_check(record):
                continue
        except Exception as e:  # noqa: BLE001 — an undetermined probe is no signal
            log(f'resolution probe failed for '
                f'{record.get("subject")!r}: {type(e).__name__}: {e}', 'WARN')
        aliases = _aliases(
            _key(record.get('decision_key')),
            _key(record.get('task_id')),
            _key_from_pr_url(record.get('pr_url')),
            _promoter_card_key(record),
        )
        if not aliases:
            continue
        items.append(_awaiting_item(
            key=aliases[0], aliases=aliases, origin='alert',
            label=str(record.get('subject') or record.get('message') or '')[:160],
            remedy=_REMEDY_ALERT,
        ))
    return items


def index_awaiting(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collapse the three sources into one key -> item map.

    Two records describing the SAME decision (a repeated alert, or a for-Larry
    row that also has a pending entry) share at least one alias, so they collapse
    to a single entry. First writer wins — the caller orders the sources so the
    pending store, the most authoritative surface, keeps its label."""
    out: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for item in items:
        if any(alias in seen for alias in item['aliases']):
            continue
        out[item['key']] = item
        seen.update(item['aliases'])
    return out


def awaiting_alias_keys(items: list[dict[str, Any]]) -> set[str]:
    """Every key any awaiting item answers to — the B-side membership test."""
    keys: set[str] = set()
    for item in items:
        keys.update(item['aliases'])
    return keys


# -------------------- set B: open decide-tab cards --------------------

def _is_mock_task_id(task_id: str) -> bool:
    """Mock/fixture card ids are never real decisions. Reuses the tab tooling's
    own marker list; a failed import just means no mock filtering."""
    try:
        import triage_decisions as td  # noqa: E402 — lazy: keeps import IO out
        return bool(td._is_mock(task_id))
    except Exception:  # noqa: BLE001
        return False


def collect_cards(
    open_task_ids: set[str], muted: set[str],
) -> dict[str, str]:
    """key -> task_id for every OPEN approval_request card, minus mocks and
    minus cards whose item is muted (premise_stale)."""
    out: dict[str, str] = {}
    for task_id in sorted(open_task_ids or set()):
        if not isinstance(task_id, str) or not task_id:
            continue
        if _is_mock_task_id(task_id):
            continue
        key = _key(task_id)
        if not key or key in muted:
            continue
        out.setdefault(key, task_id)
    return out


# -------------------- divergence (pure) --------------------

def compute_divergences(
    awaiting: dict[str, dict[str, Any]],
    alias_keys: set[str],
    cards: dict[str, str],
    muted: set[str],
) -> list[dict[str, Any]]:
    """The symmetric difference, as a list of divergence records.

    An awaiting item is carded when ANY of its aliases has an open card; a card
    is backed when its key is any awaiting item's alias. Muted (premise_stale)
    keys are excluded from both directions."""
    card_keys = set(cards)
    out: list[dict[str, Any]] = []
    for key, item in sorted(awaiting.items()):
        if key in muted:
            continue
        if any(alias in card_keys for alias in item['aliases']):
            continue
        out.append({
            'direction': MISSING_CARD,
            'key': key,
            'origin': item['origin'],
            'label': item['label'],
            'remedy': item['remedy'],
        })
    for key, task_id in sorted(cards.items()):
        if key in alias_keys or key in muted:
            continue
        out.append({
            'direction': ORPHAN_CARD,
            'key': key,
            'origin': 'chain_events',
            'label': task_id,
            'remedy': _REMEDY_ORPHAN,
        })
    return out


# -------------------- grace + dedup ledger --------------------

def ledger_key(divergence: dict[str, Any]) -> str:
    return f"{divergence['direction']}|{divergence['key']}"


def load_tracked(path: Optional[Path] = None) -> dict[str, Any]:
    p = path or state_file()
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    tracked = data.get('tracked') if isinstance(data, dict) else None
    if not isinstance(tracked, dict):
        return {}
    return {k: v for k, v in tracked.items()
            if isinstance(k, str) and isinstance(v, dict)}


def save_tracked(tracked: dict[str, Any], path: Optional[Path] = None) -> None:
    p = path or state_file()
    payload = {
        '_schema': {
            'version': 1,
            'purpose': (
                'Grace + dedup ledger for heal_approvals_surface_drift.py. One '
                'entry per divergent item (direction|decision-key): how many '
                'consecutive ticks it has diverged, and whether it has already '
                'alerted. An entry is DROPPED when the item reconciles, so a '
                'later recurrence alerts again.'
            ),
        },
        'tracked': tracked,
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                       encoding='utf-8')
        os.replace(tmp, p)
    except OSError as e:
        log(f'ledger save failed: {type(e).__name__}: {e}', 'WARN')


def update_tracking(
    tracked: dict[str, Any],
    divergences: list[dict[str, Any]],
    now: datetime,
    grace: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Age the ledger by one tick and return (new_tracked, to_alert).

    A divergence alerts on the tick its consecutive-tick count REACHES `grace`,
    and never again while it persists. A key absent from this tick's divergences
    has reconciled: its entry is dropped, so the next recurrence starts a fresh
    grace window and can alert again."""
    now_iso = now.isoformat()
    new_tracked: dict[str, Any] = {}
    to_alert: list[dict[str, Any]] = []
    for divergence in divergences:
        key = ledger_key(divergence)
        prior = tracked.get(key) or {}
        ticks = prior.get('ticks')
        ticks = (ticks + 1) if isinstance(ticks, int) and ticks > 0 else 1
        entry = {
            'direction': divergence['direction'],
            'decision_key': divergence['key'],
            'origin': divergence['origin'],
            'label': divergence['label'],
            'first_seen': prior.get('first_seen') or now_iso,
            'ticks': ticks,
            'alerted_at': prior.get('alerted_at'),
        }
        if entry['alerted_at'] is None and ticks >= grace:
            entry['alerted_at'] = now_iso
            to_alert.append(divergence)
        new_tracked[key] = entry
    return new_tracked, to_alert


# -------------------- alerting --------------------

_DIRECTION_HEADLINE = {
    MISSING_CARD: 'awaiting you but NOT on the decide tab',
    ORPHAN_CARD: 'on the decide tab but nothing is awaiting you',
}


def alert_message(divergence: dict[str, Any], grace: int) -> str:
    return (
        f'Approvals surface drift: `{divergence["label"]}` '
        f'({divergence["origin"]}, key `{divergence["key"]}`) is '
        f'{_DIRECTION_HEADLINE[divergence["direction"]]} — and has been for '
        f'{grace} consecutive checks, so this is not a promote/retire in '
        f'flight. {divergence["remedy"]}'
    )


def emit_drift_alert(divergence: dict[str, Any], grace: int) -> bool:
    """ONE actionable alert naming the item, the direction and the remedy.

    `needs_larry` is set because the remedy is a code/config change only Larry can
    order. The sentinel excludes its OWN alerts from set A (is_actionable_alert),
    so this can never feed back into the next tick's divergence."""
    try:
        import larry_alerts as la  # noqa: E402
        return bool(la.append_alert(
            source=HEALER_SOURCE,
            severity='warning',
            message=alert_message(divergence, grace),
            subject=f'{HEALER_SOURCE}:{divergence["direction"]}:'
                    f'{divergence["key"]}',
            suggested_action=(
                'Read ~/agents/logs/heal-approvals-surface-drift.log, then fix '
                'the promote/retire path named above in '
                'scripts/heal_unregistered_approval.py.'
            ),
            route='escalate',
            needs_larry=True,
            decision_key=divergence['key'],
        ))
    except Exception as e:  # noqa: BLE001 — a failed alert never wedges the tick
        log(f'alert emit failed for {divergence["key"]!r}: '
            f'{type(e).__name__}: {e}', 'ERROR')
        return False


# -------------------- tick --------------------

def run_tick(
    state: dict[str, Any],
    for_larry_records: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    heuristics: dict[str, Any],
    open_task_ids: set[str],
    tracked: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    grace: Optional[int] = None,
    resolution_check: Optional[Callable[[dict[str, Any]], Optional[str]]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """One parity check. Returns (new_tracked, divergences, to_alert).

    PURE apart from log(): it reads nothing and writes nothing. main() supplies
    the store snapshots and persists the ledger, which is what keeps the
    observe-only guarantee auditable in one place."""
    now = now or datetime.now(timezone.utc)
    grace = grace if grace is not None else grace_ticks()
    check = resolution_check or (lambda _record: None)

    pending_items, muted = collect_pending(state)
    items = (
        pending_items
        + collect_for_larry(for_larry_records)
        + collect_alerts(alerts, heuristics, now, check)
    )
    awaiting = index_awaiting(items)
    alias_keys = awaiting_alias_keys(items)
    cards = collect_cards(open_task_ids, muted)
    divergences = compute_divergences(awaiting, alias_keys, cards, muted)
    new_tracked, to_alert = update_tracking(tracked, divergences, now, grace)
    return new_tracked, divergences, to_alert


def main() -> int:
    if kill_switch().exists():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    # Set B FIRST: it is the only read that can fail closed. open_approval_card_
    # task_ids returns None on any client/query failure, and an unreadable tab is
    # NOT evidence of drift — comparing against an empty set would flag every
    # awaiting item at once. Skip the tick with the ledger untouched, so a
    # divergence that really is persisting keeps its accumulated grace.
    try:
        open_task_ids = heal.open_approval_card_task_ids()
    except Exception as e:  # noqa: BLE001
        log(f'open-card fetch raised: {type(e).__name__}: {e}; skipping tick',
            'WARN')
        return 0
    if open_task_ids is None:
        log('open-card fetch unavailable (no client / query failed); '
            'skipping tick without touching the ledger')
        return 0

    heuristics = heal.load_heuristics()
    state = approval.load_state()
    alerts = heal.read_alerts()
    for_larry_records = heal.read_for_larry_records()
    tracked = load_tracked()
    grace = grace_ticks()

    def _resolution_check(record: dict[str, Any]) -> Optional[str]:
        return heal.resolution_signal(
            record, state, alerts, heuristics, after_ts=record.get('ts'),
        )

    try:
        new_tracked, divergences, to_alert = run_tick(
            state, for_larry_records, alerts, heuristics, open_task_ids,
            tracked, grace=grace, resolution_check=_resolution_check,
        )
    except Exception as e:  # noqa: BLE001
        log(f'parity check failed: {type(e).__name__}: {e}', 'ERROR')
        return 1

    save_tracked(new_tracked)
    for divergence in to_alert:
        emitted = emit_drift_alert(divergence, grace)
        log(f'DRIFT {divergence["direction"]} key={divergence["key"]!r} '
            f'origin={divergence["origin"]} label={divergence["label"]!r} '
            f'alert={"sent" if emitted else "suppressed"}')
    log(f'done: {len(divergences)} divergence(s) tracked '
        f'({len(to_alert)} newly alerted, grace={grace} ticks)')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
