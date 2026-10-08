"""Copy the recursive runtime whitelist; never package local test/state files."""
import shutil
import os
import stat
import tempfile
import sys
from pathlib import Path


def validate_runtime_source(source):
    """Reject missing required runtime dependencies before cache or backup writes."""
    source = Path(source).resolve()
    required = ('hooks/brain_cli.py', 'scripts/vault_doctor.py',
                'scripts/doctor_repair_state.py', 'scripts/test-dev-skill.sh',
                'scripts/vault_doctor_checks/__init__.py')
    if not all((source / name).is_file() and not (source / name).is_symlink()
               for name in required):
        raise ValueError('Runtime package is incomplete')


def _owned_path(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Runtime target cannot traverse symlinks')
    for item in (path, *path.rglob('*')):
        if item.is_symlink() or item.stat().st_uid != os.geteuid():
            raise ValueError('Runtime target must contain only owned paths without symlinks')


def remove_owned_tree(path):
    """Remove only a selected owned tree, including old read-only directories."""
    path = Path(path).absolute()
    if not path.exists():
        return
    _owned_path(path)
    for directory, _, _ in os.walk(path):
        os.chmod(directory, 0o700)
    shutil.rmtree(path)


def _copy_tree(source, destination, ignored=None):
    if source.is_symlink():
        raise ValueError('Runtime source contains a symlink')
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination, 0o700)
        children = list(source.iterdir())
        excluded = set(ignored(str(source), [p.name for p in children])) if ignored else set()
        for child in children:
            if child.name not in excluded:
                _copy_tree(child, destination / child.name, ignored)
    else:
        if not source.is_file():
            raise ValueError('Runtime source contains a non-file entry')
        shutil.copyfile(source, destination)
        # Keep executable semantics; source write restrictions belong to the
        # immutable package, not to the private install target.
        os.chmod(destination, 0o700 if source.stat().st_mode & 0o111 else 0o600)


def copy_runtime(source, cache):
    source, cache = Path(source).resolve(), Path(cache).absolute()
    if source == cache or source in cache.parents or cache in source.parents:
        raise ValueError('Runtime source and target cannot contain each other')
    validate_runtime_source(source)
    if cache.exists():
        _owned_path(cache)
    elif any(part.is_symlink() for part in (cache, *cache.parents)):
        raise ValueError('Runtime target cannot traverse symlinks')
    ignored = shutil.ignore_patterns('__pycache__', '*.pyc', '.git', 'tests', 'state',
        '.pytest_cache', '.coverage', '.coverage.*', 'coverage', '.superpowers', '*.bak')
    stage = Path(tempfile.mkdtemp(prefix='.runtime-stage-', dir=cache.parent))
    previous = None
    try:
        if cache.exists():
            _copy_tree(cache, stage)
        for relative in ('hooks', 'skills', '.claude-plugin', '.codex-plugin', '.codex',
                         'scripts/vault_doctor_checks', 'scripts/dev-test'):
            origin = source / relative
            if origin.is_dir():
                _copy_tree(origin, stage / relative, ignored)
        (stage / 'scripts').mkdir(exist_ok=True, mode=0o700)
        for name in ('vault_doctor.py', 'doctor_repair_state.py', 'test-dev-skill.sh'):
            origin = source / 'scripts' / name
            if origin.is_file():
                _copy_tree(origin, stage / 'scripts' / name)
        for directory, _, _ in os.walk(stage):
            os.chmod(directory, 0o700)
        if cache.exists():
            previous = Path(tempfile.mkdtemp(prefix='.runtime-previous-', dir=cache.parent))
            previous.rmdir()
            os.rename(cache, previous)
        try:
            os.rename(stage, cache)
        except BaseException:
            if previous is not None:
                os.rename(previous, cache)
                previous = None
            raise
        if previous is not None:
            remove_owned_tree(previous)
            previous = None
    finally:
        if stage.exists():
            remove_owned_tree(stage)



if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--remove-owned-tree':
        remove_owned_tree(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == '--validate-only':
        validate_runtime_source(sys.argv[2])
    else:
        copy_runtime(*sys.argv[1:])
