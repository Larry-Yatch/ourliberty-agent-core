# Runbook — build_sequence_advancer

**Component:** `scripts/build_sequence_advancer.py` + `systemd/ourliberty-build-sequence-advancer.{service,timer}`
**Spec:** `agents/beacon/specs/build-sequence-orchestrator.md` (§ 5.1 schema, § 5.2 architecture, § 5.3 gate, § 5.4 failure modes, § 5.8 data sources)
**Shipped in:** PR-S2 (this PR)
**Activated by:** flipping `OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=true` via systemd override (default OFF — see "Activation gate" below)
**Healer:** `scripts/heal_build_sequence_advancer_heartbeat.py` (DMs Larry on stale heartbeat)

## What it does (one paragraph)

The advancer is a polling daemon that drives multi-step build sequences. Every 5 min (systemd timer), it reads every sequence file in `~/agents/blackboard/build-sequences/*.json`, and for each `active` sequence: checks the belt-and-suspenders gate (`chain_events.auto_merge.outcome=merged` + `gh pr view = MERGED`) on every in-flight step, transitions merged steps to `merged`, dispatches any pending step whose dependencies are now all merged, and (on the last merge) marks the sequence `complete`. Failures (Mirror REVISION-exhausted / EMERGENCY_HALT / forge_reject, or a >30-min gate mismatch) pause the sequence and DM Larry. The daemon is stateless across reboots — all state lives in the sequence file itself, and the first tick after a restart rebuilds live state from the file + chain_events.

## Runtime paths

| Path | Purpose | Tracked in repo? |
|---|---|---|
| `~/agents/blackboard/build-sequences/<seq-id>.json` | One file per sequence; the authoritative state. | **No.** `~/agents/blackboard/` is a runtime-only mount — the daemon creates the dir on first tick. |
| `~/agents/blackboard/build-sequence-advancer.heartbeat` | Text file with the last-tick ISO timestamp. Healer reads mtime. | No (runtime-only). |
| `~/agents/inboxes/beacon/<step-task-id>.json` | Where the advancer writes step-dispatch envelopes when a step's deps reach merged. | No (runtime-only inbox). |
| `~/agents/inboxes/build_sequence_advancer/kickoff-<seq-id>.json` | The advancer's OWN inbox. A chat-issued `approve sequence <id>` routes a kickoff `APPROVAL_REQUEST` here (via `beacon_approval_handler.dispatch_approved` → `safe_write_inbox`). `_drain_kickoff_inbox` consumes it at the top of each tick and transitions the named sequence `pending → active`. | No (runtime-only inbox). |
| `~/agents/logs/build-sequence-advancer.log` | Daemon log; mirror copy in `journalctl -u ourliberty-build-sequence-advancer.service`. | No. |

The dispatch envelope's `source` is `orchestrator` (matches `routing_validator.SYSTEM_SOURCES`); its `prompt` is a self-contained instruction to Beacon to synthesize one standard `APPROVAL_REQUEST` marker for Forge whose body is the step's `dispatch_text` verbatim. PR-S4 lands first-class `target_agent: build_sequence_advancer` routing; until then the `orchestrator → beacon` envelope path is what fires.

## Kickoff-inbox drain (chat-path `approve sequence`)

There are **two** ways a `pending` sequence reaches `active`, and they use different transport:

1. **File-outbox path** — a Beacon session dispatched with `source ∈ {larry, orchestrator}` emits the kickoff marker into her outbox, and `outbox_notifier._handle_build_sequence_advancer_kickoff` performs the transition.
2. **Chat path** — `approve sequence <id>` typed to Beacon in Telegram. The bot runs in-process and dispatches the kickoff `APPROVAL_REQUEST` (`target_agent: build_sequence_advancer`) via `beacon_approval_handler.dispatch_approved` → `safe_write_inbox`, which lands it in `~/agents/inboxes/build_sequence_advancer/kickoff-<seq-id>.json`.

Path 2 has no outbox entry the notifier ever sees, so before the fix the chat kickoff **orphaned** in the advancer's inbox and the sequence stalled at `pending` (observed on `pulse-check-xii`, 2026-07-07 — Beacon had to flip the status by hand). `_drain_kickoff_inbox` closes that: at the top of every tick (gated on the activation flag, like forward-dispatch), the advancer drains its own inbox, transitions each named `pending` sequence to `active` (appending a `kickoff-acknowledged` audit entry), and deletes the envelope. It is idempotent — a non-`pending` sequence is a consumed no-op; a missing / DAG-invalid target sequence DMs Larry (subject-bucketed) and drops the envelope; an envelope that doesn't parse as `kickoff <seq-id>` is left in place, not deleted. A newly-activated sequence dispatches its first step **in the same tick**, because the drain runs before the sequence-file loop.

Inspect a stuck chat kickoff:

```bash
ls -la ~/agents/inboxes/build_sequence_advancer/       # envelopes waiting to drain
journalctl -u ourliberty-build-sequence-advancer.service --since "10 min ago" | grep kickoff-inbox
```

## Reading sequence-file state

