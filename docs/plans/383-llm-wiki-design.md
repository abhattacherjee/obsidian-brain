# LLM wiki for /vault-ask answers: design

- **Issue:** abhattacherjee/obsidian-brain#383
- **Date:** 2026-10-03
- **Status:** draft, sections 1-3 approved in chat 2026-10-03; reviewed-page flag added 2026-10-03
- **Milestone:** v3.8
- **Related:** #377 (rerank eval, also edits vault-ask Steps 5 and 8), #376 (type-weight guard test), #272 (Codex parity, v3.11)

## Goal

`/vault-ask` throws away every answer when the chat ends. This design keeps the expensive ones as wiki pages in the vault, finds them first on later asks, and refreshes them when their sources change. It follows Karpathy's LLM Wiki pattern. The issue body records the 8 product decisions; this spec turns them into components, data and tests, grounded in the code as of `develop` at `1f73d4f`.

## Delivery: one spec, three PRs

| PR | Branch | Contents | Depends on |
|---|---|---|---|
| 1 Foundation | `feature/383-wiki-foundation` | `indexed_folders(config)`, `wiki_folder` config key, indexer exclusion of the wiki index and log files, `claude-wiki` type weight | none |
| 2 Wiki core | `feature/383-wiki-core` | `hooks/wiki.py` and its CLI, vault-ask Step 2b (wiki first) and Step 8 (filing gate), `--caller` auto-filing, dedupe, stale refresh, the STE writing rule | PR 1 |
| 3 Memory + doctor | `feature/383-wiki-memory-doctor` | `memory_sources(host, config)`, memory grep in vault-ask, the `wiki-pages` vault-doctor check, global CLAUDE.md `--caller` update | PR 2 |

Each PR goes through `/ship` on its own. #383 closes with PR 3; PRs 1 and 2 use `Refs #383`. Until PR 3 lands, the filing threshold counts vault notes only.

## Facts from the code that shape the design

- The indexed-folder list is a literal in about 12 places: `skills/vault-ask/SKILL.md:120`, `skills/vault-search/SKILL.md:115`, `skills/compress/SKILL.md:122` and `:311`, `skills/vault-stats/SKILL.md:52`, `skills/vault-reindex/SKILL.md:104`, `skills/obsidian-setup/SKILL.md:563`, `hooks/obsidian_utils.py:5145`, `hooks/open_item_dedup.py:1279` (and its cache key at `:1184-1194`, and scans at `:1292`, `:1743`), `scripts/tune_compress_rank_gap.py:91`, `scripts/test-phase1-manual.sh:145`.
- `vault_index._sync` walks `rglob("*.md")` under each folder with no filename filter (`hooks/vault_index.py:765`), so an `index.md` would be indexed today.
- `_TYPE_SCORES_BY_CONTEXT` (`hooks/vault_index.py:1495`) has 5 contexts with one shared key set. `tests/test_type_scores.py` fails when a writer file emits a `type: claude-*` with no weight, and pins the exact type set.
- `hooks/obsidian_utils.py` is excluded from the 90% coverage gate (`setup.cfg`). New logic goes in new modules so the gate covers it.
- `write_vault_note` and `note_writer.run_write` do not call `scrub_secrets`; callers scrub first.
- Claude memory files live under `~/.claude/projects/*/memory/`, outside the vault. The indexer only walks vault-relative folders, and a `[[wikilink]]` cannot resolve to them.
- There is no host detector for skills. Hooks use `_CODEX_HOST_MARKERS` (`hooks/obsidian_utils.py:1357`).
- Measured on the live vault, 2026-10-03: 2,937 notes, a no-op `ensure_index` sync of 199 ms, an FTS search of 18 ms, a 65 MB DB.

## Components

### PR 1: Foundation

