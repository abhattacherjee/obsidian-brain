"""Current-scope reports use the selected native project on both hosts."""
import io
import json
from pathlib import Path

import pytest
import skill_procedures
from operation_state import store_artifact


@pytest.fixture
def selected_host_context(host, selected_host_context, tmp_path):
    from dataclasses import replace
    from runtime_context import using_runtime_context
    canonical, worktree = tmp_path / 'selected-project', tmp_path / 'selected-worktree'
    canonical.mkdir()
    worktree.mkdir()
    selected = replace(selected_host_context, canonical_project_root=canonical, worktree=worktree)
    with using_runtime_context(selected):
        yield selected


def test_current_report_uses_selected_canonical_project(selected_host_context, tmp_path, monkeypatch):
    unrelated = tmp_path / 'unrelated-process-project'
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    context = selected_host_context
    (context.vault_path / 'claude-check-items').mkdir()
    output, errors = io.StringIO(), io.StringIO()
    assert skill_procedures.run_operation(context, 'check-items', 'prepare', {}, output, errors) == 0, errors.getvalue()
    prepared = json.loads(output.getvalue())
    identifier = prepared['operation_id']
    directory = Path(prepared['operation_dir'])
    artifacts = {
        'scope.json': {'mode': 'current', 'project': None},
        'raw_items.json': [],
        'partition.json': {'flat_groups': []},
        'merged.json': {'mode': 'ok', 'merged_by_proj': {}},
        'classifications.json': {'classifier_mode': 'ok'},
        'buckets.json': {'review': [], 'dashboard_only': []},
        'gaps.json': {},
        'cascade_summary.json': {'cascaded': 0, 'skipped': 0},
    }
    for name, data in artifacts.items():
        store_artifact(context, identifier, name, json.dumps(data))
    def unexpected_subprocess(*args, **kwargs):
        pytest.fail('Known canonical project must not consult process cwd through Git')
    monkeypatch.setattr('subprocess.run', unexpected_subprocess)
    inputs = dict(zip(['SCOPE_PATH', 'RAW_PATH', 'PART_PATH', 'MERGED_PATH', 'CLASSIFICATIONS_PATH', 'BUCKETS_PATH', 'GAPS_PATH'], [str(directory / name) for name in list(artifacts)[:7]]))
    output, errors = io.StringIO(), io.StringIO()
    assert skill_procedures.run_operation(context, 'check-items', 'stage-07', {'operation_id': identifier, 'inputs': inputs}, output, errors) == 0, errors.getvalue()
    report = Path(output.getvalue().strip())
    assert report.is_file()
    body = report.read_text()
    assert 'scope: selected-project\n' in body
    assert '# Check-Items Report — selected-project — ' in body
    assert 'unrelated-process-project' not in body
    assert 'selected-worktree' not in body
