"""Use the selected actor; retain explicit unbound pipeline compatibility."""
from pathlib import Path
from types import MappingProxyType

from runtime_context import RuntimeContext, using_runtime_context, current_runtime_context
from operation_state import operation_directory

OPERATION_ID = 'b' * 32


def _context(vault):
    vault = Path(vault).resolve()
    selected = current_runtime_context()
    if selected is not None:
        if selected.vault_path.resolve() != vault:
            raise ValueError("Pipeline test vault differs from selected runtime vault")
        return selected
    root = vault.parent
    return RuntimeContext('claude', 'cli', 'synthetic-pipeline-test', root, root, None,
        vault, root / 'pipeline-config.json', MappingProxyType({}),
        Path(__file__).resolve().parents[1], root / 'pipeline-index.sqlite3',
        root / 'pipeline-private-state')


def private_pipeline_output(vault, name='pipeline-out.json'):
    return operation_directory(_context(vault), OPERATION_ID)[1] / name


def run_native_pipeline(*args, **kwargs):
    from open_item_dedup import deep_analysis_pipeline
    vault = kwargs.get('vault_path', args[3] if len(args) > 3 else None)
    context = _context(vault)
    kwargs['operation_id'] = OPERATION_ID
    kwargs.setdefault('db_path', str(context.index_path))
    with using_runtime_context(context):
        return deep_analysis_pipeline(*args, **kwargs)
