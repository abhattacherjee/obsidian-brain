"""retro_rollup CLI (#471). Temp vault and state dir only; Haiku and gh are faked."""
from __future__ import annotations

import json
import subprocess
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
    assert run(base, "update") == 1  # an unreadable retro note is a partial failure
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


def test_snooze_survives_membership_growth(env, tmp_path):
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


# --- review fixes -------------------------------------------------------------

def _cache_rows(state):
    cache = lessons_verify.VerdictCache(state / "lessons-verdicts.db")
    try:
        return (cache._conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0],
                cache._conn.execute("SELECT COUNT(*) FROM anchors").fetchone()[0])
    finally:
        cache.close()


def _lesson_with(state, size):
    s = state_of(state)
    return next(k for k, l in s["lessons"].items() if len(l["members"]) == size), s


def test_backfill_traces_the_first_of_two_identical_items(env, tmp_path):
    vault, state, base = env
    a1 = vault / "claude-insights" / "2026-10-01-retro-a1.md"
    a1.write_text(a1.read_text().replace(
        "- [ ] Run preflight alone RULE-A\n",
        "- [ ] Run preflight alone RULE-A\n- [ ] Run preflight alone RULE-A\n"))
    _trace_items(vault, "2026-10-15-retro-c3.md", {"Preflight as its own call RULE-A": "memory `feedback_x`"})
    assert run(base, "update") == 0
    f = _accept(tmp_path, state, "feedback_x")
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    lines = [l for l in a1.read_text().splitlines() if "Run preflight alone" in l]
    assert lines == ["- [x] Run preflight alone RULE-A → memory `feedback_x`",
                     "- [ ] Run preflight alone RULE-A"]


def _tree(vault, state):
    """Every lesson file, the state file and the retired list, as bytes."""
    d = vault / "claude-insights" / "lessons"
    files = {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}
    return files, (state / "lessons-state.json").read_bytes()


@pytest.mark.parametrize("cmd", ["update", "rebuild", "refresh-tracking"])
def test_unreadable_note_with_lessons_leaves_everything_as_it_was_and_exits_1(env, cmd, capsys):
    vault, state, base = env
    assert run(base, "update") == 0
    lid_b, s = _lesson_with(state, 2)  # RULE-B spans a1 and b2 only
    p = vault / "claude-insights" / "lessons" / s["notes"][lid_b]
    p.write_text(p.read_text().replace("snoozed_until: null", 'snoozed_until: "2026-12-01"'))
    before, rows = _tree(vault, state), _cache_rows(state)
    b2 = vault / "claude-insights" / "2026-10-08-retro-b2.md"
    b2.write_text("---\ntype: claude-retro\n")
    capsys.readouterr()
    assert run(base, cmd) == 1
    err = capsys.readouterr().err
    assert "1 unreadable" in err and str(b2) in err and "left as they were" in err
    assert _tree(vault, state) == before
    assert _cache_rows(state) == rows


def test_unreadable_note_never_puts_one_item_in_two_lessons(grouped):
    vault, state, base = grouped
    # LA = notes 1-4 (G1), LB = notes 5-6 (G2); note 1 then becomes unreadable.
    _notes_with_tags(vault, {1: "G1", 2: "G1", 3: "G1", 4: "G1", 5: "G2", 6: "G2"})
    assert run(base, "update") == 0
    before = _tree(vault, state)
    (vault / "claude-insights" / "2026-10-01-retro-n1.md").write_text("---\ntype: claude-retro\n")
    assert run(base, "update") == 1
    assert _tree(vault, state) == before
    members = [k for l in state_of(state)["lessons"].values() for k in l["members"]]
    assert len(members) == len(set(members)) == 6


def test_unreadable_note_with_no_lessons_still_groups_the_readable_items(env, capsys):
    vault, state, base = env
    (vault / "claude-insights" / "2026-10-20-retro-bad.md").write_text("---\ntype: claude-retro\n")
    assert run(base, "update") == 1
    assert len(state_of(state)["lessons"]) == 2
    assert "1 unreadable" in capsys.readouterr().err


def test_unreadable_note_runs_clean_after_it_is_fixed_and_prunes(env):
    vault, state, base = env
    assert run(base, "update") == 0
    note(vault, "2026-10-22-retro-d4.md", "2026-10-22", "p3", ["Brand new thing RULE-Z"])
    assert run(base, "update") == 0
    (vault / "claude-insights" / "2026-10-22-retro-d4.md").unlink()
    assert run(base, "rebuild") == 0  # nothing unreadable: pruning still happens
    assert _cache_rows(state)[1] == 7


