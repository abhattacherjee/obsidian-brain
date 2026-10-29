"""Known-answer eval stays on the explicit DB copy and supports the old API."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from scripts import retrieval_eval


@pytest.fixture
def index(tmp_path, monkeypatch):
    folder = tmp_path / "claude-insights"
    folder.mkdir()
    query, target = retrieval_eval.CASES[0]
    for name, status, body in [
        (f"dated-{target}.md", "active", query),
        ("provisional.md", "provisional", "obsidian"),
        *[(f"other-{i}.md", "active", "obsidian") for i in range(5)],
    ]:
        (folder / name).write_text(
            f"---\ntype: claude-insight\nstatus: {status}\n---\n# Note\n\n{body}\n",
            encoding="utf-8",
        )
    db = tmp_path / "copy.db"
    retrieval_eval.vault_index.ensure_index(
        str(tmp_path), ["claude-insights"], db_path=str(db)
    )
    monkeypatch.setattr(retrieval_eval, "CASES", ((query, target), ("zebranotfound", "absent")))
    return db


def test_json_rank_counts_summary_and_no_access_logging(index, capsys, monkeypatch):
    search = retrieval_eval.vault_index.search_vault
    calls = []

    def recording_search(db, query, **kwargs):
        calls.append((db, kwargs))
        return search(db, query, **kwargs)

    monkeypatch.setattr(retrieval_eval.vault_index, "search_vault", recording_search)
    # Searches must use stored bodies, even when source notes are unavailable.
    (index.parent / "claude-insights").rename(index.parent / "unavailable-notes")
    before = index.read_bytes()
    assert retrieval_eval.main(["--db", str(index), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["db"] == str(index.resolve())
    for mode, or_count in [("include_provisional", 7), ("exclude_provisional", 6)]:
        metric = report["cases"][0][mode]
        assert metric["rank"] == 1
        assert metric["n"] == 1
        assert metric["or_candidates"] == or_count
        assert metric["ms"] >= 0
        assert report["cases"][1][mode]["rank"] is None
        assert report["cases"][1][mode]["n"] == 0
        summary = report["summary"][mode]
        assert summary == dict(rank_1=1, ranked_below_1=0, missed=1,
                               underfilled=1, max_ms=summary["max_ms"])
        assert summary["max_ms"] == max(row[mode]["ms"] for row in report["cases"])
    assert [kwargs["include_provisional"] for _, kwargs in calls] == [True, False] * 2
    for db, kwargs in calls:
        assert db == str(index.resolve())
        assert kwargs["log_access"] is False
        assert kwargs["limit"] == 15
        assert kwargs["caller"] == "vault-ask"
        assert "min_results" not in kwargs
    assert index.read_bytes() == before
    with closing(sqlite3.connect(str(index))) as conn:  # noqa: vault-db-connect -- inspect only the temporary test index
        assert conn.execute("SELECT count(*) FROM access_log").fetchone()[0] == 0


def test_markdown(index, capsys):
    assert retrieval_eval.main(["--db", str(index)]) == 0
    output = capsys.readouterr().out
    assert "| Query |" in output
    assert "1 / 1 / 7 /" in output
    assert "1 / 1 / 6 /" in output
    assert "None / 0 / 0 /" in output
    assert "include_provisional: rank-1=1, ranked-below-1=0, missed=1" in output
    assert "exclude_provisional: rank-1=1" in output
    assert "underfilled (n < 5 and or_candidates >= 5)=1" in output


@pytest.mark.parametrize("form", ["direct", "symlink", "relative"])
def test_refuses_resolved_default(tmp_path, monkeypatch, capsys, form):
    live = tmp_path / "live.db"
    live.write_bytes(b"must never open this")
    monkeypatch.setattr(retrieval_eval.vault_index, "_default_db_path", lambda: str(live))
    monkeypatch.chdir(tmp_path)
    db_arg = str(live)
    if form == "symlink":
        alias = tmp_path / "alias.db"
        alias.symlink_to(live)
        db_arg = str(alias)
    elif form == "relative":
        db_arg = "./live.db"

    def forbidden_connect(*args, **kwargs):
        pytest.fail("refused DB must never be opened")

    monkeypatch.setattr(retrieval_eval.sqlite3, "connect", forbidden_connect)
    assert retrieval_eval.main(["--db", db_arg]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "refusing" in captured.err


def test_refuses_literal_live_path_even_when_default_overridden(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(retrieval_eval.vault_index, "_default_db_path",
                        lambda: str(tmp_path / "override.db"))
    assert retrieval_eval.main(["--db", str(tmp_path / ".claude/obsidian-brain-vault.db")]) == 2
    assert "ERROR:" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["missing", "directory", "corrupt", "no_schema"])
def test_db_failure(tmp_path, capsys, kind):
    db = tmp_path / "bad.db"
    if kind == "directory":
        db.mkdir()
    elif kind == "corrupt":
        db.write_bytes(b"not sqlite")
    elif kind == "no_schema":
        db.touch()
    assert retrieval_eval.main(["--db", str(db), "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    if kind == "missing":
        assert not db.exists()


@pytest.mark.parametrize("argv", [[], ["--db", "copy.db", "--min-results", "-1"],
                                  ["--db", "copy.db", "--min-results", "five"]])
def test_usage_errors(argv):
    with pytest.raises(SystemExit) as exc:
        retrieval_eval.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("args", [[], ["--min-results", "0"]])
def test_old_search_api_omits_min_results(index, monkeypatch, capsys, args):
    calls = []

    def old_search(db, query, *, limit, log_access, include_provisional, caller):
        calls.append(dict(db=db, query=query, limit=limit, log_access=log_access,
                          include_provisional=include_provisional, caller=caller))
        return []

    monkeypatch.setattr(retrieval_eval.vault_index, "search_vault", old_search)
    assert retrieval_eval.main(["--db", str(index), "--json", *args]) == 0
    assert len(calls) == 4
    assert json.loads(capsys.readouterr().out)["summary"]["include_provisional"]["missed"] == 2


def test_positive_min_results_forwarded_and_first_basename_rank(index, monkeypatch, capsys):
    target = retrieval_eval.CASES[0][1]
    calls = []

    def new_search(db, query, **kwargs):
        calls.append(kwargs)
        return [{"path": f"{target}/unrelated.md"},
                {"path": f"folder/prefix-{target}.md"},
                {"path": f"folder/{target}-again.md"}]

    monkeypatch.setattr(retrieval_eval.vault_index, "search_vault", new_search)
    assert retrieval_eval.main(["--db", str(index), "--min-results", "5", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert all(call["min_results"] == 5 for call in calls)
    assert report["cases"][0]["include_provisional"]["rank"] == 2
    assert report["summary"]["include_provisional"]["ranked_below_1"] == 1


def test_positive_min_results_unsupported_is_usage_error(index, monkeypatch, capsys):
    def old_search(db, query, *, limit, log_access, include_provisional, caller):
        pytest.fail("unsupported option must be rejected before searching")

    monkeypatch.setattr(retrieval_eval.vault_index, "search_vault", old_search)
    assert retrieval_eval.main(["--db", str(index), "--min-results", "5"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "does not support" in captured.err


def test_uri_metacharacters_select_exact_copy(index, capsys):
    special = index.with_name("copy?#.db")
    # A closed/checkpointed index is a standalone copy.
    special.write_bytes(index.read_bytes())
    assert retrieval_eval.main(["--db", str(special), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["db"] == str(special)
    assert report["cases"][0]["include_provisional"]["rank"] == 1


def test_cli_uses_repo_hooks_and_returns_exit_code(tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(retrieval_eval.__file__).resolve()),
         "--db", str(tmp_path / "missing.db")],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "ERROR:" in result.stderr
    assert "Traceback" not in result.stderr
