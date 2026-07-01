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
# Step C — share the rate-limit ledger + failure classifier with the other
# Claude-spawning paths. Pre-Step-C this bot path never wrote
# anthropic-quota-events.jsonl, leaving zero events for 2026-05-29's stall
# storm even though auth_401 dominated the failures here.
import agent_runner  # noqa: E402
from test_isolation_guard import refuse_under_test  # noqa: E402

# ---------- config ----------

# These constants are derived at import (harmless — pure env reads / path building). The
# FATAL validation is deferred to _require_runtime_env(), called from main(), so the module
# stays importable without a live bot environment: tests import this file for its pure
# helpers, and a sys.exit at import scope crashes every importer (which made test verdicts
# depend on which sibling test happened to leak a token first), not just the one
# misconfigured bot process.
AGENT = os.environ.get("AGENT", "").strip().lower()
TOKEN_VAR = f"TELEGRAM_BOT_TOKEN_{AGENT.upper()}"
TOKEN = os.environ.get(TOKEN_VAR, "").strip()
ALLOWED_RAW = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
ALLOWED: set[int] = {int(x) for x in re.split(r"[,\s]+", ALLOWED_RAW) if x.strip()}
AGENT_DIR = Path.home() / "agent-core" / "agents" / AGENT


def _require_runtime_env() -> None:
    """Fail-fast validation of the live-bot environment. Called from main() (i.e. only when
    the bot is actually RUN), never at import. Messages unchanged from prior behavior."""
    if not AGENT or not re.match(r"^[a-z][a-z0-9_-]*$", AGENT):
        sys.exit("ERROR: AGENT env var must be set to a valid agent slug (e.g. AGENT=forge)")
    if not TOKEN:
        sys.exit(f"ERROR: {TOKEN_VAR} not set in environment.")
    if not ALLOWED:
        sys.exit("ERROR: TELEGRAM_ALLOWED_CHAT_IDS empty — refusing to run a bot anyone can talk to.")
    if not AGENT_DIR.is_dir():
        sys.exit(f"ERROR: agent dir not found: {AGENT_DIR}")

_AGENTS_ROOT = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT") or Path.home() / "agents")
LOG_DIR = _AGENTS_ROOT / "logs"
STATE_DIR = _AGENTS_ROOT / "state"
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
    refuse_under_test('telegram-send')
    if not text:
        text = "[empty response]"
    while text:
        chunk, text = text[:TELEGRAM_MAX], text[TELEGRAM_MAX:]
        http_json(f"{API}/sendMessage", {"chat_id": chat_id, "text": chunk})


def telegram_send_action(chat_id: int, action: str = "typing") -> None:
    http_json(f"{API}/sendChatAction", {"chat_id": chat_id, "action": action})


# ---------- Claude Code bridge ----------

def _append_bot_quota_event(
    failure_type: str,
    stdout: Optional[str],
    stderr: Optional[str],
    account: str,
) -> None:
    """Best-effort append to anthropic-quota-events.jsonl for a bot-wrapper
    Claude failure. Mirrors the helper in beacon_telegram_bot.py; lives here
    so forge/mirror's generic bot path also writes the ledger. Failures are
    swallowed — the ledger is observation-only and must never block reply.

    ``account`` is the tier this message actually dispatched on (spec § 6
    effective-tier discipline) — the failing account, not the global active
    tier."""
    combined = (stdout or '') + '\n' + (stderr or '')
    try:
        retry_after = agent_runner._derive_retry_after_sec(combined)
    except Exception:
        retry_after = None
    try:
        agent_runner.append_rate_limit_event(
            agent=f'{AGENT}-telegram-bot',
            task_id='',
            model='',
            account=account,
            stderr=combined,
            retry_after_sec=retry_after,
            failure_class=failure_type,
        )
    except Exception:
        pass


