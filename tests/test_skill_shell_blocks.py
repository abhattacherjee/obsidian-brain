"""Run every authored note publication through a real shell with hostile content."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from coverage_test_helpers import verify_installed_hooks as _verify_installed_hooks

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
OPERATIONS = {'note-create', 'note-append', 'summary-apply'}


def _collect_blocks():
    found = []
    for path in sorted((ROOT / 'skills').glob('*/SKILL.md')):
        for index, line in enumerate(path.read_text().splitlines()):
            match = re.search(r"--operation '([^']+)'", line)
            if line.startswith('python3 ') and match and match[1] in OPERATIONS:
                found.append((path.parent.name + '-' + str(index), path.parent.name, match[1], line))
    return found


BLOCKS = _collect_blocks()


def test_every_authored_publication_site_is_covered():
    assert len(BLOCKS) == 9
    assert {skill for _, skill, _, _ in BLOCKS} == {'compress', 'decide', 'standup', 'vault-import', 'retro', 'error-log', 'vault-stats'}
    for _, _, _, command in BLOCKS:
        assert '< "$REQUEST_PATH"' in command
        assert '<<' not in command
        assert '--skill-path "$OB_SKILL_PATH"' in command


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / 'installed plugin with spaces'
    shutil.copytree(ROOT / 'hooks', root / 'hooks', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'skills', root / 'skills')
    for host in ('claude', 'codex'):
        descriptor = root / ('.' + host + '-plugin')
        descriptor.mkdir()
        (descriptor / 'plugin.json').write_text('{"name":"obsidian-brain"}')
    return root


def _shell(tmp_path, installed, skill, command, payload, host):
    _verify_installed_hooks(installed)
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(payload))
    request.chmod(0o600)
    home = tmp_path / 'native home'
    home.mkdir(exist_ok=True)
    vault = tmp_path / 'vault'
    vault.mkdir(exist_ok=True)
    config = home / 'config.json'
    config.write_text(json.dumps({'vault_path': str(vault)}))
    config.chmod(0o600)
    environment = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / 'codex'), CLAUDE_CONFIG_DIR=str(home / 'claude'), CLAUDE_CODE_SESSION_ID='poisoned-other-session', CODEX_THREAD_ID='poisoned-other-thread', OB_SKILL_PATH=str(installed / 'skills' / skill / 'SKILL.md'), OB_RESOURCE_ROOT=str(installed), OB_HOST=host, OB_CLIENT='claude-code' if host=='claude' else 'codex-cli', OB_SESSION_ID='explicit-native-session', OB_CWD=str(tmp_path), REQUEST_PATH=str(request), TEST_CONFIG=str(config), TEST_VAULT=str(vault), TEST_STATE=str(home / 'state'), TEST_INDEX=str(home / 'index.sqlite3'))
    command = command.replace(' run ', ' --config "$TEST_CONFIG" --vault "$TEST_VAULT" --state "$TEST_STATE" --index "$TEST_INDEX" run ')
    return subprocess.run([BASH, '-c', command], cwd=tmp_path, env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)


def test_changed_installed_source_cannot_enter_combined_coverage(installed):
    source = installed / 'hooks' / 'skill_procedures.py'
    source.write_bytes(source.read_bytes() + b'\n# changed fixture source\n')
    with pytest.raises(AssertionError, match='Installed hook source changed'):
        _verify_installed_hooks(installed)


@pytest.mark.skipif(BASH is None, reason='bash unavailable')
@pytest.mark.parametrize('host', ['claude','codex'])
@pytest.mark.parametrize('identifier,skill,operation,command', BLOCKS, ids=[item[0] for item in BLOCKS])
def test_authored_content_stays_data_and_collision_keeps_original(tmp_path, installed, host, identifier, skill, operation, command):
    def run(op, payload):
        selected = re.sub(r"--operation '[^']+'", "--operation '"+op+"'", command)
        result = _shell(tmp_path, installed, skill, selected, payload, host)
        assert result.returncode == 0, result.stdout + result.stderr
        return result
    prepared = json.loads(run('prepare', {}).stdout)
    operation_id = prepared['operation_id']
    marker = tmp_path / 'SIDE_EFFECT_MARKER'
    hostile = 'OB_NOTE_EOF\nOB_UPDATE_EOF\nOB_NOTE_EOF_beef\n$(touch ' + str(marker) + ')\n`touch ' + str(marker) + '`\ntouch ' + str(marker) + '\nSENTINEL-LAST-LINE-OF-NOTE\n'
    folder = 'claude-sessions' if skill == 'vault-import' else 'claude-insights'
    destination = tmp_path / 'vault' / folder / 'note.md'
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {'operation_id': operation_id}
    if operation == 'note-create':
        payload.update(folder=folder, filename='note.md', content='---\ntype: claude-session\ntags:\n  - claude/session\n---\n\n## Summary\n'+hostile)
        if skill == 'vault-import':
            source = tmp_path / 'historical.jsonl'
            source.write_text(json.dumps({'type':'user','sessionId':'original-claude','uuid':'one','message':{'content':'Visible source'}})+'\n')
            run('import-read', {'operation_id':operation_id,'source_host':'claude','source_session_id':'original-claude','source_path':str(source)})
    else:
        destination.write_text('---\ntype: claude-insight\ntags:\n  - claude/insight\n---\n\nOriginal prose\n')
        source = json.loads(run('note-read', {'operation_id':operation_id,'path':str(destination)}).stdout)
        payload.update(path=str(destination), expected_revision=source['expected_revision'])
        payload['update_text' if operation=='note-append' else 'summary'] = '## Update (2026-10-05)\n'+hostile if operation=='note-append' else '## Summary\n'+hostile
    run(operation, payload)
    written = destination.read_text()
    assert 'SENTINEL-LAST-LINE-OF-NOTE' in written
    assert 'OB_NOTE_EOF_beef' in written
    assert not marker.exists()
    assert 'poisoned-other' not in written
    from obsidian_utils import parse_frontmatter_field
    assert parse_frontmatter_field(written, 'author_host') == host
    if operation=='note-create':
        repeated = _shell(tmp_path, installed, skill, command, payload, host)
        assert repeated.returncode != 0
        assert destination.read_text() == written
