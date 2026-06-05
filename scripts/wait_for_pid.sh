#!/usr/bin/env bash
# wait_for_pid.sh — block until a PID you captured exits, with a hard
# wall-clock timeout backstop. THE safe primitive for "wait on a backgrounded
# process" anywhere in the agent harness / review-dispatch shell. If you ever
# think you need a poll loop around a `&`-backgrounded job, call this instead
# of hand-rolling one.
#
# Why this exists
# ---------------
# Mirror has TWICE hand-rolled poll loops that wedged the whole pipeline for
# 71–102 min by holding a fleet slot + a live Opus session and blocking
# inbox-watcher behind a stuck `Bash` tool call:
#
#   PR #101 (2026-05-25): `until [ -f /tmp/regression-done ] \
#     || ! kill -0 $(pgrep -f test_regression_check.py | head -1); do sleep 3; done`
#     — `pgrep -f` self-matched the poll loop's OWN argv (the command line
#     contains the literal pattern), so it returned the loop's own PID,
#     `kill -0` always succeeded, and the loop never exited.
#
#   PR #334 (2026-06-05): `until [ ! -d /proc/$(pgrep -f '[t]est_regression_check.py' \
#     | head -1) ]; do sleep 3; done`
#     — the `[t]…` bracket trick fixed the self-match, but once the watched
#     process finished, `pgrep` returned EMPTY, command substitution collapsed
#     `/proc/$()` to `/proc/` (always a directory), `[ ! -d /proc/ ]` was never
#     true, and the loop never exited.
#
# Both share one root cause: liveness was RE-DERIVED every iteration (via
# `pgrep` / a `/proc/<pid>` path test) instead of being gated on a PID captured
# ONCE, and there was no timeout backstop, so a single bad derivation wedged
# the pipeline indefinitely. This script removes both footguns:
#   1. You pass the PID; we capture it once and gate liveness SOLELY on
#      `kill -0 "$pid"` — never pgrep, never a /proc path test.
#   2. A hard wall-clock timeout (default 900s) means the wait can never
#      outlive its ceiling. On timeout we fail LOUDLY (exit 124) so the caller
#      retries instead of wedging; inbox-watcher then re-dispatches the task.
#
# NOTE: For `scripts/test_regression_check.py` specifically the correct answer
# is still "don't background it at all — run it foreground and read the exit
# code" (see agents/mirror/CLAUDE.md). This helper is the safe fallback for the
# cases where waiting on a captured PID is genuinely unavoidable.
#
# Contract
# --------
#   wait_for_pid.sh <pid> [--timeout SECONDS] [--interval SECONDS]
#
#   exit 0   — the process is gone (it exited, or was never alive).
#   exit 124 — wall-clock timeout reached while the process was still alive
#              (the timeout(1) convention). Printed loudly to stderr.
#   exit 2   — usage error: missing / empty / non-numeric pid (this is the
#              empty-pgrep footgun — `wait_for_pid.sh "$(pgrep …)"` with no
#              match passes an empty arg), or a bad option value.
#
# Defaults are chosen so that any misuse fails fast and never hangs.
set -u

TIMEOUT_SECONDS=900     # 15 min hard ceiling — a poll wait must never outlive this.
INTERVAL_SECONDS=3

usage() {
    sed -n '2,60p' "$0"
}

PID=""
while [ $# -gt 0 ]; do
    case "$1" in
        # A bare `--timeout`/`--interval` as the LAST arg must NOT `shift 2`:
        # under `set -u` (no `set -e`) a shift past $# fails silently, leaves the
        # args unchanged, and the while-loop re-processes the flag forever — the
        # script whose whole job is to stop poll-loop wedges would wedge itself.
        # Require the value explicitly before consuming it.
        --timeout)
            if [ $# -lt 2 ]; then
                echo "wait_for_pid.sh: FATAL: --timeout requires a value." >&2
                exit 2
            fi
            TIMEOUT_SECONDS="$2"; shift 2 ;;
        --timeout=*) TIMEOUT_SECONDS="${1#*=}"; shift ;;
        --interval)
            if [ $# -lt 2 ]; then
                echo "wait_for_pid.sh: FATAL: --interval requires a value." >&2
                exit 2
            fi
            INTERVAL_SECONDS="$2"; shift 2 ;;
        --interval=*) INTERVAL_SECONDS="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift ;;
        -*) echo "wait_for_pid.sh: FATAL: unknown option: $1" >&2; exit 2 ;;
        *)
            if [ -z "$PID" ]; then
                PID="$1"
            else
                echo "wait_for_pid.sh: FATAL: unexpected extra argument: $1" >&2
                exit 2
            fi
            shift ;;
    esac
done

# --- validate the captured PID (the empty-substitution / self-match guard) ---
case "$PID" in
    ''|*[!0-9]*)
        echo "wait_for_pid.sh: FATAL: pid must be a positive integer, got '${PID}'." >&2
        echo "  An empty pid almost always means a pgrep/command substitution matched" >&2
        echo "  nothing. Do NOT fall back to a /proc path test or re-run pgrep — that is" >&2
        echo "  exactly the loop that wedged the pipeline on PR #101 and PR #334. Capture" >&2
        echo "  the PID once (e.g. \`mypid=\$!\` right after the \`&\`) and pass it here." >&2
        exit 2 ;;
esac
case "$TIMEOUT_SECONDS" in
    ''|*[!0-9]*)
        echo "wait_for_pid.sh: FATAL: --timeout must be a positive integer, got '${TIMEOUT_SECONDS}'." >&2
        exit 2 ;;
esac
# Reject 0 explicitly: a 0 ceiling means "time out on the very first iteration"
# (instant exit 124 even for a healthy process → spurious caller retries). A
# caller that wants an effectively-unbounded wait passes a large number, not 0.
if [ "$TIMEOUT_SECONDS" -lt 1 ]; then
    echo "wait_for_pid.sh: FATAL: --timeout must be >= 1 second (got ${TIMEOUT_SECONDS})." >&2
    exit 2
fi
case "$INTERVAL_SECONDS" in
    ''|*[!0-9]*)
        echo "wait_for_pid.sh: FATAL: --interval must be a positive integer, got '${INTERVAL_SECONDS}'." >&2
        exit 2 ;;
esac
[ "$INTERVAL_SECONDS" -lt 1 ] && INTERVAL_SECONDS=1

# --- the loop: liveness gated SOLELY on `kill -0 <captured pid>` ---
# `kill -0` sends no signal; it only tests whether the (captured) PID is still
# signalable. No re-derivation, no /proc, so there is no state that can stay
# "alive" after the process is gone. The elapsed counter is the backstop: even
# if `kill -0` somehow never flipped (e.g. PID reuse), the loop exits at the
# ceiling rather than spinning forever.
elapsed=0
while kill -0 "$PID" 2>/dev/null; do
    if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
        echo "wait_for_pid.sh: TIMEOUT after ${TIMEOUT_SECONDS}s waiting on pid ${PID} (still alive)." >&2
        echo "  Failing loudly (exit 124) so the caller retries rather than wedging the pipeline." >&2
        exit 124
    fi
    sleep "$INTERVAL_SECONDS"
    elapsed=$((elapsed + INTERVAL_SECONDS))
done

exit 0
