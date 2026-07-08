#!/usr/bin/env python3
"""main_suite_guardian.py — Main-Suite Green Guardian detector/classifier (D1).

Spec: agents/beacon/specs/main-suite-green-guardian.md (PR-1, shadow mode).

WHY
---
main's suite was "green-by-cancellation": the two-run gate ran parent+head
fresh in one invocation, so a deterministic full-suite flake failed on BOTH
sides and hid in the tolerated intersection. The baseline cache (#774) broke
that symmetry and surfaced the flakes as false regressions (#866 fixed the
gate verdict). But nothing drives standing reds to zero. This guardian is the
detector: a nightly full-suite run at ONE pinned origin/main SHA that
classifies every red by isolation re-run and records it in a registry, so the
red count can be driven to zero through the propose->approve->dispatch fabric
(D2/D3, later PRs).

D1 SCOPE (this file, shadow mode — detect + record only):
  * pin one origin/main SHA per cycle; skip when unchanged (identical code =
    identical information), EXCEPT after an inconclusive run (L5/D1.1);
  * canary preflight (2 hermetic tests) — canary red => whole run is env/infra
    (no per-test state mutation);
  * full-suite red set via collect_failures; run-level failure => one retry =>
    infra-flake run;
  * step-change branch (new reds > 5) => one suite-event, skip per-test
    isolation (the SHA diff answers what 40 isolation re-runs cannot);
  * otherwise per red: isolation re-run -> four-way classification (L5);
  * registry transitions: recovered (green >=2), unstable/parked (>=2 flips).

WHY ISOLATION IS VALID HERE (though #866 rejected it at the gate): the gate
must attribute a failure to a *diff*, and isolation can't tell a pre-existing
flake from diff-introduced pollution. The guardian runs at ONE SHA with no
diff question — "passes alone vs fails alone" is exactly the classification
sought (spec L6). Stated here and in the PR body so Mirror doesn't bounce it
against the #866 design comment.

The suite-run/isolation I/O is behind an injectable ``invoker`` so the
classification/registry logic is unit-testable with a fake (no real worktree
or lock). ``DefaultInvoker`` is the production path built on the reused
``test_regression_check`` primitives.

CLI:
    python3 scripts/main_suite_guardian.py [--mode shadow|propose]
        [--test-sha <SHA>] [--repo-root <PATH>] [--registry <PATH>]

Stdlib + repo siblings only (test_regression_check, regression_baseline_cache,
atomic_io). Never mutates production state outside the registry it owns.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import atomic_io
import regression_baseline_cache as baseline_cache
import suite_guardian_ledger as ledger
import test_regression_check as trc


# --- constants ---------------------------------------------------------------

def _agents_root() -> Path:
    """~/agents, honoring OURLIBERTY_AGENTS_ROOT (test isolation)."""
    override = os.environ.get('OURLIBERTY_AGENTS_ROOT')
    return Path(override) if override else Path.home() / 'agents'


def default_registry_path() -> Path:
    return _agents_root() / 'state' / 'main-suite-guardian.json'


# Per-isolation-test timeout and total wall cap (L7). The wrapper enforces the
# hard wall kill; these bound the in-process budget.
PER_ISOLATION_TIMEOUT_S = 120
TOTAL_WALL_CAP_S = 5400  # 90 min
HISTORY_MAX = 20
STEP_CHANGE_THRESHOLD = 5  # new reds > 5 in one run => step-change branch

# 2 known-hermetic sentinel tests (D1.2). No credentials, no shared-state reads,
# no network — if these fail alone the environment is broken, not the code.
CANARY_TESTS = (
    'test_atomic_io.TestAtomicWrite.test_write_bytes_roundtrip',
    'test_bootstrap_first_import.BootstrapFirstImportTest.'
    'test_every_test_file_imports_bootstrap_first',
)

# Classification labels (L5). Never conflated.
CLS_ORDER_FLAKE = 'order-flake'
CLS_ENV_FAIL = 'env-fail'
CLS_GENUINE_BREAK = 'genuine-break'
CLS_INFRA_FLAKE = 'infra-flake'
CLS_BACKLOG = 'backlog'
CLS_RECOVERED = 'recovered'
CLS_UNSTABLE = 'unstable'

# Run-result labels stored in registry _meta.last_run_result.
RUN_GREEN = 'green'
RUN_RED = 'red'
RUN_INFRA_FLAKE = 'infra-flake'
RUN_CANARY_FAILED = 'canary-failed'
RUN_STEP_CHANGE = 'step-change'
RUN_SKIPPED = 'skipped'

# last_run_result values that mean "inconclusive" — a repeat SHA after one of
# these MUST retry (D1.1), or D2.5's consecutive-inconclusive logic can never
# advance. None (never run) is not a skip reason either.
_INCONCLUSIVE_RESULTS = frozenset({RUN_INFRA_FLAKE, RUN_CANARY_FAILED})

# Env-rot fingerprints in an isolation run's output. Screened BEFORE genuine-break
# (L5) so droplet venv rot / missing credentials never page as a code break.
# Case-insensitive substring match on the combined stdout+stderr.
_ENV_SIGNATURE_PATTERNS = (
    'modulenotfounderror',
    'no module named',
    'importerror',
    'skiptest',
    'venv',
    'virtualenv',
    'command not found',
    'no such file or directory',
    'permission denied',
    'eacces',
    'erofs',
    'environment variable',
    'supabase',
    'telegram',
    'anthropic_api_key',
    'service-role',
)


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


# --- pure classification helpers (unit-tested directly) ----------------------

def has_env_signature(output: str) -> bool:
    """True iff an isolation run's output carries an env-rot fingerprint
    (import/skip/venv/credential/permission). Screened before genuine-break."""
    low = (output or '').lower()
    return any(sig in low for sig in _ENV_SIGNATURE_PATTERNS)


def classify_red(
    *,
    passed_alone: bool,
    output: str,
    previously_green: bool,
    canary_ok: bool = True,
) -> str:
    """Four-way classification (L5), never conflated.

    order: canary -> passes-alone -> env-signature -> previously-green.
    A fails-alone red with no env signature that was NOT previously green is
    backlog debt (inherited), never a break.
    """
    if not canary_ok:
        return CLS_ENV_FAIL
    if passed_alone:
        return CLS_ORDER_FLAKE
    if has_env_signature(output):
        return CLS_ENV_FAIL
    if previously_green:
        return CLS_GENUINE_BREAK
    return CLS_BACKLOG


def new_registry() -> dict:
    return {
        '_meta': {
            'last_sha': None,
            'last_run_result': None,
            'completed_runs': 0,
            'last_run_at': None,
        },
        'tests': {},
    }


def load_registry(path: Path) -> dict:
    try:
        raw = path.read_text(encoding='utf-8')
    except (OSError, FileNotFoundError):
        return new_registry()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # A corrupt registry must not wedge the guardian — start fresh; the
        # nightly full-suite run rebuilds the standing-red picture in one cycle.
        return new_registry()
    if not isinstance(data, dict) or 'tests' not in data or '_meta' not in data:
        return new_registry()
    return data


def save_registry(path: Path, registry: dict) -> None:
    atomic_io.atomic_write_json(path, registry, indent=2, sort_keys=True,
                               trailing_newline=True)


def should_skip_sha(sha: str, registry: dict) -> bool:
    """Skip when the pinned SHA equals last run's, UNLESS the last run was
    inconclusive (D1.1). Identical code yields identical information, so a
    red-main night must not re-burn the budget — but an infra-flake/canary
    failure must retry."""
    meta = registry.get('_meta', {})
    if meta.get('last_sha') != sha:
        return False
    return meta.get('last_run_result') not in _INCONCLUSIVE_RESULTS and \
        meta.get('last_run_result') is not None


def is_previously_green(
    test_id: str, registry: dict, completed_runs_before: int,
) -> bool:
    """True iff ``test_id`` was absent from the red set of >=1 prior COMPLETED
    run (L5 definition of previously-green).

    Derivable from the registry: an existing entry that has ever been green
    (``ever_green``) qualifies. A test with NO entry that is red for the first
    time qualifies iff the guardian has completed >=1 prior run — a full-suite
    run means absence-from-registry == passed-in-every-prior-run. On the
    guardian's first-ever completed run (completed_runs_before == 0) a brand-new
    red is backlog debt, never a break.
    """
    entry = registry.get('tests', {}).get(test_id)
    if entry is not None:
        return bool(entry.get('ever_green'))
    return completed_runs_before >= 1


def select_new_reds(red_set: set, registry: dict) -> set:
    """Reds that were NOT red as of the previous run — used for the step-change
    branch. A test is "was red last run" iff its entry has consecutive_red_runs
    > 0; a missing entry (or one green last run) counts as new."""
    tests = registry.get('tests', {})
    new = set()
    for tid in red_set:
        entry = tests.get(tid)
        if entry is None or entry.get('consecutive_red_runs', 0) == 0:
            new.add(tid)
    return new


def is_step_change(new_reds: set) -> bool:
    return len(new_reds) > STEP_CHANGE_THRESHOLD


# --- registry transitions ----------------------------------------------------

def _append_history(entry: dict, *, sha: str, result: str,
                    classification: str, now_iso: str) -> None:
    hist = entry.setdefault('history', [])
    hist.append({
        'ts': now_iso, 'sha': sha, 'result': result,
        'classification': classification,
    })
    if len(hist) > HISTORY_MAX:
        del hist[:-HISTORY_MAX]


def record_red(
    registry: dict, test_id: str, classification: str, *,
    sha: str, now_iso: str, completed_runs_before: int,
) -> dict:
    """Create/update a test's registry entry for a red observation and return it.

    On a category change from the prior classification, bump flip_count; at
    flip_count >= 2 the test becomes ``unstable`` and is parked (L9)."""
    tests = registry.setdefault('tests', {})
    entry = tests.get(test_id)
    if entry is None:
        entry = {
            'classification': classification,
            'first_seen': now_iso,
            'last_seen': now_iso,
            'consecutive_red_runs': 1,
            'consecutive_green_runs': 0,
            'flip_count': 0,
            'last_sha': sha,
            'ever_green': False,
            'origin_run': completed_runs_before,
            'parked': False,
            'history': [],
        }
        tests[test_id] = entry
        _append_history(entry, sha=sha, result=RUN_RED,
                        classification=classification, now_iso=now_iso)
        return entry

    prev_cls = entry.get('classification')
    if prev_cls != classification and prev_cls not in (
        None, CLS_RECOVERED, CLS_UNSTABLE,
    ):
        entry['flip_count'] = entry.get('flip_count', 0) + 1
    entry['classification'] = classification
    entry['consecutive_red_runs'] = entry.get('consecutive_red_runs', 0) + 1
    entry['consecutive_green_runs'] = 0
    entry['last_seen'] = now_iso
    entry['last_sha'] = sha

    if entry['flip_count'] >= 2:
        entry['classification'] = CLS_UNSTABLE
        entry['parked'] = True

    _append_history(entry, sha=sha, result=RUN_RED,
                    classification=entry['classification'], now_iso=now_iso)
    return entry


def record_green(registry: dict, test_id: str, *, sha: str, now_iso: str) -> None:
    """Update an existing (previously-red) entry for a green observation.
    Green >=2 consecutive runs => ``recovered`` (digest note, never carded)."""
    entry = registry.get('tests', {}).get(test_id)
    if entry is None:
        return
    entry['consecutive_green_runs'] = entry.get('consecutive_green_runs', 0) + 1
    entry['consecutive_red_runs'] = 0
    entry['ever_green'] = True
    entry['last_seen'] = now_iso
    entry['last_sha'] = sha
    if entry['consecutive_green_runs'] >= 2 and not entry.get('parked'):
        entry['classification'] = CLS_RECOVERED
    _append_history(entry, sha=sha, result=RUN_GREEN,
                    classification=entry['classification'], now_iso=now_iso)


# --- the invoker: injectable suite-run/isolation I/O -------------------------

class DefaultInvoker:
    """Production suite-run/isolation engine built on test_regression_check.

    Provisions ONE detached worktree at the pinned SHA (the sync timer cannot
    move code under it), builds the sandbox env once, runs canary + full suite +
    per-test isolation there, then scans the real tree for this run's sentinel
    (the outside-jail tripwire) on teardown. Every suite-scale run is expected
    to hold the relocated warmer single-flight lock (acquired by the wrapper).
    """

    def __init__(
        self, repo_root: Path, *,
        test_sha: Optional[str] = None,
        suite_timeout_s: int = trc.DEFAULT_TIMEOUT_PER_SHA_S,
        isolation_timeout_s: int = PER_ISOLATION_TIMEOUT_S,
        canary_tests: tuple = CANARY_TESTS,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.test_sha = test_sha
        self.suite_timeout_s = suite_timeout_s
        self.isolation_timeout_s = isolation_timeout_s
        self.canary_tests = canary_tests
        self._tmp_parent: Optional[Path] = None
        self._worktree: Optional[Path] = None
        self._env: Optional[dict] = None
        self._sentinel: Optional[str] = None
        self._run_start: float = 0.0
        self._sha: Optional[str] = None

    def resolve_sha(self) -> str:
        """Fetch origin/main and pin one resolved SHA (or resolve --test-sha)."""
        if self.test_sha:
            return trc.resolve_sha(self.test_sha, self.repo_root)
        try:
            subprocess.run(
                ['git', '-C', str(self.repo_root), 'fetch', '--quiet',
                 'origin', 'main'],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise trc.AnalysisError(f'git fetch origin main failed: {exc}') from exc
        return trc.resolve_sha('origin/main', self.repo_root)

    def setup(self, sha: str) -> None:
        self._sha = sha
        self._tmp_parent = Path(tempfile.mkdtemp(prefix='main-suite-guardian-'))
        isolated_root = self._tmp_parent / f'agents-root-{sha[:12]}'
        for sub in ('logs', 'state', 'blackboard', 'inboxes', 'outboxes'):
            (isolated_root / sub).mkdir(parents=True, exist_ok=True)
        self._env = trc.build_sandbox_env(isolated_root)
        self._sentinel = self._env['OURLIBERTY_TEST_RUN_SENTINEL']
        self._worktree = self._tmp_parent / f'guardian-wt-{sha[:12]}'
        trc.add_worktree(self.repo_root, sha, self._worktree)
        self._run_start = time.time()

    def run_canary(self) -> tuple[bool, str]:
        details = []
        for tid in self.canary_tests:
            passed, output = trc.run_single_test_in_dir(
                self._worktree, tid, self._env, self.isolation_timeout_s,
            )
            if not passed:
                details.append(f'{tid}: FAILED')
        return (not details), '; '.join(details)

    def collect_failures(self) -> set:
        return trc.run_tests_in_dir(
            self._worktree, self.suite_timeout_s, env=self._env,
        )

    def run_single(self, test_id: str) -> tuple[bool, str]:
        return trc.run_single_test_in_dir(
            self._worktree, test_id, self._env, self.isolation_timeout_s,
        )

    def store_green_baseline(self, sha: str) -> None:
        """D1.7 hygiene: on a green, canary-clean full run, store an empty
        baseline for this SHA's content key. Best-effort — never raises."""
        try:
            key = baseline_cache.content_key(sha, self.repo_root) or sha
            baseline_cache.store(key, set(), source_commit=sha)
        except Exception:  # noqa: BLE001 — hygiene, not on the critical path
            pass

    def teardown(self) -> None:
        try:
            if self._sentinel:
                trc.scan_real_tree_for_sentinel(
                    self._sentinel,
                    since_mtime=self._run_start - trc._TRIPWIRE_MTIME_MARGIN_S,
                )
        finally:
            if self._worktree is not None:
                trc.remove_worktree(self.repo_root, self._worktree)
            if self._tmp_parent is not None:
                import shutil
                shutil.rmtree(self._tmp_parent, ignore_errors=True)


