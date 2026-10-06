"""New vault writers must use the shared mutation protocol.

This is a syntax inventory, not data-flow analysis. Private artifact owners are
listed explicitly; a new direct writer needs review before joining that list.
"""
import ast
from pathlib import Path


_PRIVATE = {
    'hooks/brain_cli.py': {'main'},
    'hooks/check_items_cache.py': {'save_cache', '_try_create_lock'},
    'hooks/check_items_cli.py': {'run_semantic_merge', '_dispatch_classifier_chunk', 'run_classifier'},
    'hooks/deep_cli.py': {'run_pipeline', '_load_acted_items', '_save_acted_items'},
    'hooks/emerge_cli.py': {'run_emerge_themes', 'run_build_note'},
    'hooks/hook_bootstrap.py': {'log_import_failure'},
    'hooks/note_writer.py': {'_acquire_lock', '_release_lock'},
    'hooks/obsidian_session_hint.py': {'_write_bootstrap_atomic', '_append_hook_log'},
    'hooks/obsidian_session_log.py': {'_cleanup_session_cache'},
    'hooks/obsidian_session_reaper.py': {'_write_watermark_atomic', '_permission_canary'},
    'hooks/obsidian_utils.py': {
        '_first_seen_date', '_reap_stale_retro_sentinels',
        'mark_retro_classification_pending', 'clear_retro_classification_pending',
        '_cleanup_stale_locks', 'claim_hook_run', 'claim_hook_run._create',
        'release_hook_run', '_append_sessionend_log', '_append_reaper_log',
        'cache_set', 'cache_invalidate', 'prepare_summary_input',
    },
    'hooks/open_item_dedup.py': {
        'deep_analysis_pipeline', 'merge_groups_semantically', 'classify_groups_with_agent',
    },
    'hooks/summarizer_metrics.py': {'append_metrics_record'},
    'hooks/vault_index.py': {'ensure_index', 'rebuild_index'},
}
_DOCTOR_TEMP_CLEANUP = {
    'audit_historic_repairs.py', 'missing_frontmatter_fence.py',
    'project_name_canonicalization.py', 'project_name_normalization.py',
    'source_sessions.py', 'spurious_wikilinks.py',
}
_VAULT_WRITERS = {
    'hooks/obsidian_utils.py': {
        'write_vault_note', 'flip_note_status', 'find_unsummarized_notes',
        'upgrade_note_with_summary',
    },
    'hooks/note_writer.py': {'_atomic_rewrite'},
    'hooks/deep_cli.py': {'run_batch_edit'},
    'hooks/open_item_dedup.py': {
        'dedup_note_open_items', 'batch_cascade_checkoff', 'cascade_group_members',
    },
    'hooks/check_items_report.py': {'write_check_items_dashboard'},
    'hooks/wiki.py': {'_write', 'rebuild_wiki_index', 'append_log'},
}


def _readonly_flags(node):
    allowed = {"O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"}
    if isinstance(node, ast.Constant):
        return node.value == 0
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _readonly_flags(node.left) and _readonly_flags(node.right)
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr in allowed
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
        return (len(node.args) == 3 and not node.keywords and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "os" and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in allowed and isinstance(node.args[2], ast.Constant)
                and node.args[2].value == 0)
    return False


def _writes(node):
    name = ast.unparse(node.func)
    if name in {'os.rename', 'os.replace', 'os.unlink', 'os.remove', 'os.write', 'shutil.move'}:
        return True
    if name == 'os.open':
        flags = ast.unparse(node.args[1]) if len(node.args) > 1 else ''
        for keyword in node.keywords:
            if keyword.arg == 'flags':
                flags = ast.unparse(keyword.value)
        if any(flag in flags for flag in ('O_WRONLY', 'O_RDWR', 'O_CREAT', 'O_APPEND', 'O_TRUNC')):
            return True
        flag_node = node.args[1] if len(node.args) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == 'flags':
                flag_node = keyword.value
        return not _readonly_flags(flag_node)
    if name.endswith(('.write_text', '.write_bytes', '.write', '.writelines', '.rename', '.unlink')):
        return True
    if name in {'open', 'io.open', 'os.fdopen'} or name.endswith('.open'):
        mode = None
        for keyword in node.keywords:
            if keyword.arg == 'mode':
                mode = keyword.value
        position = 1 if name in {'open', 'io.open', 'os.fdopen'} else 0
        if mode is None and len(node.args) > position:
            mode = node.args[position]
        if mode is None:
            return False
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return any(flag in mode.value for flag in 'wax+')
        return True  # A computed mode requires review.
    return False


