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


import lessons_notes
import lessons_verify
from datetime import date
from test_retro_rollup import grouped, _notes_with_tags, _snoozed_dates, fake_haiku, _trace_items


@pytest.fixture
def ready(env):
    vault, state, base = env
    assert run(base, "update") == 0
    lid = _lesson_named(state, 3)
    path = vault / "claude-insights" / "lessons" / state_of(state)["notes"][lid]
    return vault, state, base, lid, path


def cli_json(base, capsys, *args):
    capsys.readouterr()
    code = run(base, *args)
    return code, json.loads(capsys.readouterr().out)


def test_snooze_sets_and_clears(ready, capsys):
    _, _, base, lid, path = ready
    before = path.read_bytes()
    assert cli_json(base, capsys, "snooze", lid, "2026-12-01") == (
        0, {"lesson_id": lid, "snoozed_until": "2026-12-01"})
    assert cli_json(base, capsys, "snooze", lid, "none") == (
        0, {"lesson_id": lid, "snoozed_until": None})
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", ["2026-12-01", "2000-01-01", date.today().isoformat()])
def test_snooze_written_value_reads_back(ready, capsys, value):
    _, _, base, lid, path = ready
    assert run(base, "snooze", lid, value) == 0
    assert lessons_notes.read_snooze(path) == value
    code, rep = cli_json(base, capsys, "report", "--json")
    assert code == 0
    assert next(r for r in rep["lessons"] if r["id"] == lid)["snoozed_until"] == value
    for raw in (value, '"' + value + '"', "'" + value + "'"):
        path.write_bytes(path.read_bytes().replace(('"' + value + '"').encode(), raw.encode()))
        assert lessons_notes.read_snooze(path) == value
        assert run(base, "snooze", lid, value) == 0


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
@pytest.mark.parametrize("absent", [False, True])
def test_snooze_keeps_the_rest_of_the_note(ready, eol, absent):
    _, _, base, lid, path = ready
    before = path.read_bytes().replace(b"\n", eol)
    old = b"snoozed_until: null" + eol
    if absent:
        before = before.replace(old, b"")
    path.write_bytes(before)
    assert run(base, "snooze", lid, "2026-12-01") == 0
    new = b'snoozed_until: "2026-12-01"' + eol
    expected = before.replace(b"tags:", new + b"tags:", 1) if absent else before.replace(old, new, 1)
    assert path.read_bytes() == expected


def test_snooze_exit_codes(ready, capsys):
    _, _, base, lid, path = ready
    assert run(base, "snooze", lid, "2026-12-01") == 0
    for value in ("2026-13-45", "tomorrow", "2026-1-5"):
        assert run(base, "snooze", lid, value) == 2
    assert run(base, "snooze", "unknown", "none") == 2
    (path.parent / "retired.json").write_text(json.dumps({"old": {"merged_into": lid}}))
    capsys.readouterr()
    assert run(base, "snooze", "old", "none") == 2
    assert lid in capsys.readouterr().err
    path.unlink()
    assert run(base, "snooze", lid, "none") == 1


def test_snooze_survives_rebuild(ready):
    _, _, base, lid, path = ready
    assert run(base, "snooze", lid, "2026-12-01") == 0
    assert run(base, "rebuild") == 0
    assert lessons_notes.read_snooze(path) == "2026-12-01"


def test_snooze_carries_over_merged_into(grouped):
    vault, state, base = grouped
    _notes_with_tags(vault, {1: "G1", 2: "G1", 4: "G2", 5: "G2"})
    assert run(base, "update") == 0
    survivor, retiring = sorted(state_of(state)["lessons"])
    assert run(base, "snooze", retiring, "2026-12-01") == 0
    _notes_with_tags(vault, {3: "G1 G2"})
    assert run(base, "update") == 0
    assert _snoozed_dates(vault, state) == {survivor: "2026-12-01"}


