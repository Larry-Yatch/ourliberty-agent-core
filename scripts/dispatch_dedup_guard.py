#!/usr/bin/env python3
"""dispatch_dedup_guard.py — refuse duplicate inbox writes.

Hard-blocks two failure classes:
  1. Filename-chain stacks: <ts1>-<ts2>-<ts3>-... Sage keeps re-prefixing dispatched
     filenames when she dedup-detects the prior one. After ≥3 prefixes, refuse.
  2. Prompt-hash duplicates: same prompt hash to same agent within last 5 min
     in dispatch_ledger.jsonl → refuse.

Usage:
  python3 dispatch_dedup_guard.py --target-agent main --task-file /path/to/new.json
  exit 0 = ok to proceed; exit 1 = refusal with reason on stderr

Sage's pre-dispatch protocol calls this before every gh issue create / inbox write.
The guard is enforced as a write-time pre-flight; the orchestrator's standing
inbox-pickup loop is unaffected.
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path('/home/larry/agents')
LEDGER = ROOT / 'logs' / 'dispatch_ledger.jsonl'
GUARD_LOG = ROOT / 'blackboard' / 'dispatch-dedup-refusals.jsonl'

CHAIN_DEPTH_LIMIT = 3
HASH_DEDUP_MIN = 5


def filename_chain_depth(name: str) -> int:
    """Count leading <YYYYMMDD...> prefixes."""
    base = name.removesuffix('.json')
    parts = base.split('-')
    depth = 0
    for p in parts:
        if re.fullmatch(r'\d{14}|\d{8}T\d{6}Z?|\d{8}', p):
            depth += 1
        else:
            break
    return depth


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]


def recent_dispatches(target_agent: str, minutes: int) -> list[dict]:
    if not LEDGER.exists():
        return []
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    out = []
    for line in LEDGER.read_text().splitlines()[-500:]:
        try:
            d = json.loads(line)
            ts = datetime.fromisoformat(d['ts'])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            out.append(d)
        except Exception:
            continue
    return out


def log_refusal(record: dict):
    GUARD_LOG.parent.mkdir(parents=True, exist_ok=True)
    with GUARD_LOG.open('a') as f:
        f.write(json.dumps(record) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target-agent', required=True)
    ap.add_argument('--task-file', required=True)
    args = ap.parse_args()

    p = Path(args.task_file)
    if not p.exists():
        print(f'task file not found: {p}', file=sys.stderr)
        sys.exit(2)

    name = p.name
    depth = filename_chain_depth(name)
    if depth >= CHAIN_DEPTH_LIMIT:
        record = {
            'ts': datetime.now(tz=timezone.utc).isoformat(),
            'reason': 'chain-depth-exceeded',
            'depth': depth,
            'limit': CHAIN_DEPTH_LIMIT,
            'task_file': str(p),
            'target_agent': args.target_agent,
        }
        log_refusal(record)
        print(f'REFUSE: filename chain depth {depth} >= {CHAIN_DEPTH_LIMIT}', file=sys.stderr)
        print('Use a clean distinct slug instead of stacking date prefixes', file=sys.stderr)
        sys.exit(1)

    try:
        task = json.loads(p.read_text())
    except Exception as e:
        print(f'task is not valid JSON: {e}', file=sys.stderr)
        sys.exit(2)

    prompt = task.get('prompt', '')
    if not prompt:
        print('task missing prompt — orchestrator will reject anyway', file=sys.stderr)
        sys.exit(1)

    h = prompt_hash(prompt)
    recent = recent_dispatches(args.target_agent, HASH_DEDUP_MIN)
    for d in recent:
        if d.get('prompt_hash') == h and args.target_agent in d.get('identity', ''):
            record = {
                'ts': datetime.now(tz=timezone.utc).isoformat(),
                'reason': 'prompt-hash-duplicate',
                'hash': h,
                'prior': d,
                'task_file': str(p),
                'target_agent': args.target_agent,
            }
            log_refusal(record)
            print(f'REFUSE: prompt hash {h} matches recent dispatch within {HASH_DEDUP_MIN} min', file=sys.stderr)
            sys.exit(1)

    print(f'OK: depth={depth} hash={h} target={args.target_agent}')
    sys.exit(0)


if __name__ == '__main__':
    main()