def test_refresh_tracking_memory_scan_error_is_unknown_not_gone(env, monkeypatch, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_there`"})
    assert run(base, "update") == 0
    import memory_sources

    def failing(host, config=None, errors=None):
        errors.append({"path": "/denied", "error": "permission denied"})
        return []

    monkeypatch.setattr(memory_sources, "memory_sources", failing)
    capsys.readouterr()
    assert run(base, "refresh-tracking") == 1
    assert "permission denied" in capsys.readouterr().out
    s = state_of(state)
    assert s["tracking"]["memory:feedback_there"] == {"exists": None}
    lesson = next(l for l in s["lessons"].values() if l["tracking"])
    assert lesson["classify"]["tracking_gone"] is False and lesson["classify"]["bucket"] != "file"


@pytest.mark.parametrize("config, flag, key", [
    ({"lessons_concurrency": 0}, [], "lessons_concurrency"),
    ({"lessons_concurrency": 17}, [], "lessons_concurrency"),
    ({"lessons_concurrency": "x"}, [], "lessons_concurrency"),
    ({"lessons_concurrency": True}, [], "lessons_concurrency"),
    ({}, ["--concurrency", "0"], "concurrency"),
    ({}, ["--concurrency", "17"], "concurrency"),
    ({"lessons_recent_days": 0}, [], "lessons_recent_days"),
    ({"lessons_recent_days": "x"}, [], "lessons_recent_days"),
    ({"lessons_recent_days": 1.5}, [], "lessons_recent_days"),
])
def test_bad_numeric_settings_exit_2_naming_the_key(env, monkeypatch, capsys, config, flag, key):
    import obsidian_utils
    vault, state, base = env
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: config)
    assert run(base, "update", *flag) == 2
    assert key in capsys.readouterr().err
    assert not (state / "lessons-state.json").exists()


@pytest.mark.parametrize("config, flag", [
    ({"lessons_concurrency": 16, "lessons_recent_days": 1}, []),
    ({"lessons_concurrency": 1}, []),
    ({}, ["--concurrency", "16"]),
])
def test_numeric_settings_at_the_edges_are_accepted(env, monkeypatch, config, flag):
    import obsidian_utils
    vault, state, base = env
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: config)
    assert run(base, "update", *flag) == 0


def test_lock_oserror_exits_1_without_traceback(env, tmp_path, capsys):
    vault, state, base = env
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("x")
    base = base[:-1] + [str(blocker)]
    assert run(base, "update") == 1
    assert "retro_rollup:" in capsys.readouterr().err


def test_tracking_since_stays_put_when_backfill_traces_older_notes(env, tmp_path):
    vault, state, base = env
    _trace_items(vault, "2026-10-15-retro-c3.md", {"Preflight as its own call RULE-A": "memory `feedback_x`"})
    assert run(base, "update") == 0
    lid, s = _lesson_with(state, 3)
    assert s["lessons"][lid]["tracking"]["since"] == "2026-10-15"
    f = _accept(tmp_path, state, "feedback_x")
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert "memory `feedback_x`" in (vault / "claude-insights" / "2026-10-01-retro-a1.md").read_text()
    assert run(base, "update") == 0
    rec = state_of(state)["lessons"][lid]
    assert rec["tracking"]["since"] == "2026-10-15"
    assert rec["classify"]["bucket"] == "quiet" and rec["classify"]["sessions_after_tracking"] == 0


def test_backfill_skips_accept_for_a_lesson_whose_tracking_is_gone(env, tmp_path, monkeypatch, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_gone`"})
    assert run(base, "update") == 0
    import memory_sources
    monkeypatch.setattr(memory_sources, "memory_sources", lambda host, *a, **k: [])
    assert run(base, "refresh-tracking") == 0
    f = _accept(tmp_path, state, "feedback_gone")
    before = retro_bytes(vault)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    result = json.loads(capsys.readouterr().out)
    assert [e["reason"] for e in result["skipped_entries"]] == ["tracking gone"]
    assert retro_bytes(vault) == before


def _snooze(vault, state, lid, day):
    p = vault / "claude-insights" / "lessons" / state_of(state)["notes"][lid]
    p.write_text(p.read_text().replace("snoozed_until: null", f'snoozed_until: "{day}"'))


def _retag(vault, name, old, new):
    p = vault / "claude-insights" / name
    p.write_text(p.read_text().replace(old, new))


GROUP = re.compile(r"\bG\d\b")


def group_haiku(prompt: str):
    """Same lesson iff anchor and candidate share a G<n> tag; an item can carry two."""
    if prompt.startswith("Give each lesson"):
        return json.dumps({n: f"Rule {n}" for n in re.findall(r"^LESSON (\d+):", prompt, re.M)})
    anchor = set(GROUP.findall(re.search(r"^ANCHOR: (.*)$", prompt, re.M).group(1)))
    return json.dumps({n: "same" if anchor & set(GROUP.findall(t)) else "different"
                       for n, t in re.findall(r"^CANDIDATE (\d+): (.*)$", prompt, re.M)})


@pytest.fixture
def grouped(tmp_path, monkeypatch):
    vault, state = tmp_path / "vault", tmp_path / "state"
    monkeypatch.setattr(lessons_verify, "run_haiku", lambda p, timeouts=(120, 240): group_haiku(p))
    base = ["--vault", str(vault), "--insights-folder", "claude-insights", "--state-dir", str(state)]
    return vault, state, base


def _notes_with_tags(vault, tags):
    for i, tag in tags.items():
        note(vault, f"2026-10-0{i}-retro-n{i}.md", f"2026-10-0{i}", f"p{i}", [f"Do the thing number {i} {tag}"])


def _snoozed_dates(vault, state):
    import lessons_notes
    return {lid: lessons_notes.read_snooze(vault / "claude-insights" / "lessons" / fn)
            for lid, fn in state_of(state)["notes"].items()}


def test_snooze_is_inherited_by_the_child_of_a_split(grouped):
    vault, state, base = grouped
    # c (note 3) bridges two pairs, so all five items form one lesson.
    _notes_with_tags(vault, {1: "G1", 2: "G1", 3: "G1 G2", 4: "G2", 5: "G2"})
    assert run(base, "update") == 0
    (old_id,) = state_of(state)["lessons"]
    _snooze(vault, state, old_id, "2026-12-01")
    (vault / "claude-insights" / "2026-10-03-retro-n3.md").unlink()  # the bridge goes
    assert run(base, "update") == 0
    s = state_of(state)
    assert len(s["lessons"]) == 2 and old_id in s["lessons"]
    child = next(l for l in s["lessons"] if l != old_id)
    assert s["lessons"][child]["split_from"] == old_id
    assert _snoozed_dates(vault, state) == {old_id: "2026-12-01", child: "2026-12-01"}


def test_merged_lesson_keeps_the_snooze_of_a_retired_id(grouped):
    vault, state, base = grouped
    _notes_with_tags(vault, {1: "G1", 2: "G1", 4: "G2", 5: "G2"})
    assert run(base, "update") == 0
    survivor, retiring = sorted(state_of(state)["lessons"])  # equal overlap: the smaller id survives
    _snooze(vault, state, retiring, "2026-12-01")
    _notes_with_tags(vault, {3: "G1 G2"})  # the bridge arrives
    assert run(base, "update") == 0
    s = state_of(state)
    assert list(s["lessons"]) == [survivor] and len(s["lessons"][survivor]["members"]) == 5
    assert _snoozed_dates(vault, state) == {survivor: "2026-12-01"}
    assert json.loads((vault / "claude-insights" / "lessons" / "retired.json").read_text()) == {
        retiring: {"merged_into": survivor}}


def test_memory_scan_error_keeps_a_known_gone_memory_ref_gone(env, tmp_path, monkeypatch, capsys):
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_gone`"})
    assert run(base, "update") == 0
    import memory_sources
    monkeypatch.setattr(memory_sources, "memory_sources", lambda host, *a, **k: [])
    assert run(base, "refresh-tracking") == 0
    assert state_of(state)["tracking"]["memory:feedback_gone"] == {"exists": False}

    def failing(host, config=None, errors=None):
        errors.append({"path": "/denied", "error": "permission denied"})
        return []

    monkeypatch.setattr(memory_sources, "memory_sources", failing)
    assert run(base, "refresh-tracking") == 1
    assert state_of(state)["tracking"]["memory:feedback_gone"] == {"exists": False}
    f = _accept(tmp_path, state, "feedback_gone")
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    result = json.loads(capsys.readouterr().out)
    assert [e["reason"] for e in result["skipped_entries"]] == ["tracking gone"]


def test_split_child_with_a_new_id_keeps_the_since_of_the_lesson_it_came_from(grouped, tmp_path):
    vault, state, base = grouped
    _notes_with_tags(vault, {1: "G1", 2: "G1", 3: "G1 G2", 4: "G2", 5: "G2"})
    _trace_items(vault, "2026-10-05-retro-n5.md", {"Do the thing number 5 G2": "memory `feedback_x`"})
    assert run(base, "update") == 0
    (old_id,) = state_of(state)["lessons"]
    assert state_of(state)["lessons"][old_id]["tracking"]["since"] == "2026-10-05"
    f = _accept(tmp_path, state, "feedback_x")
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0  # older notes now carry the trace
    (vault / "claude-insights" / "2026-10-03-retro-n3.md").unlink()  # the bridge goes: a split
    assert run(base, "update") == 0
    s = state_of(state)
    assert len(s["lessons"]) == 2
    assert {l.get("split_from") for l in s["lessons"].values()} == {None, old_id}
    for rec in s["lessons"].values():
        assert rec["tracking"]["since"] == "2026-10-05"
        assert rec["classify"]["bucket"] == "quiet"


def test_report_table_shows_items_dates_and_tracking(env, capsys):
    # #471 C.2: the read-only table names item count, sessions, projects, first/last seen and tracking.
    vault, state, base = env
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_grep`"})
    assert run(base, "update") == 0
    capsys.readouterr()
    assert run(base, "report") == 0
    out = capsys.readouterr().out
    assert re.search(r"^- 3 items · \d+/3 sessions · 2 projects · 2026-10-01 to 2026-10-15 · no tracking · ", out, re.M), out
    assert re.search(r"^- 2 items · \d+/2 sessions · 2 projects · 2026-10-01 to 2026-10-08 · feedback_grep · ", out, re.M), out


# --- file an issue per accepted untracked lesson (#471 Task 9) --------------------

class FakeGh:
    """Fakes `gh issue create` and `gh issue list`: records calls, reads the body file while it exists.

    A created issue is remembered (number, body) so a later `issue list` can find it. `record_then`
    makes create raise after the issue exists, like a gh timeout that came too late. `list_fail` is
    None, "rc", "timeout", "json" or "shape" and breaks `issue list`.
    """

    def __init__(self, number=7, stdout=None, returncode=0, record_then=None, list_fail=None,
                 list_pad=0):
        self.calls, self.bodies, self.modes, self.list_calls = [], [], [], []
        self.number, self.stdout, self.returncode = number, stdout, returncode
        self.record_then, self.list_fail, self.list_pad = record_then, list_fail, list_pad
        self.issues, self.on_create = [], None

    def __call__(self, args, timeout=10):
        if args[:2] == ["issue", "list"]:
            return self._list(args)
        assert args[:2] == ["issue", "create"], args
        self.calls.append(args)
        body = Path(args[args.index("--body-file") + 1])
        self.bodies.append(body.read_text())
        self.modes.append(body.stat().st_mode & 0o777)
        if self.on_create:
            self.on_create()
        repo = args[args.index("--repo") + 1]
        if self.returncode == 0 and (self.stdout is None or self.record_then):
            self.issues.append({"number": self.number, "body": self.bodies[-1]})
        if self.record_then:
            raise self.record_then
        out = self.stdout if self.stdout is not None else (
            f"Creating issue in {repo}\n\nhttps://github.com/{repo}/issues/{self.number}\n")
        return type("R", (), {"returncode": self.returncode, "stdout": out, "stderr": "boom"})()

    def _list(self, args):
        self.list_calls.append(args)
        if self.list_fail == "timeout":
            raise subprocess.TimeoutExpired("gh", 1)
        out = json.dumps(self.issues + [{"number": 900 + i, "body": "unrelated"}
                                        for i in range(self.list_pad)])
        if self.list_fail == "json":
            out = "not json"
        elif self.list_fail == "shape":
            out = json.dumps({"issues": []})
        rc = 1 if self.list_fail == "rc" else 0
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": "boom"})()


@pytest.fixture
def filing(env, monkeypatch):
    vault, state, base = env
    gh = FakeGh()
    monkeypatch.setattr(retro_rollup, "_run_gh", gh)
    monkeypatch.setattr(retro_rollup, "_git_origin", lambda path: None)
    import obsidian_utils
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {})
    assert run(base, "update") == 0
    return vault, state, base, gh


