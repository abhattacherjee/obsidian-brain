"""Discover exact native/shared surfaces without running CLI handlers."""
import ast
import importlib
import json
from pathlib import Path
import sys


def strings(node):
    return sorted({item.value for item in ast.walk(node)
                   if isinstance(item, ast.Constant) and isinstance(item.value, str)})


def bind_strings(target, value, bindings):
    if isinstance(value, ast.IfExp):
        bind_strings(target, value.body, bindings)
        bind_strings(target, value.orelse, bindings)
    elif isinstance(target, ast.Name):
        bindings.setdefault(target.id, set()).update(strings(value))
    elif (isinstance(target, (ast.Tuple, ast.List))
          and isinstance(value, (ast.Tuple, ast.List))
          and len(target.elts) == len(value.elts)):
        for field, item in zip(target.elts, value.elts):
            bind_strings(field, item, bindings)


def qualify(node, aliases):
    parts = ast.unparse(node).split('.')
    return '.'.join([aliases.get(parts[0], parts[0]), *parts[1:]])


def source_reads(tree, constants, *, include_native=False):
    """Follow lexical bindings; payload rows and unrelated loops are not config/env."""
    environment, config, native_config, unresolved = set(), set(), set(), []
    tables = {}
    for declaration in tree.body:
        if isinstance(declaration, ast.Assign) and isinstance(declaration.value, ast.Dict):
            try:
                literal = ast.literal_eval(declaration.value)
            except (ValueError, TypeError):
                continue
            for target in declaration.targets:
                if isinstance(target, ast.Name):
                    tables[target.id] = literal

    def table_values(node):
        if isinstance(node, ast.Name) and node.id in tables:
            return [tables[node.id]]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get' and node.args):
            results = []
            for table in table_values(node.func.value):
                if not isinstance(table, dict):
                    continue
                key = node.args[0]
                if isinstance(key, ast.Constant):
                    results.append(table.get(key.value, ()))
                else:
                    results.extend(table.values())
            return results
        return []

    def origin(node, bindings):
        if isinstance(node, ast.IfExp):
            return origin(node.body, bindings) or origin(node.orelse, bindings)
        if isinstance(node, ast.BinOp) and 'obsidian-brain-config.json' in strings(node):
            return 'configuration-path'
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            base = origin(node.value, bindings)
            if isinstance(base, str):
                return base + '.' + node.attr
            if node.attr == 'config' and isinstance(node.value, ast.Name) and node.value.id in {'context', 'ctx'}:
                return 'selected-config'
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func).split('.')[-1]
            if name == '_configuration':
                return 'native-config'
            if name == '_path' and 'config_path' in strings(node):
                return 'configuration-path'
            if name == 'open' and node.args and origin(node.args[0], bindings) == 'configuration-path':
                return 'config-stream'
            if name in {'loads', 'load'} and node.args:
                argument = node.args[0]
                if name == 'load' and origin(argument, bindings) == 'config-stream':
                    return 'selected-config'
                if (name == 'loads' and isinstance(argument, ast.Call)
                        and isinstance(argument.func, ast.Attribute) and argument.func.attr == 'read_text'
                        and origin(argument.func.value, bindings) == 'configuration-path'):
                    return 'selected-config'
            if name in {'load_config', 'read_config', '_read_config'}:
                return 'selected-config'
            if name == 'dict' and node.args:
                return origin(node.args[0], bindings)
        return None

    def values(node, bindings):
        candidates = table_values(node)
        if candidates:
            return sorted({item for candidate in candidates if isinstance(candidate, (list, tuple))
                           for item in candidate if isinstance(item, str)})
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'items' and not node.args):
            return values(node.func.value, bindings)
        if isinstance(node, ast.DictComp):
            inner = dict(bindings)
            for generator in node.generators:
                if isinstance(generator.target, ast.Name):
                    inner[generator.target.id] = values(generator.iter, inner)
            return values(node.key, inner)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name):
            value = bindings.get(node.id)
            return value if isinstance(value, list) else []
        if isinstance(node, ast.Attribute):
            return constants.get(ast.unparse(node), [])
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return sorted(set(value for item in node.elts for value in values(item, bindings)))
        if isinstance(node, ast.IfExp):
            return sorted(set(values(node.body, bindings) + values(node.orelse, bindings)))
        return []

    def bind(target, value, bindings):
        if isinstance(target, ast.Name):
            bindings[target.id] = origin(value, bindings) or values(value, bindings) or None
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, ast.IfExp):
            left, right = dict(bindings), dict(bindings)
            bind(target, value.body, left)
            bind(target, value.orelse, right)
            for item in target.elts:
                if isinstance(item, ast.Name):
                    a, b = left.get(item.id), right.get(item.id)
                    bindings[item.id] = sorted(set((a if isinstance(a, list) else []) + (b if isinstance(b, list) else [])))
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            for left, right in zip(target.elts, value.elts):
                bind(left, right, bindings)

    def read(node, receiver, key, bindings):
        provenance = origin(receiver, bindings)
        if provenance == 'os.environ':
            keys = values(key, bindings)
            if keys:
                environment.update(keys)
            else:
                unresolved.append({'line': node.lineno, 'expression': ast.unparse(node)})
        elif isinstance(provenance, str) and provenance in {'selected-config', 'native-config'} and isinstance(key, ast.Constant) and isinstance(key.value, str):
            (config if provenance == 'selected-config' else native_config).add(key.value)

    def visit(node, bindings):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inner = dict(bindings)
            for parameter in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                # These are the repository's explicit configuration parameters.
                inner[parameter.arg] = 'selected-config' if parameter.arg in {'config', 'cfg'} else None
            if isinstance(node, ast.Lambda):
                visit(node.body, inner)
            else:
                for child in node.body:
                    visit(child, inner)
            return
        if isinstance(node, ast.ClassDef):
            inner = dict(bindings)
            for child in node.body:
                visit(child, inner)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for field in node.names:
                name = field.asname or field.name
                qualified = field.name if isinstance(node, ast.Import) else (node.module or '') + '.' + field.name
                bindings[name] = qualified
                if name in constants:
                    bindings[name] = constants[name]
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            inner = dict(bindings)
            for item in node.items:
                visit(item.context_expr, inner)
                if item.optional_vars is not None:
                    bind(item.optional_vars, item.context_expr, inner)
            for child in node.body:
                visit(child, inner)
            bindings.update(inner)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is not None:
                visit(node.value, bindings)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    bind(target, node.value, bindings)
                    if isinstance(target, ast.Name) and not bindings[target.id] and target.id in constants:
                        bindings[target.id] = constants[target.id]
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            visit(node.iter, bindings)
            inner = dict(bindings)
            if isinstance(node.target, ast.Name):
                inner[node.target.id] = values(node.iter, bindings)
            elif (isinstance(node.target, (ast.Tuple, ast.List))
                    and isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute)
                    and node.iter.func.attr == 'items'):
                for index, target in enumerate(node.target.elts):
                    if isinstance(target, ast.Name):
                        inner[target.id] = values(node.iter, bindings) if index == 0 else None
            for child in [*node.body, *node.orelse]:
                visit(child, inner)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            inner = dict(bindings)
            for generator in node.generators:
                visit(generator.iter, inner)
                if isinstance(generator.target, ast.Name):
                    inner[generator.target.id] = values(generator.iter, inner)
                for condition in generator.ifs:
                    visit(condition, inner)
            for child in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
                visit(child, inner)
            return
        if isinstance(node, ast.Call) and node.args:
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'get', 'pop', 'setdefault'}:
                read(node, node.func.value, node.args[0], bindings)
            elif origin(node.func, bindings) == 'os.getenv':
                read(node, ast.Attribute(value=ast.Name(id='os'), attr='environ'), node.args[0], {'os': 'os', **bindings})
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            read(node, node.value, node.slice, bindings)
        for child in ast.iter_child_nodes(node):
            visit(child, bindings)

    bindings = dict(constants)
    for node in tree.body:
        visit(node, bindings)
    return (environment, config, native_config, unresolved) if include_native else (environment, config, unresolved)


