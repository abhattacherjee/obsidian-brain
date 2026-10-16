#!/bin/bash
# Commit Preflight Check
# Must be run before git commit to verify tests pass.
# Creates a token, tied to the current HEAD, that the require-preflight.py
# hook checks.
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
PROJECT_HASH=$(python3 -c "import hashlib, sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:8])" "$(realpath "$PROJECT_DIR")")
TOKEN_FILE="/tmp/.preflight-token-${PROJECT_HASH}"
# Invalidate the previous result before any check can fail or be interrupted.
# __PREFLIGHT_INVALIDATE_START__
rm -f "$TOKEN_FILE"
# __PREFLIGHT_INVALIDATE_END__
TOKEN_EXPIRY_SECONDS=300  # Token valid for 5 minutes
# The HEAD this preflight ran at. require-preflight allows a commit only
# while HEAD still matches, so a call another hook denies does not spend
# the token (#408). Empty in a repo with no commits yet.
TOKEN_HEAD=$(git -C "$PROJECT_DIR" rev-parse --verify -q HEAD 2>/dev/null || true)
if [ -z "$TOKEN_HEAD" ]; then
    echo "note: no HEAD to record; the token will be spent on the first commit it allows"
fi

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

# ── Plugin manifest version sync (ALWAYS runs, even in skip modes) ──
# Must run BEFORE the --docs-only / --skip-tests / --auto early-exit
# below — those skip modes intentionally bypass tests, but version
# drift between plugin.json and marketplace.json must NEVER be skipped
# regardless of flag. This is the gate that catches the bug PR #14
# was opened to fix; if it lives below the early-exit it provides
# zero protection against any user who runs preflight with a skip flag
# (Copilot iter-4 finding on PR #14).
PLUGIN_JSON_PRE="$PROJECT_DIR/.claude-plugin/plugin.json"
MARKETPLACE_JSON_PRE="$PROJECT_DIR/.claude-plugin/marketplace.json"
VERSION_SYNC_RAN=false
if [ -f "$PLUGIN_JSON_PRE" ] && [ -f "$MARKETPLACE_JSON_PRE" ]; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "🔖 Checking plugin manifest version sync..."
    VERSION_SYNC_EXIT=0
    VERSION_CHECK_TMP=$(mktemp "${TMPDIR:-/tmp}/preflight-version.XXXXXX")
    VERSION_CHECK_STDOUT=$(python3 - "$PLUGIN_JSON_PRE" "$MARKETPLACE_JSON_PRE" "$PROJECT_DIR/.codex-plugin/plugin.json" 2>"$VERSION_CHECK_TMP" <<'PY'
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
from pathlib import Path
codex_path = Path(sys.argv[3])
if plugin_name == "obsidian-brain" and not codex_path.is_file():
    sys.stderr.write("Codex descriptor is missing from obsidian-brain packaging\n")
    sys.exit(2)
if codex_path.is_file():
    try:
        codex = json.loads(codex_path.read_text())
        if codex.get("name") != plugin_name or codex.get("version") != plugin_v:
            sys.stderr.write("Codex descriptor name/version differs from Claude descriptor\n")
            sys.exit(2)
    except (ValueError, OSError, AttributeError) as exc:
        sys.stderr.write(f"Codex descriptor parse error: {exc}\n")
        sys.exit(2)
if not plugin_v or not plugin_name:
    sys.stderr.write("plugin.json missing 'name' or 'version'\n")
    sys.exit(2)
try:
    entries = [p for p in market.get("plugins", []) if p.get("name") == plugin_name]
    if not entries:
        sys.stderr.write(f"marketplace.json has no entry for '{plugin_name}'\n")
        sys.exit(2)
    mismatches = []
    for idx, entry in enumerate(entries):
        market_v = entry.get("version")
        if market_v is None:
            sys.stderr.write(
                f"marketplace.json entry #{idx} for '{plugin_name}' has no 'version' field\n"
            )
            sys.exit(2)
        if market_v != plugin_v:
            mismatches.append((idx, market_v))
    if mismatches:
        details = ", ".join(f"entry#{i}={v}" for i, v in mismatches)
        print(f"MISMATCH: plugin.json={plugin_v} marketplace.json={details}")
        sys.exit(1)
    suffix = "" if len(entries) == 1 else f" ({len(entries)} entries)"
    print(f"OK: {plugin_name}@{plugin_v}{suffix}")
except Exception:
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
            VERSION_SYNC_RAN=true
            ;;
        1)
            echo ""
            echo "❌ Plugin manifest versions are out of sync."
            echo "   Update .claude-plugin/marketplace.json to match plugin.json,"
            echo "   or run ./scripts/bump-version.sh which updates both."
            echo "   This check runs even in --skip-tests modes."
            rm -f "$TOKEN_FILE"
            exit 1
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
            rm -f "$TOKEN_FILE"
            exit 1
            ;;
    esac
    unset VERSION_SYNC_EXIT VERSION_CHECK_STDOUT VERSION_CHECK_STDERR VERSION_CHECK_TMP
    echo ""
fi

# Shared host rules also apply to authored documentation and skip modes.
echo "Checking shared host boundaries..."
if ! python3 "$PROJECT_DIR/scripts/ci-checks/host_neutral_lint.py" --root "$PROJECT_DIR"; then
    rm -f "$TOKEN_FILE"
    exit 1
fi

