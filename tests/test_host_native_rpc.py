"""Exercise native metadata transport with a local JSON server and no model."""

import json
import os
import sys
import time

import pytest

from ai_adapters.codex import NativeRpc
from ai_backend import _BackendFailure


def rpc(tmp_path, body, context, timeout=2):
    executable = tmp_path / "synthetic-server"
    executable.write_text("#!" + sys.executable + "\nimport json,sys,time,os\n" + body)
    executable.chmod(0o700)
    return NativeRpc(str(executable), [], context,
                     dict(os.environ), time.monotonic() + timeout)


def test_transport_initializes_and_reads_matching_reply(selected_host_context, tmp_path):
    server = rpc(tmp_path, "for line in sys.stdin:\n"
                 " message=json.loads(line)\n"
                 " if 'id' in message:\n"
                 "  print(json.dumps({'id':message['id'],'result':{'method':message['method'],'cwd':os.getcwd()}}),flush=True)\n", selected_host_context)
    try:
        server.initialize()
        assert server.request("config/read", {}) == {"method": "config/read", "cwd": str(selected_host_context.worktree)}
    finally:
        server.close()
    assert server.process.poll() is not None
    assert all(pipe.closed for pipe in (server.process.stdin, server.process.stdout, server.process.stderr))


@pytest.mark.parametrize("reply,code", [
    ({"id": 1, "method": "approval", "params": {}}, "native_inventory_approval_required"),
    ({"id": 2, "result": {}}, "native_inventory_unexpected_reply"),
    ({"id": 1, "error": {"message": "private sentinel"}}, "native_inventory_error"),
    ([], "native_inventory_invalid"),
])
def test_transport_rejects_untrusted_response_without_approval(selected_host_context, tmp_path, reply, code):
    server = rpc(tmp_path, "sys.stdin.readline()\nprint(" + repr(json.dumps(reply)) + ",flush=True)\ntime.sleep(5)\n", selected_host_context)
    try:
        with pytest.raises(_BackendFailure) as exc:
            server.request("config/read", {})
        assert exc.value.code == code
        assert "private sentinel" not in str(exc.value)
    finally:
        server.close()
    assert server.process.poll() is not None


@pytest.mark.parametrize("body,code", [
    ("time.sleep(5)\n", "native_inventory_timeout"),
    ("sys.stdin.readline()\n", "native_inventory_closed"),
    ("sys.stdin.readline()\nsys.stdout.write('x'*2100000);sys.stdout.flush();time.sleep(5)\n", "native_inventory_limit"),
])
def test_transport_deadline_eof_and_output_limit(selected_host_context, tmp_path, body, code):
    server = rpc(tmp_path, body, selected_host_context, timeout=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(_BackendFailure) as exc:
            server.request("config/read", {})
        assert exc.value.code == code
    finally:
        server.close()
    assert time.monotonic() - started < 2


def test_server_that_never_reads_input_cannot_block_deadline(selected_host_context, tmp_path):
    server = rpc(tmp_path, "time.sleep(5)\n", selected_host_context, timeout=0.3)
    started = time.monotonic()
    try:
        with pytest.raises(_BackendFailure) as exc:
            server.request("config/read", {"synthetic": "x" * 1_000_000})
        assert exc.value.code == "native_inventory_timeout"
    finally:
        server.close()
    assert time.monotonic() - started < 2
