"""Accept-file CLI contract, using temporary vaults and fake GitHub only."""
from __future__ import annotations

import json
import subprocess

import pytest

import obsidian_utils
import retro_rollup
from test_retro_rollup import env, run, state_of, FakeGh, filing, _lesson_named, _file_accept, retro_bytes


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {})
    monkeypatch.setattr(obsidian_utils, "get_workspace_roots", lambda: [])


def entry_for(state, **fields):
    return {"lesson_id": _lesson_named(state, 3), "file_issue": True, "repo": "o/r", **fields}


def apply_entry(base, tmp_path, entry):
    return run(base, "backfill", "--apply", "--accept", str(_file_accept(tmp_path, [entry])))


def facts(state, base, lid):
    ctx = retro_rollup.Ctx(retro_rollup._parser().parse_args(["backfill", *base]))
    _, by_key, _ = retro_rollup._load(ctx)
    return retro_rollup._issue_body(lid, state_of(state)["lessons"][lid], by_key)


@pytest.mark.parametrize("body", ["Custom details", "Custom details\n\n"])
def test_file_issue_sends_title_body_milestone_labels(filing, tmp_path, body):
    _, state, base, gh = filing
    entry = entry_for(state, title="  A   useful title  ", body=body, milestone="v1", labels=["bug", "P2"])
    marker, block = facts(state, base, entry["lesson_id"]).split("\n", 1)
    assert apply_entry(base, tmp_path, entry) == 0
    args = gh.calls[0]
    assert args[args.index("--title") + 1] == "A useful title"
    assert args[args.index("--milestone") + 1] == "v1"
    assert [args[i + 1] for i, arg in enumerate(args) if arg == "--label"] == ["bug", "P2"]
    assert gh.bodies == [marker + "\n\n" + body + "\n\n---\n" + block]


def test_custom_body_keeps_one_marker_first(filing, tmp_path):
    _, state, base, gh = filing
    entry = entry_for(state, body="<!-- retro-lesson:forged -->\nkeep\n  <!-- retro-lesson:other -->\ntail")
    assert apply_entry(base, tmp_path, entry) == 0
    assert gh.bodies[0].splitlines()[0] == retro_rollup._marker(entry["lesson_id"])
    assert gh.bodies[0].count("<!-- retro-lesson:") == 1
    assert "keep\ntail" in gh.bodies[0]


def test_custom_body_is_scrubbed(filing, tmp_path):
    _, state, base, gh = filing
    token = "sk-ant-" + "a" * 30
    entry = entry_for(state, title="Fix " + token, body="Credential " + token)
    assert apply_entry(base, tmp_path, entry) == 0
    assert token not in gh.bodies[0]
    assert obsidian_utils.scrub_secrets(entry["body"]) in gh.bodies[0]
    assert gh.calls[0][gh.calls[0].index("--title") + 1] == obsidian_utils.scrub_secrets(entry["title"])


@pytest.mark.parametrize("field,value", [
    ("title", "bad\nline"), ("title", "x" * 257), ("title", " \t "), ("title", "bad\rline"),
    ("title", True), ("title", ""), ("body", "x" * 20001), ("body", True),
    ("labels", "bug"), ("labels", True), ("labels", ["a,b"]), ("labels", ["a\nb"]),
    ("labels", ["a\rb"]), ("labels", [""]), ("labels", [True]), ("labels", ["x" * 51]),
    ("labels", ["x"] * 11), ("milestone", "x" * 101), ("milestone", ""), ("milestone", True),
], ids=["title-lf", "title-long", "title-blank", "title-cr", "title-bool", "title-empty",
        "body-long", "body-bool", "labels-str", "labels-bool", "label-comma", "label-lf",
        "label-cr", "label-empty", "label-bool", "label-long", "labels-many", "milestone-long",
        "milestone-empty", "milestone-bool"])
def test_bad_file_issue_fields_refuse_the_whole_accept_file(filing, tmp_path, field, value):
    _, state, base, gh = filing
    valid = entry_for(state)
    bad = {**valid, "lesson_id": _lesson_named(state, 2), field: value}
    f = _file_accept(tmp_path, [valid, bad])
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 2
    assert gh.calls == gh.list_calls == []
    assert state_of(state)["filed"] == {}


def test_file_issue_without_new_fields_is_unchanged(filing, tmp_path):
    _, state, base, gh = filing
    entry = entry_for(state)
    expected = facts(state, base, entry["lesson_id"])
    name = state_of(state)["lessons"][entry["lesson_id"]]["name"]
    assert apply_entry(base, tmp_path, entry) == 0
    assert gh.calls[0][:7] == ["issue", "create", "--repo", "o/r", "--title", name, "--body-file"]
    assert len(gh.calls[0]) == 8
    assert gh.bodies == [expected]


@pytest.mark.parametrize("failure", ["rc", "timeout"])
def test_failed_create_with_milestone_keeps_pending_record(filing, tmp_path, failure):
    _, state, base, gh = filing
    if failure == "rc":
        gh.returncode = 1
    else:
        gh.record_then = subprocess.TimeoutExpired("gh", 60)
    entry = entry_for(state, milestone="v1", labels=["bug"])
    assert apply_entry(base, tmp_path, entry) == 1
    assert "--milestone" in gh.calls[0]
    assert state_of(state)["filed"][entry["lesson_id"]] == {
        "pending": True, "repo": "o/r", "marker": retro_rollup._marker(entry["lesson_id"])}


