"""New vault writers must use the shared mutation protocol.

This is a syntax inventory, not data-flow analysis. Private artifact owners are
listed explicitly; a new direct writer needs review before joining that list.
"""
import ast
from pathlib import Path


_PRIVATE = {
    'hooks/brain_cli.py': {'main'},
    'hooks/check_items_cache.py': {'save_cache', '_try_create_lock'},
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


def _writes(node, readonly_names=()):
    name = ast.unparse(node.func)
    if name in {'_publish_private_json', '_write_private', '_publish_config'}:
        return True
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
        return not (_readonly_flags(flag_node) or
                    isinstance(flag_node, ast.Name) and flag_node.id in readonly_names)
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
    if file == 'hooks/check_items_cli.py' and function in {'run_semantic_merge', 'run_classifier'}:
        return (name == '_publish_private_json' and len(node.args) == 2
                and ast.unparse(node.args[0]) == 'output_path'
                and ast.unparse(node.args[1]) in {'result.data', 'ordered'})
    if file == 'hooks/operation_state.py' and function == '_store_artifact_locked':
        return (name == '_write_private' and len(node.args) == 3
                and ast.unparse(node.args[0]) in {'path', 'manifest_path'}
                and ast.unparse(node.args[2]) == 'context')
    if file == 'hooks/ai_backend.py' and function == '_run_bounded':
        return (name == 'os.write' and len(node.args) == 2
                and ast.unparse(node.args[0]) == 'pipe.fileno()'
                and ast.unparse(node.args[1]) == 'payload[:65536]')
    if file == 'hooks/ai_backend.py' and function == '_write_ai_receipt':
        if name == 'os.open':
            return (not node.keywords and len(node.args) == 3
                    and [ast.unparse(arg) for arg in node.args[:2]] == ['path', 'flags']
                    and isinstance(node.args[2], ast.Constant) and node.args[2].value == 0o600)
        return (name == 'os.write' and not node.keywords
                and [ast.unparse(arg) for arg in node.args] == ['descriptor', 'payload'])
    if file == 'hooks/ai_adapters/codex.py' and function == '_send':
        return (name == 'os.write' and [ast.unparse(arg) for arg in node.args]
                == ['self.process.stdin.fileno()', 'payload'])
    if file == 'hooks/ai_adapters/codex.py' and function in {'request', 'initialize'}:
        return name == 'self.process.stdin.write'
    if file == 'hooks/ai_adapters/codex.py' and function == 'execute':
        if name == 'os.open':
            return (len(node.args) == 3 and ast.unparse(node.args[0]) == 'path'
                    and ast.unparse(node.args[1]) == 'os.O_WRONLY | os.O_CREAT | os.O_EXCL'
                    and isinstance(node.args[2], ast.Constant) and node.args[2].value == 0o600)
        if name == 'os.fdopen':
            return [ast.unparse(arg) for arg in node.args] == ['fd', "'w'"]
        if name == 'stream.write':
            return [ast.unparse(arg) for arg in node.args] == ['text']
        return False
    if file == 'hooks/session_auxiliary_state.py' and function == 'cross_run_write':
        return (name == '_write_private' and not node.keywords
                and [ast.unparse(arg) for arg in node.args] == [
                    'path', 'json.dumps(value).encode()',
                    'replace(context, state_path=coordination_path(context))'])
    if file == 'hooks/session_auxiliary_state.py' and function == '_write':
        if name == 'os.fdopen':
            return [ast.unparse(arg) for arg in node.args] == ['descriptor', "'w'"]
        if name == 'os.replace':
            return [ast.unparse(arg) for arg in node.args] == ['temporary', 'path']
        if name == 'os.unlink':
            return [ast.unparse(arg) for arg in node.args] == ['temporary']
        return False
    if file == 'hooks/session_auxiliary_state.py' and function == '_locked':
        return (name == 'os.open' and not node.keywords and len(node.args) == 3
                and ast.unparse(node.args[0]) == 'path'
                and ast.unparse(node.args[1]) == "os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)"
                and isinstance(node.args[2], ast.Constant) and node.args[2].value == 0o600)
    if file == 'hooks/operation_state.py' and function == '_operation_lock':
        return (name == 'os.open' and len(node.args) == 3
                and ast.unparse(node.args[0]) == 'path'
                and ast.unparse(node.args[1]) == "os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)"
                and isinstance(node.args[2], ast.Constant) and node.args[2].value == 0o600)
    if file == 'hooks/operation_state.py' and function == '_write_private':
        if name == 'os.fdopen':
            return [ast.unparse(arg) for arg in node.args] == ['descriptor', "'wb'"]
        if name == 'stream.write':
            return [ast.unparse(arg) for arg in node.args] == ['content']
        if name == 'os.replace':
            return [ast.unparse(arg) for arg in node.args] == ['temporary', 'path']
        if name == 'os.unlink':
            return [ast.unparse(arg) for arg in node.args] == ['temporary']
        return False
    if file == 'hooks/skill_procedures.py' and function == '_configure':
        return (name == '_publish_config' and [ast.unparse(arg) for arg in node.args]
                == ['context', 'config', 'expected'])
    if file == 'hooks/skill_procedures.py' and function == '_publish_config':
        if name == 'os.fdopen':
            return [ast.unparse(arg) for arg in node.args] == ['descriptor', "'w'"]
        if name == 'os.replace':
            return [ast.unparse(arg) for arg in node.args] == ['temporary', 'path']
        if name == 'os.unlink':
            return [ast.unparse(arg) for arg in node.args] == ['temporary']
        return False
    if file == 'hooks/skill_procedures.py' and function == 'run_operation':
        return name == 'stderr.write'
    if file == 'hooks/check_items_cli.py' and function == '_publish_private_json':
        if name == 'os.replace':
            return [ast.unparse(arg) for arg in node.args] == ['temporary', 'destination']
        if name == 'os.unlink':
            return [ast.unparse(arg) for arg in node.args] == ['temporary']
        if name == 'os.fdopen':
            return (len(node.args) == 2 and ast.unparse(node.args[0]) == 'descriptor'
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value == 'w')
        return False
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
            self.readonly_names = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            assignments = {}
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    assignments.setdefault(child.id, []).append(child)
            readonly = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Assign) and _readonly_flags(child.value):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and len(assignments[target.id]) == 1:
                            readonly.add(target.id)
            self.readonly_names.append(readonly)
            self.generic_visit(node)
            self.readonly_names.pop()
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            function = '.'.join(self.functions) or '<module>'
            readonly = self.readonly_names[-1] if self.readonly_names else ()
            if _writes(node, readonly) and not _allowed(file, function, node):
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


