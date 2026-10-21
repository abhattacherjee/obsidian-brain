# #438: vault-doctor root causes

**Goal:** stop `/vault-doctor` reporting sessions that are still running, stop snapshots writing spurious wikilinks and broken backlinks, and keep wiki `orphan` rows out of the default sweep.

## Task 1: skip sessions that are still running

Files: `scripts/vault_doctor_checks/__init__.py`, `source_sessions.py`, `snapshot_integrity.py`, `snapshot_migration.py`, tests in `tests/test_vault_doctor_live_sessions.py`.

1. Add `session_is_live(sid, jsonl_path=None, now=None)` to `scripts/vault_doctor_checks/__init__.py` (shared types already live there; a helper module in the package would trip the registry's "no check interface" warning).
   - Live means: the transcript `~/.claude/projects/*/<sid>.jsonl` was modified within `LIVE_WINDOW_SECONDS` (3600), and `~/.claude/obsidian-brain-hook.log` (and `.log.1`) has no `SessionEnd` line for `sid=<first 8 chars>`.
   - No transcript, an unreadable mtime, or an empty sid: not live (report as before).
   - An unreadable hook log counts as "no SessionEnd seen" (the mtime bound still applies).
2. `source_sessions`: before emitting `missing-session-note`, skip when the session is live.
3. `snapshot_integrity`: skip `snapshot-orphan` (missing parent) and `session-snapshot-list-missing` for a live session. `-stale` is about deleted snapshots, not liveness, so it is unchanged.
4. `snapshot_migration`: skip `session-missing-snapshots-list` for a live session.

## Task 2: escape wikilinks in snapshots

File: `hooks/obsidian_context_snapshot.py`, tests in `tests/test_context_snapshot.py` or a new file.

1. Apply `escape_wikilinks()` to every section built from message text: recent user messages, key context, errors and the raw tail. Leave frontmatter alone.

## Task 3: snapshot backlink uses the first-seen date

File: `hooks/obsidian_context_snapshot.py`.

1. `_build_snapshot_note()` names the parent with `_first_seen_date(session_id)`, the same as SessionEnd. The snapshot's own filename keeps today's date.

## Task 4: wiki `orphan` only when the check is named

Files: `scripts/vault_doctor.py`, `scripts/vault_doctor_checks/wiki_pages.py`.

1. A check module may set `SCAN_EXPLICIT = True`; the dispatcher then passes `explicit=<the check was named with --check>`.
2. `wiki_pages.scan(..., explicit=False)` emits `orphan` rows only when `explicit` is true.

## Failure modes

1. **Fail-open paths:** a liveness check that errors must not hide a real gap. No transcript, bad mtime, empty sid: not live. A SessionEnd line hides nothing for an ended session. Tests: `test_live_session_skipped`, `test_ended_session_still_reported`, `test_old_transcript_still_reported`, `test_missing_transcript_not_live`.
2. **Exit codes:** no change; the doctor still exits 1 when other issues exist.
3. **Date boundaries:** Task 3 is the midnight case. Test: a session first seen the day before gets a backlink with that date.
4. **Doc counts:** CHANGELOG states no counts beyond the issue's.
5. **Broken Markdown:** CHANGELOG entry inside `[Unreleased]`.
6. **Unrelated changes:** none expected.
7. **Inputs:** the transcript mtime and the hook log (current and `.1`). The log rotates at 100 KB, so a SessionEnd may have rotated out; then only the 60-minute mtime bound applies, which still ends the skip after an hour. Test: SessionEnd only in `.log.1` counts.

**Bypass routes (the skip is a guard on reporting):** the cheapest way a real gap hides is a stale transcript touched recently by something else. Bounded to 60 minutes per touch; accepted. A sid prefix match on 8 chars could match another session's SessionEnd: match the 8-char form the log writes, which is the same prefix the doctor rows show; collisions inside one hour are negligible and only un-hide.
