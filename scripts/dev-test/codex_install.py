"""Private Codex dev-package swap with bounded native hook metadata checks."""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

_package_spec = importlib.util.spec_from_file_location(
    'dev_runtime_package_tree', Path(__file__).with_name('package_tree.py'))
_package_tree = importlib.util.module_from_spec(_package_spec)
_package_spec.loader.exec_module(_package_tree)

try:
    import tomllib
except ImportError:
    tomllib = None


def _toml_stdlib(content):
    """Validate the installer subset on Python 3.9 without external packages.

    Accept explicit tables, dotted keys, scalar values and one-line arrays or
    inline tables. Refuse unsupported TOML rather than ignore configuration.
    """
    import math

    def parts(text, delimiter, comments=False):
        result, start, quote, escaped, stack = [], 0, None, False, []
        for index, char in enumerate(text):
            if quote:
                if quote == '"' and escaped:
                    escaped = False
                elif quote == '"' and char == '\\':
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in {'"', "'"}:
                quote = char
            elif char == '#' and comments:
                text = text[:index]
                break
            elif char in '[{':
                stack.append(char)
                if len(stack) > 64:
                    raise ValueError('Unsupported TOML nesting depth')
            elif char in ']}':
                if not stack or stack.pop() != ('[' if char == ']' else '{'):
                    raise ValueError('Unsupported or malformed TOML framing')
            elif char == delimiter and not stack:
                result.append(text[start:index].strip())
                start = index + 1
        if quote or stack:
            raise ValueError('Unsupported multiline TOML')
        result.append(text[start:].strip())
        return result

    def string(text):
        if text.startswith("'") and text.endswith("'") and len(text) >= 2:
            body = text[1:-1]
            if "'" in body or any(ord(char) < 32 or ord(char) == 127 for char in body):
                raise ValueError('Unsupported TOML literal string')
            return body
        # JSON accepts escaped slashes; TOML does not. Keep the fallback strict.
        if not re.fullmatch(r'"(?:[^"\\\x00-\x1f\x7f]|\\(?:["\\btnfr]|u[0-9A-Fa-f]{4}))*"', text):
            raise ValueError('Unsupported TOML basic string')
        value = json.loads(text)
        if not isinstance(value, str):
            raise ValueError('TOML quoted value must be a string')
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('Invalid TOML Unicode scalar')
        return value

    def keys(text):
        result = []
        for item in parts(text, '.'):
            if item.startswith(('"', "'")):
                result.append(string(item))
            elif re.fullmatch(r'[A-Za-z0-9_-]+', item):
                result.append(item)
            else:
                raise ValueError('Unsupported TOML key')
        return tuple(result)

    def value(text):
        if text.startswith(('"', "'")):
            return string(text)
        if text in {'true', 'false'}:
            return text == 'true'
        if text.startswith('[') and text.endswith(']'):
            body = text[1:-1].strip()
            entries = parts(body, ',') if body else []
            if entries and not entries[-1]:
                entries.pop()
            return [value(item) for item in entries]
        if text.startswith('{') and text.endswith('}'):
            table = {}
            closed = set()
            body = text[1:-1].strip()
            for item in parts(body, ',') if body else []:
                fields = parts(item, '=')
                if len(fields) != 2:
                    raise ValueError('Unsupported TOML inline table')
                path = keys(fields[0])
                if any(path[:n] in closed for n in range(1, len(path))):
                    raise ValueError('TOML inline table cannot be extended')
                item_value = value(fields[1])
                assign(table, path, item_value)
                if isinstance(item_value, dict):
                    closed.add(path)
            return table
        if re.fullmatch(r'[+-]?(?:0|[1-9][0-9]*(?:_[0-9]+)*)', text):
            return int(text.replace('_', ''))
        if re.fullmatch(r'[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)', text):
            number = float(text)
            if math.isfinite(number):
                return number
        raise ValueError('Unsupported TOML value; configuration was not changed')

    def assign(table, path, item):
        for key in path[:-1]:
            table = table.setdefault(key, {})
            if not isinstance(table, dict):
                raise ValueError('TOML key changes an existing value type')
        if path[-1] in table:
            raise ValueError('Duplicate TOML key')
        table[path[-1]] = item

    text = content.decode('utf-8')
    if '\"\"\"' in text or "'''" in text:
        raise ValueError('Unsupported multiline TOML')
    document, table, table_path = {}, None, ()
    declared, assignment_tables, inline_tables = set(), set(), set()
    table = document
    for raw in text.splitlines():
        line = parts(raw, '\0', comments=True)[0]
        if not line:
            continue
        if line.startswith('['):
            if line.startswith('[[') or not line.endswith(']'):
                raise ValueError('Unsupported TOML table shape')
            path = keys(line[1:-1].strip())
            if path in declared or path in assignment_tables or any(path[:n] in inline_tables for n in range(1, len(path)+1)):
                raise ValueError('Duplicate or closed TOML table')
            table = document
            for key in path:
                table = table.setdefault(key, {})
                if not isinstance(table, dict):
                    raise ValueError('TOML table changes an existing value type')
            declared.add(path)
            table_path = path
            continue
        fields = parts(line, '=')
        if len(fields) != 2:
            raise ValueError('Unsupported TOML assignment')
        path = keys(fields[0])
        full_path = table_path + path
        if any(full_path[:n] in inline_tables for n in range(1, len(full_path))):
            raise ValueError('TOML inline table cannot be extended')
        item = value(fields[1])
        assign(table, path, item)
        assignment_tables.update(table_path + path[:n] for n in range(1, len(path)))
        if isinstance(item, dict):
            inline_tables.add(full_path)
    return document


