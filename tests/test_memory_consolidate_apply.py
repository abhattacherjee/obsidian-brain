"""apply: dry run, --apply and --resume for /memory-consolidate (#309).

HOME, the store, the vault and the backup root all sit under tmp_path.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "hooks"))

import memory_consolidate as mc
import note_writer
from memory_consolidate import audit, index_edit, main
from obsidian_utils import gather_session_evidence

SCRIPT = _ROOT / "hooks" / "memory_consolidate.py"
TZ = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=TZ)
STAMP = "20261124-120000"
SID = "11111111-2222-3333-4444-555555555555"

INDEX = (
    "# Memory index\n"
    "\n"
    "## Feedback\n"
    "- [Alpha lesson](feedback_a.md) — first\n"
    "- [Beta lesson](feedback_b.md) — second\n"
    "- [Stays](stay.md) — kept\n"
    "- [Old project](project_c.md) — stale\n"
    "\n"
    "Footer prose.\n"
)
ENTRIES = {
    "feedback_a": ("Alpha", "Alpha description", "Line one of alpha.\n\n## A heading in the body\n- item [[x]]\n"),
    "feedback_b": ("Beta", "Beta description", "Beta body text."),
    "project_c": ("Gamma", "Stale project", "Project state that git now holds."),
    "stay": ("Stay", "A rule that stays", "Keep me."),
}
TARGET = "claude-insights/2026-11-01-existing-0d18.md"
TARGET_TEXT = "---\ntype: claude-insight\nproject: proj\n---\n\n# Existing\n\nOriginal body.\n"


def entry_text(name, desc, body, mtype="feedback"):
    return f"---\nname: {name}\ndescription: {desc}\ntype: {mtype}\n---\n\n{body}"


def _no_live_config(*args, **kwargs):
    raise AssertionError("a test reached load_config(): it would read the live vault config")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # In-process runs must pass --vault and --insights-folder; subprocesses get HOME=tmp_path.
    monkeypatch.setattr(mc, "load_config", _no_live_config)
    store = tmp_path / ".claude/projects/-x-proj/memory"
    store.mkdir(parents=True)
    (store / "MEMORY.md").write_text(INDEX, encoding="utf-8")
    for slug, parts in ENTRIES.items():
        (store / f"{slug}.md").write_text(entry_text(*parts), encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    (vault / TARGET).write_text(TARGET_TEXT, encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    return dict(tmp=tmp_path, store=store, vault=vault, backups=backups)


ITEMS = [
    dict(slug="feedback_a", action="new", type="claude-error-fix", topic="testing"),
    dict(slug="feedback_b", action="append", target=TARGET),
    dict(slug="project_c", action="retire", reason="PR merged; git holds it"),
]


def write_plan(env, items=ITEMS, name="plan.json"):
    path = env["tmp"] / name
    path.write_text(json.dumps(dict(version=1, store=str(env["store"]), items=items)), encoding="utf-8")
    return path


def run_apply(env, *extra, plan=None, now=NOW, out=None):
    """In-process apply, the same call main() makes."""
    lines = [] if out is None else out
    code = mc.apply(plan or write_plan(env), env["store"], env["vault"], "claude-insights", "proj",
                    env["backups"], "--apply" in extra, now, out=lines.append)
    return code, lines


def cli(env, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
        env={**os.environ, "HOME": str(env["tmp"])}, timeout=60,
    )


def cli_apply(env, plan, *extra):
    return cli(env, "apply", "--plan", str(plan), "--store", str(env["store"]), "--vault", str(env["vault"]),
               "--insights-folder", "claude-insights", "--project", "proj",
               "--backup-root", str(env["backups"]), *extra)


def tree_hash(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def trees(env):
    return [tree_hash(env[k]) for k in ("vault", "store", "backups")]


def backup_dir(env, stamp=STAMP):
    return env["backups"] / f"memory-moved-{stamp}--x-proj"


def marked_notes(env, slug):
    return [p for p in (env["vault"] / "claude-insights").rglob("*.md")
            if f'migrated_from_memory: "{slug}"' in p.read_text()]


def assert_finished(env):
    """No duplicate note or section, no dangling line, one pointer line."""
    assert len(marked_notes(env, "feedback_a")) == 1
    target = (env["vault"] / TARGET).read_text()
    assert target.count("<!-- migrated_from_memory: feedback_b -->") == 1
    assert target.startswith(TARGET_TEXT)
    report = audit(env["store"])
    assert report["dangling"] == []
    index = (env["store"] / "MEMORY.md").read_text()
    assert index.count("- Moved to vault (") == 1
    for slug in ("feedback_a", "feedback_b", "project_c"):
        assert not (env["store"] / f"{slug}.md").exists()
        assert f"{slug}.md" not in index
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert run["state"] == "done"
    assert sorted(run["moved"]) == ["feedback_a", "feedback_b", "project_c"]
    return run


# --- dry run -----------------------------------------------------------------

def test_dry_run_touches_nothing(env):
    plan = write_plan(env)
    before = trees(env)
    result = cli_apply(env, plan)
    assert result.returncode == 0, result.stderr
    assert trees(env) == before
    out = result.stdout.splitlines()
    assert out[0] == f"store: {env['store']}"
    assert re.fullmatch(f"backup: {re.escape(str(env['backups']))}/memory-moved-\\d{{8}}-\\d{{6}}--x-proj", out[1])
    assert any(line.startswith("NEW feedback_a -> claude-insights/") and line.endswith("-alpha-lesson-"
               + hashlib.sha1(b"feedback_a").hexdigest()[:4] + ".md") for line in out)
    assert any(line.startswith(f"APPEND feedback_b -> {TARGET} (+") for line in out)
    assert "RETIRE project_c: PR merged; git holds it" in out
    assert "DROP - [Alpha lesson](feedback_a.md) — first" in out
    assert out[-1] == "dry run: nothing written; add --apply to write"


def test_dry_run_in_process_also_leaves_trees(env):
    before = trees(env)
    code, out = run_apply(env)
    assert code == 0
    assert trees(env) == before


# --- full apply ----------------------------------------------------------------

@pytest.mark.parametrize(("link", "name", "expected"), [
    ("adversarial + heuristic", "adversarial-fixture-with-heuristic", "adversarial fixture with heuristic"),
    ("makedirs mode is leaf-only", "makedirs-leaf", "makedirs mode is leaf-only"),
    ("Leaf-only", "makedirs-leaf", "Leaf-only"),
    ("", "foo_bar_baz", "foo bar baz"),
    ("", "feedback_differential_oracle_for_grammar_code", "differential oracle for grammar code"),
    ("oracle", "technical-db-access-via-connect", "db access via connect"),
    ("", "", "Desc here"),
])
def test_title(link, name, expected):
    slug = "feedback_adversarial_fixture_with_heuristic"
    index = f"- [{link}]({slug}.md)" if link else ""
    assert mc._title(index, dict(slug=slug, name=name, description="Desc here")) == expected


@pytest.mark.parametrize("target", [
    "{slug}.md", "./{slug}.md", "<{slug}.md>", '{slug}.md "A title"', "{slug}.md 'A title'",
    '<./{slug}.md> "A title"', " {slug}.md ",
], ids=["plain", "dot-slash", "angle", "double-quoted-title", "single-quoted-title", "angle-title", "spaces"])
def test_title_reads_the_label_of_every_link_spelling(target):
    slug = "feedback_x"
    index = f"- [Own label here]({target.format(slug=slug)})"
    entry = dict(slug=slug, name="x", description="Desc here")
    assert mc._title(index, entry) == "Own label here"


def test_title_ignores_links_to_other_files():
    slug = "feedback_x"
    entry = dict(slug=slug, name="x", description="Desc here")
    for index in ("- [Other label](other.md)", f"- [Other label](dir/{slug}.md)",
                  f"- [Other label](https://example.com/{slug}.md)", f"- [Other label](x{slug}.md)"):
        assert mc._title(index, entry) == "x"


def test_grouped_line_uses_longer_name_for_note_filename(env):
    slug = "feedback_adversarial_fixture_with_heuristic"
    store = env["store"]
    (store / f"{slug}.md").write_text(entry_text("adversarial-fixture-with-heuristic", "Fixture", "Use both."))
    (store / "MEMORY.md").write_text(INDEX.replace(
        "[Alpha lesson](feedback_a.md)", f"[Alpha lesson](feedback_a.md) · [adversarial + heuristic]({slug}.md)"))
    plan = write_plan(env, [ITEMS[0], dict(ITEMS[0], slug=slug)])
    code, out = run_apply(env, "--apply", plan=plan)
    assert code == 0, out
    [note] = marked_notes(env, slug)
    suffix = hashlib.sha1(slug.encode()).hexdigest()[:4]
    assert note.name == f"2026-11-24-adversarial-fixture-with-heuristic-{suffix}.md"


def test_apply_writes_reads_back_moves_and_edits_index(env):
    code, out = run_apply(env, "--apply")
    assert code == 0, out
    run = assert_finished(env)
    [note] = marked_notes(env, "feedback_a")
    assert note.name == "2026-11-24-alpha-lesson-" + hashlib.sha1(b"feedback_a").hexdigest()[:4] + ".md"
    text = note.read_text()
    front = text.split("---\n")[1]
    assert front.splitlines() == [
        "type: claude-error-fix", "date: 2026-11-24", "created_at: 2026-11-24T12:00:00-08:00",
        "project: proj", 'migrated_from_memory: "feedback_a"',
        f'synced_from: "{env["store"] / "feedback_a.md"}"', 'memory_type: "feedback"', "status: active",
        "tags:", "  - claude/error-fix", "  - claude/project/proj", "  - claude/topic/moved-from-memory",
        "  - claude/topic/testing",
    ]
    assert "source_session" not in text
    assert "\n# Alpha lesson\n" in text
    assert f"`{backup_dir(env)}`" in text
    assert "\n## Summary\n\nAlpha description\n" in text
    assert mc._details(text, "feedback_a", "new").strip() == ENTRIES["feedback_a"][2].strip()
    assert oct(note.stat().st_mode & 0o777) == "0o600"
    # Originals in moved/, a full copy, private modes.
    bdir = backup_dir(env)
    for slug in ("feedback_a", "feedback_b", "project_c"):
        assert (bdir / "moved" / f"{slug}.md").read_text() == entry_text(*ENTRIES[slug])
    assert (bdir / "full-copy" / "MEMORY.md").read_text() == INDEX
    assert (bdir / "full-copy" / "stay.md").exists()
    for d in (bdir, bdir / "moved", bdir / "full-copy"):
        assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert oct((bdir / "run.json").stat().st_mode & 0o777) == "0o600"
    digest = {s: hashlib.sha256(entry_text(*ENTRIES[s]).encode()).hexdigest() for s in ENTRIES}
    assert run["written"] == [dict(slug="feedback_a", action="new", note=str(note.relative_to(env["vault"])),
                                   sha256=digest["feedback_a"]),
                              dict(slug="feedback_b", action="append", note=TARGET, sha256=digest["feedback_b"])]
    # The file list lives in manifest.json, not in run.json.
    assert json.loads((bdir / "manifest.json").read_text()) == dict(version=1, files=sorted(
        [dict(name=f"{s}.md", kind="file", sha256=digest[s]) for s in ENTRIES]
        + [dict(name="MEMORY.md", kind="file", sha256=hashlib.sha256(INDEX.encode()).hexdigest())],
        key=lambda f: f["name"]))
    assert oct((bdir / "manifest.json").stat().st_mode & 0o777) == "0o600"
    assert run["planned"] == [dict(slug="feedback_a", action="new", title="Alpha lesson",
                                   note=str(note.relative_to(env["vault"])), sha256=digest["feedback_a"]),
                              dict(slug="feedback_b", action="append", title="Beta lesson", note=TARGET,
                                   sha256=digest["feedback_b"]),
                              dict(slug="project_c", action="retire", title="Old project", note=None,
                                   sha256=digest["project_c"])]
    assert run["retired"] == ["project_c"]
    assert run["sizes"]["before"] == dict(bytes=len(INDEX.encode()), chars=len(INDEX), lines=9)
    assert run["sizes"]["after"]["bytes"] == len((env["store"] / "MEMORY.md").read_bytes())
    assert run["baseline"] == dict(indexed=["feedback_a", "feedback_b", "project_c", "stay"],
                                   dangling=[], duplicates=[], orphans=[])


def test_run_json_field_names(env):
    run_apply(env, "--apply")
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert set(run) == {"version", "state", "store", "vault", "insights_folder", "project", "created_at",
                        "updated_at", "backup_dir", "plan", "refused", "warnings", "written", "retired",
                        "moved", "dropped_lines", "baseline", "sizes", "planned"}
    assert run["dropped_lines"] == ["- [Alpha lesson](feedback_a.md) — first",
                                    "- [Beta lesson](feedback_b.md) — second",
                                    "- [Old project](project_c.md) — stale"]


def test_index_rewrite_changes_only_the_moved_line(env):
    run_apply(env, "--apply", plan=write_plan(env, [ITEMS[0]]))
    after = (env["store"] / "MEMORY.md").read_text().split("\n")
    before = INDEX.split("\n")
    assert [l for l in before if l not in after] == ["- [Alpha lesson](feedback_a.md) — first"]
    [added] = [l for l in after if l not in before]
    assert added.startswith("- Moved to vault (2026-11-24): 1 entries (1 new notes tagged "
                            "claude/topic/moved-from-memory, 0 appended")
    assert added.endswith(f"originals in ~/{backup_dir(env).relative_to(env['tmp'])}")
    assert str(env["tmp"]) not in added
    # The pointer line goes after the last kept index line.
    assert after.index(added) == after.index("- [Old project](project_c.md) — stale") + 1


def test_pointer_keeps_absolute_backup_path_outside_home(env, tmp_path_factory):
    env["backups"] = tmp_path_factory.mktemp("outside-home")
    code, out = run_apply(env, "--apply")
    assert code == 0, out
    lines = (env["store"] / "MEMORY.md").read_text().splitlines()
    [pointer] = [line for line in lines if line.startswith("- Moved to vault (")]
    assert pointer.endswith(f"originals in {backup_dir(env)}")


def test_moved_note_not_counted_for_the_moving_session(env):
    run_apply(env, "--apply")
    [note] = marked_notes(env, "feedback_a")
    bundle = gather_session_evidence(str(env["vault"]), "claude-sessions", "claude-insights", SID, "proj")
    assert bundle["error_fixes"] == [] and bundle["insights"] == []
    # Positive control: the same note with source_session set IS counted.
    text = note.read_text().replace("project: proj\n", f"project: proj\nsource_session: {SID}\n", 1)
    note.write_text(text)
    bundle = gather_session_evidence(str(env["vault"]), "claude-sessions", "claude-insights", SID, "proj")
    assert [e["path"] for e in bundle["error_fixes"]] == [str(note)]


def test_append_leaves_rest_of_target_byte_identical(env):
    code, out = run_apply(env, "--apply", plan=write_plan(env, [ITEMS[1]]))
    assert code == 0, out
    text = (env["vault"] / TARGET).read_text()
    assert text.startswith(TARGET_TEXT)
    section = text[len(TARGET_TEXT):]
    assert section.startswith("\n## From project memory: Beta lesson (moved 2026-11-24)\n"
                              "<!-- migrated_from_memory: feedback_b -->\n")
    assert mc._details(text, "feedback_b", "append").strip() == "Beta body text."
    # The lock sits next to the target (in claude-insights/), as note_writer puts it.
    lock = note_writer._lock_path(env["vault"] / TARGET)
    assert lock.parent == env["vault"] / "claude-insights"
    assert not lock.exists()
    assert not note_writer._lock_path(env["store"] / "MEMORY.md").exists()


# --- refusals -----------------------------------------------------------------

def test_lossless_check_catches_one_character(env, monkeypatch):
    real = mc.write_vault_note

    def corrupt(vault, folder, name, content):
        return real(vault, folder, name, content.replace("Line one of alpha.", "Line one of alpha!"))

    monkeypatch.setattr(mc, "write_vault_note", corrupt)
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert code == 1
    assert any(l.startswith("REFUSE feedback_a: read-back failed: copied text differs") for l in out)
    assert (env["store"] / "feedback_a.md").exists()
    assert "feedback_a.md" in (env["store"] / "MEMORY.md").read_text()
    assert not (env["store"] / "feedback_b.md").exists()


def test_check_note_text_rejects_tampered_summary(tmp_path):
    entry = {"slug": "feedback_a", "description": "Alpha description", "body": "Alpha body."}
    text = "\n## Summary\n\nAlphx description\n\n## Details\n\nAlpha body.\n\n"
    text += mc._END.format(slug=entry["slug"])
    path = tmp_path / "note.md"
    with pytest.raises(mc._ItemError, match="copied summary differs from the memory description"):
        mc._check_note_text(path, text, entry, "new")


def test_write_failure_leaves_that_original(env, monkeypatch):
    monkeypatch.setattr(mc, "write_vault_note", lambda *a: "write failed: disk full")
    code, out = run_apply(env, "--apply")
    assert code == 1
    assert "REFUSE feedback_a: write failed: disk full" in out
    assert (env["store"] / "feedback_a.md").read_text() == entry_text(*ENTRIES["feedback_a"])
    assert "- [Alpha lesson](feedback_a.md) — first" in (env["store"] / "MEMORY.md").read_text()
    assert not (env["store"] / "feedback_b.md").exists()


def test_append_target_changed_during_write_is_refused(env, monkeypatch):
    real = mc._atomic_rewrite

    def racing(dest, content, expect_stat=None, stat_out=None):
        if dest.name == "MEMORY.md":
            return real(dest, content, expect_stat=expect_stat, stat_out=stat_out)
        with open(dest, "a") as fh:
            fh.write("someone else\n")
        return real(dest, content, expect_stat=expect_stat, stat_out=stat_out)

    monkeypatch.setattr(mc, "_atomic_rewrite", racing)
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[1:]))
    assert code == 1
    assert any(l.startswith("REFUSE feedback_b: write failed") for l in out)
    assert (env["vault"] / TARGET).read_text() == TARGET_TEXT + "someone else\n"
    assert (env["store"] / "feedback_b.md").exists()


@pytest.mark.parametrize("target", ["../outside.md", "link.md"])
def test_append_target_outside_vault_or_symlink_refused(env, target):
    (env["tmp"] / "outside.md").write_text("x")
    os.symlink(env["tmp"] / "outside.md", env["vault"] / "link.md")
    plan = write_plan(env, [dict(slug="feedback_b", action="append", target=target)])
    before = trees(env)
    result = cli_apply(env, plan, "--apply")
    assert result.returncode == 2
    assert "bad plan" in result.stderr
    assert trees(env) == before


def test_symlinked_insights_folder_refused(env):
    outside = env["tmp"] / "elsewhere"
    outside.mkdir()
    (env["vault"] / "claude-insights").rename(env["tmp"] / "moved-insights")
    os.symlink(outside, env["vault"] / "claude-insights")
    code, out = run_apply(env, "--apply", plan=write_plan(env, [ITEMS[0]]))
    assert code == 2
    assert any(l.startswith("REFUSE feedback_a: symlink in write path") for l in out)
    assert list(outside.iterdir()) == []
    assert (env["store"] / "feedback_a.md").exists()


def test_secret_shaped_body_refused(env):
    token = "gh" + "p_" + "A1b2C3d4" * 5
    (env["store"] / "feedback_a.md").write_text(entry_text("Alpha", "d", f"Use {token} here."))
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert code == 1
    assert "REFUSE feedback_a: text holds a secret-shaped string (scrub_secrets would change it)" in out
    assert marked_notes(env, "feedback_a") == []
    assert token not in "".join(p.read_text() for p in env["vault"].rglob("*.md"))
    assert (env["store"] / "feedback_a.md").exists()


def test_mixed_pointer_line_refuses_its_moving_slug(env):
    (env["store"] / "MEMORY.md").write_text(INDEX.replace(
        "- [Alpha lesson](feedback_a.md) — first", "- Pair: [Alpha lesson](feedback_a.md) · [Stays](stay.md)"))
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert code == 1
    assert "REFUSE feedback_a: index line also points at staying entries: stay.md" in out
    assert marked_notes(env, "feedback_a") == []
    assert (env["store"] / "feedback_a.md").exists()
    assert audit(env["store"])["dangling"] == []


def test_refusal_cascades_across_lines(env):
    """A slug refused on one mixed line makes another of its lines mixed."""
    index = INDEX.replace("- [Alpha lesson](feedback_a.md) — first",
                          "- [Alpha lesson](feedback_a.md) · [Stays](stay.md)\n"
                          "- [Alpha again](feedback_a.md) · [Beta again](feedback_b.md)")
    (env["store"] / "MEMORY.md").write_text(index)
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert code == 2  # nothing could move
    assert any(l.startswith("REFUSE feedback_b: index line also points at staying entries: feedback_a.md")
               for l in out)
    assert (env["store"] / "feedback_b.md").exists()
    assert audit(env["store"])["dangling"] == []


@pytest.mark.parametrize(("text", "expected"), [
    ("- [A](a.md) [S](s.md)\n- [A2](a.md) [B](b.md)\n", {"a", "b"}),
    # The shared line comes first, so one top-to-bottom pass misses b.
    ("- [A2](a.md) [B](b.md)\n- [A](a.md) [S](s.md)\n", {"a", "b"}),
    ("- [B](b.md) [C](c.md)\n- [A2](a.md) [B](b.md)\n- [A](a.md) [S](s.md)\n", {"a", "b", "c"}),
])
def test_index_edit_cascade_unit(text, expected):
    names = {"a.md", "b.md", "c.md", "s.md"}
    new, dropped, refused, _ = index_edit(text, names, names - {"s.md"}, "- P")
    assert (new, dropped) == (text, [])
    assert set(refused) == expected


def test_secret_refusal_cascades_to_line_partner(env):
    token = "AKIA" + "ABCDEFGHIJKLMNOP"
    (env["store"] / "feedback_a.md").write_text(entry_text("Alpha", "d", token))
    (env["store"] / "MEMORY.md").write_text(INDEX.replace(
        "- [Alpha lesson](feedback_a.md) — first", "- [Alpha lesson](feedback_a.md) · [Beta](feedback_b.md)"))
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS))
    assert code == 1
    assert "REFUSE feedback_b: index line also points at staying entries: feedback_a.md" in out
    assert (env["store"] / "feedback_b.md").exists()
    assert (env["vault"] / TARGET).read_text() == TARGET_TEXT
    assert audit(env["store"])["dangling"] == []


def test_readback_refuses_a_note_with_source_session(env, monkeypatch):
    real = mc.write_vault_note

    def inject(vault, folder, name, content):
        return real(vault, folder, name, content.replace("project: proj\n", f"project: proj\nsource_session: {SID}\n", 1))

    monkeypatch.setattr(mc, "write_vault_note", inject)
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert code == 1
    assert any(l.startswith("REFUSE feedback_a: read-back failed:") and l.endswith("carries source_session")
               for l in out)
    assert (env["store"] / "feedback_a.md").exists()


def test_readback_refuses_append_that_changed_the_rest(env, monkeypatch):
    real = mc._atomic_rewrite

    def clobber(dest, content, expect_stat=None, stat_out=None):
        if dest.name != "MEMORY.md":
            content = content.replace("Original body.", "Original body!")
        return real(dest, content, expect_stat=expect_stat, stat_out=stat_out)

    monkeypatch.setattr(mc, "_atomic_rewrite", clobber)
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[1:]))
    assert code == 1
    assert any(l.startswith("REFUSE feedback_b: read-back failed: the rest of") for l in out)
    assert (env["store"] / "feedback_b.md").exists()


def test_index_edit_warns_on_aside_of_dropped_line():
    text = "- [A](a.md) see [[s]]\n- [S](s.md)\n"
    _, dropped, _, warnings = index_edit(text, {"a.md", "s.md"}, {"a.md"}, "- P")
    assert dropped == ["- [A](a.md) see [[s]]"]
    assert warnings == {"s": "named as an aside on a dropped index line"}


def test_mention_in_another_entry_warns(env):
    (env["store"] / "stay.md").write_text(entry_text("Stay", "d", "See feedback_a.md for why."))
    code, out = run_apply(env, plan=write_plan(env, [ITEMS[0]]))
    assert code == 0
    assert "WARN feedback_a: mentioned in stay.md" in out


def test_line_appended_during_run_survives(env, monkeypatch):
    added = "- [Added by another session](stay.md) — later"

    def other_session(name):
        if name == "moved":
            with open(env["store"] / "MEMORY.md", "a", encoding="utf-8") as fh:
                fh.write(added + "\n")

    monkeypatch.setattr(mc, "_checkpoint", other_session)
    code, out = run_apply(env, "--apply")
    assert code == 0, out
    index = (env["store"] / "MEMORY.md").read_text()
    assert added in index
    assert "feedback_a.md" not in index


def test_index_stat_mismatch_retries(env, monkeypatch):
    real = mc._atomic_rewrite
    calls = []

    def flaky(dest, content, expect_stat=None, stat_out=None):
        calls.append(dest.name)
        if len(calls) == 1:
            with open(dest, "a") as fh:
                fh.write("- late line\n")
        return real(dest, content, expect_stat=expect_stat, stat_out=stat_out)

    monkeypatch.setattr(mc, "_atomic_rewrite", flaky)
    code, out = run_apply(env, "--apply", plan=write_plan(env, [ITEMS[0]]))
    assert code == 0, out
    assert calls == ["MEMORY.md", "MEMORY.md"]
    index = (env["store"] / "MEMORY.md").read_text()
    assert "- late line" in index and "feedback_a.md" not in index


def test_index_that_keeps_changing_exits_2(env, monkeypatch):
    monkeypatch.setattr(mc, "_atomic_rewrite", lambda *a, **k: "note changed on disk")
    with pytest.raises(mc.ApplyError, match="kept changing; finish with: apply --resume"):
        run_apply(env, "--apply", plan=write_plan(env, [ITEMS[0]]))
    restore()
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert run["state"] == "moved"
    assert main(["apply", "--resume", str(backup_dir(env))]) == 0


def test_index_line_added_mid_run_moves_original_back(env, monkeypatch):
    def other_session(name):
        if name == "moved":
            with open(env["store"] / "MEMORY.md", "a", encoding="utf-8") as fh:
                fh.write("- Mixed: [Alpha lesson](feedback_a.md) · [Stays](stay.md)\n")

    monkeypatch.setattr(mc, "_checkpoint", other_session)
    code, out = run_apply(env, "--apply")
    assert code == 1
    assert any(l.startswith("REFUSE feedback_a: index line also points") and l.endswith("moved back to the store")
               for l in out)
    assert (env["store"] / "feedback_a.md").exists()
    assert audit(env["store"])["dangling"] == []


def test_backup_dir_reuse_refused(env):
    backup_dir(env).mkdir()
    before = trees(env)
    with pytest.raises(mc.ApplyError, match="backup dir already exists"):
        run_apply(env, "--apply")
    restore()
    assert trees(env) == before


def test_existing_note_without_marker_refused(env):
    name = "2026-11-24-alpha-lesson-" + hashlib.sha1(b"feedback_a").hexdigest()[:4] + ".md"
    (env["vault"] / "claude-insights" / name).write_text("mine\n")
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert code == 1
    assert any(l.startswith("REFUSE feedback_a:") and "already exists and has no marker" in l for l in out)
    assert (env["vault"] / "claude-insights" / name).read_text() == "mine\n"


def test_unfinished_run_blocks_a_fresh_apply(env, monkeypatch):
    monkeypatch.setattr(mc, "_checkpoint", lambda n: (_ for _ in ()).throw(KeyboardInterrupt) if n == "backup" else None)
    with pytest.raises(KeyboardInterrupt):
        run_apply(env, "--apply", plan=write_plan(env, ITEMS[:1]))
    restore()
    result = cli_apply(env, write_plan(env, ITEMS[1:]), "--apply")
    assert result.returncode == 2
    assert f"apply --resume {backup_dir(env)}" in result.stderr


def test_midnight_boundary(env):
    late = datetime(2026, 11, 24, 23, 59, 59, tzinfo=TZ)
    code, out = run_apply(env, "--apply", now=late)
    assert code == 0
    [note] = marked_notes(env, "feedback_a")
    assert note.name.startswith("2026-11-24-")
    assert "created_at: 2026-11-24T23:59:59-08:00" in note.read_text()
    assert backup_dir(env, "20261124-235959").is_dir()


# --- stop points and rerun ----------------------------------------------------

class Crash(Exception):
    pass


def crash_at(monkeypatch, point):
    def hook(name):
        if name == point:
            raise Crash(name)
    monkeypatch.setattr(mc, "_checkpoint", hook)


_REAL = dict(_checkpoint=mc._checkpoint, _atomic_rewrite=mc._atomic_rewrite,
             write_vault_note=mc.write_vault_note)
_REAL_SAVE = mc._Run.save
_REAL_REFUSE = mc._Run.refuse
_REAL_COPYTREE = mc.shutil.copytree


def restore():
    """Undo the crash and fault patches but keep HOME (monkeypatch.undo would reset it)."""
    for name, value in _REAL.items():
        setattr(mc, name, value)
    mc._Run.save = _REAL_SAVE
    mc._Run.refuse = _REAL_REFUSE
    mc.shutil.copytree = _REAL_COPYTREE


def test_crash_before_the_rename_leaves_no_backup_and_no_notes(env, monkeypatch):
    """A backup dir only exists once complete; notes are written after it (R2)."""
    store_before, vault_before = tree_hash(env["store"]), tree_hash(env["vault"])

    def crash(src, dst):
        raise Crash("before the rename")

    with monkeypatch.context() as patch:
        patch.setattr(mc.os, "rename", crash)
        with pytest.raises(Crash):
            run_apply(env, "--apply")
    assert not backup_dir(env).exists()
    [partial] = env["backups"].iterdir()
    assert ".tmp-" in partial.name and (partial / "full-copy" / "stay.md").exists()
    assert (partial / "run.json").exists() and (partial / "manifest.json").exists()
    assert tree_hash(env["store"]) == store_before and tree_hash(env["vault"]) == vault_before
    assert marked_notes(env, "feedback_a") == []
    assert not list(env["backups"].glob(".memory-consolidate-*"))
    # A rerun is not blocked by the partial dir, warns about it and finishes.
    code, out = run_apply(env, "--apply")
    assert code == 0, out
    assert f"WARN leftover partial backup {partial}" in out
    assert mc._unfinished_runs(env["backups"], env["store"]) == []
    assert_finished(env)


@pytest.mark.parametrize("point", ["backup", "mid-4", "moved", "index-written"])
def test_stop_finishes_with_resume(env, monkeypatch, point):
    crash_at(monkeypatch, point)
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    if point == "mid-4":
        moved = sorted(p.stem for p in (backup_dir(env) / "moved").iterdir())
        assert moved == ["feedback_a"]
    result = cli(env, "apply", "--resume", str(backup_dir(env)), "--vault", str(env["vault"]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == f"store: {env['store']}"
    assert_finished(env)


def test_resume_done_is_a_no_op(env):
    run_apply(env, "--apply")
    before = trees(env)
    result = cli(env, "apply", "--resume", str(backup_dir(env)))
    assert result.returncode == 0
    assert "resuming from state: done" in result.stdout
    assert trees(env) == before


def test_resume_rechecks_notes_before_moving(env, monkeypatch):
    crash_at(monkeypatch, "notes")
    with pytest.raises(Crash):
        run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    restore()
    [note] = marked_notes(env, "feedback_a")
    note.write_text(note.read_text().replace("Line one of alpha.", "Line one of alpha?"))
    result = cli(env, "apply", "--resume", str(backup_dir(env)))
    assert result.returncode == 1
    assert "REFUSE feedback_a: read-back failed" in result.stdout
    assert (env["store"] / "feedback_a.md").exists()
    assert not (env["store"] / "feedback_b.md").exists()
    assert audit(env["store"])["dangling"] == []


def test_resume_refuses_original_changed_after_backup(env, monkeypatch):
    crash_at(monkeypatch, "backup")
    with pytest.raises(Crash):
        run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    restore()
    (env["store"] / "feedback_b.md").write_text(entry_text("Beta", "d", "edited by another session"))
    result = cli(env, "apply", "--resume", str(backup_dir(env)))
    assert result.returncode == 1
    assert "REFUSE feedback_b: original changed after the backup copy; not moved" in result.stdout
    assert (env["store"] / "feedback_b.md").exists()
    assert "feedback_b.md" in (env["store"] / "MEMORY.md").read_text()


# --- exit codes and inputs ----------------------------------------------------

def test_exit_0_via_cli(env):
    result = cli_apply(env, write_plan(env, ITEMS[:1]), "--apply")
    assert result.returncode == 0, result.stderr
    assert not (env["store"] / "feedback_a.md").exists()


def test_exit_1_via_cli(env):
    (env["store"] / "stay.md").write_text(entry_text("Stay", "d", "pass" + "word: hunter2"))
    plan = write_plan(env, [dict(slug="stay", action="retire", reason="x"), ITEMS[2]], "plan2.json")
    result = cli_apply(env, plan, "--apply")
    assert result.returncode == 1, result.stderr
    assert not (env["store"] / "project_c.md").exists()
    assert "REFUSE stay: text holds a secret-shaped string" in result.stdout


@pytest.mark.parametrize("items", [
    [dict(slug="missing_slug", action="new")],
    [dict(slug="feedback_a", action="delete")],
    [dict(slug="feedback_a", action="retire")],
])
def test_exit_2_bad_plan(env, items):
    before = trees(env)
    result = cli_apply(env, write_plan(env, items), "--apply")
    assert result.returncode == 2
    assert result.stdout.splitlines() == [f"store: {env['store']}"]
    assert "bad plan" in result.stderr
    assert trees(env) == before


def test_exit_2_store_refused(env):
    result = cli(env, "apply", "--plan", str(write_plan(env)), "--store", str(env["tmp"] / "nope"),
                 "--vault", str(env["vault"]), "--insights-folder", "claude-insights")
    assert result.returncode == 2
    assert "not a directory" in result.stderr


def test_exit_2_when_nothing_can_move(env):
    (env["store"] / "feedback_a.md").write_text(entry_text("Alpha", "d", "AKIA" + "ABCDEFGHIJKLMNOP"))
    before = trees(env)
    result = cli_apply(env, write_plan(env, ITEMS[:1]), "--apply")
    assert result.returncode == 2
    assert "nothing to move" in result.stdout
    assert trees(env) == before
    assert cli_apply(env, write_plan(env, ITEMS[:1])).returncode == 2  # dry run agrees


def test_exit_2_append_target_deleted_after_dry_run(env):
    plan = write_plan(env, [ITEMS[1]])
    assert cli_apply(env, plan).returncode == 0
    (env["vault"] / TARGET).unlink()
    result = cli_apply(env, plan, "--apply")
    assert result.returncode == 2
    assert "target is not an existing file" in result.stderr
    assert (env["store"] / "feedback_b.md").exists()


def test_exit_2_missing_index(env):
    (env["store"] / "MEMORY.md").unlink()
    assert cli_apply(env, write_plan(env), "--apply").returncode == 2


def test_exit_2_bad_vault(env):
    result = cli(env, "apply", "--plan", str(write_plan(env)), "--store", str(env["store"]),
                 "--vault", str(env["tmp"] / "novault"), "--insights-folder", "claude-insights")
    assert result.returncode == 2
    assert "vault is not a directory" in result.stderr


@pytest.mark.parametrize("folder", ["../out", "/abs"])
def test_exit_2_bad_insights_folder(env, folder):
    result = cli(env, "apply", "--plan", str(write_plan(env)), "--store", str(env["store"]),
                 "--vault", str(env["vault"]), "--insights-folder", folder)
    assert result.returncode == 2
    assert "vault-relative" in result.stderr


def test_resume_exit_2_cases(env, monkeypatch):
    assert cli(env, "apply", "--resume", str(env["tmp"] / "none")).returncode == 2
    crash_at(monkeypatch, "backup")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    bdir = str(backup_dir(env))
    other = env["tmp"] / "other-vault"
    other.mkdir()
    result = cli(env, "apply", "--resume", bdir, "--vault", str(other))
    assert result.returncode == 2 and "is not the run's vault" in result.stderr
    result = cli(env, "apply", "--resume", bdir, "--apply")
    assert result.returncode == 2 and "--resume takes only" in result.stderr
    # run.json from another store
    store2 = env["tmp"] / ".claude/projects/-y-proj/memory"
    store2.mkdir(parents=True)
    (store2 / "MEMORY.md").write_text("")
    run_path = backup_dir(env) / "run.json"
    run = json.loads(run_path.read_text())
    run["store"] = str(store2)
    run_path.write_text(json.dumps(run))
    result = cli(env, "apply", "--resume", bdir)
    assert result.returncode == 2 and "bad plan in run.json" in result.stderr
    run_path.write_text("{not json")
    assert cli(env, "apply", "--resume", bdir).returncode == 2


def test_audit_human_output_lists_counts_not_every_slug(env, capsys):
    assert main(["audit", "--store", str(env["store"])]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "entries: 4 (4 indexed)" in out
    assert not any("feedback_a" in line for line in out)


# --- review fixes (PR #529, pass 1) ---------------------------------------------

def test_entry_edited_between_validate_and_backup_is_refused_with_no_note(env, monkeypatch):
    """Notes come from the snapshot, so a changed snapshot refuses the item before any note (X-001)."""
    edited = entry_text("Beta", "Beta description", "Edited after the plan was checked.")
    real = mc.shutil.copytree

    def edit_then_copy(src, dst, **kwargs):
        (env["store"] / "feedback_b.md").write_text(edited, encoding="utf-8")
        return real(src, dst, **kwargs)

    monkeypatch.setattr(mc.shutil, "copytree", edit_then_copy)
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    restore()
    assert code == 1
    assert any(l.startswith("REFUSE feedback_b: changed during the run") for l in out)
    assert (env["vault"] / TARGET).read_text() == TARGET_TEXT
    assert (env["store"] / "feedback_b.md").read_text() == edited
    assert "- [Beta lesson](feedback_b.md) — second" in (env["store"] / "MEMORY.md").read_text()
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert run["moved"] == ["feedback_a"] and [w["slug"] for w in run["written"]] == ["feedback_a"]
    assert main(["verify", "--backup", str(backup_dir(env))]) == 0


def test_retired_entry_edited_between_backup_and_move_is_not_moved(env, monkeypatch):
    edited = entry_text("Gamma", "Stale project", "Someone updated this after the backup.")

    def edit(name):
        if name == "backup":
            (env["store"] / "project_c.md").write_text(edited, encoding="utf-8")

    monkeypatch.setattr(mc, "_checkpoint", edit)
    code, out = run_apply(env, "--apply")
    restore()
    assert code == 1
    assert "REFUSE project_c: original changed after the backup copy; not moved" in out
    assert (env["store"] / "project_c.md").read_text() == edited
    assert not (backup_dir(env) / "moved" / "project_c.md").exists()
    assert "- [Old project](project_c.md) — stale" in (env["store"] / "MEMORY.md").read_text()
    assert audit(env["store"])["dangling"] == []


def _args(env, *extra):
    return ["apply", "--plan", str(write_plan(env)), "--store", str(env["store"]), "--vault", str(env["vault"]),
            "--insights-folder", "claude-insights", "--project", "proj", "--backup-root", str(env["backups"]),
            *extra]


def test_os_error_in_the_copy_exits_2_and_leaves_no_backup(env, monkeypatch, capsys):
    def no_space(src, dst, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mc.shutil, "copytree", no_space)
    assert main(_args(env, "--apply")) == 2
    err = capsys.readouterr().err
    assert "error: [Errno 28] No space left on device" in err
    assert "--resume" not in err  # no run.json exists yet: there is nothing to resume
    assert not backup_dir(env).exists()
    assert marked_notes(env, "feedback_a") == []
    restore()
    assert main(_args(env, "--apply")) == 0


@pytest.mark.parametrize("via_resume", [False, True])
def test_os_error_in_the_move_step_exits_2_with_the_resume_hint(env, monkeypatch, capsys, via_resume):
    def boom(self):
        raise OSError(13, "Permission denied")

    if via_resume:
        crash_at(monkeypatch, "backup")
        with pytest.raises(Crash):
            run_apply(env, "--apply")
        restore()
        [bdir] = env["backups"].glob("memory-moved-*")
        args = ["apply", "--resume", str(bdir)]
    else:
        args = _args(env, "--apply")
    monkeypatch.setattr(mc._Run, "move_all", boom)
    assert main(args) == 2
    err = capsys.readouterr().err
    [bdir] = env["backups"].glob("memory-moved-*")
    assert "error: [Errno 13] Permission denied" in err
    assert f"apply --resume {bdir}" in err


def test_os_error_before_any_run_json_prints_no_hint(env, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(mc, "_prepare", boom)
    assert main(_args(env, "--apply")) == 2
    err = capsys.readouterr().err
    assert "error: [Errno 5] I/O error" in err
    assert "--resume" not in err


def test_resume_read_back_failure_after_a_move_puts_the_original_back(env, monkeypatch):
    crash_at(monkeypatch, "mid-4")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    assert [p.stem for p in (backup_dir(env) / "moved").iterdir()] == ["feedback_a"]
    [note] = marked_notes(env, "feedback_a")
    note.write_text(note.read_text().replace("Line one of alpha.", "Line one of alpha?"))
    result = cli(env, "apply", "--resume", str(backup_dir(env)))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSE feedback_a: read-back failed" in result.stdout
    assert (env["store"] / "feedback_a.md").read_text() == entry_text(*ENTRIES["feedback_a"])
    assert "- [Alpha lesson](feedback_a.md) — first" in (env["store"] / "MEMORY.md").read_text()
    assert audit(env["store"])["dangling"] == []
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert sorted(run["moved"]) == ["feedback_b", "project_c"]
    assert [r["slug"] for r in run["refused"]] == ["feedback_a"]
    assert [w["slug"] for w in run["written"]] == ["feedback_b"]


def test_resume_read_back_failure_stops_when_the_store_path_is_taken(env, monkeypatch, capsys):
    crash_at(monkeypatch, "mid-4")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    [note] = marked_notes(env, "feedback_a")
    note.write_text(note.read_text().replace("Line one of alpha.", "Line one of alpha?"))
    (env["store"] / "feedback_a.md").write_text("a new file another session made")
    assert main(["apply", "--resume", str(backup_dir(env))]) == 2
    err = capsys.readouterr().err
    assert "feedback_a is refused and its original is in" in err and "is taken" in err
    assert (env["store"] / "feedback_a.md").read_text() == "a new file another session made"
    assert (backup_dir(env) / "moved" / "feedback_a.md").exists()


@pytest.mark.parametrize("point", ["backup", "moved"])
def test_resume_with_a_deleted_append_target_refuses_only_that_item(env, monkeypatch, point):
    """The note check runs on every resume, whatever state was saved (X-005, X-020)."""
    crash_at(monkeypatch, point)
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    (env["vault"] / TARGET).unlink()
    result = cli(env, "apply", "--resume", str(backup_dir(env)))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSE feedback_b: append target is missing or unreadable" in result.stdout
    assert (env["store"] / "feedback_b.md").exists()
    assert not (backup_dir(env) / "moved" / "feedback_b.md").exists()
    assert "- [Beta lesson](feedback_b.md) — second" in (env["store"] / "MEMORY.md").read_text()
    for slug in ("feedback_a", "project_c"):
        assert not (env["store"] / f"{slug}.md").exists()
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert run["state"] == "done"
    assert sorted(run["moved"]) == ["feedback_a", "project_c"]
    assert audit(env["store"])["dangling"] == []


def test_rollback_crash_between_rename_and_save_resumes(env, monkeypatch):
    """The mixed-line rollback is repeatable after a stop between the rename and the save (X-006)."""
    def other_session(name):
        if name == "moved":
            with open(env["store"] / "MEMORY.md", "a", encoding="utf-8") as fh:
                fh.write("- Mixed: [Alpha lesson](feedback_a.md) · [Stays](stay.md)\n")

    def stop_before_save(self, slug, reason):
        if reason.endswith("moved back to the store"):
            raise Crash("after the rename, before the save")
        _REAL_REFUSE(self, slug, reason)

    monkeypatch.setattr(mc, "_checkpoint", other_session)
    monkeypatch.setattr(mc._Run, "refuse", stop_before_save)
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    assert (env["store"] / "feedback_a.md").exists()
    assert not (backup_dir(env) / "moved" / "feedback_a.md").exists()
    assert main(["apply", "--resume", str(backup_dir(env))]) == 1
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert run["state"] == "done"
    assert "feedback_a" not in run["moved"]
    assert any(r["slug"] == "feedback_a" and r["reason"].endswith("moved back to the store")
               for r in run["refused"])
    assert (env["store"] / "feedback_a.md").read_text() == entry_text(*ENTRIES["feedback_a"])
    assert audit(env["store"])["dangling"] == []


TOKEN = "gh" + "p_" + "A1b2C3d4" * 5


def test_secret_in_an_index_label_is_refused(env):
    (env["store"] / "MEMORY.md").write_text(INDEX.replace("[Alpha lesson]", f"[{TOKEN}]"))
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert code == 1
    assert "REFUSE feedback_a: index label holds a secret-shaped string (scrub_secrets would change it)" in out
    assert marked_notes(env, "feedback_a") == []
    assert TOKEN not in "".join(p.read_text() for p in env["vault"].rglob("*.md"))
    assert (env["store"] / "feedback_a.md").exists()
    assert f"[{TOKEN}](feedback_a.md)" in (env["store"] / "MEMORY.md").read_text()


def test_secret_index_label_cascades_to_the_line_partner(env):
    (env["store"] / "MEMORY.md").write_text(INDEX.replace(
        "- [Alpha lesson](feedback_a.md) — first", f"- [{TOKEN}](feedback_a.md) · [Beta](feedback_b.md)"))
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert code == 2  # neither can move
    assert "REFUSE feedback_b: index line also points at staying entries: feedback_a.md" in out
    assert (env["store"] / "feedback_b.md").exists()
    assert audit(env["store"])["dangling"] == []


def test_marker_reuse_is_scoped_by_project(env):
    """Two projects with one slug each get their own note (X-011)."""
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:1]))
    assert code == 0, out
    [first] = marked_notes(env, "feedback_a")
    before = first.read_text()
    store2 = env["tmp"] / ".claude/projects/-y-other/memory"
    store2.mkdir(parents=True)
    (store2 / "MEMORY.md").write_text(INDEX)
    (store2 / "feedback_a.md").write_text(entry_text("Alpha", "Other description", "Another project's body."))
    out2 = []
    code = mc.apply(write_plan(dict(env, store=store2), ITEMS[:1], "plan2.json"), store2, env["vault"],
                    "claude-insights", "other", env["backups"], True, NOW, out=out2.append)
    assert code == 0, out2
    assert first.read_text() == before
    notes = marked_notes(env, "feedback_a")
    assert len(notes) == 2
    [second] = [n for n in notes if n != first]
    text = second.read_text()
    assert "project: other\n" in text and "Another project's body." in text
    assert "claude/project/other" in text
    assert not (store2 / "feedback_a.md").exists()


def test_indented_first_line_survives_byte_for_byte(env):
    body = "    indented = 1\n    more\n\nplain text\n"
    (env["store"] / "feedback_a.md").write_text(entry_text("Alpha", "Alpha description", "\n\n" + body))
    assert mc.read_entry(env["store"] / "feedback_a.md")["body"] == body.rstrip()
    code, out = run_apply(env, "--apply", plan=write_plan(env, ITEMS[:1]))
    assert code == 0, out
    [note] = marked_notes(env, "feedback_a")
    assert "\n## Details\n\n    indented = 1\n    more\n\nplain text\n\n<!--" in note.read_text()
    assert (backup_dir(env) / "moved" / "feedback_a.md").read_text().endswith("\n\n\n\n" + body)


def test_apply_with_store_works_from_another_projects_directory(env):
    """audit --store X and apply --store X agree, wherever the shell is (X-014)."""
    elsewhere = env["tmp"] / "elsewhere"
    elsewhere.mkdir()

    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env={**os.environ, "HOME": str(env["tmp"])},
                              cwd=elsewhere, timeout=60)

    plan = write_plan(env, ITEMS[:1])
    common = ["--vault", str(env["vault"]), "--insights-folder", "claude-insights", "--project", "proj",
              "--backup-root", str(env["backups"])]
    assert run("audit", "--store", str(env["store"])).stdout.splitlines()[0] == f"store: {env['store']}"
    bare = run("apply", "--plan", str(plan), *common)
    assert bare.returncode == 2 and "no memory store for" in bare.stderr
    dry = run("apply", "--plan", str(plan), "--store", str(env["store"]), *common)
    assert dry.returncode == 0, dry.stderr
    done = run("apply", "--plan", str(plan), "--store", str(env["store"]), *common, "--apply")
    assert done.returncode == 0, done.stderr
    [bdir] = env["backups"].glob("memory-moved-*")
    verified = run("verify", "--backup", str(bdir), "--store", str(env["store"]))
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_abbreviated_flags_are_not_accepted(env):
    """--app must not turn a dry run into a write (X-017)."""
    plan = write_plan(env)
    before = trees(env)
    for flag in ("--app", "--appl"):
        result = cli_apply(env, plan, flag)
        assert result.returncode == 2, result.stdout
        assert "unrecognized arguments" in result.stderr
    assert trees(env) == before


def test_end_marker_text_in_a_body_is_refused_before_any_write(env):
    body = "Quote: " + mc._END.format(slug="feedback_a") + " end."
    (env["store"] / "feedback_a.md").write_text(entry_text("Alpha", "Alpha description", body))
    before = trees(env)
    code, out = run_apply(env, plan=write_plan(env, ITEMS[:2]))
    assert code == 1
    assert "REFUSE feedback_a: body contains the end marker text" in out
    assert trees(env) == before
    run_apply(env, "--apply", plan=write_plan(env, ITEMS[:2]))
    assert marked_notes(env, "feedback_a") == []
    assert (env["store"] / "feedback_a.md").exists()


def test_two_notes_carrying_one_marker_refuse_the_new_item(env):
    for name in ("2026-11-02-one-aaaa.md", "2026-11-03-two-bbbb.md"):
        (env["vault"] / "claude-insights" / name).write_text(
            '---\ntype: claude-insight\nproject: proj\nmigrated_from_memory: "feedback_a"\n---\n\nx\n')
    code, out = run_apply(env, plan=write_plan(env, ITEMS[:2]))
    assert code == 1
    [line] = [l for l in out if l.startswith("REFUSE feedback_a:")]
    assert "more than one note carries its marker" in line
    assert "2026-11-02-one-aaaa.md" in line and "2026-11-03-two-bbbb.md" in line
    assert not any(l.startswith("NEW feedback_a") for l in out)


def test_append_target_carrying_the_marker_twice_refuses_the_item(env):
    section = ("\n## From project memory: Beta (moved 2026-11-02)\n<!-- migrated_from_memory: feedback_b -->\n"
               "old\n")
    (env["vault"] / TARGET).write_text(TARGET_TEXT + section + section)
    code, out = run_apply(env, plan=write_plan(env, ITEMS[1:2]))
    assert code == 2  # nothing could move
    assert f"REFUSE feedback_b: {TARGET} carries its marker 2 times" in out
    assert not any(l.startswith("APPEND feedback_b") for l in out)


# --- review fixes (PR #529, pass 2): one lock, atomic backup, idempotent resume ----------

def lock_file(env):
    return env["backups"] / ".memory-consolidate--x-proj.lock"


def test_lock_held_by_a_live_pid_exits_2(env):
    lock_file(env).write_text(f"{os.getpid()} 2026-11-24T11:00:00-08:00\n")
    before = trees(env)
    result = cli_apply(env, write_plan(env), "--apply")
    assert result.returncode == 2
    assert "another run holds the lock" in result.stderr and f"pid {os.getpid()}" in result.stderr
    assert trees(env) == before
    assert lock_file(env).exists()  # the holder's lock is not ours to remove
    # A dry run takes no lock, so it is not blocked.
    assert cli_apply(env, write_plan(env)).returncode == 0


def test_stale_lock_warns_and_is_taken(env):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock_file(env).write_text(f"{dead.pid} 2026-11-24T11:00:00-08:00\n")
    result = cli_apply(env, write_plan(env), "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"WARN stale lock from pid {dead.pid} (2026-11-24T11:00:00-08:00); taking it" in result.stdout
    assert not lock_file(env).exists()


def test_unreadable_lock_is_not_taken(env):
    lock_file(env).write_text("garbage")
    assert cli_apply(env, write_plan(env), "--apply").returncode == 2
    assert lock_file(env).read_text() == "garbage"


def test_empty_lock_is_not_taken_and_the_message_says_what_to_do(env):
    lock_file(env).write_text("")
    result = cli_apply(env, write_plan(env), "--apply")
    assert result.returncode == 2
    assert f"remove it if no run is active: {lock_file(env)}" in result.stderr
    assert lock_file(env).read_text() == ""


def test_a_write_failure_preparing_the_lock_leaves_no_lock(env, monkeypatch):
    def full(fd, mode):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(mc.os, "fchmod", full)
        with pytest.raises(OSError):
            run_apply(env, "--apply")
    assert not lock_file(env).exists()
    assert not list(env["backups"].glob(".lock-tmp-*"))
    code, out = run_apply(env, "--apply")  # the retry is not blocked
    assert code == 0, out
    assert not lock_file(env).exists()


def test_the_lock_is_removed_after_a_crash(env, monkeypatch):
    crash_at(monkeypatch, "mid-4")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    assert not lock_file(env).exists()


def test_a_second_resume_sees_the_lock_and_exits_2(env, monkeypatch, capsys):
    crash_at(monkeypatch, "backup")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    inner = []

    def other_resume(name):
        if name == "notes":
            inner.append(main(["apply", "--resume", str(backup_dir(env))]))

    monkeypatch.setattr(mc, "_checkpoint", other_resume)
    assert main(["apply", "--resume", str(backup_dir(env))]) == 0
    assert inner == [2]
    assert "another run holds the lock" in capsys.readouterr().err
    assert_finished(env)


def test_resume_ignores_a_stale_moved_list(env, monkeypatch):
    """Move status comes from the filesystem; a journal that says moved is not believed (X-004)."""
    crash_at(monkeypatch, "mid-4")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    path = backup_dir(env) / "run.json"
    run = json.loads(path.read_text())
    run["moved"] = ["feedback_b", "project_c"]  # neither has moved
    path.write_text(json.dumps(run))
    assert main(["apply", "--resume", str(backup_dir(env))]) == 0
    assert_finished(env)


def test_resume_with_a_moved_file_the_journal_does_not_know(env, monkeypatch):
    """Crash after the rename, before any save: the file in moved/ counts, and a failed
    read-back puts it back in the store (X-004)."""
    crash_at(monkeypatch, "mid-4")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert run["moved"] == [] and (backup_dir(env) / "moved" / "feedback_a.md").exists()
    [note] = marked_notes(env, "feedback_a")
    note.write_text(note.read_text().replace("Line one of alpha.", "Line one of alpha?"))
    assert main(["apply", "--resume", str(backup_dir(env))]) == 1
    assert (env["store"] / "feedback_a.md").read_text() == entry_text(*ENTRIES["feedback_a"])
    assert not (backup_dir(env) / "moved" / "feedback_a.md").exists()
    run = json.loads((backup_dir(env) / "run.json").read_text())
    assert sorted(run["moved"]) == ["feedback_b", "project_c"]


def test_resume_of_a_finished_run_says_nothing_to_do(env, capsys):
    assert run_apply(env, "--apply")[0] == 0
    before = trees(env)
    capsys.readouterr()
    assert main(["apply", "--resume", str(backup_dir(env))]) == 0
    out = capsys.readouterr().out
    assert "resuming from state: done" in out and "nothing to do" in out
    assert trees(env) == before


def test_every_backup_dir_is_complete(env, monkeypatch):
    """The first moment a backup dir exists it has run.json, manifest.json and the copy, and no note yet."""
    crash_at(monkeypatch, "backup")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    bdir = backup_dir(env)
    assert json.loads((bdir / "run.json").read_text())["state"] == "backed-up"
    assert (bdir / "manifest.json").is_file() and (bdir / "full-copy" / "MEMORY.md").is_file()
    assert marked_notes(env, "feedback_a") == []


def test_notes_name_a_backup_dir_that_exists(env, monkeypatch):
    seen = []

    def check(name):
        if name == "notes":
            [note] = marked_notes(env, "feedback_a")
            seen.append(f"`{backup_dir(env)}`" in note.read_text() and (backup_dir(env) / "run.json").is_file())

    monkeypatch.setattr(mc, "_checkpoint", check)
    assert run_apply(env, "--apply")[0] == 0
    restore()
    assert seen == [True]


def _cli_from(env, cwd, store, *extra):
    plan = env["tmp"] / f"plan-{store.parent.name}.json"
    plan.write_text(json.dumps(dict(version=1, store=str(store), items=ITEMS[:1])))
    return subprocess.run(
        [sys.executable, str(SCRIPT), "apply", "--plan", str(plan), "--store", str(store), "--vault",
         str(env["vault"]), "--insights-folder", "claude-insights", "--backup-root", str(env["backups"]), *extra],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd=cwd, timeout=60,
        env={**os.environ, "HOME": str(env["tmp"])})


def test_default_project_comes_from_the_stores_project_not_the_cwd(env):
    """--store for project B from a cwd in project A tags notes with B and keeps A's note (X-011/X-023)."""
    tmp = env["tmp"]
    cwd_a = tmp / "projA"
    cwd_a.mkdir()
    store_a = tmp / ".claude/projects" / re.sub("[^A-Za-z0-9]", "-", str(cwd_a)) / "memory"
    store_a.mkdir(parents=True)
    (store_a / "MEMORY.md").write_text(INDEX)
    (store_a / "feedback_a.md").write_text(entry_text("Alpha", "A's description", "A's body."))
    cwd_b = tmp / "projB"
    cwd_b.mkdir()
    (env["store"].parent / "session.jsonl").write_text(json.dumps(dict(cwd=str(cwd_b))) + "\n")

    first = _cli_from(env, cwd_a, store_a, "--apply")  # cwd in A, store A: the cwd's own project
    assert first.returncode == 0, first.stdout + first.stderr
    result = _cli_from(env, cwd_a, env["store"], "--apply")  # cwd in A, store B
    assert result.returncode == 0, result.stdout + result.stderr
    notes = marked_notes(env, "feedback_a")
    assert len(notes) == 2
    projects = sorted(re.search(r"^project: (.+)$", n.read_text(), re.M).group(1) for n in notes)
    assert projects == ["proja", "projb"]
    [b_note] = [n for n in notes if "project: projb\n" in n.read_text()]
    assert "claude/project/projb" in b_note.read_text() and "Alpha description" in b_note.read_text()


