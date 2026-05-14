#!/usr/bin/env python3
"""beacon_telegram_bot.py — Telegram <-> Beacon bridge.

Polls Telegram for messages from authorized chat IDs, hands each message
to Claude Code running in Beacon's directory (~/agent-core/agents/beacon/),
and posts the reply back. Conversation continuity is preserved per-chat
via Claude Code's --resume by storing the session id keyed on chat id.

Phase D3 (commit 3) added the approval gate. The bot now intercepts:

  - User messages matching approve / yes / go / ok / ship it (exact, strict
    whitelist) → resolve most-recent pending approval, dispatch via
    safe_write_inbox, confirm to Larry. NO forward to Beacon.
  - User messages prefixed `modify: ...` or `reject: ...` → resolve as
    modified/rejected, forward a system-style note to Beacon explaining
    what happened.
  - User messages `pause` / `/pause` / `resume` / `/resume` → toggle the
    global approval-pause flag. NO forward to Beacon.
  - Beacon responses containing `=== APPROVAL_REQUEST === {json} ===
    END_APPROVAL_REQUEST ===` → extract the plan payload, consult
    trust_policy, and either (a) auto-dispatch + one-liner confirm,
    (b) queue + DM the formatted approval request, or (c) DM the
    policy rejection.
  - Reminder schedule (6h/24h/72h) checked every ~5 min in the polling loop.

Reads from environment:
  TELEGRAM_BOT_TOKEN_BEACON   — bot token from BotFather
  TELEGRAM_ALLOWED_CHAT_IDS   — comma-separated chat IDs allowed to talk to Beacon
                                (anyone else is silently ignored — security boundary)

Run via:  source ~/credentials/.env.larry && python3 ~/agent-core/scripts/beacon_telegram_bot.py

Stdlib only — no pip dependencies.
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

# D3 approval handler (commit 3). Side-effect imports ensure shared module
# paths get added.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import beacon_approval_handler as approval  # noqa: E402
import larry_alerts  # noqa: E402
import safe_write_inbox  # noqa: E402

# ---------- config ----------

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_BEACON", "").strip()
if not TOKEN:
    sys.exit("ERROR: TELEGRAM_BOT_TOKEN_BEACON not set. Source ~/credentials/.env.larry first.")

ALLOWED_RAW = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
ALLOWED: set[int] = {int(x) for x in re.split(r"[,\s]+", ALLOWED_RAW) if x.strip()}
if not ALLOWED:
    sys.exit("ERROR: TELEGRAM_ALLOWED_CHAT_IDS empty — refusing to run a bot anyone can talk to.")

BEACON_DIR = Path.home() / "agent-core" / "agents" / "beacon"
LOG_DIR = Path.home() / "agents" / "logs"
STATE_DIR = Path.home() / "agents" / "state"
SESSION_FILE = STATE_DIR / "beacon_telegram_sessions.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

API = f"https://api.telegram.org/bot{TOKEN}"
TELEGRAM_MAX = 4000  # Telegram caps at 4096; leave headroom for our markers

CLAUDE_BIN = shutil.which("claude") or "/usr/bin/claude"
CLAUDE_TIMEOUT_SEC = 600  # 10 min — long enough for Beacon to think hard

# D3 reminder cadence — check at most every REMINDER_INTERVAL_SEC. With our
# 30s getUpdates long-poll this means roughly every ~5 minutes of wall clock,
# which is more than fine for 6h/24h/72h schedule granularity.
REMINDER_INTERVAL_SEC = 300


# ---------- logging ----------

def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_DIR / "beacon_telegram_bot.log", "a") as f:
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


# ---------- HTTP helpers (stdlib only) ----------

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
    """Send a message, splitting if it exceeds Telegram's 4096-char limit."""
    if not text:
        text = "[empty response]"
    while text:
        chunk, text = text[:TELEGRAM_MAX], text[TELEGRAM_MAX:]
        http_json(f"{API}/sendMessage", {"chat_id": chat_id, "text": chunk})


def telegram_send_action(chat_id: int, action: str = "typing") -> None:
    """Show a 'typing...' indicator while we're processing."""
    http_json(f"{API}/sendChatAction", {"chat_id": chat_id, "action": action})


