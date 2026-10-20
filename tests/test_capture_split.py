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


def _front(ntype="claude-insight", topic="ledgers"):
    return f"---\ntype: {ntype}\ndate: 2026-10-01\ntopic: {topic}\ntags:\n  - claude/insight\n---\n\n"


def _section(cid, title, body, date="2026-10-02", marker=True):
    end = f"\n{capture_save.end_marker(cid)}\n" if marker else ""
    return f"\n## Update ({date}) — auto-capture {cid}\n\n**{title}**\n\n{body}\n{end}"


def _update_row(cid, sid, rel, title, date="2026-10-02", ctype="insight"):
    capture_state.ledger_append({"id": cid, "session_id": sid, "project": "demo", "type": ctype, "title": title,
                                 "status": "active", "kind": "updated", "note_path": rel,
                                 "update_heading": f"## Update ({date}) — auto-capture {cid}"})


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
    report = capture_split.split_updates(mock_config, apply=False)
    assert _snapshot(hub.parent.parent) == before
    assert _moves(report) == {"aaaaaaaaaaa1": "same-session", "aaaaaaaaaaa2": "same-session",
                              "aaaaaaaaaaa3": "same-session", "bbbbbbbbbbb2": "low-cosine"}
    assert report["totals"]["moves"] == 4
    assert report["totals"]["same-session"] == 3 and report["totals"]["low-cosine"] == 1
    assert not list(capture_state.capture_dir().glob("split-backup-*"))


def test_apply_moves_sections_and_keeps_the_rest(mock_config, hub, hand):
    report = capture_split.split_updates(mock_config, apply=True)
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
    assert f"source_session: {OTHER}" in text and "  - claude/topic/ledgers" in text
    body = text.split("---", 2)[2]
    assert body == f"\n\n# {PELICAN[0]}\n\n{PELICAN[1]}\n"  # no heading, bold title or end marker
    assert capture_state.ledger_get("bbbbbbbbbbb1")["kind"] == "updated"


def test_split_note_date_comes_from_the_heading(mock_config, hub):
    capture_split.split_updates(mock_config, apply=True)
    row = capture_state.ledger_get("aaaaaaaaaaa2")
    assert row["note_path"].split("/")[-1].startswith("2025-12-31-osprey-lantern")
    assert "date: 2025-12-31" in (hub.parent.parent / row["note_path"]).read_text()


def test_apply_backs_up_each_touched_note(mock_config, hub, hand):
    before = hub.read_bytes()
    capture_split.split_updates(mock_config, apply=True)
    (backup,) = capture_state.capture_dir().glob("split-backup-*")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    saved = backup / "claude-insights__2026-10-01-hub-1111.md"
    assert saved.read_bytes() == before and stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert len(list(backup.iterdir())) == 2


def test_second_apply_changes_nothing(mock_config, hub, hand):
    capture_split.split_updates(mock_config, apply=True)
    before = _snapshot(hub.parent.parent)
    report = capture_split.split_updates(mock_config, apply=True)
    assert _snapshot(hub.parent.parent) == before
    assert report["totals"]["moves"] == 0 and report["totals"]["repairs"] == 0
    assert len(list(capture_state.capture_dir().glob("split-backup-*"))) == 1


def test_reject_still_works_on_a_moved_capture(mock_config, hub):
    capture_split.split_updates(mock_config, apply=True)
    new = hub.parent.parent / capture_state.ledger_get("aaaaaaaaaaa1")["note_path"]
    row = capture_reject.reject("aaaaaaaaaaa1", mock_config)
    assert row["status"] == "rejected" and not new.exists()
    assert (hub.parent / "_rejected" / new.name).exists()


def test_failed_rewrite_leaves_a_duplicate_that_a_rerun_repairs(mock_config, hand, monkeypatch):
    before = hand.read_bytes()
    with monkeypatch.context() as m:  # never monkeypatch.undo(): it would drop conftest's isolation too
        m.setattr(capture_split.note_writer, "_atomic_rewrite", lambda *a, **k: "write failed: disk full")
        report = capture_split.split_updates(mock_config, apply=True)
    assert report["totals"]["failed"] == 1 and "disk full" in report["notes"][0]["error"]
    assert hand.read_bytes() == before  # the source is never half-written
    moved = hand.parent.parent / capture_state.ledger_get("bbbbbbbbbbb2")["note_path"]
    assert moved.exists()  # a duplicate, never a loss
    report = capture_split.split_updates(mock_config, apply=True)
    assert report["totals"]["repairs"] == 1 and report["totals"]["moves"] == 0
    assert PELICAN[1] not in hand.read_text() and "A user-written update stays." in hand.read_text()
    assert len([p for p in hand.parent.glob("*pelican*")]) == 1


