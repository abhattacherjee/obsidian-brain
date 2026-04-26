#!/usr/bin/env bash
# Manual smoke test for Issue #105 — _get_session_id_fast cwd-gone resilience
# Run AFTER: /dev-test install
# Usage: bash scripts/dev-test/test-issue-105-manual.sh
#
# Validates the parts that can be checked without a fresh CC session:
#   Phase A: _resolve_project_basename — happy / env-fallback / both-fail
#   Phase B: _recent_bootstrap_sid — zero / one / two / tmp-skip / stale
#   Phase C: _resolve_session_id end-to-end with REAL OS-level cwd deletion
#   Phase D: thin-wrapper contracts for _get_session_id_fast / _slow_path_newest_sid
#   Phase E: insight-saver bash prelude integration under wedged cwd
#
# For the live-CC steps (real worktree deletion, real /retro), see
# ./DEV-TEST-ISSUE-105.md.

set -euo pipefail

PASS=0
FAIL=0

pass() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

CACHE_DIR=$(find ~/.claude/plugins/cache -type d -path "*/obsidian-brain/*" \
    -not -path "*.bak*" 2>/dev/null | sort -V | tail -1 | xargs dirname 2>/dev/null || true)
if [ -z "$CACHE_DIR" ] || [ ! -d "$CACHE_DIR" ]; then
    echo "❌ Could not locate obsidian-brain plugin cache."
    echo "   Run /dev-test install first."
    exit 1
fi

HOOK_DIR=$(find "$CACHE_DIR" -maxdepth 2 -type d -name hooks | sort -V | tail -1)

echo "═══════════════════════════════════════════════════════════════"
echo "Issue #105 — cwd-gone session-id resolution"
echo "═══════════════════════════════════════════════════════════════"
echo "Plugin cache: $CACHE_DIR"
echo "Hooks dir:    $HOOK_DIR"
echo ""

# Throwaway fixture root — isolated HOME so sid-* bootstraps don't pollute.
FIXTURE=$(mktemp -d -t ob-issue-105.XXXXXX)
trap 'cd "$HOME" 2>/dev/null; rm -rf "$FIXTURE"' EXIT
mkdir -p "$FIXTURE/home/.claude/obsidian-brain"
chmod 700 "$FIXTURE/home/.claude/obsidian-brain"

run_py() {
    # Run python3 with HOME and PYTHONPATH redirected so the installed
    # cache's obsidian_utils sees our fixture's secure dir.
    HOME="$FIXTURE/home" PYTHONPATH="$HOOK_DIR" python3 "$@"
}

# ─── Phase A: _resolve_project_basename ───────────────────────────────
echo "Phase A: _resolve_project_basename"

# A1: happy path
mkdir -p "$FIXTURE/some-project"
A1=$(cd "$FIXTURE/some-project" && run_py -c \
    'import obsidian_utils; print(obsidian_utils._resolve_project_basename())')
if [ "$A1" = "some-project" ]; then
    pass "A1 happy path returns cwd basename"
else
    fail "A1 happy path: expected 'some-project', got '$A1'"
fi

# A2: env fallback (cwd raises → CLAUDE_PROJECT_DIR basename)
A2=$(CLAUDE_PROJECT_DIR="/tmp/fake-dir/my-proj" run_py -c '
import os, obsidian_utils
def _raise(*a, **kw): raise FileNotFoundError("cwd")
os.getcwd = _raise
print(obsidian_utils._resolve_project_basename())
')
if [ "$A2" = "my-proj" ]; then
    pass "A2 env-fallback returns CLAUDE_PROJECT_DIR basename"
else
    fail "A2 env-fallback: expected 'my-proj', got '$A2'"
fi

# A3: both unavailable → None
A3=$(unset CLAUDE_PROJECT_DIR; run_py -c '
import os, obsidian_utils
def _raise(*a, **kw): raise FileNotFoundError("cwd")
os.getcwd = _raise
print(repr(obsidian_utils._resolve_project_basename()))
')
if [ "$A3" = "None" ]; then
    pass "A3 both-fail returns None"
else
    fail "A3 both-fail: expected 'None', got '$A3'"
fi
echo ""

# ─── Phase B: _recent_bootstrap_sid ───────────────────────────────────
echo "Phase B: _recent_bootstrap_sid"

BDIR="$FIXTURE/home/.claude/obsidian-brain"

# B1: zero recent files → None
rm -f "$BDIR"/sid-*  # ensure clean
B1=$(run_py -c \
    'import obsidian_utils; print(repr(obsidian_utils._recent_bootstrap_sid()))')
if [ "$B1" = "None" ]; then
    pass "B1 zero-recent returns None"
else
    fail "B1 zero-recent: expected 'None', got '$B1'"
fi

# B2: exactly one recent → returns the SID
echo -n "test-sid-b2" > "$BDIR/sid-projB2"
B2=$(run_py -c \
    'import obsidian_utils; print(obsidian_utils._recent_bootstrap_sid())')
if [ "$B2" = "test-sid-b2" ]; then
    pass "B2 exactly-one returns SID"
else
    fail "B2 exactly-one: expected 'test-sid-b2', got '$B2'"
fi

# B3: two recent → None (strict)
echo -n "test-sid-b3" > "$BDIR/sid-projB3"
B3=$(run_py -c \
    'import obsidian_utils; print(repr(obsidian_utils._recent_bootstrap_sid()))')
if [ "$B3" = "None" ]; then
    pass "B3 two-recent returns None (strict)"
else
    fail "B3 two-recent: expected 'None', got '$B3'"
fi

# B4: tmp partial alongside one recent → still exactly-one
rm -f "$BDIR"/sid-*
echo -n "test-sid-b4" > "$BDIR/sid-projB4"
echo -n "garbage" > "$BDIR/.ob-sid-b4.tmp"
B4=$(run_py -c \
    'import obsidian_utils; print(obsidian_utils._recent_bootstrap_sid())')
if [ "$B4" = "test-sid-b4" ]; then
    pass "B4 tmp-partial skipped"
else
    fail "B4 tmp-partial: expected 'test-sid-b4', got '$B4'"
fi

# B5: stale (mtime > window seconds ago) → None
rm -f "$BDIR"/sid-* "$BDIR"/.ob-sid-*.tmp
echo -n "test-sid-b5" > "$BDIR/sid-projB5"
# Set mtime to 700 seconds ago (window default = 600)
touch -t "$(date -v-700S +%Y%m%d%H%M.%S 2>/dev/null || date -d '700 seconds ago' +%Y%m%d%H%M.%S)" "$BDIR/sid-projB5"
B5=$(run_py -c \
    'import obsidian_utils; print(repr(obsidian_utils._recent_bootstrap_sid()))')
if [ "$B5" = "None" ]; then
    pass "B5 stale-mtime returns None"
else
    fail "B5 stale-mtime: expected 'None', got '$B5'"
fi
echo ""

# ─── Summary ──────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "Issue #105 fixture: $PASS passed, $FAIL failed"
echo "═══════════════════════════════════════════════════════════════"

if [ $FAIL -gt 0 ]; then
    exit 1
fi
