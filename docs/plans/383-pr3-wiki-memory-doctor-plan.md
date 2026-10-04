# Wiki Memory Sources and Doctor Check Implementation Plan (#396, #383 PR 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/vault-ask` searches Claude Code memory files, cites them and counts them toward the 3-source threshold. A new `wiki-pages` vault-doctor check reports unhealthy wiki pages and rebuilds a drifted index.

**Architecture:** `hooks/memory_sources.py` is the one host-specific place: it lists memory files on Claude Code and nothing elsewhere. `hooks/wiki.py` gains memory-aware `count`, `file` and `stale`, a `memgrep` subcommand, and a pure `render_wiki_index`. `scripts/vault_doctor_checks/wiki_pages.py` reuses `wiki.py` for staleness and index rendering.

**Tech Stack:** Python 3.9+ stdlib, pytest, SKILL.md prose.

**Spec:** `docs/plans/383-llm-wiki-design.md` (PR 3 section, Data model, Threshold, Error handling).

## Global Constraints

- Stdlib only. Hooks exit 0 on their own errors; `wiki.py` keeps exit 0 ok / 1 refused (`ERROR:` line) / 2 usage error or crash.
- Every vault write goes through `write_vault_note` (atomic, `0o600`), except deleting stale `index-*.md` files typed `claude-wiki-index`.
- Memory paths: resolved, `is_relative_to(~/.claude/projects)`, symlinks skipped, `MEMORY.md` excluded (spec line 87).
- Memory citations are plain text `memory: <project-dir>/<file>.md`, never wikilinks (spec line 89).
- No Claude-only path in skill prose: the skill always calls `wiki.py memgrep`; only `memory_sources.py` knows the host.
- Paths into `python3 -c` go through `sys.argv`; JSON goes through payload files, never shell strings.
- Plain English in docs and comments. Conventional commits with the session trailer.

## Rulings (made before building)

1. **`memgrep` input.** `wiki.py memgrep` reads a JSON payload `{"pattern": "<term>"}` from stdin, like every other subcommand, and prints `{"matches": [{"name": "<project-dir>/<file>.md", "path": "<abs>"}]}`. The spec says `--pattern <term>`. A payload file keeps quotes in terms safe and matches the other subcommands. The match is a case-insensitive **fixed string**, not a regex, so a term can never be a slow pattern.
2. **Memory source names.** A memory source is named `<project-dir>/<file>.md`, exactly as `memgrep` prints it. `count` resolves it against `memory_sources(detect_host(), config)`. An unknown name is rejected (`"not a memory file on this host"`). Memory files count as `claude-memory`. They are told apart by resolved path, like vault notes.
3. **Fingerprint keys.** Memory fingerprints use the key `memory:<project-dir>/<file>.md` (spec line 122). `stale` checks those keys against the memory file on disk (`changed:` / `missing:` with the same `memory:` name). It does not send them to `resolve_sources`. A page whose `memory_sources` lists a name with no fingerprint is `unverifiable: memory:<name>`.
4. **Doctor reason rules**, per page:
   - `broken-source` when any `stale` reason starts with `missing:`.
   - `reviewed-stale` when the page is reviewed and `stale` returns any reason.
   - `stale` when the page is not reviewed and `stale` returns any reason other than `missing:`.
   - `auto-filed` when `filed_by` is `auto`.
   - `orphan` when no vault note other than the page itself and the wiki's index and log files holds `[[<page stem>]]` (aliases and headings allowed). Only pages whose `updated` date falls inside `--days` are checked for this. `DEFAULT_WINDOW_DAYS = 9999`.
   - `index-drift`: one row when the index files on disk differ from `render_wiki_index`. That covers a missing file, an extra `index-*.md` typed `claude-wiki-index`, or different content.
   - A page that cannot be read gets `page-unreadable`.
   - Confidence is 1.0 for `index-drift`, the only row `--apply` fixes, and 0.0 for every other row.