# --- orchestrator ------------------------------------------------------------

def run_guardian(
    repo_root,
    *,
    mode: str = 'shadow',
    invoker=None,
    registry_path=None,
    now: Optional[datetime] = None,
    test_sha: Optional[str] = None,
) -> dict:
    """Run one guardian cycle: pin SHA -> canary -> collect -> classify ->
    persist registry. Returns a result dict describing the run.

    ``invoker`` (injectable) supplies the suite-run/isolation I/O; when omitted
    a ``DefaultInvoker`` is built. Detection-and-record only in D1/shadow —
    cards/dispatch land in D2/D3.
    """
    repo_root = Path(repo_root)
    registry_path = Path(registry_path) if registry_path else default_registry_path()
    now_iso = _now_iso(now)
    if invoker is None:
        invoker = DefaultInvoker(repo_root, test_sha=test_sha)

    registry = load_registry(registry_path)
    meta = registry['_meta']

    sha = invoker.resolve_sha()

    if should_skip_sha(sha, registry):
        # Skip inherits the prior run's result for all streak counters — we do
        # not touch the registry (D1.1). Return without mutating state.
        return {
            'status': RUN_SKIPPED, 'sha': sha, 'mode': mode,
            'reason': 'sha unchanged since last conclusive run',
        }

    completed_runs_before = int(meta.get('completed_runs', 0))

    invoker.setup(sha)
    try:
        canary_ok, canary_detail = invoker.run_canary()
        if not canary_ok:
            meta['last_sha'] = sha
            meta['last_run_result'] = RUN_CANARY_FAILED
            meta['last_run_at'] = now_iso
            save_registry(registry_path, registry)
            return {
                'status': RUN_CANARY_FAILED, 'sha': sha, 'mode': mode,
                'canary_detail': canary_detail,
            }

        # Full suite with one retry on a run-level failure (D1.3).
        try:
            red_set = invoker.collect_failures()
        except trc.AnalysisError:
            try:
                red_set = invoker.collect_failures()
            except trc.AnalysisError as exc:
                meta['last_sha'] = sha
                meta['last_run_result'] = RUN_INFRA_FLAKE
                meta['last_run_at'] = now_iso
                save_registry(registry_path, registry)
                return {
                    'status': RUN_INFRA_FLAKE, 'sha': sha, 'mode': mode,
                    'detail': str(exc),
                }

        red_set = set(red_set)

        # Step-change branch: a mass break is triaged by SHA range, not by 40
        # isolation re-runs (D1.4). Suspend per-test bookkeeping this cycle.
        # Gated on completed_runs_before >= 1: on the first-ever completed run
        # every red is "new" (no prior entry), so a standing backlog (~13 env
        # fails per the L5 scar) would trip step-change and return without
        # cataloguing — leaving completed_runs stuck at 0 and repeating forever.
        # Per L5 a red at first-ever observation is backlog debt, never a break,
        # so the first completed run always does full per-test cataloguing.
        new_reds = select_new_reds(red_set, registry)
        if completed_runs_before >= 1 and is_step_change(new_reds):
            meta['last_sha'] = sha
            meta['last_run_result'] = RUN_STEP_CHANGE
            meta['last_run_at'] = now_iso
            save_registry(registry_path, registry)
            return {
                'status': RUN_STEP_CHANGE, 'sha': sha, 'mode': mode,
                'new_reds': sorted(new_reds), 'red_count': len(red_set),
            }

        # Per-red isolation + classification.
        classifications: dict[str, str] = {}
        for tid in sorted(red_set):
            passed_alone, output = invoker.run_single(tid)
            prev_green = is_previously_green(tid, registry, completed_runs_before)
            cls = classify_red(
                passed_alone=passed_alone, output=output,
                previously_green=prev_green, canary_ok=True,
            )
            entry = record_red(
                registry, tid, cls, sha=sha, now_iso=now_iso,
                completed_runs_before=completed_runs_before,
            )
            classifications[tid] = entry['classification']

        # Greens: any tracked (previously-red) test not red this run.
        recovered = []
        for tid in list(registry.get('tests', {}).keys()):
            if tid in red_set:
                continue
            entry = registry['tests'][tid]
            if entry.get('consecutive_red_runs', 0) == 0 and \
                    entry.get('classification') in (CLS_RECOVERED, CLS_UNSTABLE):
                continue  # already settled; nothing new to record
            record_green(registry, tid, sha=sha, now_iso=now_iso)
            if registry['tests'][tid].get('classification') == CLS_RECOVERED:
                recovered.append(tid)

        # D1.7 hygiene: green + canary-clean full run => store empty baseline.
        if not red_set:
            try:
                invoker.store_green_baseline(sha)
            except Exception:  # noqa: BLE001
                pass

        meta['last_sha'] = sha
        meta['last_run_result'] = RUN_GREEN if not red_set else RUN_RED
        meta['completed_runs'] = completed_runs_before + 1
        meta['last_run_at'] = now_iso
        save_registry(registry_path, registry)

        return {
            'status': RUN_GREEN if not red_set else RUN_RED,
            'sha': sha, 'mode': mode,
            'red_count': len(red_set),
            'classifications': classifications,
            'recovered': sorted(recovered),
        }
    finally:
        invoker.teardown()


