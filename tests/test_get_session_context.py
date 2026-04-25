# tests/test_get_session_context.py
"""Tests for first-seen-date marker, hash-resolver, and basename invariants
introduced for obsidian-brain#101 (subsumes #86)."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import obsidian_utils


def _unique_sid() -> str:
    return f"test-sid-{uuid.uuid4().hex}"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect `~/.claude/obsidian-brain/sessions/` into tmp_path so marker
    writes do not pollute the real user directory across tests."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield tmp_path