5. **Doctor index freshness.** The check calls `vault_index.ensure_index` on `indexed_folders(strict=True)` before rendering. Index lines come from the notes table, so a stale table would invent drift. This matches what `/vault-ask` already does.
6. **Doctor apply.** It copies each current index file to `<backup_root>/wiki-pages/`, then calls `wiki.rebuild_wiki_index` under `wiki._acquire_lock`. If the lock is held, the result is `skipped` with the reason.
7. **Wiki off or missing.** If `wiki_folder` is off or invalid, or the folder does not exist, `scan` prints one stderr line and returns `[]`. A missing wiki is not a fault.

## Review Focus

1. A memory file that is a symlink, or a `memory/` directory that is a symlink pointing outside `~/.claude/projects`, must never be listed, read or counted.
2. A memory name sent to `count` that matches a vault note name (`feedback_x.md` vs note `feedback_x`) must not count twice or resolve to the wrong file.
3. A page citing a memory file that was later deleted must read as `missing: memory:…`, not crash the doctor or `stale`.
4. The `orphan` scan must not count the wiki's own `index*.md` / `log-*.md` links as inbound links, and must count a link written as `[[stem|alias]]` or `[[stem#heading]]`.
5. `index-drift --apply` must leave a page untouched and only rewrite index files; running the check twice in a row after `--apply` must report no drift.

---

### Task 1: `hooks/memory_sources.py`

**Files:**
- Create: `hooks/memory_sources.py`
- Test: `tests/test_memory_sources.py`

**Interfaces:**
- Produces: `detect_host() -> str` (`"codex"` when any `obsidian_utils._CODEX_HOST_MARKERS` env var is non-blank, else `"claude-code"`); `memory_sources(host: str, config: dict | None = None) -> list[Path]` (sorted resolved paths); `memory_name(path: Path) -> str` (`"<project-dir>/<file>.md"`).

- [ ] **Step 1: Write the failing tests** (monkeypatch `HOME` to `tmp_path`):
  - lists `projects/a/memory/x.md` and `projects/b/memory/y.md`; skips `MEMORY.md`;
  - skips a symlinked file and a symlinked `memory/` dir pointing outside;
  - skips non-`.md` files and nested subfolders (`memory/sub/z.md`);
  - returns `[]` for host `"codex"` and when `~/.claude/projects` is missing;
  - `detect_host()` returns `"codex"` with `CODEX_THREAD_ID=1`, `"claude-code"` with it blank;
  - `memory_name` gives `"a/x.md"`.
- [ ] **Step 2:** Run `python3 -m pytest tests/test_memory_sources.py -q -p no:cacheprovider`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement.**

