"""Open item deduplication for Obsidian Brain.

Provides duplicate detection, creation-time prevention, and check-off
cascading for open items across session notes. All matching uses hybrid
distinctive-token + fuzzy-overlap matching. Python stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from obsidian_utils import get_workspace_roots

# --- Module-level compiled regexes (computed once at import) ---

_RE_FILE_PATH = re.compile(r'[\w./-]+\.(py|md|json|ts|js|tsx|jsx)')
_RE_PR_REF = re.compile(r'#\d+|PR\s+\d+|issue\s+\d+', re.IGNORECASE)
_RE_BRANCH = re.compile(r'(?:feature|release|hotfix)/[\w.-]+')
_RE_VERSION = re.compile(r'v?\d+\.\d+\.\d+')
_RE_MARKDOWN = re.compile(r'`([^`]*)`|\*\*([^*]*)\*\*|_([^_]*)_|\[([^\]]*)\]\([^)]*\)')

_CHECKBOX_PREFIX_RE = re.compile(r"^\s*-\s+\[[ xX]\]\s+")


def _normalize_item_text(s: str) -> str:
    """Lowercase, strip a leading markdown checkbox/bullet, collapse whitespace."""
    s = _CHECKBOX_PREFIX_RE.sub("", s)          # drop "- [ ] " / "- [x] " prefix if present
    s = re.sub(r"^\s*[-*]\s+", "", s)           # drop a plain bullet too
    return re.sub(r"\s+", " ", s).strip().lower()


def _longest_common_substring_len(a: str, b: str) -> int:
    """Length of the longest contiguous common substring of a and b."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


_ANCHOR_MIN_CHARS = 25  # the proven workaround threshold (#201)
# Above this length we skip the O(n*m) LCS DP entirely and fall back to exact
# equality. A legitimate checkbox anchor never needs a >2000-char common
# substring, and a single long no-space blob line could otherwise stall the
# DP for ~37s per candidate (#201 round-3 review).
_ANCHOR_MAX_CHARS = 2000


def anchor_text_matches(line_text: str, reference_text: str, min_chars: int = _ANCHOR_MIN_CHARS) -> bool:
    """True if line_text and reference_text share a normalized common substring >= min_chars.

    Used to confirm a checkbox line really is the open item we mean to check off,
    rather than a drifted line or quoted prose (#201)."""
    a, b = _normalize_item_text(line_text), _normalize_item_text(reference_text)
    # Long-input short-circuit (checked BEFORE the O(n*m) LCS DP): a legitimate
    # checkbox anchor never needs a >2000-char common substring. Fall back to
    # exact equality so a giant no-space blob can't stall the DP for ~37s.
    if max(len(a), len(b)) > _ANCHOR_MAX_CHARS:
        return a == b and bool(a)
    # short items: require full normalized equality (can't hit the char threshold)
    if min(len(a), len(b)) < min_chars:
        return a == b and bool(a)
    return _longest_common_substring_len(a, b) >= min_chars


def _format_text_verification_skips(
    skips: "list[tuple[str, int]]",
    unit: str,
    reason: str = "failing text verification",
    cap: int = 5,
) -> str:
    """Build a compact 'Skipped N <unit>(s) <reason>: ...' line.

    ``reason`` defaults to the original drift wording so existing callers
    are unaffected; #320 F4/F5 pass distinct wording for the checkbox-gone
    and unverifiable(blank-text) skip classes so a reader can tell drift,
    an already-consumed checkbox, and unreadable stored text apart instead
    of one undifferentiated "failing text verification" bucket.

    Caps the enumerated ``basename:line`` list at ``cap`` entries with a
    ``+N more`` tail so a large drift event can't produce an unbounded
    summary string (#250 Task 3).
    """
    shown = skips[:cap]
    enumerated = ", ".join(f"{os.path.basename(fp)}:{ln}" for fp, ln in shown)
    extra = len(skips) - len(shown)
    tail = f", +{extra} more" if extra > 0 else ""
    return f"Skipped {len(skips)} {unit}(s) {reason}: {enumerated}{tail}"


_RE_SKIPPED_COUNT = re.compile(r"^Skipped (\d+)")
# Matches the exact WRITE FAILED shape built by cascade_group_members /
# batch_cascade_checkoff:
#   f"WRITE FAILED for {len(write_failures)} file(s); "
#   f"{total_lost} verified flip(s) were NOT saved: {names}"
# Group 1 captures the lost-flip count (M), not the file count (N).
_RE_WRITE_FAILED_COUNT = re.compile(
    r"^WRITE FAILED for \d+ file\(s\); (\d+) verified flip\(s\) were NOT saved"
)


def parse_cascade_skipped_total(summary: str) -> int:
    """Sum every skip/loss count out of a cascade summary string.

    Adds each ``Skipped N ...`` line's N and each ``WRITE FAILED for N
    file(s); M verified flip(s) were NOT saved: ...`` line's M (the lost
    verified-flip count). This is the single source of truth for
    skills/check-items/SKILL.md's ``cascade_skipped_total``, whose doc
    (Output format section) promises a sum across every ``Skipped ...``/
    ``WRITE FAILED`` line -- the inline parsing there previously summed
    only ``Skipped`` lines, silently diverging from that doc (#320 R1
    Gemini).
    """
    total = 0
    for line in summary.splitlines():
        sm = _RE_SKIPPED_COUNT.match(line)
        if sm:
            total += int(sm.group(1))
            continue
        wm = _RE_WRITE_FAILED_COUNT.match(line)
        if wm:
            total += int(wm.group(1))
    return total


_STOPWORDS = frozenset({
    'the', 'a', 'an', 'to', 'for', 'in', 'on', 'of', 'and', 'or',
    'but', 'is', 'are', 'was', 'were', 'be', 'not', 'this', 'that',
    'with', 'from', 'by', 'at', 'it', 'as', 'if', 'so', 'do', 'no',
})

# ---------------------------------------------------------------------------
# Confidence tier rules (spec § Confidence tiers, lines 324-332)
# ---------------------------------------------------------------------------

CONFIDENCE_TIER_RULES = {
    "HIGH": {
        "literal_ref_patterns": [
            r"\b[0-9a-f]{7,40}\b",
            r"#\d+",
            r"\bv\d+\.\d+(?:\.\d+)?\b",
        ],
    },
    "MED": {
        "inferred_ref_patterns": [
            r"\bStory\s+\d+(?:\.\d+)*\b",
            r"\bshipped\b",
            r"\bcovered by\b",
            r"#\d+",
        ],
    },
    "LOW": {
        "fts_only_markers": ["FTS mention", "occurrence", "mentions:", "FTS:"],
    },
}

# Sources whose evidence_citation is independent of the item text and may
# therefore reach tier HIGH. Anything else — including an absent or unknown
# source — caps at MED, so a caller that has not been updated to thread
# provenance degrades to "never auto-checked" rather than "always eligible".
# (#297: the denylist form silently failed open.)
_HIGH_TRUST_SOURCES = frozenset({"agent", "prefilter", "cache"})


def _outer_subagent_timeout() -> int:
    """Return the outer subprocess.run timeout that wraps check_items_cli.py.

    Must be ≥ inner SUBAGENT_TIMEOUT_SEC * max-expected-chunks so the outer
    caller never races the inner cli. The inner CLI dispatches sequentially
    over N chunks of <=CLASSIFIER_CHUNK_SIZE groups each, so a payload of 4
    chunks at 300s/chunk needs ~1200s of outer headroom.

    Reads CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC; the env var sets the INNER
    per-chunk timeout. Outer is then `inner * 6` so users only need to tune
    one knob (and 6 covers up to ~150 classifier-eligible groups under the
    default CLASSIFIER_CHUNK_SIZE=25 — well above realistic vault sizes).
    """
    inner = int(os.environ.get("CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC", "300"))
    return inner * 6


def assign_tier(evidence_citation, item_text, classification=None,
                classifier_source=None):
    """Deterministically assign HIGH | MED | LOW from evidence citation shape.

    HIGH requires a literal ref (sha, #N, vX.Y) appearing in BOTH the citation
    and the item text. MED matches an inferred-ref shape in the citation only.
    LOW is the default.

    `classification` is optional and defaults to None (backward compatible
    with existing callers). When classification == "REVIEW", the result is
    capped at MED — a REVIEW item is by definition uncertain (the classifier
    could not confirm completion), so it must never read as HIGH confidence
    even in the edge case where its weak evidence_citation happens to contain
    a literal ref that also appears in item_text (#264 Task 1).

    `classifier_source` is optional and defaults to None (backward compatible
    with existing callers, appended rather than inserted — all 23 pre-#297
    call sites pass 1-3 positional args). See the #297 comment below the cap.

    Spec § Confidence tiers (lines 324-332).
    """
    if not evidence_citation or not item_text:
        return "LOW"
    citation = str(evidence_citation)
    text = str(item_text)

    # #297: a heuristic citation is BUILT FROM a token lifted out of the item
    # text, so "ref appears in both citation and text" is trivially true and
    # says nothing about completion. HIGH is what preselects a DONE item for
    # auto-checkoff, so anything short of a known high-trust source (agent,
    # prefilter, or a cached agent-derived verdict) is capped at MED and can
    # never be auto-checked. Allowlist, not denylist: an un-migrated caller
    # that omits classifier_source degrades safely to MED rather than
    # silently keeping the old unsafe HIGH behaviour. (Production case:
    # token 'v3.4.0' near completion phrase 'release' on an unperformed
    # validation task.)
    _cap_at_med = (classification == "REVIEW"
                   or classifier_source not in _HIGH_TRUST_SOURCES)

    for pattern in CONFIDENCE_TIER_RULES["HIGH"]["literal_ref_patterns"]:
        cit_match = re.search(pattern, citation)
        if not cit_match:
            continue
        ref = cit_match.group(0)
        if ref in text:
            return "MED" if _cap_at_med else "HIGH"

    for pattern in CONFIDENCE_TIER_RULES["MED"]["inferred_ref_patterns"]:
        if re.search(pattern, citation):
            return "MED"

    return "LOW"

_COMPLETION_PHRASES = frozenset({
    'merged', 'shipped', 'fixed', 'released', 'closed', 'removed',
    'implemented', 'deleted', 'done', 'completed',
})


def _strip_markdown(text: str) -> str:
    """Remove backticks, bold, italic, and links. Keep inner text."""
    return _RE_MARKDOWN.sub(lambda m: m.group(1) or m.group(2) or m.group(3) or m.group(4) or '', text)


def _extract_distinctive_tokens(text: str) -> list[str]:
    """Extract file paths, PR refs, branch names, version numbers."""
    tokens = []
    tokens.extend(m.group() for m in _RE_FILE_PATH.finditer(text))
    tokens.extend(m.group() for m in _RE_PR_REF.finditer(text))
    tokens.extend(m.group() for m in _RE_BRANCH.finditer(text))
    tokens.extend(m.group() for m in _RE_VERSION.finditer(text))
    return tokens


