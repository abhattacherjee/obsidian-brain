---
name: dev-test
description: "Install dev version of obsidian-brain into the plugin cache for local testing, or restore the original. Note: on a directory-source marketplace install the skills already load the checkout (#278), so install is only needed for github-source installs and the cache-asserting test scripts. Use when: (1) /dev-test install to test unreleased changes, (2) /dev-test restore to put back the original, (3) /dev-test to check current status."
metadata:
  version: 1.0.0
---

# Dev Test — Install/Restore Dev Plugin for Testing

Swaps the installed plugin cache with the current repo working copy for local testing. After install, start a new Claude Code session to pick up the changes.

**If this repo is registered as a directory-source marketplace** (`source.source == "directory"` in `~/.claude/plugins/known_marketplaces.json`, which is how a local checkout is normally installed), `/dev-test install` no longer changes which hooks the skills load: since #278 every skill resolves the registered checkout first and only falls back to the plugin cache. Your working copy is already what runs — edit and re-run, no install step. `/dev-test install` still matters for a **github-source** install (where the cache is what resolves) and for the manual test scripts under `scripts/dev-test/` that deliberately assert on the cache's contents.

**Tools needed:** Bash

## Procedure

### Step 1 — Parse argument

Check the argument passed to `/dev-test`:

- `install` → go to Step 2
- `restore` → go to Step 3
- No argument or `status` → go to Step 4

### Step 2 — Install dev version

This works from any directory — it locates the obsidian-brain checkout itself, it does not require the cwd to be inside it. Run:

```bash
# Layer 1 -- the checkout you are STANDING IN, when it is itself an
# obsidian-brain checkout. A git worktree or a second clone is a deliberate
# context signal: that tree is the one you mean, and the registry can only
# ever name one checkout. The `-f "$_T/scripts/test-dev-skill.sh"` sentinel
# is what makes the cwd safe to trust here -- it can only ever select an
# obsidian-brain checkout, never the arbitrary project you happen to be
# working in. That project (#287's bug) has no sentinel and falls through
# to layer 2 exactly as before.
#
# NOTE: that sentinel check is LOAD-BEARING -- delete it and any foreign
# toplevel wins layer 1 outright, layer 2 is never consulted, and #287
# regresses. Pinned by
# test_shell_uses_the_registry_when_the_cwd_toplevel_lacks_the_sentinel.
REPO=""
_T="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$_T" ] && [ -f "$_T/scripts/test-dev-skill.sh" ]; then
    REPO="$_T"
fi
# Layer 2 -- the registered directory-source install (the #278 precedent),
# reached only when the cwd is not inside an obsidian-brain checkout.
if [ -z "$REPO" ]; then
    REPO="$(python3 -c "
import json, os
def _ob_repo():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            if os.path.isfile(os.path.join(_i, 'scripts', 'test-dev-skill.sh')):
                return _i
    except Exception:
        pass
    return ''
print(_ob_repo())
")"
fi
if [ -z "$REPO" ] || [ ! -f "$REPO/scripts/test-dev-skill.sh" ]; then
    echo "ERROR: could not locate the obsidian-brain checkout. Looked for scripts/test-dev-skill.sh under the current git repo's toplevel, then for a directory-source marketplace entry in ~/.claude/plugins/known_marketplaces.json. /dev-test needs a local checkout to copy from; run it from the obsidian-brain repo, or register the checkout with /plugin marketplace add <path>." >&2
    exit 1
fi
# Several checkouts of this repo can coexist (worktrees, second clones, the
# registered one). Which tree was used must be observable, not inferred.
echo "Source checkout: $REPO"
bash "$REPO/scripts/test-dev-skill.sh" install
```

Report the output, then **branch on the command's exit status** — the fenced block's last command *is* the script, so the block's exit status is the script's. Never tell the user the install succeeded without checking it.

- **Exit 0 — installed.** Tell the user:

  > Dev version installed. **Start a new Claude Code session** to pick up the changes. When done testing, run `/dev-test restore`.

- **Exit 2 — installed, but the security tests failed.** The dev version *was* copied into the cache, so the install is not simply undone by ignoring it. Relay the script's error output and tell the user:

  > The dev version was copied into the plugin cache, but the **security tests failed** (see above). Do **not** start a new session against this install. Run `/dev-test restore` to revert it, fix the failures, then re-install.

- **Exit 3 — the install aborted partway; a backup is in place.** The backup was already published before the copy that failed, so the cache may now hold a mix of released and dev files. Relay the script's error output and tell the user:

  > The install **failed partway**. A backup of the original is in place and the cache **may hold a mix** of released and dev files. Run `/dev-test restore` to recover **before doing anything else** — until you do, the next `/dev-test install` will also refuse ("Backup already exists").

- **Any other non-zero exit — the install did not complete.** These are the paths that refuse *before* the cache is written — a guard rejecting the source tree, no cache to install into, a `.bak` already present, or a backup copy that failed (which the script states explicitly leaves the cache unmodified). Do **not** say a dev version was installed, and do **not** tell the user to start a new session. Relay the error output verbatim and stop; the script names the offending path and the remedy.

Stop here.

### Step 3 — Restore original

This works from any directory — it locates the obsidian-brain checkout itself, it does not require the cwd to be inside it. Run:

