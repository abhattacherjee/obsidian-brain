"""Run a real lifecycle hook after resolving its explicit disposable actor."""
import argparse
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
    original = note_transactions.apply_mutations
    def observed(actor, *positional, **keywords):
        assert current_runtime_context() is context
        for field in fields:
            assert getattr(actor, field) == getattr(context, field), field
        proof['mutation_contexts'].append({field: str(getattr(actor, field)) for field in fields})
        return original(actor, *positional, **keywords)
    note_transactions.apply_mutations = observed
    try:
        with using_runtime_context(context):
            module = importlib.import_module(Path(args.script).stem)
            module.main()
    finally:
        print('NATIVE_CONTEXT_PROOF:' + json.dumps(proof), file=sys.stderr)


if __name__ == '__main__':
    main()
