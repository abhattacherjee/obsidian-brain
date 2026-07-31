#!/usr/bin/env bash
# test-dev-skill.sh — Swap installed plugin cache with dev version for testing.
#
# Usage:
#   ./scripts/test-dev-skill.sh install   # backup cache, install dev version
#   ./scripts/test-dev-skill.sh restore   # restore original cached version
#   ./scripts/test-dev-skill.sh status    # show which version is active
#
# After "install", start a NEW Claude Code session to pick up the changes.
# After testing, run "restore" to put the original version back.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PLUGIN_NAME="obsidian-brain"

# Guard 1 (sentinel): REPO_ROOT must actually be an obsidian-brain checkout,
# not just whatever directory happens to be two levels above this script.
#
# NOTE: this sentinel (hooks/obsidian_utils.py + skills/) is deliberately
# NOT the same one skills/dev-test/SKILL.md's _ob_repo() resolver checks
# (scripts/test-dev-skill.sh itself). That asymmetry is intentional, not
# drift to "harmonize" away -- the skill asks "can this tree run the
# script?" (it needs the script to exist), this script asks "am I actually
# sitting inside a real checkout?" (it needs its own source tree to copy
# from). A tree that satisfies one and not the other fails loudly either
# way; see MINOR-4 in the #287 final review.
if [[ ! -f "$REPO_ROOT/hooks/obsidian_utils.py" ]] || [[ ! -d "$REPO_ROOT/skills" ]]; then
    echo "ERROR: $REPO_ROOT does not look like an obsidian-brain checkout (missing hooks/obsidian_utils.py or skills/)." >&2
    echo "This script must live inside a real obsidian-brain repo checkout; refusing to run." >&2
    exit 1
fi

# Guard 2 (self-copy, D3): if this script is itself running from inside the
# INSTALLED plugin tree, it is not a checkout to install FROM.
#
# Two shapes live under ~/.claude/plugins/, and both are wrong as a source:
#   * plugins/cache/*/obsidian-brain/<version>/ -- REPO_ROOT resolves to the
#     cache version directory and "install" copies the cache onto itself
#     (cp cache/hooks/*.py cache/hooks/), a byte-for-byte no-op that prints a
#     full success transcript and leaves a .bak;
#   * plugins/marketplaces/<name>/ -- obsidian-brain's marketplace.json
#     declares "source": "./", so the marketplace clone IS the plugin repo and
#     carries this very script at its root. Installing from it copies a
#     RELEASED tree that `/plugin marketplace update` rewrites behind your
#     back, not the working copy you are editing.
# Both are silent-stale rather than hard failures, so both must be refused
# loudly instead of "succeeding" -- hence the prefix covers all of
# ~/.claude/plugins/, not just the cache subtree.
#
# Scoped to the mutating subcommands (install/restore): `status` is a
# read-only report and there is no reason to withhold it from someone who
# invoked this script directly out of an installed tree to see what is there.
# (Via /dev-test that never happens -- the skill's resolver only ever hands
# over a real checkout -- so this exemption exists for the by-hand
# invocation, not for a "machine with no local checkout" scenario.)
case "${1:-status}" in
    install|restore)
        # REPO_ROOT above is resolved through symlinks (`pwd -P`). $HOME must
        # be canonicalized the same way before being used as a prefix, or a
        # symlinked $HOME (e.g. macOS /var -> /private/var) makes this
        # comparison silently fail to fire on a machine where it should --
        # the guard would compare a canonicalized path against an
        # uncanonicalized one and never match. If $HOME can't be resolved at
        # all, fail closed for these mutating subcommands rather than
        # silently skipping the guard -- a guard that can't be evaluated is
        # not a guard.
        if [[ -z "${HOME:-}" ]] || [[ ! -d "$HOME" ]]; then
            echo "ERROR: \$HOME is unset, empty, or not a directory; cannot verify this script isn't" >&2
            echo "running from inside the installed plugin tree. Refusing to run '${1}' without a" >&2
            echo "resolvable \$HOME." >&2
            exit 1
        fi
        PLUGIN_ROOT_PREFIX="$(cd "$HOME" && pwd -P)/.claude/plugins/"
        if [[ "$REPO_ROOT/" == "$PLUGIN_ROOT_PREFIX"* ]]; then
            echo "ERROR: $REPO_ROOT is inside the installed plugin tree ($PLUGIN_ROOT_PREFIX)." >&2
            echo "That covers both the plugin cache (installing it onto itself is a no-op) and a" >&2
            echo "marketplace clone (a released tree that '/plugin marketplace update' rewrites)." >&2
            echo "Run this script from a real local obsidian-brain checkout instead." >&2
            exit 1
        fi
        ;;
esac

# Discover the plugin cache dir regardless of which marketplace installed it.
# Matches ~/.claude/plugins/cache/<marketplace>/obsidian-brain ; newest wins.
CACHE_BASE="$(ls -dt "${HOME}/.claude/plugins/cache/"*/"${PLUGIN_NAME}" 2>/dev/null | head -1 || true)"
if [[ -z "$CACHE_BASE" ]]; then
    echo "ERROR: No installed ${PLUGIN_NAME} plugin cache found under ~/.claude/plugins/cache/*/${PLUGIN_NAME}"
    exit 1
