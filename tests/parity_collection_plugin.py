"""Draft collection guard: declarations cover behavior, not fixture file origin."""
import ast
import json
import inspect
import hashlib
import functools
import sys
import types
from pathlib import Path
import pytest

HOSTS = frozenset({'claude', 'codex'})
MODULES = {}
NODE_ANALYSIS = {}
IMPORT_TARGETS = {}
SOURCE_FILES = {}
RESOLVED_PATHS = {}


def _resolved(path):
    if path not in RESOLVED_PATHS:
        RESOLVED_PATHS[path] = path.resolve()
    return RESOLVED_PATHS[path]


def _node_analysis(node):
    """Cache syntax only; actor and parameter bindings remain per test."""
    if node not in NODE_ANALYSIS:
        nodes = tuple(ast.walk(node))
        parents = {child: parent for parent in nodes
                   for child in ast.iter_child_nodes(parent)}
        NODE_ANALYSIS[node] = nodes, parents
    return NODE_ANALYSIS[node]


def _static_string(node, bindings=None):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return (bindings or {}).get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left, bindings)
        right = _static_string(node.right, bindings)
        if left is not None and right is not None:
            return left + right
    return None


def _reference(node, imports=None, bindings=None):
    imports = imports or {}
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _reference(node.value, imports, bindings)
        reference = owner + '.' + node.attr if owner else None
        return imports.get(reference, reference)
    if isinstance(node, ast.Lambda):
        body = node.body
        if isinstance(body, (ast.Tuple, ast.List)):
            for element in body.elts:
                reference = _reference(element.func if isinstance(element, ast.Call)
                                       else element, imports, bindings)
                if reference and reference.split('.')[0] in CONTEXT_APIS:
                    return reference
        return _reference(body.func if isinstance(body, ast.Call) else body, imports, bindings)
    if isinstance(node, ast.NamedExpr):
        return _reference(node.value, imports, bindings)
    if isinstance(node, ast.Call):
        called = _reference(node.func, imports, bindings)
        if called in {'staticmethod', 'classmethod'} and node.args:
            return _reference(node.args[0], imports, bindings)
        if called == 'globals':
            return '__module_globals__'
        if called in {'operator.attrgetter', 'operator.methodcaller'} and node.args:
            field = _static_string(node.args[0], bindings)
            return '__attrgetter__.' + field if field else None
        if called and called.startswith('__attrgetter__.') and node.args:
            owner = _reference(node.args[0], imports, bindings)
            return owner + '.' + called.split('.', 1)[1] if owner else None
        if called in {'skill_procedures.OPERATIONS.get',
                      'hooks.skill_procedures.OPERATIONS.get'}:
            return 'skill_procedures.run_operation'
        if called in {'functools.partial', 'partial'} and node.args:
            return _reference(node.args[0], imports, bindings)
        if called in {'importlib.import_module', 'import_module', '__import__'} and node.args:
            return _static_string(node.args[0], bindings)
        if called == 'getattr' and len(node.args) >= 2:
            owner = _reference(node.args[0], imports, bindings)
            method = _static_string(node.args[1], bindings)
            if owner:
                if method is not None:
                    return owner + '.' + method
                if owner.removeprefix('hooks.') in CONTEXT_APIS:
                    return owner + '.__dynamic_shared__'
        if called == 'vars' and node.args:
            return _reference(node.args[0], imports, bindings)
    if isinstance(node, ast.Subscript):
        owner = _reference(node.value, imports, bindings)
        field = _static_string(node.slice, bindings)
        if isinstance(node.slice, ast.Constant) and type(node.slice.value) is int:
            field = str(node.slice.value)
        if owner and field is not None:
            if owner == '__module_globals__':
                return imports.get(field)
            if owner == 'sys.modules' and field.removeprefix('hooks.') in CONTEXT_APIS:
                return field
            owner = owner.removesuffix('.__dict__')
            if (owner.removeprefix('hooks.').startswith('skill_procedures.OPERATIONS')
                    or (owner == 'skill_procedures.run_operation'
                        and isinstance(node.value, (ast.Subscript, ast.Call)))):
                return 'skill_procedures.run_operation'
            resolved = owner + '.' + field
            return imports.get(resolved, resolved)
    return None


