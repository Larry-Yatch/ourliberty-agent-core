#!/usr/bin/env python3
"""pulse_check_xi.py — catalog-accuracy meter as a live Pulse check.

The ourliberty-graph "shelf" (the cross-product component catalog) is only as
useful as it is honest: a card whose factual fields (dependents, table seams)
no longer match live code silently misleads any agent that reuses it. The
accuracy meter (ourliberty-graph: pipeline/accuracy_meter.py) re-derives each
card's facts from live code and reports the share that no longer verifies, plus
a SCIP gauge (how much of the real import graph the cheap scanner+graphify
capture). This check wires that meter into the fleet's Pulse vigilance family so
catalog drift is *watched*, not noticed by luck — and so the meter's reading is
the watched signal that decides when SCIP is worth buying (ourliberty-graph
PLAN.md §5-#1, §10).

Unlike checks I–X (agent-invoked inside the Pulse /cycle), this one is
deterministic and LLM-free, so it runs on its own daily systemd timer
(ourliberty-pulse-check-xi.timer). It still rides the shared liveness skeleton:
run_check('xi', main) refreshes blackboard/pulse-check-xi.heartbeat on a clean
run and emits a pulse-check-failed:xi alert if the meter cannot run at all — so
heal_pulse_check_staleness.py (which unions cadence-listed ids) watches it for
free once config/pulse-check-cadence.json carries an "xi" entry.

Routing follows the vigilance rule — surface, don't act — with a toil-aware
split:
  - meter ran, drift over gate  -> digest alert (maintenance: re-characterize
    the drifted cards; slow-moving, no DM — surfaces in the daily CEO digest).
  - meter could not run at all   -> the check fails (no heartbeat); run_check's
    emit_failure escalates, because a blind meter is a real gap.

Artifact: ~/agents/blackboard/pulse-check-xi/check-xi-<ts>.json captures every
run (verified/needs-attention counts, the drifted card ids, and the SCIP gauge)
so the trend is recoverable and the staleness healer's artifact glob also sees
liveness.

Usage:  python3 scripts/pulse_check_xi.py [--dry-run] [--fixture REPORT.json]
Env:    OURLIBERTY_GRAPH_DIR (default /home/larry/ourliberty-graph)
        OURLIBERTY_AGENTS_ROOT (default /home/larry/agents) — test isolation
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
GRAPH_DIR = Path(os.environ.get('OURLIBERTY_GRAPH_DIR', '/home/larry/ourliberty-graph'))
METER = GRAPH_DIR / 'pipeline' / 'accuracy_meter.py'

ARTIFACT_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-xi'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-xi.log'

METER_TIMEOUT_S = 600

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from atomic_io import atomic_write_json  # noqa: E402


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass
    print(line)


class MeterUnavailable(Exception):
    """The meter could not be run or produced no parseable report — fail-closed:
    a blind meter must escalate, never silently pass as 'no drift'."""


def run_meter() -> dict:
    """Run the ourliberty-graph accuracy meter and return its parsed --json report.

    The meter exits 1 when over the drift gate and 0 otherwise — BOTH are
    successful runs, so the return code is not the success signal. A parseable
    JSON report on stdout is. Anything else (missing repo, traceback, empty
    stdout) is MeterUnavailable.
    """
    if not METER.exists():
        raise MeterUnavailable(f'meter not found at {METER} '
                               f'(is OURLIBERTY_GRAPH_DIR={GRAPH_DIR} checked out?)')
    try:
        proc = subprocess.run(
            [sys.executable, str(METER), '--json'],
            cwd=str(GRAPH_DIR), capture_output=True, text=True,
            timeout=METER_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise MeterUnavailable(f'{type(e).__name__}: {e}') from e
    try:
        report = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        tail = (proc.stderr or proc.stdout or '').strip()[-500:]
        raise MeterUnavailable(
            f'meter stdout was not valid JSON (rc={proc.returncode}): {e}; '
            f'stderr/stdout tail: {tail}') from e
    if not isinstance(report, dict) or 'attention_rate' not in report:
        raise MeterUnavailable('meter report missing expected fields')
    return report


def build_artifact(report: dict, *, now: Optional[datetime] = None) -> dict:
    """Distil the meter report into the dated check artifact."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    drifted = [
        {'id': c.get('id') or c.get('card'), 'verdict': c.get('verdict'),
         'detail': c.get('detail', '')}
        for c in report.get('cards', []) if c.get('verdict') != 'VERIFIED'
    ]
    return {
        'ts': ts,
        'check': 'xi',
        'cards_total': report.get('cards_total'),
        'verified': report.get('verified'),
        'needs_attention': report.get('needs_attention'),
        'attention_rate': report.get('attention_rate'),
        'gate': report.get('gate'),
        'over_gate': report.get('over_gate'),
        'tool_health': report.get('tool_health'),
        'drifted': drifted,
    }


def write_artifact(artifact: dict) -> Path:
    safe_ts = artifact['ts'].replace(':', '').replace('-', '')
    path = ARTIFACT_DIR / f'check-xi-{safe_ts}.json'
    atomic_write_json(path, artifact)
    return path


def escalate_drift(artifact: dict) -> None:
    """Surface catalog drift in the daily digest (maintenance-grade, no DM)."""
    import larry_alerts as la  # local import: keep liveness skeleton independent

    drifted_ids = [d['id'] for d in artifact['drifted']]
    la.append_alert(
        source='pulse-check',
        severity='warning',
        message=(
            f'Catalog accuracy meter: {artifact["needs_attention"]}/'
            f'{artifact["cards_total"]} shelf cards no longer verify against '
            f'live code (attention rate {artifact["attention_rate"]:.0%}, gate '
            f'{artifact["gate"]:.0%}). Drifted: {", ".join(drifted_ids) or "—"}. '
            'A drifted card can mislead reuse — its facts (dependents/seams) '
            'have moved.'
        ),
        subject='catalog-accuracy-drift',
        suggested_action=(
            'In ourliberty-graph, re-characterize the drifted cards: '
            './pipeline/regen_descriptor.sh <id> (or batch_catalog.sh), then '
            'commit; re-run pipeline/accuracy_meter.py to confirm green.'
        ),
        route='digest',
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',
                        help='Read a meter --json report from this file '
                             'instead of running the meter (hermetic tests).')
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print; do not write the artifact or '
                             'send any alert.')
    args = parser.parse_args(argv)

    if args.fixture:
        with open(args.fixture, encoding='utf-8') as fh:
            report = json.load(fh)
    else:
        try:
            report = run_meter()
        except MeterUnavailable as e:
            # Fail-closed: do NOT write a heartbeat. Returning nonzero lets
            # run_check.emit_failure escalate, and the staleness healer covers
            # the missing heartbeat as a backstop.
            log(f'meter unavailable: {e}', 'ERROR')
            return 1

    artifact = build_artifact(report)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    path = write_artifact(artifact)
    if artifact['over_gate']:
        escalate_drift(artifact)
        log(f'Check XI: DRIFT — {artifact["needs_attention"]}/'
            f'{artifact["cards_total"]} cards need attention '
            f'(rate {artifact["attention_rate"]:.0%}); digest alert queued; '
            f'artifact {path}')
    else:
        log(f'Check XI: clean — {artifact["verified"]}/{artifact["cards_total"]} '
            f'verified (rate {artifact["attention_rate"]:.0%}); artifact {path}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check as _hb_run_check
    sys.exit(_hb_run_check('xi', main, log_fn=log))
