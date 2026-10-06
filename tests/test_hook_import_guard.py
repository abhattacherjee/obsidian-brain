"""Lifecycle hooks survive an obsidian_utils import failure (#371).

v3.6.0 broke `import obsidian_utils` on Python 3.9. Every lifecycle hook then
exited 1 with a traceback and wrote nothing to the hook log, because the log
writer lives in the module that failed. The hooks now catch the failure, log
one `outcome=IMPORT_FAILED` line through hooks/hook_bootstrap.py, and exit 0.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import hook_bootstrap  # conftest puts hooks/ on sys.path

ROOT = Path(__file__).resolve().parents[1]

HOOKS = [
    ("obsidian_session_log.py", "SessionEnd"),
    ("obsidian_session_hint.py", "SessionStart"),
    ("obsidian_context_snapshot.py", "PreCompact"),
    ("obsidian_retro_gate.py", "Stop"),
]


def _env(home: Path) -> dict:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("GIT_", "CODEX_")) and k != "CLAUDE_CODE_SESSION_ID"
    }
    env.update(HOME=str(home), CODEX_HOME=str(home / ".codex"))
    return env


def _run(hooks_dir: Path, script: str, home: Path, stdin: str, context):
    command = [sys.executable, str(ROOT / 'tests' / 'native_hook_test_driver.py'),
        '--host', context.host, '--client', context.client,
        '--session-id', context.native_session_id, '--cwd', str(context.worktree),
        '--vault', str(context.vault_path), '--config', str(context.config_path),
        '--resource-root', str(hooks_dir.parent), '--index', str(context.index_path),
        '--state', str(context.state_path), script]
    result = subprocess.run(command, input=stdin, text=True, capture_output=True,
        env=dict(os.environ), cwd=context.worktree, timeout=5, check=False)
    proof = json.loads(next(line.removeprefix('NATIVE_CONTEXT_PROOF:')
        for line in result.stderr.splitlines() if line.startswith('NATIVE_CONTEXT_PROOF:')))
    assert proof['host'] == context.host
    assert proof['native_session_id'] == context.native_session_id
    assert proof['vault_path'] == str(context.vault_path)
    assert proof['resource_root'] == str(hooks_dir.parent)
    return result


@pytest.fixture
def broken_hooks(tmp_path):
    """A copy of hooks/ whose obsidian_utils raises at import, like #371."""
    hooks_dir = tmp_path / "broken plugin" / "hooks"
    shutil.copytree(ROOT / "hooks", hooks_dir,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (hooks_dir / "obsidian_utils.py").write_text('raise TypeError("x")\n')
    for descriptor in ('.claude-plugin', '.codex-plugin'):
        (hooks_dir.parent / descriptor).mkdir()
        (hooks_dir.parent / descriptor / 'plugin.json').write_text('{"name":"synthetic-brain"}')
    home = tmp_path / "legacy-home"
    home.mkdir()
    return hooks_dir, home


def _log_lines(home: Path, native=False) -> list[str]:
    log = (home if native else home / ".claude") / "obsidian-brain-hook.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.parametrize("script, event", HOOKS)
@pytest.mark.parametrize("stdin", ["", "realistic"], ids=["empty-stdin", "realistic-stdin"])
def test_import_failure_exits_0_and_logs_one_line(broken_hooks, script, event, stdin, selected_host_context):
    hooks_dir, home = broken_hooks
    if stdin == "realistic":
        stdin = json.dumps({"session_id": "abcdef12-3456", "cwd": "/work/my proj",
                            "hook_event_name": event})
        sid, project = selected_host_context.native_session_id[:8], selected_host_context.worktree.name.replace(" ", "_")
    else:
        sid, project = selected_host_context.native_session_id[:8], selected_host_context.worktree.name.replace(" ", "_")
    proc = _run(hooks_dir, script, home, stdin, selected_host_context)

    assert proc.returncode == 0, proc.stderr
    lines = _log_lines(selected_host_context.native_home, native=True)
    assert len(lines) == 1, lines
    fields = lines[0].split(" ")
    # Same field order as the SessionEnd writer: `awk '{print $5}'` = outcome.
    assert fields[1:5] == [event, f"project={project}", f"sid={sid}",
                           "outcome=IMPORT_FAILED"], lines[0]
    py = sys.version.split()[0]
    assert lines[0].endswith(f"detail=python={py} TypeError: x"), lines[0]
    log = selected_host_context.native_home / "obsidian-brain-hook.log"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

    if event == "SessionStart":
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]
        assert ctx["hookEventName"] == "SessionStart"
        assert "obsidian-brain failed to load" in ctx["additionalContext"]
        assert f"python={py} TypeError: x" in ctx["additionalContext"]
        assert "obsidian-brain-hook.log" in ctx["additionalContext"]
    else:
        assert proc.stdout == ""


