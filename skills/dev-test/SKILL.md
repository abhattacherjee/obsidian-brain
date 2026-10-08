---
name: dev-test
description: "Install dev version of obsidian-brain into the plugin cache for local testing, or restore the original. Note: on a directory-source marketplace install the skills already load the checkout (#278), so install is only needed for github-source installs and the cache-asserting test scripts. Use when: (1) /dev-test install to test unreleased changes, (2) /dev-test restore to put back the original, (3) /dev-test to check current status."
metadata:
  version: 1.0.0
---

## Native runtime and installed resources

Use the absolute path of this loaded `SKILL.md` as `OB_SKILL_PATH`. Read the
reference for the invoking host when this skill has paired host references.
Set `OB_HOST`, `OB_SESSION_ID`, and `OB_CWD` from that native invocation.
Claude has one frontend: use the fixed host client `claude-code`. Reject an
inherited `OB_CLIENT` that differs, including an empty declaration.
For Codex, read the operator-declared, inherited `OB_CLIENT`; never choose or
export it yourself. It must be `codex-cli` or `codex-desktop`; if missing or
invalid, stop with "Current native client binding is unavailable". Never label a Desktop
invocation as a CLI invocation or infer the frontend from transcript creation
metadata or inherited environment markers. Use the selected host's own session ID. Keep curated note taxonomy
separate from `agent_provider` and `agent_session_id` provenance.

```bash
case "$OB_HOST" in
  claude)
    if [ "${OB_CLIENT+x}" = x ] && [ "$OB_CLIENT" != claude-code ]; then
      printf '%s\n' 'Current native client binding is unavailable: conflicting Claude declaration.' >&2
      exit 1
    fi
    OB_CLIENT=claude-code
    ;;
  codex)
    case "${OB_CLIENT:-}" in
      codex-cli|codex-desktop) ;;
      *) printf '%s\n' 'Current native client binding is unavailable; stop without choosing a frontend.' >&2; exit 1 ;;
    esac
    ;;
  *) printf '%s\n' 'Current native client binding is unavailable: unknown host.' >&2; exit 1 ;;
esac
OB_SKILL_PATH='<absolute path of this loaded SKILL.md>'
OB_RESOURCE_ROOT=$(python3 -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); assert p.is_absolute(); p=p.resolve(); assert p.name == "SKILL.md" and p.parent.parent.name == "skills"; print(p.parents[2])' "$OB_SKILL_PATH")
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" context < /dev/null
```

Use the returned `config_path`, `vault_path`, `index_path`, and `state_path`.
Create the operation with this fixed literal request:

```bash
printf '{}' | python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'prepare'
```

Call `prepare` to create a private operation under native state. Retain its
`operation_id` and `operation_dir`. Register approved helper output names with `artifact-store`;
inputs are read through the immutable artifact manifest. Do not discover resources from the current directory or another plugin
cache. Each shell invocation supplies the same explicit values; a previous
shell's variables are not assumed to persist.

