"""Import revisions track source growth without replacing manual note edits."""
import hashlib
import io
import json
from pathlib import Path

import pytest

import skill_procedures as procedures
from parity_test_helpers import host, selected_host_context, context


def invoke(context, operation, payload):
    output, errors = io.StringIO(), io.StringIO()
    code = procedures.run_operation(context, 'vault-import', operation, payload, output, errors)
    return code, output.getvalue(), errors.getvalue()


def successful(context, operation, payload):
    code, output, errors = invoke(context, operation, payload)
    assert code == 0, errors
    return json.loads(output) if output.lstrip().startswith('{') else output


def source_rows(context, identity, facts):
    if context.host == 'claude':
        return [{'type': 'user', 'sessionId': identity, 'uuid': 'message-' + str(number),
                 'message': {'content': fact}} for number, fact in enumerate(facts)]
    return [{'type': 'session_meta', 'payload': {'id': identity}}] + [
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': fact,
         'local_images': [], 'local_audio': [], 'text_elements': []}} for fact in facts]


def prepare_source(context, facts=('First fact',), identity='grown-origin'):
    root = context.native_home / ('projects' if context.host == 'claude' else 'sessions')
    root.mkdir(parents=True, exist_ok=True)
    source = root / (identity + '.jsonl')
    source.write_text(''.join(json.dumps(row) + '\n' for row in source_rows(context, identity, facts)))
    return source


def read_source(context, source, identity='grown-origin'):
    operation = successful(context, 'prepare', {})['operation_id']
    payload = {'operation_id': operation, 'source_host': context.host,
               'source_session_id': identity, 'source_path': str(source)}
    return payload, successful(context, 'import-read', payload)


def publish(context, payload, text, filename='initial.md'):
    return invoke(context, 'note-create', dict(payload, filename=filename,
        content='---\ntype: claude-session\nstatus: summarized\n---\n\n## Summary\n' + text + '\n'))


def first_import(context):
    source = prepare_source(context)
    payload, ready = read_source(context, source)
    assert ready['status'] == 'ready'
    code, output, errors = publish(context, payload, 'First summary')
    assert code == 0, errors
    note = context.vault_path / context.config['sessions_folder'] / 'initial.md'
    return source, note


def test_grown_transcript_updates_same_note_and_preserves_manual_text(context):
    from obsidian_utils import parse_frontmatter_field
    source, note = first_import(context)
    original_source = source.read_bytes()
    original_note = note.read_bytes()
    source.write_text(''.join(json.dumps(row) + '\n' for row in source_rows(
        context, 'grown-origin', ['First fact', 'Later fact'])))
    grown_source = source.read_bytes()
    payload, ready = read_source(context, source)
    assert ready['status'] == 'ready' and ready['existing_path'] == str(note)
    assert ready['source_revision'] == hashlib.sha256(grown_source).hexdigest()
    assert [record['text'] for record in ready['records']] == ['First fact', 'Later fact']
    assert source.read_bytes() == grown_source and note.read_bytes() == original_note
    note.write_bytes(original_note + b'\nManual edit after source normalization\n')
    code, output, errors = publish(context, payload, 'Updated summary includes Later fact', 'alternate.md')
    assert code == 0, errors
    assert json.loads(output)['status'] == 'applied'
    assert 'Manual edit after source normalization' in note.read_text()
    assert 'Updated summary includes Later fact' in note.read_text()
    assert 'First summary' not in note.read_text()
    assert parse_frontmatter_field(note.read_text(), 'source_revision') == hashlib.sha256(grown_source).hexdigest()
    assert not note.with_name('alternate.md').exists()
    assert source.read_bytes() == grown_source and grown_source.startswith(original_source)


def test_unchanged_transcript_is_idempotent_after_import(context):
    source, note = first_import(context)
    before = note.read_bytes()
    _, skipped = read_source(context, source)
    assert skipped['status'] == 'skipped' and skipped['existing_path'] == str(note)
    assert note.read_bytes() == before
    assert len(list(note.parent.glob('*.md'))) == 1