def test_snooze_carries_over_split_from(grouped):
    vault, state, base = grouped
    _notes_with_tags(vault, {1: "G1", 2: "G1", 3: "G1 G2", 4: "G2", 5: "G2"})
    assert run(base, "update") == 0
    (lid,) = state_of(state)["lessons"]
    assert run(base, "snooze", lid, "2026-12-01") == 0
    (vault / "claude-insights" / "2026-10-03-retro-n3.md").unlink()
    assert run(base, "update") == 0
    s = state_of(state)
    assert len(s["lessons"]) == 2
    child = next(k for k in s["lessons"] if k != lid)
    assert s["lessons"][child]["split_from"] == lid
    assert set(_snoozed_dates(vault, state).values()) == {"2026-12-01"}


def test_snooze_none_then_rebuild_writes_nothing(ready, capsys):
    _, _, base, lid, path = ready
    before = path.read_bytes()
    assert run(base, "snooze", lid, "2026-12-01") == 0
    assert run(base, "snooze", lid, "none") == 0
    assert path.read_bytes() == before
    capsys.readouterr()
    assert run(base, "rebuild") == 0
    assert "notes written: 0" in capsys.readouterr().out


def test_show_prints_member_text_and_traces(ready, capsys):
    vault, state, base, lid, _ = ready
    # Traces are read live even though the state predates the trace.
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Run preflight alone RULE-A": "memory `feedback_preflight`"})
    ctx = retro_rollup.Ctx(retro_rollup._parser().parse_args(["show", *base, lid]))
    _, by_key, _ = retro_rollup._load(ctx)
    code, result = cli_json(base, capsys, "show", lid, "--json")
    assert code == 0
    assert any(m["trace"] is not None for m in result["members"])
    rep = next(r for r in lessons_notes.report(state_of(state))["lessons"] if r["id"] == lid)
    fields = ("name", "bucket", "tracking", "tracking_ref", "tracking_source", "issue_state",
              "issue_milestone", "issue_priority", "sessions")
    assert result == {"lesson_id": lid, **{k: rep[k] for k in fields}, "members": [
        {k: by_key[key][k] for k in ("note", "item", "date", "project", "text", "trace")}
        for key in state_of(state)["lessons"][lid]["members"]]}
    assert run(base, "show", lid) == 0
    out = capsys.readouterr().out
    assert rep["name"] in out and rep["bucket"] in out
    assert all(m["text"] in out for m in result["members"])


@pytest.mark.parametrize("failure", ["deleted", "invalid-utf8", "edited-away"])
def test_show_unreadable_note_lists_null_text_and_exits_1(ready, capsys, failure):
    vault, state, base, lid, _ = ready
    key = state_of(state)["lessons"][lid]["members"][0]
    source = vault / "claude-insights" / (key.split(":")[0] + ".md")
    ctx = retro_rollup.Ctx(retro_rollup._parser().parse_args(["show", *base, lid]))
    _, by_key, _ = retro_rollup._load(ctx)
    expected = {i["note"]: i["text"] for k, i in by_key.items()
                if k in state_of(state)["lessons"][lid]["members"] and k != key}
    if failure == "deleted":
        source.unlink()
    elif failure == "invalid-utf8":
        source.write_bytes(b"\xff")
    else:
        source.write_text(source.read_text().replace(by_key[key]["text"], "Changed item text"))
    code, result = cli_json(base, capsys, "show", lid, "--json")
    assert code == 1
    missing = next(m for m in result["members"] if m["note"] == key.split(":")[0])
    assert missing == {"note": key.split(":")[0], **dict.fromkeys(("item", "date", "project", "text", "trace"))}
    assert len([m for m in result["members"] if m["text"] is not None]) == 2
    assert {m["note"]: m["text"] for m in result["members"] if m["text"] is not None} == expected
    assert run(base, "show", lid) == 1
    assert "(unreadable)" in capsys.readouterr().out


def test_show_exit_codes(ready, capsys):
    _, _, base, lid, path = ready
    assert run(base, "show", lid) == 0
    assert run(base, "show", "unknown") == 2
    (path.parent / "retired.json").write_text(json.dumps({"old": {"merged_into": lid}}))
    capsys.readouterr()
    assert run(base, "show", "old") == 2
    assert lid in capsys.readouterr().err


