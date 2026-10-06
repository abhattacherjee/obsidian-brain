---
name: recall
description: "Pure read-only context resume — summarizes unsummarized notes and surfaces last-session context. Use `/check-items` to triage open items. Use when: (1) /recall command, (2) /recall <project-name>, (3) resuming work on a project and wanting prior context."
metadata:
  version: 1.7.0
---

## Native runtime and installed resources

Use the absolute path of this loaded `SKILL.md` as `OB_SKILL_PATH`. Read the
reference for the invoking host when this skill has paired host references.
Set `OB_HOST`, `OB_CLIENT`, `OB_SESSION_ID`, and `OB_CWD` from that native
invocation. Use the selected host's own session ID. Keep curated note taxonomy
separate from `agent_provider` and `agent_session_id` provenance.

```bash
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

Before preparing edits or requesting a summary of an existing note, call
`note-read` and retain its exact `expected_revision`. Apply the proposed note
with `note-apply` and that revision. A conflict leaves the current note intact;
show the pending result and do not count the note as saved. New curated notes
use `note-create`; they never overwrite a collision. Native memory discovery
is unsupported for Codex until its adapter is verified; shared vault retrieval
and wiki filing continue without borrowing another host's memory.

Read `references/host-claude.md` or `references/host-codex.md` when present.
All note writes described below use `note-create` or revision-bound `note-apply`,
including bidirectional related links. Content is a JSON string, never shell code.
Keep this rule when a later step uses the word Write or Edit.

# Recall — Load Project Context from Obsidian Vault

Searches the Obsidian vault for session notes and insights matching the current project, upgrades any unsummarized notes with AI summaries, and presents a concise context brief.

**Tools needed:** Bash, Grep, Read, Write

## Procedure

Follow these steps exactly. Do not skip steps or reorder them.

### Step 1 — Load config and derive project

Run a single call that loads config and derives the project name (saves one parent round):

Request for `config` (substitute the values as data):

```json
{}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'config' < "$REQUEST_PATH"
```

Parse each output line as KEY=VALUE, splitting on the first `=`. Also capture PIPELINE (defaults to "auto").

If the user passed a project name argument (e.g. `/recall my-project`), override `PROJECT` with that value.

If the output is empty or errors, tell the user:

> Config not found. Run `/obsidian-setup` first to configure your Obsidian vault.

Stop here if config is missing.

### Step 1b — Recover retained native facts

Before finding or summarizing notes, run the shared recovery command. Select the invoking host and client explicitly: `claude` / `claude-code`, `codex` / `codex-cli`, or `codex` / `codex-desktop`. Use the installed skill's adjacent `hooks` resource path. Pass the authoritative native session ID with `--session-id` when the native tool environment does not supply it. Do not derive identity or client from an old transcript.

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" recover < /dev/null
```

Recovery processes at most eight registered sources within one second. It can retain facts from an active session; it does not finalize that session. Read the JSON `status`, `pending_sources`, `loss_of_input`, and `warnings`. When recovery remains pending or unavailable, say so and continue reading the notes already saved. If `loss_of_input` is true, report lost source input even when `status` is `complete`. Do not claim complete recovery from a partial result. Recovery uses no AI; summaries remain a separate step.

**Create the task manifest** for the full `/recall` flow:

```
Native progress task: subject="Find unsummarized notes", activeForm="Searching for unsummarized notes"
Native progress task: subject="Summarize unsummarized notes", activeForm="Summarizing notes"
Native progress task: subject="Present read-only context brief", activeForm="Building and presenting context brief"
```

Track the returned task IDs — you will update them as each step completes. Immediately set task #1 to `in_progress` via Native progress update.

### Step 2 — Summarize unsummarized notes (deferred summarization, truncation-aware)

> ⚠️ **THIS STEP IS MANDATORY. DO NOT SKIP IT.**
>
> If Grep finds any file matching both `status: auto-logged` AND `project: $PROJECT`, you **must** produce an upgraded summary for every such file before proceeding to Step 3. "Skipping to save context" or "the other session covers it" is a bug, not an optimization — the user ran `/recall` specifically to get current-session context, and stale unsummarized notes are exactly what they asked you to fix.
>
> **Visibility requirement:** Before Step 3, emit a one-line status: `Step 2: processing N unsummarized note(s) for $PROJECT` (or `Step 2: no unsummarized notes for $PROJECT` if the intersection is empty). This makes the decision auditable in the tool trace.

