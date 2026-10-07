"""A full gate must include every shard and combine coverage before passing."""
import importlib.util
import json
from pathlib import Path

import coverage
import pytest


spec = importlib.util.spec_from_file_location(
    'ci_test_shards', Path(__file__).resolve().parents[1] / 'scripts/ci-checks/test_shards.py')
shards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shards)


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / 'checkout'
    tests = root / 'tests'
    tests.mkdir(parents=True)
    for name, size in [('test_native.py', 150), ('legacy_test.py', 80),
                       ('test_writer.py', 60), ('test_reader.py', 20)]:
        (tests / name).write_text('#' * size)
    (tests / 'helper.py').write_text('# Not a collected file')
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    for index in range(2):
        folder = artifacts / f'shard-{index}'
        folder.mkdir()
        shards.write_manifest(root, 2, index, folder / 'manifest.json')
    return root, artifacts


def test_complete_partition_contains_each_pytest_file_once(corpus):
    root, artifacts = corpus
    groups = shards.partitions(root, 2)
    assert sorted(name for group in groups for name in group) == [
        'tests/legacy_test.py', 'tests/test_native.py',
        'tests/test_reader.py', 'tests/test_writer.py']
    assert shards.validate_artifacts(root, artifacts, 2) == []


def test_zero_byte_files_cannot_create_an_empty_shard(corpus):
    root, _ = corpus
    for path in (root / 'tests').glob('*.py'):
        path.write_text('')
    with pytest.raises(ValueError, match='Every shard must contain tests'):
        shards.partitions(root, 2)


def test_measured_security_file_has_its_own_shard(corpus):
    root, _ = corpus
    (root / 'tests/test_security.py').write_text('# slow security tests')
    groups = shards.partitions(root, 2)
    assert groups[0] == ['tests/test_security.py']
    assert groups[1] == ['tests/legacy_test.py', 'tests/test_native.py',
                         'tests/test_reader.py', 'tests/test_writer.py']


@pytest.mark.parametrize('change', ['missing-shard', 'missing-file', 'duplicate-file',
                                   'wrong-index', 'wrong-count', 'new-test', 'missing-manifest'])
def test_incomplete_or_stale_partition_fails(corpus, change):
    root, artifacts = corpus
    manifest = artifacts / 'shard-0/manifest.json'
    payload = json.loads(manifest.read_text())
    if change == 'missing-shard':
        (artifacts / 'shard-0').rename(artifacts / 'unexpected-shard')
    elif change == 'missing-manifest':
        manifest.unlink()
    elif change == 'new-test':
        (root / 'tests/test_added.py').write_text('# New required tests')
    else:
        if change == 'missing-file':
            payload['files'].pop()
        elif change == 'duplicate-file':
            payload['files'].append(payload['files'][0])
        elif change == 'wrong-index':
            payload['index'] = 1
        else:
            payload['count'] = 3
        manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        shards.validate_artifacts(root, artifacts, 2)


def test_missing_coverage_data_fails(corpus):
    root, artifacts = corpus
    with pytest.raises(ValueError, match='Missing shard coverage data'):
        shards.validate_artifacts(root, artifacts, 2, coverage=True)


@pytest.mark.parametrize('complete', [False, True])
def test_combined_coverage_enforces_ninety_percent(corpus, monkeypatch, complete):
    root, artifacts = corpus
    hooks = root / 'hooks'
    hooks.mkdir()
    source = hooks / 'sample.py'
    source.write_text('\n'.join(f'value_{i} = {i}' for i in range(1, 9)) + '\n')
    (root / 'setup.cfg').write_text(
        '[coverage:run]\nsource = hooks\n[coverage:report]\nfail_under = 90\n')
    monkeypatch.chdir(root)
    for index in range(2):
        data = coverage.CoverageData(basename=str(artifacts / f'shard-{index}/.coverage'))
        lines = {1, 2, 3, 4} if not complete or index == 0 else {5, 6, 7, 8}
        data.add_lines({str(source): lines})
        data.write()
    if complete:
        assert shards.combine_coverage(root, artifacts, 2) == 100
    else:
        with pytest.raises(ValueError, match='below 90%'):
            shards.combine_coverage(root, artifacts, 2)
