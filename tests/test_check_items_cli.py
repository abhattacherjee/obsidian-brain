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
        # ACTIVE/STALE/REVIEW are all valid synthetic (L2, no-sub-agent)
        # outcomes (#264 Task 1 added REVIEW for distinctive-token items
        # with no anchor and no evidence overlap); the assertion that
        # matters here is prefiltered=True + LOW confidence, not which of
        # the three labels a given fixture text lands on.
        assert record["classification"] in ("ACTIVE", "STALE", "REVIEW")
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
        # ACTIVE/STALE/REVIEW are all valid synthetic outcomes (#264 Task 1
        # added REVIEW for distinctive-token items with no anchor/evidence);
        # what matters here is that the sub-agent was skipped, not the label.
        assert record["classification"] in ("ACTIVE", "STALE", "REVIEW")
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
# REVIEW classification (#264 Task 1)
# ---------------------------------------------------------------------------

def test_validate_classifier_payload_accepts_review():
    """_validate_classifier_payload must accept the new REVIEW classification
    value — the surfacing bucket for unanchored, distinctive open items that
    the classifier is not confident enough to call DONE or bury as ACTIVE."""
    from check_items_cli import _validate_classifier_payload
    payload = [{
        "group_id": "g1", "classification": "REVIEW", "confidence": "LOW",
        "canonical_text": "Return to feature/pull-to-refresh-v2 worktree",
        "evidence_citation": "similarly-named branch feature/pull-to-refresh-v2 (deleted)",
        "action_required": None,
    }]
    assert _validate_classifier_payload(payload) is True


def test_validate_classifier_payload_rejects_unknown_classification():
    """Regression: adding REVIEW must not loosen validation generally — an
    unrecognised classification string is still rejected."""
    from check_items_cli import _validate_classifier_payload
    payload = [{
        "group_id": "g1", "classification": "MAYBE", "confidence": "LOW",
        "canonical_text": "x", "evidence_citation": None, "action_required": None,
    }]
    assert _validate_classifier_payload(payload) is False


def test_validate_classifier_payload_done_shape_unaffected_by_review_addition():
    """Regression: adding REVIEW to the valid-classification set must not
    change validation of pre-existing classifications. A DONE payload with a
    null evidence_citation still passes SHAPE validation unchanged — citation-
    *content* enforcement (e.g. requiring DONE to always cite something
    concrete) is a separate concern _validate_classifier_payload does not
    implement today, and this task does not add it."""
    from check_items_cli import _validate_classifier_payload
    payload = [{
        "group_id": "g1", "classification": "DONE", "confidence": "HIGH",
        "canonical_text": "x", "evidence_citation": None, "action_required": None,
    }]
    assert _validate_classifier_payload(payload) is True


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


# ---------------------------------------------------------------------------
# Task 6 / #173: integration test — active-project fixture achieves >= 10 prefiltered
# ---------------------------------------------------------------------------

def test_l2_prefilter_active_project_fixture(tmp_path, monkeypatch, capsys):
    """Regression repro: 21-group active-project payload must achieve
    prefiltered >= 10 under the zone-aware completion-signal rules (#173)."""
    import json
    import os
    import sys

    fixtures = os.path.join(
        os.path.dirname(__file__),
        "fixtures",
        "check_items_prefilter",
    )
    with open(os.path.join(fixtures, "active_project_evidence.json")) as f:
        evidence = json.load(f)
    with open(os.path.join(fixtures, "active_project_partition.json")) as f:
        partition = json.load(f)

    # Build the run_classifier stdin payload shape
    payload = {
        "groups": partition["needs"],
        "evidence": evidence,
    }
    stdin_json = json.dumps(payload)
    output_path = str(tmp_path / "classifications.json")

    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "on")
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    import check_items_cli

    # Stub the sub-agent dispatch to a no-op that writes an empty result list to
    # the per-call output_path (parsed from the prompt). The integration test
    # asserts on the L2 partition only — what the sub-agent would have
    # classified is out of scope for #173.
    import re as _re

    def _fake_run(*_a, **_k):
        prompt = _k.get("input", "")
        out_match = _re.search(r"JSON to (/\S+?)\.?(?:\s|$)", prompt)
        if out_match:
            Path(out_match.group(1)).write_text(json.dumps([]), encoding="utf-8")

        class R:
            returncode = 0
            stdout = json.dumps([])
            stderr = ""
        return R()

    monkeypatch.setattr(check_items_cli.subprocess, "run", _fake_run)
    check_items_cli.run_classifier(stdin_json, output_path)

    err = capsys.readouterr().err
    # Telemetry shape: "[check-items-cli] classifier: total=21 cache_hit=- prefiltered=P subagent=S wall=Ts"
    line = next((ln for ln in err.splitlines() if "[check-items-cli] classifier:" in ln), None)
    assert line is not None, f"no classifier telemetry line found in stderr:\n{err}"
    import re
    m = re.search(r"prefiltered=(\d+)", line)
    assert m is not None, f"prefiltered count missing in telemetry: {line!r}"
    prefiltered = int(m.group(1))
    assert prefiltered >= 10, (
        f"L2 prefilter under-delivers on active project fixture: "
        f"prefiltered={prefiltered} (need >= 10). Line: {line!r}"
    )


