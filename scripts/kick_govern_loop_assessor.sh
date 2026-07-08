#!/usr/bin/env bash
# kick_govern_loop_assessor.sh — the one-command kick for operator slice 7.
#
#   bash ~/agent-core/scripts/kick_govern_loop_assessor.sh
#
# Dispatches the pre-authored govern-loop-assessor build envelope to Beacon via
# safe_write_inbox (the sanctioned source=larry headless dispatch path — same
# mechanism as build-operator-needs-you-feed, 2026-06-24), then stamps `kicked`
# into ~/agents/state/govern-loop-kickoff.json so govern_loop_readiness.py
# stops nudging. From there the standard hands-free line runs it: Beacon
# registers the mission + sequence -> Mirror DAG preflight -> Forge builds ->
# Mirror reviews -> auto-merge gate.
#
# Idempotent: refuses a second kick unless --force (the envelope also carries a
# dedup_identity, so even a forced duplicate is de-duplicated downstream).
#
# The build itself is read-only shadow mode per the approved spec
# agents/beacon/specs/govern-loop-assessor.md — it ranks where Larry's
# approvals pay off; it launches nothing and widens no autonomy.

set -euo pipefail

REPO_DIR="${HOME}/agent-core"
STATE_FILE="${HOME}/agents/state/govern-loop-kickoff.json"

if [ "${1:-}" != "--force" ] && [ -f "$STATE_FILE" ] \
    && python3 -c "import json,sys; sys.exit(0 if json.load(open('$STATE_FILE')).get('kicked') else 1)" 2>/dev/null; then
    echo "Already kicked ($(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('kicked'))")). Use --force to re-dispatch." >&2
    exit 0
fi

cd "$REPO_DIR/scripts"
python3 - <<'PYEOF'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from safe_write_inbox import safe_write_inbox

PROMPT = """GOAL: Build the govern-loop assessor per the APPROVED spec `agents/beacon/specs/govern-loop-assessor.md` (approval decision govern-loop-assessor-spec-001, resolved 2026-07-08). This is a Larry-approved build dispatched by his kick script — run it hands-free through the team; do NOT ask for a second Telegram approval (source=larry).

SCOPE — single PR, exactly the spec:
- scripts/govern_loop_assessor.py: read-only over ~/agents/state/decision-outcome-ledger.jsonl (import decision_outcome_ledger as the read API). Join decision rows to build_outcome rows by decision_key, LATEST build_outcome row wins per key (spec section 4 + 7.1). Bucket by actor and auto-approved-vs-human; payoff weights merged=+1 / merged_regressed=-1 / closed_unmerged=-0.5 in ONE named tunable place. Empty-key decision rows: count, never join, exclude from payoff (spec section 5). Ranked buckets + plain-language reason per bucket -> atomic write to ~/agents/state/govern-loop-assessment.json. --once CLI. Never raises; never writes the ledger, missions.json, or GitHub.
- Unit tests per spec section 9: fixture ledger -> expected ranking; latest-row-wins supersede; empty-key exclusion; missing/malformed ledger -> empty assessment, no raise; no-write-to-inputs check.
- Pattern-match scripts/mission_staleness.py for structure/logging/read-only discipline.

OUT OF SCOPE (spec section 5 — do not build): any autonomy proposal or action, timer/systemd wiring, dashboard surfacing, any write beyond the assessment state file.

PROCESS (your standard discipline): register the mission in missions.json (fresh govern-loop-assessor entry, cite capture cap-govern-loop-assessor-operator-layer-roi-rank-bra-28b0), author the single-step sequence (dispatch_text <=500 chars, reference spec sections by anchor), Mirror DAG preflight (--phase routing-signal), kickoff. Acceptance = spec section 6 checkboxes."""

envelope = {
    'task_id': 'build-govern-loop-assessor',
    'target_agent': 'beacon',
    'summary': ('Build operator slice 7: the govern-loop assessor '
                '(read-only shadow ranker over the decision-outcome ledger) '
                'per the approved spec, single PR, hands-free.'),
    'prompt': PROMPT,
    'source': 'larry',
    'actor': 'larry-kick-script',
    'dedup_identity': 'build:govern-loop-assessor',
    'timeout': 600,
}
dest = safe_write_inbox(
    target_agent='beacon',
    task_dict=envelope,
    source_agent='larry',
    filename='build-govern-loop-assessor.json',
)
print(f'dispatched: {dest}')

state_file = Path('~/agents/state/govern-loop-kickoff.json').expanduser()
try:
    state = json.loads(state_file.read_text())
    if not isinstance(state, dict):
        state = {}
except (OSError, ValueError):
    state = {}
state['kicked'] = datetime.now(timezone.utc).isoformat()
state['kicked_by'] = 'kick_govern_loop_assessor.sh'
state_file.parent.mkdir(parents=True, exist_ok=True)
tmp = Path(f'{state_file}.tmp.{os.getpid()}')  # pid-unique: no tmp collision with the watcher
tmp.write_text(json.dumps(state, indent=1))
tmp.replace(state_file)
print('kick stamped; the readiness nudge is now silent.')
PYEOF

echo "Slice 7 kicked. Watch: GitHub PRs, the build-sequences ladder, the Automated Work feed."
