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
#
# The skills/ half tests NON-EMPTY (`compgen -G`), not mere existence. `-d`
# alone accepted an empty skills/ -- and an empty skills/ is not an inert
# near-miss, it is the exact failure this half exists to stop: the install
# loop's `"$REPO_ROOT/skills/"*/` glob goes unmatched, bash leaves it
# literal (nullglob is off), and the run prints `  skills/*/ -> cache`,
# creates a directory literally named `*` inside the plugin cache, publishes
# a .bak that blocks the next install, and exits 0. Reproduced verbatim
# during the #287 adversarial review. `restore`'s completeness check below
# already used the non-empty predicate for this same property; the two must
# not disagree about what "has skills" means.
if [[ ! -f "$REPO_ROOT/hooks/obsidian_utils.py" ]] \
    || ! compgen -G "$REPO_ROOT/skills/*" > /dev/null 2>&1; then
    echo "ERROR: $REPO_ROOT does not look like an obsidian-brain checkout (missing hooks/obsidian_utils.py, or skills/ is missing or empty)." >&2
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
        # REPO_ROOT above is resolved through symlinks (`pwd -P`), so the
        # prefix it is compared against must be resolved to the SAME degree or
        # the `==` never matches and the guard silently fails to fire. If
        # $HOME can't be resolved at all, fail closed for these mutating
        # subcommands rather than skipping the guard -- a guard that can't be
        # evaluated is not a guard.
        if [[ -z "${HOME:-}" ]] || [[ ! -d "$HOME" ]]; then
            echo "ERROR: \$HOME is unset, empty, or not a directory; cannot verify this script isn't" >&2
            echo "running from inside the installed plugin tree. Refusing to run '${1}' without a" >&2
            echo "resolvable \$HOME." >&2
            exit 1
        fi
        # Canonicalize the plugin root DIRECTORY, rather than composing a
        # canonicalized $HOME with the literal segments /.claude/plugins/ and
        # calling the result a resolved prefix. Composing a prefix that way
        # resolves only its first component, so ANY symlink below $HOME defeats
        # it -- and `~/.claude` being a symlink into a dotfiles tree is a
        # standard stow/chezmoi layout, not an exotic one. Reproduced during the
        # #287 adversarial review: with `~/.claude` symlinked, a marketplace
        # clone installed itself over the cache at exit 0 with a full success
        # banner (the released hook landed in the cache), while the identical
        # fixture with a real `.claude` directory refused at exit 1. That was the
        # third instance of this one root cause on this branch -- $HOME
        # uncanonicalized, then the prefix too narrow, then this -- so `cd` into
        # the real thing and let the filesystem do the resolving.
        #
        # No plugins directory means the guard has nothing to catch, and skipping
        # it there is not fail-open: REPO_ROOT is an existing directory (its
        # assignment at the top of this script `cd`s into it), and no existing
        # directory can live under a path that does not exist. Skipping it is
        # also what keeps the `cd` below evaluable at all -- `cd` into a missing
        # directory fails, and under `set -euo pipefail` that would abort every
        # `install`/`restore` on a machine that has no plugins tree yet, which
        # is a refusal with no defect behind it. If the directory exists but
        # cannot be entered, the
        # `cd` fails, `set -e` aborts on the assignment, and the run stops before
        # any mutation -- fail closed, as above.
        if [[ -d "$HOME/.claude/plugins" ]]; then
            PLUGIN_ROOT_PREFIX="$(cd "$HOME/.claude/plugins" && pwd -P)/"
            if [[ "$REPO_ROOT/" == "$PLUGIN_ROOT_PREFIX"* ]]; then
                echo "ERROR: $REPO_ROOT is inside the installed plugin tree ($PLUGIN_ROOT_PREFIX)." >&2
                echo "That covers both the plugin cache (installing it onto itself is a no-op) and a" >&2
                echo "marketplace clone (a released tree that '/plugin marketplace update' rewrites)." >&2
                echo "Run this script from a real local obsidian-brain checkout instead." >&2
                exit 1
            fi
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
# Pick the highest installed semver under that cache dir, excluding both the
# .bak backup and any *.bak.partial.<pid> left behind by an interrupted backup
# (see the install arm). Neither is a version, and `sort -V` ranks both ABOVE
# the bare version string they are derived from -- so without the filter,
# `3.4.1.bak.partial.9999` would be selected as PLUGIN_VERSION and CACHE_DIR
# would point at a truncated tree.
#
# Note which pattern catches which: the partial's real name ends in
# `.partial.<pid>`, NOT in `.bak`, so the `\.bak$` arm does not match it --
# the `\.partial\.` arm is the one that does. Deleting either arm leaves a
# truncated tree selectable. Unanchored (not `\.partial\.[0-9]+$`) so it
# matches the SAME set as `status`'s orphan-listing glob (`*.partial.*`,
# below) -- the two features report on this class of directory and must
# not be able to disagree about a given one.
#
# The `|| true` wraps the WHOLE pipeline, not just the `grep`, and that is
# load-bearing rather than defensive noise. Two commands in here can exit
# non-zero on states the guard below is supposed to report:
#   * `grep -v` exits 1 when it filters out EVERYTHING (a cache dir holding
#     nothing but a .bak -- the exact aftermath of a restore interrupted
#     between the rm and the mv);
#   * `ls -1` exits non-zero when $CACHE_BASE exists but is not readable
#     (a chmod/ownership accident under ~/.claude/plugins/cache).
# Under `set -o pipefail` either status becomes the pipeline's, the bare
# assignment takes it, and `set -e` aborts the script ON THIS LINE -- before
# the guard below can print anything. Every subcommand then exits 1 with no
# output at all, which is the least useful possible response to "what happened
# to my cache?". Scoping `|| true` to `grep` alone (as a first fix did) closes
# only the first of the two. Neutralising the whole pipeline cannot admit a
# WRONG version either: every failure mode yields the empty string, which is
# exactly what the guard below tests for.
PLUGIN_VERSION="$( { ls -1 "$CACHE_BASE" 2>/dev/null \
    | grep -v -e '\.bak$' -e '\.partial\.' \
    | sort -V | tail -1; } || true )"