# ---------------------------------------------------------------------------
# Task 7 / #173: bridge narrowing — closed-issue and merged-PR bodies excluded
# ---------------------------------------------------------------------------

def test_bridge_project_evidence_omits_issue_body_from_closed_issues_text():
    """Closed-issue and merged-PR bodies are activity content, not completion
    statements. _bridge_project_evidence must extract titles only, so Rule 2
    of has_classifiable_evidence does not over-trigger on active projects (#173)."""
    import sys
    import os
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    from check_items_cli import _bridge_project_evidence

    evidence = {
        "proj-x": {
            "closed_issues": [
                {
                    "body": "We should implement resolveLastSessionAnchor properly in a follow-up.",
                    "title": "fix(query): widget-z double-count regression",
                    "number": 39,
                    "closedAt": "2026-05-11T21:00:00Z",
                    "url": "https://example/issues/39",
                }
            ],
            "merged_prs": [
                {
                    "title": "feat(dashboard): last session chip",
                    "number": 41,
                    "mergedAt": "2026-05-13T19:00:00Z",
                    "url": "https://example/pull/41",
                }
            ],
        }
    }
    bridged = _bridge_project_evidence(evidence, "proj-x")
    closed_text = bridged["closed_issues_text"]
    merged_text = bridged["merged_prs_text"]

    # Title and number are included
    assert "widget-z double-count regression" in closed_text
    assert "#39" in closed_text
    assert "last session chip" in merged_text
    assert "#41" in merged_text

    # Body content is NOT included (it's activity, not completion)
    assert "resolveLastSessionAnchor" not in closed_text
    assert "follow-up" not in closed_text

    # URL and timestamps are also excluded (noise)
    assert "https://example" not in closed_text
    assert "2026-05-11" not in closed_text


# ---------------------------------------------------------------------------
# #264 Task 2 follow-up: wire fold_tags_and_paths_into_completion_zone into
# _bridge_project_evidence so no-anchor items whose only ground-truth signal
# is a git tag or changed file path are promoted to the sub-agent classifier.
# ---------------------------------------------------------------------------

def test_bridge_project_evidence_folds_changed_paths_promotes_no_anchor_item():
    """A no-#N-anchor open item whose distinctive token appears ONLY in
    proj_evidence['changed_paths'] (not commits/merged_prs/closed_issues/
    releases/changelog/fts) must still reach has_classifiable_evidence()==True
    once _bridge_project_evidence folds changed_paths into the completion
    zone via fold_tags_and_paths_into_completion_zone()."""
    import sys
    import os
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    from check_items_cli import _bridge_project_evidence
    from check_items_prefilter import has_classifiable_evidence

    group = {
        "group_id": "g-pull-to-refresh",
        "project": "obsidian-brain",
        "representative": "Wire up PullToRefresh on the feed screen",
        "instances": [{"file": "note.md", "line": 1,
                        "text": "Wire up PullToRefresh on the feed screen",
                        "mtime": 0.0}],
    }
    evidence = {
        "obsidian-brain": {
            "commits": [],
            "merged_prs": [],
            "closed_issues": [],
            "releases": [],
            "changelog_excerpt": "",
            "fts_mentions": {},
            "tags": [],
            # Distinctive token appears ONLY here.
            "changed_paths": ["src/components/PullToRefresh.tsx"],
        }
    }

    bridged = _bridge_project_evidence(evidence, "obsidian-brain")

    assert has_classifiable_evidence(group, bridged) is True, (
        "changed_paths evidence should promote a no-anchor item to the "
        "sub-agent classifier once folded into the completion zone"
    )


