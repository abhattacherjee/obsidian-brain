---
name: check-items
description: Triage open `- [ ]` items across your Obsidian vault with evidence-grounded AI classification. Auto-closes items shipped by merged PRs, surfaces items needing external action (e.g. `gh issue close`), hides stale items by default. Replaces the old token-overlap heuristic with a two-pass AI pipeline backed by a persistence cache. Use when: (1) sweeping a project for done work, (2) auditing what's still actionable, (3) recovering from /recall deferral fatigue.
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

Before preparing edits or requesting a summary of an existing note, call
`note-read` and retain its exact `expected_revision`. Apply the proposed note
with `note-apply` and that revision. A conflict leaves the current note intact;
show the pending result and do not count the note as saved. New curated notes
use `note-create`; they never overwrite a collision. Native memory discovery
is unsupported for Codex because it has no equivalent native memory-file API; shared vault retrieval
and wiki filing continue without borrowing another host's memory.

Read `references/host-claude.md` or `references/host-codex.md` when present.
All note writes described below use `note-create` or revision-bound `note-apply`,
including bidirectional related links. Content is a JSON string, never shell code.
Every later save or edit follows this revision-bound publication rule.

# /check-items

## Invocation

```
/check-items                     # current project, 14d window (default)
/check-items <project>           # named project, 14d window
/check-items all                 # every project with open items in window
/check-items 30d                 # current project, widen window
/check-items --show-all          # include LOW-confidence + STALE
/check-items --dry-run           # run pipeline, write report, skip edit-confirm loop
/check-items --no-cache          # force re-classification of every group
```

Arguments are order-independent and combinable: `/check-items all 30d --show-all`.

## Step 1 — Parse arguments and resolve scope

Run this bash block. It parses `$ARGUMENTS` per the invocation contract above (positional project / `all` / `Nd`, plus the three flags). Output goes to a temp directory under `<prepared operation_dir>/`; the printed path is captured in `$scope_path` and passed to every subsequent step as `"$scope_path"`.

Each step below is a bash block; the embedded Python reads its inputs from `$1`, `$2`, … via `sys.argv`. Pass `"$scope_path"` captured here as the first arg, and any previous step's output path as subsequent args.

Request for `scope` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "argv": [
    "<user scope flags>"
  ]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'scope' < "$REQUEST_PATH"
```

Save the printed `scope.json` path in `$scope_path`; pass it to every subsequent step as the first arg.

Note: `window_days` in scope controls how many sessions' files to pass as `basenames` in Step 5. The `collect_open_items` helper itself scans by `max_sessions` count (not calendar days); to apply a window filter, limit the basenames list to files dated within the window before passing to `deep_analysis_pipeline`.

**Unrecognised arguments are fatal (#318).** `parse_scope` records any token that is not a flag, `all`, an `Nd` window, or a known project on `scope.unknown_tokens`, and the block above exits 2 rather than proceeding. A silently-dropped project name does not degrade the run — it makes the run answer about the *current* project while appearing to answer about the one that was named. Known projects are the union of workspace-root directories and every `project` value in the vault index, so a notes-only project with no git repo is recognised.

If the block exits 2, stop and show the user the stderr verbatim; do not fall through to Step 2.

## Step 2 — Collect open items (Stage 1)

Request for `collect` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "scope_path": "<registered scope.json>"
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'collect' < "$REQUEST_PATH"
```

## Step 3 — Coarse-group + cache partition (Stage 2a + cache load)

Convention: each step is a `bash` block. Steps 1-2 use `python3 -c "..."` with positional argv tokens (inputs passed as `sys.argv` args, e.g. `python3 -c "..." "$scope_path"`). Steps 3-10 use `python3 << 'PYEOF' ... PYEOF` heredoc with inputs passed via environment variables (e.g. `SCOPE_PATH="$scope_path" python3 << 'PYEOF' ... PYEOF`); the Python reads them via `os.environ["VAR_NAME"]` — never via `sys.argv`. Both patterns produce a runnable shell block — never paste raw `python3` blocks that rely on `sys.argv` without an argv-passing wrapper, and never use env-var heredoc style for Steps 1-2 which expect positional args.

