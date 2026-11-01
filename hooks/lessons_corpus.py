"""Load retro improvement items and find similar items across notes."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import heapq
import math
from pathlib import Path
import re

import frontmatter
import retro_trace
import tfidf


def _item_text(text: str) -> str:
    """Remove the checkbox and a recognized last-line trace, then fold spaces."""
    text = re.sub(r"^- \[[ xX]\] ", "", text, count=1)
    lines = text.split("\n")
    if retro_trace.parse_trace(lines[-1]) is not None:
        lines[-1] = lines[-1].rsplit(" → ", 1)[0]
    return " ".join("\n".join(lines).split())


def item_key(note_stem: str, text: str) -> str:
    """Identify an item independently of its checkbox, trace, case and spacing."""
    norm = _item_text(text).lower()
    return f"{note_stem}:{hashlib.sha1(norm.encode('utf-8')).hexdigest()[:16]}"


def load_items(vault_path: str, insights_folder: str) -> tuple[list[dict], list[dict]]:
    """Read every improvement in direct retro children contained in the vault."""
    vault = Path(vault_path).resolve()
    items = []
    skipped = []
    for path in sorted(Path(vault_path, insights_folder).glob("*-retro-*.md"),
                       key=lambda p: p.name):
        try:
            if not path.resolve().is_relative_to(vault):
                continue
            with open(path, encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeError):
            skipped.append({"path": str(path), "reason": "unreadable"})
            continue
        _, fm, _, _, error = frontmatter.split_frontmatter(
            frontmatter.split_lines_lf_crlf(text))
        if error:
            skipped.append({"path": str(path), "reason": "unreadable"})
            continue
        metadata = {}
        for line in fm:
            match = re.match(r"^(project|date)\s*:\s*(.*?)\s*$", line)
            if match and match[1] not in metadata:
                value = match[2].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                metadata[match[1]] = value.strip()
        project = metadata.get("project", "unknown")
        date = metadata.get("date", path.name[:10])[:10]
        for item in retro_trace.parse_items(text):
            items.append({
                "key": item_key(path.stem, item["text"]),
                "note": path.stem, "path": str(path), "item": item["item"],
                "line": item["start"] + 1, "project": project, "date": date,
                "text": _item_text(item["text"]),
                "checked": item["checked"], "trace": item["trace"],
            })
    return items, skipped


def top_candidates(items: list[dict], k: int = 10,
                   only: set[str] | None = None) -> dict[str, list[str]]:
    """Rank positive cosine matches from other notes, using corpus-wide IDF."""
    anchors = [i for i, item in enumerate(items)
               if only is None or item["key"] in only]
    if k <= 0 or not anchors:
        return {items[i]["key"]: [] for i in anchors}

    tokens = [tfidf._tokenize_for_tfidf(item["text"]) for item in items]
    term_df = Counter()
    for document in tokens:
        term_df.update(set(document))
    vectors = []
    index = defaultdict(list)
    for i, document in enumerate(tokens):
        vector = tfidf._compute_tfidf_vector(document, term_df, len(items))
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        if norm:
            vector = {term: weight / norm for term, weight in vector.items()}
        vectors.append(vector)
        for term, weight in vector.items():
            index[term].append((i, weight))

    keys = [item["key"] for item in items]
    notes = [item["note"] for item in items]
    result = {}
    for anchor in anchors:
        scores = defaultdict(float)
        for term, weight in vectors[anchor].items():
            for candidate, other_weight in index[term]:
                if notes[candidate] != notes[anchor]:
                    scores[candidate] += weight * other_weight
        best = heapq.nsmallest(
            k, (i for i, score in scores.items() if score > 0),
            key=lambda i: (-scores[i], keys[i]))
        result[keys[anchor]] = [keys[i] for i in best]
    return result
