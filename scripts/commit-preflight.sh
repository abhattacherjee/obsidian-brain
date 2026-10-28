#!/bin/bash
# Commit Preflight Check
# Must be run before git commit to verify quality checks pass.
# Creates a token that the require-preflight.py hook validates. It covers
# commits of the staged files at the current HEAD, in this checkout, until
# HEAD or the staged files change.
#
# Usage:
#   ./scripts/commit-preflight.sh              # Full verification
#   ./scripts/commit-preflight.sh --docs-only  # Skip tests for docs changes
#   ./scripts/commit-preflight.sh --skip-tests "reason"  # Skip with reason
#   ./scripts/commit-preflight.sh --auto       # Auto-detect if tests needed
#   ./scripts/commit-preflight.sh --allow-empty  # For `git commit --allow-empty`
#                                                # (refuses if anything is staged or modified)
#
# Installed by /harden-repo
# Vendored from harden-repo v1.3.0 (892d0f5); repo parts: version sync, --allow-empty, test block.
# Lint/test commands customized for this project during installation.

set -e

# Project-scoped token path (must match require-preflight.py)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Run from the repo root, whatever directory the caller was in. Everything
# below reaches for repo-relative paths -- ./scripts/pre-commit.sh,
# ./scripts/check-assertion-strength.sh, tests/ -- and running
# `bash ../scripts/commit-preflight.sh` from tests/ broke all of them at once:
# the secret scan died on "No such file or directory" and failed the run, the
# assertion gate announced itself "not installed" while sitting right there in
# scripts/, and pytest errored with "file or directory not found: tests/".
#
# The fourth symptom was the silent one, and the reason this line exists.
# `git ls-files` is scoped to the current directory, so from a subdirectory
# the lint sweep matched ZERO files, printed "no tracked .sh files to lint",
# left LINT_OK true and still appended `lint,` to the preflight token -- the
# token telling require-preflight.py that lint had run when it had inspected
# nothing at all. A fail-open attestation is worse than the three loud
# failures above it.
#
# Safe here because nothing above this line reads the caller's cwd: PROJECT_DIR
# has already resolved to an absolute path, and PROJECT_KEY below asks git with
# an explicit `-C "$PROJECT_DIR"`.
cd "$PROJECT_DIR" || exit 1
# Keyed on the repo's git common dir, NOT on this script's location.
# `--git-common-dir` is the one path identical from a main worktree and from
# every worktree linked to it, so it is what makes this agree with
# require-preflight.py's _get_token_path() in a worktree. Hashing a path meant
# this wrote /tmp/.preflight-token-<worktreeHash> while the hook read
# <mainHash>, blocking every commit made from a worktree immediately after a
# preflight that had just printed PASSED.
# Falls back to the script's own location when git cannot answer (no git, not
# a work tree), which is the pre-existing behaviour for a tarball checkout.
PROJECT_KEY="$(git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [ -n "$PROJECT_KEY" ] && [ -d "$PROJECT_KEY" ]; then
    PROJECT_KEY="$(cd "$PROJECT_KEY" && pwd -P)"
else
    PROJECT_KEY="$(realpath "$PROJECT_DIR" 2>/dev/null || (cd "$PROJECT_DIR" && pwd -P))"
fi
# The first 8 hex characters of md5(key), which require-preflight.py computes
# the same way. python3 is tried first, then `md5 -q -s` (macOS) and `md5sum`.
# This must not fall through to `set -e`: an exit here is BEFORE the token is
# removed, so it would leave an earlier run's token valid. Fail loudly instead.
PROJECT_HASH=$(python3 -c "import hashlib, sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:8])" "$PROJECT_KEY" 2>/dev/null) || PROJECT_HASH=""
if [ -z "$PROJECT_HASH" ]; then
    PROJECT_HASH=$(md5 -q -s "$PROJECT_KEY" 2>/dev/null) || PROJECT_HASH=""
    PROJECT_HASH=${PROJECT_HASH:0:8}
fi
if [ -z "$PROJECT_HASH" ]; then
    PROJECT_HASH=$(printf '%s' "$PROJECT_KEY" | md5sum 2>/dev/null) || PROJECT_HASH=""
    PROJECT_HASH=${PROJECT_HASH:0:8}
fi
if ! [[ "$PROJECT_HASH" =~ ^[0-9a-f]{8}$ ]]; then
    echo "❌ PREFLIGHT ABORTED — cannot compute the token path (python3, md5 and md5sum all failed)." >&2
    echo "   An earlier run's token may still be valid. Delete /tmp/.preflight-token-* and fix the toolchain." >&2
    exit 1
fi
TOKEN_FILE="/tmp/.preflight-token-${PROJECT_HASH}"
TOKEN_EXPIRY_SECONDS=300  # Token valid for 5 minutes

