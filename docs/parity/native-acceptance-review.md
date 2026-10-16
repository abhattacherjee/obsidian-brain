# Fresh-session acceptance review

Use this after the executor sessions. Review bytes and assertions, not their
chat summaries. This review produces a proposed checklist and gate results.
It does not automatically merge or publish anything.

## Reviewer prompt

```text
Review the Obsidian Brain native parity acceptance campaign.

PLAN=<absolute path to native-test-plan.md>
CAMPAIGN_ROOT=<absolute private evidence root containing all run reports>
CANDIDATE_SHA=<full frozen 40-character SHA>
SOURCE_CHECKOUT=<absolute checkout of that exact SHA>
ISSUE=272
PR=419

Read the plan, issue/spec acceptance criteria, capabilities.json and the
acceptance validator from SOURCE_CHECKOUT. Inventory every run and verify
manifest/report identity, exact installed hashes, client/version/platform,
artifact containment and SHA256 before reasoning about PASS. Do not trust a
passed label whose underlying assertion is absent or contradicted.

Check every required client/platform cell, lifecycle case, installed skill,
shared capability and paired identity/replay test. Explicitly separate native
dispatch from discovery, source CI, manual handler calls and copied fixtures.
Require matching real Desktop task and hook evidence, not bundled CLI metadata.
Required initial cells are CC-M/CX-M/CD-M/CC-L/CX-L; Linux Desktop is out of
scope under the issue's explicit Acceptance gates. Verify named environments,
independent-provider inventories and Desktop isolation/shared-home readiness.
Check declared_client_launch.binding_method for every required cell. Claude
uses fixed-claude-client: require client claude-code, the accepted host-constant
decision source, and evidence that a conflicting inherited declaration was
rejected. No pre-launch declaration is required for Claude. The constant alone
does not prove actual native dispatch or session identity.
Codex uses operator-launch-declaration: read trusted pre-launch declaration and
inherited-value receipts for codex-cli or codex-desktop. An agent-created Codex
OB_CLIENT is synthetic. Until actual Codex B.2 transport is verified, its
positive K/A rows remain BLOCKED.

For B.2/L/X.1/A.1 require installed event/operation receipt, client-owned
native source ID plus case marker/hash/offset, note/DB provenance, and independent
origin observation. Matching hashes cannot rule out a manual same-ID handler
call. Inspect native tool actions and observer coverage/positive controls.
Missing origin proof means BLOCKED. For AI skills verify native selection,
loaded resource and actual backend execution, not merely cached output.

Check protected-path before/after audit, attribution and exactly one enabled
candidate plugin. Plugin-caused live writes are FAIL; unexplained changes or
unauditable boundaries prevent isolation PASS. Check F.5 controls and actual
hook-descendant process/file/API observation, plus external dispatch/spawn/exit
timing for L.5. Missing instrumentation is not a waiver.

Evaluate case_readiness and each native_origin_observer.methods entry by its
covered assertions and positive controls. B.1 requires pinned package/private
paths and the complete protected baseline. B.2 needs a controlled client-owned
dispatch/debug record or independent external dispatch observation. After B.2
and the protected audit pass, K/A can use independently collected client-owned
loaded-skill/tool/backend receipts.
Missing F.5 process/file/network coverage blocks F.5, not unrelated K/B/A cases
whose own binding, origin, revision and isolation proof passes. Missing full
external timing blocks L.5. Codex hook origin remains unproven without actual
transport controls. Agent claims and synthetic calls cannot fill these gaps.

Reconcile all six backend operations and every authored analysis path, one
result/receipt per required cell. Require released-Claude baseline comparison
with immutable SHA and authority-cited difference allowlist. Check custom
folders/wiki-disabled fixtures and actual six-dashboard Dataview query results;
static text is supplemental. Do not accept untested baselines or invented
allowlists.
Read operator decisions for fork notes, retired unreadable sources and any
platform scope exception. Missing decisions/evidence remain BLOCKED.

For each of the seven criteria, list every clause, its required tests/matrix
cells, evidence references and PASS/FAIL/BLOCKED/NOT-RUN result. A criterion is
PASS only if every required clause has verified evidence. Missing reports or
skill rows are NOT-RUN, not PASS. Contradictory native observations beat a
successful unit fixture. Preserve earlier failures in campaign history.
Also map every one of the issue's ten Acceptance gates to criteria and cases;
output a ten-row gate checklist. The ship parser only discovering seven
criteria does not drop dashboard, folder, isolation or scope requirements.

Write criterion-checklist.md, criterion-checklist.json, a defect/blocker list
and proposed acceptance-ledger.json in a new private review directory. Build
sanitized native-client-dispatch and acceptance-criterion JSON receipts only
from verified passing observations. Hash their exact bytes and retain a
traceable link/hash to detailed run evidence. Do not relabel a whole partial
run passed to satisfy the validator. Do not edit product code, invent version
ranges, or tick issue/spec checkboxes during this review.

Run the repository acceptance validator against the proposed ledger and
exact candidate capability inventory; save exit code/stdout/stderr. It may
fail because capability support/version/provenance is still pending. Report
that separately from native test failures. Do not modify the matrix just to
make validation green. Run the current ship ac_gate check as a read-only
observation and save its result too.

Return a concise seven-row checklist, verified client/platform coverage,
specific missing evidence/defects and artifact paths. If everything passes,
prepare exact proposed issue/spec ticks and publication steps for the operator.
Do not merge or publish GitHub variables from this instruction alone.
```

