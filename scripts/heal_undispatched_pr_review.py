#!/usr/bin/env python3
"""
heal_undispatched_pr_review.py — GitHub-truth backstop for the Forge→Mirror
review dispatch.

THE GAP THIS CLOSES (the PR #412 incident, 2026-06-10):

Forge built and opened PR #412 cleanly at 06:50Z, but `heal_wedged_review_sessions`
reaped Forge's still-running build session/worktree at ~06:59Z (the build had gone
quiet >15min running tests, so it looked wedged). agent_runner therefore recorded
the build task `success=False` with no result — and CRUCIALLY never wrote the
`phase=build` outbox carrying `PR opened: <url>`.

Every existing recovery path keys off that build outbox:
  - the inline `_dispatch_mirror_review` hop in `process_outbox` reads it, and
  - the `_reconcile_missed_mirror_reviews` sweep re-scans ARCHIVED build outboxes.
With no build outbox ever written, BOTH are structurally blind to the PR. The
result was a clean, mergeable PR sitting unreviewed with nothing in flight — only
caught when Pulse independently queried GitHub PR state and filed a cycle-finding,
which then required a human to re-dispatch.

This healer is the durable, GitHub-truth analogue of `heal_unreviewed_merge_detector`
(which is the *merged*-PR detective; this is the *open*-PR actor): it asks GitHub
directly "what open Forge PRs exist?" and, for any that have no Mirror review task
anywhere (inbox / archive / .invalid), dispatches one. Because it reads GitHub —
not a local outbox that a reap can prevent from ever existing — it recovers the
exact failure shape that defeated the outbox-based reconcile.

Key design points (follows the heal_* conventions):
  - GitHub-truth: `gh pr list --state open` → filter to `forge/*` head branches.
  - "Already reviewed" is decided by `outbox_notifier._review_request_already_dispatched`
    (the SAME predicate the inline dispatch + outbox reconcile use), NOT GitHub's
    `reviewDecision` — Mirror emits marker blocks, never a GitHub review, so
    `reviewDecision` is always empty for these PRs and is useless here.
  - Reuses `outbox_notifier._dispatch_mirror_review` verbatim (import-safe: that
    module's executable code is under `if __name__ == '__main__'`). Reusing the
    real builder is deliberate — a hand-rolled copy of the review-task envelope is
    exactly the drift class that produced the sibling dropped-marker bug.
  - Age grace: only PRs open longer than DISPATCH_GRACE_MINUTES are considered, so
    the normal inline path gets first crack on a freshly opened PR and this healer
    only fills genuine gaps.
  - Idempotent: the dispatch's own presence check dedups; a per-PR failure ledger
    in the state file dedups the (rare) dispatch-FAILED escalation so one stuck PR
    pages Larry once, not every tick.
  - Kill-switch aware, dry-run env override (default ON — its whole job is to act),
    fail-safe (gh/dispatch errors are logged, NEVER crash the timer).
  - Read-only on GitHub (lists PRs; never merges, closes, comments, or reviews).

Known scope limits (deliberate for this revision):
  - agent-core only. REPO is fixed to Larry-Yatch/ourliberty-agent-core (the
    repo of the #412 incident). Forge can also open PRs against
    ourliberty-dashboard; those are not covered here. A multi-repo sweep is a
    straightforward follow-up (loop REPO + set target_repo per repo).
  - task_id is recovered from the head branch by stripping `forge/`, which is
    exact only when the branch round-trips the task_id. Forge build task_ids are
    kebab-case in practice (e.g. `harden-test-prod-write-isolation-001`), for
    which `forge/<task_id>` is loss-free. The dispatch validator DOES allow
    task_ids with `: @ #`/spaces (e.g. Medic-domain ids), and the worktree
    branch sanitizer collapses those to `-` while the inbox name preserves them
    — so a Forge build PR carrying such an id would derive a slightly-off
    task_id here. The dispatched review still targets the correct PR URL (Mirror
    reviews the right PR); only task_id-keyed dedup/correlation degrades, and
    the dispatch's own filename idempotency still prevents a same-key duplicate.

The dispatch-FAILED escalation is Python-emitted, so it has a registered entry in
config/alert-translations.json under source `heal-undispatched-pr-review` (subject
prefix `undispatched-pr-review`), enforced by the translation-coverage gate
(scripts/tests/test_alert_translations.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-undispatched-pr-review.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-undispatched-pr-review.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-undispatched-pr-review.json'

REPO = 'Larry-Yatch/ourliberty-agent-core'
# Only Forge-produced build PRs are dispatched a Mirror review by the pipeline.
# The reliable signal of a Forge PR is its head branch prefix (the worktree/branch
# convention `forge/<task-id>`); PR author is the human identity the bot commits
# as, so it cannot distinguish bot PRs from Larry's own and is NOT used to gate.
FORGE_BRANCH_PREFIX = 'forge/'

GH_TIMEOUT_S = 30
# Per-call row cap. Open Forge PRs at any instant are few (bounded by the fleet's
# concurrency); 200 is comfortable headroom. A truncated page only delays the
# oldest tail by one tick (the next tick re-lists), so unlike the merged-PR
# detective there is no cursor to advance past an unscanned tail — no range-paging
# needed.
_OPEN_PR_FETCH_LIMIT = 200
# Give the normal inline Forge→Mirror dispatch (and the outbox reconcile) time to
# fire on a freshly opened PR before this backstop steps in. The incident PR sat
# unreviewed for ~30min; 10min is well inside that and well past the seconds the
# happy path needs.
DISPATCH_GRACE_MINUTES = 10
# Keep the failure ledger from growing without bound.
MAX_FAILED_LEDGER = 200

ALERT_SOURCE = 'heal-undispatched-pr-review'
# Subject prefix MUST stay in sync with the translation entry keyed under
# `undispatched-pr-review` in config/alert-translations.json.
SUBJECT_PREFIX = 'undispatched-pr-review'

# Activation env var. The act (dispatch a Mirror review) is idempotent and always
# the desired outcome — every Forge PR is meant to be reviewed — so this defaults
# ON. Set to a non-"true" value for dry-run (detect + log, dispatch nothing)
# during initial verification, mirroring the merge-detector's override.
ENV_ENABLED = 'OURLIBERTY_UNDISPATCHED_PR_REVIEW_ENABLED'


# -------------------- logging + heartbeat --------------------

def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


# -------------------- kill-switch + activation --------------------

def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def healer_enabled() -> bool:
    """True unless OURLIBERTY_UNDISPATCHED_PR_REVIEW_ENABLED is explicitly a
    non-true value. Default ON. Set to e.g. 'false' to run dry-run (detect + log,
    dispatch nothing)."""
    raw = os.environ.get(ENV_ENABLED)
    if raw is None:
        return True
    return raw.strip().lower() == 'true'


# -------------------- state (failure dedup) --------------------

def load_state() -> dict[str, Any]:
    """Return {'failed_prs': {pr_url: iso}} — PRs whose review dispatch FAILED and
    were escalated, so we don't re-page on every tick."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'failed_prs': {}}
        if not isinstance(data.get('failed_prs'), dict):
            data['failed_prs'] = {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'failed_prs': {}}


