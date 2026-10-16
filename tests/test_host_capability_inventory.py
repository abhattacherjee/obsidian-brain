"""Exact inventory changes need an explicit capability review."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil

import pytest
from parity_test_helpers import REPO

DRAFT = Path(__file__).resolve().parents[1]


def scanner():
    spec = importlib.util.spec_from_file_location('capability_inventory', DRAFT / 'scripts/ci-checks/capability-inventory.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot():
    return json.loads((DRAFT / 'docs/parity/observed-surface-inventory.json').read_text())


def test_current_surface_matches_reviewed_inventory():
    scanner().validate(snapshot(), scanner().discover(REPO))


@pytest.mark.parametrize('section', ['skills', 'legacy_hook_events', 'wiki_commands', 'doctor_checks'])
def test_missing_inventory_entry_fails(section):
    expected = snapshot()
    mutated = copy.deepcopy(expected)
    mutated[section].pop()
    with pytest.raises(ValueError, match=section):
        scanner().validate(expected, mutated)


def test_actual_registry_source_change_is_detected_after_other_root_was_scanned(tmp_path):
    inventory = scanner()
    original = inventory.discover(REPO)
    for directory in ('hooks', 'scripts', 'skills'):
        shutil.copytree(REPO / directory, tmp_path / directory,
                        ignore=shutil.ignore_patterns('__pycache__'))
    path = tmp_path / 'hooks/skill_procedures.py'
    path.write_text(path.read_text() + '\nOPERATIONS["recall"]["unreviewed-operation"] = _config\n')
    observed = inventory.discover(tmp_path)
    assert 'unreviewed-operation' not in original['skill_operations']['recall']
    assert 'unreviewed-operation' in observed['skill_operations']['recall']
    with pytest.raises(ValueError, match='skill_operations'):
        inventory.validate(original, observed)


def test_unknown_registry_update_cannot_silently_disappear(tmp_path):
    path = tmp_path / 'registry.py'
    path.write_text('SKILLS = ("recall",)\n'
                    'OPERATIONS = {name: {"config": _config} for name in SKILLS}\n'
                    'OPERATIONS["recall"].update(dynamic_operations())\n')
    with pytest.raises(ValueError, match='Unresolved registry declaration'):
        scanner().operation_inventory(path)


def test_new_environment_tuple_member_cannot_hide_from_inventory(tmp_path):
    for directory in ('hooks', 'scripts', 'skills'):
        shutil.copytree(REPO / directory, tmp_path / directory, ignore=shutil.ignore_patterns('__pycache__'))
    path = tmp_path / 'hooks' / 'memory_sources.py'
    path.write_text(path.read_text() + '\nNEW_MARKERS = ("UNDECLARED_NATIVE_HOME",)\nfor marker in NEW_MARKERS:\n    os.environ.get(marker)\n')
    discovered = scanner().discover(tmp_path)
    assert 'UNDECLARED_NATIVE_HOME' in discovered['environment_reads']['hooks/memory_sources.py']
    with pytest.raises(ValueError, match='environment_reads'):
        scanner().validate(snapshot(), discovered)


def test_payload_inventory_distinguishes_event_handlers():
    import ast
    function = ast.parse('''def dispatch(event, payload):
    if event == "stop":
        payload.get("stop_hook_active")
    if event == "pre_compact" and payload.get("trigger") == "manual":
        payload["compact_details"]
''').body[0]
    inventory = scanner()
    assert inventory.fields_for_event(function, 'stop') == {'stop_hook_active'}
    assert inventory.fields_for_event(function, 'pre_compact') == {'trigger', 'compact_details'}
    assert inventory.fields_for_event(function, 'session_end') == set()


def test_new_stop_payload_field_cannot_hide_behind_existing_event(tmp_path):
    for directory in ('hooks', 'scripts', 'skills'):
        shutil.copytree(REPO / directory, tmp_path / directory,
                        ignore=shutil.ignore_patterns('__pycache__'))
    path = tmp_path / 'hooks/native_lifecycle.py'
    path.write_text(path.read_text().replace('if event == "stop":',
        'if event == "stop":\n            payload.get("NEW_STOP_FIELD")', 1))
    discovered = scanner().discover(tmp_path)
    assert 'NEW_STOP_FIELD' in discovered['native_payload_fields_by_handler']['stop']
    assert 'NEW_STOP_FIELD' not in discovered['native_payload_fields_by_handler']['session_end']
    with pytest.raises(ValueError, match='native_payload_fields_by_handler'):
        scanner().validate(snapshot(), discovered)


def test_environment_keys_follow_their_own_lexical_loop():
    import ast
    tree = ast.parse('import os\nMARKERS=("REAL_MARKER",)\ndef actual():\n    env=os.environ\n    for name in MARKERS:\n        env.get(name)\ndef validator():\n    for name in ("Summary", "Key Decisions"):\n        required.get(name)\ndef shadow():\n    env={"row":1}\n    env.get("NOT_ENV")\n')
    environment, config, unresolved = scanner().source_reads(tree, {'MARKERS':['REAL_MARKER']})
    assert environment == {'REAL_MARKER'}
    assert not config and not unresolved


def test_config_rows_are_not_selected_configuration():
    import ast
    tree = ast.parse('def rows(c):\n    c.get("classification")\n    c["evidence_citation"]\ndef selected(ctx):\n    cfg=ctx.config\n    alias=cfg\n    alias.get("vault_path")\n    ctx.config["codex_ai_model"]\ndef loaded():\n    c=load_config()\n    c.get("wiki_folder")\n')
    environment, config, unresolved = scanner().source_reads(tree, {})
    assert config == {'vault_path', 'codex_ai_model', 'wiki_folder'}
    assert not environment and not unresolved


def test_unknown_real_environment_key_remains_unresolved():
    import ast
    tree = ast.parse('from os import environ as env\ndef dynamic(key):\n    env.get(key)\n')
    environment, config, unresolved = scanner().source_reads(tree, {})
    assert not environment and not config
    assert unresolved == [{'line':3, 'expression':'env.get(key)'}]


def test_environment_input_table_uses_values_not_operation_names():
    import ast
    tree = ast.parse('''import os
INPUTS={'recall': {'run':['REAL_INPUT'], 'other':['OTHER_INPUT']}}
def dispatch(skill, operation):
    allowed=INPUTS.get(skill, {}).get(operation, ())
    saved={key: os.environ.get(key) for key in allowed}
    for key in allowed:
        os.environ.pop(key, None)
    for key, value in saved.items():
        os.environ[key]=value
''')
    environment, config, unresolved = scanner().source_reads(tree, {})
    assert environment == {'REAL_INPUT', 'OTHER_INPUT'}
    assert not config and not unresolved


def test_selected_json_configuration_has_real_runtime_keys():
    observed = scanner().discover(REPO)
    assert {'vault_path', 'index_path'} <= set(observed['config_reads']['hooks/runtime_context.py'])
    assert 'auto_log_enabled' in observed['config_reads']['hooks/native_lifecycle.py']
    assert {'model', 'mcp_servers', 'plugins'} <= set(observed['native_config_reads']['hooks/ai_adapters/codex.py'])


def test_json_payload_does_not_become_configuration():
    import ast
    tree = ast.parse('''import json
def request(payload):
    c=json.loads(payload)
    c.get('classification')
def config():
    config_path=home / 'obsidian-brain-config.json'
    selected=json.loads(config_path.read_text()) if config_path.exists() else {}
    alias=selected
    alias.get('vault_path')
''')
    environment, config, unresolved = scanner().source_reads(tree, {})
    assert config == {'vault_path'}
    assert not environment and not unresolved


def test_environment_and_config_assignments_are_not_reads():
    import ast
    tree = ast.parse('''import os
def assign(ctx):
    env=dict(os.environ)
    env['NEW_CHILD_VARIABLE']='value'
    ctx.config['new_setting']='value'
''')
    environment, config, unresolved = scanner().source_reads(tree, {})
    assert not environment and not config and not unresolved
