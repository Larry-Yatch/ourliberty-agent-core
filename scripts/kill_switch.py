#!/usr/bin/env python3
"""
Kill Switch — Emergency halt/resume for all agents.

Usage:
  python3 kill_switch.py halt    # Touch EMERGENCY_HALT, pause all new dispatch
  python3 kill_switch.py resume  # Remove EMERGENCY_HALT, resume operations
  python3 kill_switch.py status  # Check current state

The orchestrator checks for EMERGENCY_HALT at every poll cycle.
When halted:
  - No new tasks are dispatched from inboxes
  - Running tasks continue to completion (graceful)
  - Cron jobs check and skip if halted
  - Telegram bots still receive messages but queue them

Telegram commands (any agent bot):
  "STOP ALL AGENTS"   -> touch EMERGENCY_HALT
  "RESUME ALL AGENTS" -> rm EMERGENCY_HALT
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import os

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_isolation_guard import refuse_under_test

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or Path.home() / 'agents')
BLACKBOARD = AGENTS_ROOT / 'blackboard'
HALT_FILE = BLACKBOARD / 'EMERGENCY_HALT'
LOG_DIR = AGENTS_ROOT / 'logs'


def log(message, level='INFO'):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = f'[{ts}] [kill-switch] [{level}] {message}'
    print(entry)
    log_file = LOG_DIR / 'kill-switch.log'
    try:
        with open(log_file, 'a') as f:
            f.write(entry + '\n')
    except Exception:
        pass


def halt():
    """Activate emergency halt."""
    refuse_under_test('kill-switch')
    BLACKBOARD.mkdir(parents=True, exist_ok=True)
    halt_data = {
        'activated_at': datetime.now(timezone.utc).isoformat(),
        'activated_by': 'manual' if len(sys.argv) <= 2 else sys.argv[2],
        'reason': sys.argv[3] if len(sys.argv) > 3 else 'Emergency halt activated',
    }
    with open(HALT_FILE, 'w') as f:
        json.dump(halt_data, f, indent=2)
    log('EMERGENCY HALT ACTIVATED', 'CRITICAL')
    print('EMERGENCY HALT ACTIVATED — all new agent dispatch is paused.')
    print(f'Halt file: {HALT_FILE}')
    print('Running tasks will complete gracefully.')
    print('To resume: python3 kill_switch.py resume')


def resume():
    """Remove emergency halt."""
    if HALT_FILE.exists():
        # Read halt data for logging
        try:
            with open(HALT_FILE) as f:
                halt_data = json.load(f)
            activated_at = halt_data.get('activated_at', 'unknown')
        except Exception:
            activated_at = 'unknown'

        HALT_FILE.unlink()
        log(f'EMERGENCY HALT REMOVED (was active since {activated_at})', 'CRITICAL')
        print('EMERGENCY HALT REMOVED — agent dispatch resumed.')
    else:
        print('No emergency halt was active.')
        log('Resume requested but no halt was active')


def status():
    """Check current halt status."""
    if HALT_FILE.exists():
        try:
            with open(HALT_FILE) as f:
                halt_data = json.load(f)
            print(f'HALTED since {halt_data.get("activated_at", "unknown")}')
            print(f'Reason: {halt_data.get("reason", "none")}')
            print(f'Activated by: {halt_data.get("activated_by", "unknown")}')
        except Exception:
            print('HALTED (could not read halt data)')
    else:
        print('RUNNING — no emergency halt active')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: kill_switch.py {halt|resume|status}')
        sys.exit(1)

    action = sys.argv[1].lower()
    if action == 'halt':
        halt()
    elif action == 'resume':
        resume()
    elif action == 'status':
        status()
    else:
        print(f'Unknown action: {action}')
        print('Usage: kill_switch.py {halt|resume|status}')
        sys.exit(1)
