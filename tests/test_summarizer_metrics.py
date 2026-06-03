"""Tests for hooks/summarizer_metrics.py — JSONL telemetry sink for upgrade_batch()."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import summarizer_metrics  # noqa: E402


def test_append_creates_file_with_0600_perms(tmp_path, monkeypatch):
    """First call creates the .jsonl file with owner-only perms and a valid line."""
    metrics_path = tmp_path / "summarizer-metrics.jsonl"
    monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", metrics_path)

    record = {
        "ts": "2026-05-16T08:42:13Z",
        "project": "obsidian-brain",
        "session_id": "9367",
        "n_notes": 1,
        "wall_s": 1.8,
        "notes": [
            {"path": "/v/a.md", "status": "Upgraded a.md",
             "elapsed_s": 1.8, "model_used": "haiku-4.5", "fallback_reason": None}
        ],
    }
    summarizer_metrics.append_metrics_record(record)

    assert metrics_path.exists()
    assert oct(metrics_path.stat().st_mode)[-3:] == "600"
    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["n_notes"] == 1
    assert parsed["notes"][0]["model_used"] == "haiku-4.5"


def test_rotates_when_file_exceeds_threshold(tmp_path, monkeypatch):
    """When file size > ROTATE_BYTES, next append rotates to .jsonl.1 and starts fresh."""
    metrics_path = tmp_path / "summarizer-metrics.jsonl"
    monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", metrics_path)
    monkeypatch.setattr(summarizer_metrics, "ROTATE_BYTES", 500)  # tiny threshold

    # Fill above threshold with a few records (~115 bytes each x 5 = ~575 bytes)
    for i in range(5):
        summarizer_metrics.append_metrics_record({"i": i, "pad": "x" * 100})

    assert metrics_path.stat().st_size > 200, "precondition: file is above threshold"

    # Next append should rotate
    summarizer_metrics.append_metrics_record({"after_rotate": True})

    rotated = metrics_path.with_suffix(".jsonl.1")
    assert rotated.exists(), "previous file should be moved to .jsonl.1"
    # Fresh file contains only the post-rotation line
    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"after_rotate": True}
    assert oct(metrics_path.stat().st_mode)[-3:] == "600"


def test_write_failure_does_not_raise(tmp_path, monkeypatch, capsys):
    """If the sink can't write, it must log to stderr and return None — never raise."""
    # Point METRICS_PATH inside a non-existent parent that cannot be created
    # (parent of parent is a file, not a dir, so mkdir will fail).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    metrics_path = blocker / "subdir" / "metrics.jsonl"
    monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", metrics_path)

    # Must not raise
    result = summarizer_metrics.append_metrics_record({"k": "v"})
    assert result is None

    err = capsys.readouterr().err
    assert "summarizer_metrics append failed" in err


def test_does_not_rotate_at_exact_threshold(tmp_path, monkeypatch):
    """Strict > predicate: file at exactly ROTATE_BYTES should NOT rotate on next append."""
    metrics_path = tmp_path / "summarizer-metrics.jsonl"
    monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", metrics_path)
    monkeypatch.setattr(summarizer_metrics, "ROTATE_BYTES", 100)

    # Pre-seed file to exactly ROTATE_BYTES bytes using a valid JSONL line
    # (padded to exactly 100 bytes including the trailing newline).
    pad = "x" * (98 - len('{"seed":true}'))
    seed_line = f'{{"seed":true,"pad":"{pad}"}}\n'
    # Adjust to hit exactly 100 bytes
    seed_line = ("x" * 99 + "\n")  # 100 bytes, valid text, not valid JSON but doesn't matter
    assert len(seed_line.encode("utf-8")) == 100
    metrics_path.write_text(seed_line, encoding="utf-8")

    summarizer_metrics.append_metrics_record({"new": "line"})

    rotated = metrics_path.with_suffix(".jsonl.1")
    assert not rotated.exists(), "should not rotate when size == ROTATE_BYTES (strict >)"
    # The new record must be in the main file (two lines total)
    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[-1]) == {"new": "line"}


def test_non_json_serializable_record_does_not_raise(tmp_path, monkeypatch, capsys):
    """A non-JSON-serializable record must never raise — log to stderr and return None."""
    metrics_path = tmp_path / "summarizer-metrics.jsonl"
    monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", metrics_path)

    # An `object()` is not JSON-serializable; json.dumps raises TypeError
    result = summarizer_metrics.append_metrics_record({"k": object()})
    assert result is None
    err = capsys.readouterr().err
    assert "summarizer_metrics append failed" in err
    assert "TypeError" in err
