# Plan — #269: note persistence breaks when the Write is delegated to a context-blind subagent

**Issue:** #269 (enhancement, milestone v3.3). `/retro` (Step 7) and `/compress` (Step 8, Step 4A-update) author an evidentiary note from the orchestrator's live conversation buffer, then persist it with a **Write/Edit tool call**. Where writes are routed to a helper subagent (e.g. the `cc-token-router` PreToolUse hook), that helper sees only the write instructions — never the session that produced them — so it cannot ground the note's claims and may refuse to persist it as "fabricated". Observed 2026-07-24: a correct, orchestrator-authored retro could not be saved through the skill path at all.

## Acceptance (verbatim from the issue)

- `/retro` and `/compress` persist their note through a deterministic path that does not depend on a context-blind agent choosing to Write.
- A write-gating environment no longer blocks retro/insight persistence.

The issue's "companion fix" (a vault-path carve-out in `cc-token-router`) is a **different repo** and is explicitly out of scope here.

## Design decisions (grounded in code, reviewed before build)

1. **Persistence becomes a Bash-invoked Python step, not a tool-call decision.** This is the same shape every other skill/hook interaction already uses (`python3 -c '...'` with the plugin-cache `sys.path` glob), so it inherits an execution path that is not a semantic "should I write this?" judgment. Empirically validated: vault insight `2026-07-05-router-blocked-writes-python-anchor-patching-c53a` records that Bash-heredoc Python writes pass the router untouched across ~25 patches with zero silent corruptions.
2. **Content travels on stdin via a quoted heredoc**, never as a shell argument and never interpolated into the Python source — matching the project's "JSON via stdin, not shell args" and "no path interpolation in `python3 -c`" security patterns. A quoted delimiter (`<<'OB_NOTE_EOF'`) is mandatory: note bodies contain `$`, backticks, and code fences that unquoted heredocs would expand.
3. **New module `hooks/note_writer.py`, not more inline Python in the SKILLs.** Eight skills need the same block; a named CLI keeps the snippet to ~6 lines each and makes the logic testable. Follows the existing `check_items_cli.py` entry-point convention: `main()`, positional argv for paths, `sys.stdin.read(STDIN_CAP_BYTES)` for content, exit 0/2.
4. **Reuse `write_vault_note()` for the create path** rather than reimplementing. It already does temp-file + rename, `mkdir -p`, `resolve()`/`is_relative_to()` traversal containment, and `chmod 0o600` — so the skills' `mkdir -p` and `chmod` steps both disappear.
5. **Note permissions change 0644 → 0600.** This is deliberate and is a *fix*, not a regression: the repo's own security pattern mandates 0o600 for files containing user data, `/vault-stats` already chmods 600, and hook-written session notes are already 0600. The live vault currently holds a 0600/0644 split (939 insights at 0644) purely because the skill path diverged from the hook path. Only newly written notes are affected; Obsidian reads owner-readable files fine.
6. **The append path updates body and frontmatter in ONE atomic write.** Today `/compress` Step 4A-update does three separate Edit calls (append section, set `last_updated`, merge tags) — three independent failure points that can leave a half-updated note. Collapsing them into one temp-file+rename removes that window and removes the "Verify by re-Reading" step the skill currently needs.
7. **Insertion-point logic is lifted from existing code, not invented.** `obsidian_utils.upgrade_note_with_summary` already enumerates the trailing audit sections (`## Tool Usage`, `## Conversation (raw)`, `## Session Metadata`, `## Files Touched`); the append command reuses that list plus `_(Summary source: ...)_` as named in the compress SKILL, so prose and code cannot drift.
8. **Apply the fix to every Write-tool note-persistence site, not just the two named.** `grep` finds the identical pattern in `decide`, `error-log`, `standup`, `vault-stats`, and `vault-import`. Same defect, same one-block substitution; fixing two of seven would knowingly ship a half fix. `check-items`' checkbox flip and `link`'s `## Related` append are a *different* shape (surgical in-place edits with uniqueness anchors, not evidentiary-note persistence) and stay out of scope.

## Tasks

