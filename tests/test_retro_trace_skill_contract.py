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
from skill_shell import fenced_blocks, isolation_problems, REFUSED_FORMS

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
    """The example traces-file content the skill shows, parsed."""
    match = re.search(r"for example `(?P<json>\{\"traces\": .*?\})`", STEP_75)
    assert match, "example traces file content not found in Step 7.5"
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


def test_apply_takes_traces_from_a_file_not_a_heredoc():
    """#518: the traces JSON goes in a file the Write tool made, never in the command."""
    block = _step_75_apply_block()
    assert "--traces-file '<TRACES_FILE>'" in block
    assert "<<" not in block


def _first_bash_block(section: str) -> str:
    return fenced_blocks(section)[0].body


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


def _step_8_block() -> str:
    step8 = _section(CHECK_ITEMS, "## Step 8", "## Step 9")
    block = _first_bash_block(step8)
    assert "closed_applied.json" in block
    return block


def _run_step_8(tmp_path, block: str):
    scope = tmp_path / "scope.json"
    scope.write_text("{}", encoding="utf-8")
    script = f"scope_path={scope}\n{block}\n"
    return subprocess.run(["bash", "-c", script], env=dict(os.environ), capture_output=True,
                          text=True, timeout=30, stdin=subprocess.DEVNULL, cwd=tmp_path)


def test_step_8_closed_applied_block_writes_owner_only_json(tmp_path):
    flipped = [{"file": "/v/n.md", "line": 8, "text": "- [x] t", "ref": "o/r#5"}]
    block = _step_8_block().replace("\n<CLOSED_FLIPPED_JSON>\n", "\n" + json.dumps(flipped) + "\n")
    block = block.replace("<eof4>", "a1b2")
    proc = _run_step_8(tmp_path, block)
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "closed_applied.json"
    assert json.loads(out.read_text()) == flipped
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_step_8_block_refuses_when_placeholder_left_in(tmp_path):
    block = _step_8_block().replace("<eof4>", "a1b2")
    proc = _run_step_8(tmp_path, block)
    assert proc.returncode != 0
    assert "CLOSED_FLIPPED_JSON" in proc.stderr
    assert not (tmp_path / "closed_applied.json").exists()


def test_step_8_block_refuses_bad_json_and_non_lists(tmp_path):
    for bad in ("{not json", '{"a": 1}', "[1, 2]"):
        block = _step_8_block().replace("\n<CLOSED_FLIPPED_JSON>\n", "\n" + bad + "\n").replace("<eof4>", "a1b2")
        proc = _run_step_8(tmp_path, block)
        assert proc.returncode == 1, (bad, proc.stderr)
        assert not (tmp_path / "closed_applied.json").exists()


def _step_75_apply_block() -> str:
    for block in fenced_blocks(STEP_75):
        if 'retro_trace.py" apply' in block.body:
            return block.body
    raise AssertionError("apply bash block not found in Step 7.5")


def _run_step_75_apply(tmp_path, swap: bool):
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"obsidian-brain": {"source": {"source": "directory", "path": str(REPO_ROOT)},
                            "installLocation": str(REPO_ROOT)}}), encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    note = vault / "claude-insights" / "retro.md"
    note.write_text(
        "---\ntype: claude-retro\nproject: demo\ntags:\n  - claude/retro\n---\n"
        "\n## Process Improvements\n- [ ] One untraced item.\n", encoding="utf-8")
    traces = tmp_path / "traces.json"
    traces.write_text(json.dumps(_step_75_payload()), encoding="utf-8")
    block = _step_75_apply_block().replace("'<TRACES_FILE>'", f"'{traces}'")
    block = block.replace("'<REPO_ROOT>'", f"'{REPO_ROOT}'")
    vault_arg, note_arg = '"$VAULT_PATH"', "'<NOTE_PATH>'"
    assert vault_arg in block and note_arg in block
    if swap:  # scratch copy only; the SKILL file is untouched
        block = block.replace(vault_arg, "@@V@@").replace(note_arg, vault_arg).replace("@@V@@", note_arg)
    block = block.replace('"$VAULT_PATH"', f"'{vault}'").replace("'<NOTE_PATH>'", f"'{note}'")
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL, cwd=tmp_path)
    return proc, note


