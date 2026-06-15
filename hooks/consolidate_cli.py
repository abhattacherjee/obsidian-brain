"""Thin CLI backing the /consolidate skill: batch agglomerative clustering of
unassigned notes into named themes, plus stats/split/merge sub-commands.

Imports the heavy lifting from clustering.py (pure math), themes.py (schema
helpers), and obsidian_utils.generate_theme_names (Haiku naming). Prints
KEY=VALUE lines for SKILL.md to parse; never prints note bodies.

All DB connections go through vault_index._connect (not raw sqlite3.connect)
to satisfy the #192 guard.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

from obsidian_utils import load_config, generate_theme_names
from vault_index import _connect, _default_db_path
import clustering
import themes


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fallback_name(centroid: dict) -> tuple[str, str]:
    top = sorted(centroid.items(), key=lambda kv: -kv[1])[:3]
    terms = [t for t, _ in top] or ["untitled"]
    return " / ".join(terms), ""


def _partition_by_project(rows):
    buckets = {}
    for path, proj, vec in rows:
        buckets.setdefault(proj, []).append((path, vec))
    return buckets


def run_consolidate(full: bool = False) -> None:
    config = load_config()
    if not config.get("vault_path"):
        print("ERROR: vault_path not configured", file=sys.stderr)
        sys.exit(1)
    db = _default_db_path()
    threshold = float(config.get("consolidate_cluster_threshold", 0.5))
    min_size = int(config.get("consolidate_min_cluster_size", 3))

    try:
        # --- Step 1: read source rows BEFORE any write ---
        # For --full, use all vectorized notes (wipe is deferred until after planning).
        # For incremental, only unassigned notes.
        if full:
            rows = themes.get_all_vectorized_notes(db)
        else:
            rows = themes.get_unassigned_notes(db)
        print(f"SCANNED={len(rows)}" if full else f"UNASSIGNED={len(rows)}")
        buckets = _partition_by_project(rows)

        # --- Step 2: build the ENTIRE plan in-memory, NO DB writes yet ---
        plan = []  # list of (proj, name, summary, centroid, members)
        now = _now_iso()
        for proj, items in sorted(buckets.items(), key=lambda kv: (kv[0] is not None, kv[0] or "")):
            clusters = clustering.cluster_vectors(items, threshold=threshold, min_cluster_size=min_size)
            if not clusters:
                continue
            vec_by_path = dict(items)
            centroids = [themes.compute_centroid([vec_by_path[p] for p in c]) for c in clusters]
            payload = [
                {"top_terms": [t for t, _ in sorted(cen.items(), key=lambda kv: -kv[1])[:10]],
                 "sample_titles": cl[:5]}
                for cen, cl in zip(centroids, clusters)
            ]
            names, reason = generate_theme_names(payload, model=config.get("summary_model", "haiku"))
            if reason is not None:
                print(f"NAMING_FALLBACK={reason} project={proj}", file=sys.stderr)
                names = None

            for i, cl in enumerate(clusters):
                cen = centroids[i]
                if names and i < len(names):
                    name, summary = names[i]["name"], names[i]["summary"]
                else:
                    name, summary = _fallback_name(cen)
                members = [(p, vec_by_path[p]) for p in cl]
                plan.append((proj, name, summary, cen, members))

        # --- Step 3: ONE transaction — wipe (if full) then create all themes ---
        conn = _connect(db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if full:
                conn.execute("DELETE FROM theme_members")
                conn.execute("DELETE FROM themes")
            for proj, name, summary, cen, members in plan:
                themes.create_theme(conn, name, summary, cen, members, proj, now)
            themes.recompute_activation(conn, now)
            conn.commit()
        finally:
            conn.close()

        created = len(plan)
        conn = _connect(db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0]
        finally:
            conn.close()
        print(f"CREATED={created} THEMES_TOTAL={total}")

    except (sqlite3.Error, RuntimeError, ValueError, TypeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        sys.exit(1)


def run_stats() -> None:
    config = load_config()
    db = _default_db_path()
    try:
        st = themes.theme_stats(db)
        print(f"THEMES={st['themes']}")
        print(f"MEMBERS={st['members']}")
        print(f"UNASSIGNED={st['unassigned']}")
        for t in st["largest"]:
            print(f"LARGEST id={t['id']} count={t['note_count']} name={t['name']}")
        nudge = int(config.get("consolidate_unassigned_threshold", 50))
        if st["unassigned"] > nudge:
            print(f"NUDGE unassigned={st['unassigned']} exceeds {nudge}; run /consolidate")
    except (sqlite3.Error, RuntimeError, ValueError, TypeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        sys.exit(1)


def run_merge(a: int, b: int) -> None:
    db = _default_db_path()
    try:
        ok = themes.merge_themes(db, a, b, _now_iso())
        if ok:
            # merge_themes owns its own connection; refresh activation in a
            # fresh short write transaction so the surviving theme reflects
            # its merged membership recency.
            conn = _connect(db)
            try:
                conn.execute("BEGIN IMMEDIATE")
                themes.recompute_activation(conn, _now_iso())
                conn.commit()
            finally:
                conn.close()
        print(f"MERGED a={a} b={b}" if ok else f"ERROR theme(s) not found a={a} b={b}")
    except (sqlite3.Error, RuntimeError, ValueError, TypeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        sys.exit(1)


def run_split(theme_id: int) -> None:
    config = load_config()
    db = _default_db_path()
    threshold = float(config.get("consolidate_cluster_threshold", 0.5))
    min_size = int(config.get("consolidate_min_cluster_size", 3))
    split_threshold = min(0.95, threshold + 0.2)  # tighter cut to break a theme apart

    try:
        conn = _connect(db)
        row = conn.execute("SELECT project FROM themes WHERE id = ?", (theme_id,)).fetchone()
        if row is None:
            conn.close()
            print(f"ERROR theme {theme_id} not found")
            return
        project = row[0]
        items = themes._theme_member_vectors(conn, theme_id)
        conn.close()

        subclusters = clustering.cluster_vectors(items, threshold=split_threshold, min_cluster_size=min_size)
        if len(subclusters) < 2:
            print(f"NO_SPLIT theme={theme_id} subclusters={len(subclusters)}")
            return

        vec_by_path = dict(items)
        centroids = [themes.compute_centroid([vec_by_path[p] for p in c]) for c in subclusters]
        payload = [{"top_terms": [t for t, _ in sorted(c.items(), key=lambda kv: -kv[1])[:10]],
                    "sample_titles": cl[:5]} for c, cl in zip(centroids, subclusters)]
        names, reason = generate_theme_names(payload, model=config.get("summary_model", "haiku"))
        if reason is not None:
            names = None

        now = _now_iso()
        conn = _connect(db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM theme_members WHERE theme_id = ?", (theme_id,))
            conn.execute("DELETE FROM themes WHERE id = ?", (theme_id,))
            for i, cl in enumerate(subclusters):
                cen = centroids[i]
                name, summary = (names[i]["name"], names[i]["summary"]) if names else _fallback_name(cen)
                themes.create_theme(conn, name, summary, cen, [(p, vec_by_path[p]) for p in cl], project, now)
            conn.commit()
        finally:
            conn.close()
        print(f"SPLIT theme={theme_id} into={len(subclusters)}")

    except (sqlite3.Error, RuntimeError, ValueError, TypeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        sys.exit(1)
