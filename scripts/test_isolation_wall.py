"""test_isolation_wall.py — the kernel-enforced half of the test-isolation wall.

WHY THIS EXISTS (the residual classes the software guards structurally cannot cover)
------------------------------------------------------------------------------------
Layers A+B (the OURLIBERTY_* env redirects in ``scripts/tests/_bootstrap.py`` +
conftest, the ``refuse_live_state_write`` choke guards, and the #820 force-override
redirect in ``_bootstrap``/``conftest``) stop every *env-honoring, in-process,
Python* writer from resolving the live ``~/agents`` tree. Three classes slip past
them by construction:

  1. **Hardcoded-literal modules** — the ~dozen modules that bind
     ``/home/larry/agents`` as a literal, immune to the env redirect.
  2. **Arbitrary subprocesses** — a test that shells out to a program (git hook,
     helper script, sqlite CLI) that writes ~/agents; the child never imported
     our Python guards.
  3. **C-level writers** — sqlite3 / ctypes / any write that never passes through
     a Python ``open`` shim.

A per-process **read-only bind mount** over the real ``~/agents`` +
``~/agent-worktrees`` closes all three at once: the *kernel* returns ``EROFS`` on
any write to those trees, regardless of language, redirect-honoring, or how many
subprocess hops deep the writer is — a mount namespace is **inherited by every
child process**, so walling one top-level test runner covers its whole tree.

MECHANISM: ``bwrap`` (bubblewrap), NOT ``unshare``
--------------------------------------------------
This droplet sets ``kernel.apparmor_restrict_unprivileged_userns=1``, so
``unshare --map-root-user`` fails and an earlier review wrongly concluded "no
userns, wall dead." ``/usr/bin/bwrap`` is **setuid-root**, so it creates the
namespace WITHOUT unprivileged-userns and WITHOUT sudo — it sidesteps the
AppArmor restriction. We keep the whole filesystem read-WRITE via
``--dev-bind / /`` (so ``$TMPDIR``, the dispatch worktree, ``~/.claude``,
``~/.config``, bytecode, and the python/git/bash execs all still work) and
overlay a narrow ``--ro-bind`` only on the two real state trees. This is the
same shape the regression gate uses for its discover subprocess; both are kept
in parity by ``test_gate_wall_parity`` meta-tests.

DESIGN INVARIANTS
-----------------
* **Fail-open, never fail-closed.** The wall is additive defense-in-depth. If
  ``bwrap`` is missing, the probe fails, or the invocation cannot be faithfully
  reconstructed, we log loudly and run UNWALLED rather than break a test run or
  block a PR. A missing sandbox primitive must never be the reason a PR can't
  merge.
* **Idempotent / loop-safe.** Re-exec sets ``OURLIBERTY_TEST_WALL_ACTIVE=1`` in
  the child's env via ``--setenv``; the re-exec entry point returns immediately
  when it sees that flag, so a walled process never nests a second bwrap.
* **HOME-override immune.** The real home is resolved from the passwd database
  (``pwd``), not ``$HOME`` — the ~17 HOME-jailing tests set ``$HOME`` under
  ``/tmp``, and we must still wall the REAL ``~/agents``, not a jailed one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

try:
    import pwd  # POSIX-only; find the REAL home regardless of $HOME.
except ImportError:  # pragma: no cover - non-POSIX (macOS dev still has pwd)
    pwd = None

# Set in the re-exec'd child's env via ``--setenv``. Its presence means "this
# process already runs inside the wall" — the single guard that makes re-exec
# idempotent and prevents a nested bwrap. The regression gate ALSO sets this in
# its discover-subprocess prefix so that subprocess's _bootstrap does not try to
# nest a second bwrap inside the gate's own wall.
WALL_ACTIVE_ENV = 'OURLIBERTY_TEST_WALL_ACTIVE'

# Dedicated, single-purpose escape hatch: when set, the re-exec wall is skipped
# and the run proceeds UNWALLED. Intentionally DECOUPLED from the #820 redirect
# opt-out (OURLIBERTY_ALLOW_LIVE_AGENTS_ROOT) so that no single env var can
# collapse BOTH the kernel wall AND Layers A/B at once (an unguarded single-point
# disable). It exists only for fail-open operability on a host where the wall
# must be bypassed deliberately; the ad-hoc incident vector — a bare
# ``python3 -m unittest`` with neither var set — is always walled. A legitimate
# non-test process never reaches the wall at all (it does not import _bootstrap),
# and a resolution-check test that sets the #820 opt-out still passes under the
# wall because reads are unaffected — only live WRITES are refused.
WALL_DISABLE_ENV = 'OURLIBERTY_DISABLE_TEST_WALL'


def real_home() -> str | None:
    """The invoking user's REAL home, resolved from the passwd database so a
    ``$HOME`` override (the HOME-jailing tests, CI) cannot point the wall at a
    jailed tree. Falls back to ``$HOME`` only when passwd lookup is unavailable."""
    if pwd is not None:
        try:
            return pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, OSError):
            pass
    return os.environ.get('HOME')


def ro_targets() -> list[str]:
    """The real state trees to wall read-only: ``~/agents`` and
    ``~/agent-worktrees``, filtered to those that actually exist (a target that
    is absent cannot be bind-mounted and must be skipped, not error). Order is
    stable so the built prefix is deterministic (parity tests depend on it)."""
    home = real_home()
    if not home:
        return []
    out = []
    for sub in ('agents', 'agent-worktrees'):
        path = os.path.join(home, sub)
        if os.path.isdir(path):
            out.append(path)
    return out


def _probe(cmd: list[str]) -> bool:
    """Run ``cmd`` and return True iff it exits 0. Never raises — a probe that
    cannot run is treated as "primitive unavailable" so callers fall through to
    the unwalled degrade path instead of crashing."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def bwrap_prefix(
    targets: list[str] | None = None,
    workdir: str | None = None,
    set_wall_active: bool = True,
) -> list[str] | None:
    """Build and PROBE a bwrap argv prefix that makes ``targets`` read-only for
    the wrapped command (and its whole subprocess tree).

    Returns the prefix (ending in ``--``) on success, or ``None`` when bwrap is
    unavailable or the probe fails — the caller degrades to an unwalled run.
    Probing before returning guarantees a returned prefix actually works, so the
    green path is never handed a half-built wall.
    """
    bwrap = shutil.which('bwrap')
    if not bwrap:
        return None
    if targets is None:
        targets = ro_targets()
    prefix = [bwrap, '--dev-bind', '/', '/']
    if workdir:
        prefix += ['--chdir', workdir]
    for target in targets:
        prefix += ['--ro-bind', target, target]
    if set_wall_active:
        prefix += ['--setenv', WALL_ACTIVE_ENV, '1']
    prefix += ['--']
    if not _probe(prefix + ['/bin/true']):
        return None
    return prefix


