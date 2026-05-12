"""
Cache module for /check-items classifier results.

Per spec section Persistence and cache invalidation (lines 410-494).
Python stdlib only.
"""
from __future__ import annotations

import hashlib
import re

# The following constants and imports are used by Tasks 4-7 (load_cache,
# save_cache, partition, update_cache).  They live here so the module is
# the single authoritative home for cache policy values.
# json, os, sys, tempfile, time, Path, Any — added in Task 4.

SCHEMA_VERSION = 1

TTL_DONE = 86_400          # 24h
TTL_NEEDS_ACTION = 86_400  # 24h
TTL_STALE = 86_400         # 24h
TTL_ACTIVE = 604_800       # 7d


_MARKDOWN_RE = re.compile(r"[*_`~\[\]()#>]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub("", text)


def canonical_hash(text: str) -> str:
    """
    Compute a stable canonical hash for a check-item text string.

    Stable across cosmetic whitespace/markdown edits; busts on real content
    rename.  Uses SHA-256 with whitespace and markdown normalization, then
    truncates to 16 hex chars (64-bit prefix) as the cache key.

    Spec: "Canonical hash" (Persistence and cache invalidation, lines 410-494).
    """
    stripped = _strip_markdown(text or "")
    normalized = _WHITESPACE_RE.sub(" ", stripped).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
