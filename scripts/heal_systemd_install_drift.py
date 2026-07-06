#!/usr/bin/env python3
"""heal_systemd_install_drift.py — systemd install drift healer (E1.5.2).

Every 12h via systemd timer. Compares `systemd/` in the repo against
`/etc/systemd/system/` on the droplet. Any unit shipped in the repo but
not installed on the droplet is a drift — operator forgot to run the
`cp + daemon-reload + systemctl enable --now` dance after a merge.

Motivated by the E1.5 discovery that `heal-pr-auto-merge.{service,timer}`
shipped via PR #43 but were never installed — that's exactly the gap this
healer closes.

12h cadence is sufficient because install state changes only on PR merge
(not minute-by-minute). Same dry-run-by-default + activation-on-first-real-
drift pattern as the credential drift healer; same two-layer kill-switch.

Stdlib only.
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
from atomic_io import atomic_write_json  # noqa: E402

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
KILL_SWITCH = AGENTS_ROOT / 'healers.disabled'
LOG_FILE = AGENTS_ROOT / 'logs' / 'heal-systemd-install-drift.log'
HEARTBEAT_FILE = AGENTS_ROOT / 'blackboard' / 'heal-systemd-install-drift.heartbeat'
STATE_FILE = AGENTS_ROOT / 'state' / 'heal-systemd-install-drift.json'

REPO_SYSTEMD_DIR = Path(__file__).resolve().parent.parent / 'systemd'
INSTALLED_SYSTEMD_DIR = Path('/etc/systemd/system')
ALLOWLIST_FILE = (
    Path(__file__).resolve().parent.parent
    / 'config' / 'auto-remediation-allowlist.json'
)

ENV_HEALER_ENABLED = 'OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED'

RE_DM_WINDOW = timedelta(hours=12)

# A failed/disabled auto-install is escalated to a 🔴 URGENT manual-dance DM only
# after the drift has PERSISTED this long since its first sighting. The
# post-merge sync trigger (scripts/sync_agent_core.sh) runs this healer on every
# deploy; when it catches a freshly-merged unit mid-deploy — the new file is in
# the repo but the droplet checkout / install hasn't landed yet, so the
# `sudo cp ~/agent-core/systemd/<unit> …` auto-install fails — the old code paged
# instantly, then the drift cleared on its own within minutes (the deploy
# finished installing the unit). That transient deploy-sync race is exactly the
# false page this window suppresses: a drift that resolves inside the window
# never DMs, while a genuinely-stuck drift (repo has it, install keeps failing,
# or auto-remediation is off) still escalates on the first tick past the window.
# Distinct from JUST_FIRED_GRACE_S on the stuck-timer path: that one is a "did
# this timer literally just fire" health check (LastTriggerUSec < 120s), whereas
# this is a first-sighting persistence debounce on the install/content-drift
# escalation. Backstopped by the 12h scheduled tick, so even with no intervening
# sync a real miss pages within one cadence.
ESCALATE_GRACE = timedelta(minutes=30)

SYSTEMCTL_TIMEOUT_S = 10
RESTART_TIMEOUT_S = 30

# A timer whose NextElapse anchor reads "infinity" but that fired within this
# many seconds is NOT stuck — see _recently_triggered / detect_stuck_timers.
JUST_FIRED_GRACE_S = 120


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


def kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


def healer_enabled() -> bool:
    return os.environ.get(ENV_HEALER_ENABLED, '').strip().lower() == 'true'


# -------------------- state --------------------

def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {'units': {}, 'stuck_timers': {}}
        data.setdefault('units', {})
        data.setdefault('stuck_timers', {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {'units': {}, 'stuck_timers': {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        atomic_write_json(STATE_FILE, state, indent=2)
    except OSError as e:
        log(f'save_state failed: {e}', 'WARN')


def _should_re_dm(
    state: dict[str, Any], unit: str,
    now: Optional[datetime] = None, window: timedelta = RE_DM_WINDOW,
    bucket: str = 'units',
) -> bool:
    now = now or datetime.now(timezone.utc)
    entry = state.setdefault(bucket, {}).get(unit)
    if not entry:
        return True
    last_iso = entry.get('last_dm_at')
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - last) >= window


def _record_dm(
    state: dict[str, Any], unit: str, now: Optional[datetime] = None,
    bucket: str = 'units',
) -> None:
    now = now or datetime.now(timezone.utc)
    entry = state.setdefault(bucket, {}).setdefault(unit, {'dm_count': 0})
    entry['dm_count'] = entry.get('dm_count', 0) + 1
    entry['last_dm_at'] = now.isoformat()


def _within_escalate_grace(
    state: dict[str, Any], unit: str, now: Optional[datetime] = None,
    grace: timedelta = ESCALATE_GRACE, bucket: str = 'units',
) -> bool:
    """Record this drift's first sighting and report whether it is still inside
    the escalation grace window (see ESCALATE_GRACE).

    On first sight: stamp `first_seen_at` and return True (defer the page so a
    transient deploy-sync race can clear itself). On a later sight once the
    window has elapsed: return False (the drift persisted — page now). The entry
    is created WITHOUT a `last_dm_at`, so `_should_re_dm` still returns True the
    instant grace lapses. A reconciled unit is dropped from state by the GC, so
    its `first_seen_at` is cleared and a fresh drift restarts the clock."""
    now = now or datetime.now(timezone.utc)
    entry = state.setdefault(bucket, {}).setdefault(unit, {'dm_count': 0})
    # A unit we have already paged (has last_dm_at) is never re-deferred: the
    # window's job — suppress a transient deploy-race before the first page — is
    # moot once a real 🔴 went out. This also covers state written by the
    # pre-grace code, where an in-flight page carries last_dm_at but no
    # first_seen_at; without this it would be silenced for a fresh grace window
    # on upgrade. (Normal re-DM after RE_DM_WINDOW is unaffected — that path
    # already has first_seen_at and would escalate anyway.)
    if entry.get('last_dm_at'):
        return False
    first_iso = entry.get('first_seen_at')
    if not first_iso:
        entry['first_seen_at'] = now.isoformat()
        return True
    try:
        first = datetime.fromisoformat(first_iso)
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
    except ValueError:
        # Corrupt stamp — restart the clock rather than escalate off bad data.
        entry['first_seen_at'] = now.isoformat()
        return True
    return (now - first) < grace


# -------------------- DM --------------------

def dm_larry(
    message: str, subject: str, suggested_action: str,
    severity: str = 'warning', route: str = 'escalate',
) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='heal-systemd-install-drift',
            severity=severity,
            message=message,
            subject=subject,
            suggested_action=suggested_action,
            route=route,
        )
    except Exception as e:
        log(f'dm_larry failed: {type(e).__name__}: {e}', 'WARN')
        return False


def _resolve_install_alert(unit: str) -> int:
    """Retract any stale `install-drift:<unit>` escalate alert for a unit that
    has been reconciled (it left `live_set`). Returns the number of queue lines
    removed (0 = nothing to retract, or a swallowed error).

    The larry-alerts queue is append-only: a 🔴 URGENT manual-dance alert
    emitted by `_render_missing_install` / `_render_content_drift_dry_run`
    (subject `install-drift:<unit>`) stays in the queue forever even after the
    drift resolves out-of-band — the GC below only prunes this healer's internal
    re-DM dedup state, never the emitted alert. `larry_alerts.resolve_alert`
    removes the stale escalate line(s) under the queue flock and keeps the
    beacon/medic line cursors consistent. A non-zero return is the caller's
    proof that a real 🔴 had been delivered, so it can fire a stand-down DM.
    Best-effort: any failure is logged and swallowed so it can never break the
    reconciliation GC."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        removed = la.resolve_alert(
            f'heal-systemd-install-drift:install-drift:{unit}'
        )
        if removed:
            log(f'retracted {removed} stale install-drift alert line(s) for '
                f'{unit} (reconciled out-of-band)')
        return removed or 0
    except Exception as e:  # noqa: BLE001
        log(f'_resolve_install_alert failed for {unit}: '
            f'{type(e).__name__}: {e}', 'WARN')
        return 0


