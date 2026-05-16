"""Integration tests for check_items_cli.run_classifier() with L2 prefilter wired in."""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "hooks")
if HOOKS_DIR not in sys.path:
    sys.path.insert(0, HOOKS_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_group(group_id: str, text: str, mtime: float = 0.0, project: str = "obsidian-brain") -> dict:
    """Minimal group with instances carrying mtime."""
    return {
        "group_id": group_id,
        "project": project,
        "representative": text,
        "instances": [{"file": "note.md", "line": 1, "text": text, "mtime": mtime}],
    }


def _make_payload(groups: list, evidence: dict | None = None) -> str:
    return json.dumps({"groups": groups, "evidence": evidence or {}})


# Evidence using _text-suffixed keys (as expected by has_classifiable_evidence)
EVIDENCE_EMPTY_TEXT = {
    "commits_text": "",
    "merged_prs_text": "",
    "closed_issues_text": "",
    "releases_text": "",
    "changelog_excerpt": "",
    "fts_mentions_text": "",
}

EVIDENCE_WITH_MATCH_TEXT = {
    "commits_text": "fixed session_log race condition",
    "merged_prs_text": "",
    "closed_issues_text": "",
    "releases_text": "",
    "changelog_excerpt": "",
    "fts_mentions_text": "",
}

# Evidence using bare keys (as produced by open_item_dedup.py's gather_session_evidence),
# nested under a project key — this is the live production shape.
EVIDENCE_BARE_KEYS_WITH_MATCH = {
    "obsidian-brain": {
        "commits": ["abc1234 fixed session_log race condition"],
        "merged_prs": [],
        "closed_issues": [],
        "releases": [],
        "changelog_excerpt": "",
    }
}

EVIDENCE_BARE_KEYS_EMPTY = {
    "obsidian-brain": {
        "commits": [],
        "merged_prs": [],
        "closed_issues": [],
        "releases": [],
        "changelog_excerpt": "",
    }
}


# ---------------------------------------------------------------------------
# Test: all groups have evidence → sub-agent called on all
# ---------------------------------------------------------------------------

def test_all_groups_have_evidence_subagent_called(tmp_path, monkeypatch):
    """All groups have token overlap with evidence → sub-agent receives all groups."""
    import check_items_cli

    # Group text that overlaps with EVIDENCE_WITH_MATCH_TEXT
    groups = [
        _make_group("g1", "Fix session_log race condition"),
        _make_group("g2", "Fix session_log race deadlock"),
    ]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)

    output_path = str(tmp_path / "out.json")

    # Sub-agent output: two classification records
    subagent_result = [
        {
            "group_id": "g1",
            "classification": "DONE",
            "confidence": "HIGH",
            "canonical_text": "Fix session_log race condition",
            "evidence_citation": "commit abc1234",
            "action_required": None,
        },
        {
            "group_id": "g2",
            "classification": "DONE",
            "confidence": "HIGH",
            "canonical_text": "Fix session_log race deadlock",
            "evidence_citation": "commit abc1234",
            "action_required": None,
        },
    ]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stderr = ""
    mock_cp.stdout = ""

    def fake_run(*args, **kwargs):
        # Write the sub-agent result to output_path
        Path(output_path).write_text(json.dumps(subagent_result), encoding="utf-8")
        return mock_cp

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", side_effect=fake_run) as mock_sub:
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert mock_sub.call_count == 1, "Sub-agent should be called once for all groups"

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 2
    assert out[0]["group_id"] == "g1"
    assert out[1]["group_id"] == "g2"


# ---------------------------------------------------------------------------
# Test: no groups have evidence → sub-agent NOT called, all synthetic
# ---------------------------------------------------------------------------

