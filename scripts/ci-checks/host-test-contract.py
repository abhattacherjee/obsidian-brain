"""Inventory helper calls and literal launch targets; dynamic targets need review."""
import argparse
import ast
import json
import hashlib
import sys
from pathlib import Path

PROCESS_LAUNCHERS = {'subprocess.run', 'subprocess.Popen', 'subprocess.call',
                     'subprocess.check_output', 'subprocess.check_call', 'os.system'}
LAUNCHERS = {'run_path', 'run_module', 'spec_from_file_location'}


def text(node):
    return ast.unparse(node) if node else ''


def imports(tree):
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update({name.asname or name.name: name.name for name in node.names})
        elif isinstance(node, ast.ImportFrom):
            result.update({name.asname or name.name: (node.module or '') + '.' + name.name for name in node.names})
    return result


def assigned(tree):
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    result.setdefault(target.id, []).append(node.value)
    return result


def strings(node, assignments, seen=None):
    seen = set() if seen is None else seen
    values = set()
    for value in ast.walk(node):
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.add(value.value)
        elif isinstance(value, ast.Name) and value.id not in seen:
            seen.add(value.id)
            for expression in assignments.get(value.id, []):
                if expression is not None:
                    values.update(strings(expression, assignments, seen))
    return sorted(values)


def inventory(root):
    files = [*root.joinpath('hooks').rglob('*.py'), *root.joinpath('scripts').rglob('*.py'), *root.joinpath('tests').rglob('*.py')]
    result = []
    for path in sorted(files):
        if '__pycache__' in path.parts:
            continue
        tree = ast.parse(path.read_text())
        names, assignments = imports(tree), assigned(tree)
        module = '.'.join(path.relative_to(root).with_suffix('').parts)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls, launches = set(), []
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = text(call.func)
                first, *tail = name.split('.')
                qualified = '.'.join([names.get(first, first), *tail])
                calls.add(qualified)
                if qualified in PROCESS_LAUNCHERS or name.split('.')[-1] in LAUNCHERS:
                    values = strings(call, assignments)
                    targets = [value for value in values if '.py' in value or '.sh' in value]
                    launches.append({'line': call.lineno, 'callee': qualified, 'expression': text(call),
                                     'literal_script_candidates': targets,
                                     'requires_target_review': not bool(targets)})
            if calls or launches:
                result.append({'symbol': module + '.' + node.name, 'path': str(path.relative_to(root)),
                               'line': node.lineno, 'imports': names, 'calls': sorted(calls), 'launches': launches})
    return {'schema': 1, 'status': 'Static helper and launcher inventory; dynamic targets require explicit review.',
            'functions': result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--parity-matrix', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    result = inventory(root)
    matrix = json.loads(args.parity_matrix.read_text())
    errors = reviewed_processes(root, result, matrix)
    result['errors'] = errors
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def reviewed_processes(root, result, matrix):
    """Direct test subprocesses need exact reviewed bytes and expressions.

    Collection checks aliases and recursive helpers separately. This inventory
    also covers helpers that no current test invokes.
    """
    contracts = {(entry['source'], entry['expression']): entry
                 for entry in matrix.get('launchers', [])}
    errors, digests = set(), {}
    for function in result['functions']:
        source = function['path']
        if not source.startswith('tests/'):
            continue
        for launch in function['launches']:
            if launch['callee'] not in PROCESS_LAUNCHERS:
                continue
            contract = contracts.get((source, launch['expression']))
            if source not in digests:
                digests[source] = hashlib.sha256((root / source).read_bytes()).hexdigest()
            if (contract is None or contract.get('source_sha256') != digests[source]
                    or not (contract.get('capability') or
                            (contract.get('scope') in {'support', 'structural'}
                             and contract.get('reason')))):
                errors.add(f'{source}:{launch["line"]}: subprocess needs an exact reviewed launcher contract')
    return sorted(errors)


if __name__ == '__main__':
    raise SystemExit(main())