def _toml(content):
    if tomllib is None:
        return _toml_stdlib(content)
    return tomllib.loads(content.decode('utf-8'))


def _no_links(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Install paths cannot contain symlinks')


def _atomic(path, data, mode=0o600):
    _no_links(path)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.dev-stage-')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _selection_block(content, key):
    value = _toml(content)
    plugins = value.get('plugins', {})
    if not isinstance(plugins, dict):
        raise ValueError('Codex plugins configuration must be a table')
    if b'"""' in content or b"'''" in content:
        raise ValueError('Multiline TOML cannot be changed safely by this installer')
    headers = list(re.finditer(rb'(?m)^[ \t]*\[[^\r\n]+\][ \t]*(?:#[^\r\n]*)?\r?\n', content))
    matching = []
    for index, header in enumerate(headers):
        try:
            table = _toml(header.group())
        except ValueError:
            continue
        if isinstance(table.get('plugins'), dict) and key in table['plugins']:
            matching.append((header.start(), headers[index + 1].start() if index + 1 < len(headers) else len(content)))
    if len(matching) > 1 or (key in plugins and not matching):
        raise ValueError('Plugin configuration must use one explicit table')
    if matching:
        start, end = matching[0]
        return value, content[start:end], (start, end)
    return value, None, (len(content), len(content))


def _patch_entry(content, key, replacement):
    before, old, (start, end) = _selection_block(content, key)
    prefix = content[:start]
    if old is None and replacement is not None and prefix and not prefix.endswith(b'\n'):
        prefix += b'\n'
    result = prefix + (replacement or b'') + content[end:]
    after = _toml(result)
    expected = copy.deepcopy(before)
    plugins = expected.setdefault('plugins', {})
    if replacement is None:
        plugins.pop(key, None)
    else:
        plugins[key] = _toml(replacement)['plugins'][key]
    # An empty plugins table is equivalent only if introduced by this update.
    if not plugins and 'plugins' not in before:
        expected.pop('plugins')
    if after != expected:
        raise ValueError('TOML edit changed unrelated configuration')
    return result


def _selected_hooks(package, require_codex=False):
    """Validate explicit native selection; old observed compatibility uses hooks/hooks.json."""
    descriptor = package / '.codex-plugin/plugin.json'
    if descriptor.exists():
        _no_links(descriptor)
        manifest = json.loads(descriptor.read_text())
        if not isinstance(manifest, dict) or manifest.get('name') != 'obsidian-brain':
            raise ValueError('Codex descriptor must identify obsidian-brain')
        hooks = manifest.get('hooks')
        if (not isinstance(hooks, str) or not hooks or '\0' in hooks
                or Path(hooks).is_absolute()):
            raise ValueError('Codex descriptor must select a relative hooks file')
        selected = package / hooks
        _no_links(selected)
        selected = selected.resolve()
        selected.relative_to(package.resolve())
    elif require_codex:
        raise ValueError('Codex descriptor is missing')
    else:
        legacy = package / '.claude-plugin/plugin.json'
        _no_links(legacy)
        manifest = json.loads(legacy.read_text())
        if not isinstance(manifest, dict) or manifest.get('name') != 'obsidian-brain':
            raise ValueError('Installed compatibility descriptor is invalid')
        selected = package / 'hooks/hooks.json'
    _no_links(selected)
    if not selected.is_file():
        raise ValueError('Selected Codex hooks file is missing')
    registration = json.loads(selected.read_text())
    if not isinstance(registration, dict) or not isinstance(registration.get('hooks'), dict):
        raise ValueError('Selected Codex hooks registration is invalid')
    return selected.resolve(), manifest


def _snapshot(source, destination):
    _package_tree.copy_runtime(source, destination)
    _, manifest = _selected_hooks(destination, require_codex=True)
    claude = json.loads((destination / '.claude-plugin/plugin.json').read_text())
    if (not isinstance(claude, dict) or claude.get('name') != manifest.get('name')
            or not isinstance(manifest.get('version'), str)
            or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]+)?', manifest['version'])
            or claude.get('version') != manifest['version']):
        raise ValueError('Host plugin descriptors disagree')
    required = ('hooks/brain_cli.py', 'scripts/vault_doctor.py',
                'scripts/doctor_repair_state.py', 'scripts/test-dev-skill.sh',
                'scripts/vault_doctor_checks/__init__.py')
    if (not all((destination / name).is_file() for name in required)
            or not any((destination / 'skills').glob('*/SKILL.md'))):
        raise ValueError('Runtime package is incomplete')


