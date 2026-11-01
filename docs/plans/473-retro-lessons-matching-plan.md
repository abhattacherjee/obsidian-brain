# Retro lessons matching (#473) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each untracked retro lesson the issue, memory entry or rule that already covers it, so buckets and `refresh-tracking` use that tracking, while memory and rule matches are traced only through the accept file.

**Architecture:** A new pure module `hooks/lessons_match.py` builds candidate documents (issues, memory, rules), ranks them with TF-IDF and gates the single top candidate with one Haiku call. A new `retro_rollup.py match` subcommand lists issues read-only, runs the gate for untracked lessons, saves each result on the lesson record, and re-renders notes. Regroup uses a match as tracking when a lesson has no member-trace or filed tracking.

**Tech Stack:** Python 3.9+ stdlib only. `gh` (read-only `issue list`), `claude -p --model haiku` via `lessons_verify.run_haiku`. pytest.

**Spec:** `docs/plans/471-retro-lessons-design.md` ("Matching to tracking", "Buckets", "Trace write-back"), with the round 4 spike ruling on #473 (below) replacing its "top 5 per source, strongest in order issue, rule, memory".

## Global Constraints

- Python stdlib only. Every `gh` and `claude -p` call has stdin closed and a timeout. Tests mock both (`retro_rollup._run_gh`, `lessons_verify.run_haiku` or an injected runner).
- No test or measurement touches the live vault, the live state dir `~/.claude/obsidian-brain/` or GitHub (#473 C.4).
- `match` never writes to GitHub and never writes a trace. Traces come only from `backfill --apply --accept FILE` (#473 C.3).
- Atomic writes for state and notes (existing `save_state`, `lessons_notes.write_if_changed`). State 0o600, dirs 0o700.
- Public repo: the fixture holds ids, sources and labels only, never lesson, issue, memory or rule text.
- Docs in the same PR: README, `docs/architecture/architecture.json` (+ re-rendered HTML, smoke test `[main]` and `[sparse]`), CHANGELOG `[Unreleased]`, `CLAUDE.md` key files, and the design spec's matching section.

## The spike ruling (what ships)

Round 4 on #473 passed: each untracked lesson's single highest-cosine candidate across issues, memory and rules, accepted or rejected by Haiku, scored 22/22 under Opus and `gpt-6-astra` against 26/30 for TF-IDF top 1 alone (κ 0.97). Round 3 (top 5 per source, all confirmed matches) missed. So:
- **Ranking:** per source, cosine between the lesson text and each document, with idf over that source's documents (sublinear tf: `(1 + ln tf) * (1 + ln((N + 1) / (df + 1)))`, vectors L2-normalised, tokens from `tfidf._tokenize_for_tfidf`). The proposal is the highest score across the three sources; ties break by ref. A score of 0 is no proposal.
- **Gate:** one Haiku call with the prompt below. Accept = the lesson's match; reject = no match.
- **Lesson text for ranking:** the lesson name, then every member item's text, space-joined. **Anchor for the gate:** the name, then up to 5 member texts as `- <text>` lines.

Gate prompt (verbatim; `<<A>>` anchor, `<<S>>` source word, `<<C>>` candidate text cut to 800 chars):

```
You are matching a lesson from a coding-session retrospective against ONE candidate document (a GitHub issue, a memory note or a written rule). Decide:

"covers": true only if someone following the candidate would NOT need the lesson written down again. The candidate states the same rule, even in different words or for a different project.
"covers": false if it is a different rule, even on the same topic (for example both about testing, but different rules), or only mentions the topic in passing.

Examples:
1. Lesson: "Write ~/ro.sh at the first router refusal in a session." Candidate (memory): "Router override: on the first refusal, write the per-session ~/ro-<id4>.sh script once and tell the user." -> {"covers": true} (same rule).
2. Lesson: "Filing a follow-up issue should include the failing command output." Candidate (issue, open): "Bug issues must include the exact failing command and its output in the body." -> {"covers": true} (the issue already requires it).
3. Lesson: "Verify every file:line citation in a fix spec before dispatching." Candidate (rule): "Mutation-test every new guard: delete it and expect red." -> {"covers": false} (both about verification, different rules).

LESSON:
<<A>>

CANDIDATE:
(<<S>>) <<C>>

Reply with ONLY a JSON object, no prose and no code fences:
{"covers": true|false}
```

## Data contract changes

- Lesson record (`state["lessons"][lid]`) gains `"match"`: `{"kind": "issue"|"memory"|"path", "ref": str, "since": "YYYY-MM-DD"|None, "candidate": <short_hash of "<source>|<ref>|<candidate text>">}` for an accepted match, or `{"none": True, "candidate": <hash, or None when nothing was proposed>}` for a rejection or no proposal. Absent = never checked.
- **A match lives with the lesson id** (ruling 2026-11-01, so a lesson that recurs keeps its match). It is re-gated only when the lesson's current TF-IDF top candidate hash differs from the stored `candidate` (TF-IDF is cheap; Haiku runs only on a change). A new lesson id (a merge or split that mints one) gets a fresh check; a survivor that keeps its id keeps its match. The candidate hash is deterministic: lesson text built from members in sorted key order, item text with the trace stripped (`lessons_corpus._item_text`).
- **Where the check runs:** `refresh-tracking` runs it for every lesson needing one (no `match`, or a changed candidate) before re-reading tracking, so `/lessons`, weekly `/standup` and plan-week, which already run `refresh-tracking` first, keep matches current with no new command. `match` is the explicit entry point; `match --all` re-gates every eligible lesson. Lessons with member-trace tracking, a filed issue, or a pending filing record are not matched (the reconcile pass decides a pending one).
- Tracking dicts gain `"source": "trace"|"filed"|"match"`. Precedence in regroup: member-trace tracking, then the filed issue (existing), then the lesson's own `match` (keyed by lesson id).
- Kind mapping: issue → `issue` (ref `owner/repo#N`), memory → `memory` (ref = file stem), rule → `path` (ref = the file path with `$HOME` written as `~`, no line number; the #470 path trace form).
- `since`: issue `createdAt[:10]` (and `refresh-tracking` keeps it current); memory = file mtime date; rule = `git log -1 --format=%cs -S <first 60 chars of the paragraph> -- <file>` when `git -C <dir> rev-parse --show-toplevel` succeeds (stdin closed, 10 s timeout; `~/.claude` is not a git repo, so this helps project `CLAUDE.md` files only), else the file mtime date. A non-repo path never raises.
- `report --json` and lesson note frontmatter gain `tracking_source` (`trace`, `filed`, `match` or `none`). The text table appends ` (match)` to a matched ref.
- `_untraced_report` reason for a member of a matched, not accepted lesson: `lesson-matched` (was `not-accepted`).

## Failure modes

- **Fail-open:** a failed issue listing for a repo, a memory scan with errors, or a Haiku non-answer must leave the affected lessons unchecked (no `match` written), never `{"none": True}`; `match` exits 1. Tests: `test_failed_issue_listing_leaves_lessons_unchecked_and_exits_1`, `test_memory_scan_error_leaves_lessons_unchecked`, `test_haiku_non_answer_is_not_cached_as_no_match`.
- **Exit codes:** `match` exits 0 on success, 1 on any per-lesson or per-source error (other lessons still saved), 2 on bad arguments (`--concurrency` outside 1-16). Test each: `test_match_exit_codes`.
- **Date boundaries:** `since` for issue/memory/rule is a `YYYY-MM-DD` string or None; a None `since` gives `sessions_after_tracking` None and bucket `quiet`, not `rule_not_followed`. Test: `test_match_with_no_since_is_quiet_not_rule_not_followed`.
- **Doc counts:** README and CHANGELOG numbers (lessons matched on the copy, accept rate) come from the live check run at the final head, never from the spike.
- **Broken Markdown:** rule paragraphs are read outside code fences only; a `SKILL.md` with an unclosed fence yields no paragraphs after the fence, never an exception. Test: `test_unclosed_fence_stops_paragraphs`.
- **Unrelated changes:** none; `backfill` filing code is untouched except the untraced reason string and planned-filings exclusion.
- **Inputs:** reads the retro notes (vault copy in tests), `lessons-state.json`, `gh issue list` per repo (live in real runs, faked in tests), memory files from `memory_sources`, `~/.claude/CLAUDE.md`, `~/dev/claude_workspace/<project>/CLAUDE.md`, `~/.claude/skills/*/SKILL.md` and the highest numeric version dir per plugin under `~/.claude/plugins/cache/*/*/skills/*/SKILL.md`. Source of truth for each match is re-checked by `refresh-tracking` (issue state; memory stem and rule path existence). A missing or unreadable file is skipped with an error line, never a crash. Tests use a tmp HOME. Test: `test_unreadable_rule_file_is_skipped_with_error`.
- **Bypass routes for C.3 (no trace without accept):** the cheapest route would be `backfill --apply` with an accept entry naming a lesson whose tracking just changed to a match. The existing `tracking_ref` equality check covers it; add `test_backfill_traces_a_match_only_with_a_matching_accept_entry` and `test_match_and_update_never_write_a_trace` (checksum of every retro note before and after `match`, `update`, `rebuild`, `refresh-tracking`).
- **Stale match:** a lesson whose TF-IDF top candidate changed is re-gated by `refresh-tracking` or `match`; until then its stored match stands. A lesson that only gained a member, with the same top candidate, keeps its match and makes no Haiku call. Tests: `test_recurring_lesson_keeps_its_match_without_a_haiku_call`, `test_changed_top_candidate_is_regated`, `test_refresh_tracking_gates_unchecked_lessons`.
- **Outward writes:** none. `match` only reads GitHub, so no retry state table is needed.

---

### Task 1: `hooks/lessons_match.py` (pure module)

Delegate: codex — self-contained new module plus its tests, 2 files, `Check:` is one pytest command; no guarded paths.

**Files:**
- Create: `hooks/lessons_match.py`
- Test: `tests/test_lessons_match.py`

**Interfaces (Produces):**
- `RUBRIC_VERSION = "m4-2026-11-01"`, `GATE_PROMPT` (the verbatim prompt above).
- `lesson_text(name: str, member_texts: list[str]) -> str` and `anchor_text(name: str, member_texts: list[str]) -> str` (name + up to 5 `- <text>` lines).
- `memory_docs(paths: list[str], errors: list) -> list[dict]`: skips `MEMORY.md`; doc `{"source": "memory", "ref": <stem>, "text": <name + description + body, cut to 2000>, "path": str}`; an unreadable file appends `{"path", "error"}`.
- `rule_bullets(path: Path) -> list[tuple[int, str]]` (top-level `- ` lines joined with indented continuation lines) and `rule_paragraphs(path: Path) -> list[tuple[int, str]]` (blank-line separated, ≥ 80 chars, outside code fences).
- `rule_docs(home: Path, projects: list[str], workspace: Path, errors: list) -> list[dict]`: `~/.claude/CLAUDE.md` bullets, `<workspace>/<project>/CLAUDE.md` bullets, `SKILL.md` paragraphs (skills dir and highest numeric version per plugin cache dir); doc `{"source": "rule", "ref": <path with home as ~>, "line": int, "text": cut to 2000}`.
- `issue_docs(repo: str, issues: list[dict]) -> list[dict]`: doc `{"source": "issue", "ref": "owner/repo#N", "text": title + "\n" + body cut to 2000, "created": "YYYY-MM-DD"|None, "state": str}`; skips entries without an int `number`.
- `best_candidate(query: str, sources: dict[str, list[dict]]) -> tuple[dict, float] | None`: ranking as in "The spike ruling".
- `gate(anchor: str, candidate: dict, runner) -> bool | None`: builds the prompt, calls `runner(prompt)` up to 2 times until `parse_gate` answers; None on no answer.
- `parse_gate(stdout: str | None) -> bool | None`: first `{...}` object, `covers` must be a bool.
- `short_hash(text: str) -> str` (sha1 hex, first 12).

- [ ] **Step 1:** Write `tests/test_lessons_match.py`: `test_lesson_text_and_anchor_shapes`, `test_memory_docs_skip_index_and_report_unreadable`, `test_rule_bullets_join_continuations`, `test_rule_paragraphs_skip_fences_and_short`, `test_unclosed_fence_stops_paragraphs`, `test_rule_docs_pick_highest_plugin_version` (dirs `1.9.0` and `1.10.0`: only `1.10.0` read), `test_issue_docs_skip_bad_numbers`, `test_best_candidate_picks_highest_across_sources_and_breaks_ties_by_ref`, `test_best_candidate_none_when_all_scores_zero`, `test_parse_gate_accepts_only_bool_covers` (inputs: `{"covers": true}`, prose around it, `{"covers": "yes"}` → None, `None` → None, `[]` → None), `test_gate_retries_once_then_none`, `test_gate_prompt_holds_the_examples_and_reply_shape` (the three example lines and the `{"covers": true|false}` tail are in `GATE_PROMPT`).
- [ ] **Step 2:** Run `python3 -m pytest tests/test_lessons_match.py -q -p no:cacheprovider`; expect failures (module missing).
- [ ] **Step 3:** Implement `hooks/lessons_match.py` to the interfaces above.
- [ ] **Step 4:** Run the same command; expect all pass.

Check: `python3 -m pytest tests/test_lessons_match.py -q -p no:cacheprovider`

### Task 2: `retro_rollup.py match`, tracking precedence, report and notes

Delegate: claude — cross-cutting change to the state contract, regroup precedence, refresh-tracking, report, notes and backfill reasons across 3 modules.

**Files:**
- Modify: `hooks/retro_rollup.py`, `hooks/lessons_notes.py`
- Test: `tests/test_retro_rollup.py`, `tests/test_lessons_notes.py`

**Interfaces (Consumes):** Task 1. **(Produces):** `match` subcommand; `rec["match"]`; tracking `source`; `tracking_source` in report and notes.

- [ ] **Step 1: tests first.** In `tests/test_retro_rollup.py` (fake `_run_gh` for `issue list`, fake runner for Haiku, tmp HOME with a fake memory dir, `~/.claude/CLAUDE.md` and one `SKILL.md`):
  - `test_match_accepts_top_candidate_and_lesson_leaves_file_bucket` (#473 C.2)
  - `test_match_rejected_lesson_stays_in_file_bucket_and_is_not_rechecked`
  - `test_recurring_lesson_keeps_its_match_without_a_haiku_call`
  - `test_changed_top_candidate_is_regated`
  - `test_refresh_tracking_gates_unchecked_lessons`
  - `test_lessons_with_a_pending_filing_are_not_matched`
  - `test_full_issue_page_prints_a_warning_not_an_error`
  - `test_failed_issue_listing_leaves_lessons_unchecked_and_exits_1`
  - `test_memory_scan_error_leaves_lessons_unchecked`
  - `test_haiku_non_answer_is_not_cached_as_no_match`
  - `test_match_exit_codes` (0, 1, 2)
  - `test_match_with_no_since_is_quiet_not_rule_not_followed`
  - `test_refresh_tracking_refreshes_a_matched_issue_and_a_matched_rule_path` (#473 C.2)
  - `test_report_and_note_show_tracking_source` (`match` vs `trace` vs `filed` vs `none`)
  - `test_member_trace_and_filed_issue_beat_a_match`
  - `test_backfill_traces_a_match_only_with_a_matching_accept_entry` (#473 C.3)
  - `test_match_and_update_never_write_a_trace` (#473 C.3)
  - `test_untraced_reason_lesson_matched`
  - `test_planned_filings_skip_matched_lessons`
  - `test_match_makes_no_gh_write` (the fake `_run_gh` fails the test on anything but `issue list`)
- [ ] **Step 2:** Run `python3 -m pytest tests/test_retro_rollup.py tests/test_lessons_notes.py -q -p no:cacheprovider`; record the red run.
- [ ] **Step 3:** Implement:
  - `match` subcommand (`--concurrency`, default config `lessons_concurrency`, 1-16; `--all` re-checks every untracked lesson). Lessons to check: no member-trace tracking, no filed issue, no pending filing record, and `match` absent or its `candidate` differs from the current top candidate hash. The same check runs at the start of `refresh-tracking`. One `gh issue list --repo R --state all --limit 1000 --json number,title,body,state,stateReason,milestone,labels,createdAt,closedAt` per repo of those lessons' projects (`_origin_repo` / `project_repos`), read-only, 60 s timeout. A full page (1,000 issues) prints a warning line (older issues are not candidates), not an error. A failed repo marks its lessons' issue source as failed: those lessons are skipped this run (unchecked) and the run exits 1.
  - Per lesson: `best_candidate` over issues (its projects' repos), memory, rules (global + its projects); `gate`; save `rec["match"]` as in the data contract; save state after the pass; then re-render (regroup without judging, as `rebuild` does).
  - Regroup precedence and `source` as in the data contract; `refresh_tracking` unchanged in shape but now sees match tracking (it already reads `rec["tracking"]`).
  - `lessons_notes`: `tracking_source` field in frontmatter and report; ` (match)` in the text table.
  - Untraced reason `lesson-matched`; `_planned_filings` already skips tracked lessons (verify with the test).
- [ ] **Step 4:** Run the Step 2 command, then `python3 -m pytest -q -p no:cacheprovider -n 8`; expect all pass. Mutation-check the fail-open guards (make a failed listing write `{"none": True}` → the test goes red; restore).

### Task 3: spike fixture and its test

Delegate: claude — exports from scratchpad files outside the repo and strips text; needs the private ref map to verify nothing leaks.

**Files:**
- Create: `tests/fixtures/473-eval/README.md`, `round3/` and `round4/` (key, labels with `reason` removed, Haiku verdicts), `tests/test_matching_fixture.py`

- [ ] **Step 1:** Export round 3 and round 4: `key.json` (blind id → unit, item, sets, random), labels `{id: keep|drop}` per judge, round 4 `haiku.json` (lesson id → accept|reject). No text, no refs.
- [ ] **Step 2:** `tests/test_matching_fixture.py`: `test_round4_bar_holds_on_committed_labels` (design precision ≥ 0.80 and > baseline under each judge; ≥ 15 accepted), `test_round3_bar_missed_as_recorded` (design not above TF-IDF top 1), `test_fixture_holds_no_text` (every string value is an id, a label, a set name or a verdict: `^[A-Za-z0-9_-]{1,40}$`).
- [ ] **Step 3:** Run `python3 -m pytest tests/test_matching_fixture.py -q -p no:cacheprovider`; expect pass.

### Task 4: docs, architecture, spec

Delegate: claude — edits `CLAUDE.md` (guarded path) and the architecture page.

**Files:** `README.md`, `CHANGELOG.md`, `CLAUDE.md`, `docs/architecture/architecture.json` (+ rendered HTML), `docs/plans/471-retro-lessons-design.md`

- [ ] **Step 1:** README "Retro lessons": the `match` command, what it reads, that it never writes GitHub or traces, and that matches are traced only by an accept entry; numbers from the final live check.
- [ ] **Step 2:** CHANGELOG `[Unreleased]` entry for #473. `CLAUDE.md` key files: `hooks/lessons_match.py`.
- [ ] **Step 3:** Architecture: component `lessons_match`, flow `match` (retro_rollup → gh issue list → lessons_match → Haiku → state → notes), `lastUpdated`. Re-render HTML; smoke test `[main]` and `[sparse]` pass.
- [ ] **Step 4:** Design spec "Matching to tracking": replace "top 5 per source ... order issue, then rule, then memory" with the round 4 ruling and link #473.

---

## Self-review

- Spec coverage: matching sources (Task 1), tracking and buckets (Task 2, C.2), accept-only traces (Task 2, C.3), no live touches (Global Constraints, C.4), docs (Task 4, C.5), spike fixture (Task 3, C.1).
- Quantifier check: C.2 "a lesson with a confirmed match leaves bucket 2" holds for every accepted match unless its ref is gone (then `tracking_gone`, bucket 2 by design). C.3 "never traced without the user's batch confirm": the only trace writer is `backfill --apply` with an accept entry; `test_match_and_update_never_write_a_trace` covers the other commands. C.1 asked for 20 lessons with confirmed matches; round 4 had 22.
- Review Focus: a project with no repo (issue source empty, memory and rules still matched); a lesson whose top candidate is its own filed issue (filed beats match); an empty memory dir; a plugin cache with non-numeric version dirs (ignored).
