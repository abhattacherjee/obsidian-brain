"""Tests for the two repo-root guards in scripts/test-dev-skill.sh (#287).

The script derives REPO_ROOT from its own location
(``$(dirname "$0")/..``) and uses it as the source tree for ``install``.
That is correct when the script is invoked from a real obsidian-brain
checkout, but silently wrong in two other cases the caller cannot be
trusted to rule out:

1. The directory the script happens to sit two levels above isn't an
   obsidian-brain checkout at all (missing ``hooks/obsidian_utils.py`` /
   ``skills/``).
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
    return script_path


def _run(script: Path, cmd: str, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        # _BASH, not the literal "bash": `requires_bash` skips on
        # shutil.which("bash"), so invoking anything else would let the skip
        # guard and the invocation disagree about which binary is under test.
        [_BASH, str(script), cmd],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _stage_production_geometry(tmp_path: Path, version: str = "1.2.3"):
    """A checkout UNDER $HOME, plus a plugin cache — the real-world layout.

    The pre-existing fixtures put the checkout and ``$HOME`` in sibling
    directories, a geometry that does not occur in production: real checkouts
    live under the user's home (this repo is at
    ``/Users/<me>/dev/claude_workspace/obsidian-brain``). That matters because
    every other guard-2 fixture asserts the guard FIRES; without a positive
    control staged the way users actually have it, widening the prefix from
    ``$HOME/.claude/plugins/`` to ``$HOME/`` — which would refuse
    ``/dev-test install`` for essentially everyone — passes the entire suite.

    Returns ``(home, repo, script, cache_dir)``.
    """
    home = tmp_path / "home"
    repo = home / "dev" / "obsidian-brain"
    script = _write_script(repo / "scripts")
    (repo / "hooks").mkdir(parents=True)
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
    assert GUARD1_MSG in proc.stderr


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
    assert GUARD1_MSG in proc.stderr
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
    assert GUARD1_MSG in proc.stderr


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
    assert GUARD1_MSG in proc.stderr


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
    assert GUARD2_MSG in proc.stderr
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
    assert GUARD2_MSG in proc.stderr


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


@requires_bash
def test_self_copy_guard_rejects_install_from_a_marketplace_clone(
    tmp_path: Path,
) -> None:
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
    home = tmp_path / "home"
    clone = home / ".claude" / "plugins" / "marketplaces" / "obsidian-brain-repo"
    script = _write_script(clone / "scripts")
    (clone / "hooks").mkdir(parents=True)
    (clone / "hooks" / "obsidian_utils.py").write_text("# released hook\n")
    (clone / "skills" / "some-skill").mkdir(parents=True)

    proc = _run(script, "install", home)

    assert proc.returncode != 0, f"expected non-zero exit, got 0: {proc.stdout}"
    assert GUARD2_MSG in proc.stderr
    assert "marketplace clone" in proc.stderr


@requires_bash
def test_install_proceeds_from_a_checkout_under_home(tmp_path: Path) -> None:
    """Guard 2's positive control, in the geometry users actually have.

    Every other guard-2 fixture asserts the guard FIRES, and the fixtures
    where it must not fire (tests/test_dev_skill_install.py) put the repo and
    ``$HOME`` in sibling directories — which never happens in production.
    Consequence, measured: widening ``PLUGIN_ROOT_PREFIX`` to ``$HOME/``
    passed all 2367 tests, while bricking ``/dev-test install`` for every
    developer whose checkout lives under their home directory.

    So: a checkout under ``$HOME`` but outside ``.claude/plugins/`` must
    install cleanly. Asserting the guard messages are ABSENT (not just
    ``returncode == 0``) is what makes this discriminating — a future guard
    that fires for a different reason cannot launder itself through an exit
    code that some other branch also produces.
    """
    home, _repo, script, cache_dir = _stage_production_geometry(tmp_path)

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
def test_restore_puts_the_original_content_back(tmp_path: Path) -> None:
    """The successful ``restore`` path, executed end to end.

    Before this test the only ``restore`` coverage was the negative case, so
    making guard 2 over-fire for ``restore`` alone (measured: ``|| [[ "$1" ==
    restore ]]``) passed all 2367 tests. ``/dev-test restore`` would be
    permanently broken, the dev build would stay live in the cache, and every
    later session would silently run unreleased code.

    Asserts the restored BYTES, not just exit 0: a restore that leaves the dev
    content in place exits 0 too.
    """
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
@pytest.mark.parametrize(
    "shape",
    ["missing-hooks-file", "empty-skills-dir"],
    ids=["missing-hooks-file", "empty-skills-dir"],
)
def test_restore_refuses_to_promote_an_incomplete_backup(
    tmp_path: Path, shape: str
) -> None:
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
) -> None:
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
def test_install_says_so_when_security_tests_are_skipped(tmp_path: Path) -> None:
    """"Running security tests..." must not print when none ran.

    Announced unconditionally (before the ``-f`` existence check), a missing
    ``scripts/test-security.sh`` produced "Running security tests..." followed
    by a blank line and the success banner — which every reader parses as
    "ran, nothing to report". The script is absent in precisely the situations
    worth knowing about: a partial checkout, or a source tree that resolved to
    something unexpected.
    """
    home, _repo, script, _cache_dir = _stage_production_geometry(tmp_path)

    proc = _run(script, "install", home)

    assert proc.returncode == 0, proc.stderr
    assert "SKIPPED, not passed" in proc.stdout
    assert "Running security tests..." not in proc.stdout


@requires_bash
def test_install_fails_loudly_when_security_tests_fail(tmp_path: Path) -> None:
    """A security-test failure must survive to the last line and the exit code.

    It used to print a WARNING, then the full success banner, then exit 0 — so
    the failure was bracketed by success text and vanished entirely from the
    status any caller (a wrapper, CI, or the skill deciding what to tell the
    user) checks.
    """
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