# Invariant: no token exists while the checks are in flight. A token left by an
# EARLIER passing run stays usable for TOKEN_EXPIRY_SECONDS (the commit hook
# accepts any parseable JSON object whose `expires` is in the future), so a run
# that dies halfway -- a bare failing command under `set -e`, Ctrl-C, SIGTERM
# -- must not leave one behind. The token is removed now, before any check, and
# again by the EXIT trap and by the INT/TERM exits. SIGKILL runs no trap; the
# start-of-run removal is what covers it on the next run.
# Not covered: a failure or a signal in the few lines above this `rm` (the
# `cd`, the key and hash lookups, before the trap exists). The hash lookup has
# fallbacks and fails loudly rather than falling through for that reason.
# The only exits that keep the token are the two writes below, which set
# _PREFLIGHT_TOKEN_OK=1 AFTER the write succeeds, so a failed or interrupted
# write is cleaned up too. This is a flag rather than `trap - EXIT` on purpose:
# a marker block may install its own EXIT trap, and clearing every EXIT trap
# here would drop it.
# INT and TERM exit 130 and 143 (the shell convention) and the EXIT trap then
# does the cleanup; they must not fall through and carry on with the checks.
#
# A marker block can replace all three traps. Its own EXIT trap displaces the
# cleanup until the next re-arm point: _preflight_rearm chains the cleanup ahead
# of whatever EXIT trap is installed and re-installs the INT/TERM exits, after
# the lint block, after the test block and after the drift section. So the
# start-of-run removal, not the trap, is what guarantees there is no stale token.
# The cleanup returns the status it was called with, so a block's EXIT trap that
# re-exits (`rc=$?; ...; exit $rc`) must use the status it is given. A signal
# swallowed INSIDE a block cannot be detected from here, so the SKILL.md
# contract says a block's INT/TERM trap must end in `exit`.
_PREFLIGHT_TOKEN_OK=0
_PREFLIGHT_REPORTED=0
_preflight_token_cleanup() {
    # Pass the status it was called with straight through: this is chained
    # AHEAD of a marker block's own EXIT trap, and a block that does
    # `trap 'rc=$?; ...; exit $rc' EXIT` must see the real status, not ours.
    local _rc=$?
    # Top-level shell only. A subshell never inherits an EXIT trap, so this is
    # a guard in case that ever changes: the message must not fire for a
    # subshell that exits nonzero and whose failure the block handled.
    if [ "$_PREFLIGHT_TOKEN_OK" != 1 ] && [ "${BASH_SUBSHELL:-0}" = 0 ]; then
        rm -f "$TOKEN_FILE"
        if [ "$_rc" -ne 0 ] && [ "$_PREFLIGHT_REPORTED" != 1 ]; then
            echo "❌ PREFLIGHT ABORTED (exit $_rc) — no token written" >&2
        fi
    fi
    return "$_rc"
}
# Chain the cleanup ahead of whatever EXIT trap is installed now.
_preflight_rearm_exit() {
    local _cur _words _cmd=""
    _cur=$(trap -p EXIT)
    if [ -n "$_cur" ]; then
        # `trap -p` prints `trap -- 'cmd' EXIT`, quoted so that eval only
        # splits it into words: element 2 is the command.
        eval "_words=($_cur)"
        _cmd=${_words[2]}
    fi
    if [ "${_cmd#*_preflight_token_cleanup}" = "$_cmd" ]; then
        if [ -z "$_cmd" ]; then
            trap _preflight_token_cleanup EXIT
        else
            # The cleanup returns the status it was called with, and under
            # `set -e` a bare nonzero return would end the trap before the
            # block's command ran, skipping its teardown. Making the cleanup
            # an `if` condition suspends that, and `$?` at the top of either
            # branch is the condition's status: the original one. The command
            # is therefore written twice. Newlines, not `;`, so a command that
            # ends in `;` or a comment cannot break the `else`. The `:` after
            # each copy keeps a branch non-empty when the command is blank or
            # only a comment (a syntax error otherwise); it comes AFTER so it
            # cannot reset the `$?` the command reads.
            local _nl=$'\n'
            # shellcheck disable=SC2064  # expanding now is the point: _cmd is the text to chain
            trap "if _preflight_token_cleanup; then${_nl}${_cmd}${_nl}:${_nl}else${_nl}${_cmd}${_nl}:${_nl}fi" EXIT
        fi
    fi
}
_preflight_rearm() {
    _preflight_rearm_exit
    trap 'exit 130' INT
    trap 'exit 143' TERM
}
# Run pytest with any arguments given. With pytest-xdist installed the suite
# runs on half the cores (`-n <cores/2>`, at least 1); without it, serially,
# as before. Most suites start a process or a git repo per test, which splits
# well: harden-repo's own 9,800 tests take about 3 min this way. Half, not
# all: every core busy for minutes left the machine unusable for anything
# else (load average 176 on 12 cores, 2026-10-23). PREFLIGHT_WORKERS sets
# another count. The exit status is pytest's, so a failing test still fails
# the section.
# xdist can be installed but not loaded (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, or
# `-p no:xdist` in pytest.ini). Then pytest rejects `-n` with exit 4, a
# usage error. Only that status triggers a second, serial run; any other
# status is returned as is.
_run_pytest() {
    if python3 -c 'import xdist' 2>/dev/null; then
        local _workers="${PREFLIGHT_WORKERS:-}"
        case "$_workers" in
            ''|*[!0-9]*|0)
                _workers=$(python3 -c 'import os; print(max(1, (os.cpu_count() or 2) // 2))')
                ;;
        esac
        echo "   pytest-xdist found: running tests on $_workers workers"
        local _rc=0
        python3 -m pytest -n "$_workers" "$@" || _rc=$?
        if [ "$_rc" -eq 4 ]; then
            echo "   pytest rejected -n $_workers (xdist not loaded): running tests serially"
            python3 -m pytest "$@"
            return $?
        fi
        return "$_rc"
    else
        echo "   pytest-xdist not installed: running tests serially"
        echo "   (pip install pytest-xdist to run them in parallel)"
        python3 -m pytest "$@"
    fi
}
rm -f "$TOKEN_FILE"
trap _preflight_token_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Parse arguments
SKIP_TESTS=false
SKIP_REASON=""
AUTO_DETECT=false
ALLOW_EMPTY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --docs-only)
            SKIP_TESTS=true
            SKIP_REASON="documentation-only changes"
            shift
            ;;
        --skip-tests)
            if [ -z "${2:-}" ]; then
                echo "--skip-tests needs a reason"
                echo "Usage: $0 [--docs-only | --skip-tests \"reason\" | --auto | --allow-empty]"
                _PREFLIGHT_REPORTED=1
                exit 1
            fi
            SKIP_TESTS=true
            SKIP_REASON="$2"
            shift 2
            ;;
        --auto)
            AUTO_DETECT=true
            shift
            ;;
        --allow-empty)
            ALLOW_EMPTY=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--docs-only | --skip-tests \"reason\" | --auto | --allow-empty]"
            _PREFLIGHT_REPORTED=1
            exit 1
            ;;
    esac
done

echo "🔍 Running commit preflight checks..."
echo ""

# Get staged files
STAGED_FILES=$(git diff --cached --name-only 2>/dev/null || echo "")

# Count non-empty lines: `echo "" | wc -l` would count an empty list as 1.
STAGED_COUNT=$(printf '%s' "$STAGED_FILES" | grep -c '' || true)

if [ "$ALLOW_EMPTY" = true ]; then
    # An empty commit has nothing to scan or test (#116). Ask git directly:
    # the `|| echo ""` above turns a git failure into an empty list, which
    # must not read as "nothing staged" here. Two checks, because the hook
    # only checks the token's expiry and HEAD: an empty index alone would let
    # `git commit -a` or `git commit -m x <path>` commit modified tracked
    # files with no scan or test.
    INDEX_STATE=0
    git diff --cached --quiet 2>/dev/null || INDEX_STATE=$?
    case "$INDEX_STATE" in
        0)
            ;;
        1)
            echo "❌ --allow-empty is for a commit with nothing staged, but files are staged:"
            echo "$STAGED_FILES" | head -10 | sed 's/^/   /'
            echo "   Unstage them, or run preflight without --allow-empty."
            _PREFLIGHT_REPORTED=1
            exit 1
            ;;
        *)
            echo "❌ Could not read the git index (git exited $INDEX_STATE)."
            _PREFLIGHT_REPORTED=1
            exit 1
            ;;
    esac
    TREE_STATE=0
    git diff --quiet 2>/dev/null || TREE_STATE=$?
    case "$TREE_STATE" in
        0)
            ;;
        1)
            echo "❌ --allow-empty needs a clean working tree: tracked files have unstaged changes, and 'git commit -a' or a pathspec would commit them unchecked:"
            git diff --name-only | head -10 | sed 's/^/   /'
            echo "   Commit or stash them, or run preflight without --allow-empty."
            _PREFLIGHT_REPORTED=1
            exit 1
            ;;
        *)
            echo "❌ Could not read the working tree (git exited $TREE_STATE)."
            _PREFLIGHT_REPORTED=1
            exit 1
            ;;
    esac
    echo "📭 Nothing staged: preflight for 'git commit --allow-empty'"
    echo ""
    SKIP_TESTS=true
    SKIP_REASON="empty commit (--allow-empty)"
    AUTO_DETECT=false
