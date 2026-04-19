#!/usr/bin/env python3
"""Fail CI if any test calls a DB-opening helper without an explicit ``db_path=`` kwarg.

Guards against recurrence of GH #46: pytest fixtures polluting the user's live
``~/.claude/obsidian-brain-vault.db`` because ``ensure_index()`` /
``rebuild_index()`` / ``deep_analysis_pipeline()`` default ``db_path=None`` to
the user-home path when the caller forgets to pass one.

Exit 0 when every call site passes ``db_path=``; exit 1 with a file:line
violation list otherwise.

To silence a known-safe call (e.g. the helper is mocked at runtime), place a
``# noqa: no-default-db`` comment on the same line as the call's opening paren.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

GUARDED_FUNCS = frozenset({
    "ensure_index",
    "rebuild_index",
    "deep_analysis_pipeline",
})
NOQA_MARKER = "# noqa: no-default-db"
TESTS_DIR = Path(__file__).resolve().parent.parent.parent / "tests"


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _has_db_path_kwarg(node: ast.Call) -> bool:
    for kw in node.keywords:
        # ``**kwargs`` expansion has arg=None; we can't tell statically, so
        # allow it rather than reject every test that forwards kwargs.
        if kw.arg is None or kw.arg == "db_path":
            return True
    return False


def _has_noqa(source_lines: list[str], lineno: int) -> bool:
    if 0 < lineno <= len(source_lines):
        return NOQA_MARKER in source_lines[lineno - 1]
    return False


def audit_file(path: Path) -> list[tuple[int, str]]:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in GUARDED_FUNCS:
            continue
        if _has_db_path_kwarg(node):
            continue
        if _has_noqa(lines, node.lineno):
            continue
        violations.append((node.lineno, name))
    return violations


def main(argv: list[str]) -> int:
    tests_dir = Path(argv[1]) if len(argv) > 1 else TESTS_DIR
    if not tests_dir.is_dir():
        print(f"ERROR: tests directory not found: {tests_dir}", file=sys.stderr)
        return 2

    total = 0
    for path in sorted(tests_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            violations = audit_file(path)
        except SyntaxError as exc:
            print(f"{path}: SyntaxError — {exc}", file=sys.stderr)
            return 2
        for lineno, name in violations:
            rel = path.relative_to(tests_dir.parent)
            print(f"{rel}:{lineno}: {name}(...) missing db_path= kwarg")
            total += 1

    if total:
        print(
            f"\n{total} violation(s). Every call to "
            + ", ".join(sorted(GUARDED_FUNCS))
            + " in tests/ must pass an explicit db_path= kwarg to avoid "
            "polluting the user's live ~/.claude/obsidian-brain-vault.db.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
