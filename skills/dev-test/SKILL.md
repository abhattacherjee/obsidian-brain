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
if [ -z "$REPO" ]; then
    _T="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$_T" ] && [ -f "$_T/scripts/test-dev-skill.sh" ]; then
        REPO="$_T"
    fi
fi
if [ -z "$REPO" ] || [ ! -f "$REPO/scripts/test-dev-skill.sh" ]; then
    echo "ERROR: could not locate the obsidian-brain checkout. Looked for a directory-source marketplace entry in ~/.claude/plugins/known_marketplaces.json, then for scripts/test-dev-skill.sh under the current repo. /dev-test needs a local checkout to copy from; run it from the obsidian-brain repo, or register the checkout with /plugin marketplace add <path>." >&2
    exit 1
fi
bash "$REPO/scripts/test-dev-skill.sh" install
```

Report the output. Then tell the user:

> Dev version installed. **Start a new Claude Code session** to pick up the changes. When done testing, run `/dev-test restore`.

Stop here.

### Step 3 — Restore original

This works from any directory — it locates the obsidian-brain checkout itself, it does not require the cwd to be inside it. Run:

```bash
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
if [ -z "$REPO" ]; then
    _T="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$_T" ] && [ -f "$_T/scripts/test-dev-skill.sh" ]; then
        REPO="$_T"
    fi
fi
if [ -z "$REPO" ] || [ ! -f "$REPO/scripts/test-dev-skill.sh" ]; then
    echo "ERROR: could not locate the obsidian-brain checkout. Looked for a directory-source marketplace entry in ~/.claude/plugins/known_marketplaces.json, then for scripts/test-dev-skill.sh under the current repo. /dev-test needs a local checkout to copy from; run it from the obsidian-brain repo, or register the checkout with /plugin marketplace add <path>." >&2
    exit 1
fi
bash "$REPO/scripts/test-dev-skill.sh" restore
```

Report the output. Then tell the user:

> Original version restored. **Start a new session** to pick up the restored version.

Stop here.

### Step 4 — Show status

This works from any directory — it locates the obsidian-brain checkout itself, it does not require the cwd to be inside it. Run:

```bash
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
if [ -z "$REPO" ]; then
    _T="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$_T" ] && [ -f "$_T/scripts/test-dev-skill.sh" ]; then
        REPO="$_T"
    fi
fi
if [ -z "$REPO" ] || [ ! -f "$REPO/scripts/test-dev-skill.sh" ]; then
    echo "ERROR: could not locate the obsidian-brain checkout. Looked for a directory-source marketplace entry in ~/.claude/plugins/known_marketplaces.json, then for scripts/test-dev-skill.sh under the current repo. /dev-test needs a local checkout to copy from; run it from the obsidian-brain repo, or register the checkout with /plugin marketplace add <path>." >&2
    exit 1
fi
bash "$REPO/scripts/test-dev-skill.sh" status
```

Report the output.
