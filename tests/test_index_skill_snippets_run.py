"""Run the /vault-reindex and /obsidian-setup rebuild snippets for real (#393 review).

test_skill_snippets.py only compiles snippets. These run the two snippets
that call rebuild_index, against a temp HOME, config, vault and DB, so an
argv shift or a wrong folder source fails here.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SNIPPET_RE = re.compile(r"python3 -c '(.*?)'((?: \"\$[A-Z_]+\")*)", re.DOTALL)


def _rebuild_snippet(skill: str):
    text = (REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    for m in _SNIPPET_RE.finditer(text):
        if "rebuild_index(" in m.group(1):
            return m.group(1), re.findall(r"\$([A-Z_]+)", m.group(2))
    raise AssertionError(f"no rebuild_index snippet in {skill}")


def _note(p: Path, t: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {t}\ndate: 2026-10-03\nproject: demo\n---\n# t\nbody\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    vault = tmp_path / "v"
    _note(vault / "claude-sessions" / "s.md", "claude-session")
    _note(vault / "claude-wiki" / "queries" / "2026" / "10-03-q.md", "claude-wiki")
    _note(vault / "claude-wiki" / "index.md", "claude-wiki-index")
    db = tmp_path / "i.db"
    e = dict(os.environ, HOME=str(home), OBSIDIAN_BRAIN_DB=str(db),
             CLAUDE_CODE_SESSION_ID="snippet-test-0000")
    return home, vault, db, e


def _run(skill, home, vault, db, e, wiki_folder="claude-wiki", full="false"):
    (home / ".claude" / "obsidian-brain-config.json").write_text(
        json.dumps({"vault_path": str(vault), "wiki_folder": wiki_folder}))
    code, names = _rebuild_snippet(skill)
    values = {"VAULT_PATH": str(vault), "FULL_MODE": full}
    argv = [values[n] for n in names]
    return subprocess.run([sys.executable, "-c", code, *argv], cwd=REPO, env=e,
                          capture_output=True, text=True, timeout=60)


def _paths(db):
    conn = sqlite3.connect(str(db))
    try:
        return {Path(r[0]).name for r in conn.execute("SELECT path FROM notes")}
    finally:
        conn.close()


@pytest.mark.parametrize("skill", ["vault-reindex", "obsidian-setup"])
def test_rebuild_snippet_indexes_wiki_and_excludes_index_file(skill, env):
    home, vault, db, e = env
    r = _run(skill, home, vault, db, e)
    assert r.returncode == 0, r.stderr
    stats = json.loads(r.stdout.strip().splitlines()[-1])
    assert _paths(db) == {"s.md", "10-03-q.md"}
    assert stats["excluded"] == 1


def test_reindex_snippet_preserve_mode_keeps_wiki_rows(env):
    home, vault, db, e = env
    assert _run("vault-reindex", home, vault, db, e, full="true").returncode == 0
    r = _run("vault-reindex", home, vault, db, e, full="false")
    assert r.returncode == 0, r.stderr
    stats = json.loads(r.stdout.strip().splitlines()[-1])
    assert stats["mode"] == "preserve"
    assert stats["foreign_deleted"] == 0
    assert _paths(db) == {"s.md", "10-03-q.md"}


@pytest.mark.parametrize("skill", ["vault-reindex", "obsidian-setup"])
def test_rebuild_snippet_refuses_invalid_wiki_folder_and_keeps_rows(skill, env):
    home, vault, db, e = env
    assert _run(skill, home, vault, db, e).returncode == 0
    r = _run(skill, home, vault, db, e, wiki_folder="~/claude-wiki")
    assert r.returncode != 0
    assert "wiki_folder" in r.stderr
    assert _paths(db) == {"s.md", "10-03-q.md"}


@pytest.mark.parametrize("skill", ["vault-reindex", "obsidian-setup"])
def test_rebuild_snippet_refuses_unreadable_config_and_keeps_rows(skill, env):
    # A broken config makes load_config fall back to defaults; the rebuild
    # must refuse instead of pruning rows outside the default folders (C-001).
    home, vault, db, e = env
    assert _run(skill, home, vault, db, e).returncode == 0
    (home / ".claude" / "obsidian-brain-config.json").write_text('{"vault_path": "x",}')
    code, names = _rebuild_snippet(skill)
    argv = [{"VAULT_PATH": str(vault), "FULL_MODE": "false"}[n] for n in names]
    r = subprocess.run([sys.executable, "-c", code, *argv], cwd=REPO, env=e,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "vault_path" in r.stderr
    assert _paths(db) == {"s.md", "10-03-q.md"}
