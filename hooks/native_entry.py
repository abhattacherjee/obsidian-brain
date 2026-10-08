#!/usr/bin/env python3
"""Run an explicitly bound native hook from its installed resource tree."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

STARTED_AT = time.monotonic()
RESOURCE_ROOT = Path(__file__).resolve().parent.parent


def isolated_cli_options(argv):
    """Check leading launch flags, without mistaking prompt text for a flag."""
    takes_value = {"-c", "--config", "-m", "--model", "-p", "--profile",
                   "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd",
                   "--add-dir", "-i", "--image", "--enable", "--disable"}
    index = 1
    while index < len(argv):
        value = argv[index]
        if value == "--no-daemon":
            return True
        if value == "--" or not value.startswith("-"):
            return False
        index += 2 if value in takes_value else 1
    return False


def macos_process_argv(pid):
    """Read only argc-delimited argv; never interpret or emit the environment."""
    import ctypes
    import struct
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        raise OSError("Process arguments are unavailable")
    if not 4 < size.value <= 1_000_000:
        raise ValueError("Process argument size is invalid")
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError("Process arguments are unavailable")
    raw = buffer.raw[:size.value]
    argc = struct.unpack_from("i", raw)[0]
    if not 0 < argc <= 4096:
        raise ValueError("Process argument count is invalid")
    offset = raw.index(b"\0", 4) + 1  # Kernel executable path precedes argv.
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    argv = []
    for _ in range(argc):
        end = raw.index(b"\0", offset)
        argv.append(os.fsdecode(raw[offset:end]))
        offset = end + 1
    return argv


def macos_process_parent(pid):
    """Read only parent/name metadata through libproc, without spawning ps."""
    import ctypes
    # SDK sys/proc_info.h: proc_bsdshortinfo, MAXCOMLEN=16, uint32_t uid/gid.
    # Flavor 13 exposes minimal metadata across UIDs; flavor 3 is restricted.
    class BsdInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in ("pid", "ppid", "pgid", "status")]
        _fields_ += [("comm", ctypes.c_char * 16)]
        _fields_ += [(name, ctypes.c_uint32) for name in (
            "flags", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    if ctypes.sizeof(BsdInfo) != 64:
        raise ValueError("Process metadata ABI is unsupported")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    reader = library.proc_pidinfo
    reader.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    reader.restype = ctypes.c_int
    info = BsdInfo()
    if reader(pid, 13, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info) or info.pid != pid:
        raise OSError("Process metadata is unavailable")
    return info.ppid, os.fsdecode(info.comm)


def process_ancestor(pid, deadline):
    """Read PPID and real argv only for a Codex executable candidate."""
    if sys.platform.startswith("linux"):
        process = Path("/proc") / str(pid)
        stat = (process / "stat").read_bytes()
        if len(stat) > 16384:
            raise ValueError("Process stat is too large")
        closing = stat.rindex(b")")
        parent = int(stat[closing + 1:].split()[1])
        # stat is readable across UIDs; exe requires ptrace permissions and
        # would reject ordinary root-owned SSH/login ancestors.
        executable = os.fsdecode(stat[stat.index(b"(") + 1:closing])
        argv = []
        if executable in {"codex", "codex-cli"}:
            with (process / "cmdline").open("rb") as source:
                raw = source.read(1_000_001)
            if not raw or len(raw) > 1_000_000 or not raw.endswith(b"\0"):
                raise ValueError("Process arguments are invalid")
            argv = [os.fsdecode(value) for value in raw[:-1].split(b"\0")]
        return parent, argv
    if sys.platform == "darwin":
        parent, executable = macos_process_parent(pid)
        argv = macos_process_argv(pid) if executable in {"codex", "codex-cli"} else []
        return parent, argv
    raise OSError("Process ancestry is unsupported")


def isolated_cli_ancestor():
    """Use ancestry only to refuse unsupported launches, never select a client."""
    deadline = time.monotonic() + 0.8
    pid = os.getppid()
    seen = set()
    isolated = False
    for _ in range(32):
        if pid <= 1:
            return isolated
        if pid in seen or time.monotonic() >= deadline:
            return False
        seen.add(pid)
        try:
            pid, argv = process_ancestor(pid, deadline)
            if argv:
                options = argv[1:argv.index("--")] if "--" in argv else argv[1:]
                if "app-server" in options or "--daemon" in options:
                    return False
                if isolated_cli_options(argv):
                    isolated = True
        except (OSError, ValueError):
            return False
    return False


def run(argv=None, *, started_at=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value == "--resource-root" or value.startswith("--resource-root=")
           for value in arguments):
        print("[obsidian-brain] native resource root must be adjacent to the entry point.",
              file=sys.stderr)
        return 0
    selection = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    selection.add_argument("--host")
    selection.add_argument("--client")
    selection.add_argument("--require-cli-launcher", action="store_true")
    try:
        selected, _ = selection.parse_known_args(arguments)
    except SystemExit:
        print("[obsidian-brain] native lifecycle arguments are invalid; capture was skipped; no source was retained.",
              file=sys.stderr)
        return 0
    if selected.host == "codex" and not selected.client:
        print("[obsidian-brain] native client identity is unavailable; capture was skipped; no source was retained. "
              "CLI/Desktop handoff requires a current-client binding.", file=sys.stderr)
        return 0
    clients = {"claude": {"claude-code"}, "codex": {"codex-cli", "codex-desktop"}}
    if selected.client not in clients.get(selected.host, set()):
        print("[obsidian-brain] native client identity is invalid; capture was skipped; no source was retained.",
              file=sys.stderr)
        return 0
    # OB_CLIENT is a launcher declaration, never a frontend guess. Registered
    # Codex commands pass it explicitly; an absent declaration stays unbound.
    declared = os.environ.get("OB_CLIENT")
    if declared is not None and declared != selected.client:
        print("[obsidian-brain] native client declaration conflicts with the selected client; "
              "capture was skipped; no source was retained.", file=sys.stderr)
        return 0
    if selected.require_cli_launcher:
        if selected.host != "codex" or selected.client != "codex-cli" or not isolated_cli_ancestor():
            print("[obsidian-brain] isolated Codex CLI launcher is unavailable; capture was skipped; "
                  "no source was retained. Use OB_CLIENT=codex-cli codex --no-daemon; "
                  "shared-server and Desktop capture are unsupported.", file=sys.stderr)
            return 0
        arguments.remove("--require-cli-launcher")
    sys.path.insert(0, str(RESOURCE_ROOT / "hooks"))
    try:
        from brain_cli import main
        main(["--resource-root", str(RESOURCE_ROOT), *arguments, "hook"],
             started_at=STARTED_AT if started_at is None else started_at)
    except (Exception, SystemExit):
        print("[obsidian-brain] native lifecycle failed; capture was skipped; no source was retained.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