def _extend_aliases(imports, nodes, bindings=None):
    assignments = [node for node in nodes if isinstance(node, ast.Assign)]
    for node in nodes:
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append(ast.Assign(targets=[node.target], value=node.value))
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.Assign):
                    assignments.append(ast.Assign(
                        targets=[ast.Name(id=node.name + '.' + field.id)
                                 for field in member.targets if isinstance(field, ast.Name)],
                        value=member.value))
        value = node.value if isinstance(node, ast.Expr) else node
        if isinstance(value, ast.NamedExpr):
            assignments.append(ast.Assign(targets=[value.target], value=value.value))
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.List, ast.Tuple)):
            for value in node.iter.elts:
                assignments.append(ast.Assign(targets=[node.target], value=value))
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Tuple, ast.List)):
            for target in node.targets:
                if isinstance(target, (ast.Tuple, ast.List)):
                    assignments.extend(ast.Assign(targets=[field], value=value)
                                       for field, value in zip(target.elts, node.value.elts))
    # A few passes resolve ordinary alias chains without evaluating source.
    for _ in range(3):
        changed = False
        for assignment in assignments:
            reference = _reference(assignment.value, imports, bindings)
            if isinstance(assignment.value, ast.Dict):
                for key, value in zip(assignment.value.keys, assignment.value.values):
                    field = _static_string(key, bindings)
                    target_reference = _reference(value, imports, bindings)
                    if field is not None and target_reference:
                        for target in assignment.targets:
                            if isinstance(target, ast.Name):
                                imports[target.id + '.' + field] = target_reference
            if isinstance(assignment.value, (ast.List, ast.Tuple)):
                for index, value in enumerate(assignment.value.elts):
                    reference_item = _reference(value, imports, bindings)
                    if reference_item:
                        for target in assignment.targets:
                            if isinstance(target, ast.Name):
                                imports[target.id + '.' + str(index)] = reference_item
            if reference and ('.' in reference or reference.removeprefix('hooks.') in CONTEXT_APIS):
                for target in assignment.targets:
                    if isinstance(target, ast.Name) and imports.get(target.id) != reference:
                        imports[target.id] = reference
                        changed = True
        if not changed:
            break



def pytest_addoption(parser):
    parser.addoption('--parity-matrix', required=False)
    parser.addoption('--parity-audit-report', required=False)


def pytest_configure(config):
    SOURCE_FILES.clear()
    RESOLVED_PATHS.clear()
    MODULES.clear()
    NODE_ANALYSIS.clear()
    IMPORT_TARGETS.clear()
    config.addinivalue_line('markers', 'host_only(host, reason, capability): declared native-format exception')


def _calls(node, imports=None, bindings=None):
    for call in _node_analysis(node)[0]:
        if isinstance(call, ast.Call):
            resolved = _reference(call.func, imports, bindings)
            if resolved in {'map', 'filter', 'functools.partial', 'partial'} and call.args:
                callable_reference = _reference(call.args[0], imports, bindings)
                if callable_reference:
                    yield callable_reference.removeprefix('hooks.')
                    yield callable_reference.rsplit('.', 1)[-1]
            if resolved:
                if resolved.startswith('hooks.'):
                    resolved = resolved[len('hooks.'):]
                if resolved.endswith('.__dynamic_shared__'):
                    owner = resolved.rsplit('.', 1)[0]
                    yield from (owner + '.' + function for function in CONTEXT_APIS.get(owner, ()))
                yield resolved
                yield resolved.rsplit('.', 1)[-1]
            if isinstance(call.func, ast.Name):
                yield call.func.id
            elif isinstance(call.func, ast.Attribute):
                yield call.func.attr


