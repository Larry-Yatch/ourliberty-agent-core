# Runbook — promote-alerts (N4 needs-CEO-attention promotion rule)

Spec: `agents/beacon/specs/approvals-queue-rework.md` L6 / node N4.
Script: `scripts/promote_alerts.py` · Config: `config/promotion-rules.json`
Units: `systemd/ourliberty-promote-alerts.{service,timer}` (every 10 min).

## What it does

Pulse writes escalations to `~/agents/blackboard/pulse-escalations.json` (a
snapshot rewritten each cycle). That stream is ops-internal and silent by
default — it lands in `chain_events` as `escalation` rows and shows on the
Ops/System page, but it never pushes to Larry. This job is the classifier that
decides which escalations cross the "needs CEO attention" bar and PROMOTES them
into Larry's decision inbox. Everything below the bar stays silent.

## The bar (conservative; default is NOT to promote)

An escalation is promoted iff:

- it carries an explicit-for-Larry flag (`escalate_to_larry` / `for_larry`) — promotes immediately; OR
- `severity == severity_bar` (default `critical`) **AND** it has persisted at
  least `self_resolve_window_seconds` (default 600s ≈ one Pulse cycle) since
  first seen — i.e. it did **not** self-resolve within one cycle.

A critical escalation is **held** for one cycle before promoting. If it vanishes
from the snapshot in the meantime, it is treated as self-resolved and never
reaches Larry. Anything that does not clearly meet the bar stays silent.

Severity uses the canonical vocabulary reused from `larry_alerts.VALID_SEVERITIES`
(`warning` < `critical`); unrecognized severities never meet the bar.

We deliberately do **not** consume `larry-alerts.jsonl` / `sentinel-alerts.jsonl`
here — those already DM Larry via the alert queue + Beacon bot. Promoting them
again would double-surface and break existing routing.

## Tuning (Pulse-tunable)

Edit `config/promotion-rules.json` and commit; the next cycle picks it up (no
reload). Keys: `promotion_enabled`, `severity_bar`, `self_resolve_window_seconds`,
`dedup_key_fields`, `explicit_promotion_fields`. Missing/malformed config →
conservative built-in defaults (`DEFAULT_CONFIG`). This is the hook a future
Pulse-Check self-optimizer writes proposals against (mirror Check III).

## Contract with the dashboard (N4 dashboard / N5)

Each promoted escalation is push-emitted as a `needs_attention` chain_event via
`chain_event_emit.emit_event`:

| field | value |
|---|---|
| `event_type` | `needs_attention` (registered in `chain_event_shipper.KNOWN_EVENT_TYPES`) |
| `agent` | `promote-alerts` |
| `task_id` | the escalation's task_id |
| `payload.reason` | `explicit-flag` \| `critical-persisted` |
| `payload.severity` | normalized severity |
| `payload.headline` / `payload.detail` | from the escalation |
| `payload.dedup_identity` | `dedup_identity \|\| task_id` |
| `payload.source_event_type` | `escalation` |

The dashboard's `NeedsAttentionCard` on `/live` queries `event_type =
'needs_attention'`. That dashboard wiring is the dependent N4-dashboard / N5
work (edge N4 → N5); this script is the producer.

## State

`~/agents/state/promotion-probation.json` (atomic tmp+rename). Keyed by
`dedup_identity`; tracks `first_seen_ts`, `promoted`, `promoted_ts`. Idempotent:
a promoted escalation is not re-promoted while present; a failed emit (Supabase
down) leaves it un-promoted so the next cycle retries.

## Operate

```bash
# run once by hand
python3 ~/agent-core/scripts/promote_alerts.py
# logs
tail -f ~/agents/logs/promote-alerts.log
journalctl -u ourliberty-promote-alerts.service -n 50 --no-pager
# kill switch (shared healer disable)
touch ~/agents/healers.disabled   # exits 0 without acting
```

## Install (per systemd/INSTALL.md)

```bash
sudo cp ~/agent-core/systemd/ourliberty-promote-alerts.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ourliberty-promote-alerts.timer
```