**`indexed_folders(config) -> list[str]`** in `hooks/obsidian_utils.py`, next to `load_config`. Returns `[sessions_folder, insights_folder, wiki_folder]` from config with the defaults applied, in that order, with duplicates removed. An empty `wiki_folder` turns the wiki folder off; an invalid one (not a string, absolute, `~`, `..` or a dot segment) is dropped with a stderr warning. Every skill and script call site uses it, inside the skills' existing python snippets. `build_context_brief` (`hooks/obsidian_utils.py:5145`) and `deep_analysis_pipeline` (`hooks/open_item_dedup.py:1279`) keep explicit lists, because they receive their folders as parameters rather than a config. That is safe: `_sync` deletes only rows under the folders it scans, so those calls never remove wiki rows; they only skip refreshing them. The exception is `ensure_index` recreating a corrupt or pre-body-column DB from only the folders it was given; the next helper-driven sync re-adds the wiki rows. `rebuild_index` prunes rows outside its scanned folders, so both production callers (`/vault-reindex`, `/obsidian-setup`) use `indexed_folders(load_config(fresh=True), strict=True)`: they refuse an invalid `wiki_folder` instead of dropping its rows, and ignore a stale session config cache. Preserve mode reports the pruned rows as `foreign_deleted`.

The two `open_item_dedup` filesystem scans (`:1292`, `:1743`) look for open `- [ ]` items in sessions and insights. Wiki pages carry no open items, so those two scans keep the sessions and insights folders only. The indexer call at `:1281` keeps its explicit list too (see above); a code comment says why.

**`wiki_folder` config key.** Default `claude-wiki` in `_DEFAULTS` (`hooks/obsidian_utils.py:2229`). `/obsidian-setup` Step 5 creates the folder and Step 7 writes the key. `/vault-config` lists it as an editable folder setting. An existing config without the key gets the default from `_DEFAULTS`, so no migration is needed.

**Indexer exclusion.** `_sync` skips any note whose frontmatter `type` is in `vault_index._UNINDEXED_TYPES` (`{"claude-wiki-index"}`), counts it as `excluded`, and deletes its row if it was indexed before. PR 2 writes `type: claude-wiki-index` into every index and log file. A user note named `index.md` is indexed as usual because it lacks the type. (Changed in PR 1 from a filename rule: no signature change to `_sync`/`ensure_index`/`rebuild_index`, and no name collisions.)

**`claude-wiki` type weight.** Added to all 5 contexts in `_TYPE_SCORES_BY_CONTEXT` at the same weight as `claude-insight`. `tests/test_type_scores.py` gets `claude-wiki` in its pinned set. No extra ranking boost in PR 1; the wiki-first path in PR 2 is what makes pages win.

**Guard test.** `tests/test_indexed_folders.py` finds every `ensure_index(`/`rebuild_index(` call in `skills/*/SKILL.md`, `hooks/*.py`, `scripts/*.py` and `scripts/*.sh`, parses its second argument, and fails unless it is an `indexed_folders(...)` call or a name assigned from one. `hooks/obsidian_utils.py`, `hooks/open_item_dedup.py` and `hooks/vault_index.py` (the definition site) are allow-listed by name with a reason. `scripts/dev-test/` and `docs/` are not scanned. Three positive controls prove the guard flags a literal list and a named list and accepts the helper.

### PR 2: Wiki core

**`hooks/wiki.py`** (new; stdlib only; inside the coverage gate). Library functions plus a CLI, `python3 hooks/wiki.py <subcommand>`. Every subcommand reads JSON on stdin where it takes input (capped at 1,000,000 characters; oversized input is refused, not truncated) and prints JSON on stdout. Exit 0 is success, 1 is a refusal with an `ERROR: <reason>` line on stderr, 2 is a crash.

| Subcommand | Input | Does | Output |
|---|---|---|---|
| `rule` | none | Prints the writing rule (see "Writing rule") | `{"rule": "..."}` |
| `lookup` | `{"question"}` | FTS query over indexed notes with `type='claude-wiki'`, matching on `title` (which holds the question). Returns the top 3 by FTS rank. | `{"candidates": [{"path", "question", "score", "updated"}]}` |
| `stale` | `{"page"}` | Re-hashes the page's sources and runs the newer-note check | `{"stale": bool, "reasons": [...]}` |
| `count` | `{"sources", "memory_sources"}` | Resolves and counts qualifying sources (see "Threshold") | `{"count": N, "qualifying": [...], "rejected": [...]}` |
| `file` | page payload (below) | Validates, scrubs, writes the page, rebuilds the index, appends the log | `{"path", "action": "file"\|"update"\|"file-auto"}` |

