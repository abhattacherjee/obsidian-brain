# #455: promote at most 10 auto notes per session by merit; keep the rest provisional

Branch `feature/455-capture-merit-selection`, milestone v3.12. The design and its pass bar are in
issue #455 (decided 2026-10-23, validated by a blind spike). This plan does not change the design.
It fixes the mechanics the issue leaves open; each one is marked **Ruling**.

One PR (user, 2026-10-24), Tasks 1–9:

- Tasks 1–8: the live pipeline. Worker scores, provisional save, selection, promotion on use,
  closed-issue demotion, retrieval filters, regression fixture, docs.
- Task 9: the one-off backlog script. Running `--apply` on the live vault is a separate approval
  after merge.

## Rulings

1. **Tier lives in frontmatter `status`.** New auto notes get `status: provisional`. On promotion,
   insight and error-fix notes get `status: promoted`; decision notes get `status: active`, which
   keeps the decision lifecycle (`active` / `superseded`) that `dashboards/decision-timeline.md:23`
   reads. Retrieval filters `status IS NULL OR status != 'provisional'`, so the 3,362 existing auto
   notes (no status) stay visible until PR B runs. Selection never touches a note whose status is
   anything else (for example a hand-set `superseded`).
2. **Ledger `status` stays `active`/`rejected`.** It is a different field. Score data goes in new
   ledger fields: `score`, `novelty`, `duplicate_of`, `superseded_by`, `about_issue`, `repo`.
3. **Candidate references.** The worker refers to another element of its own answer by its 0-based
   array index, and to an earlier same-session note by the 12-hex ledger id. The `{{earlier}}`
   block gains those ids (`[<id>] <title>`). Code maps an index to the cid after the save. A ref
   that is not a valid index (not self) and not an id from the earlier list becomes `null`.
4. **"Real access" = an `access_log` row with `context_type` in (`search`, `tool-open`), timestamp
   after the note's `created_at`.** `related` and `recall` rows fire on their own and do not count.
   Nothing logs a tool-call open today, so `run_batch` scans each slice for `Read` tool calls whose
   `file_path` is a curated note in the vault and logs `tool-open` rows. This runs in the detached
   worker, so it adds no hook latency. A session's reads of notes it created itself are skipped.
5. **"Hand update" = a `## Update (` heading outside code fences that `capture_save.AUTO_HEADING_RE`
   does not match.** That is the heading `/compress` writes.
6. **Sticky promotion.** An accessed or hand-updated note gets `status: promoted|active` and
   `promoted_by: access|hand-update`, sits outside the cap, and selection never demotes it.
7. **What is recorded on the note:** `capture_score`, `capture_novelty`, `capture_merit`,
   `capture_rank`. They are written on first selection and whenever the note's status changes. A
   rank change alone does not rewrite the file. A 440-note session would otherwise rewrite hundreds
   of files per slice. The live rank is in the hook-log line.
8. **Closed-issue demotion.** The worker returns `about_issue` (an int, or null) when a note is
   chiefly the state of one issue. At the end of each `run_batch`, a pass checks promoted,
   non-sticky notes in that project from the last 60 days that have `about_issue`. It runs
   `gh issue view N --json state,closedAt` in the note's `repo` (the session cwd), with a 10 s
   timeout. Each project is checked at most once per 6 h (state file
   `capture/issue-check-<project>.json`). `closedAt` after the note's `created_at` leads to
   `status: provisional` and `demoted_by: issue-closed`. Selection never re-promotes such a note.
   Any `gh` failure changes nothing.
9. **Duplicate vs superseded.** Both leave the note provisional and out of the pick pool. The ledger
   keeps which one it was, and the log line names it.
10. **Selection inputs come from the ledger** (`kind == "new"`, ledger status `active`, file still
    present), recomputed per session after every slice. That makes it idempotent across slices and
    across `run_batch` runs. The per-session claim keeps it single-writer.
11. **Novelty** = 1 − max cosine of the note's `notes.tfidf_vector` to curated notes in the same
    project from other sessions. It is computed once, after `index_note`, and cached in the ledger.
    A missing vector gives novelty 0.5, as in the spike.
