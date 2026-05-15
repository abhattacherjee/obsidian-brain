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
