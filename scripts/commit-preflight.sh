#!/bin/bash
# Commit Preflight Check
# Must be run before git commit to verify tests pass.
# Creates a one-time token that the require-preflight.py hook validates.
#
# Usage:
#   ./scripts/commit-preflight.sh              # Full verification
#   ./scripts/commit-preflight.sh --docs-only  # Skip tests for docs changes
#   ./scripts/commit-preflight.sh --skip-tests "reason"  # Skip with reason
#   ./scripts/commit-preflight.sh --auto       # Auto-detect if tests needed
#
# Installed by /harden-repo
# Lint/test commands customized for this project during installation.

set -e

# Project-scoped token path (must match require-preflight.py)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_HASH=$(python3 -c "import hashlib; print(hashlib.md5('$(realpath "$PROJECT_DIR")'.encode()).hexdigest()[:8])")
TOKEN_FILE="/tmp/.preflight-token-${PROJECT_HASH}"
TOKEN_EXPIRY_SECONDS=300  # Token valid for 5 minutes

# Parse arguments
SKIP_TESTS=false
SKIP_REASON=""
AUTO_DETECT=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --docs-only)
            SKIP_TESTS=true
            SKIP_REASON="documentation-only changes"
            shift
            ;;
        --skip-tests)
            SKIP_TESTS=true
            SKIP_REASON="$2"
            shift 2
            ;;
        --auto)
            AUTO_DETECT=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--docs-only | --skip-tests \"reason\" | --auto]"
            exit 1
            ;;
    esac
done

echo "🔍 Running commit preflight checks..."
echo ""

# Get staged files
STAGED_FILES=$(git diff --cached --name-only 2>/dev/null || echo "")

if [ -z "$STAGED_FILES" ]; then
    echo "⚠️  No staged files. Stage files first with 'git add'"
    exit 1
fi

echo "📁 Staged files:"
echo "$STAGED_FILES" | head -10
TOTAL=$(echo "$STAGED_FILES" | wc -l | tr -d ' ')
if [ "$TOTAL" -gt 10 ]; then
    echo "   ... and $((TOTAL - 10)) more"
fi
echo ""

# Auto-detect if tests are needed
if [ "$AUTO_DETECT" = true ]; then
    NON_DOC_FILES=$(echo "$STAGED_FILES" | grep -vE '\.(md|txt|json|yaml|yml)$|^docs/|^specs/|^\.claude/|^README|^LICENSE|^\.gitignore' || true)
    if [ -z "$NON_DOC_FILES" ]; then
        echo "📄 Auto-detected: Documentation/config changes only"
        SKIP_TESTS=true
        SKIP_REASON="auto-detected docs/config only"
    else
        echo "🔧 Auto-detected: Code changes present - running tests"
    fi
    echo ""
fi

# Handle skip tests mode
if [ "$SKIP_TESTS" = true ]; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "⏭️  SKIPPING TESTS"
    echo "   Reason: $SKIP_REASON"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    TIMESTAMP=$(date +%s)
    TOKEN_DATA=$(cat <<EOF
{
    "created": $TIMESTAMP,
    "expires": $((TIMESTAMP + TOKEN_EXPIRY_SECONDS)),
    "staged_files": $(echo "$STAGED_FILES" | wc -l | tr -d ' '),
    "checks_run": "skipped",
    "skip_reason": "$(echo "$SKIP_REASON" | sed 's/\\/\\\\/g; s/"/\\"/g')"
}
EOF
)
    echo "$TOKEN_DATA" > "$TOKEN_FILE"

    echo "✅ PREFLIGHT PASSED (tests skipped)"
    echo "📝 You may now run: git commit -m \"your message\""
    echo ""
    exit 0
fi

# Track what we checked
CHECKS_RUN=""
CHECKS_PASSED=true

# ── Secret scanning (always runs) ────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔐 Running secret scan..."
if ./scripts/pre-commit.sh; then
    CHECKS_RUN="${CHECKS_RUN}secrets,"
else
    echo "❌ Secret scan failed"
    CHECKS_PASSED=false
fi

