# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **`/check-items`'s fallback heuristic no longer reads a *pending* issue reference as a completion (#299):** when the AI classifier degrades, the fallback calls an item done if a distinctive reference (`#51`, a commit sha, `v1.2.0`) sits within 120 characters of a completion word (`merged`, `closed`, `resolved`, `shipped`, …). Co-occurrence alone cannot tell *"Fixed in #51"* from *"Waiting on #51 to be closed"* — both shapes match — so items that were explicitly waiting on, blocked by, or merely asking you to verify something were proposed for checkoff. They are now recognised as open: a completion co-occurrence is rejected when the sentence around it is governed by a pending-intent cue (*waiting on*, *blocked until*, *pending*, *depends on*, and the verification verbs *confirm*, *verify*, *validate*, *check*, *track*, *monitor*), or by a time/condition word (*once*, *until*, *if*, *when*, *after*, *whether*) together with a forward-looking verb form (*is resolved*, *to be closed*, *will be merged*, *gets merged*). Past-tense claims are deliberately left alone — *"After the outage we finally fixed #51"* and *"#51 was closed"* still read as done — and a cue only counts when it comes *before* the completion phrase, so *"Fixed in #51 — confirm with the team"* is still a completion with a note attached. Replayed over every stored `/check-items` run on this machine (398 distinct item groups), the guard turned 6 of the 22 heuristic "done" verdicts back into open items — including a *"Validate that 9 stranded board items (v3.4.0) …"* task that had never been performed — and turned none of the genuine ones into false opens. When a verdict is rejected, the reason is kept on the item (*which* reference, *which* completion word, *which* cue) and printed to the run log, rather than the item quietly reverting to "active" with no explanation.
- **`/retro`, `/compress`, `/decide` and `/error-log` no longer file their notes under a different project's session (#260):** twice, seven weeks apart, a `/retro` run in an active session was handed a completely different, prior session — wrong session id, wrong project, and that other session's snapshots mined as first-class evidence — with nothing on screen to say so. It happened whenever the current directory had no transcript of its own yet: a plain subdirectory of a project (`.../pitch/docs`), or a session whose transcript file had not been written. In that gap the session resolver fell back to a scan that looks at *every* project's session marker and accepts whichever one is recent — a fallback built for the case where the working directory has been deleted out from under a session (a merged worktree), where there is genuinely nothing else to go on. With a perfectly readable working directory there is, so it now answers "unknown" instead of borrowing an answer from the repo next door; "unknown" is already handled everywhere and simply falls back to the live project name. Three things compounded it and are fixed alongside: the per-session cache is keyed by session id alone, so one wrong id handed back that whole session's context (project, hash, and session-note link) before anything could contradict it — cached context now records the directory it was resolved from and is thrown away, loudly, when that stops matching; and the lookup of a project's transcripts matched any project directory *ending* in the same name, so a directory called `docs` could pick up an unrelated repo's `docs` — it now keeps only the directories that correspond to the current one and refuses to guess when it cannot tell them apart — and when directory names alone cannot settle it, it reads the working directory that Claude Code records inside the transcripts themselves, which is ground truth rather than a guess about how a path was turned into a folder name. Two lookup gaps found while fixing this are closed alongside: Claude Code turns a `.` in a directory name into a `-` just as it does an `_`, but the lookup only knew about the underscore, so the transcripts of any directory with a dot in its name (a hidden directory, a versioned folder) were invisible to it; and the retry that copes with renamed folders only ran when the first lookup found *nothing*, so a first lookup that found exactly one *wrong* folder suppressed it — both lookups now run and their results are combined. Knowing about the dot cuts both ways, since it also lets a hidden directory such as `~/.openclaw` match an unrelated repo that merely ends in the same word, so a folder that does not look like the current one is now checked against the working directory recorded in its own transcripts and dropped when they name somewhere else — on this machine that turns a confident wrong answer into a correct "I cannot identify this session". The deleted-worktree fallback that motivated the original design stays reachable: the gate is on whether the working directory could be read at all, not merely on whether a name came out of it, so a session whose directory was deleted out from under it still recovers as before even though Claude Code hands hooks the project directory through the environment. Finally, being unable to identify the current session is now *said*: it prints one line naming the directory and what is unavailable because of it (session-scoped evidence, session-note linking), and `/retro` no longer reports it as "no prior-session evidence found" — that wording claims the vault is empty, when the truth was that the session could not be identified.
- **Vault notes with long frontmatter no longer lose their tags, and notes with broken frontmatter no longer have fields invented for them (#283):** the metadata reader shared by `/recall`, snapshot discovery and `/retro`'s evidence bundle only ever looked at the first 40 lines of a note (30 for the lighter field peek), so any field below that — most often the `tags:` block on `/emerge` and `/standup` notes, whose frontmatter runs as deep as index 460 (the 461st line) — was silently invisible. Measured on the live vault: **26 notes were losing their tags entirely**. The same reader also never checked that a note's frontmatter was actually closed, so on a note with a broken fence it would keep reading into the body and hand back prose as though it were metadata — including manufacturing a `status:` field out of a sentence that merely started with the word. Both readers now use the same bounded, shape-checked frontmatter parser as the search index (#277) and return nothing at all for a note they cannot parse, rather than something wrong — and a note they cannot parse is now *reported* — in `/retro`'s evidence bundle, and on stderr when snapshot *discovery* hits one — instead of disappearing without a word, so a broken note reads as broken rather than as "no insights captured this session". Files that are not notes at all — a Dataview dashboard, a pasted export or a scratch `.md` with no opening `---` fence sitting in the insights folder — are deliberately *not* reported as broken notes; left in, a single such file would raise `/retro`'s "evidence discovery partially or fully failed" banner in every project, in every session, permanently, and there is no consumer that would act on a not-a-note file anyway (`/retro` is not a vault linter).

## [3.5.0] - 2026-08-11

### Fixed
- **`/check-items` no longer fails an entire classification run when one chunk of items errors out (#297):** each chunk now gets up to 2 attempts, and a chunk that still fails degrades only its own groups — every other chunk (already-completed or still queued) keeps dispatching normally. The run as a whole only reports failure if every chunk failed.
- **`/check-items` no longer fails silently when the classifier sub-agent misbehaves (#297):** the sub-agent's captured stderr is now forwarded and printed as a diagnostic on every failed attempt, and "the sub-agent produced no output at all" is now distinguished from "the sub-agent produced output that didn't parse" instead of being reported identically.
- **`/check-items` no longer auto-checks off items that were only classified by the degraded heuristic fallback (#297):** every verdict now carries where it came from (the full AI classifier, the deterministic prefilter, the cache, or the heuristic fallback), and an item classified by the heuristic can never be auto-checked at high confidence — it's capped at MED and shown with a `[heuristic]` marker in the review output, along with a degradation banner whenever any part of the run fell back to the heuristic.
- **`/check-items` no longer caches (or later trusts) a heuristic-derived verdict (#297):** heuristic classifications are never written to the persistence cache, and any older cached verdict for the same item is evicted rather than left to be silently revalidated by a later run — items the heuristic covered are re-classified from scratch next time instead of being locked in.
- **Cached `/check-items` verdicts no longer have their expiry refreshed just by being re-read, or trusted forever by an unusable timestamp (#302):** a verdict replayed from the cache now keeps the timestamp of its last real verification instead of picking up the current run's time, so it is genuinely re-verified once its TTL elapses rather than staying "known" indefinitely on a repo whose HEAD never moves; a stamp that's corrupted, non-numeric, NaN, or dated ahead of the clock is now treated as maximally stale rather than trusted indefinitely, whether it's caught on the write that would have persisted it or the read that would have replayed it.

### Security

- **`scripts/bump-version.sh` no longer executes injected commands from the version source (harden-repo#55):** the script fed the parsed version components straight into bash arithmetic with only an is-it-empty check in front. `$(( ))` recursively expands the *contents* of the variables it evaluates, so an array-subscript payload in the version source — e.g. `x[$(rm -rf ~)].0.0` — ran as a command substitution during a bump, and the mangled result was then written back to the version file at exit 0. All three bump types were exploitable, each with the payload in the component that bump evaluates.

  The fix has two layers, because the first one alone was not enough:

  - `CURRENT_VERSION` is semver-validated before it reaches any arithmetic, with each core component bounded to 9 digits so a long component cannot overflow into a wrapped value.
  - The `-prerelease` / `+build` suffix is **stripped before the components are split**, so only digit runs ever reach `$(( ))`. This is the property that actually makes the arithmetic safe. Validating the string alone is not sufficient: arithmetic does not need a literal `$` or backtick to execute something, because a bare identifier inside `$(( ))` is looked up and its *value* re-evaluated as an arithmetic expression. A version of `1.2.3-zz` with `zz='x[$(cmd)]'` in the environment therefore still ran `cmd` and exited 0 reporting success. The same suffix path also silently **downgraded** `1.2.3-4` to `1.2.0` (`10#3-4 + 1` = 0), which is semver-shaped and so slipped past the write guard.

  Alongside those: the three arithmetic expansions force base 10 (`10#`), so a zero-padded component like `08` is no longer read as an invalid octal literal; carried-through components are normalized through `10#` as well, so `01.02.03` no longer yields `01.02.4`; and a symmetric guard refuses to write a `NEW_VERSION` that is not semver-shaped.

## [3.4.1] - 2026-08-03

### Fixed
- **`/dev-test` no longer fails when invoked from a project other than obsidian-brain (#287):** the skill used to `cd "$(git rev-parse --show-toplevel)"` and run `./scripts/test-dev-skill.sh` relative to whatever repo the current session happened to be in, so calling it from any other project's checkout broke. It now resolves the obsidian-brain checkout in two steps: `git rev-parse --show-toplevel` when that toplevel itself carries the `scripts/test-dev-skill.sh` sentinel, then the registered directory-source install from `~/.claude/plugins/known_marketplaces.json` via a dedicated `_ob_repo()` resolver, and otherwise failing loudly naming both attempted routes. The sentinel is what makes the first step safe: a foreign project's toplevel can never satisfy it, so the bug this fixes still falls through to the registry — while **a git worktree or a second clone now installs itself** rather than the registered checkout. That ordering matters: the reverse silently copied the registered tree's hooks into the cache at exit 0 with a full success transcript, so a maintainer dogfooding an unmerged fix from a worktree would have been testing code they did not write. The resolved tree is now **printed** (`Source checkout: …`) before the install runs, because several checkouts can coexist and nothing else in the transcript names the source. The plugin cache is deliberately not a fallback here, unlike other resolvers in this repo — copying the working tree *from* a stale cache would defeat the point of `/dev-test install`.
- **`scripts/test-dev-skill.sh` no longer fails silently or destructively in four cases (#287):**
  - It **refuses to run from anywhere under `~/.claude/plugins/`**, not just the plugin cache. This repo's `marketplace.json` declares `"source": "./"`, so a marketplace clone under `~/.claude/plugins/marketplaces/` also carries the script at its root and would otherwise have been installable as "the dev version" — a released tree that `/plugin marketplace update` rewrites behind your back. It also still refuses to run outside a real checkout at all. That refusal now holds **when `~/.claude` (or `~/.claude/plugins`) is itself a symlink** — a standard stow/chezmoi dotfiles layout: the check resolves the plugin directory itself rather than composing a resolved `$HOME` with the literal segments `/.claude/plugins/`, which left the two sides of the comparison resolved to different degrees so the guard never fired. Reproduced: with `~/.claude` symlinked, a marketplace clone installed itself over the cache at exit 0 with a full success banner.
  - **A checkout whose `skills/` is empty is no longer accepted as an install source.** The sentinel check tested only that `skills/` *existed*, so an empty one passed — and the install loop's `skills/*/` glob then went unmatched, stayed literal, narrated `skills/*/ -> cache` for a copy that never happened, created a directory literally named `*` inside the plugin cache, published a `.bak` that blocks the next install, and exited 0. The check now requires `skills/` to be non-empty (the same predicate `restore`'s completeness check already used), and the copy loop skips anything that is not a real directory, which also covers a `skills/` holding only files.
  - **`restore` can no longer promote an incomplete backup over a healthy cache.** Its only precondition was that the `.bak` directory *existed* — but `install`'s backup copy is not atomic, so an interrupt, a full disk, or a permission error left a truncated `.bak` indistinguishable from a good one. Restoring it wiped whatever the fragment was missing and still printed `Original vX.Y.Z restored.` at exit 0 (reproduced during review, destroying two skills). The backup is now **built out of place and renamed into position only after the copy succeeds**, so the `.bak` name can only ever appear on a copy that ran to completion — checking the fragment's *shape* instead would have sampled the failure mode rather than closed it, since an interrupt anywhere inside `skills/` (19 directories, the bulk of the tree, and so the most likely interrupt point) leaves a fragment that passes any sentinel probe. That shape check is retained as a cheap secondary guard for backups this script did not create — an older version's, or a hand-renamed one — and the non-atomic `rm`+`mv` swap has a trap that says what state a mid-flight failure leaves behind.
  - **A cache directory the version lookup cannot read now reports what happened instead of exiting 1 with no output at all.** `set -o pipefail` made an empty `grep -v` result — or an unreadable cache directory, which fails the `ls` — abort the version lookup *before* the "no cached version" guard could run, so `status`, the command you reach for to diagnose exactly this state, printed nothing whatsoever. Both cases now name the directory, and the `.bak`-only case (the aftermath of an interrupted restore) explains the one-rename recovery.
  - **An install that fails *partway* now exits 3, and `/dev-test` tells you the cache needs recovering instead of that nothing happened.** The one failure path that can leave the plugin cache modified — the backup is already published and a copy into the cache fails — used to exit 1, the same code every guard that refuses *before* writing anything uses. The skill's catch-all arm read that as *"nothing was installed"*, the exact opposite of what the script had just printed (`Install failed partway. Run "/dev-test restore" to recover.`), and a user told nothing happened does not run `restore` — leaving a half-dev cache plus a stale `.bak` that blocks the next install with "Backup already exists". The exit code now names the state (**0** installed · **2** copied but security tests failed · **3** aborted partway, backup published and the cache may hold a mix · **1** refused before anything was written), so the skill branches on what actually happened rather than pattern-matching the message text, and its catch-all no longer asserts a state it cannot know.
  - **`restore` with no backup to restore now exits 4, and `/dev-test` stops calling it a successful restore.** It used to exit 0 both when it swapped the original back in and when there was no `.bak` at all, and the skill's single exit-0 arm asserted the first — so a run that changed nothing was narrated as *"Original version restored. Start a new session to pick up the restored version."*, sending you to restart a session for a state change that never happened. `restore`'s codes now read **0** restored · **4** nothing to restore (no backup; the cache already holds the released version) · **1** refused or aborted partway.
  - **`/dev-test status` no longer ends every dev-active report with `(diff failed)`.** `diff` exits 1 to mean "the inputs differ" — the only outcome that branch is ever reached for, since a dev version is active precisely because the cache differs from its backup — and `set -o pipefail` turned that into a pipeline failure, so the report listed the changed files and then claimed the comparison had failed. Only a real `diff` error (status 2 or higher) says so now, and a cache byte-identical to its backup says that explicitly instead of printing an empty list.
  - **`/dev-test status` now discloses orphaned `.bak.partial.<pid>` directories** instead of reporting `cache is clean` beside them. A hard kill (SIGKILL, power loss) skips the install's cleanup trap and no later run removes another process's directory. They are genuinely inert — filtered from the version scan, never promoted by `restore`, invisible to install's "backup already exists" check — but each is a *full copy* of the plugin cache, so repeated crashed installs consume real disk with nothing saying so. `status` lists them with a note that they are safe to delete once no `/dev-test install` is running; nothing is auto-deleted, since the pid may belong to a live install.
  - **The install transcript stops asserting work that did not happen, and `/dev-test` stops narrating over it.** `Running security tests...` used to print before the check for whether the security-test script exists (so a missing script read as "tests ran, nothing to report"), and a security-test *failure* printed a warning followed by the full success banner at exit 0. A skipped run now says it was skipped rather than passed, and a failure ends the run with a non-zero exit and no success banner. The skill's own reporting now **branches on that exit status** in all three steps rather than unconditionally telling you "Dev version installed — start a new Claude Code session", which was the exact opposite of what the script had just said.

## [3.4.0] - 2026-07-30

### Added
- **`/vault-doctor` repairs notes missing their opening frontmatter fence (#276):** a new check detects and fixes notes whose frontmatter has no leading `---` — such a note has no parseable frontmatter at all, so its `type`, `date`, `project` and `tags` are invisible to Dataview queries and every other vault tool that filters on frontmatter. Nine notes in a real 2007-note vault were found in this state (seven of them `/retro` notes) — historical damage from the same delegated-write bug fixed by #269, where content handed to a sub-agent to transcribe verbatim lost its opening fence as a prompt delimiter. The check is conservative: it only repairs a note when several independent conditions all hold, including a minimum frontmatter size — so a `Heading\n---` setext-style Markdown heading (a valid, first-class construct where a line of text underlined with `---` renders as an `<h2>`) is never mistaken for lost frontmatter and "repaired" into a frontmatter block, and an ordinary note that happens to start with a colon line can't misfire the check either.
- **Codex compatibility design proposal (#270):** `docs/codex-compatibility-design.md` documents how `obsidian-brain` could run as both a Claude Code plugin and a Codex plugin without forking the vault format or duplicating the Python core — Codex packaging metadata, a `runtime_provider.py` adapter for provider-specific config/state/DB paths, transcript and tool-name normalization, and an incremental `skills-codex/` port ordered read-workflows-first. Status: **proposed**; documentation only, no behavior change. The implementation phases it describes will be filed as separate issues.

### Fixed
- **Notes with long frontmatter are no longer silently excluded from the search index (#277):** `vault_index.py`'s frontmatter parser used to scan only the first 40 lines for the closing `---` fence — a note whose fence sat any deeper (common for `/emerge` and `/standup` notes with long `projects:` lists, whose closing fences run as deep as line 460) was treated as having no frontmatter at all and dropped from the index with no error, no log line, and no way to notice short of an audit. A read-only sweep of the live 2013-note vault found 28 real notes affected this way. The bound now lives in one shared, dependency-free module (`hooks/frontmatter.py`) used by both `vault_index.py` and `note_writer.py`, raised to 1000 lines (~2x the deepest observed real fence), and the closing-fence scan is shape-checked (blank / `key:` / `- item` / indented-continuation lines only) so it can't mistake a body `---` horizontal rule for the frontmatter boundary. This is stricter than the old scan in one respect too: a note whose frontmatter contains a line that isn't one of those four shapes (e.g. a YAML comment, or a fence with trailing whitespace) is now reported as `malformed` rather than silently partially parsed — 0 of 2014 notes in the live vault were affected.
  - **`/vault-reindex` now distinguishes unchanged notes from malformed ones** instead of conflating both under a single `skipped` count (#277, and lands one item of #128). The old wording ("skipped: files without valid frontmatter") made a perfectly healthy `skipped: 2011` — almost entirely notes whose mtime just hadn't changed — read as if the entire vault were broken, which is what triggered this investigation. The report now shows `unchanged` and `malformed` separately, and when `malformed > 0` lists the affected filenames and failure reasons (capped at 20 entries; the reported `malformed` count itself is never capped). `skipped` is retained as `unchanged + malformed` for backward compatibility.
- **Vault note persistence no longer depends on a Write tool call (#269):** `/retro`, `/compress`, `/decide`, `/error-log`, `/standup`, `/vault-stats` and `/vault-import` now save notes through a new deterministic CLI (`hooks/note_writer.py`) instead of a Write tool call. Previously, environments that route tool calls through a context-blind helper sub-agent could have that helper simply refuse to write an orchestrator-authored note; the CLI reads the note content from stdin and performs the same path-contained, atomic write directly, so a note save can no longer be silently skipped by a helper's own judgment.
  - `/compress`'s update-existing path now applies the body append, the `last_updated` bump, and the tag merge in **one** atomic write instead of three separate edits, so a note can no longer be left half-updated if something goes wrong partway through.
  - **The CLI now validates the note content it is handed, not just its arguments.** Empty or whitespace-only stdin, a note body with no `---` frontmatter fence pair, oversize stdin (rejected outright rather than silently truncated mid-note), an empty or malformed `--last-updated`, and an invalid tag value (see the allowlist below) are all reported as `ERROR: <reason>` with a non-zero exit and no filesystem side effect. Previously each of these produced a 0-byte, truncated or structurally broken note and reported success.
  - **`write` refuses to replace an existing note unless `--overwrite` is passed** (only `/standup`'s in-place session-note upgrade passes it), so a filename-hash collision fails loudly instead of silently destroying an insight — restoring the protection the Write tool used to provide. A leading-dot filename (invisible to Obsidian) is rejected too.
  - **Heredoc terminators in the skill call sites are now per-invocation** (`OB_NOTE_EOF_<eof4>`, fresh hex each run, verified against the note content). A note body containing the old fixed terminator ended the heredoc early — truncating the note and handing the rest of its text to the shell as commands — and notes about this plugin routinely quote these very blocks. The blocks also resolve the plugin cache by numeric version (a lexicographic `max()` picks `3.9.0` over `3.10.0`) and check that `note_writer.py` exists before invoking it.
  - **A tag that would corrupt the note's YAML is now rejected by an allowlist** (`claude/topic/foo` shape: letters, digits, `/`, `_`, `.`, `-`). The earlier check listed forbidden characters and missed `]`, so `--add-tags 'a]'` produced `tags: [claude/insight, a]]` — frontmatter that no YAML parser can read, costing the note its `type` and every tag, while the command reported success. Every tag the plugin generates fits the allowlist; a hand-written vault tag starting with `.` or `_`, or containing `+` or `:`, is refused with a message naming it.
  - **A requested tag merge that cannot happen is now an error instead of a silent no-op.** A `tags:` line carrying a trailing YAML comment (`tags:   # topics`) is now recognized and merged into; a note with no recognizable `tags:` block fails loudly and is left untouched, rather than printing `OK:` with the tags quietly dropped.
    - **What you may notice:** if `/compress` updates a note whose `tags:` block is in a shape the matcher does not recognize, that update now stops with an error instead of completing with the tags missing. Add the tags by hand, or re-run without them.
  - **`/compress` no longer corrupts a note whose frontmatter is missing its closing `---`.** The closing-fence search is bounded and shape-checked, so a `---` horizontal rule further down the body can no longer be mistaken for the end of the frontmatter (which previously inserted `last_updated` and tags into the body prose at exit 0). The size bound is 1000 lines and reports its own distinct error — at 200 it false-rejected well-formed `/emerge` and `/standup` notes with long `projects:` lists and misreported them as having no closing `---`.
  - **A second `/compress` update no longer splices itself into the middle of the first.** If an update's own text contained a line like `## Tool Usage` at the start of a line, that marker was mistaken for the note's audit trail by every later update, which then inserted before it — cutting the earlier update in half, at exit 0. The scan now stops at the first `## Update (` heading. Consequence worth knowing: on a note that has both an update and an audit trail, later updates are appended at the end of the note rather than above the audit trail.
  - **Two sessions updating the same note at once no longer lose one of them.** `append-update` now takes a short-lived lock beside the note (auto-recovered if a process dies), and additionally refuses to write if the note changed on disk after it was read — which is what happens if you save the note in Obsidian while `/compress` is drafting. Previously both runs reported success and one update simply vanished.
  - **Fewer false rejections and duplicate tags on real notes.** A `tags: [a, b] # comment` line is now recognized (previously the whole update was refused); a YAML comment inside a block-style tag list no longer splits the block or re-adds a tag that was already there; quoted existing tags (`- "claude/insight"`) are no longer duplicated; and a note whose frontmatter has no `date:` field now gets `last_updated` appended instead of losing the entire update.
  - **`write` and `append-update` now agree on what valid frontmatter is**, so a note this plugin creates can always be updated by it. Previously `write` could accept a note that the update path would refuse forever.
  - **A new tag merged into a zero-indent `tags:` block now matches that block's indentation** instead of getting a hardcoded two-space indent, which left the block with mixed indentation (`tags:` / `- a` / `  - b`). Zero-indent tag blocks are valid YAML and are what most YAML writers emit.
  - **Behavior change:** notes written through this new path now land with file mode `0600` instead of `0644`. This is intentional — it brings the skill-authored write path in line with the hook-authored path (session/snapshot notes) and `/vault-stats`, which were already `0600`. Existing notes on disk are not modified; only newly written notes get the tighter mode.
- **Skills resolve the installed plugin version correctly once it reaches 3.10 or later (#274):** the remaining 35 plugin-cache resolution sites across 10 skills — `/check-items`, `/recall`, `/consolidate`, `/obsidian-setup`, `/vault-ask`, `/vault-search`, `/emerge`, `/vault-reindex`, `/link` and `/vault-config` — now pick the newest cached version numerically instead of lexicographically. Previously a plain string comparison ranked `3.9.0` above `3.10.0`, so the first time a two-digit minor version shipped, every one of these skills would have loaded the wrong (stale) cached tree the moment an older version was still present — surfacing as an opaque traceback or a stale-module import rather than a clear error. A guard test now scans every `skills/*/SKILL.md` and fails, naming the file and line, if the old lexicographic pattern is ever reintroduced.
- **Three unbounded stdin reads that could hang or be abused with oversize input are now capped (#275).** The AST-based guard that checks every stdin read in the codebase only recognized a `.read(...)` attribute call, so `json.load(sys.stdin)` was invisible to it — and two real entry points in `hooks/deep_cli.py` (the `/standup deep` batch-edit and pipeline commands) read stdin exactly that way, unbounded, while the test suite stayed green. Both now route through a single capped reader that rejects oversize input outright rather than truncating it, matching the existing `note_writer.py` behavior. Widening the guard to also catch non-`read` consuming attributes (`readlines`/`readline`/`read1`), stdin passed as an argument to another call, direct iteration, and local aliases (`f = sys.stdin; f.read()`) immediately found a third unbounded reader nobody had flagged: `scripts/vault_doctor.py`'s interactive y/N confirmation prompt used `readline()` with no size limit, now bounded at 1024 characters — no behavior change for any real answer. Internally, the cap constant and docstrings in `check_items_cli.py` also now consistently say "characters" instead of "bytes."
- **Skills no longer silently import a stale plugin-cache tree under a directory-source (local checkout) marketplace install (#278):** converted to the marketplace-installLocation-first resolver — which consults **only marketplace entries whose own `source.source` is `"directory"`**, validates the candidate by the presence of `hooks/obsidian_utils.py`, and falls back to the newest allowlisted plugin-cache version otherwise — in exactly these places: all 68 `skills/*/SKILL.md` hook-resolution sites; 6 `scripts/` verification tools (`scripts/test-security.sh`, `scripts/test-phase1-manual.sh`, `scripts/dev-test/validate_phase2.py`, `scripts/dev-test/test-issue-123-manual.py`, `scripts/dev-test/test-issue-128-manual.py`, `scripts/dev-test/test-issue-192-pollution-guard.py`); and 5 resolver snippets embedded in the still-current `scripts/dev-test/DEV-TEST-ISSUE-101.md`/`DEV-TEST-PHASE2.md` manual-verification checklists. Previously every one of these went straight to the plugin cache, so a developer running a local checkout via a directory-source marketplace entry (rather than `/dev-test install`) had every skill transparently load whatever was last released into the cache instead of the code under test — the silent-failure class #274 fixed the *version comparison* for, but not the *source*, of. **If you installed obsidian-brain the normal way — from the GitHub marketplace — nothing changes for you.** That guarantee needed the directory-source restriction above and does not follow from the sentinel check alone: obsidian-brain's `marketplace.json` declares `"source": "./"`, so the marketplace repo *is* the plugin repo and a github-source marketplace clone also carries `hooks/obsidian_utils.py` at its root. Without the restriction, a github install would have started loading skills from the cache while importing hooks from the marketplace clone — and since `/plugin marketplace update` refreshes that clone without touching the installed plugin, every window between the two would have been a silent skill/hook version mismatch. Left unchanged, deliberately: `scripts/test-dev-skill.sh` and 4 more `scripts/dev-test/test-*-manual.sh` scripts (`test-issue-101-manual.sh`, `test-issue-105-manual.sh`, `test-snapshots-manual.sh`, `test-vault-doctor-snapshots-manual.sh`) whose entire job is validating what `/dev-test install` itself wrote into the plugin cache — plus the `DEV-TEST-ISSUE-105.md`/`DEV-TEST-ISSUE-125.md` checklists, whose verification steps grep that same installed tree — for those, resolving via installLocation first would check the wrong thing. A drift test enforces byte-identical resolver bodies across all 68 `SKILL.md` sites (differing only by quote character and `sys.path.insert` vs. `print`); a second guard covers `scripts/**`, where byte identity is impossible, with the weaker invariant that any file reaching into the plugin cache must consult the marketplace registry too, and consult it first; and behavior tests prove a github-source clone that satisfies the sentinel still loses to the cache, a malformed or missing marketplace entry can never abort iteration over a later valid one, and a relative or empty `installLocation` can never resolve against the caller's cwd. The `note_writer.py` existence guard's error message previously suggested `/plugin marketplace update` as the fix for every failure — misleading under a directory-source install, where the cache isn't stale, it's simply the wrong place — and now names the resolved path and states a remedy that holds for both install types. **Worth knowing if you develop this plugin locally:** on a directory-source install, `/dev-test install` no longer changes which hooks the skills load — your checkout is already what runs. It still matters for a github-source install and for the manual test scripts that assert on the cache's contents.

## [3.3.0] - 2026-07-21

### Fixed
- **Unanchored next-step checkboxes that name shipped work are no longer silently left open (#264):** `/check-items` now surfaces them in a new **Review** tier — a distinctive, unanchored item (a named component, branch, or feature) that neither the deterministic prefilter nor the AI classifier can confirm `DONE` is shown in a visible `## Review` dashboard section instead of quietly falling into `Active`. `/check-items` also gathers git tags and changed paths as additional completion evidence, so an item can now auto-close as `Done` purely on tag/path grounding even with no anchor or PR reference. Separately, `/recall` now flags an open item when a **strictly newer** session's own summary reports it done, with a one-line nudge to run `/check-items` to confirm.

## [3.2.1] - 2026-07-09

### Fixed
- **`/retro` now force-mines current-session pre-compact snapshots (#261):** Step 3 gains a mandatory, visible per-snapshot pre-pass that emits a digest and a `RELEVANT` / `EARLIER-ARC/UNRELATED` verdict for every snapshot in the evidence bundle (default-to-`RELEVANT`), so pre-compact decision points and dead ends are no longer skimmed in favor of the vivid post-compact buffer. `## Evidence Consulted` now lists `RELEVANT` snapshots (with an explicit no-findings acknowledgment for any that yielded nothing) and separately records `EARLIER-ARC/UNRELATED` snapshots with their exclusion reason — so earlier-`/ship`-arc snapshots stop contaminating a multi-arc retro without silently vanishing from the saved note.

## [3.2.0] - 2026-06-21

> **Upgrade note:** This release improves the `/compress` topic-match cosine gate, which relies on per-note TF-IDF vectors. Notes indexed before the vector-storage migration (~26% of a typical vault) have no vector. Run `/vault-reindex` once after updating to backfill them (non-destructive — preserves Friston activation data); until you do, the cosine gate fail-opens on those notes via the AND-path.

### Added
- **`/compress` match prompt now surfaces adjudication evidence (#189):** on a high-confidence existing-note match, Step 3.5 shows the match rank with a calibration band (very strong / strong / moderate / borderline) and the next-best rank, the shared TF-IDF terms between the query and the matched note, and a snippet of the note's first paragraph — so the "update vs create new" choice distinguishes topical identity from tangential keyword overlap instead of being guessed from title/tags alone.

### Fixed
- **`/compress` now also searches `claude-session` notes (Step 3.5) and rescues a borderline rank-gap match when the top result's TF-IDF cosine is strong (`MIN_COSINE_RESCUE = 0.40`), so genuine same-topic notes (often sessions) are no longer missed as false negatives. (#254)**
- **`/compress` no longer surfaces semantically-unrelated notes as high-confidence matches (#252, #108):** the match guard now applies a TF-IDF **cosine floor** on the top result — reusing the stored per-note `tfidf_vector` — in addition to the existing FTS rank-strength and rank-delta gates. Previously a single OR-fallback hit sharing only generic terms (#252), or a cross-topic peer whose rank gap happened to pass (#108), could be matched — risking an "update" that appends to the wrong note. The cosine gate can only *reject* a match, never loosen one, and degrades to the prior rank-only behavior when no query or note vector is available.
  - **Live-calibration refinement:** OR-fallback hits that have no stored `tfidf_vector` are now **rejected** (fail-closed) rather than accepted. Previously the gate failed open for any missing-vector result regardless of how the hit was retrieved; live-vault calibration showed this allowed a NULL-vector OR-fallback hit — matched only on generic tokens — to pass as high-confidence. AND-path hits (all query terms present) without a vector remain fail-open, as full-term presence is sufficient for rank confidence.
- **`/vault-reindex` (non-destructive) now backfills `tfidf_vector` for notes indexed before the vector-storage migration** (~26% had NULL vectors), so the `/compress` cosine gate applies to (nearly) all candidates instead of fail-opening on vectorless notes. (#255)

## [3.1.0] - 2026-06-19

### Added
- `/consolidate stats` now flags themes whose size exceeds a soft cap (`consolidate_max_theme_size`, default 120) and suggests `/consolidate split <id>`. Clustering output is unchanged. (#238)

### Changed
- **Internal:** extracted the reverse-fold centroid math (shared by theme reassignment and note deletion) into a single `_reverse_fold_centroid` helper in `tfidf.py` (#246) — no behavior change.

### Fixed
- **`/standup deep` no longer risks checking off still-active items (#201):** checkoffs are now text-anchored to real `- [ ]` checkbox lines. Previously a drifted classifier line number could flip the *wrong* still-open item, and substring matching could corrupt a quoted-prose line that merely mentioned the item text. Targets are re-resolved by text against the file's actual unchecked checkboxes; when two distinct still-active checkboxes both match, the item is refused rather than guessed (the classifier line number is a diagnostic hint only, never used to disambiguate); and each flip is applied to an exact full-line match only.
- **tfidf recompute on bulk churn (#235):** the corpus IDF is now recomputed after any sync that churns more than half the corpus — counting deletions and insertions together, not insertions alone. Previously a large pruning (or a large delete-and-replace) left surviving notes with stale pre-deletion IDF, skewing similarity/clustering until the next big insert.
- `/consolidate merge <a> <a>` (self-merge) now reports a distinct `cannot merge a theme with itself` error instead of the misleading `theme(s) not found` message. (#239)
- **Theme membership integrity (#234):** a note is now guaranteed to belong to at most one theme — reassigning a note to a different best-matching theme vacates and reconciles (note_count + centroid) its prior theme instead of silently accumulating duplicate memberships.

## [3.0.0] - 2026-06-15

### Added
- `/consolidate` skill: batch-clusters notes into named themes (3+ notes/theme) with Haiku naming, plus `stats`, `split <id>`, `merge <a> <b>` sub-commands and a `--full` recluster. Themes finally populate (previously `assign_to_theme` could only join existing themes, so they stayed empty). (#230)
- `/recall` now shows a **Recurring Themes** section — the project's top themes ranked by activation, so resuming work surfaces the threads you keep returning to. (#231)
- **Theme activation is now populated** (ACT-R recency-decayed from member dates) and refreshed automatically by `/consolidate`, `/merge`, and `/emerge`. (#231)

### Changed
- **`/emerge` now operates on themes instead of raw notes**, so it scales to vaults with 10k+ notes — the synthesis sub-agent reads a compact theme corpus (~30k tokens) rather than the full raw-note corpus (~400k). With fewer than 2 themes in the window it nudges you to run `/consolidate` or widen the window. (#231)
- **Internal:** split `hooks/vault_index.py` (was ~2,068 lines) into focused modules — `hooks/tfidf.py` (TF-IDF primitives; stdlib-only leaf) and `hooks/themes.py` (theme assignment + surprise detection). `vault_index.py` keeps core index/sync/search and re-exports the moved symbols for back-compat; no behavior change. Enabling refactor for Friston Phase 3 (epic #53). Closes #229.
- **TF-IDF relevance scores are now order-independent on full vault re-index.** Previously, notes indexed early in a bulk rebuild carried inflated relevance scores until they happened to be re-touched; `/vault-reindex` now runs a second pass after a bulk insert so every note's score is computed against the final corpus.
- **Faster theme assignment on large vaults.** Theme candidates that share no terms with the note are skipped before cosine is computed (cosine is provably zero in that case), cutting the per-note cost proportionally to the fraction of unrelated themes.
- **Session-note summarization now opens 2 DB connections instead of 4.** The surprise signal and theme assignment are computed in a single transaction, eliminating two extra round-trips on every `/recall` upgrade — this is most noticeable on vaults with many sessions.

### Fixed
- `scripts/test-dev-skill.sh install` now also syncs `hooks/hooks.json` (and the `.claude-plugin/` manifests) into the plugin cache. Previously only `hooks/*.py` and skill dirs were copied, so a newly **registered** hook (e.g. a new `Stop` event) had its script copied but was never registered — it silently never fired after `/dev-test install`. Closes #227.

### Removed
- The raw-note `/emerge` corpus path (`collect_vault_corpus` / `upgrade_and_collect_corpus`), superseded by the theme-level pipeline above. (#231)

### Documentation
- Architecture page (`docs/architecture/architecture.json` + `.html`) synced for the Friston Phase 3 theme engine: corrected skill count (18→19), added `Theme`/`ThemeMember` schema to the types reference, and wired the `/recall` → Recurring Themes flow step. (#53)

## [2.7.1] - 2026-06-12

### Added
- **Deep architecture reference page** (`docs/architecture/architecture.json` + self-contained `docs/architecture/architecture.html`) — layer/flow/design views with data flows rendered as inline-SVG sequence diagrams. Generated via the `architecture-page` skill and verified against live source.
- README links to the live [interactive architecture page](https://abhattacherjee.github.io/obsidian-brain/architecture/architecture.html) on GitHub Pages.
- **`/retro` classification gate (now enforced by a Stop hook).** After writing a retro, `/retro` extracts every Process Improvements / Key Learnings item, classifies it (concrete deliverable → issue tracker / behavioral discipline → memory note / skip), confirms via `AskUserQuestion`, and files it **before** the "saved" confirmation — Step 8 surfaces what was filed. A new **`Stop` hook** (`hooks/obsidian_retro_gate.py`) makes the gate real rather than advisory: Step 7 arms a session-scoped, fail-open sentinel and the hook blocks the turn from ending until classification clears it, with `stop_hook_active` + TTL guards so it can never wedge a session. Stale orphaned sentinels are reaped opportunistically on the next retro. The step is written to be self-contained for any install — issue tracking and the memory index are referenced as optional, not assumed. Closes #223. Skill version: 1.1.0 → 1.2.0.

### Fixed
- **GitHub Pages** — add `docs/.nojekyll` so the legacy Jekyll build stops failing on every `develop` push (the `docs/` tree holds internal specs/plans, not a Jekyll site) and so the static architecture page serves verbatim.
- **GitHub Pages** — add `docs/index.html` and `docs/architecture/index.html` redirects so the Pages site root and `/architecture/` directory resolve to the architecture page (previously returned 404).

## [2.7.0] - 2026-06-11

### Added
- **`scripts/dev-test/test-issue-106-fixture.py` (#152)** — fixture-vault dev-test for
  the `vault-doctor source-sessions` UUID-first taxonomy. Seeds a temp `$HOME` with one
  insight note per signal class (`uuid-basename-stale`, `uuid-day-mismatch`,
  `missing-session-note`, `date-window-hint`, `unresolved`), runs the real
  `vault_doctor.py` dispatcher as a subprocess, and asserts all 7 invariant groups from
  issue #106: `signal_class` top-level (no `extra` key), vocabulary subset, per-class
  confidence values, `unresolved` flag, `date-window-hint` reason suffix,
  `--apply --yes` mutates only the `uuid-basename-stale` note (byte-compares all others),
  and deprecated `convergence_warning`/`convergence_count` defaults on every row.
  Self-contained: deterministic against a clean `$HOME`, cleans up via `atexit`.
  Spec reconciliations for #103 (`--min-confidence`) and #104 (imported-note skipping)
  are documented in the script header.
- **`vault-doctor --min-confidence FLOAT` (#103)** — new flag that filters issues by
  confidence before display and before apply. Semantics: keep issues with
  `confidence >= THRESHOLD`; default `0.0` keeps all (back-compat). Threshold
  `1.0` excludes `conf=0.99` — the `>=` comparison is intentionally inclusive so
  the boundary value is exact. Applies to **both** dry-run report and `--apply`
  so the preview always matches the apply scope. Unresolved issues (`confidence=0.0`)
  are filtered out when threshold > 0.0, which is intentional: their repair is unknown
  so no apply would occur anyway. Range validated: out-of-range values exit 3.
  When active, the report header gains a `[filtered: --min-confidence N, dropped K]`
  suffix — with a per-check breakdown parenthetical (`dropped K (check-a: X, check-b: Y)`)
  when more than one check was scanned, so a fully-filtered check stays attributable
  instead of silently vanishing from the report. The JSON payload gets
  `min_confidence`, `dropped_by_confidence`, and `dropped_per_check` top-level keys
  (omitted entirely when threshold=0.0 for back-compat schema stability
  — mirrors the conditional-row-extras pattern from #98). The filter runs in `main()`;
  check authors do not need to opt in. Invalid (None/NaN/non-numeric) confidence
  values from a buggy check are warned about on stderr and treated as below
  threshold rather than crashing the filter.
  Exit semantics: a run where ALL issues were filtered out still exits 0, but the
  clean line is qualified (`vault_doctor: clean at --min-confidence N (K issue(s)
  below threshold — rerun without the flag to see them)`); no new exit code was
  added — JSON consumers disambiguate all-filtered from genuinely clean via the
  `dropped_by_confidence` key.
  Resolved design question: numeric `--min-confidence` was chosen over the
  `--canonical-only` named-subset alternative proposed in the issue. Numeric is
  general across all checks (any Issue already has a confidence field) while
  `--canonical-only` would be source-sessions-specific and require naming new
  subsets as the taxonomy grows.
- **`vault-doctor --check project-name-canonicalization` (#99)** — new **opt-in**,
  one-time backfill check that rewrites worktree-slug project names (e.g.,
  `obsidian-brain--issue-81-duplicate-sid-collision`) to the canonical main-repo
  basename (e.g., `obsidian-brain`) in session notes and insights. Phase 1
  processes all session notes: for each `project_path:` field, runs
  `git rev-parse --git-common-dir` (cached per path) to derive the canonical
  name, then proposes rewriting `project:` and the `claude/project/<name>` tag
  in the frontmatter block. Tag rewriting targets the OBSERVED tag lines —
  production tags are slugified (collapsed + 40-char truncated, e.g.
  `claude/project/obsidian-brain-issue-81-duplicate-sid-co`), so the check
  matches both the raw and slugified old forms with anchored line regexes
  (prefix-sharing sibling tags are never mangled; leftover non-canonical tags
  are surfaced in the apply result). Phase 2 processes
  insights/decisions/error-fixes/retros: each `source_session:` UUID is looked
  up in the Phase-1 index (using the CANONICAL value, not the note's current
  frontmatter) and the same rewrite is proposed. Edge cases: missing
  `project_path`, deleted path, git unavailable/timed out, git errors (dubious
  ownership etc. — distinguished from a clean "not a git repository" so a
  broken repo never promotes a stale slug to canonical), empty `project:`
  field, or insight with no resolvable session all emit WARN unresolved rows
  (never auto-applied). Non-git project dirs are left alone (cwd basename is
  canonical); snapshot notes are excluded (they share the session's
  `session_id` but are not the session note). `--project` matches either the
  old name or the derived canonical, and filtered sessions still seed the
  Phase-2 index. `confidence=0.9` for resolvable rewrites; `0.0` for WARN
  rows. `DEFAULT_WINDOW_DAYS=9999` scans all notes (`--days` is ignored with
  a notice). Conceptually run after `--check project-name-normalization`
  (underscore → hyphen) so `project:` fields are already hyphen-normalized
  before the canonical comparison. Excluded from the default all-checks sweep
  (`OPT_IN=True`); run explicitly via `--check project-name-canonicalization`.
- **`vault-doctor --check session-coverage` (#98)** — new **opt-in** check that
  detects SessionEnd-hook coverage gaps: for each `<sid>.jsonl` under
  `~/.claude/projects/`, it verifies that a corresponding session note exists in
  the vault. Excluded from the default all-checks sweep (heavy all-projects JSONL
  walk + standing-audit semantics) — run it explicitly via `--check
  session-coverage`. Sessions below the configured
  `min_messages`/`min_duration_minutes` thresholds are excluded using the hook's
  own text-bearing message-count semantics (tool_result-only user entries don't
  count), and the check is a no-op when `auto_log_enabled` is false. Reports the
  expected note path, JSONL size, and a `referenced_by` count so orphaned
  sessions whose insights are already in the vault are prioritized for recovery.
  Gap rows in the `--json` payload carry additional fields: `sid`, `jsonl_path`,
  `strict_fail`, and `referenced_by_count` (only on rows that have them — other
  checks' rows are unchanged). New flags:
  - **`--strict`** — emit `FAIL:` (not `WARN:`) when any note references the
    orphaned session via `source_session`, raising priority in CI/operator
    reports. Changes the reason prefix only — the exit code is unaffected.
  - **`--reconstruct`** — mark gaps as resolvable and enable `--apply` to re-run
    the SessionEnd hook via `scripts/dev-test/replay-sessionend.py`, writing the
    missing session note. Never runs automatically — requires explicit `--apply`.
- **`vault-doctor --check audit-historic-repairs` (#95)** — one-shot, opt-in audit
  of historic source-sessions repairs. Walks the doctor backup runs under
  `~/.claude/obsidian-brain-doctor-backup/`, diffs each backed-up note's
  `source_session`/`source_session_note` against the note's current state
  (oldest backup wins — it is the true pre-doctor original), and classifies
  every historic repair by date agreement: **A** restore (original matched the
  note date, current doesn't — mtime-bug corruption), **B** keep (legit fix),
  **C** same-day ambiguous, **D** both-wrong. Only category A is applied on
  `fix`; restores are themselves backed up under
  `<run>/audit-historic-repairs/` and re-runs are drift-stable. Excluded from
  the default all-checks sweep via the new registry `OPT_IN` attribute.

### Fixed
- **Reconstructable session-coverage gaps now carry confidence 0.9 (#215)** —
  `vault-doctor --check session-coverage --reconstruct` previously emitted every
  gap with `confidence=0.0`, so any `--min-confidence` threshold > 0 silently
  nullified `--reconstruct` (the gaps were filtered out before apply). Resolvable
  gaps (reconstruct mode) now carry `confidence=0.9`, consistent with other
  applyable repairs (canonicalization proposals, audit category-A restores);
  unresolved gaps keep `0.0`. The `--min-confidence` help text was updated to
  match.
- **`audit-historic-repairs` is no longer silent on a missing/empty backup root
  (#215)** — a nonexistent backup root now prints a stderr notice
  (`backup root <path> not found — no doctor backups to audit (no-op, not a
  clean bill of health)`), and the end-of-scan coverage summary prints
  unconditionally, so an empty audit shows `audited 0 backed-up note(s)`
  instead of nothing.
- **Per-check crash containment in the `vault_doctor` dispatcher (#215)** — a
  check whose `scan()` or `apply()` raises no longer takes down the whole run.
  The crash is printed to stderr with a full traceback, the check is recorded
  in a new `crashed_checks` JSON key (present only when non-empty) and in the
  human report header, and the remaining checks still run and report. A run
  with crashed checks always exits 2; with 0 issues it prints
  `vault_doctor: 0 issues, but N check(s) crashed — results incomplete`
  instead of the plain clean line. An `apply()` crash additionally warns that
  some fixes may already be applied (pointing at the backup root).
- **`--min-confidence` drop attribution by signal class (#215)** — dropped
  issues that carry a `signal_class` (e.g. the audit's `historic-keep` /
  `historic-unreadable` infrastructure rows) are now broken out per class in
  the human header (`; by class: audit-historic-repairs: 1 historic-keep,
  1 historic-unreadable`) and in a new conditional `dropped_per_signal_class`
  JSON key, so filtered-out infrastructure failures stay visible. Attribution
  only — no filter exemption.
- **Unconditional end-of-scan summaries (#215)** — `session-coverage` and
  `project-name-canonicalization` now print their end-of-scan summary lines
  even when nothing was scanned (zero counts visible); a silent scan was
  indistinguishable from a scan that never ran. The check registry also warns
  on stderr when a module loads but does not expose the check interface,
  instead of silently skipping it.
- **`vault-doctor source-sessions` skips imported notes (#104)** — notes carrying
  `imported: true` in frontmatter OR a `claude/imported` list item under the
  `tags:` key are now silently skipped by the source-sessions check (the tag
  match is scoped to the tags: block — the same string inside a folded scalar
  or a non-tags list such as `aliases:` does NOT exclude a local note). Their
  `source_session` UUID refers to the originating vault (another machine) and
  can never resolve locally, so surfacing them as unresolved is a known
  false-positive. The skip runs after the `--project` filter, so the count
  reflects the filtered scope. A stderr line
  `[vault_doctor] source-sessions: skipped N imported note(s)` is emitted when
  any are skipped so operators know the check ran completely. Snapshot
  integrity and snapshot migration checks are unaffected — they operate on
  session/snapshot notes in the sessions folder, not on insight-type notes.
  audit-historic-repairs intentionally still covers imported notes — restoring
  a historic wrong repair on one is the correct outcome (the audit is the
  remediation path for pre-#104 wrong repairs).
- `vault_doctor --apply` now prints each error Result's detail message (previously the per-note error string was dropped; only the status mark survived).
- Date-rollover test flakiness (#205): 12 test call sites scanned hardcoded
  April-2026 fixtures with 60-day windows, so the suite started failing once
  the wall clock reached 2026-06-09 (CI was green the day before). Widened to
  the established `days=10000` convention.

## [2.6.2] - 2026-06-08

### Fixed
- **`scripts/git-flow-finish.sh` now reliably commits the post-release dev-cycle
  version bump (#75).** The bump phase used a single multi-pathspec `git add`
  (`plugin.json package.json pyproject.toml Cargo.toml version.txt CHANGELOG.md`);
  because `git add` is atomic, a missing pathspec (e.g. `package.json` in this
  Python repo) aborted the whole command and staged **nothing**, so the bump landed
  in the working tree but was never committed or pushed — leaving `develop`
  half-bumped after every release. Files are now staged individually (skipping
  absent ones), `.claude-plugin/marketplace.json` (which `bump-version.sh` updates
  in lockstep) is now included, and a fail-loud guard aborts if any change remains
  uncommitted after the bump.

## [2.6.1] - 2026-06-07

### Fixed
- Test/dev-test suite no longer pollutes the production index DB
  (`~/.claude/obsidian-brain-vault.db`). `_default_db_path()` now honors
  `OBSIDIAN_BRAIN_DB`; `_connect()` is the single guarded DB chokepoint that
  refuses to open the real production DB under a pytest context; an autouse
  fixture isolates each test's DB; and `no-default-db.py` now forbids raw
  `sqlite3.connect` bypasses across `hooks/`, `skills/`, and `scripts/` (Python
  files, `SKILL.md` python blocks, and shell heredocs, with accurate source line
  numbers). (#192)

## [2.6.0] - 2026-06-02

### Added
- **`summary_pipeline` config key** (`"auto"` default | `"subagent"`) — set to `"subagent"` in `~/.claude/obsidian-brain-config.json` to skip the Haiku `claude -p` summarizer pipeline entirely on machines with slow CLI cold-start, routing all notes straight to the sub-agent fallback. (#84)
- **In-process Haiku→Sonnet→Opus escalation chain (#165)** — `upgrade_unsummarized_note` now retries with a more capable model when the primary model returns empty output (`empty_output` reason only; timeouts and subprocess errors do not escalate). Fallback model chain is now Sonnet first, then Opus (replaces prior direct-to-Opus behavior). `model_used` in metrics is populated with the model that produced the accepted summary.
- **`summary_batch_size` config key (#166)** (default `3`) — `upgrade_batch` now groups session notes into batches of up to `summary_batch_size` and summarizes each group in a single `claude -p` spawn, amortizing CLI startup overhead (~70% reduction vs. per-note fan-out). Set to `1` in `~/.claude/obsidian-brain-config.json` to restore legacy per-note behavior. Per-note parse failures and whole-spawn failures fall through automatically to the per-note solo path, and then to the Phase 2 sub-agent.
- **`/recall` telemetry** — `upgrade_batch()` now returns per-note `elapsed_s`,
  `model_used`, and `fallback_reason`, and appends one record per call to
  `~/.claude/obsidian-brain-summarizer-metrics.jsonl` (100 KB rotation, owner-only).
  The `/recall` status line now reports total wall time and a per-model breakdown
  (e.g., `Step 2: upgraded 7 note(s) in 12.4s wall (5 haiku / 2 fallback)`). Foundation
  for the cost-reduction work tracked in #169. (#74)
- `fallback_reason` populated on pre-summarization failures in `upgrade_unsummarized_note`: `unreadable_note`, `no_session_id`, `no_conversation_content`. Full taxonomy (including Phase 2 validator reserved values, #167/#84) documented in the function's docstring. Closes #183.
- **Aged-note deferral in `/recall` (#168)** — `find_unsummarized_notes` now defers notes whose file mtime is older than `aged_summarize_threshold_days` (default `90` days), have no `[[wikilink]]` inbound references in the vault index, and carry no `summary_pin_tags` tag (default `["claude/keep", "claude/permanent"]`). Deferred notes are reported separately (`skipped_aged`) with a count and escape-hatch hint (`/recall --include-aged`). Deferral is conservative: any index query error or missing DB causes the note to be treated as referenced (not deferred). New config keys: `aged_summarize_threshold_days` (int, days) and `summary_pin_tags` (list of tag strings). Closes #168.
- **`summary_recovery` config key (#167)** (default `true`) — new `_normalize_summary` post-processor recovers structurally-loose Haiku summaries (heading variants like `# Summary` / `**Summary**` / `Summary:`, missing canonical sections, missing `## Importance`) before they reach `upgrade_note_with_summary`'s validation gate, cutting the fallback rate without escalating to Sonnet/Opus or the Phase-2 sub-agent. Set to `false` in `~/.claude/obsidian-brain-config.json` to disable. Applied in both the solo path (`upgrade_unsummarized_note`) and the batch path (`generate_summaries_batch`).
- **Standalone marketplace distribution** — obsidian-brain installs directly from
  `abhattacherjee/obsidian-brain` (`/plugin marketplace add abhattacherjee/obsidian-brain`).
- **Cross-plugin hook dedup guard** (`claim_hook_run` / `release_hook_run`) — when
  both the monorepo and standalone plugins are installed, each session is logged
  exactly once (SessionEnd / SessionStart / PreCompact). New `SKIPPED_DEDUP`
  outcome in the hook log, now emitted by SessionStart as well as SessionEnd. If
  a claimed SessionEnd fails to write its note, the dedup lock is released so a
  sibling copy or a re-fire can still produce it — a transient write error never
  silently drops a session note.

### Changed
- **Summarizer timeout budget** — `generate_summary` / `generate_snapshot_summary` first-attempt `claude -p` timeout raised from 30s to 120s (retry from 60s to 240s) to accommodate slow-start CC builds where cold-start alone can take ~46s. Fixes the 100% Haiku-pipeline failure rate on affected installs. (#84)
- **`upgrade_batch()` gains `summary_batch_size` param** — new optional keyword argument (default reads from config, falls back to 3). When `>= 2`, uses the batched path via `generate_summaries_batch`; when `1`, preserves the legacy per-note `ThreadPoolExecutor` fan-out exactly. Existing callers without the param get batching by default.
- **`upgrade_batch()` return shape** — was `list[tuple[path, status]]`, now
  `list[dict]` with 5 keys (`path`, `status`, `elapsed_s`, `model_used`,
  `fallback_reason`). Backward-incompatible for direct callers; in-tree
  callers (`skills/recall/SKILL.md`, `TestUpgradeBatch` suite) migrated in the same PR.
- **`generate_summary` / `generate_snapshot_summary`** — now return
  `tuple[str | None, str | None]` (text + fallback_reason). Reason is non-None
  only on failure: `haiku_timeout`, `haiku_subprocess_error`, or `empty_output`.
- **`upgrade_unsummarized_note`** — now returns
  `tuple[str, float, str | None, str | None]` (status, elapsed_s, model_used,
  fallback_reason). `model_used` reflects the resolved `summary_model` parameter
  (default `"haiku"`); `None` on failure paths. `sonnet-4.6` / `opus-*` tags
  reserved for Phase 3 (#165).
- `upgrade_and_collect_corpus` and the `/standup` summarizer path now route
  through `upgrade_batch`, so each Haiku summarization is included in a
  metrics record written to `~/.claude/obsidian-brain-summarizer-metrics.jsonl`
  (one JSONL record per `upgrade_batch` call, with `n_notes` reflecting the
  group size — the corpus pass groups by project, `/standup` is `n_notes=1`).
  Closes telemetry gap from #182 (follow-up to #74).
- Release flow is now "tag this repo" — the claude-code-skills monorepo is no
  longer a publish target.
- `scripts/test-dev-skill.sh` discovers the plugin cache directory
  source-agnostically (no longer hardcodes the `claude-code-skills` marketplace).

## [2.5.1] - 2026-05-16

### Changed
- `/check-items` reports now write to `<vault>/claude-check-items/`
  by default instead of `<vault>/claude-dashboards/`. These notes
  list open items rather than driving Dataview queries, so a
  dedicated folder keeps them organisationally separate from real
  dashboards. New config key `check_items_folder` (default
  `claude-check-items`) — set it to `"claude-dashboards"` in
  `~/.claude/obsidian-brain-config.json` to keep the legacy
  location. Existing files in `claude-dashboards/` are not migrated.

### Added
- L2 evidence-presence pre-filter for `/check-items` Stage 4 (`hooks/check_items_prefilter.py`).
  Items with no token overlap with the evidence bundle and no `#N`/commit-sha references are
  classified as ACTIVE or STALE (LOW confidence) without dispatching a `claude -p` sub-agent.
  Bypass with `CHECK_ITEMS_PREFILTER=off`. (#160)
- Telemetry line on every Stage 4 run: `[check-items-cli] classifier: total=N cache_hit=- prefiltered=P subagent=S wall=Ws`
  emitted to stderr for verification and debugging. (#160)
- `mtime` field threaded through `classify_groups_with_agent` payload so L2 can compute item
  age (STALE threshold: >90 days since earliest member mtime). (#160)

### Changed
- `/check-items` L2 prefilter now uses a zone-aware completion-signal
  heuristic. The previous heuristic over-routed items to the sub-agent
  on active projects where WIP items shared vocabulary with their own
  recent commits; the new rule requires either a distinctive-token hit
  in completion-zone buckets (merged PR titles, closed issue titles,
  releases, released changelog sections) or a completion verb near a
  content-token hit in commits/notes. The bridge function also narrows
  PR/issue evidence to titles only and strips `[Unreleased]` from the
  changelog excerpt before the prefilter sees it. Empirical replay
  against the issue-173 reference payload: `prefiltered` jumps from 0
  to 12 on 21 groups. (#173)

- `/check-items` Stage 4 classifier sub-agent now chunks when more than
  `CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE` (default 25) groups reach
  dispatch. Each chunk is sent to `claude -p` sequentially so a single
  call stays well under `SUBAGENT_TIMEOUT_SEC`. Telemetry adds
  `chunks=N` when N > 1. Reopens the scope of issue #160 inside this
  PR rather than deferring. (#173)
- `/check-items` classifier picks its model per chunk under chunked
  dispatch instead of inheriting one model decision from the total
  payload. Each chunk is sized at `<=CLASSIFIER_CHUNK_SIZE` (default
  25), which is below the haiku/sonnet 30-group threshold, so chunked
  runs stay on the fast/cheap model regardless of total payload size.
  Resolves the empirical 58-group / 121 KB timeout from the 2026-05-15
  reproduction where each Sonnet chunk still exceeded 180s. (#173)
- `/check-items` `SUBAGENT_TIMEOUT_SEC` default bumped from 180s to
  300s for cold-start headroom on heavier vaults. Override via
  `CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC`.

### Fixed
- Reduced `claude -p` sub-agent invocations for vaults with many open items that lack
  completion evidence, addressing the timeout root cause reported in issue #160 and
  closed prematurely by #164 (chunking now ships as the durable fix).

## [2.5.0] - 2026-05-14

### Added

- `/check-items` now uses evidence-grounded AI classification (reuses `/standup deep` pipeline). Closes #87.
- `/check-items` supports `all`, `<project>`, `Nd`, `--show-all`, `--dry-run`, `--no-cache` arguments. Order-independent and combinable.
- Two-pass deduplication: token-based coarse grouping followed by an AI semantic merge pass that catches near-duplicate items with zero token overlap. Same-project only; audit trail in dashboard report.
- Cross-project deduplication when scope is `all` — `#534` in two repos no longer collides.
- Dashboard report written to `claude-dashboards/check-items-<scope>-<date>.md` on every run (always, even on `--dry-run` or user cancel).
- `NEEDS-ACTION` tier surfaces fixes that are shipped but require external commands (`gh issue close`, token rotations, etc.) as copy-pasteable strings.
- Classification cache at `~/.claude/obsidian-brain/check-items-classifications.json` with hash / mtime / HEAD / TTL invalidation. Warm-cache runs complete in near-zero time at zero cost; only newly-changed groups are re-classified.

### Changed

- `/recall` no longer surfaces checkoff candidates. Runs as a pure read-only context load with a one-line footer nudge to invoke `/check-items` when there are open items in the project. Closes the 4-6-iteration deferral loop documented in user memory `feedback_recall_deferral_loop.md`.
- `/check-items` argument parsing is now order-independent.

### Removed

- `/recall` checkoff step — the /recall checkoff step (Steps 4-7.5 in prior SKILL.md versions) is gone. Users with muscle memory for `/recall → "skip" → continue` just get shorter `/recall` output; the new footer nudge makes the migration discoverable.

## [2.4.4] - 2026-05-11

### Changed
- `vault-doctor source-sessions`: detection refactored around UUID-first contract. The conf=0.4–0.6 wrong-pick class is eliminated. Confidence bands are now strictly `0.99` (uuid-basename-stale, auto-apply), `0.5` (date-window-hint, manual verify), `0.0` (uuid-day-mismatch / missing-session-note / unresolved, WARN). Every emitted Issue carries a `signal_class` tag. In the `--json` output, `signal_class` is exposed as a **top-level** field on each issue (no `extra` wrapper); the internal `Issue` dataclass stores it under `extra` as an implementation detail. The `apply()` path refuses to write unless `signal_class == "uuid-basename-stale"`, regardless of `--min-confidence`. (#106)

### Removed
- `vault-doctor source-sessions`: removed the `capture_signal != "created_at"` carve-out around Phase 1b — UUID-first now runs uniformly.
- `vault-doctor source-sessions`: removed the convergence guard (multiple-flag confidence cap). Made moot by UUID-first.
- `vault-doctor source-sessions`: removed the mtime SID-rewrite path (`proposed_conf=0.3`). Mtime emits as unresolved.
- `vault-doctor source-sessions`: removed the dedicated `created_at` SID-rewrite confidence (`proposed_conf=0.95`). Created_at signals can still produce a proposal when the UUID is empty/unresolved, but at conf=0.5 as `date-window-hint` (not auto-applyable) rather than the old high-confidence path.

### Fixed

- Fix YAML-frontmatter regex `\s*` cross-newline bug across 8 call sites in
  `hooks/obsidian_utils.py`, `hooks/vault_stats.py`, and
  `scripts/vault_doctor_checks/project_name_normalization.py`. An empty
  `project:` (or `date:`, `status:`, `type:`, `session_id:`) field no longer
  causes the next YAML key's value to be misread. Adds a shared
  `parse_frontmatter_field()` helper. (#94)

## [2.4.3] - 2026-05-05

### Added
- **SessionStart orphan reaper** (`hooks/obsidian_session_reaper.py`): recovers
  above-threshold sessions whose SessionEnd hook never fired (e.g. SIGKILL,
  harness crash). Runs at SessionStart with a 5 s wall-clock cap, permission
  canary, and per-project watermark so the same session is never written twice.
  Opt-out via `reaper_enabled: false` in config. Telemetry via new
  `_append_reaper_log` helper; events land in the existing
  `~/.claude/obsidian-brain-hook.log`. Closes [#125](https://github.com/abhattacherjee/obsidian-brain/issues/125);
  completes Phase 3 of [#100](https://github.com/abhattacherjee/obsidian-brain/issues/100).
- SessionEnd hook now logs structured outcome lines to `~/.claude/obsidian-brain-hook.log` for every exit path (success, all skip reasons, write failure, exception). Enables post-hoc diagnosis of dropped sessions. Issue #100 Phase 1 ([#123](https://github.com/abhattacherjee/obsidian-brain/issues/123)).
- `obsidian_utils.gather_session_evidence(vault_path, sessions_folder, insights_folder, session_id, date, project)` — pure-I/O helper that returns a structured bundle of all snapshots + insights + decisions + error-fixes scoped to the active session (#122).

### Changed
- `write_vault_note()` returns `Optional[str]` (None = success, error string on
  failure) instead of `bool`. Callers updated throughout. SessionEnd
  `WRITE_FAILED` log lines now include `errno` + target path in `detail=`
  rather than the hardcoded string "write_vault_note returned False"
  ([#125](https://github.com/abhattacherjee/obsidian-brain/issues/125) F2).
- Executable dev-test spec at `scripts/dev-test/test-issue-128-manual.py` for the proposed `vault-reindex` observability + safety improvements. Sanity check aborts until the implementation lands; encodes acceptance criteria as runnable Python. Refs [#128](https://github.com/abhattacherjee/obsidian-brain/issues/128).
- SessionEnd replay CLI (`scripts/dev-test/replay-sessionend.py`) and dropped-session fixture corpus (`tests/fixtures/dropped-sessions/`) for regression-guarding the #100 silent-drop bug. Five truncated fixtures (3 H1 long-session/worktree-teardown, 2 H2 partial-flush) drive the real `obsidian_session_log._run()` deterministically. Pair-pattern tests with `xfail strict` automatically detect when the F3 reaper fix lands in #125. Includes `capture-jsonl-fixture.py` for adding new cases. Issue #100 Phase 2 ([#124](https://github.com/abhattacherjee/obsidian-brain/issues/124)).
- `/retro` now discovers and reads every snapshot + insight + decision + error-fix written during the active session, prepending an `## Evidence Consulted` section to the saved retro body. Closes [#122](https://github.com/abhattacherjee/obsidian-brain/issues/122). Skill version: 1.0.0 → 1.1.0.

## [2.4.2] - 2026-04-26

### Added
- E2E integration test for snapshot → summarize → /recall pipeline (`tests/test_snapshot_e2e.py`). Closes #50.
- **`created_at:` ISO-8601 frontmatter** is now written by `error-log`,
  `decide`, `retro`, and `compress` (new-note flow) for sub-day-precision
  capture-time matching by vault-doctor. Net-additive — older notes continue
  to fall back to `date:` (day precision).
- `_first_seen_date(sid)` marker (atomic, idempotent JSON at `~/.claude/obsidian-brain/sessions/<sid>.json`) consulted by both `get_session_context()` and SessionEnd to keep insight wikilinks and on-disk filenames in lockstep ([#101](https://github.com/abhattacherjee/obsidian-brain/issues/101) Fix A)
- `_resolve_session_note_by_hash()` shared helper with type+project filter and collision detection ([#101](https://github.com/abhattacherjee/obsidian-brain/issues/101) Fix C)
- `tests/test_get_session_context.py` — comprehensive coverage of markers, peek helpers, resolver, and the project-slug invariant

### Fixed
- **Issue #105:** `_get_session_id_fast()` no longer raises `FileNotFoundError`
  when the cwd is deleted mid-session (e.g. via `gh pr merge --delete-branch`
  from inside a worktree). Insight savers (`/retro`, `/compress`, `/decide`,
  `/error-log`) now resolve the active session's SID via a recent-bootstrap
  best-effort fallback instead of stamping `source_session: unknown`.
- Source-session basename divergence between `get_session_context()` and SessionEnd that broke insight wikilinks across cross-midnight, worktree, and resumed sessions ([#101](https://github.com/abhattacherjee/obsidian-brain/issues/101))
- Snapshot/session hash collision in the resolver could pick a snapshot when an insight saver intended to link to a session ([#101](https://github.com/abhattacherjee/obsidian-brain/issues/101) Fix C)
- `is_resumed_session` now returns True only when a session-type note for the current project (matched by `project_path` against `cwd`) exists for this session_id's hash — eliminating false positives from cross-project hash collisions and snapshot-only matches ([#86](https://github.com/abhattacherjee/obsidian-brain/issues/86), subsumed by [#101](https://github.com/abhattacherjee/obsidian-brain/issues/101))
- **vault-doctor source-sessions silently corrupted backlinks** when a note's
  mtime drifted past the originating session's JSONL window (any later edit
  by `/check-items`, `/link`, `/compress`, sync clients, or another vault-doctor
  check). The check now uses an immutable signal chain
  (`created_at` → `date` → filename prefix → mtime) for capture-time matching,
  and trusts an existing `source_session` whose JSONL window overlaps the
  note's calendar day. Issue payloads surface `capture_signal` and
  `capture_confidence` so operators can spot heuristic falls. Closes #93.
- **vault-doctor source-sessions further hardening** (issue #93 follow-ups
  surfaced via dev-test verification):
  - Snapshot notes are now filtered out of the session-note SID index.
    PreCompact snapshots inherit the parent session's UUID; without the
    filter they could clobber the parent in the index, causing T5c's
    basename-only repairs to propose snapshot basenames as 'correct'.
  - Notes whose `source_session` UUID has a real JSONL but no session
    note are no longer routed to the date-window matcher. The UUID is
    authoritative; the missing session note is tracked separately
    (issue #98).
  - Day-precision signals (`date`, `filename`) now use a day-overlap
    matcher (greatest overlap with the UTC calendar day) instead of a
    noon-UTC point match. Morning-only and evening-only sessions are
    no longer silently excluded from candidacy.
- **vault-doctor source-sessions multi-session-day convergence**: when a
  worktree-launched insight records `project: <main>` but its source
  session has `project: <main>--<worktree-slug>`, the UUID lookup
  previously failed and the matcher converged every flagged note onto
  whatever session's window contained noon-UTC. Now uses cross-project
  UUID indexing (Phase 1b looks up UUIDs across all projects, not just
  the note's declared project), basename-only repair when UUID resolves
  but the stored basename is stale, capped confidence (≤ 0.6) on
  date-only signals, and convergence-guard tagging when ≥2 flags in a
  project target the same proposed session (confidence further capped to
  ≤ 0.4).
- `/compress` Step 3.5 rank-gap guard no longer rejects legitimate same-topic peer matches. Replaced the 1.5× ratio test with a delta-score test (`|top.rank| - |#2.rank| > MIN_RANK_DELTA`) that scales with rank magnitude, so multi-phase PRs and iterative features no longer silently duplicate instead of prompting for an update. `MIN_RANK_DELTA` tuned empirically against `scripts/compress_rank_gap_corpus.json`; chosen value lives in `hooks/compress_guard.py`. Closes #45.
- `/recall` Step 4 N=1 checkoff branch no longer hits `AskUserQuestion` `minItems=2` validation errors. Single candidates now route to the verbatim text fallback; the `2 ≤ N ≤ 4` picker branch gains an explicit "Skip all — don't check off anything" sentinel option so deferral is a visible selectable choice. Closes #78.
- `vault-doctor` `snapshot-migration` §3 (`snapshot-missing-backlink`) now resolves the parent session note via the `session_id` index built in the same scan, instead of composing a filename from `(snapshot.date, project, sha256(session_id)[:4])`. The old date-heuristic wrote wrong `source_session_note` wikilinks for cross-midnight sessions (PreCompact on day N, SessionEnd on day N+1). Orphan snapshots now emit `unresolved=True` with no speculative wikilink. Closes #68.
- `vault-doctor` `sessions_by_id` in both `snapshot_migration.py` and `snapshot_integrity.py` no longer silently relies on an arbitrary filesystem-order-dependent winner when two session notes share a `session_id`. Colliding sids are tracked in a `_sid_collisions` set at build time, and the four consumers (migration §3 / §4 and integrity §1 / §4) now treat those collisions as ambiguous: the §1/§3 emission paths surface unresolved `"ambiguous parent — multiple session notes share session_id=<sid>; resolve by deduping the colliding session notes in the sessions folder"` Issues, while the §4 fix-proposal loops skip proposing a confidently-wrong backlink or snapshots-list fix. Collision detection is project-blind so `--project=foo` can't silently filter out a cross-project collider and resurrect arbitrary-winner selection inside the filtered scan; emission indices remain project-filtered. `snapshot_integrity.py` also gains an empty-`session_id` pre-guard with a specific reason (parity with `snapshot_migration.py` §3). Surfaced during PR #80 review as a pre-existing weakness made consequential by the #68 fix. Closes #81.
- **Project-name divergence across worktrees**: session notes and insights
  written from a worktree previously recorded the worktree's basename as
  their `project:` field (e.g., `obsidian-brain--issue-81-...`), causing
  vault-doctor's source-sessions check to mis-resolve UUID lookups when
  the same insight referenced a session run in a different worktree.
  All vault notes now record the canonical main-repo basename (e.g.,
  `obsidian-brain`), derived via `git rev-parse --git-common-dir`. CC's
  internal path-encoded JSONL/bootstrap lookups are unaffected. Existing
  notes with worktree-derived project names continue to work via the
  UUID-first cross-project fallback in source-sessions.
- **PR #97 reviewer findings (issue #93)**: 8 follow-up fixes applied:
  C1+I6 surface `convergence_warning` and `convergence_count` at the
  top level of the JSON payload (not under `extra.*`) and document the
  `[CONVERGED <N> flags]` rendering in vault-doctor SKILL.md;
  C2 cap `mtime`-signal proposed confidence at 0.3 (below the 0.4
  convergence floor) so an mtime-only proposal is never auto-applied;
  C3 emit an unresolved diagnostic Issue (with `missing_session_note`
  and `jsonl_path`) when the source UUID has a real JSONL but no vault
  session note, instead of silently skipping; C4 `canonical_project_name`
  returns `"unknown"` when `os.getcwd()` raises (deleted-cwd safety so
  hooks honor their must-exit-0 contract); C5 log a stderr warning when
  `_list_all_session_notes` skips a `.md` file with malformed
  frontmatter (parity with `_list_session_notes`); I4 Phase 1b now falls
  back to `_find_jsonl_anywhere` when `_jsonl_dir_for_project` misses
  on worktree-suffixed project names, preventing date-matcher false
  positives; I7 tighten the UUID-trust regression test to assert the
  C3 unresolved-Issue contract unconditionally.

## [2.4.1] - 2026-04-22

### Fixed
- `/recall` Step 4 could silently close still-open items when Claude's paraphrased candidate presentation didn't match the candidate's actual text. Approval now lands on the verbatim text that will be matched on disk, and each candidate's source line is Read-verified before any Edit. Closes #47.

### Changed
- `/recall` Step 4 now uses Claude Code's native `AskUserQuestion` multi-select picker when ≤4 checkoff candidates are surfaced. At >4 candidates, the text prompt still applies but now shows each candidate's verbatim `- [ ] <text>` line and `file:line` anchor so users can verify before confirming. Items skipped due to source drift are excluded from the cascade step. (See #47.)

## [2.4.0] - 2026-04-21

### Added
- **ci**: `scripts/ci-checks/no-default-db.py` — AST-based guard that fails CI when any call to `ensure_index()`, `rebuild_index()`, or `deep_analysis_pipeline()` inside `tests/` omits `db_path=`. Wired as the `no-default-db-check` job in `.github/workflows/ci.yml` with a 2-minute timeout. Exit 1 on violations, exit 2 on script malfunction (missing dir, unreadable file, syntax error) so CI logs can distinguish the two. `# noqa: no-default-db` marker on any line of a multi-line call span suppresses. `**kwargs` expansion emits a stderr warning so reviewers can verify forwarding callers (GH #46)
- **snapshots**: First-class mid-session checkpoint support. Snapshots now carry `status: auto-logged` (or `summarized`) and `source_session_note` wikilink frontmatter, use seconds-resolution filenames (`-snapshot-HHMMSS`), and are AI-summarized lazily at `/recall` time alongside session notes via a dedicated snapshot prompt
- **session-end**: Threshold bypass — writes the session note even when the transcript is below `min_turns`/`min_duration_minutes` if sibling snapshots exist, so every snapshot has a navigable parent anchor. Emits a `snapshots: [...]` list when siblings are present
- **recall**: `_augment_session_input_with_snapshots()` prepends snapshot summary bodies to the session summarization input so the generated summary describes the full pre- and post-compact arc cohesively
- **recall**: Nested `↳ HH:MM:SS` snapshot rows in the session history table; `LOAD_MANIFEST` surfaces `snapshot_count` and per-snapshot summaries at auto-load depth
- **vault-index**: `log_access()` cascades snapshot accesses to the parent session via a single `executemany`, preventing hot snapshots from outranking their own parent under activation scoring
- **obsidian_utils**: Public helper `fetch_snapshot_summaries(sessions_folder, session_id, date, project)` returning ordered snapshot dicts for presentation reuse across skills; `find_snapshots_for_session()` promoted to public API
- **vault-stats**: `## Snapshots` section — trigger breakdown (compact/clear/auto), sessions-with-snapshots, max snapshots per session, orphan and broken-backlink counters, summarization fraction
- **emerge**: `--include-snapshots` opt-in flag. `collect_vault_corpus()` gains `include_types` / `exclude_types` kwargs (default excludes `claude-snapshot` so mid-session "Key context" bullets don't dilute cross-session pattern synthesis); `run_corpus` cache key includes `include_snapshots` to prevent shape-mismatch returns
- **check-items**: `collect_open_items()` filters to `type: claude-session` (legacy notes without a `type:` field preserved as sessions); snapshot bullets no longer produce false-positive open-item proposals
- **vault-search**: Session hits annotate with `· 📸 N` marker and list snapshots as nested `↳ HH:MM:SS` rows; snapshot hits annotate with `→ [[parent-stem]]`; loading a snapshot pick opens the parent session at session-depth (body + all snapshot summaries)
- **vault-ask**: Synthesis pool includes parent session body for snapshot hits and snapshot summaries for session hits, so answers reflect the full session arc rather than the post-compact tail; snapshot citations accompany parent-session wikilinks
- **config**: `/vault-config` and `/obsidian-setup` warn when `snapshot_on_clear` or `snapshot_on_compact` is set to `false`
- **vault-doctor**: `snapshot-integrity` check module (Phase B) — 5 integrity checks for snapshot notes: `snapshot-orphan` (warn), `snapshot-broken-backlink` (fix), `session-snapshot-list-stale` (fix), `session-snapshot-list-missing` (fix), `snapshot-summary-status-mismatch` (fix). All fix paths are idempotent, short-circuit to `status="skipped"` when the write is a no-op, only mutate inside the YAML frontmatter block (body-level `status:` / `source_session_note:` lines in code blocks or headings are left untouched), and normalise CRLF + UTF-8 BOM on read. Inline-list YAML (`snapshots: [...]`) parsed defensively to avoid string-iteration foot-guns (GH #57)
- **vault-doctor**: `snapshot-migration` check module (Phase C) — 4 ordered idempotent legacy-backfill checks: `snapshot-legacy-filename` (rename `-snapshot.md` → `-snapshot-HHMMSS.md` using file mtime, with collision guard and vault-wide `[[old-stem]]` wikilink rewrite rooted at the resolved vault path so sibling `insights/` and `decisions/` folders are reached even when `sessions_folder` is nested), `snapshot-missing-status` (add `status: auto-logged` or `summarized` based on `## Summary` presence; defensive idempotency re-check against stale Issue replays), `snapshot-missing-backlink` (compute parent stem from `date + slugify(project) + sha256(session_id)[:4]` and write `source_session_note` wikilink when parent exists on disk), `session-missing-snapshots-list` (backfill `snapshots:` block on parent sessions; regex constrained to frontmatter so body-level `status:` lines are never matched). `apply()` processes checks in fixed order, renames files FIRST and rolls back on wikilink-rewrite failure, and forwards both renamed paths AND renamed stems to later checks so the session's backfilled `snapshots:` list points at the POST-rename filename (GH #58)
- **vault-doctor**: skill version bumped `1.0.0 → 1.2.0` with new `--check snapshot-integrity` and `--check snapshot-migration` invocations documented

- **vault-index**: Phase 2 theme engine — `themes`, `theme_members`, and `term_df` tables; `tfidf_vector` JSON column on `notes` (auto-migrated on first `ensure_index()` call)
- **vault-index**: `_tokenize_for_tfidf()`, `_compute_tfidf_vector()` (smoothed IDF), `_cosine_similarity()` for sparse dict vectors, `_update_term_df()` for incremental IDF maintenance
- **vault-index**: `assign_to_theme()` — incremental cosine-similarity clustering after note summarization; notes joining a theme update its centroid via running average under a BEGIN IMMEDIATE transaction so concurrent callers cannot clobber each other
- **vault-index**: `detect_surprise()` — negation-proximity contradiction score persisted on `theme_members.surprise` for Phase 4 retrieval boosting
- **recall**: `upgrade_note_with_summary()` now re-indexes the summarized note, runs incremental theme assignment, and persists a surprise score when the note joins a theme. All theme-side errors are non-fatal — the note upgrade itself is never rolled back by a theme-pipeline failure
- **obsidian-setup**: Step 8.7 Performance Dependencies — optional `numpy`/`scipy` install prompt with idempotent persistence (`optional_deps_prompted`, `optional_deps_declined` config fields); `/obsidian-setup --deps` re-triggers the prompt
- **config**: `check_optional_deps()` returns import-availability for numpy and scipy

### Changed
- **pre-compact hook**: filename pattern extends from `-snapshot.md` to `-snapshot-HHMMSS.md` so multiple `/compact` events in one session no longer collide on write
- **obsidian_session_log**: writes `snapshots: [...]` back-reference list when sibling snapshots exist for this `session_id` (bidirectional link surface)
- **recall**: Replace sub-agent batch summarization (Wave 1-2-3) with parallel Haiku pipelines; sub-agents demoted to per-note fallback only
- **vault-index**: `search_vault()` and `query_related_notes()` now batch their access-log writes via `executemany` on the existing connection (1 commit instead of N, Phase 1 Copilot review deferred item)
- **vault-reindex**: Now **non-destructive by default** — preserves `access_log` (ACT-R activation history), `themes`, and `theme_members` (cluster centroids + surprise scores) while reconciling the note index with current vault contents via incremental `_sync`. Rows whose paths fall outside the current scanned folders are cleaned up (pytest fixture pollution); orphaned access-log and theme-member rows referencing notes no longer on disk are pruned, and `themes.note_count` is recomputed with zero-member themes dropped. Opt into `/vault-reindex --full` for the previous destructive behavior (full DB delete + rebuild from empty schema). `rebuild_index()` gains a `full: bool = False` kwarg and emits `preserved` / `pruned_orphans` fields in its stats dict. Skill version 1.0.0 → 2.0.0

### Fixed
- **recall**: Step 2 Phase 1 now dispatches a single Bash tool call into a Python `upgrade_batch()` helper that fans out Haiku invocations via `concurrent.futures.ThreadPoolExecutor`, instead of N parallel Bash calls. The Claude Code harness serializes parallel Bash tool calls for subprocess-blocking work, so the previous design ran sequentially (wall time ≈ Σ per-call, ~2-3 min for N=10). With true Python-thread fan-out wall time is ≈ max per-call (~30s). `upgrade_batch()` preserves input order, catches per-note exceptions as `Failed: ...` status strings so one bad note can't kill the batch, and lives in `hooks/obsidian_utils.py` (GH #69)
- **tests**: Five `test_standup_deep.py` cases (`TestFtsScopingPerProject::test_fts_evidence_scoped_to_project`, `TestPipelineDirCreation::test_creates_output_dir_if_missing`, `TestRepresentativeKey::test_groups_use_representative_key`, `TestEncodingCorruption::test_pipeline_handles_binary_content_in_notes`, `TestPipelineErrorHandling::test_ensure_index_failure`) passed no `db_path=` to `ensure_index()`/`deep_analysis_pipeline()`, silently writing fixture rows into the user's live `~/.claude/obsidian-brain-vault.db`. Each test now routes through an isolated `tmp_vault / "test.db"` (GH #46)
- **vault-index**: `_sync()` uses `Path.is_relative_to()` instead of prefix-only `startswith()` so sibling folders like `claude-sessions-archive` are no longer incorrectly treated as nested inside `claude-sessions` (Phase 1 Copilot review deferred item)
- **vault-index**: `_prior_terms_for()` → `_prior_tokens_for()` — re-tokenises the stored note body instead of reading the top-K=50 truncated `tfidf_vector` keys, so common-but-low-IDF terms (outside the top-50 cutoff) are no longer incremented on every reindex without a matching decrement. Fixes `term_df.df` drifting upward past the total note count across repeated `index_note` calls (caught by Phase 2 dev-test Step 11 invariant checker, missed by 522 pytest + 27 validator assertions). Existing drift clears on `rebuild_index()`. Regression gates: pytest `test_reindex_does_not_drift_term_df` + validator `test_reindex_invariance`
- **vault-index**: `rebuild_index()` now calls `_ensure_access_log_indexes()` alongside `_ensure_theme_indexes()` so `idx_access_note` / `idx_access_time` are present immediately after a full rebuild, matching `ensure_index()`'s invariants. Previously a rebuilt DB was missing access-log indexes until the next `ensure_index()` call. Regression gate: pytest `TestRebuildIndex` now asserts both indexes exist post-rebuild (Phase 2 Copilot round 4)
- **validate_phase2**: Module-level hook resolution now short-circuits when `-h`/`--help` is present in `sys.argv`, so `--help` produces argparse usage output even when the plugin cache path cannot be located (Phase 2 Copilot round 4)

## [2.3.0] - 2026-04-16

### Added
- `/vault-stats` skill — vault health diagnostics and usage analytics showing signal coverage, access patterns, importance distribution, and top accessed notes; saves report to vault as `claude-stats` note for trend tracking
- **vault-index**: ACT-R access tracking — `access_log` table records every note read with context type (recall, search, ask, related) for activation-based ranking
- **vault-index**: `batch_activations()` computes ACT-R base-level activation (`ln(Σ t_i^(-0.5))`) for combined recency+frequency scoring
- **vault-index**: `importance` column on `notes` table — 1-10 write-time score extracted from Haiku/sub-agent summarization output
- **vault-index**: `detect_task_context()` — heuristic detection of debugging/standup/search/general from git branch and caller skill
- **vault-index**: Context-adaptive type scores — error-fix notes rank higher when debugging, session notes rank higher for standup

### Changed
- **vault-index**: Reranker upgraded from 5 to 7 signals — adds activation (0.20 weight) and importance (0.10 weight), rebalances existing signals
- **recall**: Sub-agent summarization prompt now includes importance scoring (1-10)

## [2.2.0] - 2026-04-14

### Changed
- **vault-index**: FTS5 search now uses AND-mode queries (both terms must appear) instead of OR-mode, with automatic OR fallback when AND returns zero results
- **vault-index**: BM25 column weighting — title matches rank 10x, tag matches 5x over body matches
- **vault-index**: New Python reranker scores results by term proximity (0.35), BM25 (0.25), note type (0.15), recency (0.15), and term density (0.10)
- **vault-index**: `notes` table now stores body text for reranker proximity scoring (auto-migrated on first run)

## [2.1.0] - 2026-04-13

### Added
- `/emerge` skill — cross-project pattern discovery across vault notes within configurable time window (7d/30d/90d/this week). Python-first pipeline with single AI sub-agent for synthesis. Surfaces technical patterns, process patterns, knowledge gaps, cross-project connections, and unnamed habits.
- `/standup deep` mode — evidence-based open-item consolidation. Collects all open items across projects, gathers completion evidence from git log, GitHub releases, changelogs, and FTS5 vault search, classifies items as COMPLETED/REDUNDANT/STALE/ACTIVE via AI sub-agent, suggests link/merge opportunities, detects orphaned notes, and cascades checkoffs vault-wide.
- `encoding-corruption` vault-doctor check — detects and repairs vault notes with invalid UTF-8 bytes that cause grep binary file handling
- `collect_vault_corpus()` and `upgrade_and_collect_corpus()` in obsidian_utils.py — single-pass vault scan for pattern analysis with unsummarized note upgrade
- `deep_analysis_pipeline()` and `build_deep_presentation()` in open_item_dedup.py — similarity pass, item dedup, evidence gathering via subprocess (git/gh), orphan detection
- `emerge_cli.py` and `deep_cli.py` — extracted CLI modules for skill orchestration
- 15-minute result caching for `/emerge` and `/standup deep` to avoid redundant runs
- Acted-on item tracking (24h TTL) to prevent re-recommending previously consolidated items
- Module-level compiled section-parsing regexes shared across vault functions
- SNIP_05 test: glob import validation for SKILL.md snippets
- `project-name-normalization` vault-doctor check — detects and auto-fixes underscored project names in frontmatter
- `_glob_project_jsonls()` helper — centralizes `~/.claude/projects/` globbing with underscore-to-hyphen fallback

### Fixed
- Python 3.9 compatibility: add `from __future__ import annotations` to `vault_index.py` and `obsidian_context_snapshot.py`
- Fix underscore-to-hyphen project path matching across session ID resolution functions
- Fix ambiguous hash instructions in 4 skills to prevent 3-char hash bug
- Normalize project names (underscore → hyphen) in session context and vault-doctor comparisons
- Atomic writes with path containment for all batch vault edit operations
- `errors='replace'` on all vault file reads to handle encoding corruption gracefully

## [2.0.1] - 2026-04-12

### Fixed
- Escape bash `[[` conditionals in raw conversation excerpts to prevent Obsidian from parsing them as wikilinks
- Restore vault-index features silently dropped during v2.0.0 release merge (README, skill files, import os fixes)

### Added
- `escape_wikilinks()` helper in `obsidian_utils.py`
- `spurious-wikilinks` vault-doctor check — detects and repairs unescaped `[[` in existing session notes

## [2.0.0] - 2026-04-12

### Security
- **CRITICAL:** Move all temp/cache files from `/tmp` to `~/.claude/obsidian-brain/` (0o700) — prevents symlink attacks (C1)
- **CRITICAL:** Remove `OBSIDIAN_BRAIN_BOOTSTRAP_PREFIX` env var override — prevents arbitrary file write (C2)
- **HIGH:** Add path traversal validation to `write_vault_note()` — blocks `../` escape from vault (H1)
- **HIGH:** Add `scrub_secrets()` — best-effort regex redaction of API keys, tokens, passwords in raw session notes (H2)
- **HIGH:** Add `log_raw_messages` config toggle — disable raw conversation logging entirely (H2)
- **HIGH:** Validate `transcript_path` stays inside `~/.claude/projects/` (H3)
- **HIGH:** Fix shell injection in `commit-preflight.sh` — pass path via `sys.argv` (H4)
- Change all file permissions from 0o644 to 0o600 for vault notes, DB, and config (M1, M2)
- Fix SKILL.md config output to newline-separated KEY=VALUE — supports vault paths with spaces (M3)
- Fix `vault-reindex` to use `sys.argv` instead of inline interpolation (M4)
- Replace `sed -i` in standup with atomic `flip_note_status()` (M5)
- Cap `sys.stdin.read()` to 1MB in all hook entry points (M6)
- Escape LIKE wildcards in vault_index tag queries (M7)
- Validate `find_transcript_jsonl` output stays inside projects dir (M8)
- Standardize JSON cascade checkoff calls to use stdin pattern (L1)

### Added
- `/vault-config` skill — interactive settings menu for toggling obsidian-brain configuration
- `scripts/test-security.sh` — automated security validation (27 checks), runs from `/dev-test install` and CI
- `security-tests` CI job — runs security checks on every PR
- Security Patterns section in CLAUDE.md
- `/compress <topic>` update mode — searches vault index for existing notes via FTS5 and offers to append a dated `## Update (YYYY-MM-DD)` section instead of creating duplicates
- New `last_updated` frontmatter field set on each append to existing insight/decision notes
- New topic tags from update content are appended without duplicating existing tags
- `enforce-pr-base-branch.py` PreToolUse hook — blocks pull request creation without `--base develop` on feature branches and verifies base branch before merge, preventing accidental merges to main
- `hooks/vault_index.py` — SQLite + FTS5 vault index with lazy mtime-based sync, layered ranking queries (backlinks → tags → FTS keywords), and sub-millisecond ad-hoc search
- `/vault-reindex` skill — full index rebuild for recovery, setup, and after bulk Obsidian edits
- `/obsidian-setup` Step 8.5 — bootstraps vault index on first setup and upgrades
- `/vault-search` FTS fast path — tries instant FTS5 search before falling back to Grep
- `/vault-ask` FTS pre-filter — reduces sub-agent file reads by pre-filtering with FTS5

### Changed
- Haiku summarization timeout bumped from 15s to 30s (retry escalation: 30s/60s). Empirical measurement showed ~9-10s CLI startup overhead, leaving insufficient time for generation at 15s.
- `upgrade_unsummarized_note()` timeout is now a passthrough to `generate_summary()` — single source of truth instead of duplicated defaults.
- `check_hook_status()` SID mismatch (common after reconnects) is now `ok=True`. Only warns when bootstrap file is missing or no session files are found.
- `/recall` hook-status messages reworded for end users: `[OK]` lines suppressed from output, `[WARN]` shows actionable guidance.
- README updated with vault-index architecture details and `/vault-reindex` skill.
- `build_context_brief()` insight loading now surfaces contextually relevant insights via layered ranking (backlinks → tags → FTS keywords) instead of most-recent-by-mtime. Falls back to the original file scan if the vault index is unavailable.
- `/vault-search` and `/vault-ask` FTS snippets now call `ensure_index()` before `search_vault()` so newly written notes are always picked up.
- FTS5 schema uses contentless tables (`content=''`) — orphaned FTS entries are filtered out by JOIN, no DELETE needed.
- `build_context_brief()` fallback narrowed from `except Exception` to `except (sqlite3.Error, OSError)` so programming bugs propagate instead of silently degrading to file scan.
- All `vault_index.py` public functions use `try/finally` for connection cleanup.
- Corrupt DB recovery now removes WAL/SHM sidecar files and logs to stderr.
- Layer query failures log to stderr instead of silently passing.

### Fixed
- FTS5 hyphen-as-NOT bug: `_sanitize_fts_query()` now replaces hyphens with spaces before tokenization. Previously, `"maintain-catalog"` was interpreted as `"maintain" NOT "catalog"` by FTS5's unicode61 tokenizer.
- Contentless FTS5 delete compatibility: `_upsert_note()` and `_delete_note()` no longer use `DELETE FROM notes_fts` (invalid for contentless tables). Orphaned FTS entries are filtered by the JOIN in all queries.
- `source_session` column mapping: `_upsert_note()` now correctly reads `parsed.get("source_session")` instead of `parsed.get("session_id")`.
- Missing `import os` in `/recall` Step 4 cascade checkoff inline Python snippet.
- `upgrade_note_with_summary()` now guarantees that a returned `Upgraded` status means the summary actually landed on disk. The rewritten tempfile is `fsync`'d before `os.replace()`, the parent directory is `fsync`'d after the rename (crash-durable rename), and the target file is re-read and verified before the function returns. Verification checks that `status: summarized` appears in the **YAML frontmatter block** (anchored to the start of the file via `re.match`, not a whole-file substring match — so a body that happens to mention the literal string or contains a Markdown `---` horizontal rule cannot false-positive) AND that the first real content line of the supplied summary is present in the **`## Summary` section** as its own stripped line (line-granularity, not substring match). Empty or heading-only Summary bodies are rejected upfront with `Failed: malformed summary`. Post-write mismatches return distinct `Failed: post-write verification — …` statuses (status not flipped, summary body missing, `## Summary` section not found, YAML frontmatter not found at start, post-write read failure) so callers (and `/recall`) can no longer be told "Upgraded" about a note that did not actually receive its summary.

## [1.9.0] - 2026-04-11

### Added
- `/standup` Step 14: cascade completed open items across vault notes using `batch_cascade_checkoff()` — when items are marked done in standup, all matching `- [ ]` entries in other session notes are automatically checked off
- `/vault-doctor` skill — diagnostic and repair tool for the Obsidian vault with a pluggable check-module registry. Ships with a `source-sessions` check that scans the last 7 days of insight/decision/error-fix/retro notes, detects stale `source_session` backlinks by matching note mtimes against JSONL session windows, and atomically rewrites only the affected frontmatter fields under `--apply` with per-project confirmation and automatic backups.
- `~/.claude/obsidian-brain-hook.log` — rolling audit log of SessionStart hook invocations with the authoritative session id, rotated at 100 KB.
- `scripts/verify-hooks.sh` — manual diagnostic that simulates a SessionStart hook invocation and confirms the bootstrap and log were written.
- `/recall` brief now leads with a `[OK]`/`[WARN]` SessionStart hook status line.

### Fixed
- Session hint hook now writes the authoritative session id to the bootstrap cache, fixing stale `source_session` backlinks that pointed at previous sessions when `/compress`, `/decide`, `/error-log`, or `/retro` were run in a second-or-later session of a given project.
- `_get_session_id_fast()` now detects a new session by comparing the newest JSONL's basename against the cached sid (with a same-second mtime tie-breaker that trusts the hook-written bootstrap), invalidating the cache when a different session has become authoritative. This is defense-in-depth against the rare case where the SessionStart hook did not fire.
- `_get_session_id_fast()` slow path is now strictly read-only so it can no longer clobber the SessionStart hook's authoritative bootstrap write during the hook's own invocation, preventing a race where Claude Code fires SessionStart before flushing the new session's JSONL to disk.
- `apply()` preserves the patched note's original mtime so `/vault-doctor` re-runs never re-flag their own fixes.
- `apply()` sanitizes the project name via `_safe_project_slug()` before joining it onto the backup path, preventing path-traversal via frontmatter-provided project values.
- `apply()` separates backup and rewrite error reporting so failures are distinguishable and the backup path is preserved for recovery even when the rewrite stage fails.
- `_jsonl_dir_for_project()` uses `glob.escape()` on project names and `_safe_mtime()` wrapper to tolerate transient filesystem races between glob and stat.
- `_find_matching_session()` iterates deterministically with a same-mtime tiebreaker so window-boundary cases pick the most recently started session reproducibly.
- `_write_bootstrap_atomic()` forces absolute paths before computing the temp directory, preventing `EXDEV` errors when `OBSIDIAN_BRAIN_BOOTSTRAP_PREFIX` is relative and `/tmp` is a separate filesystem.
- `_write_bootstrap_atomic()` and `apply()` clean up orphaned temp files in `finally` blocks if `os.replace()` did not consume them.
- `vault_doctor_checks` registry now catches per-module import errors and logs them to stderr rather than aborting the whole dispatcher, keeping the check system pluggable.
- `check_hook_status()` now uses a bootstrap-independent slow-path helper so the health check is not circular and correctly flags stale bootstraps as `[WARN]`.
- `/recall` brief hook status line handles the "no JSONLs discoverable" case with a clear `could not determine current session id from JSONLs` message.
- `verify-hooks.sh` derives `PROJECT` via `get_project_name()` so it agrees with the Python hook's project-name resolution, and passes `cwd` to its Python helper via `sys.argv` rather than string interpolation (quote-safe for paths containing special characters).
- `_cleanup_session_cache()` now runs in a `try/finally` at the top level of the SessionEnd hook, so orphaned cache files are cleaned up on every SessionEnd path (threshold skips, missing config, auto-log disabled, errors) — not just the happy path.
- Several test suites migrated from `time.mktime()` (local time) to `calendar.timegm()` (UTC) to prevent CI flakiness on non-UTC runners.
- `vault_doctor.py` `_load_config()` respects strict `CLI > env > config file > default` precedence for every field; previously environment-set folder names could be overridden by config file values.
- `vault_doctor.py` validates `--days` as positive before running any check.
- `apply()` now writes backups to `<backup_root>/<project>/<folder>/<basename>` to prevent basename collisions across insight-type folders.
- All generated output uses ASCII glyphs (`[OK]`/`[WARN]`/`[FAIL]`) instead of Unicode emoji, matching the project-wide no-emoji convention.

### Changed
- SessionEnd hook now cleans up the per-session disk cache file `/tmp/.obsidian-brain-cache-<sid>.json` to prevent `/tmp` accumulation over time.

## [1.8.2] - 2026-04-10

### Added
- `/recall` session history table now includes a Duration column (e.g. `1h 20m`, `27m`)
- `/recall` session history table now includes a `#` column for easy session selection
- `/recall` skill now instructs Claude to paraphrase session titles into concise one-liners
- `_safe_sort_key()` helper for graceful handling of broken symlinks during session scan
- 12 new tests: sort order, duration formatting, session number, stat failure, 60-min boundary, cache glob regression guards

### Fixed
- `/recall` session history now sorts by date descending then mtime descending, fixing random hash-based ordering for same-day sessions
- All 12 skills now resolve hooks from plugin cache, fixing `ModuleNotFoundError` when running from non-obsidian-brain project directories
- Session scan now filters to `.md` files before sorting, avoiding unnecessary `stat()` calls on non-session files

## [1.8.1] - 2026-04-10

### Added
- pytest test suite with 101 tests across 7 test files covering all Python hook modules
- SKILL.md `python3 -c` syntax validation via parameterized compile() tests (26 snippets)
- 90% line coverage enforcement via pytest-cov (98% achieved on measured modules)
- `python-tests` CI job in GitHub Actions for all PRs
- pytest + coverage detection in `commit-preflight.sh` test section
- `setup.cfg` with pytest and coverage configuration

### Fixed
- `skills/obsidian-setup/SKILL.md` f-string escape bug caught by snippet validator

### Changed
- Upgraded GitHub Actions from v4/v5 to v6 (Node.js 24 compatible)
- `commit-preflight.sh` now fails if `tests/` directory exists but pytest is not installed

## [1.8.0] - 2026-04-10

### Added
- User-visible task manifest during `/recall` showing progress across all
  steps with per-note granularity during summarization.
- `prepare_summary_input()` helper in `obsidian_utils.py` for conditional
  JSONL-to-temp-file extraction.
- `/dev-test` skill and `test-dev-skill.sh` script for swapping the installed
  plugin cache with the repo working copy during local testing.

### Changed
- `/recall` Step 2 now uses parallel sub-agents as the default summarization
  strategy when 2+ unsummarized notes are found, with conditional JSONL
  transcript extraction for truncated sessions. Sub-agent summaries written to
  temp files (no heredoc pass-through). Per-note sub-tasks skipped when N>5.
  Single-note case unchanged (Haiku pipeline + sub-agent fallback).
- `/recall` Step 3 context building done by pure Python `build_context_brief()`
  function (<3s, direct file I/O) instead of sub-agent (~145s, 70 Read calls).
  Unsummarized note detection also moved to Python `find_unsummarized_notes()`.
  Total `/recall` reduced from ~4 min to ~1.3 min.
- `/recall` steps reduced from 8 to 4. Config + project merged into single call.
  Task manifest collapsed from 6 to 4 top-level tasks.
- Session history table titles use first sentence of `## Summary` instead of
  generic H1 heading, making each row descriptive of what happened.

### Fixed
- f-string SyntaxError in all 10 skill templates — `python3 -c '...'` one-liners
  used f-strings with dict key access (`c[\"vault_path\"]`) which breaks inside
  Bash single-quoted strings. Replaced with string concatenation across config
  load (10 skills) and session context (4 skills).
- `/recall` and `/standup` grep for unsummarized notes matched tool-usage logs
  in conversation excerpts, causing false positives and unnecessary re-summarization.
  Changed from body text pattern (`"AI summary unavailable"`) to frontmatter
  field (`^status: auto-logged`).
- Legacy notes (119 across all projects) had `status: auto-logged` but already
  contained real AI summaries from old SessionEnd inline-summarization path.
  Added defense-in-depth guard to `/recall` Step 2 and `/standup` Step 5 that
  checks for `## Summary` before re-summarizing and auto-fixes stale status fields.
- Stale metadata cache caused `find_unsummarized_notes()` to skip genuinely
  unsummarized notes and re-summarize already-upgraded ones. Function now reads
  frontmatter directly from disk, and `upgrade_note_with_summary()` invalidates
  cache entries after status changes.

## [1.7.2] - 2026-04-09

### Fixed
- **Haiku summarization timeout retry** — `generate_summary()` now retries once at 2x timeout (15s → 30s) before giving up, reducing unnecessary sub-agent fallbacks

## [1.7.1] - 2026-04-09

### Added
- **Sub-agent summary fallback** — when Haiku API times out during `/recall` Step 3, parallel sub-agents (inheriting parent model) produce structured summaries. New `upgrade_note_with_summary()` function accepts pre-generated summary text and handles the pipeline finish (frontmatter flip, dedup, atomic write). Minimal overhead when Haiku succeeds.

## [1.7.0] - 2026-04-09

### Added
- **Open item deduplication** — new `hooks/open_item_dedup.py` module with hybrid matching (distinctive tokens + fuzzy overlap) prevents duplicate open items across session notes
  - Creation-time prevention: `generate_summary()` appends existing items to Haiku prompt + post-generation dedup pass strips duplicates before disk write
  - Check-off cascading: checking off an item auto-checks matching duplicates in older notes (high confidence) or suggests them (fuzzy confidence)
  - `/recall` Step 3: `dedup_note_open_items()` runs after note upgrade (zero items loaded into model context)
  - `/recall` Step 7.5 + `/check-items`: `batch_cascade_checkoff()` handles cascade in a single Python call
- **Session-scoped cache** — file-based cache at `/tmp/.obsidian-brain-cache-{session_id}.json` avoids repeated vault scans across skills within one session (~650 tokens + ~190ms saved for 5 skills)
- **Shared helpers** — `load_config()` (cache-backed), `get_session_context()`, `read_note_metadata()` consolidate redundant config/session/frontmatter parsing across skills
- **`upgrade_unsummarized_note()`** — single Python call replaces the multi-step JSONL parse → summarize → write → dedup pipeline in `/recall` Step 3 (~1,000 tokens saved per note upgrade)
- **`match_items_against_evidence()`** — moves completion detection matching from model context to Python (~400-600 tokens saved per `/recall` invocation)
- **Config/session consolidation** — all 12 skills now use `load_config()` shared helper instead of inline `cat`/`Read` config parsing (~2,470 tokens saved per multi-skill session)
- **`/standup` always parallelizes** unsummarized note upgrades via `upgrade_unsummarized_note()` helper (60-80% time reduction)

### Fixed
- Defensive initialization of `parsed` variable in `upgrade_unsummarized_note()` to prevent potential `NameError` on future refactors
- **`/recall` Step 8 UX** — replaced vague "Want me to load this context?" with an explicit load manifest showing which sessions and insights are in the conversation, and made the session history table actionable for loading additional sessions

## [1.6.2] - 2026-04-07

### Added
- **`commit-preflight.sh` plugin manifest version sync check** — Preflight now parses `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` and fails the commit if the registry pointer version drifts from the actual plugin version. Prevents the class of bug where the marketplace listing advertises a stale version to users.
- **`bump-version.sh` auto-updates `marketplace.json`** — Running `./scripts/bump-version.sh <type>` now updates every matching plugin entry in `marketplace.json` alongside `plugin.json`, so release-branch bumps stay in lockstep by default.

### Fixed
- Bumped `.claude-plugin/marketplace.json` plugin version from stale `1.1.0` to `1.6.1` so the marketplace registry pointer matches the actually published plugin version.

### Changed
- **`/recall` Step 3 hardened against skipping** — Added an explicit mandatory-step callout, large-note chunked-read handling (Read token-limit errors are not a skip signal), missing-JSONL fallback clarification, and a required one-line status emission (`Step 3: processing N unsummarized note(s)` / `no unsummarized notes`) so the upgrade decision is auditable in the tool trace. Fixes the failure mode where `/recall` silently skipped unsummarized notes under execution momentum.

## [1.6.1] - 2026-04-07

### Fixed
- `/recall` now produces accurate summaries for long sessions. Previously the raw session note was truncated to ~40 conversation turns and `/recall` summarized only that slice; now `/recall` deterministically locates the original Claude Code transcript JSONL by `session_id` and re-parses it when it has more data than the raw note. Very large transcripts (>5 MB) are sliced into head+tail halves with an explicit warning surfaced to the user. Falls back gracefully when the JSONL is no longer on disk.

### Changed
- Raw session notes now keep more context standalone — `build_raw_fallback()` caps bumped: 120 conversation turns (was 40), 1200 chars per message (was 600), 80 tool uses (was 30), 60 files touched (was 30), 30 errors (was 15). Typical sessions remain self-contained without needing the JSONL fallback.

## [1.6.0] - 2026-04-07

### Added

- **Cross-project open items dashboard** — New `claude-dashboards/open-items.md` Dataview dashboard installed by `/obsidian-setup`. Shows all unchecked `- [ ]` items from session notes' `## Open Questions / Next Steps` sections, grouped by project, with separate "Recent (7d)" and "Items from sessions 30-90 days ago" views plus stats. Scoped to the last 90 days for performance.
- **`/check-items` skill** — Cross-project sweep that scans all session notes for unchecked items (unbounded), gathers evidence from sessions in the last 14 days per project, proposes matches via substring + completion-phrase heuristics, and flips confirmed items from `- [ ]` to `- [x]` in the source notes. The 14-day window applies only to the evidence pool used for matching; open-item collection itself is unbounded. Configurable via `/check-items <Nd>`.
- **`/recall` auto-detect** — `/recall` now detects open items from the current project that may have been completed in the most recent loaded session. Proposes candidates with evidence snippets; user confirms before any edits.
- **`/standup` Closed This Period section** — Standup notes now include a section listing items checked off during the standup window, grouped by project. Detected via file modification time. Omitted if zero items closed.

### Changed

- **`obsidian-setup` skill** — Now installs the new `open-items.md` dashboard. Skill version bumped to 1.3.0.
- **`recall` skill** — New Step 7.5 detects completed open items. Skill version bumped to 1.1.0.
- **`standup` skill** — New "Closed This Period" section. Skill version bumped to 1.1.0.

## [1.5.3] - 2026-04-06

### Added

- **Permission pre-flight check in `/obsidian-setup`** — Detects restrictive Claude Code permission modes via canary write before attempting out-of-workspace writes. Presents three options: switch mode (`Shift+Tab`), whitelist paths in settings, or continue manually.
- **Vault path canary** — Tests vault writability in `/obsidian-setup` Step 5 before creating folders, catching cases where `~/.claude/` is writable but the vault is not.
- **README troubleshooting section** — Covers silent setup failures (permission modes), `python` not found on macOS, and vault path not writable.

### Changed

- **Auto-logging description** — README now accurately reflects deferred summarization (removed reference to in-hook `claude -p` subprocess).

## [1.5.2] - 2026-04-06

### Added

- **Standup highlights summary** — `/standup` now generates a highlights summary and key open items section at the top of standup notes for quick scanning.

### Fixed

- **Python 3.9 compatibility** — Added `from __future__ import annotations` to `obsidian_utils.py` so `X | None` type hints (PEP 604) work on macOS system Python 3.9.6. Previously caused `TypeError` at import time, breaking all hooks.
- **SessionEnd hook cancellation** — Removed in-hook AI summarization (`claude -p` subprocess) from `obsidian_session_log.py`. SessionEnd hooks are fire-and-forget; the slow subprocess was killed when Claude Code's process tree exited. Summarization is now fully deferred to `/recall`.

## [1.5.1] - 2026-04-05

### Fixed

- **Hookify nudge scope** — `/obsidian-setup` now writes the claudeception-compress nudge rule to `~/.claude/` (global) instead of the project's `.claude/` directory, so the nudge triggers in any project where claudeception runs. Also fixes the existence check to look for the `.local.md` rule file instead of grepping `settings.json`.
- **Changelog PR hook now detects stale entries** — The `update-changelog-before-pr` hook now diffs CHANGELOG.md against the base branch instead of just checking for any entries under `[Unreleased]`. Stale entries from previous releases no longer cause false passes.

## [1.5.0] - 2026-04-05

### Added

- **Claudeception-to-Compress bridge** — `/compress` now detects `/claudeception` output in the conversation and surfaces extracted skills/knowledge as top-priority insight candidates. Uses layered detection: high-confidence structured markers (skill validator output, skill file paths) first, broad phrase scanning as fallback. Claudeception candidates are labeled `[from claudeception]` or `[possibly from claudeception]` and included when the user selects `all`.
- **Hookify nudge via `/obsidian-setup`** — New idempotent step in `/obsidian-setup` configures a hookify nudge that reminds users to run `/compress` after claudeception produces output. Existing users can re-run `/obsidian-setup` to pick up the nudge.

### Changed

- **`/obsidian-setup` is now idempotent** — Detects existing installations and offers upgrade/reconfigure/cancel. In upgrade mode, preserves existing config and user-customized dashboards while adding new features (dashboards, hookify nudges). Safe to re-run anytime.

## [1.4.0] - 2026-04-05

### Added

- `/standup` skill: daily/weekly summary generation across projects with AI summarization, context-shield deep reads, and source note backlinks
- `/link` skill: cross-reference related notes with bidirectional wikilinks and auto-suggestion
- `/retro` skill: honest session retrospective for meta-learning with session backlinks
- `/vault-ask` skill: synthesize answers from vault knowledge with source citations and relevance ranking
- Learning Velocity dashboard: topic frequency from curated insights, retrospective history, error patterns
- Decision Timeline dashboard: chronological decision tracking with active/superseded status views

## [1.3.0] - 2026-04-05

### Fixed

- **SessionStart hook output** — Added required `hookEventName: "SessionStart"` field to `obsidian_session_hint.py` JSON output. Claude Code silently drops `hookSpecificOutput` JSON that omits this field, causing the session hint to never appear at startup.
- **SessionStart hook matcher** — Added explicit `matcher` field to `hooks.json` SessionStart entry for clarity (optional but documents intent).

### Added

- **Session backlinks in insights** — All insight-producing skills (`/compress`, `/error-log`, `/decide`) now derive the current session ID and include a `source_session_note` wikilink in frontmatter, enabling bidirectional navigation between session notes and insights in Obsidian's graph view.
- **Session ID derivation** — Skills now detect the active session by finding the most recently modified `.jsonl` file in the Claude Code project directory, replacing the broken `$CLAUDE_SESSION_ID` environment variable approach.

### Changed

- **Templates updated** — `insight.md`, `error-fix.md`, and `decision.md` templates now include `source_session` and `source_session_note` frontmatter fields.

## [1.2.0] - 2026-04-04

### Added

- **Git Flow enforcement** — Claude Code hooks to prevent direct push to main/develop, validate branch naming (`feature/*`, `release/*`, `hotfix/*`), and require preflight checks before commit
- **Commit preflight system** — `scripts/commit-preflight.sh` with secret scanning, one-time token mechanism, and skip-tests escape hatch
- **Release pipeline** — `scripts/bump-version.sh` (targets `.claude-plugin/plugin.json`) and `scripts/git-flow-finish.sh` for automated release/hotfix completion
- **GitHub Actions CI** — Secret scan on all PRs, changelog check on PRs to main, release verification on main push
- **Branch protection** — Status check enforcement on main and develop (admin-lenient)

### Documentation

- **README vault details** — Added detailed descriptions of each `claude-*` folder, how content gets loaded into sessions, and a context loading summary table

## [1.1.0] - 2026-04-04

### Improved

- **Richer session notes** — raw fallback notes now include assistant messages, tool usage details (commands run, files edited, searches performed), and interleaved conversation (up to 40 turns). System noise (task notifications, skill loading) is filtered out.
- **Better `/recall` summaries** — summarization prompt now demands specific technical details: file paths, function names, decision rationale, error root causes with fixes, and concrete next steps.

### Fixed

- Raw notes previously only captured user messages (15 max). Now captures full conversation with both sides for `/recall` to produce high-quality summaries.

## [1.0.0] - 2026-04-04

### Added

- **Auto-logging** — SessionEnd hook automatically writes structured session notes to your Obsidian vault with YAML frontmatter, tags, and metadata. Uses a write-first pattern: raw note is always saved, AI summary attempted as best-effort upgrade.
- **Context hints** — SessionStart hook injects a one-line summary of the last session for the current project, giving you immediate continuity.
- **Context snapshots** — PreCompact hook saves a snapshot of your current context before compression or clear, preserving context that would otherwise be lost.
- **`/obsidian-setup`** — Interactive first-run configuration. Sets vault path, creates folders, copies Dataview dashboards, writes config.
- **`/compress`** — Curate and save specific insights from the current session. Suggests candidates or accepts a topic argument. Interactive preview with tag editing.
- **`/recall`** — Load project-scoped context from vault history. Finds your last session, open items, and all curated insights. Includes deferred summarization — upgrades raw notes with AI summaries on demand.
- **`/vault-search`** — Search across all sessions and insights by keyword, tag, or structured queries (e.g., `project:api-service type:decision`).
- **`/decide`** — Log architectural decisions in ADR-lite format: Context, Options, Decision, Rationale, Consequences.
- **`/error-log`** — Capture errors with root cause, fix, and prevention steps for future reference.
- **`/vault-import`** — Backfill historical sessions from CC conversation history. Uses `/conversation-search` for discovery and parallel `/context-shield` sub-agents for processing.
- **Dataview dashboards** — 3 ready-to-use dashboards: Sessions Overview, Project Index, Weekly Review.
- **Note templates** — 6 templates for all note types: session, insight, decision, error-fix, snapshot, imported session.
- **Plugin distribution** — Installable via Claude Code plugin system from GitHub.
