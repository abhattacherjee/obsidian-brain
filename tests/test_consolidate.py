import os, sys, json, sqlite3
from unittest.mock import patch
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))
import consolidate_cli
import vault_index


def _seed_notes(db, specs):
    conn = sqlite3.connect(db)
    for path, project, vec in specs:
        conn.execute(
            "INSERT INTO notes (path, type, project, title, body, mtime, tfidf_vector) "
            "VALUES (?, 'session', ?, ?, 'b', 1.0, ?)",
            (path, project, path, json.dumps(vec)),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_vault, monkeypatch):
    p = str(tmp_vault / "c.db")
    vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", p)
    # config points at the vault
    monkeypatch.setattr(consolidate_cli, "load_config",
                        lambda: {"vault_path": str(tmp_vault)})
    return p


def _fake_names(clusters, model="haiku", timeout=120):
    return ([{"name": f"Theme {i}", "summary": "s"} for i in range(len(clusters))], None)


def test_consolidate_seeds_clusters_of_three_plus(db):
    _seed_notes(db, [
        ("a.md", "proj", {"x": 1.0, "y": 0.9}),
        ("b.md", "proj", {"x": 1.0, "y": 0.8}),
        ("c.md", "proj", {"x": 0.9, "y": 1.0}),
        ("z.md", "proj", {"q": 1.0}),  # singleton, no theme
    ])
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=False)
    conn = sqlite3.connect(db)
    themes = conn.execute("SELECT name, note_count, project FROM themes").fetchall()
    members = conn.execute("SELECT note_path FROM theme_members ORDER BY note_path").fetchall()
    conn.close()
    assert len(themes) == 1
    assert themes[0][1] == 3 and themes[0][2] == "proj"
    assert [m[0] for m in members] == ["a.md", "b.md", "c.md"]


def test_consolidate_is_incremental_by_default(db):
    # Pre-existing theme + member; default run must NOT delete it.
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO themes (name, summary, centroid, note_count, created_date, updated_date, project) "
                 "VALUES ('Existing','s','{}',1,'2026-01-01','2026-01-01','proj')")
    conn.execute("INSERT INTO theme_members (theme_id, note_path, similarity, surprise, added_date) "
                 "VALUES (1,'old.md',0.9,0.7,'2026-01-01')")
    conn.commit(); conn.close()
    _seed_notes(db, [("a.md","proj",{"x":1.0}),("b.md","proj",{"x":1.0}),("c.md","proj",{"x":1.0})])
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=False)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0] == 2  # existing + 1 new
    assert conn.execute("SELECT surprise FROM theme_members WHERE note_path='old.md'").fetchone()[0] == 0.7
    conn.close()


def test_consolidate_full_wipes_then_reclusters(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO themes (name, summary, centroid, note_count, created_date, updated_date, project) "
                 "VALUES ('Stale','s','{}',1,'2026-01-01','2026-01-01','proj')")
    conn.execute("INSERT INTO theme_members (theme_id, note_path, similarity, surprise, added_date) "
                 "VALUES (1,'old.md',0.9,0.7,'2026-01-01')")
    conn.commit(); conn.close()
    _seed_notes(db, [("a.md","proj",{"x":1.0}),("b.md","proj",{"x":1.0}),("c.md","proj",{"x":1.0})])
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=True)
    conn = sqlite3.connect(db)
    names = [r[0] for r in conn.execute("SELECT name FROM themes").fetchall()]
    assert "Stale" not in names  # wiped
    assert conn.execute("SELECT COUNT(*) FROM theme_members WHERE note_path='old.md'").fetchone()[0] == 0
    conn.close()


def test_consolidate_per_project_scoping(db):
    _seed_notes(db, [
        ("p1/a.md","proj1",{"x":1.0}),("p1/b.md","proj1",{"x":1.0}),("p1/c.md","proj1",{"x":1.0}),
        ("p2/a.md","proj2",{"x":1.0}),("p2/b.md","proj2",{"x":1.0}),("p2/c.md","proj2",{"x":1.0}),
    ])
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=False)
    conn = sqlite3.connect(db)
    projs = sorted(r[0] for r in conn.execute("SELECT project FROM themes").fetchall())
    conn.close()
    assert projs == ["proj1", "proj2"]  # two separate themes, never merged across projects