# --- D2 proposal loop: propose -> approve -> dispatch (PR-2) ------------------
#
# WHAT THIS ADDS (shadow -> propose): the guardian already classifies standing
# reds (above). In propose mode it now drives them to zero through the existing
# propose->approve->dispatch fabric, without ever paging Larry for routine work:
#
#   * ONE decision per run (L1): every new actionable finding this cycle — new
#     order-flakes, a genuine-break episode, a suite-event — is batched into a
#     SINGLE pending-approval entry (id `suite-guardian-run-<date>`, chat_id=0,
#     bare_approvable=False) + one approval_request chain_event for the dashboard
#     Approvals tab. Never a per-finding DM.
#   * An FYI signal card (needs_larry=False) mirrors the run to the "what the
#     team did on its own" surface — informational, never a DECIDE-lane item.
#   * The outcome ledger (suite_guardian_ledger) measures each proposal's full
#     window: approve keeps it open for the serial drain; reject parks it (L9,
#     never re-proposed); a dispatched fix resolves off the OBSERVABLE (victim
#     green >=2 runs AND its named poison-injection test present + passing),
#     regardless of merge provenance; an approved-but-dead obligation ages out.
#   * Serial drain (D2.1): at most OPEN_FIX_CAP fix obligations in flight; the
#     next dispatches as one resolves.
#   * Edge-triggered escalation (D2.3): a genuine break DMs Larry ONCE per
#     episode at 2 consecutive reds (a new episode only after the victim returns
#     green) — the only path in this loop that pages.
#
# Every side effect that cannot run under the test jail (approval writes,
# chain_event emit, the FYI card, the escalation DM, the fix dispatch, and the
# source-grep that decides "poison test present") is an injectable dependency
# (`ProposalDeps`), mirroring PR-1's injectable ``invoker``. The state-file ops
# (registry + ledger) hit tmp files naturally under _bootstrap's redirection, so
# the whole cycle is unit-testable without a real worktree, Supabase, or DM.

