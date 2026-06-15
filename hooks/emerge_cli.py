"""CLI helpers for /emerge — theme-level pattern discovery.

``run_emerge_themes`` refreshes ``themes.activation`` and dumps a theme-structured
corpus (``emerge-themes.json``) for one analysis sub-agent. ``run_build_note``
turns that corpus + the sub-agent's analysis into a vault note. Both print
KEY=VALUE / marker lines for the emerge SKILL.md to parse.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta

import themes
from obsidian_utils import load_config, write_vault_note
from vault_index import _connect, _default_db_path


def _emerge_dir() -> str:
    """Path to the working directory for emerge temp artifacts.

    Callers that create artifacts here pass ``mode=0o700`` to ``os.makedirs``
    so the directory is created with owner-only permissions.
    """
    return os.path.expanduser("~/.claude/obsidian-brain")


def _themes_json_path() -> str:
    return os.path.join(_emerge_dir(), "emerge-themes.json")


def _analysis_path() -> str:
    return os.path.join(_emerge_dir(), "emerge-analysis.md")


def run_emerge_themes(days: int = 30) -> None:
    """Refresh activation and write a theme-structured corpus for /emerge.

    Prints ``VAULT=``, ``INS=`` and a ``STATUS=`` line for SKILL.md to parse.
    On fewer than 2 themes in the window, emits ``STATUS=SPARSE:<n>`` and writes
    no JSON (the skill nudges the user to /consolidate or widen the window).
    Otherwise writes ``emerge-themes.json`` atomically and prints
    ``STATUS=OK:<theme_count>:<unassigned_count>``.
    """
    config = load_config()
    if not config.get("vault_path"):
        print("ERROR: vault_path not configured", file=sys.stderr)
        sys.exit(1)
    vault = config["vault_path"]
    ins = config.get("insights_folder", "claude-insights")

    db = _default_db_path()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    window_start = (now - timedelta(days=days)).date().isoformat()
    today = now.date().isoformat()
    date_range = f"{window_start} to {today}"

    try:
        # --- Refresh activation (the slice's write path) ---
        conn = _connect(db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            themes.recompute_activation(conn, now_iso)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        themes_in_window = themes.get_themes_in_window(db, window_start, project=None)

        # --- Sparse guard: nothing meaningful to synthesize ---
        if len(themes_in_window) < 2:
            conn.close()
            print("VAULT=" + vault)
            print("INS=" + ins)
            print("STATUS=SPARSE:" + str(len(themes_in_window)))
            return

        theme_records = []
        for t in themes_in_window:
            previews = themes.get_theme_member_previews(conn, t["id"], top_n=3)
            theme_records.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "summary": t["summary"],
                    "note_count": t["note_count"],
                    "activation": t["activation"],
                    "project": t["project"],
                    "updated_date": t["updated_date"],
                    "members": [
                        {
                            "title": m["title"],
                            "excerpt": m["excerpt"],
                            "similarity": m["similarity"],
                            "surprise": m["surprise"],
                            "project": m["project"],
                        }
                        for m in previews
                    ],
                }
            )

        unassigned = themes.get_unassigned_notes_in_window(db, window_start, limit=30)
        conn.close()
    except (sqlite3.Error, RuntimeError, ValueError, TypeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        sys.exit(1)

    unassigned_records = [
        {
            "title": n["title"],
            "excerpt": n["excerpt"],
            "project": n["project"],
            "date": n["date"],
        }
        for n in unassigned
    ]

    projects = sorted(
        {t["project"] for t in theme_records if t["project"]}
        | {n["project"] for n in unassigned_records if n["project"]}
    )

    corpus = {
        "generated_at": now_iso,
        "window_days": days,
        "date_range": date_range,
        "projects": projects,
        "themes": theme_records,
        "unassigned_candidates": unassigned_records,
    }

    # The DB connection is closed above (all data is gathered), so the atomic
    # JSON write below holds no connection. Clean up the temp file if any
    # filesystem step fails and exit with the clean ERROR contract.
    out = _themes_json_path()
    tmp = None
    try:
        os.makedirs(_emerge_dir(), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_emerge_dir(), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(corpus, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    except OSError as exc:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        print(f"ERROR {exc}", file=sys.stderr)
        sys.exit(1)

    print("VAULT=" + vault)
    print("INS=" + ins)
    print("STATUS=OK:" + str(len(theme_records)) + ":" + str(len(unassigned_records)))


def run_build_note() -> None:
    """Build the emerge vault note from emerge-themes.json + emerge-analysis.md.

    Prints SAVED:<path> then ---REPORT--- then the analysis body. Cleans up both
    temp files on success.
    """
    config = load_config()
    vault = config["vault_path"]
    ins = config.get("insights_folder", "claude-insights")

    corpus_path = _themes_json_path()
    analysis_path = _analysis_path()

    try:
        with open(corpus_path, encoding="utf-8") as f:
            corpus = json.load(f)
        with open(analysis_path, encoding="utf-8") as f:
            analysis = f.read()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR could not read emerge artifacts ({exc}); re-run /emerge to regenerate",
              file=sys.stderr)
        sys.exit(1)

    today = datetime.now(timezone.utc).date().isoformat()
    projects = corpus.get("projects", [])
    date_range = corpus.get("date_range", "")
    theme_count = len(corpus.get("themes", []))
    tags = ["claude/emerge"] + ["claude/project/" + p for p in projects]

    fm = (
        "---\ntype: claude-emerge\ndate: " + today
        + '\ndate_range: "' + date_range + '"'
        + "\nprojects:\n" + "\n".join("  - " + p for p in projects)
        + "\ntheme_count: " + str(theme_count)
        + "\ntags:\n" + "\n".join("  - " + t for t in tags)
        + "\n---"
    )
    title = "# Emerge: Pattern Discovery (" + date_range + ")"
    header = (
        "**Projects:** " + ", ".join(projects)
        + "\n**Themes analyzed:** " + str(theme_count)
    )
    body = fm + "\n\n" + title + "\n\n" + header + "\n\n" + analysis

    h = hashlib.md5(today.encode()).hexdigest()[-4:]
    filename = today + "-emerge-patterns-" + h + ".md"

    result = write_vault_note(vault, ins, filename, body)
    if result is None:
        print("SAVED:" + os.path.join(vault, ins, filename))
        print("---REPORT---")
        print(analysis)
    else:
        print(f"ERROR write failed: {result}", file=sys.stderr)
        sys.exit(1)

    for p in [corpus_path, analysis_path]:
        try:
            os.remove(p)
        except OSError:
            pass
