# `/lessons` skill and lesson wiring (#474, #475) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user act on recurring retro lessons from one skill (`/lessons`: file, raise, skip, snooze, batch-confirm matches), and keep the lessons current and visible from skills they already run (`/retro`, `/standup`, `/emerge`).

**Architecture:** The CLI keeps every state, note and GitHub-filing write. `retro_rollup.py` gains: a richer `file_issue` accept entry (title, body, milestone, labels), a `snooze` subcommand, a read-only `show` subcommand (member item text for drafting), member dates in `report --json`, and `lessons_enabled` on the `/retro` update path. The new `skills/lessons/SKILL.md` is a prompt that reads `report --json` and `show`, asks the user, writes one accept file and runs `backfill`. `/retro`, `/standup` and `/emerge` read the same CLI.

**Tech Stack:** Python 3.9+ stdlib, `gh` (stdin closed, timeout), pytest. Skills are Markdown prompts.

**Spec:** `docs/plans/471-retro-lessons-design.md` — "Filing skill (`/lessons`)", "Keeping it current", "Other skills using the data", "Trace write-back", "Setup, configuration and documentation". User ruling for this run (2026-11-01): a `/lessons` issue draft may quote member item text once the user confirms the draft; automatic backfill filing stays text-free.

## Global Constraints