PROPOSAL_TARGET_REPO = 'ourliberty-agent-core'
PROPOSAL_KIND = 'suite-guardian-proposal'
FYI_CARD_KEY = 'suite-guardian:run'

# Classifications the guardian proposes a fix for. order-flake = test-order
# pollution (a scripts/tests/** isolation fix); genuine-break = a real
# regression. env-fail/backlog/infra-flake are NOT code the guardian can fix,
# and unstable/recovered/parked are terminal — none are ever proposed.
_ACTIONABLE_CLASSIFICATIONS = frozenset({CLS_ORDER_FLAKE, CLS_GENUINE_BREAK})

# Edge-triggered escalation fires at this many consecutive reds (D2.3).
_GENUINE_BREAK_ESCALATE_AFTER = 2


def _run_entry_task_id(now: Optional[datetime] = None) -> str:
    """The batched pending-approval id for a run — one per calendar day (the
    guardian runs nightly), so ``find_by_id_any_state`` dedups a same-day retry."""
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f'suite-guardian-run-{dt.strftime("%Y-%m-%d")}'


def _slug(test_id: str) -> str:
    """A grep-safe identifier fragment derived from a victim test_id."""
    slug = re.sub(r'[^0-9a-zA-Z]+', '_', test_id or '').strip('_').lower()
    return slug or 'unknown'


