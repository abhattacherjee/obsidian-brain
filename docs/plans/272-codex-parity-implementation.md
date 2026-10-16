# Codex parity implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. `/ship` uses one implementation pass, followed by one whole-branch review and one independent cross-model review. The user approved this plan on 2026-10-05; proceed with implementation.

**Goal:** Support Claude Code, Codex CLI, and Codex desktop in one vault, with all 19 skills and automatic capture and recovery.

**Architecture:** Extract shared stdlib Python services behind thin host adapters. Carry explicit host/session identity through configuration, transcripts, capture, writes, recovery, AI execution, and wiki operations. Keep existing entry points as compatibility wrappers and preserve existing vault content.

**Tech Stack:** Python 3.9+, stdlib, SQLite, pytest, Claude Code and Codex native hooks and AI clients.

**Spec:** `docs/codex-claude-parity-design.md`, refreshed specification from commit `cd232cb7b7ff9fcdbfd3c86fa0c84750136f8fb1`.

## Baseline and approval

- Issue: #272. Specification tracker #360 and specification PR #361 are closed/merged; this does not mean parity shipped.
- Implementation base: `develop` at `ad4a48eee23bbaf7f4be7d73af193af7db71f800`.
- Branch: `feature/272-codex-parity`; milestone: v3.9.
- Wiki dependency #396 is closed. Use its actual `hooks/memory_sources.py`, `hooks/wiki.py`, and `scripts/vault_doctor_checks/wiki_pages.py` APIs.
- Installed versions observed during planning: Codex CLI 0.155.1 and Claude Code 2.1.289. Verify the desktop version and installed contracts in Task 1; a version string alone is not acceptance evidence.
- The acceptance collector did not recognize the issue's `Acceptance gates` section. The user approved the seven criteria below on 2026-10-05. Add them to the issue and collect them again.
- This plan authorizes no merge or deployment. The shipping workflow still requires merge approval after acceptance and review.

## Global constraints

- Full parity covers all 19 authored skills. No separate `skills-codex` tree and no partial-parity release.
- Exact skill set: `obsidian-setup`, `vault-config`, `recall`, `vault-search`, `vault-ask`, `compress`, `decide`, `error-log`, `retro`, `standup`, `check-items`, `consolidate`, `emerge`, `link`, `vault-import`, `vault-doctor`, `vault-stats`, `vault-reindex`, and `dev-test`.
- Initial platforms: macOS desktop/CLI and Linux CLI. Test Python 3.9 and 3.12. Keep runtime helpers stdlib-only.
- No daemon, MCP server, hosted service, or dependency on the other assistant's installation.
- Identity is `(host, native_session_id)`. IDs are opaque. Repository identity is separate from worktree location. Never choose a Claude identity for Codex from environment variables or recent transcripts.
- Configuration precedence is CLI override, `OBSIDIAN_BRAIN_CONFIG`, then native host configuration. Preserve the current vault and index; use `OBSIDIAN_BRAIN_STATE_DIR` for private state and keep coordination data beside the shared index, outside rebuildable data.
- Keep Claude's current config path. Codex config is `$CODEX_HOME/obsidian-brain-config.json`, defaulting to `~/.codex/obsidian-brain-config.json`. Add `index_path`; preserve `OBSIDIAN_BRAIN_DB` as an explicit override. A new vault defaults to `~/.local/share/obsidian-brain/vaults/<vault-key>/index.sqlite3`, where the key hashes the resolved vault path.
- Codex state defaults to `$CODEX_HOME/obsidian-brain/state`, or `~/.codex/obsidian-brain/state`. Under either host's selected state root, use a versioned subtree keyed by vault, host, native session, canonical project root, and unique operation ID. Skill shells cannot depend on hook-only `PLUGIN_DATA`.
- Preserve legacy filenames, note types, folders, tags, dashboards, and backlinks. New notes add host provenance. Preserve manual edits and summaries.
- User data uses mode 0600 and private directories 0700. Resolve containment, scrub secrets, bound stdin, and exclude hidden reasoning before persistence and AI dispatch.
- Hooks perform no AI calls, full-vault scans, or index rebuilds. AI uses the invoking host; failures retain pending work and never switch provider.
- Tests isolate configuration, scratch state, coordination state, databases, and outbound calls. Live acceptance uses a disposable vault and native clients, never the user's production vault.
- Proposed recovery bounds: at most 8 sources and 250 ms per start/resume recovery invocation; at most 8 sources and 1 second per explicit recovery batch. Persist deterministic continuation cursors. Measure these limits in Task 1 and obtain approval for any required change.
- Codex SessionEnd declares a timeout of 3 seconds. Set its handler deadline to the configured timeout minus 500 ms; a 3-second timeout gives a 2.5-second deadline. Measure cold-start overhead and persist unfinished work before exit. Contract tests read the installed manifest.
- Run `./scripts/commit-preflight.sh` before each commit. Update architecture JSON and rendered HTML in the same stage as runtime changes.

