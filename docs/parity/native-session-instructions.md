# Instructions for a fresh native test session

Copy the prompt below into the actual client under test. Attach or provide an
absolute path to this document, the test plan and the filled private run
manifest. A client name in the prompt declares the target; it does not prove
the runtime's current-client binding.

## Prepare a campaign without filling paths by hand

Give a preparation session this prompt first. It creates the manifests and
client-specific launch instructions; the executors still start fresh afterward.

```text
Prepare an Obsidian Brain native acceptance campaign from native-test-plan.md.
Candidate SHA: <explicit full SHA selected by me; no default>
Clients/platforms: <three clients on macOS, both CLIs on Linux; name any subset>
Source checkout: <absolute checkout path>

Create a random private campaign directory and one sandbox/run manifest per
selected cell using native-run-manifest.template.json. Derive absolute paths
yourself, verify containment, and include a path-with-spaces case. Create a
read-only package copy from the exact selected commit, record its source
hashes, and retain the test instructions separately. Do not install a moving
branch or working-tree code as the pinned candidate.

Inspect current native help/settings and installed plugin metadata to find
supported isolated-home/cache and launch mechanisms. Do not guess flags or
change a running Desktop daemon's environment to manufacture isolation. If a
client cannot select the needed sandbox resources, save that blocker and
prepare other clients. Do not install into or alter my default native home.
Use only a selected scratch cache; leave existing authentication private and
use the client's supported authentication flow if a fresh home needs it.

Before scheduling, fill execution_environment with named machines/VMs/accounts,
who provides each, access/authentication method (no secrets) and provider
inventory. Linux and single-provider environments must actually exist; a
separate home or PATH mask cannot stand in for them. Mark unavailable cells
BLOCKED up front. Initial required scope is CC-M/CX-M/CD-M/CC-L/CX-L;
Linux Desktop is out of scope per issue272's Acceptance gates.

Settle Desktop isolation with the operator before any run: supported quit and
relaunch against sandbox native selections, or a dedicated macOS account/VM.
Record the supported method; ask before interrupting the user's running app.
X.1 must use the same sandbox CODEX_HOME/thread store for CLI and Desktop.
If neither method works, mark CD-M/X.1 BLOCKED rather than using the live home.

For Claude, record binding_method: fixed-claude-client in declared_client_launch.
Its client is the fixed host constant claude-code, matching the registered hook;
no pre-launch declaration is required. Reject conflicting inherited OB_CLIENT,
including an empty declaration. Record the accepted host-constant decision and
still prove actual installed dispatch and the full native session ID.
For Codex, record binding_method: operator-launch-declaration and the trusted
pre-launch OB_CLIENT declaration with its owner/receipt. The executor reads,
never creates, that value: codex-cli for CLI or codex-desktop for Desktop.
Verify that the installed Codex hook passes the declaration as --client.
Codex K/A positive rows stay BLOCKED until B.2 verifies actual transport.
Fill authorization_source from the actual operator instruction. Never export
an entire environment or authentication values as launch evidence.

Preselect independent native-origin and no-AI/no-scan observers, permissions,
coverage limits and positive controls. Inventory protected live paths before
fixture invocation, excluding auth/secret stores. Exactly one candidate plugin
must be enabled. Record per-method covered cases, controls and receipt paths in
native_origin_observer.methods, separate from hook_no_ai_scan_observer. Record
case-specific prerequisites in case_readiness. An unobserved boundary blocks
that assertion, not unrelated cases whose own origin/isolation proof passes.
B.1 needs package/private paths and the complete protected baseline. B.2 needs a
positively controlled client-owned dispatch/debug record or external observer.
After B.2 and the protected audit pass, K/A may use independently collected
client-owned loaded-skill/tool/backend receipts with their own revision and
isolation proof. F.5 still needs
hook-descendant process/file/network coverage; L.5 still needs full external
timings. Actor-written receipts and synthetic calls never prove native origin.
A detected leak stops mutating cases and preserves evidence; never clean up
user data.

Resolve baseline v3.8.1 to its immutable release SHA/package bytes. Prepare
separate Claude baseline/candidate runs and an authority-cited agreed-change
allowlist. Name an actual Obsidian/Dataview sandbox for dashboard query tests;
if unavailable mark those tests BLOCKED. Build AI inventory from the selected
source, including all six fixed operations and every authored analysis path.

Create the deterministic synthetic fixture and its before-hash manifest,
with a separate explicit DB and state directory. Native-client installation
may be prepared only inside the selected scratch resources. Do not run native
acceptance cases in this preparation session or mark anything passed.

Leave fork_note_policy and retired_source_policy unset unless a recorded
human decision supplies them. Carry my requested platform scope accurately;
do not mark an unavailable client/platform N/A without a scope decision.

Output the campaign root, each manifest path, supported launch command or
operator UI steps, required trust/auth actions, and the exact executor prompt
for each new session. Keep secrets out of those outputs. My later execution
instruction authorizes fixture mutations; do not prefill approved_actions as
true before that instruction. No GitHub writes, merges or background services.
```

