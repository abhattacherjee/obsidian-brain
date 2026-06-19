"""CLI helpers for /standup deep — thin wrappers around open_item_dedup functions.

Each function is designed to be called from a minimal ``python3 -c`` stub
in the standup SKILL.md, keeping inline code to 2-3 lines.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

from open_item_dedup import (
    anchor_text_matches,
    build_deep_presentation,
    deep_analysis_pipeline,
)

# Matches an UNCHECKED markdown checkbox line with non-empty item text.
_UNCHECKED_CHECKBOX_RE = re.compile(r"^\s*-\s+\[ \]\s+\S")


def _is_checkbox_flip(old_text: str, new_text: str) -> bool:
    """True iff (old_text -> new_text) is a pure unchecked->checked checkbox flip.

    old_text must be an unchecked checkbox line (``- [ ] <text>``) and new_text
    must equal old_text with ONLY the first ``[ ]`` replaced by ``[x]`` — every
    other character identical. Anything else (link additions, prose edits) is
    NOT a checkbox flip and keeps the legacy substring-replace path.
    """
    if not _UNCHECKED_CHECKBOX_RE.match(old_text):
        return False
    flipped = old_text.replace("[ ]", "[x]", 1)
    return flipped == new_text


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

    # Filter out recently acted-on items so they aren't re-recommended
    acted = _load_acted_items()
    if acted and not status.startswith("ERROR:") and os.path.isfile(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                pipeline_data = json.load(f)
            groups = pipeline_data.get("items", {}).get("groups", [])
            original_count = len(groups)
            filtered = [g for g in groups if g.get("representative", "") not in acted]
            if len(filtered) < original_count:
                pipeline_data["items"]["groups"] = filtered
                pipeline_data["items"]["group_count"] = len(filtered)
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(pipeline_data, f, indent=2)
                skipped = original_count - len(filtered)
                print(f"[obsidian-brain] filtered {skipped} recently acted-on item(s)", file=sys.stderr)
        except (OSError, json.JSONDecodeError):
            pass  # best-effort filtering

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


_ACTED_ITEMS_PATH = os.path.expanduser("~/.claude/obsidian-brain/deep-acted-items.json")
_ACTED_TTL_SECONDS = 86400  # 24 hours


def _load_acted_items() -> set[str]:
    """Load recently acted-on item texts (within TTL)."""
    if not os.path.isfile(_ACTED_ITEMS_PATH):
        return set()
    try:
        import time
        if time.time() - os.path.getmtime(_ACTED_ITEMS_PATH) > _ACTED_TTL_SECONDS:
            os.remove(_ACTED_ITEMS_PATH)
            return set()
        with open(_ACTED_ITEMS_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_acted_items(items: set[str]) -> None:
    """Persist acted-on item texts (append to existing). Best-effort."""
    existing = _load_acted_items()
    combined = existing | items
    try:
        os.makedirs(os.path.dirname(_ACTED_ITEMS_PATH), exist_ok=True)
        with open(_ACTED_ITEMS_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(combined), f)
    except OSError as exc:
        print(f"[obsidian-brain] warning: could not save acted items: {exc}", file=sys.stderr)


def run_batch_edit() -> None:
    """Batch edit vault files (checkoffs, link additions).

    Reads JSON array of ``[filepath, old_text, new_text]`` triples from stdin.
    Prints ``Applied N/M edits``.
    Records acted-on items so they aren't re-recommended on next run.
    """
    import tempfile

    from obsidian_utils import load_config

    c = load_config()
    vault_root = os.path.realpath(c["vault_path"])

    edits = json.load(sys.stdin)
    success = 0
    acted_texts: set[str] = set()
    for filepath, old_text, new_text in edits:
        try:
            real_path = os.path.realpath(filepath)
            if not real_path.startswith(vault_root + os.sep) and real_path != vault_root:
                print(f"[obsidian-brain] path containment violation: {filepath}", file=sys.stderr)
                continue

            with open(real_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            new_content = None
            if _is_checkbox_flip(old_text, new_text):
                # CHECKBOX FLIP: line-anchored replace. Find the FIRST line whose
                # full content (sans trailing newline) exactly equals old_text AND
                # is an unchecked checkbox, then replace ONLY that line. This kills
                # mode 2 (quoted-prose / substring) corruption — a prose line that
                # merely *contains* the item text is never touched.
                lines = content.splitlines(keepends=True)
                for idx, raw_line in enumerate(lines):
                    stripped = raw_line.rstrip("\n")
                    if stripped == old_text and _UNCHECKED_CHECKBOX_RE.match(stripped):
                        ending = raw_line[len(stripped):]  # preserve "\n" / "" / "\r\n"
                        lines[idx] = new_text + ending
                        new_content = "".join(lines)
                        break
                if new_content is None:
                    print(
                        f"[obsidian-brain] checkoff skipped (no matching checkbox line): {filepath}",
                        file=sys.stderr,
                    )
            else:
                # NON-checkbox edit (e.g. link additions): legacy substring replace.
                if old_text in content:
                    new_content = content.replace(old_text, new_text, 1)

            if new_content is not None:
                fd, tmp = tempfile.mkstemp(
                    dir=os.path.dirname(real_path), suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, real_path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                success += 1
                # Track the item text (strip checkbox prefix for matching)
                item_text = old_text.replace("- [ ] ", "").replace("- [x] ", "").strip()
                if item_text:
                    acted_texts.add(item_text)
        except OSError as e:
            print(f"[obsidian-brain] edit failed {filepath}: {e}", file=sys.stderr)
    if acted_texts:
        _save_acted_items(acted_texts)
    print(f"Applied {success}/{len(edits)} edits")


def run_build_checkoffs() -> None:
    """Re-resolve checkoff targets by TEXT before any write (#201 Guard B).

    Reads a JSON array from stdin; each element:
        {"file": "<basename or path>", "line": <int hint>, "text": "<canonical item text>"}

    For each item we IGNORE the classifier's line number as authoritative and
    instead text-anchor the target: among the file's unchecked ``- [ ] `` lines
    we pick the line at the hint IFF it text-matches, else the FIRST candidate
    that text-matches. A drifted/absent hint can therefore never check off a
    different still-active item, and quoted-prose lines (no checkbox) are never
    targeted.

    Emits to stdout::

        {"edits": [[fullpath, old_text, new_text], ...],
         "skipped": [{"file":..., "line":..., "reason":...}, ...]}

    where each ``old_text`` is the EXACT current line content (no trailing
    newline) and ``new_text`` is that line with the first ``[ ]`` flipped to
    ``[x]`` — i.e. exactly what Guard A (run_batch_edit) line-matches. This
    function is PURE of writes: it only reads + emits, and is safe to unit-test.
    """
    from obsidian_utils import load_config

    c = load_config()
    vault_root = os.path.realpath(c["vault_path"])
    sessions_folder = c["sessions_folder"]
    insights_folder = c["insights_folder"]

    raw = sys.stdin.read(1_000_000)
    items = json.loads(raw) if raw.strip() else []

    edits: list[list[str]] = []
    skipped: list[dict] = []

    def _resolve_path(file_field: str) -> str | None:
        """Resolve a basename-or-path to a contained full path, else None."""
        if os.path.isabs(file_field) or os.sep in file_field:
            candidates = [file_field]
        else:
            candidates = [
                os.path.join(vault_root, sessions_folder, file_field),
                os.path.join(vault_root, insights_folder, file_field),
            ]
        for cand in candidates:
            real = os.path.realpath(cand)
            # containment check
            if not (real == vault_root or real.startswith(vault_root + os.sep)):
                continue
            if os.path.isfile(real):
                return real
        return None

    for item in items or []:
        file_field = item.get("file", "")
        line_hint = item.get("line")
        ref_text = item.get("text", "") or ""

        real = _resolve_path(file_field)
        if real is None:
            # Distinguish containment violation from plain not-found for the report.
            probe = os.path.realpath(
                file_field if (os.path.isabs(file_field) or os.sep in file_field)
                else os.path.join(vault_root, sessions_folder, file_field)
            )
            reason = ("containment" if not (
                probe == vault_root or probe.startswith(vault_root + os.sep)
            ) else "file not found")
            skipped.append({"file": file_field, "line": line_hint, "reason": reason})
            continue

        try:
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            print(f"[obsidian-brain] checkoff: cannot read {file_field}: {exc}", file=sys.stderr)
            skipped.append({"file": file_field, "line": line_hint, "reason": "file not found"})
            continue

        # Candidate lines = unchecked checkboxes.
        candidate_idxs = [
            i for i, ln in enumerate(lines)
            if _UNCHECKED_CHECKBOX_RE.match(ln.rstrip("\n"))
        ]

        target_idx = None
        # Prefer the hint line iff it's a candidate AND text-matches.
        if isinstance(line_hint, int) and 1 <= line_hint <= len(lines):
            hint_idx = line_hint - 1
            if hint_idx in candidate_idxs and anchor_text_matches(
                lines[hint_idx].rstrip("\n"), ref_text
            ):
                target_idx = hint_idx
        # Otherwise first text-matching candidate.
        if target_idx is None:
            for i in candidate_idxs:
                if anchor_text_matches(lines[i].rstrip("\n"), ref_text):
                    target_idx = i
                    break

        if target_idx is None:
            skipped.append({
                "file": file_field, "line": line_hint,
                "reason": "no matching checkbox line",
            })
            continue

        old_text = lines[target_idx].rstrip("\n")
        new_text = old_text.replace("[ ]", "[x]", 1)
        edits.append([real, old_text, new_text])

    print(
        f"[obsidian-brain] checkoffs: resolved {len(edits)}, skipped {len(skipped)}",
        file=sys.stderr,
    )
    print(json.dumps({"edits": edits, "skipped": skipped}))
