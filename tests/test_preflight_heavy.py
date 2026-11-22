"""commit-preflight.sh sends every pytest run through `_heavy`, the shared
heavy-run slot (#525, claude-code-config#694).

The checker is a physical-line rule, not a shell parser: every non-comment
line that names a test runner must be one plain `_heavy ...` command or one
of an exact-line allowlist. Mutation tests prove it rejects a real line with
`_heavy` removed. Shell tests run the script's own `_heavy`, `_run_pytest`
and test section with a fresh HOME, so no test reaches the live
~/.claude/bin/heavy, and with stubbed python3/pytest/heavy binaries.

Known limit: a runner name split by quotes (`py'test'`) or built from a
variable (`-m "$MOD"`) is not seen; the exact counts and the allowlist guard
the plain forms.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import site
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT = ROOT / "scripts" / "commit-preflight.sh"

RUNNER_WORDS = (
    "pytest", "py.test", "vitest", "uv run",
    "unittest", "tox", "nox", "npm test", "make test",
)
ALLOWED = (
    "_run_pytest() {",
    'echo "   pytest-xdist found: running tests on $_workers workers"',
    'echo "   pytest rejected -n $_workers (xdist not loaded): running tests serially"',
    'echo "   pytest-xdist not installed: running tests serially"',
    'echo "   (pip install pytest-xdist to run them in parallel)"',
    "if command -v pytest &>/dev/null; then",
    'echo "🧪 Running pytest with coverage..."',
    '_run_pytest tests/ -v --tb=short --cov=hooks --cov-report= --deselect "$_TIMED" || rc=$?',
    'echo "❌ tests/ directory exists but pytest is not installed"',
    'echo "   Install with: pip install pytest pytest-cov"',
)
# `2>&1` is refused on purpose (`&` is an operator; fail-closed). A plain
# `> file` redirect is allowed.
OPERATORS = (";", "&&", "||", "|", "&", "$(", "`", "<(", ">(")
STATUS_SUFFIXES = (" || rc=$?", " || _rc=$?")
GATED_COUNT = 4
CHECKED_COUNT = GATED_COUNT + len(ALLOWED)
HEAVY_START = 'HEAVY_BIN="$HOME/.claude/bin/heavy"'
# harden-repo develop 823346d, templates/scripts/commit-preflight.sh: the
# helper is copied byte for byte, so a local edit must fail here.
HEAVY_BLOCK = '''HEAVY_BIN="$HOME/.claude/bin/heavy"
_heavy() {
    if [ -x "$HEAVY_BIN" ]; then "$HEAVY_BIN" -- "$@"
    else echo "⚠️  heavy not installed ($HEAVY_BIN): running without the shared run slot (#692)" >&2; "$@"; fi
}
'''
PASS_THROUGH = 'shift\nexec "$@"\n'
SLOT_TIMEOUT = "Tests not run: gave up waiting for the heavy-run slot"
TESTS_FAILED = "Tests failed or coverage below 90%"
TIMEOUT = 120


def _mentions_runner(line):
    return any(word in line for word in RUNNER_WORDS)


def _is_gated(line):
    if not line.startswith("_heavy ") or line.endswith("\\"):
        return False
    for suffix in STATUS_SUFFIXES:
        if line.endswith(suffix):
            line = line.removesuffix(suffix)
            break
    return not any(operator in line for operator in OPERATORS)


def scan_script(text: str) -> tuple[list[str], int, int]:
    problems = []
    checked = gated = 0
    for n, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or not _mentions_runner(stripped):
            continue
        checked += 1
        if _is_gated(stripped):
            gated += 1
        elif stripped not in ALLOWED:
            problems.append(f"line {n}: ungated test run: {stripped}")
    return problems, checked, gated


# --- the physical-line checker ---------------------------------------------


def test_real_script_passes_with_expected_counts():
    problems, checked, gated = scan_script(PREFLIGHT.read_text())
    assert problems == []
    assert checked == CHECKED_COUNT
    assert gated == GATED_COUNT


def test_every_allowlisted_line_is_in_the_script():
    """A stale allowlist entry would let that exact line back in unseen."""
    lines = [line.strip() for line in PREFLIGHT.read_text().split("\n")]
    for allowed in ALLOWED:
        assert lines.count(allowed) == 1, allowed


@pytest.mark.parametrize("line", [
    "echo x; python3 -m pytest -q",
    "_heavy true; python3 -m pytest",
    "true && pytest",
    "_heavy true && pytest",
    "_heavy true || python3 -m pytest",
    "_heavy true | pytest",
    "_heavy true & python3 -m pytest",
    "x=$(python3 -m pytest)",
    "x=`pytest -q`",
    '_heavy echo "$(python3 -m pytest)"',
    '_heavy echo "`pytest -q`"',
    "_heavy cat < <(python3 -m pytest)",
    "_heavy cat > >(python3 -m pytest)",
    "_heavy python3 -m pytest \\",
    "then _heavy python3 -m pytest",
    "if _heavy python3 -m pytest; then",
    "py.test -q",
    "npx vitest run",
    "uv run pytest",
    "uv run some-tests",
    "python3 -m unittest discover",
    "tox -e py",
    "nox -s tests",
    "npm test",
    "make test",
    "_heavy python3 -m pytest -q >out 2>&1",
    "command pytest",
    "$PY -m pytest",
    "env -u X python3 -m pytest",
    "python3 -m pytest \"$_TIMED\" -v --tb=short --cov=hooks --cov-append",
    "_heavy python3 -m pytest || rc=$? || true",
    "_heavy python3 -m pytest || _rc=$? || rc=$?",
    'echo "   pytest-xdist found: running tests on $_workers workers"; pytest',
    '_run_pytest tests/ -v --tb=short --cov=hooks --cov-report= --deselect "$_TIMED" && pytest',
    "if command -v pytest &>/dev/null; then pytest; fi",
    " _run_pytest() { pytest; }",
])
def test_rejects_ungated_line(line):
    assert scan_script(line) == ([f"line 1: ungated test run: {line.strip()}"], 1, 0)


def test_blind_spot_split_runner_name_is_not_seen():
    # Documents a known limit: a runner name split by quotes is invisible.
    assert scan_script("python3 -m py'test' -q") == ([], 0, 0)


def test_rejects_ungated_line_after_comment_backslash():
    script = "# comment \\\npython3 -m pytest -q"
    assert scan_script(script) == (["line 2: ungated test run: python3 -m pytest -q"], 1, 0)


@pytest.mark.parametrize("line, checked, gated", [
    ("_heavy python3 -m pytest -q", 1, 1),
    ("_heavy python3 -m pytest -q > out", 1, 1),
    ("_heavy env -u X python3 -m pytest a || rc=$?", 1, 1),
    ('    _heavy python3 -m pytest "$@" || _rc=$?', 1, 1),
    ("# python3 -m pytest only in a comment", 0, 0),
    ("rc=0", 0, 0),
    *((line, 1, 0) for line in ALLOWED),
])
def test_accepts_gated_and_allowlisted_lines(line, checked, gated):
    assert scan_script(line) == ([], checked, gated)


def _gated_line_numbers():
    numbers = [
        n for n, line in enumerate(PREFLIGHT.read_text().split("\n"), start=1)
        if _mentions_runner(line) and _is_gated(line.strip())
    ]
    assert len(numbers) == GATED_COUNT, numbers
    return numbers


@pytest.mark.parametrize("line_number", _gated_line_numbers())
def test_removing_each_real_heavy_prefix_is_rejected(line_number):
    lines = PREFLIGHT.read_text().split("\n")
    line = lines[line_number - 1].replace("_heavy ", "", 1)
    lines[line_number - 1] = line
    problems, checked, gated = scan_script("\n".join(lines))
    assert problems == [f"line {line_number}: ungated test run: {line.strip()}"]
    assert checked == CHECKED_COUNT
    assert gated == GATED_COUNT - 1


def test_appending_a_bare_run_is_rejected():
    text = PREFLIGHT.read_text()
    if not text.endswith("\n"):
        text += "\n"
    line_number = text.count("\n") + 1
    problems, checked, gated = scan_script(text + "python3 -m pytest -q\n")
    assert problems == [f"line {line_number}: ungated test run: python3 -m pytest -q"]
    assert checked == CHECKED_COUNT + 1
    assert gated == GATED_COUNT


def test_the_timed_run_is_gated():
    """The second pass (#509's timed tests) is one of the gated lines."""
    gated = [
        line.strip() for line in PREFLIGHT.read_text().split("\n")
        if _mentions_runner(line) and _is_gated(line.strip())
    ]
    assert any('"$_TIMED"' in line and "--cov-fail-under=90" in line for line in gated), gated


# --- the helper's behaviour -------------------------------------------------


def _through_function(function):
    text = PREFLIGHT.read_text()
    function_start = f"\n{function}() {{\n"
    assert text.count(HEAVY_START) == 1
    assert text.count(function_start) == 1, function
    start = text.index(HEAVY_START)
    end = text.index("\n}\n", text.index(function_start, start)) + 3
    return text[start:end]


def heavy_block():
    return _through_function("_heavy")


def test_heavy_block_is_the_harden_repo_copy():
    assert heavy_block() == HEAVY_BLOCK


@pytest.fixture
def shell_env(tmp_path):
    """A fresh HOME, so `_heavy` never finds the live ~/.claude/bin/heavy.
    PYTHONUSERBASE keeps the real user site, where pytest may live."""
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONUSERBASE"] = site.getuserbase()
    env["PREFLIGHT_WORKERS"] = "2"
    for name in ("BASH_ENV", "ENV", "PYTEST_XDIST_WORKER", "HEAVY_TOKEN"):
        env.pop(name, None)
    return env


def _write_executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _stub_heavy(env, body):
    _write_executable(Path(env["HOME"]) / ".claude" / "bin" / "heavy", body)


def _run(command, cwd, env):
    return subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, stdin=subprocess.DEVNULL,
        encoding="utf-8", errors="replace", timeout=TIMEOUT,
    )


