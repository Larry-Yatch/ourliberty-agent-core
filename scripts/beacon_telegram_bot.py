#!/usr/bin/env python3
"""beacon_telegram_bot.py — Telegram <-> Beacon bridge.

Polls Telegram for messages from authorized chat IDs, hands each message
to Claude Code running in Beacon's directory (~/agent-core/agents/beacon/),
and posts the reply back. Conversation continuity is preserved per-chat
via Claude Code's --resume by storing the session id keyed on chat id.

Phase D3 (commit 3) added the approval gate. The bot now intercepts:

  - User messages matching approve / go / ok / ship it (exact, strict
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

import fcntl
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
import active_tier  # noqa: E402  # setup-token precedence (tier2 fallback auth)
import agent_runner  # noqa: E402  # shared rate-limit ledger writer (Step C)
import beacon_approval_handler as approval  # noqa: E402
import catch_me_up  # noqa: E402  # operator-UX shortcut synthesizer
import chain_event_emit  # noqa: E402  # E4.4e PR-A: approval_request push writer
import larry_alerts  # noqa: E402
import safe_write_inbox  # noqa: E402
import state_log_query  # noqa: E402  # work-in-flight State Log reader (Slice 1 D4)
from telegram_text_utils import strip_leading_slash  # noqa: E402
from test_isolation_guard import refuse_under_test  # noqa: E402

# ---------- config ----------

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_BEACON", "").strip()
if not TOKEN:
    sys.exit("ERROR: TELEGRAM_BOT_TOKEN_BEACON not set. Source ~/credentials/.env.larry first.")

ALLOWED_RAW = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
ALLOWED: set[int] = {int(x) for x in re.split(r"[,\s]+", ALLOWED_RAW) if x.strip()}
if not ALLOWED:
    sys.exit("ERROR: TELEGRAM_ALLOWED_CHAT_IDS empty — refusing to run a bot anyone can talk to.")

BEACON_DIR = Path.home() / "agent-core" / "agents" / "beacon"

# Beacon chat model. Single source of truth is config/agent-models.json
# (beacon.telegram_model) — the same file inbox_watcher reads via a
# __file__-relative path. The pinned fallback keeps the 1M context window
# Beacon chat depends on for long spec sessions.
_MODELS_CONFIG_PATH = _SCRIPT_DIR.parent / "config" / "agent-models.json"
_DEFAULT_TELEGRAM_MODEL = "claude-opus-4-8[1m]"


def _beacon_telegram_model() -> str:
    try:
        cfg = json.loads(_MODELS_CONFIG_PATH.read_text())
        m = cfg.get("agents", {}).get("beacon", {}).get("telegram_model")
        if m:
            return m
    except Exception:
        pass
    return _DEFAULT_TELEGRAM_MODEL


def resolve_log_dir() -> Path:
    """Return the directory this module writes its log file to.

    OURLIBERTY_LOG_DIR override exists so test runs do not leak sentinel
    strings (TIER_ONE_MARKER, '401 Unauthorized', etc.) into the live
    beacon_telegram_bot.log when a test imports this module and triggers
    log() via mocked subprocess flows. Production keeps the env var unset,
    preserving the historical path.
    """
    override = os.environ.get("OURLIBERTY_LOG_DIR")
    return Path(override) if override else Path.home() / "agents" / "logs"


LOG_DIR = resolve_log_dir()
STATE_DIR = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT") or Path.home() / "agents") / "state"
SESSION_FILE = STATE_DIR / "beacon_telegram_sessions.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

API = f"https://api.telegram.org/bot{TOKEN}"
TELEGRAM_MAX = 4000  # Telegram caps at 4096; leave headroom for our markers

CLAUDE_BIN = shutil.which("claude") or "/usr/bin/claude"
CLAUDE_TIMEOUT_SEC = 600  # 10 min — long enough for Beacon to think hard

# fix-mirror-verdict-marker-gate-001 (2026-06-03) — bounded in-loop kickback
# for malformed APPROVAL_REQUEST markers. Parallel intent to the notifier's
# MAX_MARKER_ERROR_RETRIES (the Forge/Mirror marker-error cascade), but the
# telegram bot is a SYNCHRONOUS call_beacon request/response loop — not the
# file-based outbox daemon — so we re-prompt Beacon in-process instead of
# writing dead-letter inbox files. Same cap (3) keeps the budgets aligned.
MAX_APPROVAL_MARKER_RETRIES = 3

# harden-authoritative-dispatch-confirmation (2026-06-04). Cap on the prose-guard
# kickback: a no-marker reply that asserts a COMPLETED dispatch is re-prompted up
# to this many times before the bot intercepts loudly (Larry sees a "no real
# dispatch happened" notice, never the phantom prose). Mirrors the malformed-marker
# budget above.
MAX_COMPLETION_CLAIM_RETRIES = 3

# D3 reminder cadence — check at most every REMINDER_INTERVAL_SEC. With our
# 30s getUpdates long-poll this means roughly every ~5 minutes of wall clock,
# which is more than fine for 6h/24h/72h schedule granularity.
REMINDER_INTERVAL_SEC = 300

# Tier 2 fallback (claude-quota-tier2-fallback-wrapper, 2026-05-26).
# When the Tier 1 (agent) Claude Max account hits rate-limit OR auth-401,
# retry once with HOME swapped to Larry's personal Claude Max OAuth dir.
# Separate accounts = separate quota + auth buckets.
TIER2_HOME = "/home/larry/.claude-larry-personal"

# Detection regexes run against STDOUT (not stderr — the Claude CLI emits
# both rate-limit and auth-401 messages to stdout). Keep these intentionally
# loose enough to survive minor CLI phrasing changes but tight enough to
# avoid false positives on normal output.
RATE_LIMIT_RE = re.compile(
    r"(hit your limit|5-hour|resets \d+)", re.IGNORECASE
)
AUTH_401_RE = re.compile(
    r"(401|Invalid authentication credentials|Failed to authenticate)",
    re.IGNORECASE,
)


def classify_tier1_failure(stdout: str, stderr: str) -> Optional[str]:
    """Return 'rate_limit', 'auth_401', or None.

    Detection runs against the combined stdout+stderr so the AUTH_401 'Invalid
    authentication credentials' phrasing is caught regardless of which stream
    the CLI used. Rate-limit takes precedence when both match (today's
    incident: rate-limit phrasing masked the underlying 401 — we err on the
    side of treating the more-recoverable signal first, but EITHER detection
    triggers the same Tier 2 retry).
    """
    combined = (stdout or "") + "\n" + (stderr or "")
    if RATE_LIMIT_RE.search(combined):
        return "rate_limit"
    if AUTH_401_RE.search(combined):
        return "auth_401"
    return None


def tier2_available(home: Optional[str] = None) -> bool:
    """True iff the fallback tier's OAuth credentials file exists.

    Checked BEFORE the HOME-swap so a missing fallback dir DMs Larry instead
    of producing a confusing 'claude: command not found' or empty-credentials
    error inside the subprocess.

    ``home`` defaults to TIER2_HOME (the historical Tier 1 → Tier 2 fallback)
    but is parameterized so the check targets whichever tier is the *opposite*
    of the active pin (active_tier.other_home()). When the team is pinned to
    Tier 2, the fallback — and thus this credentials check — is Tier 1. The
    default is resolved at CALL time (sentinel ``None``) rather than bound at
    import time, so tests that patch ``bot.TIER2_HOME`` reach the no-arg path.
    """
    if home is None:
        home = TIER2_HOME
    return Path(home, ".claude", ".credentials.json").exists()


def _tier2_failure_dm(
    failure_type: str,
    tier2_stdout: Optional[str] = None,
    tier2_stderr: Optional[str] = None,
    active_label: str = "Tier 1",
    other_label: str = "Tier 2",
) -> None:
    """DM Larry that the active tier failed and the fallback was unavailable /
    also failed.

    Uses larry_alerts.append_alert with the existing 'warning' severity; the
    intent ('claude_tier1_failed_tier2_unavailable') is in the subject for
    cooldown bucketing (kept stable so existing dedup buckets carry over).

    When the fallback subprocess actually ran, the caller passes its stdout/
    stderr — both are surfaced in the DM body so Larry can distinguish
    'missing' vs 'auth-401' vs 'also rate-limited' from a single DM. Without
    this, both failure modes looked identical.

    ``active_label`` / ``other_label`` name the live active and fallback tiers
    (the pin can put either tier in either role); defaults preserve the
    historical Tier 1 → Tier 2 wording.
    """
    if failure_type == "rate_limit":
        recovery = (
            f"{active_label} rate-limit: wait for reset (~5h) OR provision "
            f"{other_label} per "
            "docs/runbooks/restore-larry-personal-claude-oauth-tier2.md."
        )
    else:
        recovery = (
            f"{active_label} auth-401: run scripts/auth_orchestrator.py from "
            f"chat to headless-re-auth {active_label}; runbook "
            "docs/runbooks/restore-larry-personal-claude-oauth-tier2.md "
            f"for {other_label} provisioning."
        )
    body = (
        f"Beacon bot subprocess hit {active_label} {failure_type} and "
        f"{other_label} fallback was unavailable or also failed. Beacon will "
        f"not reply to chat until manual recovery."
    )
    if tier2_stdout or tier2_stderr:
        body += (
            f"\n{other_label} stdout: {(tier2_stdout or '')[:300]!r}"
            f"\n{other_label} stderr: {(tier2_stderr or '')[:300]!r}"
        )
    try:
        larry_alerts.append_alert(
            source="beacon-telegram-bot",
            severity="warning",
            message=body,
            subject=f"claude_tier1_failed_tier2_unavailable:{failure_type}",
            suggested_action=recovery,
        )
    except Exception:
        pass


def _tier2_refuse_on_resume_dm(
    failure_type: str,
    active_label: str = "Tier 1",
    other_label: str = "Tier 2",
) -> None:
    """DM Larry that the active tier failed mid-resume and the fallback retry
    was REFUSED.

    Mirrors the refuse-on-resume discipline in agent_runner.py:822-828 — a
    session_id from the active tier is account-bound and CANNOT be replayed
    against the fallback tier's account (a different ~/.claude credentials root
    and a different Anthropic identity). Retrying with --resume would fail with
    'session not found' AND would orphan the original session's context.
    Skipping is the correct outcome; we surface it to Larry so the manual-
    recovery path (wait for the active tier's reset OR drop --resume and start
    a fresh chat on the fallback tier) is visible.

    ``active_label`` / ``other_label`` name the live tiers (defaults preserve
    the historical Tier 1 → Tier 2 wording). The subject string is kept stable
    ('claude_tier1_failed_on_resume_session_bound') so existing alert dedup
    buckets carry over regardless of which tier is active.
    """
    try:
        larry_alerts.append_alert(
            source="beacon-telegram-bot",
            severity="warning",
            message=(
                f"beacon-bot {active_label} failed mid-resume (session-bound); "
                f"manual recovery: wait for {active_label} reset OR clear "
                f"--resume and retry on {other_label} fresh."
            ),
            subject=f"claude_tier1_failed_on_resume_session_bound:{failure_type}",
            suggested_action=(
                f"{active_label} hit "
                f"{failure_type} on a --resume session. Session IDs are "
                f"account-bound — a {other_label} retry would fail with "
                "'session not found'. Wait for the active tier's 5h window to "
                "clear, or start a fresh Beacon chat (no --resume) which will "
                f"route through {other_label}."
            ),
        )
    except Exception:
        pass


def _append_bot_quota_event(
    failure_type: str,
    stdout: Optional[str],
    stderr: Optional[str],
    account: str,
) -> None:
    """Best-effort append to the shared anthropic-quota-events.jsonl ledger
    for a bot-wrapper Claude failure. Step C — pre-fix the bot path DMed
    Larry but never wrote the ledger, which left zero rate_limit/auth_401
    events for 2026-05-29's 6+ stalls. Failures are swallowed: the ledger
    is observation-only and must never block the chat reply.
    """
    combined = (stdout or '') + '\n' + (stderr or '')
    try:
        retry_after = agent_runner._derive_retry_after_sec(combined)
    except Exception:
        retry_after = None
    try:
        agent_runner.append_rate_limit_event(
            agent='beacon-telegram-bot',
            task_id='',
            model='',
            account=account,
            stderr=combined,
            retry_after_sec=retry_after,
            failure_class=failure_type,
        )
    except Exception:
        pass


# ---------- logging ----------

def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    # Re-resolve at write time so OURLIBERTY_LOG_DIR set after import (e.g.,
    # by the autouse test fixture in scripts/tests/conftest.py) still
    # redirects writes. In production the env var is unset and the path
    # collapses to LOG_DIR — no behavior change, no extra disk syscall.
    try:
        with open(resolve_log_dir() / "beacon_telegram_bot.log", "a") as f:
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
    refuse_under_test('telegram-send')
    if not text:
        text = "[empty response]"
    while text:
        chunk, text = text[:TELEGRAM_MAX], text[TELEGRAM_MAX:]
        http_json(f"{API}/sendMessage", {"chat_id": chat_id, "text": chunk})


def telegram_send_action(chat_id: int, action: str = "typing") -> None:
    """Show a 'typing...' indicator while we're processing."""
    http_json(f"{API}/sendChatAction", {"chat_id": chat_id, "action": action})


