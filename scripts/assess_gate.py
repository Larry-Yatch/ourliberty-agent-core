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
GO_LIVE_TS = '2026-06-08T21:26:53Z'   # PR #398 merge (gate live shortly after droplet sync)
N_REVIEWS = 15                         # min gate reviews before we assess (volume gate)
BASELINE_FIRST_PASS = 0.77             # pre-gate first-pass-PASS rate (302-review backtest)

AGENTS_ROOT = pathlib.Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', os.path.expanduser('~/agents')))
ARCHIVE_GLOB = str(AGENTS_ROOT / 'outboxes' / 'mirror' / '.archive' / '*.json')
SENTINEL = AGENTS_ROOT / 'state' / 'gate-soak-assessment.json'
VER = re.compile(r'===\s*(REVIEW_PASS|REVIEW_REVISION|REVIEW_ESCALATE|REVIEW_EMERGENCY_HALT)\s*===')


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None


def _collect():
    """Return per-task final review records since go-live."""
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
        rnd = d.get('revision_count') if isinstance(d.get('revision_count'), int) else 0
        rec = {'verdict': ms[-1], 'round': rnd, 'result': res, 'pr': d.get('pr_url', '')}
        task = d.get('task_id') or f
        # keep the highest-round record per task as its final outcome
        if task not in by_task or rnd >= by_task[task]['round']:
            by_task[task] = rec
    return by_task


def _bughunt_findings(records):
    """REVISION findings that are NOT the test-regression gate (i.e. spec/quality/bug-hunt).
    Returns (count, sample_descriptions)."""
    out = []
    for r in records.values():
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
            if desc.lower().startswith('regression gate'):
                continue   # pre-existing test-regression gate, not the bug-hunt
            out.append(desc)
    return len(out), out[:3]


def _already_fired():
    try:
        return SENTINEL.exists() and json.loads(SENTINEL.read_text()).get('fired_at')
    except Exception:
        return False


def _mark_fired(payload):
    try:
        SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        tmp = SENTINEL.with_suffix('.tmp')
        tmp.write_text(json.dumps({'fired_at': datetime.now(timezone.utc).isoformat(), **payload}, indent=2))
        os.replace(tmp, SENTINEL)
    except OSError as e:
        print(f"[assess-gate] WARN: sentinel write failed ({e})")


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
    records = _collect()
    n = len(records)
    if n < N_REVIEWS:
        print(f'[assess-gate] {n}/{N_REVIEWS} gate reviews since go-live; soaking, no-op.')
        return 0

    first_try_pass = sum(1 for r in records.values() if r['round'] <= 0 and r['verdict'] == 'REVIEW_PASS')
    fpr = first_try_pass / n if n else 0.0
    n_revision = sum(1 for r in records.values() if r['verdict'] == 'REVIEW_REVISION')
    n_escalate = sum(1 for r in records.values() if r['verdict'] == 'REVIEW_ESCALATE')
    n_halt = sum(1 for r in records.values() if r['verdict'] == 'REVIEW_EMERGENCY_HALT')
    bh_count, bh_samples = _bughunt_findings(records)

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
    queued = _dm(body)
    _mark_fired({'reviews': n, 'first_pass_rate': round(fpr, 3), 'bughunt_findings': bh_count,
                 'dm_queued': bool(queued)})
    print(f'[assess-gate] FIRED at {n} reviews (fpr={fpr:.0%}, dm_queued={queued}).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
