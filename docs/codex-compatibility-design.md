# Codex Compatibility Design

Date: 2026-07-06
Status: proposed

## Summary

Make `obsidian-brain` work as both a Claude Code plugin and a Codex plugin without forking the vault format or duplicating the core Python logic.

The safest design is:

1. Keep the existing Claude Code package intact.
2. Add Codex packaging metadata and Codex-friendly repository guidance.
3. Extract provider-specific assumptions behind a small runtime adapter layer.
4. Port the prompt skills incrementally, starting with read/query workflows before lifecycle auto-logging.

The Obsidian vault should remain backward-compatible. Existing `claude-sessions/`, `claude-insights/`, dashboards, SQLite index, note filenames, and frontmatter fields should continue to work unless the user opts into provider-neutral folder/tag names later.

## Source Notes

This spec is based on:

- Current repo source: `.claude-plugin/plugin.json`, `hooks/hooks.json`, `hooks/obsidian_utils.py`, `skills/*/SKILL.md`, `README.md`, `CLAUDE.md`, and test fixtures.
- Codex manual sections fetched on 2026-07-06:
  - Agent Skills: `/codex/skills.md`
  - Build plugins: `/codex/plugins/build.md`
  - Hooks: `/codex/hooks.md`
  - Slash commands in Codex CLI: `/codex/cli/slash-commands.md`
  - Custom instructions with `AGENTS.md`: `/codex/guides/agents-md.md`
  - Advanced Configuration: `/codex/config-advanced.md`
  - Import to Codex: `/codex/import.md`

## Current State

The repo is strongly Claude Code shaped:

