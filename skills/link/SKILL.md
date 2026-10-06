---
name: link
description: "Cross-references related Obsidian vault notes with bidirectional wikilinks. Use when: (1) /link to auto-suggest connections for the current session, (2) /link <description> to link specific notes, (3) user wants to connect related notes."
metadata:
  version: 1.0.0
---

## Native runtime and installed resources

Use the absolute path of this loaded `SKILL.md` as `OB_SKILL_PATH`. Read the
reference for the invoking host when this skill has paired host references.
Set `OB_HOST`, `OB_CLIENT`, `OB_SESSION_ID`, and `OB_CWD` from that native
invocation. The current client must be explicitly supplied by the invoking runtime.
If that binding is unavailable, stop and report it. Never label a Desktop
invocation as a CLI invocation or infer the frontend from transcript creation
metadata or inherited environment markers. Use the selected host's own session ID. Keep curated note taxonomy
separate from `agent_provider` and `agent_session_id` provenance.

```bash
: "${OB_CLIENT:?Current native client binding is unavailable; stop without choosing a frontend.}"
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

Before drafting an edit or summary, call `note-read` and retain its exact `expected_revision`. Use `note-apply` with that revision for the reviewed edit. A conflict preserves the current note; show the pending result and do not count it as saved. Use only the operations documented for this skill. Their writes bind the source revisions before analysis and preserve manual edits on conflict. Content is JSON data, never shell code. Read `references/host-claude.md` or `references/host-codex.md` when present. Codex has no native memory-file API; shared vault retrieval and wiki filing continue without borrowing another host's memory.

# Link — Cross-Reference Vault Notes with Bidirectional Wikilinks

Search the Obsidian vault for notes related to the current session or a specific description, then create bidirectional wikilinks between them in their respective `## Related` sections.

**Tools needed:** native shell, native content search, native file reading, trusted publication, conditional trusted updates

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config

Run:

Request for `config` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'config' < "$REQUEST_PATH"
```

Parse the single JSON object. Read its named fields; do not split output on `=`.

If the command exits non-zero or prints ERROR, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

### Step 2 — Validate vault access

Run:

```bash
test -d "$VAULT_PATH/$SESSIONS_FOLDER" && test -d "$VAULT_PATH/$INSIGHTS_FOLDER" && echo "OK" || echo "FAIL"
```

If FAIL, tell the user:

> The vault folders do not exist. Run `/obsidian-setup` to fix this.

Stop here if FAIL.

### Step 3 — Determine mode

Check if the user provided an argument after `/link`.

- **Without argument** (bare `/link`): Go to Step 4A — auto-suggest connections for the current session.
- **With argument** (e.g. `/link authentication flow notes`): Go to Step 4B — explicit link by description.

### Step 4A — Auto-suggest connections

**4A.1 — Derive project name:**

```bash
basename "$(pwd)"
```

Store as `PROJECT`. Normalize: lowercase, hyphens for spaces.

**4A.2 — Find most recent session note for this project:**

```bash
ls -t "$VAULT_PATH/$SESSIONS_FOLDER"/ | head -20
```

From that list, identify the most recent file whose name contains the project name (or whose frontmatter contains `project: $PROJECT`). If you cannot determine from the filename alone, run the fixed `grep` operation:

Keep returned paths under `$VAULT_PATH/$SESSIONS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "project: $PROJECT", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

Sort the matched files by modification time and pick the most recent one. This is the **source note**. Read its full content using native file reading.

**4A.3 — Extract keywords:**

From the current conversation topics and the source note's content, identify 3-5 meaningful keywords or concepts. Prefer: technology names, method names, architectural terms, error types, product feature names. Avoid generic words like "session", "note", "file".

**4A.4 — Search vault for related notes (parallel):**

Run these searches in parallel with the fixed `grep` operation, using `ignore_case: true`:

For each keyword (run as parallel searches across both folders):

Keep returned paths under `$VAULT_PATH/$SESSIONS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "<keyword>", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

Keep returned paths under `$VAULT_PATH/$INSIGHTS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "<keyword>", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

Collect all matched files across all keywords. Exclude the source note itself.

**4A.5 — Search by overlapping tags:**

Read the source note's frontmatter and extract its `tags:` list. For each tag that starts with `claude/topic/`, run:

Keep returned paths under `$VAULT_PATH/$SESSIONS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "<tag>", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

Keep returned paths under `$VAULT_PATH/$INSIGHTS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "<tag>", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

Add any new matches to your candidate list, excluding the source note.

**4A.6 — Rank candidates:**

Score each candidate file:
- +1 for each keyword match
- +1 for each shared tag
- +2 bonus if the file is in the insights folder (insights/decisions are higher value than raw sessions)

Sort by score descending. Take the top 5.

**4A.7 — Present candidates:**

