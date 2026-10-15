"""Run a real lifecycle hook after resolving its explicit disposable actor."""
import argparse
import hashlib
import time
import importlib
import io
import json
import os
import stat
from pathlib import Path
import sys


FIELDS = ('host', 'client', 'native_session_id', 'worktree', 'vault_path', 'config_path',
          'resource_root', 'index_path', 'state_path')


def _owned_fixture_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        assert stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_nlink == 1
        assert before.st_size <= 1024 * 1024, 'Only bounded synthetic fixtures qualify'
        data = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    assert signature(before) == signature(after) and len(data) == before.st_size
    assert signature(after) == signature(Path(path).lstat())
    return data, signature(after)


def _fixture_root_identities(context):
    roots = {}
    for name in ('canonical_project_root', 'worktree', 'resource_root', 'native_home', 'vault_path'):
        path = Path(getattr(context, name))
        assert path.resolve() == path and not path.is_symlink()
        details = path.lstat()
        assert stat.S_ISDIR(details.st_mode) and details.st_uid == os.getuid()
        roots[name] = (str(path), details.st_dev, details.st_ino)
    return roots


def fixture_replay_baseline(context):
    import transcripts
    source = _owned_fixture_bytes(context.transcript_path)
    config = _owned_fixture_bytes(context.config_path)
    batch = transcripts.read_records(context, transcripts.SourceCursor(), time.monotonic() + 2.5)
    assert batch.status == 'ok' and batch.source_complete and not batch.loss_of_input
    assert batch.metadata.get('native_session_id') == context.native_session_id
    assert _owned_fixture_bytes(context.transcript_path) == source
    return {'source': source, 'config': config, 'roots': _fixture_root_identities(context),
            'context': {field: str(getattr(context, field)) for field in FIELDS}}


def qualify_fixture_replay(context, pending, baseline, *, publication_deadlines=False, applied_revision=False):
    assert pending['status'] == 'pending' and pending['pending_sources'] > 0
    assert not pending['loss_of_input']
    assert not pending['applied_revision'] or applied_revision
    warnings = set(pending['warnings'])
    deadlines = {'Capture deadline reached', 'Transcript deadline reached',
                 'Transcript deadline reached before reading', 'Facts retained; publication deadline reached'}
    if publication_deadlines:
        deadlines |= {'End snapshot deadline reached', 'Snapshot deadline reached'}
    assert warnings & deadlines and warnings <= deadlines | {'Native transcript identity is unverified'}
    selected = {field: str(getattr(context, field)) for field in FIELDS}
    assert pending['context'] == baseline['context'] == selected
    assert _fixture_root_identities(context) == baseline['roots']
    assert _owned_fixture_bytes(context.transcript_path) == baseline['source']
    assert _owned_fixture_bytes(context.config_path) == baseline['config']
    assert pending['source_revision'] == hashlib.sha256(baseline['source'][0]).hexdigest()


def qualified_fixture_checkpoint(context, event, *, first_deadline=None, before_replay=None):
    """Functional fixture recovery only; never native timing/dispatch evidence."""
    import capture
    import note_transactions
    from runtime_context import using_runtime_context
    baseline = fixture_replay_baseline(context)
    observations = []
    with using_runtime_context(context):
        first = capture.capture_checkpoint(context, event,
            time.monotonic() + 2 if first_deadline is None else first_deadline)
        assert isinstance(first, capture.CaptureResult)
        observations.append(first)
        if first.status == 'complete':
            assert first.applied_revision, observations
            return first, tuple(observations)
        if before_replay is not None:
            before_replay()
        # Capture may have published the parent but deferred its End checkpoint.
        # Qualify that partial revision only against the actual selected parent
        # and its frozen End intent; conflicts and unrelated writes cannot pass.
        if first.applied_revision:
            with note_transactions.connect_coordination(context) as connection:
                row = connection.execute('SELECT note,cursor FROM source_sessions WHERE scope=?',
                                         (context.session_key,)).fetchone()
            assert event.kind == 'session_end' and row, first
            parent = Path(row[0])
            intent = json.loads(row[1])['parser_state'].get('_capture_end_snapshot')
            assert isinstance(intent, dict) and intent.get('parent') == str(parent), first
            assert intent.get('metadata', {}).get('agent_provider') == context.host, first
            assert intent.get('metadata', {}).get('agent_session_id') == context.native_session_id, first
            assert note_transactions.read_revision(context, parent) == first.applied_revision, first
            qualified_revision = first.applied_revision
        else:
            qualified_revision = None
        pending = {'status': first.status, 'pending_sources': first.pending_sources,
            'loss_of_input': first.loss_of_input, 'applied_revision': first.applied_revision,
            'warnings': list(first.warnings), 'context': dict(baseline['context']),
            'source_revision': hashlib.sha256(_owned_fixture_bytes(context.transcript_path)[0]).hexdigest()}
        try:
            qualify_fixture_replay(context, pending, baseline, publication_deadlines=True,
                                   applied_revision=qualified_revision is not None)
        except AssertionError as error:
            raise AssertionError(('Unqualified fixture pending result', first)) from error
        if qualified_revision:
            assert note_transactions.read_revision(context, parent) == qualified_revision, first
        # Exactly one ordinary production checkpoint with the same event/options.
        second = capture.capture_checkpoint(context, event, time.monotonic() + 2)
        assert isinstance(second, capture.CaptureResult)
        observations.append(second)
        assert second.status == 'complete' and second.applied_revision, observations
        assert _owned_fixture_bytes(context.transcript_path) == baseline['source']
        assert _owned_fixture_bytes(context.config_path) == baseline['config']
        return second, tuple(observations)


