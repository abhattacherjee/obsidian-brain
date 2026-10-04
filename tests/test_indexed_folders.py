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

# Exact (file, folder-argument) pairs that may pass an explicit list, with
# the reason. Per call, not per file: a new literal call in these files is
# still flagged.
_ALLOWED = {
    # Receive sessions/insights as parameters, not a config. Safe for wiki
    # rows: _sync deletes only under the folders it scans (pinned by
    # test_two_folder_ensure_index_keeps_wiki_rows). Exception: ensure_index
    # recreates a corrupt or pre-body-column DB from only these folders; the
    # next helper-driven sync re-adds the wiki rows.
    ("hooks/obsidian_utils.py", "[sessions_folder, insights_folder]"),
    ("hooks/open_item_dedup.py", "folders"),
    # rebuild_index's own fallback recursion passes its parameter through.
    ("hooks/vault_index.py", "folders"),
    # The wiki CLI context: _context() sets ctx["folders"] from
    # indexed_folders(cfg, strict=True) (#395).
    ("hooks/wiki.py", 'ctx["folders"]'),
}

_CALLEES = ("ensure_index", "rebuild_index")


def _scanned_files(root: Path) -> list:
    files = list((root / "skills").glob("*/SKILL.md"))
    files += list((root / "hooks").glob("*.py"))
    files += list((root / "scripts").glob("*.py"))
    files += list((root / "scripts").glob("*.sh"))
    return sorted(files)


def _call_args(text: str, open_paren: int):
    """(list of argument source strings, index after the closing paren)."""
    depth, args, cur = 0, [], []
    for i in range(open_paren, len(text)):
        ch = text[i]
        if ch in "([{":
            depth += 1
            if depth > 1:
                cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur).strip())
                return args, i + 1
            cur.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    return args, len(text)


def _is_whole_helper_call(expr: str) -> bool:
    """True only for ``indexed_folders(...)`` with nothing after its paren."""
    if not expr.startswith("indexed_folders("):
        return False
    _, end = _call_args(expr, len("indexed_folders"))
    return end == len(expr)


def _names(text: str) -> list:
    names = list(_CALLEES)
    for m in re.finditer(r"\b(?:ensure_index|rebuild_index)\s+as\s+(\w+)", text):
        names.append(m.group(1))
    return names


def _violations_text(text: str, rel: str) -> list:
    out = []
    call_re = re.compile(r"\b(" + "|".join(map(re.escape, _names(text))) + r")\(")
    for m in call_re.finditer(text):
        before = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        if re.search(r"\bdef\s+$", before) or re.search(r"\bimport\s+$|\bas\s+$", before):
            continue  # the definition itself, or an import line
        args, _ = _call_args(text, m.end() - 1)
        arg = args[1] if len(args) > 1 else ""
        if not arg or _is_whole_helper_call(arg):
            continue
        if re.fullmatch(r"[A-Za-z_]\w*", arg):
            assigns = list(re.finditer(rf"\b{arg}\s*=(?!=)\s*(.*)", text[:m.start()]))
            if assigns and _is_whole_helper_call(assigns[-1].group(1).strip()):
                continue
        if (rel, arg) in _ALLOWED:
            continue
        line = text.count("\n", 0, m.start()) + 1
        out.append(f"{rel}:{line}: {arg}")
    return out


def _violations(path: Path, root: Path) -> list:
    text = path.read_text(encoding="utf-8", errors="replace")
    return _violations_text(text, str(path.relative_to(root)))


def test_every_index_call_uses_indexed_folders():
    bad = []
    for p in _scanned_files(REPO):
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


# --- review fix wave (#393) -------------------------------------------------

import pytest


def test_strict_raises_on_invalid_wiki_folder():
    # /vault-reindex and /obsidian-setup prune rows outside the folders they
    # scan; silently dropping a bad wiki_folder there deletes the wiki rows.
    with pytest.raises(ValueError, match="wiki_folder"):
        indexed_folders({"wiki_folder": "~/claude-wiki"}, strict=True)
    with pytest.raises(ValueError, match="wiki_folder"):
        indexed_folders({"wiki_folder": 5}, strict=True)


