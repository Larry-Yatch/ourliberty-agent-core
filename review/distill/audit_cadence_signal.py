#!/usr/bin/env python3
"""Phase-2 audit-cadence SIGNAL — the durable, self-firing capture for the ONE deferred
decision: should full-codebase audits run on a SCHEDULE (the deferred "automate recurring
audits" work), or stay on-demand?

SELF-GATING and FAIL-OPEN (modeled verbatim on assess_gate.py): safe to invoke every Pulse
cycle. It no-ops until Phase-2 has distilled its first REAL (post-seed) audit, then fires
EXACTLY ONCE — a Telegram DM carrying the convergence reading + a data-driven cadence
recommendation + the explicit desktop next-step, AND drops a durable card into the Missions
Parked lane (emit_capture.sh) so the decision can't be lost even if the DM is. Writes a
sentinel, no-ops forever after. NEVER raises.

"When is it time?" is answered by Phase-2's own CONVERGENCE metric (novel-class share per
distilled audit): a high novel-share across real audits ⇒ the corpus is still learning ⇒
scheduling recurring audits pays off; a low (converged) share ⇒ on-demand audits suffice.

Spec: memory mirror-bughunt-gate-project + review/gate-soak-assessment.md.
"""
import os, sys, json, glob, pathlib, subprocess
from datetime import datetime, timezone

# this one-shot lives under review/distill/, so put scripts/ on the path for the house
# helpers (atomic_io, larry_alerts) it shares with the scripts/-resident checks.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'scripts'))

AGENTS_ROOT = pathlib.Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', os.path.expanduser('~/agents')))
PROPOSAL_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-distill-proposals'
SENTINEL = AGENTS_ROOT / 'state' / 'audit-cadence-signal.json'
EMIT_CAPTURE = pathlib.Path(os.path.expanduser('~/.config/ourliberty/emit_capture.sh'))
SEED_AUDIT = 'AUDIT_main_20260605.md'   # exclude by IDENTITY — re-distilling it gives 0% novel
MIN_FINDINGS = 15                       # denominator guard: a tiny audit isn't decision-grade
LEARNING_THRESHOLD = 0.25               # novel-share above this ⇒ "corpus still learning"


def _parse_share(d):
    try:
        return float(d.get('convergence', {}).get('novel_share'))
    except Exception:
        return None


def _post_seed_artifacts():
    """Distill artifacts for real, decision-grade post-seed audits (sorted by as_of)."""
    out = []
    for f in glob.glob(str(PROPOSAL_DIR / '*.json')):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if d.get('check') != 'distill':
            continue
        if d.get('source_audit') == SEED_AUDIT:
            continue
        if (d.get('audit_findings_total') or 0) < MIN_FINDINGS:
            continue
        if _parse_share(d) is None:
            continue
        out.append(d)
    out.sort(key=lambda d: d.get('as_of', ''))
    return out


def _already_fired():
    try:
        return SENTINEL.exists() and json.loads(SENTINEL.read_text()).get('fired_at')
    except Exception:
        return False


def _mark_fired(payload):
    try:
        import atomic_io
        SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.atomic_write_json(SENTINEL, {'fired_at': datetime.now(timezone.utc).isoformat(), **payload})
        return True
    except Exception as e:
        print(f"[audit-cadence] WARN: sentinel write failed ({e}); deferring DM")
        return False


def _dm(body):
    try:
        import larry_alerts
        return larry_alerts.append_alert(source='pulse', severity='warning',
                                         message=body, subject='audit-cadence-decision')
    except Exception as e:
        print(f"[audit-cadence] WARN: DM send failed ({e}); body follows:\n{body}")
        return False


def _capture(title, note):
    # durable backstop: a Missions Parked-lane card that survives a missed DM.
    try:
        if EMIT_CAPTURE.exists():
            subprocess.run(['bash', str(EMIT_CAPTURE), title, note],
                           capture_output=True, text=True, timeout=30)
            return True
    except Exception as e:
        print(f"[audit-cadence] WARN: emit_capture failed ({e})")
    return False


def main(argv=None):
    if _already_fired():
        print('[audit-cadence] already fired; no-op.')
        return 0
    arts = _post_seed_artifacts()
    if not arts:
        print('[audit-cadence] no post-seed decision-grade distill artifacts yet; no-op.')
        return 0

    shares = [(_parse_share(d), d.get('source_audit', '?'), d.get('audit_findings_total')) for d in arts]
    latest = shares[-1][0]
    avg = sum(s for s, _, _ in shares) / len(shares)
    provisional = len(arts) < 2
    learning = avg >= LEARNING_THRESHOLD
    rec = ("SCHEDULE recurring audits — the corpus is still learning new bug classes, so "
           "regular audits keep teaching the gate."
           if learning else
           "Keep audits ON-DEMAND — the corpus is converging (few new classes per audit), "
           "so scheduled sweeps would mostly re-confirm known classes.")
    trend = '; '.join(f"{src.replace('AUDIT_main_','').replace('.md','')}={sh:.0%} ({n})"
                      for sh, src, n in shares)

    body = (
        "AUDIT-CADENCE DECISION (deferred Phase-2 next step — action needed)\n"
        "\n"
        f"Phase-2 has now distilled {len(arts)} real audit(s) since the seed. The "
        "convergence metric is the signal for whether to automate recurring full-codebase "
        "audits (the piece we deferred when building the distill loop).\n"
        "\n"
        "CONVERGENCE (novel-class share per audit):\n"
        f"  • {trend}\n"
        f"  • avg novel-share {avg:.0%}{' (PROVISIONAL — 1 audit; confirm after the next)' if provisional else ''}\n"
        "\n"
        f"RECOMMENDATION: {rec}\n"
        "\n"
        "NEXT STEP (desktop dev work, not Beacon):\n"
        "  Open Claude Code in ourliberty-agent-core and decide audit cadence. If you choose\n"
        "  to schedule, that IS the deferred 'automate recurring audits' build.\n"
        "  Context: memory mirror-bughunt-gate-project."
    )

    # sentinel FIRST (so a state-dir failure DEFERS rather than re-DMs), then DM + capture.
    if not _mark_fired({'post_seed_audits': len(arts), 'avg_novel_share': round(avg, 3),
                        'latest_novel_share': latest, 'recommendation_learning': learning}):
        print('[audit-cadence] sentinel not written; deferring to next cycle (no DM).')
        return 0
    queued = _dm(body)
    captured = _capture('Decide full-codebase-audit cadence',
                        f"Phase-2 convergence avg novel-share {avg:.0%} over {len(arts)} audit(s). "
                        f"{rec} See memory mirror-bughunt-gate-project.")
    print(f"[audit-cadence] FIRED ({len(arts)} post-seed audits, avg novel-share {avg:.0%}, "
          f"dm_queued={queued}, captured={captured}).")
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:   # fail-open: never disrupt a Pulse cycle
        print(f"[audit-cadence] non-fatal error: {e}")
        raise SystemExit(0)
