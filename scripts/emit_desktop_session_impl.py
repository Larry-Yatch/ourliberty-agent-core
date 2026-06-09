#!/usr/bin/env python3
"""Desktop-session hook emitter impl (Missions v2 Phase 0).

Invoked (backgrounded) by emit_desktop_session.sh. Reads the Claude Code
SessionStart/SessionEnd hook JSON from $OL_HOOK_INPUT, derives repo / branch /
task_id, gates on a tracked repo, and POSTs a desktop_session_* event to the
droplet ingest endpoint so a live desktop chat shows up on the Missions board.

Best-effort by contract: any failure is swallowed (no card is an acceptable
outcome — same posture as every other push-instrumented chain event). stdlib
only; the operator's Mac holds no Supabase creds, only the narrow ingest token.

Inputs (env, set by the wrapper / settings.json):
  EVENT_TYPE             desktop_session_start | _active | _done
  OL_HOOK_INPUT          raw hook stdin JSON
  OL_DESKTOP_TASK_ID     (optional) explicit thread/mission task_id override
  OL_DASHBOARD_API_URL   (default https://api.ourliberty.dev)
  OL_INGEST_TOKEN_FILE   (default ~/.config/ourliberty/ingest-token)
  OL_TRACKED_REPOS       (default the three ourliberty repos)
  OL_HOOK_DEBUG          (optional) if set, log to stderr
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

_ALLOWED = ('desktop_session_start', 'desktop_session_active', 'desktop_session_done')
_DEFAULT_TRACKED = 'ourliberty-agent-core ourliberty-dashboard ourliberty-graph'


def _log(msg: str) -> None:
    if os.environ.get('OL_HOOK_DEBUG'):
        sys.stderr.write(f'[emit_desktop_session] {msg}\n')


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(
            ['git', '-C', cwd, *args],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return ''


def _normalize_repo(name: str) -> str:
    """Map a worktree dir (ourliberty-agent-core.phase0 / .wt / .wt2) back to
    its base repo name. Base repo names contain no '.', so the prefix before
    the first '.' is the canonical repo."""
    return name.split('.', 1)[0]


def main() -> int:
    event_type = (os.environ.get('EVENT_TYPE') or '').strip()
    if event_type not in _ALLOWED:
        _log(f'bad/empty event_type={event_type!r} — skip')
        return 0

    raw = os.environ.get('OL_HOOK_INPUT') or ''
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook = {}
    if not isinstance(hook, dict):
        hook = {}

    cwd = hook.get('cwd') or os.getcwd()
    session_id = str(hook.get('session_id') or '')

    toplevel = _git(cwd, 'rev-parse', '--show-toplevel')
    if not toplevel:
        _log('cwd is not a git repo — skip')
        return 0
    repo = _normalize_repo(Path(toplevel).name)

    tracked = (os.environ.get('OL_TRACKED_REPOS') or _DEFAULT_TRACKED).split()
    if repo not in tracked:
        _log(f'repo {repo!r} not in tracked set — skip')
        return 0

    branch = _git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD')

    # task_id resolution: explicit env > .claude/desktop-session-tag > default.
    task_id = (os.environ.get('OL_DESKTOP_TASK_ID') or '').strip()
    title = ''
    tag_file = Path(toplevel) / '.claude' / 'desktop-session-tag'
    if tag_file.is_file():
        try:
            lines = tag_file.read_text(encoding='utf-8').splitlines()
            if not task_id and lines and lines[0].strip():
                task_id = lines[0].strip()
            if len(lines) > 1 and lines[1].strip():
                title = lines[1].strip()
        except Exception:
            pass
    if not task_id:
        task_id = f'desktop-{session_id[:8]}' if session_id else 'desktop-unknown'

    payload = {
        'repo': repo,
        'branch': branch,
        'cwd': cwd,
        'title': title,
        'blocked_on_larry': False,
        'host': socket.gethostname().lower().split('.')[0],
        # NB: NOT including session_id — the shipper's sanitize_payload redacts
        # any key containing 'session_id'. task_id already carries a short ref.
        'source': hook.get('source') or hook.get('reason') or '',
    }
    body = json.dumps({
        'event_type': event_type,
        'task_id': task_id,
        'payload': payload,
    }).encode('utf-8')

    api_url = (os.environ.get('OL_DASHBOARD_API_URL')
               or 'https://api.ourliberty.dev').rstrip('/')
    token_file = (os.environ.get('OL_INGEST_TOKEN_FILE')
                  or str(Path.home() / '.config' / 'ourliberty' / 'ingest-token'))
    try:
        token = Path(token_file).read_text(encoding='utf-8').strip()
    except Exception:
        _log(f'no ingest token at {token_file} — skip')
        return 0
    if not token:
        _log('empty ingest token — skip')
        return 0

    req = urllib.request.Request(
        f'{api_url}/api/ingest/desktop-session',
        data=body, method='POST',
        headers={'Content-Type': 'application/json', 'X-Ingest-Token': token},
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            _log(f'posted {event_type} task={task_id} repo={repo} -> {resp.status}')
    except Exception as exc:  # noqa: BLE001 — best-effort
        _log(f'post failed: {exc}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
