"""Trace grammar, lossless writeback, and in-process CLI contracts."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import retro_trace as rt
from frontmatter import split_frontmatter, split_lines_lf_crlf

FIXTURE = Path(__file__).parent / "fixtures/retro/traced-retro.md"
HEADER = (
    "---\ntype: claude-retro\ndate: 2026-10-29\nproject: demo\n"
    "tags:\n  - claude/retro\n  - claude/project/demo\n---\n"
    "\n# Retro\n\n## Key Learnings\n- Plain learning.\n\n"
)
BODY = (
    "## Process Improvements\n- [ ] File the fix.\n- [ ] Remember evidence.\n"
    "  Keep this continuation.\n- [x] Update the guide.\n- [ ] Already covered.\n"
    "\n## Other\nUnchanged.\n"
)
TRACES = [
    {"item": 1, "kind": "issue", "ref": "o/r#5"},
    {"item": 2, "kind": "memory", "stem": "measure_first", "note": "new"},
    {"item": 3, "kind": "path", "path": "docs/guide.md"},
    {"item": 4, "kind": "skipped", "reason": "already covered"},
]


def make_note(tmp_path, text=HEADER + BODY):
    path = tmp_path / "retro.md"
    path.write_bytes(text.encode())
    return path


def snapshot(path):
    return path.read_bytes(), path.stat().st_mtime_ns


def test_fixture(tmp_path):
    text = FIXTURE.read_text()
    items = rt.parse_items(text)
    assert len(items) == 4
    assert [i["trace"]["kind"] for i in items] == ["memory", "memory", "path", "skipped"]
    assert all(i["checked"] for i in items)
    assert rt.untraced_count(make_note(tmp_path, text)) == 0


@pytest.mark.parametrize(("trace", "expected"), [
    ("[o/r#5](https://github.com/o/r/issues/5)",
     {"kind": "issue", "ref": "o/r#5", "url": "https://github.com/o/r/issues/5"}),
    ("[Owner_1/repo.name-2#50](https://github.com/Owner_1/repo.name-2/issues/50)",
     {"kind": "issue", "ref": "Owner_1/repo.name-2#50", "url": "https://github.com/Owner_1/repo.name-2/issues/50"}),
    ("memory `a_B.c-2`", {"kind": "memory", "stem": "a_B.c-2", "note": None}),
    ("memory `stem` (new)", {"kind": "memory", "stem": "stem", "note": "new"}),
    ("`docs/a.md`", {"kind": "path", "path": "docs/a.md"}),
    ("`~scratch`", {"kind": "path", "path": "~scratch"}),
    ("`CLAUDE.md`", {"kind": "path", "path": "CLAUDE.md"}),
    ('skipped: reason "quoted".', {"kind": "skipped", "reason": 'reason "quoted".'}),
])
def test_grammar_valid(trace, expected):
    assert rt.parse_trace("- [ ] prose → ignored → " + trace) == expected
    if expected["kind"] != "skipped":
        assert rt.parse_trace('- [ ] x → ' + trace + ', "caption". \t') == expected
        if expected.get("note") is None:
            annotated = dict(expected)
            if expected["kind"] == "memory":
                annotated["note"] = "context"
            assert rt.parse_trace('- [ ] x → ' + trace + ' (context), "caption".') == annotated


@pytest.mark.parametrize("line", [
    "- [ ] Restore the dev cache → `/dev-test restore` before merge (CI can't see those two surfaces).",
    "- [ ] Move state A → B before the release.",
    "- [ ] x → [o/r#5](https://github.com/o/r/issues/6)",
    "- [ ] x → [o/r#5](https://github.com/other/r/issues/5)",
    "- [ ] x → [o/r#5](https://github.com/o/other/issues/5)",
    "- [ ] x → [o/r#0](https://github.com/o/r/issues/0)",
    "- [ ] x → skipped:   ",
    "- [ ] x → memory `bad stem`",
    "- [ ] x → `word`",
    "- [ ] x → `a/b` trailing prose",
    "- [ ] x → `a/b` (bad) extra)",
    '- [ ] x → `a/b`, "bad" extra',
    "- [ ] x →memory `stem`",
    "- [ ] x → memory `stem` → prose",
])
def test_grammar_invalid(line):
    assert rt.parse_trace(line) is None
    assert rt.parse_items("## Process Improvements\n" + line)[0]["trace"] is None


def test_items_boundaries_and_crlf():
    lines = ["# Intro", "## Process Improvements (continued)",
             "- [ ] First → memory `stem`", "  continuation", "\ttabbed",
             "", "  orphan", "- Plain bullet", "### Subsection", "- [X] Second",
             "- [x] Third", "## Next", "- [ ] Outside"]
    assert rt.parse_items("\r\n".join(lines)) == [
        {"item": 1, "start": 2, "end": 4, "checked": False,
         "text": "\n".join(lines[2:5]), "trace": None},
        {"item": 2, "start": 9, "end": 9, "checked": True, "text": lines[9], "trace": None},
        {"item": 3, "start": 10, "end": 10, "checked": True, "text": lines[10], "trace": None},
    ]
    assert rt.parse_items("# No section\n- [ ] Outside") == []


@pytest.mark.parametrize("field", [
    'tracked_issues: ["a/b#1", c/d#2]\n',
    "tracked_issues:\n  - 'a/b#1'\n  - c/d#2\n",
])
def test_read_tracked_issues(field):
    text = HEADER.replace("tags:\n", field + "tags:\n") + BODY
    assert rt.read_tracked_issues(text) == ["a/b#1", "c/d#2"]
    assert rt.read_tracked_issues(text.replace("\n", "\r\n")) == ["a/b#1", "c/d#2"]
    assert rt.read_tracked_issues(HEADER + BODY) == []
    assert rt.read_tracked_issues("not frontmatter") == []


@pytest.mark.parametrize("existing", ["", 'tracked_issues: ["a/b#1", "a/b#1"]\n',
                                         'tracked_issues:\n  - a/b#1\n  - a/b#1\n'])
def test_apply_exact_and_merge(tmp_path, existing):
    header = HEADER.replace("date:", existing + "date:")
    path = make_note(tmp_path, header + BODY)
    assert rt.untraced_count(path) == 4
    refs = (["a/b#1"] if existing else []) + ["o/r#5"]
    assert rt.apply_traces(str(tmp_path), path.name, TRACES) == {
        "written": True, "untraced": 0, "tracked_issues": refs}
    field = 'tracked_issues: ' + json.dumps(refs) + '\n'
    expected_header = header.replace(existing, field, 1) if existing else header.replace(
        "  - claude/project/demo\n", "  - claude/project/demo\n" + field)
    expected_body = (
        "## Process Improvements\n"
        "- [ ] File the fix. → [o/r#5](https://github.com/o/r/issues/5)\n"
        "- [x] Remember evidence.\n"
        "  Keep this continuation. → memory `measure_first` (new)\n"
        "- [x] Update the guide. → `docs/guide.md`\n"
        "- [x] Already covered. → skipped: already covered\n"
        "\n## Other\nUnchanged.\n"
    )
    assert path.read_bytes() == (expected_header + expected_body).encode()
    assert path.stat().st_mode & 0o777 == 0o600
    import vault_index
    import obsidian_utils
    assert vault_index._parse_note(str(path))["tags"] == "claude/retro,claude/project/demo"
    fm = split_frontmatter(split_lines_lf_crlf(path.read_text()))[1]
    assert obsidian_utils._parse_note_tags("".join(fm)) == ["claude/retro", "claude/project/demo"]
    before = snapshot(path)
    assert rt.apply_traces(str(tmp_path), str(path), []) == {
        "written": False, "untraced": 0, "tracked_issues": refs}
    assert snapshot(path) == before
    for number, ref in [(5, "o/r#5"), (6, "o/r#7")]:
        path.write_bytes(path.read_bytes().replace(b"\n## Other", b"\n- [ ] Another issue.\n## Other"))
        result = rt.apply_traces(str(tmp_path), path.name, [{"item": number, "kind": "issue", "ref": ref}])
        assert result["tracked_issues"] == refs + (["o/r#7"] if number == 6 else [])


BAD_ENTRIES = [
    {"item": 99, "kind": "skipped", "reason": "x"},
    {"item": 1, "kind": "unknown"},
    {"item": 1, "kind": "issue", "ref": "o/r#0"},
    {"item": 1, "kind": "issue", "ref": "o/r#1\n"},
    *[{"item": 1, "kind": kind, key: value}
      for kind, key in [("memory", "stem"), ("path", "path"), ("skipped", "reason")]
      for value in ["", "   ", "a\nb", "a\rb", "a→b", "x" * 301]],
    {"item": 1, "kind": "memory", "stem": "bad`stem"},
    {"item": 1, "kind": "memory", "stem": "bad stem"},
    {"item": 1, "kind": "memory", "stem": "good", "note": "bad)note"},
    *[{"item": 1, "kind": "memory", "stem": "good", "note": v}
      for v in ["a\nb", "a\rb", "a→b", "x" * 301]],
    {"item": 1, "kind": "path", "path": "bad`path.md"},
    {"item": 1, "kind": "path", "path": "word"},
    {"item": True, "kind": "skipped", "reason": "x"},
    {"item": "1", "kind": "skipped", "reason": "x"},
    {"item": 1, "kind": "path", "path": 42},
    None,
]


@pytest.mark.parametrize("entry", BAD_ENTRIES)
def test_refuse_bad_entries(tmp_path, entry):
    path = make_note(tmp_path)
    before = snapshot(path)
    with pytest.raises(rt.TraceError):
        rt.apply_traces(str(tmp_path), path.name, [entry] + TRACES[1:])
    assert snapshot(path) == before
    assert not (tmp_path / ".retro.md.ob-lock").exists()


@pytest.mark.parametrize("case", ["missing", "duplicate", "traced", "no-frontmatter", "bad-frontmatter", "not-list"])
def test_refuse_structure(tmp_path, case):
    text, entries = HEADER + BODY, TRACES
    if case == "missing":
        entries = TRACES[:-1]
    elif case == "duplicate":
        entries = TRACES + [TRACES[0]]
    elif case == "traced":
        text = text.replace("File the fix.", "File the fix. → memory `already`")
    elif case == "no-frontmatter":
        text = BODY
    elif case == "bad-frontmatter":
        text = "---\n" + BODY
    else:
        entries = {"traces": TRACES}
    path = make_note(tmp_path, text)
    before = snapshot(path)
    with pytest.raises(rt.TraceError):
        rt.apply_traces(str(tmp_path), path.name, entries)
    assert snapshot(path) == before


def test_crlf_and_unterminated_last_line(tmp_path):
    path = make_note(tmp_path, (HEADER + BODY).replace("\n", "\r\n"))
    rt.apply_traces(str(tmp_path), path.name, TRACES)
    assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")
    path = make_note(tmp_path, HEADER + "## Process Improvements\n- [ ] Final")
    rt.apply_traces(str(tmp_path), path.name, [{"item": 1, "kind": "skipped", "reason": "done"}])
    assert path.read_bytes().endswith("- [x] Final → skipped: done".encode())


def test_held_lock(tmp_path):
    path = make_note(tmp_path)
    (tmp_path / ".retro.md.ob-lock").touch()
    before = snapshot(path)
    with pytest.raises(OSError, match="lock"):
        rt.apply_traces(str(tmp_path), path.name, TRACES)
    assert snapshot(path) == before


@pytest.mark.parametrize("failure", ["changed-stat", "write-failure"])
def test_atomic_failures(tmp_path, monkeypatch, failure):
    path = make_note(tmp_path)
    before = snapshot(path)
    atomic = rt._atomic_rewrite

    def fail(dest, content, expect_stat):
        assert expect_stat == (before[1], len(before[0]))
        if failure == "changed-stat":
            return atomic(dest, content, (expect_stat[0] - 1, expect_stat[1]))
        return "write failed"

    monkeypatch.setattr(rt, "_atomic_rewrite", fail)
    with pytest.raises(OSError):
        rt.apply_traces(str(tmp_path), path.name, TRACES)
    assert snapshot(path) == before
    assert not (tmp_path / ".retro.md.ob-lock").exists()
    assert not list(tmp_path.glob(".ob-*.tmp"))


def test_atomic_rename_failure(tmp_path, monkeypatch):
    import note_writer
    path = make_note(tmp_path)
    before = snapshot(path)

    def fail_rename(*args):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(note_writer.os, "rename", fail_rename)
    with pytest.raises(OSError, match="simulated rename failure"):
        rt.apply_traces(str(tmp_path), path.name, TRACES)
    assert snapshot(path) == before
    assert not (tmp_path / ".retro.md.ob-lock").exists()
    assert not list(tmp_path.glob(".ob-*.tmp"))


@pytest.mark.parametrize("note", ["", "  ", "Update 2026-10-29"])
def test_optional_memory_annotation(tmp_path, note):
    path = make_note(tmp_path, HEADER + "## Process Improvements\n- [ ] Remember.\n")
    rt.apply_traces(str(tmp_path), path.name, [
        {"item": 1, "kind": "memory", "stem": "remember", "note": note}])
    assert rt.parse_items(path.read_text())[0]["trace"] == {
        "kind": "memory", "stem": "remember", "note": note}


def test_path_containment(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    path = make_note(tmp_path)
    before = snapshot(path)
    with pytest.raises(rt.TraceError):
        rt.apply_traces(str(vault), str(path), TRACES)
    assert snapshot(path) == before


@pytest.mark.parametrize("text", ["no frontmatter", "---\ntype: x\n", "---\n# Heading\n---\n", None])
def test_unreadable(tmp_path, text):
    path = tmp_path / "missing.md" if text is None else make_note(tmp_path, text)
    assert rt.untraced_count(path) is None


def test_cli(tmp_path, monkeypatch, capsys):
    path = make_note(tmp_path)

    class CappedInput(io.StringIO):
        def read(self, size=-1):
            assert size == 1_000_001
            return super().read(size)

    monkeypatch.setattr("sys.stdin", CappedInput(json.dumps({"traces": TRACES})))
    assert rt.main(["apply", str(tmp_path), path.name]) == 0
    assert json.loads(capsys.readouterr().out)["untraced"] == 0
    assert rt.main(["untraced", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "0"
    assert rt.main(["items", str(path)]) == 0
    items = json.loads(capsys.readouterr().out)
    parsed = rt.parse_items(path.read_text())
    assert items == [{"item": i["item"], "line": i["start"] + 1,
                      "checked": i["checked"], "text": i["text"], "trace": i["trace"]} for i in parsed]
    assert rt.main(["untraced", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().out.strip() == "unreadable"
    assert rt.main(["items", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().err
    (tmp_path / ".retro.md.ob-lock").touch()
    monkeypatch.setattr("sys.stdin", io.StringIO('{"traces": []}'))
    assert rt.main(["apply", str(tmp_path), path.name]) == 1
    assert capsys.readouterr().err


@pytest.mark.parametrize("payload", ["{", "[]", "{}", '{"traces": null}', '{"traces": []}'])
def test_cli_bad_json_or_validation(tmp_path, monkeypatch, capsys, payload):
    path = make_note(tmp_path)
    before = snapshot(path)
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert rt.main(["apply", str(tmp_path), path.name]) == 2
    output = capsys.readouterr()
    assert output.err and not output.out
    assert snapshot(path) == before


@pytest.mark.parametrize("args", [[], ["unknown"], ["items"], ["apply", "vault"], ["untraced", "a", "b"]])
def test_cli_bad_usage(args, capsys):
    assert rt.main(args) == 2
    assert capsys.readouterr().err


BLOCK_FORMS = {
    "column0": "tracked_issues:\n- a/b#1\n- c/d#2\n",
    "blank-between": "tracked_issues:\n  - a/b#1\n\n  - c/d#2\n",
    "column0-blank-between": "tracked_issues:\n- a/b#1\n\n- c/d#2\n",
}


@pytest.mark.parametrize("form", BLOCK_FORMS)
def test_read_block_list_column0_and_blank_lines(form):
    text = HEADER.replace("tags:\n", BLOCK_FORMS[form] + "tags:\n") + BODY
    assert rt.read_tracked_issues(text) == ["a/b#1", "c/d#2"]


@pytest.mark.parametrize("form", BLOCK_FORMS)
@pytest.mark.parametrize("trailing_blank", [False, True])
def test_apply_block_list_leaves_no_orphans(tmp_path, form, trailing_blank):
    import vault_index

    field = BLOCK_FORMS[form] + ("\n" if trailing_blank else "")
    text = HEADER.replace("tags:\n", field + "tags:\n") + BODY
    path = make_note(tmp_path, text)
    result = rt.apply_traces(str(tmp_path), path.name, TRACES)
    assert result["tracked_issues"] == ["a/b#1", "c/d#2", "o/r#5"]
    written = path.read_text()
    fm_text = written.split("---\n")[1]
    assert 'tracked_issues: ["a/b#1", "c/d#2", "o/r#5"]\n' in fm_text
    assert "- a/b#1" not in fm_text and "- c/d#2" not in fm_text
    # Trailing blank lines belong to whatever follows and are kept.
    assert ("\n\ntags:" in fm_text) == trailing_blank
    assert split_frontmatter(split_lines_lf_crlf(written))[4] is None
    assert rt.read_tracked_issues(written) == ["a/b#1", "c/d#2", "o/r#5"]
    meta = vault_index._parse_note(str(path))
    assert meta is not None
    assert meta["tags"] == "claude/retro,claude/project/demo"


def test_cli_apply_oversized_stdin_writes_nothing(tmp_path, monkeypatch, capsys):
    path = make_note(tmp_path)
    before = snapshot(path)
    # Valid and applicable once truncated, so only the overflow check stops it.
    prefix = json.dumps({"traces": TRACES})
    payload = prefix + " " * (1_000_001 - len(prefix))
    assert len(payload) == 1_000_001
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert rt.main(["apply", str(tmp_path), path.name]) == 2
    output = capsys.readouterr()
    assert output.err and not output.out
    assert snapshot(path) == before


@pytest.mark.parametrize("line", ["- [ ] x → skipped:reason", "- [ ] x → skipped:"])
def test_skipped_needs_the_space(line):
    assert rt.parse_trace(line) is None


@pytest.mark.parametrize(("kind", "field", "build"), [
    ("skipped", "reason", lambda v: "skipped: " + v),
    ("memory", "stem", lambda v: f"memory `{v}`"),
    ("path", "path", lambda v: f"`{v}`"),
])
def test_value_length_boundary_300_vs_301(tmp_path, kind, field, build):
    value = "a" * 300
    if kind == "path":
        value = "d/" + "a" * 298
    entry = {"item": 1, "kind": kind, field: value}
    assert rt._render(entry) == build(value)
    entry[field] = value + "a"
    with pytest.raises(rt.TraceError):
        rt._render(entry)


def test_apply_on_crlf_note_rereads_every_trace(tmp_path):
    path = make_note(tmp_path, (HEADER + BODY).replace("\n", "\r\n"))
    rt.apply_traces(str(tmp_path), path.name, TRACES)
    items = rt.parse_items(path.read_bytes().decode())
    assert [i["trace"]["kind"] for i in items] == ["issue", "memory", "path", "skipped"]
