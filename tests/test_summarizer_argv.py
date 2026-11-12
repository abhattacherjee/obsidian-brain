"""Summarizers run with stripped argv and cwd (#510)."""

from types import SimpleNamespace

import pytest

import obsidian_utils


SUMMARY = (
    "## Summary\nDid a thing.\n\n"
    "## Key Decisions\n- None noted.\n\n"
    "## Changes Made\n- None noted.\n\n"
    "## Errors Encountered\n- None.\n\n"
    "## Open Questions / Next Steps\n- [ ] None.\n\n"
    "## Importance\n5\n"
)


@pytest.mark.parametrize(
    "function,call_args,stdout",
    [
        pytest.param(
            obsidian_utils.generate_snapshot_summary,
            (["hello"], ["hi"], {}),
            "## Summary\nWork in progress.\n",
            id="generate_snapshot_summary",
        ),
        pytest.param(
            obsidian_utils.generate_theme_names,
            ([{"top_terms": ["git"], "sample_titles": ["Git Flow"]}],),
            '[{"name": "Git Flow", "summary": "Branch work."}]',
            id="generate_theme_names",
        ),
        pytest.param(
            obsidian_utils.generate_summary,
            (["hello"], ["hi"], {}),
            SUMMARY,
            id="generate_summary",
        ),
        pytest.param(
            obsidian_utils.generate_summaries_batch,
            ([{
                "ok": True,
                "user_msgs": ["hello"],
                "assistant_msgs": ["hi"],
                "metadata": {},
            }],),
            f"===== SUMMARY 1 =====\n{SUMMARY}",
            id="generate_summaries_batch",
        ),
    ],
)
def test_summarizer_subprocess_argv_and_cwd(
    function, call_args, stdout, monkeypatch, tmp_path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args[0], kwargs))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(obsidian_utils.subprocess, "run", fake_run)
    monkeypatch.setattr(obsidian_utils, "worker_cwd", lambda: str(tmp_path))

    function(*call_args, model="haiku")

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == ["claude", "-p", "--model"]
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert kwargs["cwd"] == str(tmp_path)


def test_summarizer_cwd_none_on_oserror(monkeypatch):
    def fail_cwd():
        raise OSError("worker cwd unavailable")

    monkeypatch.setattr(obsidian_utils, "worker_cwd", fail_cwd)
    assert obsidian_utils._summarizer_cwd() is None


def test_summarizer_argv_exact():
    assert obsidian_utils.summarizer_argv("haiku") == [
        "claude", "-p", "--model", "haiku", "--setting-sources", "project",
        "--strict-mcp-config", "--tools", "", "--no-session-persistence",
    ]