def _scope(item, capabilities, launchers=(), errors=None):
    def reject(message):
        if errors is None:
            raise pytest.UsageError(message)
        errors.append(message)
    root = _resolved(Path(str(item.config.rootpath)))
    search = [Path(str(item.path)).parent, root, root / 'hooks', root / 'scripts',
              root / 'tests']

    def module(path):
        path = _resolved(path)
        if path not in SOURCE_FILES:
            info = path.stat()
            raw = path.read_bytes()
            SOURCE_FILES[path] = (info.st_mtime_ns, info.st_size,
                                  hashlib.sha256(raw).hexdigest(), raw)
        stamp, size, _, raw = SOURCE_FILES[path]
        key = (path, stamp, size)
        if key not in MODULES:
            tree = ast.parse(raw.decode('utf-8'))
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
            def static_name(node):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return node.value
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                    left, right = static_name(node.left), static_name(node.right)
                    return left + right if left is not None and right is not None else None
                return None
            for assignment in tree.body:
                if (isinstance(assignment, ast.Assign)
                        and isinstance(assignment.value, ast.Call)
                        and isinstance(assignment.value.func, ast.Name)
                        and assignment.value.func.id == 'getattr'
                        and len(assignment.value.args) >= 2
                        and isinstance(assignment.value.args[0], ast.Name)):
                    owner = imports.get(assignment.value.args[0].id)
                    if owner and owner.startswith('hooks.'):
                        owner = owner[len('hooks.'):]
                    method = static_name(assignment.value.args[1])
                    if owner and (method or owner in CONTEXT_APIS):
                        method = method or '__dynamic_shared__'
                        for target in assignment.targets:
                            if isinstance(target, ast.Name):
                                imports[target.id] = owner + '.' + method
            def collect_definitions(nodes, class_prefix=''):
                for node in nodes:
                    if isinstance(node, ast.ClassDef):
                        collect_definitions(node.body, class_prefix + node.name + '.')
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        definitions.setdefault(class_prefix + node.name, []).append(node)
                        collect_definitions(node.body)
            collect_definitions(tree.body)
            constants = {}
            for assignment in tree.body:
                if isinstance(assignment, ast.Assign):
                    value = _static_string(assignment.value, constants)
                    for target in assignment.targets:
                        if isinstance(target, ast.Name) and value is not None:
                            constants[target.id] = value
            _extend_aliases(imports, tree.body, constants)
            MODULES[key] = imports, definitions, constants
        return MODULES[key]

    def find_imported_target(call, owner):
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

    def imported_target(call, owner):
        # This resolves source files only. Calls, actor scope and string parameter
        # bindings are still evaluated separately for every collected test.
        key = (call, _resolved(owner.parent), tuple(search))
        if key not in IMPORT_TARGETS:
            IMPORT_TARGETS[key] = find_imported_target(call, owner)
        return IMPORT_TARGETS[key]

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
            if filename and any(_resolved(Path(filename)).is_relative_to(_resolved(directory))
                                for directory in search):
                pending.append((Path(filename), definition.func.__name__, initial_bindings))
    seen, calls, launcher_scope = set(), set(), set()
    while pending:
        path, name, bindings = pending.pop()
        identity = (_resolved(path), name, tuple(sorted(bindings.items())))
        if identity in seen:
            continue
        seen.add(identity)
        imports, definitions, constants = module(path)
        for definition in definitions.get(name, []):
            local_bindings = dict(constants, **bindings)
            imports = dict(imports)
            for imported in _node_analysis(definition)[0]:
                if isinstance(imported, ast.Import):
                    for alias in imported.names:
                        imports[alias.asname or alias.name.split('.')[0]] = (
                            alias.name if alias.asname else alias.name.split('.')[0])
                elif isinstance(imported, ast.ImportFrom):
                    prefix = '.' * imported.level + (imported.module or '')
                    for alias in imported.names:
                        imports[alias.asname or alias.name] = prefix + '.' + alias.name
            for assignment in _node_analysis(definition)[0]:
                if isinstance(assignment, ast.Assign):
                    value = (assignment.value.value if isinstance(assignment.value, ast.Constant)
                             and isinstance(assignment.value.value, str) else
                             local_bindings.get(assignment.value.id)
                             if isinstance(assignment.value, ast.Name) else None)
                    for target in assignment.targets:
                        if isinstance(target, ast.Name) and value is not None:
                            local_bindings[target.id] = value
            _extend_aliases(imports, _node_analysis(definition)[0], local_bindings)
            found = set(_calls(definition, imports, local_bindings))
            calls.update(found)
            # getattr can hide the function spelling, but not the imported
            # shared service receiving the call. Its runtime guard still
            # checks the real actor when the test runs.
            parents = _node_analysis(definition)[1]
            for access in _node_analysis(definition)[0]:
                parent = parents.get(access)
                if isinstance(parent, ast.Call) and parent.args and parent.args[0] is access:
                    spelling = ast.unparse(parent.func)
                    first, *rest = spelling.split('.')
                    resolved = '.'.join([imports.get(first, first), *rest])
                    if resolved == 'inspect.getsource':
                        # Reading a function's source does not invoke its service.
                        # Other calls in this same closure still require actors.
                        continue
                reference = _reference(access, imports, local_bindings)
                if reference:
                    reference = reference.removeprefix('hooks.')
                    if any(reference == service + '.' + function
                           for service, functions in CONTEXT_APIS.items()
                           for function in functions):
                        calls.add(reference)
                        calls.add(reference.rsplit('.', 1)[-1])
                if (isinstance(access, ast.Call) and isinstance(access.func, ast.Name)
                        and access.func.id == 'getattr' and access.args
                        and isinstance(access.args[0], ast.Name)):
                    owner = imports.get(access.args[0].id, access.args[0].id)
                    for service, functions in CONTEXT_APIS.items():
                        if owner in {service, 'hooks.' + service}:
                            calls.update(service + '.' + function for function in functions)
            for invocation in _node_analysis(definition)[0]:
                if not isinstance(invocation, ast.Call):
                    continue
                called = (_reference(invocation.func, imports, local_bindings)
                          or ast.unparse(invocation.func))
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
                    _, target_definitions, _ = module(target_path)
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
                source = str(_resolved(path).relative_to(root))
                contract = next((entry for entry in launchers
                                 if entry.get('source') == source
                                 and entry.get('expression') == expression), None)
                digest = SOURCE_FILES[_resolved(path)][2]
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
    # Large automatic IDs can stall CI logs and overflow Linux subprocess env.
    for item in items:
        if len(item.nodeid.encode('utf-8')) > 4096:
            # Pytest prints collected items even when this hook raises.
            items.clear()
            raise pytest.UsageError(
                f'{item.nodeid[:160]}: test ID exceeds 4096 bytes; '
                'give large parameters explicit short ids.')
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
    # Syntax and byte hashes may be reused during collection. A changed source
    # or retargeted symlink invalidates the entire collected proof.
    for original, resolved in RESOLVED_PATHS.items():
        if resolved in SOURCE_FILES and original.resolve() != resolved:
            errors.append(f'{original}: source target changed during collection')
    for source, (stamp, size, digest, _) in SOURCE_FILES.items():
        info = source.stat()
        if ((info.st_mtime_ns, info.st_size) != (stamp, size)
                or hashlib.sha256(source.read_bytes()).hexdigest() != digest):
            errors.append(f'{source}: source bytes changed during collection')
    report = config.getoption('--parity-audit-report')
    if report:
        Path(report).write_text(json.dumps({'collected': len(items), 'errors': errors}, indent=2) + '\n')
    if errors:
        raise pytest.UsageError('\n'.join(errors))


