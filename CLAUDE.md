# CLAUDE.md

This file provides shared development rules for this repository. Native Codex guidance is in `AGENTS.md`.

## Project Overview

Obsidian Brain provides Claude Code and Codex plugin packages that turn an Obsidian vault into a persistent knowledge base across sessions. It auto-logs sessions, captures curated knowledge, and enables project-scoped context resume via structured markdown notes.

**Integration pattern:** Direct filesystem writes only — no MCP server, no REST API, no Obsidian plugins required (except Dataview for dashboards).

## Development Commands

There is no build step. This Python (stdlib only) + Markdown plugin has a pytest suite, a 90% coverage gate, and a strict host-neutral source check. Run `./scripts/commit-preflight.sh` before committing. Individual checks include:

```bash
# Verify hook registration is valid JSON
python3 -c "import json; json.load(open('hooks/hooks.json'))"

# Verify plugin manifest
python3 -c "import json; json.load(open('.claude-plugin/plugin.json'))"

# Test the registered native entry with a disposable configured home and synthetic transcript
# Set HOME, CLAUDE_CONFIG_DIR or CODEX_HOME, and XDG_STATE_HOME to disposable
# directories first. Supply explicit --host/--client and native ID in stdin JSON.
python3 hooks/native_entry.py --host claude --client claude-code < "$SYNTHETIC_HOOK_INPUT"
# Inspect a selected scratch context; no model call.
python3 hooks/brain_cli.py --host "$OB_HOST" --client "$OB_CLIENT" --resource-root "$OB_RESOURCE_ROOT" --session-id "$OB_SESSION_ID" --cwd "$OB_CWD" context < /dev/null
```

## Architecture

### Two execution modes

1. **Native hooks** use `hooks/native_entry.py` and `hooks/native_lifecycle.py`.
   Claude registers `hooks/hooks.json`; Codex registers `hooks/codex-hooks.json`
   through `.codex-plugin/plugin.json`. Entry time is recorded before shared
   imports. Capture failures fail open, and Stop policy output uses the native
   JSON contract. Without verified current-client binding, capture is skipped
   and stderr says no source was retained. Bound SessionEnd handlers append one
   private outcome log entry, including pending, skipped, and failed captures.
   Bootstrap failures without a verified context report only to stderr.
2. **Skills** use the loaded `skills/*/SKILL.md`, explicit runtime context, and
   named operations in `hooks/skill_procedures.py`. The loaded skill and imported
   runtime must belong to the same installation. Paired host references contain
   native setup details.

### Key files

- `hooks/brain_cli.py` and `hooks/skill_procedures.py` — loaded-skill entry and fixed operations.
- `hooks/native_entry.py` and `hooks/native_lifecycle.py` — native hook dispatch.
- `hooks/runtime_adapters/` — named host configuration and source adapters.
- `hooks/runtime_context.py` — immutable invoking host, client, full native session
  ID, project, worktree, config, vault, resource, index, and state selection.
- `hooks/transcripts/` — native source parsing and visible-record normalization.
- `hooks/capture.py` — checkpoints, retained events, committed cursors, recovery,
  and snapshots. Partial input remains pending.
- `hooks/note_transactions.py` — revision checks and vault publication locks.
- `hooks/ai_backend.py` and `hooks/ai_adapters/` — bounded native analysis with
  strict output validation and no cross-host fallback.
- `hooks/operation_state.py` and `hooks/session_auxiliary_state.py` — private,
  versioned state scoped by vault, provider, full session identity, and project.
- `hooks/obsidian_utils.py` and `hooks/obsidian_session_*.py` — shared utilities
  and legacy compatibility entry points.
- `templates/` and `dashboards/` — compatible vault notes and Dataview views.

### Data flow

Native capture retains normalized visible events before advancing the committed
cursor. Revision checks preserve user edits. Recovery replays retained input;
partial or unavailable input never becomes a completed session claim. Well-formed
unknown or ownership-ambiguous rows retain bounded private source references.
Verified later rows can publish while the source stays partial. Replay checks
the original byte range and hash before accepting a newly supported row.
Malformed or truncated input still blocks its cursor. SessionStart and doctor
report unresolved rows with safe type labels. Native hooks do not run AI. Summary upgrades use the invoking host's analysis adapter
and publish only after output and source revisions pass validation.

Private runtime state stays outside the vault. Bound diagnostics follow the
selected session's `<state>/v1/<vault>/<host>/<session>/<project>/logs/obsidian-brain-hook.log`.
Each session has its own log. Bound SessionEnd writes one outcome line, including
`exception` when capture raises. Other native outcomes include `ok_raw_note_only`,
`pending_capture`, and `write_failed`. Unbound bootstrap failures report on stderr.
Legacy path diagnostics do not aggregate native sessions. The legacy Claude compatibility log remains
`~/.claude/obsidian-brain-hook.log`, rotates at 100 KB, and uses `key=value` fields.

### Configuration

Native storage uses the selected `CLAUDE_CONFIG_DIR` or `CODEX_HOME`, with the
corresponding default home when unset. `OBSIDIAN_BRAIN_CONFIG` selects an
independent config file; `OBSIDIAN_BRAIN_DB` and `OBSIDIAN_BRAIN_STATE_DIR` select
index and private state independently. An already-bound context retains its
selection when environment variables change. Native skills and operations
cannot switch that context to another vault or loaded installation.

