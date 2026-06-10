#!/usr/bin/env python3
"""Audit-due nudge — a self-gating Pulse one-shot that tells Larry when enough has
changed to be worth running the next full-codebase audit (which then feeds Phase-2 of the
bug-hunt gate: distill → corpus).

WHY (volume, not calendar): a full-codebase audit is the expensive multi-agent sweep that
produced AUDIT_main_<date>.md. Re-running it on a blind clock re-audits unchanged code; a
CHANGE-based trigger ("enough new code merged since the last audit") fires only when an
audit would actually find something — same shape as the bug-hunt soak trigger (N reviews,
not a date). It NUDGES; Larry gates the spend (he decides whether to actually run it).

SELF-GATING + FAIL-OPEN (modeled on assess_gate.py): safe to invoke every cycle. No-ops
until churn since the latest audit crosses a threshold, then DMs Larry ONCE per audit
anchor (re-arms automatically when a new AUDIT_main_*.md lands → the next audit cycle).
Never raises; no heartbeat (the staleness watcher does not track it).

Spec: memory mirror-bughunt-gate-project (the deferred audit-cadence decision). Companions
in cycle-prompt §5.0b: distill_detector.py (audit landed → distill it) and
audit_cadence_signal.py (after distillation → schedule-vs-on-demand decision).
"""
import os, re, glob, json, subprocess, pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENTS_ROOT = pathlib.Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', os.path.expanduser('~/agents')))
SENTINEL = AGENTS_ROOT / 'state' / 'audit-due-nudge.json'
AUDIT_GLOB = str(ROOT / 'AUDIT_main_*.md')
AUDIT_DATE = re.compile(r'AUDIT_main_(\d{8})\.md$')
# Audit docs are UNTRACKED on-disk artifacts (see review/backtest/README.md), so we anchor
# on the filename DATE (not a commit) and measure churn via `git log --since=<that date>`.
# Pulse runs this in the droplet's primary checkout where the audit file is present on disk.
# Churn scope = NON-TEST scripts/ (new production code is the bug surface; new tests aren't).
CHURN_PATHSPEC = ['--', 'scripts/', ':(exclude)scripts/tests/']
# Tunable knobs — DM reports the live numbers so these calibrate after the first fire. Fire
# when EITHER threshold is crossed since the last audit. Sized to this repo's measured
# velocity (~16 PRs/day, ~2.5k non-test LOC/day as of 2026-06-10): ~60 PRs / 12k LOC ≈ the
# change accumulated since the seed audit, i.e. "a meaningful audit-worth of new code."
PR_THRESHOLD = 60            # merged PRs (squash commits w/ "(#N)") since the last audit
LOC_THRESHOLD = 12000        # non-test scripts/ insertions+deletions since the last audit


def _git(*a):
    return subprocess.run(['git', *a], cwd=str(ROOT), capture_output=True, text=True).stdout


def _latest_audit():
    """Newest dated AUDIT_main_<YYYYMMDD>.md by filename date (ignores remediation_plan
    and any non-dated AUDIT_main_*). Returns (basename, 'YYYY-MM-DD') or (None, None)."""
    best = (None, None)
    for p in glob.glob(AUDIT_GLOB):
        m = AUDIT_DATE.search(os.path.basename(p))
        if m and (best[1] is None or m.group(1) > best[1]):
            best = (os.path.basename(p), m.group(1))
    if best[0] is None:
        return (None, None)
    d = best[1]
    return (best[0], f'{d[:4]}-{d[4:6]}-{d[6:]}')


def gather_signals():
    """Return {audit, date, pr_count, churn_loc} or None if no audit doc is present.
    Churn is measured since the audit's filename date (audits are untracked → no commit
    anchor). The audit FILENAME is the re-arm identity (a new audit ⇒ a new anchor)."""
    audit, date = _latest_audit()
    if not audit:
        return None
    since = f'{date} 00:00 +0000'   # pin to UTC midnight; don't drift with the host TZ
    subjects = _git('log', f'--since={since}', '--format=%s')
    pr_count = len(re.findall(r'\(#\d+\)', subjects))
    numstat = _git('log', f'--since={since}', '--numstat', '--format=', *CHURN_PATHSPEC)
    loc = 0
    for line in numstat.splitlines():
        cols = line.split('\t')
        if len(cols) >= 2 and cols[0].isdigit() and cols[1].isdigit():
            loc += int(cols[0]) + int(cols[1])
    return {'audit': audit, 'date': date, 'pr_count': pr_count, 'churn_loc': loc}


