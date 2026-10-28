"""Subprocess acceptance tests for harden-repo v1.3.0 (892d0f5).

All Git state belongs to per-test repositories. Commands in hook payloads are
judged, never executed; the Bash truth checks only run git --version or type.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import NamedTuple

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".claude" / "hooks"
# The vendored bytes at harden-repo tag v1.3.0, commit 892d0f5 (#418).
V130_SHA256 = {
    "enforce-pr-base-branch.py":
        "6cd5585b9894776a6cb422b8a3714e9663372c2a1f30c68b4bdf9d3d1b97bf0c",
    "prevent-direct-push.py":
        "87734a8844dcd86943afa59ef6c999e5e4becbcaded4434cfef0240af537467d",
    "require-preflight.py":
        "3de792e15dcf89b210a677e260cf0734518ec41575f1f3af5cd9f541238dbb9e",
    "update-changelog-before-pr.py":
        "883b41cbcc94a06a47fb2ce654643cb3bf87c331b611efbc8962ad263dd47292",
    "validate-branch-name.py":
        "2d44d5bcc0387ae25ccde0b3cd7b3e3fac71aaed16dd2e8cd69f06e1d642cdb3",
}

GATES = (
    pytest.param("require-preflight.py", "git", "commit -m x", id="preflight"),
    pytest.param("prevent-direct-push.py", "git", "push origin main", id="push"),
    pytest.param("validate-branch-name.py", "git", "checkout -b bad_name",
                 id="branch"),
    pytest.param("enforce-pr-base-branch.py", "gh",
                 "pr create --base main --title t --body b", id="pr-base"),
    pytest.param("update-changelog-before-pr.py", "gh",
                 "pr create --base develop --title t --body b", id="changelog"),
)
SPELLINGS = (
    pytest.param("git", "gh", id="plain"),
    pytest.param('"git"', '"gh"', id="double-quoted"),
    pytest.param("'git'", "'gh'", id="single-quoted"),
    pytest.param(r"\git", r"\gh", id="leading-backslash"),
    pytest.param(r"g\it", r"g\h", id="internal-backslash"),
    pytest.param('g""it', 'g""h', id="empty-quotes"),
)
DENSITY_GATES = (
    pytest.param("prevent-direct-push.py", "git push origin main", id="push"),
    pytest.param("require-preflight.py", "git commit -m x", id="preflight"),
)
DENSITY_PREFIXES = {"cd": "cd /tmp && ", "git-c": "git -C /tmp status && "}
PREVENDOR_HOOKS = os.environ.get("OB_PREVENDOR_HOOKS", "")


class Repo(NamedTuple):
    root: Path
    env: dict
    token: Path


class Decision(NamedTuple):
    verdict: str
    reason: str
    seconds: float


def _git(repo, *args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd or repo.root, env=repo.env, check=True,
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()


def _token_path(repo, cwd):
    """Match commit-preflight.sh's real git-common-dir key, including worktrees."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
                  cwd=cwd)
    key = os.path.realpath(common)
    digest = hashlib.md5(key.encode()).hexdigest()[:8]
    return Path(f"/tmp/.preflight-token-{digest}")


@pytest.fixture
def repo(tmp_path):
    """Commit the vendored files in a fresh repo; clean up only its own token."""
    work = tmp_path / "repo"
    work.mkdir()
    shutil.copytree(HOOKS, work / ".claude" / "hooks")
    (work / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "commit-preflight.sh",
                 work / "scripts" / "commit-preflight.sh")
    (work / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.0.0]\n\n- Seed.\n",
        encoding="utf-8",
    )
    # Ambient Git redirects/config, shell startup code, or a shortened hook
    # deadline must not make these subprocesses inspect the developer's repo.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "BASH_FUNC_"))
           and k not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS",
                         "HARDEN_HOOK_DEADLINE"}}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               CLAUDE_PROJECT_DIR=str(work))
    sandbox = Repo(work, env, tmp_path / "unused")
    _git(sandbox, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Hook Test"),
                       ("user.email", "hooks@example.invalid"),
                       ("core.hooksPath", str(tmp_path / "disabled-hooks")),
                       ("commit.gpgSign", "false")):
        _git(sandbox, "config", key, value)
    _git(sandbox, "add", ".")
    _git(sandbox, "commit", "-qm", "Seed vendored hooks")
    _git(sandbox, "branch", "develop")
    _git(sandbox, "checkout", "-qb", "feature/probe")
    token = _token_path(sandbox, work)
    lock = Path(str(token) + ".lock")
    # Refuse a hash collision or a stale file rather than remove someone
    # else's machine-wide token. No /tmp globbing, even during teardown.
    assert not os.path.lexists(token), f"Token path already occupied: {token}"
    assert not os.path.lexists(lock), f"Token lock already occupied: {lock}"
    sandbox = sandbox._replace(token=token)
    try:
        yield sandbox
    finally:
        token.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def _run_hook(repo, hook, command, *, cwd=None, hooks_dir=None):
    cwd = cwd or repo.root
    payload = {"tool_name": "Bash", "tool_input": {"command": command},
               "cwd": str(cwd)}
    script = (hooks_dir or repo.root / ".claude" / "hooks") / hook
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload),
        capture_output=True, text=True, cwd=cwd, env=repo.env, timeout=10,
    )
    seconds = time.perf_counter() - start
    assert proc.returncode == 0, (
        f"{hook} exited {proc.returncode}; a hook crash is not a verdict: "
        f"{proc.stderr[-500:]}"
    )
    if not proc.stdout.strip():
        return Decision("allow", "", seconds)
    # Malformed/non-protocol stdout is a failure, not a silent allow.
    output = json.loads(proc.stdout)["hookSpecificOutput"]
    verdict = output.get("permissionDecision", "allow")
    assert verdict in {"allow", "deny"}, proc.stdout
    return Decision(verdict, output.get("permissionDecisionReason", ""), seconds)