def test_strict_accepts_valid_and_empty():
    assert indexed_folders({"wiki_folder": "w"}, strict=True)[-1] == "w"
    assert indexed_folders({"wiki_folder": ""}, strict=True) == ["claude-sessions", "claude-insights"]


def test_wiki_folder_is_normalised_before_dedup():
    assert indexed_folders({"wiki_folder": "./claude-wiki/"})[-1] == "claude-wiki"
    assert indexed_folders({"wiki_folder": "claude-sessions/"}) == ["claude-sessions", "claude-insights"]


def test_load_config_fresh_bypasses_session_cache(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"vault_path": "/v", "wiki_folder": "new-wiki"}')
    monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", cfg)
    monkeypatch.setattr(obsidian_utils, "cache_get", lambda sid, key: {"vault_path": "/v", "wiki_folder": "old"})
    monkeypatch.setattr(obsidian_utils, "cache_set", lambda sid, key, val: None)
    assert obsidian_utils.load_config()["wiki_folder"] == "old"
    assert obsidian_utils.load_config(fresh=True)["wiki_folder"] == "new-wiki"


def test_guard_catches_sliced_helper(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("db = ensure_index(vp, indexed_folders(c)[:2])\n")
    assert _violations(p, tmp_path)


def test_guard_catches_reassigned_name(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("folders = indexed_folders(c)\nfolders = folders[:2]\ndb = ensure_index(vp, folders)\n")
    assert _violations(p, tmp_path)


def test_guard_catches_one_line_def(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("def f(c): return ensure_index(c, [a, b])\n")
    assert _violations(p, tmp_path)


def test_guard_catches_import_alias(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("from vault_index import rebuild_index as ri\nri(v, [a, b])\n")
    assert _violations(p, tmp_path)


def test_guard_catches_call_after_backticks(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("x = '``'; ensure_index(vp, [a, b])\n")
    assert _violations(p, tmp_path)


def test_guard_still_skips_definitions_and_prose(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("def ensure_index(vault_path, folders, db_path=None):\n    pass\n"
                 "# see rebuild_index() for details\n")
    assert not _violations(p, tmp_path)


def test_allow_list_is_per_call_not_per_file():
    # A new literal call in an allow-listed file must still be flagged.
    text = (REPO / "hooks" / "obsidian_utils.py").read_text(encoding="utf-8")
    probe = text + "\n\ndef _probe(v):\n    return ensure_index(v, ['a', 'b'])\n"
    found = _violations_text(probe, "hooks/obsidian_utils.py")
    assert any("['a', 'b']" in v for v in found), found


# --- cross-model fix wave (#393 R1-R3) ---------------------------------------


@pytest.mark.parametrize("bad", [False, 0, [], {}])
def test_falsy_non_string_wiki_folder_is_invalid(bad, capsys):
    # Only "" and None mean "off"; any other non-string is invalid (X-001).
    with pytest.raises(ValueError, match="wiki_folder"):
        indexed_folders({"wiki_folder": bad}, strict=True)
    assert indexed_folders({"wiki_folder": bad}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


@pytest.mark.parametrize("alias", ["./", "././", "a/..", "./."])
def test_vault_root_aliases_are_rejected(alias, capsys):
    # Validated after normalising, so nothing collapses to the vault root (X-002).
    with pytest.raises(ValueError, match="wiki_folder"):
        indexed_folders({"wiki_folder": alias}, strict=True)
    assert indexed_folders({"wiki_folder": alias}) == ["claude-sessions", "claude-insights"]


@pytest.mark.parametrize("raw", ["claude-wiki/../other", ".obsidian/../other", "~/../other"])
def test_forbidden_segments_rejected_before_normalising(raw):
    # Normalising alone would erase the ".." / "~" / dot segment (X-006).
    with pytest.raises(ValueError, match="wiki_folder"):
        indexed_folders({"wiki_folder": raw}, strict=True)