def _lesson_named(state, size):
    s = state_of(state)
    return next(k for k, l in s["lessons"].items() if len(l["members"]) == size)


def _file_accept(tmp_path, entries):
    f = tmp_path / "accept-file.json"
    f.write_text(json.dumps(entries))
    return f


def _pending(lid, repo):
    return {"pending": True, "repo": repo, "marker": f"<!-- retro-lesson:{lid} -->"}


def _issue_trace(repo, n):
    return f"[{repo}#{n}](https://github.com/{repo}/issues/{n})"


def test_file_issue_files_one_issue_and_traces_every_member(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["filed"] == [{"lesson": lid, "repo": "o/r", "issue": 7}]
    assert len(gh.calls) == 1
    args = gh.calls[0]
    assert args[:6] == ["issue", "create", "--repo", "o/r", "--title", "Follow rule A"]
    assert gh.modes == [0o600] and gh.bodies[0]
    body = gh.bodies[0]
    assert lid in body and "p1" in body and "p2" in body and "2026-10-01" in body
    assert "2026-10-01-retro-a1#1" in body and "RULE" not in body and "preflight" not in body.lower()
    assert not list(state.glob("*.md")) and not list(state.glob(".gh-body*"))  # temp body removed
    trace = _issue_trace("o/r", 7)
    for name, text in (("2026-10-01-retro-a1.md", "Run preflight alone RULE-A"),
                       ("2026-10-08-retro-b2.md", "Never chain preflight and commit RULE-A"),
                       ("2026-10-15-retro-c3.md", "Preflight as its own call RULE-A")):
        assert f"- [ ] {text} → {trace}" in (vault / "claude-insights" / name).read_text()
    assert state_of(state)["filed"] == {lid: "o/r#7"}
    assert "RULE-B" in (vault / "claude-insights" / "2026-10-01-retro-a1.md").read_text()


def test_file_issue_rerun_files_nothing_and_after_update_too(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    before = retro_bytes(vault)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert json.loads(capsys.readouterr().out)["filed"] == []
    assert run(base, "update") == 0  # the lesson now has tracking from its member traces
    assert state_of(state)["lessons"][lid]["tracking"]["ref"] == "o/r#7"
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert len(gh.calls) == 1 and retro_bytes(vault) == before


def test_crash_after_filing_then_rerun_reuses_the_saved_issue(filing, tmp_path, monkeypatch):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    before = retro_bytes(vault)
    real = retro_rollup.retro_trace.apply_traces

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(retro_rollup.retro_trace, "apply_traces", boom)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 1
    assert retro_bytes(vault) == before
    assert state_of(state)["filed"] == {lid: "o/r#7"}  # saved before any trace
    monkeypatch.setattr(retro_rollup.retro_trace, "apply_traces", real)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert len(gh.calls) == 1
    assert _issue_trace("o/r", 7) in (vault / "claude-insights" / "2026-10-08-retro-b2.md").read_text()


def test_partly_traced_filed_lesson_is_finished_by_a_rerun(filing, tmp_path, monkeypatch):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    real = retro_rollup.retro_trace.apply_traces

    def first_only(vault_, path, traces, **k):
        if "b2" in str(path):
            raise OSError("locked")
        return real(vault_, path, traces, **k)

    monkeypatch.setattr(retro_rollup.retro_trace, "apply_traces", first_only)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 1
    assert run(base, "update") == 0  # now tracked by the two traces that landed
    monkeypatch.setattr(retro_rollup.retro_trace, "apply_traces", real)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert len(gh.calls) == 1
    assert _issue_trace("o/r", 7) in (vault / "claude-insights" / "2026-10-08-retro-b2.md").read_text()


def _filed_repo(filing, tmp_path, monkeypatch, entry, config=None, origin=None, size=3, origin_repo=None):
    """origin_repo patches _origin_repo itself (what _resolve_repo asks last), so a precedence test can
    show a higher source beating a real origin value; `origin` patches only the git call under it."""
    import obsidian_utils
    vault, state, base, gh = filing
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: config or {})
    monkeypatch.setattr(retro_rollup, "_git_origin", lambda path: origin(path) if origin else None)
    if origin_repo is not None:
        monkeypatch.setattr(retro_rollup, "_origin_repo", lambda project: origin_repo)
    lid = _lesson_named(state, size)
    f = _file_accept(tmp_path, [{"lesson_id": lid, **entry}])
    code = run(base, "backfill", "--apply", "--accept", str(f))
    return code, gh


def test_repo_accept_override_beats_config_beats_origin(filing, tmp_path, monkeypatch):
    cfg = {"project_repos": {"p1": "cfg/repo"}}
    code, gh = _filed_repo(filing, tmp_path, monkeypatch, {"file_issue": True, "repo": "acc/repo"}, cfg,
                           origin_repo="orig/repo")
    assert code == 0 and gh.calls[0][3] == "acc/repo"


def test_repo_config_beats_origin_and_uses_the_busiest_project(filing, tmp_path, monkeypatch):
    # RULE-A members: p1 in two retros, p2 in one -> p1.
    cfg = {"project_repos": {"p1": "cfg/one", "p2": "cfg/two"}}
    code, gh = _filed_repo(filing, tmp_path, monkeypatch, {"file_issue": True}, cfg, origin_repo="orig/repo")
    assert code == 0 and gh.calls[0][3] == "cfg/one"


def test_repo_tie_between_projects_goes_to_the_smaller_name(filing, tmp_path, monkeypatch):
    cfg = {"project_repos": {"p1": "cfg/one", "p2": "cfg/two"}}
    code, gh = _filed_repo(filing, tmp_path, monkeypatch, {"file_issue": True}, cfg, size=2,  # RULE-B: p1 and p2 once each
                           origin_repo="orig/repo")
    assert code == 0 and gh.calls[0][3] == "cfg/one"


def test_repo_falls_back_to_the_origin_of_the_workspace_checkout(filing, tmp_path, monkeypatch):
    import obsidian_utils
    root = tmp_path / "ws"
    (root / "p1").mkdir(parents=True)
    monkeypatch.setattr(obsidian_utils, "get_workspace_roots", lambda: [str(root)])
    asked = []

    def origin(path):
        asked.append(path)
        return "git@github.com:org/from-origin.git"

    code, gh = _filed_repo(filing, tmp_path, monkeypatch, {"file_issue": True}, {}, origin)
    assert code == 0 and gh.calls[0][3] == "org/from-origin"
    assert asked == [str(root / "p1")]


def test_no_repo_skips_the_entry_and_exits_1(filing, tmp_path, monkeypatch, capsys):
    vault, state, base, gh = filing
    before = retro_bytes(vault)
    capsys.readouterr()
    code, gh = _filed_repo(filing, tmp_path, monkeypatch, {"file_issue": True})
    assert code == 1 and gh.calls == [] and retro_bytes(vault) == before
    result = json.loads(capsys.readouterr().out)
    assert result["skipped_entries"][0]["reason"] == "no repo for project p1"
    assert "filed" not in state_of(state) or state_of(state)["filed"] == {}


def test_repo_from_config_is_validated(filing, tmp_path, monkeypatch, capsys):
    code, gh = _filed_repo(filing, tmp_path, monkeypatch, {"file_issue": True},
                           {"project_repos": {"p1": "bad repo; rm -rf /"}})
    assert code == 1 and gh.calls == []
    assert "invalid repo" in json.loads(capsys.readouterr().out)["skipped_entries"][0]["reason"]


def test_file_issue_for_a_tracked_lesson_is_skipped(env, tmp_path, monkeypatch, capsys):
    vault, state, base = env
    monkeypatch.setattr(retro_rollup, "_run_gh", FakeGh())
    _trace_items(vault, "2026-10-01-retro-a1.md", {"Grep callers first RULE-B": "memory `feedback_grep`"})
    assert run(base, "update") == 0
    lid = next(k for k, l in state_of(state)["lessons"].items() if l["tracking"])
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    before = retro_bytes(vault)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert json.loads(capsys.readouterr().out)["skipped_entries"][0]["reason"] == "already tracked"
    assert retro_bytes(vault) == before


@pytest.mark.parametrize("stdout, returncode", [
    ("", 0), ("created\n", 0), ("https://github.com/other/repo/issues/7\n", 0),
    ("https://github.com/o/r/issues/7/extra\n", 0), ("https://github.com/o/r/issues/0\n", 0),
    ("https://github.com/o/r/issues/7\n", 1)])
def test_bad_gh_output_is_a_failure_with_nothing_traced_or_saved(filing, tmp_path, stdout, returncode, monkeypatch):
    vault, state, base, gh = filing
    gh2 = FakeGh(stdout=stdout, returncode=returncode)
    monkeypatch.setattr(retro_rollup, "_run_gh", gh2)
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    before = retro_bytes(vault)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 1
    assert retro_bytes(vault) == before
    # The issue may exist, so the pending record stays for the next run to reconcile.
    assert state_of(state)["filed"] == {lid: _pending(lid, "o/r")}


def test_gh_create_matching_repo_case_insensitively_is_accepted(filing, tmp_path, monkeypatch):
    vault, state, base, gh = filing
    monkeypatch.setattr(retro_rollup, "_run_gh",
                        FakeGh(stdout="https://github.com/O/R/issues/9\n"))
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert state_of(state)["filed"] == {lid: "o/r#9"}


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("gh", 1), FileNotFoundError("gh"),
                                 PermissionError("gh")], ids=["timeout", "no-gh", "other-oserror"])