def test_show_scrubs_secrets(env, capsys):
    vault, state, base = env
    token = "sk-ant-" + "a" * 30
    for p in (vault / "claude-insights").glob("*.md"):
        p.write_text(p.read_text().replace("RULE-A", "RULE-A " + token + " → memory `" + token + "`"))
    assert run(base, "update") == 0
    lid = _lesson_named(state, 3)
    for flags in ((), ("--json",)):
        capsys.readouterr()
        assert run(base, "show", lid, *flags) == 0
        out = capsys.readouterr().out
        assert token not in out
        assert obsidian_utils.scrub_secrets(token) in out
        if flags:
            members = json.loads(out)["members"]
            assert any(m["trace"] and obsidian_utils.scrub_secrets(token) in json.dumps(m["trace"])
                       for m in members)


def test_report_members_carry_dates(ready, capsys):
    vault, _, base, lid, _ = ready
    code, rep = cli_json(base, capsys, "report", "--json")
    assert code == 0
    members = next(r for r in rep["lessons"] if r["id"] == lid)["members"]
    assert {m["date"] for m in members} == {"2026-10-01", "2026-10-08", "2026-10-15"}
    (vault / "claude-insights" / (members[0]["note"] + ".md")).unlink()
    _, rep = cli_json(base, capsys, "report", "--json")
    assert next(r for r in rep["lessons"] if r["id"] == lid)["members"][0]["date"] is None


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("flag", [False, True, None, 0, "false", "absent"])
def test_update_note_honours_lessons_enabled(env, monkeypatch, capsys, flag, existing):
    vault, state, base = env
    if existing:
        assert run(base, "update") == 0
        # Uncached text makes a subsequent enabled update call the verifier.
        target = vault / "claude-insights" / "2026-10-01-retro-a1.md"
        target.write_text(target.read_text().replace("Run preflight alone", "Run preflight separately"))
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {} if flag == "absent" else {"lessons_enabled": flag})
    calls = []
    monkeypatch.setattr(lessons_verify, "run_haiku", lambda p, timeouts=(120, 240): calls.append(p) or fake_haiku(p))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in vault.rglob("*") if p.is_file()}
    state_before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in state.rglob("*") if p.is_file()}
    target = vault / "claude-insights" / "2026-10-01-retro-a1.md"
    capsys.readouterr()
    assert run(base, "update", "--note", str(target)) == 0
    if flag is False:
        assert capsys.readouterr().out == "lessons: disabled (lessons_enabled is false)\n"
        assert not calls
        assert state_before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in state.rglob("*") if p.is_file()}
        if not existing:
            assert not state.exists()
        assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in vault.rglob("*") if p.is_file()}
    else:
        assert calls


def test_update_without_note_ignores_lessons_enabled(env, monkeypatch):
    _, state, base = env
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {"lessons_enabled": False})
    assert run(base, "update") == 0
    assert len(state_of(state)["lessons"]) == 2


def test_other_commands_ignore_lessons_enabled(ready, monkeypatch):
    _, _, base, lid, _ = ready
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {"lessons_enabled": False})
    for args in (("rebuild",), ("report", "--json"), ("snooze", lid, "2026-12-01"),
                 ("show", lid), ("backfill",)):
        assert run(base, *args) == 0


def test_set_snooze_pure_helper():
    for eol in ("\n", "\r\n"):
        text = eol.join(["---", "name: unchanged", "tags: []", "---", "snoozed_until: body", ""])
        inserted = text.replace("tags: []", 'snoozed_until: "2026-12-01"' + eol + "tags: []")
        assert lessons_notes.set_snooze(text, "2026-12-01") == inserted
        cleared = inserted.replace('snoozed_until: "2026-12-01"', "snoozed_until: null")
        assert lessons_notes.set_snooze(inserted, None) == cleared
        assert lessons_notes.set_snooze(text, None) == text
        no_tags = text.replace("tags: []" + eol, "")
        assert lessons_notes.set_snooze(no_tags, "2026-12-01") == no_tags.replace(
            eol + "---", eol + 'snoozed_until: "2026-12-01"' + eol + "---", 1)
    for text in ("body", "---\nno closing delimiter\n"):
        with pytest.raises(ValueError):
            lessons_notes.set_snooze(text, None)