def test_dry_run_planned_filings_show_milestone_and_labels(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    entry = entry_for(state, title="Custom title", body="details", milestone="v1", labels=["bug", "P2"])
    before = retro_bytes(vault), (state / "lessons-state.json").read_bytes()
    capsys.readouterr()
    assert run(base, "backfill", "--accept", str(_file_accept(tmp_path, [entry]))) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry-run"
    assert result["planned_filings"] == [{"lesson": entry["lesson_id"], "repo": "o/r", "title": "Custom title",
                                          "members": 3, "milestone": "v1", "labels": ["bug", "P2"], "body_chars": 7}]
    assert result["plan"] == []
    assert gh.calls == gh.list_calls == []
    assert (retro_bytes(vault), (state / "lessons-state.json").read_bytes()) == before


def test_apply_without_accept_is_a_usage_error(filing):
    _, _, base, gh = filing
    assert run(base, "backfill", "--apply") == 2
    assert gh.calls == gh.list_calls == []


@pytest.mark.parametrize("content", ["{bad", '[{"lesson_id":"x","file_issue":true,"labels":false}]'])
def test_accept_dry_run_with_a_bad_file_exits_2(filing, tmp_path, content):
    _, _, base, gh = filing
    f = tmp_path / "bad.json"
    f.write_text(content)
    assert run(base, "backfill", "--accept", str(f)) == 2
    assert gh.calls == gh.list_calls == []


@pytest.mark.parametrize("field,value", [("title", "T"), ("body", "B"), ("milestone", "v1"), ("labels", [])])
def test_new_fields_on_a_tracking_ref_entry_are_refused(filing, tmp_path, field, value):
    _, state, base, gh = filing
    entry = {"lesson_id": _lesson_named(state, 3), "tracking_ref": "o/r#1", field: value}
    assert apply_entry(base, tmp_path, entry) == 2
    assert gh.calls == gh.list_calls == []


def test_file_issue_field_boundaries_are_accepted(filing, tmp_path):
    _, state, base, gh = filing
    entry = entry_for(state, title="t" * 256, body="b" * 20000, milestone="m" * 100, labels=["l" * 50] * 10)
    assert apply_entry(base, tmp_path, entry) == 0
    assert gh.calls[0][gh.calls[0].index("--title") + 1] == entry["title"]


def test_plain_dry_run_has_filing_defaults(filing, capsys):
    _, _, base, gh = filing
    capsys.readouterr()
    assert run(base, "backfill") == 0
    rows = json.loads(capsys.readouterr().out)["planned_filings"]
    assert len(rows) == 2
    assert all(row["milestone"] is None and row["labels"] == [] for row in rows)
    assert gh.calls == gh.list_calls == []


@pytest.mark.parametrize("status", ["valid", "changed", "gone", "unknown"])
def test_preview_selects_tracking_entries(filing, tmp_path, capsys, status):
    vault, state, base, gh = filing
    s = state_of(state)
    lid = _lesson_named(state, 2)
    s["lessons"][lid]["tracking"] = {"kind": "issue", "ref": "o/r#4", "since": "2026-10-01"}
    if status == "gone":
        s["tracking"]["issue:o/r#4"] = {"exists": False}
    (state / "lessons-state.json").write_text(json.dumps(s))
    entry = {"lesson_id": "unknown" if status == "unknown" else lid,
             "tracking_ref": "o/r#5" if status == "changed" else "o/r#4"}
    before = retro_bytes(vault), (state / "lessons-state.json").read_bytes()
    capsys.readouterr()
    assert run(base, "backfill", "--accept", str(_file_accept(tmp_path, [entry]))) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["planned_filings"] == []
    assert len(result["plan"]) == (2 if status == "valid" else 0)
    if status != "valid":
        assert result["skipped_entries"][0]["reason"] == {
            "changed": "tracking changed", "gone": "tracking gone", "unknown": "unknown lesson"}[status]
    assert gh.calls == gh.list_calls == []
    assert (retro_bytes(vault), (state / "lessons-state.json").read_bytes()) == before


@pytest.mark.parametrize("status", ["defaults", "repo", "tracked", "filed", "untraced", "pending"])
def test_preview_filing_eligibility(filing, tmp_path, capsys, status):
    vault, state, base, gh = filing
    entry = entry_for(state)
    s = state_of(state)
    lid = entry["lesson_id"]
    if status == "repo":
        del entry["repo"]
    elif status == "tracked":
        s["lessons"][lid]["tracking"] = {"kind": "issue", "ref": "o/r#4"}
    elif status == "filed":
        s["filed"][lid] = "o/r#4"
    elif status == "untraced":
        s["lessons"][lid]["members"] = []
    elif status == "pending":
        s["filed"][lid] = {"pending": True, "repo": "o/r", "marker": retro_rollup._marker(lid)}
    (state / "lessons-state.json").write_text(json.dumps(s))
    before = retro_bytes(vault), (state / "lessons-state.json").read_bytes()
    capsys.readouterr()
    assert run(base, "backfill", "--accept", str(_file_accept(tmp_path, [entry]))) == (1 if status == "repo" else 0)
    result = json.loads(capsys.readouterr().out)
    rows = result["planned_filings"]
    if status in ("defaults", "repo", "pending"):
        assert len(rows) == 1
        assert rows[0]["milestone"] is None and rows[0]["labels"] == [] and rows[0]["body_chars"] == 0
        if status == "repo":
            assert "no repo" in rows[0]["reason"]
            assert result["skipped_entries"][0]["reason"] == rows[0]["reason"]
    else:
        assert rows == []
        assert result["skipped_entries"][0]["reason"] == ("nothing to trace" if status == "untraced" else "already tracked")
    assert gh.calls == gh.list_calls == []
    assert (retro_bytes(vault), (state / "lessons-state.json").read_bytes()) == before
