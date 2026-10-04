from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run(args, stdin="", home=None, db=None):
    env = dict(os.environ)
    if home:
        env["HOME"] = str(home)
    if db:
        env["OBSIDIAN_BRAIN_DB"] = str(db)
    env["CLAUDE_CODE_SESSION_ID"] = "wiki-cli-test"
    return subprocess.run([sys.executable, str(REPO / "hooks" / "wiki.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env, timeout=60)


def test_rule_prints_json():
    r = run(["rule"])
    assert r.returncode == 0 and "ASD-STE100" in json.loads(r.stdout)["rule"]


def test_unknown_subcommand_exits_2():
    assert run(["nope"]).returncode == 2


def test_oversized_stdin_is_refused(tmp_path):
    r = run(["count"], stdin="x" * 1_000_001, home=tmp_path)
    assert r.returncode == 1 and "ERROR:" in r.stderr and "too large" in r.stderr


def test_bad_json_is_refused(tmp_path):
    r = run(["count"], stdin="{not json", home=tmp_path)
    assert r.returncode == 1 and "ERROR:" in r.stderr