Legacy Claude config remains `~/.claude/obsidian-brain-config.json`. Vault folder
names, tags, and note types are taxonomy and stay compatible across hosts.
Full parity is not certified by the current source changes. See
`docs/parity/acceptance-evidence.md` for the required native checks.

### Tag convention

All frontmatter tags use the `claude/` prefix: `claude/session`, `claude/insight`, `claude/decision`, `claude/error-fix`, `claude/snapshot`, `claude/imported`, `claude/standup`, `claude/retro`, `claude/project/<name>`, `claude/topic/<topic>`, `claude/auto`.

### Keeping the architecture page current

`docs/architecture/architecture.json` is the canonical, machine-readable architecture of this plugin (rendered to `docs/architecture/architecture.html`). It is consumed by future agents, so it must not drift from the code.

**Whenever a change adds, removes, or renames a hook, skill, vault-doctor check, CLI/support module, datastore, env var, or alters a data flow, update the architecture artifacts in the same PR:**

- Edit `docs/architecture/architecture.json` (layers / components / flows / types / environment), keeping `lastUpdated` set to the change date and `version` in sync with `plugin.json`. Every `flows[].steps[].from`/`to` must reference a real `layers[].components[].id`, and every component `files[]` path must exist on disk.
- Re-render the viewer: `~/.claude/skills/architecture-page/scripts/render-html.sh --json docs/architecture/architecture.json --output docs/architecture/architecture.html`
- Validate before committing: `~/.claude/skills/architecture-page/scripts/smoke-test.sh --json docs/architecture/architecture.json` — both `[main]` and `[sparse]` must pass.
- For a large or structural change, regenerate from scratch via the `architecture-page` skill rather than hand-editing.
- Ground every claim in live source (run `wc -l`, read the actual file) — never copy a number or path from prose/docs that may be stale.

`docs/.nojekyll` keeps GitHub Pages serving these files verbatim; do not remove it.

## Conventions

- **Commits:** Use conventional commit format — `feat(obsidian-brain):`, `fix:`, `chore:`, `docs:`
- **Python:** stdlib only, no pip dependencies. All hooks must be deterministic and safe to run at session boundaries.
- **Atomic writes:** All vault writes must use temp file + rename pattern (see `write_vault_note()` in `obsidian_utils.py`).
- **Version:** Bump `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `.claude-plugin/marketplace.json`, and `docs/architecture/architecture.json` and its generated HTML in lockstep (use `scripts/bump-version.sh`), and update `CHANGELOG.md` for releases.
- **Branching:** Never commit directly to develop/main — use feature branches.

## Security Patterns

When writing new hooks, skills, or scripts, follow these rules:

- **Path containment:** Never construct file paths from user input without `resolve()` + `is_relative_to()` containment check against the vault root.
- **No predictable /tmp paths:** Use selected owner-only native state (0o700) or `tempfile.mkstemp` — never hardcoded `/tmp/ob-*` paths.
- **No path interpolation in python3 -c:** Always pass paths via `sys.argv`, never as string literals in the source code.
- **JSON via stdin, not shell args:** Use `printf '%s' "$VAR" | python3 -c '... json.load(sys.stdin)'` — never pass JSON arrays as shell arguments.
- **Atomic writes only:** All vault file writes must use temp file + rename — never `sed -i` or direct overwrite.
- **Owner-only permissions:** `0o600` for files containing user data (notes, DB, config). `0o700` for working directories.
- **Cap stdin reads:** Hook entry points must use `sys.stdin.read(1_000_000)` — never unbounded `read()`.
- **Scrub secrets:** Apply `scrub_secrets()` to any user message content before writing to vault notes.

## Git Flow Rules

- Never commit directly to `main` or `develop` — use feature branches
- Branch naming: `feature/*`, `release/*`, `hotfix/*`
- Features branch from and merge to `develop`
- Releases branch from `develop`, merge to both `main` and `develop`
- Hotfixes branch from `main`, merge to both `main` and `develop`
- Run `./scripts/commit-preflight.sh` before every commit

## Distribution & Release

obsidian-brain is distributed as a **standalone marketplace from this repo**
(`abhattacherjee/obsidian-brain`). The repo's `.claude-plugin/marketplace.json`
(`source: "./"`) is authoritative — there is **no longer a sync step to the
claude-code-skills monorepo**.

Release flow (Git Flow):
1. `release/*` branch from `develop`; run `scripts/bump-version.sh <part>` to
   bump `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
   `.claude-plugin/marketplace.json`, and architecture JSON/HTML in lockstep.
2. Update `CHANGELOG.md`.
3. Merge to `main`, tag `vX.Y.Z`, publish a GitHub Release.
4. Back-merge `main` into `develop`.

Claude users update via `/plugin marketplace update`. Local dev testing uses
`/dev-test install` (which calls `scripts/test-dev-skill.sh`, now
source-agnostic about the cache directory).

Codex distribution uses its own descriptor and selected hooks manifest. Installed
Codex use remains blocked on verified current-client binding; no CLI/Desktop
installation or update instruction here certifies that missing native evidence.
