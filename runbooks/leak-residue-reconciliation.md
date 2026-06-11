# Leak-residue reconciliation runbook (H18)

**When to run this:** any time a test (or any non-production process) is found to
have leaked into a real `~/agents` tree — a fixture envelope dispatched, a fixture
alert paged, a fixture quota/chain row written, a fixture log line persisted. The
test jail (test-jail PRs 1–4) is designed to make this *structurally impossible*
under every blessed invocation shape, so reaching for this runbook means a guard
was bypassed, reverted, or a brand-new unguarded sink shipped. Treat a breach as a
P1: stop the bleeding first (find and fix the leak vector), then reconcile the
residue with the checklist below.

**Why a dedicated runbook (the H18 hole):** the surfaces tests leak into are
consumed by always-on daemons and are mostly append-only with no retraction, so a
*transient, immediately-noticed* leak still converts into multi-day operator toil
and keeps re-firing until manually purged. Reconciliation today is per-channel,
pattern-enumerated, and reactive — there is no single test-artifact GC. This
checklist is the manual GC. It exists because every historical containment
(`~/agents/.fixture-cleanup-20260610`, the 06-02 row-count check, the 05-28
hand-drain) needed a separate purge that nobody had written down.

**Golden rule: ARCHIVE, never delete.** Mirror the existing
`~/agents/.fixture-cleanup-<date>` convention (droplet) and
`/Users/Larry/agents.fixture-cleanup-<date>` (Mac, done 2026-06-11). Move residue
into a dated archive dir so it is auditable and reversible; never `rm`. A wrong
purge that deletes a *real* alert/envelope/offset is a second incident.

---

## 0. Scope the blast radius first

Before touching anything, establish WHAT leaked, on WHICH machine, and over WHAT
window. Both the droplet (`larry@134.209.44.80`, real `$HOME=/home/larry`) and the
Mac (`/Users/Larry`) have full live trees — a leak on either pages the real chat
and spends real money. Containment must be scoped to the machine that ran the
leaking process; the droplet purge does not touch the Mac and vice-versa.

- Identify the leak marker / fixture shape. Deliberate-leak proofs carry
  `OL-DELIBERATE-LEAK-PROBE-MARKER-DO-NOT-SHIP`; real-incident fixtures carry
  shapes like `zz-fixture-`, `notify-t-`, `notify-q-`, `t-exhausted`, `t-rev`,
  `marker-error-*`, `TIER_ONE_MARKER`, `sess-abc-`, or a test session id
  (`32401737…`). Pick the narrowest greppable string that matches the leak and
  nothing real.
- Snapshot the tree before purging (so the archive is a true before-state):
  ```sh
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  ARCHIVE=~/agents/.fixture-cleanup-$STAMP
  mkdir -p "$ARCHIVE"
  ```
- Record the leak window (first/last leaked timestamp) — several channels below
  are purged by time-window, not just by marker.

---

## 1. Alert ledger — `~/agents/blackboard/larry-alerts.jsonl`

Append-only; most alert classes have **no retraction**, so a leaked 🔴 never
self-clears and consumers (Beacon, Medic) may have already paged it.

- Find leaked lines: grep the ledger for the marker/fixture subject. Copy the full
  matching lines into `$ARCHIVE/larry-alerts.leaked.jsonl`.
- Rewrite the ledger WITHOUT the leaked lines (filter to a temp file, then move
  into place — never edit in-flight while a writer holds it):
  ```sh
  cp ~/agents/blackboard/larry-alerts.jsonl "$ARCHIVE/larry-alerts.before.jsonl"
  grep -vF "<MARKER>" "$ARCHIVE/larry-alerts.before.jsonl" \
    > ~/agents/blackboard/larry-alerts.jsonl.new \
    && mv ~/agents/blackboard/larry-alerts.jsonl.new ~/agents/blackboard/larry-alerts.jsonl
  ```
- If the leaked alert was already delivered, prefer a key-based retraction where a
  producer supports it: `larry_alerts.resolve_alert(<key>)` writes a resolve event
  with consumer-cursor bookkeeping (only `heal_systemd_install_drift` calls it
  today; every other class persists until `larry_alerts_retention.py`'s age-based
  archive). For un-retractable classes, the grep-filter above is the purge.