## Acceptance Criteria

1. All 19 skills and start/resume, completed-turn capture, compaction snapshots, session-end capture, and recovery pass on Claude Code, Codex CLI, and Codex desktop. Record client versions and macOS/Linux results.
2. Each host installs and operates without the other installed. Every AI operation uses the invoking backend. Failure preserves pending work and never changes provider; hooks make no AI calls or full scans.
3. Legacy vault content and existing Claude behavior remain usable, except for the agreed checkpoint changes. Wiki lookup, filing, metadata, source tracking, memory-source handling, and doctor checks use the shared host context.
4. Concurrent hosts, desktop/CLI sharing a Codex thread, equal native IDs, forks, resumes, worktree moves/deletion, cache entries, and snapshots keep the correct identity. No Codex output receives a Claude session link.
5. Real transcript fixtures cover mirrors, metadata-only input, incomplete lines, compaction, rotation, missing sources, unknown records, and hidden reasoning. Parser status distinguishes partial/unsupported/unavailable input. Crash and concurrent-writer tests prove replay safety and preserve manual, summary, and captured content.
6. SessionEnd meets its measured deadline and preserves pending work. Recovery respects source/time bounds. Containment, scrubbing, permissions, custom configuration/state locations, paths with spaces, upgrades, and incompatible-writer diagnostics pass.
7. Full pytest/coverage, security, DB isolation, skill snippets, architecture checks, installed package checks, exact capability inventories, and both-host conformance pass. Acceptance evidence is recorded for every criterion; desktop behavior includes live evidence.

## Review focus

| Failure class | Owning task and evidence |
| --- | --- |
| Foreign identity or host leakage | Task 2: inherited Claude ID, nested hosts, equal native IDs, moved worktrees |
| Data loss, duplicate replay, stale summaries | Task 3: injected crashes at each write boundary and competing writers |
| Successful-empty capture or privacy leak | Task 4: real parser fixtures, unsupported records, scrubbed checkpoints |
| Host-only AI or skill assumptions | Task 5: backend failure tests, unrelated working directory, all 19 skills |
| Unsupported installed lifecycle or false parity claim | Tasks 1 and 6: native desktop/CLI runs, capability inventory, claims audit |

## Task 1: Verify installed contracts before extraction

**Files:** Create `tests/fixtures/hosts/` sanitized payload/transcript fixtures and `docs/parity/runtime-contracts.md`. Create `tests/test_host_runtime_contracts.py`. Modify `.codex/hooks.json` only after verifying its supported schema; keep Claude registrations intact.

**Interfaces:** Input is a native client version, installed plugin, event payload, and transcript. Output is a versioned fixture set and a contract ledger containing observed event names, payload fields, output contracts, installation behavior, and time limits.

