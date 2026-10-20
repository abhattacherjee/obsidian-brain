# Plan: stop auto-capture updates snowballing onto hub notes (#427)

Issue: #427. Base: `develop` at `ba847d7`. Branch: `feature/427-capture-update-snowball`.

## Goal

An auto-capture candidate becomes an `## Update` only when it really refines the
same fact. Existing updates that fail the new rules move to their own notes.

## Evidence (live, 2026-10-20)

- 614 updates; 439 went into a note the same session had auto-created.
- Replay of 402 sections on the 13 hubs, cosine against each note's text before
  its first auto-capture update: median 0.22, p90 0.39; 77 below the current
  0.12 floor. Sampled by band: ≥0.35 refines the same fact, <0.25 is a
  separate topic, 0.25–0.35 is mixed. Hence the default 0.35.

## Tasks

### T1 — `find_match` skips notes the same session auto-created

- `capture_state.notes_created_by(session_id) -> set[str]`: vault-relative
  `note_path` of every merged ledger row with `kind == "new"` and that
  `session_id` (any status).
- `capture_save.find_match(query, config, session_id=None)`: drop results whose
  vault-relative path is in that set, **before** the sort, so the next result
  can still match. `save_candidate` passes its `session_id`.
- Index paths are absolute and ledger paths vault-relative: normalise both with
  `resolve()` + `relative_to(vault)` before comparing.

### T2 — score the top match against its original text, with a higher floor

- Move `_section_end` from `capture_reject.py` into `capture_save.py` (reject
  imports it from there). Add `capture_save.strip_auto_updates(text) -> str`:
  removes every section whose heading matches
  `^## Update \([^)]*\) — auto-capture [0-9a-f]{12}$`, through its
  `<!-- /auto-capture <id> -->` marker, or for an older update up to the next
  `## ` heading outside a fence. User-written `## Update` sections stay.
- New config key `auto_capture_update_min_cosine`, default `0.35`, read with
  the other `auto_capture_*` defaults. A value that is not a number in
  `[0, 1]` falls back to the default.
- In `find_match`, after filtering and sorting, when the top result is curated:
  read that note, strip frontmatter and auto-capture sections, compute
  `vault_index.compute_query_vector(db, title + "\n" + text)`, put that vector
  on the top result, and call `is_high_confidence_match(..., min_cosine=t,
  min_cosine_rescue=max(MIN_COSINE_RESCUE, t))`.
- Fail toward a new note, never toward an update: an empty query vector, an
  unreadable note, or an empty original-text vector returns `None`.

### T3 — `capture_cli.py split-updates [--apply] [--json]`

New module `hooks/capture_split.py`, called from a new `split-updates`
subcommand.

- Candidates: merged ledger rows with `kind == "updated"` and
  `status == "active"`. A row moves when rule T1 or rule T2 would have refused
  it: its note was auto-created by the same session, or cosine of
  `title + "\n" + body[:300]` against the note's original text is below the
  threshold. Reason recorded per row (`same-session` / `low-cosine`).
- Dry run (default): print one line per note (moves, by reason) and totals;
  change nothing.
- `--apply`, per source note, under `note_writer._acquire_lock`:
  1. Back up the note once to `capture_dir()/split-backup-<UTC stamp>/` (0o700
     dir, 0o600 file) before the first change.
  2. For each moving section: write a new note in the insights folder through
     `note_writer.run_write`, with `_frontmatter` (type from the row,
     `source_session` from the row, `date` from the update heading), body
     `# <title>\n\n<section body>` without the heading, bold title line and end
     marker. Then `ledger_update(id, kind="new", note_path=<new>,
     update_heading=None, split_from=<old note>)`.
  3. Rewrite the source note once with all moved sections removed, via
     `note_writer._atomic_rewrite` with the stat re-check. CRLF handled as
     Reject does.
- Order means a crash leaves a duplicate, never a loss. A re-run repairs it:
  rows with `split_from` whose heading is still in the source note get that
  section removed. A second `--apply` on a clean state changes nothing.
- Exit 0 on success (including nothing to do), 1 if any note failed (report
  each), 2 on bad config.

### T4 — docs

- `CHANGELOG.md` `[Unreleased]`: Fixed entry for #427.
- `README.md`: `auto_capture_update_min_cosine` next to the other
  `auto_capture_*` keys; `split-updates` where `capture_cli.py` is described.
- `docs/architecture/architecture.json`: `capture_split` component, the
  config key, `lastUpdated`; re-render and smoke-test per CLAUDE.md.

Commits: T1+T2 in one commit, T3+T4 in a second (the preflight runs the full
suite, 11–16 min each).

## Failure modes (tests written first)

1. **Fail-open paths:** empty query vector bypasses the cosine gate today
   (`query_vec or None`) → test: an all-stopword candidate against a matching
   note saves as new. Unreadable top note → new. Empty original text (a note
   that is only auto updates) → new. Split: a note locked by another writer is
   reported failed, exit 1, nothing half-written.
2. **Exit codes:** `split-updates --apply` exits 1 when one note fails; 0 on a
   no-op run. Test both.
3. **Date boundaries:** the split note's `date` comes from the update heading,
   not today → test with a heading dated a different year.
4. **Doc counts:** numbers in CHANGELOG / PR body come from the diff and the
   live dry run, not from this plan.
5. **Broken Markdown:** CHANGELOG entry inside `[Unreleased]`; README table rows
   inside their table. Re-read after editing.
6. **Unrelated changes:** `_section_end` moves file; no other deletions.
7. **Inputs:** the ledger (`capture_dir()/captures.jsonl`, source of truth for
   who created what), the note files on disk (source of truth for text — not
   the index body, which can lag), the index (`term_df` for weights only).
   Test: a ledger row whose note path no longer exists is skipped and reported,
   not a crash.

**Bypass routes (this is a gate):** the cheapest input that still updates a
hub is a candidate copied from the hub's grown text. T2 blocks it because the
grown text is stripped. Test: a candidate quoting an existing auto-capture
section of the note, below threshold against the original, saves as new. A
user-written `## Update` without the auto-capture suffix still counts as
original text — test that it is kept.
