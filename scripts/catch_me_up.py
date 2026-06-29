#!/usr/bin/env python3
"""catch_me_up.py — Beacon Telegram shortcut synthesizer.

Spec: agents/beacon/specs/operator-ux-catch-me-up-shortcut.md

Produces a single bounded Telegram message with five sections summarizing
chain state since the operator's last `catch me up` checkpoint:

  1. Merged PRs        (gh pr list --state merged)
  2. Open PRs          (gh pr list --state open)
  3. Pending approvals (~/agents/state/beacon-pending-approvals.json)
  4. Live alerts       (~/agents/blackboard/larry-alerts.jsonl, NOW+SOON only)
  5. Sequences         (~/agents/blackboard/build-sequences/*.json, active+paused)

Recognition is the bot's job (see beacon_telegram_bot.py). This module owns
synthesis + checkpoint persistence + the response shape.

Design constraints (spec § 2 / § 4):

  - No chain mutations. Read-only refetch from ground truth, never cached
    snapshot. PLAN_SYNTHESIS_DISCIPLINE compliant.
  - Per-section row cap (SECTION_ROW_CAP, currently 3). Overflow rendered
    as a single "(+N more)" line; no dashboard link wired in v1 since the
    dashboard URL is environment-specific.
  - Checkpoint advances on every invocation (decision: spec § 3 open
    question 4 — advance immediately, not after acknowledgment, so two
    quick checks show only the delta).
  - Bounded latency: all I/O is local files + one `gh` subprocess. No
    Claude round-trip.

Stdlib only.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
from typing import Optional

AGENTS_ROOT = Path(os.environ.get("OURLIBERTY_AGENTS_ROOT") or Path.home() / "agents")
CHECKPOINT_FILE = AGENTS_ROOT / "state" / "beacon-catch-me-up-checkpoint.json"
APPROVALS_FILE = AGENTS_ROOT / "state" / "beacon-pending-approvals.json"
ALERTS_FILE = AGENTS_ROOT / "blackboard" / "larry-alerts.jsonl"
SEQUENCES_DIR = AGENTS_ROOT / "blackboard" / "build-sequences"

GH_REPO = "Larry-Yatch/ourliberty-agent-core"
GH_LIST_LIMIT = 20
GH_TIMEOUT_SEC = 15

DEFAULT_LOOKBACK_HOURS = 24
SECTION_ROW_CAP = 3

# Tier vocabulary mirrors config/alert-translations.json. Alerts default to
# SOON when no translation entry exists (per spec § 2 of the alert-taxonomy
# spec). Both NOW and SOON surface here; FYI is filtered out.
ALERT_TIERS_SURFACED = {"NOW", "SOON"}

# Recognition vocabulary — case-insensitive, punctuation-stripped exact match.
# Phrasing variants requested by the spec's § 2 sketch. Operator can extend
# without re-deploying via the future per-user prefs file.
SHORTCUT_PHRASES = {
    "catch me up",
    "status",
    "whats happening",
    "what's happening",
    "where are we",
    "update me",
    "summary",
}


# ---------- recognition (helper used by beacon_telegram_bot.py) ----------


def _normalize(text: str) -> str:
    """Lowercase, strip leading slash + punctuation + whitespace.

    Mirrors strip_leading_slash semantics in telegram_text_utils.py but adds
    punctuation stripping so `Status?` and `catch me up!` both match.
    """
    text = text.strip()
    if text.startswith("/"):
        text = text[1:]
    text = text.lower().strip()
    # Strip trailing punctuation only; internal apostrophes (what's) matter.
    text = re.sub(r"[.?!,;:]+$", "", text).strip()
    return text


def is_catch_me_up_shortcut(text: str) -> bool:
    """Return True if `text` matches a catch-me-up shortcut phrase.

    Match is case-insensitive after normalization (slash + trailing
    punctuation stripped). Internal punctuation is preserved so the
    apostrophe in `what's happening` stays.
    """
    return _normalize(text) in SHORTCUT_PHRASES


# ---------- checkpoint persistence ----------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_checkpoint_file() -> dict:
    if not CHECKPOINT_FILE.exists():
        return {}
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_checkpoint(chat_id: int) -> Optional[str]:
    """Return last-checkpoint ISO timestamp for chat_id, or None on first use."""
    data = _read_checkpoint_file()
    val = data.get(str(chat_id))
    return val if isinstance(val, str) and val else None


def save_checkpoint(chat_id: int, ts_iso: str) -> None:
    """Atomically persist the new checkpoint timestamp for chat_id."""
    data = _read_checkpoint_file()
    data[str(chat_id)] = ts_iso
    try:
        CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CHECKPOINT_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.rename(CHECKPOINT_FILE)
    except OSError:
        # Best-effort. Worst case: next invocation re-uses prior checkpoint
        # → duplicate rows surfaced, but never a missed delta.
        pass


# ---------- data fetchers (each returns a small list of dicts) ----------


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # gh emits Z-suffix; fromisoformat handles +00:00 but not Z prior to
        # Python 3.11 in all paths — normalize.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_gh(cmd: list[str]) -> Optional[list[dict]]:
    """Run a gh subprocess. Returns parsed JSON list or None on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def fetch_merged_prs_since(since: datetime) -> list[dict]:
    """Merged PRs with mergedAt >= since. Sorted newest first."""
    data = _run_gh([
        "gh", "pr", "list",
        "--repo", GH_REPO,
        "--state", "merged",
        "--limit", str(GH_LIST_LIMIT),
        "--json", "number,title,mergedAt,url",
    ])
    if not data:
        return []
    out = []
    for pr in data:
        merged_at = _parse_iso(pr.get("mergedAt", ""))
        if merged_at and merged_at >= since:
            out.append(pr)
    out.sort(key=lambda p: p.get("mergedAt", ""), reverse=True)
    return out


