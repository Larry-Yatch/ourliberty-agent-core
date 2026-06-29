#!/usr/bin/env python3
"""pulse_check_retrospective_author.py — weekly elevation-retrospective, Stage B.

Spec: agents/beacon/specs/alert-pipeline-rework.md, PHASE 3 (Stage B).

Stage B is the *bounded-LLM* half of the weekly retrospective. Stage A
(``pulse_check_retrospective.py``) does the deterministic gather→bucket→join and
emits ``retrospective-candidates.json``; Stage B reads that artifact, asks a
bounded ``claude --print`` author to classify each FRESH bucket into one of

  - **AUTOMATE-NOW** — recurring + benign + fix fits an allowed template;
  - **FIX-PERMANENTLY** — recurring real defect → an objective build mission;
  - **KEEP-ELEVATING** — a genuine per-instance decision → no card, counted only.

…then pre-drafts the concrete change for AUTOMATE-NOW from one of 6 allowed
templates and posts each card as a ``phase:'proposed'`` mission via
``POST /api/system/missions/new`` (which queues it for the missions writer).

The LLM is advisory; a *deterministic gate* (``validate_automate_now``) is the
real authority on whether a card may be AUTOMATE-NOW — it re-checks recurrence,
benignness (never a reject-as-wrong histogram), and template validity, and
downgrades to FIX-PERMANENTLY on any miss. So a hallucinated "automate this"
can never smuggle an un-benign or template-less change onto the board.

Dedup is two-layer (spec § R4): a registry-id-prefix skip against the live
board (don't double-post an open proposal), plus the promotion-probation ledger
(``retrospective-ledger.json``) — Stage A already excludes ``suppressed-dismissed``
/ ``already-proposed`` from the FRESH set; Stage B sets the ``proposed`` flag on
post and reconciles the ``dismissed`` flag from board dismissals (``acknowledged``),
so declining a card keeps it quiet until it re-crosses the dismiss count by the
margin.

Read-only except its three writes: the proposed-mission queue (via the API),
the author artifact (canonical + validated per-ISO-week sentinel), and the
ledger dedup flags. Never commits, never opens a PR, never edits config.

Plumbing cloned from ``pulse_check_ix.py`` (HTTP helpers, validated per-week
sentinel) and the bounded-LLM pattern from ``ceo_digest_generator.py`` (claude
``--print --output-format json``, ``timeout``, ``total_cost_usd`` capture,
graceful fallback, ``refuse_under_test``). Wrapped in
``pulse_check_heartbeat.run_check('retrospective-author', …)``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', str(Path.home() / 'agents')))
REPO_ROOT = Path(__file__).resolve().parent.parent

CANONICAL_ARTIFACT = AGENTS_ROOT / 'blackboard' / 'retrospective-candidates.json'
AUTHOR_ARTIFACT_DIR = AGENTS_ROOT / 'blackboard' / 'pulse-retrospective-author'
AUTHOR_CANONICAL = AGENTS_ROOT / 'blackboard' / 'retrospective-author.json'
COSTS_FILE = AGENTS_ROOT / 'blackboard' / 'costs.jsonl'
ACTIVE_TIER_FILE = AGENTS_ROOT / 'blackboard' / 'active-tier.json'
LOG_FILE = AGENTS_ROOT / 'logs' / 'pulse-check-retrospective-author.log'

DEFAULT_API_BASE = 'http://127.0.0.1:8000'
DEFAULT_REPO = 'ourliberty-agent-core'
SPEC_DOC = 'agents/beacon/specs/alert-pipeline-rework.md'

# Modeled on ceo_digest_generator.py — same bounded sonnet author.
AUTHOR_MODEL = 'claude-sonnet-4-6'
CLAUDE_TIMEOUT_SEC = int(
    os.environ.get('RETROSPECTIVE_AUTHOR_CLAUDE_TIMEOUT_SEC', '180')
)

# Classification vocabulary (spec § Classification + confidence gate).
CLS_AUTOMATE_NOW = 'automate-now'
CLS_FIX_PERMANENTLY = 'fix-permanently'
CLS_KEEP_ELEVATING = 'keep-elevating'
VALID_CLASSES = (CLS_AUTOMATE_NOW, CLS_FIX_PERMANENTLY, CLS_KEEP_ELEVATING)

# The 6 allowed automate-now templates (spec § Classification). Each is an
# existing reversible surface; the LLM must pick exactly one and the gate
# rejects any id outside this registry. `file_hint` documents where the
# pre-drafted change would land (for the human reviewing the card) — Stage B
# does NOT edit these files; it only describes the change.
TEMPLATE_REGISTRY: dict[str, dict[str, str]] = {
    '1': {
        'name': 'route-source-to-hold',
        'summary': 'Route a benign source to `hold` so it shows on the '
                   'dashboard but does not DM next sweep.',
        'file_hint': 'config/alert-routing.json (source → hold)',
    },
    '2': {
        'name': 'medic-silenceable-subject',
        'summary': "Add a subject to Medic's `silenceable_subjects` so the "
                   'recurring benign line stops re-alerting.',
        'file_hint': 'config/medic-silenceable-subjects.json',
    },
    '3': {
        'name': 'medic-restart-allowlist',
        'summary': "Add a unit to Medic's restart/retrigger allowlist so the "
                   'recurring failure self-heals.',
        'file_hint': 'config/medic-restart-allowlist.json',
    },
    '4': {
        'name': 'check0-auto-fix-pattern',
        'summary': 'Add/relax a Pulse Check 0 `auto-fix-patterns.json` entry so '
                   'the pattern auto-resolves.',
        'file_hint': 'config/auto-fix-patterns.json',
    },
    '5': {
        'name': 'reclassify-emitter-info',
        'summary': "Reclassify an emitter to `severity='info'` so it stops "
                   'elevating.',
        'file_hint': 'the emitter source (severity=info)',
    },
    '6': {
        'name': 'healer-reconciliation',
        'summary': 'A healer-side reconciliation that closes the loop the '
                   'recurring elevation flags.',
        'file_hint': 'the owning healer script',
    },
}

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from atomic_io import atomic_write_json  # noqa: E402
# Stage B reuses Stage A's ledger helpers + paths so there is one ledger reader,
# no schema drift. Stage A runs first in run_retrospective.sh, so the ledger
# Stage B loads already carries this week's weeks/count; Stage B only mutates
# the dedup flags it owns.
import pulse_check_retrospective as stage_a  # noqa: E402
from test_isolation_guard import refuse_under_test  # noqa: E402

try:  # active_tier is optional in bare test envs; the durable env is best-effort.
    import active_tier  # noqa: E402
except Exception:  # noqa: BLE001
    active_tier = None  # type: ignore


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


# -------------------- HTTP plumbing (cloned from pulse_check_ix.py) --------------------


def _api_base() -> str:
    return os.environ.get('DASHBOARD_API_URL', DEFAULT_API_BASE).rstrip('/')


def _api_token() -> Optional[str]:
    tok = (os.environ.get('DASHBOARD_API_TOKEN') or '').strip()
    return tok or None


def _http_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    data: Optional[bytes] = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        hdrs.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace') if e.fp else ''
        status = e.code
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f'http {method} {url} failed: {e}') from e
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {'raw': raw}
    if not isinstance(payload, dict):
        payload = {'raw': payload}
    return status, payload


def list_existing_missions(
    *, api_base: Optional[str] = None, token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """GET /api/system/missions — ALL entries (every phase).

    Stage B needs proposed + acknowledged cards (not just drafting) to dedup an
    open proposal and to reconcile board dismissals into the ledger."""
    base = (api_base or _api_base()).rstrip('/')
    tok = token or _api_token()
    if not tok:
        log('DASHBOARD_API_TOKEN missing — cannot read missions for dedup', 'WARN')
        return []
    status, payload = _http_request(
        'GET', f'{base}/api/system/missions',
        headers={'X-Dashboard-Token': tok},
    )
    if status != 200:
        log(f'GET /api/system/missions returned {status}', 'WARN')
        return []
    missions = payload.get('missions') or []
    return [m for m in missions if isinstance(m, dict)] if isinstance(missions, list) else []


def post_proposed_mission(
    *,
    name: str,
    brief: str,
    predraft: dict[str, Any],
    spec_docs: list[str],
    api_base: Optional[str] = None,
    token: Optional[str] = None,
    repo: str = DEFAULT_REPO,
) -> tuple[int, dict[str, Any]]:
    """POST /api/system/missions/new with ``phase:'proposed'`` (spec § R4)."""
    base = (api_base or _api_base()).rstrip('/')
    tok = token or _api_token()
    if not tok:
        raise RuntimeError('DASHBOARD_API_TOKEN missing — cannot post proposal')
    body = {
        'name': name,
        'brief': brief,
        'repo': repo,
        'spec_docs': spec_docs,
        'phase': 'proposed',
        'proposed_by': 'retrospective-author',
        'predraft': predraft,
    }
    return _http_request(
        'POST', f'{base}/api/system/missions/new',
        headers={'X-Dashboard-Token': tok},
        body=body,
    )


# -------------------- mission identity --------------------


_KEBAB_RE = re.compile(r'[^a-z0-9]+')


def _kebab(name: str) -> str:
    """Match dashboard_api._kebab_case so the id we predict equals the id the
    endpoint derives (dedup-prefix correctness)."""
    return _KEBAB_RE.sub('-', name.strip().lower()).strip('-')


def mission_name(signature: str, week_anchor: str) -> str:
    """Stable, human-legible card name. Kebab-cases (via the endpoint) to
    ``retrospective-<sig-slug>-<week_anchor>`` so the id prefix is greppable."""
    sig_slug = _kebab(signature) or 'unknown'
    return f'retrospective {sig_slug} {week_anchor}'


def mission_id_for(signature: str, week_anchor: str) -> str:
    return _kebab(mission_name(signature, week_anchor))


# -------------------- LLM classification (bounded, modeled on ceo_digest) --------------------


def _candidate_digest(candidate: dict[str, Any]) -> dict[str, Any]:
    """Compact view of a Stage-A bucket for the LLM prompt — enough to classify,
    no event-id noise."""
    return {
        'root_signature': candidate.get('root_signature'),
        'source': candidate.get('source'),
        'normalized_subject': candidate.get('normalized_subject'),
        'count_this_period': candidate.get('count_this_period'),
        'weeks_recurring': candidate.get('weeks_recurring'),
        'recurrence_actionable': candidate.get('recurrence_actionable'),
        'resolution_histogram': candidate.get('resolution_histogram'),
        'trend': candidate.get('trend'),
        'live_unresolved': candidate.get('live_unresolved'),
        'event_types': candidate.get('event_types'),
    }


def build_prompt(candidates: list[dict[str, Any]]) -> str:
    templates = '\n'.join(
        f'  {tid}. {t["name"]} — {t["summary"]}'
        for tid, t in TEMPLATE_REGISTRY.items()
    )
    digest = json.dumps([_candidate_digest(c) for c in candidates], indent=2)
    return (
        'You are the weekly elevation-retrospective author for an autonomous '
        'agent fleet. Each item below is a bucket of alerts that reached the '
        'operator over the trailing week, already normalized to a root '
        'signature with a resolution histogram (how the operator responded: '
        'approve/reject/silence/ack/none).\n\n'
        'Classify EACH bucket into exactly one of:\n'
        f'  - "{CLS_AUTOMATE_NOW}": recurring AND benign (the operator kept '
        'taking the same low-risk action — never reject-as-wrong) AND the fix '
        'fits one of the allowed templates below.\n'
        f'  - "{CLS_FIX_PERMANENTLY}": recurring from a real defect or needing '
        'a non-trivial fix → an objective build mission (no template fits).\n'
        f'  - "{CLS_KEEP_ELEVATING}": a genuine per-instance decision the '
        'operator must keep making → no card.\n\n'
        f'Allowed automate-now templates (pick exactly one id for an '
        f'automate-now item):\n{templates}\n\n'
        'For an automate-now item, pre-draft the concrete change: the template '
        'id, the file/key it touches, the value to add, and a one-line diff '
        'sketch. Be specific and reversible.\n\n'
        'Return ONLY a JSON array (no prose), one object per bucket, with keys: '
        '"root_signature" (verbatim from the input), "classification" (one of '
        'the three strings), "rationale" (one sentence), and for automate-now '
        'also "template_id" (string "1"-"6"), "file_key", "value", '
        '"diff_sketch". Omit the template fields for non-automate-now items.\n\n'
        f'Buckets:\n{digest}\n'
    )


def _claude_classify(prompt: str) -> tuple[Optional[str], Optional[float]]:
    """Invoke the bounded claude author. Returns (raw_text, cost_usd).

    On ANY failure (timeout, non-zero exit, parse failure, empty) returns
    (None, None) so the caller skips posting this cycle. Never raises."""
    refuse_under_test('claude-spawn')
    env = None
    if active_tier is not None:
        try:
            env = active_tier.durable_claude_env()
        except Exception:  # noqa: BLE001 — fall back to inherited env
            env = None
    try:
        proc = subprocess.run(
            ['claude', '--print', '--model', AUTHOR_MODEL,
             '--output-format', 'json', prompt],
            capture_output=True, text=True, env=env,
            timeout=CLAUDE_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f'claude invocation failed: {type(e).__name__}: {e}', 'WARN')
        return None, None
    if proc.returncode != 0:
        log(f'claude exited {proc.returncode}; skipping posting this cycle', 'WARN')
        return None, None
    try:
        data = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError:
        log('claude output was not JSON; skipping', 'WARN')
        return None, None
    text = (data.get('result') or '').strip() if isinstance(data, dict) else ''
    if not text:
        log('claude returned empty result; skipping', 'WARN')
        return None, None
    cost = None
    if isinstance(data, dict):
        raw_cost = data.get('total_cost_usd', data.get('cost_usd'))
        if raw_cost is not None:
            try:
                cost = float(raw_cost)
            except (TypeError, ValueError):
                cost = None
    return text, cost


def parse_classifications(raw_text: str) -> Optional[dict[str, dict[str, Any]]]:
    """Parse the LLM's JSON array into ``{root_signature: classification_obj}``.

    Tolerant of a ```json fence and of leading/trailing prose around the array.
    Returns None when no JSON array can be recovered (caller treats as a miss)."""
    if not raw_text:
        return None
    text = raw_text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('['), text.rfind(']')
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, list):
        return None
    out: dict[str, dict[str, Any]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sig = item.get('root_signature')
        if isinstance(sig, str) and sig:
            out[sig] = item
    return out


# Type of the injectable classifier seam: candidates -> (classifications, cost).
ClassifierFn = Callable[
    [list[dict[str, Any]]],
    tuple[Optional[dict[str, dict[str, Any]]], Optional[float]],
]


def llm_classifier(
    candidates: list[dict[str, Any]],
) -> tuple[Optional[dict[str, dict[str, Any]]], Optional[float]]:
    """Production classifier: build prompt → spawn claude → parse. The unit
    tests inject a stub of this signature instead so they never spawn claude."""
    raw, cost = _claude_classify(build_prompt(candidates))
    if raw is None:
        return None, cost
    return parse_classifications(raw), cost


# -------------------- deterministic gate (the real authority) --------------------


def histogram_is_benign(histogram: dict[str, Any]) -> bool:
    """Benign = the operator never rejected-as-wrong, and acted at least once.

    A bucket the operator rejected even once is NOT a safe auto-silence target
    (rejecting means 'this elevation was wrong to suppress'). An all-`none`
    bucket (operator never engaged) is also not benign-by-evidence — there is no
    consistent low-risk action to encode."""
    if not isinstance(histogram, dict):
        return False
    reject = int(histogram.get(stage_a.RES_REJECT, 0) or 0)
    if reject > 0:
        return False
    engaged = sum(
        int(histogram.get(b, 0) or 0)
        for b in (stage_a.RES_APPROVE, stage_a.RES_SILENCE, stage_a.RES_ACK)
    )
    return engaged > 0


def validate_automate_now(
    candidate: dict[str, Any], classification: dict[str, Any],
) -> Optional[str]:
    """Deterministic gate for an LLM `automate-now`. Returns the validated
    template_id on PASS, or None to force a downgrade to fix-permanently.

    Re-checks (independent of the LLM): recurrence-actionable, benign histogram,
    and a real template id. This is what bounds a hallucinated 'automate this'
    to a safe, reversible surface (spec § Classification + confidence gate)."""
    if not candidate.get('recurrence_actionable'):
        return None
    if not histogram_is_benign(candidate.get('resolution_histogram') or {}):
        return None
    tid = classification.get('template_id')
    if isinstance(tid, (int, float)):
        tid = str(int(tid))
    if not isinstance(tid, str) or tid not in TEMPLATE_REGISTRY:
        return None
    return tid


# -------------------- decisions + pre-draft --------------------


@dataclass
class Decision:
    candidate: dict[str, Any]
    classification: str          # one of VALID_CLASSES (post-gate)
    predraft: dict[str, Any]     # carried on the proposed card
    rationale: str


def _predraft_for(
    candidate: dict[str, Any],
    classification: str,
    llm_obj: dict[str, Any],
    template_id: Optional[str],
) -> dict[str, Any]:
    sig = candidate.get('root_signature')
    base = {
        'classification': classification,
        'root_signature': sig,
        # The dismiss-reconcile reads this back off the board to set the ledger
        # `dismissed_at_count` without re-deriving the period count.
        'count_at_proposal': int(candidate.get('count_this_period', 0) or 0),
        'rationale': str(llm_obj.get('rationale', ''))[:500],
    }
    if classification == CLS_AUTOMATE_NOW and template_id:
        tpl = TEMPLATE_REGISTRY[template_id]
        base.update({
            'template_id': template_id,
            'template_name': tpl['name'],
            'file_key': str(llm_obj.get('file_key', tpl['file_hint']))[:300],
            'value': str(llm_obj.get('value', ''))[:500],
            'diff_sketch': str(llm_obj.get('diff_sketch', ''))[:800],
        })
    return base


def gate_classifications(
    candidates: list[dict[str, Any]],
    classifications: dict[str, dict[str, Any]],
) -> list[Decision]:
    """Apply the deterministic gate to the LLM output, producing one Decision
    per candidate. A candidate the LLM omitted, or labeled with an unknown
    class, defaults to KEEP-ELEVATING (the safe no-card outcome)."""
    decisions: list[Decision] = []
    for cand in candidates:
        sig = cand.get('root_signature')
        obj = classifications.get(sig) if isinstance(sig, str) else None
        obj = obj if isinstance(obj, dict) else {}
        raw_cls = obj.get('classification')
        cls = raw_cls if raw_cls in VALID_CLASSES else CLS_KEEP_ELEVATING
        template_id: Optional[str] = None
        if cls == CLS_AUTOMATE_NOW:
            template_id = validate_automate_now(cand, obj)
            if template_id is None:
                # Gate rejected the automate-now → it is still a recurring
                # problem, just not a templated one: a fix-permanently mission.
                cls = CLS_FIX_PERMANENTLY
        decisions.append(Decision(
            candidate=cand,
            classification=cls,
            predraft=_predraft_for(cand, cls, obj, template_id),
            rationale=str(obj.get('rationale', ''))[:500],
        ))
    return decisions


def _brief_for(decision: Decision, week_anchor: str) -> str:
    cand = decision.candidate
    pd = decision.predraft
    lines = [
        f'Weekly elevation retrospective ({week_anchor}) — '
        f'{decision.classification.upper()}.',
        '',
        f'Signature: {cand.get("root_signature")}',
        f'Source: {cand.get("source")}  ·  subject: {cand.get("normalized_subject")}',
        f'This period: {cand.get("count_this_period")} fire(s), '
        f'{cand.get("weeks_recurring")} week(s) recurring, trend={cand.get("trend")}.',
        f'Resolution histogram: {cand.get("resolution_histogram")}.',
        '',
        f'Rationale: {decision.rationale or "(none)"}',
    ]
    if decision.classification == CLS_AUTOMATE_NOW:
        lines += [
            '',
            f'Pre-drafted automate-now fix (template {pd.get("template_id")} '
            f'— {pd.get("template_name")}):',
            f'  file/key: {pd.get("file_key")}',
            f'  value:    {pd.get("value")}',
            f'  diff:     {pd.get("diff_sketch")}',
            '',
            'Accept to move this to drafting; dismiss to silence it (it stays '
            'quiet until it re-crosses its dismiss count by the margin).',
        ]
    else:
        lines += [
            '',
            'Recurring problem with no safe templated auto-fix — build an '
            'objective fix (spec, no code in this card). Accept to start '
            'drafting; dismiss to silence.',
        ]
    return '\n'.join(lines)


# -------------------- board dedup + ledger reconcile --------------------


def live_proposed_signatures(missions: list[dict[str, Any]]) -> set[str]:
    """Root signatures with a LIVE (not acknowledged/dismissed) proposed card on
    the board — the registry-id-prefix skip set (spec § R4 dedup)."""
    out: set[str] = set()
    for m in missions:
        if not isinstance(m, dict):
            continue
        if m.get('phase') != 'proposed' or m.get('acknowledged'):
            continue
        pd = m.get('predraft')
        sig = pd.get('root_signature') if isinstance(pd, dict) else None
        if isinstance(sig, str) and sig:
            out.add(sig)
    return out


def board_dismissals(missions: list[dict[str, Any]]) -> dict[str, int]:
    """Root signature → count-at-proposal for proposed cards the operator
    DISMISSED (``acknowledged: true``). Drives the ledger `dismissed` reconcile
    so a declined card is not re-proposed (spec acceptance: declining prevents
    re-proposal)."""
    out: dict[str, int] = {}
    for m in missions:
        if not isinstance(m, dict):
            continue
        if m.get('phase') != 'proposed' or not m.get('acknowledged'):
            continue
        pd = m.get('predraft')
        if not isinstance(pd, dict):
            continue
        sig = pd.get('root_signature')
        if isinstance(sig, str) and sig:
            out[sig] = int(pd.get('count_at_proposal', 0) or 0)
    return out


def reconcile_ledger_dismissals(
    ledger: dict[str, dict[str, Any]],
    dismissals: dict[str, int],
    *,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    """Stamp `dismissed`/`dismissed_at_count`/`dismissed_ts` onto the ledger for
    every board dismissal. Mutates + returns the same dict."""
    now_iso = now.isoformat()
    for sig, count_at in dismissals.items():
        entry = ledger.setdefault(sig, {})
        if not entry.get('dismissed'):
            entry['dismissed'] = True
            entry['dismissed_at_count'] = count_at
            entry['dismissed_ts'] = now_iso
    return ledger


def mark_ledger_proposed(
    ledger: dict[str, dict[str, Any]], signature: str, *, now: datetime,
) -> None:
    entry = ledger.setdefault(signature, {})
    entry['proposed'] = True
    entry['proposed_ts'] = now.isoformat()


# -------------------- orchestration --------------------


@dataclass
class AuthorResult:
    week_anchor: str
    decisions: list[Decision]
    posted: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    llm_cost_usd: Optional[float] = None
    llm_unavailable: bool = False

    def counts(self) -> dict[str, int]:
        c = {CLS_AUTOMATE_NOW: 0, CLS_FIX_PERMANENTLY: 0, CLS_KEEP_ELEVATING: 0}
        for d in self.decisions:
            c[d.classification] = c.get(d.classification, 0) + 1
        return c


# A poster matches post_proposed_mission's keyword call shape; injected in tests.
PosterFn = Callable[..., tuple[int, dict[str, Any]]]


def register_decisions(
    decisions: list[Decision],
    week_anchor: str,
    *,
    ledger: dict[str, dict[str, Any]],
    existing_missions: list[dict[str, Any]],
    now: datetime,
    dry_run: bool = False,
    poster: Optional[PosterFn] = None,
    api_base: Optional[str] = None,
    token: Optional[str] = None,
    repo: str = DEFAULT_REPO,
) -> AuthorResult:
    """Post one proposed card per AUTOMATE-NOW / FIX-PERMANENTLY decision (skip
    KEEP-ELEVATING), applying board-dedup + ledger `proposed` flags.

    Pure w.r.t. its inputs: ``existing_missions`` and ``poster`` are injectable
    so tests run with no HTTP. ``ledger`` is mutated in place (dedup flags)."""
    result = AuthorResult(week_anchor=week_anchor, decisions=decisions)
    live_sigs = live_proposed_signatures(existing_missions)
    existing_ids = {
        m.get('id') for m in existing_missions if isinstance(m, dict)
    }
    post = poster or post_proposed_mission

    for d in decisions:
        sig = d.candidate.get('root_signature') or ''
        if d.classification == CLS_KEEP_ELEVATING:
            continue
        mid = mission_id_for(sig, week_anchor)
        if sig in live_sigs or mid in existing_ids:
            result.skipped.append({
                'root_signature': sig,
                'reason': 'live proposed card already on board (registry-id-'
                          'prefix dedup, spec § R4)',
            })
            log(f'skip {sig}: existing proposed card')
            continue
        name = mission_name(sig, week_anchor)
        brief = _brief_for(d, week_anchor)
        if dry_run:
            result.posted.append({
                'root_signature': sig, 'dry_run': True,
                'mission_id': mid, 'classification': d.classification,
                'predraft': d.predraft,
            })
            mark_ledger_proposed(ledger, sig, now=now)
            continue
        try:
            status, payload = post(
                name=name, brief=brief, predraft=d.predraft,
                spec_docs=[SPEC_DOC],
                api_base=api_base, token=token, repo=repo,
            )
        except RuntimeError as e:
            result.errors.append({'root_signature': sig, 'error': str(e)})
            log(f'post {sig} failed: {e}', 'WARN')
            continue
        if status not in (200, 201):
            result.errors.append({
                'root_signature': sig, 'http_status': status, 'response': payload,
            })
            log(f'post {sig} returned {status}: {payload}', 'WARN')
            continue
        result.posted.append({
            'root_signature': sig,
            'mission_id': payload.get('mission_id', mid),
            'classification': d.classification,
            'status': payload.get('status'),
        })
        mark_ledger_proposed(ledger, sig, now=now)
        log(f'posted proposed {d.classification} card for {sig}: '
            f'{payload.get("mission_id", mid)}')

    return result


def author_cycle(
    artifact: dict[str, Any],
    *,
    ledger: dict[str, dict[str, Any]],
    existing_missions: list[dict[str, Any]],
    classifier: ClassifierFn,
    week_anchor: str,
    now: datetime,
    dry_run: bool = False,
    poster: Optional[PosterFn] = None,
    api_base: Optional[str] = None,
    token: Optional[str] = None,
    repo: str = DEFAULT_REPO,
) -> tuple[AuthorResult, dict[str, dict[str, Any]]]:
    """End-to-end Stage B for pre-loaded inputs: reconcile board dismissals →
    classify FRESH candidates → gate → post → set ledger flags. Returns
    ``(result, new_ledger)``. No IO beyond the injected ``poster``/``classifier``."""
    # 1. Reconcile board dismissals into the ledger FIRST so a just-declined
    #    card suppresses any same-cycle re-proposal of the same signature.
    reconcile_ledger_dismissals(ledger, board_dismissals(existing_missions), now=now)

    candidates = artifact.get('candidates') or []
    eligible = [
        c for c in candidates
        if isinstance(c, dict) and c.get('dedup_status') == 'fresh'
    ]

    if not eligible:
        return AuthorResult(week_anchor=week_anchor, decisions=[]), ledger

    classifications, cost = classifier(eligible)
    if classifications is None:
        # Bounded-LLM fallback (spec § Cadence/liveness/safety): skip posting
        # this cycle rather than guessing. The week's sentinel still records the
        # run so liveness stays fresh.
        res = AuthorResult(
            week_anchor=week_anchor, decisions=[],
            llm_cost_usd=cost, llm_unavailable=True,
        )
        return res, ledger

    decisions = gate_classifications(eligible, classifications)
    result = register_decisions(
        decisions, week_anchor,
        ledger=ledger, existing_missions=existing_missions, now=now,
        dry_run=dry_run, poster=poster, api_base=api_base, token=token, repo=repo,
    )
    result.llm_cost_usd = cost
    return result, ledger


# -------------------- artifact --------------------


def author_sentinel_path(week_anchor: str) -> Path:
    return AUTHOR_ARTIFACT_DIR / f'retrospective-author-{week_anchor}.json'


def build_author_artifact(
    result: AuthorResult,
    *,
    week_anchor: str,
    as_of_iso: str,
    source_trend_line: str,
) -> dict[str, Any]:
    counts = result.counts()
    return {
        'as_of': as_of_iso,
        'week_anchor': week_anchor,
        'summary': {
            'automate_now': counts[CLS_AUTOMATE_NOW],
            'fix_permanently': counts[CLS_FIX_PERMANENTLY],
            'keep_elevating': counts[CLS_KEEP_ELEVATING],
            'posted': len(result.posted),
            'skipped': len(result.skipped),
            'errors': len(result.errors),
            'llm_cost_usd': result.llm_cost_usd,
            'llm_unavailable': result.llm_unavailable,
            # Carried verbatim from Stage A so the one plain-language trend line
            # (= Phase-4 verification) rides along on the author artifact too.
            'trend_line': source_trend_line,
        },
        'classifications': [
            {
                'root_signature': d.candidate.get('root_signature'),
                'classification': d.classification,
                'predraft': d.predraft,
                'rationale': d.rationale,
            }
            for d in result.decisions
        ],
        'posted': result.posted,
        'skipped': result.skipped,
        'errors': result.errors,
    }


def write_author_artifact(artifact: dict[str, Any]) -> tuple[Path, Path]:
    """Atomic per-ISO-week sentinel + canonical latest. The per-week file is the
    same-week idempotency sentinel (the validated main() gate), so a mid-write
    crash must leave either the intact prior or the intact new file — never a
    truncated one that satisfies .exists() and permanently suppresses the check
    (audit #7 hardening, mirrored from Stage A / pulse_check_viii)."""
    wk = artifact['week_anchor']
    sentinel = atomic_write_json(author_sentinel_path(wk), artifact, indent=2)
    canonical = atomic_write_json(AUTHOR_CANONICAL, artifact, indent=2)
    return sentinel, canonical


def _artifact_is_valid_sentinel(path: Path) -> bool:
    """Parseable artifact with a week_anchor = a real completion marker. A
    zero-byte/truncated file satisfies .exists() but is not (audit #7)."""
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(obj, dict) and bool(obj.get('week_anchor'))


# -------------------- cost capture (modeled on ceo_digest_generator) --------------------


def _account_tier() -> str:
    try:
        with open(ACTIVE_TIER_FILE, encoding='utf-8') as f:
            return str(json.load(f).get('tier') or 'tier1')
    except (OSError, json.JSONDecodeError, AttributeError):
        return 'tier1'


def append_cost_row(week_anchor: str, cost_usd: Optional[float], success: bool) -> None:
    if cost_usd is None:
        return
    ts = datetime.now(timezone.utc).isoformat()
    row = {
        'ts': ts,
        'agent': 'retrospective-author',
        'task_id': f'retrospective-author-{week_anchor}-{ts}',
        'task_type': 'retrospective-author',
        'model': AUTHOR_MODEL,
        'account': _account_tier(),
        'cost_usd': cost_usd,
        'success': success,
        'source': 'pulse_check_retrospective_author.py',
    }
    try:
        COSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COSTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')
    except OSError as e:
        log(f'cost-capture append failed: {type(e).__name__}: {e}', 'WARN')


# -------------------- main --------------------


def _load_artifact(path: Path) -> Optional[dict[str, Any]]:
    try:
        obj = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log(f'read candidates artifact {path} failed: {e}', 'WARN')
        return None
    return obj if isinstance(obj, dict) else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Classify + print the author artifact; do not POST '
                             'missions or write the artifact/ledger.')
    parser.add_argument('--force', action='store_true',
                        help='Bypass the weekly idempotency sentinel and re-run.')
    parser.add_argument('--input', default=str(CANONICAL_ARTIFACT),
                        help='Path to the Stage A candidates artifact.')
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    week_anchor = stage_a.iso_week_monday(now.date())
    sentinel = author_sentinel_path(week_anchor)

    if (sentinel.exists() and _artifact_is_valid_sentinel(sentinel)
            and not args.force and not args.dry_run):
        log(f'Retrospective author already ran this week ({week_anchor}); '
            'skipping (use --force to re-run).')
        return 0

    artifact = _load_artifact(Path(args.input))
    if artifact is None:
        log('Stage A candidates artifact missing/unreadable — Stage A must run '
            'before Stage B; cannot author.', 'ERROR')
        return 1

    ledger = stage_a.load_ledger()
    existing_missions = [] if args.dry_run else list_existing_missions()

    result, new_ledger = author_cycle(
        artifact,
        ledger=ledger,
        existing_missions=existing_missions,
        classifier=llm_classifier,
        week_anchor=week_anchor,
        now=now,
        dry_run=args.dry_run,
    )

    out = build_author_artifact(
        result, week_anchor=week_anchor, as_of_iso=now.isoformat(),
        source_trend_line=str(
            (artifact.get('summary') or {}).get('trend_line', '')
        ),
    )

    if args.dry_run:
        print(json.dumps(out, indent=2))
        return 0

    write_author_artifact(out)
    stage_a.write_ledger(new_ledger)
    append_cost_row(week_anchor, result.llm_cost_usd, success=not result.llm_unavailable)
    s = out['summary']
    log(
        f'Retrospective author complete ({week_anchor}): '
        f'automate-now={s["automate_now"]} fix-permanently={s["fix_permanently"]} '
        f'keep-elevating={s["keep_elevating"]} posted={s["posted"]} '
        f'skipped={s["skipped"]} errors={s["errors"]} '
        f'llm_unavailable={s["llm_unavailable"]}'
    )
    return 0


if __name__ == '__main__':
    from pulse_check_heartbeat import run_check
    sys.exit(run_check('retrospective-author', main, log_fn=log))
