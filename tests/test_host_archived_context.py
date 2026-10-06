"""Runtime and source containment agree about selected native transcript roots."""
import importlib
import pytest
from test_runtime_context import runtime_case, runtime_case_data, selected_host_context, resolve


@pytest.mark.parametrize("root", ["projects", "sessions", "archived_sessions"])
def test_context_accepts_only_selected_native_transcript_roots(runtime_case, selected_host_context, root):
    path = selected_host_context.native_home / root / "nested.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{}\n')
    supported = root == 'projects' if selected_host_context.host == 'claude' else root in {'sessions', 'archived_sessions'}
    if supported:
        assert resolve(runtime_case, transcript_path=str(path)).transcript_path == path.resolve()
    else:
        module = importlib.import_module("runtime_context")
        with pytest.raises(module.RuntimeContextError) as error:
            resolve(runtime_case, transcript_path=str(path))
        assert error.value.code == "transcript_outside_host"


@pytest.mark.parametrize("symlink", [False, True])
def test_context_rejects_outside_and_native_symlink_escape(runtime_case, selected_host_context, symlink):
    module = importlib.import_module("runtime_context")
    outside = runtime_case["worktree"] / "outside.jsonl"
    outside.write_text('{}\n')
    path = outside
    if symlink:
        root = 'projects' if selected_host_context.host == 'claude' else 'archived_sessions'
        path = selected_host_context.native_home / root / "escaped.jsonl"
        path.parent.mkdir(parents=True)
        path.symlink_to(outside)
    with pytest.raises(module.RuntimeContextError) as error:
        resolve(runtime_case, transcript_path=str(path))
    assert error.value.code == "transcript_outside_host"