`file` payload: `question`, `body` (markdown, already written under the rule), `sources` (note basenames), `memory_sources` (PR 3; empty before), `projects`, `topics`, `confidence`, `filed_by` (`user` or `auto`), `caller` (required when `filed_by` is `auto`), optional `update` (an existing page path), and optional `override_reviewed` (bool, default false). Without `update`, `file` refuses when the target path exists.

`file` refuses (exit 1) when: the count is below 3; a source does not resolve to an existing file under an indexed folder; `confidence` is not one of `high`, `medium`, `low`; `filed_by` is `auto` without a `caller`; `update` points outside `<vault>/<wiki_folder>/queries/` or at a file whose frontmatter `type` is not `claude-wiki`; the resolved write path fails `is_relative_to(<vault>/<wiki_folder>)`; `update` targets a page with `reviewed: true` and the payload does not carry `override_reviewed: true` (see "Reviewed pages").

**vault-ask Step 2b: wiki first.** After parsing the question, run `wiki.py lookup`. The model judges whether the top candidate asks the same question (not a fixed similarity threshold: there is no live data to calibrate one). If it does:

- Run `wiki.py stale`. Fresh: read the page, present its answer, cite the page, and say it came from the wiki with its `updated` date. Stop after Step 8's display (no filing prompt).
- Stale and `reviewed: true`: see "Reviewed pages".
- Stale: carry on through Steps 3-7, adding the page's sources to `CANDIDATE_FILES`. Step 8 then rewrites the page with `update` set, without asking (Decision 3), and tells the user the page was refreshed and why (the `reasons` from `stale`).

**Candidate cap.** In Steps 3-5, at most 3 `claude-wiki` notes stay in `CANDIDATE_FILES`; extra wiki hits are dropped before ranking. This stops a large wiki from crowding raw notes out of the top 10.

**vault-ask Step 8: filing gate.** After displaying the answer:

1. Run `wiki.py count` on the sources cited in the answer. Below 3: write nothing, say nothing.
2. Stale refresh (from Step 2b): file with `update`, no prompt.
3. Invoked with `--caller <name>`: run `lookup`. If the model judges a candidate to be the same question, file with `update`; else file new. `filed_by: auto`. Print one line naming the page filed or updated.
4. User-typed `/vault-ask` with no `--caller`: ask with AskUserQuestion. Options: "Save as a new wiki page", "Update existing page [[...]]" (only when `lookup` found a same-question candidate), "Skip". Skip writes nothing.

To build the page body, fetch `wiki.py rule` and rewrite the answer under it. The chat answer keeps its normal style; only the page uses the rule.

**`--caller` parsing.** `/vault-ask --caller <name> <question>`. `<name>` must match `^[a-z0-9][a-z0-9_.-]{0,63}$`; anything else is treated as part of the question and the run behaves as user-typed. This is a skill-level argument and is unrelated to `search_vault`'s existing `caller` parameter.

### PR 3: Memory sources and doctor

**`memory_sources(host, config) -> list[Path]`** in a new `hooks/memory_sources.py`. On host `claude-code` it returns `~/.claude/projects/*/memory/*.md` (not `MEMORY.md`, which is an index), each resolved and checked with `is_relative_to(~/.claude/projects)`, skipping symlinks. On any other host it returns `[]` until #272 adds Codex. `detect_host()` in the same module returns `codex` when any `_CODEX_HOST_MARKERS` env var is set, else `claude-code`. This is the one place a Claude-only path lives.

**Memory search in vault-ask.** Step 4 gains a fourth search: `wiki.py memgrep --pattern <term>` greps the files from `memory_sources` and prints matching paths. Hits join `CANDIDATE_FILES` with type `claude-memory`. They are cited in the answer and on the page as plain text, `memory: <project-dir>/<file>.md`, never as wikilinks. The FTS fast path in Step 3 does not see memory files; Step 3's "5+ results skips Step 4" rule changes so the memory grep always runs.

**`wiki-pages` vault-doctor check.** New `scripts/vault_doctor_checks/wiki_pages.py`, in the default sweep. It reads `wiki_folder` from config (the doctor `scan` signature passes sessions and insights only). Issues:

