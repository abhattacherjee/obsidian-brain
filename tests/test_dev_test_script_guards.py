"""Tests for the two repo-root guards in scripts/test-dev-skill.sh (#287).

The script derives REPO_ROOT from its own location
(``$(dirname "$0")/..``) and uses it as the source tree for ``install``.
That is correct when the script is invoked from a real obsidian-brain
checkout, but silently wrong in two other cases the caller cannot be
trusted to rule out:

1. The directory the script happens to sit two levels above isn't an
   obsidian-brain checkout at all (missing ``hooks/obsidian_utils.py``, or a
   ``skills/`` that is missing or empty).
2. The script is itself running from *inside* the installed plugin tree
   (anything under ``~/.claude/plugins/``). Two shapes live there and both
   are wrong as an install SOURCE: the cache
   (``plugins/cache/*/obsidian-brain/<version>/``), where ``REPO_ROOT``
   resolves to the cache version directory and ``install`` copies the cache
   onto itself -- a byte-for-byte no-op that still prints a full success
   transcript (see D3 in docs/plans/287-dev-test-repo-root.md); and a
   marketplace clone (``plugins/marketplaces/<name>/``), which carries this
   script at its root because obsidian-brain's ``marketplace.json`` declares
   ``"source": "./"``, and which is a released tree that ``/plugin
   marketplace update`` rewrites behind your back.

Both guards must fire before any ``cp`` and before the ``.bak`` backup is
taken, so a bad invocation never leaves stray state behind.

This module also covers the two paths that only fail *silently*: a
``restore`` handed an incomplete ``.bak`` (which would destroy a healthy
cache and report success), and the ``.bak``-only cache dir that ``set -o
pipefail`` used to turn into a zero-output exit 1.

These tests drive the real script (copied into an isolated ``tmp_path``
tree) under a real ``bash`` subprocess. ``$HOME`` is always redirected to a
``tmp_path`` subdirectory -- the real ``~/.claude/plugins/cache/`` is never
read or written.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "test-dev-skill.sh"

_BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(_BASH is None, reason="bash not available")

#: Guard 2's distinguishing phrase. Kept as one constant because the positive
#: controls below assert its ABSENCE, and a hand-copied literal that drifts
#: from the script turns those assertions into tautologies.
GUARD2_MSG = "inside the installed plugin tree"
GUARD1_MSG = "does not look like an obsidian-brain checkout"


def _write_script(dest_dir: Path) -> Path:
    """Copy the real script into dest_dir/test-dev-skill.sh (mode 0755)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    script_path = dest_dir / "test-dev-skill.sh"
    script_path.write_bytes(SCRIPT_PATH.read_bytes())
    script_path.chmod(0o755)
    shutil.copytree(REPO_ROOT / "scripts/dev-test", dest_dir / "dev-test", dirs_exist_ok=True)
    for name in ("vault_doctor.py", "doctor_repair_state.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, dest_dir / name)
    shutil.copytree(REPO_ROOT / "scripts/vault_doctor_checks", dest_dir / "vault_doctor_checks", dirs_exist_ok=True)
    return script_path


#: Sentinel for `_run`'s `home` parameter: run the script with $HOME ABSENT
#: from the child's environment entirely, rather than present-and-empty (for
#: which callers pass `home=""` instead -- a distinct state; see
#: `test_home_unset_fail_closed_block_pins_its_custom_message` and
#: `test_home_empty_string_fail_closed_block_pins_its_custom_message`).
_HOME_UNSET = object()


def _run(script: Path, cmd: str, home: Path | str | object) -> subprocess.CompletedProcess:
    """Run the script with $HOME set to `home` -- or, if `home` is the
    `_HOME_UNSET` sentinel, with $HOME absent from the child's environment
    altogether. Every pre-existing caller passes a `Path`, which is
    unaffected: `str(home)` on a `Path` behaves exactly as before.
    """
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    assert context.host == "claude"
    env = {k: v for k, v in os.environ.items() if k not in {"HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}}
    if home is not _HOME_UNSET:
        env["HOME"] = str(home)
        if str(home):
            env["CLAUDE_CONFIG_DIR"] = str(Path(home) / ".claude")
    return subprocess.run(
        # _BASH, not the literal "bash": `requires_bash` skips on
        # shutil.which("bash"), so invoking anything else would let the skip
        # guard and the invocation disagree about which binary is under test.
        [_BASH, str(script), cmd, "--host", context.host, "--source", str(script.parent.parent)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )



@pytest.fixture
def selected_host_context(host, tmp_path, monkeypatch):
    from parity_test_helpers import selected_host_context as factory
    root = tmp_path / 'selected-runtime'
    root.mkdir()
    generator = factory.__wrapped__(host, root, monkeypatch)
    try:
        yield next(generator)
    finally:
        generator.close()


def _native_geometry(tmp_path):
    from tests.test_dev_skill_install import _stage_repo, _stage_cache
    root = tmp_path / 'native-installer'
    source = _stage_repo(root)
    home = _stage_cache(root)
    cache = home / '.codex/plugins/cache/claude-code-skills/obsidian-brain/2.3.0'
    return source, home, cache


def _tree_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob('*') if path.is_file() and not path.is_symlink()}


def _native_run(source, home, cache, mode, *, environment=None):
    from tests.native_install_test_helpers import metadata_environment
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    assert context.host == 'codex'
    env = metadata_environment(home) if environment is None else environment
    return subprocess.run([_BASH, str(SCRIPT_PATH), mode, '--host', context.host,
        '--source', str(source), '--cache-path', str(cache)],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)


def _codex_guard_case(name, tmp_path, monkeypatch, **parameters):
    """Exercise the corresponding native state contract, without CLI text emulation."""
    import json
    source, home, cache = _native_geometry(tmp_path)
    config = home / '.codex/config.toml'
    original = _tree_bytes(cache)
    original_config = config.read_bytes()
    from tests.native_install_test_helpers import backup_path
    backup = backup_path(home, cache)
    if name in {'test_restore_refuses_to_promote_an_incomplete_backup',
                'test_bak_only_cache_reports_instead_of_exiting_silently'}:
        backup = cache.with_name(cache.name + '.bak')
    mode = 'install'
    expect = 'refused'
    if name == 'test_sentinel_guard_rejects_non_checkout':
        shutil.rmtree(source / 'hooks')
        mode, expect = 'status', 'status'
    elif name in {'test_sentinel_guard_fires_before_any_mutation',
                  'test_sentinel_guard_rejects_checkout_missing_hooks_file'}:
        (source / 'hooks/obsidian_utils.py').unlink()
    elif name == 'test_sentinel_guard_rejects_checkout_without_a_non_empty_skills_dir':
        shutil.rmtree(source / 'skills')
        if parameters['shape'] == 'empty-skills-dir':
            (source / 'skills').mkdir()
    elif name == 'test_install_never_creates_a_literal_star_skill_dir':
        shutil.rmtree(source / 'skills')
        (source / 'skills').mkdir()
        (source / 'skills/README.md').write_text('not an installable skill')
    elif name in {'test_self_copy_guard_rejects_install_from_inside_cache',
                  'test_self_copy_guard_rejects_restore_from_inside_cache'}:
        source = cache
        mode = 'restore' if 'restore' in name else 'install'
    elif name == 'test_self_copy_guard_fires_with_symlinked_home':
        linked = tmp_path / 'linked-native-home'
        linked.symlink_to(home, target_is_directory=True)
        home = linked
        cache = linked / '.codex/plugins/cache/claude-code-skills/obsidian-brain/2.3.0'
    elif name == 'test_self_copy_guard_fires_with_symlinked_dot_claude':
        native = home / '.codex'
        target = home / 'native-dotfiles'
        native.rename(target)
        native.symlink_to(target, target_is_directory=True)
    elif name == 'test_self_copy_guard_is_skipped_when_there_is_no_plugins_dir':
        shutil.rmtree(home / '.codex/plugins')
        original = {}
    elif name == 'test_status_still_works_from_inside_cache':
        source, mode, expect = cache, 'status', 'status'
    elif name in {'test_home_fail_closed_block_pins_its_custom_message',
                  'test_home_unset_fail_closed_block_pins_its_custom_message',
                  'test_home_empty_string_fail_closed_block_pins_its_custom_message'}:
        from tests.native_install_test_helpers import metadata_environment
        environment = metadata_environment(home)
        environment['CODEX_HOME'] = str(tmp_path / 'missing-selected-native-home')
        if 'unset' in name:
            environment.pop('HOME')
        elif 'empty_string' in name:
            environment['HOME'] = ''
        proc = _native_run(source, home, cache, mode, environment=environment)
        assert proc.returncode != 0 and proc.stderr.strip()
        assert _tree_bytes(cache) == original and config.read_bytes() == original_config
        assert not backup.exists()
        return
    elif name == 'test_self_copy_guard_rejects_install_from_a_marketplace_clone':
        clone = home / '.codex/plugins/marketplaces/obsidian-brain'
        shutil.copytree(source, clone)
        source = clone
    elif name == 'test_install_proceeds_from_a_checkout_under_home':
        # A worktree below HOME is valid; selected CODEX_HOME remains private.
        destination = home / ('dev' if parameters['location'] == 'home-dev' else 'other-development') / 'obsidian-brain'
        shutil.copytree(source, destination)
        source, expect = destination, 'installed'
    elif name == 'test_restore_puts_the_original_content_back':
        installed = _native_run(source, home, cache, 'install')
        assert installed.returncode == 0, installed.stderr
        mode, expect = 'restore', 'restored'
    elif name == 'test_restore_with_no_backup_exits_4_not_0':
        mode, expect = 'restore', 'nothing'
    elif name == 'test_restore_refuses_to_promote_an_incomplete_backup':
        # A fragment without the protected config recovery record is never adopted.
        (backup / 'hooks').mkdir(parents=True)
        if parameters['shape'] == 'missing-hooks-file':
            (backup / 'skills/recall').mkdir(parents=True)
            (backup / 'skills/recall/SKILL.md').write_text('fragment')
        else:
            (backup / 'hooks/obsidian_utils.py').write_text('fragment')
            (backup / 'skills').mkdir()
        mode = 'restore'
    elif name == 'test_bak_only_cache_reports_instead_of_exiting_silently':
        cache.rename(backup)
        mode = parameters['cmd']
        proc = _native_run(source, home, cache, mode)
        if mode == 'status':
            assert proc.returncode == 0 and json.loads(proc.stdout)['dev_active'] is True
        else:
            assert proc.returncode != 0 and (proc.stderr.strip() or proc.stdout.strip())
        assert not cache.exists() and _tree_bytes(backup) == original
        assert config.read_bytes() == original_config
        return
    elif name in {'test_install_says_so_when_security_tests_are_skipped',
                  'test_install_fails_loudly_when_security_tests_fail'}:
        # Codex packaging validates its snapshot and native metadata. It does not
        # dispatch the Claude shell test footer or execute source-owned test code.
        marker = home / 'source-test-executed'
        security = source / 'scripts/security-test.sh'
        security.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
        security.chmod(0o755)
        proc = _native_run(source, home, cache, mode)
        assert proc.returncode == 0, proc.stderr
        assert not marker.exists()
        assert (cache / 'hooks/brain_cli.py').is_file()
        assert _tree_bytes(backup) == original
        return
    elif name in {'test_an_interrupted_backup_never_produces_a_bak',
                  'test_a_partway_install_exits_3_not_1'}:
        # Production fault hooks interrupt the corresponding snapshot/swap phase.
        from tests.test_host_package_install import installer
        def metadata(source, native_home):
            hooks = installer._selected_hooks(cache)[0]
            return {'data':[{'errors':[], 'hooks':[{'pluginId':'obsidian-brain@claude-code-skills', 'sourcePath':str(hooks)}]}]}
        monkeypatch.setattr(installer, 'native_hooks_inventory', metadata)
        phase = 'after_snapshot' if 'interrupted' in name else 'after_cache_swap'
        def fail(point):
            if point == phase:
                raise OSError('synthetic interruption')
        with pytest.raises(OSError, match='synthetic interruption'):
            installer.run('install', source, home / '.codex', cache_path=cache, fault=fail)
        assert _tree_bytes(cache) == original and config.read_bytes() == original_config
        assert not backup.exists()
        assert not list(cache.parent.glob('.dev-package-*'))
        return
    elif name == 'test_unreadable_cache_base_reports_instead_of_exiting_silently':
        base = cache.parent
        base.chmod(0)
        try:
            proc = _native_run(source, home, cache, parameters['cmd'])
            assert proc.returncode != 0 and proc.stderr.strip()
        finally:
            base.chmod(0o755)
        assert _tree_bytes(cache) == original and config.read_bytes() == original_config
        return
    elif name in {'test_a_leftover_partial_backup_is_not_selected_as_the_version',
                  'test_status_discloses_orphaned_partial_backups',
                  'test_status_says_nothing_about_partials_when_there_are_none',
                  'test_status_does_not_claim_the_diff_failed_when_a_dev_version_is_active'}:
        stale = cache.with_name(cache.name + parameters.get('suffix', '.bak.partial.123'))
        if 'none' not in name:
            stale.mkdir()
            (stale / 'sentinel').write_text('unrelated partial bytes')
        if 'dev_version' in name:
            installed = _native_run(source, home, cache, 'install')
            assert installed.returncode == 0, installed.stderr
        mode, expect = 'status', 'status'
    else:
        raise AssertionError('Native installer case not specified: ' + name)
    proc = _native_run(source, home, cache, mode)
    if expect == 'status':
        assert proc.returncode == 0, proc.stderr
        status = json.loads(proc.stdout)
        assert status['host'] == 'codex' and status['version'] == cache.name
        assert status['dev_active'] == backup.exists()
        if 'dev_version' not in name:
            assert _tree_bytes(cache) == original
        if 'stale' in locals() and stale.exists():
            assert (stale / 'sentinel').read_text() == 'unrelated partial bytes'
    elif expect == 'installed':
        assert proc.returncode == 0, proc.stderr
        assert _tree_bytes(backup) == original
        assert (cache / 'hooks/brain_cli.py').is_file()
    else:
        if expect == 'restored':
            assert proc.returncode == 0, proc.stderr
            assert not backup.exists()
        elif expect == 'nothing':
            assert proc.returncode == 4 and 'nothing to restore' in proc.stderr
        else:
            assert proc.returncode != 0 and proc.stderr.strip() + proc.stdout.strip()
        assert _tree_bytes(cache) == original
        assert config.read_bytes() == original_config
        if name != 'test_restore_refuses_to_promote_an_incomplete_backup':
            assert not backup.exists()
    assert not (cache / 'skills/*').exists()

#: Where a positive control stages the checkout, relative to ``$HOME``. Each
#: entry pins one rung of the "just widen the prefix a bit" ladder:
#:
#:   * ``dev/obsidian-brain``        — catches widening to ``$HOME/``
#:   * ``.claude/dev/obsidian-brain`` — catches widening to ``$HOME/.claude/``
#:
#: Measured before the second rung existed: widening ``PLUGIN_ROOT_PREFIX`` to
#: ``$HOME/`` failed 8 tests, but widening it to ``$HOME/.claude/`` passed all
#: 19 in this module. Working trees under ``~/.claude/`` are a real habit in
#: this ecosystem (this repo's own CLAUDE.md dispatches
#: ``~/.claude/skills/<name>/scripts/…``), so that middle rung would brick a
#: live layout while the module stayed green.
CHECKOUT_LOCATIONS = {
    "home-dev": ("dev", "obsidian-brain"),
    "home-dotclaude-dev": (".claude", "dev", "obsidian-brain"),
}


def _stage_production_geometry(
    tmp_path: Path,
    version: str = "1.2.3",
    repo_rel: tuple = CHECKOUT_LOCATIONS["home-dev"],
):
    """A checkout UNDER $HOME, plus a plugin cache — the real-world layout.

    The pre-existing fixtures put the checkout and ``$HOME`` in sibling
    directories, a geometry that does not occur in production: real checkouts
    live under the user's home (this repo is at
    ``/Users/<me>/dev/claude_workspace/obsidian-brain``). That matters because
    every other guard-2 fixture asserts the guard FIRES; without a positive
    control staged the way users actually have it, widening the prefix from
    ``$HOME/.claude/plugins/`` to ``$HOME/`` — which would refuse
    ``/dev-test install`` for essentially everyone — passes the entire suite.

    ``repo_rel`` selects the rung of that ladder; see ``CHECKOUT_LOCATIONS``.

    Returns ``(home, repo, script, cache_dir)``.
    """
    home = tmp_path / "home"
    repo = home.joinpath(*repo_rel)
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "hooks/brain_cli.py", repo / "hooks/brain_cli.py")
    (repo / "hooks" / "obsidian_utils.py").write_text("# dev hook\n")
    (repo / "hooks" / "hooks.json").write_text("{}\n")
    (repo / "skills" / "dev-test").mkdir(parents=True)
    (repo / "skills" / "dev-test" / "SKILL.md").write_text("# dev skill\n")

    cache_dir = (
        home / ".claude" / "plugins" / "cache" / "some-marketplace"
        / "obsidian-brain" / version
    )
    (cache_dir / "hooks").mkdir(parents=True)
    (cache_dir / "hooks" / "obsidian_utils.py").write_text("# released hook\n")
    (cache_dir / "skills" / "dev-test").mkdir(parents=True)
    (cache_dir / "skills" / "dev-test" / "SKILL.md").write_text("# released skill\n")
    return home, repo, script, cache_dir


@requires_bash
def test_sentinel_guard_rejects_non_checkout(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """Guard 1: a directory two levels above the script that lacks
    hooks/obsidian_utils.py and skills/ is not an obsidian-brain checkout --
    the script must refuse rather than trust its own location blindly.

    Deliberately placed OUTSIDE any ~/.claude/plugins/cache/ path, and run
    with "status" (not install/restore), so this failure can only be
    attributed to guard 1 -- guard 2 does not even apply to "status".
    """
    if host == 'codex':
        _codex_guard_case('test_sentinel_guard_rejects_non_checkout', tmp_path, monkeypatch)
        return
    repo = tmp_path / "some-other-project"
    script = _write_script(repo / "scripts")
    home = tmp_path / "home"  # empty; never touched by this test

    proc = _run(script, "status", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert GUARD1_MSG in proc.stderr


@requires_bash
def test_sentinel_guard_fires_before_any_mutation(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """Guard 1 must fire even for "install" -- before the cache lookup, the
    .bak backup, or any cp -- so a non-checkout invocation never mutates
    anything under $HOME.
    """
    if host == 'codex':
        _codex_guard_case('test_sentinel_guard_fires_before_any_mutation', tmp_path, monkeypatch)
        return
    repo = tmp_path / "some-other-project"
    script = _write_script(repo / "scripts")
    home = tmp_path / "home"
    home.mkdir()

    proc = _run(script, "install", home)

    assert proc.returncode != 0
    assert GUARD1_MSG in proc.stderr
    # Nothing was created under $HOME -- no cache probing, no .bak.
    assert not any(home.rglob("*"))


@requires_bash
@pytest.mark.parametrize("shape", ["absent-skills-dir", "empty-skills-dir"])
def test_sentinel_guard_rejects_checkout_without_a_non_empty_skills_dir(
    tmp_path: Path, shape: str
, host, selected_host_context, monkeypatch) -> None:
    """Guard 1's ``skills/`` half in isolation, in BOTH shapes it must reject.

    The only other guard-1 fixture (above) builds a tree with NEITHER
    sentinel, so each half of the ``||`` is covered by the other -- deleting
    either half alone still passes the suite. This tree carries the
    ``hooks/obsidian_utils.py`` sentinel but no usable ``skills/``, so a
    failure here can only be attributed to the ``skills/`` half.

    That half is genuinely load-bearing in production: with no skills to
    match, the install loop's ``"$REPO_ROOT/skills/"*/`` glob goes unmatched
    and (with nullglob unset, the bash default) stays literal, creating a
    junk ``skills/*`` directory under the cache instead of failing loudly up
    front.

    ``empty-skills-dir`` is the row that makes that docstring true rather
    than merely plausible. The guard used to test ``[[ ! -d
    "$REPO_ROOT/skills" ]]``, which an EMPTY ``skills/`` satisfies -- so the
    failure this test cites as the justification was reachable through the
    guard that was supposed to close it, and only the ``absent`` row was
    staged. Reproduced verbatim against that version: ``  skills/*/ ->
    cache`` narrated for a copy that never happened, a directory literally
    named ``*`` created in the plugin cache beside ``recall``, a ``.bak``
    published, EXIT=0. The predicate is now ``compgen -G``, matching
    ``restore``'s completeness check.
    """
    if host == 'codex':
        _codex_guard_case('test_sentinel_guard_rejects_checkout_without_a_non_empty_skills_dir', tmp_path, monkeypatch, shape=shape)
        return
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    if shape == "empty-skills-dir":
        (repo / "skills").mkdir()  # present but EMPTY
    # else: deliberately no skills/ dir at all
    home = tmp_path / "home"

    proc = _run(script, "status", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert GUARD1_MSG in proc.stderr


@requires_bash
def test_install_never_creates_a_literal_star_skill_dir(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """The residual of guard 1's ``skills/`` half, closed in the loop itself.

    Guard 1 asks whether ``skills/`` holds ANY entry (``compgen -G
    "…/skills/*"``, the predicate ``restore`` uses). The install loop globs
    ``skills/*/`` -- directories only. A ``skills/`` holding nothing but
    FILES (a README, a half-finished rsync) therefore passes the guard and
    still leaves the loop's glob unmatched, and an unmatched glob is literal
    with nullglob off: reproduced against the guard-1 fix alone, the run
    printed ``  skills/*/ -> cache`` and created a directory named ``*``
    inside the plugin cache at exit 0.

    So the loop skips anything that is not a real directory. Asserts on the
    cache contents and the transcript, not just the exit code -- the
    pre-fix run exited 0 too.
    """
    if host == 'codex':
        _codex_guard_case('test_install_never_creates_a_literal_star_skill_dir', tmp_path, monkeypatch)
        return
    home, repo, script, cache_dir = _stage_production_geometry(tmp_path)
    shutil.rmtree(repo / "skills")
    (repo / "skills").mkdir()
    (repo / "skills" / "README.md").write_text("# not a skill\n")

    proc = _run(script, "install", home)

    assert proc.returncode == 0, f"install refused: {proc.stderr}"
    assert GUARD1_MSG not in proc.stderr, "a files-only skills/ is not empty"
    assert "*" not in [p.name for p in (cache_dir / "skills").iterdir()], (
        f"a literal '*' directory landed in the cache: "
        f"{sorted(p.name for p in (cache_dir / 'skills').iterdir())}"
    )
    assert "skills/*/" not in proc.stdout, (
        f"the transcript narrated a copy that never happened: {proc.stdout!r}"
    )


@requires_bash
def test_sentinel_guard_rejects_checkout_missing_hooks_file(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """Guard 1's ``hooks/obsidian_utils.py`` half in isolation.

    Mirror of the test above: this tree carries ``skills/`` but deliberately
    omits ``hooks/obsidian_utils.py``, so a failure here can only be
    attributed to the hooks-sentinel half.
    """
    if host == 'codex':
        _codex_guard_case('test_sentinel_guard_rejects_checkout_missing_hooks_file', tmp_path, monkeypatch)
        return
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "skills" / "some-skill").mkdir(parents=True)
    # deliberately no hooks/obsidian_utils.py
    home = tmp_path / "home"

    proc = _run(script, "status", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert GUARD1_MSG in proc.stderr


@requires_bash
def test_self_copy_guard_rejects_install_from_inside_cache(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """Guard 2 (D3): a script whose own REPO_ROOT resolves to a path under
    ~/.claude/plugins/cache/ must refuse "install" -- copying the cache onto
    itself is a no-op that would otherwise print a full success transcript.

    The staged tree carries valid sentinel files (hooks/obsidian_utils.py,
    skills/) so guard 1 passes and this failure can only be attributed to
    guard 2 -- this is what keeps the fixture from being shadowed by guard 1
    (the guard-ordering trap).
    """
    if host == 'codex':
        _codex_guard_case('test_self_copy_guard_rejects_install_from_inside_cache', tmp_path, monkeypatch)
        return
    home = tmp_path / "home"
    cache_version_dir = (
        home / ".claude" / "plugins" / "cache" / "some-marketplace" / "obsidian-brain" / "9.9.9"
    )
    script = _write_script(cache_version_dir / "scripts")
    (cache_version_dir / "hooks").mkdir(parents=True)
    (cache_version_dir / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    (cache_version_dir / "skills" / "some-skill").mkdir(parents=True)

    proc = _run(script, "install", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert GUARD2_MSG in proc.stderr
    # No backup was created -- the guard fired before the .bak step.
    assert not (cache_version_dir.parent / "9.9.9.bak").exists()


@requires_bash
def test_self_copy_guard_rejects_restore_from_inside_cache(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """Guard 2 also applies to "restore" -- restoring a .bak while REPO_ROOT
    is the cache itself is equally nonsensical (there is no local checkout
    to have diverged from).
    """
    if host == 'codex':
        _codex_guard_case('test_self_copy_guard_rejects_restore_from_inside_cache', tmp_path, monkeypatch)
        return
    home = tmp_path / "home"
    cache_version_dir = (
        home / ".claude" / "plugins" / "cache" / "some-marketplace" / "obsidian-brain" / "9.9.9"
    )
    script = _write_script(cache_version_dir / "scripts")
    (cache_version_dir / "hooks").mkdir(parents=True)
    (cache_version_dir / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    (cache_version_dir / "skills" / "some-skill").mkdir(parents=True)

    proc = _run(script, "restore", home)

    assert proc.returncode != 0
    assert GUARD2_MSG in proc.stderr


@requires_bash
def test_self_copy_guard_fires_with_symlinked_home(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """Guard 2 must canonicalize $HOME the same way REPO_ROOT is canonicalized
    (`pwd -P`) before comparing prefixes. REPO_ROOT is resolved through
    symlinks by `cd ... && pwd -P`; if $HOME is used raw, a symlinked $HOME
    makes the string-prefix comparison silently fail to fire on exactly the
    machine shape where it matters -- macOS's /var -> /private/var is a live
    instance of this. pytest's tmp_path is already canonicalized, which is
    why the other guard-2 tests above pass regardless of this bug and cannot
    catch it; this test builds a genuinely symlinked $HOME to close that gap:
    a real directory, a symlink pointing at it, $HOME set to the symlink, and
    the fake cache tree laid out under the real directory (reached via the
    symlink path, matching how the script is actually invoked).
    """
    if host == 'codex':
        _codex_guard_case('test_self_copy_guard_fires_with_symlinked_home', tmp_path, monkeypatch)
        return
    real_home = tmp_path / "real_home"
    real_home.mkdir()
    home_link = tmp_path / "home_link"
    home_link.symlink_to(real_home, target_is_directory=True)

    cache_version_dir = (
        home_link / ".claude" / "plugins" / "cache" / "some-marketplace" / "obsidian-brain" / "9.9.9"
    )
    script = _write_script(cache_version_dir / "scripts")
    (cache_version_dir / "hooks").mkdir(parents=True)
    (cache_version_dir / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    (cache_version_dir / "skills" / "some-skill").mkdir(parents=True)

    proc = _run(script, "install", home_link)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert GUARD2_MSG in proc.stderr
    # No backup was created under the REAL (non-symlink) location either --
    # the guard fired before the .bak step, regardless of which path string
    # you check it through.
    real_backup = (
        real_home / ".claude" / "plugins" / "cache" / "some-marketplace"
        / "obsidian-brain" / "9.9.9.bak"
    )
    assert not real_backup.exists()


@requires_bash
def test_self_copy_guard_fires_with_symlinked_dot_claude(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """Guard 2 must canonicalize the plugin root DIRECTORY, not just ``$HOME``.

    The test above symlinks ``$HOME`` itself. That closes only the first
    component: the prefix was then built as ``$(cd "$HOME" && pwd -P)`` plus the
    LITERAL segments ``/.claude/plugins/``, so a symlink anywhere BELOW $HOME
    left the two sides of the comparison canonicalized to different degrees and
    the guard never fired. ``~/.claude`` pointing into a dotfiles tree is a
    standard stow/chezmoi layout, not an exotic one.

    Reproduced against the literal-append form, with this exact geometry:
    ``Backing up: … -> ….bak`` / ``Installing dev versions…`` / ``Dev version
    installed to cache (v3.4.0).`` at EXIT=0, after which the cache's
    ``hooks/obsidian_utils.py`` held ``# RELEASED hook`` -- a marketplace clone,
    the released tree ``/plugin marketplace update`` rewrites behind your back,
    installed as "the dev version" with a full success banner. The identical
    fixture with ``.claude`` as a real directory refused at exit 1, which
    isolates the symlink as the sole cause.

    A real plugin cache is staged deliberately: without one the pre-fix run
    exits 1 on "No installed obsidian-brain plugin cache found" and a bare
    ``returncode != 0`` assertion would pass for the wrong reason.
    """
    if host == 'codex':
        _codex_guard_case('test_self_copy_guard_fires_with_symlinked_dot_claude', tmp_path, monkeypatch)
        return
    home = tmp_path / "home"
    home.mkdir()
    real_claude = tmp_path / "dotfiles" / "claude"
    real_claude.mkdir(parents=True)
    (home / ".claude").symlink_to(real_claude, target_is_directory=True)

    clone = home / ".claude" / "plugins" / "marketplaces" / "obsidian-brain-repo"
    script = _write_script(clone / "scripts")
    (clone / "hooks").mkdir(parents=True)
    (clone / "hooks" / "obsidian_utils.py").write_text("# released hook\n")
    (clone / "skills" / "recall").mkdir(parents=True)
    (clone / "skills" / "recall" / "SKILL.md").write_text("# released skill\n")

    cache_dir = (
        home / ".claude" / "plugins" / "cache" / "some-marketplace"
        / "obsidian-brain" / "3.4.0"
    )
    (cache_dir / "hooks").mkdir(parents=True)
    (cache_dir / "hooks" / "obsidian_utils.py").write_text("# cached hook\n")
    (cache_dir / "skills" / "recall").mkdir(parents=True)
    (cache_dir / "skills" / "recall" / "SKILL.md").write_text("# cached skill\n")

    proc = _run(script, "install", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert GUARD2_MSG in proc.stderr
    # The guard fired BEFORE the backup, and the cache still holds its own
    # bytes -- not the clone's.
    assert not list(cache_dir.parent.glob("*.bak"))
    assert "# cached hook" in (
        cache_dir / "hooks" / "obsidian_utils.py"
    ).read_text(), "the released clone was installed over the cache"


@requires_bash
def test_self_copy_guard_is_skipped_when_there_is_no_plugins_dir(
    tmp_path: Path,
    host, selected_host_context, monkeypatch) -> None:
    """No ``~/.claude/plugins`` means the guard has nothing to catch.

    ``cd``-ing into the plugin root to canonicalize it has to cope with that
    directory not existing -- under ``set -euo pipefail`` a failing ``cd``
    aborts the run. Skipping the comparison there is not fail-open: REPO_ROOT
    is an existing directory (the script ``cd``s into it to compute it), and no
    existing directory can live under a path that does not exist.

    Asserts the guard message is ABSENT rather than just ``returncode != 0``:
    this fixture has no plugin cache either, so the run must fail on THAT --
    ``No installed obsidian-brain plugin cache found`` -- and not on a guard
    that could not be evaluated.
    """
    if host == 'codex':
        _codex_guard_case('test_self_copy_guard_is_skipped_when_there_is_no_plugins_dir', tmp_path, monkeypatch)
        return
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "obsidian_utils.py").write_text("# dev hook\n")
    (repo / "skills" / "dev-test").mkdir(parents=True)
    (repo / "skills" / "dev-test" / "SKILL.md").write_text("# dev skill\n")
    assert not (home / ".claude").exists()

    proc = _run(script, "install", home)

    assert GUARD2_MSG not in proc.stderr, (
        f"the guard cannot fire with no plugin tree to be inside: {proc.stderr!r}"
    )
    assert "No installed obsidian-brain plugin cache found" in proc.stdout, (
        f"expected the cache-discovery failure, got stdout={proc.stdout!r} "
        f"stderr={proc.stderr!r}"
    )


@requires_bash
def test_status_still_works_from_inside_cache(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """Judgment call (see task-2-report.md): guard 2 is scoped to the
    mutating subcommands only. "status" is read-only and must keep
    reporting even when the script happens to be running from inside the
    cache -- e.g. a machine with no local checkout that only has the cache.
    """
    if host == 'codex':
        _codex_guard_case('test_status_still_works_from_inside_cache', tmp_path, monkeypatch)
        return
    home = tmp_path / "home"
    cache_version_dir = (
        home / ".claude" / "plugins" / "cache" / "some-marketplace" / "obsidian-brain" / "9.9.9"
    )
    script = _write_script(cache_version_dir / "scripts")
    (cache_version_dir / "hooks").mkdir(parents=True)
    (cache_version_dir / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    (cache_version_dir / "skills" / "some-skill").mkdir(parents=True)

    proc = _run(script, "status", home)

    assert proc.returncode == 0, f"status should succeed from inside cache: {proc.stderr}"
    assert "Plugin: obsidian-brain" in proc.stdout


@requires_bash
def test_home_fail_closed_block_pins_its_custom_message(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """The `$HOME` fail-closed block (the `[[ -z "${HOME:-}" ]] || [[ ! -d
    "$HOME" ]]` guard on the mutating subcommands; M2 in the #287 final
    review) is shadowed in
    EXIT STATUS by `set -euo pipefail` -- delete the whole block and the
    script still exits non-zero, because the very next line (`cd "$HOME" &&
    pwd -P`) already fails under `set -u` (unset $HOME) or `set -e` ($HOME
    names a missing directory). What the block adds is message quality, not
    exit-status behaviour, so this test pins the MESSAGE rather than just the
    exit code -- that is the one thing that actually distinguishes "block
    present" from "block deleted" and makes it a real (rather than vacuous)
    guard.

    $HOME is deliberately set to a path that does not exist, exercising only
    the guard's SECOND half (`[[ ! -d "$HOME" ]]`) -- the first half
    (`[[ -z "${HOME:-}" ]]`, $HOME unset or empty) is never reached here,
    because `_run`'s default always sets $HOME to a concrete string. See
    `test_home_unset_fail_closed_block_pins_its_custom_message` and
    `test_home_empty_string_fail_closed_block_pins_its_custom_message`
    immediately below for that half, added separately so mutating either
    half of the `||` in isolation fails a distinctly-named test rather than
    being covered by the other.
    """
    if host == 'codex':
        _codex_guard_case('test_home_fail_closed_block_pins_its_custom_message', tmp_path, monkeypatch)
        return
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    (repo / "skills" / "some-skill").mkdir(parents=True)

    missing_home = tmp_path / "does-not-exist"

    proc = _run(script, "install", missing_home)

    assert proc.returncode != 0
    assert "cannot verify this script isn't" in proc.stderr


@requires_bash
def test_home_unset_fail_closed_block_pins_its_custom_message(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """The `$HOME` fail-closed block's FIRST half (`[[ -z "${HOME:-}" ]]`),
    exercised via $HOME genuinely ABSENT from the child's environment.

    Without this test, that first half is never reached by anything in this
    module: it can be deleted from the script and the whole suite still
    passes, because `test_home_fail_closed_block_pins_its_custom_message`
    above only ever reaches the second half (`_run` always sets $HOME to
    something).

    Confirmed empirically (bash 3.2.57 on macOS, this repo's CI/dev target,
    via a standalone probe: `bash -c 'if [[ -z "${HOME:-}" ]]; then echo
    EMPTY-OR-UNSET; else echo "SET=[$HOME]"; fi'` run with HOME absent from
    the subprocess env) that a plain non-interactive, non-login `bash
    script.sh` invocation -- exactly the shape `_run` uses: `_BASH` + argv,
    no `-l`/`-i`, no login-shell or interactive-shell wrapper -- does NOT
    get $HOME repopulated from the passwd database. The probe printed
    `EMPTY-OR-UNSET`, i.e. the child genuinely saw $HOME as unset rather
    than silently backfilled by bash itself. That empirical check is what
    makes this test meaningful rather than accidentally-passing for the
    wrong reason.
    """
    if host == 'codex':
        _codex_guard_case('test_home_unset_fail_closed_block_pins_its_custom_message', tmp_path, monkeypatch)
        return
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    (repo / "skills" / "some-skill").mkdir(parents=True)

    proc = _run(script, "install", _HOME_UNSET)

    assert proc.returncode != 0
    assert "cannot verify this script isn't" in proc.stderr


@requires_bash
def test_home_empty_string_fail_closed_block_pins_its_custom_message(
    tmp_path: Path,
    host, selected_host_context, monkeypatch) -> None:
    """The same first half (`[[ -z "${HOME:-}" ]]`), exercised via the OTHER
    path that makes it true: $HOME present in the child's environment but
    set to the empty string, rather than absent entirely.

    Bash's `-z "${HOME:-}"` test treats "unset" and "empty string"
    identically, but they are different states of the actual environment,
    and a narrower fix that only checked for absence (e.g. testing
    `${HOME+set}` instead of `-z`) would pass the unset test above while
    leaving this one -- and real machines with `HOME=` in their environment
    -- broken. Distinct from `test_home_unset_fail_closed_block_pins_its_custom_message`:
    that test deletes the key from the environment mapping; this one sets
    it to `""`.
    """
    if host == 'codex':
        _codex_guard_case('test_home_empty_string_fail_closed_block_pins_its_custom_message', tmp_path, monkeypatch)
        return
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    (repo / "skills" / "some-skill").mkdir(parents=True)

    proc = _run(script, "install", "")

    assert proc.returncode != 0
    assert "cannot verify this script isn't" in proc.stderr


@requires_bash
def test_self_copy_guard_rejects_install_from_a_marketplace_clone(
    tmp_path: Path,
    host, selected_host_context, monkeypatch) -> None:
    """Guard 2's OTHER shape: ``~/.claude/plugins/marketplaces/<name>/``.

    obsidian-brain's ``.claude-plugin/marketplace.json`` declares
    ``"source": "./"``, so the marketplace repo IS the plugin repo and a
    marketplace clone carries ``scripts/test-dev-skill.sh`` at its root —
    verified on this machine, where 5 of the 6 entries under
    ``~/.claude/plugins/marketplaces/`` are git repositories. A user who cds
    into that clone to inspect their install and runs ``/dev-test install``
    would otherwise install a RELEASED tree (one that ``/plugin marketplace
    update`` rewrites behind their back) as "the dev version", at exit 0.
    Guard 1 cannot catch it: the clone is a perfectly valid checkout.
    """
    if host == 'codex':
        _codex_guard_case('test_self_copy_guard_rejects_install_from_a_marketplace_clone', tmp_path, monkeypatch)
        return
    home = tmp_path / "home"
    clone = home / ".claude" / "plugins" / "marketplaces" / "obsidian-brain-repo"
    script = _write_script(clone / "scripts")
    (clone / "hooks").mkdir(parents=True)
    (clone / "hooks" / "obsidian_utils.py").write_text("# released hook\n")
    (clone / "skills" / "some-skill").mkdir(parents=True)

    proc = _run(script, "install", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert GUARD2_MSG in proc.stderr
    # Shape-DISCRIMINATING, deliberately. The guard's text names both shapes
    # (cache and marketplace clone) on every firing, so `"marketplace clone" in
    # stderr` is equally true for the cache fixture above — the fixture path,
    # not the assertion, was carrying this test. What actually distinguishes
    # this shape is the offending path the guard echoes back, so assert on
    # that: the clone's own location, under plugins/marketplaces/ rather than
    # plugins/cache/.
    assert os.path.realpath(clone) in proc.stderr
    assert "/plugins/marketplaces/" in proc.stderr
    assert "/plugins/cache/" not in proc.stderr


@requires_bash
@pytest.mark.parametrize("location", sorted(CHECKOUT_LOCATIONS), ids=sorted(CHECKOUT_LOCATIONS))
def test_install_proceeds_from_a_checkout_under_home(
    tmp_path: Path, location: str
, host, selected_host_context, monkeypatch) -> None:
    """Guard 2's positive control, in the geometries users actually have.

    Every other guard-2 fixture asserts the guard FIRES, and the fixtures
    where it must not fire (tests/test_dev_skill_install.py) put the repo and
    ``$HOME`` in sibling directories — which never happens in production.
    Consequence, measured: widening ``PLUGIN_ROOT_PREFIX`` to ``$HOME/``
    passed all 2367 tests, while bricking ``/dev-test install`` for every
    developer whose checkout lives under their home directory.

    Parametrized over ``CHECKOUT_LOCATIONS`` because the widening ladder has
    more than one rung: the ``dev/`` row catches ``$HOME/``, the
    ``.claude/dev/`` row catches ``$HOME/.claude/`` (which the ``dev/`` row
    alone does not — measured 19 passed, 0 failed under that mutation).

    So: a checkout under ``$HOME`` but outside ``.claude/plugins/`` must
    install cleanly. Asserting the guard messages are ABSENT (not just
    ``returncode == 0``) is what makes this discriminating — a future guard
    that fires for a different reason cannot launder itself through an exit
    code that some other branch also produces.
    """
    if host == 'codex':
        _codex_guard_case('test_install_proceeds_from_a_checkout_under_home', tmp_path, monkeypatch, location=location)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(
        tmp_path, repo_rel=CHECKOUT_LOCATIONS[location]
    )

    proc = _run(script, "install", home)

    assert proc.returncode == 0, f"install refused: {proc.stderr}"
    assert GUARD2_MSG not in proc.stderr
    assert GUARD1_MSG not in proc.stderr
    # The dev tree actually landed in the cache — not merely "didn't refuse".
    assert "# dev hook" in (cache_dir / "hooks" / "obsidian_utils.py").read_text()
    assert "# dev skill" in (
        cache_dir / "skills" / "dev-test" / "SKILL.md"
    ).read_text()


@requires_bash
def test_restore_puts_the_original_content_back(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """The successful ``restore`` path, executed end to end.

    Before this test the only ``restore`` coverage was the negative case, so
    making guard 2 over-fire for ``restore`` alone (measured: ``|| [[ "$1" ==
    restore ]]``) passed all 2367 tests. ``/dev-test restore`` would be
    permanently broken, the dev build would stay live in the cache, and every
    later session would silently run unreleased code.

    Asserts the restored BYTES, not just exit 0: a restore that leaves the dev
    content in place exits 0 too.
    """
    if host == 'codex':
        _codex_guard_case('test_restore_puts_the_original_content_back', tmp_path, monkeypatch)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    backup_dir = cache_dir.parent / f"{cache_dir.name}.bak"
    (backup_dir / "hooks").mkdir(parents=True)
    (backup_dir / "hooks" / "obsidian_utils.py").write_text("# original hook\n")
    (backup_dir / "skills" / "dev-test").mkdir(parents=True)
    (backup_dir / "skills" / "dev-test" / "SKILL.md").write_text("# original skill\n")
    # The live cache currently holds the dev build.
    (cache_dir / "hooks" / "obsidian_utils.py").write_text("# dev hook\n")

    proc = _run(script, "restore", home)

    assert proc.returncode == 0, f"restore failed: {proc.stderr}"
    assert "# original hook" in (cache_dir / "hooks" / "obsidian_utils.py").read_text()
    assert "# original skill" in (
        cache_dir / "skills" / "dev-test" / "SKILL.md"
    ).read_text()
    assert not backup_dir.exists(), "the promoted backup must be consumed"


@requires_bash
def test_restore_with_no_backup_exits_4_not_0(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """"Nothing to restore" is not "restored", so it cannot share exit 0.

    ``restore`` used to exit 0 both when it swapped the backup back in and when
    there was no ``.bak`` at all -- verified: the no-backup run printed *"No
    backup found … nothing to restore."* and returned EXIT=0. ``/dev-test``'s
    Step 3 has one exit-0 arm and it asserts the first: *"Original version
    restored. Start a new session to pick up the restored version."* So a run
    that changed nothing got narrated as a successful restore, and the user was
    sent to restart a session for a state change that never happened.

    This is the same one-code-one-state contract install's exit 3 established,
    applied to the arm that was left sharing.

    Pins the STDOUT too: the exit code alone would be satisfied by any new
    failure that happened to return 4.
    """
    if host == 'codex':
        _codex_guard_case('test_restore_with_no_backup_exits_4_not_0', tmp_path, monkeypatch)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    assert not list(cache_dir.parent.glob("*.bak")), "fixture must have no backup"

    proc = _run(script, "restore", home)

    assert proc.returncode == 4, (
        "a no-op restore must be distinguishable from a completed one; got "
        f"{proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "nothing to restore" in proc.stdout
    assert "Original v" not in proc.stdout, "must not claim a restore happened"
    # And it really was a no-op: the cache is untouched.
    assert "# released hook" in (cache_dir / "hooks" / "obsidian_utils.py").read_text()


@requires_bash
def test_status_does_not_claim_the_diff_failed_when_a_dev_version_is_active(
    tmp_path: Path,
    host, selected_host_context, monkeypatch) -> None:
    """``diff`` exit 1 means "they differ", which is this branch's whole point.

    ``diff -rq … | head -20 || echo "  (diff failed)"`` under ``set -o
    pipefail``: ``diff`` returns 1 whenever it finds differences -- the only
    outcome reachable here, since a dev version is active precisely BECAUSE the
    cache differs from its backup -- so the ``||`` arm fired after every
    successful listing. Reproduced: the differing-file line printed, then ``
    (diff failed)`` immediately under it, at exit 0.

    That is consequential rather than cosmetic now: ``/dev-test``'s Step 4 tells
    Claude to relay this report verbatim on exit 0, so the caller passes a
    failure that did not occur straight through to the user.
    """
    if host == 'codex':
        _codex_guard_case('test_status_does_not_claim_the_diff_failed_when_a_dev_version_is_active', tmp_path, monkeypatch)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    backup_dir = cache_dir.parent / f"{cache_dir.name}.bak"
    (backup_dir / "hooks").mkdir(parents=True)
    (backup_dir / "hooks" / "obsidian_utils.py").write_text("# original hook\n")
    (backup_dir / "skills" / "dev-test").mkdir(parents=True)
    (backup_dir / "skills" / "dev-test" / "SKILL.md").write_text("# original skill\n")
    (cache_dir / "hooks" / "obsidian_utils.py").write_text("# dev hook\n")

    proc = _run(script, "status", home)

    assert proc.returncode == 0, proc.stderr
    assert "Status: DEV VERSION ACTIVE" in proc.stdout
    # The listing really is produced -- otherwise "(diff failed)" being absent
    # would be satisfied by printing nothing at all.
    assert "obsidian_utils.py differ" in proc.stdout, (
        f"the changed-files listing is missing: {proc.stdout!r}"
    )
    assert "(diff failed)" not in proc.stdout, (
        f"diff exit 1 means 'they differ', not 'diff failed': {proc.stdout!r}"
    )


@requires_bash
@pytest.mark.parametrize(
    "shape",
    ["missing-hooks-file", "empty-skills-dir"],
    ids=["missing-hooks-file", "empty-skills-dir"],
)
def test_restore_refuses_to_promote_an_incomplete_backup(
    tmp_path: Path, shape: str
, host, selected_host_context, monkeypatch) -> None:
    """A ``.bak`` that exists is not a ``.bak`` that is complete.

    ``install``'s ``cp -R`` is not atomic and runs before that arm's ERR trap
    is installed, so an interrupt, a full disk, or an EACCES leaves a
    truncated backup indistinguishable from a good one. The old precondition
    was ``[[ -d "$BACKUP_DIR" ]]`` alone, so ``restore`` promoted the fragment
    over a healthy cache and printed ``Original vX.Y.Z restored.`` at exit 0 —
    reproduced during review, destroying ``skills/recall/SKILL.md``. Worse,
    the script's own ``Backup already exists ... Run 'restore' first`` message
    steers the user directly into that path.

    Both halves of the completeness check get their own row: with only one
    fixture, deleting either half of the ``||`` still passes.
    """
    if host == 'codex':
        _codex_guard_case('test_restore_refuses_to_promote_an_incomplete_backup', tmp_path, monkeypatch, shape=shape)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    backup_dir = cache_dir.parent / f"{cache_dir.name}.bak"
    if shape == "missing-hooks-file":
        (backup_dir / "hooks").mkdir(parents=True)
        (backup_dir / "skills" / "dev-test").mkdir(parents=True)
        (backup_dir / "skills" / "dev-test" / "SKILL.md").write_text("# partial\n")
    else:
        (backup_dir / "hooks").mkdir(parents=True)
        (backup_dir / "hooks" / "obsidian_utils.py").write_text("# partial hook\n")
        (backup_dir / "skills").mkdir(parents=True)  # present but EMPTY

    proc = _run(script, "restore", home)

    assert proc.returncode != 0, f"expected refusal, got 0: {proc.stdout}"
    assert "not a complete plugin backup" in proc.stderr
    assert "Original v" not in proc.stdout, "must not claim a restore happened"
    # The live cache is untouched, and the backup is still there to inspect.
    assert "# released hook" in (cache_dir / "hooks" / "obsidian_utils.py").read_text()
    assert backup_dir.is_dir()


@requires_bash
@pytest.mark.parametrize("cmd", ["status", "install", "restore"])
def test_bak_only_cache_reports_instead_of_exiting_silently(
    tmp_path: Path, cmd: str
, host, selected_host_context, monkeypatch) -> None:
    """The ``.bak``-only cache dir — the aftermath of an interrupted restore.

    ``grep -v '\\.bak$'`` exits 1 when it filters out everything; under
    ``set -o pipefail`` that aborted the ``PLUGIN_VERSION`` assignment before
    the "no cached version" guard could run, so all three subcommands exited 1
    with **no output whatsoever** (reproduced against the pre-fix script).
    That is the least useful possible answer to "what happened to my cache?",
    and it is exactly what a user hits while diagnosing the failure above.

    Pins the message, not just the exit code: the exit code was already
    non-zero when the bug was live.
    """
    if host == 'codex':
        _codex_guard_case('test_bak_only_cache_reports_instead_of_exiting_silently', tmp_path, monkeypatch, cmd=cmd)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    backup_dir = cache_dir.parent / f"{cache_dir.name}.bak"
    cache_dir.rename(backup_dir)  # rm landed, mv did not

    proc = _run(script, cmd, home)

    assert proc.returncode != 0
    assert "No cached version found" in proc.stderr, (
        f"expected a diagnostic, got stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "Only a .bak backup is present" in proc.stderr
    assert ".bak" in proc.stderr


@requires_bash
def test_install_says_so_when_security_tests_are_skipped(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """"Running security tests..." must not print when none ran.

    Announced unconditionally (before the ``-f`` existence check), a missing
    ``scripts/test-security.sh`` produced "Running security tests..." followed
    by a blank line and the success banner — which every reader parses as
    "ran, nothing to report". The script is absent in precisely the situations
    worth knowing about: a partial checkout, or a source tree that resolved to
    something unexpected.
    """
    if host == 'codex':
        _codex_guard_case('test_install_says_so_when_security_tests_are_skipped', tmp_path, monkeypatch)
        return
    home, _repo, script, _cache_dir = _stage_production_geometry(tmp_path)

    proc = _run(script, "install", home)

    assert proc.returncode == 0, proc.stderr
    assert "SKIPPED, not passed" in proc.stdout
    assert "Running security tests..." not in proc.stdout


@requires_bash
def test_install_fails_loudly_when_security_tests_fail(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """A security-test failure must survive to the last line and the exit code.

    It used to print a WARNING, then the full success banner, then exit 0 — so
    the failure was bracketed by success text and vanished entirely from the
    status any caller (a wrapper, CI, or the skill deciding what to tell the
    user) checks.
    """
    if host == 'codex':
        _codex_guard_case('test_install_fails_loudly_when_security_tests_fail', tmp_path, monkeypatch)
        return
    home, repo, script, _cache_dir = _stage_production_geometry(tmp_path)
    security = repo / "scripts" / "test-security.sh"
    security.write_text("#!/usr/bin/env bash\necho 'SECURITY FAILURE: boom'\nexit 1\n")
    security.chmod(0o755)

    proc = _run(script, "install", home)

    assert proc.returncode != 0, f"exit 0 hides the failure: {proc.stdout}"
    assert "SECURITY TESTS FAILED" in proc.stderr
    assert "Start a NEW Claude Code session" not in proc.stdout, (
        "the success banner must not follow a security failure"
    )


@requires_bash
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions, so chmod 000 cannot be staged",
)
def test_an_interrupted_backup_never_produces_a_bak(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """The reviewer's round-2 reproduction, at its source.

    Shape-validating the ``.bak`` on the restore side samples the failure mode
    rather than closing it: a ``cp -R`` interrupted anywhere INSIDE ``skills/``
    — 19 directories, the bulk of the tree, and therefore the most likely
    interrupt point — leaves ``hooks/obsidian_utils.py`` and a non-empty
    ``skills/`` both present, passes the sentinel probe, and gets promoted over
    a healthy cache at exit 0 (measured: a backup holding ``hooks/`` +
    ``check-items`` destroyed ``recall`` and ``vault-ask``, printing
    ``Original v3.4.0 restored.``).

    So the backup is now built at ``$BACKUP_DIR.partial.$$`` and renamed into
    place only after ``cp -R`` returns 0. The name ``.bak`` is therefore
    reachable only for a copy that ran to completion, and the state above is
    not producible at all.

    This drives a REAL partial copy — an unreadable subdirectory makes ``cp
    -R`` copy what it can and exit 1, exactly as an interrupt or a full disk
    would — and asserts the aftermath: no ``.bak``, no leftover partial, and a
    cache still holding every skill.
    """
    if host == 'codex':
        _codex_guard_case('test_an_interrupted_backup_never_produces_a_bak', tmp_path, monkeypatch)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    # A multi-skill cache, matching the tree the reviewer destroyed.
    for skill in ("check-items", "recall", "vault-ask"):
        (cache_dir / "skills" / skill).mkdir(parents=True, exist_ok=True)
        (cache_dir / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n")
    unreadable = cache_dir / "skills" / "vault-ask"

    unreadable.chmod(0o000)
    try:
        proc = _run(script, "install", home)
    finally:
        unreadable.chmod(0o755)

    assert proc.returncode != 0, f"a failed backup must not exit 0: {proc.stdout}"
    assert "The cache was NOT modified" in proc.stderr, (
        "a backup failure has changed nothing, so 'run restore' would be the "
        f"wrong advice; got stderr={proc.stderr!r}"
    )
    # The whole point: the name `.bak` never appeared.
    assert not list(cache_dir.parent.glob("*.bak")), (
        "a partial copy was published under the .bak name — `restore` would "
        "promote it over the live cache"
    )
    assert not list(cache_dir.parent.glob("*.partial.*")), (
        "the ERR trap must remove the out-of-place partial"
    )
    # And the live cache is exactly as it was — every skill still present.
    assert sorted(p.name for p in (cache_dir / "skills").iterdir()) == [
        "check-items",
        "dev-test",
        "recall",
        "vault-ask",
    ]
    assert "# released hook" in (cache_dir / "hooks" / "obsidian_utils.py").read_text()


@requires_bash
@pytest.mark.parametrize(
    "suffix",
    [
        # What the script actually creates: "${BACKUP_DIR}.partial.$$", and
        # BACKUP_DIR already carries ".bak". This row is the production shape;
        # note it does NOT end in ".bak", so the `\.bak$` arm of the filter
        # cannot catch it — only the `\.partial\.` arm can.
        ".bak.partial.4242",
        # The shorter shape, kept so the filter stays correct if the partial is
        # ever built from the bare version rather than from BACKUP_DIR.
        ".partial.4242",
        # Non-numeric suffix: unreachable via BACKUP_TMP (`$$` is always
        # numeric) but hand-plantable, and it is the shape that made the
        # version-scan filter and `status`'s orphan-listing glob
        # (`*.partial.*`, unanchored) disagree before the scan filter was
        # broadened to match. Pin that they now agree.
        ".bak.partial.abc",
    ],
)
def test_a_leftover_partial_backup_is_not_selected_as_the_version(
    tmp_path: Path, suffix: str
, host, selected_host_context, monkeypatch) -> None:
    """A crashed backup leaves ``<version>.bak.partial.<pid>`` beside the cache.

    ``sort -V`` ranks that string ABOVE the bare version it derives from
    (verified: ``3.4.1`` < ``3.4.1.bak`` < ``3.4.1.bak.partial.9999``), so
    without filtering it the version scan would select the truncated tree and
    every subcommand would operate on it — the same class of silent-wrong-tree
    bug the ``.bak`` filter already exists for.
    """
    if host == 'codex':
        _codex_guard_case('test_a_leftover_partial_backup_is_not_selected_as_the_version', tmp_path, monkeypatch, suffix=suffix)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    stale = cache_dir.parent / f"{cache_dir.name}{suffix}"
    (stale / "hooks").mkdir(parents=True)

    proc = _run(script, "status", home)

    assert proc.returncode == 0, proc.stderr
    assert "Installed cache version: 1.2.3" in proc.stdout, (
        f"the leftover partial was selected as a version: {proc.stdout!r}"
    )
    # The leftover IS named in the orphan disclosure at the end of `status`
    # (that report is deliberate — see test_status_discloses_orphaned_partial
    # _backups), so a whole-stdout `not in` would now pass or fail for the
    # wrong reason. Assert on the two lines that carry the selection instead,
    # anchored to the leftover's own name rather than the bare word "partial":
    # pytest's tmp_path is derived from the test name and contains it too.
    selection_lines = [
        ln
        for ln in proc.stdout.splitlines()
        if ln.startswith(("Installed cache version:", "Cache dir:"))
    ]
    assert len(selection_lines) == 2, f"status changed shape: {proc.stdout!r}"
    for line in selection_lines:
        assert stale.name not in line, (
            f"the leftover partial was selected: {line!r}"
        )


@requires_bash
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permissions, so chmod 000 cannot be staged",
)
@pytest.mark.parametrize("cmd", ["status", "install"])
def test_unreadable_cache_base_reports_instead_of_exiting_silently(
    tmp_path: Path, cmd: str
, host, selected_host_context, monkeypatch) -> None:
    """``ls -1`` inside a ``pipefail`` pipeline can still abort the assignment.

    Scoping ``|| true`` to the ``grep`` alone (the first fix) covered only
    "grep filtered everything". When ``$CACHE_BASE`` exists but is not
    readable, ``ls -1`` itself exits non-zero, ``pipefail`` propagates it, and
    ``set -e`` aborts on the assignment line — before the "no cached version"
    guard can print. Reproduced against that version: ``status`` and
    ``install`` both exited 1 with ZERO bytes of output, which is the least
    useful possible answer to "what happened to my cache?".

    The permission note is pinned too, not just the exit code: the exit code
    was already non-zero while the bug was live.
    """
    if host == 'codex':
        _codex_guard_case('test_unreadable_cache_base_reports_instead_of_exiting_silently', tmp_path, monkeypatch, cmd=cmd)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    cache_base = cache_dir.parent

    cache_base.chmod(0o000)
    try:
        proc = _run(script, cmd, home)
    finally:
        # Restore before teardown, or tmp_path cleanup fails on an
        # unreadable directory and the failure is attributed to the wrong test.
        cache_base.chmod(0o755)

    assert proc.returncode != 0
    assert "No cached version found" in proc.stderr, (
        f"expected a diagnostic, got stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "is not readable" in proc.stderr


@requires_bash
def test_a_partway_install_exits_3_not_1(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """A failure after backup publication keeps exit 3 and recoverable originals.

    A forbidden source symlink fails staging after hooks were copied. The active
    cache and published backup must both retain the exact released bytes.
    """
    if host == 'codex':
        _codex_guard_case('test_a_partway_install_exits_3_not_1', tmp_path, monkeypatch)
        return
    home, repo, script, cache_dir = _stage_production_geometry(tmp_path)
    original_cache = _tree_bytes(cache_dir)

    def tree_modes(root):
        return {str(path.relative_to(root)): path.stat().st_mode & 0o7777
                for path in (root, *root.rglob('*'))}

    original_modes = tree_modes(cache_dir)
    # Hooks stage first; a later source symlink must not publish that partial tree.
    (repo / "skills" / "blocked-link").symlink_to(tmp_path / "outside-source")

    proc = _run(script, "install", home)

    assert proc.returncode == 3, (
        "a partway install must be distinguishable from a guard that refused "
        f"before writing; got {proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "Install failed partway" in proc.stderr
    assert "backup of the original is in place" in proc.stderr, (
        "the message must state that a backup exists, not merely that "
        f"something failed; got {proc.stderr!r}"
    )
    assert "mix of released and dev files" in proc.stderr, (
        "the message must warn the cache may be in a mixed state; got "
        f"{proc.stderr!r}"
    )
    assert "/dev-test restore" in proc.stderr
    assert _tree_bytes(cache_dir) == original_cache
    assert _tree_bytes(cache_dir.with_name(cache_dir.name + ".bak")) == original_cache
    assert tree_modes(cache_dir) == original_modes
    assert tree_modes(cache_dir.with_name(cache_dir.name + ".bak")) == original_modes
    assert not list(cache_dir.parent.glob('.runtime-stage-*'))
    assert not list(cache_dir.parent.glob('.runtime-previous-*'))
    # The precondition the exit code is claiming: a backup really was published.
    assert list(cache_dir.parent.glob("*.bak")), (
        "exit 3 asserts a backup exists to restore from — if none was "
        "published this test is measuring the wrong failure"
    )


@requires_bash
def test_status_discloses_orphaned_partial_backups(tmp_path: Path, host, selected_host_context, monkeypatch) -> None:
    """``status`` used to say "cache is clean" beside full copies of the cache.

    A hard kill (SIGKILL, power loss) skips the install arm's ERR trap, so
    ``<version>.bak.partial.<pid>`` survives — and no later run removes it,
    because ``rm -rf "$BACKUP_TMP"`` only ever clears the CURRENT pid's name.
    The state is correctly inert (filtered from the version scan, never named
    by ``restore``), but each orphan is a FULL copy of the plugin cache, so
    they are real disk that nothing disclosed.

    Two orphans, so a single-item report cannot pass by accident.
    """
    if host == 'codex':
        _codex_guard_case('test_status_discloses_orphaned_partial_backups', tmp_path, monkeypatch)
        return
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)
    orphans = []
    for pid in ("88888", "99999"):
        stale = cache_dir.parent / f"{cache_dir.name}.bak.partial.{pid}"
        (stale / "hooks").mkdir(parents=True)
        orphans.append(stale)

    proc = _run(script, "status", home)

    assert proc.returncode == 0, proc.stderr
    # Still the correct headline — the orphans are not a backup.
    assert "Status: ORIGINAL" in proc.stdout
    assert "Orphaned partial backups" in proc.stdout, (
        f"status must disclose the leftovers, got {proc.stdout!r}"
    )
    assert "safe to delete once no '/dev-test install' is running" in proc.stdout, (
        "the hint must be qualified -- the script never auto-deletes these "
        f"because the pid may belong to a live install; got {proc.stdout!r}"
    )
    for stale in orphans:
        assert stale.name in proc.stdout, f"{stale.name} not listed in {proc.stdout!r}"
    # Disclosure only: the pid may belong to a live install, so nothing is
    # removed on the reporting path.
    for stale in orphans:
        assert stale.is_dir(), "status must not delete another process's partial"


@requires_bash
def test_status_says_nothing_about_partials_when_there_are_none(
    tmp_path: Path,
    host, selected_host_context, monkeypatch) -> None:
    """Negative control for the disclosure above.

    Without it, a report hardcoded to print unconditionally would satisfy the
    positive test while adding a permanent false alarm to every ``status`` run.
    """
    if host == 'codex':
        _codex_guard_case('test_status_says_nothing_about_partials_when_there_are_none', tmp_path, monkeypatch)
        return
    home, _repo, script, _cache_dir = _stage_production_geometry(tmp_path)

    proc = _run(script, "status", home)

    assert proc.returncode == 0, proc.stderr
    assert "Orphaned partial backups" not in proc.stdout


@requires_bash
@pytest.mark.parametrize('shape', ['missing-hooks-file', 'empty-skills-dir'])
def test_restore_rejects_backup_damaged_after_install(tmp_path, host, selected_host_context, monkeypatch, shape):
    if host == 'codex':
        source, home, cache = _native_geometry(tmp_path)
        run = lambda mode: _native_run(source, home, cache, mode)
        config = home / '.codex/config.toml'
    else:
        home, source, script, cache = _stage_production_geometry(tmp_path)
        run = lambda mode: _run(script, mode, home)
        config = None
    installed = run('install')
    assert installed.returncode == 0, installed.stderr
    from tests.native_install_test_helpers import backup_path
    backup = backup_path(home, cache)
    if shape == 'missing-hooks-file':
        (backup / 'hooks/obsidian_utils.py').unlink()
    else:
        shutil.rmtree(backup / 'skills')
        (backup / 'skills').mkdir()
    healthy = _tree_bytes(cache)
    selected_config = config.read_bytes() if config else None
    result = run('restore')
    assert result.returncode != 0 and result.stderr.strip()
    assert _tree_bytes(cache) == healthy
    assert backup.is_dir()
    if config:
        assert config.read_bytes() == selected_config
