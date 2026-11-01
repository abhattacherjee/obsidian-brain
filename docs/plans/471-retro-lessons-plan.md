# Retro Lessons Rollup (PR 1 of #471) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group every retro Process Improvements item into recurring lessons, show them on a Dataview dashboard ranked for issue prioritisation, and trace accepted lessons back into the source retros.

**Architecture:** Five small stdlib modules (`lessons_corpus`, `lessons_verify`, `lessons_group`, `lessons_notes`) behind one CLI (`hooks/retro_rollup.py`). TF-IDF picks each item's 10 nearest items from other retros; Haiku confirms same-lesson pairs (cached in SQLite); star grouping forms lessons; a private state file holds lessons and tracking; lesson notes and `report --json` are rendered from it. `retro_trace.apply_traces` gains a partial mode for the write-back.

**Tech Stack:** Python 3 stdlib only, `claude -p --model haiku`, `gh` (read-only), Obsidian Dataview, pytest.

**Spec:** `docs/plans/471-retro-lessons-design.md` (approved 2026-11-01). Cites below are at `develop` `0668451`.

## Global Constraints

- Python stdlib only; numpy and scipy are not allowed in shipped code.
- Every vault write is atomic (temp file + rename) and contained in the vault (`resolve()` + `is_relative_to()`); files 0o600, dirs 0o700.
- Every `claude -p` and `gh` call has `stdin=subprocess.DEVNULL` and a timeout; `claude -p` runs with `obsidian_utils.worker_env()`.
- Tests never touch the live vault, the live index DB (`vault_index._connect` refuses it under pytest, #192) or live GitHub; `claude -p` and `gh` are mocked.
- Verifier shape is the spike's exactly: one anchor plus its 10 candidates per call, the round 2 rubric text byte-for-byte, model `haiku`. A pair is confirmed when either side confirmed it.
- Star grouping; a lesson spans ≥ 2 retro notes.
- Ranking: sessions in the last `lessons_recent_days` (default 90), then total sessions, then projects. No session in that window → dormant.
- Nothing ticks an item without an accept-file entry. Issue traces never tick.
- The public repo gets no retro item text: the fixture holds hashes, ids, labels and verdicts only.
- Item keys ignore traces: `<note stem>:<sha1 of normalised text without trace>[:16]`.
- Docs are updated in this PR: README, architecture page (re-rendered, smoke test `[main]` and `[sparse]` pass), `CLAUDE.md`, `CHANGELOG.md`.

## Review Focus

1. A retro note edited between the dry run and apply (an item added above another, so numbers shift): apply resolves item numbers by item key at apply time and never traces the wrong line. Test in Task 6.
2. Two `update` runs at once (two sessions ending together): the second waits on the state lock or exits 1 with "another rollup run holds the lock"; the state file is never half-written. Test in Task 6.
3. A lesson whose members carry different traces (two issues, or an issue and a memory): tracking is the most frequent ref, ties broken issue > path > memory, then earliest retro date. Test in Task 4.
4. Item text that breaks Markdown or YAML in a lesson note (`|`, `[[`, a leading `---`, quotes, `#` in a lesson name): the note's frontmatter still parses and the body shows the text. Test in Task 5.
5. A CRLF retro note: items parse, keys match the LF copy, partial apply keeps CRLF. Tests in Tasks 1 and 2.

## Failure modes (one line per self-check class)

- **Fail-open paths:** a Haiku timeout, non-zero exit or unparsable reply leaves the anchor unjudged (never "different"), counted in the summary, retried next run (Task 3). A `gh` failure in `refresh-tracking` keeps the previous tracking fields, adds an error entry and exits 1 (Task 6). An unreadable retro or invalid frontmatter is skipped with reason `unreadable` (Task 2).
- **Exit codes:** every `retro_rollup.py` command exits 0 on success, 1 on a runtime or partial failure (unjudged anchors, `gh` errors, lock held, write error) with the JSON or table still printed, 2 on a usage or config error. Each branch has a test (Task 6).
- **Date boundaries:** "recent" is `date >= today - recent_days` (inclusive at exactly 90 days); `sessions_after_tracking` counts retros with `date > tracking_since` (strictly after). Dates are `YYYY-MM-DD` strings from frontmatter `date`, else the note filename prefix. Tests at the boundary in Task 4.
- **Doc counts:** dashboards go from six to seven (README, `/obsidian-setup` summary); `tests/test_dashboards.py` `EXPECTED_INSIGHT_BLOCKS` rises by the new dashboard's `claude-insights` block count; architecture counts come from `wc -l` and the JSON, not memory (Tasks 7, 8).
- **Broken Markdown:** lesson notes are generated, so Task 5 tests the YAML and body escaping; README and CHANGELOG inserts follow the "insert inside the block, re-read five lines" rule (Task 8).
- **Unrelated changes:** none. `retro_trace` default behaviour is unchanged (existing tests pass untouched); `/retro`, `/standup`, `/emerge` are not edited (that is #475).
- **Inputs:** retro notes in `<vault>/<insights_folder>/*-retro-*.md` are the source of truth. Derived and private: verdict cache `~/.claude/obsidian-brain/lessons-verdicts.db`, state `~/.claude/obsidian-brain/lessons-state.json`, `<insights_folder>/lessons/retired.json`, the lesson notes. A missing cache or state rebuilds from scratch; a corrupt state file is moved aside to `.corrupt-<ts>` and rebuilt (test in Task 6); a missing `retired.json` is an empty one.
- **Bypass routes (the accept file gates ticking):** the cheapest pass is an accept entry whose `tracking_ref` no longer matches the lesson's current tracking (stale file) → skipped and reported, test in Task 6. An entry naming an unknown lesson id → skipped. Trace values are validated by `retro_trace._render` (no newline, `→` or backtick, ≤ 300 chars), so an accept file cannot inject text. An accept file is read only with `--apply`; the dry run ignores it.

**Spec deviation, reconciled in the spec in Task 8's commit:** `report --json` reads the private state file, not the lesson notes. The notes are rendered from the state, so the content is the same and the state is the single source.

---

### Task 1: Partial apply in `retro_trace`

Delegate: codex — one function's new flag plus CLI flag and tests, 2 files, one pytest command.

**Files:**
- Modify: `hooks/retro_trace.py` (`apply_traces`, `main`)
- Test: `tests/test_retro_trace.py`

**Interfaces:**
- Produces: `apply_traces(vault_path: str, note_path: str, traces: list[dict], partial: bool = False) -> dict`. With `partial=True` the "missing trace for an untraced item" check is skipped; every other check stays. The result dict gains `"untraced": <count of items still untraced after the write>` (today it is always 0). CLI: `retro_trace.py apply <vault> <note> [--partial]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_partial_apply_traces_only_given_items(tmp_path): ...
    # note with 3 untraced items; apply item 2 as memory with partial=True
    # -> item 2 gets " → memory `x`" and "- [x] ", items 1 and 3 byte-equal, result["untraced"] == 2
def test_partial_false_still_requires_every_untraced_item(tmp_path): ...
    # same input without partial -> TraceError("missing trace for an untraced item"), file bytes and mtime unchanged
def test_partial_apply_keeps_crlf(tmp_path): ...
def test_partial_apply_rejects_already_traced_item(tmp_path): ...  # TraceError, byte-equal
def test_cli_apply_partial_flag(tmp_path): ...  # main(["apply", vault, note, "--partial"]) with stdin JSON -> exit 0
def test_cli_apply_unknown_flag_is_usage_error(tmp_path): ...  # main(["apply", vault, note, "--bogus"]) -> exit 2
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_retro_trace.py -q -p no:cacheprovider`
Expected: the six new tests FAIL; every existing test passes.

- [ ] **Step 3: Implement** the `partial` parameter and the `--partial` flag (the arg-count check in `main` accepts `apply <vault> <note>` with an optional trailing `--partial`).

- [ ] **Step 4: Run to verify they pass** — same command, all PASS.

- [ ] **Step 5: Commit** — `feat(obsidian-brain): retro_trace partial apply for the lesson backfill (#471)`

### Task 2: Corpus — items, keys and TF-IDF candidates

Delegate: codex — new pure module plus tests, 2 files, one pytest command.

**Files:**
- Create: `hooks/lessons_corpus.py`
- Test: `tests/test_lessons_corpus.py`

**Interfaces:**
- Consumes: `retro_trace.parse_items`, `retro_trace.parse_trace`, `frontmatter.split_frontmatter`, `frontmatter.split_lines_lf_crlf`, `tfidf._tokenize_for_tfidf`, `tfidf._compute_tfidf_vector`.
- Produces:
  - `item_key(note_stem: str, text: str) -> str` — drops the leading `- [ ] `/`- [x] `, drops the trace (text after the last ` → ` when `parse_trace` accepts it), collapses whitespace, lowercases, returns `f"{note_stem}:{sha1(norm).hexdigest()[:16]}"`.
  - `load_items(vault_path: str, insights_folder: str) -> tuple[list[dict], list[dict]]` — items `{key, note, path, item, line, project, date, text, checked, trace}` for every Process Improvements item in `<vault>/<insights_folder>/*-retro-*.md` (direct children, contained in the vault); skipped `[{path, reason}]` with reason `unreadable` (OSError, UnicodeError or invalid frontmatter). `date` is frontmatter `date` (first 10 chars) else the filename's `YYYY-MM-DD` prefix; `project` is frontmatter `project` unquoted, else `"unknown"`.
  - `top_candidates(items: list[dict], k: int = 10, only: set[str] | None = None) -> dict[str, list[str]]` — for each item (or each key in `only`), the `k` items from other notes with the highest cosine, ties broken by key; uses an inverted index over terms.

- [ ] **Step 1: Write the failing tests**

```python
def test_item_key_ignores_trace_and_checkbox():
    assert item_key("2026-10-01-retro-ab12", "- [ ] Run X first.") == item_key("2026-10-01-retro-ab12", "- [x] Run X first. → memory `feedback_x`")
def test_item_key_keeps_prose_arrow(): ...  # "- [ ] A → B" key differs from "- [ ] A"
def test_load_items_reads_all_items_ticked_and_traced(tmp_path): ...  # 3 notes, mixed; counts and fields
def test_load_items_skips_invalid_frontmatter_with_reason(tmp_path): ...
def test_load_items_crlf_note_matches_lf_keys(tmp_path): ...
def test_load_items_ignores_subfolders_and_non_retro_files(tmp_path): ...  # lessons/ and *-insight-*.md not read
def test_top_candidates_excludes_same_note_and_is_deterministic(): ...
def test_top_candidates_only_limits_anchors(): ...
def test_top_candidates_speed_4000_items():  # synthetic 4,000 items of ~25 tokens; full run < 10 s
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_lessons_corpus.py -q -p no:cacheprovider` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement** the three functions in `hooks/lessons_corpus.py`.

- [ ] **Step 4: Run to verify they pass** — same command, all PASS.

- [ ] **Step 5: Commit** — `feat(obsidian-brain): lessons corpus loader and TF-IDF candidates (#471)`

### Task 3: Verifier and verdict cache

Delegate: codex — new module with an injected runner, SQLite through `vault_index._connect`, 2 files, one pytest command.

**Files:**
- Create: `hooks/lessons_verify.py`
- Test: `tests/test_lessons_verify.py`

**Interfaces:**
- Consumes: `vault_index._connect`, `obsidian_utils.worker_env`.
- Produces:
  - `RUBRIC: str` — the round 2 rubric, byte-equal to the text in the Task 3 brief (copied from the spike's `haiku_verify.py`); `RUBRIC_VERSION = "r2-2026-10-31"`; `MODEL = "haiku"`.
  - `build_prompt(anchor: str, candidates: list[str]) -> str` — `RUBRIC`, blank line, `ANCHOR: …`, blank line, `CANDIDATE 1: …` ….
  - `parse_verdicts(stdout: str, n: int) -> list[bool] | None` — first `{…}` JSON object; `"same"` → True; missing key → False; no object or bad JSON → None.
  - `run_haiku(prompt: str, timeouts: tuple[int, int] = (120, 240)) -> str | None` — `["claude", "-p", "--model", MODEL]`, prompt on stdin, `env=worker_env()`, retries once on timeout, None on non-zero exit, timeout twice or `FileNotFoundError`.
  - `default_cache_path() -> Path` — `~/.claude/obsidian-brain/lessons-verdicts.db` (dir 0o700, file 0o600).
  - `class VerdictCache(path)`: `judged(anchor_key) -> bool`; `record(anchor_key, cand_keys, verdicts)` (pairs stored with `a < b`; an existing `same=1` is never lowered to 0); `edges() -> set[tuple[str, str]]` (confirmed pairs, current rubric and model only); `prune(live_keys: set[str]) -> int` (deletes rows naming a dead key, returns rows deleted). Rows are keyed by `(a, b, rubric, model)`; anchors by `(anchor, rubric, model)`.
  - `verify_anchors(texts: dict[str, str], cands: dict[str, list[str]], cache: VerdictCache, runner=run_haiku, concurrency: int = 8, progress=None) -> dict` — skips judged anchors; returns `{"judged": int, "failed": int, "skipped": int}`; a None verdict leaves the anchor unjudged; `progress(done, total, failed)` is called every 100 anchors.
  - `name_lessons(lessons: list[dict], texts: dict[str, str], runner=run_haiku) -> dict[str, str]` — one call for up to 40 lessons (more → several calls); each lesson is shown as its centre plus up to 4 members; the reply is a JSON object `{"<n>": "<imperative name, ≤ 80 chars>"}`; a lesson with no usable name gets its centre's first sentence cut to 80 chars.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_verdicts_maps_same_and_rejects_garbage(): ...
def test_run_haiku_uses_worker_env_and_devnull_free_stdin(monkeypatch): ...  # fake subprocess.run captures env and input
def test_run_haiku_retries_once_on_timeout_then_none(monkeypatch): ...
def test_cache_either_side_confirm_is_sticky(tmp_path): ...  # record a->b same, then b->a different -> edge kept
def test_cache_rubric_change_hides_old_rows(tmp_path, monkeypatch): ...  # patch RUBRIC_VERSION -> edges() empty, judged() False
def test_cache_prune_drops_dead_keys(tmp_path): ...
def test_cache_file_mode_0600(tmp_path): ...
def test_verify_anchors_skips_judged_and_counts_failures(tmp_path): ...  # runner returns None for one anchor
def test_verify_anchors_resumes_after_interrupt(tmp_path): ...  # second call judges only the remainder
def test_name_lessons_falls_back_to_centre_sentence(): ...  # runner returns None
```

- [ ] **Step 2: Run to verify they fail** — `python3 -m pytest tests/test_lessons_verify.py -q -p no:cacheprovider`, FAIL.
- [ ] **Step 3: Implement** in `hooks/lessons_verify.py`; register `lessons_verify` in `tests/test_security.py` only if it reads stdin (it does not).
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** — `feat(obsidian-brain): Haiku same-lesson verifier with a SQLite verdict cache (#471)`

### Task 4: Star lessons, ids, tracking and buckets

Delegate: codex — pure functions over plain dicts, 2 files, one pytest command.

**Files:**
- Create: `hooks/lessons_group.py`
- Test: `tests/test_lessons_group.py`

**Interfaces:**
- Produces:
  - `star_lessons(keys: list[str], edges: set[tuple[str, str]], note_of: dict[str, str]) -> list[dict]` — loop: among unassigned keys pick the one with most confirmed edges to other unassigned keys (ties: smallest key); it and its unassigned neighbours form `{"centre": k, "members": sorted(...)}`; stop when no unassigned key has an edge; keep a lesson only if its members span ≥ 2 notes.
  - `assign_ids(lessons: list[dict], old: dict[str, list[str]], retired: dict) -> tuple[list[dict], dict]` — a new lesson takes the id of the old lesson with the largest overlap if overlap ≥ half of the smaller of the two; else `"L" + sha1(centre).hexdigest()[:8]`. An old id whose members went to a lesson that took another id → `retired[old] = {"merged_into": new}`. A new lesson whose largest-overlap old lesson's id went elsewhere → `{"split_from": old}` stored on the lesson. Returns lessons with `"id"` and the updated retired map.
  - `lesson_tracking(members: list[str], items: dict[str, dict]) -> dict | None` — counts member traces of kind issue/memory/path (`ref` = issue ref, memory stem, path); most frequent wins; ties issue > path > memory, then earliest date; returns `{"kind", "ref", "since"}` where `since` is the earliest date of a retro carrying that trace (Task 6 replaces it with the issue's `createdAt` for issues). `skipped` traces count for nothing.
  - `classify(lesson: dict, items: dict, tracking: dict | None, info: dict | None, today: str, recent_days: int) -> dict` — returns `{"sessions", "recent_sessions", "projects", "first_seen", "last_seen", "sessions_after_tracking", "bucket", "dormant"}`. Buckets: `raise` (issue open, after > 0), `file` (no tracking, or `info["exists"] is False` → also sets `tracking_gone`), `didnt_hold` (issue closed, sessions after `closedAt` > 0), `rule_not_followed` (memory or path, after > 0), `quiet` (else). `sessions_after_tracking` is None when `since` is None.
  - `rank_key(c: dict) -> tuple` — `(-recent_sessions, -sessions, -projects, last_seen desc, id)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_star_bridge_cannot_join_two_rules():
    # edges a-b, a-c, b-c (rule 1) and c-d, d-e, d-f (rule 2): two lessons, not one
def test_star_drops_single_note_lessons(): ...
def test_star_is_deterministic_on_ties(): ...
def test_assign_ids_keeps_id_at_half_overlap_and_not_below(): ...
def test_assign_ids_records_merge_and_split(): ...
def test_tracking_most_frequent_then_issue_path_memory_tiebreak(): ...  # Review Focus 3
def test_tracking_ignores_skipped_traces(): ...
def test_recent_window_is_inclusive_at_exactly_90_days(): ...  # date == today-90 counts
def test_sessions_after_tracking_is_strictly_after(): ...  # same-day retro not counted
def test_buckets_all_five_and_tracking_gone(): ...
def test_rank_key_recent_before_total(): ...
```

- [ ] **Step 2–4:** run `python3 -m pytest tests/test_lessons_group.py -q -p no:cacheprovider` (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** — `feat(obsidian-brain): star lessons, stable ids, tracking and buckets (#471)`

### Task 5: Lesson notes and the report

Delegate: codex — rendering and write-if-changed over plain dicts, 2 files, one pytest command.

**Files:**
- Create: `hooks/lessons_notes.py`
- Test: `tests/test_lessons_notes.py`

**Interfaces:**
- Consumes: `obsidian_utils.write_vault_note`, `frontmatter.split_frontmatter`.
- Produces:
  - `note_filename(lesson: dict) -> str` — `f"{slug(name)[:50]}-{id}.md"`, slug `[a-z0-9-]`.
  - `render_note(lesson: dict, c: dict, items: dict[str, dict], snoozed_until: str | None) -> str` — frontmatter fields exactly: `type: claude-lesson`, `lesson_id`, `name`, `sessions`, `recent_sessions`, `projects` (flow list), `first_seen`, `last_seen`, `tracking` (`issue|memory|rule|none|gone`; path → `rule`), `tracking_ref`, `tracking_since`, `issue_state`, `issue_milestone`, `issue_priority`, `sessions_after_tracking`, `bucket`, `dormant`, `snoozed_until`, `tags: [claude/lesson, claude/project/<p>...]`. Every string value is JSON-quoted. Body: `# <name>`, then one bullet per member sorted by date then key: the item text with the checkbox and trace removed, newlines collapsed, then `[[<note stem>]] · <project>`. No timestamps, so an unchanged lesson renders byte-equal.
  - `read_snooze(path: Path) -> str | None`.
  - `write_if_changed(vault_path: str, folder: str, filename: str, content: str) -> bool` — True when written; compares bytes first.
  - `report(state: dict) -> dict` — `{"generated": <date>, "totals": {...}, "lessons": [ {id, name, bucket, dormant, sessions, recent_sessions, projects, first_seen, last_seen, tracking, tracking_ref, issue_state, issue_milestone, issue_priority, sessions_after_tracking, members: [{note, item_key}]} ]}` sorted by bucket order raise, file, didnt_hold, rule_not_followed, quiet, then `rank_key`.

- [ ] **Step 1: Write the failing tests**

```python
def test_render_frontmatter_parses_with_hostile_text(): ...  # name with '#', '"', ': '; item with '|', '[[x]]', leading '---' (Review Focus 4)
def test_render_is_byte_stable(): ...
def test_write_if_changed_skips_identical(tmp_path): ...  # second call returns False, mtime unchanged
def test_snooze_survives_rerender(tmp_path): ...
def test_report_orders_buckets_then_rank(): ...
def test_report_has_no_item_text(): ...  # members carry note + item_key only
```

- [ ] **Step 2–4:** `python3 -m pytest tests/test_lessons_notes.py -q -p no:cacheprovider` FAIL → implement → PASS.
- [ ] **Step 5: Commit** — `feat(obsidian-brain): lesson notes and report --json rendering (#471)`

### Task 6: `retro_rollup.py` CLI

Delegate: claude — wires five modules, a state lock, `gh` and the trace write-back; the contract every later PR and two other repos depend on.

**Files:**
- Create: `hooks/retro_rollup.py`
- Test: `tests/test_retro_rollup.py`, `tests/test_security.py` (register the stdin-free entry point if the security test enumerates CLIs)

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces (all commands take `--vault PATH`, `--insights-folder NAME` and `--state-dir DIR`, defaulting to config and `~/.claude/obsidian-brain/`; exit 0/1/2 as in Failure modes):
  - `update [--note PATH] [--concurrency N]` — load items; candidates for all items (or the note's items); `verify_anchors`; re-form lessons (`star_lessons` + `assign_ids`); names only for new lessons (`name_lessons`; an existing id keeps its name, so file names stay put); `classify`; write changed notes and `retired.json`; save state. A snooze carries over: the note of a lesson that took an id keeps its `snoozed_until`, and a `split_from` lesson copies its parent's. A retired lesson's note is deleted only after its snooze has been copied. Prints one summary line: `lessons: N (+new, ~changed), items in lessons: M, unjudged: U, notes written: W`.
  - `rebuild` — same without Haiku calls except for unjudged anchors; `VerdictCache.prune(live_keys)`.
  - `refresh-tracking` — for each issue ref in lesson tracking: `gh issue view N --repo O/R --json state,stateReason,milestone,labels,createdAt,closedAt` (timeout 10 s, 8 in parallel); `issue_priority` = first label matching `^P[0-9]`; `since` = `createdAt[:10]`. For memory refs: exists among `memory_sources("claude-code")` stems; for path refs: `Path(expanduser).exists()`. Saves tracking into state, re-renders notes. A `gh` error keeps the old fields, prints the error, exits 1.
  - `report [--json]` — prints `lessons_notes.report(state)`; table form groups by bucket.
  - `backfill [--apply --accept FILE]` — dry run: the bucket table plus, per lesson with tracking, the trace each untraced member would get; writes nothing. Apply: reads `[{"lesson_id", "tracking_ref"}]`; skips and reports entries whose lesson is unknown or whose current `tracking_ref` differs; for the rest, groups untraced members by note, re-reads each note, maps item keys to current item numbers, calls `apply_traces(..., partial=True)`. Prints the untraced open-item count and each remaining item with a reason: `one-off`, `lesson-untracked`, `not-accepted`, `unjudged`, `unreadable`.
  - State lock: `fcntl.flock` on `<state-dir>/lessons.lock`, non-blocking; held → exit 1 with `another rollup run holds the lock`. State is written atomically; a state file that fails to parse is renamed to `lessons-state.json.corrupt-<unix ts>` and rebuilt.

- [ ] **Step 1: Write the failing tests** (temp vault, temp state dir, fake runner via `monkeypatch.setattr(lessons_verify, "run_haiku", fake)`, fake `gh` via `monkeypatch.setattr(retro_rollup, "_run_gh", fake)`)

```python
def test_update_groups_and_writes_notes_then_rerun_writes_nothing(tmp_path): ...
def test_update_haiku_failure_is_unjudged_and_exit_1(tmp_path): ...
def test_update_note_only_judges_that_notes_items(tmp_path): ...
def test_lock_held_exits_1_and_leaves_state(tmp_path): ...  # Review Focus 2
def test_corrupt_state_moved_aside_and_rebuilt(tmp_path): ...
def test_refresh_tracking_reads_milestone_priority_and_dates(tmp_path): ...
def test_refresh_tracking_gh_error_keeps_old_fields_exit_1(tmp_path): ...
def test_refresh_tracking_missing_memory_sets_gone(tmp_path): ...
def test_backfill_dry_run_writes_nothing(tmp_path): ...  # every retro byte-equal, mtimes unchanged
def test_backfill_apply_traces_accepted_and_rerun_changes_nothing(tmp_path): ...
def test_backfill_apply_skips_stale_tracking_ref(tmp_path): ...  # bypass route
def test_backfill_apply_after_note_edit_resolves_by_key(tmp_path): ...  # Review Focus 1
def test_backfill_reports_every_untraced_item_with_reason(tmp_path): ...
def test_report_json_contract_fields(tmp_path): ...
def test_snooze_carries_through_merge_and_split(tmp_path): ...
def test_existing_lesson_keeps_name_and_filename(tmp_path): ...
def test_usage_error_exit_2(): ...
def test_no_live_paths_touched(tmp_path, monkeypatch): ...  # HOME=tmp; asserts nothing written outside tmp
```

- [ ] **Step 2–4:** `python3 -m pytest tests/test_retro_rollup.py -q -p no:cacheprovider` FAIL → implement → PASS.
- [ ] **Step 5: Live check on a copy (run now, not at the end).** Seed a cache from the spike verdicts with a scratch script (not committed; the spike used `RUBRIC_VERSION`'s exact text), then on the scratchpad vault copy: `python3 hooks/retro_rollup.py update --vault <copy> --insights-folder claude-insights --state-dir <scratch>/state` (expect about 538 lessons, largest ≤ 25, unjudged = items added since the spike), `backfill` (dry run; retro copy byte-equal before and after, checked with `shasum`), and `backfill --apply --accept <file with 3 lessons>` then the same again (second run: 0 notes changed).
- [ ] **Step 6: Commit** — `feat(obsidian-brain): retro_rollup CLI — update, rebuild, refresh-tracking, report, backfill (#471)`

### Task 7: Index exclusion, dashboard, setup and config

Delegate: claude — touches the indexer's type set, two skills' prose and a pinned dashboard test; spans 8 files.

**Files:**
- Modify: `hooks/vault_index.py:756` (`_UNINDEXED_TYPES` gains `"claude-lesson"`), `tests/test_vault_index_unindexed_types.py` (expected set)
- Modify: `hooks/obsidian_utils.py` `_DEFAULTS` (add `lessons_enabled: True`, `lessons_recent_days: 90`, `lessons_concurrency: 8`, `project_repos: {}`)
- Create: `dashboards/retro-lessons.md`; Modify: `tests/test_dashboards.py` (`EXPECTED_INSIGHT_BLOCKS`)
- Modify: `skills/obsidian-setup/SKILL.md` (Step 5 creates `<insights_folder>/lessons/`; Step 6 embeds `retro-lessons.md` byte-equal, write-if-missing; config keys in fresh mode and added-if-missing in upgrade; summary lists seven dashboards)
- Modify: `skills/vault-config/SKILL.md` (the four keys: two booleans/ints, `project_repos` as a map with add/edit/remove)
- Modify: `skills/vault-ask/SKILL.md` Step 5 (drop rows whose `type` is `claude-lesson`, beside the `provisional` drop)
- Test: `tests/test_lessons_setup.py`

**Interfaces:**
- Consumes: the lesson note frontmatter fields from Task 5.
- Produces: the dashboard: five DQL tables over `"<insights_folder>/lessons"` (`raise`, `file`, `didnt_hold`, `rule_not_followed`, then a folded `quiet`), each `WHERE type = "claude-lesson" AND bucket = "<b>" AND !dormant AND (!snoozed_until OR date(snoozed_until) < date(today))` plus the `DQL_FILTER` provisional clause the dashboard test requires, sorted `recent_sessions DESC, sessions DESC`, `LIMIT 25`; a sixth "Dormant" table. Columns as in the approved mockup.

- [ ] **Step 1: Write the failing tests**

```python
def test_claude_lesson_is_unindexed(tmp_path): ...  # a lesson note in an indexed folder never becomes a row, via ensure_index and index_note
def test_dashboard_embedded_copy_matches_repo(): ...  # covered by test_dashboards once the file exists; assert retro-lessons.md is in FILES
def test_setup_creates_lessons_folder_and_lists_seven_dashboards(): ...  # text assertions on SKILL.md
def test_defaults_include_lesson_keys(): ...
def test_vault_ask_drops_claude_lesson_rows(): ...  # text assertion on Step 5
```

- [ ] **Step 2–4:** `python3 -m pytest tests/test_lessons_setup.py tests/test_dashboards.py tests/test_vault_index_unindexed_types.py tests/test_type_scores.py -q -p no:cacheprovider` FAIL → implement → PASS.
- [ ] **Step 5: Commit** — `feat(obsidian-brain): retro lessons dashboard, setup, config keys, index exclusion (#471)`

### Task 8: Spike fixture, docs and architecture

Delegate: claude — reads the session scratchpad (outside the worktree) to export the fixture, and edits README, `CLAUDE.md` and the architecture page.

**Files:**
- Create: `tests/fixtures/471-eval/` — `README.md` (rounds, bars, scores, judges, models), `round1/{blind_ids.json,key.json,labels_opus.json,labels_astra.json,labels_sol.json,haiku.json}`, `round2/` (same), `round3/{e1_key.json,e1_labels_opus.json,e1_labels_astra.json,graph_verdicts.jsonl,lessons_star.json}`. Item text is replaced by item keys; no `text` field anywhere.
- Create: `tests/test_lessons_fixture.py`
- Modify: `README.md`, `CLAUDE.md`, `CHANGELOG.md`, `docs/architecture/architecture.json` (+ re-rendered `architecture.html`), `docs/plans/471-retro-lessons-design.md` (the `report --json` source deviation)

**Interfaces:**
- Consumes: `lessons_group.star_lessons`, `lessons_verify` row shape.

- [ ] **Step 1: Write the failing tests**

```python
def test_fixture_has_no_item_text(): ...  # walk every JSON/JSONL; no key named "text"; no value longer than 64 chars except README
def test_star_replay_reproduces_frozen_lessons(): ...  # star_lessons over graph_verdicts == lessons_star.json
def test_round2_verified_keep_rate_meets_bar(): ...  # >= 0.70 for both judges
def test_e1_star_score_not_below_spike(): ...  # mean keep >= 0.74 on the frozen labels
```

- [ ] **Step 2:** export the fixture from the scratchpad with a one-off script (not committed); run the tests (PASS).
- [ ] **Step 3: Docs.** README: Skills (none new in this PR), "Dataview Dashboards" (seven; Retro Lessons described), "Vault Structure" (`claude-insights/lessons/`), "Configuration" (four keys), new "Retro lessons" section after "Auto-Capture" (what it does; the weekly prompts arrive with #474/#475; the one-time backfill is about 3,900 Haiku calls and about 65 minutes; nothing ticks without an accept). `CLAUDE.md`: Key files gains `hooks/retro_rollup.py` and the four `lessons_*` modules; tag list gains `claude/lesson`. CHANGELOG `[Unreleased]` entry. Architecture JSON: components for the CLI and four modules, datastores (verdict cache, state file, lesson notes, `retired.json`), flows (`update`, `backfill --apply`), env none; `lastUpdated` 2026-11-01; render and smoke test.
- [ ] **Step 4: Verify docs**

Run: `~/.claude/skills/architecture-page/scripts/smoke-test.sh --json docs/architecture/architecture.json` — Expected: `[main]` and `[sparse]` pass.
Run: `./scripts/commit-preflight.sh` — Expected: pass, coverage ≥ 90%.

- [ ] **Step 5: Commit** — `docs(obsidian-brain): retro lessons fixture, README, architecture, CHANGELOG (#471)`
