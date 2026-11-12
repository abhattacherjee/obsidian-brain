# tests/test_session_hint.py
"""Tests for find_latest_session() used by obsidian_session_hint hook."""

import io
import json

import pytest

import capture_cli
import obsidian_session_hint
from obsidian_utils import find_latest_session


def test_hint_finds_latest_session(sample_session_note, tmp_vault):
    """Returns most recent session for the project."""
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result is not None
    assert result["date"] == "2026-04-10"
    assert "frobulator" in result["summary"]
    assert "integration tests" in result["next_steps"]


def test_hint_no_sessions(tmp_vault):
    """Returns None when no sessions exist."""
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result is None


def test_hint_wrong_project(sample_session_note, tmp_vault):
    """Doesn't return sessions from other projects."""
    result = find_latest_session(str(tmp_vault), "claude-sessions", "totally-different-project")
    assert result is None


_RAW_SUMMARY = (
    "Session in **test-project** (73.2 min). "
    "AI summary unavailable — raw extraction below."
)


@pytest.fixture
def _note(tmp_vault):
    def _note(name, *, type_="claude-session", sid="s-old",
              summary="Built the frobulator.", status="summarized",
              project="test-project", filler=""):
        note_path = tmp_vault / "claude-sessions" / name
        type_line = f"type: {type_}\n" if type_ is not None else ""
        summary_section = f"## Summary\n{summary}\n\n" if summary is not None else ""
        note_path.write_text(
            f"---\n{type_line}date: 2026-11-12\nsession_id: {sid}\n"
            f"project: {project}\nstatus: {status}\n---\n\n"
            f"{summary_section}{filler}## Open Questions / Next Steps\n"
            "- [ ] Add integration tests.\n",
            encoding="utf-8",
        )
        return note_path

    return _note


def test_hint_skips_own_session(tmp_vault, _note):
    _note("2026-11-12-test-project.md", sid="s-current", summary="Current work.")
    _note("2026-11-11-test-project.md", summary="Previous work.")
    result = find_latest_session(
        str(tmp_vault), "claude-sessions", "test-project", exclude_session_id="s-current"
    )
    assert result["summary"] == "Previous work."


def test_hint_skips_newest_snapshot(tmp_vault, _note):
    _note("2026-11-12-test-project-aaaa-snapshot-092042.md",
          type_="claude-snapshot", summary=None)
    _note("2026-11-11-test-project.md")
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result["summary"] == "Built the frobulator."


@pytest.mark.parametrize("type_", ["claude-snapshot", "claude-insight"])
def test_hint_skips_non_session_type_with_summary(tmp_vault, _note, type_):
    # A summarized snapshot has a real ## Summary; only the type check skips it.
    _note("2026-11-12-test-project-aaaa-snapshot-092042.md",
          type_=type_, summary="Snapshot of the current work.")
    _note("2026-11-11-test-project.md")
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result["summary"] == "Built the frobulator."


def test_hint_skips_newest_raw_note(tmp_vault, _note):
    _note("2026-11-12-test-project.md", summary=_RAW_SUMMARY, status="auto-logged")
    _note("2026-11-11-test-project.md")
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result["summary"] == "Built the frobulator."


def test_hint_accepts_summary_mentioning_unavailable_phrase(tmp_vault, _note):
    _note("2026-11-12-test-project.md", status="summarized",
          summary="Fixed SessionStart hints that printed AI summary unavailable.")
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result["summary"] == "Fixed SessionStart hints that printed AI summary unavailable."


def test_hint_cap_counts_only_project_notes(tmp_vault, _note):
    for i in range(60):
        _note(f"2026-11-12-other-{i:04d}.md", project="other-project", sid=f"o{i}")
    _note("2026-11-01-test-project.md")
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result["summary"] == "Built the frobulator."


def test_hint_reads_next_steps_past_16kb_head(tmp_vault, _note):
    filler = "## Details\n" + ("x" * 20000) + "\n\n"
    _note("2026-11-12-test-project.md", filler=filler)
    assert len(filler) > 16384
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result["summary"] == "Built the frobulator."
    assert "Add integration tests." in result["next_steps"]


