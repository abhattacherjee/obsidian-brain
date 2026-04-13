"""CLI helpers for /standup deep — thin wrappers around open_item_dedup functions.

Each function is designed to be called from a minimal ``python3 -c`` stub
in the standup SKILL.md, keeping inline code to 2-3 lines.
"""
from __future__ import annotations

import json
import os
import sys
import time

from open_item_dedup import build_deep_presentation, deep_analysis_pipeline


def run_pipeline(vault_path: str, sessions_folder: str, insights_folder: str) -> None:
    """Run deep analysis pipeline for /standup deep.

    Reads ``{"basenames": [...], "projects": [...]}`` from stdin.
    Prints status line: ``OK:<n>:<g>:<e>`` or ``CACHED:<n>:<g>:<e>``.
    """
    data = json.load(sys.stdin)
    basenames = data["basenames"]
    projects_json = json.dumps(data["projects"])

    output_path = os.path.expanduser("~/.claude/obsidian-brain/deep-pipeline.json")

    # Cache check: reuse if exists and < 15 min old
    if os.path.isfile(output_path) and (time.time() - os.path.getmtime(output_path)) < 900:
        with open(output_path) as f:
            cached = json.load(f)
        items = cached.get("items", {})
        n = items.get("total_raw", 0)
        g = items.get("group_count", 0)
        e = sum(
            1
            for v in cached.get("evidence", {}).values()
            if v.get("commits") or v.get("releases")
        )
        print(f"CACHED:{n}:{g}:{e}")
        return

    status = deep_analysis_pipeline(
        basenames,
        projects_json,
        output_path,
        vault_path,
        sessions_folder,
        insights_folder,
    )
    print(status)


def run_present(vault_path: str, sessions_folder: str, insights_folder: str) -> None:
    """Build deep analysis presentation.

    Reads basenames JSON array from stdin.
    Prints formatted markdown output.
    """
    basenames_json = sys.stdin.read(1_000_000)
    output = build_deep_presentation(
        os.path.expanduser("~/.claude/obsidian-brain/deep-pipeline.json"),
        os.path.expanduser("~/.claude/obsidian-brain/deep-classifications.json"),
        basenames_json,
        vault_path,
        sessions_folder,
        insights_folder,
    )
    print(output)


def run_batch_edit() -> None:
    """Batch edit vault files (checkoffs, link additions).

    Reads JSON array of ``[filepath, old_text, new_text]`` triples from stdin.
    Prints ``Applied N/M edits``.
    """
    edits = json.load(sys.stdin)
    success = 0
    for filepath, old_text, new_text in edits:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if old_text in content:
                content = content.replace(old_text, new_text, 1)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                success += 1
        except OSError as e:
            print(f"[obsidian-brain] edit failed {filepath}: {e}", file=sys.stderr)
    print(f"Applied {success}/{len(edits)} edits")
