"""Open item deduplication for Obsidian Brain.

Provides duplicate detection, creation-time prevention, and check-off
cascading for open items across session notes. All matching uses hybrid
distinctive-token + fuzzy-overlap matching. Python stdlib only.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# --- Module-level compiled regexes (computed once at import) ---

_RE_FILE_PATH = re.compile(r'[\w./]+\.(py|md|json|ts|js|tsx|jsx)')
_RE_PR_REF = re.compile(r'#\d+|PR\s+\d+|issue\s+\d+', re.IGNORECASE)
_RE_BRANCH = re.compile(r'(?:feature|release|hotfix)/[\w.-]+')
_RE_VERSION = re.compile(r'v?\d+\.\d+\.\d+')
_RE_MARKDOWN = re.compile(r'`[^`]*`|\*\*([^*]*)\*\*|_([^_]*)_|\[([^\]]*)\]\([^)]*\)')

_STOPWORDS = frozenset({
    'the', 'a', 'an', 'to', 'for', 'in', 'on', 'of', 'and', 'or',
    'but', 'is', 'are', 'was', 'were', 'be', 'not', 'this', 'that',
    'with', 'from', 'by', 'at', 'it', 'as', 'if', 'so', 'do', 'no',
})

_COMPLETION_PHRASES = frozenset({
    'merged', 'shipped', 'fixed', 'released', 'closed', 'removed',
    'implemented', 'deleted', 'done', 'completed',
})


def _strip_markdown(text: str) -> str:
    """Remove backticks, bold, italic, and links. Keep inner text."""
    return _RE_MARKDOWN.sub(lambda m: m.group(1) or m.group(2) or m.group(3) or '', text)


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


def collect_open_items(
    vault_path: str,
    sessions_folder: str,
    project: str,
    max_sessions: int = 10,
    exclude_path: str | None = None,
) -> list[tuple[str, int, str]]:
    """Collect unchecked open items from recent session notes for a project.

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
        if not fname.endswith('.md') or fname.endswith('-snapshot.md'):
            continue

        fpath = os.path.join(sessions_dir, fname)
        if exclude_path and os.path.abspath(fpath) == os.path.abspath(exclude_path):
            continue

        # Single-pass: read file once, check project in frontmatter,
        # then extract open items from ## Open Questions / Next Steps
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            continue

        # Check frontmatter for project match (first 20 lines)
        project_match = False
        for line in lines[:20]:
            if line.strip() == f'project: {project}':
                project_match = True
                break
        if not project_match:
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
    Tier 1 short-circuits: if a distinctive token matches, skip Tier 2.
    """
    cleaned = _strip_markdown(candidate_text)
    candidate_distinctive = _extract_distinctive_tokens(cleaned)
    candidate_tokens = _tokenize(cleaned)

    matches: list[tuple[str, int, str, str]] = []

    for fpath, line_num, item_text in existing_items:
        item_lower = item_text.lower()

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
        if candidate_tokens:
            item_cleaned = _strip_markdown(item_text).lower()
            overlap = sum(1 for t in candidate_tokens if t in item_cleaned)
            if overlap >= threshold:
                matches.append((fpath, line_num, item_text, "fuzzy"))

    return matches