```python
def memory_sources(host, config=None):
    if host != "claude-code":
        return []  # Codex memory arrives with #272
    root = Path.home() / ".claude" / "projects"
    try:
        real_root = root.resolve(strict=True)
    except OSError:
        return []
    out = []
    for proj in sorted(root.iterdir()):
        mem = proj / "memory"
        if mem.is_symlink() or not mem.is_dir():
            continue
        for f in sorted(mem.glob("*.md")):
            if f.name == "MEMORY.md" or f.is_symlink() or not f.is_file():
                continue
            real = f.resolve()
            if real.is_relative_to(real_root):
                out.append(real)
    return out
```
  Wrap each `iterdir`/`glob` in `try/except OSError: continue` so one unreadable project dir does not hide the rest.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat(obsidian-brain): memory_sources lists Claude Code memory files (#396)`.

### Task 2: Memory-aware `count`, `file`, `stale`, plus `memgrep`

**Files:**
- Modify: `hooks/wiki.py` (`count_sources`, `_validate_payload`, `file_page` meta, `stale`, new `memgrep` + `_cmd_memgrep`, `main` dispatch, module docstring)
- Test: `tests/test_wiki.py`, `tests/test_wiki_cli.py`

**Interfaces:**
- Consumes: Task 1's `detect_host`, `memory_sources`, `memory_name`.
- Produces: `memgrep(pattern: str, files: list[Path]) -> list[dict]`; `count_sources(..., memory_sources=[names])` now counts them; page frontmatter `memory_sources: [names]` and `sources_fingerprint["memory:<name>"]`.

- [ ] **Step 1: Failing tests** (fixture monkeypatches `wiki._memory_files` to return files under `tmp_path`; `_memory_files()` is a thin wrapper over `memory_sources(detect_host())`):
  - `count` with 2 notes + 1 memory name → 3 qualifying; the memory name appears in `qualifying`.
  - unknown memory name → rejected with "not a memory file on this host"; the same memory name twice → counts once.
  - `file` with 2 notes + 1 memory → the page has `memory_sources: ["proj/x.md"]` and the fingerprint key `memory:proj/x.md`; the body keeps `memory: proj/x.md` as plain text.
  - `stale` after editing the memory file → `changed: memory:proj/x.md`; after deleting it → `missing: memory:proj/x.md`; memory listed without a fingerprint → `unverifiable: memory:proj/x.md`.
  - `memgrep {"pattern": "ZebraCorn"}` matches case-insensitively, and the fixed string `a.c` does not match `abc`; an empty pattern is refused (exit 1); output names use `memory_name`.
  - Remove `test_memory_sources_refused_until_396` (its premise ends here) and say so in the commit body.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3: Implement** per Rulings 1–3. `memgrep` reads each file with `errors="replace"` and skips unreadable files. A `memory_sources` payload entry must be a string of shape `<dir>/<file>.md` with no `..`, else refused.
- [ ] **Step 4:** Run `tests/test_wiki.py tests/test_wiki_cli.py`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(obsidian-brain): wiki counts, files and checks memory sources; add memgrep (#396)`.

### Task 3: `/vault-ask` memory search

**Files:**
- Modify: `skills/vault-ask/SKILL.md` (Step 3 "5+ skips Step 4" rule, Step 4 fourth search, Steps 6–8 citations, count/file payloads)
- Test: `tests/test_vault_ask_wiki_skill_text.py`

- [ ] **Step 1: Failing skill-text tests** pinning these phrases: `wiki.py" memgrep`; "the memory search runs on every ask"; `memory: <project-dir>/<file>.md`; "never as a wikilink"; `"memory_sources": [` with names from memgrep. Also add a test that `~/.claude/projects` does not appear in the skill (no Claude-only path).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Edit the skill. Step 3: when the FTS fast path has 5+ hits, still run the memory search from Step 4 (only the Grep agents are skipped). Step 4: per term, write `{"pattern": "<term>"}` to a payload file and run `python3 "<hooks_dir>/wiki.py" memgrep`. Add each match to `CANDIDATE_FILES` as type `claude-memory`, read it by `path`, and cite it by `name`. Step 8: put memory names in `memory_sources` for both `count` and `file`.
- [ ] **Step 4:** Run the skill-text tests plus `tests/test_vault_scan_skill_text.py`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(obsidian-brain): vault-ask searches and cites memory files (#396)`.

### Task 4: `render_wiki_index`

**Files:**
- Modify: `hooks/wiki.py` (split `rebuild_wiki_index` into `render_wiki_index(ctx) -> dict[str, str]` plus the writer)
- Test: `tests/test_wiki.py`

- [ ] **Step 1: Failing test:** `render_wiki_index(ctx)` returns `{"index.md": <text>}` that equals the file `file_page` wrote; with `INDEX_SPLIT=1` and two projects, it returns `index.md` plus `index-<project>.md` keys; it writes nothing (the tree is unchanged).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Move the content-building half into `render_wiki_index`. `rebuild_wiki_index` calls it, writes each file, then deletes extras typed `claude-wiki-index` (behaviour unchanged).
- [ ] **Step 4:** Run `tests/test_wiki.py`. Expected: PASS, with no other test changed.
- [ ] **Step 5:** Commit `refactor(obsidian-brain): split render_wiki_index from the writer (#396)`.

### Task 5: `wiki-pages` vault-doctor check

**Files:**
- Create: `scripts/vault_doctor_checks/wiki_pages.py`
- Modify: `skills/vault-doctor/SKILL.md` (one bullet in the check list, same style as its neighbours)
- Test: `tests/test_vault_doctor_wiki_pages.py`, `tests/test_vault_doctor.py` (registry test)

**Interfaces:**
- Consumes: `wiki._context`-equivalent config loading, `wiki.stale`, `wiki.read_page`, `wiki.is_reviewed`, `wiki.render_wiki_index`, `wiki.rebuild_wiki_index`, `wiki._acquire_lock`.
- Produces: `NAME = "wiki-pages"`, `OPT_IN = False`, `DEFAULT_WINDOW_DAYS = 9999`, `scan(...)`, `apply(...)`.

- [ ] **Step 1: Failing tests.** Fixture: a tmp vault with sessions, insights and `claude-wiki`, a config file pointed at it (follow `tests/test_vault_doctor_memory_index.py` for the config and HOME setup), and pages filed through `wiki.file_page`. One positive and one negative fixture per reason:
  - `stale`: edit a source (positive); untouched (negative).
  - `broken-source`: delete a source (positive); all present (negative).
  - `reviewed-stale`: reviewed and edited (positive, and no plain `stale` row); reviewed and untouched (negative).
  - `auto-filed`: `filed_by: auto` (positive); `user` (negative).
  - `orphan`: no inbound link (positive); a session note holding `[[stem|alias]]` (negative); a link only from `index.md`/`log-2026.md` still counts as orphan (positive); a page outside `--days` is not checked.
  - `index-drift`: hand-edit `index.md` (positive); fresh (negative); `apply` rewrites it, backs up the old file, a second `scan` shows no drift, and page files are byte-identical before and after.
  - Wiki off → `[]` plus a stderr note. Registry lists `wiki-pages` and it is in `all_checks()`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement per Rulings 4–7.
- [ ] **Step 4:** Run `tests/test_vault_doctor_wiki_pages.py tests/test_vault_doctor.py tests/test_vault_doctor_min_confidence.py`. Expected: PASS.
- [ ] **Step 5: Mutation-test each detector** in a scratch copy with `PYTHONDONTWRITEBYTECODE=1`: disable each of the 6 reason conditions and the index-file exclusion in `orphan`, one at a time, and name the test that fails for each.
- [ ] **Step 6:** Commit `feat(obsidian-brain): wiki-pages vault-doctor check (#396)`.

### Task 6: Global CLAUDE.md, docs, CHANGELOG

**Files:**
- Modify (outside the repo): `~/.claude/CLAUDE.md`. Record the exact before/after in the PR body.
- Modify: `docs/architecture/architecture.json` + re-render `.html`, `CHANGELOG.md` `[Unreleased]`, `docs/plans/383-llm-wiki-design.md` (reconcile Rulings 1–7)

- [ ] **Step 1:** Change the vault-ask bullet in `~/.claude/CLAUDE.md` so workflows and sub-agents run `/vault-ask --caller <workflow-name> <question>`. Keep the rest of the line.
- [ ] **Step 2:** architecture.json: new component `memory-sources` (hooks/memory_sources.py), new component `dc-wiki-pages` (doctor check), `wiki-cli` purpose mentions `memgrep` and memory sources, a flow for memory search in vault-ask. Keep `lastUpdated` and `version` rules. Re-render, then run the smoke test (`[main]` and `[sparse]` PASS).
- [ ] **Step 3:** CHANGELOG entry under `[Unreleased]` → Added.
- [ ] **Step 4:** Run `./scripts/commit-preflight.sh` with the files staged, then commit `docs: wiki memory sources and doctor check (#396)`.
