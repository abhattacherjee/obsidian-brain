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

Before any out-of-workspace writes, test the selected Claude home. Use
`CLAUDE_CONFIG_DIR` when set; otherwise use `$HOME/.claude`. Do not test or
change the default home when a custom home is selected.

```bash
python3 - <<'PY_CANARY'
import os
import pathlib
import tempfile
home = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR", str(pathlib.Path.home() / ".claude")))
try:
    if not home.is_absolute():
        raise ValueError("Claude home must be absolute")
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(prefix=".obsidian-brain-canary-", dir=home) as canary:
        canary.write(b"test")
        canary.flush()
    print("OK")
except (OSError, ValueError):
    print("FAIL")
PY_CANARY
```

If **OK**: proceed silently to Step 2.

If **FAIL**: present the following message using AskUserQuestion:

> **Heads up — setup needs write access outside this project directory.**
>
> Obsidian Brain writes config to the selected Claude home and notes to your Obsidian vault. Your current Claude Code permissions block writes outside the working directory.
>
> Choose how to fix this:
>
> 1. **Switch permission mode (recommended)** — Press `Shift+Tab` to switch to "accept edits" mode for this session. Or use `/config` to change `permissions.defaultMode` permanently. Then re-run `/obsidian-setup`.
>
> 2. **Whitelist paths permanently** — Add the selected Claude home and your vault's parent directory to `sandbox.filesystem.allowWrite` in that home's `settings.json`. **Use absolute paths** — `~` is not expanded inside JSON string values:
>    ```json
>    {
>      "sandbox": {
>        "filesystem": {
>          "allowWrite": ["/Users/you/.claude", "/Users/you/Documents/vault-parent"]
>        }
>      }
>    }
>    ```
>    Replace the example Claude path with the absolute selected home (`CLAUDE_CONFIG_DIR`, or `$HOME/.claude` when unset). Then re-run `/obsidian-setup`.
>
> 3. **I'll handle it myself** — Continue setup and approve or fix writes as they come up.

**Behavior per option:**
- **Option 1:** Print the instruction, then stop. User changes mode and re-runs `/obsidian-setup`.
- **Option 2:** Print the JSON snippet with absolute paths. In upgrade mode (`MODE=upgrade`), substitute the known vault parent path from the existing config. In fresh mode, show only the selected Claude home entry with a note that the vault parent must be added after the user provides the vault path. Then stop. User edits settings and re-runs.
- **Option 3:** Continue with setup as normal. Writes may fail and the user deals with each one.


### Step 9 — Configure skill-kit:extract nudge (idempotent)

The installed hookify rule loader reads project-relative `.claude/` rules. It
does not discover rules in the global Claude home. Configure this optional
nudge for the selected project only; repeat setup in another project if needed.
Use the explicit native `OB_CWD`, never an unrelated shell directory.
The existing rule name keeps the old `claudeception` spelling.

```bash
python3 - "$OB_CWD" <<'PY_NUDGE'
import sys
import pathlib
project = pathlib.Path(sys.argv[1])
if not project.is_absolute() or not project.is_dir():
    raise ValueError("Native working directory must be an existing absolute path")
folder = project / ".claude"
if folder.is_symlink():
    raise ValueError("Project rule directory cannot be a symbolic link")
folder.mkdir(exist_ok=True)
rule = folder / "hookify.claudeception-compress-nudge.local.md"
if rule.is_symlink():
    raise ValueError("Project rule cannot be a symbolic link")
try:
    with rule.open("x", encoding="utf-8") as output:
        output.write('---\nname: claudeception-compress-nudge\nenabled: true\nevent: stop\npattern: Result:\\s*PASS|\\.claude/skills/[^/]+/SKILL\\.md|created skill|skill file written|extracted knowledge\naction: warn\n---\n\n💡 **skill-kit:extract (was claudeception) extracted knowledge from this session.** Run `/compress` to save it to your Obsidian vault.\n')
    print("CREATED")
except FileExistsError:
    print("EXISTS")
PY_NUDGE
```

Existing project rules are preserved. This writes no global rule or native
settings. If hookify is unavailable, report that this optional nudge cannot run.

This is a soft nudge — a non-blocking suggestion, not automatic execution.


Shared native progress tasks map to `TaskCreate`/`TaskUpdate`; native analysis
helpers map to `Agent`; native user decisions map to `AskUserQuestion`. The
shared procedure supplies the prompt, expected JSON, and preview/save gate.
