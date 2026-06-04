#!/usr/bin/env python3
"""seed_pulse_check_heartbeats.py — one-time liveness seed for Pulse checks I-X.

When the heartbeat wrapper + staleness watcher first ship, the watcher has no
signal to evaluate until each check fires on its own — which for the monthly
checks (V, VI) is weeks away. This bootstrap gives the watcher an honest
starting signal immediately, without ever taking a side effect a check would
normally take on its real firing day.

Two outcomes per check, and only two:

  * RUN (provably side-effect-free).  Every check except I exposes a --dry-run
    mode that computes its analysis but returns before it POSTs a mission, sends
    a DM, or writes config (verified per check). We invoke that mode through
    pulse_check_heartbeat.run_check, so a clean exit emits a *genuine* heartbeat
    and a non-zero exit emits a pulse-check-failed:<id> alert. The non-zero path
    is the point for IX/X: a check still broken from the 06-01 stall surfaces
    its status NOW instead of after a staleness window.

  * BASELINE-SEED (cannot prove safe).  If a check has no --dry-run we do NOT
    fake a heartbeat — faking success is the one thing a liveness layer must
    never do. Instead we record monitoring_since=now in the watcher's baseline
    (blackboard/pulse-check-staleness-baseline.json), so the check gets a quiet
    first cadence+grace warm-up, after which fail-closed escalation resumes.

Safety is structural, not a hardcoded allowlist: we only ever invoke a check
with ``['--dry-run']``. A check that does not recognise that flag makes argparse
exit before it does any work, which we catch and treat as "unprovable" →
baseline-seed. Event-driven checks (VII) are skipped: the watcher does not track
their staleness, so a seed signal would be meaningless.

HARD CONSTRAINT: never POST a mission, send a DM, or edit config. The only
writes are heartbeat files (via run_check on a proven dry-run) and the
monitoring_since baseline. Stdlib only.
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import heal_pulse_check_staleness as watcher  # noqa: E402
from pulse_check_heartbeat import run_check as _real_run_check  # noqa: E402


def seed_one(
    check_id: str,
    entry: Optional[dict[str, Any]],
    bb: Path,
    now: float,
    baseline: dict[str, float],
    *,
    execute: bool,
    run_check_fn: Callable[..., int],
    import_fn: Callable[[str], Any],
    log_fn: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    """Seed a single check. Mutates ``baseline`` in place when it baseline-seeds.

    Returns a result row: {check, action, detail, rc, baseline_changed}.
    ``action`` is one of skipped | would-run | ran | baseline-seeded.
    """
    if isinstance(entry, dict) and entry.get('event_driven'):
        return {
            'check': check_id, 'action': 'skipped', 'rc': None,
            'baseline_changed': False,
            'detail': 'event-driven; watcher does not track its staleness',
        }

    if not execute:
        return {
            'check': check_id, 'action': 'would-run', 'rc': None,
            'baseline_changed': False,
            'detail': 'would invoke --dry-run (or baseline-seed if unsupported)',
        }

    # Attempt the provably-safe run. Anything that prevents a clean --dry-run
    # invocation (no such flag, import failure, missing main) is "unprovable" →
    # we fall through to baseline-seed rather than risk a real-mode run.
    try:
        module = import_fn(f'pulse_check_{check_id}')
        main_fn = module.main
        check_log = getattr(module, 'log', None)
    except (ImportError, AttributeError) as exc:
        detail = f'cannot import a runnable main ({type(exc).__name__}); '
        return _baseline_seed(check_id, bb, now, baseline, detail)

    try:
        rc = run_check_fn(
            check_id, main_fn, argv=['--dry-run'], log_fn=check_log,
        )
    except SystemExit:
        # argparse rejected --dry-run before doing any work (Check I): safe, but
        # not runnable side-effect-free, so seed the baseline instead.
        return _baseline_seed(
            check_id, bb, now, baseline,
            'no --dry-run mode; ',
        )
    except Exception as exc:  # noqa: BLE001 — never let one check abort the seed
        if log_fn is not None:
            log_fn(f'seed {check_id}: unexpected {type(exc).__name__}: {exc}',
                   'ERROR')
        return _baseline_seed(
            check_id, bb, now, baseline,
            f'run raised {type(exc).__name__}; ',
        )

    if rc == 0:
        detail = '--dry-run clean; genuine heartbeat refreshed'
    else:
        detail = (f'--dry-run exited {rc}; pulse-check-failed:{check_id} '
                  'surfaced (status visible now, not after a stale window)')
    return {
        'check': check_id, 'action': 'ran', 'rc': rc,
        'baseline_changed': False, 'detail': detail,
    }


def _baseline_seed(
    check_id: str, bb: Path, now: float, baseline: dict[str, float],
    reason: str,
) -> dict[str, Any]:
    changed = False
    if check_id not in baseline:
        baseline[check_id] = now
        changed = True
        detail = (f'{reason}baseline-seeded monitoring_since '
                  '(quiet warm-up, no fake heartbeat)')
    else:
        detail = (f'{reason}already baselined; left monitoring_since as-is')
    return {
        'check': check_id, 'action': 'baseline-seeded', 'rc': None,
        'baseline_changed': changed, 'detail': detail,
    }


def seed_all(
    cadence: dict[str, Any],
    bb: Path,
    now: Optional[float] = None,
    *,
    execute: bool = True,
    run_check_fn: Callable[..., int] = _real_run_check,
    import_fn: Callable[[str], Any] = importlib.import_module,
    log_fn: Optional[Callable[..., None]] = None,
) -> list[dict[str, Any]]:
    """Seed every canonical check; return per-check result rows.

    ``execute=False`` is a no-write plan: it touches no module, heartbeat, or
    baseline file. ``run_check_fn`` / ``import_fn`` are injectable so tests can
    prove the seed only ever invokes a check with ``['--dry-run']``.
    """
    now = time.time() if now is None else now
    baseline = watcher.load_baseline(bb) if execute else {}
    rows: list[dict[str, Any]] = []
    changed = False
    for check_id in watcher.CANONICAL_CHECKS:
        row = seed_one(
            check_id, cadence.get(check_id), bb, now, baseline,
            execute=execute, run_check_fn=run_check_fn,
            import_fn=import_fn, log_fn=log_fn,
        )
        changed = changed or row['baseline_changed']
        rows.append(row)
    if execute and changed:
        watcher.save_baseline(bb, baseline)
    return rows


def _format(rows: list[dict[str, Any]]) -> str:
    width = max((len(r['check']) for r in rows), default=1)
    lines = []
    for r in rows:
        lines.append(f"  {r['check']:<{width}}  {r['action']:<15} {r['detail']}")
    return '\n'.join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--plan', action='store_true',
        help='Print the per-check plan without running anything or writing any '
             'file (no heartbeat, no baseline, no alert).',
    )
    args = parser.parse_args(argv)

    try:
        cadence = watcher.load_cadence_config()
    except (OSError, ValueError) as exc:
        print(f'cannot read cadence config: {type(exc).__name__}: {exc}',
              file=sys.stderr)
        return 1

    rows = seed_all(cadence, watcher.blackboard(), execute=not args.plan)
    header = 'PLAN (no writes):' if args.plan else 'Seed results:'
    print(header)
    print(_format(rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