```bash
# Layer 1 -- the checkout you are STANDING IN, when it is itself an
# obsidian-brain checkout. A git worktree or a second clone is a deliberate
# context signal: that tree is the one you mean, and the registry can only
# ever name one checkout. The `-f "$_T/scripts/test-dev-skill.sh"` sentinel
# is what makes the cwd safe to trust here -- it can only ever select an
# obsidian-brain checkout, never the arbitrary project you happen to be
# working in. That project (#287's bug) has no sentinel and falls through
# to layer 2 exactly as before.
#
# NOTE: that sentinel check is LOAD-BEARING -- delete it and any foreign
# toplevel wins layer 1 outright, layer 2 is never consulted, and #287
# regresses. Pinned by
# test_shell_uses_the_registry_when_the_cwd_toplevel_lacks_the_sentinel.
REPO=""
_T="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$_T" ] && [ -f "$_T/scripts/test-dev-skill.sh" ]; then
    REPO="$_T"
fi
# Layer 2 -- the registered directory-source install (the #278 precedent),
# reached only when the cwd is not inside an obsidian-brain checkout.
if [ -z "$REPO" ]; then
    REPO="$(python3 -c "
import json, os
def _ob_repo():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            if os.path.isfile(os.path.join(_i, 'scripts', 'test-dev-skill.sh')):
                return _i
    except Exception:
        pass
    return ''
print(_ob_repo())
")"
fi
if [ -z "$REPO" ] || [ ! -f "$REPO/scripts/test-dev-skill.sh" ]; then
    echo "ERROR: could not locate the obsidian-brain checkout. Looked for scripts/test-dev-skill.sh under the current git repo's toplevel, then for a directory-source marketplace entry in ~/.claude/plugins/known_marketplaces.json. /dev-test needs a local checkout to copy from; run it from the obsidian-brain repo, or register the checkout with /plugin marketplace add <path>." >&2
    exit 1
fi
# Several checkouts of this repo can coexist (worktrees, second clones, the
# registered one). Which tree was used must be observable, not inferred.
echo "Source checkout: $REPO"
bash "$REPO/scripts/test-dev-skill.sh" restore
```

Report the output, then **branch on the command's exit status** — the fenced block's last command *is* the script, so the block's exit status is the script's. Never tell the user the restore succeeded without checking it.

- **Exit 0 — restored.** Tell the user:

  > Original version restored. **Start a new session** to pick up the restored version.

- **Exit 4 — there was nothing to restore.** No backup existed, so no dev version was installed and the cache already holds the released version. Nothing changed on disk. Do **not** claim a restore happened and do **not** send the user to restart a session for a state change that did not occur. Tell the user:

  > There was no dev install to undo — the cache already holds the released version. Nothing changed, so **no new session is needed**.

- **Any other non-zero exit — nothing was restored, or the restore aborted partway.** Do **not** say the original version is back. Relay the error output verbatim and stop. Two cases the script distinguishes and that are worth passing on in your own words: it *refused* an incomplete `.bak` (the live cache is untouched and still holds the dev version), or the swap *failed partway* (the cache may be missing and needs `/plugin marketplace update`).

Stop here.

### Step 4 — Show status

This works from any directory — it locates the obsidian-brain checkout itself, it does not require the cwd to be inside it. Run:

```bash
# Layer 1 -- the checkout you are STANDING IN, when it is itself an
# obsidian-brain checkout. A git worktree or a second clone is a deliberate
# context signal: that tree is the one you mean, and the registry can only
# ever name one checkout. The `-f "$_T/scripts/test-dev-skill.sh"` sentinel
# is what makes the cwd safe to trust here -- it can only ever select an
# obsidian-brain checkout, never the arbitrary project you happen to be
# working in. That project (#287's bug) has no sentinel and falls through
# to layer 2 exactly as before.
#
# NOTE: that sentinel check is LOAD-BEARING -- delete it and any foreign
# toplevel wins layer 1 outright, layer 2 is never consulted, and #287
# regresses. Pinned by
# test_shell_uses_the_registry_when_the_cwd_toplevel_lacks_the_sentinel.
REPO=""
_T="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$_T" ] && [ -f "$_T/scripts/test-dev-skill.sh" ]; then
    REPO="$_T"
fi
# Layer 2 -- the registered directory-source install (the #278 precedent),
# reached only when the cwd is not inside an obsidian-brain checkout.
if [ -z "$REPO" ]; then
    REPO="$(python3 -c "
import json, os
def _ob_repo():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            if os.path.isfile(os.path.join(_i, 'scripts', 'test-dev-skill.sh')):
                return _i
    except Exception:
        pass
    return ''
print(_ob_repo())
")"
fi
if [ -z "$REPO" ] || [ ! -f "$REPO/scripts/test-dev-skill.sh" ]; then
    echo "ERROR: could not locate the obsidian-brain checkout. Looked for scripts/test-dev-skill.sh under the current git repo's toplevel, then for a directory-source marketplace entry in ~/.claude/plugins/known_marketplaces.json. /dev-test needs a local checkout to copy from; run it from the obsidian-brain repo, or register the checkout with /plugin marketplace add <path>." >&2
    exit 1
fi
# Several checkouts of this repo can coexist (worktrees, second clones, the
# registered one). Which tree was used must be observable, not inferred.
echo "Source checkout: $REPO"
bash "$REPO/scripts/test-dev-skill.sh" status
```

Report the output, then **branch on the command's exit status** — the fenced block's last command *is* the script, so the block's exit status is the script's.

- **Exit 0 — report the status verbatim** (installed cache version, cache dir, and whether the dev version is active).
- **Any non-zero exit — this is not a status report, it is a failure.** Do **not** paraphrase it as "no dev version is installed". Relay the error output verbatim and stop; the script names the offending path and the remedy (for example, a cache directory holding nothing but a `.bak`, which needs one rename to recover).
