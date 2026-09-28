"""Guard: shipped Python must import on Python 3.9 (#371).

macOS system ``python3`` is 3.9, and ``hooks/hooks.json`` and the skill
snippets run bare ``python3``. A PEP 604 union (``str | None``) that is
evaluated at runtime raises ``TypeError`` at import on 3.9 — which took down
every lifecycle hook in v3.6.0. Local preflight runs 3.12+, where the same
line is legal, so this static check is what catches it before a PR. The
``python-tests-py39`` CI job then runs the whole suite on 3.9.

A union is evaluated at runtime when it sits outside an annotation, or inside
an annotation in a module without ``from __future__ import annotations``.

Scope: a ``|`` is flagged only when one operand is ``None``, a builtin type,
``Path``, or a subscript of one (``list[int]``). A union of two user classes
(``Foo | Bar``) is NOT detected; that is indistinguishable from a bitwise or
without type information. The 3.9 CI job covers such cases in any module the
suite imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = sorted(
    p for d in ("hooks", "scripts", ".claude/hooks")
    for p in (ROOT / d).rglob("*.py")
    if "__pycache__" not in p.parts
)

# Operands that make ``a | b`` a type union rather than an int/set/flag ``|``.
# Only these (plus ``None`` and subscripts of these) are recognised: a union
# of two user classes such as ``Foo | Bar`` is not detected (see docstring).
_TYPE_NAMES = {
    "str", "int", "float", "bool", "bytes", "complex", "object", "type",
    "dict", "list", "tuple", "set", "frozenset", "Path",
}


def _is_type_operand(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Name) and node.id in _TYPE_NAMES:
        return True
    if isinstance(node, ast.Subscript):
        return _is_type_operand(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_type_operand(node.left) or _is_type_operand(node.right)
    return False


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """ids of every node that is part of an annotation."""
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for a in args.posonlyargs + args.args + args.kwonlyargs:
                roots.append(a.annotation)
            roots += [args.vararg and args.vararg.annotation,
                      args.kwarg and args.kwarg.annotation, node.returns]
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(n) for r in roots if r is not None for n in ast.walk(r)}


def runtime_unions(source: str) -> list[int]:
    """Line numbers of PEP 604 unions that execute when the module loads."""
    tree = ast.parse(source)
    deferred = any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    in_annotation = _annotation_nodes(tree) if deferred else set()
    return sorted({
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
        and id(n) not in in_annotation
        and (_is_type_operand(n.left) or _is_type_operand(n.right))
    })


def test_shipped_set_is_not_empty():
    """An empty SHIPPED would make the parametrized check below pass vacuously."""
    assert ROOT / "hooks" / "obsidian_utils.py" in SHIPPED
    assert ROOT / ".claude" / "hooks" / "require-preflight.py" in SHIPPED


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_runtime_pep604_union(path):
    lines = runtime_unions(path.read_text(encoding="utf-8"))
    assert not lines, (
        f"{path.relative_to(ROOT)} lines {lines}: `X | None` runs at import and "
        "raises TypeError on Python 3.9. Use typing.Optional / typing.Union."
    )


@pytest.mark.parametrize("source, expected", [
    # The v3.6.0 regression: a runtime alias, future import or not.
    ("from __future__ import annotations\nA = dict[str | None, int]\n", [2]),
    ("A = tuple[str | None, int]\n", [1]),
    # Annotations are evaluated at def time without the future import...
    ("def f(x: int | None): pass\n", [1]),
    ("x: str | None = None\n", [1]),
    # ...and deferred with it.
    ("from __future__ import annotations\ndef f(x: int | None) -> list[str] | None: pass\n", []),
    ("from __future__ import annotations\nx: str | None = None\n", []),
    # Runtime type unions outside annotations.
    ("isinstance(v, int | str)\n", [1]),
    # With the future import, only annotations are deferred: a default value
    # and a function body still run.
    ("from __future__ import annotations\ndef f(x=int | None): pass\n", [2]),
    ("from __future__ import annotations\ndef f():\n    return isinstance(v, str | None)\n", [3]),
    # A subscripted builtin is a type operand even next to a user class.
    ("A = Foo | list[int]\n", [1]),
    # *args / **kwargs annotations are annotations too...
    ("from __future__ import annotations\ndef f(*a: int | None, **k: str | None): pass\n", []),
    # ...and run at def time without the future import.
    ("def f(*a: int | None): pass\n", [1]),
    # Known gap: two user classes are not recognised (see module docstring).
    ("A = Foo | Bar\n", []),
    # Plain bitwise / set / flag ors are not unions.
    ("a = 1 | 2\nb = s | t\nc = re.I | re.M\n", []),
])
def test_runtime_unions_detector(source, expected):
    assert runtime_unions(source) == expected