- Re-check after purge: a stale 🔴 in the running dashboard means a consumer cursor
  already advanced past the line — see §6 (offsets).

## 2. Alert cooldown / silence keys — `~/agents/state/alert-cooldown*`

A leaked alert can install a cooldown/silence key that then **suppresses the real
alert** of the same class for its TTL — the dangerous inverse of noise. After
purging §1, clear any cooldown/silence key minted by the leaked alert so a genuine
recurrence pages normally.

- Inspect `~/agents/state/alert-cooldown` (and any `*-silence*` / dedup-key state).
  Archive the file, then remove only the keys whose subject matches the leak.
- When in doubt, archive-and-truncate the cooldown state entirely: cooldowns are
  soft (worst case a real alert pages slightly sooner), whereas a lingering
  fixture-installed silence can mask a real outage.

## 3. Quota / chain ledgers — `~/agents/blackboard/anthropic-quota-events.jsonl`, chain_events

The genuinely unguarded channel in H18: no retention script, no retraction
primitive, and tier/burn consumers re-read the raw window each tick — a leaked row
re-fires false tier/burn alerts every cycle (this is exactly how `TIER_ONE_MARKER`
pooled for ~5 days). Purge by **marker AND window**:

- Grep `anthropic-quota-events.jsonl` for the marker and for fixture task-ids in
  the leak window; archive matches to `$ARCHIVE/`, filter them out of the live
  ledger (same temp-file-then-move pattern as §1).
- If the leak wrote Supabase `chain_events` rows (only possible if the Supabase
  guard was bypassed — under the jail `get_supabase_client` refuses), reconcile
  those rows out of the live table by `chain_id` / fixture marker. Verify against
  the service-role DB only with a read first; never blind-delete.
- Re-run the tier/burn consumer once after the purge and confirm it no longer
  emits the false tier alert (`pulse_check_viii` filters by `is_fixture_task_id`,
  but the raw consumers do not).

## 4. Inbox / outbox residue — `~/agents/inboxes/<agent>/`, `~/agents/outboxes/<agent>/`

The money amplifier: a leaked envelope is consumed by `inbox_watcher` and becomes a
real (paid Opus) dispatch — and can self-replicate via routing-wrapper bugs (the
2026-05-28 `$261.63 / 725-run` cascade ran on residue of *earlier* leaks, ~2,564
archive re-processings). Drain residue from BOTH the live queue and the `.archive`
(the watcher re-reads archives under some routing shapes).

- For each agent inbox/outbox: move any envelope matching the marker/fixture
  prefix into `$ARCHIVE/inboxes/<agent>/`. Check `.archive/` subdirs too — fixture
  GC at `inbox_watcher` consumption only fires for ENUMERATED `fixture_patterns.py`
  prefixes, so a novel leak shape sits in the queue until you remove it here.
- Confirm no in-flight dispatch is mid-processing the leaked envelope before
  moving it (`ls ~/agents/state/in-flight/`); if one is, let it self-archive or
  stop it deliberately — do not yank an envelope out from under a live consumer.
- If the leak shape is novel (not in `fixture_patterns.py`), file a follow-up to
  add the pattern so the gate-at-emission allowlist catches the next one — but do
  NOT widen the allowlist as part of the purge itself.

## 5. Agent heartbeats — `~/agents/state/*heartbeat*`, freshness markers

A leaked heartbeat-freshen (e.g. a `ceo_digest_generator` test touching a real
liveness file) **masks a genuinely dead agent** — the watchdog reads green while
the agent is down. After a breach, recompute/clear any heartbeat the leak touched
so liveness reflects reality.

- Archive the touched heartbeat/freshness files, then let the real agent
  re-publish on its next live tick (or clear so the watchdog reports the true
  stale state). Confirm with `python3 ~/agent-core/scripts/watchdog.py | tail`.

## 6. Consumer offsets / cursors — `~/agents/state/*-offset*.txt`

Several consumers track a byte/line offset into the ledgers you just rewrote
(`beacon-alerts-offset.txt`, medic cursors, etc.). After you filter lines OUT of an
append-only ledger in §1/§3, a stored offset can now point mid-line or past the new
EOF, causing skipped or double-read alerts.

