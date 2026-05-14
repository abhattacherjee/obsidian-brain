#!/usr/bin/env python3
"""
Prototype harness for issue #87 smarter /check-items.
Runs collect_open_items + find_duplicates grouping against current vault.
Outputs JSON to ~/.claude/obsidian-brain/proto-check-items-stage1.json.

Adapted from the task-2 spec to match the ACTUAL function signatures in
hooks/open_item_dedup.py (which differ substantially from the template):

  Real collect_open_items(vault_path, sessions_folder, project,
                          max_sessions=10, exclude_path=None)
    -> list[tuple[str, int, str]]   (no window_days; uses filename date sort)

  Real find_duplicates(candidate_text, existing_items, threshold=5)
    -> list[tuple[str, int, str, str]]  (candidate vs list — not batch grouper)

  Real deep_analysis_pipeline(basenames, projects_json, output_path,
                               vault_path, sessions_folder, insights_folder,
                               db_path=None)
    -> str "OK:<total>:<groups>:<projects_with_evidence>"

The prototype re-implements the same grouping logic used inside
deep_analysis_pipeline to compute Stage-2 group counts from raw items,
then calls deep_analysis_pipeline directly for Stage 3.

Usage:
    python3 scripts/dev-test/prototype-check-items-87.py [--project PROJECT] [--window 14]
"""
from __future__ import annotations

import sys
import os
import json
import glob
import argparse
import datetime
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks"))

try:
    from open_item_dedup import collect_open_items, find_duplicates, deep_analysis_pipeline
except ImportError as e:
    print(f"ERROR: Could not import hooks: {e}", file=sys.stderr)
    print("Run from repo root or ensure hooks/ is importable.", file=sys.stderr)
    sys.exit(1)


