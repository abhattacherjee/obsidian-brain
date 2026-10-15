# Task 6 acceptance evidence

No required parity criterion is certified by this draft. Each final record must
name the tested commit, client version, platform, fixture/content hashes,
command and result. Fixtures alone do not prove installed native dispatch.

Task 5 committed as `40ba4a078a66b7e36d225be41e581b2f85bf6ff5` after the normal
preflight passed: 5,084 tests, 30 expected failures, 91.28% coverage and five
warnings in 551.87 seconds. Its log is
`/private/tmp/obsidian-272.pkcGgyS9/task5-preflight-complete.log`.
That gate covers Task 5, not the final Task 6 tree.

| Criterion | Required evidence | Current status |
| --- | --- | --- |
| 1 | All 19 skills and four lifecycle events plus recovery on Claude Code, Codex CLI and desktop | Pending; desktop hook dispatch blocked |
| 2 | Independent host installs; invoking native AI; failure retention; hooks without AI/scans | Pending installed final package checks |
| 3 | Legacy migration and shared wiki/memory/doctor behavior | Pending both-host golden checks |
| 4 | Equal IDs, CLI/desktop sharing, fork, resume, worktree movement/deletion, cache/snapshot identity | Pending final host/client matrix |
| 5 | Native parser fixtures, explicit partial/unsupported/unavailable status, crash replay and user-byte preservation | Task5 fixtures exist; final SHA acceptance pending |
| 6 | Measured SessionEnd/recovery bounds, containment, custom state/config, spaces, upgrade and incompatible writer diagnostics | Pending installed measurements and rollback checks |
| 7 | Full preflight, coverage, security, database isolation, inventory, host conformance, required CI and live desktop evidence | Pending final staged tree and CI |

Native-format cases may declare their record origin through an explicit
`host_only` capability. Reusable normalized capture, writer, CLI, skill,
migration, fork, config/environment, wiki, memory and doctor behavior requires
both invoking hosts. A format declaration cannot waive those requirements.

Scheduled fixture jobs must use synthetic data and isolated homes/vaults. They
record native binary/client versions, commit, platform, manifest/hook hashes,
fixture hashes and observed contracts without authentication values or hidden
reasoning. A missing binary, refused hook access or unavailable capture adapter
is a failed/blocked capture, never fresh evidence. If weekly capture cannot run,
equivalent fresh native evidence is required before release. Desktop bundled
CLI metadata alone does not prove a desktop task dispatched a hook.

The actual Desktop probe is blocked. An earlier automatic approval review
rejected a CUA probe for missing authorization. The user then explicitly
approved CUA inspection. On 2026-10-06, the authorized `cua.getApp("Codex")`
attempt still returned:

> Computer Use is not allowed to use the app com.openai.codex for safety reasons.

No UI observation or native dispatch proof was obtained. This is a tool
restriction, not a pending user approval. Evidence:
`/private/tmp/obsidian-272-native-cua-attempt.txt`.
Do not bypass the restriction through another automation path. No substitute
CLI or simulated frontend counts as Desktop dispatch evidence. The native
event contract currently has no observed event-local client field; a daemon
environment snapshot cannot prove which frontend invoked the event. Explicit
frontend binding remains pending.

A separate native read-only sandbox check passed. The disposable target was
writable outside the sandbox, then `codex sandbox -c
'sandbox_permissions=["disk-full-read-access"]' -- python3` refused its write
with `PermissionError`, left it absent and exited zero. This check ran no model.
Its scope is the sandbox command, not nested AI execution or Desktop dispatch.
Evidence: `/private/tmp/obsidian-272.pkcGgyS9/native-readonly-denial-evidence.json`.

Task 6 focused checks passed with the collection/runtime guard enabled: 371
public doctor/wiki cases and 115 source-session cases. Native recovery retains
the selected actor, checks the source SHA recorded by the audit, preserves an
intervening manual note, and fails incomplete or duplicate native input. These
results cover the working tree; they do not certify its final commit.

The reviewed inventory separates plugin `config_reads` from native RPC
`native_config_reads`. Environment aliases and loop keys follow lexical
bindings. Row payloads and unrelated validator loops do not become config or
environment fields. Unknown real environment keys remain unresolved and fail
review. The exact source inventory baseline was updated after manual review.

Acceptance evidence must reference JSON artifacts by relative path and SHA256.
The gate checks containment, bytes, client/version, criterion and exact tested
commit. A status label or binary metadata cannot replace native dispatch proof.
The weekly job runs the existing isolated capture/golden harness and records
Python/pytest versions, source hashes and checkout state in a separate
`synthetic-host-conformance` record. Its native client versions remain null and
`native_dispatch_verified` remains false. Installed capture is unavailable;
fresh native evidence remains required before release.

