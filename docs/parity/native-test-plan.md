# Native parity acceptance test plan

Use this plan to test the installed candidate in fresh Claude Code, Codex CLI,
and Codex Desktop sessions. Test execution produces evidence; it does not itself
tick issue #272 or certify parity.

Start each executor with [the session instructions](native-session-instructions.md).
After the runs, start a separate reviewer with
[the acceptance review instructions](native-acceptance-review.md).
Copy [the run manifest](native-run-manifest.template.json) into each private run.

## Scope and commit

Freeze one full 40-character candidate SHA before installation. Record the
installed package's file hashes and prove that they match that commit. Do not
choose a package by its version number alone. The first draft referred to
`51f02a1055367f27e1048a487e915cf73f7bfb4a`. The preparer must select the actual
candidate explicitly; the manifest has no default SHA.
Use a new campaign if the candidate changes. Do not transfer PASS labels to a
new commit, even when only documentation changed.

The authoritative requirements are issue #272, the
[design](../codex-claude-parity-design.md),
[capability inventory](capabilities.json), and
[acceptance evidence contract](acceptance-evidence.md). Keep their full scope.
The table below is a map, not a replacement for the requirement text.

| Issue criterion | Required test groups |
| --- | --- |
| C.1: skills, lifecycle, recovery, versions and platforms | B, I, L, every K row, R |
| C.2: independent installs, invoking backend, failure retention, no hook AI/scans | I, A, F.5 |
| C.3: legacy content and shared wiki/memory/doctor behavior | K, R.5, X.6 |
| C.4: identity, concurrency, CLI/Desktop sharing, forks, resumes and worktrees | B, X, L.2, L.6 |
| C.5: real-format parsing, partial status, replay and user-content preservation | R, X.2, X.3 |
| C.6: deadlines, containment, permissions, custom paths, upgrade and diagnostics | I.3, I.4, F, L.5, R.4 |
| C.7: source gates, exact inventory, all acceptance evidence, live Desktop | G, B, all required groups and capability mappings |

### Issue acceptance gates

The issue also has ten unchecked “Acceptance gates”. `ac_gate.py` discovers
the seven criteria; that does not waive these ten gate lines. Review them too.

| Gate, in issue order | Required cases | Criteria |
| --- | --- | --- |
| 1: 19 skills, lifecycle/recovery, initial supported environments | B, L, every K row, R; all five required cells | C.1, C.7 |
| 2: independent installs, backend failures, no provider switch | I.1, A.1, A.2 | C.2 |
| 3: legacy notes, custom folders, searches, dashboards, links, Claude baseline | I.5, R.5–R.7, K | C.3 |
| 4: simultaneous hosts, shared Codex thread, equal IDs, forks/resumes/worktrees | B, X, L.2 | C.4 |
| 5: real parser formats, mirrors/metadata/partial/compact/rotation/missing/unsupported | L.3, R.1–R.3 | C.5 |
| 6: failure injection, concurrent writers, replay and content preservation | L.7, X.3, R.4 | C.5 |
| 7: three-second end capture, durable recovery, no AI/scans in hooks | L.5, L.7, F.4, F.5 | C.2, C.6 |
| 8: scrubbing, containment, permissions, reasoning exclusions, production isolation | B.1, R.1, F.1–F.3, isolation audit | C.5, C.6 |
| 9: fresh installs, existing-vault pairing, custom home, spaces, upgrades/compatibility | I, X.1, F.1, R.5 | C.2, C.4, C.6 |
| 10: full source gates and live Desktop evidence | G, B.2, L in CD-M | C.7 |

## Campaign layout and run order

Use one independently prepared environment per client/platform combination.
Start with binding checks; do not spend hours on a client that cannot dispatch.

| Run | Client | Platform | Purpose |
| --- | --- | --- | --- |
| CC-M | `claude-code` | macOS | Installed skills, lifecycle and recovery |
| CX-M | `codex-cli` | macOS | Installed skills, lifecycle and recovery |
| CD-M | `codex-desktop` | macOS | Actual Desktop task and its installed hooks |
| CC-L | `claude-code` | Linux | Same installed checks on Linux |
| CX-L | `codex-cli` | Linux | Same installed checks on Linux |
| PAIR-M | Two Codex frontends, then both providers | macOS | Shared-thread handoff and concurrent identity/writer checks |
| REVIEW | Fresh evidence reviewer | Any | Verify artifacts and check all seven criteria |

Issue #272 explicitly states: “macOS desktop/CLI and Linux CLI are the initial
supported environments.” The five client/platform cells above are required.
Linux Desktop is outside this initial scope; optional observations neither
replace required cells nor block them. Never substitute a bundled CLI for
Desktop. Save the exact scope text with the campaign requirement snapshot.

