# Plan: #278 — `HOOKS=` resolver globs the plugin cache only

Issue: #278 (bug, P1-high, milestone v3.3). Duplicate merged in: #284.
Base: `develop @ 4d3458c`. Branch: `feature/278-hooks-resolver`.

## Problem

Every skill resolves the plugin's `hooks/` directory by globbing the plugin **cache**:

```python
sys.path.insert(0, max(glob.glob(os.path.expanduser(
    "~/.claude/plugins/cache/*/obsidian-brain/*/hooks")),
    key=lambda p: ([int(n) for n in re.findall("[0-9]+", p.split("/")[-2])], p),
    default="hooks"))
```

Under a **directory-source** install the cache is not merely stale, it is the wrong *place*.
Verified live in `~/.claude/plugins/known_marketplaces.json`:

```json
"obsidian-brain-repo": {"source": {"source": "directory"},
                        "installLocation": "/Users/abhishek/dev/claude_workspace/obsidian-brain"}
```

The runtime serves skills from the working tree, but the skills' own resolver reads from a cache
snapshot of the last *installed* version. Two failure classes follow, and the silent one is bigger:

| class | modules | symptom |
| --- | --- | --- |
| **missing** | `frontmatter.py`, `note_writer.py` | loud — `test -f` guard fires, clear error |
| **stale** | `check_items_cli.py`, `deep_cli.py`, `open_item_dedup.py`, `vault_index.py` | **silent** — imports and lies |

The stale half silently reverted #275's stdin cap in the cached `deep_cli.py` while
`tests/test_security.py`'s AST guard stayed green, because the guard walks the repo, not the cache.

**No version check can detect this.** Repo and cache both report `3.3.1`; the version is bumped at
release, so every commit between two releases shares one string.

## Two defects, not one

**D1 — wrong location.** The cache is consulted first (only), so a directory-source install never
reaches its own working tree.

**D2 — no validation that a sibling directory is a version at all.** `re.findall("[0-9]+", ...)`
extracts digits from *any* directory name, so non-canonical siblings join the ranking. Measured
against the real resolver expression:

| sibling dir | resolves to | mechanism |
| --- | --- | --- |
| `3.3.1.bak` | real | ASCII tiebreak (`/` 0x2F > `.` 0x2E) — accident |
| `3.3.1-old` | real | ASCII tiebreak — accident |
| `3.3.1a` | **stale** | ASCII tiebreak |
| `3.3.1~` | **stale** | ASCII tiebreak |
| `3.3.1_bak` | **stale** | ASCII tiebreak |
| `3.3.10` | **stale** | numeric key ([3,3,10] > [3,3,1]) |
| `3.4.0.bak` | **stale** | numeric key |

The three "real" outcomes win by ASCII accident, not by design. An allowlist of canonical version
dirs (`^[0-9]+([.][0-9]+)*$`) fixes D2 *and* removes the need for any tiebreak, because two
identical canonical names cannot coexist as sibling directories.

## Design decision: `installLocation` FIRST, cache as fallback

The order is the whole fix. **Cache-first-with-fallback is wrong** — it still serves the stale
cached module whenever one exists, which is exactly the silent class above. Resolution order:

1. Read `~/.claude/plugins/known_marketplaces.json` (fixed path — safe to hardcode).
2. Skip every entry whose own `source.source` is not `"directory"`. Shape-tolerant: a non-dict
   entry, or a `source` that is a string/list/`null`/absent, must `continue`, never raise — the
   whole loop shares one `try`, so a raise on entry 1 silently skips entries 2..N.
3. For each surviving entry, test the sentinel `<installLocation>/hooks/obsidian_utils.py`.
   The marketplace **key is user-chosen** (`obsidian-brain-repo` here) and must not be hardcoded.
   Verified discriminating: of 9 marketplaces, only the obsidian-brain one is `True` — including
   two *other* directory-source marketplaces (`cc-token-router-repo`, `claude-code-skills`).
4. Fall back to the cache glob, filtered by the canonical-version allowlist, sorted numerically.
5. Fall back to `"hooks"` (the existing relative default) — unchanged.

**Why step 2 exists (corrected after the final review).** An earlier version of this section said
a github install "has no directory entry, so step 3 finds nothing". That is false twice over:
every github-source marketplace carries an absolute `installLocation` pointing at its clone under
`~/.claude/plugins/marketplaces/<name>`, and obsidian-brain's `.claude-plugin/marketplace.json`
declares `"source": "./"` — the marketplace repo **is** the plugin repo — so that clone has
`hooks/obsidian_utils.py` at its root and satisfies the sentinel. Reproduced by running the
shipped FORM A against a github-shaped fake `$HOME`: it returned the clone, not the cache.

Left that way, github-source users (the entire external population) would load `SKILL.md` from the
**cache** while `sys.path` pointed at the **marketplace clone**, and `/plugin marketplace update`
— the documented update path — refreshes the clone without touching the cache. That window is
silent SKILL.md/hooks version skew: #278's own failure class, inverted.

