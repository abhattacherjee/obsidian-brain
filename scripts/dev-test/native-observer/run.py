"""Build and run a disposable, unprivileged Linux syscall observer."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
SYSCALLS = "%process,%file,socket,connect,getdents,getdents64,close,dup,dup2,dup3"
RAW_SYSCALLS = "getdents,getdents64"


def receipt(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def private_root(value):
    root = Path(value).resolve(strict=True)
    if not root.is_dir() or root == Path.home() or root == Path("/"):
        raise ValueError("Select a dedicated existing private sandbox directory")
    if root.stat().st_mode & 0o077:
        raise ValueError("Sandbox root must be owner-only (0700)")
    return root


def check_mount(image, root):
    # Colima can create an empty VM directory for an unshared macOS path.
    # Prove that the container sees our exact private fixture before launch.
    with tempfile.NamedTemporaryFile(prefix=".observer-mount-", dir=root) as marker:
        marker.write(b"private observer mount control\n")
        marker.flush()
        subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only",
             "--cap-drop", "ALL", "--user", "501:20", "-v", f"{root}:/evidence:ro",
             image, "python3", "-I", "-c",
             "from pathlib import Path; import sys; "
             "assert Path(sys.argv[1]).read_bytes() == b'private observer mount control\\n'",
             "/evidence/" + Path(marker.name).name],
            stdin=subprocess.DEVNULL, timeout=30, check=True,
        )


def verify_package(package, source, candidate_sha):
    """Bind the full archive to a real Git commit, including its complete inventory."""
    if not isinstance(candidate_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        raise ValueError("candidate_sha must be a full 40-character Git commit")
    source = Path(source).resolve(strict=True)
    kind = subprocess.check_output(
        ["git", "-C", str(source), "cat-file", "-t", candidate_sha],
        stdin=subprocess.DEVNULL, text=True, timeout=30,
    ).strip()
    if kind != "commit":
        raise ValueError("candidate_sha must identify a commit")
    tree = subprocess.check_output(
        ["git", "-C", str(source), "ls-tree", "-r", "-z", candidate_sha],
        stdin=subprocess.DEVNULL, timeout=30,
    )
    expected = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, object_type, blob = metadata.split()
        relative = raw_path.decode("utf-8")
        path = package / relative
        if object_type != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError("Observer packages must contain ordinary Git files only")
        if path.is_symlink() or not path.resolve().is_relative_to(package):
            raise ValueError("Package symlinks are not permitted")
        data = path.read_bytes()
        digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if digest != blob.decode():
            raise ValueError("Package differs from candidate commit: " + relative)
        expected[relative] = hashlib.sha256(data).hexdigest()
    actual = {}
    for path in package.rglob("*"):
        if path.is_symlink():
            raise ValueError("Package symlinks are not permitted")
        if path.is_file():
            actual[path.relative_to(package).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError("Package contains missing or extra files")
    return {"candidate_sha": candidate_sha, "files": expected,
            "inventory_sha256": hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()}


def validate_native_manifest(manifest, root, selected):
    if manifest.get("platform") != "linux":
        raise ValueError("Select a Linux run manifest")
    required = ("fixture_writes", "selected_scratch_cache_install_restore",
                "synthetic_native_analysis", "private_copy_fault_injection")
    if any(manifest.get("approved_actions", {}).get(key) is not True for key in required):
        raise ValueError("The complete fixture campaign needs explicit execution authorization")
    if not isinstance(manifest.get("authorization_source"), str) or not manifest["authorization_source"].strip():
        raise ValueError("Record the human execution authorization source")
    declared = manifest.get("declared_client_launch", {})
    client = manifest["client"]
    if declared.get("client") != client:
        raise ValueError("The recorded client binding must match the selected client")
    if client == "codex-cli":
        if (declared.get("binding_method") != "operator-launch-declaration"
                or declared.get("declared_before_launch") is not True
                or not isinstance(declared.get("declared_by"), str)
                or not declared["declared_by"].strip()):
            raise ValueError("Codex needs a matching operator declaration before launch")
        expected_kind = "trusted-pre-launch-declaration"
    else:
        if declared.get("binding_method") != "fixed-claude-client":
            raise ValueError("Claude must use the recorded fixed host-client binding")
        expected_kind = "trusted-native-context"
    path = Path(declared.get("launch_receipt_path", "")).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("Keep the trusted declaration receipt inside the sandbox")
    record = json.loads(path.read_text())
    if (record.get("kind") != expected_kind
            or record.get("client") != client
            or record.get("binding_method") != declared["binding_method"]
            or record.get("candidate_sha") != manifest.get("candidate_sha")
            or (client == "codex-cli" and record.get("declared_by") != declared["declared_by"])
            or record.get("environment_allowlist") != selected):
        raise ValueError("Trusted pre-launch receipt does not match the selected native context")
    if not record.get("declared_at"):
        raise ValueError("Trusted pre-launch receipt needs its declaration timestamp")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_control(args):
    root = private_root(args.root)
    (root / "observer-sentinel.txt").write_text("private observer control\n")
    scan_root = root / "directory-scan-control"
    scan_root.mkdir(mode=0o700)
    (scan_root / "sentinel-name-only").write_text("synthetic directory fixture\n")
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"],
        text=True, stdin=subprocess.DEVNULL, timeout=30,
    ).strip()
    check_mount(image_id, root)
    base_id = subprocess.check_output(
        ["docker", "image", "inspect", image_id, "--format",
         '{{index .Config.Labels "org.obp.observer.base-image-id"}}'],
        text=True, stdin=subprocess.DEVNULL, timeout=30,
    ).strip()
    version = subprocess.check_output(
        ["docker", "run", "--rm", "--network", "none", "--read-only",
         "--cap-drop", "ALL", "--user", "501:20", image_id, "strace", "--version"],
        text=True, stdin=subprocess.DEVNULL, timeout=30,
    ).splitlines()[0]
    cidfile = root / "control-container.id"
    command = [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--user", "501:20", "--cidfile", str(cidfile),
        "-v", f"{root}:/evidence", "-v", f"{TOOLS}:/observer:ro",
        image_id, "sh", "-c", 'umask 077; exec "$@"', "observer-strace",
        "strace", "-f", "-ttt", "-T", "-s", "256",
        "-e", f"trace={SYSCALLS}", "-e", f"raw={RAW_SYSCALLS}",
        "-o", "/evidence/control.trace",
        "python3", "-I", "/observer/control.py", "probe", "/evidence",
    ]
    started = datetime.now(timezone.utc).isoformat()
    with (root / "control.stdout").open("x") as stdout:
        with (root / "control.stderr").open("x") as stderr:
            try:
                completed = subprocess.run(
                    command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    timeout=60, check=False,
                )
            except subprocess.TimeoutExpired:
                # Remove only the exact container started by this control.
                if cidfile.exists():
                    subprocess.run(
                        ["docker", "rm", "-f", cidfile.read_text().strip()],
                        stdin=subprocess.DEVNULL, timeout=30, check=False,
                    )
                receipt(root / "control-launch.json", {
                    "evidence_kind": "synthetic", "started_utc": started,
                    "image": args.image, "image_id": image_id,
                    "result": "BLOCKED", "reason": "control timed out",
                })
                return 124
    trace = root / "control.trace"
    if trace.exists():
        trace.chmod(0o600)
    receipt(root / "control-launch.json", {
        "evidence_kind": "synthetic", "started_utc": started,
        "image": args.image, "image_id": image_id, "capabilities": [],
        "base_image_id": base_id, "strace_version": version,
        "uid": 501, "gid": 20, "privileged": False, "read_only_rootfs": True,
        "network": "none", "exit_status": completed.returncode,
        "syscalls": SYSCALLS, "raw_syscalls": RAW_SYSCALLS,
        "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest() if trace.exists() else None,
    })
    if completed.returncode:
        return completed.returncode
    return subprocess.run(
        [sys.executable, "-I", str(TOOLS / "control.py"), "validate", str(root)],
        stdin=subprocess.DEVNULL, timeout=30, check=False,
    ).returncode


def run_native(args):
    manifest = json.loads(Path(args.manifest).read_text())
    root = private_root(manifest["sandbox_root"])
    client = manifest["client"]
    if client not in ("codex-cli", "claude-code"):
        raise ValueError("This launcher supports Linux CLI cells only")
    selected = {
        "OB_CLIENT": client,
        "HOME": str(root / "home"),
        "OBSIDIAN_BRAIN_CONFIG": manifest["config_path"],
        "OBSIDIAN_BRAIN_DB": manifest["index_path"],
        "OBSIDIAN_BRAIN_STATE_DIR": manifest["state_path"],
        "XDG_STATE_HOME": str(root / "xdg/state"),
        "XDG_DATA_HOME": str(root / "xdg/data"),
        "CODEX_HOME" if client == "codex-cli" else "CLAUDE_CONFIG_DIR": manifest["native_home"],
    }
    for value in [*selected.values()][1:] + [manifest["project_path"], manifest["evidence_root"]]:
        if not Path(value).resolve().is_relative_to(root):
            raise ValueError("Every selected path must stay inside the sandbox")
    evidence = Path(manifest["evidence_root"])
    package = Path(args.package).resolve(strict=True)
    if not package.is_dir() or package == Path.home() or package == Path("/"):
        raise ValueError("Select the reviewed candidate package directory")
    vault = Path(manifest["vault_path"]).resolve(strict=True)
    if not vault.is_relative_to(root):
        raise ValueError("The fixture vault must stay inside the sandbox")
    config = json.loads(Path(manifest["config_path"]).read_text())
    if Path(config.get("vault_path", "")).resolve() != vault:
        raise ValueError("Selected config and manifest disagree on the fixture vault")
    if config.get("index_path") and Path(config["index_path"]).resolve() != Path(manifest["index_path"]).resolve():
        raise ValueError("Selected config and manifest disagree on the private index")
    declaration_hash = validate_native_manifest(manifest, root, selected)
    package_hashes = verify_package(package, args.source_checkout, manifest.get("candidate_sha"))
    for value in (selected["HOME"], selected["XDG_STATE_HOME"], selected["XDG_DATA_HOME"]):
        path = Path(value)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    evidence.chmod(0o700)
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"],
        text=True, stdin=subprocess.DEVNULL, timeout=30,
    ).strip()
    check_mount(image_id, root)
    receipt(evidence / "observer-launch.json", {
        "kind": "operator-pre-launch-observer", "client": client,
        "binding_method": manifest["declared_client_launch"]["binding_method"],
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_sha": manifest["candidate_sha"], "image": args.image,
        "image_id": image_id,
        "environment_allowlist": selected, "capabilities": [],
        "declaration_receipt_sha256": declaration_hash,
        "package_inventory_sha256": package_hashes["inventory_sha256"],
        "syscalls": SYSCALLS, "raw_syscalls": RAW_SYSCALLS,
        "native_acceptance": False,
        "sandbox_status": "BLOCKED: actual native command sandbox has not been verified",
        "home_audit": "Container HOME is inside the mounted sandbox; fixed-home writes remain observable",
    })
    receipt(evidence / "observer-package-hashes.json", package_hashes)
    receipt(evidence / "native-sandbox-status.json", {
        "result": "BLOCKED", "native_acceptance": False,
        "reason": "The actual client command sandbox needs client-owned run evidence",
        "required_evidence": ["client sandbox settings", "actual sandboxed command outcome",
                              "client-owned tool result and stderr", "any environment difference"],
        "bypass_allowed": False,
    })
    command = ["docker", "run", "-it", "--rm", "--cap-drop", "ALL", "--user", "501:20",
               "-v", f"{root}:{root}", "-v", f"{package}:/obp/package:ro",
               "-w", manifest["project_path"]]
    for key, value in selected.items():
        command.extend(["-e", f"{key}={value}"])
    command.extend([image_id, "sh", "-c", 'umask 077; exec "$@"', "observer-strace",
                    "strace", "-f", "-ttt", "-T", "-s", "256", "-e",
                    f"trace={SYSCALLS}", "-e", f"raw={RAW_SYSCALLS}",
                    "-o", str(evidence / "native-syscalls.trace")])
    if client == "codex-cli":
        command.extend(["codex", "--no-daemon"])
    else:
        command.extend(["claude", "--debug", "hooks", "--debug-file",
                        str(evidence / "claude-debug-hooks.log")])
    started = datetime.now(timezone.utc).isoformat()
    completed = subprocess.run(command, check=False)
    trace = evidence / "native-syscalls.trace"
    if trace.exists():
        trace.chmod(0o600)
    receipt(evidence / "observer-completion.json", {
        "started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(),
        "exit_status": completed.returncode, "native_acceptance": False,
        "sandbox_status": "BLOCKED pending independent client-owned sandbox evidence",
        "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest() if trace.exists() else None,
    })
    return completed.returncode


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    build = commands.add_parser("build")
    build.add_argument("--base-image", required=True)
    build.add_argument("--image", required=True)
    control = commands.add_parser("control")
    control.add_argument("--image", required=True)
    control.add_argument("--root", required=True)
    native = commands.add_parser("native")
    native.add_argument("--image", required=True)
    native.add_argument("--manifest", required=True)
    native.add_argument("--package", required=True)
    native.add_argument("--source-checkout", required=True)
    args = parser.parse_args()
    if args.mode == "build":
        base_id = subprocess.check_output(
            ["docker", "image", "inspect", args.base_image, "--format", "{{.Id}}"],
            text=True, stdin=subprocess.DEVNULL, timeout=30,
        ).strip()
        return subprocess.run(
            ["docker", "build", "--build-arg", f"BASE_IMAGE={base_id}",
             "--build-arg", f"BASE_IMAGE_ID={base_id}",
             "-t", args.image, str(TOOLS)], stdin=subprocess.DEVNULL, timeout=300,
            check=False,
        ).returncode
    if args.mode == "control":
        return run_control(args)
    return run_native(args)


if __name__ == "__main__":
    raise SystemExit(main())