def _worktree(repo, name):
    work = repo.root.parent / name
    _git(repo, "worktree", "add", "-qb", f"feature/{name}", str(work))
    assert (work / ".git").is_file(), "This must be a linked worktree"
    assert _token_path(repo, work) == repo.token
    return work


def _stage(repo, cwd):
    (cwd / "change.txt").write_text("staged change\n", encoding="utf-8")
    _git(repo, "add", "change.txt", cwd=cwd)


def _write_bound_token(repo, cwd, **overrides):
    """Match _preflight_write_token's JSON fields without running repo checks."""
    created = int(time.time())
    staged = _git(repo, "diff", "--cached", "--name-only", cwd=cwd).splitlines()
    token = {"created": created, "expires": created + 300,
             "staged_files": len(staged), "checks_run": "tests",
             "head": _git(repo, "rev-parse", "HEAD", cwd=cwd),
             "tree": _git(repo, "write-tree", cwd=cwd),
             "worktree": os.path.realpath(
                 _git(repo, "rev-parse", "--show-toplevel", cwd=cwd))}
    token.update(overrides)
    repo.token.write_text(json.dumps(token), encoding="utf-8")
    repo.token.chmod(0o600)
    return token


@pytest.mark.parametrize("hook", V130_SHA256, ids=list(V130_SHA256))
def test_hook_bytes_match_harden_repo_v130(hook):
    """#418: local edits cannot silently diverge from the v1.3.0 templates."""
    assert hashlib.sha256((HOOKS / hook).read_bytes()).hexdigest() == V130_SHA256[hook], (
        f"{hook} differs from harden-repo v1.3.0 (892d0f5); "
        "re-vendor from harden-repo rather than edit by hand."
    )


def test_worktree_preflight_token_allows_its_own_checkout(repo):
    """#418 C.1: a common-dir token authorizes the linked checkout it binds."""
    work = _worktree(repo, "linked")
    _stage(repo, work)
    assert _run_hook(repo, "require-preflight.py", "git commit -m x",
                     cwd=work).verdict == "deny"
    _write_bound_token(repo, work)
    assert _run_hook(repo, "require-preflight.py", "git commit -m x",
                     cwd=work).verdict == "allow"


def test_worktree_token_does_not_authorize_another_checkout(repo):
    """#418 C.2: matching staged trees do not let A's token authorize B's HEAD."""
    a, b = _worktree(repo, "a"), _worktree(repo, "b")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Advance B", cwd=b)
    _stage(repo, a)
    _stage(repo, b)
    assert _git(repo, "rev-parse", "HEAD", cwd=a) != _git(
        repo, "rev-parse", "HEAD", cwd=b)
    assert _git(repo, "write-tree", cwd=a) == _git(repo, "write-tree", cwd=b)
    _write_bound_token(repo, a)
    denied = _run_hook(repo, "require-preflight.py", "git commit -m x", cwd=b)
    assert denied.verdict == "deny"
    assert "another checkout" in denied.reason.lower()
    assert _run_hook(repo, "require-preflight.py", "git commit -m x",
                     cwd=a).verdict == "allow"


