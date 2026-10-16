"""Resolve the runtime beside an explicitly loaded script, never a cache guess."""
from pathlib import Path
import sys


def resolve(script):
    path = Path(script)
    if not path.is_absolute():
        raise ValueError('Loaded script path must be absolute')
    if path.is_symlink():
        raise ValueError('Loaded script must be a regular file')
    path = path.resolve(strict=True)
    if not path.is_file() or path.parent.name != 'scripts':
        raise ValueError('Loaded script must be directly under scripts')
    root = path.parent.parent
    if not (root / 'hooks/obsidian_utils.py').is_file():
        raise ValueError('Loaded runtime has no hooks sentinel')
    if not any((root / name / 'plugin.json').is_file() for name in ('.claude-plugin', '.codex-plugin')):
        raise ValueError('Loaded runtime has no plugin descriptor')
    return root


def selected_cache(value):
    """Validate an operator-selected distribution, without inferring its invoker."""
    import json
    import re
    if not value:
        raise ValueError('OB_CACHE_PATH must name the exact package directory')
    root = Path(value)
    if not root.is_absolute():
        raise ValueError('OB_CACHE_PATH must be absolute')
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError('OB_CACHE_PATH must not traverse symlinks')
    root = root.resolve(strict=True)
    descriptors = []
    for name in ('.claude-plugin', '.codex-plugin'):
        descriptor = root / name / 'plugin.json'
        if descriptor.exists():
            if descriptor.is_symlink() or descriptor.parent.is_symlink() or not descriptor.is_file():
                raise ValueError('Package descriptor must be a regular file')
            if descriptor.stat().st_size > 65536:
                raise ValueError('Package descriptor is too large')
            data = json.loads(descriptor.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('name') != 'obsidian-brain' or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?', str(data.get('version', ''))):
                raise ValueError('Package descriptor name/version is invalid')
            descriptors.append(data)
    if not descriptors:
        raise ValueError('Package has no verified plugin descriptor')
    if len({data['version'] for data in descriptors}) != 1:
        raise ValueError('Package descriptor versions differ')
    required = ['hooks/obsidian_utils.py', 'scripts/vault_doctor_checks/snapshot_integrity.py', 'skills/recall/SKILL.md']
    for data in descriptors:
        required.append(data.get('hooks', 'hooks/hooks.json'))
    for name in required:
        if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Package resource must be relative and contained')
        path = root / name
        if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
            raise ValueError('Package resource sentinel is missing or symlinked')
        if root not in path.resolve(strict=True).parents:
            raise ValueError('Package resource leaves the selected directory')
    return root


if __name__ == '__main__':
    try:
        print(selected_cache(sys.argv[2]) if sys.argv[1] == '--cache-path' else resolve(sys.argv[1]))
    except (ValueError, OSError, IndexError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
