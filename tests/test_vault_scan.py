"""Tests for hooks/vault_scan.py — the Grep-tool fallback and the
frontmatter reader for /vault-ask and /vault-search (#375, #312).

Two layers, like tests/test_note_writer.py:

- Subprocess tests run ``python3 hooks/vault_scan.py ...`` exactly as the
  skills do, so argv parsing, exit codes and the stdout/stderr split are
  proven end to end.
- In-process tests call ``vault_scan.main()`` and the helpers directly so
  coverage.py sees the branches.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import vault_scan

VAULT_SCAN = Path(__file__).resolve().parent.parent / "hooks" / "vault_scan.py"


def _note(fm: str, body: str = "# Title\n\nBody text.\n") -> str:
    return f"---\n{fm}---\n{body}"


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "claude-sessions").mkdir(parents=True)
    (v / "claude-insights" / "sub").mkdir(parents=True)
    (v / "claude-sessions" / "s1.md").write_text(
        _note("type: claude-session\ndate: 2026-01-01\n", "# S1\n\nRedis caching here.\n"))
    (v / "claude-insights" / "i1.md").write_text(
        _note("type: claude-decision\ndate: 2026-02-02\n", "# I1\n\nnothing\n"))
    (v / "claude-insights" / "sub" / "deep.md").write_text(
        _note("type: claude-insight\n", "# Deep\n\nREDIS in a subfolder.\n"))
    (v / "claude-insights" / "notes.txt").write_text("redis but not markdown\n")
    return v


def _run(*args):
    return subprocess.run(
        [sys.executable, str(VAULT_SCAN), *map(str, args)],
        capture_output=True, text=True, timeout=60,
    )


def _main(capsys, *args):
    rc = vault_scan.main([str(a) for a in args])
    out, err = capsys.readouterr()
    return rc, out, err


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_grep_subprocess_content_match(vault):
    r = _run("grep", vault, "claude-sessions", "claude-insights", "--pattern", "Redis")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [str(vault / "claude-sessions" / "s1.md")]
    assert "vault_scan: 1 match(es)" in r.stderr


def test_grep_ignore_case_and_recursive(capsys, vault):
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions", "claude-insights",
                         "--pattern", "redis", "--ignore-case")
    assert rc == 0
    assert out.splitlines() == sorted([
        str(vault / "claude-insights" / "sub" / "deep.md"),
        str(vault / "claude-sessions" / "s1.md"),
    ])
    # notes.txt also contains "redis" but is not *.md
    assert "notes.txt" not in out


def test_grep_anchor_matches_per_line(capsys, vault):
    rc, out, _ = _main(capsys, "grep", vault, "claude-sessions", "claude-insights",
                       "--pattern", "^type:.*decision", "--ignore-case")
    assert rc == 0
    assert out.splitlines() == [str(vault / "claude-insights" / "i1.md")]


def test_grep_does_not_match_across_lines(capsys, vault):
    # ripgrep matches one line at a time. "# S1" and "Redis caching" sit on
    # different lines, so `S1\s+Redis` must not match; it would if the whole
    # file were searched as one string.
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions",
                         "--pattern", r"S1\s+Redis")
    assert rc == 0
    assert out == ""
    assert "0 match(es)" in err


def test_grep_crlf_line_end_does_not_break_dollar_anchor(capsys, tmp_path):
    v = tmp_path / "v"
    (v / "f").mkdir(parents=True)
    (v / "f" / "a.md").write_bytes(b"---\r\ntype: claude-insight\r\n---\r\nend here\r\n")
    rc, out, _ = _main(capsys, "grep", v, "f", "--pattern", "here$")
    assert rc == 0
    assert out.splitlines() == [str(v / "f" / "a.md")]


def test_grep_zero_matches_still_prints_summary(capsys, vault):
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions", "--pattern", "zzzz-none")
    assert rc == 0
    assert out == ""
    assert err.strip() == (
        "vault_scan: 0 match(es), 1 file(s) scanned, 0 skipped "
        "(too_large=0, unreadable=0, outside_vault=0, bad_frontmatter=0, symlinked_dirs=0, unreadable_dirs=0)"
    )


def test_grep_invalid_regex_exits_2(vault):
    r = _run("grep", vault, "claude-sessions", "--pattern", "(unclosed")
    assert r.returncode == 2
    assert r.stderr.startswith("ERROR: invalid regex")
    assert r.stdout == ""


def test_grep_requires_pattern_and_folder(capsys, vault):
    rc, _, err = _main(capsys, "grep", vault, "claude-sessions")
    assert rc == 2 and err.startswith("ERROR:")
    rc, _, err = _main(capsys, "grep", vault, "--pattern", "x")
    assert rc == 2 and err.startswith("ERROR:")


@pytest.mark.parametrize("folder", ["..", "/etc", "~", "", ".", "a/../..", ".obsidian"])
def test_grep_rejects_unsafe_folder(capsys, vault, folder):
    rc, out, err = _main(capsys, "grep", vault, folder, "--pattern", "x")
    assert rc == 2
    assert out == ""
    assert err.startswith("ERROR: invalid folder")


def test_grep_rejects_missing_folder(capsys, vault):
    rc, _, err = _main(capsys, "grep", vault, "no-such-folder", "--pattern", "x")
    assert rc == 2
    assert err.startswith("ERROR: folder not found")


@pytest.mark.parametrize("bad", ["", "relative/vault"])
def test_grep_rejects_bad_vault(capsys, bad):
    rc, _, err = _main(capsys, "grep", bad, "claude-sessions", "--pattern", "x")
    assert rc == 2
    assert err.startswith("ERROR: invalid vault path")


def test_grep_skips_symlinked_file_outside_vault(capsys, vault, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("Redis secret outside the vault\n")
    (vault / "claude-sessions" / "link.md").symlink_to(outside)
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions", "--pattern", "Redis")
    assert rc == 0
    assert "link.md" not in out
    assert out.splitlines() == [str(vault / "claude-sessions" / "s1.md")]
    assert "1 skipped (too_large=0, unreadable=0, outside_vault=1," in err


def test_grep_rejects_symlinked_folder_outside_vault(capsys, vault, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "x.md").write_text("Redis\n")
    (vault / "escape").symlink_to(outside, target_is_directory=True)
    rc, out, err = _main(capsys, "grep", vault, "escape", "--pattern", "Redis")
    assert rc == 2
    assert out == ""
    assert err.startswith("ERROR: folder resolves outside the vault")


def test_grep_does_not_descend_symlinked_subdir(capsys, vault, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "x.md").write_text("Redis\n")
    (vault / "claude-sessions" / "linkdir").symlink_to(outside, target_is_directory=True)
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions", "--pattern", "Redis")
    assert rc == 0
    assert "x.md" not in out
    # Not descended, and counted as a symlinked dir, not as an outside-vault
    # file. (The file containment check would also hide x.md; the counter
    # tells the two guards apart.)
    assert err.strip().endswith(
        "1 file(s) scanned, 1 skipped (too_large=0, unreadable=0, "
        "outside_vault=0, bad_frontmatter=0, symlinked_dirs=1, unreadable_dirs=0)"
    )


def test_grep_skips_oversized_file(capsys, vault, monkeypatch):
    monkeypatch.setattr(vault_scan, "MAX_FILE_BYTES", 10)
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions", "--pattern", "Redis")
    assert rc == 0
    assert out == ""
    assert "1 skipped (too_large=1, unreadable=0," in err


def test_grep_skips_unreadable_file(capsys, vault, monkeypatch):
    def boom(_path):
        raise OSError("nope")
    monkeypatch.setattr(vault_scan, "_read_text", boom)
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions", "--pattern", "Redis")
    assert rc == 0
    assert out == ""
    assert "0 file(s) scanned, 1 skipped (too_large=0, unreadable=1," in err


def test_grep_dedups_overlapping_folders(capsys, vault):
    rc, out, _ = _main(capsys, "grep", vault, "claude-insights", "claude-insights/sub",
                       "--pattern", "REDIS")
    assert rc == 0
    assert out.splitlines() == [str(vault / "claude-insights" / "sub" / "deep.md")]


def _deep_tag_note(tag_line: int, extra_fm: str = "") -> str:
    lines = ["type: claude-emerge", "date: 2026-04-24", "projects:"]
    while len(lines) < tag_line - 3:
        lines.append(f"  - p{len(lines)}")
    lines += ["tags:", "  - claude/emerge", "  - claude/topic/deepneedle"]
    fm = "\n".join(lines) + "\n" + extra_fm
    return _note(fm, "# Deep\n\nbody mentions claude/topic/bodyonly here\n")


def test_frontmatter_only_finds_deep_tag(capsys, tmp_path):
    v = tmp_path / "v"
    (v / "ins").mkdir(parents=True)
    note = v / "ins" / "deep.md"
    note.write_text(_deep_tag_note(300))
    idx = note.read_text().splitlines().index("  - claude/topic/deepneedle")
    assert idx > 290  # the tag really sits past line 290
    rc, out, _ = _main(capsys, "grep", v, "ins", "--pattern", "claude/topic/deepneedle",
                       "--frontmatter-only")
    assert rc == 0
    assert out.splitlines() == [str(note)]


def test_frontmatter_only_ignores_body(capsys, tmp_path):
    v = tmp_path / "v"
    (v / "ins").mkdir(parents=True)
    (v / "ins" / "deep.md").write_text(_deep_tag_note(50))
    rc, out, err = _main(capsys, "grep", v, "ins", "--pattern", "claude/topic/bodyonly",
                         "--frontmatter-only")
    assert rc == 0
    assert out == ""
    # Positive control: without the flag the body match is found.
    rc, out, _ = _main(capsys, "grep", v, "ins", "--pattern", "claude/topic/bodyonly")
    assert out.splitlines() == [str(v / "ins" / "deep.md")]


def test_frontmatter_only_skips_broken_fence(capsys, tmp_path):
    v = tmp_path / "v"
    (v / "ins").mkdir(parents=True)
    (v / "ins" / "ok.md").write_text(_note("tags:\n  - claude/topic/x\n"))
    (v / "ins" / "broken.md").write_text("---\ntags:\n  - claude/topic/x\n# no closing fence\n")
    (v / "ins" / "nofm.md").write_text("claude/topic/x but no frontmatter\n")
    rc, out, err = _main(capsys, "grep", v, "ins", "--pattern", "claude/topic/x",
                         "--frontmatter-only")
    assert rc == 0
    assert out.splitlines() == [str(v / "ins" / "ok.md")]
    # broken.md has a fence that does not close: skipped. nofm.md has no
    # frontmatter at all: scanned (it simply cannot match), not skipped.
    assert err.strip() == (
        "vault_scan: 1 match(es), 2 file(s) scanned, 1 skipped "
        "(too_large=0, unreadable=0, outside_vault=0, bad_frontmatter=1, symlinked_dirs=0, unreadable_dirs=0)"
    )


def test_grep_unexpected_error_exits_1(capsys, vault, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(vault_scan, "grep_files", boom)
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions", "--pattern", "x")
    assert rc == 1
    assert err.startswith("ERROR: unexpected error")


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def _deep_meta_note(fm_len: int = 460) -> str:
    lines = ["projects:"]
    while len(lines) < fm_len - 8:
        lines.append(f"  - p{len(lines)}")
    lines += [
        "type: claude-emerge",
        "date: 2026-04-24",
        'project: "obsidian-brain"',
        "session_id: abc-123",
        "source_session_note: '[[2026-04-24-parent-aa11]]'",
        "tags:",
        "  - claude/emerge",
        "  - claude/auto",
    ]
    return _note("\n".join(lines) + "\n", "# Emerge Title\n\n" + "word " * 100)


def test_meta_reads_fields_deep_in_frontmatter(tmp_path):
    v = tmp_path / "v"
    (v / "ins").mkdir(parents=True)
    note = v / "ins" / "2026-04-24-emerge.md"
    note.write_text(_deep_meta_note())
    text_lines = note.read_text().splitlines()
    assert text_lines.index("type: claude-emerge") > 440
    r = _run("meta", v, note)
    assert r.returncode == 0, r.stderr
    rows = [json.loads(line) for line in r.stdout.splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row == {
        "path": str(note),
        "date": "2026-04-24",
        "type": "claude-emerge",
        "project": "obsidian-brain",
        "session_id": "abc-123",
        "source_session_note": "[[2026-04-24-parent-aa11]]",
        "tags": ["claude/emerge", "claude/auto"],
        "title": "Emerge Title",
        "snippet": row["snippet"],
        "error": None,
    }
    assert row["snippet"].startswith("# Emerge Title word word")
    assert len(row["snippet"]) == 200


def test_meta_flow_tags_title_fallback_and_missing_fields(capsys, tmp_path):
    v = tmp_path / "v"
    (v / "ins").mkdir(parents=True)
    note = v / "ins" / "plain-note.md"
    note.write_text(_note('type: claude-insight\ntags: [claude/insight, "claude/topic/x"]\n',
                          "no heading here\n"))
    rc, out, _ = _main(capsys, "meta", v, note)
    assert rc == 0
    row = json.loads(out)
    assert row["title"] == "plain-note"
    assert row["tags"] == ["claude/insight", "claude/topic/x"]
    assert row["date"] is None and row["project"] is None
    assert row["snippet"] == "no heading here"
    assert row["error"] is None


def test_meta_empty_tags_is_empty_list(capsys, tmp_path):
    v = tmp_path / "v"
    (v / "ins").mkdir(parents=True)
    note = v / "ins" / "n.md"
    note.write_text(_note("type: claude-insight\ntags:\ndate: 2026-01-01\n"))
    rc, out, _ = _main(capsys, "meta", v, note)
    row = json.loads(out)
    assert row["tags"] == []
    assert row["date"] == "2026-01-01"


def test_meta_bad_files_do_not_abort_the_rest(capsys, vault, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(_note("type: claude-insight\n"))
    good = vault / "claude-sessions" / "s1.md"
    broken = vault / "claude-sessions" / "broken.md"
    broken.write_text("---\ntype: claude-insight\nSECRET prose line\n")
    link = vault / "claude-sessions" / "link.md"
    link.symlink_to(outside)
    txt = vault / "claude-insights" / "notes.txt"
    rc, out, _ = _main(capsys, "meta", vault, outside, good, vault / "claude-sessions" / "gone.md",
                       broken, link, txt, "claude-insights/i1.md")
    assert rc == 0
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["path"] for r in rows] == [
        str(outside), str(good), str(vault / "claude-sessions" / "gone.md"),
        str(broken), str(link), str(txt), "claude-insights/i1.md",
    ]
    err = {r["path"]: r["error"] for r in rows}
    assert err[str(outside)] == "outside the vault"
    assert err[str(link)] == "outside the vault"
    assert err[str(good)] is None
    assert err[str(vault / "claude-sessions" / "gone.md")] == "not found"
    assert err[str(txt)] == "not a .md file"
    # The parse failure is reported as a category, never as note text.
    assert err[str(broken)] == "unparsable frontmatter: no_closing_fence"
    assert "SECRET" not in out
    # A relative path resolves against the vault.
    assert rows[-1]["type"] == "claude-decision"
    for r in rows:
        if r["error"]:
            assert r["type"] is None and r["tags"] is None and r["title"] is None


def test_meta_oversized_and_unreadable(capsys, vault, monkeypatch):
    good = vault / "claude-sessions" / "s1.md"
    monkeypatch.setattr(vault_scan, "MAX_FILE_BYTES", 10)
    rc, out, _ = _main(capsys, "meta", vault, good)
    assert json.loads(out)["error"] == "file too large"
    monkeypatch.setattr(vault_scan, "MAX_FILE_BYTES", 5 * 1024 * 1024)

    def boom(_path):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(vault_scan, "_read_text", boom)
    rc, out, _ = _main(capsys, "meta", vault, good)
    assert rc == 0
    assert json.loads(out)["error"] == "unreadable: Permission denied"


def test_meta_requires_files_and_valid_vault(capsys, vault):
    rc, _, err = _main(capsys, "meta", vault)
    assert rc == 2 and err.startswith("ERROR:")
    rc, _, err = _main(capsys, "meta", "rel", "x.md")
    assert rc == 2 and err.startswith("ERROR: invalid vault path")


def test_unknown_command_exits_2(capsys):
    rc, _, err = _main(capsys, "frobnicate")
    assert rc == 2 and err.startswith("ERROR:")
    rc, _, err = _main(capsys)
    assert rc == 2 and err.startswith("ERROR:")


def test_help_exits_0(capsys):
    rc, out, _ = _main(capsys, "--help")
    assert rc == 0
    assert "grep" in out and "meta" in out


def test_meta_subprocess_error_path(tmp_path):
    r = _run("meta", tmp_path / "missing-vault", "x.md")
    assert r.returncode == 2
    assert r.stderr.startswith("ERROR: invalid vault path")


def test_non_utf8_bytes_are_replaced_not_fatal(capsys, tmp_path):
    v = tmp_path / "v"
    (v / "f").mkdir(parents=True)
    (v / "f" / "a.md").write_bytes(b"---\ntype: claude-insight\n---\n# T\n\xff\xfe Redis\n")
    rc, out, _ = _main(capsys, "grep", v, "f", "--pattern", "Redis")
    assert rc == 0 and out.strip() == str(v / "f" / "a.md")
    rc, out, _ = _main(capsys, "meta", v, v / "f" / "a.md")
    assert json.loads(out)["error"] is None


def test_module_has_no_cwd_dependency(tmp_path, vault):
    # Run from an unrelated cwd: the CLI must not depend on being inside the repo.
    r = subprocess.run(
        [sys.executable, str(VAULT_SCAN), "grep", str(vault), "claude-sessions",
         "--pattern", "Redis"],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(vault / "claude-sessions" / "s1.md")
    assert os.path.isabs(r.stdout.strip())


def test_parse_fields_shapes():
    fm = [
        "type: claude-insight\n",
        "tags:\n",
        "  - claude/a\n",
        "\n",
        "  - 'claude/b'\n",
        "nested:\n",
        "  inner: not-top-level\n",
        "type: second-value-wins\n",
        "plain line without colon\n",
    ]
    fields = vault_scan._parse_fields(fm)
    assert fields["tags"] == ["claude/a", "claude/b"]
    # Last wins, like vault_index._parse_note_detailed (the index behind the
    # Step 3 fast path), so the fast path and the fallback agree.
    assert fields["type"] == "second-value-wins"
    assert "inner" not in fields
    assert vault_scan._parse_fields(["tags: claude/solo\n"])["tags"] == ["claude/solo"]
    # An unclosed quote keeps the text, minus the stray quote.
    assert vault_scan._scalar('"open # not a comment') == "open # not a comment"


def test_grep_invalid_regex_in_process(capsys, vault):
    rc, out, err = _main(capsys, "grep", vault, "claude-sessions", "--pattern", "[")
    assert rc == 2 and out == ""
    assert err.startswith("ERROR: invalid regex")


def test_meta_block_tags_in_process(capsys, tmp_path):
    v = tmp_path / "v"
    (v / "ins").mkdir(parents=True)
    note = v / "ins" / "n.md"
    note.write_text(_deep_meta_note(60))
    rc, out, _ = _main(capsys, "meta", v, note)
    row = json.loads(out)
    assert row["tags"] == ["claude/emerge", "claude/auto"]
    assert row["type"] == "claude-emerge"


def test_is_inside_handles_resolve_errors(monkeypatch, tmp_path):
    def boom(self, *a, **k):
        raise RuntimeError("symlink loop")
    monkeypatch.setattr(Path, "resolve", boom)
    assert vault_scan._is_inside(tmp_path / "x.md", tmp_path) is False


# ---------------------------------------------------------------------------
# Review fix wave (#375, #312)
# ---------------------------------------------------------------------------


def test_pattern_equals_form_accepts_leading_dash(vault):
    (vault / "claude-sessions" / "d.md").write_text(
        _note("type: claude-session\n", "# D\n\nran the tool with --no-verify\n"))
    r = _run("grep", vault, "claude-sessions", "--pattern=--no-verify")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [str(vault / "claude-sessions" / "d.md")]
    # Control: the space form is what broke; argparse reads the value as a flag.
    r = _run("grep", vault, "claude-sessions", "--pattern", "--no-verify")
    assert r.returncode == 2
    assert "expected one argument" in r.stderr


def test_import_failure_prints_error_not_traceback(tmp_path):
    # vault_scan.py alone, without frontmatter.py / note_writer.py beside it.
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "vault_scan.py").write_text(VAULT_SCAN.read_text())
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run(
        [sys.executable, str(lone / "vault_scan.py"), "grep", str(tmp_path), "x",
         "--pattern=x"],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path), env=env,
    )
    assert r.returncode == 1
    assert r.stdout == ""
    assert r.stderr.startswith("ERROR: cannot import vault_scan dependencies:"), r.stderr
    assert "Traceback" not in r.stderr


def test_skip_counter_outside_vault_and_bad_frontmatter(capsys, vault, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(_note("tags:\n  - claude/topic/x\n"))
    (vault / "claude-sessions" / "link.md").symlink_to(outside)
    (vault / "claude-sessions" / "broken.md").write_text("---\ntags:\n  - x\n# no fence\n")
    rc, _, err = _main(capsys, "grep", vault, "claude-sessions", "--pattern", "zzz",
                       "--frontmatter-only")
    assert rc == 0
    assert err.strip() == (
        "vault_scan: 0 match(es), 1 file(s) scanned, 2 skipped "
        "(too_large=0, unreadable=0, outside_vault=1, bad_frontmatter=1, symlinked_dirs=0, unreadable_dirs=0)"
    )


def test_symlinked_dir_inside_vault_is_counted_once(capsys, vault):
    # A symlinked subdir pointing back inside the vault is still not descended,
    # and overlapping folder arguments do not count it twice.
    (vault / "claude-insights" / "loop").symlink_to(vault / "claude-sessions",
                                                    target_is_directory=True)
    rc, out, err = _main(capsys, "grep", vault, "claude-insights", "claude-insights",
                         "--pattern", "Redis")
    assert rc == 0
    assert out == ""
    assert "symlinked_dirs=1, unreadable_dirs=0)" in err
    assert ", 1 skipped (" in err


def _meta_row(capsys, tmp_path, fm, body="# T\n\nb\n"):
    v = tmp_path / "v"
    (v / "ins").mkdir(parents=True, exist_ok=True)
    note = v / "ins" / "n.md"
    note.write_text(_note(fm, body))
    rc, out, err = _main(capsys, "meta", v, note)
    assert rc == 0
    return json.loads(out), err


def test_meta_strips_inline_comment_from_unquoted_scalars(capsys, tmp_path):
    row, _ = _meta_row(capsys, tmp_path, (
        "date: 2026-01-01 # written by hand\n"
        'project: "has # inside"\n'
        "session_id: 'a # b' # trailing\n"
        "type: claude-insight#not-a-comment\n"
        "source_session_note: # only a comment\n"
        "tags:\n  - claude/a # why\n"
    ))
    assert row["date"] == "2026-01-01"
    assert row["project"] == "has # inside"
    assert row["session_id"] == "a # b"
    assert row["type"] == "claude-insight#not-a-comment"
    assert row["tags"] == ["claude/a"]
    assert row["source_session_note"] is None


def test_meta_flow_tags_are_quote_aware(capsys, tmp_path):
    row, _ = _meta_row(capsys, tmp_path, "tags: [a, \"b, c\", 'd,e', f] # note\n")
    assert row["tags"] == ["a", "b, c", "d,e", "f"]
    row, _ = _meta_row(capsys, tmp_path, "tags: [a, \"b #c\"]\n")
    assert row["tags"] == ["a", "b #c"]
    row, _ = _meta_row(capsys, tmp_path, "tags: []\n")
    assert row["tags"] == []


def test_meta_warns_once_when_obsidian_utils_missing(capsys, vault, monkeypatch):
    monkeypatch.setitem(sys.modules, "obsidian_utils", None)  # import -> ImportError
    monkeypatch.setattr(vault_scan, "_UTILS_WARNED", False)
    for name in ("b1.md", "b2.md"):
        (vault / "claude-sessions" / name).write_text("---\ntype: x\nprose line\n")
    rc, out, err = _main(capsys, "meta", vault, vault / "claude-sessions" / "b1.md",
                         vault / "claude-sessions" / "b2.md")
    assert rc == 0
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["error"] for r in rows] == ["unparsable frontmatter: unknown"] * 2
    assert err.count("vault_scan: obsidian_utils unavailable: ") == 1


def test_meta_parse_failure_bug_is_not_swallowed(monkeypatch):
    # Only ImportError degrades; a bug inside the categorizer still surfaces.
    import obsidian_utils

    def boom(_reason):
        raise ValueError("bug")
    monkeypatch.setattr(obsidian_utils, "_describe_note_parse_failure", boom)
    with pytest.raises(ValueError):
        vault_scan._describe_parse_failure("x")

    # An import that fails with something other than ImportError is a bug too.
    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("broken module")
    monkeypatch.setitem(sys.modules, "obsidian_utils", _Broken())
    with pytest.raises(RuntimeError):
        vault_scan._describe_parse_failure("x")


def test_meta_duplicate_keys_last_wins(capsys, tmp_path):
    row, _ = _meta_row(capsys, tmp_path, "type: claude-session\ntype: claude-insight\n")
    assert row["type"] == "claude-insight"


def test_meta_title_skips_h2_before_h1(capsys, tmp_path):
    row, _ = _meta_row(capsys, tmp_path, "type: claude-insight\n",
                       "## Context\n\ntext\n\n# Real Title\n")
    assert row["title"] == "Real Title"


# ---------------------------------------------------------------------------
# unreadable directories (X-001) and YAML quote escapes (X-002)
# ---------------------------------------------------------------------------

_NOT_ROOT = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions",
)


@pytest.fixture
def locked(request):
    paths: list[Path] = []
    yield paths
    for p in paths:
        p.chmod(0o755)


@_NOT_ROOT
def test_grep_counts_unreadable_subdir(capsys, vault, locked):
    sub = vault / "claude-insights" / "sub"
    sub.chmod(0)
    locked.append(sub)
    rc, out, err = _main(capsys, "grep", vault, "claude-insights", "--pattern", "nomatch")
    assert rc == 0
    assert "unreadable_dirs=1)" in err
    assert ", 1 skipped (" in err


@_NOT_ROOT
def test_grep_counts_unreadable_passed_folder(capsys, vault, locked):
    folder = vault / "claude-insights"
    folder.chmod(0)
    locked.append(folder)
    rc, out, err = _main(capsys, "grep", vault, "claude-insights", "--pattern", "nomatch")
    assert rc == 0
    assert out == ""
    assert "0 file(s) scanned, 1 skipped (" in err
    assert "unreadable_dirs=1)" in err


def test_scalar_single_quote_escape():
    assert vault_scan._scalar("'Bob''s app'") == "Bob's app"
    assert vault_scan._scalar("'it''s' # note") == "it's"
    assert vault_scan._scalar("'a # b'") == "a # b"


def test_scalar_double_quote_escape():
    assert vault_scan._scalar('"[[a \\"q\\" b]]"') == '[[a "q" b]]'
    assert vault_scan._scalar('"back\\\\slash"') == "back\\slash"
    assert vault_scan._scalar('"x \\" # still inside" # c') == 'x " # still inside'


def test_flow_items_quote_escapes():
    assert vault_scan._flow_items("'it''s', b") == ["it's", "b"]
    assert vault_scan._flow_items('"a \\"b, c\\"", d') == ['a "b, c"', "d"]
    assert vault_scan._flow_items('"a\\\\", e') == ["a\\", "e"]
    assert vault_scan._flow_items("'a'',b', c") == ["a',b", "c"]


def test_meta_quote_escapes_end_to_end(capsys, tmp_path):
    row, _ = _meta_row(capsys, tmp_path, (
        "project: 'Bob''s app'\n"
        'source_session_note: "[[a \\"q\\" b]]"\n'
        "tags: ['it''s', \"a \\\"b\\\"\"]\n"
    ))
    assert row["project"] == "Bob's app"
    assert row["source_session_note"] == '[[a "q" b]]'
    assert row["tags"] == ["it's", 'a "b"']
