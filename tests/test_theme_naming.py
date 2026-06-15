import os, sys, json
from unittest.mock import patch
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))
import obsidian_utils


def _clusters():
    return [
        {"top_terms": ["tfidf", "theme", "vector"], "sample_titles": ["TF-IDF perf", "theme join"]},
        {"top_terms": ["git", "merge", "branch"], "sample_titles": ["git flow finish"]},
    ]


def test_generate_theme_names_parses_json_response():
    payload = json.dumps([
        {"name": "TF-IDF & Themes", "summary": "Vector + theme work."},
        {"name": "Git Flow", "summary": "Branch/merge discipline."},
    ])

    class R:
        returncode = 0
        stdout = "```json\n" + payload + "\n```"
        stderr = ""

    with patch("obsidian_utils.subprocess.run", return_value=R()):
        names, reason = obsidian_utils.generate_theme_names(_clusters(), model="haiku")
    assert reason is None
    assert names[0]["name"] == "TF-IDF & Themes"
    assert names[1]["summary"] == "Branch/merge discipline."


def test_generate_theme_names_cli_missing_returns_reason():
    with patch("obsidian_utils.subprocess.run", side_effect=FileNotFoundError()):
        names, reason = obsidian_utils.generate_theme_names(_clusters())
    assert names is None
    assert reason == "haiku_subprocess_error"


def test_generate_theme_names_count_mismatch_is_failure():
    # model returned the wrong number of names -> treat as failure, caller falls back
    payload = json.dumps([{"name": "only one", "summary": "x"}])

    class R:
        returncode = 0
        stdout = payload
        stderr = ""

    with patch("obsidian_utils.subprocess.run", return_value=R()):
        names, reason = obsidian_utils.generate_theme_names(_clusters())
    assert names is None
    assert reason == "count_mismatch"


def test_generate_theme_names_empty_clusters_short_circuits():
    # must not spawn a subprocess for an empty list
    with patch("obsidian_utils.subprocess.run", side_effect=AssertionError("should not spawn")):
        names, reason = obsidian_utils.generate_theme_names([])
    assert names == []
    assert reason is None
