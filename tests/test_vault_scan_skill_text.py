"""The /vault-ask and /vault-search skills use hooks/vault_scan.py where a
fixed-line Read or a missing Grep tool used to lose data (#312, #375).

Skills are prose the model follows, so these tests pin the prose: most
instructions are checked inside the step they belong to, not anywhere in the
file, so moving a sentence to the wrong step fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILLS = {
    "vault-ask": REPO / "skills" / "vault-ask" / "SKILL.md",
    "vault-search": REPO / "skills" / "vault-search" / "SKILL.md",
}

_FALLBACK_HEAD = "**If the Grep tool is not available in this session**"
_FALLBACK_CMD = {
    "vault-ask": (
        "python3 \"$HOOKS/vault_scan.py\" grep '<vault_path>' '<sessions_folder>' "
        "'<insights_folder>' --pattern='<term>' --ignore-case"
    ),
    "vault-search": (
        "python3 \"$HOOKS/vault_scan.py\" grep '<vault_path>' '<sessions_folder>' "
        "'<insights_folder>' --pattern='<pattern>' --ignore-case"
    ),
}
_META_CMD = "python3 \"$HOOKS/vault_scan.py\" meta '<vault_path>' '<file_1>' '<file_2>'"
_NO_GREP_FIRST = (
    "If the Grep tool is not in your tool list, go straight to vault_scan.py grep "
    "— do not call Grep first."
)
_SKIPPED_LINE = "K note(s) were not searched (see the breakdown) — run /vault-doctor"


def _step(skill: str, number: str) -> str:
    text = SKILLS[skill].read_text(encoding="utf-8")
    m = re.search(
        rf"^### Step {re.escape(number)} — .*?(?=^### Step |\Z)", text, re.M | re.S,
    )
    assert m, f"{skill}: Step {number} not found"
    return m.group(0)


def _fallback_section(skill: str) -> str:
    """From the fallback paragraph to the end of the code block that follows it."""
    step = _step(skill, "4")
    start = step.index(_FALLBACK_HEAD)
    block_open = step.index("```bash\n", start)
    block_close = step.index("\n```", block_open + len("```bash\n"))
    return step[start:block_close]


def _scan_calls(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith('python3 "$HOOKS/vault_scan.py"')]


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_step4_fallback_command_is_exact(skill):
    section = _fallback_section(skill)
    assert _scan_calls(section) == [_FALLBACK_CMD[skill]]


def test_vault_search_tag_command_does_not_satisfy_the_fallback_pin():
    # The tag-mode block sits before the fallback paragraph; the slice must
    # not reach back to it.
    section = _fallback_section("vault-search")
    assert "--frontmatter-only" not in "\n".join(_scan_calls(section))


def test_vault_ask_step4_keeps_the_agent3_tag_search():
    step = _step("vault-ask", "4")
    assert 'Grep(pattern="claude/topic/.*<term>", path=SESSIONS_DIR' in step
    assert 'Grep(pattern="claude/topic/.*<term>", path=INSIGHTS_DIR' in step
    assert "--pattern='claude/topic/.*<term>'" in _fallback_section("vault-ask")


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_step4_says_skip_grep_when_it_is_not_in_the_tool_list(skill):
    step = _step(skill, "4")
    assert _NO_GREP_FIRST in step
    # Said before the first Grep( call, so it is read before one is made.
    assert step.index(_NO_GREP_FIRST) < step.index("Grep(")


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_step4_grep_success_check_and_skipped_line(skill):
    step = _step(skill, "4")
    assert "exited 0 and stderr has the `vault_scan: " in step
    assert "not \"no match\"" in step
    assert _SKIPPED_LINE in step


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_step5_reads_metadata_with_exact_meta_command(skill):
    step = _step(skill, "5")
    assert _scan_calls(step) == [_META_CMD]
    assert "Read(" not in step
    assert "exited 0 and printed one JSON row per file" in step


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_no_fixed_line_frontmatter_reads(skill):
    steps = ("4", "5", "5b", "6") if skill == "vault-search" else ("4", "5", "6")
    text = "\n".join(_step(skill, n) for n in steps)
    hits = re.findall(r"first \d+ lines|limit=\d+", text)
    assert not hits, hits


@pytest.mark.parametrize("skill,number", [("vault-ask", "5"), ("vault-search", "5")])
def test_rationale_says_frontmatter_runs_past_a_fixed_limit(skill, number):
    step = _step(skill, number)
    # \b: "past line 400" (the old, wrong limit) must not satisfy this.
    assert re.search(r"past line 40\b", step), step
    assert "fixed line limit silently drops fields" in step


def test_vault_search_tag_mode_is_frontmatter_only():
    assert "--frontmatter-only" in _step("vault-search", "2")
    step4 = _step("vault-search", "4")
    tag_part = step4.split("**For tag mode:**", 1)[1].split("**For structured mode:**", 1)[0]
    # The command itself, not just the prose, must carry the flag.
    cmds = _scan_calls(tag_part)
    assert len(cmds) == 1, cmds
    assert cmds[0].split()[-1] == "--frontmatter-only"
    assert "Grep(" not in tag_part


def test_vault_search_tag_pattern_is_a_regex():
    assert ("Pattern: the tag string, used as a regex (escape `.`, `+` and other "
            "regex characters)") in _step("vault-search", "2")


def test_vault_search_step5_null_and_empty_rules():
    assert ("A missing `date`, `type`, `project`, `session_id` or `source_session_note` "
            "is `null`; missing or empty `tags` is `[]`; `title` falls back to the "
            "filename.") in _step("vault-search", "5")


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_vault_scan_blocks_resolve_hooks_and_check_the_script(skill):
    text = SKILLS[skill].read_text(encoding="utf-8")
    blocks = re.findall(r"^```bash\n(.*?)^```", text, re.M | re.S)
    scan_blocks = [b for b in blocks if "vault_scan.py" in b]
    assert len(scan_blocks) >= 2, skill
    for b in scan_blocks:
        assert b.startswith('cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"\n')
        assert '\nHOOKS=$(python3 -c "\n' in b
        assert 'test -f "$HOOKS/vault_scan.py" ||' in b


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_vault_scan_calls_single_quote_pasted_values(skill):
    # #386: values pasted from earlier steps go in single quotes, never "$VAR".
    text = SKILLS[skill].read_text(encoding="utf-8")
    calls = _scan_calls(text)
    assert calls, skill
    for ln in calls:
        rest = ln[len('python3 "$HOOKS/vault_scan.py"'):]
        assert '"' not in rest, ln
        assert "'<vault_path>'" in rest, ln


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_pattern_uses_the_equals_form(skill):
    # `--pattern '<x>'` breaks when x starts with "-": argparse reads it as a flag.
    text = SKILLS[skill].read_text(encoding="utf-8")
    assert "--pattern '" not in text
    grep_calls = [c for c in _scan_calls(text) if " grep " in c]
    assert grep_calls, skill
    for ln in grep_calls:
        assert "--pattern='" in ln, ln


def test_vault_import_reads_whole_frontmatter_for_session_ids():
    """#312: vault-import used `head -20` to collect session ids, which
    silently misses a session_id below line 20 and re-imports the session."""
    text = (REPO / "skills" / "vault-import" / "SKILL.md").read_text(encoding="utf-8")
    assert "head -20" not in text
    assert """awk 'NR==1 { if ($0 != "---") exit; next } $0 == "---" { exit } { print }'""" in text