def test_bridge_project_evidence_folds_tags_promotes_no_anchor_item():
    """Same as above, but the distinctive token appears only in
    proj_evidence['tags'] (a recent git tag), not changed_paths."""
    import sys
    import os
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    from check_items_cli import _bridge_project_evidence
    from check_items_prefilter import has_classifiable_evidence

    group = {
        "group_id": "g-pull-to-refresh-tag",
        "project": "obsidian-brain",
        "representative": "Ship the PullToRefresh gesture",
        "instances": [{"file": "note.md", "line": 1,
                        "text": "Ship the PullToRefresh gesture",
                        "mtime": 0.0}],
    }
    evidence = {
        "obsidian-brain": {
            "commits": [],
            "merged_prs": [],
            "closed_issues": [],
            "releases": [],
            "changelog_excerpt": "",
            "fts_mentions": {},
            "tags": ["v3.3.0-PullToRefresh"],
            "changed_paths": [],
        }
    }

    bridged = _bridge_project_evidence(evidence, "obsidian-brain")

    assert has_classifiable_evidence(group, bridged) is True, (
        "tags evidence should promote a no-anchor item to the sub-agent "
        "classifier once folded into the completion zone"
    )


def test_bridge_project_evidence_no_match_stays_unclassifiable_with_tags_and_paths():
    """Regression/negative: an item with no distinctive-token match anywhere
    (including tags/changed_paths) must still return False — the fold must
    not manufacture false-positive matches."""
    import sys
    import os
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    from check_items_cli import _bridge_project_evidence
    from check_items_prefilter import has_classifiable_evidence

    group = {
        "group_id": "g-unrelated",
        "project": "obsidian-brain",
        "representative": "Investigate the flaky nightly job",
        "instances": [{"file": "note.md", "line": 1,
                        "text": "Investigate the flaky nightly job",
                        "mtime": 0.0}],
    }
    evidence = {
        "obsidian-brain": {
            "commits": [],
            "merged_prs": [],
            "closed_issues": [],
            "releases": [],
            "changelog_excerpt": "",
            "fts_mentions": {},
            "tags": ["v3.3.0"],
            "changed_paths": ["src/components/PullToRefresh.tsx"],
        }
    }

    bridged = _bridge_project_evidence(evidence, "obsidian-brain")

    assert has_classifiable_evidence(group, bridged) is False, (
        "unrelated tags/changed_paths must not falsely promote an item"
    )


def test_strip_unreleased_removes_unreleased_section():
    import sys
    import os
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    from check_items_cli import _strip_unreleased_section

    cl = (
        "# Changelog\n\n"
        "## [Unreleased]\n"
        "### Added\n"
        "- WIP feature foo_bar in module-x\n"
        "- Pending widget for module-y\n\n"
        "## [0.2.0] - 2026-05-11\n"
        "### Fixed\n"
        "- widget-z double-counting in query layer\n"
    )
    out = _strip_unreleased_section(cl)
    assert "foo_bar" not in out
    assert "Pending widget" not in out
    # Released sections preserved
    assert "[0.2.0]" in out
    assert "widget-z double-counting" in out


def test_strip_unreleased_no_unreleased_section_returns_unchanged():
    import sys
    import os
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    from check_items_cli import _strip_unreleased_section

    cl = "# Changelog\n\n## [0.1.0] - 2026-04-30\n- Initial release\n"
    assert _strip_unreleased_section(cl) == cl


def test_strip_unreleased_empty_input_returns_empty():
    import sys
    import os
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    from check_items_cli import _strip_unreleased_section
    assert _strip_unreleased_section("") == ""
    assert _strip_unreleased_section(None) == ""


def test_strip_unreleased_unreleased_at_end_with_no_released_section():
    """Edge case: only `[Unreleased]` section present, nothing released yet."""
    import sys
    import os
    HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
    if HOOKS not in sys.path:
        sys.path.insert(0, HOOKS)
    from check_items_cli import _strip_unreleased_section
    cl = (
        "# Changelog\n\n"
        "## [Unreleased]\n"
        "### Added\n"
        "- WIP foo_bar\n"
    )
    out = _strip_unreleased_section(cl)
    assert "foo_bar" not in out
    # Header may or may not be retained — either is fine, but WIP content must be gone


# ---------------------------------------------------------------------------
# Classifier chunking — sequential dispatch for large group counts (#160/#173)
# ---------------------------------------------------------------------------

