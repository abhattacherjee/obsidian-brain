"""Execute the loaded-SKILL resource contract, without cwd or cache guessing."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from coverage_test_helpers import verify_installed_hooks

REPO = Path(__file__).resolve().parents[1]
SKILLS = ('obsidian-setup', 'vault-config', 'recall', 'vault-search', 'vault-ask',
          'compress', 'decide', 'error-log', 'retro', 'standup', 'check-items',
          'consolidate', 'emerge', 'link', 'vault-import', 'vault-doctor',
          'vault-stats', 'vault-reindex', 'dev-test')
ROOT_CODE = ('import pathlib,sys; p=pathlib.Path(sys.argv[1]); assert p.is_absolute(); '
             'p=p.resolve(); assert p.name == "SKILL.md" and p.parent.parent.name == "skills"; '
             'print(p.parents[2])')
ROOT_RE = re.compile(r"^OB_RESOURCE_ROOT=\$\(python3 -c '(.*?)' \"\$OB_SKILL_PATH\"\)$", re.MULTILINE)


def _root_code(skill):
    text = (REPO / 'skills' / skill / 'SKILL.md').read_text()
    matches = ROOT_RE.findall(text)
    assert len(matches) == 1, f'{skill} must have one loaded-path bootstrap'
    return matches[0]


def _context_command(skill):
    text = (REPO / 'skills' / skill / 'SKILL.md').read_text()
    matches = [line for line in text.splitlines()
               if line.startswith('python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py"')
               and ' context < /dev/null' in line]
    initial = [line for line in matches if '--vault' not in line]
    assert len(initial) == 1, f'{skill} must look up configured state once'
    retries = [line for line in matches if '--vault' in line]
    if skill == 'obsidian-setup':
        assert len(retries) == 1 and '--vault "$OB_VAULT"' in retries[0]
        assert text.index('### Step 3 — Validate the vault path') < text.index(retries[0])
    else:
        assert retries == [], f'{skill} must not override the configured vault'
    return initial[0]


@pytest.mark.parametrize('skill', SKILLS)
def test_every_skill_uses_the_same_loaded_resource_contract(skill):
    assert _root_code(skill) == ROOT_CODE
    command = _context_command(skill)
    for name in ('OB_HOST', 'OB_CLIENT', 'OB_RESOURCE_ROOT', 'OB_SESSION_ID', 'OB_CWD'):
        assert '"$' + name + '"' in command
    text = (REPO / 'skills' / skill / 'SKILL.md').read_text()
    assert 'known_marketplaces.json' not in text
    assert 'glob.glob(os.path.expanduser' not in text
    assert '--skill-path "$OB_SKILL_PATH"' in text


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / 'loaded installation with spaces'
    shutil.copytree(REPO / 'hooks', root / 'hooks', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(REPO / 'skills', root / 'skills')
    for descriptor in ('.claude-plugin', '.codex-plugin'):
        (root / descriptor).mkdir()
        (root / descriptor / 'plugin.json').write_text('{"name":"obsidian-brain"}')
    home = tmp_path / 'home'
    vault = tmp_path / 'selected vault'
    vault.mkdir()
    cwd = tmp_path / 'unrelated cwd with spaces'
    cwd.mkdir()
    poison = home / '.claude/plugins/cache/obsidian-brain-repo/obsidian-brain/999.999/hooks'
    poison.mkdir(parents=True)
    (poison / 'brain_cli.py').write_text('raise RuntimeError("POISONED CACHE SELECTED")')
    (home / '.claude/plugins/known_marketplaces.json').write_text('{broken registry')
    for host in ('claude', 'codex'):
        native = home / ('.' + host)
        native.mkdir(exist_ok=True)
        (native / 'obsidian-brain-config.json').write_text(json.dumps({'vault_path': str(vault)}))
    env = dict(os.environ, HOME=str(home), CLAUDE_CONFIG_DIR=str(home / '.claude'),
               CODEX_HOME=str(home / '.codex'), OBSIDIAN_BRAIN_DB=str(tmp_path / 'index.db'),
               OBSIDIAN_BRAIN_STATE_DIR=str(tmp_path / 'state'), OB_CLIENT='cli',
               OB_SESSION_ID='synthetic-loaded-skill', OB_CWD=str(cwd), OB_VAULT=str(vault))
    env.pop('OBSIDIAN_BRAIN_CONFIG', None)
    return root, home, vault, cwd, env


def _resolve(skill, loaded, cwd, env, root):
    verify_installed_hooks(root)
    return subprocess.run([sys.executable, '-c', _root_code(skill), str(loaded)],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize('skill', SKILLS)
def test_file_derived_resolver_selects_loaded_tree_not_cwd_or_poison_cache(skill, installed):
    root, _, _, cwd, env = installed
    result = _resolve(skill, root / 'skills' / skill / 'SKILL.md', cwd, env, root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(root)


@pytest.mark.parametrize('host', ['claude', 'codex'])
@pytest.mark.parametrize('skill', ['recall', 'obsidian-setup', 'dev-test'])
def test_file_derived_launcher_uses_native_config_and_selected_installation(host, skill, installed, selected_host_context):
    root, home, vault, cwd, env = installed
    loaded = root / 'skills' / skill / 'SKILL.md'
    env = dict(env, OB_HOST=host, OB_CLIENT=selected_host_context.client, OB_SKILL_PATH=str(loaded))
    resolve = _resolve(skill, loaded, cwd, env, root)
    assert resolve.returncode == 0, resolve.stderr
    env['OB_RESOURCE_ROOT'] = resolve.stdout.strip()
    verify_installed_hooks(root)
    result = subprocess.run(['bash', '-c', _context_command(skill)], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert context['host'] == host
    assert context['resource_root'] == str(root)
    assert context['vault_path'] == str(vault)
    assert context['config_path'] == str(home / ('.' + host) / 'obsidian-brain-config.json')


@pytest.mark.parametrize('loaded', ['skills/recall/SKILL.md', '/tmp/WRONG.md'])
def test_bad_loaded_path_refuses_instead_of_falling_back(loaded, installed):
    root, _, _, cwd, env = installed
    result = _resolve('recall', loaded, cwd, env, root)
    assert result.returncode != 0
    assert not result.stdout.strip()


def test_operation_cannot_mix_loaded_skill_and_another_launcher(installed, selected_host_context):
    root, _, _, cwd, env = installed
    verify_installed_hooks(root)
    result = subprocess.run([sys.executable, str(root / 'hooks/brain_cli.py'),
        '--host', selected_host_context.host, '--client', selected_host_context.client, '--resource-root', str(root),
        '--session-id', 'synthetic-loaded-skill', '--cwd', str(cwd), 'run',
        '--skill-path', str(REPO / 'skills/recall/SKILL.md'), '--operation', 'config'],
        input='{}', cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'loaded installation' in result.stderr


def test_dev_install_passes_loaded_root_even_when_cwd_and_cache_disagree(installed, selected_host_context):
    root, _, _, cwd, env = installed
    verify_installed_hooks(root)
    scripts = root / 'scripts'
    scripts.mkdir()
    (scripts / 'test-dev-skill.sh').write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    text = (REPO / 'skills/dev-test/SKILL.md').read_text()
    commands = [line for line in text.splitlines()
                if line.startswith('python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py"')
                and "--operation 'dev-install'" in line]
    assert commands, 'dev-test must invoke its trusted install procedure'
    request = cwd / 'request.json'
    payload = {'mode': 'status'}
    expected = ['status', '--host', selected_host_context.host, '--source', str(root)]
    if selected_host_context.host == 'codex':
        cache = selected_host_context.native_home / 'plugins/cache/fixture/obsidian-brain/3.8.1'
        cache.mkdir(parents=True)
        payload['cache_path'] = str(cache)
        expected.extend(['--cache-path', str(cache)])
    request.write_text(json.dumps(payload))
    env = dict(env, OB_HOST=selected_host_context.host, OB_CLIENT=selected_host_context.client, OB_RESOURCE_ROOT=str(root),
               OB_SKILL_PATH=str(root / 'skills/dev-test/SKILL.md'), REQUEST_PATH=str(request))
    result = subprocess.run(['bash', '-c', commands[0]], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected


def test_changed_loaded_hook_source_cannot_enter_combined_coverage(installed):
    root, _, _, _, _ = installed
    source = root / 'hooks' / 'skill_procedures.py'
    source.write_bytes(source.read_bytes() + b'\n# changed fixture source\n')
    with pytest.raises(AssertionError, match='Installed hook source changed'):
        verify_installed_hooks(root)


@pytest.fixture
def selected_host_context(host, installed, monkeypatch):
    from runtime_context import resolve_runtime_context, using_runtime_context
    root, home, vault, cwd, env = installed
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / '.claude'))
    monkeypatch.setenv('CODEX_HOME', str(home / '.codex'))
    selected = resolve_runtime_context(host, 'claude-code' if host == 'claude' else 'codex-cli',
        {'session_id': env['OB_SESSION_ID'], 'cwd': str(cwd)},
        {'config_path': home / ('.' + host) / 'obsidian-brain-config.json',
         'resource_root': root, 'index_path': home.parent / 'index.db',
         'state_path': home.parent / 'state'})
    with using_runtime_context(selected):
        yield selected
