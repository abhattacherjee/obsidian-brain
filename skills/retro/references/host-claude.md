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

The parent runs preview, save, classification confirmation, and filing inline.
Every eligible snapshot remains evidence; preserve the blocking classification gate.

Shared native progress tasks map to `TaskCreate`/`TaskUpdate`; native analysis
helpers map to `Agent`; native user decisions map to `AskUserQuestion`. The
shared procedure supplies the prompt, expected JSON, and preview/save gate.

A Claude continuation preamble may cite `~/.claude/projects/<project-dir>/<session-id>.jsonl`. Its complete basename identifies the prior Claude session. With no such proven identity, pass no extra evidence IDs.