- [ ] Create a private, disposable vault/config/state/index sandbox. Record exact client versions and fixture provenance without account data or secrets.
- [ ] Exercise actual CLI and desktop start/resume, Stop, compaction, end, fork, and interrupted sessions. Capture native visible-message and tool layouts, mirrored events, and `stop_hook_active` behavior.
- [ ] Verify `CODEX_THREAD_ID` against the rollout thread ID in a skill shell, and the authored skill's handoff to its installed resource root. These are measured contracts, not inferred filenames.
- [ ] Test no-auth plugin installation and which manifest is selected. Confirm desktop and CLI both load Codex wrappers, rather than Claude lifecycle hooks. Check nested Claude-inside-Codex behavior.
- [ ] Test `.claude-plugin/marketplace.json` as the single marketplace source in both clients. If desktop compatibility fails, explicitly migrate to `.agents/plugins/marketplace.json`; do not maintain divergent marketplace entries. Record this result before choosing Codex's manifest location.
- [ ] Add failing fixture-validation tests for payload shape, native blocking Stop JSON, loop protection, observed manifest timeout, and selected lifecycle dispatch. Implement the fixture validator and run `python3 -m pytest tests/test_host_runtime_contracts.py -q`; require PASS before committing. Record absent runtime support in the contract ledger; add failing implementation assertions when the relevant wrapper is built in Task 4.
- [ ] Record measured cold-start overhead and validate the proposed recovery/end bounds. Stop implementation if an installed client cannot provide a required contract; describe the exact gap for a user decision.
- [ ] Commit sanitized fixtures and the contract ledger after preflight. Native evidence must precede the runtime rewrite, not arrive at final review.

## Task 2: Extract host identity, configuration, and resources

**Files:** Create `hooks/runtime_context.py`, `hooks/runtime_adapters/__init__.py`, `hooks/runtime_adapters/claude.py`, `hooks/runtime_adapters/codex.py`, and `hooks/brain_cli.py`. Modify `hooks/obsidian_utils.py`, `hooks/hook_bootstrap.py`, `hooks/vault_index.py`, `hooks/memory_sources.py`, `hooks/wiki.py`, `hooks/obsidian_session_hint.py`, `hooks/obsidian_session_log.py`, `hooks/obsidian_context_snapshot.py`, `hooks/obsidian_retro_gate.py`, `scripts/vault_doctor_checks/wiki_pages.py`, and `scripts/vault_doctor_checks/memory_index.py`. Create `tests/test_runtime_context.py` and `tests/test_host_configuration.py`. Update architecture JSON/HTML.

**Interfaces:** Add frozen stdlib dataclasses. `RuntimeContext` fields are `host: str`, `client: str`, `native_session_id: str`, `canonical_project_root: Path`, `worktree: Path`, `transcript_path: Optional[Path]`, `vault_path: Path`, `config_path: Path`, `config: Mapping[str, object]`, `resource_root: Path`, `index_path: Path`, and `state_path: Path`. `resolve_runtime_context(host: str, client: str, payload: Mapping[str, object], overrides: Mapping[str, object]) -> RuntimeContext` raises `RuntimeContextError(code: str, message: str)` for invalid input; wrappers translate it to host-native diagnostics without guessing an identity. These signatures are plan decisions, not preexisting APIs. Proposed `brain_cli.py` commands accept paths through argv and bounded JSON through stdin.

- [ ] Add failing identity tests for inherited `CLAUDE_CODE_SESSION_ID`, concurrent recent Claude transcripts, equal host IDs, nested hosts, forks/resumes, and moved/deleted worktrees. Include the vault's observed Codex-retro/Claude-link regression.
- [ ] Add failing precedence, custom `CODEX_HOME`, spaces, missing installation, index preservation, containment, and unrelated-cwd tests. Run the two new test modules and confirm failures.
- [ ] Implement context resolution and native adapters. Retain old imports as wrappers; explicit context replaces marker guessing in shared code.
- [ ] Route configuration/resource/index resolution and #396 memory discovery through context. Preserve `memory_sources(host, config, errors)`, `failed_scopes(errors)`, `memory_name(path)`, and wiki compatibility commands. Record unsupported host memory sources explicitly, rather than presenting an empty supported source set.
- [ ] Introduce the shared CLI boundary and host-qualified cache keys. Remove basename-only bootstrap identity and any Codex use of Claude home/state paths.
- [ ] Run the new tests and existing config/session/index/wiki/memory/doctor tests. Require preservation of existing index data and independent-host operation.
- [ ] Update architecture component `hosts` and affected flows, render and validate; preflight and commit.

