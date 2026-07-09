"""_bootstrap.py — the SINGLE SOURCE OF TRUTH for the test-run sandbox.

WHY THIS FILE EXISTS
--------------------
scripts/tests/__init__.py used to hold the sandbox setup (the OURLIBERTY_*
env redirects + the #428 atexit live-write tripwire). That only engaged when
the ``scripts.tests`` PACKAGE was imported — i.e. under
``python3 -m unittest scripts.tests.<module>`` (dotted) or pytest. But the
regression gate and every Forge/Mirror dev loop run

    python3 -m unittest discover -s scripts/tests

which imports each ``test_*.py`` as a TOP-LEVEL module and NEVER executes the
package ``__init__``. So under discover the sandbox never armed, and any test
that transitively reached a production log()/alert/state write hit the REAL
~/agents tree. That leak class survived PR #412, #428 and #436 and spammed
Larry with false ``transcript-not-persisted`` CRITICAL DMs (mock /tmp paths,
fixture quota events, even ``../../../../etc/pwned`` path-traversal fixture
lines in the live inbox_watcher.log).

The fix: move the canonical setup HERE and have EVERY ``test_*.py`` import this
module as its first import (loader-agnostic form). A top-level import engages
the sandbox before any other import can freeze a path constant — regardless of
which loader brought the test module in. ``__init__.py`` now delegates to this
module too, so the dotted/pytest path and the discover path share ONE source
of truth and cannot drift.

IDEMPOTENCY
-----------
``engage()`` may be called more than once per process — and, worse, from TWO
distinct module identities: under ``discover`` the test files import this as
the top-level ``_bootstrap``; under the dotted/pytest path ``__init__`` imports
it as ``scripts.tests._bootstrap``. A module-level flag would not be shared
across those two identities, so the guard is a PROCESS-GLOBAL env var. The
first engage wins; every later call (any identity) is a harmless no-op. This is
what keeps the #428 tripwire from double-instrumenting the log helpers or
registering two atexit scans that would double-fire ``os._exit``.
"""
import atexit
import os
import site
import sys
import tempfile
import time
import uuid

try:
    import pwd  # POSIX-only; used to find the REAL home regardless of $HOME.
except ImportError:  # pragma: no cover - non-POSIX
    pwd = None

# Process guards that survive the dual module identity described above (a
# module-level bool would not, because top-level `_bootstrap` and
# `scripts.tests._bootstrap` are two different module objects in sys.modules).
# They are keyed by PID, NOT a bare '1': env vars are inherited by child
# subprocesses, so a bare flag would make a spawned child's engage() no-op and
# never arm ITS OWN tripwire (the UnittestGateExitSelfCheck child does exactly
# this — it inherits the parent gate's env and must still arm). Keying by PID
# means "already engaged" holds only WITHIN the process that set it; a value
# inherited from a different PID reads as not-engaged, so the child re-engages
# and arms freshly.
_ENGAGE_GUARD = 'OURLIBERTY_TEST_BOOTSTRAP_ENGAGED'
_ARMED_GUARD = 'OURLIBERTY_TEST_BOOTSTRAP_TRIPWIRE_ARMED'

# Module attributes exposed for debugging / for __init__'s re-export. The
# authoritative state lives in the PID-keyed guards above; these mirror it for
# the identity that actually ran engage().
_RUNTIME_TRIPWIRE_ARMED = False
_RUNTIME_TRIPWIRE_SENTINEL = None


