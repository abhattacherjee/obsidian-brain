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
from pathlib import Path
from datetime import datetime, timezone, timedelta

import themes
from obsidian_utils import load_config, write_vault_note
from vault_index import _connect, _default_db_path


def _emerge_dir(operation_id):
    from runtime_context import current_runtime_context
    from operation_state import operation_directory
    return str(operation_directory(current_runtime_context(), operation_id)[1])


def _themes_json_path(operation_id):
    return os.path.join(_emerge_dir(operation_id), 'emerge-themes.json')


def _analysis_path(operation_id):
    return os.path.join(_emerge_dir(operation_id), 'emerge-analysis.md')


def run_emerge_themes(days: int = 30, *, operation_id=None) -> None:
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
                            "note_path": m["note_path"],
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
            "note_path": n["note_path"],
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
    from runtime_context import current_runtime_context
    from operation_state import operation_directory, store_artifact
    context = current_runtime_context()
    identity, directory = operation_directory(context, operation_id)
    semantic = {'algorithm': 'emerge-v1', 'days': days,
                'corpus_sha256': hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest(),
                'sources': _corpus_revisions(vault, corpus)}
    out = store_artifact(context, identity, 'emerge-themes.json', json.dumps(corpus, indent=2), semantic=semantic)
    print(json.dumps({'operation_id': identity, 'operation_dir': str(directory),
                      'themes_path': str(out), 'analysis_path': str(directory / 'emerge-analysis.md')}))

    print("VAULT=" + vault)
    print("INS=" + ins)
    print("STATUS=OK:" + str(len(theme_records)) + ":" + str(len(unassigned_records)))


def _corpus_revisions(vault_path, corpus):
    vault = Path(vault_path).resolve()
    names = [member.get('note_path') for theme in corpus.get('themes', [])
             for member in theme.get('members', [])]
    names += [item.get('note_path') for item in corpus.get('unassigned_candidates', [])]
    revisions = {}
    for name in names:
        if name is None:
            continue
        path = vault / name
        if path.is_symlink() or vault not in path.resolve().parents:
            raise ValueError('Emerge source escapes selected vault')
        revisions[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return revisions


def _strip_leading_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block from analysis body text.

    The /emerge analysis sub-agent is instructed to emit only ``##`` body
    sections, but a stray YAML frontmatter block prepended by the sub-agent
    would otherwise be embedded verbatim into the note body, yielding a
    malformed double-frontmatter note. If ``text`` (after left-stripping
    whitespace) opens with a line that is exactly ``---``, drop everything
    through the next line that is exactly ``---`` (plus any immediately
    following blank lines). When there is no well-formed closing ``---``, the
    text is returned unchanged so we never corrupt legitimate content.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    lines = stripped.split("\n")
    if lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            rest = lines[i + 1:]
            while rest and rest[0].strip() == "":
                rest.pop(0)
            return "\n".join(rest)
    # No closing delimiter: leave content untouched.
    return text


def run_build_note(*, operation_id=None, themes_path=None, analysis_path=None) -> None:
    """Build the emerge vault note from emerge-themes.json + emerge-analysis.md.

    Prints SAVED:<path> then ---REPORT--- then the analysis body. Cleans up both
    temp files on success.
    """
    config = load_config()
    vault = config["vault_path"]
    ins = config.get("insights_folder", "claude-insights")

    from runtime_context import current_runtime_context
    from operation_state import operation_directory, operation_semantic, read_artifact
    context = current_runtime_context()
    if operation_id is None:
        raise ValueError('Emerge publication requires explicit operation identity')
    identity, directory = operation_directory(context, operation_id)
    corpus_path = str(directory / 'emerge-themes.json')
    resolved_analysis = str(directory / 'emerge-analysis.md')
    try:
        corpus = json.loads(read_artifact(context, identity, 'emerge-themes.json', supplied_path=themes_path))
        analysis = read_artifact(context, identity, 'emerge-analysis.md', supplied_path=analysis_path).decode('utf-8')
        semantic = operation_semantic(context, identity)
        if semantic is not None and semantic.get('sources') != _corpus_revisions(vault, corpus):
            raise ValueError('Emerge source revision changed')
    except (OSError, ValueError) as exc:
        print(f"ERROR could not read emerge artifacts ({exc}); re-run /emerge to regenerate", file=sys.stderr)
        sys.exit(1)
    analysis_path = resolved_analysis

    # Belt-and-suspenders: the sub-agent is told not to emit frontmatter, but
    # strip a stray leading block so it never embeds as double-frontmatter.
    analysis = _strip_leading_frontmatter(analysis)

    today = datetime.now(timezone.utc).date().isoformat()
    projects = corpus.get("projects", [])
    date_range = corpus.get("date_range", "")
    theme_count = len(corpus.get("themes", []))
    author_host = context.host
    tags = ["claude/emerge"] + ["claude/project/" + p for p in projects]

    fm = (
        "---\ntype: claude-emerge\nauthor_host: " + author_host
        + "\noperation_id: " + identity + "\ndate: " + today
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

    filename = today + "-emerge-patterns-" + identity + ".md"

    result = write_vault_note(vault, ins, filename, body, expected_revision=None)
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
