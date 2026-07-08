#!/usr/bin/env python3
"""pulse_check_xiv.py — weekly alert-precision meter (V1: measure + surface).

Spec: agents/beacon/specs/pulse-check-xiv.md § 7. Addresses hole #1 of the
2026-07-07 pulse audit: fleet-wide alert precision / toil.

V1 is a MEASUREMENT + SAFETY instrument that changes NO config. It reads the
raw alert log ALONE and RE-DERIVES each alert's tier/decision by re-running the
pure classifier `alert_triage_state.classify()` per line with the live registry
+ translations — the join to `alert-triage.json` is structurally impossible
(alert_id is a rotating line number; the triage row carries no source/subject),
so v2 of the spec reconstructs the decision from clean keys instead.

What V1 does NOT do (deliberately sequenced behind the substrate that makes it
safe — XIV-b/XIV-c):
  - It NEVER writes `config/alert-translations.json` (no auto-silence).
  - It emits NO `approve check-xiv-update-<date>` shortcut (no config-landing).
  - It proposes NO route demotions.
Those are XIV-c, gated behind XIV-b (the tier-4 write-back loop). V1 reports and
surfaces only.

Inputs (read-only, no LLM calls):
  - `~/agents/blackboard/larry-alerts.jsonl` — one JSON object per line
    (`{ts, source, severity, message, route, subject, [template], [intent],
    [kind], ...}`). The only source. Everything `classify()` reads.
  - Live `registry` + `translations` via `alert_triage_state.load_registry()` /
    `load_translations()` — the same blessed loaders the Check-0 triage CLI uses.

Metrics (per source and per (source, signature), over the trailing window):
  - volume, silence_rate (tier-3/total), ask_rate (tier-4/total),
    dispatch_rate (tier-1+2/total), recurrence (count ÷ distinct signatures),
    novelty (share of a source's tier-4 that is novel-fallthrough).
  - noise_candidate_share (fleet) = tier-3-silenced + recurring-novel-tier-4.
    Reported ONLY; it CANNOT distinguish ignored-noise from unfixed-real
    (no action-rate substrate until XIV-b) — never an auto-action trigger.

Emissions (§ 4):
  1. Precision report → artifact always; digest DM only on the first Monday of
     the month OR when the over-silence surface trips (alert-toil applies to
     XIV itself).
  2. Over-silence safety surface → a warning DM per source at ~100% silence AND
     high volume (the one genuinely Larry-worthy signal: guard against an
     over-suppressed real signal; park-don't-decay applied to the allowlist).
  3. Nothing else auto-fires.

Partial-data contract (§ 5): the log read is try/except'd; the artifact is
always written with a `sources` status block; a dark/unreadable log is a 0-exit
with `sources.log == 'error'`, escalating only after 2 consecutive dark runs.
Only an artifact-write failure is non-zero.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
REPO_ROOT = Path(__file__).resolve().parent.parent
ALERTS_FILE = AGENTS_ROOT / 'blackboard' / 'larry-alerts.jsonl'
ARTIFACT_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-check-xiv'
DARK_STATE_FILE = ARTIFACT_DIR / 'dark-run-state.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-xiv.log'

WINDOW_DAYS = 14                     # trailing analysis window
RECURRING_NOVEL_MIN_COUNT = 3        # a signature is "recurring" at >= 3 hits
TOP_CANDIDATES_CAP = 10              # digest names at most 10; rest -> artifact
RETENTION_DAYS = 182                 # ~26 weeks self-pruned
SAMPLE_MESSAGES_CAP = 3              # sample bodies per candidate signature
DARK_ESCALATE_AFTER = 2              # consecutive dark runs before a DM

# Over-silence thresholds (re-measured from the live distribution at build; see
# the spec § 7.5 baseline note). A signature at near-total silence AND high
# volume is the over-suppression risk worth surfacing.
OVER_SILENCE_MIN_VOLUME = 50
OVER_SILENCE_SILENCE_RATE = 0.95

# Signature normalization — ORDER IS LOAD-BEARING (§ 3): UUID and SHA collapse
# BEFORE the digit pass, else `\d+` mangles the hex so the SHA regex can no
# longer match. Lowercase + whitespace-collapse come last.
_UUID_RE = re.compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I)
_SHA_RE = re.compile(r'\b[0-9a-f]{7,40}\b')
_DIGITS_RE = re.compile(r'\d+')
_WS_RE = re.compile(r'\s+')

# The exact rationale `classify()` returns for a Gate-4 novel fallthrough (no
# registry template AND no translation match) — a missing template/allowlist by
# definition. Matched verbatim so `novelty` counts only true fallthroughs.
NOVEL_FALLTHROUGH_RATIONALE = 'novel: no registry template and no translation match'

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from atomic_io import atomic_write_json  # noqa: E402


def log(msg: str, level: str = 'INFO') -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f'[{ts}] [{level}] {msg}'
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


# -------------------- signature normalization --------------------


def normalize_signature(subject: Optional[str]) -> str:
    """Collapse an alert subject to its normalized identity (§ 3).

    Order is load-bearing: UUID -> `@`, SHA -> `@`, digits -> `#`, lowercase,
    collapse whitespace. Running the digit pass before the SHA pass would rewrite
    the numeric characters of a hex digest so the SHA regex could not match it.
    """
    s = subject or ''
    s = _UUID_RE.sub('@', s)
    s = _SHA_RE.sub('@', s)
    s = _DIGITS_RE.sub('#', s)
    s = s.lower()
    s = _WS_RE.sub(' ', s).strip()
    return s


# -------------------- record load + classification --------------------


@dataclass
class AlertRecord:
    ts: datetime
    source: str
    subject: Optional[str]
    message: str
    raw: dict[str, Any]


@dataclass
class ClassifiedRecord:
    source: str
    signature: str
    subject: Optional[str]
    message: str
    tier: int
    rationale: str

    @property
    def is_novel(self) -> bool:
        return self.tier == 4 and self.rationale == NOVEL_FALLTHROUGH_RATIONALE


def _parse_ts(ts_str: Any) -> Optional[datetime]:
    if not isinstance(ts_str, str) or not ts_str:
        return None
    s = ts_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_records(
    alerts_path: Path = ALERTS_FILE,
    *,
    now: Optional[datetime] = None,
    window_days: int = WINDOW_DAYS,
) -> tuple[list[AlertRecord], str]:
    """Read the raw alert log; return (records, status).

    status is one of:
      - 'ok'    — read succeeded, at least one in-window record.
      - 'empty' — read succeeded, no in-window records (a clean quiet window).
      - 'error' — the log is missing or unreadable (a DARK run; § 5 contract).

    Only 'error' is dark. A missing file is dark, not empty: we could not
    observe the substrate at all, which is the failure the dark-run escalation
    guards against.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    if not alerts_path.exists():
        log(f'{alerts_path} missing — dark run', 'WARN')
        return [], 'error'
    out: list[AlertRecord] = []
    try:
        with open(alerts_path, errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get('ts'))
                if ts is None or ts < cutoff:
                    continue
                subject = rec.get('subject')
                out.append(AlertRecord(
                    ts=ts,
                    source=str(rec.get('source') or ''),
                    subject=str(subject) if subject is not None else None,
                    message=str(rec.get('message') or ''),
                    raw=rec,
                ))
    except OSError as e:
        log(f'read {alerts_path} failed: {e}', 'WARN')
        return [], 'error'
    return out, ('ok' if out else 'empty')