def poison_test_name(test_id: str) -> str:
    """The named poison-injection test that proves a fix holds (D2.4). A unique,
    grep-able symbol under scripts/tests/**: its presence + passing is the
    observable half of ledger resolution."""
    return f'test_poison_{_slug(test_id)}'


def build_fix_task_prompt(
    test_id: str, classification: str, poison_test_name_: str,
) -> str:
    """The fix-task template Forge receives when a proposal is dispatched. Names
    the poison-injection test explicitly and constrains its scope to
    scripts/tests/** (D2.4)."""
    if classification == CLS_ORDER_FLAKE:
        cause = (
            'This test is an ORDER-FLAKE: it fails in the full suite but passes '
            'in isolation, so some other test leaks shared state into it. Find '
            'the polluter and make the isolation deterministic (fixture teardown '
            '/ per-test tmp state), not by reordering or skipping.'
        )
    else:
        cause = (
            'This test is a GENUINE-BREAK: it was previously green and now fails '
            'in isolation with no environment signature — a real regression. Fix '
            'the underlying cause so the test passes alone AND in the full suite.'
        )
    return (
        f'Main-Suite Green Guardian fix task for `{test_id}`.\n\n'
        f'{cause}\n\n'
        f'REQUIRED: add a named poison-injection regression test '
        f'`{poison_test_name_}` under scripts/tests/** that reproduces the '
        f'failure mode and would fail if this fix regresses. The guardian '
        f'resolves this obligation only once `{test_id}` is green for >=2 '
        f'consecutive nightly runs AND `{poison_test_name_}` is present and '
        f'passing at main. Keep the poison test itself scoped to '
        f'scripts/tests/** only.'
    )


