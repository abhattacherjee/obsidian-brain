# Plan: keep provisional auto-capture notes out of Obsidian Sync (#465)

## Goal

Obsidian Sync on the Mac cannot keep the iOS vaults current. On 2026-10-28 `claude-insights/` held 6,946 notes in one flat folder. 4,389 of them are `status: provisional` auto-captures (#455) that search and recall skip, but Sync uploads and every device indexes. About 5,000 insight files changed in a week, and appended updates piled onto single notes (up to 170 in one day).

This change:

1. keeps provisional notes in `claude-insights/_provisional/`, which the user excludes from Sync;
2. moves a note between the two folders whenever its status changes, and keeps every path-keyed store correct;
3. moves the existing provisional notes with a dry-run-first migration;
4. caps appended updates at 3 per note per local day.

The merit floor moved to #466 (it waits for #457). The code map behind this plan is in the session scratchpad (`465-map.md`). Its cites are at `develop` `4dcad71`.

## Rulings (user skipped design questions on 2026-10-28; these are the implementer's calls, stated to the user)

- **R1 Folders.** Provisional notes live in `<insights_folder>/_provisional/`. Every other status (promoted, active, none) lives directly in `<insights_folder>/`. `_rejected/` is unchanged. Placement only ever moves a note between `<insights_folder>/<name>` and `<insights_folder>/_provisional/<name>`. A note anywhere else is never moved.
- **R2 One move function.** `hooks/note_placement.py` owns every move. A move keeps `notes.path`, `access_log.note_path` and `theme_members.note_path` (absolute paths) and every ledger row's `note_path` (vault-relative) in step with the file. No other code renames insight notes.
- **R3 Update cap.** At most `MAX_UPDATES_PER_NOTE_PER_DAY = 3` capture updates per note per local day. Past the cap, the capture is saved as a new provisional note (in `_provisional/`, so it costs Sync nothing) instead of being appended. Count by note basename, so rows written before a move still count. Rejected updates do not count. Two sessions racing past the cap by one is accepted and documented.
- **R4 Pause.** A pause flag file, `<capture_dir>/PAUSED`, stops new capture batches. It is checked in `capture_worker.launch`, `capture_cli.sweep` and at the start of `capture_worker.run_batch`. A flag older than 2 hours counts as not paused and is logged, so a crashed migration cannot disable capture for good.
- **R5 Readers.** Code that lists or resolves insight notes walks `<insights_folder>` recursively and skips `_rejected/`, so provisional notes stay visible where they were visible before. `/vault-search` still finds them; `/vault-ask` and `/recall` still leave them out by status.

## Move protocol (R2)

`relocate(config, old_abs, new_abs, db) -> Path`:

1. Both paths resolve inside `<vault>/<insights_folder>` and are R1 homes. Anything else raises `ValueError`.
2. Take the note lock (`note_writer._acquire_lock`) on both the old and the new path. If either is held, do nothing and return the old path; the next placement retries.
3. If `new_abs` exists and `old_abs` exists: collision. Log it and return the old path (never overwrite).
4. Open the DB, `BEGIN IMMEDIATE`, then `UPDATE notes SET path=new`, `UPDATE access_log SET note_path=new`, `UPDATE theme_members SET note_path=new`, all `WHERE path=old`. `os.rename` the file inside the transaction, then commit. If the rename fails, roll back. Holding the write lock across the rename means a concurrent `_sync` cannot prune or re-insert in between.
5. Append one ledger row per merged ledger row whose `note_path` equals the old vault-relative path, with the same `id` and the new `note_path`. `_merged_rows` keeps the first `ts`, so times are unchanged.
6. Release the locks.

Re-running it is idempotent. If `old_abs` is gone and `new_abs` exists, steps 4–5 still run, minus the rename. So a crash between the rename and the ledger append is repaired by running the migration again.

`vault_index._sync` gets one guard: before `_delete_note` on an indexed path missing from the listing, re-check `os.path.exists`. If the file is there again (moved in after the listing), keep the row.

`place(config, abs_path, status, db) -> Path` computes the R1 home and calls `relocate` when it differs. It returns the path the note now has.

## Tasks

Each task lists its files (at most 5 for a cx brief), the tests to write first, and the check command.

### T1 Pause flag (R4)
- Files: `hooks/capture_state.py`, `hooks/capture_worker.py`, `hooks/capture_cli.py`, `tests/test_capture_state.py`, `tests/test_capture_worker.py`.
- `capture_state.pause(reason)`, `resume()`, `is_paused() -> bool` (flag age under `PAUSE_TTL_SECONDS = 7200`; a stale flag logs once and returns False), and `active_claims() -> list[str]` (claim files still inside their TTL).
- Gate `launch`, `sweep` and `run_batch` (before `claim`) on `is_paused()`. `run_batch` returns `"paused"` and logs it.
- Tests: each entry point returns without spawning or claiming while paused. A stale flag does not block. `resume` removes only `PAUSED`.
- Check: `python3 -m pytest tests/test_capture_state.py tests/test_capture_worker.py tests/test_capture_cli.py -q -p no:cacheprovider`

### T2 Move function and prune guard (R1, R2)
- Files: `hooks/note_placement.py` (new), `hooks/vault_index.py`, `tests/test_note_placement.py` (new), `tests/test_vault_index.py`.
- `PROVISIONAL_DIR`, `home_for`, `relocate` and `place`, as in the move protocol.
- Tests (temp vault and temp DB; never the live DB):
  - **Move bookkeeping:** after a move, `notes`, `access_log` and `theme_members` hold only the new path, with row counts unchanged, and every ledger row (`new` and `updated` kinds) merges to the new `note_path` with its first `ts` kept.
  - **Refusals:** a collision leaves both files and every row untouched. A held lock moves nothing. A path outside the R1 homes raises.
  - **Failure and recovery:** a rename failure (monkeypatched) rolls the DB back. The rerun after a "crash" (file moved, DB untouched) repairs the DB and ledger.
  - **Prune guard:** a `_sync` whose listing predates a move keeps the moved row. Deleting the `os.path.exists` guard turns this red.
- Check: `python3 -m pytest tests/test_note_placement.py tests/test_vault_index.py -q -p no:cacheprovider`

### T3 Place notes at every status change (R1)
- Files: `hooks/capture_select.py`, `hooks/capture_backlog.py`, `hooks/note_writer.py`, `tests/test_capture_select.py`, `tests/test_capture_backlog.py`.
- After each status write, call `note_placement.place` and carry the returned path forward:
  - `capture_select._apply` (after `update_note_status`);
  - `promote_used`;
  - `demote_closed`;
  - `capture_backlog.apply`;
  - the `note_writer` CLI `append-update` branch, when `promote_provisional` flipped the status (import `note_placement` lazily there, to avoid an import cycle).
- `plan_session` and `demote_closed` resolve a stale ledger path by also trying the other R1 home before skipping, so an unmigrated or half-migrated ledger still works.
- Tests:
  - a promoted note ends in `claude-insights/` and a demoted one in `_provisional/`, with index and ledger rows following;
  - a selection rerun with no status change moves nothing;
  - `promote_used` finds a note through its migrated `access_log` row;
  - the CLI promotion moves the note.
- Check: `python3 -m pytest tests/test_capture_select.py tests/test_capture_backlog.py tests/test_note_writer.py -q -p no:cacheprovider`

### T4 New captures, splits and the update cap (R1, R3)
- Files: `hooks/capture_save.py`, `hooks/capture_split.py`, `tests/test_capture_save.py`, `tests/test_capture_split.py`.
- `save_candidate` writes a new note to `<insights_folder>/_provisional/`, with the ledger `note_path` to match. `new_filename` checks both R1 homes for a free name.
- `capture_split` writes each split note to the R1 home of the status it gives that note.
- Update cap: before appending to `match`, count today's ledger rows with `kind == "updated"`, the same basename as `match` and `status != "rejected"`. At 3 or more, save the candidate as a new provisional note, and set `capped_update_of: <match>` on its ledger row.
- Tests:
  - a new capture lands in `_provisional/`;
  - updates 1–3 append, and update 4 becomes a new note with `capped_update_of`;
  - an update logged before a move (old path) still counts;
  - a rejected update does not count;
  - the day boundary resets the count (fake `today` and ledger `ts` either side of local midnight);
  - split notes land by status.
- Check: `python3 -m pytest tests/test_capture_save.py tests/test_capture_split.py -q -p no:cacheprovider`

### T5a Recursive readers in hooks (R5)
- Files: `hooks/obsidian_utils.py`, `hooks/deep_cli.py`, `hooks/open_item_dedup.py`, `tests/test_gather_session_evidence.py`, `tests/test_deep_cli.py`.
- Changes:
  - `gather_session_evidence`: recursive glob, skipping `_rejected`;
  - `deep_cli._resolve_path`: a basename also resolves under `<insights_folder>/_provisional/`;
  - `open_item_dedup` (`:1304`, `:1743`): resolve through both R1 homes.
- Tests: a note in `_provisional/` is found by each, and a note in `_rejected/` is not. Positive control: the same note at the top level is found.
- Check: the named test files.

### T5b Recursive readers in vault-doctor checks (R5)
- Files (two cx runs, at most 5 files each):
  - run 1: `scripts/vault_doctor_checks/encoding_corruption.py`, `project_name_normalization.py`, `missing_frontmatter_fence.py`, `project_name_canonicalization.py`, and their tests;
  - run 2: `crossed_source_session.py`, `source_sessions.py`, `session_coverage.py`, `audit_historic_repairs.py`, and their tests.
- Each check walks insights recursively and skips `_rejected`. Backup paths built from `parent.name` use the path relative to the vault instead, so a `_provisional` note's backup does not collide with a top-level note of the same name. `audit_historic_repairs` keeps its `folder_hint` for `_provisional` notes.
- Tests: per check, a note in `_provisional/` is scanned (and repaired where the check repairs), with the top-level case as a control.
- Check: `python3 -m pytest tests/ -q -p no:cacheprovider -k "vault_doctor or doctor"`

### T6 Migration script (C.2, C.6)
- Files: `scripts/capture_provisional_relocate.py` (new), `tests/test_capture_provisional_relocate.py` (new).
- Default is a dry run. It prints JSON counts and writes nothing:
  - provisional notes directly in `<insights_folder>`, by frontmatter status;
  - non-provisional notes in `_provisional/`;
  - `access_log` and `theme_members` rows on those paths;
  - ledger rows on those paths;
  - name collisions.
- `--vault` and `--db` point at a copy. Both default to the live config and DB.
- `--apply`, in order:
  1. back up the DB (`vault_index._backup_db`, as `scripts/capture_provisional_backfill.py` does);
  2. `capture_state.pause("migration #465")`;
  3. wait up to 600 s for `active_claims()` to empty (exit 3 if they don't, and resume);
  4. `ensure_index`;
  5. `place` every note;
  6. verify, then resume (always, in `finally`).
- Verification after apply: zero provisional notes directly in `<insights_folder>`, and zero `access_log`/`theme_members` rows whose path is one of the moved old paths. Exit 1 if either is not zero.
- Exit codes: 0 ok, 1 verification failed, 2 usage error, 3 capture still active.
- Tests: on a temp vault, DB and ledger:
  - the dry run writes nothing (mtime and row-count snapshot);
  - apply moves every note and verifies;
  - a rerun is a no-op;
  - a collision is reported and skipped;
  - the pause wait times out to exit 3, with the flag removed afterwards;
  - forced verification failure gives exit 1.
- Live check, right after the first pass: run the dry run against a copy of the live DB and vault (`cp` to the scratchpad) and record the counts. They should be close to 4,407 notes and 441 `access_log` rows.
- Check: `python3 -m pytest tests/test_capture_provisional_relocate.py -q -p no:cacheprovider`

### T7 Docs
- Files: `CHANGELOG.md` (`[Unreleased]`), `docs/architecture/architecture.json` and `.html`, `README.md` (the Obsidian Sync note).
- Content:
  - CHANGELOG: the move, the pause flag, the update cap and the migration script.
  - architecture.json: the new `note_placement` component, the script, and the flow "status change → place → relocate". Keep `lastUpdated` and `version` in sync. Re-render and smoke-test, main and sparse.
  - README: one line telling users to add `claude-insights/_provisional/` to Obsidian Sync's excluded folders.
- Not run by this PR: the live migration. The user runs `--apply` after merge (step for the merge gate).

## Failure modes

- **Fail-open paths:**
  - A move that updates the file but not the DB or ledger silently drops the note from selection forever (`plan_session` `is_file` skip) and orphans its access history. Tests: T2 bookkeeping and crash-repair; T3 stale-ledger fallback.
  - A `_sync` racing a move prunes the moved row. Test: the T2 prune-guard mutation.
  - **Bypass routes for the update cap:**
    - counting by full path, so a pre-move row does not count (T4 old-path test);
    - counting rejected rows, which caps too early (T4);
    - a local-midnight boundary off by the UTC offset (T4 day-boundary test);
    - two sessions both passing at N−1 (accepted, documented in R3).
  - **Bypass for the pause:** `run_batch` started by a worker spawned before the flag; it checks at start, and the migration waits on `active_claims()` (T1, T6).
- **Exit codes:** migration 0/1/2/3, each triggered in T6. `run_batch` returns `"paused"` (T1).
- **Date boundaries:** the update cap's local day (T4). The pause TTL at exactly 7,200 s: a flag at 7,199 s pauses, and one at 7,201 s does not (T1).
- **Doc counts:** CHANGELOG and README numbers come from commands run at head. The PR body says that the live migration counts are from the copy dry run.
- **Broken Markdown:** CHANGELOG entries go inside `[Unreleased]`; re-read the surrounding lines.
- **Unrelated changes:** none planned. Any test that changes because a note now lives in `_provisional/` is listed in the PR body with the reason.
- **Inputs:**
  - Frontmatter `status` is the source of truth for placement. The DB `status` column is a cache: T6 counts by frontmatter, and a test covers a note whose DB status is stale.
  - The ledger is append-only and merged by `id` (a test covers a missing ledger file).
  - The config's `insights_folder` (a test covers a non-default folder name).
  - The DB at `~/.claude/obsidian-brain-vault.db`: tests never touch it, and they pass `db` explicitly.

## Out of scope

- The merit floor (#466).
- Excluding the folder in Obsidian Sync: a user setting, which the README note covers.
- Running the live migration: after merge, at the user's go.
