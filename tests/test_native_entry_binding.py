"""Native entry binding checks; these do not certify Desktop dispatch."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


def desktop_module():
    spec = importlib.util.spec_from_file_location("brain_desktop_binding_test", ROOT / "hooks" / "desktop_binding.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DesktopMetadata:
    """Only the OS metadata boundary is replaced; verification stays real."""

    def __init__(self):
        self.identities = {
            20: (20, 10, 501, 2_000_000, "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            10: (10, 1, 501, 1_000_000, "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"),
        }
        self.arguments = ["codex", "-c", "features.code_mode_host=true", "app-server", "--analytics-default-enabled"]
        self.endpoints = {
            20: [dict(fd=0, handle=100, peer=200, shared=False, anonymous=True, listener=False),
                 dict(fd=1, handle=101, peer=201, shared=False, anonymous=True, listener=False)],
            10: [dict(fd=175, handle=200, peer=100, shared=False, anonymous=True, listener=False),
                 dict(fd=177, handle=201, peer=101, shared=False, anonymous=True, listener=False)],
        }

    def identity(self, pid):
        return self.identities[pid]

    def argv(self, pid):
        assert pid == 20
        return self.arguments

    def sockets(self, pid):
        return self.endpoints[pid]

    def signature(self, pid):
        return "54ec7f8749a10262f4f06fbb66688c3a1f78137b" if pid == 10 else "9968cf1ea4020a7730965d776bd02cdc2ce4544d"


def test_desktop_transport_requires_actual_reciprocal_private_endpoints():
    proof = desktop_module().collect_transport(DesktopMetadata(), 20)
    assert proof["server"][0:4] == (20, 10, 501, 2_000_000)
    assert proof["desktop"][0:4] == (10, 1, 501, 1_000_000)
    assert proof["pairs"] == [(0, 175, 100, 200), (1, 177, 101, 201)]


@pytest.mark.parametrize("failure", ["wrong-peer", "shared", "named", "listener", "duplicate-peer", "wrong-parent", "wrong-signature", "socket-transport", "proxy", "nearer-cli"])
def test_desktop_transport_refuses_ambiguous_or_foreign_ingress(failure):
    reader = DesktopMetadata()
    if failure == "wrong-peer":
        reader.endpoints[10][0]["peer"] = 999
    elif failure == "shared":
        reader.endpoints[20][0]["shared"] = True
    elif failure == "named":
        reader.endpoints[20][0]["anonymous"] = False
    elif failure == "listener":
        reader.endpoints[20].append(dict(fd=7, handle=300, peer=0, shared=False, anonymous=False, listener=True))
    elif failure == "duplicate-peer":
        reader.endpoints[10].append(dict(reader.endpoints[10][0], fd=178))
    elif failure == "wrong-parent":
        reader.identities[10] = (10, 1, 501, 1_000_000, "/tmp/ChatGPT")
    elif failure == "wrong-signature":
        reader.signature = lambda pid: "forged"
    elif failure == "socket-transport":
        reader.arguments += ["--listen", "unix://"]
    elif failure == "proxy":
        reader.arguments = ["codex", "app-server", "proxy"]
    else:
        reader.arguments = ["codex", "--no-daemon"]
    with pytest.raises((OSError, ValueError)):
        desktop_module().collect_transport(reader, 20)


@pytest.mark.parametrize('verified', [False, True])
def test_registered_desktop_dispatch_requires_live_protected_proof(monkeypatch, capsys, verified):
    module = entry()
    observed = []
    monkeypatch.setattr(module, 'isolated_desktop_ancestor', lambda: verified, raising=False)
    monkeypatch.setitem(sys.modules, 'brain_cli', SimpleNamespace(main=lambda args, **kwargs: observed.append(args)))
    assert module.run(['--host', 'codex', '--client', 'codex-desktop',
                       '--require-current-launcher', '--event', 'stop']) == 0
    if verified:
        assert observed == [['--resource-root', str(ROOT), '--host', 'codex', '--client',
                             'codex-desktop', '--event', 'stop', 'hook']]
    else:
        assert observed == []
        assert 'no source was retained' in capsys.readouterr().err


@pytest.fixture
def protected_desktop(monkeypatch):
    module = desktop_module()
    metadata = DesktopMetadata()
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(module.os, 'getuid', lambda: 501)
    monkeypatch.setattr(module, 'MacReader', lambda: metadata)
    monkeypatch.setattr(module, 'forced_policy', lambda: 'os-forced-false')
    monkeypatch.setattr(module, '_boot_id', lambda: 'boot-1')
    monkeypatch.setattr(module, '_file_identity', lambda path: [1, 2, 3, 4, 5])
    receipt = {
        'schema': 1, 'uid': 501, 'boot': 'boot-1', 'policy': 'os-forced-false',
        'server': list(metadata.identities[20]), 'desktop': list(metadata.identities[10]),
        'pairs': [[0, 175, 100, 200], [1, 177, 101, 201]],
        'signatures': ['54ec7f8749a10262f4f06fbb66688c3a1f78137b', '9968cf1ea4020a7730965d776bd02cdc2ce4544d'],
        'observed_before_us': 500000,
        'observer_sha256': __import__('hashlib').sha256((ROOT / 'hooks/desktop_binding.py').read_bytes()).hexdigest(),
        'files': {path: [1, 2, 3, 4, 5] for path in module.PINS},
    }
    monkeypatch.setattr(module, '_read_receipt', lambda uid: receipt)
    return module, metadata, receipt


def test_desktop_admission_requires_prebirth_attestation_and_live_transport(protected_desktop):
    module, _, _ = protected_desktop
    assert module.verified_desktop_ancestor(20) is True


@pytest.mark.parametrize('failure', ['user', 'boot', 'policy', 'postbirth', 'pid-reuse', 'file-change', 'observer-change', 'nearer-cli'])
def test_desktop_admission_refuses_changed_protected_or_live_proof(protected_desktop, failure):
    module, metadata, receipt = protected_desktop
    if failure == 'user':
        receipt['uid'] = 502
    elif failure == 'boot':
        receipt['boot'] = 'earlier-boot'
    elif failure == 'policy':
        receipt['policy'] = 'not-forced'
    elif failure == 'postbirth':
        receipt['observed_before_us'] = 1500000
    elif failure == 'pid-reuse':
        receipt['server'][3] = 1999999
    elif failure == 'file-change':
        receipt['files'][module.ASAR][-1] = 6
    elif failure == 'observer-change':
        receipt['observer_sha256'] = '0' * 64
    else:
        metadata.identities[30] = (30, 20, 501, 3000000, module.SERVER)
        metadata.argv = lambda pid: ['codex', '--no-daemon']
    assert module.verified_desktop_ancestor(30 if failure == 'nearer-cli' else 20) is False


def test_desktop_attestation_rejects_user_owned_file_and_symlink(tmp_path, monkeypatch):
    module = desktop_module()
    target = tmp_path / 'root-looking.json'
    target.write_text('{"schema":1}')
    target.chmod(0o444)
    monkeypatch.setattr(module, '_receipt_path', lambda uid: target)
    monkeypatch.setattr(module, '_private_directory', lambda path, uid: (1, 2))
    with pytest.raises(ValueError, match='root-owned'):
        module._read_receipt(501)
    link = tmp_path / 'link.json'
    link.symlink_to(target)
    monkeypatch.setattr(module, '_receipt_path', lambda uid: link)
    with pytest.raises(OSError):
        module._read_receipt(501)


def test_desktop_profile_forces_only_remote_denial_without_enrollment():
    module = desktop_module()
    profile = module.profile_payload()
    assert profile['PayloadScope'] == 'System'
    assert [item['PayloadType'] for item in profile['PayloadContent']] == ['com.apple.ManagedClient.preferences']
    preferences = profile['PayloadContent'][0]['PayloadContent']
    assert set(preferences) == {'com.openai.codex'}
    import base64
    encoded = preferences['com.openai.codex']['Forced'][0]['mcx_preference_settings']
    assert set(encoded) == {'requirements_toml_base64'}
    assert base64.b64decode(encoded['requirements_toml_base64']) == b'allow_remote_control = false\n'


def test_desktop_profile_generator_is_private_create_only(tmp_path, monkeypatch):
    import plistlib
    module = desktop_module()
    tmp_path.chmod(0o700)
    target = tmp_path / 'desktop.mobileconfig'
    monkeypatch.setattr(sys, 'argv', ['desktop_binding.py', '--profile-output', str(target)])
    assert module.main() == 0
    saved = target.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    assert plistlib.loads(saved)['PayloadScope'] == 'System'
    assert module.main() == 1
    assert target.read_bytes() == saved


def test_desktop_profile_generator_refuses_public_directory(tmp_path, monkeypatch):
    module = desktop_module()
    tmp_path.chmod(0o755)
    target = tmp_path / 'desktop.mobileconfig'
    monkeypatch.setattr(sys, 'argv', ['desktop_binding.py', '--profile-output', str(target)])
    assert module.main() == 1
    assert not target.exists()


def test_desktop_root_observer_refuses_unprivileged_execution(monkeypatch):
    module = desktop_module()
    monkeypatch.setattr(module.os, 'geteuid', lambda: 501)
    with pytest.raises(ValueError, match='root-staged'):
        module.observe_new_desktop(180)


@pytest.mark.parametrize('failure', [None, 'old-desktop', 'changed-policy', 'changed-code'])
def test_desktop_observer_mints_only_new_live_proof_after_forced_policy(tmp_path, monkeypatch, failure):
    module = desktop_module()
    reader = DesktopMetadata()
    reader.signature = lambda pid, **kwargs: ('54ec7f8749a10262f4f06fbb66688c3a1f78137b' if pid == 10
                                             else '9968cf1ea4020a7730965d776bd02cdc2ce4544d')
    def inventory(kind, uid, pids, size):
        assert (kind, uid) == (4, 501)
        pids[0], pids[1] = 10, 20
        return 8
    reader.lib = SimpleNamespace(proc_listpids=inventory)
    monkeypatch.setattr(module, '_observer_is_protected', lambda: None)
    monkeypatch.setattr(module, 'forced_policy', lambda: 'forced-false')
    queries = iter(['forced-false', 'changed' if failure == 'changed-policy' else 'forced-false', 'forced-false'])
    monkeypatch.setattr(module, '_user_policy', lambda uid: next(queries))
    monkeypatch.setattr(module, '_boot_id', lambda: 'boot-1')
    monkeypatch.setattr(module, '_pin_files', lambda: {path: [1, 2, 3, 4, 5] for path in module.PINS})
    monkeypatch.setattr(module, '_file_identity', lambda path: [1, 2, 3, 4, 6 if failure == 'changed-code' else 5])
    monkeypatch.setattr(module, 'MacReader', lambda: reader)
    real_stat = module.os.stat
    monkeypatch.setattr(module.os, 'stat', lambda path, **kwargs: SimpleNamespace(st_uid=501)
                        if str(path) == '/dev/console' else real_stat(path, **kwargs))
    monkeypatch.setattr(module.time, 'time_ns', lambda: 500_000_000)
    ticks = iter([0, 0, 0.1, 2, 2])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    minted = tmp_path / 'receipt.json'
    def publish(uid, receipt):
        assert uid == 501
        minted.write_text(json.dumps(receipt))
        return minted
    monkeypatch.setattr(module, '_write_receipt', publish)
    if failure == 'old-desktop':
        reader.identities[10] = (10, 1, 501, 400000, reader.identities[10][4])
    if failure:
        with pytest.raises(ValueError):
            module.observe_new_desktop(1)
        assert not minted.exists()
    else:
        assert module.observe_new_desktop(1) == 0
        receipt = json.loads(minted.read_bytes())
        assert receipt['observed_before_us'] == 500000
        assert receipt['desktop'][3] == 1000000 and receipt['server'][3] == 2000000
        assert receipt['pairs'] == [[0, 175, 100, 200], [1, 177, 101, 201]]
        assert receipt['policy'] == 'forced-false'


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


def test_timing_opt_in_forwards_actual_wrapper_entry_without_changing_budget(monkeypatch):
    module = entry()
    observed = []
    monkeypatch.setenv('OB_NATIVE_DIAGNOSTIC_TIMING', '1')
    monkeypatch.setitem(sys.modules, 'brain_cli', SimpleNamespace(
        main=lambda args, **kwargs: observed.append(kwargs)))
    assert module.run(['--host', 'claude', '--client', 'claude-code', '--event', 'session_end'],
                      started_at=123.0) == 0
    assert observed == [{'started_at': 123.0, 'entry_monotonic_ns': module.ENTRY_MONOTONIC_NS}]


@pytest.mark.parametrize('hostile', [None, 'wrong-digest', 'wrong-size', 'symlink', 'hardlink'])
def test_desktop_asset_pins_hash_real_files_and_refuse_hostile_identity(tmp_path, monkeypatch, hostile):
    module = desktop_module()
    asset = tmp_path / 'asset'
    asset.write_bytes(b'public synthetic Desktop code')
    expected = hashlib.sha256(asset.read_bytes()).hexdigest()
    size = asset.stat().st_size
    if hostile == 'symlink':
        asset.rename(tmp_path / 'original')
        asset.symlink_to(tmp_path / 'original')
    elif hostile == 'hardlink':
        os.link(asset, tmp_path / 'alias')
    monkeypatch.setattr(module, 'PINS', {str(asset): ('0' * 64 if hostile == 'wrong-digest' else expected, None)})
    monkeypatch.setattr(module, 'FILE_SIZES', {str(asset): size + (1 if hostile == 'wrong-size' else 0)})
    if hostile:
        with pytest.raises(ValueError):
            module._pin_files()
    else:
        assert module._pin_files() == {str(asset): module._file_identity(asset)}


@pytest.fixture
def protected_desktop_filesystem():
    import pwd
    import stat
    import tempfile
    # The privileged writer rejects /tmp even when the leaf itself is private.
    # Use the real account home, not HOME or pytest's platform temp root.
    base = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    for path in [base, *base.parents]:
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022
        assert info.st_uid in {0, os.getuid()}
    with tempfile.TemporaryDirectory(prefix='.obp-desktop-test-', dir=base) as directory:
        yield Path(directory)


@pytest.mark.parametrize('hostile', [None, 'symlink-parent', 'writable-parent', 'existing-user-file', 'oversize'])
def test_desktop_attestation_publication_uses_real_guarded_filesystem(protected_desktop_filesystem, monkeypatch, hostile):
    module = desktop_module()
    parent = protected_desktop_filesystem / 'attestation'
    if hostile == 'symlink-parent':
        target = protected_desktop_filesystem / 'elsewhere'
        target.mkdir(mode=0o700)
        parent.symlink_to(target, target_is_directory=True)
    elif hostile == 'writable-parent':
        parent.mkdir(mode=0o700)
        parent.chmod(0o777)
    else:
        parent.mkdir(mode=0o700)
    destination = parent / 'binding.json'
    monkeypatch.setattr(module, '_receipt_path', lambda uid: destination)
    # Privilege admission is checked separately. Exercise actual fd-relative
    # publication as the fixture owner, without root or a real attestation.
    if hostile == 'existing-user-file':
        destination.write_text('sentinel')
        destination.chmod(0o600)
    receipt = {'schema': 1, 'synthetic': 'x' * 33000 if hostile == 'oversize' else 'public-proof'}
    if hostile:
        if hostile == 'symlink-parent':
            import errno
            with pytest.raises(OSError) as error:
                module._write_receipt(os.getuid(), receipt)
            assert error.value.errno in {errno.ELOOP, errno.ENOTDIR}
        else:
            reason = {'writable-parent': 'Desktop proof parent is unsafe',
                      'existing-user-file': 'Existing Desktop attestation is unsafe',
                      'oversize': 'Desktop attestation exceeds its bound'}[hostile]
            with pytest.raises(ValueError, match=reason):
                module._write_receipt(os.getuid(), receipt)
        if hostile == 'existing-user-file':
            assert destination.read_text() == 'sentinel'
        else:
            assert not destination.exists()
    else:
        assert module._write_receipt(os.getuid(), receipt) == destination
        assert json.loads(destination.read_bytes()) == receipt
        assert destination.stat().st_mode & 0o777 == 0o444
        assert destination.stat().st_nlink == 1
    if not parent.is_symlink():
        assert not list(parent.glob('.binding-*'))


@pytest.mark.parametrize('failure', [None, 'short', 'reused-pid', 'missing-executable'])
def test_desktop_process_collector_requires_complete_identity_bytes(monkeypatch, failure):
    import struct
    module = desktop_module()
    reader = module.MacReader.__new__(module.MacReader)
    def pidinfo(pid, flavor, offset, buffer, size):
        assert (pid, flavor, size) == (42, 3, 136)
        struct.pack_into('III', buffer, 12, 43 if failure == 'reused-pid' else 42, 10, 501)
        struct.pack_into('QQ', buffer, 120, 123, 456)
        return 135 if failure == 'short' else 136
    def pidpath(pid, buffer, size):
        buffer.value = b'/reviewed/codex'
        return 0 if failure == 'missing-executable' else len(buffer.value)
    reader.lib = SimpleNamespace(proc_pidinfo=pidinfo, proc_pidpath=pidpath)
    if failure:
        with pytest.raises(OSError):
            reader.identity(42)
    else:
        assert reader.identity(42) == (42, 10, 501, 123000456, '/reviewed/codex')


@pytest.mark.parametrize('failure', [None, 'oversize', 'zero-argc', 'unterminated'])
def test_desktop_argv_collector_obeys_argc_and_never_returns_environment(monkeypatch, failure):
    import struct
    module = desktop_module()
    raw = struct.pack('i', 0 if failure == 'zero-argc' else 3) + b'/reviewed/codex\0\0codex\0app-server\0--listen=stdio://'
    if failure != 'unterminated':
        raw += b'\0PRIVATE_ENV_NOT_AN_ARGUMENT=synthetic\0'
    def sysctl(mib, count, buffer, size, new, newlen):
        if buffer is None:
            size._obj.value = 1_000_001 if failure == 'oversize' else len(raw)
        else:
            module.ctypes.memmove(buffer, raw, len(raw))
        return 0
    monkeypatch.setattr(module.ctypes, 'CDLL', lambda *a, **k: SimpleNamespace(sysctl=sysctl))
    reader = module.MacReader.__new__(module.MacReader)
    if failure:
        with pytest.raises((OSError, ValueError)):
            reader.argv(42)
    else:
        assert reader.argv(42) == ['codex', 'app-server', '--listen=stdio://']


@pytest.mark.parametrize('shape', ['pair', 'named', 'shared', 'listener', 'short-inventory', 'short-socket'])
def test_desktop_socket_collector_decodes_kernel_boundaries(shape):
    import struct
    module = desktop_module()
    reader = module.MacReader.__new__(module.MacReader)
    def inventory(pid, flavor, offset, buffer, size):
        assert (pid, flavor) == (42, 1)
        struct.pack_into('iI', buffer, 0, 0, 2)
        return 7 if shape == 'short-inventory' else 8
    def socket(pid, fd, flavor, buffer, size):
        assert (pid, fd, flavor, size) == (42, 0, 3, 792)
        struct.pack_into('I', buffer, 4, 1 if shape == 'shared' else 0)
        struct.pack_into('Q', buffer, 160, 100)
        struct.pack_into('i', buffer, 184, 1)
        struct.pack_into('h', buffer, 188, 2 if shape == 'listener' else 0)
        struct.pack_into('i', buffer, 256, 3)
        struct.pack_into('Q', buffer, 264, 200)
        if shape == 'named':
            buffer[282] = b'x'
        return 791 if shape == 'short-socket' else 792
    reader.lib = SimpleNamespace(proc_pidinfo=inventory, proc_pidfdinfo=socket)
    if shape.startswith('short'):
        with pytest.raises(OSError):
            reader.sockets(42)
    else:
        assert reader.sockets(42) == [{'fd': 0, 'handle': 100, 'peer': 200,
            'shared': shape == 'shared', 'anonymous': shape != 'named', 'listener': shape == 'listener'}]


@pytest.mark.parametrize('failure', [None, 'unforced', 'missing', 'wrong-type', 'too-large', 'allow-true'])
def test_forced_policy_requires_os_force_and_exact_false_scalar(monkeypatch, failure):
    module = desktop_module()
    released = []
    handles = iter([11, 12])
    def string_copy(value, buffer, size, encoding):
        assert (value, size) == (13, 4096)
        buffer.value = (module.POLICY_VALUE if failure != 'allow-true' else
                        module.base64.b64encode(b'allow_remote_control = true\n').decode()).encode()
        return failure != 'too-large'
    functions = {
        'CFStringCreateWithCString': lambda *a: next(handles),
        'CFPreferencesAppValueIsForced': lambda key, domain: failure != 'unforced',
        'CFPreferencesCopyAppValue': lambda key, domain: None if failure == 'missing' else 13,
        'CFGetTypeID': lambda value: 8 if failure == 'wrong-type' else 7,
        'CFStringGetTypeID': lambda: 7,
        'CFStringGetCString': string_copy,
        'CFRelease': lambda value: released.append(value),
    }
    monkeypatch.setattr(module.ctypes, 'CDLL', lambda path: SimpleNamespace(**functions))
    if failure:
        with pytest.raises(ValueError):
            module.forced_policy()
    else:
        assert module.forced_policy() == hashlib.sha256(b'allow_remote_control = false\n').hexdigest()
    assert 11 in released and 12 in released
    if failure not in {'unforced', 'missing'}:
        assert 13 in released
