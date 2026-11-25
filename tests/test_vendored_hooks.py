"""Subprocess acceptance tests for the vendored harden-repo hooks and preflight.

The vendored files are whatever harden-repo release fleet-sync last applied
(v1.5.0, 9424640, at #532). No test here pins one release's bytes: the
generation test reads harden-repo's own template ledger instead.

All Git state belongs to per-test repositories. Commands in hook payloads are
judged, never executed; the Bash truth checks only run git --version or type.
"""

import importlib.util
import json
import math
import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess
import sys
import time
from typing import NamedTuple

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".claude" / "hooks"
# The files harden-repo vendors here, by their path in its templates/.
VENDORED = {
    ".claude/hooks/enforce-pr-base-branch.py": "templates/hooks/enforce-pr-base-branch.py",
    ".claude/hooks/prevent-direct-push.py": "templates/hooks/prevent-direct-push.py",
    ".claude/hooks/require-preflight.py": "templates/hooks/require-preflight.py",
    ".claude/hooks/update-changelog-before-pr.py": "templates/hooks/update-changelog-before-pr.py",
    ".claude/hooks/validate-branch-name.py": "templates/hooks/validate-branch-name.py",
    "scripts/commit-preflight.sh": "templates/scripts/commit-preflight.sh",
}
# Must name a released tree (a plugin cache directory or a checkout at a
# release tag): it vouches for the files with its own plugin.json version.
HARDEN_ROOT_ENV = "OB_HARDEN_REPO_ROOT"

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
    """The pair's token path for *cwd* under the repo's own HOME: the same
    git-common-dir key for a worktree and its main checkout (#532)."""
    from preflight_token import token_path
    return token_path(cwd, repo.env)


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
    # One HOME for the hooks and the preflight, so both name one token path,
    # and `_heavy` never finds the live ~/.claude/bin/heavy (#525, #532).
    home = tmp_path / "home"
    home.mkdir()
    env.pop("XDG_CACHE_HOME", None)
    env["HOME"] = str(home)
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
    # else's token. No globbing, even during teardown.
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