With step 2, github installs resolve the cache exactly as they did before #278 — **zero behaviour
change for external users** — and the fix stays scoped to the reported bug. Pinned in both
directions by `test_a_directory_source_install_beats_the_cache` and
`test_a_github_source_install_falls_through_to_the_cache`, whose fixture deliberately builds a
clone that *does* satisfy the sentinel.

## Global Constraints

- **Quoting contexts are opposite and both are load-bearing.** Sites inside `python3 -c '...'` may
  use only **double** quotes internally; sites inside `HOOKS=$(python3 -c "...")` may use only
  **single** quotes. There are therefore **two** canonical forms differing *only* by quote
  character. Task 4's test must derive one from the other by quote-swap so they cannot drift
  semantically.
- **No backslashes in the regex.** Use `[.]`, never `\.` — a backslash survives bash double quotes
  by accident of what bash escapes, and relying on that is fragile.
- **No f-strings** in inline python (`technical_no_quotes_in_fstring_inline_python`).
- Python **stdlib only**. `json`, `glob`, `os`, `re`, `sys` are all already imported at various
  sites; the canonical form must import what it uses rather than assuming.
- The resolver must **never raise**. A malformed/absent `known_marketplaces.json` falls through to
  the cache path — a resolver that throws breaks every skill.
- **`scripts/test-dev-skill.sh:19` is NOT a bug — do not change it.** Its `ls -dt` CACHE_BASE
  deliberately targets the cache because `/dev-test` *installs into* the cache. "Fixing" it breaks
  `/dev-test`.
- 68 inline copies across 18 SKILL.md files is accepted duplication, not sloppiness: each SKILL.md
  bash block is its own shell, so the resolver cannot be hoisted to one `$HOOKS` per file. Task 4's
  byte-identity test is the mitigation and is mandatory.

## THE TESTING TRAP (read before writing any verification)

**The cache is currently byte-identical to the checkout** — refreshed 2026-07-30 12:34 UTC.
Verified independently: `identical=25 differing=0 repo-only=[] cache-only=[]`, `lines[1:40]` count
0, `_read_stdin_capped` count 5.

**Consequence: the cache-only resolver currently finds everything.** A "does `/retro` still save?"
check passes whether or not this fix is present — a green result from a condition that can no
longer occur. That is the vacuous-guard trap, and it would make every functional check here
worthless.

To exercise the bug, force the failing state. The real-world trigger:

```bash
touch hooks/_probe_278.py          # module in the checkout, absent from the cache
# old resolver: cannot see it.  new resolver: must see it.
rm hooks/_probe_278.py             # always clean up
```

Every functional assertion in Task 4 must be **fail-first proven** by this probe or by mutation
(delete the allowlist, invert the ordering, and confirm a named test fails).

## Tasks

### Task 1 — canonical resolver, python-quoting form (58 sites)

Define the two canonical forms. Apply form A (internal double quotes) to the
`sys.path.insert` variants: 28 × `import glob, re; …`, 19 × `import re; …`,
10 × `import sys, os, glob, re; …`, 1 × `import sys, json, os, glob, re; …`.
28 + 19 + 10 + 1 = **58** (an earlier draft of this plan said 48 — arithmetic error,
corrected here; 58 + 9 bash + 1 vault-doctor = the 68 skills-level total).

Form A is self-sufficient (it imports everything it uses), so replace the entire existing
resolver line **including** its leading `import ...;` prefix rather than preserving it.
Every site then ends up byte-identical.

**Quoting is determined by the site's OUTER shell quote, not by the task split.** Two
`check-items` sites are `python3 -c "…"` (outer double), so they must use *single* quotes
internally while still ending in `sys.path.insert`. That is a third legal combination and
is consistent with the stated invariant: the body is identical modulo quote character, and
the last line is chosen by context. Task 4 must accept all three combinations, not two.

### Task 2 — bash-quoting form (9 sites)

Apply form B (internal single quotes) to the 9 `HOOKS=$(python3 -c "…")` sites. Keep the adjacent
`test -f "$HOOKS/note_writer.py"` guard, but fix its message (Task 5).

### Task 3 — `vault-doctor` DISPATCHER (1 site, different shape)

`skills/vault-doctor/SKILL.md:51` is its own bug:

```bash
DISPATCHER="$(ls -dt ~/.claude/plugins/cache/*/obsidian-brain/*/scripts/vault_doctor.py | head -1)"
```

Three distinct defects: it targets `scripts/` not `hooks/`; it orders by **mtime** (`ls -dt`), so it
picks whichever cache dir was touched last rather than the newest version; and `find … | head -1`
over a glob is unsafe with `.bak` siblings. Resolve `<install>/scripts/vault_doctor.py` first, then
the allowlisted cache. Do **not** reuse form A/B verbatim — different subdirectory.

### Task 4 — drift-detection + fail-first tests

- Assert every in-scope site uses the canonical form **byte-identically**, with the site count
  asserted by **equality** against an independently derived count (`== 68`, never `>= 68`) so a
  deleted site fails the test instead of silently passing. Blocks are indented at their site, so
  normalize by the block's own leading indent before comparing — comparing raw text produces
  spurious families.
