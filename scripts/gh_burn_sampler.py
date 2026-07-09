#!/usr/bin/env python3
"""gh_burn_sampler.py — continuous GitHub rate-limit burn sampler (gh-api-burn phase 1).

Appends one JSON line per run to ``~/agents/logs/gh-api-burn.jsonl`` capturing the
current GraphQL + REST budget, read via ``gh_budget.remaining()`` — which uses the
FREE ``gh api rate_limit`` call (it does NOT consume the GraphQL budget), so this can
run on a tight cadence (a ~5-min timer) without adding to the burn it measures.

The accumulated log is the data substrate for ``gh_burn_analyzer.py``: after >=24h of
samples the analyzer measures the real peak-consumption-per-hour vs the 5000 ceiling
and how many hours hit exhaustion, then pings Larry to authorize the phase-2 durable
fix. Sampling is pure measurement — it never touches PR state or decision logic.

stdlib only. Fail-quiet: a failed budget read writes no line (nulls would pollute the
analyzer); the timer simply samples again next tick.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import gh_budget  # noqa: E402

HOME = Path.home()
AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(HOME / 'agents')))
BURN_LOG = AGENTS_ROOT / 'logs' / 'gh-api-burn.jsonl'


def build_sample(data: dict, now: Optional[datetime] = None) -> dict[str, Any]:
    """Reduce a ``gh_budget.remaining()`` dict to one flat burn-log record."""
    now = now or datetime.now(timezone.utc)
    graphql = data.get('graphql') if isinstance(data, dict) else None
    rest = data.get('rest') if isinstance(data, dict) else None
    graphql = graphql if isinstance(graphql, dict) else {}
    rest = rest if isinstance(rest, dict) else {}
    return {
        'ts': now.isoformat(),
        'graphql_remaining': graphql.get('remaining'),
        'graphql_limit': graphql.get('limit'),
        'graphql_reset': graphql.get('reset'),
        'rest_remaining': rest.get('remaining'),
        'rest_limit': rest.get('limit'),
    }


def append_sample(sample: dict, path: Path = BURN_LOG) -> None:
    """Append one JSON line to the burn log (append-only; created on first run)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(sample) + '\n')


def main() -> int:
    data = gh_budget.remaining()
    if not data or 'graphql' not in data:
        print('[gh_burn_sampler] budget read unavailable this tick; no sample '
              'written', file=sys.stderr, flush=True)
        return 0
    sample = build_sample(data)
    append_sample(sample, BURN_LOG)
    print(f'[gh_burn_sampler] {json.dumps(sample)}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