fi
# Pick the highest installed semver under that cache dir (excluding .bak backups).
#
# The `|| true` is load-bearing, not defensive noise: `grep -v` exits 1 when it
# filters out EVERYTHING (a cache dir holding nothing but a .bak -- the exact
# aftermath of a restore interrupted between the rm and the mv). Under
# `set -o pipefail` that status becomes the pipeline's, the bare assignment
# takes it, and `set -e` aborts the script ON THIS LINE -- before the guard
# below can print anything. Every subcommand then exits 1 with no output at
# all, which is the least useful possible response to "what happened to my
# cache?". Keeping the pipeline's status clean lets the guard actually report.
PLUGIN_VERSION="$(ls -1 "$CACHE_BASE" 2>/dev/null | { grep -v '\.bak$' || true; } | sort -V | tail -1)"
if [[ -z "$PLUGIN_VERSION" ]]; then
    echo "ERROR: No cached version found at $CACHE_BASE" >&2
    if compgen -G "$CACHE_BASE/*.bak" > /dev/null 2>&1; then
        echo "Only a .bak backup is present -- a previous 'restore' was interrupted between" >&2
        echo "removing the cache and moving the backup into place. Recover by dropping the" >&2
        echo "suffix:" >&2
        echo "  mv '$CACHE_BASE'/*.bak '$CACHE_BASE/<version>'" >&2
        echo "or reinstall the plugin with '/plugin marketplace update'." >&2
    fi
    exit 1
fi

CACHE_DIR="${CACHE_BASE}/${PLUGIN_VERSION}"
BACKUP_DIR="${CACHE_BASE}/${PLUGIN_VERSION}.bak"

cmd="${1:-status}"

