"""Tests for upgrade_batch() return shape + telemetry sink wiring (issue #74)."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
import obsidian_utils  # noqa: E402
import summarizer_metrics  # noqa: E402


@pytest.fixture
def fake_note(tmp_path):
    """Return a factory producing a minimal valid session note in tmp_path/claude-sessions/."""
    sess_dir = tmp_path / "claude-sessions"
    sess_dir.mkdir()

    def _make(name: str = "n.md", session_id: str = "abc"):
        p = sess_dir / name
        p.write_text(
            f"---\nsession_id: {session_id}\nproject: obsidian-brain\n---\n"
            "## Conversation (raw)\n**User:** hi\n**Assistant:** hello\n"
        )
        return p
    return _make


def test_upgrade_batch_returns_dicts_with_all_five_fields(monkeypatch, tmp_path, fake_note):
    """Happy-path: returns list[dict] with path, status, elapsed_s, model_used, fallback_reason."""
    monkeypatch.setattr(
        summarizer_metrics, "METRICS_PATH",
        tmp_path / "metrics.jsonl",
    )

    def fake_uun(path, *a, **kw):
        return (f"Upgraded {os.path.basename(path)}", 0.5, "haiku-4.5", None)

    monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_uun)

    p1 = fake_note("a.md", "s1")
    p2 = fake_note("b.md", "s2")

    result = obsidian_utils.upgrade_batch(
        [str(p1), str(p2)], str(tmp_path), "claude-sessions", "obsidian-brain",
    )

    assert isinstance(result, list)
    assert len(result) == 2
    for r in result:
        assert set(r.keys()) == {"path", "status", "elapsed_s", "model_used", "fallback_reason"}
        assert r["status"].startswith("Upgraded ")
        assert r["elapsed_s"] >= 0
        assert r["model_used"] == "haiku-4.5"
        assert r["fallback_reason"] is None
    assert result[0]["path"] == str(p1)
    assert result[1]["path"] == str(p2)


def test_upgrade_batch_writes_telemetry_record(monkeypatch, tmp_path, fake_note):
    """Telemetry sink gets exactly one record per upgrade_batch call, with embedded per-note array."""
    metrics_path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", metrics_path)

    def fake_uun(path, *a, **kw):
        return (f"Upgraded {os.path.basename(path)}", 0.5, "haiku-4.5", None)

    monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_uun)

    p1 = fake_note("a.md", "s1")
    obsidian_utils.upgrade_batch(
        [str(p1)], str(tmp_path), "claude-sessions", "obsidian-brain",
    )

    assert metrics_path.exists()
    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["project"] == "obsidian-brain"
    assert rec["n_notes"] == 1
    assert "ts" in rec
    assert rec["wall_s"] >= 0
    assert len(rec["notes"]) == 1
    assert rec["notes"][0]["model_used"] == "haiku-4.5"


def test_upgrade_batch_per_note_failure_captured_with_reason(monkeypatch, tmp_path, fake_note):
    """A note that failed with fallback_reason carries it through into the dict + telemetry."""
    metrics_path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(summarizer_metrics, "METRICS_PATH", metrics_path)

    def fake_uun(path, *a, **kw):
        return (
            f"Failed: AI summarization returned empty for {os.path.basename(path)}",
            1.2, None, "haiku_timeout",
        )

    monkeypatch.setattr(obsidian_utils, "upgrade_unsummarized_note", fake_uun)

    p1 = fake_note("a.md", "s1")
    result = obsidian_utils.upgrade_batch(
        [str(p1)], str(tmp_path), "claude-sessions", "obsidian-brain",
    )

    assert len(result) == 1
    assert result[0]["status"].startswith("Failed:")
    assert result[0]["model_used"] is None
    assert result[0]["fallback_reason"] == "haiku_timeout"

    rec = json.loads(metrics_path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["notes"][0]["fallback_reason"] == "haiku_timeout"
