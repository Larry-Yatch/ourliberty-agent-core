#!/usr/bin/env python3
"""Pulse Check I — weekly optimization digest.

Spec: `agents/beacon/specs/pulse-check-i.md`.

Runs after Ledger writes the weekly sidecar (Monday morning). Reads
`~/agents/blackboard/ledger/weekly-YYYY-MM-DD.json`, joins Pulse's own
engineering signals (retry overhead, recurring-task repeats from outbox
archives, σ-flagged anomalies), synthesizes 0–3 proposed optimizations
tagged with effort + impact, and emits:

  - A digest DM via `larry_alerts.append_alert` (heartbeat shape when
    nothing is actionable; full digest shape when proposals exist).
  - A `**Check I:**` block appended to `runbooks/cycle-journal.md`.
  - A structured JSON sidecar at `~/agents/blackboard/pulse-check-i/
    check-i-YYYY-MM-DD.json` (firing date) for audit and test verification.

Determinism: no LLM in the loop. Pulse-the-LLM running /cycle may invoke
this script as part of its Mon/Wed/Fri/Sun cycles and extend the digest
with prose; the deterministic baseline ensures the acceptance criteria
in spec § 6 hold regardless.

Triggers:
  - Scheduled: `/cycle` on Mon/Wed/Fri/Sun, after Ledger's sentinel
    exists. cycle-prompt.md § Check I gates this. Ledger itself remains
    weekly Monday; Check I re-reads the same sidecar each firing.
  - Manual: `/optimize` on Telegram. If the sidecar is missing or >24h old,
    this script auto-invokes `scripts/run_ledger.sh` to refresh it before
    proceeding. `--force` skips the Mon/Wed/Fri/Sun weekday gate.

Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# scripts/ on sys.path so sibling-module imports work whether the script is
# invoked directly or via `python3 -m`. safe_write_inbox itself adjusts
# sys.path; doing it here too keeps imports order-independent.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from task_type_inference import infer_task_type
from typing import Any, Optional

import safe_write_inbox  # noqa: E402 — sys.path adjusted above
from fixture_patterns import is_fixture_task_id  # noqa: E402

# --- constants ---

SCHEMA_VERSION = "v1"
SIDECAR_MAX_AGE_DAYS = 7
# Trigger an auto-refresh (run_ledger.sh) when the sidecar is missing or
# older than this many hours. Above SIDECAR_MAX_AGE_DAYS the >7d stale-skip
# takes over instead.
SIDECAR_REFRESH_AGE_HOURS = 24.0
SIDECAR_REFRESH_TIMEOUT_SEC = 120
SCRIPT_DIR = Path(__file__).resolve().parent

# Weekdays on which Check I fires (Monday=0 ... Sunday=6).
# Mon/Wed/Fri/Sun cadence — Ledger remains weekly Monday; Check I re-reads
# the same sidecar on each firing.
CHECK_I_FIRING_WEEKDAYS = frozenset({0, 2, 4, 6})

# Heuristic thresholds — tune after week 2 per spec § 8.
RETRY_OVERHEAD_PCT_THRESHOLD = 15.0
HIGH_REPEAT_COUNT_THRESHOLD = 3  # >= N retry suffixes for same task_id
SIGMA_ANOMALY_ESCALATE_THRESHOLD = 3.0
MAX_PROPOSALS_PER_DIGEST = 3

HOME = Path(os.environ.get("HOME", "/home/larry"))
DEFAULT_SIDECAR_DIR = HOME / "agents" / "blackboard" / "ledger"
DEFAULT_OUTBOX_ROOT = HOME / "agents" / "outboxes"
DEFAULT_OUTPUT_DIR = HOME / "agents" / "blackboard" / "pulse-check-i"
DEFAULT_HALT_FLAG = HOME / "agents" / "blackboard" / "EMERGENCY_HALT"
DEFAULT_JOURNAL = (
    Path(__file__).resolve().parents[1] / "runbooks" / "cycle-journal.md"
)

# Closed-loop step 5 (2026-05-24) — auto-dispatch tunables.
# Eligible-proposal heuristic: effort='small' AND impact contains a dollar
# amount. Deliberately narrow per closed-loop spec § 5 — judgment-shaped
# proposals still surface in the digest DM for Larry to triage.
AUTO_DISPATCH_DOLLAR_RE = re.compile(r"\$\d")
# Same proposal recurring across Check I runs should only dispatch once
# per window. 7 days lines up with the weekly Ledger cadence — a recurring
# σ-anomaly that survives a week is genuinely new evidence.
AUTO_DISPATCH_DEDUP_WINDOW_DAYS = 7
DEFAULT_DISPATCH_STATE_FILE = HOME / "agents" / "state" / "pulse-check-i-dispatched.json"


# --- IO helpers ---


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _default_week_ending(now: Optional[datetime] = None) -> datetime:
    """Most recent Monday at 00:00 UTC."""
    now = now or datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return today - timedelta(days=today.weekday())  # Monday=0


def _load_sidecar(sidecar_dir: Path, week_ending: str) -> Optional[dict[str, Any]]:
    path = sidecar_dir / f"weekly-{week_ending}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _sidecar_age_hours(sidecar_dir: Path, week_ending: str,
                      now: Optional[datetime] = None) -> Optional[float]:
    path = sidecar_dir / f"weekly-{week_ending}.json"
    if not path.exists():
        return None
    now = now or datetime.now(timezone.utc)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (now - mtime).total_seconds() / 3600.0


def _refresh_sidecar() -> None:
    """Invoke run_ledger.sh as a best-effort subprocess.

    Any failure (non-zero exit, timeout, missing script) is logged but does
    not raise — Check I falls back to whatever sidecar state exists on disk.
    """
    script = SCRIPT_DIR / "run_ledger.sh"
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["bash", str(script)],
            timeout=SIDECAR_REFRESH_TIMEOUT_SEC,
            check=False,
            capture_output=True,
        )
        took = time.monotonic() - started
        print(
            f"[pulse-check-i] sidecar refresh: invoked run_ledger.sh, "
            f"exit={result.returncode}, took {took:.1f}s"
        )
        if result.returncode != 0:
            stderr_tail = (result.stderr or b"").decode(
                "utf-8", errors="replace"
            ).strip().splitlines()[-3:]
            print(
                f"[pulse-check-i] WARN: run_ledger.sh non-zero; "
                f"stderr tail: {stderr_tail}"
            )
    except subprocess.TimeoutExpired:
        took = time.monotonic() - started
        print(
            f"[pulse-check-i] WARN: run_ledger.sh timed out after "
            f"{took:.1f}s (limit {SIDECAR_REFRESH_TIMEOUT_SEC}s); continuing"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"[pulse-check-i] WARN: run_ledger.sh invocation failed: "
            f"{type(exc).__name__}: {exc}; continuing"
        )


# --- engineering signals ---


_MARKER_ERROR_PREFIX = "marker-error-"
_DEAD_LETTER_MARKER_PREFIX = "dead-letter-marker-"


def _unwrap_marker_error_base(stem: str) -> str | None:
    """Recover the underlying base task_id from a marker-error wrapped stem.

    Marker-error retries chain: each failed marker emission wraps the prior
    stem with another `marker-error-` prefix and appends a `-<N>` numeric
    suffix. e.g. `opmanual-d35-5b-shipped-note-001` → first retry as
    `marker-error-opmanual-d35-5b-shipped-note-001-1` → second retry as
    `marker-error-marker-error-opmanual-d35-5b-shipped-note-001-1-2` → ...

    Returns the un-wrapped base (here `opmanual-d35-5b-shipped-note-001`)
    or None if the stem is not a marker-error file.
    """
    if not stem.startswith(_MARKER_ERROR_PREFIX):
        return None
    depth = 0
    body = stem
    while body.startswith(_MARKER_ERROR_PREFIX):
        body = body[len(_MARKER_ERROR_PREFIX):]
        depth += 1
    # Strip the trailing `-<digit>` cascade-depth markers (one per wrap).
    for _ in range(depth):
        idx = body.rfind("-")
        if idx == -1 or not body[idx + 1:].isdigit():
            break
        body = body[:idx]
    return body


def gather_retry_repeats(outbox_root: Path) -> list[dict[str, Any]]:
    """Scan outbox archives for tasks with real retry signal.

    `retry_count` reflects the number of *actual retry events* observed for
    a base task_id — not the count of archive entries. The inbox-watcher's
    `safe_write_inbox` rotates any second result with the same task_id to
    `<task>.1.json`, `<task>.2.json`, etc. Those rotations are ambiguous:
    they may be marker-error driven retries, beacon-clarification revision
    rounds, or just chain phases (Forge result + Mirror result both landing
    under the same canonical id). Counting them all as "retries" produced
    false-positive templating proposals (see Pulse audits 2026-05-24/25 on
    `opmanual-d35-5b-shipped-note-001` and `task-34-e4-2-mission-control-
    migration`), so v2 instead counts only artifacts with unambiguous retry
    semantics.

    Counted as retry events:
      - `marker-error-<base>-<N>.json` (and arbitrarily-nested
        `marker-error-marker-error-...-<base>-<N>-<M>-....json` wrappings)
        — each standalone file in the archive is one failed-marker retry of
        the underlying base task. The base is recovered by unwrapping the
        nested `marker-error-` prefixes and stripping the same number of
        trailing `-<digit>` depth markers.

    Skipped (NOT retries):
      - `notify-*` — inter-agent workflow channel; same v1 bug PR #33 fixed
        in Ledger's compute_retry_overhead one level up.
      - `dead-letter-marker-*` — terminal infra-failure marker, written
        once after a marker-error cascade exceeds retry budget. The cascade
        itself already contributes retry events; counting the dead-letter
        too would double-count.
      - Canonical `<task>.json` and `<task>.N.json` plain rotations — these
        are normal chain phases or revision rounds via the rotation-on-
        collision behavior, not retries by the originating dispatch.

    Returns a list of {task_id, agent, retry_count} for entries meeting
    HIGH_REPEAT_COUNT_THRESHOLD, sorted desc by retry_count then task_id.
    """
    counts: dict[tuple[str, str], int] = {}
    if not outbox_root.exists():
        return []
    for agent_dir in outbox_root.iterdir():
        archive = agent_dir / ".archive"
        if not archive.is_dir():
            continue
        for f in archive.iterdir():
            name = f.name
            if not name.endswith(".json"):
                continue
            stem = name[: -len(".json")]
            # dead-letter-marker is a terminal recovery artifact, not a retry.
            if stem.startswith(_DEAD_LETTER_MARKER_PREFIX):
                continue
            base = _unwrap_marker_error_base(stem)
            if base is None:
                # Plain canonical entry or `.N.json` rotation — not a retry
                # event under v2 semantics.
                continue
            # Inherited exclusion: notify-* underlying base is workflow noise.
            if infer_task_type(base) == "notification":
                continue
            # Fixture-pattern allowlist (2026-05-27): test artifacts must not
            # contaminate self-optimizing Check signal. See
            # scripts/fixture_patterns.py for rationale.
            if is_fixture_task_id(base):
                continue
            key = (agent_dir.name, base)
            counts[key] = counts.get(key, 0) + 1
    repeats = [
        {"agent": agent, "task_id": tid, "retry_count": n}
        for (agent, tid), n in counts.items()
        if n >= HIGH_REPEAT_COUNT_THRESHOLD
    ]
    repeats.sort(key=lambda r: (-r["retry_count"], r["task_id"]))
    return repeats


# --- proposal synthesis ---


def synthesize_proposals(
    sidecar: dict[str, Any],
    repeats: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build up to MAX_PROPOSALS_PER_DIGEST proposals from sidecar + signals.

    Each proposal is {title, effort, impact, rationale}. The heuristics are
    deliberately simple — v1; tune after 2 weeks of real data per spec § 8.
    """
    proposals: list[dict[str, Any]] = []

    retry_overhead = sidecar.get("retry_overhead", {}) or {}
    overhead_pct = float(retry_overhead.get("percent_of_total", 0.0) or 0.0)
    overhead_usd = float(retry_overhead.get("total_retry_cost_usd", 0.0) or 0.0)
    if overhead_pct >= RETRY_OVERHEAD_PCT_THRESHOLD:
        proposals.append({
            "title": "Investigate retry / clarification cost sources",
            "effort": "medium",
            "impact": (
                f"~${overhead_usd:.2f}/wk reclaimable ({overhead_pct:.1f}% "
                f"of total spend is retries/clarifications)"
            ),
            "rationale": (
                "Retry overhead is above the 15% threshold. Audit the "
                "outbox-notifier log for the dominant retry shapes (revision, "
                "clarification, cycle-fix) and tighten the upstream "
                "preflight / spec template that caused them."
            ),
        })

    # σ anomalies above the escalate threshold get their own proposal slot.
    # Fixture-pattern allowlist (2026-05-27): drop test-artifact task_ids so a
    # leaked fixture σ outlier can't trigger a hallucinated proposal.
    sigma_hits = [
        a for a in (sidecar.get("anomalies") or [])
        if isinstance(a, dict)
        and a.get("task_id") != "_ramp_up_notice"
        and not is_fixture_task_id(a.get("task_id"))
        and float(a.get("sigma_above", 0.0) or 0.0)
        >= SIGMA_ANOMALY_ESCALATE_THRESHOLD
    ]
    if sigma_hits and len(proposals) < MAX_PROPOSALS_PER_DIGEST:
        top = sigma_hits[0]
        proposals.append({
            "title": f"Review high-σ anomaly task `{top.get('task_id')}`",
            "effort": "small",
            "impact": (
                f"${float(top.get('cost_usd', 0.0)):.2f} task vs "
                f"${float(top.get('baseline_usd', 0.0)):.2f} baseline "
                f"({float(top.get('sigma_above', 0.0)):.1f}σ above)"
            ),
            "rationale": (
                f"Ledger flagged this task at "
                f"{float(top.get('sigma_above', 0.0)):.1f}σ above baseline. "
                f"Read the chain archive and propose either: a fast-path "
                f"for the shape, a prompt-discipline fix, or a model "
                f"downgrade if the depth wasn't warranted."
            ),
        })

    if repeats and len(proposals) < MAX_PROPOSALS_PER_DIGEST:
        top_repeat = repeats[0]
        proposals.append({
            "title": (
                f"Template / fast-path repeating shape "
                f"`{top_repeat['task_id']}`"
            ),
            "effort": "medium",
            "impact": (
                f"{top_repeat['retry_count']} repeats observed this week; "
                f"templating would collapse most retry cycles"
            ),
            "rationale": (
                f"Outbox archives show this task_id retried "
                f"{top_repeat['retry_count']} times on agent "
                f"`{top_repeat['agent']}`. Recurring shapes are the "
                f"prime candidate for the teach-to-fish discipline — "
                f"propose a templated dispatch or an upstream fix to "
                f"Beacon."
            ),
        })

    return proposals[:MAX_PROPOSALS_PER_DIGEST]