def _bash(script, cwd, env):
    return _run(["bash", "-c", script], cwd, env)


def _warning_count(proc):
    return sum("heavy not installed" in line for line in proc.stderr.splitlines())


def test_heavy_missing_warns_once_and_preserves_status(tmp_path, shell_env):
    proc = _bash(heavy_block() + "_heavy sh -c 'exit 3'; echo rc=$?\n", tmp_path, shell_env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["rc=3"]
    assert len(proc.stderr.splitlines()) == _warning_count(proc) == 1, proc.stderr


def test_heavy_not_executable_counts_as_missing(tmp_path, shell_env):
    _stub_heavy(shell_env, "exit 75\n")
    (Path(shell_env["HOME"]) / ".claude" / "bin" / "heavy").chmod(0o644)
    proc = _bash(heavy_block() + "_heavy sh -c 'exit 0'; echo rc=$?\n", tmp_path, shell_env)
    assert proc.stdout.splitlines() == ["rc=0"], proc.stderr
    assert _warning_count(proc) == 1, proc.stderr


def test_heavy_passes_command_to_stub(tmp_path, shell_env):
    log = tmp_path / "heavy-args"
    shell_env["HEAVY_LOG"] = str(log)
    _stub_heavy(shell_env, 'printf "%s\\n" "$@" > "$HEAVY_LOG"\n' + PASS_THROUGH)
    script = heavy_block() + "_heavy printf '[%s]\\n' \"a b\" c; echo rc=$?\n"
    proc = _bash(script, tmp_path, shell_env)
    assert proc.returncode == 0, proc.stderr
    assert log.read_text().splitlines() == ["--", "printf", "[%s]\\n", "a b", "c"]
    assert proc.stdout.splitlines() == ["[a b]", "[c]", "rc=0"]
    assert _warning_count(proc) == 0, proc.stderr


def test_heavy_stub_status_reaches_the_caller(tmp_path, shell_env):
    _stub_heavy(shell_env, "exit 75\n")
    proc = _bash(heavy_block() + "_heavy true; echo rc=$?\n", tmp_path, shell_env)
    assert proc.stdout.splitlines() == ["rc=75"], proc.stderr


def _stub_python(tmp_path, env, xdist_rc, codes):
    """python3 and pytest stubs on PATH. Each `-m pytest` call is logged and
    exits with the next code from `codes` (0 once they run out)."""
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "pytest-calls"
    args = tmp_path / "pytest-args"
    statuses = tmp_path / "pytest-statuses"
    calls.touch()
    args.touch()
    statuses.write_text("".join(f"{code}\n" for code in codes))
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        XDIST_RC=str(xdist_rc),
        PYTEST_CALLS=str(calls),
        PYTEST_ARGS=str(args),
        PYTEST_CODES=str(statuses),
    )
    _write_executable(bin_dir / "python3", """\
if [ "$1" = -c ]; then
    exit "$XDIST_RC"
fi
printf '%s\\n' "$*" >> "$PYTEST_CALLS"
printf '%s\\n' "$@" >> "$PYTEST_ARGS"
call_number=$(wc -l < "$PYTEST_CALLS")
code=$(sed -n "$((call_number))p" "$PYTEST_CODES")
exit "${code:-0}"
""")
    _write_executable(bin_dir / "pytest", "exit 0\n")
    return calls


