"""Tests for the two repo-root guards in scripts/test-dev-skill.sh (#287).

The script derives REPO_ROOT from its own location
(``$(dirname "$0")/..``) and uses it as the source tree for ``install``.
That is correct when the script is invoked from a real obsidian-brain
checkout, but silently wrong in two other cases the caller cannot be
trusted to rule out:

1. The directory the script happens to sit two levels above isn't an
   obsidian-brain checkout at all (missing ``hooks/obsidian_utils.py`` /
   ``skills/``).
2. The script is itself running from *inside* the installed plugin cache
   (``~/.claude/plugins/cache/*/obsidian-brain/<version>/``). In that case
   ``REPO_ROOT`` resolves to the cache version directory, and ``install``
   would copy the cache onto itself -- a byte-for-byte no-op that still
   prints a full success transcript (see D3 in
   docs/plans/287-dev-test-repo-root.md).

Both guards must fire before any ``cp`` and before the ``.bak`` backup is
taken, so a bad invocation never leaves stray state behind.

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


def _write_script(dest_dir: Path) -> Path:
    """Copy the real script into dest_dir/test-dev-skill.sh (mode 0755)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    script_path = dest_dir / "test-dev-skill.sh"
    script_path.write_bytes(SCRIPT_PATH.read_bytes())
    script_path.chmod(0o755)
    return script_path


def _run(script: Path, cmd: str, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        ["bash", str(script), cmd],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


@requires_bash
def test_sentinel_guard_rejects_non_checkout(tmp_path: Path) -> None:
    """Guard 1: a directory two levels above the script that lacks
    hooks/obsidian_utils.py and skills/ is not an obsidian-brain checkout --
    the script must refuse rather than trust its own location blindly.

    Deliberately placed OUTSIDE any ~/.claude/plugins/cache/ path, and run
    with "status" (not install/restore), so this failure can only be
    attributed to guard 1 -- guard 2 does not even apply to "status".
    """
    repo = tmp_path / "some-other-project"
    script = _write_script(repo / "scripts")
    home = tmp_path / "home"  # empty; never touched by this test

    proc = _run(script, "status", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert "does not look like an obsidian-brain checkout" in proc.stderr


@requires_bash
def test_sentinel_guard_fires_before_any_mutation(tmp_path: Path) -> None:
    """Guard 1 must fire even for "install" -- before the cache lookup, the
    .bak backup, or any cp -- so a non-checkout invocation never mutates
    anything under $HOME.
    """
    repo = tmp_path / "some-other-project"
    script = _write_script(repo / "scripts")
    home = tmp_path / "home"
    home.mkdir()

    proc = _run(script, "install", home)

    assert proc.returncode != 0
    assert "does not look like an obsidian-brain checkout" in proc.stderr
    # Nothing was created under $HOME -- no cache probing, no .bak.
    assert not any(home.rglob("*"))


@requires_bash
def test_sentinel_guard_rejects_checkout_missing_skills_dir(tmp_path: Path) -> None:
    """Guard 1's ``skills/`` half in isolation.

    The only other guard-1 fixture (above) builds a tree with NEITHER
    sentinel, so each half of the ``||`` is covered by the other -- deleting
    either half alone still passes the suite. This tree carries the
    ``hooks/obsidian_utils.py`` sentinel but deliberately omits ``skills/``,
    so a failure here can only be attributed to the ``skills/`` half.

    That half is genuinely load-bearing in production: with ``skills/``
    absent, the install loop's ``"$REPO_ROOT/skills/"*/`` glob goes
    unmatched and (with nullglob unset, the bash default) stays literal,
    creating a junk ``skills/*`` directory under the cache instead of
    failing loudly up front.
    """
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    # deliberately no skills/ dir
    home = tmp_path / "home"

    proc = _run(script, "status", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert "does not look like an obsidian-brain checkout" in proc.stderr


@requires_bash
def test_sentinel_guard_rejects_checkout_missing_hooks_file(tmp_path: Path) -> None:
    """Guard 1's ``hooks/obsidian_utils.py`` half in isolation.

    Mirror of the test above: this tree carries ``skills/`` but deliberately
    omits ``hooks/obsidian_utils.py``, so a failure here can only be
    attributed to the hooks-sentinel half.
    """
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "skills" / "some-skill").mkdir(parents=True)
    # deliberately no hooks/obsidian_utils.py
    home = tmp_path / "home"

    proc = _run(script, "status", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert "does not look like an obsidian-brain checkout" in proc.stderr


@requires_bash
def test_self_copy_guard_rejects_install_from_inside_cache(tmp_path: Path) -> None:
    """Guard 2 (D3): a script whose own REPO_ROOT resolves to a path under
    ~/.claude/plugins/cache/ must refuse "install" -- copying the cache onto
    itself is a no-op that would otherwise print a full success transcript.

    The staged tree carries valid sentinel files (hooks/obsidian_utils.py,
    skills/) so guard 1 passes and this failure can only be attributed to
    guard 2 -- this is what keeps the fixture from being shadowed by guard 1
    (the guard-ordering trap).
    """
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
    assert "inside the installed plugin cache" in proc.stderr
    # No backup was created -- the guard fired before the .bak step.
    assert not (cache_version_dir.parent / "9.9.9.bak").exists()


@requires_bash
def test_self_copy_guard_rejects_restore_from_inside_cache(tmp_path: Path) -> None:
    """Guard 2 also applies to "restore" -- restoring a .bak while REPO_ROOT
    is the cache itself is equally nonsensical (there is no local checkout
    to have diverged from).
    """
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
    assert "inside the installed plugin cache" in proc.stderr


@requires_bash
def test_self_copy_guard_fires_with_symlinked_home(tmp_path: Path) -> None:
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
    assert "inside the installed plugin cache" in proc.stderr
    # No backup was created under the REAL (non-symlink) location either --
    # the guard fired before the .bak step, regardless of which path string
    # you check it through.
    real_backup = (
        real_home / ".claude" / "plugins" / "cache" / "some-marketplace"
        / "obsidian-brain" / "9.9.9.bak"
    )
    assert not real_backup.exists()


@requires_bash
def test_status_still_works_from_inside_cache(tmp_path: Path) -> None:
    """Judgment call (see task-2-report.md): guard 2 is scoped to the
    mutating subcommands only. "status" is read-only and must keep
    reporting even when the script happens to be running from inside the
    cache -- e.g. a machine with no local checkout that only has the cache.
    """
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
def test_home_fail_closed_block_pins_its_custom_message(tmp_path: Path) -> None:
    """M2: the `$HOME` fail-closed block (script lines ~45-50) is shadowed in
    EXIT STATUS by `set -euo pipefail` -- delete the whole block and the
    script still exits non-zero, because the very next line (`cd "$HOME" &&
    pwd -P`) already fails under `set -u` (unset $HOME) or `set -e` ($HOME
    names a missing directory). What the block adds is message quality, not
    exit-status behaviour, so this test pins the MESSAGE rather than just the
    exit code -- that is the one thing that actually distinguishes "block
    present" from "block deleted" and makes it a real (rather than vacuous)
    guard.

    $HOME is deliberately set to a path that does not exist (not unset --
    `_run` always sets $HOME) so the block's `[[ ! -d "$HOME" ]]` half fires.
    """
    repo = tmp_path / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "obsidian_utils.py").write_text("# fake hook\n")
    (repo / "skills" / "some-skill").mkdir(parents=True)

    missing_home = tmp_path / "does-not-exist"

    proc = _run(script, "install", missing_home)

    assert proc.returncode != 0
    assert "cannot verify this script isn't" in proc.stderr
