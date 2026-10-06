"""Reject native host assumptions outside exact named adapter functions."""
from __future__ import annotations

import ast
import re
from pathlib import Path

RULES = (
    ('native_path', re.compile(r'~/(?:\.claude|\.codex)\b|[\"\x27](?:\.claude|\.codex)[\"\x27]|\.(?:claude|codex)/')),
    ('native_cli', re.compile(r'\b(?:claude\s+(?:[^\n]*?\s)?-p|codex\s+(?:[^\n]*?\s)?exec)\b|[\"\x27]claude[\"\x27]\s*,\s*[\"\x27]-p[\"\x27]')),
    ('native_tool', re.compile(r'\b(?:TaskCreate|TaskUpdate|AskUserQuestion|Grep|Agent)\b|\b(?:Write|Edit|Read|Grep|Bash|Agent)\s+tool\b|\b(?:use|Use|using|Using)\s+(?:Write|Edit|Read|Grep|Bash|Agent)\b|`(?:Write|Edit|Read|Grep|Bash|Agent)`|\b(?:Write|Edit|Read|Grep|Bash|Agent)\s*\(|^\*\*Tools needed:\*\*.*\b(?:Write|Edit|Read|Grep|Bash|Agent)\b')),
    ('native_env', re.compile(r'\b(?:CLAUDE_[A-Z0-9_]+|CODEX_[A-Z0-9_]+|PLUGIN_ROOT|PLUGIN_DATA)\b')),
)


def _functions(source):
    tree = ast.parse(source)
    scopes = {}
    def walk(node, parents):
        next_parents = parents
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            next_parents = parents + [node.name]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for line in range(node.lineno, node.end_lineno + 1):
                    scopes[line] = '.'.join(next_parents)
        for child in ast.iter_child_nodes(node):
            walk(child, next_parents)
    walk(tree, [])
    return scopes


def _decision_valid(value):
    return isinstance(value, dict) and set(value) == {'claude', 'codex'} and all(isinstance(decision, str) and (decision == 'supported' or (decision.startswith('unsupported:') and decision.split(':', 1)[1].strip())) for decision in value.values())


def lint_source(path, source, adapters=None):
    """Allow only declared exact functions with both-host capability evidence."""
    adapters = adapters or {}
    path = Path(path)
    scopes = _functions(source) if path.suffix == '.py' else {}
    errors = []
    if path.suffix == '.py':
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.List, ast.Tuple)):
                values = {item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)}
                if ('claude' in values and '-p' in values) or ('codex' in values and 'exec' in values):
                    key = path.as_posix() + ':' + scopes.get(node.lineno, '<module>')
                    if scopes.get(node.lineno) is None or not _decision_valid(adapters.get(key)):
                        errors.append((node.lineno, 'native_cli', ast.get_source_segment(source, node)))
    for number, line in enumerate(source.splitlines(), 1):
        for code, pattern in RULES:
            if not pattern.search(line):
                continue
            key = path.as_posix() + ':' + scopes.get(number, '<module>')
            allowed = adapters.get(key) if number in scopes else None
            if allowed is not None:
                if not _decision_valid(allowed):
                    errors.append((number, 'adapter_capability_invalid', key))
                continue
            errors.append((number, code, line.strip()))
    return errors


def lint_reference_pairs(root):
    errors = []
    for path in Path(root).glob('skills/**/host-*.md'):
        if path.name not in {'host-claude.md', 'host-codex.md'} or path.parent.name != 'references':
            errors.append(str(path))
            continue
        peer = path.with_name('host-codex.md' if path.name == 'host-claude.md' else 'host-claude.md')
        if not peer.is_file():
            errors.append(str(path))
    return errors


def lint_repository(root, adapters):
    """Check shared sources and exact reviewed adapter declarations."""
    root = Path(root).resolve()
    errors = []
    if not isinstance(adapters, dict):
        return [('docs/parity/host-adapters.json', 0, 'adapter_manifest_invalid', 'Expected an object.')]
    for key, decision in adapters.items():
        if not isinstance(key, str) or ':' not in key:
            errors.append(('docs/parity/host-adapters.json', 0, 'adapter_declaration_invalid', str(key)))
            continue
        relative, function = key.rsplit(':', 1)
        path = Path(relative)
        if (path.is_absolute() or '..' in path.parts or path.as_posix() != relative
                or not relative.startswith('hooks/') or path.suffix != '.py'
                or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', function)
                or not _decision_valid(decision)):
            errors.append(('docs/parity/host-adapters.json', 0, 'adapter_declaration_invalid', key))
            continue
        try:
            source = root / path
            source.resolve().relative_to(root)
            functions = set(_functions(source.read_text()).values())
            if function not in functions:
                raise ValueError('Declared function does not exist.')
        except (OSError, SyntaxError, ValueError) as error:
            errors.append(('docs/parity/host-adapters.json', 0, 'adapter_function_invalid', key + ': ' + str(error)))
    sources = (sorted((root / 'hooks').rglob('*.py')) + sorted((root / 'skills').glob('*/SKILL.md'))
               + sorted((root / 'scripts/vault_doctor_checks').rglob('*.py'))
               + [path for path in (root / 'scripts/vault_doctor.py', root / 'scripts/doctor_repair_state.py') if path.is_file()]
               + sorted((root / 'skills').glob('*/references/*.md')))
    sources = [path for path in sources if path.name not in {'host-claude.md', 'host-codex.md'}]
    for source in sources:
        relative = source.relative_to(root).as_posix()
        try:
            source.resolve().relative_to(root)
            errors.extend((relative, number, code, text)
                          for number, code, text in lint_source(relative, source.read_text(), adapters))
        except (OSError, SyntaxError, ValueError) as error:
            errors.append((relative, 0, 'source_invalid', str(error)))
    errors.extend((str(Path(path).relative_to(root)), 0, 'reference_pair_missing',
                   'Native references need the matching host reference.')
                  for path in lint_reference_pairs(root))
    return errors


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate adapter key: ' + key)
        result[key] = value
    return result


def main(argv=None):
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--adapters', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest = args.adapters or root / 'docs/parity/host-adapters.json'
    try:
        adapters = json.loads(manifest.read_text(), object_pairs_hook=_unique_object)
        errors = lint_repository(root, adapters)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    for path, line, code, detail in errors:
        print(f'{path}:{line}: {code}: {detail}', file=sys.stderr)
    if errors:
        return 1
    print('Host boundary passed: shared hooks, authored skills, paired references and exact adapter decisions.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
