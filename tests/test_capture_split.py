"""Tests for hooks/capture_split.py: moving snowballed auto-capture updates to their own notes (#427)."""

import json
import stat

import pytest

import capture_cli
import capture_reject
import capture_save
import capture_split
import capture_state

S = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"

ZEBRA = ("Zebra quokka ledger rotation",
         "The zebra quokka ledger rotates at one hundred kilobytes and keeps one backup file named dot one.")
ZEBRA_MORE = ("Zebra quokka ledger rotation backup",
              "The zebra quokka ledger backup file dot one is replaced on each rotation at one hundred kilobytes.")
PELICAN = ("Pelican harbour cron jitter",
           "Pelican harbour scheduling uses cron with random jitter so nightly jobs never collide on the hour.")
OSPREY = ("Osprey lantern websocket retries",
          "Osprey lantern websocket clients retry with exponential backoff capped at thirty seconds.")


def _front(ntype="claude-insight", topic="ledgers", extra=""):
    """The templates' real shape: the topic lives only in the ``claude/topic/<x>`` tag, never a ``topic:`` key."""
    return (f"---\ntype: {ntype}\ndate: 2026-10-01\n{extra}project: demo\ntags:\n  - claude/insight\n"
            f"  - claude/project/demo\n  - claude/topic/{topic}\n---\n\n")


def _index(vault):
    capture_save.vault_index.ensure_index(str(vault), ["claude-sessions", "claude-insights"],
                                          db_path=capture_save.vault_index._default_db_path())


def _split(config, apply=False):
    """split_updates with the index built first, as on a real machine (the dry run never builds it)."""
    _index(config["vault_path"])
    return capture_split.split_updates(config, apply=apply)


def _section(cid, title, body, date="2026-10-02", marker=True):
    end = f"\n{capture_save.end_marker(cid)}\n" if marker else ""
    return f"\n## Update ({date}) — auto-capture {cid}\n\n**{title}**\n\n{body}\n{end}"


def _update_row(cid, sid, rel, title, date="2026-10-02", ctype="insight"):
    capture_state.ledger_append({"id": cid, "session_id": sid, "project": "demo", "type": ctype, "title": title,
                                 "status": "active", "kind": "updated", "note_path": rel,
                                 "update_heading": f"## Update ({date}) — auto-capture {cid}"})


_CFG: dict = {}


@pytest.fixture(autouse=True)
def _remember_config(mock_config):
    _CFG["config"] = mock_config  # the live dict: a test that edits it is seen by _cli


@pytest.fixture
def vault(mock_config, tmp_vault):
    for i in range(5):
        (tmp_vault / "claude-insights" / f"filler-{i}.md").write_text(
            _front() + f"# Filler {i} topic\n\nUnrelated filler words number {i} about gardening tomatoes and soil.\n")
    return tmp_vault


@pytest.fixture
def hub(vault):
    """An auto note S created, grown by three of S's updates."""
    rel = "claude-insights/2026-10-01-hub-1111.md"
    (vault / rel).write_text(_front() + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n"
                             + _section("aaaaaaaaaaa1", *PELICAN)
                             + _section("aaaaaaaaaaa2", *OSPREY, date="2025-12-31")
                             + _section("aaaaaaaaaaa3", *ZEBRA_MORE))
    capture_state.ledger_append({"id": "000000000001", "session_id": S, "project": "demo", "type": "insight",
                                 "title": ZEBRA[0], "status": "active", "kind": "new", "note_path": rel})
    _update_row("aaaaaaaaaaa1", S, rel, PELICAN[0])
    _update_row("aaaaaaaaaaa2", S, rel, OSPREY[0], date="2025-12-31")
    _update_row("aaaaaaaaaaa3", S, rel, ZEBRA_MORE[0])
    return vault / rel


@pytest.fixture
def hand(vault):
    """A hand-written note: one refining update from OTHER, one off-topic, one user-written update."""
    rel = "claude-insights/hand-written.md"
    (vault / rel).write_text(_front() + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n"
                             + _section("bbbbbbbbbbb1", *ZEBRA_MORE)
                             + "typed after the marker\n"
                             + _section("bbbbbbbbbbb2", *PELICAN)
                             + "\n## Update (2026-10-03)\n\nA user-written update stays.\n")
    _update_row("bbbbbbbbbbb1", OTHER, rel, ZEBRA_MORE[0])
    _update_row("bbbbbbbbbbb2", OTHER, rel, PELICAN[0])
    return vault / rel


