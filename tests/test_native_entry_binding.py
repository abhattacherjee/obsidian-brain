"""Native entry binding checks; these do not certify Desktop dispatch."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clear_launcher_declaration(monkeypatch):
    monkeypatch.delenv("OB_CLIENT", raising=False)


def entry():
    spec = importlib.util.spec_from_file_location("brain_native_entry_test", ROOT / "hooks" / "native_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("client", ["codex-cli", "codex-desktop"])
def test_explicit_current_client_reaches_adjacent_runtime(monkeypatch, client):
    module = entry()
    observed = []
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=lambda args, **kwargs: observed.append((args, kwargs))))
    assert module.run(["--host", "codex", "--client", client, "--event", "stop"], started_at=123.0) == 0
    args, kwargs = observed[0]
    assert args == ["--resource-root", str(ROOT), "--host", "codex", "--client", client, "--event", "stop", "hook"]
    assert kwargs == {"started_at": 123.0}


def test_unknown_codex_frontend_does_not_guess_from_origin_or_environment(monkeypatch, capsys):
    module = entry()
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "Codex Desktop")
    monkeypatch.setenv("CODEX_THREAD_ID", "native-session")
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=lambda *args, **kwargs: pytest.fail("Unresolved client reached runtime")))
    assert module.run(["--host", "codex", "--event", "session_start"]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "client identity is unavailable" in output.err


@pytest.mark.parametrize("host,client", [
    ("codex", "claude-code"), ("claude", "codex-cli"),
    ("codex", "cli"), ("codex", "Codex Desktop"), ("unknown", "codex-cli"),
])
def test_invalid_native_binding_never_enters_runtime(monkeypatch, capsys, host, client):
    module = entry()
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(
        main=lambda *args, **kwargs: pytest.fail("Invalid client reached runtime")))
    assert module.run(["--host", host, "--client", client]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "identity is invalid" in output.err


@pytest.mark.parametrize("host,client,declared", [
    ("codex", "codex-cli", value) for value in ("", "codex-desktop", "claude-code", "unknown")
] + [("claude", "claude-code", value) for value in ("", "codex-cli", "codex-desktop", "unknown")])
def test_conflicting_launcher_declaration_skips_without_runtime(monkeypatch, capsys, host, client, declared):
    module = entry()
    monkeypatch.setenv("OB_CLIENT", declared)
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(
        main=lambda *args, **kwargs: pytest.fail("Conflicting declaration reached runtime")))
    assert module.run(["--host", host, "--client", client]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "declaration conflicts" in output.err


@pytest.mark.parametrize("event", ["SessionStart", "Stop", "PreCompact", "SessionEnd"])
@pytest.mark.parametrize("declared", [None, "codex-cli", "codex-desktop", "invalid; echo injected"])
def test_registered_shell_command_passes_launcher_value_as_one_argument(
        tmp_path, monkeypatch, capsys, event, declared):
    # Observe real shell expansion, including a loaded resource path with spaces.
    # This is a command contract check, not evidence of native host dispatch.
    resource = tmp_path / "installed package with spaces"
    hooks = resource / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "native_entry.py").write_text(
        "import json, sys; print(json.dumps(sys.argv[1:]))\n")
    definition = json.loads((ROOT / "hooks/codex-hooks.json").read_text())
    command = definition["hooks"][event][0]["hooks"][0]["command"]
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(resource)}
    env.pop("OB_CLIENT", None)
    if declared is not None:
        env["OB_CLIENT"] = declared
    result = subprocess.run(command, shell=True, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    arguments = json.loads(result.stdout)
    assert arguments[arguments.index("--client") + 1] == (declared or "")
    observed = []
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(
        main=lambda *args, **kwargs: observed.append(args)))
    if declared is not None:
        monkeypatch.setenv("OB_CLIENT", declared)
    module = entry()
    monkeypatch.setattr(module, "isolated_cli_ancestor", lambda: True)
    assert module.run(arguments) == 0
    output = capsys.readouterr()
    assert output.out == ""
    if declared == "codex-cli":
        assert len(observed) == 1
        assert output.err == ""
    else:
        assert observed == []
        assert "no source was retained" in output.err


@pytest.mark.parametrize("chain,allowed", [
    ([(2, []), (1, ["/opt/codex", "--no-daemon"])], True),
    ([(2, ["/opt/codex"]), (1, [])], False),
    ([(2, ["/opt/codex", "app-server", "--daemon"]), (1, ["/opt/codex", "--no-daemon"])], False),
    ([(2, ["/opt/codex", "--no-daemon"]), (1, ["/opt/codex", "app-server", "--daemon"])], False),
    ([(1, [])], False),
    ([(1, ["/opt/codex", "--", "--no-daemon"])], False),
    ([(1, ["/opt/codex", "say", "--no-daemon"])], False),
    ([(1, ["/opt/codex", "--model", "--no-daemon"])], False),
    ([(1, ["/opt/codex", "-c", "instructions=be --no-daemon"])], False),
    ([(1, ["/opt/codex", "--no-daemon", "what's wrong"])], True),
    ([(1, ["/opt/codex", "--sandbox", "workspace-write", "--no-daemon", "resume", "thread"])], True),
])
def test_launcher_ancestry_only_permits_isolated_codex(monkeypatch, chain, allowed):
    module = entry()
    monkeypatch.setattr(module.os, "getppid", lambda: 3)
    rows = iter(chain)
    monkeypatch.setattr(module, "process_ancestor", lambda *args: next(rows))
    assert module.isolated_cli_ancestor() is allowed


def test_unreadable_launcher_ancestry_fails_closed(monkeypatch):
    module = entry()
    def unavailable(*args):
        raise OSError("private diagnostic")
    monkeypatch.setattr(module, "process_ancestor", unavailable)
    assert module.isolated_cli_ancestor() is False


def test_linux_cross_uid_non_codex_ancestor_never_reads_protected_exe(monkeypatch):
    module = entry()
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"42 (sshd: user [priv]) S 1 0 0\n")
    def protected(path):
        pytest.fail("An SSH ancestor's protected exe/cmdline must not be read")
    monkeypatch.setattr(Path, "readlink", protected)
    monkeypatch.setattr(Path, "open", protected)
    assert module.process_ancestor(42, module.time.monotonic() + 1) == (1, [])


@pytest.mark.parametrize("returned,pid_matches", [(64, True), (0, True), (32, True), (64, False)])
def test_macos_minimal_metadata_abi_and_refusal(monkeypatch, returned, pid_matches):
    import ctypes
    import struct
    class Reader:
        def __call__(self, pid, flavor, unused, buffer, size):
            assert flavor == 13 and size == 64
            payload = bytearray(64)
            struct.pack_into("IIII", payload, 0, pid if pid_matches else pid + 1, 1, 0, 0)
            payload[16:20] = b"sshd"
            ctypes.memmove(buffer, bytes(payload), 64)
            return returned
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(proc_pidinfo=Reader()))
    if returned == 64 and pid_matches:
        assert entry().macos_process_parent(42) == (1, "sshd")
    else:
        with pytest.raises(OSError, match="metadata is unavailable"):
            entry().macos_process_parent(42)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS root-owned launchd metadata")
def test_macos_root_owned_launchd_metadata_is_readable():
    assert entry().macos_process_parent(1) == (0, "launchd")


@pytest.mark.parametrize("launcher_args,allowed", [
    (["--no-daemon", "what's wrong"], True),
    (["-c", "instructions=be --no-daemon"], False),
])
@pytest.mark.parametrize("sandboxed", [False, pytest.param(True, marks=pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS seatbelt control"))])
def test_real_private_process_preserves_launcher_argument_boundaries(tmp_path, launcher_args, allowed, sandboxed):
    import shutil
    executable = tmp_path / "codex"
    python_binary = Path(sys.executable).resolve()
    framework_binary = python_binary.parents[1] / "Resources/Python.app/Contents/MacOS/Python"
    if sys.platform == "darwin" and framework_binary.is_file():
        # CLT Python loads its library relative to this framework layout.
        # Keep that layout private and link only the installed read-only library.
        framework_version = python_binary.parents[1]
        executable = tmp_path / "framework/Resources/Python.app/Contents/MacOS/codex"
        executable.parent.mkdir(parents=True)
        for library_name in ("Python", "Python3"):
            library = framework_version / library_name
            if library.is_file():
                (tmp_path / "framework" / library_name).symlink_to(library)
        python_binary = framework_binary
    shutil.copy2(python_binary, executable)
    if sys.platform == "darwin":
        signed = subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(executable)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        assert signed.returncode == 0, signed.stderr
    # A harmless copied Python executable supplies actual argv. The guard reads
    # the real parent process; the ancestry boundary stops above this fixture,
    # rather than including whichever native app happens to run the test suite.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import importlib.util, sys\n"
        "spec=importlib.util.spec_from_file_location('entry',sys.argv[1])\n"
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
        "if sys.platform=='darwin': assert module.macos_process_parent(1)==(0,'launchd')\n"
        "original=module.process_ancestor\n"
        "def selected(pid, deadline):\n"
        "    parent, argv=original(pid, deadline)\n"
        "    return (1 if argv else parent), argv\n"
        "module.process_ancestor=selected\n"
        "print(module.isolated_cli_ancestor())\n")
    child_code = (
        "import os, sys; "
        "os.spawnv(os.P_WAIT, sys.executable, "
        + repr([str(executable), str(probe), str(ROOT / "hooks/native_entry.py")]) + ")")
    command = [sys.executable, "-c", "import os, sys; os.execv(sys.argv[1], sys.argv[1:])",
               str(executable), "-c", child_code, *launcher_args]
    if sandboxed:
        command = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", *command]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(allowed), result.stderr


@pytest.mark.parametrize("client,isolated", [("codex-cli", False), ("codex-desktop", True)])
def test_registered_capture_refuses_unsupported_launcher(monkeypatch, capsys, client, isolated):
    module = entry()
    monkeypatch.setattr(module, "isolated_cli_ancestor", lambda: isolated)
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(
        main=lambda *args, **kwargs: pytest.fail("Unsupported launcher reached runtime")))
    assert module.run(["--host", "codex", "--client", client, "--require-cli-launcher"]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert "shared-server and Desktop capture are unsupported" in output.err


@pytest.mark.parametrize("override", [["--resource-root", "/unrelated"], ["--resource-root=/unrelated"]])
def test_native_entry_refuses_another_resource_tree(monkeypatch, capsys, override):
    module = entry()
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=lambda *args, **kwargs: pytest.fail("Foreign tree reached runtime")))
    assert module.run(["--host", "claude", "--client", "claude-code", *override]) == 0
    assert "resource root must be adjacent" in capsys.readouterr().err


def test_import_or_runtime_failure_remains_fail_open(monkeypatch, capsys):
    module = entry()
    def fail(*args, **kwargs):
        raise RuntimeError("Sensitive diagnostic must not be echoed")
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=fail))
    assert module.run(["--host", "claude", "--client", "claude-code", "--event", "stop"]) == 0
    error = capsys.readouterr().err
    assert "capture was skipped; no source was retained" in error
    assert "Sensitive" not in error


@pytest.mark.parametrize("arguments", [["--host"], ["--client"]])
def test_malformed_binding_fails_open_without_entering_runtime(monkeypatch, capsys, arguments):
    module = entry()
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(main=lambda *args, **kwargs: pytest.fail("Malformed binding reached runtime")))
    assert module.run(arguments) == 0
    assert "arguments are invalid" in capsys.readouterr().err


def test_codex_sessionend_descriptor_grants_supported_three_second_budget():
    import json
    definition = json.loads((ROOT/'hooks/codex-hooks.json').read_text())
    handlers = [handler for group in definition['hooks']['SessionEnd'] for handler in group['hooks']]
    assert handlers and all(handler['timeout'] == 3 for handler in handlers)
    assert all('--client codex-cli' not in handler['command'] for handler in handlers)


@pytest.mark.parametrize("declared", [None, "claude-code"])
def test_claude_matching_or_unset_declaration_enters_runtime(monkeypatch, declared):
    module = entry()
    if declared is not None:
        monkeypatch.setenv("OB_CLIENT", declared)
    observed = []
    monkeypatch.setitem(sys.modules, "brain_cli", SimpleNamespace(
        main=lambda *args, **kwargs: observed.append(args)))
    assert module.run(["--host", "claude", "--client", "claude-code", "--event", "stop"]) == 0
    assert len(observed) == 1