elif [ -z "$STAGED_FILES" ]; then
    echo "⚠️  No staged files. Stage files first with 'git add'"
    echo "   (For an empty commit, run: $0 --allow-empty)"
    _PREFLIGHT_REPORTED=1
    exit 1
else
    echo "📁 Staged files:"
    echo "$STAGED_FILES" | head -10
    if [ "$STAGED_COUNT" -gt 10 ]; then
        echo "   ... and $((STAGED_COUNT - 10)) more"
    fi
    echo ""
fi

# What this run checks, recorded in the token (#128). require-preflight.py
# keeps the token across commit calls only while HEAD, the staged tree and
# this checkout all still match it, so a commit call that another hook denies
# does not spend it. The tree is read here, before any check runs, and again
# just before the token is written (_preflight_write_token): a check that
# stages a file would otherwise leave a token naming a tree nobody checked.
# HEAD is empty on an unborn branch; the hook spends such a token on its
# first commit.
TOKEN_HEAD=$(git rev-parse --verify -q HEAD 2>/dev/null || true)
# stdout and stderr together, so git's own error can be shown without a temp
# file (an unwritable TMPDIR must reach the drift check's own message). On
# success the output is the tree id alone; anything else fails closed.
if ! _wt_out=$(git write-tree 2>&1) || ! [[ "$_wt_out" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then
    echo "❌ PREFLIGHT FAILED — cannot read the staged tree (git write-tree failed; unmerged files?)"
    printf '%s\n' "$_wt_out" | sed 's/^/   git: /' || true
    _PREFLIGHT_REPORTED=1
    exit 1
fi
TOKEN_TREE=$_wt_out
if ! TOKEN_WORKTREE=$(git rev-parse --show-toplevel 2>/dev/null) || [ -z "$TOKEN_WORKTREE" ]; then
    echo "❌ PREFLIGHT FAILED — cannot read this checkout's toplevel"
    _PREFLIGHT_REPORTED=1
    exit 1
fi

# Write the token: a temp file in the same directory, then a rename, so the
# hook never reads a half-written token. JSON comes from python, not a shell
# heredoc, because the worktree path may hold a quote or a backslash.
# Refuses -- no token, exit 1 -- when the staged tree is no longer the one
# read before the checks.
# Args: created, staged file count, checks_run, skip_reason ("" for none).
_preflight_write_token() {
    local _tree_now
    if ! _tree_now=$(git write-tree 2>/dev/null) || [ "$_tree_now" != "$TOKEN_TREE" ]; then
        echo "❌ PREFLIGHT FAILED — the staged files changed while preflight ran; no token written."
        echo "   Stage everything first, then run preflight again."
        _PREFLIGHT_REPORTED=1
        exit 1
    fi
    python3 -E - "$TOKEN_FILE" "$1" "$TOKEN_EXPIRY_SECONDS" "$2" "$3" "$4" \
        "$TOKEN_HEAD" "$TOKEN_TREE" "$TOKEN_WORKTREE" <<'PY'
import json, os, sys, tempfile
path, created, ttl, staged, checks, reason, head, tree, worktree = sys.argv[1:10]
token = {"created": int(created), "expires": int(created) + int(ttl),
         "staged_files": int(staged), "checks_run": checks}
if reason:
    token["skip_reason"] = reason
token.update(head=head, tree=tree, worktree=os.path.realpath(worktree))
fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                           dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(token, handle, indent=4)
        handle.write("\n")
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
}

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
    VERSION_CHECK_STDOUT=$(python3 - "$PLUGIN_JSON_PRE" "$MARKETPLACE_JSON_PRE" 2>"$VERSION_CHECK_TMP" <<'PY'
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
            _PREFLIGHT_REPORTED=1
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
            _PREFLIGHT_REPORTED=1
            exit 1
            ;;
    esac
    unset VERSION_SYNC_EXIT VERSION_CHECK_STDOUT VERSION_CHECK_STDERR VERSION_CHECK_TMP
    echo ""
fi

# Handle skip tests mode
if [ "$SKIP_TESTS" = true ]; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "⏭️  SKIPPING TESTS"
    echo "   Reason: $SKIP_REASON"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # version-sync is the one check never skipped; record it when it ran.
    SKIP_CHECKS_RUN="skipped"
    if [ "$VERSION_SYNC_RAN" = true ]; then
        SKIP_CHECKS_RUN="version-sync,skipped"
    fi
    TIMESTAMP=$(date +%s)
    _preflight_write_token "$TIMESTAMP" "$STAGED_COUNT" "$SKIP_CHECKS_RUN" "$SKIP_REASON"
    _PREFLIGHT_TOKEN_OK=1

    echo "✅ PREFLIGHT PASSED (tests skipped)"
    echo "📝 You may now run: git commit -m \"your message\""
    echo ""
    exit 0
fi

# Track what we checked
CHECKS_RUN=""
CHECKS_PASSED=true

# Plugin manifest version sync already ran above, before the skip-mode exit,
# so no flag can bypass it. Record it here for the normal-mode token.
if [ "$VERSION_SYNC_RAN" = true ]; then
    CHECKS_RUN="${CHECKS_RUN}version-sync,"
fi

# ── Secret scanning (always runs) ────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔐 Running secret scan..."
if ./scripts/pre-commit.sh; then
    CHECKS_RUN="${CHECKS_RUN}secrets,"
else
    echo "❌ Secret scan failed"
    CHECKS_PASSED=false
fi

# ── Assertion-strength gate (WARN by default) ────────────────
# `assertions,` is only ever appended to CHECKS_RUN when the gate actually ran
# to completion — the preflight token is the hook's commit authorization, so it
# must never attest to a check that was skipped or that crashed.
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔬 Running assertion-strength gate..."
ASSERT_MODE="${ASSERT_GATE:-warn}"
if [ ! -f ./scripts/check-assertion-strength.sh ]; then
    if [ "$ASSERT_MODE" = "enforce" ]; then
        echo "❌ check-assertion-strength.sh is not installed — ASSERT_GATE=enforce requires it"
        CHECKS_PASSED=false
    else
        echo "⏭️  check-assertion-strength.sh not installed — skipping"
    fi
elif [ ! -x ./scripts/check-assertion-strength.sh ]; then
    if [ "$ASSERT_MODE" = "enforce" ]; then
        echo "❌ check-assertion-strength.sh is present but not executable — ASSERT_GATE=enforce requires it"
        echo "   Fix with: chmod +x ./scripts/check-assertion-strength.sh"
        CHECKS_PASSED=false
    else
        echo "⏭️  check-assertion-strength.sh is present but not executable — skipping"
        echo "   Fix with: chmod +x ./scripts/check-assertion-strength.sh"
    fi
else
    if ./scripts/check-assertion-strength.sh; then
        ASSERT_RC=0
    else
        ASSERT_RC=$?
    fi
    if [ "$ASSERT_RC" -eq 0 ]; then
        CHECKS_RUN="${CHECKS_RUN}assertions,"
    elif [ "$ASSERT_RC" -eq 1 ]; then
        # Exit 1 means findings, which the gate only returns under
        # ASSERT_GATE=enforce. In WARN mode findings are informational and
        # must never fail the commit.
        if [ "$ASSERT_MODE" = "enforce" ]; then
            echo "❌ Assertion-strength gate failed (ASSERT_GATE=enforce)"
            CHECKS_PASSED=false
        else
            echo "⚠️  Weak assertions found (WARN mode — not blocking)"
            CHECKS_RUN="${CHECKS_RUN}assertions,"
        fi
    else
        # Any other exit code means the gate itself broke (syntax error, no
        # grep, ...). That is not a finding — report it as the failure it is
        # and do not attest to a check that never ran.
        echo "❌ Assertion-strength gate CRASHED (exit $ASSERT_RC) — the check did not run"
        if [ "$ASSERT_MODE" = "enforce" ]; then
            CHECKS_PASSED=false
        fi
    fi
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
_preflight_rearm

# ══════════════════════════════════════════════════════════════
# TEST SECTION — customized by /harden-repo during installation
# ══════════════════════════════════════════════════════════════
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🧪 Running tests..."

# __HARDEN_TEST_START__
if [ -d "tests" ]; then
    if command -v pytest &>/dev/null; then
        echo "🧪 Running pytest with coverage..."
        if pytest tests/ -v --tb=short --cov=hooks --cov-report=term-missing --cov-fail-under=90; then
            CHECKS_RUN="${CHECKS_RUN}tests,"
        else
            echo "❌ Tests failed or coverage below 90%"
            CHECKS_PASSED=false
        fi
    else
        echo "❌ tests/ directory exists but pytest is not installed"
        echo "   Install with: pip install pytest pytest-cov"
        CHECKS_PASSED=false
    fi
else
    echo "⏭️  No test directory — skipping tests"
fi
# __HARDEN_TEST_END__
_preflight_rearm

# ══════════════════════════════════════════════════════════════
# TEMPLATE DRIFT — advisory only, NEVER blocks
# ══════════════════════════════════════════════════════════════
# Reports when this repo's harden-repo-installed hooks and scripts have
# fallen behind the templates they came from.
#
# Why this lives here rather than in a SessionStart hook: this script is
# already installed in every hardened repo and already runs before every
# commit, so wiring the check in costs no new settings.json entry and no
# per-session latency. It fires exactly when you are about to change code.
#
# This section MUST NOT set CHECKS_PASSED=false, and no branch below does.
# Drift is work to schedule, not a reason to block the commit in front of
# you -- and a check that can wedge every commit across every hardened repo
# is a worse failure than the drift it reports.
#
# That last sentence is why the doctor runs under a wall-clock deadline. It
# is a subprocess, and until this timeout existed it had no bound at all: a
# doctor that hangs -- on a wedged git call, a stalled network filesystem, a
# future doctor that waits on something -- blocked every commit in every
# repo carrying this script, indefinitely, with no way out but Ctrl-C.
# macOS ships no timeout(1), so the deadline is enforced here in shell:
# background the doctor, poll for it, kill it when the clock runs out. A run
# that was killed produced no verdict, so it scores UNKNOWN and attests
# nothing -- absent evidence is never health, exactly as for exit 2.
#
# doctor.py exits 1 both when it found something AND when the repo is not
# hardened at all, i.e. when nothing was checked -- so `set -e` needs the
# guard on the substitution either way, and exit 1 alone is never evidence
# of anything.
#
# As invoked here -- without --fix -- exit 2 means the doctor could not read
# something and produced no verdict. That is reported as "could not check",
# never as "no drift": absent evidence is never health. Anything that adds
# --fix to this invocation widens exit 2 to cover refused and aborted
# repairs too, and must revisit the branch below that assumes otherwise.
#
# A verdict has THREE shapes, not two: OK, DRIFT and DEGRADED. Each is
# dispatched by its own branch below, and each attests `drift,` in the
# preflight token. Everything else is UNKNOWN and attests nothing.
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🩺 Checking for template drift..."

DOCTOR_PATH="${CLAUDE_PLUGIN_ROOT:-}/skills/harden-repo/scripts/doctor.py"
# Wall-clock seconds the doctor gets. Generous against a real run -- the
# doctor hashes a few dozen small files -- and short enough that a hang costs
# one pause rather than the commit. Raising it raises the worst-case delay on
# every commit in every hardened repo.
DRIFT_TIMEOUT=10
if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ] || [ ! -f "$DOCTOR_PATH" ]; then
    # Outside a Claude Code session with the repo-hardener plugin loaded
    # there is no template to compare against. Same shape as the shellcheck
    # and pytest guards above: a missing tool skips, it never fails.
    echo "⏭️  repo-hardener plugin not loaded — skipping drift check"
else
    # `--repo "$PROJECT_DIR"`, never `--repo .`: the preflight is routinely
    # run from a subdirectory, and a cwd-relative --repo points the doctor
    # at a directory with no scripts/ and no .claude/hooks/, which it
    # correctly reports as "not hardened" -- a fully hardened repo scoring
    # as nothing-to-check.
    #
    # stderr is captured rather than discarded because it carries the only
    # actionable diagnostic in the exit-2 case: a stale CLAUDE_PLUGIN_ROOT
    # and an internally corrupt shipped template say different things and
    # need different fixes. Throwing both away collapsed them into one
    # generic line nobody could act on. The temp file is removed on every
    # path below.
    DRIFT_ERR=$(mktemp "${TMPDIR:-/tmp}/preflight-drift.XXXXXX" 2>/dev/null || printf '')
    # stdout needs a file too, not a command substitution: the doctor runs in
    # the BACKGROUND so this shell can watch the clock, and `$(...)` blocks
    # until the child exits, which is the exact thing the deadline exists to
    # avoid. Same mktemp fallback as stderr, and the same reason for tracking
    # whether it worked: with stdout pointed at /dev/null the doctor's report
    # is gone, and "the doctor said nothing" and "nobody was listening" must
    # not print the same line.
    DRIFT_OUT=$(mktemp "${TMPDIR:-/tmp}/preflight-drift-out.XXXXXX" 2>/dev/null || printf '')
    DOCTOR_OUT_CAPTURED=1
    if [ -z "$DRIFT_OUT" ]; then
        DRIFT_OUT="/dev/null"
        DOCTOR_OUT_CAPTURED=0
    fi
    # DOCTOR_ERR_CAPTURED is the difference between "the doctor said nothing"
    # and "nobody was listening". mktemp fails on an unwritable or missing
    # TMPDIR, and the fallback below points the doctor stderr at /dev/null,
    # where those two look identical. The guards further down used to read
    # the second as the first, so a doctor that printed a clean report, wrote
    # a traceback and exited 1 was announced as "installed artifacts match
    # the shipped templates".
    DOCTOR_ERR_CAPTURED=1
    if [ -z "$DRIFT_ERR" ]; then
        DRIFT_ERR="/dev/null"
        DOCTOR_ERR_CAPTURED=0
    fi
    # The straight-line `rm` below covers every branch this script takes, but
    # not a Ctrl-C or a SIGTERM between the mktemp and it, which would leave
    # the file behind on every interrupted preflight.
    #
    # The trap already installed is SAVED and put back when this section is
    # done, rather than cleared. This file is a TEMPLATE: the
    # __HARDEN_LINT__ and __HARDEN_TEST__ blocks above are replaced per
    # consumer repo with commands this plugin does not control, and a lint or
    # test block that sets its own EXIT trap to tear down a scratch venv or a
    # temp database is ordinary. A blind `trap - EXIT` here removes it with
    # no sign that anything happened.
    #
    # The /dev/null guard is load-bearing: that is the mktemp-failure
    # fallback, and `rm -f /dev/null` on a machine running this as root would
    # delete the device node.
    _DRIFT_PREV_TRAP=$(trap -p EXIT)
    trap '_preflight_token_cleanup || :; [ "${DRIFT_ERR:-/dev/null}" = "/dev/null" ] || rm -f "$DRIFT_ERR"; [ "${DRIFT_OUT:-/dev/null}" = "/dev/null" ] || rm -f "$DRIFT_OUT"' EXIT
    DOCTOR_RC=0
    DOCTOR_TIMED_OUT=0
    # `-E` is load-bearing, not tidiness. It makes the interpreter ignore
    # every PYTHON* environment variable, and the inherited environment is
    # not this script's to control. With PYTHONVERBOSE=1 exported, a
    # completely clean doctor run writes tens of kilobytes of import tracing
    # to stderr (~38 KB as measured once on one CPython 3 build on macOS; the
    # exact figure is a property of the interpreter version, not a constant)
    # -- and the "clean report, then wrote to stderr" guard below would then
    # report UNKNOWN on every commit in every hardened repo, forever.
    # PYTHONVERBOSE is the only variable measured to do that:
    # PYTHONWARNINGS and PYTHONDEVMODE each wrote 0 bytes of stderr on the
    # same clean run, so they are not a quieter version of the problem, they
    # are not a problem at all today. `-E` still blocks the lot, because
    # which PYTHON* variable is noisy is a property of the interpreter
    # version and not something this script can pin. doctor.py puts its own
    # directory on sys.path itself, so it never needed PYTHONPATH: nothing
    # is lost by ignoring the environment. Do not delete this flag as noise.
    #
    # Backgrounded and watched rather than run inline. The poll is on the WALL
    # CLOCK, not on an iteration count, because the sleep granularity is not
    # guaranteed: `sleep 0.1` works on every shell measured here, but a POSIX
    # sleep need only accept whole seconds, and counting iterations would turn
    # a 10s deadline into a 100s one wherever the fraction is refused. `date`
    # is asked the time on each pass instead, so the deadline holds either way
    # and only the polling granularity changes.
    python3 -E "$DOCTOR_PATH" --repo "$PROJECT_DIR" --json >"$DRIFT_OUT" 2>"$DRIFT_ERR" &
    DOCTOR_PID=$!
    DRIFT_DEADLINE=$(( $(date +%s) + DRIFT_TIMEOUT ))
    while kill -0 "$DOCTOR_PID" 2>/dev/null; do
        if [ "$(date +%s)" -ge "$DRIFT_DEADLINE" ]; then
            DOCTOR_TIMED_OUT=1
            break
        fi
        sleep 0.1 2>/dev/null || sleep 1
    done
    if [ "$DOCTOR_TIMED_OUT" = 1 ]; then
        # TERM first, KILL only if it is ignored: a doctor holding a lock or a
        # temp file deserves the chance to clean up, and SIGKILL denies it.
        # Both are `|| true` -- the process may exit between the check and the
        # signal, and `set -e` would turn that race into a failed preflight.
        kill -TERM "$DOCTOR_PID" 2>/dev/null || true
        _DRIFT_GRACE=$(( $(date +%s) + 2 ))
        while kill -0 "$DOCTOR_PID" 2>/dev/null && [ "$(date +%s)" -lt "$_DRIFT_GRACE" ]; do
            sleep 0.1 2>/dev/null || sleep 1
        done
        kill -KILL "$DOCTOR_PID" 2>/dev/null || true
    fi
    # `wait` reaps the child and yields its status. Guarded because a killed
    # child exits nonzero and this script runs under `set -e`; the status is
    # not read on the timeout path, where DOCTOR_TIMED_OUT is the verdict.
    wait "$DOCTOR_PID" 2>/dev/null || DOCTOR_RC=$?
    # Read back what the run managed to print. `|| printf ''` covers the
    # /dev/null fallback and an unreadable file alike: an empty DRIFT_JSON
    # reaches the parser, which reports UNKNOWN rather than guessing.
    DRIFT_JSON=$(cat "$DRIFT_OUT" 2>/dev/null || printf '')
    # Whether the doctor wrote ANYTHING is `-s` on a real file, never the
    # emptiness of `head -1`. head -1 of a stderr whose first line is blank
    # is the empty string, which is indistinguishable from a silent doctor --
    # so a traceback with one blank line in front of it scored as health.
    # DOCTOR_ERR_SEEN answers "did it write"; DOCTOR_ERR_LINE is only what to
    # show.
    DOCTOR_ERR_SEEN=0
    if [ "$DOCTOR_ERR_CAPTURED" = 1 ] && [ -s "$DRIFT_ERR" ]; then
        DOCTOR_ERR_SEEN=1
    fi
    # The first NON-EMPTY line, for the same reason: a leading blank line
    # must not turn the diagnostic into an empty string.
    DOCTOR_ERR_LINE=$(grep -m1 . "$DRIFT_ERR" 2>/dev/null || printf '')
    # ...except for a Python traceback, where the first line is the banner
    # and carries nothing: "Traceback (most recent call last):" tells the
    # developer that the doctor died and not one thing about why. The line
    # that matters is the LAST one, "ExceptionType: message" -- for the
    # dominant failure, an uncaught exception, that is the whole diagnostic.
    # Only the banner triggers this. For anything else the first line is
    # normally the relevant one and the last is trailing noise.
    if [ "$DOCTOR_ERR_LINE" = "Traceback (most recent call last):" ]; then
        DOCTOR_ERR_LINE=$(grep . "$DRIFT_ERR" 2>/dev/null | tail -1 || printf '')
    fi
    # Set by any branch below that already puts DOCTOR_ERR_LINE in its
    # message, so the follow-on line at the end of the section does not
    # print the same text twice.
    DOCTOR_ERR_SHOWN=0
    [ "$DRIFT_ERR" = "/dev/null" ] || rm -f "$DRIFT_ERR"
    [ "$DRIFT_OUT" = "/dev/null" ] || rm -f "$DRIFT_OUT"
    # Restore the caller EXIT trap rather than leaving the consumer repo
    # with none. `eval` of an empty string is a no-op, which is the right
    # answer when there was no previous trap.
    trap - EXIT
    eval "${_DRIFT_PREV_TRAP:-}"
    _preflight_rearm

    if [ "$DOCTOR_TIMED_OUT" = 1 ]; then
        # First, ahead of every other branch: a killed process exits nonzero,
        # so the exit-code branch would claim it and print "could not check
        # (exit 143)" -- true, and useless. A hang and an unreadable ledger
        # need different responses from the reader, so they must not print the
        # same line. Nothing this run printed is scored -- a report cut off
        # mid-write is not a verdict -- and no branch below attests `drift,`
        # for this message.
        DRIFT_MSG="doctor timed out after ${DRIFT_TIMEOUT}s — drift is UNKNOWN, not clean"
    elif [ "$DOCTOR_RC" -ge 2 ]; then
        # stdout is deliberately NOT parsed here. Exit 2 means the run has
        # no verdict to parse, and whatever it managed to print before
        # giving up must not be scored.
        if [ "$DOCTOR_ERR_SEEN" = 1 ]; then
            DRIFT_MSG="doctor could not check (exit ${DOCTOR_RC}): ${DOCTOR_ERR_LINE:-stderr held only blank lines}"
            DOCTOR_ERR_SHOWN=1
        elif [ "$DOCTOR_ERR_CAPTURED" = 0 ]; then
            # NOT the same as a silent doctor, and reporting it as one is a
            # lie: nothing was ever listening. mktemp failed, stderr went to
            # /dev/null, and the diagnostic that would name this failure was
            # discarded before it was written.
            DRIFT_MSG="doctor could not check (exit ${DOCTOR_RC}): stderr could not be captured (mktemp failed), so its diagnostic is lost"
        else
            DRIFT_MSG="doctor could not check (exit ${DOCTOR_RC}): no diagnostic on stderr"
        fi
    elif [ "$DOCTOR_OUT_CAPTURED" = 0 ]; then
        # Same distinction the stderr side already draws: the doctor may have
        # produced a perfect report, and it went to /dev/null because mktemp
        # failed. Reporting that as "could not parse doctor output" would
        # blame the doctor for the shell's problem.
        DRIFT_MSG="doctor output could not be captured (mktemp failed) — drift is UNKNOWN, not clean"
    else
    # Everything from the opening quote below to the `') || DRIFT_MSG=` line
    # is ONE single-quoted bash string. A single apostrophe anywhere inside
    # it -- in code, in a comment, in a message -- closes that string early
    # and breaks this script, usually as a syntax error hundreds of lines
    # further down. So: no contractions, no possessives, no quoted words in
    # there. tests/test_embedded_parser.py enforces the rule and also parses
    # the extracted block with ast.parse, which is the only syntax gate the
    # embedded Python gets: shellcheck does not read inside the string and
    # py_compile never sees a .sh file.
    DRIFT_MSG=$(printf '%s' "$DRIFT_JSON" | python3 -E -c '
import json, sys

# Force UTF-8 on stdout whatever the process locale is. Every line this
# parser prints except OK and DRIFT carries an em dash, and under a
# non-UTF-8 locale -- LC_ALL=en_SG.ISO8859-1 gives stdout the iso8859-1
# codec -- the first such print raises UnicodeEncodeError. The parser then
# dies mid-report and the shell falls back to "could not parse doctor
# output", so every DEGRADED verdict, every note: line and every UNKNOWN
# diagnostic is lost. Only the pure-ASCII OK and DRIFT lines survive, and
# the check can then never say anything but clean, drift or could-not-parse.
# The -E flag above does not help: it blocks PYTHON* variables, and the
# locale arrives through LC_ALL and LANG. This is the same guard doctor.py
# installs at the top of main(), for the same reason, hasattr-guarded the
# same way.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def flat(value):
    """Doctor-controlled text, collapsed onto a single line.

    Newlines are what let doctor text forge a line this parser did not
    write: a second line the shell might read as the verdict, or a line
    reading exactly __UNRECOGNIZED__, which is the marker the shell greps
    for with ^...$ to decide whether to print the destructive-flag caveat.
    Every interpolation of a doctor string below goes through this.
    """
    return " ".join(str(value).split())


def counted(value):
    """len() over whatever the doctor put in a list field.

    A mistyped field must reach the disagreement guard below and be
    reported, not raise here and lose the diagnostic.
    """
    try:
        return len(value)
    except TypeError:
        return 0


# Every branch here demands POSITIVE confirmation of health, and a verdict
# has THREE shapes. Naming only two was how `DRIFT 0` -- a repair prompt for
# a repo with nothing to repair -- got printed for months:
#
#   OK          the doctor said it checked the repo, gave a findings count,
#               and that count and all three of its lists agree on zero.
#   DRIFT n     a real count of artifact and/or settings.json findings.
#   DEGRADED n  checked, zero findings, but the run carried warnings. Today
#               that means the template ledger could not be read, so drift
#               could not be attributed to a released version.
#
# Anything else prints UNKNOWN: absent output, unparseable output, output
# that is not a report, a missing summary, a report that says it checked
# nothing (not hardened, or a summary with no valid `checked` field -- in
# practice the most common outcome of all, and the one branch here whose
# text comes from the doctor), a missing count, or a count that disagrees
# with the lists in EITHER direction.
#
# Line 1 of whatever this prints IS the verdict: the shell dispatches on its
# leading token. So every exit path below prints a line 1 whose leading
# token is drawn from a fixed vocabulary -- exactly `OK`, or a `DRIFT ` or
# `DEGRADED ` prefix -- and every doctor-derived string appears only AFTER a
# fixed literal that cannot itself be one of those tokens, with its newlines
# collapsed by flat(). Without that, a doctor whose "I checked nothing"
# message happened to read `OK` would be dispatched as the clean verdict.
#
# DEGRADED DOES attest `drift,` in the preflight token, and that is
# deliberate. The artifact verdict itself is reliable: IN-SYNC is decided by
# hashing the normalized file against the shipped template and never
# consults the ledger. Only the version ATTRIBUTION -- "this matches the
# v1.0.7 generation" -- degrades. So the question the token answers, "did
# the drift check produce a verdict", is honestly yes. What DEGRADED must
# NOT do is print a repair command: there are zero findings to repair, and
# the ledger lives in the plugin, not in this repo, so --fix cannot reach
# it. That is why it is its own branch and not a DRIFT with a count of 0.
#
# That rationale rests on something worth stating, because it is not
# guaranteed: every warn() call site in the doctor today lives in
# load_ledger, so a warning means exactly "version attribution degraded",
# and a ledger the doctor could not read can only over-report drift, never
# manufacture a false IN-SYNC. If a future warning ever means anything else
# -- a partial scan, an unreadable artifact -- that reasoning breaks and
# this branch must stop attesting `drift,` until it is re-established.
raw = sys.stdin.read().strip()
if not raw:
    print("could not run the doctor — drift is UNKNOWN, not clean")
    raise SystemExit(0)
try:
    report = json.loads(raw)
except ValueError:
    print("doctor output was unreadable — drift is UNKNOWN, not clean")
    raise SystemExit(0)
if not isinstance(report, dict):
    print("doctor output was not a report — drift is UNKNOWN, not clean")
    raise SystemExit(0)

summary = report.get("summary")
if not isinstance(summary, dict):
    print("doctor output had no summary — drift is UNKNOWN, not clean")
    raise SystemExit(0)

# summary.checked is the field that carries this decision, and the test is
# `is not True` rather than `is False` on purpose: a summary with no checked
# key at all, or a null one, must land here too. checked=false means nothing
# is installed to compare, which is not the same as being in sync.
# (summary.findings is read below as the COUNT; it is null on the
# not-hardened branch, which is a second, independent reason that branch
# cannot be scored as clean -- but it is not what protects this one.)
if summary.get("checked") is not True:
    # The message is free text the DOCTOR controls, so it can never be
    # its own. Printed raw, a doctor whose message read "OK" was dispatched
    # by the shell as the clean verdict -- green tick, `drift,` written into
    # the commit authorization -- over a run that examined nothing. The
    # fixed prefix below is not decoration; it is what makes this line
    # unreadable as a verdict token.
    print("not a verdict — %s (drift is UNKNOWN, not clean)"
          % flat(summary.get("message") or "nothing installed to check"))
    raise SystemExit(0)

# bool is excluded explicitly: isinstance(True, int) is True in Python, so a
# summary carrying "findings": true would otherwise be read as the count 1.
total = summary.get("findings")
if isinstance(total, bool) or not isinstance(total, int):
    print("doctor reported no findings count — drift is UNKNOWN, not clean")
    raise SystemExit(0)

# The doctor reports problems in three places, and summary.findings counts
# the first two. settings holds hook-wiring problems (a hook installed but
# never registered in settings.json), warnings holds "I could not read the
# ledger" -- neither is artifact drift, and neither is health.
findings = report.get("findings") or []
settings = report.get("settings") or []
warnings = report.get("warnings") or []

# IN-SYNC rows are dropped before anything below counts them. The doctor
# already filters them out of --json unless -v is passed, so today this
# changes nothing -- and that is precisely the trap. summary.findings counts
# ACTIONABLE findings only; add -v to the invocation above and a PERFECTLY
# CLEAN repo comes back with total=0 and every artifact listed in findings,
# which the disagreement guard below would read as a self-contradictory
# report and print UNKNOWN on every commit in every hardened repo, forever.
# Filtering here makes the list agree with the count whatever flags the
# invocation grows.
if isinstance(findings, list):
    findings = [f for f in findings
                if not (isinstance(f, dict) and f.get("state") == "IN-SYNC")]

# The count and the lists have to agree in BOTH directions, and a report
# where they do not is broken, not clean. `total == 0` with rows in the
# lists is one half of it. The other half -- a count with empty lists --
# printed `DRIFT 5 (no artifact findings, see notes below)` with no notes
# below it, and handed the user a repair command for five findings it could
# not name. Both are UNKNOWN.
if total != counted(findings) + counted(settings):
    print("count and findings disagree — drift is UNKNOWN, not clean")
    raise SystemExit(0)

if total == 0 and not findings and not settings:
    if not warnings:
        print("OK")
        raise SystemExit(0)
    # Checked, nothing drifted, but part of the run was blind. A named
    # outcome rather than DRIFT 0, because there is nothing to repair and
    # the repair hint DRIFT prints could not have helped anyway.
    print("DEGRADED %d warning(s) — the artifact verdict stands, "
          "version attribution does not" % len(warnings))
    for warning in warnings:
        print("   note: %s" % flat(warning))
    raise SystemExit(0)

states = {}
for item in findings:
    item = item if isinstance(item, dict) else {}
    state = flat(item.get("state") or "UNKNOWN")
    states[state] = states.get(state, 0) + 1
if settings:
    states["settings.json"] = len(settings)
detail = ", ".join("%d %s" % (n, s) for s, n in sorted(states.items()))
print("DRIFT %d (%s)" % (total, detail or "no artifact findings, see notes below"))
for item in findings[:5]:
    item = item if isinstance(item, dict) else {}
    print("   %-14s %s  %s" % (flat(item.get("state") or "?"),
                               flat(item.get("target") or "?"),
                               flat(item.get("detail") or "")))
if len(findings) > 5:
    print("   ... and %d more" % (len(findings) - 5))
for entry in settings[:5]:
    print("   %-14s %s" % ("settings.json", flat(entry)))
if len(settings) > 5:
    print("   ... and %d more" % (len(settings) - 5))
for warning in warnings:
    print("   note: %s" % flat(warning))
# A machine-readable marker, stripped again before any of this is shown.
# The shell has to know whether an UNRECOGNIZED row is in the verdict, so it
# can say that the repair command above does not cover it -- and the
# human-readable detail line is not a stable thing to grep for that.
if any(isinstance(f, dict) and f.get("state") == "UNRECOGNIZED" for f in findings):
    print("__UNRECOGNIZED__")
') || DRIFT_MSG="could not parse doctor output — drift is UNKNOWN, not clean"
    fi

    # A completed clean verdict writes NOTHING to stderr. Output there on an
    # exit this section treats as a verdict means something went wrong after
    # the report was printed: a doctor that emits a full clean JSON document
    # and then dies in an uncaught exception would otherwise be scored from
    # that JSON -- printing the clean line and writing `drift,` into the
    # commit authorization off a run that crashed. That is the same rule the
    # exit-2 branch applies to a run that never printed a verdict at all.
    #
    # The EXIT CODE is checked first because it is the stronger evidence and
    # it is in hand whether or not stderr was ever capturable. Invoked
    # without --fix, the doctor returns 0 only when it found nothing at all,
    # so a clean report with a nonzero exit is self-contradictory by
    # construction. That test is what catches the crashed run when mktemp
    # failed, and when the traceback started with a blank line: both left the
    # stderr test looking at an empty string and scored the crash as health.
    if [ "$DRIFT_MSG" = "OK" ] && [ "$DOCTOR_RC" -ne 0 ]; then
        DRIFT_MSG="doctor printed a clean report but exited ${DOCTOR_RC} — drift is UNKNOWN, not clean"
    elif [ "$DRIFT_MSG" = "OK" ] && [ "$DOCTOR_ERR_SEEN" = 1 ]; then
        # The stderr text itself is printed by the follow-on line below,
        # which every UNKNOWN path shares.
        DRIFT_MSG="doctor printed a clean report and then wrote to stderr — drift is UNKNOWN, not clean"
    fi

    # `drift,` is appended to CHECKS_RUN on exactly the three branches that
    # produced a real verdict -- OK, DRIFT and DEGRADED. The preflight token
    # is the hook's commit authorization and must never attest to a check
    # that was skipped or crashed -- the same rule the assertion-strength
    # gate earlier in this script follows.
    if [ "$DRIFT_MSG" = "OK" ]; then
        echo "✅ installed artifacts match the shipped templates"
        CHECKS_RUN="${CHECKS_RUN}drift,"
    elif printf '%s' "$DRIFT_MSG" | head -1 | grep -q '^DRIFT '; then
        echo "⚠️  $(printf '%s' "$DRIFT_MSG" | head -1)"
        # grep -v strips the parser's __UNRECOGNIZED__ marker; `|| true`
        # because grep exits 1 when it filters everything away, and this
        # script runs under `set -e`.
        printf '%s\n' "$DRIFT_MSG" | tail -n +2 | grep -v '^__UNRECOGNIZED__$' || true
        echo ""
        echo "   Not blocking this commit. To repair:"
        echo "     python3 \"\$CLAUDE_PLUGIN_ROOT/skills/harden-repo/scripts/doctor.py\" --repo \"$PROJECT_DIR\" --fix"
        # Deliberately no --all. --all is the form the INSTALLER needs,
        # because a tool call has no TTY for the doctor's per-file prompt.
        # This line is read by a human sitting at a terminal, who does have
        # one; printing --all to them silently repairs every DRIFTED-BEHIND
        # and MISSING artifact in the repo with nothing to confirm.
        if printf '%s\n' "$DRIFT_MSG" | grep -q '^__UNRECOGNIZED__$'; then
            # Without this the advisory hands the user a command that does
            # nothing for what it just reported: --fix acts only on
            # DRIFTED-BEHIND and MISSING, so an UNRECOGNIZED row survives it
            # and the identical warning reprints on every commit forever.
            echo ""
            echo "   UNRECOGNIZED means the file was changed locally and matches no"
            echo "   released generation, so the command above leaves it alone."
            echo "   Repairing one needs --fix-unrecognized, which REPLACES the file"
            echo "   with the shipped template -- your version survives only in the"
            echo "   .bak written beside it. Read the doctor section of harden-repo's"
            echo "   SKILL.md before you run that."
        fi
        CHECKS_RUN="${CHECKS_RUN}drift,"
    elif printf '%s' "$DRIFT_MSG" | head -1 | grep -q '^DEGRADED '; then
        echo "⚠️  $(printf '%s' "$DRIFT_MSG" | head -1)"
        printf '%s\n' "$DRIFT_MSG" | tail -n +2
        echo ""
        echo "   Not blocking this commit, and there is nothing here to repair:"
        echo "   every installed artifact matches its template. What failed is the"
        echo "   template ledger, which lives in the repo-hardener plugin and not"
        echo "   in this repo, so no repair run against this repo can reach it."
        echo "   Reinstall or update the plugin if this keeps appearing."
        CHECKS_RUN="${CHECKS_RUN}drift,"
    else
        # Not a skip: every message that reaches here is "the tool could not
        # give an answer", which is a warning, not a clean bill of health.
        # The one genuine skip -- no plugin loaded -- is handled above and
        # keeps the skip icon.
        echo "⚠️  $DRIFT_MSG"
        # Every graceful-UNKNOWN path lands here -- empty stdout, unreadable
        # output, output that is not a report -- and on all of them the
        # doctor stderr is the only thing that says WHY. It used to be
        # dropped: a doctor that died on an uncaught exception printed
        # "could not run the doctor" and its actual diagnostic appeared
        # nowhere in the run.
        if [ "$DOCTOR_ERR_SEEN" = 1 ] && [ "$DOCTOR_ERR_SHOWN" = 0 ]; then
            echo "   doctor stderr: $DOCTOR_ERR_LINE"
        fi
    fi
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "$CHECKS_PASSED" = false ]; then
    echo "❌ PREFLIGHT FAILED - Fix errors before committing"
    rm -f "$TOKEN_FILE"
    _PREFLIGHT_REPORTED=1
    exit 1
fi

# Create confirmation token
TIMESTAMP=$(date +%s)
_preflight_write_token "$TIMESTAMP" "$STAGED_COUNT" "${CHECKS_RUN%,}" ""
_PREFLIGHT_TOKEN_OK=1

echo ""
echo "✅ PREFLIGHT PASSED"
echo ""
echo "Token created (expires in ${TOKEN_EXPIRY_SECONDS}s)"
echo "📝 You may now run: git commit -m \"your message\""
echo ""
