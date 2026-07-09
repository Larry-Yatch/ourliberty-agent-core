#!/usr/bin/env python3
"""system_resource_watch.py — reliable box-resource watcher.

Runs every ~5 min via systemd timer (ourliberty-system-resource-watch.timer).
Evaluates the honest resource verdict from `system_resource_health.collect()`
and, on REAL trouble, pings Larry so he never has to guess whether the System
Health gauge's orange bar means something:

  - CRITICAL  → DM now (larry_alerts route='escalate') AND stamp `needs_larry`
                so it lands at the top of the Approvals / Needs-You surface,
                with the culprit process NAMED and a runnable suggested action.
  - WARNING   → NO DM (anti-toil: warnings show on the gauge, they don't page).
  - RECOVERY  → a one-line closure DM when it drops back to green after a
                critical, closing the loop and clearing the needs-you row.

Anti-flap: state is persisted between runs. A DM fires only on a TRANSITION
into a worse level than the one already alerted — a sustained critical does not
re-page every 5 minutes (larry_alerts cooldown is a second backstop). PSI's own
60s averaging smooths sub-minute spikes before they ever reach here.

This watcher does NOT auto-fix — resource trouble is ambiguous (which process,
kill vs restart vs scale) and belongs to Larry's judgment. It surfaces + names +
suggests; it never kills.

Usage:
  system_resource_watch.py            # one evaluation, emit on transition
  system_resource_watch.py --dry-run  # evaluate + print, never emit / write state
  system_resource_watch.py --json     # machine-readable snapshot to stdout
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import system_resource_health as srh  # noqa: E402

SOURCE = 'system-resource-watch'
# Stable id for the Approvals / "Needs You" card so upsert (on critical) and
# resolve (on recovery) target the same row.
NEEDS_YOU_KEY = f'{SOURCE}:resource-health'
REPO_DIR = Path('/home/larry/agent-core')
AGENTS_ROOT = Path('/home/larry/agents')
STATE_FILE = AGENTS_ROOT / 'blackboard' / 'system-resource-watch-state.json'
CONFIG_PATH = REPO_DIR / 'config' / 'system_health_thresholds.json'
DROPLET = 'larry@134.209.44.80'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError:
        pass


def _suggested_action(snapshot: dict[str, Any]) -> str:
    """A runnable command that surfaces the culprit + the honest signals, so
    Larry (or Claude) can act without re-deriving state."""
    top = snapshot['signals'].get('top_process') or {}
    pid = top.get('pid')
    base = (
        f"ssh {DROPLET} 'ps -eo pid,pmem,rss,etime,comm --sort=-rss | head -8; "
        "echo; cat /proc/pressure/memory; echo; free -h'"
    )
    if pid:
        return (
            f"{base}  # top consumer: {top.get('name','?')} (pid {pid}). "
            f"Restart its service or kill {pid} if it's a runaway."
        )
    return base


def _dm_body(snapshot: dict[str, Any]) -> str:
    v = snapshot['verdict']
    lines = [v['headline']]
    for r in v['reasons']:
        lines.append(f'• {r}')
    if v['suggested_actions']:
        lines.append('')
        lines.append('Suggested:')
        for a in v['suggested_actions']:
            lines.append(f'→ {a}')
    return '\n'.join(lines)


def _emit(
    severity: str,
    body: str,
    subject: str,
    suggested_action: str,
    route: str,
    needs_larry: bool,
) -> bool:
    """Fire-and-forget to larry_alerts. Import lazily so tests can run the
    verdict logic without the alert stack / test-jail refusal."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
    except Exception:
        return False
    return la.append_alert(
        source=SOURCE,
        severity=severity,
        message=body,
        subject=subject,
        suggested_action=suggested_action,
        route=route,
        decision_key=NEEDS_YOU_KEY,
        needs_larry=needs_larry,
    )


