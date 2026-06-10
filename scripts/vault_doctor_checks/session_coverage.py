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
   that could silently hide gaps. SNAPSHOT notes (``*-<hash4>-snapshot*.md``
   filenames / ``type: claude-snapshot``) live in the same folder and carry
   the session's ``session_id`` — they are EXCLUDED from both coverage
   indexes (a snapshot is not the session note), and their hash4 values are
   collected separately for the snapshot-anchor bypass below.
2. Build a ``referenced_by`` index: for each note in the insights/decisions/
   error-fixes/retros folders, map ``source_session`` UUID → list of
   basenames.
3. Walk ``~/.claude/projects/`` for JSONLs whose mtime is within the window.
   Derive the project name from the first parseable JSONL line that carries a
   ``cwd`` field (production JSONLs often start with summary/file-history
   lines without one).
4. Below-threshold sessions are skipped — the hook would also have skipped
   them. The user-message count mirrors the hook's
   ``extract_user_messages``/``_extract_text`` semantics exactly: each
   non-empty text block in a user entry counts separately (one entry with 3
   text blocks counts 3), the flat fallback format (``role=="user"`` with
   top-level ``content``) is included, and ``tool_result``/``tool_use``
   blocks never count. EXCEPTION (snapshot-anchor bypass): the hook writes
   the session note DESPITE thresholds when the session has sibling
   snapshots (obsidian_session_log's "snapshot-bypass" path), so a
   below-threshold JSONL whose sid appears in a snapshot's ``session_id``
   frontmatter (or, for frontmatter-less snapshots, whose hash4 matches the
   snapshot filename) remains gap-eligible. The sid-exact match mirrors the
   hook's find_snapshots_for_session and avoids 4-hex-collision false
   bypasses between unrelated sessions.
5. A JSONL is **covered** if its stem (``sid``) appears in the session_id
   index or if ``sha256(sid).hexdigest()[:4]`` matches a legacy filename-hash.
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
  ``--reconstruct`` mark issues as resolvable (``unresolved=False``,
                    ``confidence=0.9`` — consistent with other applyable
                    repairs) and enable ``apply()``. Without it, gaps are
                    unresolved and carry ``confidence=0.0``.
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

# Characters replaced with '-' when deriving a project slug. This is an
# INTENTIONAL approximation of obsidian_utils.slugify (it preserves '_' and
# '-' just like the hook's version) — not a copy that must be kept in sync.
# Exact parity is not required: the slug only feeds the expected-filename
# heuristic, never the coverage match (which is session_id/hash-based).
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


def _extract_texts(content) -> list[str]:
    """Mirror of obsidian_utils._extract_text: list of non-empty text chunks.

    A string content yields one chunk; a list yields one chunk per
    {"type": "text"} block or bare string item. tool_use / tool_result
    blocks are skipped. Empty/whitespace-only chunks are filtered.
    """
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    texts.append(str(part.get("text", "")))
                # Skip tool_use and tool_result blocks (same as the hook).
            elif isinstance(part, str):
                texts.append(part)
    return [t for t in texts if t.strip()]


def _count_user_texts(entry: dict) -> int:
    """Count the text chunks a transcript entry contributes to the hook's
    ``extract_user_messages`` list.

    Mirrors extract_user_messages + _extract_text semantics EXACTLY,
    because ``should_skip_session`` thresholds on ``len(user_messages)``,
    which is a count of text BLOCKS, not entries:
      - canonical CC format: ``type=="user"`` → blocks from
        ``entry["message"]["content"]`` (one entry with 3 text blocks
        counts 3);
      - flat fallback format: ``role=="user"`` (when type is not "user")
        → blocks from top-level ``entry["content"]``;
      - tool_result/tool_use-only entries contribute 0.
    """
    if entry.get("type") == "user":
        msg = entry.get("message", {})
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        return len(_extract_texts(content))
    if entry.get("role") == "user":
        return len(_extract_texts(entry.get("content", "")))
    return 0


def _parse_ts_epoch(ts) -> float | None:
    """Parse a transcript timestamp to a POSIX float, or None.

    Mirrors hooks/obsidian_utils._parse_ts semantics: ISO-8601 (Z-suffixed,
    offset-bearing, naive, with or without fractional seconds) plus epoch
    seconds/milliseconds (numeric values > 1e12 are treated as millis).
    Accepts non-string values (epoch timestamps arrive as JSON numbers).
    Naive ISO datetimes are interpreted in local time — consistent with the
    hook, which only ever subtracts them pairwise (duration) or formats a
    local date.
    """
    if ts is None:
        return None
    s = str(ts)
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        pass
    try:
        val = float(s)
    except ValueError:
        return None
    if val > 1e12:
        val /= 1000.0
    try:
        # Round-trip through fromtimestamp to validate the range (mirrors
        # the hook's OSError guard on out-of-range epochs).
        return datetime.fromtimestamp(val, tz=timezone.utc).timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_jsonl_metrics(
    jsonl_path: Path,
) -> tuple[int, float, object, str | None] | None:
    """Parse a JSONL for threshold checking.

    Returns (user_message_count, duration_minutes, first_ts_raw, cwd) or
    None when the file is completely unparsable (every line fails JSON
    decode) or empty.

    Lines are STREAMED (never read_text'd whole) so peak memory stays
    O(longest line) even on multi-megabyte transcripts — matches the hook's
    read_transcript discipline.

    ``user_message_count`` sums TEXT BLOCKS contributed by user entries
    (see ``_count_user_texts``) — mirrors the hook's extract_user_messages,
    including the flat ``role=="user"`` fallback format.
    ``first_ts_raw`` is the raw timestamp value (str or number) of the first
    parseable entry with one (for date derivation); None if absent.
    ``cwd`` is the ``cwd`` field from the first parseable line that HAS one
    (production JSONLs often start with summary/file-history lines without
    a cwd); None if no line carries one.

    ``duration_minutes`` is ``(last_ts - first_ts) / 60`` in wall-clock time.
    Timestamp parsing mirrors obsidian_utils._parse_ts (ISO variants + epoch
    seconds/millis); the timestamp key chain mirrors extract_session_metadata
    (``timestamp`` → ``ts`` → ``created_at``). Returns 0.0 when fewer than
    two timestamp-bearing entries exist (mirrors the hook's treatment of
    very short sessions).
    """
    user_count = 0
    first_ts: float | None = None
    last_ts: float | None = None
    first_cwd: str | None = None
    first_ts_raw = None
    parsed_any = False

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
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

                # First parseable line that HAS a cwd wins (earlier lines may
                # lack one).
                if first_cwd is None:
                    cwd_val = entry.get("cwd")
                    if cwd_val:
                        first_cwd = cwd_val

                user_count += _count_user_texts(entry)

                ts_value = (
                    entry.get("timestamp") or entry.get("ts") or entry.get("created_at")
                )
                if ts_value is not None:
                    ts = _parse_ts_epoch(ts_value)
                    if ts is not None:
                        if first_ts is None:
                            first_ts = ts
                            first_ts_raw = ts_value
                        last_ts = ts
    except OSError:
        return None

    if not parsed_any:
        return None

    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        duration_minutes = (last_ts - first_ts) / 60.0
    else:
        duration_minutes = 0.0

    return (user_count, duration_minutes, first_ts_raw, first_cwd)


def _first_seen_date_from_ts(first_ts_raw) -> str:
    """Convert the first JSONL timestamp to a YYYY-MM-DD string in local time.

    The production hook calls ``obsidian_utils._first_seen_date(sid)`` which
    for new (unwritten) sessions returns ``datetime.date.today().isoformat()``
    — i.e., the local calendar date when the session STARTED (or the first
    SessionStart/PreCompact event ran). We approximate this by converting the
    first JSONL entry's timestamp to local time via
    ``datetime.fromtimestamp(ts)`` (no tz arg → system local timezone),
    matching the system's interpretation of "today" as the hook would have
    seen it. Parsing mirrors obsidian_utils._parse_ts (ISO variants + epoch
    seconds/millis) via _parse_ts_epoch.

    Falls back to today's local date when the timestamp is absent or
    unparsable.
    """
    epoch = _parse_ts_epoch(first_ts_raw) if first_ts_raw is not None else None
    if epoch is not None:
        try:
            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            pass
    return datetime.now().strftime("%Y-%m-%d")


# Trailing 4-char hash of a SESSION note filename: <date>-<slug>-<hash4>.md
_HASH_RE = re.compile(r"-([0-9a-f]{4})\.md$")
# Snapshot note filename: <date>-<slug>-<hash4>-snapshot[-<HHMMSS>].md —
# written by obsidian_context_snapshot via make_filename(..., suffix=
# "-snapshot-<HHMMSS>"); the optional group also matches pre-spec
# "-snapshot.md" names (see obsidian_utils.find_snapshots_for_session).
_SNAPSHOT_HASH_RE = re.compile(r"-([0-9a-f]{4})-snapshot(?:-\d{6})?\.md$")


def _index_session_notes(
    vault: Path, sessions_folder: str
) -> tuple[set[str], set[str], set[str], set[str], int]:
    """Build coverage indexes for fast JSONL lookup.

    Returns:
        sid_set:    set of ``session_id`` frontmatter values found in session notes.
        hash_set:   set of 4-char filename-hash suffixes (``*-<hash4>.md``) used
                    as a LEGACY fallback. A hash enters this set only when the
                    note lacks a ``session_id`` frontmatter field (true legacy)
                    or its read failed — modern notes are covered via sid_set
                    and must not become collision candidates that could
                    silently hide gaps behind a 4-hex-char match.
        snapshot_sids: set of ``session_id`` frontmatter values found in
                    SNAPSHOT notes (``*-<hash4>-snapshot*.md`` /
                    ``type: claude-snapshot``). Snapshots live in the sessions
                    folder and carry the session's ``session_id``, but a
                    snapshot is NOT the session note — they are excluded from
                    sid_set / hash_set (otherwise a missing session note would
                    look covered by its own snapshot). These sids feed the
                    snapshot-anchor threshold bypass in scan(); keying on
                    session_id mirrors the hook's find_snapshots_for_session
                    (frontmatter sid match) exactly.
        snapshot_hashes: filename-hash4 fallback for snapshot notes whose
                    frontmatter is unreadable or lacks ``session_id`` —
                    degraded (collision-prone) bypass signal only.
        unreadable: count of session notes whose read failed (sid-index is
                    degraded for these; only the hash fallback covers them).
    """
    sessions_dir = vault / sessions_folder
    sid_set: set[str] = set()
    hash_set: set[str] = set()
    snapshot_sids: set[str] = set()
    snapshot_hashes: set[str] = set()
    unreadable = 0

    if not sessions_dir.is_dir():
        return sid_set, hash_set, snapshot_sids, snapshot_hashes, unreadable

    for entry in sessions_dir.iterdir():
        if not entry.name.endswith(".md"):
            continue

        snap_m = _SNAPSHOT_HASH_RE.search(entry.name)
        if snap_m:
            # Snapshot note: record its owning session for the threshold
            # bypass; never let it provide session coverage. Prefer the
            # frontmatter session_id (exact — mirrors the hook's
            # find_snapshots_for_session, which matches on frontmatter sid);
            # fall back to the filename hash4 when the read fails or the
            # field is absent. The hash4 fallback is collision-prone: two
            # unrelated sids can share a 4-hex hash (observed in practice),
            # which would bypass thresholds for the wrong session.
            try:
                snap_fm = _parse_frontmatter(
                    entry.read_text(encoding="utf-8", errors="replace"),
                    source=str(entry),
                )
                snap_sid = snap_fm.get("session_id", "")
            except OSError as exc:
                snap_sid = ""
                print(
                    f"[session-coverage] WARNING: could not read snapshot note "
                    f"{entry} ({exc}); bypass falls back to filename hash",
                    file=sys.stderr,
                )
            if snap_sid:
                snapshot_sids.add(snap_sid)
            else:
                snapshot_hashes.add(snap_m.group(1))
            continue

        m = _HASH_RE.search(entry.name)

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
        # Defense-in-depth for renamed snapshots: even when the filename
        # doesn't match the snapshot pattern, a claude-snapshot note must not
        # count as session coverage (it shares the session's session_id).
        if fm.get("type") == "claude-snapshot":
            sid = fm.get("session_id", "")
            if sid:
                snapshot_sids.add(sid)
            continue
        sid = fm.get("session_id", "")
        if sid:
            sid_set.add(sid)
        elif m:
            # True legacy note (no session_id frontmatter) — hash fallback.
            hash_set.add(m.group(1))

    return sid_set, hash_set, snapshot_sids, snapshot_hashes, unreadable


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
        reconstruct:     If True, mark issues resolvable (unresolved=False,
                         confidence=0.9) to enable apply(). Otherwise gaps are
                         unresolved with confidence=0.0.
    """
    vault = Path(vault_path)
    # Path.home() reads $HOME on POSIX (tests monkeypatch it) and falls back
    # to pwd-database lookups when unset — sibling-module convention, more
    # defensive than expanduser-and-walk-up.
    home = Path.home()
    projects_root = home / ".claude" / "projects"

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

    # Step 1: index existing session notes (+ snapshot sids/hashes for the bypass).
    sid_set, hash_set, snapshot_sids, snapshot_hashes, n_unreadable_notes = (
        _index_session_notes(vault, sessions_folder)
    )

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

            user_count, duration_minutes, first_ts_raw, cwd = metrics

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

            h4 = _sid_hash(sid)

            # Step 4: threshold check — skip if hook would have skipped.
            # Replicates obsidian_utils.should_skip_session semantics:
            #   skip if user_count < min_messages           (strictly less-than)
            #   skip if 0 < duration < min_duration_minutes (strictly less-than)
            # Exactly-at-threshold sessions are NOT skipped — they ARE gaps.
            # EXCEPTION: when the session has sibling snapshots, the hook
            # bypasses thresholds and writes the note anyway as a snapshot
            # anchor (obsidian_session_log "snapshot-bypass") — so a
            # below-threshold session WITH snapshots stays gap-eligible.
            below_threshold = (user_count < min_messages) or (
                duration_minutes > 0 and duration_minutes < min_duration
            )
            # sid-exact match mirrors the hook's find_snapshots_for_session;
            # the hash4 set only covers frontmatter-less/unreadable snapshots.
            snapshot_bypass = below_threshold and (
                sid in snapshot_sids or h4 in snapshot_hashes
            )
            if below_threshold and not snapshot_bypass:
                n_below_threshold += 1
                continue

            # Step 5: coverage check.
            if sid in sid_set or h4 in hash_set:
                n_covered += 1
                continue

            # Step 6: emit gap Issue.
            n_gaps += 1
            size = st.st_size
            mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            )

            # Compose expected note basename using the same formula as the hook.
            # The hook calls _first_seen_date(sid) which returns local-date when
            # the session started. We derive that from first JSONL timestamp in
            # local time (see module docstring for the reasoning).
            date_str = _first_seen_date_from_ts(first_ts_raw)
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
            if snapshot_bypass:
                reason += (
                    "; below thresholds but session has snapshot(s) — the "
                    "hook's snapshot-anchor bypass would have written the note"
                )

            issues.append(
                Issue(
                    check=NAME,
                    note_path=str(expected_note_path),
                    project=derived_project,
                    current_source=f"{sid}.jsonl ({size} bytes)",
                    proposed_source=f"[[{expected_basename[:-3]}]]",  # strip .md
                    reason=reason,
                    # Resolvable gaps (reconstruct mode) carry 0.9 — the
                    # repair is known and applyable, consistent with other
                    # applyable repairs (canonicalization proposals, audit
                    # category-A restores). Unresolved gaps stay 0.0 so a
                    # --min-confidence threshold > 0 filters them, and never
                    # silently nullifies --reconstruct.
                    confidence=0.9 if reconstruct else 0.0,
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
                        "snapshot_bypass": snapshot_bypass,
                    },
                )
            )

    # Step 7: end-of-scan stderr summary. The five buckets partition the
    # scanned JSONLs exactly; unreadable session notes are an index-side
    # (note-side) counter appended for operator visibility. Printed
    # UNCONDITIONALLY (even when nothing was scanned) — a silent scan is
    # indistinguishable from a scan that never ran.
    total_scanned = n_gaps + n_covered + n_below_threshold + n_unparsable + n_project_filtered
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

        # Coerce outcome to str defensively: a malformed replay payload with
        # a non-string outcome (number, null) must fall through to the
        # per-issue error path below, not raise AttributeError on
        # .startswith() and abort the whole sweep.
        outcome = str(out.get("outcome", "UNKNOWN"))
        if outcome in _SUCCESS_OUTCOMES:
            # vault_writes is the replay's ground truth for what was written
            # (the predicted issue.note_path may differ in date — see docstring:
            # the hook's _first_seen_date returns TODAY for never-written
            # sessions, while the scan predicted from the JSONL first timestamp).
            # Shape-guard the [path, bytes] entries: a malformed payload
            # (non-list outer, empty/non-list inner) becomes a per-issue
            # error Result instead of a TypeError/IndexError crash.
            vault_writes = out.get("vault_writes", [])
            first_entry = None
            if isinstance(vault_writes, list) and vault_writes:
                candidate = vault_writes[0]
                if isinstance(candidate, (list, tuple)) and candidate:
                    first_entry = candidate
            if first_entry is not None:
                actual_path = str(first_entry[0])
                results.append(
                    Result(check=NAME, note_path=actual_path, status="applied")
                )
            elif vault_writes:
                results.append(
                    Result(
                        check=NAME,
                        note_path=issue.note_path,
                        status="error",
                        error=(
                            f"replay reported success ({outcome}) but its "
                            f"vault_writes is malformed "
                            f"({str(vault_writes)[:120]!r}); "
                            f"check whether the note now exists before "
                            f"re-running"
                        ),
                    )
                )
            else:
                results.append(
                    Result(
                        check=NAME,
                        note_path=issue.note_path,
                        status="error",
                        error=(
                            f"replay reported success ({outcome}) but wrote "
                            f"nothing (vault_writes empty); check whether "
                            f"the note now exists before re-running"
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
