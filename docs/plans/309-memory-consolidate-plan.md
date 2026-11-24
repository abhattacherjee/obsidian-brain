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
- `reason` (`retire` only): required, non-empty.
- `store` must equal the resolved `--store` (or default store); a mismatch fails closed.
- Duplicate slugs, unknown keys and missing required keys fail closed.

### Index editing rule

Parse `MEMORY.md` with the shared link parser (Task 1). A line's **pointers** are its markdown
links to direct children of the store (`_md_child_link_targets`); its **asides** are wikilinks
and bare mentions of store files that are not also pointers (`_link_targets` minus pointers).

- A line whose pointers are all moving is dropped.
- A line whose pointers include a moving entry **and** a staying entry is not edited. Those
  moving slugs are refused (reported, not moved), so the index never points at a moved file.
  Measured 2026-11-24 on this repo's `MEMORY.md`: 29 of 122 lines carry more than one pointer,
  so the skill must plan grouped entries as a group. The control-tower store had 0 such lines.
- An aside naming a moving slug on a staying line is a warning, not a refusal (6 aside-only
  references on this repo's index). The aside text is left as it is.
- Lines with no pointer (headings, prose, an earlier pointer line) are kept.
- After the drops, one pointer line is added after the last kept index line:
  `- Moved to vault (<date>): <n> entries tagged claude/topic/moved-from-memory; find them with /vault-ask; originals in <backup dir>`.
- Mentions of a moving slug inside other memory files' bodies are warnings only.

### Store selection

`--store PATH` or the default from cwd. The default reuses `obsidian_utils._cc_encoded_dirnames`
on the main worktree root (`git rev-parse --path-format=absolute --git-common-dir`, its parent;
else cwd) under `memory_sources._projects_root()`, and picks the candidate whose `memory/`
exists. The resolved store must be `<projects root>/<one dir>/memory`, not a symlink, and every
entry a regular non-symlink file; otherwise exit 2. A computed store that does not exist is exit 2.
Every command prints `store: <path>` first.

### Markers (run-independent, so a rerun can find them)

- `new` note: frontmatter `migrated_from_memory: <slug>`.
- `append`: the section heading `## From project memory: <title> (moved <date>)` followed by the
  line `<!-- migrated_from_memory: <slug> -->`. A rerun searches the target for that comment line.

### Lossless check

The note's `## Details` text (new) or the appended section's details text must equal the memory
file's body (frontmatter stripped, then `.strip()` on both) exactly. Read-back in apply and
`verify` check 5 both compare against the original (`moved/<slug>.md` once moved). The writer
does no scrub, no wikilink escaping and no line-ending change; `[[slug]]` links in a body are
copied as they are (Obsidian shows them as unresolved links; recorded as a known cost).
`write_vault_note` overwrites, so `new` checks `dest.exists()` right before the write and refuses.

### Write order (apply)

1. Validate the plan and every entry (file exists or is already moved, frontmatter parses,
   `scrub_secrets(text) == text`, index rule). Choose the backup dir
   `<backup root>/memory-moved-<YYYYMMDD-HHMMSS>-<store-dir-name>/` (default backup root
   `~/.claude/backups`) and refuse if it exists. Refused entries are dropped and listed.
2. Write each `new` note and each `append` (lock + `_atomic_rewrite` via `note_writer`). Read each
   back with the lossless check. An entry whose read-back fails is not moved.
3. Create the backup dir (0o700, each new level made private), copy the whole store to
   `full-copy/`, and write `run.json` (schema below) with `"state": "notes-written"`.
4. Move each written or `retire` original to `moved/<slug>.md`; update `run.json` to
   `"state": "moved"`.
5. Re-read the live `MEMORY.md`, apply the index rule, write it with
   `_atomic_rewrite(expect_stat=...)`, retrying the read-edit-write up to 3 times on a stat
   mismatch; update `run.json` to `"state": "done"` with the after sizes.

**Rerun after an interruption.** `apply --resume <backup dir>` reads its `run.json` and finishes
from the recorded state: an entry whose original is gone and whose marked note exists counts as
moved, and its index line is still dropped. A plain rerun with the same plan skips the write for
a slug whose marker is already present (re-reading it back). Tests cover a stop after steps 2,
3, mid-4, 4 and 5.

### `run.json` schema (v1)

```json
{
  "version": 1,
  "state": "notes-written | moved | done",
  "store": "/abs/store",
  "vault": "/abs/vault",
  "insights_folder": "claude-insights",
  "created_at": "2026-11-24T12:00:00-08:00",
  "plan": {"...": "the validated plan"},
  "refused": [{"slug": "x", "reason": "..."}],
  "warnings": [{"slug": "x", "reason": "..."}],
  "written": [{"slug": "x", "action": "new|append", "note": "claude-insights/....md"}],
  "retired": ["slug"],
  "moved": ["slug"],
  "baseline": {"indexed": ["slug"], "dangling": ["name.md"], "duplicates": ["slug"], "orphans": ["slug"]},
  "sizes": {"before": {"bytes": 0, "chars": 0, "lines": 0}, "after": {"bytes": 0, "chars": 0, "lines": 0}}
}
```

Task 4's brief copies these names from the committed Task 3 code, not from this sketch.

## Tasks

### Task 1: share the memory-index link parser

Delegate: codex — moves code between two files and adds one test file; `Check:` is the existing memory_index suite plus the new one.

- New `hooks/memory_links.py`: move `_MD_LINK_RE`, `_WIKILINK_RE`, `_BARE_MD_RE`,
  `_link_targets()` and `_md_child_link_targets()` out of
  `scripts/vault_doctor_checks/memory_index.py` unchanged (with their comments). Add
  `line_pointers(line, store_names) -> set[str]` (`_md_child_link_targets(line) & store_names`)
  and `line_asides(line, store_names) -> set[str]` (`(_link_targets(line) & store_names)` minus
  the pointers).
- `memory_index.py` imports them from `memory_links` (hooks dir put on `sys.path` the way
  `project_name_normalization.py` does), so `mi._link_targets` etc. still resolve.
- Tests `tests/test_memory_links.py`: pointers and asides on a one-link line, a multi-link line
  (`- Fixtures: [a](a.md) · [b](b.md)`), a wikilink-only line, a `([[slug]])` aside after a
  pointer, a bare mention, a link to a non-store file (ignored), and a URL ending in `.md`.
- Check: `python3 -m pytest -q tests/test_vault_doctor_memory_index.py tests/test_memory_links.py`

### Task 2: `audit`, store selection and plan validation

Delegate: codex — one new module and one test file; functions, exit codes and fixtures named below.

- New `hooks/memory_consolidate.py` (stdlib only; capped reads; no raw `sqlite3.connect`).
  Functions:
  - `resolve_store(store_arg: str | None, cwd: str) -> Path` per "Store selection"; raises
    `StoreError` (exit 2) on any refusal.
  - `read_entry(path) -> dict`: `slug`, `name`, `description`, `memory_type`, `body` (frontmatter
    stripped), `text` (whole file), `mtime`. Refuses symlinks and non-regular files.
  - `audit(store: Path) -> dict`: `store`, `index` (`bytes`, `chars`, `lines`, `soft_limit_bytes`
    17000, `hard_limit_bytes` 24000, `line_limit` 200, `over`: list of limit names hit),
    `entries` (slug, memory type, body chars, mtime ISO, `indexed` bool), `orphans`, `dangling`
    (markdown links to missing direct children), `duplicates` (an entry that is a pointer on more
    than one line), `mode` (`rescue` if orphans, else `shrink` if any limit hit, else `ok`).
  - `load_plan(path, store, vault_root) -> (items, errors)` enforcing every rule in "Plan JSON".
  - `index_edit(index_text, store_names, moving: set, pointer_line: str) -> (new_text,
    dropped_lines, refused: dict[slug, reason], warnings: dict[slug, reason])` per "Index editing rule".
- CLI: `python3 hooks/memory_consolidate.py audit [--store PATH] [--json]`. Exit 0 `ok`,
  1 a limit hit or drift found, 2 store refused or unreadable or bad args.
- Tests `tests/test_memory_consolidate_audit.py` with `HOME` at `tmp_path`: shrink fixture
  (0 orphans, index over 17,000 bytes), rescue fixture (orphans), a fixture shaped like this
  repo's `MEMORY.md` (multi-link lines, `[[wikilinks]]`, `([[slug]])` asides), dangling and
  duplicate rows, the exact-boundary size (16,999 vs 17,000 bytes, 199/200/201 lines), each plan
  rule failing closed, a symlinked store and a symlinked entry refused, `index_edit` refusing a
  mixed pointer line, warning on an aside, keeping a heading and blank lines byte-identical.
- Check: `python3 -m pytest -q tests/test_memory_consolidate_audit.py`

### Task 3: `apply` (dry run, `--apply`, `--resume`)

Delegate: claude — the destructive path; writes to the vault, the store and the backup root, with ordering and rerun rules that span modules.

- `apply(plan_path, store, vault_root, insights_folder, project, backup_root, do_apply: bool, now)`
  following "Write order". Dry run prints one line per item (`NEW <slug> -> <file>`,
  `APPEND <slug> -> <target> (+N chars)`, `RETIRE <slug>`, `REFUSE <slug>: <reason>`,
  `WARN <slug>: <reason>`) and the index lines it would drop, and touches nothing.
- New note: filename `<date>-<slugify(title, 50)>-<sha1(slug)[:4]>.md`; title is the index line's
  link text, else the entry's `name:`/`description:` frontmatter, else the slug. Frontmatter
  exactly: `type`, `date`, `created_at` (aware ISO), `project` (`canonical_project_name`),
  `migrated_from_memory`, `synced_from`, `memory_type`, `status: active`, `tags` (type tag,
  `claude/project/<project>`, `claude/topic/moved-from-memory`, `claude/topic/<topic>`).
  No `source_session` or `source_session_note`. Body: `# <title>`, a one-line provenance quote
  naming the backup path, `## Summary` (the `description:`), `## Details` (the memory body, verbatim).
- Append: the marker heading and comment line, then the same provenance, summary and details,
  through `note_writer._acquire_lock` + `_atomic_rewrite`.
- CLI: `apply --plan PATH [--store PATH] [--apply]` and `apply --resume DIR`; vault path and
  insights folder come from `load_config(fresh=True)`, overridable with `--vault` and
  `--insights-folder`; `--backup-root` overrides `~/.claude/backups`.
  Exit 0 all done, 1 some items refused (the rest done), 2 bad plan or nothing could be done.
- Tests `tests/test_memory_consolidate_apply.py`: dry run leaves vault, store and backup root
  byte-identical (hash the trees); a written note read back has every field and no
  `source_session`; `gather_session_evidence` with the moving session's id does not return it,
  and the same note with `source_session` set is returned (positive control); the lossless check
  catches a one-character change; append leaves the rest of the target byte-identical; an append
  target outside the vault or a symlink is refused; a secret-shaped body is refused; a line
  appended to `MEMORY.md` between load and index write survives; a mixed pointer line refuses its
  slug; a write failure for one item leaves that original in place; backup dir reuse is refused;
  a stop after steps 2, 3, mid-4, 4 and 5 each finishes with `--resume` or a rerun, with no
  duplicate note or appended section and no dangling index line.
- Check: `python3 -m pytest -q tests/test_memory_consolidate_apply.py`

### Task 4: `verify` and the reindex smoke test

Delegate: codex — one function group in the module from Task 2 plus one test file; the checks are listed one by one.

- `verify(backup_dir, store, vault_root, insights_folder) -> (ok, report)` reading `run.json`
  (field names copied from the committed Task 3 code). `store` must match `run.json`'s store.
  Checks 1–3 fail only on drift that is not in `run.json`'s `baseline`:
  1. every markdown index link names an existing file;
  2. no entry is a pointer on two index lines;
  3. every `*.md` left in the store (not `MEMORY.md`) is referenced by the index;
  4. old indexed set minus new indexed set equals the moved-or-retired slugs that were in the old
     index, exactly;
  5. each `new`/`append` slug has its marker in its note and passes the lossless check against
     `moved/<slug>.md`;
  6. each moved or retired slug exists in `moved/`, and `full-copy/MEMORY.md` exists;
  7. sizes before and after, in bytes, characters and lines, labelled.
- `smoke(vault_root, folders, db_path, query) -> int`: `ensure_index(vault_root, folders, db_path)`
  then `search_vault(db_path, query, log_access=False)`; returns the hit count.
- CLI: `verify --backup DIR [--store PATH]` (exit 0 pass, 1 any check failed, 2 unreadable),
  `smoke --query TEXT [--db PATH] [--vault PATH]` (exit 0 at least one hit, 1 none, 2 error).
- Tests `tests/test_memory_consolidate_verify.py`: a clean run passes; one fixture per check 1–6
  that makes only that check fail and exit 1; pre-existing drift in the baseline does not fail;
  `smoke` against a scratch DB finds a written note and returns 1 for a query with no hits.
- Check: `python3 -m pytest -q tests/test_memory_consolidate_verify.py`

After Task 4: a bare-CLI scratch dogfood (audit, dry run, apply, verify, smoke on a copy of this
repo's store with scratch HOME, vault, backup root and DB) before Task 5.

### Task 5: `skills/memory-consolidate/SKILL.md`

Delegate: claude — skill prose and its contract test; judgement steps a brief cannot check.

- Steps: (1) `audit` and show the report, store path first; (2) read each candidate and apply the
  keep/move test, word for word from the issue comment: keep deploy and live-service rules,
  approval limits, hook and preflight mechanics and daily tool gotchas; move one-off design
  lessons, general testing lessons and anything global `CLAUDE.md` already says; stale
  point-in-time state is `retire`; plan entries that share an index line as a group;
  (3) dedupe: `/vault-search` per candidate, and a hit counts only when the matching note is
  about that lesson, not a retro that mentions it; (4) write the plan JSON with the Write tool
  (no heredoc; #524); (5) `apply` dry run, show it, ask the user; (6) `apply --apply`;
  (7) `verify`; (8) `smoke` with a phrase from a moved fact; (9) report sizes with units and the
  backup path. Hooks dir found with the `_ob_hooks()` block the other skills use.
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
  plan of about 10 real entries (new, append, retire, one grouped line, one mixed-line refusal).
  Run `verify`, `smoke`, then `vault_doctor.py` on the scratch vault (frontmatter fence, project
  name, spurious wikilinks). Record what each printed.

## Failure modes

- **Fail-open paths:** a plan item that fails validation must refuse, never default (Task 2 tests
  each rule). A write or read-back failure must leave the original in place (Task 3). `verify`
  must exit 1 on any failed check (Task 4, one fixture per check). `smoke` with no hits exits 1.
  An interrupted apply must not leave a dangling index line (Task 3 stop-point tests).
- **Exit codes:** audit 0/1/2, apply 0/1/2, verify 0/1/2, smoke 0/1/2. Each branch has a test
  that triggers it and checks `returncode`.
- **Date boundaries:** `date` is local, `created_at` is aware ISO with offset; the backup dir
  has a time to the second, so two runs a day get two dirs; a test freezes `now` near midnight.
- **Doc counts:** README and architecture counts come from the diff, not memory.
- **Broken Markdown:** the index rewrite keeps headings and blank lines; a test diff-checks a
  fixture index before and after a one-entry move (only that line and the pointer line change).
- **Unrelated changes:** Task 1 moves code without editing it; `git diff --stat` on
  `memory_index.py` shows only the import swap.
- **Inputs:** reads the store's `MEMORY.md` and `*.md` (the live files are the source of truth,
  re-read at index-write time), the plan JSON (written by the skill), the obsidian-brain config
  (`load_config(fresh=True)`), vault notes for append targets, and `run.json` for verify/resume.
  Tests: missing store, missing `MEMORY.md`, unreadable entry, plan naming a missing slug, append
  target deleted between dry run and apply, `run.json` from another store.
- **Bypass routes:** the destructive step is the move. Cheapest ways it could run unverified:
  (a) a `new` item whose note write silently failed or changed text — blocked by the lossless
  read-back before move; (b) a `retire` used to skip the vault note for a durable entry — allowed
  by design, so the dry run lists every `RETIRE` and the skill asks first; (c) a `target` that
  resolves outside the vault via `..` or a symlink — refused; (d) an index line pointing at a
  moving and a staying entry — refused, not edited; (e) a stale `run.json` from another run —
  `verify` takes an explicit `--backup` dir and checks its `store` matches; (f) a symlinked store
  or entry redirecting the move — refused.