def test_snooze_lock_and_read_only_commands(ready, capsys):
    import fcntl
    vault, state, base, lid, path = ready
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for root in (vault, state) for p in root.rglob("*") if p.is_file()}
    with open(state / "lessons.lock", "r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(base, "snooze", lid, "2026-12-01") == 1
        assert run(base, "show", lid, "--json") == 0
        assert run(base, "report", "--json") == 0
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns)
                      for root in (vault, state) for p in root.rglob("*") if p.is_file()}


def test_snooze_refuses_note_outside_lessons(ready, tmp_path):
    _, state, base, lid, _ = ready
    outside = tmp_path / "outside.md"
    outside.write_text('---\nsnoozed_until: null\n---\n')
    s = state_of(state)
    s["notes"][lid] = str(outside)
    (state / "lessons-state.json").write_text(json.dumps(s))
    before = outside.read_bytes()
    assert run(base, "snooze", lid, "2026-12-01") == 1
    assert outside.read_bytes() == before


# --- review survivors (#478): X-001, C-001, C-010 -----------------------------------

def _set_state(state, mutate):
    s = state_of(state)
    mutate(s)
    (state / "lessons-state.json").write_text(json.dumps(s))


def _retire_into(vault, old, survivor):
    d = vault / "claude-insights" / "lessons"
    d.mkdir(parents=True, exist_ok=True)
    (d / "retired.json").write_text(json.dumps({old: {"merged_into": survivor}}))


@pytest.mark.parametrize("second", [
    {"file_issue": True, "repo": "o/r"}, {"tracking_ref": "o/r#9"}])
def test_x001_repeated_lesson_id_refuses_the_accept_file(filing, tmp_path, capsys, second):
    vault, state, base, gh = filing
    first = entry_for(state, title="First")
    f = _file_accept(tmp_path, [first, {"lesson_id": first["lesson_id"], **second}])
    before = retro_bytes(vault), (state / "lessons-state.json").read_bytes()
    for args in (("backfill", "--accept", str(f)), ("backfill", "--apply", "--accept", str(f))):
        capsys.readouterr()
        assert run(base, *args) == 2
        assert "repeats lesson_id" in capsys.readouterr().err
    assert gh.calls == gh.list_calls == []
    assert (retro_bytes(vault), (state / "lessons-state.json").read_bytes()) == before