def evaluate(sig):
    """Pure: is an audit due, and why?"""
    reasons = []
    if sig['pr_count'] >= PR_THRESHOLD:
        reasons.append(f"{sig['pr_count']} PRs merged since {sig['audit']} (≥{PR_THRESHOLD})")
    if sig['churn_loc'] >= LOC_THRESHOLD:
        reasons.append(f"{sig['churn_loc']} LOC changed in non-test scripts/ (≥{LOC_THRESHOLD})")
    return {'due': bool(reasons), 'reasons': reasons}


def _last_nagged_audit():
    """The audit filename we last nagged for (the re-arm identity). A newer audit ⇒ a
    different name ⇒ the nudge re-arms for the next cycle."""
    try:
        return json.loads(SENTINEL.read_text()).get('audit') if SENTINEL.exists() else None
    except Exception:
        return None


def _mark(sig, verdict):
    try:
        import atomic_io
        SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.atomic_write_json(SENTINEL, {
            'audit': sig['audit'],
            'nagged_at': datetime.now(timezone.utc).isoformat(),
            'pr_count': sig['pr_count'], 'churn_loc': sig['churn_loc'],
            'reasons': verdict['reasons']})
        return True
    except Exception as e:
        print(f"[audit-due] WARN: sentinel write failed ({e}); deferring DM")
        return False


def _dm(body):
    try:
        import larry_alerts
        return larry_alerts.append_alert(source='pulse', severity='warning',
                                         message=body, subject='audit-due-nudge')
    except Exception as e:
        print(f"[audit-due] WARN: DM send failed ({e}); body follows:\n{body}")
        return False


def main(argv=None):
    sig = gather_signals()
    if sig is None:
        print('[audit-due] no committed audit baseline; no-op.')
        return 0
    verdict = evaluate(sig)
    if not verdict['due']:
        print(f"[audit-due] not due (PRs {sig['pr_count']}/{PR_THRESHOLD}, "
              f"LOC {sig['churn_loc']}/{LOC_THRESHOLD} since {sig['audit']}); no-op.")
        return 0
    if _last_nagged_audit() == sig['audit']:
        print(f"[audit-due] already nagged for {sig['audit']}; "
              "no-op until a newer audit lands.")
        return 0

    body = (
        "AUDIT DUE — enough has changed to feed Phase-2 (your call to run it)\n"
        "\n"
        f"Since the last full-codebase audit ({sig['audit']}):\n"
        + ''.join(f"  • {r}\n" for r in verdict['reasons']) +
        "\n"
        "A fresh full-codebase audit (the multi-agent sweep) gives Phase-2 new findings to\n"
        "distill into the bug-hunt corpus. This is a NUDGE — you decide whether the spend is\n"
        "worth it now.\n"
        "\n"
        "If you run one: drop the report as AUDIT_main_<date>.md → distill_detector DMs you to\n"
        "run review/distill/run_distill.py → audit_cadence_signal then fires the\n"
        "schedule-vs-on-demand cadence decision with real convergence data.\n"
        "\n"
        "(One nudge per audit; re-arms when the next audit lands. Thresholds tunable in\n"
        "scripts/audit_due_nudge.py.) Context: memory mirror-bughunt-gate-project."
    )
    # DM-FIRST (deliberate divergence from the sentinel-first one-shots): this is a NUDGE
    # whose whole job is delivery, so we record the anchor only once the DM actually goes
    # out. A cooldown/transient suppression then retries next cycle instead of silently
    # dropping the nudge until the next audit lands. (A durably-silenced subject just
    # retries a cheap no-op each cycle.) On the rare sentinel-write failure after a send we
    # re-DM once next cycle — an acceptable trade for a nudge.
    if not _dm(body):
        print(f"[audit-due] due ({sig['audit']}) but DM not delivered (cooldown/silence); "
              "retry next cycle, sentinel unwritten.")
        return 0
    if not _mark(sig, verdict):
        print('[audit-due] DM sent but sentinel write failed; may re-DM next cycle.')
        return 0
    print(f"[audit-due] FIRED for {sig['audit']} "
          f"(PRs={sig['pr_count']}, LOC={sig['churn_loc']}).")
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:   # fail-open: never disrupt a Pulse cycle
        print(f"[audit-due] non-fatal error: {e}")
        raise SystemExit(0)
