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
import urllib.error
import urllib.parse
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


def _read_ingest_token() -> str | None:
    """Resolve the narrow ingest token: the token FILE first (the operator's Mac
    holds it there), else the OL_INGEST_TOKEN / DESKTOP_INGEST_TOKEN env var (the
    droplet services load DESKTOP_INGEST_TOKEN from .env.larry, where no token
    file exists). Returns the token, or None with a stderr diagnostic. The env
    fallback is what lets an in-service emit/retract authenticate on the droplet."""
    token_file = (os.environ.get('OL_INGEST_TOKEN_FILE')
                  or str(Path.home() / '.config' / 'ourliberty' / 'ingest-token'))
    try:
        token = Path(token_file).read_text(encoding='utf-8').strip()
        if token:
            return token
    except Exception:  # noqa: BLE001 — absent/unreadable file -> try env
        pass
    token = (os.environ.get('OL_INGEST_TOKEN')
             or os.environ.get('DESKTOP_INGEST_TOKEN') or '').strip()
    if token:
        return token
    _err(f'no ingest token (file {token_file} absent/empty; no OL_INGEST_TOKEN / '
         'DESKTOP_INGEST_TOKEN env)')
    return None


def emit_capture(
    *,
    title: str,
    note: str | None = None,
    source: str = 'agent',
    label: str | None = None,
    session_id: str | None = None,
) -> str | None:
    """Park a durable capture card via `POST /api/ingest/capture`.

    The shared, importable core of the capture gesture: any agent script can
    call this in-process to land a card in the Missions Parked lane. Derives
    origin (repo / branch) from the current git context, reads the narrow
    ingest token, and POSTs. The optional `label` is an allowlisted first-class
    tag (server-validated against CAPTURE_ALLOWED_LABELS).

    Returns the server-assigned `capture_id` on success, or `None` on ANY
    failure (missing title/token, network error, unexpected response). NEVER
    raises — an in-process caller (Pulse Check I) must not crash on a failed
    park; diagnostics go to stderr via `_err`.
    """
    title = (title or '').strip()
    if not title:
        _err('no title — skip')
        return None
    note = (note or '').strip() or None
    source = (source or _DEFAULT_SOURCE).strip()
    session_id = (session_id or '').strip() or None

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
        'note': note,
        'origin': origin,
        'label': label,
    }).encode('utf-8')

    api_url = (os.environ.get('OL_DASHBOARD_API_URL')
               or 'https://api.ourliberty.dev').rstrip('/')
    token = _read_ingest_token()
    if not token:
        return None

    req = urllib.request.Request(
        f'{api_url}/api/ingest/capture',
        data=body, method='POST',
        headers={'Content-Type': 'application/json', 'X-Ingest-Token': token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        # Surface the response BODY, not just "HTTP Error 400: Bad Request".
        # The ingest endpoint returns an actionable detail (e.g.
        # {"detail":"invalid origin.source='x'"}) that bare str(exc) drops —
        # without it a rejected capture gives no clue what to fix.
        detail = ''
        try:
            detail = exc.read().decode('utf-8', 'replace').strip()
        except Exception:  # noqa: BLE001 — body read is best-effort
            pass
        _err(f'POST failed: {exc}' + (f' — {detail[:500]}' if detail else ''))
        return None
    except Exception as exc:  # noqa: BLE001
        _err(f'POST failed: {exc}')
        return None

    capture_id = payload.get('capture_id') if isinstance(payload, dict) else None
    if not capture_id:
        _err(f'unexpected response: {payload!r}')
        return None
    if os.environ.get('OL_HOOK_DEBUG'):
        _err(f'parked {capture_id} repo={repo} branch={branch} label={label}')
    return capture_id


def retract_capture(
    capture_id: str, reason: str | None = None,
) -> dict | None:
    """Auto-retract a machine-owned proposal capture (slice 9 — Medic self-
    retract when its condition clears). POSTs to
    `/api/ingest/capture/{id}/retract` with the SAME narrow ingest token
    emit_capture uses.

    Returns the server's JSON result dict — `{retracted: True, ...}` on a drop,
    or `{retracted: False, reason: ...}` for a card that moved on ('not-found',
    'not-parked') or a machine does not own ('not-machine-retractable'). Returns
    `None` on a transport/token error (no token, network, non-JSON) — the signal
    to a reconciler that the outcome is UNKNOWN and it should retry, vs a dict
    'retracted False' which is a TERMINAL 'nothing to do'. NEVER raises."""
    capture_id = (capture_id or '').strip()
    if not capture_id:
        _err('no capture_id — skip retract')
        return None

    api_url = (os.environ.get('OL_DASHBOARD_API_URL')
               or 'https://api.ourliberty.dev').rstrip('/')
    token = _read_ingest_token()
    if not token:
        return None

    body = json.dumps({'reason': reason}).encode('utf-8')
    quoted = urllib.parse.quote(capture_id, safe='')
    req = urllib.request.Request(
        f'{api_url}/api/ingest/capture/{quoted}/retract',
        data=body, method='POST',
        headers={'Content-Type': 'application/json', 'X-Ingest-Token': token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read().decode('utf-8', 'replace').strip()
        except Exception:  # noqa: BLE001 — body read is best-effort
            pass
        _err(f'retract HTTP {exc.code}: {detail}')
        return None
    except Exception as e:  # noqa: BLE001 — retract must never crash a caller
        _err(f'retract failed: {e}')
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    title = (os.environ.get('OL_CAPTURE_TITLE') or '').strip()
    if not title:
        _err('no title (set OL_CAPTURE_TITLE or pass it as the first arg) — skip')
        return 2

    capture_id = emit_capture(
        title=title,
        note=os.environ.get('OL_CAPTURE_NOTE'),
        source=os.environ.get('OL_CAPTURE_SOURCE') or _DEFAULT_SOURCE,
        session_id=os.environ.get('OL_CAPTURE_SESSION_ID'),
    )
    if not capture_id:
        # emit_capture already wrote the specific reason to stderr.
        return 1
    sys.stdout.write(f'captured {capture_id}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
