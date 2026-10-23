"""search_vault(..., log_access=False) for internal callers (#448).

Internal searches (capture dedup, wiki lookup, check-items dedup, /compress
dedup) are not a person retrieving a note. Logging their hits as accesses
fed ACT-R activation with noise, so they pass log_access=False. Python callers are checked with `ast`, SKILL.md
code blocks with a regex. The tests make every new search_vault call outside the user-facing list say so.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import vault_index

REPO = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("hooks", "skills", "scripts", "mods")

# Calls that ARE a person or agent retrieving notes: they keep logging.
# The only such callers are SKILL.md procedures, so no .py file is listed.
USER_FACING = {
    "skills/vault-search/SKILL.md",
    "skills/vault-ask/SKILL.md",
}
USER_FACING_PY: set[str] = set()


def _make_index(tmp_path: Path) -> str:
    vault = tmp_path / "vault"
    ins = vault / "claude-insights"
    ins.mkdir(parents=True)
    for i in range(3):
        (ins / f"2026-10-2{i}-note-{i}.md").write_text(
            "---\ntype: claude-insight\ndate: 2026-10-20\nproject: p\n---\n\n"
            f"# Frobulator widget {i}\n\nThe frobulator widget body {i}.\n",
            encoding="utf-8",
        )
    db = tmp_path / "flag.db"
    vault_index.ensure_index(str(vault), ["claude-insights"], db_path=str(db))
    return str(db)


def _access_rows(db: str) -> int:
    conn = vault_index._connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    finally:
        conn.close()


def test_log_access_false_writes_no_rows(tmp_path):
    db = _make_index(tmp_path)
    before = _access_rows(db)
    hits = vault_index.search_vault(db, "frobulator widget", log_access=False)
    assert hits, "fixture must return hits, or the row count proves nothing"
    assert _access_rows(db) == before


def test_default_still_logs(tmp_path):
    db = _make_index(tmp_path)
    before = _access_rows(db)
    hits = vault_index.search_vault(db, "frobulator widget")
    assert hits
    assert _access_rows(db) == before + len(hits)


# --- caller guard: Python files via ast --------------------------------------

_NAME = "search_vault"


def _py_findings(text: str) -> list[tuple[int, str]]:
    """Return (line, problem) for each search_vault use in Python *text* that
    breaks the rule. Problems: "alias" (imported under another name),
    "indirect" (referenced but not called directly), "flag" (called without a
    keyword ``log_access`` whose value is the constant False).

    Names bound to the function: ``from vault_index import search_vault [as x]``
    and any attribute access ending in ``.search_vault`` (covers
    ``import vault_index as y`` then ``y.search_vault``). The definition is
    skipped because a FunctionDef is not a Call.
    """
    tree = ast.parse(text)
    names = {_NAME}
    problems: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == _NAME:
                    if a.asname and a.asname != _NAME:
                        names.add(a.asname)
                        problems.append((node.lineno, "alias"))
    called: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        hit = (isinstance(f, ast.Name) and f.id in names) or (
            isinstance(f, ast.Attribute) and f.attr == _NAME
        )
        if not hit:
            continue
        called.add(id(f))
        ok = any(
            k.arg == "log_access"
            and isinstance(k.value, ast.Constant)
            and k.value.value is False
            for k in node.keywords
        )
        if not ok:
            problems.append((node.lineno, "flag"))
    for node in ast.walk(tree):
        is_ref = (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute) and node.attr == _NAME
        )
        if is_ref and id(node) not in called:
            problems.append((node.lineno, "indirect"))
    return sorted(set(problems))


def _py_call_lines(text: str) -> list[int]:
    tree = ast.parse(text)
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == _NAME)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == _NAME)
        )
    ]


# --- caller guard: SKILL.md / shell text via regex ---------------------------

_CALL_RE = re.compile(r"\bsearch_vault\s*\(")


def _calls(text: str):
    """Yield (line_no, argument text) for each search_vault( call in non-Python
    text. Skips the definition, any match after a '#' on its line (a comment)
    and an empty argument list (prose)."""
    for m in _CALL_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line = text[line_start:m.start()]
        if "#" in line or line.rstrip().endswith("def"):
            continue
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        args = text[m.end():i - 1]
        if not args.strip():
            continue  # "search_vault()" in prose: a real call needs db_path
        yield text.count("\n", 0, m.start()) + 1, args


def _files():
    for d in SCAN_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix in (".py", ".md", ".sh") and path.is_file():
                rel = path.relative_to(REPO).as_posix()
                if rel != "hooks/vault_index.py":  # the definition
                    yield path, rel


def _py_scan():
    """(rel, line) of every direct call, and (rel, line, problem) violations."""
    calls, bad = [], []
    for path, rel in _files():
        if path.suffix != ".py":
            continue
        text = path.read_text(encoding="utf-8")
        calls += [(rel, n) for n in _py_call_lines(text)]
        if rel not in USER_FACING_PY:
            bad += [(rel, n, why) for n, why in _py_findings(text)]
    return calls, bad


def _text_calls():
    found = []
    for path, rel in _files():
        if path.suffix == ".py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, args in _calls(text):
            found.append((rel, line_no, args))
    return found


def test_scan_finds_the_known_callers():
    """Positive control: the scan must see the callers it is meant to police,
    or an empty result would pass every assertion below."""
    py_files = {rel for rel, _ in _py_scan()[0]}
    for expected in (
        "hooks/capture_save.py",
        "hooks/wiki.py",
        "hooks/open_item_dedup.py",
        "scripts/tune_compress_rank_gap.py",
        "scripts/dev-test/test-issue-192-pollution-guard.py",
    ):
        assert expected in py_files, expected
    md_files = {rel for rel, _, _ in _text_calls()}
    for expected in (
        "skills/compress/SKILL.md",
        "skills/vault-search/SKILL.md",
        "skills/vault-ask/SKILL.md",
    ):
        assert expected in md_files, expected


def test_python_callers_pass_log_access_false():
    _, bad = _py_scan()
    assert not bad, f"search_vault in .py must be called directly with log_access=False: {bad}"


def test_skill_callers_pass_log_access_false():
    bad = [
        f"{rel}:{line}"
        for rel, line, args in _text_calls()
        if rel not in USER_FACING and not re.search(r"\blog_access\s*=\s*False\b", args)
    ]
    assert not bad, f"internal search_vault calls must pass log_access=False: {bad}"


def test_user_facing_callers_still_log():
    for rel, line, args in _text_calls():
        if rel in USER_FACING:
            assert "log_access" not in args, f"{rel}:{line} must keep logging"


def test_no_text_file_aliases_search_vault():
    """An aliased import in a SKILL.md/shell snippet would hide calls from the regex."""
    bad = []
    for path, rel in _files():
        if path.suffix == ".py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bsearch_vault\s+as\s+\w", text):
            bad.append(rel)
    assert not bad, f"do not alias search_vault, the caller scan cannot see it: {bad}"


def test_capture_split_scorer_does_not_search():
    """capture_split._Scorer scores with stored vectors and logs nothing; the
    scan above covers it, this pins the reason it is not in the list."""
    text = (REPO / "hooks" / "capture_split.py").read_text(encoding="utf-8")
    assert _py_call_lines(text) == []


@pytest.mark.parametrize("snippet", [
    'x = "#"; search_vault(db, q)',
    "search_vault(db, q)  # log_access=False",
    "search_vault(db, q, extra=f(log_access=False))",
    "from vault_index import (\n    search_vault  # c\n    as sv,\n)\nsv(db, q)\n",
    "import vault_index as vi\nvi.search_vault(db, q)",
    "search_vault(db, q, log_access=True)",
    "search_vault(db, q, **kw)",
    "f = vault_index.search_vault\nf(db, q)",
], ids=["hash-in-string", "comment-flag", "nested-call-flag", "multiline-alias",
        "module-alias", "flag-true", "kwargs-only", "indirect-reference"])
def test_py_scanner_flags(snippet):
    assert _py_findings(snippet)


@pytest.mark.parametrize("snippet", [
    "search_vault(db, q, log_access=False)",
    "x = vi.search_vault(\n  db, q, limit=3, log_access=False)\n",
    "def search_vault(db_path):\n    pass\n",
    "# search_vault(db, q) in a comment\nx = 1\n",
], ids=["inline", "multiline", "definition", "comment-only"])
def test_py_scanner_passes(snippet):
    assert _py_findings(snippet) == []


@pytest.mark.parametrize("snippet,expected", [
    ("x = search_vault(db, q, log_access=False)", ["db, q, log_access=False"]),
    ("x = vault_index.search_vault(\n  db, q,\n  limit=3)\n", ["\n  db, q,\n  limit=3"]),
    ("# search_vault(db) in a comment\n", []),
    ("def search_vault(db_path):\n", []),
    ("`search_vault` in prose\n", []),
    ("the same search_vault() path\n", []),
    ("x = _vault_index.search_vault(db, q)", ["db, q"]),
    ("x = vi.search_vault(db, q)", ["db, q"]),
    ("x = search_vault (db, q)", ["db, q"]),
], ids=["inline", "multiline", "comment", "def", "prose", "prose-empty-call",
        "underscore-qualified", "alias-qualified", "space-before-paren"])
def test_call_scanner(snippet, expected):
    assert [a for _, a in _calls(snippet)] == expected