12. **Retrieval.** `query_related_notes` excludes provisional notes in all three layers.
    `search_vault` gains `include_provisional=True`. The default keeps every internal caller
    (`find_match`, `/compress`, `open_item_dedup`, `wiki`) deduping against provisional notes.
    `/vault-ask` Step 3 passes `False`. `vault_scan.py meta` gains `status`, and `/vault-ask`
    Steps 4–5 drop `provisional` rows. The `/recall` file-scan fallback checks frontmatter status.
    `/vault-search` stays explicit and includes them.
13. **The prompt keeps its low bar for saving.** Selection, not the worker, enforces the cap.

## Failure modes (self-check classes, planned tests)

1. **Fail-open paths.**
   - A worker score that is missing, a bool, a float string or out of range falls back to 5.
   - An unknown ref becomes null.
   - An exception during selection logs `select-failed` and leaves every status as it is. A
     provisional note stays provisional; it is never promoted by mistake.
   - A `gh` error demotes nothing.
   - A missing vector gives novelty 0.5 and cosine 0, and selection still runs.
   - Tests: `test_score_fallback[...]`, `test_bad_ref_nulled`, `test_selection_error_changes_nothing`,
     `test_gh_failure_demotes_nothing`.
2. **Exit codes.** PR B's script exits 1 on a missing DB or a failed backup, and 2 on bad
   arguments. A dry run exits 0 and writes nothing. Tests: one per exit path, plus a checksum of the
   notes and DB before and after a dry run.
3. **Date boundaries.**
   - The `closedAt` vs `created_at` comparison uses tz-aware datetimes. One test crosses a UTC date
     boundary.
   - The 60-day window and the 6 h rate limit use epoch seconds.
   - PR B's window `2026-10-18..2026-10-23` is inclusive on both ends, with a test for each edge.
4. **Doc counts.** CHANGELOG and architecture numbers are counted from the diff. A count check runs
   in `test_architecture_counts` where it applies.
5. **Broken Markdown.** CHANGELOG and skill edits are made inside the existing blocks. The repo
   preflight's markdownlint covers them.
6. **Unrelated changes.** No deletions are planned. `git diff --diff-filter=D` must be empty.
7. **Inputs.**
   - The ledger `captures.jsonl` is the truth for session membership.
   - Note frontmatter is the truth for tier.
   - `notes.status` and `notes.tfidf_vector` are derived. They can be stale until `index_note`, so
     selection calls `index_note` before reading a vector, and after each frontmatter write.
   - A deleted or `_rejected` note file is skipped.
   - Tests: `test_stale_index_reindexed_before_novelty`, `test_missing_file_skipped`.

**Bypass routes (selection is a gate on the default retrieval pool):**
- The cheapest way into the pool is a note without a status line. That covers old notes by design,
  and PR B closes it for the backlog window.
- The cheapest way into the top 10 is a reworded duplicate. The worker's `duplicate_of` and the
  cosine ≥ 0.6 backstop both stop it (tests).
- A forged sticky promotion would need an access row. Only `search` and `tool-open` count, and only
  after `created_at`. A `related` row does not count (test).

## Tasks

### Task 1: worker output — `hooks/capture_worker_prompt.md`, `hooks/capture_worker.py`

