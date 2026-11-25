"""Verify and search the results of a real consolidation in a scratch HOME."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import memory_consolidate as mc
from test_memory_consolidate_apply import TARGET, cli, env, marked_notes, write_plan


def append_line(path, line):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def finish(env):
    append_line(env["store"] / "feedback_a.md", "Chrysoprase.")
    assert mc.main(["apply", "--plan", str(write_plan(env)), "--store", str(env["store"]),
                    "--vault", str(env["vault"]), "--project", "proj",
                    "--insights-folder", "claude-insights", "--backup-root", str(env["backups"]),
                    "--apply"]) == 0
    [backup] = env["backups"].iterdir()
    return backup


@pytest.fixture
def backup(env):
    return finish(env)


def assert_failures(backup, capsys, *names):
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == "verify: FAILED"
    assert {line.split(":")[0][5:] for line in lines if line.startswith("FAIL ")} == set(names)


def test_clean_run(env, backup, capsys):
    # Verify sizes come from disk even after a harmless post-apply edit.
    append_line(env["store"] / "MEMORY.md", "Résumé")
    ok, checks = mc.verify(backup, env["store"])
    assert ok
    assert [row["check"] for row in checks] == list(mc.VERIFY_CHECKS[1:])
    sizes = []
    for folder in (backup / "full-copy", env["store"]):
        data = (folder / "MEMORY.md").read_bytes()
        sizes.append(f"{len(data)} bytes, {len(data.decode())} chars, {len(data.splitlines())} lines")
    assert checks[-1]["detail"] == f"before: {sizes[0]}; after: {sizes[1]}"
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"store: {env['store']}", *(f"PASS {c['check']}: {c['detail']}" for c in checks), "verify: ok"]


def test_dangling(env, backup, capsys):
    append_line(env["store"] / "MEMORY.md", "- [Gone](gone.md)")
    assert_failures(backup, capsys, "index-links-resolve")


def test_duplicate(env, backup, capsys):
    append_line(env["store"] / "MEMORY.md", "- [Again](stay.md)")
    assert_failures(backup, capsys, "no-duplicates")


def test_orphan(env, backup, capsys):
    (env["store"] / "extra.md").write_text("Extra")
    assert_failures(backup, capsys, "all-indexed")


def test_unexpected_removal(env, backup, capsys):
    index = env["store"] / "MEMORY.md"
    index.write_text("".join(line for line in index.read_text().splitlines(True) if "stay.md" not in line))
    # Losing the staying entry's only reference also makes it an orphan.
    assert_failures(backup, capsys, "removed-equals-moved", "all-indexed")


def test_moved_entry_still_referenced(env, backup, capsys):
    append_line(env["store"] / "MEMORY.md", "See also [[feedback_a]].")
    assert_failures(backup, capsys, "removed-equals-moved")


@pytest.mark.parametrize(("action", "old", "new"), [
    ("new", "Chrysoprase.", "Chrysoprase!"),
    ("append", "Beta body text.", "Beta body text!"),
    ("new", "\n## Summary\n\nAlpha description", "\n## Summary\n\nAlphx description"),
    ("append", "\n### Summary\n\nBeta description", "\n### Summary\n\nBetx description"),
    ("new", 'migrated_from_memory: "feedback_a"', "migrated_from_memory: other"),
    ("new", "status: active", "source_session_note: example"),
])
def test_note_drift(env, backup, capsys, action, old, new):
    run = json.loads((backup / "run.json").read_text())
    [note] = [env["vault"] / row["note"] for row in run["written"] if row["action"] == action]
    text = note.read_text()
    assert old in text
    note.write_text(text.replace(old, new, 1))
    assert_failures(backup, capsys, "notes-lossless")


def test_missing_backup(backup, capsys):
    (backup / "moved/project_c.md").unlink()
    assert_failures(backup, capsys, "backups-present")


def test_baseline_dangling(env):
    append_line(env["store"] / "MEMORY.md", "- [Gone](gone.md)")
    assert mc.verify(finish(env))[0]


def test_baseline_duplicate(env):
    append_line(env["store"] / "MEMORY.md", "- [Again](stay.md)")
    assert mc.verify(finish(env))[0]


def test_baseline_orphan(env):
    (env["store"] / "extra.md").write_text("Extra")
    assert mc.verify(finish(env))[0]


def test_baseline_does_not_hide_new_drift(env, capsys):
    """The baseline only excuses what was already wrong before the run."""
    append_line(env["store"] / "MEMORY.md", "- [Again](stay.md)")
    backup = finish(env)
    (env["store"] / "extra.md").write_text("Extra")
    assert_failures(backup, capsys, "all-indexed")


def test_partial_move_verifies_clean(env, monkeypatch, capsys):
    """An original refused during the move stays live; its note still matches it (X-008)."""
    edited = (env["store"] / "feedback_b.md").read_text().replace("Beta body text.", "Edited later.")

    def edit(name):
        if name == "backup":  # after the backup copy, before the move
            (env["store"] / "feedback_b.md").write_text(edited, encoding="utf-8")

    monkeypatch.setattr(mc, "_checkpoint", edit)
    append_line(env["store"] / "feedback_a.md", "Chrysoprase.")
    assert mc.main(["apply", "--plan", str(write_plan(env)), "--store", str(env["store"]),
                    "--vault", str(env["vault"]), "--project", "proj", "--insights-folder", "claude-insights",
                    "--backup-root", str(env["backups"]), "--apply"]) == 1
    [backup] = env["backups"].iterdir()
    assert (env["store"] / "feedback_b.md").read_text() == edited
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "PASS removed-equals-moved: unexpected: []; missing: []" in lines
    assert "PASS notes-lossless: ok" in lines


def test_missing_backup(backup, capsys):
    (backup / "moved/project_c.md").unlink()
    assert_failures(backup, capsys, "backups-present")


def test_missing_full_copy_entry(backup, capsys):
    (backup / "full-copy/stay.md").unlink()
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 1
    out = capsys.readouterr().out
    assert "FAIL backups-present: missing full-copy/stay.md" in out.splitlines()


def test_changed_full_copy_entry(backup, capsys):
    (backup / "full-copy/stay.md").write_text("tampered")
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 1
    assert "FAIL backups-present: changed full-copy/stay.md" in capsys.readouterr().out.splitlines()


def strip_file_list(backup):
    (backup / "manifest.json").unlink()


def test_missing_manifest_fails(backup, capsys):
    strip_file_list(backup)
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 1
    assert "FAIL backups-present: missing manifest.json" in capsys.readouterr().out.splitlines()


def test_missing_manifest_and_full_copy_entry_fails(backup, capsys):
    strip_file_list(backup)
    (backup / "full-copy/stay.md").unlink()
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 1
    assert "FAIL backups-present: missing manifest.json" in capsys.readouterr().out.splitlines()


def with_symlink(env):
    os.symlink("stay.md", env["store"] / "ref.txt")  # a relative link inside the store
    return finish(env)


def test_manifest_lists_the_symlink(env):
    backup = with_symlink(env)
    rows = json.loads((backup / "manifest.json").read_text())["files"]
    assert dict(name="ref.txt", kind="symlink", target="stay.md") in rows
    assert os.readlink(backup / "full-copy/ref.txt") == "stay.md"
    assert mc.verify(backup)[0]


def test_deleted_manifest_symlink_fails_backups_present(env, capsys):
    backup = with_symlink(env)
    (backup / "full-copy/ref.txt").unlink()
    assert_failures(backup, capsys, "backups-present")
    assert mc.verify(backup)[1][-2]["detail"] == "missing full-copy/ref.txt"


def test_retargeted_manifest_symlink_fails_backups_present(env, capsys):
    backup = with_symlink(env)
    (backup / "full-copy/ref.txt").unlink()
    os.symlink("feedback_a.md", backup / "full-copy/ref.txt")
    assert_failures(backup, capsys, "backups-present")
    assert mc.verify(backup)[1][-2]["detail"] == "changed full-copy/ref.txt"


def test_manifest_has_its_own_size_cap(backup, monkeypatch):
    monkeypatch.setattr(mc, "_MANIFEST_LIMIT", 10)
    with pytest.raises(mc.ApplyError, match="cannot read manifest.json"):
        mc.verify(backup)


@pytest.mark.parametrize("files", [False, None, {}, [], "x"])
def test_malformed_manifest_files_is_an_error(backup, files):
    """A manifest whose file list is not a non-empty list stops verify; it never reads as empty."""
    data = json.loads((backup / "manifest.json").read_text())
    data["files"] = files
    (backup / "manifest.json").write_text(json.dumps(data))
    (backup / "full-copy/stay.md").unlink()
    with pytest.raises(mc.ApplyError, match="cannot read manifest.json"):
        mc.verify(backup)


def test_still_in_store(env, backup, capsys):
    """An original copied back into the store fails backups-present (and is an orphan)."""
    (env["store"] / "project_c.md").write_text((backup / "moved/project_c.md").read_text())
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "FAIL backups-present: still in store: project_c.md" in out
    assert {l.split(":")[0] for l in out if l.startswith("FAIL ")} == {"FAIL backups-present", "FAIL all-indexed"}


@pytest.mark.parametrize("with_list", [True, False], ids=["file-list", "old-run-json"])
def test_missing_full_copy_index(backup, capsys, with_list):
    if not with_list:
        strip_file_list(backup)
    (backup / "full-copy/MEMORY.md").unlink()
    capsys.readouterr()
    assert mc.main(["verify", "--backup", str(backup)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert "FAIL removed-equals-moved: missing full-copy/MEMORY.md" in out
    [line] = [l for l in out if l.startswith("FAIL backups-present")]
    assert "missing full-copy/MEMORY.md" in line
    assert "PASS sizes: before: unavailable; after: " in "\n".join(out)


def test_foreign_store(env, backup, capsys):
    other = env["tmp"] / ".claude/projects/-other/memory"
    other.mkdir(parents=True)
    (other / "MEMORY.md").touch()
    assert mc.main(["verify", "--backup", str(backup), "--store", str(other)]) == 2
    assert f"error: run.json is for {env['store']}" in capsys.readouterr().err


def test_missing_run(env, capsys):
    assert mc.main(["verify", "--backup", str(env["backups"])]) == 2
    assert "error: cannot read run.json" in capsys.readouterr().err


def test_unfinished_run(backup, capsys):
    path = backup / "run.json"
    data = json.loads(path.read_text())
    data["state"] = "moved"
    path.write_text(json.dumps(data))
    assert_failures(backup, capsys, "state")


def run_smoke(env, query, *extra):
    return cli(env, "smoke", "--db", str(env["tmp"] / "scratch.db"), "--vault", str(env["vault"]),
               "--query", query, *extra)


@pytest.mark.parametrize("expect_missing", [False, True], ids=["moved-note", "missing-note"])
def test_smoke_expected_notes(env, backup, expect_missing):
    [note] = marked_notes(env, "feedback_a")
    path = note.relative_to(env["vault"]).as_posix()
    args = ["--expect", f"./{path}"]
    lines = ["smoke: 1 hit(s) for chrysoprase", f"HIT {path}"]
    if expect_missing:
        args.extend(["--expect", TARGET])
        lines.append(f"MISSING {TARGET}")
    result = run_smoke(env, "chrysoprase", *args)
    assert result.returncode == int(expect_missing), result.stderr
    assert result.stdout.splitlines() == lines


def test_smoke_no_hits(env, backup):
    result = run_smoke(env, "absentwordxyz")
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines() == ["smoke: 0 hit(s) for absentwordxyz"]


def test_smoke_empty_vault(env):
    result = cli(env, "smoke", "--db", str(env["tmp"] / "scratch.db"), "--vault", "", "--query", "x")
    assert result.returncode == 2
    assert "error: vault is not a directory" in result.stderr
