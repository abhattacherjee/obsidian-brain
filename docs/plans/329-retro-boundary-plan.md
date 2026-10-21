# #329 — Pick the prior retro by `created_at`, not by filename

**Issue:** #329 (absorbs #344, #345, #346, #348, #349, #355). **Branch:** `feature/329-retro-boundary-created-at`.

## Goal

`/retro` scopes a new retrospective to the work after the most recent prior retro. Today it takes the last entry of `bundle["retros"]`, which is sorted by filename. Retro filenames end in an md5 hash, so within one day that order is random. Make the bundle say which retro is most recent, and by which key.

## Design

The pressure-test changed one assumption in the issue. The issue says `created_at` is UTC and "sorts correctly as a string". The live vault disagrees: 662 retros write `created_at` with `+00:00`, 9 write a local offset such as `-06:00`, and 40 have no `created_at` at all. A string sort across mixed offsets is wrong. Example: `2026-10-20T23:00:00-06:00` (05:00 UTC on the 21st) sorts before `2026-10-21T01:00:00+00:00`, but it is the newer instant. So the fix parses each value into an aware datetime and compares instants.

**Key tiers, weakest common key.** Each retro gets the strongest key it can supply:

1. `created_at`: parsed with `datetime.fromisoformat` (a trailing `Z` is read as `+00:00`). A value with no offset is read as UTC, the convention `skills/retro/SKILL.md` documents (`created_at: <ISO-8601-UTC>`).
2. `date`: a valid `YYYY-MM-DD` frontmatter value.
3. `filename`: the stem.

All retros in one bundle are compared on the **weakest tier every one of them supplies**. If one retro lacks a parseable `created_at`, the whole bundle is ordered by `date`, and so on. Mixing tiers would compare a UTC instant with a local date, which is the very bug being fixed. Ties on the chosen key break by stem, so the choice is deterministic.

**Where it lives.** One pure helper in `hooks/obsidian_utils.py`, `_retro_order(retros) -> (sorted_retros, key_name)`. `gather_session_evidence` calls it once:

- every insight-family dict gains `created_at` and `date` (raw frontmatter strings, `""` when absent);
- `bundle["retros"]` is sorted oldest to newest by the chosen key, so "the last entry is the most recent" becomes true;
- a new `bundle["prior_retro_key"]` names the key used: `"created_at"`, `"date"`, `"filename"`, or `""` when there are no retros.

Insights, decisions and error-fixes keep their filename order. They are listed, not selected from, so order there is display only (the issue's scope section).

**SKILL.md.** Step 3a (`:210`) and Step 4 (`:294`) drop "ascending filename order = most recent". They say the last entry is the most recent by `bundle["prior_retro_key"]`, and the `Scope:` line names the key, e.g. `Scope: after [[2026-10-20-retro-0829]] (most recent by created_at)`. The fallback bundle written when the helper crashes gains `"prior_retro_key": ""`.

## Tasks

1. **Tests first** in `tests/test_gather_session_evidence.py`, each failing on `develop`:
   - three same-date retros whose filename order and `created_at` order disagree, asserting `bundle["retros"][-1]` and `prior_retro_key == "created_at"` (C.1, C.2);
   - cross-midnight: a retro whose filename carries the previous local date but whose `created_at` is the newest instant is picked (C.3);
   - mixed offsets: `23:00-06:00` beats `01:00+00:00` the next day (the pressure-test finding);
   - inverse: three different dates select the newest; a single retro selects itself (C.4);
   - one retro with no `created_at` or an unparseable one: no crash, `prior_retro_key == "date"`; no usable `date` either gives `"filename"` (C.5);
   - equal `created_at` instants: the higher stem wins, and it is the same result for both input orders (C.6);
   - the dicts carry `created_at` and `date`.
2. **Code:** `_retro_order` and the `gather_session_evidence` changes above, plus a docstring update.
3. **SKILL.md:** both sites, the `Scope:` line wording and the crash-fallback bundle. Add a test that the phrase `ascending filename order = most recent` is gone from `skills/retro/SKILL.md` and that the file names `prior_retro_key` (C.7).
4. **CHANGELOG** `[Unreleased]` entry.

## Failure modes (self-check classes)

- **Fail-open paths:** an unparseable `created_at` must not crash or silently win. It drops the bundle to the `date` tier. Test: the C.5 cases. A `date` that is not `YYYY-MM-DD` drops it to `filename`. Test: same.
- **Exit codes:** no script exit codes change. The helper already exits non-zero on a crash, and the crash fallback keeps its shape plus the new key.
- **Date boundaries:** cross-midnight UTC versus local date, and mixed offsets. Tests: the cross-midnight and mixed-offset cases.
- **Doc counts:** the CHANGELOG states no counts. Check the PR body numbers against the diff.
- **Broken Markdown:** two SKILL.md paragraphs and one CHANGELOG entry. Re-read 5 lines around each.
- **Unrelated changes:** only `hooks/obsidian_utils.py`, `skills/retro/SKILL.md`, `tests/test_gather_session_evidence.py`, `CHANGELOG.md` and this plan. Run `git diff --diff-filter=D --name-only origin/develop...HEAD`.
- **Inputs:** the helper reads retro frontmatter from the vault's insights folder through `read_note_metadata_detailed`, which is the only copy. A missing field is the C.5 case. No repo/live split.
- **Bypass routes:** not a gate. The nearest risk is a fixture that passes today: a wide-gap fixture with different date prefixes. The C.2 fixture uses one date with disagreeing orders, and every new test must fail on `develop` before the code lands.
