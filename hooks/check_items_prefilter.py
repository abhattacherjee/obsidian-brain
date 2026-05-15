"""L2 pre-filter for /check-items Stage 4.

Deterministic triage that classifies items with no plausible completion
evidence as ACTIVE/STALE without dispatching a claude -p sub-agent.
Python stdlib only. Per spec docs/superpowers/specs/2026-05-15-check-items-call-reduction-design.md.
"""
from __future__ import annotations

import re

STOPWORDS = frozenset({
    "the", "and", "or", "for", "of", "to", "in", "is", "a", "an",
    "that", "this", "with", "on", "by", "it", "as", "be", "are",
})

REF_PATTERN = re.compile(r'(?<!\w)#\d+\b|\b[0-9a-f]{7,40}\b')

_WORD_RE = re.compile(r'\w+')


def _content_tokens(text: str) -> list[str]:
    """Extract content-bearing tokens from text. Drops stopwords and tokens shorter than 3 chars."""
    if not text:
        return []
    return [t for t in _WORD_RE.findall(text)
            if t.lower() not in STOPWORDS and len(t) > 2]


def has_classifiable_evidence(group: dict, evidence: dict) -> bool:
    """Return True if the group has any plausible completion evidence.

    Two checks (first match wins):
    1. Explicit issue/PR/commit-sha references in canonical_text — let the
       sub-agent handle anti-conflation (spec § Anti-conflation rule).
    2. Token overlap between canonical_text content tokens and the evidence
       bundle haystack.

    Args:
        group: A group dict with at minimum a 'representative' field containing
               the canonical text. May also carry 'instances' list with 'text'.
        evidence: Dict with optional keys: commits_text, merged_prs_text,
                  closed_issues_text, releases_text, changelog_excerpt,
                  fts_mentions_text. Any missing key is treated as empty string.

    Returns:
        True if the sub-agent should classify this item; False if L2 can
        synthesize a result deterministically.
    """
    canonical_text = group.get("representative") or group.get("canonical_text", "")

    # Check 1: explicit issue/PR/commit-sha reference
    if REF_PATTERN.search(canonical_text):
        return True

    # Check 2: token overlap with evidence bundle
    tokens = _content_tokens(canonical_text)
    if not tokens:
        return False

    haystack = "\n".join([
        evidence.get("commits_text") or "",
        evidence.get("merged_prs_text") or "",
        evidence.get("closed_issues_text") or "",
        evidence.get("releases_text") or "",
        evidence.get("changelog_excerpt") or "",
        evidence.get("fts_mentions_text") or "",
    ]).lower()

    return any(t.lower() in haystack for t in tokens)
