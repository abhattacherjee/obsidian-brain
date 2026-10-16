"""Native auxiliary directory failures retain the public best-effort results."""
from pathlib import Path
import pytest
import obsidian_utils
from parity_test_helpers import host, selected_host_context


@pytest.mark.parametrize('operation, expected', [('get', None), ('clear', False), ('reap', 0)])
def test_native_retro_directory_failure_is_best_effort(selected_host_context, monkeypatch, operation, expected):
    directory = obsidian_utils._retro_gate_dir()
    directory.rmdir()
    original = Path.mkdir
    def fail_selected_directory(path, *args, **kwargs):
        if path == directory:
            raise OSError('synthetic selected directory failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', fail_selected_directory)
    functions = {'get': obsidian_utils.get_retro_classification_pending,
                 'clear': obsidian_utils.clear_retro_classification_pending,
                 'reap': obsidian_utils._reap_stale_retro_sentinels}
    args = () if operation == 'reap' else (selected_host_context.native_session_id,)
    result = functions[operation](*args)
    assert type(result) is type(expected)
    assert result == expected