def test_classifier_private_publication_requires_jobs_json_and_rejects_note_writes():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/check_items_cli.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_publish_private_json')
    source = ast.unparse(function)
    assert "jobs = session_state_path(context) / 'jobs'" in source
    assert 'destination.resolve().relative_to(jobs.resolve())' in source
    assert "destination.suffix != '.json'" in source
    assert 'context.state_path.resolve().is_relative_to(context.vault_path.resolve())' in source
    assert 'destination.is_symlink()' in source
    assert 'stat.S_ISREG(details.st_mode)' in source
    assert 'details.st_uid != os.getuid()' in source
    assert 'stat.S_IMODE(details.st_mode) != 384' in source
    assert not _violations(source, 'hooks/check_items_cli.py')
    violations = _violations(source + '\n    Path(note_path).write_text("bypass")\n',
                             'hooks/check_items_cli.py')
    assert len(violations) == 1
    assert violations[0][-1] == 'Path(note_path).write_text'


def test_native_pipe_exceptions_cannot_write_a_file_descriptor():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/ai_backend.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_run_bounded')
    source = ast.unparse(function)
    assert "selector.register(process.stdin, selectors.EVENT_WRITE, 'input')" in source
    assert "if key.data == 'input':" in source
    assert not _violations(source, 'hooks/ai_backend.py')
    assert _violations(source + '\n    os.write(note_fd, payload)\n', 'hooks/ai_backend.py')
    for name in ('request', 'initialize'):
        source = f'def {name}():\n    self.process.stdin.write(payload)\n    Path(note_path).write_text(payload)\n'
        assert len(_violations(source, 'hooks/ai_adapters/codex.py')) == 1