TIMEOUT_ON_SECOND = """\
count=0
if [ -f "$HEAVY_COUNT" ]; then
    read -r count < "$HEAVY_COUNT"
fi
count=$((count + 1))
printf '%s\\n' "$count" > "$HEAVY_COUNT"
if [ "$count" -eq 2 ]; then
    exit 75
fi
""" + PASS_THROUGH
PARALLEL = "-m pytest -n 2 tests/ -q"
SERIAL = "-m pytest tests/ -q"


@pytest.mark.parametrize("xdist_rc, codes, heavy, rc, expected_calls", [
    pytest.param(0, (1,), None, 1, [PARALLEL], id="parallel-fail"),
    pytest.param(0, (0,), None, 0, [PARALLEL], id="parallel-pass"),
    pytest.param(0, (4, 0), None, 0, [PARALLEL, SERIAL], id="retry-pass"),
    pytest.param(0, (4, 1), None, 1, [PARALLEL, SERIAL], id="retry-fail"),
    pytest.param(1, (1,), None, 1, [SERIAL], id="serial-fail"),
    pytest.param(1, (0,), None, 0, [SERIAL], id="serial-pass"),
    pytest.param(0, (), "exit 75\n", 75, [], id="parallel-slot-timeout"),
    pytest.param(1, (), "exit 75\n", 75, [], id="serial-slot-timeout"),
    pytest.param(0, (1,), PASS_THROUGH, 1, [PARALLEL], id="held-slot-parallel-fail"),
    pytest.param(1, (1,), PASS_THROUGH, 1, [SERIAL], id="held-slot-serial-fail"),
    pytest.param(0, (4, 0), PASS_THROUGH, 0, [PARALLEL, SERIAL], id="held-slot-retry-pass"),
    pytest.param(0, (4,), TIMEOUT_ON_SECOND, 75, [PARALLEL], id="retry-slot-timeout"),
])
def test_run_pytest_status(tmp_path, shell_env, xdist_rc, codes, heavy, rc, expected_calls):
    calls = _stub_python(tmp_path, shell_env, xdist_rc, codes)
    shell_env["HEAVY_COUNT"] = str(tmp_path / "heavy-count")
    if heavy is not None:
        _stub_heavy(shell_env, heavy)
    script = _through_function("_run_pytest") + '_run_pytest tests/ -q; echo "rc=$?"\n'
    proc = _bash(script, tmp_path, shell_env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines()[-1] == f"rc={rc}", proc.stdout
    assert calls.read_text().splitlines() == expected_calls
    assert _warning_count(proc) == (len(expected_calls) if heavy is None else 0), proc.stderr


def test_one_run_without_heavy_prints_one_warning(tmp_path, shell_env):
    """CI has no ~/.claude/bin/heavy: a test run goes ahead directly and the
    warning is printed once, on stderr."""
    calls = _stub_python(tmp_path, shell_env, 1, (0,))
    script = _through_function("_run_pytest") + '_run_pytest tests/ -q; echo "rc=$?"\n'
    proc = _bash(script, tmp_path, shell_env)
    assert proc.stdout.splitlines()[-1] == "rc=0", proc.stdout
    assert calls.read_text().splitlines() == [SERIAL]
    assert _warning_count(proc) == 1, proc.stderr
    assert "heavy not installed" not in proc.stdout


_TIMED = "tests/test_security.py::TestDecisionTimeIsBounded"
UNTIMED_CALL = f"-m pytest -n 2 tests/ -v --tb=short --cov=hooks --cov-report= --deselect {_TIMED}"
TIMED_CALL = (f"-m pytest {_TIMED} -v --tb=short --cov=hooks --cov-append "
              "--cov-report=term-missing --cov-fail-under=90")


def _test_section():
    text = PREFLIGHT.read_text()
    start = text.index("# __HARDEN_TEST_START__\n")
    end = text.index("# __HARDEN_TEST_END__\n", start)
    return text[start:end]


@pytest.mark.parametrize("codes, heavy, expected_calls, failure", [
    pytest.param((0, 0), None, [UNTIMED_CALL, TIMED_CALL], None, id="both-pass"),
    pytest.param((0, 0), PASS_THROUGH, [UNTIMED_CALL, TIMED_CALL], None, id="held-slot-both-pass"),
    pytest.param((1,), None, [UNTIMED_CALL], TESTS_FAILED, id="untimed-fail"),
    pytest.param((0, 1), None, [UNTIMED_CALL, TIMED_CALL], TESTS_FAILED, id="timed-fail"),
    pytest.param((), "exit 75\n", [], SLOT_TIMEOUT, id="untimed-slot-timeout"),
    pytest.param((0,), TIMEOUT_ON_SECOND, [UNTIMED_CALL], SLOT_TIMEOUT, id="timed-slot-timeout"),
    pytest.param((4,), TIMEOUT_ON_SECOND, [UNTIMED_CALL], SLOT_TIMEOUT, id="retry-slot-timeout"),
])
def test_test_section(tmp_path, shell_env, codes, heavy, expected_calls, failure):
    calls = _stub_python(tmp_path, shell_env, 0, codes)
    count_file = tmp_path / "heavy-count"
    shell_env["HEAVY_COUNT"] = str(count_file)
    if heavy is not None:
        _stub_heavy(shell_env, heavy)
    (tmp_path / "tests").mkdir()
    script = (
        "set -e\n" + _through_function("_run_pytest")
        + "CHECKS_RUN=; CHECKS_PASSED=true\n" + _test_section()
        + 'echo "checks_run=$CHECKS_RUN passed=$CHECKS_PASSED"\n'
    )
    proc = _bash(script, tmp_path, shell_env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    expected = "checks_run=tests, passed=true" if failure is None else "checks_run= passed=false"
    assert proc.stdout.splitlines()[-1] == expected, proc.stdout
    assert (TESTS_FAILED in proc.stdout) == (failure == TESTS_FAILED), proc.stdout
    assert (SLOT_TIMEOUT in proc.stdout) == (failure == SLOT_TIMEOUT), proc.stdout
    assert calls.read_text().splitlines() == expected_calls
    assert _warning_count(proc) == (len(expected_calls) if heavy is None else 0), proc.stderr
    if heavy == TIMEOUT_ON_SECOND:
        assert count_file.read_text() == "2\n"


def test_run_pytest_runs_real_pytest_without_heavy(tmp_path, shell_env):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n")
    (tests / "test_one.py").write_text(
        "from pathlib import Path\n\n"
        "def test_one():\n"
        "    Path('pytest-ran').write_text('ran')\n"
    )
    for name in ("PYTEST_ADDOPTS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_PLUGINS"):
        shell_env.pop(name, None)
    script = _through_function("_run_pytest") + "_run_pytest tests/ -q -p no:cacheprovider\n"
    proc = _bash(script, root, shell_env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (root / "pytest-ran").read_text() == "ran"
    assert _warning_count(proc) == 1, proc.stderr


# --- no test reaches the live heavy -----------------------------------------


# tests/conftest.py wraps subprocess.Popen: running the real
# commit-preflight.sh with the developer's HOME raises (#525). These tests
# prove that guard works. The argv never starts a process.
def _real_home():
    return pwd.getpwuid(os.getuid()).pw_dir


def test_guard_rejects_preflight_with_real_home_in_env():
    with pytest.raises(AssertionError, match=r"\(#525\)"):
        subprocess.run(["true", "x/commit-preflight.sh"],
                       env={**os.environ, "HOME": _real_home()})


def test_guard_rejects_preflight_with_real_home_from_environ(monkeypatch):
    monkeypatch.setenv("HOME", _real_home())
    with pytest.raises(AssertionError, match=r"\(#525\)"):
        subprocess.run(["true", "x/commit-preflight.sh"])


def test_guard_allows_preflight_with_tmp_home(tmp_path):
    proc = subprocess.run(["true", "scripts/commit-preflight.sh"],
                          env={**os.environ, "HOME": str(tmp_path)})
    assert proc.returncode == 0


def test_guard_ignores_other_commands_with_real_home():
    proc = subprocess.run(["true", "scripts/other.sh"],
                          env={**os.environ, "HOME": _real_home()})
    assert proc.returncode == 0


def test_guard_keeps_iterator_argv_working():
    assert subprocess.run(iter(["true"])).returncode == 0


def test_guard_rejects_bash_c_string_naming_preflight():
    with pytest.raises(AssertionError, match=r"\(#525\)"):
        subprocess.run(["bash", "-c", "bash scripts/commit-preflight.sh --auto"],
                       env={**os.environ, "HOME": _real_home()})


def test_guard_rejects_shell_true_string_naming_preflight():
    with pytest.raises(AssertionError, match=r"\(#525\)"):
        subprocess.run("bash scripts/commit-preflight.sh", shell=True,
                       env={**os.environ, "HOME": _real_home()})


def test_guard_reads_home_from_bytes_env_keys():
    env = {k.encode(): v.encode() for k, v in os.environ.items() if k != "HOME"}
    env[b"HOME"] = os.fsencode(_real_home())
    with pytest.raises(AssertionError, match=r"\(#525\)"):
        subprocess.run(["true", "x/commit-preflight.sh"], env=env)


def test_guard_rejects_script_path_as_argv0():
    with pytest.raises(AssertionError, match=r"\(#525\)"):
        subprocess.run([str(PREFLIGHT), "--help"],
                       env={**os.environ, "HOME": _real_home()})


def _git(*args, cwd, env):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.invalid",
                    "-c", "user.name=t", *args],
                   check=True, capture_output=True, env=env)


def _token_path(root, env):
    common = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True, env=env).stdout.strip()
    digest = hashlib.md5(os.path.realpath(common).encode()).hexdigest()[:8]
    return Path(f"/tmp/.preflight-token-{digest}")