The native executor may fill `approved_actions` from the actual subsequent
execution instruction. It must retain that authorization's source, rather
than treating the template's booleans as independent permission.

## Executor prompt

```text
Execute the Obsidian Brain native parity acceptance plan.

Inputs:
- PLAN: <absolute path to docs/parity/native-test-plan.md>
- RUN_MANIFEST: <absolute path to this run's filled run-manifest.json>
- MODE: native | paired | observe-handoff
- HANDOFF: <absolute path if resuming an observer/fork/exit check; otherwise none>

Read PLAN, RUN_MANIFEST, the repository AGENTS.md/CLAUDE.md, and the loaded
plugin's relevant SKILL.md files before execution. First record protected-path
and fixture before-hashes, and execute B.1/B.2. Only after native binding is
verified may vault-ask search the selected test vault for prior test findings.
Its access-log writes belong after that baseline. If the vault is empty or
binding blocked, record that outcome; never fall back to my live vault.

This instruction authorizes the plan's listed fixture writes, synthetic
analysis through this client's existing backend, and install/restore only
inside the manifest's verified sandbox/cache. It does not authorize writes
to my personal vault, default DB, global plugin cache or shared config, nor
GitHub checkbox changes, publishing credentials, changing product code,
merging a PR, installing unrelated tools, or bypassing a tool restriction.

First prove candidate SHA, installed package identity, exactly one enabled
plugin, environment readiness and audited sandbox selections. Read the trusted
binding method. Claude uses the fixed host constant claude-code, matching its
hook registration, and rejects conflicting inherited declarations. Record the
accepted host-constant decision; it does not prove native dispatch by itself.
Codex requires the trusted pre-launch OB_CLIENT declaration; do not set it from
the manifest or choose a frontend yourself. Agent-set Codex values make
exploratory probes synthetic.
Codex K/A positive rows stay BLOCKED
until B.2 verifies actual supported hook transport. Test B.1/B.2 before the
long suite; do not change hook commands or infer frontend from creation headers.
Native evidence must match installed handler/operation receipt, client-owned
session source with marker/hash/offset, result provenance and independent
origin observation. Manual calls cannot substitute, even with the same ID.

Use the absolute loaded skill path and its documented launcher. Repeat all
explicit context values for each shell invocation. Resolve helpers from that
loaded installation, not cwd, newest cache, another provider or this source
checkout. Native client/session IDs must come from this invocation's verified
runtime handoff. A manifest label is not a native transport receipt.

Use normal client lifecycle controls. Before compact, clear, fork or exit,
save handoff.json with current case, session identity/provenance, canaries,
fixture/output hashes, next assertion and exact evidence directory. Ask me
only for the specific native control that cannot be performed safely by you.
Do not end with a generic progress update while independent work remains.

When a skill presents a preview, show it. My instruction covers the listed
synthetic fixture mutations within the sandbox. If the loaded skill requires
an explicit interactive response you do not have, save that pending preview
in handoff.json, ask a concise question and continue independent tests.
Do not pretend I replied or silently drop the save step.

Run the plan in order and write one row per case, installed skill and AI path.
Cover every fixed backend operation and authored analysis branch from the
selected-source inventory, not just one successful model call. Compare released
Claude baseline/custom-folder/dashboard behavior with the candidate. Use the
preselected observers for no-AI/no-scan assertions and process-spawn-to-exit
SessionEnd timing; wrapper-only timing and absent visible calls cannot prove
those contracts. Recheck protected paths after the run and risky fixture steps.
PASS requires actual assertions and hashed evidence. FAIL is an observed
contradiction. BLOCKED names an unavailable prerequisite, undecided policy or
tool restriction. NOT-RUN is not acceptance. If binding fails, preserve the
error and mark dependent native cases BLOCKED; continue safe independent
installation, negative-control and replay checks with their correct evidence
kind. Do not set an overall PASS because a subset passed. Evaluate prerequisites per
case and record the exact blocked assertion. Missing F.5 instrumentation alone
must not blanket-block K/B/A cases with valid independent origin and isolation
proof. Keep Codex hook-origin assertions BLOCKED without actual positively
controlled transport evidence. Never promote synthetic probes to native results.

No production calls or live alert channels. Use synthetic inputs only.
Do not modify live native transcripts. Use private copies for malformed,
rotation, crash and replay tests. Do not inspect or export hidden reasoning,
auth values, user conversations or an environment dump. For real process
identity/backend evidence save only allowlisted fields and hashes.

Do not fix the plugin during this run. Record a defect and stop its dependent
cases. A new fixed candidate needs a new campaign; this run must retain its
failed evidence. Avoid repeated broad reviews or full suites per skill.

Stay active through each foreground job. Check real output before reporting
that you are waiting. Send concise progress at meaningful milestones. Long
noninteractive commands need stdin closed and a timeout. Preserve partial
logs on timeout; record the timeout as an actual result.

At completion save report.json, report.md, run-manifest.json, fixture-manifest.json
and hashes in EVIDENCE_ROOT. Include before/after hashes, actual commands or
UI actions, exit status, UTC timestamps, timing method, assertion details,
evidence kind and blocker. Verify the saved files can be read back. Never
tick issue/spec criteria or publish an acceptance variable from this session.

Final response: candidate SHA, client/version/platform, case and skill counts
by result, concrete failures/blockers, report path and any one required
operator handoff. Do not claim parity or merge readiness.
```