def _upsert_needs_you(snapshot: dict[str, Any]) -> bool:
    """Create/refresh the Approvals / "Needs You" card for an active critical.

    The DM and the Approvals surface are SEPARATE stores: append_alert(...
    needs_larry=True) only stamps the alerts feed, which the Needs-You read
    model does NOT read. The surface is backed by for_larry_signal, so a
    producer that wants a row must upsert it here (mirrors main_suite_guardian).
    Lazy import + swallow so the test jail / a missing module never crashes the
    watcher."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import for_larry_signal as fls  # noqa: E402
    except Exception:
        return False
    v = snapshot['verdict']
    record = {
        'source': SOURCE,
        'severity': 'critical',
        'needs_larry': True,
        'kind': 'resource-health',
        'title': 'System resources need attention',
        'summary': _dm_body(snapshot),
        'suggested_action': _suggested_action(snapshot),
        'headline': v['headline'],
    }
    try:
        fls.upsert_record(NEEDS_YOU_KEY, record)
        return True
    except Exception:
        return False


def _resolve_needs_you() -> bool:
    """Clear the Approvals / "Needs You" card on recovery (idempotent no-op if
    no card exists)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import for_larry_signal as fls  # noqa: E402
    except Exception:
        return False
    try:
        return fls.resolve_record(NEEDS_YOU_KEY)
    except Exception:
        return False


def evaluate(
    snapshot: dict[str, Any],
    prior: dict[str, Any],
) -> dict[str, Any]:
    """Pure transition decision. Returns the action to take + next state, given
    the current snapshot and prior persisted state. No I/O — unit-testable.

    Decision table (alerted = the worst level we've already paged on):
      green   → critical : escalate DM + needs_larry
      warning → critical : escalate DM + needs_larry
      critical→ green    : closure DM (recovery), clear needs_larry
      * → warning         : gauge-only, no DM
      no worsening        : no-op
    """
    level = snapshot['verdict']['level']
    alerted = prior.get('alerted_level', 'green')

    action = 'none'
    if level == 'critical' and alerted != 'critical':
        action = 'escalate'
    elif level == 'green' and alerted == 'critical':
        action = 'recover'

    # `alerted_level` tracks the worst level currently being paged on. It goes
    # to 'critical' when we escalate, and resets to 'green' only on recovery —
    # a drop critical→warning is still "being handled", so it does not re-page
    # nor clear until fully green.
    if action == 'escalate':
        next_alerted = 'critical'
    elif action == 'recover':
        next_alerted = 'green'
    else:
        next_alerted = alerted

    next_state = {
        'level': level,
        'alerted_level': next_alerted,
        'headline': snapshot['verdict']['headline'],
        'updated_at': _now_iso(),
    }
    if prior.get('alerted_level') != next_alerted or 'since' not in prior:
        next_state['since'] = _now_iso()
    else:
        next_state['since'] = prior['since']
    return {'action': action, 'next_state': next_state}


def run(
    proc_root: Path = Path('/proc'),
    config_path: Path = CONFIG_PATH,
    state_path: Path = STATE_FILE,
    dry_run: bool = False,
) -> dict[str, Any]:
    snapshot = srh.collect(proc_root=proc_root, config_path=config_path)
    prior = _load_state(state_path)
    decision = evaluate(snapshot, prior)
    action = decision['action']

    if not dry_run:
        if action == 'escalate':
            _emit(
                severity='critical',
                body=_dm_body(snapshot),
                subject='resource-critical',
                suggested_action=_suggested_action(snapshot),
                route='escalate',
                needs_larry=True,
            )
            # DM is only half the contract — also create the Approvals row.
            _upsert_needs_you(snapshot)
        elif action == 'recover':
            # Distinct subject so a fast re-flap's closure isn't swallowed by
            # the 6 h info-cooldown bucket shared with the critical's key.
            _emit(
                severity='info',
                body='System resources recovered — back to healthy.',
                subject='resource-recovered',
                suggested_action='',
                route='closure',
                needs_larry=False,
            )
            # Clear the Approvals row raised on the critical.
            _resolve_needs_you()
        _write_state(decision['next_state'], state_path)

    return {'snapshot': snapshot, 'action': action, 'state': decision['next_state']}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='Box-resource health watcher.')
    ap.add_argument('--dry-run', action='store_true',
                    help='evaluate + print, never emit or persist')
    ap.add_argument('--json', action='store_true',
                    help='machine-readable snapshot to stdout')
    args = ap.parse_args(argv)

    result = run(dry_run=args.dry_run)
    verdict = result['snapshot']['verdict']

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"[{verdict['level']}] {verdict['headline']} "
              f"(action={result['action']})")
        for r in verdict['reasons']:
            print(f'  • {r}')

    # Exit 0 healthy/warning, 1 when currently critical — lets `systemctl
    # status` / journald flag a bad run without the emit path.
    return 1 if verdict['level'] == 'critical' else 0


if __name__ == '__main__':
    raise SystemExit(main())