def classify_records(
    records: list[AlertRecord],
    *,
    registry: dict[str, dict[str, Any]],
    translations: dict[str, Any],
    route_fn: Optional[Callable[[str, Optional[str], bool], str]] = None,
) -> list[ClassifiedRecord]:
    """Re-derive each alert's tier/rationale via the pure `classify()`.

    This RECONSTRUCTS the triage decision from clean keys (source/subject/
    template) rather than joining to the stale stored triage rows.
    """
    import alert_triage_state as ats  # noqa: E402
    out: list[ClassifiedRecord] = []
    for r in records:
        result = ats.classify(r.raw, registry=registry,
                              translations=translations, route_fn=route_fn)
        out.append(ClassifiedRecord(
            source=r.source,
            signature=normalize_signature(r.subject),
            subject=r.subject,
            message=r.message,
            tier=int(result.get('tier') or 0),
            rationale=str(result.get('rationale') or ''),
        ))
    return out


# -------------------- metrics --------------------


def _rates(counts: dict[int, int], total: int) -> dict[str, float]:
    if total <= 0:
        return {'silence_rate': 0.0, 'ask_rate': 0.0, 'dispatch_rate': 0.0}
    return {
        'silence_rate': round(counts.get(3, 0) / total, 4),
        'ask_rate': round(counts.get(4, 0) / total, 4),
        'dispatch_rate': round((counts.get(1, 0) + counts.get(2, 0)) / total, 4),
    }


