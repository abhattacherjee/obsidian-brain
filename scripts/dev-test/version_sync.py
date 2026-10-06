"""Stage synchronized release metadata, then publish with exact-byte rollback."""
import argparse
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

VERSION = re.compile(r'([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})(?:[-+][0-9A-Za-z.+-]+)?\Z')


def bump(current, selection):
    match = VERSION.fullmatch(current) if isinstance(current, str) else None
    if match is None:
        raise ValueError('Malformed current version; expected X.Y.Z')
    major, minor, patch = map(int, match.groups())
    if selection == 'major':
        result = f'{major + 1}.0.0'
    elif selection == 'minor':
        result = f'{major}.{minor + 1}.0'
    elif selection == 'patch':
        result = f'{major}.{minor}.{patch + 1}'
    elif re.fullmatch(r'[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}', selection):
        result = selection
    else:
        raise ValueError('Invalid version selection; expected major, minor, patch or X.Y.Z')
    if not VERSION.fullmatch(result):
        raise ValueError('Computed version exceeds supported numeric bounds')
    return result


def _stage(path, content, mode):
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.version-stage-')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        return Path(temporary)
    except BaseException:
        os.unlink(temporary)
        raise


def _recover_pending(root):
    journal = root / '.version-sync.pending.json'
    if not journal.exists():
        return False
    if journal.is_symlink() or journal.stat().st_mode & 0o077:
        raise ValueError('Version recovery journal is not private')
    if journal.stat().st_size > 4_000_000:
        raise ValueError('Version recovery journal exceeds its limit')
    items = json.loads(journal.read_bytes())
    allowed = {'.claude-plugin/plugin.json', '.codex-plugin/plugin.json',
               '.claude-plugin/marketplace.json', 'docs/architecture/architecture.json'}
    if not isinstance(items, list) or not items or len(items) > 4:
        raise ValueError('Invalid version recovery journal')
    names = [item['path'] for item in items]
    if len(set(names)) != len(names) or not set(names).issubset(allowed):
        raise ValueError('Version recovery paths are not release metadata')
    for item in items:
        path = root / item['path']
        old, new = bytes.fromhex(item['old']), bytes.fromhex(item['new'])
        if path.is_symlink() or path.read_bytes() not in (old, new):
            raise ValueError('Release metadata changed since interruption; refusing recovery')
    for item in items:
        path = root / item['path']
        temporary = _stage(path, bytes.fromhex(item['old']), item['mode'])
        os.replace(temporary, path)
    journal.unlink()
    return True


def _run_owned(root, selection, replace=os.replace):
    root = Path(root).resolve()
    descriptor_paths = [root / '.claude-plugin/plugin.json', root / '.codex-plugin/plugin.json']
    marketplace = root / '.claude-plugin/marketplace.json'
    paths = descriptor_paths + [marketplace]
    architecture = root / 'docs/architecture/architecture.json'
    if architecture.exists():
        paths.append(architecture)
    originals = {}
    values = {}
    modes = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or symlinked release metadata')
        originals[path] = path.read_bytes()
        values[path] = json.loads(originals[path])
        modes[path] = path.stat().st_mode & 0o777
    claude, codex = (values[path] for path in descriptor_paths)
    if not isinstance(claude, dict) or not isinstance(codex, dict):
        raise ValueError('Plugin descriptors must be JSON objects')
    if claude.get('name') != codex.get('name') or not claude.get('name'):
        raise ValueError('Host descriptors must identify the same plugin')
    if claude.get('version') != codex.get('version'):
        raise ValueError('Host descriptor versions differ before bump')
    current_version = claude.get('version')
    next_version = bump(current_version, selection)
    catalog = values[marketplace]
    plugins = catalog.get('plugins') if isinstance(catalog, dict) else None
    if not isinstance(plugins, list):
        raise ValueError('Marketplace must contain a plugins array')
    matches = [entry for entry in plugins if isinstance(entry, dict) and entry.get('name') == claude['name']]
    if len(matches) != 1:
        raise ValueError('Marketplace must contain exactly one matching plugin entry')
    if matches[0].get('version') != claude['version']:
        raise ValueError('Marketplace version differs before bump')
    for path in descriptor_paths:
        values[path]['version'] = next_version
    matches[0]['version'] = next_version
    if architecture in values:
        values[architecture]['version'] = next_version
    staged = {}
    rollback = {}
    published = []
    try:
        for path in paths:
            if path.read_bytes() != originals[path]:
                raise ValueError('Release metadata changed during preparation')
            staged[path] = _stage(path, (json.dumps(values[path], indent=2) + '\n').encode(), modes[path])
            rollback[path] = _stage(path, originals[path], modes[path])
        journal = root / '.version-sync.pending.json'
        journal_data = [{'path': str(path.relative_to(root)), 'old': originals[path].hex(),
                         'new': staged[path].read_bytes().hex(), 'mode': modes[path]}
                        for path in paths]
        temporary = _stage(journal, json.dumps(journal_data).encode(), 0o600)
        os.replace(temporary, journal)
        try:
            for path in paths:
                replace(staged[path], path)
                published.append(path)
        except BaseException:
            for path in reversed(published):
                os.replace(rollback[path], path)
            journal.unlink()
            raise
        journal.unlink()
    finally:
        for temporary in list(staged.values()) + list(rollback.values()):
            if temporary.exists():
                temporary.unlink()
    print(f'Version bumped from {current_version} to {next_version}; both hosts and marketplace synchronized.')
    return next_version


def run(root, selection, replace=os.replace):
    root = Path(root).resolve()
    descriptor = os.open(root / '.version-sync.lock',
                         os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _recover_pending(root):
            raise ValueError('Recovered interrupted version bump; re-run the requested bump')
        return _run_owned(root, selection, replace)
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('selection')
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        run(args.root, args.selection)
    except (OSError, ValueError, KeyError) as exc:
        print('ERROR: ' + str(exc))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