def _make_chunking_fake_run(output_path: str, return_rcs: list | None = None,
                            timeout_on: int | None = None):
    """Build a subprocess.run stub that handles chunked dispatch.

    For each call, the stub:
      - parses the embedded `<output-json-path>` from the prompt (which is
        either output_path for single dispatch, or a temp `.classout.json`
        file for chunked dispatch)
      - reads the corresponding `<input-json-path>` to learn which group_ids
        are in this chunk
      - writes one DONE-classification record per group_id to the per-chunk
        output path
    Optionally returns a non-zero rc for the call index in `return_rcs`, or
    raises TimeoutExpired for the call index in `timeout_on`.
    """
    import re as _re
    call_index = {"n": 0}

    def fake_run(*args, **kwargs):
        idx = call_index["n"]
        call_index["n"] += 1

        if timeout_on is not None and idx == timeout_on:
            raise subprocess.TimeoutExpired(cmd=args[0] if args else "claude", timeout=1)

        prompt = kwargs.get("input", "")
        in_match = _re.search(r"(/\S+\.classin\.json)", prompt)
        out_match = _re.search(r"JSON to (/\S+?)\.?(?:\s|$)", prompt)
        assert in_match, f"input path missing from prompt: {prompt[:200]!r}"
        assert out_match, f"output path missing from prompt: {prompt[:200]!r}"
        in_path = in_match.group(1)
        out_path = out_match.group(1)

        chunk_payload = json.loads(Path(in_path).read_text())
        chunk_results = [
            {
                "group_id": g["group_id"],
                "classification": "DONE",
                "confidence": "HIGH",
                "canonical_text": g["representative"],
                "evidence_citation": "commit abc1234",
                "action_required": None,
            }
            for g in chunk_payload["groups"]
        ]
        Path(out_path).write_text(json.dumps(chunk_results), encoding="utf-8")

        mock_cp = MagicMock()
        mock_cp.returncode = (return_rcs[idx] if return_rcs and idx < len(return_rcs) else 0)
        mock_cp.stderr = ""
        mock_cp.stdout = ""
        return mock_cp

    return fake_run, call_index


# Need subprocess import in the test module for TimeoutExpired (already imported
# transitively via patch target, but make it explicit here).
import subprocess  # noqa: E402


