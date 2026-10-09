"""Shared vault publication must preserve revisions and process ownership."""
import hashlib
import json
import os
import select
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType
from dataclasses import replace

import pytest

from runtime_context import RuntimeContext, using_runtime_context
from note_transactions import NoteMutation, apply_mutations, read_revision


@pytest.fixture
def context(selected_host_context):
    selected_host_context.index_path.parent.mkdir(parents=True, exist_ok=True)
    return selected_host_context


def test_new_note_operation_is_idempotent(context):
    note = context.vault_path / "sessions" / "n.md"
    mutation = NoteMutation(note, None, {"document": "hello\n"}, "create-1")
    first = apply_mutations(context, [mutation])
    second = apply_mutations(context, [mutation])
    assert first.status == "applied"
    assert second.status == "unchanged"
    assert note.read_text() == "hello\n"
    assert first.revision == second.revision


def test_managed_metadata_preserves_user_fields_and_detects_manual_edits(context):
    note = context.vault_path / "session.md"
    note.write_text('---\ntype: claude-session\nmy_field: keep\n---\nUser prose.\n')
    baseline = read_revision(context, note)
    first = apply_mutations(context, [NoteMutation(note, baseline, {
        "capture": "first fact", "metadata": json.dumps({"agent_provider": "codex", "capture_state": "active"})
    }, "metadata-first")])
    assert first.status == "applied"
    assert "my_field: keep" in note.read_text()
    assert "User prose." in note.read_text()
    baseline = read_revision(context, note)
    note.write_text(note.read_text().replace('capture_state: "active"', 'capture_state: "manual"'))
    result = apply_mutations(context, [NoteMutation(note, baseline, {
        "metadata": json.dumps({"capture_state": "ended"})
    }, "metadata-stale")])
    assert result.status == "conflict"
    assert 'capture_state: "manual"' in note.read_text()


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
    path_fields = ('canonical_project_root', 'worktree', 'transcript_path', 'vault_path',
                   'config_path', 'resource_root', 'index_path', 'state_path', 'native_home',
                   'user_home', 'invocation_cwd', 'coordination_root')
    descriptor = {name: str(getattr(context, name)) if getattr(context, name) is not None else None
                  for name in path_fields}
    descriptor.update(host=context.host, client=context.client,
                      native_session_id=context.native_session_id, config=dict(context.config))
    script = '''
import sys, time, json
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from note_transactions import ownership_lock
from runtime_context import RuntimeContext, using_runtime_context
values = json.loads(sys.argv[2])
for name in ('canonical_project_root','worktree','transcript_path','vault_path','config_path',
             'resource_root','index_path','state_path','native_home','user_home','invocation_cwd','coordination_root'):
    if values[name] is not None:
        values[name] = Path(values[name])
ctx = RuntimeContext(**values)
with using_runtime_context(ctx), ownership_lock(ctx):
    print("held", flush=True)
    time.sleep(2)
'''
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1] / "hooks"),
         json.dumps(descriptor)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
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