## Task 3: Unify writes, revisions, and durable cursors

**Files:** Modify `hooks/note_writer.py`, `hooks/wiki.py`, `hooks/check_items_cache.py`, `hooks/deep_cli.py`, `hooks/obsidian_utils.py`, `hooks/obsidian_session_log.py`, and `hooks/obsidian_context_snapshot.py`, plus any direct writer found during the inventory. Create `hooks/capture.py`, `tests/test_host_note_transactions.py`, and `tests/test_capture_recovery_transactions.py`. Update architecture JSON/HTML.

**Interfaces:** Add `NoteMutation(path: Path, expected_revision: Optional[str], managed_changes: Mapping[str, str], operation_id: str)` and `WriteResult(status: str, revision: Optional[str], pending_path: Optional[Path], warnings: Tuple[str, ...])`. `apply_mutations(context: RuntimeContext, mutations: Sequence[NoteMutation]) -> WriteResult` returns `applied|unchanged|conflict|pending`; it never reports a conflict as successful publication. All writers enter through this boundary. Capture event identity is `(session_key, source_generation, source_event_id_or_offset)`. Durable state stores scrubbed checkpoint, applied note revision, and committed cursor separately.

- [ ] Inventory every direct write and rename. Add tests that fail if any writer bypasses the shared transaction entry point.
- [ ] Add failing concurrent-writer and crash tests before checkpoint publication, after checkpoint, after note replacement, and before cursor commit. Cover stale locks, lock ownership, process death, competing summary/manual edits, legacy unmanaged notes, and wiki page/index/log updates.
- [ ] Run the two transaction modules; confirm the new guarantees fail before implementation.
- [ ] Implement per-session ownership locks, per-vault coordination, atomic writes, revision checks, managed capture regions, reviewed-content protection, warning-after-save semantics, and conflict/pending results. Publish scrubbed checkpoint before note, and note before committed cursor.
- [ ] Make summaries conditional on the revision they read. Preserve manual edits; record conflicts rather than overwriting content. Keep coordination data across index rebuilds.
- [ ] Route all writers through the protocol. Fix cache/reaper overlap within this change. Verify a replay after any injected crash neither loses nor duplicates content.
- [ ] Include wiki tests for failed stale checks, explicit reviewed overwrite choices, caller deduplication, the two/three-source boundary, source aliases, snapshot-parent collapse, self-citation refusal, disabled/custom folders, and warning-after-save. Apply these cases to both hosts in Task 6.
- [ ] Run writer, summary, wiki, recovery, and concurrent-process tests; update architecture; preflight and commit.

## Task 4: Implement transcript adapters and capture lifecycle

**Files:** Create `hooks/transcripts/__init__.py`, `hooks/transcripts/claude.py`, and `hooks/transcripts/codex.py`. Modify `hooks/capture.py`, `hooks/brain_cli.py`, `hooks/obsidian_session_hint.py`, `hooks/obsidian_session_log.py`, `hooks/obsidian_context_snapshot.py`, `hooks/obsidian_retro_gate.py`, `hooks/obsidian_session_reaper.py`, and `skills/recall/SKILL.md`. Create thin wrappers under `.codex/hooks/` as permitted by the updated single-source test. Modify `tests/test_host_hook_single_source.py`; create `tests/test_host_transcript_source.py`, `tests/test_host_capture_lifecycle.py`, and `tests/test_host_bounded_capture_recovery.py`. Update architecture JSON/HTML.

**Interfaces:** `read_records(context: RuntimeContext, cursor: SourceCursor, deadline: float) -> TranscriptBatch` returns status `ok|partial|unsupported|unavailable`, normalized visible records, source generation, consumed byte offset, and warnings. `capture_checkpoint(context: RuntimeContext, event: CaptureEvent, deadline: float) -> CaptureResult` and `recover_pending(context: RuntimeContext, max_sources: int, deadline: float) -> CaptureResult` use Task 3 transactions. `CaptureResult` carries `status: str` (`complete|pending|filtered|unsupported|unavailable|conflict`), `applied_revision: Optional[str]`, `pending_sources: int`, `loss_of_input: bool`, and `warnings: Tuple[str, ...]`. Deadlines use `time.monotonic()`; wrapper entry records the start time before imports. Define these records in their owning modules and test JSON serialization at CLI boundaries.