- Plugin metadata lives in `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.
- Hook registration lives in `hooks/hooks.json` and commands use `${CLAUDE_PLUGIN_ROOT}`.
- Runtime state and config default to `~/.claude/`:
  - `~/.claude/obsidian-brain-config.json`
  - `~/.claude/obsidian-brain/`
  - `~/.claude/obsidian-brain-vault.db`
  - `~/.claude/projects/`
  - `~/.claude/plugins/cache/*/obsidian-brain/*/hooks`
- Transcript parsing assumes Claude Code JSONL fields such as `sessionId`, `gitBranch`, `entry.type`, `entry.message.content`, and tool blocks named `Bash`, `Read`, `Write`, `Edit`, `MultiEdit`, `Grep`, `Glob`, `Agent`.
- Skills are Markdown workflows, but many snippets hard-code Claude cache lookup and Claude-only orchestration terms such as `TaskCreate`, `TaskUpdate`, `Agent`, `/context-shield`, `/conversation-search`, and `claude -p`.
- Vault schema uses `claude/*` tags and `type: claude-session`, `claude-insight`, `claude-decision`, etc.

The reusable parts are substantial:

- Atomic vault writes.
- Path containment and secret scrubbing.
- Markdown templates.
- SQLite/FTS indexing.
- Vault search/stat/doctor/check-items logic.
- Most note taxonomy and dashboard behavior.

## Compatibility Goals

- Installable in Codex as a plugin with bundled skills and hooks.
- Usable locally as repo-scoped Codex skills during development.
- Preserve existing Claude Code installation and behavior.
- Share one Python core across Claude Code and Codex.
- Allow the same Obsidian vault to contain both old Claude-generated notes and new Codex-generated notes.
- Avoid relying on Codex built-in slash commands for custom workflow names. Codex custom workflows should be invoked through skills, explicit skill mentions, or plugin-provided skills.

## Non-Goals

- No Obsidian plugin, MCP server, or REST service.
- No migration of existing notes from `claude/*` tags to new tags in the first release.
- No exact emulation of Claude Code transcript shape inside Codex hooks.
- No automatic import of all historical Claude sessions into Codex state. Codex already has its own `/import` path for supported external-agent artifacts.

## Proposed Repository Additions

### 1. Codex Plugin Manifest

Add:

```text
.codex-plugin/plugin.json
```

Minimal manifest:

```json
{
  "name": "obsidian-brain",
  "version": "3.2.1",
  "description": "Persistent Obsidian memory for coding-agent sessions.",
  "skills": "./skills-codex/"
}
```

Use `skills-codex/` rather than reusing `skills/` directly at first. The current `skills/` files are valid Markdown, but their command snippets assume Claude Code runtime paths and tool names. A separate folder prevents accidentally shipping Claude-specific instructions to Codex users.

Later, when skills have been normalized, `skills` can point to a shared folder or generated output.

### 2. Codex Local Marketplace

Add a repo-local marketplace for development:

```text
.agents/plugins/marketplace.json
```

Proposed entry:

```json
{
  "name": "obsidian-brain-local",
  "plugins": [
    {
      "name": "obsidian-brain",
      "source": {
        "source": "local",
        "path": "./"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
```

Verify whether `path: "./"` is accepted when the plugin root is the repo root. If Codex requires plugin folders under `plugins/`, use:

```text
plugins/obsidian-brain -> symlink or copied package root
```

and set `path` to `./plugins/obsidian-brain`.

### 3. Codex Repository Guidance

Add:

```text
AGENTS.md
```

This should be a Codex-focused equivalent of `CLAUDE.md`, covering:

- stdlib-only Python for hooks;
- `pytest` command;
- architecture artifacts update rule;
- security patterns;
- git-flow conventions;
- dual-runtime compatibility rules.

Do not rename `CLAUDE.md`; keep both files so each agent reads its native instructions.

### 4. Codex Hook Registration

Codex can load bundled plugin hooks from `hooks/hooks.json`, but the current file uses `${CLAUDE_PLUGIN_ROOT}`. Add a Codex-specific hook manifest:

```text
hooks/codex-hooks.json
```

or, if Codex plugin packaging only auto-discovers `hooks/hooks.json`, generate `hooks/hooks.json` per package during release. Do not make the shared `hooks/hooks.json` use Codex-only paths until Claude Code support is removed.

Codex hook commands should resolve the plugin root without `${CLAUDE_PLUGIN_ROOT}`. Options:

- Use a small installed wrapper script with an absolute path injected by packaging.
- Use `python3 -m obsidian_brain.hooks.session_log` after converting hooks into an importable package.
- Use `command` paths inside `.codex-plugin/plugin.json` if Codex manifest lifecycle config supports root-relative expansion in the installed bundle.

Recommended first implementation:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "python3 hooks/codex_session_hint.py",
            "statusMessage": "Loading Obsidian Brain context"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "manual|auto",
        "hooks": [
          {
            "type": "command",
            "command": "python3 hooks/codex_context_snapshot.py",
            "statusMessage": "Saving Obsidian Brain snapshot"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 hooks/codex_session_log.py",
            "statusMessage": "Saving Obsidian Brain session"
          }
        ]
      }
    ]
  }
}
```

Open question: whether plugin-bundled relative hook commands execute from the plugin root or the session `cwd`. The Codex manual says commands run with the session `cwd`; therefore plain relative paths are unsafe unless the plugin loader rewrites them. Treat this as a packaging test requirement before enabling lifecycle auto-logging.

## Runtime Adapter Design

Add a small provider abstraction rather than scattering `~/.claude` and transcript assumptions.

### New Module

```text
hooks/runtime_provider.py
```

Responsibilities:

- detect runtime: `claude`, `codex`, or `unknown`;
- return config path;
- return state directory;
- return vault DB path;
- return transcript/session root when available;
- normalize hook stdin into a provider-neutral `SessionEvent`;
- locate installed plugin resources for skill snippets.

Sketch:

```python
@dataclass
class SessionEvent:
    provider: str
    event: str
    session_id: str
    cwd: str
    transcript_path: str | None
    source: str | None
    raw: dict
```

Path policy:

- Claude default: existing `~/.claude/...`.
- Codex default: `CODEX_HOME` or `~/.codex`, then:
  - config: `~/.codex/obsidian-brain-config.json`
  - state: `~/.codex/obsidian-brain/`
  - DB: `~/.codex/obsidian-brain-vault.db`
- Environment overrides:
  - `OBSIDIAN_BRAIN_CONFIG`
  - `OBSIDIAN_BRAIN_STATE_DIR`
  - `OBSIDIAN_BRAIN_DB`

Then change `obsidian_utils.py` to consume provider paths from this module. Keep backward compatibility by defaulting to Claude paths when no runtime can be detected and existing Claude config exists.

## Transcript and Tool Normalization

Current parsing supports Claude Code JSONL and a simple flat fallback. Codex compatibility needs explicit adapters.

Add:

```text
hooks/transcripts.py
```

or extend `obsidian_utils.py` with isolated functions:

- `parse_claude_transcript(path) -> list[NormalizedMessage]`
- `parse_codex_transcript(path) -> list[NormalizedMessage]`
- `normalize_tool_name(provider, name) -> str`

Normalized message shape:

```python
{
  "role": "user|assistant|system|tool",
  "text": "...",
  "timestamp": "...",
  "tool_uses": [
    {"name": "Bash", "detail": "..."}
  ],
  "raw": {...}
}
```

Codex tool mapping should include at least:

- `functions.exec_command` -> `Bash`
- `functions.apply_patch` -> `Edit`
- `web.run` -> `WebSearch` or `WebFetch` depending on command
- `functions.view_image` -> `Read`
- MCP tool names preserved with `mcp:` prefix or grouped as `MCP`

Do not block Codex support on perfect transcript reconstruction. The first Codex release can log:

- user prompts;
- assistant final/update text if available from Codex transcript;
- shell commands and patches if present;
- cwd, branch, duration, files touched.

## Vault Schema Strategy

### Phase 1: Backward-Compatible Claude Schema

Keep writing:

- folders: `claude-sessions`, `claude-insights`, `claude-dashboards`, `claude-check-items`;
- tags: `claude/session`, `claude/project/<name>`, `claude/auto`;
- types: `claude-session`, `claude-insight`, etc.

Add frontmatter:

```yaml
agent_provider: codex
agent_session_id: <id>
```

For Claude Code notes, either omit these fields or add `agent_provider: claude` only when notes are touched or newly written.

This preserves existing Dataview dashboards and search.

### Phase 2: Optional Neutral Schema

Add config:

```json
{
  "schema_prefix": "claude",
  "agent_provider": "codex"
}
```

Allowed `schema_prefix` values:

- `claude` for existing vault compatibility;
- `agent` for new neutral vaults.

Neutral tags would be:

- `agent/session`
- `agent/insight`
- `agent/project/<name>`

This must be opt-in because dashboards, tests, and user queries currently assume `claude/*`.

## Codex Skills Port

Create `skills-codex/` with a narrow initial set:

```text
skills-codex/obsidian-setup/SKILL.md
skills-codex/recall/SKILL.md
skills-codex/vault-search/SKILL.md
skills-codex/vault-ask/SKILL.md
skills-codex/decide/SKILL.md
skills-codex/error-log/SKILL.md
skills-codex/compress/SKILL.md
```

Porting rules:

- Replace Claude Code slash-command framing with Codex skill invocation framing.
- Replace `~/.claude/plugins/cache/*/obsidian-brain/*/hooks` lookup with a deterministic helper:
  - local repo: `hooks/`;
  - Codex plugin install: path resolved by wrapper or `OBSIDIAN_BRAIN_PLUGIN_ROOT`;
  - fallback: `~/.codex/plugins/**/obsidian-brain/**/hooks` only if verified.
- Replace `TaskCreate`/`TaskUpdate` instructions with ordinary progress updates. Codex has goals and plans, but skills should not depend on Claude-specific task tools.
- Replace `Agent(...)` instructions with Codex subagent instructions only after subagent behavior is verified. For initial release, prefer deterministic Python helpers over subagent fallback.
- Replace `claude -p` summarization with one of:
  - in-session Codex summarization written back by the skill;
  - `codex exec` only if noninteractive invocation and auth behavior are verified;
  - no subprocess summarization in hooks.
- Replace references to `/context-shield` and `/conversation-search` with Codex-native skills or explicit helper scripts.

Priority order:

1. `obsidian-setup`: must write Codex config paths and create the same vault folders.
2. `vault-search`: mostly Python/FTS and easiest to validate.
3. `recall`: read-only context brief, with summarization fallback simplified.
4. `decide`, `error-log`, `compress`: write curated notes.
5. `vault-ask`, `standup`, `emerge`, `check-items`: more complex retrieval/synthesis.
6. `vault-import`: last, because source transcript/import semantics differ most.

## CLI Helpers

The current repo relies on long shell snippets embedded in skills. Codex compatibility is a chance to reduce prompt fragility.

Add a stable CLI:

```text
hooks/obsidian_brain_cli.py
```

Commands:

```bash
python3 hooks/obsidian_brain_cli.py config
python3 hooks/obsidian_brain_cli.py setup --vault PATH --provider codex
python3 hooks/obsidian_brain_cli.py recall --project PROJECT
python3 hooks/obsidian_brain_cli.py search --query QUERY
python3 hooks/obsidian_brain_cli.py write-decision ...
python3 hooks/obsidian_brain_cli.py hook-session-start
python3 hooks/obsidian_brain_cli.py hook-pre-compact
python3 hooks/obsidian_brain_cli.py hook-stop
```

Skills should call this CLI instead of importing `obsidian_utils` through cache globbing.

## Testing Plan

### Unit Tests

Add tests for:

- provider path resolution with `HOME`, `CODEX_HOME`, and environment overrides;
- config loading from old Claude path and new Codex path;
- Codex hook stdin normalization;
- transcript parser with synthetic Codex JSONL;
- tool-name normalization;
- writing Codex-origin session notes while preserving existing frontmatter fields;
- schema prefix behavior.

### Integration Tests

Add a test fixture:

```text
tests/fixtures/codex-sessions/basic-session.jsonl
```

Add test scripts:

```text
scripts/test-codex-plugin-manifest.sh
scripts/test-codex-hooks.sh
scripts/test-codex-skill-snippets.py
```

Checks:

- `.codex-plugin/plugin.json` is valid JSON and has `skills`;
- `.agents/plugins/marketplace.json` is valid JSON;
- Codex hook manifest is valid JSON;
- all `skills-codex/*/SKILL.md` files have required frontmatter;
- no `skills-codex` file contains `~/.claude/plugins/cache`, `${CLAUDE_PLUGIN_ROOT}`, `TaskCreate`, `TaskUpdate`, or `claude -p` unless explicitly allowlisted.

### Manual Validation

1. Install the local Codex plugin from the repo marketplace.
2. Restart Codex and confirm `/plugins` shows `obsidian-brain`.
3. Confirm `$obsidian-setup` appears under `/skills`.
4. Run setup against a temp Obsidian vault.
5. Run `$vault-search` and `$recall` in a repo with seed notes.
6. Trust Codex hooks via `/hooks`.
7. Start, compact, and stop a session; verify session/snapshot notes are written.
8. Open the vault in Obsidian and verify Dataview dashboards still render.

## Release Plan

### Phase 0: Spec and Scaffolding

- Add this spec.
- Add `AGENTS.md`.
- Add `.codex-plugin/plugin.json`.
- Add `.agents/plugins/marketplace.json`.
- Add validation tests for manifests.

### Phase 1: Shared Runtime Paths

- Add `runtime_provider.py`.
- Add provider-aware config/state/DB paths.
- Preserve old Claude defaults.
- Add tests for path resolution.

### Phase 2: Codex Read Skills

- Add `skills-codex/obsidian-setup`, `vault-search`, and `recall`.
- Add `obsidian_brain_cli.py` for deterministic snippets.
- Validate against a temp vault.

### Phase 3: Codex Write Skills

- Add Codex versions of `compress`, `decide`, and `error-log`.
- Include `agent_provider: codex` frontmatter.
- Keep `claude/*` tags by default.

### Phase 4: Codex Lifecycle Hooks

- Add Codex hook wrappers.
- Verify hook command path resolution in an installed plugin.
- Add `SessionStart`, `PreCompact`, and `Stop` support.
- Only then enable auto-logging in the Codex package.

### Phase 5: Advanced Skills

- Port `vault-ask`, `standup`, `emerge`, `check-items`, `vault-doctor`, `vault-stats`, and `vault-import`.
- Replace Claude-only subagent and subprocess patterns with Codex-native or CLI-helper implementations.

## Risks and Open Questions

- Codex plugin-bundled hook command path resolution needs empirical validation. The manual states commands run in session `cwd`; plugin-relative command rewriting is not guaranteed by the snippets read for this spec.
- Codex transcript file location and schema need fixture capture from a real Codex session. Do not assume Claude Code JSONL compatibility.
- Existing dashboards are named `claude-*`; keeping them avoids migration pain but makes the Codex UX look Claude-branded in Obsidian.
- `claude -p` is deeply embedded in summarization and check-items flows. These should move behind a provider-neutral summarization helper.
- Codex skills are selected through `/skills` or explicit `$skill` mentions, not arbitrary custom slash commands. Documentation must teach `$recall` or plugin skill invocation rather than `/recall`.

## Acceptance Criteria

- Claude Code install and tests continue to pass unchanged.
- Codex can install the plugin locally and discover at least setup/search/recall skills.
- A Codex setup run writes config under `CODEX_HOME`/`~/.codex` without touching `~/.claude`.
- Codex-origin notes are searchable by existing vault search and visible in current dashboards.
- No Codex skill depends on Claude Code plugin cache paths.
- Lifecycle hook support is disabled or clearly experimental until installed-plugin path resolution and transcript schema are verified.
