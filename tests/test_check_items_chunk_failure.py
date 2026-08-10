"""Tests for _dispatch_classifier_chunk's "no output" vs "invalid JSON" distinction.

Regression coverage for #297 defect 1: the chunked caller in run_classifier()
pre-creates each chunk's output_path as a 0-byte temp file to allocate a safe
path (check_items_cli.py:690-695). _dispatch_classifier_chunk used to test
`if Path(output_path).exists():`, which is unconditionally true for that
pre-created file, so a sub-agent that wrote nothing fell into
`json.loads("")` and was misreported as "invalid JSON" (rc 4) instead of the
distinct "wrote no output" failure (RC_NO_OUTPUT / rc 6).
"""
import json
import os
import sys
from pathlib import Path

HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

import check_items_cli  # noqa: E402


def _make_stub(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """A subprocess.CompletedProcess-shaped stub. Does NOT write output_path —
    callers pre-create/pre-write it (or not) before invoking the function
    under test, per the caller-asymmetry this module is guarding against."""
    class _Stub:
        pass
    stub = _Stub()
    stub.returncode = returncode
    stub.stdout = stdout
    stub.stderr = stderr
    return stub


_CHUNK_GROUPS = [{"group_id": "g1", "project": "obsidian-brain", "representative": "x", "instances": []}]
_EVIDENCE = {}

_VALID_STDOUT_RESULT = [
    {
        "group_id": "g1",
        "classification": "DONE",
        "confidence": "HIGH",
        "canonical_text": "Fix session_log race condition",
        "evidence_citation": "commit abc1234",
        "action_required": None,
    }
]


def test_empty_file_and_empty_stdout_reports_no_output(tmp_path, monkeypatch, capsys):
    """Chunked-caller shape: output_path pre-created as a 0-byte file (as the
    real chunked dispatch loop does), sub-agent stdout also empty. This must
    be reported as RC_NO_OUTPUT, not misparsed as invalid JSON."""
    output_path = str(tmp_path / "out.json")
    Path(output_path).touch()  # 0-byte pre-created temp, mirrors :690-695

    monkeypatch.setattr(
        check_items_cli.subprocess, "run",
        lambda *a, **kw: _make_stub(returncode=0, stdout=""),
    )

    rc, results = check_items_cli._dispatch_classifier_chunk(
        _CHUNK_GROUPS, _EVIDENCE, "haiku", output_path,
    )

    err = capsys.readouterr().err
    assert rc == check_items_cli.RC_NO_OUTPUT
    assert results == []
    assert "wrote no output" in err
    assert "invalid JSON" not in err


def test_malformed_file_still_reports_invalid_json(tmp_path, monkeypatch, capsys):
    """A non-blank but malformed output file must still be reported as
    invalid JSON (rc 4) — the no-output guard must not swallow real
    parse failures."""
    output_path = str(tmp_path / "out.json")
    Path(output_path).write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(
        check_items_cli.subprocess, "run",
        lambda *a, **kw: _make_stub(returncode=0, stdout=""),
    )

    rc, results = check_items_cli._dispatch_classifier_chunk(
        _CHUNK_GROUPS, _EVIDENCE, "haiku", output_path,
    )

    err = capsys.readouterr().err
    assert rc == 4
    assert results == []
    assert "invalid JSON" in err


def test_blank_file_falls_back_to_stdout(tmp_path, monkeypatch, capsys):
    """Guards the pre-existing stdout fallback: 0-byte output file (chunked
    caller shape) but the sub-agent DID produce valid JSON on stdout — this
    must still succeed via the stdout fallback, not be misreported."""
    output_path = str(tmp_path / "out.json")
    Path(output_path).touch()

    monkeypatch.setattr(
        check_items_cli.subprocess, "run",
        lambda *a, **kw: _make_stub(returncode=0, stdout=json.dumps(_VALID_STDOUT_RESULT)),
    )

    rc, results = check_items_cli._dispatch_classifier_chunk(
        _CHUNK_GROUPS, _EVIDENCE, "haiku", output_path,
    )

    assert rc == 0
    assert results == _VALID_STDOUT_RESULT


def test_whitespace_only_file_treated_as_no_output(tmp_path, monkeypatch, capsys):
    """A file containing only whitespace (no real content) must be treated
    the same as an empty file, not fall into json.loads("\\n  \\n")."""
    output_path = str(tmp_path / "out.json")
    Path(output_path).write_text("\n  \n", encoding="utf-8")

    monkeypatch.setattr(
        check_items_cli.subprocess, "run",
        lambda *a, **kw: _make_stub(returncode=0, stdout=""),
    )

    rc, results = check_items_cli._dispatch_classifier_chunk(
        _CHUNK_GROUPS, _EVIDENCE, "haiku", output_path,
    )

    err = capsys.readouterr().err
    assert rc == check_items_cli.RC_NO_OUTPUT
    assert results == []
    assert "wrote no output" in err