# --- auto-dispatch (closed-loop step 5) ---


def _is_auto_dispatch_eligible(proposal: dict[str, Any]) -> bool:
    """A proposal auto-dispatches iff effort=='small' and impact mentions $N.

    The dollar-amount check is the proxy for "quantified" — without it the
    candidate is judgment-shaped and should stay in the digest DM. v1 is
    deliberately narrow per closed-loop spec § 5; tune after observation.
    """
    if proposal.get("effort") != "small":
        return False
    impact = proposal.get("impact") or ""
    if not isinstance(impact, str):
        return False
    return bool(AUTO_DISPATCH_DOLLAR_RE.search(impact))


def _proposal_dedup_key(proposal: dict[str, Any]) -> str:
    """SHA-1 of stable proposal fields. The same recurring proposal across
    Check I runs maps to the same key — different proposals collide only on
    a real content collision (which would still semantically mean "same fix"
    so a single dispatch is correct).
    """
    blob = "␟".join([
        str(proposal.get("title") or ""),
        str(proposal.get("impact") or ""),
        str(proposal.get("rationale") or ""),
    ])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _short_proposal_slug(proposal: dict[str, Any]) -> str:
    """Stable filesystem-safe slug from proposal content (first 10 hex of
    the dedup key). Avoids using the title directly so renames in
    synthesize_proposals don't change the slug shape.
    """
    return _proposal_dedup_key(proposal)[:10]


