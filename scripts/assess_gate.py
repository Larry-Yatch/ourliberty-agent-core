#!/usr/bin/env python3
"""One-shot soak assessment for the Mirror bug-hunt gate (shipped PR #398, 2026-06-08).

SELF-GATING: safe to invoke every Pulse cycle. It no-ops until the gate has reviewed
N PRs since go-live, then fires EXACTLY ONE Telegram DM to Larry with the assessment +
the explicit next step, writes a sentinel, and no-ops forever after. It NEVER raises
(fail-open) so it can't disrupt a Pulse cycle.

Spec: review/gate-soak-assessment.md. Decision gate: keep / dial-back / greenlight the
Phase-2 self-tuning loop. The DM only REPORTS — the Phase-2 build is done in a desktop
Claude Code session, not by Beacon. After the Phase-2 decision, delete this script and
its cycle-prompt invocation.
"""
import os, re, json, glob, pathlib
from datetime import datetime, timezone

# --- knobs ---
GO_LIVE_TS = '2026-06-08T21:26:53Z'   # PR #398 merge (gate live shortly after droplet sync).
# Bounded failure mode: an off-by-hours value only shifts which archives count; it cannot
# cause a double-fire (the sentinel guards that). Nudge later if the droplet synced much later.
N_REVIEWS = 15                         # min gate reviews before we assess (volume gate)
BASELINE_FIRST_PASS = 0.77             # pre-gate first-pass-PASS rate (302-review backtest)

AGENTS_ROOT = pathlib.Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', os.path.expanduser('~/agents')))
ARCHIVE_GLOB = str(AGENTS_ROOT / 'outboxes' / 'mirror' / '.archive' / '*.json')
SENTINEL = AGENTS_ROOT / 'state' / 'gate-soak-assessment.json'
VER = re.compile(r'===\s*(REVIEW_PASS|REVIEW_REVISION|REVIEW_ESCALATE|REVIEW_EMERGENCY_HALT)\s*===')


def _parse_ts(s):
    # ALWAYS return tz-aware (or None). A legacy/foreign archive with a naive
    # completed_at would otherwise make `ts < cutoff` raise TypeError (naive vs
    # aware) OUTSIDE the try — crashing every cycle and never firing the DM.
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _collect():
    """Return {task_id: [records...]} for all review outboxes since go-live.
    Each record: {verdict, ts, result, pr}. We do NOT use revision_count to pick a
    task's final verdict — it RESETS to 0 on every Beacon replan iteration
    (outbox_notifier hardcodes revision_count:0 per replan), so a stale pre-replan
    REVISION would outrank the real post-replan PASS. Order by completed_at instead."""
    cutoff = _parse_ts(GO_LIVE_TS)
    by_task = {}
    for f in glob.glob(ARCHIVE_GLOB):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if d.get('phase') != 'review':
            continue
        ts = _parse_ts(d.get('completed_at'))
        if not ts or not cutoff or ts < cutoff:
            continue
        res = d.get('result') or ''
        if not isinstance(res, str):
            continue
        ms = VER.findall(res)
        if not ms:
            continue
        task = d.get('task_id')
        if not task:
            continue   # can't dedup a review without its task_id; skip rather than key on path (would over-count)
        by_task.setdefault(task, []).append(
            {'verdict': ms[-1], 'ts': ts, 'result': res, 'pr': d.get('pr_url', '')})
    return by_task


def _bughunt_findings(by_task):
    """REVISION findings across ALL review records that are NOT the test-regression
    gate (i.e. spec/quality/bug-hunt). Returns (count, sample_descriptions). The
    regression-gate split is a heuristic on Mirror's free-text 'regression gate'
    phrasing — substring match (not startswith) to tolerate prefixes; the DM labels
    this metric as approximate."""
    out = []
    for recs in by_task.values():
        for r in recs:
            if r['verdict'] != 'REVIEW_REVISION':
                continue
            m = re.search(r'===\s*REVIEW_REVISION\s*===(.*?)===\s*END_REVIEW_REVISION\s*===', r['result'], re.S)
            if not m:
                continue
            try:
                j = json.loads(m.group(1).strip())
            except Exception:
                continue
            for fn in (j.get('findings') or []):
                desc = str(fn.get('description', ''))
                if 'regression gate' in desc.lower():
                    continue   # the pre-existing test-regression gate, not the bug-hunt
                out.append(desc)
    return len(out), out[:3]


def _already_fired():
    try:
        return SENTINEL.exists() and json.loads(SENTINEL.read_text()).get('fired_at')
    except Exception:
        return False