IDENTITY_ORIGINALS = {}


CONTEXT_APIS = {
    'capture': ('capture_checkpoint', 'recover_pending', 'recover_registered'),
    'note_transactions': ('apply_mutations', 'delete_note', 'move_note', 'ownership_lock'),
    'ai_backend': ('execute_ai',),
    'skill_procedures': ('run_operation',),
    'session_auxiliary_state': ('directory', 'cache_get', 'cache_update', 'first_seen_date'),
}



def _rewrite_guard_aliases(modules, transform, patcher=None):
    """Replace stored callable aliases without invoking user descriptors."""
    visited = set()
    owners = {module.__name__ for module in modules}
    list_undo = []

    def dictionary_set(mapping, key, value):
        if patcher is None:
            mapping[key] = value
        else:
            patcher.setitem(mapping, key, value)

    def attribute_set(owner, key, value):
        if patcher is None:
            setattr(owner, key, value)
        else:
            patcher.setattr(owner, key, value)

    def visit(value):
        replacement = transform(value)
        if replacement is not value:
            return replacement
        if id(value) in visited:
            return value
        visited.add(id(value))
        if type(value) is dict:
            for key, member in list(value.items()):
                replacement = visit(member)
                if replacement is not member:
                    dictionary_set(value, key, replacement)
        elif type(value) is list:
            for index, member in enumerate(list(value)):
                replacement = visit(member)
                if replacement is not member:
                    value[index] = replacement
                    if patcher is not None:
                        list_undo.append((value, index, member, replacement))
        elif inspect.isclass(value) and value.__module__ in owners:
            for base in value.__bases__:
                visit(base)
            for name, member in list(vars(value).items()):
                if name.startswith('__'):
                    continue
                if type(member) in {staticmethod, classmethod}:
                    replacement = transform(member.__func__)
                    if replacement is not member.__func__:
                        attribute_set(value, name, type(member)(replacement))
                else:
                    replacement = visit(member)
                    if replacement is not member:
                        attribute_set(value, name, replacement)
        return value

    for module in modules:
        if module.__name__ == __name__:
            continue
        for name, value in list(vars(module).items()):
            if name.startswith('__'):
                continue
            replacement = visit(value)
            if replacement is not value:
                attribute_set(module, name, replacement)
    return list_undo