def _snapshot(vault):
    files = {str(p.relative_to(vault)): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    ledger = capture_state.capture_dir() / capture_state.LEDGER_NAME
    return files, ledger.read_bytes()


def _moves(report):
    return {m["id"]: m["reason"] for n in report["notes"] for m in n["moves"]}


def test_dry_run_lists_moves_and_changes_nothing(mock_config, hub, hand):
    before = _snapshot(hub.parent.parent)
    report = _split(mock_config, apply=False)
    assert _snapshot(hub.parent.parent) == before
    assert _moves(report) == {"aaaaaaaaaaa1": "same-session", "aaaaaaaaaaa2": "same-session",
                              "aaaaaaaaaaa3": "same-session", "bbbbbbbbbbb2": "low-cosine"}
    assert report["totals"]["moves"] == 4
    assert report["totals"]["same-session"] == 3 and report["totals"]["low-cosine"] == 1
    assert not list(capture_state.capture_dir().glob("split-backup-*"))


def test_apply_moves_sections_and_keeps_the_rest(mock_config, hub, hand):
    report = _split(mock_config, apply=True)
    assert report["totals"]["failed"] == 0
    hub_text, hand_text = hub.read_text(), hand.read_text()
    assert "auto-capture" not in hub_text and ZEBRA[1] in hub_text
    assert "bbbbbbbbbbb1" in hand_text and "bbbbbbbbbbb2" not in hand_text and PELICAN[1] not in hand_text
    assert "typed after the marker" in hand_text and "A user-written update stays." in hand_text
    row = capture_state.ledger_get("bbbbbbbbbbb2")
    assert row["kind"] == "new" and row["split_from"] == "claude-insights/hand-written.md"
    assert row["update_heading"] is None and row["split_heading"].endswith("auto-capture bbbbbbbbbbb2")
    new = hub.parent.parent / row["note_path"]
    text = new.read_text()
    assert text.startswith("---\n") and "type: claude-insight" in text and "source: auto-capture" in text
    assert "status: provisional" not in text  # #455: a migrated note is not a new capture
    assert f"source_session: {OTHER}" in text and "  - claude/topic/ledgers" in text
    body = text.split("---", 2)[2]
    assert body == f"\n\n# {PELICAN[0]}\n\n{PELICAN[1]}\n"  # no heading, bold title or end marker
    assert capture_state.ledger_get("bbbbbbbbbbb1")["kind"] == "updated"


def test_split_note_date_comes_from_the_heading(mock_config, hub):
    _split(mock_config, apply=True)
    row = capture_state.ledger_get("aaaaaaaaaaa2")
    assert row["note_path"].split("/")[-1].startswith("2025-12-31-osprey-lantern")
    assert "date: 2025-12-31" in (hub.parent.parent / row["note_path"]).read_text()


def test_apply_backs_up_each_touched_note(mock_config, hub, hand):
    before = hub.read_bytes()
    _split(mock_config, apply=True)
    (backup,) = capture_state.capture_dir().glob("split-backup-*")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    saved = backup / "claude-insights__2026-10-01-hub-1111.md"
    assert saved.read_bytes() == before and stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert len(list(backup.iterdir())) == 2


def test_second_apply_changes_nothing(mock_config, hub, hand):
    _split(mock_config, apply=True)
    before = _snapshot(hub.parent.parent)
    report = _split(mock_config, apply=True)
    assert _snapshot(hub.parent.parent) == before
    assert report["totals"]["moves"] == 0 and report["totals"]["repairs"] == 0
    assert len(list(capture_state.capture_dir().glob("split-backup-*"))) == 1


def test_reject_still_works_on_a_moved_capture(mock_config, hub):
    _split(mock_config, apply=True)
    new = hub.parent.parent / capture_state.ledger_get("aaaaaaaaaaa1")["note_path"]
    row = capture_reject.reject("aaaaaaaaaaa1", mock_config)
    assert row["status"] == "rejected" and not new.exists()
    assert (hub.parent / "_rejected" / new.name).exists()


def test_failed_rewrite_leaves_a_duplicate_that_a_rerun_repairs(mock_config, hand, monkeypatch):
    before = hand.read_bytes()
    with monkeypatch.context() as m:  # never monkeypatch.undo(): it would drop conftest's isolation too
        m.setattr(capture_split.note_writer, "_atomic_rewrite", lambda *a, **k: "write failed: disk full")
        report = _split(mock_config, apply=True)
    assert report["totals"]["failed"] == 1 and "disk full" in report["notes"][0]["error"]
    assert hand.read_bytes() == before  # the source is never half-written
    moved = hand.parent.parent / capture_state.ledger_get("bbbbbbbbbbb2")["note_path"]
    assert moved.exists()  # a duplicate, never a loss
    report = _split(mock_config, apply=True)
    assert report["totals"]["repairs"] == 1 and report["totals"]["moves"] == 0
    _consistent(hand.parent.parent, hand, "bbbbbbbbbbb2", "*pelican*")


def _consistent(vault, hand, cid, title_glob):
    """After a repair: one split note, its ledger row points at it, the source no longer has it, Reject works."""
    row = capture_state.ledger_get(cid)
    assert row["kind"] == "new" and (vault / row["note_path"]).is_file()
    assert len(list(hand.parent.glob(title_glob))) == 1  # no duplicate
    assert cid not in hand.read_text() and "A user-written update stays." in hand.read_text()
    assert capture_reject.reject(cid, {"vault_path": str(vault), "insights_folder": "claude-insights"})["status"] == "rejected"
    assert not (vault / row["note_path"]).exists()


def test_crash_after_ledger_row_before_note_write_is_repaired(mock_config, hand, monkeypatch):
    """Crash point 1: the ledger already names the new note, the note was never written."""
    before = hand.read_bytes()
    with monkeypatch.context() as m:
        m.setattr(capture_split.note_writer, "run_write", lambda *a, **k: 1)
        report = _split(mock_config, apply=True)
    assert report["totals"]["failed"] == 1 and report["totals"]["moves"] == 0  # nothing was written
    assert "write failed" in report["notes"][0]["errors"][0]["error"]
    assert hand.read_bytes() == before
    row = capture_state.ledger_get("bbbbbbbbbbb2")
    assert row["kind"] == "new" and not (hand.parent.parent / row["note_path"]).exists()
    report = _split(mock_config, apply=True)
    assert report["totals"]["repairs"] == 1 and report["totals"]["failed"] == 0
    assert PELICAN[1] in (hand.parent.parent / row["note_path"]).read_text()  # written from the section
    _consistent(hand.parent.parent, hand, "bbbbbbbbbbb2", "*pelican*")


def test_crash_before_the_ledger_row_moves_cleanly_next_time(mock_config, hand, monkeypatch):
    """Crash point 0: ledger_update fails; nothing was written for that move."""
    before = hand.read_bytes()

    def boom(*a, **k):
        raise OSError("ledger unwritable")

    with monkeypatch.context() as m:
        m.setattr(capture_split.capture_state, "ledger_update", boom)
        report = _split(mock_config, apply=True)
    assert report["totals"]["failed"] == 1 and hand.read_bytes() == before
    assert capture_state.ledger_get("bbbbbbbbbbb2")["kind"] == "updated" and not list(hand.parent.glob("*pelican*"))
    report = _split(mock_config, apply=True)
    assert report["totals"]["moves"] == 1 and report["totals"]["failed"] == 0
    _consistent(hand.parent.parent, hand, "bbbbbbbbbbb2", "*pelican*")


def test_one_failing_move_does_not_block_the_rest(mock_config, hub, monkeypatch):
    real = capture_split._write_split_note

    def fail_osprey(vault, rel, content):
        if "osprey" in rel:
            raise OSError("disk full")
        return real(vault, rel, content)

    with monkeypatch.context() as m:
        m.setattr(capture_split, "_write_split_note", fail_osprey)
        report = _split(mock_config, apply=True)
    assert report["totals"]["moves"] == 2 and report["totals"]["failed"] == 1
    text = hub.read_text()
    assert "aaaaaaaaaaa1" not in text and "aaaaaaaaaaa3" not in text  # rewritten without the moves that worked
    assert OSPREY[1] in text  # the failed one stays, and the next run repairs it
    report = _split(mock_config, apply=True)
    assert report["totals"]["repairs"] == 1 and OSPREY[1] not in hub.read_text()


def test_null_title_moves_as_untitled(mock_config, vault):
    rel = "claude-insights/nulltitle.md"
    (vault / rel).write_text(_front() + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n" + _section("eeeeeeeeeee4", "Untitled capture", PELICAN[1]))
    capture_state.ledger_append({"id": "eeeeeeeeeee4", "session_id": OTHER, "project": "demo", "type": "insight",
                                 "title": None, "status": "active", "kind": "updated", "note_path": rel,
                                 "update_heading": "## Update (2026-10-02) — auto-capture eeeeeeeeeee4"})
    report = _split(mock_config, apply=True)
    assert report["totals"]["failed"] == 0 and report["totals"]["moves"] == 1
    new = (vault / capture_state.ledger_get("eeeeeeeeeee4")["note_path"]).read_text()
    assert "# Untitled capture\n\n" + PELICAN[1] in new and "**Untitled capture**" not in new


def test_edit_made_during_the_split_is_never_overwritten(mock_config, hand, monkeypatch):
    """A writer that skips the lock (Obsidian) edits the note mid-split: the rewrite refuses."""
    real = capture_split._write_split_note

    def edit_then_write(*a, **k):
        with hand.open("a") as fh:
            fh.write("\nTyped in Obsidian meanwhile.\n")
        return real(*a, **k)

    with monkeypatch.context() as m:
        m.setattr(capture_split, "_write_split_note", edit_then_write)
        report = _split(mock_config, apply=True)
    assert report["totals"]["failed"] == 1 and "changed on disk" in report["notes"][0]["error"]
    assert "Typed in Obsidian meanwhile." in hand.read_text()
    _split(mock_config, apply=True)  # the repair finishes the job
    assert "Typed in Obsidian meanwhile." in hand.read_text() and PELICAN[1] not in hand.read_text()


def test_repair_rewrites_a_split_note_that_is_gone(mock_config, hand, monkeypatch):
    """Crash point 2 with the split note deleted meanwhile: the leftover section is the only copy, so it is rewritten."""
    with monkeypatch.context() as m:
        m.setattr(capture_split.note_writer, "_atomic_rewrite", lambda *a, **k: "write failed")
        _split(mock_config, apply=True)
    (hand.parent.parent / capture_state.ledger_get("bbbbbbbbbbb2")["note_path"]).unlink()
    report = _split(mock_config, apply=True)
    assert report["totals"]["repairs"] == 1 and report["totals"]["failed"] == 0
    _consistent(hand.parent.parent, hand, "bbbbbbbbbbb2", "*pelican*")


def test_locked_note_fails_and_writes_nothing(mock_config, hub):
    before = _snapshot(hub.parent.parent)
    lock = hub.parent / f".{hub.name}.ob-lock"
    lock.write_text("123\n")
    report = _split(mock_config, apply=True)
    assert report["totals"]["failed"] == 1 and "another process" in report["notes"][0]["error"]
    assert lock.read_text() == "123\n"  # another writer's lock is never taken over or removed
    lock.unlink()
    assert _snapshot(hub.parent.parent) == before
    assert not list(capture_state.capture_dir().glob("split-backup-*"))


def test_missing_note_and_missing_section_are_skipped(mock_config, vault):
    _update_row("ccccccccccc1", OTHER, "claude-insights/gone.md", "gone")
    (vault / "claude-insights" / "edited.md").write_text(_front() + "# Edited\n\nThe user removed the update.\n")
    _update_row("ccccccccccc2", OTHER, "claude-insights/edited.md", "removed")
    _update_row("ccccccccccc3", OTHER, "../outside.md", "escape")
    report = _split(mock_config, apply=True)
    reasons = {s["id"]: s["reason"] for s in report["skipped"]}
    assert reasons == {"ccccccccccc1": "note-missing", "ccccccccccc2": "section-missing",
                       "ccccccccccc3": "outside-vault"}
    assert report["totals"]["failed"] == 0


def test_old_unmarked_section_moves_up_to_the_next_heading(mock_config, vault):
    rel = "claude-insights/old.md"
    (vault / rel).write_text(_front() + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n"
                             + _section("ddddddddddd1", *PELICAN, marker=False) + "\n## Later\n\nTail stays.\n")
    _update_row("ddddddddddd1", OTHER, rel, PELICAN[0])
    _split(mock_config, apply=True)
    text = (vault / rel).read_text()
    assert PELICAN[1] not in text and "## Later\n\nTail stays." in text
    new = (vault / capture_state.ledger_get("ddddddddddd1")["note_path"]).read_text()
    assert PELICAN[1] in new and "Tail stays" not in new


def test_crlf_note_keeps_its_line_endings(mock_config, hand):
    hand.write_bytes(hand.read_bytes().replace(b"\n", b"\r\n"))
    _split(mock_config, apply=True)
    data = hand.read_bytes()
    assert PELICAN[1].encode() not in data and b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_rejected_rows_never_move(mock_config, hub):
    capture_state.ledger_update("aaaaaaaaaaa1", status="rejected")
    assert "aaaaaaaaaaa1" not in _moves(_split(mock_config, apply=False))


def test_report_counts_what_stays_per_session(mock_config, hand):
    """Kept on-topic updates: 2 from OTHER and 1 from THIRD on one note -> 2, not 3 (per session, not per note)."""
    third = "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee"
    rel = "claude-insights/hand-written.md"
    with hand.open("a") as fh:
        fh.write(_section("bbbbbbbbbbb3", *ZEBRA_MORE, date="2026-10-04") + _section("bbbbbbbbbbb4", *ZEBRA_MORE, date="2026-10-05"))
    _update_row("bbbbbbbbbbb3", OTHER, rel, ZEBRA_MORE[0], date="2026-10-04")
    _update_row("bbbbbbbbbbb4", third, rel, ZEBRA_MORE[0], date="2026-10-05")
    report = _split(mock_config, apply=False)
    assert report["totals"]["max_kept_from_one_session"] == 2


def _exact_cosine(monkeypatch):
    """The PELICAN section on ``hand`` scores exactly 0.6: query {a:1} against note {a:3, b:4}."""
    monkeypatch.setattr(capture_save.vault_index, "compute_query_vector",
                        lambda db, q: {"a": 1.0} if q.startswith(PELICAN[0]) else {"a": 3.0, "b": 4.0})


def test_split_threshold_boundary_matches_find_match(mock_config, hand, monkeypatch):
    """``>=`` keeps, as in find_match: exactly at the configured threshold stays; just above it moves."""
    _index(mock_config["vault_path"])
    _exact_cosine(monkeypatch)
    at = capture_split.split_updates({**mock_config, "auto_capture_update_min_cosine": 0.6})
    assert "bbbbbbbbbbb2" not in _moves(at) and at["threshold"] == 0.6
    above = capture_split.split_updates({**mock_config, "auto_capture_update_min_cosine": 0.61})
    assert _moves(above)["bbbbbbbbbbb2"] == "low-cosine" and above["threshold"] == 0.61


def test_split_uses_the_shared_scorer(mock_config, hand, monkeypatch):
    calls = []
    real = capture_save.original_vector
    monkeypatch.setattr(capture_save, "original_vector", lambda db, note: calls.append(note) or real(db, note))
    _split(mock_config)
    assert calls and all(c.name == "hand-written.md" for c in calls)


def test_dry_run_without_an_index_exits_two_and_builds_nothing(mock_config, hand, capsys):
    db = capture_save.Path(capture_save.vault_index._default_db_path())
    assert not db.exists()
    rc, out = _cli(capsys, index=False)
    assert rc == 2 and "index missing" in out.err and "/vault-reindex" in out.err
    assert not db.exists()
    rc, out = _cli(capsys, "--json", index=False)
    assert rc == 2 and "index missing" in json.loads(out.out)["error"]
    assert _cli(capsys, "--apply", index=False)[0] == 0 and db.exists()  # --apply may build it


def test_unreadable_index_fails_before_any_write(mock_config, hub, hand, capsys):
    db = capture_save.Path(capture_save.vault_index._default_db_path())
    db.write_bytes(b"\x00garbage" * 500)
    before = _snapshot(hub.parent.parent)
    for argv in ((), ("--apply",)):
        rc, out = _cli(capsys, *argv, index=False)
        assert rc == 1 and out.err.startswith("[obsidian-brain] capture split-updates: ") and "Traceback" not in out.err
    rc, out = _cli(capsys, "--apply", "--json", index=False)
    assert rc == 1 and "error" in json.loads(out.out)
    assert _snapshot(hub.parent.parent) == before and not list(capture_state.capture_dir().glob("split-backup-*"))


def test_nothing_to_do_skips_exit_zero_but_are_listed(mock_config, vault, capsys):
    _update_row("ccccccccccc9", OTHER, "claude-insights/gone.md", "gone")
    rc, out = _cli(capsys, "--apply")
    assert rc == 0 and "skipped ccccccccccc9: note-missing" in out.out


# --- CLI ---------------------------------------------------------------------

def _cli(capsys, *argv, index=True):
    vault_path = _CFG["config"].get("vault_path")
    if index and vault_path and capture_save.Path(vault_path).is_dir():
        _index(vault_path)
    rc = capture_cli.main(["split-updates", *argv])
    return rc, capsys.readouterr()


def test_cli_dry_run_text_and_json(mock_config, hub, capsys):
    rc, out = _cli(capsys)
    assert rc == 0
    assert "claude-insights/2026-10-01-hub-1111.md: move 3 (same-session 3, low-cosine 0)" in out.out
    assert "would move 3 sections from 1 note" in out.out
    rc, out = _cli(capsys, "--json")
    assert rc == 0 and json.loads(out.out)["totals"]["moves"] == 3


def test_cli_noop_apply_exits_zero(mock_config, vault, capsys):
    assert _cli(capsys, "--apply")[0] == 0


def test_cli_apply_exits_one_when_a_note_fails(mock_config, hub, capsys):
    (hub.parent / f".{hub.name}.ob-lock").write_text("1\n")
    rc, out = _cli(capsys, "--apply")
    assert rc == 1 and "2026-10-01-hub-1111.md" in out.err


def test_cli_bad_config_exits_two(mock_config, capsys):
    mock_config["vault_path"] = "/nonexistent/vault"
    assert _cli(capsys)[0] == 2
    del mock_config["vault_path"]
    assert _cli(capsys)[0] == 2


def test_update_that_cannot_be_scored_moves(mock_config, vault):
    """An all-stopword update has no query vector; find_match would refuse it, so it moves."""
    rel = "claude-insights/scored.md"
    (vault / rel).write_text(_front() + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n" + _section("eeeeeeeeeee1", "the and", "of the and"))
    _update_row("eeeeeeeeeee1", OTHER, rel, "the and")
    assert _moves(_split(mock_config)) == {"eeeeeeeeeee1": "low-cosine"}


def test_bad_heading_date_falls_back_to_the_capture_time(mock_config, vault):
    rel = "claude-insights/baddate.md"
    (vault / rel).write_text(_front() + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n" + _section("eeeeeeeeeee2", *PELICAN, date="2026-13-45"))
    capture_state.ledger_append({"id": "eeeeeeeeeee2", "session_id": OTHER, "project": "demo", "type": "decision",
                                 "title": PELICAN[0], "status": "active", "kind": "updated", "note_path": rel,
                                 "update_heading": "## Update (2026-13-45) — auto-capture eeeeeeeeeee2",
                                 "ts": 1767268800.0})  # 2026-01-01 12:00 UTC
    _split(mock_config, apply=True)
    row = capture_state.ledger_get("eeeeeeeeeee2")
    text = (vault / row["note_path"]).read_text()
    day = capture_split.datetime.date.fromtimestamp(1767268800.0).isoformat()
    assert f"date: {day}" in text and "type: claude-decision" in text and "created_at: 2026-01-01T" in text


def test_unreadable_note_in_dry_run_is_reported(mock_config, vault, capsys):
    rel = "claude-insights/binary.md"
    (vault / rel).write_bytes(b"\xff\xfe not utf-8")
    _update_row("eeeeeeeeeee3", OTHER, rel, "x")
    rc, out = _cli(capsys)
    assert rc == 1 and "binary.md: move 0 (same-session 0, low-cosine 0) FAILED: UnicodeDecodeError" in out.out


def test_cli_apply_text_report(mock_config, hub, capsys):
    rc, out = _cli(capsys, "--apply")
    assert rc == 0 and "moved 3 sections from 1 note" in out.out and "backup: " in out.out


def test_updates_on_a_note_the_session_wrote_by_hand_move(mock_config, vault):
    """source_session in the frontmatter counts as the session's own note, even for an on-topic update."""
    rel = "claude-insights/by-hand.md"
    (vault / rel).write_text(_front(extra=f"source_session: {S}\n")
                             + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n" + _section("fffffffffff1", *ZEBRA_MORE)
                             + _section("fffffffffff2", *ZEBRA_MORE, date="2026-10-03"))
    _update_row("fffffffffff1", S, rel, ZEBRA_MORE[0])
    _update_row("fffffffffff2", OTHER, rel, ZEBRA_MORE[0], date="2026-10-03")
    assert _moves(_split(mock_config)) == {"fffffffffff1": "same-session"}


# --- #427 Codex survivors -------------------------------------------------------

def _leave_leftover(mock_config, monkeypatch):
    """A split whose source rewrite failed: row kind=new + split_from, section still in the source."""
    with monkeypatch.context() as m:
        m.setattr(capture_split.note_writer, "_atomic_rewrite", lambda *a, **k: "write failed")
        _split(mock_config, apply=True)


def test_reject_of_a_split_row_removes_the_leftover_section(mock_config, hand, monkeypatch):
    _leave_leftover(mock_config, monkeypatch)
    assert PELICAN[1] in hand.read_text()
    new = hand.parent.parent / capture_state.ledger_get("bbbbbbbbbbb2")["note_path"]
    capture_reject.reject("bbbbbbbbbbb2", mock_config)
    assert not new.exists() and (hand.parent / "_rejected" / new.name).exists()
    assert PELICAN[1] not in hand.read_text() and "bbbbbbbbbbb2" not in hand.read_text()
    assert "A user-written update stays." in hand.read_text() and "bbbbbbbbbbb1" in hand.read_text()
    report = _split(mock_config, apply=True)  # nothing left for a later run
    assert report["totals"]["repairs"] == 0 and report["totals"]["failed"] == 0


def test_reject_of_a_split_row_whose_note_was_never_written_keeps_the_section_in_rejected(mock_config, hand, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(capture_split.note_writer, "run_write", lambda *a, **k: 1)
        _split(mock_config, apply=True)
    row = capture_reject.reject("bbbbbbbbbbb2", mock_config)
    assert PELICAN[1] not in hand.read_text()
    assert PELICAN[1] in (hand.parent.parent / row["rejected_path"]).read_text()


def test_repair_removes_the_leftover_of_an_already_rejected_split_row(mock_config, hand, monkeypatch):
    """The other order: the row was rejected (by an older Reject) while its section was still in the source."""
    _leave_leftover(mock_config, monkeypatch)
    new = hand.parent.parent / capture_state.ledger_get("bbbbbbbbbbb2")["note_path"]
    capture_state.ledger_update("bbbbbbbbbbb2", status="rejected")
    new.unlink()  # gone with the rejection; the repair must not write it back
    report = _split(mock_config, apply=True)
    assert report["totals"]["repairs"] == 1 and report["totals"]["failed"] == 0
    assert PELICAN[1] not in hand.read_text() and not new.exists()
    assert capture_state.ledger_get("bbbbbbbbbbb2")["status"] == "rejected"


def test_reject_rereads_the_row_under_the_lock(mock_config, hand, monkeypatch):
    """A split --apply finishing between Reject's ledger read and its lock: Reject follows the fresh row."""
    _index(mock_config["vault_path"])
    real = capture_reject.note_writer._acquire_lock
    state = {"split": False}

    def split_first(path):
        if not state["split"]:
            state["split"] = True
            capture_split.split_updates(mock_config, apply=True)
        return real(path)

    monkeypatch.setattr(capture_reject.note_writer, "_acquire_lock", split_first)
    row = capture_reject.reject("bbbbbbbbbbb2", mock_config)
    assert state["split"] and row["status"] == "rejected"
    new = hand.parent.parent / row["note_path"]
    assert not new.exists() and (hand.parent / "_rejected" / new.name).exists()  # the split note did not stay live
    assert PELICAN[1] not in hand.read_text()


def test_topic_survives_a_title_containing_three_dashes(mock_config, vault):
    text = ('---\ntype: claude-insight\ntitle: "a---b"\ntags:\n  - claude/insight\n  - claude/topic/ledgers\n---\n\n# T\n')
    assert capture_split._topic(text) == "ledgers"
    assert capture_split._topic("# no frontmatter\n") == "general"