## Two different ledgers

1. `report.json` belongs to one executor and may contain failed/blocked cases.
2. `acceptance-ledger.json` follows the repository validator: `tested_sha`,
   three `clients`, and criteria `"1"` through `"7"`. Its `status: "passed"`
   is justified only by complete verified assertions. Use the tracked
   [pending ledger](acceptance-ledger.json) as the starting structure.

Do not feed an executor report directly to the acceptance validator or to the
ship skill's `ac_gate.py`. The ship gate has its own collected issue/spec rows.

For a verified passing client, reference a small receipt such as:

```json
{
  "kind": "native-client-dispatch",
  "tested_sha": "<exact candidate SHA>",
  "status": "passed",
  "client": "codex-desktop",
  "version": "<observed Desktop app version>",
  "dispatch_verified": true,
  "observations": [
    {
      "platform": "macos",
      "case": "B.2",
      "action": "<actual native task action>",
      "run_report": "<private run ID and report hash>",
      "receipt": "<verified dispatch artifact and hash>"
    }
  ]
}
```

For a fully verified criterion, use `kind: "acceptance-criterion"`,
`criterion: "1"` (or its actual number), `tested_sha`, `status: "passed"`
and detailed clause/test/evidence references. These are examples of shapes,
not evidence to copy. The repository validator checks selected envelope
fields and hashes; the reviewer must still check all semantic assertions,
matrix cells and skill coverage.

Client ledger entries use actual `version`, `status`, `dispatch_verified`
and `evidence: [{"path": "client-desktop.json", "sha256": "<hash>"}]`.
Criterion entries use `status` and the same hashed reference shape. Keep each
receipt below the validator's one-million-byte cap. A client receipt may list
several platform observations; do not discard their separate run provenance.

## Check the proposed evidence

Run from SOURCE_CHECKOUT with absolute ledger path and the frozen SHA:

```bash
python3 scripts/ci-checks/check-parity-acceptance.py \
  --matrix docs/parity/capabilities.json \
  --ledger "$REVIEW_ROOT/acceptance-ledger.json" \
  --sha "$CANDIDATE_SHA"
```

Save the actual exit status even when nonzero. The validator also requires
every required capability to be supported for both hosts with observed version
range and fixture provenance. Those fields are currently pending in the
initial candidate. Passing native tests does not automatically update them.

If evidence warrants capability edits, prepare a separate reviewed change
with precise observed versions and provenance. That creates a new candidate
SHA. Reconcile the installed bytes and rerun affected native tests and required
source gates for it; do not reuse an old ledger's SHA. Use exact observed
versions rather than claiming a wider untested compatibility range.

The ship issue/spec checklist is a separate final gate. Locate the installed
ship skill, read its current instructions, and run:

```bash
python3 /absolute/installed/ship/scripts/ac_gate.py check --pr 419
```

Unticked issue/spec criteria correctly keep this check nonzero even if the
proposed private acceptance ledger has complete evidence. Only after reviewed
evidence warrants the ticks should the authorized shipping workflow collect,
record and check them for the current head. Never bypass either gate.

## Optional publication after explicit direction

Prepare a UTF-8 JSON bundle with only `ledger` and `artifacts`; the latter maps
safe direct `.json` filenames to their exact JSON text strings. Every artifact
must be referenced by the ledger with a matching SHA256. Keep the bundle at
most 48 KiB and omit raw private logs, authentication and hidden reasoning.

Validate in a new private directory using a file read through Python rather
than printing the bundle:

```bash
python3 - "$BUNDLE_PATH" "$CANDIDATE_SHA" "$CHECKED_OUTPUT" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
env = dict(os.environ)
env['NATIVE_ACCEPTANCE_BUNDLE'] = Path(sys.argv[1]).read_text(encoding='utf-8')
result = subprocess.run([
    sys.executable, 'scripts/ci-checks/check-parity-acceptance.py',
    '--matrix', 'docs/parity/capabilities.json', '--bundle-env',
    '--output-dir', sys.argv[3], '--sha', sys.argv[2],
], env=env, stdin=subprocess.DEVNULL, timeout=60)
raise SystemExit(result.returncode)
PY
```

An authorized operator can then publish the validated same-SHA bundle:

```bash
gh variable set -R abhattacherjee/obsidian-brain \
  "P_$CANDIDATE_SHA" < "$BUNDLE_PATH"
```

The existing acceptance workflow consumes the exact SHA variable. Read its
current dispatch inputs before invoking it; do not invent them. Native
acceptance skipped on a feature PR to `develop` is not a passing native check.
Preserve the source CI evidence and run metadata separately.

## Required final checklist

| Criterion | Result | Verified clauses and client/platform cells | Missing evidence or defect | Hashed references |
| --- | --- | --- | --- | --- |
| C.1 | NOT-RUN until reviewed | | | |
| C.2 | NOT-RUN until reviewed | | | |
| C.3 | NOT-RUN until reviewed | | | |
| C.4 | NOT-RUN until reviewed | | | |
| C.5 | NOT-RUN until reviewed | | | |
| C.6 | NOT-RUN until reviewed | | | |
| C.7 | NOT-RUN until reviewed | | | |

Keep the human-facing checklist short. Put commands, byte-level evidence and
timing details in the private artifacts. A ready-to-merge result requires both
verified native evidence and successful current-head repository/ship gates.
