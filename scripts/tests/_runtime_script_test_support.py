"""Shared helpers for tests that exec a real runtime shell script
(sync_agent_core.sh, run_cycle.sh) in a tmpdir-rooted fake agent-core repo.

Both of those scripts shell out to larry_alerts.py — wrapped in `timeout 10 …`
— to enqueue sync-blocked / wrong-branch alerts. Two cross-platform snags bit
every such harness, so they live here as one source of truth:

  * larry_alerts.py imports the atomic_io + file_lock sibling modules (#392),
    plus test_isolation_guard (#469, test-jail PR-2). A fake scripts dir that
    copies only larry_alerts.py makes every emission die on ModuleNotFoundError,
    silently dropping the alert (the calls end in `|| true`).
    copy_larry_alerts_cli() copies the CLI AND its deps, so adding a new dep is
    a one-line change here instead of a silent break in N harnesses.

  * GNU coreutils `timeout` is present on the droplet but not on macOS, where
    its absence drops every alert the same way. install_timeout_shim() drops a
    pass-through shim on a bin dir the harness puts on PATH.

  * test_isolation_guard.refuse_under_test() (#469) RAISES when the run sentinel
    OURLIBERTY_TEST_RUN_SENTINEL is set — and the test bootstrap sets it in the
    parent, so a `os.environ.copy()` subprocess inherits it and every guarded
    sink (larry_alerts.append_alert) raises TestIsolationBreach instead of
    emitting. A harness that LEGITIMATELY drives the guarded sink for real in a
    HOME-sandboxed tmp tree must scrub the sentinel from the child env — the
    subprocess equivalent of test_isolation_guard.allow(). scrub_run_sentinel()
    does exactly that.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent

_RUN_SENTINEL_VAR = 'OURLIBERTY_TEST_RUN_SENTINEL'

# larry_alerts.py + the sibling modules it imports. Single source of truth: when
# larry_alerts.py grows a new local dependency, add it here once.
_LARRY_ALERTS_CLI = (
    _SCRIPTS_DIR / 'larry_alerts.py',
    _SCRIPTS_DIR / 'atomic_io.py',
    _SCRIPTS_DIR / 'file_lock.py',
    _SCRIPTS_DIR / 'test_isolation_guard.py',
)


def copy_larry_alerts_cli(dst_scripts_dir: Path) -> None:
    """Copy larry_alerts.py and its sibling-module deps into a fake scripts dir
    so the CLI imports cleanly when a shell script shells out to it."""
    for src in _LARRY_ALERTS_CLI:
        shutil.copy2(src, dst_scripts_dir / src.name)


def scrub_run_sentinel(env: dict) -> dict:
    """Drop OURLIBERTY_TEST_RUN_SENTINEL from a subprocess env so a script that
    shells out to a choke-guarded sink (larry_alerts.append_alert calls
    test_isolation_guard.refuse_under_test) can emit FOR REAL into its
    HOME-sandboxed tmp tree. The subprocess equivalent of
    test_isolation_guard.allow(): the child writes only under the redirected
    $HOME, so it is fully sandboxed; without scrubbing, the guard raises
    TestIsolationBreach and the alert is silently dropped (`|| true`). Mutates
    and returns env for chaining."""
    env.pop(_RUN_SENTINEL_VAR, None)
    return env


def redirect_agents_root(env: dict) -> dict:
    """Point OURLIBERTY_AGENTS_ROOT at the child's redirected $HOME/agents so a
    HOME-sandboxed subprocess writes alerts where the harness reads them.

    The test bootstrap (#820 force-override) pins OURLIBERTY_AGENTS_ROOT to a
    /tmp sandbox in the PARENT process, and an ``os.environ.copy()`` subprocess
    inherits it -- so the child larry_alerts.py resolves AGENTS_ROOT to that
    inherited sandbox instead of the $HOME the harness overrode, dropping the
    alert off-tree where _read_alerts never looks. Realign it to $HOME/agents
    (larry_alerts' own default) AFTER HOME has been set in env. Mutates and
    returns env for chaining."""
    env["OURLIBERTY_AGENTS_ROOT"] = str(Path(env["HOME"]) / "agents")
    return env


def install_timeout_shim(bin_dir: Path) -> None:
    """If the platform lacks GNU `timeout` (macOS), drop a pass-through shim on
    `bin_dir` (which the caller must prepend to the subprocess PATH). The shim
    drops the duration arg and execs the rest — test commands are fast, so the
    timeout itself is immaterial. No-op when a real `timeout` is on PATH."""
    if shutil.which('timeout') is not None:
        return
    shim = bin_dir / 'timeout'
    shim.write_text('#!/bin/sh\nshift\nexec "$@"\n')
    os.chmod(shim, 0o755)