def _allowed(file, function, node):
    name = ast.unparse(node.func)
    if file == 'hooks/note_transactions.py':
        return True
    if file == 'hooks/obsidian_retro_gate.py' and function == 'native_decision':
        # This function owns only its private decision journal.
        if name == 'os.open':
            return (len(node.args) == 3 and not node.keywords
                    and ast.unparse(node.args[0]) == "str(directory / (key + '.json'))"
                    and ast.unparse(node.args[1]) == 'os.O_CREAT | os.O_EXCL | os.O_WRONLY'
                    and isinstance(node.args[2], ast.Constant) and node.args[2].value == 0o600)
        if name == 'os.fdopen':
            return (len(node.args) == 2 and ast.unparse(node.args[0]) == 'descriptor'
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value == 'w')
        return False
    if file == 'hooks/brain_cli.py' and function == 'main':
        return name in {'stdout.write', 'stderr.write'}
    if function in _PRIVATE.get(file, set()):
        return True
    if function == 'apply' and file.startswith('scripts/vault_doctor_checks/'):
        if Path(file).name in _DOCTOR_TEMP_CLEANUP:
            return name == 'os.unlink' and len(node.args) == 1 and ast.unparse(node.args[0]) == 'tmp'
        if Path(file).name == 'encoding_corruption.py':
            return name == 'Path(backup_path).write_bytes'
    return False


def _violations(source, file):
    violations = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.functions = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            function = '.'.join(self.functions) or '<module>'
            if _writes(node) and not _allowed(file, function, node):
                violations.append((file, function, node.lineno, ast.unparse(node.func)))
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return violations


def test_vault_writers_do_not_bypass_transaction_boundary():
    root = Path(__file__).resolve().parents[1]
    violations = []
    for directory in ('hooks', 'scripts/vault_doctor_checks'):
        for path in sorted((root / directory).rglob('*.py')):
            file = path.relative_to(root).as_posix()
            violations.extend(_violations(path.read_text(), file))
    assert not violations, '\n'.join(map(str, violations))
    for file, functions in _VAULT_WRITERS.items():
        assert functions.isdisjoint(_PRIVATE.get(file, set()))


def test_scanner_rejects_inserted_vault_publication():
    source = 'def write_vault_note():\n    Path("note.md").write_text("lost edit")\n'
    assert _violations(source, 'hooks/obsidian_utils.py') == [
        ('hooks/obsidian_utils.py', 'write_vault_note', 2, "Path('note.md').write_text"),
    ]


def test_scanner_allows_stdout_but_not_file_write_in_cli():
    source = 'def main():\n    stdout.write("result")\n    Path("note.md").write_text("bypass")\n'
    violations = _violations(source, 'hooks/brain_cli.py')
    assert len(violations) == 1
    assert violations[0][-1] == "Path('note.md').write_text"


def test_doctor_backup_exception_cannot_write_note():
    source = 'def apply():\n    Path(backup_path).write_bytes(raw)\n    Path(note_path).write_bytes(raw)\n'
    violations = _violations(source, 'scripts/vault_doctor_checks/encoding_corruption.py')
    assert len(violations) == 1
    assert violations[0][-1] == 'Path(note_path).write_bytes'


def test_scanner_rejects_computed_open_modes():
    source = 'def writer():\n    open(path, mode)\n    os.open(path, flags)\n'
    assert len(_violations(source, 'hooks/new_writer.py')) == 2


def test_scanner_accepts_readonly_flag_expressions_but_rejects_unknown_and_write_flags():
    source = '''def reader():
    os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC)
    os.open(path, flags=os.O_RDONLY | os.O_DIRECTORY)
    os.open(path, os.O_RDONLY | unknown_flags)
    os.open(path, os.O_RDONLY | getattr(os, "O_WRONLY", 0))
'''
    violations = _violations(source, 'hooks/new_reader.py')
    assert [row[2] for row in violations] == [4, 5]


def test_retro_journal_exception_is_private_and_cannot_publish_notes():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/obsidian_retro_gate.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'native_decision')
    assert [arg.arg for arg in function.args.args] == ['context', 'data']
    source = ast.unparse(function)
    assert "directory = session_state_path(context) / 'retro-decisions'" in source
    assert 'directory.chmod(448)' in source
    assert 'key = hashlib.sha256(' in source
    assert not _violations(source, 'hooks/obsidian_retro_gate.py')
    source += '\n    Path(note_path).write_text("bypass")\n'
    violations = _violations(source, 'hooks/obsidian_retro_gate.py')
    assert len(violations) == 1
    assert violations[0][-1] == 'Path(note_path).write_text'