# ---------- Claude Code bridge ----------

def _run_claude_once(
    cmd: list, home_override: Optional[str] = None,
    oauth_token: Optional[str] = None,
) -> Optional[subprocess.CompletedProcess]:
    """Single subprocess.run for `claude`. Returns the CompletedProcess
    or None on TimeoutExpired / FileNotFoundError / unexpected exception.

    `home_override`, when set, builds a copy of os.environ with HOME
    swapped to the override path — used for the Tier 2 fallback. We never
    mutate the parent process env; the override lives only on the child.

    `oauth_token`, when set, authenticates the child via
    CLAUDE_CODE_OAUTH_TOKEN — the long-lived setup-token path that mirrors
    agent_runner._apply_tier_auth. It takes precedence over the HOME-swap's
    auto-refreshing .credentials.json, so the fallback no longer 401s when
    the Tier 2 creds.json has lapsed. The token value is never logged.
    """
    env = None
    if home_override or oauth_token:
        env = {**os.environ}
    if home_override:
        env["HOME"] = home_override
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    refuse_under_test('claude-spawn')
    try:
        return subprocess.run(
            cmd,
            cwd=str(BEACON_DIR),
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None


def call_beacon(prompt: str, session_id: Optional[str]) -> tuple[str, Optional[str]]:
    """Run claude in Beacon's directory; return (reply_text, new_session_id).

    Tier selection honors the team-wide pin (active_tier.read()['tier']):
    the PRIMARY attempt authenticates against the ACTIVE tier — HOME =
    active_tier.current_home(), token = that tier's long-lived setup-token —
    mirroring agent_runner (scripts/agent_runner.py:1305). The bot's systemd
    HOME=/home/larry only sets the tier1 default; before this the primary was
    hardwired to Tier 1 and silently drained it even while the rest of the
    team was pinned to Tier 2, so Tier 1 usage climbed until it capped
    (2026-06-29 incident). The fallback targets the OPPOSITE tier
    (active_tier.other_home()).

    On non-zero exit: log BOTH stdout and stderr (the 2026-05-26 incident
    surfaced because only stderr was logged and the rate-limit/auth-401
    message lives on stdout). Detect rate-limit or auth-401 from the combined
    output and, if detected, retry once on the fallback tier's HOME before
    falling back to the error response.

    Resume-discipline rule (mirrors agent_runner.py:822-828): `--resume`
    session IDs are NOT portable between accounts. A fallback-tier retry on a
    --resume session would fail with 'session not found' AND would orphan the
    original session's context. When the active tier fails (rate-limit or
    auth-401) on a request that carries `--resume`, we DM Larry with the
    session-bound recovery instructions and return early — no fallback
    subprocess invocation. When the request has NO `--resume` (fresh session),
    the fallback proceeds as normal. The earlier design here (fallback
    cold-start acceptable for chat sessions) was retired after the
    2026-05-26/27 incident — the cross-account session failure mode is real
    and identical to the agent_runner.py case.

    A stale cross-tier session (e.g. a session created on Tier 1 before the
    pin moved to Tier 2) self-heals: --resume of an unknown session under the
    active tier's HOME errors with 'session not found' (NOT a rate-limit/
    auth-401), which trips the retry-without-resume path below — a fresh
    session is started on the active tier.
    """
    # Resolve the active (pinned) tier and its opposite (the fallback target).
    active_name = active_tier.read()['tier']
    other_name = 'tier2' if active_name == 'tier1' else 'tier1'
    active_home = active_tier.current_home()
    fallback_home = active_tier.other_home()
    _TIER_LABEL = {'tier1': 'Tier 1', 'tier2': 'Tier 2'}
    active_label = _TIER_LABEL[active_name]
    other_label = _TIER_LABEL[other_name]

    cmd = [CLAUDE_BIN, "--print", "--output-format", "json", "--model", _beacon_telegram_model()]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += [prompt]

    # Primary attempt authenticates via the ACTIVE tier's long-lived setup-
    # token — the account active_tier.current_home() is bound to — mirroring
    # agent_runner._apply_tier_auth. Without it the primary fell back to HOME's
    # ~/.claude/.credentials.json, which rots silently on an OAuth refresh
    # failure and then 401s every message; on a --resume session the fallback
    # below is refused (session-bound), leaving Beacon dead to Larry. The token
    # matches the session's bound account, so --resume continuity is preserved.
    # None (token unconfigured) => HOME's credentials.json (prior behavior).
    # Log the auth source + tier (NOT the token) so a future auth/routing
    # regression is visible in the log — the 2026-06-25 incident was invisible
    # precisely because the bot's primary auth path emitted no signal.
    _primary_token = active_tier._setup_token_for_tier(active_name)
    log(f"call_beacon: primary auth="
        f"{'setup_token' if _primary_token else 'credentials_json'} "
        f"({active_name}) home={active_home}")
    result = _run_claude_once(
        cmd, home_override=active_home, oauth_token=_primary_token,
    )
    if result is None:
        return ("[Beacon timed out or claude binary missing — please retry]", session_id)

    if result.returncode != 0:
        # Log BOTH streams — stderr-only logging was the gap that masked
        # today's auth-401 (CLI puts both rate-limit and 401 phrasing on stdout).
        log(
            f"claude exit {result.returncode}: "
            f"stdout={(result.stdout or '')[:500]!r} "
            f"stderr={(result.stderr or '')[:500]!r}"
        )

        # Fallback detection: classify from the combined output.
        failure_type = classify_tier1_failure(result.stdout, result.stderr)
        if failure_type:
            # Step C: record the active-tier failure (rate_limit OR auth_401)
            # to the shared anthropic-quota-events ledger before any retry/DM
            # path runs. Pre-Step-C this bot only DMed and the dominant
            # auth_401 class never reached the ledger Check VIII relies on.
            _append_bot_quota_event(
                failure_type=failure_type,
                stdout=result.stdout,
                stderr=result.stderr,
                account=active_name,
            )
            # Refuse-on-resume (mirrors agent_runner.py:822-828). A fallback-
            # tier retry on a --resume session would fail with 'session not
            # found' AND orphan the original session's context — session IDs
            # are account-bound. DM Larry the recovery hint and return early;
            # no fallback subprocess invocation. (Log marker stays the literal
            # TIER2_FALLBACK_SKIPPED — heal_pipeline_stall greps it by name.)
            if session_id:
                log(
                    f"TIER2_FALLBACK_SKIPPED reason={failure_type} "
                    f"cause=resume_session_account_bound "
                    f"active={active_name} fallback={other_name}"
                )
                _tier2_refuse_on_resume_dm(
                    failure_type,
                    active_label=active_label,
                    other_label=other_label,
                )
                return (
                    f"[claude {failure_type} on --resume session — "
                    f"{other_label} retry refused (session-bound); DM sent]\n"
                    f"{(result.stdout or '')[:1500]}",
                    session_id,
                )
            # Setup-token precedence (mirrors agent_runner._apply_tier_auth):
            # when the fallback tier's long-lived setup-token is configured, the
            # fallback authenticates via it and does NOT depend on the
            # .credentials.json under the fallback HOME. In that case the
            # existence gate below must NOT short-circuit the retry — the
            # creds.json may be absent/lapsed (intentionally unrefreshed) while
            # dispatch auth via the token is healthy. The token is never logged.
            t2_setup_token = active_tier._setup_token_for_tier(other_name)
            if not t2_setup_token and not tier2_available(fallback_home):
                log(
                    f"TIER2_FALLBACK_UNAVAILABLE reason={failure_type} "
                    f"home={fallback_home} (missing credentials file)"
                )
                _tier2_failure_dm(
                    failure_type,
                    active_label=active_label,
                    other_label=other_label,
                )
                return (
                    f"[claude {failure_type} — {other_label} unavailable; DM sent]\n"
                    f"{(result.stdout or '')[:1500]}",
                    session_id,
                )
            log(
                f"TIER2_FALLBACK_ATTEMPT reason={failure_type} home={fallback_home} "
                f"auth={'setup_token' if t2_setup_token else 'credentials_json'}"
            )
            t2 = _run_claude_once(
                cmd, home_override=fallback_home, oauth_token=t2_setup_token,
            )
            if t2 is not None and t2.returncode == 0:
                log(f"TIER2_FALLBACK_USED reason={failure_type}")
                try:
                    data = json.loads(t2.stdout)
                    reply = data.get("result") or data.get("text") or t2.stdout
                    new_session = data.get("session_id") or session_id
                    return (reply.strip(), new_session)
                except json.JSONDecodeError:
                    return (t2.stdout.strip() or "[empty response]", session_id)
            t2_stdout = t2.stdout if t2 is not None else None
            t2_stderr = t2.stderr if t2 is not None else None
            log(
                f"TIER2_FALLBACK_FAILED reason={failure_type} "
                f"exit={t2.returncode if t2 else 'none'} "
                f"stdout={(t2_stdout or '')[:300]!r} "
                f"stderr={(t2_stderr or '')[:300]!r}"
            )
            # Step C: also capture the fallback-tier failure as a ledger event
            # so the both-tiers-walled-at-once class is visible to Check VIII.
            t2_failure = (
                classify_tier1_failure(t2_stdout or '', t2_stderr or '')
                or failure_type
            )
            _append_bot_quota_event(
                failure_type=t2_failure,
                stdout=t2_stdout,
                stderr=t2_stderr,
                account=other_name,
            )
            # Keep the (failure_type, t2_stdout, t2_stderr) POSITIONAL trio the
            # test asserts on; tier labels ride as kwargs.
            _tier2_failure_dm(
                failure_type, t2_stdout, t2_stderr,
                active_label=active_label, other_label=other_label,
            )
            # Echo the fallback tier's stdout (not the active tier's) in the
            # chat reply so the error body distinguishes 'missing' vs 'auth-401'
            # vs 'also rate-limited'. Before this fix the body was result.stdout
            # (the active tier's), masking the real fallback failure mode.
            return (
                f"[claude {failure_type} — {other_label} retry also failed; DM sent]\n"
                f"{(t2_stdout or '')[:1500]}",
                session_id,
            )

        # If --resume failed (stale session), retry once without it. Check the
        # COMBINED streams: a cross-tier stale session (Tier 1 session resumed
        # under the Tier 2 HOME after a pin move) reports 'No conversation found
        # with session ID' on stdout, not stderr — stderr-only missed it and
        # dead-ended the chat. Broadening to stdout+stderr makes the stale
        # cross-tier session self-heal into a fresh session on the active tier.
        _resume_err = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
        if session_id and "session" in _resume_err:
            log("retrying without --resume after session error")
            return call_beacon(prompt, None)
        return (
            f"[claude exit {result.returncode}]\n{(result.stderr or '')[:1500]}",
            session_id,
        )

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

    if kind == 'approve_graduation':
        template = action.get('template', '')
        entry = approval.find_graduation_pending(template)
        if entry is None:
            telegram_send(chat_id,
                          f"No graduation pending for {template!r}.")
            return True
        try:
            dest = approval.dispatch_approved(entry)
            approval.resolve(entry['id'], 'approved')
            telegram_send(chat_id,
                          f"✅ Graduation approved: {template} is now auto-fix. "
                          f"Config PR dispatched to {entry.get('target_agent', 'forge')}.")
            log(f"graduation approved for {template} -> dispatched to {dest}")
        except (safe_write_inbox.DispatchRejected,
                safe_write_inbox.RoutingDenied) as e:
            telegram_send(chat_id,
                f"Graduation dispatch FAILED for {template}: "
                f"{type(e).__name__}: {e}. Entry remains pending — retry approval.")
            log(f"graduation dispatch failed for {template}: {e}")
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

    harden-authoritative-dispatch-confirmation (2026-06-04): if Beacon's reply
    carries NO marker but asserts a COMPLETED dispatch/approval
    (`approval.is_completion_claim`), it's the 2026-06-03 phantom shape — the
    prose claims a dispatch that never went through the deterministic path. The
    bot intercepts and re-prompts for a real marker (bounded by
    MAX_COMPLETION_CLAIM_RETRIES), never forwarding the phantom to Larry.

    D3.5 5c-followup-3 (audit 3.A): `inherited_replan_count` /
    `inherited_max_replans` carry the system-controlled replan budget
    forward when this response is Beacon's chat-mode re-plan after Larry
    replied `modify:` or `reject:` to a replan-pending entry. The bot's
    handle_user_command captures these from the entry being modified and
    passes them through so the next add_pending preserves budget tracking.
    Without this, every modify/reject on a replan resets the counter and
    defeats max_replans.
    """
    # fix-mirror-verdict-marker-gate-001 (2026-06-03) — bounded in-loop
    # kickback. A malformed APPROVAL_REQUEST marker used to only log + forward
    # the raw reply with a warning (no re-emit chance), parallel to the
    # pre-fix Mirror silent-drop. Now we re-prompt Beacon to re-emit a clean
    # canonical block, capped at MAX_APPROVAL_MARKER_RETRIES, then fall back to
    # forward+warn on exhaust so the thread is loud, never silently dropped.
    # Synchronous re-prompt (not the file-based dead-letter cascade) because
    # this bot is a request/response loop, not the outbox daemon.
    payload = None
    narrative = None
    marker_errors = 0
    claim_errors = 0
    while True:
        try:
            payload, narrative = approval.extract_approval_request(reply)
        except approval.MalformedApprovalMarker as e:
            marker_errors += 1
            if marker_errors > MAX_APPROVAL_MARKER_RETRIES:
                telegram_send(chat_id, reply)
                telegram_send(
                    chat_id,
                    f"⚠ Beacon's APPROVAL_REQUEST marker was malformed "
                    f"{MAX_APPROVAL_MARKER_RETRIES}x in a row ({e}). No approval "
                    f"flow triggered — re-issue the plan when ready.",
                )
                log(
                    f"malformed approval marker from beacon — kickback exhausted "
                    f"({MAX_APPROVAL_MARKER_RETRIES}/{MAX_APPROVAL_MARKER_RETRIES}): {e}"
                )
                return
            log(
                f"malformed approval marker from beacon — kickback "
                f"{marker_errors}/{MAX_APPROVAL_MARKER_RETRIES}: {e}"
            )
            correction = (
                "Your previous response carried an APPROVAL_REQUEST marker that "
                f"failed to parse: {e}\n\n"
                "Re-emit your response with EXACTLY ONE canonical block: "
                "`=== APPROVAL_REQUEST ===` on its own line, a single valid JSON "
                "object with the required fields, then `=== END_APPROVAL_REQUEST ===`. "
                "Put any narrative ABOVE the block — JSON only INSIDE it. Prefer "
                "marker.py to hand-typing the delimiters."
            )
            session_id = _bot_state['sessions'].get(str(chat_id))
            reply, new_session = call_beacon(correction, session_id)
            if new_session and new_session != session_id:
                _bot_state['sessions'][str(chat_id)] = new_session
                save_sessions(_bot_state['sessions'])
            continue

        # Extract succeeded. harden-authoritative-dispatch-confirmation
        # (2026-06-04): if there is NO marker but the reply asserts a COMPLETED
        # dispatch/approval ("Approved — X dispatches to Forge now"), it's the
        # phantom — no pending entry, no safe_write_inbox happened this turn, yet
        # the prose claims one did. Intercept and re-prompt Beacon for a real
        # APPROVAL_REQUEST marker (the deterministic path is the ONLY authoritative
        # "dispatched" emitter); never forward the phantom to Larry. Bounded like
        # the malformed-marker kickback above, then loud-intercept on exhaust.
        if payload is None and approval.is_completion_claim(reply):
            claim_errors += 1
            if claim_errors > MAX_COMPLETION_CLAIM_RETRIES:
                telegram_send(
                    chat_id,
                    "⚠ Beacon asserted a dispatch/approval completed, but emitted "
                    f"no APPROVAL_REQUEST marker {MAX_COMPLETION_CLAIM_RETRIES}x in "
                    "a row — so NOTHING was dispatched. Intercepted: the claim was "
                    "not forwarded. Nothing reached Forge; re-issue when ready.",
                )
                log(
                    "completion-claim with no marker from beacon — kickback "
                    f"exhausted ({MAX_COMPLETION_CLAIM_RETRIES}/"
                    f"{MAX_COMPLETION_CLAIM_RETRIES}); phantom suppressed"
                )
                return
            log(
                "completion-claim with no marker from beacon — kickback "
                f"{claim_errors}/{MAX_COMPLETION_CLAIM_RETRIES}; re-prompting"
            )
            correction = (
                "Your previous response told Larry a dispatch/approval already "
                "COMPLETED (e.g. 'Approved — X dispatches to Forge now'), but you "
                "emitted no APPROVAL_REQUEST marker — so NOTHING was actually "
                "dispatched. The system, not your prose, is the only authoritative "
                "source of a 'dispatched' confirmation.\n\n"
                "If you intend to dispatch: re-emit with EXACTLY ONE canonical "
                "`=== APPROVAL_REQUEST ===` block (JSON only inside, narrative "
                "above; prefer marker.py) so the bot actually performs the "
                "dispatch and confirms it.\n\n"
                "If you only meant to describe intent or status: rephrase so it "
                "does NOT claim a completed dispatch (no 'dispatched', no "
                "'approved — goes to Forge now')."
            )
            session_id = _bot_state['sessions'].get(str(chat_id))
            reply, new_session = call_beacon(correction, session_id)
            if new_session and new_session != session_id:
                _bot_state['sessions'][str(chat_id)] = new_session
                save_sessions(_bot_state['sessions'])
            continue

        break

    if payload is None:
        telegram_send(chat_id, reply)
        return

    # Marker present — handle approval flow. Degrade to force_ask if the trust
    # decision raises for any reason (audit #21): a malformed policy or any
    # unexpected error must surface the decision to Larry, never crash the
    # marker-handling path for this update.
    try:
        action_str, rule = approval.trust_decision(payload)
    except Exception as e:
        log(f"trust_decision raised; defaulting to force_ask: {type(e).__name__}: {e}")
        action_str, rule = 'force_ask', None

    # Record the autonomy decision (powers the Automated Work + needs-Larry views).
    chain_event_emit.emit_event(
        **approval.build_autonomy_decision_chain_event(
            payload, decision=action_str, rule=rule, source='beacon'),
    )

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
        chain_event_emit.emit_event(
            **approval.build_approval_request_chain_event(payload),
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
    chain_event_emit.emit_event(
        **approval.build_approval_request_chain_event(payload),
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
    refuse_under_test('telegram-send')
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

        # Fix-first routing (2026-06-03): route=digest events are successful
        # routine heals that need no DM — they're surfaced in the daily CEO
        # digest instead. Advance the offset and skip (no DM). closure +
        # escalate fall through and DM as before. This is the ONLY no-DM
        # advance besides malformed; the non-digest failed-DM path below still
        # does NOT advance the offset (M2 per-line-ack invariant intact).
        #
        # B1 (alert-pipeline-rework): `hold` joins `digest` as a no-DM route.
        # A held line lands on the dashboard (via the shipper) but does NOT DM
        # Larry; it is later promoted to a DM only by appending a fresh
        # escalate line (held_alert_escalation / append_promotion). The
        # `severity != 'critical'` guard is the read-time half of the
        # critical-always-DMs guarantee (the emit-time half is append_alert
        # forcing route='escalate' for critical): even if a critical somehow
        # carries route=hold/digest, the bot still DMs it.
        if alert.get('route') in ('digest', 'hold') and \
                alert.get('severity') != 'critical':
            log(f"alert idx={idx} route={alert.get('route')}; skipping DM "
                f"(source={alert.get('source')}, subject={alert.get('subject', '-')})")
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

    # Work-in-flight State Log shortcut — intercept "work in flight" / "state
    # of work" / "/state" variants before any Beacon round-trip. Reads the
    # standing State Log the narrator keeps fresh (system self-awareness Slice
    # 1); no chain mutations, no Claude spawn. Distinct from catch_me_up: this
    # is the always-current standing picture, that is a delta synthesis. Spec:
    # agents/beacon/specs/system-awareness-slice-1-state-log.md § D4.
    if state_log_query.is_state_log_query(text):
        try:
            doc = state_log_query.read_state_log()
            reply = state_log_query.format_reply(doc)
        except Exception as e:
            log(f"state_log_query error: {type(e).__name__}: {e}")
            telegram_send(
                chat_id,
                f"⚠ work-in-flight lookup failed ({type(e).__name__}). "
                f"Falling back to normal chat — say it again to retry.",
            )
        else:
            telegram_send(chat_id, reply)
            log(f"state_log answer delivered to {chat_id}")
        return

    # Operator-UX shortcut — intercept "catch me up" / "status" / variants
    # before any Beacon round-trip. Cheap local synthesis from refetched
    # ground truth; no chain mutations. Spec:
    # agents/beacon/specs/operator-ux-catch-me-up-shortcut.md.
    if catch_me_up.is_catch_me_up_shortcut(text):
        try:
            summary = catch_me_up.synthesize(chat_id)
        except Exception as e:
            log(f"catch_me_up synth error: {type(e).__name__}: {e}")
            telegram_send(
                chat_id,
                f"⚠ catch-me-up synthesis failed ({type(e).__name__}). "
                f"Falling back to normal chat — say it again to retry.",
            )
        else:
            telegram_send(chat_id, summary)
            log(f"catch_me_up delivered to {chat_id}")
        return

    # Strip leading `/` AFTER approval handling so pause/resume/approve
    # tokens still match in parse_user_reply, but novel slash-prefixed
    # commands (e.g. /diagnose) reach Beacon instead of being eaten by
    # Claude Code's CLI parser.
    text = strip_leading_slash(text)

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


# Single-instance lock. Held open for the process lifetime — never closed —
# so the exclusive flock persists. Module-global keeps the fd from being
# garbage-collected, which would release the lock.
_LOCK_PATH = Path.home() / "agents" / "state" / "beacon-telegram-bot.lock"
_lock_fd = None


def _acquire_singleton_lock() -> bool:
    """Take an exclusive non-blocking lock. Return True if acquired, False if
    another instance (the production daemon) already holds it. Defense in depth
    so even a no-arg second invocation cannot start a competing getUpdates loop."""
    global _lock_fd
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _lock_fd = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def main() -> None:
    if not _acquire_singleton_lock():
        log(f"another instance already holds {_LOCK_PATH}; exiting without "
            f"polling to avoid a competing getUpdates loop (Telegram 409 burst)")
        return
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
    # This script is the production daemon entrypoint and takes NO subcommands.
    # systemd starts it with zero arguments. Any extra argv means an errant
    # invocation (e.g. `beacon_telegram_bot.py get-last-messages`) which would
    # otherwise start a second getUpdates long-poll and trigger a Telegram 409
    # burst against the daemon. Reject before any Telegram API call.
    if len(sys.argv) > 1:
        sys.stderr.write(
            "beacon_telegram_bot.py is the Beacon daemon entrypoint and takes "
            "no subcommands; it is started by systemd (ourliberty-beacon-bot."
            "service) with no arguments. To read recent Telegram activity use "
            "`tail -N /home/larry/agents/logs/beacon_telegram_bot.log` instead "
            "of invoking this script.\n")
        sys.exit(2)
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted, shutting down")
