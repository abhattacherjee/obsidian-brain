# #449: derive importance for curated notes at index time

Branch `feature/449-curated-importance`, milestone v3.9.1, epic #450.

## Decisions (user, 2026-10-23)

- Values: auto-capture 3, hand-saved 6. Session notes keep their Haiku score.
- Derive at index time; no note is rewritten. An `importance:` frontmatter line (integer 1–10)
  overrides the derived value.
- Scope: `claude-insight`, `claude-decision`, `claude-error-fix` only.

## Tasks

### Task 1: `_derive_importance` and `_upsert_note` (`hooks/vault_index.py`)

- Constants: `IMPORTANCE_AUTO_CAPTURE = 3`, `IMPORTANCE_HAND_SAVED = 6`,
  `CURATED_IMPORTANCE_TYPES = ("claude-insight", "claude-decision", "claude-error-fix")`.
- `_frontmatter_importance(raw) -> int | None`: a plain decimal integer string 1–10 (after the
  parser's quote strip), else `None`. `"6.5"`, `"high"`, `"0"`, `"11"`, `""`, `"+6"` are invalid.
- `_derive_importance(parsed, existing) -> int`, first match wins:
  1. valid frontmatter `importance` → that value (any type);
  2. type in `CURATED_IMPORTANCE_TYPES` → 3 if `source == "auto-capture"`, else 6;
  3. `existing` when not `None`;
  4. 5.
- `_upsert_note` stores `_derive_importance(parsed, existing_importance)`. Every write path
  (`_sync`, `index_note`, both `rebuild_index` modes) goes through it.
- Fix the `rerank_results` docstring: importance comes from the index (Haiku score for sessions,
  derived for curated notes, frontmatter override), not "frontmatter".

### Task 2: backfill (`hooks/vault_index.py` + `scripts/importance_backfill.py`)

- `backfill_importance(db_path, *, apply=False) -> dict`: for every indexed row whose type is
  not `claude-session`, parse the file and compute `_derive_importance(parsed, current)`.
  Returns `{"applied", "scanned", "changed", "by_value": {new: n}, "from_to": {"5->3": n},
  "unreadable": n}`. Dry run changes nothing.
- `apply=True`: one `BEGIN IMMEDIATE` transaction of
  `UPDATE notes SET importance=? WHERE path=? AND importance=?` (compare-and-set on the value
  read, so a concurrent `_upsert_note` is never overwritten). Reports `updated` (rowcount sum).
- No DB backup: the only rows touched are non-session rows, whose prior value is 5 on the live
  DB (measured), and `from_to` records every change, so a revert is one known UPDATE. Rows
  with an unreadable or unparseable file are skipped and counted.
- Raises `FileNotFoundError` for a missing DB (never creates one).
- Script: default dry run, `--apply`, `--db` (default: the configured index). Prints counts.
  Exit 0 ok, 1 missing DB or failure, 2 usage.

### Task 3: docs

- `CHANGELOG.md` Unreleased entry; `docs/architecture/architecture.json` (new script,
  importance in the index flow), re-render, smoke test.

## Failure modes (tests written first)

1. **Fail-open paths:** an invalid frontmatter value must not be stored or crash; it falls back
   (test per invalid shape). A missing/unreadable file in the backfill is skipped and counted,
   never set to a default (test). The backfill on a missing DB raises, the script exits 1.
2. **Exit codes:** script exits 1 on a missing DB, 2 on a bad flag (tests).
3. **Date boundaries:** none; no dates involved (checked: the change reads no timestamps).
4. **Doc counts:** CHANGELOG/PR numbers come from the live dry run and the measurement script.
5. **Broken Markdown:** CHANGELOG entry inside the Unreleased block; preflight markdownlint.
6. **Unrelated changes:** `git diff --stat` and `--diff-filter=D` before the PR; no deletions.
7. **Inputs:** the note file is the source of truth for `type`, `source`, `importance`; the DB
   value is a cache of the derivation (except sessions, where the DB holds the Haiku score).
   Tests: a note edited from auto-capture to hand-saved re-derives on the next sync; a session
   note's DB score survives a re-sync and the backfill.

**Bypass routes** (the derivation is a ranking input, not a gate): the cheapest way to raise a
note's rank is an `importance: 10` line, which is the intended override. An auto-capture note
whose `source:` line is removed by hand becomes hand-saved, which is intended too.

## Tests (`tests/test_importance.py`)

- Derivation table: auto 3, hand 6 for each curated type; session keeps DB; other types keep DB;
  frontmatter override; each invalid shape falls back.
- Through `capture_save` (the real writer) → indexed at 3. A `/compress`-shaped note via
  `index_note` → 6.
- Re-sync: auto note edited to drop `source: auto-capture` → 6.
- Backfill: dry run changes nothing and reports `by_value`; apply updates; session rows
  untouched; unreadable file skipped; CAS skip when the row changed after the read; missing DB.
- Rank: two notes equal on every other signal; the hand-saved one ranks first.
- Script exit codes 0/1/2.
- Live (PR body): auto share of the top 10 for the 10 queries, develop (46/100 at `f9abdc2`;
  52/100 before the #448 purge) vs the branch after the backfill.