@pytest.mark.parametrize("script, event", HOOKS)
def test_normal_import_path_is_unchanged(tmp_path, script, event, selected_host_context):
    """Negative control: the real obsidian_utils loads, no IMPORT_FAILED."""
    transcript = tmp_path / ".claude" / "projects" / "example" / "sid-normal.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")
    payload = {"session_id": "sid-normal", "cwd": str(tmp_path),
               "transcript_path": str(transcript), "hook_event_name": event}
    proc = _run(ROOT / "hooks", script, tmp_path, json.dumps(payload), selected_host_context)
    assert proc.returncode == 0, proc.stderr
    assert not any("IMPORT_FAILED" in line for line in _log_lines(selected_host_context.native_home, native=True))
    assert "failed to load" not in proc.stdout


# --- hook_bootstrap in process (the subprocess runs above do not count
# toward coverage) --------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", _Stdin('{"session_id": "s1", "cwd": "/a/b"}'))
    return tmp_path


class _Stdin:
    def __init__(self, text):
        self.text = text

    def read(self, n=-1):
        return self.text if n < 0 else self.text[:n]


def test_creates_claude_dir_owner_only(home):
    hook_bootstrap.log_import_failure("Stop", TypeError("boom"))
    assert stat.S_IMODE((home / ".claude").stat().st_mode) == 0o700
    [line] = _log_lines(home)
    assert " Stop project=b sid=s1 outcome=IMPORT_FAILED detail=" in line


def test_existing_log_is_appended_and_tightened(home):
    (home / ".claude").mkdir()
    log = home / ".claude" / "obsidian-brain-hook.log"
    log.write_text("earlier line\n")
    log.chmod(0o644)
    hook_bootstrap.log_import_failure("Stop", TypeError("boom"))
    assert _log_lines(home)[0] == "earlier line"
    assert len(_log_lines(home)) == 2
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_rotates_over_100kb(home):
    (home / ".claude").mkdir()
    log = home / ".claude" / "obsidian-brain-hook.log"
    log.write_text("x" * (100 * 1024 + 1))
    hook_bootstrap.log_import_failure("Stop", TypeError("boom"))
    assert (home / ".claude" / "obsidian-brain-hook.log.1").stat().st_size == 100 * 1024 + 1
    assert len(_log_lines(home)) == 1


def test_exactly_100kb_is_not_rotated(home):
    """Same `>` boundary as obsidian_utils._append_sessionend_log."""
    (home / ".claude").mkdir()
    log = home / ".claude" / "obsidian-brain-hook.log"
    log.write_text("x" * (100 * 1024))
    hook_bootstrap.log_import_failure("Stop", TypeError("boom"))
    assert not (home / ".claude" / "obsidian-brain-hook.log.1").exists()


def test_never_raises_when_log_dir_is_unwritable(home, capsys):
    (home / ".claude").write_text("a file, not a dir")
    detail = hook_bootstrap.log_import_failure("Stop", TypeError("boom"))
    assert detail.endswith("TypeError: boom")
    assert "log write failed" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["not json", "[1, 2]", "null"])
def test_bad_stdin_falls_back_to_unknown_sid(home, monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", _Stdin(text))
    hook_bootstrap.log_import_failure("Stop", TypeError("boom"))
    [line] = _log_lines(home)
    assert " sid=unknown " in line


def test_detail_is_one_line_and_capped():
    detail = hook_bootstrap.failure_detail(ValueError("a\nb\tc" + "z" * 1000))
    assert "\n" not in detail and "\t" not in detail
    assert len(detail) == 300


def test_log_constants_match_obsidian_utils():
    import obsidian_utils
    assert hook_bootstrap._HOOK_LOG_NAME == obsidian_utils._HOOK_LOG_NAME
    assert hook_bootstrap._HOOK_LOG_MAX_BYTES == obsidian_utils._HOOK_LOG_MAX_BYTES


@pytest.fixture
def selected_host_context(selected_host_context, host, request):
    from runtime_context import resolve_runtime_context, using_runtime_context
    original = selected_host_context
    root = request.getfixturevalue('broken_hooks')[0].parent if 'broken_hooks' in request.fixturenames else ROOT
    selected = resolve_runtime_context(host, original.client,
        {'session_id': original.native_session_id, 'cwd': str(original.worktree)},
        {'config_path': original.config_path, 'resource_root': root,
         'index_path': original.index_path, 'state_path': original.state_path})
    with using_runtime_context(selected):
        yield selected
