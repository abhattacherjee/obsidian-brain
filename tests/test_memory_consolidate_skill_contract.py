"""Contract for skills/memory-consolidate/SKILL.md (#309).

The skill holds the judgement steps; the CLI holds the mechanics. These tests pin the parts
of the skill text the CLI's safety depends on: dry run before --apply, the keep/move test,
the dedupe rule, no heredoc, and no source_session on moved notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))

import memory_consolidate as mc
from skill_shell import SHELL_LANGS, fenced_blocks

SKILL = ROOT / "skills" / "memory-consolidate" / "SKILL.md"
CLI = ROOT / "hooks" / "memory_consolidate.py"
# The repo's shared scanner reads bash, sh, shell and zsh; scripts/ci-checks/no-default-db.py also reads console.
_LANGS = SHELL_LANGS | {"console"}


@pytest.fixture(scope="module")
def text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _blocks(text: str) -> list[str]:
    return [block.body for block in fenced_blocks(text, _LANGS)]


def _code(block: str) -> str:
    """The block without shell comments: ``#`` outside quotes, at a word start, to the line end."""
    out = []
    for line in block.splitlines():
        quote = None
        for i, ch in enumerate(line):
            if quote:
                quote = None if ch == quote else quote
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1].isspace()):
                line = line[:i]
                break
        out.append(line)
    return "\n".join(out)


def _writes(block: str) -> bool:
    """True when the block runs ``apply`` with --apply, --resume or an argparse abbreviation of either."""
    code = _code(block)
    return bool(re.search(r"\bapply\b", code) and re.search(r"(?<![\w-])--(?:app|res)\w*", code))


def test_block_finders_find_blocks(text):
    assert len(_blocks(text)) >= 6  # find, audit, dry run, apply, verify, smoke
    assert len(re.findall(r"```json\n(.*?)```", text, re.S)) >= 1


@pytest.mark.parametrize("fence", ["bash", "sh", "shell", "zsh", "console"])
def test_every_shell_fence_is_scanned(fence):
    assert _blocks(f"```{fence}\ncat <<EOF\nx\nEOF\n```\n") == ["cat <<EOF\nx\nEOF"]


@pytest.mark.parametrize(("block", "expected"), [
    ("python3 x.py apply --plan P --apply", True),
    ("python3 x.py apply --plan P --app", True),
    ("python3 x.py apply --plan P --appl", True),
    ("python3 x.py apply --resume B", True),
    ("python3 x.py apply --res B", True),
    ("python3 x.py apply --plan P", False),
    ("python3 x.py apply --plan P --store S", False),
    ("python3 x.py audit --store S", False),
    ("python3 x.py apply --plan P  # --apply later", False),
    ("python3 x.py apply --plan P --store 'a # b'", False),
])
def test_writes_classifier(block, expected):
    assert _writes(block) is expected


@pytest.mark.parametrize(("block", "kept"), [
    ("cmd --store '<S>' # note", "cmd --store '<S>' "),
    ("cmd # --store '<S>'", "cmd "),
    ("# whole line\ncmd", "\ncmd"),
    ("cmd '#x' \"#y\" a#b", "cmd '#x' \"#y\" a#b"),
])
def test_comment_stripper(block, kept):
    assert _code(block) == kept


def test_frontmatter_names_the_skill(text):
    assert text.startswith("---\nname: memory-consolidate\n")


@pytest.mark.parametrize("sub", ["audit", "apply", "verify", "smoke"])
def test_every_cli_subcommand_is_used_and_exists(text, sub):
    assert re.search(rf"python3 '<HOOKS>/memory_consolidate.py' {sub}\b", text), sub
    assert f'add_parser("{sub}"' in CLI.read_text(encoding="utf-8"), sub


def test_dry_run_comes_before_apply_and_every_writing_block_follows_the_approval(text):
    blocks = fenced_blocks(text, _LANGS)
    dry = [b for b in blocks if "apply --plan" in b.body and not _writes(b.body)]
    writing = [b for b in blocks if _writes(b.body)]
    assert dry and writing  # positive control: both kinds of block exist
    approval = text.index("ask before going")
    approval_line = text[:approval].count("\n") + 1
    assert dry[0].line < approval_line
    for block in writing:  # every --apply or --resume block, not just the first
        assert block.line > approval_line, block.body
    assert any("--apply" in b.body for b in writing)
    # No dry-run block abbreviates the flag: the CLI refuses an abbreviation, so it would not run.
    assert not any(re.search(r"--app\w*", _code(b.body)) for b in dry)


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


def test_verify_check_names_are_documented(text):
    """The names come from the CLI's own tuple, so a rename there fails here."""
    assert len(mc.VERIFY_CHECKS) == 8 and mc.VERIFY_CHECKS[0] == "state"
    step = text[text.index("### Step 7"):text.index("### Step 8")]
    for name in mc.VERIFY_CHECKS:
        assert f"`{name}`" in step, name


def test_smoke_checks_the_moved_note(text):
    assert "--expect '<note path>'" in text


def _store_checked(blocks: list[str]) -> tuple[int, list[str]]:
    """How many apply/verify commands the blocks hold, and which of them lack a live --store flag.
    A flag in a shell comment does not count (X-025)."""
    seen, missing = 0, []
    for block in blocks:
        code = _code(block)
        if re.search(r"memory_consolidate\.py' (apply --plan|verify --backup)", code):
            seen += 1
            if "--store '<STORE>'" not in code:
                missing.append(block)
    return seen, missing


def test_apply_and_verify_pass_the_audited_store(text):
    """apply resolves the store of the current directory unless told (X-014); the skill must tell it."""
    assert _store_checked(_blocks(text)) == (3, [])  # dry run, apply, verify


def test_a_commented_out_store_flag_fails_the_store_check():
    live = "python3 '<HOOKS>/memory_consolidate.py' apply --plan '<PLAN>' --store '<STORE>'"
    commented = "python3 '<HOOKS>/memory_consolidate.py' apply --plan '<PLAN>' # --store '<STORE>'"
    assert _store_checked([live]) == (1, [])
    assert _store_checked([commented]) == (1, [commented])


def test_smoke_exit_codes_are_documented(text):
    step = text[text.index("### Step 8"):text.index("### Step 9")]
    assert "Exit 2" in step
    assert "smoke: 0 hit(s)" in step and "`MISSING <note path>`" in step


def test_resume_lock_and_leftover_messages_are_documented(text):
    step = text[text.index("### Step 6"):text.index("### Step 7")]
    for phrase in ("apply --resume", "another run holds the lock", "WARN leftover partial backup",
                   "WARN stale lock", "pass --project", "nothing to do"):
        assert phrase in step, phrase