@pytest.mark.parametrize('failure', ['source-growth', 'owned-summary-edit'])
def test_failed_grown_update_preserves_published_note_and_source_revision(context, failure):
    from obsidian_utils import parse_frontmatter_field
    source, note = first_import(context)
    old_revision = parse_frontmatter_field(note.read_text(), 'source_revision')
    prepare_source(context, ['First fact', 'Later fact'])
    payload, ready = read_source(context, source)
    assert ready['status'] == 'ready'
    if failure == 'source-growth':
        prepare_source(context, ['First fact', 'Later fact', 'Newer fact'])
    else:
        note.write_text(note.read_text().replace('First summary', 'Manual owned summary edit'))
    before = note.read_bytes()
    source_before = source.read_bytes()
    code, output, errors = publish(context, payload, 'Stale generated summary')
    assert code != 0
    assert note.read_bytes() == before and source.read_bytes() == source_before
    assert parse_frontmatter_field(note.read_text(), 'source_revision') == old_revision
    assert 'Stale generated summary' not in note.read_text()


def test_existing_other_host_and_full_id_never_suppress_import(context):
    from vault_index import ensure_index, index_note
    from obsidian_utils import indexed_folders
    folder = context.vault_path / context.config['sessions_folder']
    folder.mkdir(parents=True)
    source = prepare_source(context)
    revision = hashlib.sha256(source.read_bytes()).hexdigest()
    other_host = 'codex' if context.host == 'claude' else 'claude'
    sentinels = []
    for name, provider, identity in [('other-host', other_host, 'grown-origin'),
                                     ('other-id', context.host, 'grown-origin-other')]:
        note = folder / (name + '.md')
        note.write_text('---\ntype: claude-session\nagent_provider: ' + provider +
            '\nagent_session_id: ' + identity + '\nsource_revision: ' + revision + '\n---\n\nForeign note\n')
        sentinels.append((note, note.read_bytes()))
    ensure_index(str(context.vault_path), indexed_folders(dict(context.config)), db_path=str(context.index_path))
    for note, _ in sentinels:
        assert index_note(str(context.index_path), str(note))
    _, ready = read_source(context, source)
    assert ready['status'] == 'ready' and 'existing_path' not in ready
    assert all(note.read_bytes() == before for note, before in sentinels)


def test_legacy_import_without_saved_revision_updates_without_erasing_manual_sections(context):
    from vault_index import ensure_index, index_note
    from obsidian_utils import indexed_folders, parse_frontmatter_field
    source = prepare_source(context, ['Old fact', 'Added after legacy import'])
    folder = context.vault_path / context.config['sessions_folder']
    folder.mkdir(parents=True)
    note = folder / 'legacy.md'
    note.write_text('---\ntype: claude-session\nagent_provider: ' + context.host +
        '\nagent_session_id: grown-origin\n---\n\n## Summary\nLegacy manually curated summary\n\n## Personal notes\nKeep this text\n')
    before = note.read_bytes()
    ensure_index(str(context.vault_path), indexed_folders(dict(context.config)), db_path=str(context.index_path))
    assert index_note(str(context.index_path), str(note))
    payload, ready = read_source(context, source)
    assert ready['status'] == 'ready' and ready['existing_path'] == str(note)
    assert note.read_bytes() == before
    code, output, errors = publish(context, payload, 'New owned summary includes Added after legacy import')
    assert code == 0, errors
    assert 'Legacy manually curated summary' in note.read_text() and 'Keep this text' in note.read_text()
    assert '<!-- obsidian-brain:summary:start -->' in note.read_text()
    assert parse_frontmatter_field(note.read_text(), 'source_revision') == hashlib.sha256(source.read_bytes()).hexdigest()
    _, unchanged = read_source(context, source)
    assert unchanged['status'] == 'skipped'


def test_source_growth_during_unchanged_lookup_is_pending(context, monkeypatch):
    source, note = first_import(context)
    before = note.read_bytes()
    original_read = procedures._import_note_read
    def grow_during_note_read(selected, path):
        result = original_read(selected, path)
        prepare_source(context, ['First fact', 'Concurrent growth'])
        return result
    monkeypatch.setattr(procedures, '_import_note_read', grow_during_note_read)
    operation = successful(context, 'prepare', {})['operation_id']
    code, output, errors = invoke(context, 'import-read', {'operation_id': operation,
        'source_host': context.host, 'source_session_id': 'grown-origin', 'source_path': str(source)})
    assert code != 0 and not output and 'changed during revision lookup' in errors
    assert note.read_bytes() == before