def _real_user_site_dirs():
    """The per-user site-packages directories of the REAL invoking user,
    resolved independently of ``$HOME``.

    pip ``--user`` installs (fastapi / uvicorn / httpx for the dashboard tests
    live here on this droplet) land in ``<home>/.local/lib/pythonX.Y/site-packages``.
    Python computes that path from ``$HOME`` at interpreter startup, so if the
    suite is invoked under an overridden/jailed ``$HOME`` — e.g. CI, or the
    scratch-HOME no-production-write tripwire — the real user-site silently
    drops off ``sys.path`` and ``import fastapi`` raises ModuleNotFoundError.

    We derive the home from the passwd database (``pwd``), which reflects the
    actual account and is immune to a ``$HOME`` override, and fall back to
    ``site.getusersitepackages()`` for the common (un-overridden) case."""
    dirs = []
    homes = []
    if pwd is not None:
        try:
            homes.append(pwd.getpwuid(os.getuid()).pw_dir)
        except (KeyError, OSError):
            pass
    ver = f'python{sys.version_info.major}.{sys.version_info.minor}'
    for home in homes:
        if home:
            dirs.append(os.path.join(home, '.local', 'lib', ver, 'site-packages'))
    try:
        dirs.append(site.getusersitepackages())
    except Exception:
        pass
    # De-dupe while preserving order.
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _preserve_user_site():
    """Keep the real user-site importable for this process AND for any test
    subprocess, regardless of a ``$HOME`` override later in the run.

    Prepended (ahead of the system ``dist-packages``) so it matches the normal
    pip ``--user`` precedence: e.g. the newer ``typing_extensions`` that fastapi
    /pydantic require lives in user-site and MUST win over the older copy in
    ``/usr/lib/python3/dist-packages``. Mirrored to the front of ``PYTHONPATH``
    so subprocess-spawning tests inherit the same ordering. Idempotent."""
    to_add = [d for d in _real_user_site_dirs()
              if os.path.isdir(d) and d not in sys.path]
    # Insert preserving the candidate order at the front of sys.path.
    for usersite in reversed(to_add):
        sys.path.insert(0, usersite)

    existing = os.environ.get('PYTHONPATH', '')
    parts = existing.split(os.pathsep) if existing else []
    new_parts = [d for d in _real_user_site_dirs()
                 if os.path.isdir(d) and d not in parts]
    if new_parts:
        os.environ['PYTHONPATH'] = os.pathsep.join(new_parts + parts)


def _is_this_process(var: str) -> bool:
    return os.environ.get(var) == str(os.getpid())


def is_armed() -> bool:
    """True if THIS PROCESS armed the runtime production-write tripwire. Reads
    the PID-keyed guard so the answer is correct from either module identity
    (top-level ``_bootstrap`` or ``scripts.tests._bootstrap``) and is NOT fooled
    by a guard value inherited from a parent process."""
    return _is_this_process(_ARMED_GUARD)


def run_sentinel():
    """The run sentinel stamped through the production log() helpers (or None
    under pytest before the conftest fixture mints its own)."""
    return os.environ.get('OURLIBERTY_TEST_RUN_SENTINEL')


def _arm_runtime_tripwire():
    """Mint the sentinel, instrument the production log() helpers, and register
    an atexit scan of the real ~/agents tree. Mirrors conftest's session
    fixture setup/teardown across the package-import / process-exit boundary.

    Fail-open by design: if the tripwire infrastructure can't be imported the
    gate still runs (degraded) — better than a bootstrap that hard-fails every
    test invocation."""
    global _RUNTIME_TRIPWIRE_ARMED, _RUNTIME_TRIPWIRE_SENTINEL

    here = os.path.dirname(os.path.abspath(__file__))
    scripts_dir = os.path.dirname(here)
    # The tripwire module is imported by bare name (as conftest does); its
    # instrument_log_helpers in turn imports agent_runner by bare name. Put BOTH
    # the tests dir and scripts/ on the path or instrumentation silently no-ops
    # (import fails -> nothing stamped -> the scan can never see a leak).
    for p in (here, scripts_dir):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        import test_no_production_writes_runtime as runtime
    except Exception:
        return

    sentinel = runtime.mint_sentinel()
    os.environ['OURLIBERTY_TEST_RUN_SENTINEL'] = sentinel
    session_start = time.time()
    undo = runtime.instrument_log_helpers(sentinel)
    _RUNTIME_TRIPWIRE_SENTINEL = sentinel
    _RUNTIME_TRIPWIRE_ARMED = True
    os.environ[_ARMED_GUARD] = str(os.getpid())

    def _scan_at_exit(runtime=runtime, sentinel=sentinel,
                      session_start=session_start, undo=undo):
        try:
            _, message = runtime.run_session_end_tripwire(
                sentinel, session_start, undo, runner='unittest gate',
            )
        except Exception as exc:
            # A scan bug must not red-flag a clean gate (the GateSelfCheck tests
            # cover scan correctness); warn and let the real exit code stand.
            print(f'[production-write-tripwire] scan skipped ({exc!r})',
                  file=sys.stderr)
            return
        if not message:
            return
        print(message, file=sys.stderr)
        # Force a non-zero gate exit. os._exit only fires on a CONFIRMED leak
        # (rare alarm), so the green path is untouched; flush first so a piped
        # capture (Mirror's gate) keeps the runner's already-printed results.
        # NB: the regression gate (scripts/test_regression_check.py) keys off
        # this non-zero exit and surfaces it as a synthetic failure — without
        # that, the exit code alone is swallowed by its FAIL/ERROR-line parser.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    atexit.register(_scan_at_exit)


