# Plan: `/memory-consolidate` moves memory facts into the vault (#309)

## Design (settled 2026-11-24)

The user picked the second manual run's design (issue comment 7142778361) over the issue's
first proposal. The issue body's `## Acceptance Criteria` holds the 11 criteria this plan builds.

- Two modes, one pipeline. **Shrink**: the index is near its limit and every entry is live.
  **Rescue**: orphans exist. Both produce the same move plan.
- Judgement stays in the skill, mechanics go in a stdlib CLI. Claude reads entries and writes
  a plan JSON (keep/move test, note type, dedupe hit). The CLI audits, applies and verifies.
- One vault note per fact, body copied word for word. A dedupe hit appends to the existing note.
- No `source_session` on moved notes. Checked 2026-11-24: `gather_session_evidence`
  (`hooks/obsidian_utils.py`) selects insights by `source_session` in the session's id set,
  so a moved note with the moving session's id counts as that session's finding (#495).
  Moved notes carry `migrated_from_memory: <slug>` and `synced_from: <source path>`.
  No code reads `synced_from` or `migrated_from_memory` today (grep of hooks/, scripts/, skills/).
- Originals move to a backup dir. Nothing is deleted.
- Markers: frontmatter `migrated_from_memory:` plus tag `claude/topic/moved-from-memory`.
  Earlier hand runs used four markers; this one is the only one the CLI writes or reads.

### Plan JSON (the contract between skill and CLI)

```json
{
  "version": 1,
  "store": "/abs/path/to/memory",
  "items": [
    {"slug": "feedback_x", "action": "new", "type": "claude-insight", "topic": "testing"},
    {"slug": "feedback_y", "action": "append", "target": "claude-insights/2026-11-03-foo-0d18.md"},
    {"slug": "project_z", "action": "retire", "reason": "PR merged; git holds it"}
  ]
}
```

- `slug`: a file stem in the store, `[A-Za-z0-9][A-Za-z0-9_.-]{0,200}`, not `MEMORY`. Anything
  else fails closed (exit 2, nothing written).
- `action`: exactly `new`, `append` or `retire`.
- `type` (`new` only): `claude-insight` (default), `claude-error-fix` or `claude-decision`.
  The tag is `claude/insight`, `claude/error-fix` or `claude/decision`.
- `topic` (`new` only, optional): kebab-case `[a-z0-9-]{1,40}`; default comes from the slug.
- `target` (`append` only): a vault-relative path to an existing `.md` file. Resolved and checked
  with `resolve()` + `is_relative_to(vault_root)`; a symlink or a path outside the vault fails closed.
- Duplicate slugs, unknown keys and missing required keys fail closed.

### Index editing rule

Parse `MEMORY.md` with the shared link parser (Task 1). For each line, the store entries it
references are its markdown links, wikilinks and bare mentions that name a real store file.
- A line whose referenced entries are all moving is dropped.
- A line that references a moving entry **and** an entry that is not moving is not edited. Those
  moving slugs are refused (reported, not moved), so the index never points at a moved file.
- Lines with no store reference (headings, prose, the pointer line) are kept.
- After the drops, one pointer line is added after the last kept index line:
  `- Moved to vault (<date>): <n> entries tagged claude/topic/moved-from-memory; find them with /vault-ask; originals in <backup dir>`.
- Mentions of a moving slug inside other memory files' bodies are reported as warnings only.

### Write order (apply)

1. Validate the plan and every entry (file exists, frontmatter parses, `scrub_secrets(text) == text`,
   index rule above). Refused entries are dropped from this run and listed.
2. Write each `new` note (`write_vault_note`, refuse to overwrite) and each `append`
   (lock + atomic rewrite via `note_writer`). Read each back: the marker is present and the body
   is non-empty. An entry whose read-back fails is not moved.
3. Make the backup dir `~/.claude/backups/memory-moved-<YYYYMMDD-HHMMSS>-<store-dir-name>/` (0o700,
   each level made private), refuse if it exists, copy the whole store to `full-copy/`.
4. Move each written or `retire` original to `moved/<slug>.md`.
5. Re-read the live `MEMORY.md`, apply the index rule, write it atomically (temp + rename, 0o600).
6. Write `run.json` in the backup dir: the plan, the refused list, the old index link set, the
   moved set and the sizes. `verify` reads this.

A rerun with the same plan finds a note already marked for a slug and skips the write for it
(re-reading it back instead), so an interrupted run can finish without duplicate notes.

## Tasks

### Task 1: share the memory-index link parser

Delegate: codex — moves code between two files and adds one test file; `Check:` is the existing memory_index suite plus the new one.

- New `hooks/memory_links.py`: move `_MD_LINK_RE`, `_WIKILINK_RE`, `_BARE_MD_RE`,
  `_link_targets()` and `_md_child_link_targets()` out of
  `scripts/vault_doctor_checks/memory_index.py` unchanged (with their comments). Add
  `line_refs(line: str, store_names: set[str]) -> set[str]`: the `.md` names from
  `_link_targets(line)` that are in `store_names`.
- `memory_index.py` imports them from `memory_links` (hooks dir put on `sys.path` the way
  `project_name_normalization.py` does), so `mi._link_targets` etc. still resolve.
- Tests `tests/test_memory_links.py`: `line_refs` on a one-link line, a multi-link line
  (`- Fixtures: [a](a.md) · [b](b.md)`), a wikilink line, a bare mention, a link to a non-store
  file (ignored), and a URL ending in `.md` (ignored by `_md_child_link_targets`).
- Check: `python3 -m pytest -q tests/test_vault_doctor_memory_index.py tests/test_memory_links.py`

### Task 2: `audit` and plan validation

Delegate: codex — one new module and one test file; functions, exit codes and fixtures named below.

- New `hooks/memory_consolidate.py` (stdlib only). Functions:
  - `default_store(cwd) -> Path`: the Claude Code store for the main worktree of `cwd`
    (`git rev-parse --path-format=absolute --git-common-dir`, its parent; else `cwd`), encoded
    as `~/.claude/projects/<path with every non-alphanumeric char replaced by '-'>/memory`.
  - `audit(store: Path) -> dict`: `store`, `index` (`bytes`, `chars`, `lines`, `soft_limit_bytes`
    17000, `hard_limit_bytes` 24000, `line_limit` 200, `over`: list of limit names hit),
    `entries` (slug, memory type from frontmatter, body chars, mtime ISO, `indexed` bool),
    `orphans`, `dangling` (markdown links to missing direct children), `duplicates`
    (an entry referenced by more than one index line), `mode` (`rescue` if orphans else
    `shrink` if any limit hit else `ok`).
  - `load_plan(path, store, vault_root) -> (items, errors)` enforcing every rule in
    "Plan JSON" above.
  - `index_edit(index_text, store_names, moving: set) -> (new_text, dropped_lines, refused_slugs)`
    implementing "Index editing rule".
- CLI: `python3 hooks/memory_consolidate.py audit [--store PATH] [--json]`. Always prints
  `store: <path>` first. Exit 0 ok, 1 a limit hit or drift found, 2 store unreadable or bad args.
- Tests `tests/test_memory_consolidate_audit.py` with `HOME` at `tmp_path`: shrink fixture
  (0 orphans, index over 17,000 bytes), rescue fixture (orphans), a fixture shaped like this
  repo's `MEMORY.md` (multi-link lines, `[[wikilinks]]`, `([[slug]])` asides), dangling and
  duplicate rows, the exact-boundary size (16,999 vs 17,000 bytes), each plan rule failing
  closed, `index_edit` refusing a mixed line and keeping a heading.
- Check: `python3 -m pytest -q tests/test_memory_consolidate_audit.py`

### Task 3: `apply` (dry run and `--apply`)

Delegate: claude — the destructive path; writes to the vault, the store and the backup root, with ordering and rerun rules that span modules.

- `apply(plan_path, store, vault_root, insights_folder, project, backup_root, apply: bool, now)`
  following "Write order". Dry run prints one line per item (`NEW <slug> -> <file>`,
  `APPEND <slug> -> <target> (+N chars)`, `RETIRE <slug>`, `REFUSE <slug>: <reason>`) and the
  index lines it would drop, and touches nothing.
- New note: filename `<date>-<slugify(title, 50)>-<sha1(slug)[:4]>.md`; title is the index line's
  link text, else the entry's `name:`/`description:` frontmatter, else the slug. Frontmatter
  exactly: `type`, `date`, `created_at` (aware ISO), `project` (`canonical_project_name`),
  `migrated_from_memory`, `synced_from`, `memory_type`, `status: active`, `tags` (type tag,
  `claude/project/<project>`, `claude/topic/moved-from-memory`, `claude/topic/<topic>`).
  No `source_session` or `source_session_note`. Body: `# <title>`, a one-line provenance quote
  naming the backup path, `## Summary` (the `description:`), `## Details` (the memory body, verbatim).
- Append: `## From project memory: <title> (moved <date>)` then the same provenance, summary and
  details, through `note_writer._acquire_lock` + `_atomic_rewrite`.
- CLI: `apply --plan PATH [--store PATH] [--apply]`; vault path and insights folder come from
  `load_config(fresh=True)`, overridable with `--vault`, `--backup-root` for tests.
  Exit 0 all done, 1 some items refused (the rest done), 2 bad plan or nothing could be done.
- Tests `tests/test_memory_consolidate_apply.py`: dry run leaves vault, store and backup root
  byte-identical (hash the trees); a written note read back has every field and no
  `source_session`; `gather_session_evidence` with the moving session's id does not return it;
  append leaves the rest of the target byte-identical; an append target outside the vault or a
  symlink is refused; a secret-shaped body is refused; a line appended to `MEMORY.md` between
  load and index write survives; a mixed index line refuses its slug; a write failure for one
  item leaves that original in place; backup dir reuse is refused; a rerun after an interrupted
  run makes no duplicate note.
- Check: `python3 -m pytest -q tests/test_memory_consolidate_apply.py`

### Task 4: `verify` and the reindex smoke test

Delegate: codex — one function group in the module from Task 2 plus one test file; the checks are listed one by one.

- `verify(backup_dir, store, vault_root, insights_folder) -> (ok, report)` reading `run.json`:
  1. every markdown index link names an existing file;
  2. no entry is referenced by two index lines;
  3. every `*.md` left in the store (not `MEMORY.md`) is referenced by the index;
  4. old indexed link set minus new link set equals the moved set (indexed entries only;
     orphans and `retire` items are excluded from both sides);
  5. each `new`/`append` slug has a vault note with `migrated_from_memory: <slug>` (new) or the
     `## From project memory:` section naming its backup path (append), with a body over 50 chars;
  6. each moved or retired slug exists in `moved/`;
  7. sizes before and after, in bytes, characters and lines, labelled.
- `smoke(vault_root, folders, db_path, query) -> int`: `ensure_index(vault_root, folders, db_path)`
  then `search_vault(db_path, query, log_access=False)`; returns the hit count.
- CLI: `verify --backup DIR [--store PATH]` (exit 0 pass, 1 any check failed, 2 unreadable),
  `smoke --query TEXT [--db PATH]` (exit 0 at least one hit, 1 none).
- Tests `tests/test_memory_consolidate_verify.py`: a clean run passes; one fixture per check 1–6
  that makes only that check fail and exit 1; `smoke` against a scratch DB finds a written note
  and returns 1 for a query with no hits.
- Check: `python3 -m pytest -q tests/test_memory_consolidate_verify.py`

### Task 5: `skills/memory-consolidate/SKILL.md`

Delegate: claude — skill prose and its contract test; judgement steps a brief cannot check.

- Steps: (1) `audit` and show the report, store path first; (2) read each candidate and apply the
  keep/move test, word for word from the issue comment: keep deploy and live-service rules,
  approval limits, hook and preflight mechanics and daily tool gotchas; move one-off design
  lessons, general testing lessons and anything global `CLAUDE.md` already says; stale
  point-in-time state is `retire`; (3) dedupe: `/vault-search` per candidate, and a hit counts
  only when the matching note is about that lesson, not a retro that mentions it; (4) write the
  plan JSON with the Write tool (no heredoc; #524); (5) `apply` dry run, show it, ask the user;
  (6) `apply --apply`; (7) `verify`; (8) `smoke` with a phrase from a moved fact; (9) report sizes
  with units and the backup path. Hooks dir found with the `_ob_hooks()` block the other skills use.
- Contract test `tests/test_memory_consolidate_skill_contract.py`: the skill names each CLI
  subcommand, says dry run first, states the keep/move test and the dedupe rule, and has no
  heredoc and no `source_session`.
- Check: `python3 -m pytest -q tests/test_memory_consolidate_skill_contract.py tests/test_skill_shell_blocks.py tests/test_skill_snippets.py`

### Task 6: docs and registration

Delegate: claude — architecture.json and README edits with render and smoke scripts.

- `docs/architecture/architecture.json`: add the skill and `hooks/memory_consolidate.py` and
  `hooks/memory_links.py` components, the memory-store → vault flow and the backup datastore;
  `lastUpdated` 2026-11-24; re-render the HTML; run the smoke test (`[main]` and `[sparse]`).
- README skills table row; CHANGELOG `Unreleased` entry.
- Check: `~/.claude/skills-archive/architecture-page/scripts/smoke-test.sh --json docs/architecture/architecture.json`

### Task 7: dogfood on a scratch copy

Delegate: claude — reads the live store and vault (copied to scratch), outside any one brief.

- Copy this project's live memory store and the vault's insights folder to scratch. Point HOME,
  the vault (scratch config), the backup root and the DB at scratch. Run the skill's steps on a
  plan of about 10 real entries (new, append, retire, one mixed-line refusal). Run `verify`,
  `smoke`, then `vault_doctor.py` on the scratch vault (frontmatter fence, project name,
  spurious wikilinks). Record what each printed.

## Failure modes

- **Fail-open paths:** a plan item that fails validation must refuse, never default (Task 2 tests
  each rule). A write or read-back failure must leave the original in place (Task 3). `verify`
  must exit 1 on any failed check (Task 4, one fixture per check). `smoke` with no hits exits 1.
- **Exit codes:** audit 0/1/2, apply 0/1/2, verify 0/1/2, smoke 0/1. Each branch has a test that
  triggers it and checks `returncode`.
- **Date boundaries:** `date` is local, `created_at` is aware ISO with offset; the backup dir
  has a time to the second, so two runs a day get two dirs; a test freezes `now` near midnight.
- **Doc counts:** README and architecture counts come from the diff, not memory.
- **Broken Markdown:** the index rewrite keeps headings and blank lines; a test diff-checks a
  fixture index before and after a one-entry move (only that line and the pointer line change).
- **Unrelated changes:** Task 1 moves code without editing it; `git diff --stat` on
  `memory_index.py` shows only the import swap.
- **Inputs:** reads the store's `MEMORY.md` and `*.md` (the live files are the source of truth,
  re-read at index-write time), the plan JSON (written by the skill), the obsidian-brain config
  (`load_config(fresh=True)`), and vault notes for append targets. Tests: missing store, missing
  `MEMORY.md`, unreadable entry, plan naming a missing slug, append target deleted between dry run
  and apply.
- **Bypass routes:** the destructive step is the move. Cheapest ways it could run unverified:
  (a) a plan item with `action: new` whose note write silently failed — blocked by read-back
  before move; (b) a `retire` used to skip the vault note for a durable entry — allowed by design,
  so the dry run lists every `RETIRE` and the skill asks first; (c) a `target` that resolves
  outside the vault via `..` or a symlink — refused; (d) an index line linking a moving and a
  staying entry — refused, not edited; (e) a stale `run.json` from another run — `verify` takes
  an explicit `--backup` dir and checks its `store` matches.
