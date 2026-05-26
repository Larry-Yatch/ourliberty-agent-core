# AUTO_MERGE serializer queue — runbook

D3.5 5d-prime (2026-05-26). The serializer holds a PR's auto-merge when
another open PR in the same repo touches overlapping files, and skips
the merge entirely when GitHub reports CONFLICTING. Lives in
`scripts/outbox_notifier.py`; queue state file at
`~/agents/state/auto-merge-queue.json`. The E1.3 healer
(`scripts/heal_pr_auto_merge.py`) remains the post-hoc safety net for
anything that bypasses both gates.

## Inspect the queue

```bash
cat ~/agents/state/auto-merge-queue.json | jq
```

Empty / missing file = nothing held. Each entry:

```json
{
  "pr_number": 112,
  "task_id": "build-...",
  "repo": "Larry-Yatch/ourliberty-agent-core",
  "pr_url": "https://github.com/Larry-Yatch/ourliberty-agent-core/pull/112",
  "changed_files": ["docs/operating-manual.md", "scripts/foo.py"],
  "queued_at": "2026-05-26T13:53:19+00:00",
  "blocker_pr_number": 109,
  "watchdog_dm_sent": false,
  "unknown_attempts": 0,
  "reply_chat_id": 7998341473,
  "summary": "..."
}
```

- `blocker_pr_number`: the PR this entry is waiting on. Null when the
  entry is in UNKNOWN-defer state (gate 2 returned UNKNOWN; retrying on
  the next sweep).
- `watchdog_dm_sent`: true once the 24h-stale DM has fired for this
  entry. Prevents re-DM.
- `unknown_attempts`: count of UNKNOWN mergeable replies. On the second
  attempt, the gate proceeds with the merge (let git be the authority).

## Watch the queue from logs

```bash
grep -E 'AUTO_MERGE_(HELD|SKIPPED_CONFLICTING|DEFERRED_UNKNOWN|QUEUE_)' \
  ~/agents/logs/outbox-notifier.log | tail -20
```

Key log lines:
- `AUTO_MERGE_HELD task=X pr=URL blocker=#Y` — gate 1 fired.
- `AUTO_MERGE_SKIPPED_CONFLICTING task=X pr=URL` — gate 2 fired; Larry
  got a rebase DM.
- `AUTO_MERGE_DEFERRED_UNKNOWN task=X pr=URL` — gate 2 returned UNKNOWN;
  retry on next sweep.
- `AUTO_MERGE_QUEUE_RELEASE blocker=#Y releasing N entries` — post-merge
  release pass fired.
- `AUTO_MERGE_QUEUE_RELEASED pr=URL task=X outcome=Z` — per-entry
  release outcome.
- `AUTO_MERGE_QUEUE_UNKNOWN_RETRY pr=URL ... outcome=Z` — sweep retry of
  a deferred-UNKNOWN entry.
- `AUTO_MERGE_HELD_FAIL_CLOSED ...` — queue file corrupt; daemon
  refused the merge.

## Manually release a stuck entry

If a queued PR is stuck (e.g. blocker is taking forever) and you want
to merge anyway:

```bash
# 1. Inspect the entry.
cat ~/agents/state/auto-merge-queue.json | jq '.queue[] | select(.pr_number == 112)'

# 2. Verify the PR is genuinely mergeable now.
gh pr view 112 --repo Larry-Yatch/ourliberty-agent-core \
  --json mergeable,mergeStateStatus

# 3. Merge manually (the canonical AUTO_MERGE command).
gh pr merge 112 --repo Larry-Yatch/ourliberty-agent-core \
  --squash --delete-branch

# 4. Remove the queue entry. The next sweep will also clean it up when
#    it sees the PR is MERGED, but doing it now closes the loop cleanly.
jq '.queue |= map(select(.pr_number != 112))' \
  ~/agents/state/auto-merge-queue.json > /tmp/q.json && \
  mv /tmp/q.json ~/agents/state/auto-merge-queue.json
```

Step 4 is safe to skip — the next sweep tick (5s) detects the blocker
PR as MERGED and runs the release pass, which drains entries that were
waiting on it. Editing the file by hand is the faster path; it'll be
overwritten atomically on the next queue mutation.

## Recover from a corrupt queue

If `auto-merge-queue.json` fails to parse, the notifier flips into
**fail-closed mode** and refuses ALL subsequent auto-merges until the
daemon restarts. You'll see in the log:

```
AUTO_MERGE_QUEUE_CORRUPT at /home/larry/agents/state/auto-merge-queue.json:
  JSONDecodeError: ...; refusing all subsequent AUTO_MERGE attempts
  until daemon restart
```

…and Larry will receive a broadcast critical alert. Recovery:

```bash
# 1. Inspect the broken file.
cat ~/agents/state/auto-merge-queue.json

# 2. Move it out of the way (preserve for inspection).
mv ~/agents/state/auto-merge-queue.json \
   ~/agents/state/auto-merge-queue.json.broken-$(date +%s)

# 3. Restart the notifier (systemd unit name may differ — adjust).
sudo systemctl restart outbox-notifier.service

# 4. Confirm the daemon picked up the fix.
journalctl -u outbox-notifier.service --since '1 min ago' | tail
```

After restart, the queue is empty (cold start). PRs that had Mirror
PASS during the outage will not re-trigger gate 1 automatically — if
the original outbox was archived, the work is lost. Inspect
`~/agents/outboxes/mirror/.archive/` for the affected `t-rev*.json`
files and manually run the merge command from the DM body.

## Tune the watchdog threshold

The default 24h watchdog DM fires once per stuck queue entry. To raise
it (e.g. before a planned vacation), edit
`config/agent-models.json`:

```json
{
  "auto_merge_queue": {
    "watchdog_dm_hours": 72,
    "_note": "raised for week-long travel 2026-06-01 → 2026-06-08"
  }
}
```

The notifier picks up the change on its next module-cache invalidation
(daemon restart, or any call to `_invalidate_loop_bounds_cache`).

## Cross-repo isolation

PRs in different repos never block each other. A PR in
`ourliberty-agent-core` modifying `docs/operating-manual.md` does NOT
block a PR in `ourliberty-dashboard` modifying its own
`docs/operating-manual.md`. The serializer keys overlap by exact
`repo + path`.
