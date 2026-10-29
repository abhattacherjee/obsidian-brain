"""Read-only selection replay: all stores and logs are temporary copies."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import selection_replay as replay


START = dt.datetime(2026, 10, 29, 23, 59, 40, tzinfo=dt.timezone.utc).timestamp()
SESSION = "c5e16f5f-session"


def _row(cid, offset=0, *, session=SESSION, **fields):
    return dict(id=cid, kind="new", status="active", session_id=session,
                note_path=f"claude-insights/_provisional/{cid}.md", ts=START + offset,
                score=8, novelty=0.4, type="insight") | fields


def _log(offset, outcome, *, sid=SESSION[:8], detail="slices=1;promoted=1;demoted=0"):
    when = dt.datetime.fromtimestamp(START + offset, dt.timezone.utc).isoformat()
    return f"{when} Capture project=X sid={sid} outcome={outcome} detail={detail}\n"


def _write_case(tmp_path, *, rows=None, vectors=None, access=(), logs=None):
    if rows is None:
        rows = [_row("a"), _row("b", 15, novelty=0.5), _row("c", 25, novelty=0.6)]
    if vectors is None:
        # B displaces A at run 2. C excludes B at run 3, letting A return.
        vectors = {"a.md": {"x": 1}, "b.md": {"x": 0.8, "y": 0.6}, "c.md": {"y": 1}}
    ledger, db, log = tmp_path / "copy.jsonl", tmp_path / "copy.db", tmp_path / "hook.log"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    conn = sqlite3.connect(str(db))  # noqa: vault-db-connect -- two-table fixture in tmp_path only
    with conn:
        conn.executescript("CREATE TABLE notes(path TEXT PRIMARY KEY, tfidf_vector TEXT);"
                           "CREATE TABLE access_log(note_path TEXT, context_type TEXT, timestamp REAL);")
        conn.executemany("INSERT INTO notes VALUES (?, ?)",
                         [(f"/copied-vault/claude-insights/{name}", json.dumps(vector))
                          for name, vector in vectors.items()])
        conn.executemany("INSERT INTO access_log VALUES (?, ?, ?)", access)
    conn.close()
    if logs is None:
        logs = [f"{dt.datetime.fromtimestamp(START, dt.timezone.utc).isoformat()} SessionStart outcome=ok\n",
                _log(10, "promoted", detail="cid=a;merit=.68;rank=1;why=pick"),
                _log(10, "saved:1"),
                _log(20, "demoted", detail="cid=a;merit=.68;rank=2;why=displaced"),
                _log(20, "promoted", detail="cid=b;merit=.71;rank=1;why=pick"),
                _log(20, "saved:1"),
                _log(30, "demoted", detail="cid=b;merit=.71;rank=3;why=below-cap"),
                _log(30, "promoted", detail="cid=c;merit=.74;rank=1;why=pick"),
                _log(30, "promoted", detail="cid=a;merit=.68;rank=2;why=pick"),
                _log(30, "saved:1")]
    log.write_text("".join(logs), encoding="utf-8")
    return {"ledger": ledger, "db": db, "log": log}


def _argv(case, *extra):
    return [part for key, path in case.items() for part in (f"--{key}", str(path))] + list(extra)


def _report(case, capsys, *extra):
    assert replay.main(_argv(case, "--json", *extra)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_three_runs_hysteresis_and_live_validation(tmp_path, capsys):
    report = _report(_write_case(tmp_path), capsys, "--bonus", "0.1", "--bonus", "0.02", "--bonus", "0")
    assert [arm["bonus"] for arm in report["arms"]] == [0, 0.1, 0.02]
    today, sticky_arm = report["arms"][:2]
    assert today["sessions_replayed"] == 1
    assert today["sessions"][SESSION]["moves_by_note"] == {"a": 3, "b": 2, "c": 1}
    assert today["notes_moved"] == 3
    assert today["notes_moved_more_than_once"] == 2
    assert today["notes_moved_more_than_twice"] == 1
    assert today["max_moves_on_one_note"] == 3
    assert sticky_arm["notes_moved_more_than_once"] == 0
    assert sticky_arm["max_moves_on_one_note"] == 1
    assert today["mean_final_merit"] == pytest.approx(0.71)
    assert sticky_arm["mean_final_merit"] == pytest.approx(0.71)
    assert sticky_arm["merit_drop"] == pytest.approx(0)
    validation = report["validation"]
    assert validation["sessions"] == 1
    assert validation["arm_0"]["total_moves"] == validation["live"]["total_moves"] == 6
    assert validation["arm_0"]["notes_moved_more_than_once"] == 2
    assert validation["live"]["notes_moved_more_than_once"] == 2
    assert validation["largest_gaps"][0]["gap"] == 0


def test_merit_drop_uses_base_merit(tmp_path, capsys):
    case = _write_case(tmp_path, logs=[_log(10, "saved:1"), _log(20, "saved:1")])
    report = _report(case, capsys, "--bonus", "0.1")
    assert report["arms"][0]["mean_final_merit"] == pytest.approx(0.71)
    assert report["arms"][1]["mean_final_merit"] == pytest.approx(0.68)
    assert report["arms"][1]["merit_drop"] == pytest.approx(0.03)


@pytest.mark.parametrize("offset,expected", [(10, {}), (15, {"a": 1}), (20, {"a": 1})])
def test_sticky_never_moves_after_first_access(tmp_path, capsys, offset, expected):
    # Basename matching survives movement from _provisional into the promoted home.
    case = _write_case(tmp_path, access=[("/other/home/a.md", "tool-open", START + offset),
                                       ("/other/home/a.md", "search", START + 100)])
    report = _report(case, capsys)
    result = report["arms"][0]["sessions"][SESSION]
    assert {cid: n for cid, n in result["moves_by_note"].items() if cid == "a"} == expected
    assert "a" not in result["final_picks"]


def test_related_and_future_access_do_not_freeze(tmp_path, capsys):
    case = _write_case(tmp_path, access=[("/vault/a.md", "related", START + 15),
                                       ("/vault/a.md", "recall", START + 15),
                                       ("/vault/a.md", "search", START + 31)])
    result = _report(case, capsys)["arms"][0]["sessions"][SESSION]
    assert result["moves_by_note"]["a"] == 3


def test_duplicates_superseded_rejected_inactive_and_update_rows_are_excluded(tmp_path, capsys):
    rows = [_row("a"), _row("duplicate", duplicate_of="a", score=10),
            _row("superseded", superseded_by="a", score=10),
            _row("rejected", note_path="claude-insights/_rejected/rejected.md", score=10),
            _row("inactive", status="retracted", score=10), _row("update", kind="update", score=10)]
    case = _write_case(tmp_path, rows=rows, vectors={f"{row['id']}.md": {row['id']: 1} for row in rows})
    result = _report(case, capsys)["arms"][0]["sessions"][SESSION]
    assert result["moves_by_note"] == {"a": 1}
    assert result["final_picks"] == ["a"]


def test_merge_preserves_first_ts_and_applies_final_fields(tmp_path, capsys):
    rows = [_row("a", score=1), dict(id="a", ts=START + 999, score=10, novelty=1)]
    case = _write_case(tmp_path, rows=rows, vectors={"a.md": {"x": 1}})
    arm = _report(case, capsys)["arms"][0]
    assert arm["sessions"][SESSION]["moves_by_note"] == {"a": 1}
    assert arm["mean_final_merit"] == 1


def test_ambiguous_and_unmapped_prefixes_counted_once(tmp_path, capsys):
    rows = [_row("a"), _row("x", session="deadbeef-one"),
            _row("y", session="deadbeef-two", status="retracted")]
    logs = [_log(10, "saved:1"), _log(10, "saved:1", sid="deadbeef"),
            _log(20, "saved:1", sid="deadbeef"), _log(30, "saved:1", sid="unmapped")]
    report = _report(_write_case(tmp_path, rows=rows, logs=logs), capsys)
    assert report["skipped"]["ambiguous_sid"] == 1
    assert report["skipped"]["unmapped_sid"] == 1
    assert list(report["arms"][0]["sessions"]) == [SESSION]


@pytest.mark.parametrize("store", ["db", "ledger"])
@pytest.mark.parametrize("form", ["direct", "symlink", "hardlink", "relative"])
def test_refuses_live_paths_before_any_read(tmp_path, monkeypatch, capsys, store, form):
    case = _write_case(tmp_path)
    live_dir = tmp_path / "secure" / "capture"
    live_dir.mkdir(parents=True)
    live = tmp_path / "live.db" if store == "db" else live_dir / replay.capture_state.LEDGER_NAME
    live.write_text("must not open", encoding="utf-8")
    monkeypatch.setattr(replay.vault_index, "_default_db_path", lambda: str(tmp_path / "live.db"))
    monkeypatch.setattr(replay.capture_state, "capture_dir", lambda: live_dir)
    monkeypatch.setattr(replay.capture_state.obsidian_utils, "_SECURE_DIR", str(live_dir.parent))
    monkeypatch.chdir(tmp_path)
    if form == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(live)
        case[store] = alias
    elif form == "hardlink":
        alias = tmp_path / "hardalias"
        os.link(live, alias)
        case[store] = alias
    elif form == "relative":
        case[store] = "./" + str(live.relative_to(tmp_path))
    else:
        case[store] = live

    def forbidden(*args, **kwargs):
        pytest.fail("a refused input must not be opened")

    monkeypatch.setattr(replay.sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert replay.main(_argv(case)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR: refusing" in captured.err


@pytest.mark.parametrize("store,relative", [
    ("db", ".claude/obsidian-brain-vault.db"),
    ("ledger", ".claude/obsidian-brain/capture/captures.jsonl"),
])
def test_literal_home_live_paths_also_refused(tmp_path, monkeypatch, capsys, store, relative):
    case = _write_case(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(replay.vault_index, "_default_db_path", lambda: str(tmp_path / "override.db"))
    monkeypatch.setattr(replay.capture_state, "capture_dir", lambda: tmp_path / "override-capture")
    case[store] = tmp_path / relative
    assert replay.main(_argv(case)) == 2
    assert "ERROR:" in capsys.readouterr().err


def test_no_writing_helpers_or_note_reads(tmp_path, monkeypatch, capsys):
    case = _write_case(tmp_path)
    originals = {path: path.read_bytes() for path in case.values()}

    def forbidden(*args, **kwargs):
        pytest.fail("replay called a writing helper")

    for module, names in [(replay.capture_select, ("plan_session", "run_selection")),
                          (replay.capture_state, ("ledger_update", "ledger_append", "capture_dir")),
                          (replay.vault_index, ("ensure_index", "_connect"))]:
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    connect = sqlite3.connect
    calls = []

    def read_only_connect(database, **kwargs):
        assert database.startswith("file:") and database.endswith("?mode=ro")
        assert kwargs == {"uri": True}
        conn = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE forbidden_write(x)")
        calls.append(database)
        return conn

    monkeypatch.setattr(replay.sqlite3, "connect", read_only_connect)
    _report(case, capsys)
    assert len(calls) == 1
    assert {path: path.read_bytes() for path in case.values()} == originals


@pytest.mark.parametrize("bonus", ["-0.01", "bad", "nan", "inf", "-inf"])
def test_invalid_bonus_is_usage_error(tmp_path, capsys, bonus):
    case = _write_case(tmp_path)
    with pytest.raises(SystemExit) as exc:
        replay.main(_argv(case, f"--bonus={bonus}"))
    assert exc.value.code == 2
    assert "ERROR:" in capsys.readouterr().err


@pytest.mark.parametrize("store", ["ledger", "db", "log"])
@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_input_failure_exits_one(tmp_path, monkeypatch, capsys, store, kind):
    case = _write_case(tmp_path)
    target = case[store]
    if kind == "missing":
        target.unlink()
    elif kind == "directory":
        target.unlink()
        target.mkdir()
    elif store == "db":
        def denied(*args, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")
        monkeypatch.setattr(replay.sqlite3, "connect", denied)
    else:
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError("unreadable copy")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    assert replay.main(_argv(case)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    if kind == "missing":
        assert not target.exists()


@pytest.mark.parametrize("logs,message", [
    ([_log(10, "promoted", detail="cid=a;merit=.68;rank=1;why=pick")], "no saved lines"),
    ([_log(10, "saved:1", sid="notfound")], "map to ledger"),
    ([_log(-10, "saved:1")], "0 replayable sessions"),
])
def test_empty_or_stale_logs_fail(tmp_path, capsys, logs, message):
    case = _write_case(tmp_path, logs=logs)
    assert replay.main(_argv(case)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_all_ambiguous_is_not_successful_no_churn(tmp_path, capsys):
    case = _write_case(tmp_path, rows=[_row("a"), _row("b", session=SESSION[:8] + "-other")])
    assert replay.main(_argv(case)) == 1
    assert "'ambiguous_sid': 1" in capsys.readouterr().err


def test_missing_vectors_skip_sessions_and_count_rows_once(tmp_path, capsys):
    case = _write_case(tmp_path, rows=[_row("a"), _row("missing"), _row("absent", session="12345678-other")],
                       vectors={"a.md": {"x": 1}},
                       logs=[_log(10, "saved:1"), _log(20, "saved:1"),
                             _log(10, "saved:1", sid="12345678")])
    report = _report(case, capsys)
    assert report["skipped"]["rows_without_vector"] == 2
    assert report["skipped"]["sessions_without_vectors"] == 1
    assert report["arms"][0]["sessions_replayed"] == 1


@pytest.mark.parametrize("vector", [None, {}, {"zero": 0}])
def test_session_without_vectors_is_error(tmp_path, capsys, vector):
    case = _write_case(tmp_path, rows=[_row("a")], vectors={"a.md": vector})
    assert replay.main(_argv(case)) == 1
    message = capsys.readouterr().err
    assert "0 replayable sessions" in message
    assert "'sessions_without_vectors': 1" in message


@pytest.mark.parametrize("vector", [[1], {"x": "oops"}, {"x": float("nan")}])
def test_corrupt_vector_fails_closed(tmp_path, capsys, vector):
    case = _write_case(tmp_path, vectors={"a.md": vector})
    assert replay.main(_argv(case)) == 1
    assert "vector" in capsys.readouterr().err


@pytest.mark.parametrize("store", ["ledger", "log"])
def test_non_utf8_input_fails_cleanly(tmp_path, capsys, store):
    case = _write_case(tmp_path)
    case[store].write_bytes(b"\xff\xfe")
    assert replay.main(_argv(case)) == 1
    assert "ERROR:" in capsys.readouterr().err


def test_malformed_ledger_rows_are_disclosed(tmp_path, capsys):
    case = _write_case(tmp_path)
    with case["ledger"].open("a", encoding="utf-8") as source:
        source.write('broken json\n[]\n{"id": 3}\n')
    report = _report(case, capsys)
    assert report["skipped"]["invalid_ledger_rows"] == 3


@pytest.mark.parametrize("changes", [{"ts": None}, {"session_id": None}, {"note_path": None}])
def test_invalid_new_row_fails_cleanly(tmp_path, capsys, changes):
    case = _write_case(tmp_path, rows=[_row("a", **changes)])
    assert replay.main(_argv(case)) == 1
    assert "ERROR:" in capsys.readouterr().err


def test_cross_utc_midnight_and_rotated_logs_sort_by_full_time(tmp_path, capsys):
    case = _write_case(tmp_path, logs=[_log(30, "saved:1"), _log(20, "saved:1")])
    rotated = tmp_path / "hook.log.1"
    rotated.write_text(_log(10, "saved:1"), encoding="utf-8")
    report = _report(case, capsys, "--log", str(rotated))
    assert report["arms"][0]["sessions"][SESSION]["moves_by_note"] == {"a": 3, "b": 2, "c": 1}
    assert report["window_start_utc"] == "2026-10-29T23:59:50+00:00"


def test_validation_excludes_partial_session_and_nonselection_moves(tmp_path, capsys):
    rows = [_row("a", -1, session="11111111-before"), _row("c", 0)]
    logs = [_log(0, "saved:1"), _log(10, "saved:1", sid="11111111"),
            _log(10, "promoted", detail="cid=c;merit=.74;rank=1;why=pick")]
    logs += [_log(10, "promoted", detail=f"cid=c;merit=.74;rank=1;why={why}")
             for why in ("sticky-access", "sticky-hand-update", "duplicate", "superseded", "issue-closed")]
    report = _report(_write_case(tmp_path, rows=rows, logs=logs), capsys)
    assert report["arms"][0]["sessions_replayed"] == 2
    assert report["validation"]["sessions"] == 1
    assert report["validation"]["live"]["total_moves"] == 1
    assert report["validation"]["largest_gaps"][0]["session_id"] == SESSION


def test_merit_is_mean_of_session_means(tmp_path, capsys):
    rows = [_row("a", score=1, novelty=0),
            _row("b", session="12345678-other", score=10, novelty=1),
            _row("c", session="12345678-other", score=10, novelty=1)]
    case = _write_case(tmp_path, rows=rows,
                       vectors={f"{cid}.md": {cid: 1} for cid in "abc"},
                       logs=[_log(10, "saved:1"), _log(10, "saved:2", sid="12345678")])
    arm = _report(case, capsys)["arms"][0]
    assert arm["sessions_replayed"] == 2
    assert arm["mean_final_merit"] == pytest.approx((0.07 + 1) / 2)


def test_default_cap_and_decisions_start_provisional(tmp_path, capsys):
    rows = [_row(f"n{i:02}", type="decision") for i in range(11)]
    case = _write_case(tmp_path, rows=rows,
                       vectors={f"{row['id']}.md": {row['id']: 1} for row in rows})
    arm = _report(case, capsys)["arms"][0]
    assert len(arm["sessions"][SESSION]["final_picks"]) == replay.capture_select.CAP == 10
    assert arm["notes_moved"] == 10
    assert arm["mean_final_merit"] == pytest.approx(0.7)


def test_all_sticky_is_explicitly_no_final_merit(tmp_path, capsys):
    case = _write_case(tmp_path, rows=[_row("a")], vectors={"a.md": {"x": 1}},
                       access=[("/vault/a.md", "search", START + 5)])
    arm = _report(case, capsys)["arms"][0]
    assert arm["sessions_replayed"] == 1
    assert arm["notes_moved"] == 0
    assert arm["sessions_with_final_picks"] == 0
    assert arm["mean_final_merit"] is None
    assert arm["merit_drop"] is None


def test_validation_largest_ten_gaps_use_absolute_gap(tmp_path, capsys):
    rows, logs = [], []
    for i in range(12):
        session = f"{i:08}-session"
        cid = f"n{i:02}"
        rows.append(_row(cid, session=session))
        logs.append(_log(0, "saved:1", sid=session[:8]))
        logs.extend(_log(1, "promoted", sid=session[:8], detail=f"cid={cid};why=pick")
                    for _ in range(i))
    case = _write_case(tmp_path, rows=rows,
                       vectors={f"{row['id']}.md": {row['id']: 1} for row in rows}, logs=logs)
    validation = _report(case, capsys)["validation"]
    assert validation["sessions"] == 12
    assert len(validation["largest_gaps"]) == 10
    assert [row["gap"] for row in validation["largest_gaps"]] == [*range(-10, -1), 1]


@pytest.mark.parametrize("kind", ["corrupt", "empty", "missing_access_table", "wrong_context_column"])
def test_broken_db_is_not_zero_churn(tmp_path, capsys, kind):
    case = _write_case(tmp_path)
    if kind == "corrupt":
        case["db"].write_bytes(b"not sqlite")
    elif kind == "empty":
        case["db"].write_bytes(b"")
    else:
        conn = sqlite3.connect(str(case["db"]))  # noqa: vault-db-connect -- mutate tmp fixture only
        with conn:
            conn.execute("DROP TABLE access_log")
            if kind == "wrong_context_column":
                conn.execute("CREATE TABLE access_log(note_path TEXT, context TEXT, timestamp REAL)")
        conn.close()
    assert replay.main(_argv(case)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err


def test_ambiguous_basename_is_an_error(tmp_path, capsys):
    case = _write_case(tmp_path)
    conn = sqlite3.connect(str(case["db"]))  # noqa: vault-db-connect -- mutate tmp fixture only
    with conn:
        conn.execute("INSERT INTO notes VALUES (?, ?)", ("/other/a.md", '{"different": 1}'))
    conn.close()
    assert replay.main(_argv(case)) == 1
    assert "ambiguous DB basename: a.md" in capsys.readouterr().err


@pytest.mark.parametrize("line", [
    "invalid-time Capture sid=c5e16f5f outcome=saved:1\n",
    "2026-10-29T23:59:50 Capture sid=c5e16f5f outcome=saved:1\n",
    _log(10, "saved:bad"),
    _log(10, "saved:1", sid="short"),
    _log(10, "promoted", detail="merit=.68;why=pick"),
])
def test_malformed_relevant_log_line_fails(tmp_path, capsys, line):
    case = _write_case(tmp_path, logs=[_log(20, "saved:1"), line])
    assert replay.main(_argv(case)) == 1
    assert "ERROR:" in capsys.readouterr().err


def test_markdown_output(tmp_path, capsys):
    case = _write_case(tmp_path)
    assert replay.main(_argv(case)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    for text in ("| Bonus | Sessions |", "Mean final merit", "Merit drop", "Validation:",
                 "Notes with >1 move", "Largest absolute gaps", "Skipped:", "Inputs:"):
        assert text in captured.out


def test_uri_quoting_and_real_cli_from_another_directory(tmp_path):
    case = _write_case(tmp_path)
    special = tmp_path / "copy ?#%.db"
    case["db"].rename(special)
    case["db"] = special
    result = subprocess.run([sys.executable, str(Path(replay.__file__).resolve()), *_argv(case, "--json")],
                            cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["inputs"]["db"] == str(special)
    assert report["arms"][0]["total_moves"] == 6


def test_intermediate_slice_moves_are_replayed(tmp_path, capsys):
    # One batch of three slices: promoted/demoted lines at slice times (10, 20),
    # a single saved line at the end (30). Rows arrive between the slices.
    rows = [_row("a"), _row("b", 15, novelty=0.5), _row("c", 25, novelty=0.6)]
    logs = [_log(10, "promoted", detail="cid=a;merit=.68;rank=1;why=pick"),
            _log(20, "demoted", detail="cid=a;merit=.68;rank=2;why=displaced"),
            _log(20, "promoted", detail="cid=b;merit=.71;rank=1;why=pick"),
            _log(30, "saved:3")]
    report = _report(_write_case(tmp_path, rows=rows, logs=logs), capsys)
    moves = report["arms"][0]["sessions"][SESSION]["moves_by_note"]
    # Replays at 10, 20, 30: a picked, then b displaces a, then c excludes b and a returns.
    assert moves == {"a": 3, "b": 2, "c": 1}


def test_saved_line_after_last_move_with_same_pool_is_not_a_separate_selection(tmp_path, capsys, monkeypatch):
    # Last move at 20, saved line at 21, no ledger row between: live ran no selection at 21.
    rows = [_row("a"), _row("b", 15, novelty=0.5)]
    logs = [_log(10, "promoted", detail="cid=a;merit=.68;rank=1;why=pick"),
            _log(20, "demoted", detail="cid=a;merit=.68;rank=2;why=displaced"),
            _log(20, "promoted", detail="cid=b;merit=.71;rank=1;why=pick"),
            _log(21, "saved:2")]
    real, calls = replay.capture_select.select, []

    def counting(candidates, *args, **kwargs):
        calls.append(len(candidates))
        return real(candidates, *args, **kwargs)

    monkeypatch.setattr(replay.capture_select, "select", counting)
    report = _report(_write_case(tmp_path, rows=rows, logs=logs), capsys, "--bonus", "0.05")
    # Replays at 10 and 20 only; the saved time 21 has the same pool as 20. One run per arm per time.
    assert len(calls) == 2 * len(report["arms"])
    assert report["arms"][0]["sessions"][SESSION]["moves_by_note"] == {"a": 2, "b": 1}


def test_same_pool_replayed_60_seconds_later_is_a_separate_selection(tmp_path, capsys, monkeypatch):
    # A later batch adds no rows, so the pool is unchanged, but it is a real second selection.
    rows = [_row("a"), _row("b", 15, novelty=0.5)]
    logs = [_log(10, "promoted", detail="cid=a;merit=.68;rank=1;why=pick"),
            _log(20, "demoted", detail="cid=a;merit=.68;rank=2;why=displaced"),
            _log(20, "promoted", detail="cid=b;merit=.71;rank=1;why=pick"),
            _log(80, "saved:2")]
    real, calls = replay.capture_select.select, []

    def counting(candidates, *args, **kwargs):
        calls.append(len(candidates))
        return real(candidates, *args, **kwargs)

    monkeypatch.setattr(replay.capture_select, "select", counting)
    report = _report(_write_case(tmp_path, rows=rows, logs=logs), capsys, "--bonus", "0.05")
    assert len(calls) == 3 * len(report["arms"])


def test_fractional_ledger_ts_counts_in_same_second_selection(tmp_path, capsys):
    rows = [_row("a", 0.25)]
    logs = [_log(0, "saved:1")]
    report = _report(_write_case(tmp_path, rows=rows, logs=logs), capsys)
    assert report["arms"][0]["sessions"][SESSION]["final_picks"] == ["a"]