def is_walled() -> bool:
    """True if this process already runs inside the wall."""
    return os.environ.get(WALL_ACTIVE_ENV) == '1'


def _reconstruct_cmd() -> list[str] | None:
    """The original interpreter invocation to re-run under the wall, rebuilt
    from ``sys.orig_argv`` (Python 3.10+). Returns ``None`` when it cannot be
    reconstructed faithfully (older Python, empty argv) so the caller degrades.

    For a python interpreter invocation (``python3 -m unittest ...`` — the whole
    test workflow here) we pin ``sys.executable`` as argv[0] so the SAME
    interpreter re-execs, not whatever ``python3`` happens to resolve to on
    PATH. For a non-python entry point (a console script) we resolve argv[0]
    via ``shutil.which`` and fail-open (return None -> unwalled) when it is not a
    runnable path — otherwise bwrap would fail to exec it in a grandchild,
    INVISIBLE to the os.execvp except-guard, and hard-fail the run.
    """
    orig = getattr(sys, 'orig_argv', None)
    if not orig or not orig[0]:
        return None
    if os.path.basename(orig[0]).startswith('python'):
        # ``python3 -m unittest ...`` — pin sys.executable (always a runnable
        # absolute path) so the SAME interpreter re-execs.
        return [sys.executable] + list(orig[1:])
    # Non-python entry point: resolve up front; degrade to unwalled if it is not
    # a runnable path, keeping the fail-open invariant on this branch too.
    resolved = shutil.which(orig[0])
    if not resolved:
        return None
    return [resolved] + list(orig[1:])


def reexec_under_wall() -> None:
    """Re-exec the current process under the bwrap read-only-bind wall, unless
    it is already walled, has opted out, or the wall primitive is unavailable.

    This is the universal catch for the ACTUAL incident vector: an agent (or a
    developer) running a bare ``python3 -m unittest ...`` inside a Forge/Mirror
    dispatch worktree. There is no code chokepoint for that — the command is
    typed into a shell — so we intercept it at the one place every test file is
    guaranteed to reach FIRST: the ``_bootstrap`` import. Because the namespace
    is inherited by children, wrapping this one process walls the entire test
    run and everything it shells out to.

    On success this call does NOT return — ``os.execvp`` replaces the process.
    Every non-success path returns quietly-or-loudly so the test run continues
    unwalled rather than breaking (fail-open invariant).
    """
    if is_walled():
        return  # already inside the wall (re-exec child, or the gate's own wall)
    if os.environ.get(WALL_DISABLE_ENV):
        return  # dedicated escape hatch (decoupled from the #820 redirect opt-out)

    if shutil.which('bwrap') is None:
        # Expected on macOS dev and any host without bubblewrap. Loud so a
        # droplet that lost bwrap is noticed, but non-fatal.
        print(
            '[test-isolation-wall] bwrap not found — test run is UNWALLED '
            '(kernel-enforced ~/agents isolation off; Layers A+B still apply). '
            'Install bubblewrap to arm the wall.',
            file=sys.stderr,
        )
        return

    cmd = _reconstruct_cmd()
    if not cmd:
        print(
            '[test-isolation-wall] cannot reconstruct invocation '
            '(sys.orig_argv unavailable) — test run is UNWALLED.',
            file=sys.stderr,
        )
        return

    prefix = bwrap_prefix()
    if prefix is None:
        print(
            '[test-isolation-wall] bwrap probe failed — test run is UNWALLED '
            '(Layers A+B still apply).',
            file=sys.stderr,
        )
        return

    argv = prefix + cmd
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:  # pragma: no cover - exec of a probed binary rarely fails
        # The probe just succeeded, so this is a genuine surprise (e.g. the
        # binary was removed between probe and exec). Degrade rather than die.
        print(
            f'[test-isolation-wall] re-exec failed ({exc!r}) — test run is '
            'UNWALLED.',
            file=sys.stderr,
        )
        return
