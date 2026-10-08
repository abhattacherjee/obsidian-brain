# Codex doctor capabilities

Memory index: unsupported:no equivalent native memory-file API. Report this explicitly; never read another host’s memory directory.

Session coverage: use the Codex normalized source adapter. Existing Claude reconstruction does not establish Codex support. Main Task6 doctor owner must supply bounded Codex coverage/reconstruction evidence before this capability is marked supported.

Read the operator-declared, inherited `OB_CLIENT`. It must be `codex-cli`
or `codex-desktop`; if missing or invalid, stop with "Current native client binding
is unavailable". Never set or export `OB_CLIENT` yourself. Do not default Desktop
to CLI or infer the frontend from a transcript header or environment markers.
