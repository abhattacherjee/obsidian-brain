# Plan: vault-ask/vault-search robustness batch (#376, #312, #375)

Milestone v3.7. One branch (`feature/376-312-375-vault-scan`), one PR, three commits.

## Acceptance criteria (from the issues)

| # | Issue | Criterion |
|---|---|---|
| 1 | #376 | `_TYPE_SCORES_BY_CONTEXT` has an explicit weight for claude-snapshot, claude-stats, claude-emerge, claude-memory and claude-check-items-report in every context. |
| 2 | #376 | A test fails if a `type: claude-*` written by any writer in hooks/, skills/ or templates/ has no weight in some context. |
| 3 | #312 | No skill instructs a fixed small-line frontmatter read that can silently miss a field, or every such read has an explicit recovery path. |
| 4 | #312 | The rationale at vault-ask Step 5 reflects real frontmatter depth (closing fences seen at line 460), not "exceeding 20 lines". |
| 5 | #312 | Verified against a real deep-frontmatter note (`2026-04-24-emerge-patterns-ab12.md`, fields at line 445). |
| 6 | #375 | vault-ask Step 4 and vault-search Step 4 say what to run when the Grep tool is missing: `hooks/vault_scan.py grep`. |
| 7 | #375 | On the live vault, the fallback returns the same files as the Grep tool for each pattern shape the skills use. |

## Task 1 — #376 type weights (commit 1, includes this plan file)

`hooks/vault_index.py` `_TYPE_SCORES_BY_CONTEXT` (~l.1482). Unknown types fall back to 0.5 at ~l.1673 (`type_scores.get(type, 0.5)`).

Add to every one of the 5 contexts:

| type | debugging | standup | search | emerge | general | why |
|---|---|---|---|---|---|---|
| claude-snapshot | 0.7 | 0.6 | 0.4 | 0.4 | 0.4 | mid-session context, like a session note but less curated |
| claude-memory | 0.6 | 0.3 | 0.9 | 0.8 | 0.9 | curated, migrated memory facts, close to insights |
| claude-emerge | 0.1 | 0.4 | 0.3 | 0.0 | 0.3 | pattern reports; 0.0 in emerge so it does not re-ingest its own output |
| claude-stats | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | health reports, not knowledge |
| claude-check-items-report | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | triage reports, not knowledge |

Add one short comment above the table saying unknown types score 0.5 and that `tests/test_type_scores.py` requires every written type to be listed.

New `tests/test_type_scores.py`:
- Scan `hooks/*.py`, `skills/*/SKILL.md`, `templates/*.md` for `type:\s*"?(claude-[a-z-]+)` and collect the set. Union it with `{"claude-memory"}` (written outside this repo; 3 live notes).
- Exclude folder-name false positives only if they appear (e.g. `claude-sessions`, `claude-insights`); prefer a regex that does not match them. Assert the scan found at least the 6 original types (a positive control, so an empty scan cannot pass).
- Assert every context in `_TYPE_SCORES_BY_CONTEXT` has a key for every collected type.
- Assert all contexts share the same key set.
- Prove fail-first: removing `claude-snapshot` from one context must fail the test.

## Task 2 — `hooks/vault_scan.py` (commit 2, with Task 3)

Stdlib-only CLI. Follow `hooks/note_writer.py` for structure, validation and error shape (`ERROR: <reason>` on stderr, non-zero exit). Reuse note_writer's `_validate_vault_path` and `_validate_folder` (read their real signatures first).

### `grep`

`python3 hooks/vault_scan.py grep <vault> <folder> [<folder> ...] --pattern <regex> [--ignore-case] [--frontmatter-only]`