def test_consolidate_haiku_failure_uses_deterministic_fallback_name(db):
    _seed_notes(db, [("a.md","proj",{"alpha":1.0,"beta":0.5,"gamma":0.3}),
                     ("b.md","proj",{"alpha":1.0,"beta":0.6,"gamma":0.2}),
                     ("c.md","proj",{"alpha":0.9,"beta":0.5,"gamma":0.4})])
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: (None, "haiku_timeout")):
        consolidate_cli.run_consolidate(full=False)
    conn = sqlite3.connect(db)
    name = conn.execute("SELECT name FROM themes").fetchone()[0]
    conn.close()
    assert name == "alpha / beta / gamma"  # top-3 centroid terms by weight, joined by " / "


def test_stats_reports_counts(db, capsys):
    _seed_notes(db, [("a.md","proj",{"x":1.0}),("b.md","proj",{"x":1.0}),("c.md","proj",{"x":1.0}),
                     ("u.md","proj",{"q":1.0})])
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=False)
    consolidate_cli.run_stats()
    out = capsys.readouterr().out
    assert "THEMES=1" in out
    assert "UNASSIGNED=1" in out  # u.md never clustered


def test_merge_combines_members_and_drops_second(db):
    conn = sqlite3.connect(db)
    for tid, name in [(1,"A"),(2,"B")]:
        conn.execute("INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
                     "VALUES (?,?,?,?,?,?,?,?)",(tid,name,"s",json.dumps({"x":1.0}),1,"d","d","proj"))
    conn.execute("INSERT INTO theme_members VALUES (1,'a.md',0.9,0.0,'2026-01-01')")
    conn.execute("INSERT INTO theme_members VALUES (2,'b.md',0.9,0.0,'2026-01-01')")
    # member vectors must exist for centroid recompute — use DISTINCT terms so
    # the merged centroid is detectable (x:0.5, y:0.5), not a collapse to one term.
    conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                 "VALUES ('a.md','session','proj','t','b',1.0,?)", (json.dumps({"x":1.0}),))
    conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                 "VALUES ('b.md','session','proj','t','b',1.0,?)", (json.dumps({"y":1.0}),))
    conn.commit(); conn.close()
    consolidate_cli.run_merge(1, 2)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM themes WHERE id=2").fetchone()[0] == 0
    assert conn.execute("SELECT note_count FROM themes WHERE id=1").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM theme_members WHERE theme_id=1").fetchone()[0] == 2
    # assert centroid is recomputed from both members' distinct term vectors
    import json as _json
    cen = _json.loads(conn.execute("SELECT centroid FROM themes WHERE id=1").fetchone()[0])
    conn.close()
    assert cen == pytest.approx({"x": 0.5, "y": 0.5})


def test_split_breaks_theme_into_subclusters(db):
    # one theme holding two clearly-separate groups -> split into two themes
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
                 "VALUES (1,'Mixed','s','{}',6,'d','d','proj')")
    grpA = [("a1.md",{"a":1.0}),("a2.md",{"a":1.0}),("a3.md",{"a":1.0})]
    grpB = [("b1.md",{"b":1.0}),("b2.md",{"b":1.0}),("b3.md",{"b":1.0})]
    for path, vec in grpA + grpB:
        conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                     "VALUES (?,'session','proj','t','b',1.0,?)",(path,json.dumps(vec)))
        conn.execute("INSERT INTO theme_members VALUES (1,?,0.9,0.0,'d')",(path,))
    conn.commit(); conn.close()
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_split(1)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM themes WHERE id=1").fetchone()[0] == 0  # old gone
    assert conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0] == 2  # two sub-themes
    conn.close()


def test_split_falls_back_on_haiku_failure(db):
    """Split still produces sub-themes with non-empty fallback names when Haiku fails."""
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
                 "VALUES (1,'Mixed','s','{}',6,'d','d','proj')")
    grpA = [("a1.md",{"a":1.0}),("a2.md",{"a":1.0}),("a3.md",{"a":1.0})]
    grpB = [("b1.md",{"b":1.0}),("b2.md",{"b":1.0}),("b3.md",{"b":1.0})]
    for path, vec in grpA + grpB:
        conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                     "VALUES (?,'session','proj','t','b',1.0,?)",(path,json.dumps(vec)))
        conn.execute("INSERT INTO theme_members VALUES (1,?,0.9,0.0,'d')",(path,))
    conn.commit(); conn.close()
    with patch("consolidate_cli.generate_theme_names", lambda *a, **k: (None, "haiku_timeout")):
        consolidate_cli.run_split(1)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM themes WHERE id=1").fetchone()[0] == 0  # old gone
    sub_themes = conn.execute("SELECT name FROM themes").fetchall()
    conn.close()
    assert len(sub_themes) == 2  # two sub-themes created
    assert all(row[0] for row in sub_themes)  # all names are non-empty fallbacks


