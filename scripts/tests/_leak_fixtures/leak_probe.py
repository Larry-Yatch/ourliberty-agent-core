"""leak_probe.py — the DELIBERATELY-LEAKING test fixture (test-jail Layer E proof).

WHAT THIS IS
------------
A unittest TestCase that, by design, makes an UN-mocked call to one guarded
production chokepoint and so MUST fail under the test jail. It is the
adversary in the acceptance proof: the meta-test
``scripts/tests/test_deliberate_leak_is_caught.py`` runs THIS fixture under every
invocation shape (bare discover, dotted module, direct file, the regression
gate) on both machines and asserts each run fails LOUD with a
``TestIsolationBreach`` and writes ZERO bytes to a stand-in for the real
``~/agents`` tree. When that holds, a future #438-class change — a side effect
added to a code path old tests drive end-to-end — breaks the BUILD, not Larry's
evening.

WHY IT IS NOT COLLECTED BY THE NORMAL SUITE
-------------------------------------------
This file lives under ``scripts/tests/_leak_fixtures/`` and is named
``leak_probe.py`` (NOT ``test_*.py``). ``unittest discover``'s default pattern is
``test*.py``, so it is never collected by the bare-discover suite — which is
critical, because if it WERE collected it would fail forever and break the
green baseline. The meta-test invokes it by EXPLICIT path / dotted module only.

HOW IT LEAKS
------------
On import it engages the PR-1 sandbox (``_bootstrap``), which mints
``OURLIBERTY_TEST_RUN_SENTINEL``. Then the single test method calls exactly one
guarded chokepoint (selected by the ``OL_LEAK_CHANNEL`` env var) WITHOUT mocking
it. Each chokepoint's first statement is ``refuse_under_test(...)`` (Layer B),
which raises ``TestIsolationBreach`` because the sentinel is set — so the call
never reaches the real side effect. The breach IS the proof.

The payloads carry ``LEAK_MARKER`` so the meta-test can scan the jailed home for
it and assert the marker NEVER landed on disk (zero-write hermeticity).
"""
import os
import sys
import unittest
from pathlib import Path

# A unique, greppable marker embedded in every leak payload. The meta-test scans
# the jailed ~/agents stand-in for this string and asserts it NEVER appears —
# i.e. the guard refused the call before any byte was written.
LEAK_MARKER = 'OL-DELIBERATE-LEAK-PROBE-MARKER-DO-NOT-SHIP'

# The four guarded chokepoints the acceptance section enumerates.
CHANNELS = ('alert', 'inbox', 'supabase', 'claude')


def _ensure_repo_on_path() -> None:
    """Put the repo's ``scripts/`` and ``scripts/tests/`` (and this fixture dir)
    on ``sys.path`` so ``import _bootstrap`` and the production-module imports
    resolve under EVERY invocation shape.

    Resolution order:
      1. ``OL_LEAK_REPO_ROOT`` (set by the meta-test) — authoritative, and the
         only thing that works for the gate shape, where a thin shim copy of this
         test runs from a tmp worktree whose ``__file__`` is NOT under the repo.
      2. Walk up from ``__file__`` to the first ancestor that contains
         ``scripts/tests/_bootstrap.py`` — covers the direct-file / dotted shapes
         even with ``OL_LEAK_REPO_ROOT`` unset.
    """
    roots: list[Path] = []
    env_root = os.environ.get('OL_LEAK_REPO_ROOT')
    if env_root:
        roots.append(Path(env_root))
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / 'scripts' / 'tests' / '_bootstrap.py').exists():
            roots.append(parent)
            break
    for root in roots:
        for sub in (
            root / 'scripts' / 'tests' / '_leak_fixtures',
            root / 'scripts' / 'tests',
            root / 'scripts',
        ):
            sp = str(sub)
            if sub.is_dir() and sp not in sys.path:
                sys.path.insert(0, sp)


_ensure_repo_on_path()

# Engage the PR-1 sandbox — this mints OURLIBERTY_TEST_RUN_SENTINEL, exactly as
# every blessed test file does (and as the AST first-import gate enforces). Under
# the regression gate the sentinel is ALSO set by build_sandbox_env; either source
# arms the Layer-B guards. Imported AFTER the path setup so it resolves under the
# direct/dotted shapes too.
try:
    import _bootstrap  # noqa: F401  (engages at import)
except ImportError:  # pragma: no cover - path setup above should prevent this
    pass


def _leak_alert() -> None:
    """UN-mocked larry_alerts.append_alert — the pager (H1)."""
    import larry_alerts
    larry_alerts.append_alert(
        'leak-probe', 'critical',
        f'{LEAK_MARKER}: deliberate un-mocked pager write — must be refused',
        subject=LEAK_MARKER,
    )


def _leak_inbox() -> None:
    """UN-mocked safe_write_inbox — the money amplifier (H4): a leaked envelope
    becomes an autonomous paid Opus dispatch."""
    import safe_write_inbox
    safe_write_inbox.safe_write_inbox(
        'forge',
        {'task_id': LEAK_MARKER, 'prompt': LEAK_MARKER, 'source': 'leak-probe'},
        'beacon',
        f'zz-{LEAK_MARKER}.json',
    )


def _leak_supabase() -> None:
    """UN-mocked supabase client build — the live service-role DB (H7)."""
    import supabase_factory
    supabase_factory.get_supabase_client(
        'http://127.0.0.1:1', f'fake-service-role-key-{LEAK_MARKER}',
    )


def _leak_claude() -> None:
    """UN-mocked claude spawn via agent_runner — real Opus spend (H6).

    ``run_claude`` is refused at the FIRST guarded chokepoint it traverses —
    the concurrency-guard slot acquisition (``concurrency_guard.acquire`` →
    ``refuse_under_test('concurrency-guard')``) — before it can ever reach the
    ``subprocess.Popen`` claude spawn (itself guarded by
    ``refuse_under_test('claude-spawn')`` at agent_runner.py). Either guard
    raises ``TestIsolationBreach``; the spawn is structurally unreachable from a
    test process. This is defense-in-depth: the outer guard fires first, the
    spawn guard is the inner backstop."""
    import agent_runner
    agent_runner.run_claude(
        'forge', f'{LEAK_MARKER}: deliberate un-mocked claude spawn',
        working_dir='/tmp',
    )


_DISPATCH = {
    'alert': _leak_alert,
    'inbox': _leak_inbox,
    'supabase': _leak_supabase,
    'claude': _leak_claude,
}


class DeliberateLeakProbe(unittest.TestCase):
    """A test that deliberately leaks. It MUST error with TestIsolationBreach
    under the jail — that failure is the acceptance proof, not a bug."""

    def test_leak(self):
        channel = os.environ.get('OL_LEAK_CHANNEL', 'alert')
        leak = _DISPATCH.get(channel)
        if leak is None:
            self.fail(
                f'unknown OL_LEAK_CHANNEL={channel!r}; '
                f'expected one of {sorted(_DISPATCH)}'
            )
        # NO mock, NO test_isolation_guard.allow() — the whole point is that the
        # un-mocked call escapes to a guarded chokepoint and is refused LOUD.
        leak()


if __name__ == '__main__':
    unittest.main()
