"""Disposable invoking context and strict AI mocks for check-items tests."""
from pathlib import Path
import subprocess
import os
import uuid
import pytest
from ai_backend import AIResult
from runtime_context import using_runtime_context


def ai_response(data=None, rc=0):
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    model = context.config['classifier_model' if context.host == 'claude' else 'codex_ai_model']
    return rc, AIResult('ok' if rc == 0 else 'invalid_output', data,
                        'fixture-revision', '', context.host, model)


def verdicts(groups, classification='DONE', confidence='HIGH'):
    return [{'group_id':g['group_id'],'classification':classification,'confidence':confidence,
             'canonical_text':g.get('representative',g.get('canonical_text','work')),
             'evidence_citation':'commit abc1234' if classification=='DONE' else None,
             'action_required':None} for g in groups]


@pytest.fixture
def selected_host_context(host, selected_host_context):
    from dataclasses import replace
    selected = replace(selected_host_context, config=dict(selected_host_context.config,
        summary_model='haiku', classifier_model='claude-haiku-4-5-20251001', codex_ai_model='gpt-native-configured'))
    with using_runtime_context(selected):
        yield selected


@pytest.fixture(autouse=True)
def native_ai_context(selected_host_context, tmp_path, monkeypatch):
    import ai_backend
    def blocked(*args, **kwargs):
        raise AssertionError('test attempted a live native AI subprocess')
    monkeypatch.setattr(subprocess, 'Popen', blocked)
    monkeypatch.setattr(ai_backend, 'execute_ai', blocked)
    import check_items_cli
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    monkeypatch.setattr(check_items_cli, '_safe_workdir', lambda: private)
    yield selected_host_context


def private_output(name="out.json"):
    from runtime_context import current_runtime_context
    from note_transactions import session_state_path
    context = current_runtime_context()
    jobs = session_state_path(context) / "jobs"
    jobs.mkdir(mode=0o700, exist_ok=True)
    directory = jobs / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    output = directory / name
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    return output