# Keep the static helper inventory current; collection enforces its actor contracts.
HOST_INVENTORY=$(mktemp "${TMPDIR:-/tmp}/obsidian-host-inventory.XXXXXX")
if ! python3 "$PROJECT_DIR/scripts/ci-checks/host-test-contract.py" --root "$PROJECT_DIR" --output "$HOST_INVENTORY" --parity-matrix "$PROJECT_DIR/docs/parity/capabilities.json"; then
    rm -f "$HOST_INVENTORY" "$TOKEN_FILE"
    exit 1
fi
rm -f "$HOST_INVENTORY"

# The full pytest run below collects with the actor guard before executing any test.
# Keep one guarded collection; a separate collect-only pass duplicates that work.

# Handle skip tests mode
if [ "$SKIP_TESTS" = true ]; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "⏭️  SKIPPING TESTS"
    echo "   Reason: $SKIP_REASON"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # Record the host check and any version check even when tests are skipped.
    # Other checks retain the skipped marker in the audit trail.
    SKIP_CHECKS_RUN="host-neutral,skipped"
    if [ "$VERSION_SYNC_RAN" = true ]; then
        SKIP_CHECKS_RUN="version-sync,host-neutral,skipped"
    fi
    TIMESTAMP=$(date +%s)
    TOKEN_DATA=$(cat <<EOF
{
    "created": $TIMESTAMP,
    "expires": $((TIMESTAMP + TOKEN_EXPIRY_SECONDS)),
    "head": "$TOKEN_HEAD",
    "staged_files": $(echo "$STAGED_FILES" | wc -l | tr -d ' '),
    "checks_run": "$SKIP_CHECKS_RUN",
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
CHECKS_RUN="host-neutral,"
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

# Plugin manifest version sync was already verified above (before the
# skip-tests early-exit) so it cannot be bypassed by --docs-only,
# --skip-tests, or --auto. Record it in CHECKS_RUN for the normal-mode
# token bookkeeping.
if [ "$VERSION_SYNC_RAN" = true ]; then
    CHECKS_RUN="${CHECKS_RUN}version-sync,"
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
if [ -d "tests" ]; then
    PREFLIGHT_TEST_PYTHON="$(command -v python3)"
    if "$PREFLIGHT_TEST_PYTHON" - <<'PY'
import importlib
import sys

missing = []
for name in ("pytest", "pytest_cov", "xdist", "coverage"):
    try:
        importlib.import_module(name)
    except ImportError:
        missing.append(name)
if missing:
    print("Test dependencies unavailable in " + sys.executable + ": " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY
    then
        echo "🧪 Running parallel tests and serial timing controls with coverage..."
        # __PARALLEL_COVERAGE_START__
        if (
            TEST_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ob-preflight-tests.XXXXXXXX")" || exit 1
            echo "Test receipts: $TEST_RUN_DIR"
            mkdir -p "$TEST_RUN_DIR/artifacts/local-0" || exit 1
            export COVERAGE_FILE="$TEST_RUN_DIR/.coverage"
            "$PREFLIGHT_TEST_PYTHON" scripts/ci-checks/test_shards.py list \
                --root "$PROJECT_DIR" --count 1 --index 0 \
                --manifest "$TEST_RUN_DIR/artifacts/local-0/manifest.json" \
                > "$TEST_RUN_DIR/test-files.txt" || exit 1
            PYTHONPATH=tests:hooks:scripts "$PREFLIGHT_TEST_PYTHON" -m pytest tests/ \
                -v --tb=short -n 4 --dist=load --max-worker-restart=0 \
                --deselect=tests/test_security.py::TestDecisionTimeIsBounded:: \
                --deselect=tests/test_security.py::TestPatternDecisionTimeIsBounded:: \
                --basetemp="$TEST_RUN_DIR/pytest-coverage" \
                --cov=hooks --cov-report= --cov-fail-under=0 \
                -p parity_collection_plugin --parity-matrix docs/parity/capabilities.json || exit 1
            PYTHONPATH=tests:hooks:scripts "$PREFLIGHT_TEST_PYTHON" -m pytest \
                tests/test_security.py::TestDecisionTimeIsBounded \
                tests/test_security.py::TestPatternDecisionTimeIsBounded \
                -v --tb=short -p no:xdist \
                --basetemp="$TEST_RUN_DIR/pytest-coverage-timing" \
                --cov=hooks --cov-append --cov-report= --cov-fail-under=0 \
                -p parity_collection_plugin --parity-matrix docs/parity/capabilities.json || exit 1
            cp "$COVERAGE_FILE" "$TEST_RUN_DIR/artifacts/local-0/.coverage" || exit 1
            "$PREFLIGHT_TEST_PYTHON" scripts/ci-checks/test_shards.py coverage \
                --root "$PROJECT_DIR" --count 1 --prefix local \
                --artifacts "$TEST_RUN_DIR/artifacts" || exit 1
        ); then
            CHECKS_RUN="${CHECKS_RUN}tests,"
        else
            echo "❌ Tests failed or coverage below 90%"
            CHECKS_PASSED=false
        fi
        # __PARALLEL_COVERAGE_END__
    else
        echo "❌ Required test dependencies are missing; tests did not start"
        echo "   Install with: $PREFLIGHT_TEST_PYTHON -m pip install -r requirements-dev.txt"
        rm -f "$TOKEN_FILE"
        exit 1
    fi
else
    echo "⏭️  No test directory — skipping tests"
fi
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
    "head": "$TOKEN_HEAD",
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
