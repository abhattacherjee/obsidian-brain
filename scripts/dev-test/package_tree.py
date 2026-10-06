"""Copy the recursive runtime whitelist; never package local test/state files."""
import shutil
import sys
from pathlib import Path


def copy_runtime(source, cache):
    source, cache = Path(source).resolve(), Path(cache).resolve()
    if source == cache or source in cache.parents or cache in source.parents:
        raise ValueError('Runtime source and target cannot contain each other')
    ignored = shutil.ignore_patterns('__pycache__', '*.pyc', '.git', 'tests', 'state',
        '.pytest_cache', '.coverage', '.coverage.*', 'coverage', '.superpowers', '*.bak')
    for relative in ('hooks', 'skills', '.claude-plugin', '.codex-plugin', '.codex',
                     'scripts/vault_doctor_checks', 'scripts/dev-test'):
        origin = source / relative
        if origin.is_dir():
            for path in (origin, *origin.rglob('*')):
                if path.is_symlink():
                    raise ValueError('Runtime source contains a symlink')
            shutil.copytree(origin, cache / relative, dirs_exist_ok=True, ignore=ignored)
    (cache / 'scripts').mkdir(exist_ok=True)
    for name in ('vault_doctor.py', 'test-dev-skill.sh'):
        origin = source / 'scripts' / name
        if origin.is_file():
            if origin.is_symlink():
                raise ValueError('Runtime script cannot be a symlink')
            shutil.copy2(origin, cache / 'scripts' / name)


if __name__ == '__main__':
    copy_runtime(*sys.argv[1:])
