"""Classifier failures remain visible under either invoking host.

Issue #297 diagnostics cover retries, missing output and invalid schema.
Native calls reject incomplete results rather than publish partial success.
Spec: .superpowers/sdd/2026-08-10-issue-297-classifier-degradation-plan/
task-3-brief.md
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)

import open_item_dedup as oid  # noqa: E402


# ---------------------------------------------------------------------------
# Test isolation: _LAST_CLASSIFIER_MODE is a module-level global. Without
# resetting it, tests pass or fail depending on execution order.
# Per memory technical_import_time_statedir_needs_autouse_isolation.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_classifier_mode():
    oid._LAST_CLASSIFIER_MODE = "ok"
    yield
    oid._LAST_CLASSIFIER_MODE = "ok"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_bound_cli(monkeypatch, fake, operation="classifier"):
    """Mock the trusted wrapper call; native transport is tested separately."""
    import check_items_cli
    def invoke(payload, output):
        result = fake(["trusted-cli", "wrapper", operation, output], input=payload)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode
    monkeypatch.setattr(check_items_cli,
                        "run_classifier" if operation == "classifier" else "run_semantic_merge", invoke)


def _make_group(group_id: str, text: str, mtime: float = 0.0, project: str = "obsidian-brain") -> dict:
    return {
        "group_id": group_id,
        "project": project,
        "representative": text,
        "members": [{"file": "note.md", "line": 1, "text": text, "mtime": mtime}],
    }


def _classifier_record(group_id: str, classification: str = "DONE") -> dict:
    return {
        "group_id": group_id,
        "classification": classification,
        "confidence": "HIGH",
        "canonical_text": f"canonical {group_id}",
        "evidence_citation": f"evidence for {group_id}",
        "action_required": None,
    }


EVIDENCE = {"obsidian-brain": {}}


def _out_path_from_cmd(cmd) -> str:
    # subprocess.run is invoked as:
    #   ["python3", cli_path, "classifier", str(out_path)]
    return cmd[3]


# ---------------------------------------------------------------------------
# Test 1: failed attempt (non-zero rc / no output) forwards child stderr
# ---------------------------------------------------------------------------

def test_failed_attempt_forwards_child_stderr(selected_host_context, monkeypatch, capsys):
    merged_groups = [_make_group("g1", "Fix bug #87")]

    mock_cp = MagicMock()
    mock_cp.returncode = 6
    mock_cp.stdout = ""
    mock_cp.stderr = (
        "[check-items-cli] chunk 1/3 ...classifier wrote no output..."
    )

    def fake_run(cmd, *args, **kwargs):
        # Deliberately do NOT write out_path — simulates a hard failure.
        return mock_cp

    _mock_bound_cli(monkeypatch, fake_run)

    result = oid.classify_groups_with_agent(merged_groups, EVIDENCE)

    captured = capsys.readouterr()
    assert result == []
    assert "rc=6" in captured.err, (
        f"Expected 'rc=6' in stderr; got: {captured.err!r}"
    )
    assert "classifier wrote no output" in captured.err, (
        f"Expected child stderr substring forwarded; got: {captured.err!r}"
    )
    assert oid.get_last_classifier_mode() == "heuristic-fallback"


# ---------------------------------------------------------------------------
# Test 2: total failure prints the terminal diagnostic
# ---------------------------------------------------------------------------

def test_total_failure_prints_terminal_diagnostic(selected_host_context, monkeypatch, capsys):
    merged_groups = [_make_group("g1", "Fix bug #87"), _make_group("g2", "Other item")]

    mock_cp = MagicMock()
    mock_cp.returncode = 6
    mock_cp.stdout = ""
    mock_cp.stderr = "[check-items-cli] chunk 1/3 ...classifier wrote no output..."

    def fake_run(cmd, *args, **kwargs):
        return mock_cp

    _mock_bound_cli(monkeypatch, fake_run)

    result = oid.classify_groups_with_agent(merged_groups, EVIDENCE)

    captured = capsys.readouterr()
    assert result == []
    assert "falling back to the token-overlap heuristic" in captured.err, (
        f"Expected terminal fallback diagnostic; got: {captured.err!r}"
    )
    assert "2" in captured.err, (
        f"Expected group count '2' named in terminal diagnostic; got: {captured.err!r}"
    )
    assert oid.get_last_classifier_mode() == "heuristic-fallback"


# ---------------------------------------------------------------------------
# Test 3: incomplete native results cannot report partial success
# ---------------------------------------------------------------------------

def test_partial_native_result_is_rejected(selected_host_context, monkeypatch, capsys):
    merged_groups = [
        _make_group("g1", "Fix bug #87"),
        _make_group("g2", "Other item"),
        _make_group("g3", "Third item"),
    ]

    # Classifier only classified g1 and g2; g3 never came back.
    payload = [_classifier_record("g1"), _classifier_record("g2")]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stdout = ""
    mock_cp.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        out_path = _out_path_from_cmd(cmd)
        Path(out_path).write_text(json.dumps(payload), encoding="utf-8")
        return mock_cp

    _mock_bound_cli(monkeypatch, fake_run)

    result = oid.classify_groups_with_agent(merged_groups, EVIDENCE)

    captured = capsys.readouterr()
    assert result == []
    assert oid.get_last_classifier_mode() == "heuristic-fallback"
    assert "PARTIAL" not in captured.err


# ---------------------------------------------------------------------------
# Test 4: full result sets `ok` mode, no PARTIAL/FAILED noise
# ---------------------------------------------------------------------------

def test_full_result_sets_ok_mode(selected_host_context, monkeypatch, capsys):
    merged_groups = [
        _make_group("g1", "Fix bug #87"),
        _make_group("g2", "Other item"),
        _make_group("g3", "Third item"),
    ]
    payload = [_classifier_record("g1"), _classifier_record("g2"), _classifier_record("g3")]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stdout = ""
    mock_cp.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        out_path = _out_path_from_cmd(cmd)
        Path(out_path).write_text(json.dumps(payload), encoding="utf-8")
        return mock_cp

    _mock_bound_cli(monkeypatch, fake_run)

    result = oid.classify_groups_with_agent(merged_groups, EVIDENCE)

    captured = capsys.readouterr()
    assert len(result) == 3
    assert oid.get_last_classifier_mode() == "ok"
    assert "PARTIAL" not in captured.err
    assert "FAILED" not in captured.err


# ---------------------------------------------------------------------------
# Test 5: schema-validation failure is reported, not silent
# ---------------------------------------------------------------------------

def test_validation_failure_is_reported(selected_host_context, monkeypatch, capsys):
    merged_groups = [_make_group("g1", "Fix bug #87")]

    # Structurally invalid: missing required fields.
    invalid_payload = [{"group_id": "g1", "classification": "DONE"}]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stdout = ""
    mock_cp.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        out_path = _out_path_from_cmd(cmd)
        Path(out_path).write_text(json.dumps(invalid_payload), encoding="utf-8")
        return mock_cp

    _mock_bound_cli(monkeypatch, fake_run)

    result = oid.classify_groups_with_agent(merged_groups, EVIDENCE)

    captured = capsys.readouterr()
    assert result == []
    assert "validation" in captured.err.lower(), (
        f"Expected the validation failure to be named in stderr; got: {captured.err!r}"
    )
    assert oid.get_last_classifier_mode() == "heuristic-fallback"
