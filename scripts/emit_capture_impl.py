#!/usr/bin/env python3
"""Durable-capture emitter impl (Missions v2 Phase 1).

Invoked by emit_capture.sh when I flag a follow-up / idea / hole mid-work, or
when Larry says "capture this." Derives origin (repo / branch / session) from
the current git context, reads the SAME narrow ingest token the desktop-session
emitter uses, and POSTs to the droplet's `POST /api/ingest/capture` so the
follow-up becomes a durable card in the Missions Parked lane.

Unlike the desktop-session hook (best-effort, always exits 0 — a missed card is
acceptable), a capture is a deliberate gesture whose whole point is durability,
so this surfaces a non-zero exit + a stderr line on failure. It still never
crashes; a bad environment is reported, not raised.

stdlib only; the operator's Mac holds no Supabase creds, only the ingest token.

Inputs (env, set by the wrapper):
  OL_CAPTURE_TITLE       the capture title (required)
  OL_CAPTURE_NOTE        (optional) a sentence of context
  OL_CAPTURE_SOURCE      (default desktop-chat) one of the server's fixed set
  OL_CAPTURE_SESSION_ID  (optional) originating chat/session id
  OL_DASHBOARD_API_URL   (default https://api.ourliberty.dev)
  OL_INGEST_TOKEN_FILE   (default ~/.config/ourliberty/ingest-token)
  OL_HOOK_DEBUG          (optional) if set, log extra detail to stderr
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

_DEFAULT_SOURCE = 'desktop-chat'


def _err(msg: str) -> None:
    sys.stderr.write(f'[emit_capture] {msg}\n')


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(
            ['git', '-C', cwd, *args],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return ''


def _normalize_repo(name: str) -> str:
    """Map a worktree dir (ourliberty-agent-core.wt / .phase0) back to its base
    repo name. Base repo names contain no '.', so the prefix before the first
    '.' is canonical. Mirrors emit_desktop_session_impl._normalize_repo."""
    return name.split('.', 1)[0]


def main() -> int:
    title = (os.environ.get('OL_CAPTURE_TITLE') or '').strip()
    if not title:
        _err('no title (set OL_CAPTURE_TITLE or pass it as the first arg) — skip')
        return 2

    note = (os.environ.get('OL_CAPTURE_NOTE') or '').strip()
    source = (os.environ.get('OL_CAPTURE_SOURCE') or _DEFAULT_SOURCE).strip()
    session_id = (os.environ.get('OL_CAPTURE_SESSION_ID') or '').strip() or None

    cwd = os.getcwd()
    repo = None
    branch = None
    toplevel = _git(cwd, 'rev-parse', '--show-toplevel')
    if toplevel:
        repo = _normalize_repo(Path(toplevel).name)
        branch = _git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD') or None

    origin = {
        'source': source,
        'session_id': session_id,
        'repo': repo,
        'branch': branch,
    }
    body = json.dumps({
        'title': title,
        'note': note or None,
        'origin': origin,
    }).encode('utf-8')

    api_url = (os.environ.get('OL_DASHBOARD_API_URL')
               or 'https://api.ourliberty.dev').rstrip('/')
    token_file = (os.environ.get('OL_INGEST_TOKEN_FILE')
                  or str(Path.home() / '.config' / 'ourliberty' / 'ingest-token'))
    try:
        token = Path(token_file).read_text(encoding='utf-8').strip()
    except Exception:
        _err(f'no ingest token at {token_file}')
        return 3
    if not token:
        _err('empty ingest token')
        return 3

    req = urllib.request.Request(
        f'{api_url}/api/ingest/capture',
        data=body, method='POST',
        headers={'Content-Type': 'application/json', 'X-Ingest-Token': token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode('utf-8') or '{}')
    except Exception as exc:  # noqa: BLE001
        _err(f'POST failed: {exc}')
        return 4

    capture_id = payload.get('capture_id') if isinstance(payload, dict) else None
    if not capture_id:
        _err(f'unexpected response: {payload!r}')
        return 4
    sys.stdout.write(f'captured {capture_id}\n')
    if os.environ.get('OL_HOOK_DEBUG'):
        _err(f'parked {capture_id} repo={repo} branch={branch}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
