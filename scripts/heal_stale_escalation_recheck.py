#!/usr/bin/env python3
"""
heal_stale_escalation_recheck.py — re-check a pending session-less escalation
whose cited blocker has since cleared, act on it, and retire the card.

THE GAP THIS CLOSES (2026-07-27, RSDPM #111).

A session-less Mirror escalation emits a decision card via
`outbox_notifier._emit_no_session_decision_approval`. That card is a SNAPSHOT
taken at emit time. Nothing re-evaluates it afterwards. The only automatic
path that ever retires one is
`outbox_notifier._reconcile_no_session_decision_on_merge`, which fires solely
when the PR MERGES — so a card whose premise dies before the merge sits on the
Approvals tab, wrong, until a human touches it.

RSDPM #111 is the worked example, and note the timing, because it rules out the
obvious fix:

    05:41:02  card fires. Mirror is CORRECT: the diff is clean, but the
              regression gate BLOCKs because main is red on
              control-inventory.contract.test.ts (missing `queue-error`).
    05:43:33  RSDPM #110 merges `npm run controls:sync`. main goes green.
              THE CARD IS NOW WRONG. Nothing notices.
    14:38:34  card finally retired — 'PR merged; decision moot' — by the
              merge reconciler, triggered by a human merging the PR by hand.

The blocker cleared 2m31s AFTER the card fired. An emit-time re-check would
have passed and fired the card anyway. The missing piece is a re-check WHILE
PENDING, which is what this healer is.

WHY IT ACTS INSTEAD OF JUST RE-LABELLING (Larry's call, 2026-07-27): a card
that says "the blocker cleared, go re-review" is still a card. Per
[[alert-toil-principle]] only irreversible/ambiguous decisions should reach
him, and "the base went green, update the branch" is neither. So on a
confirmed-stale card this healer takes the SAME action that resolved #111 by
hand — `gh pr update-branch` — and retires the card as superseded, letting the
existing Mirror + auto-review pipeline finish the job.

WHY update-branch AND NOT a bare re-review: the target repos run CI on
`pull_request`, so the checkout is the merge ref computed at event time.
Re-running the EXISTING run reuses the stale merge commit and stays red; only a
NEW event (which update-branch produces, via `synchronize`) builds against the
fixed base. Re-dispatching Mirror at the unchanged head would have re-read the
same red gate.

THE DECISION LADDER (first match wins, per candidate card):

  1. PR is not OPEN (MERGED/CLOSED) -> retire 'expired'. The merge case is
     usually already handled upstream; this catches CLOSED, which nothing
     else retires, and is a harmless no-op when the reconciler won the race.
  2. PR head != the head the card was cut against -> retire 'expired'
     (superseded). The card describes a commit that is no longer the PR. A
     review of the new head emits its own card under Contract D, so this
     loses nothing.
  3. STALE-GATE SIGNATURE, all of which must hold: base branch CI is green,
     the PR branch does NOT contain the base head (it is behind), the PR's own
     checks are failing, and the PR is a non-draft with no conflict. -> ACT:
     `gh pr update-branch`, then retire 'expired'.
  4. Anything else -> LEAVE PENDING. Uncertain is not stale.

TWO CARD CLASSES, TWO LADDER DEPTHS. Rules 2 and 3 both end in a retire, and
they are justified ONLY by Contract D — "a review of the new head emits its own
card". That premise is true for `mirror-review-*` cards and FALSE for
`heal_unregistered_approval`'s promoted `unreg-approval-*` cards, whose retire
is unrecoverable (source record already cleared, re-promotion blocked by the
history match, and the modal case cuts no replacement card at all). Promoted
cards therefore get rule 1 only; see PROMOTED_CARD_PREFIX. Applying the full
ladder to them would automate the exact #1058 stranding this healer's sibling
fix exists to prevent — rule 2 would fire on the first tick for any PR pushed
since its escalation, because the promoted coordinate carries the
escalation-era head.

ON RULE 3 AND CODE-FLAVOURED ESCALATIONS: the signature does not attempt to
parse Mirror's prose to decide whether it blamed the gate or the diff, because
prose parsing is exactly the trap that makes these cards brittle. It does not
need to. If Mirror escalated over the CODE, updating the branch triggers a
fresh review that re-escalates at the new head with a NEW card — the decision
is re-asked with accurate context, not silently dropped. The failure mode is a
re-asked question, not a lost one.

ORDERING IS LOAD-BEARING: act -> record ledger -> retire card. Dying after the
update but before the retire leaves the card pending, and the NEXT tick sees a
moved head and retires it under rule 2. Retiring first would risk a card
vanishing with nothing done — unrecoverable, since the card is gone by then.

SAFETY MODEL:
  - Kill switch (~/agents/healers.disabled) and --dry-run.
  - Ledger caps one auto-update per (repo, PR, head) for all time, so a
    update-branch that does not move the head cannot thrash. The rule-3 guard
    is independently self-limiting: "branch is behind base" goes false once
    the update lands.
  - Blast-radius cap per run (MAX_ACTIONS_PER_RUN).
  - Read-only `gh` calls go direct; the one destructive verb goes through
    `gh_write` so it refuses under test.
  - Every probe failure is UNCERTAIN, never stale: a card is retired only on
    positive evidence. Missing/malformed ledger degrades to empty (safe: the
    rule-3 guard still bounds re-action).
  - Never raises out of main(); heartbeats each run and alerts on its own
    failure, like its sibling healers.
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

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from atomic_io import atomic_write_json  # noqa: E402
from test_isolation_guard import gh_write  # noqa: E402
import beacon_approval_handler as approval  # noqa: E402
import heal_unregistered_approval as hua  # noqa: E402 — owns the promoted-card id shape
import larry_alerts as la  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-stale-escalation-recheck.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-stale-escalation-recheck.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-stale-escalation-recheck.json'

HEALER_SOURCE = 'heal-stale-escalation-recheck'

# Session-less Mirror decision cards: the id shape minted by
# `_emit_no_session_decision_approval`. These carry the FULL ladder, because
# their class satisfies the Contract D premise rules 2/3 depend on — a review
# of a new head emits its own per-head card, so retiring a superseded one
# loses nothing.
CARD_ID_PREFIX = 'mirror-review-'

# `heal_unregistered_approval`'s promoted stranded-escalation cards (agent-core
# #1058). Same decision class, same coordinate — but they get a STRICTLY
# NARROWER ladder (see `_full_ladder_card`), because the Contract D premise is
# FALSE for them and retiring one is unrecoverable:
#
#   * the source for-Larry record is cleared at promote time
#     (heal_unregistered_approval's resolve-on-promote), and
#   * `_healer_task_registered` matches the retired card in HISTORY, so the
#     record can never be re-promoted, and
#   * the modal promoted card originates from the notifier's ACTION_NEEDED
#     bucket, which by design cuts NO Approvals card at all — so a repeat
#     escalation writes only another for-Larry record under the same id, whose
#     promotion is now blocked.
#
# Rules 2/3 would therefore delete Larry's ONLY decision surface with nothing
# done — and rule 2 fires on the very first tick for any PR pushed since its
# escalation, because the promoted coordinate carries the escalation-era head.
# That is the #1058 stranding, automated. Only rule 1 (the PR reached a
# terminal state, so the decision is genuinely moot) is safe here.
PROMOTED_CARD_PREFIX = f'{hua.PROMOTED_TASK_PREFIX}-'

CARD_ID_PREFIXES = (CARD_ID_PREFIX, PROMOTED_CARD_PREFIX)

GH_TIMEOUT_S = 60

# Blast radius: at most this many PRs are auto-updated per tick. A backlog
# drains over successive ticks rather than firing a burst of CI runs.
MAX_ACTIONS_PER_RUN = 3


# -------------------- logging / heartbeat --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(json.dumps({
            'ts': datetime.now(timezone.utc).isoformat(),
            'healer': HEALER_SOURCE,
        }))
    except OSError:
        pass


# -------------------- ledger --------------------

def load_ledger() -> dict[str, Any]:
    """Ledger of already-actioned (repo, pr, head) keys.

    A missing or malformed file degrades to empty rather than crashing. That
    fail-open direction is safe here: rule 3 additionally requires the branch
    to be BEHIND its base, which stops being true once an update lands, so a
    lost ledger cannot produce an unbounded re-action loop.
    """
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {'actioned': {}}
    if not isinstance(data, dict):
        return {'actioned': {}}
    actioned = data.get('actioned')
    if not isinstance(actioned, dict):
        data['actioned'] = {}
    return data


def save_ledger(ledger: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(STATE_FILE, ledger)
    except OSError as e:
        log(f'ledger write failed: {type(e).__name__}: {e}', 'WARN')


def ledger_key(repo: str, pr_number: int, head_sha: str) -> str:
    return f'{repo}#{pr_number}@{head_sha[:12]}'


# -------------------- gh helpers --------------------

def _gh_env() -> dict[str, str]:
    return {**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/snap/bin'}


def gh_json(*args: str, default: Any = None) -> Any:
    """Run a READ-ONLY `gh` command capturing JSON. Returns default on any
    failure — a probe that cannot answer is UNCERTAIN, never 'stale'."""
    try:
        result = subprocess.run(
            ['gh', *args],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=_gh_env(),
        )
        if result.returncode != 0:
            log(f'gh {" ".join(args[:3])}... rc={result.returncode} '
                f'stderr={result.stderr.strip()[:200]}', 'WARN')
            return default
        out = result.stdout.strip()
        return json.loads(out) if out else default
    except (subprocess.TimeoutExpired, json.JSONDecodeError,
            FileNotFoundError, OSError) as e:
        log(f'gh {" ".join(args[:3])}... exception: {type(e).__name__}: {e}',
            'WARN')
        return default


def update_branch(repo: str, pr_number: int) -> tuple[bool, str]:
    """`gh pr update-branch` — the one destructive verb, so it is routed
    through gh_write. Returns (ok, detail)."""
    try:
        result = gh_write(
            ['gh', 'pr', 'update-branch', str(pr_number), '--repo', repo],
            capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=_gh_env(),
        )
        if result.returncode == 0:
            return True, (result.stdout or '').strip()[:200]
        return False, (result.stderr or '').strip()[:200]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f'{type(e).__name__}: {e}'


# -------------------- PR probes --------------------

def probe_pr(repo: str, pr_number: int) -> Optional[dict[str, Any]]:
    return gh_json(
        'pr', 'view', str(pr_number), '--repo', repo, '--json',
        'state,isDraft,mergeStateStatus,headRefOid,baseRefName,statusCheckRollup',
        default=None,
    )


def base_branch_is_green(repo: str, base_ref: str) -> bool:
    """True only when the base branch's most recent completed CI run SUCCEEDED.

    Deliberately positive-evidence-only: no runs, an in-flight run, or an
    unreadable response all return False, which keeps the card pending.
    """
    runs = gh_json(
        'run', 'list', '--repo', repo, '--branch', base_ref, '--limit', '5',
        '--json', 'conclusion,status', default=None,
    )
    if not isinstance(runs, list):
        return False
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get('status') != 'completed':
            continue  # in flight — look further back for the last verdict
        return run.get('conclusion') == 'success'
    return False


def pr_checks_failing(pr: dict[str, Any]) -> bool:
    """True iff at least one completed check on the PR is a hard failure."""
    rollup = pr.get('statusCheckRollup')
    if not isinstance(rollup, list):
        return False
    bad = {'FAILURE', 'TIMED_OUT', 'STARTUP_FAILURE'}
    for check in rollup:
        if not isinstance(check, dict):
            continue
        verdict = check.get('conclusion') or check.get('state')
        if isinstance(verdict, str) and verdict.upper() in bad:
            return True
    return False


def branch_is_behind_base(repo: str, base_ref: str, head_sha: str) -> bool:
    """True iff base's head is NOT an ancestor of the PR head — i.e. the PR
    has not absorbed whatever landed on base.

    Uses the compare API: `status` is 'behind' or 'diverged' when base carries
    commits the head lacks. Unreadable ⇒ False (uncertain, leave pending).
    """
    cmp_data = gh_json(
        'api', f'repos/{repo}/compare/{base_ref}...{head_sha}',
        '--jq', '{status: .status}', default=None,
    )
    if not isinstance(cmp_data, dict):
        return False
    return cmp_data.get('status') in ('behind', 'diverged')


# -------------------- card scanning --------------------

def candidate_cards() -> list[dict[str, Any]]:
    """Pending session-less Mirror decision cards carrying a PR coordinate.

    The coordinate comes from `recheck_target`, the same structured field the
    Approvals tab's third action uses. A card without one cannot be acted on
    (we would be guessing the PR), so it is skipped rather than parsed out of
    the prose summary.

    `head_sha` is required only for FULL-ladder cards, whose rules 2/3 compare
    against it. Promoted cards run rule 1 alone, which needs nothing but the
    repo and PR number — and requiring a head there stranded them (review
    round 2): a promoted coordinate may legitimately omit `head_sha` when the
    promote-time probe failed, and rule 1 is the ONLY thing that retires a
    promoted card once its PR merges. Verified there is no other path —
    `heal_unregistered_approval.reconcile_retire` finds no `#N` in the
    `mirror-review:<task>` ledger subject so it never gh-probes,
    `heal_stale_approvals`' coordinate expiry requires the `mirror-review-pr-`
    wrapper, and `outbox_notifier._reconcile_no_session_decision_on_merge`
    matches `mirror-review-*` only. So a headless promoted card would sit on
    the tab forever after its PR merged.
    """
    try:
        pending = approval.load_state().get('pending', [])
    except Exception as e:  # noqa: BLE001 — a broken state file must not crash
        log(f'could not read approval state: {type(e).__name__}: {e}', 'WARN')
        return []
    out = []
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        card_id = entry.get('id')
        if not isinstance(card_id, str) \
                or not card_id.startswith(CARD_ID_PREFIXES):
            continue
        target = (entry.get('dispatch_payload') or {}).get('recheck_target')
        if not isinstance(target, dict):
            continue
        pr_url = target.get('pr_url')
        head_sha = target.get('head_sha')
        if not isinstance(pr_url, str):
            continue
        if not isinstance(head_sha, str) or not head_sha:
            head_sha = None
        full_ladder = _full_ladder_card(card_id)
        if full_ladder and head_sha is None:
            # Rules 2/3 compare against the card's head; without one there is
            # nothing to compare and the full ladder cannot run.
            continue
        coord = parse_pr_url(pr_url)
        if coord is None:
            continue
        repo, pr_number = coord
        out.append({
            'card_id': card_id,
            'repo': repo,
            'pr_number': pr_number,
            'head_sha': head_sha,
            'pr_url': pr_url,
            'full_ladder': full_ladder,
        })
    return out


def _full_ladder_card(card_id: str) -> bool:
    """True iff every rung may act on this card.

    False for promoted stranded-escalation cards, which get rule 1 only — see
    PROMOTED_CARD_PREFIX for why retiring one is unrecoverable. Keyed on the id
    prefix the promoter owns, not on the coordinate's shape: the coordinate is
    incidental data that another promotion class could grow, and this ladder
    runs an irreversible verb.
    """
    return not card_id.startswith(PROMOTED_CARD_PREFIX)


def parse_pr_url(pr_url: str) -> Optional[tuple[str, int]]:
    """'https://github.com/OWNER/REPO/pull/123' -> ('OWNER/REPO', 123)."""
    parts = pr_url.rstrip('/').split('/')
    if len(parts) < 7 or parts[-2] != 'pull':
        return None
    try:
        pr_number = int(parts[-1])
    except ValueError:
        return None
    owner, repo_name = parts[-4], parts[-3]
    if not owner or not repo_name:
        return None
    return f'{owner}/{repo_name}', pr_number


# -------------------- the ladder --------------------

def retire(card_id: str, note: str, dry_run: bool) -> bool:
    if dry_run:
        log(f'DRY-RUN would retire {card_id}: {note}')
        return True
    try:
        resolved = approval.resolve(card_id, 'expired', note=note)
    except Exception as e:  # noqa: BLE001
        log(f'retire failed for {card_id}: {type(e).__name__}: {e}', 'WARN')
        return False
    if resolved is None:
        log(f'retire no-op for {card_id} (already gone)')
        return False
    log(f'retired {card_id}: {note}')
    return True


def evaluate(card: dict[str, Any], ledger: dict[str, Any], dry_run: bool,
             budget: list[int]) -> str:
    """Apply the ladder to one card. Returns an outcome tag for the summary."""
    repo, pr_number = card['repo'], card['pr_number']
    card_id, card_head = card['card_id'], card.get('head_sha')

    pr = probe_pr(repo, pr_number)
    if pr is None:
        return 'probe-failed'

    # Rule 1 — terminal PR. Safe for EVERY card class: the PR reached a
    # terminal state, so there is no decision left to lose.
    state = pr.get('state')
    if state in ('MERGED', 'CLOSED'):
        retire(card_id, f'PR {state.lower()}; decision moot', dry_run)
        return f'retired-{state.lower()}'

    # Rules 2 and 3 both END in a retire, and retiring a promoted card is
    # unrecoverable (PROMOTED_CARD_PREFIX). Stop here for that class: an open
    # PR keeps its card, which is the whole point of the card.
    #
    # Derived from the id here rather than read off `card['full_ladder']`:
    # candidate_cards stamps that key for observability, but a caller that
    # builds a card dict without it must not silently change which rungs run
    # (in either direction). One pure predicate, one source of truth.
    if not _full_ladder_card(card_id):
        return 'left-pending-promoted-card'

    # Belt-and-braces: candidate_cards guarantees a head on full-ladder cards,
    # so this is unreachable from the healer's own scan. A hand-built card dict
    # must degrade to "uncertain, leave it" rather than crash the tick or, far
    # worse, compare a live head against None and retire as superseded.
    if not isinstance(card_head, str) or not card_head:
        return 'left-pending-no-card-head'

    # Rule 2 — the card describes a head that is no longer the PR's.
    live_head = pr.get('headRefOid')
    if isinstance(live_head, str) and live_head != card_head:
        retire(
            card_id,
            f'superseded by newer head {live_head[:8]} '
            f'(card was cut at {card_head[:8]})',
            dry_run,
        )
        return 'retired-superseded'

    # Rule 3 — the stale-gate signature. Every clause must hold.
    if pr.get('isDraft'):
        return 'left-pending-draft'
    if pr.get('mergeStateStatus') in ('DIRTY', 'CONFLICTING'):
        return 'left-pending-conflict'
    if not pr_checks_failing(pr):
        return 'left-pending-checks-not-red'

    base_ref = pr.get('baseRefName')
    if not isinstance(base_ref, str) or not base_ref:
        return 'left-pending-no-base'
    if not base_branch_is_green(repo, base_ref):
        return 'left-pending-base-not-green'
    if not branch_is_behind_base(repo, base_ref, card_head):
        return 'left-pending-not-behind'

    key = ledger_key(repo, pr_number, card_head)
    if key in ledger['actioned']:
        return 'left-pending-already-actioned'
    if budget[0] <= 0:
        log(f'budget exhausted, deferring {repo}#{pr_number} to next tick')
        return 'deferred-budget'

    # ACT -> LEDGER -> RETIRE. This order is deliberate: a crash after the
    # update leaves the card pending, and the next tick retires it under
    # rule 2 once it sees the moved head. The reverse order could lose a card
    # with nothing done.
    if dry_run:
        log(f'DRY-RUN would update-branch {repo}#{pr_number} '
            f'(base {base_ref} green, branch behind, PR checks red)')
        budget[0] -= 1
        return 'dry-run-would-act'

    ok, detail = update_branch(repo, pr_number)
    if not ok:
        log(f'update-branch failed for {repo}#{pr_number}: {detail}', 'WARN')
        return 'act-failed'
    budget[0] -= 1
    log(f'update-branch OK for {repo}#{pr_number}: {detail}')

    ledger['actioned'][key] = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'card_id': card_id,
        'base_ref': base_ref,
    }
    save_ledger(ledger)

    retire(
        card_id,
        f'blocker cleared: base {base_ref} is green and the branch was behind; '
        f'branch updated, pipeline re-reviewing',
        dry_run,
    )
    return 'acted'


# -------------------- main --------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='probe and report without writing anything')
    args = parser.parse_args()

    heartbeat()

    if KILL_SWITCH.exists():
        log('kill switch present; standing down')
        return 0

    try:
        cards = candidate_cards()
        if not cards:
            log('no pending session-less escalation cards with a PR coordinate')
            return 0

        ledger = load_ledger()
        budget = [MAX_ACTIONS_PER_RUN]
        outcomes: dict[str, int] = {}
        for card in cards:
            tag = evaluate(card, ledger, args.dry_run, budget)
            outcomes[tag] = outcomes.get(tag, 0) + 1
            log(f'{card["repo"]}#{card["pr_number"]} '
                f'({card["card_id"]}) -> {tag}')

        summary = ', '.join(f'{k}={v}' for k, v in sorted(outcomes.items()))
        log(f'tick complete: {len(cards)} card(s); {summary}')
        return 0
    except Exception as e:  # noqa: BLE001 — a healer must not die silently
        log(f'healer failed: {type(e).__name__}: {e}', 'ERROR')
        try:
            la.append_alert(
                source=HEALER_SOURCE,
                severity='warning',
                message=f'heal_stale_escalation_recheck failed: '
                        f'{type(e).__name__}: {e}',
                subject='Stale-escalation recheck healer failed',
                suggested_action='Check ~/agents/logs/'
                                 'heal-stale-escalation-recheck.log',
            )
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