@dataclass
class SourceMetrics:
    volume: int = 0
    tier_counts: dict[int, int] = field(default_factory=dict)
    signatures: dict[str, int] = field(default_factory=dict)
    tier4_total: int = 0
    tier4_novel: int = 0

    @property
    def distinct_signatures(self) -> int:
        return len(self.signatures)

    @property
    def novelty(self) -> float:
        if self.tier4_total <= 0:
            return 0.0
        return round(self.tier4_novel / self.tier4_total, 4)

    @property
    def recurrence(self) -> float:
        if self.distinct_signatures <= 0:
            return 0.0
        return round(self.volume / self.distinct_signatures, 4)

    def as_dict(self) -> dict[str, Any]:
        d = {
            'volume': self.volume,
            'distinct_signatures': self.distinct_signatures,
            'recurrence': self.recurrence,
            'novelty': self.novelty,
        }
        d.update(_rates(self.tier_counts, self.volume))
        return d


def compute_metrics(records: list[ClassifiedRecord]) -> dict[str, Any]:
    """Compute per-source, per-signature, and fleet metrics + candidate lists.

    Pure: given classified records, returns the full metric structure. No IO.
    """
    per_source: dict[str, SourceMetrics] = {}
    # (source, signature) -> aggregate
    sig_agg: dict[tuple[str, str], dict[str, Any]] = {}
    fleet_tiers: dict[int, int] = {}

    for r in records:
        sm = per_source.setdefault(r.source, SourceMetrics())
        sm.volume += 1
        sm.tier_counts[r.tier] = sm.tier_counts.get(r.tier, 0) + 1
        sm.signatures[r.signature] = sm.signatures.get(r.signature, 0) + 1
        if r.tier == 4:
            sm.tier4_total += 1
            if r.is_novel:
                sm.tier4_novel += 1
        fleet_tiers[r.tier] = fleet_tiers.get(r.tier, 0) + 1

        key = (r.source, r.signature)
        agg = sig_agg.setdefault(key, {
            'source': r.source, 'signature': r.signature, 'count': 0,
            'tier_counts': {}, 'novel_count': 0, 'samples': [],
        })
        agg['count'] += 1
        agg['tier_counts'][r.tier] = agg['tier_counts'].get(r.tier, 0) + 1
        if r.is_novel:
            agg['novel_count'] += 1
        if r.message and r.message not in agg['samples'] \
                and len(agg['samples']) < SAMPLE_MESSAGES_CAP:
            agg['samples'].append(r.message)

    total = len(records)

    # Recurring-novel candidates: signatures that are novel-fallthrough tier-4
    # AND recur >= RECURRING_NOVEL_MIN_COUNT identically. Ranked by count x the
    # source's novelty (a high-novelty noisy source ranks first).
    candidates: list[dict[str, Any]] = []
    recurring_novel_total = 0
    for (source, signature), agg in sig_agg.items():
        if agg['novel_count'] >= RECURRING_NOVEL_MIN_COUNT:
            recurring_novel_total += agg['novel_count']
            src_novelty = per_source[source].novelty
            candidates.append({
                'source': source,
                'signature': signature,
                'count': agg['count'],
                'novel_count': agg['novel_count'],
                'novelty': src_novelty,
                'rank_score': round(agg['count'] * src_novelty, 4),
                'sample_messages': list(agg['samples']),
            })
    candidates.sort(key=lambda c: (c['rank_score'], c['count']), reverse=True)

    # Over-silence findings: (source, signature) at near-total silence AND high
    # volume — the over-suppression risk. Signature-level so a single noisy
    # allowlist entry is named precisely.
    over_silence: list[dict[str, Any]] = []
    for (source, signature), agg in sig_agg.items():
        vol = agg['count']
        if vol < OVER_SILENCE_MIN_VOLUME:
            continue
        srate = agg['tier_counts'].get(3, 0) / vol if vol else 0.0
        if srate >= OVER_SILENCE_SILENCE_RATE:
            over_silence.append({
                'source': source,
                'signature': signature,
                'volume': vol,
                'silence_rate': round(srate, 4),
            })
    over_silence.sort(key=lambda f: f['volume'], reverse=True)

    # noise_candidate_share (fleet) = tier-3-silenced + recurring-novel-tier-4.
    # A REPORTING figure only — see the module + artifact proxy_note.
    noise_numer = fleet_tiers.get(3, 0) + recurring_novel_total
    noise_candidate_share = round(noise_numer / total, 4) if total else 0.0

    distinct_fleet = len({(r.source, r.signature) for r in records})
    fleet = {
        'volume': total,
        'distinct_signatures': distinct_fleet,
        'recurrence': round(total / distinct_fleet, 4) if distinct_fleet else 0.0,
        'noise_candidate_share': noise_candidate_share,
    }
    fleet.update(_rates(fleet_tiers, total))

    return {
        'fleet': fleet,
        'per_source': {s: m.as_dict() for s, m in sorted(per_source.items())},
        'recurring_novel_candidates': candidates,
        'over_silence_findings': over_silence,
    }