def test_gh_timeout_or_missing_gh_is_a_failure(filing, tmp_path, monkeypatch, capsys, exc):
    # Fresh vault and state per case, so each one reaches `issue create`.
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    creates = []

    def fail(args, timeout=10):
        if args[:2] == ["issue", "create"]:
            creates.append(args)
            raise exc
        return gh(args, timeout)

    monkeypatch.setattr(retro_rollup, "_run_gh", fail)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 1
    assert len(creates) == 1
    entries = json.loads(capsys.readouterr().out)["skipped_entries"]
    assert len(entries) == 1 and entries[0]["reason"].startswith("filing failed")
    assert state_of(state)["filed"] == {lid: _pending(lid, "o/r")}


def test_dry_run_lists_planned_filings_and_calls_no_gh(env, monkeypatch, capsys):
    vault, state, base = env
    import obsidian_utils
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {"project_repos": {"p1": "o/r"}})
    monkeypatch.setattr(retro_rollup, "_git_origin", lambda path: None)
    assert run(base, "update") == 0  # the env gh fake fails the test if called
    before, state_before = retro_bytes(vault), (state / "lessons-state.json").read_bytes()
    capsys.readouterr()
    assert run(base, "backfill") == 0
    result = json.loads(capsys.readouterr().out)
    assert {(p["repo"], p["title"]) for p in result["planned_filings"]} == {
        ("o/r", "Follow rule A"), ("o/r", "Follow rule B")}
    assert all(p["members"] >= 2 and p["lesson"] for p in result["planned_filings"])
    assert result["filed"] == []
    assert retro_bytes(vault) == before and (state / "lessons-state.json").read_bytes() == state_before


def test_dry_run_planned_filing_without_a_repo_says_so(env, monkeypatch, capsys):
    vault, state, base = env
    import obsidian_utils
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {})
    monkeypatch.setattr(retro_rollup, "_git_origin", lambda path: None)
    assert run(base, "update") == 0
    capsys.readouterr()
    assert run(base, "backfill") == 0
    plans = json.loads(capsys.readouterr().out)["planned_filings"]
    assert plans and all(p["repo"] is None and p["reason"] == "no repo for project p1" for p in plans)