Request for `stage-01` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "inputs": {
  "RAW_PATH": "<registered RAW_PATH>",
  "SCOPE_PATH": "<registered SCOPE_PATH>"
}
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'stage-01' < "$REQUEST_PATH"
```

## Step 4 — Semantic merge on needs-reclassification set (Stage 2b)

Request for `stage-02` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "inputs": {
  "PART_PATH": "<registered PART_PATH>",
  "SCOPE_PATH": "<registered SCOPE_PATH>"
}
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'stage-02' < "$REQUEST_PATH"
```


## Step 5 — Gather evidence (Stage 3)

Request for `stage-03` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "inputs": {
  "MERGED_PATH": "<registered MERGED_PATH>",
  "SCOPE_PATH": "<registered SCOPE_PATH>"
}
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'stage-03' < "$REQUEST_PATH"
```

`deep_analysis_pipeline` gathers per-project evidence for every project in `merged_by_proj`. For a project with a resolved local git repo, evidence is git-derived: commits, merged PRs, closed issues, releases, FTS-indexed vault mentions, and tags/changed paths folded in from recent commits. For a project with no local repo, `gather_note_completion_evidence()` (#318) is the only evidence source: it flags an item when a strictly newer session's own `## Summary` reports it done (mirroring `/recall`'s `contradicted_by` signal), gated on the same completion-phrase guard the heuristic classifier uses so a bare co-mention can never fabricate DONE evidence. Both write into the same per-project bucket (`note_completions` alongside the git-derived keys) — Step 6's classifier and Step 7's `assign_tier` treat a project whose ONLY real evidence is `note_completions` as capped at tier MED regardless of citation wording, via `note_evidence_only_for()` (`hooks/check_items_cli.py`), never HIGH.

## Step 6 — Classify (Stage 4)

Request for `stage-04` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "inputs": {
  "EVIDENCE_PATH": "<registered EVIDENCE_PATH>",
  "MERGED_PATH": "<registered MERGED_PATH>",
  "SCOPE_PATH": "<registered SCOPE_PATH>"
}
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'stage-04' < "$REQUEST_PATH"
```

## Step 7 — Apply tier rules + present review (Stage 5)

Request for `stage-05` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "inputs": {
  "CLASSIFICATIONS_PATH": "<registered CLASSIFICATIONS_PATH>",
  "SCOPE_PATH": "<registered SCOPE_PATH>"
}
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'stage-05' < "$REQUEST_PATH"
```

If `scope.dry_run` is true OR the user types `none` at the confirm prompt: skip Step 8 (Edit + cascade) and go straight to Step 9 (dashboard). The dashboard is ALWAYS written.

## Step 8 — Apply confirmed checkoffs (Stage 6) + cascade (Stage 7)

Send the group IDs explicitly kept selected by the user. The fixed operation
reads the protected preview and merged members, checks every exact source SHA
captured before AI, verifies each grouped checkbox text, and applies primary
and sibling checkoffs through the shared transaction boundary. A stale source
remains pending; never re-read it after AI to approve a replacement revision.
Only selected groups are stamped `applied=True`. Publication uses the shared transaction boundary.

```json
{
  "operation_id": "<prepared id>",
  "reviewed_group_ids": ["<user-selected group ID>"]
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'apply-reviewed' < "$REQUEST_PATH"
```

The operation updates the same buckets and cascade summary artifacts used by
Step 9. A pending or failed result is reported before continuing. Already
published files in a partial CAS result remain published; do not call the run
fully successful. The legacy `stage-06` skip-tracking procedure remains an
internal compatibility operation; new native review uses `apply-reviewed`.

**Reading `cascade_total` and `cascade_skipped_total`:** Step 9 reads both mechanically from `cascade_summary.json` (written above, alongside `buckets_path`) — nothing to carry forward by hand. Still surface both to the terminal Output format's `Cascaded:` and `Skipped:` lines from this block's own printed `[cascade]`/`cascade_skipped_total=` output. If the python block above exited non-zero, its `WRITE FAILED` line is the reason — report it to the user verbatim rather than proceeding as if the cascade fully succeeded.

## Step 9 — Write dashboard report (Stage 8) — ALWAYS

(Implemented in Task 22.) Every argument below is derived mechanically from files already on disk by this step's own block — #318 I1: before this fix, `classifications`/`merges`/`evidence_gaps` were prose instructions with no executable block behind them, the same "written, tested, and unreachable unless a model complies" shape as F14. `write_check_items_dashboard()`'s path convention: `<vault>/<check_items_folder>/check-items-<scope>-<YYYY-MM-DD>.md` (folder configurable, default `claude-check-items`).

