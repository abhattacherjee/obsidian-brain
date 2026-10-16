"""The legacy scan plan must still publish through the selected actor's CAS."""
import json
from pathlib import Path

import obsidian_session_reaper as reaper
import obsidian_utils
import note_transactions
from runtime_context import using_runtime_context


def test_legacy_reaper_preserves_note_created_after_revision_read(selected_host_context, tmp_path, monkeypatch):
    context = selected_host_context
    source_dir = tmp_path / "explicit-claude-source"
    source_dir.mkdir()
    source = source_dir / "legacy-origin-session.jsonl"
    source.write_text("\n".join(json.dumps({
        "type": "user", "sessionId": "legacy-origin-session",
        "timestamp": "2026-04-24T12:0%d:00Z" % index,
        "message": {"role": "user", "content": "Visible legacy fact %d" % index},
    }) for index in range(5)) + "\n")
    monkeypatch.setattr(reaper, "_resolve_project_jsonl_dir", lambda project: source_dir)
    monkeypatch.setattr(obsidian_utils, "canonical_project_name", lambda: "reaper-project")
    monkeypatch.setattr(obsidian_utils, "_first_seen_date", lambda sid: "2026-04-24")
    monkeypatch.setattr(note_transactions, "context_for_vault", lambda vault: context)
    import runtime_adapters.claude
    watermark = tmp_path / "legacy-watermark"
    monkeypatch.setattr(runtime_adapters.claude, "legacy_reaper_watermark", lambda project: watermark)
    original_write = obsidian_utils.write_vault_note
    publication = []
    manual = "Human note created while the reaper rendered its source.\n"
    def publish(vault, folder, filename, content, **kwargs):
        assert kwargs["expected_revision"] is None
        target = Path(vault) / folder / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(manual)
        with using_runtime_context(context):
            result = original_write(vault, folder, filename, content, **kwargs)
        publication.append((target, result))
        return result
    monkeypatch.setattr(obsidian_utils, "write_vault_note", publish)
    with using_runtime_context(None):
        result = reaper._reap_orphaned_sessions("reaper-project", str(context.vault_path),
            "claude-sessions", {"min_messages": 1, "min_duration_minutes": 0,
                                "reaper_max_runtime_seconds": 5})
    assert result.reaped == 0
    assert len(publication) == 1
    target, error = publication[0]
    assert error is not None and "conflict" in error.lower()
    assert target.read_text() == manual
    assert not watermark.exists()
