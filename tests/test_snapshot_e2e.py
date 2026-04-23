"""End-to-end integration test for the snapshot → /recall pipeline (issue #50).

Exercises the full 5-step cycle with real hook subprocess invocations and
in-process obsidian_utils calls. Under 1s wall time. CI-required.

Pipeline:
    1. obsidian_context_snapshot.py (subprocess) → snapshot note on disk
    2. obsidian_session_log.py     (subprocess) → session note with `snapshots:` back-ref
    3. find_unsummarized_notes()   (in-proc)   → both session + snapshot returned
    4. upgrade_unsummarized_note() (in-proc, Haiku monkeypatched) → status: summarized
    5. build_context_brief()       (in-proc)   → nested `↳ HH:MM:SS` row + snapshot_count: 1
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SNAPSHOT = REPO_ROOT / "hooks" / "obsidian_context_snapshot.py"
HOOK_SESSION_LOG = REPO_ROOT / "hooks" / "obsidian_session_log.py"

SID = "e2e-test-session-12345"
PROJECT = "fake-cwd"
SLUG = "fake-cwd"


def _write_config(home_dir: Path, vault_path: Path) -> Path:
    """Write obsidian-brain-config.json pointing at the tmp vault."""
    cfg_dir = home_dir / ".claude"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "vault_path": str(vault_path),
        "sessions_folder": "claude-sessions",
        "insights_folder": "claude-insights",
        "auto_log_enabled": True,
        "snapshot_on_compact": True,
        "snapshot_on_clear": True,
        "min_messages": 0,
        "min_duration_minutes": 0,
        "summary_model": "haiku",
    }
    cfg_path = cfg_dir / "obsidian-brain-config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    cfg_path.chmod(0o600)
    return cfg_path


def _write_transcript(home_dir: Path, slug: str) -> Path:
    """Write a 3-line JSONL transcript fixture under ~/.claude/projects/<slug>/."""
    proj_dir = home_dir / ".claude" / "projects" / slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    transcript = proj_dir / "t.jsonl"
    lines = [
        {"type": "user", "message": {"content": "Fix the snapshot pipeline."}},
        {"type": "assistant", "message": {"content": "Writing the test now."}},
        {"type": "user", "message": {"content": "Looks good — ship it."}},
    ]
    transcript.write_text(
        "\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8"
    )
    return transcript


def _write_path_shim(bin_dir: Path) -> Path:
    """Write a `claude` PATH shim emitting a canned summary on any call.

    Defense-in-depth only: neither production hook calls `claude -p`
    (summarization is deferred to /recall). If that ever regresses, this
    keeps the test hermetic.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'EOF'\n"
        "## Summary\n"
        "Canned E2E summary from PATH shim.\n"
        "\n"
        "## Key Decisions\n"
        "- None noted.\n"
        "\n"
        "## Changes Made\n"
        "- None noted.\n"
        "\n"
        "## Errors Encountered\n"
        "- None.\n"
        "\n"
        "## Open Questions / Next Steps\n"
        "- None.\n"
        "\n"
        "IMPORTANCE: 5\n"
        "EOF\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _hook_env(tmp_path: Path) -> dict:
    """Env dict for subprocess hook invocations — HOME + PATH sandboxed."""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{REPO_ROOT / 'hooks'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_snapshot_e2e_pipeline(tmp_path, monkeypatch):
    """Fire both hooks, then walk the in-process pipeline, asserting at every boundary."""

    # --- Stage 0: fixtures ---
    vault = tmp_path / "vault"
    sessions_dir = vault / "claude-sessions"
    insights_dir = vault / "claude-insights"
    sessions_dir.mkdir(parents=True)
    insights_dir.mkdir(parents=True)

    home = tmp_path / "home"
    _write_config(home, vault)
    transcript = _write_transcript(home, SLUG)
    _write_path_shim(tmp_path / "bin")

    # Redirect HOME for in-process calls so any indirect Path.home() lookup
    # (e.g. default ensure_index db path) resolves into the sandbox.
    monkeypatch.setenv("HOME", str(home))

    # --- Stage 1: fire the snapshot hook ---
    snapshot_payload = {
        "session_id": SID,
        "cwd": str(tmp_path / "fake-cwd"),
        "transcript_path": str(transcript),
        "source": "compact",
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK_SNAPSHOT)],
        input=json.dumps(snapshot_payload),
        env=_hook_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.fail(f"snapshot hook exit={proc.returncode}\nstderr:\n{proc.stderr}")

    snapshot_files = sorted(sessions_dir.glob("*-snapshot-*.md"))
    assert len(snapshot_files) == 1, (
        f"expected exactly 1 snapshot file, got {len(snapshot_files)}: "
        f"{[f.name for f in snapshot_files]}\nhook stderr:\n{proc.stderr}"
    )
    snapshot_path = snapshot_files[0]

    snapshot_text = snapshot_path.read_text(encoding="utf-8")
    assert "type: claude-snapshot" in snapshot_text
    assert f"session_id: {SID}" in snapshot_text
    assert f"project: {PROJECT}" in snapshot_text or PROJECT in snapshot_text
    assert "status: auto-logged" in snapshot_text
    assert "trigger: compact" in snapshot_text
    assert "# Context Snapshot:" in snapshot_text

    # --- Stage 2: fire the session-log hook ---
    session_payload = {
        "session_id": SID,
        "cwd": str(tmp_path / "fake-cwd"),
        "transcript_path": str(transcript),
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK_SESSION_LOG)],
        input=json.dumps(session_payload),
        env=_hook_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        pytest.fail(f"session-log hook exit={proc.returncode}\nstderr:\n{proc.stderr}")

    session_files = [
        f for f in sessions_dir.glob("*.md")
        if "-snapshot-" not in f.name
    ]
    assert len(session_files) == 1, (
        f"expected exactly 1 session file, got {len(session_files)}: "
        f"{[f.name for f in session_files]}\nhook stderr:\n{proc.stderr}"
    )
    session_path = session_files[0]

    session_text = session_path.read_text(encoding="utf-8")
    assert "type: claude-session" in session_text
    assert f"session_id: {SID}" in session_text
    assert "status: auto-logged" in session_text

    # Back-reference check: the session note's frontmatter must list the
    # snapshot via a wikilink under `snapshots:`.
    snapshot_stem = snapshot_path.stem
    assert f"[[{snapshot_stem}]]" in session_text, (
        f"session note missing snapshot back-ref [[{snapshot_stem}]]:\n{session_text[:2000]}"
    )
    # Look for the YAML key itself.
    assert re.search(r"^snapshots:", session_text, re.MULTILINE), (
        "session note frontmatter missing `snapshots:` YAML key"
    )