def test_c001_preview_shows_an_inherited_filing_as_adopted_not_filed(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    entry = entry_for(state)
    lid = entry["lesson_id"]
    _retire_into(vault, "old-lesson", lid)
    _set_state(state, lambda s: s["filed"].update({"old-lesson": "o/r#7"}))
    f = _file_accept(tmp_path, [entry])
    capsys.readouterr()
    assert run(base, "backfill", "--accept", str(f)) == 0
    rows = json.loads(capsys.readouterr().out)["planned_filings"]
    assert [r["lesson"] for r in rows] == [lid]
    assert "o/r#7" in rows[0]["reason"]
    assert gh.calls == [] and gh.list_calls == []
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    out = json.loads(capsys.readouterr().out)
    assert gh.calls == [] and out["filed"] == []
    assert state_of(state)["lessons"][lid]["tracking"]["ref"] == "o/r#7"


@pytest.mark.parametrize("list_fail", [None, "rc"])
def test_c001_preview_matches_apply_for_an_inherited_pending_record(filing, tmp_path, capsys, list_fail):
    vault, state, base, gh = filing
    gh.list_fail = list_fail
    entry = entry_for(state)
    lid = entry["lesson_id"]
    _retire_into(vault, "old-lesson", lid)
    _set_state(state, lambda s: s["filed"].update(
        {"old-lesson": {"pending": True, "repo": "o/r", "marker": retro_rollup._marker("old-lesson")}}))
    f = _file_accept(tmp_path, [entry])
    capsys.readouterr()
    assert run(base, "backfill", "--accept", str(f)) == 0
    rows = json.loads(capsys.readouterr().out)["planned_filings"]
    assert rows[0]["reason"] == "pending reconcile"  # apply may file it, or may not: not a sure issue
    assert gh.calls == [] and gh.list_calls == []
    capsys.readouterr()
    code = run(base, "backfill", "--apply", "--accept", str(f))
    out = json.loads(capsys.readouterr().out)
    creates = [c for c in gh.calls if c[:2] == ["issue", "create"]]
    if list_fail is None:  # the reconcile finds nothing and drops the record, so apply files
        assert code == 0 and len(creates) == 1 and [x["lesson"] for x in out["filed"]] == [lid]
    else:  # the reconcile fails: an earlier issue may exist, so nothing is filed
        assert code == 1 and creates == []
        assert out["skipped_entries"][0]["reason"] == "cannot check for an earlier filing"


def _gone_tracking(s, lid, kind="issue", ref="o/r#4"):
    s["lessons"][lid]["tracking"] = {"kind": kind, "ref": ref, "since": "2026-10-01"}
    s["tracking"][f"{kind}:{ref}"] = {"exists": False}


def test_c010_gone_tracking_can_be_filed_in_preview_and_apply(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    entry = entry_for(state)
    lid = entry["lesson_id"]
    _set_state(state, lambda s: _gone_tracking(s, lid))
    f = _file_accept(tmp_path, [entry])
    capsys.readouterr()
    assert run(base, "backfill", "--accept", str(f)) == 0
    result = json.loads(capsys.readouterr().out)
    assert [r["lesson"] for r in result["planned_filings"]] == [lid]
    assert "reason" not in result["planned_filings"][0] and result["skipped_entries"] == []
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    out = json.loads(capsys.readouterr().out)
    assert [x["lesson"] for x in out["filed"]] == [lid]
    assert sum(1 for c in gh.calls if c[:2] == ["issue", "create"]) == 1
    new_ref = state_of(state)["lessons"][lid]["tracking"]["ref"]
    assert new_ref != "o/r#4" and state_of(state)["filed"][lid] == new_ref


def test_c010_gone_tracking_without_untraced_members_is_nothing_to_trace(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    entry = entry_for(state)
    lid = entry["lesson_id"]
    _set_state(state, lambda s: (_gone_tracking(s, lid), s["lessons"][lid].update(members=[])))
    f = _file_accept(tmp_path, [entry])
    for args in (("backfill", "--accept", str(f)), ("backfill", "--apply", "--accept", str(f))):
        capsys.readouterr()
        assert run(base, *args) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["skipped_entries"][0]["reason"] == "nothing to trace"
    assert gh.calls == []


def test_c010_live_tracking_is_still_refused(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    entry = entry_for(state)
    lid = entry["lesson_id"]
    _set_state(state, lambda s: s["lessons"][lid].update(
        tracking={"kind": "issue", "ref": "o/r#4", "since": "2026-10-01"}))
    f = _file_accept(tmp_path, [entry])
    for args in (("backfill", "--accept", str(f)), ("backfill", "--apply", "--accept", str(f))):
        capsys.readouterr()
        assert run(base, *args) == 0
        assert json.loads(capsys.readouterr().out)["skipped_entries"][0]["reason"] == "already tracked"
    assert gh.calls == []


def test_c006_cli_rejects_abbreviated_options(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    f = _file_accept(tmp_path, [entry_for(state)])
    before = retro_bytes(vault), (state / "lessons-state.json").read_bytes()
    for args in (("backfill", "--accept", str(f), "--appl"), ("backfill", "--acc", str(f))):
        capsys.readouterr()
        try:
            code = run(base, *args)
        except SystemExit as exc:
            code = exc.code
        assert code == 2, args
    assert gh.calls == gh.list_calls == []
    assert (retro_bytes(vault), (state / "lessons-state.json").read_bytes()) == before


def test_x011_gone_own_ref_does_not_hide_a_live_merged_in_ref(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    entry = entry_for(state)
    lid = entry["lesson_id"]
    _retire_into(vault, "old-lesson", lid)

    def mutate(s):
        s["filed"].update({lid: "o/r#3", "old-lesson": "o/r#7"})
        s["tracking"]["issue:o/r#3"] = {"exists": False}
    _set_state(state, mutate)
    f = _file_accept(tmp_path, [entry])
    capsys.readouterr()
    assert run(base, "backfill", "--accept", str(f)) == 0
    rows = json.loads(capsys.readouterr().out)["planned_filings"]
    assert rows[0]["reason"] == "adopts existing issue o/r#7"
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert gh.calls == []
    assert state_of(state)["lessons"][lid]["tracking"]["ref"] == "o/r#7"
