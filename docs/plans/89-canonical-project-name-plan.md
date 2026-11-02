# #89 — skills derive the canonical project name (worktree-safe)

**Goal:** inside a git worktree, `/recall`, `/vault-ask`, `/vault-stats`, `/check-items` and `/link` must derive the main-repo project name, which is what session notes store in `project:` (`canonical_project_name()`, `hooks/obsidian_utils.py:1331`). Today they use `basename(cwd)` or `basename(git rev-parse --show-toplevel)`, which is the worktree folder name, so `/recall` finds 0 sessions.

**Design (approved 2026-11-02):** call `canonical_project_name()` from each snippet. It resolves via `git rev-parse --git-common-dir`, falls back to the lowercased cwd basename outside git, and normalises `_`/space to `-`. No suffix-strip retry: the root cause is the derivation, not the vault.

**Constraint:** `tests/test_hooks_resolver_drift.py::test_resolver_site_count_is_exactly_84` pins the `_ob_hooks` resolver site count. No new resolver block may be added; `/link` gets `PROJECT` from its existing Step 1 snippet.

## Task 1 — recall, vault-ask, vault-stats, link + tests

Delegate: codex — self-contained edits to 4 SKILL.md files plus 1 new test file, with one pytest check command.

- `skills/recall/SKILL.md` Step 1: `project = canonical_project_name()` (import it beside `load_config`).
- `skills/vault-ask/SKILL.md`: same, import beside `load_config, indexed_folders`.
- `skills/vault-stats/SKILL.md`: same.
- `skills/link/SKILL.md`: Step 1 snippet also prints `PROJECT=` + `canonical_project_name()`; Steps 4A.1 and 4B.1 use that `PROJECT` instead of `basename "$(pwd)"`.
- New `tests/test_skill_project_name.py`:
  - static: no `skills/*/SKILL.md` contains `basename(os.getcwd())` used for `project`, or `basename "$(pwd)"`; each of the five skills references `canonical_project_name`.
  - real run: extract the `/recall` Step 1 `python3 -c '...'` snippet, run it with `cwd` = a linked `git worktree` (`git worktree add`) of a temp repo named `main-repo`, worktree folder `main-repo--issue-9`; fake `HOME` with a config; `PYTHONPATH=<repo>/hooks`. Assert `PROJECT=main-repo`.
  - real run, outside git: cwd = a temp dir `My_Dir` with no repo; assert `PROJECT=my-dir`.
  - same real run for the `/vault-ask` and `/link` Step 1 snippets in the worktree.

Check: `python3 -m pytest tests/test_skill_project_name.py tests/test_hooks_resolver_drift.py tests/test_skill_snippets.py -q`

## Task 2 — check-items (3 derivations)

Delegate: codex — one SKILL.md file, mechanical swap, one pytest check command.

- `skills/check-items/SKILL.md` Step 2 (~line 176), Step 2b (~line 282), Step 10 `scope_name` (~line 1133): replace `os.path.basename(res.stdout.strip())` with `canonical_project_name()` imported from `obsidian_utils` (each block already puts hooks on `sys.path`). Keep the existing `git rev-parse --show-toplevel` call as the "inside a git repo?" check, and its error/`None`/`"unknown"` branches.
- Extend `tests/test_skill_project_name.py` static check: check-items has no `os.path.basename(res.stdout` and has 3 `canonical_project_name()` calls.

Check: `python3 -m pytest tests/test_skill_project_name.py tests/test_skill_snippets.py tests/test_hooks_resolver_drift.py -q`

## Failure modes

1. **Fail-open paths:** `canonical_project_name()` never raises; returns `"unknown"` when cwd is gone. Outside git it falls back to the basename — tested by the non-git real run. check-items keeps its "not in a git repo" error exit.
2. **Exit codes:** snippets' exit codes unchanged (config-missing exits 1 stays). Not touched otherwise.
3. **Date boundaries:** not applicable; no dates read or written.
4. **Doc counts:** CHANGELOG/PR body say "five skills" and "three check-items sites" — count from the diff before writing.
5. **Broken Markdown:** edits are inside existing fenced code blocks and one prose line each in `/link`; `test_skill_snippets.py` compiles every snippet.
6. **Unrelated changes:** none planned; verify `git diff --stat origin/develop...HEAD` lists only the 5 SKILL.md files, the new test, CHANGELOG and this plan.
7. **Inputs:** reads git metadata of the cwd (`--git-common-dir`; source of truth is the repo's shared `.git`). Missing git → basename fallback (tested). Name normalisation now also maps `_` to `-` in check-items, matching what notes store.