def test_codex_wire_files_require_private_directory_outside_vault():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/ai_adapters/codex.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'execute')
    source = ast.unparse(function)
    assert 'metadata = directory.lstat()' in source
    assert 'not stat.S_ISDIR(metadata.st_mode)' in source
    assert 'metadata.st_uid != os.geteuid()' in source
    assert 'stat.S_IMODE(metadata.st_mode) != 448' in source
    assert 'directory.resolve().is_relative_to(context.vault_path.resolve())' in source
    expected_assignment = ast.parse(
        "schema_path, output_path = (directory / 'schema.json', directory / 'result.json')"
    ).body[0]
    assert any(
        isinstance(node, ast.Assign)
        and ast.dump(node, include_attributes=False)
        == ast.dump(expected_assignment, include_attributes=False)
        for node in ast.walk(function)
    )
    assert not _violations(source, 'hooks/ai_adapters/codex.py')
    assert _violations(source + '\n    Path(note_path).write_text(prompt)\n',
                       'hooks/ai_adapters/codex.py')


def test_operation_artifact_boundary_and_exact_write_calls():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/operation_state.py').read_text())
    functions = {node.name: ast.unparse(node) for node in tree.body
                 if isinstance(node, ast.FunctionDef)}
    directory = functions['operation_directory']
    assert 'selected_state == vault or vault in selected_state.parents' in directory
    assert "root = session_state_path(context) / 'jobs'" in directory
    assert 'directory.stat().st_uid != os.getuid()' in directory
    assert '_no_symlinks(path)' in functions['_artifact']
    assert "path = _artifact(context, operation_id, name)" in functions['_store_artifact_locked']
    lock = functions['_operation_lock']
    assert "path = directory / '.operation.lock'" in lock
    assert 'stat.S_ISREG(info.st_mode)' in lock
    assert 'info.st_uid != os.getuid()' in lock
    for name in ('_write_private', '_operation_lock'):
        source = functions[name]
        assert not _violations(source, 'hooks/operation_state.py')
        assert _violations(source + '\n    Path(note_path).write_text("bypass")\n',
                           'hooks/operation_state.py')
    assert _violations('def writer():\n    _write_private(vault_note, content)\n', 'hooks/new_writer.py')
    assert _violations('def writer():\n    _publish_private_json(vault_note, content)\n', 'hooks/new_writer.py')


def test_config_private_exception_is_bound_to_selected_config_and_cas():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/skill_procedures.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_publish_config')
    assert [arg.arg for arg in function.args.args] == ['context', 'value', 'expected_revision']
    source = ast.unparse(function)
    assert 'path = Path(context.config_path)' in source
    assert "if path.suffix != '.json':" in source
    assert 'path.resolve().is_relative_to(context.vault_path.resolve())' in source
    assert source.count('_no_symlinks(path)') == 3
    assert 'stat.S_ISREG(details.st_mode)' in source
    assert 'details.st_uid != os.getuid()' in source
    assert 'stat.S_IMODE(details.st_mode) != 384' in source
    assert 'hashlib.sha256(raw).hexdigest() != expected_revision' in source
    assert not _violations(source, 'hooks/skill_procedures.py')
    assert _violations(source + '\n    Path(note_path).write_text("bypass")\n',
                       'hooks/skill_procedures.py')
    assert _violations('def writer():\n    _publish_config(context, data, revision)\n',
                       'hooks/new_writer.py')


def test_native_rpc_send_exception_is_only_deadline_bounded_stdin():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/ai_adapters/codex.py').read_text())
    owners = [(owner.name, node) for owner in tree.body if isinstance(owner, ast.ClassDef)
              for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == '_send']
    assert len(owners) == 1 and owners[0][0] == 'NativeRpc'
    source = ast.unparse(owners[0][1])
    assert 'selector.register(self.process.stdin, selectors.EVENT_WRITE)' in source
    assert 'remaining = self.deadline - time.monotonic()' in source
    assert 'if remaining <= 0:' in source
    assert 'payload = payload[written:]' in source
    assert not _violations(source, 'hooks/ai_adapters/codex.py')
    assert _violations(source + '\n    os.write(note_fd, payload)\n', 'hooks/ai_adapters/codex.py')