def engage() -> None:
    """Establish the test sandbox AT IMPORT. Idempotent across calls and across
    the dual module identity (guarded by a process-global env flag)."""
    # KERNEL-ENFORCED WALL (scripts/test_isolation_wall) - re-exec THIS process
    # under a bwrap read-only bind over the real ~/agents + ~/agent-worktrees
    # BEFORE any other setup, so an ad-hoc ``python3 -m unittest`` run in a
    # Forge/Mirror dispatch worktree (the actual incident vector - a command
    # typed into a shell, with no code chokepoint) is *physically* unable to
    # write live state. This is the one place every test file is guaranteed to
    # reach first (the enforced bootstrap-first-import), and the mount namespace
    # is inherited by children, so this single wrap covers the whole test run
    # and everything it shells out to - closing the subprocess / C-level /
    # hardcoded-literal write classes the env redirects below structurally
    # cannot. Fail-open: skipped under pytest (conftest owns that path and
    # pytest is not the incident vector), when already walled, when the #820
    # live opt-out is set, or when bwrap is unavailable. On success os.execvp
    # does NOT return - the process is replaced by its walled self, which
    # re-enters engage() with the wall active (is_walled() short-circuits the
    # re-exec) and proceeds through the normal setup below.
    if "pytest" not in sys.modules:
        try:
            _here = os.path.dirname(os.path.abspath(__file__))
            _scripts_dir = os.path.dirname(_here)
            if _scripts_dir not in sys.path:
                sys.path.insert(0, _scripts_dir)
            import test_isolation_wall
            test_isolation_wall.reexec_under_wall()
        except Exception as _wall_exc:  # never let the wall break a test run
            print(
                f"[test-isolation-wall] setup skipped ({_wall_exc!r})",
                file=sys.stderr,
            )

    if _is_this_process(_ENGAGE_GUARD):
        return
    os.environ[_ENGAGE_GUARD] = str(os.getpid())

    # Keep pip --user packages importable even if the suite runs under an
    # overridden/jailed HOME (CI, or the scratch-HOME tripwire). Do this FIRST,
    # before any test triggers a fastapi import. This does NOT touch HOME — the
    # sandbox is built on OURLIBERTY_* env redirection (mirrored from conftest;
    # see test_conftest_init_parity.py), not a HOME swap.
    _preserve_user_site()

    # Production log redirection (Gap: 2026-05-27 live-log leak). Many daemons
    # resolve OURLIBERTY_LOG_DIR at write time; pin it to a tmp dir so an
    # un-mocked log() call lands in /tmp, not ~/agents/logs/. setdefault-style
    # (only when unset) so an outer harness / the conftest fixture still wins.
    if not os.environ.get('OURLIBERTY_LOG_DIR'):
        os.environ['OURLIBERTY_LOG_DIR'] = tempfile.mkdtemp(
            prefix='ourliberty-test-logs-',
        )

    # Import-time sandbox for the OURLIBERTY_*_ROOT family (Gap A). Production
    # modules bind
    #   AGENTS_ROOT = Path(os.environ.get('OURLIBERTY_AGENTS_ROOT', '/home/larry/agents'))
    # AT IMPORT; without an import-time default a test that imports such a
    # module and then triggers an inbox/blackboard/state write hits the REAL
    # ~/agents tree. FORCE-OVERRIDE (not setdefault): the 2026-07-02 tier-bench
    # outage happened because a child unittest INHERITED a live OURLIBERTY_AGENTS_ROOT
    # and setdefault refused to override it, so every writer resolved live. We now
    # unconditionally redirect to a fresh sandbox UNLESS the caller explicitly opts
    # out via OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT (the regression gate sets it — it
    # pins its own coordinated AGENTS_ROOT/LOG_DIR/WORKTREES tree and must not be
    # split-brained by a fresh mkdtemp here).
    if not os.environ.get('OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT'):
        _sandbox_agents_root = tempfile.mkdtemp(
            prefix='ourliberty-test-agents-root-')
        os.makedirs(os.path.join(_sandbox_agents_root, 'logs'), exist_ok=True)
        os.environ['OURLIBERTY_AGENTS_ROOT'] = _sandbox_agents_root
        os.environ['OURLIBERTY_WORKTREES_ROOT'] = os.path.join(
            _sandbox_agents_root, 'worktrees')

    # Test-isolation guard (2026-06-02 live-DB leak): block the chain-event
    # emitter from ever building a live Supabase client during a test run.
    # chain_event_emit._get_client() returns None when this is set.
    os.environ['OURLIBERTY_DISABLE_LIVE_EMIT'] = '1'

    # Hermetic `gh` (2026-07-08 gh-quota flake family — the PR #884 false
    # BLOCK): integration tests that drive production routing deep enough to
    # reach the gh read helpers (headRefOid dedup, mergeable prechecks,
    # terminal-PR-state, label lookups) shell out to the REAL gh CLI —
    # ~106 GraphQL-backed `gh pr view`/`gh pr list` calls per full-suite run,
    # none of them behind a mock. Those calls burn the shared account's
    # 5000/hr GraphQL quota out from under live automation (Mirror, healers)
    # AND couple test outcomes/timing to network + quota state: the
    # 2026-07-08 head run flaked with graphql remaining=0 while the parent
    # ran clean, a false BLOCK the #866 fresh-parent reverify cannot cover
    # when the HEAD run is the flaky one. Every production helper is
    # fail-open on gh errors (the full suite is green with gh hard-failing),
    # so the hermetic default is a PATH-shimmed `gh` that exits 1 with a
    # marker on stderr. PATH is inherited by children, so scripts spawned by
    # tests are covered too; tests that assert specific gh behavior keep
    # patching subprocess per-test on top (mocks intercept before PATH ever
    # resolves). Deliberate network tests can opt out per-invocation via
    # OURLIBERTY_ALLOW_REAL_GH=1.
    if not os.environ.get('OURLIBERTY_ALLOW_REAL_GH'):
        _gh_shim_dir = tempfile.mkdtemp(prefix='ourliberty-test-gh-shim-')
        _gh_shim = os.path.join(_gh_shim_dir, 'gh')
        with open(_gh_shim, 'w') as _fh:
            _fh.write(
                '#!/bin/sh\n'
                'echo "hermetic-test-guard: real gh blocked during test runs'
                ' (scripts/tests/_bootstrap.py; set OURLIBERTY_ALLOW_REAL_GH=1'
                ' to opt out)" >&2\n'
                'exit 1\n'
            )
        os.chmod(_gh_shim, 0o755)
        os.environ['PATH'] = (
            _gh_shim_dir + os.pathsep + os.environ.get('PATH', '')
        )

    # Block the post-merge baseline warm fork from test processes: tests that
    # mock `gh pr merge` to success (AutoMergePRTest and the routing
    # integration classes) reach outbox_notifier's
    # _spawn_post_merge_baseline_warm with subprocess.Popen UNMOCKED — on the
    # droplet that detaches a REAL `git fetch` + full-suite warm from inside
    # the gate's own suite run. The production #764 re-entrancy guard already
    # skips the spawn when REGBASELINE_WARMING=1, so default it on for the
    # whole test process. The warm-fixture classes
    # (PostMergeBaselineWarmTest, test_regression_baseline_cache) pop it in
    # setUp — and mock Popen — to exercise the real spawn construction.
    os.environ.setdefault('REGBASELINE_WARMING', '1')

    # Runtime production-write tripwire (#428). pytest owns the scan via
    # conftest's session-scoped fixture; arming here too would double-instrument
    # the helpers and double-fire the exit. Under pytest, still set the sentinel
    # env var so the OURLIBERTY_TEST_RUN_SENTINEL mirror stays present for parity
    # (the fixture overwrites it with its own minted value).
    if 'pytest' in sys.modules:
        os.environ.setdefault(
            'OURLIBERTY_TEST_RUN_SENTINEL',
            'OL-TEST-RUN-SENTINEL-' + uuid.uuid4().hex,
        )
    else:
        _arm_runtime_tripwire()


# Engage AT IMPORT — this is the whole point: importing this module (top-level
# under discover, or as a package member via __init__) arms the sandbox before
# any subsequent import in the test file can freeze a path constant.
engage()
