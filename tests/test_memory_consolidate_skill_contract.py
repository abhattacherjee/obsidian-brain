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


_SHELL_FENCE = r"```(?:bash|sh|shell|zsh)\n(.*?)```"


def _blocks(text: str) -> list[str]:
    return re.findall(_SHELL_FENCE, text, re.S)


def _writes(block: str) -> bool:
    """True when the block runs ``apply`` with --apply or any argparse abbreviation of it."""
    return bool(re.search(r"\bapply\b", block) and re.search(r"(?<![\w-])--app\w*", block))


def test_block_finders_find_blocks(text):
    assert len(_blocks(text)) >= 6  # find, audit, dry run, apply, verify, smoke
    assert len(re.findall(r"```json\n(.*?)```", text, re.S)) >= 1


@pytest.mark.parametrize("fence", ["bash", "sh", "shell", "zsh"])
def test_every_shell_fence_is_scanned(fence):
    assert _blocks(f"```{fence}\ncat <<EOF\nx\nEOF\n```\n") == ["cat <<EOF\nx\nEOF\n"]


@pytest.mark.parametrize(("block", "expected"), [
    ("python3 x.py apply --plan P --apply", True),
    ("python3 x.py apply --plan P --app", True),
    ("python3 x.py apply --plan P --appl", True),
    ("python3 x.py apply --plan P", False),
    ("python3 x.py apply --plan P --store S", False),
    ("python3 x.py audit --store S", False),
])
def test_writes_classifier(block, expected):
    assert _writes(block) is expected


def test_frontmatter_names_the_skill(text):
    assert text.startswith("---\nname: memory-consolidate\n")


@pytest.mark.parametrize("sub", ["audit", "apply", "verify", "smoke"])
def test_every_cli_subcommand_is_used_and_exists(text, sub):
    assert re.search(rf"python3 '<HOOKS>/memory_consolidate.py' {sub}\b", text), sub
    assert f'add_parser("{sub}"' in CLI.read_text(encoding="utf-8"), sub


def test_dry_run_comes_before_apply(text):
    blocks = _blocks(text)
    assert blocks
    dry = next(i for i, b in enumerate(blocks) if "apply --plan" in b and not _writes(b))
    real = next(i for i, b in enumerate(blocks) if "apply --plan" in b and _writes(b))
    assert dry < real
    # Neither block abbreviates the flag: the CLI refuses an abbreviation, so it would not run.
    assert "--apply" in blocks[real]
    assert not re.search(r"--app\w*", blocks[dry])
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
    blocks = _blocks(text)
    assert blocks
    for block in blocks:
        assert "<<" not in block


def test_no_source_session_in_plan_or_commands(text):
    json_blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    assert json_blocks and _blocks(text)
    for block in _blocks(text) + json_blocks:
        assert "source_session" not in block
    assert "no `source_session`" in text


def test_verify_check_names_are_documented_and_exist(text):
    cli = CLI.read_text(encoding="utf-8")
    for name in (
        "index-links-resolve", "no-duplicates", "all-indexed", "removed-equals-moved",
        "notes-lossless", "backups-present", "sizes",
    ):
        assert f"`{name}`" in text, name
        assert f'"{name}"' in cli, name


def test_smoke_checks_the_moved_note(text):
    assert "--expect '<note path>'" in text


def test_apply_and_verify_pass_the_audited_store(text):
    """apply resolves the store of the current directory unless told (X-014); the skill must tell it."""
    seen = []
    for block in _blocks(text):
        if re.search(r"memory_consolidate\.py' (apply --plan|verify --backup)", block):
            seen.append(block)
            assert "--store '<STORE>'" in block, block
    assert len(seen) == 3  # dry run, apply, verify


def test_smoke_exit_codes_are_documented(text):
    step = text[text.index("### Step 8"):text.index("### Step 9")]
    assert "Exit 2" in step
    assert "smoke: 0 hit(s)" in step and "`MISSING <note path>`" in step
