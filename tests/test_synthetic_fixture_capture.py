"""Scheduled fixture evidence cannot turn into native dispatch evidence."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def collector():
    spec = importlib.util.spec_from_file_location('synthetic_fixture_capture',
        ROOT / 'scripts/dev-test/capture-synthetic-host-fixtures.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('exit_code', [0, 1])
def test_synthetic_capture_records_actual_harness_result(selected_host_context, monkeypatch, exit_code):
    module = collector()
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ['git', 'rev-parse']:
            return SimpleNamespace(returncode=0, stdout='a'*40+'\n', stderr='')
        if command[:2] == ['git', 'status']:
            return SimpleNamespace(returncode=0, stdout=' M hooks/capture.py\n', stderr='')
        return SimpleNamespace(returncode=exit_code, stdout='fixture result', stderr='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    record = module.capture(ROOT, 'a'*40)
    assert record['status'] == ('passed' if exit_code == 0 else 'failed')
    assert record['exit_code'] == exit_code and record['tracked_checkout_dirty'] is True
    assert record['scope'] == 'synthetic-host-conformance'
    assert record['native_dispatch_verified'] is False
    assert set(record['native_client_versions'].values()) == {None}
    assert 'hooks/capture.py' in record['source_sha256']
    assert 'hooks/codex-hooks.json' in record['source_sha256']
    assert len(record['source_sha256']['hooks/capture.py']) == 64
    command, kwargs = calls[-1]
    assert '-p' in command and 'parity_collection_plugin' in command
    assert '--parity-matrix' in command
    assert set(module.TESTS) <= set(command)
    assert kwargs['timeout'] == 180 and kwargs['stdin'] == module.subprocess.DEVNULL


def test_synthetic_capture_rejects_an_invented_commit(selected_host_context, monkeypatch):
    module = collector()
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout='b'*40+'\n', stderr=''))
    with pytest.raises(ValueError, match='differs from the current checkout'):
        module.capture(ROOT, 'a'*40)