- Python stdlib only. Every `gh` and `claude -p` call has stdin closed and a timeout. Tests mock `retro_rollup._run_gh` and the Haiku runner.
- No test or measurement touches the live vault, `~/.claude/obsidian-brain/` or GitHub (#474 C.4, #475 C.3). Tests use `tmp_path` and a tmp HOME.
- GitHub is changed only by the user's explicit choice (#474 C.2): issue creation only through `backfill --apply --accept FILE` after a confirm step; `gh issue edit` only inside the raise prompt after the user picks a change. `/lessons` never runs `gh issue create`.
- Filing stays two-phase (pending record saved before `gh issue create`, reconcile on every `--apply`). The `<!-- retro-lesson:ID -->` marker stays the first line of every filed body, custom or not.
- Item text leaves the machine only in a `/lessons` draft the user confirmed. Any body text from an accept file is passed through `obsidian_utils.scrub_secrets` before `gh`.
- Atomic writes (`write_vault_note` / existing note writer, `save_state`); files 0o600, dirs 0o700; the accept file is created with `tempfile.mkstemp` under `~/.claude/obsidian-brain/`.
- Docs in the same PR: README (Skills list, Retro lessons section), `docs/architecture/architecture.json` (+ re-rendered HTML, smoke test `[main]` and `[sparse]`), CHANGELOG `[Unreleased]`, and the three changed SKILL.md files plus the new one.

## Data contract changes

- **Accept entry** `{"lesson_id", "file_issue": true, "repo"?, "title"?, "body"?, "milestone"?, "labels"?}`. `title`: str, 1-256 chars, no newline. `body`: str ≤ 20,000 chars. `milestone`: str 1-100 chars. `labels`: list of 0-10 str, each 1-50 chars, no comma or newline. Any other type or size → `UsageError` (exit 2), nothing filed. Absent fields keep today's behaviour (title = lesson name, text-free body, no milestone, no labels).
- **Filed body with `body`:** marker line, blank line, `scrub_secrets(body)` with any `<!-- retro-lesson:` line removed, blank line, `---`, then today's text-free facts block (`_issue_body` minus its marker line). Without `body`: `_issue_body` unchanged.
- **`gh issue create` args:** add `--milestone M` and one `--label L` per label when given. `planned_filings` (dry run) gains `milestone` and `labels`; it shows `title` as today.
- **`snooze <lesson_id> <YYYY-MM-DD|none>`:** under the lessons lock, sets or clears `snoozed_until` in that lesson's note frontmatter through the atomic note writer. Unknown or retired id → exit 2 (a retired id's message names its `merged_into` successor). Bad date → exit 2. No note file for the lesson → exit 1. Prints `{"lesson_id", "snoozed_until"}`.
- **`show <lesson_id> [--json]`:** read-only, no lock needed beyond a state read. Prints the name, bucket, tracking fields and each member `{note, item, date, project, text, trace}` (text read live from the retro note, then `scrub_secrets`). Unknown id → exit 2; a member whose note is unreadable is listed with `text: null` and the run exits 1.
- **`report --json`:** each `members[]` entry gains `date` (`YYYY-MM-DD`, the retro note's date).
- **`update --note X`** with config `lessons_enabled` `is False`: prints `lessons: disabled (lessons_enabled is false)`, exits 0, no Haiku call, no state or note write. `update` without `--note`, `rebuild`, `match`, `refresh-tracking`, `report`, `backfill`, `snooze`, `show` ignore the flag.

## Failure modes

- **Fail-open:** a malformed accept entry must refuse the whole file before any `gh` call (`test_bad_file_issue_fields_refuse_the_whole_accept_file`). A `gh issue create` failure with milestone or labels follows the existing pending/reconcile path, never a second filing (`test_failed_create_with_milestone_keeps_pending_record`). `show` with an unreadable note never prints another item's text in its place (`test_show_unreadable_note_lists_null_text_and_exits_1`).
- **Exit codes:** `snooze` 0 / 1 (no note) / 2 (unknown, retired, bad date); `show` 0 / 1 (unreadable note) / 2 (unknown); `update --note` disabled 0. Test each: `test_snooze_exit_codes`, `test_show_exit_codes`, `test_update_note_honours_lessons_enabled`.
- **Date boundaries:** a snooze date equal to today counts as snoozed for the skills (`snoozed_until >= today`); `/emerge`'s window is inclusive of its start date. `snooze` accepts quoted or unquoted dates on read (existing `read_snooze`). Test: `test_snooze_written_value_reads_back`.
- **Doc counts:** README and SKILL.md give no counts from live data. Any number in CHANGELOG comes from a command run at the final head.
- **Broken Markdown:** the snooze write inserts or replaces one frontmatter line and keeps the rest of the note byte-identical (`test_snooze_keeps_the_rest_of_the_note`).
- **Unrelated changes:** none; filing logic is unchanged when the new fields are absent (`test_file_issue_without_new_fields_is_unchanged`).
- **Inputs:** the accept file (user-confirmed JSON from `/lessons`, or hand-written), the lessons state, the lesson notes (user-edited `snoozed_until`), the retro notes (item text for `show`), the config (`lessons_enabled`). Body text is untrusted: scrubbed, size-capped, and a forged marker line is removed (`test_custom_body_keeps_one_marker_first`).
- **Bypass routes for C.2 (no GitHub change without explicit choice):** (1) the skill calling `gh issue create` itself — contract test greps `skills/lessons/SKILL.md` for `gh issue create` and `gh api -X POST` and expects none; (2) `backfill --apply` run without the confirm step — the skill text must show the dry run and ask before `--apply`, asserted in the contract test; (3) `gh issue edit` outside the raise prompt — the contract test checks each `gh issue edit` line sits under the raise step.
- **Snooze survives a rebuild and carries over (#474 C.3):** `test_snooze_survives_rebuild`, `test_snooze_carries_over_merged_into`, `test_snooze_carries_over_split_from` (the carry-over code exists at `retro_rollup.py:335-341`; these tests pin it with the new `snooze` writer).
- **Outward writes:** `gh issue create` (existing two-phase state table, unchanged) and `gh issue edit` (skill, one call per explicit choice; on failure the skill shows the error and moves on, nothing is retried).
- **`/retro` never waits (#475 C.1):** the detached start uses `nohup … < /dev/null > log 2>&1 &` and the step has no prompt; a held lock makes the background `update` exit 1 into the log, never block `/retro`.

---

### Task 1: richer `file_issue` accept entry

Delegate: codex — self-contained change to `_read_accept`, `_issue_body`/`_create_issue` and the filing path, with tests in a new file; one pytest `Check:`.

**Files:**
- Modify: `hooks/retro_rollup.py` (`_read_accept` ~795, `_issue_body` ~887, `_create_issue` ~901, the filing call in `backfill` ~1071, `planned_filings`)
- Test: `tests/test_lessons_cli.py` (new; reuse the `env`/`menv`-style fixtures by importing helpers from `tests/test_retro_rollup.py` or copying the minimal ones)

**Interfaces (Produces):** accept entry fields and filed-body shape as in "Data contract changes". `_create_issue(ctx, repo, title, body, milestone=None, labels=())`.

- [ ] Tests first: `test_file_issue_sends_title_body_milestone_labels` (fake `_run_gh` records args: `--title`, `--milestone`, two `--label`; body file starts with the marker, holds the scrubbed custom body, then `---` and the facts block); `test_custom_body_keeps_one_marker_first` (a body containing a marker line yields exactly one marker, first); `test_custom_body_is_scrubbed` (an `sk-ant-…`-shaped token is redacted in the sent body; build the token at runtime, never as a literal); `test_bad_file_issue_fields_refuse_the_whole_accept_file` (parametrize: title with newline, title 257 chars, body 20,001 chars, labels not a list, label with comma, 11 labels, milestone 101 chars → exit 2, no `gh` call); `test_file_issue_without_new_fields_is_unchanged`; `test_failed_create_with_milestone_keeps_pending_record`; `test_dry_run_planned_filings_show_milestone_and_labels`.
- [ ] Implement; run `python3 -m pytest tests/test_lessons_cli.py tests/test_retro_rollup.py -q -p no:cacheprovider`.
- [ ] Commit `feat(obsidian-brain): file_issue accept entries take title, body, milestone and labels (#474)`.

### Task 2: `snooze`, `show`, member dates, `lessons_enabled`

Delegate: codex — CLI subcommands and a pure frontmatter helper with their tests; same two files as Task 1 plus `hooks/lessons_notes.py`; one pytest `Check:`. Runs after Task 1 lands.

**Files:**
- Modify: `hooks/retro_rollup.py` (parser ~1240, `main` dispatch, `update` path), `hooks/lessons_notes.py` (`report` members, new `set_snooze(text, value) -> str`)
- Test: `tests/test_lessons_cli.py`

**Interfaces (Produces):** `lessons_notes.set_snooze(note_text: str, value: str | None) -> str` (replace or insert the `snoozed_until:` frontmatter line as a JSON string, or drop it for None; raise `ValueError` when the note has no frontmatter). Subcommands and `report` change as in "Data contract changes".

- [ ] Tests first: `test_snooze_sets_and_clears`, `test_snooze_written_value_reads_back` (via `read_snooze` and `report --json`), `test_snooze_keeps_the_rest_of_the_note`, `test_snooze_exit_codes`, `test_snooze_survives_rebuild`, `test_snooze_carries_over_merged_into`, `test_snooze_carries_over_split_from`, `test_show_prints_member_text_and_traces`, `test_show_unreadable_note_lists_null_text_and_exits_1`, `test_show_exit_codes`, `test_report_members_carry_dates`, `test_update_note_honours_lessons_enabled` (config false → exit 0, fake Haiku never called, state file bytes unchanged; config true or absent → runs), `test_update_without_note_ignores_lessons_enabled`.
- [ ] Implement; run `python3 -m pytest tests/test_lessons_cli.py tests/test_retro_rollup.py tests/test_lessons_notes.py -q -p no:cacheprovider`.
- [ ] Commit `feat(obsidian-brain): snooze and show subcommands, member dates, lessons_enabled on the retro update (#474, #475)`.

### Task 3: `/lessons` skill

Delegate: claude — a prompt file written through `/skill-authoring`, which reads conventions outside the worktree and needs judgment on the prompt flow; plus its contract test.

**Files:**
- Create: `skills/lessons/SKILL.md`
- Test: `tests/test_lessons_skill_contract.py`

Flow (the skill text must carry each step):
1. Resolve the hooks dir with the same `_ob_hooks` snippet the other skills use; read config. `lessons_enabled` false → say so and stop.
2. Run `retro_rollup.py refresh-tracking` (stdin closed). Exit 1 → show the error lines, say tracking may be stale, carry on read-only.
3. `report --json`. Drop lessons with `snoozed_until >= today` and dormant ones. Walk `raise`, then `file`, then `didnt_hold`, then `rule_not_followed`, top first, up to N lessons (argument, default 10).
4. Per lesson one AskUserQuestion: **file** (bucket `file` only), **raise** (buckets with an issue), **skip**, **snooze**.
   - File: repo from the CLI's resolution order; with several projects ask which repo. Run `show <id> --json`, draft a title and a body (problem in two sentences, the member wordings as quoted bullets, a proposed fix). Show the draft. Ask milestone (read-only `gh api repos/R/milestones?state=open`) and priority label (read-only `gh label list --repo R`). On confirm, add `{"lesson_id","file_issue":true,"repo","title","body","milestone","labels"}` to the pending accept list. Nothing is sent yet.
   - Raise: show the issue's state, milestone and priority (report fields, plus read-only `gh issue view`). Options: keep, change milestone, change priority. Only on a change the user picked, run `gh issue edit R#N --milestone …` or `--add-label`/`--remove-label` (stdin closed, timeout). Add `{"lesson_id","tracking_ref"}` so untraced members get the issue trace.
   - Snooze: ask a date (default today + 30 days), run `retro_rollup.py snooze <id> <date>`.
5. Batch list: every lesson whose tracking is a memory or rule match with untraced members (buckets `rule_not_followed` and `quiet`), one multi-select "lesson → matched ref", all ticked; the user unticks wrong ones. Ticked → `{"lesson_id","tracking_ref"}`.
6. Write the accept list to a `mkstemp` file under `~/.claude/obsidian-brain/` (0o600). Run `backfill --accept FILE` (dry run), show the planned filings and trace counts, ask once "Apply: N issues to file, M lessons to trace?". Yes → `backfill --apply --accept FILE`; show `filed`, `reconciled`, traced and untraced counts and exit status. No → delete the file, nothing changes. Delete the file after apply.

- [ ] Write the contract test first: no `gh issue create` and no `gh api -X POST`/`--method POST` in the skill; every `gh issue edit` appears inside the Raise step; `backfill --apply` appears only after a dry-run `backfill --accept` and a confirm question; every `gh` and `python3` command line closes stdin (`< /dev/null`); `refresh-tracking` runs before `report --json`; the shell blocks pass `bash -n` (reuse `tests/test_skill_shell_blocks.py` helpers if they cover new skills automatically, else add the skill).
- [ ] Invoke `/skill-authoring` and write `skills/lessons/SKILL.md` (frontmatter `name: lessons`, a description with "Use when:" triggers).
- [ ] Run `python3 -m pytest tests/test_lessons_skill_contract.py tests/test_skill_shell_blocks.py tests/test_skill_snippets.py -q -p no:cacheprovider`.
- [ ] Commit `feat(obsidian-brain): /lessons skill to file, raise, snooze and confirm retro lessons (#474)`.

### Task 4: `/retro`, `/standup`, `/emerge` wiring

Delegate: claude — edits three existing skill prompts in place, matching each skill's step style, plus a contract test.

**Files:**
- Modify: `skills/retro/SKILL.md` (new Step 7.6 after Step 7.5, before Step 8), `skills/standup/SKILL.md`, `skills/emerge/SKILL.md`
- Test: `tests/test_lessons_wiring_contract.py`

- [ ] `/retro` Step 7.6: when config `lessons_enabled` is not false and Step 7 wrote the note, start `nohup python3 "<hooks>/retro_rollup.py" update --note "<note path>" < /dev/null >> "$HOME/.claude/obsidian-brain/lessons-update.log" 2>&1 &` and print one line `lesson update queued`. No prompt, no wait, no output check. Skipped silently when disabled.
- [ ] `/standup`: a "Recurring lessons" section from `report --json` (no `gh`): top 5 across `raise`, `file`, `didnt_hold`, `rule_not_followed`, skipping dormant and snoozed, one line each (name, bucket, sessions, tracking ref or "no tracking"). Weekly mode first runs `refresh-tracking`; then, when those buckets hold any lesson and `lessons_enabled` is not false, says "N lessons wait" and asks whether to run `/lessons`. No state file → the section says "no lessons yet" and nothing is offered.
- [ ] `/emerge`: a "Lessons growing in this window" section: lessons with at least one member `date` inside the window and `sessions >= 2`, sorted by members in the window, top 10.
- [ ] Contract test: Step 7.6 sits after 7.5 and before Step 8, holds `nohup`, `< /dev/null`, `&`, `--note`, and no AskUserQuestion; standup offers `/lessons` only in weekly mode and only under a non-empty-buckets condition, and states the count; emerge reads `members[].date`; all three use `report --json`, never the state file directly.
- [ ] Run `python3 -m pytest tests/test_lessons_wiring_contract.py tests/test_skill_shell_blocks.py tests/test_skill_snippets.py tests/test_emerge_v2.py tests/test_standup_deep.py -q -p no:cacheprovider`.
- [ ] Commit `feat(obsidian-brain): keep retro lessons current from /retro and show them in /standup and /emerge (#475)`.

### Task 5: docs and architecture

Delegate: claude — README, architecture page and CHANGELOG across several files, with the render and smoke scripts.

- [ ] README: add `/lessons` to the Skills list; extend the Retro lessons section with `/lessons`, the `/retro` background update, the `/standup` and `/emerge` sections, `snooze` and `show`; Configuration says `lessons_enabled` now works. Remove the vault-config and setup notes that say `lessons_enabled` "does nothing until #475 ships" (`README.md:325`, `skills/vault-config/SKILL.md:79`).
- [ ] `docs/architecture/architecture.json`: the `/lessons` skill component, the `/retro` → update flow, the `/standup` and `/emerge` reads, the `/lessons` → backfill flow; `lastUpdated`, `version`; re-render; smoke test `[main]` and `[sparse]`.
- [ ] CHANGELOG `[Unreleased]`: one entry each for #474 and #475.
- [ ] Spec: mark the three sections as shipped where they differ (filing only for bucket `file`; raise traces untraced members to the issue).
- [ ] Preflight, commit `docs(obsidian-brain): document /lessons and lesson wiring (#474, #475)`.

## Self-review

- Spec coverage: "Filing skill" → Task 3 (+ Task 1 for title/body/milestone/labels, Task 2 for snooze/show); "Keeping it current" → Task 2 (`lessons_enabled`) and Task 4 (`/retro`; `plan-week` rebuild is claude-code-skills#283, not here); "Other skills using the data" → Task 4; docs → Task 5. Cross-repo `kickoff` and github-board consumers stay as their own issues.
- Deviation from the spec, called out: **file** is offered only for bucket `file` (no tracking). A tracked lesson (`didnt_hold`, a closed issue) cannot be filed again through `backfill` today; re-filing a closed issue is a separate decision.
- Types: `_create_issue(..., milestone=None, labels=())` is used only by the filing path; `set_snooze` is used only by `snooze`.
- Size: two code tasks in one module plus three prompt tasks; expected authored diff ~1,100 lines, under the ~1,500 cap.