def validate_native_cache(home, plugin_id, hooks_result, cache_path):
    """Check all matching native hooks/list source paths; never select by age."""
    if not isinstance(plugin_id, str) or not plugin_id.startswith('obsidian-brain@'):
        raise ValueError('Select the exact native obsidian-brain plugin ID')
    cache = Path(cache_path).absolute()
    root = Path(home).absolute() / 'plugins/cache'
    relative = cache.relative_to(root)
    if (len(relative.parts) != 3 or relative.parts[1] != 'obsidian-brain'
            or relative.parts[0] != plugin_id.split('@', 1)[1]):
        raise ValueError('Native plugin ID does not match selected cache')
    matching = []
    def collect(value):
        if isinstance(value, dict):
            if value.get('pluginId') == plugin_id:
                source = value.get('sourcePath')
                if not isinstance(source, str) or not Path(source).is_absolute():
                    raise ValueError('Native hook source path is unavailable')
                path = Path(source)
                _no_links(path)
                parts = path.relative_to(root).parts
                if len(parts) < 4 or root.joinpath(*parts[:3]) != cache:
                    raise ValueError('Matching native hooks disagree on installed cache root')
                if not path.is_file():
                    raise ValueError('Native selected hooks file is missing')
                matching.append(path)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(hooks_result)
    if not matching:
        raise ValueError('No native hooks prove this installed cache')
    selected_hooks, _ = _selected_hooks(cache)
    if any(path.resolve() != selected_hooks for path in matching):
        raise ValueError('Native hooks do not match descriptor selection')
    return cache


def native_hooks_inventory(source, home):
    """Read installed hook selection through bounded native metadata RPC only."""
    sys.path.insert(0, str(Path(source) / 'hooks'))
    try:
        from ai_adapters.codex import NativeRpc
        from ai_backend import _BackendFailure
        binary = shutil.which('codex')
        if binary is None:
            raise ValueError('Native Codex metadata client is unavailable')
        environment = os.environ.copy()
        environment['CODEX_HOME'] = str(home)
        rpc = NativeRpc(binary, [], SimpleNamespace(worktree=Path(source)), environment,
                        time.monotonic() + 15)
        try:
            rpc.initialize()
            result = rpc.request('hooks/list', {'cwds': [str(Path(source).resolve())]})
            if not isinstance(result, dict) or not isinstance(result.get('data'), list):
                raise ValueError('Native hooks metadata is invalid')
            if any(not isinstance(group, dict) or group.get('errors') for group in result['data']):
                raise ValueError('Native hooks discovery failed')
            return result
        finally:
            rpc.close()
    except (ImportError, OSError) as exc:
        raise ValueError('Native Codex metadata is unavailable') from exc
    except _BackendFailure as exc:
        raise ValueError('Native Codex metadata request failed') from exc
    finally:
        sys.path.pop(0)