The CI acceptance input lives outside the source commit. For a PR, checkout,
validation and lookup use `github.event.pull_request.head.sha`; a push uses
`github.sha`. A repository Actions variable named `P_<40-character-SHA>` holds
a UTF-8 JSON bundle of at most 48 KiB. Its only keys are `ledger` and `artifacts`.
`ledger` has the existing acceptance-ledger schema. `artifacts` maps each safe,
direct `.json` filename to its exact JSON text bytes as a JSON string. Every
artifact must be referenced by the ledger, and its SHA256 must match those
bytes. Duplicate keys, missing files, path escapes, wrong commits and incomplete
native proof fail. The tracked pending ledger remains a progress record.

After actual acceptance has produced sanitized evidence for the feature commit,
an authorized repository administrator can publish the checked bundle:

```bash
NATIVE_ACCEPTANCE_BUNDLE="$(cat bundle.json)" \
python3 scripts/ci-checks/check-parity-acceptance.py \
  --matrix docs/parity/capabilities.json --bundle-env \
  --output-dir /private/tmp/native-acceptance-checked --sha "$FEATURE_SHA"
gh variable set -R OWNER/REPO "P_$FEATURE_SHA" < bundle.json
```

Use a fresh output directory. `gh variable set` reads the file from stdin;
it has no `--body-file` option. Do not publish a synthetic fixture record as
native proof. No acceptance variable has been written for this change because
actual Desktop dispatch remains blocked. CI uploads the validated bundle as
`native-acceptance-<SHA>` for audit only; it never selects a moving latest artifact.
GitHub documents [context index lookup](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)
and the [48 KB variable limit](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).

Native acceptance runs on explicit dispatch and on `release/*` or `hotfix/*`
PRs to `main`. Feature PRs to `develop` run the shared contract gates without
claiming native certification. The ship gate still requires native proof for
this parity PR. Before a release bundle can pass, each required capability in
`capabilities.json` must name its verified client version range and fixture
provenance. Pending capability records cannot certify a release.

## Native CLI progress, 2026-10-15

These are partial test results, not acceptance certification. All seven issue
criteria remain unchecked. The native CLI results below use source commit
`2ed4b3502b15ffb7c35e1c825a14f57f1f956eec`.

- Normal preflight on `1de35c85c29fc6bf60f6e3071e5a7c4ba91ca167` passed
  9,808 tests with 91.48% coverage. The earlier Python 3.9 focused run passed
  176 tests. CI run `38765148573` passed in 352 seconds, versus 514 seconds for
  `38758588224`: 162 seconds saved, a 31.5% reduction. Both measurements cover
  workflow creation through the last completed job. Host conformance run
  `38765148000` also passed; its feature-branch native acceptance job was skipped.
- The installed Codex CLI read a historical source larger than the old 4 MiB
  batch limit: 4,229,273 bytes and 563 normalized records. Two native helpers
  produced reviewed summaries. Normal publication succeeded for both selected
  sources; a deliberate filename collision was refused; unchanged imports were
  skipped without duplicate notes. Independent checks verified the published
  bodies, source hashes, configuration and destination sentinel.
- A real resume recorded `SYNTHETIC_IMPORT_GROWTH_272_2ED4B35` and exited with
  wrapper status 0. Normal import then updated the existing summary from the
  grown source while preserving both manual markers, including one added after
  the read. Both replays skipped without duplicate notes. Independent checks
  verified the exact reviewed summary, complete manual tail and six physical
  files. The parent exited normally with wrapper status 0 and process births
  confirmed gone.
- Positive independent process samples recorded registered SessionStart, Stop
  and SessionEnd arguments with `--client codex-cli`, `--require-cli-launcher`
  and direct native parentage. These samples do not prove the complete native
  dispatch-to-exit deadline or the absence of other calls.
- Native `dev-test` remains open. Its first successful install preserved the
  backup but omitted bundled templates and dashboards. Acceptance requires
  another installed-package run after the packaging repair, followed by restore
  and fresh-session checks.

Local receipts are under the private campaign directory
`/Users/abhishek/.local/state/obsidian-272-parity/20261015`. Filenames below are
relative to that directory. They are progress receipts, not a release bundle.

