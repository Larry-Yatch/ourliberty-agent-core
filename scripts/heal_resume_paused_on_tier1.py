#!/usr/bin/env python3
"""heal_resume_paused_on_tier1.py — auto-resume paused_on_tier1 tasks.

Step B of `rate-limit-resilience-001`
(`docs/rate-limit-resilience-B-resume-healer-brief.md`).

Background
----------
When a Tier 1 rate-limit or auth_401 hits a `--resume` session, Tier 2
fallback is structurally unavailable — session IDs are account-bound, so a
fallback would fail with `session not found`. `agent_runner._mark_paused_on_tier1`
writes a sentinel into the in-flight state file
(`~/agents/state/in-flight/<task_stem>.json`) and the work parks indefinitely
until Larry notices the DM. `heal_pipeline_stall` already DETECTS this
condition; nothing RESUMES the work when the tier window clears.

What this healer does
---------------------
Scans `~/agents/state/in-flight/*.json` for the `paused_on_tier1` marker,
checks whether the recorded tier's cooldown / 5h window has cleared via
`active_tier.cooldown_until(tier)`, and re-dispatches the original task as
a FRESH dispatch — `session_id` / `resume_session_id` stripped, `task_id`
suffixed `-resume-<UTC ts>` so it can't collide with archive entries,
`source='auto-retry'` (system source that bypasses hard-topology checks).
On a successful re-dispatch, deletes the in-flight file so the marker
isn't seen again.

Safety
------
- Bounded systemd-oneshot (no detached poll loops, per repo convention).
- `~/agents/healers.disabled` kill switch (blanket).
- `OURLIBERTY_AUTORESUME_ENABLED` activation env var (default OFF → dry-run
  mode logs candidates + DMs Larry a one-time activation prompt). Pattern
  borrowed from `heal_pr_auto_merge`'s "default off until verified" gate.
- `MAX_RESUMES_PER_RUN=5` blast-radius cap.
- Per-task retry budget (`MAX_RESUME_ATTEMPTS=3`) keyed in
  `~/agents/state/heal-resume-paused-on-tier1.json` so a chronically-failing
  task doesn't get hammered each tick. Exhausted → DM Larry one-time then
  skip until the marker is cleared manually.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Allow imports of sibling modules in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
IN_FLIGHT_DIR = AGENTS_ROOT / 'state' / 'in-flight'
INBOXES_ROOT = AGENTS_ROOT / 'inboxes'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-resume-paused-on-tier1.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-resume-paused-on-tier1.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-resume-paused-on-tier1.json'

ENV_AUTORESUME_ENABLED = 'OURLIBERTY_AUTORESUME_ENABLED'
MAX_RESUMES_PER_RUN = 5
MAX_RESUME_ATTEMPTS = 3

# Resume-related envelope keys to strip on re-dispatch. The session_id IS
# account-bound and re-using it on a fresh tier would either fail
# (`session not found` on the other account) or orphan the previous
# session's context. resume_session_id mirrors it on the build-phase
# dispatches the outbox notifier writes; both must go.
_RESUME_KEYS = ('session_id', 'resume_session_id')

# Sources we recognize as "auto-retry-eligible" — the healer only ever
# re-dispatches under our own well-known system source so the routing-events
# log clearly attributes the re-dispatch to this healer.
_HEALER_SOURCE = 'auto-retry'


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


def autoresume_enabled() -> bool:
    """True iff OURLIBERTY_AUTORESUME_ENABLED=true (case-insensitive)."""
    return os.environ.get(ENV_AUTORESUME_ENABLED, '').strip().lower() == 'true'


# -------------------- DM to Larry --------------------


def dm_larry(message: str, subject: str, suggested_action: str,
             severity: str = 'warning') -> bool:
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-resume-paused-on-tier1',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


# -------------------- state file (per-task retry counter) --------------------


def load_state() -> dict[str, Any]:
    """{'tasks': {task_stem: {'attempts': N, 'exhausted_alerted': bool,
    'activation_alerted': bool, 'last_attempt_iso': str}}}."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'tasks': {}}
        data.setdefault('tasks', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'tasks': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def get_task_state(state: dict[str, Any], task_stem: str) -> dict[str, Any]:
    entry = state['tasks'].setdefault(task_stem, {})
    entry.setdefault('attempts', 0)
    entry.setdefault('exhausted_alerted', False)
    entry.setdefault('activation_alerted', False)
    entry.setdefault('last_attempt_iso', None)
    return entry


# -------------------- in-flight + archive plumbing --------------------


def scan_paused_markers() -> list[tuple[Path, str, dict[str, Any]]]:
    """Return [(in_flight_path, task_stem, marker_dict), ...] for each
    in-flight file that carries a `paused_on_tier1` marker."""
    out: list[tuple[Path, str, dict[str, Any]]] = []
    if not IN_FLIGHT_DIR.exists():
        return out
    for p in sorted(IN_FLIGHT_DIR.glob('*.json')):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        marker = data.get('paused_on_tier1')
        if not isinstance(marker, dict):
            continue
        out.append((p, p.stem, marker))
    return out


def find_archived_envelope(task_stem: str,
                           agent_hint: Optional[str]) -> Optional[tuple[str, Path, dict[str, Any]]]:
    """Locate the original task envelope archived by the inbox watcher.

    Returns (agent_id, archive_path, envelope_dict) or None. The agent_hint
    short-circuits the scan when the marker carried it; otherwise we scan
    every agent inbox's `.archive/` (slow path for legacy markers written
    before the agent_id field landed)."""
    candidates: list[str] = []
    if agent_hint:
        candidates.append(agent_hint)
    else:
        if INBOXES_ROOT.exists():
            for d in sorted(INBOXES_ROOT.iterdir()):
                if d.is_dir() and not d.name.startswith('.'):
                    candidates.append(d.name)

    for agent in candidates:
        path = INBOXES_ROOT / agent / '.archive' / f'{task_stem}.json'
        if not path.exists():
            continue
        try:
            env = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(env, dict):
            return agent, path, env
    return None


# -------------------- tier cooldown gate --------------------


def _marker_since_ts(marker: dict[str, Any], in_flight_path: Path) -> datetime:
    """The instant the task paused — the marker's `at`, else the in-flight
    file mtime as a fallback for legacy markers without it."""
    at = marker.get('at')
    if isinstance(at, str):
        try:
            dt = datetime.fromisoformat(at.replace('Z', '+00:00'))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(
            in_flight_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc) - timedelta(days=1)


def resolved_out_of_band(task_stem: str, since_ts: datetime
                         ) -> tuple[bool, Optional[str]]:
    """True if the paused task was already handled out-of-band since it
    paused — Larry re-dispatched it via the Approvals tab (`larry_action`),
    or the producing agent already started a fresh attempt (a later
    `session_start` supersedes this pointer). chain_events match on the EXACT
    task_id, so this is high-confidence; `check_pr=False` deliberately avoids
    a weak title-substring PR match clearing a still-pending task. Failsafe:
    any infra error returns (False, None) so a real paused task still
    re-dispatches."""
    try:
        import task_resolution as tr  # noqa: E402
        return tr.resolved_out_of_band(
            task_stem, since_ts, check_pr=False,
            log_fn=lambda m: log(m, 'INFO'))
    except Exception as e:  # noqa: BLE001 -- never block resume on an error
        log(f'resolution check failed for {task_stem}: '
            f'{type(e).__name__}: {e}; proceeding', 'WARN')
        return (False, None)


def cooldown_cleared(tier: str) -> tuple[bool, Optional[str]]:
    """Return (cleared, until_iso). cleared=True iff no active cooldown
    is recorded for this tier (the recorded one already expired, or none
    was ever set)."""
    try:
        import active_tier  # noqa: E402
    except Exception as e:
        log(f'active_tier import failed: {type(e).__name__}: {e}; '
            f'defensive: treat as NOT cleared', 'WARN')
        return False, None
    try:
        until = active_tier.cooldown_until(tier)
    except Exception as e:
        log(f'cooldown_until({tier!r}) failed: {type(e).__name__}: {e}; '
            f'defensive: treat as NOT cleared', 'WARN')
        return False, None
    return (until is None), until


# -------------------- re-dispatch --------------------


_TASK_ID_TS_RE = re.compile(r'^[\w.\-]+$')


def _resume_task_id(original: str, now: Optional[datetime] = None) -> str:
    """Append -resume-<UTC compact ts> to the task id so the re-dispatch
    can't collide with the archived original."""
    now = now or datetime.now(timezone.utc)
    suffix = now.strftime('%Y%m%dT%H%M%SZ')
    return f'{original}-resume-{suffix}'


def build_resumed_envelope(envelope: dict[str, Any],
                           now: Optional[datetime] = None) -> tuple[dict[str, Any], str]:
    """Return (new_envelope, new_task_id). Pure function — the heart of
    the fresh-dispatch contract:

      - strip every `_RESUME_KEYS` (session_id, resume_session_id)
      - set source='auto-retry'
      - bump task_id with `-resume-<UTC ts>`
      - drop transient routing fields the watcher will re-derive
        (`dispatched_by`, `_notify_depth`)
      - keep phase as preflight by default (fresh dispatches start at
        preflight; the build phase is auto-arranged from PROCEED). If
        the original was phase=build, we still re-dispatch as preflight
        — the spec is explicit that account-bound session IDs make
        build-phase resumption structurally impossible.
    """
    new = dict(envelope)
    for k in _RESUME_KEYS:
        new.pop(k, None)
    new.pop('dispatched_by', None)
    new.pop('_notify_depth', None)
    new.pop('marker_error_count', None)

    original_id = new.get('task_id', '')
    new_id = _resume_task_id(original_id, now=now) if original_id else _resume_task_id('unknown', now=now)
    new['task_id'] = new_id
    new['source'] = _HEALER_SOURCE
    new['phase'] = 'preflight'
    new['_resumed_from'] = original_id
    return new, new_id


def redispatch(agent: str, envelope: dict[str, Any]) -> Optional[Path]:
    """Re-dispatch via safe_write_inbox. Returns the written path or
    None on failure (logged)."""
    try:
        import safe_write_inbox as swi  # noqa: E402
    except Exception as e:
        log(f'safe_write_inbox import failed: {type(e).__name__}: {e}', 'ERROR')
        return None
    filename = f'{envelope["task_id"]}.json'
    try:
        dest = swi.safe_write_inbox(
            target_agent=agent,
            task_dict=envelope,
            source_agent=_HEALER_SOURCE,
            filename=filename,
        )
        return dest
    except (swi.DispatchRejected, swi.RoutingDenied) as e:
        log(f're-dispatch rejected for {agent}/{filename}: '
            f'{type(e).__name__}: {e}', 'ERROR')
        return None
    except Exception as e:
        log(f're-dispatch failed for {agent}/{filename}: '
            f'{type(e).__name__}: {e}', 'ERROR')
        return None


def clear_in_flight_marker(in_flight_path: Path) -> None:
    """Drop the in-flight file once the re-dispatch has landed. The watcher
    will create a fresh in-flight entry when it picks up the resumed task."""
    try:
        in_flight_path.unlink()
    except OSError as e:
        log(f'failed to clear in-flight marker {in_flight_path}: {e}', 'WARN')


# -------------------- main loop --------------------


def process_one(in_flight_path: Path, task_stem: str, marker: dict[str, Any],
                state: dict[str, Any], *, enabled: bool) -> bool:
    """Process a single paused task. Returns True iff a re-dispatch was
    actually issued (used by the per-tick blast-radius cap)."""
    failure_type = marker.get('failure_type') or 'unknown'
    agent_hint = marker.get('agent_id')
    tier = marker.get('tier') or 'tier1'

    # Out-of-band resolution gate: if Larry already re-dispatched this task,
    # or the producing agent already re-ran it, the paused marker is stale.
    # Clear it silently rather than re-dispatch a duplicate (and never DM
    # about it) — this is the false-positive the 2026-06-04 audit flagged.
    since_ts = _marker_since_ts(marker, in_flight_path)
    resolved, reason = resolved_out_of_band(task_stem, since_ts)
    if resolved:
        log(f'task={task_stem} resolved out-of-band ({reason}); clearing '
            f'stale in-flight marker — no re-dispatch')
        clear_in_flight_marker(in_flight_path)
        state['tasks'].pop(task_stem, None)
        return False

    cleared, until = cooldown_cleared(tier)
    if not cleared:
        log(f'task={task_stem} tier={tier} cooldown_until={until} — '
            f'still cooling down; skipping')
        return False

    found = find_archived_envelope(task_stem, agent_hint)
    if not found:
        log(f'task={task_stem} agent_hint={agent_hint!r} — '
            f'no archived envelope found; skipping', 'WARN')
        return False
    agent, archive_path, envelope = found

    task_state = get_task_state(state, task_stem)
    attempts = task_state.get('attempts', 0)
    if attempts >= MAX_RESUME_ATTEMPTS:
        if not task_state.get('exhausted_alerted'):
            dm_larry(
                message=(
                    f'Auto-resume budget EXHAUSTED for task `{task_stem}` '
                    f'(agent={agent}, failure_type={failure_type}, '
                    f'tier={tier}, attempts={attempts}/{MAX_RESUME_ATTEMPTS}). '
                    f'Healer will stop re-dispatching this task until the '
                    f'in-flight marker is cleared manually.\n\n'
                    f'In-flight file: {in_flight_path}\n'
                    f'Archived envelope: {archive_path}'
                ),
                subject=f'heal-resume-exhausted:{task_stem}',
                suggested_action=(
                    f'Investigate why repeated re-dispatch keeps failing. '
                    f'If safe to retry, `rm {in_flight_path}` to reset the '
                    f'healer; if the task is no longer wanted, delete the '
                    f'archived envelope too.'
                ),
            )
            task_state['exhausted_alerted'] = True
        log(f'task={task_stem} attempts={attempts} ≥ '
            f'MAX_RESUME_ATTEMPTS={MAX_RESUME_ATTEMPTS}; skipping', 'WARN')
        return False

    new_env, new_task_id = build_resumed_envelope(envelope)

    if not enabled:
        # Dry-run mode — one-time activation DM so Larry knows the healer
        # is sitting idle on real work.
        if not task_state.get('activation_alerted'):
            dm_larry(
                message=(
                    f'heal-resume-paused-on-tier1 is running in DRY-RUN '
                    f'mode and has a real candidate to re-dispatch:\n\n'
                    f'  task={task_stem}\n'
                    f'  agent={agent}\n'
                    f'  failure_type={failure_type}\n'
                    f'  tier={tier} (cooldown cleared)\n'
                    f'  would re-dispatch as: {new_task_id}\n\n'
                    f'To activate live mode, edit the service unit:\n'
                    f'  sudo systemctl edit ourliberty-heal-resume-paused-on-tier1.service\n'
                    f'then add to [Service]:\n'
                    f'  Environment="{ENV_AUTORESUME_ENABLED}=true"\n'
                    f'then `sudo systemctl restart '
                    f'ourliberty-heal-resume-paused-on-tier1.timer`.'
                ),
                subject=f'heal-resume-dry-run:{task_stem}',
                suggested_action=(
                    f'Enable autoresume via the unit env var above, OR '
                    f'manually re-dispatch the task.'
                ),
            )
            task_state['activation_alerted'] = True
        log(f'DRY-RUN: task={task_stem} agent={agent} tier={tier} cleared; '
            f'would re-dispatch as {new_task_id}')
        return False

    dest = redispatch(agent, new_env)
    task_state['attempts'] = attempts + 1
    task_state['last_attempt_iso'] = datetime.now(timezone.utc).isoformat()
    if dest is None:
        log(f'task={task_stem} re-dispatch FAILED (attempt '
            f'{task_state["attempts"]}/{MAX_RESUME_ATTEMPTS})', 'ERROR')
        return False

    log(f'task={task_stem} agent={agent} tier={tier} → re-dispatched as '
        f'{new_task_id} ({dest}); clearing in-flight marker')
    clear_in_flight_marker(in_flight_path)
    return True


def main() -> int:
    if kill_switch_active():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    enabled = autoresume_enabled()
    if not enabled:
        log('autoresume DRY-RUN (set OURLIBERTY_AUTORESUME_ENABLED=true to '
            'enable live re-dispatch)')

    markers = scan_paused_markers()
    if not markers:
        log('no paused_on_tier1 markers found')
        return 0

    log(f'scanned {len(markers)} paused marker(s); cap='
        f'{MAX_RESUMES_PER_RUN}/tick')

    state = load_state()
    redispatched = 0
    for in_flight_path, task_stem, marker in markers:
        if redispatched >= MAX_RESUMES_PER_RUN:
            log(f'hit MAX_RESUMES_PER_RUN={MAX_RESUMES_PER_RUN}; '
                f'remaining tasks deferred to next tick')
            break
        try:
            did_redispatch = process_one(
                in_flight_path, task_stem, marker, state, enabled=enabled,
            )
        except Exception as e:
            log(f'process_one({task_stem!r}) crashed: '
                f'{type(e).__name__}: {e}', 'ERROR')
            did_redispatch = False
        if did_redispatch:
            redispatched += 1

    save_state(state)
    log(f'done: {redispatched} task(s) re-dispatched')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        sys.exit(1)
