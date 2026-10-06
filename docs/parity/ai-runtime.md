# Native AI runtime implementation

Task 5 is under validation on the #272 feature branch. Task 6 packaging,
full host/client acceptance and whole-branch review remain required. This
page does not claim shipped Codex parity.

## Execution and model identity

`hooks/ai_backend.py` serves `snapshot_summary`, `theme_names`,
`session_summary`, `session_summaries`, `semantic_merge` and `classify_items`.
An explicit `RuntimeContext` selects the invoking host. No operation falls
back to another provider. Lifecycle hooks do not dispatch AI.

Requests contain inline scrubbed input, its revision, a timeout and validation
options. The backend caps input at 512,000 bytes and output at 1,000,000 bytes.
Native subprocess stdin, stdout, stderr, cancellation and cleanup are bounded
by the deadline. Internal calls carry the nested-AI marker so capture hooks do
not record analysis as user activity; unrelated policy gates remain active.

Requested models and observed models are separate. Claude classifier calls
retain the existing threshold policy unless `classifier_model` supplies an
explicit full Claude model ID. That key affects only Claude classification.
Codex uses `codex_ai_model`, `codex_summary_model` or its native profile/default.
It never receives a Claude classifier alias from this wrapper.

Claude success metadata reports an actual model only when `modelUsage`
identifies exactly one full model ID. Codex reports the model observed through
its native contract. Unknown identity stays unknown. Failure does not stamp a
requested alias as the executed model.

## Validation and cache replay

The shared backend rejects duplicate JSON keys, nonfinite numbers, wrong
schemas and semantic violations. Check-items also verifies known IDs,
project boundaries, merge totals and exact classifier coverage. Every
successful chunk must verify its requested IDs. Retryable exhausted chunks stay
unclassified while later verified chunks can remain in a partial result. No
checkoff is proposed for an unclassified group. Cancellation, authentication and
policy failures end the run and preserve prior output.

Classifier cache keys include the full input and evidence, selected vault and
canonical project, local prompt and threshold/chunk/prefilter policy, and the
actual shared backend contract. `ai_contract_identity()` supplies its output
schema, analysis instruction and validation-policy revision. Changing any of
these invalidates replay even if the local prompt label stays unchanged.

Successful records retain the observed backend, full model ID and prompt
fingerprint. Claude `haiku`, `sonnet`, `opus` and `fable` aliases cannot prove
which model will execute next. They therefore miss before execution unless an
explicit full `classifier_model` establishes the selection. A previous run's
observed alias resolution cannot authorize replay. Unknown Codex defaults also
miss. Matching explicit known models may replay only when the other inputs and
policies match.

Setup's unknown model defaults disable classifier cache replay. Stage 8 reports
`cache_disabled` and skips storing an unusable classifier cache. Configure an
explicit full Claude `classifier_model` or a Codex model to enable replay;
matching evidence and policy are still required. A logged-out Claude JSON
result is an authentication error, including when stderr is empty. Its private
result text is not returned in diagnostics.

## Publication and identity

Fixed shared skill handlers use explicit operation IDs. Owner-only artifacts
live under the vault/host/session/project-scoped private `jobs/<uuid>` directory,
outside the vault. Registered artifacts use content hashes, checked names,
0600 files and 0700 directories. Symlink or foreign-operation paths are refused.
Models receive inline input and return data; they do not choose publication
paths or write vault notes.

Source revisions are captured from the exact bytes read before AI. Check-items
carries `source_revision` through each member to cascade. A later manual edit
leaves the affected file unchanged and reports a conflict; independent notes
can still save. Summary publication binds its result to retained capture and
managed-region revisions. Stale capture leaves the source intact and queues
later work; existing user prose survives valid summary publication.
CRLF parsing preserves original bytes. Legacy notes without retained capture
use full-document compare-and-swap against the pre-AI SHA. Native capture uses
managed-region compare-and-swap to preserve outside prose and reject later
capture. Deep evidence pipelines require an explicit context and operation ID;
both cold and cached outputs are registered private artifacts. The unbound
pipeline has no direct-writer fallback.

Native deep edits verify the protected pre-AI source-manifest revision and
actor identity, then return nonzero for partial, stale or skipped updates.
The legacy default remains unchanged. Direct registry checks also corrected
double-encoded unsummarized JSON, the status-mutation payload, and omitted
ACTIVE/STALE dashboard entries. These checks remain under validation; the
latest full gate passed 4963 tests but failed the unchanged 90% coverage gate
at 85.29%. No Task 5 commit or full-gate success is claimed.

`session_lookup.py` uses a read-only index and bounded frontmatter reads to
verify full host/native session identity before adopting an existing note.
Short hashes do not prove identity. Ambiguous matches, candidate overflow,
unreadable indexes and expired deadlines stay pending. Missing indexes create
nothing. Lookup does not rebuild an index or scan the whole vault.

## Reproducible fixture checks

Tests use disposable vaults, state and databases. Native AI transports are
mocked; the suite requires no host credentials or live service calls.

| Contract | Tests |
| --- | --- |
| Native execution, schema, bounds, failure and observed model | `tests/test_host_ai_backend.py`, `tests/test_host_codex_ai_contract.py` |
| Exact coverage, alias remapping, schema/policy cache invalidation and source conflicts | `tests/test_check_items_native_ai.py`, `tests/test_check_items_cache.py`, `tests/test_check_items_chunk_failure.py` |
| Bound helper context and summary compare-and-swap | `tests/test_host_summary_ai.py`, `tests/test_host_summary_publication.py` |
| Private operation identity and artifacts | `tests/test_host_operation_state.py`, `tests/test_host_pipeline_state.py` |
| Full session identity and bounded adoption | `tests/test_host_session_lookup.py`, `tests/test_host_archived_context.py` |
| Exact private-file and stdin-pipe writer exceptions | `tests/test_vault_mutation_boundary.py` |

Run the cache and writer checks with:

```sh
python3 -m pytest tests/test_check_items_native_ai.py tests/test_check_items_cache.py tests/test_vault_mutation_boundary.py -q
```

Run the backend, summary, operation-state and identity checks with:

```sh
python3 -m pytest tests/test_host_ai_backend.py tests/test_host_codex_ai_contract.py tests/test_host_summary_ai.py tests/test_host_summary_publication.py tests/test_host_operation_state.py tests/test_host_pipeline_state.py tests/test_host_session_lookup.py tests/test_host_archived_context.py -q
```