@pytest.mark.parametrize("branch", ["main", "develop"], ids=["main", "develop"])
def test_push_from_worktree_still_protects_shared_branches(repo, branch):
    """#418 C.3: a sibling worktree remains in scope for protected pushes."""
    work = _worktree(repo, "linked")
    assert _run_hook(repo, "prevent-direct-push.py", f"git push origin {branch}",
                     cwd=work).verdict == "deny"
    assert _run_hook(repo, "prevent-direct-push.py", "git push origin feature/probe",
                     cwd=work).verdict == "allow"


@pytest.mark.parametrize("hook,program,args", GATES)
@pytest.mark.parametrize("shape,expected", [
    pytest.param("cat <<'EOF' > notes.md\n{verb}\nEOF\n", "allow", id="heredoc"),
    pytest.param('grep -rn "{verb}" docs/', "allow", id="grep"),
    pytest.param("echo '{verb}' > f.txt", "allow", id="echo"),
    pytest.param("printf '%s\\n' '{verb}'", "allow", id="printf"),
    pytest.param("gh issue edit 1 --body 'do not {verb}'", "allow", id="issue-body"),
    pytest.param("{verb}", "deny", id="bare-control"),
])
def test_data_mentions_do_not_run_the_guarded_verb(repo, hook, program, args,
                                                 shape, expected):
    """#343 C.1: quoted data allows, while the same bare verb really denies."""
    command = shape.format(verb=f"{program} {args}")
    assert _run_hook(repo, hook, command).verdict == expected


@pytest.mark.parametrize("hook,program,args", GATES)
@pytest.mark.parametrize("git_spelling,gh_spelling", SPELLINGS)
def test_command_name_spellings_match_plain_denial(repo, hook, program, args,
                                                  git_spelling, gh_spelling):
    """#366 C.1/C.2: Bash quote removal cannot bypass any of the five gates."""
    plain = _run_hook(repo, hook, f"{program} {args}").verdict
    assert plain == "deny", "Plain-name control must exercise the guard"
    spelling = git_spelling if program == "git" else gh_spelling
    assert _run_hook(repo, hook, f"{spelling} {args}").verdict == plain


@pytest.mark.parametrize("program", [
    pytest.param("git", id="git"),
    pytest.param("gh", id="gh", marks=pytest.mark.skipif(
        shutil.which("gh") is None, reason="gh is not installed (Bash truth only)")),
])
@pytest.mark.parametrize("git_spelling,gh_spelling", SPELLINGS)
def test_bash_resolves_command_name_spellings(repo, program, git_spelling, gh_spelling):
    """#366: Bash runs the same git, or resolves the same gh, for each spelling."""
    spelling = git_spelling if program == "git" else gh_spelling
    command = f"{spelling} --version" if program == "git" else f"type -P {spelling}"
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command], cwd=repo.root,
        env=repo.env, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    if program == "git":
        assert proc.stdout.strip() == _git(repo, "--version")
        hook, args = "prevent-direct-push.py", "push origin main"
    else:
        assert Path(proc.stdout.strip()).resolve() == Path(shutil.which("gh")).resolve()
        hook, args = "enforce-pr-base-branch.py", "pr create --base main --title t --body b"
    assert _run_hook(repo, hook, f"{spelling} {args}").verdict == "deny"


def _dense_command(repo, prefix, verb, kb):
    # Bare `cd /tmp && ... <verb>` correctly descopes the verb. Return to
    # this repo after the dense prefix so the expected denial tests a guard.
    tail = f"cd {shlex.quote(str(repo.root))} && {verb}" if prefix.startswith("cd ") else verb
    count = (kb * 1024 - len(tail.encode())) // len(prefix.encode())
    assert count > 0
    command = prefix * count + tail
    assert kb * 1024 - len(prefix.encode()) < len(command.encode()) <= kb * 1024
    return command


def _best_denied_time(repo, hook, command, *, hooks_dir=None, allow_timeout=False):
    samples = []
    for _ in range(3):
        try:
            result = _run_hook(repo, hook, command, hooks_dir=hooks_dir)
        except subprocess.TimeoutExpired:
            if not allow_timeout:
                raise
            samples.append(math.inf)  # No decision within 10 s violates the budget.
        else:
            assert result.verdict == "deny", f"{hook} density row failed open"
            samples.append(result.seconds)
    return min(samples)