def test_no_groups_have_evidence_subagent_not_called(tmp_path, monkeypatch):
    """No token overlap, no refs → sub-agent skipped; output is all synthetic records."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)  # 10 days ago → ACTIVE
    groups = [
        _make_group("g1", "Investigate dispatcher discovery", mtime_recent),
        _make_group("g2", "Explore vault growth patterns", mtime_recent),
    ]
    payload_str = _make_payload(groups, EVIDENCE_EMPTY_TEXT)

    output_path = str(tmp_path / "out.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    mock_sub = MagicMock()
    with patch("check_items_cli.subprocess.run", mock_sub):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert mock_sub.call_count == 0, "Sub-agent must NOT be invoked when all items are prefiltered"

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 2
    for record in out:
        assert record["classification"] in ("ACTIVE", "STALE")
        assert record["confidence"] == "LOW"
        assert record.get("prefiltered") is True


# ---------------------------------------------------------------------------
# Test: mixed groups → sub-agent called only on subset; output order preserved
# ---------------------------------------------------------------------------

def test_mixed_groups_order_preserved(tmp_path, monkeypatch):
    """Some groups have evidence, some don't. Output must be in input order."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [
        _make_group("g1", "Investigate dispatcher discovery", mtime_recent),  # no evidence
        _make_group("g2", "Fix session_log race condition", mtime_recent),    # has evidence
        _make_group("g3", "Explore vault growth patterns", mtime_recent),     # no evidence
    ]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)

    output_path = str(tmp_path / "out.json")

    subagent_result = [
        {
            "group_id": "g2",
            "classification": "DONE",
            "confidence": "HIGH",
            "canonical_text": "Fix session_log race condition",
            "evidence_citation": "commit abc1234",
            "action_required": None,
        }
    ]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stderr = ""
    mock_cp.stdout = ""

    subagent_call_count = []

    def fake_run(*args, **kwargs):
        subagent_call_count.append(1)
        Path(output_path).write_text(json.dumps(subagent_result), encoding="utf-8")
        return mock_cp

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert len(subagent_call_count) == 1, "Sub-agent called exactly once for the one evidenced group"

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 3, "All three input groups must appear in output"

    # Verify input order is preserved: g1, g2, g3
    assert out[0]["group_id"] == "g1"
    assert out[1]["group_id"] == "g2"
    assert out[2]["group_id"] == "g3"

    # g2 came from sub-agent (DONE), g1 and g3 are synthetic
    assert out[1]["classification"] == "DONE"
    assert out[0].get("prefiltered") is True
    assert out[2].get("prefiltered") is True


# ---------------------------------------------------------------------------
# Test: L2 disabled → sub-agent called on all groups (existing behavior)
# ---------------------------------------------------------------------------

def test_prefilter_disabled_subagent_called_for_all(tmp_path, monkeypatch):
    """When CHECK_ITEMS_PREFILTER=off, all groups go to sub-agent regardless of evidence."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [
        _make_group("g1", "Investigate dispatcher discovery", mtime_recent),
        _make_group("g2", "Explore vault growth patterns", mtime_recent),
    ]
    payload_str = _make_payload(groups, EVIDENCE_EMPTY_TEXT)

    output_path = str(tmp_path / "out.json")

    subagent_result = [
        {
            "group_id": "g1",
            "classification": "ACTIVE",
            "confidence": "LOW",
            "canonical_text": "Investigate dispatcher discovery",
            "evidence_citation": None,
            "action_required": None,
        },
        {
            "group_id": "g2",
            "classification": "ACTIVE",
            "confidence": "LOW",
            "canonical_text": "Explore vault growth patterns",
            "evidence_citation": None,
            "action_required": None,
        },
    ]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stderr = ""
    mock_cp.stdout = ""

    def fake_run(*args, **kwargs):
        Path(output_path).write_text(json.dumps(subagent_result), encoding="utf-8")
        return mock_cp

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")
    with patch("check_items_cli.subprocess.run", side_effect=fake_run) as mock_sub:
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert mock_sub.call_count == 1, "Sub-agent must be called when prefilter is disabled"

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Test: bare-key evidence (production shape from open_item_dedup.py) → bridging works
# ---------------------------------------------------------------------------

def test_bare_key_evidence_bridging_with_match(tmp_path, monkeypatch):
    """Evidence in bare-key production shape {project: {commits: [...], ...}} is bridged
    correctly so has_classifiable_evidence finds token overlap and routes to sub-agent."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [
        _make_group("g1", "Fix session_log race condition", mtime_recent),
    ]
    # Use the live production evidence shape (bare keys, nested under project)
    payload_str = _make_payload(groups, EVIDENCE_BARE_KEYS_WITH_MATCH)

    output_path = str(tmp_path / "out.json")

    subagent_result = [
        {
            "group_id": "g1",
            "classification": "DONE",
            "confidence": "HIGH",
            "canonical_text": "Fix session_log race condition",
            "evidence_citation": "commit abc1234",
            "action_required": None,
        }
    ]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stderr = ""
    mock_cp.stdout = ""

    def fake_run(*args, **kwargs):
        Path(output_path).write_text(json.dumps(subagent_result), encoding="utf-8")
        return mock_cp

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", side_effect=fake_run) as mock_sub:
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    # With match evidence, group should go to sub-agent (not synthetic)
    assert mock_sub.call_count == 1, "Sub-agent should be called — evidence has token overlap"

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 1
    assert out[0]["classification"] == "DONE"
    assert out[0].get("prefiltered") is not True


