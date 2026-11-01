"""retro_rollup CLI (#471). Temp vault and state dir only; Haiku and gh are faked."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import lessons_verify
import retro_rollup

TAG = re.compile(r"RULE-([A-Z])")


def fake_haiku(prompt: str):
    """Same lesson iff anchor and candidate carry the same RULE-x tag; names lessons too."""
    if prompt.startswith("Give each lesson"):
        blocks = re.findall(r"LESSON (\d+):\n(.*)", prompt)
        return json.dumps({n: f"Follow rule {TAG.search(t).group(1)}" for n, t in blocks if TAG.search(t)})
    anchor = re.search(r"^ANCHOR: (.*)$", prompt, re.M).group(1)
    a = TAG.search(anchor)
    out = {}
    for n, text in re.findall(r"^CANDIDATE (\d+): (.*)$", prompt, re.M):
        c = TAG.search(text)
        out[n] = "same" if a and c and a.group(1) == c.group(1) else "different"
    return json.dumps(out)


def note(vault: Path, name: str, date: str, project: str, items: list[str], eol="\n") -> Path:
    folder = vault / "claude-insights"
    folder.mkdir(parents=True, exist_ok=True)
    body = "".join(f"- [ ] {i}\n" for i in items)
    text = f"---\ntype: claude-retro\ndate: {date}\nproject: {project}\n---\n\n## Process Improvements\n{body}\n## Key Learnings\n- x\n"
    path = folder / name
    path.write_bytes(text.replace("\n", eol).encode())
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    monkeypatch.setattr(lessons_verify, "run_haiku", lambda p, timeouts=(120, 240): fake_haiku(p))
    monkeypatch.setattr(retro_rollup, "_run_gh", lambda args: pytest.fail("gh called"))
    note(vault, "2026-10-01-retro-a1.md", "2026-10-01", "p1",
         ["Run preflight alone RULE-A", "Grep callers first RULE-B", "Unrelated one-off thought zebra"])
    note(vault, "2026-10-08-retro-b2.md", "2026-10-08", "p2",
         ["Never chain preflight and commit RULE-A", "Grep every caller before edits RULE-B"])
    note(vault, "2026-10-15-retro-c3.md", "2026-10-15", "p1",
         ["Preflight as its own call RULE-A", "Another unrelated one-off walrus"])
    base = ["--vault", str(vault), "--insights-folder", "claude-insights", "--state-dir", str(state)]
    return vault, state, base


def run(base, *args):
    return retro_rollup.main([args[0], *base, *args[1:]])


def retro_bytes(vault: Path) -> dict:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (vault / "claude-insights").glob("*-retro-*.md")}


def lesson_notes(vault: Path) -> dict:
    d = vault / "claude-insights" / "lessons"
    return {p.name: p.read_text() for p in d.glob("*.md")} if d.exists() else {}


def state_of(state: Path) -> dict:
    return json.loads((state / "lessons-state.json").read_text())


def test_update_groups_and_writes_notes_then_rerun_writes_nothing(env, capsys):
    vault, state, base = env
    assert run(base, "update") == 0
    notes = lesson_notes(vault)
    assert len(notes) == 2
    s = state_of(state)
    sizes = sorted(len(l["members"]) for l in s["lessons"].values())
    assert sizes == [2, 3]
    assert {l["name"] for l in s["lessons"].values()} == {"Follow rule A", "Follow rule B"}
    mtimes = {p: p.stat().st_mtime_ns for p in (vault / "claude-insights" / "lessons").glob("*.md")}
    capsys.readouterr()
    assert run(base, "update") == 0
    assert "notes written: 0" in capsys.readouterr().out
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes
    assert (state / "lessons-verdicts.db").stat().st_mode & 0o777 == 0o600


def test_update_haiku_failure_is_unjudged_and_exit_1(env, monkeypatch, capsys):
    vault, state, base = env
    monkeypatch.setattr(lessons_verify, "run_haiku", lambda p, timeouts=(120, 240): None)
    assert run(base, "update") == 1
    out = capsys.readouterr().out
    assert "failed: 7" in out and "lessons: 0" in out
    assert state_of(state)["totals"]["unjudged"] == 7


def test_update_note_only_judges_that_notes_items(env):
    vault, state, base = env
    seen = []
    orig = lessons_verify.run_haiku

    def spy(p, timeouts=(120, 240)):
        if p.startswith("You decide"):
            seen.append(re.search(r"^ANCHOR: (.*)$", p, re.M).group(1))
        return orig(p)

    lessons_verify.run_haiku = spy
    try:
        assert run(base, "update", "--note", str(vault / "claude-insights" / "2026-10-15-retro-c3.md")) == 0
    finally:
        lessons_verify.run_haiku = orig
    assert seen and all("RULE-A" in a or "walrus" in a for a in seen)


def test_lock_held_exits_1_and_leaves_state(env, capsys):
    vault, state, base = env
    assert run(base, "update") == 0
    before = (state / "lessons-state.json").read_bytes()
    import fcntl
    fd = os.open(state / "lessons.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert run(base, "update") == 1
        assert "another rollup run holds the lock" in capsys.readouterr().err
    finally:
        os.close(fd)
    assert (state / "lessons-state.json").read_bytes() == before


def test_corrupt_state_moved_aside_and_rebuilt(env):
    vault, state, base = env
    state.mkdir()
    (state / "lessons-state.json").write_text("{not json")
    assert run(base, "update") == 0
    assert list(state.glob("lessons-state.json.corrupt-*"))
    assert len(state_of(state)["lessons"]) == 2


def _trace_items(vault, name, item_trace):
    p = vault / "claude-insights" / name
    lines = p.read_text().split("\n")
    out = []
    for line in lines:
        for text, trace in item_trace.items():
            if line == f"- [ ] {text}":
                line = f"{line} → {trace}"
        out.append(line)
    p.write_text("\n".join(out))


def test_refresh_tracking_reads_milestone_priority_and_dates(env, monkeypatch, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md",
                 {"Run preflight alone RULE-A": "[o/r#5](https://github.com/o/r/issues/5)"})
    assert run(base, "update") == 0

    def gh(args):
        assert args[:5] == ["issue", "view", "5", "--repo", "o/r"]
        return type("R", (), {"returncode": 0, "stdout": json.dumps({
            "state": "OPEN", "milestone": {"title": "v3.10"},
            "labels": [{"name": "enhancement"}, {"name": "P2-medium"}],
            "createdAt": "2026-10-05T10:00:00Z", "closedAt": None})})()

    monkeypatch.setattr(retro_rollup, "_run_gh", gh)
    assert run(base, "refresh-tracking") == 0
    s = state_of(state)
    lesson = next(l for l in s["lessons"].values() if l["tracking"])
    assert lesson["classify"]["bucket"] == "raise"
    assert lesson["classify"]["sessions_after_tracking"] == 2  # 10-08 and 10-15 are after 10-05
    text = next(t for t in lesson_notes(vault).values() if '"o/r#5"' in t)
    assert 'issue_milestone: "v3.10"' in text and 'issue_priority: "P2-medium"' in text


def test_refresh_tracking_gh_error_keeps_old_fields_exit_1(env, monkeypatch, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md",
                 {"Run preflight alone RULE-A": "[o/r#5](https://github.com/o/r/issues/5)"})
    assert run(base, "update") == 0
    s = state_of(state)
    s["tracking"]["issue:o/r#5"] = {"exists": True, "state": "OPEN", "milestone": "old", "priority": None,
                                    "created": "2026-10-02", "closed": None}
    (state / "lessons-state.json").write_text(json.dumps(s))
    monkeypatch.setattr(retro_rollup, "_run_gh",
                        lambda args: type("R", (), {"returncode": 1, "stdout": ""})())
    assert run(base, "refresh-tracking") == 1
    assert state_of(state)["tracking"]["issue:o/r#5"]["milestone"] == "old"
    assert "gh exited with code 1" in capsys.readouterr().out


def test_refresh_tracking_missing_memory_sets_gone(env, monkeypatch):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_gone`"})
    assert run(base, "update") == 0
    import memory_sources
    monkeypatch.setattr(memory_sources, "memory_sources", lambda host, *a, **k: [])
    assert run(base, "refresh-tracking") == 0
    lesson = next(l for l in state_of(state)["lessons"].values() if l["tracking"])
    assert lesson["classify"]["bucket"] == "file" and lesson["classify"]["tracking_gone"] is True


def test_backfill_dry_run_writes_nothing(env, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md",
                 {"Run preflight alone RULE-A": "[o/r#5](https://github.com/o/r/issues/5)"})
    assert run(base, "update") == 0
    before = retro_bytes(vault)
    capsys.readouterr()
    assert run(base, "backfill") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry-run" and result["planned"] == 2
    assert retro_bytes(vault) == before


def _accept(tmp_path, state, ref):
    lid = next(k for k, l in state_of(state)["lessons"].items() if l["tracking"])
    f = tmp_path / "accept.json"
    f.write_text(json.dumps([{"lesson_id": lid, "tracking_ref": ref}]))
    return f


def test_backfill_apply_traces_accepted_and_rerun_changes_nothing(env, tmp_path, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_grep`"})
    assert run(base, "update") == 0
    f = _accept(tmp_path, state, "feedback_grep")
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    b2 = (vault / "claude-insights" / "2026-10-08-retro-b2.md").read_text()
    assert "- [x] Grep every caller before edits RULE-B → memory `feedback_grep`" in b2
    assert "- [ ] Never chain preflight and commit RULE-A\n" in b2  # other lesson untouched
    before = retro_bytes(vault)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert retro_bytes(vault) == before


def test_backfill_apply_skips_stale_tracking_ref(env, tmp_path, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_grep`"})
    assert run(base, "update") == 0
    f = _accept(tmp_path, state, "feedback_other")
    before = retro_bytes(vault)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["skipped_entries"][0]["reason"] == "tracking changed"
    assert retro_bytes(vault) == before


def test_backfill_apply_after_note_edit_resolves_by_key(env, tmp_path):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_grep`"})
    assert run(base, "update") == 0
    f = _accept(tmp_path, state, "feedback_grep")
    p = vault / "claude-insights" / "2026-10-08-retro-b2.md"
    p.write_text(p.read_text().replace("## Process Improvements\n", "## Process Improvements\n- [ ] A new first item\n"))
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    text = p.read_text()
    assert "- [ ] A new first item\n" in text
    assert "- [x] Grep every caller before edits RULE-B → memory `feedback_grep`" in text


def test_backfill_reports_every_untraced_item_with_reason(env, tmp_path, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_grep`"})
    (vault / "claude-insights" / "2026-10-20-retro-bad.md").write_text("---\ntype: claude-retro\n")
    assert run(base, "update") == 0
    f = _accept(tmp_path, state, "feedback_grep")
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    result = json.loads(capsys.readouterr().out)
    reasons = sorted(r["reason"] for r in result["items"])
    assert reasons == ["lesson-untracked"] * 3 + ["one-off"] * 2 + ["unreadable"]
    assert result["untraced_open"] == 6


def test_report_json_contract_fields(env, capsys):
    vault, state, base = env
    assert run(base, "update") == 0
    capsys.readouterr()
    assert run(base, "report", "--json") == 0
    rep = json.loads(capsys.readouterr().out)
    assert set(rep) == {"generated", "totals", "lessons"}
    assert set(rep["lessons"][0]) == {
        "id", "name", "bucket", "dormant", "sessions", "recent_sessions", "projects", "first_seen",
        "last_seen", "tracking", "tracking_ref", "issue_state", "issue_milestone", "issue_priority",
        "sessions_after_tracking", "snoozed_until", "members"}
    assert "RULE" not in json.dumps(rep["lessons"][0]["members"])


def test_snooze_carries_through_merge_and_split(env, tmp_path):
    vault, state, base = env
    assert run(base, "update") == 0
    s = state_of(state)
    lid, fn = next((k, v) for k, v in s["notes"].items() if len(s["lessons"][k]["members"]) == 3)
    p = vault / "claude-insights" / "lessons" / fn
    p.write_text(p.read_text().replace("snoozed_until: null", 'snoozed_until: "2026-12-01"'))
    note(vault, "2026-10-22-retro-d4.md", "2026-10-22", "p3", ["Preflight runs by itself RULE-A"])
    assert run(base, "update") == 0
    s2 = state_of(state)
    assert lid in s2["lessons"] and len(s2["lessons"][lid]["members"]) == 4
    assert 'snoozed_until: "2026-12-01"' in (vault / "claude-insights" / "lessons" / s2["notes"][lid]).read_text()


def test_existing_lesson_keeps_name_and_filename(env):
    vault, state, base = env
    assert run(base, "update") == 0
    s = state_of(state)
    lid = next(k for k, l in s["lessons"].items() if len(l["members"]) == 3)
    s["lessons"][lid]["name"] = "Custom kept name"
    (state / "lessons-state.json").write_text(json.dumps(s))
    note(vault, "2026-10-22-retro-d4.md", "2026-10-22", "p3", ["Preflight runs by itself RULE-A"])
    assert run(base, "update") == 0
    s2 = state_of(state)
    assert s2["lessons"][lid]["name"] == "Custom kept name"
    assert s2["notes"][lid] == s["notes"][lid]


def test_rebuild_makes_no_verdict_calls(env, monkeypatch):
    vault, state, base = env
    assert run(base, "update") == 0
    note(vault, "2026-10-22-retro-d4.md", "2026-10-22", "p3", ["Brand new thing RULE-Z"])
    calls = []
    monkeypatch.setattr(lessons_verify, "run_haiku", lambda p, timeouts=(120, 240): calls.append(p) or None)
    assert run(base, "rebuild") == 0
    assert not [c for c in calls if c.startswith("You decide")]
    assert state_of(state)["totals"]["unjudged"] == 1


@pytest.mark.parametrize("argv", [[], ["bogus"], ["backfill", "--apply"], ["update", "--vault", "/nonexistent-vault-x"]])
def test_usage_error_exit_2(argv, tmp_path):
    if argv[:1] == ["backfill"]:
        argv = argv + ["--vault", str(tmp_path)]
    assert retro_rollup.main(argv) == 2


def test_no_live_paths_touched(env):
    # conftest gives every test a tmp HOME; --state-dir must win over the HOME default.
    vault, state, base = env
    assert run(base, "update") == 0
    default = Path.home() / ".claude" / "obsidian-brain"
    assert not (default / "lessons-verdicts.db").exists()
    assert not (default / "lessons-state.json").exists()
    assert (state / "lessons-verdicts.db").exists() and (state / "lessons-state.json").exists()


@pytest.mark.parametrize("cmd", ["update", "rebuild", "refresh-tracking"])
def test_no_retro_items_with_lessons_refuses_and_keeps_notes(env, cmd, capsys):
    # Retro notes gone mid-sync (or all unreadable) must not delete every lesson note.
    vault, state, base = env
    assert run(base, "update") == 0
    notes, before = lesson_notes(vault), state_of(state)
    for p in (vault / "claude-insights").glob("*-retro-*.md"):
        p.unlink()
    capsys.readouterr()
    assert run(base, cmd) == 1
    assert "refusing" in capsys.readouterr().err
    assert lesson_notes(vault) == notes
    assert state_of(state) == before


def test_missing_insights_folder_exits_2(env, capsys):
    vault, state, base = env
    base = [a if a != "claude-insights" else "no-such-folder" for a in base]
    assert run(base, "rebuild") == 2
    assert "insights folder not found" in capsys.readouterr().err
    assert not (state / "lessons-state.json").exists()


def test_no_retro_items_and_no_lessons_is_fine(tmp_path, monkeypatch):
    # A fresh vault with no retros yet is not an error.
    monkeypatch.setattr(lessons_verify, "run_haiku", lambda p, timeouts=(120, 240): pytest.fail("haiku called"))
    (tmp_path / "vault" / "claude-insights").mkdir(parents=True)
    base = ["--vault", str(tmp_path / "vault"), "--insights-folder", "claude-insights",
            "--state-dir", str(tmp_path / "state")]
    assert run(base, "update") == 0


@pytest.mark.parametrize("written", ["2026-12-01", '"2026-12-01"', "'2026-12-01'"])
def test_snooze_as_obsidian_writes_it_survives_update(env, written):
    # Obsidian's date property editor writes the date unquoted; it must not be wiped to null.
    vault, state, base = env
    assert run(base, "update") == 0
    lid, fn = next(iter(state_of(state)["notes"].items()))
    p = vault / "claude-insights" / "lessons" / fn
    p.write_text(p.read_text().replace("snoozed_until: null", f"snoozed_until: {written}"))
    note(vault, "2026-10-22-retro-d4.md", "2026-10-22", "p3", ["Preflight runs by itself RULE-A"])
    assert run(base, "update") == 0
    assert 'snoozed_until: "2026-12-01"' in p.read_text()


def test_report_json_reads_snooze_live_from_the_note(env, capsys):
    vault, state, base = env
    assert run(base, "update") == 0
    lid, fn = next(iter(state_of(state)["notes"].items()))
    p = vault / "claude-insights" / "lessons" / fn
    p.write_text(p.read_text().replace("snoozed_until: null", "snoozed_until: 2026-12-01"))
    capsys.readouterr()
    assert run(base, "report", "--json") == 0
    rep = {l["id"]: l for l in json.loads(capsys.readouterr().out)["lessons"]}
    assert rep[lid]["snoozed_until"] == "2026-12-01"
    assert all(l["snoozed_until"] is None for k, l in rep.items() if k != lid)