- [ ] Add failing tests using Task 1 fixtures: mirrored events, metadata-only files, partial final lines, compaction, truncation/rotation, unknown tools/records, hidden reasoning, forks, absent sources, and source containment.
- [ ] Add failing lifecycle tests for active/ended/interrupted state, complete/partial capture, below-threshold retention, immutable deduplicated snapshots, date boundaries, resume after end, and incomplete existing notes.
- [ ] Add timeout, source-budget, and forced-crash tests. Assert installed-manifest deadline, cursor-after-durable-data ordering, and deterministic recovery continuation.
- [ ] Implement transcript adapters without inventing edits from orchestration text or treating unsupported input as an empty successful session.
- [ ] Wire completed-turn capture, precompaction snapshot, start/resume recovery/context injection, bounded end capture, recall recovery, and vault-doctor recovery. Reaper uses the same services and cannot finalize active sessions or skip existing incomplete notes.
- [ ] Implement native Stop output and per-host/session/turn retro decisions. Capture errors must not become policy blocks. Preserve loop protection and Claude's output contract.
- [ ] Re-run native lifecycle checks in the disposable vault, including abrupt termination and same Codex thread across desktop/CLI. Run parser/lifecycle/recovery and existing hook suites; update architecture; preflight and commit.

## Task 5: Port AI execution and all 19 authored skills

**Files:** Create `hooks/ai_backend.py`; modify four AI functions in `hooks/obsidian_utils.py` and two in `hooks/check_items_cli.py`, their consumers `hooks/consolidate_cli.py`, `hooks/emerge_cli.py`, and `hooks/deep_cli.py`, all 19 existing `skills/*/SKILL.md` procedures, and `hooks/brain_cli.py`. Create `tests/test_host_ai_backend.py` and `tests/test_host_skill_contracts.py`. Create paired `references/host-claude.md` and `references/host-codex.md` under the `standup`, `emerge`, `recall`, `obsidian-setup`, and `retro` skills. Update architecture JSON/HTML.

**Interfaces:** `execute_ai(context: RuntimeContext, operation: str, request: AIRequest) -> AIResult` selects only the invoking native backend. `AIRequest` carries scrubbed input, operation options, input revision, and timeout; `AIResult` carries `status: str` (`ok|unavailable|auth_error|timeout|invalid_output|conflict`), validated result data, input revision, and diagnostic text. Failure leaves the original pending operation durable. Authored skills call shared CLI commands; assistants perform interactive reasoning using their native tools.

- [ ] Inventory six AI sites and every skill's unconditional Claude paths, tool names, identity lookup, and write commands. Check the exact skill set against the spec, including wiki changes from #396.
- [ ] The six functions are `generate_snapshot_summary`, `generate_theme_names`, `generate_summary`, and `generate_summaries_batch` in `hooks/obsidian_utils.py`, plus `run_semantic_merge` and `_dispatch_classifier_chunk` in `hooks/check_items_cli.py`.
- [ ] Add failing tests for no-other-host installation, backend unavailable/auth failure/timeout/malformed result, privacy scrubbing, revision conflicts, and pending-work retention. Assert no cross-provider fallback or model-written vault updates.
- [ ] Add named cases for bounded output, cancellation cleanup, nested-job marker propagation without disabling unrelated policies, and policy-denial results.
- [ ] Implement native backend adapters using observed contracts. Do not invent model aliases or permission bypasses. Hooks cannot dispatch AI.
- [ ] Port each authored procedure to context-aware shared commands. Preserve arguments, semantics, taxonomy, output, memory capabilities, and conflict behavior. Cover setup, diagnostics, dev installs, recall, retro, wiki, and historical imports.
- [ ] Keep shared procedures authored once. Move tool-specific orchestration for the five named skills into their paired host reference files.
- [ ] Run all 19 skill snippets from unrelated working directories, paths with spaces, and isolated host installations. Test forced backend failures before review.
- [ ] Run AI/skill tests and existing summarizer/theme/deferred-work tests; update architecture; preflight and commit.