def test_bare_key_evidence_bridging_no_match(tmp_path, monkeypatch):
    """Evidence in bare-key production shape with empty values → group is synthetic."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [
        _make_group("g1", "Investigate dispatcher discovery", mtime_recent),
    ]
    payload_str = _make_payload(groups, EVIDENCE_BARE_KEYS_EMPTY)

    output_path = str(tmp_path / "out.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    mock_sub = MagicMock()
    with patch("check_items_cli.subprocess.run", mock_sub):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert mock_sub.call_count == 0, "Sub-agent must NOT be called — no evidence"

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 1
    assert out[0].get("prefiltered") is True


# ---------------------------------------------------------------------------
# Tests for telemetry line
# ---------------------------------------------------------------------------

def test_telemetry_line_appears_in_stderr(tmp_path, monkeypatch, capsys):
    """run_classifier emits exactly one telemetry line to stderr."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [_make_group("g1", "Investigate dispatcher discovery", mtime_recent)]
    payload_str = _make_payload(groups, EVIDENCE_EMPTY_TEXT)
    output_path = str(tmp_path / "out.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", MagicMock()):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    captured = capsys.readouterr()
    telemetry_lines = [
        l for l in captured.err.splitlines()
        if l.startswith("[check-items-cli] classifier:")
    ]
    assert len(telemetry_lines) == 1, f"Expected exactly 1 telemetry line, got: {telemetry_lines}"


def test_telemetry_line_has_all_five_fields(tmp_path, monkeypatch, capsys):
    """Telemetry line contains total, cache_hit, prefiltered, subagent, wall fields."""
    import check_items_cli
    import re

    mtime_recent = time.time() - (10 * 86400)
    groups = [
        _make_group("g1", "Investigate dispatcher discovery", mtime_recent),
        _make_group("g2", "Explore vault growth patterns", mtime_recent),
    ]
    payload_str = _make_payload(groups, EVIDENCE_EMPTY_TEXT)
    output_path = str(tmp_path / "out.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", MagicMock()):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    captured = capsys.readouterr()
    telemetry_line = next(
        (l for l in captured.err.splitlines() if "[check-items-cli] classifier:" in l),
        None,
    )
    assert telemetry_line is not None

    # Parse and verify all five fields are present and have parseable values
    assert re.search(r'total=\d+', telemetry_line), f"Missing total= in: {telemetry_line}"
    assert re.search(r'cache_hit=[-\d]+', telemetry_line), f"Missing cache_hit= in: {telemetry_line}"
    assert re.search(r'prefiltered=\d+', telemetry_line), f"Missing prefiltered= in: {telemetry_line}"
    assert re.search(r'subagent=\d+', telemetry_line), f"Missing subagent= in: {telemetry_line}"
    assert re.search(r'wall=\d+s', telemetry_line), f"Missing wall= in: {telemetry_line}"

    # Verify numeric values are consistent with the all-synthetic scenario
    total_m = re.search(r'total=(\d+)', telemetry_line)
    prefiltered_m = re.search(r'prefiltered=(\d+)', telemetry_line)
    subagent_m = re.search(r'subagent=(\d+)', telemetry_line)
    assert int(total_m.group(1)) == 2
    assert int(prefiltered_m.group(1)) == 2
    assert int(subagent_m.group(1)) == 0


# ---------------------------------------------------------------------------
# Test: empty to_classify path — subprocess never invoked
# ---------------------------------------------------------------------------