def test_note_edit_between_plan_and_apply_is_refused_not_mistraced(filing, tmp_path, monkeypatch):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    real = retro_rollup.retro_trace.apply_traces
    b2 = vault / "claude-insights" / "2026-10-08-retro-b2.md"

    def racing(vault_, path, traces, **k):
        if str(path).endswith("b2.md"):  # an item lands above the target after the plan was made
            b2.write_text(b2.read_text().replace(
                "## Process Improvements\n", "## Process Improvements\n- [ ] Sneaky new first item\n"))
        return real(vault_, path, traces, **k)

    monkeypatch.setattr(retro_rollup.retro_trace, "apply_traces", racing)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 1
    text = b2.read_text()
    assert "- [ ] Sneaky new first item\n" in text
    assert "- [ ] Never chain preflight and commit RULE-A\n" in text  # nothing traced, nothing ticked
    assert "github.com" not in text and "[x]" not in text


def test_state_without_a_filed_key_is_accepted(filing, tmp_path):
    vault, state, base, gh = filing
    s = state_of(state)
    s.pop("filed", None)
    (state / "lessons-state.json").write_text(json.dumps(s))
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert state_of(state)["filed"] == {lid: "o/r#7"}


@pytest.mark.parametrize("entry", [
    {"lesson_id": "L1", "file_issue": True, "tracking_ref": "x"},
    {"lesson_id": "L1", "file_issue": False},
    {"lesson_id": "L1", "file_issue": "yes"},
    {"lesson_id": "L1", "file_issue": True, "repo": "no spaces/allowed"},
    {"lesson_id": "L1", "file_issue": True, "repo": 5},
    {"lesson_id": "L1", "tracking_ref": "x", "repo": "o/r"},
])
def test_bad_accept_entries_exit_2(env, tmp_path, entry):
    vault, state, base = env
    f = _file_accept(tmp_path, [entry])
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 2


@pytest.mark.parametrize("url, want", [
    ("https://github.com/o/r", "o/r"), ("https://github.com/o/r.git", "o/r"),
    ("https://github.com/o/r/", "o/r"), ("https://user@github.com/o/r.git", "o/r"),
    ("git@github.com:o/r.git", "o/r"), ("git@github.com:o/r", "o/r"),
    ("ssh://git@github.com/o/r.git", "o/r"), ("https://github.com/a.b/c_d-e", "a.b/c_d-e"),
    ("http://github.com/o/r", None), ("https://gitlab.com/o/r", None),
    ("https://github.com.evil.com/o/r", None), ("https://evil.com/github.com/o/r", None),
    ("git@github.com:o/r/extra", None), ("https://github.com/o", None),
    ("file:///tmp/o/r", None), ("/local/path/o/r", None), ("https://github.com/o/r r", None),
    ("", None), ("git@github.com:o/r;rm", None)])
def test_repo_from_remote(url, want):
    assert retro_rollup._repo_from_remote(url) == want


def test_git_origin_reads_the_real_remote(tmp_path):
    import subprocess
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, stdin=subprocess.DEVNULL)
    assert retro_rollup._git_origin(str(repo)) is None  # no origin
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:me/proj.git"],
                   check=True, stdin=subprocess.DEVNULL)
    assert retro_rollup._git_origin(str(repo)) == "git@github.com:me/proj.git"
    assert retro_rollup._git_origin(str(tmp_path / "missing")) is None



# --- two-phase filing: pending record, marker, reconcile (#471 review C-013, X-017) ---

def _accept_file_issue(tmp_path, lid, repo="o/r"):
    return _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": repo}])


def _set_filed(state, lid, value):
    s = state_of(state)
    s["filed"] = {lid: value}
    (state / "lessons-state.json").write_text(json.dumps(s))


def test_pending_record_is_saved_before_gh_and_the_marker_is_the_first_body_line(filing, tmp_path):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    seen = []
    gh.on_create = lambda: seen.append(state_of(state)["filed"])
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 0
    assert seen == [{lid: _pending(lid, "o/r")}]
    assert gh.bodies[0].splitlines()[0] == f"<!-- retro-lesson:{lid} -->"
    assert state_of(state)["filed"] == {lid: "o/r#7"}


