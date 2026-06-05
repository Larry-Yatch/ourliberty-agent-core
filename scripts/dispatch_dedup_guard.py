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


def record_dispatch(target_agent: str, prompt_hash_val: str, task_file: str) -> bool:
    """Append an approved dispatch to the ledger so a later invocation can see a
    prompt-hash duplicate within HASH_DEDUP_MIN. Return True if the row was
    written, False if the write failed.

    Without this the hash-dedup is DEAD: nothing populates `dispatch_ledger.jsonl`
    with `prompt_hash`/`identity` rows, so `recent_dispatches` always comes back
    empty and the guard only ever enforces the filename-chain check. Recording
    here is what arms the content-duplicate refusal.

    A write failure used to be swallowed (fail-open), silently disarming the
    content-duplicate check the guard exists to provide (audit #56). We now
    return False so main() can fail CLOSED — refusing the dispatch rather than
    proceeding with a blind dedup window."""
    record = {
        'ts': datetime.now(tz=timezone.utc).isoformat(),
        'identity': target_agent,
        'target_agent': target_agent,
        'prompt_hash': prompt_hash_val,
        'task_file': task_file,
    }
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open('a') as f:
            f.write(json.dumps(record) + '\n')
        return True
    except OSError:
        return False


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
        # The hash check targets the SAME prompt re-dispatched to the SAME agent
        # under a DIFFERENT filename (re-slugging); a literal re-run of the same
        # task_file is a retry, not that abuse, so don't self-block it. Match the
        # agent on the exact target_agent field or the boundary of the legacy
        # `agent:...` identity (substring `in` would false-match 'main' against
        # 'maintenance').
        if d.get('task_file') == str(p):
            continue
        agent_matches = (
            d.get('target_agent') == args.target_agent
            or d.get('identity', '').split(':', 1)[0] == args.target_agent
        )
        if d.get('prompt_hash') == h and agent_matches:
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

    if not record_dispatch(args.target_agent, h, str(p)):
        # Fail closed (audit #56): if we couldn't arm the ledger, refuse rather
        # than dispatch into a blind dedup window where a re-slugged prompt storm
        # would go unblocked. A persistently-unwritable ledger surfaces here as a
        # dispatch stall (visible/alertable) instead of a silent disarm.
        record = {
            'ts': datetime.now(tz=timezone.utc).isoformat(),
            'reason': 'ledger-write-failed',
            'hash': h,
            'task_file': str(p),
            'target_agent': args.target_agent,
        }
        log_refusal(record)
        print(f'REFUSE: could not record dispatch to ledger {LEDGER} '
              f'(disk full / perms?); refusing to dispatch with dedup disarmed',
              file=sys.stderr)
        sys.exit(1)
    print(f'OK: depth={depth} hash={h} target={args.target_agent}')
    sys.exit(0)


if __name__ == '__main__':
    main()
