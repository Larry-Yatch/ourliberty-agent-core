#!/usr/bin/env python3
"""drain_board_to_beacon.py — autonomously feed the idle build line from the
Parked board.

The Missions Narrator already does the judgment: for each parked capture it
authors a briefing + a `recommended_action`, stamping `delegate` on the cards it
deems ready for the team (missions_narrator.derive_recommended_action). Today a
human has to click "Delegate to team" on each one (dashboard_api._handle_capture_
delegate). This oneshot is exactly that button, fired autonomously for the top-N
ready cards — so the build pipeline stays fed without a click.

It is deliberately the *narrowest possible* autonomous supplier:

  * SELECTS only parked captures that (a) the Narrator already recommended
    `delegate`, (b) do NOT trip the careful-keyword escalator
    (missions_narrator.classify_careful — irreversible / outward-facing / spendy
    work: delete, deploy, migrate, credential, payment, rotate, ...), and (c)
    originate in `ourliberty-agent-core` (the only repo whose Beacon→Forge
    dispatches auto-fire per config/trust-policy.json). Everything else is left
    for Larry to delegate by hand.

  * Gates the careful check on classify_careful() — the RAW keyword scan — NOT
    the card's derived `risk` field. Once an agent-core auto_approve rule is live,
    trust_policy makes every agent-core card evaluate to `safe`, so the derived
    risk would silently defeat this gate; the keyword scan is independent of the
    trust action and stays honest.

  * Does NOT bypass any gate. It writes the SAME `delegate-<capture_id>` envelope
    the dashboard writes, into Beacon's inbox. Beacon scopes it and emits an
    APPROVAL_REQUEST; trust_policy then decides auto-fire vs ask. The drain only
    automates the *human click*, never the trust decision or the review.

  * Dedups three ways so a card is delegated at most once: the deterministic
    `delegate-<capture_id>` filename (collapses with a manual delegate of the
    same card — identical task_id), the capture's `spawned` ref, and this
    oneshot's own state file.

Safety / ops:

  * Ships DISABLED. With OURLIBERTY_BOARD_DRAIN_ENABLED unset/false, `--once` is
    a no-op (logs and exits 0). `--dry-run` always works and shows exactly what
    it WOULD delegate without touching anything — soak it first.
  * Bounded: at most OURLIBERTY_BOARD_DRAIN_MAX (default 2) delegations per tick.
  * captures.json is mutated only via the same atomic read-modify-replace the
    dashboard uses (_stamp_spawned_on_capture); the single git committer remains
    heal_missions_card_gc. The drain never commits.
  * main() is fail-open: any unexpected error logs to stderr and returns 0, so a
    bad tick never wedges the timer.

State file: ~/agents/state/board-drain-state.json
  {"delegated": {"<capture_id>": {"task_id": ..., "at": <iso>}}, ...}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
STATE_PATH = AGENTS_ROOT / 'state' / 'board-drain-state.json'

ENABLED_ENV = 'OURLIBERTY_BOARD_DRAIN_ENABLED'


def _env_int(name: str, default: int) -> int:
    """Parse an int env var, falling back to `default` on absence/garbage. Runs
    at import time, so it must NEVER raise — a ValueError here would exit the
    systemd run non-zero before main()'s fail-open try/except exists, wedging the
    timer on an operator typo."""
    try:
        raw = os.environ.get(name)
        return int(raw) if raw not in (None, '') else default
    except (ValueError, TypeError):
        return default


MAX_PER_TICK = _env_int('OURLIBERTY_BOARD_DRAIN_MAX', 2)

# The only repo whose Beacon→Forge dispatches auto-fire (config/trust-policy.json).
# Restricting the autonomous supply to this repo keeps the drain from spraying
# force_ask pings for repos that still require Larry's click.
AUTODISPATCH_REPO = os.environ.get(
    'OURLIBERTY_BOARD_DRAIN_REPO', 'ourliberty-agent-core')

CAPTURES_SCHEMA_VERSION = 2


def log(msg: str) -> None:
    print(f'[board-drain] {msg}', file=sys.stderr, flush=True)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, '').strip().lower() in ('1', 'true', 'yes', 'on')


# ---------------- captures.json (read / atomic stamp) ----------------
# Mirrors dashboard_api._read_captures_registry / _atomic_write_captures /
# _stamp_spawned_on_capture, minus the FastAPI HTTPException coupling. Kept
# byte-compatible (indent=2 + trailing newline) so the diff the committer picks
# up is minimal.

def _read_captures_registry(captures_path: Path) -> Optional[dict[str, Any]]:
    """Load captures.json as {schema_version, captures}. Missing → empty
    registry. Malformed → None (the caller skips the tick rather than risk
    writing onto a corrupt file)."""
    if not captures_path.exists():
        return {'schema_version': CAPTURES_SCHEMA_VERSION, 'captures': []}
    try:
        raw = captures_path.read_text()
        data = (json.loads(raw) if raw.strip()
                else {'schema_version': CAPTURES_SCHEMA_VERSION, 'captures': []})
    except (OSError, json.JSONDecodeError) as e:
        log(f'captures.json unreadable/malformed, skipping tick: {e}')
        return None
    if not isinstance(data, dict) or not isinstance(data.get('captures'), list):
        log('captures.json malformed (shape), skipping tick')
        return None
    data.setdefault('schema_version', CAPTURES_SCHEMA_VERSION)
    return data


def _atomic_write_captures(path: Path, registry: dict[str, Any]) -> None:
    """Atomic same-dir tmp + os.replace, unique tmp name (mirrors the dashboard)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(registry, indent=2) + '\n')
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _stamp_spawned_on_capture(
    captures_path: Path, capture_id: str, spawned: dict[str, Any],
) -> None:
    """Stamp the `spawned` ref so the board shows the card as delegated (and the
    dashboard grays out its Delegate button). Idempotent: a re-stamp with the
    same (task_id, kind) is a no-op. State stays `parked` — additive only."""
    registry = _read_captures_registry(captures_path)
    if registry is None:
        return
    cap = next(
        (c for c in registry.get('captures') or []
         if isinstance(c, dict) and c.get('id') == capture_id),
        None,
    )
    if cap is None:
        return
    existing = cap.get('spawned')
    if (isinstance(existing, dict)
            and existing.get('task_id') == spawned.get('task_id')
            and existing.get('kind') == spawned.get('kind')):
        return
    cap['spawned'] = spawned
    registry['schema_version'] = CAPTURES_SCHEMA_VERSION
    _atomic_write_captures(captures_path, registry)