def fetch_open_prs() -> list[dict]:
    """Currently open PRs (any author). Sorted newest first by updatedAt."""
    data = _run_gh([
        "gh", "pr", "list",
        "--repo", GH_REPO,
        "--state", "open",
        "--limit", str(GH_LIST_LIMIT),
        "--json", "number,title,updatedAt,url,isDraft",
    ])
    if not data:
        return []
    data.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
    return data


def fetch_pending_approvals() -> list[dict]:
    """Currently pending approvals from beacon's queue."""
    if not APPROVALS_FILE.exists():
        return []
    try:
        with open(APPROVALS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    pending = data.get("pending") if isinstance(data, dict) else None
    return pending if isinstance(pending, list) else []


def fetch_live_alerts_since(since: datetime) -> list[dict]:
    """Raw alerts (kind != notification) since `since`, filtered to NOW+SOON tiers.

    Alert tier comes from the (source, subject) translation lookup in
    larry_alerts.translate_alert. Unmatched alerts default to SOON, so they
    surface — silence-on-unmatched is forbidden per the alert-taxonomy spec.
    """
    if not ALERTS_FILE.exists():
        return []
    try:
        import larry_alerts  # local import keeps catch_me_up importable in tests
    except ImportError:
        return []
    out: list[dict] = []
    try:
        with open(ALERTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") == "notification":
                    continue
                ts = _parse_iso(rec.get("ts", ""))
                if not ts or ts < since:
                    continue
                translation = larry_alerts.translate_alert(
                    rec.get("source", ""), rec.get("subject")
                )
                tier = (translation or {}).get("tier", "SOON")
                if tier not in ALERT_TIERS_SURFACED:
                    continue
                rec["_tier"] = tier
                out.append(rec)
    except OSError:
        return []
    # Newest first.
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out


def fetch_active_sequences() -> list[dict]:
    """Build sequences with status in {active, paused}. Newest first by created_at."""
    if not SEQUENCES_DIR.exists():
        return []
    out = []
    try:
        for path in sorted(SEQUENCES_DIR.glob("*.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    seq = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(seq, dict):
                continue
            if seq.get("status") in ("active", "paused"):
                out.append(seq)
    except OSError:
        return []
    out.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return out


# ---------- formatting ----------


def _truncate(text: str, max_len: int = 60) -> str:
    """Trim to max_len with ellipsis. Keeps the message Telegram-friendly."""
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _format_since(checkpoint: Optional[datetime], now: datetime) -> str:
    """Human-readable 'since X' header."""
    if checkpoint is None:
        return f"since {DEFAULT_LOOKBACK_HOURS}h ago"
    delta = now - checkpoint
    secs = int(delta.total_seconds())
    if secs < 60:
        return "since just now"
    mins = secs // 60
    if mins < 60:
        return f"since {mins}m ago"
    hours = mins // 60
    if hours < 48:
        return f"since {hours}h ago"
    days = hours // 24
    return f"since {days}d ago"


def _cap_rows(rows: list[str]) -> list[str]:
    """Apply SECTION_ROW_CAP with an overflow marker."""
    if len(rows) <= SECTION_ROW_CAP:
        return rows
    extra = len(rows) - SECTION_ROW_CAP
    return rows[:SECTION_ROW_CAP] + [f"  (+{extra} more)"]


def _render_merged_section(prs: list[dict]) -> list[str]:
    if not prs:
        return ["📦 Merged: none"]
    lines = ["📦 Merged:"]
    rows = [f"  #{p['number']} {_truncate(p.get('title', ''))}" for p in prs]
    lines.extend(_cap_rows(rows))
    return lines


def _render_open_section(prs: list[dict]) -> list[str]:
    if not prs:
        return ["🔧 Open PRs: none"]
    lines = ["🔧 Open PRs:"]
    rows = []
    for p in prs:
        marker = " (draft)" if p.get("isDraft") else ""
        rows.append(f"  #{p['number']}{marker} {_truncate(p.get('title', ''))}")
    lines.extend(_cap_rows(rows))
    return lines


def _render_approvals_section(approvals: list[dict]) -> list[str]:
    if not approvals:
        return ["✋ Pending approvals: none"]
    lines = ["✋ Pending approvals:"]
    rows = [
        f"  {a.get('id', '?')} — {_truncate(a.get('plan_summary', ''))}"
        for a in approvals
    ]
    lines.extend(_cap_rows(rows))
    return lines


def _render_alerts_section(alerts: list[dict]) -> list[str]:
    if not alerts:
        return ["🔔 Live alerts (NOW/SOON): none"]
    lines = ["🔔 Live alerts (NOW/SOON):"]
    rows = []
    for a in alerts:
        tier = a.get("_tier", "SOON")
        glyph = "🔴" if tier == "NOW" else "🟡"
        subject = a.get("subject") or a.get("source", "?")
        rows.append(f"  {glyph} {tier} · {_truncate(subject, 50)}")
    lines.extend(_cap_rows(rows))
    return lines


def _render_sequences_section(sequences: list[dict]) -> list[str]:
    if not sequences:
        return ["🧵 Sequences: none active"]
    lines = ["🧵 Sequences:"]
    rows = []
    for s in sequences:
        status = s.get("status", "?")
        current = s.get("current_steps") or []
        current_str = ",".join(current) if current else "—"
        rows.append(
            f"  {s.get('seq_id', '?')} [{status}] · {_truncate(current_str, 40)}"
        )
    lines.extend(_cap_rows(rows))
    return lines


def format_summary(
    *,
    checkpoint: Optional[datetime],
    now: datetime,
    merged_prs: list[dict],
    open_prs: list[dict],
    approvals: list[dict],
    alerts: list[dict],
    sequences: list[dict],
) -> str:
    """Render the five sections into a single Telegram message."""
    header = f"Catch-me-up — {_format_since(checkpoint, now)}"
    sections: list[str] = [header]
    sections.append("")
    sections.extend(_render_merged_section(merged_prs))
    sections.append("")
    sections.extend(_render_open_section(open_prs))
    sections.append("")
    sections.extend(_render_approvals_section(approvals))
    sections.append("")
    sections.extend(_render_alerts_section(alerts))
    sections.append("")
    sections.extend(_render_sequences_section(sequences))
    return "\n".join(sections)


# ---------- top-level synthesis (called by the bot) ----------


def synthesize(chat_id: int) -> str:
    """Return the formatted catch-me-up message for chat_id.

    Side effect: advances the per-chat checkpoint to now BEFORE returning,
    so a follow-up invocation 5 min later shows only the delta since this
    call (spec § 4 acceptance criterion 3).
    """
    now = datetime.now(timezone.utc)
    checkpoint_iso = load_checkpoint(chat_id)
    checkpoint = _parse_iso(checkpoint_iso) if checkpoint_iso else None
    since = checkpoint or (now - timedelta(hours=DEFAULT_LOOKBACK_HOURS))

    merged_prs = fetch_merged_prs_since(since)
    open_prs = fetch_open_prs()
    approvals = fetch_pending_approvals()
    alerts = fetch_live_alerts_since(since)
    sequences = fetch_active_sequences()

    msg = format_summary(
        checkpoint=checkpoint,
        now=now,
        merged_prs=merged_prs,
        open_prs=open_prs,
        approvals=approvals,
        alerts=alerts,
        sequences=sequences,
    )
    save_checkpoint(chat_id, now.isoformat())
    return msg


if __name__ == "__main__":
    # Smoke: print a summary for chat_id 0 (uses default 24h lookback if no
    # prior checkpoint). Does NOT persist a chat_id=0 checkpoint.
    import sys
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print(synthesize(cid))