# -------------------- artifact + emission --------------------


PROXY_NOTE = (
    'noise_candidate_share is an explicitly-labeled proxy: tier-3-silenced + '
    'recurring-novel-tier-4. It CANNOT distinguish ignored-noise from an '
    'unfixed-real problem that recurs because it is unfixed — there is no '
    'Larry-action-rate substrate until XIV-b closes the tier-4 write-back loop. '
    'It is a reporting figure, never an auto-action trigger.'
)


def build_artifact(
    metrics: dict[str, Any],
    *,
    now: datetime,
    window_days: int,
    log_status: str,
    consecutive_dark_runs: int,
) -> dict[str, Any]:
    art: dict[str, Any] = {
        'as_of': now.isoformat(),
        'window': {
            'days': window_days,
            'start': (now - timedelta(days=window_days)).isoformat(),
            'end': now.isoformat(),
        },
        'sources': {'log': log_status},
        'consecutive_dark_runs': consecutive_dark_runs,
        'proxy_note': PROXY_NOTE,
    }
    if log_status == 'error':
        # Dark run: no metrics to report; the status block + dark counter carry
        # the meaning. Empty metric shells keep the schema stable for readers.
        art.update({
            'fleet': {}, 'per_source': {},
            'recurring_novel_candidates': [], 'over_silence_findings': [],
        })
    else:
        art.update(metrics)
        # Cap the candidate list surfaced in the digest; the artifact keeps ALL.
        art['recurring_novel_candidates_capped'] = \
            metrics['recurring_novel_candidates'][:TOP_CANDIDATES_CAP]
    return art


def artifact_path_for(now: datetime) -> Path:
    return ARTIFACT_DIR / f'check-xiv-{now.date().isoformat()}.json'


def _artifact_is_valid_sentinel(path: Path) -> bool:
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(obj, dict) and bool(obj.get('as_of'))