def test_hint_only_raw_and_snapshot_notes(tmp_vault, _note):
    _note("2026-11-12-test-project.md", summary=_RAW_SUMMARY, status="auto-logged")
    _note("2026-11-11-test-project.md", type_="claude-snapshot", summary=None)
    assert find_latest_session(str(tmp_vault), "claude-sessions", "test-project") is None


@pytest.mark.parametrize("summary", [" \t\n  ", "", None])
def test_hint_skips_empty_summary(tmp_vault, _note, summary):
    _note("2026-11-12-test-project.md", summary=summary)
    assert find_latest_session(str(tmp_vault), "claude-sessions", "test-project") is None


def test_hint_accepts_legacy_note(tmp_vault, _note):
    _note("2026-11-12-test-project.md", type_=None)
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    assert result["summary"] == "Built the frobulator."


@pytest.mark.parametrize("raw_count, found", [(51, False), (50, False), (49, True)])
def test_hint_project_note_cap(tmp_vault, _note, raw_count, found):
    _note("2026-10-01-test-project-0000.md")
    for i in range(1, raw_count + 1):
        _note(f"2026-10-01-test-project-{i:04d}.md",
              summary=_RAW_SUMMARY, status="auto-logged")
    result = find_latest_session(str(tmp_vault), "claude-sessions", "test-project")
    if found:
        assert result["summary"] == "Built the frobulator."
    else:
        assert result is None


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


@pytest.fixture
def sweeps(monkeypatch, tmp_vault):
    calls = []
    monkeypatch.setattr(capture_cli, "sweep", lambda limit=3, exclude=None: calls.append(exclude) or [])
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setattr(obsidian_session_hint, "load_config", lambda: {
        "vault_path": str(tmp_vault), "sessions_folder": "claude-sessions",
        "reaper_enabled": False,
    })
    monkeypatch.setattr(obsidian_session_hint, "claim_hook_run", lambda *a, **k: True)
    monkeypatch.setattr(obsidian_session_hint, "get_project_name", lambda cwd: "test-project")
    # _append_hook_log uses ~/.claude directly; isolate both startup writes.
    monkeypatch.setattr(obsidian_session_hint, "_append_hook_log", lambda *a, **k: None)
    monkeypatch.setattr(obsidian_session_hint, "_write_bootstrap_atomic", lambda *a: True)
    _stdin(monkeypatch, {"session_id": "s-current"})
    return calls


def test_hook_only_raw_note_prints_nothing(_note, sweeps, capsys):
    _note("2026-11-12-test-project.md", summary=_RAW_SUMMARY, status="auto-logged")
    obsidian_session_hint._run()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no previous sessions found for test-project" in captured.err


def test_hook_injects_summarized_note(_note, sweeps, capsys):
    _note("2026-11-12-test-project.md")
    obsidian_session_hint._run()
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Built the frobulator." in context
    assert "AI summary unavailable" not in context


def test_hook_compact_prints_nothing(_note, sweeps, monkeypatch, capsys):
    _note("2026-11-12-test-project.md")
    _stdin(monkeypatch, {"session_id": "s-current", "source": "compact"})
    obsidian_session_hint._run()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hint skipped: compact" in captured.err
    assert sweeps == ["s-current"]


def test_hook_sdk_prints_nothing(_note, sweeps, monkeypatch, capsys):
    _note("2026-11-12-test-project.md")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-py")
    obsidian_session_hint._run()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hint skipped: headless sdk entrypoint" in captured.err


def test_hook_own_session_prints_nothing(_note, sweeps, capsys):
    _note("2026-11-12-test-project.md", sid="s-current")
    obsidian_session_hint._run()
    assert capsys.readouterr().out == ""


def test_hook_empty_summary_defence(sweeps, monkeypatch, capsys):
    monkeypatch.setattr(obsidian_session_hint, "find_latest_session", lambda *a, **k: {
        "date": "2026-11-12", "summary": " \t\n", "next_steps": "Next work.",
    })
    obsidian_session_hint._run()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no previous sessions found for test-project" in captured.err
