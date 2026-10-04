"""indexed_folders(config): the one list of folders user-facing search indexes (#383)."""
from __future__ import annotations

import re
from pathlib import Path

import obsidian_utils
from obsidian_utils import indexed_folders


def test_default_has_wiki_folder():
    assert obsidian_utils._DEFAULTS["wiki_folder"] == "claude-wiki"


def test_order_and_defaults_from_empty_config():
    assert indexed_folders({}) == ["claude-sessions", "claude-insights", "claude-wiki"]


def test_uses_configured_names():
    cfg = {"sessions_folder": "s", "insights_folder": "i", "wiki_folder": "w"}
    assert indexed_folders(cfg) == ["s", "i", "w"]


def test_empty_or_none_wiki_folder_disables_it():
    assert indexed_folders({"wiki_folder": ""}) == ["claude-sessions", "claude-insights"]
    assert indexed_folders({"wiki_folder": None}) == ["claude-sessions", "claude-insights"]


def test_duplicate_names_are_scanned_once():
    cfg = {"sessions_folder": "notes", "insights_folder": "notes", "wiki_folder": "notes"}
    assert indexed_folders(cfg) == ["notes"]


def test_traversal_wiki_folder_is_dropped_with_warning(capsys):
    assert indexed_folders({"wiki_folder": "../outside"}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_absolute_wiki_folder_is_dropped(capsys):
    assert indexed_folders({"wiki_folder": "/tmp/x"}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_non_string_wiki_folder_is_dropped(capsys):
    assert indexed_folders({"wiki_folder": 7}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_nested_wiki_folder_is_allowed():
    assert indexed_folders({"wiki_folder": "kb/claude-wiki"})[-1] == "kb/claude-wiki"


def test_does_not_mutate_input():
    cfg = {"wiki_folder": "w"}
    indexed_folders(cfg)
    assert cfg == {"wiki_folder": "w"}


REPO = Path(__file__).resolve().parent.parent

# Files that legitimately pass explicit folder lists, with the reason.
_ALLOWED = {
    # Receive sessions/insights as parameters from callers, not a config.
    # Safe: _sync deletes only under scanned folders, so wiki rows survive.
    "hooks/obsidian_utils.py": "build_context_brief takes folder params",
    "hooks/open_item_dedup.py": "deep_analysis_pipeline takes folder params",
    # Defines the functions; rebuild_index recurses with its own argument.
    "hooks/vault_index.py": "definition site",
}

_CALL_RE = re.compile(r"\b(ensure_index|rebuild_index)\(")


def _scanned_files(root: Path) -> list:
    files = list((root / "skills").glob("*/SKILL.md"))
    files += list((root / "hooks").glob("*.py"))
    files += list((root / "scripts").glob("*.py"))
    files += list((root / "scripts").glob("*.sh"))
    return sorted(files)


def _second_arg(text: str, open_paren: int) -> str:
    """Source text of the call's second positional argument ('' if none)."""
    depth, args, cur = 0, [], []
    for ch in text[open_paren:]:
        if ch in "([{":
            depth += 1
            if depth > 1:
                cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur).strip())
                break
            cur.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    return args[1] if len(args) > 1 else ""


def _violations(path: Path, root: Path) -> list:
    text = path.read_text(encoding="utf-8", errors="replace")
    out = []
    for m in _CALL_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        before = text[line_start:m.start()]
        if before.lstrip().startswith(("def ", "#")) or "``" in before:
            continue
        arg = _second_arg(text, m.end() - 1)
        if not arg or arg.startswith("indexed_folders("):
            continue
        if re.fullmatch(r"[A-Za-z_]\w*", arg) and re.search(rf"\b{arg}\s*=\s*indexed_folders\(", text):
            continue
        line = text.count("\n", 0, m.start()) + 1
        out.append(f"{path.relative_to(root)}:{line}: {arg}")
    return out


def test_every_index_call_uses_indexed_folders():
    bad = []
    for p in _scanned_files(REPO):
        if str(p.relative_to(REPO)) in _ALLOWED:
            continue
        bad += _violations(p, REPO)
    assert not bad, "pass folders via indexed_folders(config):\n" + "\n".join(bad)


def test_guard_catches_a_literal_list(tmp_path):
    # Positive control: the guard must flag today's shape.
    p = tmp_path / "x.py"
    p.write_text('db = ensure_index(vp, [c.get("sessions_folder"), c.get("insights_folder")])\n')
    assert _violations(p, tmp_path)


def test_guard_catches_a_named_list(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("folders = [a, b]\ndb = ensure_index(vp, folders)\n")
    assert _violations(p, tmp_path)


def test_guard_accepts_the_helper(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("folders = indexed_folders(c)\ndb = ensure_index(vp, folders)\n"
                 "rebuild_index(v, indexed_folders(c), full=True)\n")
    assert not _violations(p, tmp_path)