Find unsummarized notes for this project in a single Python call (replaces multiple Grep rounds):

Request for `unsummarized` (substitute the values as data):

```json
{
  "project": "<canonical project>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'unsummarized' < "$REQUEST_PATH"
```

**Optional flags (#168 aged-note deferral):**
- If the user passed `--include-aged`, call with `include_aged=True` to include aged-out deferred notes:
  ```python
  find_unsummarized_notes(vault, sessions_folder, project, include_aged=True)
  ```
- If the user passed `--max-age-days N`, call with `aged_threshold_days=N` to override the config threshold:
  ```python
  find_unsummarized_notes(vault, sessions_folder, project, aged_threshold_days=N)
  ```

Parse the JSON output: `{"unsummarized": ["/path/to/note1.md", ...], "auto_fixed": N, "skipped_aged": [...]}`.

The function handles project filtering, defense-in-depth (skips notes with real `## Summary` but stale `auto-logged` status, auto-fixes them), and returns only genuinely unsummarized note paths.

If `auto_fixed > 0`, report: `Auto-fixed N note(s) with stale status.`

If `skipped_aged` is non-empty, report: `Skipped <len> aged-out unreferenced note(s) (>90d, no inbound links, not pinned). Run \`/recall --include-aged\` to summarize them anyway.` (Use the actual configured threshold from `aged_summarize_threshold_days`, default 90d.) This is the #168 deferral.

Store the length of `unsummarized` as `N`.

Update task #1 to completed. Update task #2 subject to `Summarize N unsummarized note(s)` and set to `in_progress`.

#### Path A: N=0 (no unsummarized notes)

Update task #2 subject to `No unsummarized notes found` and set to `completed`. Skip to Step 3.

#### Path B: N>=1 (parallel native pipelines with sub-agent fallback)

> **Config escape hatch (#84):** If `PIPELINE=subagent`, skip Phase 1 and use Phase 2 for every note. With `PIPELINE=auto`, run the bound native backend first. Claude startup details are in [the Claude reference](references/host-claude.md).

**Task management threshold:** If N <= 5, create a sub-task per note. If N > 5, skip per-note sub-tasks — use a single progress update on task #2 instead. This saves ~15-20s of parent round-trip overhead at large N.

##### Phase 1 — Parallel native upgrades (single batch call)

If N <= 5, create a sub-task for each note (subject `"Upgrade: <basename>"`, activeForm `"Upgrading <basename> via the native backend"`).

**Single Bash tool call** — `upgrade_batch()` uses the invoking host’s bound AI backend and bounded batch concurrency.

Request for `upgrade-batch` (substitute the values as data):

```json
{
  "paths": [
    "<eligible source notes>"
  ],
  "project": "<canonical project>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'upgrade-batch' < "$REQUEST_PATH"
```

Parse the returned JSON. A top-level `"error"` sends every note to Phase 2. Otherwise each result includes `path`, `status`, `elapsed_s`, the actual `model_used` when reported, and `fallback_reason`. The backend may batch session notes (`summary_batch_size`, default 3; use 1 to disable). Per-note or whole-batch failures remain pending and enter Phase 2. Never claim a configured alias was the actual model.
- `status` starts with `Upgraded ` → mark as succeeded
- anything else (including `Failed: ...`, empty, or unexpected prefix) → add to the Phase 2 fallback list

The stderr line emits a per-model breakdown visible in the tool trace (e.g. `Step 2: upgraded 7 note(s) in 2.8s wall (5 haiku / 2 fallback)`).

If N <= 5: update each sub-task accordingly (succeeded or `Failed: <basename>`).
If N > 5: update task #2 subject to `Upgrade N notes: M succeeded, F pending fallback`.

Use one trusted batch call so the selected native backend controls bounded concurrency.

##### Phase 2 — Sub-agent fallback (only for failed notes)

If no failures, skip this phase entirely.

For each failed note, run `note-read` to bind its exact current revision before analysis. Then use a native analysis helper; if unavailable, perform the same analysis inline. If multiple notes failed, spawn all sub-agents in a **single message turn**:

```
Native analysis helper({
  description: "Summarize session note <basename>",
  prompt: "Read the session note at <NOTE_PATH>. Produce a structured summary with these exact markdown sections:\n\n## Summary\n1-3 sentence overview of what was accomplished.\n\n## Key Decisions\n- Bullet list of important technical decisions. Write \"None noted.\" if none.\n\n## Changes Made\n- Bullet list of files modified/created with brief description. Write \"None noted.\" if none.\n\n## Errors Encountered\n- Bullet list of errors and how resolved. Write \"None.\" if none.\n\n## Open Questions / Next Steps\n- [ ] Checkbox list of unresolved items. Write \"None.\" if none.\n\nReturn the summary text to the parent; do not publish vault or scratch files. After the summary sections, add a final line:\nIMPORTANCE: N\nwhere N is 1-10. 1-3: trivial (config, interrupted). 4-6: standard work. 7-8: key decisions or error resolutions. 9-10: major releases or security audits.\n\nReturn the complete summary text and IMPORTANCE line."
})
```

When sub-agents return, for each:

1. Require complete returned summary text with the requested sections. Keep the SHA256 from `note-read`; do not read again to rebind it after analysis.
2. Apply the returned text through the trusted launcher:

   Request for `summary-apply` (substitute the values as data):

   ```json
   {
     "operation_id": "<prepared id>",
     "path": "<source note>",
     "expected_revision": "<pre-model SHA256>",
     "summary": "<generated summary>"
   }
   ```

   ```bash
   python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'summary-apply' < "$REQUEST_PATH"
   ```

   If the write-back status starts with `Failed:`, count this note as permanently failed — do NOT count it as upgraded. If N <= 5, update the per-note sub-task to `Permanently failed: <basename>`.

   If the write-back succeeds, and N <= 5, update the per-note sub-task to `Fallback succeeded: <basename>`.

3. Missing or invalid summary output leaves the note pending for the next `/recall`. Report the failure. Helpers return text, so no temporary-file cleanup is needed.

If N > 5: update task #2 subject to reflect final Phase 2 results (e.g. `Upgrade N notes: M native + F fallback succeeded, K failed`).

##### Completion

Mark task #2 as completed. Report results:
- How many upgraded via the native backend pipeline (Phase 1 successes)
- How many upgraded via sub-agent fallback (Phase 2 write-back successes)
- How many permanently failed (notes where both Phase 1 native backend AND Phase 2 sub-agent fallback failed or were skipped — these stay unsummarized for next `/recall`)

For failed notes: "Note `<basename>` could not be summarized. It will be retried on the next `/recall`."

### Step 3 — Build context brief (Python)

Update task #3 to `in_progress`.

Run a single Python call that reads all session and insight files and composes the brief — no sub-agent needed:

Request for `brief` (substitute the values as data):

```json
{
  "project": "<canonical project>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'brief' < "$REQUEST_PATH"
```

The first line of the emitted `CONTEXT_BRIEF` is always the hook-status line. If it starts with `[OK]`, omit it from the displayed output — the user doesn't need to see "session logging active" every time. If it starts with `[WARN]`, display it verbatim so the user knows to take action (e.g., run `/obsidian-setup`).

If the command fails (non-zero exit code), print the error and stop — do not fall back to in-context reads.

**Parse the output.** Split on section labels:

1. Extract `<<<OB_CONTEXT_BRIEF>>>` — everything between this delimiter and `<<<OB_LOAD_MANIFEST>>>`. This is the brief to display.
2. Extract `<<<OB_LOAD_MANIFEST>>>` — parse `full_session_title`, `full_session_date`, `full_session_path`, `summary_session_title`, `summary_session_date`, `insight_count`, `snapshot_count` (optional), and all `snapshot:` lines (there may be zero or more, each followed by optional 2-space-indented `key_context` bullets).
3. Extract `<<<OB_OPEN_ITEM_CANDIDATES>>>` — either `NO_CANDIDATES`, `NO_ITEMS`, or a JSON array. Count the number of `- [ ]` items across all scanned session notes. Store as `open_items_total`. When the payload is a JSON array, each element may carry two optional fields — `contradicted_by` (a `YYYY-MM-DD` date) and `contradicted_by_title` (that session's title) — meaning a STRICTLY NEWER session's own summary reports that item done. Collect every element that has a non-empty `contradicted_by` into a list of flagged items (`text`, `contradicted_by`) for Step 4. Elements without `contradicted_by` are not flagged — ignore them (do not surface, do not count as done).

**Present the brief immediately** (same turn — saves one parent round):

> **Here's what I found from your Obsidian vault for `$PROJECT`:**

Then output the `CONTEXT_BRIEF` section. For the session history table, paraphrase each session's Title column into a concise one-line summary (under ~80 characters) that captures the key accomplishment. Keep all other columns (date, duration, branch) verbatim.

Snapshots appear in the brief as nested indented rows beneath their parent session (rows starting with `↳ HH:MM:SS`). Render them verbatim — do not paraphrase snapshot titles (they're already one-line summaries). Display the `snapshot:` lines from LOAD_MANIFEST as bullet points under the most-recent session in the "Loaded into this conversation" output.

If unsummarized notes were upgraded in Step 2, also mention:

> _Upgraded N session note(s) with AI summaries._

### Step 3b — Recurring Themes (read-only)

Surface the project's top recurring themes (ranked by stored activation, kept fresh by `/consolidate` and `/emerge`). This is a fast, read-only DB read — `$PROJECT` is the value already derived in Step 1.

Request for `recurring-themes` (substitute the values as data):

```json
{
  "project": "<canonical project>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'recurring-themes' < "$REQUEST_PATH"
```

If the output is non-empty, append it verbatim to the brief (between the context brief and the open-items footer). If it is empty, print nothing — there are no themes yet.

**Graceful degradation:** the helper swallows its own exceptions (`ImportError`, empty/missing DB, no themes) and returns `""`, so a missing index or an un-consolidated vault simply prints nothing and `/recall` continues normally. Do not treat an empty result as an error.

### Step 4 — Show read-only context brief footer

For each flagged item collected in Step 3 (those carrying a non-empty `contradicted_by`), render one line, in the order returned:

> ⚠ "<item's text field, verbatim>" looks done per session <contradicted_by> — run `/check-items` to confirm

If `contradicted_by_title` is present and non-empty, you may append it in parentheses after the date for extra context. Do not paraphrase the item text.

Then append to the brief:

> _N open items in this project — run `/check-items` to triage._

Where N is the count of `- [ ]` items found while scanning sessions in Step 3 (the `open_items_total` value already computed by the Python block in Step 3; if not present, count by re-scanning the same notes) MINUS the number of flagged items already rendered above, so a flagged item is never double-counted in the plain footer.

This step remains strictly read-only: never check anything off, and never prompt the user to action an individual item beyond the single flagged-line nudge above. Do NOT independently compute candidate matches or cite session evidence of your own — the flagged lines are rendered only from the `contradicted_by` field Python already computed in Step 3.

If `N == 0` and there are no flagged items either, omit the footer/warning block entirely. If there are flagged items but `N == 0`, still render the flagged lines (omit only the plain `_N open items...` line).

Mark task #3 (the renamed final task) as `completed` and end.

## Edge Cases

- **No sessions found:** Tell the user no session history was found for this project. Suggest they start a session and it will be logged automatically.
- **No insights found:** Omit the "Curated Insights" section. Mention: "No curated insights yet for this project."
- **Very large vault (50+ sessions):** Only grep, never glob the entire folder. Limit reads to the most recent 5 sessions + all insights.
- **Config exists but vault path is invalid:** Warn the user and suggest running `/obsidian-setup` again.
- **Open items exist:** Do not attempt to check them off. Append the footer nudge pointing to `/check-items` instead.

A native session summary is stale when `capture_revision` differs from
`summary_revision`, or no `summary_revision` exists. Label it stale explicitly.
Use unchanged raw capture facts as evidence; do not present its old summary as fresh.
Failed or cancelled AI leaves the operation pending and preserves the note.
