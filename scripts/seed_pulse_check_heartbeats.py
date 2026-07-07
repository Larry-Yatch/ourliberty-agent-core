#!/usr/bin/env python3
"""seed_pulse_check_heartbeats.py — one-time liveness seed for Pulse checks I-X.

When the heartbeat wrapper + staleness watcher first ship, the watcher has no
signal to evaluate until each check fires on its own — which for the monthly
checks (V, VI) is weeks away. This bootstrap gives the watcher an honest
starting signal immediately, without ever taking a side effect a check would
normally take on its real firing day.

Before probing, we load any MISSING vars from /home/larry/credentials/.env.larry
(the same creds production loads via systemd) so the DB-backed checks (iii/iv/x)
can authenticate instead of tripping their missing-env guard. See load_env_file.

Three outcomes per check:

  * RUN (provably side-effect-free).  Every check except I exposes a --dry-run
    mode that computes its analysis but returns before it POSTs a mission, sends
    a DM, or writes config (verified per check). We invoke that mode through
    pulse_check_heartbeat.run_check, so a clean exit emits a *genuine* heartbeat
    and a non-zero exit emits a pulse-check-failed:<id> alert. The non-zero path
    is the point for IX/X: a check still broken from the 06-01 stall surfaces
    its status NOW instead of after a staleness window.

  * UNVALIDATED (env absent).  If a check fails ONLY because a required env var
    is still unset after the .env.larry load (its missing-env guard raises), we
    classify it as "unvalidated: env absent" — a neutral outcome that emits NO
    pulse-check-failed and writes NO heartbeat. We could not authenticate the
    check, so we make no claim about its health either way. A genuine non-env
    failure still surfaces as a failure via the RUN path above.

  * BASELINE-SEED (cannot prove safe).  If a check has no --dry-run we do NOT
    fake a heartbeat — faking success is the one thing a liveness layer must
    never do. Instead we record monitoring_since=now in the watcher's baseline
    (blackboard/pulse-check-staleness-baseline.json), so the check gets a quiet
    first cadence+grace warm-up, after which fail-closed escalation resumes.

Safety is structural, not a hardcoded allowlist: we only ever invoke a check
with ``['--dry-run']``. A check that does not recognise that flag makes argparse
exit before it does any work, which we catch and treat as "unprovable" →
baseline-seed. Event-driven checks are skipped: the watcher does not track
their staleness, so a seed signal would be meaningless. (No canonical check is
event-driven since VII's retirement on 2026-07-07; the skip branch stays
because it is config-driven.)

HARD CONSTRAINT: never POST a mission, send a DM, or edit config. The only
writes are heartbeat files (via run_check on a proven dry-run) and the
monitoring_since baseline. Stdlib only.
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import heal_pulse_check_staleness as watcher  # noqa: E402
from pulse_check_heartbeat import run_check as _real_run_check  # noqa: E402


# Production loads these creds from this file via systemd's EnvironmentFile; a
# bare-shell seed run must do the same or the DB-backed checks (iii/iv/x) hit
# their missing-env guard and the seed cries wolf. See load_env_file.
ENV_FILE = Path('/home/larry/credentials/.env.larry')

# A check's env guard raises e.g. RuntimeError('SUPABASE_URL /
# SUPABASE_SERVICE_ROLE_KEY missing — cannot run Check III'). The dash style
# varies across checks, so we key on the stable "<VARS> missing" shape, not the
# whole message.
_ENV_MISSING_RE = re.compile(
    r'\b([A-Z][A-Z0-9_]+(?:\s*/\s*[A-Z][A-Z0-9_]+)*)\s+missing\b'
)


class _EnvAbsent(SystemExit):
    """Sentinel: a check failed only because a required env var was unset.

    Subclassing SystemExit is deliberate. run_check re-raises SystemExit
    untouched (it never emits a pulse-check-failed alert for it), so wrapping a
    check's env-guard RuntimeError as _EnvAbsent lets the neutral "env absent"
    outcome propagate through run_check without firing the alert — while leaving
    run_check, the checks, and the --dry-run-only contract unchanged.
    """


def load_env_file(path: Path = ENV_FILE) -> int:
    """Load MISSING vars from a KEY=value env-file into os.environ.

    Mirrors the stdlib parser in deploy_notifier.load_vercel_token (no
    python-dotenv). Never overrides an already-set var, so an explicit
    environment always wins. Returns the count of vars loaded.
    """
    if not path.exists():
        return 0
    loaded = 0
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value.strip().strip("'").strip('"').strip()
            loaded += 1
    except OSError:
        pass
    return loaded


def _env_absent_reason(exc: BaseException) -> Optional[str]:
    """Return a reason string iff ``exc`` is a missing-required-env failure.

    A failure counts as env-absent only when the message names env vars in the
    "<VARS> missing" shape AND those vars are genuinely unset in os.environ.
    Requiring real absence keeps a non-env RuntimeError that merely mentions
    "missing" from being misclassified as neutral.
    """
    if not isinstance(exc, RuntimeError):
        return None
    match = _ENV_MISSING_RE.search(str(exc))
    if not match:
        return None
    names = [n.strip() for n in match.group(1).split('/') if n.strip()]
    absent = [n for n in names if not os.environ.get(n)]
    if not absent:
        return None
    return 'env absent: ' + ', '.join(absent)


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
    ``action`` is one of
    skipped | would-run | ran | baseline-seeded | unvalidated.
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

    # Wrap main so a missing-required-env failure becomes _EnvAbsent. run_check
    # re-raises that (a SystemExit subclass) without emitting an alert, so the
    # neutral "env absent" outcome never trips a false pulse-check-failed.
    def env_aware_main(argv: Optional[list] = None) -> int:
        try:
            return main_fn(argv)
        except RuntimeError as exc:
            reason = _env_absent_reason(exc)
            if reason is not None:
                raise _EnvAbsent(reason) from exc
            raise

    try:
        rc = run_check_fn(
            check_id, env_aware_main, argv=['--dry-run'], log_fn=check_log,
        )
    except _EnvAbsent as exc:
        # Check ran but only failed for lack of a required env var (prod loads
        # it; this shell did not). Neutral: no heartbeat, no alert, no baseline.
        return {
            'check': check_id, 'action': 'unvalidated', 'rc': None,
            'baseline_changed': False,
            'detail': (f'unvalidated: {exc} — check could not authenticate, so '
                       'no heartbeat and no pulse-check-failed emitted'),
        }
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

    # Match the production auth path before probing: load any missing creds from
    # .env.larry so the DB-backed checks (iii/iv/x) can actually validate. A
    # --plan preview stays read-only, but loading env touches no seed artifact.
    loaded = load_env_file()
    if loaded:
        print(f'loaded {loaded} env var(s) from {ENV_FILE}')

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