def payload_keys(node, receivers=('payload',)):
    keys = set()
    for child in ast.walk(node):
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and ast.unparse(child.func.value) in receivers
                and child.func.attr == 'get' and child.args
                and isinstance(child.args[0], ast.Constant)):
            keys.add(child.args[0].value)
        if (isinstance(child, ast.Subscript) and ast.unparse(child.value) in receivers
                and isinstance(child.slice, ast.Constant)):
            keys.add(child.slice.value)
    return keys


def event_truth(node, event):
    if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
            and node.left.id == 'event' and len(node.ops) == 1):
        other = node.comparators[0]
        if isinstance(other, ast.Constant):
            if isinstance(node.ops[0], ast.Eq):
                return {event == other.value}
            if isinstance(node.ops[0], ast.NotEq):
                return {event != other.value}
        if isinstance(other, (ast.List, ast.Tuple, ast.Set)):
            if isinstance(node.ops[0], ast.In):
                return {event in strings(other)}
            if isinstance(node.ops[0], ast.NotIn):
                return {event not in strings(other)}
    if isinstance(node, ast.BoolOp):
        values = {True} if isinstance(node.op, ast.And) else {False}
        for child in node.values:
            values = {left and right if isinstance(node.op, ast.And) else left or right
                      for left in values for right in event_truth(child, event)}
        return values
    return {False, True}


