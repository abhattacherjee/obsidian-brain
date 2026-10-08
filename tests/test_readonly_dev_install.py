"""Read-only packages install into writable private caches and restore safely."""
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess

import pytest
from tests.test_dev_skill_install import (
    selected_host_context, _stage_repo, _stage_cache, _run_install,
)
from tests.native_install_test_helpers import native_folder, metadata_environment

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('readonly_package_tree', ROOT / 'scripts/dev-test/package_tree.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


@pytest.fixture(autouse=True)
def remove_private_readonly_fixture(tmp_path):
    yield
    # Source permissions are checked unchanged during the test. Remove only
    # this disposable fixture afterwards so pytest can clear its scratch root.
    source = tmp_path / 'repo'
    if source.exists():
        package.remove_owned_tree(source)


def inventory(root):
    return {str(p.relative_to(root)): (stat.S_IMODE(p.stat().st_mode),
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in (root, *root.rglob('*'))}


def readonly(root):
    for p in root.rglob('*'):
        p.chmod(0o555 if p.is_dir() or p.stat().st_mode & 0o111 else 0o444)
    root.chmod(0o555)


def test_readonly_source_install_and_readonly_partial_restore(tmp_path, selected_host_context):
    source = _stage_repo(tmp_path)
    home = _stage_cache(tmp_path)
    cache = home / native_folder() / 'plugins/cache/claude-code-skills/obsidian-brain/2.3.0'
    original = inventory(cache)
    readonly(source)
    source_before = inventory(source)
    result = _run_install(tmp_path, home)
    assert result.returncode == 0, result.stderr
    backup = cache.with_name(cache.name + '.bak')
    assert inventory(backup) == original
    assert inventory(source) == source_before
    for p in (cache, *cache.rglob('*')):
        assert p.stat().st_mode & stat.S_IWUSR
        assert stat.S_IMODE(p.stat().st_mode) & 0o077 == 0
    assert cache.joinpath('scripts/test-dev-skill.sh').stat().st_mode & stat.S_IXUSR
    # Reproduce the read-only partial cache left by the old installer.
    readonly(cache)
    result = subprocess.run(
        ['bash', str(source / 'scripts/test-dev-skill.sh'), 'restore',
         '--host', selected_host_context.host, '--source', str(source), '--cache-path', str(cache)],
        env=metadata_environment(home), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert inventory(cache) == original
    assert not backup.exists()
    assert inventory(source) == source_before


def test_midcopy_failure_keeps_cache_and_backup(tmp_path, monkeypatch):
    source = _stage_repo(tmp_path)
    cache = tmp_path / 'cache'; cache.mkdir()
    (cache / 'original').write_text('Original cache bytes.')
    backup = tmp_path / 'cache.bak'; backup.mkdir()
    (backup / 'original').write_text('Original backup bytes.')
    readonly(source)
    before_source, before_cache, before_backup = inventory(source), inventory(cache), inventory(backup)
    original_copy = package._copy_tree
    copied = []
    def fail_after_some_copy(origin, destination, ignored=None):
        if origin.is_file() and source in origin.parents:
            copied.append(origin)
            if len(copied) == 3:
                raise OSError('Synthetic midway copy failure')
        return original_copy(origin, destination, ignored)
    monkeypatch.setattr(package, '_copy_tree', fail_after_some_copy)
    with pytest.raises(OSError, match='midway'):
        package.copy_runtime(source, cache)
    assert len(copied) == 3
    assert inventory(cache) == before_cache
    assert inventory(backup) == before_backup
    assert inventory(source) == before_source
    assert not list(tmp_path.glob('.runtime-stage-*'))


def test_owned_cleanup_refuses_symlink_without_touching_target(tmp_path):
    target = tmp_path / 'protected'; target.mkdir()
    (target / 'sentinel').write_text('Keep.')
    selected = tmp_path / 'selected'; selected.mkdir()
    (selected / 'link').symlink_to(target, target_is_directory=True)
    before = inventory(target)
    with pytest.raises(ValueError, match='owned paths'):
        package.remove_owned_tree(selected)
    assert inventory(target) == before


def test_publish_failure_restores_original_tree(tmp_path, monkeypatch):
    source = _stage_repo(tmp_path)
    cache = tmp_path / 'cache'; cache.mkdir()
    (cache / 'original').write_text('Original exact bytes.')
    readonly(cache)
    before = inventory(cache)
    original_rename = os.rename
    def fail_publish(origin, destination):
        if Path(origin).name.startswith('.runtime-stage-') and Path(destination) == cache:
            raise OSError('Synthetic publish failure')
        return original_rename(origin, destination)
    monkeypatch.setattr(package.os, 'rename', fail_publish)
    with pytest.raises(OSError, match='publish'):
        package.copy_runtime(source, cache)
    assert inventory(cache) == before
    assert not list(tmp_path.glob('.runtime-stage-*'))
    assert not list(tmp_path.glob('.runtime-previous-*'))