def call_agent(prompt: str, session_id: Optional[str]) -> tuple[str, Optional[str]]:
    # Per-task tier dispatch (spec docs/specs/tier-dispatch-spec.md §4/§10-W2):
    # choose the tier for THIS message instead of hardcoding tier1. A resume
    # (session_id present) is bound to the tier its session was created on
    # (I2 — never cross-tier migrate); a new message round-robins the healthy
    # primary pool. active_tier is reachable via the already-imported
    # agent_runner (which `import active_tier`).
    at = agent_runner.active_tier
    session_tier = at.lookup_session_tier(session_id) if session_id else None
    if session_id and session_tier is None:
        # Spec §5: a resume with no recorded binding (pre-existing/pruned
        # session). Select fresh; the no-resume retry below still won't cross
        # accounts because a benched bound tier never reaches here.
        log(f"call_agent: resume session {session_id[:12]}... has no tier "
            f"binding (selecting fresh)")
    tier = at.select_dispatch_tier(session_tier=session_tier)
    if tier is None:
        # All tiers unavailable. A chat bot can't hold silently — tell the user
        # to retry; the chat keeps its session_id so continuity survives.
        log("call_agent: TIER_HOLD no dispatch tier available (all benched)")
        return ("[All accounts are rate-limited right now — please retry in a "
                "few minutes.]", session_id)

    cmd = [CLAUDE_BIN, "--print", "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += [prompt]

    # Authenticate via the SELECTED tier's long-lived setup-token (the resolver
    # agent_runner uses for a dispatch). Without it claude fell back to HOME's
    # ~/.claude/.credentials.json, which rots silently and then 401s every
    # message. HOME discipline mirrors run_claude: setup-token auth is
    # HOME-independent (keep the real home; tier3 shares it); only a creds.json
    # fallback swaps HOME to the tier home. The token matches the session's
    # bound account, so --resume continuity holds.
    env = {**os.environ}
    auth_source = agent_runner._apply_tier_auth(
        env, tier, os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''),
    )
    if auth_source == 'setup_token':
        env['HOME'] = at.TIER1_HOME
    else:
        # Creds.json HOME swap: pin agents-root + gh/git config to the REAL
        # home (I10/I11) so the child resolves ~/agents state (session-tier
        # map, costs) and git/gh credentials under the real home, not the
        # swapped tier home — mirrors run_claude. (Dormant while setup-tokens
        # are configured for all pool tiers; correct defense for the fallback.)
        env['HOME'] = at.home_for_tier(tier)
        env.setdefault('OURLIBERTY_AGENTS_ROOT',
                       str(Path(at.TIER1_HOME) / 'agents'))
        env.setdefault('GH_CONFIG_DIR',
                       os.path.join(at.TIER1_HOME, '.config', 'gh'))
        env.setdefault('GIT_CONFIG_GLOBAL',
                       os.path.join(at.TIER1_HOME, '.gitconfig'))
    # Log the dispatch tier + auth source (NOT the token) so a future auth
    # regression is visible — this bridge's silent auth path is why the
    # 2026-06-25 incident took hours to pin down.
    log(f"call_agent: dispatch_tier={tier} auth={auth_source}")
    _t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(AGENT_DIR),
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return (f"[{AGENT.title()} timed out after 10 min — please retry, ideally with a tighter scope]", session_id)
    except FileNotFoundError:
        return (f"[claude binary not found at {CLAUDE_BIN}]", session_id)
    except Exception as e:
        return (f"[exception: {e}]", session_id)

    if result.returncode != 0:
        log(f"claude exit {result.returncode}: {result.stderr[:500]}")
        # Step C: classify and ledger rate_limit / auth_401 failures before
        # any --resume retry or chat reply path. Resume-class is the dominant
        # auth_401 carrier through this bot wrapper (session_id present + 401
        # because the underlying account's OAuth expired). The ledger event is
        # attributed to the tier that actually ran (§6).
        failure_type = agent_runner.classify_tier1_failure(
            result.stdout, result.stderr,
        )
        if failure_type:
            _append_bot_quota_event(
                failure_type, result.stdout, result.stderr, tier)
            # §7/§16: BENCH the tier that just walled so the next dispatch (from
            # ANY path) doesn't immediately re-pick it via round-robin. Without
            # this, only agent_runner benched — a bot-driven rate_limit left the
            # tier 'usable' and it got slammed again on the very next message.
            # rate_limit parses "resets <time>"; auth_401 gets the fixed window.
            if failure_type in ('rate_limit', 'auth_401'):
                try:
                    at.set_cooldown(
                        tier,
                        raw_excerpt=(result.stdout or '') + '\n'
                        + (result.stderr or ''),
                        kind=('auth_401' if failure_type == 'auth_401'
                              else 'rate_limit'))
                except Exception as _cd_exc:
                    # Don't swallow silently: if the bench write fails the tier
                    # stays 'usable' and the next dispatch re-picks it — log so
                    # the failure is visible (F3).
                    log(f"set_cooldown({tier}) failed after {failure_type}: "
                        f"{_cd_exc!r}")
        if session_id and "session" in result.stderr.lower():
            log("retrying without --resume after session error")
            return call_agent(prompt, None)
        return (f"[claude exit {result.returncode}]\n{result.stderr[:1500]}", session_id)

    try:
        data = json.loads(result.stdout)
        reply = data.get("result") or data.get("text") or result.stdout
        new_session = data.get("session_id") or session_id
        # §5: bind the (new) session to the tier it ran on so the next message's
        # --resume stays on the same account (I2). Idempotent on a resume.
        if new_session:
            try:
                at.record_session_tier(new_session, tier)
            except Exception:
                pass
        # §8/§10-W2: stamp an account-tagged cost row so this bot's burn is
        # visible to the per-tier rolling-5h reader (this path does NOT route
        # through inbox_watcher, which is the only writer today).
        try:
            at.append_cost_row(
                tier,
                model=(data.get('model') or ''),
                cost_usd=data.get('total_cost_usd'),
                usage=data.get('usage'),
                agent=f'{AGENT}-telegram-bot',
                source=f'{AGENT}-telegram-bot',
                duration_sec=round(time.time() - _t0, 2),
            )
        except Exception:
            pass
        return (reply.strip(), new_session)
    except json.JSONDecodeError:
        return (result.stdout.strip() or "[empty response]", session_id)


# ---------- main loop ----------

def main() -> None:
    _require_runtime_env()   # fail-fast on a misconfigured live env (deferred from import)
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
