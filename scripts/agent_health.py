#!/usr/bin/env python3
"""agent_health.py — Improvement #3: per-agent success-rate observability.

Parses each agent's log for "completed (success=True/False)" lines in
the trailing window and computes success rate. Outputs JSON for cycle
to consume + a 1-line human summary.

Usage:
  python3 agent_health.py [--window-min N]
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
import argparse
import json
import os
import re
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get('AGENTS_LOGS', '/home/larry/agents/logs'))
AGENTS = ('beacon', 'forge', 'mirror', 'pulse')

# matches "[2026-04-30 12:34:56] [main] [INFO] Task X completed (success=True)"
COMPLETED_RE = re.compile(
    r'\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]\s+\[\w+\]\s+\[INFO\]\s+Task\s+\S+\s+completed\s+\(success=(True|False)\)'
)


def agent_health(agent_id, window_min=60):
    """Compute success rate for one agent in last <window_min> minutes."""
    log_path = LOG_DIR / f'{agent_id}.log'
    if not log_path.exists():
        return {'agent': agent_id, 'available': False, 'reason': 'log missing'}
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, 2)
            file_size = f.tell()
            tail_size = min(file_size, 5 * 1024 * 1024)  # 5MB max
            f.seek(file_size - tail_size)
            content = f.read().decode('utf-8', errors='ignore')
    except OSError as e:
        return {'agent': agent_id, 'available': False, 'reason': str(e)}

    cutoff = time.time() - window_min * 60
    successes = 0
    failures = 0
    for m in COMPLETED_RE.finditer(content):
        date_str = m.group(1)
        time_str = m.group(2)
        try:
            ts = time.mktime(time.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M:%S'))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if m.group(3) == 'True':
            successes += 1
        else:
            failures += 1

    total = successes + failures
    rate = (successes / total) if total > 0 else None
    health = 'idle' if total == 0 else (
        'healthy' if rate >= 0.9 else
        'degraded' if rate >= 0.5 else
        'critical'
    )
    return {
        'agent': agent_id,
        'available': True,
        'window_min': window_min,
        'tasks': total,
        'successes': successes,
        'failures': failures,
        'success_rate': rate,
        'health': health,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-min', type=int, default=60)
    parser.add_argument('--json', action='store_true', help='output JSON')
    args = parser.parse_args()

    results = [agent_health(a, args.window_min) for a in AGENTS]
    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    # Human-readable summary
    parts = []
    for r in results:
        if not r.get('available'):
            continue
        if r['tasks'] == 0:
            parts.append(f'{r["agent"]}=idle')
        else:
            rate_pct = int(r['success_rate'] * 100)
            tag = '✓' if r['health'] == 'healthy' else ('⚠' if r['health'] == 'degraded' else '✗')
            parts.append(f'{r["agent"]}={r["successes"]}/{r["tasks"]} ({rate_pct}%) {tag}')
    print(f'agent-health [{args.window_min}m]: ' + ' | '.join(parts))
    return 0


if __name__ == '__main__':
    main()