Request for `stage-07` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "inputs": {
  "BUCKETS_PATH": "<registered BUCKETS_PATH>",
  "CLASSIFICATIONS_PATH": "<registered CLASSIFICATIONS_PATH>",
  "GAPS_PATH": "<registered GAPS_PATH>",
  "MERGED_PATH": "<registered MERGED_PATH>",
  "PART_PATH": "<registered PART_PATH>",
  "RAW_PATH": "<registered RAW_PATH>",
  "SCOPE_PATH": "<registered SCOPE_PATH>"
}
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'stage-07' < "$REQUEST_PATH"
```

`report_path` is the dashboard note's full path — surface it to the user in the terminal Output format's `Report:` line.

N5: this block has no top-level `try` around its core inputs (`raw_path`/`part_path`/`merged_path`/`classifications_path`/`buckets_path`) — the right fail-loud default for a step marked ALWAYS, since a dashboard built on a missing or corrupt upstream artefact would be worse than none at all. If this block exits non-zero, `$report_path` is unset and no dashboard was written this run — stop and show the user the traceback verbatim rather than reporting the run as complete.

## Step 10 — Persist cache updates

Request for `stage-08` (substitute the values as data):

```json
{
  "operation_id": "<prepared id>",
  "inputs": {
  "CLASSIFICATIONS_PATH": "<registered CLASSIFICATIONS_PATH>",
  "PARTITION_PATH": "<registered PARTITION_PATH>",
  "SCOPE_PATH": "<registered SCOPE_PATH>"
}
}
```

```bash
python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py" --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" run --skill-path "$OB_SKILL_PATH" --operation 'stage-08' < "$REQUEST_PATH"
```

**#323 F3 — if the block above printed `cache NOT updated: <reason>` and exited non-zero:** STOP here — do not summarise this run as clean, and do not report `Cached: N reused, M fresh` (from Step 7) as if it were the final state. Report the `cache NOT updated: ...` line to the user verbatim. This run's classifications are lost (fail-safe: every group re-derives from scratch next run, at the cost of re-classification tokens, never incorrect output) — but any checkoffs already applied in Step 8 are on disk and unaffected.

## Output format

```
✓ /check-items obsidian-brain (14d)
  Raw: 225  Groups: 40  Merged: 24
  Mode: semantic+classifier  Cached: 8 reused, 16 fresh
  Result: 3 DONE (auto-checked), 2 NEEDS-ACTION (commands below), 4 REVIEW (needs a look), 19 ACTIVE (silent)
  Report: ~/Obsidian/claude-check-items/check-items-obsidian-brain-2026-05-11.md
  Cascaded: 2 sibling notes
  Skipped: 3 sibling(s) refused or lost — see report for basename:line detail
  Cache: updated
```

`Skipped:` (#320 F1) is populated from `cascade_skipped_total` (Step 8) — the count of cascade candidates that were refused (drift, unverifiable stored text, checkbox already gone) or lost (a failed write), summed across every `Skipped ...`/`WRITE FAILED` line in the cascade summary. Omit the line entirely when `cascade_skipped_total` is 0 — don't print `Skipped: 0`.

`Cache:` (#323 F3) reflects Step 10's outcome and is always printed, never omitted: `updated` on success, or `NOT updated — <reason>; verdicts will be re-derived next run` when Step 10 printed `cache NOT updated: <reason>` and exited non-zero.

## Notes

- All sub-agent prompts live in `hooks/check_items_cli.py` (the semantic-merge and classifier prompt constants). Do NOT inline those prompts in this SKILL.md.
- The cache file is at `<native state_path>/check-items-classifications.json` (0o600). Safe to delete for a full reset.
- `/recall` no longer surfaces checkoff candidates. If you used to invoke `/recall → "skip"`, just run `/check-items` directly.

Native classification publishes only when every requested group has one valid
result. Cancellation, unavailable AI, or exhausted chunks leave the operation
pending. They never become a heuristic success, dashboard, or cache update.
Cache replay begins after full evidence is gathered; Step 10 verifies the same
full input, evidence, prompt, policy, backend, and actual model before stamping.
