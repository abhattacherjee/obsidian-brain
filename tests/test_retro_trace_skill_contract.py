"""Contract tests: the /retro and /check-items skills call retro_trace the way it works (#470)."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import retro_trace

REPO_ROOT = Path(__file__).resolve().parents[1]
RETRO = (REPO_ROOT / "skills" / "retro" / "SKILL.md").read_text(encoding="utf-8")
CHECK_ITEMS = (REPO_ROOT / "skills" / "check-items" / "SKILL.md").read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    body = text[text.index(start):]
    return body[:body.index(end)]


STEP_75 = _section(RETRO, "### Step 7.5", "### Step 8")


def test_step_75_numbers_items_then_applies_traces_before_clearing():
    items = STEP_75.index('retro_trace.py" items')
    apply = STEP_75.index('retro_trace.py" apply')
    clear = STEP_75.index("clear_retro_classification_pending(")
    assert items < apply < clear


def test_step_75_names_every_trace_kind_the_cli_accepts():
    for kind in ('"kind": "issue"', '"kind": "memory"', '"kind": "path"', '"kind": "skipped"'):
        assert kind in STEP_75


def _step_75_payload() -> dict:
    """The JSON line inside the skill's apply heredoc, parsed."""
    match = re.search(
        r"<<'OB_TRACES_EOF_<eof4>'\n(?P<json>.*)\nOB_TRACES_EOF_<eof4>\n", STEP_75)
    assert match, "apply heredoc not found in Step 7.5"
    return json.loads(match["json"])


def test_step_75_example_payload_applies_to_a_real_note(tmp_path):
    payload = _step_75_payload()
    note = tmp_path / "retro.md"
    note.write_text(
        "---\ntype: claude-retro\nproject: demo\ntags:\n  - claude/retro\n---\n"
        "\n## Process Improvements\n- [ ] One untraced item.\n", encoding="utf-8")
    assert retro_trace.untraced_count(note) == 1
    result = retro_trace.apply_traces(str(tmp_path), note.name, payload["traces"])
    assert result["written"] is True and result["untraced"] == 0
    items = retro_trace.parse_items(note.read_text(encoding="utf-8"))
    assert items[0]["trace"]["kind"] == payload["traces"][0]["kind"]
    assert retro_trace.untraced_count(note) == 0


def test_declined_is_a_trace_and_cancel_writes_nothing():
    assert "skipped: user declined" in STEP_75
    assert "Cancel is not decline" in STEP_75


def test_clear_step_reports_blocked_when_items_are_untraced():
    assert "retro_gate_untraced" in STEP_75
    assert 'print(f"blocked: {untraced}' in STEP_75


def test_apply_heredoc_terminator_is_per_invocation():
    assert "<<'OB_TRACES_EOF_<eof4>'" in STEP_75
    assert "\nOB_TRACES_EOF_<eof4>\n" in STEP_75


def _first_bash_block(section: str) -> str:
    return section.split("```bash\n", 1)[1].split("\n```", 1)[0]


def test_check_items_step_2b_block_runs_and_lists_a_closed_candidate(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    vault = tmp_path / "vault"
    insights = vault / "claude-insights"
    insights.mkdir(parents=True)
    note = insights / "2026-10-29-retro-demo.md"
    item = "- [ ] File it → [o/r#5](https://github.com/o/r/issues/5)"
    note.write_text(
        "---\ntype: claude-retro\nproject: demo\ntracked_issues: [\"o/r#5\"]\n---\n"
        f"\n## Process Improvements\n{item}\n", encoding="utf-8")
    (home / ".claude" / "obsidian-brain-config.json").write_text(
        json.dumps({"vault_path": str(vault)}), encoding="utf-8")
    # The FORM_A resolver reads directory-source marketplaces under HOME.
    (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"obsidian-brain": {"source": {"source": "directory", "path": str(REPO_ROOT)},
                            "installLocation": str(REPO_ROOT)}}), encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    gh = fakebin / "gh"
    gh.write_text('#!/bin/sh\necho \'{"state":"CLOSED","stateReason":"COMPLETED"}\'\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    scope_dir = tmp_path / "scope"
    scope_dir.mkdir()
    scope = scope_dir / "scope.json"
    scope.write_text(json.dumps({"mode": "vault", "project": None}), encoding="utf-8")

    block = _first_bash_block(_section(CHECK_ITEMS, "## Step 2b", "## Step 3"))
    script = f'scope_path={scope}\n{block}\n'
    env = {**os.environ, "HOME": str(home), "PATH": f"{fakebin}:{os.environ['PATH']}"}
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL,
                          cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "!!" not in proc.stderr, proc.stderr
    result = json.loads((scope_dir / "closed_candidates.json").read_text())
    assert result["errors"] == []
    assert [(c["file"], c["ref"], c["preselect"]) for c in result["candidates"]] == [
        (str(note.resolve()), "o/r#5", True)]
    # Guard: the block really ran this repo's hooks, not a stale cache copy.
    assert f"closed_path={scope_dir / 'closed_candidates.json'}" in proc.stdout


def test_check_items_shows_and_flips_closed_candidates():
    assert "=== Closed on GitHub ===" in CHECK_ITEMS
    assert "[closed on GH]" in CHECK_ITEMS
    assert "[closed on GH: not planned]" in CHECK_ITEMS
    step8 = _section(CHECK_ITEMS, "## Step 8", "## Step 9")
    assert "Closed on GitHub" in step8 and "dry run" in step8


def test_step_8_writes_and_step_9_reads_closed_applied():
    step8 = _section(CHECK_ITEMS, "## Step 8", "## Step 9")
    step9 = _section(CHECK_ITEMS, "## Step 9", "## Step 10")
    assert "closed_applied.json" in step8 and "CLOSED_FLIPPED" in step8
    assert "closed_applied.json" in step9
    assert "applied += len(closed_applied)" in step9
    assert "closed_on_github=closed_applied" in step9


def test_step_8_closed_applied_block_writes_owner_only_json(tmp_path):
    step8 = _section(CHECK_ITEMS, "## Step 8", "## Step 9")
    block = step8.split("```bash\n", 1)[1].split("\n```", 1)[0]
    assert "closed_applied.json" in block
    # The skill ships an inline `[]` default; the caller replaces it.
    block = block.replace("CLOSED_FLIPPED='[]' ", "")
    scope = tmp_path / "scope.json"
    scope.write_text("{}", encoding="utf-8")
    flipped = [{"file": "/v/n.md", "line": 8, "text": "- [x] t", "ref": "o/r#5"}]
    env = {**os.environ, "CLOSED_FLIPPED": json.dumps(flipped)}
    script = f'scope_path={scope}\n{block}\n'
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                          text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "closed_applied.json"
    assert json.loads(out.read_text()) == flipped
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
