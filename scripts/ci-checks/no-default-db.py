#!/usr/bin/env python3
"""Fail CI if any test calls a DB-opening helper without an explicit ``db_path=`` kwarg.

Guards against recurrence of GH #46: pytest fixtures polluting the user's live
``~/.claude/obsidian-brain-vault.db`` because ``ensure_index()`` /
``rebuild_index()`` / ``deep_analysis_pipeline()`` default ``db_path=None`` to
the user-home path when the caller forgets to pass one.

Exit codes:
    0 — every call site passes ``db_path=`` (clean)
    1 — one or more violations (printed to stdout, summary on stderr)
    2 — script malfunction: missing tests dir, unreadable file, parse error

To silence a known-safe call (e.g. the helper is mocked at runtime), place a
``# noqa: no-default-db`` comment anywhere on the lines spanning the call
(from the function name through the closing paren).

Known limitations:
    * Name-based matching — the guard checks the *trailing* attribute or name
      (``ensure_index`` / ``vault_index.ensure_index`` / ``self.x.ensure_index``
      all match). Unrelated helpers with the same trailing name will trip the
      guard; add ``# noqa: no-default-db`` or rename them.
    * Aliased imports — ``from vault_index import ensure_index as ei`` then
      ``ei(...)`` will NOT be detected. Avoid aliasing the guarded helpers.
    * ``**kwargs`` expansion — calls forwarding unknown keywords via
      ``**kwargs`` are allowed (we cannot statically verify them) but a warning
      is emitted to stderr so reviewers can verify the caller.
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

# --- #192: forbid raw sqlite3.connect bypasses of vault_index._connect ---

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Dirs scanned for raw-connect bypasses (relative to repo root).
RAW_CONNECT_SCAN_DIRS = ("hooks", "skills", "scripts")

# rel_path -> set of function names permitted to call sqlite3.connect directly.
RAW_CONNECT_ALLOWLIST = {
    "hooks/vault_index.py": {"_connect"},
}

def _extract_python_blocks(md_source: str) -> str:
    """Return a line-aligned projection of md_source containing only the bodies
    of ```python fenced blocks; every other line (prose and the fence markers)
    is blanked. Line numbers are PRESERVED — the returned string has the same
    line count as the source — so audit findings report the true source line
    rather than an offset into concatenated blocks (#192 follow-up)."""
    lines = md_source.splitlines()
    out = [""] * len(lines)
    in_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not in_block:
            if stripped == "```python":
                in_block = True
            # the opening-fence line itself stays blank
        else:
            if stripped == "```":
                in_block = False
                # the closing-fence line itself stays blank
            else:
                out[i] = line
    return "\n".join(out)


def _is_sqlite_connect(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sqlite3"
    )


