# Plan: write each retro Process Improvements item's tracking back into the note (#470, folds #65)

## Goal

`/retro` Step 7.5 files each Process Improvements item as a GitHub issue, a memory entry, a doc or skill edit, or a skip. It never writes the result back, so the 3,887 items in the 778 live retro notes are all `- [ ]` and none says where it went. This change:

1. writes one trace onto each item when Step 7.5 files it, and ticks it unless it went to an issue;
2. keeps the retro Stop gate armed while any item in the note has no trace;
3. lets `/check-items` offer a tick for an item whose tracked issue is closed (#65).

The backfill of old items and the cross-session rollup are #471. Cites are at `develop` `2e38bb2`.

## Rulings

- **R1 Trace grammar.** A trace is the text after the **last** ` → ` on an item's **last** line, and it must be one of these four forms, end-anchored:
  - issue: `[OWNER/REPO#N](https://github.com/OWNER/REPO/issues/N)`. OWNER and REPO match `[A-Za-z0-9_.-]+`, N is `[1-9][0-9]*`, and the URL must repeat the same owner, repo and number.
  - memory: ``memory `STEM` `` with STEM matching `[A-Za-z0-9_.-]+`.
  - path: `` `PATH` `` where PATH has no backtick and contains `/` or `~` or ends in `.<ext>` (`[A-Za-z0-9]+`).
  - skipped: `skipped: REASON` with a non-empty REASON (anything to the end of the line).

  After the issue, memory or path form only this may follow: an optional ` (…)` with no `)` inside, then an optional `, "…"`, then an optional `.`, then spaces. This accepts the live example `2026-10-29-retro-2816` (`(Update 2026-10-29)`, `, "Re-baseline a numeric bar"`). Of the 3,887 live items, 106 contain `→`, and 96 of those are prose. The anchoring keeps prose such as ``→ `/dev-test restore` before merge`` from reading as a trace.
- **R2 Items.** The section starts at the line `## Process Improvements` (trailing text allowed) and ends at the next line starting with `## ` or at the end of the file. `###` lines stay inside it. An item is a top-level `- [ ] ` or `- [x] `/`- [X] ` line, plus the indented, non-blank lines after it, up to the next blank line, top-level line or heading. Items are numbered from 1 in file order. Plain `- ` bullets are not items. The live survey: 3,875 top-level open, 12 done, 311 continuation lines, 3 `###` lines, 9 notes without the section.
- **R3 Write-back (`apply`).** The input is JSON `{"traces": [...]}` with one entry per **untraced** item. Each entry has `item` (1-based) and `kind` (`issue` with `ref` `OWNER/REPO#N`; `memory` with `stem` and an optional `note` such as `new` or `Update 2026-10-29`; `path` with `path`; `skipped` with `reason`). Values may not contain a newline, `→` or (memory and path) a backtick, and are capped at 300 characters. Every entry is validated before anything is written: an unknown item, a traced item, a duplicate, a missing untraced item or a bad value is an error, and the note is left byte-equal. When all entries are valid, under the note's lock (`note_writer._acquire_lock`), the change appends ` → <trace>` to each item's last line. It ticks `- [ ]` to `- [x]` for every kind except `issue`, and merges the issue refs into frontmatter `tracked_issues`. It writes through `note_writer._atomic_rewrite` with the pre-read stat check, keeping line endings and the 0o600 mode. No other line changes.
- **R4 `tracked_issues`.** The field is written as a flow list of double-quoted refs, `tracked_issues: ["owner/repo#1", "owner/repo#7"]`, existing refs first, in order, without duplicates. It is read back from either a flow list or a block list (`  - "owner/repo#1"`). `note_writer._set_fields` cannot write it because `_FIELD_VALUE_RE` rejects `#`, `"` and `[`, so `retro_trace` builds the line itself and places it with `_set_fields`-style replace-or-append on the frontmatter lines. A test checks that `vault_index` still reads the note's `tags` after the field is added.
- **R5 Gate.** `obsidian_utils.clear_retro_classification_pending(session_id, force=False)`. Without `force`, it reads the sentinel's `retro_path` and refuses (returns False, sentinel kept) while `retro_trace.untraced_count(path)` is a positive int. `None` (note missing, unreadable, no frontmatter) and `0` clear. `obsidian_retro_gate.py` passes `force=True` on its two fail-open paths (`stop_hook_active`, TTL). When it blocks, the hook names the untraced count when it can read one; otherwise it keeps today's reason. The hook does not auto-clear: Step 7.5 also classifies Key Learnings, which carry no traces, so the skill still clears the gate itself.
- **R6 Cancel versus declined.** A classification the user **cancels** (no go-ahead, an `apply` error, or the session ends) writes nothing, so the note stays byte-equal and the gate stays armed. The Stop hook then blocks once, and `stop_hook_active` force-clears it. This is the intended sequence. An item the user **declines** is an outcome: it gets `→ skipped: user declined`.
- **R7 Closed-issue candidates (#65).** `retro_trace.closed_candidates(vault, insights_folder, project=None, gh=None)` reads retro notes `<vault>/<insights_folder>/*-retro-*.md` (direct children; a glob, so it needs no index), filtered by frontmatter `project` when given. For each note with `tracked_issues`, it collects the open items whose trace is an issue ref. It looks up each distinct ref once per run with `gh issue view N --repo OWNER/REPO --json state,stateReason` (`stdin=DEVNULL`, `timeout=GH_TIMEOUT_SECONDS = 10`). A `CLOSED` ref yields one candidate per item line: `{file, line, text, ref, state_reason, preselect}`, where `preselect` is true only for `COMPLETED`, and the label is `[closed on GH]`, or `[closed on GH: not planned]` for any other reason. An open ref yields nothing. A `gh` failure, a timeout, non-JSON output or a missing `gh` yields no candidate for that ref and one entry in `errors`. Only a ref listed in the note's `tracked_issues` is looked up, so prose links never reach `gh`.
- **R8 CLI.** `python3 hooks/retro_trace.py`:
  - `apply <vault> <note>` reads the JSON on stdin (capped at 1,000,000 bytes) and prints `{"written": bool, "untraced": int, "tracked_issues": [...]}`. Exit 0 on success, 2 on a validation error (the note untouched, the error on stderr), 1 on a lock or write error.
  - `untraced <note>` prints the count, or `unreadable` with exit 1.
  - `items <note>` prints the parsed items as JSON (item, line, checked, text, trace), so the skill can number them.
  - `closed-candidates <vault> <insights_folder> [--project P]` prints `{"candidates": [...], "errors": [...]}` and always exits 0. The skill reports any errors.

## Tasks

### Task 1 — `retro_trace` parse, apply, untraced and the CLI

Delegate: codex — new self-contained module plus its tests, 3 files, one pytest command.

- Files: `hooks/retro_trace.py` (new), `tests/test_retro_trace.py` (new), `tests/fixtures/retro/traced-retro.md` (synthetic; its four traced items reproduce, shape for shape, the four in the live note `2026-10-29-retro-2816`, which is not copied into the repo).
- Functions: `parse_items(text) -> list[dict]` (keys `item`, `start`, `end` (0-based line indexes), `checked`, `text`, `trace` = `{"kind", ...}` or None); `parse_trace(line) -> dict | None`; `untraced_count(path) -> int | None`; `read_tracked_issues(text) -> list[str]`; `apply_traces(vault_path, note_path, traces) -> dict`, which raises `TraceError` (a `ValueError`) on validation and `OSError` on lock or write. CLI `apply`, `untraced` and `items` as in R8.
- Tests first:
  - The fixture parses to its 4 traced items with kinds memory, memory, path and skipped, and `untraced_count` is 0.
  - Grammar: each R1 form is accepted, with and without trailing text. Rejected: prose `A → B`; ``→ `/dev-test restore` before merge``; an issue URL whose number differs from the ref; `skipped:` with no reason; an arrow on a non-last line of a multi-line item.
  - Items: continuation lines belong to the item above, `###` stays in the section, the next `## ` ends it, plain bullets are not items, and a note with no section has 0 items.
  - Apply on a fixture with one item per kind (one of them multi-line): every item line matches the expected output, issue items stay `- [ ]`, the others are `- [x]`, `tracked_issues` equals the expected list, and every line outside the items and that field is byte-equal. A second apply that adds one ref merges without duplicates.
  - Byte-equal on refusal: a missing item, an extra item, an already-traced item, a duplicate, a bad ref, a newline or `→` in a value. Each raises `TraceError`, and the file bytes and mtime are unchanged.
  - CRLF note: line endings are kept. A held lock raises `OSError` and changes nothing.
  - `vault_index.parse_frontmatter_field` (or the indexer's tag parse) still reads `tags` after `tracked_issues` is added.
  - CLI exit codes 0, 2, 1, and `untraced` on a missing file exits 1.
- Check: `python3 -m pytest tests/test_retro_trace.py -q -p no:cacheprovider`

### Task 2 — closed-issue candidates (#65)

Delegate: codex — adds one function and the CLI subcommand to the Task 1 module and its tests, mocked `gh`, one pytest command.

- Files: `hooks/retro_trace.py`, `tests/test_retro_trace_closed.py` (new).
- `closed_candidates(vault_path, insights_folder, project=None, gh=None) -> dict` per R7. `gh` is a callable `(args: list[str]) -> subprocess.CompletedProcess`, defaulting to a `subprocess.run` wrapper with `stdin=DEVNULL`, `capture_output=True`, `text=True` and `timeout=GH_TIMEOUT_SECONDS`. CLI `closed-candidates`.
- Tests first (temp vault, fake `gh`; no live GitHub calls):
  - A closed `COMPLETED` ref gives a preselected candidate with the right file, 1-based line and text. A closed `NOT_PLANNED` ref gives a candidate that is not preselected and carries the not-planned label. An open ref gives none.
  - A `gh` that exits non-zero, raises `TimeoutExpired`, raises `FileNotFoundError` or prints non-JSON gives no candidate and one error naming the ref.
  - Each distinct ref is looked up once even when it appears in two notes.
  - A ticked item, an item whose ref is not in `tracked_issues`, and a note of another project (with `project=` given) give no candidate and no lookup.
- Check: `python3 -m pytest tests/test_retro_trace_closed.py tests/test_retro_trace.py -q -p no:cacheprovider`

### Task 3 — gate refuses to clear while items are untraced

Delegate: claude — changes the contract of a live Stop hook and of `obsidian_utils` (a 5,000-line shared module). Inline.

- Files: `hooks/obsidian_utils.py` (`clear_retro_classification_pending`), `hooks/obsidian_retro_gate.py`, `tests/test_retro_gate.py`.
- Per R5. `retro_trace` is imported inside the function (it imports `note_writer`, which imports `obsidian_utils`).
- Tests first: an armed gate whose note has one untraced item refuses to clear and the sentinel stays; after `apply`, it clears. `force=True` clears regardless. A sentinel whose `retro_path` is missing clears. The hook blocks with the count in its reason, and still fails open on `stop_hook_active` and TTL with an untraced note. Every existing `test_retro_gate.py` test passes unchanged.
- Check: `python3 -m pytest tests/test_retro_gate.py -q -p no:cacheprovider`

### Task 4 — skill wiring

Delegate: claude — SKILL.md prose for two skills, with the cancel and declined distinction. Inline.

- Files: `skills/retro/SKILL.md` (Step 7.5), `skills/check-items/SKILL.md` (new Step 2b, Step 7 display, Step 8 flip), `tests/test_retro_trace_skill_contract.py` (new).
- Retro Step 7.5:
  1. Run `retro_trace.py items` to number the items.
  2. Classify and file as today.
  3. Build the traces JSON: one entry per item, with the issue ref from the `gh issue create` URL, the memory file stem, the edited path, or `skipped: <reason>`. A declined item gets `skipped: user declined`.
  4. Pipe it to `retro_trace.py apply`, and show its output.
  5. Clear the gate. When the clear prints `blocked`, re-run `untraced` and trace what is left.

  A cancel (R6) writes nothing.
- Check-items:
  - Step 2b runs `closed-candidates` for the scope and prints each error line.
  - Step 7 lists the candidates under `=== Closed on GitHub ===`, `[x]` when `preselect`, else `[ ]`, with the label and ref.
  - Step 8 flips the confirmed ones with the same read, verify and Edit procedure. Dry-run shows them and flips nothing.
- Contract tests: Step 7.5 names `retro_trace.py apply` and `skipped: user declined`; the clear step handles `blocked`; check-items names `closed-candidates` and `[closed on GH]`.
- Check: `python3 -m pytest tests/test_retro_trace_skill_contract.py -q -p no:cacheprovider`

### Task 5 — architecture page and CHANGELOG

Delegate: claude — runs the architecture-page scripts under `~/.claude/skills/`, outside the worktree.

- Files: `docs/architecture/architecture.json`, `docs/architecture/architecture.html`, `CHANGELOG.md`.
- Add the `retro_trace` component (`files: ["hooks/retro_trace.py"]`) and its flows: retro Step 7.5 → `retro_trace apply` → retro note; Stop gate → `retro_trace.untraced_count`; check-items → `retro_trace.closed_candidates` → `gh`. Set `lastUpdated` to the change date and keep `version` equal to `plugin.json`. Re-render and run the smoke test (`[main]` and `[sparse]` pass). Add an `[Unreleased]` CHANGELOG entry.
- Check: `~/.claude/skills/architecture-page/scripts/smoke-test.sh --json docs/architecture/architecture.json`

## Failure modes (self-check classes)

1. **Fail-open paths.**
   - `apply` with any invalid entry must write nothing: Task 1 byte-equal tests.
   - `untraced_count` returning None for an unreadable note clears the gate. This is deliberate, matches the gate's fail-open contract, and is tested in Task 3.
   - `closed_candidates` on any `gh` failure offers no tick: Task 2 failure tests.
   - A note the parser cannot split (no frontmatter) gives `TraceError`, not a silent no-op: Task 1.
2. **Exit codes.** `apply` returns 2 on a validation error and 1 on a write error, never 0. `untraced` exits 1 on an unreadable note. `closed-candidates` exits 0 but always prints `errors`, and the skill must print them. Tests in Tasks 1 and 2.
3. **Date boundaries.** None. Traces carry free-text notes only, and no date arithmetic is added. The gate TTL is unchanged.
4. **Doc counts.** The CHANGELOG and PR body state counts (items, tests). Count them from the diff and the live survey command in this plan, not from memory.
5. **Broken Markdown.** The CHANGELOG entry goes inside `[Unreleased]`'s existing block. The new SKILL.md steps keep their fences closed. A contract test reads both SKILL files.
6. **Unrelated changes.** Only the files named in Tasks 1–5 plus this plan change, with no deletions. Check with `git diff --stat origin/develop...HEAD` and `git diff --diff-filter=D --name-only origin/develop...HEAD`.
7. **Inputs.**
   - The retro note on disk is the source of truth for traces, and `tracked_issues` in the same note is the source for lookups. There is no cache.
   - A stale or missing note: `untraced_count` returns None (tested).
   - A note edited between read and write: the stat check refuses with `OSError` (tested with a held lock and a changed mtime).
   - The fixture is synthetic and committed, so it cannot go stale under the test. Phase 6 step 1 also runs `parse_items` read-only on the live `2026-10-29-retro-2816` to confirm the shapes match.

### Bypass routes (the gate)

- **Filler traces.** The model writes `→ skipped: x` on every item without filing anything. This is the cheapest input that passes. The gate checks shape, not truth, so this route is accepted and stated: hooks enforce that a decision was recorded, and the skill and the Step 6 preview advise on whether it is right. A test pins that `skipped:` with an empty reason does not count as a trace.
- **Editing the note by hand** to add arrows, or calling `clear(force=True)` directly. Accepted, for the same reason: the agent runs as the user.
- **Deleting the Process Improvements section.** That gives 0 items, so the gate clears. Accepted, because it shows in the note diff and in the Step 6 preview.
- **Prose that looks like a trace** (`→ \`foo/bar\``) is counted as traced. R1's end anchor limits this to an arrow followed only by the path form. The Task 1 tests include the live prose counterexample.

## Dogfood (Phase 6 step 1)

This runs on a copy, never the live vault:

1. Make a scratch vault with `claude-insights/`.
2. Run the skill's own commands with `VAULT_PATH` set to it: `note_writer.py write` for a retro with 4 Process Improvements items, then arm the gate under a scratch `HOME`.
3. Pipe a synthetic Stop payload to `obsidian_retro_gate.py` and expect a block naming 4.
4. Run `retro_trace.py apply` with one item per form. The issue form uses `abhattacherjee/obsidian-brain#414`, which is closed.
5. Clear the gate and expect `cleared`.
6. Run `closed-candidates` with the real `gh`, a read-only call, and expect one preselected candidate for #414.

The output note is posted on the PR (AC 5).