def _harden_roots():
    """[(root, released)] for the harden-repo trees that can vouch for a
    vendored file.

    The roots are searched in a fixed order ($OB_HARDEN_REPO_ROOT, every
    cached plugin version, the marketplace clone, the dev checkout, all under
    the real home: the tests run with a tmp HOME) and every root's versions
    are merged, so order does not matter. A tree counts only with its ledger,
    its doctor_core.py and its plugin.json.

    `released` is True for the env root and the plugin cache, whose
    plugin.json version names a release. $OB_HARDEN_REPO_ROOT must therefore
    name a released tree (a plugin cache directory or a checkout at a release
    tag): it vouches with its own version. The marketplace clone and the dev
    checkout may hold unreleased templates under the next version, so they
    vouch through their ledger only."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    candidates = []
    if os.environ.get(HARDEN_ROOT_ENV):
        candidates.append((Path(os.environ[HARDEN_ROOT_ENV]), True))
    cache = home / ".claude/plugins/cache/harden-repo/repo-hardener"
    if cache.is_dir():
        candidates.extend((path, True) for path in sorted(cache.iterdir()))
    candidates.append((home / ".claude/plugins/marketplaces/harden-repo", False))
    candidates.append((home / "dev/claude_workspace/harden-repo", False))
    roots = []
    for root, released in candidates:
        if all((root / rel).is_file() for rel in (
                "skills/harden-repo/data/template-ledger.json",
                "skills/harden-repo/scripts/doctor_core.py",
                ".claude-plugin/plugin.json")):
            roots.append((root, released))
    return roots


def _load_doctor_core(root, index):
    """That tree's own doctor_core, under a name of its own."""
    spec = importlib.util.spec_from_file_location(
        f"_ob_doctor_core_{index}", root / "skills/harden-repo/scripts/doctor_core.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _norm_version(version):
    return version[1:] if version.startswith("v") else version


def _vendored_versions(roots, files):
    """{vendored path: set of harden-repo versions whose bytes it matches}.

    `roots` is a list of (root, released) pairs from _harden_roots. A file
    matches a version when that tree's ledger records its hash for the
    template (every release before the tree's own), or, in a released tree
    only, when it equals the tree's own template (the tree's release). An
    unreleased checkout's plugin.json names the next version, which no
    release has shipped yet. Hashes come from the tree's
    doctor_core.artifact_hash, which sets marker bodies aside, so
    commit-preflight.sh matches with this repo's LINT and TEST bodies in."""
    found = {rel: set() for rel in files}
    for index, (root, released) in enumerate(roots):
        core = _load_doctor_core(root, index)
        ledger = json.loads(
            (root / "skills/harden-repo/data/template-ledger.json").read_text(encoding="utf-8"))
        own = _norm_version(json.loads(
            (root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"])
        for rel, text in files.items():
            template_rel = VENDORED[rel]
            digest = core.artifact_hash(text)
            for version in (ledger.get(template_rel) or {}).get(digest) or []:
                found[rel].add(_norm_version(version))
            template = root / template_rel
            if released and template.is_file() and digest == core.artifact_hash(
                    template.read_text(encoding="utf-8")):
                found[rel].add(own)
    return found


def _vendored_files():
    return {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in VENDORED}


def _generation_problem(found):
    """None, or why `found` ({path: set of versions}) is not one generation:
    a file matching no release ("unknown"), or known files sharing no release
    ("mixed")."""
    unknown = sorted(rel for rel, versions in found.items() if not versions)
    if unknown:
        return f"unknown: {unknown} match no harden-repo release"
    if not set.intersection(*found.values()):
        return f"mixed: the vendored files come from different releases: {found}"
    return None


def _assert_one_generation(roots, files):
    """The two assertions of the generation test: every file matches some
    release, and one release covers all of them."""
    problem = _generation_problem(_vendored_versions(roots, files))
    assert problem is None, (
        f"{problem}; searched {[str(r[0]) for r in roots]}. Re-vendor with "
        "doctor.py --fix --safe rather than edit by hand.")


@pytest.mark.parametrize("found,expected", [
    pytest.param({"a": {"1.5.0"}, "b": {"1.5.0", "1.5.1"}}, None, id="shared"),
    pytest.param({"a": set(), "b": {"1.5.0"}}, "unknown", id="unknown"),
    pytest.param({"a": {"1.4.0"}, "b": {"1.5.0"}}, "mixed", id="mixed"),
])
def test_the_generation_rule_on_its_own(found, expected):
    """Runs in CI, where the ledger is absent: the unknown and mixed rules."""
    problem = _generation_problem(found)
    if expected is None:
        assert problem is None, problem
    else:
        assert problem is not None and problem.startswith(expected), problem


def test_vendored_files_are_one_released_generation():
    """#418, #532: the five hooks and commit-preflight.sh are bytes some
    harden-repo release shipped (outside the preflight's marker blocks), and
    all six come from one release, so the hook never pairs with a script that
    writes its token elsewhere. Any later release passes once fleet-sync
    applies it: nothing here names a version.

    Needs a harden-repo tree (see _harden_roots). CI has none (harden-repo is
    private), so it skips there; a copied ledger would be one more pin to
    update by hand. The gap: in CI a hook edited without running the local
    preflight is caught only by the shared-block test in tests/test_security.py
    (test_the_shared_blocks_are_identical_in_all_five_hooks) and by
    test_the_vendored_hooks_compile below. Edits outside the shared blocks
    that still compile pass CI."""
    roots = _harden_roots()
    if not roots:
        pytest.skip(f"no harden-repo tree found; set {HARDEN_ROOT_ENV}")
    _assert_one_generation(roots, _vendored_files())


@pytest.mark.parametrize("rel", sorted(r for r in VENDORED if r.endswith(".py")))
def test_the_vendored_hooks_compile(rel):
    """Runs in CI, where the generation test skips: a hook that no longer
    compiles fails here."""
    path = ROOT / rel
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_a_local_edit_matches_no_release():
    """Mutation control for the generation test: one changed byte in a hook
    is in no ledger, and a hook from another release breaks the shared set."""
    roots = _harden_roots()
    if not roots:
        pytest.skip(f"no harden-repo tree found; set {HARDEN_ROOT_ENV}")
    files = _vendored_files()
    edited = dict(files)
    hook = ".claude/hooks/prevent-direct-push.py"
    edited[hook] = files[hook] + "# local edit\n"
    assert not _vendored_versions(roots, edited)[hook]
    found = _vendored_versions(roots, files)
    assert found[hook], "the control needs the real file to match first"


def test_a_tree_without_the_current_release_fails_not_skips(tmp_path):
    """A harden-repo tree older than the vendored files (a stale plugin
    cache) must fail the generation test's assertions, not skip them. This
    control skips itself when no stale tree exists on this machine (CI, or
    every tree already knows the release)."""
    roots = _harden_roots()
    if not roots:
        pytest.skip(f"no harden-repo tree found; set {HARDEN_ROOT_ENV}")
    files = _vendored_files()
    current = set.intersection(*_vendored_versions(roots, files).values())
    assert current, "the vendored files must match a release first"
    stale = [r for r in roots
             if not set.intersection(*_vendored_versions([r], files).values())]
    if not stale:
        pytest.skip("every harden-repo tree found already knows this release")
    # Call the rule itself: an AssertionError raised from inside could be a
    # skip or a crash in disguise.
    assert _generation_problem(_vendored_versions(stale, files)) is not None


def test_an_unreleased_checkout_does_not_vouch_for_its_templates(tmp_path):
    """A dev checkout's plugin.json names the next version; its templates
    must not credit this repo's files to it. Positive control: the same tree
    marked released does."""
    roots = _harden_roots()
    if not roots:
        pytest.skip(f"no harden-repo tree found; set {HARDEN_ROOT_ENV}")
    real = roots[0][0]
    fake = tmp_path / "harden-fake"
    for rel in ("skills/harden-repo/scripts/doctor_core.py",
                "skills/harden-repo/data/template-ledger.json"):
        (fake / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real / rel, fake / rel)
    (fake / ".claude-plugin").mkdir()
    (fake / ".claude-plugin/plugin.json").write_text(
        json.dumps({"version": "9.9.9"}), encoding="utf-8")
    files = _vendored_files()
    for rel, text in files.items():
        out = fake / VENDORED[rel]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    assert all("9.9.9" not in v for v in _vendored_versions([(fake, False)], files).values())
    assert all("9.9.9" in v for v in _vendored_versions([(fake, True)], files).values())


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


def _write_manifests(root, plugin_version, market_version):
    plugin_dir = root / ".claude-plugin"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "plugin.json").write_text(json.dumps(
        {"name": "probe", "version": plugin_version}), encoding="utf-8")
    (plugin_dir / "marketplace.json").write_text(json.dumps(
        {"plugins": [{"name": "probe", "version": market_version}]}), encoding="utf-8")


def _preflight(repo, *flags):
    for name in ("pre-commit.sh",):
        dest = repo.root / "scripts" / name
        if not dest.exists():
            shutil.copy2(ROOT / "scripts" / name, dest)
    return subprocess.run(
        ["bash", str(repo.root / "scripts" / "commit-preflight.sh"), *flags],
        cwd=repo.root, env=repo.env, capture_output=True, text=True,
        timeout=120, stdin=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("market,expected_rc", [
    pytest.param("1.0.0", 0, id="in-sync-control"),
    pytest.param("0.0.0", 1, id="mismatch"),
])
def test_version_sync_runs_in_the_lint_block(repo, market, expected_rc):
    """Repo part of the preflight, in its LINT marker block since #532: a
    version mismatch fails the run and leaves no token; in sync, the token
    records that the check ran. Skip modes exit before the LINT block;
    tests/test_manifest_version_sync.py covers them in CI."""
    _write_manifests(repo.root, "1.0.0", market)
    _git(repo, "add", ".claude-plugin")
    proc = _preflight(repo)
    assert proc.returncode == expected_rc, proc.stdout[-800:] + proc.stderr[-400:]
    assert repo.token.exists() is (expected_rc == 0), "a failed preflight must leave no token"
    if expected_rc:
        assert "MISMATCH" in proc.stdout
    else:
        checks = json.loads(repo.token.read_text(encoding="utf-8"))["checks_run"]
        assert "version-sync" in checks.split(","), checks


def test_a_malformed_marketplace_fails_the_version_sync(repo):
    """Exit 2 from the check (a structural error) fails the run too."""
    _write_manifests(repo.root, "1.0.0", "1.0.0")
    (repo.root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": "not a list"}), encoding="utf-8")
    _git(repo, "add", ".claude-plugin")
    proc = _preflight(repo)
    assert proc.returncode == 1, proc.stdout[-800:]
    assert "version check failed (exit 2" in proc.stdout
    assert not repo.token.exists()


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