def should_escalate_break(entry: dict) -> bool:
    """Pure edge-trigger (D2.3): a genuine break at >=2 consecutive reds that has
    not already been escalated this episode. ``break_escalated`` is reset when the
    victim returns green (a new episode), so a standing break pages exactly once."""
    return bool(
        entry.get('classification') == CLS_GENUINE_BREAK
        and entry.get('consecutive_red_runs', 0) >= _GENUINE_BREAK_ESCALATE_AFTER
        and not entry.get('break_escalated')
    )


def select_actionable(
    run_result: dict, registry: dict, *, ledger_path: Optional[Path] = None,
) -> list[dict]:
    """The new actionable findings to propose fixes for THIS run. Pure over the
    run result + registry + ledger state. Filters out anything already tracked:
    a test with a live (proposed/approved) or parked ledger row is skipped
    (dedup + L9), while a test with no row — or a terminal non-parked row whose
    break recurred — is proposed. Ordered deterministically by test_id."""
    lp = ledger_path or ledger.default_ledger_path()
    classifications = run_result.get('classifications') or {}
    tests = registry.get('tests', {})
    out: list[dict] = []
    for tid in sorted(classifications):
        cls = classifications[tid]
        if cls not in _ACTIONABLE_CLASSIFICATIONS:
            continue
        if tests.get(tid, {}).get('parked'):
            continue
        row = ledger.get(tid, path=lp)
        if isinstance(row, dict):
            # Live obligation (awaiting decision or in the fix pipeline) — do not
            # re-propose. A PARKED row is never re-proposed (L9). Only a terminal
            # non-parked row (resolved/abandoned) whose break recurred is eligible.
            if row.get('status') == ledger.OPEN:
                continue
            if row.get('decision') == ledger.DEC_PARKED:
                continue
        out.append({
            'test_id': tid,
            'classification': cls,
            'poison_test_name': poison_test_name(tid),
        })
    return out


# --- injectable side-effect dependencies -------------------------------------

def _prod_lookup_decision(run_task_id: Optional[str]) -> Optional[str]:
    """Production decision reader: map an approval entry's resolved status back to
    the ledger's decision vocabulary. 'pending' -> undecided (None-ish), an
    approve/modify -> 'approved', a reject/expire -> 'rejected'. A missing entry
    reads as undecided (the batch may not have been surfaced yet)."""
    if not run_task_id:
        return None
    try:
        import beacon_approval_handler as ah
        entry = ah.find_by_id_any_state(run_task_id)
    except Exception:  # noqa: BLE001 — decision read is best-effort
        return None
    if not isinstance(entry, dict):
        return None
    status = entry.get('status')
    if status in ('approved', 'modified'):
        return 'approved'
    if status in ('rejected', 'expired'):
        return 'rejected'
    return None  # 'pending' or unknown — still awaiting Larry


def _prod_escalate(*, test_id: str, entry: dict) -> None:
    import larry_alerts as la
    la.append_alert(
        source='suite-guardian',
        severity='critical',
        message=(
            f'Guardian: genuine regression standing red for '
            f'{entry.get("consecutive_red_runs", 0)} consecutive runs: {test_id}. '
            f'Isolation-reproducible, no env signature — this is a real break.'
        ),
        subject=test_id,
        route='escalate',
        needs_larry=True,
    )


def _prod_dispatch_fix(
    repo_root: Path,
    *, test_id: str, classification: str, poison_test_name_: str,
    prompt: str, now: datetime,
) -> Optional[str]:
    """Land one approved fix task in Forge's inbox. Returns the fix task_id on a
    successful write, None on any rejection/failure (the drain retries next run)."""
    dt = now.astimezone(timezone.utc)
    fix_task_id = f'suite-guardian-fix-{_slug(test_id)}-{dt.strftime("%Y%m%d")}'
    task = {
        'task_id': fix_task_id,
        'source': 'beacon',
        'target_agent': 'forge',
        'target_repo': PROPOSAL_TARGET_REPO,
        'task_type': 'code',
        'pr_title': f'fix(suite-guardian): drive standing red to green — {test_id}',
        'summary': f'Guardian fix for {test_id} ({classification}).',
        'prompt': prompt,
    }
    try:
        import safe_write_inbox
        safe_write_inbox.safe_write_inbox(
            'forge', task, 'beacon', f'{fix_task_id}.json',
        )
    except Exception:  # noqa: BLE001 — a failed dispatch stays undispatched
        return None
    return fix_task_id


def _prod_poison_present(repo_root: Path, poison_test_name_: Optional[str]) -> bool:
    """Observable half of resolution: is the named poison test present in the
    source tree? A source-grep of scripts/tests/** for the symbol. Best-effort;
    a scan error reads as absent (keeps the obligation open rather than
    falsely resolving)."""
    if not poison_test_name_:
        return False
    tests_dir = Path(repo_root) / 'scripts' / 'tests'
    try:
        for py in tests_dir.rglob('test_*.py'):
            try:
                if poison_test_name_ in py.read_text(encoding='utf-8'):
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


