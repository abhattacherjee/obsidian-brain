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


def _workflow_shard_block(job):
    """Read the executable literal from the production workflow."""
    import re
    import textwrap

    source = (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text()
    section = re.split(r'\n  \S', source.split('\n  ' + job + ':\n', 1)[1], maxsplit=1)[0]
    return textwrap.dedent(section.split('        run: |\n', 1)[1].split(
        '      - ', 1)[0])


def _check_workflow_timing_owner(tmp_path, job, block):
    """Execute every matrix index and check actual test commands and artifacts."""
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    count, owner = (6, 4) if job == 'coverage-shards' else (4, 0)
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    driver = binaries / 'driver'
    driver.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
if pathlib.Path(sys.argv[0]).name == 'python':
    os.execv(sys.executable, [sys.executable, os.environ['SHARD_HELPER'],
                            '--root', os.environ['SHARD_ROOT'], *args[1:]])
with open(os.environ['ARGV_LOG'], 'a') as handle:
    handle.write(json.dumps(args) + '\\n')
data = pathlib.Path('.coverage')
with data.open('ab' if '--cov-append' in args else 'wb') as handle:
    handle.write(b'serial\\n' if 'no:xdist' in args else b'parallel\\n')
''')
    driver.chmod(0o700)
    for name in ('python', 'pytest'):
        (binaries / name).symlink_to(driver)
    timing = ['tests/test_security.py::TestDecisionTimeIsBounded',
              'tests/test_security.py::TestPatternDecisionTimeIsBounded']
    serial_indices = []
    for index in range(count):
        work = tmp_path / str(index)
        work.mkdir()
        log = work / 'argv.jsonl'
        env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                   RUNNER_TEMP=str(work), ARGV_LOG=str(log), SHARD_ROOT=str(root),
                   SHARD_HELPER=str(root / 'scripts/ci-checks/test_shards.py'))
        # macOS ships Bash3; Ubuntu uses its built-in mapfile for this same block.
        compatibility = '''
if ! type mapfile >/dev/null 2>&1; then
  mapfile() {
    [[ "$1" == -t && "$2" == TEST_FILES ]] || return 1
    TEST_FILES=()
    while IFS= read -r shard_line; do TEST_FILES+=("$shard_line"); done
  }
fi
'''
        result = subprocess.run(['bash', '-c', compatibility + block.replace(
            '${{ matrix.shard }}', str(index))], cwd=work, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        ordinary = calls[0]
        assert ordinary[ordinary.index('-n') + 1] == '4'
        assert all('--deselect=' + name + '::' in ordinary for name in timing)
        files = [arg for arg in ordinary if arg.startswith('tests/')]
        assert files == shards.partitions(root, count)[index]
        serial = [args for args in calls if 'no:xdist' in args]
        assert len(calls) == 1 + len(serial)
        assert len(serial) == (1 if index == owner else 0), 'Wrong timing owner'
        if serial:
            serial_indices.append(index)
            args = serial[0]
            assert [arg for arg in args if arg.startswith('tests/')] == timing
            assert '-n' not in args
            assert '--junitxml=' + str(work / 'shard-data/serial-timing.xml') in args
            if job == 'coverage-shards':
                assert '--cov-append' in args and '--cov=hooks' in args
        for args in calls:
            assert 'parity_collection_plugin' in args
            assert 'docs/parity/capabilities.json' in args
        if job == 'coverage-shards':
            expected = b'parallel\nserial\n' if index == owner else b'parallel\n'
            assert (work / 'shard-data/.coverage').read_bytes() == expected
    assert serial_indices == [owner]


@pytest.mark.parametrize('job', ['coverage-shards', 'py39-shards'])
def test_workflow_runs_timing_classes_once_on_the_measured_owner(tmp_path, job):
    _check_workflow_timing_owner(tmp_path, job, _workflow_shard_block(job))


@pytest.mark.parametrize('mutation', ['wrong-owner', 'missing-serial'])
def test_workflow_timing_controls_reject_lost_or_moved_tests(tmp_path, mutation):
    block = _workflow_shard_block('coverage-shards')
    if mutation == 'wrong-owner':
        block = block.replace('== 4', '== 0', 1)
    else:
        block = '\n'.join('  :' if 'pytest ' in line and 'no:xdist' in line else line
                          for line in block.splitlines())
    with pytest.raises(AssertionError, match='Wrong timing owner'):
        _check_workflow_timing_owner(tmp_path, 'coverage-shards', block)


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


@pytest.mark.parametrize('outside', [
    'pytest-coverage/popen-gw0/test_reflection0/skill_procedures.py',
    'pytest-coverage/popen-gw0/test_reflection0/other.py',
    'pytest-coverage/popen-gw0/test_install0/hooks/skill_procedures.py',
    'other/skill_procedures.py',
])
def test_only_direct_synthetic_collection_helpers_are_excluded(corpus, monkeypatch,
                                                              capsys, outside):
    root, artifacts = corpus
    hooks = root / 'hooks'
    hooks.mkdir()
    source = hooks / 'skill_procedures.py'
    source.write_text('value = 1\n')
    (root / 'setup.cfg').write_text(
        '[coverage:run]\nsource = hooks\n[coverage:report]\nfail_under = 90\n')
    monkeypatch.chdir(root)
    missing = root.parent / outside
    for index in range(2):
        data = coverage.CoverageData(basename=str(artifacts / f'shard-{index}/.coverage'))
        data.add_lines({str(source): [1], str(missing): [1]})
        data.write()
    if outside == 'pytest-coverage/popen-gw0/test_reflection0/skill_procedures.py':
        assert shards.combine_coverage(root, artifacts, 2) == 100
        assert 'Excluded 1 synthetic collection fixture module(s)' in capsys.readouterr().out
    else:
        with pytest.raises(ValueError, match='Unexpected coverage source outside checkout'):
            shards.combine_coverage(root, artifacts, 2)


def test_missing_checkout_source_still_fails(corpus, monkeypatch):
    root, artifacts = corpus
    (root / 'hooks').mkdir()
    (root / 'setup.cfg').write_text(
        '[coverage:run]\nsource = hooks\n[coverage:report]\nfail_under = 90\n')
    monkeypatch.chdir(root)
    for index in range(2):
        data = coverage.CoverageData(basename=str(artifacts / f'shard-{index}/.coverage'))
        data.add_lines({str(root / 'hooks/missing.py'): [1]})
        data.write()
    with pytest.raises(coverage.exceptions.NoSource):
        shards.combine_coverage(root, artifacts, 2)


def test_installed_production_copy_is_mapped_and_counted(corpus, monkeypatch):
    root, artifacts = corpus
    hooks = root / 'hooks'
    hooks.mkdir()
    source = hooks / 'skill_procedures.py'
    source.write_text('first = 1\nsecond = 2\n')
    installed = root.parent / 'pytest-coverage/popen-gw0/test_install0/installed plugin with spaces/hooks'
    (root / 'setup.cfg').write_text(
        '[coverage:run]\nsource = hooks\n[coverage:paths]\nhooks =\n    hooks\n'
        '    */installed plugin with spaces/hooks\n[coverage:report]\nfail_under = 90\n')
    monkeypatch.chdir(root)
    for index in range(2):
        data = coverage.CoverageData(basename=str(artifacts / f'shard-{index}/.coverage'))
        data.add_lines({str(installed / 'skill_procedures.py'): [index + 1]})
        data.write()
    assert shards.combine_coverage(root, artifacts, 2) == 100


@pytest.mark.parametrize('failure', ['list', 'worker', 'serial', 'copy', 'coverage', None])
def test_local_parallel_gate_never_records_token_after_failed_phase(tmp_path, failure):
    """Execute the real shell control flow with isolated phase exit controls."""
    import os
    import shlex
    import subprocess
    import sys

    source = (Path(__file__).resolve().parents[1] / 'scripts/commit-preflight.sh').read_text()
    block = source.split('# __PARALLEL_COVERAGE_START__', 1)[1].split(
        '# __PARALLEL_COVERAGE_END__', 1)[0]
    tail = source.split('# __HARDEN_TEST_END__', 1)[1]
    driver = tmp_path / 'phase-driver'
    driver.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
phase = ('list' if 'list' in args else 'coverage' if 'coverage' in args
         else 'worker' if '-n' in args else 'serial')
with open(os.environ['PHASE_LOG'], 'a') as handle:
    handle.write(json.dumps({'phase': phase, 'args': args}) + '\\n')
if phase == os.environ.get('FAIL_PHASE'):
    sys.exit(1)
if phase == 'serial' and os.environ.get('FAIL_PHASE') == 'copy':
    pathlib.Path(os.environ['COVERAGE_FILE']).unlink()
    sys.exit(0)
if phase == 'list':
    pathlib.Path(args[args.index('--manifest') + 1]).write_text('{}')
elif phase in ('worker', 'serial'):
    pathlib.Path(os.environ['COVERAGE_FILE']).write_bytes(b'controlled coverage')
''')
    driver.chmod(0o700)
    token = tmp_path / 'token.json'
    log = tmp_path / 'phases.jsonl'
    prefix = '\n'.join([
        'CHECKS_PASSED=true', 'CHECKS_RUN=""', 'STAGED_FILES=probe',
        'TOKEN_HEAD=controlled-head', 'TOKEN_EXPIRY_SECONDS=300',
        'PROJECT_DIR=' + shlex.quote(str(tmp_path)),
        'TOKEN_FILE=' + shlex.quote(str(token)),
        'PREFLIGHT_TEST_PYTHON=' + shlex.quote(str(driver)),
    ]) + '\n'
    environment = dict(os.environ, TMPDIR=str(tmp_path), PHASE_LOG=str(log),
                       FAIL_PHASE=failure or '')
    result = subprocess.run(['bash', '-c', prefix + block + tail],
                            cwd=tmp_path, env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=20)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    expected = ['list', 'worker', 'serial', 'coverage']
    if failure:
        expected = (['list', 'worker', 'serial'] if failure == 'copy'
                    else expected[:expected.index(failure) + 1])
        assert result.returncode != 0, result.stdout + result.stderr
        assert not token.exists()
        assert 'PREFLIGHT FAILED' in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(token.read_text())['checks_run'] == 'tests'
    assert [call['phase'] for call in calls] == expected
    if failure == 'list':
        return  # No pytest phase may start after failed manifest preparation.
    worker = calls[1]['args']
    assert worker[worker.index('-n') + 1] == '4'
    assert '--dist=load' in worker and '--max-worker-restart=0' in worker
    assert 'tests/' in worker
    timing = ['tests/test_security.py::TestDecisionTimeIsBounded',
              'tests/test_security.py::TestPatternDecisionTimeIsBounded']
    assert all('--deselect=' + name + '::' in worker for name in timing)
    if len(calls) >= 3:
        serial = calls[2]['args']
        assert all(name in serial for name in timing)
        assert 'no:xdist' in serial and '--cov-append' in serial
        assert '-n' not in serial
    for call in calls[1:3]:
        if call['phase'] not in ('worker', 'serial'):
            continue
        assert '-m' in call['args'] and 'pytest' in call['args']
        assert 'parity_collection_plugin' in call['args']
        assert 'docs/parity/capabilities.json' in call['args']


@pytest.mark.parametrize('interrupted', [False, True])
def test_new_preflight_invalidates_previous_token_before_checks(tmp_path, interrupted):
    """A killed or failed new run must not leave an older approval usable."""
    import shlex
    import subprocess

    source = (Path(__file__).resolve().parents[1] / 'scripts/commit-preflight.sh').read_text()
    invalidation = source.split('# __PREFLIGHT_INVALIDATE_START__', 1)[1].split(
        '# __PREFLIGHT_INVALIDATE_END__', 1)[0]
    assert source.index('# __PREFLIGHT_INVALIDATE_START__') < source.index('TOKEN_HEAD=')
    token = tmp_path / 'previous-token.json'
    token.write_text(json.dumps({'head': 'unchanged', 'expires': 9999999999}))
    status = 137 if interrupted else 1
    command = 'TOKEN_FILE=' + shlex.quote(str(token)) + '\n' + invalidation
    command += '\nexit ' + str(status)  # Interrupt only this private synthetic shell.
    result = subprocess.run(['bash', '-c', command], cwd=tmp_path,
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == status
    assert not token.exists(), 'A previous approval survived the new run starting'