def condition_keys(node, event):
    if not isinstance(node, ast.BoolOp):
        return payload_keys(node)
    keys = set()
    for child in node.values:
        keys.update(condition_keys(child, event))
        truth = event_truth(child, event)
        if isinstance(node.op, ast.And) and truth == {False}:
            break
        if isinstance(node.op, ast.Or) and truth == {True}:
            break
    return keys


def fields_for_event(node, event):
    if isinstance(node, ast.If):
        fields = condition_keys(node.test, event)
        truth = event_truth(node.test, event)
        branches = (node.body if True in truth else []) + (node.orelse if False in truth else [])
        for child in branches:
            fields.update(fields_for_event(child, event))
        return fields
    fields = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(child, ast.If):
            fields.update(fields_for_event(child, event))
        else:
            fields.update(fields_for_event(child, event))
    if isinstance(node, (ast.Call, ast.Subscript)):
        fields.update(payload_keys(node))
    return fields


def operation_inventory(path):
    tree = ast.parse(path.read_text())
    declaration = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'SKILLS'
                               for target in node.targets))
    skills = ast.literal_eval(declaration)
    operations = {name: set() for name in skills}

    def value(node, bindings):
        return bindings[node.id] if isinstance(node, ast.Name) else ast.literal_eval(node)

    def visit(node, bindings):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        mentions = any(isinstance(child, ast.Name) and child.id == 'OPERATIONS'
                       for child in ast.walk(node))
        if not mentions:
            return
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if ast.unparse(node.iter) == 'OPERATIONS.values()':
                for child in node.body:
                    if (isinstance(child, ast.Assign) and len(child.targets) == 1
                            and isinstance(child.targets[0], ast.Subscript)
                            and isinstance(child.targets[0].value, ast.Name)
                            and child.targets[0].value.id == node.target.id):
                        keys = [value(child.targets[0].slice, bindings)]
                    elif (isinstance(child, ast.Expr) and isinstance(child.value, ast.Call)
                          and ast.unparse(child.value.func) == node.target.id + '.update'
                          and len(child.value.args) == 1 and isinstance(child.value.args[0], ast.Dict)):
                        keys = [value(key, bindings) for key in child.value.args[0].keys]
                    else:
                        raise ValueError('Unresolved registry values update')
                    for name in skills:
                        operations[name].update(keys)
                return
            members = skills if isinstance(node.iter, ast.Name) and node.iter.id == 'SKILLS' else value(node.iter, bindings)
            for member in members:
                for child in node.body:
                    visit(child, {**bindings, node.target.id: member})
            if node.orelse:
                raise ValueError('Unsupported registry loop else')
            return
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == 'OPERATIONS':
                if not isinstance(node.value, ast.DictComp):
                    raise ValueError('Unsupported registry initialization')
                keys = [value(key, bindings) for key in node.value.value.keys]
                for name in skills:
                    operations[name].update(keys)
                return
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Subscript)
                    and isinstance(target.value.value, ast.Name)
                    and target.value.value.id == 'OPERATIONS'):
                operations[value(target.value.slice, bindings)].add(value(target.slice, bindings))
                return
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (isinstance(call.func, ast.Attribute) and call.func.attr == 'update'
                    and isinstance(call.func.value, ast.Subscript)
                    and isinstance(call.func.value.value, ast.Name)
                    and call.func.value.value.id == 'OPERATIONS'
                    and len(call.args) == 1 and isinstance(call.args[0], ast.Dict)):
                operations[value(call.func.value.slice, bindings)].update(
                    value(key, bindings) for key in call.args[0].keys)
                return
        raise ValueError(f'Unresolved registry declaration at {path}:{node.lineno}')

    for node in tree.body:
        visit(node, {})
    return {name: sorted(keys) for name, keys in sorted(operations.items())}