@dataclass
class ProposalDeps:
    """Injectable side-effect surface for the propose loop. Every field is a
    callable; production wiring is filled by :func:`default_proposal_deps`, and
    tests pass fakes that record calls and hit tmp state files."""

    add_pending: Callable[[dict, int], dict]
    find_pending: Callable[[str], Optional[dict]]
    emit_approval_request: Callable[[dict], bool]
    lookup_decision: Callable[[Optional[str]], Optional[str]]
    upsert_card: Callable[[str, dict], None]
    resolve_card: Callable[[str], bool]
    escalate: Callable[..., None]
    dispatch_fix: Callable[..., Optional[str]]
    poison_present: Callable[[Optional[str]], bool]


def default_proposal_deps(
    repo_root: Path, *, chat_id: int = 0,
) -> ProposalDeps:
    """Production dependency wiring — lazy imports so the module loads clean under
    the test jail (larry_alerts/safe_write_inbox refuse-under-test)."""

    def _add_pending(payload: dict, cid: int) -> dict:
        import beacon_approval_handler as ah
        return ah.add_pending(payload, cid)

    def _find_pending(task_id: str) -> Optional[dict]:
        import beacon_approval_handler as ah
        return ah.find_by_id_any_state(task_id)

    def _emit_approval_request(payload: dict) -> bool:
        import beacon_approval_handler as ah
        import chain_event_emit as ce
        return ce.emit_event(**ah.build_approval_request_chain_event(payload))

    def _upsert_card(key: str, record: dict) -> None:
        import for_larry_signal as fls
        fls.upsert_record(key, record)

    def _resolve_card(key: str) -> bool:
        import for_larry_signal as fls
        return fls.resolve_record(key)

    def _dispatch(**kw) -> Optional[str]:
        return _prod_dispatch_fix(repo_root, **kw)

    def _poison(name: Optional[str]) -> bool:
        return _prod_poison_present(repo_root, name)

    return ProposalDeps(
        add_pending=_add_pending,
        find_pending=_find_pending,
        emit_approval_request=_emit_approval_request,
        lookup_decision=_prod_lookup_decision,
        upsert_card=_upsert_card,
        resolve_card=_resolve_card,
        escalate=_prod_escalate,
        dispatch_fix=_dispatch,
        poison_present=_poison,
    )


def _build_batch_summary(proposals: list[dict]) -> str:
    lines = [
        'Main-Suite Green Guardian — proposed fixes for this run.',
        f'{len(proposals)} standing red(s) worth a fix task:',
    ]
    for p in proposals:
        lines.append(f'  • {p["test_id"]} [{p["classification"]}]')
    lines.append(
        'Approve to let the guardian dispatch these (serial-drained, <=3 in '
        'flight); reject to park them (never re-proposed).'
    )
    return '\n'.join(lines)


