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
import time
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


def _ttl_for(classification: str) -> int:
    # REVIEW is a nudge for human judgement (#264 Task 1) — it must
    # re-evaluate on the same cadence as ACTIVE, not the short DONE/
    # NEEDS-ACTION/STALE cycle, so it maps explicitly to TTL_ACTIVE
    # (matches the default fallback, spelled out here for clarity).
    return {
        "DONE": TTL_DONE,
        "NEEDS-ACTION": TTL_NEEDS_ACTION,
        "STALE": TTL_STALE,
        "REVIEW": TTL_ACTIVE,
    }.get(classification, TTL_ACTIVE)


def _mtime_matches(cached_members: list[dict], current_members: list[dict]) -> bool:
    """True iff all current member mtimes match cached within 1s tolerance."""
    cached_by_key = {(m.get("file"), m.get("line")): m.get("mtime") for m in cached_members}
    for cm in current_members:
        cached_mtime = cached_by_key.get((cm.get("file"), cm.get("line")))
        if cached_mtime is None:
            return False
        try:
            if abs(float(cm.get("mtime", 0)) - float(cached_mtime)) > 1.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def partition(
    groups: list[dict],
    cache: dict,
    project: str,
    head_sha: str,
    force: bool = False,
    now: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Apply invalidation rules in spec order (first match wins):
        force -> new -> head_changed -> mtime_changed -> ttl_expired ->
        heuristic_cached.
    Returns (known_unchanged, needs_reclassification). Groups routed to
    `needs` carry `_reason` for dashboard visibility. Groups routed to
    `known` carry `_cached_classification` / `_cached_confidence` /
    `_cached_evidence_citation` / `_cached_action_required` (NOT `_reason`).
    """
    if now is None:
        now = time.time()
    run = cache.get("runs", {}).get(project, {})
    cached_groups_by_hash = {
        g["canonical_hash"]: g
        for g in run.get("groups", [])
        if isinstance(g, dict) and isinstance(g.get("canonical_hash"), str)
    }
    cached_head = run.get("project_head_at_classify")

    known: list[dict] = []
    needs: list[dict] = []

    for g in groups:
        h = g.get("canonical_hash")
        if force:
            g["_reason"] = "force"
            needs.append(g)
            continue
        cached = cached_groups_by_hash.get(h)
        if cached is None:
            g["_reason"] = "new"
            needs.append(g)
            continue
        if cached_head != head_sha:
            g["_reason"] = "head_changed"
            needs.append(g)
            continue
        if not _mtime_matches(cached.get("members", []), g.get("members", [])):
            g["_reason"] = "mtime_changed"
            needs.append(g)
            continue
        try:
            classified_ts = float(cached.get("classified_ts", 0))
        except (TypeError, ValueError):
            classified_ts = 0.0
        if now - classified_ts > _ttl_for(cached.get("classification", "ACTIVE")):
            g["_reason"] = "ttl_expired"
            needs.append(g)
            continue
        # #297: a cached verdict that was produced by the token-overlap
        # heuristic must never be replayed as `known`. Entries written before
        # #297 carry no classifier_source at all, so fall back to the
        # citation's shape — classify_groups_heuristic always emits
        # "heuristic: token '<t>' near completion phrase '<p>'". Replaying one
        # would let SKILL.md stamp it "cache" (a trusted source) and
        # preselect it for auto-checkoff, which is the exact defect #297 is
        # about. Routing to `needs` re-classifies it with real evidence.
        if (cached.get("classifier_source") == "heuristic"
                or str(cached.get("evidence_citation") or "").startswith("heuristic:")):
            g["_reason"] = "heuristic_cached"
            needs.append(g)
            continue
        g["_cached_classification"] = cached.get("classification")
        g["_cached_confidence"] = cached.get("confidence")
        g["_cached_evidence_citation"] = cached.get("evidence_citation")
        g["_cached_action_required"] = cached.get("action_required")
        known.append(g)

    return known, needs


def update_cache(
    cache: dict,
    project: str,
    all_groups: list[dict],
    fresh_classifications: list[dict],
    head_sha: str,
    now: float | None = None,
) -> dict:
    """
    Merge fresh classifications into the cache and GC entries whose
    canonical_hash is no longer in the current run.

    1. Build a hash set from all_groups (the current run's groups).
    2. Keep cached groups whose hash is in the current set; evict the rest.
    3. Overwrite by canonical_hash with any fresh classifications.
    4. Bump last_run_ts and project_head_at_classify.
    """
    if now is None:
        now = time.time()

    # #297 defect 5: only known-trusted verdicts may be persisted. partition()
    # replays cached verdicts as `known` until the project's HEAD moves, so a
    # single degraded run would poison every later run with token-co-occurrence
    # citations. Allowlist, not denylist: an un-migrated or misspelled source
    # (absent, "", "Heuristic", a future value nobody has audited yet) must be
    # refused rather than silently persisted — the same allowlist-over-denylist
    # reasoning as assign_tier's _HIGH_TRUST_SOURCES cap (open_item_dedup.py).
    # Enforced here rather than only in SKILL.md because skills are advisory
    # and code is not (memory: feedback_skills_advisory_not_enforcement).
    _TRUSTED_SOURCES = {"agent", "prefilter", "cache"}
    _rejected = [fc for fc in (fresh_classifications or [])
                 if fc.get("classifier_source") not in _TRUSTED_SOURCES]
    if _rejected:
        print(
            f"[check-items-cache] refusing to cache {len(_rejected)} "
            f"verdict(s) with untrusted/unknown classifier_source for "
            f"{project}; they will be re-classified on the next run",
            file=sys.stderr,
        )
    _rejected_hashes = {fc.get("canonical_hash") for fc in _rejected}
    fresh_classifications = [fc for fc in (fresh_classifications or [])
                             if fc.get("classifier_source") in _TRUSTED_SOURCES]

    current_hashes = {g.get("canonical_hash") for g in all_groups}
    fresh_by_hash = {fc.get("canonical_hash"): fc for fc in fresh_classifications}

    cache.setdefault("schema_version", SCHEMA_VERSION)
    cache.setdefault("runs", {})
    run = cache["runs"].setdefault(project, {})
    existing_groups = run.get("groups", [])

    surviving: list[dict] = []
    seen: set[str] = set()
    for g in existing_groups:
        # Mirrors partition()'s isinstance(g, dict) filter when it builds
        # cached_groups_by_hash: a corrupt cache file (a stray scalar or
        # list inside `groups`) degrades to a skipped entry rather than
        # crashing the whole cache write with AttributeError.
        if not isinstance(g, dict):
            continue
        h = g.get("canonical_hash")
        if h not in current_hashes:
            continue
        if h in _rejected_hashes:
            # This run could not verify the group — do not let an older entry
            # be revalidated by the unconditional project_head_at_classify bump.
            continue
        if h in fresh_by_hash:
            surviving.append(_freeze_classification(fresh_by_hash[h], now, prior=g))
        else:
            surviving.append(g)
        seen.add(h)

    for h, fc in fresh_by_hash.items():
        if h in seen or h not in current_hashes:
            continue
        # The common case: a brand-new canonical_hash that partition() routed
        # here with _reason "new", so there is no prior entry and `now` is the
        # right stamp. Two rarer routes also land here: a
        # classifier_source="cache" replay whose backing entry vanished between
        # this run's two load_cache() calls (a concurrent run — warned by
        # _freeze_classification's else-branch below), and a hash whose prior
        # entry was just evicted by the #297 _rejected_hashes branch earlier
        # in this function.
        surviving.append(_freeze_classification(fc, now))

    run["groups"] = surviving
    run["last_run_ts"] = int(now)
    run["project_head_at_classify"] = head_sha
    return cache


def _freeze_classification(fc: dict, now: float, prior: dict | None = None) -> dict:
    """Normalize a fresh classification dict into the on-disk cache entry shape.

    `prior` is the existing on-disk cache entry for the same canonical_hash
    (or None if there isn't one).
    """
    # #302: classifier_source == "cache" means this run *replayed* a verdict
    # from partition()'s known-hit path without re-deriving it (Step 6 of
    # check-items/SKILL.md). SKILL.md Step 10 unconditionally stamps
    # classified_ts=int(time.time()) on every record it hands to
    # update_cache, including these replays. If we accepted that stamp here,
    # a replayed verdict's TTL clock would reset on every run that merely
    # *reads* it, so partition()'s ttl_expired check would never fire on a
    # repo whose HEAD is static — measuring "last read" instead of "last
    # verification". Inherit the prior on-disk classified_ts verbatim
    # instead: partition() already tolerates a non-numeric classified_ts by
    # treating it as ancient (see
    # test_partition_handles_non_numeric_classified_ts), so round-tripping a
    # possibly-corrupt prior value is fail-safe, whereas "healing" it by
    # stamping `now` would re-introduce exactly this bug. That tolerance
    # has one gap: a *numeric* bad value (a millisecond-valued stamp, a
    # hand-edited future date) parses fine and is NOT read as ancient, so the
    # clamp below handles the future-dated case explicitly.
    _is_replay = fc.get("classifier_source") == "cache"
    if _is_replay and isinstance(prior, dict) and "classified_ts" in prior:
        classified_ts = prior["classified_ts"]
        # A future-dated inherited stamp is permanently un-expirable:
        # partition()'s `now - classified_ts > ttl` can never be true while the
        # value is ahead of the clock, so a clock-skewed or hand-edited entry
        # would pin the verdict as "known" forever -- the very failure #302
        # exists to prevent. Clamp it to 0 (ancient) rather than to `now`:
        # this run did NOT verify the verdict, so granting it a fresh full TTL
        # would be a bounded restatement of the same "measuring last read, not
        # last verification" bug. 0 forces re-derivation on the next run, which
        # is exactly how partition() already treats a non-numeric stamp.
        # Non-numeric values are deliberately NOT coerced: partition() reads
        # them as ancient already, and comparing a str to a float would raise
        # TypeError and take down the entire cache update.
        try:
            _numeric_ts = float(classified_ts)
        except (TypeError, ValueError):
            _numeric_ts = None
        if _numeric_ts is not None and _numeric_ts > now:
            print(
                f"[check-items-cache] WARNING: cached classified_ts "
                f"({classified_ts}) for {fc.get('canonical_hash')} is in the "
                f"future; treating it as unverified so it re-derives next run",
                file=sys.stderr,
            )
            classified_ts = 0
    else:
        if _is_replay:
            # A replay whose prior entry cannot supply a timestamp -- either no
            # prior at all, or a prior carrying no classified_ts. Both are the
            # same invariant violation: partition() can only have emitted
            # "cache" by finding a usable entry, so reaching here means the
            # cache changed between Step 3's load_cache() and Step 10's (they
            # run in separate processes), or a caller bug. This is the one
            # place the #302 fix silently reverts to re-stamping, so it must
            # not be silent.
            print(
                f"[check-items-cache] WARNING: classifier_source='cache' for "
                f"{fc.get('canonical_hash')} but no prior on-disk "
                f"classified_ts to inherit; stamping now, so its TTL restarts "
                f"(cache changed mid-run?)",
                file=sys.stderr,
            )
        classified_ts = fc.get("classified_ts", int(now))

    return {
        "canonical_hash": fc.get("canonical_hash"),
        "canonical_text": fc.get("canonical_text") or fc.get("representative", ""),
        "members": fc.get("members", []),
        "classification": fc.get("classification"),
        "confidence": fc.get("confidence"),
        "evidence_citation": fc.get("evidence_citation"),
        "action_required": fc.get("action_required"),
        "classified_ts": classified_ts,
        "classifier_source": fc.get("classifier_source"),
    }
