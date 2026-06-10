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
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OL_CAPTURE_TITLE="${1:-${OL_CAPTURE_TITLE:-}}" \
OL_CAPTURE_NOTE="${2:-${OL_CAPTURE_NOTE:-}}" \
  exec python3 "$HERE/emit_capture_impl.py"
