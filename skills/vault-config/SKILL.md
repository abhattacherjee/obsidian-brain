---
name: vault-config
description: "Interactive configuration menu for obsidian-brain settings. Use when: (1) /vault-config to view and change settings, (2) user wants to toggle log_raw_messages, (3) user wants to adjust session filtering thresholds."
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

# Vault Config — Manage obsidian-brain Settings

Interactive menu for viewing and changing obsidian-brain configuration, one setting at a time.

**Tools needed:** native shell, native file reading, trusted publication

## Procedure

### Step 1 — Load current config

Run:

Request for `config` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'config' < "$REQUEST_PATH"
```

Parse the JSON output into a config dict.

If error, tell the user to run `/obsidian-setup` first. Stop.

### Step 2 — Display settings table

Present the current settings as a numbered table:

```
Obsidian Brain Configuration

1. vault_path             <value>
2. sessions_folder        <value>
3. insights_folder        <value>
4. wiki_folder            <value>      <- wiki pages from /vault-ask (#383; empty = off)
5. log_raw_messages       <value>      <- controls raw conversation logging
6. min_turns              <value>      <- minimum turns to log a session
7. min_duration_minutes   <value>      <- minimum duration to log
8. snapshot_on_compact    <value>      <- checkpoint note on /compact
9. snapshot_on_clear      <value>      <- checkpoint note on /clear

Enter a number to change, or 'done' to exit.
```

For any key not present in the config, show its default value: `wiki_folder` defaults to `claude-wiki`, `log_raw_messages` defaults to `true`, `min_turns` defaults to `3`, `min_duration_minutes` defaults to `2`, `snapshot_on_compact` defaults to `true`, `snapshot_on_clear` defaults to `true`.

When rendering rows 8 or 9, if the value is `False`, append this warning inline on the same row:

> ⚠ Disables pre-clear/compact checkpoint. Recommended: True.

The warning only fires when the value is explicitly `False` — don't show it for the default `true`.

### Step 3 — Handle user selection

Wait for user input.

- `done` or empty → Stop. Print "Configuration unchanged."
- Number → Go to Step 4 with the selected setting.

### Step 4 — Change setting

- **Boolean settings** (`log_raw_messages`, `snapshot_on_compact`, `snapshot_on_clear`): Toggle the value (true→false, false→true). If the user is about to set `snapshot_on_compact` or `snapshot_on_clear` to `False`, warn first:

  > ⚠ Disabling this removes the pre-compact/clear checkpoint safety net. You will lose in-flight context on accidental `/clear`. Proceed? (y/N)

  Only toggle after explicit confirmation.
- **String settings** (`vault_path`, `sessions_folder`, `insights_folder`, `wiki_folder`): Show current value, ask for new value.
- **Number settings** (`min_turns`, `min_duration_minutes`): Show current value, ask for new value. Validate it's a positive integer.

### Step 5 — Write updated config

Run:

Request for `configure`:

```json
{
  "expected_revision": "<config-read SHA256>",
  "settings": {
    "<setting>": "<value>"
  }
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'configure' < "$REQUEST_PATH"
```

Where `$KEY` is the setting name and `$JSON_VALUE` is the new value as a JSON literal (e.g., `"false"`, `"3"`, `'"/path/to/vault"'`).

If the changed key is `sessions_folder`, `insights_folder` or `wiki_folder`: other skills in this session read a cached copy of the config, so tell the user to run `/vault-reindex`, which reads the config fresh and re-indexes the new folders. For `wiki_folder`, first check the value: it must be a relative folder name with no `..`, `~` or dot-prefixed segment (an empty value turns the wiki folder off). `/vault-reindex` refuses an invalid value and names it.

Confirm the change, then go back to Step 2 to redisplay the table.

## Fixed request shapes

Pass these objects through the installed launcher for the named operation. Keep
one operation ID across source reads, analysis and reviewed publication.

Request for `config-read`:

```json
{}
```
