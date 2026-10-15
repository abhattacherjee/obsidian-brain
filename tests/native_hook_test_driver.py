"""Run a real lifecycle hook after resolving its explicit disposable actor."""
import argparse
import hashlib
import time
import importlib
import io
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    for field in ('host', 'client', 'session-id', 'cwd', 'vault', 'config', 'resource-root', 'index', 'state'):
        parser.add_argument('--' + field, required=True)
    parser.add_argument('--transcript')
    parser.add_argument('--recover-deferred', action='store_true')
    parser.add_argument('--expire-first-capture', action='store_true')
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
    fields = ('host', 'client', 'native_session_id', 'worktree', 'vault_path', 'config_path',
              'resource_root', 'index_path', 'state_path')
    proof = {field: str(getattr(context, field)) for field in fields}
    proof['mutation_contexts'] = []
    proof['capture_results'] = []
    proof['recovery_attempted'] = False
    source_before = (context.transcript_path.read_bytes()
                     if args.recover_deferred or args.expire_first_capture else None)
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
                assert args.script == 'obsidian_session_hint.py'
                assert len(proof['capture_results']) == 1
                pending = proof['capture_results'][0]
                assert pending['event'] == 'session_start' and pending['status'] == 'pending'
                assert pending['pending_sources'] > 0 and not pending['loss_of_input']
                assert pending['warnings'] and set(pending['warnings']) <= {
                    'Capture deadline reached', 'Transcript deadline reached',
                    'Transcript deadline reached before reading',
                    'Facts retained; publication deadline reached'}
                assert pending['context'] == {field: str(getattr(context, field)) for field in fields}
                assert pending['source_revision'] == source_revision
                assert context.transcript_path.read_bytes() == source_before
                proof['recovery_attempted'] = True
                # A real next lifecycle checkpoint, not a retry of the failed call.
                from native_lifecycle import emit_bound
                emit_bound(context, 'stop', {})
                completed = proof['capture_results'][-1]
                assert completed['event'] == 'stop' and completed['status'] == 'complete'
                assert completed['applied_revision'] and proof['mutation_contexts']
                assert context.transcript_path.read_bytes() == source_before
    finally:
        print('NATIVE_CONTEXT_PROOF:' + json.dumps(proof), file=sys.stderr)


if __name__ == '__main__':
    main()
