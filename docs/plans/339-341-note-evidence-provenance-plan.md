# #339 + #341: note evidence on repo-backed projects, member-level Rule 0

**Goal:** bring note-completion evidence back to repo-backed projects in `/check-items` without letting a note-derived DONE reach tier HIGH (#339). Also make prefilter Rule 0 match any group member, not only the representative (#341).

**Design ruling (user, 2026-10-21):** use git corroboration, not a model-declared `evidence_source` field. The cap is decided per verdict, from data we control.

## Task 1: per-verdict note cap (#339)

Files: `hooks/check_items_cli.py`, `hooks/open_item_dedup.py`, `skills/check-items/SKILL.md`, tests in `tests/test_check_items_note_evidence.py`.

1. Add `note_cap_for(evidence, project, citation, item_text) -> bool` to `hooks/check_items_cli.py`, next to `note_evidence_only_for()`:
   - Return `True` when `note_evidence_only_for(evidence, project)` is `True`. This covers a missing project name (H4) and a project whose only evidence is notes.
   - Return `False` when the project has no non-empty `note_completions`. Behaviour for those projects is unchanged.
   - Otherwise the project has both notes and git evidence. Return `False` only when some literal ref (same three patterns as `CONFIDENCE_TIER_RULES["HIGH"]["literal_ref_patterns"]`, imported, not copied) appears in the citation AND in the item text AND in the corroboration corpus. Otherwise return `True`.
   - **Corroboration corpus:** `commits`, `tags`, `releases`, `merged_prs` (rendered as `#<number> <title>`), `closed_issues` (same), `changelog_excerpt`. **Never** `fts_mentions` (its keys are item text, so it would corroborate every item), `changed_paths`, or `note_completions`.
   - **Ref matching is boundary-exact:** `#31` must not match `#318` (`#N(?!\d)`). A version ref must not match a longer version (`v3.4` against `v3.4.1`). A sha matches when one is a prefix of the other, with at least 7 hex characters, so a 7-char short sha in `git log --oneline` corroborates a 40-char citation sha and vice versa.
   - **Fail closed:** if the citation or the item text is missing or empty, return `True` when notes are present.
2. `run_classifier`: stamp `note_evidence_only` per record with `note_cap_for(evidence, project, record["evidence_citation"], record["canonical_text"])`, replacing the per-project memo. Keep the record key name `note_evidence_only` (Step 7 and `assign_tier` read it). Update the comment: the key is now per verdict.
3. `skills/check-items/SKILL.md` Step 6 cache-merge block: stamp with `note_cap_for(evidence, project, cached citation, representative)`. Update the Step 5 prose that says note evidence exists only for repo-less projects.
4. `hooks/open_item_dedup.py` `deep_analysis_pipeline`: gather `note_completions` for every project, not only in the `else:` (no repo) branch. Rewrite the #318 I3 comment to say that per-verdict corroboration now holds the safety property.
5. `CLASSIFIER_PROMPT`: update the sentence that says a `note_completions` entry is a valid DONE citation only for a project with no repo. It is valid for any project; the tier cap is applied in code.
6. Update `tests/test_check_items_note_evidence.py::test_repo_backed_project_never_gets_note_completions`. That test pins the old restriction, so invert it: a repo-backed project now does carry `note_completions`. Name the change in the report.

## Task 2: member-level Rule 0 (#341)

Files: `hooks/check_items_cli.py` (`_bridge_project_evidence`), `hooks/check_items_prefilter.py` (`has_classifiable_evidence`), tests.

1. In `_bridge_project_evidence`, also emit `note_completion_keys`: a list of `[basename(file), line]` from each note record. Add it to the shape-B `_TEXT_KEYS` set.
2. In Rule 0, return `True` when:
   - any of the group's `instances` (or `members`) matches a note key by `(basename(file), line)`, or
   - the normalised text of the representative, or of any instance or member, equals a normalised note text.
3. When the note list is empty, the rule changes nothing.

## Failure modes (one line per self-check class)

1. **Fail-open paths:** missing citation, missing item text, missing project and malformed evidence (non-list `merged_prs`, non-dict entries) must all cap (`True`) when notes are present. Tests: `test_note_cap_missing_citation_caps`, `test_note_cap_malformed_bucket_caps`.
2. **Exit codes:** no script exit codes change. Checked: no CLI entry point is touched beyond the stamp.
3. **Date boundaries:** not applicable. No date logic changes; the note scan's strictly-newer rule is untouched.
4. **Doc counts:** CHANGELOG and SKILL.md must not state counts the diff does not support.
5. **Broken Markdown:** the CHANGELOG entry goes inside `[Unreleased]`; edit the SKILL.md Step 5 paragraph in place.
6. **Unrelated changes:** expect one inverted test (Task 1.6) and no deleted files.
7. **Inputs:** the evidence bundle written by `deep_analysis_pipeline` is the only source of truth, read by `run_classifier` and SKILL.md Step 6. A stale or missing bucket means the ref is not corroborated, so the verdict caps (fail closed). Test: `test_note_cap_empty_git_buckets_caps`.

**Bypass routes (gate change):** these are the cheapest inputs that might pass the cap, each with a failing test planned:
- A ref present only in `fts_mentions` (keys are item text): `test_note_cap_fts_mentions_never_corroborates`.
- A ref present only in `note_completions` text: `test_note_cap_note_text_never_corroborates`.
- A prefix collision, `#31` in git against `#318` in the citation and item: `test_note_cap_issue_ref_boundary`.
- A version prefix, `v3.4` against `v3.4.1`: `test_note_cap_version_boundary`.
- A short-sha collision under 7 characters: `test_note_cap_short_sha_rejected`.
- Paraphrased and sentence-cased note citations on a repo-backed project with no git corroboration tier MED: `test_note_cap_paraphrase_repo_backed_is_med`, run end to end through `assign_tier`.
- The cache replay path stamps the same cap: `test_cache_merge_uses_note_cap_for` (a source-level check that SKILL.md Step 6 calls `note_cap_for`).

**Inverse direction:** a corroborated ref on a repo-backed project with notes still reaches HIGH (`test_note_cap_corroborated_ref_reaches_high`). A repo-backed project without notes is unchanged (`test_note_cap_no_notes_unchanged`).
