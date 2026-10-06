# Codex invocation

Select `OB_HOST=codex` and the actual client `OB_CLIENT=codex-cli` or
`OB_CLIENT=codex-desktop`. Use the authoritative `CODEX_THREAD_ID` as
`OB_SESSION_ID`, or an explicit native ID supplied by the invoking client.
An inherited Claude session variable does not identify this Codex session.
Use the absolute loaded skill path and native working directory.

Use native Codex progress and delegated analysis tools when available.
Request required user decisions through the native in-turn interaction tool;
when unavailable, preserve pending work and report that the confirmation
capability is unavailable. Do not fabricate approval. Bind source revisions
before analysis. Helpers return content; the parent applies changes through
the installed launcher. Do not switch to Claude when a capability fails.

Recall never checks off open items. Revision-bound summaries leave conflicts pending.

Track shared progress tasks with native progress updates or a concise task list.
Use native delegated analysis only when available and authorized. If absent,
run that analysis inline with the same shared prompt and output schema. User
approval remains explicit; a missing tool is never treated as approval.