### Task 1 — `note_writer.py write` (create path)
Files: `hooks/note_writer.py` (new), `tests/test_note_writer.py` (new).
- `main()` dispatching on `argv[1]`; usage/exit 2 on unknown command or arity, mirroring `check_items_cli.main()`.
- `write <vault_path> <folder> <filename>`: read `sys.stdin.read(1_000_000)`, delegate to `obsidian_utils.write_vault_note`, print `OK: <abs path>` to stdout on success or `ERROR: <msg>` and exit 1 on failure. Never partially write.
- Tests fail-first: byte-fidelity of content that opens with `---` frontmatter (guards the leading-fence-eaten class, cf. `feedback_subagent_verbatim_write_leading_fence`); content containing `$VAR`, backticks, and a fenced code block survives verbatim; missing target folder is created; mode is exactly 0o600; `..` traversal in `filename` is blocked with no file created anywhere; oversize stdin is truncated at the cap rather than raising.

### Task 2 — `note_writer.py append-update` (update path)
Files: `hooks/note_writer.py`, `tests/test_note_writer.py`.
- `append-update <vault_path> <note_path> [--last-updated YYYY-MM-DD] [--add-tags a,b,c]`: read the update section from stdin, insert it immediately before the first trailing metadata section, else at EOF; apply frontmatter mutations; write once via temp file + rename with 0o600.
- Trailing-section markers reuse the `upgrade_note_with_summary` list plus `_(Summary source: ...)_`.
- `last_updated`: replace in place if present, else insert directly after the `date:` line. `date`, `source_session`, `source_session_note`, `type` are never touched.
- Tags: append only tags not already present, at the end of the `tags:` block; preserve list style and indentation.
- Path containment against `vault_path` before any filesystem side effect.
- Tests fail-first: insertion lands before each trailing marker variant *and* at EOF when none is present; `last_updated` add-vs-replace both covered; duplicate tag is not re-added while a genuinely new one is; a note with no `tags:` block does not crash; malformed/absent frontmatter fails loudly (non-zero exit, file unchanged on disk) instead of half-writing; original body content is byte-identical outside the inserted section.

### Task 3 — Skill rewrites
Files: `skills/retro/SKILL.md` (Step 7), `skills/compress/SKILL.md` (Step 8 and Step 4A-update.4/.5), `skills/decide/SKILL.md`, `skills/error-log/SKILL.md`, `skills/standup/SKILL.md`, `skills/vault-stats/SKILL.md`, `skills/vault-import/SKILL.md`.
- Replace each "use the **Write** tool … then `chmod`" block with the heredoc CLI call. Drop the now-redundant `mkdir -p` and `chmod`.
- Compress 4A-update: collapse .4 (body Edit) + .5 (frontmatter Edits) into one `append-update` call; drop the "re-Read to verify" step, which the atomic write plus non-zero exit makes redundant.
- Retro Step 7 keeps the classification-gate arming call **after** the write, unchanged and in the same order.
- State the quoted-delimiter requirement inline in each block so a future edit cannot drop the quotes.

### Task 4 — docs + architecture + changelog
- `docs/architecture/architecture.json`: add `note_writer` as a support/CLI component with its real file path and the skill→CLI→vault flow; keep `lastUpdated` current and `version` in sync with `plugin.json`. Re-render via the `architecture-page` script and pass `smoke-test.sh` (`[main]` and `[sparse]`).
- `CHANGELOG.md` `[Unreleased]` — Fixed entry for #269, calling out the 0644 → 0600 permission change on newly written notes.

## Out of scope / deferred
- The `cc-token-router` vault-path carve-out (companion fix, different repo).
- `check-items`' checkbox flip and `link`'s `## Related` append — different edit shape, not evidentiary-note persistence.
- Backfilling permissions on the 939 existing 0644 notes — no behavior depends on it; would be a separate `/vault-doctor` repair if ever wanted.
- Applying `scrub_secrets()` to skill-authored note bodies — these are model-authored summaries, not raw user messages; changing it here would be an unrelated behavior change.

## Verification
- `python3 -m pytest tests/ -q` green.
- `./scripts/commit-preflight.sh` before every commit.
- Dogfood: run the new `write` and `append-update` commands against a **scratch vault directory**, never the live vault; confirm the live vault is untouched.
- Phase 6 `/deep-review` to zero actionable, then CI green-gate.