### Environment readiness before scheduling

The preparer fills `execution_environment` for each run: type, identity,
operator/provider, access method, native client availability and supported
authentication setup. No credentials belong in the manifest. Linux cells need
a named Linux host/VM/container and authenticated client; unavailable resources
are BLOCKED up front. No macOS run stands in for Linux.

I.1 requires a clean machine/VM/container with only the invoking provider
installed, checked by its package/application inventory and executable lookup.
A separate account is sufficient only if the other provider really is absent
from that execution environment; a new account with both globally installed
is not proof. PATH masking remains supplemental. Record who supplies the
independent environments and whether they are ready before running cases.

For CD-M and X.1, settle Desktop isolation before starting:

1. Use a supported mechanism to quit the app and relaunch it with the sandbox
   native home/config, if the installed Desktop supports that selection.
2. Otherwise use a dedicated macOS account/VM with its own native home, plugin
   install, vault, DB and state. Do not change the user's running Desktop daemon.
3. If neither is available, mark CD-M and X.1 BLOCKED during preparation.

The operator approves any quit/relaunch that could interrupt their work. Save
the selected method and native launch observation in `desktop_isolation`.
X.1 shares the **same sandbox CODEX_HOME and thread store** between CLI and
Desktop. Other cases keep separate native homes. If Desktop cannot use that
home, X.1 stays BLOCKED even if its independent tests pass.

Choose F.5 observability now: record an available OS process-spawn and file-open
observer, operator permissions, and coverage limits. On a Linux test VM,
`strace -f` process/file tracing of the native client tree is one option if
already installed and permitted. On macOS use an available supported system
audit/process observer operated by the test environment owner. Do not install
or grant privileged tooling implicitly. Supplement with PATH-front
`claude`/`codex` recorder shims in the disposable launch environment, forwarding
to pinned real binaries without recording prompts/auth. Shims miss absolute-path
binaries and alone cannot certify F.5. Positive controls must detect a benign
subprocess and an unrelated fixture-file read. Identify hook descendants apart
from legitimate foreground skill AI. Insufficient observation blocks F.5 up front.

### Readiness is scoped to each case

Record each observer's covered assertions, controls and receipts separately.
A missing F.5 process/file/network observer blocks F.5; it does not by itself
block B.1, B.2, K or A. Evaluate each case's own prerequisites:

| Scope | Required readiness |
|---|---|
| B.1 | Private selected paths, pinned installed package, client binding, and the complete protected-path baseline or a controlled file-write audit. |
| B.2 and lifecycle origin | A positively controlled client-owned hook-dispatch/debug record or an independent external observer that distinguishes actual native dispatch from manual handler calls. Missing Codex transport proof remains BLOCKED. |
| K and A origin | After B.2 and the protected audit pass, use client-owned loaded-skill selection, resource path, tool operation and actual backend receipts, independently collected and tied to the case marker/full native ID. Apply their revision and isolation prerequisites. |
| F.5 | Hook-descendant process, file and network coverage, with controls for each asserted boundary. Skill/backend receipts alone cannot prove absence of hook AI or scans. |
| L.5 | Complete external dispatch/spawn/exit timings for the required cold/warm runs. Wrapper timing alone cannot pass. |

An observer can cover one scope without covering another. Record coverage and
positive controls per method in `native_origin_observer.methods`, separate from
`hook_no_ai_scan_observer`. Record prerequisites and blockers per case in
`case_readiness`; do not turn one missing instrumentation boundary into a
blanket block of all 19 skills. Independent isolation and origin remain
mandatory for every native assertion. A client-owned record must come from the
actual client and have an independently identified collector; an actor-written
receipt or synthetic harness never becomes native evidence.

Order: B → I → early L.1/L.3 → K → remaining L → X/R/F/A → G → review.
PAIRED tests run after both involved clients pass their basic checks.
Record an interruption checkpoint before ending or compacting a session.
A follow-up observer session must inspect outcomes after the tested session
ends; the ending session cannot attest to its own post-exit state.

Run source gates once for the candidate, not once per skill or client. Use
existing same-SHA verified CI for those gates. Do not repeat full reviews.
Native runs still need real installed execution in every required matrix cell.

## Preparation and authority

1. Create a private campaign directory with a random name, mode `0700`.
   Each run gets a separate vault, index DB, private state, config, native home,
   project and evidence directory. Include spaces in at least one run's paths.
2. Prepare these selections **before starting the client**. Use supported native
   launch/settings mechanisms. A shell variable set inside an already-running
   Desktop task does not prove that its hook process or app server uses it.