def test_merge_self_is_rejected(db):
    """merge(a, a) must be a no-op: theme and its members must survive intact."""
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
                 "VALUES (1,'Solo','s','{\"x\":1.0}',2,'d','d','proj')")
    conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                 "VALUES ('m1.md','session','proj','t','b',1.0,'{\"x\":1.0}')")
    conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                 "VALUES ('m2.md','session','proj','t','b',1.0,'{\"y\":1.0}')")
    conn.execute("INSERT INTO theme_members VALUES (1,'m1.md',0.9,0.0,'d')")
    conn.execute("INSERT INTO theme_members VALUES (1,'m2.md',0.9,0.0,'d')")
    conn.commit(); conn.close()
    consolidate_cli.run_merge(1, 1)
    conn = sqlite3.connect(db)
    theme_count = conn.execute("SELECT COUNT(*) FROM themes WHERE id=1").fetchone()[0]
    member_count = conn.execute("SELECT COUNT(*) FROM theme_members WHERE theme_id=1").fetchone()[0]
    conn.close()
    assert theme_count == 1, "theme must still exist after self-merge"
    assert member_count == 2, "theme must still have 2 members after self-merge"


def test_consolidate_refreshes_activation(db):
    """After seeding a 3-note theme, its activation must be > 0.0 (the refresh
    pass ran). Without recompute_activation in run_consolidate it stays 0.0."""
    _seed_notes(db, [("a.md","proj",{"x":1.0}),("b.md","proj",{"x":1.0}),("c.md","proj",{"x":1.0})])
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_consolidate(full=False)
    conn = sqlite3.connect(db)
    act = conn.execute("SELECT activation FROM themes").fetchone()[0]
    conn.close()
    assert act > 0.0


def test_merge_refreshes_activation(db):
    """After a merge, activation on the surviving theme must be recomputed > 0.0."""
    conn = sqlite3.connect(db)
    for tid, name in [(1,"A"),(2,"B")]:
        conn.execute("INSERT INTO themes (id,name,summary,centroid,note_count,activation,created_date,updated_date,project) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",(tid,name,"s",json.dumps({"x":1.0}),1,0.0,"d","d","proj"))
    today = consolidate_cli._now_iso()[:10]
    conn.execute("INSERT INTO theme_members VALUES (1,'a.md',0.9,0.0,?)", (today,))
    conn.execute("INSERT INTO theme_members VALUES (2,'b.md',0.9,0.0,?)", (today,))
    conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                 "VALUES ('a.md','session','proj','t','b',1.0,?)", (json.dumps({"x":1.0}),))
    conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                 "VALUES ('b.md','session','proj','t','b',1.0,?)", (json.dumps({"y":1.0}),))
    conn.commit(); conn.close()
    consolidate_cli.run_merge(1, 2)
    conn = sqlite3.connect(db)
    act = conn.execute("SELECT activation FROM themes WHERE id=1").fetchone()[0]
    conn.close()
    assert act > 0.0


def test_merge_not_found_prints_marker_no_crash(db, capsys):
    """run_merge on absent theme IDs prints the not-found marker, does not crash,
    and never touches activation (no themes exist)."""
    consolidate_cli.run_merge(99, 100)
    out = capsys.readouterr().out
    assert "ERROR theme(s) not found a=99 b=100" in out
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0] == 0
    conn.close()


def test_consolidate_rolls_back_on_recompute_failure(db, monkeypatch):
    """A sqlite3.Error inside the seed transaction (raised by recompute_activation)
    must roll back the whole transaction: SystemExit(1) AND zero themes committed."""
    _seed_notes(db, [("a.md","proj",{"x":1.0}),("b.md","proj",{"x":1.0}),("c.md","proj",{"x":1.0})])

    def _boom(*a, **k):
        raise sqlite3.Error("boom")

    monkeypatch.setattr(consolidate_cli.themes, "recompute_activation", _boom)
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        with pytest.raises(SystemExit) as exc:
            consolidate_cli.run_consolidate(full=False)
    assert exc.value.code == 1
    conn = sqlite3.connect(db)
    # create_theme INSERTs happened before recompute_activation in the same
    # transaction; the rollback must undo them — zero themes committed.
    assert conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0] == 0
    conn.close()


