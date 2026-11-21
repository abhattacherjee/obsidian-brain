# Plan: retro runs in worktree-isolated sessions; note bodies come from a file (#518, #517)

## Finding that set the scope

A worktree-isolated agent on Claude Code 2.1.330 ran nine probe commands on 2026-11-21.
Only `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` was refused ("names git in a
form too complex to verify"). A plain `git rev-parse --show-toplevel`, a quoted heredoc,
`HOOKS=$(python3 -c "...")`, a multi-line `python3 -c '...'` and a `nohup ... &` line all ran.
The heredoc refusals in #518 came from the same command carrying that `cd` line.

So the inline `python3 -c` helpers stay. Moving them into scripts (#518 "Fix" bullet 2) is
dropped: they run as they are, and the acceptance criteria do not need it.

## Tasks

### Task 1: `note_writer.py write --body-file PATH`

Delegate: codex — one CLI file plus its test file, a clear `Check:` command.

- `hooks/note_writer.py`, `write` branch of `main()`: accept `--body-file PATH` in any position
  after `write`, next to `--overwrite`. With it, read the note from PATH and do not read stdin.
  Without it, behaviour is unchanged (stdin).
- Read PATH as UTF-8 text with the same `STDIN_CAP_CHARS` cap and the same oversize rule:
  over the cap prints `_oversize_error()`-style `ERROR:` and exits 1, nothing written.
- PATH missing, a directory, unreadable, or not valid UTF-8: print
  `ERROR: cannot read --body-file <PATH>: <reason>` to stderr, exit 1, write nothing.
- `--body-file` with no value, or given twice: print usage-style `ERROR:` and exit 2.
- The content then goes through `run_write()` exactly as stdin content does (same validation).
- Update the usage string and the module docstring.
- Tests (`tests/test_note_writer.py`): file form writes a byte-identical note to the stdin form;
  `--overwrite` together with `--body-file`; missing file, directory and chmod-000 file
  (skip when root) each exit 1 with no note on disk; oversize file exits 1 with no note;
  `--body-file` with no value exits 2; given twice exits 2; stdin is not read when the flag is
  set (pass stdin content that differs and assert the file content won).

### Task 2: `retro_trace.py apply --traces-file PATH`

Delegate: codex — one CLI file plus its test file, a clear `Check:` command.

- `hooks/retro_trace.py` `main()`: `apply <vault> <note> [--partial] [--traces-file PATH]`,
  flags in either order. With `--traces-file`, read the JSON from PATH (same cap) instead of
  stdin.
- PATH missing, a directory, unreadable or not UTF-8: print
  `apply: cannot read --traces-file <PATH>: <reason>; nothing written` to stderr, exit 2.
  Over the cap: the existing over-cap message, exit 2. Unknown flag, flag with no value,
  or given twice: usage, exit 2.
- Update the usage string.
- Tests (`tests/test_retro_trace.py`): file form traces the note like stdin; with `--partial`
  in both orders; missing and directory files exit 2 and leave the note byte-identical;
  `--traces-file` with no value exits 2.

### Task 3: retro SKILL.md has no git `cd` and no heredoc

Delegate: claude — SKILL.md prose and the skill-contract tests that pin it.

- Step 1: first run `git rev-parse --show-toplevel` as a command of its own; if it fails, run
  `pwd`. The printed path is `<REPO_ROOT>`. The config block also prints `WORK=` (the
  `~/.claude/obsidian-brain` directory, created at 0o700) for the body and traces files.
- Every `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` line (8) becomes
  `cd '<REPO_ROOT>'`.
- Step 7: write the note with the Write tool to `<WORK>/retro-note-<hash>.md`, then run
  `note_writer.py write ... --body-file '<BODY_FILE>'`. On `OK:` run `rm -f '<BODY_FILE>'`.
  On failure keep the file so the note text is not lost, and tell the user where it is.
  The heredoc terminator rules paragraph goes.
- Step 7.5 step 4: write `{"traces": [...]}` with the Write tool to
  `<WORK>/retro-traces-<hash>.json` and pass `--traces-file`; remove it after exit 0.
- Tests: `tests/test_retro_trace_skill_contract.py` reads the example payload and runs the
  documented apply block from the new form. New test: retro SKILL.md bash blocks contain no
  `$(git` and no `<<`; the write and apply calls carry `--body-file` / `--traces-file`.

### Task 4: the other six skills write notes from a file (#517)

Delegate: claude — prose in six SKILL.md files and the shared snippet tests.

- `decide`, `compress`, `standup` (2 calls), `vault-import`, `error-log`, `vault-stats`: the
  `note_writer.py" write` heredoc becomes Write tool + `--body-file`, same keep-on-failure rule.
  The body file goes in `<HOME>/.claude/obsidian-brain/` (run `echo "$HOME"` if unknown).
- Tests: a test that every `note_writer.py" write` call in `skills/` carries `--body-file` and
  no `<<'OB_NOTE_EOF`. Update the heredoc tests in `tests/test_skill_snippets.py` and
  `tests/test_skill_shell_blocks.py` that expect those openers.

### Task 5: docs

Delegate: claude — CHANGELOG and architecture.json need judgment about what changed.

- `CHANGELOG.md` Unreleased: Fixed (#518) and Changed (#517).
- `docs/architecture/architecture.json`: note_writer and retro_trace descriptions if they
  name stdin as the only input; re-render and smoke-test.

## Failure modes

- **Fail-open paths:** a missing or unreadable body file must not write an empty note or print
  `OK:`. Tests: missing, directory, chmod-000, non-UTF-8 → exit 1, no file. Same for traces
  (exit 2, note unchanged).
- **Exit codes:** every `ERROR:` branch has an exit code named above; each test asserts it.
- **Date boundaries:** none; nothing here reads dates. Checked: the change touches argument
  parsing and file reads only.
- **Doc counts:** "8 cd lines" and "six skills / seven calls" are counted from grep at plan time;
  the PR body recounts from the diff.
- **Broken Markdown:** SKILL.md edits keep each fenced block closed; the shell-block tests parse
  every fence; CHANGELOG entry goes inside the Unreleased block.
- **Unrelated changes:** the `cd "$(git ...)"` lines in the other 20 skills stay (follow-up issue);
  no file deletions planned.
- **Inputs:** `--body-file` / `--traces-file` read one local file the model wrote moments
  before; the vault is the destination, not a source. A stale body file from a failed earlier
  run could be re-used: the skill names a fresh `<hash>` per run, and the file is removed on
  success. Test: a missing file is refused, not treated as empty.
- **Bypass routes:** not a gate. The one guard is "no note on a bad body file"; the cheapest
  input that could pass is an empty file, which `run_write()` already refuses (empty content
  test reused via the file form).