def test_default_project_without_a_transcript_asks_for_project(env):
    elsewhere = env["tmp"] / "elsewhere"
    elsewhere.mkdir()
    result = _cli_from(env, elsewhere, env["store"], "--apply")
    assert result.returncode == 2 and "pass --project" in result.stderr
    assert marked_notes(env, "feedback_a") == []


def test_a_note_failing_after_the_index_edit_stops_instead_of_restoring(env, monkeypatch, capsys):
    crash_at(monkeypatch, "index-written")
    with pytest.raises(Crash):
        run_apply(env, "--apply")
    restore()
    target = env["vault"] / TARGET
    saved = target.read_text(encoding="utf-8")
    target.unlink()
    moved = backup_dir(env) / "moved" / "feedback_b.md"
    assert moved.exists()
    capsys.readouterr()
    code = main(["apply", "--resume", str(backup_dir(env))])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error: feedback_b: its note failed after MEMORY.md was edited (")
    assert f"the original is still in {moved} and its old index line is in run.json dropped_lines" in err
    assert moved.exists() and not (env["store"] / "feedback_b.md").exists()
    assert json.loads((backup_dir(env) / "run.json").read_text())["state"] != "done"
    target.write_text(saved, encoding="utf-8")
    assert main(["apply", "--resume", str(backup_dir(env))]) == 0
    assert_finished(env)