def main():
    parser = argparse.ArgumentParser()
    for field in ('host', 'client', 'session-id', 'cwd', 'vault', 'config', 'resource-root', 'index', 'state'):
        parser.add_argument('--' + field, required=True)
    parser.add_argument('--transcript')
    parser.add_argument('--recover-deferred', action='store_true')
    parser.add_argument('--expire-first-capture', action='store_true')
    parser.add_argument('--expire-first-transcript', action='store_true')
    parser.add_argument('script')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.resource_root) / 'hooks'))
    from runtime_context import resolve_runtime_context, using_runtime_context, current_runtime_context
    import note_transactions
    context = resolve_runtime_context(args.host, args.client, {}, {
        'session_id': args.session_id, 'cwd': args.cwd, 'vault_path': args.vault,
        'config_path': args.config, 'resource_root': args.resource_root,
        'index_path': args.index, 'state_path': args.state,
        **({'transcript_path': args.transcript} if args.transcript else {})})
    fields = FIELDS
    proof = {field: str(getattr(context, field)) for field in fields}
    proof['mutation_contexts'] = []
    proof['capture_results'] = []
    proof['recovery_attempted'] = False
    baseline = fixture_replay_baseline(context) if args.recover_deferred else None
    source_before = (baseline['source'][0] if baseline else context.transcript_path.read_bytes()
                     if args.expire_first_capture or args.expire_first_transcript else None)
    source_revision = hashlib.sha256(source_before).hexdigest() if source_before is not None else None
    proof['source_revision'] = source_revision
    proof['source_path'] = str(context.transcript_path) if source_before is not None else None
    original = note_transactions.apply_mutations
    def observed(actor, *positional, **keywords):
        assert current_runtime_context() is context
        for field in fields:
            assert getattr(actor, field) == getattr(context, field), field
        proof['mutation_contexts'].append({field: str(getattr(actor, field)) for field in fields})
        return original(actor, *positional, **keywords)
    note_transactions.apply_mutations = observed
    if source_before is not None:
        import capture
        original_capture = capture.capture_checkpoint
        if args.expire_first_transcript:
            import transcripts
            original_read = transcripts.read_records
            first_read = [True]
            def expired_read(actor, cursor, deadline):
                selected = time.monotonic() - 1 if first_read[0] else deadline
                first_read[0] = False
                return original_read(actor, cursor, selected)
            transcripts.read_records = expired_read
        def observed_capture(actor, event, deadline):
            assert actor is context and current_runtime_context() is context
            selected_deadline = (time.monotonic() - 1 if args.expire_first_capture
                                 and not proof['capture_results'] else deadline)
            result = original_capture(actor, event, selected_deadline)
            assert isinstance(result, capture.CaptureResult)
            proof['capture_results'].append({
                'event': event.kind, 'status': result.status,
                'pending_sources': result.pending_sources, 'loss_of_input': result.loss_of_input,
                'warnings': list(result.warnings), 'applied_revision': result.applied_revision,
                'context': {field: str(getattr(actor, field)) for field in fields},
                'source_revision': hashlib.sha256(actor.transcript_path.read_bytes()).hexdigest()})
            return result
        capture.capture_checkpoint = observed_capture
    try:
        with using_runtime_context(context):
            module = importlib.import_module(Path(args.script).stem)
            try:
                module.main()
            except SystemExit as error:
                if error.code not in (None, 0):
                    raise
            proof['initial_mutation_count'] = len(proof['mutation_contexts'])
            if not proof['mutation_contexts'] and args.recover_deferred:
                assert len(proof['capture_results']) == 1
                pending = proof['capture_results'][0]
                events = {'obsidian_session_hint.py': 'session_start',
                          'obsidian_context_snapshot.py': 'pre_compact',
                          'obsidian_session_log.py': 'session_end', 'obsidian_retro_gate.py': 'stop'}
                expected = events[Path(args.script).name]
                assert pending['event'] == expected
                qualify_fixture_replay(context, pending, baseline)
                proof['recovery_attempted'] = True
                from native_lifecycle import emit_bound
                replay = 'stop' if expected == 'session_start' else expected
                proof['replay_scope'] = 'unit-fixture-event-replay-not-native-L3'
                emit_bound(context, replay, {'trigger': 'manual'} if replay == 'pre_compact' else {})
                completed = proof['capture_results'][-1]
                assert completed['event'] == replay and completed['status'] == 'complete'
                assert completed['applied_revision'] and proof['mutation_contexts']
                assert context.transcript_path.read_bytes() == source_before
    finally:
        print('NATIVE_CONTEXT_PROOF:' + json.dumps(proof), file=sys.stderr)


if __name__ == '__main__':
    main()