if [[ -z "$PLUGIN_VERSION" ]]; then
    echo "ERROR: No cached version found at $CACHE_BASE" >&2
    if [[ ! -r "$CACHE_BASE" ]]; then
        echo "($CACHE_BASE exists but is not readable -- check its permissions and ownership.)" >&2
    fi
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

        # Build the backup OUT OF PLACE, then publish it by rename.
        #
        # `cp -R` is not atomic. Interrupt it (Ctrl-C on a slow copy), fill the
        # disk, or hit an EACCES partway and the destination is a truncated
        # tree. Validating that tree's SHAPE afterwards samples the failure
        # mode rather than closing it: an interrupt anywhere inside skills/ --
        # 19 directories, the bulk of the tree, and therefore the MOST likely
        # place a copy is interrupted -- leaves both hooks/obsidian_utils.py
        # and a non-empty skills/ in place, so it passes any sentinel probe and
        # `restore` promotes the fragment over a healthy cache at exit 0.
        #
        # Renaming into place makes completeness structural instead: `mv`
        # within one directory is atomic, and it only runs after `cp -R`
        # returned 0, so the name "$BACKUP_DIR" can ONLY ever appear on a copy
        # that ran to completion. A crash leaves "$BACKUP_TMP" -- a name
        # `restore` never looks at, `install`'s "backup already exists" check
        # never sees, and the version scan above filters out -- so a stale
        # partial is inert rather than promotable. It is safe to delete by hand.
        BACKUP_TMP="${BACKUP_DIR}.partial.$$"
        rm -rf "$BACKUP_TMP"
        # A backup failure has changed NOTHING, so "run restore" (the message
        # used once the cache is being written, below) would be exactly the
        # wrong advice here.
        #
        # Message FIRST, cleanup second, and the cleanup cannot abort the body.
        # Trap bodies run with `errexit` still in force, so a non-zero command
        # inside one kills the rest of it (verified on bash 3.2.57: `trap
        # 'false; echo REACHED >&2' ERR` never prints REACHED). With the `rm`
        # first, an `rm` that cannot complete -- a root-owned entry inside the
        # partial, a read-only parent -- would swallow the one diagnostic this
        # arm exists to deliver, at exactly the moment the user most needs to be
        # told the cache is intact. The `|| true` keeps that ordering true for
        # any future command appended after it.
        trap 'echo "ERROR: Backup of $CACHE_DIR failed. The cache was NOT modified and no backup was kept." >&2; rm -rf "$BACKUP_TMP" || true' ERR
        echo "Backing up: $CACHE_DIR -> $BACKUP_DIR"
        cp -R "$CACHE_DIR" "$BACKUP_TMP"
        mv "$BACKUP_TMP" "$BACKUP_DIR"
        trap - ERR

        # From here the cache itself is being overwritten, so recovery means
        # putting the backup back.
        #
        # This is the ONLY failure path that leaves the cache modified, and it
        # gets its own exit code (3) instead of sharing the generic 1 that
        # every refuse-before-writing guard uses. Sharing it was a real defect:
        # /dev-test's catch-all arm read "non-zero and not 2" as "nothing was
        # installed" -- the exact opposite of what this trap prints -- and a
        # user told nothing happened does not run `restore`, so they are left
        # with a half-dev cache plus a stale .bak that blocks the next install
        # with "Backup already exists". A distinct code lets the caller branch
        # on the STATE rather than pattern-match this message's wording.
        #
        # Keep the `exit 3` inside the trap: without it the trap only prints
        # and `set -e` then exits with the failing command's own status (1),
        # which is the code this arm exists to stop overloading.
        trap 'echo "ERROR: Install failed partway. A backup of the original is in place and the cache may hold a mix of released and dev files. Run \"/dev-test restore\" to recover." >&2; exit 3' ERR

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

        # Copy skills.
        #
        # `[[ -d ... ]] || continue` is the residual of guard 1's empty-skills/
        # case, not redundancy with it. Guard 1 asks whether skills/ has ANY
        # entry (matching restore's predicate); this loop globs only skills/*/,
        # so a skills/ holding nothing but files -- a README, a half-finished
        # rsync -- passes the guard and still leaves the glob unmatched, and an
        # unmatched glob is literal here (nullglob is off). Without this line
        # that shape mkdirs a directory named `*` into the plugin cache and
        # narrates `  skills/*/ -> cache` for a copy that never happened;
        # reproduced during the #287 adversarial review.
        for skill_dir in "$REPO_ROOT/skills/"*/; do
            [[ -d "$skill_dir" ]] || continue
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
        # Exit 4: there was nothing to restore. Distinct from the exit 0 a
        # completed swap returns, for the same reason install's partway failure
        # got exit 3 -- one exit code must name one state. Sharing 0 meant
        # /dev-test's single exit-0 arm narrated "Original version restored.
        # Start a new session" over a run that restored nothing and changed
        # nothing, sending the user to restart a session for a state change that
        # never happened. Not an error (nothing is wrong, and nothing is left to
        # recover), just not a restore -- hence its own code rather than folding
        # into the generic 1 that the refusal paths use.
        if [[ ! -d "$BACKUP_DIR" ]]; then
            echo "No backup found at $BACKUP_DIR — nothing to restore."
            echo "Current cache is the original version."
            exit 4
        fi

        # Sanity check: CACHE_DIR must be under CACHE_BASE
        if [[ "$CACHE_DIR" != "${CACHE_BASE}/"* ]]; then
            echo "ERROR: CACHE_DIR '$CACHE_DIR' is outside expected base. Aborting." >&2
            exit 1
        fi

        # SECONDARY check. The primary defence is on the install side: the
        # backup is now built at "$BACKUP_DIR.partial.$$" and renamed into
        # place only after `cp -R` succeeds, so a ".bak" produced by THIS
        # script is complete by construction and this branch cannot fire for
        # it. What remains for a shape probe to catch is everything that did
        # not come from the current install path: a .bak left by an older
        # version of this script (which copied straight to the final name and
        # so could truncate), one hand-renamed or hand-edited by a user, or one
        # carried over from a different plugin version. All of those are
        # indistinguishable from a good backup by existence alone -- and the
        # "Backup already exists / run restore first" message steers the user
        # straight into promoting them over a healthy cache, at exit 0,
        # reporting "restored". Kept because it costs two stat calls and its
        # failure mode is silent data loss.
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
            # `diff` exits 1 for "the inputs differ" -- which is not a failure,
            # it is the ONLY outcome this branch is ever reached for: a dev
            # version is active precisely because the cache differs from its
            # backup. Under `set -o pipefail` (line 12) that 1 became the
            # pipeline's status, so `|| echo "  (diff failed)"` fired after
            # EVERY successful dev-active listing -- and /dev-test's Step 4 now
            # relays this report verbatim, so the caller passed a failure that
            # did not occur straight through to the user. `head -20` closing the
            # pipe early (SIGPIPE, 141) was a second, independent trigger.
            # Only status >= 2 means diff itself failed; branch on that alone.
            diff_rc=0
            diff_out="$(diff -rq "$BACKUP_DIR" "$CACHE_DIR" 2>/dev/null)" || diff_rc=$?
            if [[ "$diff_rc" -ge 2 ]]; then
                echo "  (diff failed)"
            elif [[ -n "$diff_out" ]]; then
                printf '%s\n' "$diff_out" | head -20 || true
            else
                echo "  (none — the cache is byte-identical to the backup)"
            fi
        elif [[ -d "$CACHE_DIR" ]]; then
            echo "Status: ORIGINAL (no backup, cache is clean)"
        else
            echo "Status: NOT INSTALLED (cache dir missing)"
        fi

        # Orphaned out-of-place backups. A hard kill (SIGKILL, power loss)
        # skips the install arm's ERR trap, so `${VERSION}.bak.partial.<pid>`
        # survives -- and no later run removes it, because `rm -rf
        # "$BACKUP_TMP"` only ever clears the CURRENT pid's name. The state is
        # deliberately inert (filtered from the version scan, never named by
        # `restore`, invisible to install's "backup already exists" check), but
        # each one is a FULL copy of the plugin cache, so reporting "cache is
        # clean" beside a pile of them understates real disk use. Disclose;
        # never auto-delete, since the pid may belong to a live install.
        if compgen -G "$CACHE_BASE/*.partial.*" > /dev/null 2>&1; then
            echo ""
            echo "Orphaned partial backups found (leftovers from an interrupted install)."
            echo "They are never used by 'restore'; safe to delete once no '/dev-test install' is running:"
            for _partial in "$CACHE_BASE"/*.partial.*; do
                [[ -e "$_partial" ]] || continue
                echo "  $_partial"
            done
        fi
        ;;

    *)
        echo "Usage: $0 {install|restore|status}"
        exit 1
        ;;
esac
