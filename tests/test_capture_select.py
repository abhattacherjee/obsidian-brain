"""Tests for hooks/capture_select.py (#455): merit selection, sticky promotion, closed-issue demotion.

Every test uses a tmp vault and a tmp index; no test runs gh or claude.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import capture_save
import capture_select
import capture_state
import note_placement
import note_writer
import obsidian_utils
import vault_index

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "ffffffff-0000-1111-2222-333333333333"
CREATED = "2026-10-23T10:00:00+00:00"
CREATED_TS = 1792749600.0  # CREATED as epoch seconds
FOLDERS = ["claude-sessions", "claude-insights"]


def _words(i: int, n: int = 10) -> str:
    """``n`` tokens no other note index shares, so notes stay well apart in TF-IDF space."""
    return " ".join(f"w{chr(97 + i // 26)}{chr(97 + i % 26)}x{chr(97 + k)}" for k in range(n))


@pytest.fixture
def db(mock_config, tmp_path):
    """A tmp index seeded with 40 session notes that use the words every auto note shares (its tags, "note").

    Index IDF is computed as each note is inserted, so without a corpus the
    first few notes are near-twins by their shared tags alone.
    """
    sessions = Path(mock_config["vault_path"]) / "claude-sessions"
    for i in range(40):
        (sessions / f"s{i}.md").write_text("---\ntype: claude-session\nproject: demo\n---\n\n"
                                          "# note\n\nclaude insight decision error fix project demo topic auto note\n")
    return vault_index.ensure_index(mock_config["vault_path"], FOLDERS, db_path=str(tmp_path / "idx.db"))


def _note(config, i, *, score=5, ntype="insight", sid=SID, ts=None, status=None, body=None, created=CREATED,
          day="2026-10-23", extra_fm="", ledger=True, **row_fields):
    """Write auto note ``n<i>.md`` the way capture_save does, plus its ledger row; returns the row."""
    cand = {"type": ntype, "title": f"note {i}", "body": "", "topic": "t"}
    front = capture_save._frontmatter(cand, session_id=sid, project="demo", session_note="", today=day,
                                      created_at=created)
    if status is not None:
        front = front.replace("status: provisional", f"status: {status}" if status else "")
    if extra_fm:
        front = front.replace("source: auto-capture\n", f"source: auto-capture\n{extra_fm}")
    rel = f"claude-insights/n{i}.md"
    (Path(config["vault_path"]) / rel).write_text(front + f"\n# note {i}\n\n{body or _words(i)}\n")
    row = {"session_id": sid, "project": "demo", "type": ntype, "title": f"note {i}", "status": "active",
           "kind": "new", "note_path": rel, "score": score, "ts": CREATED_TS + i if ts is None else ts, **row_fields}
    if ledger:
        row["id"] = capture_state.ledger_append(row)
    return row


def _current_note_path(config, row):
    current = capture_state.ledger_get(row["id"]) if row.get("id") else None
    return Path(config["vault_path"]) / (current or row)["note_path"]


def _fm(config, row) -> dict:
    return obsidian_utils._peek_frontmatter_fields(
        _current_note_path(config, row),
        ("status", "capture_score", "capture_novelty", "capture_merit", "capture_rank", "promoted_by", "demoted_by"))


def _status(config, row):
    return _fm(config, row)["status"]


def _log_lines():
    path = Path.home() / ".claude" / obsidian_utils._HOOK_LOG_NAME
    return path.read_text().splitlines() if path.exists() else []


def _access(db, row, config, context, ts):
    conn = vault_index._connect(db)
    conn.execute("INSERT INTO access_log (note_path, timestamp, context_type, project) VALUES (?, ?, ?, ?)",
                 (str(Path(config["vault_path"]) / row["note_path"]), ts, context, "demo"))
    conn.commit()
    conn.close()


@pytest.fixture(params=["claude-insights", "knowledge/insights"])
def placement_env(tmp_path, monkeypatch, request):
    vault = tmp_path / "vault"
    folder = vault / request.param
    folder.mkdir(parents=True, mode=0o700)
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    monkeypatch.setattr(capture_state, "capture_dir", lambda: ledger)
    config = {"vault_path": str(vault), "insights_folder": request.param}
    db = vault_index.ensure_index(str(vault), [request.param], db_path=str(tmp_path / "placement.db"))
    return SimpleNamespace(config=config, vault=vault, folder=folder, db=db)


def _placement_note(env, status="provisional", *, in_provisional=True):
    path = env.folder / "_provisional" / "placed.md" if in_provisional else env.folder / "placed.md"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(f"---\ntype: claude-insight\nstatus: {status}\nproject: demo\n"
                    f"created_at: {CREATED}\ndate: 2026-10-23\nsource_session: {SID}\n"
                    "---\n\n# Placed\n\nA durable lesson.\n")
    path.chmod(0o600)
    assert vault_index.index_note(env.db, str(path))
    conn = vault_index._connect(env.db)
    try:
        conn.execute("INSERT INTO access_log (note_path, timestamp, context_type) VALUES (?, ?, 'search')",
                     (str(path), CREATED_TS - 1))
        conn.execute("INSERT INTO themes (id, name, note_count, created_date, updated_date) "
                     "VALUES (1, 'Placement', 1, '2026-10-23', '2026-10-23')")
        conn.execute("INSERT INTO theme_members (theme_id, note_path, similarity, added_date) "
                     "VALUES (1, ?, 0.8, '2026-10-23')", (str(path),))
        conn.commit()
    finally:
        conn.close()
    for cid, kind, ts in (("placed-new", "new", CREATED_TS), ("placed-update", "updated", CREATED_TS + 1)):
        capture_state.ledger_append({"id": cid, "kind": kind, "ts": ts, "status": "active",
                                     "session_id": SID, "project": "demo", "type": "insight", "score": 9,
                                     "about_issue": 7, "repo": str(env.vault),
                                     "note_path": path.relative_to(env.vault).as_posix()})
    return path


def _assert_placement(env, old, new, status):
    assert not old.exists() and new.is_file()
    assert obsidian_utils._peek_frontmatter_fields(new, ("status",))["status"] == status
    conn = vault_index._connect(env.db)
    try:
        for table, column in (("notes", "path"), ("access_log", "note_path"), ("theme_members", "note_path")):
            assert [r[0] for r in conn.execute(f"SELECT {column} FROM {table}")] == [str(new)]
        assert conn.execute("SELECT status FROM notes").fetchone()[0] == status
    finally:
        conn.close()
    rows = capture_state._merged_rows()
    assert {r["note_path"] for r in rows} == {new.relative_to(env.vault).as_posix()}
    assert {r["id"]: r["ts"] for r in rows} == {"placed-new": CREATED_TS, "placed-update": CREATED_TS + 1}
    assert new.stat().st_mode & 0o777 == 0o600
    assert not list(env.vault.rglob("*.ob-lock"))


def test_selection_places_both_status_changes_and_carries_entry_path(placement_env):
    env = placement_env
    old = _placement_note(env)
    entry, = capture_select.plan_session(SID, env.config, env.db)
    assert capture_select._apply(entry, env.config, env.db, "demo", SID) == "promoted"
    promoted = env.folder / old.name
    assert entry["path"] == promoted
    _assert_placement(env, old, promoted, "promoted")
    capture_state.ledger_update("placed-new", duplicate_of="another-note")
    assert capture_select.run_selection(SID, "demo", env.config, db=env.db)["demoted"] == 1
    _assert_placement(env, promoted, old, "provisional")


def test_selection_rerun_does_not_call_placement(placement_env, monkeypatch):
    env = placement_env
    _placement_note(env)
    capture_select.run_selection(SID, "demo", env.config, db=env.db)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in env.vault.rglob("*.md")}
    ledger = capture_state._ledger_path().read_bytes()
    assert capture_select.run_selection(SID, "demo", env.config, db=env.db) == {
        "promoted": 0, "demoted": 0, "picked": 1}
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in env.vault.rglob("*.md")} == before
    assert capture_state._ledger_path().read_bytes() == ledger


def test_refused_placement_is_retried_on_the_next_selection(placement_env, monkeypatch):
    """X-001: a promoted note left in _provisional/ by a refused move goes home on a rerun."""
    env = placement_env
    old = _placement_note(env)
    real = note_placement.place
    monkeypatch.setattr(note_placement, "place", lambda config, path, status, db: Path(path))
    assert capture_select.run_selection(SID, "demo", env.config, db=env.db)["promoted"] == 1
    assert old.is_file() and obsidian_utils._peek_frontmatter_fields(old, ("status",))["status"] == "promoted"
    monkeypatch.setattr(note_placement, "place", real)
    assert capture_select.run_selection(SID, "demo", env.config, db=env.db) == {
        "promoted": 0, "demoted": 0, "picked": 1}
    assert not old.exists() and (env.folder / old.name).is_file()


@pytest.mark.parametrize("in_provisional", [True, False])
def test_plan_session_resolves_the_other_r1_home(placement_env, in_provisional):
    env = placement_env
    actual = _placement_note(env, in_provisional=in_provisional)
    stale = env.folder / actual.name if in_provisional else env.folder / "_provisional" / actual.name
    capture_state.ledger_update("placed-new", note_path=stale.relative_to(env.vault).as_posix())
    entry, = capture_select.plan_session(SID, env.config, env.db)
    assert entry["path"] == actual and entry["want"] == "promoted"


@pytest.mark.parametrize("relative", ["elsewhere/placed.md", "claude-insights/_rejected/placed.md",
                                     "claude-insights/nested/placed.md", "../placed.md"])
def test_stale_ledger_fallback_stays_within_r1_homes(placement_env, relative):
    env = placement_env
    _placement_note(env)
    capture_state.ledger_update("placed-new", note_path=relative)
    assert capture_select.plan_session(SID, env.config, env.db) == []


def test_promote_used_finds_migrated_access_history(placement_env):
    env = placement_env
    old = _placement_note(env, in_provisional=False)
    path = note_placement.place(env.config, old, "provisional", env.db)
    conn = vault_index._connect(env.db)
    try:
        conn.execute("UPDATE access_log SET timestamp = ?", (CREATED_TS + 60,))
        conn.commit()
    finally:
        conn.close()
    assert capture_select.promote_used(env.config, now=CREATED_TS + 120, db=env.db) == {
        "checked": 1, "promoted": 1}
    _assert_placement(env, path, old, "promoted")
    assert capture_select.promote_used(env.config, now=CREATED_TS + 120, db=env.db)["promoted"] == 0


def test_demote_closed_finds_stale_ledger_and_places_note(placement_env):
    env = placement_env
    old = _placement_note(env, "promoted", in_provisional=False)
    new = env.folder / "_provisional" / old.name
    capture_state.ledger_update("placed-new", note_path=new.relative_to(env.vault).as_posix())
    assert capture_select.demote_closed("demo", env.config, now=CREATED_TS + 86400, run=_gh(), db=env.db) == {
        "checked": 1, "demoted": 1}
    _assert_placement(env, old, new, "provisional")


@pytest.mark.parametrize("ntype,status,want", [
    ("claude-insight", "'Provisional' # pending", "promoted"),
    ("claude-decision", "provisional", "active"),
])
def test_cli_hand_promotion_places_after_unlock_and_prints_final_path(placement_env, monkeypatch, capsys,
                                                                   ntype, status, want):
    import io
    import sys

    env = placement_env
    old = _placement_note(env, status)
    note_writer.set_frontmatter_fields(str(env.vault), str(old), {"type": ntype})
    new = env.folder / old.name
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", env.db)
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: {**env.config, "vault_path": "/wrong-vault"})
    monkeypatch.setattr(vault_index, "ensure_index", lambda *a, **kw: pytest.fail("existing DB needs no vault sync"))
    monkeypatch.setattr(sys, "argv", ["note_writer.py", "append-update", str(env.vault), str(old)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("## Update (2026-10-24)\n\nA hand update.\n"))
    with pytest.raises(SystemExit) as exc:
        note_writer.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"OK: {new}\n"
    assert "A hand update." in new.read_text()
    _assert_placement(env, old, new, want)


def test_cli_promotion_initializes_explicit_missing_db(placement_env, tmp_path, monkeypatch, capsys):
    env = placement_env
    old = _placement_note(env)
    db = str(tmp_path / "fresh-cli.db")
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", db)
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: env.config)
    assert note_writer.run_append_update(str(env.vault), str(old), "## Update (2026-10-24)\n\nHand.\n",
                                         promote_provisional=True) == 0
    final = env.folder / old.name
    assert capsys.readouterr().out == f"OK: {final}\n" and not old.exists()
    assert _index_stat(db, str(final))["status"] == "promoted"
    assert {r["note_path"] for r in capture_state._merged_rows()} == {final.relative_to(env.vault).as_posix()}


def test_cli_subprocess_promotes_with_isolated_config_db_and_ledger(placement_env, tmp_path, monkeypatch):
    import sys

    env = placement_env
    old = _placement_note(env)
    home = tmp_path / "cli-home"
    config_dir = home / ".claude"
    config_dir.mkdir(parents=True, mode=0o700)
    config_file = config_dir / "obsidian-brain-config.json"
    config_file.write_text(json.dumps(env.config))
    config_file.chmod(0o600)
    ledger = config_dir / "obsidian-brain" / "capture"
    ledger.mkdir(parents=True, mode=0o700)
    ledger_file = ledger / capture_state.LEDGER_NAME
    ledger_file.write_bytes(capture_state._ledger_path().read_bytes())
    ledger_file.chmod(0o600)
    process_env = {**os.environ, "HOME": str(home), "OBSIDIAN_BRAIN_DB": env.db}
    result = subprocess.run([sys.executable, str(Path(note_writer.__file__)), "append-update", str(env.vault),
                             str(old)], input="## Update (2026-10-24)\n\nFresh process.\n",
                            env=process_env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    final = env.folder / old.name
    assert result.stdout == f"OK: {final}\n"
    monkeypatch.setattr(capture_state, "capture_dir", lambda: ledger)
    _assert_placement(env, old, final, "promoted")


@pytest.mark.parametrize("promote,status", [(True, "promoted"), (False, "provisional")])
def test_append_without_status_change_does_not_place_or_index(placement_env, monkeypatch, capsys, promote, status):
    env = placement_env
    path = _placement_note(env, status, in_provisional=status == "provisional")
    ledger = capture_state._ledger_path().read_bytes()
    monkeypatch.setattr(note_placement, "place", lambda *a: pytest.fail("no status change"))
    monkeypatch.setattr(vault_index, "ensure_index", lambda *a, **kw: pytest.fail("no promotion to index"))
    assert note_writer.run_append_update(str(env.vault), str(path), "## Update (2026-10-24)\n\nHand.\n",
                                         promote_provisional=promote) == 0
    assert capsys.readouterr().out == f"OK: {path}\n" and path.is_file()
    assert capture_state._ledger_path().read_bytes() == ledger


def test_cli_placement_failure_reports_saved_update_and_exits_one(placement_env, monkeypatch, capsys):
    env = placement_env
    path = _placement_note(env)
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", env.db)
    monkeypatch.setattr(obsidian_utils, "load_config", lambda: env.config)

    def fail(*args):
        raise OSError("placement unavailable")

    monkeypatch.setattr(note_placement, "place", fail)
    assert note_writer.run_append_update(str(env.vault), str(path), "## Update (2026-10-24)\n\nHand.\n",
                                         promote_provisional=True) == 1
    out = capsys.readouterr()
    assert out.out == "" and "update saved but placement failed" in out.err
    assert "Hand." in path.read_text() and not list(env.vault.rglob("*.ob-lock"))


def test_selection_places_with_missing_ledger_and_stale_db_status(placement_env):
    env = placement_env
    old = _placement_note(env)
    conn = vault_index._connect(env.db)
    try:
        conn.execute("UPDATE notes SET status = 'superseded'")
        conn.commit()
    finally:
        conn.close()
    row = capture_state.ledger_get("placed-new")
    capture_state._ledger_path().unlink()
    entry, = capture_select.plan_session(SID, env.config, env.db, rows=[{**row, "novelty": 0.4}])
    assert capture_select._apply(entry, env.config, env.db, "demo", SID) == "promoted"
    assert entry["path"] == env.folder / old.name and not old.exists()
    assert not capture_state._ledger_path().exists()
    assert _index_stat(env.db, str(entry["path"]))["status"] == "promoted"


@pytest.mark.parametrize("stage", ["source", "destination"])
def test_stale_ledger_fallback_refuses_symlinks_outside_vault(placement_env, tmp_path, stage):
    env = placement_env
    actual = _placement_note(env)
    outside = tmp_path / "outside.md"
    outside.write_bytes(actual.read_bytes())
    top = env.folder / actual.name
    if stage == "source":
        top.symlink_to(outside)  # A matching, safe alternative must not rescue an escaping source.
    else:
        actual.unlink()
        actual.symlink_to(outside)
    capture_state.ledger_update("placed-new", note_path=top.relative_to(env.vault).as_posix())
    assert capture_select.plan_session(SID, env.config, env.db) == []
    run = _gh()
    assert capture_select.demote_closed("demo", env.config, now=CREATED_TS + 86400, run=run,
                                        db=env.db) == {"checked": 0, "demoted": 0}
    assert run.calls == []


# --- pure core -----------------------------------------------------------------

def test_merit_formula_and_fallbacks():
    assert capture_select.merit(8, 0.5, "insight") == round(0.56 + 0.15, 4)
    assert capture_select.merit(8, 0.5, "decision") == round(0.56 + 0.15 + 0.02, 4)
    assert capture_select.merit(8, 0.5, "error-fix") == capture_select.merit(8, 0.5, "decision")
    assert capture_select.merit(None, None, "insight") == capture_select.merit(5, 0.5, "insight")
    assert capture_select.merit(True, float("nan"), "insight") == capture_select.merit(5, 0.5, "insight")
    assert capture_select.merit(0, 0, "insight") == capture_select.merit(5, 0.0, "insight")  # 0 novelty is real


def test_select_is_by_merit_not_order():
    items = [{"id": f"n{i}", "merit": 0.3, "vector": {f"t{i}": 1.0}, "ts": i} for i in range(12)]
    items += [{"id": f"best{i}", "merit": 0.9, "vector": {f"b{i}": 1.0}, "ts": 100 + i} for i in range(3)]
    assert set(capture_select.select(items, cap=3)) == {"best0", "best1", "best2"}
    assert capture_select.select(list(reversed(items)), cap=3) == capture_select.select(items, cap=3)


def test_select_backstop_and_diversity_penalty():
    a = {"id": "a", "merit": 0.9, "vector": {"x": 1.0, "y": 1.0}, "ts": 1}
    twin = {"id": "twin", "merit": 0.89, "vector": {"x": 1.0, "y": 0.9}, "ts": 2}  # cosine ~0.999
    near = {"id": "near", "merit": 0.85, "vector": {"x": 1.0, "z": 1.0}, "ts": 3}  # cosine 0.5 to a
    far = {"id": "far", "merit": 0.7, "vector": {"q": 1.0}, "ts": 4}
    assert capture_select.select([a, twin, near, far], cap=2) == ["a", "far"]  # near: 0.85 - 0.25 < 0.7
    assert capture_select.select([a, twin], cap=5) == ["a"]
    assert capture_select.select([a, twin, near, far], cap=0) == []


def test_select_without_vectors_still_runs():
    items = [{"id": str(i), "merit": m, "vector": None, "ts": i} for i, m in enumerate([0.2, 0.9, 0.5])]
    assert capture_select.select(items, cap=2) == ["1", "2"]


def test_naive_or_bad_created_at_uses_the_fallback():
    assert capture_select._epoch("2026-10-23T10:00:00", 5.0) == 5.0
    assert capture_select._epoch(None, 6.0) == 6.0 and capture_select._epoch("junk", 7.0) == 7.0
    assert capture_select._epoch(CREATED, 0.0) == CREATED_TS


def test_hand_updated_rules(tmp_path):
    p = tmp_path / "n.md"
    base = "---\ntype: claude-insight\n---\n\n# T\n\nBody.\n"
    p.write_text(base + "\n## Update (2026-10-23) — auto-capture 0123456789ab\n\nx\n")
    assert capture_select.hand_updated(p) is False
    p.write_text(base + "\n```\n## Update (2026-10-23)\n```\n")
    assert capture_select.hand_updated(p) is False
    p.write_text(base + "\n## Update (2026-10-23)\n\nwritten by /compress\n")
    assert capture_select.hand_updated(p) is True
    assert capture_select.hand_updated(tmp_path / "missing.md") is False


@pytest.mark.parametrize("heading", [
    "## Update (2026-10-20) — auto-capture abcdef123456 extra",
    "## Update (2026-10-20) — auto-capture abc",
])
def test_a_near_miss_auto_heading_counts_as_a_hand_update(tmp_path, heading):
    p = tmp_path / "n.md"
    p.write_text(f"---\ntype: claude-insight\n---\n\n# T\n\nBody.\n\n{heading}\n\nby hand\n")
    assert capture_select.hand_updated(p) is True


def test_dup_cos_boundary_is_at_point_six():
    assert capture_select.DUP_COS == 0.6
    a = {"id": "a", "merit": 0.9, "vector": {"x": 1.0}, "ts": 1}

    def twin(cos):
        return {"id": "b", "merit": 0.8, "vector": {"x": cos, "y": (1 - cos * cos) ** 0.5}, "ts": 2}

    assert capture_select.select([a, twin(0.5999)], cap=5) == ["a", "b"]  # just below: picked
    assert capture_select.select([a, twin(0.6001)], cap=5) == ["a"]       # just above: skipped
    assert capture_select.select([a, twin(0.6)], cap=5) == ["a"]          # the bound itself is skipped (>=)


# --- run_selection ---------------------------------------------------------------

def test_merit_not_order_promotes_the_best_three_that_arrive_last(mock_config, db):
    rows = [_note(mock_config, i, score=3) for i in range(12)] + [_note(mock_config, 12 + i, score=9) for i in range(3)]
    got = capture_select.run_selection(SID, "demo", mock_config, db=db)
    statuses = [_status(mock_config, r) for r in rows]
    assert statuses[12:] == ["promoted"] * 3
    assert statuses.count("promoted") == 10 and statuses.count("provisional") == 5
    assert got == {"promoted": 10, "demoted": 0, "picked": 10}


def test_duplicate_is_skipped_and_superseded_is_demoted(mock_config, db):
    keep = _note(mock_config, 0, score=6)
    dup = _note(mock_config, 1, score=10, duplicate_of=keep["id"])
    old = _note(mock_config, 2, score=8)
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert (_status(mock_config, keep), _status(mock_config, dup), _status(mock_config, old)) == \
        ("promoted", "provisional", "promoted")
    newer = _note(mock_config, 3, score=7)
    capture_state.ledger_update(old["id"], superseded_by=newer["id"])
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _status(mock_config, old) == "provisional" and _status(mock_config, newer) == "promoted"
    assert any(f"cid={old['id']}" in ln and "outcome=demoted" in ln and "why=superseded" in ln for ln in _log_lines())
    assert not any(f"cid={dup['id']}" in ln for ln in _log_lines())  # never promoted, so never demoted


@pytest.fixture
def incumbent_at_cap(mock_config, db, monkeypatch, request):
    """Cached novelty and missing vectors isolate the merit gap from MMR penalties."""
    ntype = request.param
    top = capture_select.promoted_status(ntype)
    incumbent = _note(mock_config, 0, score=5, novelty=0.0, ntype=ntype, status=top)
    for i in range(1, capture_select.CAP):
        _note(mock_config, i, score=10, novelty=1.0, ntype=ntype, status=top)
    monkeypatch.setattr(capture_select, "_fresh_index_rows", lambda db, paths: {})
    return incumbent, ntype, top


@pytest.mark.parametrize("incumbent_at_cap", ["insight", "decision", "error-fix"], indirect=True)
@pytest.mark.parametrize("gap", [0.1, 0.3])
def test_incumbent_bonus_controls_displacement_at_cap(mock_config, db, incumbent_at_cap, gap):
    incumbent, ntype, top = incumbent_at_cap
    challenger = _note(mock_config, capture_select.CAP, score=5, novelty=gap / 0.3, ntype=ntype)
    entries = {e["row"]["id"]: e for e in capture_select.plan_session(SID, mock_config, db)}
    stays = gap < capture_select.INCUMBENT_BONUS
    assert entries[challenger["id"]]["merit"] - entries[incumbent["id"]]["merit"] == pytest.approx(gap)
    assert entries[incumbent["id"]]["why"] == ("pick" if stays else "displaced")
    assert entries[challenger["id"]]["why"] == ("below-cap" if stays else "pick")
    assert capture_select.run_selection(SID, "demo", mock_config, db=db) == {
        "promoted": 0 if stays else 1, "demoted": 0 if stays else 1, "picked": capture_select.CAP}
    assert _status(mock_config, incumbent) == (top if stays else "provisional")
    assert _status(mock_config, challenger) == ("provisional" if stays else top)
    # Check the file written by run_selection, including when the incumbent stays put.
    assert float(_fm(mock_config, incumbent)["capture_merit"]) == capture_select.merit(5, 0.0, ntype)


@pytest.mark.parametrize("incumbent_at_cap", ["insight", "decision", "error-fix"], indirect=True)
def test_zero_incumbent_bonus_restores_challenger_win(mock_config, db, monkeypatch, incumbent_at_cap):
    incumbent, ntype, top = incumbent_at_cap
    challenger = _note(mock_config, capture_select.CAP, score=5, novelty=1 / 3, ntype=ntype)
    before = {e["row"]["id"]: e for e in capture_select.plan_session(SID, mock_config, db)}
    assert before[incumbent["id"]]["want"] == top
    assert before[challenger["id"]]["want"] == "provisional"
    monkeypatch.setattr(capture_select, "INCUMBENT_BONUS", 0.0)
    after = {e["row"]["id"]: e for e in capture_select.plan_session(SID, mock_config, db)}
    assert after[incumbent["id"]]["want"] == "provisional"
    assert after[incumbent["id"]]["why"] == "displaced"
    assert after[challenger["id"]]["want"] == top
    assert capture_select.run_selection(SID, "demo", mock_config, db=db)["promoted"] == 1
    assert _status(mock_config, challenger) == top


def test_duplicate_incumbent_is_skipped_after_a_stronger_pick(mock_config, db):
    incumbent = _note(mock_config, 0, score=1, novelty=1.0, status="promoted", body=_words(0, 20))
    challenger = _note(mock_config, 1, score=10, novelty=1.0, body=_words(0, 20) + " again")
    entries = {e["row"]["id"]: e for e in capture_select.plan_session(SID, mock_config, db)}
    old, new = entries[incumbent["id"]], entries[challenger["id"]]
    assert old["kind"] == new["kind"] == "candidate"
    assert capture_select._cosine_similarity(old["vector"], new["vector"]) >= capture_select.DUP_COS
    assert old["want"] == "provisional" and old["why"] == "displaced"
    assert new["want"] == "promoted"
    assert capture_select.run_selection(SID, "demo", mock_config, db=db) == {
        "promoted": 1, "demoted": 1, "picked": 1}
    assert _status(mock_config, incumbent) == "provisional"


def test_later_slice_displaces_an_earlier_pick(mock_config, db):
    rows = [_note(mock_config, i, score=5) for i in range(10)]
    assert capture_select.run_selection(SID, "demo", mock_config, db=db)["promoted"] == 10
    strong = _note(mock_config, 10, score=10)
    got = capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert got == {"promoted": 1, "demoted": 1, "picked": 10}
    assert _status(mock_config, strong) == "promoted"
    out = [r for r in rows if _status(mock_config, r) == "provisional"]
    assert len(out) == 1
    line = next(ln for ln in _log_lines() if f"cid={out[0]['id']}" in ln and "demoted" in ln)
    assert "why=displaced" in line and "rank=11" in line


@pytest.mark.parametrize("how", ["search", "tool-open", "hand-update"])
def test_used_note_is_never_demoted_and_sits_outside_the_cap(mock_config, db, how):
    used = _note(mock_config, 0, score=1)
    if how == "hand-update":
        path = Path(mock_config["vault_path"]) / used["note_path"]
        path.write_text(path.read_text() + "\n## Update (2026-10-24)\n\nAdded by /compress.\n")
    else:
        _access(db, used, mock_config, how, CREATED_TS + 3600)
    rows = [_note(mock_config, 1 + i, score=9) for i in range(12)]
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    fm = _fm(mock_config, used)
    assert fm["status"] == "promoted" and fm["promoted_by"] == ("hand-update" if how == "hand-update" else "access")
    assert [_status(mock_config, r) for r in rows].count("promoted") == 10  # the cap is untouched
    assert any(f"cid={used['id']}" in ln and f"why=sticky-{fm['promoted_by']}" in ln for ln in _log_lines())
    for i in range(3):
        _note(mock_config, 20 + i, score=10)
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _status(mock_config, used) == "promoted"


def test_sticky_promotion_outlives_a_purged_access_log(mock_config, db):
    """promoted_by on the note is the record: an access_log purge or GC does not undo the promotion."""
    used = _note(mock_config, 0, score=1)
    _access(db, used, mock_config, "search", CREATED_TS + 60)
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    conn = vault_index._connect(db)
    conn.execute("DELETE FROM access_log")
    conn.commit()
    conn.close()
    for i in range(11):
        _note(mock_config, 1 + i, score=10)
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _fm(mock_config, used)["status"] == "promoted"


@pytest.mark.parametrize("context, offset", [("related", 3600), ("recall", 3600), ("search", -3600)])
def test_automatic_or_earlier_access_is_not_use(mock_config, db, context, offset):
    note = _note(mock_config, 0, score=1)
    _access(db, note, mock_config, context, CREATED_TS + offset)
    for i in range(10):
        _note(mock_config, 1 + i, score=9)
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _status(mock_config, note) == "provisional" and _fm(mock_config, note)["promoted_by"] is None


def test_cosine_backstop_skips_a_reworded_duplicate(mock_config, db):
    a = _note(mock_config, 0, score=9, body=_words(0, 20))
    b = _note(mock_config, 1, score=9, body=_words(0, 20) + " again")
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert {_status(mock_config, a), _status(mock_config, b)} == {"promoted", "provisional"}


def test_second_run_writes_nothing(mock_config, db):
    rows = [_note(mock_config, i, score=3 + i % 5) for i in range(14)]
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    paths = [_current_note_path(mock_config, r) for r in rows]
    before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in paths]
    lines, ledger = len(_log_lines()), capture_state._ledger_path().read_text()
    assert capture_select.run_selection(SID, "demo", mock_config, db=db) == {"promoted": 0, "demoted": 0, "picked": 10}
    assert [(p.stat().st_mtime_ns, p.read_bytes()) for p in paths] == before
    assert len(_log_lines()) == lines and capture_state._ledger_path().read_text() == ledger


def test_parts_are_recorded_and_logged(mock_config, db):
    pick = _note(mock_config, 0, score=8, ntype="error-fix")
    rest = [_note(mock_config, 1 + i, score=2) for i in range(10)]
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    fm = _fm(mock_config, pick)
    assert fm["capture_score"] == "8" and fm["capture_novelty"] == "1.0" and fm["capture_rank"] == "1"
    assert fm["capture_merit"] == str(capture_select.merit(8, 1.0, "error-fix"))
    low = [_fm(mock_config, r) for r in rest if _status(mock_config, r) == "provisional"]
    assert len(low) == 1 and low[0]["capture_rank"] == "11" and low[0]["capture_score"] == "2"
    promoted = [ln for ln in _log_lines() if "outcome=promoted" in ln]
    assert len(promoted) == 10
    assert f"cid={pick['id']};merit={fm['capture_merit']};rank=1;why=pick" in promoted[0] or any(
        f"cid={pick['id']};merit={fm['capture_merit']};rank=1;why=pick" in ln for ln in promoted)


def test_decision_is_promoted_to_active(mock_config, db):
    d = _note(mock_config, 0, ntype="decision", score=9)
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _status(mock_config, d) == "active"


def test_foreign_status_missing_file_and_rejected_note_are_left_alone(mock_config, db):
    sup = _note(mock_config, 0, ntype="decision", status="superseded", score=10)
    bare = _note(mock_config, 1, status="", score=10)  # an auto note from before #455: no status line
    gone = _note(mock_config, 2, score=10)
    (Path(mock_config["vault_path"]) / gone["note_path"]).unlink()
    rej = _note(mock_config, 3, score=10, ledger=False)
    rej_rel = "claude-insights/_rejected/n3.md"
    (Path(mock_config["vault_path"]) / "claude-insights" / "_rejected").mkdir()
    (Path(mock_config["vault_path"]) / rej["note_path"]).rename(Path(mock_config["vault_path"]) / rej_rel)
    capture_state.ledger_append({**rej, "note_path": rej_rel})
    before = {p: p.read_bytes() for p in Path(mock_config["vault_path"]).rglob("*.md")}
    assert capture_select.run_selection(SID, "demo", mock_config, db=db) == {"promoted": 0, "demoted": 0, "picked": 0}
    assert {p: p.read_bytes() for p in Path(mock_config["vault_path"]).rglob("*.md")} == before
    assert _status(mock_config, sup) == "superseded" and _status(mock_config, bare) is None


def test_missing_file_skipped(mock_config, db):
    gone = _note(mock_config, 0, score=10)
    (Path(mock_config["vault_path"]) / gone["note_path"]).unlink()
    keep = _note(mock_config, 1, score=2)
    assert capture_select.run_selection(SID, "demo", mock_config, db=db)["promoted"] == 1
    assert _status(mock_config, keep) == "promoted"


def test_stale_index_reindexed_before_novelty(mock_config, db):
    other = _note(mock_config, 0, sid=OTHER, score=5, body=_words(5, 20), day="2026-10-20")
    vault_index.index_note(db, str(Path(mock_config["vault_path"]) / other["note_path"]))
    mine = _note(mock_config, 1, score=5, body=_words(9, 20))
    path = Path(mock_config["vault_path"]) / mine["note_path"]
    vault_index.index_note(db, str(path))  # indexed with its first body ...
    time.sleep(0.01)
    path.write_text(path.read_text().replace(_words(9, 20), _words(5, 20)))  # ... then rewritten as other's twin
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    nov = capture_state.ledger_get(mine["id"])["novelty"]
    assert nov < 0.3  # scored against the new body, not the stale vector


def test_novelty_counts_other_sessions_earlier_notes_only(mock_config, db):
    _note(mock_config, 0, sid=OTHER, body=_words(7, 20), day="2026-10-22")
    _note(mock_config, 1, sid=OTHER, body=_words(8, 20), day="2026-10-25")  # captured later: not prior art
    twin = _note(mock_config, 2, body=_words(7, 20))
    fresh = _note(mock_config, 3, body=_words(8, 20))
    mate = _note(mock_config, 4, body=_words(7, 20) + " more")  # same session: never prior art
    vault_index.ensure_index(mock_config["vault_path"], FOLDERS, db_path=db)
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    nov = {r["id"]: capture_state.ledger_get(r["id"])["novelty"] for r in (twin, fresh, mate)}
    assert nov[twin["id"]] < 0.3 and nov[fresh["id"]] > 0.6
    assert nov[mate["id"]] < 0.3  # its twin in OTHER, not its session mate, sets its novelty


def test_missing_vector_gives_half_novelty_and_is_not_cached(mock_config, db, monkeypatch):
    note = _note(mock_config, 0, score=6)
    monkeypatch.setattr(capture_select, "_fresh_index_rows", lambda db, paths: {})
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _fm(mock_config, note)["capture_novelty"] == "0.5" and _status(mock_config, note) == "promoted"
    assert "novelty" not in capture_state.ledger_get(note["id"])


def test_cached_novelty_is_reused(mock_config, db, monkeypatch):
    note = _note(mock_config, 0, score=6, novelty=0.0)
    monkeypatch.setattr(capture_select, "_others", lambda *a: pytest.fail("novelty recomputed"))
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _fm(mock_config, note)["capture_novelty"] == "0.0"  # a cached 0 is a value, not "missing"


def test_selection_error_changes_nothing(mock_config, db, monkeypatch):
    rows = [_note(mock_config, i, score=9) for i in range(3)]
    monkeypatch.setattr(capture_select, "select", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert [_status(mock_config, r) for r in rows] == ["provisional"] * 3


def test_a_note_that_cannot_be_written_is_logged_and_the_rest_go_on(mock_config, db, monkeypatch):
    rows = [_note(mock_config, i, score=9) for i in range(3)]
    real = capture_select.note_writer.set_frontmatter_fields

    def flaky(vault, path, fields, **kw):
        if path.endswith("n1.md"):
            raise OSError("another process is updating this note")
        return real(vault, path, fields, **kw)

    monkeypatch.setattr(capture_select.note_writer, "set_frontmatter_fields", flaky)
    assert capture_select.run_selection(SID, "demo", mock_config, db=db)["promoted"] == 2
    assert _status(mock_config, rows[1]) == "provisional"
    assert any("outcome=select-write-failed" in ln and rows[1]["id"] in ln for ln in _log_lines())


def test_demotions_are_written_before_promotions(mock_config, db, monkeypatch):
    rows = [_note(mock_config, i, score=5) for i in range(10)]
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    _note(mock_config, 10, score=10)
    order = []
    real = capture_select.note_writer.set_frontmatter_fields
    monkeypatch.setattr(capture_select.note_writer, "set_frontmatter_fields",
                        lambda v, p, f, **kw: order.append(f["status"]) or real(v, p, f, **kw))
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert order == ["provisional", "promoted"]


def test_a_failed_demotion_blocks_every_promotion_in_that_run(mock_config, db, monkeypatch):
    rows = [_note(mock_config, i, score=5) for i in range(10)]
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    strong = [_note(mock_config, 10 + i, score=10) for i in range(2)]  # displace two of the ten
    real = capture_select.note_writer.set_frontmatter_fields
    fail = {"on": True}

    def flaky(vault, path, fields, **kw):
        if fail["on"] and fields.get("status") == "provisional":
            raise OSError("another process is updating this note")
        return real(vault, path, fields, **kw)

    monkeypatch.setattr(capture_select.note_writer, "set_frontmatter_fields", flaky)
    got = capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert got["promoted"] == 0 and got.get("deferred") is True
    assert [_status(mock_config, r) for r in strong] == ["provisional"] * 2
    assert [_status(mock_config, r) for r in rows].count("promoted") == 10  # never over the cap
    assert any("outcome=select-failed" in ln and "detail=demotion-write;cid=" in ln for ln in _log_lines())
    fail["on"] = False  # the next slice finishes the job
    got = capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert got["promoted"] == 2 and got["demoted"] == 2 and "deferred" not in got
    every = rows + strong
    assert [_status(mock_config, r) for r in every].count("promoted") == 10


def test_a_pick_clears_the_backlog_flag(mock_config, db):
    note = _note(mock_config, 0, score=9, extra_fm="demoted_by: backlog\n")
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    fm = _fm(mock_config, note)
    assert fm["status"] == "promoted" and fm["demoted_by"] is None


def test_issue_closed_note_is_never_re_promoted(mock_config, db):
    note = _note(mock_config, 0, score=10, extra_fm="demoted_by: issue-closed\n")
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _status(mock_config, note) == "provisional"


# --- tool opens ----------------------------------------------------------------

def _read_row(path, ts="2026-10-24T09:00:00Z"):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": str(path)}}]}}


def test_log_tool_opens(mock_config, db, tmp_path):
    vault = Path(mock_config["vault_path"])
    hand = vault / "claude-insights" / "hand.md"
    hand.write_text("---\ntype: claude-insight\nproject: demo\n---\n\n# Hand\n\nA hand note.\n")
    session = vault / "claude-sessions" / "s.md"
    session.write_text("---\ntype: claude-session\nproject: demo\n---\n\n# S\n")
    own = _note(mock_config, 0)
    vault_index.ensure_index(str(vault), FOLDERS, db_path=db)
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    rows = [_read_row(hand), _read_row(session), _read_row(vault / own["note_path"]), _read_row(outside),
            {"type": "user", "message": {"role": "user", "content": "hi"}}, "junk"]
    assert capture_select.log_tool_opens(rows, mock_config, SID, db) == 1
    assert capture_select.log_tool_opens(rows, mock_config, SID, db) == 0  # a retried slice logs nothing twice
    conn = vault_index._connect(db)
    got = conn.execute("SELECT note_path, timestamp, context_type FROM access_log").fetchall()
    conn.close()
    assert [tuple(r) for r in got] == [(str(hand), 1792832400.0, "tool-open")]


def test_log_slice_opens_reads_the_slice_bytes(mock_config, db, tmp_path):
    hand = Path(mock_config["vault_path"]) / "claude-insights" / "hand.md"
    hand.write_text("---\ntype: claude-insight\nproject: demo\n---\n\n# Hand\n\nA hand note.\n")
    vault_index.ensure_index(mock_config["vault_path"], FOLDERS, db_path=db)
    t = tmp_path / "t.jsonl"
    first = json.dumps(_read_row(hand, "2026-10-24T08:00:00Z")) + "\n"
    t.write_text(first + json.dumps(_read_row(hand)) + "\n")
    assert capture_select.log_slice_opens(t, len(first), t.stat().st_size, mock_config, SID, db) == 1
    assert capture_select.log_slice_opens(t, 5, 5, mock_config, SID, db) == 0


# --- closed-issue demotion -----------------------------------------------------

def _gh(state="CLOSED", closed="2026-10-24T12:00:00Z", rc=0, stdout=None):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        out = stdout if stdout is not None else json.dumps({"state": state, "closedAt": closed})
        return subprocess.CompletedProcess(argv, rc, out, "")

    run.calls = calls
    return run


@pytest.fixture
def promoted_issue_note(mock_config, db, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    row = _note(mock_config, 0, score=9, about_issue=455, repo=str(repo), ts=time.time() - 86400)
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _status(mock_config, row) == "promoted"
    return row


def test_closed_issue_demotes(mock_config, db, promoted_issue_note):
    run = _gh()
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db) == {"checked": 1, "demoted": 1}
    fm = _fm(mock_config, promoted_issue_note)
    assert fm["status"] == "provisional" and fm["demoted_by"] == "issue-closed"
    argv, kw = run.calls[0]
    assert argv == ["gh", "issue", "view", "455", "--json", "state,closedAt"]
    assert kw["cwd"] == promoted_issue_note["repo"] and kw["timeout"] == 10
    assert any("why=issue-closed:455" in ln and "outcome=demoted" in ln for ln in _log_lines())
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _status(mock_config, promoted_issue_note) == "provisional"  # selection never re-promotes it


def test_a_ledger_path_that_escapes_the_vault_is_not_read(mock_config, db, tmp_path_factory, promoted_issue_note):
    outside = tmp_path_factory.mktemp("elsewhere") / "evil.md"
    vault_note = Path(mock_config["vault_path"]) / promoted_issue_note["note_path"]
    outside.write_text(vault_note.read_text())
    before = outside.read_bytes()
    capture_state.ledger_append({**{k: v for k, v in promoted_issue_note.items() if k != "id"},
                                 "note_path": os.path.relpath(outside, mock_config["vault_path"]),
                                 "ts": time.time()})
    link = Path(mock_config["vault_path"]) / "claude-insights" / "link.md"
    link.symlink_to(outside)
    capture_state.ledger_append({**{k: v for k, v in promoted_issue_note.items() if k != "id"},
                                 "note_path": "claude-insights/link.md", "ts": time.time()})
    vault_note.unlink()  # leave only the escaping rows
    run = _gh()
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db) == {"checked": 0, "demoted": 0}
    assert run.calls == [] and outside.read_bytes() == before


@pytest.mark.parametrize("run", [
    _gh(closed="2026-10-23T09:00:00Z"),           # closed before capture
    _gh(state="OPEN", closed=None),
    _gh(rc=1),
    _gh(stdout="not json"),
    _gh(stdout="[]"),
    _gh(closed="2026-10-24T12:00:00"),            # naive time: cannot be compared, so nothing
    _gh(closed="garbage"),
])
def test_gh_failure_or_open_issue_demotes_nothing(mock_config, db, promoted_issue_note, run):
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db)["demoted"] == 0
    assert _status(mock_config, promoted_issue_note) == "promoted"


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("gh", 10), FileNotFoundError("gh")])
def test_gh_failure_demotes_nothing(mock_config, db, promoted_issue_note, exc):
    def run(argv, **kw):
        raise exc
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db)["demoted"] == 0
    assert _status(mock_config, promoted_issue_note) == "promoted"


def test_close_time_compares_across_a_utc_date_boundary(mock_config, db, tmp_path):
    """Captured 23:30 at UTC-7 (06:30Z next day): a 05:00Z close on that day came first."""
    repo = tmp_path / "r"
    repo.mkdir()
    row = _note(mock_config, 0, score=9, about_issue=7, repo=str(repo), ts=time.time() - 3600,
                created="2026-10-23T23:30:00-07:00")
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert capture_select.demote_closed("demo", mock_config, run=_gh(closed="2026-10-24T05:00:00Z"), db=db,
                                        now=time.time())["demoted"] == 0
    assert capture_select.demote_closed("demo", mock_config, run=_gh(closed="2026-10-24T07:00:00Z"), db=db,
                                        now=time.time() + capture_select.ISSUE_CHECK_EVERY_SECONDS)["demoted"] == 1
    assert _status(mock_config, row) == "provisional"


def test_sticky_notes_are_exempt(mock_config, db, promoted_issue_note):
    _access(db, promoted_issue_note, mock_config, "search", CREATED_TS + 60)
    run = _gh()
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db)["demoted"] == 0
    capture_select.run_selection(SID, "demo", mock_config, db=db)  # now recorded as promoted_by: access
    later = time.time() + capture_select.ISSUE_CHECK_EVERY_SECONDS
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db, now=later) == {"checked": 0, "demoted": 0}
    assert _status(mock_config, promoted_issue_note) == "promoted"


def test_rate_limit_skips_a_second_run_inside_six_hours(mock_config, db, promoted_issue_note):
    now = time.time()
    assert capture_select.demote_closed("demo", mock_config, run=_gh(state="OPEN"), db=db, now=now)["demoted"] == 0
    run = _gh()
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db, now=now + 6 * 3600 - 1) == \
        {"skipped": "rate-limited", "demoted": 0}
    assert run.calls == []
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db, now=now + 6 * 3600)["demoted"] == 1


def test_only_recent_promoted_issue_notes_of_the_project_are_checked(mock_config, db, tmp_path):
    repo = str(tmp_path)
    old = _note(mock_config, 0, score=9, about_issue=1, repo=repo, ts=time.time() - 61 * 86400)
    other = _note(mock_config, 1, score=9, about_issue=2, repo=repo, ts=time.time(), project="elsewhere")
    plain = _note(mock_config, 2, score=9, repo=repo, ts=time.time())
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    run = _gh()
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db) == {"checked": 0, "demoted": 0}
    assert run.calls == [] and all(_status(mock_config, r) == "promoted" for r in (old, other, plain))


def test_demote_closed_default_runner_is_resolved_at_call_time(mock_config, db, promoted_issue_note, monkeypatch):
    seen = []
    monkeypatch.setattr(capture_select.subprocess, "run", lambda argv, **kw: seen.append(argv) or SimpleNamespace(
        returncode=1, stdout=""))
    capture_select.demote_closed("demo", mock_config, db=db)
    assert seen and seen[0][0] == "gh"


def test_a_provisional_issue_note_is_not_flagged(mock_config, db, tmp_path):
    """Only promoted notes are demoted: flagging a provisional one would bar it from selection for good."""
    row = _note(mock_config, 0, score=1, about_issue=9, repo=str(tmp_path), ts=time.time())
    for i in range(10):
        _note(mock_config, 1 + i, score=10, ts=time.time())
    capture_select.run_selection(SID, "demo", mock_config, db=db)
    assert _status(mock_config, row) == "provisional"
    run = _gh()
    assert capture_select.demote_closed("demo", mock_config, run=run, db=db) == {"checked": 0, "demoted": 0}
    assert _fm(mock_config, row)["demoted_by"] is None and run.calls == []


def test_update_note_status_keeps_the_vector(mock_config, db):
    row = _note(mock_config, 0)
    path = str(Path(mock_config["vault_path"]) / row["note_path"])
    assert vault_index.update_note_status(db, path)  # not indexed yet: indexed in full
    conn = vault_index._connect(db)
    vec = conn.execute("SELECT tfidf_vector FROM notes WHERE path = ?", (path,)).fetchone()[0]
    conn.execute("UPDATE notes SET tfidf_vector = ? WHERE path = ?", ('{"kept": 1.0}', path))
    conn.commit()
    conn.close()
    wrote = []
    capture_select.note_writer.set_frontmatter_fields(mock_config["vault_path"], path, {"status": "promoted"},
                                                      stat_out=wrote)
    assert vault_index.update_note_status(db, path, *wrote)
    conn = vault_index._connect(db)
    got = conn.execute("SELECT status, tfidf_vector, mtime FROM notes WHERE path = ?", (path,)).fetchone()
    conn.close()
    assert vec and got["status"] == "promoted" and got["tfidf_vector"] == '{"kept": 1.0}'
    assert vault_index.mtime_unchanged(Path(path).stat().st_mtime, got["mtime"])
    assert vault_index.update_note_status(db, path + ".missing") is False


def _index_stat(db, path):
    conn = vault_index._connect(db)
    row = conn.execute("SELECT status, mtime, size FROM notes WHERE path = ?", (path,)).fetchone()
    conn.close()
    return row


def test_update_note_status_stamps_a_fresh_row(mock_config, db):
    row = _note(mock_config, 0)
    path = str(Path(mock_config["vault_path"]) / row["note_path"])
    vault_index.index_note(db, path)
    wrote = []
    capture_select.note_writer.set_frontmatter_fields(mock_config["vault_path"], path, {"status": "promoted"},
                                                      stat_out=wrote)
    assert vault_index.update_note_status(db, path, *wrote)
    got, st = _index_stat(db, path), Path(path).stat()
    assert got["status"] == "promoted" and got["size"] == st.st_size
    assert vault_index.mtime_unchanged(st.st_mtime, got["mtime"])


def test_update_note_status_leaves_a_row_stale_after_an_unseen_external_edit(mock_config, db):
    """The index never saw the external edit, so stamping our mtime would hide it from _sync for good."""
    row = _note(mock_config, 0, body=_words(3, 20))
    path = Path(mock_config["vault_path"]) / row["note_path"]
    vault_index.index_note(db, str(path))
    old = _index_stat(db, str(path))
    time.sleep(0.01)
    path.write_text(path.read_text().replace(_words(3, 20), _words(4, 20)))  # edited in Obsidian, not indexed yet
    wrote = []
    capture_select.note_writer.set_frontmatter_fields(mock_config["vault_path"], str(path), {"status": "promoted"},
                                                      stat_out=wrote)
    assert vault_index.update_note_status(db, str(path), *wrote)
    got = _index_stat(db, str(path))
    assert got["status"] == "promoted" and got["mtime"] == old["mtime"] and got["size"] == old["size"]
    assert not vault_index.mtime_unchanged(path.stat().st_mtime, got["mtime"])
    vault_index.ensure_index(mock_config["vault_path"], FOLDERS, db_path=db)  # _sync re-indexes the content
    conn = vault_index._connect(db)
    body = conn.execute("SELECT body FROM notes WHERE path = ?", (str(path),)).fetchone()["body"]
    conn.close()
    assert _words(4, 20) in body


def test_update_note_status_without_a_pre_stat_never_stamps(mock_config, db):
    row = _note(mock_config, 0)
    path = str(Path(mock_config["vault_path"]) / row["note_path"])
    vault_index.index_note(db, path)
    old = _index_stat(db, path)
    capture_select.note_writer.set_frontmatter_fields(mock_config["vault_path"], path, {"status": "promoted"})
    assert vault_index.update_note_status(db, path)
    got = _index_stat(db, path)
    assert got["status"] == "promoted" and got["mtime"] == old["mtime"]


# --- promote_used: promotion on use for finished sessions ---------------------------

LATER = CREATED_TS + 30 * 86400  # "now" for promote_used: after every fixture access row


def _provisional(config, db, i=0, **kw):
    """A provisional note that has been indexed, from a session whose selection is long over."""
    row = _note(config, i, score=kw.pop("score", 1), **kw)
    vault_index.index_note(db, str(Path(config["vault_path"]) / row["note_path"]))
    return row


def test_promote_used_promotes_a_finished_sessions_note_searched_a_day_later(mock_config, db):
    row = _provisional(mock_config, db)
    _access(db, row, mock_config, "search", CREATED_TS + 86400)
    got = capture_select.promote_used(mock_config, db=db, now=LATER)
    assert got["promoted"] == 1
    fm = _fm(mock_config, row)
    assert fm["status"] == "promoted" and fm["promoted_by"] == "access"
    assert vault_index._connect(db).execute("SELECT status FROM notes WHERE path LIKE ?", ("%n0.md",)).fetchone()[0] \
        == "promoted"
    assert any(f"cid={row['id']}" in ln and "outcome=promoted" in ln and "why=sticky-access" in ln
               for ln in _log_lines())
    capture_select.run_selection(SID, "demo", mock_config, db=db)  # selection then agrees: still promoted, sticky
    assert _fm(mock_config, row)["status"] == "promoted"


def test_promote_used_keeps_a_decision_active(mock_config, db):
    row = _provisional(mock_config, db, ntype="decision")
    _access(db, row, mock_config, "tool-open", CREATED_TS + 60)
    capture_select.promote_used(mock_config, db=db, now=LATER)
    assert _status(mock_config, row) == "active"


@pytest.mark.parametrize("context, offset", [("related", 3600), ("recall", 3600), ("search", -3600),
                                              ("tool-open", -3600)])
def test_promote_used_ignores_automatic_and_earlier_rows(mock_config, db, context, offset):
    row = _provisional(mock_config, db)
    _access(db, row, mock_config, context, CREATED_TS + offset)
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 0
    assert _status(mock_config, row) == "provisional"


def test_promote_used_twice_writes_once(mock_config, db):
    row = _provisional(mock_config, db)
    _access(db, row, mock_config, "search", CREATED_TS + 60)
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 1
    path = Path(mock_config["vault_path"]) / row["note_path"]
    before, lines = (path.stat().st_mtime_ns, path.read_bytes()), len(_log_lines())
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 0
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before and len(_log_lines()) == lines


def test_promote_used_skips_rejected_issue_closed_and_outside_the_vault(mock_config, db, tmp_path_factory):
    closed = _provisional(mock_config, db, 0, extra_fm="demoted_by: issue-closed\n")
    rej = _provisional(mock_config, db, 1)
    vault = Path(mock_config["vault_path"])
    (vault / "claude-insights" / "_rejected").mkdir()
    (vault / rej["note_path"]).rename(vault / "claude-insights" / "_rejected" / "n1.md")
    moved = str(vault / "claude-insights" / "_rejected" / "n1.md")
    outside = tmp_path_factory.mktemp("elsewhere") / "outside.md"
    outside.write_text((vault / _note(mock_config, 5, ledger=False)["note_path"]).read_text())  # plain provisional
    conn = vault_index._connect(db)
    conn.execute("UPDATE notes SET path = ? WHERE path LIKE ?", (moved, "%n1.md"))
    conn.execute("INSERT INTO notes (path, type, status, mtime, project) VALUES (?, 'claude-insight', 'provisional', 1, 'demo')",
                 (str(outside),))
    for p in (str(vault / closed["note_path"]), moved, str(outside)):
        conn.execute("INSERT INTO access_log (note_path, timestamp, context_type, project) VALUES (?, ?, 'search', 'demo')",
                     (p, CREATED_TS + 60))
    conn.commit()
    conn.close()
    before = outside.read_bytes()
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 0
    assert "status: provisional" in (vault / "claude-insights" / "_rejected" / "n1.md").read_text()
    assert _status(mock_config, closed) == "provisional" and outside.read_bytes() == before


def test_promote_used_confirms_status_from_the_frontmatter(mock_config, db):
    """A stale index row says provisional, but the file was promoted since: nothing is written."""
    row = _provisional(mock_config, db)
    path = Path(mock_config["vault_path"]) / row["note_path"]
    path.write_text(path.read_text().replace("status: provisional", "status: superseded"))
    _access(db, row, mock_config, "search", CREATED_TS + 60)
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 0
    assert "status: superseded" in path.read_text()


def test_promote_used_promotes_a_loaded_row_with_a_hand_update(mock_config, db):
    row = _provisional(mock_config, db)
    path = Path(mock_config["vault_path"]) / row["note_path"]
    path.write_text(path.read_text() + "\n## Update (2026-10-24)\n\nAdded by /compress.\n")
    _access(db, row, mock_config, "search", CREATED_TS - 3600)  # loaded by the join, but before capture
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 1
    fm = _fm(mock_config, row)
    assert fm["status"] == "promoted" and fm["promoted_by"] == "hand-update"
    assert any(f"cid={row['id']}" in ln and "why=sticky-hand-update" in ln for ln in _log_lines())


def test_promote_used_does_not_scan_note_bodies_it_did_not_load(mock_config, db):
    row = _provisional(mock_config, db)  # no access row: not loaded
    path = Path(mock_config["vault_path"]) / row["note_path"]
    path.write_text(path.read_text() + "\n## Update (2026-10-24)\n\nAdded by /compress.\n")
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 0
    assert _status(mock_config, row) == "provisional"


def test_promote_used_ignores_an_auto_update_section(mock_config, db):
    row = _provisional(mock_config, db)
    path = Path(mock_config["vault_path"]) / row["note_path"]
    path.write_text(path.read_text() + "\n## Update (2026-10-24) — auto-capture 0123456789ab\n\nauto\n")
    _access(db, row, mock_config, "search", CREATED_TS - 3600)
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 0


@pytest.mark.parametrize("status", ['"Provisional"', "provisional # keep for now", "PROVISIONAL"])
def test_promote_used_finds_a_provisional_status_in_any_spelling(mock_config, db, status):
    row = _provisional(mock_config, db)
    path = Path(mock_config["vault_path"]) / row["note_path"]
    path.write_text(path.read_text().replace("status: provisional", f"status: {status}"))
    vault_index.index_note(db, str(path))
    _access(db, row, mock_config, "search", CREATED_TS + 60)
    assert capture_select.promote_used(mock_config, db=db, now=LATER)["promoted"] == 1
    assert _status(mock_config, row) == "promoted"


def test_update_note_status_leaves_a_row_stale_after_an_edit_that_lands_after_our_write(mock_config, db):
    """Only our own write may be stamped: an edit between our write and the index update stays visible to _sync."""
    row = _note(mock_config, 0, body=_words(3, 20))
    path = Path(mock_config["vault_path"]) / row["note_path"]
    vault_index.index_note(db, str(path))
    old = _index_stat(db, str(path))
    wrote = []
    capture_select.note_writer.set_frontmatter_fields(mock_config["vault_path"], str(path), {"status": "promoted"},
                                                      stat_out=wrote)
    time.sleep(0.01)
    path.write_text(path.read_text().replace(_words(3, 20), _words(4, 20)))  # an external edit lands now
    assert vault_index.update_note_status(db, str(path), *wrote)
    got = _index_stat(db, str(path))
    assert got["status"] == "promoted" and got["mtime"] == old["mtime"] and got["size"] == old["size"]
    vault_index.ensure_index(mock_config["vault_path"], FOLDERS, db_path=db)
    conn = vault_index._connect(db)
    body = conn.execute("SELECT body FROM notes WHERE path = ?", (str(path),)).fetchone()["body"]
    conn.close()
    assert _words(4, 20) in body


def test_set_frontmatter_fields_reports_the_stat_of_what_it_wrote(mock_config, db):
    row = _note(mock_config, 0)
    path = Path(mock_config["vault_path"]) / row["note_path"]
    wrote = []
    assert capture_select.note_writer.set_frontmatter_fields(mock_config["vault_path"], str(path),
                                                             {"status": "promoted"}, stat_out=wrote)
    st = path.stat()
    assert wrote[1] == (st.st_mtime_ns, st.st_size) and len(wrote) == 2
    unchanged = []
    assert capture_select.note_writer.set_frontmatter_fields(mock_config["vault_path"], str(path),
                                                             {"status": "promoted"}, stat_out=unchanged) is False
    assert unchanged == []


def test_update_note_status_goes_by_the_stat_the_setter_took_under_its_lock(mock_config, db):
    """An edit that lands after the caller's own stat but before the setter's locked read must leave the row stale."""
    row = _note(mock_config, 0, body=_words(3, 20))
    path = Path(mock_config["vault_path"]) / row["note_path"]
    vault_index.index_note(db, str(path))
    old = _index_stat(db, str(path))
    earlier = (path.stat().st_mtime, path.stat().st_size)  # what a caller's own stat would have said
    time.sleep(0.01)
    path.write_text(path.read_text().replace(_words(3, 20), _words(4, 20)))  # lands before the locked read
    wrote = []
    capture_select.note_writer.set_frontmatter_fields(mock_config["vault_path"], str(path), {"status": "promoted"},
                                                      stat_out=wrote)
    assert wrote[0] != earlier and vault_index.mtime_unchanged(earlier[0], old["mtime"])
    assert vault_index.update_note_status(db, str(path), *wrote)
    got = _index_stat(db, str(path))
    assert got["status"] == "promoted" and got["mtime"] == old["mtime"] and got["size"] == old["size"]
    vault_index.ensure_index(mock_config["vault_path"], FOLDERS, db_path=db)
    conn = vault_index._connect(db)
    body = conn.execute("SELECT body FROM notes WHERE path = ?", (str(path),)).fetchone()["body"]
    conn.close()
    assert _words(4, 20) in body