```bash
# Quickly inspect one sequence's state without involving systemd.
python3 ~/agent-core/scripts/build_sequence_advancer.py --dump-state <seq-id>

# Full file (human-readable JSON; the dump-state version is a digest).
cat ~/agents/blackboard/build-sequences/<seq-id>.json | jq .

# All active sequences right now.
for f in ~/agents/blackboard/build-sequences/*.json; do
  jq -r '"\(.seq_id)\t\(.status)\t\(.current_steps | join(","))"' "$f"
done
```

Each step's `status` follows spec § 5.1's enum: `pending` → `dispatchable` → `dispatched` → (Mirror) → `merged` or `failed`. The audit_log inside each file is the append-only history of state transitions, gate events, and dispatch attempts; it is the canonical source for "what happened when".

## Pause / resume / cancel

Until PR-S4 ships Beacon's 6 sequence shortcuts (`pause`, `resume`, `cancel`, `retry`, `skip`, `approve sequence`), the operator interface is direct atomic edits of the sequence file:

```bash
# Pause an active sequence (the daemon will skip processing on next tick).
python3 -c '
import json, os, sys
from pathlib import Path
p = Path(sys.argv[1])
seq = json.loads(p.read_text())
seq["status"] = "paused"
seq["audit_log"].append({
    "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    "event": "sequence-paused-by-operator",
    "actor": "larry",
    "reason": sys.argv[2] if len(sys.argv) > 2 else "manual",
})
tmp = p.with_suffix(".tmp")
tmp.write_text(json.dumps(seq, indent=2))
os.replace(tmp, p)
print(f"paused {seq[\"seq_id\"]}")
' ~/agents/blackboard/build-sequences/<seq-id>.json "<optional reason>"

# Resume: same script but with seq["status"] = "active". Next tick re-evaluates.

# Cancel: same script but with seq["status"] = "failed". In-flight PRs continue;
# no new step dispatches.
```

The reason file edits use `tmp + os.replace`: per spec § 5.1 atomic-writes are mandatory so a SIGKILL mid-edit cannot leave a partial JSON. Plain `vim ~/agents/blackboard/build-sequences/<seq-id>.json` is acceptable only when the daemon is fully stopped (`systemctl stop ourliberty-build-sequence-advancer.timer`) — vim's atomic save semantics are similar but not identical and the conservative posture is "stop the daemon first."

## Diagnosing a stalled advancer

The healer DMs Larry when the heartbeat file is >10 min stale (2 missed ticks at the 5-min cadence — spec § 5.4 failure mode 3). When that DM fires, work through this in order:

1. **Is the timer enabled and active?**
   ```bash
   systemctl status ourliberty-build-sequence-advancer.timer
   systemctl list-timers ourliberty-build-sequence-advancer.timer
   ```
   If `Active: inactive (dead)` → `sudo systemctl enable --now ourliberty-build-sequence-advancer.timer`. If the timer was deliberately disabled (e.g., during a Pulse-cycle pause), the healer is doing its job and the right answer is to re-enable.

2. **Is the activation gate closed?** The service unit ships with `OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=false` by default. The daemon checks this at the top of each tick and exits cleanly when not set — meaning the heartbeat is NEVER written. The healer correctly flags this as stale because, from its perspective, the daemon isn't running. To activate:
   ```bash
   sudo systemctl edit ourliberty-build-sequence-advancer.service
   # In the editor, add:
   #   [Service]
   #   Environment="OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=true"
   sudo systemctl daemon-reload
   # The next 5-min tick will pick it up and write the heartbeat.
   ```

3. **Are recent ticks failing?**
   ```bash
   journalctl -u ourliberty-build-sequence-advancer.service --since "1 hour ago" | tail -100
   ```
   Look for `FATAL`, `ERROR`, or repeated WARN lines about supabase connectivity. If supabase is unreachable, the chain_events leg of the belt-and-suspenders gate returns False and no steps will advance — the daemon is "running" but effectively inert until supabase comes back.

4. **Did a single bad sequence file poison the tick?** The daemon is supposed to log + DM and continue past bad files (per spec § 5.4), but if a file is causing per-tick errors:
   ```bash
   ls -la ~/agents/blackboard/build-sequences/
   # Look at any *.broken sidecar; the runbook above documents manual moves.
   ```

5. **As a last resort, run a tick by hand.** Useful for capturing exactly what's failing without waiting for the timer.
   ```bash
   sudo systemctl start ourliberty-build-sequence-advancer.service
   journalctl -u ourliberty-build-sequence-advancer.service -n 50
   ```

## Handling a corrupted sequence

The daemon distinguishes two failure modes per spec § 5.4 and handles each differently — see this table before you DM-respond.

