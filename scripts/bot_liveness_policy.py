#!/usr/bin/env python3
"""Per-bot liveness policy for watchdog.

Each bot declares its deployment mode (`systemd` or `tmux-or-systemd`) in
`config/bot-liveness-policy.json`. The watchdog reads this policy when
checking liveness so e.g. pulse-bot's tmux deployment (PR #161) is not
false-alarmed as down — and the recovery command surfaced to Larry's
phone matches the actual deployment shape.

Schema (version 1):
    {
      "_schema": {...},
      "<agent>": {
        "mode": "systemd" | "tmux-or-systemd",
        "systemd_unit": "<unit-name>",
        "tmux_session": "<session>",   # tmux-or-systemd only
        "launcher": "<path>"           # tmux-or-systemd only
      },
      ...
    }

Rule + enforcement pairing (doctrine-of-doctrine, PR #163):
    Rule       — each bot entry has a valid mode + all required fields.
    Mechanism  — load_policy() raises BotPolicyError on malformed input;
                 watchdog refuses to operate on a corrupt policy file.
    Tests      — scripts/tests/test_bot_liveness_policy.py asserts the
                 schema gate.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

MODE_SYSTEMD = 'systemd'
MODE_TMUX_OR_SYSTEMD = 'tmux-or-systemd'
VALID_MODES = (MODE_SYSTEMD, MODE_TMUX_OR_SYSTEMD)

DEFAULT_MODE = MODE_SYSTEMD

# Repo-rooted default path. Tests override via BOT_LIVENESS_POLICY_PATH.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = _REPO_ROOT / 'config' / 'bot-liveness-policy.json'

POLICY_PATH_ENV = 'BOT_LIVENESS_POLICY_PATH'


class BotPolicyError(Exception):
    """Raised when bot-liveness-policy.json is malformed or unreadable."""


def _resolve_path(path: Optional[Path | str]) -> Path:
    if path is not None:
        return Path(path)
    env_override = os.environ.get(POLICY_PATH_ENV)
    if env_override:
        return Path(env_override)
    return DEFAULT_POLICY_PATH


def _validate_entry(agent: str, entry: dict) -> None:
    if not isinstance(entry, dict):
        raise BotPolicyError(f'{agent!r}: entry must be a JSON object, got {type(entry).__name__}')
    mode = entry.get('mode')
    if mode not in VALID_MODES:
        raise BotPolicyError(
            f'{agent!r}: mode must be one of {VALID_MODES}, got {mode!r}'
        )
    if mode == MODE_SYSTEMD:
        if not entry.get('systemd_unit'):
            raise BotPolicyError(f'{agent!r}: systemd mode requires non-empty systemd_unit')
    elif mode == MODE_TMUX_OR_SYSTEMD:
        missing = [k for k in ('tmux_session', 'systemd_unit', 'launcher') if not entry.get(k)]
        if missing:
            raise BotPolicyError(
                f'{agent!r}: tmux-or-systemd mode requires non-empty {missing}'
            )


def load_policy(path: Optional[Path | str] = None) -> dict:
    """Load and validate the per-bot liveness policy.

    Raises BotPolicyError on missing file, unreadable JSON, or schema
    violation. The watchdog refuses to operate on a corrupt policy file —
    masking the bug behind a default would silently regress the carve-out
    semantics this module exists to express.
    """
    resolved = _resolve_path(path)
    try:
        with open(resolved, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise BotPolicyError(f'policy file not found: {resolved}') from e
    except (OSError, json.JSONDecodeError) as e:
        raise BotPolicyError(f'cannot read policy file {resolved}: {e}') from e
    if not isinstance(data, dict):
        raise BotPolicyError(f'policy root must be a JSON object, got {type(data).__name__}')
    for agent, entry in data.items():
        if agent.startswith('_'):
            continue
        _validate_entry(agent, entry)
    return data


def policy_agents(policy: dict) -> list[str]:
    """Return the agent names declared in the policy, excluding `_`-prefixed
    metadata keys (e.g. `_schema`). Order preserved from the file."""
    return [k for k in policy.keys() if not k.startswith('_')]


def _default_entry(agent: str) -> dict:
    """Default entry for a bot missing from the policy file: assume
    systemd with the conventional unit name. Preserves the pre-policy
    behavior so a dropped entry doesn't crash watchdog."""
    return {
        'mode': MODE_SYSTEMD,
        'systemd_unit': f'ourliberty-{agent}-bot.service',
    }


def _entry_for(policy: dict, agent: str) -> dict:
    entry = policy.get(agent)
    if isinstance(entry, dict) and 'mode' in entry:
        return entry
    return _default_entry(agent)


def _systemd_is_active(unit: str) -> bool:
    try:
        r = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False
    return r.returncode == 0 and r.stdout.strip() == 'active'


def _tmux_has_session(session: str) -> bool:
    try:
        r = subprocess.run(
            ['tmux', 'has-session', '-t', session],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    return r.returncode == 0


def check_liveness(agent: str, policy: Optional[dict] = None) -> tuple[bool, str]:
    """Return (alive, mode_used).

    For mode=systemd: mode_used is 'systemd' if `systemctl is-active <unit>`
    succeeds, otherwise 'down'.

    For mode=tmux-or-systemd: tmux is preferred (the intended deployment
    after PR #161); mode_used is 'tmux' if `tmux has-session -t <s>` succeeds,
    else 'systemd' if `systemctl is-active <unit>` succeeds, else 'down'.
    """
    if policy is None:
        policy = load_policy()
    entry = _entry_for(policy, agent)
    mode = entry['mode']
    if mode == MODE_SYSTEMD:
        if _systemd_is_active(entry['systemd_unit']):
            return True, 'systemd'
        return False, 'down'
    if mode == MODE_TMUX_OR_SYSTEMD:
        if _tmux_has_session(entry['tmux_session']):
            return True, 'tmux'
        if _systemd_is_active(entry['systemd_unit']):
            return True, 'systemd'
        return False, 'down'
    raise BotPolicyError(f'{agent!r}: unknown mode {mode!r}')


def recovery_command(agent: str, policy: Optional[dict] = None) -> str:
    """Return the recovery shell command for `agent` per its policy mode.

    systemd        → `sudo systemctl restart <unit>`
    tmux-or-systemd → `bash <launcher>` (tmux is intended deployment;
                       starting systemd would create a competing instance
                       fighting tmux for getUpdates — the destructive shape
                       this policy exists to prevent).
    """
    if policy is None:
        policy = load_policy()
    entry = _entry_for(policy, agent)
    mode = entry['mode']
    if mode == MODE_SYSTEMD:
        return f'sudo systemctl restart {entry["systemd_unit"]}'
    if mode == MODE_TMUX_OR_SYSTEMD:
        return f'bash {entry["launcher"]}'
    raise BotPolicyError(f'{agent!r}: unknown mode {mode!r}')
