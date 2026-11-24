"""Verify and search the results of a real consolidation in a scratch HOME."""

import json
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
    assert [row["check"] for row in checks] == [
        "index-links-resolve", "no-duplicates", "all-indexed", "removed-equals-moved",
        "notes-lossless", "backups-present", "sizes",
    ]
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
    ("new", 'migrated_from_memory: "feedback_a"', "migrated_from_memory: other"),
    ("new", "status: active", "source_session_note: example"),
])
def test_note_drift(env, backup, capsys, action, old, new):
    run = json.loads((backup / "run.json").read_text())
    [note] = [env["vault"] / row["note"] for row in run["written"] if row["action"] == action]
    note.write_text(note.read_text().replace(old, new))
    assert_failures(backup, capsys, "notes-lossless")


def test_missing_backup(backup, capsys):
    (backup / "moved/project_c.md").unlink()
    assert_failures(backup, capsys, "backups-present")


def test_baseline_dangling(env):
    append_line(env["store"] / "MEMORY.md", "- [Gone](gone.md)")
    assert mc.verify(finish(env))[0]


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
