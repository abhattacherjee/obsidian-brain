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

Recall never checks off open items. Revision-bound summaries leave conflicts pending.

Shared native progress tasks map to `TaskCreate`/`TaskUpdate`; native analysis
helpers map to `Agent`; native user decisions map to `AskUserQuestion`. The
shared procedure supplies the prompt, expected JSON, and preview/save gate.

## Claude batch scheduling

> **Config escape hatch (#84):** If `PIPELINE=subagent`, SKIP Phase 1 (the `upgrade_batch` Haiku `claude -p` pipeline) entirely and treat ALL N notes as the Phase 2 fallback list — route every note directly to the sub-agent path in Phase 2. This is for machines where `claude -p` cold-start latency exceeds the timeout budget (the Haiku pipeline would waste ~2-4 min/note on doomed timeouts). When `PIPELINE=auto` (default), proceed with Phase 1 as written below.

> **Why a single Bash call, not N parallel calls?** The Claude Code harness serializes parallel Bash tool calls through a limited shell pool when each subprocess blocks on I/O (e.g., `claude -p --model haiku` taking 5-30s). Dispatching 10 Bash calls in one message still executes them one at a time — wall time ≈ Σ per-call. Pushing fan-out into a single Python process with `ThreadPoolExecutor` gives true concurrency (the GIL releases during subprocess waits), so wall time ≈ max per-call. See `claude-insights/2026-04-21-recall-parallel-bash-dispatch-runs-sequentially-fbee-error.md` and GH #69.