def _mark_fired(payload):
    try:
        import atomic_io  # house standard (durable tmp+fsync+replace, unique tmp name)
        SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.atomic_write_json(SENTINEL, {'fired_at': datetime.now(timezone.utc).isoformat(), **payload})
        return True
    except Exception as e:
        print(f"[assess-gate] WARN: sentinel write failed ({e}); deferring DM to avoid repeats")
        return False


def _dm(body):
    try:
        import larry_alerts
        return larry_alerts.append_alert(source='pulse', severity='warning',
                                         message=body, subject='bughunt-gate-soak')
    except Exception as e:
        print(f"[assess-gate] WARN: DM send failed ({e}); body follows:\n{body}")
        return False


def main(argv=None):
    if _already_fired():
        print('[assess-gate] already fired; no-op.')
        return 0
    by_task = _collect()
    n = len(by_task)   # unique PRs reviewed since go-live
    if n < N_REVIEWS:
        print(f'[assess-gate] {n}/{N_REVIEWS} gate reviews since go-live; soaking, no-op.')
        return 0

    # final verdict per task = latest review by completed_at (replan-safe).
    # latest review by completed_at; on an exact-ts tie prefer PASS (a resolved state)
    # so the tie-break is deterministic rather than glob/OS-order dependent.
    finals = {t: max(recs, key=lambda r: (r['ts'], r['verdict'] == 'REVIEW_PASS'))
              for t, recs in by_task.items()}
    # first-pass-PASS = a task with exactly ONE review record that ended PASS (no
    # revisions/replans). revision_count-independent, so replan resets can't inflate it.
    first_try_pass = sum(1 for recs in by_task.values()
                         if len(recs) == 1 and recs[0]['verdict'] == 'REVIEW_PASS')
    fpr = first_try_pass / n if n else 0.0
    n_revision = sum(1 for r in finals.values() if r['verdict'] == 'REVIEW_REVISION')
    n_escalate = sum(1 for r in finals.values() if r['verdict'] == 'REVIEW_ESCALATE')
    n_halt = sum(1 for r in finals.values() if r['verdict'] == 'REVIEW_EMERGENCY_HALT')
    bh_count, bh_samples = _bughunt_findings(by_task)

    delta = fpr - BASELINE_FIRST_PASS
    health = 'intact' if delta > -0.10 else 'DROPPED — possible over-blocking'
    samples = '\n'.join(f'   • {s[:130]}' for s in bh_samples) or '   (none)'

    body = (
        "BUG-HUNT GATE — SOAK ASSESSMENT (action needed)\n"
        "\n"
        f"The Mirror bug-hunt gate (shipped PR #398) has now reviewed {n} PRs since go-live.\n"
        "This is the checkpoint we set: assess whether it's working, then decide on Phase 2.\n"
        "\n"
        "NUMBERS:\n"
        f"  • First-pass-PASS rate: {fpr:.0%} (pre-gate baseline 77%) → loop health: {health}\n"
        "    (approximate — a Beacon replan under a new task_id can shift this slightly)\n"
        f"  • Verdicts: {n_revision} revision, {n_escalate} escalate, {n_halt} emergency-halt\n"
        f"  • Non-regression-gate revision findings (spec + bug-hunt): {bh_count}\n"
        "  • Sample findings (eyeball for false positives):\n"
        f"{samples}\n"
        "\n"
        "WHAT THIS MEANS: if the first-pass rate held near 77% and the sample findings look\n"
        "like real bugs (not noise), the gate is working → ready for Phase 2 (the Pulse\n"
        "audit→distill→corpus self-tuning loop). If the rate dropped a lot or the findings\n"
        "look noisy, dial back the thresholds first.\n"
        "\n"
        "NEXT STEP (you do this — it's desktop dev work, not Beacon):\n"
        "  Open Claude Code on your desktop in ourliberty-agent-core and say either\n"
        "  'proceed with the bug-hunt gate Phase 2' or 'dial back the gate thresholds'.\n"
        "  Full context: memory mirror-bughunt-gate-project + review/gate-soak-assessment.md.\n"
        "  (Deeper false-positive review happens in that session against the live data.)"
    )
    # Write the sentinel FIRST; only DM if it landed. A persistent state-dir write
    # failure then DEFERS the DM to a later cycle (retry) rather than re-sending it
    # every cycle. _dm() prints the body to stdout on send failure, so it's recoverable.
    if not _mark_fired({'reviews': n, 'first_pass_rate': round(fpr, 3), 'bughunt_findings': bh_count}):
        print('[assess-gate] sentinel not written; deferring to next cycle (no DM this run).')
        return 0
    queued = _dm(body)
    print(f'[assess-gate] FIRED at {n} reviews (fpr={fpr:.0%}, dm_queued={queued}).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
