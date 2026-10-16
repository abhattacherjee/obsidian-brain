# Disposable Linux native observer

This observer traces a native CLI and its descendants. It requires no host
installation, `SYS_PTRACE`, privileged container or credential copying. Build
from a reviewed single-provider image. Only the disposable image installs strace.
The runtime runs as UID 501 with all capabilities dropped.

```sh
python3 scripts/dev-test/native-observer/run.py build \
  --base-image obp-linux-codex:f89edd71 --image obp-linux-codex-observer:f89edd71
mkdir -m 700 -p /Users/<operator>/<private-campaign>/observer-controls/codex
python3 scripts/dev-test/native-observer/run.py control \
  --image obp-linux-codex-observer:f89edd71 \
  --root /Users/<operator>/<private-campaign>/observer-controls/codex
```

Repeat for the Claude-only image. The foreground control closes stdin, has a
timeout and disables networking. It checks descendant exec, fixture file-open
and a refused localhost socket connection. It also checks actual UID/GID,
private permissions and a fixture directory enumeration linked to its open FD.
Receipts are synthetic controls;
they do not establish native acceptance or readiness on another image.
Choose a directory shared into Colima. On this host `/Users` is shared and
`/private/tmp` is not. The launcher checks a private sentinel before tracing;
an empty VM directory is refused. Export control receipts to private temporary
storage afterward. Keep control fixtures outside the candidate package.

For a prepared Linux run, authenticate through the client's normal private
login flow first. Then the operator starts the native session:

```sh
python3 scripts/dev-test/native-observer/run.py native \
  --image <reviewed-observer-image> --manifest <private-run-manifest.json> \
  --package <reviewed-candidate-package-directory> \
  --source-checkout <repository-containing-the-candidate-commit>
```

The launcher mounts only that private sandbox and a read-only package. It
records selected paths before launch, uses `--no-daemon` for Codex, and keeps
native trust and approval settings unchanged. Paste the approved executor prompt
in the session. Login does not belong in the syscall trace.

Before launch, the manifest must record all four execution approvals, their human
authorization source, a full candidate commit and the operator's prior client
binding. Codex requires `binding_method: operator-launch-declaration`; Claude
uses `binding_method: fixed-claude-client` and needs no operator-selected frontend.
The complete package inventory and every Git blob must match that
commit in `--source-checkout`; extra files, changed bytes and symlinks are refused.
Codex's pre-launch receipt must have `kind` equal to
`trusted-pre-launch-declaration`, matching `client`, `binding_method`,
`candidate_sha`, `declared_by` and `environment_allowlist`, plus `declared_at`.
Claude's context receipt uses `kind: trusted-native-context`, the fixed binding
method, candidate commit and matching selections; no frontend declaration is
required. Each allowlist contains `OB_CLIENT`,
`HOME`, the selected config/DB/state paths, XDG state/data paths and the native
home. The executor must independently check the inherited context after launch.
The launcher cannot turn an absent operator declaration into native proof.

The container receives `HOME=<sandbox>/home` deliberately. This is a child
environment selection, not a shell variable used for another purpose. Fixed-home
writes remain in the mounted sandbox for the before/after protected-path audit.
Both native and control strace processes start under umask 077; traces are 0600
and private home/evidence directories are 0700.

Docker's capability and seccomp policy may prevent the client's command sandbox
from working. `native-sandbox-status.json` starts BLOCKED. The executor must record
the actual client sandbox settings, a sandboxed command, its client-owned result
and stderr, and any environment difference. A container exit does not verify the
client sandbox. Never disable or weaken it to make a case pass. The launcher saves
exit status and trace hash in `observer-completion.json` after normal completion.

Process/file/socket/connect events include paths, exec arguments and connection
metadata. `getdents`/`getdents64` use raw FD/pointer/count/result fields, preserving
directory-scan evidence without decoding directory entries. `close` and
`dup`/`dup2`/`dup3` record FD lifecycle metadata. Attribute enumeration by PID and
the preceding directory-open result, accounting for FD inheritance and reuse.
An FD copied through an untraced mechanism, or otherwise unattributed, makes the
affected scan assertion BLOCKED; these controls do not certify every FD path.
Syscall payloads and environment values are not traced. Treat raw
traces as private, inspect before sharing, and export only allowlisted synthetic
evidence. Identify native hook descendants separately from legitimate foreground
AI. Check trace exit status, positive controls and native client receipts before
claiming coverage. A sandbox failure or missing trace is BLOCKED, not a PASS.

No detached container or persistent service is created. Docker `--rm` removes
the container on normal exit. Keep the private evidence directory for review.
