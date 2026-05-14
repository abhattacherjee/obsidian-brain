#!/usr/bin/env python3
"""
Manual dev-test merge gate for issue #87 — smarter /check-items.

5 assertions, exit 0 on pass. Executable-plan pattern: this script IS the
plan — no separate prose phase doc needed.

Usage:
    python3 scripts/dev-test/test-issue-87-manual.py

Spec ref: § Testing / Manual smoke test (lines 660-667).

Signature adaptations from plan's hallucinated template to real signatures:
  - collect_open_items(vault_path, sessions_folder, project, max_sessions=50)
    (no window_days; uses filename date sort)
  - find_duplicates(candidate_text, existing_items, threshold=5)
    (per-candidate, not batch; grouping loop is local)
  - partition(groups, cache, project, head_sha, force=False)
    from check_items_cache (not from open_item_dedup)
  - CLASSIFIER_PROMPT / SEMANTIC_MERGE_PROMPT in hooks/check_items_cli.py
  - Dashboard dir: <vault>/claude-dashboards/

Refs #87
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: add hooks/ to sys.path
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _REPO_ROOT / "hooks"
sys.path.insert(0, str(_HOOKS_DIR))

try:
    from open_item_dedup import collect_open_items, find_duplicates, _PIPELINE_EVIDENCE_CACHE
    import check_items_cache as cache_mod
    from check_items_cache import partition, load_cache
    from check_items_cli import CLASSIFIER_PROMPT, SEMANTIC_MERGE_PROMPT
except ImportError as exc:
    print(f"ERROR: import failed: {exc}", file=sys.stderr)
    print("Run from repo root or with hooks/ in PYTHONPATH.", file=sys.stderr)
    sys.exit(2)

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path.home() / ".claude" / "obsidian-brain-config.json"
if not _CONFIG_PATH.exists():
    print(f"ERROR: config not found at {_CONFIG_PATH}", file=sys.stderr)
    sys.exit(2)

_CONFIG = json.loads(_CONFIG_PATH.read_text())
VAULT_PATH = _CONFIG["vault_path"]
SESSIONS_FOLDER = _CONFIG["sessions_folder"]
INSIGHTS_FOLDER = _CONFIG.get("insights_folder", "claude-insights")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAILURES: list[str] = []
_PASSED = 0


def _pass(label: str) -> None:
    global _PASSED
    _PASSED += 1
    print(f"  PASS  [{_PASSED}] {label}")


def _fail(label: str, detail: str) -> None:
    _FAILURES.append(f"[{len(_FAILURES) + 1}] {label}: {detail}")
    print(f"  FAIL  {label}: {detail}")


def _group_items(raw_items):
    """Mirror Stage 2 grouping logic from deep_analysis_pipeline internals."""
    seen_grouped: set[int] = set()
    groups: list[dict] = []
    for idx, (fpath, line_num, item_text) in enumerate(raw_items):
        if idx in seen_grouped:
            continue
        others = [(f, l, t) for j, (f, l, t) in enumerate(raw_items) if j != idx]
        dupes = find_duplicates(item_text, others)
        members = [{"file": os.path.basename(fpath), "line": line_num, "text": item_text}]
        for df, dl, dt, dc in dupes:
            for j, (f2, l2, _) in enumerate(raw_items):
                if os.path.abspath(f2) == os.path.abspath(df) and l2 == dl:
                    seen_grouped.add(j)
            members.append({"file": os.path.basename(df), "line": dl, "text": dt, "confidence": dc})
        gid = f"g{len(groups):04d}"
        groups.append({
            "group_id": gid,
            "project": "obsidian-brain",
            "representative": item_text,
            "members": members,
            "canonical_hash": f"hash{gid}",
        })
        seen_grouped.add(idx)
    return groups


# ---------------------------------------------------------------------------
# Assertion 1: Cold-cache pipeline runs with real signatures
# collect_open_items + find_duplicates grouping loop
# ---------------------------------------------------------------------------

print()
print("Assertion 1: cold-cache pipeline (collect_open_items + grouping)")
_PIPELINE_EVIDENCE_CACHE.clear()

try:
    raw_items = collect_open_items(
        vault_path=VAULT_PATH,
        sessions_folder=SESSIONS_FOLDER,
        project="obsidian-brain",
        max_sessions=50,
    )
    groups = _group_items(raw_items)
    _pass(f"collect_open_items returned {len(raw_items)} items, grouped into {len(groups)} groups")
except Exception as exc:
    _fail("cold-cache pipeline", str(exc))

# ---------------------------------------------------------------------------
# Assertion 2: Warm-cache partition() completes in <2s wall
# ---------------------------------------------------------------------------

print()
print("Assertion 2: warm-cache partition() completes in <2s")

try:
    cache = load_cache()
    head_sha = "deadbeef1234567"  # stable fake SHA — no git call needed
    t0 = time.monotonic()
    known, needs = partition(
        groups=groups if _PASSED >= 1 else [],
        cache=cache,
        project="obsidian-brain",
        head_sha=head_sha,
        force=False,
    )
    elapsed = time.monotonic() - t0
    if elapsed < 2.0:
        _pass(f"partition() completed in {elapsed:.3f}s (<2s); known={len(known)}, needs={len(needs)}")
    else:
        _fail("warm-cache partition", f"took {elapsed:.3f}s (≥2s limit)")
except Exception as exc:
    _fail("warm-cache partition", str(exc))

# ---------------------------------------------------------------------------
# Assertion 3: CLASSIFIER_PROMPT and SEMANTIC_MERGE_PROMPT contain required strings
# ---------------------------------------------------------------------------

print()
print("Assertion 3: prompt constants contain 'STRICT JSON' and 'no prose'")

try:
    problems = []
    if "STRICT JSON" not in CLASSIFIER_PROMPT:
        problems.append("CLASSIFIER_PROMPT missing 'STRICT JSON'")
    if "no prose" not in CLASSIFIER_PROMPT:
        problems.append("CLASSIFIER_PROMPT missing 'no prose'")
    if "STRICT JSON" not in SEMANTIC_MERGE_PROMPT:
        problems.append("SEMANTIC_MERGE_PROMPT missing 'STRICT JSON'")
    if "no prose" not in SEMANTIC_MERGE_PROMPT:
        problems.append("SEMANTIC_MERGE_PROMPT missing 'no prose'")
    if problems:
        _fail("prompt constants", "; ".join(problems))
    else:
        _pass("CLASSIFIER_PROMPT and SEMANTIC_MERGE_PROMPT both contain 'STRICT JSON' and 'no prose'")
except Exception as exc:
    _fail("prompt constants", str(exc))

# ---------------------------------------------------------------------------
# Assertion 4: Dashboard dir writable (or today's file exists)
# ---------------------------------------------------------------------------

print()
print("Assertion 4: dashboard dir writable (or today's file exists)")

try:
    import datetime
    dashboards_dir = Path(VAULT_PATH) / "claude-dashboards"
    today_str = datetime.date.today().isoformat()
    today_file = dashboards_dir / f"check-items-vault-{today_str}.md"

    if today_file.exists():
        _pass(f"today's dashboard file exists: {today_file.name}")
    elif dashboards_dir.is_dir() and os.access(str(dashboards_dir), os.W_OK):
        _pass(f"claude-dashboards dir is writable: {dashboards_dir}")
    elif not dashboards_dir.exists():
        # Try to create it — verify filesystem is accessible
        try:
            dashboards_dir.mkdir(parents=True, exist_ok=True)
            if os.access(str(dashboards_dir), os.W_OK):
                _pass(f"claude-dashboards dir created and writable: {dashboards_dir}")
            else:
                _fail("dashboard dir", f"created but not writable: {dashboards_dir}")
        except OSError as create_exc:
            _fail("dashboard dir", f"could not create: {create_exc}")
    else:
        _fail("dashboard dir", f"exists but not writable: {dashboards_dir}")
except Exception as exc:
    _fail("dashboard dir writable", str(exc))

# ---------------------------------------------------------------------------
# Assertion 5: /recall SKILL.md contains no AskUserQuestion or batch_cascade_checkoff
# ---------------------------------------------------------------------------

print()
print("Assertion 5: /recall SKILL.md contains no AskUserQuestion or batch_cascade_checkoff")

try:
    recall_skill_path = _REPO_ROOT / "skills" / "recall" / "SKILL.md"
    if not recall_skill_path.exists():
        _fail("/recall SKILL.md", f"file not found: {recall_skill_path}")
    else:
        content = recall_skill_path.read_text(encoding="utf-8")
        problems = []
        if "AskUserQuestion" in content:
            problems.append("contains 'AskUserQuestion'")
        if "batch_cascade_checkoff" in content:
            problems.append("contains 'batch_cascade_checkoff'")
        if problems:
            _fail("/recall SKILL.md", "; ".join(problems))
        else:
            _pass("/recall SKILL.md has no AskUserQuestion or batch_cascade_checkoff")
except Exception as exc:
    _fail("/recall SKILL.md", str(exc))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

total = _PASSED + len(_FAILURES)
print()
print(f"{'=' * 56}")
print(f"  {total} assertions, {len(_FAILURES)} failures")
print(f"{'=' * 56}")

if _FAILURES:
    for msg in _FAILURES:
        print(f"  FAIL: {msg}")
    sys.exit(1)
else:
    print("  All assertions passed — merge gate: GREEN")
    sys.exit(0)
