# #59 — config cache must notice a changed config file

Issue: #59. Base: `develop` at 182aed3.

## Root cause (reproduced by `tests/test_load_config_defaults_cache.py::test_cache_still_used_while_defaults_are_unchanged`)

`load_config()` (`hooks/obsidian_utils.py:2470`) caches the parsed config in
`cache-<sid>.json` and trusts it for the whole session while `_DEFAULTS` is unchanged. A write
to `~/.claude/obsidian-brain-config.json` (the `/vault-config` Step 5 rename, a hand edit,
another session) is never seen. That test asserts the stale read today ("the cached view wins").

## Design

Cache a signature of the config file beside the dict, and trust the cache only when the
file's signature now matches.

- `_config_file_sig() -> list | str`: `[st_ino, st_mtime_ns, st_size]` from `os.stat(_CONFIG_PATH)`,
  or the string `"absent"` on `OSError`. A string sentinel, not `None`, because `cache_get`
  returns `None` for a missing key: a cache written by older code (no sig key) must never
  match a missing file.
- `load_config`: compute the file sig **before** reading the file. Trust the cache only when
  `cache_get(sid, "config_file_sig") == file_sig` (plus the existing `_DEFAULTS` checks). On a
  miss, read, then `cache_set(sid, "config_file_sig", file_sig)` with the sig taken before
  the read. A write racing between stat and read then leaves a sig that no longer matches,
  so the next call re-reads (fails toward a re-read, never toward a stale pin).
- Cost: one `os.stat` per call. The cache file read is unchanged.

`fresh=True` stays (callers keep working).

## Task 1: file-signature check in `load_config`

Delegate: codex — one source file plus one test file, `pytest` check.

- `hooks/obsidian_utils.py`: add `_config_file_sig()`; wire it into `load_config` as above;
  update the docstring (name #59).
- `tests/test_load_config_defaults_cache.py`:
  - rename `test_cache_still_used_while_defaults_are_unchanged` to
    `test_rewritten_config_is_read_in_the_same_session`: after the rewrite, plain
    `load_config()` returns `/elsewhere` (AC 1, AC 6).
  - `test_unchanged_file_serves_the_cache`: second call does not open the config file
    (monkeypatch `builtins.open` or count `json.load` on `_CONFIG_PATH`) (AC 2).
  - `test_atomic_rename_is_seen`: rewrite via tempfile + `os.rename` with the same size
    (new inode) → new value (AC 1).
  - `test_missing_file_cached_then_created_file_is_read`: no file → defaults; a second call
    with no file does not re-stat-miss into a read loop (returns cached defaults); create the
    file → its value (AC 4).
  - `test_cache_from_older_code_without_file_sig_is_not_trusted`: cache holds `config` and
    `config_defaults_sig` but no `config_file_sig`, file missing → still re-reads (the
    `"absent"` sentinel never equals `None`).
  - `test_sig_taken_before_read`: monkeypatch so the file changes between stat and read;
    the next call re-reads.
  - `test_file_sig_is_not_a_config_key`.

## Task 2: `/vault-config` re-reads before redisplay; lessons_enabled end-to-end

Delegate: codex — one SKILL.md plus one new test file, `pytest` check.

- `skills/vault-config/SKILL.md`:
  - Step 5's last line: "Confirm the change, then go back to Step 1 to re-read the config and
    redisplay the table (Step 2)." (AC 5)
  - Replace "other skills in this session read a cached copy of the config, so tell the user
    to run `/vault-reindex`" with: the index still holds notes from the old folders, so tell
    the user to run `/vault-reindex`. (The cache is no longer stale; the index is.)
- `tests/test_vault_config_cache_invalidation.py` (name from the issue):
  - Extract the Step 5 `python3 -c '...'` snippet from `skills/vault-config/SKILL.md` and the
    Step 7.6 lessons read (`print("off" if c.get("lessons_enabled") is False else "on")`) from
    `skills/retro/SKILL.md`; fail loudly if either is not found.
  - With `HOME=tmp_path` and `_CONFIG_PATH` monkeypatched to `tmp_path/.claude/obsidian-brain-config.json`
    holding `lessons_enabled: false` and a real vault/insights dir: `retro_rollup.Ctx` sees
    `False`; run the Step 5 snippet (`lessons_enabled`, `true`) by subprocess; `Ctx` now sees
    `True` and the Step 7.6 snippet prints `on` (AC 3).
  - Same flow for `snapshot_on_clear` → `false`: plain `load_config()["snapshot_on_clear"] is False`
    (the value the ⚠ row keys on) (AC 5).
  - Text checks: Step 5 says "go back to Step 1"; the "read a cached copy" sentence is gone (AC 5).

## Task 3: docs

Delegate: claude — architecture JSON, render script and CHANGELOG need repo-wide context.

- `docs/architecture/architecture.json` `load_config()` purpose: the cache is keyed on the
  config file's `(st_ino, st_mtime_ns, st_size)` too (#59); `lastUpdated` 2026-11-12.
  Re-render HTML; run the smoke test.
- `CHANGELOG.md`: Unreleased → Fixed entry.

## Failure modes

- **Fail-open paths:** `os.stat` error → `"absent"` sentinel → treated as missing file →
  read path prints the existing "config not found" warning and returns defaults. Test:
  missing-file case. Cache from older code with no sig key → mismatch → re-read. Test above.
- **Exit codes:** no CLI or exit code changes. N/A.
- **Date boundaries:** none; `st_mtime_ns` is compared for equality only, never ordered.
  Same-size in-place rewrite within one filesystem timestamp tick is missed (accepted: the
  `/vault-config` path renames, so its inode always changes; test `test_atomic_rename_is_seen`).
- **Doc counts:** CHANGELOG and architecture text state no counts. N/A.
- **Broken Markdown:** SKILL.md edit inside existing Step 5 paragraph; `tests/test_skill_snippets.py`
  still compiles every snippet.
- **Unrelated changes:** diff limited to the 6 files above plus the plan.
- **Inputs:** reads `~/.claude/obsidian-brain-config.json` (source of truth) and
  `cache-<sid>.json` (derived copy). Stale copy → sig mismatch test; missing source → missing-file
  test; missing sig in copy → older-code test.
- **Bypass routes:** the guard is the cache trust check. Cheapest stale input that would pass:
  a cache whose `config_file_sig` equals the current file sig but whose dict came from an older
  file. Only possible if sig is taken after the read; `test_sig_taken_before_read` covers it.