def discover(root):
    operations = operation_inventory(root / 'hooks/skill_procedures.py')
    wiki_tree = ast.parse((root / 'hooks/wiki.py').read_text())
    commands = next(node.value for node in wiki_tree.body if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name) and node.target.id == '_COMMANDS')
    doctor_checks = []
    for path in sorted((root / 'scripts/vault_doctor_checks').glob('*.py')):
        tree = ast.parse(path.read_text())
        names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        if {'scan', 'apply'}.issubset(names):
            declaration = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                               and any(isinstance(target, ast.Name) and target.id == 'NAME'
                                       for target in node.targets))
            doctor_checks.append(ast.literal_eval(declaration))
    skills = sorted(path.parent.name for path in (root / 'skills').glob('*/SKILL.md'))
    hooks = json.loads((root / 'hooks/hooks.json').read_text())['hooks']
    tree = ast.parse((root / 'hooks/native_lifecycle.py').read_text())
    dispatch = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'dispatch')
    events = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == 'EVENTS'
                          for target in node.targets))
    runtime = ast.parse((root / 'hooks/runtime_context.py').read_text())
    resolver = next(node for node in runtime.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'resolve_runtime_context')
    identity_fields = payload_keys(resolver)
    retro = ast.parse((root / 'hooks/obsidian_retro_gate.py').read_text())
    decision = next(node for node in retro.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'native_decision')
    fields = {event: sorted(fields_for_event(dispatch, event) | identity_fields
                            | (payload_keys(decision, ('data',)) if event == 'stop' else set()))
              for event in strings(events)}
    environment, config, native_config, unresolved = {}, {}, {}, []
    constants_by_path = {}
    trees = {}
    for path in sorted([*(root / 'hooks').rglob('*.py'), *(root / 'scripts').rglob('*.py')]):
        if '__pycache__' in path.parts:
            continue
        tree = ast.parse(path.read_text())
        trees[path] = tree
        constants = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(node.value, (ast.List, ast.Tuple, ast.Set, ast.IfExp)):
                        bind_strings(target, node.value, constants)
        constants_by_path[path] = {name: sorted(values) for name, values in constants.items()}
    # Extracted host adapters may expose a tuple through a literal-return
    # helper. Read that exact body; never execute a helper to discover keys.
    for path, tree in trees.items():
        imported_functions = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                relative = Path(*node.module.split('.')).with_suffix('.py')
                source = next((candidate for candidate in
                               (root / 'hooks' / relative, root / 'scripts' / relative)
                               if candidate in trees), None)
                if source:
                    for field in node.names:
                        imported_functions[field.asname or field.name] = (source, field.name)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and not node.value.args
                    and not node.value.keywords and node.value.func.id in imported_functions):
                continue
            source, name = imported_functions[node.value.func.id]
            function = next((item for item in trees[source].body
                             if isinstance(item, ast.FunctionDef) and item.name == name), None)
            if function is None:
                continue
            body = [item for item in function.body if not (isinstance(item, ast.Expr)
                    and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str))]
            if (len(body) == 1 and isinstance(body[0], ast.Return)
                    and isinstance(body[0].value, (ast.Tuple, ast.List, ast.Set))):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants_by_path[path][target.id] = strings(body[0].value)
    # Resolve imported tuple constants in their source module rather than
    # borrowing a same-named variable from an unrelated file.
    for path, tree in trees.items():
        constants = constants_by_path[path]
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imports = ([(node.module or '', name.name, name.asname or name.name)
                        for name in node.names] if isinstance(node, ast.ImportFrom) else
                       [(name.name, None, name.asname or name.name) for name in node.names])
            for module_name, field, alias in imports:
                if not module_name:
                    continue
                relative = Path(*module_name.split('.'))
                candidates = [root / 'hooks' / relative.with_suffix('.py'),
                              root / 'scripts' / relative.with_suffix('.py'),
                              root / relative.with_suffix('.py')]
                if isinstance(node, ast.ImportFrom) and node.level:
                    directory = path.parent
                    for _ in range(node.level - 1):
                        directory = directory.parent
                    candidates.insert(0, directory / relative.with_suffix('.py'))
                imported = next((candidate for candidate in candidates
                                 if candidate in constants_by_path), None)
                if imported is None:
                    continue
                for name, values in constants_by_path[imported].items():
                    if field == name:
                        constants[alias] = values
                    elif field is None:
                        constants[alias + '.' + name] = values
    for path, tree in trees.items():
        relative = str(path.relative_to(root))
        env_keys, config_keys, native_keys, unknown = source_reads(tree, constants_by_path[path], include_native=True)
        unresolved.extend({'path': relative, **item} for item in unknown)
        if env_keys:
            environment[relative] = sorted(env_keys)
        if config_keys:
            config[relative] = sorted(config_keys)
        if native_keys:
            native_config[relative] = sorted(native_keys)
    return {
        'schema': 1, 'skills': skills,
        'skill_operations': operations,
        'legacy_hook_events': sorted(hooks), 'native_payload_fields_by_handler': fields,
        'runtime_override_fields': sorted(payload_keys(resolver, ('overrides',))),
        'wiki_commands': sorted([*(ast.literal_eval(key) for key in commands.keys), 'rule']),
        'doctor_checks': sorted(doctor_checks), 'config_reads': config,
        'native_config_reads': native_config,
        'environment_reads': environment, 'unresolved_environment_reads': unresolved,
    }


def validate(expected, observed):
    if expected != observed:
        changed = sorted(key for key in set(expected) | set(observed) if expected.get(key) != observed.get(key))
        raise ValueError('Capability inventory changed: ' + ', '.join(changed))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(discover(args.root.resolve()), indent=2) + '\n')