@pytest.mark.parametrize('publication', ['before_replace', 'after_replace'])
@pytest.mark.parametrize('existing_capture', [False, True])
def test_prepared_retry_preserves_later_unowned_prose_and_metadata(
        selected_host_context, monkeypatch, publication, existing_capture):
    import note_transactions as transactions
    actor = selected_host_context
    note = actor.vault_path / 'prepared-retry.md'
    original = '---\ntype: claude-session\nhuman_field: initial\ncapture_state: "active"\n---\nOriginal prose.\n'
    if existing_capture:
        original += ('<!-- obsidian-brain:capture:start -->\nold facts\n'
                     '<!-- obsidian-brain:capture:end -->\n')
    note.write_text(original)
    expected = read_revision(actor, note)
    mutation = NoteMutation(note, expected, {'capture':'new facts',
        'metadata':json.dumps({'capture_state':'ended'})}, 'prepared-retry-operation')
    failed = False
    atomic = transactions._atomic_write
    def write(path, document, file_mode=None):
        nonlocal failed
        if path == note and publication == 'before_replace' and not failed:
            failed = True
            raise OSError('synthetic failure before publication')
        if path == note and publication == 'before_replace':
            with transactions.connect_coordination(actor) as connection:
                prepared = connection.execute("SELECT document, revision FROM operations WHERE phase='prepared'").fetchone()
            assert prepared[0] == document
            assert prepared[1] == hashlib.sha256(document.encode()).hexdigest()
            assert 'Human addition after the failed attempt.' in prepared[0]
        return atomic(path, document, file_mode)
    def fault(point):
        nonlocal failed
        if publication == 'after_replace' and point == 'after_replace' and not failed:
            failed = True
            raise OSError('synthetic crash after publication')
    monkeypatch.setattr(transactions, '_atomic_write', write)
    monkeypatch.setattr(transactions, '_fault', fault)
    assert apply_mutations(actor, [mutation]).status == 'pending'
    current = note.read_text().replace('human_field: initial', 'human_field: edited\nnew_human_field: keep')
    note.write_text(current + '\nHuman addition after the failed attempt.\n')
    retried = apply_mutations(actor, [mutation])
    assert retried.status == ('applied' if publication == 'before_replace' else 'unchanged'), retried.warnings
    final = note.read_text()
    assert 'human_field: edited\nnew_human_field: keep' in final
    assert 'Human addition after the failed attempt.' in final
    assert 'Original prose.' in final
    assert 'capture_state: "ended"' in final
    assert final.count('new facts') == 1
    assert final.count('<!-- obsidian-brain:capture:start -->') == 1
    assert apply_mutations(actor, [mutation]).status == 'unchanged'
    assert note.read_text() == final


@pytest.mark.parametrize('change', ['document', 'capture', 'metadata'])
def test_prepared_retry_refuses_later_edits_to_its_owned_input(selected_host_context, monkeypatch, change):
    import note_transactions as transactions
    actor = selected_host_context
    note = actor.vault_path / 'prepared-conflict.md'
    original = ('---\ncapture_state: "active"\n---\nManual body.\n'
                '<!-- obsidian-brain:capture:start -->\nold facts\n'
                '<!-- obsidian-brain:capture:end -->\n')
    note.write_text(original)
    expected = read_revision(actor, note)
    changes = ({'document':'replacement document'} if change == 'document' else
               {'capture':'new facts'} if change == 'capture' else
               {'metadata':json.dumps({'capture_state':'ended'})})
    mutation = NoteMutation(note, expected, changes, 'prepared-conflict-operation')
    atomic = transactions._atomic_write
    def fail(path, document, file_mode=None):
        if path == note:
            raise OSError('synthetic write failure')
        return atomic(path, document, file_mode)
    monkeypatch.setattr(transactions, '_atomic_write', fail)
    assert apply_mutations(actor, [mutation]).status == 'pending'
    human = (original + 'Later human body.\n' if change == 'document' else
             original.replace('old facts', 'human capture') if change == 'capture' else
             original.replace('capture_state: "active"', 'capture_state: "manual"'))
    note.write_text(human)
    monkeypatch.setattr(transactions, '_atomic_write', atomic)
    assert apply_mutations(actor, [mutation]).status == 'conflict'
    assert note.read_text() == human


def test_physical_vault_lock_excludes_process_with_different_XDG_root(context, tmp_path):
    import note_transactions as transactions
    alternate = tmp_path / 'other-private-xdg'
    alternate.mkdir(mode=0o700)
    other = replace(context, coordination_root=alternate.resolve())
    assert transactions.coordination_location(other) != transactions.coordination_location(context)
    process = _holder(other)
    note = context.vault_path / 'different-root-contended.md'
    try:
        mutation = NoteMutation(note, None, {'document':'published after ownership'}, 'different-root')
        result = apply_mutations(context, [mutation])
        assert result.status == 'pending' and result.pending_path.is_file()
        assert not note.exists()
    finally:
        process.terminate()
        process.communicate(timeout=5)
    pending=result.pending_path
    result = transactions.recover_pending_mutations(context)
    assert result.status in {'applied','unchanged'}
    assert note.read_text() == 'published after ownership' and not pending.exists()
