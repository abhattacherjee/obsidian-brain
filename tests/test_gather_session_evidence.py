"""Tests for obsidian_utils.gather_session_evidence().

Spec: docs/superpowers/specs/2026-05-03-issue-122-retro-evidence-base-design.md
Issue: https://github.com/abhattacherjee/obsidian-brain/issues/122
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

import obsidian_utils


def _write(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def test_gather_session_evidence_unknown_sid_returns_empty(tmp_vault: Path) -> None:
    """session_id == 'unknown' must return all-empty lists with no I/O attempted."""
    bundle = obsidian_utils.gather_session_evidence(
        vault_path=str(tmp_vault),
        sessions_folder="claude-sessions",
        insights_folder="claude-insights",
        session_id="unknown",
        date="2026-05-03",
        project="obsidian-brain",
    )
    assert bundle["session_id"] == "unknown"
    assert bundle["snapshots"] == []
    assert bundle["insights"] == []
    assert bundle["decisions"] == []
    assert bundle["error_fixes"] == []
    assert bundle["discovery_errors"] == []
