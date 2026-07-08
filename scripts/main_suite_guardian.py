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
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import atomic_io
import regression_baseline_cache as baseline_cache
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
        new_reds = select_new_reds(red_set, registry)
        if is_step_change(new_reds):
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

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