## Minimal launch messages

Use the executor prompt above in each client with a separate manifest. Once
its documents are available, these short messages select the intended run:

```text
Execute native-session-instructions.md in this fresh Codex CLI session.
PLAN=<absolute plan path>
RUN_MANIFEST=<absolute CX-M or CX-L manifest path>
MODE=native
```

```text
Execute native-session-instructions.md in this fresh Codex Desktop task.
PLAN=<absolute plan path>
RUN_MANIFEST=<absolute CD-M manifest path>
MODE=native
The task itself must supply Desktop-native evidence. Do not substitute CLI.
```

```text
Execute native-session-instructions.md in this fresh Claude Code session.
PLAN=<absolute plan path>
RUN_MANIFEST=<absolute CC-M or CC-L manifest path>
MODE=native
```

For lifecycle continuations:

```text
Execute native-session-instructions.md with MODE=observe-handoff.
PLAN=<absolute plan path>
RUN_MANIFEST=<same campaign's manifest path>
HANDOFF=<absolute saved handoff.json path>
Observe the previous native session's exit/compact/fork result, save evidence,
then continue its next eligible cases. Do not change the tested session ID to
this observer's ID or certify that session from memory.
```

## Executor report shape

This is a campaign report, not the release acceptance ledger. Keep these
separate. `result` uses `PASS`, `FAIL`, `BLOCKED`, or `NOT-RUN`.

```json
{
  "schema": 1,
  "run_id": "CC-M-example",
  "tested_sha": "<40-character pinned SHA>",
  "client": "claude-code",
  "version": "<observed native version>",
  "platform": "macos",
  "binding": {
    "result": "BLOCKED",
    "dispatch_verified": false,
    "reason": "<actual observation; replace this example>",
    "evidence": []
  },
  "cases": [
    {
      "id": "B.2",
      "criteria": ["1", "4", "7"],
      "result": "BLOCKED",
      "expected": "Current client and full native ID supplied by invoking runtime",
      "observed": "<actual observation>",
      "evidence_kind": "native",
      "depends_on": [],
      "evidence": [{"path": "binding.json", "sha256": "<64-character hash>"}],
      "error_or_blocker": "<specific reason>"
    }
  ],
  "skills": [],
  "ai_operations": [],
  "origin_receipts": [],
  "isolation_audit": {},
  "baseline_comparison": {},
  "capabilities": [],
  "operator_actions_needed": [],
  "started_at": "<UTC ISO timestamp>",
  "finished_at": "<UTC ISO timestamp or null while running>"
}
```

Add all required case rows rather than leaving out unavailable cases.
For each of the 19 skills, use the same assertion/evidence/result fields with
`id` equal to `skill.<name>`. Capability rows name the exact IDs from
`capabilities.json` and the supporting cases, clients and platforms.
Manifest and report identity must agree. Relative artifact paths stay inside
EVIDENCE_ROOT; no symlinks or `..`. Use full native IDs privately; export only
the allowlisted synthetic identity needed to establish ownership.