3. Select `CLAUDE_CONFIG_DIR` for Claude or `CODEX_HOME` for Codex. Explicitly
   select `OBSIDIAN_BRAIN_CONFIG`, `OBSIDIAN_BRAIN_DB`, and
   `OBSIDIAN_BRAIN_STATE_DIR` for the disposable fixture. Record resolved values
   from the installed launcher. Do not copy authentication values to evidence.
4. Install the pinned candidate using the selected client's supported plugin
   installation flow. Observe current client help/plugin metadata rather than
   guessing version-sensitive install flags. Save the exact command/action,
   exit status and selected resource root. Hook trust uses the normal client UI;
   the operator handles any trust prompt the agent cannot act on.
5. Start a **new** session after install or restore. Record client version,
   platform, Python version, package descriptor and hook manifest hashes.
   Record Desktop app version separately from its bundled CLI version.
6. Give the executor the intended client in its run manifest. Claude Code
   uses the fixed host client `claude-code`, matching its hook registration;
   no pre-launch `OB_CLIENT` declaration is required. Record this accepted
   host-constant decision as `binding_method: fixed-claude-client` and reject
   any conflicting inherited declaration, including an empty value. Still
   verify actual installed hook invocation and the full native session ID.
   For Codex, a trusted operator/preparer declares `OB_CLIENT=codex-cli` or
   `codex-desktop` in the **client process environment before launch**.
   Record `binding_method: operator-launch-declaration`, its owner, timestamp,
   allowlisted launch record and observer receipt. The agent reads the inherited
   value and never chooses or exports a Codex frontend. No full environment dump.
   For Desktop this must reach the actual application and task processes through
   its supported isolation mechanism, not merely a later tool shell.
   A manifest label, agent export or daemon environment guess is not native
   evidence. Verify that the installed Codex hook command passes the declaration
   as `--client`. CLI runs must use `--no-daemon`; observe actual hook ancestry
   and arguments. Until B.2 verifies supported transport, Codex K/A positive rows
   remain BLOCKED. Agent-set Codex client probes are synthetic only.

Launch native Codex acceptance actors from an independent plain Terminal/tmux
shell, outside any Codex tool process or app-server/shared-server ancestry. A
nested `--no-daemon` child still inherits forbidden server ancestry and remains
BLOCKED.

