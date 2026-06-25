#!/usr/bin/env python3
"""notify_larry.py — alert sink for agent_core_health_check.py.

Bridges the agent-core health check's alert path into the canonical
`larry_alerts` broadcast alert stream. The health check invokes:

    python3 notify_larry.py --tier <t> --subject <s> --message <m>

and this forwards into `larry_alerts.append_alert(source='ourliberty-health',
severity='warning', ...)` — the broadcast infra-alert sink, which carries its
own per-`source:subject` cooldown machinery. Repo drift is broadcast
infra-noise, not a chat-initiated task closure, so `append_alert` (not
`append_notification`, which needs a `chat_id` and has no cooldown) is the
correct sink.

History: `agent_core_health_check.py:56` has pointed `NOTIFY_SCRIPT` at this
path since the 2026-05-08 gm-agent-core port, but the file never existed — so
every alert hit the `.exists() is False` branch and was silently dropped
(~787 'notify script missing, alert dropped' log lines over 6+ weeks). This
wires the path.

The `--tier` argument is accepted for argv compatibility with the existing
caller but is NOT mapped to severity: repo drift is always `warning`
(divergence needing human judgment, not an outage — nothing is down).
"""
from __future__ import annotations

import argparse
import sys

ALERT_SOURCE = 'ourliberty-health'


def send(subject: str, message: str) -> bool:
    """Forward one health alert into the larry_alerts broadcast stream.

    Returns True if the alert was appended, False if it was cooldown-suppressed
    or the append failed. Both outcomes are non-fatal for the caller, which
    fire-and-forgets.
    """
    try:
        import larry_alerts
    except ImportError as e:
        print(f'[notify_larry] larry_alerts import failed: {e}', file=sys.stderr)
        return False
    return larry_alerts.append_alert(
        source=ALERT_SOURCE,
        severity='warning',
        message=message,
        subject=subject,
        route='escalate',
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description='Forward agent-core health alerts into Larry\'s alert stream')
    p.add_argument('--tier', default=None,
                   help='Accepted for caller compatibility; NOT mapped to severity '
                        '(repo drift is always warning).')
    p.add_argument('--subject', required=True,
                   help='Alert subject — also the dedup-key suffix for cooldown.')
    p.add_argument('--message', required=True, help='Alert body.')
    args = p.parse_args(argv)

    ok = send(args.subject, args.message)
    if not ok:
        print('[notify_larry] alert not appended '
              '(cooldown-suppressed or sink failure)', file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
