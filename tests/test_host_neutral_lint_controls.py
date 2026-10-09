"""Mutation controls for the strict shared-host boundary."""
import importlib.util
from pathlib import Path
import pytest

SOURCE = Path(__file__).parents[1] / 'scripts' / 'ci-checks' / 'host_neutral_lint.py'
spec = importlib.util.spec_from_file_location('host_neutral_lint', SOURCE)
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)


@pytest.mark.parametrize('injection', ['claude -p', 'read ~/.claude/projects', 'Use Write', 'Agent helper', 'Grep searches', 'TaskCreate({})', 'TaskUpdate({})', 'AskUserQuestion({})', 'os.environ.get("CLAUDE_CODE_SESSION_ID")'])
def test_shared_skill_host_injection_fails(injection):
    assert lint.lint_source('skills/recall/SKILL.md', injection)


@pytest.mark.parametrize('injection', ['subprocess.run(["claude", "-p"])', 'Path.home() / ".claude"', 'os.environ.get("CODEX_HOME")', 'TaskCreate({})'])
def test_shared_python_host_injection_fails(injection):
    assert lint.lint_source('hooks/shared.py', 'def operation():\n    ' + injection + '\n')


def test_config_host_enums_and_legacy_taxonomy_are_valid():
    assert not lint.lint_source('hooks/shared.py', 'HOSTS = {"claude", "codex"}\nFOLDER = "claude-sessions"\nTYPE = "claude-wiki"\n')


def test_named_adapter_does_not_exempt_other_function():
    source = 'def native_root():\n    return "~/.claude"\ndef unrelated():\n    return "~/.claude"\n'
    adapters = {'hooks/runtime_adapters/source.py:native_root': {'claude': 'supported', 'codex': 'supported'}}
    errors = lint.lint_source('hooks/runtime_adapters/source.py', source, adapters)
    assert [(number, code) for number, code, _ in errors] == [(4, 'native_path')]


def test_adapter_requires_both_host_decisions():
    source = 'def native_root():\n    return "~/.claude"\n'
    key = 'hooks/runtime_adapters/source.py:native_root'
    assert lint.lint_source('hooks/runtime_adapters/source.py', source, {key: {'claude': 'supported'}})[0][1] == 'adapter_capability_invalid'
    assert not lint.lint_source('hooks/runtime_adapters/source.py', source, {key: {'claude': 'supported', 'codex': 'unsupported:no native memory API'}})


def test_reference_requires_pair(tmp_path):
    refs = tmp_path / 'skills' / 'recall' / 'references'
    refs.mkdir(parents=True)
    (refs / 'host-claude.md').write_text('claude -p')
    assert lint.lint_reference_pairs(tmp_path)
    (refs / 'host-codex.md').write_text('codex exec')
    assert not lint.lint_reference_pairs(tmp_path)


def test_authored_draft_has_no_host_assumptions_and_exact_six_pairs():
    root = Path(__file__).parents[1]
    sources = list((root / 'skills').glob('*/SKILL.md'))
    assert len(sources) == 19
    for path in sources:
        assert not lint.lint_source(path.relative_to(root), path.read_text()), path
    assert not lint.lint_reference_pairs(root)
    pairs = {path.parent.parent.name for path in (root / 'skills').glob('*/references/host-claude.md')}
    assert pairs == {'standup', 'emerge', 'recall', 'obsidian-setup', 'retro', 'vault-doctor'}
    for name in ('vault-search', 'vault-ask', 'link', 'standup'):
        text = (root / 'skills' / name / 'SKILL.md').read_text()
        assert "--operation 'grep'" in text
    text = (root / 'skills' / 'emerge' / 'SKILL.md').read_text()
    assert 'emerge-analysis.md' in text and '<registered analysis.json>' not in text
    assert 'the parent stores it with `artifact-store`' in text


@pytest.mark.parametrize('command', ['["claude", "--model", "haiku", "-p"]', '["claude",\n        "--model",\n        "haiku",\n        "-p"]', '["codex", "--config", "fixture", "exec"]'])
def test_flagged_or_multiline_native_command_cannot_hide(command):
    assert lint.lint_source('hooks/shared.py', 'def command():\n    return ' + command + '\n')


def test_module_key_never_exempts_shared_host_constants():
    errors = lint.lint_source('hooks/shared.py', 'ROOT = "~/.claude"\n', {'hooks/shared.py:<module>': {'claude': 'supported', 'codex': 'supported'}})
    assert errors and errors[0][1] == 'native_path'