- Walks each `<vault>/<folder>` recursively for `*.md` (the Grep tool's `glob="*.md"` is recursive).
- Skips any file whose `resolve()` is not under the resolved vault (symlink escape), and any file over 5 MB.
- Compiles the pattern with `re.MULTILINE` (plus `re.IGNORECASE` with the flag), so `^key:.*value` matches per line the way ripgrep does. Invalid regex → `ERROR:` + exit 2.
- Reads with `encoding="utf-8", errors="replace"`.
- `--frontmatter-only`: split with `frontmatter.split_lines_lf_crlf` + `frontmatter.split_frontmatter` and search only the frontmatter lines (joined with `\n`). A file whose frontmatter does not parse is skipped and counted.
- stdout: matching absolute paths, sorted, one per line. stderr: always one summary line, `vault_scan: <N> match(es), <M> file(s) scanned, <K> skipped`, so an empty stdout is never ambiguous.
- Exit 0 whether or not anything matched; 2 for bad arguments; 1 for an unexpected error.

### `meta`

`python3 hooks/vault_scan.py meta <vault> <file> [<file> ...]`

- For each file, output one JSON object per line (JSON Lines): `path`, `date`, `type`, `project`, `session_id`, `source_session_note`, `tags` (list), `title` (first `# ` heading in the body, else the filename stem), `snippet` (first 200 chars of the body, stripped), `error` (null on success).
- Parse through `obsidian_utils.read_note_metadata_detailed` (or `frontmatter.split_frontmatter` directly if importing obsidian_utils is too heavy — say which you chose and why). No line bound below `frontmatter.MAX_FRONTMATTER_LINES`.
- A file outside the vault (after `resolve()`), missing, or with unparsable frontmatter gets an `error` string and null fields; it never aborts the other files. Exit 0 if the arguments were valid.

### Tests — `tests/test_vault_scan.py`

Use `tmp_path` vaults. Cover at least:
- content match; `--ignore-case`; `^type:.*decision` per-line match; recursive subfolder.
- `--frontmatter-only`: a tag at frontmatter line ~300 is found; the same string only in the body is NOT found; a note with a broken fence is skipped and counted in stderr.
- containment: folder `..`, absolute folder, and a symlinked file pointing outside the vault are rejected or skipped.
- invalid regex → exit 2 with `ERROR:`.
- the stderr summary line is always printed, including on zero matches.
- `meta`: fields at line ~445 of a 460-line frontmatter are returned; out-of-vault path → `error` set, other files still returned; title fallback to stem; snippet length ≤ 200.
- Run the CLI as a subprocess for at least the happy path and one error path, and call the functions in-process for coverage.

## Task 3 — skill prose (commit 2)

`HOOKS` resolution: reuse the exact `_ob_hooks()` resolver block the skills already use (copy it from the same SKILL.md). Values pasted from earlier steps go in single quotes (`'<vault_path>'`), never `"$VAR"` — see #386.

`skills/vault-ask/SKILL.md`
- Step 4 (~l.135): keep "Use the Grep tool (never Bash grep)". Add: if the Grep tool is not available in this session, run each of the same searches with `vault_scan.py grep` (one call per pattern, folder name(s) as arguments, `--ignore-case` for `-i=true`), and use its stdout as the file list. Give one bash block.
- Step 5 (~l.183-185): replace `Read(file_path=..., limit=40)` with one `vault_scan.py meta` call over all `CANDIDATE_FILES`, reading `type` and `date` from its JSON Lines. Replace the "exceeding 20 lines" rationale: frontmatter can run past line 400 (fences seen at line 460), so a fixed line limit silently drops fields.

`skills/vault-search/SKILL.md`
- Step 2 tag mode (~l.76): "Search only within the first 30 lines" → search only the frontmatter, via `vault_scan.py grep --frontmatter-only` (the Grep tool cannot restrict a search to frontmatter).
- Step 4: make the tag-mode search use `vault_scan.py grep --frontmatter-only`; for structured and keyword mode, keep the Grep tool and add the same "Grep tool not available" fallback as vault-ask.
- Step 5 (~l.154): replace "use Read to read the first 40 lines" with one `vault_scan.py meta` call; keep the field list; snippet and title come from `meta`.

Skill-text tests, `tests/test_vault_scan_skill_text.py`:
- Both skills mention `vault_scan.py grep` in Step 4 and `vault_scan.py meta` in Step 5.
- Neither skill contains "first 30 lines", "first 40 lines", `limit=40` or "exceeding 20 lines".
- vault-search tag mode mentions `--frontmatter-only`.
- `tests/test_skill_shell_blocks.py` and `tests/test_skill_snippets.py` still pass (they lint the bash blocks).

## Task 4 — docs (commit 3)

- `docs/architecture/architecture.json`: add a `vault_scan` component (file `hooks/vault_scan.py`) in the layer holding note_writer/CLI modules; wire it to vault-ask and vault-search if flows list skill→module steps. Set `lastUpdated` to 2026-10-02; keep `version` = plugin.json.
- Re-render: `~/.claude/skills/architecture-page/scripts/render-html.sh --json docs/architecture/architecture.json --output docs/architecture/architecture.html`
- Validate: `~/.claude/skills/architecture-page/scripts/smoke-test.sh --json docs/architecture/architecture.json` — `[main]` and `[sparse]` must pass.
- `CHANGELOG.md` under `## [Unreleased]`: `### Added` (vault_scan.py, #375/#312), `### Fixed` (type weights #376; 30/40-line frontmatter reads #312; Grep fallback #375). Plain English, each line ending with its issue number.

## Live checks (orchestrator runs these after the implementer, not the implementer)

- Criterion 5: `vault_scan.py meta` on `2026-04-24-emerge-patterns-ab12.md` returns `type`/`date`/`tags`; a 40-line read does not.
- Criterion 7: for one term per shape (content term, `claude/topic/.*<term>`, `^type:.*decision`), compare `vault_scan.py grep` output with the Grep tool's file list on the live vault.
