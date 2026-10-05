"""Shared vault publication must preserve revisions and process ownership."""
import hashlib
import json
import os
import select
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from runtime_context import RuntimeContext
from note_transactions import NoteMutation, apply_mutations, read_revision


@pytest.fixture
def context(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return RuntimeContext("codex", "codex-cli", "native-id", tmp_path, tmp_path,
                          None, vault, tmp_path / "config.json", MappingProxyType({}),
                          tmp_path, tmp_path / "index.sqlite3", tmp_path / "state")


def test_new_note_operation_is_idempotent(context):
    note = context.vault_path / "sessions" / "n.md"
    mutation = NoteMutation(note, None, {"document": "hello\n"}, "create-1")
    first = apply_mutations(context, [mutation])
    second = apply_mutations(context, [mutation])
    assert first.status == "applied"
    assert second.status == "unchanged"
    assert note.read_text() == "hello\n"
    assert first.revision == second.revision


def test_stale_document_revision_preserves_manual_edit(context):
    note = context.vault_path / "n.md"
    note.write_text("original\n")
    revision = read_revision(context, note)
    note.write_text("manual edit\n")
    result = apply_mutations(context, [NoteMutation(note, revision, {"document": "replacement\n"}, "stale-1")])
    assert result.status == "conflict"
    assert result.pending_path.is_file()
    assert note.read_text() == "manual edit\n"


def test_legacy_capture_adds_continuation_without_replacing_body(context):
    note = context.vault_path / "n.md"
    legacy = "---\ntype: claude-session\n---\n# Original title\nUser prose.\n"
    note.write_text(legacy)
    revision = read_revision(context, note)
    result = apply_mutations(context, [NoteMutation(note, revision, {"capture": "new exchange\n"}, "capture-1")])
    assert result.status == "applied"
    assert note.read_text().startswith(legacy)
    assert "new exchange" in note.read_text()


def test_managed_capture_preserves_user_edits_outside_region(context):
    note = context.vault_path / "n.md"
    apply_mutations(context, [NoteMutation(note, None, {"capture": "first\n"}, "capture-1")])
    revision = read_revision(context, note)
    with note.open("a") as stream:
        stream.write("\nUser addition.\n")
    result = apply_mutations(context, [NoteMutation(note, revision, {"capture": "second\n"}, "capture-2")])
    assert result.status == "applied"
    assert "User addition." in note.read_text()
    assert "second" in note.read_text()
    assert "first" not in note.read_text()


def test_managed_capture_manual_edit_is_a_conflict(context):
    note = context.vault_path / "n.md"
    apply_mutations(context, [NoteMutation(note, None, {"capture": "first\n"}, "capture-1")])
    revision = read_revision(context, note)
    note.write_text(note.read_text().replace("first", "manual"))
    result = apply_mutations(context, [NoteMutation(note, revision, {"capture": "second\n"}, "capture-2")])
    assert result.status == "conflict"
    assert "manual" in note.read_text()
    assert "second" not in note.read_text()


def test_summary_cannot_publish_after_capture_advances(context):
    note = context.vault_path / "n.md"
    apply_mutations(context, [NoteMutation(note, None, {"capture": "first\n"}, "capture-1")])
    revision = read_revision(context, note)
    apply_mutations(context, [NoteMutation(note, revision, {"capture": "second\n"}, "capture-2")])
    result = apply_mutations(context, [NoteMutation(note, revision, {"summary": "summary of first\n"}, "summary-1")])
    assert result.status == "conflict"
    assert "summary of first" not in note.read_text()


def test_reusing_operation_id_with_different_content_is_rejected(context):
    note = context.vault_path / "n.md"
    apply_mutations(context, [NoteMutation(note, None, {"document": "first\n"}, "op-1")])
    result = apply_mutations(context, [NoteMutation(note, None, {"document": "second\n"}, "op-1")])
    assert result.status == "conflict"
    assert note.read_text() == "first\n"


def test_selected_vault_rejects_symlink_escape(context, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (context.vault_path / "escape").symlink_to(external, target_is_directory=True)
    result = apply_mutations(context, [NoteMutation(context.vault_path / "escape" / "n.md", None, {"document": "secret"}, "escape-1")])
    assert result.status == "conflict"
    assert not (external / "n.md").exists()


def _holder(context):
    script = '''
import sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from note_transactions import ownership_lock
ctx = SimpleNamespace(index_path=Path(sys.argv[2]))
with ownership_lock(ctx):
    print("held", flush=True)
    time.sleep(2)
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1] / "hooks"),
         str(context.index_path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    ready, _, _ = select.select([process.stdout], [], [], 5)
    if not ready:
        process.terminate()
        process.communicate(timeout=5)
        pytest.fail("The scratch lock holder did not start within five seconds")
    line = process.stdout.readline().strip()
    if line != "held":
        process.communicate(timeout=5)
        pytest.fail("The scratch lock holder failed to acquire ownership")
    return process


def test_slow_live_writer_is_not_stolen_based_on_lock_age(context):
    process = _holder(context)
    try:
        for path in context.index_path.parent.glob(".*.coordination/*.lock"):
            os.utime(path, (1, 1))
        note = context.vault_path / "n.md"
        result = apply_mutations(context, [NoteMutation(note, None, {"document": "new"}, "contended")])
        assert result.status == "pending"
        assert result.pending_path.is_file()
        assert not note.exists()
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_process_death_releases_ownership(context):
    process = _holder(context)
    process.terminate()
    process.communicate(timeout=5)
    note = context.vault_path / "n.md"
    result = apply_mutations(context, [NoteMutation(note, None, {"document": "new"}, "after-death")])
    assert result.status == "applied"
    assert note.read_text() == "new"
