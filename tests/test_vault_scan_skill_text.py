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
        "'<insights_folder>' '<wiki_folder>' --pattern='<term>' --ignore-case"
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


def _scan_calls(text):
    return [line for line in text.splitlines() if line.startswith('python3 "$OB_RESOURCE_ROOT/hooks/brain_cli.py"') and ("--operation 'grep'" in line or "--operation 'metadata'" in line)]



@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_step4_fallback_command_is_exact(skill):
    section=_fallback_section(skill)
    calls=_scan_calls(section)
    assert len(calls)==1 and "--operation 'grep'" in calls[0]
    assert '< "$REQUEST_PATH"' in calls[0]
    assert '"pattern": "<pattern>"' in section and '"ignore_case": true' in section



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
    step=_step(skill,'5')
    calls=_scan_calls(step)
    assert len(calls)==1 and "--operation 'metadata'" in calls[0]
    assert '"paths"' in step and 'Read(' not in step
    assert 'exited 0 and printed one JSON row per file' in step



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
    assert '--frontmatter-only' in _step('vault-search','2')
    part=_step('vault-search','4').split('**For tag mode:**',1)[1].split('**For structured mode:**',1)[0]
    calls=_scan_calls(part)
    assert len(calls)==1 and "--operation 'grep'" in calls[0]
    assert '"frontmatter_only": true' in part
    assert 'Grep(' not in part



def test_vault_search_tag_pattern_is_a_regex():
    assert ("Pattern: the tag string, used as a regex (escape `.`, `+` and other "
            "regex characters)") in _step("vault-search", "2")


def test_vault_search_step5_null_and_empty_rules():
    assert ("A missing `date`, `type`, `project`, `session_id` or `source_session_note` "
            "is `null`; missing or empty `tags` is `[]`; `title` falls back to the "
            "filename.") in _step("vault-search", "5")


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_vault_scan_blocks_resolve_hooks_and_check_the_script(skill):
    text=SKILLS[skill].read_text()
    calls=_scan_calls(text)
    assert len(calls)>=2
    for call in calls:
        assert '--resource-root "$OB_RESOURCE_ROOT"' in call
        assert '--skill-path "$OB_SKILL_PATH"' in call
    assert 'p.is_absolute()' in text
    assert 'known_marketplaces.json' not in text and 'default="hooks"' not in text



@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_vault_scan_calls_single_quote_pasted_values(skill):
    text=SKILLS[skill].read_text()
    calls=_scan_calls(text)
    assert calls
    for call in calls:
        assert '< "$REQUEST_PATH"' in call
        assert '<pattern>' not in call and '<term>' not in call
    assert 'Content is a JSON string, never shell code.' in text



@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_pattern_uses_the_equals_form(skill, tmp_path, monkeypatch):
    import skill_procedures, vault_scan
    from note_transactions import context_for_vault
    captured=[]
    monkeypatch.setattr(vault_scan,'main',lambda argv: captured.append(argv) or 0)
    pattern='-dangerous "quotes" $(touch marker)'
    skill_procedures._grep(context_for_vault(tmp_path), {'pattern':pattern})
    assert '--pattern='+pattern in captured[0]
    assert '--pattern' not in captured[0]
    assert any("--operation 'grep'" in line for line in _scan_calls(SKILLS[skill].read_text()))



def test_vault_import_reads_whole_frontmatter_for_session_ids(tmp_path):
    import time
    import session_lookup
    text=(REPO / 'skills/vault-import/SKILL.md').read_text()
    assert 'head -20' not in text
    assert 'complete bounded frontmatter' in text and 'full provider/native ID' in text
    folder=tmp_path / 'claude-sessions'
    folder.mkdir()
    note=folder / 'historic.md'
    note.write_text('---\ntype: claude-session\n' + ''.join('custom_%s: value\n' % i for i in range(80)) + 'session_id: deeply-nested-native-id\n---\nBody\n')
    fields=session_lookup._identity(note, tmp_path, folder, time.monotonic()+1)
    assert fields['session_id']=='deeply-nested-native-id'