def run_proposal_cycle(
    repo_root,
    run_result: dict,
    *,
    registry_path=None,
    ledger_path=None,
    deps: Optional[ProposalDeps] = None,
    now: Optional[datetime] = None,
    chat_id: int = 0,
) -> dict:
    """Drive the propose->approve->dispatch loop for one guardian run. Reads the
    registry + ledger, reconciles prior decisions, resolves/ages-out obligations,
    serial-drains approved fixes, batches new actionable findings into ONE pending
    entry (+ FYI card), and edge-triggers escalation. Returns a summary dict of
    what changed. Never raises into the nightly timer — each side effect is
    guarded, and the pure ledger/registry ops are fail-safe."""
    repo_root = Path(repo_root)
    registry_path = Path(registry_path) if registry_path else default_registry_path()
    lp = Path(ledger_path) if ledger_path else ledger.default_ledger_path()
    if deps is None:
        deps = default_proposal_deps(repo_root, chat_id=chat_id)
    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = _now_iso(now)

    summary: dict = {
        'proposed': [], 'dispatched': [], 'resolved': [], 'parked': [],
        'abandoned': [], 'escalated': [], 'pending_entry': None,
    }

    registry = load_registry(registry_path)
    tests = registry.setdefault('tests', {})
    registry_dirty = False

    # 1. Reconcile prior proposals' decisions: approve keeps the obligation open
    #    for the serial drain; reject parks it (L9 — never re-proposed).
    for row in ledger.list_open(path=lp):
        if row.get('decision') != ledger.DEC_PROPOSED:
            continue
        decision = deps.lookup_decision(row.get('run_task_id'))
        if decision == 'approved':
            ledger.set_decision(row['test_id'], ledger.DEC_APPROVED, now=n, path=lp)
        elif decision == 'rejected':
            if ledger.set_decision(row['test_id'], ledger.DEC_PARKED, now=n, path=lp):
                summary['parked'].append(row['test_id'])

    # 2. Episode reset: a victim that is currently green clears its escalation
    #    latch, so a NEW break episode can page again (D2.3 edge-trigger).
    for tid, entry in tests.items():
        if entry.get('consecutive_red_runs', 0) == 0 and entry.get('break_escalated'):
            entry['break_escalated'] = False
            registry_dirty = True

    # 3. Observable-based resolution of dispatched fixes (D2.6): victim green >=2
    #    runs AND the named poison test present + passing, regardless of merge
    #    provenance.
    for row in ledger.list_open(path=lp):
        if not row.get('fix_task_id'):
            continue
        entry = tests.get(row['test_id'], {})
        green_streak = int(entry.get('consecutive_green_runs', 0))
        present = bool(deps.poison_present(row.get('poison_test_name')))
        if ledger.record_observation(
            row['test_id'], green_streak=green_streak, poison_present=present,
            now=n, path=lp,
        ):
            summary['resolved'].append(row['test_id'])

    # 4. Abandoned age-out: an approved-but-dead obligation terminates (D2.6).
    summary['abandoned'] = ledger.age_out_abandoned(now=n, path=lp)

    # 5. Serial drain: dispatch approved, not-yet-dispatched fixes up to the cap.
    for row in ledger.dispatchable_fixes(cap=ledger.OPEN_FIX_CAP, path=lp):
        tid = row['test_id']
        cls = tests.get(tid, {}).get('classification', CLS_GENUINE_BREAK)
        pname = row.get('poison_test_name') or poison_test_name(tid)
        prompt = build_fix_task_prompt(tid, cls, pname)
        fix_task_id = deps.dispatch_fix(
            test_id=tid, classification=cls, poison_test_name_=pname,
            prompt=prompt, now=n,
        )
        if fix_task_id and ledger.mark_dispatched(tid, fix_task_id, now=n, path=lp):
            summary['dispatched'].append(tid)

    # 6. New proposals -> ONE pending entry per run (L1) + one approval_request.
    actionable = select_actionable(run_result, registry, ledger_path=lp)
    if actionable:
        run_task_id = _run_entry_task_id(n)
        proposals = []
        for item in actionable:
            ledger.open_proposal(
                item['test_id'], run_task_id=run_task_id,
                poison_test_name=item['poison_test_name'], now=n, path=lp,
            )
            proposals.append(item)
            summary['proposed'].append(item['test_id'])
        # Dedup the surfaced decision: emit at most once per run id (a same-day
        # retry finds the existing entry and skips the re-emit + re-DM).
        if deps.find_pending(run_task_id) is None:
            payload = {
                'task_id': run_task_id,
                'summary': _build_batch_summary(proposals),
                'prompt': _build_batch_summary(proposals),
                'target_agent': 'forge',
                'target_repo': PROPOSAL_TARGET_REPO,
                'kind': PROPOSAL_KIND,
                'bare_approvable': False,
                'proposals': proposals,
            }
            deps.add_pending(payload, chat_id)
            deps.emit_approval_request(payload)
            summary['pending_entry'] = run_task_id

    # 7. Edge-triggered escalation for genuine breaks (the only paging path).
    for tid, entry in tests.items():
        if should_escalate_break(entry):
            deps.escalate(test_id=tid, entry=entry)
            entry['break_escalated'] = True
            registry_dirty = True
            summary['escalated'].append(tid)

    # 8. FYI signal card (needs_larry=False) — informational mirror, never a
    #    DECIDE-lane item. Self-clears on a quiet run.
    activity = (summary['proposed'] or summary['dispatched']
                or summary['resolved'] or summary['escalated']
                or summary['parked'] or summary['abandoned'])
    if activity:
        deps.upsert_card(FYI_CARD_KEY, {
            'needs_larry': False,
            'source': 'suite-guardian',
            'ts': now_iso,
            'summary': (
                f'Guardian run: {len(summary["proposed"])} proposed, '
                f'{len(summary["dispatched"])} dispatched, '
                f'{len(summary["resolved"])} resolved, '
                f'{len(summary["escalated"])} escalated.'
            ),
            'proposed': summary['proposed'],
            'dispatched': summary['dispatched'],
            'resolved': summary['resolved'],
            'escalated': summary['escalated'],
        })
    else:
        deps.resolve_card(FYI_CARD_KEY)

    if registry_dirty:
        save_registry(registry_path, registry)

    return summary


# --- CLI ---------------------------------------------------------------------

def _default_mode(repo_root: Path) -> str:
    """Read mode from config/suite-guardian.json; Stage 0/shadow on any error
    (L3: on unreadable config -> Stage 0)."""
    try:
        cfg = json.loads(
            (repo_root / 'config' / 'suite-guardian.json').read_text('utf-8')
        )
        mode = cfg.get('mode')
        if mode in ('shadow', 'propose'):
            return mode
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return 'shadow'


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog='main_suite_guardian')
    parser.add_argument('--repo-root', default=str(Path.home() / 'agent-core'))
    parser.add_argument('--mode', choices=('shadow', 'propose'), default=None)
    parser.add_argument('--test-sha', default=None,
                        help='pin a scratch SHA (acceptance test 3)')
    parser.add_argument('--registry', default=None)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    mode = args.mode or _default_mode(repo_root)

    try:
        result = run_guardian(
            repo_root, mode=mode, test_sha=args.test_sha,
            registry_path=args.registry,
        )
    except trc.AnalysisError as exc:
        print(f'main_suite_guardian: run failed: {exc}', file=sys.stderr)
        return 2

    # In propose mode a conclusive run drives the propose->approve->dispatch loop
    # (D2). Shadow mode detects + records only. A skipped/inconclusive run carries
    # no per-test verdict, so there is nothing to propose or drain this cycle.
    if mode == 'propose' and result.get('status') in (RUN_GREEN, RUN_RED):
        try:
            proposal = run_proposal_cycle(
                repo_root, result, registry_path=args.registry,
            )
            result['proposal'] = proposal
        except Exception as exc:  # noqa: BLE001 — the loop must never wedge the timer
            print(f'main_suite_guardian: proposal cycle failed: {exc}',
                  file=sys.stderr)

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