def test_failed_note_write_keeps_the_section_in_the_source(mock_config, hand, monkeypatch):
    before = hand.read_bytes()
    monkeypatch.setattr(capture_split.note_writer, "run_write", lambda *a, **k: 1)
    report = capture_split.split_updates(mock_config, apply=True)
    assert report["totals"]["failed"] == 1 and "write failed" in report["notes"][0]["error"]
    assert hand.read_bytes() == before
    assert capture_state.ledger_get("bbbbbbbbbbb2")["kind"] == "updated"


def test_edit_made_during_the_split_is_never_overwritten(mock_config, hand, monkeypatch):
    """A writer that skips the lock (Obsidian) edits the note mid-split: the rewrite refuses."""
    real = capture_split._write_split_note

    def edit_then_write(*a, **k):
        with hand.open("a") as fh:
            fh.write("\nTyped in Obsidian meanwhile.\n")
        return real(*a, **k)

    with monkeypatch.context() as m:
        m.setattr(capture_split, "_write_split_note", edit_then_write)
        report = capture_split.split_updates(mock_config, apply=True)
    assert report["totals"]["failed"] == 1 and "changed on disk" in report["notes"][0]["error"]
    assert "Typed in Obsidian meanwhile." in hand.read_text()
    capture_split.split_updates(mock_config, apply=True)  # the repair finishes the job
    assert "Typed in Obsidian meanwhile." in hand.read_text() and PELICAN[1] not in hand.read_text()


def test_repair_keeps_the_section_when_the_new_note_is_gone(mock_config, hand, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(capture_split.note_writer, "_atomic_rewrite", lambda *a, **k: "write failed")
        capture_split.split_updates(mock_config, apply=True)
    (hand.parent.parent / capture_state.ledger_get("bbbbbbbbbbb2")["note_path"]).unlink()
    report = capture_split.split_updates(mock_config, apply=True)
    assert PELICAN[1] in hand.read_text()
    assert report["totals"]["repairs"] == 0 and report["skipped"][0]["reason"] == "split-note-missing"


def test_locked_note_fails_and_writes_nothing(mock_config, hub):
    before = _snapshot(hub.parent.parent)
    lock = hub.parent / f".{hub.name}.ob-lock"
    lock.write_text("123\n")
    report = capture_split.split_updates(mock_config, apply=True)
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
    report = capture_split.split_updates(mock_config, apply=True)
    reasons = {s["id"]: s["reason"] for s in report["skipped"]}
    assert reasons == {"ccccccccccc1": "note-missing", "ccccccccccc2": "section-missing",
                       "ccccccccccc3": "outside-vault"}
    assert report["totals"]["failed"] == 0


def test_old_unmarked_section_moves_up_to_the_next_heading(mock_config, vault):
    rel = "claude-insights/old.md"
    (vault / rel).write_text(_front() + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n"
                             + _section("ddddddddddd1", *PELICAN, marker=False) + "\n## Later\n\nTail stays.\n")
    _update_row("ddddddddddd1", OTHER, rel, PELICAN[0])
    capture_split.split_updates(mock_config, apply=True)
    text = (vault / rel).read_text()
    assert PELICAN[1] not in text and "## Later\n\nTail stays." in text
    new = (vault / capture_state.ledger_get("ddddddddddd1")["note_path"]).read_text()
    assert PELICAN[1] in new and "Tail stays" not in new


def test_crlf_note_keeps_its_line_endings(mock_config, hand):
    hand.write_bytes(hand.read_bytes().replace(b"\n", b"\r\n"))
    capture_split.split_updates(mock_config, apply=True)
    data = hand.read_bytes()
    assert PELICAN[1].encode() not in data and b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_rejected_rows_never_move(mock_config, hub):
    capture_state.ledger_update("aaaaaaaaaaa1", status="rejected")
    assert "aaaaaaaaaaa1" not in _moves(capture_split.split_updates(mock_config, apply=False))


def test_report_counts_what_stays_per_session(mock_config, hand):
    report = capture_split.split_updates(mock_config, apply=False)
    assert report["totals"]["max_kept_from_one_session"] == 1


# --- CLI ---------------------------------------------------------------------

def _cli(capsys, *argv):
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
    assert _moves(capture_split.split_updates(mock_config)) == {"eeeeeeeeeee1": "low-cosine"}


def test_bad_heading_date_falls_back_to_the_capture_time(mock_config, vault):
    rel = "claude-insights/baddate.md"
    (vault / rel).write_text(_front() + f"# {ZEBRA[0]}\n\n{ZEBRA[1]}\n" + _section("eeeeeeeeeee2", *PELICAN, date="2026-13-45"))
    capture_state.ledger_append({"id": "eeeeeeeeeee2", "session_id": OTHER, "project": "demo", "type": "decision",
                                 "title": PELICAN[0], "status": "active", "kind": "updated", "note_path": rel,
                                 "update_heading": "## Update (2026-13-45) — auto-capture eeeeeeeeeee2",
                                 "ts": 1767268800.0})  # 2026-01-01 12:00 UTC
    capture_split.split_updates(mock_config, apply=True)
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