@pytest.mark.parametrize("hook,verb", DENSITY_GATES)
@pytest.mark.parametrize("prefix", DENSITY_PREFIXES.values(), ids=list(DENSITY_PREFIXES))
@pytest.mark.parametrize("kb", [2, 4, 8, 17], ids=["2kb", "4kb", "8kb", "17kb"])
def test_dense_prefixes_keep_the_guard_active(repo, hook, verb, prefix, kb):
    """#353 C.1: dense cd and git -C prefixes cannot turn either gate off."""
    assert _run_hook(repo, hook, _dense_command(repo, prefix, verb, kb)).verdict == "deny"


@pytest.mark.parametrize("hook,verb", DENSITY_GATES)
@pytest.mark.parametrize("prefix", DENSITY_PREFIXES.values(), ids=list(DENSITY_PREFIXES))
def test_density_decision_budget_and_growth(repo, hook, verb, prefix):
    """#353 C.2/C.3: best-of-three 17 KB decisions stay under 1 s and 8x 4 KB."""
    small = _best_denied_time(repo, hook, _dense_command(repo, prefix, verb, 4))
    large = _best_denied_time(repo, hook, _dense_command(repo, prefix, verb, 17))
    assert large < 1.0, f"17 KB best of 3: {large:.3f}s (budget 1.0s)"
    assert large / small < 8, f"4 KB {small:.3f}s -> 17 KB {large:.3f}s: {large / small:.2f}x"


@pytest.mark.skipif(not PREVENDOR_HOOKS or not Path(PREVENDOR_HOOKS).is_dir(),
                    reason="Set OB_PREVENDOR_HOOKS to a pre-vendor hooks directory")
@pytest.mark.parametrize("hook,verb", DENSITY_GATES)
def test_density_rows_are_red_on_the_pre_vendor_hooks(repo, hook, verb):
    """#353: the same sweep exposes each old hook's quadratic cd handling.

    The git -C status rows were already linear at 8c76197, so require a
    regression in the sweep for each hook, rather than in every shape.

    Run: git worktree add <dir> 8c76197
    Then: OB_PREVENDOR_HOOKS=<dir>/.claude/hooks python3 -m pytest \
        tests/test_vendored_hooks.py -q -p no:cacheprovider --tb=short
    """
    hooks_dir = Path(PREVENDOR_HOOKS).resolve()
    assert (hooks_dir / hook).is_file()
    measurements = {}
    for label, prefix in DENSITY_PREFIXES.items():
        small = _best_denied_time(repo, hook, _dense_command(repo, prefix, verb, 4),
                                  hooks_dir=hooks_dir, allow_timeout=True)
        large = _best_denied_time(repo, hook, _dense_command(repo, prefix, verb, 17),
                                  hooks_dir=hooks_dir, allow_timeout=True)
        measurements[label] = (small, large)
    assert any(large >= 1.0 or large / small >= 8
               for small, large in measurements.values()), (
        f"Old {hook} did not expose the quadratic (4 KB, 17 KB seconds): {measurements}"
    )


@pytest.mark.parametrize("expiry", [
    pytest.param(9999999999, id="far-future"),
    pytest.param(None, id="near-future-binding-control"),
])
def test_unbound_token_does_not_authorize_a_commit(repo, expiry):
    """Valid expiry cannot rescue a token missing head, tree and worktree."""
    _stage(repo, repo.root)
    # v1.3.0 refuses both >1-hour expiry and the legacy unbound format (#166).
    # A near-future row reaches binding validation; 9999999999 alone cannot.
    repo.token.write_text(json.dumps({
        "expires": int(time.time()) + 300 if expiry is None else expiry,
    }), encoding="utf-8")
    result = _run_hook(repo, "require-preflight.py", "git commit -m x")
    assert result.verdict == "deny"
    if expiry is None:
        assert "old format" in result.reason.lower()
    _write_bound_token(repo, repo.root)
    assert _run_hook(repo, "require-preflight.py", "git commit -m x").verdict == "allow"


def test_staging_and_commit_in_one_call_cannot_reuse_a_checked_tree(repo):
    """G-20: a valid token for the pre-add tree cannot approve add && commit."""
    _stage(repo, repo.root)
    token = _write_bound_token(repo, repo.root)
    (repo.root / "unchecked.txt").write_text("not checked\n", encoding="utf-8")
    assert _git(repo, "write-tree") == token["tree"]
    assert "unchecked.txt" not in _git(repo, "diff", "--cached", "--name-only")
    result = _run_hook(repo, "require-preflight.py", "git add -A && git commit -m x")
    assert result.verdict == "deny"
    assert "stage" in result.reason.lower()
    assert _git(repo, "write-tree") == token["tree"], "Payload must not execute"
    assert _run_hook(repo, "require-preflight.py", "git commit -m x").verdict == "allow"
