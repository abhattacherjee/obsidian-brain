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


# -----------------------------------------------------------------------------
# run_classifier() chunk-failure locality (#297 defect 2)
#
# A failing chunk must degrade only its OWN groups: every already-completed
# chunk is kept, every remaining chunk still runs, and only total failure
# (every chunk exhausted) falls back to the old whole-run `return rc`.
# -----------------------------------------------------------------------------

def _ref_group(n: int) -> dict:
    """Group whose canonical_text has a `#N` reference, so has_classifiable_evidence()
    routes it to the sub-agent (to_classify) via REF_PATTERN regardless of the
    evidence dict's contents."""
    text = f"Fix bug #{n}"
    return {
        "group_id": f"g{n}",
        "project": "obsidian-brain",
        "representative": text,
        "instances": [{"file": "note.md", "line": 1, "text": text, "mtime": 0.0}],
    }


def _noref_group(n: int) -> dict:
    """Group with no #N/sha reference and no evidence overlap — has_classifiable_evidence()
    returns False, so L2 routes it to synthetic_classification() and it never
    reaches the sub-agent or the scripted dispatch stub."""
    text = "clean up some vague leftover code"
    return {
        "group_id": f"s{n}",
        "project": "obsidian-brain",
        "representative": text,
        "instances": [{"file": "note.md", "line": 1, "text": text, "mtime": 0.0}],
    }


def _classifier_result(group_id: str) -> dict:
    return {
        "group_id": group_id,
        "classification": "DONE",
        "confidence": "HIGH",
        "canonical_text": f"Fix bug #{group_id[1:]}",
        "evidence_citation": "commit abc1234",
        "action_required": None,
    }


def _payload(groups: list) -> str:
    return json.dumps({"groups": groups, "evidence": {}})


def _make_scripted_dispatch(script: dict):
    """Build a _dispatch_classifier_chunk replacement driven by `script`.

    `script` maps a chunk's group_id tuple -> a list of (rc, results)
    outcomes, consumed in call order (one entry per attempt on that chunk).
    Calling past the end of a chunk's outcome list repeats the last one.

    Returns (stub, calls); `calls` records (group_id_tuple, chunk_label) in
    call order so tests can assert exactly which chunks/attempts fired.
    """
    calls: list = []

    def _stub(chunk_groups, evidence, model, output_path, chunk_label=""):
        key = tuple(g["group_id"] for g in chunk_groups)
        calls.append((key, chunk_label))
        attempt_n = sum(1 for k, _ in calls if k == key)
        outcomes = script[key]
        return outcomes[min(attempt_n - 1, len(outcomes) - 1)]

    return _stub, calls


def test_failing_chunk_does_not_discard_completed_chunks(tmp_path, monkeypatch, capsys):
    """Chunk 1 fails both attempts (RC_NO_OUTPUT); chunks 2 and 3 succeed.
    run_classifier must return 0, keep chunks 2/3's group_ids, drop chunk
    1's, and log the degrade-not-abort message."""
    monkeypatch.setattr(check_items_cli, "CLASSIFIER_CHUNK_SIZE", 2)

    groups = [_ref_group(n) for n in range(1, 7)]  # -> chunks (g1,g2)(g3,g4)(g5,g6)
    script = {
        ("g1", "g2"): [
            (check_items_cli.RC_NO_OUTPUT, []),
            (check_items_cli.RC_NO_OUTPUT, []),
        ],
        ("g3", "g4"): [(0, [_classifier_result("g3"), _classifier_result("g4")])],
        ("g5", "g6"): [(0, [_classifier_result("g5"), _classifier_result("g6")])],
    }
    stub, calls = _make_scripted_dispatch(script)
    monkeypatch.setattr(check_items_cli, "_dispatch_classifier_chunk", stub)

    output_path = str(tmp_path / "out.json")
    rc = check_items_cli.run_classifier(_payload(groups), output_path)

    err = capsys.readouterr().err
    assert rc == 0
    assert [key for key, _ in calls] == [
        ("g1", "g2"), ("g1", "g2"), ("g3", "g4"), ("g5", "g6"),
    ], "chunk 1 must be attempted twice (retry); chunks 2 and 3 once each"

    out = json.loads(Path(output_path).read_text())
    assert {r["group_id"] for r in out} == {"g3", "g4", "g5", "g6"}
    assert "left unclassified, continuing" in err


def test_chunk_retried_once_before_giving_up(tmp_path, monkeypatch):
    """Chunk 1 fails on attempt 1 but succeeds on attempt 2 — the retry
    recovers it, so all 6 group_ids must be present and rc must be 0."""
    monkeypatch.setattr(check_items_cli, "CLASSIFIER_CHUNK_SIZE", 2)

    groups = [_ref_group(n) for n in range(1, 7)]
    script = {
        ("g1", "g2"): [
            (3, []),
            (0, [_classifier_result("g1"), _classifier_result("g2")]),
        ],
        ("g3", "g4"): [(0, [_classifier_result("g3"), _classifier_result("g4")])],
        ("g5", "g6"): [(0, [_classifier_result("g5"), _classifier_result("g6")])],
    }
    stub, calls = _make_scripted_dispatch(script)
    monkeypatch.setattr(check_items_cli, "_dispatch_classifier_chunk", stub)

    output_path = str(tmp_path / "out.json")
    rc = check_items_cli.run_classifier(_payload(groups), output_path)

    assert rc == 0
    out = json.loads(Path(output_path).read_text())
    assert {r["group_id"] for r in out} == {"g1", "g2", "g3", "g4", "g5", "g6"}
    assert [key for key, _ in calls] == [
        ("g1", "g2"), ("g1", "g2"), ("g3", "g4"), ("g5", "g6"),
    ]


