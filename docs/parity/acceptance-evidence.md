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
criteria remain unchecked. The tested source commit is
`2ed4b3502b15ffb7c35e1c825a14f57f1f956eec`.

- Normal commit preflight passed: 9,797 tests, 91.48% coverage. The separate
  Python 3.9 focused run passed 176 tests. Published CI run `38758588224` passed
  in 514 seconds, unchanged from its predecessor. Further shard changes still
  require full preflight and a measured CI run.
- The installed Codex CLI read a historical source larger than the old 4 MiB
  batch limit: 4,229,273 bytes and 563 normalized records. Two native helpers
  produced reviewed summaries. Normal publication succeeded for both selected
  sources; a deliberate filename collision was refused; unchanged imports were
  skipped without duplicate notes. Independent checks verified the published
  bodies, source hashes, configuration and destination sentinel.
- A real resume recorded `SYNTHETIC_IMPORT_GROWTH_272_2ED4B35` and exited with
  wrapper status 0. Importing that growth and preserving concurrent manual edits
  have not yet passed.

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

The import subset contains selected bounded sidecars only. One raw output
exceeded the normal collection limit and remains untouched in the VM. This
subset does not prove complete trace coverage or the absence of other calls.
It does not qualify the full 19-skill plan, lifecycle and cache checks, native
Desktop dispatch, dashboard checks or Claude regression testing. Desktop
binding remains unimplemented and unverified; Claude testing remains paused
until the Codex plan passes.