def _classify_route(subject: str, healed: bool) -> str:
    """Delegate to larry_alerts.classify_route (single-source significance).

    Falls back to 'escalate' (fail-loud) if the import fails for any reason."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
        return la.classify_route('heal-systemd-install-drift', subject, healed)
    except Exception as e:
        log(f'_classify_route failed: {type(e).__name__}: {e}', 'WARN')
        return 'escalate'


# -------------------- drift detection --------------------

def list_repo_units(repo_dir: Path = REPO_SYSTEMD_DIR) -> list[str]:
    """Return sorted unit filenames in the repo's systemd/ dir."""
    if not repo_dir.is_dir():
        return []
    return sorted(
        p.name for p in repo_dir.iterdir()
        if p.suffix in ('.service', '.timer') and p.is_file()
    )


def list_installed_units(
    installed_dir: Path = INSTALLED_SYSTEMD_DIR,
) -> set[str]:
    """Return set of installed unit names in /etc/systemd/system/."""
    if not installed_dir.is_dir():
        return set()
    try:
        return {
            p.name for p in installed_dir.iterdir()
            if p.suffix in ('.service', '.timer')
        }
    except OSError as e:
        log(f'list_installed_units error: {e}', 'WARN')
        return set()


def detect_drift(
    repo_dir: Path = REPO_SYSTEMD_DIR,
    installed_dir: Path = INSTALLED_SYSTEMD_DIR,
) -> list[str]:
    """Return sorted list of unit filenames shipped in repo but not installed.

    The reverse case (installed but not in repo) is intentionally ignored —
    that's a removal or a manual operator install, neither of which is a
    drift this healer should police.
    """
    repo_units = list_repo_units(repo_dir)
    installed = list_installed_units(installed_dir)
    return sorted(u for u in repo_units if u not in installed)


def _normalize_unit_content(text: str) -> str:
    """Strip a single trailing newline; otherwise leave the content exact.

    A repo file and its installed copy that differ only by one trailing
    newline are NOT content drift — `cp` and editors routinely add/drop one.
    Any other difference (whitespace, body, a second trailing newline) is
    real and must be caught.
    """
    if text.endswith('\n'):
        return text[:-1]
    return text


def detect_content_drift(
    repo_dir: Path = REPO_SYSTEMD_DIR,
    installed_dir: Path = INSTALLED_SYSTEMD_DIR,
) -> list[str]:
    """Return sorted unit filenames present in BOTH dirs whose installed
    MAIN file content differs from the repo copy.

    Only the main unit file (`<installed_dir>/<unit>`) is compared. Drop-in
    overrides under `<installed_dir>/<unit>.d/` are deliberately NOT read:
    operator live-tuning via `systemctl edit` lands there and must be
    preserved, never clobbered by this healer. A single trailing newline is
    normalized; everything else is compared exactly. Units present in only
    one side are out of scope here (missing-install handles repo-only units).

    Per-unit read failures are logged INFO and the unit is skipped so the
    function never raises.
    """
    repo_units = list_repo_units(repo_dir)
    installed = list_installed_units(installed_dir)
    drifted: list[str] = []
    for unit in repo_units:
        if unit not in installed:
            continue
        try:
            repo_text = (repo_dir / unit).read_text()
            installed_text = (installed_dir / unit).read_text()
        except OSError as e:
            log(
                f'content-drift read of {unit} raised '
                f'{type(e).__name__}: {e}; skipping',
                'INFO',
            )
            continue
        if _normalize_unit_content(repo_text) != _normalize_unit_content(
            installed_text
        ):
            drifted.append(unit)
    return sorted(drifted)


# -------------------- stuck-timer detection --------------------

_STUCK_TIMER_PROPS = (
    'ActiveState',
    'NextElapseUSecRealtime',
    'NextElapseUSecMonotonic',
    'LastTriggerUSec',
    # The service (or other unit) this timer activates. While that unit is
    # itself active/activating, systemd deliberately leaves the timer's next
    # elapse at infinity — that is expected, not a stall (see the triggered-unit
    # guard in detect_stuck_timers).
    'Unit',
)


def _systemctl_show(unit: str) -> Optional[dict[str, str]]:
    """Return parsed KEY=VALUE map from `systemctl show <unit> --property=...`.

    Returns None on shell-out failure or parse error so the caller can skip
    the unit + log without raising.
    """
    try:
        result = subprocess.run(
            [
                'systemctl', 'show', unit,
                '--property=' + ','.join(_STUCK_TIMER_PROPS),
                '--no-pager',
            ],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f'systemctl show {unit} raised {type(e).__name__}: {e}', 'INFO')
        return None
    if result.returncode != 0:
        log(
            f'systemctl show {unit} rc={result.returncode} '
            f'stderr={(result.stderr or "").strip()!r}',
            'INFO',
        )
        return None
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition('=')
        if not sep:
            continue
        props[key.strip()] = value.strip()
    return props


# systemd renders LastTriggerUSec wall-clock with a trailing zone ABBREVIATION
# (e.g. "MDT"), not a numeric offset, so strptime can't recover the offset.
# Map the abbreviations this host actually emits (droplet tz is America/Denver
# per the .timer headers + INSTALL.md) plus the common US zones + UTC to a fixed
# offset, so a parsed trigger carries its true instant. The abbreviation itself
# disambiguates which side of a DST gap/fold the timestamp is on — which is
# exactly what a naive parse threw away (audit #23).
_TZ_ABBREV_OFFSET_HOURS: dict[str, int] = {
    'UTC': 0, 'GMT': 0,
    'EST': -5, 'EDT': -4,
    'CST': -6, 'CDT': -5,
    'MST': -7, 'MDT': -6,
    'PST': -8, 'PDT': -7,
}