Each data operation uses the installed launcher with a JSON request on stdin:

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation '<fixed operation>' < "$REQUEST_PATH"
```

Map config JSON `vault_path`, `sessions_folder`, and `insights_folder` to the
procedure variables `VAULT_PATH`/`VAULT`, `SESSIONS_FOLDER`/`SESS`, and
`INSIGHTS_FOLDER`/`INS`. Use the canonical project returned in config JSON (and native `session` when available),
not the basename of an unrelated shell working directory.

Use only the operations documented for this skill. Their writes bind the source revisions before analysis and preserve manual edits on conflict. Content is JSON data, never shell code. Read `references/host-claude.md` or `references/host-codex.md` when present. Codex has no native memory-file API; shared vault retrieval and wiki filing continue without borrowing another host's memory.


# Dev Test — Install/Restore Dev Plugin for Testing

Swap the invoking host's installed plugin cache with the working copy for local testing. After install, start a new session in that host. Use Claude Code for a Claude installation or the selected Codex client for a Codex installation; cache distribution checks do not certify native hook dispatch.

Use the absolute loaded SKILL.md to select the installed resource root.

**Tools needed:** native shell

## Procedure

### Step 1 — Parse argument

Check the argument passed to `/dev-test`:

- `install` → go to Step 2
- `restore` → go to Step 3
- No argument or `status` → go to Step 4

For Codex, obtain the explicit installed cache path from native plugin metadata
or the operator. It must select one verified obsidian-brain marketplace/version
under the frozen native home's plugin cache. Pass it as `cache_path`
for install, status and restore. Refuse a missing or unverified selection;
never choose the highest version or infer the current frontend from the path.
Omit `cache_path` for Claude.

For install and restore from an installed skill, obtain an explicit verified
external obsidian-brain checkout or runtime package from the operator. Pass its
absolute path as `source_path`. It must exist outside the selected native home
and cannot traverse symlinks or parent directories. Verify the source commit
or package before selecting it. Never infer a source from cwd or select another
installation. The loaded skill still runs its own installed launcher. A skill
loaded from an external checkout may omit `source_path` and use that checkout.
Status does not require an external source.

On Python 3.9, Codex config validation accepts tables, dotted keys, strings,
booleans, finite numbers, and one-line arrays or inline tables. Unsupported
multiline values, dates, and arrays of tables leave config and cache unchanged.
Use Python 3.11 or later for those TOML forms. No extra package is required.

### Step 2 — Install dev version

This works from any directory. Use the verified external source selected above; cwd does not select it. Run:

Request for `dev-install`:

```json
{
  "mode": "install",
  "source_path": "<verified external obsidian-brain source; omit only when loaded from that checkout>",
  "cache_path": "<explicit verified Codex installed cache path; omit for Claude>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'dev-install' < "$REQUEST_PATH"
```

Report the output, then **branch on the command's exit status** — the fenced block's last command *is* the script, so the block's exit status is the script's. Never tell the user the install succeeded without checking it.

- **Exit 0 — installed.** Tell the user:

  > Dev version installed. **Start a new session in the invoking host** to pick up the changes. When done testing, run `/dev-test restore`.

- **Exit 2 — installed, but the security tests failed.** The dev version *was* copied into the cache, so the install is not simply undone by ignoring it. Relay the script's error output and tell the user:

  > The dev version was copied into the plugin cache, but the **security tests failed** (see above). Do **not** start a new session against this install. Run `/dev-test restore` to revert it, fix the failures, then re-install.

- **Exit 3 — the install aborted partway; a backup is in place.** The backup was already published before the copy that failed, so the cache may now hold a mix of released and dev files. Relay the script's error output and tell the user:

  > The install **failed partway**. A backup of the original is in place and the cache **may hold a mix** of released and dev files. Run `/dev-test restore` to recover **before doing anything else** — until you do, the next `/dev-test install` will also refuse ("Backup already exists").

- **Any other non-zero exit — the install did not complete.** These are the paths that refuse *before* the cache is written — a guard rejecting the source tree, no cache to install into, a `.bak` already present, or a backup copy that failed (which the script states explicitly leaves the cache unmodified). Do **not** say a dev version was installed, and do **not** tell the user to start a new session. Relay the error output verbatim and stop; the script names the offending path and the remedy.

Stop here.

### Step 3 — Restore original

This works from any directory. Use the verified external source selected above; cwd does not select it. Run:

Request for `dev-install` (substitute the values as data):

```json
{
  "mode": "restore",
  "source_path": "<same verified external source used for install>",
  "cache_path": "<same explicit verified Codex cache path; omit for Claude>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'dev-install' < "$REQUEST_PATH"
```

Report the output, then **branch on the command's exit status** — the fenced block's last command *is* the script, so the block's exit status is the script's. Never tell the user the restore succeeded without checking it.

- **Exit 0 — restored.** Tell the user:

  > Original version restored. **Start a new session** to pick up the restored version.

- **Exit 4 — there was nothing to restore.** No backup existed, so no dev version was installed and the cache already holds the released version. Nothing changed on disk. Do **not** claim a restore happened and do **not** send the user to restart a session for a state change that did not occur. Tell the user:

  > There was no dev install to undo — the cache already holds the released version. Nothing changed, so **no new session is needed**.

- **Any other non-zero exit — nothing was restored, or the restore aborted partway.** Do **not** say the original version is back. Relay the error output verbatim and stop. Two cases the script distinguishes and that are worth passing on in your own words: it *refused* an incomplete `.bak` (the live cache is untouched and still holds the dev version), or the swap *failed partway* (the cache may be missing and needs `/plugin marketplace update`).

Stop here.

### Step 4 — Show status

This works from any directory and uses the loaded launcher. No external source is needed. Run:

Request for `dev-install` (substitute the values as data):

```json
{
  "mode": "status",
  "cache_path": "<explicit verified Codex installed cache path; omit for Claude>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'dev-install' < "$REQUEST_PATH"
```

Report the output, then **branch on the command's exit status** — the fenced block's last command *is* the script, so the block's exit status is the script's.

- **Exit 0 — report the status verbatim** (installed cache version, cache dir, and whether the dev version is active).
- **Any non-zero exit — this is not a status report, it is a failure.** Do **not** paraphrase it as "no dev version is installed". Relay the error output verbatim and stop; the script names the offending path and the remedy (for example, a cache directory holding nothing but a `.bak`, which needs one rename to recover).
