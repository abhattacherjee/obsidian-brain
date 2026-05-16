"""L2 pre-filter for /check-items Stage 4.

Deterministic triage that classifies items with no plausible completion
evidence as ACTIVE/STALE without dispatching a claude -p sub-agent.
Python stdlib only. Per spec docs/superpowers/specs/2026-05-15-check-items-call-reduction-design.md.
"""
from __future__ import annotations

import os
import re
import time

STOPWORDS = frozenset({
    "the", "and", "or", "for", "of", "to", "in", "is", "a", "an",
    "that", "this", "with", "on", "by", "it", "as", "be", "are",
})

COMPLETION_SIGNAL_TOKENS = frozenset({
    "done", "shipped", "merged", "closed", "fixed", "resolved",
    "released", "deprecated", "removed", "reverted", "completed",
})

REF_PATTERN = re.compile(r'(?<!\w)#\d+\b|\b[0-9a-f]{7,40}\b')

_WORD_RE = re.compile(r'\w+')


def _content_tokens(text: str) -> list[str]:
    """Extract content-bearing tokens from text. Drops stopwords and tokens shorter than 3 chars."""
    if not text:
        return []
    return [t for t in _WORD_RE.findall(text)
            if t.lower() not in STOPWORDS and len(t) > 2]


def _has_nearby_completion_signal(haystack: str, content_tokens: list[str],
                                  window: int = 120) -> bool:
    """Return True if any completion-signal token appears within `window` chars
    of any content-token hit in `haystack`.

    Both haystack and tokens are matched case-insensitively. Empty inputs
    return False.
    """
    if not haystack or not content_tokens:
        return False
    h = haystack.lower()
    for tok in content_tokens:
        tok_l = tok.lower()
        if not tok_l:
            continue
        idx = 0
        while True:
            i = h.find(tok_l, idx)
            if i == -1:
                break
            lo = max(0, i - window)
            hi = min(len(h), i + len(tok_l) + window)
            slice_ = h[lo:hi]
            if any(sig in slice_ for sig in COMPLETION_SIGNAL_TOKENS):
                return True
            idx = i + 1
    return False


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


_STALE_THRESHOLD_DAYS = 90
_STALE_THRESHOLD_SECS = _STALE_THRESHOLD_DAYS * 86_400


def synthetic_classification(group: dict, now: float | None = None) -> dict:
    """Produce a synthetic classification record for a group with no evidence.

    Decision matrix (per spec § L2 Decision matrix):
    - Item age > 90 days since oldest (smallest) member mtime → STALE / LOW
    - Item age <= 90 days                                     → ACTIVE / LOW

    Age is derived from the oldest (smallest) mtime across the group's
    'instances' list. If mtime is 0 or missing, defaults to 0 (epoch),
    which yields an age larger than any threshold → STALE.

    The returned record shape matches _validate_classifier_payload():
        group_id, classification, confidence, canonical_text,
        evidence_citation, action_required.
    Plus the L2-specific marker: prefiltered=True.
    L2 synthetic records are NOT written to the L1 cache.

    Args:
        group: Group dict with 'group_id', 'representative' (canonical text),
               and 'instances' list. Each instance may carry 'mtime' (float,
               seconds since epoch; threaded from member dicts in classify_groups_with_agent).
        now: Current time as float (seconds since epoch). Defaults to time.time().
             Injectable for deterministic tests.

    Returns:
        Classification dict compatible with the classifier output schema.
    """
    if now is None:
        now = time.time()

    instances = group.get("instances", [])
    mtimes = [float(inst.get("mtime", 0)) for inst in instances]
    earliest_mtime = min(mtimes) if mtimes else 0.0

    age_secs = now - earliest_mtime
    classification = "STALE" if age_secs > _STALE_THRESHOLD_SECS else "ACTIVE"

    canonical_text = group.get("representative") or group.get("canonical_text", "")

    return {
        "group_id": group.get("group_id"),
        "classification": classification,
        "confidence": "LOW",
        "canonical_text": canonical_text,
        "evidence_citation": None,
        "action_required": None,
        "prefiltered": True,
    }


def is_prefilter_enabled() -> bool:
    """Return True unless CHECK_ITEMS_PREFILTER=off (case-insensitive).

    Default: enabled. Set CHECK_ITEMS_PREFILTER=off to skip L2 and route
    all L1-miss items directly to the sub-agent (useful for A/B comparison
    and debugging). L1 cache is unaffected by this flag.
    """
    val = os.environ.get("CHECK_ITEMS_PREFILTER", "on")
    return val.strip().lower() != "off"
