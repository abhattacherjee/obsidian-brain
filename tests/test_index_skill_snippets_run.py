"""Run authored reindex procedures against isolated config, vault and index.

The file-derived launcher must preserve folder selection and refuse bad config
without removing indexed rows.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
def _rebuild_command(skill):
    text = (REPO / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines()
             if line.startswith('python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py"')
             and "--operation 'reindex'" in line]
    assert len(lines) == 1, f"expected one trusted reindex invocation in {skill}"
    return lines[0]


def _invoke(skill, home, vault, db, environment, full="false"):
    request = home / 'reindex-request.json'
    request.write_text(json.dumps({'full': full == 'true'}))
    cwd = home / 'unrelated cwd with spaces'
    cwd.mkdir(exist_ok=True)
    environment = dict(environment, OB_RESOURCE_ROOT=str(REPO),
        OB_SKILL_PATH=str(REPO / 'skills' / skill / 'SKILL.md'),
        OB_HOST='claude', OB_CLIENT='cli', OB_SESSION_ID='snippet-test-0000',
        OB_CWD=str(cwd), OB_VAULT=str(vault), REQUEST_PATH=str(request),
        CLAUDE_CONFIG_DIR=str(home / '.claude'), CODEX_HOME=str(home / '.codex'))
    return subprocess.run(['bash', '-c', _rebuild_command(skill)], cwd=cwd,
        env=environment, capture_output=True, text=True, timeout=60)


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
    return _invoke(skill, home, vault, db, e, full)


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
    r = _invoke(skill, home, vault, db, e)
    assert r.returncode != 0
    assert "configuration" in r.stderr
    assert _paths(db) == {"s.md", "10-03-q.md"}