def test_all_chunks_failing_returns_nonzero(tmp_path, monkeypatch):
    """Every chunk exhausts its retry budget — total failure must still
    return non-zero (the LAST chunk's failure rc), preserving today's
    whole-run heuristic-fallback semantics for the caller."""
    monkeypatch.setattr(check_items_cli, "CLASSIFIER_CHUNK_SIZE", 2)

    groups = [_ref_group(n) for n in range(1, 7)]
    script = {
        ("g1", "g2"): [(3, []), (3, [])],
        ("g3", "g4"): [(4, []), (4, [])],
        ("g5", "g6"): [(7, []), (7, [])],
    }
    stub, calls = _make_scripted_dispatch(script)
    monkeypatch.setattr(check_items_cli, "_dispatch_classifier_chunk", stub)

    output_path = str(tmp_path / "out.json")
    rc = check_items_cli.run_classifier(_payload(groups), output_path)

    assert rc == 7, f"Expected the last chunk's failure rc (7), got {rc}"
    assert len(calls) == 6, "every chunk must exhaust both attempts"


def test_prefiltered_results_survive_a_failed_chunk(tmp_path, monkeypatch):
    """L2 prefilter produces synthetic records for some groups; a failing
    sub-agent chunk covering OTHER groups must not drop the synthetic
    ones from output_path."""
    monkeypatch.setattr(check_items_cli, "CLASSIFIER_CHUNK_SIZE", 2)

    groups = [
        _noref_group(1), _noref_group(2),
        _ref_group(1), _ref_group(2), _ref_group(3), _ref_group(4),
    ]
    script = {
        ("g1", "g2"): [(0, [_classifier_result("g1"), _classifier_result("g2")])],
        ("g3", "g4"): [
            (check_items_cli.RC_NO_OUTPUT, []),
            (check_items_cli.RC_NO_OUTPUT, []),
        ],
    }
    stub, calls = _make_scripted_dispatch(script)
    monkeypatch.setattr(check_items_cli, "_dispatch_classifier_chunk", stub)

    output_path = str(tmp_path / "out.json")
    rc = check_items_cli.run_classifier(_payload(groups), output_path)

    assert rc == 0
    out = json.loads(Path(output_path).read_text())
    assert {r["group_id"] for r in out} == {"s1", "s2", "g1", "g2"}


def test_truncation_before_retry_prevents_stale_read(tmp_path, monkeypatch):
    """A garbage/partial write from a failed attempt must never be read as
    the next attempt's output (run_classifier's truncate-before-every-
    attempt guard, hooks/check_items_cli.py:742-745).

    Unlike the tests above, this one stubs `subprocess.run` rather than
    `_dispatch_classifier_chunk` — it exists specifically to exercise the
    REAL Path.read_text()/json.loads() path inside _dispatch_classifier_chunk
    against the real per-chunk temp output file, since stubbing
    _dispatch_classifier_chunk wholesale (as every other test here does)
    never touches disk and so can never catch a truncation regression.

    Chunk 1 (g1), attempt 1: writes garbage to the chunk's output file AND
    returns a non-zero rc. _dispatch_classifier_chunk's `cp.returncode != 0`
    check returns early WITHOUT ever reading the file, so the garbage
    survives on disk for the retry.
    Chunk 1 (g1), attempt 2: writes nothing to the output file; returns rc 0
    with valid JSON on stdout instead.

    With truncation present: the file is blank at attempt 2, so the code
    falls through to the stdout fallback and succeeds. With truncation
    deleted: the file still holds the attempt-1 garbage, `file_text.strip()`
    is truthy, `json.loads` raises, and the chunk fails (rc 4) — this test
    goes red.
    """
    import re as _re

    monkeypatch.setattr(check_items_cli, "CLASSIFIER_CHUNK_SIZE", 1)

    # CLASSIFIER_CHUNK_SIZE=1 -> 2 single-group chunks: chunk 1 = [g1] (the
    # one under test), chunk 2 = [g2] (must succeed normally, proving the
    # rest of the run is unaffected).
    groups = [_ref_group(1), _ref_group(2)]

    call_index = {"n": 0}

    def fake_run(*args, **kwargs):
        idx = call_index["n"]
        call_index["n"] += 1

        prompt = kwargs.get("input", "")
        out_match = _re.search(r"JSON to (/\S+?)\.?(?:\s|$)", prompt)
        assert out_match, f"output path missing from prompt: {prompt[:200]!r}"
        out_path = out_match.group(1)

        if idx == 0:
            # chunk 1 (g1), attempt 1: garbage on disk + non-zero rc, so
            # _dispatch_classifier_chunk returns early and never reads it.
            Path(out_path).write_text("{partial", encoding="utf-8")
            return _make_stub(returncode=3, stdout="")

        if idx == 1:
            # chunk 1 (g1), attempt 2: nothing written to the file; success
            # comes via the stdout fallback.
            return _make_stub(returncode=0, stdout=json.dumps([_classifier_result("g1")]))

        # chunk 2 (g2): ordinary single-attempt success, own output file.
        Path(out_path).write_text(json.dumps([_classifier_result("g2")]), encoding="utf-8")
        return _make_stub(returncode=0, stdout="")

    monkeypatch.setattr(check_items_cli.subprocess, "run", fake_run)

    output_path = str(tmp_path / "out.json")
    rc = check_items_cli.run_classifier(_payload(groups), output_path)

    assert rc == 0
    out = json.loads(Path(output_path).read_text())
    assert {r["group_id"] for r in out} == {"g1", "g2"}