### Task 5 implementation evidence (2026-10-05)

Task 5 committed as `40ba4a078a66b7e36d225be41e581b2f85bf6ff5` after normal
preflight passed with 5,084 tests, 30 expected failures and 91.28% coverage.
Task 6 packaging, full host/client acceptance and whole-branch review remain
pending. See
[Native AI runtime implementation](../parity/ai-runtime.md) for the current
contracts and focused fixture commands. This is not a shipped parity claim.

- The six shared AI operations dispatch only through the explicit invoking
  host. Requests carry inline scrubbed input and source revisions; outputs pass
  shared strict JSON/schema and semantic validation before private publication.
- Requested aliases and observed full model IDs are separate. Claude success
  uses exactly-one-full-ID `modelUsage` metadata; unknown identity stays unknown.
  Explicit full `classifier_model` controls only Claude classification. Codex
  uses its native model settings. Aliases and unknown defaults cannot authorize
  classifier cache replay, including a prior run's observed alias resolution.
- Cache fingerprints include the actual shared output schema, analysis
  instruction and validation-policy revision, plus local prompt/policy and full
  evidence/input. Focused regressions change each shared contract independently
  and require a cache miss.
- Check-items source SHA comes from the exact bytes parsed before AI and survives
  member reconstruction. A manual edit during fake AI remains unchanged with a
  conflict; independent notes can still save. Exhausted/cancelled classifier
  chunks publish no partial output.
- Private operation artifacts use explicit job IDs, identity/content manifests,
  owner-only modes and symlink checks outside the vault. Summary tests preserve
  user prose and reject stale capture. Bounded read-only session lookup verifies
  full origin identity instead of adopting a short-hash collision.
- CRLF parsing preserves bytes. Legacy summary publication without retained
  capture uses full-document compare-and-swap against the pre-AI SHA; native
  capture uses region checks that preserve outside prose and reject later
  capture. Deep pipelines require explicit context/operation ID and register
  cold and cached private outputs; no unbound direct-writer fallback remains.
  Read-worker focused checks: 66 passed; loaded resolver/reindex/pipeline checks:
  114 passed without skips.
- `tests/test_check_items_native_ai.py` and `tests/test_check_items_cache.py`:
  145 passed after shared-contract fingerprint tests. The mutation-boundary
  fixture suite: 13 passed, including exact deadline-bounded native stdin-pipe
  exceptions. All tests use mocked AI and disposable state. Broader Task 5 and
  Task 6 gates remain required; these counts describe focused checks only.

Task 5 freshness evidence: recall reads the owned summary region without
truncating it at the outer ownership marker, then checks the actual capture
SHA and summary revision. Stale summaries are labeled pending/historical and
excluded from ranking and completion evidence. The focused utils, recall,
snapshot and native-summary checks passed: 195 tests in 2.68 seconds.

Direct registry checks found and fixed double-encoded unsummarized JSON, an
incorrect status-mutation shape, and missing ACTIVE/STALE dashboard entries.
Native deep edits now verify the protected pre-AI source-manifest revision and
actor identity. Partial, stale or skipped updates return nonzero; the legacy
default remains unchanged. The new direct-registry checks are still under
validation. The latest full gate had 4963 passed and 30 xfailed, but failed
coverage at 85.29% against the unchanged 90% requirement. Subprocess coverage
and SHA-verified installed-source measurement are now configured; a fresh full
gate remains required before committing.

## Task 6: Package, verify the full matrix, and review