| Receipt | SHA256 |
| --- | --- |
| `preflight-uri-scrub-final.log` | `e87fefdd89477f2904fd75e89393c09179ccb8bf3159f1c11e1fd2b60f82c912` |
| `independent-native-import-publication.json` | `327f82461e6dff3aed023ad3d3ad5598f23282c1a13913513c2548913a6499b4` |
| `native-import-final-subset.json` | `74f49d458140deae7aaf8dceb77ebff58a25552888c72b2339641396c9eabeb5` |
| `owned-import-helper-dispatch-metadata.json` | `2dde4205b8d4ca7b5df820c042ca054faf5c48ead2a52c81050b3d2aa9e3e3d7` |
| `close-2ed4b350-1792079057998907000.json` | `95d8f1c3fe61a1b55b5831d795cebd84f8ae71f80cab2964136534fed1bdcd5e` |
| `native-import-grown-final-subset.json` | `e4832f271cadc817cc9bc99836fcf91046ce7be3777e143a84e5b48d8b914220` |
| `independent-native-import-growth.json` | `619e38bb3f844176b9f2e7b289bc94fb60cc29b5430babca9e45b3d08050909e` |
| `independent-growth-hook-arguments.json` | `341d204e4d28b1b928233bc18ac2d2ed9c71442a13cc51a7837e76d1dbcfdbcd` |
| `close-2ed4b350-1792079603578003000.json` | `a7429d139dcab5171ff7308b9b295189ed7b4764661afa625700519eb33691f0` |
| `ci-1de35c8-measured-comparison.json` | `331fdfff8a7c6f8b096297c247fa9b8a2baca4504183b32b07ae607ca98f44e0` |
| `preflight-runtime-assets-final.log` | `f5067c6a91c8820f781e739d35cfdba923d49a9e2d19968e312702242bf65a3a` |
| `ci-863e56c-measured-comparison.json` | `0a04097e599e0a10566c51a5d7ebd77e14ffec4f50472ecd4e7fd287a0832570` |
| `conformance-38769398945-run.json` | `d8064477c51beee6bc374ba66ca3877a022f6102c99b3f278af77cf6926a8819` |
| `conformance-38769398945-jobs.json` | `b7f2c32580c1f1ce44b7994e920c06e36f0643fdb19c8a5206854157ce6a0bf7` |
| `install-863e56ca-1792082867456768000.json` | `c2260196312d625929a30fed0920cd75645ddd5c6f4c06043fd01ab9255a8013` |
| `native-evidence-1792083772077081000/metadata.json` | `08e9f59286756fca728c0a7f23ed908c7d8540622f3f96e1b1ef96d3cc90d4cf` |
| `native-evidence-1792083772077081000/423f92a1484d-dev-test-install-863-review.json` | `f911b88fc4850584cb092b23a0e3360a75731d6070f347557f8f6a4487a87a38` |
| `native-evidence-1792084077819434000/metadata.json` | `69e6170f2bcd9e889b4c0c78b876d65c1316911a40e529a37f9ddabd9e9e83fd` |
| `native-evidence-1792084077819434000/d74c703f7a11-dev-active-new-session-review.json` | `a670c850b152e16dbf68cf885ff054486b495e576c3631b5da592dc9e2319bcc` |
| `native-evidence-1792084157261750000/metadata.json` | `ff0b5c53af508a2c4da505b284578201e9221758341ea05f46ed89ad2b91e680` |
| `native-evidence-1792084157261750000/488089576e8c-dev-restored-review.json` | `8e929f641abc08b9dfb55c28075aeeafd90d49bb23476b5a73490f92135aad75` |
| `native-evidence-1792084460261181000/metadata.json` | `bfd9655adf9a0686aaab1ea19ac0fdc70c40cfa91167880d9365cb479dfaaf47` |
| `independent-final-dev-cycle-863.json` | `96676241eddfd3094c00553ae6b8136351f4b04eebd5a5f6e16fdeddafb61547` |
| `close-863e56ca-1792083378908029000.json` | `28704f62f498d0e5e52d720819afef92c7fddeca72036594a2cc3b2fa84eab71` |
| `close-863e56ca-1792083793894011000.json` | `edf8934c36b2d1c31df27bb3c816059ab0faac174729ea4fd8149ae409bf6927` |
| `close-863e56ca-1792084166892055000.json` | `4539579332bdb31939889c6b6abbd25ed6144d5c1e54f8ee64c6cdae324a801a` |
| `close-863e56ca-1792084538900795000.json` | `acde16c61dfd8e104c96d34673beb9513a9b4021544627552b09c3a074952beb` |

