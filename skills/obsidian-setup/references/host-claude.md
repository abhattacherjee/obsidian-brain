# Claude invocation

Use `OB_HOST=claude` and the fixed host client `OB_CLIENT=claude-code`.
Claude Code has one frontend; this constant matches its hook registration.
No pre-launch declaration is required. If an inherited `OB_CLIENT` is present
and differs from `claude-code`, including an empty value, stop with
"Current native client binding is unavailable". Never overwrite a conflicting
declaration. Use the authoritative `CLAUDE_CODE_SESSION_ID` as `OB_SESSION_ID`.
An inherited `CODEX_THREAD_ID`
does not identify this Claude session. Use the absolute loaded skill path and
native working directory; do not search marketplace caches.

Use Claude's native progress, Agent, and AskUserQuestion tools for the shared
procedure's progress, delegated analysis, and required confirmation steps.
Read and bind source revisions before delegating analysis. Agents return
analysis or summaries; the parent applies vault changes through the launcher.
Missing native capability is reported rather than switching providers.

Use this host's native permission and hook-trust controls. Pair only shared
vault/schema/index settings; do not copy another host's AI or permission settings.

### Step 1.5 — Permission pre-flight check

Before any out-of-workspace writes, test whether Claude Code can write to `~/.claude/`:

```bash
echo "test" > ~/.claude/.obsidian-brain-canary 2>&1 && rm -f ~/.claude/.obsidian-brain-canary && echo "OK" || echo "FAIL"
```

If **OK**: proceed silently to Step 2.

If **FAIL**: present the following message using AskUserQuestion:

> **Heads up — setup needs write access outside this project directory.**
>
> Obsidian Brain writes config to `~/.claude/` and notes to your Obsidian vault. Your current Claude Code permissions block writes outside the working directory.
>
> Choose how to fix this:
>
> 1. **Switch permission mode (recommended)** — Press `Shift+Tab` to switch to "accept edits" mode for this session. Or use `/config` to change `permissions.defaultMode` permanently. Then re-run `/obsidian-setup`.
>
> 2. **Whitelist paths permanently** — Add `$HOME/.claude` and your vault's parent directory to `sandbox.filesystem.allowWrite` in `~/.claude/settings.json`. **Use absolute paths** — `~` is not expanded inside JSON string values:
>    ```json
>    {
>      "sandbox": {
>        "filesystem": {
>          "allowWrite": ["/Users/you/.claude", "/Users/you/Documents/vault-parent"]
>        }
>      }
>    }
>    ```
>    Replace `/Users/you` with your actual home directory (run `echo $HOME` to find it). Then re-run `/obsidian-setup`.
>
> 3. **I'll handle it myself** — Continue setup and approve or fix writes as they come up.

**Behavior per option:**
- **Option 1:** Print the instruction, then stop. User changes mode and re-runs `/obsidian-setup`.
- **Option 2:** Print the JSON snippet with absolute paths. In upgrade mode (`MODE=upgrade`), substitute the known vault parent path from the existing config. In fresh mode, show only the `$HOME/.claude` entry with a note that the vault parent must be added after the user provides the vault path. Then stop. User edits settings and re-runs.
- **Option 3:** Continue with setup as normal. Writes may fail and the user deals with each one.


### Step 9 — Configure skill-kit:extract nudge (idempotent)

Check if the skill-kit:extract-to-compress nudge (rule file and name keep the old `claudeception` spelling) is already configured **globally** (in `~/.claude/`, not the project `.claude/`):

```bash
test -f ~/.claude/hookify.claudeception-compress-nudge.local.md && echo "EXISTS" || echo "MISSING"
```

If EXISTS, skip this step — the nudge is already configured.

If MISSING, write the hookify rule file directly to `~/.claude/` using the Write tool:

**File: `~/.claude/hookify.claudeception-compress-nudge.local.md`**

```markdown
---
name: claudeception-compress-nudge
enabled: true
event: stop
pattern: Result:\s*PASS|\.claude/skills/[^/]+/SKILL\.md|created skill|skill file written|extracted knowledge
action: warn
---

💡 **skill-kit:extract (was claudeception) extracted knowledge from this session.** Run `/compress` to save it to your Obsidian vault.
```

**Important:** This rule MUST be in `~/.claude/` (global), not the project's `.claude/` directory. The nudge should trigger in any project where skill-kit:extract runs, not just obsidian-brain.

This is a soft nudge — a non-blocking suggestion, not automatic execution.


Shared native progress tasks map to `TaskCreate`/`TaskUpdate`; native analysis
helpers map to `Agent`; native user decisions map to `AskUserQuestion`. The
shared procedure supplies the prompt, expected JSON, and preview/save gate.