def test_slot_timeout_fails_the_real_preflight_and_leaves_no_token(tmp_path, shell_env):
    """End to end: with heavy giving up (75), the whole script fails, says
    so, and writes no token."""
    env = {k: v for k, v in shell_env.items()
           if not k.startswith(("GIT_", "BASH_FUNC_"))
           and k not in {"CLAUDE_PLUGIN_ROOT", "HARDEN_HOOK_DEADLINE", "ASSERT_GATE"}}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    _stub_heavy(env, "exit 75\n")
    root = tmp_path / "target"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    _git("init", "-q", "-b", "feature/probe", cwd=root, env=env)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root, env=env)
    for name in ("commit-preflight.sh", "pre-commit.sh"):
        dest = root / "scripts" / name
        shutil.copy(ROOT / "scripts" / name, dest)
        dest.chmod(0o755)
    (root / "tests" / "test_one.py").write_text("def test_one():\n    assert 1 + 1 == 2\n")
    _git("add", "-A", cwd=root, env=env)
    token = _token_path(root, env)
    token.unlink(missing_ok=True)
    try:
        proc = _run(["bash", "scripts/commit-preflight.sh"], root, env)
        output = proc.stdout + proc.stderr
        assert proc.returncode != 0, output
        assert SLOT_TIMEOUT in output, output
        assert not token.exists(), output
        # No other failing check may stand in for the slot timeout.
        failures = [line for line in output.splitlines() if "❌" in line]
        assert len(failures) == 2, output
        assert SLOT_TIMEOUT in failures[0], output
        assert "PREFLIGHT FAILED" in failures[1], output
    finally:
        token.unlink(missing_ok=True)
