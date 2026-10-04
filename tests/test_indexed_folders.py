"""indexed_folders(config): the one list of folders user-facing search indexes (#383)."""
from __future__ import annotations

import obsidian_utils
from obsidian_utils import indexed_folders


def test_default_has_wiki_folder():
    assert obsidian_utils._DEFAULTS["wiki_folder"] == "claude-wiki"


def test_order_and_defaults_from_empty_config():
    assert indexed_folders({}) == ["claude-sessions", "claude-insights", "claude-wiki"]


def test_uses_configured_names():
    cfg = {"sessions_folder": "s", "insights_folder": "i", "wiki_folder": "w"}
    assert indexed_folders(cfg) == ["s", "i", "w"]


def test_empty_or_none_wiki_folder_disables_it():
    assert indexed_folders({"wiki_folder": ""}) == ["claude-sessions", "claude-insights"]
    assert indexed_folders({"wiki_folder": None}) == ["claude-sessions", "claude-insights"]


def test_duplicate_names_are_scanned_once():
    cfg = {"sessions_folder": "notes", "insights_folder": "notes", "wiki_folder": "notes"}
    assert indexed_folders(cfg) == ["notes"]


def test_traversal_wiki_folder_is_dropped_with_warning(capsys):
    assert indexed_folders({"wiki_folder": "../outside"}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_absolute_wiki_folder_is_dropped(capsys):
    assert indexed_folders({"wiki_folder": "/tmp/x"}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_non_string_wiki_folder_is_dropped(capsys):
    assert indexed_folders({"wiki_folder": 7}) == ["claude-sessions", "claude-insights"]
    assert "wiki_folder" in capsys.readouterr().err


def test_nested_wiki_folder_is_allowed():
    assert indexed_folders({"wiki_folder": "kb/claude-wiki"})[-1] == "kb/claude-wiki"


def test_does_not_mutate_input():
    cfg = {"wiki_folder": "w"}
    indexed_folders(cfg)
    assert cfg == {"wiki_folder": "w"}
