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

EXTENSION — opt-in label for human PRs (2026-06-22):

The same structural blindness also stranded PRs Larry opens by hand (#509, #510,
#625): they live on non-`forge/*` branches, so the inline Forge→Mirror dispatch
(keyed off Forge's build outbox, which only Forge writes) never fired, AND this
backstop's forge-only filter skipped them — they sat unreviewed until a human
noticed. This healer now also routes any *non-draft* PR carrying the
`auto-review` label to a Mirror review, exactly like a Forge PR (a Mirror PASS
auto-merges).

GROUND-TRUTH BACK-OFF (the PR #865 triple-dispatch, 2026-07-08):

The head-aware dedup alone proved too eager on a PR mid-pipeline. Between a
review's dispatch and its verdict the head keeps moving (build checkpoints,
revision pushes), so "no archived review record carries the CURRENT head" is
true for long stretches of a perfectly healthy review cascade — and the
revision-round records (`review-<task>-rev<N>.json`) recorded no head at all.
This healer fired three times in 40 minutes for one task; the third dispatch
re-reviewed a head that already carried a PASS `mirror-review` commit status
and overwrote the merged PR's findings comment with a stale REVISION.
`pipeline_backoff_reason` now runs before any dispatch (local checks first, gh
last): an active Mirror session, a recent review record of ANY round shape
(dispatch-time window), or a SUCCESS `mirror-review` status on the current head
each mean the pipeline owns the PR. A failure status or a gh error falls
through to the window-bounded recency guard rather than permanently exempting
the PR — only a definitive PASS terminally backs off (re-reviewing a PASS is
the exact #865 harm). Companion notifier fixes: re-review dispatches record the
head they cover, and the shared `review_record_name_re` grammar (reused here)
teaches the dedup predicate — archive, live-inbox, AND lost-result legs — the
revision-round filenames.

Why a LABEL and not a branch rule: neither branch prefix nor PR author can
separate Larry's hand-opened PRs from the agent team's. The team commits as
Larry's own GitHub identity and uses the very same prefixes (`fix/`, `feat/`,
`chore/`, …). So "any non-forge branch == human" is false — it would auto-merge
agent PRs, including the `feat/new-mission-*` PRs that heal_orphan_autoregister
must CLOSE rather than merge. The label is the one reliable signal: it is applied
only on the desktop side (whatever opens a PR for Larry tags it — the droplet
agents never add it), so a labeled PR is unambiguously cleared for the team.
Draft is the safety valve: a draft labeled PR is "still iterating" and is left
alone until marked ready, so an unfinished PR is never auto-merged.

Key design points (follows the heal_* conventions):
  - GitHub-truth: `gh pr list --state open` → keep Forge build PRs (`forge/*`
    head) and non-draft PRs labeled `auto-review`; everything else is skipped.
  - "Already reviewed" is decided by `outbox_notifier._review_request_already_dispatched`
    (the SAME predicate the inline dispatch + outbox reconcile use), NOT GitHub's
    `reviewDecision` — Mirror emits marker blocks, never a GitHub review, so
    `reviewDecision` is always empty for these PRs and is useless here.
  - Reuses `outbox_notifier._dispatch_mirror_review` verbatim (import-safe: that
    module's executable code is under `if __name__ == '__main__'`). Reusing the
    real builder is deliberate — a hand-rolled copy of the review-task envelope is
    exactly the drift class that produced the sibling dropped-marker bug.
  - Grace, per class: a Forge PR waits DISPATCH_GRACE_MINUTES from its open time so
    the normal inline path gets first crack on a freshly opened PR. A hand/claude
    PR has no inline path, so it waits only HAND_PR_GRACE_MINUTES and gates on its
    LAST COMMIT — a short debounce against the author still pushing, not dead
    latency. This healer only fills genuine gaps either way.
  - Idempotent: the dispatch's own presence check dedups; a per-PR failure ledger
    in the state file dedups the (rare) dispatch-FAILED escalation so one stuck PR
    pages Larry once, not every tick.
  - Kill-switch aware, dry-run env override (default ON — its whole job is to act),
    fail-safe (gh/dispatch errors are logged, NEVER crash the timer).
  - Read-only on GitHub (lists PRs; never merges, closes, comments, or reviews).

Multi-repo (2026-06-22): sweeps both Larry-Yatch/ourliberty-agent-core AND
Larry-Yatch/ourliberty-dashboard (REPOS). The whole pipeline is repo-agnostic —
the routing allowlist + repo_paths in config/agent-models.json cover both, and the
auto-merge path derives its repo from the PR URL — so a labeled PR opened against
EITHER repo auto-flows (a real dashboard PR, #72, has gone through Mirror
auto-merge). This closes the gap where a desktop-opened dashboard PR (#80/#81) was
flagged unrouted but never auto-routed, because this backstop was agent-core-only.
Per-PR task_id for the opt-in path is repo-qualified (`pr-<repo>-<number>`) so a
PR #N present in both repos can't collide onto one review-request dedup key.

CANONICAL task_id RECOVERY (G-rule forge-marker-task-id-mismatch-xii, 2026-07-11):

  A Forge PR's head branch does NOT always round-trip the envelope task_id.
  Three observed shapes defeated the naive `forge/`-strip:
    1. stripped prefix — branch `xii-v1` vs canonical `pulse-check-xii-v1`;
    2. hash-suffixed rename — branch
       `cap-build-flip-readiness-gauge-5-completeness-gate-m-a453` vs canonical
       `flip-readiness-gauge-spec-001` (no string relationship at all);
    3. truncated suffix — branch
       `forge/heal-orphaned-mirror-claim-reinject-not-concluded-` vs canonical
       `heal-orphaned-mirror-claim-reinject-not-concluded-001` (PR #928).
  For shape 3 the naive strip yields a truncated id, so the dedup check misses
  the canonical `review-…-001.json` the inline path already dispatched, and this
  backstop dispatches a SECOND, wrongly-keyed review — a duplicate file in
  Mirror's inbox, a $0.00 cost row under the wrong id, and a wasted review
  concurrency slot. `task_resolution.pr_matches_task` matches only by string, so
  it covers shape 3 but NOT the prefix/rename shapes.

  The authoritative, shape-independent signal is the Forge build outbox itself:
  it records BOTH the canonical task_id AND a `PR opened: <url>` line. So for a
  `forge/*` PR, `canonical_task_id_for_pr` scans the recent Forge build-outbox
  archive and, on a PR-URL match, returns the canonical task_id — recovering the
  correct dedup key no matter how the branch was mangled. Resolution runs BEFORE
  the dedup check so the existing canonical review is found and no duplicate is
  dispatched. When NO archive names the PR (the reaped-outbox #412 shape) the
  resolver falls back to the naive strip — there this backstop is the SOLE
  dispatcher, so a duplicate is impossible. The review envelope's `branch` field
  stays the real head branch (Mirror checks out the actual branch); only the
  task_id is remapped. Opt-in `pr-<repo>-<number>` keying (claude/*,
  auto-review) is already unambiguous and is left untouched.

The dispatch-FAILED escalation is Python-emitted, so it has a registered entry in
config/alert-translations.json under source `heal-undispatched-pr-review` (subject
prefix `undispatched-pr-review`), enforced by the translation-coverage gate
(scripts/tests/test_alert_translations.py).
"""
from __future__ import annotations

import glob
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

import pipeline_live_state  # noqa: E402  # canonical "is a review live?" probe

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-undispatched-pr-review.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-undispatched-pr-review.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-undispatched-pr-review.json'

# Repos this backstop sweeps. The Forge→Mirror→auto-merge pipeline is repo-agnostic
# (routing allowlist + repo_paths in config/agent-models.json cover both; the merge
# path derives its repo from the PR URL), so a labeled PR opened against EITHER repo
# auto-flows. Dashboard coverage closes the gap where a desktop-opened dashboard PR
# (e.g. #80/#81) was flagged unrouted but never auto-routed (this healer was
# agent-core-only). Kept as an explicit pair (mirrors heal_pipeline_stall.REPOS)
# rather than config-derived to keep this backstop dependency-light.
REPOS = (
    'Larry-Yatch/ourliberty-agent-core',
    'Larry-Yatch/ourliberty-dashboard',
)
# Three classes of open PR get auto-routed to a Mirror review:
#   1. Forge build PRs — head branch `forge/<task-id>` (the original #412 gap).
#      Reviewed unconditionally; the proven path.
#   2. Claude Code PRs — head branch `claude/<...>` (the #653 gap). Opened on the
#      laptop and already through the local code-review gate; `claude/` is a
#      reliable Claude-Code-EXCLUSIVE prefix (the droplet agents never use it,
#      unlike `fix/feat/chore`), so it gates safely on its own. Draft-gated. A
#      session-less REVISION on one cold-starts Forge (forge-cold-start-revision).
#   3. Opt-in PRs — any PR carrying the AUTO_REVIEW_LABEL, reviewed when NOT a
#      draft (the #509/#510/#625 gap — PRs Larry opens by hand sat unreviewed).
FORGE_BRANCH_PREFIX = 'forge/'
CLAUDE_BRANCH_PREFIX = 'claude/'  # Claude Code laptop PRs (class 2 above)
# The explicit opt-in marker for class 2. Neither branch prefix nor PR author can
# distinguish Larry's hand-opened PRs from the agent team's: the team commits as
# Larry's own GitHub identity AND uses the same branch prefixes (`fix/`, `feat/`,
# `chore/`, … — see post_merge_verifier.list_recent_merged_agent_prs). So "any
# non-forge branch == human" is FALSE and would auto-merge agent PRs (incl.
# `feat/new-mission-*`, which heal_orphan_autoregister must CLOSE, not merge).
# The label is the only reliable signal: it is applied only on the desktop side
# (whatever opens a PR for Larry tags it; the droplet agents never add it), so a
# labeled PR is unambiguously "cleared for the team." Draft still gates it — a
# draft labeled PR is "still iterating" and is left alone until marked ready.
AUTO_REVIEW_LABEL = 'auto-review'

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
# Grace for hand-spun PRs (class 2/3: `claude/*` and `auto-review`-labeled). These
# have NO inline dispatch path — this backstop is their ONLY route — so the long
# DISPATCH_GRACE_MINUTES (which exists purely to let inline dispatch win the race
# on a Forge PR) buys them nothing but latency. They need only a short debounce
# against the author still pushing, gated on the LAST COMMIT rather than PR-open
# time (see select_orphaned_prs). A re-push past this window is re-reviewed anyway
# (the dedup is head-SHA-aware), so the downside of being slightly eager is one
# re-review cycle, not a wrong merge.
HAND_PR_GRACE_MINUTES = 3
# Ground-truth back-off window (the PR #865 triple-dispatch, 2026-07-08). Any
# review-task record for the task — including the revision-round names the
# exact-name dedup never matches — with an mtime younger than this means the
# review cascade is actively working the PR, so the backstop stands down.
#
# Anchored on DISPATCH time, not verdict time: inbox_watcher archives a review
# task by a plain rename, which preserves the file's original (dispatch-time)
# mtime. So the interval this window must span is dispatch → NEXT-round
# dispatch, which includes the verdict AND the whole Forge revision run before
# the re-review is dispatched — far longer than the ~25-40min dispatch→verdict
# gap. Sized well above a full revision cycle (a REVISION whose Forge run runs
# long, plus routing lag — the very lag this healer exists to tolerate) so a
# healthy-but-slow cascade is never mistaken for an orphan (the #865 shape). A
# genuinely orphaned PR (the #412 shape) has NO review records at all, so a
# generous window delays nothing on the actual rescue path; a cascade that DIES
# mid-cycle is still rescued one window later.
RECENT_REVIEW_ACTIVITY_MINUTES = 180
# Commit-status context the notifier posts every Mirror verdict under. MUST
# stay in sync with outbox_notifier._MIRROR_REVIEW_STATUS_CONTEXT (a test
# asserts the two are equal). A SUCCESS status with this context on a PR's
# CURRENT head is GitHub-truth that the head already has a passing review.
MIRROR_REVIEW_STATUS_CONTEXT = 'mirror-review'
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

def _repo_segment(repo_coords: str) -> str:
    """`owner/name` → `name`. gh + PR URLs use `owner/name`, but the dispatch
    envelope's `target_repo` and config/agent-models.json `repo_paths` keys use the
    bare repo name."""
    return repo_coords.rsplit('/', 1)[-1]


def _fetch_repo_prs(repo: str) -> Optional[list[dict[str, Any]]]:
    """List open PRs for ONE repo via gh. Returns parsed rows (each tagged with its
    `_repo`), or None on a gh failure for that repo (already logged).

    `commits` (→ `lastCommitAt`, the hand-PR debounce signal) is fetched as an
    OPTIONAL extra field. gh rejects an unknown `--json` field with a non-zero
    exit, which would blind the WHOLE sweep — so a failed call is retried once
    WITHOUT `commits`. On a gh too old to know the field, we therefore degrade to
    no `lastCommitAt` (selection falls back to `createdAt`) instead of going dark;
    on a genuine gh outage both attempts fail and we return None as before."""
    base_fields = (
        'number,url,headRefName,createdAt,title,headRefOid,isDraft,labels'
    )
    last_err = ''
    # Preferred projection first; the no-commits fallback handles field-unsupported.
    for fields in (base_fields + ',commits', base_fields):
        cmd = [
            'gh', 'pr', 'list',
            '--repo', repo,
            '--state', 'open',
            '--limit', str(_OPEN_PR_FETCH_LIMIT),
            '--json', fields,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log(f'gh pr list ({repo}) failed: {type(e).__name__}: {e}', 'WARN')
            return None
        if proc.returncode == 0:
            return parse_open_prs(proc.stdout, repo)
        last_err = proc.stderr.strip()[:300]
        # Non-zero: fall through to the no-commits projection once, then give up.
    log(f'gh pr list ({repo}) returned nonzero: {last_err}', 'WARN')
    return None


def fetch_open_prs() -> Optional[list[dict[str, Any]]]:
    """List open PRs across all REPOS via gh. Returns the combined parsed rows
    (each tagged with `_repo`), or None ONLY when every repo's gh call failed — a
    single-repo gh hiccup must not blind the sweep to the other repo, and a healer
    must never crash the timer. A repo with no open PRs contributes [] (still a
    success). Each row carries number, url, headRefName, createdAt, lastCommitAt,
    title, headRefOid, isDraft, labels, _repo."""
    combined: list[dict[str, Any]] = []
    any_ok = False
    for repo in REPOS:
        rows = _fetch_repo_prs(repo)
        if rows is None:
            continue  # this repo errored (already logged); still try the others
        any_ok = True
        combined.extend(rows)
    return combined if any_ok else None


def parse_open_prs(raw_json: str, repo: Optional[str] = None) -> list[dict[str, Any]]:
    """Normalize `gh pr list --json ...` stdout into flat rows, tagging each with
    `_repo` (the owning `owner/name`). Skips malformed entries rather than
    raising."""
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
        # Reduce the commits array to the newest committedDate. ISO-8601 UTC
        # strings (…Z) compare lexicographically in chronological order, so max()
        # is the last commit. Absent/empty/malformed → None (selection falls back
        # to createdAt for the grace check).
        commits = r.get('commits')
        last_commit_at = None
        if isinstance(commits, list):
            dates = [
                c.get('committedDate') for c in commits
                if isinstance(c, dict) and c.get('committedDate')
            ]
            if dates:
                last_commit_at = max(dates)
        out.append({
            'number': number,
            'url': url,
            'headRefName': head,
            'createdAt': created,
            # Newest commit's timestamp (ISO-8601), or None. Gates the hand-PR
            # grace so the debounce tracks the last push, not PR-open time.
            'lastCommitAt': last_commit_at,
            'title': r.get('title') or '',
            # head commit SHA — lets the dedup distinguish "this PR's CURRENT
            # head was reviewed" from "an EARLIER head was reviewed". Tolerated
            # absent (older gh, mocks) → dedup falls back to existence-only.
            'headRefOid': r.get('headRefOid'),
            # Draft state gates the opt-in path (a draft is "still iterating",
            # never auto-routed). Forge PRs ignore it. Absent (older gh / mocks)
            # coerces to False — gh always returns a requested field in practice.
            'isDraft': bool(r.get('isDraft')),
            # Label NAMES (gh returns `labels` as [{name,color,...}]). The
            # `auto-review` label is the opt-in marker for the non-Forge path.
            # Malformed/absent → empty list (no opt-in).
            'labels': [
                lbl.get('name') for lbl in (r.get('labels') or [])
                if isinstance(lbl, dict) and lbl.get('name')
            ],
            # Owning repo (`owner/name`). Drives the dispatch's target_repo and
            # makes the per-PR task_id repo-unique, so PR #N existing in BOTH repos
            # can't collide onto a single `review-…json` dedup key.
            '_repo': repo,
        })
    return out


def head_has_passing_review_status(repo: str, head_sha: str) -> bool:
    """Does this exact head carry a SUCCESS `mirror-review` commit status?

    GitHub-side ground truth the pipeline itself writes:
    `outbox_notifier._post_mirror_review_commit_status` posts one status per
    Mirror verdict (PASS → success; REVISION / ESCALATE / HALT → failure). A
    SUCCESS status on the CURRENT head means this head already PASSED — the PR
    is not orphaned, it is waiting on auto-merge (possibly held on an overlap),
    and re-reviewing it is exactly the harm the PR #865 incident showed (a
    re-review of a PASSed head returned REVISION and left a stale red verdict
    on a merged PR).

    Returns True ONLY on a definitively observed success. Everything else —
    a failure status (an in-flight REVISION round, which the window-bounded
    recency check already covers, and a dead cascade must stay rescuable), no
    status yet, OR any gh error — returns False, so the caller falls through to
    the recency guard rather than permanently exempting the PR from rescue
    (avoiding the fail-quiet 'healer looks healthy while doing nothing'
    anti-pattern). One cheap combined-status call."""
    cmd = [
        'gh', 'api', f'repos/{repo}/commits/{head_sha}/status',
        '--jq', f'[.statuses[] | select(.context=="{MIRROR_REVIEW_STATUS_CONTEXT}") '
                f'| .state]',
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f'gh commit-status ({repo}@{head_sha[:12]}) failed: '
            f'{type(e).__name__}: {e}', 'WARN')
        return False
    if proc.returncode != 0:
        log(f'gh commit-status ({repo}@{head_sha[:12]}) returned '
            f'{proc.returncode}: {proc.stderr.strip()[:200]}', 'WARN')
        return False
    try:
        states = json.loads(proc.stdout or '[]')
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(states, list) and 'success' in states


def recent_review_record(
    review_stem: str,
    mirror_inbox: Path,
    now: datetime,
    window_minutes: int = RECENT_REVIEW_ACTIVITY_MINUTES,
) -> Optional[str]:
    """Reason string when the review pipeline holds a recent record for this
    task that should keep the backstop off; None when the task looks genuinely
    orphaned. `review_stem` is the CANONICAL review filename stem the writer
    produced — `canonical_inbox_name('review-<task>.json')` minus `.json` — so
    a task_id that `sanitize_component` rewrites (e.g. a `/` in the id) is
    matched under its real on-disk name, not the raw form.

    Scans Mirror's inbox + `.archive/` + `.invalid/` for ALL of the task's
    review-record shapes via `outbox_notifier.review_record_name_re` — the
    SAME name grammar the head-aware dedup uses, so the two can't disagree
    about which records exist (the drift that caused #865, where the `-rev<N>`
    round names were invisible to one scanner). `.archive/.lost-result/` is NOT
    descended into (glob is non-recursive): died-verdictless re-dispatch pacing
    is owned by the dedup predicate's debounce+cap, not this time window.

    - a matching file LIVE in the inbox blocks regardless of age (a queued
      review is pending no matter how long it has queued);
    - a matching `.archive/` / `.invalid/` record blocks only while its mtime
      (= dispatch time; archive is a plain rename) is younger than
      `window_minutes`. A future mtime (clock skew) also blocks: conservative.

    Fail-open per directory: an unreadable dir/entry is skipped, so an fs
    hiccup can only make the backstop MORE willing to dispatch (its default
    posture), never wedge it."""
    import outbox_notifier  # grammar owner; already imported by main() by now
    name_re = outbox_notifier.review_record_name_re(review_stem)
    glob_pat = f'{glob.escape(review_stem)}*.json'
    for sub in ('', '.archive', '.invalid'):
        d = mirror_inbox / sub if sub else mirror_inbox
        try:
            entries = d.glob(glob_pat)
        except OSError:
            continue
        for p in entries:
            if not name_re.fullmatch(p.name):
                continue
            if not sub:
                return f'{p.name} still queued in Mirror inbox'
            try:
                mtime = datetime.fromtimestamp(
                    p.stat().st_mtime, tz=timezone.utc,
                )
            except OSError:
                continue
            age_min = (now - mtime).total_seconds() / 60.0
            if age_min <= window_minutes:
                return f'{sub}/{p.name} written {max(0, int(age_min))}m ago'
    return None


def pipeline_backoff_reason(
    pr: dict[str, Any],
    mirror_inbox: Path,
    review_stem: str,
    now: datetime,
) -> Optional[str]:
    """Reason to NOT dispatch a backstop review — the review pipeline already
    owns this PR — or None if it looks genuinely orphaned (the #412 shape).

    Local checks FIRST, gh LAST, so a mid-cascade PR (the common false-orphan)
    backs off with zero gh round-trips:
      1. an active Mirror session for the PR (live claude proc / inbox task);
      2. a recent review record for the task (any round shape, dispatch-time
         window) — catches a healthy cascade whose head has moved since its
         review was dispatched (the exact #865 head-drift the head-keyed dedup
         misses);
      3. a SUCCESS `mirror-review` status on the CURRENT head — a PASS merely
         held/unmerged, which must never be re-reviewed. Failure / absent / gh
         error fall through to a real dispatch, gated by (2)."""
    repo_short = _repo_segment(pr.get('_repo') or '')
    live, live_reason = pipeline_live_state.pr_review_in_progress(
        repo_short, pr['number'],
    )
    if live:
        return f'active review ({live_reason})'
    recent = recent_review_record(review_stem, mirror_inbox, now)
    if recent:
        return f'recent review record — {recent}'
    # gh, last. Only probe with the PR's OWN repo (never guess a fallback: a
    # wrong-repo status query 404s → False anyway, but the guess could also
    # false-match a same-SHA commit in the other repo). Missing repo/head →
    # skip the probe and fall through (recency already had its say).
    repo = pr.get('_repo')
    head_sha = pr.get('headRefOid')
    if repo and isinstance(head_sha, str) and head_sha:
        if head_has_passing_review_status(repo, head_sha):
            return f'passing mirror-review status on {head_sha[:12]}'
    return None


# -------------------- core detection (pure) --------------------

def task_id_for_branch(head_ref: str, pr_number: Optional[int] = None,
                       repo: Optional[str] = None) -> str:
    """Map an open PR's head branch to the task_id used for its Mirror review
    request filename (`review-<task_id>.json`).

    - Forge build branch `forge/<task-id>`: strip the prefix. This is the inverse
      of the worktree/branch convention and matches the task_id the build outbox
      would have carried — so the derived filename lines up with the inline path's
      idempotency key (else the backstop would duplicate the inline review). NOT
      repo-qualified, precisely so it stays equal to the inline path's key.
    - Any other (opt-in) branch: there is no upstream task_id and no inline dispatch
      to collide with, so key by repo + PR number — `pr-<repo-name>-<number>`
      (e.g. `pr-ourliberty-dashboard-80`). Repo-qualified because PR numbers are
      per-repo: agent-core #80 and dashboard #80 must NOT collide onto one
      `review-…json` dedup key (which would suppress one's review or point Mirror
      at the wrong PR). Stable across ticks, unique per PR. Falls back to
      `pr-<number>` (no repo) then the raw branch (no number), defensively."""
    if head_ref.startswith(FORGE_BRANCH_PREFIX):
        return head_ref[len(FORGE_BRANCH_PREFIX):]
    if pr_number is not None:
        if repo:
            return f'pr-{_repo_segment(repo)}-{pr_number}'
        return f'pr-{pr_number}'
    return head_ref


def _normalize_pr_url(url: Optional[str]) -> str:
    """Canonical form for PR-URL equality: trim whitespace + trailing slash and
    lowercase. gh and the outbox `PR opened:` line both emit the same
    `https://github.com/<owner>/<repo>/pull/<N>` shape; lowercasing tolerates any
    incidental host/owner-case difference. Empty/None → ''."""
    return (url or '').strip().rstrip('/').lower()


def canonical_task_id_for_pr(
    pr_url: str,
    fallback_task_id: str,
    now: Optional[datetime] = None,
) -> str:
    """Recover the CANONICAL task_id Forge's build outbox recorded for `pr_url`,
    or `fallback_task_id` when no recent Forge build outbox names this PR.

    A Forge PR's head branch does not always round-trip the task_id (stripped
    prefix / hash-suffixed rename / truncated suffix — see the module docstring),
    so the naive `forge/`-strip fallback can key the review under a WRONG id and
    dispatch a duplicate. The build outbox, by contrast, carries BOTH the
    canonical task_id AND a `PR opened: <url>` line, so a PR-URL match recovers
    the right id regardless of branch shape.

    Scans `<AGENTS_ROOT>/outboxes/forge/.archive/*.json`, bounded to
    `outbox_notifier.RECONCILE_WINDOW_HOURS` (the SAME window the notifier's
    reconcile sweep uses — single source of truth, no drift), and for each Forge
    build outbox (agent==forge, phase==build) compares its extracted PR URL
    (`outbox_notifier._extract_pr_url_from_build_result`, normalized) to `pr_url`.
    First match wins.

    Fail-open, per the healer's never-crash-the-tick contract: an unimportable
    notifier, an unreadable archive dir, or a malformed entry yields
    `fallback_task_id`, never raises — so the worst case is the pre-fix naive
    behavior, never a crashed tick."""
    target = _normalize_pr_url(pr_url)
    if not target:
        return fallback_task_id
    try:
        import outbox_notifier
        window_hours = outbox_notifier.RECONCILE_WINDOW_HOURS
    except Exception as e:  # noqa: BLE001 — resolution must never crash the tick
        log(f'canonical_task_id_for_pr: notifier unusable '
            f'({type(e).__name__}); using fallback', 'WARN')
        return fallback_task_id
    archive_dir = AGENTS_ROOT / 'outboxes' / 'forge' / '.archive'
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_hours * 3600
    try:
        entries = sorted(archive_dir.glob('*.json'))
    except OSError:
        return fallback_task_id
    for outbox_file in entries:
        if outbox_file.name.startswith('.'):
            continue
        try:
            if outbox_file.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            data = json.loads(outbox_file.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get('agent') != 'forge' or data.get('phase') != 'build':
            continue
        task_id = data.get('task_id')
        if not task_id or not isinstance(task_id, str):
            continue
        try:
            outbox_pr = outbox_notifier._extract_pr_url_from_build_result(
                data.get('result', ''))
        except Exception:  # noqa: BLE001 — one bad entry must not abort the scan
            continue
        if outbox_pr and _normalize_pr_url(outbox_pr) == target:
            return task_id
    return fallback_task_id


def _default_resolve_task_id(
    head_ref: str, pr_number: Optional[int], repo: Optional[str],
    pr_url: Optional[str] = None,
) -> str:
    """Pure default resolver for `select_orphaned_prs`: the branch-derived
    task_id, ignoring `pr_url`. `main()` injects the archive-backed
    `resolve_canonical_task_id` in production; tests keep this pure default so the
    selection core stays filesystem-free."""
    return task_id_for_branch(head_ref, pr_number, repo)


def resolve_canonical_task_id(
    head_ref: str, pr_number: Optional[int], repo: Optional[str],
    pr_url: Optional[str],
) -> str:
    """Production resolver injected by `main()`: canonical-resolve `forge/*` PRs
    via the build-outbox URL scan; everything else keeps the pure branch key.

    Only `forge/*` branches can BOTH mis-derive their task_id AND collide with
    the inline path's canonical review, so only they need the archive scan. The
    opt-in `pr-<repo>-<number>` keying (claude/*, auto-review) is already
    unambiguous and has no inline path to collide with — return it unchanged."""
    fallback = task_id_for_branch(head_ref, pr_number, repo)
    if not head_ref.startswith(FORGE_BRANCH_PREFIX) or not pr_url:
        return fallback
    return canonical_task_id_for_pr(pr_url, fallback)


def _is_reviewable_pr(head_ref: str, is_draft: bool, labels: Any) -> bool:
    """Whether an open PR should be auto-routed to a Mirror review.

    - `forge/*` — Forge build PR: always (the proven path; draft state ignored,
      Forge does not draft its build PRs).
    - `claude/*` — Claude Code laptop PR: routed when NOT a draft. `claude/` is a
      reliable Claude-Code-exclusive prefix (the droplet agents never use it), so
      it is a safe gate on its own — unlike `fix/feat/chore`, which both humans
      and agents use. Already code-reviewed locally; a session-less REVISION on
      one cold-starts Forge (forge-cold-start-revision).
    - anything else — routed only when it carries the `auto-review` label AND is
      NOT a draft. The label is the explicit opt-in (the only signal that
      distinguishes a PR cleared for the team from an agent-authored PR — see
      AUTO_REVIEW_LABEL); draft is the "still iterating" safety valve, since a
      Mirror PASS auto-merges and a draft must never be merged out from under its
      author. `labels` is the row's list of label names (empty/None → no opt-in)."""
    if not head_ref:
        return False
    if head_ref.startswith(FORGE_BRANCH_PREFIX):
        return True
    if is_draft:
        return False
    if head_ref.startswith(CLAUDE_BRANCH_PREFIX):
        return True
    return AUTO_REVIEW_LABEL in (labels or [])


def select_orphaned_prs(
    open_prs: list[dict[str, Any]],
    now: datetime,
    already_dispatched: Any,
    resolve_task_id: Any = None,
    grace_minutes: int = DISPATCH_GRACE_MINUTES,
    hand_grace_minutes: int = HAND_PR_GRACE_MINUTES,
) -> list[dict[str, Any]]:
    """Pure selection step. From the open-PR rows, return those that are
    reviewable (Forge build PRs + non-draft `auto-review`-labeled PRs — see
    `_is_reviewable_pr`), past their grace window, and have NO Mirror review
    task yet.

    Grace is PER CLASS, because the window's only purpose is to let the inline
    Forge→Mirror dispatch win the race before this backstop fires:
      - Forge PR (`forge/*`): HAS an inline path, so wait `grace_minutes` from
        the PR's OPEN time (`createdAt`) — unchanged.
      - hand/claude PR (the opt-in path): has NO inline dispatch, so the long
        grace buys nothing. Wait only `hand_grace_minutes` and gate on the LAST
        COMMIT (`lastCommitAt`, fallback `createdAt`) — a short debounce against
        the author still pushing, not a stale PR-open-age proxy.

    `already_dispatched(task_id, head_sha) -> bool` is injected (the production
    caller passes a thin wrapper over
    `outbox_notifier._review_request_already_dispatched`) so this core is
    testable without the notifier or the filesystem. `head_sha` is the PR's
    current head commit; the wrapper treats a review of a DIFFERENT (older) head
    as not-yet-dispatched, so a PR updated after its first review is re-reviewed.

    `resolve_task_id(head_ref, pr_number, repo, pr_url) -> str` is likewise
    injected (default `_default_resolve_task_id`, the pure branch-derived key).
    `main()` passes `resolve_canonical_task_id`, which — for a `forge/*` PR whose
    branch does not round-trip its task_id — recovers the CANONICAL id from the
    build-outbox archive by PR-URL match BEFORE the dedup check runs, so an
    existing canonical review is found and no wrongly-keyed duplicate is
    dispatched. Each returned row is the input row augmented with its derived
    `task_id`.
    """
    if resolve_task_id is None:
        resolve_task_id = _default_resolve_task_id
    forge_cutoff = now - timedelta(minutes=grace_minutes)
    hand_cutoff = now - timedelta(minutes=hand_grace_minutes)
    out: list[dict[str, Any]] = []
    for pr in open_prs:
        head = str(pr.get('headRefName') or '')
        if not _is_reviewable_pr(head, bool(pr.get('isDraft')), pr.get('labels')):
            continue  # not Forge, and not a non-draft auto-review-labeled PR
        if head.startswith(FORGE_BRANCH_PREFIX):
            ts, cutoff = _parse_iso(pr.get('createdAt')), forge_cutoff
        else:
            # Last push if known, else PR-open time; debounced on the short grace.
            ts = _parse_iso(pr.get('lastCommitAt') or pr.get('createdAt'))
            cutoff = hand_cutoff
        if ts is None or ts > cutoff:
            continue  # too fresh — forge: let inline fire; hand: still pushing
        # Resolve BEFORE the dedup check so a mangled forge branch maps to the
        # CANONICAL build-outbox task_id and the existing canonical review is
        # found (no wrongly-keyed duplicate). Non-forge/opt-in PRs pass through
        # the pure branch key. Fail-open: a raising resolver falls back to the
        # branch key rather than dropping the PR from the sweep.
        try:
            task_id = resolve_task_id(head, pr.get('number'), pr.get('_repo'),
                                      pr.get('url'))
        except Exception as e:  # noqa: BLE001 — never let resolution crash selection
            log(f'resolve_task_id raised for branch={head}: '
                f'{type(e).__name__}: {e}; using branch-derived key', 'WARN')
            task_id = task_id_for_branch(head, pr.get('number'), pr.get('_repo'))
        if not task_id:
            continue  # degenerate branch → no dispatchable task_id
        # Died-verdictless coverage note (post-#850 read-only review checkout):
        # pre-#850, the [WIP][session-start] push moved the PR head, so this
        # head-aware check accidentally re-dispatched a review whose session
        # died without a verdict. That recovery is now deliberate and lives in
        # the shared predicate: a run whose outbox could not be persisted is
        # archived under `.archive/.lost-result/` (inbox_watcher's positive
        # marker), and _review_request_already_dispatched treats a same-head
        # lost-result envelope as re-dispatchable (debounced + capped) — see
        # its docstring for the full coverage map of the other death shapes.
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
    GitHub PR row. There is no build outbox to read here — for a Forge PR the reap
    prevented it; for a human PR one never existed — so we reconstruct from GitHub
    truth: task_id (from branch / PR number), branch, target_repo (the PR's OWN
    repo, so a dashboard PR routes a dashboard review — the pipeline is repo-agnostic
    and Mirror's worktree gate has a path for both), pr_title. No `claude_session_id`
    is available (no live build session), so a downstream REVIEW_REVISION starts
    Forge fresh rather than --resume; the correct behavior for a recovered PR."""
    return {
        'task_id': pr['task_id'],
        'target_repo': (
            _repo_segment(pr['_repo']) if pr.get('_repo')
            else 'ourliberty-agent-core'
        ),
        'branch': pr['headRefName'],
        'pr_title': pr.get('title') or '',
        'dispatched_by': 'heal-undispatched-pr-review',
        # Thread the PR head we already have from gh-pr-list so the dispatch
        # records the right commit without a second gh round-trip (and the
        # round-0 dedup keys on this exact head).
        'head_sha': pr.get('headRefOid'),
    }


def emit_failed_alert(pr: dict[str, Any]) -> bool:
    """Page Larry that an orphaned PR's review dispatch did not take. Per-PR dedup
    is the caller's job (state ledger); larry_alerts adds a (source,subject)
    cooldown. Never raises."""
    number = pr['number']
    url = pr['url']
    repo = pr.get('_repo') or 'Larry-Yatch/ourliberty-agent-core'
    # Repo-qualify the subject so the (source,subject) cooldown can't make an
    # agent-core PR #N suppress a dashboard PR #N alert (PR numbers are per-repo).
    # SUBJECT_PREFIX is unchanged, so the alert-translation coverage gate still
    # matches on it.
    subject = f'{SUBJECT_PREFIX}:{_repo_segment(repo)}:{number}'
    message = (
        f'PR #{number} ({repo}) is open with no Mirror review, and the backstop '
        f'review dispatch did not take (review task still absent after dispatch). '
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
        f'~/agents/inboxes/mirror/, or `gh pr view {number} --repo {repo}`.'
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


def _review_task_in_claimed(review_filename: str, mirror_inbox: Path) -> bool:
    """True iff `review_filename` sits in ANY `<mirror_inbox>/.claimed/<slot>/`.

    Closes the dispatch→claim race that produced a false-positive critical page
    (G-rule heal-undispatched-pr-review-claimed-race-fp-001, PRs #903/#905/#910):
    the backstop writes the Mirror review task, inbox_watcher relocates it to
    `.claimed/<slot>/<name>.json` within ~2-3s, and the shared presence predicate
    (`outbox_notifier._review_request_already_dispatched`) — which only scans the
    inbox root, `.archive/`, and `.invalid/` — then reads the task as absent and
    pages "dispatch did not take" even though it plainly did. A `.claimed/` hit is
    positive proof the dispatch landed (a task can only be claimed if it was first
    written), so this can never mask a genuine failure.

    Fully defensive: a missing `.claimed` dir, an unreadable slot, or any OSError
    yields False and never raises — this is a timer-driven healer and must never
    crash a tick. Performs no writes. Slot dirs (not the filename) are globbed, so
    a task_id carrying glob metacharacters can't distort the match."""
    try:
        claimed_root = mirror_inbox / '.claimed'
        for slot in claimed_root.glob('*'):
            try:
                if (slot / review_filename).exists():
                    return True
            except OSError:
                continue
    except OSError:
        return False
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

    # gh-api-burn phase 1: back off before the `gh pr list` scan below when the
    # shared GraphQL budget is low. Fail-open (unknown -> proceed); a broken guard
    # never wedges the healer.
    try:
        import gh_budget
        if gh_budget.should_skip('heal_undispatched_pr_review', log=log):
            return 0
    except Exception as e:  # noqa: BLE001
        log(f'gh_budget guard unavailable ({type(e).__name__}); proceeding')

    open_prs = fetch_open_prs()
    if open_prs is None:
        log('gh unavailable this tick; no scan')
        return 0

    now = datetime.now(timezone.utc)
    state = load_state()
    failed = state['failed_prs']

    orphaned = select_orphaned_prs(
        open_prs, now, _already_dispatched, resolve_canonical_task_id)
    log(f'scanned {len(open_prs)} open PR(s); {len(orphaned)} reviewable PR(s) '
        f'(Forge + non-draft auto-review) past grace with no Mirror review')

    mirror_inbox = safe_write_inbox.INBOXES_ROOT / 'mirror'
    dry_run = not healer_enabled()
    dispatched = 0
    for pr in orphaned:
        url = str(pr['url']).rstrip('/')

        # Ground-truth back-off (the PR #865 triple-dispatch, 2026-07-08).
        # Between a review's dispatch and its verdict the PR head keeps moving
        # (build + revision pushes), so the head-keyed dedup in select_orphaned
        # reports "current head never reviewed" for long stretches of a
        # perfectly healthy review cascade — this healer fired three times in
        # 40min for ONE task, the third re-reviewing a head that already carried
        # a PASS status and overwriting the merged PR's findings comment with a
        # stale REVISION. `pipeline_backoff_reason` consults the records the
        # pipeline itself writes (active session, recent review record of any
        # round shape, a PASS commit status on the current head) so a
        # mid-cascade PR is left alone. Evaluated BEFORE the dry-run branch so a
        # dry-run tick reports exactly what a live tick would do (no phantom
        # "would dispatch" for a PR the guards protect). Local checks run before
        # any gh call, so the common false-orphan skips with zero round-trips.
        review_stem = safe_write_inbox.canonical_inbox_name(
            f'review-{pr["task_id"]}.json'
        )
        review_stem = (
            review_stem[:-len('.json')]
            if review_stem.endswith('.json') else review_stem
        )
        backoff = pipeline_backoff_reason(pr, mirror_inbox, review_stem, now)
        if backoff:
            log(f'PIPELINE_BACKOFF PR #{pr["number"]} task={pr["task_id"]} '
                f'pr={url} — {backoff}; review pipeline owns this PR, not '
                f'dispatching', 'INFO')
            continue

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
        # HEAD-AWARE on purpose: with no head, the stale archived envelope
        # that made this PR selectable in the first place would itself satisfy
        # an existence-only check — recording a FAILED dispatch as success and
        # permanently suppressing the escalation below. A dispatch that
        # actually landed passes regardless (live inbox file blocks any head).
        #
        # ...OR the task is already in a `.claimed/<slot>/` slot: inbox_watcher
        # relocates a claimed review to `<mirror_inbox>/.claimed/<slot>/<name>`
        # within ~2-3s of dispatch, and `_already_dispatched` (inbox root /
        # `.archive/` / `.invalid/` only) does not look there — so during the
        # dispatch→claim race a landed dispatch reads as absent and pages a
        # spurious critical alert (G-rule heal-undispatched-pr-review-claimed-
        # race-fp-001). A `.claimed/` hit is positive proof the dispatch landed.
        review_filename = safe_write_inbox.canonical_inbox_name(
            f'review-{pr["task_id"]}.json')
        mirror_inbox = safe_write_inbox.INBOXES_ROOT / 'mirror'
        if (_already_dispatched(pr['task_id'], pr.get('headRefOid'))
                or _review_task_in_claimed(review_filename, mirror_inbox)):
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
