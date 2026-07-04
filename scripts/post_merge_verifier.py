#!/usr/bin/env python3
"""
post_merge_verifier.py — Automated production verification of merged agent PRs.

Runs every 20 min via systemd timer. Silent unless it dispatches or finds a
regression. For each merged agent PR in the last LOOKBACK_HOURS that has
  - a linked issue (auto-close reference),
  - not yet been dispatched for verification,
it writes a focused Playwright verification task to Luma's inbox. The task
instructs Luma to:
  1. Read the original issue's acceptance criteria
  2. Write a small Playwright spec targeting production (PRODUCTION_URL env)
  3. Run it against the live deploy, report per-criterion pass/fail
  4. NOT modify any code — just verify and report

When Luma's result comes back in her outbox, a companion function reads the
structured VERIFICATION RESULT block and records pass/fail in
blackboard/post-merge-verifications.json.

Regression detection: on fail, we re-open the closed issue with a comment
pointing at the verification detail and add the `regression` label. Sage's
next pickup routes it back through the dispatch chain.

Part of pipeline audit 2026-04-15 Phase 6a.
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from atomic_io import atomic_write_json  # noqa: E402
from test_isolation_guard import gh_write  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT') or '/home/larry/agents')
STATE_FILE = AGENTS_ROOT / 'blackboard' / 'post-merge-verifications.json'
LOG_FILE = AGENTS_ROOT / 'blackboard' / 'post-merge-verifier.log'
# TODO(Larry): re-target inbox to whichever agent verifies merges in Phase D.
INBOX_MAIN = AGENTS_ROOT / 'inboxes' / 'forge'
OUTBOX_ARCHIVE = AGENTS_ROOT / 'outboxes' / 'archive'
# Configurable: which GitHub repo to verify merges against, where the live
# product runs, and (optionally) which Telegram chat receives briefings.
REPO = os.environ.get('OURLIBERTY_VERIFY_REPO', 'Larry-Yatch/ourliberty-agent-core')
PRODUCTION_URL = os.environ.get('OURLIBERTY_PRODUCTION_URL', 'https://example.invalid')
BRIEFING_CHAT_ENV = os.environ.get('OURLIBERTY_BRIEFING_CHAT', '0')
try:
    BRIEFING_CHAT = int(BRIEFING_CHAT_ENV)
except ValueError:
    BRIEFING_CHAT = 0
PR_ROUTING_FILE = AGENTS_ROOT / 'blackboard' / 'pr-routing.json'
SHIP_TRACKING_DIR = AGENTS_ROOT / 'shared' / 'ship-tracking'
GH_TIMEOUT = 20

LOOKBACK_HOURS = 24
MAX_DISPATCHES_PER_RUN = 5  # Don't flood Luma with verifications in one run
MAX_VERIFY_RETRIES = 2

# Regex that finds the structured VERIFICATION RESULT block in Luma's output
VERIFY_RESULT_RE = re.compile(
    r'VERIFICATION\s+RESULT\s*[:\-]?\s*(pass|fail|inconclusive)',
    re.IGNORECASE,
)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        atomic_write_json(STATE_FILE, state, indent=2)
    except OSError as e:
        log(f'WARN: failed to save state: {e}')


def gh_json(*args: str, default=None):
    try:
        result = subprocess.run(
            ['gh', *args],
            capture_output=True, text=True, timeout=GH_TIMEOUT,
            env={**os.environ, 'PATH': '/usr/bin:/usr/local/bin'},
        )
        if result.returncode != 0:
            return default
        out = result.stdout.strip()
        if not out:
            return default
        return json.loads(out)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return default


def gh_plain(*args: str) -> str | None:
    """Run gh for a command that returns plain text (e.g. issue comment/reopen).

    Logs stderr on non-zero exit so silent breakages (auth, rate limit, 404) become
    visible in the post-merge-verifier log. Before 2026-04-20 this returned None
    silently and a whole class of regression-reopen gaps went undetected.
    """
    try:
        result = gh_write(
            ['gh', *args],
            capture_output=True, text=True, timeout=GH_TIMEOUT,
            env={**os.environ, 'PATH': '/usr/bin:/usr/local/bin'},
        )
        if result.returncode != 0:
            err = (result.stderr or '').strip().replace('\n', ' | ')[:300]
            log(f'WARN: gh {" ".join(args[:3])}... failed rc={result.returncode} stderr={err!r}')
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        log(f'WARN: gh {" ".join(args[:3])}... timed out after {GH_TIMEOUT}s')
        return None
    except FileNotFoundError:
        log('FATAL: gh CLI not found on PATH')
        return None


def extract_feature(title: str) -> str:
    """Pull feature slug from PR title like 'enhance(concierge): ...'. Fallback 'uncategorized'."""
    m = re.search(r'^\s*(?:enhance|fix|feat|polish|chore|docs|refactor)\(([\w-]+)\)\s*:', title or '', re.IGNORECASE)
    return m.group(1).lower() if m else 'uncategorized'


def list_recent_merged_agent_prs() -> list[dict]:
    """Merged PRs in the last LOOKBACK_HOURS authored by an agent branch prefix."""
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    prs = gh_json(
        'pr', 'list',
        '--repo', REPO,
        '--state', 'merged',
        '--search', f'merged:>={since}',
        '--limit', '50',
        '--json', 'number,title,headRefName,mergedAt,closingIssuesReferences',
    ) or []

    agent_prefixes = ('beacon/', 'forge/', 'mirror/', 'pulse/', 'feat/', 'fix/', 'chore/', 'polish/', 'refactor/', 'enhance/', 'ship/', 'hotfix/')
    return [p for p in prs if any((p.get('headRefName') or '').startswith(prefix) for prefix in agent_prefixes)]


def _tracking_matches(track_file, tracking: dict, pr_num: int, feature: str) -> bool:
    """True if a ship-tracking file corresponds to this PR/feature (audit #44).

    The upstream /ship writer is not in this fork, so the on-disk schema isn't
    pinned here; match on any of the plausible identifiers so a real tracking
    file is recognized while an unrelated one is skipped (falling through to
    the briefing fallback). A bare PR number / feature slug is required — a file
    with neither is treated as non-matching rather than a wildcard.
    """
    if not isinstance(tracking, dict):
        return False
    # PR number under any of the conventional keys.
    for key in ('pr_num', 'pr_number', 'pr'):
        val = tracking.get(key)
        if val is not None:
            try:
                if int(val) == int(pr_num):
                    return True
            except (TypeError, ValueError):
                pass
    # Feature slug, via an explicit field or the filename stem.
    if feature and feature != 'uncategorized':
        if str(tracking.get('feature') or '').lower() == feature.lower():
            return True
        try:
            if track_file.stem.lower() == feature.lower():
                return True
        except AttributeError:
            pass
    return False


def find_originating_chat(pr_num: int, feature: str) -> int:
    """Look up the originating chat_id for a PR/feature.

    3-source lookup (most specific to least):
    1. pr-routing.json — maps PR numbers to chat IDs (set during /ship dispatch)
    2. ship-tracking files — contain originating_chat_id for /ship features
    3. Briefing fallback — only if both lookups fail
    """
    # Source 1: pr-routing.json
    try:
        if PR_ROUTING_FILE.exists():
            with open(PR_ROUTING_FILE) as f:
                routing = json.load(f)
            chat = routing.get(str(pr_num), {}).get('chat_id')
            if chat:
                return int(chat)
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    # Source 2: ship-tracking files (scan for matching feature)
    #
    # Audit #44: the loop used to return the FIRST ship-tracking file that had
    # any originating_chat_id, ignoring `feature`/`pr_num` entirely — so with
    # two concurrent /ship features it could deliver the verification briefing
    # to the wrong operator chat. Require the tracking file to actually
    # correspond to this PR/feature before trusting its chat_id; otherwise skip
    # it and fall through to the briefing fallback (the documented safe
    # default). Matching on any of the plausible identifiers keeps this robust
    # to the tracking-file schema (the /ship writer lives upstream and is not
    # in this fork, so ship-tracking is currently vestigial here — this is a
    # fail-safe tightening that can only no-op to the fallback, never mis-route).
    try:
        if SHIP_TRACKING_DIR.exists():
            for track_file in sorted(SHIP_TRACKING_DIR.glob('*.json'), reverse=True):
                with open(track_file) as f:
                    tracking = json.load(f)
                if not _tracking_matches(track_file, tracking, pr_num, feature):
                    continue
                chat = tracking.get('originating_chat_id')
                if chat:
                    return int(chat)
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    # Source 3: fallback
    log(f'ROUTING_WARN: no originating chat found for PR #{pr_num} / {feature} — falling back to briefing')
    return BRIEFING_CHAT


def dispatch_verification(pr_num: int, pr_title: str, issue_num: int, feature: str, attempt: int) -> bool:
    """Write a verification task to Luma's inbox. Returns True on success."""
    test_filename = f'verify-pr-{pr_num}.spec.ts'
    prompt = f'''🔍 VERIFICATION TASK — PR #{pr_num} just merged.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🛑 ABSOLUTE RULES — READ THIS FIRST, VIOLATIONS FAIL THE TASK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This is a READ-ONLY verification. You are FORBIDDEN from taking any of these actions — running any of them is a task failure regardless of result:

  🚫 `git add` / `git commit` — do not stage or commit anything
  🚫 `git push` — do not push any branch to origin
  🚫 `git checkout -b verify/pr-*` — do not create a verify branch
  🚫 `gh pr create` — do not open a pull request of any kind
  🚫 `gh pr ready` / `gh pr merge` — do not touch PR state
  🚫 Writing any file anywhere under the repo that persists after you exit

The ONLY thing you output to GitHub is handled automatically by the
post_merge_verifier — not you. "verify/pr-*" PRs were retired 2026-04-20 and
generating one is explicitly noise the founder has rejected twice.

If you feel tempted to "capture" your verification as a PR "for visibility":
DON'T. The structured VERIFICATION RESULT block in step 4 IS the artifact. It
is parsed by post_merge_verifier.py, written to
blackboard/post-merge-verifications.json, and — on fail — triggers automatic
issue reopen + regression label. Opening a PR does nothing the pipeline
doesn't already do, and creates cleanup work for the founder.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**What's being verified**: Issue #{issue_num}'s acceptance criteria, against the LIVE PRODUCTION deploy at {PRODUCTION_URL}.

**Where to work**: Write your scratch spec file under `/tmp/verify-pr-{pr_num}-{int(time.time())}.spec.ts` (NOT inside the repo — `/tmp` guarantees it can't be accidentally committed). Point Playwright at that file via `--config` or an absolute path. You can reference fixtures from the repo read-only; you just cannot add files inside it.

**Steps**:

1. `gh issue view {issue_num} --repo {REPO}` — read the full spec. Extract every acceptance criterion (lines starting with `- [ ]` or `- [x]`, or numbered "must" / "should" requirements).

**1.5. Agent Browser exploration (hybrid mode — Phase D):**
   Before writing any Playwright spec, do a quick exploratory walkthrough using Agent Browser
   to understand the live state of the affected pages. This informs which assertions the
   Playwright spec should include.
   ```
   agent-browser open {PRODUCTION_URL}/<affected-page> --args --no-sandbox
   agent-browser snapshot -i --json
   ```
   Review the accessibility snapshot to understand:
   - What interactive elements exist on the page
   - What text/headings are visible (inform assertion targets)
   - Whether the page loaded correctly or has errors
   Use these observations to write more precise, targeted Playwright assertions in step 2.
   This exploration is quick (seconds) and dramatically improves spec quality.

2. Write a Playwright spec to a TEMPORARY path under `/tmp/` (see "Where to work" above) that:
   - Targets production at `{PRODUCTION_URL}` (NOT localhost).
   - Has one `test()` block per mechanically-testable acceptance criterion.
   - Uses test credentials from `.env.local` (`TEST_USER_EMAIL`, `TEST_USER_PASSWORD`) if auth is needed. If `.env.local` is missing, use the email/password from the Vercel env (same names).
   - Skips criteria that can't be tested mechanically (e.g. subjective ones like "feels premium"). Note these explicitly.
   - **Informed by the Agent Browser exploration** — use the exact element text, headings, and interactive refs you observed in step 1.5 to write precise selectors and assertions.
   - ⛔ DO NOT write the spec inside `__tests__/` or anywhere in the repo tree.

3. Run it from the repo root (so node_modules resolve) but against the /tmp path:
   ```
   pnpm exec playwright test /tmp/verify-pr-{pr_num}-*.spec.ts --project=chromium
   ```

4. Report the result in a structured block. This format is mandatory — a
   downstream script parses it:

   ```
   VERIFICATION RESULT: pass | fail | inconclusive

   Criteria:
     - AC1 [status]: <criterion text> — <detail>
     - AC2 [status]: <criterion text> — <detail>
     ...

   Test file: /tmp/verify-pr-{pr_num}-*.spec.ts (temporary)
   ```

5. **STOP. You are done.**
   - Do not `git status` to "check what you touched" — you didn't touch the repo.
   - Do not commit, branch, push, or PR anything (see absolute rules above).
   - Regression handling is automatic — the verifier reads your VERIFICATION RESULT
     block from this task's output and handles issue reopen + labeling itself.
   - Delete `/tmp/verify-pr-{pr_num}-*.spec.ts` if you like, or leave it (cron sweeps /tmp).

**Attempt**: {attempt}/{MAX_VERIFY_RETRIES + 1}. If this attempt crashes, you'll be re-dispatched.

**Why this matters**: "merged" ≠ "works for users". The Academy RLS recursion was merged and broken simultaneously for hours. This verification closes that gap automatically — WITHOUT creating a PR.
'''

    task = {
        'prompt': prompt,
        # TODO(Larry): wire up dispatcher source in Phase D. Default to 'beacon'
        # since Larry's beacon plays the dispatch/spec role pre-compass.
        'source': 'beacon',
        'reply_chat_id': find_originating_chat(pr_num, feature),
        'issue_number': issue_num,
        'verification_for_pr': pr_num,
        'verification_attempt': attempt,
        'timeout': 5400,  # 90 min — includes Playwright run time
    }

    INBOX_MAIN.mkdir(parents=True, exist_ok=True)
    task_file = INBOX_MAIN / f'verify-pr-{pr_num}-attempt{attempt}-{int(time.time())}.json'
    try:
        atomic_write_json(task_file, task, indent=2)
    except OSError as e:
        log(f'  #{pr_num}: failed to write verification task: {e}')
        return False

    log(f'  #{pr_num} → dispatched verification (attempt {attempt}, issue #{issue_num}, feature={feature})')
    return True


def parse_verification_result_from_outbox(pr_num: int) -> tuple[str, str] | None:
    """Look in archived outbox for this PR's verification result.

    Returns (result, full_output) or None if not yet available.
    """
    if not OUTBOX_ARCHIVE.exists():
        return None
    for result_file in OUTBOX_ARCHIVE.glob(f'verify-pr-{pr_num}-*-result.json'):
        try:
            with open(result_file) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not data.get('success'):
            continue
        output = data.get('output') or ''
        m = VERIFY_RESULT_RE.search(output)
        if m:
            return (m.group(1).lower(), output)
    return None


def signal_verification_outcome(pr_num: int, issue_num: int, result: str, output: str) -> None:
    """Post verification outcome to the origin issue. On fail, also reopen + label.

    Replaces the PR-artifact flow (2026-04-20). The origin issue is now the
    permanent record for every verification — pass, fail, or inconclusive.

    - pass: ✅ comment (issue stays closed).
    - fail: reopen issue + add `regression` label + 🔴 detail comment.
    - inconclusive: ⚠️ comment (issue unchanged; human spot-check).
    """
    excerpt = output[:1500]
    if result == 'pass':
        comment = (
            f'✅ **Verification passed** — PR #{pr_num} meets acceptance criteria in production.\n\n'
            f'```\n{excerpt}\n```\n\n'
            f'_Automated by post\\_merge\\_verifier.py._'
        )
        gh_plain('issue', 'comment', str(issue_num), '--repo', REPO, '--body', comment)
        log(f'  #{pr_num}: ✅ posted pass signal to issue #{issue_num}')
    elif result == 'fail':
        comment = (
            f'🔴 **Verification failed after merge** — PR #{pr_num} did not meet the acceptance criteria '
            f'when tested in production.\n\n'
            f'```\n{excerpt}\n```\n\n'
            f'_Automated by post\\_merge\\_verifier.py. '
            f'Reopening and adding `regression` label so Sage will re-dispatch._'
        )
        gh_plain('issue', 'reopen', str(issue_num), '--repo', REPO)
        gh_plain('issue', 'comment', str(issue_num), '--repo', REPO, '--body', comment)
        gh_plain('issue', 'edit', str(issue_num), '--repo', REPO, '--add-label', 'regression')
        log(f'  #{pr_num}: ⚠️ regression — reopened issue #{issue_num} with label `regression`')
    else:  # inconclusive
        comment = (
            f'⚠️ **Verification inconclusive** — PR #{pr_num} could not be mechanically verified '
            f'against acceptance criteria in production.\n\n'
            f'```\n{excerpt}\n```\n\n'
            f'_Automated by post\\_merge\\_verifier.py. Human spot-check recommended._'
        )
        gh_plain('issue', 'comment', str(issue_num), '--repo', REPO, '--body', comment)
        log(f'  #{pr_num}: ⚠️ posted inconclusive signal to issue #{issue_num}')


# Back-compat alias for any external caller that imports the old name.
reopen_with_regression = lambda pr, iss, out: signal_verification_outcome(pr, iss, 'fail', out)


def main() -> int:
    log('Starting post-merge verifier run')
    state = load_state()
    prs = list_recent_merged_agent_prs()
    if not prs:
        log('No recently merged agent PRs')
        return 0

    log(f'Found {len(prs)} merged agent PR(s) in last {LOOKBACK_HOURS}h')

    dispatches = 0

    for pr in prs:
        pr_num = pr.get('number')
        title = pr.get('title') or ''
        feature = extract_feature(title)
        refs = pr.get('closingIssuesReferences') or []
        if not refs:
            # No closing issue — can't verify against a spec
            log(f'  #{pr_num}: skip (no closing issue reference)')
            continue
        issue_num = refs[0].get('number')
        if not issue_num:
            continue

        pr_key = str(pr_num)
        entry = state.get(pr_key, {})

        # Step 1: if we've dispatched a verification but haven't recorded the result, look for it
        if entry.get('dispatched_at') and not entry.get('result'):
            parsed = parse_verification_result_from_outbox(pr_num)
            if parsed:
                result, output = parsed
                entry['result'] = result
                entry['result_at'] = datetime.now(timezone.utc).isoformat()
                entry['output_excerpt'] = output[:2000]

                signal_verification_outcome(pr_num, issue_num, result, output)

                state[pr_key] = entry
                continue

            # No result yet — if it's been a while, consider retry
            try:
                dispatched_at = datetime.fromisoformat(entry['dispatched_at'].replace('Z', '+00:00'))
                age_hours = (datetime.now(timezone.utc) - dispatched_at).total_seconds() / 3600
            except (ValueError, KeyError):
                age_hours = 0

            attempts = int(entry.get('attempts', 1))
            if age_hours > 2 and attempts <= MAX_VERIFY_RETRIES:
                # Luma probably crashed — retry
                if dispatches >= MAX_DISPATCHES_PER_RUN:
                    log(f'  #{pr_num}: would retry but already at MAX_DISPATCHES_PER_RUN')
                    continue
                if dispatch_verification(pr_num, title, issue_num, feature, attempts + 1):
                    entry['attempts'] = attempts + 1
                    entry['dispatched_at'] = datetime.now(timezone.utc).isoformat()
                    state[pr_key] = entry
                    dispatches += 1
                continue

            # Stuck detector: attempts exhausted AND still no parseable result AND dispatched
            # long enough ago that Luma should have finished. Post an inconclusive signal to
            # the origin issue so nothing drops silently (fixes the PR 1800 gap on 2026-04-20
            # where issue #1799 FAILED but never got reopened because the result-block parse
            # never succeeded).
            if attempts > MAX_VERIFY_RETRIES and age_hours > 2 and not entry.get('stuck_signaled'):
                excerpt = (
                    f'Luma dispatched {attempts} time(s) without returning a parseable '
                    f'VERIFICATION RESULT block. Oldest dispatch: {age_hours:.1f}h ago. '
                    f'Typically means the spec crashed, timed out, or the structured-result '
                    f'format drifted. Check outbox for verify-pr-{pr_num}-*.json.'
                )
                signal_verification_outcome(pr_num, issue_num, 'inconclusive', excerpt)
                entry['stuck_signaled'] = True
                entry['stuck_at'] = datetime.now(timezone.utc).isoformat()
                state[pr_key] = entry
                continue
            log(f'  #{pr_num}: verification in flight ({age_hours:.1f}h), no result yet')
            continue

        # Step 2: never dispatched — do it now
        if not entry.get('dispatched_at'):
            if dispatches >= MAX_DISPATCHES_PER_RUN:
                log(f'  #{pr_num}: queued for next run (MAX_DISPATCHES_PER_RUN reached)')
                continue
            if dispatch_verification(pr_num, title, issue_num, feature, attempt=1):
                entry = {
                    'pr_number': pr_num,
                    'issue_number': issue_num,
                    'feature': feature,
                    'title': title[:100],
                    'dispatched_at': datetime.now(timezone.utc).isoformat(),
                    'attempts': 1,
                }
                state[pr_key] = entry
                dispatches += 1

        # Step 3: entry.result already set → done, move on

    save_state(state)
    log(f'Done. {dispatches} verification(s) dispatched this run.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f'FATAL: {e}')
        sys.exit(1)
