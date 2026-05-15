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
    "commits_text": "fix session_log race condition abc1234",
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
        "commits": ["abc1234 fix session_log race condition"],
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
