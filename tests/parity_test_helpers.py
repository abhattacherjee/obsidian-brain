"""Disposable contexts for scratch conformance; never native AI or live homes."""
import json
import os
from pathlib import Path
import sys
from dataclasses import dataclass, field

import pytest

REPO = Path(os.environ.get('OBSIDIAN_PARITY_REPO', Path(__file__).resolve().parents[1])).resolve()
sys.path[:0] = [str(REPO / 'hooks'), str(REPO / 'scripts')]


@pytest.fixture(params=['claude', 'codex'])
def host(request):
    return request.param


@pytest.fixture
def selected_host_context(host, tmp_path, monkeypatch):
    from runtime_context import resolve_runtime_context, using_runtime_context
    home = tmp_path / 'home'
    project = tmp_path / 'project'
    vault = tmp_path / 'vault'
    resources = tmp_path / 'plugin'
    for path in (home, project, vault, resources / 'hooks', resources / '.claude-plugin', resources / '.codex-plugin'):
        path.mkdir(parents=True)
    for directory in ('.claude-plugin', '.codex-plugin'):
        (resources / directory / 'plugin.json').write_text('{"name":"synthetic"}')
    config = tmp_path / 'selected.json'
    config.write_text(json.dumps({'vault_path': str(vault), 'min_duration_minutes': 0,
                                 'sessions_folder': 'claude-sessions',
                                 'insights_folder': 'claude-insights',
                                 'wiki_folder': 'claude-wiki',
                                 'codex_summary_model': 'synthetic-native-model'}))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(home / '.codex'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / '.claude'))
    for name in ('OBSIDIAN_BRAIN_CONFIG', 'OBSIDIAN_BRAIN_DB', 'OBSIDIAN_BRAIN_STATE_DIR', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
        monkeypatch.delenv(name, raising=False)
    selected = resolve_runtime_context(host, 'claude-code' if host == 'claude' else 'codex-cli',
        {'session_id': 'synthetic-session', 'cwd': str(project)},
        {'config_path': config, 'resource_root': resources})
    with using_runtime_context(selected):
        yield selected


@pytest.fixture
def context(selected_host_context):
    return selected_host_context


@dataclass
class HostIdentityScenario:
    """Explicit disposable actors for namespace and handoff comparisons."""
    root: Path
    actors: list = field(default_factory=list)

    def register(self, *contexts):
        from runtime_context import RuntimeContext
        for context in contexts:
            if not isinstance(context, RuntimeContext):
                raise AssertionError('Identity scenario requires real RuntimeContext actors')
            for path in (context.vault_path, context.state_path, context.config_path,
                         context.canonical_project_root):
                Path(path).resolve().relative_to(self.root.resolve())
            self.actors.append(context)

    def permits(self, api, context):
        return ((api.startswith(('note_transactions.', 'session_auxiliary_state.'))
                 or api in {'capture.publish_events', 'capture.read_cursor',
                            'capture.capture_checkpoint', 'capture.recover_registered',
                            'capture.recover_pending'})
                and any(context == actor for actor in self.actors))


@pytest.fixture
def host_identity_scenario(tmp_path):
    return HostIdentityScenario(tmp_path)
