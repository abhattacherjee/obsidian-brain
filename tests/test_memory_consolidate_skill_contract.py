"""Contract for skills/memory-consolidate/SKILL.md (#309).

The skill holds the judgement steps; the CLI holds the mechanics. These tests pin the parts
of the skill text the CLI's safety depends on: dry run before --apply, the keep/move test,
the dedupe rule, no heredoc, and no source_session on moved notes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "memory-consolidate" / "SKILL.md"
CLI = ROOT / "hooks" / "memory_consolidate.py"


@pytest.fixture(scope="module")
def text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def test_frontmatter_names_the_skill(text):
    assert text.startswith("---\nname: memory-consolidate\n")


@pytest.mark.parametrize("sub", ["audit", "apply", "verify", "smoke"])
def test_every_cli_subcommand_is_used_and_exists(text, sub):
    assert re.search(rf"python3 '<HOOKS>/memory_consolidate.py' {sub}\b", text), sub
    assert f'add_parser("{sub}"' in CLI.read_text(encoding="utf-8"), sub


def test_dry_run_comes_before_apply(text):
    blocks = _blocks(text)
    dry = next(i for i, b in enumerate(blocks) if "apply --plan" in b and "--apply" not in b)
    real = next(i for i, b in enumerate(blocks) if "apply --plan" in b and "--apply" in b)
    assert dry < real
    between = text[text.index(blocks[dry]):text.index(blocks[real])]
    assert "ask before going" in between


def test_keep_move_test_is_stated(text):
    for phrase in (
        "deploy and live-service rules",
        "approval limits",
        "hook and\n  preflight mechanics",
        "daily tool gotchas",
        "one-off design lessons, general testing lessons",
        "global `CLAUDE.md`",
    ):
        assert phrase in text, phrase


def test_dedupe_rule_is_stated(text):
    assert "A hit counts only when the matching note is **about that\nlesson**" in text


def test_no_heredoc_in_any_block(text):
    for block in _blocks(text):
        assert "<<" not in block


def test_no_source_session_in_plan_or_commands(text):
    for block in _blocks(text) + re.findall(r"```json\n(.*?)```", text, re.S):
        assert "source_session" not in block
    assert "no `source_session`" in text


def test_smoke_checks_the_moved_note(text):
    assert "--expect '<note path>'" in text
