# Codex invocation

Use `OB_HOST=codex`. Read the operator-declared, inherited `OB_CLIENT`.
It must be `codex-cli` or `codex-desktop`; if missing or invalid, stop with
"Current native client binding is unavailable". Never set or export
`OB_CLIENT` yourself. The operator must declare it before launching the native
client. Use the authoritative `CODEX_THREAD_ID` as `OB_SESSION_ID`, or an
explicit native ID supplied by the invoking client.
An inherited Claude session variable does not identify this Codex session.
Use the absolute loaded skill path and native working directory.

Use native Codex progress and delegated analysis tools when available.
Request required user decisions through the native in-turn interaction tool;
when unavailable, preserve pending work and report that the confirmation
capability is unavailable. Do not fabricate approval. Bind source revisions
before analysis. Helpers return content; the parent applies changes through
the installed launcher. Do not switch to Claude when a capability fails.

Use this host's native permission and hook-trust controls. Pair only shared
vault/schema/index settings; do not copy another host's AI or permission settings.

Track shared progress tasks with native progress updates or a concise task list.
Use native delegated analysis only when available and authorized. If absent,
run that analysis inline with the same shared prompt and output schema. User
approval remains explicit; a missing tool is never treated as approval.

The invoking runtime must explicitly supply the current client. If it does not,
stop with "Current native client binding is unavailable". Do not default Desktop
to CLI or infer the frontend from a transcript header or environment markers.

Registered Desktop hooks additionally require the verified local stdio setup in
`docs/parity/runtime-contracts.md` from the matching source checkout. The operator
must install the reviewed app-only profile, run the protected observer, and launch
Desktop afresh with its declaration. A declaration, a trusted hook listing, or an
old attestation alone is insufficient. Do not restart the app or install policy
from a skill. Missing proof stays pending; do not substitute a CLI invocation.

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
