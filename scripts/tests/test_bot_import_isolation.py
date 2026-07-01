#!/usr/bin/env python3
"""Guard: the telegram bots must be importable WITHOUT a live bot environment.

Both bots used to `sys.exit()` at import scope when the token/AGENT env was unset. That
crashed any test module that imported them standalone, and made suite verdicts depend on
which sibling test happened to `os.environ.setdefault` a token FIRST — the isolation-debt
class in [[test-isolation-hygiene-debt]]. The fatal checks now live in each bot's
_require_runtime_env() (called from main(), i.e. only when RUN). This locks that in: import
each bot in a subprocess with the bot env scrubbed and assert it does NOT exit.

If this fails, someone reintroduced an import-scope `sys.exit`/env-required side effect —
move it into main()/_require_runtime_env(), don't re-add the token to a fixture."""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

import os
import subprocess
import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent


class BotImportIsolationTest(unittest.TestCase):
    def _assert_imports_clean(self, module, extra_env=None):
        # Scrub every telegram/agent env var so we prove the bot imports without them.
        env = {k: v for k, v in os.environ.items()
               if k not in ('TELEGRAM_BOT_TOKEN_BEACON', 'TELEGRAM_ALLOWED_CHAT_IDS', 'AGENT')
               and not k.startswith('TELEGRAM_BOT_TOKEN_')}
        env['PYTHONPATH'] = str(_SCRIPTS)
        if extra_env:
            env.update(extra_env)
        r = subprocess.run(
            [sys.executable, '-c', f'import {module}'],
            capture_output=True, text=True, env=env, cwd=str(_SCRIPTS), timeout=60,
        )
        self.assertEqual(
            r.returncode, 0,
            f'{module} did not import cleanly (rc={r.returncode}) with the bot env scrubbed '
            f'— an import-scope sys.exit/env requirement regressed. stderr tail:\n{r.stderr[-800:]}',
        )
        self.assertNotIn(
            'SystemExit', r.stderr,
            f'{module} raised SystemExit at import. stderr tail:\n{r.stderr[-500:]}',
        )

    def test_beacon_bot_imports_without_token(self):
        self._assert_imports_clean('beacon_telegram_bot')

    def test_agent_bot_imports_without_token(self):
        # AGENT drives module-level path building; a valid slug keeps import side-effect-free
        # while still proving the TOKEN/ALLOWED/dir checks are deferred out of import scope.
        self._assert_imports_clean('agent_telegram_bot', extra_env={'AGENT': 'forge'})


if __name__ == '__main__':
    unittest.main()