| Reason | Fix on `--apply` |
|---|---|
| `stale` (same rules as `wiki.py stale`) | none; reported for the next `/vault-ask` to refresh |
| `broken-source` (a cited source is missing) | none; reported |
| `orphan` (no inbound wikilink from any vault note other than the wiki's own index and log files) | none; reported |
| `index-drift` (the index files do not match a fresh rebuild) | rebuild the index files |
| `auto-filed` (`filed_by: auto`, listed for review) | none; reported |
| `reviewed-stale` (`reviewed: true` and stale; never auto-refreshed) | none; reported so the user can re-check their edits |

The orphan scan respects `--days` like the other checks. Each reason has a positive and a negative fixture, and each detector is mutation-tested on its own.

**Global CLAUDE.md.** The vault-ask rule in `~/.claude/CLAUDE.md` changes to tell callers to run `/vault-ask --caller <workflow-name> <question>`. That file is outside this repo; the PR body records the exact diff applied.

## Data model

**Page path:** `<vault>/<wiki_folder>/queries/YYYY/MM-DD-<slug>.md`. The slug is the question lowercased, non-alphanumerics collapsed to `-`, trimmed to 60 characters at a word boundary. On collision `-2`, `-3`, and so on are appended. Year folders keep any one directory small.

**Frontmatter:**

```yaml
---
type: claude-wiki
title: "<question, verbatim>"
question: "<question, verbatim>"
projects: [obsidian-brain]
tags: [claude/wiki, claude/project/obsidian-brain, claude/topic/<t>]
sources: ["[[note-a]]", "[[note-b]]"]
memory_sources: ["obsidian-brain/feedback_x.md"]
sources_fingerprint: {"note-a": "<sha256[:16]>", "memory:obsidian-brain/feedback_x.md": "<sha256[:16]>"}
confidence: high
created: 2026-10-03
updated: 2026-10-03
filed_by: user
caller: <name>
reviewed: true
---
```

`title` duplicates `question` so the FTS `title` column holds the question for `lookup`. `caller` is present only when `filed_by: auto`. `reviewed` is absent on pages the wiki writes; the user adds `reviewed: true` by hand after editing a page. The value is read leniently, because a miss would overwrite the user's edits: after stripping whitespace and quotes and lowercasing, `true`, `yes`, `on` and `1` all mean reviewed. Absent, `false`, `no`, `off`, `0` or empty means not reviewed. Any other value also counts as reviewed (fail closed toward keeping edits). `projects` and the project tags come from the `project:` field of the cited notes, deduplicated and sorted. `confidence` maps from vault-ask's certainty wording: "You explicitly decided" is `high`, "it appears" is `medium`, "Limited context" is `low`.

**Body:** the answer rewritten under the writing rule, then a `### Sources` section with one line per source, as vault-ask writes it today. `scrub_secrets` runs on the body and on `question` before writing.

**Fingerprint:** SHA-256 of each source file's bytes, first 16 hex characters. A content hash, not mtime: `/recall` and `/check-items` change mtimes without changing meaning, and mtime would trigger false refreshes.

**Stale** means any one of:

1. a source's hash differs from `sources_fingerprint`;
2. a source file no longer exists;
3. a note dated after the page's `updated` is in the top 5 FTS hits for the question and is not already a source.

`stale` returns every reason that fired, not only the first.

**Threshold:** `count` counts distinct sources of type `claude-insight`, `claude-error-fix`, `claude-decision`, `claude-retro`, `claude-session`, `claude-memory` (migrated memory notes already in the vault), plus memory files (PR 3). A `claude-snapshot` counts as its parent session (resolved via `source_session_note`), so a snapshot and its parent count once. Other types (`claude-wiki`, `claude-standup`, `claude-emerge`, `claude-stats`, `claude-check-items-report`) do not count. The gate is `count >= 3`. The count is computed by code from the cited source list, so the skill cannot argue past it.

**Index files:**

- Each index file starts with a frontmatter block carrying `type: claude-wiki-index`, so the indexer skips it (PR 1).
- Up to 500 pages: one `<wiki_folder>/index.md`, one line per page, `- [[<slug>]] — <question> (updated <date>, <confidence>[, reviewed])`, sorted by `updated` descending.
- Above 500 pages: `index.md` lists one line per project linking to `index-<project>.md`, and each project file lists its pages. A page with several projects appears in each.
- Rebuilt in full on every write, from the SQLite `notes` table (`type='claude-wiki'`), not by reading page files. A full rebuild cannot drift, and reading the table costs one query.
- `lookup` never reads the index files. They exist for people browsing in Obsidian.

**Log files:** `<wiki_folder>/log-YYYY.md`, append-only, one file per calendar year (UTC). Each log file starts with a frontmatter block carrying `type: claude-wiki-index`; entries are appended below it. Each entry:

```markdown
## [2026-10-03] file-auto | <caller> | <question>
- Created: [[<slug>]]
```

The operation is `file`, `update` or `file-auto`; `<caller>` is `-` for user filings. `update` entries use `- Updated:`.

## Writing rule (Decision 8)

One constant, `WRITING_RULE` in `hooks/wiki.py`, printed by `wiki.py rule`. It tells the model to write "80% of the way to ASD-STE100":

- short sentences: at most 20 words in procedures, 25 in descriptions;
- one topic per sentence; active voice;
- one meaning per word, the same term used throughout; no filler words;
- technical terms, names, paths, flags, numbers and quoted text stay exact, and no STE word swap may lose information;
- citations and `[[wikilinks]]` are never reworded.

Diagram, HTML and video page formats are out of scope. A page may contain a Mermaid diagram when the synthesis needs one.

## Reviewed pages

Pages are LLM-owned by default. A user who edits a page by hand marks it `reviewed: true` in its frontmatter. That flag protects the edits:

- **No automatic rewrite, ever.** `file` refuses an `update` of a reviewed page unless the payload carries `override_reviewed: true`. Stale refresh and `--caller` auto-filing never set it.
- **Stale reviewed page, user-typed `/vault-ask`:** answer from the page, then warn that its sources changed, with the `stale` reasons. Ask with AskUserQuestion: "Keep my page" (default; writes nothing), "Refresh and overwrite my edits" (files with `update` and `override_reviewed: true`; the rewritten page drops the flag), or "Save the fresh answer as a new page" (files new; the reviewed page is untouched).
- **Stale reviewed page, `--caller` run:** answer from the page, add one line saying it is reviewed and stale with the reasons, and write nothing.
- **Dedupe:** when `--caller` auto-filing matches a reviewed page, it does not update it. It writes nothing and names the reviewed page in its output line, so repeated research does not fork the page into near-copies.
- **Doctor:** the `reviewed-stale` reason lists reviewed pages whose sources changed.
- **Fingerprint:** unchanged by the flag. A hand edit changes the page, not its sources, so it never marks the page stale by itself.

## Error handling

- A failed write never loses the answer: the chat answer is displayed before Step 8.
- Write order: page, then index files, then log, each atomic (temp file in the same directory, `0o600`, `os.rename`). If the index or log write fails after the page is written, `file` exits 1 and names what is missing. The next write repairs the index (full rebuild), and the doctor's `index-drift` reason reports it in between.
- A lock file `<wiki_folder>/.wiki.lock`, using the same acquire pattern as `note_writer._acquire_lock`, wraps page write + index rebuild + log append, so two auto-filing sub-agents cannot interleave log entries or rebuild the index on top of each other. Lock timeout: refuse with exit 1, never write without the lock.
- Folder names go through `note_writer._validate_folder`. Every write path is resolved and checked with `is_relative_to(<vault>/<wiki_folder>)`. Memory paths are checked with `is_relative_to(~/.claude/projects)`.
- Every refusal prints `ERROR: <reason>` and the skill shows it. A refusal is never reported as "filed".
- A stale check that cannot read a source reports it as missing (reason 2), never as fresh.
- A stale refresh whose new answer cites fewer than 3 qualifying sources does not rewrite the page. The skill tells the user the page could not be refreshed and why, the page stays as it was, and the doctor keeps reporting it as `stale`.

## Scale

| Load | Behaviour | Notes |
|---|---|---|
| 10k vault notes | `ensure_index` sync about 0.7 s per ask, FTS search tens of ms, DB about 220 MB | Linear estimates from the 2026-10-03 measurement (68 µs per note sync, 22 KB per note). Sync cost predates #383; the wiki adds one folder. Known limit, not fixed here. |
| 10k wiki pages | `lookup` is one FTS query; `file` is one page write plus one table query for the index | No per-page file reads on any hot path |
| Index files | Split by project above 500 pages | Karpathy reports one index works up to about hundreds of pages |
| Log | One file per year | Bounded per file |
| Ranking | At most 3 wiki notes in vault-ask candidates | Keeps raw notes in the top 10 |
| Doctor orphan scan | O(vault notes), windowed by `--days` | Batch job |

Growth is bounded by the 3-source threshold and by dedupe: a repeat question updates its page.

## Testing

`tests/test_wiki.py`, `tests/test_indexed_folders.py`, `tests/test_memory_sources.py`, `tests/test_vault_doctor_wiki_pages.py`, plus additions to `tests/test_type_scores.py` and the vault-ask skill-text tests.

- **Threshold boundary:** exactly 2 and exactly 3 sources; a snapshot plus its parent counted once; duplicate cites collapsed; non-counting types ignored.
- **Refusals, both directions:** each refusal fires on its bad input, and the matching good input writes a page byte-identical to a golden file. Each guard is mutation-tested alone (with `PYTHONDONTWRITEBYTECODE=1`), and the new tests are run against the parent commit to show they fail there.
- **Staleness:** each of the 3 triggers fires alone; an mtime-only touch does not mark a page stale; an unreadable source reports missing.
- **Index and log:** the index rebuilds after a page is deleted by hand; the split at 501 pages; the log is append-only across 2 writes and rolls at a year boundary (Dec 31 to Jan 1 UTC); index and log files are not in FTS; a session note named `index.md` elsewhere still is.
- **Lock:** a held lock makes `file` refuse with exit 1 and leaves no partial page.
- **Reviewed pages:** `update` of a reviewed page refuses without `override_reviewed` and succeeds with it; stale refresh and `--caller` filing never pass it (skill-text test); a `--caller` dedupe hit on a reviewed page writes nothing; `"true "`, `yes`, `On`, `1` and an unknown value such as `maybe` all count as reviewed, while absent, `false` and `no` do not; the doctor's `reviewed-stale` fires only when both conditions hold.
- **`indexed_folders`:** the literal-list grep guard; the helper's order and dedupe; a config without `wiki_folder` gets the default.
- **Secrets:** a fake token in `body` and in `question` comes out redacted on disk.
- **Writing rule:** `rule` output carries the key phrases; the vault-ask skill text tells the model to fetch `wiki.py rule` before writing a page.
- **Forced failure:** each CLI subcommand run on a built-to-fail input, and with its input unreadable, must exit non-zero.
- **Dogfood:** PR 1 reindexes a scratch copy of the live vault and DB (live DB checksum unchanged before and after). PR 2 runs a real `/vault-ask` through the skill on a scratch vault copy in user-typed and `--caller` modes, then a repeat ask that hits the page. PR 3 runs the doctor over that scratch copy.

## Interaction with #377

#377's design moves vault-ask's Step 5 rule table into a script and adds a citation log. Both #377 and this design edit vault-ask Steps 5 and 8. Whichever lands second rebases its skill edits onto the other. The candidate cap (at most 3 wiki notes) is applied before Step 5's ranking in either version.

## Out of scope

- Speeding up `ensure_index`'s per-ask sync.
- Codex memory sources (#272, v3.11).
- Diagram, HTML or video page formats.
- An `obsidian-wiki` page-format convergence beyond what Decision 4 already takes.

## Acceptance criteria

The issue's 12 criteria apply. This spec maps them to PRs: PR 1 covers C.6 in part (indexing through one helper, index files not indexed) and C.12 for its components; PR 2 covers C.1, C.2, C.4, C.5, C.6 (search returns pages), C.8, C.10, C.11 and C.12 for its components; PR 3 covers C.3, C.7, C.9 and C.12 for its components.