case "$cmd" in
    install)
        if [[ ! -d "$CACHE_DIR" ]]; then
            echo "ERROR: Cache directory not found: $CACHE_DIR"
            echo "Available versions:"
            ls "$CACHE_BASE" 2>/dev/null || echo "  (none)"
            exit 1
        fi

        if [[ -d "$BACKUP_DIR" ]]; then
            echo "WARNING: Backup already exists at $BACKUP_DIR"
            echo "Run 'restore' first, or remove the backup manually."
            exit 1
        fi

        echo "Backing up: $CACHE_DIR -> $BACKUP_DIR"
        cp -R "$CACHE_DIR" "$BACKUP_DIR"

        # On failure, warn user to restore manually
        trap 'echo "ERROR: Install failed partway. Run \"/dev-test restore\" to recover." >&2' ERR

        echo "Installing dev versions..."

        # Copy hooks (Python files + registration manifest)
        cp "$REPO_ROOT/hooks/"*.py "$CACHE_DIR/hooks/"
        echo "  hooks/*.py -> cache"
        if [[ -f "$REPO_ROOT/hooks/hooks.json" ]]; then
            cp "$REPO_ROOT/hooks/hooks.json" "$CACHE_DIR/hooks/"
            echo "  hooks/hooks.json -> cache"
        fi

        # Copy plugin manifests so version/metadata changes propagate
        if [[ -d "$CACHE_DIR/.claude-plugin" ]]; then
            if [[ -f "$REPO_ROOT/.claude-plugin/plugin.json" ]]; then
                cp "$REPO_ROOT/.claude-plugin/plugin.json" "$CACHE_DIR/.claude-plugin/"
                echo "  .claude-plugin/plugin.json -> cache"
            fi
            if [[ -f "$REPO_ROOT/.claude-plugin/marketplace.json" ]]; then
                cp "$REPO_ROOT/.claude-plugin/marketplace.json" "$CACHE_DIR/.claude-plugin/"
                echo "  .claude-plugin/marketplace.json -> cache"
            fi
        fi

        # Copy skills
        for skill_dir in "$REPO_ROOT/skills/"*/; do
            skill_name=$(basename "$skill_dir")
            mkdir -p "$CACHE_DIR/skills/$skill_name"
            if compgen -G "$skill_dir"* > /dev/null 2>&1; then
                cp "$skill_dir"* "$CACHE_DIR/skills/$skill_name/"
            fi
            echo "  skills/$skill_name/ -> cache"
        done

        # Copy runtime scripts (whitelist — scripts/ is heterogeneous;
        # see feedback_plugin_sync_scripts_heterogeneous.md memory).
        # These are dispatched by skills at runtime and must reflect the
        # repo state, not the released cache.
        if [[ -f "$REPO_ROOT/scripts/vault_doctor.py" ]]; then
            cp "$REPO_ROOT/scripts/vault_doctor.py" "$CACHE_DIR/scripts/"
            echo "  scripts/vault_doctor.py -> cache"
        fi
        if [[ -d "$REPO_ROOT/scripts/vault_doctor_checks" ]]; then
            mkdir -p "$CACHE_DIR/scripts/vault_doctor_checks"
            cp "$REPO_ROOT/scripts/vault_doctor_checks/"*.py "$CACHE_DIR/scripts/vault_doctor_checks/"
            echo "  scripts/vault_doctor_checks/*.py -> cache"
        fi

        trap - ERR

        # Run security tests against installed cache.
        #
        # The announcement lives INSIDE the existence check: printed before it,
        # "Running security tests..." followed by silence reads as "ran, nothing
        # to report" when in fact nothing ran -- and the script is missing in
        # exactly the situations you would most want told about (a partial
        # checkout, or a source tree that resolved to something unexpected).
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        security_status="skipped"
        if [ -f "$SCRIPT_DIR/test-security.sh" ]; then
            echo ""
            echo "Running security tests..."
            if bash "$SCRIPT_DIR/test-security.sh"; then
                security_status="passed"
            else
                security_status="failed"
            fi
        else
            echo ""
            echo "NOTE: $SCRIPT_DIR/test-security.sh not found — security tests were SKIPPED, not passed."
        fi

        echo ""
        # A failure used to print a WARNING and then the success banner at
        # exit 0, so the failure vanished from both the transcript's last word
        # and the exit status any caller checks. Say what happened, and say it
        # in the exit code.
        if [[ "$security_status" == "failed" ]]; then
            echo "Dev version installed to cache (v${PLUGIN_VERSION}), but SECURITY TESTS FAILED (see above)." >&2
            echo "Do not run a live session against this install until they pass." >&2
            echo "To revert it, run:" >&2
            echo "  /dev-test restore" >&2
            exit 2
        fi
        echo "Dev version installed to cache (v${PLUGIN_VERSION})."
        echo "Start a NEW Claude Code session to pick up the changes."
        echo ""
        echo "When done testing, run:"
        echo "  /dev-test restore"
        ;;

    restore)
        if [[ ! -d "$BACKUP_DIR" ]]; then
            echo "No backup found at $BACKUP_DIR — nothing to restore."
            echo "Current cache is the original version."
            exit 0
        fi

        # Sanity check: CACHE_DIR must be under CACHE_BASE
        if [[ "$CACHE_DIR" != "${CACHE_BASE}/"* ]]; then
            echo "ERROR: CACHE_DIR '$CACHE_DIR' is outside expected base. Aborting." >&2
            exit 1
        fi

        # A .bak DIRECTORY only proves an install started, not that the backup
        # finished. `cp -R` above is not atomic and runs before that arm's ERR
        # trap is installed, so an interrupt (Ctrl-C on a slow copy), a full
        # disk, or an EACCES leaves a truncated .bak that is indistinguishable
        # from a good one -- and the "Backup already exists / run restore
        # first" message then steers the user straight into promoting it over
        # a healthy cache, at exit 0, reporting "restored". Require the same
        # sentinel shape guard 1 asks of a checkout before overwriting
        # anything.
        if [[ ! -f "$BACKUP_DIR/hooks/obsidian_utils.py" ]] \
            || ! compgen -G "$BACKUP_DIR/skills/*" > /dev/null 2>&1; then
            echo "ERROR: $BACKUP_DIR is not a complete plugin backup (missing hooks/obsidian_utils.py" >&2
            echo "or a non-empty skills/). It is most likely the remains of an interrupted install." >&2
            echo "Refusing to overwrite the live cache at $CACHE_DIR with it." >&2
            echo "Inspect the backup, then either fix it and re-run '/dev-test restore', or delete it" >&2
            echo "and reinstall with '/plugin marketplace update'." >&2
            exit 1
        fi

        # rm + mv is not atomic either: between them the plugin has no cache
        # directory at all, and this arm runs with no ERR handler (the install
        # arm's trap is cleared at the end of that branch). Say what state a
        # failure leaves behind, since the next invocation of this script
        # cannot -- it will find nothing but the .bak.
        trap 'echo "ERROR: restore failed partway; the cache at \"$CACHE_DIR\" may be missing or incomplete." >&2
              echo "Recover by reinstalling: /plugin marketplace update" >&2' ERR

        echo "Restoring: $BACKUP_DIR -> $CACHE_DIR"
        rm -rf "$CACHE_DIR"
        mv "$BACKUP_DIR" "$CACHE_DIR"

        trap - ERR

        echo ""
        echo "Original v${PLUGIN_VERSION} restored."
        echo "Start a NEW session to pick up the restored version."
        ;;

    status)
        echo "Plugin: $PLUGIN_NAME"
        echo "Installed cache version: $PLUGIN_VERSION"
        echo "Cache dir: $CACHE_DIR"
        echo ""

        if [[ -d "$BACKUP_DIR" ]]; then
            echo "Status: DEV VERSION ACTIVE (backup exists)"
            echo "Backup: $BACKUP_DIR"
            echo ""
            echo "Files changed from original:"
            diff -rq "$BACKUP_DIR" "$CACHE_DIR" 2>/dev/null | head -20 || echo "  (diff failed)"
        elif [[ -d "$CACHE_DIR" ]]; then
            echo "Status: ORIGINAL (no backup, cache is clean)"
        else
            echo "Status: NOT INSTALLED (cache dir missing)"
        fi
        ;;

    *)
        echo "Usage: $0 {install|restore|status}"
        exit 1
        ;;
esac
