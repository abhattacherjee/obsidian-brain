"""Native host entry points for the shared runtime."""


def selected_home(host, context=None):
    if host == 'claude':
        from .claude import selected_home as resolve
    elif host == 'codex':
        from .codex import selected_home as resolve
    else:
        raise ValueError('Select the native diagnostic host explicitly')
    return resolve(context)


def metrics_path(context):
    if context.host not in {'claude', 'codex'}:
        raise ValueError('Select the native metrics host explicitly')
    from note_transactions import session_state_path
    return session_state_path(context) / 'logs' / 'obsidian-brain-summarizer-metrics.jsonl'


def private_workdir(context=None):
    from runtime_context import current_runtime_context
    context = context if context is not None else current_runtime_context()
    if context is not None:
        from note_transactions import session_state_path
        return session_state_path(context) / 'jobs'
    from .claude import legacy_workdir
    return legacy_workdir()
