"""Synthetic observer controls; these are not native acceptance tests."""

import argparse
import json
import os
import re
import socket
import subprocess
from pathlib import Path


def probe(root):
    sentinel = root / "observer-sentinel.txt"
    assert sentinel.read_text() == "private observer control\n"
    assert os.getuid() == 501 and os.getgid() == 20
    with os.scandir(root / "directory-scan-control") as entries:
        assert [entry.name for entry in entries] == ["sentinel-name-only"]
    # A second process establishes descendant coverage, including absolute exec.
    subprocess.run(["/usr/bin/true"], check=True, stdin=subprocess.DEVNULL)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect(("127.0.0.1", 9))
        except OSError:
            pass
    print(json.dumps({"uid": os.getuid(), "gid": os.getgid()}))


def validate(root):
    trace = (root / "control.trace").read_text()
    # Match this fixture's open FD/PID to raw getdents calls, rather than
    # counting Python startup enumeration of unrelated library directories.
    opened = re.search(
        r'^(\d+)\s+.*openat\(.*"/evidence/directory-scan-control",.*O_DIRECTORY.* = (\d+)',
        trace, re.MULTILINE,
    )
    directory_scan = False
    directory_close = False
    if opened:
        pid, fd = opened.groups()
        directory_scan = bool(re.search(
            rf'^{pid}\s+.*getdents(?:64)?\(0x{int(fd):x},\s+0x[0-9a-f]+,\s+0x[0-9a-f]+\)',
            trace[opened.end():], re.MULTILINE,
        ))
        directory_close = bool(re.search(
            rf'^{pid}\s+.*close\({fd}\)\s+= 0', trace[opened.end():], re.MULTILINE,
        ))
    identity = json.loads((root / "control.stdout").read_text())
    checks = {
        "descendant_exec": 'execve("/usr/bin/true"' in trace,
        "fixture_open": 'observer-sentinel.txt", O_RDONLY' in trace,
        "socket": "socket(AF_INET, SOCK_STREAM" in trace,
        "connect": 'sin_port=htons(9)' in trace and '127.0.0.1' in trace,
        "exits": "+++ exited with 0 +++" in trace,
        "trace_private": (root / "control.trace").stat().st_mode & 0o777 == 0o600,
        "directory_private": root.stat().st_mode & 0o777 == 0o700,
        "actual_uid_gid": identity == {"uid": 501, "gid": 20},
        "fixture_directory_enumeration": directory_scan,
        "fixture_directory_fd_closed": directory_close,
        "directory_payload_not_decoded": "sentinel-name-only" not in trace,
    }
    result = {
        "evidence_kind": "synthetic",
        "native_acceptance": False,
        "checks": checks,
        "ready_for_control_scope": all(checks.values()),
        "limits": [
            "Controls establish tracing capability, not native hook dispatch.",
            "Process/file/socket/connect and FD lifecycle metadata are recorded.",
            "Directory enumeration uses raw FD/pointer/count/results; no dirent payload.",
            "Directory scans need per-PID FD attribution, including inheritance/reuse.",
            "Real runs must separately identify hook descendants and native origin.",
        ],
    }
    (root / "control-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["probe", "validate"])
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.mode == "probe":
        probe(args.root)
    else:
        raise SystemExit(validate(args.root))
