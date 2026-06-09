#!/usr/bin/env bash
# emit_desktop_session.sh — Claude Code SessionStart/SessionEnd hook.
#
# Drains the hook's stdin JSON, then BACKGROUNDS the Python emitter so the
# session is never delayed by the network call. Always exits 0 — best-effort
# telemetry; a missed card is an acceptable outcome (same posture as every
# other push-instrumented chain event).
#
# settings.json registers this with the event_type as the single arg, e.g.
#   "command": "$CLAUDE_PROJECT_DIR/scripts/emit_desktop_session.sh desktop_session_start"
#   "command": "$CLAUDE_PROJECT_DIR/scripts/emit_desktop_session.sh desktop_session_done"
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Drain stdin BEFORE backgrounding (the child won't have the pipe). Pass it +
# the event type through the environment; all other OL_* knobs inherit from the
# session env if the operator set any.
OL_HOOK_INPUT="$(cat 2>/dev/null || true)" \
EVENT_TYPE="${1:-}" \
  nohup python3 "$HERE/emit_desktop_session_impl.py" >/dev/null 2>&1 &

exit 0
