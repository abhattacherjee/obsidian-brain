"""
obsidian_utils.py — Shared utilities for obsidian-brain hook scripts.

Extracted from the validated spike (spike_session_log.py) with these changes:
  - No hardcoded config; uses load_config() reading ~/.claude/obsidian-brain-config.json
  - All functions take explicit parameters (vault_path, model, etc.) — no global state
  - File extraction uses tool_use blocks instead of regex heuristics
  - Python stdlib only

Every public function catches its own errors and logs to stderr.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import vault_index as _vault_index
except ImportError as exc:
    _vault_index = None  # type: ignore[assignment]
    print(f"[obsidian-brain] vault_index not available, access tracking disabled: {exc}",
          file=sys.stderr)

# Unguarded (unlike vault_index above) and imported the same way vault_index.py
# imports it: frontmatter.py is stdlib-`re`-only and has no siblings to fail on,
# so there is nothing for a try/except to degrade to — a caller that cannot
# import it cannot parse frontmatter at all. No cycle: this module already
# imports vault_index, which imports frontmatter.
from frontmatter import (  # noqa: E402
    MAX_FRONTMATTER_LINES,
    NO_CLOSING_FENCE_EXHAUSTED_REASON,
    NO_OPENING_FENCE_REASON,
    split_frontmatter,
    split_lines_lf_crlf,
)

# Session IDs are CC UUIDs (or test fixtures). Restrict to safe filename chars
# so the marker path never escapes ~/.claude/obsidian-brain/sessions/.
_SID_FILENAME_SAFE = re.compile(r"\A[A-Za-z0-9._-]{1,128}\Z")


def parse_frontmatter_field(content: str, key: str) -> str | None:
    """Return the YAML scalar value for ``key``, or None.

    Returns None when:
      - content is empty
      - key is absent
      - key is present but value is empty (after stripping horizontal
        whitespace and surrounding quotes)

    Only horizontal whitespace (space, tab) is consumed between the colon
    and the value — never ``\n`` — so an empty ``key:`` line cannot
    capture the next YAML key's value (issue #94).

    Search region:
      - If ``content`` starts with ``---`` and a closing ``\n---`` is
        found, the search region is the frontmatter block up to and
        including the three dashes of the closing fence (no trailing
        newline).
      - If ``content`` starts with ``---`` but no closing ``\n---`` is
        found, the search region is the full ``content`` (best-effort
        for callers like ``vault_stats``'s 2 KB head buffer that may
        truncate before the closing fence).
      - If ``content`` does not start with ``---``, the search region
        is the full ``content``.

    Quote stripping uses ``.strip().strip('"').strip("'")`` to match the
    existing migrated call sites — strictly behaviorally-equivalent reads
    on the happy path. Empty-value semantics intentionally differ from
    the old buggy regex (which could cross newlines into the next YAML
    key); see ``tests/test_frontmatter_field_migration_parity.py`` for
    the full parity matrix and
    ``test_empty_type_treated_as_legacy_keep`` for the type-filter
    behavioral pin.

    Stdlib only.
    """
    if not content:
        return None

    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            search_region = content[: end + 4]
        else:
            search_region = content
    else:
        search_region = content

    pattern = re.compile(rf"^{re.escape(key)}:[ \t]*(.*)$", re.MULTILINE)
    m = pattern.search(search_region)
    if not m:
        return None

    value = m.group(1).strip().strip('"').strip("'")
    if not value:
        return None
    return value


def _first_seen_date(sid: str) -> str:
    """Return the canonical first-seen calendar date for a session_id.

    Atomic, idempotent, lazy: marker is written on first call and read
    verbatim on subsequent calls — every subsequent call (across days,
    worktrees, processes) returns the same date. If the marker becomes
    corrupt or unreadable (e.g., truncated by an external process), the
    function self-heals by rewriting it with today's date — note that
    this resets the lockstep guarantee for that session_id.

    Silent-failure paths (all log to stderr): unsafe sid shape, mkdir
    failure, marker write failure — fall back to today's date and lockstep
    is voided for that call. Used by get_session_context() and SessionEnd
    so that source_session_note wikilinks and on-disk filenames stay in
    lockstep even when sessions cross midnight.

    Marker location: ~/.claude/obsidian-brain/sessions/<sid>.json (0o600).
    """
    if not _SID_FILENAME_SAFE.fullmatch(sid):
        print(
            f"[obsidian-brain] _first_seen_date: refusing unsafe sid shape; "
            f"falling back to today's date",
            file=sys.stderr,
        )
        return datetime.date.today().isoformat()

    marker_dir = Path.home() / ".claude" / "obsidian-brain" / "sessions"
    try:
        marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if marker_dir.stat().st_mode & 0o077:
            os.chmod(marker_dir, 0o700)
    except OSError as exc:
        print(f"[obsidian-brain] _first_seen_date: cannot create marker dir: {exc}",
              file=sys.stderr)
        return datetime.date.today().isoformat()  # graceful fallback

    marker = marker_dir / f"{sid}.json"
    try:
        date = json.loads(marker.read_text(encoding="utf-8"))["first_seen_date"]
        # Self-heal mode if a previous bug or manual edit left it permissive.
        try:
            if marker.stat().st_mode & 0o077:
                os.chmod(marker, 0o600)
        except OSError:
            pass  # mode-tightening is best-effort; readback already succeeded
        return date
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError):
        pass  # fall through and (re)write

    today = datetime.date.today().isoformat()
    payload = {
        "first_seen_date": today,
        "first_seen_iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{sid}.", suffix=".json.tmp", dir=str(marker_dir)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, marker)  # atomic on POSIX
        tmp_path = None  # rename succeeded; nothing to clean up
    except OSError as exc:
        print(f"[obsidian-brain] _first_seen_date: marker write failed: {exc}",
              file=sys.stderr)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError as cleanup_exc:
                print(
                    f"[obsidian-brain] _first_seen_date: failed to clean up temp file "
                    f"{tmp_path}: {cleanup_exc}",
                    file=sys.stderr,
                )
    return today


# ---------------------------------------------------------------------------
# Retro-classification gate helpers
# ---------------------------------------------------------------------------

# Sanitize session_id to safe filename characters.
_RETRO_SID_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# Single source of truth for the retro-gate TTL. Imported by
# hooks/obsidian_retro_gate.py so the value is never duplicated.
RETRO_GATE_TTL_SECONDS = 7200  # 2 hours


def _retro_gate_dir() -> Path:
    """Return the retro-gate sentinel directory, computed at call time."""
    return Path.home() / ".claude" / "obsidian-brain" / "retro-gate"


def _reap_stale_retro_sentinels() -> int:
    """Delete retro-gate sentinels whose mtime is older than RETRO_GATE_TTL_SECONDS.

    Uses mtime (not JSON content) so corrupt or foreign files are handled safely.
    Best-effort: OSErrors on individual files are swallowed.  Returns the count of
    files reaped (0 when the gate dir is absent or no files qualify).
    """
    gate_dir = _retro_gate_dir()
    if not gate_dir.exists():
        return 0
    cutoff = time.time() - RETRO_GATE_TTL_SECONDS
    reaped = 0
    try:
        candidates = list(gate_dir.glob("*.json"))
    except OSError:
        return 0
    for f in candidates:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                reaped += 1
        except OSError:
            continue
    return reaped


def mark_retro_classification_pending(session_id: str, retro_path: str) -> str:
    """Write a retro-classification-pending sentinel atomically.

    Sentinel location: ~/.claude/obsidian-brain/retro-gate/<sanitized_sid>.json
    Sentinel content: {"session_id": str, "retro_path": str, "created_at": float}
    Permissions: dir 0o700, file 0o600.

    Returns the sentinel path as a string, or "" if session_id is falsy or
    writing fails (silent-failure — swallows errors, always returns).
    """
    if not session_id:
        return ""

    sanitized = _RETRO_SID_SAFE.sub("_", session_id)
    if not sanitized:
        return ""

    gate_dir = _retro_gate_dir()
    try:
        gate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if gate_dir.stat().st_mode & 0o077:
            os.chmod(gate_dir, 0o700)
    except OSError as exc:
        print(f"[obsidian-brain] mark_retro_classification_pending: cannot create gate dir: {exc}",
              file=sys.stderr)
        return ""

    # Opportunistically reap stale orphaned sentinels. Wrapped in its own
    # try/except so a reap failure can never break the mark operation.
    try:
        _reap_stale_retro_sentinels()
    except Exception as exc:
        print(f"[obsidian-brain] mark_retro_classification_pending: reap failed (non-fatal): {exc}",
              file=sys.stderr)

    sentinel = gate_dir / f"{sanitized}.json"

    # Path-containment check: sanitized name must not escape gate_dir.
    try:
        sentinel.resolve().relative_to(gate_dir.resolve())
    except ValueError:
        print(f"[obsidian-brain] mark_retro_classification_pending: sentinel path escapes gate dir",
              file=sys.stderr)
        return ""

    payload = {
        "session_id": session_id,
        "retro_path": retro_path,
        "created_at": time.time(),
    }
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{sanitized}.", suffix=".json.tmp", dir=str(gate_dir)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, sentinel)  # atomic on POSIX
        tmp_path = None
        return str(sentinel)
    except OSError as exc:
        print(f"[obsidian-brain] mark_retro_classification_pending: write failed: {exc}",
              file=sys.stderr)
        return ""
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def clear_retro_classification_pending(session_id: str) -> bool:
    """Delete the retro-classification-pending sentinel if present.

    Returns True if the sentinel existed (and was removed), False otherwise.
    Never raises — swallows all errors.
    """
    if not session_id:
        return False

    sanitized = _RETRO_SID_SAFE.sub("_", session_id)
    if not sanitized:
        return False

    gate_dir = _retro_gate_dir()
    sentinel = gate_dir / f"{sanitized}.json"

    # Path-containment check.
    try:
        sentinel.resolve().relative_to(gate_dir.resolve())
    except ValueError:
        return False

    try:
        sentinel.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def get_retro_classification_pending(session_id: str) -> dict | None:
    """Return the parsed sentinel dict for session_id, or None.

    Returns None if the sentinel is absent, unreadable, or malformed (missing
    required keys). Never raises.
    """
    if not session_id:
        return None

    sanitized = _RETRO_SID_SAFE.sub("_", session_id)
    if not sanitized:
        return None

    gate_dir = _retro_gate_dir()
    sentinel = gate_dir / f"{sanitized}.json"

    # Path-containment check.
    try:
        sentinel.resolve().relative_to(gate_dir.resolve())
    except ValueError:
        return None

    try:
        data = json.loads(sentinel.read_text(encoding="utf-8"))
        # Require the three expected keys to guard against corrupt writes.
        if not all(k in data for k in ("session_id", "retro_path", "created_at")):
            return None
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# One read block. Measured against the live vault (2098 notes): 2082 of them
# close their frontmatter fence inside the FIRST block, and the deepest fence
# in the vault (index 460 -- the 461st line -- of an /emerge note, 23.7 KB
# in) needs three.
_FRONTMATTER_READ_BLOCK = 8192

# Second, cruder backstop on the same read. MAX_FRONTMATTER_LINES is counted
# in "\n"s, so a file that contains NO "\n" at all -- a CR-only (classic-Mac)
# note, or a single-line minified blob -- never trips it and would be read to
# EOF at any size. 2 MB is ~17x the largest note in the live vault (117 KB)
# and ~85x its deepest frontmatter region (24 KB).
_FRONTMATTER_MAX_CHARS = 2_000_000

# The stable prefix every "the read itself failed" reason starts with, shared
# by this module's reader and vault_index._parse_note_detailed. Kept as a
# constant because two different things key off the exact wording: the egress
# helper (_describe_note_parse_failure) lets reasons with this prefix through
# uncategorized because they are content-free by construction, and
# vault_index._classify_parse_failure maps it to "unreadable".
_UNREADABLE_REASON_PREFIX = "unreadable file:"

# Reported when _FRONTMATTER_MAX_CHARS -- NOT the line cap -- stops the read.
# The "frontmatter exceeds" prefix is load-bearing: it is what
# vault_index._classify_parse_failure maps to "frontmatter_too_long", which is
# what actually happened. Letting split_frontmatter diagnose this instead
# yields "no closing '---'" for a note whose fence may sit just past the cut,
# i.e. the wrong-diagnosis failure frontmatter.py's own docstring calls out as
# the one that "sends someone to repair a file that is not broken".
_FRONTMATTER_TOO_LARGE_REASON = (
    f"frontmatter exceeds {_FRONTMATTER_MAX_CHARS} characters (read limit "
    "reached before the frontmatter block ended -- the note may be fine; "
    "this is a size limit, not a missing fence)"
)

# A COMPLETE closing-fence line, expressed as raw text rather than as a line
# index. Every "\n" in a file is a line terminator (either on its own or as
# the second half of a "\r\n"), so a match means: the previous line ended,
# then a line whose entire content is "---" began AND ended. That is exactly
# "a terminated line at index >= 1 whose rstrip('\r\n') == '---'", which is
# the only thing split_frontmatter accepts as a closing fence. Matching on
# raw text instead of splitting the accumulated block into lines after every
# read is a performance requirement, not a style choice: split_lines_lf_crlf
# is a per-character Python loop, so splitting each 8 KB block would cost
# ~8000 iterations per note in the reaper's hot path, where the whole budget
# is 5 seconds for ~1000 notes. str.find is C-speed and lets us split only
# the frontmatter region itself (typically a few hundred characters).
_CLOSING_FENCE_MARKERS = ("\n---\n", "\n---\r\n")


def _find_closing_fence_end(text: str, start: int) -> int:
    """Return the index just past the earliest complete closing-fence line in
    ``text`` at or after ``start``, or -1 if there is none.

    "Complete" is load-bearing: the marker includes the fence's OWN
    terminator, so a fence straddling a read-block boundary ("\\n--" in one
    block, "-\\n" in the next) is not mistaken for a finished one. Callers
    that resume the search must rewind by ``len(marker) - 1`` for the same
    reason -- see _read_frontmatter_region.
    """
    best_start = -1
    best_end = -1
    for marker in _CLOSING_FENCE_MARKERS:
        i = text.find(marker, start)
        if i != -1 and (best_start == -1 or i < best_start):
            best_start, best_end = i, i + len(marker)
    return best_end


def _read_frontmatter_region(
    path: str | Path,
) -> tuple[list[str] | None, str | None, str | None]:
    """Read only as far into *path* as ``split_frontmatter`` can look.

    Returns ``(lines, read_error, size_caveat)``:

    * ``lines`` in ``split_lines_lf_crlf`` form (terminators attached), or
      None if the file could not be read at all.
    * ``read_error`` -- set only when ``lines`` is None, using the same
      ``"unreadable file: ..."`` shape as ``vault_index._parse_note_detailed``.
    * ``size_caveat`` -- set (to ``_FRONTMATTER_TOO_LARGE_REASON``) when the
      CHARACTER cap cut the read short, i.e. the returned lines are a prefix.
      Callers must prefer this string over ``split_frontmatter``'s
      BARE-EXHAUSTION verdict (``NO_CLOSING_FENCE_EXHAUSTED_REASON``) and over
      that one only -- see (3) below. The other two verdicts survive
      truncation intact: each is derived from a line that was actually read
      and inspected (``lines[0]`` for the missing opening fence, the named
      offending line for the shape stop), so the prefix cannot have hidden
      what they report.

    Three return values rather than an early return on the character cap, and
    that is load-bearing rather than a wide type for its own sake. Under the
    LINE cap, ``newlines > MAX_FRONTMATTER_LINES`` must fire first, so at
    least ``MAX + 1`` TERMINATED lines already occupy indices 0..MAX and any
    unterminated tail necessarily sits at index >= ``MAX + 1``, where the
    ``lines[:MAX_FRONTMATTER_LINES + 1]`` slice discards it regardless. Only
    the CHARACTER cap can seat a fragment at a low index. So bailing out early
    on ``char_capped`` -- returning the caveat without lines -- would make the
    ``lines.pop()`` fragment-drop below provably unreachable, and a dead guard
    is worse than a wide return type: the next reader deletes it as obviously
    redundant, and it stops being dead the moment the caps change.

    Bounded on purpose, and bounded three ways. The callers run in loops over
    the whole vault -- ``build_context_brief`` touches ~1000 session notes +
    ~1100 insight notes, and ``obsidian_session_reaper._build_existing_sid_set``
    runs on the SessionStart hook inside a 5-second budget -- so reading whole
    files to find a fence that lives in the first few lines is not affordable.
    The read stops at whichever of these comes first:

    1. **A complete closing fence** (``_find_closing_fence_end``). This is the
       bound that actually fires on real notes, and it is the whole point:
       0 of 2098 live vault notes exceed ``MAX_FRONTMATTER_LINES``, so a
       line-count-only stop degenerates into a whole-file slurp -- measured at
       99.3% of the vault's 24.1 MB, and a 47-53x slowdown of
       ``_build_existing_sid_set``. Stopping at the fence is parse-identical:
       ``split_frontmatter`` returns at the first such line and never inspects
       anything past it, and no caller of this function uses ``body_lines``.
    2. **``MAX_FRONTMATTER_LINES`` newlines** -- the backstop for a
       pathological file whose fence never arrives. Truncating to
       ``MAX_FRONTMATTER_LINES + 1`` lines is semantics-preserving for
       ``split_frontmatter``: it only ever inspects
       ``lines[1:min(len(lines), MAX_FRONTMATTER_LINES + 1)]`` (so index
       ``MAX_FRONTMATTER_LINES`` is the deepest line it can read), and its
       "frontmatter exceeds N lines" branch triggers on
       ``len(lines) > MAX_FRONTMATTER_LINES`` -- a ``MAX + 1``-line prefix
       reproduces both exactly.
    3. **``_FRONTMATTER_MAX_CHARS``** -- because (2) counts ``"\\n"`` only, so
       a CR-only note or a minified single-line blob has zero newlines and
       would otherwise be read to EOF at any size.

    (3) is the one stop that is NOT semantics-preserving: for a file holding
    >2 MB of text before its 1001st newline, an unbounded read could still
    have found a fence further in. That shape does not exist in the live vault
    (largest note: 117 KB; deepest frontmatter region: 24 KB) and a bounded
    read is the deliberate trade -- but the note must not then be ACCUSED of
    being broken, which is why (3) reports ``size_caveat``. Left to
    ``split_frontmatter``, a char-capped read renders as "no closing '---'"
    for a note whose fence is merely past the cut: the wrong-diagnosis failure
    ``frontmatter.py``'s own docstring calls out as the one that "sends
    someone to repair a file that is not broken". (2) needs no such caveat --
    a MAX+1-line prefix reproduces ``split_frontmatter``'s own "frontmatter
    exceeds N lines" branch exactly.

    A possibly-truncated trailing line is never handed to the parser either
    way: the tail is kept only on a clean EOF, and dropped whenever a cap cut
    the read short.
    """
    text = ""
    scan_from = 0
    fence_end = -1
    truncated = False
    char_capped = False
    newlines = 0
    try:
        # newline="" (universal-newline translation OFF) is required here,
        # not cosmetic: split_lines_lf_crlf below is documented to treat a
        # bare "\r" as NOT a line terminator. Opening with the default
        # newline=None translates every bare "\r" to "\n" before the
        # splitter ever sees it, silently defeating that guarantee. Same
        # reasoning as vault_index._parse_note_detailed (#277 follow-up).
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            while True:
                block = fh.read(_FRONTMATTER_READ_BLOCK)
                if not block:
                    break  # clean EOF
                text += block
                # "\r\n" contains "\n", so this counts CRLF lines correctly;
                # a bare "\r" is deliberately not counted (see above).
                newlines += block.count("\n")
                fence_end = _find_closing_fence_end(text, scan_from)
                if fence_end != -1:
                    break
                # Rewind by len("\n---\r\n") - 1 so a fence split across this
                # boundary is still found on the next pass.
                scan_from = max(0, len(text) - 5)
                if newlines > MAX_FRONTMATTER_LINES:
                    truncated = True
                    break
                if len(text) >= _FRONTMATTER_MAX_CHARS:
                    # Checked AFTER the line cap so that when both would fire
                    # the semantics-preserving path wins: a MAX+1-line prefix
                    # lets split_frontmatter reach its own "frontmatter exceeds
                    # N lines" branch. The char cap has no such equivalent, so
                    # it is the one that has to be reported -- but the lines
                    # are still returned, because the caller still has to
                    # decide whether the prefix parses at all.
                    truncated = True
                    char_capped = True
                    break
    except OSError as exc:
        # exc.strerror (e.g. "No such file or directory"), NOT str(exc):
        # str(OSError) embeds the full path argument, which would leak the
        # absolute vault path into this reason string and, from there, into
        # /retro's discovery_errors and the model's context. Same rule (and
        # same reason) as vault_index._parse_note_detailed.
        return None, f"{_UNREADABLE_REASON_PREFIX} {exc.strerror or type(exc).__name__}", None

    if fence_end != -1:
        # Cut at the fence: everything past it is body, and splitting it
        # would be the per-character cost this function exists to avoid.
        text = text[:fence_end]
    lines = split_lines_lf_crlf(text)
    if truncated and lines and not lines[-1].endswith("\n"):
        # A cap stopped the read mid-line. That trailing element is a
        # fragment, not a line, so it must never reach the parser -- a
        # fragment that happens to read "---" would forge a closing fence and
        # hand back body prose as fields, which is #283 itself.
        #
        # This matters for the CHARACTER cap specifically. Under the line cap
        # the fragment necessarily sits at index >= MAX_FRONTMATTER_LINES + 1
        # (the cap needs MAX+1 newlines before it fires, so the unterminated
        # tail is line MAX+2 at the earliest) and the slice below would drop
        # it anyway; under the char cap a 2 MB file of few long lines puts the
        # fragment at a low index, well inside the slice.
        #
        # Under the char cap this pop is not merely live but OBSERVABLE, and
        # it is what makes the caveat above always consumed rather than
        # silently discarded. Reaching here with char_capped set means
        # fence_end == -1, and the rewind (`len(text) - 5`) guarantees no
        # marker was skipped across a block boundary -- so `text` contains no
        # complete "\n---\n"/"\n---\r\n" at all. A TERMINATED fence line at
        # index >= 1 would have produced one, therefore the only fence that
        # can exist here is a final UNTERMINATED "---", which is exactly what
        # this pop removes. Hence split_frontmatter can never succeed on a
        # char-capped read: it always returns an error, and the caller always
        # has a verdict to weigh the caveat against. Remove the pop and
        # split_frontmatter SUCCEEDS instead, returning fabricated fields
        # harvested from a truncated prefix -- a loud, testable failure rather
        # than a quiet one.
        lines.pop()
    return (
        lines[:MAX_FRONTMATTER_LINES + 1],
        None,
        _FRONTMATTER_TOO_LARGE_REASON if char_capped else None,
    )


def _peek_frontmatter_fields(
    path: Path, fields: tuple[str, ...]
) -> dict[str, str | None]:
    """Return ``{field: unquoted YAML scalar or None}`` for each name in
    *fields*, from a single read + single parse of *path*'s frontmatter.

    This is the only implementation; ``_peek_frontmatter_field`` is a
    one-field wrapper over it. It exists because
    ``obsidian_session_reaper._build_existing_sid_set`` needs ``type``,
    ``project`` and ``session_id`` from every session note on the SessionStart
    hook: three single-field calls meant three uncached reads and three
    parses of the same bytes, ~1000 notes deep, inside a 5-second budget.

    The frontmatter block is located by ``frontmatter.split_frontmatter`` (via
    ``_read_frontmatter_region``), the same bounded, shape-checked scan the
    index uses. Per field, the FIRST ``field:``-prefixed line inside the fence
    pair wins; quote-stripping handles the common cases ``"value"`` and
    ``'value'``; unquoted scalars are returned verbatim. Empty values
    (``field:`` with no scalar) are reported to stderr and normalized to None
    for clean truthy-checks at call sites.
    """
    result: dict[str, str | None] = {field: None for field in fields}
    lines, read_err, _size_caveat = _read_frontmatter_region(path)
    if lines is None:
        print(
            f"[obsidian-brain] _peek_frontmatter_field: cannot read {path.name} "
            f"for fields={list(fields)!r}: {read_err}; this file will be "
            f"excluded from filtering",
            file=sys.stderr,
        )
        return result

    _open_fence, fm_lines, _close_fence, _body_lines, split_err = split_frontmatter(lines)
    if split_err:
        # No opening fence, no closing fence, or an oversized block: there is
        # no frontmatter region to read a field out of. Returning a match from
        # the unfenced region would be the forged-field bug (#283).
        return result

    remaining = set(fields)
    for raw_line in fm_lines:
        if not remaining:
            break
        stripped = raw_line.strip()
        for field in tuple(remaining):
            if not stripped.startswith(f"{field}:"):
                continue
            remaining.discard(field)  # first match wins, as in the 1-field scan
            value = stripped[len(field) + 1:].strip()
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            if not value:
                print(
                    f"[obsidian-brain] _peek_frontmatter_field: {path.name} has empty "
                    f"{field!r} field — possible mid-write or corruption",
                    file=sys.stderr,
                )
                break
            result[field] = value
            break
    return result


def _peek_frontmatter_field(path: Path, field: str) -> str | None:
    """Return the unquoted YAML scalar for ``field:`` from a vault note's
    frontmatter, or None. One-field wrapper over ``_peek_frontmatter_fields``
    so there is exactly one parsing implementation.

    The frontmatter block is located by ``frontmatter.split_frontmatter``, the
    same bounded, shape-checked scan the index uses. This replaces a 30-line
    bound that missed fields in any note with a long ``tags:``/``projects:``
    block, and — more dangerously — a scan that returned whatever
    ``field:``-shaped line it found even when no closing ``---`` was ever
    seen, i.e. a value scavenged from body prose (#283).

    Two shape rules are TIGHTER than the old hand-rolled scan, both inherited
    from the shared parser and both affecting 0 of 2098 live vault notes:

    - The opening ``---`` must be at line index 0. The old scan accepted it
      anywhere in the first 30 lines, so a note with leading blank lines (or
      any preamble) used to parse; now it returns None.
    - The closing fence is matched with ``rstrip("\\r\\n")``, not ``.strip()``.
      A fence written with trailing spaces (``---␣␣``) no longer closes the
      block, so such a note returns None instead of its field values.
    """
    return _peek_frontmatter_fields(path, (field,))[field]


def _peek_frontmatter_type(path: Path) -> str | None:
    return _peek_frontmatter_field(path, "type")


def _peek_frontmatter_project_path(path: Path) -> str | None:
    return _peek_frontmatter_field(path, "project_path")


def _resolve_session_note_by_hash(
    sessions_dir: Path | str,
    h: str,
    cwd: str | None = None,
) -> tuple[str | None, list[str]]:
    """Resolve ``*-{h}.md`` to a session-type note matching this project.

    Replaces first-match-wins glob discipline with a type+project filter
    that handles the three known collision modes:
      1. session_id sharing with snapshot notes (#101 Fix C)
      2. cross-project 4-char hash collision (#101 Fix C)
      3. genuine same-session_id duplicates (subsumes #86)

    Returns ``(basename_without_ext, collisions)``:
      - basename_without_ext: file stem (no ``.md``) when resolved
      - collisions: list of full filenames *with* ``.md`` so stderr
        warnings are unambiguous when shown to the operator

      - exactly one session-type match              → (basename, [])
      - 2+ session matches but exactly one cwd match → (basename, [other names])
      - 0 session-type matches                       → (None, [])
      - 2+ matches and ambiguous after cwd filter    → (None, [all session names])

    Caller pattern: warn on non-empty ``collisions`` and fall back to a
    composed name (via ``make_filename(_first_seen_date(sid), project, sid)``)
    when ``basename is None``.
    """
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        print(
            f"[obsidian-brain] _resolve_session_note_by_hash: sessions_dir "
            f"{sessions_dir} does not exist or is not readable; treating as no-match",
            file=sys.stderr,
        )
        return None, []

    try:
        matches = sorted(sessions_dir.glob(f"*-{h}.md"))
    except OSError as exc:
        # Permission errors / transient I/O on the sessions dir must not crash
        # SessionEnd. Fall back to (None, []) so callers compose a fresh name.
        print(
            f"[obsidian-brain] _resolve_session_note_by_hash: glob failed on "
            f"{sessions_dir}: {exc}",
            file=sys.stderr,
        )
        return None, []
    # Treat type=None as claude-session for backward-compat with legacy notes
    # that pre-date the explicit type frontmatter field — matches the same
    # convention used by collect_open_items() in hooks/open_item_dedup.py.
    session_matches = [m for m in matches
                       if (_peek_frontmatter_type(m) or "claude-session") == "claude-session"]
    if not session_matches:
        return None, []

    # Apply cwd filter when provided — covers single-match cross-project collision
    if cwd:
        cwd_matches = [m for m in session_matches
                       if _peek_frontmatter_project_path(m) == cwd]
        if len(cwd_matches) == 1:
            others = [m.name for m in session_matches if m != cwd_matches[0]]
            return cwd_matches[0].stem, others
        if len(cwd_matches) == 0:
            # No cwd match — surface ALL the cross-project matches as collisions
            # so caller can warn, then fall back to composed name.
            return None, [m.name for m in session_matches]
        # Multiple cwd matches — fall through to ambiguous return below

    # No cwd OR multiple cwd matches — leniently return single-match basename
    if len(session_matches) == 1:
        return session_matches[0].stem, []
    return None, [m.name for m in session_matches]


def _safe_getcwd() -> str:
    """Return os.getcwd() or empty string if cwd is deleted/unmounted.

    Hook paths must not crash on cwd-gone (issue #105 territory) — callers
    that pass this to _resolve_session_note_by_hash get the (None, []) /
    (None, [...]) lenient fallback when cwd resolution fails.
    """
    try:
        return os.getcwd()
    except (OSError, FileNotFoundError):
        return ""


def _resolve_project_basename() -> str | None:
    """Project basename for CC-path lookups (~/.claude/projects/, sid-* bootstraps).

    Resolution order:
      1. os.getcwd() — normal case
      2. CLAUDE_PROJECT_DIR env var — when cwd is gone (worktree deleted
         mid-session via `gh pr merge --delete-branch`); see issue #105
      3. None — caller should treat as 'cannot determine project' and
         fall through to project-agnostic fallbacks

    Never raises. Preserves a lazy fallback order: consult CLAUDE_PROJECT_DIR
    only after cwd resolution fails.

    Falsy basenames (empty string from cwd='/' or env var with trailing slash)
    are normalized to None so callers fall through to safer fallback layers
    instead of triggering an unscoped cross-project glob (which would
    silently mis-attribute the active session).
    """
    try:
        cwd_base = os.path.basename(os.getcwd())
        return cwd_base if cwd_base else None
    except OSError:
        env = os.environ.get("CLAUDE_PROJECT_DIR")
        if not env:
            return None
        env_base = os.path.basename(env.rstrip("/"))
        return env_base if env_base else None


def _recent_bootstrap_sid(window_seconds: int = 600) -> str | None:
    """Final-fallback session-id resolver for issue #105 (cwd-gone scenario).

    Scans ~/.claude/obsidian-brain/sid-* for files with mtime within the recency
    window (default 10 min — covers any normal SessionStart-to-/retro interaction
    window). Returns the SID iff exactly ONE recent file is found. Strict by
    design: zero or 2+ matches return None to prevent silent mis-attribution
    across projects (the same bug class as issue #101).

    NOTE: bootstrap files are written exactly once by SessionStart and immutable
    thereafter — so mtime IS capture time for them. This is the opposite of the
    `technical_mtime_not_capture_time` warning, which applies to vault notes
    edited by /check-items, /link, etc.

    Reads the bootstrap dir via os.path.expanduser at call time (not the
    module-level _SECURE_DIR constant) so HOME-redirecting test fixtures work.
    """
    import time
    bdir = os.path.expanduser("~/.claude/obsidian-brain")
    cutoff = time.time() - window_seconds
    try:
        entries = os.listdir(bdir)
    except OSError:
        return None

    candidates: list[str] = []
    for name in entries:
        if not name.startswith("sid-") or name.endswith(".tmp"):
            continue
        path = os.path.join(bdir, name)
        if _safe_mtime(path) < cutoff:
            continue
        try:
            with open(path, "r") as f:
                content = f.read().strip()
        except OSError:
            continue
        if not content:
            continue
        # Validate SID format before trusting the file content. Without this,
        # a corrupted or attacker-controlled bootstrap file could propagate
        # path-traversal-style strings (e.g., "../foo") into cache_get/cache_set
        # path composition. _SID_FILENAME_SAFE is [A-Za-z0-9._-]{1,128}.
        if not _SID_FILENAME_SAFE.fullmatch(content):
            continue
        candidates.append(content)
        if len(candidates) > 1:
            return None  # short-circuit — strict exactly-one

    return candidates[0] if len(candidates) == 1 else None


# --- Secure working directory ---
# All temp/cache files use ~/.claude/obsidian-brain/ (0o700) instead of /tmp.
# This prevents symlink attacks and cache poisoning on multi-user systems.

_SECURE_DIR = os.path.expanduser("~/.claude/obsidian-brain")


def _ensure_secure_dir() -> str:
    """Create and return the secure working directory with 0o700 permissions."""
    os.makedirs(_SECURE_DIR, mode=0o700, exist_ok=True)
    st = os.stat(_SECURE_DIR)
    if st.st_mode & 0o077:
        os.chmod(_SECURE_DIR, 0o700)
    return _SECURE_DIR


# --- Cross-plugin hook dedup guard (standalone-marketplace migration) ---
# When both the monorepo and standalone obsidian-brain plugins are installed,
# every lifecycle hook fires once per installed plugin copy. These coordinate
# via a lock file under the install-source-independent secure dir so only one
# copy acts.

_LOCK_DIR = os.path.join(_SECURE_DIR, "locks")
_HOOK_DEDUP_TTL_SECONDS = 15
_LOCK_CLEANUP_MAX_AGE_SECONDS = 2 * 24 * 3600  # prune lock files older than 2 days


def _cleanup_stale_locks(max_age_seconds: int = _LOCK_CLEANUP_MAX_AGE_SECONDS) -> None:
    """Best-effort prune of lock files older than max_age_seconds. Never raises."""
    try:
        now = time.time()
        for name in os.listdir(_LOCK_DIR):
            p = os.path.join(_LOCK_DIR, name)
            try:
                if now - os.stat(p).st_mtime > max_age_seconds:
                    os.unlink(p)
            except OSError:
                continue
    except OSError:
        pass


def _lock_path(event_type: str, session_id: str) -> str:
    """Composed lock-file path for a (event_type, session_id) trigger.

    Sanitizes both components so they cannot escape _LOCK_DIR. Shared by
    claim_hook_run() and release_hook_run() so the two never drift."""
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:64]
    safe_event = re.sub(r"[^A-Za-z0-9_-]", "_", event_type)[:32]
    return os.path.join(_LOCK_DIR, f"{safe_sid}-{safe_event}")


def claim_hook_run(event_type: str, session_id: str,
                   ttl_seconds: int = _HOOK_DEDUP_TTL_SECONDS) -> bool:
    """Return True if THIS process should handle the (event_type, session_id)
    trigger; False if a sibling plugin copy already claimed it within ttl_seconds.

    Coordinates the double-install case (monorepo + standalone both present) via
    an O_EXCL lock file under _SECURE_DIR/locks/, a fixed path shared by every
    install source. Simultaneous duplicate fires land within the TTL window and
    the loser returns False; legitimate later fires (e.g. a second SessionStart)
    are outside the window and re-claim.

    Taking over a stale lock (a legitimate later fire past the TTL) is not
    atomic: two processes can both observe the stale lock, but the unlink +
    re-create race resolves to one winner and the loser returns False, so the
    failure direction is always "suppress", never "duplicate".

    Fail-open: an empty session_id (no key to dedup on) or any filesystem error
    returns True — a permissions quirk must never silently drop a hook.
    """
    if not session_id:
        return True
    try:
        os.makedirs(_LOCK_DIR, mode=0o700, exist_ok=True)
    except OSError as exc:
        print(f"[obsidian-brain] dedup lock dir unavailable, proceeding: {exc}", file=sys.stderr)
        return True

    lock_path = _lock_path(event_type, session_id)

    payload = f"{os.getpid()} {time.time():.3f}\n".encode("utf-8")

    def _create() -> bool:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        _cleanup_stale_locks()  # only the winner scans the locks dir
        return True

    try:
        return _create()
    except FileExistsError:
        try:
            age = time.time() - os.stat(lock_path).st_mtime
        except OSError:
            return True  # fail open
        if age < ttl_seconds:
            return False  # a sibling plugin copy owns this trigger
        # Stale lock -> a legitimate later fire. Take it over.
        try:
            os.unlink(lock_path)
            return _create()
        except FileExistsError:
            return False  # lost the re-claim race
        except OSError as exc:
            print(f"[obsidian-brain] dedup re-claim failed, proceeding: {exc}", file=sys.stderr)
            return True
    except OSError as exc:
        print(f"[obsidian-brain] dedup claim failed, proceeding: {exc}", file=sys.stderr)
        return True


def release_hook_run(event_type: str, session_id: str) -> None:
    """Release a dedup lock previously taken by claim_hook_run() so a sibling
    plugin copy (or a re-fire of the same trigger) can still handle it.

    Call this when the work AFTER a successful claim fails — e.g. the vault
    write errors or the hook raises. Without it, a transient failure on the
    winning copy would convert a suppressed sibling into a permanently lost
    note. Do NOT call it on success: the lock must persist for its TTL so a
    near-simultaneous sibling fire is still deduplicated. Never raises."""
    if not session_id:
        return
    lock_path = _lock_path(event_type, session_id)
    try:
        # Only release a lock THIS process still owns. claim_hook_run wrote our
        # PID into the payload; if a legitimate later fire took over our stale
        # lock (>TTL), the PID won't match and unlinking its fresh lock would
        # let a sibling re-claim and write a duplicate. On any read/parse error
        # fall through to the unconditional unlink so a transient glitch never
        # strands a claimed-but-failed run (a lost note is worse than a rare dup).
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                owner_pid = int(f.read().split(None, 1)[0])
            if owner_pid != os.getpid():
                return  # taken over by another process — not ours to release
        except (OSError, ValueError, IndexError):
            pass  # unreadable / unparseable — fall through to best-effort unlink
        os.unlink(lock_path)
    except OSError:
        pass  # already gone / never created / unwritable — nothing to undo


# --- Session-scoped cache ---
# Avoids repeated vault scans when multiple skills run in one session.

_CACHE_PREFIX = os.path.join(_SECURE_DIR, "cache-")
_BOOTSTRAP_PREFIX = os.path.join(_SECURE_DIR, "sid-")


def _bootstrap_prefix() -> str:
    """Return the bootstrap file prefix. Fixed to secure directory."""
    return _BOOTSTRAP_PREFIX


# ---------------------------------------------------------------------------
# SessionEnd telemetry log
# ---------------------------------------------------------------------------

_HOOK_LOG_NAME = "obsidian-brain-hook.log"
_HOOK_LOG_MAX_BYTES = 100 * 1024  # 100 KB — same cap as SessionStart log


def _sanitize_log_field(value: str, default: str = "") -> str:
    """Sanitize a log field: strip \\n, \\r, \\t so each event stays on one line."""
    s = value if value else default
    return s.replace("\n", " ").replace("\r", " ").replace("\t", " ")


def _append_sessionend_log(
    project: str,
    session_id: str,
    outcome: str,
    msgs: int = 0,
    dur_min: float = 0.0,
    detail: str = "",
) -> None:
    """Append a one-line SessionEnd outcome record; rotate when oversized.

    Writes to ~/.claude/obsidian-brain-hook.log alongside SessionStart entries
    and the future Reaped entries (issue #125 reaper).

    Best-effort: catches any exception (OSError from filesystem, TypeError
    from bad input types, etc.) and prints a stderr warning. Failure to log
    must not block the SessionEnd hook contract — the hook always exits 0.
    """
    log_dir = os.path.join(os.path.expanduser("~"), ".claude")
    log_path = os.path.join(log_dir, _HOOK_LOG_NAME)
    try:
        os.makedirs(log_dir, exist_ok=True)
        try:
            if os.path.getsize(log_path) > _HOOK_LOG_MAX_BYTES:
                os.replace(log_path, log_path + ".1")
        except FileNotFoundError:
            pass  # no existing log; nothing to rotate
        # Other errors (permission, etc.) propagate to outer except → stderr warning
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        # Sanitize all fields — \n, \r, or \t in any field would corrupt the
        # one-line-per-event format.
        short_sid = _sanitize_log_field((session_id or "unknown")[:8]).replace(" ", "_")
        safe_project = _sanitize_log_field(project, "unknown").replace(" ", "_")
        safe_outcome = _sanitize_log_field(outcome, "UNKNOWN").replace(" ", "_")
        safe_detail = _sanitize_log_field(detail)
        line = (
            f"{timestamp} SessionEnd project={safe_project} sid={short_sid} "
            f"outcome={safe_outcome} msgs={int(msgs)} dur={float(dur_min):.1f} "
            f"detail={safe_detail}\n"
        )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass  # best-effort; chmod failure is not fatal for telemetry
    except Exception as exc:
        print(f"[obsidian-brain] sessionend log append failed: {exc}", file=sys.stderr)


def _append_reaper_log(project: str, sid: Optional[str], event: str, detail: str = "") -> None:
    """Reaper-specific telemetry. Same log file + rotation as SessionEnd
    telemetry, but uses event= keyword to keep enum spaces distinct.

    Per-jsonl events include sid=<short>; summary events pass sid=None.

    Best-effort: any exception (including mkdir failure) is caught and printed
    to stderr so the reaper itself is never interrupted by a log write error.
    """
    try:
        log_dir = Path.home() / ".claude"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = log_dir / _HOOK_LOG_NAME

        # Rotate at 100 KB (mirrors _append_sessionend_log)
        try:
            if log_path.exists() and log_path.stat().st_size >= _HOOK_LOG_MAX_BYTES:
                rotated = log_dir / (_HOOK_LOG_NAME + ".1")
                os.replace(log_path, rotated)
        except OSError:
            pass

        iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        safe_project = _sanitize_log_field(project, "unknown").replace(" ", "_")
        parts = [iso, "Reaper", f"project={safe_project}"]
        if sid:
            safe_sid = _sanitize_log_field(sid).replace(" ", "_")
            parts.append(f"sid={safe_sid}")
        safe_event = _sanitize_log_field(event, "UNKNOWN").replace(" ", "_")
        parts.append(f"event={safe_event}")
        if detail:
            safe_detail = _sanitize_log_field(detail)
            parts.append(f"detail={safe_detail}")
        line = " ".join(parts) + "\n"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
            os.chmod(log_path, 0o600)
        except OSError:
            pass  # best-effort
    except Exception as exc:
        print(f"[obsidian-brain] reaper log append failed: {exc}", file=sys.stderr)


def _safe_mtime(path: str) -> float:
    """Return file mtime, or -1.0 if the path is missing/unstatable.

    Used by _get_session_id_fast() and similar helpers that need best-effort
    mtime comparison over globs — filesystem races (a JSONL rotated between
    glob and stat) must not crash the caller.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return -1.0


# Module-level flag for SF7 one-shot warning. Avoids spamming stderr on every
# canonical_project_name() call when git is unavailable for the whole process.
_git_fallback_warned: bool = False


def _git_canonical_project_name_with_reason(
    cwd: str | None = None,
) -> tuple[str | None, str]:
    """Return (canonical_name_or_None, reason).

    reason is one of:
      - "ok" — name resolved successfully
      - "not-a-repo" — git ran but cwd is not inside a git work-tree (returncode != 0).
        This is a NORMAL operating condition; callers should NOT warn.
      - "git-unavailable" — git binary missing or subprocess raised OSError.
        Genuine error; callers SHOULD warn.
      - "empty-output" — git ran clean but returned no path. Should not happen.
      - "resolve-failed" — relative-path resolve raised OSError.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
    except (OSError, subprocess.SubprocessError):
        return (None, "git-unavailable")
    if result.returncode != 0:
        return (None, "not-a-repo")
    common_dir = result.stdout.strip()
    if not common_dir:
        return (None, "empty-output")
    common_dir_path = Path(common_dir)
    if not common_dir_path.is_absolute():
        base = Path(cwd) if cwd else Path.cwd()
        try:
            common_dir_path = (base / common_dir).resolve()
        except OSError:
            return (None, "resolve-failed")
    name = common_dir_path.parent.name
    if not name:
        return (None, "empty-output")
    return (name, "ok")


def _git_canonical_project_name(cwd: str | None = None) -> str | None:
    """Return the main-repo basename via `git rev-parse --git-common-dir`.

    For a worktree, `--git-common-dir` returns the path to the SHARED .git
    of the main repo (e.g., `/path/main-repo/.git`). The parent's basename
    is the canonical project name across all worktrees.

    Returns None if not in a git repository or git is unavailable. Caller
    should fall back to cwd basename in that case.
    """
    name, _reason = _git_canonical_project_name_with_reason(cwd)
    return name


def canonical_project_name(cwd: str | None = None) -> str:
    """Return the canonical (main-repo) project name for cwd.

    Worktrees of the same repo all return the same canonical name. This
    is what should be written to vault-note frontmatter `project:` fields
    so cross-worktree work groups under one logical project.

    Falls back to cwd basename if not in a git repo. Returns 'unknown' when
    the working directory cannot be determined (e.g., the directory was
    deleted mid-session via `gh pr merge --delete-branch`); hooks must exit
    0, so raising would violate the contract. Result is lowercased and
    underscores/spaces normalized to hyphens.

    Emits a one-shot stderr warning per process ONLY when git is genuinely
    unavailable or errors out (binary missing, empty output, resolve failure).
    Does NOT warn for the normal "cwd is not in a git repo" case — that's a
    common, expected operating condition and warning would be noise (review
    Copilot R2).
    """
    name, reason = _git_canonical_project_name_with_reason(cwd)
    if name is None:
        if reason in ("git-unavailable", "empty-output", "resolve-failed"):
            global _git_fallback_warned
            if not _git_fallback_warned:
                _git_fallback_warned = True
                print(
                    f"[obsidian_utils] canonical_project_name: git error "
                    f"({reason}), using cwd basename — verify project: "
                    f"frontmatter writes",
                    file=sys.stderr,
                )
        try:
            base = cwd if cwd else os.getcwd()
        except (OSError, FileNotFoundError):
            return "unknown"
        name = os.path.basename(base)
    return name.lower().replace(" ", "-").replace("_", "-")


def _glob_project_jsonls(safe_project: str, suffix: str = "*.jsonl") -> list[str]:
    """Glob ~/.claude/projects/*<project>/<suffix>, with underscore-to-hyphen fallback.

    Claude Code normalizes underscores to hyphens in project directory names,
    so a project at ``personal_ws/`` gets stored as ``personal-ws/``.
    """
    import glob as _glob
    pattern = os.path.expanduser(
        f"~/.claude/projects/*{safe_project}/{suffix}"
    )
    matches = _glob.glob(pattern)
    if not matches and "_" in safe_project:
        alt = safe_project.replace("_", "-")
        pattern = os.path.expanduser(f"~/.claude/projects/*{alt}/{suffix}")
        matches = _glob.glob(pattern)
    return matches


def _try_slow_jsonl_glob(project: str) -> str:
    """Slow path: glob all JSONLs under ~/.claude/projects/*<project>/, return
    SID of the newest. Used by both _resolve_session_id (when bootstrap is
    skipped or empty) and as the existing health-check entry point.

    Returns 'unknown' if no JSONLs match. Bootstrap-blind by contract — never
    reads or trusts the sid-<project> bootstrap file.
    """
    import glob as _glob
    safe_project = _glob.escape(project)
    matches = _glob_project_jsonls(safe_project)
    entries = [(_safe_mtime(p), p) for p in matches]
    viable = [(m, p) for m, p in entries if m >= 0]
    if not viable:
        return "unknown"
    _, newest = max(viable)
    return os.path.splitext(os.path.basename(newest))[0]


def _slow_path_newest_sid() -> str:
    """Determine the current session id by scanning JSONL files directly.

    Bootstrap-independent — does NOT read, write, or trust ANY bootstrap
    file (neither the per-project sid-<project> cache nor the cross-project
    recent-bootstrap directory scan). Used by health checks (e.g.,
    check_hook_status) that must not be fooled by stale bootstraps.

    Returns 'unknown' if no JSONLs are found for the current cwd.
    """
    return _resolve_session_id(allow_bootstrap=False)


def _try_bootstrap_fast_path(project: str) -> str | None:
    """Bootstrap fast path: read sid-<project>, validate against JSONL existence
    and newest-mtime tiebreaker. Returns cached SID on hit, None on miss/stale.

    Validation strategy:
      1. Read bootstrap file (~0.1 ms)
      2. Verify cached JSONL still exists
      3. Determine the newest JSONL deterministically via (mtime, path)
         tuple comparison. Ties broken by path string so the result is
         reproducible on filesystems with 1-second mtime resolution.
      4. If the newest JSONL's basename equals the cached sid, trust the
         cache. If the cached JSONL shares the newest mtime (same-second
         race), also trust the cache.
      5. Otherwise return None (let caller fall through to slow path).

    READ-ONLY — never writes the bootstrap file. SessionStart hook is the sole
    authoritative writer.
    """
    import glob as _glob
    bootstrap = f"{_bootstrap_prefix()}{project}"
    safe_project = _glob.escape(project)

    try:
        with open(bootstrap, 'r') as f:
            cached_sid = f.read().strip()
    except OSError:
        return None
    if not cached_sid:
        return None
    # Validate SID format before trusting the bootstrap file content. Without
    # this, a corrupted or attacker-controlled sid-<project> file with content
    # like "../../../tmp/foo" could propagate path-traversal strings into
    # cache_get/cache_set composition. Mirrors the validation in
    # _recent_bootstrap_sid() and _first_seen_date().
    if not _SID_FILENAME_SAFE.fullmatch(cached_sid):
        return None

    safe_cached = _glob.escape(cached_sid)
    cached_matches = _glob_project_jsonls(safe_project, f"{safe_cached}.jsonl")
    if not cached_matches:
        return None

    all_matches = _glob_project_jsonls(safe_project)
    if not all_matches:
        return cached_sid  # no other JSONLs; trust cache

    entries = [(_safe_mtime(p), p) for p in all_matches]
    viable = [(m, p) for m, p in entries if m >= 0]
    if not viable:
        return cached_sid  # no viable JSONLs; trust cache

    newest_mtime, newest_path = max(viable)
    newest_sid = os.path.splitext(os.path.basename(newest_path))[0]
    if newest_sid == cached_sid:
        return cached_sid

    # Tie-breaker: same-second race across worktrees → trust cache
    cached_mtimes = [_safe_mtime(p) for p in cached_matches]
    cached_newest = max((m for m in cached_mtimes if m >= 0), default=-1.0)
    if cached_newest == newest_mtime:
        return cached_sid

    return None  # different session is strictly newer — fall through


def _resolve_session_id(allow_bootstrap: bool = True) -> str:
    """Single source of truth for current-session SID resolution. Never raises.

    Resolution layers (each failure → next):
      1. Project basename via _resolve_project_basename (cwd → env → None)
      2. Bootstrap fast path (skipped if allow_bootstrap=False)
      3. Slow-path JSONL glob
      4. Recent-bootstrap best-effort scan (skipped if allow_bootstrap=False)
         — issue #105 fallback for cwd-gone
      5. 'unknown' sentinel

    The `allow_bootstrap` flag gates BOTH bootstrap-reading layers (2 and 4),
    so callers that need a bootstrap-blind result (e.g., health checks via
    _slow_path_newest_sid) get a JSONL-only resolution.
    """
    project = _resolve_project_basename()
    if project is not None:
        if allow_bootstrap:
            sid = _try_bootstrap_fast_path(project)
            if sid:
                return sid
        sid = _try_slow_jsonl_glob(project)
        if sid != "unknown":
            return sid
    if allow_bootstrap:
        sid = _recent_bootstrap_sid()
        if sid:
            return sid
    return "unknown"


def _get_session_id_fast() -> str:
    """Derive session ID, using bootstrap file for speed on repeat calls.

    See _try_bootstrap_fast_path for the validation strategy and
    _resolve_session_id for the full layered fallback chain (issue #105).
    """
    return _resolve_session_id(allow_bootstrap=True)


def cache_get(session_id: str, key: str):
    """Read a key from the session cache. Returns None on miss."""
    cache_path = f"{_CACHE_PREFIX}{session_id}.json"
    try:
        with open(cache_path, 'r') as f:
            data = json.load(f)
        return data.get(key)
    except (OSError, json.JSONDecodeError):
        return None


def cache_set(session_id: str, key: str, value) -> None:
    """Write a key to the session cache. Atomic write."""
    _ensure_secure_dir()
    cache_path = f"{_CACHE_PREFIX}{session_id}.json"
    try:
        with open(cache_path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            print(f"[obsidian-brain] cache corrupted, resetting: {exc}", file=sys.stderr)
        data = {}

    data[key] = value

    fd, tmp = tempfile.mkstemp(prefix='.ob-cache-', suffix='.json.tmp', dir=_SECURE_DIR)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, cache_path)
    except OSError as exc:
        print(f"[obsidian-brain] cache write failed: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def cache_invalidate(session_id: str, *keys: str) -> None:
    """Remove specific keys from cache. No keys = clear all."""
    cache_path = f"{_CACHE_PREFIX}{session_id}.json"
    if not keys:
        try:
            os.unlink(cache_path)
        except OSError:
            pass
        return

    try:
        with open(cache_path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return

    for k in keys:
        data.pop(k, None)

    _ensure_secure_dir()
    fd, tmp = tempfile.mkstemp(prefix='.ob-cache-', suffix='.json.tmp', dir=_SECURE_DIR)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, cache_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

# Shared cap for the raw-note conversation section. Used by build_raw_fallback
# as the write-time truncation limit, and returned by parse_full_transcript
# so /recall can deterministically detect truncation by comparing the parsed
# message total against it — avoiding any dependence on the raw note's
# message-filtering heuristics.
RAW_NOTE_MAX_TURNS = 120

_CONFIG_PATH = Path.home() / ".claude" / "obsidian-brain-config.json"

_DEFAULTS: dict = {
    "vault_path": "",
    "sessions_folder": "claude-sessions",
    "insights_folder": "claude-insights",
    "dashboards_folder": "claude-dashboards",
    "check_items_folder": "claude-check-items",
    "min_messages": 3,
    "min_duration_minutes": 2,
    "summary_model": "haiku",
    "summary_pipeline": "auto",  # "auto" = Haiku claude -p + sub-agent fallback; "subagent" = skip Haiku pipeline. Consumed by /recall SKILL.md Step 2 (summarization is deferred to /recall), #84
    "summary_batch_size": 3,  # notes per claude -p spawn in upgrade_batch (#166); 1 = legacy per-note fan-out
    "summary_recovery": True,  # #167: post-process loose summaries (heading normalization, synth missing sections, default importance) before escalating/falling back. Set false to disable.
    "consolidate_cluster_threshold": 0.5,  # cosine sim for single-linkage edge in /consolidate
    "consolidate_min_cluster_size": 3,  # smallest cluster that becomes a theme
    "consolidate_unassigned_threshold": 50,  # /consolidate stats nudge when unassigned exceeds this
    "consolidate_max_theme_size": 120,  # /consolidate stats flags themes larger than this + suggests split
    "aged_summarize_threshold_days": 90,  # #168: notes whose file mtime is older than this AND have no inbound vault links AND no pin tag are deferred (skipped) by /recall
    "summary_pin_tags": ["claude/keep", "claude/permanent"],  # #168: notes carrying any of these frontmatter tags are never deferred
    "auto_log_enabled": True,
    "snapshot_on_compact": True,
    "snapshot_on_clear": True,
    "optional_deps_prompted": False,
    "optional_deps_declined": [],
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Read ~/.claude/obsidian-brain-config.json, returning defaults for missing keys.

    Session-scoped caching: first call loads from disk and writes to cache;
    subsequent calls within the same session hit the cache.
    """
    sid = _get_session_id_fast()
    cached = cache_get(sid, "config")
    if cached is not None:
        return cached

    # dict(_DEFAULTS) is a shallow copy — any list/dict values in _DEFAULTS
    # would be shared across calls, so an in-place mutation on
    # config["optional_deps_declined"] (or a future list default) would
    # leak back into _DEFAULTS and future callers. deepcopy severs that link.
    import copy as _copy
    config = _copy.deepcopy(_DEFAULTS)
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        if isinstance(user_cfg, dict):
            config.update(user_cfg)
    except FileNotFoundError:
        print(
            f"[obsidian-brain] config not found at {_CONFIG_PATH}, using defaults",
            file=sys.stderr,
        )
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"[obsidian-brain] error reading config: {exc}, using defaults",
            file=sys.stderr,
        )

    # Auto-fix config file permissions if group/world readable
    try:
        config_stat = os.stat(_CONFIG_PATH)
    except OSError:
        pass  # file doesn't exist or can't stat — nothing to fix
    else:
        if config_stat.st_mode & 0o077:
            try:
                os.chmod(_CONFIG_PATH, 0o600)
                print("[obsidian-brain] fixed config permissions to 0o600", file=sys.stderr)
            except OSError as exc:
                print(f"[obsidian-brain] WARNING: config is world-readable and chmod failed: {exc}", file=sys.stderr)

    cache_set(sid, "config", config)
    return config


def get_workspace_roots() -> list[str]:
    """Return list of absolute workspace-root directories to scan for projects.

    Reads ``workspace_roots`` from ~/.claude/obsidian-brain-config.json if
    present (list of paths, each tilde-expanded). Falls back to historical
    defaults [~/dev/claude_workspace, ~/projects] when the key is absent so
    existing users need no config migration.

    Only directories that exist on disk are included in the returned list.
    """
    config = load_config()
    raw = config.get("workspace_roots")
    if raw is not None and isinstance(raw, list):
        roots = [os.path.expanduser(str(p)) for p in raw]
    else:
        home = os.path.expanduser("~")
        roots = [
            os.path.join(home, "dev", "claude_workspace"),
            os.path.join(home, "projects"),
        ]
    return [r for r in roots if os.path.isdir(r)]


def check_optional_deps(packages: tuple[str, ...] = ("numpy", "scipy")) -> dict[str, bool]:
    """Return {package: is_importable} for each optional performance dep.

    Used by /obsidian-setup to decide whether to offer installation, and
    by callers that want a stdlib fallback when a fast-path dep is missing.
    """
    import importlib
    result: dict[str, bool] = {}
    for pkg in packages:
        try:
            importlib.import_module(pkg)
            result[pkg] = True
        except Exception:
            # Compiled optional deps (numpy/scipy) can raise OSError for
            # missing shared libs, ValueError for ABI mismatch, etc. —
            # treat any failure as "unavailable" rather than crashing setup.
            result[pkg] = False
    return result


def check_hook_status() -> dict:
    """Inspect the bootstrap file to report session-logging health.

    Returns:
        {"ok": bool, "message": str, "bootstrap_sid": str, "current_sid": str}

    "ok" is True if the bootstrap file exists (session logging is active).
    A SID mismatch (bootstrap points at a previous session) is normal after
    reconnects and does NOT indicate a problem — sessions are still logged.
    "ok" is False only when the bootstrap file is missing entirely or no
    session files can be found.
    """
    # Cwd-based project name (NOT canonical) — used for CC's path-encoded
    # JSONL/bootstrap directory lookups. Frontmatter project is canonical;
    # see canonical_project_name().
    project = os.path.basename(os.getcwd())
    bootstrap = f"{_bootstrap_prefix()}{project}"

    # Read bootstrap BEFORE deriving current_sid so we see its real state.
    try:
        with open(bootstrap, "r") as f:
            bootstrap_sid = f.read().strip()
    except OSError:
        bootstrap_sid = None

    # Use the bootstrap-independent slow path so the check isn't circular.
    # _get_session_id_fast() can return the cached bootstrap value, which
    # would make this health check report OK even when the bootstrap is
    # stale.
    current_sid = _slow_path_newest_sid()

    if bootstrap_sid is None:
        return {
            "ok": False,
            "message": "Session logging may not be active — run /obsidian-setup to configure",
            "bootstrap_sid": "",
            "current_sid": current_sid,
        }

    if current_sid == "unknown":
        return {
            "ok": False,
            "message": "No session files found — run /obsidian-setup to verify configuration",
            "bootstrap_sid": bootstrap_sid,
            "current_sid": current_sid,
        }

    # Bootstrap exists = session logging is working. SID mismatch is
    # expected after reconnects and is not a problem — keep ok=True
    # but include diagnostic detail for debugging.
    if bootstrap_sid != current_sid:
        return {
            "ok": True,
            "message": "Session logging active (resumed session)",
            "bootstrap_sid": bootstrap_sid,
            "current_sid": current_sid,
        }

    return {
        "ok": True,
        "message": "Session logging active",
        "bootstrap_sid": bootstrap_sid,
        "current_sid": current_sid,
    }


def get_session_context(vault_path: str | None = None, sessions_folder: str | None = None) -> dict:
    """Get session ID, hash, project, and session note name. Cached.

    Returns {session_id, hash, project, session_note_name} or
    {session_id: 'unknown', hash: '', project: <canonical-project>, session_note_name: ''}.
    """
    sid = _get_session_id_fast()
    # Include args in cache key so different call signatures don't collide
    cache_key = f"session_context:{vault_path or ''}:{sessions_folder or ''}"
    cached = cache_get(sid, cache_key)
    if cached is not None:
        return cached

    project = canonical_project_name()
    if sid == "unknown":
        # Don't cache "unknown" — would pollute cache shared across projects
        return {"session_id": "unknown", "hash": "", "project": project, "session_note_name": ""}

    h = hashlib.sha256(sid.encode()).hexdigest()[:4]

    session_note_name = ""
    if vault_path and sessions_folder:
        sessions_dir = Path(vault_path) / sessions_folder
        resolved, collisions = _resolve_session_note_by_hash(
            sessions_dir, h, cwd=_safe_getcwd()
        )
        # WARN fires once per (session, vault, folder) — cache_set below
        # persists the result to ~/.claude/obsidian-brain/cache-<sid>.json,
        # suppressing repeats across this hook process and any subsequent
        # skill invocations within the same session until SessionEnd cleans
        # up the cache. Don't spam stderr.
        if collisions:
            print(
                f"[obsidian-brain] WARN: hash {h} matches {len(collisions) + (1 if resolved else 0)} "
                f"session note(s); chose {resolved or '<none — fell back to composed name>'} "
                f"(others: {collisions})",
                file=sys.stderr,
            )
        if resolved:
            session_note_name = resolved

    # If not found, compose the canonical basename. Both _first_seen_date()
    # and make_filename() are also called by SessionEnd, so insight wikilinks
    # and session-note filenames stay in lockstep across cross-midnight,
    # worktree, and resumed-session conditions. (#101 Fix A + Fix B.)
    if not session_note_name:
        session_note_name = make_filename(
            _first_seen_date(sid), slugify(project), sid
        )[:-3]

    ctx = {"session_id": sid, "hash": h, "project": project, "session_note_name": session_note_name}
    cache_set(sid, cache_key, ctx)
    return ctx


def _classify_note_parse_failure(reason: str | None) -> str:
    """Content-free category for a ``read_note_metadata_detailed`` reason.

    Delegates to ``vault_index._classify_parse_failure``, which exists for
    exactly this: ``split_frontmatter``'s "no closing '---'" reason embeds up
    to 60 characters of the note's OWN text, and vault notes are user/LLM
    authored content. Every reason that leaves this module for stderr or for
    ``/retro``'s ``discovery_errors`` — both of which reach the model's
    context and the session transcript — must go through here first. Because
    the output is a fixed word from a closed set, ``scrub_secrets()`` on top
    would be redundant: no note text survives to be scrubbed.

    Falls back to "unknown (classifier unavailable)" when ``vault_index``
    could not be imported (the same degraded mode the access-tracking import
    already tolerates) rather than re-implementing the classifier here — one
    copy, like the parser. The fallback names WHERE to look instead of
    colliding with the classifier's own "unknown", which means the opposite
    ("the classifier ran and did not recognise this wording").

    ``getattr``, not a direct attribute access: ``_classify_parse_failure`` is
    a ``_``-prefixed private of another module, so a rename there is a
    plausible refactor. A bare access would let ``AttributeError`` escape
    ``gather_session_evidence``, whose contract is that file-read failures are
    captured in ``discovery_errors`` and never raised — that half is the real
    hazard. In ``find_snapshots_for_session`` the cost is smaller: that call
    sits inside ``if not meta:``, i.e. it only ever runs for a snapshot
    ALREADY known to be unparseable, so no healthy snapshot is at risk; the
    escaping ``AttributeError`` would be swallowed by that function's
    ``except Exception`` and the snapshot logged with a bare exception type
    instead of a category — a degraded diagnostic for an already-malformed
    file, still correctly skipped. A missing symbol is the same degraded mode
    as a missing module, so it takes the same branch.
    (``tests/test_vault_index_frontmatter.py`` pins the symbol's existence so
    the rename fails in CI rather than in a user's /retro.)
    """
    if _vault_index is not None:
        classifier = getattr(_vault_index, "_classify_parse_failure", None)
        if classifier is not None:
            return classifier(reason)
    return "unknown (classifier unavailable)"


def _describe_note_parse_failure(reason: str | None) -> str:
    """Egress-safe rendering of a ``read_note_metadata_detailed`` reason, for
    anything that reaches a user, stderr, or the model's context.

    Two tiers, because the reasons are not all equally dangerous:

    * The three ``split_frontmatter`` reasons all get the closed-set category
      and nothing else. Only ONE of them actually embeds note content — the
      "no closing '---'; stopped at a line that is not frontmatter:
      '<excerpt>'" variant, up to 60 characters of the note's OWN text; the
      missing-opening-fence and "frontmatter exceeds N" reasons are fixed
      strings with at most a number in them. Categorizing all three is
      deliberate conservatism: which of the three a reason is, is itself
      decided by the classifier this tier calls, so a per-reason carve-out
      would have to re-derive that here and would rot the day a fourth reason
      (or a new excerpt) is added.
    * ``"unreadable file: <strerror>"`` is content-free AND path-free by
      construction — that is exactly why ``_read_frontmatter_region`` and
      ``vault_index._parse_note_detailed`` build it from ``exc.strerror``
      instead of ``str(exc)``. Collapsing it to the bare word "unreadable" is
      pure loss: "Permission denied" (fix your perms) and "Input/output error"
      (your vault mount is flaky) demand opposite responses, and the user can
      no longer tell them apart. So it passes through intact.

    ``_FRONTMATTER_TOO_LARGE_REASON`` is this module's own fixed string and is
    likewise content-free, but it classifies cleanly as "frontmatter_too_long"
    and there is nothing extra to preserve, so it takes the categorized tier.
    """
    if reason is not None and reason.startswith(_UNREADABLE_REASON_PREFIX):
        return reason
    return _classify_note_parse_failure(reason)


def read_note_metadata_detailed(file_path: str) -> tuple[dict | None, str | None]:
    """Parse YAML frontmatter from a vault note.

    Returns ``(meta, None)`` on success, or ``(None, reason)`` when the note
    has no parsable frontmatter — mirroring
    ``vault_index._parse_note_detailed``, whose ``(meta, reason)`` shape this
    is deliberately copying. ``reason`` is either ``"unreadable file: ..."``
    or the ``frontmatter.split_frontmatter`` error verbatim (missing opening
    fence / missing closing fence + the offending line / oversized block).

    Keeping the reason matters because a bare None conflates "this note is
    fine and simply has no frontmatter" with "this note is broken": callers
    that used to re-probe the file to tell them apart cannot, since a note
    with broken-but-present frontmatter re-reads without error and so
    vanishes from ``/retro``'s evidence bundle with an empty
    ``discovery_errors``. Pass the reason through
    ``_classify_note_parse_failure`` before it reaches a user, stderr, or the
    model — it can embed note text.

    The frontmatter block is located by ``frontmatter.split_frontmatter`` (via
    ``_read_frontmatter_region``), so a note whose fields sit deep in a long
    ``tags:``/``projects:`` block parses like any other, and a note whose
    closing ``---`` is missing returns None instead of harvesting body prose
    into fields — an unfenced ``Note: this is body prose`` line used to become
    a real ``meta['Note']`` entry, and a prose line beginning ``status:``
    used to forge a ``status`` field the note never had (#283).

    One shape rule is TIGHTER than the old hand-rolled scan, inherited from
    the shared parser and affecting 0 of 2098 live vault notes (the same
    tightening ``_peek_frontmatter_field`` documents, because the old
    ``read_note_metadata`` compared with ``.strip()`` too): the closing fence
    is matched with ``rstrip("\r\n")``, so a fence written with trailing
    spaces (``---␣␣``) no longer closes the block and such a note returns
    ``(None, reason)`` instead of its field values.

    ``tags`` is returned as a **list** here, unlike the index's parser which
    joins it to a comma string — callers of this function index into it.

    Cached per file path within the session (the failure reason is cached
    alongside the sentinel, so a cache hit is as informative as a miss).
    What is cached is the RAW reason, deliberately, and it must stay raw:
    ``_classify_parse_failure`` recognises reasons by prefix-matching
    ``split_frontmatter``'s exact wording, so caching the CLASSIFIED value
    would make the second call re-classify a category word like
    "no_closing_fence" as "unknown" — a silent, cache-warmth-dependent bug
    where the first /retro of a session reports the right category and every
    later one reports none. Classify at egress instead
    (``_describe_note_parse_failure``), never on the way in.
    """
    sid = _get_session_id_fast()
    cache_key = f"metadata:{os.path.realpath(file_path)}"
    cached = cache_get(sid, cache_key)
    if cached is not None:
        # `is True` and not a truthy check: every parsed frontmatter value is
        # a str, so a note that literally declares `__no_frontmatter__: true`
        # yields the *string* "true" and cannot impersonate the sentinel.
        if isinstance(cached, dict) and cached.get("__no_frontmatter__") is True:
            return None, cached.get("__reason__")
        return cached, None

    lines, read_err, size_caveat = _read_frontmatter_region(file_path)
    if lines is None:
        # Unreadable file: return None WITHOUT caching the sentinel — the
        # failure is about the read, not about the note's content, and a
        # transient error must not pin "no frontmatter" for the session.
        return None, read_err

    _open_fence, fm_lines, _close_fence, _body_lines, split_err = split_frontmatter(lines)
    if split_err:
        # Covers all three malformed shapes: no opening fence, no closing
        # fence, and an oversized frontmatter block. Each means "this note
        # has no parsable frontmatter", which is what the sentinel records.
        #
        # size_caveat wins ONLY over the bare-exhaustion verdict. A truncated
        # read makes exactly one of split_frontmatter's three reasons
        # untrustworthy: the one produced by running off the end of a prefix,
        # where the real fence may simply sit past the cut. Reporting that as
        # "no closing '---'" accuses a note that may be fine -- the
        # wrong-diagnosis failure frontmatter.py's docstring calls out -- so
        # the caveat ("frontmatter exceeds ... characters") replaces it.
        #
        # The other two verdicts are DEFINITIVE regardless of truncation,
        # because each is derived from a line that was actually read and
        # inspected: "does not open with a '---' fence" reads lines[0], always
        # in the first block; the "stopped at a line that is not frontmatter"
        # variant names the offending line. Overwriting those mislabels a file
        # that is not a note as an oversized note (which defeats the
        # no_opening_fence filter in gather_session_evidence for any file over
        # the char cap) and tells someone whose frontmatter demonstrably dies
        # at line 3 that "the note may be fine".
        #
        # Exact equality, NEVER startswith: `==` here does not depend on the
        # constant's trailing ')' happening to fall exactly where the
        # shape-stop variant's text diverges (into "; stopped at ..."). A
        # hand-truncated or reworded literal could silently start matching
        # the shape-stop variant too, which is precisely why the literal is
        # imported from frontmatter.py (and used by the return that produces
        # it) rather than copied — the two cannot drift apart into a gate
        # that silently never matches.
        #
        # Still the RAW wording either way, so the cached reason re-classifies
        # correctly on the next hit (see the docstring).
        if size_caveat and split_err == NO_CLOSING_FENCE_EXHAUSTED_REASON:
            reason = size_caveat
        else:
            reason = split_err
        cache_set(sid, cache_key, {"__no_frontmatter__": True, "__reason__": reason})
        return None, reason

    meta: dict = {}
    tags: list[str] = []
    in_tags = False

    for line in fm_lines:
        stripped = line.strip()
        if stripped.startswith('- ') and in_tags:
            tags.append(stripped[2:].strip())
            continue
        in_tags = False
        if ':' in stripped:
            key, _, val = stripped.partition(':')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key == 'tags':
                in_tags = True
                continue
            meta[key] = val

    if tags:
        meta['tags'] = tags

    cache_set(sid, cache_key, meta)
    return meta, None


def read_note_metadata(file_path: str) -> dict | None:
    """Parse YAML frontmatter from a vault note. Returns dict or None.

    Thin wrapper over ``read_note_metadata_detailed`` for the call sites that
    only need "did it parse". Use the detailed variant when a None must be
    explainable to a user (see its docstring).
    """
    return read_note_metadata_detailed(file_path)[0]


def find_snapshots_for_session(
    sessions_folder_path: Path, session_id: str, date: str | None, project: str
) -> list[str]:
    """Return chronologically-sorted wikilinks of all snapshots whose
    frontmatter session_id and project match the given session. Empty list
    if none exist.

    Matching rules:
    - Filename pattern: <date>-<project-slug>-*-snapshot*.md (captures both
      pre-spec `-snapshot.md` and post-spec `-snapshot-<HHMMSS>.md`).
    - Requires frontmatter `session_id` AND `project` to match.
    - Malformed snapshots are logged to stderr and skipped — one bad file
      must not block back-reference writing.

    Sorted lexicographically by filename stem; HHMMSS suffix makes this
    chronological for post-spec snapshots. Pre-spec (no HHMMSS) sorts first.

    If `date` is None, discovery is date-agnostic: globs `*-{slug}-*-snapshot*.md`
    and relies entirely on frontmatter session_id+project filtering. Use this
    mode for cross-midnight sessions where snapshots may span multiple
    YYYY-MM-DD prefixes.
    """
    if not sessions_folder_path.is_dir():
        return []
    slug = slugify(project)
    wikilinks: list[str] = []
    if date is None:
        glob_pattern = f"*-{slug}-*-snapshot*.md"
    else:
        glob_pattern = f"{date}-{slug}-*-snapshot*.md"
    for p in sorted(sessions_folder_path.glob(glob_pattern)):
        try:
            meta, reason = read_note_metadata_detailed(str(p))
            if not meta:
                # Honour this function's "malformed snapshots are logged to
                # stderr and skipped" contract. That logging used to arrive
                # via the except below, which only fired because the old
                # reader let UnicodeDecodeError escape; with errors="replace"
                # plus split_frontmatter's (None, reason) path nothing raises
                # any more, so a malformed snapshot would drop in silence.
                # `reason` is None only when the fence pair parsed fine but
                # held no `key: value` lines — an empty dict, not a defect.
                if reason:
                    # Never the raw reason: it can embed up to 60 characters
                    # of the note's own text, and this line reaches the model's
                    # context via the transcript. _describe_note_parse_failure
                    # categorizes those and passes the content-free
                    # "unreadable file: <strerror>" shape through, so a flaky
                    # mount still reads as a mount problem.
                    print(
                        f"[obsidian-brain] skipping malformed snapshot {p.name}: "
                        f"{_describe_note_parse_failure(reason)}",
                        file=sys.stderr,
                    )
                continue
            if meta.get("session_id") == session_id and (
                meta.get("project", "").lower() == project.lower()
                or slugify(meta.get("project", "")) == slug
            ):
                wikilinks.append(f"[[{p.stem}]]")
        except Exception as exc:  # noqa: BLE001
            # The exception TYPE (plus strerror when there is one), never
            # str(exc): str(OSError) embeds the full path argument, which
            # would leak the absolute vault path into the transcript and the
            # model's context, and an arbitrary exception's message can carry
            # note text. This is the last unclassified egress in this function
            # — same rule as the `reason` branch above.
            detail = getattr(exc, "strerror", None) or type(exc).__name__
            print(f"[obsidian-brain] skipping malformed snapshot {p.name}: {detail}",
                  file=sys.stderr)
            continue
    return wikilinks


_SECTION_RE_CACHE: dict[tuple[str, ...], re.Pattern] = {}


def _extract_sections(body: str, section_headers: tuple[str, ...]) -> str:
    """Return concatenated content of the named `## Foo` sections, or '' if none.

    Header matching is literal — case-sensitive, full-line. Sections end at
    the next `## ` heading or EOF. Empty string if no section matches.
    """
    key = tuple(section_headers)
    if key not in _SECTION_RE_CACHE:
        pattern = "|".join(re.escape(h) for h in section_headers)
        _SECTION_RE_CACHE[key] = re.compile(
            rf"^({pattern})\s*$\n(.*?)(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
    chunks = []
    for m in _SECTION_RE_CACHE[key].finditer(body):
        chunks.append(m.group(1) + "\n" + m.group(2).strip())
    return "\n\n".join(chunks)


def _extract_hhmmss_from_filename(filename: str) -> str:
    """Return the HHMMSS suffix from a post-spec snapshot filename, or '??????'."""
    m = re.search(r"-snapshot-(\d{6})\.md$", filename)
    return m.group(1) if m else "??????"


def _augment_session_input_with_snapshots(
    transcript: str,
    sessions_folder_path: Path,
    session_id: str,
    date: str,
    project: str,
) -> str:
    """Prepend sibling snapshot bodies (preferring summaries) to session input.

    Used by the session-note branch of upgrade_unsummarized_note to give
    generate_summary a cohesive view of the whole arc, not just the
    post-last-compact tail.

    Returns the original transcript unchanged if no snapshots exist.
    """
    wikilinks = find_snapshots_for_session(sessions_folder_path, session_id, date, project)
    if not wikilinks:
        return transcript

    blocks: list[str] = []
    for link in wikilinks:
        stem = link.strip("[]")
        snap_path = sessions_folder_path / f"{stem}.md"
        if not snap_path.exists():
            continue
        try:
            body = snap_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hh = _extract_hhmmss_from_filename(snap_path.name)
        meta = read_note_metadata(str(snap_path)) or {}
        # Default missing `trigger:` to "auto" (consistent with _snapshot_stats
        # and fetch_snapshot_summaries) so legacy/malformed notes don't get
        # mislabeled as compact. Copilot PR #43 round 2 finding.
        trigger = meta.get("trigger", "auto")
        summary_block = _extract_sections(
            body, ("## Summary", "## Key context that may be lost (summary)")
        )
        if not summary_block.strip():
            summary_block = _extract_sections(
                body,
                ("## What was happening", "## Key context that may be lost",
                 "## Uncommitted work", "## Last messages (raw)"),
            )
        if not summary_block.strip():
            continue
        blocks.append(f"[snapshot {hh}, trigger={trigger}]\n{summary_block}")

    if not blocks:
        return transcript

    prefix = "===== EARLIER IN THIS SESSION (from snapshots) =====\n\n" + "\n\n".join(blocks)
    if transcript:
        return (
            prefix
            + "\n\n===== CURRENT TAIL (post-compact transcript) =====\n\n"
            + transcript
        )
    return prefix


def fetch_snapshot_summaries(
    sessions_folder_path: Path,
    session_id: str,
    date: str,
    project: str,
) -> list[dict]:
    """Return chronologically-sorted summary dicts for a session's snapshots.

    Each dict has: ``path``, ``hhmmss``, ``trigger``, ``summary`` (first
    sentence of ``## Summary`` if upgraded; first bullet of
    ``## What was happening`` as fallback; ``"(not yet summarized)"`` if
    neither exists), ``key_context`` (``## Key context that may be lost
    (summary)`` if upgraded; ``""`` otherwise).

    Shared helper used by build_context_brief(), the vault-search skill,
    and vault-ask so presentation stays consistent.
    """
    results: list[dict] = []
    for link in find_snapshots_for_session(sessions_folder_path, session_id, date, project):
        stem = link.strip("[]")
        path = sessions_folder_path / f"{stem}.md"
        if not path.exists():
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta = read_note_metadata(str(path)) or {}
        hh = _extract_hhmmss_from_filename(path.name)
        summary = ""
        key_context = ""

        summary_match = re.search(r"^## Summary\s*\n(.+?)(?=\n## |\Z)", body, re.MULTILINE | re.DOTALL)
        if summary_match:
            summary = summary_match.group(1).strip().split("\n", 1)[0].strip()
        else:
            wh_match = re.search(r"^## What was happening\s*\n(.+?)(?=\n## |\Z)",
                                 body, re.MULTILINE | re.DOTALL)
            if wh_match:
                summary = wh_match.group(1).strip().split("\n", 1)[0].strip()

        kc_match = re.search(
            r"^## Key context that may be lost \(summary\)\s*\n(.+?)(?=\n## |\Z)",
            body, re.MULTILINE | re.DOTALL,
        )
        if kc_match:
            key_context = kc_match.group(1).strip()

        results.append({
            "path": str(path),
            "hhmmss": hh,
            # Default missing `trigger:` to "auto" so /recall doesn't mislabel
            # legacy snapshots as compact. Copilot PR #43 round 2 finding.
            "trigger": meta.get("trigger", "auto"),
            "summary": summary or "(not yet summarized)",
            "key_context": key_context,
        })
    return results


def gather_session_evidence(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    session_id: str,
    project: str,
) -> dict:
    """Discover and load all artifacts written during this session.

    Returns a structured bundle of snapshots (from sessions_folder) and
    insights/decisions/error-fixes (from insights_folder) whose frontmatter
    `source_session` matches the given session_id.

    Snapshots are returned sorted ascending by stem (YYYY-MM-DD-... prefix),
    which gives correct chronological order including across-midnight sessions.
    Pre-spec snapshots (hhmmss == '??????') sort before all post-spec ones.
    Insights/decisions/error-fixes are returned sorted ascending by filename.
    File-read failures are captured in `discovery_errors` and never raised.

    Used by /retro to ground retrospective analysis in the full session
    arc (pre-compact + post-compact) rather than just the active conversation.
    """
    bundle: dict = {
        "session_id": session_id,
        "snapshots": [],
        "insights": [],
        "decisions": [],
        "error_fixes": [],
        "discovery_errors": [],
    }
    if session_id == "unknown" or not session_id:
        return bundle
    sessions_path = Path(vault_path) / sessions_folder
    if sessions_path.is_dir():
        # Pass date=None for date-agnostic discovery so cross-midnight sessions
        # (snapshots written on YYYY-MM-DD and YYYY-MM-(DD+1)) are both found.
        # Frontmatter session_id+project filters inside find_snapshots_for_session
        # exclude any cross-project or cross-session decoys the broader glob picks up.
        for link in find_snapshots_for_session(sessions_path, session_id, None, project):
            stem = link.strip("[]")
            snap_path = sessions_path / f"{stem}.md"
            if not snap_path.exists():
                continue
            try:
                body = snap_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                # exc.strerror, NOT str(exc): str(OSError) embeds the full
                # path argument, leaking the absolute vault path into
                # discovery_errors and from there into the model's context.
                # Same shape (and same reason) as _read_frontmatter_region's
                # reason — and newly reachable, because that reader only pulls
                # ~8 KB of frontmatter while this read pulls the whole file, so
                # on a cloud-synced vault a partially-materialized note can
                # succeed there and fail EIO here.
                bundle["discovery_errors"].append(
                    f"{snap_path.name}: {_UNREADABLE_REASON_PREFIX} "
                    f"{exc.strerror or type(exc).__name__}"
                )
                continue
            meta = read_note_metadata(str(snap_path)) or {}
            bundle["snapshots"].append({
                "path": str(snap_path),
                "stem": stem,
                "hhmmss": _extract_hhmmss_from_filename(snap_path.name),
                "trigger": meta.get("trigger", "auto"),
                "body": body,
            })
        bundle["snapshots"].sort(key=lambda s: (0 if s["hhmmss"] == "??????" else 1, s["stem"]))
    insights_path = Path(vault_path) / insights_folder
    if insights_path.is_dir():
        type_buckets = {
            "claude-insight": bundle["insights"],
            "claude-decision": bundle["decisions"],
            "claude-error-fix": bundle["error_fixes"],
        }
        for note_path in sorted(insights_path.glob("*.md")):
            meta, reason = read_note_metadata_detailed(str(note_path))
            if meta is None:
                # Record WHY. The old code re-probed the file and only logged
                # an OSError, so a note with broken-but-present frontmatter
                # (which re-reads perfectly) vanished from the bundle with an
                # empty discovery_errors — /retro then reported "no insights
                # captured this session" and the note was invisible. The
                # detailed reader distinguishes "unreadable" from "malformed"
                # on its own, so the probe goes with it.
                #
                # Classified, never raw: the reason can embed up to 60
                # characters of the note's own text and discovery_errors flows
                # straight into the model's context.
                # A missing OPENING fence is filtered because it does not
                # mean "this note is broken" — it means "this file is not a
                # note". Nothing stops such a file from sitting in the
                # insights folder: a Dataview dashboard copied or moved there
                # (this plugin installs its own 6 into the separate
                # `dashboards_folder`, which this loop never globs — the live
                # insights folder measures 0 of them today), a pasted export,
                # a scratch file. This parse runs BEFORE the source_session
                # filter below, so ONE such file would raise /retro's
                # "evidence discovery partially or fully failed" banner in
                # every project, every session, permanently
                # (skills/retro/SKILL.md turns any non-empty discovery_errors
                # into that warning). There is no consumer that would act on
                # a not-a-note file sitting in the insights folder, and
                # /retro is not a vault linter, so filtering it here rather
                # than surfacing it stands on its own merits — it is not
                # covered by /vault-doctor's missing_frontmatter_fence check,
                # which only repairs a narrower case: a genuine former note
                # that lost precisely its opening '---' line, where every
                # line above the closing fence is still frontmatter-shaped.
                # A Dataview dashboard, a pasted export, or a scratch file
                # fails that check's own precondition that the first line be
                # key:-shaped, so it is left untouched either way.
                #
                # no_closing_fence / frontmatter_too_long / unreadable are
                # kept: those DO mean "this is a note and it is broken",
                # which is exactly what /retro should surface.
                #
                # Keyed off the RAW reason (an exact match against the
                # producing module's own constant), not off
                # _classify_note_parse_failure: that classifier degrades to
                # "unknown (classifier unavailable)" when vault_index cannot
                # be imported, which compares unequal to every category and
                # would fail the filter OPEN — re-raising the permanent banner
                # this filter exists to prevent, in an already-degraded mode.
                # The classifier is still what RENDERS the message below,
                # where degrading costs only the wording.
                if reason and reason != NO_OPENING_FENCE_REASON:
                    bundle["discovery_errors"].append(
                        f"{note_path.name}: {_describe_note_parse_failure(reason)}"
                    )
                continue
            if meta.get("source_session") != session_id:
                continue
            note_type = meta.get("type", "")
            target = type_buckets.get(note_type)
            if target is None:
                continue
            try:
                body = note_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                # exc.strerror, not str(exc) — see the snapshot-body read
                # above for why the absolute path must not reach here.
                bundle["discovery_errors"].append(
                    f"{note_path.name}: {_UNREADABLE_REASON_PREFIX} "
                    f"{exc.strerror or type(exc).__name__}"
                )
                continue
            title_match = re.search(r"^# (.+?)$", body, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else note_path.stem
            target.append({
                "path": str(note_path),
                "stem": note_path.stem,
                "title": title,
                "body": body,
            })
    return bundle


def match_items_against_evidence(
    evidence_text: str,
    open_items: list[tuple[str, int, str]],
) -> list[dict]:
    """Match open items against evidence prose for completion detection.

    Different from find_duplicates() — this matches items (short checkbox
    lines) against free-form text (summaries, changelogs, commit messages).

    Returns [{"file": f, "line": l, "text": t, "evidence": snippet, "confidence": score}]
    for items that appear to be completed based on the evidence.
    """
    try:
        _hooks_dir = os.path.dirname(os.path.abspath(__file__))
        if _hooks_dir not in sys.path:
            sys.path.insert(0, _hooks_dir)
        from open_item_dedup import (
            _strip_markdown, _extract_distinctive_tokens, _tokenize,
            _COMPLETION_PHRASES,
        )
    except ImportError as exc:
        print(f"[obsidian-brain] match_items: import failed: {exc}", file=sys.stderr)
        return []

    if not evidence_text.strip():
        return []

    evidence_lower = evidence_text.lower()
    evidence_tokens = _tokenize(evidence_text)
    candidates: list[dict] = []

    for fpath, line_num, item_text in open_items:
        cleaned = _strip_markdown(item_text)
        distinctive = _extract_distinctive_tokens(cleaned)
        tokens = _tokenize(cleaned)

        # Score: distinctive tokens get higher weight
        score = 0
        match_positions: list[int] = []

        # Check distinctive tokens
        for dt in distinctive:
            dt_lower = dt.lower()
            pos = evidence_lower.find(dt_lower)
            if pos >= 0:
                score += 3
                match_positions.append(pos)

        # Check regular tokens (3+ chars) — set intersection for word-boundary matching
        matched_token_set = tokens & evidence_tokens
        matched_tokens = len(matched_token_set)
        # Find positions for matched tokens (for evidence snippet extraction)
        for t in matched_token_set:
            pos = evidence_lower.find(t)
            if pos >= 0:
                match_positions.append(pos)

        score += matched_tokens

        # Minimum threshold: 3+ token matches, or any distinctive token match
        if score < 3:
            continue

        # Completion phrase boost: check ±100 char window around EACH match position
        has_completion_phrase = False
        if match_positions:
            for mpos in match_positions:
                if has_completion_phrase:
                    break
                window_start = max(0, mpos - 100)
                window_end = min(len(evidence_lower), mpos + 100)
                window = evidence_lower[window_start:window_end]
                for phrase in _COMPLETION_PHRASES:
                    if phrase in window:
                        has_completion_phrase = True
                        score += 2
                        break

        # Extract evidence snippet (~60 chars around best match (first position)
        best_match_pos = min(match_positions) if match_positions else -1
        snippet = ""
        if best_match_pos >= 0:
            start = max(0, best_match_pos - 30)
            end = min(len(evidence_text), best_match_pos + 30)
            snippet = evidence_text[start:end].strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(evidence_text):
                snippet = snippet + "..."

        candidates.append({
            "file": fpath,
            "line": line_num,
            "text": item_text,
            "evidence": snippet,
            "confidence": score,
            "has_completion_phrase": has_completion_phrase,
        })

    # Sort by confidence descending
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def read_transcript(path: str) -> list[dict]:
    """Parse a JSONL transcript file into a list of entry dicts."""
    messages: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (OSError, IOError) as exc:
        print(f"[obsidian-brain] failed to read transcript: {exc}", file=sys.stderr)
    return messages


def _extract_text(content) -> list[str]:
    """Extract text from message content (string or list of blocks)."""
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                # Skip tool_use and tool_result blocks for text extraction
            elif isinstance(part, str):
                texts.append(part)
    return [t for t in texts if t.strip()]


def extract_user_messages(entries: list[dict]) -> list[str]:
    """Extract user message texts from CC transcript JSONL entries.

    CC format: top-level ``type`` field ("user"/"assistant"),
    message nested under ``entry["message"]["content"]``.
    Also supports flat format as fallback.
    """
    texts: list[str] = []
    for entry in entries:
        # CC JSONL format
        if entry.get("type") == "user":
            msg = entry.get("message", {})
            texts.extend(_extract_text(msg.get("content", "")))
        # Flat format fallback
        elif entry.get("role") == "user":
            texts.extend(_extract_text(entry.get("content", "")))
    return texts


def extract_assistant_messages(entries: list[dict]) -> list[str]:
    """Extract assistant message texts from CC transcript JSONL entries."""
    texts: list[str] = []
    for entry in entries:
        if entry.get("type") == "assistant":
            msg = entry.get("message", {})
            texts.extend(_extract_text(msg.get("content", "")))
        elif entry.get("role") == "assistant":
            texts.extend(_extract_text(entry.get("content", "")))
    return texts


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def _parse_ts(ts_str: str) -> datetime.datetime | None:
    """Best-effort timestamp parsing (ISO formats + epoch seconds/millis)."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.datetime.strptime(ts_str, fmt)
        except ValueError:
            continue
    # Epoch seconds or milliseconds
    try:
        val = float(ts_str)
        if val > 1e12:
            val /= 1000.0
        return datetime.datetime.fromtimestamp(val, tz=datetime.timezone.utc)
    except (ValueError, OSError):
        pass
    return None


def extract_session_metadata(messages: list[dict], cwd: str) -> dict:
    """Extract session metadata from transcript entries.

    Returns dict with: project, project_path, git_branch, files_touched,
    errors, duration_minutes, commits.
    """
    meta: dict = {
        "project": canonical_project_name(cwd) if cwd else "unknown",
        "project_path": cwd or "",
        "git_branch": "",
        "files_touched": [],
        "errors": [],
        "duration_minutes": 0,
        "commits": [],
    }

    # --- Git branch: try transcript gitBranch field first, then CLI fallback ---
    for entry in messages:
        branch = entry.get("gitBranch")
        if branch and branch != "HEAD":
            meta["git_branch"] = branch
            break
    if not meta["git_branch"]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=cwd or None,
            )
            if result.returncode == 0:
                meta["git_branch"] = result.stdout.strip()
        except Exception:
            pass

    # --- Duration: first and last entry timestamps ---
    timestamps: list[str] = []
    for entry in messages:
        ts = entry.get("timestamp") or entry.get("ts") or entry.get("created_at")
        if ts:
            timestamps.append(str(ts))
    if len(timestamps) >= 2:
        try:
            first = _parse_ts(timestamps[0])
            last = _parse_ts(timestamps[-1])
            if first and last:
                delta = (last - first).total_seconds() / 60.0
                meta["duration_minutes"] = round(delta, 1)
        except Exception:
            pass

    # --- Files touched + Errors: delegate to shared helpers ---
    meta["files_touched"] = _extract_files_touched(messages)[:60]
    meta["errors"] = _extract_errors(messages)[:30]

    return meta


def _entry_content(entry: dict) -> str | list | None:
    """Return the raw transcript content value for an entry.

    Supports both the canonical CC JSONL shape (nested under
    entry['message']['content']) and the flat fallback shape
    (entry['content'] directly). Mirrors the shape handling of
    extract_user_messages / extract_assistant_messages so the
    _extract_* helpers below stay consistent across transcript formats.

    Return value is whatever the transcript stored — typically a
    list of content blocks (for tool-use / text / tool_result blocks)
    but can also be a plain string in flat-format transcripts, or
    None when no content is present. Callers must `isinstance` check
    before iterating.
    """
    msg = entry.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if content is not None:
            return content
    return entry.get("content")


def _extract_files_touched(messages: list[dict]) -> list[str]:
    """Extract unique file paths from Edit/Write/MultiEdit tool_use blocks.

    Shared between extract_session_metadata (SessionEnd write path) and
    parse_full_transcript (/recall read path) to prevent drift. Handles
    both CC and flat transcript shapes via _entry_content.
    """
    files_seen: list[str] = []
    for entry in messages:
        content = _entry_content(entry)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in (
                "Edit",
                "Write",
                "MultiEdit",
            ):
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                fp = inp.get("file_path", "")
                if fp and fp not in files_seen:
                    files_seen.append(fp)
    return files_seen


def _extract_errors(messages: list[dict]) -> list[str]:
    """Extract unique error snippets from tool_result blocks with is_error=true.

    Shared between extract_session_metadata (SessionEnd write path) and
    parse_full_transcript (/recall read path) to prevent drift. Handles
    both CC and flat transcript shapes via _entry_content.
    """
    errors: list[str] = []
    for entry in messages:
        content = _entry_content(entry)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result" and block.get("is_error"):
                err_content = block.get("content", "")
                if isinstance(err_content, str) and err_content.strip():
                    snippet = err_content.strip()[:200]
                    if snippet not in errors:
                        errors.append(snippet)
    return errors


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


def _dedup_summary_open_items(summary_text: str, existing_items: list) -> str:
    """Remove duplicate open items from AI-generated summary text.

    Operates on the string before disk write. Uses find_duplicates()
    for matching — same logic as dedup_note_open_items() but on a string.
    """
    # Lazy import to avoid top-level dependency on hooks/ being on sys.path
    _hooks_dir = os.path.dirname(os.path.abspath(__file__))
    if _hooks_dir not in sys.path:
        sys.path.insert(0, _hooks_dir)
    from open_item_dedup import find_duplicates

    # Find the ## Open Questions / Next Steps section
    pattern = r'(## Open Questions / Next Steps\n)(.*?)(?=\n## |\Z)'
    match = re.search(pattern, summary_text, re.DOTALL)
    if not match:
        return summary_text

    section_header = match.group(1)
    section_body = match.group(2)

    # Parse individual - [ ] items
    lines = section_body.split('\n')
    kept_lines: list[str] = []
    removed = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('- [ ] '):
            item_text = stripped[6:]
            dupes = find_duplicates(item_text, existing_items)
            # Only auto-remove high-confidence matches; fuzzy could be false positives
            high_dupes = [d for d in dupes if d[3] == "high"]
            if high_dupes:
                removed = True
                continue  # drop this line
        kept_lines.append(line)

    if not removed:
        return summary_text

    new_body = '\n'.join(kept_lines)
    # If all items removed, add placeholder
    if not any(l.strip().startswith('- [ ]') for l in kept_lines):
        new_body = 'None.'
    # Ensure consistent trailing newline before next section
    if not new_body.endswith('\n'):
        new_body += '\n'

    return summary_text[:match.start()] + section_header + new_body + summary_text[match.end():]


def parse_importance(summary_text: str) -> int:
    """Extract importance score (1-10) from summary text.

    Looks for either '## Importance\\nN' or 'IMPORTANCE: N' format.
    Returns 5 (default) if not found or invalid.
    """
    # Try ## Importance section
    match = re.search(r'##\s*Importance\s*\n\s*(\d+)', summary_text)
    if match:
        score = int(match.group(1))
        return max(1, min(10, score))
    # Try IMPORTANCE: N format (sub-agent output)
    match = re.search(r'IMPORTANCE:\s*(\d+)', summary_text)
    if match:
        score = int(match.group(1))
        return max(1, min(10, score))
    return 5


SNAPSHOT_SUMMARY_PROMPT = """You are a technical summarizer. You will be given the tail of a Claude Code session, captured the moment before the user invoked /compact or /clear. Your job is to produce a SHORT pre-compact checkpoint summary. Do NOT respond conversationally. Do NOT ask questions.

Context: the user is about to lose the in-context conversation. What they need from this summary later is: (1) what they were working on, (2) what progress they had just made, (3) what was in flight — unanswered questions, half-finished thoughts, assumptions not yet tested.

Do NOT restate decisions that were already committed or finalized — those will be captured in the parent session's cohesive summary later. Focus on what's transient and in flight.

TRANSCRIPT:
{transcript}

OUTPUT EXACTLY these two sections with no preamble, no commentary:

## Summary
2-4 sentences: what was happening, what progress mattered, why context was about to be compacted or cleared.

## Key context that may be lost (summary)
- 3-7 bullets: in-flight decisions, open questions, silent assumptions, hypotheses not yet verified.
"""


# Model-escalation chain for the summarizer (#165). After the primary model
# (summary_model, default "haiku") fails with a *quality* reason, retry with a
# more capable model. Sonnet is the first fallback (~3x cost) before Opus (~5x),
# replacing the old behavior where the recall Phase-2 sub-agent fell straight to
# Opus. CLI aliases passed to `claude -p --model`.
_SUMMARY_FALLBACK_CHAIN = ("sonnet", "opus")
# Only these generate_summary failure reasons warrant escalating to a more
# capable model. Timeouts (haiku_timeout) are NOT escalated — a larger model is
# slower and would worsen the slow-CLI problem (#84). Subprocess errors
# (haiku_subprocess_error: CLI missing / non-zero rc) are NOT escalated — a
# different model won't fix an environment problem.
_MODEL_ESCALATION_REASONS = frozenset({"empty_output"})

# Capability rank for escalation: only escalate to a MORE capable model than the
# primary. Prevents a backwards chain when summary_model is explicitly "opus"/"sonnet".
_MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


def _escalation_models(primary: str) -> list[str]:
    """Ordered model list: the primary, then any fallback-chain model strictly
    more capable than the primary (#165). e.g. haiku -> [haiku, sonnet, opus];
    sonnet -> [sonnet, opus]; opus -> [opus]."""
    base = _MODEL_RANK.get(primary, 0)
    return [primary] + [m for m in _SUMMARY_FALLBACK_CHAIN if _MODEL_RANK.get(m, 0) > base]


# ---------------------------------------------------------------------------
# Summary recovery (#167)
# ---------------------------------------------------------------------------
#
# Escalation / fallback decision matrix for structurally-loose summaries:
#
#   empty_output           (whitespace-only or no text)         -> ESCALATE (a)
#                           handled by the #165 chain in upgrade_unsummarized_note
#
#   haiku_timeout          (subprocess.TimeoutExpired)          -> do NOT escalate
#                           a larger model is slower; falls to solo/sub-agent
#
#   haiku_subprocess_error (CLI missing / rc != 0)              -> do NOT escalate
#                           different model won't fix the env; falls back
#
#   schema-loose but non-empty                                  -> RECOVER (b)
#       (missing/variant headings, missing sections,
#        missing ## Importance)
#       -> apply _normalize_summary, do NOT escalate or fall back
#
# _normalize_summary implements RECOVER (b).  It is conservative: it never
# fabricates Summary *content*.  If no recognisable Summary heading is found,
# the text is returned unchanged so the downstream ## Summary check still
# fails and the note escalates / falls back as before.


_CANONICAL_SECTIONS: list[str] = [
    "Summary",
    "Key Decisions",
    "Changes Made",
    "Errors Encountered",
    "Open Questions / Next Steps",
    "Importance",
]

# Pre-compiled per-section heading-normalization patterns (case-insensitive,
# multiline).  Built once at import time; re-used across calls.
_HEADING_NORM_PATTERNS: list[tuple[re.Pattern, str]] = []
for _sec in _CANONICAL_SECTIONS:
    _esc = re.escape(_sec)
    # Matches ATX headings NOT at level 2 (#{1}(?!#) = level-1 only;
    # #{3,6} = levels 3-6), bold-markdown, or bare "Name:" forms.
    # Deliberately excludes "## <Name>" so already-correct headings are
    # left unchanged (idempotent — re-running on a well-formed summary
    # must produce zero recovery_notes).
    _pat = re.compile(
        r"(?m)^(?:#{1}(?!#)\s*" + _esc + r"\s*|"
        r"#{3,6}\s*" + _esc + r"\s*|"
        r"\*\*\s*" + _esc + r"\s*\*\*\s*:?\s*|"
        + _esc + r"\s*:\s*)$",
        re.IGNORECASE,
    )
    _HEADING_NORM_PATTERNS.append((_pat, f"## {_sec}"))
del _sec, _esc, _pat  # clean up loop variables from module namespace

# Pre-compiled per-section presence regexes for Step-3 section synthesis.
# Built once at import time alongside _HEADING_NORM_PATTERNS.
_SECTION_PRESENCE_RES: dict[str, re.Pattern] = {
    name: re.compile(rf"^## {re.escape(name)}\s*$", re.MULTILINE)
    for name in _CANONICAL_SECTIONS
}

# The one regex that upgrade_note_with_summary uses as its gating check.
_SUMMARY_SECTION_RE = re.compile(r"^## Summary\s*$", re.MULTILINE)


def _normalize_summary(text: str) -> tuple[str, list[str]]:
    """Recover a structurally-loose AI summary so it passes the downstream
    ``## Summary`` check and section parsing, instead of escalating to a
    costlier model or falling through to the sub-agent (#167).

    Returns ``(normalized_text, recovery_notes)``.  CONSERVATIVE: it only
    acts on recognizable structure and NEVER fabricates Summary *content* —
    if no recognizable summary heading exists, the original text is returned
    unchanged so the note still fails and escalates/falls back.

    Recoveries (each appended to ``recovery_notes`` when applied):
      - heading variant normalization: ``# Summary``, ``### Summary``,
        ``**Summary**``, ``Summary:`` (bare) -> ``## Summary``, for each
        canonical section name (case-insensitive on the section name).
      - synthesize a missing NON-summary section as ``## <Name>\\nNone.``
        (only Key Decisions / Changes Made / Errors Encountered /
        Open Questions / Next Steps — NOT Summary).
      - if neither a ``## Importance`` heading nor an ``IMPORTANCE:`` line
        is present, append ``## Importance\\n5`` (default importance).
    """
    recovery_notes: list[str] = []

    # Step 1: heading normalization — replace variant headings with canonical
    # ## forms.  Operates LINE-BY-LINE to skip fenced code blocks, so a line
    # like `# Summary` inside a ``` or ~~~ fence is left unchanged (idempotency
    # fix: well-formed summaries that contain code blocks are not mutated).
    #
    # Implementation note: the patterns use MULTILINE `$` which matches before
    # `\n` but does NOT consume it.  When applied via subn() to an individual
    # line that ends with `\n`, the match covers everything up to (not including)
    # the `\n`, so the replacement text lacks the trailing newline.  We therefore
    # split with keepends=True but strip/restore the line ending ourselves so the
    # substitution result is re-joined correctly.
    lines = text.splitlines(keepends=True)
    in_fence = False
    out_lines: list[str] = []
    # Track which section names were recovered so we can build recovery_notes
    # after the line loop (one note per section, regardless of line count).
    recovered_sections: set[str] = set()
    for line in lines:
        stripped = line.rstrip("\n").rstrip()
        # Toggle fence state on opening/closing fence markers.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            # Inside a code fence — pass through unchanged.
            out_lines.append(line)
            continue
        # Outside a fence — apply heading-normalization patterns.
        # Work on the bare content (no trailing newline) so the replacement is
        # not affected by the `$`-before-`\n` consumption, then restore the eol.
        eol = line[len(line.rstrip("\n")):]  # "\n" or "" for last line
        bare = line.rstrip("\n")
        new_bare = bare
        for pattern, replacement in _HEADING_NORM_PATTERNS:
            sec_name = replacement[3:]  # strip leading "## "
            result, n_subs = pattern.subn(replacement, new_bare)
            if n_subs:
                new_bare = result
                recovered_sections.add(sec_name)
        out_lines.append(new_bare + eol)
    normalized = "".join(out_lines)
    for sec_name in recovered_sections:
        recovery_notes.append(f"normalized heading: {sec_name}")

    # Step 2: if the gating regex still doesn't match after normalization,
    # recovery cannot succeed — return the ORIGINAL text unchanged so the
    # downstream ## Summary check fails and the note escalates/falls back.
    if not _SUMMARY_SECTION_RE.search(normalized):
        return text, []

    # Step 3: synthesize missing non-Summary, non-Importance sections.
    # Uses pre-compiled _SECTION_PRESENCE_RES — built once at import time.
    for sec_name in _CANONICAL_SECTIONS:
        if sec_name in ("Summary", "Importance"):
            continue
        if not _SECTION_PRESENCE_RES[sec_name].search(normalized):
            normalized = normalized.rstrip("\n") + f"\n\n## {sec_name}\nNone."
            recovery_notes.append(f"synthesized section: {sec_name}")

    # Step 4: default importance when both ## Importance and IMPORTANCE: N are absent.
    has_importance_heading = re.search(r"^## Importance", normalized, re.MULTILINE)
    has_importance_line = re.search(r"^\s*IMPORTANCE:\s*\d+", normalized, re.MULTILINE)
    if not has_importance_heading and not has_importance_line:
        normalized = normalized.rstrip("\n") + "\n\n## Importance\n5"
        recovery_notes.append("defaulted importance")

    return normalized, recovery_notes


def _summary_recovery_enabled() -> bool:
    """Return True when summary_recovery is enabled in config (default True).

    Config errors must not break summarization — returns True on any exception.
    """
    try:
        return bool(load_config().get("summary_recovery", True))
    except Exception:  # noqa: BLE001 — config errors must not break summarization
        return True


def generate_snapshot_summary(
    user_msgs: list[str],
    assistant_msgs: list[str],
    metadata: dict,
    model: str = "haiku",
    timeout: int = 120,
) -> tuple[str | None, str | None]:
    """Call ``claude -p --model <model>`` with the snapshot-specific prompt.

    Returns ``(summary_text, fallback_reason)``:
      * On success: ``(text, None)``
      * On failure: ``(None, "haiku_timeout" | "haiku_subprocess_error" | "empty_output" | "unknown_failure")``

    Reuses the retry/timeout pattern of generate_summary but with a tighter
    scope (no open-item dedup, no importance scoring — snapshots don't carry
    those structural fields).

    Note: snapshot summaries are out of scope for the Phase 2 #167 validator
    (which targets structured session summaries); reserved reasons such as
    ``"missing_section"`` / ``"importance_missing"`` / ``"schema_loose"`` do
    not apply here.
    """
    sampled_user = user_msgs[-10:] if len(user_msgs) > 10 else user_msgs
    sampled_asst = assistant_msgs[-10:] if len(assistant_msgs) > 10 else assistant_msgs
    user_sample = "\n---\n".join(sampled_user)[:8000]
    asst_sample = "\n---\n".join(sampled_asst)[:8000]
    transcript = f"USER MESSAGES:\n{user_sample}\n\n---\n\nASSISTANT MESSAGES:\n{asst_sample}"

    prompt = SNAPSHOT_SUMMARY_PROMPT.format(transcript=transcript)

    attempts = (timeout, timeout * 2)
    last_reason: str | None = None
    for i, attempt_timeout in enumerate(attempts):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", model],
                input=prompt, capture_output=True, text=True, timeout=attempt_timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip(), None
            if result.returncode != 0:
                last_reason = "haiku_subprocess_error"
            else:
                last_reason = "empty_output"
            print(f"[obsidian-brain] claude -p (snapshot) failed (rc={result.returncode})",
                  file=sys.stderr)
            break
        except FileNotFoundError:
            last_reason = "haiku_subprocess_error"
            print("[obsidian-brain] claude CLI not found", file=sys.stderr)
            break
        except subprocess.TimeoutExpired:
            last_reason = "haiku_timeout"
            if i < len(attempts) - 1:
                print(f"[obsidian-brain] claude -p (snapshot) timed out at {attempt_timeout}s, retrying...",
                      file=sys.stderr)
                continue
            print(f"[obsidian-brain] claude -p (snapshot) timed out at {attempt_timeout}s",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            last_reason = "haiku_subprocess_error"
            print(f"[obsidian-brain] claude -p (snapshot) error: {exc}", file=sys.stderr)
            break
    return None, last_reason or "unknown_failure"


def generate_theme_names(
    clusters: list[dict],
    model: str = "haiku",
    timeout: int = 120,
) -> tuple[list[dict] | None, str | None]:
    """Name + summarize N clusters in ONE ``claude -p --model <model>`` spawn.

    ``clusters`` items: ``{"top_terms": [...], "sample_titles": [...]}``.
    Returns ``([{"name","summary"}, ...], None)`` with exactly ``len(clusters)``
    entries on success, or ``(None, reason)`` on failure
    (``"haiku_timeout" | "haiku_subprocess_error" | "empty_output" |
    "parse_error" | "count_mismatch" | "unknown_failure"``). Never raises.
    """
    if not clusters:
        return [], None

    lines = [
        "You are naming clusters of related notes for a knowledge base.",
        "For EACH cluster below, return a short Title Case name (<= 6 words) and a "
        "one-sentence summary. Respond with ONLY a JSON array of "
        '{"name": str, "summary": str}, one object per cluster, in order.',
        "",
    ]
    for i, c in enumerate(clusters):
        terms = ", ".join(c.get("top_terms", [])[:10])
        titles = "; ".join(c.get("sample_titles", [])[:5])
        lines.append(f"Cluster {i + 1}: top terms = [{terms}]; sample titles = [{titles}]")
    prompt = "\n".join(lines)

    attempts = (timeout, timeout * 2)
    last_reason = "unknown_failure"
    for idx, attempt_timeout in enumerate(attempts):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", model],
                input=prompt, capture_output=True, text=True, timeout=attempt_timeout,
            )
        except FileNotFoundError:
            return None, "haiku_subprocess_error"
        except subprocess.TimeoutExpired:
            last_reason = "haiku_timeout"
            if idx == 0:
                continue
            return None, last_reason
        if result.returncode != 0:
            last_reason = "haiku_subprocess_error"
            break
        raw = result.stdout.strip()
        if not raw:
            last_reason = "empty_output"
            break
        # Strip ```json fences if present.
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None, "parse_error"
        if not isinstance(parsed, list) or len(parsed) != len(clusters):
            return None, "count_mismatch"
        out = [{"name": str(p.get("name", "")).strip(),
                "summary": str(p.get("summary", "")).strip()} for p in parsed]
        if any(not o["name"] for o in out):
            return None, "parse_error"
        return out, None
    return None, last_reason


def generate_summary(
    user_msgs: list[str],
    assistant_msgs: list[str],
    metadata: dict,
    model: str = "haiku",
    timeout: int = 120,
) -> tuple[str | None, str | None]:
    """Call ``claude -p --model <model>`` to summarize the session.

    Returns ``(summary_text, fallback_reason)``:
      * On success: ``(text, None)``
      * On failure: ``(None, "haiku_timeout" | "haiku_subprocess_error" | "empty_output" | "unknown_failure")``

    Reserved future reasons (will be populated downstream by the Phase 2 #167/#84
    validator, not returned by this function):
      ``"missing_section"``, ``"importance_missing"``, ``"schema_loose"``.

    Samples first 10 + last 10 messages for large sessions.
    """
    # Sample messages for large sessions
    if len(user_msgs) > 20:
        sampled_user = (
            user_msgs[:10] + ["[... middle messages omitted ...]"] + user_msgs[-10:]
        )
    else:
        sampled_user = user_msgs

    if len(assistant_msgs) > 20:
        sampled_asst = (
            assistant_msgs[:10]
            + ["[... middle messages omitted ...]"]
            + assistant_msgs[-10:]
        )
    else:
        sampled_asst = assistant_msgs

    user_sample = "\n---\n".join(sampled_user)[:12000]
    assistant_sample = "\n---\n".join(sampled_asst)[:12000]

    preamble = metadata.get("snapshot_preamble", "")
    cohesion_hint = ""
    if preamble:
        cohesion_hint = (
            "\nSome earlier context may come from pre-compact snapshots — "
            "synthesize the whole arc into a cohesive narrative rather than "
            "three disjoint summaries.\n"
        )

    prompt = f"""You are a technical summarizer. You will be given the transcript of a Claude Code coding session. Your job is to produce a structured summary. Do NOT respond conversationally. Do NOT ask questions. Just output the summary.
{cohesion_hint}
SESSION METADATA:
- Project: {metadata.get('project', 'unknown')}
- Branch: {metadata.get('git_branch', 'unknown')}
- Duration: {metadata.get('duration_minutes', 0)} minutes
- Files touched: {', '.join(metadata.get('files_touched', [])[:15]) or 'none detected'}

{preamble}

TRANSCRIPT (user and assistant messages):
{user_sample}

---

{assistant_sample}

OUTPUT EXACTLY these markdown sections with no preamble, no commentary, no questions:

## Summary
1-3 sentence overview of what was accomplished in this session.

## Key Decisions
- Bullet list of important technical decisions made. Write "None noted." if none.

## Changes Made
- Bullet list of files modified/created with brief description. Write "None noted." if none.

## Errors Encountered
- Bullet list of errors and how resolved. Write "None." if none.

## Open Questions / Next Steps
- [ ] Checkbox list of unresolved items. Write "None." if none.

## Importance
Rate this session 1-10. 1-3: trivial (config, interrupted). 4-6: standard work. 7-8: key decisions or error resolutions. 9-10: major releases or security audits. Output ONLY the number.
"""

    # Layer 1: Append existing open items to prevent AI duplication
    existing_items = []
    if metadata.get("vault_path") and metadata.get("sessions_folder"):
        try:
            _hooks_dir = os.path.dirname(os.path.abspath(__file__))
            if _hooks_dir not in sys.path:
                sys.path.insert(0, _hooks_dir)
            from open_item_dedup import collect_open_items
            existing_items = collect_open_items(
                metadata["vault_path"],
                metadata["sessions_folder"],
                metadata.get("project", "unknown"),
                max_sessions=10,
            )
        except Exception as exc:
            print(f"[obsidian-brain] open item collection failed (non-fatal): {exc}", file=sys.stderr)

    if existing_items:
        prompt += "\n\n## Existing Open Items for This Project (DO NOT DUPLICATE)\n"
        prompt += "The following items are already tracked in older session notes. Do NOT include any item\n"
        prompt += "that is semantically equivalent to these — same PR, same branch, same task, same file.\n"
        prompt += "Only add genuinely NEW open items from this session's conversation.\n\n"
        for _, _, item_text in existing_items:
            prompt += f"- {item_text}\n"

    attempts = (timeout, timeout * 2)  # escalate on first timeout
    last_reason: str | None = None
    for i, attempt_timeout in enumerate(attempts):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", model],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=attempt_timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                summary_text = result.stdout.strip()
                # Layer 2: Post-generation dedup pass (string-based, pre-write)
                if existing_items:
                    summary_text = _dedup_summary_open_items(summary_text, existing_items)
                return summary_text, None
            if result.returncode != 0:
                last_reason = "haiku_subprocess_error"
                print(
                    f"[obsidian-brain] claude -p failed (rc={result.returncode}): "
                    f"{result.stderr[:200]}",
                    file=sys.stderr,
                )
                break  # non-timeout failure, don't retry
            last_reason = "empty_output"
            print(
                f"[obsidian-brain] claude -p failed (rc={result.returncode}): "
                f"{result.stderr[:200]}",
                file=sys.stderr,
            )
            break  # empty stdout, don't retry
        except FileNotFoundError:
            last_reason = "haiku_subprocess_error"
            print(
                "[obsidian-brain] claude CLI not found, summarization unavailable",
                file=sys.stderr,
            )
            break  # won't succeed on retry
        except subprocess.TimeoutExpired as exc:
            last_reason = "haiku_timeout"
            stderr_snippet = f" stderr: {exc.stderr[:200]}" if exc.stderr else ""
            if i < len(attempts) - 1:
                print(f"[obsidian-brain] claude -p timed out at {attempt_timeout}s, retrying with {attempts[i+1]}s{stderr_snippet}", file=sys.stderr)
                continue
            print(f"[obsidian-brain] claude -p timed out at {attempt_timeout}s, giving up{stderr_snippet}", file=sys.stderr)
        except Exception as exc:
            last_reason = "haiku_subprocess_error"
            print(f"[obsidian-brain] claude -p error ({type(exc).__name__}): {exc}", file=sys.stderr)
            break  # unknown error, don't retry

    return None, last_reason or "unknown_failure"


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (re.compile(r'gh[ps]_[A-Za-z0-9_]{36,}'), '[REDACTED:github-token]'),
    (re.compile(r'AKIA[0-9A-Z]{16}'), '[REDACTED:aws-key]'),
    (re.compile(r'(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+'), r'\1=[REDACTED]'),
    (re.compile(r'-----BEGIN [A-Z ]+-----'), '[REDACTED:pem-header]'),
    (re.compile(r'(?i)Bearer\s+[A-Za-z0-9._\-]{20,}'), 'Bearer [REDACTED]'),
    (re.compile(r'(?i)(key|secret|token)\s*[=:]\s*[A-Za-z0-9+/=]{40,}'), r'\1=[REDACTED:base64]'),
]


def scrub_secrets(text: str) -> str:
    """Best-effort redaction of common secret patterns."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def escape_wikilinks(text: str) -> str:
    """Escape ``[[`` so Obsidian does not parse bash conditionals as wikilinks.

    Bash ``[[ $VAR == pattern ]]`` in conversation excerpts triggers
    Obsidian's wikilink parser, creating spurious outgoing links.
    """
    return text.replace("[[", r"\[\[")


# ---------------------------------------------------------------------------
# Vault operations
# ---------------------------------------------------------------------------


def write_vault_note(
    vault_path: str, folder: str, filename: str, content: str
) -> Optional[str]:
    """Atomic write: temp file + chmod 0o600 + rename into vault folder.

    Creates the target folder if it does not exist.

    Returns:
        None on success.
        A non-empty error string on failure (F2 contract — callers check ``if err:``).
    """
    dest_dir = Path(vault_path) / folder
    dest = dest_dir / filename

    # Path traversal check — BEFORE any filesystem side effects
    vault_real = Path(vault_path).resolve()
    if not dest.resolve().is_relative_to(vault_real):
        msg = f"path traversal blocked: {dest}"
        print(f"[obsidian-brain] {msg}", file=sys.stderr)
        return msg

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"cannot create vault dir {dest_dir}: {exc}"
        print(f"[obsidian-brain] {msg}", file=sys.stderr)
        return msg

    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(dest_dir), prefix=".ob-", suffix=".md.tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.chmod(tmp_path, 0o600)
            os.rename(tmp_path, str(dest))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        msg = f"write failed for {dest}: {exc}"
        print(f"[obsidian-brain] {msg}", file=sys.stderr)
        return msg

    print(f"[obsidian-brain] wrote {dest}", file=sys.stderr)
    return None


def flip_note_status(path: str, old_status: str, new_status: str) -> bool:
    """Atomically change a note's frontmatter status field.

    Reads the file, replaces 'status: <old>' with 'status: <new>' in the
    frontmatter, and writes back via temp file + rename.
    Returns True on success.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        print(f"[obsidian-brain] cannot read {path}: {exc}", file=sys.stderr)
        return False

    old_line = f"status: {old_status}"
    new_line = f"status: {new_status}"

    # Constrain replacement to the frontmatter block (between --- delimiters)
    if not content.startswith("---"):
        return False
    end_idx = content.index("\n---", 3) + 1 if "\n---" in content[3:] else -1
    if end_idx < 0:
        return False
    frontmatter = content[:end_idx]
    if old_line not in frontmatter:
        return False

    new_content = frontmatter.replace(old_line, new_line, 1) + content[end_idx:]

    dir_path = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=".ob-flip-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            orig_mode = stat.S_IMODE(os.stat(path).st_mode)
            os.chmod(tmp_path, orig_mode)
            os.rename(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        print(f"[obsidian-brain] flip_note_status failed for {path}: {exc}", file=sys.stderr)
        return False

    return True


def find_latest_session(
    vault_path: str, sessions_folder: str, project: str
) -> dict | None:
    """Find the most recent session note for a project.

    Searches YAML frontmatter for ``project: <project>``.
    Returns ``{date, summary, next_steps}`` or None.
    """
    sessions_dir = Path(vault_path) / sessions_folder
    if not sessions_dir.exists():
        return None

    slug = slugify(project)
    # Collect candidate files sorted by name descending (newest date first)
    candidates = sorted(sessions_dir.glob("*.md"), reverse=True)

    for note_path in candidates:
        try:
            text = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Quick check: does frontmatter mention this project?
        # Look for project: <name> in the YAML block
        fm_end = text.find("\n---", 3)  # skip opening ---
        if fm_end == -1:
            continue
        frontmatter = text[: fm_end + 4]

        # Match project field (case-insensitive basename or slug)
        fm_project = parse_frontmatter_field(frontmatter, "project")
        if not fm_project:
            continue
        if fm_project.lower() != project.lower() and slugify(fm_project) != slug:
            continue

        # Extract date from frontmatter
        date_str = parse_frontmatter_field(frontmatter, "date") or ""

        # Extract summary section
        summary = ""
        summary_match = re.search(
            r"## Summary\n(.+?)(?=\n## |\Z)", text, re.DOTALL
        )
        if summary_match:
            summary = summary_match.group(1).strip()

        # Extract next steps section
        next_steps = ""
        ns_match = re.search(
            r"## Open Questions / Next Steps\n(.+?)(?=\n## |\Z)", text, re.DOTALL
        )
        if ns_match:
            next_steps = ns_match.group(1).strip()

        return {"date": date_str, "summary": summary, "next_steps": next_steps}

    return None


def _parse_note_tags(frontmatter: str) -> list[str]:
    """Parse note tags from frontmatter, handling both YAML forms (#168).

    Supports:
    - Inline list:  ``tags: [claude/session, claude/keep]``
    - Block list::
        tags:
          - claude/session
          - claude/keep

    Returns a list of stripped tag strings, or [] when no tags are found.
    """
    # Try inline form first: tags: [a, b] or tags: a, b
    inline_raw = parse_frontmatter_field(frontmatter, "tags")
    if inline_raw:
        cleaned = inline_raw.strip().lstrip("[").rstrip("]")
        tags = [t.strip().strip('"').strip("'")
                for t in re.split(r"[,\s]+", cleaned) if t.strip()]
        if tags:
            return tags

    # Try YAML block-list form:
    #   tags:
    #     - claude/session
    #     - claude/keep
    block_match = re.search(
        r'^tags:\s*\n((?:[ \t]+-[ \t]+.+\n?)+)', frontmatter, re.MULTILINE
    )
    if block_match:
        block = block_match.group(1)
        tags = [t.strip().strip('"').strip("'")
                for t in re.findall(r'^[ \t]+-[ \t]+(.+)$', block, re.MULTILINE)
                if t.strip()]
        if tags:
            return tags

    return []


def _note_has_inbound_links(basename_stem: str, db_path: str | None = None) -> bool:
    """Return True if any vault note body references ``[[<basename_stem>]]`` (#168).

    Queries the existing vault index DB (read-only) via a body LIKE lookup —
    no full-vault file rescan. CONSERVATIVE on failure: if the index is missing
    or the query errors, returns True (assume referenced) so the note is NOT
    deferred — deferral is an optimization and must never drop a referenced note.
    Over-matching (prefix) also errs toward True (safe).
    """
    try:
        if db_path is None:
            if _vault_index is not None:
                db_path = _vault_index._default_db_path()
            else:
                # Fall back to the known default path string directly
                db_path = os.path.join(os.path.expanduser("~"), ".claude", "obsidian-brain-vault.db")
        if not os.path.exists(db_path):
            return True  # conservative: assume referenced
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)  # noqa: vault-db-connect — opens via uri=True (file:...?mode=ro) which _connect() does not accept (plain-path connect + realpath guard can't parse a URI); read-only, so it cannot pollute
        try:
            # Escape LIKE wildcards in the stem so that `_` (common in project
            # slugs) is treated as a literal character, not a single-char wildcard.
            esc = basename_stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%[[{esc}%"
            row = conn.execute(
                "SELECT 1 FROM notes WHERE body LIKE ? ESCAPE '\\' LIMIT 1", (pattern,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception as exc:
        print(f"[obsidian-utils] inbound-link check failed for {basename_stem!r}: {exc}", file=sys.stderr)
        return True  # conservative: assume referenced on any error


def find_unsummarized_notes(
    vault_path: str,
    sessions_folder: str,
    project: str,
    aged_threshold_days: int | None = None,
    include_aged: bool = False,
    pin_tags: tuple | list | None = None,
) -> str:
    """Find unsummarized session notes for a project, with defense-in-depth.

    Scans sessions folder for notes with status: auto-logged in frontmatter,
    filters by project, and checks for false positives (notes that already
    have a real ## Summary but stale status). Auto-fixes stale status inline.

    Accepts notes of type ``claude-session`` and ``claude-snapshot``; rejects
    other typed notes (e.g. ``claude-insight``). Legacy notes without a
    ``type:`` field are accepted for backward compatibility.

    Aged-note deferral (#168): notes are *deferred* (skipped, not lost) when
    ALL three conditions hold:
      1. File mtime is older than ``aged_threshold_days`` (default from config
         key ``aged_summarize_threshold_days``, built-in default 90 days).
      2. No inbound ``[[wikilink]]`` references found in the vault index DB.
      3. No pin tag (tags matching ``summary_pin_tags``, default
         ``["claude/keep", "claude/permanent"]``) in the note's frontmatter.

    Deferred notes are returned in ``skipped_aged`` and can be forced into
    ``unsummarized`` by passing ``include_aged=True`` or invoking
    ``/recall --include-aged``.

    Args:
        vault_path: Obsidian vault root.
        sessions_folder: Folder name (relative to vault) containing sessions.
        project: Project slug to filter on.
        aged_threshold_days: Override the config-backed deferral threshold (days).
            None (default) reads from config key ``aged_summarize_threshold_days``.
        include_aged: If True, bypass deferral — all qualifying notes go into
            ``unsummarized`` regardless of age/link/pin status.
        pin_tags: Override the config-backed pin-tag list. None (default) reads
            from config key ``summary_pin_tags``.

    Returns JSON string:
        {"unsummarized": [paths], "auto_fixed": N, "skipped_aged": [paths]}
    """
    # Resolve config-backed defaults once.
    if aged_threshold_days is None:
        try:
            aged_threshold_days = int(load_config().get("aged_summarize_threshold_days", 90))
        except Exception:
            aged_threshold_days = 90
    if pin_tags is None:
        try:
            pin_tags = list(load_config().get("summary_pin_tags", ["claude/keep", "claude/permanent"]))
        except Exception:
            pin_tags = ["claude/keep", "claude/permanent"]

    now = time.time()
    cutoff_seconds = aged_threshold_days * 86400

    sessions_dir = Path(vault_path) / sessions_folder
    if not sessions_dir.is_dir():
        return json.dumps({"unsummarized": [], "auto_fixed": 0, "skipped_aged": []})

    unsummarized: list[str] = []
    skipped_aged: list[str] = []
    auto_fixed = 0

    for f in sorted(sessions_dir.iterdir(), reverse=True):
        if f.suffix != '.md':
            continue

        # Read ENTIRE file from disk — DO NOT use read_note_metadata() which
        # has a persistent cache that may be stale after status changes.
        try:
            content = f.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            print(f"[obsidian-brain] cannot read {f.name}: {exc}", file=sys.stderr)
            continue

        # Parse frontmatter inline (no cache)
        if not content.startswith('---'):
            continue
        fm_end = content.find('\n---', 3)
        if fm_end == -1:
            continue
        frontmatter = content[:fm_end]

        # Must be auto-logged
        status_val = parse_frontmatter_field(frontmatter, "status")
        if status_val != "auto-logged":
            continue

        # Type filter — accept both sessions and snapshots. Legacy notes
        # without a `type:` field — and notes with an empty `type:` value —
        # are kept (current permissive behavior; #94 made these equivalent).
        type_val = parse_frontmatter_field(frontmatter, "type")
        if type_val and type_val not in ("claude-session", "claude-snapshot"):
            continue

        # Must match project
        fm_project = parse_frontmatter_field(frontmatter, "project")
        if not fm_project:
            continue
        if fm_project.lower() != project.lower() and slugify(fm_project) != slugify(project):
            continue

        # Defense-in-depth: check if already has a real summary
        has_summary = bool(re.search(r'^## Summary', content, re.MULTILINE))
        has_unavailable = 'AI summary unavailable' in content

        if has_summary and not has_unavailable:
            # Already summarized by legacy code path — fix status on disk
            try:
                fixed = re.sub(
                    r'^status: auto-logged',
                    'status: summarized',
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )
                # Atomic write: temp file + rename (per CLAUDE.md convention)
                fd, tmp = tempfile.mkstemp(
                    prefix='.ob-fix-', suffix='.md.tmp', dir=str(f.parent)
                )
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as fw:
                        fw.write(fixed)
                    os.replace(tmp, str(f))
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    continue
                # Invalidate cache for this file
                sid = _get_session_id_fast()
                cache_key = f"metadata:{os.path.realpath(str(f))}"
                cache_set(sid, cache_key, None)
                auto_fixed += 1
            except OSError:
                pass
            continue

        # Aged-note deferral (#168): skip notes that are old, unreferenced,
        # and not pinned — deferral is an optimization, never a permanent drop.
        if not include_aged:
            file_age = now - f.stat().st_mtime
            if file_age > cutoff_seconds:
                # AGE condition met — check pin tags next.
                # Use _parse_note_tags to handle both inline and block-list YAML
                # forms; parse_frontmatter_field alone misses the block-list form
                # used by obsidian_session_log.py (#168 PR review fix).
                note_tags: list[str] = _parse_note_tags(frontmatter)
                pinned = any(pt in note_tags for pt in pin_tags)
                if not pinned:
                    # INBOUND-LINK check — conservative: assume linked on error.
                    if not _note_has_inbound_links(f.stem):
                        skipped_aged.append(str(f))
                        continue

        unsummarized.append(str(f))

    # Ordering bias: within a session_id group, snapshots sort first so the
    # per-note pipeline summarizes them before the parent session's cohesion
    # step runs. Advisory only — Section 5's cohesion helper falls back to
    # raw bodies for snapshots not yet upgraded.
    def _bias_key(path_str):
        name = os.path.basename(path_str)
        try:
            content = Path(path_str).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ("", 1, name)
        sid = ""
        typ = ""
        for line in content.splitlines()[:30]:
            line = line.strip()
            if line.startswith("session_id:"):
                sid = line.split(":", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("type:"):
                # Strip both quote styles so `type: "claude-snapshot"` and
                # `type: 'claude-snapshot'` (both valid YAML) sort correctly
                # under the snapshot-first bias. Copilot PR #43 round 2
                # finding — sid-line already strips; type-line didn't.
                typ = line.split(":", 1)[1].strip().strip('"').strip("'")
        return (sid, 0 if typ == "claude-snapshot" else 1, name)

    unsummarized.sort(key=_bias_key)
    return json.dumps({"unsummarized": unsummarized, "auto_fixed": auto_fixed, "skipped_aged": skipped_aged})


# BH-003: bound for the open-item evidence pool in build_context_brief().
# Mirrors open_item_dedup.collect_open_items()'s own `max_sessions` default —
# items are already capped to that recent window, so evidence built from the
# same window cannot miss a session that could contradict one of them.
_OPEN_ITEM_EVIDENCE_WINDOW = 10


def build_context_brief(
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    project: str,
    hook_status_line: str | None = None,
) -> str:
    """Build the /recall context brief entirely in Python.

    Reads session and insight files directly (no sub-agent), composes
    a structured markdown brief, and runs open-item detection.

    Args:
        vault_path: Obsidian vault root.
        sessions_folder: Folder name (relative to vault) containing sessions.
        insights_folder: Folder name (relative to vault) containing insights.
        project: Project slug to filter on.
        hook_status_line: Optional pre-formatted status line (e.g. "[OK] …" or
            "[WARN] …") to prepend to the brief so /recall can surface SessionStart
            hook health at a glance.

    Returns a structured string with labeled sections:
      CONTEXT_BRIEF: <markdown brief>
      LOAD_MANIFEST: <key-value metadata>
      MOST_RECENT_SESSION_PATH: <path>
      OPEN_ITEM_CANDIDATES: <JSON array or NO_CANDIDATES>
    """
    sessions_dir = Path(vault_path) / sessions_folder
    insights_dir = Path(vault_path) / insights_folder

    # --- 1. Scan and filter sessions ---
    def _safe_sort_key(p: Path) -> tuple:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (p.name[:10], mtime)

    session_files: list[tuple[str, str, dict]] = []  # (filename, path, metadata)
    if sessions_dir.is_dir():
        md_files = [f for f in sessions_dir.iterdir() if f.suffix == '.md']
        for f in sorted(md_files, key=_safe_sort_key, reverse=True):
            meta = read_note_metadata(str(f))
            if not meta:
                continue
            # Exclude snapshot-type notes from the top-level session table.
            if meta.get('type') == 'claude-snapshot':
                continue
            fm_project = meta.get('project', '')
            if fm_project.lower() != project.lower() and slugify(fm_project) != slugify(project):
                continue
            session_files.append((f.name, str(f), meta))

    # --- 2. Read sessions (tiered) ---
    most_recent_summary = ""
    most_recent_open_items = ""
    most_recent_title = ""
    most_recent_date = ""
    most_recent_path = ""
    second_summary = ""
    second_title = ""
    second_date = ""

    _summary_re = re.compile(r"## Summary\n(.+?)(?=\n## |\Z)", re.DOTALL)
    _next_steps_re = re.compile(r"## Open Questions / Next Steps\n(.+?)(?=\n## |\Z)", re.DOTALL)

    if len(session_files) >= 1:
        _, most_recent_path, meta = session_files[0]
        most_recent_date = meta.get('date', '')
        most_recent_title = f"Session: {meta.get('project', project)}"
        if meta.get('git_branch'):
            most_recent_title += f" ({meta['git_branch']})"
        try:
            text = Path(most_recent_path).read_text(encoding='utf-8', errors='replace')
            m = _summary_re.search(text)
            if m:
                most_recent_summary = m.group(1).strip()
                # Use first sentence of summary as title
                first_line = most_recent_summary.split('\n')[0].strip()
                if first_line:
                    most_recent_title = first_line
            m = _next_steps_re.search(text)
            if m:
                most_recent_open_items = m.group(1).strip()
        except OSError:
            most_recent_summary = "(could not read session note)"
        else:
            try:
                if _vault_index is not None:
                    _db = _vault_index._default_db_path()
                    if os.path.isfile(_db):
                        _vault_index.log_access(_db, most_recent_path, "recall", project)
            except Exception as exc:
                print(f"[obsidian-brain] access log for recall context failed: {exc}",
                      file=sys.stderr)

    if len(session_files) >= 2:
        _, second_path, meta = session_files[1]
        second_date = meta.get('date', '')
        second_title = f"Session: {meta.get('project', project)}"
        if meta.get('git_branch'):
            second_title += f" ({meta['git_branch']})"
        try:
            with open(second_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = [f.readline() for _ in range(50)]
            text = ''.join(lines)
            m = _summary_re.search(text)
            if m:
                second_summary = m.group(1).strip()
                # Use first sentence of summary as title
                first_line = second_summary.split('\n')[0].strip()
                if first_line:
                    second_title = first_line
        except OSError:
            second_summary = "(could not read session note)"
        else:
            try:
                if _vault_index is not None:
                    _db = _vault_index._default_db_path()
                    if os.path.isfile(_db):
                        _vault_index.log_access(_db, second_path, "recall", project)
            except Exception as exc:
                print(f"[obsidian-brain] access log for recall context failed: {exc}",
                      file=sys.stderr)

    # History table (last 5 sessions)
    history_rows: list[str] = []
    most_recent_snaps: list[dict] = []
    for i, (fname, fpath, meta) in enumerate(session_files[:5]):
        date = meta.get('date', '')
        title = meta.get('project', project)
        branch = meta.get('git_branch', '')
        # Format duration as readable time
        dur_min = meta.get('duration_minutes', 0)
        try:
            dur_min = float(dur_min)
        except (ValueError, TypeError):
            dur_min = 0.0
        if dur_min >= 60:
            duration = f"{int(dur_min // 60)}h {int(dur_min % 60)}m"
        elif dur_min > 0:
            duration = f"{int(dur_min)}m"
        else:
            duration = ""
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                content_text = f.read()
            # Prefer first sentence of ## Summary as title (more descriptive)
            summary_match = re.search(r'## Summary\n+(.+)', content_text)
            if summary_match:
                title = summary_match.group(1).strip()
            else:
                # Fall back to H1 heading
                for line_text in content_text.split('\n'):
                    if line_text.startswith('# '):
                        title = line_text[2:].strip()
                        break
        except OSError:
            pass
        history_rows.append(f"| {i+1} | {date} | {duration} | {title} | {branch} |")

        # Append indented snapshot rows beneath this session.
        sid = meta.get("session_id", "")
        note_date = meta.get("date", "")
        if sid and note_date:
            _snaps = fetch_snapshot_summaries(sessions_dir, sid, note_date, project)
            if i == 0:
                most_recent_snaps = _snaps
            for sn in _snaps:
                hh = sn["hhmmss"]
                hhmmss_pretty = (
                    f"{hh[:2]}:{hh[2:4]}:{hh[4:]}" if hh and hh != "??????" else "--:--:--"
                )
                snap_title = sn["summary"][:80]
                history_rows.append(
                    f"|   | ↳ {hhmmss_pretty} |  |   {snap_title} | — |"
                )

    # --- 3. Load insights via vault index (layered ranking) ---
    insight_entries: list[tuple[str, str]] = []  # (title, key_point)
    insight_count = 0

    # Collect session context for layered ranking
    _session_ids: list[str] = []
    _session_tags: list[str] = []
    _session_summary = ""
    for _, _, _meta in session_files[:5]:
        sid = _meta.get("session_id", "")
        if sid:
            _session_ids.append(sid)
        for tag in _meta.get("tags", []):
            if "claude/topic/" in tag and tag not in _session_tags:
                _session_tags.append(tag)

    # Build summary from loaded sessions
    if most_recent_summary:
        _session_summary = most_recent_summary

    _use_vault_index = True
    try:
        from vault_index import ensure_index, query_related_notes
    except ImportError:
        _use_vault_index = False

    if _use_vault_index:
        try:
            db_path = ensure_index(vault_path, [sessions_folder, insights_folder])
            ranked_notes = query_related_notes(
                db_path=db_path,
                project=project,
                session_ids=_session_ids,
                session_tags=_session_tags,
                session_summary=_session_summary,
                note_types=["claude-insight", "claude-decision", "claude-error-fix", "claude-retro"],
                limit=20,
            )
            insight_count = len(ranked_notes)
            for note in ranked_notes:
                title = note["title"]
                key_point = ""
                note_path = note["path"]
                try:
                    with open(note_path, "r", encoding="utf-8", errors="replace") as fh:
                        past_frontmatter = False
                        frontmatter_closed = False
                        for line_text in fh:
                            stripped = line_text.strip()
                            if stripped == "---":
                                if not past_frontmatter:
                                    past_frontmatter = True
                                    continue
                                else:
                                    frontmatter_closed = True
                                    continue
                            if not frontmatter_closed:
                                continue
                            if stripped.startswith("# "):
                                continue
                            if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                                key_point = stripped[:100]
                                break
                except OSError:
                    pass
                insight_entries.append((title, key_point))
        except (sqlite3.Error, OSError) as _vi_exc:
            print(f"[obsidian-brain] vault index failed ({type(_vi_exc).__name__}: {_vi_exc}); "
                  "falling back to file scan", file=sys.stderr)
            _use_vault_index = False

    if not _use_vault_index:
        # Fallback to original file scan if vault index unavailable
        if insights_dir.is_dir():
            insight_files = sorted(
                [f for f in insights_dir.iterdir() if f.suffix == '.md'],
                reverse=True
            )
            project_insights: list[Path] = []
            for f in insight_files:
                meta = read_note_metadata(str(f))
                if not meta:
                    continue
                fm_project = meta.get('project', '')
                if fm_project.lower() != project.lower() and slugify(fm_project) != slugify(project):
                    continue
                project_insights.append(f)

            insight_count = len(project_insights)
            for f in project_insights[:20]:
                title = f.stem
                key_point = ""
                try:
                    with open(f, 'r', encoding='utf-8', errors='replace') as fh:
                        past_frontmatter = False
                        frontmatter_closed = False
                        for line_text in fh:
                            stripped = line_text.strip()
                            if stripped == '---':
                                if not past_frontmatter:
                                    past_frontmatter = True
                                    continue
                                else:
                                    frontmatter_closed = True
                                    continue
                            if not frontmatter_closed:
                                continue
                            if stripped.startswith('# '):
                                title = stripped[2:].strip()
                                continue
                            if stripped and not stripped.startswith('#') and not stripped.startswith('---'):
                                key_point = stripped[:100]
                                break
                except OSError:
                    pass
                insight_entries.append((title, key_point))

    # Trim insights to ~500 tokens (~375 words, ~1875 chars)
    insight_text_parts: list[str] = []
    total_chars = 0
    for title, key_point in insight_entries:
        entry = f"- **{title}**"
        if key_point:
            entry += f" — {key_point}"
        if total_chars + len(entry) > 1875:
            insight_text_parts.append(f"- **{title}**")
            total_chars += len(title) + 6
            if total_chars > 2200:
                break
            continue
        insight_text_parts.append(entry)
        total_chars += len(entry)

    insights_section = "\n".join(insight_text_parts) if insight_text_parts else "No curated insights yet for this project."

    # --- 4. Compose brief ---
    brief_parts: list[str] = []
    if hook_status_line:
        brief_parts.append(hook_status_line)
        brief_parts.append("")
    brief_parts.append(f"## Project Context: {project}")

    if most_recent_summary:
        brief_parts.append(f"\n### Last Session ({most_recent_date})")
        brief_parts.append(most_recent_summary)
        if most_recent_open_items:
            brief_parts.append(f"\n**Open Items / Next Steps:**\n{most_recent_open_items}")
    else:
        brief_parts.append(f"\nNo session history found for {project}.")

    if second_summary:
        brief_parts.append(f"\n### Previous Session ({second_date})")
        brief_parts.append(second_summary)

    brief_parts.append(f"\n### Curated Insights")
    brief_parts.append(insights_section)

    if history_rows:
        brief_parts.append("\n### Recent Session History")
        brief_parts.append("| # | Date | Duration | Title | Branch |")
        brief_parts.append("|---|------|----------|-------|--------|")
        brief_parts.extend(history_rows)

    brief = "\n".join(brief_parts)

    # --- 5. Open-item detection ---
    # Fix B (#264 task 3): an open item is only "contradicted" by a session
    # STRICTLY NEWER (by session date) than the item's own source session —
    # never by its own note, and never by an older one. Previously this only
    # matched every item against the single most-recent session's own
    # sections, so a stale item never got flagged even after a later session
    # reported it done (and, worse, could false-positive off its own note's
    # sections when that note happened to be the most recent one).
    candidates_output = "NO_CANDIDATES"
    if most_recent_path:
        try:
            _hooks_dir = os.path.dirname(os.path.abspath(__file__))
            if _hooks_dir not in sys.path:
                sys.path.insert(0, _hooks_dir)
            from open_item_dedup import collect_open_items

            # Evidence pool: (date, title, "## Summary" text) for every
            # session already enumerated above (session_files is the full
            # project-filtered, snapshot-excluded scan — a superset of what
            # collect_open_items() reads).
            # BH-003: session_files is the full project-filtered scan
            # (unbounded). Reading every note's body to build the evidence
            # pool doesn't scale. Cap the (expensive) body-read to the same
            # recent-N window collect_open_items() uses below — items only
            # ever come from that same recent window, and any session that
            # could strictly-newer-contradict an item is necessarily within
            # it too, so this cap cannot drop a needed newer session. The
            # cheap date-lookup dict (no file read) still covers ALL
            # project sessions so item_date resolution is unaffected.
            _session_evidence: list[tuple[str, str, str]] = []
            _session_date_by_path: dict[str, str] = {}
            for _fname, _fpath, _meta in session_files:
                _session_date_by_path[os.path.abspath(_fpath)] = _meta.get('date', '')
            for _fname, _fpath, _meta in session_files[:_OPEN_ITEM_EVIDENCE_WINDOW]:
                _date = _meta.get('date', '')
                if not _date:
                    continue
                try:
                    _content = Path(_fpath).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                _m = _summary_re.search(_content)
                if not _m:
                    continue
                _summary_text = _m.group(1).strip()
                if not _summary_text:
                    continue
                _title = f"Session: {_meta.get('project', project)}"
                _first_line = _summary_text.split('\n')[0].strip()
                if _first_line:
                    _title = _first_line
                _session_evidence.append((_date, _title, _summary_text))

            items = collect_open_items(vault_path, sessions_folder, project)
            if not items:
                candidates_output = "NO_ITEMS"
            else:
                flagged: list[dict] = []
                for fpath, line_num, item_text in items:
                    item_date = _session_date_by_path.get(os.path.abspath(fpath), '')
                    if not item_date:
                        # Can't establish "strictly newer" without a known
                        # source date — never flag.
                        continue
                    best: dict | None = None
                    for ev_date, ev_title, ev_summary in _session_evidence:
                        # Compare day-prefixes only (mirrors _safe_sort_key's
                        # `p.name[:10]` convention above) so a future note whose
                        # `date:` frontmatter carries a full datetime (not just
                        # YYYY-MM-DD) still compares correctly against a
                        # date-only value. Both sides are guaranteed non-empty
                        # strings here (see the `if not item_date` / `if not
                        # _date` guards above).
                        if ev_date[:10] <= item_date[:10]:
                            continue  # same-date or older session — never contradicts
                        matched = match_items_against_evidence(
                            ev_summary, [(fpath, line_num, item_text)]
                        )
                        for c in matched:
                            # BH-001: confidence >= 3 alone only means a
                            # distinctive token (e.g. a branch/file name) was
                            # co-mentioned — it does NOT mean the newer
                            # session reports the item DONE. A session that
                            # merely mentions the same branch/file (even to
                            # say it's still in progress) must not flag the
                            # item. Require completion language too.
                            if c.get("confidence", 0) < 3:
                                continue
                            if not c.get("has_completion_phrase"):
                                continue
                            if best is None or c["confidence"] > best["confidence"]:
                                c["contradicted_by"] = ev_date
                                c["contradicted_by_title"] = ev_title
                                best = c
                    if best is not None:
                        flagged.append(best)
                if flagged:
                    candidates_output = json.dumps(flagged)
        except Exception as exc:
            print(f"[obsidian-brain] open-item detection failed (non-fatal): {exc}", file=sys.stderr)

    # --- 6. Compose structured output ---

    # Snapshot summaries for the most-recent session (if any) — included
    # at auto-load depth: summaries only, not raw transcripts.
    manifest_snapshot_lines: list[str] = []
    if most_recent_snaps:
        manifest_snapshot_lines.append(f"snapshot_count: {len(most_recent_snaps)}")
        for sn in most_recent_snaps:
            manifest_snapshot_lines.append(
                f"snapshot: [{sn['hhmmss']}] ({sn['trigger']}) {sn['summary']}"
            )
            if sn["key_context"]:
                for bullet in sn["key_context"].splitlines():
                    if bullet.strip():
                        manifest_snapshot_lines.append(f"  {bullet.strip()}")

    manifest_lines = [
        f"full_session_title: {most_recent_title or '(none)'}",
        f"full_session_date: {most_recent_date or '(none)'}",
        f"full_session_path: {most_recent_path or '(none)'}",
        f"summary_session_title: {second_title or '(none)'}",
        f"summary_session_date: {second_date or '(none)'}",
        f"insight_count: {insight_count}",
    ]
    manifest_lines.extend(manifest_snapshot_lines)

    # Use unique delimiters that cannot appear in user-authored markdown content
    output_parts = [
        "<<<OB_CONTEXT_BRIEF>>>",
        brief,
        "",
        "<<<OB_LOAD_MANIFEST>>>",
        "\n".join(manifest_lines),
        "",
        "<<<OB_MOST_RECENT_SESSION_PATH>>>",
        most_recent_path,
        "",
        "<<<OB_OPEN_ITEM_CANDIDATES>>>",
        candidates_output,
    ]

    return "\n".join(output_parts)


def recurring_themes_section(db_path: str, project: str | None, top_n: int = 3) -> str:
    """Return a '## Recurring Themes' markdown block for /recall, or '' if none."""
    try:
        import themes
        rows = themes.get_top_themes_for_project(db_path, project, top_n=top_n)
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["## Recurring Themes"]
    for t in rows:
        summary = (t.get("summary") or "").strip()
        first = summary.split(". ")[0].rstrip(".") if summary else ""
        suffix = f" — {first}" if first else ""
        lines.append(f"- **{t['name']}**{suffix} ({t['note_count']} notes)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Filename / slug helpers
# ---------------------------------------------------------------------------


def slugify(text: str, max_len: int = 40) -> str:
    """Turn arbitrary text into a filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "session"


def make_filename(
    date_str: str, slug: str, session_id: str, suffix: str = ""
) -> str:
    """Build note filename: ``YYYY-MM-DD-<slug>-<hash>[suffix].md``

    Uses a 4-char SHA256 hash of the session_id.
    """
    h = hashlib.sha256(session_id.encode()).hexdigest()[:4]
    return f"{date_str}-{slug}-{h}{suffix}.md"


def should_skip_session(
    user_messages: list[str],
    duration: float,
    min_messages: int = 3,
    min_duration: float = 2.0,
) -> bool:
    """Return True if the session is below logging thresholds.

    Skips if user message count < min_messages.
    Skips if duration is known (> 0) and below min_duration.
    """
    if len(user_messages) < min_messages:
        return True
    if duration > 0 and duration < min_duration:
        return True
    return False


def extract_tool_uses(messages: list[dict]) -> list[dict]:
    """Extract tool usage details from transcript for the raw fallback note.

    Returns a list of dicts: [{"name": "Edit", "detail": "file.py:10-20"}, ...]

    Handles both the canonical CC JSONL shape (entry['message']['content'])
    and the flat fallback shape (entry['content']) via _entry_content.
    """
    tool_uses: list[dict] = []
    for entry in messages:
        content = _entry_content(entry)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {}) if isinstance(block.get("input"), dict) else {}
            detail = ""
            if name in ("Edit", "Write", "MultiEdit"):
                fp = inp.get("file_path", "")
                detail = f"`{fp}`" if fp else ""
            elif name == "Bash":
                cmd = inp.get("command", "")[:120]
                detail = f"`{cmd}`" if cmd else ""
            elif name == "Read":
                fp = inp.get("file_path", "")
                detail = f"`{fp}`" if fp else ""
            elif name in ("Grep", "Glob"):
                pattern = inp.get("pattern", "")
                detail = f'pattern="{pattern}"' if pattern else ""
            elif name == "WebFetch":
                url = inp.get("url", "")[:80]
                detail = url if url else ""
            elif name == "WebSearch":
                query = inp.get("query", "")[:80]
                detail = f'"{query}"' if query else ""
            elif name == "Agent":
                desc = inp.get("description", "")[:80]
                detail = desc if desc else ""
            else:
                detail = ""

            if name:
                tool_uses.append({"name": name, "detail": detail})
    return tool_uses


def get_project_name(cwd: str) -> str:
    """Return the basename of the working directory as the project name.

    Used for CC's path-encoded bootstrap/JSONL lookups; for vault frontmatter
    use ``canonical_project_name()`` instead so worktrees of the same repo
    share one logical project value.
    """
    return Path(cwd).name if cwd else "unknown"


def find_transcript_jsonl(session_id: str) -> Path | None:
    """Locate the original Claude Code transcript JSONL by session_id.

    Returns the Path if found, None otherwise. Uses find(1) so it is
    agnostic to project-path encoding (hyphens vs underscores).
    """
    if not session_id or session_id == "unknown":
        return None
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return None
    # Reject any session_id containing glob metacharacters, path separators,
    # or whitespace. `find -name` matches basenames and treats its argument
    # as a glob, so separators would never match and metacharacters would
    # match the wrong file. UUIDs never contain any of these — an occurrence
    # indicates garbage input rather than a legitimate lookup.
    if any(c in session_id for c in "*?[]/\\ \t\n\r"):
        return None
    target = f"{session_id}.jsonl"
    # Primary path: external `find` with -print -quit (fast on large trees).
    # Suppress stderr so permission-denied or other noise on unrelated
    # subtrees does not poison the exit code. Use stdout whenever it's
    # non-empty regardless of returncode — `find` commonly returns non-zero
    # after encountering a restricted directory even when it also printed a
    # legitimate match from a sibling directory.
    try:
        result = subprocess.run(
            ["find", str(projects_dir), "-name", target, "-type", "f", "-print", "-quit"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5,
        )
        if result.stdout.strip():
            first = result.stdout.strip().split("\n")[0]
            if first:
                real_first = os.path.realpath(first)
                if not real_first.startswith(str(projects_dir.resolve()) + os.sep):
                    return None
                return Path(real_first)
            return None
    except subprocess.TimeoutExpired:
        # If `find` already timed out on this tree, a pure-Python rglob
        # will almost certainly be slower — the tree is too large. Treat
        # timeout as a hard failure so /recall falls back to the raw note
        # rather than hanging on a worse scan.
        return None
    except FileNotFoundError:
        # `find` not on PATH (sandboxed/minimal container). Fall through
        # to the pure-Python rglob fallback below.
        pass
    except OSError:
        pass

    # Fallback: pure-Python rglob. Only reached when `find` is unavailable,
    # never when it timed out. Dependency-free so /recall still works in
    # sandboxed environments that don't ship with `find`.
    try:
        for path in projects_dir.rglob(target):
            if path.is_file():
                real = os.path.realpath(str(path))
                if not real.startswith(str(projects_dir.resolve()) + os.sep):
                    continue  # skip symlinks escaping projects_dir
                return Path(real)
    except OSError:
        return None
    return None


def parse_full_transcript(jsonl_path: Path, max_bytes: int = 5_000_000) -> dict:
    """Parse a Claude Code transcript JSONL WITHOUT the raw-note caps.

    Delegates to the canonical extract_user_messages / extract_assistant_messages /
    extract_tool_uses helpers so tool-use and error detection stay in parity
    with the SessionEnd write path. Never fails silently on data loss —
    the returned `warnings` list is the caller's signal for every hiccup.

    Applies a hard byte budget. When the transcript exceeds max_bytes, it
    is sliced into head + tail halves of the budget; partial lines at the
    slice boundaries are detected (head not ending on \\n, tail not starting
    on \\n) and dropped explicitly with a warning.

    Returns a dict with keys:
        - user_msgs: list[str]
        - assistant_msgs: list[str]
        - tool_uses: list[dict]   (same shape as extract_tool_uses output)
        - files_touched: list[str]
        - errors: list[str]
        - truncated: bool          (True if byte budget kicked in)
        - warnings: list[str]      (visible issues the caller should surface)
        - raw_note_max_turns: int  (the RAW_NOTE_MAX_TURNS constant, for caller reference)
        - raw_note_would_truncate: bool  (True iff build_raw_fallback would hit its write cap)
    """
    warnings: list[str] = []
    bad_lines = 0
    unknown_block_types: set[str] = set()
    truncated = False

    def _empty_result(warning: str) -> dict:
        return {
            "user_msgs": [], "assistant_msgs": [], "tool_uses": [],
            "files_touched": [], "errors": [], "truncated": False,
            "warnings": [warning],
            # Always include the shared cap + derived signal so /recall's
            # decision logic can key off one consistent schema regardless
            # of which branch ran. Empty transcripts cannot have truncated.
            "raw_note_max_turns": RAW_NOTE_MAX_TURNS,
            "raw_note_would_truncate": False,
        }

    try:
        size = jsonl_path.stat().st_size
    except OSError as exc:
        return _empty_result(f"Could not stat transcript file: {exc}")

    if size == 0:
        return _empty_result("Transcript file is empty (0 bytes).")

    if size <= max_bytes:
        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            return _empty_result(f"Could not read transcript file: {exc}")
    else:
        # Slice head + tail of the byte budget. Boundary-safe: only drop a
        # line when the slice actually cut it mid-record. If head_bytes
        # ends on a newline, its last line is complete — keep it.
        half = max_bytes // 2
        # Guarantee head and tail do not overlap: tail starts at max(half, size - half).
        tail_offset = max(half, size - half)
        # Peek at the byte immediately before tail_offset to determine
        # whether the tail slice started exactly at the beginning of a
        # record. If that preceding byte is a newline, tail_offset lies at
        # a record boundary and the first tail line is complete; otherwise
        # the record was cut mid-line and the first tail line is partial.
        # (Checking tail_text[0] == "\n" is wrong — a clean record boundary
        # means the tail text starts with the first char of a record, not
        # a newline.)
        tail_starts_cleanly = False
        try:
            with open(jsonl_path, "rb") as fh:
                head_bytes = fh.read(half)
                if tail_offset > 0:
                    fh.seek(tail_offset - 1)
                    prev_byte = fh.read(1)
                    tail_starts_cleanly = prev_byte in (b"\n", b"\r")
                else:
                    tail_starts_cleanly = True
                fh.seek(tail_offset)
                tail_bytes = fh.read()
        except OSError as exc:
            return _empty_result(f"Could not slice transcript file: {exc}")

        head_text = head_bytes.decode("utf-8", errors="replace")
        tail_text = tail_bytes.decode("utf-8", errors="replace")
        head_lines = head_text.splitlines()
        tail_lines = tail_text.splitlines()
        partial_dropped = 0
        # Drop the last head line only if head_bytes did not end on a newline
        # (meaning the record is genuinely cut mid-line).
        if head_lines and not head_text.endswith(("\n", "\r")):
            head_lines.pop()
            partial_dropped += 1
        # Drop the first tail line only if tail_offset did NOT land at a
        # clean record boundary (byte before tail_offset is not a newline).
        if tail_lines and not tail_starts_cleanly:
            tail_lines.pop(0)
            partial_dropped += 1
        lines = head_lines + tail_lines
        truncated = True
        if partial_dropped:
            warnings.append(
                f"Transcript byte budget exceeded ({size} > {max_bytes} bytes) — "
                f"middle section sliced, {partial_dropped} partial JSONL lines dropped at slice boundaries."
            )
        else:
            warnings.append(
                f"Transcript byte budget exceeded ({size} > {max_bytes} bytes) — "
                f"middle section sliced (both slice boundaries fell on record boundaries cleanly)."
            )

    # First pass: collect parsed JSONL records into an entries list. The
    # downstream extract_* helpers accept both the canonical CC shape
    # (top-level `type` plus nested `message.content`) and the flat
    # fallback shape, so no explicit normalization is needed here.
    entries: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if not isinstance(obj, dict):
            bad_lines += 1
            continue
        entries.append(obj)
        # Collect any unexpected content block types for user-visible warnings.
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype and btype not in (
                        "text", "tool_use", "tool_result", "thinking",
                        "image", "redacted_thinking",
                    ):
                        unknown_block_types.add(btype)

    # Delegate to canonical helpers so behavior stays in parity with the
    # SessionEnd write path.
    user_msgs = extract_user_messages(entries)
    assistant_msgs = extract_assistant_messages(entries)
    tool_uses = extract_tool_uses(entries)

    # Files touched + errors: delegate to the shared helpers used by
    # extract_session_metadata so both code paths stay in lockstep.
    # No caps applied here — caps belong to the display layer, not
    # re-parse, so the full summary can see everything the transcript
    # actually contains.
    files_seen = _extract_files_touched(entries)
    errors = _extract_errors(entries)

    # Note: we do not inject a "[... middle truncated ...]" marker into
    # user_msgs. At this point it would land at the very end of the list
    # (after the tail slice), not at the actual head/tail boundary, which
    # would be misleading. The slice is already surfaced via `truncated`
    # and the `warnings` list, which is what the caller uses for display.

    if bad_lines:
        warnings.append(f"{bad_lines} malformed JSONL line(s) skipped while re-parsing transcript.")
    if unknown_block_types:
        warnings.append(
            f"Unknown content block types encountered (data may be incomplete): {', '.join(sorted(unknown_block_types))}"
        )

    # Simulate the exact write loop in build_raw_fallback to determine
    # whether the raw fallback would have truncated. This is the only
    # fully deterministic signal: the cap applies to lines actually
    # written, and filtered system-noise user messages do not increment
    # the counter. Simple parsed_total > cap comparison can false-positive
    # when noise filtering gives headroom.
    raw_note_would_truncate = _would_raw_fallback_truncate(user_msgs, assistant_msgs)

    return {
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "tool_uses": tool_uses,
        "files_touched": files_seen,
        "errors": errors,
        "truncated": truncated,
        "warnings": warnings,
        # The raw-note cap constant, preserved for backward compatibility
        # with callers that still want to inspect it.
        "raw_note_max_turns": RAW_NOTE_MAX_TURNS,
        # Definitive signal: true iff build_raw_fallback would have hit
        # its write cap before consuming all user+assistant messages.
        # This is what /recall should branch on.
        "raw_note_would_truncate": raw_note_would_truncate,
    }


def _would_raw_fallback_truncate(
    user_msgs: list[str], assistant_msgs: list[str]
) -> bool:
    """Return True iff build_raw_fallback's write loop would hit its
    RAW_NOTE_MAX_TURNS cap before consuming all user+assistant messages.

    Mirrors the exact loop in build_raw_fallback so the signal is
    deterministic: filtered system-noise user messages do not count
    toward the cap, so a transcript with more total messages than the
    cap may still fit entirely when enough noise is filtered out.
    """
    max_turns = RAW_NOTE_MAX_TURNS
    u_idx, a_idx = 0, 0
    turn = 0
    while turn < max_turns and (
        u_idx < len(user_msgs)
        or (assistant_msgs and a_idx < len(assistant_msgs))
    ):
        if u_idx < len(user_msgs):
            snippet = user_msgs[u_idx][:1200].replace("\n", " ")
            # Same filter as build_raw_fallback: skip system noise.
            if not (
                snippet.startswith("<task-notification>")
                or snippet.startswith("Base directory for this skill:")
                or snippet.startswith("<local-command")
            ):
                turn += 1
            u_idx += 1
        if assistant_msgs and a_idx < len(assistant_msgs):
            a_idx += 1
            turn += 1
    # Truncated iff the loop bailed on the cap with inputs remaining.
    return u_idx < len(user_msgs) or a_idx < len(assistant_msgs)


def build_raw_fallback(
    user_msgs: list[str],
    metadata: dict,
    assistant_msgs: list[str] | None = None,
    tool_uses: list[dict] | None = None,
    config: dict | None = None,
) -> str:
    """Build a detailed note body without AI summarization -- raw data extraction.

    Includes user messages, assistant messages, tool usage, files touched,
    and errors for maximum context when /recall does deferred summarization.
    """
    sections: list[str] = []

    project = metadata.get("project", "unknown")
    duration = metadata.get("duration_minutes", 0)

    sections.append("## Summary")
    sections.append(
        f"Session in **{project}** ({duration} min). "
        "AI summary unavailable \u2014 raw extraction below.\n"
    )

    sections.append("## Key Decisions")
    sections.append("_Not extracted (AI summary unavailable)._\n")

    sections.append("## Changes Made")
    files = metadata.get("files_touched", [])
    if files:
        for f in files[:60]:
            sections.append(f"- `{f}`")
    else:
        sections.append("None detected.")
    sections.append("")

    # Tool usage details (commands run, files edited) — scrubbed for secrets
    if tool_uses:
        sections.append("## Tool Usage")
        for tu in tool_uses[:80]:
            name = tu.get("name", "")
            detail = escape_wikilinks(scrub_secrets(tu.get("detail", "")))
            if name and detail:
                sections.append(f"- **{name}**: {detail}")
            elif name:
                sections.append(f"- **{name}**")
        sections.append("")

    sections.append("## Errors Encountered")
    errors = metadata.get("errors", [])
    if errors:
        for e in errors[:30]:
            sections.append(f"- {e}")
    else:
        sections.append("None.")
    sections.append("")

    sections.append("## Open Questions / Next Steps")
    sections.append("_Not extracted (AI summary unavailable)._\n")

    # Interleaved conversation for /recall to summarize (controlled by config toggle)
    if (config or {}).get("log_raw_messages", True):
        sections.append("## Conversation (raw)")
        max_turns = RAW_NOTE_MAX_TURNS
        u_idx, a_idx = 0, 0
        turn = 0
        while turn < max_turns and (u_idx < len(user_msgs) or (assistant_msgs and a_idx < len(assistant_msgs))):
            if u_idx < len(user_msgs):
                snippet = escape_wikilinks(scrub_secrets(user_msgs[u_idx][:1200].replace("\n", " ")))
                # Skip system noise (task notifications, command loading, etc.)
                if not snippet.startswith("<task-notification>") and not snippet.startswith("Base directory for this skill:") and not snippet.startswith("<local-command"):
                    sections.append(f"**User:** {snippet}")
                    turn += 1
                u_idx += 1
            if assistant_msgs and a_idx < len(assistant_msgs):
                snippet = escape_wikilinks(scrub_secrets(assistant_msgs[a_idx][:1200].replace("\n", " ")))
                sections.append(f"**Assistant:** {snippet}")
                a_idx += 1
                turn += 1
        sections.append("")

    return "\n".join(sections)


def is_resumed_session(
    vault_path: str, sessions_folder: str, session_id: str,
    cwd: str | None = None,
) -> bool:
    """Return True iff a session-type note matching this session_id's hash
    exists for the current project. Snapshot-type notes are intentionally
    ignored (#101 Fix C), and cross-project hash collisions are skipped via
    project_path filtering. Subsumes #86.

    ``cwd`` overrides ``os.getcwd()`` for the project_path filter. SessionEnd
    callers should pass ``hook_input["cwd"]`` (the authoritative project
    path from Claude Code) so that hook processes that have chdir'd
    elsewhere still classify the session against the right project.
    Falls back to ``_safe_getcwd()`` when ``cwd`` is None.
    """
    sessions_dir = Path(vault_path) / sessions_folder
    if not sessions_dir.exists():
        return False
    h = hashlib.sha256(session_id.encode()).hexdigest()[:4]
    effective_cwd = cwd if cwd is not None else _safe_getcwd()
    resolved, collisions = _resolve_session_note_by_hash(
        sessions_dir, h, cwd=effective_cwd
    )
    if collisions:
        # Caller contract is bool-only; warn so the operator can investigate.
        print(
            f"[obsidian-brain] WARN: is_resumed_session: hash {h} collides "
            f"across {len(collisions) + (1 if resolved else 0)} session note(s)",
            file=sys.stderr,
        )
    return resolved is not None


def upgrade_note_with_summary(
    note_path: str,
    summary_text: str,
    vault_path: str,
    sessions_folder: str,
    project: str,
    source: str = "sub-agent fallback",
    warnings: list[str] | None = None,
) -> str:
    """Apply a pre-generated summary to a raw session note.

    Handles the pipeline finish: read raw note, validate summary has
    ## Summary, rebuild note (frontmatter with status: summarized, title,
    summary sections, audit trail), run dedup, atomic write.

    Returns a one-line status string.

    Return contract: success strings begin with ``"Upgraded "``; all other
    return values (including ``"Failed: ..."``, empty, or any unexpected
    prefix) indicate failure. Callers routing on return value — notably
    ``skills/recall/SKILL.md`` Step 2 Phase 1 — MUST check the ``"Upgraded "``
    prefix as the positive path. Adding any new return prefix to this
    function requires an audit of routing call sites.
    """
    if warnings is None:
        warnings = []

    if not re.search(r"^## Summary\s*$", summary_text, re.MULTILINE):
        return f"Failed: malformed summary (no ## Summary section) from {source} for {os.path.basename(note_path)}"

    # Read the raw note
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            raw_lines = f.readlines()
    except OSError as exc:
        return f"Failed: cannot read {os.path.basename(note_path)}: {exc}"

    # Build upgraded note: original frontmatter + new summary + original audit trail
    new_lines: list[str] = []

    # Copy frontmatter, flipping status
    past_first_marker = False
    frontmatter_end = 0
    for i, line in enumerate(raw_lines):
        if line.strip() == '---':
            if not past_first_marker:
                past_first_marker = True
                new_lines.append(line)
                continue
            else:
                # End of frontmatter
                new_lines.append(line)
                frontmatter_end = i + 1
                break
        if past_first_marker:
            if line.strip().startswith('status:'):
                new_lines.append(re.sub(r'^(\s*status:\s*).*', r'\1summarized', line) + '\n' if not line.endswith('\n') else re.sub(r'^(\s*status:\s*).*', r'\1summarized', line))
            else:
                new_lines.append(line)

    if frontmatter_end == 0:
        return f"Failed: malformed frontmatter in {os.path.basename(note_path)} (missing closing ---)"

    # Add title from original
    title_found = False
    for line in raw_lines[frontmatter_end:]:
        if line.strip().startswith('# '):
            new_lines.append('\n')
            new_lines.append(line)
            title_found = True
            break
    if not title_found:
        new_lines.append('\n# Untitled Session\n')

    # Add warnings if any
    if warnings:
        new_lines.append('\n## ⚠️ Transcript re-parse warnings\n')
        for w in warnings:
            new_lines.append(f'- {w}\n')

    # Add summary sections
    new_lines.append('\n')
    new_lines.append(summary_text + '\n')

    # Add source note
    new_lines.append(f'\n_(Summary source: {source})_\n')

    # Preserve original audit trail sections (skip frontmatter).
    # Only exclude ## Changes Made / ## Errors Encountered if summary_text
    # actually contains them — otherwise preserve the raw audit data.
    audit_sections = [
        '## Tool Usage', '## Conversation (raw)',
        '## Session Metadata', '## Files Touched',
    ]
    if '## Changes Made' not in summary_text:
        audit_sections.append('## Changes Made')
    if '## Errors Encountered' not in summary_text:
        audit_sections.append('## Errors Encountered')

    in_audit = False
    for line in raw_lines[frontmatter_end:]:
        stripped = line.strip()
        if any(stripped.startswith(s) for s in audit_sections):
            in_audit = True
        elif stripped.startswith('## '):
            in_audit = False
        if in_audit:
            new_lines.append(line)

    # Extract the summary body signature BEFORE writing so we can fail the
    # upgrade with a clear "malformed summary" error rather than silently
    # degrading post-write verification. The signature is the first non-blank,
    # non-heading line of the Summary section — used to prove on re-read that
    # the body actually landed, not just the status flip.
    #
    # Heading detection follows ATX-heading rules strictly: `#{1,6}` must be
    # followed by whitespace or end-of-line. A line like `#1234 issue ref` or
    # `#hashtag note` is legitimate content, not a heading, and must not be
    # skipped — otherwise it could produce a false "empty or heading-only
    # Summary body" failure when it is the first real content line.
    #
    # The level-2 break uses `##(?:\s|$)` (any whitespace after, or EOL) so
    # a tab-separated or double-space-separated next section like
    # `##\tKey Decisions` still terminates the Summary block cleanly.
    _atx_heading_re = re.compile(r'^#{1,6}(?:\s|$)')
    _h2_re = re.compile(r'^##(?:\s|$)')
    summary_signature = None
    in_summary = False
    for line in summary_text.split('\n'):
        if line.strip() == '## Summary':
            in_summary = True
            continue
        if in_summary:
            stripped = line.strip()
            if _h2_re.match(stripped):
                break  # next top-level section — Summary body was empty
            if _atx_heading_re.match(stripped):
                continue  # sub-heading inside Summary — skip but keep looking
            if stripped:
                summary_signature = stripped
                break

    if summary_signature is None:
        return f"Failed: malformed summary (empty or heading-only Summary body) from {source} for {os.path.basename(note_path)}"

    importance = parse_importance(summary_text)

    # Atomic write with fsync + post-write verification.
    # Guarantees the summary actually landed on disk before returning success.
    # `or "."` handles the case where note_path is a bare filename (no
    # directory component), which would otherwise produce `dir=""` and
    # crash tempfile.mkstemp on every platform.
    note_dir = os.path.dirname(note_path) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='.ob-upgrade-', suffix='.md.tmp', dir=note_dir)
    except OSError as exc:
        return f"Failed: cannot create temp file in {note_dir}: {exc}"
    try:
        try:
            orig_mode = os.stat(note_path).st_mode
        except OSError:
            orig_mode = 0o600
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, orig_mode)
        os.replace(tmp_path, note_path)
        # fsync the containing directory so the rename itself is durable
        # across a crash, not just the file contents.
        try:
            dir_fd = os.open(note_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Directory fsync is best-effort on filesystems that don't
            # support it (e.g. some network mounts). The in-process
            # verification below is the real guarantee for non-crash
            # failure modes.
            pass
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_exc:
            print(
                f"[obsidian-brain] failed to clean up temp file {tmp_path}: {cleanup_exc}",
                file=sys.stderr,
            )
        return f"Failed: atomic write error for {os.path.basename(note_path)}: {exc}"

    # Post-write verification: re-read the target file and confirm the
    # summary actually landed. Protects against silent write-loss from
    # concurrent writers, filesystem races, or phantom "success" returns.
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            verify_content = f.read()
    except OSError as exc:
        return f"Failed: post-write read verification failed for {os.path.basename(note_path)}: {exc}"

    # Scope the status check to the YAML frontmatter block so a note body
    # that happens to mention "status: summarized" (in a conversation
    # excerpt, a code block, or this very PR's diff) cannot false-positive
    # the check. Anchor to the start of the file (allowing an optional
    # UTF-8 BOM) so a Markdown horizontal rule `---` in the body cannot
    # be mistaken for the opening frontmatter delimiter.
    fm_match = re.match(
        r'\ufeff?---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)',
        verify_content,
        re.DOTALL,
    )
    if fm_match is None:
        return f"Failed: post-write verification — YAML frontmatter not found at start of {os.path.basename(note_path)}"
    frontmatter_block = fm_match.group(1)
    if not re.search(r'^\s*status:\s*summarized\s*$', frontmatter_block, re.MULTILINE):
        return f"Failed: post-write verification — status not flipped to summarized in {os.path.basename(note_path)}"

    # Scope the signature check to the ## Summary section specifically.
    # Checking the whole file would false-positive if the signature text
    # happens to appear in a preserved audit trail (Conversation raw,
    # Tool Usage), even though the actual Summary body was clobbered.
    # Boundary uses `##(?:\s|$)` to be consistent with ATX-heading rules —
    # tab-separated or multi-space-separated next sections still terminate
    # the Summary block extraction cleanly.
    summary_match = re.search(
        r'^## Summary\s*\n(.*?)(?=^##(?:\s|$)|\Z)',
        verify_content,
        re.MULTILINE | re.DOTALL,
    )
    if summary_match is None:
        return f"Failed: post-write verification — ## Summary section not found in {os.path.basename(note_path)}"
    summary_block = summary_match.group(1)
    # Compare at line granularity — a substring match could false-positive
    # if the signature is a substring of some other line in the Summary
    # (e.g. the signature is "Fixed the bug." and an adjacent line says
    # "Before: Fixed the bug. After: also broken."). The signature must
    # appear as its own stripped line in the Summary block on disk.
    summary_block_lines = {line.strip() for line in summary_block.split('\n')}
    if summary_signature not in summary_block_lines:
        return f"Failed: post-write verification — summary body missing from {os.path.basename(note_path)}"

    # Connection A: upsert + importance in one BEGIN IMMEDIATE transaction.
    # Replaces the separate importance-UPDATE connection and the index_note
    # call (which opened its own connection). Parsing is done in-process via
    # _parse_note so no extra I/O round-trip is needed; os.stat is cheap.
    # Best-effort — a failure here MUST NEVER mask the successful upgrade.
    #
    # _index_ok tracks whether Connection A committed successfully.  The
    # surprise-body read is intentionally OUTSIDE this try so that a read
    # error (file removed in the commit→read race, decode error) does NOT:
    #   (a) trigger a spurious rollback of an already-committed transaction, or
    #   (b) log a misleading "index+importance write-back failed" message, or
    #   (c) skip theme assignment entirely (the old coupled-read bug).
    _index_ok: bool = False
    try:
        if _vault_index is not None:
            _db = _vault_index._default_db_path()
            if os.path.isfile(_db):
                _parsed = _vault_index._parse_note(note_path)
                if _parsed is not None and os.path.isfile(note_path):
                    _st = os.stat(note_path)
                    _conn = _vault_index._connect(_db)
                    try:
                        _conn.execute("BEGIN IMMEDIATE")
                        _vault_index._upsert_note(
                            _conn, note_path, _parsed, _st.st_mtime, _st.st_size
                        )
                        _conn.execute(
                            "UPDATE notes SET importance = ? WHERE path = ?",
                            (importance, note_path),
                        )
                        _conn.commit()
                        _index_ok = True
                    except Exception as _exc:
                        try:
                            _conn.rollback()
                        except Exception:
                            pass
                        print(f"[obsidian-brain] index+importance write-back failed for "
                              f"{os.path.basename(note_path)}: "
                              f"{type(_exc).__name__}: {_exc}", file=sys.stderr)
                    finally:
                        _conn.close()
                else:
                    _skip_reason = "parse failed" if _parsed is None else "note file gone"
                    print(f"[obsidian-brain] index+importance skipped ({_skip_reason}) for "
                          f"{os.path.basename(note_path)}", file=sys.stderr)
    except Exception as exc:
        print(f"[obsidian-brain] index+importance write-back failed for "
              f"{os.path.basename(note_path)}: {exc}", file=sys.stderr)

    # Read the full raw file (frontmatter + body) for surprise scoring.
    # Done AFTER Connection A, in its own try, so a read error:
    #   - never triggers a spurious rollback of the committed transaction
    #   - never logs a misleading "index+importance write-back failed" message
    #   - never skips theme assignment (Connection B gates on _index_ok, not
    #     on body-read success — assign_to_theme handles note_text=None by
    #     writing the default surprise of 0.0 and still assigning the theme)
    # Friston-data continuity: byte-exact parity with legacy
    # open(note_path, "r", encoding="utf-8").read() input to detect_surprise.
    _note_body_for_surprise: str | None = None
    if _index_ok:
        try:
            with open(note_path, "r", encoding="utf-8") as _fh:
                _note_body_for_surprise = _fh.read()
        except OSError as _rexc:
            print(f"[obsidian-brain] surprise-body read failed for "
                  f"{os.path.basename(note_path)}: "
                  f"{type(_rexc).__name__}: {_rexc}", file=sys.stderr)
            _note_body_for_surprise = None

    # Connection B: theme assignment + surprise in one transaction.
    # assign_to_theme opens a single BEGIN IMMEDIATE internally and now also
    # computes + writes surprise when note_text is supplied — eliminating the
    # separate fourth connection from the old pipeline.
    # Gates on _index_ok (not _note_body_for_surprise) so a body-read failure
    # still allows theme assignment (with default surprise=0.0).
    # Best-effort — a failure here MUST NEVER mask the successful upgrade.
    try:
        if _vault_index is not None:
            _db = _vault_index._default_db_path()
            if os.path.isfile(_db) and _index_ok:
                try:
                    _assignment = _vault_index.assign_to_theme(
                        _db, note_path, project=project,
                        note_text=_note_body_for_surprise,
                    )
                except Exception as _exc:
                    print(f"[obsidian-brain] assign_to_theme failed for "
                          f"{os.path.basename(note_path)}: {_exc}",
                          file=sys.stderr)
    except Exception as _exc:
        print(f"[obsidian-brain] theme pipeline unexpected error "
              f"for {os.path.basename(note_path)}: {_exc}", file=sys.stderr)

    # Run dedup pass (non-fatal — note is already upgraded)
    removed = []
    dedup_failed = False
    try:
        _hooks_dir = os.path.dirname(os.path.abspath(__file__))
        if _hooks_dir not in sys.path:
            sys.path.insert(0, _hooks_dir)
        from open_item_dedup import dedup_note_open_items
        removed = dedup_note_open_items(vault_path, sessions_folder, project, note_path)
    except (ImportError, OSError) as exc:
        dedup_failed = True
        print(f"[obsidian-brain] dedup failed (non-fatal, note already upgraded): {exc}", file=sys.stderr)
    except Exception as exc:
        dedup_failed = True
        print(f"[obsidian-brain] dedup unexpected error: {exc}", file=sys.stderr)

    # Invalidate metadata cache for this note (status changed from auto-logged to summarized)
    sid = _get_session_id_fast()
    cache_set(sid, f"metadata:{os.path.realpath(note_path)}", None)

    # Build status
    status = f"Upgraded {os.path.basename(note_path)} (source: {source})"
    if removed:
        status += f", deduped {len(removed)} item(s)"
    if dedup_failed:
        status += ", dedup failed (see stderr)"
    if warnings:
        status += f", {len(warnings)} warning(s)"
    return status


def prepare_summary_input(note_path: str) -> str:
    """Check if raw note would truncate; if so, extract JSONL to temp file.

    Called by /recall Step 3 before spawning sub-agents. Determines
    whether the sub-agent should read the raw note directly or a
    pre-extracted JSONL temp file with sampled messages.

    Returns one of:
      RAW_OK:<note_path>
      JSONL_PREPPED:<temp_file_path>:<note_path>
      NO_CONTENT:<note_path>
    """
    # Read only frontmatter (first 20 lines) — avoids loading large raw notes into memory
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            raw_lines = [f.readline() for _ in range(20)]
    except OSError as exc:
        print(f"[obsidian-brain] cannot read {os.path.basename(note_path)}: {exc}", file=sys.stderr)
        return f"NO_CONTENT:{note_path}"

    session_id = None
    project = "unknown"
    git_branch = "unknown"
    duration_minutes = 0.0
    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith('session_id:'):
            session_id = stripped.split(':', 1)[1].strip().strip('"').strip("'")
        elif stripped.startswith('project:'):
            project = stripped.split(':', 1)[1].strip().strip('"')
        elif stripped.startswith('git_branch:'):
            git_branch = stripped.split(':', 1)[1].strip().strip('"')
        elif stripped.startswith('duration_minutes:'):
            try:
                duration_minutes = float(stripped.split(':', 1)[1].strip())
            except ValueError:
                pass

    if not session_id:
        print(f"[obsidian-brain] no session_id in {os.path.basename(note_path)}", file=sys.stderr)
        return f"NO_CONTENT:{note_path}"

    # Find and parse JSONL transcript
    try:
        jsonl_path = find_transcript_jsonl(session_id)
        if not jsonl_path:
            return f"RAW_OK:{note_path}"

        parsed = parse_full_transcript(jsonl_path)

        # Surface transcript warnings (Issue #1: never discard these)
        for w in parsed.get("warnings", []):
            print(f"[obsidian-brain] transcript warning for {os.path.basename(note_path)}: {w}", file=sys.stderr)

        if not parsed.get("raw_note_would_truncate", False):
            return f"RAW_OK:{note_path}"

        # Raw note would truncate — extract JSONL content to temp file
        user_msgs = parsed.get("user_msgs", [])
        assistant_msgs = parsed.get("assistant_msgs", [])
        if not user_msgs and not assistant_msgs:
            return f"RAW_OK:{note_path}"

        # Sample messages using same logic as generate_summary()
        if len(user_msgs) > 20:
            sampled_user = user_msgs[:10] + ["[... middle messages omitted ...]"] + user_msgs[-10:]
        else:
            sampled_user = user_msgs
        if len(assistant_msgs) > 20:
            sampled_asst = assistant_msgs[:10] + ["[... middle messages omitted ...]"] + assistant_msgs[-10:]
        else:
            sampled_asst = assistant_msgs

        user_sample = "\n---\n".join(sampled_user)[:12000]
        assistant_sample = "\n---\n".join(sampled_asst)[:12000]

        files_touched = parsed.get("files_touched", [])[:15]
        files_str = ", ".join(files_touched) if files_touched else "none detected"

        content = f"""# Session Summary Input (extracted from JSONL transcript)

**Project:** {project}
**Branch:** {git_branch}
**Duration:** {duration_minutes} minutes
**Files touched:** {files_str}

## Conversation

### User Messages (sampled)
{user_sample}

### Assistant Messages (sampled)
{assistant_sample}
"""

        # Write to temp file (use full session_id to avoid collisions)
        temp_path = os.path.join(_ensure_secure_dir(), f"prep-{session_id}.md")
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                f.write(content)
        except OSError as exc:
            print(f"[obsidian-brain] cannot write temp file {temp_path}, falling back to truncated raw note: {exc}", file=sys.stderr)
            return f"RAW_OK:{note_path}"

        return f"JSONL_PREPPED:{temp_path}:{note_path}"

    except Exception as exc:
        print(f"[obsidian-brain] unexpected error in JSONL prep for {os.path.basename(note_path)}: {exc}", file=sys.stderr)
        return f"RAW_OK:{note_path}"


def _prepare_note_for_summary(
    note_path: str,
    vault_path: str,
    sessions_folder: str,
    project: str,
) -> dict:
    """Prepare a session/snapshot note for AI summarization (the pre-model stage).

    Reads the raw note, extracts session_id, parses the JSONL transcript (with
    raw-note fallback), builds summarizer metadata, determines note_type, and
    (for sessions) computes the snapshot cohesion preamble into metadata.

    Returns a dict. On failure: {"ok": False, "status": <one-line "Failed: ..." string>,
    "fallback_reason": <classifier>}. On success: {"ok": True, "raw_lines": [...],
    "session_id": str, "user_msgs": [...], "assistant_msgs": [...], "metadata": {...},
    "note_type": str, "source": str, "warnings": [...]}.

    Extracted from upgrade_unsummarized_note (#166) so the multi-note batch path
    can reuse identical preparation. Pure: no model calls, no writes.
    """
    # Read the raw note
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            raw_lines = f.readlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "status": f"Failed: cannot read {os.path.basename(note_path)}: {exc}",
            "fallback_reason": "unreadable_note",
        }

    # Extract session_id from frontmatter
    session_id = None
    for line in raw_lines[:20]:
        stripped = line.strip()
        if stripped.startswith('session_id:'):
            session_id = stripped.split(':', 1)[1].strip().strip('"').strip("'")
            break
    if not session_id:
        return {
            "ok": False,
            "status": f"Failed: no session_id in frontmatter of {os.path.basename(note_path)}",
            "fallback_reason": "no_session_id",
        }

    # Find and parse the JSONL transcript
    jsonl_path = find_transcript_jsonl(session_id)
    parsed: dict = {}
    warnings: list[str] = []
    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    source = "raw note"

    if jsonl_path:
        parsed = parse_full_transcript(jsonl_path)
        user_msgs = parsed.get("user_msgs", [])
        assistant_msgs = parsed.get("assistant_msgs", [])
        warnings = parsed.get("warnings", [])

        # Decide which source to use
        raw_note_would_truncate = parsed.get("raw_note_would_truncate", False)
        truncated = parsed.get("truncated", False)

        # Data always comes from JSONL when found — label accurately
        if truncated:
            source = "JSONL transcript (head+tail, middle truncated)"
        elif raw_note_would_truncate:
            source = "JSONL transcript (raw note would have truncated)"
        else:
            source = "JSONL transcript (full, raw note also sufficient)"
    else:
        # Fall back to raw note content for summarization
        source = "raw note (JSONL not found)"
        # Extract user/assistant messages from raw note conversation section.
        # Supports both session notes (## Conversation (raw)) and snapshot notes
        # (## Last messages (raw)).
        in_conversation = False
        for line in raw_lines:
            stripped = line.strip()
            if stripped in ('## Conversation (raw)', '## Last messages (raw)'):
                in_conversation = True
                continue
            if in_conversation:
                if stripped.startswith('## '):
                    break
                if stripped.startswith('**User:**'):
                    user_msgs.append(stripped[9:].strip())
                elif stripped.startswith('**Assistant:**'):
                    assistant_msgs.append(stripped[14:].strip())

    # Fall back to raw note if JSONL yielded empty messages (corrupted/empty JSONL)
    if jsonl_path and not user_msgs and not assistant_msgs:
        source = "raw note (JSONL found but empty)"
        in_conversation = False
        for line in raw_lines:
            stripped = line.strip()
            if stripped in ('## Conversation (raw)', '## Last messages (raw)'):
                in_conversation = True
                continue
            if in_conversation:
                if stripped.startswith('## '):
                    break
                if stripped.startswith('**User:**'):
                    user_msgs.append(stripped[9:].strip())
                elif stripped.startswith('**Assistant:**'):
                    assistant_msgs.append(stripped[14:].strip())

    if not user_msgs and not assistant_msgs:
        return {
            "ok": False,
            "status": f"Failed: no conversation content in {os.path.basename(note_path)}",
            "fallback_reason": "no_conversation_content",
        }

    # Build metadata for generate_summary
    metadata: dict = {"project": project, "vault_path": vault_path, "sessions_folder": sessions_folder}
    for line in raw_lines[:20]:
        stripped = line.strip()
        if stripped.startswith('git_branch:'):
            metadata["git_branch"] = stripped.split(':', 1)[1].strip().strip('"')
        elif stripped.startswith('duration_minutes:'):
            try:
                metadata["duration_minutes"] = float(stripped.split(':', 1)[1].strip())
            except ValueError:
                pass

    # Add files_touched and errors from parsed transcript if available
    if jsonl_path and parsed:
        metadata["files_touched"] = parsed.get("files_touched", [])
        metadata["errors"] = parsed.get("errors", [])

    # Route by note type — snapshots use a shorter, focused prompt.
    note_type = ""
    for line in raw_lines[:20]:
        if line.strip().startswith("type:"):
            note_type = line.split(":", 1)[1].strip().strip('"').strip("'")
            break

    # Select generator and prepare per-type input. The snapshot cohesion
    # preamble (session path only) is computed ONCE here, before the
    # escalation loop, so it is reused across model retries.
    if note_type != "claude-snapshot":
        date_str = ""
        for line in raw_lines[:20]:
            if line.strip().startswith("date:"):
                date_str = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
        sessions_dir_path = Path(vault_path) / sessions_folder
        preamble = _augment_session_input_with_snapshots(
            "", sessions_dir_path, session_id, date_str, project,
        )
        if preamble:
            # Stash the snapshot preamble into metadata so generate_summary can
            # prepend it to its sampled transcript. New optional key; existing
            # callers unaffected (absent key → no-op).
            metadata["snapshot_preamble"] = preamble

    return {
        "ok": True,
        "raw_lines": raw_lines,
        "session_id": session_id,
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "metadata": metadata,
        "note_type": note_type,
        "source": source,
        "warnings": warnings,
    }


def upgrade_unsummarized_note(
    note_path: str,
    vault_path: str,
    sessions_folder: str,
    project: str,
    summary_model: str = "haiku",
    summary_timeout: int | None = None,
) -> tuple[str, float, str | None, str | None]:
    """Upgrade an unsummarized session note with an AI summary.

    Orchestrates: find JSONL → parse transcript → decide source →
    generate summary → dedup open items → atomic write.

    Returns a 4-tuple ``(status, elapsed_s, model_used, fallback_reason)``:

    - **status** (*str*) — one-line status string for the model to relay.
      Success strings begin with ``"Upgraded "``; all other values (including
      ``"Failed: ..."``) indicate failure. Callers routing on return value —
      notably ``skills/recall/SKILL.md`` Step 2 Phase 1 — MUST check the
      ``"Upgraded "`` prefix as the positive path.
    - **elapsed_s** (*float*) — wall-clock seconds rounded to 2 decimal places.
    - **model_used** (*str | None*) — the CLI alias of the model that produced
      the accepted summary (``"haiku"`` on the common path; ``"sonnet"`` or
      ``"opus"`` when the escalation chain fired on an ``"empty_output"`` from
      the primary model, per #165), or ``None`` on failure paths that never
      reached summarization.
    - **fallback_reason** (*str | None*) — non-``None`` when summarization
      itself failed or a worker / pre-summarization check caught a problem.
      ``None`` indicates that summarization succeeded; it does NOT by itself
      confirm overall success. Callers MUST also check
      ``status.startswith("Upgraded ")`` — ``upgrade_note_with_summary`` can
      still return a ``"Failed: ..."`` status (malformed summary, atomic write
      error, post-write verification failure, etc.) even after the model
      returned a result, and those post-summarization failures currently flow
      through with ``fallback_reason=None``. Classifying them is tracked as a
      Phase 2 prep follow-up. Full taxonomy of populated values:

      Pre-summarization (this function, before any model call):
        ``"unreadable_note"``         — OSError or UnicodeDecodeError reading the note (perms, encoding, ENOENT, bad UTF-8)
        ``"no_session_id"``           — frontmatter is missing the ``session_id:`` field
        ``"no_conversation_content"`` — neither JSONL nor raw-note section yielded messages

      Summarizer subprocess (set inside ``generate_summary`` / ``generate_snapshot_summary``):
        ``"haiku_timeout"``           — ``claude -p`` exceeded the per-call timeout
        ``"haiku_subprocess_error"``  — ``claude -p`` returned non-zero or unexpected I/O
        ``"empty_output"``            — model returned empty / whitespace-only text
        ``"unknown_failure"``         — defensive default returned by ``generate_summary`` / ``generate_snapshot_summary`` when the retry loop exits without setting ``last_reason`` (should be unreachable)

      Worker wrapper (set by ``upgrade_batch`` when a future raises):
        ``"worker_exception"``        — per-note worker raised an uncaught exception

      Reserved for the Phase 2 validator (#167/#84) — declared here for stability,
      not yet emitted by this function:
        ``"missing_section"``         — summary lacks a required H2 section
        ``"importance_missing"``      — IMPORTANCE: N line absent or unparseable
        ``"schema_loose"``            — summary structurally valid but fails strict schema

      Callers MUST treat any non-``None`` value as a failure classifier. Adding a
      new value requires updating both this docstring and the validator (#167/#84).

    Adding any new return prefix to the ``status`` field requires an audit
    of routing call sites.
    """
    _t0 = time.monotonic()

    def _ret(
        status: str,
        model_used: str | None = None,
        fallback_reason: str | None = None,
    ) -> tuple[str, float, str | None, str | None]:
        return status, round(time.monotonic() - _t0, 2), model_used, fallback_reason

    prep = _prepare_note_for_summary(note_path, vault_path, sessions_folder, project)
    if not prep["ok"]:
        return _ret(prep["status"], fallback_reason=prep["fallback_reason"])
    user_msgs = prep["user_msgs"]
    assistant_msgs = prep["assistant_msgs"]
    metadata = prep["metadata"]
    note_type = prep["note_type"]
    source = prep["source"]
    warnings = prep["warnings"]

    gen_fn = generate_snapshot_summary if note_type == "claude-snapshot" else generate_summary

    # Model-escalation chain (#165): try the primary model, then escalate to
    # Sonnet, then Opus, but ONLY when the failure is a quality reason
    # (empty_output). Timeouts / subprocess errors break immediately — a more
    # capable model won't fix a slow CLI cold-start or a missing binary.
    models_to_try = _escalation_models(summary_model)
    summary_text = None
    fallback_reason = None
    model_used = None
    for _model in models_to_try:
        gen_kwargs: dict = {"model": _model}
        if summary_timeout is not None:
            gen_kwargs["timeout"] = summary_timeout
        summary_text, fallback_reason = gen_fn(
            user_msgs, assistant_msgs, metadata, **gen_kwargs,
        )
        if summary_text:
            if _summary_recovery_enabled():
                summary_text, _rec = _normalize_summary(summary_text)
                if _rec:
                    warnings = warnings + [f"summary recovered (#167): {', '.join(_rec)}"]
            model_used = _model
            break
        if fallback_reason not in _MODEL_ESCALATION_REASONS:
            # timeout / subprocess error — escalating model won't help
            break

    if not summary_text:
        return _ret(
            f"Failed: AI summarization returned empty for {os.path.basename(note_path)}",
            model_used=None,
            fallback_reason=fallback_reason,
        )

    status = upgrade_note_with_summary(
        note_path, summary_text, vault_path, sessions_folder, project,
        source=source, warnings=warnings,
    )
    # model_used is the CLI alias of the model that produced the accepted
    # summary — "haiku" on the common path, "sonnet"/"opus" when escalated (#165).
    return _ret(status, model_used=model_used, fallback_reason=None)


def generate_summaries_batch(
    prepared_notes: list[dict],
    model: str = "haiku",
    timeout: int = 120,
    project: str = "unknown",
    vault_path: str = "",
    sessions_folder: str = "",
) -> list[tuple[str | None, str | None]]:
    """Summarize N SESSION notes in ONE ``claude -p --model <model>`` spawn.

    ``prepared_notes`` is a list of ok=True session-note prep dicts (caller
    guarantees: not snapshots, not prep failures).  Returns a list of
    ``(summary_text|None, fallback_reason|None)`` ALIGNED to ``prepared_notes``
    order.

    Failure reasons returned for all notes on whole-spawn failure:
      ``"haiku_timeout"``           — TimeoutExpired (retried once)
      ``"haiku_subprocess_error"``  — rc != 0, FileNotFoundError, or other error (no retry)
      ``"empty_output"``            — subprocess rc==0 but stdout empty (no retry)

    Per-note parse failures on success path:
      ``"missing_section"``         — block absent or lacks ``## Summary``

    (#166)
    """
    n = len(prepared_notes)
    if n == 0:
        return []

    # ---- Build multi-note prompt -----------------------------------------------
    # Collect open items once for all notes (same project).
    existing_items: list = []
    try:
        _hooks_dir = os.path.dirname(os.path.abspath(__file__))
        if _hooks_dir not in sys.path:
            sys.path.insert(0, _hooks_dir)
        from open_item_dedup import collect_open_items as _collect_open_items
        existing_items = _collect_open_items(vault_path, sessions_folder, project, max_sessions=10)
    except Exception as exc:  # noqa: BLE001
        print(f"[obsidian-brain] open item collection failed (non-fatal): {exc}", file=sys.stderr)

    def _sample_msgs(user_msgs: list[str], assistant_msgs: list[str]) -> tuple[str, str]:
        if len(user_msgs) > 20:
            sampled_user: list[str] = (
                user_msgs[:10] + ["[... middle messages omitted ...]"] + user_msgs[-10:]
            )
        else:
            sampled_user = user_msgs
        if len(assistant_msgs) > 20:
            sampled_asst: list[str] = (
                assistant_msgs[:10] + ["[... middle messages omitted ...]"] + assistant_msgs[-10:]
            )
        else:
            sampled_asst = assistant_msgs
        return "\n---\n".join(sampled_user)[:12000], "\n---\n".join(sampled_asst)[:12000]

    prompt_parts: list[str] = [
        f"You are a technical summarizer. You will be given {n} session transcript(s) below.\n"
        f"Each transcript is delimited by a line '===== NOTE k =====' where k is the note number.\n"
        "For EACH note, output its summary starting with a line '===== SUMMARY k =====' on its own line,\n"
        "followed by EXACTLY these markdown sections:\n"
        "## Summary / ## Key Decisions / ## Changes Made / ## Errors Encountered / ## Open Questions / Next Steps / ## Importance\n"
        f"Output blocks for ALL {n} notes, in order. NO commentary outside the delimited blocks.\n"
        "Do NOT respond conversationally. Do NOT ask questions. Just output the summaries.\n\n"
        "For ## Importance: Rate 1-10. 1-3: trivial. 4-6: standard. 7-8: key decisions. 9-10: major.\n"
    ]

    for k, prep in enumerate(prepared_notes, start=1):
        user_sample, asst_sample = _sample_msgs(prep["user_msgs"], prep["assistant_msgs"])
        meta = prep["metadata"]
        preamble = meta.get("snapshot_preamble", "")
        files_str = ", ".join(meta.get("files_touched", [])[:15]) or "none detected"
        meta_line = (
            f"NOTE {k} METADATA: project={meta.get('project', 'unknown')} "
            f"branch={meta.get('git_branch', 'unknown')} "
            f"files_touched={files_str}"
        )
        prompt_parts.append(f"===== NOTE {k} =====\n")
        prompt_parts.append(f"{meta_line}\n")
        if preamble:
            prompt_parts.append(
                f"{preamble}\n"
                "Some earlier context may come from pre-compact snapshots — "
                "synthesize the whole arc into a cohesive narrative.\n"
            )
        prompt_parts.append(
            f"SESSION TRANSCRIPT:\n{user_sample}\n\n---\n\n{asst_sample}\n\n"
        )
        prompt_parts.append(
            "OUTPUT EXACTLY these markdown sections with no preamble, no commentary:\n\n"
            "## Summary\n1-3 sentence overview of what was accomplished.\n\n"
            "## Key Decisions\n- Bullet list. Write \"None noted.\" if none.\n\n"
            "## Changes Made\n- Bullet list. Write \"None noted.\" if none.\n\n"
            "## Errors Encountered\n- Bullet list. Write \"None.\" if none.\n\n"
            "## Open Questions / Next Steps\n- [ ] Checkbox list. Write \"None.\" if none.\n\n"
            "## Importance\nRate 1-10. Output ONLY the number.\n\n"
        )

    if existing_items:
        prompt_parts.append("## Existing Open Items for This Project (DO NOT DUPLICATE)\n")
        prompt_parts.append(
            "The following items are already tracked in older session notes. Do NOT include any item\n"
            "that is semantically equivalent to these — same PR, same branch, same task, same file.\n"
            "Only add genuinely NEW open items from this session's conversation.\n\n"
        )
        for _, _, item_text in existing_items:
            prompt_parts.append(f"- {item_text}\n")

    prompt = "".join(prompt_parts)

    # ---- Subprocess with retry on timeout -------------------------------------
    attempts = (timeout, timeout * 2)
    last_reason: str | None = None
    stdout_text: str | None = None

    for i, attempt_timeout in enumerate(attempts):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", model],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=attempt_timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                stdout_text = result.stdout.strip()
                break
            if result.returncode != 0:
                last_reason = "haiku_subprocess_error"
                print(
                    f"[obsidian-brain] claude -p batch failed (rc={result.returncode}): "
                    f"{result.stderr[:200]}",
                    file=sys.stderr,
                )
                break
            # rc==0 but empty stdout
            last_reason = "empty_output"
            print("[obsidian-brain] claude -p batch returned empty stdout", file=sys.stderr)
            break
        except FileNotFoundError:
            last_reason = "haiku_subprocess_error"
            print("[obsidian-brain] claude CLI not found, batch summarization unavailable", file=sys.stderr)
            break
        except subprocess.TimeoutExpired:
            last_reason = "haiku_timeout"
            if i < len(attempts) - 1:
                print(
                    f"[obsidian-brain] claude -p batch timed out at {attempt_timeout}s, retrying...",
                    file=sys.stderr,
                )
                continue
            print(
                f"[obsidian-brain] claude -p batch timed out at {attempt_timeout}s, giving up",
                file=sys.stderr,
            )
            break
        except Exception as exc:  # noqa: BLE001
            last_reason = "haiku_subprocess_error"
            print(f"[obsidian-brain] claude -p batch error ({type(exc).__name__}): {exc}", file=sys.stderr)
            break

    # Whole-spawn failure — return same reason for all notes.
    if stdout_text is None:
        reason = last_reason or "haiku_subprocess_error"
        return [(None, reason)] * n

    # ---- Parse delimited output ------------------------------------------------
    try:
        _summary_header_re = re.compile(
            r"^=====\s*SUMMARY\s+(\d+)\s*=====\s*$", re.MULTILINE
        )
        parts_split = _summary_header_re.split(stdout_text)
        # parts_split alternates: [preamble, k1, block1, k2, block2, ...]
        parsed_blocks: dict[int, str] = {}
        it = iter(parts_split)
        next(it, None)  # skip preamble before first header
        for k_str, block in zip(it, it):
            try:
                k_int = int(k_str)
            except ValueError:
                continue
            if k_int not in parsed_blocks:  # first-occurrence-wins (#165 Fix 1)
                parsed_blocks[k_int] = block

        _has_summary_re = re.compile(r"^## Summary", re.MULTILINE)
        _recovery_enabled = _summary_recovery_enabled()

        results: list[tuple[str | None, str | None]] = []
        for i_note, prep in enumerate(prepared_notes):
            k = i_note + 1
            if k in parsed_blocks:
                block_text = parsed_blocks[k].strip()
                # #167: normalize heading variants / synth missing sections BEFORE
                # the ## Summary check so recoverable blocks pass instead of
                # becoming (None, "missing_section").
                if _recovery_enabled:
                    block_text, _rec = _normalize_summary(block_text)
                    if _rec:
                        print(
                            f"[obsidian-brain] batch summary recovered (#167) for note {k}: "
                            f"{', '.join(_rec)}",
                            file=sys.stderr,
                        )
                if _has_summary_re.search(block_text):
                    if existing_items:
                        try:
                            block_text = _dedup_summary_open_items(block_text, existing_items)
                        except Exception as exc:  # noqa: BLE001 — dedup is best-effort, never fail the note
                            print(
                                f"[obsidian-brain] batch open-item dedup failed (non-fatal) "
                                f"({type(exc).__name__}): {exc}",
                                file=sys.stderr,
                            )
                    results.append((block_text, None))
                else:
                    results.append((None, "missing_section"))
            else:
                results.append((None, "missing_section"))
        return results
    except Exception as exc:  # noqa: BLE001
        print(f"[obsidian-brain] batch parse error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return [(None, "missing_section")] * n


def upgrade_batch(
    paths: list[str],
    vault_path: str,
    sessions_folder: str,
    project: str,
    max_workers: int = 10,
    summary_model: str = "haiku",
    summary_timeout: int | None = None,
    summary_batch_size: int | None = None,
) -> list[dict]:
    """Fan out upgrade_unsummarized_note() concurrently, with optional batching.

    Returns a list of per-note dicts IN THE SAME ORDER AS ``paths`` (not
    completion order). Each dict has the keys:

      * ``path``  — vault note path (echoes input)
      * ``status`` — one-line status string; success starts with ``"Upgraded "``
      * ``elapsed_s`` — wall-time inside the worker, rounded to 2 dp
      * ``model_used`` — see ``upgrade_unsummarized_note`` docstring
      * ``fallback_reason`` — see ``upgrade_unsummarized_note`` docstring

    ``summary_batch_size`` controls note grouping for the ``claude -p`` spawn:
      * ``None`` (default) — read from ``load_config()`` (config key
        ``summary_batch_size``, default 3).
      * ``1`` — legacy per-note fan-out via ThreadPoolExecutor (unchanged
        behavior; use to pin legacy-contract tests).
      * ``>=2`` — batched path: session notes are grouped into batches of up
        to ``summary_batch_size`` and summarized in a single ``claude -p``
        spawn per group, amortizing CLI startup cost (~70% startup-overhead
        reduction vs. per-note). Snapshots always go through the solo path.
        Per-note parse failures (``missing_section``) and whole-spawn failures
        fall through to ``upgrade_unsummarized_note`` (the solo path), so the
        Phase 2 sub-agent in SKILL.md still catches anything both paths fail.

    Side effect: appends one record per call to
    ``~/.claude/obsidian-brain-summarizer-metrics.jsonl`` via
    ``summarizer_metrics.append_metrics_record`` (issue #74). The sink never
    raises, so telemetry failures cannot break this function.

    Per-note exceptions are caught and converted to dicts with
    ``status="Failed: <exc_type>: <exc_msg>"``, ``elapsed_s=0.0``,
    ``model_used=None``, ``fallback_reason="worker_exception"`` so one bad
    note never kills the batch. The exception message is truncated to 500
    characters to avoid JSONL blow-out.

    Raises ``ValueError`` if ``max_workers < 1``.
    """
    if not paths:
        return []

    if max_workers < 1:
        raise ValueError(f"max_workers must be >= 1, got {max_workers}")

    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone

    # Resolve batch size.
    if summary_batch_size is None:
        try:
            summary_batch_size = int(load_config().get("summary_batch_size", 3))
        except Exception as exc:  # noqa: BLE001
            print(
                f"[obsidian-brain] invalid summary_batch_size in config "
                f"({type(exc).__name__}): {exc}; using 3",
                file=sys.stderr,
            )
            summary_batch_size = 3
    else:
        summary_batch_size = int(summary_batch_size)
    summary_batch_size = max(1, summary_batch_size)

    _wall_t0 = time.monotonic()

    # ---- Legacy per-note fan-out path (batch_size <= 1) ----------------------
    if summary_batch_size <= 1:
        workers = min(max_workers, len(paths))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [
                ex.submit(
                    upgrade_unsummarized_note,
                    p,
                    vault_path,
                    sessions_folder,
                    project,
                    summary_model,
                    summary_timeout,
                )
                for p in paths
            ]
            results: list[dict] = []
            for p, fut in zip(paths, futs):
                try:
                    status, elapsed_s, model_used, fallback_reason = fut.result()
                except Exception as exc:  # noqa: BLE001 — per-note isolation
                    exc_str = str(exc)[:500]
                    status = f"Failed: {type(exc).__name__}: {exc_str}"
                    elapsed_s, model_used, fallback_reason = 0.0, None, "worker_exception"
                results.append({
                    "path": p,
                    "status": status,
                    "elapsed_s": elapsed_s,
                    "model_used": model_used,
                    "fallback_reason": fallback_reason,
                })

        wall_s = round(time.monotonic() - _wall_t0, 2)
        try:
            _hooks_dir = os.path.dirname(os.path.abspath(__file__))
            if _hooks_dir not in sys.path:
                sys.path.insert(0, _hooks_dir)
            import summarizer_metrics
            summarizer_metrics.append_metrics_record({
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "project": project,
                "n_notes": len(results),
                "wall_s": wall_s,
                "notes": results,
            })
        except Exception as exc:  # noqa: BLE001 — telemetry must never break recall
            print(
                f"[obsidian-brain] metrics sink unavailable ({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
        return results

    # ---- Batched path (batch_size >= 2) ---------------------------------------

    # Step 1: Prepare all notes concurrently.
    workers = min(max_workers, len(paths))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        prep_futs = {p: ex.submit(_prepare_note_for_summary, p, vault_path, sessions_folder, project)
                     for p in paths}
    preps: dict[str, dict] = {}
    for p, fut in prep_futs.items():
        try:
            preps[p] = fut.result()
        except Exception as exc:  # noqa: BLE001
            preps[p] = {
                "ok": False,
                "status": f"Failed: worker error preparing {os.path.basename(p)}: {str(exc)[:200]}",
                "fallback_reason": "worker_exception",
            }

    results_by_path: dict[str, dict] = {}

    # Step 2: Route prep failures immediately.
    failed_prep_paths = [p for p in paths if not preps[p]["ok"]]
    for p in failed_prep_paths:
        prep = preps[p]
        results_by_path[p] = {
            "path": p,
            "status": prep["status"],
            "elapsed_s": 0.0,
            "model_used": None,
            "fallback_reason": prep.get("fallback_reason"),
        }

    # Step 3: Route snapshots to solo path.
    snapshot_paths = [p for p in paths if preps[p].get("ok") and preps[p].get("note_type") == "claude-snapshot"]
    session_paths = [p for p in paths if preps[p].get("ok") and preps[p].get("note_type") != "claude-snapshot"]

    if snapshot_paths:
        snap_workers = min(max_workers, len(snapshot_paths))
        with ThreadPoolExecutor(max_workers=snap_workers) as ex:
            snap_futs = {
                p: ex.submit(upgrade_unsummarized_note, p, vault_path, sessions_folder, project, summary_model, summary_timeout)
                for p in snapshot_paths
            }
        for p, fut in snap_futs.items():
            try:
                status, elapsed_s, model_used, fallback_reason = fut.result()
            except Exception as exc:  # noqa: BLE001
                exc_str = str(exc)[:500]
                status = f"Failed: {type(exc).__name__}: {exc_str}"
                elapsed_s, model_used, fallback_reason = 0.0, None, "worker_exception"
            results_by_path[p] = {
                "path": p,
                "status": status,
                "elapsed_s": elapsed_s,
                "model_used": model_used,
                "fallback_reason": fallback_reason,
            }

    # Step 4: Group session notes into size/char-aware batches.
    _CHAR_CAP = 60000

    groups: list[list[str]] = []
    current_group: list[str] = []
    current_chars = 0

    for p in session_paths:
        prep = preps[p]
        u_est = min(len("\n---\n".join(prep["user_msgs"])), 12000)
        a_est = min(len("\n---\n".join(prep["assistant_msgs"])), 12000)
        note_chars = u_est + a_est
        if current_group and (len(current_group) >= summary_batch_size or current_chars + note_chars > _CHAR_CAP):
            groups.append(current_group)
            current_group = [p]
            current_chars = note_chars
        else:
            current_group.append(p)
            current_chars += note_chars
    if current_group:
        groups.append(current_group)

    # Step 5: Process each group.
    for group_paths in groups:
        group_preps = [preps[p] for p in group_paths]
        g_t0 = time.monotonic()

        # Try primary model, escalate on whole-group empty_output.
        models_to_try = _escalation_models(summary_model)
        group_results: list[tuple[str | None, str | None]] | None = None
        used_model = summary_model

        for _model in models_to_try:
            batch_out = generate_summaries_batch(
                group_preps,
                model=_model,
                timeout=(summary_timeout or 120),
                project=project,
                vault_path=vault_path,
                sessions_folder=sessions_folder,
            )
            # Escalate at group level only on whole-spawn empty_output.
            if all(text is None and reason == "empty_output" for text, reason in batch_out):
                if _model != models_to_try[-1]:
                    continue  # try next model
            used_model = _model
            group_results = batch_out
            break

        if group_results is None:
            print(
                "[obsidian-brain] BUG: group_results unset after escalation loop (empty models_to_try?)",
                file=sys.stderr,
            )
            group_results = [(None, "empty_output")] * len(group_paths)

        g_wall = time.monotonic() - g_t0
        per_note_elapsed = round(g_wall / len(group_paths), 2)

        # Solo fallback paths for notes that need it.
        solo_fallback_paths: list[str] = []

        for p, (summary_text, parse_reason) in zip(group_paths, group_results):
            try:
                if summary_text is not None:
                    # Attempt write-back.
                    prep = preps[p]
                    write_status = upgrade_note_with_summary(
                        p, summary_text, vault_path, sessions_folder, project,
                        source=prep["source"], warnings=prep["warnings"],
                    )
                    if write_status.startswith("Upgraded "):
                        results_by_path[p] = {
                            "path": p,
                            "status": write_status,
                            "elapsed_s": per_note_elapsed,
                            "model_used": used_model,
                            "fallback_reason": None,
                        }
                    else:
                        # Write-back failed — route to solo fallback.
                        solo_fallback_paths.append(p)
                else:
                    # No usable summary — route to solo fallback.
                    solo_fallback_paths.append(p)
            except Exception as exc:  # noqa: BLE001 — never let one note crash the batch
                print(
                    f"[obsidian-brain] write-back raised unexpectedly for "
                    f"{os.path.basename(p)} ({type(exc).__name__}): {exc}",
                    file=sys.stderr,
                )
                solo_fallback_paths.append(p)

        # Run solo fallback for all notes that need it in this group.
        if solo_fallback_paths:
            solo_workers = min(max_workers, len(solo_fallback_paths))
            with ThreadPoolExecutor(max_workers=solo_workers) as ex:
                solo_futs = {
                    p: ex.submit(upgrade_unsummarized_note, p, vault_path, sessions_folder, project, summary_model, summary_timeout)
                    for p in solo_fallback_paths
                }
            for p, fut in solo_futs.items():
                try:
                    status, elapsed_s, model_used, fallback_reason = fut.result()
                except Exception as exc:  # noqa: BLE001
                    exc_str = str(exc)[:500]
                    status = f"Failed: {type(exc).__name__}: {exc_str}"
                    elapsed_s, model_used, fallback_reason = 0.0, None, "worker_exception"
                results_by_path[p] = {
                    "path": p,
                    "status": status,
                    "elapsed_s": elapsed_s,
                    "model_used": model_used,
                    "fallback_reason": fallback_reason,
                }

    # Step 6: Assemble in input order.
    results = [results_by_path[p] for p in paths]

    wall_s = round(time.monotonic() - _wall_t0, 2)
    try:
        _hooks_dir = os.path.dirname(os.path.abspath(__file__))
        if _hooks_dir not in sys.path:
            sys.path.insert(0, _hooks_dir)
        import summarizer_metrics
        summarizer_metrics.append_metrics_record({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "project": project,
            "n_notes": len(results),
            "wall_s": wall_s,
            "notes": results,
        })
    except Exception as exc:  # noqa: BLE001 — telemetry must never break recall
        print(
            f"[obsidian-brain] metrics sink unavailable ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
    return results