The import subset contains selected bounded sidecars only. One raw output
exceeded the normal collection limit and remains untouched in the VM. This
subset does not prove complete trace coverage or the absence of other calls.
It does not qualify the full 19-skill plan, lifecycle and cache checks, native
Desktop dispatch, dashboard checks or Claude regression testing. Desktop
binding remains unimplemented and unverified; Claude testing remains paused
until the Codex plan passes.

### Later checkpoint on 2026-10-15: fixed native dev-test cycle

The earlier packaging failure above is retained. The completed cycle below uses
`863e56ca8ffd2196c0836cd8a52940d292b057a9`, tree
`45d97184ee869245f3183e77a93f8db55f5096af`; it does not transfer all earlier
native results to this source.

- Normal preflight passed 9,810 tests out of 9,843 collected, with 3 skipped
  and 30 expected failures, at 91.48% combined coverage. The parallel phase
  passed 9,554 tests in 637.16 seconds; the separate serial phase passed all
  256 timing cases in 102.78 seconds.
- [CI run 38769399058](https://github.com/abhattacherjee/obsidian-brain/actions/runs/38769399058)
  passed in 341 seconds, measured from workflow creation to the last completed
  job. The same measure was 514 seconds for baseline run `38758588224`:
  173 seconds saved, a 33.6576% reduction. This measures workflow wall time,
  not a controlled CPU benchmark.
  [Host conformance run 38769398945](https://github.com/abhattacherjee/obsidian-brain/actions/runs/38769398945)
  passed; native acceptance was skipped.
- The normal native plugin manager installed all 477 canonical source files.
  Its retained install receipt binds installed-verification SHA256
  `542ddf99e739ed2a376ba23445e694dffd87a6c78fa5a5b7e822808c50b1f172`.
  Actual native `dev-test install` then exited zero, preserved all 498 original
  cache files and installed exactly 162 runtime files, including all 14 bundled
  template/dashboard entries. The curated runtime intentionally excludes
  repository documentation and tests.
- A new native session loaded that curated runtime and returned
  `dev_active: true`; all 649 recorded readiness checks passed. Normal
  `dev-test restore` exited zero. A further new native session loaded the
  restored full 477-file source and returned `dev_active: false`, with status
  exit zero and protected files unchanged.
- Independent final filesystem checks matched every original cache entry:
  560 entries, 498 files, no missing or changed entries, and no added entries.
  All 21 existing generated bytecode files and the three generated files in
  the source export passed header and reviewed-source code checks. Recovery
  backup and configuration record were absent. The selected config, native
  config and synthetic sentinel retained their exact hashes and mode `0600`.
- The first strict inventory attempt and its normal restore remain recorded:
  one added generated bytecode file caused that attempt to stop, with no
  original entry missing or changed. It was not relabeled as a successful
  inventory. All four dev-cycle clients later exited normally with wrapper
  status zero, their observed process births gone and observers closed.

These results qualify this scoped native CLI dev-test cycle only. The D8
model-pinned classifier-cache test is at readiness, with no cache PASS yet.
All seven issue acceptance criteria remain unchecked. The full 19-skill plan,
remaining lifecycle/cache checks, complete native end-to-end deadline,
Desktop acceptance, dashboard checks and Claude regression testing remain
pending. Desktop implementation work is underway; it is not certified.

L.4 remains unqualified. Source review of the official CLI
`0.159.0-alpha.12.1` and Desktop-bundled CLI `0.162.0-alpha.17.2` shows
`SessionStart` with `source: clear` identifies the replacement session; it
carries no outgoing session ID or clear-transition witness. `SessionEnd`
provides only `reason: other`. See the exact
[0.159 hook contract](https://github.com/openai/codex/blob/rust-v0.159.0-alpha.12.1/codex-rs/hooks/src/schema.rs#L499-L523),
[0.162 hook contract](https://github.com/openai/codex/blob/rust-v0.162.0-alpha.17.2/codex-rs/hooks/src/schema.rs#L799-L801)
and [replacement sequence](https://github.com/openai/codex/blob/rust-v0.162.0-alpha.17.2/codex-rs/tui/src/app/session_lifecycle.rs#L1010-L1035).
A thread registry cannot prove the foreground thread: resuming B without a turn
can leave its hook queued before clear creates C. Neither a guessed newest
source nor a fabricated clear payload is acceptable. This is source-contract
proof only; an actual native clear reproduction is **NOT RUN**. The unsubmitted
`H/upstream-codex-clear-witness-proposal.md` (SHA256
`c18440300fe46b1f5449510e7074fab3f14d87df65f43697b8a0da831ba9e394`)
proposes an upstream witness; it is not a fix, submission or compact equivalent.

Claude testing remains paused until the Codex plan passes.