> **Suggested connections for this session:**
>
> 1. [[note-filename-without-extension]] — <reason: shares topics X and Y>
> 2. [[note-filename-without-extension]] — <reason>
> 3. [[note-filename-without-extension]] — <reason>
>
> Which would you like to link? (e.g. `1,3` or `all` or `none`)

Wait for the user's response. If `none`, stop. For each selected candidate, go to Step 5 with the source note and that candidate as the target.

### Step 4B — Explicit link request

**4B.1 — Identify source note:**

If the user's description contains "this session", "current session", or similar: derive the project name from `basename "$(pwd)"` and find the most recent session note for that project (same as Step 4A.2).

Otherwise, use the user's description to search for a source note:

Keep returned paths under `$VAULT_PATH/$SESSIONS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "<keywords from description>", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

Keep returned paths under `$VAULT_PATH/$INSIGHTS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "<keywords from description>", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

**4B.2 — Identify target note:**

Parse the user's description for target note clues. Search both folders:

Keep returned paths under `$VAULT_PATH/$SESSIONS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "<target keywords>", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

Keep returned paths under `$VAULT_PATH/$INSIGHTS_FOLDER/`. Send this JSON to the fixed `grep` operation:

```json
{"pattern": "<target keywords>", "ignore_case": true}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'grep' < "$REQUEST_PATH"
```

**4B.3 — Resolve ambiguity:**

If multiple matches were found for either source or target, present them and ask the user to pick:

> Found multiple matches. Which note did you mean?
>
> 1. [[note-filename-1]]
> 2. [[note-filename-2]]

Wait for the user's selection. Once both source and target are unambiguously identified, go to Step 5.

### Step 5 — Create bidirectional link

For each source-target pair:

**5.1 — Read the source note** using native file reading.

**5.2 — Check for duplicate link in source:**

Search the source note's content for `[[target-filename]]` (filename without `.md`). If the link already exists in the source, skip writing to the source and note it:

> [[source-note]] already links to [[target-note]] — skipping source.

**5.3 — Add link to source note:**

- If the source note has a `## Related` section: prepare a revision-bound `note-apply` request to append a new line to that section:
  `- [[target-filename]] — <one-line reason for the connection>`
- If no `## Related` section exists: prepare a revision-bound `note-apply` request to append the following to the end of the file:

  ```
  
  ## Related
  
  - [[target-filename]] — <one-line reason for the connection>
  ```

- Preserve the note bytes captured by `note-read`. Publish the approved document with `note-apply` and that pre-analysis SHA256. A conflict keeps the operation pending.

After writing, set permissions:

```bash
chmod 600 "<source-note-path>"
```

**5.4 — Read the target note** using native file reading.

**5.5 — Check for duplicate link in target:**

Search the target note's content for `[[source-filename]]`. If it already exists, skip writing to the target:

> [[target-note]] already links back to [[source-note]] — skipping target.

**5.6 — Add link to target note** (mirror of 5.3, but reversed — source becomes the linked note):

- If the target note has a `## Related` section: append:
  `- [[source-filename]] — <one-line reason for the connection>`
- If no `## Related` section: append:

  ```
  
  ## Related
  
  - [[source-filename]] — <one-line reason for the connection>
  ```

After writing, set permissions:

```bash
chmod 600 "<target-note-path>"
```

### Step 6 — Confirm

For each linked pair, print:

> **Linked:** [[source-note]] ↔ [[target-note]]
> Reason: <connection reason>
> Links are bidirectional — both notes now reference each other in their Related section.

If multiple pairs were linked, print a summary after all pairs are done:

> **All done.** N bidirectional link(s) created.

## Edge Cases

- **No session note found for current project:** Tell the user no session note was found. Suggest they work in a session so one is auto-logged at session end, then run `/link` again.
- **No candidates found in auto-suggest:** Tell the user no related notes were found for the extracted keywords. Suggest running `/link <specific description>` to search manually.
- **Already linked (both directions):** Tell the user both notes already reference each other — no changes needed.
- **Only one direction already linked:** Add only the missing direction; do not duplicate the existing link.
- **Wikilink format:** Always use the filename without the `.md` extension: `[[2026-04-04-obsidian-brain-297a]]` not `[[2026-04-04-obsidian-brain-297a.md]]`.
- **Large vault (50+ files):** Never glob or read the entire folder. Always use the fixed `grep` operation to search — never `ls` the whole folder for matching.
- **Config exists but vault path is invalid:** Warn the user and suggest running `/obsidian-setup` again.

## Fixed request shapes

Pass these objects through the installed launcher for the named operation. Keep
one operation ID across source reads, analysis and reviewed publication.

Request for `note-read`:

```json
{
  "operation_id": "<prepared id>",
  "path": "<vault-relative note.md>"
}
```

Request for `note-apply`:

```json
{
  "operation_id": "<same prepared id>",
  "path": "<same vault-relative note.md>",
  "expected_revision": "<note-read SHA256>",
  "content": "<complete reviewed note>"
}
```
