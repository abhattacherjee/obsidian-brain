# Plan — #277: a 40-line frontmatter bound silently drops 28 live notes from the index

**Issue:** #277 (bug, P2-medium, milestone v3.3). `_parse_note()` (`hooks/vault_index.py:292`) searches for the closing frontmatter fence in only `lines[1:40]`. Any note whose frontmatter runs deeper returns `None` and is dropped from `notes`/`notes_fts` with no error — invisible to `/recall`, `/vault-search`, `/vault-ask`, `/standup` and `/emerge`. Silent because `_sync()` increments the same `skipped` counter for "mtime unchanged" (`:674`) and "unparseable" (`:679`).

## Acceptance (verbatim from the issue)

- All 28 notes appear in `notes` / `notes_fts` after a reindex.
- `/vault-search` returns content from a standup and an emerge-patterns note.
- A note with frontmatter deeper than the bound fails **loudly**, naming the file, rather than being silently skipped.
- A regression fixture with frontmatter deeper than 40 lines (use a real ~250-line `projects:` list shape, not a minimal one).
- A live-corpus audit confirms zero unparseable notes remain.

Also **Refs #128** — this lands item 2 of that six-item umbrella (split `skipped` into `unchanged` + `malformed`, and fix the SKILL.md wording). Items 1, 3, 4, 5, 6 of #128 stay open; do **not** close it.

## Live-corpus calibration (measured before design, read-only, 2026-07-26)

Run across all 2013 notes in `claude-sessions` + `claude-insights`:

| measurement | value |
|---|---|
| notes on disk | 2013 |
| currently indexed (`_parse_note` returns a dict) | 1985 |
| dropped by the 40-line bound | **28** (17 standup + 11 emerge-patterns) |
| closing-fence depth of the dropped notes | 44 → 460 |
| notes with an opening fence but no closing fence | **0** |
| notes rejected by `note_writer`'s shape check | **0** |
| notes indexed today that the shape check would reject | **0** (no regression) |

Fence-depth distribution: 1978 at `<20`, 7 at `20–29`, **nothing at `30–39`**, 7 at `40–99`, 18 at `100–299`, 3 at `300+`. The empty 30–39 band is the cliff that kept this invisible — no note ever failed marginally.

Also measured, and it **narrows** scope: every scalar key a bounded field-scan looks for (`type`, `project`, `session_id`, `date`, `git_branch`, `duration_minutes`) sits at a maximum depth of **9**, and **zero** notes carry any of them past line 20. The long `projects:` lists always follow the scalars. So the four `raw_lines[:20]` scans in `obsidian_utils.py` (`:4720`, `:4803`, `:4820`, `:4830`), the `lines[:20]` scan in `open_item_dedup.py:264`, the `lines[:30]` scan in `vault_doctor_checks/spurious_wikilinks.py:63`, and `_peek_frontmatter_field`'s 30-line bound are all **correct on real data and stay untouched**.

## Design decisions (grounded in code, reviewed before build)

1. **Adopt the shape check, do not merely raise the number.** `note_writer._split_frontmatter` already solved this problem properly: a bound *plus* a requirement that every candidate line be blank / `key:`-shaped / `- ` / an indented continuation. Raising 40 → 1000 without the shape check would be a net regression: a note with no closing fence but a `---` horizontal rule at line 300 would have 300 lines of prose parsed as frontmatter and be indexed with corrupt metadata — worse than being dropped. Fenceless notes are not hypothetical in this vault; `/vault-doctor` repaired 9 of them on 2026-07-26.

2. **Extract one shared, dependency-free module rather than copy the logic.** This defect class has now shipped three times with three different numbers (`note_writer._FM_MAX_LINES = 200`, `_parse_note` 40, `_peek_frontmatter_field` 30). A second hand-maintained copy of the shape check is the same bug waiting to recur. New `hooks/frontmatter.py` owns the constant, the regexes, and the splitter; `note_writer.py` and `vault_index.py` both import it.

3. **The new module must import nothing from the package.** `obsidian_utils` imports `vault_index` (`:29`, guarded) and `note_writer` imports `obsidian_utils` (`:38`). So `vault_index` → `note_writer` would close a cycle. `hooks/frontmatter.py` depends on `re` only, which keeps every existing import edge intact.

4. **`note_writer`'s observable behaviour must not change at all.** It shipped one day ago behind a full deep-review; this task is a pure move. Keep `_FM_MAX_LINES`, `_split_frontmatter`, `_split_lines_lf_crlf` as working names in `note_writer` (re-export), so its existing suite continues to exercise the same call paths and is the guard that the move was faithful.

5. **`_parse_note` keeps its `dict | None` signature.** It has two in-tree callers (`vault_index.py:677`, `:1248`) plus tests. Add `_parse_note_detailed(path) -> (dict | None, str | None)` carrying the failure reason, and reduce `_parse_note` to a wrapper returning the first element. No caller churn, and the reason becomes available where it is needed.

6. **`_sync` splits the counter and keeps `skipped` as the back-compat sum.** Add `unchanged`, `malformed`, and `malformed_files` (a capped list of `(basename, reason)`), leaving `skipped == unchanged + malformed` so any existing consumer keeps working while the report can name the broken files. This is #128 item 2.

7. **Backfill needs no `--full` — but must be proven, not assumed.** The 28 were never inserted, so they are absent from `indexed` and the mtime-equality guard at `:673` cannot skip them; the next default `/vault-reindex` re-parses and inserts them. That is a claim about control flow, so Phase 6 dogfoods it on a scratch copy of the live DB and asserts all 28 land in `notes` **and** `notes_fts`.