def test_classifier_chunking_under_threshold_single_dispatch(tmp_path, monkeypatch, capsys):
    """N groups at exactly the chunk-size threshold → single dispatch, no `chunks=` in telemetry."""
    import check_items_cli

    monkeypatch.setattr("check_items_cli.CLASSIFIER_CHUNK_SIZE", 25)
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")  # force all → classifier

    groups = [_make_group(f"g{i}", f"Fix bug number {i}") for i in range(25)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    fake_run, calls = _make_chunking_fake_run(output_path)
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert calls["n"] == 1, f"Expected 1 dispatch for 25 groups (threshold), got {calls['n']}"

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 25
    assert [r["group_id"] for r in out] == [f"g{i}" for i in range(25)]

    captured = capsys.readouterr()
    assert "chunks=" not in captured.err, (
        f"Telemetry must not include chunks= for single-dispatch path; got: {captured.err!r}"
    )


def test_classifier_chunking_above_threshold_splits(tmp_path, monkeypatch, capsys):
    """N > chunk_size groups → ceil(N/chunk) sequential dispatches, results merged."""
    import check_items_cli

    monkeypatch.setattr("check_items_cli.CLASSIFIER_CHUNK_SIZE", 25)
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    # 60 groups → 3 chunks of 25 / 25 / 10
    groups = [_make_group(f"g{i:03d}", f"Fix bug number {i}") for i in range(60)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    fake_run, calls = _make_chunking_fake_run(output_path)
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert calls["n"] == 3, f"Expected 3 chunks for 60 groups (chunk_size=25), got {calls['n']}"

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 60
    # Input order preserved across chunk boundaries
    assert [r["group_id"] for r in out] == [f"g{i:03d}" for i in range(60)]

    captured = capsys.readouterr()
    assert "chunks=3" in captured.err, (
        f"Telemetry must include chunks=3 for 3-chunk dispatch; got: {captured.err!r}"
    )
    assert "chunking 60 groups into 3 chunk(s)" in captured.err, (
        f"Chunking announcement missing from stderr; got: {captured.err!r}"
    )


def test_classifier_chunking_chunk_failure_returns_rc(tmp_path, monkeypatch):
    """If any chunk times out, run_classifier returns rc=3 (all-or-nothing)."""
    import check_items_cli

    monkeypatch.setattr("check_items_cli.CLASSIFIER_CHUNK_SIZE", 10)
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    # 30 groups → 3 chunks of 10. Time out the 2nd chunk.
    groups = [_make_group(f"g{i:03d}", f"Fix bug number {i}") for i in range(30)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    fake_run, calls = _make_chunking_fake_run(output_path, timeout_on=1)
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 3, f"Expected rc=3 (timeout) when chunk 2/3 times out, got {rc}"
    # Dispatch stopped at the failing chunk (no chunk 3 attempted)
    assert calls["n"] == 2, f"Expected 2 dispatches before bail-out, got {calls['n']}"


def test_classifier_chunking_env_override(tmp_path, monkeypatch, capsys):
    """CLASSIFIER_CHUNK_SIZE override via module attribute splits at the new threshold."""
    import check_items_cli

    monkeypatch.setattr("check_items_cli.CLASSIFIER_CHUNK_SIZE", 5)
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    # 12 groups, chunk_size=5 → 3 chunks of 5/5/2
    groups = [_make_group(f"g{i:02d}", f"Fix bug number {i}") for i in range(12)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    fake_run, calls = _make_chunking_fake_run(output_path)
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert calls["n"] == 3, f"Expected 3 chunks for 12 groups (chunk_size=5), got {calls['n']}"

    captured = capsys.readouterr()
    assert "chunks=3" in captured.err


def test_classifier_chunk_size_env_clamped_to_minimum():
    """CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE values below 1 are clamped to 1 at import time."""
    import importlib
    import check_items_cli
    saved = os.environ.get("CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE")
    try:
        os.environ["CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE"] = "0"
        importlib.reload(check_items_cli)
        assert check_items_cli.CLASSIFIER_CHUNK_SIZE == 1
        os.environ["CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE"] = "-5"
        importlib.reload(check_items_cli)
        assert check_items_cli.CLASSIFIER_CHUNK_SIZE == 1
    finally:
        if saved is None:
            os.environ.pop("CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE", None)
        else:
            os.environ["CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE"] = saved
        importlib.reload(check_items_cli)


def test_classifier_chunking_boundary_at_chunk_size_plus_one(tmp_path, monkeypatch):
    """Exactly CHUNK_SIZE+1 groups → chunked dispatch (2 chunks of N/1).

    Guards against a regression flipping `len > N` to `len >= N` or vice
    versa: the single-dispatch test at N=25 and the multi-dispatch test
    at N=60 both pass under the wrong inequality.
    """
    import check_items_cli

    monkeypatch.setattr("check_items_cli.CLASSIFIER_CHUNK_SIZE", 25)
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    groups = [_make_group(f"g{i:03d}", f"Fix bug number {i}") for i in range(26)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    fake_run, calls = _make_chunking_fake_run(output_path)
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert calls["n"] == 2, (
        f"Expected 2 chunks at the threshold+1 boundary (26 groups, "
        f"chunk_size=25), got {calls['n']}"
    )

    out = json.loads(Path(output_path).read_text())
    assert len(out) == 26
    assert [r["group_id"] for r in out] == [f"g{i:03d}" for i in range(26)]


def test_classifier_chunking_merge_preserves_input_order_across_chunks(tmp_path, monkeypatch):
    """Sub-agent returning each chunk's groups in REVERSE order → final
    output is still in original input order.

    Exercises the group_id-keyed merge step (lines ~681) for chunked
    dispatch. The earlier order-preservation test relied on chunks
    coming back in input order, which would still pass under a buggy
    merge that keyed on chunk-position rather than group_id.
    """
    import check_items_cli
    import re as _re

    monkeypatch.setattr("check_items_cli.CLASSIFIER_CHUNK_SIZE", 5)
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    groups = [_make_group(f"g{i:03d}", f"Fix bug number {i}") for i in range(12)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    def fake_run_reversed(*args, **kwargs):
        prompt = kwargs.get("input", "")
        in_match = _re.search(r"(/\S+\.classin\.json)", prompt)
        out_match = _re.search(r"JSON to (/\S+?)\.?(?:\s|$)", prompt)
        chunk_payload = json.loads(Path(in_match.group(1)).read_text())
        # Reverse the sub-agent's output order — production must reorder by group_id.
        chunk_results = [
            {
                "group_id": g["group_id"],
                "classification": "DONE",
                "confidence": "HIGH",
                "canonical_text": g["representative"],
                "evidence_citation": "commit abc1234",
                "action_required": None,
            }
            for g in reversed(chunk_payload["groups"])
        ]
        Path(out_match.group(1)).write_text(json.dumps(chunk_results))
        mock_cp = MagicMock()
        mock_cp.returncode = 0
        mock_cp.stderr = ""
        mock_cp.stdout = ""
        return mock_cp

    with patch("check_items_cli.subprocess.run", side_effect=fake_run_reversed):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    out = json.loads(Path(output_path).read_text())
    assert [r["group_id"] for r in out] == [f"g{i:03d}" for i in range(12)], (
        f"Final output must be in original input order, got: "
        f"{[r['group_id'] for r in out]!r}"
    )


def test_classifier_chunking_failure_cleans_up_chunk_outputs(tmp_path, monkeypatch):
    """Chunk-failure path's `finally` must unlink every chunk output temp
    that was created, including the one from the failing chunk.

    Guards against a future refactor that moves cleanup inside the loop
    body or drops the `finally`. Without cleanup, 0o600 temp files
    containing partial classifier output would leak under the workdir.
    """
    import check_items_cli

    monkeypatch.setattr("check_items_cli.CLASSIFIER_CHUNK_SIZE", 10)
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    # Probe the workdir contents before
    workdir = Path(check_items_cli._safe_workdir())
    pre_classouts = set(workdir.glob("*.classout.json"))

    groups = [_make_group(f"g{i:03d}", f"Fix bug number {i}") for i in range(30)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    fake_run, _ = _make_chunking_fake_run(output_path, timeout_on=1)
    with patch("check_items_cli.subprocess.run", side_effect=fake_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 3
    post_classouts = set(workdir.glob("*.classout.json"))
    leaked = post_classouts - pre_classouts
    assert not leaked, (
        f"Chunk output temp files leaked on failure path: "
        f"{[p.name for p in leaked]}"
    )


def test_classifier_chunking_uses_per_chunk_model_picking(tmp_path, monkeypatch, capsys):
    """When chunking, model is picked per chunk (not from the total count).

    Without this, 60-group payloads would propagate sonnet to every chunk
    even though each chunk has <=30 groups and qualifies for haiku.
    Empirically observed in the 23:07 telemetry replay: 25-group sonnet
    chunks still timed out, while haiku at the same size finishes well
    under SUBAGENT_TIMEOUT_SEC.
    """
    import re as _re
    import check_items_cli

    monkeypatch.setattr("check_items_cli.CLASSIFIER_CHUNK_SIZE", 25)
    monkeypatch.setenv("CHECK_ITEMS_PREFILTER", "off")

    # 60 groups (total) > 30 (sonnet threshold), but each chunk has 25 or
    # 10 (haiku threshold). All chunks must pick haiku.
    groups = [_make_group(f"g{i:03d}", f"Fix bug number {i}") for i in range(60)]
    payload_str = _make_payload(groups, EVIDENCE_WITH_MATCH_TEXT)
    output_path = str(tmp_path / "out.json")

    models_seen: list = []

    def model_recording_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        # cmd shape: ["claude", "-p", "--model", model]
        if len(cmd) >= 4 and cmd[2] == "--model":
            models_seen.append(cmd[3])
        # Write empty result to the chunk output path
        prompt = kwargs.get("input", "")
        out_match = _re.search(r"JSON to (/\S+?)\.?(?:\s|$)", prompt)
        if out_match:
            in_match = _re.search(r"(/\S+\.classin\.json)", prompt)
            chunk_payload = json.loads(Path(in_match.group(1)).read_text())
            chunk_results = [
                {
                    "group_id": g["group_id"],
                    "classification": "ACTIVE",
                    "confidence": "LOW",
                    "canonical_text": g["representative"],
                    "evidence_citation": None,
                    "action_required": None,
                }
                for g in chunk_payload["groups"]
            ]
            Path(out_match.group(1)).write_text(json.dumps(chunk_results))
        mock_cp = MagicMock()
        mock_cp.returncode = 0
        mock_cp.stderr = ""
        mock_cp.stdout = ""
        return mock_cp

    with patch("check_items_cli.subprocess.run", side_effect=model_recording_run):
        rc = check_items_cli.run_classifier(payload_str, output_path)

    assert rc == 0
    assert len(models_seen) == 3, (
        f"Expected 3 chunks for 60 groups (chunk_size=25), got {len(models_seen)}"
    )
    assert all(m == "haiku" for m in models_seen), (
        f"Every chunk must pick haiku when chunk size <=30; got models: "
        f"{models_seen!r}"
    )
