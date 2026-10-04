from __future__ import annotations
import json, os, subprocess, sys

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run(args, stdin="", home=None, db=None):
    env = dict(os.environ)
    if home:
        env["HOME"] = str(home)
    if db:
        env["OBSIDIAN_BRAIN_DB"] = str(db)
    env["CLAUDE_CODE_SESSION_ID"] = "wiki-cli-test"
    return subprocess.run([sys.executable, str(REPO / "hooks" / "wiki.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env, timeout=60)


def test_rule_prints_json():
    r = run(["rule"])
    assert r.returncode == 0 and "ASD-STE100" in json.loads(r.stdout)["rule"]


def test_unknown_subcommand_exits_2():
    assert run(["nope"]).returncode == 2


def test_oversized_stdin_is_refused(tmp_path):
    r = run(["count"], stdin="x" * 1_000_001, home=tmp_path)
    assert r.returncode == 1 and "ERROR:" in r.stderr and "too large" in r.stderr


def test_bad_json_is_refused(tmp_path):
    r = run(["count"], stdin="{not json", home=tmp_path)
    assert r.returncode == 1 and "ERROR:" in r.stderr


import io
import wiki


def _setup(tmp_path, wiki_folder="claude-wiki"):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    vault = tmp_path / "v"
    for n, t in (("i1", "claude-insight"), ("i2", "claude-insight"), ("d1", "claude-decision")):
        p = vault / "claude-insights" / f"{n}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntype: {t}\ndate: 2026-10-01\nproject: demo\n---\n# {n}\nzebracorn\n")
    (home / ".claude" / "obsidian-brain-config.json").write_text(
        json.dumps({"vault_path": str(vault), "wiki_folder": wiki_folder}))
    return home, vault, tmp_path / "i.db"


PAYLOAD = {"question": "How does zebracorn work?", "body": "It works.\n", "sources": ["i1", "i2", "d1"],
           "memory_sources": [], "topics": [], "confidence": "medium", "filed_by": "user"}


def test_cli_file_lookup_stale_round_trip(tmp_path):
    home, vault, db = _setup(tmp_path)
    r = run(["file"], stdin=json.dumps(PAYLOAD), home=home, db=db)
    assert r.returncode == 0, r.stderr
    page = json.loads(r.stdout)["path"]
    assert Path(page).is_file()
    r = run(["lookup"], stdin=json.dumps({"question": "zebracorn work"}), home=home, db=db)
    assert r.returncode == 0 and json.loads(r.stdout)["candidates"][0]["path"] == page
    r = run(["stale"], stdin=json.dumps({"page": page}), home=home, db=db)
    assert r.returncode == 0 and json.loads(r.stdout) == {"stale": False, "reasons": []}
    r = run(["count"], stdin=json.dumps({"sources": ["i1", "nope"]}), home=home, db=db)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["count"] == 1 and out["rejected"][0]["name"] == "nope"


def test_cli_refusal_exits_1(tmp_path):
    home, vault, db = _setup(tmp_path)
    r = run(["file"], stdin=json.dumps(dict(PAYLOAD, sources=["i1"])), home=home, db=db)
    assert r.returncode == 1 and "ERROR: only 1 qualifying sources (need 3)" in r.stderr


def test_cli_stale_outside_wiki_exits_1(tmp_path):
    home, vault, db = _setup(tmp_path)
    r = run(["stale"], stdin=json.dumps({"page": str(vault / "claude-insights" / "i1.md")}), home=home, db=db)
    assert r.returncode == 1 and "under" in r.stderr


def test_cli_wiki_off_or_invalid_exits_1(tmp_path):
    home, vault, db = _setup(tmp_path, wiki_folder="")
    assert run(["lookup"], stdin="{}", home=home, db=db).returncode == 1
    home2, _, db2 = _setup(tmp_path / "b", wiki_folder="../x")
    r = run(["count"], stdin="{}", home=home2, db=db2)
    assert r.returncode == 1 and "wiki_folder" in r.stderr


# In-process main() so coverage sees the dispatch paths.

def test_main_in_process(monkeypatch, capsys):
    assert wiki.main(["rule"]) == 0 and "ASD-STE100" in capsys.readouterr().out
    assert wiki.main([]) == 2
    monkeypatch.setattr(wiki.sys, "stdin", io.StringIO("[1]"))
    assert wiki.main(["count"]) == 1 and "JSON object" in capsys.readouterr().err

    def boom():
        raise RuntimeError("kaput")
    monkeypatch.setitem(wiki._COMMANDS, "count", boom)
    assert wiki.main(["count"]) == 2 and "internal: RuntimeError" in capsys.readouterr().err


def test_list_fields_must_be_lists(monkeypatch):
    import pytest
    with pytest.raises(wiki.WikiRefusal, match="sources must be a list"):
        wiki._list_field({"sources": "i1"}, "sources")


def test_commands_in_process(tmp_path, monkeypatch, capsys):
    import vault_index
    home, vault, db = _setup(tmp_path)
    ctx = {"vault": str(vault), "wiki_folder": "claude-wiki",
           "folders": ["claude-sessions", "claude-insights", "claude-wiki"], "db": str(db)}
    monkeypatch.setattr(wiki, "_context", lambda: ctx)

    def call(cmd, payload):
        monkeypatch.setattr(wiki.sys, "stdin", io.StringIO(json.dumps(payload)))
        rc = wiki.main([cmd])
        cap = capsys.readouterr()
        return rc, (json.loads(cap.out) if rc == 0 else cap.err)

    rc, out = call("file", PAYLOAD)
    assert rc == 0
    page = out["path"]
    assert call("lookup", {"question": "zebracorn work"})[1]["candidates"][0]["path"] == page
    assert call("stale", {"page": page})[1] == {"stale": False, "reasons": []}
    assert call("count", {"sources": ["i1", "i2"]})[1]["count"] == 2
    rc, err = call("stale", {"page": str(vault / "claude-insights" / "i1.md")})
    assert rc == 1 and "under" in err


@pytest.mark.parametrize("payload,code", [({"pattern": ""}, 1), ({"pattern": "  "}, 1), ({"pattern": 5}, 1),
                                          ({"pattern": "x" * 201}, 1)])
def test_memgrep_refuses_bad_patterns(payload, code, monkeypatch, capsys):
    monkeypatch.setattr(wiki, "_read_stdin", lambda: payload)
    assert wiki.main(["memgrep"]) == code
    assert "ERROR: pattern must be" in capsys.readouterr().err


def test_memgrep_cli_prints_matches(monkeypatch, capsys, tmp_path):
    f = tmp_path / "p" / "memory" / "m.md"
    f.parent.mkdir(parents=True)
    f.write_text("needle here")
    monkeypatch.setattr(wiki, "_read_stdin", lambda: {"pattern": "NEEDLE"})
    monkeypatch.setattr(wiki, "_memory_files", lambda: [f])
    assert wiki.main(["memgrep"]) == 0
    assert json.loads(capsys.readouterr().out) == {"matches": [{"name": "p/m.md", "path": str(f)}]}