def _build_dispatch_envelope(
    proposal: dict[str, Any],
    fired_at: datetime,
    sidecar: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Construct the inbox envelope Beacon will receive.

    Beacon's job on arrival is to draft a spec following the standard
    template and emit an APPROVAL_REQUEST marker (step 4 wired the
    extraction). The envelope carries enough context that Beacon doesn't
    need to re-derive the optimization candidate from sidecar files.
    """
    slug = _short_proposal_slug(proposal)
    task_id = f"pulse-auto-{slug}-{fired_at.strftime('%Y%m%d')}"
    title = str(proposal.get("title") or "(untitled proposal)")
    rationale = str(proposal.get("rationale") or "")
    impact = str(proposal.get("impact") or "")
    evidence_lines: list[str] = []
    # Surface a compact slice of the sidecar so Beacon can quote concrete
    # numbers in the spec without round-tripping back to disk.
    if sidecar:
        head_total = sidecar.get("total_usd")
        if head_total is not None:
            evidence_lines.append(f"- Ledger weekly total: ${float(head_total):.2f}")
        anomalies = sidecar.get("anomalies") or []
        if anomalies and isinstance(anomalies[0], dict):
            a = anomalies[0]
            if a.get("task_id") != "_ramp_up_notice":
                evidence_lines.append(
                    f"- Top anomaly: task=`{a.get('task_id')}` "
                    f"cost=${float(a.get('cost_usd', 0.0)):.2f} "
                    f"baseline=${float(a.get('baseline_usd', 0.0)):.2f} "
                    f"σ={float(a.get('sigma_above', 0.0)):.1f}"
                )
    evidence_block = "\n".join(evidence_lines) if evidence_lines else "(no sidecar evidence threaded)"
    prompt = (
        f"{title}\n\n"
        f"Rationale: {rationale}\n\n"
        f"Impact: {impact}\n\n"
        f"Evidence from Ledger sidecar:\n{evidence_block}\n\n"
        f"This is an auto-dispatched optimization candidate from Pulse "
        f"Check I (closed-loop step 5). Read the relevant sidecar / outbox "
        f"archives, then draft a spec following the standard template and "
        f"emit an APPROVAL_REQUEST marker. Larry approves before any build."
    )
    return {
        "task_id": task_id,
        "source": "pulse-auto-dispatch",
        "target_agent": "beacon",
        "target_repo": "ourliberty-agent-core",
        "task_type": "feature-development",
        "phase": "preflight",
        "prompt": prompt,
    }


def _load_dispatch_state(path: Path) -> dict[str, dict[str, Any]]:
    """Read dedup state. Fail-open on any error — better to over-dispatch
    once than to silently never fire. Returns {dedup_key: {task_id,
    dispatched_at_iso}}.
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[pulse-check-i] WARN: dispatch state read failed ({e}); "
              f"treating as empty")
        return {}