def _parse_systemd_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse systemctl's humanized timestamp into a datetime.

    `systemctl show -p LastTriggerUSec` renders e.g. "Mon 2026-06-01 18:00:09
    MDT" (there is no flag that yields epoch for `show`). We drop the leading
    weekday and, when the trailing zone abbreviation is one we recognize,
    attach its fixed UTC offset so the result is timezone-AWARE — an absolute
    instant that compares correctly across a DST boundary. If the zone is
    absent or unrecognized we fall back to a naive datetime (prior behavior),
    and the caller compares it in local wall-clock as before.

    Returns None for empty / 'n/a' / anything unparseable.
    """
    if not value:
        return None
    v = value.strip()
    if not v or v.lower() == 'n/a':
        return None
    parts = v.split()
    # Expect: <weekday> <YYYY-MM-DD> <HH:MM:SS> [<TZ>]
    if len(parts) < 3:
        return None
    try:
        parsed = datetime.strptime(
            f'{parts[1]} {parts[2]}', '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    if len(parts) >= 4:
        off = _TZ_ABBREV_OFFSET_HOURS.get(parts[3].upper())
        if off is not None:
            return parsed.replace(tzinfo=timezone(timedelta(hours=off)))
    return parsed


def _recently_triggered(
    last_trigger: Optional[str],
    now: Optional[datetime] = None,
    grace_s: int = JUST_FIRED_GRACE_S,
) -> Optional[bool]:
    """Did the timer fire within `grace_s` seconds of `now`?

    Returns None when the trigger timestamp can't be parsed, so the caller can
    fall back to its prior behavior rather than silently suppress. The window
    is two-sided (abs) to tolerate sub-second clock skew where a just-fired
    timestamp reads slightly in the future.

    When the parsed trigger is timezone-aware (its zone abbreviation was
    recognized), the comparison is done in absolute time — correct across DST
    transitions (audit #23). `now` defaults to an aware UTC instant; a caller
    supplying a *naive* `now` against an aware trigger is interpreted as
    wall-clock in the trigger's own zone (the matching-zone same-instant case
    the prior naive math assumed). When the trigger is naive (unknown zone), we
    keep the original naive local comparison.
    """
    parsed = _parse_systemd_timestamp(last_trigger)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        ref = now if now is not None else datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=parsed.tzinfo)
        return abs((ref - parsed).total_seconds()) <= grace_s
    # Naive trigger (zone absent/unrecognized): compare in local wall-clock.
    ref = now if now is not None else datetime.now()
    if ref.tzinfo is not None:
        ref = ref.astimezone().replace(tzinfo=None)
    return abs((ref - parsed).total_seconds()) <= grace_s


def _triggered_unit_active_state(unit: str) -> Optional[str]:
    """Return the ActiveState of `unit` (a timer's triggered service), or None.

    Used by detect_stuck_timers to tell an expected infinity anchor (the
    triggered unit is running, so systemd hasn't computed the next elapse) from
    a genuine stall. Wraps the same TimeoutExpired/FileNotFoundError/OSError
    handling as _systemctl_show, returning None on any shell-out failure, rc!=0,
    or absent property so the caller falls back to the prior predicate rather
    than suppressing a real stuck timer on a probe failure.
    """
    try:
        result = subprocess.run(
            [
                'systemctl', 'show', unit,
                '--property=ActiveState',
                '--no-pager',
            ],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'systemctl show {unit} (triggered-unit ActiveState) raised '
            f'{type(e).__name__}: {e}',
            'INFO',
        )
        return None
    if result.returncode != 0:
        log(
            f'systemctl show {unit} (triggered-unit ActiveState) '
            f'rc={result.returncode} stderr={(result.stderr or "").strip()!r}',
            'INFO',
        )
        return None
    for line in result.stdout.splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() == 'ActiveState':
            return value.strip()
    return None


def detect_stuck_timers(
    installed_dir: Path = INSTALLED_SYSTEMD_DIR,
    now: Optional[datetime] = None,
) -> list[dict[str, Optional[str]]]:
    """Return a list of `.timer` units genuinely in the infinity trap.

    Trap predicate: ActiveState=active AND NextElapseUSecRealtime is empty
    AND NextElapseUSecMonotonic=infinity. Both empty-realtime and infinity-
    monotonic are required — either alone is a normal transient state.

    False-positive guard 1 (the just-fired filter): the same infinity-anchor
    reading appears *transiently* when a daemon-reload lands in the sub-second
    window a timer is firing at the top of its period. This bites at 00:00 /
    12:00, when many timers fire at once and this healer issues its own
    daemon-reload — whichever timers are mid-fire get snapshotted anchor-less.
    They are NOT stuck: they fire again on schedule (proven by unbroken 5-min
    trigger history straight through both the 2026-05-31 00:04 and 2026-06-01
    12:00 "incidents"). 8fe7bef's OnUnitActiveSec->OnCalendar conversion did
    NOT fix this — the transient hits OnCalendar timers too. So a timer that
    triggered within JUST_FIRED_GRACE_S is treated as healthy; a genuinely
    wedged timer's last trigger is far older and still classifies as stuck.

    False-positive guard 2 (the triggered-unit-active filter): a `.timer`
    reports NextElapseUSecRealtime empty + NextElapse=infinity for the ENTIRE
    duration its triggered `Unit=` service is active — systemd does not compute
    a next elapse while the triggered unit runs. ourliberty-cycle.service is
    Type=simple and runs a full /cycle (3-20 min, RuntimeMaxSec=1200), so the
    infinity-anchor state persists far past JUST_FIRED_GRACE_S=120: any tick
    landing >120s into a cycle misread the normal running state as stuck (G-rule
    heal-systemd-install-drift-stuck-cycle-timer, ~daily near 06:00Z 2026-07-04..
    07-06). So after the just-fired filter, resolve the timer's triggered unit
    (its `Unit=` prop) and read its ActiveState: while that unit is
    active/activating, the infinity anchor is expected — skip. A genuinely
    wedged timer's triggered unit is inactive/failed/dead, so it still
    classifies as stuck.

    Probe-failure fallback: if the triggered unit is unknown (no `Unit=` prop)
    or its ActiveState can't be read, the guard does NOT suppress — it falls back
    to the prior predicate so a real stuck timer is never hidden by a probe
    failure.

    Per-unit shell-out failures are logged INFO and the unit is skipped;
    the function never raises.
    """
    # Aware UTC instant so the just-fired comparison against an aware,
    # zone-resolved LastTriggerUSec is absolute (DST-correct, audit #23).
    now = now or datetime.now(timezone.utc)
    stuck: list[dict[str, Optional[str]]] = []
    for unit in sorted(list_installed_units(installed_dir)):
        if not unit.endswith('.timer'):
            continue
        props = _systemctl_show(unit)
        if props is None:
            continue
        if props.get('ActiveState') != 'active':
            continue
        if props.get('NextElapseUSecRealtime', '') != '':
            continue
        if props.get('NextElapseUSecMonotonic') != 'infinity':
            continue
        last_trigger = props.get('LastTriggerUSec')
        if _recently_triggered(last_trigger, now=now):
            log(
                f'{unit}: NextElapse=infinity but it fired at '
                f'{last_trigger!r} (within {JUST_FIRED_GRACE_S}s) — transient '
                f'post-fire recompute, not stuck; skipping',
                'INFO',
            )
            continue
        # Triggered-unit-active guard: while the service this timer activates is
        # itself running, the infinity anchor is expected systemd behavior, not
        # a stall. On a probe failure (unknown Unit= / unreadable ActiveState)
        # fall through to classify stuck so a real wedge is never hidden.
        triggered_unit = props.get('Unit')
        if triggered_unit:
            triggered_state = _triggered_unit_active_state(triggered_unit)
            if triggered_state in ('active', 'activating'):
                log(
                    f'{unit}: NextElapse=infinity but its triggered unit '
                    f'{triggered_unit!r} is {triggered_state} — expected while '
                    f'the triggered unit runs, not stuck; skipping',
                    'INFO',
                )
                continue
        stuck.append({
            'unit': unit,
            'last_trigger': last_trigger or None,
        })
    return stuck


def _heal_stuck_timer(unit: str) -> tuple[int, str]:
    """Inline daemon-reload + restart for a stuck timer.

    Mirrors `heal_stale_daemon_code.auto_restart_unit` shape but inlined
    here to avoid a cross-module import dependency between the two healers.
    Returns `(restart_rc, restart_stderr)`; a failed daemon-reload is
    logged WARN and we continue to the restart anyway.
    """
    try:
        reload_result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'daemon-reload'],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
        if reload_result.returncode != 0:
            log(
                f'daemon-reload before stuck-timer restart of {unit} failed '
                f'rc={reload_result.returncode} '
                f'stderr={(reload_result.stderr or "").strip()!r}; '
                f'proceeding with restart anyway',
                'WARN',
            )
    except subprocess.TimeoutExpired:
        log(
            f'daemon-reload before stuck-timer restart of {unit} timed out '
            f'after {RESTART_TIMEOUT_S}s; proceeding with restart anyway',
            'WARN',
        )
    except FileNotFoundError:
        log(
            f'daemon-reload before stuck-timer restart of {unit}: sudo or '
            f'systemctl not found in PATH; proceeding with restart anyway',
            'WARN',
        )

    try:
        result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'restart', unit],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
        return result.returncode, (result.stderr or '').strip()
    except subprocess.TimeoutExpired:
        return -1, f'systemctl restart timed out after {RESTART_TIMEOUT_S}s'
    except FileNotFoundError:
        return -1, 'sudo or systemctl not found in PATH'


# -------------------- remediation allowlist --------------------

def _remediation_allowed(class_name: str) -> bool:
    """Return True iff `class_name` is listed in the allowlist config.

    Fail safe: missing file, JSON parse error, or unexpected shape returns
    False and WARN-logs. Never raises. This is the third gate stacked on
    top of the kill-switch file and the OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED
    env flag — all three must pass before a class takes privileged action.
    """
    try:
        raw = ALLOWLIST_FILE.read_text()
    except FileNotFoundError:
        log(
            f'auto-remediation allowlist missing at {ALLOWLIST_FILE}; '
            f'class {class_name!r} not allowed',
            'WARN',
        )
        return False
    except OSError as e:
        log(
            f'auto-remediation allowlist read error: {type(e).__name__}: {e}; '
            f'class {class_name!r} not allowed',
            'WARN',
        )
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log(
            f'auto-remediation allowlist malformed JSON: {e}; '
            f'class {class_name!r} not allowed',
            'WARN',
        )
        return False
    if not isinstance(data, dict):
        log(
            f'auto-remediation allowlist top-level not an object; '
            f'class {class_name!r} not allowed',
            'WARN',
        )
        return False
    classes = data.get('classes')
    if not isinstance(classes, list) or not all(
        isinstance(c, str) for c in classes
    ):
        log(
            f'auto-remediation allowlist "classes" missing or not a list of '
            f'strings; class {class_name!r} not allowed',
            'WARN',
        )
        return False
    return class_name in classes


# -------------------- shared cp + daemon-reload core --------------------

def _cp_and_reload(unit: str) -> tuple[int, str]:
    """cp the repo unit file into /etc/systemd/system + daemon-reload.

    Shared core of both missing-install and content-drift remediation.
    Returns (rc, stderr); rc==0 means both steps succeeded. Never raises —
    timeout / FileNotFoundError are folded into a non-zero rc + message so
    callers can fall back to the manual-dance alert.
    """
    src = REPO_SYSTEMD_DIR / unit
    try:
        cp_result = subprocess.run(
            ['sudo', '-n', 'cp', str(src), str(INSTALLED_SYSTEMD_DIR) + '/'],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return -1, f'cp {unit} timed out after {RESTART_TIMEOUT_S}s'
    except FileNotFoundError:
        return -1, 'sudo or cp not found in PATH'
    if cp_result.returncode != 0:
        return cp_result.returncode, (
            f'cp failed: {(cp_result.stderr or "").strip()}'
        )

    try:
        reload_result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'daemon-reload'],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return -1, f'daemon-reload after cp of {unit} timed out'
    except FileNotFoundError:
        return -1, 'sudo or systemctl not found in PATH'
    if reload_result.returncode != 0:
        return reload_result.returncode, (
            f'daemon-reload failed: {(reload_result.stderr or "").strip()}'
        )
    return 0, ''


# -------------------- missing-install remediation --------------------

def _remediate_missing_install(unit: str) -> tuple[int, str]:
    """cp the repo unit file into /etc/systemd/system, daemon-reload, then
    ACTIVATE it according to its class. Returns (rc, stderr); never raises.

    Activation (via _activates_via_enable):
      - a .timer, or a standalone long-running daemon that carries its own
        [Install] (e.g. ourliberty-inbox-watcher.service) -> enable --now; verify
        ActiveState=active (timers additionally verify NextElapseUSecRealtime). A
        standalone daemon is NOT started by any timer, so cp+daemon-reload alone
        leaves it installed-but-DEAD — the gap that let this healer report a false
        'install-healed' and de-dup the unit from re-alerting (audit #2).
      - a oneshot service, or a timer-activated service with NO [Install] (e.g.
        ourliberty-cycle.service, started by its sibling .timer) -> cp+daemon-
        reload is enough; `enable` is wrong/fails for them.

    A verification failure flips rc non-zero so the caller falls back to the
    manual-dance alert (and the unit keeps re-alerting) rather than reporting a
    false success.
    """
    rc, stderr = _cp_and_reload(unit)
    if rc != 0:
        return rc, stderr

    if not _activates_via_enable(unit):
        return 0, ''  # oneshot / timer-activated: cp+daemon-reload suffices

    # A .timer or a standalone [Install] daemon: enable --now + verify.
    try:
        enable_result = subprocess.run(
            ['sudo', '-n', 'systemctl', 'enable', '--now', unit],
            capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return -1, f'enable --now {unit} timed out'
    except FileNotFoundError:
        return -1, 'sudo or systemctl not found in PATH'
    if enable_result.returncode != 0:
        return enable_result.returncode, (
            f'enable --now failed: {(enable_result.stderr or "").strip()}'
        )
    post = _systemctl_show(unit) or {}
    if post.get('ActiveState') != 'active':
        return -1, (
            f'post-enable verify failed: ActiveState={post.get("ActiveState")!r}'
        )
    # A timer must additionally have a scheduled next fire — UNLESS it just
    # fired. When a `Persistent=true` timer's `enable --now` catches a missed
    # schedule it fires its service IMMEDIATELY, and for ~1s afterward systemd
    # reports NextElapseUSecRealtime empty / NextElapse=infinity while it
    # recomputes the next elapse. That transient is a SUCCESSFUL install+enable,
    # not a failure (prod 2026-06-10: ourliberty-heal-missions-card-gc.timer was
    # active+firing yet got rc=-1 here, falling back to a 🔴 URGENT manual-dance
    # alert). Reuse the exact just-fired grace detect_stuck_timers already
    # applies (_recently_triggered against the just-fetched LastTriggerUSec) so a
    # timer that fired within JUST_FIRED_GRACE_S is not treated as a verify
    # failure. An unparseable / absent LastTriggerUSec makes _recently_triggered
    # return None (falsy) → we keep the fail-loud -1, never a silent false pass.
    if unit.endswith('.timer') and not post.get('NextElapseUSecRealtime'):
        if not _recently_triggered(
            post.get('LastTriggerUSec'), grace_s=JUST_FIRED_GRACE_S,
        ):
            return -1, 'post-enable verify failed: NextElapseUSecRealtime empty'
        # else: just-fired transient — the install genuinely succeeded.

    return 0, ''


# -------------------- content-drift remediation --------------------

_SERVICE_CLASS_PROPS = ('Type', 'ActiveState')


def _service_is_long_running(unit: str) -> bool:
    """True iff `unit` is a currently-active, non-oneshot `.service`.

    Decides whether a content-drifted service needs a restart to pick up its
    new ExecStart *now* (a long-running daemon) vs. a daemon-reload being
    enough (a oneshot — its next timer fire re-execs it with fresh content,
    and restarting a oneshot would run it off-schedule). On any shell-out or
    parse failure return False: the safe default never restarts.
    """
    try:
        result = subprocess.run(
            [
                'systemctl', 'show', unit,
                '--property=' + ','.join(_SERVICE_CLASS_PROPS),
                '--no-pager',
            ],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(
            f'systemctl show {unit} (service class) raised '
            f'{type(e).__name__}: {e}; treating as not-long-running',
            'INFO',
        )
        return False
    if result.returncode != 0:
        return False
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition('=')
        if sep:
            props[key.strip()] = value.strip()
    if props.get('Type') == 'oneshot':
        return False
    return props.get('ActiveState') == 'active'


def _remediate_content_drift(unit: str) -> tuple[int, str]:
    """Re-install a content-drifted unit. Returns (rc, stderr); never raises.

    Shared core: cp repo->installed + daemon-reload (via `_cp_and_reload`).
    Then, by type:
      .timer  — daemon-reload alone will NOT reset an already-active timer's
                OnUnitActiveSec anchor, so `systemctl restart <unit>` re-anchors
                it; verify ActiveState=active + NextElapseUSecRealtime populated
                (verification failure flips rc non-zero -> manual-dance fallback).
      .service — a oneshot needs only the daemon-reload (its next timer fire
                re-execs with the new ExecStart); we do NOT enable/start it. A
                currently long-running .service is restarted so the new content
                takes effect now.
    """
    rc, stderr = _cp_and_reload(unit)
    if rc != 0:
        return rc, stderr

    if unit.endswith('.timer'):
        try:
            restart_result = subprocess.run(
                ['sudo', '-n', 'systemctl', 'restart', unit],
                capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return -1, f'restart {unit} timed out'
        except FileNotFoundError:
            return -1, 'sudo or systemctl not found in PATH'
        if restart_result.returncode != 0:
            return restart_result.returncode, (
                f'restart failed: {(restart_result.stderr or "").strip()}'
            )
        post = _systemctl_show(unit) or {}
        if post.get('ActiveState') != 'active':
            return -1, (
                f'post-restart verify failed: ActiveState='
                f'{post.get("ActiveState")!r}'
            )
        if not post.get('NextElapseUSecRealtime'):
            return -1, (
                'post-restart verify failed: NextElapseUSecRealtime empty'
            )
        return 0, ''

    if unit.endswith('.service') and _service_is_long_running(unit):
        try:
            restart_result = subprocess.run(
                ['sudo', '-n', 'systemctl', 'restart', unit],
                capture_output=True, text=True, timeout=RESTART_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return -1, f'restart {unit} timed out'
        except FileNotFoundError:
            return -1, 'sudo or systemctl not found in PATH'
        if restart_result.returncode != 0:
            return restart_result.returncode, (
                f'restart failed: {(restart_result.stderr or "").strip()}'
            )

    return 0, ''


# -------------------- DM rendering --------------------

def _classify_unit(unit: str, repo_dir: Path = REPO_SYSTEMD_DIR) -> str:
    """Classify a repo unit into one of three remediation classes.

    Returns one of:
      'timer'        — a `.timer` unit. Enable/restart the timer; existing
                       timer remediation copy is correct.
      'oneshot'      — a `.service` with `Type=oneshot`. A daemon-reload is
                       enough: the unit re-execs with fresh content on its next
                       timer fire, and restarting it would run it off-schedule.
      'long-running' — a `.service` that stays resident (`Type=simple`/
                       `notify`/`forking`/`exec`/`idle`/`dbus`, or no explicit
                       `Type=`, which systemd defaults to `simple`). A
                       daemon-reload alone NEVER restarts it, so remediation
                       MUST `systemctl restart`/`enable --now` to load new
                       content and there is no "next fire" to re-exec it.

    The `Type=` is parsed from the *repo* copy of the unit file (the last
    `Type=` assignment wins, matching systemd). On any read failure a
    `.service` falls back to 'long-running' — the safe default, since
    over-restarting a daemon is harmless but failing to restart one leaves it
    on stale code (the service-restart-after-merge gap this healer exists to
    close).
    """
    if unit.endswith('.timer'):
        return 'timer'
    unit_type: Optional[str] = None
    try:
        for line in (repo_dir / unit).read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith('Type='):
                unit_type = stripped.split('=', 1)[1].strip()
    except OSError as e:
        log(
            f'_classify_unit read of {unit} raised {type(e).__name__}: {e}; '
            f'defaulting to long-running',
            'INFO',
        )
        return 'long-running'
    return 'oneshot' if unit_type == 'oneshot' else 'long-running'


def _unit_has_install_section(unit: str, repo_dir: Path = REPO_SYSTEMD_DIR) -> bool:
    """True iff the repo copy of `unit` carries an ``[Install]`` section.

    ``systemctl enable`` REQUIRES an ``[Install]`` section, so this distinguishes
    a standalone daemon (e.g. ourliberty-inbox-watcher.service — has [Install]
    WantedBy=multi-user.target, no sibling timer, MUST be enable --now'd) from a
    timer-activated service (e.g. ourliberty-cycle.service — Type=simple, NO
    [Install]; its sibling .timer carries the [Install] and starts it, so
    `enable` would fail with 'no install target'). Read failure -> False (never
    attempt an enable we can't justify; cp+daemon-reload is the safe default)."""
    try:
        for line in (repo_dir / unit).read_text().splitlines():
            if line.strip().lower() == '[install]':
                return True
    except OSError:
        return False
    return False


def _activates_via_enable(unit: str) -> bool:
    """True iff bringing `unit` up should be done with ``enable --now``: a
    .timer, or a long-running .service that carries its own [Install] (standalone
    daemon). oneshot services and timer-activated (no-[Install]) services come up
    via cp+daemon-reload alone — `enable` is wrong (or fails) for them."""
    if unit.endswith('.timer'):
        return True
    if _classify_unit(unit) != 'long-running':
        return False  # oneshot
    return _unit_has_install_section(unit)


def _render_stuck_timer_heal(unit: str, next_fire: str) -> tuple[str, str, str]:
    message = (
        f'Auto-healed stuck timer `{unit}`. Trap: `NextElapseUSecRealtime` '
        f'empty + `NextElapseUSecMonotonic=infinity` (timer never fires). '
        f'Recovery: daemon-reload + restart. Next fire now: {next_fire}.'
    )
    # Distinct subject from the dry-run/manual `stuck-timer:` so the healed
    # outcome and the manual-action copy are separate translation entries
    # (fix-first routing: healed events carry no imperative).
    subject = f'stuck-timer-healed:{unit}'
    suggested = (
        f'Verify on the droplet: '
        f'`systemctl show {unit} --property=NextElapseUSecRealtime`.'
    )
    return message, subject, suggested


def _render_stuck_timer_dry_run(unit: str) -> tuple[str, str, str]:
    message = (
        f'Stuck timer detected: `{unit}`. `NextElapseUSecRealtime` empty + '
        f'`NextElapseUSecMonotonic=infinity`. Manual recovery: '
        f'`sudo systemctl daemon-reload && sudo systemctl restart {unit}`.'
    )
    subject = f'stuck-timer:{unit}'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80):\n'
        f'  sudo systemctl daemon-reload\n'
        f'  sudo systemctl restart {unit}\n'
        f'Then verify: '
        f'`systemctl show {unit} --property=NextElapseUSecRealtime`.'
    )
    return message, subject, suggested


def _render_install_healed(unit: str, next_fire: str) -> tuple[str, str, str]:
    # Describe what _remediate_missing_install actually did for this unit class.
    if unit.endswith('.timer'):
        activation = f'enabled --now. Next fire: {next_fire}.'
    elif _activates_via_enable(unit):  # standalone [Install] daemon
        activation = 'enabled --now and verified active (running).'
    elif _classify_unit(unit) == 'oneshot':
        activation = 'left to re-exec with the new content on its next timer fire.'
    else:  # long-running service activated by its sibling timer (no [Install])
        activation = 'left for its sibling timer to start on the next fire.'
    message = (
        f'Auto-installed `{unit}` — it was shipped in the repo but missing '
        f'from /etc/systemd/system/. Installed, daemon-reloaded, and '
        f'{activation}'
    )
    # Distinct subject from the failed/manual `install-drift:` so the healed
    # outcome and the manual install-dance copy are separate translation
    # entries (fix-first routing: healed events carry no imperative).
    subject = f'install-healed:{unit}'
    suggested = f'Verify on the droplet: `systemctl status {unit}`.'
    return message, subject, suggested


def _render_missing_install(unit: str) -> tuple[str, str, str]:
    repo_path = f'~/agent-core/systemd/{unit}'
    # cp + daemon-reload is the common core. Only a .timer or a standalone
    # [Install] daemon needs a third `enable --now` step; an oneshot or a
    # timer-activated (no-[Install]) service is brought up by its sibling timer,
    # so daemon-reload IS the whole dance — fold the explanation onto the reload
    # line as a comment rather than emitting a second, redundant daemon-reload.
    if _activates_via_enable(unit):  # timer or standalone [Install] daemon
        reload_line = 'sudo systemctl daemon-reload'
        enable_line = (
            f'sudo systemctl enable --now {unit}  '
            f'# enable --now: starts it now + at boot'
        )
    elif _classify_unit(unit) == 'oneshot':
        reload_line = (
            'sudo systemctl daemon-reload  '
            '# oneshot: its sibling timer re-execs it on the next fire — no enable needed'
        )
        enable_line = None
    else:  # long-running service activated by its sibling timer (no [Install])
        reload_line = (
            'sudo systemctl daemon-reload  '
            '# its sibling timer starts it on the next fire — no enable needed'
        )
        enable_line = None
    dance = [
        f'  sudo cp {repo_path} /etc/systemd/system/',
        f'  {reload_line}',
    ]
    if enable_line:
        dance.append(f'  {enable_line}')
    message = (
        f'Unit `{unit}` is shipped in the repo (`systemd/{unit}`) but is not '
        f'installed under `/etc/systemd/system/`. Likely cause: the PR that '
        f'added it merged without the operator running the install dance. '
        f'Without install, the timer never fires.'
    )
    subject = f'install-drift:{unit}'
    suggested = (
        'On the droplet (ssh larry@134.209.44.80):\n'
        + '\n'.join(dance)
        + f'\nThen verify: `systemctl status {unit}`'
    )
    return message, subject, suggested


def _render_content_healed(unit: str, next_fire: str) -> tuple[str, str, str]:
    unit_class = _classify_unit(unit)
    if unit_class == 'timer':
        action_phrase = 'restarted to re-anchor its schedule'
        tail = f' Next fire: {next_fire}.'
    elif unit_class == 'oneshot':
        action_phrase = (
            'daemon-reloaded (next timer fire re-execs with the new content)'
        )
        tail = f' Next fire: {next_fire}.'
    else:  # long-running daemon — no "next fire"; it had to be restarted now
        action_phrase = 'daemon-reloaded and restarted to load the new content'
        tail = ''
    message = (
        f'Auto-reconciled `{unit}` — its installed copy under '
        f'/etc/systemd/system/ had drifted from the repo. Re-copied, '
        f'daemon-reloaded, and {action_phrase}.{tail}'
    )
    # Distinct subject from the failed/manual `install-drift:` so a healthy
    # auto-reconcile routes to its own no-action translation entry rather than
    # the imperative "run the install dance" copy (fix-first routing: healed
    # events carry no imperative). Mirrors `install-healed:` / `stuck-timer-healed:`.
    subject = f'content-healed:{unit}'
    suggested = f'Verify on the droplet: `systemctl status {unit}`.'
    return message, subject, suggested


def _render_content_drift_dry_run(unit: str) -> tuple[str, str, str]:
    repo_path = f'~/agent-core/systemd/{unit}'
    unit_class = _classify_unit(unit)
    if unit_class == 'timer':
        post_line = f'sudo systemctl restart {unit}  # re-anchor the timer'
    elif unit_class == 'oneshot':
        post_line = (
            'sudo systemctl daemon-reload  '
            '# oneshot re-execs on its next timer fire'
        )
    else:  # long-running daemon — daemon-reload won't pick up new content
        post_line = (
            f'sudo systemctl restart {unit}  '
            f'# long-running daemon — daemon-reload alone keeps it on stale code'
        )
    message = (
        f'Unit `{unit}` is installed under `/etc/systemd/system/` but its '
        f'contents differ from the repo copy (`systemd/{unit}`). Likely '
        f'cause: a PR changed the unit file but the operator did not '
        f're-install it, so the droplet is running stale unit config.'
    )
    subject = f'install-drift:{unit}'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80):\n'
        f'  sudo cp {repo_path} /etc/systemd/system/\n'
        f'  sudo systemctl daemon-reload\n'
        f'  {post_line}\n'
        f'Then verify: `systemctl status {unit}`'
    )
    return message, subject, suggested


def _render_install_resolved(unit: str) -> tuple[str, str, str]:
    """Stand-down copy for a previously-PAGED install-drift that has since been
    reconciled. The queue retraction (`_resolve_install_alert`) removes the stale
    line, but it cannot un-send the 🔴 DM already on Larry's phone — this one-line
    closure does that. Emitted only when a real escalate line was actually
    removed (proof the 🔴 went out), so it never fires for a drift that was
    deferred-and-resolved inside the grace window (which never paged)."""
    message = (
        f'Stand down — `{unit}` is now installed under /etc/systemd/system/ and '
        f'the earlier 🔴 install-drift alert has been retracted. The drift '
        f'reconciled (post-merge install completed / out-of-band fix); no action '
        f'needed.'
    )
    # Distinct healed-style subject so it routes to its own no-imperative
    # translation entry, like install-healed:/content-healed:.
    subject = f'install-resolved:{unit}'
    suggested = f'Verify on the droplet: `systemctl status {unit}`.'
    return message, subject, suggested


def _activation_message() -> tuple[str, str, str]:
    message = (
        f'Heal-systemd-install-drift is in dry-run mode '
        f'(`{ENV_HEALER_ENABLED}` is not set). It detected at least one '
        f'systemd unit shipped in the repo but not installed on the droplet, '
        f'but did not send per-unit DMs.'
    )
    subject = 'install-drift-healer: activate to receive missing-install alerts'
    suggested = (
        f'On the droplet (ssh larry@134.209.44.80): '
        f'`sudo systemctl edit ourliberty-heal-systemd-install-drift.service`, '
        f'add `Environment="{ENV_HEALER_ENABLED}=true"` under `[Service]`, '
        f'then `sudo systemctl restart '
        f'ourliberty-heal-systemd-install-drift.timer`.'
    )
    return message, subject, suggested


# -------------------- main --------------------

def run_once(
    repo_dir: Optional[Path] = None,
    installed_dir: Optional[Path] = None,
    state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    dry_run_override: Optional[bool] = None,
) -> dict[str, int]:
    """Single-tick orchestration."""
    counts = {
        'missing_install': 0, 'dm_sent': 0,
        'dm_suppressed_dedup': 0, 'dm_deferred_grace': 0,
        'reconciled_gc': 0, 'stand_down': 0,
        'stuck_timer': 0, 'timer_healed': 0,
        'install_healed': 0,
        'content_drift': 0, 'content_healed': 0,
    }
    if kill_switch_active():
        log(f'KILL_SWITCH active at {KILL_SWITCH}; exiting cleanly')
        return counts
    heartbeat()

    dry_run = (
        dry_run_override
        if dry_run_override is not None
        else not healer_enabled()
    )

    state_was_none = state is None
    if state is None:
        state = load_state()

    drifts = detect_drift(
        repo_dir or REPO_SYSTEMD_DIR,
        installed_dir or INSTALLED_SYSTEMD_DIR,
    )
    counts['missing_install'] = len(drifts)
    live_set = set(drifts)

    for unit in drifts:
        if not _should_re_dm(state, unit, now=now):
            counts['dm_suppressed_dedup'] += 1
            continue

        if dry_run:
            state.setdefault('_meta', {})
            if not state['_meta'].get('activation_alerted'):
                a_msg, a_subj, a_sug = _activation_message()
                dm_larry(message=a_msg, subject=a_subj, suggested_action=a_sug)
                state['_meta']['activation_alerted'] = True
                counts['dm_sent'] += 1
            log(f'DRY-RUN install drift: {unit} (suppressed; activate via '
                f'{ENV_HEALER_ENABLED}=true)')
            continue

        if _remediation_allowed('install-drift'):
            rc, stderr = _remediate_missing_install(unit)
            if rc == 0:
                post = _systemctl_show(unit) or {}
                next_fire = post.get('NextElapseUSecRealtime') or 'unknown'
                counts['install_healed'] += 1
                msg, subj, sug = _render_install_healed(unit, next_fire)
                route = _classify_route(subj, healed=True)
                ok = dm_larry(message=msg, subject=subj, suggested_action=sug,
                              severity='info', route=route)
                if ok:
                    counts['dm_sent'] += 1
                    _record_dm(state, unit, now=now)
                    log(f'auto-installed {unit} next_fire={next_fire!r}')
                else:
                    log(f'DM append suppressed for healed install {unit}', 'WARN')
                continue
            log(
                f'auto-install of {unit} failed rc={rc} stderr={stderr!r}; '
                f'falling back to manual-dance alert',
                'WARN',
            )

        # Escalation grace: an auto-install that was disabled or just failed is
        # not paged on first sight — a freshly-merged unit the post-merge trigger
        # caught mid-deploy clears within minutes (deploy finishes installing it)
        # and is GC'd below, never DMing. Only a drift still present past the
        # window pages.
        if _within_escalate_grace(state, unit, now=now):
            counts['dm_deferred_grace'] += 1
            log(f'missing install {unit}: within escalation grace '
                f'({ESCALATE_GRACE}); deferring manual-dance DM '
                f'(transient deploy-sync race clears itself)')
            continue

        msg, subj, sug = _render_missing_install(unit)
        ok = dm_larry(message=msg, subject=subj, suggested_action=sug)
        if ok:
            counts['dm_sent'] += 1
            _record_dm(state, unit, now=now)
            log(f'DM sent for missing install: {unit}')
        else:
            log(f'DM append suppressed for {unit}', 'WARN')

    # Content-drift pass — units present in BOTH dirs whose installed file
    # content differs from the repo copy. Runs AFTER missing-install (a unit
    # absent from the droplet cannot be content-drifted). Same install-drift
    # allowlist + dry-run + kill-switch + _should_re_dm gating; shares the
    # `units` state bucket, so both feed the single reconciliation GC below.
    content_drifts = detect_content_drift(
        repo_dir or REPO_SYSTEMD_DIR,
        installed_dir or INSTALLED_SYSTEMD_DIR,
    )
    counts['content_drift'] = len(content_drifts)
    live_set |= set(content_drifts)

    for unit in content_drifts:
        if not _should_re_dm(state, unit, now=now):
            counts['dm_suppressed_dedup'] += 1
            continue

        if dry_run:
            state.setdefault('_meta', {})
            if not state['_meta'].get('activation_alerted'):
                a_msg, a_subj, a_sug = _activation_message()
                dm_larry(message=a_msg, subject=a_subj, suggested_action=a_sug)
                state['_meta']['activation_alerted'] = True
                counts['dm_sent'] += 1
            log(f'DRY-RUN content drift: {unit} (suppressed; activate via '
                f'{ENV_HEALER_ENABLED}=true)')
            continue

        if _remediation_allowed('install-drift'):
            rc, stderr = _remediate_content_drift(unit)
            if rc == 0:
                post = _systemctl_show(unit) or {}
                next_fire = post.get('NextElapseUSecRealtime') or 'unknown'
                counts['content_healed'] += 1
                msg, subj, sug = _render_content_healed(unit, next_fire)
                route = _classify_route(subj, healed=True)
                ok = dm_larry(message=msg, subject=subj, suggested_action=sug,
                              route=route)
                if ok:
                    counts['dm_sent'] += 1
                    _record_dm(state, unit, now=now)
                    log(f'auto-reconciled {unit} next_fire={next_fire!r}')
                else:
                    log(f'DM append suppressed for healed content {unit}',
                        'WARN')
                continue
            log(
                f'auto-reconcile of {unit} failed rc={rc} stderr={stderr!r}; '
                f'falling back to manual-dance alert',
                'WARN',
            )

        # Same escalation grace as missing-install: a content "differ" the
        # post-merge trigger sees mid-deploy (old file on disk, new copy not yet
        # synced) reconciles within minutes, so defer the page until the drift
        # persists past the window.
        if _within_escalate_grace(state, unit, now=now):
            counts['dm_deferred_grace'] += 1
            log(f'content drift {unit}: within escalation grace '
                f'({ESCALATE_GRACE}); deferring manual-dance DM '
                f'(transient deploy-sync race clears itself)')
            continue

        msg, subj, sug = _render_content_drift_dry_run(unit)
        ok = dm_larry(message=msg, subject=subj, suggested_action=sug)
        if ok:
            counts['dm_sent'] += 1
            _record_dm(state, unit, now=now)
            log(f'DM sent for content drift: {unit}')
        else:
            log(f'DM append suppressed for {unit}', 'WARN')

    # GC entries that have been reconciled (missing-install OR content drift).
    # A reconciled unit that was previously alerted may have left a stale 🔴
    # escalate line in the append-only queue; retract it so the operator's queue
    # clears with the drift (defense-in-depth alongside the just-fired-grace fix
    # that stops the false alert being emitted in the first place).
    inst_dir = installed_dir or INSTALLED_SYSTEMD_DIR
    for gone in list(state['units'].keys()):
        if gone not in live_set:
            state['units'].pop(gone, None)
            counts['reconciled_gc'] += 1
            if not dry_run:
                removed = _resolve_install_alert(gone)
                # A non-zero removal means a real 🔴 install-drift line was in
                # the queue (so it had been paged), and it is now reconciled.
                # The retraction clears the queue but cannot un-send the DM on
                # Larry's phone — fire a one-line stand-down so the alert he saw
                # visibly closes. Never fires for a grace-deferred drift (which
                # never paged → nothing to remove → removed == 0).
                #
                # Gate on the unit being genuinely installed now: a unit can also
                # leave live_set because it was DELETED from the repo, not
                # installed. The stale 🔴 is still retracted above (correct), but
                # a "now installed, stand down" DM would be a false all-clear, so
                # skip it when the unit is absent from /etc/systemd/system/.
                if removed and (inst_dir / gone).exists():
                    counts['stand_down'] += 1
                    s_msg, s_subj, s_sug = _render_install_resolved(gone)
                    dm_larry(message=s_msg, subject=s_subj,
                             suggested_action=s_sug, route='closure')

    # Stuck-timer pass — independent of the missing-install loop. The
    # _should_re_dm cooldown (separate `stuck_timers` state bucket) throttles
    # only the NOTIFICATION, never the remediation: in enabled mode we restart
    # a wedged timer on every detection. The daemon-reload+restart is
    # idempotent and cheap, and a dead timer must not stay dead just because we
    # already DM'd about it inside the RE_DM_WINDOW. (Prior bug: the cooldown
    # gated the heal itself, so a timer still/again stuck on the next 12h tick
    # was suppressed and left unhealed — the cadence and the window are both
    # 12h, so this bit routinely.)
    stuck = detect_stuck_timers(installed_dir or INSTALLED_SYSTEMD_DIR)
    counts['stuck_timer'] = len(stuck)
    stuck_live = set()
    for entry in stuck:
        unit = entry['unit']
        if not isinstance(unit, str):
            continue
        stuck_live.add(unit)

        if dry_run:
            # Dry-run never restarts, so the cooldown legitimately gates the
            # advisory DM (its only side effect).
            if not _should_re_dm(state, unit, now=now, bucket='stuck_timers'):
                counts['dm_suppressed_dedup'] += 1
                continue
            msg, subj, sug = _render_stuck_timer_dry_run(unit)
            ok = dm_larry(message=msg, subject=subj, suggested_action=sug)
            if ok:
                counts['dm_sent'] += 1
                _record_dm(state, unit, now=now, bucket='stuck_timers')
                log(f'DRY-RUN stuck timer: {unit} (DM sent; no restart attempted)')
            else:
                log(f'DM append suppressed for stuck timer {unit}', 'WARN')
            continue

        # Enabled mode: always remediate on detection.
        rc, stderr = _heal_stuck_timer(unit)
        if rc != 0:
            log(
                f'stuck-timer restart of {unit} failed rc={rc} '
                f'stderr={stderr!r}',
                'WARN',
            )
            continue
        post = _systemctl_show(unit) or {}
        next_fire = post.get('NextElapseUSecRealtime') or 'unknown'
        counts['timer_healed'] += 1
        log(f'auto-healed stuck timer: {unit} next_fire={next_fire!r}')

        # Throttle only the closure DM, not the heal above.
        if not _should_re_dm(state, unit, now=now, bucket='stuck_timers'):
            counts['dm_suppressed_dedup'] += 1
            continue
        msg, subj, sug = _render_stuck_timer_heal(unit, next_fire)
        route = _classify_route(subj, healed=True)
        ok = dm_larry(message=msg, subject=subj, suggested_action=sug,
                      route=route)
        if ok:
            counts['dm_sent'] += 1
            _record_dm(state, unit, now=now, bucket='stuck_timers')
        else:
            log(f'DM append suppressed for healed timer {unit}', 'WARN')

    # GC reconciled stuck-timer entries.
    for gone in list(state.get('stuck_timers', {}).keys()):
        if gone not in stuck_live:
            state['stuck_timers'].pop(gone, None)
            counts['reconciled_gc'] += 1

    log(f'tick: dry_run={dry_run} '
        + ' '.join(f'{k}={v}' for k, v in counts.items() if v))

    if state_was_none:
        save_state(state)
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `--triggered` (alias `--once`) is the post-merge sync hook: sync_agent_core.sh
    # invokes it within one sync cycle (<=1h) when a merged commit touched a
    # systemd/*.service|*.timer, so a new/changed unit installs without waiting for
    # the 12h timer. It runs exactly ONE run_once() tick and honors every gate
    # unchanged (kill-switch, install-drift allowlist, healer_enabled env, re-DM
    # dedup) — there is no dry_run_override, so OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED
    # still decides remediate-vs-dry-run. The distinct log line keeps triggered runs
    # auditable against the timer runs. The 12h timer remains the backstop.
    triggered = bool(argv) and argv[0] in ('--triggered', '--once')
    try:
        if triggered:
            log('triggered run (post-merge sync hook): single tick')
        run_once()
        return 0
    except Exception as e:  # noqa: BLE001
        log(f'FATAL: {type(e).__name__}: {e}', 'ERROR')
        return 1


if __name__ == '__main__':
    sys.exit(main())