def test_auxiliary_state_exceptions_require_selected_private_path():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/session_auxiliary_state.py').read_text())
    functions = {node.name: ast.unparse(node) for node in tree.body if isinstance(node, ast.FunctionDef)}
    boundary = functions['_private_path']
    assert 'root = directory(context, kind)' in boundary
    assert 'path.resolve() != root / path.name' in boundary
    assert '_no_symlinks(path)' in boundary
    for name in ('_write', '_locked'):
        source = functions[name]
        assert 'path = _private_path(context, path)' in source
        assert not _violations(source, 'hooks/session_auxiliary_state.py')
        assert _violations(source + '\n    Path(note_path).write_text("bypass")\n', 'hooks/session_auxiliary_state.py')
    assert _violations('def _write():\n    os.replace(temporary, vault_note)\n', 'hooks/session_auxiliary_state.py')


def test_readonly_local_flags_cannot_hide_reassigned_write_flags():
    safe = 'def reader():\n    flags = os.O_RDONLY | os.O_DIRECTORY\n    os.open(path, flags)\n'
    assert not _violations(safe, 'hooks/new_reader.py')
    changed = safe.replace('    os.open', '    flags = os.O_WRONLY\n    os.open')
    assert _violations(changed, 'hooks/new_reader.py')
    assert _violations(safe.replace('os.O_RDONLY', 'os.O_WRONLY'), 'hooks/new_reader.py')


def test_cross_run_cache_exception_cannot_publish_a_note():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/session_auxiliary_state.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'cross_run_write')
    source = ast.unparse(function)
    assert 'path = cross_run_directory(context) / name' in source
    assert not _violations(source, 'hooks/session_auxiliary_state.py')
    assert _violations(source.replace('_write_private(path,', '_write_private(note_path,'),
                       'hooks/session_auxiliary_state.py')


def test_ai_receipt_exception_is_scoped_and_cannot_publish_vault_content():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'hooks/ai_backend.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_write_ai_receipt')
    source = ast.unparse(function)
    assert "if context is None:\n        return" in source
    assert "from session_auxiliary_state import directory" in source
    assert "path = directory(context, 'logs') / 'ai-operations.jsonl'" in source
    assert "flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)" in source
    assert 'details = os.fstat(descriptor)' in source
    assert 'not stat.S_ISREG(details.st_mode)' in source
    assert 'details.st_uid != os.getuid()' in source
    assert 'details.st_nlink != 1' in source
    assert 'os.fchmod(descriptor, 384)' in source
    assert 'fcntl.flock(descriptor, fcntl.LOCK_EX)' in source
    assert not _violations(source, 'hooks/ai_backend.py')

    # The path helper independently refuses vault storage and symbolic links.
    auxiliary = ast.parse((root / 'hooks/session_auxiliary_state.py').read_text())
    helper = next(node for node in auxiliary.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'directory')
    helper_source = ast.unparse(helper)
    assert '_no_symlinks(root)' in helper_source
    assert '_no_symlinks(scoped)' in helper_source
    assert 'root.resolve() == context.vault_path.resolve()' in helper_source
    assert 'context.vault_path.resolve() in root.resolve().parents' in helper_source

    forbidden = [
        source + '\n    Path(note_path).write_text("bypass")\n',
        source + '\n    os.write(note_fd, payload)\n',
        source.replace('os.open(path, flags, 384)', 'os.open(note_path, flags, 384)'),
        source.replace('os.open(path, flags, 384)', 'os.open(path, flags, 420)'),
        source.replace('os.write(descriptor, payload)', 'os.write(note_fd, payload)'),
        source.replace('def _write_ai_receipt(', 'def unrelated_vault_writer('),
    ]
    assert all(_violations(mutant, 'hooks/ai_backend.py') for mutant in forbidden)
    assert _violations(source, 'hooks/unreviewed_writer.py')
