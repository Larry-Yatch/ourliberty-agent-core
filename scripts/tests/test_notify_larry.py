#!/usr/bin/env python3
"""Tests for notify_larry.py — the agent-core health-check alert sink.

Verifies the sink forwards into larry_alerts.append_alert (the broadcast
infra-alert path with cooldown), NOT append_notification (chat-closure DM),
and that --tier is accepted for caller compatibility but never promotes
severity above 'warning'.
"""
try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import notify_larry  # noqa: E402


class _FakeAlerts(types.ModuleType):
    """Stand-in for the larry_alerts module that records append_alert calls."""

    def __init__(self, return_value=True):
        super().__init__('larry_alerts')
        self.calls = []
        self._return = return_value

    def append_alert(self, **kwargs):
        self.calls.append(kwargs)
        return self._return

    def append_notification(self, **kwargs):  # must never be called
        raise AssertionError('notify_larry must not use append_notification')


class TestNotifyLarry(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get('larry_alerts')

    def tearDown(self):
        if self._saved is not None:
            sys.modules['larry_alerts'] = self._saved
        else:
            sys.modules.pop('larry_alerts', None)

    def _install(self, return_value=True):
        fake = _FakeAlerts(return_value)
        sys.modules['larry_alerts'] = fake
        return fake

    def test_forwards_to_append_alert_with_canonical_args(self):
        fake = self._install()
        ok = notify_larry.send('drift subject', 'drift body')
        self.assertTrue(ok)
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call['source'], 'ourliberty-health')
        self.assertEqual(call['severity'], 'warning')
        self.assertEqual(call['route'], 'escalate')
        self.assertEqual(call['subject'], 'drift subject')
        self.assertEqual(call['message'], 'drift body')

    def test_tier_does_not_promote_severity(self):
        fake = self._install()
        rc = notify_larry.main(
            ['--tier', 'breakdown', '--subject', 's', '--message', 'm'])
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls[0]['severity'], 'warning')

    def test_main_returns_nonzero_when_not_appended(self):
        # append_alert returns False on cooldown-suppression or failure.
        self._install(return_value=False)
        rc = notify_larry.main(['--subject', 's', '--message', 'm'])
        self.assertEqual(rc, 1)

    def test_import_failure_is_nonfatal(self):
        # Simulate larry_alerts being unimportable: send() returns False, no raise.
        sys.modules['larry_alerts'] = None  # forces ImportError on `import`
        self.assertFalse(notify_larry.send('s', 'm'))


if __name__ == '__main__':
    unittest.main()
