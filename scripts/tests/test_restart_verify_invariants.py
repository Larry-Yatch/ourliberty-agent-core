#!/usr/bin/env python3
"""test_restart_verify_invariants.py — repo-wide lint over the SET of
`systemctl restart --no-block` verifiers, not over any one of them.

WHY THIS IS A LINT AND NOT THREE TESTS
--------------------------------------
Three modules in scripts/ issue `sudo -n systemctl restart --no-block <unit>`
and then decide, from an `is-active` read, whether the restart worked:

    heal_stale_daemon_code.py        — had the Job= discriminator all along
    heal_claude_json_bind_drift.py   — did NOT, until this batch
    heal_dashboard_api_sha_drift.py  — did NOT, until this batch

`--no-block` returns as soon as the job is ENQUEUED. A unit whose start job is
held behind an `After=` ordering dependency that is still draining reads
`inactive` for the whole check window while being perfectly healthy — the repo
has measured ~90s for exactly this pair (ourliberty-outbox-notifier and
ourliberty-spec-review-runner are both ordered
`After=ourliberty-inbox-watcher.service`, whose claude loop takes ~90s to
SIGTERM-drain). Concluding failure from the is-active read alone pages Larry for
a unit that is active seconds later.

`systemctl show <unit> --property=Job` is the discriminator that tells the two
apart, and one copy having it while another does not is precisely how this
defect shipped twice. So the invariant is asserted over the SET: any module that
can derive a negative verdict from an is-active read must also consult `Job=`.

Run:
    cd ~/agent-core && python3 -m unittest scripts.tests.test_restart_verify_invariants
"""
from __future__ import annotations

try:  # engage the test sandbox before any production import reads env/paths
    from . import _bootstrap  # noqa: F401
except ImportError:  # discover loads this module top-level (no package parent)
    import _bootstrap  # noqa: F401

import unittest
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parent.parent

# Marker of "this module returns a negative verdict derived from an is-active
# read" — the `-1` rc contract every one of these verifiers shares.
_NEGATIVE_VERDICT_MARKERS = ('return -1', 'VERDICT_NOT_ENQUEUED')
_JOB_PROBE = '--property=Job'


def _restart_verifiers() -> list[tuple[Path, str]]:
    """Every scripts/*.py that issues a --no-block restart AND reads is-active."""
    found = []
    for path in sorted(_REPO_SCRIPTS.glob('*.py')):
        text = path.read_text()
        if "'--no-block'" not in text and '"--no-block"' not in text:
            continue
        if 'is-active' not in text:
            continue
        found.append((path, text))
    return found


class RestartVerifierJobProbeLintTests(unittest.TestCase):

    def test_every_negative_verdict_verifier_consults_the_pending_job(self):
        verifiers = _restart_verifiers()
        # Guard the guard, INSIDE the assertion it guards rather than as its own
        # test: a refactor that renames the shellout would otherwise turn this
        # lint into a silent no-op that still reports green.
        self.assertGreaterEqual(
            len(verifiers), 3,
            f'expected at least 3 --no-block restart verifiers, found '
            f'{[p.name for p, _ in verifiers]} — this lint has gone blind')
        offenders = []
        for path, text in verifiers:
            if not any(m in text for m in _NEGATIVE_VERDICT_MARKERS):
                continue
            if _JOB_PROBE in text:
                continue
            line = next(
                (i + 1 for i, ln in enumerate(text.splitlines())
                 if any(m in ln for m in _NEGATIVE_VERDICT_MARKERS)), 0)
            offenders.append(f'{path.name}:{line}')
        self.assertEqual(
            offenders, [],
            msg=('these modules conclude a FAILED restart from an is-active '
                 'read after `systemctl restart --no-block`, without asking '
                 f'`systemctl show {_JOB_PROBE}` whether the start job is still '
                 'enqueued. A unit held behind an After= dependency reads '
                 'inactive while healthy, so this false-pages. Port '
                 '_unit_pending_job (see heal_stale_daemon_code.py): '
                 f'{offenders}'))

    def test_the_job_probe_semantics_match_across_copies(self):
        """All copies must fail SAFE the same way: an unreadable probe returns
        '' so it can never manufacture a false in-progress verdict that
        suppresses a real failure."""
        import heal_claude_json_bind_drift as bd
        import heal_dashboard_api_sha_drift as sha
        import heal_stale_daemon_code as sd
        for fn in (bd.unit_pending_job, sha._unit_pending_job,
                   sd._unit_pending_job):
            with self.subTest(fn=fn.__module__):
                doc = (fn.__doc__ or '')
                self.assertIn('Job=', doc)
                self.assertIn("''", doc)


if __name__ == '__main__':
    unittest.main()