- Assert the resolver **body** is identical across all three legal combinations modulo the quote
  character: (double, `sys.path.insert`) ×56, (single, `sys.path.insert`) ×2, (single, `print`) ×9.
- Unit-test the allowlist against **all seven** sibling names in the D2 table above, using the
  exact resolver expression — including the three that currently pass by ASCII accident.
- Functional test via the `_probe_278.py` trigger: old resolver cannot see it, new one can.
- Extend `tests/test_security.py`'s stdin-cap AST guard to walk the path the skills *actually*
  resolve, not just the repo — this is what let the cached `deep_cli.py` revert #275 unnoticed.
- **Mutation-prove each guard**: delete the allowlist filter → a named test must fail; invert
  installLocation/cache order → a named test must fail.

### Task 5 — error message, architecture, changelog

- The `test -f` guard's message says "Run `/plugin marketplace update` (or `/dev-test install`)".
  Under a directory-source install `marketplace update` **cannot** help — it is the misdiagnosis
  the message invites, and it cost this project a wrong root-cause once already. Rewrite to name
  the resolved path and the actual remedy.
- Classify the 14 `scripts/` sites: `scripts/dev-test/*` and `scripts/test-*.sh` import via the
  cache glob, which is the documented dogfood hazard (a dogfood script that loads the cache
  silently tests the wrong tree). Fix the live ones; leave historical fixtures if changing them
  would rewrite recorded evidence — state the classification per file rather than sweeping blind.
- Update `docs/architecture/architecture.json` (`lastUpdated`, and any flow whose module
  resolution is described), re-render `architecture.html`, pass `smoke-test.sh` `[main]` +
  `[sparse]`.
- `CHANGELOG.md` under `[Unreleased]`.

## Out of scope

- **Scripts whose stated purpose is validating the `/dev-test install` result are correctly
  cache-targeted, not a #278 defect.** `/dev-test install` writes the working tree *into* the
  plugin cache; a script whose entire job is confirming that write succeeded must read the cache
  it just wrote to — resolving via marketplace `installLocation` first would validate the wrong
  thing (the checkout, not the install). The distinguishing test is **whether the file's entire
  job is validating what `/dev-test install` itself wrote into the plugin cache** — not the
  presence of a `# Run AFTER: /dev-test install` header. An earlier draft of this section named
  that header as the criterion, and it is wrong: `test-issue-123-manual.py` and
  `test-issue-128-manual.py` both carry that exact header and both **were** converted, correctly,
  because they assert on *implementation content* (does this tree contain the #123/#128 markers?),
  which is a question about whichever tree the skills load, not about the install. Stating the
  header as the test would send the next sweep to revert them.

  Seven files are in the cache-only class — do not "fix" them. The list is pinned by
  `CACHE_ONLY_SCRIPTS` in `tests/test_hooks_resolver_drift.py`, which also asserts every entry
  still exists so a rename cannot leave a dead exemption behind:
  - `scripts/test-dev-skill.sh` — writes the cache; must find it as Claude Code laid it out
  - `scripts/dev-test/test-issue-101-manual.sh`
  - `scripts/dev-test/test-issue-105-manual.sh`
  - `scripts/dev-test/test-snapshots-manual.sh`
  - `scripts/dev-test/test-vault-doctor-snapshots-manual.sh`
  - `scripts/dev-test/DEV-TEST-ISSUE-105.md` — manual checklist that greps the installed tree
  - `scripts/dev-test/DEV-TEST-ISSUE-125.md` — same
- **Audit-method limitation.** The site inventory that produced this plan's "14 `scripts/` sites"
  and the later "68 + N" counts was anchored on the literal string shape
  `plugins/cache/*/obsidian-brain` (a `glob.glob(...)` call). That grep cannot see other shapes
  that resolve the same cache directory differently — e.g. `find "$CACHE_DIR" -maxdepth 2 -type d
  -name hooks`, used by the five files above — so a shape-anchored audit undercounts by
  construction. Any future audit of this class must state which literal shape(s) it searched for,
  not just report a total. Partly closed since:
  `test_no_scripts_file_reaches_the_cache_without_the_registry` scans `scripts/**` with a
  deliberately shape-agnostic key (any `plugins/cache` mention, which catches the `find` and
  `ls -dt` shapes too) and requires a `known_marketplaces.json` lookup in the same file, with the
  seven cache-only files allowlisted. Byte identity is impossible there — different defaults,
  different path indices (`[-2]`/`[-3]`/`[-4]`), Python and shell hosts — so this weaker invariant
  is what `scripts/` gets in place of the 68 copies' byte-for-byte guard.
- A generated resolver at a fixed path. Fewer copies, but it adds an install-time artifact that can
  itself go missing — trading 68 visible copies for one invisible single point of failure.
- Restoring `3.3.1.bak` into the glob path. A sibling session moved it to
  `~/.claude/plugins/obsidian-brain-cache-backups/3.3.1.bak-pre-20260730`; with the Task 1
  allowlist its location stops mattering.
