"""Only byte-identical production copies can share coverage file identities."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOTS = {'installed plugin with spaces', 'loaded installation with spaces'}


def verify_installed_hooks(root):
    assert root.name in FIXTURE_ROOTS, 'Unrecognized coverage fixture root.'
    original = ROOT / 'hooks'
    copied = root / 'hooks'
    assert not copied.is_symlink(), 'Installed hook directory is a symlink.'
    def files(directory):
        return {path.relative_to(directory): path for path in directory.rglob('*')
                if path.is_file() and '__pycache__' not in path.parts}
    source, target = files(original), files(copied)
    assert source.keys() == target.keys(), 'Installed hook file inventory changed.'
    for name in source:
        path = target[name]
        assert not any(parent.is_symlink() for parent in (path, *path.parents)), 'Installed hook source is a symlink.'
        assert hashlib.sha256(source[name].read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest(), 'Installed hook source changed: ' + str(name)