def test_all_synthetic_subprocess_never_invoked(tmp_path, monkeypatch):
    """When all groups are prefiltered by L2, subprocess.run is never called.

    This is the critical optimization: no claude -p invocations when the vault
    has no completion evidence for any open item. The all-synthetic case must
    exit 0 and write valid output without touching the subprocess machinery.
    """
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [
        _make_group("g1", "Investigate dispatcher discovery", mtime_recent),
        _make_group("g2", "Explore vault growth patterns", mtime_recent),
        _make_group("g3", "Document architecture decisions", mtime_recent),
    ]
    payload_str = _make_payload(groups, EVIDENCE_EMPTY_TEXT)
    output_path = str(tmp_path / "out.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    mock_sub = MagicMock()
    with patch("check_items_cli.subprocess.run", mock_sub):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0, f"Expected exit 0, got {rc}"
    assert mock_sub.call_count == 0, (
        f"subprocess.run was called {mock_sub.call_count} time(s) — "
        "expected 0 when all groups have no evidence"
    )

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 3, "All 3 groups must appear in output"
    group_ids = [r["group_id"] for r in out]
    assert group_ids == ["g1", "g2", "g3"], "Input order must be preserved"
    for record in out:
        assert record.get("prefiltered") is True
        assert record["classification"] in ("ACTIVE", "STALE")
        assert record["confidence"] == "LOW"


# ---------------------------------------------------------------------------
# I1: _bridge_project_evidence fallthrough warning
# ---------------------------------------------------------------------------

def test_bridge_project_evidence_fallthrough_emits_warning(monkeypatch, capsys):
    """Unrecognized evidence shape triggers a stderr WARN line (I1)."""
    import check_items_cli

    # Malformed evidence: neither bare-key nested nor _text-suffixed
    malformed_evidence = {"some_random_key": "value", "another_key": 42}
    check_items_cli._bridge_project_evidence(malformed_evidence, "some-project")

    captured = capsys.readouterr()
    assert "[check-items-cli] WARN:" in captured.err, (
        f"Expected WARN on stderr for unrecognized evidence shape; got: {captured.err!r}"
    )
    assert "unrecognized shape" in captured.err
    assert "some-project" in captured.err


def test_bridge_project_evidence_no_warning_on_shape_a(monkeypatch, capsys):
    """No WARN emitted for shape A (project-nested bare keys)."""
    import check_items_cli

    evidence = {
        "obsidian-brain": {
            "commits": ["abc1234 fix something"],
            "merged_prs": [],
            "closed_issues": [],
            "releases": [],
            "changelog_excerpt": "",
        }
    }
    result = check_items_cli._bridge_project_evidence(evidence, "obsidian-brain")

    captured = capsys.readouterr()
    assert "WARN" not in captured.err, f"Unexpected WARN for valid shape A: {captured.err!r}"
    assert "commits_text" in result


def test_bridge_project_evidence_no_warning_on_shape_b(monkeypatch, capsys):
    """No WARN emitted for shape B (_text-suffixed keys)."""
    import check_items_cli

    evidence = {
        "commits_text": "fix session_log race condition abc1234",
        "merged_prs_text": "",
        "closed_issues_text": "",
        "releases_text": "",
        "changelog_excerpt": "",
        "fts_mentions_text": "",
    }
    result = check_items_cli._bridge_project_evidence(evidence, "some-project")

    captured = capsys.readouterr()
    assert "WARN" not in captured.err, f"Unexpected WARN for valid shape B: {captured.err!r}"
    assert result == evidence


def test_bridge_project_evidence_no_warning_on_empty(monkeypatch, capsys):
    """Empty evidence dict ({}) → returns {} without WARN (empty is not malformed)."""
    import check_items_cli

    result = check_items_cli._bridge_project_evidence({}, "some-project")

    captured = capsys.readouterr()
    # Empty dict with no keys is the normal "no evidence" case — should NOT warn
    assert result == {}
    assert "WARN" not in captured.err, (
        f"Unexpected WARN for empty evidence dict: {captured.err!r}"
    )


def test_bridge_project_evidence_multi_project_no_warn_when_project_missing(monkeypatch, capsys):
    """Multi-project shape A payload with a missing project key → {} without WARN (I1 fix).

    When evidence = {"projA": {...}, "projB": {...}} and the group asks for
    "projC", the payload IS shape A — just without this project's data.
    This should silently return {} rather than emitting a WARN, because the
    shape is recognized and the answer is legitimately "no evidence for projC".
    """
    import check_items_cli

    evidence = {
        "projA": {
            "commits": ["abc1234 fix something"],
            "merged_prs": [],
            "closed_issues": [],
            "releases": [],
            "changelog_excerpt": "",
        },
        "projB": {
            "commits": [],
            "merged_prs": [{"title": "Add feature X", "number": 42}],
            "closed_issues": [],
            "releases": [],
            "changelog_excerpt": "",
        },
    }

    result = check_items_cli._bridge_project_evidence(evidence, "projC")

    captured = capsys.readouterr()
    assert result == {}, (
        f"Expected {{}} for project not in multi-project payload; got: {result!r}"
    )
    assert "WARN" not in captured.err, (
        f"Unexpected WARN for recognized shape A with missing project: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# I2: group_id=None collision in run_classifier
# ---------------------------------------------------------------------------

def test_run_classifier_missing_group_id_returns_error(tmp_path, monkeypatch):
    """Groups with group_id=None cause run_classifier to return a non-zero error code (I2)."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [
        _make_group("g1", "Fix session_log race condition", mtime_recent),
        # group with group_id=None — malformed input
        {
            "group_id": None,
            "project": "obsidian-brain",
            "representative": "Some orphaned item",
            "instances": [{"file": "note.md", "line": 1, "text": "Some orphaned item", "mtime": mtime_recent}],
        },
    ]
    payload_str = _make_payload(groups, EVIDENCE_EMPTY_TEXT)
    output_path = str(tmp_path / "out.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", MagicMock()):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc != 0, f"Expected non-zero rc for groups with group_id=None, got {rc}"


def test_run_classifier_missing_group_id_logs_to_stderr(tmp_path, monkeypatch, capsys):
    """Groups with group_id=None produce a diagnostic message on stderr (I2)."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [
        {
            "group_id": None,
            "project": "obsidian-brain",
            "representative": "An item without a valid group_id",
            "instances": [{"file": "note.md", "line": 1, "text": "item", "mtime": mtime_recent}],
        }
    ]
    payload_str = _make_payload(groups, EVIDENCE_EMPTY_TEXT)
    output_path = str(tmp_path / "out.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", MagicMock()):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc != 0
    captured = capsys.readouterr()
    # Should emit a clear diagnostic mentioning group_id
    assert "group_id" in captured.err.lower() or "group_id" in captured.err, (
        f"Expected 'group_id' in stderr; got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# I3: file-path branch must apply _validate_classifier_payload
# ---------------------------------------------------------------------------

def test_file_path_branch_validates_schema(tmp_path, monkeypatch):
    """If sub-agent writes invalid JSON to output file, run_classifier returns rc=4 (I3)."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [_make_group("g1", "Fix session_log race condition", mtime_recent)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    # Sub-agent writes a dict instead of a list (wrong shape) to output_path
    invalid_result = {"wrong": "shape"}

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stderr = ""
    mock_cp.stdout = ""

    def fake_run(*args, **kwargs):
        Path(output_path).write_text(json.dumps(invalid_result), encoding="utf-8")
        return mock_cp

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 4, f"Expected rc=4 for invalid file-path output shape, got {rc}"


def test_file_path_branch_validates_schema_missing_fields(tmp_path, monkeypatch):
    """File-path branch rejects list of dicts missing required classifier fields (I3)."""
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)
    groups = [_make_group("g1", "Fix session_log race condition", mtime_recent)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    # Missing required fields like 'evidence_citation', 'action_required', etc.
    invalid_result = [{"group_id": "g1", "classification": "DONE"}]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stderr = ""
    mock_cp.stdout = ""

    def fake_run(*args, **kwargs):
        Path(output_path).write_text(json.dumps(invalid_result), encoding="utf-8")
        return mock_cp

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 4, f"Expected rc=4 for file-path output missing required fields, got {rc}"


# ---------------------------------------------------------------------------
# I4: non-dict records in outer telemetry (open_item_dedup.py)
# ---------------------------------------------------------------------------

def test_non_dict_records_dropped_with_warning(tmp_path, monkeypatch, capsys):
    """Non-dict entries in classifier output emit a WARN via production code (I4).

    Invokes open_item_dedup.classify_groups_with_agent() for real, with a mocked
    subprocess that writes JSON containing a stray non-dict entry to out_path.
    Asserts:
      (1) WARN is emitted to stderr by the production pre-validation check.
      (2) The function returns [] because _validate_classifier_response rejects
          any list containing a non-dict (warn-and-reject semantics).
    Deleting the production WARN block in open_item_dedup.py would cause this
    test to fail on assertion (1).
    """
    import open_item_dedup as oid

    mtime_recent = time.time() - (10 * 86400)  # 10 days ago → ACTIVE
    merged_groups = [
        _make_group("g1", "Fix session_log race condition", mtime_recent),
    ]
    evidence = {
        "obsidian-brain": {
            "commits": ["abc1234 fix session_log race condition"],
            "merged_prs": [],
            "closed_issues": [],
            "releases": [],
            "changelog_excerpt": "",
        }
    }

    # Malformed sub-agent response: valid dict + stray non-dict string entry.
    # _validate_classifier_response will reject this (non-dict present), but the
    # pre-validation WARN block should fire first.
    malformed_response = [
        {
            "group_id": "g1",
            "classification": "DONE",
            "confidence": "HIGH",
            "canonical_text": "Fix session_log race condition",
            "evidence_citation": "commit abc1234",
            "action_required": None,
        },
        "this-is-not-a-dict",  # stray non-dict entry
    ]

    mock_cp = MagicMock()
    mock_cp.returncode = 0
    mock_cp.stderr = ""
    mock_cp.stdout = ""

    def fake_run(*args, **kwargs):
        # Extract out_path from the CLI args: ["python3", cli_path, "classifier", out_path]
        cli_args = args[0]
        out_path = cli_args[3]
        Path(out_path).write_text(json.dumps(malformed_response), encoding="utf-8")
        return mock_cp

    with patch("open_item_dedup.subprocess.run", side_effect=fake_run):
        result = oid.classify_groups_with_agent(merged_groups, evidence)

    captured = capsys.readouterr()
    assert "WARN" in captured.err, (
        f"Expected WARN in stderr for non-dict records; got: {captured.err!r}"
    )
    assert "non-dict" in captured.err, (
        f"Expected 'non-dict' in warning; got: {captured.err!r}"
    )
    assert "1" in captured.err, (
        f"Expected count '1' in warning; got: {captured.err!r}"
    )
    # validator rejects the response → heuristic-fallback → returns []
    assert result == [], (
        f"Expected [] when response contains non-dict records; got: {result!r}"
    )
    assert oid.get_last_classifier_mode() == "heuristic-fallback", (
        f"Expected heuristic-fallback mode after non-dict rejection; "
        f"got: {oid.get_last_classifier_mode()!r}"
    )


# ---------------------------------------------------------------------------
# I5: evidence={} integration — all groups become synthetic via run_classifier
# ---------------------------------------------------------------------------

def test_empty_evidence_dict_all_groups_synthetic(tmp_path, monkeypatch):
    """When evidence={} (bridge returns {}), all groups become synthetic with prefiltered=True (I5).

    This exercises the full _bridge_project_evidence → has_classifiable_evidence →
    synthetic_classification path when the evidence payload is completely absent.
    All groups must appear in the output with prefiltered=True and rc=0.
    """
    import check_items_cli

    mtime_recent = time.time() - (10 * 86400)  # 10 days ago → ACTIVE
    groups = [
        _make_group("g1", "Investigate dispatcher discovery", mtime_recent),
        _make_group("g2", "Explore vault growth patterns", mtime_recent),
        _make_group("g3", "Document architecture decisions", mtime_recent),
    ]
    # evidence={}: no evidence key at all in the payload
    payload_str = json.dumps({"groups": groups, "evidence": {}})
    output_path = str(tmp_path / "out.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    mock_sub = MagicMock()
    with patch("check_items_cli.subprocess.run", mock_sub):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0, f"Expected rc=0 for evidence={{}}, got {rc}"
    assert mock_sub.call_count == 0, (
        f"Sub-agent should not be invoked when evidence is empty; "
        f"called {mock_sub.call_count} times"
    )

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 3, f"Expected 3 output records, got {len(out)}"

    for record in out:
        assert record.get("prefiltered") is True, (
            f"Expected prefiltered=True for all groups with empty evidence; "
            f"record={record}"
        )
    # Input order preserved
    assert [r["group_id"] for r in out] == ["g1", "g2", "g3"]
