"""Read-only memory consolidation contracts."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "hooks"))

from memory_consolidate import StoreError, audit, index_edit, load_plan, main, read_entry, resolve_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".claude/projects/-x-proj/memory"
    path.mkdir(parents=True)
    return path


def populate(store, index, *slugs):
    (store / "MEMORY.md").write_text(index, encoding="utf-8")
    for slug in slugs:
        (store / f"{slug}.md").write_text("body", encoding="utf-8")


def test_clean(store, capsys):
    populate(store, "- [A](a.md)\n", "a")
    report = audit(store)
    assert report["mode"] == "ok"
    assert main(["audit", "--store", str(store), "--json"]) == 0
    first, payload = capsys.readouterr().out.split("\n", 1)
    assert first == f"store: {store}"
    assert json.loads(payload) == report
    assert main(["audit", "--store", str(store)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"store: {store}", "index: 12 bytes, 12 chars, 1 lines",
        "limits: 17000 soft bytes, 24000 hard bytes, 200 lines", "over: none",
        "mode: ok", "entries: 1 (1 indexed)", "orphans (0): ", "dangling (0): ", "duplicates (0): ",
    ]


def test_shrink(store):
    populate(store, "- [A](a.md)\n" + " " * 17000, "a")
    report = audit(store)
    assert report["mode"] == "shrink"
    assert report["index"]["over"] == ["soft_bytes"]
    assert report["orphans"] == []
    assert main(["audit", "--store", str(store)]) == 1


def test_rescue(store):
    populate(store, "- [A](a.md)\n", "b", "a")
    report = audit(store)
    assert report["mode"] == "rescue"
    assert report["orphans"] == ["b"]


def test_real_index(store):
    populate(store, "# Memory\n\n- Fixtures: [a](a.md) · [b](b.md)\n"
             "- [C](c.md) — see [[d]]\n- [D](d.md)\nSee [[e]].", *"abcde")
    report = audit(store)
    assert [(e["slug"], e["indexed"]) for e in report["entries"]] == [(s, True) for s in "abcde"]
    assert report["orphans"] == []
    assert report["duplicates"] == []


def test_drift(store):
    populate(store, "- [A](a.md) [A](a.md)\n- [Another](a.md)\n- [Gone](gone.md)", "a")
    report = audit(store)
    assert report["dangling"] == ["gone.md"]
    assert report["duplicates"] == ["a"]
    assert report["mode"] == "ok"
    assert main(["audit", "--store", str(store)]) == 1


@pytest.mark.parametrize(("index", "over"), [
    ("é" * 8499 + " ", []), ("é" * 8500, ["soft_bytes"]),
    ("\n" * 200, []), ("\n" * 201, ["lines"]),
    ("\n" * 201 + " " * 23799, ["soft_bytes", "hard_bytes", "lines"]),
    # The hard limit is `>=`: 23999 stays under it, 24000 and 24001 are over.
    (" " * 23999, ["soft_bytes"]), (" " * 24000, ["soft_bytes", "hard_bytes"]),
    (" " * 24001, ["soft_bytes", "hard_bytes"]),
], ids=["16999-bytes", "17000-bytes", "200-lines", "201-lines", "all-limits",
        "23999-bytes", "24000-bytes", "24001-bytes"])
def test_index_limits(store, index, over):
    populate(store, index)
    assert audit(store)["index"] == dict(bytes=len(index.encode()), chars=len(index),
                                        lines=len(index.splitlines()), soft_limit_bytes=17000,
                                        hard_limit_bytes=24000, line_limit=200, over=over)


def test_store_selection(store, tmp_path):
    with pytest.raises(StoreError, match="expected"):
        resolve_store(str(tmp_path), "/")
    assert resolve_store(None, "/x/proj") == store
    with pytest.raises(StoreError, match="no memory store for /a/b_c; pass --store"):
        resolve_store(None, "/a/b_c")
    projects = store.parent.parent
    candidates = [projects / name / "memory" for name in ("-a-b-c", "-a-b_c")]
    for p in candidates:
        p.mkdir(parents=True)
    with pytest.raises(StoreError) as error:
        resolve_store(None, "/a/b_c")
    assert str(error.value) == f"multiple memory stores: {candidates[0]}, {candidates[1]}; pass --store"


@pytest.mark.parametrize("link_project", [False, True])
def test_symlink_store(store, link_project):
    project = store.parent.with_name("alias")
    if link_project:
        project.symlink_to(store.parent)
    else:
        project.mkdir()
        (project / "memory").symlink_to(store)
    with pytest.raises(StoreError, match="symlink"):
        resolve_store(str(project / "memory"), "/")


def test_worktree_uses_main_root(store, monkeypatch):
    def git(args, **kwargs):
        assert args == ["git", "-C", "/worktree", "rev-parse", "--path-format=absolute", "--git-common-dir"]
        assert kwargs == dict(stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=True)
        return subprocess.CompletedProcess(args, 0, stdout="/x/proj/.git\n")

    monkeypatch.setattr(subprocess, "run", git)
    assert resolve_store(None, "/worktree") == store


def test_bad_args(capsys):
    assert main(["audit", "--unknown"]) == 2
    assert capsys.readouterr().err.splitlines()[-1] == "error: unrecognized arguments: --unknown"


@pytest.mark.parametrize("argv", [
    ["audit", "--jso"], ["apply", "--plan", "p", "--appl"], ["verify", "--backu", "b"],
    ["smoke", "--quer", "q"], ["apply", "--pla", "p"],
])
def test_abbreviated_flags_are_rejected(store, argv, capsys):
    """argparse would take --app for --apply and turn a dry run into a write.

    The ``store`` fixture points HOME at tmp_path, so an accepted abbreviation would fail
    later for another reason; only a parse error prints the usage line, which tells them apart."""
    assert main(argv) == 2
    assert "usage:" in capsys.readouterr().err


def test_unreadable_entries(store, capsys):
    with pytest.raises(StoreError, match="MEMORY.md"):
        audit(store)
    populate(store, "[A](a.md)")
    (store / "a.md").symlink_to(store / "MEMORY.md")
    with pytest.raises(StoreError, match="non-symlink"):
        audit(store)
    assert main(["audit", "--store", str(store)]) == 2
    assert capsys.readouterr().err.startswith("error: ")


@pytest.mark.parametrize(("header", "kind"), [
    ("metadata:\n  type: feedback", "feedback"),
    ("type: project\nmetadata:\n  type: feedback", "project"),
])
def test_read_entry(store, header, kind):
    path = store / "a.md"
    text = "---\nname: 'A'\ndescription: \"A \\\"quote\\\"\"\n" + header + "\n---\n\n body \n"
    path.write_text(text, encoding="utf-8")
    assert read_entry(path) == dict(slug="a", name="A", description='A "quote"', memory_type=kind,
                                   body=" body", text=text, mtime=path.stat().st_mtime)
    populate(store, "see [[a]]")
    assert audit(store)["entries"] == [dict(slug="a", memory_type=kind, body_chars=5, indexed=True,
        mtime=datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"))]


@pytest.mark.parametrize("text", ["no frontmatter\r\n", "---\nname: unfinished\n"])
def test_plain_entry(store, text):
    path = store / "test.md"
    path.write_bytes(text.encode())
    assert read_entry(path) == dict(slug="test", name="", description="", memory_type="",
                                   body=text.strip(), text=text, mtime=path.stat().st_mtime)


def test_read_limit(store):
    path = store / "large.md"
    path.write_text("é" * 2_000_000, encoding="utf-8")
    assert len(read_entry(path)["text"]) == 2_000_000
    with path.open("a", encoding="utf-8") as f:
        f.write("!")
    with pytest.raises(StoreError, match="exceeds 2,000,000"):
        read_entry(path)


@pytest.fixture
def plan_env(store, tmp_path):
    populate(store, "", "a", "b", "feedback_Fix..THIS")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").touch()
    (vault / "alias").symlink_to(vault)
    (tmp_path / "outside.md").touch()
    plan = tmp_path / "plan.json"

    def validate(items, **overrides):
        payload = {"version": 1, "store": str(store), "items": items}
        payload.update(overrides)
        plan.write_text(json.dumps(payload), encoding="utf-8")
        return load_plan(plan, store, vault)

    return validate


@pytest.mark.parametrize(("overrides", "error"), [
    ({"version": 2}, "version must be 1"), ({"version": True}, "version must be 1"),
    ({"store": "/"}, "store mismatch"), ({"extra": 0}, "plan keys must be version, store, items"),
])
def test_plan_header(plan_env, overrides, error):
    assert plan_env([dict(slug="a", action="new")], **overrides)[1] == [error]


def test_empty_items(plan_env):
    assert plan_env([]) == ([], ["items must be a non-empty list"])


@pytest.mark.parametrize(("slug", "fields", "error"), [
    ("../a", {"action": "new"}, "invalid slug"),
    ("MEMORY", {"action": "new"}, "invalid slug"),
    ("memory", {"action": "new"}, "invalid slug"),
    ("Memory", {"action": "new"}, "invalid slug"),
    ("memory.md", {"action": "new"}, "invalid slug"),
    ("absent", {"action": "new"}, "missing or non-regular entry file"),
    ("a", {"action": "skip"}, "invalid action"),
    ("a", {"action": "new", "target": "note.md"}, "unknown key target"),
    ("a", {"action": "new", "type": "other"}, "invalid type"),
    ("a", {"action": "new", "topic": "Too Big"}, "invalid topic"),
    ("a", {"action": "append", "target": "/note.md"}, "target must be a vault-relative .md path"),
    ("a", {"action": "append", "target": "bad\0.md"}, "target must be a vault-relative .md path"),
    ("a", {"action": "append", "target": "../outside.md"}, "target escapes the vault"),
    ("a", {"action": "append", "target": "alias/note.md"}, "target contains a symlink"),
    ("a", {"action": "append", "target": "absent.md"}, "target is not an existing file"),
    ("a", {"action": "retire"}, "reason must be a non-empty string"),
    ("a", {"action": "retire", "reason": " "}, "reason must be a non-empty string"),
])
def test_invalid_item(plan_env, slug, fields, error):
    assert plan_env([dict(slug=slug, **fields)])[1] == [f"item 0 ({slug!r}): {error}"]


def test_duplicate_slug(plan_env):
    assert plan_env([dict(slug="a", action="new")] * 2)[1] == ["item 1 ('a'): duplicate slug"]


def test_invalid_json(store, tmp_path):
    path = tmp_path / "plan"
    path.write_text("!", encoding="utf-8")
    assert load_plan(path, store, tmp_path) == ([], ["Expecting value: line 1 column 1 (char 0)"])


def test_valid_plan(plan_env):
    items = [dict(slug="feedback_Fix..THIS", action="new"),
             dict(slug="a", action="append", target="note.md"),
             dict(slug="b", action="retire", reason="merged")]
    assert plan_env(items) == ([dict(items[0], type="claude-insight", topic="fix-this"), *items[1:]], [])


MIXED = "- Fixtures: [a](a.md) · [b](b.md)"
SINGLE = "- [A](a.md)"
NAMES = {"a.md", "b.md"}
REFUSAL = {"a": "index line also points at staying entries: b.md"}


def test_index_edit_preserves_lines():
    before = ["# Head", "", SINGLE, "- [B](b.md)", "", "Foot", ""]
    edited, dropped, refused, warnings = index_edit("\n".join(before), NAMES, {"a.md"}, "- moved")
    assert edited.split("\n") == ["# Head", "", "- [B](b.md)", "- moved", "", "Foot", ""]
    assert (dropped, refused, warnings) == ([SINGLE], {}, {})


@pytest.mark.parametrize(("before", "moving", "result"), [
    (MIXED, {"a.md"}, (MIXED, [], REFUSAL, {})),
    (MIXED + "\n", NAMES, ("- moved\n", [MIXED], {}, {})),
    (SINGLE + "\n" + MIXED, {"a.md"}, (SINGLE + "\n" + MIXED, [], REFUSAL, {})),
    ("- [B](b.md) — see [[a]]", {"a.md"},
     ("- [B](b.md) — see [[a]]", [], {}, {"a": "named as an aside on a kept index line"})),
])
def test_index_edit_decisions(before, moving, result):
    assert index_edit(before, NAMES, moving, "- moved") == result
