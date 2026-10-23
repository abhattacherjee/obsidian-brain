# #447 + #448: parse the session cache once, index-filter bulk scans, clean access_log

Batch: one branch (`feature/447-448-cache-memo-access-log`), one PR, milestone v3.9.1.
Both issues touch the same hot paths (`hooks/obsidian_utils.py`, `hooks/vault_index.py`).

## Decisions (user, 2026-10-23)

- #448 cleanup rule: delete every `context_type = 'search'` row with timestamp at or after
  2026-10-18 00:00 local. Keep `recall` and `related` rows. Back up the DB and dry-run first.
- #447 AC reworded: a directory listing stays; only notes the index proves irrelevant skip the
  metadata read. This keeps malformed-note `discovery_errors` and notes newer than the last sync.
- #447 proposal item 3 (`find_latest_session`, reaper SID set) is out: neither uses the cache, so
  both are linear, and the index does not store a session note's `session_id`.

## Tasks

### Task 1: in-process cache memo and deferred writes (`hooks/obsidian_utils.py`)

- `_CACHE_MEMO: dict[path, (stat_key, data)]`, `stat_key = (st_ino, st_mtime_ns, st_size)`.
- `cache_get`: stat the file; on a key match use the memo, else `json.load` once and store it.
  Return a deep copy, so a caller that mutates the result cannot change the memo.