def read_dark_run_count() -> int:
    try:
        obj = json.loads(DARK_STATE_FILE.read_text())
        n = obj.get('consecutive_dark_runs')
        return int(n) if isinstance(n, int) and n >= 0 else 0
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def write_dark_run_count(n: int) -> None:
    try:
        atomic_write_json(DARK_STATE_FILE, {'consecutive_dark_runs': int(n)},
                          indent=2)
    except OSError as e:
        log(f'dark-run state write failed: {e}', 'WARN')


def prune_old_artifacts(*, now: datetime, retention_days: int = RETENTION_DAYS) -> int:
    cutoff = now.date() - timedelta(days=retention_days)
    removed = 0
    try:
        entries = list(ARTIFACT_DIR.glob('check-xiv-*.json'))
    except OSError:
        return 0
    for p in entries:
        stem = p.name[len('check-xiv-'):-len('.json')]
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue
        if d < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def emit_alert(
    *,
    severity: str,
    message: str,
    subject: str,
    route: str = 'escalate',
    suggested_action: Optional[str] = None,
) -> bool:
    """Thin wrapper over larry_alerts.append_alert (tests monkeypatch this).

    `source` is always 'pulse-check-xiv'; `route='escalate'` is required because
    an 'info' severity would otherwise default to the silent digest lane. XIV
    must not silence its own alerts.
    """
    try:
        import larry_alerts as la  # noqa: E402
        return la.append_alert(
            source='pulse-check-xiv',
            severity=severity,
            message=message,
            subject=subject,
            route=route,
            suggested_action=suggested_action,
        )
    except Exception as e:  # noqa: BLE001 — never let a DM failure crash the check
        log(f'emit_alert failed: {type(e).__name__}: {e}', 'WARN')
        return False


def is_first_monday(d: date) -> bool:
    return d.weekday() == 0 and d.day <= 7


def format_digest(artifact: dict[str, Any]) -> str:
    fleet = artifact.get('fleet', {})
    date_str = artifact['as_of'][:10]
    lines = [
        f'Check XIV — alert-precision meter ({date_str})',
        '',
        f'fleet volume={fleet.get("volume", 0)} over {artifact["window"]["days"]}d; '
        f'silence={fleet.get("silence_rate", 0):.0%}, '
        f'ask={fleet.get("ask_rate", 0):.0%}, '
        f'dispatch={fleet.get("dispatch_rate", 0):.0%} '
        f'(dispatch≈0 = the fleet auto-fixes nothing).',
        f'noise_candidate_share={fleet.get("noise_candidate_share", 0):.0%} '
        f'(proxy — see artifact).',
    ]
    candidates = artifact.get('recurring_novel_candidates_capped', [])
    if candidates:
        lines.append('')
        lines.append(f'Top recurring-novel candidates (missing template/allowlist), '
                     f'up to {TOP_CANDIDATES_CAP}:')
        for c in candidates:
            lines.append(f'  - {c["source"]} / "{c["signature"]}" ×{c["count"]}')
    over = artifact.get('over_silence_findings', [])
    if over:
        lines.append('')
        lines.append('Over-silence findings (see the warning DM).')
    lines.append('')
    lines.append('V1 reports only — no config changed, nothing auto-landed '
                 '(that is XIV-c, gated behind XIV-b).')
    return '\n'.join(lines)


def format_over_silence(source: str, findings: list[dict[str, Any]]) -> str:
    date_str = datetime.now(timezone.utc).date().isoformat()
    lines = [
        f'Check XIV — over-silence surface for `{source}` ({date_str})',
        '',
        'These signatures are at near-total silence AND high volume — confirm '
        'the blanket silence is still right (park-don\'t-decay: an ignored '
        'recurring signal may be an unfixed real problem, not noise):',
    ]
    for f in findings:
        lines.append(f'  - "{f["signature"]}" vol={f["volume"]}, '
                     f'silence={f["silence_rate"]:.0%}')
    lines.append('')
    lines.append('V1 surfaces only; it changes no allowlist entry.')
    return '\n'.join(lines)