# ── Plugin manifest version sync ─────────────────────────────
# Ensures .claude-plugin/marketplace.json registry pointer stays in lockstep
# with .claude-plugin/plugin.json. Drift has caused the marketplace listing
# to advertise a stale version to users (bug fixed on 2026-04-07).
PLUGIN_JSON="$PROJECT_DIR/.claude-plugin/plugin.json"
MARKETPLACE_JSON="$PROJECT_DIR/.claude-plugin/marketplace.json"
if [ -f "$PLUGIN_JSON" ] && [ -f "$MARKETPLACE_JSON" ]; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "🔖 Checking plugin manifest version sync..."
    # Initialize explicitly so a stale value from an earlier iteration
    # (if this script is ever sourced or extended) can't leak into the
    # check and falsely fail on a clean repo.
    VERSION_SYNC_EXIT=0
    VERSION_CHECK_STDOUT=""
    VERSION_CHECK_STDERR=""
    VERSION_CHECK_TMP=$(mktemp -t preflight-version-XXXXXX)
    VERSION_CHECK_STDOUT=$(python3 - "$PLUGIN_JSON" "$MARKETPLACE_JSON" 2>"$VERSION_CHECK_TMP" <<'PY'
import json, sys, traceback
plugin_path, market_path = sys.argv[1], sys.argv[2]
try:
    plugin = json.load(open(plugin_path))
    market = json.load(open(market_path))
except Exception as e:
    sys.stderr.write(f"parse error: {e}\n")
    sys.exit(2)
plugin_v = plugin.get("version")
plugin_name = plugin.get("name")
if not plugin_v or not plugin_name:
    sys.stderr.write("plugin.json missing 'name' or 'version'\n")
    sys.exit(2)
try:
    entries = [p for p in market.get("plugins", []) if p.get("name") == plugin_name]
    if not entries:
        sys.stderr.write(f"marketplace.json has no entry for '{plugin_name}'\n")
        sys.exit(2)
    market_v = entries[0].get("version")
    if market_v is None:
        sys.stderr.write(f"marketplace.json entry for '{plugin_name}' has no 'version' field\n")
        sys.exit(2)
    if market_v != plugin_v:
        print(f"MISMATCH: plugin.json={plugin_v} marketplace.json={market_v}")
        sys.exit(1)
    print(f"OK: {plugin_name}@{plugin_v}")
except Exception:
    # Defensive: any unexpected crash (KeyError, TypeError on a malformed
    # entry, etc.) goes to stderr with traceback and exits 2 so the shell
    # caller can surface it as a structural error, not a version mismatch.
    sys.stderr.write("unexpected error during version sync check:\n")
    traceback.print_exc(file=sys.stderr)
    sys.exit(2)
PY
) || VERSION_SYNC_EXIT=$?
    VERSION_CHECK_STDERR=$(cat "$VERSION_CHECK_TMP" 2>/dev/null || true)
    rm -f "$VERSION_CHECK_TMP"
    if [ -n "$VERSION_CHECK_STDOUT" ]; then
        echo "$VERSION_CHECK_STDOUT"
    fi
    case "$VERSION_SYNC_EXIT" in
        0)
            CHECKS_RUN="${CHECKS_RUN}version-sync,"
            ;;
        1)
            echo ""
            echo "❌ Plugin manifest versions are out of sync."
            echo "   Update .claude-plugin/marketplace.json to match plugin.json,"
            echo "   or run ./scripts/bump-version.sh which updates both."
            CHECKS_PASSED=false
            ;;
        *)
            echo ""
            echo "❌ Plugin manifest version check failed with a structural error:"
            if [ -n "$VERSION_CHECK_STDERR" ]; then
                echo "$VERSION_CHECK_STDERR" | sed 's/^/   /'
            else
                echo "   (no error output — python exited with code $VERSION_SYNC_EXIT)"
            fi
            echo "   Fix the manifest files before committing."
            CHECKS_PASSED=false
            ;;
    esac
    unset VERSION_SYNC_EXIT VERSION_CHECK_STDOUT VERSION_CHECK_STDERR VERSION_CHECK_TMP
fi

# ══════════════════════════════════════════════════════════════
# LINT SECTION — customized by /harden-repo during installation
# ══════════════════════════════════════════════════════════════
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Running lint checks..."

# __HARDEN_LINT_START__
echo "⏭️  No linter detected — skipping lint"
# __HARDEN_LINT_END__

# ══════════════════════════════════════════════════════════════
# TEST SECTION — customized by /harden-repo during installation
# ══════════════════════════════════════════════════════════════
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🧪 Running tests..."

# __HARDEN_TEST_START__
echo "⏭️  No test runner detected — skipping tests"
# __HARDEN_TEST_END__

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "$CHECKS_PASSED" = false ]; then
    echo "❌ PREFLIGHT FAILED - Fix errors before committing"
    rm -f "$TOKEN_FILE"
    exit 1
fi

# Create confirmation token
TIMESTAMP=$(date +%s)
TOKEN_DATA=$(cat <<EOF
{
    "created": $TIMESTAMP,
    "expires": $((TIMESTAMP + TOKEN_EXPIRY_SECONDS)),
    "staged_files": $(echo "$STAGED_FILES" | wc -l | tr -d ' '),
    "checks_run": "${CHECKS_RUN%,}"
}
EOF
)

echo "$TOKEN_DATA" > "$TOKEN_FILE"

echo ""
echo "✅ PREFLIGHT PASSED"
echo ""
echo "Token created (expires in ${TOKEN_EXPIRY_SECONDS}s)"
echo "📝 You may now run: git commit -m \"your message\""
echo ""
