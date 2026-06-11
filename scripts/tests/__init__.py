"""Test package init — process-level log isolation for unittest invocation.

The canonical sandbox setup now lives in ``scripts/tests/_bootstrap.py`` (the
OURLIBERTY_* env redirects + the #428 atexit live-write tripwire). This
``__init__`` exists for the DOTTED / pytest loader paths
(``python3 -m unittest scripts.tests.<module>`` and pytest), where importing the
``scripts.tests`` package runs this file once at package-import time. It simply
DELEGATES to ``_bootstrap`` so there is a SINGLE SOURCE OF TRUTH and no drift
between the two bootstraps.

Under ``python3 -m unittest discover -s scripts/tests`` this ``__init__`` does
NOT run (discover imports each ``test_*.py`` as a TOP-LEVEL module). That path
is covered because every ``test_*.py`` imports ``_bootstrap`` as its first
import — see _bootstrap.py's module docstring and the
test_bootstrap_first_import.py gate.

PARITY IS ENFORCED. scripts/tests/test_conftest_init_parity.py asserts that
conftest.py's autouse protections each have a mirror in ``_bootstrap`` and that
this ``__init__`` delegates to it. When you add a protection to conftest.py, add
its env-var mirror to _bootstrap.engage() and register it in that test.
"""
# Package-relative import: under the dotted/pytest loader sys.path carries the
# repo root (not scripts/tests), so a bare ``import _bootstrap`` would not
# resolve here — but the relative form imports the submodule of THIS package.
from . import _bootstrap  # noqa: F401

# Importing _bootstrap above already engaged the sandbox at module import; this
# call is the explicit, idempotent re-affirmation (a no-op if already engaged).
_bootstrap.engage()

# Re-export the tripwire state on the PACKAGE so test_conftest_init_parity.py
# (which reads ``scripts.tests._RUNTIME_TRIPWIRE_ARMED``) sees the live value.
# Read via the process-global helpers, not _bootstrap's own attributes: under
# discover the top-level ``_bootstrap`` may have armed under a different module
# identity than this ``scripts.tests._bootstrap``, and only the env guard is
# shared across both.
_RUNTIME_TRIPWIRE_ARMED = _bootstrap.is_armed()
_RUNTIME_TRIPWIRE_SENTINEL = _bootstrap.run_sentinel()
