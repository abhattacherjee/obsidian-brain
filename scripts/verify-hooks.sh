#!/usr/bin/env bash
# verify-hooks.sh — manual diagnostic for obsidian-brain SessionStart hook.
#
# Simulates a SessionStart hook invocation with a dummy session_id and
# verifies that the bootstrap file and hook log were written. Does NOT
# need a running Claude Code session.
#
# Usage: ./scripts/verify-hooks.sh
# Exit:  0 on success, non-zero on any failure.
#
# Note on the bootstrap check:
#   load_config() inside the hook calls _get_session_id_fast(), which
#   refreshes the bootstrap file with the authoritative session_id
#   derived from ~/.claude/projects/*. When this script runs inside a
#   live Claude Code session, that overwrite races with our dummy-sid
#   write, so the bootstrap file may end up containing the real sid by
#   the time we read it. The append-only hook log is the authoritative
#   signal that our invocation ran; the bootstrap check is advisory.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO_ROOT/hooks/obsidian_session_hint.py"
PROJECT="$(basename "$(pwd)" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')"
DUMMY_SID="verify-hooks-$(date +%s)"
BOOTSTRAP="/tmp/.obsidian-brain-sid-$PROJECT"
LOG="$HOME/.claude/obsidian-brain-hook.log"

echo "→ Simulating SessionStart hook for project=$PROJECT"
printf '{"cwd": "%s", "session_id": "%s"}' "$(pwd)" "$DUMMY_SID" \
    | python3 "$HOOK" >/dev/null

# Log is append-only and the authoritative proof the hook fired.
# The dummy sid is truncated to 8 chars in the log ("verify-h").
if ! tail -5 "$LOG" 2>/dev/null | grep -q "sid=verify-h"; then
    echo "[FAIL] hook log does not contain a verify-hooks entry" >&2
    echo "  log path: $LOG" >&2
    echo "  last 5 lines:" >&2
    tail -5 "$LOG" >&2 || true
    exit 1
fi

if ! tail -5 "$LOG" 2>/dev/null | grep -q "bootstrap_updated=true"; then
    echo "[FAIL] hook log does not show bootstrap_updated=true" >&2
    echo "  log path: $LOG" >&2
    tail -5 "$LOG" >&2 || true
    exit 1
fi

# Advisory: the bootstrap file should exist (it may have been overwritten
# with the real session sid by load_config(); that is expected).
if [[ ! -f "$BOOTSTRAP" ]]; then
    echo "[WARN] bootstrap file $BOOTSTRAP does not exist"
    echo "  the hook log shows bootstrap_updated=true, so this is unusual"
fi

echo "[OK] Hook fired, bootstrap written, log updated."
echo "  bootstrap: $BOOTSTRAP"
echo "  log:       $LOG"
