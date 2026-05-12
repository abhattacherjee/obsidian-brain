"""
Cache module for /check-items classifier results.

Per spec section Persistence and cache invalidation (lines 410-494).
Python stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

CACHE_DIR: Path = Path.home() / ".claude" / "obsidian-brain"
CACHE_PATH: Path = CACHE_DIR / "check-items-classifications.json"

# The following constants are used by Tasks 4-7 (load_cache,
# save_cache, partition, update_cache).  They live here so the module is
# the single authoritative home for cache policy values.

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


def _empty_cache() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "runs": {}}


def load_cache() -> dict[str, Any]:
    """
    Load the cache. On corruption or schema-version mismatch, warn to stderr
    and return an empty cache. Never blocks the pipeline.
    """
    if not CACHE_PATH.exists():
        return _empty_cache()
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[check-items-cache] WARNING: cache load failed ({exc}); using empty cache",
              file=sys.stderr)
        return _empty_cache()
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        print(f"[check-items-cache] WARNING: cache schema mismatch; using empty cache",
              file=sys.stderr)
        return _empty_cache()
    data.setdefault("runs", {})
    return data


def save_cache(data: dict[str, Any]) -> None:
    """Atomically write the cache with 0o600 permissions."""
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=str(CACHE_DIR), suffix=".tmp", encoding="utf-8"
    )
    try:
        json.dump(data, tmp, indent=2, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, str(CACHE_PATH))
    os.chmod(str(CACHE_PATH), 0o600)