def emit_digest_and_surfaces(artifact: dict[str, Any], *, now: datetime) -> None:
    over = artifact.get('over_silence_findings', [])
    # Over-silence warning DM(s) — one per source (spec subject shape).
    by_source: dict[str, list[dict[str, Any]]] = {}
    for f in over:
        by_source.setdefault(f['source'], []).append(f)
    for source, findings in by_source.items():
        emit_alert(
            severity='warning',
            message=format_over_silence(source, findings),
            subject=f'pulse-check-xiv-oversilence:{source}',
            route='escalate',
            suggested_action=(
                'Review the over-silenced signatures in '
                '~/agents/blackboard/pulse-check-xiv/ and confirm the '
                'alert-translations.json entry is still the right call.'
            ),
        )

    # Precision digest — first Monday of the month OR when over-silence trips.
    if is_first_monday(now.date()) or over:
        emit_alert(
            severity='info',
            message=format_digest(artifact),
            subject='pulse-check-xiv-digest',
            route='escalate',
        )


# -------------------- main --------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute + print but do not write artifact or DM.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass same-day idempotency (re-run for today).')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    target_path = artifact_path_for(now)

    if (target_path.exists() and _artifact_is_valid_sentinel(target_path)
            and not args.force and not args.dry_run):
        log(f'Check XIV already ran today ({now.date().isoformat()}); '
            'skipping (use --force to re-run).')
        return 0

    records, log_status = load_records(now=now)

    if log_status == 'error':
        dark_count = read_dark_run_count() + 1
        artifact = build_artifact(
            {}, now=now, window_days=WINDOW_DAYS, log_status='error',
            consecutive_dark_runs=dark_count)
        if args.dry_run:
            print(json.dumps(artifact, indent=2))
            return 0
        # Only artifact-write failure is non-zero (§ 5 contract).
        try:
            atomic_write_json(target_path, artifact, indent=2)
        except OSError as e:
            log(f'artifact write failed: {e}', 'ERROR')
            return 1
        write_dark_run_count(dark_count)
        if dark_count >= DARK_ESCALATE_AFTER:
            emit_alert(
                severity='warning',
                message=(
                    f'Check XIV has had {dark_count} consecutive dark runs — '
                    f'the alert log at {ALERTS_FILE} is missing or unreadable, '
                    'so alert-precision is unobservable. Investigate the log '
                    'before the next firing.'),
                subject='pulse-check-xiv-dark',
                route='escalate',
                suggested_action=f'ls -l {ALERTS_FILE}; tail {LOG_FILE}',
            )
        log(f'Check XIV dark run ({dark_count} consecutive); artifact written.')
        return 0

    registry, translations = {}, {}
    try:
        import alert_triage_state as ats  # noqa: E402
        registry = ats.load_registry()
        translations = ats.load_translations()
    except Exception as e:  # noqa: BLE001
        log(f'registry/translations load failed: {type(e).__name__}: {e}', 'WARN')

    classified = classify_records(
        records, registry=registry, translations=translations)
    metrics = compute_metrics(classified)
    artifact = build_artifact(
        metrics, now=now, window_days=WINDOW_DAYS, log_status=log_status,
        consecutive_dark_runs=0)

    if args.dry_run:
        print(json.dumps(artifact, indent=2))
        return 0

    try:
        atomic_write_json(target_path, artifact, indent=2)
    except OSError as e:
        log(f'artifact write failed: {e}', 'ERROR')
        return 1

    write_dark_run_count(0)
    prune_old_artifacts(now=now)
    emit_digest_and_surfaces(artifact, now=now)

    fleet = metrics['fleet']
    log(f'Check XIV complete: volume={fleet["volume"]} '
        f'silence={fleet["silence_rate"]} ask={fleet["ask_rate"]} '
        f'dispatch={fleet["dispatch_rate"]} '
        f'candidates={len(metrics["recurring_novel_candidates"])} '
        f'over_silence={len(metrics["over_silence_findings"])}')
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check as _hb_run_check
    sys.exit(_hb_run_check('xiv', main, log_fn=log))
