"""#471: lesson config defaults, setup folder and dashboard list, vault-ask drop rule."""
from pathlib import Path

import obsidian_utils

REPO = Path(__file__).resolve().parent.parent
SETUP = (REPO / "skills" / "obsidian-setup" / "SKILL.md").read_text()
CONFIG_SKILL = (REPO / "skills" / "vault-config" / "SKILL.md").read_text()
ASK = (REPO / "skills" / "vault-ask" / "SKILL.md").read_text()

LESSON_DEFAULTS = {"lessons_enabled": True, "lessons_recent_days": 90,
                   "lessons_concurrency": 8, "project_repos": {}}
DASHBOARDS = ["sessions-overview.md", "project-index.md", "weekly-review.md",
              "learning-velocity.md", "decision-timeline.md", "open-items.md",
              "retro-lessons.md"]


def test_defaults_include_lesson_keys():
    for key, value in LESSON_DEFAULTS.items():
        assert obsidian_utils._DEFAULTS[key] == value, key


def test_setup_creates_lessons_folder():
    assert '"$VAULT_PATH/claude-insights/lessons"' in SETUP


def test_setup_fresh_config_writes_lesson_keys():
    for key in LESSON_DEFAULTS:
        assert f'"{key}":' in SETUP, key


def test_setup_summary_lists_seven_dashboards():
    line = next(l for l in SETUP.splitlines() if l.startswith("> - Dashboards installed:"))
    assert [d for d in DASHBOARDS if f"`{d}`" in line] == DASHBOARDS


def test_vault_config_offers_lesson_keys():
    for key in LESSON_DEFAULTS:
        assert f" {key} " in CONFIG_SKILL, key


def test_vault_config_never_passes_the_map_as_an_argument():
    assert 'json.load(sys.stdin) if sys.argv[2] == "-"' in CONFIG_SKILL


def test_vault_ask_drops_claude_lesson_rows():
    assert "Drop every file whose `type` is `claude-lesson`" in ASK


def test_every_retro_lessons_table_hides_snoozed_lessons():
    text = (REPO / "dashboards" / "retro-lessons.md").read_text()
    wheres = [l for l in text.splitlines() if l.startswith("WHERE type = \"claude-lesson\"")]
    assert len(wheres) == 6
    clause = "(!snoozed_until OR date(snoozed_until) < date(today))"
    assert [l for l in wheres if clause not in l] == []
