# Codex doctor capabilities

Memory index: unsupported:no equivalent native memory-file API. Report this explicitly; never read another host’s memory directory.

Session coverage: use the Codex normalized source adapter. Existing Claude reconstruction does not establish Codex support. Main Task6 doctor owner must supply bounded Codex coverage/reconstruction evidence before this capability is marked supported.

Read the operator-declared, inherited `OB_CLIENT`. It must be `codex-cli`
or `codex-desktop`; if missing or invalid, stop with "Current native client binding
is unavailable". Never set or export `OB_CLIENT` yourself. Do not default Desktop
to CLI or infer the frontend from a transcript header or environment markers.

Context, metadata and local operations use the ordinary workspace sandbox.
Native AI may also need SQLite writes under the selected private `CODEX_HOME`
and network access to the declared provider API. The user must first authorize
that provider, the exact payload and the writes.

If the outer tool sandbox blocks an authorized native AI operation, request
normal one-time tool approval for the exact loaded `brain_cli.py` operation and
its explicit approved payload (synthetic for acceptance tests). Keep the same
bound context, source revisions and declared model. Do not invent approval,
override the model, switch to Claude, change global sandbox or network policy,
relax backend tool restrictions, or raise deadlines. This permission applies
to that command; it is not a general extra-directory grant.
