#!/usr/bin/env bash
# emit_capture.sh — one-gesture durable capture (Missions v2 Phase 1).
#
# Invoked when I flag a follow-up / idea / hole mid-work, or when Larry says
# "capture this." Unlike emit_desktop_session.sh (a backgrounded best-effort
# hook), this runs the emitter in the FOREGROUND and propagates its exit code:
# a capture's whole point is durability, so a failed POST should be visible,
# not swallowed.
#
# Usage:
#   emit_capture.sh "<title>" ["<note>"]
# Title may also come from OL_CAPTURE_TITLE; the positional arg wins when given.
# Other OL_CAPTURE_* / OL_* knobs inherit from the environment.
#
# POSITIONAL ONLY — there are no flags, and a flag-style call is refused rather
# than accepted. `--title X --note Y` used to "succeed": it filed a capture
# titled "--title" with the real title in the note and silently dropped every
# argument after the second. Three such cards exist in captures.json. Failing
# loudly costs a retry; succeeding wrongly costs a mislabeled durable record.
set -uo pipefail

if [[ $# -gt 0 && "${1-}" == --* ]]; then
  cat >&2 <<'EOF'
emit_capture.sh: refusing a flag-style invocation — this script takes
POSITIONAL arguments only, and would otherwise file a capture titled "--title".

usage: emit_capture.sh "<title>" ["<note>"]

There is no --title / --note / --repo option. The repo (origin) is derived from
the CURRENT DIRECTORY's git context, never from an argument — cd into the repo
the capture belongs to before running this.

Title may also come from $OL_CAPTURE_TITLE when no positional arg is given.
EOF
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OL_CAPTURE_TITLE="${1:-${OL_CAPTURE_TITLE:-}}" \
OL_CAPTURE_NOTE="${2:-${OL_CAPTURE_NOTE:-}}" \
  exec python3 "$HERE/emit_capture_impl.py"
