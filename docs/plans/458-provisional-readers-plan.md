# #458: keep provisional notes out of themes, /emerge and dashboards; at most 1 retro in /recall's related list

Branch `feature/458-provisional-readers`, milestone v3.9.2. The problem, the fixes and the spike
bar are in issue #458. This plan fixes the mechanics; each choice is marked **Ruling**.

## Rulings

1. **No auto-note cap.** Spike round 1 showed it makes `/recall` worse (judges kept 70–80% of the
   auto notes it removed, 30% of what it added). See the #458 comments.
2. **"Provisional" uses the existing `NOT_PROVISIONAL_SQL` / `normalize_status`.** No new spelling
   of the check (#455 found 4). Themes and /emerge add `NOT_PROVISIONAL_SQL.format(p="n.")`.
3. **The /recall retro rule is a selection pass over the layered list.** `query_related_notes`
   builds the layered candidate list as today (Layer 1 backlinks by date DESC, Layer 2 tags,
   Layer 3 FTS), and a new pure function `select_related(rows, limit, max_retros=1)` picks the
   result: walk the rows in layer order, take each row, but skip a `claude-retro` row once
   `max_retros` retros are taken; stop at `limit`. The kept retro is the first in layer order,
   which is the newest backlinked retro when Layer 1 has one. Nothing else is reordered.
   - Layers 2 and 3 run while fewer than `limit` non-skipped rows are collected (count with the
     same rule, so skipped retros do not stop a layer early).
   - Layer 3 fetches `limit * 2` rows instead of `limit - len(results)`, so skipped retros do not
     leave slots empty. Its `ORDER BY f.rank` is unchanged.
   - Access is logged only for the selected rows, as today.
   - `note_types` still decides whether retros are candidates at all; the rule only applies when
     `claude-retro` is among them.
4. **`max_retros` defaults to 1** (`RELATED_MAX_RETROS = 1`), a keyword argument. No config key.
   The spike tested 1.
5. **The /recall file-scan fallback** (`obsidian_utils.build_context_brief`, index unavailable)
   applies the same rule to its date-ordered list: at most 1 retro among its 20.
6. **Themes never take a provisional note.** `get_unassigned_notes`, `get_all_vectorized_notes`
   and `assign_to_theme` skip provisional rows. A note promoted later has no theme row, so the
   next incremental `/consolidate` picks it up. Nothing else changes for promoted notes.
7. **Cleanup, run by every `/consolidate`.** New `themes.prune_provisional_members(conn, now)`
   removes every provisional member: it reverse-folds the note out of the centroid
   (`_reverse_fold_centroid`, as `_delete_note` does), decrements `note_count`, and deletes a theme
   left with no members. It runs inside `run_consolidate`'s write transaction, before the new
   themes are created, in both modes, and prints `PRUNED_PROVISIONAL=<n>`. Idempotent: a second
   run prunes 0. Live: 579 provisional members today, in 272 themes, largest 132.
8. **/emerge** (`get_unassigned_notes_in_window`) skips provisional notes. Without this, its 30
   "new candidates" would be the 30 newest provisional notes. Found while tracing the theme
   readers; it is the same class as fix 2 in the issue.
9. **Dashboards.** The Dataview filter is `(!status OR !startswith(lower(string(status)), "provisional"))`.
   It goes on every query that lists or groups `claude-insights` notes and is not already limited
   to a non-provisional status. Queries that list rows get a `LIMIT` (50 for full lists, 10 or 20
   where one exists). `GROUP BY` queries get the filter only. Files:
   - `weekly-review.md`: the filter and `LIMIT 100`. Sessions are not provisional, so the filter
     applies to both folders.
   - `decision-timeline.md`: "All Decisions" gets the filter and `LIMIT 50`. "Decisions by Project"
     gets the filter. "Active" and "Superseded" already filter by status.
   - `project-index.md`: "Error Fixes" gets the filter and `LIMIT 50`.
   - `sessions-overview.md`: "Recent Insights" gets the filter. "Active Decisions" gets `LIMIT 20`.
   - `learning-velocity.md` and `open-items.md`: retros and tasks; no change. Say so in the PR.
   The same text is copied inline in `skills/obsidian-setup/SKILL.md` Step 6; update both and add a
   parity test that the inline copies equal `dashboards/*.md`. Installed copies in the vault are
   not overwritten in upgrade mode; the CHANGELOG says how to refresh them.
10. **The spike fixture (`tests/fixtures/458-eval/`).** Per unit: the ordered layered candidate
    list (an opaque id, `layer`, and whether it is a retro), the `current` top 20 and both judges'
    labels from rounds 1 and 2 merged per note. Ids are HMAC-SHA256 under a fresh random salt that
    is not stored, as in `455-eval`. No note names, text or judge reasons. The test runs the real
    `select_related` on each unit's list and checks: the selection equals the spike's `v2` set,
    and its pooled keep rate is at least `current` + 5 points under each judge.

## Failure modes (self-check classes, planned tests)

1. **Fail-open paths.** A row with `type` NULL is not a retro, test. `limit=0` returns [], test. `prune_provisional_members` on a theme whose centroid JSON
   is bad: treat as `{}` like `_delete_note`, test. An exception in prune aborts the whole
   `/consolidate` transaction (no half-pruned state), test with a forced error.
2. **Exit codes.** `/consolidate` exits 1 on a prune `sqlite3.Error`, as for the rest of the
   transaction; test.
3. **Date boundaries.** Two retros with the same date: the first in layer order is kept (Layer 1
   order is date DESC; ties keep SQLite's order, which the test pins by inserting rowids). No new
   date arithmetic.
4. **Doc counts.** CHANGELOG and architecture.json state "at most 1 retro" and the prune count from
   the live dry run; counted from the run, not from memory.
5. **Broken Markdown.** Dashboard fences and the obsidian-setup inline copies: the parity test
   fails on any drift; CHANGELOG entries inside `[Unreleased]`.
6. **Unrelated changes.** Only the files named in the tasks. Check `--diff-filter=D` before the PR.
7. **Inputs.** `query_related_notes` reads the index (`notes.tags`, `importance`, `status`); the
   index is the source of truth, and a stale row is re-synced by `ensure_index` before the query.
   Dashboards: the repo `dashboards/*.md` is the source; the obsidian-setup inline copy is checked
   against it (parity test). The fixture builder reads the spike dir and a DB copy, never the live
   DB for writing.

## Tasks

### Task 1: `/recall` retro rule — `hooks/vault_index.py`, `hooks/obsidian_utils.py`
- `RELATED_MAX_RETROS`, `select_related`; wire into `query_related_notes` (Ruling 3).
- File-scan fallback (Ruling 5).
- Tests (`tests/test_vault_index_related_retros.py`): 5 backlinked retros + 30 other notes → 1
  retro, the newest, and 20 results; retros in Layers 2 and 3 are also skipped once one is kept;
  a layer is not stopped early by skipped retros; Layer 3 fills the slots freed by skipped retros;
  `note_types` without retros is unchanged; access is logged only for selected rows; fallback.
  Mutation: drop the retro check → red.

### Task 2: themes and /emerge — `hooks/themes.py`, `hooks/consolidate_cli.py`
- Filters (Rulings 6, 8); `prune_provisional_members` (Ruling 7) called in `run_consolidate`.
- Tests (`tests/test_themes_provisional.py`): incremental and `--full` skip provisional and odd
  spellings (`Provisional`, `"provisional"`, `provisional # x`); `assign_to_theme` returns None
  for a provisional note and writes no row; prune reverse-folds the centroid (matches recomputing
  from the survivors), decrements the count, drops an emptied theme, and is idempotent; /emerge
  skips provisional. Mutation: remove each filter → red.

### Task 3: dashboards — `dashboards/*.md`, `skills/obsidian-setup/SKILL.md`
- Edits per Ruling 9. Test (`tests/test_dashboards.py`): every `FROM "claude-insights"` query
  either filters status or carries the provisional filter; every non-GROUP query has `LIMIT`; the
  obsidian-setup inline copies equal the files.

### Task 4: spike fixture — `tests/fixtures/458-eval/`, `tests/test_recall_cap_eval.py`
- `build_fixture.py` (dev tool, CI never runs it) from the spike dir; the test per Ruling 10.

### Task 5: docs
- CHANGELOG `[Unreleased]` (Changed: /recall keeps at most 1 retro; Fixed: themes, /emerge, dashboards; how to
  refresh installed dashboards and run `/consolidate` once).
- architecture.json: `query_related_notes`, themes, emerge, dashboards descriptions; re-render and
  smoke-test.

## Acceptance criteria → tasks

| Criterion | Task |
|---|---|
| ≤1 retro in /recall top 20, otherwise today's order (DB copy, 5 projects) | 1, live check |
| Retro rule test (all layers, fills `limit`, fallback) | 1 |
| /consolidate never adds provisional (both paths, odd spellings) | 2 |
| Cleanup on a DB copy: no provisional member, largest theme smaller | 2, live check |
| Dashboards filtered and limited, test | 3 |
| architecture.json updated | 5 |
| Spike bar met, labels committed anonymized | 4 |
| Re-run the #457 measurement after merge | after merge |