Private DB isolation is essential: prior vault knowledge records 174 fixture
snapshot rows reaching a live DB through callers missing an explicit DB path
(insight title: “Pytest fixture vaults can pollute the user's live vault DB”).
Check indirect indexing calls too. A separate vault alone is insufficient.

### Isolation audit

Before any fixture/skill invocation, an observer outside the tested agent
records private path/hash/mtime inventories for default
`~/.claude/obsidian-brain*`, `~/.claude/obsidian-brain-hook.log`, the default
index DB and sidecars, live vault folders, and default `~/.codex` plugin/config/
state paths. Exclude auth files and secret stores entirely. Do not export
content or paths publicly. Use a bounded read-only inventory; never checkpoint
or rebuild the live DB. If complete inventory is impractical, use an available
file-write audit with verified sentinel controls. Unobservable protected paths
make isolation BLOCKED.

Repeat after the run, install/restore and fault injection. Attribute each
outside change with the independent observer. Normal housekeeping in another
active session must be predeclared and recorded separately; it cannot excuse a
plugin write to the live vault/DB. Plugin-caused protected writes are FAIL;
unexplained changes prevent PASS pending attribution. Stop further mutating
cases on a leak, preserving evidence; do not delete/restore user data as test
cleanup. Exactly **one** obsidian-brain plugin must be enabled in the selected
home, with no directory marketplace loading another live checkout.

## Deterministic fixture

Use only synthetic content. Put a unique `OBP-<run>-<case>-<nonce>` marker in
each input. Keep canonical expected facts in `fixture-manifest.json`.

- Three insight notes about a pretend project `parity-lab`: SQLite selected for
  a single-writer cache, retries limited to two attempts, and 24-hour expiry.
  The question “Why does parity-lab use SQLite?” has a known cited answer.
- Two related notes for bidirectional linking and two overlapping notes for
  consolidation. Preserve distinct facts, dates, provenance and manual text.
- A session with a completed task, an open task, a duplicated open task, and
  the unchecked sentinel `DO NOT CHECK OFF MANUAL SENTINEL`.
- A legacy `claude/session` note and legacy project/topic tags; an immutable
  snapshot with a link to its parent; an unrelated note that must stay unchanged.
- Three source notes that qualify for wiki filing. A reviewed wiki page with
  manual edits, an unreviewed page, and a deliberately stale citation.
- A synthetic error and fix for error-log; a decision with two alternatives;
  a repeatable thematic pattern for emerge; a small import corpus with one
  duplicate and one same-name/different-content collision.
- A manual-note marker, a summary marker and a raw-capture marker used to
  assert preservation across replay. A fake secret-looking marker used only
  to check scrubbing; no actual token or credential.

Seed files are fixture inputs, not plugin-output evidence. Hash them before
tests. Follow the loaded skills' schema and taxonomy; do not seed impossible
frontmatter merely to satisfy an expected output. Snapshot and transcript
copies remain distinguishable from the actual native source.

## Result rules

Every case and every skill has one of:

- **PASS:** all assertions observed, with hashed evidence for this candidate,
  installed client, platform and native invocation.
- **FAIL:** executed behavior contradicts an assertion. Save exact error and
  before/after state. Do not fix the product during this campaign.
- **BLOCKED:** a prerequisite, policy decision, tool or runtime capability is
  unavailable. Name it and save the refusal or observation.
- **NOT-RUN:** not attempted. Explain why; this cannot satisfy a criterion.

Continue independent tests after a failure. Mark dependent cases BLOCKED with
the upstream case ID; do not relabel the whole run PASS. A safely rejected
negative test can PASS when rejection is the expected assertion. That does not
turn a missing positive native dispatch into a PASS.

Label evidence `native`, `copied-native-replay`, `synthetic`, or `source-ci`.
Manual `native_entry.py` calls, fake backends, imported transcripts and unit
fixtures cannot satisfy native lifecycle or installed frontend dispatch.

## B — Binding first

| ID | Agent action | PASS assertion and evidence |
| --- | --- | --- |
| B.1 | Inspect loaded plugin/skill metadata, independent pre-launch declaration and protected-path baseline; resolve root from loaded `SKILL.md`, then request context through its documented launcher. | Exactly one selected plugin; installed bytes match candidate; inherited declaration matches launch record; resolved home/config/vault/DB/state/project are private and correct. Save argv/context JSON. Agent-created environment values or repository helpers do not prove native skill execution. |
| B.2 | Observe actual start/turn hook delivery. Match installed-handler outcome, client-owned session store, note/DB provenance and independent dispatch-origin receipt. | Supported event-local provider/client/full ID and owned marker agree across all four records. Origin receipt distinguishes native invocation from a manual handler call. No frontend inference from creation headers or daemon environment. |
| B.3 | Exercise a separate unbound negative probe against an isolated fixture. | Missing identity skips capture, warns that no source was retained, and creates no false capture state. Keep this evidence synthetic and separate from B.2. |

**Defect in the initial candidate `51f02a1`:** `hooks/codex-hooks.json` supplies
`--host codex` but does not supply `--client`. Entry skips Codex capture without
that argument. Test the installed bytes and actual transport. If still present,
record B.2 FAIL/BLOCKED with stderr; do not patch the installed manifest, export a
guessed client, or call the handler manually to certify native dispatch. An
operator-supplied target label does not fix the missing hook argument.

### Required origin proof

For B.2, every L case, X.1 and each A.1 native receipt, require:

1. Installed-handler evidence: outcome/event, provider, full native ID, time,
   path and handler hash. A.1 uses the installed skill operation/backend receipt;
   a skill call need not create a lifecycle log.
2. Client-owned session source: Claude under the selected
   `CLAUDE_CONFIG_DIR/projects`, Codex under `CODEX_HOME/sessions`. Verify full
   ID, unique marker, byte range/offset and source hash. Export only allowlisted
   synthetic visible/event fields, not reasoning/auth/developer content.
3. Resulting note/DB/operation provenance and hashes bound to that source.
4. Independent origin observation: a client hook-dispatch record or an external
   process/event observer linked to actual native invocation and operator action.
   For skills match native skill selection, loaded path, tool operation and
   actual backend execution in the client-owned record.

The first three are necessary but a manual call using the same ID could forge
that triangle. Therefore matching logs/notes alone are insufficient. Inspect
native tool actions for manual handler injection and reject it as hook proof.
If origin cannot be distinguished, the native assertion is BLOCKED. Hashes
establish byte integrity, not origin. Observer evidence has an identified owner,
collection method and positive control; an agent-authored claim is not a trace.

## I — Installation

| ID | Agent action | PASS assertion and evidence |
| --- | --- | --- |
| I.1 | Fresh install in an independent environment containing the tested client and no other provider installation. Start a new session and execute one read skill and one write skill. | Discovery, trusted hooks and actual operations work without the other provider. PATH masking on a machine with both installed is only supplemental evidence, not independent-install proof. |
| I.2 | Run from an unrelated cwd, with installation and project paths containing spaces. Exercise dev-test status through the loaded skill. | Helpers stay in the loaded package; no newest-cache search or cwd-selected resources. No mutation outside the selected cache. |
| I.3 | Upgrade a disposable previous released installation to the candidate; run a read and a write; restore the previous installation and start fresh again. | Candidate matches pinned bytes; restore matches original bytes; config, pending source state and user-note bytes survive. Retain backups until verification. |
| I.4 | In a separate scratch copy, present an incompatible writer/state version using a reviewed fixture or existing test driver. | Diagnostic refuses unsafe mutation, preserves source/notes/DB sidecars and identifies incompatible state. An older writer must not silently rewrite it. |
| I.5 | Run the same K/legacy/custom-folder/dashboard fixture inputs on the released Claude package and candidate in separate fresh Claude environments. | Baseline release SHA/package hashes and client/fixture versions recorded; normalized semantic differences map only to documented agreed changes. An unexplained difference is FAIL. |

For I.5 the currently resolved baseline is `v3.8.1`, SHA
`9e640475ecb9e205b924cfd99937bf5e954cd0fa`. The preparer verifies that immutable
release selection and records it; unavailable release bytes block the comparison.
Package version text alone cannot select the baseline. Normalize only
predeclared volatile timestamps, private paths and native IDs; compare facts,
taxonomy, filenames/folders, query results, links/source tracking and manual
bytes, not exact model prose. Build `agreed-change-allowlist.json` from the
issue/spec and recorded human decisions, citing authority for each allowed
difference. Never expand it to excuse a newly observed regression. The feature
branch's identical `3.8.1` version label does not establish identical bytes.

## L — Real native lifecycle

Use normal client controls. When unavailable, record the exact missing control
and obtain an operator action. Do not simulate these controls with Python.
The agent must write `handoff.json` with native ID, case, markers and evidence
directory before asking the operator to compact, clear, resume, fork or exit.

| ID | Agent action | PASS assertion and evidence |
| --- | --- | --- |
| L.1 | Start fresh with installed trusted hooks; exchange at least three distinct synthetic turns and satisfy configured capture duration/turn thresholds. | SessionStart and each completed-turn Stop actually dispatch; visible user/final text captured once; note has correct provider/full ID and no borrowed Claude link. Record thresholds and elapsed time. |
| L.2 | Exit, then resume the exact native session and send a new unique turn. | Resume dispatch occurs on the next turn, selects the same logical source and preserves prior text without duplicate capture. Opening a resume picker alone is insufficient. |
| L.3 | Send a pre-compaction marker, use native compact, then send a post-compaction marker. | PreCompact actually dispatches, checkpoint is immutable and linked to its parent, and recall/retro can use both sides. Record actual event and snapshot hashes. |
| L.4 | Test native clear in a sacrificial session with snapshots enabled, then repeat in another sacrificial session with the scratch toggle disabled. | Enabled captures the checkpoint; disabled produces no checkpoint and exposes the documented warning. No old source is assigned to the new session. |
| L.5 | Prepare owned source, exit normally, and externally observe event dispatch, hook spawn, wrapper entry and process exit/completion. A follow-up session checks durable state; repeat cold/warm at least three times. | Native SessionEnd proven; one bound outcome per invocation. Report dispatch-to-spawn, spawn-to-exit and wrapper-entry-to-completion separately. Codex's 3-second hook-process limit includes interpreter startup; 2.5-second work budget starts at wrapper entry. Wrapper-only timing cannot establish the native end deadline. |
| L.6 | Interrupt a synthetic long-running task, then send another turn. | Aborted turn remains accurately identified; completed-turn state does not falsely mean session end; later owned output is captured without cross-session history. |
| L.7 | In scratch state, leave pending retained input through a controlled interrupted capture, then start/resume and run doctor. | Automatic bounded recovery and doctor expose pending work, replay owned input once and preserve manual/summary regions. If interruption cannot safely target a fixture, BLOCKED. |

Desktop: the run must originate in an actual Desktop conversation. Retain
an operator observation or allowed UI evidence plus a matching native task ID,
handler invocation and output state. A screenshot alone is insufficient;
the bundled CLI version and `hooks/list` alone are insufficient. If CUA denies
the Codex app, record it and let the human operate normal controls; do not
bypass the restriction with another automation API.

## K — All installed skills

Read and invoke each **loaded** SKILL.md through its native skill mechanism.
Record the actual invocation, loaded path/hash, operation IDs, backend and
output bytes. Follow documented prepare/read/analyze/preview/save operations.
Do not implement the effect yourself and count it as skill execution.

The operator's execution prompt can approve listed writes to the fixture.
Honor any explicit preview gate that still needs an interactive response;
save a handoff and continue independent cases rather than waiting silently.

| Skill | Synthetic task | Required observed assertion |
| --- | --- | --- |
| `obsidian-setup` | Configure the selected private vault. | Selected config and folders created; no global home write; repeated setup preserves existing notes. |
| `vault-config` | Read config; change a scratch threshold; re-read and restore. | Revision-bound write succeeds; stale revision refuses; new context reads restored value. |
| `decide` | Record SQLite versus JSON-file choice for parity-lab. | Preview and saved decision keep alternatives, rationale and invoking provenance. |
| `error-log` | Record the synthetic retry error and its two-attempt fix. | Problem, cause, fix and provenance are saved; no unrelated data copied. |
| `compress` | Save the synthetic learned retry/expiry facts. | Curated insight retains meaning and source identity; classification/save policy followed. |
| `recall` | Retrieve the project's decision and snapshot from a fresh/resumed session. | Relevant facts cite real fixture notes; snapshots resolve to their parent; no foreign session link. |
| `standup` | Summarize synthetic completed and open work. | Completed/open distinction is accurate; manual unchecked sentinel remains open. |
| `retro` | Analyze pre/post-compaction synthetic work. | Evidence includes parent and snapshot, claims reflect observed work, and classification-before-save runs. |
| `check-items` | Triage duplicate and completed synthetic checkboxes. | Approved fixture updates occur once; manual sentinel is never checked off; concurrent edit survives/refuses safely. |
| `consolidate` | Merge the overlapping fixture insights. | Distinct facts and source tracking survive; approved originals handled as documented; no unrelated note removed. |
| `emerge` | Find the retry pattern in seeded themes. | Analysis grounded in selected sources; selected native backend performs synthesis; saved artifact has correct provenance. |
| `link` | Link the two related notes; repeat once. | Bidirectional links appear once; repeated execution is idempotent; concurrent revision conflicts preserve edits. |
| `vault-ask` | Ask why SQLite was selected; approve eligible wiki filing; ask again. | Cited answer matches fixture; only qualifying sources filed; fresh wiki reused; reviewed stale page not overwritten without approval. |
| `vault-search` | Search the unique project and retry markers. | Relevant hits and bounded results; no unrelated project/session leakage. |
| `vault-stats` | Report fixture inventory. | Counts agree with the current fixture; no stale live-vault/index counts. |
| `vault-import` | Import the synthetic legacy corpus twice, including collision. | Imported provenance correct; duplicate replay safe; different-content collision reported/preserved rather than overwritten. |
| `vault-reindex` | Rebuild only the selected private index. | Fixture notes searchable; access/theme/non-derivable state preserved according to the documented contract; no live DB touched. |
| `vault-doctor` | Audit fixture issues; approve only supported fixture repairs; rerun. | Reports correct issues, bounded recovery and memory boundary; repairs preserve user bytes and rerun reflects changes. |
| `dev-test` | Status, scratch install, new session, scratch restore, new session. | Exit statuses honored, bytes/backup verified, correct host cache selected; refusal is not called a successful install. |

## X — Identity and concurrent use

| ID | Agent action | PASS assertion and evidence |
| --- | --- | --- |
| X.1 | In PAIR-M, open/resume the same native Codex thread in CLI and Desktop sequentially, with one distinct marker from each frontend. | Same thread identity retained; each event carries its actual current frontend; source is neither forked accidentally nor linked to Claude. Hold a shared test lock so concurrent operators do not race the handoff. |
| X.2 | Run independent Claude and Codex sessions concurrently against a designated shared fixture vault, separate native state/homes. | Full provider/session/project identities separate; no wrong source, link, cache entry or snapshot; legitimate revisions serialize or report conflict. |
| X.3 | Request two competing edits to one fixture note from different native sessions, then replay each pending operation. | Conflict does not overwrite manual/summary/capture bytes, create duplicate facts or discard either pending request. Save before, conflict and final hashes. |
| X.4 | Native fork a sacrificial thread and send a child-only marker. Also exercise equal raw IDs with a copied fixture in both providers. | Child owns new events; proven parent rows not duplicated; ambiguous history stays pending. Provider distinguishes equal raw IDs. Copied equal-ID test is supplemental, not a live fork. |
| X.5 | Move, then remove only a disposable project worktree after capturing; resume/doctor from the preserved test project. | Canonical identity and snapshots stable; deletion does not select newest unrelated source or resurrect a foreign cache. Keep source copies and common Git repository. |
| X.6 | Inspect host memory handling with a synthetic Claude memory fixture and a foreign Codex marker. | Claude uses its own approved memory sources; Codex reports no verified native memory adapter and never borrows Claude memory. Shared vault/wiki lookup still works. |

X.4 note-placement assertions need `fork_note_policy` in the manifest.
X.5/R.2 unreadable retired-source assertions need `retired_source_policy`.
If either is unset, test invariant safety and mark the undecided policy branch
BLOCKED. Never choose a product policy to make acceptance pass.

## R — Parsing, replay and preservation

First export allowlisted visible records from the actual synthetic native run.
Use those exact native-format bytes as the base for private replay copies.
Record transformations and hashes; never mutate the client's live transcript.

| ID | Agent action | PASS assertion and evidence |
| --- | --- | --- |
| R.1 | Compare actual native visible turns with captured facts; replay copies with duplicate mirrors, metadata-only content, hidden-reasoning markers and an incomplete trailing line. | Mirrors capture once; metadata/reasoning excluded; partial/unsupported/unavailable statuses distinguished; incomplete bytes do not advance a committed cursor. |
| R.2 | Rotate/remove a private copied source; make a retired copy unreadable; inject a well-formed unknown row, then a malformed known row into separate copies. | Rotation keeps generation identity; unavailable input remains pending. Approved policy captures known owned rows around well-formed unknown rows while marking partial; malformed/ownership-invalid input stays fail-closed. No implicit acknowledgement/deletion of unreadable input. |
| R.3 | Replay actual-format fixture copies twice, including unknown-row replay after reviewed schema support. | Original byte range/hash verified; unchanged events applied once; changed bytes are refused and remain pending. Do not invent new parser support to run the test. |
| R.4 | Use the existing reviewed crash/replay test driver on sandbox copies at retention, note publication and cursor advancement boundaries. | Restart completes or remains explicitly pending; no dropped retained event or falsely advanced cursor; WAL/SHM and manual bytes preserved. Tag driver evidence synthetic/replay. |
| R.5 | Exercise legacy notes, golden lookup/filing/metadata/source tracking, snapshots and migrated notes from both invoking providers. | Agreed legacy content remains usable; taxonomy unchanged; native provenance correct; no cross-provider memory borrowing. |
| R.6 | Repeat setup, capture, search, wiki filing, reindex and doctor with custom sessions/insights/wiki folders; test wiki disabled in a separate fixture. | All writers/readers/dashboard inputs honor selected folders; no output appears in default folders; disabled wiki has documented behavior without foreign-memory fallback. Preserve approved legacy pairing. |
| R.7 | Load the six shipped dashboards against the synthetic vault using an actual supported Obsidian/Dataview environment; compare rendered query results with expected fixture inventory and I.5 baseline. | Sessions overview, project index, weekly review, learning velocity, decision timeline and open-items queries retain expected records, links/tags and ACTIVE/STALE entries. Record Obsidian/Dataview versions, dashboard/query hashes and rendered row evidence. Static source inspection alone is supplemental; unavailable query runtime is BLOCKED. |

## F/A/G — Bounds, backend and source gates

| ID | Agent action | PASS assertion and evidence |
| --- | --- | --- |
| F.1 | Use scratch custom home/config/state/DB paths, spaces and changed environment after binding. | Existing operation remains bound to original selected resources; new invocation resolves explicit new selection; no default path touched. |
| F.2 | Attempt traversal, symlink escape and foreign installation/resource-root operations against private sentinel fixtures. | Rejected before modification; outside sentinel hash unchanged; no unsafe config fallback. |
| F.3 | Inspect permissions and capture the fake secret marker. | Private directories `0700`, sensitive artifacts `0600`; fake secret scrubbed from published output; no authentication/environment/hidden reasoning exported. |
| F.4 | Replay bounded oversized/many-source fixtures, then measure installed start/resume recovery. | Per-row/batch bounds honored; unresolved tail stays pending; start/resume recovery budget is 1 second from wrapper entry and at most eight registered sources per pass; repeated passes make bounded progress. Save monotonic timing, not guessed duration. |
| F.5 | Use the preselected, positively controlled observer during native hook events; distinguish hook descendants from foreground skill/model work. | No hook-origin model launch/API operation, full-vault scan or index rebuild; process/file/network audit coverage and limits stated. Shims alone and absence of visible calls cannot establish PASS. Insufficient observer coverage is BLOCKED. |
| A.1 | Execute every fixed backend operation and authored native analysis path listed below in each required client/platform cell; save one receipt per path. | Actual provider/client/backend and observed model (or explicitly unknown model) recorded, input/source revisions respected, output validated and publication provenance retained. Cached output cannot prove a new backend execution. |
| A.2 | For every A.1 backend operation/path that can leave pending work, cancel or safely fail a native fixture operation and verify retention; also run reviewed invalid-output controls. | Notes/source unchanged or approved partial results retained, pending request survives, no fallback/provider switch. Separate fake-backend controls from actual installed cancellation/failure evidence. |
| G.1 | Verify same-SHA full CI, coverage, security, DB isolation, skill snippets, architecture, installed-package controls and both-host contracts. | All required gates PASS for frozen SHA; full collection accounted for. Save run IDs, exact logs/hashes and counts. Never replace complete CI with a focused test. |
| G.2 | Reconcile every required capability ID and host/version/provenance with cases and evidence. | No required row omitted, unsupported or pending; all 19 installed skills accounted for separately per client/platform. Native memory boundary retained for its optional capability. |

### Complete AI inventory for A.1/A.2

The fixed backend registry currently has six operations. Reconcile this list
against the **selected SHA**, not this prose alone, and store an inventory hash.
Exercise them through documented installed skill operations; do not directly
call backend functions and label that a native skill test.

| Backend operation | Fixture trigger through installed skill |
| --- | --- |
| `snapshot_summary` | recall/standup upgrade of an unsummarized immutable snapshot |
| `session_summary` | recall/standup upgrade of one unsummarized session |
| `session_summaries` | recall/standup upgrade of a batch of unsummarized sessions |
| `theme_names` | consolidate/theme-split with synthetic distinct themes |
| `semantic_merge` | check-items semantic grouping of related synthetic open items |
| `classify_items` | check-items classification of known completed/open fixture IDs |

Require an observed receipt for the actual selected operation. If a trigger
uses a different operation, correct the fixture using documented controls;
do not infer coverage. Use unique inputs/cold operation state to establish
real execution rather than cache replay; test valid cached replay separately.

Authored analysis may run in the native main session or a native helper rather
than the fixed backend. Account for these distinct paths too:

| Skill/path | Native analysis branches to cover |
| --- | --- |
| recall | Native upgrade helper; documented inline fallback after upgrade failure; brief and recurring-theme synthesis |
| standup | Native upgrades, context-shield helper reads when applicable, standup synthesis, deep-pipeline classifications and reviewed deep edits/checkoffs |
| emerge | Native theme-analysis helper → registered analysis artifact → build-note |
| vault-import | Native helper summaries for explicitly selected historical session fixtures |
| check-items | Semantic grouping, evidence gathering, classification, reviewed application |
| consolidate | Native theme naming and analysis-driven theme merge/split |
| retro | Main-session retrospective with snapshots and classification-before-save |
| compress / decide / error-log | Main-session synthesis with correct source provenance and reviewed publication |
| link | Main-session link selection followed by revision-bound publication |
| vault-ask | Main-session cited answer synthesis and eligible reviewed wiki filing |

The remaining skills have no fixed backend operation; still test their native
skill execution in K. Record the deliberate invoking-client model activity
separately from deterministic helpers. For helper paths verify they stay in
the invoking provider; inherited foreign session variables never select it.
Use the source inventory to detect added/removed analysis paths. Every required
path needs its own row for each required cell; “one AI call worked” cannot
satisfy C.2. Unreachable/unavailable paths remain BLOCKED or a separately
reviewed scoped N/A with evidence, never silently omitted.

## Evidence and handoffs

Each run saves `run-manifest.json`, `report.json`, `report.md`, `handoff.json`
when needed, `fixture-manifest.json`, and a file-hash manifest. Store full raw
synthetic logs privately; export small allowlisted receipts for aggregation.
Every receipt names tested SHA, client/version/platform, native identity
provenance, loaded package hashes, case ID, command/action, exit/result,
assertions, UTC time, timing method, artifact paths and SHA256.

One result row per case and per installed skill; include expected and observed
values, upstream dependencies, evidence kind, paths/hashes and blocker/error.
The reviewer's schema is described in the
[review instructions](native-acceptance-review.md). A valid JSON status field
alone cannot certify behavior.

At session exit/compaction/fork, persist handoff before asking for the one
necessary operator control. A new session reads the handoff and observes the
previous result without inventing a completed event. Preserve failures and
pending work. Do not delete test environments until review and operator cleanup.

## Initial campaign expectation

At the initial pinned candidate, Codex client binding is expected to block
positive hook capture. Desktop CUA inspection may remain denied. The plan
should expose those facts quickly, still collect independent evidence, and
produce an honest criterion checklist. It must not force seven green labels.