def _save_dispatch_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    """Persist dedup state. Best-effort — failure logs a WARN but does not
    propagate (the dispatch already succeeded; a missed state-write means
    we may re-dispatch next run, which is recoverable).
    """
    try:
        _atomic_write(path, json.dumps(state, indent=2) + "\n")
    except OSError as e:
        print(f"[pulse-check-i] WARN: dispatch state write failed ({e})")


def _is_within_dedup_window(
    entry: dict[str, Any], now: datetime, window_days: int,
) -> bool:
    """True iff entry['dispatched_at'] is within `window_days` of `now`."""
    iso = entry.get("dispatched_at")
    if not isinstance(iso, str):
        return False
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts) < timedelta(days=window_days)


def auto_dispatch_proposals(
    check_i: dict[str, Any],
    fired_at: datetime,
    state_path: Path,
    sidecar: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Dispatch eligible proposals to Beacon's inbox, honoring dedup.

    Returns the list of dispatch records actually written (each
    `{task_id, dedup_key, dispatched_at}`). Skipped proposals (ineligible
    or in dedup window) and write failures (DispatchRejected / RoutingDenied)
    are logged but do not propagate.
    """
    proposals = check_i.get("proposals") or []
    if not proposals:
        return []
    state = _load_dispatch_state(state_path)
    dispatched_now: list[dict[str, Any]] = []
    for p in proposals:
        if not _is_auto_dispatch_eligible(p):
            continue
        key = _proposal_dedup_key(p)
        prior = state.get(key)
        if prior and _is_within_dedup_window(
            prior, fired_at, AUTO_DISPATCH_DEDUP_WINDOW_DAYS,
        ):
            print(f"[pulse-check-i] auto-dispatch dedup skip: "
                  f"key={key[:10]} prior_task={prior.get('task_id')} "
                  f"dispatched_at={prior.get('dispatched_at')}")
            continue
        envelope = _build_dispatch_envelope(p, fired_at, sidecar=sidecar)
        task_id = envelope["task_id"]
        try:
            dest = safe_write_inbox.safe_write_inbox(
                target_agent="beacon",
                task_dict=envelope,
                source_agent="pulse-auto-dispatch",
                filename=f"{task_id}.json",
            )
        except safe_write_inbox.DispatchRejected as e:
            print(f"[pulse-check-i] WARN: auto-dispatch rejected for "
                  f"task={task_id}: {e}")
            continue
        except safe_write_inbox.RoutingDenied as e:
            print(f"[pulse-check-i] WARN: auto-dispatch routing denied for "
                  f"task={task_id}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 — never crash Check I on dispatch
            print(f"[pulse-check-i] WARN: auto-dispatch unexpected error for "
                  f"task={task_id}: {type(e).__name__}: {e}")
            continue
        record = {
            "task_id": task_id,
            "dedup_key": key,
            "dispatched_at": fired_at.isoformat(),
            "dest": str(dest),
        }
        dispatched_now.append(record)
        state[key] = {
            "task_id": task_id,
            "dispatched_at": fired_at.isoformat(),
        }
        print(f"[pulse-check-i] auto-dispatched: task={task_id} "
              f"dest={dest} key={key[:10]}")
    if dispatched_now:
        _save_dispatch_state(state_path, state)
    return dispatched_now


# --- manual dispatch (Larry-driven /dispatch <N>) ---


def _find_latest_audit(output_dir: Path) -> Optional[Path]:
    """Return the most-recently-modified `check-i-*.json` in output_dir, or
    None if the directory is empty / missing. Manual dispatch defaults to
    this when --audit isn't supplied.
    """
    if not output_dir.is_dir():
        return None
    candidates = sorted(
        output_dir.glob("check-i-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def manual_dispatch_proposal(
    audit_path: Path,
    n: int,
    state_path: Path,
    fired_at: datetime,
    sidecar: Optional[dict[str, Any]] = None,
) -> int:
    """Dispatch proposal #N (1-indexed) from the audit JSON at audit_path.

    Bypasses _is_auto_dispatch_eligible — Larry's explicit /dispatch is the
    gate. Dedup hits log a WARN and proceed (manual override). Returns 0 on
    successful dispatch, 1 on any error (missing file, out-of-range N,
    safe_write_inbox failure).
    """
    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            audit = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[pulse-check-i] manual-dispatch ERROR: cannot read audit "
              f"{audit_path}: {e}")
        return 1

    proposals = audit.get("proposals") or []
    if n < 1 or n > len(proposals):
        print(f"[pulse-check-i] manual-dispatch ERROR: proposal N={n} out "
              f"of range. Audit {audit_path} has {len(proposals)} "
              f"proposal(s); valid N is 1..{len(proposals) or 0}.")
        return 1

    proposal = proposals[n - 1]
    envelope = _build_dispatch_envelope(proposal, fired_at, sidecar=sidecar)
    task_id = envelope["task_id"]
    key = _proposal_dedup_key(proposal)

    state = _load_dispatch_state(state_path)
    prior = state.get(key)
    if prior and _is_within_dedup_window(
        prior, fired_at, AUTO_DISPATCH_DEDUP_WINDOW_DAYS,
    ):
        print(f"[pulse-check-i] manual-dispatch WARN: proposal previously "
              f"dispatched at {prior.get('dispatched_at')} as task_id "
              f"{prior.get('task_id')}; manual override proceeding.")

    try:
        dest = safe_write_inbox.safe_write_inbox(
            target_agent="beacon",
            task_dict=envelope,
            source_agent="pulse-auto-dispatch",
            filename=f"{task_id}.json",
        )
    except safe_write_inbox.DispatchRejected as e:
        print(f"[pulse-check-i] manual-dispatch ERROR: rejected for "
              f"task={task_id}: {e}")
        return 1
    except safe_write_inbox.RoutingDenied as e:
        print(f"[pulse-check-i] manual-dispatch ERROR: routing denied for "
              f"task={task_id}: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[pulse-check-i] manual-dispatch ERROR: unexpected for "
              f"task={task_id}: {type(e).__name__}: {e}")
        return 1

    state[key] = {
        "task_id": task_id,
        "dispatched_at": fired_at.isoformat(),
    }
    _save_dispatch_state(state_path, state)
    title = str(proposal.get("title") or "(untitled proposal)")
    print(f"[pulse-check-i] manual-dispatch: proposal N={n} [{title}] "
          f"→ beacon inbox {dest}")
    return 0


# --- digest assembly ---


def _round2(x: float) -> float:
    return round(x, 2)


def assemble_check_i(
    sidecar: Optional[dict[str, Any]],
    repeats: list[dict[str, Any]],
    week_ending: str,
    sidecar_filename: Optional[str],
    fired_at: datetime,
) -> dict[str, Any]:
    """Return the structured Check I result.

    Four modes:
      - `skipped` — sidecar unavailable; no DM, just a journal note.
      - `no-signal` — sidecar present, no proposals, no anomalies, no
        repeats, retry overhead below threshold. Scheduled runs suppress
        the DM (closed-loop spec § 4); `--force` (/optimize) still DMs.
      - `heartbeat` — sidecar present, no proposals, but some signal
        present (anomalies / repeats / elevated retry overhead). DM
        still fires so Larry sees the underlying signal.
      - `digest` — sidecar + proposals; full DM.

    `has_signal` is included in the returned dict so the DM gate at the
    call site can decide whether to suppress without re-deriving it.
    """
    if sidecar is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "week_ending": week_ending,
            "ledger_sidecar": None,
            "fired_at": fired_at.isoformat(),
            "mode": "skipped",
            "skip_reason": "Ledger sidecar unavailable",
            "ledger_headline": None,
            "engineering_signals": None,
            "proposals": [],
            "has_signal": False,
        }

    total_usd = float(sidecar.get("total_usd", 0.0) or 0.0)
    delta = sidecar.get("delta_vs_prior_week")
    raw_anoms = sidecar.get("anomalies") or []
    # Fixture-pattern allowlist (2026-05-27): the headline count + the
    # sigma_anomalies list both feed has_signal and the journal block; a
    # fixture leak in either would falsely flag the cycle as actionable.
    real_anoms = [
        a for a in raw_anoms
        if isinstance(a, dict)
        and a.get("task_id") != "_ramp_up_notice"
        and not is_fixture_task_id(a.get("task_id"))
    ]
    retry_overhead = sidecar.get("retry_overhead", {}) or {}
    overhead_pct = float(retry_overhead.get("percent_of_total", 0.0) or 0.0)
    overhead_usd = float(retry_overhead.get("total_retry_cost_usd", 0.0) or 0.0)

    proposals = synthesize_proposals(sidecar, repeats)
    has_signal = bool(
        proposals or real_anoms or repeats
        or overhead_pct >= RETRY_OVERHEAD_PCT_THRESHOLD
    )
    if proposals:
        mode = "digest"
    elif has_signal:
        mode = "heartbeat"
    else:
        mode = "no-signal"

    return {
        "schema_version": SCHEMA_VERSION,
        "week_ending": week_ending,
        "ledger_sidecar": sidecar_filename,
        "fired_at": fired_at.isoformat(),
        "mode": mode,
        "skip_reason": None,
        "ledger_headline": {
            "total_usd": total_usd,
            "delta_vs_prior_week": delta,
            "anomaly_count": len(real_anoms),
        },
        "engineering_signals": {
            "retry_overhead_usd": overhead_usd,
            "retry_overhead_pct": overhead_pct,
            "sigma_anomalies": real_anoms,
            "high_repeat_tasks": repeats,
        },
        "proposals": proposals,
        "has_signal": has_signal,
    }


# --- DM rendering ---


def render_dm(check_i: dict[str, Any]) -> str:
    """Produce the Telegram-bound DM body.

    Spec § 6: heartbeat shape on empty weeks ("Week of X: chain shapes
    nominal"); digest shape with Ledger's headline + Pulse's
    interpretation layer + proposals when actionable.
    """
    week = check_i["week_ending"]
    mode = check_i["mode"]
    if mode == "skipped":
        return (
            f"🩺 Pulse Check I (week of {week}): skipped — "
            f"{check_i.get('skip_reason', 'no reason recorded')}."
        )

    head = check_i["ledger_headline"] or {}
    total_usd = float(head.get("total_usd", 0.0) or 0.0)
    delta = head.get("delta_vs_prior_week")
    delta_phrase = ""
    if isinstance(delta, dict):
        absolute = float(delta.get("absolute_usd", 0.0) or 0.0)
        percent = float(delta.get("percent", 0.0) or 0.0)
        sign = "+" if absolute >= 0 else "−"
        delta_phrase = (
            f" ({sign}${abs(_round2(absolute)):.2f}, "
            f"{sign}{abs(_round2(percent)):.1f}% vs prior)"
        )

    if mode == "no-signal":
        return (
            f"🩺 Pulse Check I (week of {week}): no signal — "
            f"no proposals, no anomalies, no high-repeat tasks, "
            f"retry overhead within bounds. "
            f"Ledger total ${_round2(total_usd):.2f}{delta_phrase}."
        )

    if mode == "heartbeat":
        return (
            f"🩺 Pulse Check I (week of {week}): chain shapes nominal — "
            f"no proposed optimizations this week. "
            f"Ledger total ${_round2(total_usd):.2f}{delta_phrase}."
        )

    lines = [
        f"🩺 Pulse Check I (week of {week}):",
        f"Ledger total ${_round2(total_usd):.2f}{delta_phrase}; "
        f"{head.get('anomaly_count', 0)} σ-flagged anomaly(ies).",
    ]
    sigs = check_i["engineering_signals"] or {}
    overhead_pct = float(sigs.get("retry_overhead_pct", 0.0) or 0.0)
    if overhead_pct > 0:
        lines.append(f"Retry overhead: {_round2(overhead_pct):.1f}% of spend.")
    lines.append("")
    lines.append(f"Proposed optimizations ({len(check_i['proposals'])}):")
    for i, p in enumerate(check_i["proposals"], 1):
        lines.append(
            f"  {i}. [{p['effort']}] {p['title']} — {p['impact']}"
        )
    return "\n".join(lines)


# --- journal block ---


def render_journal_block(check_i: dict[str, Any]) -> str:
    """Render the `**Check I:**` block appended to cycle-journal.md.

    The block is additive — it does not replace the standard A-H cycle
    entry (spec § 6). Pulse-the-LLM running /cycle still writes its
    normal journal section; this block is appended right after.
    """
    week = check_i["week_ending"]
    mode = check_i["mode"]
    lines = ["", f"**Check I ({week}):**", ""]
    if mode == "skipped":
        lines.append(
            f"- Skipped: {check_i.get('skip_reason', 'no reason recorded')}"
        )
        return "\n".join(lines)

    head = check_i["ledger_headline"] or {}
    sigs = check_i["engineering_signals"] or {}
    lines.append(
        f"- Ledger total: ${_round2(float(head.get('total_usd', 0.0))):.2f}; "
        f"{head.get('anomaly_count', 0)} anomaly(ies)"
    )
    overhead_pct = float(sigs.get("retry_overhead_pct", 0.0) or 0.0)
    overhead_usd = float(sigs.get("retry_overhead_usd", 0.0) or 0.0)
    lines.append(
        f"- Retry overhead: ${_round2(overhead_usd):.2f} "
        f"({_round2(overhead_pct):.1f}%)"
    )
    repeats = sigs.get("high_repeat_tasks") or []
    if repeats:
        names = ", ".join(
            f"`{r['task_id']}`×{r['retry_count']}" for r in repeats[:5]
        )
        lines.append(f"- High-repeat tasks: {names}")

    if mode == "no-signal":
        lines.append("- Mode: no-signal — DM suppressed (scheduled run)")
        return "\n".join(lines)

    if mode == "heartbeat":
        lines.append("- Mode: heartbeat (no proposed optimizations)")
        return "\n".join(lines)

    lines.append(f"- Mode: digest — {len(check_i['proposals'])} proposal(s):")
    for i, p in enumerate(check_i["proposals"], 1):
        lines.append(
            f"  {i}. [{p['effort']}] {p['title']} — {p['impact']}"
        )
        lines.append(f"     Rationale: {p['rationale']}")
    return "\n".join(lines)


# Identifies a `**Check I (YYYY-MM-DD):**` header line so append_journal can
# skip re-writing a block whose week is already in the journal (a single week
# is hit by the scheduled /cycle, on-demand /optimize, and manual re-runs).
_CHECK_I_HEADER_RE = re.compile(
    r"^\*\*Check I \((\d{4}-\d{2}-\d{2})\):\*\*", re.MULTILINE
)


def append_journal(journal_path: Path, block: str) -> None:
    """Append a journal block, idempotent per Check I week marker.

    If ``block`` carries a ``**Check I (YYYY-MM-DD):**`` header and the
    journal already contains that week's header, skip the write so the same
    week never stacks duplicate blocks. Blocks without a parseable Check I
    header always append (other callers are unaffected).
    """
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    m = _CHECK_I_HEADER_RE.search(block)
    if m and journal_path.exists():
        week = m.group(1)
        existing = journal_path.read_text(encoding="utf-8")
        if week in _CHECK_I_HEADER_RE.findall(existing):
            print(
                f"[pulse-check-i] journal: skipped \u2014 block for {week} "
                "already present",
                file=sys.stderr,
            )
            return
    with open(journal_path, "a", encoding="utf-8") as f:
        f.write(block + "\n")


# --- main ---


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Pulse Check I — weekly digest.")
    p.add_argument(
        "--week-ending",
        help="ISO date (YYYY-MM-DD) — Monday of the week to digest; "
             "defaults to the current Monday.",
    )
    p.add_argument("--sidecar-dir", default=str(DEFAULT_SIDECAR_DIR))
    p.add_argument("--outbox-root", default=str(DEFAULT_OUTBOX_ROOT))
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--halt-flag", default=str(DEFAULT_HALT_FLAG))
    p.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the Mon/Wed/Fri/Sun weekday gate. Used by /optimize.",
    )
    p.add_argument(
        "--no-dm",
        action="store_true",
        help="Skip larry_alerts.append_alert (test / dry-run).",
    )
    p.add_argument(
        "--no-journal",
        action="store_true",
        help="Skip appending to cycle-journal.md (test / dry-run).",
    )
    p.add_argument(
        "--no-auto-dispatch",
        action="store_true",
        help="Skip the auto-dispatch step (test / dry-run).",
    )
    p.add_argument(
        "--dispatch-state-file",
        default=str(DEFAULT_DISPATCH_STATE_FILE),
        help="Dedup state file for auto-dispatch (closed-loop step 5).",
    )
    p.add_argument(
        "--dispatch",
        type=int,
        metavar="N",
        help="Manual dispatch: send proposal N (1-indexed) from the most "
             "recent audit JSON to Beacon's inbox. Bypasses the small-effort "
             "eligibility gate and dedup window — Larry's /dispatch is the "
             "gate.",
    )
    p.add_argument(
        "--audit",
        help="Override the audit JSON path for --dispatch. Defaults to the "
             "most-recently-modified `check-i-*.json` in --output-dir.",
    )
    args = p.parse_args(argv)

    if args.dispatch is not None:
        output_dir = Path(args.output_dir)
        if args.audit:
            audit_path = Path(args.audit)
        else:
            latest = _find_latest_audit(output_dir)
            if latest is None:
                print(f"[pulse-check-i] manual-dispatch ERROR: no audit JSON "
                      f"found in {output_dir}. Run /optimize first to "
                      f"produce one.")
                return 1
            audit_path = latest
        now = datetime.now(timezone.utc)
        return manual_dispatch_proposal(
            audit_path=audit_path,
            n=args.dispatch,
            state_path=Path(args.dispatch_state_file),
            fired_at=now,
        )

    halt_flag = Path(args.halt_flag)
    if halt_flag.exists():
        print(f"[pulse-check-i] EMERGENCY_HALT present at {halt_flag}; "
              f"skipping run.")
        return 0

    now = datetime.now(timezone.utc)
    if args.week_ending:
        week_ending_dt = datetime.fromisoformat(args.week_ending).replace(
            tzinfo=timezone.utc
        )
    else:
        week_ending_dt = _default_week_ending(now)

    # Spec § 6: fires on Mon/Wed/Fri/Sun cycles unless forced (`/optimize`).
    if (
        not args.force
        and not args.week_ending
        and now.weekday() not in CHECK_I_FIRING_WEEKDAYS
    ):
        print(f"[pulse-check-i] today is not in (Mon/Wed/Fri/Sun) "
              f"(weekday={now.weekday()}); skipping. "
              f"Use --force or /optimize for ad-hoc runs.")
        return 0

    week_ending = week_ending_dt.date().isoformat()
    sidecar_dir = Path(args.sidecar_dir)
    outbox_root = Path(args.outbox_root)
    output_dir = Path(args.output_dir)
    journal_path = Path(args.journal)

    sidecar = _load_sidecar(sidecar_dir, week_ending)
    age_hours = _sidecar_age_hours(sidecar_dir, week_ending, now=now)

    # Auto-refresh: missing or >24h old → invoke run_ledger.sh, then re-read.
    # The 7d stale-skip below still applies against the (possibly refreshed)
    # sidecar, so a refresh that fails leaves the existing logic intact.
    if sidecar is None or (age_hours is not None
                           and age_hours > SIDECAR_REFRESH_AGE_HOURS):
        _refresh_sidecar()
        sidecar = _load_sidecar(sidecar_dir, week_ending)
        age_hours = _sidecar_age_hours(sidecar_dir, week_ending, now=now)

    sidecar_filename = (
        f"weekly-{week_ending}.json" if sidecar is not None else None
    )

    # Stale check: spec § 6, sidecar > 7 days old → skip.
    if sidecar is not None:
        if age_hours is not None and age_hours > SIDECAR_MAX_AGE_DAYS * 24:
            sidecar = None
            sidecar_filename = None

    repeats = gather_retry_repeats(outbox_root)
    check_i = assemble_check_i(
        sidecar=sidecar,
        repeats=repeats,
        week_ending=week_ending,
        sidecar_filename=sidecar_filename,
        fired_at=now,
    )

    # Audit filename uses firing date so the 4 weekly firings each get
    # their own record. Sidecar lookup above still uses week_ending —
    # Ledger remains weekly Monday.
    firing_date = now.date().isoformat()
    out_path = output_dir / f"check-i-{firing_date}.json"
    _atomic_write(out_path, json.dumps(check_i, indent=2) + "\n")

    dm_body = render_dm(check_i)
    dm_result = "skipped (--no-dm)"
    # Closed-loop spec § 4: on scheduled runs with no signal, skip the DM
    # but still write audit JSON + journal entry. /optimize (--force)
    # bypasses suppression so on-demand callers always get a reply.
    suppress_dm = (
        check_i.get("mode") != "skipped"
        and not check_i.get("has_signal", True)
        and not args.force
    )
    if args.no_dm:
        pass
    elif suppress_dm:
        dm_result = "suppressed (no signal, scheduled run)"
    else:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import larry_alerts  # type: ignore
            ok = larry_alerts.append_alert(
                source="pulse",
                severity="warning",
                message=dm_body,
                subject=f"check-i-{week_ending}",
            )
            dm_result = "queued" if ok else (
                "cooldown-suppressed or write failed"
            )
        except Exception as e:  # noqa: BLE001
            dm_result = f"larry_alerts unavailable ({e})"

    if not args.no_journal:
        block = render_journal_block(check_i)
        append_journal(journal_path, block)

    # Closed-loop step 5 — auto-dispatch eligible proposals to Beacon's
    # inbox. Wrapped in a try/except defensively: even an unhandled error
    # here must not regress the digest path. Eligibility is narrow (small
    # effort + $-quantified); ineligible proposals continue to surface
    # via the digest DM for Larry to triage.
    dispatched: list[dict[str, Any]] = []
    if not args.no_auto_dispatch:
        try:
            dispatched = auto_dispatch_proposals(
                check_i=check_i,
                fired_at=now,
                state_path=Path(args.dispatch_state_file),
                sidecar=sidecar,
            )
        except Exception as e:  # noqa: BLE001 — Check I must never crash
            print(f"[pulse-check-i] WARN: auto-dispatch step crashed "
                  f"({type(e).__name__}: {e}); continuing")

    print(f"[pulse-check-i] mode={check_i['mode']}")
    print(f"[pulse-check-i] wrote {out_path}")
    print(f"[pulse-check-i] DM: {dm_result}")
    if not args.no_journal:
        print(f"[pulse-check-i] journal: appended to {journal_path}")
    print(f"[pulse-check-i] auto-dispatched: {len(dispatched)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
