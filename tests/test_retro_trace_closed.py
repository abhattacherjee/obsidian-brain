"""Closed-issue suggestions use tracked refs and never call live GitHub."""
import json
import subprocess

import pytest

import retro_trace as rt


ITEM = "- [ ] Do the thing → [o/r#5](https://github.com/o/r/issues/5)"


def note(vault, name="2026-retro-demo.md", *, project="demo", tracked=True,
         body=ITEM):
    path = vault / "insights" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    field = 'tracked_issues: ["o/r#5", "o/r#7"]\n' if tracked else ""
    path.write_text(
        f"---\ntype: claude-retro\nproject: {project}\n{field}---\n"
        f"\n## Process Improvements\n{body}\n", encoding="utf-8")
    return path


def fake_gh(*, state="CLOSED", reason="COMPLETED", stdout=None,
            returncode=0, exception=None):
    calls = []

    def gh(args):
        calls.append(args)
        if exception is not None:
            raise exception
        return subprocess.CompletedProcess(
            args, returncode,
            json.dumps({"state": state, "stateReason": reason}) if stdout is None else stdout,
            "failure detail " * 100)

    return gh, calls


@pytest.mark.parametrize(("state", "reason", "preselect", "label"), [
    ("CLOSED", "COMPLETED", True, "[closed on GH]"),
    ("closed", "completed", True, "[closed on GH]"),
    ("CLOSED", "NOT_PLANNED", False, "[closed on GH: not planned]"),
    ("CLOSED", None, False, "[closed on GH: not planned]"),
])
def test_closed_candidate(tmp_path, state, reason, preselect, label):
    path = note(tmp_path)
    gh, calls = fake_gh(state=state, reason=reason)
    assert rt.closed_candidates(str(tmp_path), "insights", gh=gh) == {
        "candidates": [{"file": str(path.resolve()), "line": 8, "text": ITEM,
                        "ref": "o/r#5", "state_reason": reason or "",
                        "preselect": preselect, "label": label}], "errors": []}
    assert calls == [["issue", "view", "5", "--repo", "o/r", "--json", "state,stateReason"]]


def test_open(tmp_path):
    note(tmp_path)
    gh, _ = fake_gh(state="OPEN")
    assert rt.closed_candidates(str(tmp_path), "insights", gh=gh) == {
        "candidates": [], "errors": []}


@pytest.mark.parametrize("failure", [
    {"returncode": 1},
    {"exception": subprocess.TimeoutExpired("gh", 10)},
    {"exception": FileNotFoundError("gh missing")},
    {"exception": OSError("cannot execute")},
    {"stdout": "not json"},
    {"stdout": "[]"},
    {"stdout": '{"stateReason": "COMPLETED"}'},
    {"stdout": '{"state": null}'},
], ids=["exit", "timeout", "missing", "oserror", "json", "array", "no-state", "null-state"])
def test_gh_failure(tmp_path, failure):
    note(tmp_path)
    note(tmp_path, "2027-retro-demo.md")
    gh, calls = fake_gh(**failure)
    result = rt.closed_candidates(str(tmp_path), "insights", gh=gh)
    assert result["candidates"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["ref"] == "o/r#5"
    assert 0 < len(result["errors"][0]["error"]) <= 200
    assert len(calls) == 1


def test_dedup_and_sort_items_by_file_and_first_line(tmp_path):
    later = note(tmp_path, "z-retro-demo.md")
    first_line = "- [ ] Multi-line item"
    earlier = note(tmp_path, "a-retro-demo.md", body=f"{first_line}\n  {ITEM}\n{ITEM}")
    gh, calls = fake_gh()
    result = rt.closed_candidates(str(tmp_path), "insights", gh=gh)
    assert result["errors"] == []
    assert [(c["file"], c["line"], c["text"]) for c in result["candidates"]] == [
        (str(earlier), 8, first_line), (str(earlier), 10, ITEM), (str(later), 8, ITEM)]
    assert len(calls) == 1


@pytest.mark.parametrize("excluded", ["checked", "untracked", "project", "no-tracked", "nested"])
def test_excluded_items_never_looked_up(tmp_path, excluded):
    kwargs = {
        "checked": {"body": ITEM.replace("[ ]", "[x]")},
        "untracked": {"body": ITEM.replace("#5", "#6").replace("/5)", "/6)")},
        "project": {"project": "other"},
        "no-tracked": {"tracked": False},
        "nested": {"name": "_provisional/2026-retro-nested.md"},
    }[excluded]
    note(tmp_path, **kwargs)
    gh, calls = fake_gh()
    assert rt.closed_candidates(str(tmp_path), "insights", project="demo", gh=gh) == {
        "candidates": [], "errors": []}
    assert calls == []


def test_invalid_and_unreadable_notes_skipped(tmp_path, monkeypatch):
    bad = note(tmp_path, "bad-retro-demo.md")
    bad.write_text(bad.read_text().replace("---\n", "", 1))
    unreadable = note(tmp_path, "unreadable-retro-demo.md")
    read_note = rt._read_note

    def read(path):
        if path == unreadable:
            raise OSError("unreadable")
        return read_note(path)

    monkeypatch.setattr(rt, "_read_note", read)
    gh, calls = fake_gh()
    assert rt.closed_candidates(str(tmp_path), "insights", gh=gh) == {
        "candidates": [], "errors": []}
    assert calls == []


def test_errors_sorted_by_ref(tmp_path):
    note(tmp_path, body=ITEM.replace("#5", "#7").replace("/5)", "/7)") + "\n" + ITEM)
    gh, _ = fake_gh(returncode=1)
    assert [e["ref"] for e in rt.closed_candidates(str(tmp_path), "insights", gh=gh)["errors"]] == [
        "o/r#5", "o/r#7"]


def test_run_gh(monkeypatch):
    calls = []
    expected = subprocess.CompletedProcess([], 0, "{}", "")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(rt.subprocess, "run", run)
    assert rt._run_gh(["issue", "view", "5"]) is expected
    assert rt.GH_TIMEOUT_SECONDS == 10
    assert calls == [(["gh", "issue", "view", "5"], {
        "stdin": subprocess.DEVNULL, "capture_output": True, "text": True,
        "timeout": rt.GH_TIMEOUT_SECONDS})]


def test_cli(tmp_path, monkeypatch, capsys):
    path = note(tmp_path, project="  'demo'  ")
    note(tmp_path, "other-retro-demo.md", project="other")
    gh, calls = fake_gh()
    monkeypatch.setattr(rt, "_run_gh", gh)
    assert rt.main(["closed-candidates", str(tmp_path), "insights", "--project", "demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [c["file"] for c in result["candidates"]] == [str(path)]
    assert len(calls) == 1
    assert rt.main(["closed-candidates", str(tmp_path), "insights"]) == 0
    assert len(json.loads(capsys.readouterr().out)["candidates"]) == 2
    monkeypatch.setattr(rt, "_run_gh", fake_gh(returncode=1)[0])
    assert rt.main(["closed-candidates", str(tmp_path), "insights"]) == 0
    assert len(json.loads(capsys.readouterr().out)["errors"]) == 1


@pytest.mark.parametrize("args", [[], ["vault"], ["vault", "insights", "--project"],
                                  ["vault", "insights", "--wrong", "demo"],
                                  ["vault", "insights", "extra"]])
def test_cli_bad_usage(args, capsys):
    assert rt.main(["closed-candidates", *args]) == 2
    assert "closed-candidates" in capsys.readouterr().err
