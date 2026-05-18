#!/usr/bin/env bash
# test_push_with_rebase.sh — exercises scripts/_lib_push_with_rebase.sh against
# synthetic git scenarios reproducing Pulse's non-FF push refusal.
#
# Usage:  bash tests/scripts/test_push_with_rebase.sh
# Exit:   0 on all pass, 1 on any failure.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB="$REPO_ROOT/scripts/_lib_push_with_rebase.sh"

if [ ! -f "$LIB" ]; then
    echo "FAIL: lib not found at $LIB"
    exit 1
fi
# shellcheck source=/dev/null
source "$LIB"

PASS=0
FAIL=0

# Make a synthetic remote + two clones (A = our agent, B = the other party
# that races us by pushing first).
#
# Outputs four paths separated by '|':  bare_remote | workspace_A | workspace_B | tmp_root
setup_synth_repos() {
    local tmp
    tmp="$(mktemp -d)"
    local bare="$tmp/remote.git"
    local a="$tmp/a"
    local b="$tmp/b"

    git init -q --bare "$bare"
    # Force the bare HEAD to main so both clones agree on the branch name
    # regardless of the system's init.defaultBranch.
    git -C "$bare" symbolic-ref HEAD refs/heads/main

    # Seed via B: initial commit pushed to the bare remote
    git clone -q "$bare" "$b" 2>/dev/null
    git -C "$b" config user.email "test@example.invalid"
    git -C "$b" config user.name "Test"
    git -C "$b" checkout -q -b main
    echo "initial" > "$b/file.txt"
    git -C "$b" add file.txt
    git -C "$b" commit -q -m "initial"
    git -C "$b" push -q -u origin main

    # Clone the seeded bare into A; configure identity
    git clone -q "$bare" "$a" 2>/dev/null
    git -C "$a" config user.email "test@example.invalid"
    git -C "$a" config user.name "Test"

    echo "$bare|$a|$b|$tmp"
}

cleanup() {
    [ -n "${1:-}" ] && [ -d "$1" ] && rm -rf "$1"
}

# Scenario A: happy path — nobody else pushed, push_with_rebase pushes cleanly.
test_happy_path() {
    local triple
    triple="$(setup_synth_repos)"
    local bare a b tmp
    IFS="|" read -r bare a b tmp <<< "$triple"
    local log="$tmp/test.log"

    cd "$a" || return 1
    echo "local change" >> file.txt
    git add file.txt
    git commit -q -m "A: local commit"

    if push_with_rebase origin main "$log" && \
       grep -q "pushed to origin/main" "$log" && \
       ! grep -q "rebased" "$log"; then
        cleanup "$tmp"
        return 0
    fi
    echo "  -- happy path log --"
    cat "$log"
    cleanup "$tmp"
    return 1
}

# Scenario B: non-FF — origin advanced on a different file, rebase + retry push.
test_non_ff_rebase_retry() {
    local triple
    triple="$(setup_synth_repos)"
    local bare a b tmp
    IFS="|" read -r bare a b tmp <<< "$triple"
    local log="$tmp/test.log"

    # A makes a local commit on a new file
    cd "$a" || return 1
    echo "A content" > a_file.txt
    git add a_file.txt
    git commit -q -m "A: add a_file"

    # B advances origin via a different new file
    cd "$b" || return 1
    git pull -q origin main
    echo "B content" > b_file.txt
    git add b_file.txt
    git commit -q -m "B: add b_file"
    git push -q origin main

    # A now attempts push_with_rebase — first push refused, rebase, retry push
    cd "$a" || return 1
    if push_with_rebase origin main "$log" && \
       grep -q "push refused" "$log" && \
       grep -q "rebased and pushed to origin/main" "$log"; then
        # Confirm A's tree now contains both files (post-rebase)
        if [ -f a_file.txt ] && [ -f b_file.txt ]; then
            cleanup "$tmp"
            return 0
        fi
        echo "  -- expected both a_file.txt and b_file.txt --"
    fi
    echo "  -- non-FF log --"
    cat "$log"
    cleanup "$tmp"
    return 1
}

# Scenario C: rebase conflict — both A and B modified the same line, rebase aborts.
test_rebase_conflict_aborts() {
    local triple
    triple="$(setup_synth_repos)"
    local bare a b tmp
    IFS="|" read -r bare a b tmp <<< "$triple"
    local log="$tmp/test.log"

    cd "$a" || return 1
    echo "A version" > file.txt
    git add file.txt
    git commit -q -m "A: modify file.txt"

    cd "$b" || return 1
    git pull -q origin main
    echo "B version" > file.txt
    git add file.txt
    git commit -q -m "B: modify file.txt"
    git push -q origin main

    cd "$a" || return 1
    # Expect push_with_rebase to return non-zero
    if push_with_rebase origin main "$log"; then
        echo "  FAIL: push_with_rebase returned 0 on conflict"
        echo "  -- conflict log --"
        cat "$log"
        cleanup "$tmp"
        return 1
    fi

    if ! grep -q "rebase failed" "$log"; then
        echo "  FAIL: expected 'rebase failed' in log"
        cat "$log"
        cleanup "$tmp"
        return 1
    fi

    # No rebase-in-progress state should remain
    if [ -d ".git/rebase-merge" ] || [ -d ".git/rebase-apply" ]; then
        echo "  FAIL: rebase state lingers after abort"
        cleanup "$tmp"
        return 1
    fi

    cleanup "$tmp"
    return 0
}

# Scenario D: autostash protects an unrelated unstaged change during rebase.
test_autostash_preserves_unstaged() {
    local triple
    triple="$(setup_synth_repos)"
    local bare a b tmp
    IFS="|" read -r bare a b tmp <<< "$triple"
    local log="$tmp/test.log"

    cd "$a" || return 1
    echo "A new" > a_file.txt
    git add a_file.txt
    git commit -q -m "A: add a_file"

    # Create an unrelated, untracked / unstaged change that should survive autostash
    echo "uncommitted" > scratch.txt
    git add scratch.txt   # autostash handles staged-but-uncommitted

    cd "$b" || return 1
    git pull -q origin main
    echo "B new" > b_file.txt
    git add b_file.txt
    git commit -q -m "B: add b_file"
    git push -q origin main

    cd "$a" || return 1
    if push_with_rebase origin main "$log" && \
       grep -q "rebased and pushed" "$log" && \
       [ -f scratch.txt ] && \
       [ "$(cat scratch.txt)" = "uncommitted" ]; then
        cleanup "$tmp"
        return 0
    fi
    echo "  -- autostash log --"
    cat "$log"
    cleanup "$tmp"
    return 1
}

run() {
    local desc="$1"
    local fn="$2"
    if $fn; then
        PASS=$((PASS+1))
        echo "  PASS: $desc"
    else
        FAIL=$((FAIL+1))
        echo "  FAIL: $desc"
    fi
}

echo "Running tests for push_with_rebase against synthetic git repos..."
run "happy path: clean push, no rebase" test_happy_path
run "non-FF: rebase + retry push succeeds" test_non_ff_rebase_retry
run "rebase conflict: aborts cleanly, no lingering rebase state" test_rebase_conflict_aborts
run "autostash preserves an unrelated unstaged change" test_autostash_preserves_unstaged

echo
echo "----"
echo "passed: $PASS"
echo "failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
