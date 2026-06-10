"""vault_doctor check: detect SessionEnd-hook coverage gaps.

For each ``<sid>.jsonl`` under ``~/.claude/projects/``, this check verifies
that a corresponding session note exists in ``<vault>/<sessions_folder>/``.
When the SessionEnd hook fails, is killed, or never runs, the JSONL exists
but the note is missing — this check surfaces those gaps.

This check is OPT-IN (``OPT_IN = True``): it does not run in the default
all-checks sweep because (a) it walks every JSONL under ``~/.claude/projects/``
across ALL projects — a heavy filesystem + parse pass that would slow every
default `vault_doctor` run, and (b) it is a standing audit (gap rows persist
until the operator recovers or accepts them), not actionable per-run drift.
Run it explicitly:

    python3 scripts/vault_doctor.py --check session-coverage

Detection strategy:
1. Build an index of existing session notes: ``session_id`` frontmatter →
   covered; plus filename-hash (4-char sha256 suffix) as a legacy fallback
   for notes written before ``session_id`` was added to frontmatter. A
   filename hash only enters the fallback pool when the note LACKS a
   ``session_id`` (true legacy) or its read failed — modern notes are covered
   by their session_id and must not contribute 4-char collision candidates
   that could silently hide gaps.
2. Build a ``referenced_by`` index: for each note in the insights/decisions/
   error-fixes/retros folders, map ``source_session`` UUID → list of
   basenames.
3. Walk ``~/.claude/projects/`` for JSONLs whose mtime is within the window.
   Derive the project name from the first parseable JSONL line that carries a
   ``cwd`` field (production JSONLs often start with summary/file-history
   lines without one).
4. A JSONL is **covered** if its stem (``sid``) appears in the session_id
   index or if ``sha256(sid).hexdigest()[:4]`` matches a legacy filename-hash.
5. Below-threshold sessions are skipped — the hook would also have skipped
   them. The user-message count mirrors the hook's
   ``extract_user_messages``/``_extract_text`` semantics: a user entry counts
   only if its ``message.content`` is a non-empty string or contains a
   ``{"type": "text"}`` block with non-empty text. Entries whose content is
   only ``tool_result`` blocks (the majority of ``type=="user"`` lines in a
   real transcript) do NOT count.
6. Otherwise emit a gap Issue with ``signal_class="session-coverage-gap"``.

Project-name derivation limitation:
  The derived project name is the slugified BASENAME of the JSONL's ``cwd``
  field. The production hook uses the git-aware ``canonical_project_name()``
  (``git rev-parse --show-toplevel`` basename), so sessions launched from a
  worktree or a subdirectory may display a non-canonical expected note path
  here. ``--project`` therefore expects the cwd-basename slug, not the
  canonical git name. Gap DETECTION is unaffected — coverage is matched by
  session_id / filename hash, never by project name.

The date used to compose the expected filename is derived from the first
entry's ``timestamp`` field in the JSONL, converted to **local time** — this
matches the production hook behaviour:

  ``obsidian_session_log._run()`` calls ``obsidian_utils._first_seen_date(sid)``
  which, for new sessions, returns ``datetime.date.today().isoformat()`` (local
  time). So for an unwritten note we fall back to deriving the date from the
  first JSONL timestamp in local time (``datetime.fromtimestamp(ts)``, which
  honours the local timezone). Sessions that cross midnight may land on the
  wrong day — we document the caveat in the Issue's extra dict but don't
  special-case it here.

Repair mode (``--reconstruct``):
  ``apply()`` invokes ``scripts/dev-test/replay-sessionend.py`` as a subprocess
  to re-run the production hook code path and write the missing session note.
  This is never automatic — users must pass ``--apply`` explicitly.

Flags:
  ``--strict``      emit FAIL (not WARN) when any note references the orphaned
                    session via ``source_session``. Changes the reason prefix
                    only — not the exit code.
  ``--reconstruct`` mark issues as resolvable (``unresolved=False``) and enable
                    ``apply()``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import Issue, Result
from .source_sessions import _EXTRA_INSIGHT_FOLDERS, _parse_frontmatter

NAME = "session-coverage"
DESCRIPTION = (
    "Detect JSONL files that have no corresponding session note "
    "(SessionEnd hook missed or failed; opt-in, run via --check)"
)
DEFAULT_WINDOW_DAYS = 30  # JSONL mtime window

# Opt-in: excluded from the default all-checks sweep (see module docstring).
OPT_IN = True

# Extra scan-time flags consumed by the dispatcher (vault_doctor.py).
EXTRA_SCAN_FLAGS = ("strict", "reconstruct")

# Replay/hook outcomes that mean "the session note was written".
# The production SessionEnd hook emits OK_RAW_NOTE_ONLY
# (obsidian_session_log._Outcome — "OK" is reserved, never emitted today);
# the reaper emits REAPED_OK. "OK" is kept for forward-compatibility.
_SUCCESS_OUTCOMES = {"OK_RAW_NOTE_ONLY", "OK", "REAPED_OK"}

# Characters not safe for a project name in a filename path component.
# Keep in sync with obsidian_utils.slugify (we can't import hooks here).
_UNSAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _slugify(name: str) -> str:
    """Best-effort approximation of obsidian_utils.slugify() for project names.

    Rules:
    - Lowercase
    - Replace every char outside [a-zA-Z0-9_-] with a hyphen
    - Collapse consecutive hyphens to one
    - Strip leading/trailing hyphens

    We can't import obsidian_utils here (it's in the hooks/ tree, not on the
    default sys.path for scripts/). The approximation is close enough for the
    expected-filename heuristic — the actual marker file governs note naming
    on the hook side. See the module docstring for the cwd-basename vs
    canonical_project_name limitation.
    """
    name = name.lower()
    name = _UNSAFE_SLUG_RE.sub("-", name)
    # Collapse consecutive hyphens
    name = re.sub(r"-{2,}", "-", name)
    return name.strip("-") or "session"


def _sid_hash(sid: str) -> str:
    """Return the 4-char sha256 hash used in filenames (matches obsidian_utils.make_filename)."""
    return hashlib.sha256(sid.encode()).hexdigest()[:4]


def _make_filename(date_str: str, slug: str, sid: str) -> str:
    """Build note filename: YYYY-MM-DD-<slug>-<hash4>.md (mirrors obsidian_utils.make_filename)."""
    return f"{date_str}-{slug}-{_sid_hash(sid)}.md"


def _load_thresholds(home: Path) -> tuple[int, float, bool]:
    """Read min_messages / min_duration_minutes / auto_log_enabled from config.

    Matches the precedence in vault_doctor._load_config: reads the JSON
    directly (does NOT import load_config — that function's session-scoped
    cache can be stale outside a live Claude Code session).

    Returns (min_messages, min_duration_minutes, auto_log_enabled) with
    defaults (3, 2.0, True). Each key is coerced independently so one bad
    value does not discard the others. A missing config file is silent
    (defaults apply — same as the hook); any other read/parse failure warns
    to stderr.
    """
    cfg_path = home / ".claude" / "obsidian-brain-config.json"
    min_messages, min_duration, auto_log = 3, 2.0, True
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return (min_messages, min_duration, auto_log)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[session-coverage] WARNING: could not read config {cfg_path}: "
            f"{exc}; using threshold defaults (3, 2.0, auto_log on)",
            file=sys.stderr,
        )
        return (min_messages, min_duration, auto_log)

    if not isinstance(cfg, dict):
        print(
            f"[session-coverage] WARNING: config {cfg_path} is not a JSON "
            f"object; using threshold defaults",
            file=sys.stderr,
        )
        return (min_messages, min_duration, auto_log)

    # Coerce each key independently — one bad key must not discard the rest.
    try:
        min_messages = int(cfg.get("min_messages", 3))
    except (ValueError, TypeError):
        print(
            "[session-coverage] WARNING: bad min_messages in config; using 3",
            file=sys.stderr,
        )
    try:
        min_duration = float(cfg.get("min_duration_minutes", 2.0))
    except (ValueError, TypeError):
        print(
            "[session-coverage] WARNING: bad min_duration_minutes in config; using 2.0",
            file=sys.stderr,
        )
    auto_log = bool(cfg.get("auto_log_enabled", True))

    return (min_messages, min_duration, auto_log)


def _user_entry_has_text(entry: dict) -> bool:
    """Return True if a type=="user" entry carries human-visible text.

    Mirrors obsidian_utils._extract_text semantics: content counts when it is
    a non-empty string, a non-empty string item in a list, or a
    {"type": "text"} block with non-empty text. tool_result / tool_use blocks
    are skipped — a user entry whose content is ONLY tool results (the
    majority of type=="user" lines in a real transcript) does not count.
    """
    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return False
    content = msg.get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and str(part.get("text", "")).strip():
                    return True
            elif isinstance(part, str) and part.strip():
                return True
    return False


def _parse_jsonl_metrics(
    jsonl_path: Path,
) -> tuple[int, float, str | None, str | None] | None:
    """Parse a JSONL for threshold checking.

    Returns (user_message_count, duration_minutes, first_ts_iso, cwd) or
    None when the file is completely unparsable (every line fails JSON
    decode).

    ``user_message_count`` counts only TEXT-BEARING user entries (see
    ``_user_entry_has_text``) — mirrors the hook's extract_user_messages.
    ``first_ts_iso`` is the ISO-8601 timestamp of the first parseable entry
    with a timestamp field (for date derivation); None if absent.
    ``cwd`` is the ``cwd`` field from the first parseable line that HAS one
    (production JSONLs often start with summary/file-history lines without
    a cwd); None if no line carries one.

    ``duration_minutes`` is ``(last_ts - first_ts) / 60`` in wall-clock time.
    Returns 0.0 when fewer than two timestamp-bearing entries exist (mirrors
    the hook's treatment of very short sessions).
    """
    try:
        raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = raw.splitlines()
    if not lines:
        return None

    user_count = 0
    first_ts: float | None = None
    last_ts: float | None = None
    first_cwd: str | None = None
    first_ts_iso: str | None = None
    parsed_any = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        parsed_any = True

        # First parseable line that HAS a cwd wins (earlier lines may lack one).
        if first_cwd is None:
            cwd_val = entry.get("cwd")
            if cwd_val:
                first_cwd = cwd_val

        if entry.get("type") == "user" and _user_entry_has_text(entry):
            user_count += 1

        ts_raw = entry.get("timestamp")
        if ts_raw:
            try:
                if ts_raw.endswith("Z"):
                    ts_raw = ts_raw[:-1] + "+00:00"
                ts = datetime.fromisoformat(ts_raw).timestamp()
                if first_ts is None:
                    first_ts = ts
                    first_ts_iso = entry.get("timestamp", "")
                last_ts = ts
            except ValueError:
                pass

    if not parsed_any:
        return None

    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        duration_minutes = (last_ts - first_ts) / 60.0
    else:
        duration_minutes = 0.0

    return (user_count, duration_minutes, first_ts_iso, first_cwd)


def _first_seen_date_from_ts(first_ts_iso: str | None) -> str:
    """Convert the first JSONL timestamp to a YYYY-MM-DD string in local time.

    The production hook calls ``obsidian_utils._first_seen_date(sid)`` which
    for new (unwritten) sessions returns ``datetime.date.today().isoformat()``
    — i.e., the local calendar date when the session STARTED (or the first
    SessionStart/PreCompact event ran). We approximate this by converting the
    first JSONL entry's timestamp to local time via
    ``datetime.fromtimestamp(ts)`` (no tz arg → system local timezone),
    matching the system's interpretation of "today" as the hook would have
    seen it.

    Falls back to today's local date when the timestamp is absent or
    unparsable.
    """
    if first_ts_iso:
        try:
            ts_raw = first_ts_iso
            if ts_raw.endswith("Z"):
                ts_raw = ts_raw[:-1] + "+00:00"
            epoch = datetime.fromisoformat(ts_raw).timestamp()
            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            pass
    return datetime.now().strftime("%Y-%m-%d")


def _index_session_notes(
    vault: Path, sessions_folder: str
) -> tuple[set[str], set[str], int]:
    """Build coverage indexes for fast JSONL lookup.

    Returns:
        sid_set:    set of ``session_id`` frontmatter values found in session notes.
        hash_set:   set of 4-char filename-hash suffixes (``*-<hash4>.md``) used
                    as a LEGACY fallback. A hash enters this set only when the
                    note lacks a ``session_id`` frontmatter field (true legacy)
                    or its read failed — modern notes are covered via sid_set
                    and must not become collision candidates that could
                    silently hide gaps behind a 4-hex-char match.
        unreadable: count of session notes whose read failed (sid-index is
                    degraded for these; only the hash fallback covers them).
    """
    sessions_dir = vault / sessions_folder
    sid_set: set[str] = set()
    hash_set: set[str] = set()
    unreadable = 0

    # Regex to extract the trailing 4-char hash from a session note filename.
    _hash_re = re.compile(r"-([0-9a-f]{4})\.md$")

    if not sessions_dir.is_dir():
        return sid_set, hash_set, unreadable

    for entry in sessions_dir.iterdir():
        if not entry.name.endswith(".md"):
            continue
        m = _hash_re.search(entry.name)

        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Note unreadable — sid-index is degraded for this note; keep the
            # filename hash as the only coverage signal so an unreadable but
            # real note doesn't surface as a false gap.
            unreadable += 1
            if m:
                hash_set.add(m.group(1))
            print(
                f"[session-coverage] WARNING: could not read session note "
                f"{entry} ({exc}); sid-index degraded, hash fallback only",
                file=sys.stderr,
            )
            continue

        fm = _parse_frontmatter(text, source=str(entry))
        sid = fm.get("session_id", "")
        if sid:
            sid_set.add(sid)
        elif m:
            # True legacy note (no session_id frontmatter) — hash fallback.
            hash_set.add(m.group(1))

    return sid_set, hash_set, unreadable


def _index_referenced_by(
    vault: Path, insights_folder: str
) -> dict[str, list[str]]:
    """Map source_session UUID → list of note basenames that reference it.

    Scans the user-configured insights folder plus the conventional auxiliary
    folders (decisions, error-fixes, retros). Mirrors the folder list from
    source_sessions.scan(). Unreadable notes are warned to stderr — a silent
    skip would silently downgrade --strict severity (the reference count
    drives the FAIL: prefix).
    """
    folders = [insights_folder] + [
        f for f in _EXTRA_INSIGHT_FOLDERS if f != insights_folder
    ]
    ref_index: dict[str, list[str]] = {}

    for folder in folders:
        folder_path = vault / folder
        if not folder_path.is_dir():
            continue
        for entry in folder_path.iterdir():
            if not entry.name.endswith(".md"):
                continue
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(
                    f"[session-coverage] WARNING: could not read note {entry} "
                    f"({exc}); its source_session reference (if any) is not "
                    f"counted — strict severity may be understated",
                    file=sys.stderr,
                )
                continue
            fm = _parse_frontmatter(text, source=str(entry))
            src_sid = fm.get("source_session", "")
            if src_sid:
                ref_index.setdefault(src_sid, []).append(entry.name)

    return ref_index


def scan(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    days: int,
    project: str | None = None,
    strict: bool = False,
    reconstruct: bool = False,
) -> list[Issue]:
    """Detect JSONLs that have no corresponding session note in the vault.

    Args:
        vault_path:      Absolute path to the Obsidian vault root.
        sessions_folder: Vault-relative folder for session notes (e.g. ``claude-sessions``).
        insights_folder: Vault-relative folder for insights (e.g. ``claude-insights``).
        days:            How many days of JSONL mtime history to consider.
        project:         If set, only surface gaps for this project name
                         (cwd-basename slug — see module docstring).
        strict:          If True, emit FAIL prefix when any insight references the gap.
        reconstruct:     If True, mark issues resolvable (unresolved=False) to enable apply().
    """
    vault = Path(vault_path)
    home = Path("~/.claude").expanduser().parent  # Path.home()
    projects_root = Path("~/.claude/projects").expanduser()

    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - days * 86400

    # Load threshold config (reads config file directly — no cache import).
    min_messages, min_duration, auto_log_enabled = _load_thresholds(home)

    # When auto-logging is disabled, the hook intentionally writes nothing —
    # every "gap" would be a false positive. Bail out early.
    if not auto_log_enabled:
        print(
            "[session-coverage] auto_log_enabled is false in config — the "
            "SessionEnd hook intentionally writes no notes; skipping scan "
            "(all gaps would be false positives)",
            file=sys.stderr,
        )
        return []

    # Step 1: index existing session notes.
    sid_set, hash_set, n_unreadable_notes = _index_session_notes(vault, sessions_folder)

    # Step 2: index referenced_by (insights/decisions/etc pointing to a session).
    ref_index = _index_referenced_by(vault, insights_folder)

    issues: list[Issue] = []

    # Counters for end-of-scan stderr summary.
    n_gaps = 0
    n_covered = 0
    n_below_threshold = 0
    n_unparsable = 0
    n_project_filtered = 0
    n_project_dirs = 0

    if not projects_root.is_dir():
        # No projects directory at all — nothing to scan.
        print(
            "[session-coverage] ~/.claude/projects not found; nothing to scan",
            file=sys.stderr,
        )
        return []

    # Step 3: walk project dirs.
    for proj_dir in sorted(projects_root.iterdir()):
        if not proj_dir.is_dir():
            continue

        jsonl_files = list(proj_dir.glob("*.jsonl"))
        if not jsonl_files:
            continue

        dir_counted = False

        for jsonl in sorted(jsonl_files):
            # Single stat per JSONL — st_mtime and st_size are reused below so
            # there is no second stat() (TOCTOU crash window between exists()
            # and stat() on a file CC might prune mid-scan).
            try:
                st = jsonl.stat()
            except OSError as exc:
                # File vanished or unreadable between glob and stat — count it
                # in the unparsable bucket so the summary partition stays total.
                n_unparsable += 1
                print(
                    f"[session-coverage] WARNING: could not stat JSONL "
                    f"(counted as unparsable): {jsonl}: {exc}",
                    file=sys.stderr,
                )
                if not dir_counted:
                    n_project_dirs += 1
                    dir_counted = True
                continue

            mtime = st.st_mtime
            if mtime < cutoff:
                continue

            sid = jsonl.stem

            # Step 3a: parse the JSONL for project name + threshold metrics.
            metrics = _parse_jsonl_metrics(jsonl)
            if metrics is None:
                # Completely unparsable — count as below-threshold (not a gap),
                # warn, and move on.
                n_unparsable += 1
                print(
                    f"[session-coverage] WARNING: unparsable JSONL (skipped): {jsonl}",
                    file=sys.stderr,
                )
                if not dir_counted:
                    n_project_dirs += 1
                    dir_counted = True
                continue

            user_count, duration_minutes, first_ts_iso, cwd = metrics

            # Derive project name from cwd field.
            if cwd:
                derived_project = _slugify(Path(cwd).name)
            else:
                derived_project = "unknown"

            # Step 3b: project filter.
            if project and derived_project != project.replace("_", "-").lower():
                n_project_filtered += 1
                if not dir_counted:
                    n_project_dirs += 1
                    dir_counted = True
                continue

            if not dir_counted:
                n_project_dirs += 1
                dir_counted = True

            # Step 3c: threshold check — skip if hook would have skipped.
            # Replicates obsidian_utils.should_skip_session semantics:
            #   skip if user_count < min_messages           (strictly less-than)
            #   skip if 0 < duration < min_duration_minutes (strictly less-than)
            # Exactly-at-threshold sessions are NOT skipped — they ARE gaps.
            if user_count < min_messages:
                n_below_threshold += 1
                continue
            if duration_minutes > 0 and duration_minutes < min_duration:
                n_below_threshold += 1
                continue

            # Step 4: coverage check.
            h4 = _sid_hash(sid)
            if sid in sid_set or h4 in hash_set:
                n_covered += 1
                continue

            # Step 5: gap detected.
            n_gaps += 1
            size = st.st_size
            mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            )

            # Compose expected note basename using the same formula as the hook.
            # The hook calls _first_seen_date(sid) which returns local-date when
            # the session started. We derive that from first JSONL timestamp in
            # local time (see module docstring for the reasoning).
            date_str = _first_seen_date_from_ts(first_ts_iso)
            expected_basename = _make_filename(date_str, derived_project, sid)
            expected_note_path = vault / sessions_folder / expected_basename

            # Build referenced_by list.
            refs = ref_index.get(sid, [])
            ref_count = len(refs)

            # Reason string — FAIL prefix when strict AND there are references.
            strict_fail = strict and ref_count > 0
            prefix = "FAIL:" if strict_fail else "WARN:"
            reason = (
                f"{prefix} JSONL exists ({size} bytes) but session note missing;"
                f" referenced by {ref_count} note(s)"
            )

            issues.append(
                Issue(
                    check=NAME,
                    note_path=str(expected_note_path),
                    project=derived_project,
                    current_source=f"{sid}.jsonl ({size} bytes)",
                    proposed_source=f"[[{expected_basename[:-3]}]]",  # strip .md
                    reason=reason,
                    confidence=0.0,
                    extra={
                        "unresolved": not reconstruct,
                        "signal_class": "session-coverage-gap",
                        "sid": sid,
                        "jsonl_path": str(jsonl),
                        "jsonl_bytes": size,
                        "jsonl_mtime": mtime_iso,
                        "cwd": cwd or "",
                        "referenced_by": refs,
                        "strict_fail": strict_fail,
                    },
                )
            )

    # Step 7: end-of-scan stderr summary. The five buckets partition the
    # scanned JSONLs exactly; unreadable session notes are an index-side
    # (note-side) counter appended for operator visibility.
    total_scanned = n_gaps + n_covered + n_below_threshold + n_unparsable + n_project_filtered
    if total_scanned > 0 or n_project_dirs > 0:
        print(
            f"[session-coverage] scanned {total_scanned} jsonl(s) across"
            f" {n_project_dirs} project dir(s):"
            f" {n_gaps} gaps, {n_covered} covered,"
            f" {n_below_threshold} below-threshold,"
            f" {n_unparsable} unparsable,"
            f" {n_project_filtered} project-filtered;"
            f" {n_unreadable_notes} unreadable session note(s)",
            file=sys.stderr,
        )

    return issues


def apply(issues: list[Issue], backup_root: str) -> list[Result]:
    """Reconstruct missing session notes via replay-sessionend.py.

    Only reachable when ``scan`` ran with ``reconstruct=True`` (which sets
    ``unresolved=False`` on each Issue). Issues with ``unresolved=True`` are
    returned as-is with status ``"unresolved"``.

    For each resolvable issue:
    - If the expected note now exists → ``"skipped"`` (already recovered).
    - If the gap has no recorded cwd → ``"error"`` (we never fabricate a
      ``--cwd``; the operator can run replay-sessionend.py manually with the
      correct one).
    - Otherwise invoke ``scripts/dev-test/replay-sessionend.py`` via subprocess
      with ``--json`` and map the outcome:
        - success outcomes (``_SUCCESS_OUTCOMES``: OK_RAW_NOTE_ONLY / OK /
          REAPED_OK) with non-empty ``vault_writes`` → ``"applied"`` — the
          Result's note_path is the ACTUAL written path from vault_writes,
          which is ground truth. Note: the scan-predicted note_path can differ
          from the hook's real output — the hook's ``_first_seen_date(sid)``
          returns TODAY's date for sessions whose marker file was never
          written, while the scan predicts from the JSONL's first timestamp.
        - success outcome but EMPTY vault_writes → ``"error"`` (the replay
          reported success but wrote nothing — broken contract).
        - ``SKIPPED_*``  → ``"skipped"``  (with the skip reason in ``error``)
        - anything else  → ``"error"``

    No backup needed here — ``apply()`` creates a new file, nothing is
    overwritten.

    Defense-in-depth: raises ``RuntimeError`` if called with an issue whose
    ``signal_class`` is not ``"session-coverage-gap"`` — this is a
    programming error, not a per-issue error.
    """
    _REPLAY = (
        Path(__file__).resolve().parents[1]  # scripts/
        / "dev-test"
        / "replay-sessionend.py"
    )
    # Upfront existence check — a broken plugin install must produce a clear
    # per-issue error, not N confusing subprocess launch failures.
    replay_missing = not _REPLAY.exists()

    results: list[Result] = []

    for issue in issues:
        if issue.extra.get("unresolved"):
            results.append(
                Result(check=NAME, note_path=issue.note_path, status="unresolved")
            )
            continue

        # Defense-in-depth guard.
        sc = issue.extra.get("signal_class", "")
        if sc != "session-coverage-gap":
            raise RuntimeError(
                f"apply() refuses signal_class={sc!r} for {issue.note_path}; "
                f"only session-coverage-gap is reconstructable."
            )

        if replay_missing:
            results.append(
                Result(
                    check=NAME,
                    note_path=issue.note_path,
                    status="error",
                    error=f"replay script missing from plugin install: {_REPLAY}",
                )
            )
            continue

        # If the note already exists (another tool wrote it), skip.
        if Path(issue.note_path).exists():
            results.append(
                Result(check=NAME, note_path=issue.note_path, status="skipped")
            )
            continue

        jsonl_path = issue.extra.get("jsonl_path", "")
        cwd = issue.extra.get("cwd", "")
        if not cwd:
            # Never fabricate a --cwd: the hook derives the project (and thus
            # the note's project frontmatter + filename slug) from it, so a
            # made-up value would write a wrongly-attributed note.
            results.append(
                Result(
                    check=NAME,
                    note_path=issue.note_path,
                    status="error",
                    error=(
                        "gap has no recorded cwd (JSONL carries no cwd field); "
                        "run replay-sessionend.py manually with the correct "
                        f"--cwd: --jsonl {jsonl_path}"
                    ),
                )
            )
            continue

        cmd = [
            sys.executable,
            str(_REPLAY),
            "--jsonl", jsonl_path,
            "--cwd", cwd,
            "--json",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            results.append(
                Result(
                    check=NAME,
                    note_path=issue.note_path,
                    status="error",
                    error="replay-sessionend.py timed out after 120s",
                )
            )
            continue
        except OSError as exc:
            results.append(
                Result(
                    check=NAME,
                    note_path=issue.note_path,
                    status="error",
                    error=f"failed to launch replay: {exc}",
                )
            )
            continue

        # Parse JSON outcome from stdout.
        try:
            out = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            results.append(
                Result(
                    check=NAME,
                    note_path=issue.note_path,
                    status="error",
                    error=(
                        f"replay stdout parse error: {exc}; "
                        f"returncode={proc.returncode}; stderr={proc.stderr[:200]}"
                    ),
                )
            )
            continue

        outcome = out.get("outcome", "UNKNOWN")
        if outcome in _SUCCESS_OUTCOMES:
            # vault_writes is the replay's ground truth for what was written
            # (the predicted issue.note_path may differ in date — see docstring:
            # the hook's _first_seen_date returns TODAY for never-written
            # sessions, while the scan predicted from the JSONL first timestamp).
            vault_writes = out.get("vault_writes", [])
            if vault_writes:
                actual_path = vault_writes[0][0]
                results.append(
                    Result(check=NAME, note_path=actual_path, status="applied")
                )
            else:
                results.append(
                    Result(
                        check=NAME,
                        note_path=issue.note_path,
                        status="error",
                        error=(
                            f"replay reported success ({outcome}) but wrote "
                            f"nothing (vault_writes empty)"
                        ),
                    )
                )
        elif outcome.startswith("SKIPPED_"):
            results.append(
                Result(
                    check=NAME,
                    note_path=issue.note_path,
                    status="skipped",
                    error=f"replay outcome: {outcome}",
                )
            )
        else:
            detail = out.get("detail", "")
            results.append(
                Result(
                    check=NAME,
                    note_path=issue.note_path,
                    status="error",
                    error=f"replay outcome={outcome}: {detail}",
                )
            )

    return results