def test_native_command_adapter_cannot_omit_other_host():
    source = 'def command():\n    return ["claude", "--model", "haiku", "-p"]\n'
    key = 'hooks/runtime_adapters/ai.py:command'
    assert lint.lint_source('hooks/runtime_adapters/ai.py', source, {key: {'claude': 'supported'}})
    assert not lint.lint_source('hooks/runtime_adapters/ai.py', source, {key: {'claude': 'supported', 'codex': 'supported'}})


def _repository(tmp_path, source='def native_root():\n    return "~/.claude"\n'):
    path = tmp_path / 'hooks' / 'adapter.py'
    path.parent.mkdir()
    path.write_text(source)
    skill = tmp_path / 'skills' / 'example' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('Shared native capability.')
    return tmp_path


def test_repository_rejects_undeclared_host_use(tmp_path):
    root = _repository(tmp_path)
    errors = lint.lint_repository(root, {})
    assert any(path == 'hooks/adapter.py' and code == 'native_path' for path, _, code, _ in errors)


@pytest.mark.parametrize('key,decision', [
    ('hooks/missing.py:native_root', {'claude': 'supported', 'codex': 'supported'}),
    ('hooks/adapter.py:missing', {'claude': 'supported', 'codex': 'supported'}),
    ('hooks/adapter.py:<module>', {'claude': 'supported', 'codex': 'supported'}),
    ('hooks/adapter.py:native_root', {'claude': 'supported'}),
    ('hooks/adapter.py:native_root', {'claude': 'supported', 'codex': 'unsupported:  '}),
    ('../outside.py:native_root', {'claude': 'supported', 'codex': 'supported'}),
])
def test_declared_adapter_requires_real_function_and_both_host_decisions(tmp_path, key, decision):
    root = _repository(tmp_path)
    assert lint.lint_repository(root, {key: decision})


def test_repository_exact_adapter_does_not_cover_inserted_neighbor(tmp_path):
    root = _repository(tmp_path, 'def native_root():\n    return "~/.claude"\ndef new_writer():\n    return "~/.claude"\n')
    adapters = {'hooks/adapter.py:native_root': {'claude': 'supported', 'codex': 'unsupported:no native API'}}
    errors = lint.lint_repository(root, adapters)
    assert [(path, number, code) for path, number, code, _ in errors] == [('hooks/adapter.py', 4, 'native_path')]


def test_repository_adapter_cannot_exempt_an_entire_skill(tmp_path):
    root = _repository(tmp_path)
    (root / 'skills/example/SKILL.md').write_text('Use Write')
    adapters = {'hooks/adapter.py:native_root': {'claude': 'supported', 'codex': 'supported'}}
    assert any(path == 'skills/example/SKILL.md' for path, _, _, _ in lint.lint_repository(root, adapters))


def test_cli_rejects_duplicate_adapter_decisions(tmp_path, capsys):
    root = _repository(tmp_path)
    manifest = root / 'adapters.json'
    manifest.write_text('{"hooks/adapter.py:native_root":{},"hooks/adapter.py:native_root":{}}')
    assert lint.main(['--root', str(root), '--adapters', str(manifest)]) == 1
    assert 'Duplicate adapter key' in capsys.readouterr().err


def test_actual_repository_host_boundary():
    import json
    root = Path(__file__).parents[1]
    decisions = json.loads((root / 'docs/parity/host-adapters.json').read_text())
    assert lint.lint_repository(root, decisions) == []


def test_docs_only_preflight_cannot_skip_shared_host_rule(tmp_path):
    import json
    import os
    import shutil
    import subprocess
    root = _repository(tmp_path)
    (root / 'skills/example/SKILL.md').write_text('Use Write')
    scripts = root / 'scripts/ci-checks'
    scripts.mkdir(parents=True)
    repo = Path(__file__).parents[1]
    shutil.copyfile(repo / 'scripts/commit-preflight.sh', root / 'scripts/commit-preflight.sh')
    shutil.copyfile(SOURCE, scripts / 'host_neutral_lint.py')
    manifest = root / 'docs/parity/host-adapters.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'hooks/adapter.py:native_root': {'claude': 'supported', 'codex': 'supported'}}))
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1')
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
        environment.pop(key, None)
    subprocess.run(['git', 'init', '-q', str(root)], env=environment, check=True, timeout=5)
    subprocess.run(['git', '-C', str(root), 'add', '.'], env=environment, check=True, timeout=5)
    result = subprocess.run(['bash', 'scripts/commit-preflight.sh', '--docs-only'], cwd=root,
                            env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode != 0
    assert 'skills/example/SKILL.md:1: native_tool' in result.stderr
    assert 'PREFLIGHT PASSED' not in result.stdout