def _remove_context_guards(item):
    root = Path(str(item.config.rootpath)).resolve()
    api_modules = {name for api in CONTEXT_APIS for name in (api, 'hooks.' + api)}
    for spelling, module in list(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if spelling not in api_modules and (not filename or not Path(filename).resolve().is_relative_to(root)):
            continue
        def original(value):
            while inspect.isfunction(value) and vars(value).get('__parity_context_guard__') is True:
                value = value.__parity_original__
            return value
        _rewrite_guard_aliases((module,), original)


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
    active = IDENTITY_ORIGINALS.get(id(original))
    if active is not None and active[0] is original:
        original = active[1]
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
    from runtime_context import RuntimeContext, current_runtime_context
    selected = item.funcargs.get('selected_host_context')
    host = item.callspec.params['host'] if selected is not None else None
    if selected is not None:
        assert isinstance(selected, RuntimeContext), 'selected_host_context must be a real RuntimeContext'
        assert selected.host == host, 'selected RuntimeContext does not match invoking host'
        assert current_runtime_context() is selected, 'selected invoking context is not active'
    patcher = pytest.MonkeyPatch()
    replacements = {}
    identity_undo = []
    identities = set()
    def restore_identities():
        for target, code, key, had_key, old_value in reversed(identity_undo):
            target.__code__ = code
            IDENTITY_ORIGINALS.pop(id(target), None)
            if had_key:
                target.__globals__[key] = old_value
            else:
                target.__globals__.pop(key, None)
        identity_undo.clear()
    # Also covers a setup failure before the hook reaches its yield.
    item.addfinalizer(restore_identities)
    scenario = item.funcargs.get('host_identity_scenario')
    def guard_module(module_name, module):
        functions = CONTEXT_APIS[module_name]
        for name in functions:
            original = getattr(module, name, None)
            if not callable(original):
                continue
            api = module_name + '.' + name
            if id(original) in identities:
                continue
            identities.add(id(original))
            target = original
            if inspect.isfunction(target):
                original = types.FunctionType(target.__code__, target.__globals__,
                                              target.__name__, target.__defaults__, target.__closure__)
                original.__kwdefaults__ = target.__kwdefaults__
                original.__module__ = target.__module__

            def wrap(original=original, api=api):
                @functools.wraps(original)
                def checked(*args, **kwargs):
                    assert selected is not None, (
                        f'{api}: selected_host_context fixture is required for behavior')
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
            if inspect.isfunction(target):
                # Change the function itself: every stored reference, even
                # in a closure or opaque holder, keeps the same guard.
                key = '__parity_dispatch_' + str(id(target))
                namespace = {}
                exec('def dispatch(*args, **kwargs):\n    return ' + key + '(*args, **kwargs)\n', namespace)
                code = namespace['dispatch'].__code__.replace(co_freevars=target.__code__.co_freevars)
                had_key = key in target.__globals__
                old_value = target.__globals__.get(key)
                identity_undo.append((target, target.__code__, key, had_key, old_value))
                IDENTITY_ORIGINALS[id(target)] = (target, original)
                target.__globals__[key] = replacement
                target.__code__ = code
            else:
                replacements[id(target)] = (target, replacement)
                patcher.setattr(module, name, replacement)
    for module_name in CONTEXT_APIS:
        for spelling in (module_name, 'hooks.' + module_name):
            module = sys.modules.get(spelling)
            if module is not None:
                guard_module(module_name, module)

    class GuardLoader:
        def __init__(self, loader, module_name):
            self.loader = loader
            self.module_name = module_name
        def create_module(self, spec):
            create = getattr(self.loader, 'create_module', None)
            return create(spec) if create is not None else None
        def exec_module(self, module):
            # A failed import remains a failed import. Guard only completed
            # exports, before importlib returns them to the test body.
            self.loader.exec_module(module)
            guard_module(self.module_name, module)
        def __getattr__(self, name):
            return getattr(self.loader, name)

    class GuardFinder:
        def find_spec(self, fullname, path=None, target=None):
            module_name = fullname[6:] if fullname.startswith('hooks.') else fullname
            if fullname not in {module_name, 'hooks.' + module_name} or module_name not in CONTEXT_APIS:
                return None
            for finder in tuple(sys.meta_path):
                if finder is self:
                    continue
                find = getattr(finder, 'find_spec', None)
                if find is None:
                    continue
                spec = find(fullname, path, target)
                if spec is not None:
                    if spec.loader is None or not hasattr(spec.loader, 'exec_module'):
                        raise ImportError('Parity API requires an executable module loader: ' + fullname)
                    spec.loader = GuardLoader(spec.loader, module_name)
                    return spec
            return None

    finder = GuardFinder()
    sys.meta_path.insert(0, finder)
    def remove_finder():
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
    item.addfinalizer(remove_finder)
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
    def guarded(value):
        replacement = replacements.get(id(value))
        return replacement[1] if replacement is not None and value is replacement[0] else value
    list_undo = _rewrite_guard_aliases(modules, guarded, patcher)
    try:
        yield
    finally:
        for container, index, original, replacement in reversed(list_undo):
            if index < len(container) and container[index] is replacement:
                container[index] = original
        remove_finder()
        patcher.undo()
        restore_identities()
