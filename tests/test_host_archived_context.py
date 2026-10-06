"""Runtime and source containment agree about native archived transcripts."""
import importlib

import pytest

from test_runtime_context import runtime_case, resolve


@pytest.mark.parametrize("root", ["sessions", "archived_sessions"])
def test_codex_context_accepts_native_current_and_archived_roots(runtime_case, root):
    path = runtime_case["codex_home"] / root / "rollout-native-thread.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"type":"session_meta","payload":{"id":"native-thread"}}\n')
    assert resolve(runtime_case, transcript_path=str(path)).transcript_path == path.resolve()


@pytest.mark.parametrize("symlink", [False, True])
def test_codex_context_rejects_outside_and_archived_symlink_escape(runtime_case, symlink):
    module = importlib.import_module("runtime_context")
    outside = runtime_case["worktree"] / "outside.jsonl"
    outside.write_text('{}\n')
    path = outside
    if symlink:
        path = runtime_case["codex_home"] / "archived_sessions" / "escaped.jsonl"
        path.parent.mkdir(parents=True)
        path.symlink_to(outside)
    with pytest.raises(module.RuntimeContextError) as error:
        resolve(runtime_case, transcript_path=str(path))
    assert error.value.code == "transcript_outside_host"


@pytest.mark.parametrize("root", ["projects", "sessions", "archived_sessions"])
def test_claude_context_accepts_only_native_projects(runtime_case, root):
    module = importlib.import_module("runtime_context")
    path = runtime_case["home"] / ".claude" / root / "nested.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{}\n')
    if root == "projects":
        assert resolve(runtime_case, host="claude", client="claude-code", transcript_path=str(path)).transcript_path == path.resolve()
    else:
        with pytest.raises(module.RuntimeContextError) as error:
            resolve(runtime_case, host="claude", client="claude-code", transcript_path=str(path))
        assert error.value.code == "transcript_outside_host"
