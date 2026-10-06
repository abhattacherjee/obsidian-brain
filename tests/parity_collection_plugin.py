"""Draft collection guard: declarations cover behavior, not fixture file origin."""
import ast
import json
import inspect
import hashlib
import functools
import sys
from pathlib import Path
import pytest

HOSTS = frozenset({'claude', 'codex'})
MODULES = {}


def pytest_addoption(parser):
    parser.addoption('--parity-matrix', required=False)
    parser.addoption('--parity-audit-report', required=False)


def pytest_configure(config):
    config.addinivalue_line('markers', 'host_only(host, reason, capability): declared native-format exception')


def _calls(node, imports=None):
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            if isinstance(call.func, ast.Name):
                yield (imports or {}).get(call.func.id, call.func.id)
                yield call.func.id
            elif isinstance(call.func, ast.Attribute):
                text = ast.unparse(call.func)
                first, *rest = text.split('.')
                yield '.'.join([(imports or {}).get(first, first), *rest])
                yield call.func.attr


def _scope(item, capabilities, launchers=(), errors=None):
    def reject(message):
        if errors is None:
            raise pytest.UsageError(message)
        errors.append(message)
    root = Path(str(item.config.rootpath))
    search = [Path(str(item.path)).parent, root, root / 'hooks', root / 'scripts',
              root / 'tests']

    def module(path):
        path = path.resolve()
        key = (path, path.stat().st_mtime_ns, path.stat().st_size)
        if key not in MODULES:
            tree = ast.parse(path.read_text())
            imports, definitions = {}, {}
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for name in node.names:
                        imports[name.asname or name.name.split('.')[0]] = (
                            name.name if name.asname else name.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    prefix = '.' * node.level + (node.module or '')
                    for name in node.names:
                        separator = '.' if node.module else ''
                        imports[name.asname or name.name] = prefix + separator + name.name
            def collect_definitions(nodes, class_prefix=''):
                for node in nodes:
                    if isinstance(node, ast.ClassDef):
                        collect_definitions(node.body, class_prefix + node.name + '.')
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        definitions.setdefault(class_prefix + node.name, []).append(node)
                        collect_definitions(node.body)
            collect_definitions(tree.body)
            MODULES[key] = imports, definitions
        return MODULES[key]

    def imported_target(call, owner):
        directories = search
        if call.startswith('.'):
            level = len(call) - len(call.lstrip('.'))
            directory = owner.parent
            for _ in range(level - 1):
                directory = directory.parent
            directories = [directory]
            call = call.lstrip('.')
        parts = call.split('.')
        # Literal/container attribute calls are not import-qualified helpers.
        # Launcher detection below still examines every call independently.
        if not all(part.isidentifier() for part in parts):
            return None
        for length in range(len(parts) - 1, 0, -1):
            relative = Path(*parts[:length])
            for directory in directories:
                candidates = [directory / relative.with_suffix('.py'),
                              directory / relative / '__init__.py']
                for candidate in candidates:
                    if candidate.is_file():
                        return candidate, parts[-1]
        return None

    parameters = getattr(getattr(item, 'callspec', None), 'params', {})
    initial_bindings = {name: value for name, value in parameters.items()
                        if isinstance(value, str)}
    test_name = item.originalname or item.name.split('[', 1)[0]
    test_class = getattr(item, 'cls', None)
    if test_class is not None:
        test_name = test_class.__name__ + '.' + test_name
    pending = [(Path(str(item.path)), test_name,
                initial_bindings)]
    # Fixture definitions carry their true source file, including conftest and
    # imported fixture modules. Their local helpers need the same recursion.
    for fixture in item._fixtureinfo.name2fixturedefs.values():
        for definition in fixture:
            try:
                filename = inspect.getsourcefile(definition.func)
            except TypeError:
                filename = None
            if filename and any(Path(filename).resolve().is_relative_to(directory.resolve())
                                for directory in search):
                pending.append((Path(filename), definition.func.__name__, initial_bindings))
    seen, calls, launcher_scope = set(), set(), set()
    while pending:
        path, name, bindings = pending.pop()
        identity = (path.resolve(), name, tuple(sorted(bindings.items())))
        if identity in seen:
            continue
        seen.add(identity)
        imports, definitions = module(path)
        for definition in definitions.get(name, []):
            local_bindings = dict(bindings)
            for assignment in ast.walk(definition):
                if isinstance(assignment, ast.Assign):
                    value = (assignment.value.value if isinstance(assignment.value, ast.Constant)
                             and isinstance(assignment.value.value, str) else
                             local_bindings.get(assignment.value.id)
                             if isinstance(assignment.value, ast.Name) else None)
                    for target in assignment.targets:
                        if isinstance(target, ast.Name) and value is not None:
                            local_bindings[target.id] = value
            found = set(_calls(definition, imports))
            calls.update(found)
            for invocation in ast.walk(definition):
                if not isinstance(invocation, ast.Call):
                    continue
                called = ast.unparse(invocation.func)
                first, *rest = called.split('.')
                called = '.'.join([imports.get(first, first), *rest])
                def literal(argument):
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        return argument.value
                    if isinstance(argument, ast.Name):
                        return local_bindings.get(argument.id)
                    return None
                if called == 'skill_procedures.run_operation' and len(invocation.args) > 1:
                    skill = literal(invocation.args[1])
                    if skill is not None:
                        capability = 'skill.' + skill
                        if capability in {entry['id'] for entry in capabilities}:
                            launcher_scope.add(capability)
                if isinstance(invocation.func, ast.Name) and invocation.func.id in definitions:
                    target = (path, invocation.func.id)
                elif (isinstance(invocation.func, ast.Attribute)
                      and isinstance(invocation.func.value, ast.Name)
                      and invocation.func.value.id in {'self', 'cls'}
                      and (sibling := name.rsplit('.', 1)[0] + '.' + invocation.func.attr)
                      in definitions):
                    target = (path, sibling)
                elif called in definitions:
                    target = (path, called)
                else:
                    target = imported_target(called, path)
                if target:
                    target_path, target_name = target
                    _, target_definitions = module(target_path)
                    for target_definition in target_definitions.get(target_name, []):
                        names = [argument.arg for argument in target_definition.args.args]
                        if names and names[0] in {'self', 'cls'} and isinstance(invocation.func, ast.Attribute):
                            names = names[1:]
                        child_bindings = {field: value for field, argument in zip(names, invocation.args)
                                          if (value := literal(argument)) is not None}
                        for keyword in invocation.keywords:
                            value = literal(keyword.value)
                            if keyword.arg and value is not None:
                                child_bindings[keyword.arg] = value
                        pending.append((target_path, target_name, child_bindings))
                if called not in {'subprocess.run', 'subprocess.Popen',
                                   'subprocess.call', 'subprocess.check_call',
                                   'subprocess.check_output', 'os.system'}:
                    continue
                expression = ast.unparse(invocation)
                source = str(path.resolve().relative_to(root.resolve()))
                contract = next((entry for entry in launchers
                                 if entry.get('source') == source
                                 and entry.get('expression') == expression), None)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if not contract or contract.get('source_sha256') != digest:
                    reject(f'{item.nodeid}: launcher contract missing or changed: '
                           f'{source}:{invocation.lineno}')
                    continue
                capability = contract.get('capability')
                if capability:
                    if capability not in {entry['id'] for entry in capabilities}:
                        reject(f'{item.nodeid}: unknown launcher capability')
                        continue
                    launcher_scope.add(capability)
                elif (contract.get('scope') not in {'support', 'structural'}
                      or not contract.get('reason')):
                    reject(f'{item.nodeid}: launcher exemption needs a reviewed reason')
    return launcher_scope | {entry['id'] for entry in capabilities
                             if calls.intersection(entry.get('calls', []))}


def pytest_collection_modifyitems(config, items):
    path = config.getoption('--parity-matrix')
    if not path:
        raise pytest.UsageError('Parity capability matrix is required for collection.')
    matrix = json.loads(Path(path).read_text())
    entries = matrix['capabilities']
    by_id = {entry['id']: entry for entry in entries}
    errors, paired = [], {}
    for item in items:
        scope = _scope(item, entries, matrix.get('launchers', []), errors)
        if not scope:
            continue
        marker = item.get_closest_marker('host_only')
        uses_host = 'host' in item.fixturenames
        if marker and uses_host:
            errors.append(f'{item.nodeid}: host fixture and host_only conflict')
            continue
        if marker:
            host = marker.args[0] if marker.args else marker.kwargs.get('host')
            reason = marker.kwargs.get('reason') or (marker.args[1] if len(marker.args) > 1 else None)
            capability = marker.kwargs.get('capability')
            entry = by_id.get(capability)
            if host not in HOSTS or not reason or capability not in scope or entry is None:
                errors.append(f'{item.nodeid}: invalid host_only declaration')
                continue
            # Fixture-origin formats are separate from reusable data operations.
            if entry['required'] or any(by_id[name]['required'] for name in scope):
                errors.append(f'{item.nodeid}: required behavior cannot waive another host')
                continue
            other = next(iter(HOSTS - {host}))
            if entry['hosts'].get(other, {}).get('status') != 'unsupported:' + reason:
                errors.append(f'{item.nodeid}: host_only reason does not match capability matrix')
            continue
        if not uses_host:
            errors.append(f'{item.nodeid}: capture/writer/CLI/skill test needs host fixture or host_only')
            continue
        if 'selected_host_context' not in item.fixturenames:
            errors.append(f'{item.nodeid}: selected_host_context fixture is required for behavior')
            continue
        host = getattr(item, 'callspec', None)
        host = host.params.get('host') if host is not None else None
        if host not in HOSTS:
            errors.append(f'{item.nodeid}: host fixture must explicitly parameterize claude and codex')
            continue
        key = (item.nodeid.split('[', 1)[0], item.originalname, tuple(sorted(scope)),
               tuple(sorted((name, repr(value)) for name, value in item.callspec.params.items() if name != 'host')))
        paired.setdefault(key, set()).add(host)
    for key, hosts in paired.items():
        if hosts != HOSTS:
            errors.append(f'{key[0]}::{key[1]}: missing invoking host {sorted(HOSTS - hosts)}')
    report = config.getoption('--parity-audit-report')
    if report:
        Path(report).write_text(json.dumps({'collected': len(items), 'errors': errors}, indent=2) + '\n')
    if errors:
        raise pytest.UsageError('\n'.join(errors))


CONTEXT_APIS = {
    'capture': ('capture_checkpoint', 'recover_pending', 'recover_registered'),
    'note_transactions': ('apply_mutations', 'delete_note', 'move_note', 'ownership_lock'),
    'ai_backend': ('execute_ai',),
    'skill_procedures': ('run_operation',),
    'session_auxiliary_state': ('directory', 'cache_get', 'cache_update', 'first_seen_date'),
}


def _remove_context_guards(item):
    root = Path(str(item.config.rootpath)).resolve()
    api_modules = {name for api in CONTEXT_APIS for name in (api, 'hooks.' + api)}
    for spelling, module in list(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if spelling not in api_modules and (not filename or not Path(filename).resolve().is_relative_to(root)):
            continue
        for name, value in list(vars(module).items()):
            original = value
            while inspect.isfunction(original) and vars(original).get('__parity_context_guard__') is True:
                original = original.__parity_original__
            if original is not value:
                setattr(module, name, original)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    try:
        yield
    finally:
        # Fixture monkeypatch finalizers can restore a saved call guard after
        # its call-scoped patcher was undone. Strip only our tagged wrappers.
        _remove_context_guards(item)


def _actual_ai_context_rejection(original, api, context, result):
    """Only the production AI service may return its typed context rejection."""
    if api != 'ai_backend.execute_ai' or context is not None:
        return False
    code = getattr(original, '__code__', None)
    production = Path(__file__).resolve().parents[1] / 'hooks' / 'ai_backend.py'
    if code is None or code.co_name != 'execute_ai' or Path(code.co_filename).resolve() != production:
        return False
    module = sys.modules.get(original.__module__)
    result_type = getattr(module, 'AIResult', None)
    return (result_type is not None and type(result) is result_type
            and result.status == 'unavailable' and result.error_code == 'context_required')


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _remove_context_guards(item)
    if 'selected_host_context' not in item.fixturenames:
        yield
        return
    from runtime_context import RuntimeContext, current_runtime_context
    selected = item.funcargs['selected_host_context']
    host = item.callspec.params['host']
    assert isinstance(selected, RuntimeContext), 'selected_host_context must be a real RuntimeContext'
    assert selected.host == host, 'selected RuntimeContext does not match invoking host'
    assert current_runtime_context() is selected, 'selected invoking context is not active'
    patcher = pytest.MonkeyPatch()
    replacements = {}
    scenario = item.funcargs.get('host_identity_scenario')
    for module_name, functions in CONTEXT_APIS.items():
        for spelling in (module_name, 'hooks.' + module_name):
            module = sys.modules.get(spelling)
            if module is None:
                continue
            for name in functions:
                original = getattr(module, name, None)
                if not callable(original):
                    continue
                api = module_name + '.' + name

                def wrap(original=original, api=api):
                    @functools.wraps(original)
                    def checked(*args, **kwargs):
                        context = args[0] if args else kwargs.get('context', kwargs.get('ctx'))
                        real = isinstance(context, RuntimeContext)
                        if real and context.host != host:
                            assert scenario is not None and scenario.permits(api, context), (
                                f'{api}: actual RuntimeContext does not match invoking host')
                        result = original(*args, **kwargs)
                        # Invalid-context rejection tests retain the service's
                        # exception. A successful call cannot use a fake actor.
                        assert real or _actual_ai_context_rejection(original, api, context, result), (
                            f'{api}: successful call requires a real RuntimeContext')
                        return result
                    checked.__parity_context_guard__ = True
                    checked.__parity_original__ = original
                    return checked

                replacement = wrap()
                replacements[id(original)] = (original, replacement)
                patcher.setattr(module, name, replacement)
    # Imported aliases in this test and its fixture modules retain the same
    # proof as module-qualified calls, including mocked service functions.
    modules = {item.module}
    root = Path(str(item.config.rootpath)).resolve()
    for module in list(sys.modules.values()):
        filename = getattr(module, '__file__', None)
        if filename and Path(filename).resolve().is_relative_to(root):
            modules.add(module)
    for definitions in item._fixtureinfo.name2fixturedefs.values():
        for definition in definitions:
            module = sys.modules.get(definition.func.__module__)
            if module is not None:
                modules.add(module)
    for module in modules:
        for name, value in list(vars(module).items()):
            replacement = replacements.get(id(value))
            if replacement is not None and value is replacement[0]:
                patcher.setattr(module, name, replacement[1])
    try:
        yield
    finally:
        patcher.undo()