**Files:** Modify `hooks/hooks.json`, `.codex/hooks.json` and the Codex plugin manifest selected by Task 1, `.claude-plugin/plugin.json`, the single selected marketplace file, `scripts/test-dev-skill.sh`, `scripts/dev-test/`, `scripts/vault_doctor.py`, `scripts/vault_doctor_checks/wiki_pages.py`, `scripts/vault_doctor_checks/memory_index.py`, `README.md`, `CLAUDE.md`, `AGENTS.md`, architecture JSON/HTML, and `.github/workflows/ci.yml`. Change release metadata only through `scripts/bump-version.sh`. Create `docs/parity/capabilities.json`, `docs/parity/acceptance-evidence.md`, `tests/test_host_capability_inventory.py`, and `tests/test_host_behavior_conformance.py`. Add golden host fixtures under `tests/fixtures/hosts/golden/`.

**Interfaces:** Capability entries identify item, host, supported version range, fixture provenance, and `supported|unsupported:<reason>|n/a`. Acceptance entries map each numbered criterion to reproducible evidence and exact tested commit/client version.

- [ ] Add failing exact-set inventory checks for skills, hooks/fields, CLI/wiki commands, memory adapters, config reads, environment reads, and doctor checks. Every opt-out needs an explicit reason; no unsupported required parity item can pass release acceptance.
- [ ] Validate fields per handler, not only event names. Discover environment reads from constant tuples as well as literal accesses. Extend version-sync checks to the selected Codex descriptor.
- [ ] Implement installed plugin selection, trust/permission diagnostics, fresh install, existing-vault pairing, upgrade, custom homes/state/config, and incompatible-writer checks. Verify each host without the other installed.
- [ ] Test Codex dev-install rollback restores both the old cache and its `config.toml` plugin entry after success or failure. Package snapshots exclude `.git`, tests, coverage artifacts, and local state. Published changes bump synchronized manifest versions with the version script.
- [ ] Parameterize capture, writer, CLI, wiki, and skill conformance for both hosts. Compare each output to a fixed golden oracle as well as to its peer; separately verify native identity/tool/provenance fields. Include strict/fresh wiki rebuild and #396 memory/doctor cases.
- [ ] Enforce at collection time that every test on capture/writer/CLI/skill paths uses the `host` fixture or `@host_only(host, reason)` with a matching `unsupported:<reason>` matrix entry. Mutation-test this enforcement by adding a test without either declaration; collection must fail.
- [ ] Add CI coverage for supported Python/platform/client contracts, host-neutral path/config lint, upstream drift, and instruction drift. Make every spec section 12 check a required status check on `develop`. Inspect affected architecture text as well as running the main and sparse smoke tests.
- [ ] Add `.github/pull_request_template.md` with an enforced Codex-impact choice. Run synthetic fixture capture weekly; if scheduled capture cannot run, require equivalent fresh evidence before release. These checks use isolated fixtures and never live vaults or alert channels.
- [ ] Run full preflight after staging, isolated dogfood, forced failures, and all live acceptance cases. Record desktop evidence, deadlines, source/time bounds, independent installs, races, and crash recovery. Never mark a human or platform check passed without evidence.
- [ ] Push and open one PR with `--base develop` and `Closes #272`; verify base. Run one whole-branch review, fix findings, then the mandatory independent Gemini review through `review:deep --phase2-only`. Run a final claims audit against actual files and head SHA.
- [ ] Recollect/record/check the acceptance ledger and require CI green on the final SHA. Present remaining human checks if any; do not claim full parity or close #272 early.
- [ ] Ask for merge approval. After approval, merge to `develop`, close #272 manually if needed, move its card, and verify branch/worktree cleanup. Release promotion is a later release workflow; merging to `main` requires the user's explicit phrase.

## Plan review

- Spec coverage: runtime identity/config/resources, all writers, both transcript adapters, every lifecycle/recovery path, six AI sites, all 19 skills, packaging, wiki/memory/doctor, and the full acceptance matrix each have an owning task.
- Execution order: installed-client evidence first; identity before transactions; transactions before capture; capture before skills; full matrix before review and merge.
- Review cost: one implementation pass, one whole-branch review, one independent cross-model review. No per-task review loops are planned. This is a substantial runtime rewrite; the plan does not promise completion in one short session.
- Approved on 2026-10-05: this architecture and sequence, the seven acceptance criteria, and the proposed recovery/deadline limits. Implementation may proceed; merge approval remains a later gate.
