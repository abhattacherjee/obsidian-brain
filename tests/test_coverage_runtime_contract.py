"""Development dependencies must support the subprocess measurement contract."""
from pathlib import Path
import io
import pytest

from coverage import Coverage
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def test_coverage_config_uses_supported_subprocess_measurement():
    measurement = Coverage(config_file=str(ROOT / 'setup.cfg'))
    assert 'subprocess' in measurement.get_option('run:patch')
    assert measurement.get_option('report:fail_under') == 90
    assert measurement.get_option('run:source_pkgs') == ['skill_procedures']
    assert measurement.get_option('paths')['hooks'] == [
        'hooks', '*/installed plugin with spaces/hooks',
        '*/loaded installation with spaces/hooks']
    omitted = measurement.get_option('run:omit')
    assert len(omitted) == 6
    assert {pattern.rsplit('/', 1)[-1] for pattern in omitted} == {
        'obsidian_utils.py', 'deep_cli.py'}
    assert measurement.get_option('report:omit') == [
        'hooks/obsidian_utils.py', 'hooks/deep_cli.py']


@pytest.mark.parametrize('covered', [False, True])
def test_report_preserves_existing_policy_after_paths_are_combined(tmp_path, monkeypatch,
                                                                 covered):
    """Mapped data can contain paths that collection itself would omit."""
    monkeypatch.chdir(tmp_path)
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    for name in ('obsidian_utils.py', 'deep_cli.py', 'new_logic.py'):
        (hooks / name).write_text('first = 1\nsecond = 2\n')
    measurement = Coverage(config_file=str(ROOT / 'setup.cfg'),
                           data_file=str(tmp_path / '.coverage'))
    measurement.set_option('run:source', None)
    measurement.set_option('run:source_pkgs', None)
    data = measurement.get_data()
    # This is the canonical-path data produced by combine, including excluded
    # copies. The new module stays counted even though no line was executed.
    data.add_lines({str(hooks / 'obsidian_utils.py'): [1, 2],
                    str(hooks / 'deep_cli.py'): [1, 2],
                    str(hooks / 'new_logic.py'): [1, 2] if covered else []})
    report = io.StringIO()
    assert measurement.report(file=report) == (100 if covered else 0)
    assert 'new_logic.py' in report.getvalue()
    assert 'obsidian_utils.py' not in report.getvalue()
    assert 'deep_cli.py' not in report.getvalue()


def test_declared_dependencies_support_python39_subprocess_coverage():
    requirements = [Requirement(line) for line in (ROOT / 'requirements-dev.txt').read_text().splitlines()
                    if line.strip() and not line.startswith('#')]
    by_name = {requirement.name: requirement for requirement in requirements}
    assert set(by_name) == {'pytest', 'pytest-cov', 'coverage'}
    # These compatible releases are available on Python 3.9. Older coverage
    # lacks the subprocess patch, so silently accepting it would lose evidence.
    assert Version('7.10.6') in by_name['coverage'].specifier
    assert Version('7.9.2') not in by_name['coverage'].specifier
    assert by_name['coverage'].extras == {'toml'}
    assert Version('7.1.0') in by_name['pytest-cov'].specifier
    assert Version('6.3.0') not in by_name['pytest-cov'].specifier
    assert Version('8.4.2') in by_name['pytest'].specifier