# ---------- Claude Code bridge ----------

def call_beacon(prompt: str, session_id: Optional[str]) -> tuple[str, Optional[str]]:
    """Run claude in Beacon's directory; return (reply_text, new_session_id)."""
    cmd = [CLAUDE_BIN, "--print", "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += [prompt]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(BEACON_DIR),
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return ("[Beacon timed out after 10 min — please retry, ideally with a tighter scope]", session_id)
    except FileNotFoundError:
        return (f"[claude binary not found at {CLAUDE_BIN}]", session_id)
    except Exception as e:
        return (f"[exception: {e}]", session_id)

    if result.returncode != 0:
        log(f"claude exit {result.returncode}: {result.stderr[:500]}")
        # If --resume failed (stale session), retry once without it
        if session_id and "session" in result.stderr.lower():
            log("retrying without --resume after session error")
            return call_beacon(prompt, None)
        return (f"[claude exit {result.returncode}]\n{result.stderr[:1500]}", session_id)

    # Try to parse JSON output
    try:
        data = json.loads(result.stdout)
        reply = data.get("result") or data.get("text") or result.stdout
        new_session = data.get("session_id") or session_id
        return (reply.strip(), new_session)
    except json.JSONDecodeError:
        # Fallback: assume plaintext output
        return (result.stdout.strip() or "[empty response]", session_id)


# ---------- D3 approval gate ----------

def handle_user_command(chat_id: int, action: dict) -> bool:
    """Handle a recognized user approval command. Returns True if handled
    (caller should NOT forward to Beacon)."""
    kind = action.get('action', 'none')

    if kind == 'pause':
        approval.set_paused(True)
        telegram_send(chat_id, approval.format_pause_confirmation())
        log(f"approval pause activated by {chat_id}")
        return True

    if kind == 'resume':
        approval.set_paused(False)
        backlog = approval.pop_paused_backlog()
        telegram_send(chat_id, approval.format_resume_confirmation(len(backlog)))
        for entry in backlog:
            telegram_send(entry.get('chat_id') or chat_id,
                          approval.format_approval_dm(entry))
        log(f"approval resume activated by {chat_id}, backlog={len(backlog)}")
        return True

    if kind == 'approve':
        entry = approval.most_recent_pending()
        if entry is None:
            telegram_send(chat_id,
                          "Nothing pending to approve right now.")
            return True
        try:
            dest = approval.dispatch_approved(entry)
            approval.resolve(entry['id'], 'approved')
            telegram_send(chat_id, approval.format_dispatch_confirmation(entry))
            log(f"approved {entry['id']} -> dispatched to {dest}")
        except (safe_write_inbox.DispatchRejected,
                safe_write_inbox.RoutingDenied) as e:
            telegram_send(chat_id,
                f"Dispatch FAILED for {entry['id']}: {type(e).__name__}: {e}. "
                f"Entry remains pending — fix the issue and retry approval.")
            log(f"dispatch failed for {entry['id']}: {e}")
        return True

    if kind == 'modify':
        entry = approval.most_recent_pending()
        if entry is None:
            telegram_send(chat_id, "Nothing pending to modify right now.")
            return True
        reason = action.get('reason', '')
        # D3.5 5c-followup-3 (audit 3.A): capture the system-controlled replan
        # state from the entry being modified. Without this, Beacon's chat-
        # mode re-plan via _send_beacon_response would call add_pending(...)
        # without replan_count/max_replans, defeating the budget cap. The
        # next Forge→Mirror cycle's REVIEW_ESCALATE notify would carry
        # replan_count=0 and the loop becomes unbounded.
        prior_replan_count = entry.get('_replan_count', 0)
        prior_max_replans = entry.get('_max_replans')
        approval.resolve(entry['id'], 'modified', note=reason)
        # Forward a structured note to Beacon so she can re-plan.
        relay = (
            f"[D3 approval gate] Larry asked to MODIFY plan {entry['id']}. "
            f"Reason: {reason}. The previous plan was archived as 'modified'. "
            f"Please propose a revised plan with a new APPROVAL_REQUEST marker, "
            f"taking the modification request into account."
        )
        session_id = _bot_state['sessions'].get(str(chat_id))
        reply, new_session = call_beacon(relay, session_id)
        if new_session and new_session != session_id:
            _bot_state['sessions'][str(chat_id)] = new_session
            save_sessions(_bot_state['sessions'])
        _send_beacon_response(
            chat_id, reply,
            inherited_replan_count=prior_replan_count,
            inherited_max_replans=prior_max_replans,
        )
        return True

    if kind == 'reject':
        entry = approval.most_recent_pending()
        if entry is None:
            telegram_send(chat_id, "Nothing pending to reject right now.")
            return True
        reason = action.get('reason', '')
        # 5c-followup-3 audit 3.A: same replan-state capture as the modify
        # branch above. A `reject:` on a replan-pending entry sometimes
        # bounces Beacon into emitting a different replan; without this
        # capture, the new entry would lose replan_count tracking.
        prior_replan_count = entry.get('_replan_count', 0)
        prior_max_replans = entry.get('_max_replans')
        approval.resolve(entry['id'], 'rejected', note=reason)
        telegram_send(chat_id, f"❌ Rejected: {entry['id']}. Beacon notified.")
        relay = (
            f"[D3 approval gate] Larry REJECTED plan {entry['id']}. "
            f"Reason: {reason}. Plan archived. Acknowledge and stand by."
        )
        session_id = _bot_state['sessions'].get(str(chat_id))
        reply, new_session = call_beacon(relay, session_id)
        if new_session and new_session != session_id:
            _bot_state['sessions'][str(chat_id)] = new_session
            save_sessions(_bot_state['sessions'])
        _send_beacon_response(
            chat_id, reply,
            inherited_replan_count=prior_replan_count,
            inherited_max_replans=prior_max_replans,
        )
        return True

    return False


def _send_beacon_response(
    chat_id: int, reply: str,
    inherited_replan_count: int = 0,
    inherited_max_replans=None,
) -> None:
    """Send Beacon's response with approval-marker interception.

    If Beacon emitted `=== APPROVAL_REQUEST ===`, extract the plan, consult
    trust_policy, and either dispatch directly (auto_approve) or queue +
    DM the formatted request (force_ask). The marker block is stripped
    from the narrative shown to Larry.

    D3.5 5c-followup-3 (audit 3.A): `inherited_replan_count` /
    `inherited_max_replans` carry the system-controlled replan budget
    forward when this response is Beacon's chat-mode re-plan after Larry
    replied `modify:` or `reject:` to a replan-pending entry. The bot's
    handle_user_command captures these from the entry being modified and
    passes them through so the next add_pending preserves budget tracking.
    Without this, every modify/reject on a replan resets the counter and
    defeats max_replans.
    """
    try:
        payload, narrative = approval.extract_approval_request(reply)
    except approval.MalformedApprovalMarker as e:
        telegram_send(chat_id, reply)
        telegram_send(
            chat_id,
            f"⚠ Beacon's APPROVAL_REQUEST marker was malformed ({e}). "
            f"No approval flow triggered.",
        )
        log(f"malformed approval marker from beacon: {e}")
        return

    if payload is None:
        telegram_send(chat_id, reply)
        return

    # Marker present — handle approval flow.
    action_str, rule = approval.trust_decision(payload)

    if narrative:
        telegram_send(chat_id, narrative)

    if action_str == 'reject':
        telegram_send(chat_id, approval.format_policy_rejection(payload, rule or {}))
        log(f"trust_policy rejected: {payload.get('task_id')}")
        return

    if action_str == 'auto_approve':
        entry = approval.add_pending(
            payload, chat_id=chat_id,
            replan_count=inherited_replan_count,
            max_replans=inherited_max_replans,
        )
        try:
            approval.dispatch_approved(entry)
            approval.resolve(entry['id'], 'approved',
                             note=f'auto_approved by rule: {rule}')
            telegram_send(chat_id,
                          approval.format_auto_approve_confirmation(entry, rule or {}))
            log(f"auto_approved + dispatched: {payload.get('task_id')}")
        except (safe_write_inbox.DispatchRejected,
                safe_write_inbox.RoutingDenied) as e:
            telegram_send(
                chat_id,
                f"Auto-approve dispatch FAILED for {entry['id']}: "
                f"{type(e).__name__}: {e}",
            )
            log(f"auto_approve dispatch failed for {entry['id']}: {e}")
        return

    # force_ask path
    queued = approval.is_paused()
    entry = approval.add_pending(
        payload, chat_id=chat_id,
        queued_during_pause=queued,
        replan_count=inherited_replan_count,
        max_replans=inherited_max_replans,
    )
    if queued:
        telegram_send(
            chat_id,
            f"⏸ Approval queued during pause: {entry['id']}. "
            f"It'll be DMed on /resume.",
        )
        log(f"approval queued during pause: {entry['id']}")
    else:
        telegram_send(chat_id, approval.format_approval_dm(entry))
        log(f"approval DMed for {entry['id']}")


def _check_due_reminders() -> None:
    """Send any reminders that have crossed their threshold."""
    due = approval.due_reminders()
    for entry, hours in due:
        chat_id = entry.get('chat_id')
        if chat_id is None:
            continue
        telegram_send(chat_id, approval.format_reminder_dm(entry, hours))
        approval.record_reminder_sent(entry['id'], hours)
        log(f"reminder sent ({hours}h) for {entry['id']}")


def _send_alert_dm(chat_id: int, text: str) -> bool:
    """Send a chunked DM; return True only if every chunk got HTTP 200 + ok=True.

    Per-line ack on the alert queue depends on this returning truthfully —
    advancing the offset on a half-failed send loses the alert (M2 fix).
    """
    if not text:
        return True
    chunks: list[str] = []
    remaining = text
    while remaining:
        chunks.append(remaining[:TELEGRAM_MAX])
        remaining = remaining[TELEGRAM_MAX:]
    for chunk in chunks:
        result = http_json(f"{API}/sendMessage",
                           {"chat_id": chat_id, "text": chunk})
        if not result or not result.get("ok"):
            return False
    return True


def _check_pending_alerts() -> None:
    """Poll the shared larry-alerts queue and DM each new line.

    Two record shapes share the same queue file (D3.5 5a-followup):

    1. **Alerts** (no `kind` field, OR `kind: "alert"`) — infra failures from
       watchdog/sentinel. Broadcast to every authorized chat (whoever's
       available should see infra alerts). Severity emoji prefix; existing
       D3.5-prep behavior.
    2. **Notifications** (`kind: "notification"`) — chain-completion DMs
       (review-pass / revision / escalate / emergency / reject /
       clarification-exhausted). Targeted to the originating `chat_id` only
       (not broadcast). Intent-specific emoji prefix.

    Per-line ack: offset advances ONLY after delivery confirmed for every
    target. Telegram failure -> stop, retry next sweep. At-least-once
    delivery (Telegram-side timeout could result in dup).
    """
    offset = larry_alerts.read_offset()
    pending = larry_alerts.read_pending(offset)
    if not pending:
        return
    for idx, alert in pending:
        if alert.get('_malformed'):
            log(f"alert idx={idx} malformed, skipping: {alert.get('raw', '')[:80]!r}")
            larry_alerts.write_offset(idx + 1)
            continue

        # Determine target chats: notifications + approval-requests go to
        # the originating chat_id only; alerts broadcast to all authorized
        # chats. Both targeted shapes share the chat_id validation path.
        kind = alert.get('kind')
        if kind in ('notification', 'approval_request'):
            target_chat = alert.get('chat_id')
            if not isinstance(target_chat, int) or target_chat not in ALLOWED:
                # Defense-in-depth: a record claiming an unauthorized
                # chat_id gets dropped (offset advances so we don't wedge),
                # not delivered. The writer caller should have validated;
                # this catches misconfigured pipelines or tampering.
                log(
                    f"{kind} idx={idx} has invalid/unauthorized "
                    f"chat_id={target_chat!r}; dropping"
                )
                larry_alerts.write_offset(idx + 1)
                continue
            targets = [target_chat]
        else:
            targets = sorted(ALLOWED)

        # D3.5 5c — approval-request rendering. Look up the live pending
        # entry by approval_id and render via approval.format_approval_dm;
        # this picks up any updates to the formatting since the record was
        # appended. Falls back to the appended body if the entry has
        # already been resolved (race: bot was offline, entry resolved
        # via auto-approve path, now we're reading the queue).
        if kind == 'approval_request':
            approval_id = alert.get('approval_id')
            entry = (
                approval.find_pending_by_id(approval_id)
                if isinstance(approval_id, str) else None
            )
            text = (
                approval.format_approval_dm(entry)
                if entry is not None
                else larry_alerts.format_dm(alert)
            )
        else:
            text = larry_alerts.format_dm(alert)
        all_delivered = True
        for chat_id in targets:
            if not _send_alert_dm(chat_id, text):
                all_delivered = False
                log(f"alert idx={idx} delivery to {chat_id} failed")
                break
        if all_delivered:
            larry_alerts.write_offset(idx + 1)
            kind_desc = alert.get('kind') or 'alert'
            if kind_desc == 'notification':
                tag = f"intent={alert.get('intent')}"
            elif kind_desc == 'approval_request':
                tag = f"approval_id={alert.get('approval_id')}"
            else:
                tag = (
                    f"source={alert.get('source')}, "
                    f"subject={alert.get('subject', '-')}"
                )
            log(f"{kind_desc} idx={idx} delivered ({tag})")
        else:
            # Don't advance — preserve order; retry on next sweep.
            log(f"alert idx={idx} send failed; will retry next sweep")
            return


def _process_update(update: dict) -> None:
    """Process one Telegram update. Raises on truly unexpected errors —
    caller catches at the outer per-update boundary."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    text = msg.get("text", "").strip()
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return

    if chat_id not in ALLOWED:
        log(f"ignored unauthorized chat {chat_id} ({chat.get('username') or '?'}): {text[:50]!r}")
        return

    log(f"<- {chat_id}: {text[:120]!r}")

    # D3 — intercept approval commands before forwarding to Beacon.
    action = approval.parse_user_reply(text)
    if action.get('action') != 'none':
        if handle_user_command(chat_id, action):
            return

    telegram_send_action(chat_id, "typing")

    session_id = _bot_state['sessions'].get(str(chat_id))
    reply, new_session = call_beacon(text, session_id)
    if new_session and new_session != session_id:
        _bot_state['sessions'][str(chat_id)] = new_session
        save_sessions(_bot_state['sessions'])

    log(f"-> {chat_id}: {reply[:120]!r}")
    _send_beacon_response(chat_id, reply)


# ---------- main loop ----------

# Shared state visible to the approval handler (modify/reject paths need
# to read+write sessions because they call back into Beacon).
_bot_state: dict = {'sessions': {}}


def main() -> None:
    log(f"Beacon bot starting (cwd={BEACON_DIR}, allowed={sorted(ALLOWED)})")
    _bot_state['sessions'] = load_sessions()
    offset = 0
    last_reminder_check = 0.0

    while True:
        # Periodic reminder + alert sweep — rate-limited. Same cadence
        # (REMINDER_INTERVAL_SEC) because both are bounded-cost passes.
        now = time.time()
        if now - last_reminder_check >= REMINDER_INTERVAL_SEC:
            try:
                _check_due_reminders()
            except Exception as e:
                log(f"reminder sweep error: {type(e).__name__}: {e}")
            try:
                _check_pending_alerts()
            except Exception as e:
                log(f"alert sweep error: {type(e).__name__}: {e}")
            last_reminder_check = now

        url = f"{API}/getUpdates?offset={offset}&timeout=30"
        data = http_json(url, timeout=35)
        if not data or not data.get("ok"):
            time.sleep(3)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            try:
                _process_update(update)
            except Exception as e:
                # NEVER let a single bad update crash the bot — the message
                # would replay on systemd restart, looping forever.
                log(f"unhandled error processing update: {type(e).__name__}: {e}")
                try:
                    msg = update.get("message") or update.get("edited_message") or {}
                    chat_id = msg.get("chat", {}).get("id")
                    if chat_id in ALLOWED:
                        telegram_send(chat_id,
                            f"⚠ Bot internal error: {type(e).__name__}: {e}. "
                            f"Check journalctl -u ourliberty-beacon-bot.")
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted, shutting down")