def _tokenize(text: str) -> set[str]:
    """Lowercase, split, drop stopwords, keep tokens >= 3 chars."""
    words = re.findall(r'[a-z0-9][-a-z0-9/.#]*[a-z0-9]|[a-z0-9]', text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _safe_mtime(path: str) -> float:
    """Return os.path.getmtime(path), falling back to 0.0 on OSError.

    0.0 (epoch) is the safe-conservative fallback: age = now - 0 is always
    > 90 days, so L2 classifies the item as STALE rather than silently
    treating a stat-failure as ACTIVE. A one-line warning is emitted to
    stderr so the failure is visible in hook logs.
    """
    try:
        return os.path.getmtime(path)
    except OSError as exc:
        print(
            f"[obsidian-brain] mtime unavailable for {path!r}: {exc}; defaulting to 0 (STALE)",
            file=sys.stderr,
        )
        return 0.0


def collect_open_items(
    vault_path: str,
    sessions_folder: str,
    project: str,
    max_sessions: int = 10,
    exclude_path: str | None = None,
) -> list[tuple[str, int, str]]:
    """Collect unchecked open items from recent session notes for a project.

    Filters to `type: claude-session` notes; notes without a `type:` field
    are treated as sessions (legacy). Snapshot notes (`claude-snapshot`) are
    excluded — their "Key context" bullets often look like action items and
    would produce false-positive proposals.

    Returns [(file_path, line_number, item_text)] from the most recent
    max_sessions session notes matching the project. Single-pass per file,
    early termination, no stat() calls.
    """
    sessions_dir = os.path.join(vault_path, sessions_folder)
    if not os.path.isdir(sessions_dir):
        return []

    # listdir + reverse sort = newest first (filenames are YYYY-MM-DD-*)
    all_files = sorted(os.listdir(sessions_dir), reverse=True)

    results: list[tuple[str, int, str]] = []
    matched = 0

    for fname in all_files:
        if not fname.endswith('.md'):
            continue

        fpath = os.path.join(sessions_dir, fname)
        if exclude_path and os.path.abspath(fpath) == os.path.abspath(exclude_path):
            continue

        # Single-pass: read file once, check project in frontmatter,
        # then extract open items from ## Open Questions / Next Steps
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except OSError as exc:
            print(f"[obsidian-brain] skipping unreadable note {fname}: {exc}", file=sys.stderr)
            continue
        except UnicodeDecodeError as exc:
            print(f"[obsidian-brain] encoding error in {fname}: {exc}", file=sys.stderr)
            continue

        # Check frontmatter for project match and type (first 20 lines).
        # Strip quotes to handle both `project: foo` and `project: "foo"`.
        # Notes without a `type:` field are treated as claude-session (legacy).
        project_match = False
        is_session = True  # default for notes with no type field (legacy)
        type_field_seen = False
        for line in lines[:20]:
            stripped = line.strip()
            if stripped.startswith('project:'):
                val = stripped.split(':', 1)[1].strip().strip('"').strip("'")
                if val == project:
                    project_match = True
            elif stripped.startswith('type:'):
                type_field_seen = True
                tval = stripped.split(':', 1)[1].strip().strip('"').strip("'")
                is_session = (tval == 'claude-session')
        if not project_match or (type_field_seen and not is_session):
            continue

        matched += 1

        # Find ## Open Questions / Next Steps and collect - [ ] items
        in_section = False
        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == '## Open Questions / Next Steps':
                in_section = True
                continue
            if in_section:
                if stripped.startswith('## '):
                    break  # next section
                if stripped.startswith('- [ ] '):
                    item_text = stripped[6:]  # after "- [ ] "
                    results.append((fpath, line_num, item_text))

        if matched >= max_sessions:
            break

    return results


def find_duplicates(
    candidate_text: str,
    existing_items: list[tuple[str, int, str]],
    threshold: int = 5,
) -> list[tuple[str, int, str, str]]:
    """Find items in existing_items that are duplicates of candidate_text.

    Returns [(file_path, line_number, item_text, confidence)] where
    confidence is "high" (distinctive token match) or "fuzzy" (token overlap).

    Tier 0: exact text equality after markdown strip + lowercase always yields
    "high" confidence, regardless of distinctive-token presence. This guarantees
    character-identical items (including short or stop-word-heavy ones) always
    cascade correctly.
    Tier 1 short-circuits: if a distinctive token matches, skip Tier 2.
    """
    cleaned = _strip_markdown(candidate_text)
    candidate_distinctive = _extract_distinctive_tokens(cleaned)
    candidate_tokens = _tokenize(cleaned)
    candidate_normalized = cleaned.strip().lower()

    matches: list[tuple[str, int, str, str]] = []

    for fpath, line_num, item_text in existing_items:
        item_lower = item_text.lower()

        # Tier 0: exact text equality after markdown strip + lowercase (always high)
        if _strip_markdown(item_text).strip().lower() == candidate_normalized:
            matches.append((fpath, line_num, item_text, "high"))
            continue

        # Tier 1: distinctive token match (high confidence, short-circuit)
        tier1_hit = False
        for dt in candidate_distinctive:
            if dt.lower() in item_lower:
                matches.append((fpath, line_num, item_text, "high"))
                tier1_hit = True
                break
        if tier1_hit:
            continue

        # Tier 2: fuzzy token overlap (lower confidence)
        # Use set intersection, not substring — avoids "fix" matching "prefix"
        if candidate_tokens:
            item_tokens = _tokenize(_strip_markdown(item_text))
            overlap = len(candidate_tokens & item_tokens)
            if overlap >= threshold:
                matches.append((fpath, line_num, item_text, "fuzzy"))

    return matches


def cascade_checkoff(
    checked_item_text: str,
    existing_items: list[tuple[str, int, str]],
    source_file: str | None = None,
    source_line: int | None = None,
) -> list[tuple[str, int, str, str]]:
    """Find duplicates of a checked-off item for cascading.

    Excludes the source item by (file, line) to avoid self-matching.
    Returns [(file_path, line_number, item_text, confidence)].
    """
    dupes = find_duplicates(checked_item_text, existing_items)
    if source_file is not None and source_line is not None:
        src_abs = os.path.abspath(source_file)
        dupes = [
            (f, l, t, c) for f, l, t, c in dupes
            if not (os.path.abspath(f) == src_abs and l == source_line)
        ]
    return dupes


def dedup_note_open_items(
    vault_path: str,
    sessions_folder: str,
    project: str,
    note_path: str,
) -> list[str]:
    """Remove duplicate open items from a written note. Atomic rewrite.

    Reads note_path, finds - [ ] items in ## Open Questions / Next Steps,
    checks each against existing items in other session notes. Removes
    duplicates and rewrites the file atomically.

    Returns list of removed item texts (empty if no duplicates).
    """
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError as exc:
        print(f"[obsidian-brain] dedup: cannot read {note_path}: {exc}", file=sys.stderr)
        return []

    existing = collect_open_items(
        vault_path, sessions_folder, project,
        max_sessions=10, exclude_path=note_path,
    )
    if not existing:
        return []

    # Find open items section and mark duplicates for removal
    in_section = False
    lines_to_remove: set[int] = set()
    removed_texts: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '## Open Questions / Next Steps':
            in_section = True
            continue
        if in_section:
            if stripped.startswith('## '):
                break
            if stripped.startswith('- [ ] '):
                item_text = stripped[6:]
                dupes = find_duplicates(item_text, existing)
                # Only auto-remove high-confidence matches; fuzzy could be false positives
                high_dupes = [d for d in dupes if d[3] == "high"]
                if high_dupes:
                    lines_to_remove.add(i)
                    removed_texts.append(item_text)

    if not lines_to_remove:
        return []

    # Remove duplicate lines
    new_lines = [line for i, line in enumerate(lines) if i not in lines_to_remove]

    # Atomic rewrite: temp file + rename
    note_dir = os.path.dirname(note_path)
    fd, tmp_path = tempfile.mkstemp(
        prefix='.ob-dedup-', suffix='.md.tmp', dir=note_dir,
    )
    try:
        # Preserve original file permissions
        try:
            orig_mode = os.stat(note_path).st_mode
        except OSError:
            orig_mode = 0o644
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        os.chmod(tmp_path, orig_mode)
        os.replace(tmp_path, note_path)
    except OSError as exc:
        print(f"[obsidian-brain] dedup: atomic write failed for {note_path}: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return []

    return removed_texts


def batch_cascade_checkoff(
    vault_path: str,
    sessions_folder: str,
    project: str,
    checked_texts: list[str],
) -> str:
    """Cascade check-off for multiple items. Edits files directly.

    Collects open items once, finds duplicates for each checked text,
    auto-checks high-confidence matches, reports fuzzy-only suggestions.
    Returns a compact summary string.
    """
    existing = collect_open_items(vault_path, sessions_folder, project)
    if not existing:
        return "No open items found for cascading."

    # Collect all cascade targets, deduped by (file, line)
    high_targets: dict[tuple[str, int], str] = {}  # (file, line) -> item_text
    fuzzy_raw: list[tuple[tuple[str, int], str, str]] = []  # (key, item_text, basename)

    for checked_text in checked_texts:
        dupes = cascade_checkoff(checked_text, existing)
        for fpath, line_num, item_text, confidence in dupes:
            # #320 R1 (Gemini): same line_num hardening as
            # cascade_group_members above -- line_num is a hint carried
            # from find_duplicates/existing_items and is not guaranteed to
            # be a genuine positive int. Reject at the boundary instead of
            # letting a bad value reach `idx = ln - 1` / `lines[idx]` in the
            # write loop below, which raises TypeError mid-loop and can
            # leave a cascade half-applied after earlier files were already
            # committed via os.replace. isinstance(True, int) is True in
            # Python, so bool must be excluded explicitly.
            if not isinstance(line_num, int) or isinstance(line_num, bool) or line_num <= 0:
                print(
                    f"[obsidian-brain] batch_cascade_checkoff: candidate in "
                    f"{os.path.basename(fpath)} has a malformed line number "
                    f"({line_num!r}, type {type(line_num).__name__}); skipping.",
                    file=sys.stderr,
                )
                continue
            key = (fpath, line_num)
            if confidence == "high":
                # #320 F7: item_text is a hint carried from upstream grouping
                # data, not guaranteed to be a string (a corrupted cache
                # entry could hand back an int/list/dict). A non-string
                # reaches .strip() at the flip site below and raises
                # AttributeError mid-loop, which can leave a cascade
                # half-applied after earlier files were already committed
                # via os.replace. Sanitize at the boundary instead: treat
                # anything non-string as blank (unverifiable), same as an
                # empty string.
                high_targets[key] = item_text if isinstance(item_text, str) else ""
            else:
                fuzzy_raw.append((key, item_text, os.path.basename(fpath)))

    # Filter fuzzy suggestions: exclude any that were promoted to high
    fuzzy_suggestions = [
        (text, basename) for key, text, basename in fuzzy_raw
        if key not in high_targets
    ]

    if not high_targets and not fuzzy_suggestions:
        return "No duplicates found for cascading."

    # Edit files for high-confidence targets. Carry each target's stored item
    # text through so the flip site can verify it, not just trust the index
    # (#250 -- verify-don't-re-resolve, mirroring the group-member cascade).
    # Group by file to minimize file rewrites.
    files_to_edit: dict[str, list[tuple[int, str]]] = {}
    for (fpath, line_num), item_text in high_targets.items():
        files_to_edit.setdefault(fpath, []).append((line_num, item_text))

    edited_count = 0
    edited_files: set[str] = set()
    drift_skips: list[tuple[str, int]] = []
    unverifiable_skips: list[tuple[str, int]] = []  # #320 F5: blank/missing text, split from drift
    checkbox_skips: list[tuple[str, int]] = []  # #320 F4: checkbox already gone, was stderr-only
    write_failures: list[tuple[str, int]] = []  # #320 F2: verified flips lost to a failed os.replace

    for fpath, line_refs in files_to_edit.items():
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except OSError as exc:
            print(f"[obsidian-brain] cascade: cannot read {os.path.basename(fpath)}: {exc}", file=sys.stderr)
            continue

        file_edit_count = 0
        for ln, ref_text in line_refs:
            idx = ln - 1  # 0-indexed
            if 0 <= idx < len(lines) and lines[idx].lstrip().startswith('- [ ] '):
                # Checkbox guard passed (unchanged -- this is what prevents
                # prose corruption). Verify the line still IS the item we
                # mean to check off before flipping it: never re-resolve to
                # a different line, only confirm or refuse this one (#250).
                if not ref_text.strip():
                    unverifiable_skips.append((fpath, ln))
                    print(
                        f"[obsidian-brain] cascade: line {ln} in {os.path.basename(fpath)} "
                        f"has no reference text to verify against (unverifiable); skipping.",
                        file=sys.stderr,
                    )
                elif anchor_text_matches(lines[idx].rstrip("\n"), ref_text):
                    lines[idx] = lines[idx].replace('- [ ] ', '- [x] ', 1)
                    file_edit_count += 1
                else:
                    drift_skips.append((fpath, ln))
                    print(
                        f"[obsidian-brain] cascade: line {ln} in {os.path.basename(fpath)} "
                        f"no longer matches the recorded item text (line drifted); skipping.",
                        file=sys.stderr,
                    )
            else:
                # #320 F4: this was stderr-only, which made a renumbered line
                # landing on prose or an already-checked box the most likely
                # real drift signal AND the one least likely to be seen.
                checkbox_skips.append((fpath, ln))
                print(
                    f"[obsidian-brain] cascade: line {ln} in {os.path.basename(fpath)} "
                    f"no longer contains expected checkbox (file may have changed)",
                    file=sys.stderr,
                )

        if file_edit_count > 0:
            note_dir = os.path.dirname(fpath)
            fd, tmp_path = tempfile.mkstemp(
                prefix='.ob-cascade-', suffix='.md.tmp', dir=note_dir,
            )
            try:
                # Preserve original file permissions
                try:
                    orig_mode = os.stat(fpath).st_mode
                except OSError:
                    orig_mode = 0o644
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                os.chmod(tmp_path, orig_mode)
                os.replace(tmp_path, fpath)
                edited_files.add(os.path.basename(fpath))
                edited_count += file_edit_count  # count only after successful write
            except OSError as exc:
                print(f"[obsidian-brain] cascade: write failed for {os.path.basename(fpath)}: {exc}", file=sys.stderr)
                # #320 F2: file_edit_count verified flips were computed but
                # never reached disk. Name the loss instead of letting it
                # silently collapse into "nothing to cascade".
                write_failures.append((fpath, file_edit_count))
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # Build summary. #320 F3: once ANY high-confidence target existed, never
    # collapse "every one of them was refused or lost" down to the same
    # "No duplicates found for cascading." wording used for a genuinely
    # empty run -- that early return above already covers the true empty
    # case. Keep the success wording byte-identical.
    parts: list[str] = []
    if edited_count:
        parts.append(
            f"Cascaded {edited_count} high-confidence duplicate(s) "
            f"in {len(edited_files)} file(s)."
        )
    elif drift_skips or unverifiable_skips or checkbox_skips or write_failures:
        parts.append(
            "Cascaded 0 high-confidence duplicate(s) — "
            "every candidate was refused or failed to save."
        )
    if fuzzy_suggestions:
        parts.append("Fuzzy suggestions (edit manually if same item):")
        seen: set[str] = set()
        for item_text, basename in fuzzy_suggestions:
            key = f"{item_text}|{basename}"
            if key not in seen:
                seen.add(key)
                parts.append(f'  - "{item_text}" in {basename}')
    if drift_skips:
        # #250 Task 3: surface text-verification skips in the RETURN VALUE,
        # not only stderr -- a hook's caller may never show stderr.
        parts.append(_format_text_verification_skips(drift_skips, "item"))
    if unverifiable_skips:
        parts.append(_format_text_verification_skips(
            unverifiable_skips, "item",
            reason="with no text to verify against (unverifiable)",
        ))
    if checkbox_skips:
        parts.append(_format_text_verification_skips(
            checkbox_skips, "item", reason="whose checkbox is gone",
        ))
    if write_failures:
        total_lost = sum(n for _, n in write_failures)
        names = ", ".join(os.path.basename(fp) for fp, _ in write_failures)
        parts.append(
            f"WRITE FAILED for {len(write_failures)} file(s); "
            f"{total_lost} verified flip(s) were NOT saved: {names}"
        )

    return "\n".join(parts) if parts else "No duplicates found for cascading."


def cascade_group_members(
    groups: list,
    source_skips: "set[tuple[str, int]] | None" = None,
) -> str:
    """Flip checkbox on every member of each group, atomically per file.

    Each group dict must contain a ``members`` list of dicts with at least
    ``file`` (full path) and ``line`` keys. Members whose ``(file, line)`` is
    in ``source_skips`` are NOT flipped (caller already primary-flipped them).

    Lines that no longer contain a ``- [ ] `` checkbox at apply-time are
    skipped with a stderr warning (file may have changed since grouping).

    Verify-don't-re-resolve (#250): each target is flipped ONLY when the line
    at its stored index still text-anchors to the member's own stored
    ``text`` (via ``anchor_text_matches``). A member is never re-searched for
    elsewhere in the file -- the index is a hint that is confirmed or
    refused, never re-resolved to a different line. This is deliberately NOT
    #201 Guard B's "unique text match -> act, 2+ -> refuse" contract: a
    cascade group IS a set of near-identical lines by construction, so
    refusing on 2+ matches would refuse exactly the case the cascade exists
    to serve. Verifying each member against its own index instead preserves
    multi-sibling cascades while still closing the drift hole (a member line
    that drifted onto a DIFFERENT still-active checkbox is no longer
    flipped). Blank/missing stored text is UNVERIFIABLE and is skipped, not
    flipped, for the same reason.

    Returns a compact summary string: ``"Cascaded N member-line(s) across M
    file(s)."`` or ``"No member lines to cascade."`` for empty input. Any
    text-verification skips (drifted or unverifiable) are appended as an
    additional line so a hook caller sees them even without stderr.
    """
    if source_skips is None:
        source_skips = set()

    # Collect all (full_path, line_number) -> reference text targets,
    # deduplicated. When the same key appears twice, keep the FIRST
    # non-blank text rather than letting a later blank overwrite a usable
    # anchor (#250).
    targets: dict[tuple[str, int], str] = {}  # ordered dict as ordered map
    for group in groups or []:
        for m in group.get("members", []) or []:
            fpath = m.get("file", "")
            line_num = m.get("line")
            if not fpath:
                continue
            # #320 R1 (Gemini): line_num is a hint carried from merged.json,
            # not guaranteed to be a genuine positive int (a corrupted cache
            # entry could hand back a str/float/bool). `not isinstance(...,
            # int) or isinstance(..., bool) or line_num <= 0` mirrors the
            # non-string `text` sanitization above -- reject at the boundary
            # instead of letting a bad value reach `idx = ln - 1` /
            # `lines[idx]` below, which raises TypeError mid-loop and can
            # leave a cascade half-applied after earlier files were already
            # committed via os.replace. isinstance(True, int) is True in
            # Python, so bool must be excluded explicitly -- a bool must
            # NOT be accepted as a line number (True would silently flip
            # line 0).
            if not isinstance(line_num, int) or isinstance(line_num, bool) or line_num <= 0:
                print(
                    f"[obsidian-brain] cascade_group_members: member in "
                    f"{os.path.basename(fpath)} has a malformed line number "
                    f"({line_num!r}, type {type(line_num).__name__}); skipping.",
                    file=sys.stderr,
                )
                continue
            key = (fpath, line_num)
            if key in source_skips:
                continue
            # #320 F7: a member's "text" is a hint carried from merged.json,
            # not guaranteed to be a string (a corrupted cache entry could
            # hand back an int/list/dict). A non-string reaches .strip() at
            # the flip site below and raises AttributeError mid-loop, which
            # can leave a cascade half-applied after earlier files were
            # already committed via os.replace. Sanitize at the boundary:
            # treat anything non-string as blank (unverifiable).
            text = m.get("text", "")
            text = text if isinstance(text, str) else ""
            if key not in targets:
                targets[key] = text
            elif not targets[key].strip() and text.strip():
                targets[key] = text

    if not targets:
        return "No member lines to cascade."

    # Group by file to minimise rewrites
    files_to_lines: dict[str, list[tuple[int, str]]] = {}
    for (fpath, line_num), ref_text in targets.items():
        files_to_lines.setdefault(fpath, []).append((line_num, ref_text))

    total_flipped = 0
    files_edited: set[str] = set()
    drift_skips: list[tuple[str, int]] = []
    unverifiable_skips: list[tuple[str, int]] = []  # #320 F5: blank/missing text, split from drift
    checkbox_skips: list[tuple[str, int]] = []  # #320 F4: checkbox already gone, was stderr-only
    write_failures: list[tuple[str, int]] = []  # #320 F2: verified flips lost to a failed os.replace

    for fpath, line_refs in files_to_lines.items():
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            print(
                f"[obsidian-brain] cascade_group_members: cannot read "
                f"{os.path.basename(fpath)}: {exc}",
                file=sys.stderr,
            )
            continue

        file_flipped = 0
        for ln, ref_text in line_refs:
            idx = ln - 1  # 0-indexed
            if 0 <= idx < len(lines) and lines[idx].lstrip().startswith("- [ ] "):
                # Checkbox guard passed (unchanged -- this is what prevents
                # prose corruption, #250's "mode 2"). Verify the line still
                # IS the member we mean to check off before flipping it.
                if not ref_text.strip():
                    unverifiable_skips.append((fpath, ln))
                    print(
                        f"[obsidian-brain] cascade_group_members: line {ln} in "
                        f"{os.path.basename(fpath)} has no reference text to "
                        f"verify against (unverifiable); skipping.",
                        file=sys.stderr,
                    )
                elif anchor_text_matches(lines[idx].rstrip("\n"), ref_text):
                    lines[idx] = lines[idx].replace("- [ ] ", "- [x] ", 1)
                    file_flipped += 1
                else:
                    drift_skips.append((fpath, ln))
                    print(
                        f"[obsidian-brain] cascade_group_members: line {ln} in "
                        f"{os.path.basename(fpath)} no longer matches the "
                        f"grouped item text (line drifted); skipping.",
                        file=sys.stderr,
                    )
            else:
                # #320 F4: this was stderr-only, which made a renumbered line
                # landing on prose or an already-checked box the most likely
                # real drift signal AND the one least likely to be seen.
                checkbox_skips.append((fpath, ln))
                print(
                    f"[obsidian-brain] cascade_group_members: line {ln} in "
                    f"{os.path.basename(fpath)} no longer contains expected "
                    f"checkbox (file may have changed); skipping.",
                    file=sys.stderr,
                )

        if file_flipped == 0:
            continue

        note_dir = os.path.dirname(fpath)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".ob-cascade-", suffix=".md.tmp", dir=note_dir,
        )
        try:
            try:
                orig_mode = os.stat(fpath).st_mode
            except OSError:
                orig_mode = 0o644
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            os.chmod(tmp_path, orig_mode)
            os.replace(tmp_path, fpath)
            files_edited.add(os.path.basename(fpath))
            total_flipped += file_flipped
        except OSError as exc:
            print(
                f"[obsidian-brain] cascade_group_members: write failed for "
                f"{os.path.basename(fpath)}: {exc}",
                file=sys.stderr,
            )
            # #320 F2: file_flipped verified flips were computed but never
            # reached disk. Name the loss instead of letting it silently
            # collapse into "nothing to cascade".
            write_failures.append((fpath, file_flipped))
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # #320 F3: once ANY target existed, never collapse "every one of them
    # was refused or lost" down to the same "No member lines to cascade."
    # wording used for a genuinely empty run -- that early return above
    # already covers the true empty case. Keep the success wording
    # byte-identical.
    if total_flipped:
        base = f"Cascaded {total_flipped} member-line(s) across {len(files_edited)} file(s)."
    elif drift_skips or unverifiable_skips or checkbox_skips or write_failures:
        base = "Cascaded 0 member-line(s) — every candidate was refused or failed to save."
    else:
        base = "No member lines to cascade."

    parts = [base]
    if drift_skips:
        # #250 Task 3: surface text-verification skips in the RETURN VALUE,
        # not only stderr -- a hook's caller may never show stderr.
        parts.append(_format_text_verification_skips(drift_skips, "member-line"))
    if unverifiable_skips:
        parts.append(_format_text_verification_skips(
            unverifiable_skips, "member-line",
            reason="with no text to verify against (unverifiable)",
        ))
    if checkbox_skips:
        parts.append(_format_text_verification_skips(
            checkbox_skips, "member-line", reason="whose checkbox is gone",
        ))
    if write_failures:
        total_lost = sum(n for _, n in write_failures)
        names = ", ".join(os.path.basename(fp) for fp, _ in write_failures)
        parts.append(
            f"WRITE FAILED for {len(write_failures)} file(s); "
            f"{total_lost} verified flip(s) were NOT saved: {names}"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Deep analysis pipeline
# ---------------------------------------------------------------------------

_RE_WIKILINK = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]')

# Module-level cache for deep_analysis_pipeline evidence gathering.
# Key shape: see _cache_key() — 6-tuple (basenames, projects_json, vault_path,
#   sessions_folder, insights_folder, db_path). TTL = 15 minutes.
# Prevents redundant git/gh subprocess calls when /check-items and /standup
# deep mode run back-to-back in the same interpreter process.
# Spec § Open questions / Cache coupling (line 699); Testing test 12 (line 658).
#
# Cache entry format: (timestamp: float, status: str, output_json: str)
# output_json is stored so cache hits can re-write the requested output_path
# (the cache key does not include output_path, so a hit must replay the write).
_PIPELINE_EVIDENCE_CACHE: dict[tuple, tuple[float, str, str]] = {}
_PIPELINE_CACHE_TTL_SEC = 900  # 15 minutes


def _evidence_cache_get(key: tuple, now: float) -> tuple[str, str] | None:
    """Return (status, output_json) if entry exists and is within TTL, else None."""
    entry = _PIPELINE_EVIDENCE_CACHE.get(key)
    if entry is None:
        return None
    ts, status, output_json = entry
    if now - ts > _PIPELINE_CACHE_TTL_SEC:
        del _PIPELINE_EVIDENCE_CACHE[key]
        return None
    return (status, output_json)


def _evidence_cache_put(key: tuple, result: tuple[str, str], now: float) -> None:
    """Store (status, output_json) tuple in the module-level cache."""
    status, output_json = result
    _PIPELINE_EVIDENCE_CACHE[key] = (now, status, output_json)


def _cache_key(basenames, projects_json, vault_path, sessions_folder,
               insights_folder, db_path):
    """Stable hashable key for _PIPELINE_EVIDENCE_CACHE.

    Includes every input that materially affects deep_analysis_pipeline output:
    basenames (sorted to normalize order), projects_json, vault_path,
    sessions_folder, insights_folder, db_path. A change in any field forces
    a fresh subprocess burst.
    """
    basenames_key = tuple(sorted(basenames)) if basenames else ()
    return (basenames_key, projects_json, vault_path, sessions_folder,
            insights_folder, db_path or "")


# Note types excluded from orphan detection (they are aggregation notes)
_ORPHAN_EXCLUDE_TYPES = frozenset({
    'claude-standup', 'claude-emerge', 'claude-retro',
})


def _resolve_project_paths() -> dict[str, str]:
    """Return dict mapping project name -> repo path for local git repos.

    Scans workspace roots from config (or historical defaults) for directories
    containing .git.  Roots are supplied by get_workspace_roots() which reads
    ``workspace_roots`` from obsidian-brain-config.json when present.
    """
    result: dict[str, str] = {}
    scan_dirs = get_workspace_roots()
    for scan_dir in scan_dirs:
        if not os.path.isdir(scan_dir):
            continue
        try:
            for entry in os.listdir(scan_dir):
                full = os.path.join(scan_dir, entry)
                if os.path.isdir(full) and os.path.isdir(os.path.join(full, ".git")):
                    result[entry] = full
        except OSError:
            continue
    return result


# Bounds for #264 Task 2's widened git ground truth (tags, changed_paths).
# Kept small and deterministic — these feed a claude -p prompt payload and an
# L2 evidence-text blob, so an unbounded git dump would blow both budgets.
_MAX_EVIDENCE_TAGS = 20
_MAX_EVIDENCE_CHANGED_PATHS = 200


def deep_analysis_pipeline(
    basenames: list[str],
    projects_json: str,
    output_path: str,
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
    db_path: str | None = None,
) -> str:
    """Single-pass deep analysis: similarity, open items, evidence gathering.

    Returns 'OK:<total_items>:<groups>:<projects_with_evidence>:<projects_without_repo>'.
    Writes structured JSON to output_path (atomic: tempfile + rename).

    15-minute module-level cache keyed on (projects_json, vault_path,
    sessions_folder): when /check-items and /standup deep both invoke
    this in the same process back-to-back, the second call skips all
    git/gh subprocess calls and returns the cached result string.
    Cache helpers _evidence_cache_get/_evidence_cache_put expose the
    cache for targeted unit tests without mocking the full pipeline.
    Spec § Open questions / Cache coupling (line 699);
    Testing test 12 (line 658). Refs #87.
    """
    _ck = _cache_key(basenames, projects_json, vault_path, sessions_folder,
                     insights_folder, db_path)
    _now = time.time()
    _cached = _evidence_cache_get(_ck, _now)
    if _cached is not None:
        # Warm-cache hit: skip all subprocess calls; re-write output_path so the
        # caller always finds a valid file regardless of which path was used on
        # the previous (cold-cache) call (cache key excludes output_path).
        _cached_status, _cached_json = _cached
        try:
            out_dir = os.path.dirname(output_path) or "."
            os.makedirs(out_dir, mode=0o700, exist_ok=True)
            _fd, _tmp = tempfile.mkstemp(prefix=".ob-pipeline-hit-", suffix=".json", dir=out_dir)
            with os.fdopen(_fd, 'w', encoding='utf-8') as _f:
                _f.write(_cached_json)
            os.chmod(_tmp, 0o600)
            os.replace(_tmp, output_path)
        except OSError as _exc:
            print(f"[obsidian-brain] pipeline cache: write failed: {_exc}", file=sys.stderr)
        return _cached_status

    import vault_index

    # 1. Warm vault index
    folders = [sessions_folder, insights_folder]
    try:
        actual_db = vault_index.ensure_index(vault_path, folders, db_path=db_path)
    except Exception as exc:
        return f"ERROR:vault index failed: {exc}"

    # 2. Similarity pass — extract keywords per note, find unlinked similar pairs
    # Build wikilink graph for the window notes
    basename_stems = {os.path.splitext(b)[0] for b in basenames}
    # Map stem -> full path for notes in the window
    stem_to_path: dict[str, str] = {}
    for b in basenames:
        stem = os.path.splitext(b)[0]
        for folder in [sessions_folder, insights_folder]:
            candidate = os.path.join(vault_path, folder, b)
            if os.path.isfile(candidate):
                stem_to_path[stem] = candidate
                break

    # Parse wikilinks from each note
    outgoing_links: dict[str, set[str]] = {}  # stem -> set of linked stems
    note_keywords: dict[str, list[str]] = {}
    for stem, fpath in stem_to_path.items():
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except OSError:
            continue
        links = set(_RE_WIKILINK.findall(content))
        outgoing_links[stem] = links
        note_keywords[stem] = vault_index.extract_keywords(content)

    # Build bidirectional linked set
    linked_pairs: set[frozenset[str]] = set()
    for stem, links in outgoing_links.items():
        for target in links:
            if target in basename_stems:
                linked_pairs.add(frozenset([stem, target]))

    # Find similar unlinked pairs via keyword overlap
    link_suggestions: list[dict] = []
    merge_suggestions: list[dict] = []
    stems_list = sorted(stem_to_path.keys())

    for i, stem_a in enumerate(stems_list):
        kw_a = set(note_keywords.get(stem_a, []))
        if not kw_a:
            continue
        for stem_b in stems_list[i + 1:]:
            kw_b = set(note_keywords.get(stem_b, []))
            if not kw_b:
                continue
            shared = kw_a & kw_b
            if len(shared) < 3:
                continue
            pair = frozenset([stem_a, stem_b])
            if pair in linked_pairs:
                continue  # already linked
            overlap_ratio = len(shared) / min(len(kw_a), len(kw_b))
            entry = {
                "note_a": stem_a,
                "note_b": stem_b,
                "shared_keywords": sorted(shared),
            }
            if overlap_ratio >= 0.7 and len(merge_suggestions) < 5:
                merge_suggestions.append(entry)
            elif len(link_suggestions) < 5:
                link_suggestions.append(entry)

    # 3. Collect open items per project
    try:
        projects: list[str] = json.loads(projects_json) if projects_json else []
    except json.JSONDecodeError as exc:
        return f"ERROR:invalid projects JSON: {exc}"
    all_raw_items: list[tuple[str, int, str]] = []
    all_groups: list[dict] = []

    for project in projects:
        items = collect_open_items(
            vault_path, sessions_folder, project, max_sessions=50,
        )
        all_raw_items.extend(items)

        # Group duplicates: for each item, check against all others
        seen_grouped: set[int] = set()
        for idx, (fpath, line_num, item_text) in enumerate(items):
            if idx in seen_grouped:
                continue
            others = [(f, l, t) for j, (f, l, t) in enumerate(items) if j != idx]
            dupes = find_duplicates(item_text, others)
            if dupes:
                group_members = [{
                    "file": os.path.basename(fpath),
                    "line": line_num,
                    "text": item_text,
                    "mtime": _safe_mtime(fpath),
                }]
                for df, dl, dt, dc in dupes:
                    # Mark dupe indices as seen
                    for j, (f2, l2, t2) in enumerate(items):
                        if os.path.abspath(f2) == os.path.abspath(df) and l2 == dl:
                            seen_grouped.add(j)
                    group_members.append({
                        "file": os.path.basename(df),
                        "line": dl,
                        "text": dt,
                        "confidence": dc,
                        "mtime": _safe_mtime(df),
                    })
                all_groups.append({
                    "project": project,
                    "representative": item_text,
                    "members": group_members,
                })
                seen_grouped.add(idx)

    # 4. Gather evidence per project
    project_paths = _resolve_project_paths()
    evidence: dict[str, dict] = {}
    projects_with_evidence = 0
    projects_without_repo: list[str] = []

    for project in projects:
        repo_path = project_paths.get(project)
        if not repo_path:
            # #318: this used to `continue` in silence. EVERY evidence source
            # below is git-derived, so a repo-less project reaches the
            # classifier with an empty bundle, caps at tier LOW, and can never
            # reach DONE+HIGH (the only combination Step 7 preselects). The
            # run then presents as a normal triage. Name it instead.
            projects_without_repo.append(project)
            print(
                f"[obsidian-brain] {project}: no local git repo — every "
                f"git-derived evidence source is unavailable, so no item in "
                f"this project can reach tier HIGH or classification DONE",
                file=sys.stderr,
            )

        proj_evidence: dict[str, object] = {}

        if repo_path:
            # git log (last 40 commits)
            try:
                proc = subprocess.run(
                    ["git", "log", "--oneline", "-40"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0:
                    proj_evidence["commits"] = proc.stdout.strip().split("\n")[:40]
                else:
                    print(f"[obsidian-brain] git log failed for {project}: {proc.stderr.strip()[:200]}", file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"[obsidian-brain] git log error for {project}: {exc}", file=sys.stderr)

            # git tag --list (#264 Task 2: widen git ground truth). A genuinely-
            # shipped item may be grounded in a release tag rather than a PR/issue
            # title (e.g. the reporter's `feature/pull-to-refresh-v2` case, shipped
            # via tag v2.0.0 with no #N anchor in the checkbox text). Sorted by
            # creation recency and capped/deduped to keep the evidence blob small.
            try:
                proc = subprocess.run(
                    ["git", "tag", "--list", "--sort=-creatordate"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0:
                    _seen_tags: set[str] = set()
                    _tags: list[str] = []
                    for _line in proc.stdout.strip().split("\n"):
                        _line = _line.strip()
                        if not _line or _line in _seen_tags:
                            continue
                        _seen_tags.add(_line)
                        _tags.append(_line)
                        if len(_tags) >= _MAX_EVIDENCE_TAGS:
                            break
                    proj_evidence["tags"] = _tags
                else:
                    print(f"[obsidian-brain] git tag failed for {project}: {proc.stderr.strip()[:200]}", file=sys.stderr)
                    proj_evidence["tags"] = []
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"[obsidian-brain] git tag error for {project}: {exc}", file=sys.stderr)
                proj_evidence["tags"] = []

            # git log --name-only (#264 Task 2: changed file paths on the default
            # branch's recent history). Completion may live in changed paths rather
            # than a PR title (e.g. source/test/doc files landing under a shipped
            # component's directory). Deduped and hard-capped so the evidence blob
            # stays bounded regardless of commit size.
            try:
                proc = subprocess.run(
                    ["git", "log", "--name-only", "--pretty=format:", "-40"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0:
                    _seen_paths: set[str] = set()
                    _paths: list[str] = []
                    for _line in proc.stdout.strip().split("\n"):
                        _line = _line.strip()
                        if not _line or _line in _seen_paths:
                            continue
                        _seen_paths.add(_line)
                        _paths.append(_line)
                        if len(_paths) >= _MAX_EVIDENCE_CHANGED_PATHS:
                            break
                    proj_evidence["changed_paths"] = _paths
                else:
                    print(f"[obsidian-brain] git log --name-only failed for {project}: {proc.stderr.strip()[:200]}", file=sys.stderr)
                    proj_evidence["changed_paths"] = []
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"[obsidian-brain] git log --name-only error for {project}: {exc}", file=sys.stderr)
                proj_evidence["changed_paths"] = []

            # gh release list
            try:
                proc = subprocess.run(
                    ["gh", "release", "list", "--limit", "5"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0:
                    proj_evidence["releases"] = proc.stdout.strip().split("\n")[:5]
                else:
                    print(f"[obsidian-brain] gh release list failed for {project}: {proc.stderr.strip()[:200]}", file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"[obsidian-brain] gh release error for {project}: {exc}", file=sys.stderr)

            # gh pr list --state merged
            try:
                proc = subprocess.run(
                    ["gh", "pr", "list", "--state", "merged", "--limit", "20",
                     "--json", "number,title,mergedAt,url"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0:
                    try:
                        proj_evidence["merged_prs"] = json.loads(proc.stdout)
                    except json.JSONDecodeError as exc:
                        print(f"[obsidian-brain] gh pr list JSON error for {project}: {exc}", file=sys.stderr)
                        proj_evidence["merged_prs"] = []
                else:
                    print(f"[obsidian-brain] gh pr list failed for {project}: {proc.stderr.strip()[:200]}", file=sys.stderr)
                    proj_evidence["merged_prs"] = []
            except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                print(f"[obsidian-brain] gh pr list error for {project}: {exc}", file=sys.stderr)
                proj_evidence["merged_prs"] = []

            # gh issue list --state closed
            try:
                proc = subprocess.run(
                    ["gh", "issue", "list", "--state", "closed", "--limit", "20",
                     "--json", "number,title,closedAt,body,url"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0:
                    try:
                        proj_evidence["closed_issues"] = json.loads(proc.stdout)
                    except json.JSONDecodeError as exc:
                        print(f"[obsidian-brain] gh issue list JSON error for {project}: {exc}", file=sys.stderr)
                        proj_evidence["closed_issues"] = []
                else:
                    print(f"[obsidian-brain] gh issue list failed for {project}: {proc.stderr.strip()[:200]}", file=sys.stderr)
                    proj_evidence["closed_issues"] = []
            except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                print(f"[obsidian-brain] gh issue list error for {project}: {exc}", file=sys.stderr)
                proj_evidence["closed_issues"] = []

            # CHANGELOG.md excerpt
            changelog_path = os.path.join(repo_path, "CHANGELOG.md")
            if os.path.isfile(changelog_path):
                try:
                    with open(changelog_path, 'r', encoding='utf-8', errors='replace') as f:
                        proj_evidence["changelog_excerpt"] = f.read(2000)
                except OSError:
                    pass

            # FTS5 search for each open item scoped to THIS project
            proj_items = [g["representative"] for g in all_groups if g["project"] == project]
            fts_mentions: dict[str, int] = {}
            for item_text in proj_items[:10]:  # cap to avoid excessive queries
                kws = vault_index.extract_keywords(item_text, limit=3)
                if kws:
                    # Pass keywords as space-separated (not "OR"-joined — search_vault
                    # handles tokenization internally; literal "OR" would be a search term)
                    hits = vault_index.search_vault(
                        actual_db, " ".join(kws), project=project, limit=5,
                    )
                    fts_mentions[item_text[:60]] = len(hits)
            if fts_mentions:
                proj_evidence["fts_mentions"] = fts_mentions

        if proj_evidence:
            evidence[project] = proj_evidence
            projects_with_evidence += 1

    if projects and not projects_with_evidence:
        print(
            f"[obsidian-brain] WARNING: 0 of {len(projects)} project(s) "
            f"produced any evidence. This run cannot classify anything as "
            f"DONE; every item will read ACTIVE/REVIEW at tier LOW. This is "
            f"a precondition failure, not a triage result.",
            file=sys.stderr,
        )

    # 5. Build output JSON
    output_data = {
        "link_suggestions": link_suggestions,
        "merge_suggestions": merge_suggestions,
        "items": {
            "total_raw": len(all_raw_items),
            "groups": all_groups,
            "group_count": len(all_groups),
        },
        "evidence": evidence,
        "evidence_gaps": {
            "projects_scanned": len(projects),
            "projects_with_evidence": projects_with_evidence,
            "projects_without_repo": projects_without_repo,
            "all_projects_gapped": bool(projects) and projects_with_evidence == 0,
        },
    }

    # Atomic write: tempfile + rename (ensure dir exists first)
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".ob-pipeline-", suffix=".json", dir=out_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, output_path)
    except OSError as exc:
        print(f"[obsidian-brain] pipeline: write failed: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return f"ERROR:{exc}"

    total = len(all_raw_items)
    groups = len(all_groups)
    _result = f"OK:{total}:{groups}:{projects_with_evidence}:{len(projects_without_repo)}"
    # Cache from in-memory output_data rather than re-reading the file on disk.
    # Re-reading could yield an empty string on a race or disk error, which
    # would make later cache hits silently rewrite output_path with empty JSON.
    try:
        _output_json_str = json.dumps(output_data, indent=2)
    except (TypeError, ValueError) as exc:
        print(f"[obsidian-brain] cache skip: couldn't serialise output_data ({exc})",
              file=sys.stderr)
        return _result
    _evidence_cache_put(_ck, (_result, _output_json_str), _now)
    return _result


def fold_tags_and_paths_into_completion_zone(zone_text: dict, proj_evidence: dict) -> dict:
    """Fold #264 Task 2's `tags` and `changed_paths` evidence sources into the
    COMPLETION-ZONE evidence text consumed by
    check_items_prefilter.has_classifiable_evidence().

    `zone_text` is the flat, `_text`-suffixed dict produced by an evidence
    bridge (e.g. check_items_cli.py's `_bridge_project_evidence()`) for the
    pre-existing evidence sources (commits, merged_prs, closed_issues,
    releases, changelog_excerpt, fts_mentions). `proj_evidence` is the raw
    per-project evidence dict this pipeline builds (see
    `deep_analysis_pipeline`), now additionally carrying `tags` (recent git
    tags) and `changed_paths` (files touched on the default branch's recent
    history).

    Both new sources represent completed / merged-to-default-branch work —
    the same class of signal as `releases_text` / `changelog_excerpt` — so
    they are appended into those two buckets rather than left under keys
    `has_classifiable_evidence()` does not read (it only inspects
    merged_prs_text, closed_issues_text, releases_text, and
    changelog_excerpt for its completion-zone rule). Tags are release-like
    -> `releases_text`; changed paths are prose-adjacent free text ->
    `changelog_excerpt`.

    Returns a NEW dict; never mutates `zone_text` in place (callers may reuse
    or cache it).

    Wired in production via check_items_cli._bridge_project_evidence()
    (Shape A path), which calls this helper before has_classifiable_evidence().
    """
    out = dict(zone_text)

    tags = proj_evidence.get("tags") or []
    if tags:
        tags_blob = " ".join(str(t) for t in tags)
        out["releases_text"] = (str(out.get("releases_text") or "") + " " + tags_blob).strip()

    changed_paths = proj_evidence.get("changed_paths") or []
    if changed_paths:
        paths_blob = " ".join(str(p) for p in changed_paths)
        out["changelog_excerpt"] = (str(out.get("changelog_excerpt") or "") + "\n" + paths_blob).strip()

    return out


def build_deep_presentation(
    pipeline_path: str,
    classifications_path: str,
    basenames_json: str,
    vault_path: str,
    sessions_folder: str,
    insights_folder: str,
) -> str:
    """Build formatted markdown from pipeline JSON + classifications.

    Runs orphan detection (O(N) wikilink scan of window notes, skipping
    standup/emerge types) and builds sections for open item consolidation,
    suggested links, orphaned notes, potential merges, and action prompts.
    """
    # Load pipeline data
    try:
        with open(pipeline_path, 'r', encoding='utf-8') as f:
            pipeline = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return f"Error reading pipeline data: {exc}"

    # Load classifications (may be empty dict)
    try:
        with open(classifications_path, 'r', encoding='utf-8') as f:
            classifications = json.load(f)
    except (OSError, json.JSONDecodeError):
        classifications = {}

    try:
        basenames: list[str] = json.loads(basenames_json) if basenames_json else []
    except json.JSONDecodeError as exc:
        return f"Error parsing basenames JSON: {exc}"

    # --- Orphan detection ---
    # Build set of all stems referenced via wikilinks across window notes
    linked_stems: set[str] = set()
    note_types: dict[str, str] = {}  # stem -> type

    for b in basenames:
        stem = os.path.splitext(b)[0]
        for folder in [sessions_folder, insights_folder]:
            fpath = os.path.join(vault_path, folder, b)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            except OSError:
                continue

            # Extract type from frontmatter (first 20 lines)
            for line in content.split('\n')[:20]:
                stripped = line.strip()
                if stripped.startswith('type:'):
                    note_types[stem] = stripped.split(':', 1)[1].strip().strip('"').strip("'")
                    break

            # Collect all wikilink targets
            for target in _RE_WIKILINK.findall(content):
                linked_stems.add(target)
            break  # found in this folder

    # Find orphans: notes in window not linked by any other note, excluding aggregation types
    all_stems = {os.path.splitext(b)[0] for b in basenames}
    orphans: list[str] = []
    for stem in sorted(all_stems):
        note_type = note_types.get(stem, "")
        if note_type in _ORPHAN_EXCLUDE_TYPES:
            continue
        if stem not in linked_stems:
            orphans.append(stem)

    # --- Build markdown ---
    sections: list[str] = []

    # Open Item Consolidation
    items_data = pipeline.get("items", {})
    groups = items_data.get("groups", [])
    total_raw = items_data.get("total_raw", 0)

    # Use classifications if available (list of dicts with classification/evidence)
    classified_items: list[dict] = []
    if isinstance(classifications, list):
        classified_items = classifications

    if classified_items:
        # Group classified items by classification
        by_class: dict[str, list[dict]] = {}
        for item in classified_items:
            cls = item.get("classification", "UNKNOWN").upper()
            by_class.setdefault(cls, []).append(item)

        class_counts = {k: len(v) for k, v in by_class.items()}
        count_parts = ", ".join(f"**{v}** {k.lower()}" for k, v in sorted(class_counts.items()))
        sections.append(f"## Open Item Consolidation\n\n"
                        f"**{total_raw}** raw items classified: {count_parts}.\n")

        # Render each classification group in priority order
        class_order = _DEEP_CLASS_ORDER
        for cls in class_order:
            items_in_class = by_class.get(cls, [])
            if not items_in_class:
                continue
            sections.append(f"### {cls.title()} ({len(items_in_class)})\n")
            for item in items_in_class:
                canonical = item.get("canonical", "?")
                evidence = item.get("evidence", "")
                project = item.get("project", "")
                instances = item.get("instances", [])
                proj_label = f" ({project})" if project else ""
                sections.append(f"- **{canonical}**{proj_label}")
                if evidence:
                    sections.append(f"  - Evidence: {evidence}")
                if instances:
                    locs = [f"`{inst.get('file', '?')}:{inst.get('line', '?')}`" for inst in instances[:3]]
                    sections.append(f"  - Found in: {', '.join(locs)}")
            sections.append("")

        # Show any remaining unclassified groups
        remaining = {k: v for k, v in by_class.items() if k not in class_order}
        for cls, items_in_class in sorted(remaining.items()):
            sections.append(f"### {cls.title()} ({len(items_in_class)})\n")
            for item in items_in_class:
                canonical = item.get("canonical", "?")
                evidence = item.get("evidence", "")
                sections.append(f"- **{canonical}**")
                if evidence:
                    sections.append(f"  - Evidence: {evidence}")
            sections.append("")
    else:
        # Fallback: show raw pipeline groups without classification labels
        sections.append(f"## Open Item Consolidation\n\n"
                        f"**{total_raw}** raw items, **{len(groups)}** duplicate groups detected.\n")
        if groups:
            for g in groups:
                rep = g.get("representative", "?")
                project = g.get("project", "?")
                members = g.get("members", [])
                sections.append(f"- **{rep}** ({project}) — {len(members)} occurrences")
        sections.append("")

    # Suggested Links
    link_suggestions = pipeline.get("link_suggestions", [])
    if link_suggestions:
        sections.append("## Suggested Links\n")
        for ls in link_suggestions:
            kws = ", ".join(ls.get("shared_keywords", [])[:5])
            sections.append(f"- [[{ls['note_a']}]] ↔ [[{ls['note_b']}]] (shared: {kws})")
        sections.append("")

    # Orphaned Notes
    if orphans:
        sections.append(f"## Orphaned Notes ({len(orphans)})\n")
        sections.append("These notes are not linked from any other note in the window:\n")
        for orphan in orphans:
            sections.append(f"- [[{orphan}]]")
        sections.append("")

    # Potential Insight Merges
    merge_suggestions = pipeline.get("merge_suggestions", [])
    if merge_suggestions:
        sections.append("## Potential Insight Merges\n")
        for ms in merge_suggestions:
            kws = ", ".join(ms.get("shared_keywords", [])[:5])
            sections.append(f"- [[{ms['note_a']}]] + [[{ms['note_b']}]] (shared: {kws})")
        sections.append("")

    # Actions prompt
    sections.append("## Actions\n")
    sections.append("Review the suggestions above and apply as needed:")
    actions: list[str] = []
    if groups:
        actions.append("- [ ] Consolidate duplicate open items")
    if link_suggestions:
        actions.append("- [ ] Add suggested wikilinks")
    if orphans:
        actions.append("- [ ] Review orphaned notes for linking opportunities")
    if merge_suggestions:
        actions.append("- [ ] Consider merging similar insight notes")
    if not actions:
        actions.append("- No actions needed — vault is well-connected!")
    sections.extend(actions)

    return "\n".join(sections)


def cross_project_dedup(groups_by_project):
    """
    Vault-scope dedup that respects project boundaries.

    Input shape: {project_name: [coarse_group_dict, ...]}
    Output shape: flat list of group dicts, project boundary preserved.

    Distinctive tokens like '#534' are keyed by (project, token) so the same
    token in two repos does NOT collide. Within-project grouping is already
    handled by find_duplicates(); this function is a pass-through with the
    project-scoped collision-avoidance guarantee for vault-wide scans.

    Spec § Pipeline architecture Stage 2a, lines 67-72.
    """
    if not groups_by_project:
        return []
    flat = []
    seen_keys = set()
    for project, groups in groups_by_project.items():
        for g in groups:
            key = (project, g.get("group_id") or id(g))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out = dict(g)
            out.setdefault("project", project)
            flat.append(out)
    return flat


def merge_groups_semantically(coarse_groups):
    """
    Stage 2b orchestrator: invoke the semantic-merge sub-agent via
    check_items_cli.run_semantic_merge, validate the response, enforce
    same-project rule, and apply the merge map by folding absorbed group
    members into the canonical group.

    Accepts either a list of groups OR a dict {project: [groups]}.
    Returns the same shape it received.

    On 2 consecutive sub-agent failures, returns coarse_groups unchanged.
    (Token-only fallback marker added in Task 13.)

    Spec §§ Pipeline architecture Stage 2b (lines 73-86) and Merge-pass rules.
    """
    global _LAST_SEMANTIC_MERGE_MODE
    _LAST_SEMANTIC_MERGE_MODE = "ok"

    return_dict_shape = isinstance(coarse_groups, dict)
    if return_dict_shape:
        flat_groups = [g for v in coarse_groups.values() for g in v]
    else:
        flat_groups = list(coarse_groups or [])

    if not flat_groups:
        return coarse_groups

    workdir = _check_items_workdir()
    _unique = f"{os.getpid()}-{time.time_ns()}"
    in_path = workdir / f"semantic-merge-{_unique}.in.json"
    out_path = workdir / f"semantic-merge-{_unique}.out.json"
    payload = {"groups": [
        {
            "group_id": g.get("group_id"),
            "project": g.get("project"),
            "representative": g.get("representative", ""),
            "member_texts": [m.get("text", "") for m in g.get("members", [])],
        }
        for g in flat_groups
    ]}
    payload_str = json.dumps(payload)
    # NOT a stdin cap: this bounds an OUTBOUND payload written for a
    # sub-agent, and it measures real bytes (.encode("utf-8") below), so
    # "BYTES" is accurate here. Renamed off the STDIN_CAP_* family in #275
    # so it is not mistaken for the character-based stdin cap that
    # check_items_cli.py and note_writer.py share.
    _PAYLOAD_CAP_BYTES = 1_000_000
    if len(payload_str.encode("utf-8")) >= _PAYLOAD_CAP_BYTES:
        print(
            f"[check-items] payload {len(payload_str)} bytes >= {_PAYLOAD_CAP_BYTES} cap; "
            f"skipping semantic-merge sub-agent, using token-only.",
            file=sys.stderr,
        )
        _LAST_SEMANTIC_MERGE_MODE = "token-only (payload too large)"
        for p in (in_path, out_path):
            try:
                p.unlink()
            except OSError:
                pass
        if return_dict_shape:
            return coarse_groups
        return flat_groups

    in_path.write_text(payload_str, encoding="utf-8")
    os.chmod(str(in_path), 0o600)

    cli_path = os.path.join(os.path.dirname(__file__), "check_items_cli.py")
    attempt = 0
    merge_map = None
    while attempt < 2:
        attempt += 1
        try:
            cp = subprocess.run(
                ["python3", cli_path, "semantic_merge", str(out_path)],
                input=in_path.read_text(),
                capture_output=True,
                text=True,
                timeout=_outer_subagent_timeout(),
            )
            if cp.returncode != 0 or not out_path.exists():
                continue
            merge_map = json.loads(out_path.read_text())
            if not isinstance(merge_map, dict) or "merges" not in merge_map:
                merge_map = None
                continue
            break
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            print(f"[check-items] semantic-merge attempt {attempt} failed: {exc}",
                  file=sys.stderr)
            continue

    for p in (in_path, out_path):
        try:
            p.unlink()
        except OSError:
            pass

    if merge_map is None:
        _LAST_SEMANTIC_MERGE_MODE = "token-only (semantic pass failed)"
        if return_dict_shape:
            return coarse_groups
        return flat_groups

    groups_by_id = {g.get("group_id"): dict(g) for g in flat_groups}

    for merge in merge_map.get("merges", []):
        canonical_id = merge.get("canonical_group_id")
        absorbed_ids = merge.get("absorbed_group_ids", [])
        canonical = groups_by_id.get(canonical_id)
        if canonical is None:
            continue
        canonical_project = canonical.get("project")
        for aid in absorbed_ids:
            absorbed = groups_by_id.get(aid)
            if absorbed is None:
                continue
            if absorbed.get("project") != canonical_project:
                print(f"[check-items] dropping cross-project merge "
                      f"{aid} -> {canonical_id}", file=sys.stderr)
                continue
            canonical.setdefault("members", []).extend(absorbed.get("members", []))
            canonical.setdefault("absorbed_reasoning", []).append({
                "absorbed": aid,
                "reasoning": merge.get("reasoning", ""),
            })
            groups_by_id.pop(aid, None)
        groups_by_id[canonical_id] = canonical

    surviving = list(groups_by_id.values())

    if return_dict_shape:
        out = {}
        for g in surviving:
            out.setdefault(g.get("project"), []).append(g)
        return out
    return surviving


def _check_items_workdir():
    """Return the 0o700 workdir under ~/.claude/obsidian-brain."""
    p = Path.home() / ".claude" / "obsidian-brain"
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Semantic merge mode tracker (Task 13)
# ---------------------------------------------------------------------------

_LAST_SEMANTIC_MERGE_MODE = "ok"


def get_last_semantic_merge_mode():
    """Returns 'ok' on last successful merge, or
    'token-only (semantic pass failed)' on fallback."""
    return _LAST_SEMANTIC_MERGE_MODE


# ---------------------------------------------------------------------------
# Stage 4: classify_groups_with_agent orchestrator (Task 16)
# ---------------------------------------------------------------------------

# Priority order for /standup deep's classification sections, and the single
# source of truth for which labels are valid at all (#243).
#
# These MUST be one definition, not two. They were two: class_order in
# build_deep_presentation still listed the pre-#264 labels COMPLETED/REDUNDANT,
# which no classifier has emitted since, so DONE / NEEDS-ACTION / REVIEW matched
# nothing and fell through to the trailing "remaining" branch. That branch is
# not merely unordered — it renders only canonical + evidence, dropping the
# project label and the "Found in `file:line`" provenance that the prioritized
# branch emits. The three buckets a standup is actually read for were the three
# rendered with no way to locate the item.
#
# Deriving the valid set from the ordered tuple makes that drift impossible:
# adding a label here is what makes it valid, so a new label cannot be accepted
# by the validator while being invisible to the renderer. check_items_cli.py
# keeps its own copy of the set for import-independence; a test pins the two
# together.
_DEEP_CLASS_ORDER = ("DONE", "NEEDS-ACTION", "REVIEW", "STALE", "ACTIVE")
_VALID_CLASSIFICATIONS = frozenset(_DEEP_CLASS_ORDER)
_REQUIRED_CLASSIFIER_FIELDS = {
    "group_id", "classification", "confidence",
    "canonical_text", "evidence_citation", "action_required",
}


def _validate_classifier_response(parsed):
    """Return True iff parsed is a list of dicts containing all required fields
    with classification in the valid set."""
    if not isinstance(parsed, list):
        return False
    for item in parsed:
        if not isinstance(item, dict):
            return False
        if not _REQUIRED_CLASSIFIER_FIELDS.issubset(item.keys()):
            return False
        if item.get("classification") not in _VALID_CLASSIFICATIONS:
            return False
    return True


def classify_groups_with_agent(merged_groups, evidence):
    """
    Stage 4 orchestrator: invoke the classifier sub-agent via
    check_items_cli.run_classifier with one retry on schema-validation
    failure. Returns the parsed list on success.

    Sets `_LAST_CLASSIFIER_MODE` to one of three values:
      - 'ok': every input group_id came back classified.
      - 'partial': validation succeeded but some group_ids in
        `merged_groups` are missing from the returned records (a chunked
        run can now succeed partially, #297 defect 2). Returns the
        records that did come back; the caller fills the missing
        group_ids from the heuristic.
      - 'heuristic-fallback': both attempts failed (non-zero return code,
        missing output, or schema validation failure). Returns [].

    Diagnostics for every failed attempt (including the child's captured
    stderr) and the terminal outcome are printed to stderr so a classifier
    failure is never silent (#297 defect 1).

    Spec §§ Pipeline architecture Stage 4 + Expected output.
    """
    global _LAST_CLASSIFIER_MODE
    _LAST_CLASSIFIER_MODE = "ok"

    if not merged_groups:
        return []

    workdir = _check_items_workdir()
    _unique = f"{os.getpid()}-{time.time_ns()}"
    in_path = workdir / f"classify-{_unique}.in.json"
    out_path = workdir / f"classify-{_unique}.out.json"

    payload = {
        "groups": [
            {
                "group_id": g.get("group_id"),
                "project": g.get("project"),
                "representative": g.get("representative", ""),
                "instances": [
                    {
                        "file": m.get("file"),
                        "line": m.get("line"),
                        "text": m.get("text", ""),
                        # Missing mtime → 0 → epoch (1970), which L2 bins as STALE (fail-safe).
                        "mtime": m.get("mtime", 0),
                    }
                    for m in g.get("members", [])
                ],
            }
            for g in merged_groups
        ],
        "evidence": evidence or {},
    }
    payload_str = json.dumps(payload)
    # NOT a stdin cap: this bounds an OUTBOUND payload written for a
    # sub-agent, and it measures real bytes (.encode("utf-8") below), so
    # "BYTES" is accurate here. Renamed off the STDIN_CAP_* family in #275
    # so it is not mistaken for the character-based stdin cap that
    # check_items_cli.py and note_writer.py share.
    _PAYLOAD_CAP_BYTES = 1_000_000
    if len(payload_str.encode("utf-8")) >= _PAYLOAD_CAP_BYTES:
        print(
            f"[check-items] classifier payload {len(payload_str)} bytes >= "
            f"{_PAYLOAD_CAP_BYTES} cap; falling back to heuristic.",
            file=sys.stderr,
        )
        _LAST_CLASSIFIER_MODE = "heuristic-fallback"
        for p in (in_path, out_path):
            try:
                p.unlink()
            except OSError:
                pass
        return []

    in_path.write_text(payload_str, encoding="utf-8")
    os.chmod(str(in_path), 0o600)

    cli_path = os.path.join(os.path.dirname(__file__), "check_items_cli.py")
    parsed = None
    attempt = 0
    while attempt < 2:
        attempt += 1
        try:
            cp = subprocess.run(
                ["python3", cli_path, "classifier", str(out_path)],
                input=in_path.read_text(),
                capture_output=True,
                text=True,
                timeout=_outer_subagent_timeout(),
            )
            if cp.returncode != 0 or not out_path.exists():
                _tail = (cp.stderr or "").strip()[-800:]
                print(
                    f"[check-items] classifier attempt {attempt}/2 failed: "
                    f"rc={cp.returncode} output_written={out_path.exists()}"
                    + (f"; child stderr: {_tail}" if _tail else ""),
                    file=sys.stderr,
                )
                continue
            candidate = json.loads(out_path.read_text())
            # I4: warn early (pre-validation) so the diagnostic is always
            # reachable, even though _validate_classifier_response will still
            # reject the whole response if any non-dict is present.
            if isinstance(candidate, list):
                _non_dict = sum(1 for r in candidate if not isinstance(r, dict))
                if _non_dict:
                    print(
                        f"[check-items] WARN: classifier emitted {_non_dict} non-dict"
                        f" records — dropped",
                        file=sys.stderr,
                    )
            if not _validate_classifier_response(candidate):
                print(
                    f"[check-items] classifier attempt {attempt}/2 failed: "
                    f"schema validation rejected the response (missing "
                    f"required fields or an invalid classification value)",
                    file=sys.stderr,
                )
                continue
            parsed = candidate
            break
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            print(f"[check-items] classifier attempt {attempt} failed: {exc}",
                  file=sys.stderr)
            continue

    for p in (in_path, out_path):
        try:
            p.unlink()
        except OSError:
            pass

    if parsed is None:
        print(
            f"[check-items] classifier FAILED after {attempt} attempt(s); "
            f"falling back to the token-overlap heuristic for all "
            f"{len(merged_groups)} group(s). Heuristic citations are token "
            f"co-occurrence, not evidence — verify before accepting any DONE.",
            file=sys.stderr,
        )
        _LAST_CLASSIFIER_MODE = "heuristic-fallback"
        return []

    _returned_ids = {r.get("group_id") for r in parsed}
    _missing = [g.get("group_id") for g in merged_groups
                if g.get("group_id") not in _returned_ids]
    if _missing:
        # A chunked run can now succeed partially (#297 defect 2). The caller
        # fills exactly these group_ids from the heuristic.
        _LAST_CLASSIFIER_MODE = "partial"
        print(
            f"[check-items] classifier PARTIAL: {len(_missing)} of "
            f"{len(merged_groups)} group(s) were not classified by the agent "
            f"and will fall back to the heuristic.",
            file=sys.stderr,
        )

    # #297: stamp provenance on every record the agent path actually
    # returned, so a downstream heuristic-only cap (assign_tier) and cache
    # refusal (Task 5) can tell an agent verdict from a token-overlap guess.
    # Stamp unconditionally (not setdefault) — the sub-agent's own JSON must
    # never be able to declare its own provenance, which under assign_tier's
    # allowlist would let a payload self-assert into the high-trust set.
    for r in parsed:
        r["classifier_source"] = "prefilter" if r.get("prefiltered") else "agent"

    # Outer telemetry: summary of this classification run.
    # cache_hit is intentionally NOT emitted here — the orchestration layer
    # is SKILL.md prose: Step 3 heredoc owns partition()'s `known` list,
    # Step 6 heredoc owns this function's invocation, and they share state
    # only via partition.json on disk. Threading len(known) through
    # classify_groups_with_agent's signature is invasive; dropping it
    # entirely (emitting '-' from the CLI side) is cleaner than a placeholder.
    #
    # All three counts derive from `parsed` (the output). Non-dict records are
    # detected and warned before _validate_classifier_response() runs (above);
    # because the validator rejects any list containing a non-dict, `parsed`
    # here is guaranteed to contain only dicts — the redundant post-validation
    # drop guard is not needed.
    _prefiltered_count = sum(
        1 for r in parsed if isinstance(r, dict) and r.get("prefiltered")
    )
    _subagent_count = sum(
        1 for r in parsed if isinstance(r, dict) and not r.get("prefiltered")
    )
    _total_classified = _prefiltered_count + _subagent_count
    print(
        f"[check-items] classifier-result: total_classified={_total_classified} "
        f"prefiltered={_prefiltered_count} subagent={_subagent_count}",
        file=sys.stderr,
    )
    return parsed


_LAST_CLASSIFIER_MODE = "ok"


def get_last_classifier_mode():
    """Returns 'ok' if every group was classified, 'partial' if the
    classifier succeeded but some group_ids came back missing (the
    caller must fill those from Task 17's heuristic), or
    'heuristic-fallback' if the classifier failed entirely and Task 17's
    heuristic must be invoked for all groups."""
    return _LAST_CLASSIFIER_MODE


# ---------------------------------------------------------------------------
# Stage 4: classify_groups_heuristic (Task 17 — long-term fallback)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The PHRASE GATE and the TOKEN GATE below sit UPSTREAM of the pair loop in
# _heuristic_member_verdict, and that placement used to make any narrowing of
# either one SILENT BY CONSTRUCTION. The #299 reporting contract covers the
# pair loop: a pair rejected there returns (tok, phr, reason) and the reason
# reaches the citation and stderr. But when a narrowing here suppressed the
# ONLY token or the ONLY completion phrase in a member text, no pair was ever
# built — _heuristic_member_verdict returned (None, None, None), the cue and
# boundary machinery never ran, and the record was ACTIVE with
# evidence_citation None and nothing logged. Indistinguishable from a text that
# simply had no completion language in it.
#
# That was not hypothetical. The trailing compound anchor added to
# _COMPLETION_PHRASE_RE for #299 suppressed the only completion word in "This
# was merged-in with #51", turning a real DONE into exactly such a silent
# ACTIVE; it took a cross-model review to find, because there was no output to
# notice. The particle allowlist below fixed those instances without touching
# the hazard itself, which belongs to the POSITION of these gates rather than
# to their contents: a replay of 3671 live open-item texts (every `- [ ]` line
# in the vault plus every merged.json member text) still showed 21 verdict
# downgrades against pre-#299, of which 2 carried no citation and printed
# nothing.
#
# The PHRASE side of that channel is now CLOSED. When no pair survives the loop
# above, _heuristic_member_verdict re-scans with _LOOSE_COMPLETION_PHRASE_RE —
# the pre-#299 shape, without the compound anchor — and reports whatever the
# anchor swallowed as a _COMPOUND_PHRASE_REASON rejection. Both live silent
# cases became reported rejections and no verdict moved (measured on all three
# corpora: same downgrade counts, 0 upgrades, 0 silent downgrades). A FUTURE
# narrowing of _COMPLETION_PHRASE_RE is therefore self-documenting for free, so
# long as it stays a TIGHTENING of the loose shape rather than an edit to the
# word list the two regexes share.
#
# The TOKEN side is NOT closed. _DISTINCTIVE_TOKEN_RE has no loose counterpart,
# so narrowing it still suppresses pairs without a trace. ANY future narrowing
# of the token gate must therefore be justified against a replay of the live
# corpora, not against intuition or a synthetic fixture. The question to answer
# is not "does this block the shape I have in mind" but "which live texts lose
# their ONLY match, and is each of those genuinely not a completion" — because
# the ones that are will disappear without a trace.
# ---------------------------------------------------------------------------
_DISTINCTIVE_TOKEN_RE = re.compile(
    r"(#\d+|\b[0-9a-f]{7,40}\b|\bv\d+\.\d+(?:\.\d+)?\b)"
)
# Verb particles allowed to follow a completion word across a hyphen. Chosen as
# the particles that form a completion phrasal verb with the words below —
# "merged-in", "merged-into", "closed-out", "fixed-up", "shipped-off",
# "merged-back", "closed-down", "done-over", "seen-through". The particle must
# be the WHOLE final hyphen-segment (`(?![\w-])`), so "release-overhaul" and
# "closed-out-of-scope" stay compound modifiers.
#
# Known limit, accepted: the exception re-admits an IDENTIFIER whose final
# hyphen segment happens to be a particle — `scripts/release-out.sh` and
# `docs/closed-out.md` read as completion words. Requiring the particle to be
# followed by whitespace or sentence punctuation would not fix it (it would
# still admit `release-out.sh`, since the `.` reads as punctuation), and the
# live corpus does not motivate it either way: of 3671 open-item texts exactly
# ONE carries a `<completion-word>-<particle>` shape at all, "a closed-out
# feature work" — an adjectival compound, followed by whitespace, so the
# proposed tightening would admit it too. Both sides of this exception rest on
# synthetic examples; it is not worth narrowing on the evidence available.
#
# `on` is deliberately absent, and what that exclusion actually buys is
# "merged-on main" / "closed-on friday" — a branch or date qualifier rather
# than a completion phrasal verb — staying blocked. It is NOT what blocks the
# live vault's "Development-Complete-on-board": that one is blocked EITHER WAY
# by the segment-exactness lookahead, because its `on` is followed by `-board`
# and so is not the whole final segment. Mutation-verified: adding `on` here
# leaves "Development-Complete-on-board" blocked and breaks only the two
# "merged-on"/"closed-on" assertions in
# test_verb_particle_survives_the_trailing_compound_anchor.
_COMPLETION_PARTICLES = r"in|into|out|up|off|over|back|through|down"

# The trailing anchor is load-bearing, and the leading `\b` deliberately is not
# symmetric with it. A completion word followed by a hyphen is USUALLY part of a
# compound modifier, not a claim: live vault item "the post-release-verification
# DoD item will flip" put 'release' within 120 chars of '#101' and the all-pairs
# sweep turned it into a false DONE — the exact #299 defect class. All 10 live
# member texts carrying a `<completion-word>-<word>` shape are compounds of that
# kind (release-notes, closed-range, release-version-sweep,
# github-release-board-promote, ...), so dropping the anchor is not an option. A
# LEADING hyphen, by contrast, still reads as a claim ("auto-merged",
# "back-merged"), so `\b` stays on the left.
#
# The one exception on the right is a verb PARTICLE. "This was merged-in with
# #51" and "Issue #77 was closed-out last week" are completion claims, and a
# bare `(?![-\w])` rejected both SILENTLY — no other pair in either text
# qualified, so the guard produced an ACTIVE with no rejection to cite.
# `(?!-(?!particle))` reads as: reject a following hyphen UNLESS a particle
# follows it.
_COMPLETION_WORDS = (
    r"done|merged|shipped|closed|fixed|complete[d]?|resolved|release[d]?"
)
_COMPLETION_PHRASE_RE = re.compile(
    r"\b(" + _COMPLETION_WORDS + r")\b"
    r"(?!-(?!(?:" + _COMPLETION_PARTICLES + r")(?![\w-])))",
    re.IGNORECASE,
)
# The pre-#299 phrase shape: the SAME word list, without the trailing compound
# anchor. Never used to reach a DONE — it exists so the anchor cannot suppress
# a completion word silently. Because the two regexes are generated from one
# word list and differ ONLY by that anchor, every occurrence this one matches
# and _COMPLETION_PHRASE_RE does not is, by construction, a completion word
# followed by a non-particle hyphen segment: a hyphenated compound. See
# _compound_phrase_rejection and the block comment above.
_LOOSE_COMPLETION_PHRASE_RE = re.compile(
    r"\b(" + _COMPLETION_WORDS + r")\b", re.IGNORECASE,
)
_HEURISTIC_PROXIMITY_CHARS = 120

# ---------------------------------------------------------------------------
# #299: conditional / forward-looking guard on the heuristic's DONE verdict
#
# Token-near-phrase co-occurrence cannot tell a CLAIM of past completion
# ("Fixed in #51") from a REFERENCE to a completion that has not happened yet
# ("Waiting on #51 to be closed", "Blocked until #64 is resolved"). Both shapes
# put a distinctive token within 120 chars of a completion phrase, so the
# pre-#299 rule called every one of them DONE. Production case from the issue:
#   "Confirm whether a fresh /plugin update on adversarial-review will pull
#    the new version once #51 is resolved"
# The guard therefore looks at what GOVERNS the co-occurrence, in two tiers.
#
# Tier 1 — pending-intent cues. These name an outstanding action by the
# author of the item (waiting/blocking, or verification-intent). If one governs
# the co-occurrence, the item is about *reaching* the completion, whatever the
# tense of the phrase, so the cue alone rejects DONE. "Confirm #51 was merged"
# is an open verification task even though "was merged" is past tense.
#
# Tier 2 — time/condition subordinators. These appear just as often inside
# genuine completion claims ("After the outage we finally fixed #51"), so on
# their own they would cost real DONEs. They reject only when the completion
# phrase is ALSO in a forward-looking form (see _FORWARD_FORM_RE).
#
# Both tiers are searched only in the text to the LEFT of the completion phrase,
# because a cue governs what follows it: "Fixed in #51 — confirm with the team"
# is a completion claim with a follow-up note attached, not a pending item. Cost
# of that scoping: a cue placed after the co-occurrence never fires. That is
# deliberate — it is the difference between narrowing DONE and rejecting it
# everywhere, and an over-corrected heuristic that only ever says ACTIVE is a
# silent failure of its own.
#
# The two tiers differ in how far left they look, and the split is grounded in
# what each kind of cue scopes over:
#   * A tier-1 cue describes the ITEM's own action, so it is searched across the
#     whole enclosing SENTENCE, clause boundaries included. Live vault case:
#     "Monitor when PR #228 merges to main; auto-promote #227 on next release" —
#     'Monitor' sits two clauses before the #227/'release' pair it governs, and
#     clause-clipping let that one through as DONE.
#   * A tier-2 subordinator scopes over its own CLAUSE, so it is clipped at
#     ';'/':' as well. Widened to the sentence, "Fix #99 merged - this work is
#     done; release shipped." style notes start losing real DONEs. The CLAUSE
#     clip covers BOTH halves of the tier-2 test — the cue search and the
#     forward-form search that corroborates it. Clipping only the cue left the
#     lookback anchored at the sentence, so a modal in a PRECEDING clause could
#     corroborate a cue in this one: "It will ship; once #51 merged." rejected
#     a bare-past claim by citing the 'will' from the clause next door. The
#     verdict was reported, not silent, but the reason named corroboration the
#     documented scope excludes, and a reason that cites the wrong evidence is
#     the same class of defect as no reason at all.
# A (token, phrase) pair must itself sit within one sentence to qualify at all —
# "Blocked until #64 is resolved. Fixed #12." pairs #12 with 'Fixed', not with
# the governed 'resolved' in the sentence next door. That gate REPORTS the pairs
# it drops (as a lower-priority rejection than a real cue) instead of dropping
# them silently: it is the one path that can turn a pre-#299 DONE into an ACTIVE
# with no cue to blame, and an unexplained downgrade is the same silent failure
# the guard exists to remove. Precedence: an ungoverned pair (DONE) beats a cue
# rejection, which beats a boundary rejection.
#
# Finally, a rejection recorded against one member survives even when a SIBLING
# member of the same group yields DONE. The group verdict stays DONE, but
# skills/check-items/SKILL.md Step 8 cascades a DONE group's checkoff to every
# member (file, line) it has — so the objection against the member the guard
# judged NOT done has to reach the citation and the log, or the cascade ticks
# that line off on another member's evidence with the reason already deleted.
# The objection therefore names that member's own (file, line) too: the reason
# alone tells an operator that SOMETHING was disputed, but the cascade acts on
# lines, and without the location the only thing quoted is the group
# representative — a different line from the one about to be ticked off.
# ---------------------------------------------------------------------------

# Sentence and clause terminators. Three things here are load-bearing:
#   * `(?=\s|$)` stops "v1.2.0" and "3.4.1" from being split into sentences.
#   * `_ABBREV_LOOKBEHINDS` stops a KNOWN abbreviation's trailing dot from
#     splitting. "Waiting on infra, e.g. #51 to be closed" is one sentence;
#     splitting at "e.g." clipped the tier-1 window so the item read as DONE.
#     Covered set: `e.g.`, `i.e.`, `etc.`, `vs.` — and nothing else. Any other
#     dot, including an initial ("A.B.") or a title ("Dr."), intentionally ENDS
#     the sentence. See the allowlist comment below.
#   * A BLANK line, not a bare newline, terminates a sentence. A wrapped line is
#     one sentence, and treating `\n` as a terminator silently dropped pairs
#     ("Fixed in\n#51") that the pre-#299 code called DONE. Accepted cost: a
#     multi-line member whose lines are separate list items is now read as ONE
#     sentence, so a tier-1 cue reaches across the newline and can reject a pair
#     on the next line. That widening is not reachable on current data — of 1437
#     live member texts, 0 contain a newline at all — so the change is
#     latent-only today in both directions.
# Abbreviations whose trailing dot must NOT end a sentence, as an ALLOWLIST of
# literal spellings rather than a shape. The shape this replaces,
# `(?<![A-Za-z]\.[A-Za-z])`, also matched INITIALS: "Awaiting update from A.B.
# The fix for #51 is merged." collapsed into ONE sentence, so the tier-1 cue
# 'Awaiting' in the first half governed the '#51'/'merged' pair in the second
# and a real completion read ACTIVE. Same discipline as `_HIGH_TRUST_SOURCES`
# and the #297 note on assign_tier: a shape/denylist fails open, an allowlist
# fails closed. Of 43 live member texts carrying an `X.Y.` dot the sampled ones
# are all `e.g.`, so the allowlist keeps the benefit and drops the `Dr.`/
# initials false positive.
#
# It does NOT drop that false positive for `etc.`, and the shortfall is
# deliberate. Unlike the other three entries, `etc.` is commonly SENTENCE-FINAL,
# and there the lookbehind stops it terminating its own sentence, so a tier-1
# cue reaches across into the next one: "Waiting on infra, DBs, etc. Fixed #51."
# reads ACTIVE where the pre-#299 code read DONE. That is the same mechanism as
# the `A.B.` initials false ACTIVE this allowlist was introduced to remove,
# reintroduced for the one entry that commonly ends a sentence. There is no
# clean fix — mid-sentence and sentence-final `etc.` need opposite treatment
# from a regex that can only do one — and dropping `etc.` from the allowlist
# would trade this safe-direction error (the item stays open, WITH a reported
# reason on the citation and in the log) for a false DONE that ticks a live item
# off. Python lookbehinds are fixed-width, hence one per spelling; the
# zero-width `\b` keeps "Netc." from reading as "etc.".
_ABBREV_LOOKBEHINDS = (
    r"(?<!\b[Ee]\.[Gg])(?<!\b[Ii]\.[Ee])"
    r"(?<!\b[Ee][Tt][Cc])(?<!\b[Vv][Ss])"
)
_SENTENCE_BOUNDARY_RE = re.compile(
    r"(?:" + _ABBREV_LOOKBEHINDS + r"[.!?](?=\s|$))|\r?\n[ \t]*\r?\n"
)
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?:" + _ABBREV_LOOKBEHINDS + r"[.;:!?](?=\s|$))|\r?\n[ \t]*\r?\n"
)

# Tier 1 — self-sufficient. Waiting/blocking language plus verification-intent
# verbs. Grounded in the reported failures ("Waiting on #51...", "Blocked until
# #64...", "Check back after v1.2.0...", "Track whether abc1234...", "Confirm
# whether ... once #51 ...") and their immediate inflections. `validate` is in
# the set for the #297 production case documented on assign_tier: token 'v3.4.0'
# near completion phrase 'release' on an *unperformed* validation task.
# `(?<![\w./-])` / `(?![\w./-])` keep the cue anchored to prose. Live vault case:
# "Implement fix in git-flow#21: modify `check-pr-base.py` to allow `release/*`"
# — a bare \b let 'check' match inside the FILENAME and reject the item for a
# reason that has nothing to do with its meaning. A guard that fires for the
# wrong reason is not a guard, it is a coincidence.
#
# The optional `(?:re|cross|double|self)-` prefix exists because the leading
# anchor rejects a preceding '-': without it `re-verify`, `re-check`,
# `cross-check`, `re-confirm` and `self-check` never fired at all, and only the
# hand-spelled `double-?check` worked. The TRAILING `(?![\w./-])` is what
# actually protects `check-pr-base.py`, so the leading '-' was costing those
# cues for nothing.
_PENDING_INTENT_CUE_RE = re.compile(
    r"(?<![\w./-])((?:(?:re|cross|double|self)-)?(?:"
    r"waiting\s+(?:on|for)|waiting|awaiting|await|"
    r"blocked(?:\s+(?:on|by|until))?|blocker|"
    r"pending|"
    r"depends?\s+on|depending\s+on|dependent\s+on|"
    r"confirm|verify|validate|ensure|double-?check|check(?:\s+back)?|"
    r"track|monitor|revisit|follow[\s-]?up"
    r"))(?![\w./-])",
    re.IGNORECASE,
)

# Tier 2 — needs corroboration from _FORWARD_FORM_RE. Anchored like tier 1, but
# with `/` deliberately LEFT OUT of both classes. A slash joins prose
# alternatives at least as often as it joins path segments, and including it
# made the most explicitly conditional phrasing there is match NOTHING: in
# "Bump the cache if/when #51 is merged", 'if' failed the TRAILING anchor and
# 'when' failed the LEADING one, so tier 2 went silent and the item read DONE
# ("when/if", "before/after" the same way).
#
# The asymmetry against tier 1, which keeps `/`, is principled rather than an
# oversight. A tier-1 cue rejects on its own, so a path segment that happens to
# read as a cue ('scripts/verify.sh', 'docs/track') must not fire — that is the
# live case the anchoring was added for. Tier 2 cannot reject on a cue alone;
# it also needs a forward-looking verb form, and _FORWARD_FORM_RE KEEPS `/` in
# its classes. So re-admitting a path segment such as `hooks/after/x` as a
# tier-2 cue costs nothing on its own.
#
# The DOT stays in both classes, and it is the dot — not the slash — that
# protects the case this anchoring exists for: "After `go.get.it`, #123 was
# closed." keeps its DONE because 'get' cannot match inside the dotted
# filename and corroborate 'After'.
#
# Known limit, deliberate: a slash-joined TIER-1 pair ("confirm/verify #51 is
# merged") still matches neither cue, because tier 1 keeps `/` for the reason
# above. Under-firing there yields a false DONE, but the fix is not to drop `/`
# from tier 1 — that would let a filename reject an item outright, which is the
# worse failure and the one already observed on live data.
_CONDITIONAL_CUE_RE = re.compile(
    r"(?<![\w.-])(once|until|till|unless|if|when|whenever|after|before|whether)"
    r"(?![\w.-])",
    re.IGNORECASE,
)

# Forward-looking verb forms: copula/non-finite/modal constructions that put the
# completion in the future or in question ("is resolved", "to be closed",
# "will be merged", "gets merged"). Deliberately EXCLUDES past forms — "was
# fixed", "were closed", "has been merged" are completion claims, and a tense
# test that swept them in would turn "After the outage #51 was fixed" into a
# false ACTIVE. That exclusion is why tense discrimination is not redundant with
# the tier-2 cue list: it is what keeps bare-past claims out of the guard.
_FORWARD_FORM_RE = re.compile(
    r"(?<![\w./-])(be|is|are|isn't|aren't|get|gets|getting|will|won't|would|"
    r"should|shall|can|could|may|might|must|needs?|going\s+to|gonna)"
    r"(?![\w./-])",
    re.IGNORECASE,
)

# How far back from the completion phrase a forward-looking form may sit.
# "will finally be closed" is 3 words; 24 chars covers that. The window is the
# TIGHTER of this distance and the enclosing clause, so it bounds the reach
# WITHIN a clause while the clause start bounds it across clauses — the char
# count alone would still walk past a ';' on a short leading clause.
_FORWARD_FORM_LOOKBACK_CHARS = 24

# Bound on regex matches scanned per member text, so a pathologically long
# member cannot make the pair sweep quadratic-with-a-big-constant.
_HEURISTIC_MAX_MATCHES = 20


def _segment_start(text, pos, boundary_re):
    """Index of the start of the `boundary_re`-delimited segment holding `pos`.

    The scan runs over the FULL text and stops at the first boundary ending
    past `pos`, rather than handing `pos` to finditer as an endpos. An endpos
    makes `$` match there, so a '.' at `pos - 1` reads as a terminator even
    when the real text has no whitespace after it: `_sentence_start("Waiting on
    the fix.#51 is closed", 19)` returned 19 instead of 0. That artifact can
    only ever DROP a pair, never manufacture a DONE — but a dropped pair is now
    a reported rejection rather than a no-op, and any future relaxation of the
    same-sentence gate would turn it into a false-DONE vector.
    """
    start = 0
    for boundary in boundary_re.finditer(text):
        end = boundary.end()
        if end > pos:
            break
        start = end
    return start


def _sentence_start(text, pos):
    """Index of the start of the sentence containing `pos`."""
    return _segment_start(text, pos, _SENTENCE_BOUNDARY_RE)


def _clause_start(text, pos):
    """Index of the start of the clause containing `pos` (';' and ':' split)."""
    return _segment_start(text, pos, _CLAUSE_BOUNDARY_RE)


def _conditional_rejection(text, tok_match, phr_match):
    """Return a human-readable reason when this token/phrase co-occurrence is
    a forward-looking or pending reference rather than a completion claim,
    else None (#299). See the block comment above for the two-tier rule."""
    span_start = min(tok_match.start(), phr_match.start())
    sent_start = _sentence_start(text, span_start)

    # Tier 1: the whole sentence ahead of the phrase, clauses included.
    pending = _PENDING_INTENT_CUE_RE.search(text, sent_start, phr_match.start())
    if pending:
        return (f"pending-intent cue '{pending.group(0).strip()}' governs it "
                f"(the item's own action is still outstanding)")

    # Tier 2: clipped to the clause holding the co-occurrence.
    clause_start = _clause_start(text, span_start)
    conditional = _CONDITIONAL_CUE_RE.search(text, clause_start, phr_match.start())
    if not conditional:
        return None

    # Tier 2 needs a forward-looking verb form immediately before the phrase,
    # and that form is clipped to the same CLAUSE as the cue it corroborates —
    # `clause_start`, not `sent_start`. See the block comment above.
    look_from = max(clause_start,
                    phr_match.start() - _FORWARD_FORM_LOOKBACK_CHARS)
    forward = _FORWARD_FORM_RE.search(text, look_from, phr_match.start())
    if forward:
        return (f"conditional cue '{conditional.group(0).strip()}' + "
                f"forward-looking form '{forward.group(0).strip()}' make it a "
                f"reference to future completion")
    return None


_CROSS_SENTENCE_REASON = (
    "the token and the completion phrase sit in different sentences, so "
    "neither governs the other"
)

# The final clause states the condition that actually routes a text HERE, and
# it is not "no phrase survived the phrase gate". _compound_phrase_rejection is
# reached when the pair LOOP produced nothing — no DONE, no cue rejection, no
# boundary rejection — which is exactly when no surviving phrase sat within
# `_HEURISTIC_PROXIMITY_CHARS` of a token, since every in-range pair lands in
# one of those three. Strict phrases elsewhere in the text can and do survive:
# "Fixed the flaky parser here. <140 chars> the post-release-verification for
# #101" keeps 'Fixed', and the earlier wording called that text's citation a
# lie. Of the 4 compound citations on the live 3671-text corpus, 2 are of that
# shape, so the false clause was printed text, not a constructible edge case.
_COMPOUND_PHRASE_REASON = (
    "that completion word is part of a hyphenated compound modifier rather "
    "than a completion claim, and no phrase that survived the phrase gate "
    "sat within range of a distinctive token"
)

# Both reasons above are compared by VALUE, never by identity, at the two
# bucketing sites in classify_groups_heuristic. `is` happens to work today only
# because each constant object is threaded through the return path unchanged.
# Any value-preserving reconstruction — a round-trip through JSON, a `str()`
# copy, a reason assembled from parts, a helper that returns an equal string —
# yields a DIFFERENT object, and an identity test would then drop the rejection
# into the CUE bucket, inverting the precedence the buckets exist to establish,
# with nothing failing. (Mutation-checked: rebuilding either reason as an
# equal-but-distinct string leaves the suite green under `==` and fails
# test_rejection_kinds_have_a_total_order_across_members under `is`.)
#
# What `==` does NOT buy: if a future change makes a reason genuinely different
# TEXT — interpolating the offending compound, say — neither comparison matches
# and the bucketing has to move to an explicit kind tag rather than to the
# message string. `==` removes the silent failure from the value-preserving
# case; it is not a licence to vary the text.


def _heuristic_member_verdict(text):
    """Scan one member text for a DONE-qualifying co-occurrence (#299).

    Returns (tok_match, phr_match, rejection):
      (tok, phr, None)   - an ungoverned co-occurrence: DONE.
      (tok, phr, reason) - no pair reached DONE: either every qualifying pair
                           was governed by a conditional/pending cue, or the
                           only pairs in range straddled a sentence boundary.
                           Not DONE; `reason` goes on the citation.
      (None, None, None) - no token/phrase pair within the proximity window.

    All (token, phrase) pairs are considered, up to `_HEURISTIC_MAX_MATCHES`
    of each. The pre-#299 code searched once for each and tested that single
    pair, so with the guard bolted on, one governed mention ("Blocked until #64
    is resolved. Fixed #12.") would have suppressed a genuine claim elsewhere
    in the same text purely because of regex match order. Widening to all pairs
    makes the verdict a property of the content instead. The cap is the cost of
    that widening: a qualifying pair past the 20th token or 20th phrase is not
    seen, which can only lose a DONE, never invent one.

    A cue rejection outranks a cross-sentence rejection: the cue says WHY the
    item is still open, while the boundary only says the pair proves nothing
    either way. Below both sits the compound-phrase rejection recovered by
    _compound_phrase_rejection, which says only that the phrase gate left
    nothing in range.
    """
    tokens = list(_DISTINCTIVE_TOKEN_RE.finditer(text))[:_HEURISTIC_MAX_MATCHES]
    phrases = list(_COMPLETION_PHRASE_RE.finditer(text))[:_HEURISTIC_MAX_MATCHES]
    first_rejection = None
    boundary_rejection = None
    for tok_match in tokens:
        for phr_match in phrases:
            if abs(tok_match.start() - phr_match.start()) > _HEURISTIC_PROXIMITY_CHARS:
                continue
            # A pair split across a sentence boundary is not a co-occurrence
            # the guard can reason about: the cue region for one half would
            # reach into the other half's clause (#299). Recorded rather than
            # dropped — this is the one path that downgrades a pre-#299 DONE
            # with no cue to name, so dropping it silently would leave an
            # unexplained ACTIVE with evidence_citation None.
            if (_sentence_start(text, tok_match.start())
                    != _sentence_start(text, phr_match.start())):
                if boundary_rejection is None:
                    boundary_rejection = (tok_match, phr_match,
                                          _CROSS_SENTENCE_REASON)
                continue
            reason = _conditional_rejection(text, tok_match, phr_match)
            if reason is None:
                return tok_match, phr_match, None
            if first_rejection is None:
                first_rejection = (tok_match, phr_match, reason)
    if first_rejection is not None:
        return first_rejection
    if boundary_rejection is not None:
        return boundary_rejection
    return _compound_phrase_rejection(text, tokens)


def _compound_phrase_rejection(text, tokens):
    """Report a pair the PHRASE GATE swallowed, rather than returning silence.

    Reached only when no pair survived the loop above, which is exactly when
    the #299 reporting contract used to produce an ACTIVE carrying
    evidence_citation None and printing nothing (see the block comment on the
    gates). Any pair found HERE is a completion word inside a hyphenated
    compound, by construction rather than by assumption: a loose match that is
    ALSO a strict match was already seen by the loop, which would have returned
    DONE, a cue rejection or a boundary rejection instead of falling through —
    so reaching this point means the trailing compound anchor is the only thing
    that removed it.

    The _HEURISTIC_MAX_MATCHES cap does not open a hole in that argument, which
    is worth spelling out because it is the non-obvious half. The two regexes
    share a word list and differ only by a trailing negative lookahead, so the
    strict matches are a SUBSEQUENCE of the loose ones at identical spans
    (a rejected match cannot shift the scan onto a different span: the leading
    `\b` gives no other start inside the same word). The loose list is
    therefore at least as long, its 20-item truncation cuts no later, and a
    strict match past the strict cap is necessarily past the loose cap too —
    it can never reach this scan to be misreported as a compound. Verified over
    20,000 randomized completion-word/suffix texts: 0 violations of either
    property. A re-test against _COMPLETION_PHRASE_RE here would be dead code.

    Reporting-only. The verdict is ACTIVE either way; the caller ranks this
    below every other rejection precisely because it explains an ABSENCE of
    evidence rather than naming a reason the item is open. What changes is that
    the downgrade stops being invisible.
    """
    loose = list(
        _LOOSE_COMPLETION_PHRASE_RE.finditer(text)
    )[:_HEURISTIC_MAX_MATCHES]
    for tok_match in tokens:
        for phr_match in loose:
            if abs(tok_match.start()
                   - phr_match.start()) <= _HEURISTIC_PROXIMITY_CHARS:
                return tok_match, phr_match, _COMPOUND_PHRASE_REASON
    return None, None, None


def _member_location(member):
    """`" at <file>:<line>"` for a member, or "" when either part is missing.

    Appended to the DONE-DISPUTED citation and log line: Step 8 cascades a DONE
    group's checkoff to every member (file, line), so an operator reading the
    objection needs to know WHICH line is about to be ticked off on a sibling's
    evidence. Members reach the heuristic straight from merged.json, so a
    missing or null file/line is possible; the suffix is dropped rather than
    printing "at None:None".
    """
    file_path = member.get("file")
    line_no = member.get("line")
    if not file_path or line_no is None:
        return ""
    return f" at {file_path}:{line_no}"


def classify_groups_heuristic(merged_groups, evidence):
    """
    Tightened heuristic classifier (long-term fallback).

    Spec § Interim coexistence Patch 2 + memory feedback_check_items_filter_tightening:
    DONE requires BOTH a distinctive token AND a completion phrase within
    +/- 120 chars in the same member text. Otherwise -> ACTIVE.

    #299: that co-occurrence is additionally rejected when a conditional or
    pending-intent cue governs it, so forward-looking items ("Blocked until #64
    is resolved") no longer read as completions. See the block comment above.

    NEEDS-ACTION and STALE are not assigned by the heuristic (they need
    cross-source evidence the sub-agent provides).
    """
    out = []
    for g in merged_groups or []:
        classification = "ACTIVE"
        confidence = "LOW"
        evidence_citation = None
        canonical_text = g.get("representative", "")
        cue_rejection = None
        boundary_rejection = None
        compound_rejection = None
        done_match = None
        # Both guard log lines quote the item, because a bare group_id makes an
        # operator map an opaque id back to an item by hand. Whitespace is
        # collapsed first: the log contract is one line per event, and a
        # representative carrying a newline would split the line in two and
        # break the grep the lines exist for.
        _log_text = " ".join(canonical_text.split())[:80]

        # Every member is scanned even after one yields DONE: a rejection
        # recorded against a LATER member still has to be reported, because
        # SKILL.md Step 8 cascades the group's checkoff to that member's
        # (file, line) too.
        for m in g.get("members", []) or []:
            text = m.get("text", "") or ""
            tok_match, phr_match, reason = _heuristic_member_verdict(text)
            if not tok_match:
                continue
            if reason is None:
                if done_match is None:
                    done_match = (tok_match.group(0), phr_match.group(0))
                continue
            # `==`, not `is`: see the note on the reason constants.
            rejected = (tok_match.group(0), phr_match.group(0), reason,
                        _member_location(m))
            if reason == _CROSS_SENTENCE_REASON:
                if boundary_rejection is None:
                    boundary_rejection = rejected
            elif reason == _COMPOUND_PHRASE_REASON:
                if compound_rejection is None:
                    compound_rejection = rejected
            elif cue_rejection is None:
                cue_rejection = rejected

        # Precedence across members, kept in explicit buckets so it mirrors the
        # within-member contract in _heuristic_member_verdict rather than
        # restating it as a conditional overwrite: ungoverned DONE beats a cue
        # rejection beats a boundary rejection. A cue says WHY the item is
        # open; the boundary only says the pair proves nothing either way; and
        # the compound rejection, last, only says the phrase gate left nothing
        # in range. Choosing first-come across members, as the code used to,
        # made the reported reason depend on member ORDER — the same group with
        # the same two members cited the uninformative boundary reason or the
        # governing cue purely by list position, and that reason is what rides
        # on the citation and, for a DONE group, on the sibling-objection
        # parenthetical.
        rejection = cue_rejection or boundary_rejection or compound_rejection
        # ... but only an OBJECTION reaches the sibling-dispute path below. The
        # two consumers ask different questions. The ACTIVE citation asks "this
        # group was downgraded — why?", and a compound rejection answers that:
        # it names the phrase the gate removed, which is the whole reason the
        # downgrade is not silent. DONE-DISPUTED asks "this line is about to be
        # ticked off on a SIBLING's evidence — did the guard object to it?", and
        # a compound rejection does not object to anything. It is reached only
        # when the member had no in-range (token, surviving-phrase) pair at all
        # — the same evidentiary state as a member with no completion language
        # whatsoever, which raises no alarm. Firing on one and not the other
        # split two indistinguishable absences: members ["Fixed in #51", "Bump
        # the release-notes for #77"] raised DONE-DISPUTED while ["Fixed in
        # #51", "some prose with no token at all"] did not.
        #
        # A boundary rejection DOES belong here, and the line between them is
        # not arbitrary: it is reached only from inside the proximity check, so
        # a real token and a phrase that survived the gate were within range of
        # each other and the guard declined to credit them. That is the guard
        # refusing a verdict on evidence it examined — and it is the one path
        # that turns a DONE the pre-#299 code emitted into an ACTIVE, so a
        # cascade onto such a line is landing on a line this code actively
        # disputed. A cue rejection is stronger still, naming why it is open.
        #
        # Nothing is lost when a compound rejection is dropped here: it exists
        # to explain a DOWNGRADE, and a DONE group was not downgraded.
        disputed = cue_rejection or boundary_rejection

        if done_match is not None:
            classification = "DONE"
            # #297: the heuristic's own token-co-occurrence evidence
            # cannot justify HIGH confidence at any distance —
            # confidence is written to the dashboard and the cache,
            # and a consumer keying off it (rather than the
            # assign_tier-derived tier) must not read it as HIGH.
            confidence = "MED"
            _dtok, _dphr = done_match
            evidence_citation = (
                f"heuristic: token '{_dtok}' "
                f"near completion phrase '{_dphr}'"
            )
            if disputed is not None:
                # The group is DONE on this member's evidence, but another
                # member was judged NOT done. Keep that objection: Step 8
                # cascades the checkoff to every member line of a DONE group,
                # so the rejected line gets ticked off on a sibling's evidence
                # — the human needs to see which line the guard disputed.
                _rtok, _rphr, _rreason, _rwhere = disputed
                evidence_citation += (
                    f" (sibling member rejected{_rwhere}: token '{_rtok}' near "
                    f"completion phrase '{_rphr}', but {_rreason})"
                )
                print(
                    f"[check-items] heuristic-guard DONE-DISPUTED: group "
                    f"{g.get('group_id')} [{_log_text}]: classified DONE, but "
                    f"sibling member rejected{_rwhere} — token '{_rtok}' near "
                    f"'{_rphr}', but {_rreason}",
                    file=sys.stderr,
                )

        if classification == "ACTIVE" and rejection is not None:
            # #299: a rejected DONE must not become an indistinguishable
            # ACTIVE. The reason rides on the record's evidence_citation so
            # classifications.json (and any consumer reading it before
            # partition_for_review) keeps it, and is ALSO logged to stderr
            # because partition_for_review deliberately scrubs
            # evidence_citation on ACTIVE before the dashboard write (spec
            # § Classification semantics line 322) — without the log line the
            # reason would die at that boundary, which is exactly the silent
            # failure this guard exists to avoid.
            #
            # ACTIVE, not REVIEW: REVIEW (#264) means "distinctive open item
            # with a weak POSITIVE signal the classifier could not confirm",
            # and it is routed to the visible review bucket for adjudication.
            # Here the signal is explicitly negative — the guard has a reason
            # to believe the item is not done — so promoting it to a review row
            # would ask the human to re-adjudicate a verdict already decided.
            # The location is deliberately NOT appended here: an all-ACTIVE
            # group ticks nothing off, so there is no line for an operator to
            # go look at, and the group's own canonical text is already on the
            # log line.
            _tok, _phr, _reason, _ = rejection
            evidence_citation = (
                f"heuristic: DONE rejected — token '{_tok}' near completion "
                f"phrase '{_phr}', but {_reason}"
            )
            print(
                f"[check-items] heuristic-guard DONE-REJECTED: group "
                f"{g.get('group_id')} [{_log_text}]: DONE rejected — token "
                f"'{_tok}' near '{_phr}', but {_reason}",
                file=sys.stderr,
            )

        out.append({
            "group_id": g.get("group_id"),
            "classification": classification,
            "confidence": confidence,
            "canonical_text": canonical_text,
            "evidence_citation": evidence_citation,
            "action_required": None,
            "classifier_source": "heuristic",
        })
    return out


# ---------------------------------------------------------------------------
# Stage 5: partition_for_review (Task 18)
# ---------------------------------------------------------------------------

def partition_for_review(classifications, show_all=False):
    """
    Partition classifier output into UX-relevant buckets for Stage 5.

    Returns:
        {
            "review": [...DONE + NEEDS-ACTION + REVIEW (+ STALE if show_all)...],
            "dashboard_only": [...ACTIVE (always) + STALE (default-mode)...]
        }

    ACTIVE entries have evidence_citation forced to None before dashboard
    write, per spec § Classification semantics line 322.

    REVIEW entries (#264 Task 1 — unanchored, distinctive open items the
    classifier cannot confirm as DONE) are always routed to the visible
    `review` bucket, never `dashboard_only`, and — unlike ACTIVE — keep their
    evidence_citation intact: it carries the weak signal (e.g. a similarly-
    named branch/tag/path) the human needs to judge the item.
    """
    review = []
    dashboard_only = []
    for item in classifications or []:
        kind = item.get("classification")
        if kind in ("DONE", "NEEDS-ACTION", "REVIEW"):
            review.append(item)
        elif kind == "STALE":
            if show_all:
                review.append(item)
            else:
                dashboard_only.append(item)
        elif kind == "ACTIVE":
            scrubbed = dict(item)
            scrubbed["evidence_citation"] = None
            dashboard_only.append(scrubbed)
    return {"review": review, "dashboard_only": dashboard_only}


def verify_before_edit(file_path: str, line_number: int, expected_text: str) -> bool:
    """
    Re-read target line and compare against expected text BEFORE flipping
    a checkbox via Edit tool.

    Strips the checkbox prefix (`- [ ]`, `- [x]`, `- [X]`) and surrounding
    whitespace from the file side before comparing to `expected_text`. The
    classifier produces bare canonical text without the prefix; this function
    normalizes the file line to match. Returns False on missing file,
    out-of-range line, or read error.

    Memory feedback_open_item_checkoff_verify_before_edit: verification
    is mandatory before Edit-tool dispatch.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        print(
            f"[check-items] verify_before_edit: cannot read {file_path}: {exc}",
            file=sys.stderr,
        )
        return False
    if not 1 <= line_number <= len(lines):
        print(
            f"[check-items] verify_before_edit: line {line_number} out of range for"
            f" {file_path} ({len(lines)} lines)",
            file=sys.stderr,
        )
        return False
    actual_line = lines[line_number - 1]
    actual = _CHECKBOX_PREFIX_RE.sub("", actual_line).strip()
    expected = (expected_text or "").strip()
    return actual == expected