def _recovery_paths(home, cache):
    """Keep recoverable runtimes outside native plugin discovery."""
    identity = hashlib.sha256(str(cache.absolute()).encode()).hexdigest()
    root = home / '.obsidian-brain-dev-install' / identity
    return root / 'backup', root / 'config.json'


def _move_owned_tree(source, target):
    """Preserve root permissions when moving read-only directories across parents."""
    _package_tree._owned_path(source)
    mode = stat.S_IMODE(source.stat().st_mode)
    changed = not mode & stat.S_IWUSR
    if changed:
        source.chmod(mode | stat.S_IWUSR)
    try:
        os.rename(source, target)
    except BaseException:
        if changed:
            source.chmod(mode)
        raise
    if changed:
        try:
            target.chmod(mode)
        except BaseException:
            os.rename(target, source)
            source.chmod(mode)
            raise


def _private_recovery_directory(home, cache):
    root = _recovery_paths(home, cache)[0].parent
    _no_links(root)
    for path in (root.parent, root):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('Installer recovery directory is not private and owned')
    if root.stat().st_dev != cache.parent.stat().st_dev:
        raise ValueError('Installer recovery must be on the cache filesystem')
    return root


def _select(home, config, cache_path=None):
    if cache_path is not None:
        chosen = Path(cache_path).absolute()
        _no_links(chosen)
        relative = chosen.relative_to((home / 'plugins/cache').absolute())
        if (len(relative.parts) != 3 or relative.parts[1] != 'obsidian-brain'
                or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]+)?', relative.parts[2])
                or not (chosen.is_dir() or chosen.with_name(chosen.name + '.bak').is_dir()
                        or _recovery_paths(home, chosen)[0].is_dir())):
            raise ValueError('Explicit cache path is not an installed plugin version')
        return 'obsidian-brain@' + relative.parts[0], chosen
    raise ValueError('Codex dev install requires an explicit native installed --cache-path')


