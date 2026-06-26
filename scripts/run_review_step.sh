#!/usr/bin/env bash
# run_review_step.sh — run ONE long review step (the test regression check, a
# subagent task, any slow command) FOREGROUND under a hard wall-clock ceiling,
# so it can NEVER hang a Mirror review. This is the safe primitive that removes
# all reason to background-and-poll during a review.
#
# Why this exists
# ---------------
# Mirror reviews have repeatedly wedged the WHOLE review queue by hand-rolling
# an unbounded poll around a long step. The step is backgrounded, then a shell
# loop waits for it — and when the awaited signal never arrives, the loop spins
# forever, the parent review process stays blocked on that one Bash call,
# and the hung session holds the per-agent `inbox:mirror` dispatch lease, which
# serializes ALL Mirror reviews. Every PR queued behind it dead-stalls until a
# human kills the process by hand.
#
# Three distinct shapes of this same wedge have fired in production:
#
#   PR #101 (2026-05-25, ~71 min): `until ... ! kill -0 $(pgrep -f
#     test_regression_check.py | head -1); do sleep 3; done` — `pgrep -f`
#     self-matched the poll loop's OWN argv, so liveness never flipped.
#
#   PR #334 (2026-06-05, ~102 min): `until [ ! -d /proc/$(pgrep -f
#     '[t]est_regression_check.py' | head -1) ]; do sleep 3; done` — once the
#     process finished, `pgrep` returned empty, `/proc/$()` collapsed to
#     `/proc/` (always a directory), and the `until` never exited.
#
#   PR #717 / #720 (2026-06-26, ~85-100 min): a Bash-tool *background-mode*
#     command polled by `until [ -s <task>.output ] && grep -qE
#     'verdict|timed out|Traceback' <that file>; do sleep 15; done`. When the
#     backgrounded command finished WITHOUT ever writing one of those keywords
#     (e.g. it emitted only warnings and exited 0), the content sentinel never
#     appeared and the poll spun forever. `wait_for_pid.sh` could not be used
#     because the Bash tool's background mode hides the child's `$!`.
#
# Every one shares two roots: (1) liveness/completion was re-derived from a
# fragile proxy each iteration (a `pgrep`, a `/proc` path, a grep for a
# content sentinel that may never be written), and (2) there was no wall-clock
# ceiling. The robust answer is to NOT background and poll at all: run the
# step in the FOREGROUND with a hard timeout and read the exit code. This
# script is that primitive — it owns the child, bounds it on the wall clock,
# kills it on timeout, and reports a single unambiguous result.
#
# Relationship to the other primitives:
#   - `scripts/test_regression_check.py` should be run THROUGH this helper (or
#     plain foreground) — never backgrounded. See agents/mirror/CLAUDE.md.
#   - `scripts/wait_for_pid.sh` remains the primitive for the rarer case where
#     a process is ALREADY backgrounded with a shell `&` and you hold its `$!`
#     and genuinely cannot run it foreground. If you control the launch, prefer
#     this helper: foreground + bounded + kills-on-timeout in one call.
#
# Contract
# --------
#   run_review_step.sh [--timeout SECONDS] [--interval SECONDS] [--label TEXT] \
#       [--] <command> [args...]
#
#   exit <rc> — the command ran to completion within the budget; <rc> is the
#               command's OWN exit code, passed through unchanged (0 = success).
#   exit 124  — the command exceeded the wall-clock ceiling; it (and its
#               process group, best-effort) was killed and a clear
#               `=== REVIEW_STEP_TIMED_OUT ===` banner was printed. 124 is the
#               timeout(1) convention. Mirror: treat a timed-out step as
#               INCONCLUSIVE and emit REVIEW_ESCALATE — never hang, never PASS.
#   exit 2    — usage error: no command given, or a bad option value. Fails
#               fast so a misuse can never turn into a hang.
#
# Defaults are chosen so any misuse fails fast and the wait can never outlive
# its ceiling.
set -u

TIMEOUT_SECONDS=900     # 15 min hard ceiling — a review step must never outlive this.
INTERVAL_SECONDS=2
KILL_GRACE_SECONDS=5    # grace between SIGTERM and SIGKILL on a timed-out step.
LABEL=""

usage() {
    sed -n '2,80p' "$0"
}

# --- parse OUR options up to the command (first non-option token, or `--`) ---
# Mirror's command frequently carries its OWN `--flags` (e.g.
# `python3 scripts/test_regression_check.py --parent-sha X --output json`), so
# we must stop consuming options the moment the command begins. A leading token
# that is not one of our known options (or an explicit `--`) starts it.
while [ $# -gt 0 ]; do
    case "$1" in
        # A bare `--timeout`/`--interval`/`--label` as the LAST arg must NOT
        # `shift 2`: under `set -u` (no `set -e`) a shift past $# fails
        # silently, leaves the args unchanged, and the while-loop re-processes
        # the flag forever — the script whose whole job is to stop hangs would
        # hang itself. Require the value explicitly before consuming it.
        --timeout)
            if [ $# -lt 2 ]; then
                echo "run_review_step.sh: FATAL: --timeout requires a value." >&2
                exit 2
            fi
            TIMEOUT_SECONDS="$2"; shift 2 ;;
        --timeout=*) TIMEOUT_SECONDS="${1#*=}"; shift ;;
        --interval)
            if [ $# -lt 2 ]; then
                echo "run_review_step.sh: FATAL: --interval requires a value." >&2
                exit 2
            fi
            INTERVAL_SECONDS="$2"; shift 2 ;;
        --interval=*) INTERVAL_SECONDS="${1#*=}"; shift ;;
        --label)
            if [ $# -lt 2 ]; then
                echo "run_review_step.sh: FATAL: --label requires a value." >&2
                exit 2
            fi
            LABEL="$2"; shift 2 ;;
        --label=*) LABEL="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        -*) echo "run_review_step.sh: FATAL: unknown option: $1" >&2; exit 2 ;;
        *) break ;;   # first non-option token: the command begins here.
    esac
