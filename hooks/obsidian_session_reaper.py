#!/usr/bin/env python3
"""
obsidian_session_reaper.py -- SessionStart orphan-session reaper for obsidian-brain plugin.

Phase 3 of issue #100 / issue #125.

Recovers session notes that were never written because the SessionEnd hook did not
fire (SIGKILL, worktree teardown, terminal close mid-dispatch).  Called from
obsidian_session_hint.py once per SessionStart after the existing bootstrap-write
block.

Module dependency chain (no cycles):
    obsidian_session_hint  ──▶  obsidian_session_reaper  ──▶  obsidian_session_log
                           └──▶  obsidian_utils                       ──▶  obsidian_utils

Always exits cleanly — never raises; all failures logged to obsidian-brain-hook.log.
Python stdlib only (no pip dependencies).
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Ensure hooks/ is importable when running from the repo root.
_HOOKS_DIR = str(Path(__file__).parent)
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)


# ---------------------------------------------------------------------------
# ReaperOutcome dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReaperOutcome:
    """Immutable summary returned by reap_orphaned_sessions().

    Fields
    ------
    reaped : int
        Number of orphaned sessions successfully written to the vault.
    skipped_below_threshold : int
        Sessions whose transcript did not meet the configured msg/duration
        thresholds — same logic as live SessionEnd.
    skipped_already_written : int
        Sessions whose vault note already existed (idempotency check).
    skipped_permission_blocked : bool
        True if the vault-write canary detected a restrictive permission mode
        (Plan mode, restricted env), blocking further processing.  Watermark NOT advanced.
    timeout : bool
        True if the reaper hit the wall-clock budget (default 5 s) before
        processing all candidate JSONLs.  The watermark is advanced to the last
        successfully-processed entry so the next SessionStart resumes.
    wall_ms : float
        Total elapsed wall-clock time in milliseconds for this reaper run.
    """

    reaped: int
    skipped_below_threshold: int
    skipped_already_written: int
    skipped_permission_blocked: bool
    timeout: bool
    wall_ms: float
    # Registered recovery does not expose counts; its actual status stays distinct.
    recovery_status: str = None


# ---------------------------------------------------------------------------
# Watermark helpers (Task 6)
# ---------------------------------------------------------------------------


def _read_watermark(path: Path) -> int:
    """Returns epoch seconds; 0 if missing or corrupt."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _write_watermark_atomic(path: Path, value: int) -> None:
    """Atomic write via tempfile + rename; ensures parent dir exists with 0o700."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=".watermark.")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(int(value)))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Permission canary (Task 7)
# ---------------------------------------------------------------------------


def _permission_canary(vault_path: str) -> bool:
    """Touch a 0-byte sentinel inside vault root. True if writable.

    Run once per reaper invocation, only when about to write the first
    reconstructed note. Avoids touching the vault on no-orphan startups.
    """
    try:
        canary = Path(vault_path) / ".obsidian-brain-canary"
        canary.touch()
        canary.unlink()
        return True
    except (PermissionError, OSError):
        return False


# ---------------------------------------------------------------------------
# Helper: resolve project JSONL directory
# ---------------------------------------------------------------------------


def _resolve_project_jsonl_dir(project: str) -> Path:
    """Named legacy Claude transcript lookup, outside native registered recovery."""
    from runtime_adapters.claude import legacy_project_transcript_dir
    return legacy_project_transcript_dir(project)


# ---------------------------------------------------------------------------
# Helper: build set of already-written SIDs (8-char prefix) from vault
# ---------------------------------------------------------------------------


def _build_existing_sid_set(vault_path: str, sessions_folder: str, project: str) -> set:
    """Set of full SIDs for which a ``claude-session`` vault note already exists.

    Reads ``session_id`` AND ``type`` frontmatter fields from each note (rather
    than parsing the filename) because make_filename uses a 4-char SHA256 hash,
    not the SID.  Only notes with ``type: claude-session`` are counted —
    snapshots (``type: claude-snapshot``) must NOT block reaping: a session that
    was SIGKILL'd after a PreCompact snapshot but before SessionEnd fired would
    be incorrectly marked as "already written" if snapshots were included.

    Legacy notes without a ``type`` field are treated as ``claude-session`` for
    backward-compat (mirrors the same convention used elsewhere in obsidian_utils).

    This is O(N) I/O where N is the count of existing session notes for this
    project.  The lookup uses full SIDs to eliminate the collision risk of the
    previous 8-char prefix set.
    """
    from obsidian_utils import _peek_frontmatter_fields

    sessions = Path(vault_path) / sessions_folder
    if not sessions.is_dir():
        return set()
    sids: set = set()
    for note in sessions.glob("*.md"):
        # ONE read + ONE parse per note, not three. This loop runs on the
        # SessionStart hook inside reaper_max_runtime_seconds (5s) over every
        # session note in the vault (~1000), so three single-field peeks meant
        # reading and re-parsing the same bytes three times. Reading all three
        # fields up front (rather than short-circuiting on `type`) is what
        # makes that possible; the only visible difference is that an
        # empty-but-present `project:`/`session_id:` on a non-session note now
        # emits its corruption warning too.
        fields = _peek_frontmatter_fields(note, ("type", "project", "session_id"))
        # Legacy notes without a `type` are treated as claude-session (see
        # this function's docstring).
        if (fields["type"] or "claude-session") != "claude-session":
            continue  # skip snapshots, insights, etc.
        # Project filter: only count notes belonging to this project.
        note_project = fields["project"]
        if note_project and note_project != project:
            continue
        raw_sid = fields["session_id"]
        if raw_sid:
            sids.add(raw_sid)
    return sids


# ---------------------------------------------------------------------------
# Helper: log summary line
# ---------------------------------------------------------------------------


def _log_summary(project: str, out: ReaperOutcome) -> None:
    from obsidian_utils import _append_reaper_log

    detail = (
        f"reaped={out.reaped} "
        f"skipped_existing={out.skipped_already_written} "
        f"skipped_below={out.skipped_below_threshold} "
        f"canary_blocked={out.skipped_permission_blocked} "
        f"timeout={out.timeout} "
        f"wall_ms={out.wall_ms}"
    )
    _append_reaper_log(project=project, sid=None, event="SUMMARY", detail=detail)


# ---------------------------------------------------------------------------
# Internal main loop (testable directly)
# ---------------------------------------------------------------------------


def _reap_orphaned_sessions(
    project: str,
    vault_path: str,
    sessions_folder: str,
    config: dict,
) -> ReaperOutcome:
    """Main reaper loop — internal, directly testable.

    Iterates legacy transcript files selected by the named Claude adapter
    whose mtime > watermark, checks thresholds, deduplicates against vault,
    and writes reconstructed notes for orphaned sessions.
    """
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    if context is not None:
        return _registered_reaper_outcome(context, vault_path)
    from obsidian_utils import (
        _append_reaper_log,
        _safe_mtime,
        build_raw_fallback,
        canonical_project_name,
        extract_assistant_messages,
        extract_session_metadata,
        extract_tool_uses,
        extract_user_messages,
        make_filename,
        read_transcript,
        should_skip_session,
        slugify,
        write_vault_note,
        _first_seen_date,
    )
    from obsidian_session_log import _build_note

    start_ts = time.monotonic()
    max_runtime = config.get("reaper_max_runtime_seconds", 5)

    from runtime_adapters.claude import legacy_reaper_watermark
    watermark_path = legacy_reaper_watermark(project)
    watermark = _read_watermark(watermark_path)

    project_jsonl_dir = _resolve_project_jsonl_dir(project)
    if not project_jsonl_dir.is_dir():
        wall_ms = int((time.monotonic() - start_ts) * 1000)
        out = ReaperOutcome(
            reaped=0,
            skipped_below_threshold=0,
            skipped_already_written=0,
            skipped_permission_blocked=False,
            timeout=False,
            wall_ms=wall_ms,
        )
        _log_summary(project, out)
        return out

    try:
        jsonls = sorted(
            (p for p in project_jsonl_dir.iterdir() if p.suffix == ".jsonl"),
            key=lambda p: _safe_mtime(p),
        )
    except OSError as exc:
        _append_reaper_log(project=project, sid=None,
                           event="READ_DIR_FAILED",
                           detail=f"{type(exc).__name__}: {exc}")
        wall_ms = int((time.monotonic() - start_ts) * 1000)
        out = ReaperOutcome(
            reaped=0,
            skipped_below_threshold=0,
            skipped_already_written=0,
            skipped_permission_blocked=False,
            timeout=False,
            wall_ms=wall_ms,
        )
        _log_summary(project, out)
        return out

    # Hoist canonical_project_name() before the loop — spawns git rev-parse
    # only once per reaper invocation instead of once per JSONL file.
    # Falls back to `project` param if canonical resolution returns "unknown"
    # (e.g., cwd deleted or not a git repo).
    canonical = canonical_project_name()

    # effective_project is used for all VAULT-SIDE identifiers: frontmatter,
    # dedupe set lookup, and filename slug.  This collapses worktree variants
    # (e.g. "obsidian-brain-issue-125-...") to the canonical repo name so the
    # reaper never creates duplicate notes with a different basename than what
    # live SessionEnd would produce.
    #
    # NOTE: _resolve_project_jsonl_dir MUST keep using raw `project` (the
    # cwd-basename → CC's path encoding).  Only vault-side identifiers use
    # effective_project.
    effective_project = canonical if canonical and canonical != "unknown" else project

    existing_sids = _build_existing_sid_set(vault_path, sessions_folder, effective_project)

    canary_done = False
    last_processed_mtime = watermark

    # Mutable counters (ReaperOutcome is frozen; accumulate locally)
    n_reaped = 0
    n_below = 0
    n_existing = 0
    n_perm_blocked = False
    did_timeout = False

    for jsonl in jsonls:
        if time.monotonic() - start_ts >= max_runtime:
            did_timeout = True
            break

        mtime = _safe_mtime(jsonl)
        if mtime <= watermark:
            continue

        sid = jsonl.stem
        sid_short = sid[:8]
        if sid in existing_sids:
            n_existing += 1
            last_processed_mtime = max(last_processed_mtime, mtime)
            continue

        try:
            messages = read_transcript(str(jsonl))
            user_msgs = extract_user_messages(messages)
            # Use empty cwd — we don't know the original working directory
            # for an orphaned/reconstructed session.
            metadata = extract_session_metadata(messages, "")
            # Override the extractor's "unknown" project with effective_project
            # so worktree variants (e.g. obsidian-brain-issue-125-...) collapse
            # to the canonical repo name.  effective_project is hoisted above the
            # loop and matches the project name used for dedupe + filename slug.
            metadata["project"] = effective_project
        except Exception as exc:
            _append_reaper_log(project=project, sid=sid_short,
                               event="READ_FAILED", detail=str(exc))
            continue

        min_msgs = config.get("min_messages", 3)
        min_dur = config.get("min_duration_minutes", 2)
        # Two-stage threshold check (mirrors SessionEnd should_skip_session usage):
        # Stage 1: check message count alone (duration=0 so duration gate skipped)
        if should_skip_session(user_msgs, 0, min_messages=min_msgs, min_duration=min_dur):
            n_below += 1
            _append_reaper_log(project=project, sid=sid_short,
                               event="SKIPPED_BELOW_THRESHOLD")
            last_processed_mtime = max(last_processed_mtime, mtime)
            continue
        # Stage 2: check with actual duration
        duration_min = float(metadata.get("duration_minutes", 0.0))
        if should_skip_session(user_msgs, duration_min, min_messages=min_msgs, min_duration=min_dur):
            n_below += 1
            _append_reaper_log(project=project, sid=sid_short,
                               event="SKIPPED_BELOW_THRESHOLD")
            last_processed_mtime = max(last_processed_mtime, mtime)
            continue

        # Canary: check vault writability once, only when about to write
        if not canary_done:
            if not _permission_canary(vault_path):
                n_perm_blocked = True
                _append_reaper_log(project=project, sid=sid_short,
                                   event="SKIPPED_PERMISSION_BLOCKED")
                break
            canary_done = True

        # Build and write the reconstructed note.
        # Thread the real JSONL path into metadata so _build_note can substitute
        # it into the reconstructed-note banner instead of literal <slug>/<sid>.
        metadata["transcript_path"] = str(jsonl)
        date_str = _first_seen_date(sid)
        filename = make_filename(date_str, slugify(effective_project), sid)
        from note_transactions import context_for_vault, read_revision
        write_context = context_for_vault(vault_path)
        expected_revision = read_revision(
            write_context, Path(vault_path) / sessions_folder / filename,
        )
        assistant_msgs = extract_assistant_messages(messages)
        tool_uses = extract_tool_uses(messages)
        body = build_raw_fallback(user_msgs, metadata,
                                  assistant_msgs=assistant_msgs,
                                  tool_uses=tool_uses, config=config)
        content = _build_note(sid, metadata, body, resumed=False, reconstructed=True)
        err = write_vault_note(vault_path, sessions_folder, filename, content,
                               expected_revision=expected_revision)
        if err is None:
            n_reaped += 1
            _append_reaper_log(project=project, sid=sid_short, event="REAPED_OK")
            last_processed_mtime = max(last_processed_mtime, mtime)
        else:
            _append_reaper_log(project=project, sid=sid_short,
                               event="WRITE_FAILED", detail=err)
            break

    if last_processed_mtime > watermark:
        try:
            # Use math.ceil so the watermark advances past sub-second mtime
            # fragments — prevents re-reading files whose fractional mtime would
            # still satisfy `mtime > watermark` on the next run.
            _write_watermark_atomic(watermark_path, math.ceil(last_processed_mtime))
        except OSError as exc:
            _append_reaper_log(project=project, sid=None,
                               event="WATERMARK_WRITE_FAILED", detail=str(exc))

    wall_ms = int((time.monotonic() - start_ts) * 1000)
    out = ReaperOutcome(
        reaped=n_reaped,
        skipped_below_threshold=n_below,
        skipped_already_written=n_existing,
        skipped_permission_blocked=n_perm_blocked,
        timeout=did_timeout,
        wall_ms=wall_ms,
    )
    _log_summary(project, out)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def reap_orphaned_sessions(
    project: str,
    vault_path: str,
    sessions_folder: str,
    config: dict,
) -> ReaperOutcome:
    """Scan for orphaned session JSONLs and write reconstructed vault notes.

    Parameters
    ----------
    project:
        Canonical project slug (from obsidian_utils.canonical_project_name()).
    vault_path:
        Absolute path to the user's Obsidian vault root.
    sessions_folder:
        Vault sub-folder name for session notes (e.g. "claude-sessions").
    config:
        Loaded config dict from load_config().  Reads ``reaper_enabled`` and
        ``reaper_max_runtime_seconds`` keys (both optional, safe defaults).

    Returns
    -------
    ReaperOutcome
        Summary of the run.  Always returns; never raises.
    """
    try:
        from runtime_context import current_runtime_context
        context = current_runtime_context()
        if context is not None:
            return _registered_reaper_outcome(context, vault_path)
        return _reap_orphaned_sessions(project, vault_path, sessions_folder, config)
    except Exception as exc:
        from obsidian_utils import _append_reaper_log
        _append_reaper_log(
            project=project,
            sid=None,
            event="REAPER_CRASHED",
            detail=f"{type(exc).__name__}: {exc}",
        )
        print(f"[obsidian-brain] reaper unexpected error: {exc}", file=sys.stderr)
        return ReaperOutcome(
            reaped=0,
            skipped_below_threshold=0,
            skipped_already_written=0,
            skipped_permission_blocked=False,
            timeout=False,
            wall_ms=0.0,
        )


def reap_registered_sessions(context, max_sources=8, deadline=None):
    """Recover registered sources with shared cursors; never finalize active ones."""
    from runtime_context import using_runtime_context
    from capture import CaptureResult, recover_registered
    deadline = time.monotonic() + 1 if deadline is None else deadline
    with using_runtime_context(context):
        try:
            return recover_registered(context, max_sources=max_sources, deadline=deadline, include_active=False)
        except Exception as exc:
            print(f"[obsidian-brain] registered-source recovery failed: {exc}", file=sys.stderr)
            return CaptureResult("unavailable", pending_sources=1, warnings=(str(exc),))


def _registered_reaper_outcome(context, vault_path):
    """Keep the legacy response shape without inventing native recovery counts."""
    if Path(vault_path).resolve() != context.vault_path.resolve():
        raise ValueError('Recovery cannot switch the selected runtime vault')
    started = time.monotonic()
    budget = min(5.0, max(0.0, float(context.config.get('reaper_max_runtime_seconds', 1))))
    deadline = started + budget
    result = reap_registered_sessions(context, max_sources=8, deadline=deadline)
    ended = time.monotonic()
    return ReaperOutcome(0, 0, 0, False, ended >= deadline,
                         int((ended - started) * 1000), recovery_status=result.status)
