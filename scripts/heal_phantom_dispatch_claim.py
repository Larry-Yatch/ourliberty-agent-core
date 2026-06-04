#!/usr/bin/env python3
"""heal_phantom_dispatch_claim.py — catch dispatch-claims that never dispatched.

On 2026-06-03 Beacon DM'd Larry "Approved — `deploy-notifier-ready-logonly`
dispatches to Forge now." It never did: 15+ min later there was NO Forge inbox
envelope, NO in-flight worktree, NO outbox-notifier translation, NO approval
record. Beacon asserted an action in conversational prose without the canonical
marker that actually performs it, and nothing detected the discrepancy — the
approved work silently stranded until Larry asked. Same marker-vs-prose disease
as the Mirror auto-merge drift, but at the dispatch-CONFIRMATION step.

This watcher reads Beacon's outbound message log (the bot's
`-> <chat>: <repr>` lines), finds messages that CLAIM a Forge dispatch and name
a task_id, and after a grace window verifies that a real dispatch artifact
exists. If none does, it escalates a larry-alert so the stranded work surfaces.

DETECTION ONLY. Re-constructing an envelope from chat prose is unsafe, so this
never re-dispatches; it tells Larry to verify + re-dispatch. The stronger
structural fix (have the dispatch path emit the authoritative "dispatched" DM so
the word can only appear when a real envelope was written) is a follow-up, not
this build. Stdlib only.

Key formats this depends on (verified live 2026-06-03):
  - Beacon outbound line: `[%Y-%m-%dT%H:%M:%S%z] -> <chat_id>: <repr(reply[:120])>`
    (beacon_telegram_bot.py log() + send path). The payload is a Python repr of
    the FIRST 120 CHARS of the reply, so it is a single line and may be cut
    mid-sentence — the claim phrase and backtick-quoted task_id both land early
    enough to survive in practice.
  - Real-dispatch evidence: a Forge inbox file `<task>.json` / `build-<task>.json`
    (live / .archive / .invalid), a state/in-flight/<task>.json, an active
    worktree wt-forge-<task>, or an outbox-notifier.log line naming the task.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CONFIG_FILE = (
    Path(__file__).resolve().parent.parent
    / 'config' / 'phantom-dispatch-claim-patterns.json'
)

DEFAULT_GRACE_MINUTES = 10
DEFAULT_SCAN_WINDOW_MINUTES = 30
DEFAULT_MIN_BARE_KEBAB_HYPHENS = 2

# An outbound line: `[<ts>] -> <chat_id>: <repr-payload>`. The `] -> <digits>: `
# shape deliberately excludes the bot's own `approved <task> -> dispatched to
# <path>` confirmation lines (those have no `-> <digits>:`), so only true
# chat-reply DMs are scanned.
_OUTBOUND_RE = re.compile(r'^\[([^\]]+)\] -> (\d+): (.*)$')
_TS_FMT = '%Y-%m-%dT%H:%M:%S%z'

# A backtick-quoted token is the strongest task_id signal (kebab-case, lower).
_BACKTICK_RE = re.compile(r'`([a-z0-9][a-z0-9-]*[a-z0-9])`')


# ---------- path resolution (all env-overridable for hermetic tests) ----------


def agents_root() -> Path:
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def log_dir() -> Path:
    # Match beacon_telegram_bot.resolve_log_dir(): OURLIBERTY_LOG_DIR wins, else
    # <agents_root>/logs. Tests set OURLIBERTY_LOG_DIR to a temp dir so they
    # never read the live log.
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    return Path(override) if override else agents_root() / 'logs'


def worktrees_root() -> Path:
    # Production worktrees live under ~/agent-worktrees (NOT under agents_root),
    # so this needs its own override rather than riding on OURLIBERTY_AGENTS_ROOT.
    override = os.environ.get('OURLIBERTY_WORKTREES_ROOT')
    return Path(override) if override else Path.home() / 'agent-worktrees'


def blackboard() -> Path:
    return agents_root() / 'blackboard'


def kill_switch() -> Path:
    return agents_root() / 'healers.disabled'


def healer_heartbeat() -> Path:
    return blackboard() / 'heal-phantom-dispatch-claim.heartbeat'


def alerted_state_file() -> Path:
    return blackboard() / 'phantom-dispatch-claim-alerted.json'


def beacon_log_file() -> Path:
    return log_dir() / 'beacon_telegram_bot.log'


def outbox_log_file() -> Path:
    return log_dir() / 'outbox-notifier.log'


def forge_inbox_dir() -> Path:
    return agents_root() / 'inboxes' / 'forge'


def in_flight_dir() -> Path:
    return agents_root() / 'state' / 'in-flight'


def healer_log_file() -> Path:
    return agents_root() / 'logs' / 'heal-phantom-dispatch-claim.log'


# ---------- logging / heartbeat ----------


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        lf = healer_log_file()
        lf.parent.mkdir(parents=True, exist_ok=True)
        with open(lf, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def heartbeat() -> None:
    try:
        hb = healer_heartbeat()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(
            json.dumps({'ts': datetime.now(timezone.utc).isoformat()})
        )
    except OSError:
        pass


# ---------- config ----------


def load_config(path: Optional[Path] = None) -> dict:
    """Return the pattern config. Raises on unreadable / shape-invalid JSON so
    main() can fail loud (alert + exit) rather than silently monitoring with an
    empty pattern set."""
    cfg_path = path or CONFIG_FILE
    data = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('phantom-dispatch config is not a JSON object')
    if not isinstance(data.get('dispatch_claim_phrases'), list) \
            or not data['dispatch_claim_phrases']:
        raise ValueError("config missing a non-empty 'dispatch_claim_phrases'")
    return data


def _grace_sec(cfg: dict) -> float:
    val = cfg.get('grace_minutes', DEFAULT_GRACE_MINUTES)
    return (val if isinstance(val, (int, float)) and val > 0
            else DEFAULT_GRACE_MINUTES) * 60.0


def _scan_window_sec(cfg: dict) -> float:
    val = cfg.get('scan_window_minutes', DEFAULT_SCAN_WINDOW_MINUTES)
    return (val if isinstance(val, (int, float)) and val > 0
            else DEFAULT_SCAN_WINDOW_MINUTES) * 60.0


# ---------- line parsing ----------


def parse_outbound_line(line: str) -> Optional[tuple[float, str, str]]:
    """Parse one Beacon outbound log line.

    Returns (claim_epoch, chat_id, message_text) or None if the line is not an
    outbound DM. The message payload is a Python repr of the bot's truncated
    reply; ast.literal_eval recovers the (truncated) text. On a repr that won't
    evaluate, fall back to stripping one matching pair of outer quotes so a
    slightly odd line still yields searchable text rather than being dropped."""
    m = _OUTBOUND_RE.match(line.rstrip('\n'))
    if not m:
        return None
    ts_str, chat_id, payload = m.group(1), m.group(2), m.group(3)
    try:
        claim_epoch = datetime.strptime(ts_str, _TS_FMT).timestamp()
    except ValueError:
        return None
    text = _decode_payload(payload)
    if text is None:
        return None
    return claim_epoch, chat_id, text


def _decode_payload(payload: str) -> Optional[str]:
    payload = payload.strip()
    if not payload:
        return None
    try:
        value = ast.literal_eval(payload)
        if isinstance(value, str):
            return value
    except (ValueError, SyntaxError):
        pass
    if len(payload) >= 2 and payload[0] == payload[-1] and payload[0] in '\'"':
        return payload[1:-1]
    return payload


# ---------- claim detection ----------


def _matches_any(text_lower: str, phrases) -> bool:
    return any(
        isinstance(p, str) and p and p.lower() in text_lower
        for p in phrases
    )


def is_dispatch_claim(text: str, cfg: dict) -> bool:
    return _matches_any(text.lower(), cfg.get('dispatch_claim_phrases', []))


def is_strong_claim(text: str, cfg: dict) -> bool:
    return _matches_any(text.lower(), cfg.get('strong_dispatch_phrases', []))


def extract_task_id(text: str, cfg: dict) -> Optional[str]:
    """Best-effort task_id. Prefer a backtick-quoted kebab token; else a bare
    kebab token with enough hyphen segments to look like a real task_id (a
    conservative fallback that ignores ordinary single-hyphen words)."""
    m = _BACKTICK_RE.search(text)
    if m:
        return m.group(1)
    min_hyphens = cfg.get('min_bare_kebab_hyphens', DEFAULT_MIN_BARE_KEBAB_HYPHENS)
    if not isinstance(min_hyphens, int) or min_hyphens < 1:
        min_hyphens = DEFAULT_MIN_BARE_KEBAB_HYPHENS
    bare_re = re.compile(
        r'\b([a-z][a-z0-9]*(?:-[a-z0-9]+){%d,})\b' % min_hyphens
    )
    m = bare_re.search(text)
    return m.group(1) if m else None


# ---------- real-dispatch verification ----------


def dispatch_exists(task_id: str) -> tuple[bool, str]:
    """True (+evidence label) if any real Forge-dispatch artifact for task_id
    exists right now. Checked at evaluation time, so a late-but-real dispatch
    correctly reads as 'not phantom'."""
    inbox = forge_inbox_dir()
    names = [f'{task_id}.json', f'build-{task_id}.json']
    for sub in ('', '.archive', '.invalid'):
        base = inbox / sub if sub else inbox
        for name in names:
            if (base / name).exists():
                loc = sub or 'live'
                return True, f'forge-inbox:{loc}'

    if (in_flight_dir() / f'{task_id}.json').exists():
        return True, 'in-flight'

    if (worktrees_root() / f'wt-forge-{task_id}').is_dir():
        return True, 'worktree'

    if _outbox_log_mentions(task_id):
        return True, 'outbox-notifier-log'

    return False, ''


def _outbox_log_mentions(task_id: str) -> bool:
    path = outbox_log_file()
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if task_id in line:
                    return True
    except OSError:
        return False
    return False


def recent_forge_activity(since_epoch: float, until_epoch: float) -> bool:
    """True if ANY Forge-dispatch activity happened in [since, until]. Used only
    for ambiguous (no-task_id) claims: if the system did SOMETHING Forge-ward
    around the claim, we cannot say the claim specifically stranded, so stay
    quiet. Conservative by construction — most ticks see some activity."""
    def _any_recent(paths) -> bool:
        for p in paths:
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if since_epoch <= mt <= until_epoch:
                return True
        return False

    inbox = forge_inbox_dir()
    try:
        inbox_files = [p for p in inbox.glob('*.json')]
    except OSError:
        inbox_files = []
    if _any_recent(inbox_files):
        return True

    try:
        worktrees = [p for p in worktrees_root().glob('wt-forge-*')]
    except OSError:
        worktrees = []
    if _any_recent(worktrees):
        return True

    return _any_recent([outbox_log_file()])


# ---------- claim scanning + evaluation ----------


def scan_claims(log_text: str, cfg: dict, now: float) -> list[dict]:
    """Parse outbound lines within the scan window into claim dicts. A claim is
    {task_id|None, claim_ts (epoch), claim_iso, strong (bool), text}. Only lines
    matching a dispatch_claim_phrase are returned."""
    window_start = now - _scan_window_sec(cfg)
    claims: list[dict] = []
    for line in log_text.splitlines():
        parsed = parse_outbound_line(line)
        if parsed is None:
            continue
        claim_ts, _chat, text = parsed
        if claim_ts < window_start or claim_ts > now:
            continue
        if not is_dispatch_claim(text, cfg):
            continue
        claims.append({
            'task_id': extract_task_id(text, cfg),
            'claim_ts': claim_ts,
            'claim_iso': datetime.fromtimestamp(
                claim_ts, timezone.utc).isoformat(),
            'strong': is_strong_claim(text, cfg),
            'text': text,
        })
    return claims


def _dedup_key(claim: dict) -> str:
    return f'{claim["task_id"] or "ambiguous"}|{claim["claim_iso"]}'


def evaluate_claim(claim: dict, cfg: dict, now: float) -> Optional[dict]:
    """Return an alert dict for a phantom claim, or None (fresh / inside grace /
    real-dispatch-found / ambiguous-but-quiet). Does the live artifact lookups."""
    grace = _grace_sec(cfg)
    age = now - claim['claim_ts']
    if age < grace:
        return None  # inside grace — normal dispatch lag, not yet a phantom.

    task_id = claim['task_id']
    if task_id:
        exists, _ev = dispatch_exists(task_id)
        if exists:
            return None
        mins = age / 60.0
        return {
            'key': _dedup_key(claim),
            'subject': f'phantom-dispatch:{task_id}',
            'message': (
                f'Beacon told Larry it dispatched `{task_id}` to Forge at '
                f'{claim["claim_iso"]} ({mins:.0f} min ago), but NO real Forge '
                'dispatch exists — no inbox envelope (live/.archive/.invalid), '
                'no state/in-flight entry, no wt-forge worktree, no '
                'outbox-notifier line. The approved work has silently stranded '
                'between "Beacon said so" and reality.'
            ),
            'suggested_action': (
                f'Verify in the Telegram thread, then re-dispatch `{task_id}` '
                'to Forge (Beacon emits the real APPROVAL/dispatch marker, not '
                'just chat prose).'
            ),
        }

    # Ambiguous (no extractable task_id): alert only if the message strongly
    # indicates a dispatch AND no Forge activity at all followed in the window.
    if not claim['strong']:
        return None
    if recent_forge_activity(claim['claim_ts'], now):
        return None
    mins = age / 60.0
    return {
        'key': _dedup_key(claim),
        'subject': 'phantom-dispatch:ambiguous',
        'message': (
            f'Beacon strongly implied a Forge dispatch at {claim["claim_iso"]} '
            f'({mins:.0f} min ago) but named no task_id, and NO Forge dispatch '
            f'activity at all followed. Claimed text: {claim["text"]!r}.'
        ),
        'suggested_action': (
            'Check the Telegram thread for what was meant to dispatch, and '
            're-dispatch it explicitly if it stranded.'
        ),
    }


# ---------- dedup state ----------


def load_alerted_state() -> set[str]:
    try:
        data = json.loads(alerted_state_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    keys = data.get('alerted') if isinstance(data, dict) else None
    return set(k for k in keys if isinstance(k, str)) if isinstance(keys, list) \
        else set()


def save_alerted_state(keys: set[str]) -> None:
    """Atomic tmp + replace. Best-effort; never raises (a watcher must not die
    because it could not persist dedup state — worst case is a duplicate DM,
    which larry_alerts' per-subject cooldown still dampens)."""
    payload = {
        '_schema': {
            'version': 1,
            'purpose': (
                'Dedup keys (task_id|claim_iso) for phantom-dispatch claims '
                'already alerted, so each phantom DMs Larry exactly once across '
                'ticks. Idempotent.'
            ),
        },
        'alerted': sorted(keys),
    }
    try:
        path = alerted_state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        pass


# ---------- orchestration ----------


def run_once(now: float, cfg: dict, alerted: set[str]) -> list[dict]:
    """Return new (not-yet-alerted) phantom alerts. Pure w.r.t. the dedup set:
    does not mutate `alerted` or persist anything — main() owns that."""
    try:
        log_text = beacon_log_file().read_text(encoding='utf-8', errors='replace')
    except OSError:
        log_text = ''
    out: list[dict] = []
    for claim in scan_claims(log_text, cfg, now):
        key = _dedup_key(claim)
        if key in alerted:
            continue
        alert = evaluate_claim(claim, cfg, now)
        if alert is not None:
            out.append(alert)
    return out


def main() -> int:
    if kill_switch().exists():
        log('KILL_SWITCH active; exiting')
        return 0
    heartbeat()

    try:
        cfg = load_config()
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log(f'config unreadable: {type(e).__name__}: {e}', 'ERROR')
        _emit(
            subject='phantom-dispatch-config-unreadable',
            message=(
                'config/phantom-dispatch-claim-patterns.json could not be '
                f'loaded ({type(e).__name__}: {e}); the phantom-dispatch '
                'watcher is detecting nothing until it is fixed.'
            ),
            suggested_action=(
                'Validate config/phantom-dispatch-claim-patterns.json '
                '(python3 -c "import json,sys; json.load(open(sys.argv[1]))" '
                'config/phantom-dispatch-claim-patterns.json).'
            ),
        )
        return 1

    now = time.time()
    alerted = load_alerted_state()
    alerts = run_once(now, cfg, alerted)
    if not alerts:
        log('tick: no phantom dispatch-claims')
        return 0

    fired = 0
    for alert in alerts:
        delivered = _emit(
            subject=alert['subject'],
            message=alert['message'],
            suggested_action=alert['suggested_action'],
        )
        # Mark alerted once we have attempted the emit: a phantom is a one-time
        # finding, and the alert (or larry_alerts' cooldown suppression of a
        # duplicate) means re-DMing it every tick would just be noise.
        alerted.add(alert['key'])
        log(f'{"alerted" if delivered else "suppressed"}: {alert["subject"]}',
            'WARN' if delivered else 'INFO')
        fired += int(bool(delivered))
    save_alerted_state(alerted)
    log(f'done: {fired} alert(s) fired, {len(alerts) - fired} suppressed')
    return 0


def _emit(subject: str, message: str, suggested_action: str) -> bool:
    """Append a larry-alert (route=escalate, severity warning). Cooldown +
    routing enforced inside larry_alerts. Never raises."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402

        return la.append_alert(
            source='heal-phantom-dispatch-claim',
            severity='warning',
            message=message,
            subject=subject,
            suggested_action=suggested_action,
            route='escalate',
        )
    except Exception as e:  # noqa: BLE001
        log(f'emit failed for {subject}: {type(e).__name__}: {e}', 'WARN')
        return False


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log(f'FATAL: {type(exc).__name__}: {exc}', 'ERROR')
        # Self-failure must reach Larry: an exception here means the
        # phantom-dispatch net itself is down.
        _emit(
            subject='phantom-dispatch-self-failure',
            message=(
                'heal_phantom_dispatch_claim.py crashed '
                f'({type(exc).__name__}: {exc}); phantom dispatch-claims are '
                'not being detected until it is fixed.'
            ),
            suggested_action=(
                'Run python3 ~/agent-core/scripts/heal_phantom_dispatch_claim.py '
                'and read the traceback.'
            ),
        )
        sys.exit(1)