## Tasks

### Task 1 — extract `hooks/frontmatter.py`
Files: `hooks/frontmatter.py` (new), `hooks/note_writer.py`, `tests/test_frontmatter.py` (new).
- Move verbatim from `note_writer.py`: `_FM_MAX_LINES` (1000) → `MAX_FRONTMATTER_LINES`, `_FM_KEY_RE`, `_FM_ITEM_RE`, `_FM_CONT_RE`, `_split_lines_lf_crlf` → `split_lines_lf_crlf`, `_split_frontmatter` → `split_frontmatter`. Carry the existing docstring across — it records *why* the bound is 1000 and why the shape check exists; that rationale is the most valuable thing in the file.
- Stdlib `re` only. No import from `obsidian_utils`, `vault_index`, or `note_writer`.
- `note_writer.py` imports the new names and re-binds its private aliases; **delete** the moved definitions so exactly one copy exists.
- Tests fail-first: the full existing `test_note_writer.py` suite still passes unchanged (the faithfulness guard); plus direct tests on the new module for each of the five error strings, the exact-`MAX_FRONTMATTER_LINES` boundary (fence at the limit accepted, one past rejected — an exact-threshold fixture, not a wide-gap one), CRLF and bare-`\r` inputs, and a note whose body contains a `---` rule after a missing closing fence being rejected rather than mis-split.

### Task 2 — `_parse_note` adopts the shared splitter
Files: `hooks/vault_index.py`, `tests/test_vault_index_frontmatter.py` (new).
- Add `_parse_note_detailed(file_path) -> (dict | None, str | None)`; `_parse_note` becomes `return _parse_note_detailed(file_path)[0]`.
- Replace the `lines[1:40]` scan with `frontmatter.split_frontmatter(...)`; parse keys/tags from the returned frontmatter lines and take the body from the returned body lines. Preserve every existing behaviour: `tags:` block collection, `title` fallback to the first `# ` heading in the body, `source_session_note` → `source_note` wikilink stripping, `body` stripped.
- Failure reasons to propagate: unreadable file, no opening fence, and each `split_frontmatter` error verbatim.
- Tests fail-first: **a fixture whose frontmatter is a realistic ~250-line `projects:` list** (issue acceptance — not a minimal one) parses, and its `type`/`project`/`date`/`tags`/`body` are all correct; the same fixture fails to parse on the pre-fix 40-line bound (prove the test is load-bearing by reverting the bound, not by assertion alone); a fenceless note whose body holds a `---` rule returns `None` **and** does not silently produce body-derived metadata; existing round-trip behaviour for a normal short note is byte-identical to before.

### Task 3 — split the `_sync` counter and surface malformed files
Files: `hooks/vault_index.py`, `tests/test_vault_index_frontmatter.py`.
- `stats` gains `unchanged`, `malformed`, `malformed_files`; `skipped` retained as the sum. Cap `malformed_files` (20 entries) so a pathological vault cannot balloon the returned dict, and record the true count in `malformed`.
- Increment `unchanged` at `:674` (mtime match) and `malformed` at `:679` (parse failure), appending `(basename, reason)` from `_parse_note_detailed`.
- `rebuild_index` passes the new keys through to its returned stats in both preserve and full modes.
- Tests fail-first: a vault fixture mixing unchanged, changed, and malformed notes yields the exact expected split, and `skipped` still equals `unchanged + malformed`; `malformed_files` names the right file with a non-empty reason; the cap holds at 21+ malformed notes while `malformed` reports the true count.

### Task 4 — report, docs, architecture
Files: `skills/vault-reindex/SKILL.md`, `docs/architecture/architecture.json`, `docs/architecture/architecture.html`, `CHANGELOG.md`.
- `vault-reindex` Step 4: stop describing `skipped` as "files without valid frontmatter" — that wording is what made a healthy `skipped: 2011` read as a whole-vault failure and triggered this investigation. Report `unchanged` and `malformed` separately, and when `malformed > 0` list the named files so the user can act.
- `architecture.json`: register `hooks/frontmatter.py` as a component with its real path, wire it into the flows that reference `note_writer` and `vault_index`, keep `lastUpdated` current and `version` in sync with `plugin.json`. Re-render via the `architecture-page` script; `smoke-test.sh` must pass `[main]` and `[sparse]`.
- `CHANGELOG.md` `[Unreleased]` → Fixed: notes with long frontmatter are no longer silently excluded from the index; reindex now distinguishes unchanged from malformed.

## Out of scope / deferred
- The five other bounded field-scans listed under calibration — measured correct on the live corpus (max key depth 9 vs a bound of 20), so changing them would be churn against evidence.
- #128 items 1, 3, 4, 5, 6 (foreign-path prune surfacing, prune audit log, DB backup, dry-run, fall-through guard). Refs, not Closes.
- Re-tokenising already-indexed notes: unchanged mtimes keep their existing rows by design; `--full` remains the documented escape hatch.

## Verification
- `python3 -m pytest tests/ -q` green (2065 tests as of `b2a0b8d`; the count must go up, not sideways).
- `./scripts/commit-preflight.sh` before every commit, as a separate Bash call from `git commit`.
- Mutation-test every new guard: delete it, re-run, confirm a test actually fails. A guard whose removal leaves the suite green is not tested.
- Phase 6 dogfood on a **scratch copy** of `~/.claude/obsidian-brain-vault.db`; the live DB is checksummed before and after with the deterministic `find -print0 | sort -z | xargs -0 shasum` form and must be byte-identical.
- Post-fix live-corpus audit: re-run the read-only parse sweep and confirm **0** unparseable notes.
