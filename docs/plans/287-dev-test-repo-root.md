# Plan — #287: `/dev-test` must resolve the plugin repo root, not the invoking project's

## Context

`skills/dev-test/SKILL.md` runs, in all three of its steps:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
./scripts/test-dev-skill.sh <install|restore|status>
```

Invoked from any other project — the normal case, since you install the dev
plugin *while working on something else* — `git rev-parse --show-toplevel`
returns **that** project's root. `./scripts/test-dev-skill.sh` does not exist
there and the command fails. Observed live from a `control-tower` session,
where the toplevel resolved to `/Users/abhishek/dev/claude_workspace/control-tower`.

It works whenever you happen to be standing in the obsidian-brain repo, which
is exactly where a maintainer tests it. The failure only appears in the
situation the skill exists for.

This is the same bug shape that hit all 12 skills in April 2026
(`ModuleNotFoundError for obsidian_utils When Running Skills From Other
Projects`) — cwd-relative resolution inside a plugin that runs from anywhere.
That one was fixed with the plugin-cache glob, then re-fixed in #278 to prefer
the registered directory-source install. `/dev-test` is the one place where
**neither** of those answers is correct on its own; see D3.

## Design decisions

### D1 — Both fixes proposed in the issue body are broken

The issue suggests `${CLAUDE_PLUGIN_ROOT}` or `${BASH_SOURCE[0]:-$0}`. Both were
measured in a live skill Bash block on 2026-07-30 and neither works:

| Proposed | Actual value in a skill Bash block | Resolves to |
|---|---|---|
| `${CLAUDE_PLUGIN_ROOT}` | **unset** (`env \| grep -i claude` shows no such var) | empty |
| `${BASH_SOURCE[0]}` | empty — the block is not a sourced script | falls through to `$0` |
| `$0` | `/bin/zsh` | `/bin/../..` = `/` |

A SKILL.md fenced block is not a file that bash sources; Claude runs the
commands through the Bash tool, so there is no script path to derive from.

Separately, even if `CLAUDE_PLUGIN_ROOT` *were* set it would be the wrong
answer: per the vault error-fix note `CLAUDE_PLUGIN_ROOT resolves to marketplace
directory, not cache`, it points at `~/.claude/plugins/marketplaces/<repo>/` —
a clone, not the working checkout you want to copy *from*.

**Neither suggestion is implemented.** The issue body is reconciled to match
(see Task 5).

### D2 — Resolution order

Three layers, in this order:

1. **Registered directory-source install** in
   `~/.claude/plugins/known_marketplaces.json` whose `installLocation` contains
   the sentinel `scripts/test-dev-skill.sh`. Deterministic and cwd-independent —
   the #278 precedent, already used by `vault-doctor` (FORM C).
2. **`git rev-parse --show-toplevel`, but only if it contains the same
   sentinel.** This preserves the case that works today: an *unregistered*
   local checkout with the cwd inside it. Dropping this layer would regress a
   currently-working invocation.
3. **Hard fail** with an actionable message naming both attempted routes.

### D3 — The plugin cache is deliberately NOT a fallback

Every other resolver in this repo falls back to
`~/.claude/plugins/cache/*/obsidian-brain/*/…`. This one must not, and the
reason is specific to what the script does:

```bash
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # scripts/test-dev-skill.sh:14
```

The script derives its source tree from **its own location**. Resolve the
script out of the cache and `REPO_ROOT` becomes the cache version directory, so
`install` copies the cache onto itself — `cp cache/hooks/*.py cache/hooks/`, a
byte-for-byte no-op that prints a full success transcript and leaves a `.bak`.
That is precisely the silent-stale class #278 existed to kill: it does not
raise, it lies. A loud failure is strictly better than a successful-looking
self-copy.

Corollary: `/dev-test install` is meaningless without a local checkout to copy
*from*. Failing when there is none is the correct semantic, not a limitation.

### D4 — The 3 new copies must be covered by the drift test

`tests/test_hooks_resolver_drift.py` discovers resolver sites via
`_DEF_RE = ^(\s*)def (_ob_hooks|_ob_doctor)\(\):$` and pins each to a canonical
form with an **equality** assertion on the total (`EXPECTED_SITE_COUNT == 68`).

A new resolver named anything else is **invisible** to `_SITES`: the count
never moves, byte identity never applies, and three unguarded hand-copies ship.
That is exactly finding C1 from the #278 final review, which caught a 69th copy
of the old one-liner passing the whole suite. Registering FORM_D is therefore
mandatory, not optional polish.

### D5 — Shell quoting context

The resolver is wrapped in `python3 -c "…"` inside `$( )`, matching
`vault-doctor` (FORM C). The Python body therefore uses **single** quotes
internally throughout. A mismatch here breaks the skill *silently* — the shell
terminates the string early and Python receives a truncated program.

## Global Constraints

- **Python: stdlib only.** No pip dependencies.
- **No path interpolation into `python3 -c`** (CLAUDE.md security pattern). The
  resolver takes no arguments; it reads only `~/.claude/plugins/known_marketplaces.json`.
- **FORM_D is byte-pinned.** The text in `skills/dev-test/SKILL.md` and the
  `FORM_D` constant in `tests/test_hooks_resolver_drift.py` must be
  byte-identical after per-block dedent. Copy one into the other; do not retype.
- **`EXPECTED_SITE_COUNT` moves 68 → 71 by equality**, and
  `EXPECTED_FORM_COUNTS` gains `FORM_D: 3`. The existing
  `test_form_counts_sum_to_the_site_count` invariant must still hold.
- **`scripts/test-dev-skill.sh` stays in `CACHE_ONLY_SCRIPTS`.** Its exemption
  reason ("Writes the cache; must find it the way Claude Code laid it out")
  remains true — do not port a FORM A/B/C resolver into it.
- **Baseline is 2306 passing tests** on `develop` at `8d4505f`. The suite must
  be green at every task boundary.
- Run `./scripts/commit-preflight.sh` before every commit, as a **separate**
  Bash call from `git commit`.

## THE TESTING TRAP

This machine has a directory-source registration for obsidian-brain pointing at
this very checkout. That means **layer 1 and layer 2 of D2 return the same
path here**, and a test that only checks "the resolver returned the checkout"
passes no matter which layer produced it — including if you delete one entirely.

Every behavioural test must therefore drive the resolver against a **synthetic
registry fixture** (a temp `known_marketplaces.json` and a temp tree), never
against the live `~/.claude`. For each layer, assert the *discriminating* case:

- layer 1 wins when registry and cwd point at **different** trees
- layer 2 is reached only when the registry yields nothing
- layer 2 rejects a toplevel **without** the sentinel
- neither → non-zero exit and an error naming both routes

And for every guard added, run the mutation: delete that guard, re-run, confirm
a **named** test fails for the right reason. A passing test is not evidence.

## Tasks

### Task 1 — Replace the 3 resolution sites in `skills/dev-test/SKILL.md`

Replace the `cd "$(git rev-parse --show-toplevel …)"` + `./scripts/…` pair in
Step 2 (install), Step 3 (restore), and Step 4 (status) with the block below.
The three copies differ **only** in the trailing subcommand.

Canonical FORM_D Python body (single-quoted internals, per D5):

```python
import json, os
def _ob_repo():
    try:
        for _m in json.load(open(os.path.expanduser('~/.claude/plugins/known_marketplaces.json'))).values():
            _s = _m.get('source') if isinstance(_m, dict) else None
            if not (isinstance(_s, dict) and _s.get('source') == 'directory'):
                continue
            _i = _m.get('installLocation') if isinstance(_m, dict) else None
            if not (isinstance(_i, str) and os.path.isabs(_i)):
                continue
            if os.path.isfile(os.path.join(_i, 'scripts', 'test-dev-skill.sh')):
                return _i
    except Exception:
        pass
    return ''
print(_ob_repo())
```

Shell wrapper (identical in all three steps except the final argument):

```bash
REPO="$(python3 -c "
<FORM_D body above>
")"
if [ -z "$REPO" ]; then
    _T="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$_T" ] && [ -f "$_T/scripts/test-dev-skill.sh" ]; then
        REPO="$_T"
    fi
fi
if [ -z "$REPO" ] || [ ! -f "$REPO/scripts/test-dev-skill.sh" ]; then
    echo "ERROR: could not locate the obsidian-brain checkout. Looked for a directory-source marketplace entry in ~/.claude/plugins/known_marketplaces.json, then for scripts/test-dev-skill.sh under the current repo. /dev-test needs a local checkout to copy from; run it from the obsidian-brain repo, or register the checkout with /plugin marketplace add <path>." >&2
    exit 1
fi
bash "$REPO/scripts/test-dev-skill.sh" install
```

Also update the surrounding prose so it no longer implies the command must be
run from inside the repo.

**Test:** none in this task — Task 3 pins the text, Task 4 exercises behaviour.
Task 1's own verification is that all three blocks are byte-identical apart
from the subcommand.

### Task 2 — Harden `scripts/test-dev-skill.sh` against a self-copy

The issue asks for the script to fail clearly when it cannot find its own repo
root, rather than trusting the caller. Two guards, immediately after
`REPO_ROOT` is computed at line 14:

1. **Sentinel check** — `REPO_ROOT` must contain `hooks/obsidian_utils.py` and
   `skills/`. If not, exit non-zero: the script is not sitting in an
   obsidian-brain checkout.
2. **Self-copy guard** — if `REPO_ROOT` resolves to a path under
   `~/.claude/plugins/cache/`, exit non-zero with an explicit message that
   installing the cache onto itself is a no-op and a real checkout is required
   (D3).

Both guards must fire *before* any `cp`, and before the `.bak` backup is taken —
a guard that trips after the backup leaves the user with stray state.

`status` should keep working in as many situations as possible; apply guard 2
to `install` only if applying it to `status` would break reporting on a
cache-only machine. State which you chose and why in the report.

**Test:** `tests/test_dev_test_script_guards.py` (new) — drive the script with a
synthetic `REPO_ROOT` (temp dir, plus a temp dir under a fake cache path) and
assert non-zero exit + the expected stderr substring. Use `pytest.mark.skipif`
if the test shells out to `bash` and it may be unavailable, matching the
existing `_REQUIRES_<BIN>` convention in this suite.

### Task 3 — Register FORM_D in `tests/test_hooks_resolver_drift.py`

Per D4. Concretely:

- Add a `FORM_D` constant holding the Task 1 body **byte-identical** to what
  shipped in `skills/dev-test/SKILL.md` (copy it from the file).
- Extend `_DEF_RE` to `(_ob_hooks|_ob_doctor|_ob_repo)`.
- Extend `_IMPORT_RE` to admit FORM_D's `import json, os` line alongside the
  existing `import glob, json, os, re[, sys]`.
- Extend `_TAIL_RE` to admit `print(_ob_repo())`.
- `EXPECTED_FORM_COUNTS[FORM_D] = 3`; `EXPECTED_SITE_COUNT = 71`.
- Update the module docstring and the several comments that say "68" so they
  do not contradict the new count.

**Test:** the existing suite is the test — `test_resolver_site_count_is_exactly_71`,
`test_every_resolver_site_is_byte_identical_to_a_canonical_form`, and
`test_canonical_form_family_counts_are_exact` must all pass and must *fail* if
FORM_D's text and the SKILL.md copies diverge by one byte.

**Mutation to run and report:** change a single character inside one of the
three SKILL.md copies and confirm the byte-identity test fails naming that
site. Then revert. Also confirm that renaming `_ob_repo` in SKILL.md alone
makes the count test fail — proving the new sites are genuinely discovered and
not silently skipped.

### Task 4 — Behaviour tests for the FORM_D resolver

New tests (extend `tests/test_hooks_resolver_drift.py`'s behavioural section,
or a new module if that file's structure makes it awkward — implementer's call,
state which). Per THE TESTING TRAP, every case uses a synthetic registry and
temp trees; none may read the live `~/.claude`.

Required cases:

1. A directory-source entry whose tree **has** the sentinel wins, even when the
   cwd is a different git repo.
2. A directory-source entry whose tree **lacks** the sentinel is skipped, and a
   later valid entry still wins (a bad entry must not shadow a good one — the
   C2 defect from #278).
3. A **github**-source entry is ignored entirely, even with a valid
   `installLocation` that has the sentinel. (This repo's `marketplace.json`
   declares `"source": "./"`, so a github clone root *does* carry
   `scripts/test-dev-skill.sh` — this case is real, not theoretical.)
4. An entry with a missing, empty, non-string, or relative `installLocation`
   is skipped without aborting the loop.
5. A malformed / unreadable registry yields `''` rather than raising.
6. The shell fallback: registry yields `''` and the cwd toplevel **has** the
   sentinel → that toplevel is used.
7. The shell fallback rejects a toplevel **without** the sentinel → non-zero
   exit, error mentions both routes.

Cases 6–7 exercise shell, not Python — drive them via `bash -c` against the
extracted block, the same way the existing behavioural tests drive FORM A/B/C.

### Task 5 — Reconcile docs and the issue body

- `docs/architecture/architecture.json`: the `sk-dev-test` component purpose
  (line ~163) says it installs "via scripts/test-dev-skill.sh". Extend it to
  name the resolution route, matching how `sk-vault-doctor` is described.
  Keep `lastUpdated` at the change date and all three version fields
  (`version`, `techStack.framework.version`, `gitFlow.currentVersion`) in sync.
  Re-render and run `smoke-test.sh` — both `[main]` and `[sparse]` must pass.
- `CHANGELOG.md`: add the fix under `[Unreleased]`.
- Edit the **body** of issue #287 (`gh issue edit 287 --body-file`) to record
  that both suggested fixes were measured and rejected (D1), and what shipped
  instead. Do not leave a superseded suggestion standing as the spec.

## Out of scope

- The ~58 other `cd "$(git rev-parse --show-toplevel …)"` sites across skills.
  Those load config / derive the project name and genuinely want the
  **invoking** project's root, exactly as the issue says. Not touched.
- Adding a `bin/` launcher. Claude Code puts `<plugin_root>/bin` on `PATH`
  (verified — it is on `PATH` here even though the directory does not exist),
  so a launcher would resolve to whichever install is *active*, which for a
  github-source install is the cache — reintroducing D3's self-copy. It also
  adds a new distribution surface (manifest, sync excludes, architecture entry)
  for a three-site fix.
- Changing how `test-dev-skill.sh` discovers the **cache** it writes to. Only
  its own repo-root self-location is in scope.