- `cache_set`: start from the memo when its key still matches the file, else re-read. Store a
  JSON round-trip of the value (keeps today's tuple→list, int-key→str behaviour). Write
  atomically; take the new stat key from `os.fstat` on the temp file before `os.replace`, so a
  write by another process after the rename cannot be mistaken for ours.
- `cache_deferred()` context manager: inside it `cache_set` updates the memo and marks keys dirty
  without writing; on exit, one flush re-reads the file if it changed, merges the dirty keys over
  it and writes once. Nesting is allowed (flush at the outermost exit). `cache_invalidate` inside
  the block flushes first, then runs as today.
- Bulk callers (`gather_session_evidence`, `build_context_brief`) run inside `cache_deferred()`.

### Task 2: index pre-filter helper (`hooks/obsidian_utils.py`)

- `_indexed_rows_under(folder: Path) -> dict[str, sqlite3.Row] | None`: read-only query of
  `path, mtime, type, project, source_session` for rows under `folder` (no `ensure_index`, no
  write). `None` when `vault_index` is unavailable, the DB file is missing, or any
  `sqlite3.Error`. The #192 pollution guard's `RuntimeError` is not caught.
- A note may skip its metadata read only when: it is indexed, its indexed `mtime` equals
  `os.stat().st_mtime`, and the indexed fields prove it irrelevant. Everything else goes through
  today's code unchanged.

### Task 3: `gather_session_evidence` uses the filter

- Keep `sorted(insights_path.glob("*.md"))`. For each note the helper proves irrelevant
  (`source_session` not in `id_set`), `continue` before `read_note_metadata_detailed`.
- Helper returns `None` → today's full scan.

### Task 4: `build_context_brief` session scan uses the filter

- Same listing and sort. Skip without reading when indexed, mtime unchanged, and the indexed
  `type == 'claude-snapshot'` or the indexed project fails today's project match.

### Task 5: `search_vault(..., log_access=True)` (`hooks/vault_index.py`)

- `log_access=False` skips `_batch_log_access`. Default stays `True`.
- Pass `False` from: `capture_save.find_match`, `wiki.lookup`, `open_item_dedup` FTS mentions,
  `/compress` SKILL.md dedup (3 calls), `scripts/tune_compress_rank_gap.py`.
- `capture_split._Scorer` does not call `search_vault` (it uses `original_vector` and
  `compute_query_vector`, which log nothing). The caller-grep test covers it so a future call
  cannot slip in.
- `/vault-search` and `/vault-ask` Step 3 keep logging. Update `wiki.lookup`'s docstring and
  `/vault-ask` Step 2b's "(the hits are logged as accesses)".

### Task 6: access-log GC (`hooks/vault_index.py`)

- `gc_access_log(conn, keep=ACCESS_LOG_KEEP_PER_NOTE)` (1,000, per the Friston spec): delete rows
  ranked past `keep` per `note_path` by `timestamp DESC, id DESC`. Returns the deleted count.
- `ensure_index`: run it after `_sync` only when `SELECT COUNT(*) FROM access_log > keep` (no
  note can exceed `keep` otherwise). `rebuild_index`: always, reported in its stats.

### Task 7: one-off purge (`hooks/vault_index.py` + `scripts/access_log_purge.py`)

- `purge_access_log(db_path, context_type, since_ts, *, apply=False, backup_dir=None)`: dry run
  prints counts by day and returns them; `apply=True` backs up the DB first with the sqlite
  backup API into `~/.claude/obsidian-brain/` (0o600), then deletes in one transaction and
  returns before/after counts. A failed backup aborts with no delete.
- Script: `--since YYYY-MM-DD` (local midnight), `--context search`, `--apply`. Default is the
  dry run. Exit 0 ok, 1 refused/failed, 2 usage.

### Task 8: docs

- `docs/architecture/architecture.json` (new script, new functions in flows), re-render, smoke test.
- `CHANGELOG.md` Unreleased entry.

## Failure modes (one line per self-check class; tests written first)

1. **Fail-open paths:** the helper's DB error must fall back to the full scan, not to "nothing
   matched" → test with the index forced to fail (patched `_connect` raising `sqlite3.Error`)
   asserts the same bundle and brief as the full scan. A stale index row (mtime differs) must not
   skip → test edits a note after indexing and asserts it is still found. Purge backup failure
   must not delete → test.
2. **Exit codes:** `access_log_purge.py` exits 1 on a missing DB or failed backup, 2 on a bad
   `--since` → tests.
3. **Date boundaries:** `--since` is local midnight; a row at 23:59:59 the day before is kept and
   one at 00:00:00 is deleted → boundary test.
4. **Doc counts:** CHANGELOG and PR numbers counted from the diff and live runs, not from memory.
5. **Broken Markdown:** CHANGELOG entry inside the Unreleased block; markdownlint via preflight.
6. **Unrelated changes:** `git diff --stat` / `--diff-filter=D` checked before PR; no deletions planned.
7. **Inputs:** the session cache file (on disk is truth; memo is a copy keyed by stat — test a
   write by "another process" between calls is seen), the index DB (truth is the note on disk;
   index row used only when mtime matches — test above), the vault folders (listing is truth).

**Bypass routes** (the pre-filter is a skip gate): the cheapest way past it is a note whose
indexed fields are stale but whose mtime was restored (e.g. `touch -r`). Accepted: it needs a
deliberate mtime rewind, and `_sync` itself trusts the same mtime check. A note missing from the
index always goes through the full read.

## Tests

- `tests/test_cache_memo.py`: ≤1 `json.load` over 1,000 `cache_get` calls; external rewrite seen;
  mutation of a returned value does not leak; `cache_deferred` writes once and merges an
  external key written mid-block; invalidate inside the block.
- `tests/test_bulk_scan_index_filter.py`: parity of `gather_session_evidence` and
  `build_context_brief` with index vs forced-failure; stale-mtime note found; malformed note still
  reported; growth guard: metadata reads for irrelevant notes stay 0 as the fixture doubles (an
  operation count, not wall-clock, so it cannot flake).
- `tests/test_access_log_flag.py`: `log_access=False` writes no rows; default logs; caller grep
  over `hooks/`, `skills/`, `scripts/` — every `search_vault(` call outside the user-facing list
  passes `log_access=False`.
- `tests/test_access_log_gc.py`: >N rows for one note → newest N kept; rerank on a fixed query
  set unchanged when no note exceeds N; `ensure_index` gate.
- `tests/test_access_log_purge.py`: dry run deletes nothing; apply backs up then deletes only
  matching rows; boundary; backup failure aborts; script exit codes.
- Live (recorded in the PR): `gather_session_evidence` and `build_context_brief` timings on the
  live vault, before and after.