done

if [ $# -eq 0 ]; then
    echo "run_review_step.sh: FATAL: no command given." >&2
    echo "  usage: run_review_step.sh [--timeout S] [--label T] [--] <command> [args...]" >&2
    exit 2
fi

case "$TIMEOUT_SECONDS" in
    ''|*[!0-9]*)
        echo "run_review_step.sh: FATAL: --timeout must be a positive integer, got '${TIMEOUT_SECONDS}'." >&2
        exit 2 ;;
esac
# Reject 0: a 0 ceiling means "time out on the first iteration" (instant kill of
# a healthy step). A caller that wants an effectively-unbounded wait passes a
# large number, not 0 — but for a review step, leave the default.
if [ "$TIMEOUT_SECONDS" -lt 1 ]; then
    echo "run_review_step.sh: FATAL: --timeout must be >= 1 second (got ${TIMEOUT_SECONDS})." >&2
    exit 2
fi
case "$INTERVAL_SECONDS" in
    ''|*[!0-9]*)
        echo "run_review_step.sh: FATAL: --interval must be a positive integer, got '${INTERVAL_SECONDS}'." >&2
        exit 2 ;;
esac
[ "$INTERVAL_SECONDS" -lt 1 ] && INTERVAL_SECONDS=1

[ -n "$LABEL" ] || LABEL="$*"

# --- run the command foreground-equivalent, bounded on the wall clock --------
# We background the command ONLY so this script can enforce the ceiling and own
# the kill; from the caller's perspective this call blocks until the command
# finishes or the ceiling trips (i.e. it behaves like a foreground run with a
# timeout). `set -m` puts the child in its own process group so a timeout can
# signal the whole tree (pytest spawns children); we fall back to the bare pid
# if the group signal isn't deliverable. Liveness is gated SOLELY on `kill -0`
# of the captured pid — never a pgrep, a /proc path, or a content sentinel.
set -m 2>/dev/null || true
"$@" &
child=$!
set +m 2>/dev/null || true

_kill_step() {
    # Tear down a timed-out step and BLOCK until it is gone, bounded by the
    # SIGTERM->SIGKILL grace. We SIGTERM the child's process group first
    # (negative pid; the child leads its own group thanks to `set -m` at
    # launch) so pytest workers and other descendants die too, with the bare
    # pid as a fallback. Then we escalate to SIGKILL from a SHORT-LIVED
    # background timer and BLOCK on `wait "$child"`: a TERM-able step dies in
    # milliseconds (wait returns at once); a TERM-ignoring step dies at the
    # SIGKILL after the grace (wait returns then). Blocking on `wait` — rather
    # than a foreground `sleep` poll — also reaps the child so bash's job
    # control never prints an async "Terminated" notice over our banner.
    kill -TERM "-${child}" 2>/dev/null || kill -TERM "${child}" 2>/dev/null || true
    ( sleep "$KILL_GRACE_SECONDS"
      kill -KILL "-${child}" 2>/dev/null || kill -KILL "${child}" 2>/dev/null || true
    ) >/dev/null 2>&1 &
    killer=$!
    wait "$child" 2>/dev/null || true
    # The step is gone; cancel the (likely still-sleeping) escalation timer and
    # reap it quietly so it cannot leave a stray job notice or process behind.
    kill "$killer" 2>/dev/null || true
    wait "$killer" 2>/dev/null || true
}

elapsed=0
while kill -0 "$child" 2>/dev/null; do
    if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
        _kill_step
        echo "=== REVIEW_STEP_TIMED_OUT ==="
        echo "run_review_step.sh: '${LABEL}' exceeded the ${TIMEOUT_SECONDS}s wall-clock budget and was killed."
        echo "Treat this step as INCONCLUSIVE — emit REVIEW_ESCALATE (do NOT hang, do NOT REVIEW_PASS)."
        echo "=== END_REVIEW_STEP_TIMED_OUT ==="
        {
            echo "=== REVIEW_STEP_TIMED_OUT ==="
            echo "run_review_step.sh: '${LABEL}' exceeded the ${TIMEOUT_SECONDS}s wall-clock budget (still running) and was killed."
            echo "  Failing loudly (exit 124) so the review concludes with REVIEW_ESCALATE rather than wedging the inbox:mirror queue."
        } >&2
        exit 124
    fi
    sleep "$INTERVAL_SECONDS"
    elapsed=$((elapsed + INTERVAL_SECONDS))
done

# Child is gone within budget — reap its real exit code and pass it through.
wait "$child"
exit $?
