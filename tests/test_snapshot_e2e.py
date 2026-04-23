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

    # Sanity: skeleton proves fixtures land in the right places.
    assert (home / ".claude" / "obsidian-brain-config.json").is_file()
    assert transcript.is_file()
    assert (tmp_path / "bin" / "claude").is_file()
    assert sessions_dir.is_dir()