def save_state(state: dict[str, Any]) -> None:
    failed = state.get('failed_prs', {})
    if isinstance(failed, dict) and len(failed) > MAX_FAILED_LEDGER:
        kept = sorted(failed.items(), key=lambda kv: kv[1] or '')[-MAX_FAILED_LEDGER:]
        state['failed_prs'] = dict(kept)
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, indent=2))
        tmp.rename(STATE_FILE)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


# -------------------- time helpers --------------------

def _parse_iso(ts: str) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# -------------------- gh I/O (mockable seam) --------------------

def fetch_open_prs() -> Optional[list[dict[str, Any]]]:
    """List open PRs via gh. Returns parsed rows, or None on ANY gh failure
    (already logged) — a healer must never crash the timer. Each row carries
    number, url, headRefName, createdAt, title, headRefOid."""
    cmd = [
        'gh', 'pr', 'list',
        '--repo', REPO,
        '--state', 'open',
        '--limit', str(_OPEN_PR_FETCH_LIMIT),
        '--json', 'number,url,headRefName,createdAt,title,headRefOid',
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh pr list failed: {type(e).__name__}: {e}', 'WARN')
        return None
    if proc.returncode != 0:
        log(f'gh pr list returned {proc.returncode}: '
            f'{proc.stderr.strip()[:300]}', 'WARN')
        return None
    return parse_open_prs(proc.stdout)


def parse_open_prs(raw_json: str) -> list[dict[str, Any]]:
    """Normalize `gh pr list --json ...` stdout into flat rows. Skips malformed
    entries rather than raising."""
    try:
        rows = json.loads(raw_json or '[]')
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        number = r.get('number')
        url = r.get('url')
        head = r.get('headRefName')
        created = r.get('createdAt')
        if number is None or not url or not head or not created:
            continue
        out.append({
            'number': number,
            'url': url,
            'headRefName': head,
            'createdAt': created,
            'title': r.get('title') or '',
            # head commit SHA — lets the dedup distinguish "this PR's CURRENT
            # head was reviewed" from "an EARLIER head was reviewed". Tolerated
            # absent (older gh, mocks) → dedup falls back to existence-only.
            'headRefOid': r.get('headRefOid'),
        })
    return out


# -------------------- core detection (pure) --------------------

def task_id_for_branch(head_ref: str) -> str:
    """Map a Forge head branch to its task_id: strip the `forge/` prefix. This is
    the inverse of the worktree/branch convention `forge/<task-id>` and matches
    the task_id the build outbox would have carried — so the derived review
    filename `review-<task-id>.json` lines up with the inline path's idempotency
    key."""
    if head_ref.startswith(FORGE_BRANCH_PREFIX):
        return head_ref[len(FORGE_BRANCH_PREFIX):]
    return head_ref


def select_orphaned_prs(
    open_prs: list[dict[str, Any]],
    now: datetime,
    already_dispatched: Any,
    grace_minutes: int = DISPATCH_GRACE_MINUTES,
) -> list[dict[str, Any]]:
    """Pure selection step. From the open-PR rows, return those that are Forge
    build PRs, older than the grace window, and have NO Mirror review task yet.

    `already_dispatched(task_id, head_sha) -> bool` is injected (the production
    caller passes a thin wrapper over
    `outbox_notifier._review_request_already_dispatched`) so this core is
    testable without the notifier or the filesystem. `head_sha` is the PR's
    current head commit; the wrapper treats a review of a DIFFERENT (older) head
    as not-yet-dispatched, so a PR updated after its first review is re-reviewed.
    Each returned row is the input row augmented with its derived `task_id`.
    """
    cutoff = now - timedelta(minutes=grace_minutes)
    out: list[dict[str, Any]] = []
    for pr in open_prs:
        head = str(pr.get('headRefName') or '')
        if not head.startswith(FORGE_BRANCH_PREFIX):
            continue  # not a Forge build PR
        created = _parse_iso(pr.get('createdAt'))
        if created is None or created > cutoff:
            continue  # too fresh — let the normal inline dispatch fire first
        task_id = task_id_for_branch(head)
        if not task_id:
            continue  # degenerate 'forge/' branch → no dispatchable task_id
        try:
            if already_dispatched(task_id, pr.get('headRefOid')):
                continue  # CURRENT head already in inbox / archive / .invalid
        except Exception as e:  # noqa: BLE001 — never let a probe crash selection
            log(f'already_dispatched probe raised for task={task_id}: '
                f'{type(e).__name__}: {e}; treating as undispatched', 'WARN')
        out.append({**pr, 'task_id': task_id})
    return out


# -------------------- dispatch + escalation (effectful) --------------------

def _synthesize_build_data(pr: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal `data` envelope `_dispatch_mirror_review` consumes from a
    GitHub PR row. There is no Forge build outbox to read (the reap prevented it),
    so we reconstruct from GitHub truth: task_id (from branch), branch, target_repo
    (this repo), pr_title. No `claude_session_id` is available — Forge's build
    session is gone — so a downstream REVIEW_REVISION will start Forge fresh rather
    than --resume; that is the correct, available behavior for a recovered PR."""
    return {
        'task_id': pr['task_id'],
        'target_repo': 'ourliberty-agent-core',
        'branch': pr['headRefName'],
        'pr_title': pr.get('title') or '',
        'dispatched_by': 'heal-undispatched-pr-review',
    }


def emit_failed_alert(pr: dict[str, Any]) -> bool:
    """Page Larry that an orphaned PR's review dispatch did not take. Per-PR dedup
    is the caller's job (state ledger); larry_alerts adds a (source,subject)
    cooldown. Never raises."""
    number = pr['number']
    url = pr['url']
    subject = f'{SUBJECT_PREFIX}:{number}'
    message = (
        f'PR #{number} is open with no Mirror review, and the backstop review '
        f'dispatch did not take (review task still absent after dispatch). '
        f'PR: {url} (branch {pr.get("headRefName")}).'
    )
    # Don't assert a single root cause: _dispatch_mirror_review writes nothing —
    # and returns without raising — for several reasons (missing target_repo,
    # RoutingDenied/DispatchRejected, OR the cost-budget gate denying the task).
    # Name all of them so the page doesn't send Larry chasing the wrong one.
    suggested_action = (
        f'The Mirror review-request was not written for task `{pr["task_id"]}`. '
        f'Likely causes: routing denied / dispatch rejected, missing '
        f'target_repo, or the cost-budget gate. Check the outbox-notifier log '
        f'(~/agents/logs/outbox-notifier.log) for a "review-request dispatch '
        f'FAILED" / RoutingDenied / cost-budget line referencing the task. '
        f'Re-dispatch manually by writing a review task to '
        f'~/agents/inboxes/mirror/, or `gh pr view {number} --repo {REPO}`.'
    )
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source=ALERT_SOURCE,
            severity='critical',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
            route='escalate',
        )
    except Exception as e:  # noqa: BLE001 — emission must never crash the healer
        log(f'emit_failed_alert failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- main --------------------

def main() -> int:
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return 0
    heartbeat()

    # Import the notifier's dispatch + presence predicate here (not at module
    # top) so a notifier import hiccup degrades to a logged no-op tick rather
    # than a crashed timer, and so the pure core above imports cleanly in tests.
    try:
        import outbox_notifier as notifier
        import safe_write_inbox
    except Exception as e:  # noqa: BLE001
        log(f'could not import dispatch deps: {type(e).__name__}: {e}; '
            f'skipping tick', 'ERROR')
        return 0

    def _already_dispatched(task_id: str, head_sha: Optional[str] = None) -> bool:
        fname = safe_write_inbox.canonical_inbox_name(f'review-{task_id}.json')
        return notifier._review_request_already_dispatched(fname, head_sha)

    open_prs = fetch_open_prs()
    if open_prs is None:
        log('gh unavailable this tick; no scan')
        return 0

    now = datetime.now(timezone.utc)
    state = load_state()
    failed = state['failed_prs']

    orphaned = select_orphaned_prs(open_prs, now, _already_dispatched)
    log(f'scanned {len(open_prs)} open PR(s); {len(orphaned)} Forge PR(s) '
        f'past grace with no Mirror review')

    dry_run = not healer_enabled()
    dispatched = 0
    for pr in orphaned:
        url = str(pr['url']).rstrip('/')
        if dry_run:
            log(f'[dry-run] would dispatch Mirror review for PR #{pr["number"]} '
                f'(task={pr["task_id"]}, {url})', 'WARN')
            continue

        # TOCTOU recheck (mirrors the sibling _reconcile_missed_mirror_reviews):
        # the PR was open when listed, but an auto-merge / babysit loop may have
        # merged or closed it since. Don't spin Mirror up on a dead PR. None
        # (gh unknown) → leave for the next tick rather than dispatch blind.
        parsed = notifier._parse_pr_url(url)
        if parsed is not None:
            is_open = notifier._gh_pr_is_open(*parsed)
            if is_open is None:
                log(f'PR #{pr["number"]} open-state unknown this tick; '
                    f'leaving for next sweep', 'INFO')
                continue
            if not is_open:
                log(f'PR #{pr["number"]} no longer open (merged/closed since '
                    f'listing); skipping review dispatch', 'INFO')
                continue

        log(f'ORPHANED_PR_REVIEW PR #{pr["number"]} task={pr["task_id"]} '
            f'pr={url} — no Mirror review dispatched; dispatching backstop review',
            'WARN')
        data = _synthesize_build_data(pr)
        try:
            notifier._dispatch_mirror_review(data, url)
        except Exception as e:  # noqa: BLE001 — one bad PR must not stop the rest
            log(f'_dispatch_mirror_review raised for task={pr["task_id"]}: '
                f'{type(e).__name__}: {e}', 'WARN')

        # Verify the dispatch took (the inner call swallows DispatchRejected /
        # RoutingDenied as a WARN). If the review task is now present, success.
        if _already_dispatched(pr['task_id']):
            dispatched += 1
            failed.pop(url, None)  # clear any prior failure record
            continue

        # Dispatch did not produce a review task — escalate once per PR.
        if url in failed:
            log(f'PR #{pr["number"]} review dispatch still failing '
                f'(already escalated); leaving for inspection', 'WARN')
            continue
        if emit_failed_alert(pr):
            log(f'ALERT PR #{pr["number"]} review dispatch FAILED ({url})', 'WARN')
        failed[url] = now.isoformat()

    save_state(state)
    log(f'tick: open={len(open_prs)} orphaned={len(orphaned)} '
        f'dispatched={dispatched} dry_run={dry_run}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — fail-safe: never crash the timer
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        sys.exit(1)