- Archive each offset file, then reconcile it to the rewritten ledger: simplest
  safe reset is to set the offset to the new end-of-file so already-delivered real
  alerts are not re-paged, after confirming no un-delivered real alert sits past
  the old cursor. Cross-check the cursor against the §1 leaked-line positions.

## 7. Daemon log lines — `~/agents/logs/*.log`

Lowest-severity but highest-confusion: leaked log lines (e.g.
`../../../../etc/pwned` path-traversal fixtures, `transcript-not-persisted`
CRITICAL DMs citing mock `/tmp` paths, fixture quota events) get read by humans and
by log-scraping classifiers and trigger false investigations.

- Grep each daemon log (`beacon`, `inbox_watcher`, `medic-dispatcher`, `heal-*`)
  for the marker; archive the matching lines, then filter them out (temp-file +
  move). Logs are rotated, so a marker may span the active log and a `.1`/`.gz`
  roll — check both.

---

## 8. Verify clean

After all channels are reconciled, prove zero residue remains:

```sh
grep -rIl "<MARKER>" ~/agents 2>/dev/null   # expect: no output
ls ~/agents/state/in-flight/                # expect: empty (or only real work)
python3 ~/agent-core/scripts/watchdog.py | tail -3   # expect: overall=healthy
```

- Confirm the archive dir holds a full copy of everything removed
  (`du -sh "$ARCHIVE"` is non-zero and matches what you purged).
- Re-run the offending consumer once and confirm the false alert/dispatch/tier
  event does NOT re-fire.

## 9. Close the loop (so it can't recur silently)

A reconciliation that doesn't fix the leak vector just buys time until the next
tick re-leaks. Before declaring the incident closed:

- Identify and fix the leak vector (a reverted/ bypassed choke guard, a new
  unguarded sink, an invocation shape that skipped `_bootstrap`). The
  deliberate-leak acceptance proof
  (`scripts/tests/test_deliberate_leak_is_caught.py`) and the chokepoint census
  gate exist precisely so this fails the BUILD next time — confirm they still pass
  and, if the leak shape was novel, extend them to cover it.
- If the leak shape was not in `fixture_patterns.py`, add it (separately from the
  purge) so the gate-at-emission allowlist catches the next one.
- Record the incident (date, marker, channels touched, archive path, root-cause
  vector) at the journal head so the next responder has the provenance.

---

## Incident history (provenance for the channel list above)

- **2026-05-28 — `$261.63 / 725-run` cascade:** fixture/test envelopes
  (`notify-t-pf`, `notify-q-1`, `dead-letter-*`, `marker-error-*`) — residue of
  earlier leaks — dispatched to Opus as real tasks in a loop, self-replicating via
  a `notify-notify-` routing-wrapper bug (~2,564 archive re-processings). Drove the
  §4 inbox/`.archive` drain requirement.
- **2026-05-29 — fixture-replay incident:** leaked fixture envelopes re-dispatched;
  motivated the gate-at-emission fixture allowlist (`fixture_patterns.py`, PR #170)
  — the enumerated-pattern GC that §4 relies on (and whose gaps it covers).
- **Tier-fixture pooling (purged 2026-06-10, `~/agents/.fixture-cleanup-20260610`):**
  `TIER_ONE_MARKER` / "resets 11:30am" quota lines pooled ~5 days, re-firing false
  tier alerts long after writing stopped — the §3 unguarded-quota-ledger case.
- **#438 transcript storm:** false `transcript-not-persisted` CRITICAL DMs citing
  mock `/tmp` paths leaked into the live inbox_watcher log — the §7 log-line case;
  also the canonical "a side effect on a path old tests drive end-to-end breaks
  Larry's evening, not the build" failure the test jail closes.
- **2026-06-11 — Mac phantom purge (M5):** `/Users/Larry/agents` (fixture alerts
  carrying the real `chat_id 7998341473`) archived to
  `/Users/Larry/agents.fixture-cleanup-20260611` — the precedent that
  reconciliation is per-machine and the Mac is a full live tree, not a sandbox.