def load_config():
    config_path = Path.home() / ".claude" / "obsidian-brain-config.json"
    if not config_path.exists():
        print(f"ERROR: Config not found at {config_path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(config_path.read_text())


def discover_projects(vault_path: str, sessions_folder: str) -> list[str]:
    """Scan session notes to find all unique non-empty project values."""
    sessions_dir = os.path.join(vault_path, sessions_folder)
    if not os.path.isdir(sessions_dir):
        return []
    projects: set[str] = set()
    for fname in os.listdir(sessions_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(sessions_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i >= 20:
                        break
                    stripped = line.strip()
                    if stripped.startswith("project:"):
                        val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                        if val:
                            projects.add(val)
                        break
        except OSError:
            continue
    return sorted(projects)


def session_date(basename: str) -> str:
    """Extract YYYY-MM-DD prefix from filename, or '' if not present."""
    parts = basename.split("-")
    if len(parts) >= 3 and len(parts[0]) == 4:
        return "-".join(parts[:3])
    return ""


def filter_sessions_by_window(
    vault_path: str,
    sessions_folder: str,
    window_days: int,
) -> list[str]:
    """Return basenames of session notes within window_days from today."""
    sessions_dir = os.path.join(vault_path, sessions_folder)
    cutoff = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
    result = []
    for fname in sorted(os.listdir(sessions_dir), reverse=True):
        if not fname.endswith(".md"):
            continue
        date_str = session_date(fname)
        if date_str and date_str < cutoff:
            break  # files are sorted newest-first; once we pass cutoff we're done
        result.append(fname)
    return result


def group_items(
    raw_items: list[tuple[str, int, str]],
) -> list[dict]:
    """
    Group raw items using the same algorithm as deep_analysis_pipeline internals.

    For each item not yet grouped, find duplicates among the remaining items,
    and collect them into a group. This mirrors the seen_grouped logic inside
    deep_analysis_pipeline.
    """
    seen_grouped: set[int] = set()
    groups: list[dict] = []

    for idx, (fpath, line_num, item_text) in enumerate(raw_items):
        if idx in seen_grouped:
            continue
        others = [(f, l, t) for j, (f, l, t) in enumerate(raw_items) if j != idx]
        dupes = find_duplicates(item_text, others)
        if dupes:
            group_members = [{
                "file": os.path.basename(fpath),
                "line": line_num,
                "text": item_text,
            }]
            for df, dl, dt, dc in dupes:
                for j, (f2, l2, t2) in enumerate(raw_items):
                    if os.path.abspath(f2) == os.path.abspath(df) and l2 == dl:
                        seen_grouped.add(j)
                group_members.append({
                    "file": os.path.basename(df),
                    "line": dl,
                    "text": dt,
                    "confidence": dc,
                })
            groups.append({
                "representative": item_text,
                "members": group_members,
            })
            seen_grouped.add(idx)

    return groups


def main():
    parser = argparse.ArgumentParser(description="Prototype for issue #87")
    parser.add_argument("--project", default=None, help="Scope to one project (default: all)")
    parser.add_argument("--window", type=int, default=14, help="Day window for session filtering (default: 14)")
    args = parser.parse_args()

    config = load_config()
    vault_path = config["vault_path"]
    sessions_folder = config["sessions_folder"]
    insights_folder = config.get("insights_folder", "claude-insights")

    print(f"Vault: {vault_path}")
    print(f"Window: {args.window}d (used for session filename date filtering)")
    print(f"Project scope: {args.project or 'all'}")
    print()

    # Determine which projects to scan
    if args.project:
        projects = [args.project]
    else:
        projects = discover_projects(vault_path, sessions_folder)

    # Determine which session basenames fall within the window
    window_basenames = filter_sessions_by_window(vault_path, sessions_folder, args.window)
    print(f"Sessions in {args.window}d window: {len(window_basenames)}")

    # Collect a project->items map (using max_sessions=50 to see all recent items)
    print()
    print("=== Stage 1: collect_open_items (per project) ===")
    all_raw_items: list[tuple[str, int, str]] = []
    by_project_raw: dict[str, int] = {}
    per_project_items: dict[str, list[tuple[str, int, str]]] = {}

    for proj in projects:
        items = collect_open_items(
            vault_path=vault_path,
            sessions_folder=sessions_folder,
            project=proj,
            max_sessions=50,
        )
        if items:
            per_project_items[proj] = items
            by_project_raw[proj] = len(items)
            all_raw_items.extend(items)

    raw_count = len(all_raw_items)
    print(f"Raw open items (all projects): {raw_count}")
    for proj, count in sorted(by_project_raw.items()):
        print(f"  {proj}: {count} items")

    print()
    print("=== Stage 2a: find_duplicates (token coarse grouping, per project) ===")
    all_groups: list[dict] = []
    by_project_groups: dict[str, int] = {}

    for proj, items in per_project_items.items():
        groups = group_items(items)
        if groups:
            all_groups.extend(groups)
            by_project_groups[proj] = len(groups)

    # Projects with no duplicate groups still appear in raw but not groups
    group_count = len(all_groups)
    reduction_pct = (1.0 - group_count / raw_count) * 100 if raw_count else 0.0
    print(f"Coarse groups: {group_count} (from {raw_count} raw, {reduction_pct:.1f}% reduction)")

    for proj, count in sorted(by_project_groups.items()):
        raw = by_project_raw.get(proj, 0)
        print(f"  {proj}: {count} groups (from {raw} raw)")

    # Stage 3: deep_analysis_pipeline — uses real signature
    print()
    print(f"=== Stage 3: deep_analysis_pipeline (all {len(projects)} projects) ===")

    output_dir = Path.home() / ".claude" / "obsidian-brain"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    pipeline_output_path = str(output_dir / "proto-check-items-pipeline.json")

    # deep_analysis_pipeline uses basenames to determine which notes to analyze
    # for similarity/orphan detection; we pass all window notes
    projects_json = json.dumps(projects)

    try:
        status = deep_analysis_pipeline(
            basenames=window_basenames,
            projects_json=projects_json,
            output_path=pipeline_output_path,
            vault_path=vault_path,
            sessions_folder=sessions_folder,
            insights_folder=insights_folder,
        )
        print(f"Pipeline result: {status}")
        # Parse OK:<total>:<groups>:<projects_with_evidence>
        if status.startswith("OK:"):
            parts = status.split(":")
            pipeline_total = int(parts[1]) if len(parts) > 1 else 0
            pipeline_groups = int(parts[2]) if len(parts) > 2 else 0
            projects_with_evidence = int(parts[3]) if len(parts) > 3 else 0
            print(f"  total_raw={pipeline_total}, groups={pipeline_groups}, "
                  f"projects_with_evidence={projects_with_evidence}")

            # Load evidence keys from pipeline output
            evidence_keys: dict[str, list[str]] = {}
            try:
                with open(pipeline_output_path, "r", encoding="utf-8") as f:
                    pipeline_data = json.load(f)
                evidence_section = pipeline_data.get("evidence", {})
                for proj, ev in evidence_section.items():
                    evidence_keys[proj] = list(ev.keys())
                    ev_summary = {k: (len(v) if isinstance(v, list) else "present")
                                  for k, v in ev.items()}
                    print(f"  {proj}: {ev_summary}")
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  WARNING: could not read pipeline output: {exc}", file=sys.stderr)
    except Exception as e:
        print(f"  WARNING: deep_analysis_pipeline failed: {e}", file=sys.stderr)
        status = f"ERROR:{e}"
        evidence_keys = {}
        projects_with_evidence = 0
        pipeline_total = 0
        pipeline_groups = 0

    # Write stage1 summary JSON
    output_path = output_dir / "proto-check-items-stage1.json"

    output = {
        "generated_at": datetime.datetime.now().isoformat(),
        "window_days": args.window,
        "project_scope": args.project,
        "sessions_in_window": len(window_basenames),
        "raw_count": raw_count,
        "group_count": group_count,
        "reduction_pct": round(reduction_pct, 1),
        "by_project": {
            proj: {
                "raw": by_project_raw.get(proj, 0),
                "groups": by_project_groups.get(proj, 0),
            }
            for proj in sorted(set(list(by_project_raw.keys()) + list(by_project_groups.keys())))
        },
        "groups_sample": all_groups[:10],
        "pipeline_status": status if isinstance(status, str) else "",
        "evidence_keys": evidence_keys,
        "signature_adaptations": [
            "collect_open_items: uses sessions_folder param, no window_days; max_sessions=50",
            "find_duplicates: called per candidate vs list (not batch); grouping loop is local",
            "deep_analysis_pipeline: uses basenames+projects_json+output_path signature",
        ],
    }

    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=str(output_dir), suffix=".json"
    )
    json.dump(output, tmp, indent=2, default=str)
    tmp.flush()
    tmp_path = tmp.name
    tmp.close()
    os.replace(tmp_path, str(output_path))
    os.chmod(str(output_path), 0o600)

    print()
    print(f"=== Results written to {output_path} ===")
    print(f"Raw items:  {raw_count}")
    print(f"Groups:     {group_count}")
    print(f"Reduction:  {reduction_pct:.1f}%")
    print()
    print("Copy these numbers into PROTOTYPE-CHECK-ITEMS-87-FINDINGS.md")


if __name__ == "__main__":
    main()
