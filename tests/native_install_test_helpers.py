"""Isolated installer metadata transport; no installed client or model runs."""
import os
from pathlib import Path


def native_folder():
    from runtime_context import current_runtime_context
    return '.codex' if current_runtime_context().host == 'codex' else '.claude'


def metadata_environment(home):
    home = Path(home)
    binary = home / 'metadata-bin'
    binary.mkdir(parents=True, exist_ok=True)
    executable = binary / 'codex'
    executable.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    if message['method'] == 'initialize':
        result = {}
    elif message['method'] == 'hooks/list':
        root = Path(os.environ['CODEX_HOME']) / 'plugins/cache'
        hooks = []
        for cache in root.glob('*/obsidian-brain/*'):
            if not cache.is_dir():
                continue
            descriptor = cache / '.codex-plugin/plugin.json'
            if descriptor.exists():
                selected = cache / json.loads(descriptor.read_text())['hooks']
            else:
                selected = cache / 'hooks/hooks.json'
            if selected.is_file():
                hooks.append({'pluginId':'obsidian-brain@' + cache.parent.parent.name,
                              'sourcePath':str(selected.resolve())})
        result = {'data':[{'errors':[], 'hooks':hooks}]}
    else:
        raise AssertionError('Installer requested non-metadata operation')
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}), flush=True)
''')
    executable.chmod(0o700)
    return dict(os.environ, HOME=str(home), CODEX_HOME=str(home / '.codex'),
                CLAUDE_CONFIG_DIR=str(home / '.claude'),
                PATH=str(binary) + os.pathsep + os.environ.get('PATH',''))


def backup_path(home, cache):
    """Mirror the selected host's documented recovery location."""
    if native_folder() == '.claude':
        return cache.with_name(cache.name + '.bak')
    import hashlib
    identity = hashlib.sha256(str(cache.absolute()).encode()).hexdigest()
    return Path(home) / '.codex/.obsidian-brain-dev-install' / identity / 'backup'