- Prompt:
  - Add rubric v2 verbatim from the issue, plus the "do not penalise project state" line.
  - Add the field rules:
    - `score` (1–10);
    - `duplicate_of` and `superseded_by` (an index into this array, or an id from the earlier
      list, or null);
    - `about_issue` (an int, or null; set only when the note is chiefly one issue's state).
  - Update the example JSON line. Keep the low-bar rule.
- `earlier_titles` returns `(id, title)` pairs. `EARLIER_HEADER` explains the ids. The byte and
  count caps are unchanged.
- `parse_candidates` keeps its filter and adds `_coerce(item, n, earlier_ids)`:
  - `score`: an int 1–10 that is not a bool, or else 5.
  - Refs:
    - an int `0 <= j < n` with `j != i` becomes `("idx", j)`;
    - a 12-hex string in `earlier_ids` becomes `("id", s)`;
    - anything else becomes None.
  - `about_issue`: a positive int, or a string `"#N"` / `"N"`, becomes an int; anything else
    becomes None.
- `run_batch`:
  - `pending` gains `"cids": [...]`, filled after each save (None for an update or a skip).
  - After the candidate loop, resolve the `idx` refs to cids and `ledger_update` the rows.
  - Then call `capture_select.run_selection` and `capture_select.log_tool_opens(rows, ...)`. Both
    are wrapped, so a failure logs and never blocks the offset advance.
  - After the slice loop, call `capture_select.demote_closed(project, config)`, which is
    rate-limited.
  - The outcome detail gains `;promoted=P;demoted=D`.
- Tests (`tests/test_capture_worker.py`):
  - coercion table;
  - prompt has the rubric and ids;
  - refs resolve across a retry (pending resumes);
  - a selection failure still advances the offset.

### Task 2: frontmatter setter — `hooks/note_writer.py`

- `set_frontmatter_fields(vault, note_path, fields: dict[str, str|int|float|None]) -> bool`:
  - resolve and contain the path;
  - lock, split the frontmatter, then replace or insert each top-level `key: value` (None removes
    the key);
  - keep line endings;
  - `_atomic_rewrite(expect_stat=...)`;
  - returns whether the file changed.
  - It refuses a note with no frontmatter. Values must match `^[A-Za-z0-9._:-]+$`, an allow-list,
    so a value can never inject YAML.
- Tests:
  - replace, insert and remove;
  - CRLF;
  - concurrent change (stat mismatch) refused;
  - a value outside the allow-list refused;
  - a path outside the vault refused;
  - no change returns False and keeps the mtime.

### Task 3: save as provisional — `hooks/capture_save.py`

- `_frontmatter` writes `status: provisional`, replacing the template's `status: active` on
  decisions and inserting the line after `source:` otherwise.
- The ledger row gains `score`, `about_issue` and `repo`. The caller passes the session cwd, taken
  from the transcript rows' `cwd` in `run_batch`.
- The update path is unchanged: an update never counts toward the cap.
- Tests:
  - each type is written provisional;
  - the decision has no `status: active`;
  - the row fields are present;
  - the update path writes no status.

### Task 4: selection — new `hooks/capture_select.py`

- Pure core:
  - `merit(score, novelty, note_type) -> float`
    (`0.7*score/10 + 0.3*novelty + 0.02` for error-fix and decision).
  - `select(items, cap=10, lam=0.5, dup_cos=0.6) -> list[str]`: greedy MMR. Each item is
    `{id, merit, vector}`. Ties break by earlier capture time, then id.
- `run_selection(session_id, project, config, *, now=None) -> dict`:
  1. Load the ledger rows. Classify each note:
     - missing (skip);
     - foreign status (skip);
     - sticky (promote if needed, set `promoted_by`, outside the cap);
     - excluded (`duplicate_of`, `superseded_by`, `demoted_by`);
     - candidate.
  2. Compute novelty where it is missing (cached).
  3. `select` over the candidates. The top 10 are promoted, the rest provisional.
  4. Write only the changes, with `set_frontmatter_fields`, then `index_note`.
  5. Log one line per change: `outcome=promoted|demoted detail=cid=<id>;merit=<m>;rank=<r>;why=<pick|displaced|duplicate|superseded|sticky-access|sticky-hand-update>`.
  6. Return the counts.
- `is_sticky(abs_path, created_at, db)` (Rulings 4 and 5).
- `log_tool_opens(rows, vault, project, session_id, db)` (Ruling 4).
- Tests (`tests/test_capture_select.py`), with a real tmp vault and a real index:
  - **merit not order:** 15 candidates, the best 3 last, all 3 promoted;
  - `duplicate_of` skipped, `superseded_by` demoted;
  - **displacement:** a later slice's stronger note displaces an earlier pick, which is demoted
    with `why=displaced`;
  - accessed (`search` and `tool-open`) and hand-updated notes are never demoted and sit outside
    the cap;
  - a `related` row is not an access;
  - cosine ≥ 0.6 backstop;
  - idempotence (a second run makes no writes);
  - a log line per change;
  - the four parts recorded on the note.

### Task 5: closed-issue demotion — `hooks/capture_select.py`

- `demote_closed(project, config, *, now=None, run=subprocess.run) -> dict` (Ruling 8).
- Tests:
  - a fixture `gh` JSON with `CLOSED` and `closedAt` after `created_at` demotes the note;
  - closed before capture does not;
  - an open issue does not;
  - sticky notes are exempt;
  - a `gh` error, a timeout or bad JSON changes nothing;
  - the rate limit skips a second run inside 6 h;
  - selection does not re-promote a note with `demoted_by: issue-closed`.

### Task 6: retrieval — `hooks/vault_index.py`, `hooks/obsidian_utils.py`, `hooks/vault_scan.py`, skills

- Apply Ruling 12. Update the skill text in `skills/vault-ask/SKILL.md` (Steps 3–5) and
  `skills/recall/SKILL.md` where it describes related insights.
- Tests:
  - the `/recall` brief leaves out a provisional note and shows the same note once it is promoted
    (both promotion paths);
  - `search_vault(include_provisional=False)` leaves it out, and the default includes it;
  - `vault_scan meta` prints `status`;
  - the file-scan fallback leaves it out.

### Task 7: regression fixture — `tests/fixtures/455-eval/`, `tests/test_capture_select_eval.py`

- Commit:
  - `blind.json`, `key.json` and `labels/*.json` from `~/.claude/obsidian-brain/spike-455/`;
  - `notes.json.gz`: for each labelled unit, every note as
    `{name, type, ts, score, novelty, vector}`, from the spike report and the live index's
    `tfidf_vector` (1,547 notes in 9 units, about 2 MB raw);
  - `README.md`: provenance, and how to rebuild.
- The test replays `capture_select.select` per unit, maps the picks to blind ids through
  `key.json`, and computes the keep rate per judge:
  - `opus` = round1 ∪ round2;
  - `astra` = round2.
  - A pick with no label counts as a drop.
- Asserts:
  - v2 ≥ 80% under Opus;
  - v2 ≥ 65% under Astra;
  - v2 ≥ capture-order + 25 points under each judge.
- When `~/.claude/skills/ship/scripts/blind_eval.py` exists, also run its `score` and require
  agreement within 1 point. That check is skipped without the script, which is how CI runs.

### Task 8: docs

- `docs/architecture/architecture.json`:
  - component `capture-select`;
  - update the `mid-session-batch` and `search-and-rank` flows;
  - add a `capture-demote-closed` flow;
  - add the `db-capture` row fields and the `note-schema` status values;
  - re-render, then run the smoke test.
- `CHANGELOG.md` Unreleased entry.

### Task 9: backlog — `hooks/capture_backlog.py`, `scripts/capture_provisional_backfill.py`

- Input: auto notes (`source: auto-capture`, not under `_rejected`, created 2026-10-18 to
  2026-10-23 inclusive) grouped by `source_session`.
- Score them with rubric v2 through Haiku in batches of 20. `--scores FILE` reuses the spike's
  `scores_v2.json` and caches new scores, so a re-run costs no tokens.
- Compute novelty, then run `select` per session.
- **Ever opened → kept promoted:** a `search` or `tool-open` access row, a hand update, or a `Read`
  of the note in any transcript under `~/.claude/projects` (the Task 4 scanner reused).
- Dry run (the default): counts per project and per session (promoted, provisional, kept-opened)
  and the token estimate. It writes nothing.
- `--apply`:
  - back up the DB first with the `access_log_purge` backup pattern, and copy each touched note to
    `capture_dir()/backlog-backup-<UTC>/` (the `capture_split` pattern);
  - write `status` plus `demoted_by: backlog` (the recoverable flag) and the score parts;
  - `index_note` each one.
  - It never deletes a file.
- Exit codes: 0 ok, 1 refused or failed, 2 usage.
- Tests:
  - dry run writes nothing (checksums);
  - apply makes backups and keeps ever-opened notes promoted;
  - window edges;
  - no file is deleted;
  - every exit code.
- Architecture entry and CHANGELOG line.

## Acceptance criteria → tasks

| AC | Tasks |
|---|---|
| Worker returns score / duplicate_of / superseded_by, score fallback 5 tested | 1 |
| Saved provisional; ≤ 10 promoted per session; log line per change; parts recorded | 3, 4 |
| Merit not order; duplicate skipped / superseded demoted; displacement; sticky never demoted | 4 |
| Provisional out of `/recall` related and `/vault-ask`; both promotion paths tested | 6 |
| Closed issue demotes, fixture issue state | 5 |
| Regression fixture keep rates | 7 |
| Backlog script | 9 |
| Re-measure one week after merge | NEEDS-HUMAN, recorded at the gate as a dated follow-up |
