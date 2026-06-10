#!/usr/bin/env python3
"""Phase-2 distill DETECTOR — a self-gating one-shot, invoked every Pulse cycle.

SELF-GATING and FAIL-OPEN (modeled on assess_gate.py): safe to invoke every cycle. It
no-ops unless a full-codebase audit doc (AUDIT_main_*.md) has landed in the repo that has
NOT yet been distilled, then fires EXACTLY ONE Telegram DM telling Larry to run the
distiller on his desktop, records that it nagged (so it never re-nags that audit), and
no-ops again. It NEVER runs an LLM and NEVER raises.

Why a DM, not auto-run: distillation is a heavy local `claude -p` job that must run on the
desktop (NOT the rate-limited droplet auth) — see review/distill/run_distill.py. The
detector's whole job is to make sure a landed audit can't be silently missed.

Spec: review/gate-soak-assessment.md sibling + memory mirror-bughunt-gate-project.
"""
import os, re, json, glob, pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENTS_ROOT = pathlib.Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', os.path.expanduser('~/agents')))
PROPOSAL_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-distill-proposals'
NAG_SENTINEL = AGENTS_ROOT / 'state' / 'distill-detector-nags.json'
AUDIT_GLOB = str(ROOT / 'AUDIT_main_*.md')
# The seed audit already produced the corpus — never nag about distilling it.
SEED_AUDIT = 'AUDIT_main_20260605.md'
AUDIT_DATE = re.compile(r'AUDIT_main_(\d{8})\.md$')


def _audit_date(name):
    m = AUDIT_DATE.search(name)
    return m.group(1) if m else None


def _already_distilled(date):
    # a proposal artifact for this audit means the distiller already ran.
    return date is not None and (PROPOSAL_DIR / f'distill-{date}.json').exists()


def _load_nags():
    try:
        return json.loads(NAG_SENTINEL.read_text()) if NAG_SENTINEL.exists() else {}
    except Exception:
        return {}


def _record_nag(nags, name):
    try:
        import atomic_io
        NAG_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        nags[name] = datetime.now(timezone.utc).isoformat()
        atomic_io.atomic_write_json(NAG_SENTINEL, nags)
        return True
    except Exception as e:
        print(f"[distill-detector] WARN: nag sentinel write failed ({e}); deferring DM")
        return False


def _dm(body):
    try:
        import larry_alerts
        return larry_alerts.append_alert(source='pulse', severity='warning',
                                         message=body, subject='distill-audit-landed')
    except Exception as e:
        print(f"[distill-detector] WARN: DM send failed ({e}); body follows:\n{body}")
        return False


def main(argv=None):
    nags = _load_nags()
    for path in sorted(glob.glob(AUDIT_GLOB)):
        name = os.path.basename(path)
        if name == SEED_AUDIT:
            continue
        date = _audit_date(name)
        if date is None:
            continue   # not a dated audit (e.g. AUDIT_main_*_remediation_plan.md) — ignore
        if _already_distilled(date) or name in nags:
            continue
        # an un-distilled, un-nagged audit — fire exactly one DM.
        body = (
            "PHASE-2 DISTILL — a full-codebase audit landed, not yet distilled.\n"
            "\n"
            f"Audit: {name}\n"
            "\n"
            "The bug-hunt gate's Phase-2 loop teaches the corpus from audit findings. To\n"
            "run it (desktop dev work — uses local claude, NOT the droplet auth):\n"
            "\n"
            "  1. Extract findings:  python3 review/backtest/extract_findings.py "
            f"{name}\n"
            "  2. Distill:           python3 review/distill/run_distill.py "
            "review/backtest/findings.json \\\n"
            f"                          --source-audit {name}\n"
            "  3. Review the proposal artifact, then 'approve distill-update-<date>'.\n"
            "\n"
            "Context: memory mirror-bughunt-gate-project."
        )
        if not _record_nag(nags, name):
            print('[distill-detector] sentinel not written; deferring DM to next cycle.')
            return 0
        queued = _dm(body)
        print(f"[distill-detector] FIRED for {name} (dm_queued={queued}).")
        return 0
    print('[distill-detector] no un-distilled audits; no-op.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:   # fail-open: never disrupt a Pulse cycle
        print(f"[distill-detector] non-fatal error: {e}")
        raise SystemExit(0)