# ---------------- drain state (own dedup) ----------------

def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {'delegated': {}}
    try:
        data = json.loads(STATE_PATH.read_text() or '{}')
    except (OSError, json.JSONDecodeError):
        return {'delegated': {}}
    if not isinstance(data, dict):
        return {'delegated': {}}
    data.setdefault('delegated', {})
    return data


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent),
                              prefix=STATE_PATH.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(state, indent=2) + '\n')
        os.replace(tmp, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------- eligibility ----------------

def _origin_repo(cap: dict[str, Any]) -> Optional[str]:
    """The capture's origin repo, in CANONICAL spelling.

    origin.repo comes from the emitting machine's checkout DIRECTORY NAME, so the
    same repo arrives as 'agent-core' from the droplet and 'ourliberty-agent-core'
    from the desktop. Before canonicalization every droplet-emitted capture failed
    the AUTODISPATCH_REPO equality check below and was skipped as `repo=agent-core`
    — silently, forever. Canonicalizing here fixes cards already on the board, not
    just new ones. It normalizes spelling only; the restriction to a single
    auto-dispatching repo is unchanged.
    """
    origin = cap.get('origin')
    if not isinstance(origin, dict):
        return None
    import routing_validator  # lazy; sibling module (matches this file's convention)
    return routing_validator.canonical_repo(origin.get('repo'))


def _is_eligible(cap: dict[str, Any], *, classify_careful, state: dict[str, Any],
                 beacon_inbox: Path) -> tuple[bool, str]:
    """Return (eligible, reason). Conservative: every condition must hold."""
    cid = cap.get('id')
    if not cid:
        return False, 'no-id'
    if cap.get('state') != 'parked':
        return False, f'state={cap.get("state")}'
    # Only act on cards the Narrator has BRIEFED and recommended delegating.
    if cap.get('recommended_action') != 'delegate':
        return False, f'recommended_action={cap.get("recommended_action")}'
    # Raw keyword escalator — independent of the (soon auto_approve) risk field.
    if classify_careful(cap):
        return False, 'careful'
    # Both sides canonicalized: AUTODISPATCH_REPO is env-overridable, so a bare
    # override ('agent-core') would otherwise reintroduce the same spelling mismatch.
    import routing_validator  # lazy; sibling module
    if _origin_repo(cap) != routing_validator.canonical_repo(AUTODISPATCH_REPO):
        return False, f'repo={_origin_repo(cap)}'
    if isinstance(cap.get('spawned'), dict):
        return False, 'already-spawned'
    if cid in (state.get('delegated') or {}):
        return False, 'in-drain-state'
    if (beacon_inbox / f'delegate-{cid}.json').exists():
        return False, 'inbox-proposal-exists'
    return True, 'ok'


def _select(registry: dict[str, Any], *, classify_careful, state: dict[str, Any],
            beacon_inbox: Path, limit: int) -> list[dict[str, Any]]:
    captures = [c for c in (registry.get('captures') or []) if isinstance(c, dict)]
    # Oldest-touched first — drain the stalest backlog before fresh arrivals.
    def _key(c: dict[str, Any]) -> str:
        return str(c.get('last_touched') or
                   (c.get('origin') or {}).get('captured_at') or '')
    eligible = []
    for cap in sorted(captures, key=_key):
        ok, _reason = _is_eligible(
            cap, classify_careful=classify_careful, state=state,
            beacon_inbox=beacon_inbox)
        if ok:
            eligible.append(cap)
        if len(eligible) >= limit:
            break
    return eligible


# ---------------- delegation (mirror the dashboard envelope) ----------------

def _build_envelope(cap: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Replicate dashboard_api._handle_capture_delegate's envelope verbatim so a
    drain delegation and a manual delegation of the same card are indistinguishable
    downstream (same task_id, same dedup_identity)."""
    cid = cap['id']
    title = cap.get('title') or cid
    briefing = cap.get('briefing') if isinstance(cap.get('briefing'), dict) else {}
    suggest = briefing.get('suggest')
    summary = suggest or title or cid
    task_id = f'delegate-{cid}'
    prompt = (
        f'Auto-delegated from the Parked board (board-drain) — parked card '
        f'`{cid}` ("{title}"), which the Narrator recommended delegating. '
        'Treat this exactly as a "Delegate to team" click: scope and '
        'propose/run this down as the team. '
        f'Briefing — what: {briefing.get("what") or "(none)"}; '
        f'why: {briefing.get("why") or "(none)"}; '
        f'suggested next step: {suggest or "(none)"}. '
        'This is a human-approval-gate proposal: whether the resulting dispatch '
        'auto-fires or asks is governed by trust_policy. Do NOT mutate the '
        'capture — it stays parked; the delegation lives as this proposal.'
    )
    envelope = {
        'task_id': task_id,
        'target_agent': 'beacon',
        'summary': summary,
        'prompt': prompt,
        'source': 'dashboard',
        'actor': 'board-drain',
        'capture_id': cid,
        'action': 'delegate',
        'dedup_identity': f'delegate:{cid}',
        'timeout': 600,
    }
    return task_id, envelope


def _delegate(cap: dict[str, Any], *, captures_path: Path, dry_run: bool,
              state: dict[str, Any]) -> str:
    """Write the delegate proposal to Beacon's inbox + stamp/record dedup.
    Returns a one-line summary for the audit log."""
    import safe_write_inbox  # lazy; sibling module (jail-guarded)

    cid = cap['id']
    task_id, envelope = _build_envelope(cap)
    if dry_run:
        return f'WOULD delegate {cid} (task {task_id}) — {envelope["summary"][:60]}'

    safe_write_inbox.safe_write_inbox(
        target_agent='beacon',
        task_dict=envelope,
        source_agent='dashboard',
        filename=f'{task_id}.json',
    )
    _stamp_spawned_on_capture(captures_path, cid, {
        'kind': 'delegate',
        'task_id': task_id,
        'stamped_at': _now_utc_iso(),
    })
    state.setdefault('delegated', {})[cid] = {
        'task_id': task_id, 'at': _now_utc_iso()}
    return f'delegated {cid} (task {task_id}) — {envelope["summary"][:60]}'


# ---------------- main ----------------

def run(*, captures_path: Path, beacon_inbox: Path, classify_careful,
        dry_run: bool, limit: int) -> dict[str, Any]:
    """Pure-ish core (deps injected for tests). Returns a result dict."""
    registry = _read_captures_registry(captures_path)
    if registry is None:
        return {'status': 'skipped', 'reason': 'captures-unreadable', 'delegated': []}
    state = _load_state()
    selected = _select(registry, classify_careful=classify_careful, state=state,
                       beacon_inbox=beacon_inbox, limit=limit)
    lines = []
    for cap in selected:
        try:
            lines.append(_delegate(cap, captures_path=captures_path,
                                   dry_run=dry_run, state=state))
        except Exception as e:  # one bad card never stops the rest
            lines.append(f'ERROR delegating {cap.get("id")}: {e}')
    if not dry_run and selected:
        _save_state(state)
    return {
        'status': 'dry-run' if dry_run else 'ok',
        'eligible_selected': len(selected),
        'delegated': lines,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='Drain the Parked board into Beacon.')
    ap.add_argument('--dry-run', action='store_true',
                    help='show what would be delegated; touch nothing')
    ap.add_argument('--once', action='store_true',
                    help='run one tick (default action)')
    ap.add_argument('--limit', type=int, default=MAX_PER_TICK,
                    help=f'max delegations this tick (default {MAX_PER_TICK})')
    args = ap.parse_args(argv)

    try:
        # Dry-run is always allowed (soak/inspection). A live tick requires the
        # enable flag — ships disabled.
        if not args.dry_run and not _enabled():
            log(f'disabled ({ENABLED_ENV} not set) — no-op. '
                'Use --dry-run to preview.')
            return 0

        import heal_missions_card_gc as gc  # repo-path + captures-path helpers
        from missions_narrator import classify_careful

        repo_paths = gc.load_repo_paths()
        cpath = gc.captures_path(repo_paths)
        if cpath is None:
            log('ourliberty-agent-core not in repo_paths — cannot locate '
                'captures.json; no-op.')
            return 0
        beacon_inbox = AGENTS_ROOT / 'inboxes' / 'beacon'

        result = run(captures_path=cpath, beacon_inbox=beacon_inbox,
                     classify_careful=classify_careful,
                     dry_run=args.dry_run, limit=max(0, args.limit))
        log(f'{result["status"]}: selected={result.get("eligible_selected", 0)}')
        for line in result.get('delegated', []):
            log('  ' + line)
        return 0
    except Exception as e:  # fail-open — never wedge the timer
        log(f'unexpected error (fail-open): {e}')
        return 0


if __name__ == '__main__':
    sys.exit(main())