| Failure mode | What the daemon does | What Larry sees | Operator action |
|---|---|---|---|
| **Unparseable JSON** (file won't even `json.loads()`) | Skips the file on every tick. DMs Larry once per `subject=sequence-unparseable:<filename>` (cooldown-gated). Does NOT write to the file (would lose data). | DM with the JSON parse error + path. | Edit the file by hand to fix the syntax, OR `mv` to `<filename>.broken` to remove it from the daemon's view. |
| **Schema-invalid (parseable JSON, wrong shape)** | Atomic-rewrites the file with `status: paused` + appends a `sequence-paused-invalid` audit_log entry. DMs Larry with the validation errors. | DM with the validation errors + "set status back to active after you fix it." | Fix the file (the audit_log entry names which fields failed); set `status: active` again; next tick resumes. |
| **30-min gate-mismatch, gh confirms merge** (`gh_merged=True`, chain_events lags) | After 30 min, **completes the step** (`step-merged`, `gate_resolution: gh-authoritative`) — gh is authoritative, chain_events is just behind. **No pause, no DM.** | Nothing — silent, correct completion. | None. (Before 2026-07-08 this false-paused + paged; `pulse-check-xii` was the last victim.) |
| **30-min gate-mismatch, gh does NOT confirm** (`chain_merged=True`, gh says OPEN/CLOSED) | After 30 min, sets `status: paused` + `gate-mismatch-timeout`. DMs Larry. | DM with the chain/gh mismatch summary. | Investigate: `gh pr view <pr_url> --json state`. chain_events says merged but gh disagrees — the PR may be closed-unmerged or chain emitted a false positive. |
| **Mirror EMERGENCY_HALT / forge_reject / revision_exhausted** (chain_events recorded a terminal-failure event) | Sets the failed step's status to `failed` + sequence `status: paused`. DMs Larry. | DM with the failure reason from chain_events. | Address the underlying Mirror / Forge issue; once fixed, set sequence `status: active` again to retry. (PR-S4 will land a `retry sequence X step Y` shortcut.) |

Other sequences in the same `~/agents/blackboard/build-sequences/` dir continue advancing normally — a single bad file never stops the world (per spec § 5.4 + tested in `test_unparseable_json_dms_and_does_not_crash`).

## Kill switches

The advancer respects two kill switches, in priority order:

1. **`~/agents/healers.disabled`** — blanket switch shared with every healer + daemon in the codebase. Touch this file and ALL maintenance machinery pauses on its next tick. Use during incident response when you need to freeze the chain.
2. **`OURLIBERTY_BUILD_SEQUENCE_ADVANCER_ENABLED=false`** — per-daemon gate set on the systemd unit (default value). Closes only this daemon.

The healer (`heal_build_sequence_advancer_heartbeat.py`) has its own switches:

- `~/agents/healers.disabled` — same blanket switch
- `OURLIBERTY_HEAL_BUILD_SEQUENCE_ADVANCER_DISABLE=true` — mutes only this one healer when a known issue is in flight and you don't want it re-DMing every 5 min

## Operating principles (read once)

- **Stateless across reboots.** The sequence file is the only durable state. If `~/agents/blackboard/build-sequence-advancer.heartbeat` disappears, the daemon will rebuild it on its next tick. If the entire droplet reboots, the daemon picks up where it left off from the sequence files alone — there is no cursor file, no cache, no replay log to manage.
- **Belt-and-suspenders gate: fast path needs both; the >30-min tiebreaker trusts gh only.** Both `chain_events.auto_merge.outcome=merged` AND `gh pr view = MERGED` must agree for a step to transition to `merged` on the fast path (spec § 5.3). A single-side signal is a `gate-mismatch` that starts a 30-min countdown. At timeout the resolution is asymmetric (§ 5.3): if **gh** confirms the merge and only chain_events lags, the step completes (gh is authoritative); if **chain_events** confirms but gh does not, it pauses + DMs. **Do not edit the gate logic to "just trust chain_events"** — trusting the *unreliable* side alone is exactly the failure mode the spec is engineered to prevent. Trusting **gh** (the authoritative merge state) at the timeout is the sanctioned exception, not a violation of this rule.
- **Append-only audit_log.** The audit_log inside each sequence file is forensic record. Never edit or delete entries — even when manually pausing a sequence, append a new entry instead.
- **Cooldown-gated DMs.** Larry-alerts are subject-keyed (`sequence-paused:<seq-id>`, `sequence-unparseable:<filename>`, etc.). A single bad sequence does not flood DMs at the 5-min tick cadence.

## Cross-references

- **Spec:** `agents/beacon/specs/build-sequence-orchestrator.md` — single source of truth for behavior. This runbook documents ops; the spec governs design.
- **Sibling healer:** `scripts/heal_chain_event_shipper_heartbeat.py` + runbook at `runbooks/chain-event-shipper.md` — closest analogue (E4.4d PR-B's heartbeat-stale healer). Same shape modulo subject and threshold.
- **Future PRs:** PR-S3 ships the dashboard ladder UI (reads these sequence files); PR-S4 ships Beacon's 6 sequence shortcuts + Mirror's preflight DAG verification (operator interface for pause/resume/cancel/retry/skip).
- **Spec-drift note:** spec § 5.3 references a `chain_events.event_type='auto_merge_success'` that does not exist in `KNOWN_EVENT_TYPES`; the actual emitted type is `auto_merge` with `outcome=merged|already_merged|failed` in the payload. The gate code adapts accordingly (`chain_event_says_merged` in `build_sequence_advancer.py`); Beacon will land a separate doc-only PR bringing § 5.3 wording in line with codebase reality.