@contextlib.contextmanager
def _ownership(path):
    _no_links(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Installer lock is not private')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('Installer lock ownership changed')
        yield
    finally:
        os.close(descriptor)


def _validate_restore_backup(backup):
    """Reject an incomplete runtime before replacing the healthy installed cache."""
    _no_links(backup)
    for path in backup.rglob('*'):
        if path.is_symlink():
            raise ValueError('Backup contains a symlink')
    if not (backup / 'hooks/obsidian_utils.py').is_file() or not any(
            path.is_file() for path in (backup / 'skills').glob('*/SKILL.md')):
        raise ValueError('Not a complete plugin backup; installed cache was preserved')
    _selected_hooks(backup)


def run(mode, source, home, fault=lambda point: None, cache_path=None):
    source = Path(source).resolve() if mode == 'install' else None
    home = Path(home).absolute()
    _no_links(home)
    config_path = home / 'config.toml'
    original = config_path.read_bytes() if config_path.exists() else b''
    parsed = _toml(original)
    key, cache = _select(home, parsed, cache_path)
    backup, config_record = _recovery_paths(home, cache)
    legacy_backup = cache.with_name(cache.name + '.bak')
    legacy_record = legacy_backup.with_name(legacy_backup.name + '.config.json')
    if legacy_backup.exists() or legacy_record.exists():
        if backup.exists() or config_record.exists():
            raise ValueError('Both legacy and private backups exist; explicit recovery required')
        backup, config_record = legacy_backup, legacy_record
    if mode == 'status':
        print(json.dumps({'host': 'codex', 'version': cache.name, 'dev_active': backup.exists()}))
        return 0
    if mode == 'install':
        if source == home.resolve() or home.resolve() in source.parents:
            raise ValueError('Install source must be outside the selected native home')
        if not (source / 'hooks/obsidian_utils.py').is_file():
            raise ValueError('Source is not an obsidian-brain checkout')
    _no_links(cache)
    recovery = _private_recovery_directory(home, cache)
    with _ownership(recovery / '.dev-install.lock'):
        if config_path.exists() and config_path.read_bytes() != original:
            raise ValueError('Native configuration changed during preparation')
        if mode == 'restore':
            if not backup.is_dir() or not config_record.is_file():
                print('No complete backup recovery record found; nothing to restore.', file=sys.stderr)
                return 4
            _package_tree._owned_path(backup)
            _validate_restore_backup(backup)
            _no_links(config_record)
            record_info = config_record.stat()
            if record_info.st_uid != os.geteuid() or record_info.st_mode & 0o077:
                raise ValueError('Backup configuration record is not private and owned')
            record = json.loads(config_record.read_text())
            if record['key'] != key:
                raise ValueError('Backup belongs to another plugin selection')
            restored_config = _patch_entry(original, key, record['entry'].encode() if record['entry'] is not None else None)
            parked = recovery / 'restore.partial'
            if parked.exists():
                raise ValueError('A prior interrupted restore needs recovery')
            had_cache = cache.exists()
            if had_cache:
                _move_owned_tree(cache, parked)
            try:
                _move_owned_tree(backup, cache)
                _atomic(config_path, restored_config)
            except BaseException:
                if cache.exists():
                    _move_owned_tree(cache, backup)
                if had_cache:
                    _move_owned_tree(parked, cache)
                raise
            if had_cache:
                _package_tree.remove_owned_tree(parked)
            config_record.unlink()
            print('Original Codex cache and plugin configuration restored.')
            return 0
        if backup.exists() or config_record.exists():
            raise ValueError('Backup already exists; restore before another install')
        failed = recovery / 'failed.partial'
        if failed.exists():
            raise ValueError('A prior failed install needs recovery')
        validate_native_cache(home, key, native_hooks_inventory(source, home), cache)
        _, old_entry, _ = _selection_block(original, key)
        if old_entry is None:
            replacement = ('[plugins.' + json.dumps(key) + ']\nenabled = true\n').encode()
        else:
            enabled_lines = list(re.finditer(rb'(?m)^[ \t]*enabled[ \t]*=[^\r\n]*(?:\r?\n|$)', old_entry))
            if len(enabled_lines) > 1:
                raise ValueError('Ambiguous plugin enabled setting')
            if enabled_lines:
                line = enabled_lines[0]
                replacement = old_entry[:line.start()] + b'enabled = true\n' + old_entry[line.end():]
            else:
                if 'enabled' in parsed.get('plugins', {}).get(key, {}):
                    raise ValueError('Plugin enabled key must be a plain TOML assignment')
                replacement = old_entry.rstrip(b'\r\n') + b'\nenabled = true\n'

        updated = _patch_entry(original, key, replacement)
        stage = Path(tempfile.mkdtemp(prefix='.dev-package-', dir=recovery))
        swapped = False
        try:
            _snapshot(source, stage)
            fault('after_snapshot')
            _atomic(config_record, json.dumps({'key': key, 'entry': old_entry.decode() if old_entry is not None else None,
                'original_config_sha256': hashlib.sha256(original).hexdigest(),
                'installed_config_sha256': hashlib.sha256(updated).hexdigest()}).encode())
            _move_owned_tree(cache, backup)
            os.rename(stage, cache)
            swapped = True
            fault('after_cache_swap')
            if config_path.exists() and config_path.read_bytes() != original:
                raise ValueError('Native configuration changed before install')
            _atomic(config_path, updated)
            fault('after_config_swap')
            validate_native_cache(home, key, native_hooks_inventory(source, home), cache)
        except BaseException:
            if swapped:
                _move_owned_tree(cache, failed)
                _move_owned_tree(backup, cache)
                _package_tree.remove_owned_tree(failed)
            elif backup.exists() and not cache.exists():
                _move_owned_tree(backup, cache)
            # Restore our selected entry while preserving unrelated concurrent edits.
            if config_path.exists():
                current = config_path.read_bytes()
                if current == updated:
                    _atomic(config_path, original)
                else:
                    _, current_entry, _ = _selection_block(current, key)
                    _, published_entry, _ = _selection_block(updated, key)
                    if current_entry == published_entry:
                        _atomic(config_path, _patch_entry(current, key, old_entry))
            if config_record.exists():
                config_record.unlink()
            raise
        finally:
            if stage.exists():
                _package_tree.remove_owned_tree(stage)
        print('Codex dev runtime installed; original cache and plugin entry are backed up.')
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('install', 'restore', 'status'))
    parser.add_argument('--source')
    parser.add_argument('--cache-path', required=True)
    args = parser.parse_args()
    if args.mode == 'install' and args.source is None:
        parser.error('--source is required for install')
    home = os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')
    try:
        return run(args.mode, args.source, home, cache_path=args.cache_path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