def audit_raw_connect(rel_path: str, source: str) -> list[tuple[int, str]]:
    """Return (lineno, message) for raw sqlite3.connect calls outside the
    allowlisted function for rel_path, unless suppressed by
    '# noqa: vault-db-connect' on the call's line. SyntaxError -> no findings
    (best-effort for partial SKILL.md snippets)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    allowed = RAW_CONNECT_ALLOWLIST.get(rel_path, set())
    lines = source.splitlines()
    findings: list[tuple[int, str]] = []

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            if _is_sqlite_connect(node):
                enclosing = self.stack[-1] if self.stack else "<module>"
                line = lines[node.lineno - 1] if 0 <= node.lineno - 1 < len(lines) else ""
                if enclosing not in allowed and "noqa: vault-db-connect" not in line:
                    findings.append(
                        (
                            node.lineno,
                            f"raw sqlite3.connect in {enclosing}() — route through "
                            f"vault_index._connect() or add '# noqa: vault-db-connect'",
                        )
                    )
            self.generic_visit(node)

    _V().visit(tree)
    return findings


def audit_shell_raw_connect(rel_path: str, source: str) -> list[tuple[int, str]]:
    """Line-based scan of a shell script for raw sqlite3.connect( calls
    (typically inside a python heredoc or `python3 -c`). Heredoc python often
    interpolates shell vars and would not parse as standalone Python, so this is
    intentionally a text scan, not AST. Shell scripts never define
    vault_index._connect, so any occurrence is a bypass unless the line carries
    '# noqa: vault-db-connect' (#192 follow-up: .sh files were previously
    unscanned)."""
    findings: list[tuple[int, str]] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if "sqlite3.connect(" in line and "noqa: vault-db-connect" not in line:
            findings.append(
                (
                    i,
                    "raw sqlite3.connect in shell script — route through "
                    "vault_index._connect() or add '# noqa: vault-db-connect'",
                )
            )
    return findings


def scan_raw_connect() -> list[str]:
    """Scan RAW_CONNECT_SCAN_DIRS for bypass violations. Returns formatted lines."""
    out: list[str] = []
    for d in RAW_CONNECT_SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            src = path.read_text(encoding="utf-8", errors="replace")
            for lineno, msg in audit_raw_connect(rel, src):
                out.append(f"{rel}:{lineno}: {msg}")
        for path in sorted(base.rglob("SKILL.md")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            md = path.read_text(encoding="utf-8", errors="replace")
            blocks = _extract_python_blocks(md)
            if not blocks.strip():
                continue
            for lineno, msg in audit_raw_connect(rel, blocks):
                out.append(f"{rel}:{lineno}: {msg} (embedded python)")
        for path in sorted(base.rglob("*.sh")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            src = path.read_text(encoding="utf-8", errors="replace")
            for lineno, msg in audit_shell_raw_connect(rel, src):
                out.append(f"{rel}:{lineno}: {msg}")
    return out


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _kwargs_state(node: ast.Call) -> tuple[bool, bool]:
    """Return (has_db_path, has_kwargs_expansion)."""
    has_db_path = False
    has_expansion = False
    for kw in node.keywords:
        if kw.arg is None:
            has_expansion = True
        elif kw.arg == "db_path":
            has_db_path = True
    return has_db_path, has_expansion


def _call_line_span(node: ast.Call) -> range:
    start = node.lineno
    end = getattr(node, "end_lineno", None) or start
    return range(start, end + 1)


def _has_noqa(source_lines: list[str], span: range) -> bool:
    for lineno in span:
        if 0 < lineno <= len(source_lines):
            if NOQA_MARKER in source_lines[lineno - 1]:
                return True
    return False


def audit_file(path: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return (violations, kwargs_warnings) for a single test file."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    violations: list[tuple[int, str]] = []
    warnings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in GUARDED_FUNCS:
            continue
        span = _call_line_span(node)
        if _has_noqa(lines, span):
            continue
        has_db_path, has_expansion = _kwargs_state(node)
        if has_db_path:
            continue
        if has_expansion:
            warnings.append((node.lineno, name))
            continue
        violations.append((node.lineno, name))
    return violations, warnings


def main(argv: list[str]) -> int:
    tests_dir = Path(argv[1]) if len(argv) > 1 else TESTS_DIR
    if not tests_dir.is_dir():
        print(f"ERROR: tests directory not found: {tests_dir}", file=sys.stderr)
        return 2

    total_violations = 0
    for path in sorted(tests_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            violations, warnings = audit_file(path)
        except SyntaxError as exc:
            print(f"{path}: SyntaxError — {exc}", file=sys.stderr)
            return 2
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            print(f"{path}: cannot read — {exc}", file=sys.stderr)
            return 2
        rel = path.relative_to(tests_dir.parent) if tests_dir.parent in path.parents else path
        for lineno, name in violations:
            print(f"{rel}:{lineno}: {name}(...) missing db_path= kwarg")
            total_violations += 1
        for lineno, name in warnings:
            print(
                f"{rel}:{lineno}: warning: {name}(...) accepted via **kwargs "
                "— verify caller passes db_path",
                file=sys.stderr,
            )

    if total_violations:
        print(
            f"\n{total_violations} violation(s). Every call to "
            + ", ".join(sorted(GUARDED_FUNCS))
            + " in tests/ must pass an explicit db_path= kwarg to avoid "
            "polluting the user's live ~/.claude/obsidian-brain-vault.db.",
            file=sys.stderr,
        )

    raw_connect_violations = scan_raw_connect()
    for v in raw_connect_violations:
        print(f"RAW-CONNECT BYPASS: {v}")
    if raw_connect_violations:
        print(
            f"\n{len(raw_connect_violations)} raw sqlite3.connect bypass(es) in "
            "hooks/skills/scripts. Route through vault_index._connect() or add "
            "'# noqa: vault-db-connect' with a brief reason.",
            file=sys.stderr,
        )

    return 1 if (total_violations or raw_connect_violations) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