def test_split_noop_when_cohesive(db, capsys):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
                 "VALUES (1,'Tight','s','{}',3,'d','d','proj')")
    for p in ["a.md","b.md","c.md"]:
        conn.execute("INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
                     "VALUES (?,'session','proj','t','b',1.0,?)",(p,json.dumps({"x":1.0})))
        conn.execute("INSERT INTO theme_members VALUES (1,?,0.9,0.0,'d')",(p,))
    conn.commit(); conn.close()
    with patch("consolidate_cli.generate_theme_names", _fake_names):
        consolidate_cli.run_split(1)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM themes WHERE id=1").fetchone()[0] == 1  # unchanged
    conn.close()
    assert "NO_SPLIT" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# T1: run_stats WARN line fires when note_count STRICTLY exceeds cap
# ---------------------------------------------------------------------------

def test_stats_flags_oversized_theme(tmp_vault, monkeypatch, capsys):
    """WARN + split suggestion printed when a theme has note_count > cap."""
    import vault_index as _vi
    p = str(tmp_vault / "c.db")
    _vi.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", p)
    monkeypatch.setattr(
        consolidate_cli, "load_config",
        lambda: {"vault_path": str(tmp_vault), "consolidate_max_theme_size": 5},
    )
    conn = sqlite3.connect(p)
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
        "VALUES (1,'BigTheme','s','{}',6,'d','d','proj')"
    )
    conn.commit(); conn.close()
    consolidate_cli.run_stats()
    out = capsys.readouterr().out
    assert "WARN theme id=1 has 6 members" in out
    assert "/consolidate split 1" in out


# ---------------------------------------------------------------------------
# T2: run_stats stays silent when note_count == cap (strict > boundary)
# ---------------------------------------------------------------------------

def test_stats_silent_at_cap_boundary(tmp_vault, monkeypatch, capsys):
    """No WARN when note_count is exactly at the cap — boundary is strict >."""
    import vault_index as _vi
    p = str(tmp_vault / "c.db")
    _vi.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", p)
    monkeypatch.setattr(
        consolidate_cli, "load_config",
        lambda: {"vault_path": str(tmp_vault), "consolidate_max_theme_size": 5},
    )
    conn = sqlite3.connect(p)
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
        "VALUES (1,'ExactCap','s','{}',5,'d','d','proj')"
    )
    conn.commit(); conn.close()
    consolidate_cli.run_stats()
    out = capsys.readouterr().out
    assert "WARN" not in out


# ---------------------------------------------------------------------------
# T2b: run_stats WARN loop iterates ALL oversized themes, not just the first
# ---------------------------------------------------------------------------

def test_stats_flags_multiple_oversized_themes(tmp_vault, monkeypatch, capsys):
    """WARN is printed for every oversized theme, not just the first one found."""
    import vault_index as _vi
    p = str(tmp_vault / "c.db")
    _vi.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=p)
    monkeypatch.setenv("OBSIDIAN_BRAIN_DB", p)
    monkeypatch.setattr(
        consolidate_cli, "load_config",
        lambda: {"vault_path": str(tmp_vault), "consolidate_max_theme_size": 5},
    )
    conn = sqlite3.connect(p)
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
        "VALUES (1,'BigA','s','{}',6,'d','d','proj')"
    )
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
        "VALUES (2,'BigB','s','{}',7,'d','d','proj')"
    )
    conn.commit(); conn.close()
    consolidate_cli.run_stats()
    out = capsys.readouterr().out
    assert "WARN theme id=1 has 6 members" in out
    assert "WARN theme id=2 has 7 members" in out
    assert "/consolidate split 1" in out
    assert "/consolidate split 2" in out


# ---------------------------------------------------------------------------
# T3: run_merge(a, a) emits the DISTINCT self-merge message, not not-found
# ---------------------------------------------------------------------------

def test_merge_self_emits_distinct_message(db, capsys):
    """run_merge(1, 1) must say 'cannot merge a theme with itself', NOT 'not found'."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO themes (id,name,summary,centroid,note_count,created_date,updated_date,project) "
        "VALUES (1,'Solo','s','{\"x\":1.0}',1,'d','d','proj')"
    )
    conn.execute(
        "INSERT INTO notes (path,type,project,title,body,mtime,tfidf_vector) "
        "VALUES ('m1.md','session','proj','t','b',1.0,'{\"x\":1.0}')"
    )
    conn.execute("INSERT INTO theme_members VALUES (1,'m1.md',0.9,0.0,'d')")
    conn.commit(); conn.close()
    consolidate_cli.run_merge(1, 1)
    out = capsys.readouterr().out
    assert "cannot merge a theme with itself" in out
    assert "theme(s) not found" not in out
