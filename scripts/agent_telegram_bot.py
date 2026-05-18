#!/usr/bin/env python3
"""agent_telegram_bot.py — generic Telegram <-> agent bridge.

Reads the AGENT environment variable (e.g. AGENT=forge) and runs as that
agent's Telegram bot. The agent's working directory is ~/agent-core/agents/<AGENT>/
and the bot token is read from TELEGRAM_BOT_TOKEN_<UPPER_AGENT>.

This is the production-shaped successor to beacon_telegram_bot.py — same
semantics, parameterized by agent. systemd units invoke this with AGENT set
in the unit's Environment= directive.

Stdlib only. Per-chat session continuity via claude --resume.

Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# Telegram text helpers (shared with beacon_telegram_bot.py)
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from telegram_text_utils import strip_leading_slash  # noqa: E402

# ---------- config ----------

AGENT = os.environ.get("AGENT", "").strip().lower()
if not AGENT or not re.match(r"^[a-z][a-z0-9_-]*$", AGENT):
    sys.exit("ERROR: AGENT env var must be set to a valid agent slug (e.g. AGENT=forge)")

TOKEN_VAR = f"TELEGRAM_BOT_TOKEN_{AGENT.upper()}"
TOKEN = os.environ.get(TOKEN_VAR, "").strip()
if not TOKEN:
    sys.exit(f"ERROR: {TOKEN_VAR} not set in environment.")

ALLOWED_RAW = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
ALLOWED: set[int] = {int(x) for x in re.split(r"[,\s]+", ALLOWED_RAW) if x.strip()}
if not ALLOWED:
    sys.exit("ERROR: TELEGRAM_ALLOWED_CHAT_IDS empty — refusing to run a bot anyone can talk to.")

AGENT_DIR = Path.home() / "agent-core" / "agents" / AGENT
if not AGENT_DIR.is_dir():
    sys.exit(f"ERROR: agent dir not found: {AGENT_DIR}")

LOG_DIR = Path.home() / "agents" / "logs"
STATE_DIR = Path.home() / "agents" / "state"
SESSION_FILE = STATE_DIR / f"{AGENT}_telegram_sessions.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

API = f"https://api.telegram.org/bot{TOKEN}"
TELEGRAM_MAX = 4000

CLAUDE_BIN = shutil.which("claude") or "/usr/bin/claude"
CLAUDE_TIMEOUT_SEC = 600


# ---------- logging ----------

def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {AGENT}: {msg}"
    print(line, flush=True)
    try:
        with open(LOG_DIR / f"{AGENT}_telegram_bot.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------- session continuity ----------

def load_sessions() -> dict[str, str]:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_sessions(sessions: dict[str, str]) -> None:
    try:
        SESSION_FILE.write_text(json.dumps(sessions, indent=2))
    except OSError as e:
        log(f"save_sessions error: {e}")


# ---------- HTTP helpers ----------

def http_json(url: str, payload: Optional[dict] = None, timeout: int = 35) -> Optional[dict]:
    try:
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code} {url}: {e.read()[:200]}")
    except urllib.error.URLError as e:
        log(f"URL error {url}: {e}")
    except Exception as e:
        log(f"http_json unexpected error {url}: {e}")
    return None


def telegram_send(chat_id: int, text: str) -> None:
    if not text:
        text = "[empty response]"
    while text:
        chunk, text = text[:TELEGRAM_MAX], text[TELEGRAM_MAX:]
        http_json(f"{API}/sendMessage", {"chat_id": chat_id, "text": chunk})


def telegram_send_action(chat_id: int, action: str = "typing") -> None:
    http_json(f"{API}/sendChatAction", {"chat_id": chat_id, "action": action})


# ---------- Claude Code bridge ----------

def call_agent(prompt: str, session_id: Optional[str]) -> tuple[str, Optional[str]]:
    cmd = [CLAUDE_BIN, "--print", "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += [prompt]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(AGENT_DIR),
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return (f"[{AGENT.title()} timed out after 10 min — please retry, ideally with a tighter scope]", session_id)
    except FileNotFoundError:
        return (f"[claude binary not found at {CLAUDE_BIN}]", session_id)
    except Exception as e:
        return (f"[exception: {e}]", session_id)

    if result.returncode != 0:
        log(f"claude exit {result.returncode}: {result.stderr[:500]}")
        if session_id and "session" in result.stderr.lower():
            log("retrying without --resume after session error")
            return call_agent(prompt, None)
        return (f"[claude exit {result.returncode}]\n{result.stderr[:1500]}", session_id)

    try:
        data = json.loads(result.stdout)
        reply = data.get("result") or data.get("text") or result.stdout
        new_session = data.get("session_id") or session_id
        return (reply.strip(), new_session)
    except json.JSONDecodeError:
        return (result.stdout.strip() or "[empty response]", session_id)


# ---------- main loop ----------

def main() -> None:
    log(f"{AGENT.title()} bot starting (cwd={AGENT_DIR}, allowed={sorted(ALLOWED)})")
    sessions = load_sessions()
    offset = 0

    while True:
        url = f"{API}/getUpdates?offset={offset}&timeout=30"
        data = http_json(url, timeout=35)
        if not data or not data.get("ok"):
            time.sleep(3)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            text = msg.get("text", "").strip()
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            if not text or chat_id is None:
                continue

            if chat_id not in ALLOWED:
                log(f"ignored unauthorized chat {chat_id} ({chat.get('username') or '?'}): {text[:50]!r}")
                continue

            log(f"<- {chat_id}: {text[:120]!r}")
            text = strip_leading_slash(text)
            telegram_send_action(chat_id, "typing")

            session_id = sessions.get(str(chat_id))
            reply, new_session = call_agent(text, session_id)
            if new_session and new_session != session_id:
                sessions[str(chat_id)] = new_session
                save_sessions(sessions)

            log(f"-> {chat_id}: {reply[:120]!r}")
            telegram_send(chat_id, reply)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted, shutting down")
