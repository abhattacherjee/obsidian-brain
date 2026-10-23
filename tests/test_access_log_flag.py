"""search_vault(..., log_access=False) for internal callers (#448).

Internal searches (capture dedup, wiki lookup, check-items dedup, /compress
dedup) are not a person retrieving a note. Logging their hits as accesses
fed ACT-R activation with noise, so they pass log_access=False. The grep
test makes every new search_vault call outside the user-facing list say so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import vault_index

REPO = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("hooks", "skills", "scripts", "mods")

# Calls that ARE a person or agent retrieving notes: they keep logging.
USER_FACING = {
    "skills/vault-search/SKILL.md",
    "skills/vault-ask/SKILL.md",
}


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


# --- caller grep -------------------------------------------------------------

_CALL_RE = re.compile(r"\bsearch_vault\s*\(")


def _calls(text: str):
    """Yield (line_no, argument text) for each search_vault( call. Skips the
    definition, any match after a '#' on its line (a comment) and an empty
    argument list (prose)."""
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


def _all_calls():
    found = []
    for d in SCAN_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".py", ".md", ".sh") or not path.is_file():
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel == "hooks/vault_index.py":
                continue  # the definition
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_no, args in _calls(text):
                found.append((rel, line_no, args))
    return found


def test_grep_finds_the_known_callers():
    """Positive control: the scan must see the callers it is meant to police,
    or an empty result would pass every assertion below."""
    files = {rel for rel, _, _ in _all_calls()}
    for expected in (
        "hooks/capture_save.py",
        "hooks/wiki.py",
        "hooks/open_item_dedup.py",
        "skills/compress/SKILL.md",
        "scripts/tune_compress_rank_gap.py",
        "skills/vault-search/SKILL.md",
        "skills/vault-ask/SKILL.md",
    ):
        assert expected in files, expected


def test_internal_callers_pass_log_access_false():
    bad = [
        f"{rel}:{line}"
        for rel, line, args in _all_calls()
        if rel not in USER_FACING and not re.search(r"\blog_access\s*=\s*False\b", args)
    ]
    assert not bad, f"internal search_vault calls must pass log_access=False: {bad}"


def test_user_facing_callers_still_log():
    for rel, line, args in _all_calls():
        if rel in USER_FACING:
            assert "log_access" not in args, f"{rel}:{line} must keep logging"


def test_capture_split_scorer_does_not_search():
    """capture_split._Scorer scores with stored vectors and logs nothing; the
    grep above covers it, this pins the reason it is not in the list."""
    text = (REPO / "hooks" / "capture_split.py").read_text(encoding="utf-8")
    assert list(_calls(text)) == []


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


def test_no_file_imports_search_vault_under_an_alias():
    """An aliased import (`search_vault as sv`) would hide calls from the scan."""
    bad = []
    for d in SCAN_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".py", ".md", ".sh") or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\bsearch_vault\s+as\s+\w", text):
                bad.append(path.relative_to(REPO).as_posix())
    assert not bad, f"do not alias search_vault, the caller scan cannot see it: {bad}"