@pytest.mark.parametrize('failure', ['symlink', 'directory', 'oversize', 'replacement'])
def test_import_destination_read_refuses_unsafe_bytes_without_mutation(context, monkeypatch, failure):
    folder = context.vault_path / context.config['sessions_folder']
    folder.mkdir(parents=True, exist_ok=True)
    note = folder / 'guarded.md'
    note.write_text('Published summary')
    if failure == 'symlink':
        target = folder / 'target.md'
        note.rename(target)
        note.symlink_to(target)
    elif failure == 'directory':
        note.unlink()
        note.mkdir()
    elif failure == 'oversize':
        note.write_bytes(b'x' * 1000001)
    else:
        original = procedures.os.fdopen
        class ReplacedStream:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def read(self, size):
                data = self.stream.read(size)
                replacement = folder / 'replacement.md'
                replacement.write_text('Concurrent published revision')
                replacement.replace(note)
                return data
        monkeypatch.setattr(procedures.os, 'fdopen', lambda *args, **kwargs:
                            ReplacedStream(original(*args, **kwargs)))
    before = note.read_bytes() if note.is_file() else None
    with pytest.raises((ValueError, OSError)):
        procedures._import_note_read(context, note)
    if failure == 'replacement':
        assert note.read_text() == 'Concurrent published revision'
    elif before is not None:
        assert note.read_bytes() == before
    else:
        assert note.is_dir()


def test_bounded_import_reads_all_batches_and_reimports_idempotently(context, monkeypatch):
    import transcripts
    facts = ['Batch fact ' + str(number) + ' ' + ('x' * 80) for number in range(9)]
    source = prepare_source(context, facts)
    monkeypatch.setattr(transcripts, 'MAX_BATCH_BYTES', max(map(len, source.read_bytes().splitlines(keepends=True))) * 2)
    original = transcripts.read_records
    batches = []
    def observe(*args):
        batch = original(*args)
        batches.append(batch)
        return batch
    monkeypatch.setattr(transcripts, 'read_records', observe)
    before = source.read_bytes()
    payload, ready = read_source(context, source)
    assert len(batches) >= 2 and any(batch.status == 'partial' for batch in batches)
    assert [record['text'] for record in ready['records']] == facts
    assert ready['source_revision'] == hashlib.sha256(before).hexdigest()
    code, _, errors = publish(context, payload, 'All nine batch facts summarized')
    assert code == 0, errors
    note = context.vault_path / context.config['sessions_folder'] / 'initial.md'
    note_before = note.read_bytes()
    _, skipped = read_source(context, source)
    assert skipped['status'] == 'skipped'
    assert note.read_bytes() == note_before and source.read_bytes() == before


@pytest.mark.parametrize('failure', ['deadline', 'unfinished', 'unknown', 'mixed', 'suffixed', 'loss', 'no-progress'])
def test_import_does_not_continue_unsafe_partial_batches(context, monkeypatch, failure):
    from dataclasses import replace
    import transcripts
    source = prepare_source(context, ['First fact', 'Final fact'])
    original = transcripts.read_records
    calls = []
    def unsafe(origin, cursor, deadline):
        calls.append(cursor)
        batch = original(origin, cursor, deadline)
        warnings = {'deadline': ('Transcript deadline reached',),
                    'unfinished': ('Incomplete final transcript line retained',),
                    'unknown': ('Unknown transcript record',),
                    'mixed': ('Transcript batch limit reached', 'Transcript deadline reached'),
                    'suffixed': ('Transcript batch limit reached extra',)}.get(
                        failure, ('Transcript batch limit reached',))
        return replace(batch, status='partial', warnings=warnings, source_complete=False,
                       loss_of_input=failure == 'loss',
                       consumed_offset=cursor.offset if failure == 'no-progress' else batch.consumed_offset)
    monkeypatch.setattr(transcripts, 'read_records', unsafe)
    operation = successful(context, 'prepare', {})['operation_id']
    before = source.read_bytes()
    code, _, errors = invoke(context, 'import-read', {'operation_id': operation,
        'source_host': context.host, 'source_session_id': 'grown-origin', 'source_path': str(source)})
    assert code != 0 and 'pending' in errors
    assert len(calls) == 1 and not list(context.vault_path.rglob('*.md'))
    assert source.read_bytes() == before
