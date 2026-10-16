"""Exercise the real writer CLI inside an explicitly resolved native actor."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))


def main():
    from runtime_context import resolve_runtime_context, using_runtime_context, current_runtime_context
    import note_transactions
    import note_writer
    parser = argparse.ArgumentParser()
    for name in ('host', 'client', 'session-id', 'cwd', 'vault', 'config', 'resource-root', 'index', 'state'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('writer_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    context = resolve_runtime_context(args.host, args.client, {}, {
        'session_id': args.session_id, 'cwd': args.cwd, 'vault_path': args.vault,
        'config_path': args.config, 'resource_root': args.resource_root,
        'index_path': args.index, 'state_path': args.state})
    proof = {name: str(getattr(context, name)) for name in (
        'host', 'client', 'native_session_id', 'worktree', 'vault_path', 'config_path',
        'resource_root', 'index_path', 'state_path')}
    proof['mutation_contexts'] = []
    original = note_transactions.apply_mutations
    def observed(actor, *positional, **keywords):
        assert actor is context and current_runtime_context() is context
        proof['mutation_contexts'].append({name: str(getattr(actor, name)) for name in (
            'host', 'client', 'native_session_id', 'vault_path', 'index_path')})
        return original(actor, *positional, **keywords)
    note_transactions.apply_mutations = observed
    writer_args = args.writer_args[1:] if args.writer_args[:1] == ['--'] else args.writer_args
    sys.argv = [str(Path(note_writer.__file__)), *writer_args]
    try:
        with using_runtime_context(context):
            note_writer.main()
    finally:
        # Captured stderr is a private pipe; normal CLI diagnostics stay intact.
        print('NATIVE_CONTEXT_PROOF:' + json.dumps(proof), file=sys.stderr)


if __name__ == '__main__':
    main()