def test_failed_pending_save_means_no_gh_call_and_exit_1(filing, tmp_path, monkeypatch, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    before = retro_bytes(vault)

    def no_save(ctx, st):
        raise OSError("disk full")

    monkeypatch.setattr(retro_rollup, "save_state", no_save)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 1
    assert gh.calls == [] and retro_bytes(vault) == before
    assert "disk full" in json.loads(capsys.readouterr().out)["skipped_entries"][0]["reason"]


def test_save_fails_after_gh_created_the_issue_then_rerun_adopts_it(filing, tmp_path, monkeypatch, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _accept_file_issue(tmp_path, lid)
    real_save = retro_rollup.save_state

    def save_unless_filed(ctx, st):
        if gh.calls:
            raise OSError("disk full")
        real_save(ctx, st)

    monkeypatch.setattr(retro_rollup, "save_state", save_unless_filed)
    before = retro_bytes(vault)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 1
    assert retro_bytes(vault) == before and len(gh.calls) == 1
    assert state_of(state)["filed"] == {lid: _pending(lid, "o/r")}
    monkeypatch.setattr(retro_rollup, "save_state", real_save)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert len(gh.calls) == 1 and len(gh.list_calls) == 1  # adopted, not filed again
    out = json.loads(capsys.readouterr().out)
    assert out["filed"] == [] and out["reconciled"] == [{"lesson": lid, "repo": "o/r", "issue": 7,
                                                         "outcome": "adopted"}]
    assert state_of(state)["filed"] == {lid: "o/r#7"}
    assert _issue_trace("o/r", 7) in (vault / "claude-insights" / "2026-10-08-retro-b2.md").read_text()
    listing = gh.list_calls[0]
    assert listing == ["issue", "list", "--repo", "o/r", "--state", "all", "--author", "@me",
                       "--json", "number,body,state,createdAt,closedAt,milestone", "--limit", "1000"]


def test_gh_create_timeout_after_the_issue_exists_is_adopted_not_refiled(filing, tmp_path, monkeypatch):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _accept_file_issue(tmp_path, lid)
    slow = FakeGh(number=11, record_then=subprocess.TimeoutExpired("gh", 60))
    monkeypatch.setattr(retro_rollup, "_run_gh", slow)
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 1
    assert len(slow.calls) == 1 and state_of(state)["filed"] == {lid: _pending(lid, "o/r")}
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert len(slow.calls) == 1  # still one create
    assert state_of(state)["filed"] == {lid: "o/r#11"}
    assert _issue_trace("o/r", 11) in (vault / "claude-insights" / "2026-10-01-retro-a1.md").read_text()


def test_pending_record_with_no_matching_issue_files_exactly_once(filing, tmp_path):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    gh.issues.append({"number": 3, "body": "<!-- retro-lesson:other -->\nsomething else"})
    _set_filed(state, lid, _pending(lid, "o/r"))
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 0
    assert len(gh.list_calls) == 1 and len(gh.calls) == 1
    assert state_of(state)["filed"] == {lid: "o/r#7"}


@pytest.mark.parametrize("fail, pad", [("rc", 0), ("timeout", 0), ("json", 0), ("shape", 0), (None, 1000)])
def test_pending_record_with_a_failed_search_never_files_blind(filing, tmp_path, monkeypatch, capsys, fail, pad):
    # pad=1000: a full page with no match may hide the issue.
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    broken = FakeGh(list_fail=fail, list_pad=pad)
    monkeypatch.setattr(retro_rollup, "_run_gh", broken)
    _set_filed(state, lid, _pending(lid, "o/r"))
    before = retro_bytes(vault)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 1
    assert broken.calls == [] and retro_bytes(vault) == before
    reason = json.loads(capsys.readouterr().out)["skipped_entries"][0]["reason"]
    assert reason == "cannot check for an earlier filing"
    assert state_of(state)["filed"] == {lid: _pending(lid, "o/r")}


def test_search_needs_the_exact_marker_not_just_the_lesson_id(filing, tmp_path):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    gh.issues.append({"number": 5, "body": f"mentions {lid} but has no marker"})
    gh.issues.append({"number": 6, "body": f"<!-- retro-lesson:{lid}x -->\nlonger id"})
    _set_filed(state, lid, _pending(lid, "o/r"))
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 0
    assert len(gh.calls) == 1 and state_of(state)["filed"] == {lid: "o/r#7"}


def test_dry_run_lists_a_pending_lesson_as_pending_reconcile(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    _set_filed(state, lid, _pending(lid, "o/r"))
    capsys.readouterr()
    assert run(base, "backfill") == 0
    plans = {p["lesson"]: p for p in json.loads(capsys.readouterr().out)["planned_filings"]}
    assert plans[lid]["reason"] == "pending reconcile" and plans[lid]["repo"] == "o/r"
    assert gh.calls == [] and gh.list_calls == []


def test_load_state_keeps_both_forms_and_drops_junk(filing):
    vault, state, base, gh = filing
    s = state_of(state)
    s["filed"] = {"a": "o/r#3", "b": _pending("b", "o/r"), "c": "garbage", "d": {"pending": True},
                  "e": {"pending": True, "repo": "bad repo"}, "f": 5}
    (state / "lessons-state.json").write_text(json.dumps(s))
    ctx = retro_rollup.Ctx(retro_rollup._parser().parse_args(
        ["backfill", "--vault", str(vault), "--state-dir", str(state)]))
    assert retro_rollup.load_state(ctx)["filed"] == {"a": "o/r#3", "b": _pending("b", "o/r")}


def test_issue_tracking_is_saved_with_the_filing_even_if_every_trace_write_fails(filing, tmp_path, monkeypatch, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)

    def boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(retro_rollup.retro_trace, "apply_traces", boom)
    before = retro_bytes(vault)
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 1
    assert retro_bytes(vault) == before
    assert state_of(state)["lessons"][lid]["tracking"] == {
        "kind": "issue", "ref": "o/r#7", "since": state_of(state)["lessons"][lid]["tracking"]["since"]}
    capsys.readouterr()
    assert run(base, "backfill") == 0  # dry run
    dry = json.loads(capsys.readouterr().out)
    members = {(p["note"], p["item"]) for p in dry["plan"] if p["lesson"] == lid}
    assert len(members) == 3 and all(p["trace"]["ref"] == "o/r#7" for p in dry["plan"] if p["lesson"] == lid)
    assert all(p["lesson"] != lid for p in dry["planned_filings"])
    assert run(base, "report", "--json") == 0
    rep = json.loads(capsys.readouterr().out)
    assert next(l for l in rep["lessons"] if l["id"] == lid)["tracking_ref"] == "o/r#7"
    assert run(base, "update") == 0  # a regroup must not drop the issue while no trace has landed
    capsys.readouterr()
    assert run(base, "report", "--json") == 0
    rep = json.loads(capsys.readouterr().out)
    assert next(l for l in rep["lessons"] if l["id"] == lid)["tracking_ref"] == "o/r#7"


def test_freshly_filed_lesson_is_quiet_not_raise(filing, tmp_path):
    # Sessions before the issue existed are not recurrences: the new issue counts as opened today.
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    f = _file_accept(tmp_path, [{"lesson_id": lid, "file_issue": True, "repo": "o/r"}])
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert run(base, "update") == 0
    s = state_of(state)
    c = s["lessons"][lid]["classify"]
    assert c["bucket"] == "quiet" and c["sessions_after_tracking"] == 0, c
    info = s["tracking"]["issue:o/r#7"]
    assert info["state"] == "OPEN" and info["created"] == s["generated"] and info["exists"] is True



def test_newest_page_not_full_and_no_match_files_without_a_search(filing, tmp_path):
    # The plain listing is complete when its page is not full, so no index-lagged search is needed.
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    gh.list_pad = 3
    _set_filed(state, lid, _pending(lid, "o/r"))
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 0
    assert len(gh.list_calls) == 1 and "--search" not in gh.list_calls[0] and len(gh.calls) == 1


def test_full_page_with_no_match_refuses_and_never_files_or_searches(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    gh.list_pad = 1000
    _set_filed(state, lid, _pending(lid, "o/r"))
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 1
    captured = capsys.readouterr()
    assert "more than 1000 of my issues in o/r; cannot rule out an earlier filing" in captured.err
    assert json.loads(captured.out)["skipped_entries"][0]["reason"] == "cannot check for an earlier filing"
    assert gh.calls == [] and len(gh.list_calls) == 1 and "--search" not in gh.list_calls[0]
    assert state_of(state)["filed"] == {lid: _pending(lid, "o/r")}


def test_full_page_that_holds_the_marker_is_adopted(filing, tmp_path):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    gh.list_pad = 999
    gh.issues.append({"number": 41, "body": f"<!-- retro-lesson:{lid} -->\nbody"})
    _set_filed(state, lid, _pending(lid, "o/r"))
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 0
    assert gh.calls == [] and state_of(state)["filed"] == {lid: "o/r#41"}


def test_adopted_issue_records_its_real_state_and_dates(filing, tmp_path):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    gh.issues.append({"number": 41, "body": f"<!-- retro-lesson:{lid} -->\nbody", "state": "CLOSED",
                      "createdAt": "2026-09-01T10:00:00Z", "closedAt": "2026-09-20T08:00:00Z",
                      "milestone": {"title": "v1"}})
    _set_filed(state, lid, _pending(lid, "o/r"))
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 0
    assert gh.calls == []
    s = state_of(state)
    assert s["tracking"]["issue:o/r#41"] == {"exists": True, "state": "CLOSED", "milestone": "v1",
                                             "priority": None, "created": "2026-09-01",
                                             "closed": "2026-09-20"}
    assert s["lessons"][lid]["tracking"] == {"kind": "issue", "ref": "o/r#41", "since": "2026-09-01"}


def _drop_tracking(state, lid):
    s = state_of(state)
    s["lessons"][lid]["tracking"] = None  # filed, saved with no tracking
    (state / "lessons-state.json").write_text(json.dumps(s))


def test_filed_lesson_with_no_saved_tracking_keeps_its_issue_after_update(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    _set_filed(state, lid, "o/r#5")
    _drop_tracking(state, lid)
    assert run(base, "update") == 0
    s = state_of(state)
    assert s["lessons"][lid]["tracking"]["ref"] == "o/r#5" and s["filed"][lid] == "o/r#5"
    capsys.readouterr()
    assert run(base, "backfill") == 0
    dry = json.loads(capsys.readouterr().out)
    assert all(p["lesson"] != lid for p in dry["planned_filings"])
    traced = [p for p in dry["plan"] if p["lesson"] == lid]
    assert len(traced) == 3 and all(p["trace"]["ref"] == "o/r#5" for p in traced)


def _bridge_lessons(vault, state, base, monkeypatch):
    monkeypatch.setattr(retro_rollup, "_run_gh", lambda *a, **k: pytest.fail("gh called"))
    monkeypatch.setattr(retro_rollup, "_git_origin", lambda path: None)
    import obsidian_utils
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {})


def test_filing_survives_a_merge_into_another_id(grouped, monkeypatch, capsys):
    vault, state, base = grouped
    _bridge_lessons(vault, state, base, monkeypatch)
    _notes_with_tags(vault, {1: "G1", 2: "G1", 4: "G2", 5: "G2"})
    assert run(base, "update") == 0
    survivor, retiring = sorted(state_of(state)["lessons"])
    s = state_of(state)
    s["filed"] = {retiring: "o/r#5"}
    (state / "lessons-state.json").write_text(json.dumps(s))
    _notes_with_tags(vault, {3: "G1 G2"})  # the bridge arrives: retiring merges into survivor
    assert run(base, "update") == 0
    s = state_of(state)
    assert list(s["lessons"]) == [survivor]
    assert s["lessons"][survivor]["tracking"]["ref"] == "o/r#5"
    assert s["filed"][survivor] == "o/r#5" and s["filed"][retiring] == "o/r#5"
    capsys.readouterr()
    assert run(base, "backfill") == 0
    assert json.loads(capsys.readouterr().out)["planned_filings"] == []


def test_filing_survives_a_split_into_two_children(grouped, monkeypatch):
    vault, state, base = grouped
    _bridge_lessons(vault, state, base, monkeypatch)
    _notes_with_tags(vault, {1: "G1", 2: "G1", 3: "G1 G2", 4: "G2", 5: "G2"})
    assert run(base, "update") == 0
    (old_id,) = state_of(state)["lessons"]
    s = state_of(state)
    s["filed"] = {old_id: "o/r#5"}
    (state / "lessons-state.json").write_text(json.dumps(s))
    (vault / "claude-insights" / "2026-10-03-retro-n3.md").unlink()  # the bridge goes
    assert run(base, "update") == 0
    s = state_of(state)
    assert len(s["lessons"]) == 2
    child = next(l for l in s["lessons"] if l != old_id)
    assert s["lessons"][child]["split_from"] == old_id
    for lid in (old_id, child):
        assert s["lessons"][lid]["tracking"]["ref"] == "o/r#5" and s["filed"][lid] == "o/r#5"


# --- pending filings survive a regroup (#471 re-check C-013, X-022) ---

def _merge_setup(vault, state, base, monkeypatch):
    _bridge_lessons(vault, state, base, monkeypatch)
    _notes_with_tags(vault, {1: "G1", 2: "G1", 4: "G2", 5: "G2"})
    assert run(base, "update") == 0
    survivor, retiring = sorted(state_of(state)["lessons"])
    return survivor, retiring


def _save_filed(state, filed):
    s = state_of(state)
    s["filed"] = filed
    (state / "lessons-state.json").write_text(json.dumps(s))


def test_merge_keeps_the_survivors_own_pending_record(grouped, monkeypatch):
    vault, state, base = grouped
    survivor, retiring = _merge_setup(vault, state, base, monkeypatch)
    _save_filed(state, {survivor: _pending(survivor, "o/r"), retiring: "o/r#5"})
    _notes_with_tags(vault, {3: "G1 G2"})
    assert run(base, "update") == 0
    s = state_of(state)
    assert s["filed"][survivor] == _pending(survivor, "o/r")
    assert s["lessons"][survivor]["tracking"] is None


def test_pending_record_of_a_merged_in_lesson_moves_to_the_survivor(grouped, monkeypatch, capsys):
    vault, state, base = grouped
    survivor, retiring = _merge_setup(vault, state, base, monkeypatch)
    _save_filed(state, {retiring: _pending(retiring, "o/r")})
    _notes_with_tags(vault, {3: "G1 G2"})
    assert run(base, "update") == 0
    s = state_of(state)
    assert list(s["lessons"]) == [survivor]
    assert s["filed"][survivor] == _pending(retiring, "o/r") and s["filed"][retiring] == _pending(retiring, "o/r")
    capsys.readouterr()
    assert run(base, "backfill") == 0
    plans = {p["lesson"]: p for p in json.loads(capsys.readouterr().out)["planned_filings"]}
    assert plans[survivor]["reason"] == "pending reconcile" and plans[survivor]["repo"] == "o/r"
    gh = FakeGh()
    gh.issues.append({"number": 9, "body": f"{_pending(retiring, 'o/r')['marker']}\nfiled earlier"})
    monkeypatch.setattr(retro_rollup, "_run_gh", gh)
    f = _file_accept(vault.parent, [{"lesson_id": survivor, "file_issue": True, "repo": "o/r"}])
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert gh.calls == [] and len(gh.list_calls) == 1
    assert state_of(state)["filed"][survivor] == "o/r#9"


def test_pending_record_of_a_split_parent_moves_to_the_child(grouped, monkeypatch, capsys):
    vault, state, base = grouped
    _bridge_lessons(vault, state, base, monkeypatch)
    _notes_with_tags(vault, {1: "G1", 2: "G1", 3: "G1 G2", 4: "G2", 5: "G2"})
    assert run(base, "update") == 0
    (old_id,) = state_of(state)["lessons"]
    _save_filed(state, {old_id: _pending(old_id, "o/r")})
    (vault / "claude-insights" / "2026-10-03-retro-n3.md").unlink()
    assert run(base, "update") == 0
    s = state_of(state)
    child = next(l for l in s["lessons"] if l != old_id)
    assert s["lessons"][child]["split_from"] == old_id
    assert s["filed"][child] == _pending(old_id, "o/r") and s["filed"][old_id] == _pending(old_id, "o/r")
    gh = FakeGh()
    gh.issues.append({"number": 9, "body": f"{_pending(old_id, 'o/r')['marker']}\nfiled earlier"})
    monkeypatch.setattr(retro_rollup, "_run_gh", gh)
    f = _file_accept(vault.parent, [{"lesson_id": child, "file_issue": True, "repo": "o/r"}])
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    assert gh.calls == [] and state_of(state)["filed"][child] == "o/r#9"


def test_pending_record_with_a_malformed_marker_falls_back_to_the_lesson_marker(filing, tmp_path):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    bad = "<!-- retro-lesson:a b --> extra"
    gh.issues.append({"number": 4, "body": f"{bad}\nnot ours"})
    gh.issues.append({"number": 6, "body": f"<!-- retro-lesson:{lid} -->\nours"})
    _set_filed(state, lid, {"pending": True, "repo": "o/r", "marker": bad})
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 0
    assert gh.calls == [] and state_of(state)["filed"] == {lid: "o/r#6"}


# --- one reconcile pass for every pending record (#471 re-check round 3: X-023, X-024, C-013) ---

def _gh_with(monkeypatch, *issues, **kw):
    gh = FakeGh(**kw)
    gh.issues.extend(issues)
    monkeypatch.setattr(retro_rollup, "_run_gh", gh)
    return gh


def test_marker_must_match_exactly_a_trailing_newline_falls_back():
    rec = {"marker": "<!-- retro-lesson:L1 -->\n"}
    assert retro_rollup._reconcile_marker(rec, "L1") == "<!-- retro-lesson:L1 -->"
    assert retro_rollup._reconcile_marker({"marker": "<!-- retro-lesson:L2 -->\n"}, "L1") == "<!-- retro-lesson:L1 -->"
    assert retro_rollup._reconcile_marker({"marker": "<!-- retro-lesson:L2 -->"}, "L1") == "<!-- retro-lesson:L2 -->"


def test_x023_survivor_pending_and_a_merged_in_filing_files_nothing(grouped, monkeypatch, capsys):
    vault, state, base = grouped
    survivor, retiring = _merge_setup(vault, state, base, monkeypatch)
    _save_filed(state, {survivor: _pending(survivor, "o/r"), retiring: "o/r#5"})
    _notes_with_tags(vault, {3: "G1 G2"})
    assert run(base, "update") == 0
    gh = _gh_with(monkeypatch)  # the survivor's marker is in no issue
    f = _file_accept(vault.parent, [{"lesson_id": survivor, "file_issue": True, "repo": "o/r"}])
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 0
    out = json.loads(capsys.readouterr().out)
    assert gh.calls == [] and len(gh.list_calls) == 1  # one listing, no issue create
    s = state_of(state)
    assert s["filed"][survivor] == "o/r#5" and s["lessons"][survivor]["tracking"]["ref"] == "o/r#5"
    assert [r["outcome"] for r in out["reconciled"]] == ["none"] and out["filed"] == []
    traced = "".join(p.read_text() for p in (vault / "claude-insights").glob("*retro*.md"))
    assert _issue_trace("o/r", 5) in traced


def test_c013_pending_record_of_a_retired_id_is_adopted_even_when_the_survivor_is_tracked(grouped, monkeypatch, capsys):
    vault, state, base = grouped
    survivor, retiring = _merge_setup(vault, state, base, monkeypatch)
    _notes_with_tags(vault, {3: "G1 G2"})
    assert run(base, "update") == 0
    s = state_of(state)
    s["lessons"][survivor]["tracking"] = {"kind": "issue", "ref": "o/r#2", "since": "2026-10-01"}
    s["filed"] = {retiring: _pending(retiring, "o/r")}
    (state / "lessons-state.json").write_text(json.dumps(s))
    gh = _gh_with(monkeypatch, {"number": 9, "body": f"{_pending(retiring, 'o/r')['marker']}\nearlier"})
    f = _file_accept(vault.parent, [{"lesson_id": survivor, "file_issue": True, "repo": "o/r"}])
    capsys.readouterr()
    run(base, "backfill", "--apply", "--accept", str(f))
    out = json.loads(capsys.readouterr().out)
    assert gh.calls == [] and len(gh.list_calls) == 1
    assert state_of(state)["filed"][retiring] == "o/r#9"
    assert out["reconciled"] == [{"lesson": retiring, "repo": "o/r", "issue": 9, "outcome": "adopted"}]


def test_pending_record_without_a_matching_issue_is_deleted_outcome_none(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    other = _lesson_named(state, 2)
    _set_filed(state, "gone-id", _pending("gone-id", "o/r"))
    f = _file_accept(tmp_path, [{"lesson_id": other, "tracking_ref": "x"}])
    capsys.readouterr()
    run(base, "backfill", "--apply", "--accept", str(f))
    out = json.loads(capsys.readouterr().out)
    assert state_of(state)["filed"] == {} and gh.calls == []
    assert out["reconciled"] == [{"lesson": "gone-id", "repo": "o/r", "issue": None, "outcome": "none"}]


def test_failed_listing_keeps_records_exits_1_and_files_nothing_in_that_repo(filing, tmp_path, monkeypatch, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    broken = _gh_with(monkeypatch, list_fail="rc")
    s = state_of(state)
    s["filed"] = {lid: _pending(lid, "o/r"), "other": _pending("other", "o/r")}
    (state / "lessons-state.json").write_text(json.dumps(s))
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 1
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert broken.calls == [] and len(broken.list_calls) == 1
    assert sorted(r["outcome"] for r in out["reconciled"]) == ["error", "error"]
    assert out["skipped_entries"][0]["reason"] == "cannot check for an earlier filing"
    assert "gh exited with code 1" in captured.err
    assert set(state_of(state)["filed"]) == {lid, "other"}


def test_two_pending_records_in_one_repo_cost_one_listing(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    s = state_of(state)
    s["filed"] = {"a": _pending("a", "o/r"), "b": _pending("b", "o/r")}
    (state / "lessons-state.json").write_text(json.dumps(s))
    run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid)))
    assert len(gh.list_calls) == 1 and len(gh.calls) == 1


def test_one_marker_under_two_ids_adopts_the_same_issue_for_both(filing, tmp_path, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    marker = f"<!-- retro-lesson:{lid} -->"
    gh.issues.append({"number": 9, "body": f"{marker}\nearlier"})
    s = state_of(state)
    s["filed"] = {lid: _pending(lid, "o/r"), "copy": {**_pending(lid, "o/r")}}
    (state / "lessons-state.json").write_text(json.dumps(s))
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 0
    assert state_of(state)["filed"] == {lid: "o/r#9", "copy": "o/r#9"} and gh.calls == []


def test_dry_run_lists_pending_filings_and_makes_no_gh_call(filing, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    s = state_of(state)
    s["filed"] = {lid: _pending(lid, "o/r"), "gone-id": _pending("gone-id", "p/q")}
    (state / "lessons-state.json").write_text(json.dumps(s))
    capsys.readouterr()
    assert run(base, "backfill") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pending_filings"] == sorted(
        [{"lesson": lid, "repo": "o/r"}, {"lesson": "gone-id", "repo": "p/q"}], key=lambda r: r["lesson"])
    assert gh.calls == [] and gh.list_calls == []


def test_failed_listing_blocks_filing_when_only_a_merged_in_id_has_the_pending_record(grouped, monkeypatch, capsys):
    vault, state, base = grouped
    survivor, retiring = _merge_setup(vault, state, base, monkeypatch)
    _notes_with_tags(vault, {3: "G1 G2"})
    assert run(base, "update") == 0
    _save_filed(state, {retiring: _pending(retiring, "o/r")})
    s = state_of(state)
    s["filed"].pop(survivor, None)
    (state / "lessons-state.json").write_text(json.dumps(s))
    gh = _gh_with(monkeypatch, list_fail="rc")
    f = _file_accept(vault.parent, [{"lesson_id": survivor, "file_issue": True, "repo": "o/r"}])
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(f)) == 1
    assert gh.calls == []
    assert json.loads(capsys.readouterr().out)["skipped_entries"][0]["reason"] == "cannot check for an earlier filing"


def test_unsaved_reconcile_result_files_nothing_this_run(filing, tmp_path, monkeypatch, capsys):
    vault, state, base, gh = filing
    lid = _lesson_named(state, 3)
    _set_filed(state, lid, _pending(lid, "o/r"))

    real_save, calls = retro_rollup.save_state, []

    def first_save_fails(ctx, st):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        real_save(ctx, st)

    monkeypatch.setattr(retro_rollup, "save_state", first_save_fails)
    capsys.readouterr()
    assert run(base, "backfill", "--apply", "--accept", str(_accept_file_issue(tmp_path, lid))) == 1
    captured = capsys.readouterr()
    assert gh.calls == [] and "disk full" in captured.err