def test_step_75_documented_apply_command_runs_and_traces_the_note(tmp_path):
    proc, note = _run_step_75_apply(tmp_path, swap=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["untraced"] == 0
    assert retro_trace.parse_items(note.read_text(encoding="utf-8"))[0]["trace"] is not None


def test_step_75_apply_test_catches_swapped_vault_and_note_arguments(tmp_path):
    proc, note = _run_step_75_apply(tmp_path, swap=True)
    assert proc.returncode != 0
    assert retro_trace.parse_items(note.read_text(encoding="utf-8"))[0]["trace"] is None


def _bash_blocks(text: str) -> list[str]:
    return [b.body for b in fenced_blocks(text)]


def test_retro_blocks_run_in_a_worktree_isolated_session():
    """#518: a worktree-isolated session refuses git in a command substitution and
    `|| { ...; }` groups (probed on Claude Code 2.1.330); guards use
    `if ! test -f ...; then ...; fi`. No block may carry either, nor a heredoc."""
    blocks = _bash_blocks(RETRO)
    assert len(blocks) >= 9
    for block in blocks:
        assert isolation_problems(block, heredoc=True) == [], block[:120]
    assert "```bash\ngit rev-parse --show-toplevel\n```" in RETRO


def test_isolation_checker_flags_every_refused_form_and_passes_a_clean_block():
    """Positive control: the checker the real test uses really fires."""
    for name, (body, expected) in REFUSED_FORMS.items():
        assert expected in isolation_problems(body, heredoc=False), name
    heredoc = "cat <<'EOF'\nx\nEOF"
    assert isolation_problems(heredoc, heredoc=True) == ["heredoc"]
    assert isolation_problems(heredoc) == []
    clean = "cd '/repo'\nif ! test -f f; then\n  echo missing >&2\n  exit 1\nfi\nx=$(python3 -c 'print(1)')"
    assert isolation_problems(clean) == []


def test_shell_block_collector_takes_every_fence_spelling():
    text = ("```bash\na\n```\n```sh\nb\n```\n```shell\nc\n```\n```zsh\nd\n```\n"
            "   ```bash\n   e\n   ```\n```python\nnot shell\n```\n"
            "~~~bash\nf\n~~~\n  ~~~sh\n  g\n  ~~~\n~~~python\nnot shell\n~~~\n")
    assert [b.strip() for b in _bash_blocks(text)] == ["a", "b", "c", "d", "e", "f", "g"]


def _needs_repo_cd(block: str) -> bool:
    """Blocks that load this repo's hooks. The datetime one-liner uses stdlib only,
    so it never touches the repo."""
    return "python3" in block and any(
        marker in block for marker in ("$HOOKS", "_ob_hooks", "obsidian_utils", "retro_trace"))


def test_every_hooks_block_starts_with_the_repo_root_cd():
    """#518: each block that runs this repo's Python starts with `cd '<REPO_ROOT>'`,
    because blocks run in separate shells and the cwd may not be the repo."""
    needing = [b for b in _bash_blocks(RETRO) if _needs_repo_cd(b)]
    for block in needing:
        assert block.lstrip("\n").lstrip().splitlines()[0] == "cd '<REPO_ROOT>'", block[:120]
    # Positive control: the blocks that exist today. A new one must be added here.
    assert len(needing) == 8
    # Blocks that need no cd, by content: the git probe and the `ls -l` check.
    exempt = [b for b in _bash_blocks(RETRO) if not _needs_repo_cd(b)]
    assert any(b.strip() == "git rev-parse --show-toplevel" for b in exempt)
    assert any(b.strip() == "ls -l '<NOTE_PATH>'" for b in exempt)


def _step_1_config_block() -> str:
    for block in _bash_blocks(RETRO):
        if 'print("VAULT=" + c["vault_path"])' in block:
            return block
    raise AssertionError("Step 1 config block not found")


def _run_step_1(tmp_path, block: str, precreate_work: bool = True):
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    (home / ".claude" / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"obsidian-brain": {"source": {"source": "directory", "path": str(REPO_ROOT)},
                            "installLocation": str(REPO_ROOT)}}), encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    (home / ".claude" / "obsidian-brain-config.json").write_text(
        json.dumps({"vault_path": str(vault)}), encoding="utf-8")
    work = home / ".claude" / "obsidian-brain"
    if precreate_work:
        work.mkdir()
        work.chmod(0o755)
    script = block.replace("'<REPO_ROOT>'", f"'{REPO_ROOT}'")
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL, cwd=tmp_path)
    return proc, home, work


def test_step_1_config_block_prints_work_and_makes_it_private(tmp_path):
    proc, home, work = _run_step_1(tmp_path, _step_1_config_block())
    assert proc.returncode == 0, proc.stderr
    assert f"WORK={work}" in proc.stdout.splitlines()
    assert f"VAULT={tmp_path / 'vault'}" in proc.stdout.splitlines()
    assert stat.S_IMODE(work.stat().st_mode) == 0o700


def test_step_1_config_block_creates_work_private_when_it_does_not_exist(tmp_path):
    """Fresh HOME: `~/.claude/obsidian-brain` is absent before the run."""
    block = _step_1_config_block()
    proc, home, work = _run_step_1(tmp_path, block, precreate_work=False)
    assert proc.returncode == 0, proc.stderr
    assert f"WORK={work}" in proc.stdout.splitlines()
    assert stat.S_IMODE(work.stat().st_mode) == 0o700


def test_step_1_test_catches_a_missing_chmod_and_a_missing_work_line(tmp_path):
    """Mutation controls on a scratch copy of the block (the SKILL file is untouched)."""
    block = _step_1_config_block()
    assert "os.chmod(_w, 0o700)\n" in block and 'print("WORK=" + _w)\n' in block
    proc, _home, work = _run_step_1(tmp_path / "a", block.replace("os.chmod(_w, 0o700)\n", ""))
    assert stat.S_IMODE(work.stat().st_mode) == 0o755
    proc, _home, _work = _run_step_1(tmp_path / "b", block.replace('print("WORK=" + _w)\n', ""))
    assert not any(ln.startswith("WORK=") for ln in proc.stdout.splitlines())


def test_retro_write_takes_the_note_from_a_body_file():
    """#517: the note body comes from a file the Write tool made, never the command."""
    writes = [b for b in _bash_blocks(RETRO) if 'note_writer.py" write' in b]
    assert len(writes) == 1
    assert "--body-file '<BODY_FILE>'" in writes[0]
    assert "keep `<BODY_FILE>`" in RETRO
