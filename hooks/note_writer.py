"""CLI for deterministic vault note persistence.

Skills previously persisted vault notes via a Write tool call, which breaks
in environments that route writes through a context-blind helper sub-agent
(#269). This gives skills two deterministic commands instead:

- ``write``: create a new note. Reads note content from stdin, delegates to
  obsidian_utils.write_vault_note for the atomic mkdir + traversal-checked +
  chmod 0o600 + rename write, and prints the absolute destination path on
  success so the skill can echo it back to the user.
- ``append-update``: append a dated update section to an EXISTING note (used
  by /compress's update flow) and apply frontmatter mutations
  (``last_updated``, new tags) in the SAME atomic write. Never creates a
  file -- the target must already exist.

Stdin is capped at 1_000_000 characters (project CLAUDE.md security pattern)
and OVERSIZE input is rejected, never truncated: a silently truncated note
that still reports ``OK:`` is the exact failure mode this CLI exists to
remove (worst at /standup's in-place overwrite, where a truncated write
destroys the original note).

Both commands also validate the CONTENT they are handed, not just their
arguments: empty/whitespace-only stdin, a note body with no frontmatter
fence pair, and malformed ``--add-tags``/``--last-updated`` values are all
rejected before any filesystem side effect. Without that, the CLI faithfully
persists garbage and prints ``OK:`` — the skill then reports "saved!" over a
0-byte or structurally broken note.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

from obsidian_utils import write_vault_note

# Counts CHARACTERS, not bytes -- sys.stdin.read(n) is a character read on a
# text stream (the docstrings have always said "characters"; the constant was
# named ...BYTES, which contradicted them). Note hooks/check_items_cli.py has
# its own separate STDIN_CAP_BYTES; this rename is deliberately local.
STDIN_CAP_CHARS = 1_000_000


def _read_stdin_capped() -> tuple[str, bool]:
    """Read stdin, returning ``(text, oversized)``.

    Reads ``STDIN_CAP_CHARS + 1`` characters so overflow is *detectable*:
    a plain ``read(cap)`` returns a silently truncated string that is
    indistinguishable from an input that happened to be exactly at the cap,
    which is how a 1.2 MB note previously landed truncated mid-body with an
    ``OK:`` line. Callers must reject when ``oversized`` is True rather than
    writing the truncated prefix.
    """
    text = sys.stdin.read(STDIN_CAP_CHARS + 1)
    if len(text) > STDIN_CAP_CHARS:
        return text[:STDIN_CAP_CHARS], True
    return text, False


def _validate_folder(folder: str):
    """Return an error message if ``folder`` is not a benign, vault-relative
    path, else None.

    write_vault_note()'s own containment check only blocks writes that
    escape the vault ROOT — it does not constrain *where inside* the vault
    a write lands. Obsidian executes JavaScript from
    ``.obsidian/plugins/<x>/main.js``, so a folder like
    ``.obsidian/plugins/evil`` passes containment cleanly and lands
    executable code. ``folder``/``filename`` are assembled by an LLM from
    session content, so they're not fully trusted inputs — validate here,
    before any filesystem side effect.

    Deliberately NOT an allowlist of known vault folder names: folder names
    are user-configurable (~/.claude/obsidian-brain-config.json) and several
    skills write to different folders. The rules below (no absolute/home
    path, no vault-root alias, no ``..`` segment, no dot-prefixed segment)
    block the actual vector without that coupling.

    Note: ``Path("").parts == Path(".").parts == ()`` — pathlib treats both
    as "no path at all" rather than as a single dot-segment, so the
    dot-prefix loop below never sees them. ``folder in ("", ".")`` is
    checked explicitly for that reason: unchecked, ``Path(vault)/""`` and
    ``Path(vault)/"."`` both collapse to the vault root itself (still fully
    contained, so write_vault_note()'s own guard would not catch it either),
    silently defeating the "write targets a named subfolder" contract.
    """
    if folder.startswith("/"):
        return f"invalid folder (must be relative to vault root, not absolute): {folder!r}"
    if folder.startswith("~"):
        return f"invalid folder (must not reference a home directory with '~'): {folder!r}"
    if folder in ("", "."):
        return f"invalid folder (must name a subfolder, not the vault root itself): {folder!r}"
    for part in Path(folder).parts:
        if part == "..":
            return f"invalid folder (contains a '..' traversal segment): {folder!r}"
        if part.startswith("."):
            return (
                f"invalid folder (contains a hidden/dot path segment, e.g. "
                f"'.obsidian'): {folder!r}"
            )
    return None


def _validate_vault_path(vault_path: str):
    """Return an error message if ``vault_path`` is not a non-empty,
    absolute path to an existing directory, else None.

    Neither ``write_vault_note()`` nor ``_resolve_note_path()`` validates
    this argument -- both resolve it and then check *containment* of the
    folder/note-path *within* it, which is a no-op guard when the vault
    path itself is degenerate: ``Path("").resolve()`` and
    ``Path(".").resolve()`` both resolve to the current working directory,
    so a write is trivially "contained" in whatever directory the process
    happens to be running in.

    Every skill call site is preceded by `cd "$(git rev-parse
    --show-toplevel ...)"`, so if a skill block runs with `$VAULT_PATH`
    left unsubstituted (e.g. an unset variable in a fresh shell), an
    unvalidated empty vault_path silently writes session content into the
    user's git working tree -- exit 0, an `OK:` line, no filesystem error
    at all. Verified empirically: prior to this guard,
    ``write("", "claude-insights", "leak.md")`` created
    ``<cwd>/claude-insights/leak.md`` and printed ``OK:``.

    Checked, in order: non-empty, absolute (a relative path is exactly as
    unmoored as an empty one -- it resolves relative to the CWD too), and
    an existing directory. This runs before any other validation or
    filesystem side effect in both ``write`` and ``append-update``.
    """
    if not vault_path:
        return f"invalid vault path (must not be empty): {vault_path!r}"
    if not Path(vault_path).is_absolute():
        return f"invalid vault path (must be absolute, not relative): {vault_path!r}"
    if not Path(vault_path).is_dir():
        return f"invalid vault path (not an existing directory): {vault_path!r}"
    return None


def _validate_filename(filename: str):
    """Return an error message if ``filename`` is not a bare ``*.md`` name,
    else None.

    Bare-name check (``Path(filename).name == filename``) rejects any path
    separator — including ``../`` traversal and a plain subdirectory like
    ``notes/x.md`` — so a filename segment can never place the write outside
    the single target folder write_vault_note() was given.

    Also rejects any leading-dot filename, which would create a note
    invisible to Obsidian (never indexed, never surfaced by /recall) while
    the skill prints "saved!" with a path. Checked as ``startswith(".")``,
    not via a length comparison against ``".md"`` and not via
    ``Path(...).stem``: the length check caught only the literal ``".md"``
    and let ``"..md"`` and ``".secret.md"`` through, and pathlib treats a
    leading-dot name as an extension-less dotfile, so
    ``Path(".md").stem == ".md"`` (not empty) and would silently miss all
    three. ``startswith(".")`` subsumes every one of them.
    """
    if Path(filename).name != filename:
        return (
            f"invalid filename (must be a bare name, no path separators): "
            f"{filename!r}"
        )
    if not filename.endswith(".md"):
        return f"invalid filename (must end with .md): {filename!r}"
    if filename.startswith("."):
        return (
            f"invalid filename (must not start with '.' — a hidden note is "
            f"never indexed by Obsidian): {filename!r}"
        )
    return None


def _validate_note_content(content: str):
    """Return an error message if ``content`` is not a plausible, complete
    vault note, else None.

    Three checks, all on the content the caller piped in on stdin — the
    layer nothing else inspects:

    1. Non-empty (after stripping whitespace). An empty heredoc body, or a
       shell variable that expanded to nothing, previously produced a
       0-byte note, exit 0 and an ``OK:`` line; /retro then armed its
       Stop-hook classification gate pointing at that empty file.
    2. Starts with a ``---`` frontmatter fence at column 0. Every caller
       pipes a full note (frontmatter + body), so this is safe for all of
       them — and it permanently closes the indented-heredoc corruption
       class: an indented ``   ---`` is not a frontmatter fence, so a
       wrongly-indented heredoc now fails loudly instead of landing a note
       whose frontmatter no longer parses.
    3. Parses as a frontmatter block under the SAME rules ``append-update``
       uses (``_split_frontmatter``). This check used to be an independent,
       unbounded "is there a bare ``---`` anywhere below?" scan, which let
       ``write`` accept notes ``append-update`` then refused forever: a note
       whose frontmatter has no closing fence but whose BODY contains a
       ``---`` horizontal rule passed here and was unreachable to the update
       path for the rest of its life. Delegating means the two commands
       cannot drift apart again -- anything this CLI writes, it can update.
    """
    if not content.strip():
        return "note content is empty or whitespace-only (nothing written)"

    lines = _split_lines_lf_crlf(content)
    first = lines[0].rstrip("\r\n")
    if first != "---":
        return (
            "note content must begin with a '---' frontmatter fence at "
            f"column 0 (first line was: {first[:60]!r})"
        )
    _open, fm_lines, _close, _body, split_err = _split_frontmatter(lines)
    if split_err:
        return f"note content has an unparseable frontmatter block: {split_err}"
    return None


def _validate_update_text(update_text: str):
    """Return an error message if ``update_text`` is not a usable update
    section, else None.

    Empty/whitespace-only content previously returned ``OK:``, bumped
    ``last_updated`` and appended only blank lines — the note looked freshly
    updated but gained nothing, and /compress deliberately drops its
    verification re-read on the strength of this command's exit code.

    ``strip()`` being non-empty is exactly equivalent to "contains at least
    one non-blank line": a line with any non-whitespace character survives
    ``strip()``, and a text made only of blank lines does not. One check
    covers both requirements.
    """
    if not update_text.strip():
        return (
            "update section content is empty or whitespace-only "
            "(note left unchanged)"
        )
    return None


def run_write(
    vault_path: str,
    folder: str,
    filename: str,
    content: str,
    overwrite: bool = False,
) -> int:
    """Write ``content`` to ``<vault_path>/<folder>/<filename>``.

    Validates ``vault_path``/``folder``/``filename`` (see
    _validate_vault_path/_validate_folder/_validate_filename) and then
    ``content`` (see _validate_note_content) before touching the
    filesystem, then delegates the actual write entirely to
    write_vault_note() for the atomic write, the path-traversal containment
    check, and the 0o600 permission — this function does not reimplement
    any of that.

    Argument validation runs BEFORE content validation deliberately: the
    path guards are the security-critical ones, and keeping them first means
    a traversal/dot-segment probe is still rejected *by its own guard*
    regardless of what content happens to be piped in.

    ``overwrite`` (CLI: ``--overwrite``) must be passed explicitly to
    replace an existing note. Claude Code's Write tool — which every caller
    used before #269 — refuses to overwrite a file it has not Read in the
    session, so a filename-hash collision used to be loud; without this
    flag the conversion would silently destroy an existing insight and
    report success. Exactly one call site legitimately overwrites in place
    (/standup's Step 6.6 note upgrade) and it passes the flag.

    Prints ``OK: <abs path>`` to stdout and returns 0 on success.
    Prints ``ERROR: <msg>`` to stderr and returns 1 on failure (validation
    rejection or write_vault_note() error). Never partially writes:
    validation failures have no filesystem side effect at all, and
    write_vault_note() either lands the file atomically or returns an error
    before any rename.
    """
    validation_err = (
        _validate_vault_path(vault_path)
        or _validate_folder(folder)
        or _validate_filename(filename)
        or _validate_note_content(content)
    )
    if validation_err:
        print(f"ERROR: {validation_err}", file=sys.stderr)
        return 1

    dest_probe = Path(vault_path) / folder / filename
    if not overwrite and dest_probe.exists():
        print(
            f"ERROR: note already exists (pass --overwrite to replace it "
            f"deliberately): {dest_probe}",
            file=sys.stderr,
        )
        return 1

    err = write_vault_note(vault_path, folder, filename, content)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    # write_vault_note() returns only None/error, not the path, so the
    # destination is composed here from the same three inputs it was
    # given — kept in sync with its own dest_dir/dest construction.
    dest = Path(vault_path) / folder / filename
    print(f"OK: {dest.resolve()}")
    return 0


# ---------------------------------------------------------------------------
# append-update
# ---------------------------------------------------------------------------

# Trailing metadata sections that mark the end of the "real" note body.
# Reuses the exact list obsidian_utils.upgrade_note_with_summary() treats as
# audit-trail sections to preserve (see its `audit_sections` list, ~line
# 4300), plus the "_(Summary source: ...)_" line the /compress SKILL.md also
# names. An update section must land BEFORE the first of these, scanning
# top-down -- never after -- so it reads as part of the note's substantive
# content rather than getting buried under (or inside) the audit trail.
_TRAILING_MARKERS = (
    "## Tool Usage",
    "## Conversation (raw)",
    "## Session Metadata",
    "## Files Touched",
)
_SUMMARY_SOURCE_RE = re.compile(r"^_\(Summary source:.*\)_\s*$")

_DATE_LINE_RE = re.compile(r"^date:\s*.*$")
_LAST_UPDATED_LINE_RE = re.compile(r"^last_updated:\s*.*$")
# Tolerates a trailing YAML comment (`tags:   # topics`), which the old
# `^tags:\s*$` rejected -- silently dropping every requested tag on an
# otherwise ordinary note.
_TAGS_KEY_RE = re.compile(r"^tags:\s*(#.*)?$")
_TAG_ITEM_RE = re.compile(r"^(?P<indent>\s*)-\s*(?P<tag>\S.*?)\s*$")

# Shape of a line that may legitimately appear INSIDE frontmatter, used to
# bound _split_frontmatter's closing-fence search (see its docstring).
_FM_MAX_LINES = 1000
_FM_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*\s*:")
_FM_ITEM_RE = re.compile(r"^\s*-\s")
# An indented continuation: multi-line YAML values, block scalars (`x: |`),
# and nested mappings all produce these.
_FM_CONT_RE = re.compile(r"^\s+\S")
# YAML flow-style tags, e.g. `tags: [claude/insight, claude/topic/foo]` or
# the empty `tags: []` -- an alternative to the block style above that
# _TAGS_KEY_RE/_TAG_ITEM_RE don't recognize (bug: silently no-op'd on a
# flow-style note, reported as a false success).
# `tail` captures any trailing YAML comment so a rewrite preserves it.
# Without this, `tags: [a, b] # topics` matched NEITHER style and the
# whole update aborted -- a harsher outcome than the block-style
# equivalent that _TAGS_KEY_RE was already widened for. `inner` stays
# GREEDY: it must run to the LAST `]` on the line, so a `]` inside a
# quoted existing item does not truncate the list.
_TAGS_FLOW_RE = re.compile(r"^tags:\s*\[(?P<inner>.*)\](?P<tail>\s*(?:#.*)?)$")

# All comparisons below use rstrip("\r\n") (a character-set strip, not a
# substring strip) rather than rstrip("\n"), so a CRLF-authored note's line
# endings are recognized -- match/compare only ever inspects content, never
# reconstructs a line from its stripped form, so this never discards an
# original line ending.


# What a tag value may contain -- an ALLOWLIST, deliberately not a denylist.
# A previous version enumerated forbidden YAML metacharacters (`:`, `#`,
# quotes) and leaked `]`: `--add-tags 'a]'` on a flow-style note produced
# `tags: [claude/insight, a]]`, which makes yaml.safe_load fail on the WHOLE
# frontmatter -- the note loses `type` and every tag, at rc 0. `[`, `{`, `}`,
# `,`, `&`, `*`, `!`, `%`, `@` are all the same shape, and chasing them one
# at a time is how that bug arrived. Tags are a constrained format
# (`claude/topic/foo`), so enumerate what is legal instead: start
# alphanumeric, then alphanumerics plus `/ _ . -`.
#
# \A/\Z, not ^/$: `$` also matches just before a trailing newline, so
# `^...$` would accept `"claude/topic/a\n"`.
_TAG_VALUE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9/_.\-]*\Z")

# Same two traps as above: `$` accepted `"2026-01-01\n"` (which injected a
# blank line into the frontmatter), and Python's `\d` is Unicode-aware, so
# `٢٠٢٦-٠١-٠١` matched and was written verbatim. re.ASCII + \A/\Z close both.
_LAST_UPDATED_VALUE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", re.ASCII)

# A fenced code block opener/closer: up to 3 leading spaces/tabs, then a run
# of >= 3 backticks or tildes (CommonMark). Used to keep the insertion-point
# scan out of fenced blocks.
_FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# An update section this command appended earlier. Everything from here to
# EOF is appended-update territory, never the original audit trail -- see
# _find_insertion_index.
_UPDATE_HEADING_RE = re.compile(r"^##\s+Update\s*\(")


def _validate_tags(add_tags_csv):
    """Return ``(tags, error)`` for a ``--add-tags`` CSV value.

    ``add_tags_csv is None`` (flag omitted) yields ``([], None)`` — not
    requested is not an error. A flag that IS present must name at least one
    valid tag: these values are LLM-generated from session content and are
    interpolated straight into YAML frontmatter, so by this repo's own
    standard they are untrusted input and every one of them is validated
    before either merge path (block-style or flow-style) runs.

    Per item: surrounding spaces/tabs are stripped (conventional CSV
    spacing, e.g. ``a, b``), then the tag must be non-empty and must match
    ``_TAG_VALUE_RE`` — an allowlist, see its comment for why a denylist is
    the wrong shape here. Note the strip is ``" \\t"`` only, so it
    deliberately does NOT strip a newline: ``"a\\ntype: hijacked"`` is still
    seen (and rejected) rather than quietly trimmed into something valid.
    """
    if add_tags_csv is None:
        return [], None

    tags = []
    for raw in add_tags_csv.split(","):
        tag = raw.strip(" \t")
        if not tag:
            return None, (
                f"invalid --add-tags value (empty tag item): {add_tags_csv!r} "
                "— omit the flag entirely when there are no new tags"
            )
        if not _TAG_VALUE_RE.match(tag):
            return None, (
                f"invalid tag {tag!r}: a tag must start with a letter or digit "
                "and contain only letters, digits, '/', '_', '.' and '-' "
                "(anything else can corrupt the note's YAML frontmatter)"
            )
        tags.append(tag)
    return tags, None


def _validate_last_updated(last_updated):
    """Return an error message if a PRESENT ``--last-updated`` value is
    unusable, else None. ``None`` (flag omitted) is fine — that means "do
    not bump", which is the documented opt-in behaviour.

    An empty value is the ``$VAULT_PATH=""`` shape all over again: the call
    site passes ``--last-updated "$TODAY"``, and an unset ``TODAY`` yields a
    present flag with an empty value, which used to be indistinguishable
    from "flag omitted" and skipped the bump with no signal at all. The
    ``YYYY-MM-DD`` format check additionally keeps an arbitrary string
    (newline included) out of the frontmatter line this value is rendered
    into.
    """
    if last_updated is None:
        return None
    if not last_updated.strip():
        return (
            "--last-updated was given an empty value (omit the flag entirely "
            "if no last_updated bump is wanted)"
        )
    if not _LAST_UPDATED_VALUE_RE.match(last_updated):
        return f"invalid --last-updated value (must be YYYY-MM-DD): {last_updated!r}"
    return None


def _find_insertion_index(body_lines: list[str]) -> int:
    """Return the index in ``body_lines`` where the update section belongs:
    the first trailing marker at fence depth 0, or ``len(body_lines)`` if
    there is none or if an appended update section is reached first.

    The scan STOPS at the first ``## Update (`` heading (returning EOF).
    Without that stop, the first update whose text contains a column-0
    ``## Tool Usage`` (or any other trailing marker) writes that marker into
    the body, and every later update then treats it as the start of the audit
    trail and inserts BEFORE it -- splicing the new update into the MIDDLE of
    the earlier one, at rc 0, and compounding on every subsequent run.
    Updates are appended in date order, so anything at or past the first
    ``## Update (`` heading is by construction not the original audit trail.

    Deliberate tradeoff, recorded so it is not "fixed" by accident: on a note
    that already has an update AND a real audit trail, later updates now land
    at EOF (after the audit trail) rather than before it. That is cosmetically
    worse than the old placement but it is not destructive, whereas the old
    behaviour shredded user content. A structural rule cannot separate a real
    ``## Tool Usage`` audit heading from one quoted inside an update section
    -- they are byte-identical -- so the ambiguity is resolved toward not
    corrupting anything.

    Fence tracking is load-bearing, not defensive: notes ABOUT this plugin
    routinely quote a session-note template inside a fenced block, and such
    a block contains lines like ``## Tool Usage``. Without fence state the
    scan matched the quoted heading and wedged the whole update section
    INSIDE someone else's code fence — rendering it as literal code and
    pushing the real body out past the fence, at exit 0.

    A fence opens on a line of >= 3 backticks/tildes and closes only on a
    line using the SAME character, at least as long, with nothing but
    whitespace after it (CommonMark) — so a ```` ```python ```` opener is
    not mistaken for a closer. An unclosed fence swallows the rest of the
    note, which degrades to "append at EOF": the safe direction.
    """
    open_fence = None  # (fence char, fence length)
    for idx, line in enumerate(body_lines):
        stripped = line.rstrip("\r\n")
        if open_fence is None and _UPDATE_HEADING_RE.match(stripped):
            return len(body_lines)
        m = _FENCE_RE.match(stripped)
        if m:
            marker = m.group("fence")
            if open_fence is None:
                open_fence = (marker[0], len(marker))
            elif (
                marker[0] == open_fence[0]
                and len(marker) >= open_fence[1]
                and not m.group("info").strip()
            ):
                open_fence = None
            continue
        if open_fence is None and _is_trailing_marker(line):
            return idx
    return len(body_lines)


def _is_trailing_marker(line: str) -> bool:
    """True if ``line`` is one of the exact trailing-section headings, or
    matches the "_(Summary source: ...)_" line (its suffix varies)."""
    stripped = line.rstrip("\r\n")
    if stripped in _TRAILING_MARKERS:
        return True
    return bool(_SUMMARY_SOURCE_RE.match(stripped))


def _detect_line_ending(text: str) -> str:
    """Return ``text``'s dominant line ending, ``"\\r\\n"`` or ``"\\n"``.

    Any line this command *adds* (frontmatter mutations, the inserted update
    section, separator blank lines) is emitted using this detected ending
    rather than a hardcoded ``"\\n"``, so a CRLF-authored note doesn't get
    silently rewritten to LF -- content outside the inserted section must be
    byte-identical, and that includes not flipping the file's own line-ending
    convention. Ties, or a file with no line endings at all (e.g. a single
    line with no trailing newline), default to ``"\\n"``.
    """
    crlf_count = text.count("\r\n")
    lf_only_count = len(re.findall(r"(?<!\r)\n", text))
    return "\r\n" if crlf_count > lf_only_count else "\n"


def _normalize_eol(text: str, eol: str) -> str:
    """Canonicalize every GENUINE line terminator in ``text`` to ``eol``,
    so content coming from elsewhere (the update-section text piped in on
    stdin, which is not read from the note file and so carries no
    relationship to its line ending) matches the destination note's
    convention instead of mixing endings within one file.

    Exactly two ordered replacements, both targeting only real line
    terminators: (1) collapse existing ``"\\r\\n"`` pairs to ``"\\n"``, then
    (2) expand every ``"\\n"`` to ``eol``. A bare ``"\\r"`` NOT part of a
    ``"\\r\\n"`` pair -- e.g. a pasted terminal progress-bar redraw inside a
    fenced code block -- is touched by neither step and survives
    byte-identical. (A prior version also did a blanket
    ``.replace("\\r", "\\n")``, which silently turned any such bare ``\\r``
    into a real line break -- exactly the kind of content corruption this
    function exists to prevent, just inside the appended section instead of
    the rest of the file. Fixed; do not reintroduce that replacement.)
    """
    normalized = text.replace("\r\n", "\n")
    if eol == "\r\n":
        normalized = normalized.replace("\n", "\r\n")
    return normalized


def _split_lines_lf_crlf(text: str) -> list[str]:
    """Split ``text`` into lines, each with its terminator attached,
    recognizing ONLY ``"\\r\\n"`` and ``"\\n"`` as line terminators.

    Deliberately NOT ``str.splitlines(keepends=True)``: that method also
    treats a bare ``"\\r"`` (not part of a ``"\\r\\n"`` pair) -- plus
    ``\\v``, ``\\f``, and several Unicode separators -- as a line break.
    A note body can legitimately contain a bare ``\\r`` (e.g. a pasted
    terminal progress-bar redraw inside a fenced code block); splitting on
    it would be the same class of corruption ``_normalize_eol`` guards
    against, just via a different function. Every line this parses is
    later reassembled with a plain ``"".join(...)``, so this is lossless
    for reconstruction regardless of where it draws boundaries -- but where
    it draws them still matters for ``_is_trailing_marker``/frontmatter-key
    matching and the "does the file already end in a newline" check, so it
    must not draw a boundary at a bare ``\\r`` that isn't really one.
    """
    lines: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            lines.append(text[start:i + 1])
            start = i + 1
            i += 1
        elif ch == "\r" and i + 1 < n and text[i + 1] == "\n":
            lines.append(text[start:i + 2])
            start = i + 2
            i += 2
        else:
            i += 1
    if start < n:
        lines.append(text[start:])
    return lines


def _unquote_tag(raw: str) -> str:
    """Strip surrounding whitespace and one layer of matching quotes from a
    single flow-style list item, e.g. ``' "claude/insight" '`` ->
    ``'claude/insight'``."""
    item = raw.strip()
    if len(item) >= 2 and item[0] == item[-1] and item[0] in ("'", '"'):
        item = item[1:-1]
    return item


def _split_flow_items(inner: str) -> list[str]:
    """Split a flow-style tag list's raw inner text (the substring between
    ``[`` and ``]``) on top-level commas, returning each item's raw,
    still-possibly-quoted substring verbatim (not yet stripped/unquoted).
    Returns ``[]`` for an empty/whitespace-only inner (the ``tags: []``
    case). Tag values aren't expected to contain a literal comma, so a plain
    ``split(",")`` is sufficient -- no quoted-comma edge case to handle."""
    if not inner.strip():
        return []
    return inner.split(",")


def _resolve_note_path(vault_path: str, note_path: str):
    """Validate and resolve ``note_path`` before any filesystem write.

    Returns ``(resolved Path, None)`` on success or ``(None, error message)``
    on failure. Checked, in order:

    1. Containment: ``resolve()`` + ``is_relative_to()`` against the vault
       root (same pattern as ``write_vault_note()`` / ``_validate_folder()``).
    2. Dot-segment reject on every directory segment between the vault root
       and the file. Containment alone does not stop a write that targets an
       *existing* file already sitting inside ``.obsidian/**`` -- that path
       is fully contained within the vault root, so only this check (mirrors
       ``_validate_folder()``'s dot-segment rule) blocks it.
    3. The target must already exist and be a regular file: unlike
       ``write``, this command only ever appends to an existing note -- it
       must never create one.

    Steps 1-3 are pure ``resolve()``/``exists()``/``is_file()`` checks --
    reads, not writes -- so this can run before the file is ever opened.
    """
    vault_real = Path(vault_path).resolve()
    resolved = Path(note_path).resolve()

    if not resolved.is_relative_to(vault_real):
        return None, f"path traversal blocked: {note_path!r} is not inside vault root"

    rel_parts = resolved.relative_to(vault_real).parts
    for part in rel_parts[:-1]:
        if part.startswith("."):
            return None, (
                "invalid note path (contains a hidden/dot path segment, "
                f"e.g. '.obsidian'): {note_path!r}"
            )

    if not resolved.exists():
        return None, f"note does not exist: {note_path!r}"
    if not resolved.is_file():
        return None, f"note path is not a regular file: {note_path!r}"

    return resolved, None


def _split_frontmatter(lines: list[str]):
    """Split ``lines`` (from ``_split_lines_lf_crlf``) into the
    frontmatter fence pair and body.

    Returns ``(open_fence_line, frontmatter_lines, close_fence_line,
    body_lines, error)``. On success ``error`` is None; on failure the first
    four are None and ``error`` names WHICH failure occurred.

    The distinct error strings matter: the SKILL.md call sites tell the model
    to surface this message to the user, so a wrong diagnosis sends someone to
    repair a file that is not broken. Reporting "no closing '---'" on a note
    whose fence demonstrably exists (it was just past the line bound) is
    exactly that failure.

    The closing-fence search is BOUNDED and SHAPE-CHECKED, not "first ``---``
    anywhere in the file". On a note whose closing fence is missing (e.g.
    corrupted by an earlier bad write) but whose body contains a ``---``
    horizontal rule, the unbounded version treated the title heading and body
    prose as frontmatter: ``last_updated`` and new tags were inserted among
    the prose and the update section was appended after the rule, at rc 0.

    Every candidate frontmatter line must therefore be blank, ``key:``-shaped,
    a ``- `` list item, or an indented continuation (multi-line YAML values).
    A ``# Title`` heading or a prose paragraph is none of those, so the scan
    stops and the whole command fails loudly instead of silently mutating the
    body -- that shape check is the guard doing the real work here.

    ``_FM_MAX_LINES`` is a second, cruder bound for a pathological file whose
    body happens to be all key-shaped lines. It is NOT a "notes are small"
    assumption: it was 200, which false-rejected 11 well-formed notes in the
    live vault -- /emerge and /standup output with long ``projects:`` lists,
    whose closing fences sit as deep as line 460. 1000 is ~2x headroom over
    the deepest observed. Raise it, do not remove it.
    """
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, None, None, None, (
            "malformed frontmatter (file does not open with a '---' fence)"
        )
    for i in range(1, min(len(lines), _FM_MAX_LINES + 1)):
        stripped = lines[i].rstrip("\r\n")
        if stripped == "---":
            return lines[0], lines[1:i], lines[i], lines[i + 1:], None
        if not stripped.strip():
            continue
        if (
            _FM_KEY_RE.match(stripped)
            or _FM_ITEM_RE.match(stripped)
            or _FM_CONT_RE.match(stripped)
        ):
            continue
        return None, None, None, None, (
            "malformed or missing frontmatter (no closing '---'; stopped at a "
            f"line that is not frontmatter: {stripped[:60]!r})"
        )
    if len(lines) > _FM_MAX_LINES:
        return None, None, None, None, (
            f"frontmatter exceeds {_FM_MAX_LINES} lines (limit reached before "
            "the frontmatter block ended -- the note may be fine; this is a "
            "size limit, not a missing fence)"
        )
    return None, None, None, None, "malformed or missing frontmatter (no closing '---')"


def _apply_last_updated(fm_lines: list[str], last_updated: str, eol: str = "\n"):
    """Return ``(new_fm_lines, error)``.

    Replaces an existing top-level ``last_updated:`` line's value in place.
    If absent, inserts a new ``last_updated: <value>`` line immediately
    after the top-level ``date:`` line. Never touches ``date``,
    ``source_session``, ``source_session_note``, or ``type`` -- every other
    line is copied through unchanged.

    ``eol`` is the line ending used for the new/replaced line itself (see
    ``_detect_line_ending``) -- defaults to ``"\\n"`` so direct unit-test
    callers that don't care about CRLF can omit it.
    """
    out = list(fm_lines)
    for idx, line in enumerate(out):
        if _LAST_UPDATED_LINE_RE.match(line.rstrip("\r\n")):
            out[idx] = f"last_updated: {last_updated}{eol}"
            return out, None

    for idx, line in enumerate(out):
        if _DATE_LINE_RE.match(line.rstrip("\r\n")):
            out.insert(idx + 1, f"last_updated: {last_updated}{eol}")
            return out, None

    # No `date:` anchor: append at the END of the frontmatter block rather
    # than failing. Aborting here discarded the whole append -- the update
    # section the user reviewed and approved -- because an OPTIONAL secondary
    # bookkeeping bump had nowhere to anchor. That severity ordering was
    # inverted; `last_updated` is still applied, just without a date anchor.
    # Reachable on real data: one live-vault note carries `created:` and
    # `source_session*` but no `date:`.
    out.append(f"last_updated: {last_updated}{eol}")
    return out, None


def _apply_add_tags(fm_lines: list[str], add_tags: list[str], eol: str = "\n"):
    """Return ``(new_fm_lines, error)`` with ``add_tags`` merged into the
    ``tags:`` block -- either style below, whichever the note actually uses.

    Block style (``tags:`` on its own line, followed by ``- item`` lines):
    new tags are appended at the end of the block, in the block's own
    existing indentation -- including a ZERO indent (``- a``), which is valid
    YAML and what ``yaml.dump`` emits. Two spaces is used only when the block
    has no existing item to copy the indentation from.

    Flow style (``tags: [a, b]`` or ``tags: []`` on one line): new tags are
    appended inside the brackets using ``", "`` as the separator -- the
    conventional flow-style spacing -- and, when every existing item is
    quoted with the same quote character, that same quote character;
    existing items are otherwise left completely untouched (not
    re-serialized), so any of their original micro-spacing survives as-is.

    No-op (returns ``fm_lines`` unchanged, no error) if ``add_tags`` is
    empty, or if every tag in ``add_tags`` is already present. Tags already
    present (in the existing block/list, or repeated within ``add_tags``
    itself) are skipped. ``eol`` is the line ending used for any new/rewritten
    line (see ``_detect_line_ending``).

    ERROR (not a no-op) when ``add_tags`` is non-empty and NO ``tags:`` key
    matched in either style. This used to return ``fm_lines`` unchanged and
    the command still printed ``OK:`` -- the caller asked for a mutation, it
    did not happen, and nothing said so. A silently dropped tag is the exact
    failure class this CLI exists to remove. It is reachable on a real note:
    ``tags:   # topics`` (a trailing YAML comment) did not match the old
    ``^tags:\\s*$`` key regex, so a perfectly ordinary note silently lost
    every requested tag. The regex now tolerates a trailing comment, and
    anything it still cannot recognize fails loudly instead of quietly.
    """
    if not add_tags:
        return fm_lines, None

    # Block style.
    tags_idx = None
    for idx, line in enumerate(fm_lines):
        if _TAGS_KEY_RE.match(line.rstrip("\r\n")):
            tags_idx = idx
            break
    if tags_idx is not None:
        existing = []
        indent = "  "
        end_idx = tags_idx + 1
        for idx in range(tags_idx + 1, len(fm_lines)):
            raw = fm_lines[idx].rstrip("\r\n")
            # Blank and `#`-comment lines INSIDE the block are skipped, not
            # treated as its end. Breaking on them truncated the dedupe set to
            # the items above the comment AND pointed end_idx there, so new
            # items were inserted above the comment and an item below it was
            # re-added as a duplicate. This is the same tolerance _TAGS_KEY_RE
            # already got for `tags:   # topics`; the item scan never got it.
            # end_idx deliberately does NOT advance for these lines, so
            # trailing blanks after the block are not absorbed into it.
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            m = _TAG_ITEM_RE.match(raw)
            if not m:
                break
            # Assign, don't `or` — a ZERO-indent block (`- a`, valid YAML and
            # what yaml.dump emits) yields an empty-string indent, which `or`
            # treats as "not found" and replaces with the 2-space default,
            # appending `  - c` into a zero-indent block and leaving it with
            # mixed indentation. The regex's `indent` group always
            # participates, so an empty match here means "no indent", not
            # "no match". The 2-space default below still applies when the
            # block has no existing items to learn from.
            indent = m.group("indent")
            # _unquote_tag here too, not just on the flow path: a quoted
            # existing item (`- "claude/insight"`) was compared WITH its
            # quotes, so the already-present check missed and the tag was
            # re-added unquoted -- the same tag twice after YAML parsing.
            existing.append(_unquote_tag(m.group("tag")))
            end_idx = idx + 1

        seen = set(existing)
        new_lines = []
        for tag in add_tags:
            if not tag or tag in seen:
                continue
            seen.add(tag)
            new_lines.append(f"{indent}- {tag}{eol}")

        return fm_lines[:end_idx] + new_lines + fm_lines[end_idx:], None

    # Flow style.
    for idx, line in enumerate(fm_lines):
        m = _TAGS_FLOW_RE.match(line.rstrip("\r\n"))
        if not m:
            continue

        raw_items = _split_flow_items(m.group("inner"))
        existing = [_unquote_tag(r) for r in raw_items]
        seen = set(existing)
        new_tags = []
        for tag in add_tags:
            if not tag or tag in seen:
                continue
            seen.add(tag)
            new_tags.append(tag)

        if not new_tags:
            return fm_lines, None  # every requested tag already present

        quote = ""
        if raw_items:
            first = raw_items[0].strip()
            if len(first) >= 2 and first[0] == first[-1] and first[0] in ("'", '"'):
                quote = first[0]

        rendered_new = [f"{quote}{tag}{quote}" for tag in new_tags]
        if raw_items:
            new_inner = m.group("inner").rstrip() + ", " + ", ".join(rendered_new)
        else:
            new_inner = ", ".join(rendered_new)

        new_line = f"tags: [{new_inner}]{m.group('tail')}{eol}"
        return fm_lines[:idx] + [new_line] + fm_lines[idx + 1:], None

    return None, (
        "cannot merge tags: the note has no recognizable 'tags:' block "
        f"(requested: {', '.join(add_tags)}). Add the tags manually, or fix "
        "the note's frontmatter -- a block-style 'tags:' followed by '- item' "
        "lines, or a flow-style 'tags: [a, b]'. A trailing '# comment' on "
        "either form is supported."
    )


# A crashed process must not wedge a note forever, so a lock older than this
# is treated as abandoned and taken over. 60s is far longer than any real
# append-update (milliseconds) and short enough that a user retrying after a
# crash is not left stuck.
_STALE_LOCK_SECONDS = 60


def _lock_path(dest: Path) -> Path:
    """Sibling lock file for ``dest``, dot-prefixed so Obsidian never indexes
    it and the vault's own dot-segment guards treat it as hidden."""
    return dest.parent / f".{dest.name}.ob-lock"


def _acquire_lock(dest: Path):
    """Return ``(lock_path, error)``. O_CREAT|O_EXCL is the atomic claim.

    The mtime/size re-check in _atomic_rewrite alone does NOT make concurrent
    updates safe, and this was measured rather than assumed: with two
    append-update processes launched simultaneously, both read the original,
    both re-checked while it was still original, and both renamed -- 5 trials
    out of 5 lost one writer's update with two rc 0 exits. The uncovered
    window is only the microseconds between the check and the rename, but two
    processes doing identical work march through it in lockstep, so the
    "unlikely" interleaving is in fact the reliable one.

    The stat re-check is kept as well: it catches a writer that does NOT take
    this lock -- most realistically the user saving the note in Obsidian while
    /compress drafts the update -- which no amount of locking between our own
    processes can cover.
    """
    lock = _lock_path(dest)
    for attempt in (0, 1):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # vanished between the two calls -- retry the claim
            if attempt == 0 and age > _STALE_LOCK_SECONDS:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            return None, (
                f"another process is updating this note (lock held at {lock}); "
                "nothing written -- re-run in a moment"
            )
        except OSError as exc:
            return None, f"cannot create lock file {lock}: {exc}"
        else:
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            return lock, None
    return None, f"could not acquire lock {lock}"


def _release_lock(lock):
    if lock is None:
        return
    try:
        Path(lock).unlink()
    except OSError:
        pass


def _atomic_rewrite(dest: Path, content: str, expect_stat=None):
    """Rewrite an EXISTING file at ``dest`` with ``content``, atomically.

    Mirrors obsidian_utils.write_vault_note()'s temp-file + chmod 0o600 +
    rename idiom exactly, adapted to rewriting a known destination in place
    rather than creating one fresh under a vault/folder/filename triple: the
    temp file is created as a sibling of ``dest`` (same directory, so
    ``os.rename`` is an atomic same-filesystem move) and is only renamed
    over ``dest`` after both the write and the chmod succeed. On any
    failure the temp file is unlinked and ``dest`` is left byte-identical --
    the rename is the only step that can touch ``dest``, and it happens
    last.

    ``expect_stat`` is the ``(st_mtime_ns, st_size)`` observed when the
    caller READ ``dest``. Re-checked immediately before the rename: this is
    an unlocked read-modify-write, and ``os.rename`` makes each write atomic
    without doing anything about interleaving -- two concurrent
    ``append-update`` runs both exited 0 while one writer's update section
    and tag vanished entirely. That silent loss is precisely what this CLI
    exists to eliminate, and /compress's prose tells the model not to
    re-read the note afterwards, so nothing else would catch it.

    This narrows the window to the microseconds between the stat and the
    rename rather than closing it -- a lockfile would be needed for that --
    but it converts the common case (two sessions running /compress on the
    same note) from silent loss into a loud, retryable error.

    Returns None on success, a non-empty error string on failure.

    Opens with ``newline=""`` -- ``content`` already carries whatever line
    endings it was built with (see ``run_append_update``'s CRLF handling),
    so no newline translation should happen on the way out either.
    """
    dest_dir = dest.parent
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(dest_dir), prefix=".ob-", suffix=".md.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
            os.chmod(tmp_path, 0o600)
            if expect_stat is not None:
                current = os.stat(str(dest))
                if (current.st_mtime_ns, current.st_size) != expect_stat:
                    raise RuntimeError(
                        "note changed on disk after it was read (concurrent "
                        "update?); nothing written -- re-run to pick up the "
                        "other change"
                    )
            os.rename(tmp_path, str(dest))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        return f"write failed for {dest}: {exc}"
    return None


def _parse_append_update_flags(argv: list[str]):
    """Parse the optional ``--last-updated``/``--add-tags`` flags that
    follow ``<vault_path> <note_path>`` for the ``append-update`` command.

    Returns ``(last_updated, add_tags_csv, error)``. On success ``error`` is
    None and either value may be None if its flag was omitted (both are
    optional -- an omitted flag is a no-op for that mutation, not an
    error). On a parse error the first two values are None and ``error`` is
    a usage-appropriate message.

    ``None`` here means "flag absent" and is the ONLY benign case: a flag
    present with an empty or malformed value is rejected downstream by
    ``_validate_last_updated``/``_validate_tags``, which is why this
    function must keep returning ``None`` (not ``""``) for an absent flag.
    """
    last_updated = None
    add_tags_csv = None
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag == "--last-updated":
            if i + 1 >= len(argv):
                return None, None, "--last-updated requires a value"
            last_updated = argv[i + 1]
            i += 2
        elif flag == "--add-tags":
            if i + 1 >= len(argv):
                return None, None, "--add-tags requires a value"
            add_tags_csv = argv[i + 1]
            i += 2
        else:
            return None, None, f"unknown flag: {flag!r}"
    return last_updated, add_tags_csv, None


def run_append_update(
    vault_path: str,
    note_path: str,
    update_text: str,
    last_updated: str | None = None,
    add_tags_csv: str | None = None,
) -> int:
    """Append ``update_text`` to the existing note at ``note_path`` and, in
    the SAME atomic write, apply frontmatter mutations.

    Insertion point: scans the note body (after the frontmatter's closing
    fence) top-down for the first line matching a trailing metadata marker
    (see ``_TRAILING_MARKERS``/``_SUMMARY_SOURCE_RE``) *outside any fenced
    code block* (see ``_find_insertion_index``) and inserts ``update_text``
    immediately before it, adding a blank line on either side as needed. If
    no marker is found, appends at end of file.

    Content validation (``update_text``, ``last_updated``, ``add_tags_csv``)
    happens before the note is even read — see ``_validate_update_text`` /
    ``_validate_last_updated`` / ``_validate_tags``. Empty content, an empty
    or malformed date, or a tag that would corrupt the frontmatter are all
    errors, not silent no-ops: /compress drops its verification re-read on
    the strength of this command's exit code.

    Frontmatter mutations (both optional, applied via ``_apply_last_updated``
    / ``_apply_add_tags``): replace-or-insert ``last_updated``, and merge new
    tags into the ``tags:`` block. ``date``, ``source_session``,
    ``source_session_note``, and ``type`` are never touched.

    Validates ``vault_path`` (see ``_validate_vault_path``) and path
    containment (``_resolve_note_path``), then parses/mutates entirely in
    memory before calling ``_atomic_rewrite`` -- the single write call. On
    ANY validation, frontmatter-parse, or write failure, prints
    ``ERROR: <reason>`` to stderr, returns 1, and leaves the file on disk
    byte-identical (no write attempt is made until every check upstream of
    it has already succeeded).

    Prints ``OK: <resolved path>`` to stdout and returns 0 on success.

    Line endings: read/written with ``newline=""`` so no universal-newline
    translation happens in either direction, and every line this function
    *adds* -- frontmatter mutations, separators, the update section itself
    -- is emitted using the note's own detected line ending (see
    ``_detect_line_ending``/``_normalize_eol``) rather than a hardcoded
    ``"\\n"``. Without this, a CRLF-authored note would come back out with
    its entire body silently rewritten to LF, which is exactly the kind of
    outside-the-inserted-section change this command promises never to make.
    """
    vault_err = (
        _validate_vault_path(vault_path)
        or _validate_update_text(update_text)
        or _validate_last_updated(last_updated)
    )
    if vault_err:
        print(f"ERROR: {vault_err}", file=sys.stderr)
        return 1

    add_tags, tags_err = _validate_tags(add_tags_csv)
    if tags_err:
        print(f"ERROR: {tags_err}", file=sys.stderr)
        return 1

    resolved, err = _resolve_note_path(vault_path, note_path)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    lock, lock_err = _acquire_lock(resolved)
    if lock_err:
        print(f"ERROR: {lock_err}", file=sys.stderr)
        return 1
    try:
        return _append_update_locked(
            resolved, note_path, update_text, last_updated, add_tags
        )
    finally:
        _release_lock(lock)


def _append_update_locked(
    resolved: Path,
    note_path: str,
    update_text: str,
    last_updated,
    add_tags: list[str],
) -> int:
    """The read-modify-write half of run_append_update, run while holding the
    note's lock. Split out purely so the lock has one obvious scope and one
    release point (the caller's ``finally``)."""
    try:
        with open(resolved, "r", encoding="utf-8", newline="") as fh:
            original_text = fh.read()
        # Snapshot taken AFTER the read, so it reflects exactly the bytes this
        # run is about to mutate. _atomic_rewrite re-checks it before the
        # rename and refuses to clobber a note another process changed in
        # between (see its docstring).
        read_st = os.stat(resolved)
        read_stat = (read_st.st_mtime_ns, read_st.st_size)
    except OSError as exc:
        print(f"ERROR: cannot read {note_path}: {exc}", file=sys.stderr)
        return 1

    eol = _detect_line_ending(original_text)

    lines = _split_lines_lf_crlf(original_text)
    open_fence, fm_lines, close_fence, body_lines, fm_split_err = _split_frontmatter(lines)
    if fm_split_err:
        print(f"ERROR: {fm_split_err}: {note_path}", file=sys.stderr)
        return 1

    # `is not None`, not truthiness: an empty value is a *present* flag with
    # a broken value (already rejected by _validate_last_updated above), not
    # an omitted one.
    if last_updated is not None:
        fm_lines, fm_err = _apply_last_updated(fm_lines, last_updated, eol=eol)
        if fm_err:
            print(f"ERROR: {fm_err}", file=sys.stderr)
            return 1

    fm_lines, tags_merge_err = _apply_add_tags(fm_lines, add_tags, eol=eol)
    if tags_merge_err:
        print(f"ERROR: {tags_merge_err}", file=sys.stderr)
        return 1

    insertion_idx = _find_insertion_index(body_lines)

    prefix = body_lines[:insertion_idx]
    if prefix:
        last_line = prefix[-1]
        if not last_line.endswith(("\n", "\r")):
            # The file has no trailing newline at all (only possible when
            # `prefix` runs all the way to EOF, i.e. no trailing marker was
            # found) -- terminate that last line AND add the blank-line
            # separator (two line breaks total). One eol alone would only
            # terminate the line, leaving the inserted section running
            # straight onto it with no separating blank line.
            prefix = prefix[:-1] + [last_line + eol, eol]
        elif last_line.strip() != "":
            prefix = prefix + [eol]
    elif not close_fence.endswith(("\n", "\r")):
        # Symmetric case: the note is frontmatter ONLY, with no trailing
        # newline, so the closing `---` is the file's last line and carries
        # no terminator. With an empty `prefix` the branch above never runs,
        # and the reassembly below would weld the update heading straight
        # onto the fence (`---## Update (...)`) -- leaving the frontmatter
        # unterminated, so Obsidian loses the note's type and tags, at exit
        # 0. Terminate the fence AND add the blank-line separator.
        close_fence = close_fence + eol + eol

    block_text = _normalize_eol(update_text, eol)
    if not block_text.endswith(eol):
        block_text += eol
    new_body_lines = prefix + [block_text, eol] + body_lines[insertion_idx:]

    new_text = open_fence + "".join(fm_lines) + close_fence + "".join(new_body_lines)

    write_err = _atomic_rewrite(resolved, new_text, expect_stat=read_stat)
    if write_err:
        print(f"ERROR: {write_err}", file=sys.stderr)
        return 1

    print(f"OK: {resolved}")
    return 0


def _oversize_error() -> str:
    """The single ``ERROR:`` line both commands print on oversize stdin.

    Rejecting rather than truncating: a truncated note written under an
    ``OK:`` line loses its tail (e.g. ``## Session Metadata``) with no
    signal, and at /standup's in-place ``--overwrite`` site it would replace
    a complete note with a truncated one.
    """
    return (
        f"ERROR: stdin exceeds the {STDIN_CAP_CHARS}-character cap; "
        "nothing written (split the content or shorten the note)"
    )


def main():
    """CLI entrypoint.

    Usage:
      note_writer.py write <vault_path> <folder> <filename> [--overwrite]
      note_writer.py append-update <vault_path> <note_path> \
[--last-updated YYYY-MM-DD] [--add-tags a,b,c]
    """
    usage = (
        "usage: note_writer.py write <vault_path> <folder> <filename> "
        "[--overwrite]\n"
        "       note_writer.py append-update <vault_path> <note_path> "
        "[--last-updated YYYY-MM-DD] [--add-tags a,b,c]"
    )
    if len(sys.argv) < 2:
        print(usage, file=sys.stderr)
        sys.exit(2)

    cmd = sys.argv[1]
    if cmd == "write":
        positional = []
        overwrite = False
        for arg in sys.argv[2:]:
            if arg == "--overwrite":
                overwrite = True
            elif arg.startswith("--"):
                print(f"ERROR: unknown flag: {arg!r}", file=sys.stderr)
                sys.exit(2)
            else:
                positional.append(arg)
        if len(positional) != 3:
            print(usage, file=sys.stderr)
            sys.exit(2)
        vault_path, folder, filename = positional
        content, oversized = _read_stdin_capped()
        if oversized:
            print(_oversize_error(), file=sys.stderr)
            sys.exit(1)
        sys.exit(run_write(vault_path, folder, filename, content, overwrite=overwrite))

    if cmd == "append-update":
        if len(sys.argv) < 4:
            print(usage, file=sys.stderr)
            sys.exit(2)
        vault_path, note_path = sys.argv[2], sys.argv[3]
        last_updated, add_tags_csv, flag_err = _parse_append_update_flags(sys.argv[4:])
        if flag_err:
            print(f"ERROR: {flag_err}", file=sys.stderr)
            sys.exit(2)
        update_text, oversized = _read_stdin_capped()
        if oversized:
            print(_oversize_error(), file=sys.stderr)
            sys.exit(1)
        sys.exit(
            run_append_update(vault_path, note_path, update_text, last_updated, add_tags_csv)
        )

    print(f"unknown command: {cmd}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
